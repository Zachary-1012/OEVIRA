"""Exact source substitutions for the existing business input snapshot."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from .api import ApiError, _not_found, _string
from .collaboration import require_material_access, require_work_access, work_commission


def normalized_table_bindings(bindings):
    if not isinstance(bindings, list):
        raise ApiError("INVALID_INPUT", "tables must be an array", 400)
    normalized, seen = [], set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ApiError("INVALID_INPUT", "Each table needs a saved source binding", 400)
        item = deepcopy(binding)
        identifier = item.setdefault("input_id", f"table:{item.get('target', '')}:{index + 1}")
        _string(identifier, "input_id", maximum=200)
        if identifier in seen or identifier == "source":
            raise ApiError("SOURCE_INPUT_CONFLICT", "Each saved input has one stable identity", 409)
        seen.add(identifier)
        normalized.append(item)
    return normalized


def input_source_catalog(business):
    sources = []
    for binding, saved in zip(business.get("table_bindings", []), business.get("table_sources", [])):
        sources.append({"input_id": binding["input_id"], "target": binding["target"], "kind": "table",
                        "ref": deepcopy(binding.get("source_ref") or {"kind": "material", "id": saved["material_id"], "version": saved["sha256"]})})
    source = business.get("inputs", {}).get("source")
    if business.get("operation") == "financial_source_analysis" and isinstance(source, dict):
        sources.append({"input_id": "source", "target": "source", "kind": "financial_original",
                        "ref": {"kind": "material", "id": source["material_id"], "version": source["material_sha256"]}})
    return sources


def _resolve_ref(connection, identity, work_id, reference):
    if not isinstance(reference, dict) or not {"kind", "id", "version"} <= reference.keys() or set(reference) - {"kind", "id", "version", "observation_id"}:
        raise ApiError("INVALID_SOURCE_REFERENCE", "A source needs its exact kind, identity and saved version", 400)
    kind = _string(reference["kind"], "source.kind", maximum=40)
    identifier = _string(reference["id"], "source.id", maximum=200)
    version = _string(reference["version"], "source.version", maximum=200)
    if kind == "material":
        if "observation_id" in reference:
            raise ApiError("INVALID_SOURCE_REFERENCE", "A material reference cannot name another observation", 400)
        material_id = identifier
    elif kind in {"observation", "source"}:
        observation_id = identifier if kind == "observation" else reference.get("observation_id")
        if not observation_id:
            raise ApiError("SOURCE_SNAPSHOT_REQUIRED", "Choose a retained observation of this source, not its changing latest page", 409)
        observation = connection.execute("SELECT * FROM monitor_observations WHERE id=? AND workspace_id=?", (observation_id, identity["workspace"]["id"])).fetchone()
        if observation is None:
            raise _not_found()
        if kind == "source" and observation["source_id"] != identifier:
            raise ApiError("SOURCE_REFERENCE_MISMATCH", "The observation belongs to a different source", 409)
        monitor = connection.execute("SELECT * FROM monitor_rules WHERE id=? AND workspace_id=?", (observation["monitor_id"], identity["workspace"]["id"])).fetchone()
        if monitor is None:
            raise _not_found()
        if monitor["work_id"]:
            require_work_access(connection, identity, monitor["work_id"])
        source_scope = work_commission(connection, monitor["work_id"])
        if source_scope and source_scope != work_commission(connection, work_id):
            raise ApiError("CLIENT_SCOPE_MISMATCH", "The source observation belongs to another client commission", 403)
        if version != observation["sha256"]:
            raise ApiError("SOURCE_VERSION_CONFLICT", "The observation does not match its selected saved version", 409)
        material_id = observation["material_id"]
    else:
        raise ApiError("INVALID_SOURCE_REFERENCE", "Use a saved Material, Source or Observation reference", 400)
    material = require_material_access(connection, identity, material_id, target_work_id=work_id)
    if material["sha256"] != version:
        raise ApiError("SOURCE_VERSION_CONFLICT", "The source no longer matches the selected saved version", 409)
    return material


def prepare_source_revision(store, connection, identity, work_id, business, changes, *, legacy_revision):
    """Resolve sources through their owner and reparse affected table inputs only."""
    from .business_execution import _hash, _resolve_tables
    source_changes = changes.get("source_changes", [])
    if not isinstance(source_changes, list) or not source_changes:
        raise ApiError("INVALID_SOURCE_CHANGES", "Provide at least one exact source change", 400)
    candidate = deepcopy(business)
    candidate["table_bindings"] = normalized_table_bindings(candidate.get("table_bindings", []))
    catalog = {item["input_id"]: item for item in input_source_catalog(candidate)}
    current_revision = business.get("input_snapshot_revision", legacy_revision)
    inputs = {**deepcopy(business["inputs"]), **deepcopy({key: value for key, value in changes.items() if key != "source_changes"})}
    bindings = {item["input_id"]: item for item in candidate["table_bindings"]}
    affected, records, touched = set(), [], set()
    for change in source_changes:
        allowed = {"op", "input_id", "input_snapshot_revision", "old_ref", "new_ref", "binding", "reason"}
        if not isinstance(change, dict) or set(change) - allowed:
            raise ApiError("INVALID_SOURCE_CHANGES", "Use a source operation and its exact input snapshot", 400)
        op, identifier = _string(change.get("op"), "source_change.op", maximum=40), change.get("input_id")
        _string(identifier, "input_id", maximum=200)
        if op not in {"append", "replace", "exclude"} or identifier in touched:
            raise ApiError("INVALID_SOURCE_CHANGES", "Change each input once using append, replace or exclude", 400)
        if type(change.get("input_snapshot_revision")) is not int or change["input_snapshot_revision"] != current_revision:
            raise ApiError("INPUT_SNAPSHOT_CONFLICT", "Review the current source inputs before adopting this correction", 409,
                           {"input_snapshot_revision": current_revision, "inputs": list(catalog.values())})
        touched.add(identifier)
        old = catalog.get(identifier)
        if op == "append":
            if old is not None or "old_ref" in change or not isinstance(change.get("binding"), dict):
                raise ApiError("SOURCE_INPUT_CONFLICT", "An appended source needs a new input identity and its existing business mapping", 409)
        elif old is None or change.get("old_ref") != old["ref"]:
            raise ApiError("SOURCE_INPUT_CONFLICT", "The old source does not match this exact frozen input", 409)
        reason = _string(change.get("reason"), "source_change.reason", maximum=3000)
        if op == "exclude":
            if "new_ref" in change or "binding" in change:
                raise ApiError("INVALID_SOURCE_CHANGES", "Exclusion cannot silently adopt a replacement", 400)
            if old["kind"] != "table":
                raise ApiError("REPLACEMENT_SOURCE_REQUIRED", "This analysis needs an original; select its replacement to continue", 409)
            affected.add(old["target"])
            del bindings[identifier]
        else:
            material = _resolve_ref(connection, identity, work_id, change.get("new_ref"))
            if old and old["kind"] == "financial_original":
                if "binding" in change:
                    raise ApiError("INVALID_SOURCE_CHANGES", "The financial original does not use a table binding", 400)
                inputs["source"] = {"material_id": material["id"], "material_sha256": material["sha256"],
                                    "filename": material["filename"], "title": material["title"]}
                affected.add("source")
            else:
                binding = deepcopy(change.get("binding", bindings.get(identifier)))
                if not isinstance(binding, dict) or ("binding" in change and set(binding) & {"material_id", "material_sha256", "source_ref", "input_id"}):
                    raise ApiError("INVALID_SOURCE_CHANGES", "The mapping carries business columns; the source is selected by new_ref", 400)
                if old and binding.get("target") != old["target"]:
                    raise ApiError("SOURCE_TARGET_CHANGED", "Replace the source of the selected input without moving it to another business field", 409)
                binding.update(input_id=identifier, material_id=material["id"], material_sha256=material["sha256"], source_ref=deepcopy(change["new_ref"]))
                _string(binding.get("target"), "binding.target", maximum=300)
                affected.add(binding.get("target"))
                bindings[identifier] = binding
        records.append({**deepcopy(change), "reason": reason, "provenance": "user_adopted_source_change"})
    if any(not isinstance(target, str) for target in affected):
        raise ApiError("INVALID_SOURCE_CHANGES", "Each source input must have its business target", 400)
    if any(target.split(".")[0] in changes for target in affected):
        raise ApiError("SOURCE_DATA_CORRECTION_REQUIRED", "A source correction cannot also overwrite its derived business values", 409)
    source_by_id = {binding["input_id"]: source for binding, source in zip(candidate["table_bindings"], candidate.get("table_sources", []))}
    old_bindings = {binding["input_id"]: binding for binding in candidate["table_bindings"]}
    for change in records:
        identifier, op = change["input_id"], change["op"]
        if catalog.get(identifier, {}).get("kind") == "financial_original":
            continue
        binding = bindings.get(identifier, old_bindings.get(identifier))
        target = binding["target"]
        destination = inputs
        parts = target.split(".")
        for part in parts[:-1]:
            destination = destination.setdefault(part, {})
            if not isinstance(destination, dict):
                raise ApiError("SOURCE_TARGET_CHANGED", "The business input structure changed", 409)
        current_rows = destination.get(parts[-1], [])
        if not isinstance(current_rows, list):
            raise ApiError("SOURCE_TARGET_CHANGED", "A mapped source must retain its business row collection", 409)
        new_rows = []
        if op != "exclude":
            parsed, metadata = _resolve_tables(store, connection, identity, work_id, [binding], {})
            new_rows = parsed
            for part in parts:
                new_rows = new_rows[part]
        if op == "append":
            destination[parts[-1]] = current_rows + new_rows
        else:
            previous_source = source_by_id[identifier]
            fingerprints = previous_source.get("row_fingerprints")
            if fingerprints is None:
                # Legacy saved inputs already carried per-row Material identity.
                # Never guess ownership from equal numbers or row position.
                fingerprints = [_hash(row) for row in current_rows if isinstance(row, dict)
                                and isinstance(row.get("source"), dict)
                                and row["source"].get("material_id") == previous_source["material_id"]
                                and row["source"].get("material_sha256") == previous_source["sha256"]]
                if len(fingerprints) != previous_source["used_nonempty_rows"]:
                    raise ApiError("SOURCE_INPUT_LINEAGE_UNAVAILABLE", "The historical input does not identify its selected source rows; retain it and explicitly resolve the row mapping", 409,
                                   {"input_id": identifier})
            remaining = Counter(fingerprints)
            updated, inserted = [], False
            for row in current_rows:
                fingerprint = _hash(row)
                if remaining[fingerprint]:
                    remaining[fingerprint] -= 1
                    if op == "replace" and not inserted:
                        updated.extend(new_rows)
                        inserted = True
                else:
                    updated.append(row)
            if any(remaining.values()):
                raise ApiError("SOURCE_INPUT_MODIFIED", "Some mapped source rows were manually changed; resolve those exact input changes before substituting the source", 409,
                               {"input_id": identifier})
            if op == "replace" and not inserted:
                updated.extend(new_rows)
            destination[parts[-1]] = updated
        if op != "exclude":
            source_by_id[identifier] = metadata[0]
    result_bindings = list(bindings.values())
    return inputs, result_bindings, [source_by_id[item["input_id"]] for item in result_bindings], records
