"""Durable, lease-fenced comparison worker for the first complete workflow."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from flask import jsonify, request

from .api import ApiError, _job_data, _parse_material, _request_fingerprint, _resource_path, _string, _validate_citations, _work_row
from .auth import utc_now
from .monitoring import tick_monitoring
from .collaboration import require_work_access, require_material_access, require_workspace_access

_SQLITE_MATERIAL_BATCH_SIZE = 500
_MAX_COMPARISON_SEGMENTS = 50_000
_WORKER_SCHEMA_LOCK = threading.Lock()
_EXECUTION_GROUPS = {
    "acquisition": ("acquire_url",),
    "media": ("media_render", "media_preview", "media_transcribe", "media_interchange"),
    "intelligence": ("intelligence_turn",),
}


def install_worker_schema(store) -> None:
    """Add lease liveness without replacing the Job's actual progress timestamp."""
    with _WORKER_SCHEMA_LOCK:
        with store.transaction(immediate=False) as connection:
            present = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            indexed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='jobs_workspace_active_kind'").fetchone()
            receipts = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_control_receipts'").fetchone()
        if {"lease_heartbeat_at", "control_revision", "pause_requested", "suspended_from"} <= present and indexed and receipts:
            return
        with store.transaction() as connection:
            present = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "lease_heartbeat_at" not in present:
                connection.execute("ALTER TABLE jobs ADD COLUMN lease_heartbeat_at TEXT")
            for name, declaration in (
                ("control_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("pause_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("suspended_from", "TEXT"),
            ):
                if name not in present:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            connection.execute("""CREATE TABLE IF NOT EXISTS job_control_receipts (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                operation_id TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                action TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                control_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, operation_id)
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_workspace_active_kind ON jobs(workspace_id, kind, status)")


def _clean_criteria(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ApiError("INVALID_INPUT", "criteria must contain 1 to 20 comparison strings", 400)
    return [_string(item, "criteria[]", maximum=256) for item in value]


def enqueue_comparison(store, identity: dict, work_id: str, data: dict):
    if identity["role"] not in {"owner", "editor"}:
        raise ApiError("FORBIDDEN", "Your workspace role cannot start a comparison", 403)
    if set(data) != {"material_ids", "criteria"}:
        raise ApiError("INVALID_INPUT", "Comparison requires material_ids and criteria", 400)
    material_ids = data["material_ids"]
    if (not isinstance(material_ids, list) or len(material_ids) < 2
            or any(not isinstance(item, str) or not item or len(item) > 80 for item in material_ids)
            or len(set(material_ids)) != len(material_ids)):
        raise ApiError("INVALID_INPUT", "material_ids must contain at least 2 distinct material IDs", 400)
    criteria = _clean_criteria(data["criteria"])
    workspace_id = identity["workspace"]["id"]
    key = request.headers.get("Idempotency-Key", "").strip()
    if not 1 <= len(key) <= 200:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for comparison", 400)
    job_id = str(uuid.uuid4())
    now = utc_now()
    fingerprint = _request_fingerprint({"work_id": work_id, "criteria": criteria, "material_ids": material_ids})
    with store.transaction() as connection:
        work = require_work_access(connection, identity, work_id)
        if work is None:
            raise ApiError("NOT_FOUND", "The requested resource was not found", 404)
        existing = connection.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? AND kind = 'material_comparison' AND idempotency_key = ?",
            (workspace_id, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different comparison", 409)
            return jsonify({"data": _job_data(existing)}), 200
        require_work_access(connection, identity, work_id, write=True)
        for material_id in material_ids:
            require_material_access(connection, identity, material_id, target_work_id=work_id)
        if work["status"] != "active":
            raise ApiError("WORK_ARCHIVED", "Reopen this work before creating a comparison", 409)
        by_id = {}
        for offset in range(0, len(material_ids), _SQLITE_MATERIAL_BATCH_SIZE):
            batch = material_ids[offset:offset + _SQLITE_MATERIAL_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT id, sha256, filename, format, status, reason FROM materials "
                f"WHERE workspace_id = ? AND id IN ({placeholders})",
                (workspace_id, *batch),
            ).fetchall()
            by_id.update((row["id"], row) for row in rows)
        if len(by_id) != len(material_ids):
            raise ApiError("NOT_FOUND", "One or more requested materials were not found", 404)
        materials = [
            {
                "material_id": material_id,
                "sha256": by_id[material_id]["sha256"],
                "filename": by_id[material_id]["filename"],
                "format": by_id[material_id]["format"],
                "status": by_id[material_id]["status"],
                "reason": by_id[material_id]["reason"],
            }
            for material_id in material_ids
        ]
        connection.executemany(
            "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
            [(work_id, material_id, workspace_id, now) for material_id in material_ids],
        )
        snapshot = {
            "work": {"id": work["id"], "version": work["version"], "goal": work["goal"], "brief": json.loads(work["brief_json"])},
            "criteria": criteria,
            "materials": materials,
        }
        payload = {"work_id": work_id, "criteria": criteria, "input_snapshot": snapshot}
        connection.execute(
            "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) VALUES(?, ?, ?, ?, 'material_comparison', 'queued', ?, ?, ?, ?, 'Waiting for comparison', ?, ?)",
            (job_id, workspace_id, identity["user"]["id"], work_id, json.dumps(payload, ensure_ascii=False), fingerprint, key, len(material_ids) + 1, now, now),
        )
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return jsonify({"data": _job_data(job)}), 202


def enqueue_url_acquisition(store, identity: dict, data: dict):
    result, status = create_url_acquisition_job(store, identity, data, request.headers.get("Idempotency-Key", ""))
    return jsonify({"data": result}), status


def create_url_acquisition_job(store, identity: dict, data: dict, idempotency_key: str, *, app=None) -> tuple[dict, int]:
    """Queue one acquisition through the same domain boundary, without HTTP state."""
    if set(data) - {"url", "work_id"}:
        raise ApiError("INVALID_INPUT", "URL acquisition contains an unsupported field", 400)
    url = _string(data.get("url"), "url", maximum=4096)
    if any(ord(character) < 32 for character in url):
        raise ApiError("INVALID_INPUT", "url contains a control character", 400)
    try:
        parsed_url = urlsplit(url)
        port = parsed_url.port
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", "url must be a valid public HTTP(S) URL", 400) from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or port not in {None, 80, 443}:
        raise ApiError("INVALID_INPUT", "url must be a public HTTP(S) address without credentials on a standard port", 400)
    work_id = data.get("work_id")
    if work_id is not None:
        work_id = _string(work_id, "work_id", maximum=80)
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if not 1 <= len(key) <= 200:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for URL acquisition", 400)
    workspace_id = identity["workspace"]["id"]
    payload = {"url": url, "work_id": work_id}
    fingerprint = _request_fingerprint(payload)
    job_id = str(uuid.uuid4())
    now = utc_now()
    with store.transaction() as connection:
        require_workspace_access(connection, identity, write=True)
        if identity.get("session_id"):
            session = connection.execute(
                "SELECT 1 FROM sessions WHERE id=? AND workspace_id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?",
                (identity["session_id"], workspace_id, identity["user"]["id"], now),
            ).fetchone()
            if session is None:
                raise ApiError("UNAUTHENTICATED", "Reconnect your session before acquiring a source", 401)
        if work_id:
            work = require_work_access(connection, identity, work_id, write=True)
            if work["status"] != "active":
                raise ApiError("WORK_ARCHIVED", "Reopen this work before acquiring a source for it", 409)
        existing = connection.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? AND kind = 'acquire_url' AND idempotency_key = ?",
            (workspace_id, key),
        ).fetchone()
        if existing:
            if existing["user_id"] != identity["user"]["id"]:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This operation key is already in use", 409)
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different URL request", 409)
            return _job_data(existing, app=app), 200
        connection.execute(
            "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) VALUES(?, ?, ?, ?, 'acquire_url', 'queued', ?, ?, ?, 1, 'Waiting for source read', ?, ?)",
            (job_id, workspace_id, identity["user"]["id"], work_id, json.dumps(payload, ensure_ascii=False), fingerprint, key, now, now),
        )
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_data(row, app=app), 202


def _set_job_error(
    store,
    job_id: str,
    generation: int,
    code: str,
    message: str,
    *,
    progress: tuple[int, int, str] | None = None,
    details: dict | None = None,
) -> None:
    with store.transaction() as connection:
        row = connection.execute("SELECT status, cancel_requested FROM jobs WHERE id = ? AND lease_generation = ?", (job_id, generation)).fetchone()
        if row is None or row["status"] not in {"running", "cancel_requested"}:
            return
        if details is not None and not row["cancel_requested"] and row["status"] != "cancel_requested":
            payload_row = connection.execute("SELECT payload_json FROM jobs WHERE id = ? AND lease_generation = ?", (job_id, generation)).fetchone()
            payload = json.loads(payload_row["payload_json"])
            payload["_acquisition_failure"] = {
                "http_status": details.get("http_status"),
                "retry_after": details.get("retry_after"),
            }
            connection.execute("UPDATE jobs SET payload_json = ? WHERE id = ? AND lease_generation = ?",
                               (json.dumps(payload, ensure_ascii=False), job_id, generation))
        if row["cancel_requested"] or row["status"] == "cancel_requested":
            connection.execute("UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), job_id, generation))
        elif progress is not None:
            connection.execute(
                "UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?, progress_completed = ?, progress_total = ?, progress_label = ?, updated_at = ? WHERE id = ? AND lease_generation = ?",
                (code, message, *progress, utc_now(), job_id, generation),
            )
        else:
            connection.execute(
                "UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND lease_generation = ?",
                (code, message, utc_now(), job_id, generation),
            )


def _workspace_can_write(connection, workspace_id: str, user_id: str, work_id: str | None = None) -> bool:
    member = connection.execute(
        "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if not (member and member["active"] and member["role"] in {"owner", "editor"}):
        return False
    if work_id:
        try:
            require_work_access(connection, {"workspace": {"id": workspace_id}, "user": {"id": user_id}}, work_id, write=True)
        except ApiError:
            return False
    return True


def claim_can_commit(connection, claim: dict, *, allow_stopping: bool = False) -> bool:
    """Fence every domain receipt, including errors, to its current attempt."""
    row = connection.execute("SELECT lease_generation,status FROM jobs WHERE id=? AND workspace_id=?",
                             (claim["id"], claim["workspace_id"])).fetchone()
    states = {"running", "pause_requested", "cancel_requested"} if allow_stopping else {"running"}
    return row is not None and row["lease_generation"] == claim["generation"] and row["status"] in states


def _previous_source(store, claim: dict, requested_url: str):
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    with store.transaction(immediate=False) as connection:
        rows = connection.execute(
            "SELECT * FROM materials WHERE workspace_id = ? AND source_url = ? ORDER BY created_at DESC, rowid DESC",
            (claim["workspace_id"], requested_url),
        )
        for row in rows:
            source_work_id, target_work_id = row["work_id"], claim.get("work_id")
            if source_work_id is not None and source_work_id != target_work_id:
                if target_work_id is None or connection.execute(
                    "SELECT 1 FROM work_materials WHERE workspace_id=? AND work_id=? AND material_id=?",
                    (claim["workspace_id"], target_work_id, row["id"]),
                ).fetchone() is None:
                    continue
            try:
                metadata = json.loads(row["source_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            if metadata.get("acquisition_method") != "public_http":
                continue
            try:
                require_material_access(connection, identity, row["id"], target_work_id=claim.get("work_id"))
            except ApiError:
                continue
            return row
    return None


def _acquisition_metadata(result: dict) -> dict:
    fields = (
        "url", "requested_url", "title", "mime_type", "captured_at", "published_at",
        "published_at_origin", "author", "etag", "last_modified", "acquisition_method",
        "extraction_scope", "text_sha256", "entries", "discovered_links", "next_url",
        "entries_truncated", "links_truncated", "entry_count_observed",
    )
    metadata = {field: result.get(field) for field in fields}
    metadata["status"] = result.get("status")
    metadata["reason"] = result.get("reason")
    limitations = result.get("limitations", [])
    if not isinstance(limitations, list):
        limitations = ["Source limitations were returned in an unrecognized format."]
    metadata["limitations"] = [item[:1000] for item in limitations if isinstance(item, str)][:50]
    metadata["text_projection_format"] = "text/plain"
    return metadata


def _stage_acquisition(store, claim: dict, payload: dict, stage: dict) -> bool:
    job_id = claim["id"]
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
            (job_id, claim["workspace_id"]),
        ).fetchone()
        if row is None or row["lease_generation"] != claim["generation"] or row["status"] not in {"running", "pause_requested", "cancel_requested"}:
            return False
        if row["cancel_requested"] or row["status"] == "cancel_requested":
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?",
                (utc_now(), job_id, claim["generation"]),
            )
            return False
        if not _workspace_can_write(connection, claim["workspace_id"], claim["user_id"], claim.get("work_id")):
            connection.execute(
                "UPDATE jobs SET status = 'failed', error_code = 'ACCESS_REVOKED', error_message = 'Workspace access changed before the source could be saved.', updated_at = ? WHERE id = ? AND lease_generation = ?",
                (utc_now(), job_id, claim["generation"]),
            )
            return False
        staged_payload = {**payload, "_staged_acquisition": stage}
        changed = connection.execute(
            "UPDATE jobs SET material_id = ?, payload_json = ?, progress_completed = 0, progress_total = 1, progress_label = 'Saving the original and source text', updated_at = ? WHERE id = ? AND lease_generation = ? AND status IN ('running','pause_requested')",
            (stage["material_id"], json.dumps(staged_payload, ensure_ascii=False), utc_now(), job_id, claim["generation"]),
        )
    return changed.rowcount == 1


def _write_staged_original(store, workspace_id: str, stage: dict, content: bytes) -> Path:
    relative_path = f"objects/{workspace_id}/materials/{stage['material_id']}/original"
    target = _resource_path(store, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != stage["sha256"]:
            raise ApiError("MATERIAL_FILE_INTEGRITY_ERROR", "A saved source file exists but does not match its recovery record", 409, {"material_id": stage["material_id"]})
        return target
    fd, temporary_name = tempfile.mkstemp(prefix=f".{stage['material_id']}-", suffix=".partial", dir=target.parent)
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
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != stage["sha256"]:
        raise ApiError("MATERIAL_FILE_INTEGRITY_ERROR", "The original source failed readback verification", 500, {"material_id": stage["material_id"]})
    return target


def _finish_unchanged_acquisition(store, claim: dict, previous, requested_url: str) -> None:
    path = _resource_path(store, previous["relative_path"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != previous["sha256"]:
        _set_job_error(store, claim["id"], claim["generation"], "SOURCE_FILE_INTEGRITY_ERROR", "The unchanged source points to an original file that could not be verified.")
        return
    with store.transaction() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (claim["id"], claim["workspace_id"])).fetchone()
        if job is None or job["lease_generation"] != claim["generation"] or job["status"] not in {"running", "cancel_requested"}:
            return
        if job["cancel_requested"] or job["status"] == "cancel_requested":
            connection.execute("UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), claim["id"], claim["generation"]))
            return
        if not _workspace_can_write(connection, claim["workspace_id"], claim["user_id"], claim.get("work_id")):
            connection.execute("UPDATE jobs SET status = 'failed', error_code = 'ACCESS_REVOKED', error_message = 'Workspace access changed before the source result could be confirmed.', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), claim["id"], claim["generation"]))
            return
        work = _work_row(connection, claim["workspace_id"], claim["work_id"]) if claim["work_id"] else None
        work_available = claim["work_id"] is None or (work is not None and work["status"] == "active")
        if work_available and claim["work_id"]:
            connection.execute("INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)", (claim["work_id"], previous["id"], claim["workspace_id"], utc_now()))
        status = "completed" if work_available else "partial"
        code = None if work_available else "WORK_ARCHIVED_AFTER_ACQUISITION"
        message = None if work_available else "The source was unchanged and remains available in the workspace, but the selected work was archived."
        connection.execute(
            "UPDATE jobs SET status = ?, material_id = ?, progress_completed = 1, progress_total = 1, progress_label = ?, error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND lease_generation = ?",
            (status, previous["id"], "Source unchanged; verified saved material reused", code, message, utc_now(), claim["id"], claim["generation"]),
        )


def _finish_staged_acquisition(app, claim: dict, payload: dict, stage: dict) -> None:
    store = app.extensions["oevira.store"]
    try:
        content = base64.b64decode(stage["content_b64"], validate=True)
        if len(content) > 3 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != stage["sha256"]:
            raise ValueError("The staged source bytes failed integrity verification")
        projection = stage.get("source_text")
        if not isinstance(projection, str):
            raise ValueError("The staged source text is unavailable")
        parsed = _parse_material("webpage.txt", projection.encode("utf-8"))
        metadata = stage["source"]
        source_status = metadata["status"]
        if source_status not in {"ready", "partial"}:
            raise ValueError("The staged acquisition status is invalid")
        _write_staged_original(store, claim["workspace_id"], stage, content)
        source_metadata = dict(metadata)
        limitations = list(source_metadata.get("limitations", []))
        now = utc_now()
        with store.transaction() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (claim["id"], claim["workspace_id"])).fetchone()
            if job is None or job["lease_generation"] != claim["generation"] or job["status"] not in {"running", "cancel_requested"}:
                return
            if not _workspace_can_write(connection, claim["workspace_id"], claim["user_id"], claim.get("work_id")):
                connection.execute("UPDATE jobs SET status = 'failed', error_code = 'ACCESS_REVOKED', error_message = 'Workspace access changed before the source could be saved.', updated_at = ? WHERE id = ? AND lease_generation = ?", (now, claim["id"], claim["generation"]))
                return
            work = _work_row(connection, claim["workspace_id"], claim["work_id"]) if claim["work_id"] else None
            work_available = claim["work_id"] is None or (work is not None and work["status"] == "active")
            linked_work_id = claim["work_id"] if work_available else None
            late_cancel = bool(job["cancel_requested"] or job["status"] == "cancel_requested")
            if not work_available:
                limitations.append("The selected work was archived during acquisition; the saved material remains available in the workspace without that association.")
            if late_cancel:
                limitations.append("Cancellation arrived after source bytes were durably staged; the captured source was saved so it can be recovered.")
            source_metadata["limitations"] = list(dict.fromkeys(limitations))

            parse_status = parsed["status"]
            material_status = parse_status if source_status == "ready" else "partial"
            partial = source_status == "partial" or parse_status != "ready" or not work_available or late_cancel
            reasons = [item for item in (metadata.get("reason"), parsed.get("reason")) if isinstance(item, str) and item.strip()]
            if not work_available:
                reasons.append("The selected work was archived during acquisition; this material is preserved in the workspace.")
            if late_cancel:
                reasons.append("The source response was already captured when cancellation arrived; the material was saved for recovery.")
            reason = "; ".join(dict.fromkeys(reasons))[:1000] or None
            filename = stage["filename"]
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason, text, sha256, relative_path, size_bytes, source_url, etag, last_modified, source_json, source_text, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stage["material_id"], claim["workspace_id"], claim["user_id"], linked_work_id,
                    stage["title"], filename, stage["format"], material_status, reason, parsed["text"],
                    stage["sha256"], f"objects/{claim['workspace_id']}/materials/{stage['material_id']}/original",
                    len(content), payload["url"], metadata.get("etag"), metadata.get("last_modified"),
                    json.dumps(source_metadata, ensure_ascii=False), projection, now,
                ),
            )
            connection.executemany(
                "INSERT INTO material_segments(material_id, workspace_id, id, label, text, start_offset, end_offset, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [(stage["material_id"], claim["workspace_id"], *segment) for segment in parsed["segments"]],
            )
            if linked_work_id:
                connection.execute("INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)", (linked_work_id, stage["material_id"], claim["workspace_id"], now))
            if not partial:
                error_code, error_message = None, None
                job_status = "completed"
                label = "Source and text projection saved and verified"
            else:
                error_code = "SOURCE_PARTIAL" if work_available and not late_cancel else "ACQUISITION_PARTIAL"
                error_message = reason or "The source was saved with incomplete content or association details."
                job_status = "partial"
                label = "Source saved with review limitations"
            clean_payload = {"url": payload["url"], "work_id": payload.get("work_id")}
            connection.execute(
                "UPDATE jobs SET status = ?, material_id = ?, payload_json = ?, progress_completed = 1, progress_total = 1, progress_label = ?, error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND lease_generation = ?",
                (job_status, stage["material_id"], json.dumps(clean_payload, ensure_ascii=False), label, error_code, error_message, now, claim["id"], claim["generation"]),
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, ApiError) else "SOURCE_SAVE_FAILED"
        message = exc.message if isinstance(exc, ApiError) else "The source result could not be saved; its recovery record is preserved."
        _set_job_error(store, claim["id"], claim["generation"], code, message)


def _process_url_acquisition(app, claim: dict) -> None:
    store = app.extensions["oevira.store"]
    job = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (claim["id"], claim["workspace_id"]))
    if job is None:
        return
    payload = json.loads(job["payload_json"])
    stage = payload.get("_staged_acquisition")
    if isinstance(stage, dict):
        _finish_staged_acquisition(app, claim, payload, stage)
        return
    if not _advance(store, claim["id"], claim["generation"], 0, 1, "Checking source permission and prior version"):
        return
    with store.transaction(immediate=False) as connection:
        allowed = _workspace_can_write(connection, claim["workspace_id"], claim["user_id"], claim.get("work_id"))
    if not allowed:
        _set_job_error(store, claim["id"], claim["generation"], "ACCESS_REVOKED", "Workspace access changed before the source could be read.")
        return
    if claim["work_id"]:
        work = store.row("SELECT status FROM works WHERE id = ? AND workspace_id = ?", (claim["work_id"], claim["workspace_id"]))
        if work is None or work["status"] != "active":
            _set_job_error(store, claim["id"], claim["generation"], "WORK_ARCHIVED", "The selected work was archived before the source read began.")
            return
    previous = _previous_source(store, claim, payload["url"])
    try:
        from .acquisition import AcquisitionError, acquire_url

        result = acquire_url(
            payload["url"],
            etag=previous["etag"] if previous else None,
            last_modified=previous["last_modified"] if previous else None,
        )
    except Exception as exc:
        from .acquisition import AcquisitionError

        if isinstance(exc, AcquisitionError):
            _set_job_error(store, claim["id"], claim["generation"], exc.code, str(exc),
                           details={"http_status": exc.status, "retry_after": exc.retry_after})
        else:
            _set_job_error(store, claim["id"], claim["generation"], "SOURCE_READ_FAILED", "The source could not be read; no material was created.")
        return
    if result.get("status") == "unchanged":
        if previous is None:
            _set_job_error(store, claim["id"], claim["generation"], "SOURCE_UNCHANGED_WITHOUT_BASE", "The source reported no change, but no saved material version is available.")
            return
        _finish_unchanged_acquisition(store, claim, previous, payload["url"])
        return
    if result.get("status") not in {"ready", "partial"} or not isinstance(result.get("content"), bytes) or not isinstance(result.get("text"), str):
        _set_job_error(store, claim["id"], claim["generation"], "INVALID_SOURCE_RESULT", "The source reader returned an incomplete result; no material was created.")
        return
    content = result["content"]
    projection = result["text"]
    if len(content) > 3 * 1024 * 1024:
        _set_job_error(store, claim["id"], claim["generation"], "SOURCE_TOO_LARGE", "The source exceeded the current saved-material limit.")
        return
    mime = result.get("mime_type") if isinstance(result.get("mime_type"), str) else ""
    extension = "html" if "html" in mime else "xml" if "xml" in mime or "rss" in mime or "atom" in mime else "txt"
    raw_title = result.get("title")
    title = "".join(char for char in raw_title if char.isprintable()).strip()[:200] if isinstance(raw_title, str) else ""
    title = title or "网页材料"
    metadata = _acquisition_metadata(result)
    # The selected Job payload, not reader-returned metadata, is authoritative
    # for which request entered the public HTTP acquisition pipeline.
    metadata["requested_url"] = payload["url"]
    metadata["acquisition_method"] = "public_http"
    material_id = str(uuid.uuid4())
    stage = {
        "material_id": material_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
        "title": title,
        "filename": f"网页原件.{extension}",
        "format": "html" if extension == "html" else "xml" if extension == "xml" else "text",
        "source": metadata,
        "source_text": projection,
    }
    if _stage_acquisition(store, claim, payload, stage):
        payload["_staged_acquisition"] = stage
        _finish_staged_acquisition(app, claim, payload, stage)


def _claim_job(store, *, allowed_kinds=None, excluded_kinds=()):
    install_worker_schema(store)
    now = datetime.now(timezone.utc)
    now_text = utc_now()
    stale_before = (now - timedelta(minutes=15)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'interrupted', error_code = 'WORKER_RESTARTED', error_message = 'Worker stopped before confirming completion; verify the saved result before retrying.', updated_at = ? WHERE status IN ('running', 'cancel_requested', 'pause_requested') AND COALESCE(lease_heartbeat_at, updated_at) < ?",
            (now_text, stale_before),
        )
        predicate = ""
        parameters = ()
        if allowed_kinds is not None:
            if not allowed_kinds:
                return None
            predicate = " AND kind IN (" + ",".join("?" for _ in allowed_kinds) + ")"
            parameters = tuple(allowed_kinds)
        elif excluded_kinds:
            predicate = " AND kind NOT IN (" + ",".join("?" for _ in excluded_kinds) + ")"
            parameters = tuple(excluded_kinds)
        row = connection.execute(
            "SELECT * FROM jobs AS candidate WHERE status = 'queued'" + predicate +
            " AND ((kind='workspace_restore' AND NOT EXISTS (SELECT 1 FROM jobs AS active "
            "WHERE active.workspace_id=candidate.workspace_id AND active.status IN ('running','cancel_requested','pause_requested'))) "
            "OR (kind!='workspace_restore' AND NOT EXISTS (SELECT 1 FROM jobs AS restore "
            "WHERE restore.workspace_id=candidate.workspace_id AND restore.kind='workspace_restore' "
            "AND restore.status IN ('queued','running','cancel_requested','pause_requested')))) "
            "ORDER BY created_at, rowid LIMIT 1", parameters
        ).fetchone()
        if row is None:
            return None
        generation = row["lease_generation"] + 1
        connection.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_generation = ?, progress_completed = 0, progress_label = 'Checking saved materials', error_code = NULL, error_message = NULL, updated_at = ?, lease_heartbeat_at = ? WHERE id = ? AND status = 'queued'",
            (generation, now_text, now_text, row["id"]),
        )
        return {
            "id": row["id"], "workspace_id": row["workspace_id"], "user_id": row["user_id"],
            "work_id": row["work_id"], "kind": row["kind"], "generation": generation,
        }


def _advance(store, job_id: str, generation: int, completed: int, total: int, label: str) -> bool:
    with store.transaction() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["lease_generation"] != generation or row["status"] not in {"running", "cancel_requested"}:
            return False
        if row["cancel_requested"] or row["status"] == "cancel_requested":
            connection.execute("UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), job_id, generation))
            return False
        if not _workspace_can_write(connection, row["workspace_id"], row["user_id"], row["work_id"]):
            connection.execute("UPDATE jobs SET status='failed',error_code='ACCESS_REVOKED',error_message='Workspace or client access changed.',updated_at=? WHERE id=? AND lease_generation=?", (utc_now(), job_id, generation))
            return False
        connection.execute(
            "UPDATE jobs SET progress_completed = ?, progress_total = ?, progress_label = ?, updated_at = ? WHERE id = ? AND lease_generation = ? AND status = 'running'",
            (completed, total, label, utc_now(), job_id, generation),
        )
        return True


def _selected_segment_count(store, workspace_id: str, material_ids: list[str]) -> int:
    total = 0
    with store.transaction(immediate=False) as connection:
        for offset in range(0, len(material_ids), _SQLITE_MATERIAL_BATCH_SIZE):
            batch = material_ids[offset:offset + _SQLITE_MATERIAL_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM material_segments WHERE workspace_id = ? "
                f"AND material_id IN ({placeholders})",
                (workspace_id, *batch),
            ).fetchone()
            total += row["count"]
    return total


def _process_comparison(app, claim: dict) -> None:
    store = app.extensions["oevira.store"]
    job_id = claim["id"]
    generation = claim["generation"]
    job = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, claim["workspace_id"]))
    if job is None:
        return
    payload = json.loads(job["payload_json"])
    snapshot = payload["input_snapshot"]
    material_ids = payload.get("material_ids")
    if material_ids is None:
        material_ids = [item["material_id"] for item in snapshot["materials"]]
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    try:
        with store.transaction(immediate=False) as connection:
            require_work_access(connection, identity, claim["work_id"], write=True)
            for material_id in material_ids:
                require_material_access(connection, identity, material_id, target_work_id=claim["work_id"])
    except ApiError:
        _set_job_error(store, job_id, generation, "ACCESS_REVOKED", "The comparison's client or source access changed.")
        return
    minimum_report_chars = sum(
        len(item["filename"]) + len("- **") + len("**：") + len("状态未识别")
        for item in snapshot["materials"]
    ) * len(payload["criteria"])
    if minimum_report_chars > 200000:
        _set_job_error(
            store,
            job_id,
            generation,
            "COMPARISON_RESULT_TOO_LARGE",
            f"The {len(material_ids)} selected materials across {len(payload['criteria'])} criteria require "
            f"at least {minimum_report_chars} characters for the saved per-material result, above the "
            "200000-character limit. 0 selected materials were analyzed; all selected IDs remain attached "
            "to this job and no result was saved.",
            progress=(0, len(material_ids) + 1, "Selection exceeds the saved-result size limit"),
        )
        return
    segment_count = _selected_segment_count(store, claim["workspace_id"], material_ids)
    if segment_count > _MAX_COMPARISON_SEGMENTS:
        _set_job_error(
            store,
            job_id,
            generation,
            "COMPARISON_SEGMENT_BUDGET_EXCEEDED",
            f"The {len(material_ids)} selected materials contain {segment_count} parsed segments, "
            f"above the current {_MAX_COMPARISON_SEGMENTS}-segment processing budget. "
            "0 selected materials were read; all selected IDs remain attached to this job and no result was saved.",
            progress=(0, len(material_ids) + 1, "Selection exceeds the segment-processing budget"),
        )
        return
    total = len(material_ids) + 1
    material_inputs = []
    failures = []
    verified_originals = {}
    for index, material_id in enumerate(material_ids, start=1):
        if not _advance(store, job_id, generation, index - 1, total, f"Checking material {index} of {len(material_ids)}"):
            return
        material = store.row(
            "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
            (material_id, claim["workspace_id"]),
        )
        expected = next((item for item in snapshot["materials"] if item["material_id"] == material_id), None)
        if material is None or expected is None or material["sha256"] != expected["sha256"]:
            failures.append({"material_id": material_id, "reason": "Material changed or is no longer available."})
            material_inputs.append({"material_id": material_id, "parsed": {"status": "unavailable", "reason": "Material changed or is no longer available.", "text": "", "segments": []}})
            continue
        try:
            original_path = _resource_path(store, material["relative_path"])
            if not original_path.is_file():
                integrity_reason = "The original file is missing; saved parser text was not used as a substitute."
            elif hashlib.sha256(original_path.read_bytes()).hexdigest() != material["sha256"]:
                integrity_reason = "The original file failed SHA-256 verification; saved parser text was not used as a substitute."
            else:
                integrity_reason = None
        except (ApiError, OSError):
            integrity_reason = "The original file path could not be verified; saved parser text was not used as a substitute."
        if integrity_reason:
            failures.append({"material_id": material_id, "reason": integrity_reason})
            material_inputs.append({"material_id": material_id, "parsed": {"status": "unavailable", "reason": integrity_reason, "text": "", "segments": []}})
            continue
        verified_originals[material_id] = material["sha256"]
        segments = store.rows(
            "SELECT id, label, text, start_offset, end_offset, kind FROM material_segments WHERE material_id = ? AND workspace_id = ? ORDER BY start_offset, id",
            (material_id, claim["workspace_id"]),
        )
        parsed = {
            "status": material["status"], "reason": material["reason"], "format": material["format"],
            "text": material["text"] or "",
            "segments": [
                {"id": part["id"], "label": part["label"], "text": part["text"], "start": part["start_offset"], "end": part["end_offset"], "kind": part["kind"]}
                for part in segments
            ],
        }
        if material["status"] == "partial":
            failures.append({"material_id": material_id, "reason": material["reason"] or "Material was only partially readable."})
        elif material["status"] not in {"ready", "partial"} or not parsed["text"].strip():
            failures.append({"material_id": material_id, "reason": material["reason"] or "Material has no readable text."})
        material_inputs.append({"material_id": material_id, "parsed": parsed})
    if not _advance(store, job_id, generation, len(material_ids), total, "Searching source excerpts against the selected criteria"):
        return

    try:
        from .comparison import ComparisonInputLimitError, build_comparison

        goal = snapshot["work"]["goal"]
        result = build_comparison(material_inputs, payload["criteria"], goal)
        if not isinstance(result, dict):
            raise ValueError("Comparison contract returned an invalid result")
        title = _string(result.get("title"), "comparison.title", maximum=300)
        body = result.get("body")
        citations = result.get("citations", [])
        rows = result.get("rows", [])
        limitations = result.get("limitations", [])
        if not isinstance(body, str) or not body.strip() or not isinstance(rows, list) or not isinstance(limitations, list):
            raise ValueError("Comparison result is incomplete")
        if any(not isinstance(item, str) for item in limitations):
            raise ValueError("Comparison limitations are invalid")
        limitations = list(limitations)
        if failures:
            limitations.extend(f"{item['material_id']}: {item['reason']}" for item in failures)
        deterministic_limit = "字面匹配仅用于定位原文线索，不代表语义核验，也不证明事实一致或冲突。"
        if deterministic_limit not in limitations:
            limitations.append(deterministic_limit)
        if not isinstance(citations, list):
            raise ValueError("Comparison citations are invalid")
        filenames = {item["material_id"]: item["filename"] for item in snapshot["materials"]}
        citations = []
        citation_keys = set()
        body_lines = [body, "", "## 逐项比较"]

        def add_excerpt(material_id, segment_id, quote, label, heading):
            if not isinstance(material_id, str) or material_id not in filenames or not isinstance(segment_id, str) or not isinstance(quote, str) or not quote:
                raise ValueError("Comparison excerpt is invalid")
            safe_quote = quote.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
            safe_label = str(label or "原文位置未提供").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
            body_lines.append(f"- {heading} · **{filenames[material_id]}**（{safe_label}）：“{safe_quote}” `[{material_id}:{segment_id}]`")
            marker = (material_id, segment_id, quote)
            if marker not in citation_keys:
                citation_keys.add(marker)
                citations.append({"material_id": material_id, "segment_id": segment_id, "quote": quote})

        status_labels = {
            "found": "已解析文字中找到字面线索",
            "not_found": "已解析文字中未匹配该依据",
            "partial": "材料仅部分可读",
            "partial_unassessed": "有文字但未能完整评估",
            "unavailable": "材料内容不可用",
            "read_error": "材料读取失败",
        }
        for comparison_row in rows:
            if not isinstance(comparison_row, dict) or not isinstance(comparison_row.get("criterion"), str) or not isinstance(comparison_row.get("cells"), list):
                raise ValueError("Comparison row structure is invalid")
            criterion = comparison_row["criterion"].replace("|", "\\|").replace("\r", " ").replace("\n", " ")
            body_lines.extend(["", f"### 比较依据：{criterion}"])
            for cell in comparison_row["cells"]:
                if not isinstance(cell, dict) or cell.get("material_id") not in filenames:
                    raise ValueError("Comparison cell references an unknown material")
                body_lines.append(f"- **{filenames[cell['material_id']]}**：{status_labels.get(cell.get('status'), '状态未识别')}")
                excerpts = cell.get("excerpts", [])
                if not isinstance(excerpts, list):
                    raise ValueError("Comparison excerpts are invalid")
                for excerpt in excerpts[:5]:
                    add_excerpt(cell["material_id"], excerpt.get("segment_id"), excerpt.get("quote"), excerpt.get("label"), "原文")
                match_count = cell.get("matched_segment_count", len(excerpts))
                if match_count > 5:
                    body_lines.append(f"  - 共找到 {match_count} 条线索，已展开5条；其余未展开，可调整依据后重新比较。")
            for field, heading in (("numeric_difference_candidates", "数字/日期差异待核"), ("conflict_candidates", "相反用词候选待核")):
                candidates = comparison_row.get(field, [])
                if not isinstance(candidates, list):
                    raise ValueError("Comparison candidate list is invalid")
                for candidate in candidates[:10]:
                    if not isinstance(candidate, dict):
                        raise ValueError("Comparison candidate is invalid")
                    for evidence in candidate.get("evidence", [])[:10]:
                        add_excerpt(evidence.get("material_id"), evidence.get("segment_id"), evidence.get("quote"), evidence.get("label"), heading)
        body_lines.extend(["", "## 限制与未决"])
        body_lines.extend(f"- {item}" for item in limitations)
        body = "\n".join(body_lines)
        if len(body) > 200000:
            _set_job_error(
                store,
                job_id,
                generation,
                "COMPARISON_RESULT_TOO_LARGE",
                f"All {len(material_ids)} selected materials were assessed across {len(payload['criteria'])} criteria, "
                f"but the {len(body)}-character result exceeds the 200000-character saved-result limit. "
                "No result was saved; the full selection remains attached to this job.",
                progress=(len(material_ids), total, "All selected materials assessed; result exceeds saved-result limit"),
            )
            return
    except ComparisonInputLimitError as exc:
        _set_job_error(
            store,
            job_id,
            generation,
            "COMPARISON_INPUT_TOO_LARGE",
            f"The {len(material_ids)} selected materials contain {exc.actual_chars} parsed text characters, "
            f"above the current {exc.limit_chars}-character comparison limit. "
            "No comparison result was saved; the full selection remains attached to this job.",
            progress=(len(material_ids), total, "Parsed-text budget exceeded; no result was generated"),
        )
        return
    except Exception:
        _set_job_error(store, job_id, generation, "COMPARISON_FAILED", "The comparison could not be completed; materials and saved progress are preserved.")
        return

    try:
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, claim["workspace_id"])).fetchone()
            if row is None or row["lease_generation"] != generation or row["status"] not in {"running", "cancel_requested"}:
                return
            if row["cancel_requested"] or row["status"] == "cancel_requested":
                connection.execute("UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), job_id, generation))
                return
            membership = connection.execute(
                "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (claim["workspace_id"], row["user_id"]),
            ).fetchone()
            work = _work_row(connection, claim["workspace_id"], claim["work_id"])
            if membership is None or not membership["active"] or membership["role"] not in {"owner", "editor"} or work is None:
                connection.execute("UPDATE jobs SET status = 'failed', error_code = 'ACCESS_REVOKED', error_message = 'Workspace access changed before the result could be saved.', updated_at = ? WHERE id = ? AND lease_generation = ?", (utc_now(), job_id, generation))
                return
            for material_id, expected_hash in verified_originals.items():
                require_work_access(connection, identity, claim["work_id"], write=True)
                require_material_access(connection, identity, material_id, target_work_id=claim["work_id"])
                source = connection.execute("SELECT relative_path, sha256 FROM materials WHERE id = ? AND workspace_id = ?", (material_id, claim["workspace_id"])).fetchone()
                try:
                    source_path = _resource_path(store, source["relative_path"]) if source else None
                    source_still_valid = source_path is not None and source_path.is_file() and source["sha256"] == expected_hash and hashlib.sha256(source_path.read_bytes()).hexdigest() == expected_hash
                except (ApiError, OSError):
                    source_still_valid = False
                if not source_still_valid:
                    connection.execute(
                        "UPDATE jobs SET status = 'failed', error_code = 'SOURCE_FILE_INTEGRITY_ERROR', error_message = 'A material original changed during comparison; no result was published. Retry to continue with its unavailable state.', progress_label = 'Source changed; result not published', updated_at = ? WHERE id = ? AND lease_generation = ?",
                        (utc_now(), job_id, generation),
                    )
                    return
            citations = _validate_citations(connection, claim["workspace_id"], citations)
            now = utc_now()
            artifact_id = str(uuid.uuid4())
            version_snapshot = dict(snapshot)
            connection.execute(
                "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                (artifact_id, claim["workspace_id"], claim["work_id"], row["user_id"], title, now, now),
            )
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, 'Deterministic material comparison', ?, ?, NULL)",
                (artifact_id, claim["workspace_id"], title, body, json.dumps(citations, ensure_ascii=False), json.dumps(version_snapshot, ensure_ascii=False), now, row["user_id"]),
            )
            connection.execute("INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)", (claim["work_id"], artifact_id, claim["workspace_id"], now))
            status = "partial" if failures else "completed"
            error_code = "SOME_MATERIALS_UNAVAILABLE" if failures else None
            error_message = "Some materials were not readable; see the result limitations." if failures else None
            connection.execute(
                "UPDATE jobs SET status = ?, progress_completed = ?, progress_total = ?, progress_label = ?, artifact_id = ?, error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND status = 'running' AND lease_generation = ?",
                (status, total, total, "Comparison saved and verified", artifact_id, error_code, error_message, now, job_id, generation),
            )
    except ApiError:
        _set_job_error(store, job_id, generation, "RESULT_VALIDATION_FAILED", "The comparison result did not match saved source material; no artifact was published.")
    except Exception:
        _set_job_error(store, job_id, generation, "RESULT_SAVE_FAILED", "The comparison result could not be saved; verify the job before retrying.")


def _execute_claim(app, claim: dict) -> None:
    from .job_control import acknowledge_stop

    try:
        _dispatch_claim(app, claim)
    finally:
        acknowledge_stop(app.extensions["oevira.store"], claim)


def _dispatch_claim(app, claim: dict) -> None:
    with app.app_context():
        store = app.extensions["oevira.store"]
        if claim["kind"] == "material_comparison":
            _process_comparison(app, claim)
        elif claim["kind"] == "material_research":
            from .creation_research import process_research_job
            process_research_job(app, claim)
        elif claim["kind"] == "acquire_url":
            _process_url_acquisition(app, claim)
        elif claim["kind"] == "business_analysis":
            from .business_execution import process_business_analysis_job
            process_business_analysis_job(app, claim)
        elif claim["kind"] == "business_action":
            from .business_execution_actions import process_business_action_job
            process_business_action_job(app, claim)
        elif claim["kind"] == "delivery_batch":
            from .delivery import process_delivery_job
            process_delivery_job(app, claim)
        elif claim["kind"] == "revision_batch":
            from .revisions import process_revision_job
            process_revision_job(app, claim)
        elif claim["kind"] == "community_publish":
            from .community import process_community_publish_job
            process_community_publish_job(app, claim)
        elif claim["kind"] in {"media_render", "media_preview"}:
            from .media_api import process_media_render_job
            process_media_render_job(app, claim)
        elif claim["kind"] == "media_transcribe":
            from .media_api import process_media_transcription_job
            process_media_transcription_job(app, claim)
        elif claim["kind"] == "media_interchange":
            from .media_api import process_media_interchange_job
            process_media_interchange_job(app, claim)
        elif claim["kind"] == "intelligence_turn":
            from .intelligence_api import process_intelligence_job
            process_intelligence_job(app, claim)
        elif claim["kind"] == "workspace_backup":
            app.extensions["oevira.workspace_data"].process_backup_job(app, claim)
        elif claim["kind"] == "workspace_restore":
            app.extensions["oevira.workspace_data"].process_restore_job(app, claim)
        else:
            _set_job_error(store, claim["id"], claim["generation"], "UNKNOWN_JOB_KIND", "This job type is not supported by the current worker.")



def run_once(app, *, allowed_kinds=None, excluded_kinds=()) -> bool:
    with app.app_context():
        claim = _claim_job(app.extensions["oevira.store"], allowed_kinds=allowed_kinds, excluded_kinds=excluded_kinds)
    if claim is None:
        return False
    _execute_claim(app, claim)
    return True


def _run_claim_safely(app, claim: dict) -> None:
    try:
        _execute_claim(app, claim)
    except Exception:
        app.logger.error("Job execution ended without a confirmed result")
        with app.extensions["oevira.store"].transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status='interrupted', error_code='WORKER_EXECUTION_UNCONFIRMED', "
                "error_message='Execution ended without a confirmed result; inspect saved results before retrying.', "
                "updated_at=? WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested')",
                (utc_now(), claim["id"], claim["generation"]),
            )


def run_forever(app, stop_event: threading.Event) -> None:
    """Keep scheduling and leases live while bounded domain jobs execute."""
    owner_lock = app.extensions.setdefault("oevira.worker_owner_lock", threading.Lock())
    if not owner_lock.acquire(blocking=False):
        return
    store = app.extensions["oevira.store"]
    state = app.extensions["oevira.worker_state"]
    limits = {"acquisition": 2, "media": 1, "intelligence": 1, "work": 1}
    configured = app.config.get("WORKER_EXECUTION_LIMITS", {})
    periodic_errors = set()
    periodic_stop = threading.Event()
    periodic_threads = []
    active = {}
    stop_started = None
    next_heartbeat = 0.0

    def periodic(name, interval, callback):
        while not stop_event.is_set() and not periodic_stop.is_set():
            try:
                with app.app_context():
                    callback()
                periodic_errors.discard(name)
            except Exception:
                app.logger.error("%s cycle failed", name)
                periodic_errors.add(name)
            periodic_stop.wait(interval)

    def account_maintenance():
        service = app.extensions.get("oevira.account_service")
        if service is not None:
            service.close_scheduled_account_browsers()
            service.purge_due_deletions()
            service.purge_expired_account_exports()

    try:
        if (not isinstance(configured, dict) or set(configured) - set(limits)
                or any(type(value) is not int or not 1 <= value <= 4 for value in configured.values())):
            raise ValueError("Worker execution limits must be integers from 1 to 4 for the existing job groups")
        limits.update(configured)
        if sum(limits.values()) > 8:
            raise ValueError("At most eight domain jobs can execute concurrently")
        install_worker_schema(store)
        for name, interval, callback in (
            ("Monitoring scheduler", 0.5, lambda: tick_monitoring(app)),
            ("Account maintenance", 60.0, account_maintenance),
        ):
            thread = threading.Thread(target=periodic, args=(name, interval, callback),
                                      name="oevira-" + name.lower().replace(" ", "-"), daemon=True)
            thread.start()
            periodic_threads.append(thread)
        state["status"] = "running"
        reserved_kinds = tuple(kind for kinds in _EXECUTION_GROUPS.values() for kind in kinds)
        while True:
            now = time.monotonic()
            for job_id, execution in list(active.items()):
                if execution["thread"].is_alive():
                    continue
                if execution["shutdown_cancel"]:
                    with store.transaction() as connection:
                        connection.execute(
                            "UPDATE jobs SET status='interrupted', cancel_requested=0, error_code='WORKER_STOPPED', "
                            "error_message='The application stopped during processing; the saved checkpoint is retained.', updated_at=? "
                            "WHERE id=? AND lease_generation=? AND status='cancelled'",
                            (utc_now(), job_id, execution["claim"]["generation"]),
                        )
                del active[job_id]
            if now >= next_heartbeat:
                with store.transaction() as connection:
                    connection.executemany(
                        "UPDATE jobs SET lease_heartbeat_at=? WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested','pause_requested')",
                        [(utc_now(), execution["claim"]["id"], execution["claim"]["generation"]) for execution in active.values()],
                    )
                state["last_heartbeat_at"] = utc_now()
                state["active_jobs"] = len(active)
                next_heartbeat = now + 1.0
            if stop_event.is_set():
                state["status"] = "stopping"
                if stop_started is None:
                    stop_started = now
                    periodic_stop.set()
                    with store.transaction() as connection:
                        for execution in active.values():
                            claim = execution["claim"]
                            changed = connection.execute(
                                "UPDATE jobs SET cancel_requested=1, status='cancel_requested', updated_at=? "
                                "WHERE id=? AND lease_generation=? AND status='running' AND cancel_requested=0",
                                (utc_now(), claim["id"], claim["generation"]),
                            ).rowcount
                            execution["shutdown_cancel"] = bool(changed)
                if not active:
                    break
                if now - stop_started >= 10:
                    # Fence uncooperative/unknown effects; do not put them back
                    # in the queue or claim that cancellation was acknowledged.
                    with store.transaction() as connection:
                        for execution in active.values():
                            claim = execution["claim"]
                            connection.execute(
                                "UPDATE jobs SET status='interrupted', lease_generation=lease_generation+1, "
                                "error_code='WORKER_STOP_UNCONFIRMED', error_message='Processing did not confirm shutdown; inspect its existing result before retrying.', "
                                "updated_at=? WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested','pause_requested')",
                                (utc_now(), claim["id"], claim["generation"]),
                            )
                    break
            else:
                for group, limit in limits.items():
                    count = sum(execution["group"] == group for execution in active.values())
                    while count < limit and not stop_event.is_set():
                        claim = _claim_job(store, allowed_kinds=_EXECUTION_GROUPS.get(group),
                                           excluded_kinds=reserved_kinds if group == "work" else ())
                        if claim is None:
                            break
                        thread = threading.Thread(target=_run_claim_safely, args=(app, claim),
                                                  name="oevira-job-" + group, daemon=True)
                        active[claim["id"]] = {"claim": claim, "thread": thread, "group": group, "shutdown_cancel": False}
                        thread.start()
                        count += 1
                state["status"] = "degraded" if periodic_errors else "running"
            # stop_event.wait() returns immediately after shutdown; use an
            # independent bounded wait while active jobs acknowledge it.
            if stop_event.is_set():
                threading.Event().wait(0.05)
            else:
                stop_event.wait(0.1)
    except Exception:
        state["status"] = "degraded"
        raise
    finally:
        periodic_stop.set()
        for thread in periodic_threads:
            thread.join(timeout=1)
        state["active_jobs"] = sum(execution["thread"].is_alive() for execution in active.values())
        state["status"] = ("stop_unconfirmed" if state["active_jobs"] else "stopped") if stop_event.is_set() else "degraded"
        owner_lock.release()
