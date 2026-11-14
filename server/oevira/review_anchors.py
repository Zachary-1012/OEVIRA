"""Resolve review targets through the original result's native object owner."""
from __future__ import annotations

import difflib
import hashlib
import json

from .api import ApiError


def _reject(code, message):
    raise ApiError(code, message, 409)


def load_review_version(connection, workspace_id, artifact_id, version):
    if type(version) is not int or version < 1:
        raise ApiError("INVALID_INPUT", "Select a positive saved result version", 400)
    row = connection.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                             (artifact_id, workspace_id, version)).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "The selected result version was not found", 404)
    return dict(row)


def load_media_review_version(connection, identity, project_id, version, *, write=False):
    from .media_api import require_media_review_access

    row = require_media_review_access(connection, identity, project_id, version, write=write)
    return {**dict(row), "_review_kind": "media_project"}


def verify_review_version(connection, version):
    if version.get("_review_kind") == "media_project":
        current = connection.execute("SELECT state_json,parent_version FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                                     (version["project_id"], version["workspace_id"], version["version"])).fetchone()
        if current is None or any(current[field] != version[field] for field in ("state_json", "parent_version")):
            _reject("ANCHOR_SOURCE_CHANGED", "The saved Media review source changed while it was being checked")
        return
    current = load_review_version(connection, version["workspace_id"], version["artifact_id"], version["version"])
    if any(current[field] != version[field] for field in ("title", "body", "input_snapshot_json")):
        _reject("ANCHOR_SOURCE_CHANGED", "The saved review source changed while it was being checked")


def verify_review_sources(connection, identity, version):
    if version.get("_review_kind") == "media_project":
        load_media_review_version(connection, identity, version["project_id"], version["version"])
        return
    from .collaboration import require_artifact_access, require_material_access, work_commission

    artifact = require_artifact_access(connection, identity, version["artifact_id"])
    snapshot = json.loads(version["input_snapshot_json"] or "{}")
    material_ids = {item.get("material_id") or item.get("id") for item in snapshot.get("materials", []) if isinstance(item, dict)}
    for kind in ("document", "presentation", "spreadsheet", "pdf_document"):
        source = snapshot.get(kind, {}).get("source", {}) if isinstance(snapshot.get(kind), dict) else {}
        if source.get("material_id"):
            material_ids.add(source["material_id"])
    for material_id in material_ids - {None}:
        material = require_material_access(connection, identity, material_id, target_work_id=artifact["work_id"])
        source_scope = work_commission(connection, material["work_id"])
        if source_scope and source_scope != work_commission(connection, artifact["work_id"]):
            raise ApiError("CLIENT_SCOPE_MISMATCH", "The native review source belongs to a different client", 403)


def comment_revision_replay(connection, workspace_id, comment_id, key, fingerprint):
    if key is None:
        return None
    receipt = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation=? AND key=?",
                                 (workspace_id, f"revise_comment:{comment_id}", key)).fetchone()
    if receipt is None:
        return None
    if receipt["request_hash"] != fingerprint:
        _reject("IDEMPOTENCY_CONFLICT", "This key was already used for a different review decision")
    version = connection.execute("SELECT snapshot_json FROM artifact_comment_versions WHERE comment_id=? AND version=?",
                                 (comment_id, receipt["resource_id"])).fetchone()
    if version is None:
        _reject("COMMENT_REPLAY_UNAVAILABLE", "The saved review decision is unavailable; its receipt was preserved")
    return json.loads(version["snapshot_json"])


def _text_selection(text, anchor):
    quote = anchor.get("quote")
    start, end = anchor.get("start"), anchor.get("end")
    if anchor.get("unit", "unicode_codepoint") != "unicode_codepoint":
        _reject("ANCHOR_UNIT_MISMATCH", "Text review positions use Unicode code points")
    if quote is None and start is None and end is None:
        return {"start": 0, "end": len(text), "quote": text, "unit": "unicode_codepoint"}
    if not isinstance(quote, str) or not quote:
        _reject("INVALID_ANCHOR", "Select the exact text on the saved object")
    if start is None and end is None:
        positions, offset = [], 0
        while (position := text.find(quote, offset)) >= 0:
            positions.append(position)
            offset = position + 1
        if len(positions) != 1:
            _reject("ANCHOR_AMBIGUOUS", "Specify the exact occurrence of the selected text")
        start, end = positions[0], positions[0] + len(quote)
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] != quote):
        _reject("ANCHOR_MISMATCH", "The selected text does not match that saved version")
    return {"start": start, "end": end, "quote": quote, "unit": "unicode_codepoint"}


