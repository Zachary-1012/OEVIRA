"""Authenticated API binding for original DOCX inspection and local revision."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from flask import g, request, send_file

from .auth import utc_now
from .documents import (
    DocumentError,
    MAX_CHANGE_TEXT_CHARS,
    MAX_INPUT_BYTES,
    MAX_REPLACEMENT_IMAGE_BYTES,
    inspect_document,
    revise_document,
)


DOCUMENT_REVISION_OPERATION = "document_revision"
DOCX_MIMETYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TARGET_WORK_UNSET = object()


def _api():
    from . import api

    return api


def _api_error(code: str, message: str, status: int = 400, details: dict | None = None):
    return _api().ApiError(code, message, status, details)


def _workspace_id() -> str:
    return g.identity["workspace"]["id"]


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


def _authorize_material_read(store, identity: dict, material_id: str,
                             target_work_id: str | None, *, write_target: bool = False):
    from .collaboration import require_material_access

    with store.transaction(immediate=False) as connection:
        row = require_material_access(connection, identity, material_id)
        _authorize_material_scope(connection, identity, row, target_work_id,
                                  write_target=write_target)
        return dict(row)


def _access_identity(identity: dict | None, workspace_id: str) -> dict:
    if identity is not None:
        if (isinstance(identity, dict) and isinstance(identity.get("workspace"), dict)
                and identity["workspace"].get("id") == workspace_id
                and isinstance(identity.get("user"), dict)
                and isinstance(identity["user"].get("id"), str)):
            return identity
        raise _api_error("IDENTITY_REQUIRED", "The supplied Word actor does not match this workspace", 401)
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
    raise _api_error("IDENTITY_REQUIRED", "A current authorized Word actor is required", 401)


def _ensure_document_dependencies(store, identity: dict, artifact_id: str,
                                 snapshot: dict, *, write: bool = False):
    from .collaboration import require_artifact_access, require_material_access

    document = _snapshot_document(snapshot)
    references: dict[str, dict] = {}
    source = document.get("source")
    if isinstance(source, dict) and isinstance(source.get("material_id"), str):
        references[source["material_id"]] = source
    for reference in snapshot.get("materials", []):
        if isinstance(reference, dict) and isinstance(reference.get("id"), str):
            references.setdefault(reference["id"], {
                "material_id": reference["id"],
                "sha256": reference.get("sha256"),
                "size_bytes": reference.get("size_bytes"),
            })
    for change in document.get("changes", []):
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
                raise _api_error("FILE_INTEGRITY_ERROR", "A Word source Material differs from its saved reference", 409)
            _authorize_material_scope(
                connection, identity, row, target_work_id, write_target=write,
            )
        return dict(artifact)


def authorize_document_artifact(store, identity: dict, version_row, *,
                               write: bool = False) -> dict:
    """Recheck current Word Artifact and source-Material authority without opening the DOCX."""
    workspace_id = version_row["workspace_id"]
    artifact_id = version_row["artifact_id"]
    identity = _access_identity(identity, workspace_id)
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word version record is unreadable", 409) from exc
    return _ensure_document_dependencies(store, identity, artifact_id, snapshot, write=write)


def _as_document_error(exc: DocumentError):
    code = exc.code
    if code.endswith("TOO_LARGE") or code.endswith("SIZE_LIMIT") or code.endswith("ENTRY_LIMIT"):
        status = 413
    elif code in {"DOCX_SIGNATURE_PRESENT", "DOCX_OBJECT_NOT_FOUND", "DOCX_OBJECT_NOT_EDITABLE",
                  "DOCX_TEXT_MATCH_NOT_FOUND", "DOCX_TEXT_MATCH_AMBIGUOUS", "DOCX_NO_CHANGES"}:
        status = 409 if code in {"DOCX_SIGNATURE_PRESENT"} else 400
    elif code in {"DOCX_MAIN_DOCUMENT_UNSUPPORTED", "DOCX_COMPRESSION_METHOD_UNSUPPORTED"}:
        status = 415
    else:
        status = 400
    messages = {
        "DOCX_SIGNATURE_PRESENT": "This DOCX contains a digital signature; editing would invalidate it.",
        "DOCX_OBJECT_NOT_FOUND": "The selected Word object location is no longer present in this version.",
        "DOCX_OBJECT_NOT_EDITABLE": "The selected object contains a Word feature that is preserved but not editable in this operation.",
        "DOCX_TEXT_MATCH_NOT_FOUND": "The exact selected text is not present in this paragraph.",
        "DOCX_TEXT_MATCH_AMBIGUOUS": "The exact selected text appears more than once in this paragraph.",
        "DOCX_NO_CHANGES": "At least one selected text range must change.",
        "DOCX_MAIN_DOCUMENT_UNSUPPORTED": "This Word package uses a document structure that this editor cannot safely revise.",
        "DOCX_INPUT_TOO_LARGE": "The DOCX exceeds the supported input size.",
    }
    return _api_error(code, messages.get(code, "The DOCX could not be safely inspected or revised."), status)


def _inspect(content: bytes) -> dict:
    try:
        return inspect_document(content)
    except DocumentError as exc:
        raise _as_document_error(exc) from exc


def _revise(content: bytes, changes: list[dict], *, store=None,
            workspace_id: str | None = None, identity: dict | None = None,
            target_work_id: str | None = None) -> dict:
    model_changes = []
    for item in changes:
        if "image_material_id" not in item:
            model_changes.append(item)
            continue
        if not isinstance(item.get("image_material_id"), str):
            raise _api_error("INVALID_INPUT", "image_material_id must identify an image Material", 400)
        if (store is None or not isinstance(workspace_id, str)
                or not isinstance(identity, dict)):
            raise _api_error("DOCX_IMAGE_SOURCE_UNAVAILABLE", "Image replacement needs a workspace Material", 409)
        _image_row, image_bytes, source = _read_image_material(
            store, workspace_id, item["image_material_id"],
            identity=identity, target_work_id=target_work_id,
        )
        expected_hash = item.get("expected_image_sha256")
        if expected_hash is not None and source["sha256"] != expected_hash:
            raise _api_error("DOCX_IMAGE_SOURCE_CHANGED", "The saved replacement image source changed", 409)
        replacement = {"content": image_bytes, "filename": source["filename"], "source": source}
        if "alt_text" in item:
            replacement["alt_text"] = item["alt_text"]
        model_changes.append({"object_id": item["object_id"], "replace_image": replacement})
    try:
        result = revise_document(content, model_changes)
    except DocumentError as exc:
        raise _as_document_error(exc) from exc
    if not result["changes"]:
        raise _api_error("DOCX_NO_CHANGES", "At least one selected text range must change", 400)
    return result


def _safe_read(store, relative_path: str, expected_size: int, expected_sha256: str) -> bytes:
    api = _api()
    if type(expected_size) is not int or expected_size < 0 or expected_size > MAX_INPUT_BYTES:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX has an invalid size record", 409)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX has an invalid hash record", 409)
    path = api._resource_path(store, relative_path)
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX is missing or has changed size", 409)
        with path.open("rb") as stream:
            content = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise _api_error("FILE_UNAVAILABLE", "The saved DOCX is unavailable", 409) from exc
    if len(content) != expected_size or len(content) > MAX_INPUT_BYTES:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX does not match its recorded size", 409)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX failed integrity verification", 409)
    return content


def _read_material(store, workspace_id: str, material_id: str, *,
                   identity: dict | None = None, target_work_id=_TARGET_WORK_UNSET,
                   write_target: bool = False):
    identity = identity or getattr(g, "identity", None)
    if not isinstance(identity, dict) or identity.get("workspace", {}).get("id") != workspace_id:
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    metadata = store.row("SELECT id, work_id FROM materials WHERE id = ? AND workspace_id = ?",
                         (material_id, workspace_id))
    if metadata is None:
        raise _api()._not_found()
    effective_target = metadata["work_id"] if target_work_id is _TARGET_WORK_UNSET else target_work_id
    row = _authorize_material_read(store, identity, material_id, effective_target,
                                   write_target=write_target)
    if Path(row["filename"]).suffix.lower() != ".docx" or row["format"] not in {"docx", None}:
        raise _api_error("UNSUPPORTED_DOCUMENT", "This material is not a supported DOCX file", 415)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    return row, content


def _read_image_material(store, workspace_id: str, material_id: str, *,
                         identity: dict, target_work_id: str | None,
                         write_target: bool = False):
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or identity["workspace"].get("id") != workspace_id):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    row = _authorize_material_read(store, identity, material_id, target_work_id,
                                   write_target=write_target)
    extension = Path(row["filename"]).suffix.lower().lstrip(".")
    if extension not in {"png", "jpg", "jpeg"} or row["format"] not in {extension, None}:
        raise _api_error("UNSUPPORTED_IMAGE_MATERIAL", "Select a PNG or JPEG image material", 415)
    if row["size_bytes"] > MAX_REPLACEMENT_IMAGE_BYTES:
        raise _api_error("DOCX_IMAGE_SIZE_UNSUPPORTED", "The selected image exceeds the replacement size limit", 413)
    content = _safe_read(store, row["relative_path"], row["size_bytes"], row["sha256"])
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            if image.width * image.height > 100_000_000:
                raise _api_error("DOCX_IMAGE_DIMENSIONS_UNSUPPORTED", "The selected image dimensions exceed the replacement limit", 413)
            image.verify()
    except _api().ApiError:
        raise
    except Exception as exc:
        raise _api_error("DOCX_IMAGE_INVALID", "The selected material is not a valid image", 415) from exc
    if ((extension in {"jpg", "jpeg"} and image_format != "JPEG")
            or (extension == "png" and image_format != "PNG")):
        raise _api_error("DOCX_IMAGE_FORMAT_MISMATCH", "The image filename does not match its encoded format", 415)
    canonical_extension = "png" if image_format == "PNG" else "jpg"
    source = {"kind": "material", "material_id": row["id"], "filename": row["filename"],
              "sha256": row["sha256"], "size_bytes": row["size_bytes"],
              "format": canonical_extension}
    return row, content, source


def _material_reference(row) -> dict:
    return {
        "kind": "material",
        "material_id": row["id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "format": "docx",
    }


def _snapshot_document(snapshot) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "document":
        raise _api_error("ARTIFACT_NOT_DOCUMENT", "This Artifact version is not an editable Word document", 409)
    document = snapshot.get("document")
    if (not isinstance(document, dict) or not isinstance(document.get("file"), dict)
            or not isinstance(document.get("base_file"), dict)
            or not isinstance(document.get("source"), dict)):
        raise _api_error("DOCUMENT_FILE_UNAVAILABLE", "This Word version has an incomplete source or file reference", 409)
    return document


def _version_row(store, workspace_id: str, artifact_id: str, version: int):
    row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, version),
    )
    if row is None:
        raise _api()._not_found()
    return row


def _mark_export_interrupted(store, export_id: str, workspace_id: str, code: str) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'interrupted', error_code = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (code, utc_now(), export_id, workspace_id),
        )


def _complete_export(store, export_id: str, workspace_id: str, content: bytes):
    row = _api()._complete_export_record(store, export_id, workspace_id, content)
    return row


def _write_export_file(store, export_row, content: bytes) -> bytes:
    _path, readback = _api()._write_export_bytes(store, export_row, content)
    return readback


def _load_base_file(store, workspace_id: str, artifact_id: str, reference: dict, *,
                    before_version: int, identity: dict, target_work_id: str | None) -> bytes:
    if reference.get("kind") == "material":
        material, content = _read_material(
            store, workspace_id, reference.get("material_id"), identity=identity,
            target_work_id=target_work_id,
        )
        if material["sha256"] != reference.get("sha256") or material["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The original DOCX changed since this version was saved", 409)
        return content
    if reference.get("kind") == "export" and reference.get("format") == "docx":
        row = store.row(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'docx'",
            (reference.get("export_id"), workspace_id, artifact_id),
        )
        if row is None or type(row["version"]) is not int or row["version"] >= before_version:
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The base DOCX export is not an earlier version of this Artifact", 409)
        if row["sha256"] != reference.get("sha256") or row["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("FILE_INTEGRITY_ERROR", "The base DOCX export does not match its saved reference", 409)
        base_version = _version_row(store, workspace_id, artifact_id, row["version"])
        _export, content = _ensure_export_ready(
            store, workspace_id, artifact_id, base_version, identity=identity,
        )
        return content
    raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The base DOCX file reference is unsupported", 409)


def _recover_export(store, workspace_id: str, artifact_id: str, version_row, export_row,
                    *, identity: dict):
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word version record is unreadable", 409) from exc
    document = _snapshot_document(snapshot)
    artifact = _ensure_document_dependencies(store, identity, artifact_id, snapshot)
    reference = document["file"]
    if reference.get("export_id") != export_row["id"]:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved DOCX reference does not match its version", 409)
    base = _load_base_file(
        store, workspace_id, artifact_id, document["base_file"],
        before_version=version_row["version"], identity=identity,
        target_work_id=artifact["work_id"],
    )
    if (version_row["restore_version"] is not None
            and document["base_file"].get("kind") == "export"
            and document["base_file"].get("sha256") == reference.get("sha256")
            and document["base_file"].get("size_bytes") == reference.get("size_bytes")):
        recovered_content = base
        if hashlib.sha256(base).hexdigest() != reference.get("sha256") or len(base) != reference.get("size_bytes"):
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The restored DOCX does not match its saved version", 409)
    else:
        changes = document.get("changes")
        if not isinstance(changes, list) or not changes:
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word change record is incomplete", 409)
        request_changes = []
        for item in changes:
            if (isinstance(item, dict) and item.get("kind") == "tracked_change"
                    and item.get("operation") in {"accept", "reject"}
                    and isinstance(item.get("object_id"), str)):
                request_changes.append({"object_id": item["object_id"],
                                        "operation": item["operation"]})
                continue
            if (isinstance(item, dict) and item.get("kind") == "image_replacement"
                    and isinstance(item.get("object_id"), str)
                    and isinstance(item.get("after"), dict)):
                source = item["after"].get("source")
                if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
                    raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The replacement image source is unavailable", 409)
                request = {"object_id": item["object_id"],
                           "image_material_id": source["material_id"],
                           "expected_image_sha256": source.get("sha256")}
                if item.get("alt_text_updated"):
                    request["alt_text"] = item.get("alt_text", "")
                request_changes.append(request)
                continue
            if not isinstance(item, dict) or not {"object_id", "before", "after"}.issubset(item):
                raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word change record is invalid", 409)
            request_changes.append({"object_id": item["object_id"], "before": item["before"], "after": item["after"]})
        revised = _revise(base, request_changes, store=store, workspace_id=workspace_id,
                          identity=identity, target_work_id=artifact["work_id"])
        if revised["sha256"] != reference.get("sha256") or revised["size_bytes"] != reference.get("size_bytes"):
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The DOCX version could not be reproduced from its parent", 409)
        recovered_content = revised["content"]
    readback = _write_export_file(store, export_row, recovered_content)
    completed = _complete_export(store, export_row["id"], workspace_id, readback)
    return completed, readback


def _ensure_export_ready(store, workspace_id: str, artifact_id: str, version_row, *,
                         identity: dict | None = None):
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word version record is unreadable", 409) from exc
    document = _snapshot_document(snapshot)
    identity = _access_identity(identity, workspace_id)
    _ensure_document_dependencies(store, identity, artifact_id, snapshot)
    file_reference = document["file"]
    row = store.row(
        "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND version = ? AND format = 'docx'",
        (file_reference.get("export_id"), workspace_id, artifact_id, version_row["version"]),
    )
    if row is None:
        # Older restored Artifacts copied the source version's file reference
        # without an export row for the new version. Keep those immutable
        # versions readable while new restores receive their own export row.
        restored_from = version_row["restore_version"]
        if restored_from is not None:
            prior_export = store.row(
                "SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND artifact_id = ? AND format = 'docx'",
                (file_reference.get("export_id"), workspace_id, artifact_id),
            )
            if (prior_export is not None and prior_export["version"] == restored_from
                    and prior_export["sha256"] == file_reference.get("sha256")
                    and prior_export["size_bytes"] == file_reference.get("size_bytes")):
                prior_version = _version_row(store, workspace_id, artifact_id, restored_from)
                return _ensure_export_ready(store, workspace_id, artifact_id, prior_version,
                                            identity=identity)
        raise _api_error("DOCUMENT_FILE_UNAVAILABLE", "The saved DOCX export record is unavailable", 409)
    if (row["sha256"] not in {None, file_reference.get("sha256")}
            or row["size_bytes"] not in {None, file_reference.get("size_bytes")}):
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
        raise _api_error("FILE_INTEGRITY_ERROR", "The DOCX export record differs from its immutable version", 409)

    target = _api()._resource_path(store, row["relative_path"])
    if target.exists():
        try:
            content = _safe_read(store, row["relative_path"], file_reference["size_bytes"], file_reference["sha256"])
        except Exception:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise
        completed = _complete_export(store, row["id"], workspace_id, content)
        return completed, content
    if target.is_symlink() or target.is_dir():
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
        raise _api_error("FILE_INTEGRITY_ERROR", "The saved DOCX path is not a regular file; its contents were preserved", 409)
    _mark_export_interrupted(store, row["id"], workspace_id, "FILE_UNAVAILABLE")
    export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id))
    return _recover_export(store, workspace_id, artifact_id, version_row, export_row,
                           identity=identity)


def prepare_restored_document_export(store, workspace_id: str, artifact_id: str,
                                     source_version_row, target_version: int, *,
                                     identity: dict | None = None) -> dict | None:
    """Create the immutable file/export record for a restored Word version."""
    try:
        snapshot = json.loads(source_version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word version record is unreadable", 409) from exc
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "document":
        return None
    identity = _access_identity(identity, workspace_id)
    _ensure_document_dependencies(store, identity, artifact_id, snapshot, write=True)
    document = _snapshot_document(snapshot)
    _source_export, content = _ensure_export_ready(
        store, workspace_id, artifact_id, source_version_row, identity=identity,
    )

    export_id = str(uuid.uuid4())
    output_file = _output_file_reference(export_id, content)
    restored_document = dict(document)
    restored_document["base_file"] = dict(document["file"])
    restored_document["file"] = output_file
    restored_document["changes"] = []
    restored_document["restored_from_version"] = source_version_row["version"]
    restored_snapshot = dict(snapshot)
    restored_snapshot["document"] = restored_document

    now = utc_now()
    relative_path = f"objects/{workspace_id}/exports/{export_id}.docx"
    export = {
        "id": export_id,
        "workspace_id": workspace_id,
        "artifact_id": artifact_id,
        "version": target_version,
        "format": "docx",
        "idempotency_key": f"document-restore:{artifact_id}:{target_version}",
        "request_hash": _api()._request_fingerprint({
            "artifact_id": artifact_id,
            "source_version": source_version_row["version"],
            "target_version": target_version,
            "sha256": output_file["sha256"],
        }),
        "status": "writing",
        "relative_path": relative_path,
        "created_at": now,
        "updated_at": now,
    }
    return {"snapshot": restored_snapshot, "export": export, "content": content}


def persist_restored_document_export(store, prepared: dict, *,
                                    identity: dict | None = None) -> None:
    """Write and read back the exact restored DOCX under its new export identity."""
    export = prepared["export"]
    workspace_id = export["workspace_id"]
    identity = _access_identity(identity, workspace_id)
    try:
        _ensure_document_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export["id"], workspace_id))
        readback = _write_export_file(store, row, prepared["content"])
        _ensure_document_dependencies(
            store, identity, export["artifact_id"], prepared["snapshot"], write=True,
        )
        _complete_export(store, export["id"], workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export["id"], workspace_id, "EXPORT_FAILED")
        if isinstance(exc, _api().ApiError):
            raise
        raise _api_error("EXPORT_FAILED", "The restored Word file needs recovery", 500,
                         {"export_id": export["id"]}) from exc


def _format_title(value, default: str) -> str:
    if value is None:
        value = default
    return _api()._string(value, "title", maximum=300)


def _normalize_changes(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise _api_error("INVALID_INPUT", "changes must contain 1 to 100 selected Word object edits", 400)
    changes: list[dict] = []
    object_ids: set[str] = set()
    for index, item in enumerate(value):
        tracked_operation = isinstance(item, dict) and set(item) == {"object_id", "operation"}
        text_change = isinstance(item, dict) and set(item) == {"object_id", "before", "after"}
        image_change = (isinstance(item, dict)
                        and set(item) in ({"object_id", "image_material_id"},
                                          {"object_id", "image_material_id", "alt_text"}))
        if not (tracked_operation or text_change or image_change):
            raise _api_error("INVALID_INPUT", "Each change requires a text range, tracked-change operation or image material", 400, {"index": index})
        object_id = _api()._string(item["object_id"], "object_id", maximum=500)
        if object_id in object_ids:
            raise _api_error("INVALID_INPUT", "Each Word paragraph, table cell, header, footer, note or comment object may appear once", 400, {"index": index})
        object_ids.add(object_id)
        if tracked_operation:
            if not isinstance(item["operation"], str) or item["operation"] not in {"accept", "reject"}:
                raise _api_error("INVALID_INPUT", "Tracked-change operation must be accept or reject", 400,
                                 {"index": index})
            changes.append({"object_id": object_id, "operation": item["operation"]})
            continue
        if image_change:
            material_id = _api()._string(item["image_material_id"], "image_material_id", maximum=80)
            replacement = {"object_id": object_id, "image_material_id": material_id}
            if "alt_text" in item:
                alt_text = item["alt_text"]
                if (not isinstance(alt_text, str) or len(alt_text) > 2_000
                        or any(ord(char) < 32 and char not in "\t" for char in alt_text)):
                    raise _api_error("INVALID_INPUT", "alt_text is invalid or exceeds the text limit", 400,
                                     {"index": index})
                replacement["alt_text"] = alt_text
            changes.append(replacement)
            continue
        before = _api()._raw_text(item["before"], "before", maximum=MAX_CHANGE_TEXT_CHARS)
        after = item["after"]
        if not isinstance(after, str) or len(after) > MAX_CHANGE_TEXT_CHARS or any(char in after for char in "\r\n\t"):
            raise _api_error("INVALID_INPUT", "after must be a single-line string within the supported length", 400, {"index": index})
        changes.append({"object_id": object_id, "before": before, "after": after})
    return changes


def _normalize_request(data: dict, *, initial: bool) -> dict:
    allowed = {"title", "work_id", "revision_note", "changes"}
    if not initial:
        allowed = {"base_version", "title", "revision_note", "changes"}
    if set(data) - allowed:
        raise _api_error("INVALID_INPUT", "The Word revision contains an unsupported field", 400,
                         {"fields": sorted(set(data) - allowed)})
    base_version = None
    if not initial:
        base_version = data.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise _api_error("INVALID_INPUT", "base_version must be a positive integer", 400)
    work_id = data.get("work_id")
    if not initial and work_id is not None:
        raise _api_error("INVALID_INPUT", "work_id cannot change on an existing Word Artifact", 400)
    if work_id is not None:
        work_id = _api()._string(work_id, "work_id", maximum=80)
    title = data.get("title")
    if title is not None:
        title = _api()._string(title, "title", maximum=300)
    revision_note = data.get("revision_note", "")
    if not isinstance(revision_note, str) or len(revision_note) > 1000:
        raise _api_error("INVALID_INPUT", "revision_note must be a string of at most 1000 characters", 400)
    return {
        "base_version": base_version,
        "work_id": work_id,
        "title": title,
        "revision_note": revision_note.strip(),
        "changes": _normalize_changes(data.get("changes")),
    }


def _revision_fingerprint(material_id: str | None, artifact_id: str | None, operation: dict) -> str:
    return _api()._request_fingerprint({
        "material_id": material_id,
        "artifact_id": artifact_id,
        "base_version": operation["base_version"],
        "work_id": operation["work_id"],
        "title": operation["title"],
        "revision_note": operation["revision_note"],
        "changes": operation["changes"],
    })


def _ensure_editor(connection, workspace_id: str, user_id: str) -> None:
    member = connection.execute(
        "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if member is None or not member["active"] or member["role"] not in {"owner", "editor"}:
        raise _api_error("FORBIDDEN", "Workspace write access is no longer available", 403)


def _revision_body(change_count: int) -> str:
    return f"Word 对象局部文字修订：{change_count}处。"


def _output_file_reference(export_id: str, content: bytes) -> dict:
    return {
        "kind": "export",
        "export_id": export_id,
        "format": "docx",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _document_data(store, artifact_id: str, version_row, content: bytes, *, changes: list[dict] | None = None) -> dict:
    snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    document = _snapshot_document(snapshot)
    manifest = _inspect(content)
    file_reference = document["file"]
    return {
        "artifact_id": artifact_id,
        "version": version_row["version"],
        "restored_from_version": version_row["restore_version"],
        "source": document["source"],
        "base_file": document["base_file"],
        "file": file_reference,
        "export_id": file_reference["export_id"],
        "sha256": manifest["sha256"],
        "size_bytes": manifest["size_bytes"],
        "download_url": f"/api/document-exports/{file_reference['export_id']}/file",
        "objects": manifest["objects"],
        "warnings": manifest["warnings"],
        "changes": changes if changes is not None else document.get("changes", []),
        "unchanged_verified": True,
    }


def _revision_data(store, workspace_id: str, artifact_id: str, version: int,
                   *, changes: list[dict] | None = None,
                   identity: dict | None = None) -> dict:
    artifact = store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (artifact_id, workspace_id))
    if artifact is None:
        raise _api()._not_found()
    version_row = _version_row(store, workspace_id, artifact_id, version)
    identity = _access_identity(identity, workspace_id)
    _export, content = _ensure_export_ready(
        store, workspace_id, artifact_id, version_row, identity=identity,
    )
    with store.transaction(immediate=False) as connection:
        artifact_data = _api()._artifact_data(connection, artifact, version)
    return {
        "artifact": artifact_data,
        "document": _document_data(store, artifact_id, version_row, content, changes=changes),
    }


def _revision_response(store, artifact_id: str, version: int, *, changes: list[dict] | None = None,
                       status: int = 200):
    return _api()._success(_revision_data(
        store, _workspace_id(), artifact_id, version, changes=changes, identity=g.identity,
    ), status)


def _idempotency_key() -> str:
    key = _api()._idempotency_key()
    if not key:
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for a Word revision", 400)
    return key


def _replay_revision(store, identity: dict, key: str, fingerprint: str):
    workspace_id = identity["workspace"]["id"]
    api = _api()
    with store.transaction(immediate=False) as connection:
        record = api._idempotent_resource(connection, workspace_id, DOCUMENT_REVISION_OPERATION, key, fingerprint)
    if record is None:
        return None
    if record["resource_type"] != "export":
        raise _api_error("IDEMPOTENCY_CONFLICT", "The saved Word revision record is invalid", 409)
    export = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (record["resource_id"], workspace_id))
    if export is None or export["format"] != "docx":
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The original Word revision result is unavailable", 409)
    version_row = _version_row(store, workspace_id, export["artifact_id"], export["version"])
    _ensure_export_ready(store, workspace_id, export["artifact_id"], version_row,
                         identity=identity)
    return _revision_data(store, workspace_id, export["artifact_id"], export["version"],
                          identity=identity)


def _save_revision(store, *, initial: bool, material_row, artifact_row, base_version_row,
                   source_reference: dict, base_reference: dict, operation: dict,
                   result: dict, identity: dict, key: str, fingerprint: str):
    api = _api()
    workspace_id = identity["workspace"]["id"]
    artifact_id = str(uuid.uuid4()) if initial else artifact_row["id"]
    version = 1 if initial else base_version_row["version"] + 1
    export_id = str(uuid.uuid4())
    output_file = _output_file_reference(export_id, result["content"])
    now = utc_now()
    revision_note = operation["revision_note"] or ("局部修订 DOCX 原稿" if initial else "局部修订 DOCX 文稿")
    artifact_title = operation["title"] or (material_row["title"] if initial else artifact_row["title"])
    document_snapshot = {
        "source": source_reference,
        "base_file": base_reference,
        "file": output_file,
        "changes": result["changes"],
    }
    inherited_materials = []
    if not initial and base_version_row is not None:
        try:
            previous_snapshot = json.loads(base_version_row["input_snapshot_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The prior Word material record is unreadable", 409) from exc
        previous_materials = previous_snapshot.get("materials", [])
        if isinstance(previous_materials, list):
            inherited_materials = [
                dict(item) for item in previous_materials
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("sha256"), str)
            ]
    image_sources = {
        item["after"]["source"]["material_id"]: item["after"]["source"]
        for item in result["changes"]
        if (item.get("kind") == "image_replacement" and isinstance(item.get("after"), dict)
            and isinstance(item["after"].get("source"), dict)
            and isinstance(item["after"]["source"].get("material_id"), str))
    }
    export_key = "document-version:" + hashlib.sha256(
        f"{workspace_id}:{DOCUMENT_REVISION_OPERATION}:{key}".encode("utf-8")
    ).hexdigest()
    relative_path = f"objects/{workspace_id}/exports/{export_id}.docx"
    replay_export_id = None

    with store.transaction() as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
        existing = api._idempotent_resource(connection, workspace_id, DOCUMENT_REVISION_OPERATION, key, fingerprint)
        if existing is not None:
            if existing["resource_type"] != "export":
                raise _api_error("IDEMPOTENCY_CONFLICT", "The existing Word revision record is invalid", 409)
            replay_export_id = existing["resource_id"]
        else:
            current_work_id = operation["work_id"]
            if initial:
                from .collaboration import require_material_access

                current_material = connection.execute(
                    "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                    (material_row["id"], workspace_id),
                ).fetchone()
                if (current_material is None or current_material["sha256"] != source_reference["sha256"]
                        or current_material["size_bytes"] != source_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original DOCX changed during revision", 409)
                if current_work_id is None:
                    current_work_id = current_material["work_id"]
                if current_work_id:
                    work = api._work_row(connection, workspace_id, current_work_id)
                    if work is None:
                        raise api._not_found()
                    if work["status"] != "active":
                        raise _api_error("WORK_ARCHIVED", "Reopen this work before saving a Word result", 409)
                    connection.execute(
                        "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_work_id, current_material["id"], workspace_id, now),
                    )
                authorized_source = require_material_access(
                    connection, identity, source_reference["material_id"],
                )
                if (authorized_source["sha256"] != source_reference["sha256"]
                        or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original DOCX changed during revision", 409)
                _authorize_material_scope(
                    connection, identity, authorized_source, current_work_id, write_target=True,
                )
            else:
                from .collaboration import require_artifact_access, require_material_access

                current_artifact = require_artifact_access(
                    connection, identity, artifact_id, write=True,
                )
                if current_artifact["current_version"] != base_version_row["version"]:
                    raise _api_error("VERSION_CONFLICT", "Artifact has changed since it was opened", 409,
                                     {"current": api._artifact_data(connection, current_artifact)})
                current_work_id = current_artifact["work_id"]
                authorized_source = require_material_access(
                    connection, identity, source_reference["material_id"],
                )
                if (authorized_source["sha256"] != source_reference["sha256"]
                        or authorized_source["size_bytes"] != source_reference["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "The original DOCX changed during revision", 409)
                _authorize_material_scope(
                    connection, identity, authorized_source, current_work_id, write_target=True,
                )

            revision_materials_by_id = {
                item["id"]: item for item in inherited_materials
            }
            revision_materials_by_id[source_reference["material_id"]] = {
                "id": source_reference["material_id"],
                "sha256": source_reference["sha256"],
                "format": "docx",
                "status": material_row["status"],
                "filename": source_reference["filename"],
            }
            if inherited_materials:
                from .collaboration import require_material_access

                for inherited in inherited_materials:
                    inherited_row = require_material_access(connection, identity, inherited["id"])
                    if (inherited.get("sha256") is not None
                            and inherited_row["sha256"] != inherited["sha256"]):
                        raise _api_error("FILE_INTEGRITY_ERROR", "A prior Word source Material changed", 409)
                    _authorize_material_scope(
                        connection, identity, inherited_row, current_work_id, write_target=True,
                    )
            for image_source in image_sources.values():
                from .collaboration import require_material_access

                image_row = require_material_access(connection, identity, image_source["material_id"])
                if (image_row is None or image_row["sha256"] != image_source["sha256"]
                        or image_row["size_bytes"] != image_source["size_bytes"]):
                    raise _api_error("FILE_INTEGRITY_ERROR", "A replacement image changed during Word revision", 409)
                _authorize_material_scope(
                    connection, identity, image_row, current_work_id, write_target=True,
                )
                if current_work_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_work_id, image_row["id"], workspace_id, now),
                    )
                revision_materials_by_id[image_row["id"]] = {
                    "id": image_row["id"], "sha256": image_row["sha256"],
                    "format": image_source["format"], "status": image_row["status"],
                    "filename": image_row["filename"],
                }
            revision_materials = list(revision_materials_by_id.values())
            snapshot_prior = {
                "kind": "document",
                "document": document_snapshot,
                "materials": revision_materials,
            }
            snapshot = api._input_snapshot(connection, workspace_id, current_work_id, [], snapshot_prior)
            if initial:
                connection.execute(
                    "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, workspace_id, current_work_id, identity["user"]["id"], artifact_title, now, now),
                )
                version_body = _revision_body(len(result["changes"]))
                connection.execute(
                    "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, '[]', ?, ?, ?, ?, NULL)",
                    (artifact_id, workspace_id, artifact_title, version_body,
                     json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), revision_note, now, identity["user"]["id"]),
                )
                if current_work_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_work_id, artifact_id, workspace_id, now),
                    )
            else:
                version_body = _revision_body(len(result["changes"]))
                connection.execute(
                    "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
                    (artifact_id, workspace_id, version, artifact_title, version_body,
                     json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), revision_note, now,
                     identity["user"]["id"], base_version_row["version"]),
                )
                updated = connection.execute(
                    "UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?",
                    (artifact_title, version, now, artifact_id, workspace_id, base_version_row["version"]),
                )
                if updated.rowcount != 1:
                    raise _api_error("VERSION_CONFLICT", "Artifact has changed since it was opened", 409)

            connection.execute(
                "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, 'docx', ?, ?, 'writing', ?, ?, ?)",
                (export_id, workspace_id, artifact_id, version, export_key, fingerprint, relative_path, now, now),
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, ?, ?, ?, 'export', ?, ?)",
                (workspace_id, DOCUMENT_REVISION_OPERATION, key, fingerprint, export_id, now),
            )

    if replay_export_id:
        replay = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (replay_export_id, workspace_id))
        if replay is None or replay["format"] != "docx":
            raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The original Word revision result is unavailable", 409)
        replay_version = _version_row(store, workspace_id, replay["artifact_id"], replay["version"])
        _ensure_export_ready(store, workspace_id, replay["artifact_id"], replay_version,
                             identity=identity)
        return {"data": _revision_data(store, workspace_id, replay["artifact_id"], replay["version"],
                                       identity=identity),
                "status": 200}

    export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id))
    try:
        readback = _write_export_file(store, export_row, result["content"])
        _complete_export(store, export_id, workspace_id, readback)
    except Exception as exc:
        _mark_export_interrupted(store, export_id, workspace_id, "EXPORT_FAILED")
        if isinstance(exc, api.ApiError):
            raise
        raise _api_error("EXPORT_FAILED", "The Word version was saved but its DOCX file needs recovery", 500,
                         {"export_id": export_id}) from exc
    return {"data": _revision_data(store, workspace_id, artifact_id, version,
                                   changes=result["changes"], identity=identity),
            "status": 201 if initial else 200}


def _initial_revision(store, material_id: str, data: dict):
    result = revise_document_file(store, _api()._require_write(), None,
                                  {**data, "material_id": material_id}, _idempotency_key())
    return _api()._success(result["data"], result["status"])


def _followup_revision(store, artifact_id: str, data: dict):
    result = revise_document_file(store, _api()._require_write(), artifact_id,
                                  data, _idempotency_key())
    return _api()._success(result["data"], result["status"])


def revise_document_file(store, identity: dict, artifact_id: str | None,
                         data: dict, idempotency_key: str) -> dict:
    """Apply one original-backed Word revision without HTTP request state."""
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or not isinstance(identity.get("workspace", {}).get("id"), str)
            or not isinstance(identity.get("user"), dict)
            or not isinstance(identity.get("user", {}).get("id"), str)):
        raise _api_error("IDENTITY_REQUIRED", "A verified workspace identity is required", 401)
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        _ensure_editor(connection, workspace_id, identity["user"]["id"])
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise _api_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for a Word revision", 400)

    if artifact_id is None:
        material_id = data.get("material_id") if isinstance(data, dict) else None
        if not isinstance(material_id, str) or not material_id:
            raise _api_error("INVALID_INPUT", "material_id is required for a new Word revision", 400)
        operation_data = {key: value for key, value in data.items() if key != "material_id"}
        operation = _normalize_request(operation_data, initial=True)
    else:
        operation = _normalize_request(data, initial=False)
    fingerprint = _revision_fingerprint(material_id if artifact_id is None else None,
                                        artifact_id, operation)
    replay = _replay_revision(store, identity, idempotency_key, fingerprint)
    if replay is not None:
        return {"data": replay, "status": 200}

    if artifact_id is None:
        requested_work_id = operation["work_id"]
        material, base = _read_material(
            store, workspace_id, material_id, identity=identity,
            target_work_id=(requested_work_id if requested_work_id is not None
                            else _TARGET_WORK_UNSET),
            write_target=True,
        )
        target_work_id = requested_work_id or material["work_id"]
        source_reference = _material_reference(material)
        try:
            revised = _revise(base, operation["changes"], store=store, workspace_id=workspace_id,
                              identity=identity, target_work_id=target_work_id)
        except ValueError as exc:
            raise _as_document_error(exc) from exc
        return _save_revision(
            store, initial=True, material_row=material, artifact_row=None, base_version_row=None,
            source_reference=source_reference, base_reference=source_reference, operation=operation,
            result=revised, identity=identity, key=idempotency_key, fingerprint=fingerprint,
        )

    api = _api()
    from .collaboration import require_artifact_access

    with store.transaction(immediate=False) as connection:
        artifact = dict(require_artifact_access(connection, identity, artifact_id, write=True))
    if artifact["current_version"] != operation["base_version"]:
        with store.transaction(immediate=False) as connection:
            current = api._artifact_row(connection, workspace_id, artifact_id)
            raise _api_error("VERSION_CONFLICT", "Artifact has changed since it was opened", 409,
                             {"current": api._artifact_data(connection, current)})
    version_row = _version_row(store, workspace_id, artifact_id, operation["base_version"])
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        document = _snapshot_document(snapshot)
    except (TypeError, ValueError) as exc:
        raise _api_error("DOCUMENT_RECOVERY_REQUIRED", "The saved Word version record is unreadable", 409) from exc
    _base_export, base = _ensure_export_ready(
        store, workspace_id, artifact_id, version_row, identity=identity,
    )
    target_work_id = artifact["work_id"]
    try:
        revised = _revise(base, operation["changes"], store=store, workspace_id=workspace_id,
                          identity=identity, target_work_id=target_work_id)
    except ValueError as exc:
        raise _as_document_error(exc) from exc
    material_row, _source_bytes = _read_material(
        store, workspace_id, document["source"].get("material_id"),
        identity=identity, target_work_id=target_work_id,
    )
    return _save_revision(
        store, initial=False, material_row=material_row, artifact_row=artifact, base_version_row=version_row,
        source_reference=document["source"], base_reference=document["file"], operation=operation,
        result=revised, identity=identity, key=idempotency_key, fingerprint=fingerprint,
    )


def revise_document_artifact(store, identity: dict, artifact_id: str, data: dict,
                             idempotency_key: str) -> dict:
    """Pure follow-up Word use case for AI and cross-artifact RevisionBatch callers."""
    if not isinstance(artifact_id, str) or not artifact_id:
        raise _api_error("INVALID_INPUT", "artifact_id is required for a Word revision", 400)
    return revise_document_file(store, identity, artifact_id, data, idempotency_key)["data"]


def render_document_export(store, version_row, *, identity: dict | None = None) -> bytes:
    """Deliver the original-backed saved DOCX through the shared export owner."""
    identity = _access_identity(identity, version_row["workspace_id"])
    _record, content = _ensure_export_ready(
        store, version_row["workspace_id"], version_row["artifact_id"], version_row,
        identity=identity,
    )
    return content


def register_routes(app, store=None) -> None:
    """Register Word routes on the existing authenticated API application."""
    if app.extensions.get("oevira.documents_api.registered"):
        return
    store = store or app.extensions["oevira.store"]

    @app.get("/api/materials/<material_id>/document")
    def inspect_material_document(material_id: str):
        workspace_id = _workspace_id()
        material, content = _read_material(store, workspace_id, material_id)
        return _api()._success({
            "material_id": material["id"],
            "filename": material["filename"],
            "sha256": material["sha256"],
            "size_bytes": material["size_bytes"],
            **_inspect(content),
        })

    @app.get("/api/artifacts/<artifact_id>/document")
    def reopen_artifact_document(artifact_id: str):
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
        version_row = _version_row(store, workspace_id, artifact_id, version)
        _export, content = _ensure_export_ready(
            store, workspace_id, artifact_id, version_row, identity=g.identity,
        )
        with store.transaction(immediate=False) as connection:
            artifact_data = _api()._artifact_data(connection, artifact, version)
        return _api()._success({
            "artifact": artifact_data,
            "document": _document_data(store, artifact_id, version_row, content),
        })

    @app.post("/api/materials/<material_id>/document-revisions")
    def create_initial_document_revision(material_id: str):
        return _initial_revision(store, material_id, _api()._body())

    @app.post("/api/artifacts/<artifact_id>/document-revisions")
    def create_followup_document_revision(artifact_id: str):
        return _followup_revision(store, artifact_id, _api()._body())

    @app.get("/api/document-exports/<export_id>/file")
    def download_document_export(export_id: str):
        workspace_id = _workspace_id()
        export = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ? AND format = 'docx'", (export_id, workspace_id))
        if export is None:
            raise _api()._not_found()
        version_row = _version_row(store, workspace_id, export["artifact_id"], export["version"])
        _ready_export, content = _ensure_export_ready(
            store, workspace_id, export["artifact_id"], version_row, identity=g.identity,
        )
        path = _api()._resource_path(store, export["relative_path"])
        response = send_file(path, as_attachment=True, download_name=path.name, mimetype=DOCX_MIMETYPE, conditional=False)
        response.headers["Content-Length"] = str(len(content))
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.extensions.setdefault("oevira.revision_handlers", {})["document"] = revise_document_artifact
    app.extensions["oevira.documents_api.registered"] = True
