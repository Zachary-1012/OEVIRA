"""PPTX object editing over the existing Material, Artifact and export owners.

This module adds no database or file-store authority. Original PPTX bytes are
read from Materials; each editable result is an immutable Artifact version whose
file reference points to the existing ``exports`` table and Artifact Store.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path
import tempfile
import uuid
from datetime import datetime, timezone

from flask import g, request

from .office import MAX_CHANGE_TEXT_CHARS, MAX_INPUT_BYTES, inspect_presentation, revise_presentation
from .office_spreadsheet import SpreadsheetError, inspect_spreadsheet, revise_spreadsheet
from .office_pdf import PdfRevisionError, inspect_pdf, revise_pdf
from .auth import utc_now


MAX_PRESENTATION_CHANGES = 500
MAX_TOTAL_CHANGE_TEXT_CHARS = 200_000
PRESENTATION_REVISION_OPERATION = "presentation_revision"
SPREADSHEET_REVISION_OPERATION = "spreadsheet_revision"
PDF_REVISION_OPERATION = "pdf_revision"
_TARGET_WORK_UNSET = object()


def _api():
    # Lazy import lets the existing API register this module without creating a
    # second validation, identity, or persistence layer.
    from . import api

    return api


def _api_error(code: str, message: str, status: int = 400, details: dict | None = None):
    return _api().ApiError(code, message, status, details)


def _workspace_id() -> str:
    return g.identity["workspace"]["id"]


def _access_identity(identity: dict | None, workspace_id: str) -> dict:
    if identity is not None:
        if (isinstance(identity, dict) and isinstance(identity.get("workspace"), dict)
                and identity["workspace"].get("id") == workspace_id
                and isinstance(identity.get("user"), dict)
                and isinstance(identity.get("user", {}).get("id"), str)):
            return identity
        raise _api_error("IDENTITY_REQUIRED", "The supplied file actor does not match this workspace", 401)
    try:
        from flask import has_request_context

        if has_request_context():
            current = getattr(g, "identity", None)
            if (isinstance(current, dict) and isinstance(current.get("workspace"), dict)
                    and current["workspace"].get("id") == workspace_id
                    and isinstance(current.get("user", {}).get("id"), str)):
                return current
    except RuntimeError:
        pass
    raise _api_error("IDENTITY_REQUIRED", "A current authorized file actor is required", 401)


def _authorize_material_scope(connection, identity: dict, material_row,
                              target_work_id: str | None, *, write_target: bool = False) -> None:
    from .collaboration import require_work_access, work_commission

    source_scope = work_commission(connection, material_row["work_id"])
    target_scope = None
    if target_work_id is not None:
        require_work_access(connection, identity, target_work_id, write=write_target)
        target_scope = work_commission(connection, target_work_id)
    if source_scope is not None and source_scope != target_scope:
        raise _api_error("CLIENT_SCOPE_MISMATCH",
                         "Private client material requires a target Work in the same client scope", 403)


def _authorized_material_row(store, identity: dict, workspace_id: str, material_id: str,
                             target_work_id=_TARGET_WORK_UNSET, *, write_target: bool = False):
    from .collaboration import require_material_access

    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or identity["workspace"].get("id") != workspace_id):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    metadata = store.row("SELECT id, work_id FROM materials WHERE id = ? AND workspace_id = ?",
                         (material_id, workspace_id))
    if metadata is None:
        raise _api()._not_found()
    effective_target = metadata["work_id"] if target_work_id is _TARGET_WORK_UNSET else target_work_id
    with store.transaction(immediate=False) as connection:
        row = require_material_access(connection, identity, material_id)
        _authorize_material_scope(connection, identity, row, effective_target,
                                  write_target=write_target)
        return dict(row)


def _ensure_native_artifact_dependencies(store, identity: dict, artifact_id: str,
                                         snapshot: dict, *, write: bool = False):
    from .collaboration import require_artifact_access, require_material_access

    references: dict[str, dict] = {}
    for value in snapshot.get("materials", []):
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            references[value["id"]] = {
                "material_id": value["id"],
                "sha256": value.get("sha256"),
                "size_bytes": value.get("size_bytes"),
            }
    for owner_key in ("presentation", "spreadsheet", "pdf_document"):
        owner = snapshot.get(owner_key)
        source = owner.get("source") if isinstance(owner, dict) else None
        if isinstance(source, dict) and isinstance(source.get("material_id"), str):
            references.setdefault(source["material_id"], source)
        if owner_key == "presentation" and isinstance(owner, dict):
            for change in owner.get("changes", []):
                after = change.get("after") if isinstance(change, dict) else None
                image_source = after.get("source") if isinstance(after, dict) else None
                if isinstance(image_source, dict) and isinstance(image_source.get("material_id"), str):
                    references.setdefault(image_source["material_id"], image_source)

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id, write=write)
        target_work_id = artifact["work_id"]
        for material_id, reference in references.items():
            row = require_material_access(connection, identity, material_id)
            expected_hash = reference.get("sha256")
            expected_size = reference.get("size_bytes")
            if (expected_hash is not None and row["sha256"] != expected_hash
                    or expected_size is not None and row["size_bytes"] != expected_size):
                raise _api_error("FILE_INTEGRITY_ERROR", "A file source Material differs from its saved reference", 409)
            _authorize_material_scope(connection, identity, row, target_work_id, write_target=write)
        return dict(artifact)


def authorize_office_artifact(store, identity: dict, version_row, *,
                             write: bool = False) -> dict:
    """Recheck a native Office Artifact and its Material scope without reading export bytes."""
    workspace_id = version_row["workspace_id"]
    artifact_id = version_row["artifact_id"]
    identity = _access_identity(identity, workspace_id)
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("FILE_RECOVERY_REQUIRED", "The saved Office version record is unreadable", 409) from exc
    if not isinstance(snapshot, dict) or snapshot.get("kind") not in {
            "presentation", "spreadsheet", "pdf_document"}:
        raise _api_error("INVALID_ARTIFACT_KIND", "This version is not a native Office file Artifact", 409)
    return _ensure_native_artifact_dependencies(store, identity, artifact_id,
                                                snapshot, write=write)


def _as_presentation_error(exc: ValueError):
    return _api_error(
        "PRESENTATION_NOT_EDITABLE",
        "The presentation could not be read or safely revised.",
        422,
        {"reason": str(exc)},
    )


def _normalize_allowed_slides(value) -> list[int]:
    if not isinstance(value, list) or not value or any(type(number) is not int for number in value):
        raise _api_error("INVALID_INPUT", "allowed_slides must be a non-empty list of page numbers", 400)
    if any(number <= 0 for number in value):
        raise _api_error("INVALID_INPUT", "allowed_slides must contain positive page numbers", 400)
    if len(set(value)) != len(value):
        raise _api_error("INVALID_INPUT", "allowed_slides cannot contain duplicates", 400)
    return sorted(value)


def _normalize_changes(value) -> list[dict]:
    if not isinstance(value, list) or not value or len(value) > MAX_PRESENTATION_CHANGES:
        raise _api_error(
            "INVALID_INPUT",
            f"changes must contain 1 to {MAX_PRESENTATION_CHANGES} selected object revisions",
            400,
        )
    normalized = []
    seen: set[tuple[int, str]] = set()
    total_text_chars = 0
    for index, item in enumerate(value):
        whole_text_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "text"}
        exact_span_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "before", "after"}
        chart_data_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "chart_data"}
        picture_replace_change = (isinstance(item, dict)
                                  and set(item) in ({"slide", "object_id", "image_material_id"},
                                                    {"slide", "object_id", "image_material_id", "alt_text"}))
        picture_valid = (not picture_replace_change
                         or (isinstance(item["image_material_id"], str)
                             and bool(item["image_material_id"].strip())
                             and ("alt_text" not in item or isinstance(item["alt_text"], str))))
        chart_valid = (not chart_data_change or _valid_chart_data_request(item["chart_data"]))
        if (not (whole_text_change or exact_span_change or picture_replace_change or chart_data_change)
                or type(item["slide"]) is not int or item["slide"] < 1
                or not isinstance(item["object_id"], str) or not item["object_id"].strip()
                or len(item["object_id"]) > 200
                or (whole_text_change and not isinstance(item["text"], str))
                or (exact_span_change and (not isinstance(item["before"], str) or not item["before"]
                                           or not isinstance(item["after"], str)))
                or not picture_valid or not chart_valid):
            raise _api_error(
                "INVALID_INPUT",
                "Each change needs a slide/object anchor and a supported text, image, or chart-data revision",
                400, {"index": index},
            )
        identity = (item["slide"], item["object_id"])
        if identity in seen:
            raise _api_error("INVALID_INPUT", "An object can only be revised once per request", 400, {"index": index})
        seen.add(identity)
        if picture_replace_change:
            supplied = {"image_material_id": item["image_material_id"]}
            if "alt_text" in item:
                supplied["alt_text"] = item["alt_text"]
                if (len(item["alt_text"]) > 2_000
                        or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", item["alt_text"])):
                    raise _api_error("INVALID_INPUT", "alt_text is invalid or exceeds the text limit", 400,
                                     {"index": index})
                total_text_chars += len(item["alt_text"])
        elif chart_data_change:
            supplied = {"chart_data": item["chart_data"]}
            total_text_chars += _chart_data_text_size(item["chart_data"])
            if total_text_chars > MAX_TOTAL_CHANGE_TEXT_CHARS:
                raise _api_error("INVALID_INPUT", "Combined replacement text exceeds the request limit", 413)
        else:
            supplied = ({"text": item["text"]} if whole_text_change else
                        {"before": item["before"], "after": item["after"]})
        if any(len(text) > MAX_CHANGE_TEXT_CHARS for text in supplied.values()):
            raise _api_error("INVALID_INPUT", "A text replacement or anchor exceeds the text limit", 400, {"index": index})
        total_text_chars += sum(len(text) for text in supplied.values())
        if total_text_chars > MAX_TOTAL_CHANGE_TEXT_CHARS:
            raise _api_error("INVALID_INPUT", "Combined replacement text exceeds the request limit", 413)
        normalized.append({"slide": item["slide"], "object_id": item["object_id"], **supplied})
    return normalized


def _valid_chart_data_request(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"categories", "series"}:
        return False
    categories, series = value["categories"], value["series"]
    if not isinstance(categories, list) or not 1 <= len(categories) <= 5_000:
        return False
    if not isinstance(series, list) or not 1 <= len(series) <= 100:
        return False

    def valid_typed(item, allowed):
        if (not isinstance(item, dict) or set(item) != {"kind", "value"}
                or not isinstance(item["kind"], str) or item["kind"] not in allowed):
            return False
        kind, raw = item["kind"], item["value"]
        if kind == "text":
            return (isinstance(raw, str) and len(raw) <= MAX_CHANGE_TEXT_CHARS
                    and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", raw))
        if kind == "number":
            return not isinstance(raw, bool) and isinstance(raw, (str, int, float))
        return kind == "blank" and raw is None

    if not all(valid_typed(item, {"text", "number", "blank"}) for item in categories):
        return False
    for item in series:
        if (not isinstance(item, dict) or set(item) != {"name", "values"}
                or not isinstance(item["name"], str) or not item["name"].strip()
                or len(item["name"]) > 255
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", item["name"])
                or not isinstance(item["values"], list) or len(item["values"]) != len(categories)
                or not all(valid_typed(value, {"number", "blank"}) for value in item["values"])):
            return False
    return True


def _chart_data_text_size(value: dict) -> int:
    total = 0
    for item in value["categories"]:
        if item["kind"] == "text":
            total += len(item["value"])
    for item in value["series"]:
        total += len(item["name"])
        total += sum(len(value["value"]) for value in item["values"]
                     if value["kind"] == "text")
    return total


def _safe_read(store, relative_path: str, expected_size: int, expected_sha256: str) -> bytes:
    api = _api()
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved file has an invalid size record", 409)
    if expected_size > MAX_INPUT_BYTES:
        raise _api_error("PRESENTATION_TOO_LARGE", "The saved presentation exceeds the supported size", 413)
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved file has an invalid hash record", 409)
    path = api._resource_path(store, relative_path)
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved presentation is missing or has changed size", 409)
        with path.open("rb") as stream:
            content = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise _api_error("FILE_UNAVAILABLE", "The saved presentation is unavailable", 409) from exc
    if len(content) > MAX_INPUT_BYTES or len(content) != expected_size:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved presentation does not match its recorded size", 409)
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved presentation failed integrity verification", 409)
    return content


def _read_material(store, workspace_id: str, material_id: str, *,
                   identity: dict | None = None, target_work_id=_TARGET_WORK_UNSET,
                   write_target: bool = False):
    identity = _access_identity(identity, workspace_id)
    row = _authorized_material_row(store, identity, workspace_id, material_id,
                                   target_work_id, write_target=write_target)
    if Path(row["filename"]).suffix.lower() != ".pptx" or row["format"] not in {"pptx", None}:
        raise _api_error("UNSUPPORTED_PRESENTATION", "This material is not a supported PPTX file", 415)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    return row, content


def _read_image_material(store, workspace_id: str, material_id: str, *,
                         identity: dict, target_work_id: str | None,
                         write_target: bool = False):
    row = _authorized_material_row(store, identity, workspace_id, material_id,
                                   target_work_id, write_target=write_target)
    extension = Path(row["filename"]).suffix.lower().lstrip(".")
    if extension not in {"png", "jpg", "jpeg"} or row["format"] not in {extension, None}:
        raise _api_error("UNSUPPORTED_IMAGE_MATERIAL", "Select a PNG or JPEG image material", 415)
    if row["size_bytes"] > 32 * 1024 * 1024:
        raise _api_error("PPTX_IMAGE_SIZE_UNSUPPORTED", "The selected image exceeds the replacement size limit", 413)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            if image.width * image.height > 100_000_000:
                raise _api_error("PPTX_IMAGE_DIMENSIONS_UNSUPPORTED", "The selected image dimensions exceed the replacement limit", 413)
            image.verify()
    except _api().ApiError:
        raise
    except Exception as exc:
        raise _api_error("PPTX_IMAGE_INVALID", "The selected material is not a valid image", 415) from exc
    if (extension in {"jpg", "jpeg"} and image_format != "JPEG"
            or extension == "png" and image_format != "PNG"):
        raise _api_error("PPTX_IMAGE_FORMAT_MISMATCH", "The image filename does not match its encoded format", 415)
    source = {"kind": "material", "material_id": row["id"], "filename": row["filename"],
              "sha256": row["sha256"], "size_bytes": row["size_bytes"], "format": extension}
    return row, content, source


def _material_file_ref(row) -> dict:
    return {
        "kind": "material",
        "material_id": row["id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "format": "pptx",
    }


def _inspect(content: bytes) -> dict:
    try:
        return inspect_presentation(content)
    except ValueError as exc:
        raise _as_presentation_error(exc) from exc


def _revise_with_effective_changes(content: bytes, changes: list[dict], allowed_slides: list[int],
                                   *, store=None, workspace_id: str | None = None,
                                   identity: dict | None = None,
                                   target_work_id: str | None = None) -> dict:
    try:
        model_changes = []
        for item in changes:
            if isinstance(item, dict) and "image_material_id" in item:
                if (store is None or not isinstance(workspace_id, str)
                        or not isinstance(identity, dict)):
                    raise _api_error("PPTX_IMAGE_SOURCE_UNAVAILABLE", "Image replacement needs a workspace Material", 409)
                _image_row, image_bytes, source_reference = _read_image_material(
                    store, workspace_id, item["image_material_id"],
                    identity=identity, target_work_id=target_work_id,
                )
                if (item.get("expected_image_sha256") is not None
                        and source_reference["sha256"] != item["expected_image_sha256"]):
                    raise _api_error("PPTX_IMAGE_SOURCE_CHANGED", "The saved replacement image source changed", 409)
                replacement = {"content": image_bytes, "filename": source_reference["filename"],
                               "source": source_reference}
                if "alt_text" in item:
                    replacement["alt_text"] = item["alt_text"]
                model_changes.append({"slide": item["slide"], "object_id": item["object_id"],
                                      "replace_image": replacement})
            else:
                model_changes.append(item)
        result = revise_presentation(content, model_changes, allowed_slides)
        if not result["changes"]:
            raise _api_error("NO_PRESENTATION_CHANGES", "At least one selected slide object must change", 400)
        return result
    except ValueError as exc:
        raise _as_presentation_error(exc) from exc


def _snapshot_presentation(snapshot) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "presentation":
        raise _api_error("ARTIFACT_NOT_PRESENTATION", "This artifact version is not a PPTX presentation", 409)
    presentation = snapshot.get("presentation")
    if not isinstance(presentation, dict) or not isinstance(presentation.get("file"), dict):
        raise _api_error("PRESENTATION_FILE_UNAVAILABLE", "This artifact version has no PPTX file reference", 409)
    return presentation


def _output_file_ref(export_id: str, sha256: str, size_bytes: int) -> dict:
    return {"kind": "export", "export_id": export_id, "format": "pptx",
            "sha256": sha256, "size_bytes": size_bytes}


def _revision_body(changes: list[dict], allowed_slides: list[int]) -> str:
    return (
        f"PowerPoint 对象局部修订：{len(changes)} 个对象；"
        f"选定页：{', '.join(str(number) for number in allowed_slides)}。"
    )


def _ensure_editor(connection, workspace_id: str, user_id: str) -> None:
    member = connection.execute(
        "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if member is None or not member["active"] or member["role"] not in {"owner", "editor"}:
        raise _api_error("FORBIDDEN", "Workspace write access is no longer available", 403)


def _presentation_response(store, artifact_id: str, version: int, *, workspace_id: str | None = None) -> dict:
    workspace_id = workspace_id or _workspace_id()
    api = _api()
    identity = _access_identity(None, workspace_id)
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id)
    if artifact is None:
        raise api._not_found()
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, version),
    )
    if version_row is None:
        raise api._not_found()
    export, content = _ensure_export_ready(store, workspace_id, artifact_id, version_row,
                                           identity=identity)
    inspected = _inspect(content)
    return {
        "artifact_id": artifact_id,
        "version": version,
        "sha256": export["sha256"],
        "size_bytes": export["size_bytes"],
        "export_id": export["id"],
        "download_url": f"/api/exports/{export['id']}/file",
        "slides": inspected["slides"],
        "warnings": inspected["warnings"],
    }


def _version_from_request(value, field: str = "base_version") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _api_error("INVALID_INPUT", f"{field} must be a positive integer", 400)
    return value


def _normalize_initial_request(data: dict, material_id: str) -> dict:
    allowed = {"allowed_slides", "changes", "title", "work_id", "revision_note"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "Presentation revision contains an unsupported field", 400)
    return {
        "material_id": material_id,
        "allowed_slides": _normalize_allowed_slides(data.get("allowed_slides")),
        "changes": _normalize_changes(data.get("changes")),
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
        "work_id": _api()._optional_string(data.get("work_id"), "work_id", maximum=80),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
    }


def _normalize_followup_request(data: dict, artifact_id: str) -> dict:
    allowed = {"base_version", "allowed_slides", "changes", "revision_note", "title"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "Presentation revision contains an unsupported field", 400)
    return {
        "artifact_id": artifact_id,
        "base_version": _version_from_request(data.get("base_version")),
        "allowed_slides": _normalize_allowed_slides(data.get("allowed_slides")),
        "changes": _normalize_changes(data.get("changes")),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
    }


def _revision_key_fingerprint(operation: dict) -> tuple[str, str]:
    api = _api()
    key = api._idempotency_key()
    if not key:
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for presentation revisions", 400)
    return key, api._request_fingerprint(operation)


def _find_revision_replay(store, workspace_id: str, key: str, fingerprint: str):
    api = _api()
    with store.transaction(immediate=False) as connection:
        record = api._idempotent_resource(connection, workspace_id, PRESENTATION_REVISION_OPERATION, key, fingerprint)
        if record is None:
            return None
        if record["resource_type"] != "export":
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved revision record is invalid", 409)
        export = connection.execute(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'pptx'",
            (record["resource_id"], workspace_id),
        ).fetchone()
        if export is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved presentation revision is unavailable", 409)
        artifact = api._artifact_row(connection, workspace_id, export["artifact_id"])
        version = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (export["artifact_id"], workspace_id, export["version"]),
        ).fetchone()
        if artifact is None or version is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved presentation version is unavailable", 409)
        return {"export_id": export["id"], "artifact_id": artifact["id"], "version": export["version"]}


def _presentation_revision_data(store, workspace_id: str, artifact_id: str,
                                version: int, export_id: str) -> dict:
    api = _api()
    with store.transaction(immediate=False) as connection:
        artifact = api._artifact_row(connection, workspace_id, artifact_id)
        version_row = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, version),
        ).fetchone()
        export = connection.execute(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pptx'",
            (export_id, workspace_id, artifact_id),
        ).fetchone()
        if artifact is None or version_row is None or export is None:
            raise api._not_found()
        snapshot = json.loads(version_row["input_snapshot_json"])
        presentation = _snapshot_presentation(snapshot)
        result = {
            "artifact": api._artifact_data(connection, artifact, version),
            "file": api._export_data(export),
            "changes": presentation["changes"],
            "diff": presentation["changes"],
            "unchanged_verified": bool(presentation.get("unchanged_verified", False)),
            "sha256": export["sha256"],
            "size_bytes": export["size_bytes"],
        }
    return result


def _revision_response(store, artifact_id: str, version: int, export_id: str, *, status: int,
                       workspace_id: str | None = None):
    return _api()._success(_presentation_revision_data(
        store, workspace_id or _workspace_id(), artifact_id, version, export_id), status)


def _mark_export_interrupted(store, export_id: str, workspace_id: str, code: str) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'interrupted', error_code = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (code, utc_now(), export_id, workspace_id),
        )


def _complete_export(store, export_id: str, workspace_id: str, content: bytes) -> object:
    digest = hashlib.sha256(content).hexdigest()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'completed', sha256 = ?, size_bytes = ?, error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('writing', 'interrupted', 'completed')",
            (digest, len(content), utc_now(), export_id, workspace_id),
        )
        row = connection.execute(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ?",
            (export_id, workspace_id),
        ).fetchone()
        if row is None or row["status"] != "completed" or row["sha256"] != digest or row["size_bytes"] != len(content):
            raise _api_error("EXPORT_STATE_CONFLICT", "The saved PPTX version could not be confirmed", 409, {"export_id": export_id})
        return row


def _write_export_file(store, export_row, content: bytes) -> bytes:
    api = _api()
    target = api._resource_path(store, export_row["relative_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_hash = hashlib.sha256(content).hexdigest()
    if target.exists():
        existing = _safe_read(store, export_row["relative_path"], len(content), expected_hash)
        return existing
    fd, temporary_name = tempfile.mkstemp(prefix=f".{export_row['id']}-", suffix=".partial", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            pass
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    readback = _safe_read(store, export_row["relative_path"], len(content), expected_hash)
    return readback


def _load_base_reference(store, workspace_id: str, artifact_id: str | None, reference: dict,
                         *, before_version: int | None = None, identity: dict | None = None,
                         target_work_id: str | None = None) -> bytes:
    if not isinstance(reference, dict):
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The base PPTX reference is unavailable", 409)
    if reference.get("kind") == "material":
        material, content = _read_material(
            store, workspace_id, reference.get("material_id"), identity=identity,
            target_work_id=target_work_id,
        )
        if material["sha256"] != reference.get("sha256") or material["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The original material changed since the version was saved", 409)
        return content
    if reference.get("kind") == "export" and artifact_id:
        row = store.row(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pptx'",
            (reference.get("export_id"), workspace_id, artifact_id),
        )
        if row is None or row["id"] != reference.get("export_id"):
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The base PPTX export reference changed", 409)
        if row["sha256"] != reference.get("sha256") or row["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The base PPTX reference does not match its export record", 409)
        if before_version is not None and row["version"] >= before_version:
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The base PPTX reference is not an earlier version", 409)
        version_row = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, row["version"]),
        )
        if version_row is None:
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The base PPTX version is unavailable", 409)
        _base_export, content = _ensure_export_ready(
            store, workspace_id, artifact_id, version_row, identity=identity,
        )
        return content
    raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The base PPTX reference is unsupported", 409)


def _recover_export(store, workspace_id: str, artifact_id: str, version_row, export_row,
                    *, identity: dict, target_work_id: str | None):
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    presentation = _snapshot_presentation(snapshot)
    file_reference = presentation["file"]
    if file_reference.get("export_id") != export_row["id"]:
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved PPTX reference does not match its version", 409)
    target = _api()._resource_path(store, export_row["relative_path"])
    if target.exists():
        try:
            existing = _safe_read(store, export_row["relative_path"], file_reference["size_bytes"], file_reference["sha256"])
        except Exception as exc:
            _mark_export_interrupted(store, export_row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            if hasattr(exc, "code"):
                raise
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved PPTX file cannot be verified", 409) from exc
        completed = _complete_export(store, export_row["id"], workspace_id, existing)
        return completed, existing

    base = _load_base_reference(
        store, workspace_id, artifact_id, presentation.get("base_file"),
        before_version=version_row["version"], identity=identity,
        target_work_id=target_work_id,
    )
    stored_changes = presentation.get("changes")
    allowed_slides = presentation.get("allowed_slides")
    if not isinstance(stored_changes, list) or not isinstance(allowed_slides, list):
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The revision recovery record is incomplete", 409)
    requested_changes = []
    for item in stored_changes:
        if not isinstance(item, dict) or not {"slide", "object_id", "kind"} <= item.keys():
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The revision change record is invalid", 409)
        if item["kind"] == "picture_replacement":
            after = item.get("after")
            source = after.get("source") if isinstance(after, dict) else None
            if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
                raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The image replacement source is unavailable", 409)
            try:
                _image_row, _image_content, current_source = _read_image_material(
                    store, workspace_id, source["material_id"],
                    identity=identity, target_work_id=target_work_id,
                )
            except Exception as exc:
                if hasattr(exc, "code"):
                    raise
                raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The image replacement source is unavailable", 409) from exc
            if (current_source["sha256"] != source.get("sha256")
                    or current_source["size_bytes"] != source.get("size_bytes")):
                raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The image material changed since the revision was saved", 409)
            request = {"slide": item["slide"], "object_id": item["object_id"],
                       "image_material_id": source["material_id"],
                       "expected_image_sha256": source["sha256"]}
            if isinstance(after.get("alt_text"), str):
                request["alt_text"] = after["alt_text"]
            requested_changes.append(request)
        elif item.get("kind") in {"shape_text", "table_cell"} and isinstance(item.get("after"), str):
            requested_changes.append({"slide": item["slide"], "object_id": item["object_id"],
                                      "text": item["after"]})
        elif item.get("kind") == "chart_data_replacement" and isinstance(item.get("after"), dict):
            requested_changes.append({"slide": item["slide"], "object_id": item["object_id"],
                                      "chart_data": item["after"]})
        else:
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The revision change kind is unsupported", 409)
    try:
        revised = _revise_with_effective_changes(
            base, requested_changes, allowed_slides, store=store,
            workspace_id=workspace_id, identity=identity, target_work_id=target_work_id,
        )
    except ValueError as exc:
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved PPTX version could not be regenerated", 409, {"reason": str(exc)}) from exc
    if revised["sha256"] != file_reference.get("sha256") or len(revised["content"]) != file_reference.get("size_bytes"):
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "Regenerated PPTX bytes do not match the saved version hash", 409)
    readback = _write_export_file(store, export_row, revised["content"])
    completed = _complete_export(store, export_row["id"], workspace_id, readback)
    return completed, readback


def _ensure_export_ready(store, workspace_id: str, artifact_id: str, version_row, *,
                         identity: dict | None = None):
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    identity = _access_identity(identity, workspace_id)
    artifact = _ensure_native_artifact_dependencies(store, identity, artifact_id, snapshot)
    presentation = _snapshot_presentation(snapshot)
    row = store.row(
        "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pptx'",
        (presentation["file"].get("export_id"), workspace_id, artifact_id),
    )
    if row is None:
        raise _api_error("PRESENTATION_FILE_UNAVAILABLE", "The saved PPTX file reference is unavailable", 409)
    if row["status"] == "completed":
        target = _api()._resource_path(store, row["relative_path"])
        if not target.exists():
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_UNAVAILABLE")
            interrupted = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id))
            return _recover_export(store, workspace_id, artifact_id, version_row, interrupted,
                                   identity=identity, target_work_id=artifact["work_id"])
        try:
            content = _safe_read(store, row["relative_path"], presentation["file"]["size_bytes"], presentation["file"]["sha256"])
        except Exception:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise
        if row["sha256"] != presentation["file"]["sha256"] or row["size_bytes"] != presentation["file"]["size_bytes"]:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            row = _complete_export(store, row["id"], workspace_id, content)
        return row, content
    return _recover_export(store, workspace_id, artifact_id, version_row, row,
                           identity=identity, target_work_id=artifact["work_id"])


def _as_spreadsheet_error(exc: ValueError):
    code = getattr(exc, "code", "SPREADSHEET_NOT_EDITABLE")
    details = getattr(exc, "details", {})
    return _api_error("SPREADSHEET_NOT_EDITABLE", "The workbook cannot be safely revised with this request", 422,
                      {"reason": code, **details})


def _read_spreadsheet_material(store, workspace_id: str, material_id: str, *,
                               identity: dict | None = None, target_work_id=_TARGET_WORK_UNSET,
                               write_target: bool = False):
    identity = _access_identity(identity, workspace_id)
    row = _authorized_material_row(store, identity, workspace_id, material_id,
                                   target_work_id, write_target=write_target)
    if Path(row["filename"]).suffix.lower() != ".xlsx" or row["format"] not in {"xlsx", None}:
        raise _api_error("UNSUPPORTED_SPREADSHEET", "This material is not a supported XLSX workbook", 415)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    try:
        inspect_spreadsheet(content)
    except ValueError as exc:
        raise _as_spreadsheet_error(exc) from exc
    return row, content


def _spreadsheet_material_ref(row) -> dict:
    return {"kind": "material", "material_id": row["id"], "filename": row["filename"],
            "sha256": row["sha256"], "size_bytes": row["size_bytes"], "format": "xlsx"}


def _snapshot_spreadsheet(snapshot) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "spreadsheet":
        raise _api_error("ARTIFACT_NOT_SPREADSHEET", "This artifact version is not an original-backed XLSX workbook", 409)
    spreadsheet = snapshot.get("spreadsheet")
    if (not isinstance(spreadsheet, dict) or not isinstance(spreadsheet.get("file"), dict)
            or spreadsheet["file"].get("format") != "xlsx"):
        raise _api_error("SPREADSHEET_FILE_UNAVAILABLE", "This artifact version has no verified XLSX file reference", 409)
    return spreadsheet


def _spreadsheet_export_ref(export_id: str, content: bytes) -> dict:
    return {"kind": "export", "export_id": export_id, "format": "xlsx",
            "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def _normalize_spreadsheet_changes(value) -> list[dict]:
    maximum = 500
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise _api_error("INVALID_INPUT", f"changes must contain 1 to {maximum} typed cell changes", 400)
    normalized = []
    seen = set()
    total_chars = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"object_id", "before", "after"}:
            raise _api_error("INVALID_INPUT", "Each change needs object_id and typed before/after values", 400,
                             {"index": index})
        object_id = item["object_id"]
        if not isinstance(object_id, str) or not object_id.strip() or len(object_id) > 200:
            raise _api_error("INVALID_INPUT", "object_id must identify a cell in the exact workbook version", 400,
                             {"index": index})
        if object_id in seen:
            raise _api_error("INVALID_INPUT", "A workbook cell can only be revised once per request", 400,
                             {"index": index})
        seen.add(object_id)
        change = {"object_id": object_id}
        for field in ("before", "after"):
            typed = item[field]
            if not isinstance(typed, dict) or set(typed) != {"kind", "value"}:
                raise _api_error("INVALID_INPUT", f"{field} must be a typed cell value", 400, {"index": index})
            if typed["kind"] not in {"text", "number", "boolean", "blank", "formula", "date"}:
                raise _api_error("INVALID_INPUT", f"{field}.kind is unsupported", 400, {"index": index})
            if typed["kind"] in {"text", "formula", "date"}:
                if not isinstance(typed["value"], str):
                    raise _api_error("INVALID_INPUT", f"{field}.value must be text", 400, {"index": index})
                total_chars += len(typed["value"])
            elif typed["kind"] == "blank":
                if typed["value"] is not None:
                    raise _api_error("INVALID_INPUT", f"{field}.value must be null for a blank cell", 400,
                                     {"index": index})
            elif typed["kind"] == "boolean":
                if not isinstance(typed["value"], bool):
                    raise _api_error("INVALID_INPUT", f"{field}.value must be boolean", 400, {"index": index})
            elif typed["kind"] == "number":
                if isinstance(typed["value"], bool) or not isinstance(typed["value"], (str, int, float)):
                    raise _api_error("INVALID_INPUT", f"{field}.value must be a finite number", 400,
                                     {"index": index})
                total_chars += len(str(typed["value"]))
            change[field] = typed
        normalized.append(change)
    if total_chars > 200_000:
        raise _api_error("INVALID_INPUT", "Combined workbook change text exceeds the request limit", 413)
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > 1_000_000:
        raise _api_error("INVALID_INPUT", "The workbook change request exceeds the size limit", 413)
    return normalized


def _normalize_initial_spreadsheet_request(data: dict, material_id: str) -> dict:
    allowed = {"title", "work_id", "revision_note", "changes"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "Spreadsheet revision contains an unsupported field", 400)
    return {
        "material_id": material_id,
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
        "work_id": _api()._optional_string(data.get("work_id"), "work_id", maximum=80),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
        "changes": _normalize_spreadsheet_changes(data.get("changes")),
    }


def _normalize_followup_spreadsheet_request(data: dict, artifact_id: str) -> dict:
    allowed = {"base_version", "title", "revision_note", "changes"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "Spreadsheet revision contains an unsupported field", 400)
    return {
        "artifact_id": artifact_id,
        "base_version": _version_from_request(data.get("base_version")),
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
        "changes": _normalize_spreadsheet_changes(data.get("changes")),
    }


def _spreadsheet_fingerprint(operation: dict, key: str) -> tuple[str, str]:
    if not key:
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for workbook revisions", 400)
    return key, _api()._request_fingerprint(operation)


def _find_spreadsheet_replay(store, workspace_id: str, key: str, fingerprint: str):
    api = _api()
    with store.transaction(immediate=False) as connection:
        record = api._idempotent_resource(connection, workspace_id, SPREADSHEET_REVISION_OPERATION, key, fingerprint)
        if record is None:
            return None
        if record["resource_type"] != "export":
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved workbook revision record is invalid", 409)
        export = connection.execute(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'xlsx'",
            (record["resource_id"], workspace_id),
        ).fetchone()
        if export is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved workbook revision is unavailable", 409)
        artifact = api._artifact_row(connection, workspace_id, export["artifact_id"])
        version = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (export["artifact_id"], workspace_id, export["version"]),
        ).fetchone()
        if artifact is None or version is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved workbook version is unavailable", 409)
        return {"export_id": export["id"], "artifact_id": artifact["id"], "version": export["version"]}


def _snapshot_file_reference(spreadsheet: dict, key: str) -> dict:
    reference = spreadsheet.get(key)
    if (not isinstance(reference, dict) or reference.get("format") != "xlsx"
            or not isinstance(reference.get("sha256"), str)
            or type(reference.get("size_bytes")) is not int):
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved XLSX file reference is incomplete", 409)
    return reference


def _load_spreadsheet_base(store, workspace_id: str, artifact_id: str,
                           reference: dict, *, before_version: int | None = None,
                           identity: dict | None = None,
                           target_work_id: str | None = None) -> bytes:
    if not isinstance(reference, dict) or reference.get("format") != "xlsx":
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The base XLSX reference is unavailable", 409)
    if reference.get("kind") == "material":
        _material, content = _read_spreadsheet_material(
            store, workspace_id, reference.get("material_id"), identity=identity,
            target_work_id=target_work_id,
        )
        if hashlib.sha256(content).hexdigest() != reference.get("sha256") or len(content) != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The original workbook changed since this version was saved", 409)
        return content
    if reference.get("kind") == "export":
        row = store.row(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'xlsx'",
            (reference.get("export_id"), workspace_id, artifact_id),
        )
        if row is None or row["id"] != reference.get("export_id"):
            raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The base XLSX export reference changed", 409)
        if row["sha256"] != reference.get("sha256") or row["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The base workbook does not match its export record", 409)
        if before_version is not None and row["version"] >= before_version:
            raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The base workbook is not an earlier version", 409)
        version_row = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, row["version"]),
        )
        if version_row is None:
            raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The base workbook version is unavailable", 409)
        _base_export, content = _ensure_spreadsheet_export_ready(
            store, workspace_id, artifact_id, version_row, identity=identity,
        )
        return content
    raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The base XLSX reference is unsupported", 409)


def _recover_spreadsheet_export(store, workspace_id: str, artifact_id: str, version_row, export_row,
                                *, identity: dict, target_work_id: str | None):
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    spreadsheet = _snapshot_spreadsheet(snapshot)
    file_reference = _snapshot_file_reference(spreadsheet, "file")
    if file_reference.get("export_id") != export_row["id"]:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook file does not match its version", 409)
    target = _api()._resource_path(store, export_row["relative_path"])
    if target.exists():
        try:
            content = _safe_read(store, export_row["relative_path"], file_reference["size_bytes"],
                                 file_reference["sha256"])
        except Exception as exc:
            _mark_export_interrupted(store, export_row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            if hasattr(exc, "code"):
                raise
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved workbook cannot be verified", 409) from exc
    else:
        _mark_export_interrupted(store, export_row["id"], workspace_id, "FILE_UNAVAILABLE")
        base = _load_spreadsheet_base(store, workspace_id, artifact_id,
                                      _snapshot_file_reference(spreadsheet, "base_file"),
                                      before_version=version_row["version"], identity=identity,
                                      target_work_id=target_work_id)
        changes = spreadsheet.get("changes")
        if not isinstance(changes, list):
            raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook change record is incomplete", 409)
        if changes:
            requested = []
            for item in changes:
                if not isinstance(item, dict) or not {"object_id", "before", "after"} <= item.keys():
                    raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook change record is invalid", 409)
                after = item["after"]
                if not isinstance(after, dict) or not {"kind", "value"} <= after.keys():
                    raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook output value is invalid", 409)
                requested.append({"object_id": item["object_id"], "before": item["before"],
                                  "after": {"kind": after["kind"], "value": after["value"]}})
            try:
                content = revise_spreadsheet(base, requested)["content"]
            except ValueError as exc:
                raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook could not be regenerated", 409,
                                 {"reason": getattr(exc, "code", str(exc))}) from exc
        else:
            content = base
    try:
        inspected = inspect_spreadsheet(content)
    except ValueError as exc:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The recovered workbook is not a readable XLSX file", 409,
                         {"reason": getattr(exc, "code", str(exc))}) from exc
    if inspected["sha256"] != file_reference.get("sha256") or inspected["size_bytes"] != file_reference.get("size_bytes"):
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "Recovered XLSX bytes do not match the saved version hash", 409)
    readback = _write_export_file(store, export_row, content)
    completed = _complete_export(store, export_row["id"], workspace_id, readback)
    return completed, readback


def _ensure_spreadsheet_export_ready(store, workspace_id: str, artifact_id: str, version_row, *,
                                     identity: dict | None = None):
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook version record is unreadable", 409) from exc
    spreadsheet = _snapshot_spreadsheet(snapshot)
    identity = _access_identity(identity, workspace_id)
    artifact = _ensure_native_artifact_dependencies(store, identity, artifact_id, snapshot)
    file_reference = _snapshot_file_reference(spreadsheet, "file")
    row = store.row(
        "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'xlsx'",
        (file_reference.get("export_id"), workspace_id, artifact_id),
    )
    if row is None:
        raise _api_error("SPREADSHEET_FILE_UNAVAILABLE", "The saved XLSX export record is unavailable", 409)
    if row["sha256"] not in {None, file_reference["sha256"]} or row["size_bytes"] not in {None, file_reference["size_bytes"]}:
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
        raise _api_error("FILE_INTEGRITY_ERROR", "The workbook export metadata differs from its version", 409)
    target = _api()._resource_path(store, row["relative_path"])
    if row["status"] == "completed" and target.exists():
        content = _safe_read(store, row["relative_path"], file_reference["size_bytes"], file_reference["sha256"])
        try:
            inspect_spreadsheet(content)
        except ValueError as exc:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved XLSX package is no longer readable", 409,
                             {"reason": getattr(exc, "code", str(exc))}) from exc
        return row, content
    if row["status"] == "completed":
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_UNAVAILABLE")
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id))
    return _recover_spreadsheet_export(
        store, workspace_id, artifact_id, version_row, row, identity=identity,
        target_work_id=artifact["work_id"],
    )


def _spreadsheet_response(store, artifact_id: str, version: int, *,
                          workspace_id: str | None = None,
                          identity: dict | None = None) -> dict:
    api = _api()
    workspace_id = workspace_id or _workspace_id()
    identity = _access_identity(identity, workspace_id)
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id)
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, version),
    )
    if version_row is None:
        raise api._not_found()
    export, content = _ensure_spreadsheet_export_ready(
        store, workspace_id, artifact_id, version_row, identity=identity,
    )
    inspected = inspect_spreadsheet(content)
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    spreadsheet = _snapshot_spreadsheet(snapshot)
    with store.transaction(immediate=False) as connection:
        artifact_data = api._artifact_data(connection, artifact, version)
    return {
        "artifact": artifact_data,
        "spreadsheet": {
            "artifact_id": artifact_id,
            "version": version,
            "sha256": export["sha256"],
            "size_bytes": export["size_bytes"],
            "export_id": export["id"],
            "download_url": f"/api/exports/{export['id']}/file",
            "sheets": inspected["sheets"],
            "source": spreadsheet.get("source"),
            "base_file": spreadsheet.get("base_file"),
            "changes": spreadsheet.get("changes", []),
            "recalculated_cells": spreadsheet.get("recalculated_cells", []),
            "charts_updated": spreadsheet.get("charts_updated", []),
            "restored_from_version": spreadsheet.get("restored_from_version"),
            "calculation_engine": spreadsheet.get("calculation_engine"),
            "calculation_revision": spreadsheet.get("calculation_revision"),
            "unchanged_verified": bool(spreadsheet.get("unchanged_verified", False)),
            "warnings": inspected["warnings"] + spreadsheet.get("warnings", []),
        },
    }


def _run_spreadsheet_revision(store, identity: dict, *, initial: bool, operation: dict,
                              material_row=None, artifact_row=None, base_version_row=None,
                              source_reference: dict, base_reference: dict,
                              result: dict, key: str, fingerprint: str):
    api = _api()
    workspace_id = identity["workspace"]["id"]
    artifact_id = str(uuid.uuid4()) if initial else artifact_row["id"]
    version = 1 if initial else base_version_row["version"] + 1
    export_id = str(uuid.uuid4())
    output_reference = _spreadsheet_export_ref(export_id, result["content"])
    created_at = utc_now()
    relative_path = f"objects/{workspace_id}/exports/{export_id}.xlsx"
    export_key = "spreadsheet-version:" + hashlib.sha256(
        f"{workspace_id}:{SPREADSHEET_REVISION_OPERATION}:{key}".encode("utf-8")
    ).hexdigest()
    replay_export_id = None
    saved_spreadsheet = {
        "source": source_reference,
        "base_file": base_reference,
        "file": output_reference,
        "changes": result["changes"],
        "recalculated_cells": result["recalculated_cells"],
        "charts_updated": result.get("charts_updated", []),
        "calculation_engine": result["calculation_engine"],
        "calculation_revision": result["calculation_revision"],
        "unchanged_verified": bool(result["unchanged_verified"]),
        "warnings": result["warnings"],
    }
    revision_body = f"XLSX 单元格修订：{len(result['changes'])} 个单元格。"

    with store.transaction() as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
        existing = api._idempotent_resource(connection, workspace_id, SPREADSHEET_REVISION_OPERATION, key, fingerprint)
        if existing is not None:
            if existing["resource_type"] != "export":
                raise _api_error("IDEMPOTENCY_CONFLICT", "The existing workbook revision record is invalid", 409)
            replay_export_id = existing["resource_id"]
        elif initial:
            from .collaboration import require_material_access

            current_material = connection.execute(
                "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                (material_row["id"], workspace_id),
            ).fetchone()
            if (current_material is None or current_material["sha256"] != source_reference["sha256"]
                    or current_material["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original workbook changed during revision", 409)
            work_id = operation.get("work_id") or current_material["work_id"]
            if work_id:
                work = api._work_row(connection, workspace_id, work_id)
                if work is None:
                    raise api._not_found()
                if work["status"] != "active":
                    raise _api_error("WORK_ARCHIVED", "Reopen this work before saving a workbook revision", 409)
                connection.execute(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    (work_id, current_material["id"], workspace_id, created_at),
                )
            authorized_source = require_material_access(
                connection, identity, source_reference["material_id"],
            )
            if (authorized_source["sha256"] != source_reference["sha256"]
                    or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original workbook changed during revision", 409)
            _authorize_material_scope(connection, identity, authorized_source, work_id,
                                      write_target=True)
            seed = {"materials": [{"id": source_reference["material_id"],
                                   "sha256": source_reference["sha256"], "format": "xlsx",
                                   "status": current_material["status"], "filename": current_material["filename"]}]}
            snapshot = api._input_snapshot(connection, workspace_id, work_id, [], seed)
            title = operation.get("title") or f"{current_material['title']}（工作簿）"
            connection.execute(
                "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                (artifact_id, workspace_id, work_id, identity["user"]["id"], title, created_at, created_at),
            )
            citations_json = "[]"
            parent_version = None
        else:
            from .collaboration import require_artifact_access, require_material_access

            current_artifact = require_artifact_access(connection, identity, artifact_id, write=True)
            if current_artifact["current_version"] != base_version_row["version"]:
                raise _api_error("VERSION_CONFLICT", "The workbook has changed since it was opened", 409,
                                 {"current": api._artifact_data(connection, current_artifact)})
            saved_base = connection.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                (artifact_id, workspace_id, base_version_row["version"]),
            ).fetchone()
            if saved_base is None:
                raise api._not_found()
            saved_snapshot = json.loads(saved_base["input_snapshot_json"] or "{}")
            saved = _snapshot_spreadsheet(saved_snapshot)
            saved_file = _snapshot_file_reference(saved, "file")
            if saved_file.get("export_id") != base_reference.get("export_id"):
                raise _api_error("VERSION_CONFLICT", "The workbook base file changed while it was being revised", 409)
            base_export = connection.execute(
                "SELECT id, sha256, size_bytes, status FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'xlsx'",
                (base_reference.get("export_id"), workspace_id, artifact_id),
            ).fetchone()
            if (base_export is None or base_export["status"] != "completed"
                    or base_export["sha256"] != base_reference.get("sha256")
                    or base_export["size_bytes"] != base_reference.get("size_bytes")):
                raise _api_error("FILE_INTEGRITY_ERROR", "The current workbook version is not verified", 409)
            work_id = current_artifact["work_id"]
            authorized_source = require_material_access(
                connection, identity, source_reference["material_id"],
            )
            if (authorized_source["sha256"] != source_reference["sha256"]
                    or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original workbook changed during revision", 409)
            _authorize_material_scope(connection, identity, authorized_source, work_id,
                                      write_target=True)
            snapshot = api._input_snapshot(connection, workspace_id, work_id, [], saved_snapshot)
            citations = api._validate_citations(connection, workspace_id,
                                                json.loads(saved_base["citations_json"] or "[]"))
            citations_json = json.dumps(citations, ensure_ascii=False)
            title = operation.get("title") or current_artifact["title"]
            parent_version = base_version_row["version"]
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
                (artifact_id, workspace_id, version, title, revision_body, citations_json,
                 operation["revision_note"], created_at, identity["user"]["id"], parent_version),
            )

        snapshot["kind"] = "spreadsheet"
        snapshot["spreadsheet"] = saved_spreadsheet
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(snapshot_json.encode("utf-8")) > 1_000_000:
            raise _api_error("SPREADSHEET_DIFF_TOO_LARGE", "The saved workbook revision record exceeds its size limit", 413)
        if initial:
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (artifact_id, workspace_id, title, revision_body, citations_json, snapshot_json,
                 operation["revision_note"], created_at, identity["user"]["id"]),
            )
            work_id = operation.get("work_id") or material_row["work_id"]
            if work_id:
                connection.execute(
                    "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    (work_id, artifact_id, workspace_id, created_at),
                )
        else:
            connection.execute(
                "UPDATE artifact_versions SET input_snapshot_json = ? WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                (snapshot_json, artifact_id, workspace_id, version),
            )
            updated = connection.execute(
                "UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?",
                (title, version, created_at, artifact_id, workspace_id, parent_version),
            )
            if updated.rowcount != 1:
                raise _api_error("VERSION_CONFLICT", "The workbook has changed since it was opened", 409)
        connection.execute(
            "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, 'xlsx', ?, ?, 'writing', ?, ?, ?)",
            (export_id, workspace_id, artifact_id, version, export_key, fingerprint, relative_path, created_at, created_at),
        )
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, ?, ?, ?, 'export', ?, ?)",
            (workspace_id, SPREADSHEET_REVISION_OPERATION, key, fingerprint, export_id, created_at),
        )

    if replay_export_id:
        replay = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'xlsx'",
                           (replay_export_id, workspace_id))
        if replay is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The concurrent workbook revision could not be read back", 409)
        row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                        (replay["artifact_id"], workspace_id, replay["version"]))
        _ensure_spreadsheet_export_ready(store, workspace_id, replay["artifact_id"], row,
                                         identity=identity)
        return replay["artifact_id"], replay["version"], replay["id"], False

    export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id))
    try:
        readback = _write_export_file(store, export_row, result["content"])
        inspect_spreadsheet(readback)
        _complete_export(store, export_id, workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export_id, workspace_id, "SPREADSHEET_SAVE_FAILED")
        if hasattr(exc, "code"):
            raise
        raise _api_error("SPREADSHEET_SAVE_FAILED", "The XLSX version was saved as recoverable; retry with the same idempotency key", 500,
                         {"export_id": export_id}) from exc
    return artifact_id, version, export_id, True


def _recover_spreadsheet_replay(store, replay: dict, *, workspace_id: str,
                                identity: dict | None = None, status: int = 200):
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (replay["artifact_id"], workspace_id, replay["version"]),
    )
    if version_row is None:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook version is unavailable", 409)
    identity = _access_identity(identity, workspace_id)
    _ensure_spreadsheet_export_ready(store, workspace_id, replay["artifact_id"], version_row,
                                     identity=identity)
    return {"data": _spreadsheet_response(store, replay["artifact_id"], replay["version"],
                                           workspace_id=workspace_id, identity=identity), "status": status}


def _initial_spreadsheet_revision(store, material_id: str, data: dict):
    identity = _api()._require_write()
    result = revise_spreadsheet_file(store, identity, None,
                                     {**data, "material_id": material_id},
                                     _api()._idempotency_key())
    return _api()._success(result["data"], result["status"])


def _followup_spreadsheet_revision(store, artifact_id: str, data: dict):
    result = revise_spreadsheet_file(store, _api()._require_write(), artifact_id,
                                     data, _api()._idempotency_key())
    return _api()._success(result["data"], result["status"])


def revise_spreadsheet_file(store, identity: dict, artifact_id: str | None,
                            data: dict, idempotency_key: str) -> dict:
    """Apply one original-backed workbook revision without HTTP request state.

    With ``artifact_id=None``, data must include ``material_id`` and creates
    version 1. Otherwise data carries ``base_version`` for a follow-up.
    Returns ``{"data": {"artifact", "spreadsheet"}, "status": HTTP-like status}``.
    """
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or not isinstance(identity.get("workspace", {}).get("id"), str)
            or not isinstance(identity.get("user"), dict)
            or not isinstance(identity.get("user", {}).get("id"), str)):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
    if artifact_id is None:
        material_id = data.get("material_id") if isinstance(data, dict) else None
        if not isinstance(material_id, str) or not material_id:
            raise _api_error("INVALID_INPUT", "material_id is required for a new workbook revision", 400)
        operation_data = {key: value for key, value in data.items() if key != "material_id"}
        operation = _normalize_initial_spreadsheet_request(operation_data, material_id)
    else:
        operation = _normalize_followup_spreadsheet_request(data, artifact_id)
    key, fingerprint = _spreadsheet_fingerprint(operation, idempotency_key)
    replay = _find_spreadsheet_replay(store, workspace_id, key, fingerprint)
    if replay is not None:
        return _recover_spreadsheet_replay(store, replay, workspace_id=workspace_id,
                                           identity=identity)

    if artifact_id is None:
        material, content = _read_spreadsheet_material(
            store, workspace_id, operation["material_id"], identity=identity,
            target_work_id=(operation.get("work_id") or _TARGET_WORK_UNSET),
            write_target=True,
        )
        source_reference = _spreadsheet_material_ref(material)
        try:
            result = revise_spreadsheet(content, operation["changes"])
        except ValueError as exc:
            raise _as_spreadsheet_error(exc) from exc
        operation["result"] = result
        saved_artifact_id, version, _export_id, created = _run_spreadsheet_revision(
            store, identity, initial=True, operation=operation, material_row=material,
            source_reference=source_reference, base_reference=dict(source_reference), result=result,
            key=key, fingerprint=fingerprint,
        )
        return {"data": _spreadsheet_response(store, saved_artifact_id, version,
                                               workspace_id=workspace_id, identity=identity),
                "status": 201 if created else 200}

    api = _api()
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id, write=True)
    if artifact["current_version"] != operation["base_version"]:
        with store.transaction(immediate=False) as connection:
            latest = api._artifact_row(connection, workspace_id, artifact_id)
            raise _api_error("VERSION_CONFLICT", "The workbook has changed since it was opened", 409,
                             {"current": api._artifact_data(connection, latest)})
    base_version = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, operation["base_version"]),
    )
    if base_version is None:
        raise api._not_found()
    try:
        base_snapshot = json.loads(base_version["input_snapshot_json"] or "{}")
        spreadsheet = _snapshot_spreadsheet(base_snapshot)
        base_reference = _snapshot_file_reference(spreadsheet, "file")
    except (TypeError, ValueError) as exc:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The workbook version record is unreadable", 409) from exc
    _export, content = _ensure_spreadsheet_export_ready(
        store, workspace_id, artifact_id, base_version, identity=identity,
    )
    try:
        result = revise_spreadsheet(content, operation["changes"])
    except ValueError as exc:
        raise _as_spreadsheet_error(exc) from exc
    operation["result"] = result
    saved_artifact_id, version, _export_id, _created = _run_spreadsheet_revision(
        store, identity, initial=False, operation=operation, artifact_row=artifact,
        base_version_row=base_version, source_reference=spreadsheet["source"],
        base_reference=base_reference, result=result, key=key, fingerprint=fingerprint,
    )
    return {"data": _spreadsheet_response(store, saved_artifact_id, version,
                                           workspace_id=workspace_id, identity=identity), "status": 200}


def revise_spreadsheet_artifact(store, identity: dict, artifact_id: str,
                                data: dict, idempotency_key: str) -> dict:
    """Pure follow-up use case for AI and cross-artifact RevisionBatch callers."""
    if not isinstance(artifact_id, str) or not artifact_id:
        raise _api_error("INVALID_INPUT", "artifact_id is required for a workbook revision", 400)
    return revise_spreadsheet_file(store, identity, artifact_id, data, idempotency_key)["data"]


def render_spreadsheet_export(store, version_row, *, identity: dict | None = None) -> bytes:
    """Return the exact original-backed XLSX bytes for an Artifact version."""
    _export, content = _ensure_spreadsheet_export_ready(
        store, version_row["workspace_id"], version_row["artifact_id"], version_row,
        identity=identity)
    return content


def validate_spreadsheet_export(content: bytes, version_row) -> bool:
    """Verify the saved XLSX export reference and its readable OOXML package."""
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        spreadsheet = _snapshot_spreadsheet(snapshot)
        file_reference = _snapshot_file_reference(spreadsheet, "file")
        if (len(content) != file_reference.get("size_bytes")
                or hashlib.sha256(content).hexdigest() != file_reference.get("sha256")):
            return False
        inspect_spreadsheet(content)
        return True
    except (ValueError, TypeError, KeyError, _api().ApiError):
        return False


def prepare_restored_spreadsheet_export(store, workspace_id: str, artifact_id: str,
                                        source_version_row, target_version: int, *,
                                        identity: dict | None = None) -> dict | None:
    """Prepare a new immutable XLSX export for a restored Artifact version."""
    try:
        snapshot = json.loads(source_version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("SPREADSHEET_RECOVERY_REQUIRED", "The saved workbook version record is unreadable", 409) from exc
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "spreadsheet":
        return None
    identity = _access_identity(identity, workspace_id)
    _ensure_native_artifact_dependencies(store, identity, artifact_id, snapshot, write=True)
    spreadsheet = _snapshot_spreadsheet(snapshot)
    _source_export, content = _ensure_spreadsheet_export_ready(
        store, workspace_id, artifact_id, source_version_row, identity=identity)
    export_id = str(uuid.uuid4())
    output_reference = _spreadsheet_export_ref(export_id, content)
    restored_spreadsheet = dict(spreadsheet)
    restored_spreadsheet["base_file"] = dict(spreadsheet["file"])
    restored_spreadsheet["file"] = output_reference
    restored_spreadsheet["changes"] = []
    restored_spreadsheet["recalculated_cells"] = []
    restored_spreadsheet["unchanged_verified"] = True
    restored_spreadsheet["restored_from_version"] = source_version_row["version"]
    restored_snapshot = dict(snapshot)
    restored_snapshot["spreadsheet"] = restored_spreadsheet
    now = utc_now()
    export = {
        "id": export_id, "workspace_id": workspace_id, "artifact_id": artifact_id,
        "version": target_version, "format": "xlsx",
        "idempotency_key": f"spreadsheet-restore:{artifact_id}:{target_version}",
        "request_hash": _api()._request_fingerprint({"artifact_id": artifact_id,
                                                       "source_version": source_version_row["version"],
                                                       "target_version": target_version,
                                                       "sha256": output_reference["sha256"]}),
        "status": "writing", "relative_path": f"objects/{workspace_id}/exports/{export_id}.xlsx",
        "created_at": now, "updated_at": now,
    }
    return {"snapshot": restored_snapshot, "export": export, "content": content}


def persist_restored_spreadsheet_export(store, prepared: dict, *,
                                        identity: dict | None = None) -> None:
    """Write and read back the exact restored XLSX under its new export identity."""
    export = prepared["export"]
    workspace_id = export["workspace_id"]
    identity = _access_identity(identity, workspace_id)
    try:
        _ensure_native_artifact_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?",
                        (export["id"], workspace_id))
        readback = _write_export_file(store, row, prepared["content"])
        inspect_spreadsheet(readback)
        _ensure_native_artifact_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        _complete_export(store, export["id"], workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export["id"], workspace_id, "EXPORT_FAILED")
        if isinstance(exc, _api().ApiError):
            raise
        raise _api_error("EXPORT_FAILED", "The restored XLSX file needs recovery", 500,
                         {"export_id": export["id"]}) from exc


def _as_pdf_error(exc: ValueError):
    code = getattr(exc, "code", "PDF_REVISION_FAILED")
    details = getattr(exc, "details", {})
    return _api_error("PDF_REVISION_NOT_AVAILABLE", "The PDF opinion could not be anchored safely", 422,
                      {"reason": code, **details})


def _read_pdf_material(store, workspace_id: str, material_id: str, *,
                       identity: dict | None = None, target_work_id=_TARGET_WORK_UNSET,
                       write_target: bool = False):
    identity = _access_identity(identity, workspace_id)
    row = _authorized_material_row(store, identity, workspace_id, material_id,
                                   target_work_id, write_target=write_target)
    if Path(row["filename"]).suffix.lower() != ".pdf" or row["format"] not in {"pdf", None}:
        raise _api_error("UNSUPPORTED_PDF", "This material is not a supported PDF file", 415)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    try:
        inspect_pdf(content)
    except ValueError as exc:
        raise _as_pdf_error(exc) from exc
    return row, content


def _pdf_material_ref(row) -> dict:
    return {"kind": "material", "material_id": row["id"], "filename": row["filename"],
            "sha256": row["sha256"], "size_bytes": row["size_bytes"], "format": "pdf"}


def _snapshot_pdf(snapshot) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "pdf_document":
        raise _api_error("ARTIFACT_NOT_PDF", "This artifact version is not an original-backed PDF", 409)
    document = snapshot.get("pdf_document")
    if (not isinstance(document, dict) or not isinstance(document.get("file"), dict)
            or document["file"].get("format") != "pdf"):
        raise _api_error("PDF_FILE_UNAVAILABLE", "This artifact version has no verified PDF file reference", 409)
    return document


def _pdf_export_ref(export_id: str, content: bytes) -> dict:
    return {"kind": "export", "export_id": export_id, "format": "pdf",
            "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def _normalize_pdf_changes(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise _api_error("INVALID_INPUT", "changes must contain 1 to 100 page-anchored PDF opinions", 400)
    normalized = []
    total_chars = 0
    for index, item in enumerate(value):
        allowed = {"page", "quote", "comment", "occurrence", "rect", "quads"}
        if (not isinstance(item, dict) or set(item) - allowed
                or not {"page", "comment"} <= item.keys()
                or (("quote" in item) == ("rect" in item or "quads" in item))
                or ("rect" in item and "quads" in item)):
            raise _api_error("INVALID_INPUT", "Each PDF opinion needs a quote or a PDF-coordinate rectangle/quadrilateral", 400,
                             {"index": index})
        page, comment = item["page"], item["comment"]
        quote = item.get("quote")
        occurrence = item.get("occurrence")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise _api_error("INVALID_INPUT", "page must be a positive PDF page number", 400, {"index": index})
        if "quote" in item and (not isinstance(quote, str) or not quote.strip() or len(quote) > 8_000):
            raise _api_error("INVALID_INPUT", "quote must contain the exact selected PDF text", 400,
                             {"index": index})
        if not isinstance(comment, str) or not comment.strip() or len(comment) > 8_000:
            raise _api_error("INVALID_INPUT", "comment must contain the requested opinion", 400,
                             {"index": index})
        if ("quote" in item and occurrence is not None
                and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0)):
            raise _api_error("INVALID_INPUT", "occurrence must be a non-negative matching text index", 400,
                             {"index": index})
        if "quote" not in item and occurrence is not None:
            raise _api_error("INVALID_INPUT", "occurrence is only available for a text quote", 400,
                             {"index": index})
        change = {"page": page, "comment": comment}
        if "quote" in item:
            change["quote"] = quote
            change["occurrence"] = occurrence
            total_chars += len(quote)
        elif "rect" in item:
            rect = item["rect"]
            if (not isinstance(rect, list) or len(rect) != 4
                    or any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in rect)):
                raise _api_error("INVALID_INPUT", "rect must be [left,bottom,right,top] in PDF user-space points", 400,
                                 {"index": index})
            change["rect"] = rect
        else:
            quads = item["quads"]
            if (not isinstance(quads, list) or not 1 <= len(quads) <= 500
                    or any(not isinstance(quad, list) or len(quad) != 8
                           or any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in quad)
                           for quad in quads)):
                raise _api_error("INVALID_INPUT", "quads must contain PDFium-order PDF-coordinate points", 400,
                                 {"index": index})
            change["quads"] = quads
        total_chars += len(comment)
        normalized.append(change)
    if total_chars > 200_000:
        raise _api_error("INVALID_INPUT", "Combined PDF opinion text exceeds the request limit", 413)
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > 1_000_000:
        raise _api_error("INVALID_INPUT", "The PDF opinion request exceeds the size limit", 413)
    return normalized


def _normalize_initial_pdf_request(data: dict, material_id: str) -> dict:
    allowed = {"title", "work_id", "revision_note", "changes"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "PDF revision contains an unsupported field", 400)
    return {
        "material_id": material_id,
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
        "work_id": _api()._optional_string(data.get("work_id"), "work_id", maximum=80),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
        "changes": _normalize_pdf_changes(data.get("changes")),
    }


def _normalize_followup_pdf_request(data: dict, artifact_id: str) -> dict:
    allowed = {"base_version", "title", "revision_note", "changes"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "PDF revision contains an unsupported field", 400)
    return {
        "artifact_id": artifact_id,
        "base_version": _version_from_request(data.get("base_version")),
        "title": _api()._optional_string(data.get("title"), "title", maximum=300),
        "revision_note": _api()._optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "",
        "changes": _normalize_pdf_changes(data.get("changes")),
    }


def _pdf_fingerprint(operation: dict, key: str) -> tuple[str, str]:
    if not key:
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for PDF revisions", 400)
    return key, _api()._request_fingerprint(operation)


def _find_pdf_replay(store, workspace_id: str, key: str, fingerprint: str):
    api = _api()
    with store.transaction(immediate=False) as connection:
        record = api._idempotent_resource(connection, workspace_id, PDF_REVISION_OPERATION, key, fingerprint)
        if record is None:
            return None
        if record["resource_type"] != "export":
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved PDF revision record is invalid", 409)
        export = connection.execute("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'pdf'",
                                    (record["resource_id"], workspace_id)).fetchone()
        if export is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved PDF revision is unavailable", 409)
        artifact = api._artifact_row(connection, workspace_id, export["artifact_id"])
        version = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (export["artifact_id"], workspace_id, export["version"]),
        ).fetchone()
        if artifact is None or version is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The saved PDF version is unavailable", 409)
        return {"export_id": export["id"], "artifact_id": artifact["id"], "version": export["version"]}


def _pdf_file_reference(document: dict, key: str) -> dict:
    reference = document.get(key)
    if (not isinstance(reference, dict) or reference.get("format") != "pdf"
            or not isinstance(reference.get("sha256"), str)
            or type(reference.get("size_bytes")) is not int):
        raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF file reference is incomplete", 409)
    return reference


def _load_pdf_base(store, workspace_id: str, artifact_id: str,
                   reference: dict, *, before_version: int | None = None,
                   identity: dict | None = None,
                   target_work_id: str | None = None) -> bytes:
    if not isinstance(reference, dict) or reference.get("format") != "pdf":
        raise _api_error("PDF_RECOVERY_REQUIRED", "The base PDF reference is unavailable", 409)
    if reference.get("kind") == "material":
        _material, content = _read_pdf_material(
            store, workspace_id, reference.get("material_id"), identity=identity,
            target_work_id=target_work_id,
        )
        if hashlib.sha256(content).hexdigest() != reference.get("sha256") or len(content) != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The original PDF changed since this version was saved", 409)
        return content
    if reference.get("kind") == "export":
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pdf'",
                        (reference.get("export_id"), workspace_id, artifact_id))
        if row is None or row["id"] != reference.get("export_id"):
            raise _api_error("PDF_RECOVERY_REQUIRED", "The base PDF export reference changed", 409)
        if row["sha256"] != reference.get("sha256") or row["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The base PDF does not match its export record", 409)
        if before_version is not None and row["version"] >= before_version:
            raise _api_error("PDF_RECOVERY_REQUIRED", "The base PDF is not an earlier Artifact version", 409)
        version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                                (artifact_id, workspace_id, row["version"]))
        if version_row is None:
            raise _api_error("PDF_RECOVERY_REQUIRED", "The base PDF version is unavailable", 409)
        _export, content = _ensure_pdf_export_ready(
            store, workspace_id, artifact_id, version_row, identity=identity,
        )
        return content
    raise _api_error("PDF_RECOVERY_REQUIRED", "The base PDF reference is unsupported", 409)


def _recover_pdf_export(store, workspace_id: str, artifact_id: str, version_row, export_row,
                        *, identity: dict, target_work_id: str | None):
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    pdf_document = _snapshot_pdf(snapshot)
    file_reference = _pdf_file_reference(pdf_document, "file")
    if file_reference.get("export_id") != export_row["id"]:
        raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF file does not match its Artifact version", 409)
    target = _api()._resource_path(store, export_row["relative_path"])
    if target.exists():
        try:
            content = _safe_read(store, export_row["relative_path"], file_reference["size_bytes"],
                                 file_reference["sha256"])
        except Exception as exc:
            _mark_export_interrupted(store, export_row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            if hasattr(exc, "code"):
                raise
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved PDF cannot be verified", 409) from exc
    else:
        _mark_export_interrupted(store, export_row["id"], workspace_id, "FILE_UNAVAILABLE")
        base = _load_pdf_base(store, workspace_id, artifact_id,
                              _pdf_file_reference(pdf_document, "base_file"),
                              before_version=version_row["version"], identity=identity,
                              target_work_id=target_work_id)
        changes = pdf_document.get("changes")
        if not isinstance(changes, list):
            raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF change record is incomplete", 409)
        if changes:
            requested = []
            for item in changes:
                if (not isinstance(item, dict) or not {"page", "comment"} <= item.keys()
                        or not any(key in item for key in ("quote", "rect", "quads"))):
                    raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF opinion record is invalid", 409)
                requested.append({key: item[key] for key in ("page", "quote", "comment", "occurrence", "rect", "quads")
                                  if key in item})
            try:
                revised = revise_pdf(base, requested, author=pdf_document["author"],
                                     annotated_at=pdf_document["annotated_at"])
                content = revised["content"]
            except ValueError as exc:
                raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF version could not be regenerated", 409,
                                 {"reason": getattr(exc, "code", str(exc))}) from exc
        else:
            content = base
    try:
        inspect_pdf(content)
    except ValueError as exc:
        raise _api_error("PDF_RECOVERY_REQUIRED", "The recovered PDF is not readable", 409,
                         {"reason": getattr(exc, "code", str(exc))}) from exc
    if len(content) != file_reference["size_bytes"] or hashlib.sha256(content).hexdigest() != file_reference["sha256"]:
        raise _api_error("PDF_RECOVERY_REQUIRED", "Regenerated PDF bytes do not match the saved version", 409)
    readback = _write_export_file(store, export_row, content)
    completed = _complete_export(store, export_row["id"], workspace_id, readback)
    return completed, readback


def _ensure_pdf_export_ready(store, workspace_id: str, artifact_id: str, version_row, *,
                             identity: dict | None = None):
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF version record is unreadable", 409) from exc
    pdf_document = _snapshot_pdf(snapshot)
    identity = _access_identity(identity, workspace_id)
    artifact = _ensure_native_artifact_dependencies(store, identity, artifact_id, snapshot)
    file_reference = _pdf_file_reference(pdf_document, "file")
    row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pdf'",
                    (file_reference.get("export_id"), workspace_id, artifact_id))
    if row is None:
        raise _api_error("PDF_FILE_UNAVAILABLE", "The saved PDF export record is unavailable", 409)
    if row["sha256"] not in {None, file_reference["sha256"]} or row["size_bytes"] not in {None, file_reference["size_bytes"]}:
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
        raise _api_error("FILE_INTEGRITY_ERROR", "The PDF export metadata differs from its version", 409)
    target = _api()._resource_path(store, row["relative_path"])
    if row["status"] == "completed" and target.exists():
        content = _safe_read(store, row["relative_path"], file_reference["size_bytes"], file_reference["sha256"])
        try:
            inspect_pdf(content)
        except ValueError as exc:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved PDF is no longer readable", 409,
                             {"reason": getattr(exc, "code", str(exc))}) from exc
        return row, content
    if row["status"] == "completed":
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_UNAVAILABLE")
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id))
    return _recover_pdf_export(store, workspace_id, artifact_id, version_row, row,
                               identity=identity, target_work_id=artifact["work_id"])


def _pdf_response(store, artifact_id: str, version: int, *, workspace_id: str | None = None,
                  identity: dict | None = None) -> dict:
    api = _api()
    workspace_id = workspace_id or _workspace_id()
    identity = _access_identity(identity, workspace_id)
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id)
    version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                            (artifact_id, workspace_id, version))
    if version_row is None:
        raise api._not_found()
    export, content = _ensure_pdf_export_ready(
        store, workspace_id, artifact_id, version_row, identity=identity,
    )
    inspected = inspect_pdf(content)
    pdf_document = _snapshot_pdf(json.loads(version_row["input_snapshot_json"] or "{}"))
    with store.transaction(immediate=False) as connection:
        artifact_data = api._artifact_data(connection, artifact, version)
    return {"artifact": artifact_data,
            "pdf": {"artifact_id": artifact_id, "version": version,
                    "sha256": export["sha256"], "size_bytes": export["size_bytes"],
                    "export_id": export["id"], "download_url": f"/api/exports/{export['id']}/file",
                    "pages": inspected["pages"], "source": pdf_document.get("source"),
                    "base_file": pdf_document.get("base_file"), "changes": pdf_document.get("changes", []),
                    "restored_from_version": pdf_document.get("restored_from_version"),
                    "signature_status": pdf_document.get("signature_status", inspected.get("signature_status")),
                    "unchanged_verified": bool(pdf_document.get("unchanged_verified", False)),
                    "warnings": inspected.get("warnings", []) + pdf_document.get("warnings", [])}}


def _pdf_author(store, identity: dict) -> str:
    user = identity["user"]
    if isinstance(user.get("name"), str) and user["name"].strip():
        return user["name"].strip()[:300]
    row = store.row("SELECT name FROM users WHERE id = ?", (user["id"],))
    if row is not None and isinstance(row["name"], str) and row["name"].strip():
        return row["name"].strip()[:300]
    return user["id"][:300]


def _run_pdf_revision(store, identity: dict, *, initial: bool, operation: dict,
                      material_row=None, artifact_row=None, base_version_row=None,
                      source_reference: dict, base_reference: dict,
                      result: dict, key: str, fingerprint: str,
                      author: str, annotated_at: str):
    api = _api()
    workspace_id = identity["workspace"]["id"]
    artifact_id = str(uuid.uuid4()) if initial else artifact_row["id"]
    version = 1 if initial else base_version_row["version"] + 1
    export_id = str(uuid.uuid4())
    output_reference = {"kind": "export", "export_id": export_id, "format": "pdf",
                        "sha256": hashlib.sha256(result["content"]).hexdigest(),
                        "size_bytes": len(result["content"])}
    created_at = utc_now()
    relative_path = f"objects/{workspace_id}/exports/{export_id}.pdf"
    export_key = "pdf-version:" + hashlib.sha256(
        f"{workspace_id}:{PDF_REVISION_OPERATION}:{key}".encode("utf-8")).hexdigest()
    saved_document = {
        "source": source_reference, "base_file": base_reference, "file": output_reference,
        "changes": result["changes"], "author": author, "annotated_at": annotated_at,
        "signature_status": result.get("signature_status", {}),
        "unchanged_verified": bool(result["unchanged_verified"]), "warnings": result["warnings"],
    }
    revision_body = f"PDF 原页批注：{len(result['changes'])} 项文字锚定意见。"
    replay_export_id = None
    with store.transaction() as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
        existing = api._idempotent_resource(connection, workspace_id, PDF_REVISION_OPERATION, key, fingerprint)
        if existing is not None:
            if existing["resource_type"] != "export":
                raise _api_error("IDEMPOTENCY_CONFLICT", "The existing PDF revision record is invalid", 409)
            replay_export_id = existing["resource_id"]
        elif initial:
            from .collaboration import require_material_access

            current_material = connection.execute("SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                                                  (material_row["id"], workspace_id)).fetchone()
            if (current_material is None or current_material["sha256"] != source_reference["sha256"]
                    or current_material["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original PDF changed during review", 409)
            work_id = operation.get("work_id") or current_material["work_id"]
            if work_id:
                work = api._work_row(connection, workspace_id, work_id)
                if work is None:
                    raise api._not_found()
                if work["status"] != "active":
                    raise _api_error("WORK_ARCHIVED", "Reopen this work before saving PDF opinions", 409)
                connection.execute("INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                                   (work_id, current_material["id"], workspace_id, created_at))
            authorized_source = require_material_access(
                connection, identity, source_reference["material_id"],
            )
            if (authorized_source["sha256"] != source_reference["sha256"]
                    or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original PDF changed during review", 409)
            _authorize_material_scope(connection, identity, authorized_source, work_id,
                                      write_target=True)
            seed = {"materials": [{"id": source_reference["material_id"], "sha256": source_reference["sha256"],
                                   "format": "pdf", "status": current_material["status"],
                                   "filename": current_material["filename"]}]}
            snapshot = api._input_snapshot(connection, workspace_id, work_id, [], seed)
            title = operation.get("title") or f"{current_material['title']}（PDF审阅）"
            connection.execute("INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                               (artifact_id, workspace_id, work_id, identity["user"]["id"], title, created_at, created_at))
            citations_json = "[]"
            parent_version = None
        else:
            from .collaboration import require_artifact_access, require_material_access

            current_artifact = require_artifact_access(connection, identity, artifact_id, write=True)
            if current_artifact["current_version"] != base_version_row["version"]:
                raise _api_error("VERSION_CONFLICT", "The PDF has changed since it was opened", 409,
                                 {"current": api._artifact_data(connection, current_artifact)})
            saved_base = connection.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                                            (artifact_id, workspace_id, base_version_row["version"])).fetchone()
            if saved_base is None:
                raise api._not_found()
            saved_snapshot = json.loads(saved_base["input_snapshot_json"] or "{}")
            saved_pdf = _snapshot_pdf(saved_snapshot)
            if _pdf_file_reference(saved_pdf, "file").get("export_id") != base_reference.get("export_id"):
                raise _api_error("VERSION_CONFLICT", "The PDF base file changed while it was being annotated", 409)
            base_export = connection.execute("SELECT id, sha256, size_bytes, status FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pdf'",
                                             (base_reference.get("export_id"), workspace_id, artifact_id)).fetchone()
            if (base_export is None or base_export["status"] != "completed"
                    or base_export["sha256"] != base_reference.get("sha256")
                    or base_export["size_bytes"] != base_reference.get("size_bytes")):
                raise _api_error("FILE_INTEGRITY_ERROR", "The current PDF version is not verified", 409)
            authorized_source = require_material_access(
                connection, identity, source_reference["material_id"],
            )
            if (authorized_source["sha256"] != source_reference["sha256"]
                    or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                raise _api_error("FILE_INTEGRITY_ERROR", "The original PDF changed during review", 409)
            _authorize_material_scope(
                connection, identity, authorized_source, current_artifact["work_id"],
                write_target=True,
            )
            snapshot = api._input_snapshot(connection, workspace_id, current_artifact["work_id"], [], saved_snapshot)
            citations = api._validate_citations(connection, workspace_id,
                                                json.loads(saved_base["citations_json"] or "[]"))
            citations_json = json.dumps(citations, ensure_ascii=False)
            title = operation.get("title") or current_artifact["title"]
            parent_version = base_version_row["version"]
            connection.execute("INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
                               (artifact_id, workspace_id, version, title, revision_body, citations_json,
                                operation["revision_note"], created_at, identity["user"]["id"], parent_version))

        snapshot["kind"] = "pdf_document"
        snapshot["pdf_document"] = saved_document
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(snapshot_json.encode("utf-8")) > 1_000_000:
            raise _api_error("PDF_DIFF_TOO_LARGE", "The saved PDF opinion record exceeds its size limit", 413)
        if initial:
            connection.execute("INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL)",
                               (artifact_id, workspace_id, title, revision_body, citations_json, snapshot_json,
                                operation["revision_note"], created_at, identity["user"]["id"]))
            work_id = operation.get("work_id") or material_row["work_id"]
            if work_id:
                connection.execute("INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                                   (work_id, artifact_id, workspace_id, created_at))
        else:
            connection.execute("UPDATE artifact_versions SET input_snapshot_json = ? WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                               (snapshot_json, artifact_id, workspace_id, version))
            updated = connection.execute("UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?",
                                         (title, version, created_at, artifact_id, workspace_id, parent_version))
            if updated.rowcount != 1:
                raise _api_error("VERSION_CONFLICT", "The PDF has changed since it was opened", 409)
        connection.execute("INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, 'pdf', ?, ?, 'writing', ?, ?, ?)",
                           (export_id, workspace_id, artifact_id, version, export_key, fingerprint, relative_path, created_at, created_at))
        connection.execute("INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, ?, ?, ?, 'export', ?, ?)",
                           (workspace_id, PDF_REVISION_OPERATION, key, fingerprint, export_id, created_at))

    if replay_export_id:
        replay = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'pdf'",
                           (replay_export_id, workspace_id))
        if replay is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The concurrent PDF revision could not be read back", 409)
        version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                                (replay["artifact_id"], workspace_id, replay["version"]))
        _ensure_pdf_export_ready(store, workspace_id, replay["artifact_id"], version_row,
                                 identity=identity)
        return replay["artifact_id"], replay["version"], replay["id"], False
    export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id))
    try:
        readback = _write_export_file(store, export_row, result["content"])
        inspect_pdf(readback)
        _complete_export(store, export_id, workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export_id, workspace_id, "PDF_SAVE_FAILED")
        if hasattr(exc, "code"):
            raise
        raise _api_error("PDF_SAVE_FAILED", "The PDF review version was saved as recoverable", 500,
                         {"export_id": export_id}) from exc
    return artifact_id, version, export_id, True


def revise_pdf_file(store, identity: dict, artifact_id: str | None,
                    data: dict, idempotency_key: str) -> dict:
    """Apply one PDF annotation revision without HTTP request state."""
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or not isinstance(identity.get("workspace", {}).get("id"), str)
            or not isinstance(identity.get("user"), dict)
            or not isinstance(identity.get("user", {}).get("id"), str)):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
    if artifact_id is None:
        material_id = data.get("material_id") if isinstance(data, dict) else None
        if not isinstance(material_id, str) or not material_id:
            raise _api_error("INVALID_INPUT", "material_id is required for new PDF opinions", 400)
        operation_data = {key: value for key, value in data.items() if key != "material_id"}
        operation = _normalize_initial_pdf_request(operation_data, material_id)
        initial = True
    else:
        operation = _normalize_followup_pdf_request(data, artifact_id)
        initial = False
    if not idempotency_key:
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for PDF revisions", 400)
    fingerprint = _api()._request_fingerprint(operation)
    replay = _find_pdf_replay(store, workspace_id, idempotency_key, fingerprint)
    if replay is not None:
        version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                                (replay["artifact_id"], workspace_id, replay["version"]))
        _ensure_pdf_export_ready(store, workspace_id, replay["artifact_id"], version_row,
                                 identity=identity)
        return {"data": _pdf_response(store, replay["artifact_id"], replay["version"],
                                       workspace_id=workspace_id, identity=identity),
                "status": 200}

    author = _pdf_author(store, identity)
    annotated_at = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    if initial:
        material, content = _read_pdf_material(
            store, workspace_id, operation["material_id"], identity=identity,
            target_work_id=(operation.get("work_id") or _TARGET_WORK_UNSET),
            write_target=True,
        )
        source_reference = _pdf_material_ref(material)
        base_reference = dict(source_reference)
        material_row, artifact_row, base_version_row = material, None, None
    else:
        api = _api()
        from .collaboration import require_artifact_access

        with store.transaction(immediate=False) as connection:
            artifact_row = require_artifact_access(connection, identity, artifact_id, write=True)
        if artifact_row["current_version"] != operation["base_version"]:
            with store.transaction(immediate=False) as connection:
                current = api._artifact_row(connection, workspace_id, artifact_id)
                raise _api_error("VERSION_CONFLICT", "The PDF has changed since it was opened", 409,
                                 {"current": api._artifact_data(connection, current)})
        base_version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                                     (artifact_id, workspace_id, operation["base_version"]))
        if base_version_row is None:
            raise api._not_found()
        snapshot = json.loads(base_version_row["input_snapshot_json"] or "{}")
        pdf_document = _snapshot_pdf(snapshot)
        base_reference = _pdf_file_reference(pdf_document, "file")
        source_reference = pdf_document["source"]
        _export, content = _ensure_pdf_export_ready(
            store, workspace_id, artifact_id, base_version_row, identity=identity,
        )
        material_row = None
    try:
        result = revise_pdf(content, operation["changes"], author=author, annotated_at=annotated_at)
    except ValueError as exc:
        raise _as_pdf_error(exc) from exc
    saved_operation = dict(operation)
    saved_operation["annotated_at"] = annotated_at
    saved_operation["author"] = author
    artifact_id, version, _export_id, created = _run_pdf_revision(
        store, identity, initial=initial, operation=saved_operation,
        material_row=material_row, artifact_row=artifact_row, base_version_row=base_version_row,
        source_reference=source_reference, base_reference=base_reference, result=result,
        key=idempotency_key, fingerprint=fingerprint, author=author, annotated_at=annotated_at,
    )
    return {"data": _pdf_response(store, artifact_id, version, workspace_id=workspace_id,
                                   identity=identity),
            "status": (201 if created else 200) if initial else 200}


def revise_pdf_artifact(store, identity: dict, artifact_id: str, data: dict, idempotency_key: str) -> dict:
    """Pure follow-up PDF use case for AI and cross-artifact RevisionBatch callers."""
    if not isinstance(artifact_id, str) or not artifact_id:
        raise _api_error("INVALID_INPUT", "artifact_id is required for PDF revision", 400)
    return revise_pdf_file(store, identity, artifact_id, data, idempotency_key)["data"]


def _initial_pdf_revision(store, material_id: str, data: dict):
    result = revise_pdf_file(store, _api()._require_write(), None,
                             {**data, "material_id": material_id}, _api()._idempotency_key())
    return _api()._success(result["data"], result["status"])


def _followup_pdf_revision(store, artifact_id: str, data: dict):
    result = revise_pdf_file(store, _api()._require_write(), artifact_id,
                             data, _api()._idempotency_key())
    return _api()._success(result["data"], result["status"])


def render_pdf_revision_export(store, version_row, *, identity: dict | None = None) -> bytes:
    identity = _access_identity(identity, version_row["workspace_id"])
    _export, content = _ensure_pdf_export_ready(store, version_row["workspace_id"],
                                                version_row["artifact_id"], version_row,
                                                identity=identity)
    return content


def validate_pdf_revision_export(content: bytes, version_row) -> bool:
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        pdf_document = _snapshot_pdf(snapshot)
        reference = _pdf_file_reference(pdf_document, "file")
        return (len(content) == reference["size_bytes"]
                and hashlib.sha256(content).hexdigest() == reference["sha256"]
                and inspect_pdf(content)["page_count"] > 0)
    except (ValueError, TypeError, KeyError, _api().ApiError):
        return False


def prepare_restored_pdf_export(store, workspace_id: str, artifact_id: str,
                                source_version_row, target_version: int, *,
                                identity: dict | None = None) -> dict | None:
    try:
        snapshot = json.loads(source_version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("PDF_RECOVERY_REQUIRED", "The saved PDF version record is unreadable", 409) from exc
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "pdf_document":
        return None
    identity = _access_identity(identity, workspace_id)
    _ensure_native_artifact_dependencies(store, identity, artifact_id, snapshot, write=True)
    pdf_document = _snapshot_pdf(snapshot)
    _source_export, content = _ensure_pdf_export_ready(
        store, workspace_id, artifact_id, source_version_row, identity=identity,
    )
    export_id = str(uuid.uuid4())
    output_reference = _pdf_export_ref(export_id, content)
    restored = dict(pdf_document)
    restored["base_file"] = dict(pdf_document["file"])
    restored["file"] = output_reference
    restored["changes"] = []
    restored["restored_from_version"] = source_version_row["version"]
    restored_snapshot = dict(snapshot)
    restored_snapshot["pdf_document"] = restored
    now = utc_now()
    export = {"id": export_id, "workspace_id": workspace_id, "artifact_id": artifact_id,
              "version": target_version, "format": "pdf",
              "idempotency_key": f"pdf-restore:{artifact_id}:{target_version}",
              "request_hash": _api()._request_fingerprint({"artifact_id": artifact_id,
                                                           "source_version": source_version_row["version"],
                                                           "target_version": target_version,
                                                           "sha256": output_reference["sha256"]}),
              "status": "writing", "relative_path": f"objects/{workspace_id}/exports/{export_id}.pdf",
              "created_at": now, "updated_at": now}
    return {"snapshot": restored_snapshot, "export": export, "content": content}


def persist_restored_pdf_export(store, prepared: dict, *, identity: dict | None = None) -> None:
    export = prepared["export"]
    workspace_id = export["workspace_id"]
    identity = _access_identity(identity, workspace_id)
    try:
        _ensure_native_artifact_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?",
                        (export["id"], workspace_id))
        readback = _write_export_file(store, row, prepared["content"])
        inspect_pdf(readback)
        _ensure_native_artifact_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        _complete_export(store, export["id"], workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export["id"], workspace_id, "EXPORT_FAILED")
        if isinstance(exc, _api().ApiError):
            raise
        raise _api_error("EXPORT_FAILED", "The restored PDF file needs recovery", 500,
                         {"export_id": export["id"]}) from exc


def _recover_idempotent_revision(store, replay: dict, *, status: int = 200):
    api = _api()
    workspace_id = _workspace_id()
    artifact = store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (replay["artifact_id"], workspace_id))
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (replay["artifact_id"], workspace_id, replay["version"]),
    )
    if artifact is None or version_row is None:
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved presentation revision is unavailable", 409)
    export, _ = _ensure_export_ready(store, workspace_id, replay["artifact_id"], version_row)
    return _revision_response(store, replay["artifact_id"], replay["version"], export["id"], status=status)


def _fingerprint_initial(operation: dict) -> tuple[str, str]:
    return _revision_key_fingerprint(operation)


def _fingerprint_followup(operation: dict) -> tuple[str, str]:
    return _revision_key_fingerprint(operation)


def _link_picture_materials(connection, identity: dict, workspace_id: str, work_id: str | None,
                            changes: list[dict], created_at: str) -> list[dict]:
    from .collaboration import require_material_access

    references = [item.get("after", {}).get("source") for item in changes
                  if isinstance(item, dict) and item.get("kind") == "picture_replacement"]
    snapshots = {}
    if work_id:
        work = connection.execute("SELECT id, status FROM works WHERE id = ? AND workspace_id = ?",
                                  (work_id, workspace_id)).fetchone()
        if work is None:
            raise _api()._not_found()
        if work["status"] != "active":
            raise _api_error("WORK_ARCHIVED", "Reopen this work before replacing presentation media", 409)
    for reference in references:
        if not isinstance(reference, dict) or reference.get("kind") != "material":
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The selected image material reference is invalid", 409)
        material = require_material_access(connection, identity, reference.get("material_id"))
        if (material is None or material["sha256"] != reference.get("sha256")
                or material["size_bytes"] != reference.get("size_bytes")):
            raise _api_error("FILE_INTEGRITY_ERROR", "The selected image material changed during revision", 409)
        _authorize_material_scope(connection, identity, material, work_id, write_target=True)
        if work_id:
            connection.execute(
                "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_id, material["id"], workspace_id, created_at),
            )
        snapshots[material["id"]] = {"id": material["id"], "sha256": material["sha256"],
                                      "format": material["format"], "status": material["status"],
                                      "filename": material["filename"]}
    return [snapshots[key] for key in sorted(snapshots)]


def _run_revision(store, identity: dict, *, initial: bool, operation: dict,
                  material_row=None, artifact_row=None, base_version_row=None,
                  base_reference: dict, source_reference: dict,
                  work_id: str | None, title: str, key: str, fingerprint: str,
                  allowed_slides: list[int], revision_note: str):
    api = _api()
    workspace_id = identity["workspace"]["id"]
    artifact_id = str(uuid.uuid4()) if initial else artifact_row["id"]
    version = 1 if initial else base_version_row["version"] + 1
    export_id = str(uuid.uuid4())
    digest = hashlib.sha256(operation["result"]["content"]).hexdigest()
    size_bytes = len(operation["result"]["content"])
    relative_path = f"objects/{workspace_id}/exports/{export_id}.pptx"
    export_key = "presentation-version:" + hashlib.sha256(f"{workspace_id}:{PRESENTATION_REVISION_OPERATION}:{key}".encode("utf-8")).hexdigest()
    created_at = utc_now()
    output_file = _output_file_ref(export_id, digest, size_bytes)
    body = _revision_body(operation["result"]["changes"], allowed_slides)
    raced_export_id = None

    with store.transaction() as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
        existing = api._idempotent_resource(connection, workspace_id, PRESENTATION_REVISION_OPERATION, key, fingerprint)
        if existing is not None:
            if existing["resource_type"] != "export":
                raise _api_error("IDEMPOTENCY_CONFLICT", "The existing revision record is invalid", 409)
            raced_export_id = existing["resource_id"]
        else:
            if initial:
                from .collaboration import require_material_access

                current_material = connection.execute(
                    "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                    (material_row["id"], workspace_id),
                ).fetchone()
                if current_material is None or current_material["sha256"] != source_reference["sha256"] or current_material["size_bytes"] != source_reference["size_bytes"]:
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original material changed during revision", 409)
                current_work_id = work_id
                if current_work_id:
                    work = api._work_row(connection, workspace_id, current_work_id)
                    if work is None:
                        raise api._not_found()
                    if work["status"] != "active":
                        raise _api_error("WORK_ARCHIVED", "Reopen this work before saving a presentation result", 409)
                    connection.execute(
                        "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_work_id, material_row["id"], workspace_id, created_at),
                    )
                authorized_source = require_material_access(
                    connection, identity, source_reference["material_id"],
                )
                if (authorized_source["sha256"] != source_reference["sha256"]
                        or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original presentation changed during revision", 409)
                _authorize_material_scope(
                    connection, identity, authorized_source, current_work_id, write_target=True,
                )
                picture_materials = _link_picture_materials(
                    connection, identity, workspace_id, current_work_id,
                    operation["result"]["changes"], created_at,
                )
                snapshot_seed = {"materials": [{
                    "id": source_reference["material_id"],
                    "sha256": source_reference["sha256"],
                    "format": "pptx",
                    "status": current_material["status"],
                    "filename": current_material["filename"],
                }, *picture_materials]}
                snapshot = api._input_snapshot(connection, workspace_id, current_work_id, [], snapshot_seed)
                artifact_title = title
                connection.execute(
                    "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, workspace_id, current_work_id, identity["user"]["id"], artifact_title, created_at, created_at),
                )
                citations_json = "[]"
                parent_version = None
            else:
                from .collaboration import require_artifact_access, require_material_access

                current_artifact = require_artifact_access(
                    connection, identity, artifact_id, write=True,
                )
                if current_artifact["current_version"] != base_version_row["version"]:
                    raise _api_error("VERSION_CONFLICT", "Presentation has changed since it was opened", 409,
                                     {"current": api._artifact_data(connection, current_artifact)})
                saved_base = connection.execute(
                    "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                    (artifact_id, workspace_id, base_version_row["version"]),
                ).fetchone()
                if saved_base is None:
                    raise api._not_found()
                saved_snapshot = json.loads(saved_base["input_snapshot_json"] or "{}")
                saved_presentation = _snapshot_presentation(saved_snapshot)
                if saved_presentation["file"].get("export_id") != base_reference.get("export_id"):
                    raise _api_error("VERSION_CONFLICT", "Presentation base file changed while it was being revised", 409)
                base_export = connection.execute(
                    "SELECT id, sha256, size_bytes, status FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'pptx'",
                    (base_reference["export_id"], workspace_id, artifact_id),
                ).fetchone()
                if (base_export is None or base_export["status"] != "completed"
                        or base_export["sha256"] != base_reference["sha256"]
                        or base_export["size_bytes"] != base_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The current presentation version is not verified", 409)
                current_work_id = current_artifact["work_id"]
                authorized_source = require_material_access(
                    connection, identity, source_reference["material_id"],
                )
                if (authorized_source["sha256"] != source_reference["sha256"]
                        or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original presentation changed during revision", 409)
                _authorize_material_scope(
                    connection, identity, authorized_source, current_work_id, write_target=True,
                )
                picture_materials = _link_picture_materials(
                    connection, identity, workspace_id, current_work_id,
                    operation["result"]["changes"], created_at,
                )
                snapshot = api._input_snapshot(connection, workspace_id, current_work_id, [], saved_snapshot)
                citations = json.loads(saved_base["citations_json"])
                citations = api._validate_citations(connection, workspace_id, citations)
                citations_json = json.dumps(citations, ensure_ascii=False)
                artifact_title = title or current_artifact["title"]
                parent_version = base_version_row["version"]
                if picture_materials:
                    saved_materials = {item["id"]: item for item in snapshot.get("materials", [])
                                       if isinstance(item, dict) and isinstance(item.get("id"), str)}
                    saved_materials.update({item["id"]: item for item in picture_materials})
                    snapshot["materials"] = [saved_materials[item_id] for item_id in sorted(saved_materials)]
                connection.execute(
                    "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
                    (artifact_id, workspace_id, version, artifact_title, body, citations_json, revision_note, created_at, identity["user"]["id"], parent_version),
                )

            snapshot["kind"] = "presentation"
            snapshot["presentation"] = {
                "source": source_reference,
                "base_file": base_reference,
                "file": output_file,
                "allowed_slides": allowed_slides,
                "changes": operation["result"]["changes"],
            "unchanged_verified": bool(operation["result"].get("unchanged_verified")),
                "warnings": operation["result"].get("warnings", []),
            }
            snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            if len(snapshot_json.encode("utf-8")) > 1_000_000:
                raise _api_error("PRESENTATION_DIFF_TOO_LARGE", "The saved revision record exceeds its size limit", 413)

            if initial:
                connection.execute(
                    "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (artifact_id, workspace_id, artifact_title, body, citations_json, snapshot_json, revision_note, created_at, identity["user"]["id"]),
                )
                if current_work_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_work_id, artifact_id, workspace_id, created_at),
                    )
            else:
                connection.execute(
                    "UPDATE artifact_versions SET input_snapshot_json = ? WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                    (snapshot_json, artifact_id, workspace_id, version),
                )
                updated = connection.execute(
                    "UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?",
                    (artifact_title, version, created_at, artifact_id, workspace_id, parent_version),
                )
                if updated.rowcount != 1:
                    raise _api_error("VERSION_CONFLICT", "Presentation has changed since it was opened", 409)

            connection.execute(
                "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, 'pptx', ?, ?, 'writing', ?, ?, ?)",
                (export_id, workspace_id, artifact_id, version, export_key, fingerprint, relative_path, created_at, created_at),
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, ?, ?, ?, 'export', ?, ?)",
                (workspace_id, PRESENTATION_REVISION_OPERATION, key, fingerprint, export_id, created_at),
            )

    if raced_export_id:
        raced = store.row("SELECT artifact_id, version FROM exports WHERE id = ? AND workspace_id = ?", (raced_export_id, workspace_id))
        if raced is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The concurrent revision could not be read back", 409)
        raced_version = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (raced["artifact_id"], workspace_id, raced["version"]),
        )
        if raced_version is None:
            raise _api_error("IDEMPOTENCY_CONFLICT", "The concurrent presentation version is unavailable", 409)
        _ensure_export_ready(store, workspace_id, raced["artifact_id"], raced_version,
                             identity=identity)
        return False

    export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id))
    try:
        readback = _write_export_file(store, export_row, operation["result"]["content"])
        _inspect(readback)
        _complete_export(store, export_id, workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export_id, workspace_id, "PRESENTATION_SAVE_FAILED")
        if hasattr(exc, "code"):
            raise
        raise _api_error("PRESENTATION_SAVE_FAILED", "The PPTX version was saved as recoverable; retry with the same idempotency key", 500, {"export_id": export_id}) from exc
    return True


def _initial_revision(store, material_id: str, data: dict):
    api = _api()
    identity = api._require_write()
    workspace_id = identity["workspace"]["id"]
    operation = _normalize_initial_request(data, material_id)
    key, fingerprint = _fingerprint_initial(operation)
    replay = _find_revision_replay(store, workspace_id, key, fingerprint)
    if replay is not None:
        return _recover_idempotent_revision(store, replay)

    requested_work_id = operation["work_id"] if "work_id" in data else _TARGET_WORK_UNSET
    material, content = _read_material(
        store, workspace_id, material_id, identity=identity,
        target_work_id=requested_work_id, write_target=True,
    )
    title = operation["title"] or f"{material['title']}（演示文稿）"
    work_id = operation["work_id"] if "work_id" in data else material["work_id"]
    source_reference = _material_file_ref(material)
    base_reference = dict(source_reference)
    try:
        result = _revise_with_effective_changes(
            content, operation["changes"], operation["allowed_slides"],
            store=store, workspace_id=workspace_id, identity=identity,
            target_work_id=(operation["work_id"] if "work_id" in data
                            else material["work_id"]),
        )
    except ValueError as exc:
        raise _as_presentation_error(exc) from exc
    operation["result"] = result
    created = _run_revision(
        store, identity, initial=True, operation=operation,
        material_row=material, artifact_row=None, base_version_row=None,
        base_reference=base_reference,
        source_reference=source_reference, work_id=work_id, title=title,
        key=key, fingerprint=fingerprint,
        allowed_slides=operation["allowed_slides"], revision_note=operation["revision_note"],
    )
    record = _find_revision_replay(store, workspace_id, key, fingerprint)
    if record is None:
        raise _api_error("PRESENTATION_SAVE_FAILED", "The presentation revision was not recorded", 500)
    return _recover_idempotent_revision(store, record, status=201 if created else 200)


def _followup_revision(store, artifact_id: str, data: dict):
    api = _api()
    result = revise_presentation_artifact(store, api._require_write(), artifact_id,
                                          data, api._idempotency_key())
    return api._success(result, 200)


def revise_presentation_artifact(store, identity: dict, artifact_id: str,
                                 data: dict, idempotency_key: str) -> dict:
    """Pure follow-up PPTX use case for AI and cross-artifact RevisionBatch callers."""
    api = _api()
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or not isinstance(identity.get("workspace", {}).get("id"), str)
            or not isinstance(identity.get("user"), dict)
            or not isinstance(identity.get("user", {}).get("id"), str)):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    if not isinstance(artifact_id, str) or not artifact_id:
        raise _api_error("INVALID_INPUT", "artifact_id is required for a presentation revision", 400)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for presentation revisions", 400)
    workspace_id = identity["workspace"]["id"]
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
        require_artifact_access(connection, identity, artifact_id, write=True)
    operation = _normalize_followup_request(data, artifact_id)
    fingerprint = api._request_fingerprint(operation)
    replay = _find_revision_replay(store, workspace_id, idempotency_key, fingerprint)
    if replay is not None:
        version_row = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (replay["artifact_id"], workspace_id, replay["version"]),
        )
        if version_row is None:
            raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved presentation version is unavailable", 409)
        _ensure_export_ready(store, workspace_id, replay["artifact_id"], version_row,
                             identity=identity)
        return _presentation_revision_data(store, workspace_id, replay["artifact_id"], replay["version"],
                                           replay["export_id"])

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id, write=True)
    if artifact["current_version"] != operation["base_version"]:
        with store.transaction(immediate=False) as connection:
            latest = api._artifact_row(connection, workspace_id, artifact_id)
            raise _api_error("VERSION_CONFLICT", "Presentation has changed since it was opened", 409,
                             {"current": api._artifact_data(connection, latest)})
    base_version = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, operation["base_version"]),
    )
    if base_version is None:
        raise api._not_found()
    try:
        base_snapshot = json.loads(base_version["input_snapshot_json"] or "{}")
        presentation = _snapshot_presentation(base_snapshot)
        base_reference = presentation["file"]
        source_reference = presentation["source"]
    except (TypeError, ValueError) as exc:
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved presentation version is unreadable", 409) from exc
    _export, content = _ensure_export_ready(
        store, workspace_id, artifact_id, base_version, identity=identity,
    )
    try:
        result = _revise_with_effective_changes(
            content, operation["changes"], operation["allowed_slides"],
            store=store, workspace_id=workspace_id, identity=identity,
            target_work_id=artifact["work_id"],
        )
    except ValueError as exc:
        raise _as_presentation_error(exc) from exc
    operation["result"] = result
    title = operation["title"] or artifact["title"]
    _run_revision(
        store, identity, initial=False, operation=operation,
        material_row=None, artifact_row=artifact, base_version_row=base_version,
        base_reference=base_reference, source_reference=source_reference,
        work_id=artifact["work_id"], title=title, key=idempotency_key,
        fingerprint=fingerprint, allowed_slides=operation["allowed_slides"],
        revision_note=operation["revision_note"],
    )
    replay = _find_revision_replay(store, workspace_id, idempotency_key, fingerprint)
    if replay is None:
        raise _api_error("PRESENTATION_SAVE_FAILED", "The presentation revision was not recorded", 500)
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (replay["artifact_id"], workspace_id, replay["version"]),
    )
    if version_row is None:
        raise _api_error("PRESENTATION_RECOVERY_REQUIRED", "The saved presentation version is unavailable", 409)
    _ensure_export_ready(store, workspace_id, replay["artifact_id"], version_row,
                         identity=identity)
    return _presentation_revision_data(store, workspace_id, replay["artifact_id"], replay["version"],
                                       replay["export_id"])


def render_presentation_export(store, version_row, *, identity: dict | None = None) -> bytes:
    """Shared export adapter: return the exact immutable PPTX version bytes."""
    _export, content = _ensure_export_ready(
        store, version_row["workspace_id"], version_row["artifact_id"], version_row,
        identity=identity,
    )
    return content


def validate_presentation_export(content: bytes, version_row) -> bool:
    """Shared export validator for a derived PPTX export of this Artifact version."""
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        presentation = _snapshot_presentation(snapshot)
        file_reference = presentation["file"]
        if (len(content) != file_reference.get("size_bytes")
                or hashlib.sha256(content).hexdigest() != file_reference.get("sha256")):
            return False
        inspect_presentation(content)
        return True
    except (ValueError, TypeError, KeyError, _api().ApiError):
        return False


def register_routes(app, store=None) -> None:
    """Register the PPTX routes on the existing authenticated API app."""
    if app.extensions.get("oevira.office_api.registered"):
        return
    store = store or app.extensions["oevira.store"]

    @app.get("/api/materials/<material_id>/presentation")
    def inspect_material_presentation(material_id: str):
        workspace_id = _workspace_id()
        material, content = _read_material(store, workspace_id, material_id)
        inspected = _inspect(content)
        return _api()._success({
            "material_id": material["id"], "filename": material["filename"],
            "sha256": material["sha256"], "size_bytes": material["size_bytes"],
            "slides": inspected["slides"], "warnings": inspected["warnings"],
        })

    @app.get("/api/artifacts/<artifact_id>/presentation")
    def inspect_artifact_presentation(artifact_id: str):
        workspace_id = _workspace_id()
        artifact = store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (artifact_id, workspace_id))
        if artifact is None:
            raise _api()._not_found()
        version_value = request.args.get("version")
        if version_value is None:
            version = artifact["current_version"]
        else:
            try:
                version = int(version_value)
            except ValueError as exc:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400)
        data = _presentation_response(store, artifact_id, version)
        return _api()._success(data)

    @app.post("/api/materials/<material_id>/presentation-revisions")
    def create_initial_presentation_revision(material_id: str):
        return _initial_revision(store, material_id, _api()._body())

    @app.post("/api/artifacts/<artifact_id>/presentation-revisions")
    def create_followup_presentation_revision(artifact_id: str):
        return _followup_revision(store, artifact_id, _api()._body())

    @app.get("/api/materials/<material_id>/spreadsheet")
    def inspect_material_spreadsheet(material_id: str):
        workspace_id = _workspace_id()
        material, content = _read_spreadsheet_material(store, workspace_id, material_id)
        return _api()._success({
            "material_id": material["id"], "filename": material["filename"],
            "sha256": material["sha256"], "size_bytes": material["size_bytes"],
            **inspect_spreadsheet(content),
        })

    @app.get("/api/artifacts/<artifact_id>/spreadsheet")
    def reopen_artifact_spreadsheet(artifact_id: str):
        workspace_id = _workspace_id()
        artifact = store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?",
                             (artifact_id, workspace_id))
        if artifact is None:
            raise _api()._not_found()
        version_value = request.args.get("version")
        if version_value is None:
            version = artifact["current_version"]
        else:
            try:
                version = int(version_value)
            except ValueError as exc:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400)
        return _api()._success(_spreadsheet_response(store, artifact_id, version))

    @app.post("/api/materials/<material_id>/spreadsheet-revisions")
    def create_initial_spreadsheet_revision(material_id: str):
        return _initial_spreadsheet_revision(store, material_id, _api()._body())

    @app.post("/api/artifacts/<artifact_id>/spreadsheet-revisions")
    def create_followup_spreadsheet_revision(artifact_id: str):
        return _followup_spreadsheet_revision(store, artifact_id, _api()._body())

    @app.get("/api/materials/<material_id>/pdf")
    def inspect_material_pdf(material_id: str):
        workspace_id = _workspace_id()
        material, content = _read_pdf_material(store, workspace_id, material_id)
        return _api()._success({
            "material_id": material["id"], "filename": material["filename"],
            "sha256": material["sha256"], "size_bytes": material["size_bytes"],
            **inspect_pdf(content),
        })

    @app.get("/api/artifacts/<artifact_id>/pdf")
    def reopen_artifact_pdf(artifact_id: str):
        workspace_id = _workspace_id()
        artifact = store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?",
                             (artifact_id, workspace_id))
        if artifact is None:
            raise _api()._not_found()
        version_value = request.args.get("version")
        if version_value is None:
            version = artifact["current_version"]
        else:
            try:
                version = int(version_value)
            except ValueError as exc:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise _api_error("INVALID_INPUT", "version must be a positive integer", 400)
        return _api()._success(_pdf_response(store, artifact_id, version))

    @app.post("/api/materials/<material_id>/pdf-revisions")
    def create_initial_pdf_revision(material_id: str):
        return _initial_pdf_revision(store, material_id, _api()._body())

    @app.post("/api/artifacts/<artifact_id>/pdf-revisions")
    def create_followup_pdf_revision(artifact_id: str):
        return _followup_pdf_revision(store, artifact_id, _api()._body())

    revision_handlers = app.extensions.setdefault("oevira.revision_handlers", {})
    revision_handlers["presentation"] = revise_presentation_artifact
    revision_handlers["spreadsheet"] = revise_spreadsheet_artifact
    app.extensions.setdefault("oevira.revision_handlers", {})["pdf_document"] = revise_pdf_artifact
    app.extensions["oevira.office_api.registered"] = True