def _document_objects(store, version, identity):
    from .documents_api import render_document_export
    from .documents import inspect_document

    content = render_document_export(store, version, identity=identity)
    return inspect_document(content), hashlib.sha256(content).hexdigest()


def _native_value(store, version, anchor, *, mapped=False, identity=None):
    snapshot = json.loads(version["input_snapshot_json"] or "{}")
    kind = anchor["kind"]
    if snapshot.get("kind") != kind:
        _reject("ANCHOR_KIND_MISMATCH", "This review target does not belong to the selected result type")
    prop = anchor.get("property", "value" if kind == "spreadsheet" else "annotations" if kind == "pdf_document" else "text")
    locator = anchor.get("locator") if mapped else None
    if mapped and not isinstance(locator, dict):
        _reject("ANCHOR_MAPPING_UNAVAILABLE", "The saved native target has no confirmed object mapping")
    native = snapshot.get(kind, {})
    origin = native.get("source", {}).get("sha256") if isinstance(native, dict) and isinstance(native.get("source"), dict) else None
    if mapped and anchor.get("origin_sha256") is not None and anchor["origin_sha256"] != origin:
        _reject("ANCHOR_MAPPING_UNAVAILABLE", "The native result now comes from a different original file")
    if kind == "document":
        inspection, digest = _document_objects(store, version, identity)
        matches = [item for item in inspection["objects"] if (item["location"] == locator if mapped else item["id"] == anchor.get("object_id"))]
        if len(matches) != 1 or prop != "text":
            _reject("ANCHOR_TARGET_MISSING", "The selected document object or property cannot be resolved")
        item = matches[0]
        return {"kind": kind, "property": prop, "object_id": item["id"], "locator": item["location"], "source_sha256": digest, "origin_sha256": origin}, item["text"]
    if kind == "presentation":
        from .office_api import render_presentation_export
        from .office import inspect_presentation

        content = render_presentation_export(store, version, identity=identity)
        selected_slide = locator["slide"] if mapped else anchor.get("slide")
        object_id = locator["object_id"] if mapped else anchor.get("object_id")
        matches = [item for slide in inspect_presentation(content)["slides"] if slide["number"] == selected_slide
                   for item in slide["objects"] if item["id"] == object_id]
        if len(matches) != 1:
            _reject("ANCHOR_TARGET_MISSING", "The selected slide object or property cannot be resolved")
        item = matches[0]
        if "property" not in anchor:
            prop = "image" if item["kind"] == "picture" else "chart_data" if "chart_data" in item else "text"
        field = "media" if prop == "image" else prop
        if prop not in {"text", "image", "chart_data", "geometry", "crop"} or field not in item or item[field] is None:
            _reject("ANCHOR_TARGET_MISSING", "The selected slide property has no resolved native value")
        canonical = {"kind": kind, "property": prop, "slide": selected_slide, "object_id": object_id,
                     "locator": {"slide": selected_slide, "object_id": object_id}, "source_sha256": hashlib.sha256(content).hexdigest(), "origin_sha256": origin}
        if prop in {"geometry", "crop"}:
            unit = "emu" if prop == "geometry" else "ooxml_crop_100000"
            if anchor.get("unit", unit) != unit:
                _reject("ANCHOR_UNIT_MISMATCH", "The selected slide property uses its native Office coordinate unit")
            canonical["unit"] = unit
            canonical["coordinate_space"] = "object_local"
        return canonical, item[field]
    if kind == "spreadsheet":
        from .office_api import render_spreadsheet_export
        from .office_spreadsheet import inspect_spreadsheet

        content = render_spreadsheet_export(store, version, identity=identity)
        inspection = inspect_spreadsheet(content)
        matches = [(sheet, cell) for sheet in inspection["sheets"] for cell in sheet["cells"]
                   if (sheet["sheet_id"] == locator["sheet_id"] and cell["address"] == locator["address"] if mapped else cell["object_id"] == anchor.get("object_id"))]
        if len(matches) != 1 or prop not in {"value", "formula"}:
            _reject("ANCHOR_TARGET_MISSING", "The selected workbook cell or property cannot be resolved")
        sheet, cell = matches[0]
        value = cell["value"]
        if prop == "formula":
            value = value.get("value") if value.get("kind") == "formula" else None
        return {"kind": kind, "property": prop, "object_id": cell["object_id"], "source_sha256": inspection["sha256"],
                "locator": {"sheet_id": sheet["sheet_id"], "address": cell["address"]}, "origin_sha256": origin}, value
    if kind == "pdf_document":
        from .office_api import render_pdf_revision_export
        from .office_pdf import inspect_pdf

        content = render_pdf_revision_export(store, version, identity=identity)
        inspection = inspect_pdf(content)
        page_number = locator["page"] if mapped else anchor.get("page")
        rect = locator["rect"] if mapped else anchor.get("rect")
        if (type(page_number) is not int or not 1 <= page_number <= inspection["page_count"]
                or not isinstance(rect, list) or len(rect) != 4
                or any(type(value) not in {int, float} for value in rect) or prop != "annotations"):
            _reject("INVALID_ANCHOR", "Select an exact PDF page region in PDF user-space points")
        import math

        page = inspection["pages"][page_number - 1]
        page_basis = {field: page.get(field) for field in ("text", "bbox", "mediabox", "cropbox", "rotation")}
        page_digest = hashlib.sha256(json.dumps(page_basis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if mapped and locator.get("page_basis_sha256") != page_digest:
            _reject("ANCHOR_MAPPING_UNAVAILABLE", "The selected PDF page changed; locate the comment again instead of reusing its old page number")
        left, bottom, right, top = rect
        bounds = page["bbox"]
        if (not all(math.isfinite(value) for value in rect)
                or not bounds[0] <= left < right <= bounds[2] or not bounds[1] <= bottom < top <= bounds[3]):
            _reject("ANCHOR_MISMATCH", "The PDF region is outside the selected page")
        values = []
        for item in page["annotations"]:
            box = item.get("rect")
            if box and max(left, box[0]) < min(right, box[2]) and max(bottom, box[1]) < min(top, box[3]):
                values.append({"rect": box, "contents": item.get("contents"), "subtype": item.get("subtype")})
        return {"kind": kind, "property": prop, "page": page_number, "rect": rect,
                "locator": {"page": page_number, "rect": rect, "page_basis_sha256": page_digest}, "source_sha256": hashlib.sha256(content).hexdigest(), "origin_sha256": origin,
                "coordinate_space": "pdf_user_space_points_bottom_left"}, values
    if kind == "research":
        prop = anchor.get("property", "statement")
        proposition_id = locator["proposition_id"] if mapped else anchor.get("proposition_id")
        matches = [item for item in snapshot.get("research", {}).get("propositions", []) if item.get("proposition_id") == proposition_id]
        if len(matches) != 1 or prop not in {"statement", "reasoning", "assumptions", "supporting_refs", "counterevidence_refs"}:
            _reject("ANCHOR_TARGET_MISSING", "The selected research proposition or property cannot be resolved")
        return {"kind": kind, "property": prop, "proposition_id": proposition_id,
                "locator": {"proposition_id": proposition_id}}, matches[0].get(prop)
    if kind == "business_analysis":
        prop = anchor.get("property", "body")
        section_id = locator["section_id"] if mapped else anchor.get("section_id")
        matches = [item for item in snapshot.get("business", {}).get("result", {}).get("sections", []) if item.get("id") == section_id]
        if len(matches) != 1 or prop not in {"body", "client_body", "title"}:
            _reject("ANCHOR_TARGET_MISSING", "The selected business section or property cannot be resolved")
        return {"kind": kind, "property": prop, "section_id": section_id,
                "locator": {"section_id": section_id}}, matches[0].get(prop)
    _reject("ANCHOR_KIND_UNSUPPORTED", "Use this object's supported native review target")


def _review_value(store, identity, version, anchor, *, mapped=False):
    if version.get("_review_kind") == "media_project":
        from .media_api import read_media_review_value

        return read_media_review_value(store, identity, version["project_id"], version["version"], anchor, mapped=mapped)
    return _native_value(store, version, anchor, mapped=mapped, identity=identity)


def validate_review_anchor(store, version, anchor, *, identity=None):
    if not isinstance(anchor, dict):
        _reject("INVALID_ANCHOR", "A review target must be an object")
    if version.get("_review_kind") == "media_project":
        canonical, _value = _review_value(store, identity, version, anchor)
        return canonical
    if "kind" not in anchor:
        if set(anchor) != {"start", "end", "quote"}:
            _reject("INVALID_ANCHOR", "Use an exact Unicode text span on the selected saved version")
        snapshot = json.loads(version["input_snapshot_json"] or "{}")
        if snapshot.get("kind") in {"document", "presentation", "spreadsheet", "pdf_document"}:
            _reject("NATIVE_ANCHOR_REQUIRED", "Select the native file object instead of its revision summary")
        selection = _text_selection(version["body"], anchor)
        return {key: selection[key] for key in ("start", "end", "quote")}
    allowed = {"kind", "property", "object_id", "slide", "page", "rect", "proposition_id", "section_id", "quote", "start", "end", "unit", "source_sha256", "locator", "origin_sha256", "coordinate_space"}
    if (set(anchor) - allowed or not isinstance(anchor["kind"], str)
            or ("property" in anchor and not isinstance(anchor["property"], str))):
        _reject("INVALID_ANCHOR", "The review target contains an unsupported field")
    canonical, value = _native_value(store, version, anchor, identity=identity)
    if anchor.get("source_sha256") is not None and anchor["source_sha256"] != canonical.get("source_sha256"):
        _reject("ANCHOR_SOURCE_CHANGED", "The review target belongs to a different native file")
    for field in ("locator", "origin_sha256", "coordinate_space"):
        if field in anchor and anchor[field] != canonical.get(field):
            _reject("ANCHOR_MAPPING_UNAVAILABLE", "The supplied native locator does not match the selected saved object")
    if not isinstance(value, str) and any(field in anchor for field in ("quote", "start", "end")):
        _reject("INVALID_ANCHOR", "This native property is selected by its object identity rather than text offsets")
    if isinstance(value, str) and canonical["property"] in {"text", "statement", "reasoning", "body", "client_body", "title"}:
        canonical.update(_text_selection(value, anchor))
    return canonical


def _changed_text(before, after, start, end, *, explicit_target=False):
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(before), len(after)) - prefix
           and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]):
        suffix += 1
    old_core = before[prefix:len(before) - suffix if suffix else len(before)]
    new_core = after[prefix:len(after) - suffix if suffix else len(after)]
    if len(old_core) * len(new_core) > 20_000_000:
        _reject("COMMENT_MAPPING_REVIEW_REQUIRED", "The changes are too broad to map this old text selection reliably")
    changes = []
    for operation, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(None, old_core, new_core, autojunk=False).get_opcodes():
        if operation == "equal":
            continue
        old_start, old_end, new_start, new_end = (value + prefix for value in (old_start, old_end, new_start, new_end))
        if operation == "insert":
            intersects = start <= old_start <= end if explicit_target or start == end else start < old_start < end
        else:
            intersects = max(start, old_start) < min(end, old_end)
        if intersects:
            changes.append({"operation": operation, "before_start": old_start, "before_end": old_end,
                            "after_start": new_start, "after_end": new_end,
                            "before": before[old_start:old_end], "after": after[new_start:new_end]})
    return changes


def review_resolution_evidence(store, source, result, anchor, result_anchor=None, *, identity=None):
    confirmed_target = validate_review_anchor(store, result, result_anchor, identity=identity) if result_anchor is not None else None
    if "kind" not in anchor:
        if confirmed_target is not None and "kind" in confirmed_target:
            _reject("ANCHOR_KIND_MISMATCH", "Use the same kind of review target in the revised version")
        before, after = source["body"], result["body"]
        changes = _changed_text(before, after, anchor["start"], anchor["end"], explicit_target=confirmed_target is not None)
        target = {"kind": "text_span", "unit": "unicode_codepoint"}
    else:
        original_target, before = _review_value(store, identity, source, anchor, mapped=True)
        if source.get("_review_kind") == "media_project" and confirmed_target is not None:
            if any(confirmed_target.get(field) != original_target.get(field) for field in ("kind", "object_id", "property")):
                _reject("ANCHOR_TARGET_MISMATCH", "The resolution must identify the same Media object and property")
            target, after = _review_value(store, identity, result, confirmed_target, mapped=True)
        else:
            target, after = _review_value(store, identity, result, anchor, mapped=True)
            if confirmed_target is not None and (confirmed_target.get("locator") != target["locator"] or confirmed_target.get("property") != target["property"]):
                _reject("ANCHOR_TARGET_MISMATCH", "The resolution must identify the same native object and property")
        if isinstance(before, str) and isinstance(after, str) and "start" in anchor:
            changes = _changed_text(before, after, anchor["start"], anchor["end"], explicit_target=confirmed_target is not None)
        else:
            changes = [{"property": anchor["property"], "before": before, "after": after}] if before != after else []
    if confirmed_target is not None:
        if "start" in confirmed_target:
            changes = [change for change in changes if "after_start" not in change
                       or max(confirmed_target["start"], change["after_start"]) < min(confirmed_target["end"], change["after_end"])
                       or (change["after_start"] == change["after_end"] and confirmed_target["start"] <= change["after_start"] <= confirmed_target["end"])]
        target = confirmed_target
    if not changes:
        _reject("COMMENT_TARGET_UNCHANGED", "No verified change maps to the selected review target; explain a non-adoption or select the exact revised target")
    return {"source_version": source["version"], "result_version": result["version"], "target": target, "changes": changes}
