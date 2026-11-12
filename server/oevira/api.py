"""Authenticated HTTP boundary for the OEVIRA local product core."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import uuid
from urllib.parse import urlsplit
import zipfile

from flask import Response, current_app, g, jsonify, make_response, request, send_file, send_from_directory

from .auth import AuthenticationError, authenticate, bearer_or_cookie_token, exchange_local_capability, utc_now


_EXPORT_MIME_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def _success(data, status: int = 200):
    return jsonify({"data": data}), status


def _error(code: str, message: str, status: int, details: dict | None = None):
    return jsonify({"error": {"code": code, "message": message, "details": details or {}}}), status


def _body() -> dict:
    if not request.is_json:
        raise ApiError("INVALID_INPUT", "A JSON request body is required", 400)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", "The JSON body must be an object", 400)
    return value


def _string(value, field: str, *, minimum: int = 1, maximum: int = 5000) -> str:
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{field} must be a string", 400, {"field": field})
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise ApiError("INVALID_INPUT", f"{field} length must be {minimum} to {maximum}", 400, {"field": field})
    return result


def _optional_string(value, field: str, *, maximum: int = 2000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{field} must be a string", 400, {"field": field})
    result = value.strip()
    if len(result) > maximum:
        raise ApiError("INVALID_INPUT", f"{field} is too long", 400, {"field": field})
    return result or None


def _raw_text(value, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{field} must be a string", 400, {"field": field})
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise ApiError("INVALID_INPUT", f"{field} is empty or too long", 400, {"field": field})
    return value


def _brief(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", "brief must be an object", 400, {"field": "brief"})
    allowed = {"audience", "deliverable", "budget", "required_facts", "forbidden_claims", "customer_requests"}
    if set(value) - allowed:
        raise ApiError("INVALID_INPUT", "brief contains an unsupported field", 400, {"fields": sorted(set(value) - allowed)})
    result = {}
    for field in ("audience", "deliverable"):
        item = _optional_string(value.get(field), f"brief.{field}", maximum=2000)
        if item is not None:
            result[field] = item
    if "budget" in value and value["budget"] is not None:
        budget = value["budget"]
        if not isinstance(budget, dict) or set(budget) - {"minimum", "maximum", "currency", "notes"}:
            raise ApiError("INVALID_INPUT", "brief.budget must contain minimum, maximum, currency or notes", 400)
        normalized_budget = {}
        for bound in ("minimum", "maximum"):
            amount = budget.get(bound)
            if amount is not None:
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, (int, float))
                    or (isinstance(amount, float) and not math.isfinite(amount))
                    or amount < 0
                ):
                    raise ApiError("INVALID_INPUT", f"brief.budget.{bound} must be a finite non-negative number", 400)
                normalized_budget[bound] = amount
        if "minimum" in normalized_budget and "maximum" in normalized_budget and normalized_budget["minimum"] > normalized_budget["maximum"]:
            raise ApiError("INVALID_INPUT", "brief.budget minimum cannot exceed maximum", 400)
        currency = _optional_string(budget.get("currency"), "brief.budget.currency", maximum=8)
        notes = _optional_string(budget.get("notes"), "brief.budget.notes", maximum=1000)
        if currency is not None:
            normalized_budget["currency"] = currency
        if notes is not None:
            normalized_budget["notes"] = notes
        result["budget"] = normalized_budget
    for field in ("required_facts", "forbidden_claims", "customer_requests"):
        if field not in value:
            continue
        items = value[field]
        if not isinstance(items, list) or len(items) > 50:
            raise ApiError("INVALID_INPUT", f"brief.{field} must be an array of at most 50 strings", 400)
        result[field] = [_string(item, f"brief.{field}[]", maximum=1000) for item in items]
    return result


def _request_fingerprint(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key() -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise ApiError("INVALID_INPUT", "Idempotency-Key must contain 1 to 200 characters", 400)
    return value


def _require_write() -> dict:
    identity = g.identity
    if identity["role"] not in {"owner", "editor"}:
        raise ApiError("FORBIDDEN", "Your workspace role cannot make this change", 403)
    return identity


def _not_found() -> ApiError:
    return ApiError("NOT_FOUND", "The requested resource was not found", 404)


def _work_row(connection: sqlite3.Connection, workspace_id: str, work_id: str):
    return connection.execute("SELECT * FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id)).fetchone()


def _work_data(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    materials = connection.execute(
        "SELECT material_id FROM work_materials WHERE work_id = ? AND workspace_id = ? ORDER BY created_at, material_id",
        (row["id"], row["workspace_id"]),
    ).fetchall()
    artifacts = connection.execute(
        "SELECT artifact_id FROM work_artifacts WHERE work_id = ? AND workspace_id = ? ORDER BY created_at, artifact_id",
        (row["id"], row["workspace_id"]),
    ).fetchall()
    commission = connection.execute("SELECT c.id,c.name,c.version,c.scope_json FROM client_commissions c JOIN work_commissions w ON w.commission_id=c.id WHERE w.work_id=? AND c.workspace_id=?", (row["id"], row["workspace_id"])).fetchone()
    return {
        "id": row["id"], "title": row["title"], "goal": row["goal"],
        "brief": json.loads(row["brief_json"]), "status": row["status"], "version": row["version"],
        "material_ids": [item["material_id"] for item in materials],
        "artifact_ids": [item["artifact_id"] for item in artifacts],
        "commission": {"id": commission["id"], "name": commission["name"], "version": commission["version"], "scope": json.loads(commission["scope_json"])} if commission else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _material_data(connection: sqlite3.Connection, row: sqlite3.Row, *, include_segments: bool = True) -> dict:
    source = json.loads(row["source_json"] or "{}")
    result = {
        "id": row["id"], "title": row["title"], "filename": row["filename"],
        "status": row["status"], "reason": row["reason"], "format": row["format"],
        "text": row["text"], "segments": [], "sha256": row["sha256"],
        "created_at": row["created_at"], "work_id": row["work_id"],
        "source": source or None, "source_text": row["source_text"],
    }
    if include_segments:
        segments = connection.execute(
            "SELECT id, label, text, start_offset, end_offset, kind FROM material_segments WHERE material_id = ? AND workspace_id = ? ORDER BY start_offset, id",
            (row["id"], row["workspace_id"]),
        ).fetchall()
        result["segments"] = [
            {"id": part["id"], "label": part["label"], "text": part["text"], "start": part["start_offset"], "end": part["end_offset"], "kind": part["kind"]}
            for part in segments
        ]
    return result


def _parse_material(filename: str, content: bytes) -> dict:
    try:
        from .material_parser import parse_material

        parsed = parse_material(filename, content)
        if not isinstance(parsed, dict):
            raise ValueError("parser contract returned a non-object")
        parse_status = _string(parsed.get("status"), "parser.status", maximum=40)
        if parse_status not in {"ready", "partial", "unavailable", "error"}:
            raise ValueError("parser status is not recognized")
        reason = _optional_string(parsed.get("reason"), "parser.reason", maximum=1000)
        output_format = _optional_string(parsed.get("format"), "parser.format", maximum=40)
        extracted_text = parsed.get("text")
        if extracted_text is not None and not isinstance(extracted_text, str):
            raise ValueError("parser text is not a string")
        extracted_text = extracted_text or ""
        segments = parsed.get("segments", [])
        if not isinstance(segments, list) or len(segments) > 20000:
            raise ValueError("parser segment list is invalid")
        normalized_segments = []
        seen_segment_ids = set()
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("parser segment is invalid")
            segment_id = _string(segment.get("id"), "segment.id", maximum=200)
            if segment_id in seen_segment_ids:
                raise ValueError("parser returned duplicate segment ids")
            seen_segment_ids.add(segment_id)
            label = _string(segment.get("label"), "segment.label", maximum=500)
            segment_text = segment.get("text")
            kind = _string(segment.get("kind"), "segment.kind", maximum=80)
            start = segment.get("start")
            end = segment.get("end")
            if not isinstance(segment_text, str) or isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
                raise ValueError("parser segment fields are invalid")
            if start < 0 or end < start or end > len(extracted_text) or extracted_text[start:end] != segment_text:
                raise ValueError("parser segment offsets do not match saved text")
            normalized_segments.append((segment_id, label, segment_text, start, end, kind))
        return {
            "status": parse_status, "reason": reason, "format": output_format,
            "text": extracted_text, "segments": normalized_segments,
        }
    except Exception:
        return {
            "status": "error", "reason": "The original is preserved, but its content could not be parsed.",
            "format": Path(filename).suffix.lower().lstrip(".") or None,
            "text": None, "segments": [],
        }


def _artifact_row(connection: sqlite3.Connection, workspace_id: str, artifact_id: str):
    return connection.execute("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (artifact_id, workspace_id)).fetchone()


def _artifact_data(connection: sqlite3.Connection, row: sqlite3.Row, version: int | None = None) -> dict:
    selected = row["current_version"] if version is None else version
    item = connection.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (row["id"], row["workspace_id"], selected),
    ).fetchone()
    if item is None:
        raise _not_found()
    history = connection.execute(
        "SELECT version, created_at, revision_note FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? ORDER BY version",
        (row["id"], row["workspace_id"]),
    ).fetchall()
    return {
        "id": row["id"], "title": item["title"], "body": item["body"], "version": item["version"],
        "versions": [{"version": part["version"], "created_at": part["created_at"], "revision_note": part["revision_note"]} for part in history],
        "citations": json.loads(item["citations_json"]),
        "input_snapshot": json.loads(item["input_snapshot_json"]),
        "revision_note": item["revision_note"], "work_id": row["work_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _validate_citations(connection: sqlite3.Connection, workspace_id: str, value) -> list[dict]:
    if not isinstance(value, list):
        raise ApiError("INVALID_INPUT", "citations must be an array of source references", 400)
    citations = []
    for index, citation in enumerate(value):
        if not isinstance(citation, dict) or set(citation) != {"material_id", "segment_id", "quote"}:
            raise ApiError("INVALID_CITATION", "Each citation needs material_id, segment_id and quote", 400, {"index": index})
        material_id = _string(citation["material_id"], "material_id", maximum=80)
        segment_id = _string(citation["segment_id"], "segment_id", maximum=200)
        quote = _raw_text(citation["quote"], "quote", maximum=10000)
        segment = connection.execute(
            "SELECT s.text FROM material_segments s JOIN materials m ON m.id = s.material_id WHERE s.id = ? AND s.material_id = ? AND s.workspace_id = ? AND m.workspace_id = ?",
            (segment_id, material_id, workspace_id, workspace_id),
        ).fetchone()
        if segment is None or quote not in segment["text"]:
            raise ApiError("INVALID_CITATION", "Citation does not match accessible saved source text", 400, {"index": index})
        citations.append({"material_id": material_id, "segment_id": segment_id, "quote": quote})
    return citations


def _input_snapshot(connection: sqlite3.Connection, workspace_id: str, work_id: str | None, citations: list[dict], prior: dict | None = None) -> dict:
    snapshot = dict(prior or {})
    if work_id:
        work = _work_row(connection, workspace_id, work_id)
        if work is None:
            raise _not_found()
        snapshot.setdefault("work", {
            "id": work["id"], "version": work["version"], "goal": work["goal"],
            "brief": json.loads(work["brief_json"]),
        })
    else:
        snapshot.setdefault("work", None)
    prior_materials = {item["id"]: item for item in snapshot.get("materials", []) if isinstance(item, dict) and "id" in item}
    for material_id in sorted({item["material_id"] for item in citations}):
        material = connection.execute(
            "SELECT id, sha256, format, status, filename FROM materials WHERE id = ? AND workspace_id = ?",
            (material_id, workspace_id),
        ).fetchone()
        if material is None:
            raise _not_found()
        prior_materials[material_id] = {
            "id": material["id"], "sha256": material["sha256"], "format": material["format"],
            "status": material["status"], "filename": material["filename"],
        }
    snapshot["materials"] = [prior_materials[key] for key in sorted(prior_materials)]
    return snapshot


def _presentation_snapshot_value(snapshot: dict) -> dict | None:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "presentation":
        return None
    presentation = snapshot.get("presentation")
    file_reference = presentation.get("file") if isinstance(presentation, dict) else None
    if (not isinstance(file_reference, dict)
            or file_reference.get("kind") != "export"
            or file_reference.get("format") != "pptx"
            or not isinstance(file_reference.get("export_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", file_reference.get("sha256", ""))
            or type(file_reference.get("size_bytes")) is not int
            or file_reference.get("size_bytes") <= 0):
        raise ApiError("PRESENTATION_FILE_UNAVAILABLE", "The saved presentation version has an incomplete PPTX file reference", 409)
    return presentation


def _resource_path(store, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ApiError("FILE_UNAVAILABLE", "Stored file is unavailable", 409)
    path = store.root.joinpath(*relative.parts)
    resolved = path.resolve()
    if not resolved.is_relative_to(store.root) or path.is_symlink():
        raise ApiError("FILE_UNAVAILABLE", "Stored file is unavailable", 409)
    return resolved


def recover_pending_materials(store) -> None:
    """Reconcile uploads interrupted between durable metadata and parser readback."""
    pending = store.rows("SELECT * FROM materials WHERE status = 'pending' ORDER BY created_at, id")
    for row in pending:
        try:
            source = json.loads(row["source_json"] or "{}")
        except (ValueError, TypeError):
            source = {}
        if isinstance(source, dict) and source.get("kind") == "controlled_browser_snapshot" and source.get("capture_origin") == "frozen_preview":
            # Browser owns this interrupted save and must retain the preview's access boundary.
            continue
        try:
            target = _resource_path(store, row["relative_path"])
            if not target.is_file():
                recovered = False
                for candidate in target.parent.glob(".material-*"):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    if hashlib.sha256(candidate.read_bytes()).hexdigest() != row["sha256"]:
                        continue
                    try:
                        os.link(candidate, target)
                    except FileExistsError:
                        pass
                    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == row["sha256"]:
                        candidate.unlink(missing_ok=True)
                        recovered = True
                        break
                if not recovered:
                    with store.transaction() as connection:
                        connection.execute(
                            "UPDATE materials SET status = 'error', reason = 'Upload was interrupted before the original could be verified; no matching staged file remains.' WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                            (row["id"], row["workspace_id"]),
                        )
                    continue
            content = target.read_bytes()
            if hashlib.sha256(content).hexdigest() != row["sha256"]:
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE materials SET status = 'error', reason = 'The saved original failed SHA-256 verification; its file was preserved for recovery.' WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                        (row["id"], row["workspace_id"]),
                    )
                continue
            parsed = _parse_material(row["filename"], content)
            with store.transaction() as connection:
                current = connection.execute(
                    "SELECT status FROM materials WHERE id = ? AND workspace_id = ?",
                    (row["id"], row["workspace_id"]),
                ).fetchone()
                if current is None or current["status"] != "pending":
                    continue
                connection.execute(
                    "UPDATE materials SET status = ?, reason = ?, format = ?, text = ? WHERE id = ? AND workspace_id = ?",
                    (parsed["status"], parsed["reason"], parsed["format"], parsed["text"], row["id"], row["workspace_id"]),
                )
                connection.executemany(
                    "INSERT INTO material_segments(material_id, workspace_id, id, label, text, start_offset, end_offset, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    [(row["id"], row["workspace_id"], *segment) for segment in parsed["segments"]],
                )
        except (OSError, ApiError):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE materials SET status = 'error', reason = 'The interrupted original could not be safely located or read during startup recovery; the recovery record was retained.' WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                    (row["id"], row["workspace_id"]),
                )


def register_routes(app) -> None:
    store = app.extensions["oevira.store"]

    @app.get("/api/status")
    def status():
        identity = store.row("SELECT user_id FROM local_identity WHERE singleton = 1")
        return _success({
            "product": "OEVIRA",
            "version": "0.1.0",
            "setup_required": identity is None,
            "runtime": {
                "api": "available",
                "worker": {
                    "status": app.extensions["oevira.worker_state"]["status"],
                    "last_heartbeat_at": app.extensions["oevira.worker_state"]["last_heartbeat_at"],
                },
            },
            "ai": {"status": "unbound"},
        })

    @app.post("/api/local-session")
    def local_session():
        data = _body()
        if set(data) - {"capability", "display_name"}:
            raise ApiError("INVALID_INPUT", "Local session contains an unsupported field", 400)
        token, identity = exchange_local_capability(store, data.get("capability"), data.get("display_name"))
        response = make_response(jsonify({"data": {"token": token, **identity}}), 200)
        response.set_cookie(
            app.config["SESSION_COOKIE_NAME"], token, httponly=True, secure=False,
            samesite="Strict", path="/", max_age=7 * 24 * 60 * 60,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/session")
    def session():
        return _success({"user": g.identity["user"], "workspace": g.identity["workspace"]})

    @app.get("/api/works")
    def list_works():
        rows = store.rows("SELECT * FROM works WHERE workspace_id = ? ORDER BY updated_at DESC", (g.identity["workspace"]["id"],))
        result = []
        with store.transaction(immediate=False) as connection:
            result = [_work_data(connection, row) for row in rows]
        return _success(result)

    @app.post("/api/works")
    def create_work():
        identity = _require_write()
        data = _body()
        if set(data) - {"title", "goal", "brief", "commission_id"}:
            raise ApiError("INVALID_INPUT", "Work contains an unsupported field", 400)
        title = _string(data.get("title"), "title", maximum=200)
        goal = _string(data.get("goal"), "goal", maximum=5000)
        brief = _brief(data.get("brief"))
        payload = {"title": title, "goal": goal, "brief": brief}
        commission_id = data.get("commission_id")
        if commission_id is not None:
            commission_id = _string(commission_id, "commission_id", maximum=80)
            payload["commission_id"] = commission_id
        fingerprint = _request_fingerprint(payload)
        key = _idempotency_key()
        now = utc_now()
        work_id = str(uuid.uuid4())
        workspace_id = identity["workspace"]["id"]
        with store.transaction() as connection:
            if commission_id:
                from .collaboration import require_commission_access
                require_commission_access(connection, identity, commission_id, write=True)
            if key:
                existing = connection.execute(
                    "SELECT * FROM idempotency_records WHERE workspace_id = ? AND operation = 'create_work' AND key = ?",
                    (workspace_id, key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != fingerprint:
                        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for different work", 409)
                    row = _work_row(connection, workspace_id, existing["resource_id"])
                    if row is not None:
                        return _success(_work_data(connection, row), 200)
                    raise ApiError("IDEMPOTENCY_CONFLICT", "The original work is unavailable", 409)
            connection.execute(
                "INSERT INTO works(id, workspace_id, owner_user_id, title, goal, brief_json, status, version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                (work_id, workspace_id, identity["user"]["id"], title, goal, json.dumps(brief, ensure_ascii=False), now, now),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id, version, title, goal, brief_json, status, created_at, created_by) VALUES(?, 1, ?, ?, ?, 'active', ?, ?)",
                (work_id, title, goal, json.dumps(brief, ensure_ascii=False), now, identity["user"]["id"]),
            )
            if commission_id:
                connection.execute("INSERT INTO work_commissions VALUES(?,?,?)", (work_id, commission_id, now))
            if key:
                connection.execute(
                    "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'create_work', ?, ?, 'work', ?, ?)",
                    (workspace_id, key, fingerprint, work_id, now),
                )
            row = _work_row(connection, workspace_id, work_id)
            result = _work_data(connection, row)
        return _success(result, 201)

    @app.get("/api/works/<work_id>")
    def get_work(work_id: str):
        with store.transaction(immediate=False) as connection:
            row = _work_row(connection, g.identity["workspace"]["id"], work_id)
            if row is None:
                raise _not_found()
            return _success(_work_data(connection, row))

    @app.patch("/api/works/<work_id>")
    def update_work(work_id: str):
        identity = _require_write()
        data = _body()
        base_version = data.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
        allowed = {"base_version", "title", "goal", "brief", "status"}
        if set(data) - allowed or len(data) == 1:
            raise ApiError("INVALID_INPUT", "Provide at least one supported work field to update", 400)
        workspace_id = identity["workspace"]["id"]
        with store.transaction() as connection:
            row = _work_row(connection, workspace_id, work_id)
            if row is None:
                raise _not_found()
            if row["version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "Work has changed since it was opened", 409, {"current": _work_data(connection, row)})
            title = _string(data["title"], "title", maximum=200) if "title" in data else row["title"]
            goal = _string(data["goal"], "goal", maximum=5000) if "goal" in data else row["goal"]
            brief = _brief(data["brief"]) if "brief" in data else json.loads(row["brief_json"])
            status = data.get("status", row["status"])
            if not isinstance(status, str) or status not in {"active", "archived"}:
                raise ApiError("INVALID_INPUT", "status must be active or archived", 400)
            now = utc_now()
            version = row["version"] + 1
            connection.execute(
                "UPDATE works SET title = ?, goal = ?, brief_json = ?, status = ?, version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (title, goal, json.dumps(brief, ensure_ascii=False), status, version, now, work_id, workspace_id, base_version),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id, version, title, goal, brief_json, status, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (work_id, version, title, goal, json.dumps(brief, ensure_ascii=False), status, now, identity["user"]["id"]),
            )
            updated = _work_row(connection, workspace_id, work_id)
            result = _work_data(connection, updated)
        return _success(result)

    @app.get("/api/materials")
    def list_materials():
        rows = store.rows("SELECT * FROM materials WHERE workspace_id = ? ORDER BY created_at DESC", (g.identity["workspace"]["id"],))
        with store.transaction(immediate=False) as connection:
            return _success([_material_data(connection, row, include_segments=False) for row in rows])

    @app.post("/api/materials")
    def create_material():
        return _create_material(store)

    @app.post("/api/materials/<material_id>/retry-parsing")
    def retry_material_parsing(material_id: str):
        return _retry_material_parsing(store, _require_write(), material_id, _body(), _idempotency_key())

    @app.post("/api/materials/url")
    def create_material_from_url():
        from .worker import enqueue_url_acquisition

        return enqueue_url_acquisition(store, g.identity, _body())

    @app.get("/api/materials/<material_id>")
    def get_material(material_id: str):
        row = store.row("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, g.identity["workspace"]["id"]))
        if row is None:
            raise _not_found()
        with store.transaction(immediate=False) as connection:
            row = connection.execute("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, g.identity["workspace"]["id"])).fetchone()
            return _success(_material_data(connection, row))

    @app.get("/api/materials/<material_id>/file")
    def download_material(material_id: str):
        row = store.row("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, g.identity["workspace"]["id"]))
        if row is None:
            raise _not_found()
        path = _resource_path(store, row["relative_path"])
        if not path.is_file():
            raise ApiError("FILE_UNAVAILABLE", "Original material is unavailable", 409)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise ApiError("FILE_INTEGRITY_ERROR", "Original material failed integrity verification", 409)
        response = send_file(path, as_attachment=True, download_name=row["filename"], mimetype="application/octet-stream", conditional=False)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/api/works/<work_id>/compare")
    def compare_work(work_id: str):
        from .worker import enqueue_comparison

        return enqueue_comparison(store, g.identity, work_id, _body())

    @app.post("/api/works/<work_id>/research")
    def research_work(work_id: str):
        from .creation_research import enqueue_research

        return enqueue_research(store, g.identity, work_id, _body())

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        row = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, g.identity["workspace"]["id"]))
        if row is None:
            raise _not_found()
        return _success(_job_data(row))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        from .job_control import control_job

        data = _body() if request.data else {}
        return _success(control_job(app, store, _require_write(), job_id, "cancel", data, legacy=True))

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        from .job_control import control_job

        data = _body() if request.data else {}
        return _success(control_job(app, store, _require_write(), job_id, "retry", data, legacy=True), 202)

    @app.post("/api/jobs/<job_id>/pause")
    def pause_job(job_id: str):
        from .job_control import control_job

        return _success(control_job(app, store, _require_write(), job_id, "pause", _body()))

    @app.post("/api/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        from .job_control import control_job

        return _success(control_job(app, store, _require_write(), job_id, "resume", _body()))

    @app.get("/api/artifacts")
    def list_artifacts():
        rows = store.rows("SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY updated_at DESC", (g.identity["workspace"]["id"],))
        visible = []
        for row in rows:
            version_row = store.row("SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?", (row["id"], row["workspace_id"], row["current_version"]))
            try:
                if version_row is not None:
                    _authorize_native_artifact(store, g.identity, version_row)
            except ApiError as error:
                if error.status in {403, 404}:
                    continue
                raise
            visible.append(row)
        with store.transaction(immediate=False) as connection:
            return _success([_artifact_data(connection, row) for row in visible])

    @app.post("/api/artifacts")
    def create_artifact():
        return _create_artifact(store, _body())

    @app.get("/api/artifacts/<artifact_id>")
    def get_artifact(artifact_id: str):
        row = _artifact_row_from_store(store, g.identity["workspace"]["id"], artifact_id)
        if row is None:
            raise _not_found()
        selected = request.args.get("version")
        if selected is not None:
            try:
                selected = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be an integer", 400) from exc
        native_version = store.row("SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                                   (artifact_id, g.identity["workspace"]["id"], selected if selected is not None else row["current_version"]))
        if native_version is not None:
            _authorize_native_artifact(store, g.identity, native_version)
        with store.transaction(immediate=False) as connection:
            row = _artifact_row(connection, g.identity["workspace"]["id"], artifact_id)
            return _success(_artifact_data(connection, row, selected))

    @app.post("/api/artifacts/<artifact_id>/versions")
    def create_artifact_version(artifact_id: str):
        return _create_artifact_version(store, artifact_id, _body())

    @app.post("/api/artifacts/<artifact_id>/restore")
    def restore_artifact(artifact_id: str):
        return _restore_artifact(store, artifact_id, _body())

    @app.post("/api/artifacts/<artifact_id>/exports")
    def export_artifact(artifact_id: str):
        return _create_export(store, artifact_id, _body())

    @app.get("/api/exports/<export_id>/file")
    def download_export(export_id: str):
        row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, g.identity["workspace"]["id"]))
        if row is None:
            raise _not_found()
        version_row = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (row["artifact_id"], row["workspace_id"], row["version"]),
        )
        if version_row is None:
            raise ApiError("EXPORT_RECOVERY_REQUIRED", "The saved result version is unavailable", 409, {"export_id": export_id})
        _authorize_native_artifact(store, g.identity, version_row)
        if row["format"] == "pptx":
            try:
                snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
            except (TypeError, ValueError) as exc:
                raise ApiError("EXPORT_RECOVERY_REQUIRED", "The saved presentation version is unreadable", 409, {"export_id": export_id}) from exc
            if isinstance(snapshot, dict) and snapshot.get("kind") == "presentation":
                return _download_presentation_export(store, row)
        path = _resource_path(store, row["relative_path"])
        if not path.is_file():
            raise ApiError("EXPORT_NOT_READY", "Export file is not available; retry the same idempotent export to recover it", 409, {"export_id": export_id})
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if row["status"] == "completed" and digest != row["sha256"]:
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE exports SET status = 'interrupted', error_code = 'FILE_INTEGRITY_ERROR', updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'completed'",
                    (utc_now(), export_id, g.identity["workspace"]["id"]),
                )
            raise ApiError("FILE_INTEGRITY_ERROR", "Export failed integrity verification", 409)
        if row["status"] != "completed":
            artifact_version = store.row(
                "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                (row["artifact_id"], row["workspace_id"], row["version"]),
            )
            if artifact_version is None:
                raise ApiError("EXPORT_RECOVERY_REQUIRED", "The saved result version is unavailable; the export was preserved", 409, {"export_id": export_id})
            from .business_execution import saved_export_view
            artifact_version = saved_export_view(store, export_id, artifact_version)
            citations = json.loads(artifact_version["citations_json"])
            with store.transaction(immediate=False) as connection:
                citation_lines = _citation_lines(connection, row["workspace_id"], citations)
            if not _validate_export_bytes(row["format"], content, artifact_version["title"], artifact_version["body"], citation_lines, store=store, version_row=artifact_version, identity=g.identity):
                raise ApiError("EXPORT_RECOVERY_REQUIRED", "The saved file could not be verified; it was preserved for recovery", 409, {"export_id": export_id})
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE exports SET status = 'completed', sha256 = ?, size_bytes = ?, error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('writing', 'interrupted')",
                    (digest, len(content), utc_now(), export_id, row["workspace_id"]),
                )
                confirmed = connection.execute("SELECT status, sha256 FROM exports WHERE id = ? AND workspace_id = ?", (export_id, row["workspace_id"])).fetchone()
                if confirmed["status"] != "completed" or confirmed["sha256"] != digest:
                    raise ApiError("EXPORT_STATE_CONFLICT", "Export recovery could not be confirmed", 409, {"export_id": export_id})
        response = send_file(
            path, as_attachment=True, download_name=path.name,
            mimetype=_EXPORT_MIME_TYPES.get(row["format"], "application/octet-stream"), conditional=False,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", defaults={"web_path": ""})
    @app.get("/<path:web_path>")
    def serve_web(web_path: str):
        if web_path.startswith("api/"):
            raise _not_found()
        web_root = app.config.get("WEB_ROOT")
        if not web_root:
            raise ApiError("WEB_NOT_CONFIGURED", "Web client is not configured", 404)
        requested = web_path or "index.html"
        return send_from_directory(web_root, requested)

    from .office_api import register_routes as register_office_routes
    from .workspace_data import register_workspace_data_routes

    register_office_routes(app, store)
    register_workspace_data_routes(app, store)


def _artifact_row_from_store(store, workspace_id: str, artifact_id: str):
    return store.row("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (artifact_id, workspace_id))


def _job_data(row: sqlite3.Row, *, app=None) -> dict:
    from .job_control import available_controls

    app = app if app is not None else current_app
    error = None if row["error_code"] is None else {"code": row["error_code"], "message": row["error_message"] or "Job failed"}
    controls = [] if row["error_code"] == "WORKSPACE_RESTORE_HISTORY_ONLY" else available_controls(
        row, resume_kinds=app.extensions.get("oevira.job_resume_validators", {}),
        cancelled_retry_kinds=app.extensions.get("oevira.job_cancelled_retry_kinds", ()),
    )
    fields = row.keys()
    result_refs = []
    if row["artifact_id"]:
        result_refs.append({"kind": "artifact", "id": row["artifact_id"]})
    if row["kind"] in {"delivery_batch", "revision_batch"}:
        payload = json.loads(row["payload_json"])
        result_refs.append({"kind": row["kind"], "id": payload["batch_id"]})
    if row["kind"] == "community_publish":
        payload = json.loads(row["payload_json"])
        publication_result = payload.get("_publication_result")
        if row["status"] == "completed" and isinstance(publication_result, dict) and isinstance(publication_result.get("release_id"), str) and publication_result["release_id"]:
            result_refs.append({"kind": "release", "id": publication_result["release_id"], "delivery": publication_result.get("delivery")})
    return {
        "id": row["id"], "kind": row["kind"], "status": row["status"],
        "progress": {"completed": row["progress_completed"], "total": row["progress_total"], "label": row["progress_label"]},
        "artifact_id": row["artifact_id"], "material_id": row["material_id"], "error": error, "work_id": row["work_id"],
        "retryable": "retry" in controls,
        "control_revision": row["control_revision"] if "control_revision" in fields else 0,
        "pause_requested": bool(row["pause_requested"]) if "pause_requested" in fields else False,
        "suspended_from": row["suspended_from"] if "suspended_from" in fields else None,
        "available_controls": controls,
        "result_refs": result_refs,
    }


def _create_material(store):
    identity = _require_write()
    if request.mimetype == "multipart/form-data":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise ApiError("INVALID_INPUT", "A file field is required", 400)
        content = upload.stream.read(current_app.config["MAX_MATERIAL_BYTES"] + 1)
        if len(content) > current_app.config["MAX_MATERIAL_BYTES"]:
            raise ApiError("MATERIAL_TOO_LARGE", "Material exceeds the current upload limit", 413)
        title_value = request.form.get("title") or Path(upload.filename.replace("\\", "/")).name
        filename_value = upload.filename
        work_id = request.form.get("work_id") or None
    else:
        data = _body()
        if set(data) - {"title", "text", "filename", "work_id"}:
            raise ApiError("INVALID_INPUT", "Material contains an unsupported field", 400)
        text = _raw_text(data.get("text"), "text", maximum=current_app.config["MAX_TEXT_MATERIAL_CHARS"])
        title_value = data.get("title")
        filename_value = _string(data.get("filename", f"{title_value or 'material'}.txt"), "filename", maximum=255)
        content = text.encode("utf-8")
        work_id = data.get("work_id")
    title = _string(title_value, "title", maximum=200)
    filename = Path(filename_value.replace("\\", "/")).name
    filename = "".join(character for character in filename if character.isprintable())[:255]
    if not filename or filename in {".", ".."}:
        raise ApiError("INVALID_INPUT", "filename is invalid", 400)
    if not content:
        raise ApiError("INVALID_INPUT", "Material must not be empty", 400)
    workspace_id = identity["workspace"]["id"]
    if work_id is not None and not isinstance(work_id, str):
        raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
    if work_id:
        work = store.row("SELECT id, status FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id))
        if work is None:
            raise _not_found()
        if work["status"] != "active":
            raise ApiError("WORK_ARCHIVED", "Reopen this work before adding materials", 409)

    material_id = str(uuid.uuid4())
    now = utc_now()
    digest = hashlib.sha256(content).hexdigest()
    relative_path = f"objects/{workspace_id}/materials/{material_id}/original"
    target = _resource_path(store, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".material-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise ApiError("MATERIAL_STORAGE_FAILED", "Original material could not be stored", 500, {"material_id": material_id}) from exc

    try:
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, status, reason, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', 'Material is being processed', ?, ?, ?, ?)",
                (material_id, workspace_id, identity["user"]["id"], work_id, title, filename, digest, relative_path, len(content), now),
            )
            if work_id:
                connection.execute(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    (work_id, material_id, workspace_id, now),
                )
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    try:
        os.replace(temporary_name, target)
    except OSError as exc:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET status = 'error', reason = 'Original file storage failed' WHERE id = ? AND workspace_id = ?",
                (material_id, workspace_id),
            )
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise ApiError("MATERIAL_STORAGE_FAILED", "Original material could not be stored", 500, {"material_id": material_id}) from exc

    parsed = _parse_material(filename, content)
    parse_status = parsed["status"]
    reason = parsed["reason"]
    output_format = parsed["format"]
    extracted_text = parsed["text"]
    normalized_segments = parsed["segments"]

    with store.transaction() as connection:
        connection.execute(
            "UPDATE materials SET status = ?, reason = ?, format = ?, text = ? WHERE id = ? AND workspace_id = ?",
            (parse_status, reason, output_format, extracted_text, material_id, workspace_id),
        )
        connection.executemany(
            "INSERT INTO material_segments(material_id, workspace_id, id, label, text, start_offset, end_offset, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [(material_id, workspace_id, *segment) for segment in normalized_segments],
        )
        row = connection.execute("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, workspace_id)).fetchone()
        return _success(_material_data(connection, row), 201)


def _retry_material_parsing(store, identity, material_id, data, key):
    from .collaboration import require_material_access, require_workspace_access
    if set(data) != {"source_sha256"}:
        raise ApiError("INVALID_INPUT", "Retry reading the exact retained original", 400)
    expected = _string(data["source_sha256"], "source_sha256", maximum=64)
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required when retrying a saved source", 400)
    workspace_id = identity["workspace"]["id"]
    fingerprint = _request_fingerprint({"material_id": material_id, **data})
    operation = "material_retry_parsing"
    with store.transaction(immediate=False) as connection:
        require_workspace_access(connection, identity, write=True)
        original = require_material_access(connection, identity, material_id)
        if original["sha256"] != expected:
            raise ApiError("SOURCE_CHANGED", "The selected original no longer matches its saved hash", 409)
        if _idempotent_resource(connection, workspace_id, operation, key, fingerprint):
            return _success(_material_data(connection, original))
        if original["status"] == "pending":
            raise ApiError("MATERIAL_NOT_READY", "The original upload is still being completed", 409)
        has_segments = connection.execute("SELECT 1 FROM material_segments WHERE material_id=? LIMIT 1", (material_id,)).fetchone()
        if original["text"] or has_segments:
            # Existing anchors are immutable. This retry only finishes a read
            # that never produced a text projection, not an existing revision.
            return _success(_material_data(connection, original))
        path = _resource_path(store, original["relative_path"])
        if not path.is_file():
            raise ApiError("FILE_UNAVAILABLE", "The retained original is unavailable", 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ApiError("FILE_INTEGRITY_ERROR", "The retained original failed integrity verification", 409)
        filename = original["filename"]
    parsed = _parse_material(filename, content)
    with store.transaction() as connection:
        require_workspace_access(connection, identity, write=True)
        current = require_material_access(connection, identity, material_id)
        if _idempotent_resource(connection, workspace_id, operation, key, fingerprint):
            return _success(_material_data(connection, current))
        if current["sha256"] != expected or current["relative_path"] != original["relative_path"]:
            raise ApiError("SOURCE_CHANGED", "The original changed while its text was being read", 409)
        has_segments = connection.execute("SELECT 1 FROM material_segments WHERE material_id=? LIMIT 1", (material_id,)).fetchone()
        if not current["text"] and not has_segments:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ApiError("FILE_INTEGRITY_ERROR", "The original changed during reading", 409)
            connection.execute("UPDATE materials SET status=?,reason=?,format=?,text=? WHERE id=? AND workspace_id=?",
                               (parsed["status"], parsed["reason"], parsed["format"], parsed["text"], material_id, workspace_id))
            connection.executemany("INSERT INTO material_segments(material_id,workspace_id,id,label,text,start_offset,end_offset,kind) VALUES(?,?,?,?,?,?,?,?)",
                                   [(material_id, workspace_id, *segment) for segment in parsed["segments"]])
        connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)", (workspace_id, operation, key, fingerprint, "material", material_id, utc_now()))
        saved = connection.execute("SELECT * FROM materials WHERE id=? AND workspace_id=?", (material_id, workspace_id)).fetchone()
        return _success(_material_data(connection, saved))


def _idempotent_resource(connection, workspace_id: str, operation: str, key: str | None, fingerprint: str):
    if not key:
        return None
    existing = connection.execute(
        "SELECT * FROM idempotency_records WHERE workspace_id = ? AND operation = ? AND key = ?",
        (workspace_id, operation, key),
    ).fetchone()
    if existing is None:
        return None
    if existing["request_hash"] != fingerprint:
        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for different input", 409)
    return existing


def _create_artifact(store, data):
    identity = _require_write()
    allowed = {"title", "body", "citations", "work_id"}
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Artifact contains an unsupported field", 400)
    title = _string(data.get("title"), "title", maximum=300)
    body = _raw_text(data.get("body"), "body", maximum=200000, allow_empty=True)
    work_id = data.get("work_id")
    if work_id is not None and not isinstance(work_id, str):
        raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
    workspace_id = identity["workspace"]["id"]
    citations = []
    if "citations" not in data or not isinstance(data["citations"], list):
        raise ApiError("INVALID_INPUT", "citations must be an array", 400)
    fingerprint = _request_fingerprint({"title": title, "body": body, "citations": data.get("citations", []), "work_id": work_id})
    key = _idempotency_key()
    artifact_id = str(uuid.uuid4())
    now = utc_now()
    with store.transaction() as connection:
        if work_id:
            work = _work_row(connection, workspace_id, work_id)
            if work is None:
                raise _not_found()
            if work["status"] != "active":
                raise ApiError("WORK_ARCHIVED", "Reopen this work before adding results", 409)
        citations = _validate_citations(connection, workspace_id, data.get("citations", []))
        existing = _idempotent_resource(connection, workspace_id, "create_artifact", key, fingerprint)
        if existing:
            row = _artifact_row(connection, workspace_id, existing["resource_id"])
            if row is not None:
                return _success(_artifact_data(connection, row), 200)
            raise ApiError("IDEMPOTENCY_CONFLICT", "The original artifact is unavailable", 409)
        snapshot = _input_snapshot(connection, workspace_id, work_id, citations)
        connection.execute(
            "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
            (artifact_id, workspace_id, work_id, identity["user"]["id"], title, now, now),
        )
        connection.execute(
            "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, '', ?, ?, NULL)",
            (artifact_id, workspace_id, title, body, json.dumps(citations, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), now, identity["user"]["id"]),
        )
        if work_id:
            connection.execute("INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)", (work_id, artifact_id, workspace_id, now))
        if key:
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'create_artifact', ?, ?, 'artifact', ?, ?)",
                (workspace_id, key, fingerprint, artifact_id, now),
            )
        row = _artifact_row(connection, workspace_id, artifact_id)
        result = _artifact_data(connection, row)
    return _success(result, 201)


def _create_artifact_version(store, artifact_id: str, data: dict):
    return _success(revise_artifact(store, _require_write(), artifact_id, data, _idempotency_key()))


def revise_artifact(store, identity: dict, artifact_id: str, data: dict, idempotency_key: str | None = None) -> dict:
    from .collaboration import require_artifact_access

    if not isinstance(data, dict):
        raise ApiError("INVALID_INPUT", "Artifact version must be an object", 400)
    allowed = {"base_version", "title", "body", "citations", "revision_note"}
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Artifact version contains an unsupported field", 400)
    if "citations" not in data:
        raise ApiError("INVALID_INPUT", "citations must be provided so source references are preserved", 400)
    base_version = data.get("base_version")
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    title = _string(data.get("title"), "title", maximum=300)
    body = _raw_text(data.get("body"), "body", maximum=200000, allow_empty=True)
    revision_note = _optional_string(data.get("revision_note"), "revision_note", maximum=2000) or ""
    workspace_id = identity["workspace"]["id"]
    key = idempotency_key
    operation = f"revise_artifact:{artifact_id}"
    fingerprint = _request_fingerprint(data)
    with store.transaction() as connection:
        row = require_artifact_access(connection, identity, artifact_id, write=True)
        existing = _idempotent_resource(connection, workspace_id, operation, key, fingerprint)
        if existing:
            return _artifact_data(connection, row, int(existing["resource_id"]))
        if row["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Artifact has changed since it was opened", 409, {"current": _artifact_data(connection, row)})
        current = connection.execute(
            "SELECT input_snapshot_json FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, base_version),
        ).fetchone()
        prior_snapshot = json.loads(current["input_snapshot_json"] or "{}")
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "business_analysis":
            raise ApiError("BUSINESS_REVISION_REQUIRED", "Revise the selected business sections through their calculation and review owner", 409)
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "document":
            raise ApiError("DOCUMENT_REVISION_REQUIRED", "Word documents must be changed through a document revision", 409)
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "spreadsheet":
            raise ApiError("SPREADSHEET_REVISION_REQUIRED", "Revise the selected workbook cells to preserve its formulas and structure", 409)
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "pdf_document":
            raise ApiError("PDF_REVISION_REQUIRED", "Revise this PDF through its exact page and source anchors", 409)
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "research":
            raise ApiError("RESEARCH_REVISION_REQUIRED", "Revise the selected research propositions and their evidence", 409)
        if isinstance(prior_snapshot, dict) and prior_snapshot.get("kind") == "community_native":
            raise ApiError("COMMUNITY_REVISION_REQUIRED", "Revise this native work through its publication draft", 409)
        citations = _validate_citations(connection, workspace_id, data["citations"])
        prior_presentation = _presentation_snapshot_value(prior_snapshot)
        snapshot = _input_snapshot(connection, workspace_id, row["work_id"], citations, prior_snapshot)
        if prior_presentation is not None and (
                snapshot.get("kind") != "presentation"
                or snapshot.get("presentation") != prior_presentation):
            raise ApiError("PRESENTATION_SNAPSHOT_CONFLICT", "Text revisions must preserve the saved PPTX version reference", 409)
        now = utc_now()
        version = base_version + 1
        connection.execute(
            "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, workspace_id, version, title, body, json.dumps(citations, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), revision_note, now, identity["user"]["id"], base_version),
        )
        connection.execute("UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?", (title, version, now, artifact_id, workspace_id, base_version))
        updated = _artifact_row(connection, workspace_id, artifact_id)
        result = _artifact_data(connection, updated)
        if key:
            connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
                               (workspace_id, operation, key, fingerprint, "artifact_version", str(version), now))
    return result


def _restore_artifact(store, artifact_id: str, data: dict):
    from .collaboration import require_artifact_access

    identity = _require_write()
    if set(data) - {"base_version", "restore_version", "revision_note"}:
        raise ApiError("INVALID_INPUT", "Artifact restore contains an unsupported field", 400)
    base_version = data.get("base_version")
    restore_version = data.get("restore_version")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (base_version, restore_version)):
        raise ApiError("INVALID_INPUT", "base_version and restore_version must be positive integers", 400)
    revision_note = _optional_string(data.get("revision_note"), "revision_note", maximum=2000)
    workspace_id = identity["workspace"]["id"]
    key = _idempotency_key()
    operation = f"restore_artifact:{artifact_id}"
    fingerprint = _request_fingerprint(data)
    version = base_version + 1
    with store.transaction(immediate=False) as connection:
        row = require_artifact_access(connection, identity, artifact_id, write=True)
        existing = _idempotent_resource(connection, workspace_id, operation, key, fingerprint)
        if existing:
            return _success(_artifact_data(connection, row, int(existing["resource_id"])))
        if row["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Artifact has changed since it was opened", 409, {"current": _artifact_data(connection, row)})
        source = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, restore_version),
        ).fetchone()
        if source is None:
            raise _not_found()
        source_snapshot = json.loads(source["input_snapshot_json"] or "{}")
        if isinstance(source_snapshot, dict) and source_snapshot.get("kind") == "community_native":
            raise ApiError("COMMUNITY_REVISION_REQUIRED", "Restore this native work through its publication draft", 409)
        _presentation_snapshot_value(source_snapshot)
        source_snapshot_json = source["input_snapshot_json"] or "{}"

    restored_file = None
    restore_format = "docx"
    persist_restored_export = None
    if isinstance(source_snapshot, dict) and source_snapshot.get("kind") == "document":
        from .documents_api import prepare_restored_document_export, persist_restored_document_export

        source_version = store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, restore_version),
        )
        if source_version is None:
            raise _not_found()
        restored_file = prepare_restored_document_export(
            store, workspace_id, artifact_id, source_version, version, identity=identity,
        )
        persist_restored_export = persist_restored_document_export
    elif isinstance(source_snapshot, dict) and source_snapshot.get("kind") == "spreadsheet":
        from .office_api import prepare_restored_spreadsheet_export, persist_restored_spreadsheet_export

        restored_file = prepare_restored_spreadsheet_export(
            store, workspace_id, artifact_id, source, version, identity=identity,
        )
        restore_format = "xlsx"
        persist_restored_export = persist_restored_spreadsheet_export
    elif isinstance(source_snapshot, dict) and source_snapshot.get("kind") == "pdf_document":
        from .office_api import prepare_restored_pdf_export, persist_restored_pdf_export

        restored_file = prepare_restored_pdf_export(store, workspace_id, artifact_id, source, version, identity=identity)
        restore_format = "pdf"
        persist_restored_export = persist_restored_pdf_export

    now = utc_now()
    note = revision_note or f"Restored from version {restore_version}"
    restored_snapshot_json = (
        json.dumps(restored_file["snapshot"], ensure_ascii=False, separators=(",", ":"))
        if restored_file is not None else source_snapshot_json
    )
    with store.transaction() as connection:
        row = require_artifact_access(connection, identity, artifact_id, write=True)
        existing = _idempotent_resource(connection, workspace_id, operation, key, fingerprint)
        if existing:
            return _success(_artifact_data(connection, row, int(existing["resource_id"])))
        if row["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Artifact has changed since it was opened", 409, {"current": _artifact_data(connection, row)})
        source = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, restore_version),
        ).fetchone()
        if source is None:
            raise _not_found()
        if (source["input_snapshot_json"] or "{}") != source_snapshot_json:
            raise ApiError("ARTIFACT_VERSION_CHANGED", "The selected version changed while it was being restored", 409)
        connection.execute(
            "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version, restore_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, workspace_id, version, source["title"], source["body"], source["citations_json"], restored_snapshot_json, note, now, identity["user"]["id"], base_version, restore_version),
        )
        if restored_file is not None:
            export = restored_file["export"]
            connection.execute(
                "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'writing', ?, ?, ?)",
                (export["id"], workspace_id, artifact_id, version, restore_format, export["idempotency_key"],
                 export["request_hash"], export["relative_path"], export["created_at"], export["updated_at"]),
            )
        connection.execute("UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?", (source["title"], version, now, artifact_id, workspace_id, base_version))
        updated = _artifact_row(connection, workspace_id, artifact_id)
        result = _artifact_data(connection, updated)
        if key:
            connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
                               (workspace_id, operation, key, fingerprint, "artifact_version", str(version), now))
    if restored_file is not None:
        persist_restored_export(store, restored_file, identity=identity)
    return _success(result)


def _citation_lines(connection: sqlite3.Connection, workspace_id: str, citations: list[dict]) -> list[str]:
    lines = []
    for citation in citations:
        row = connection.execute(
            "SELECT m.filename, s.label FROM materials m JOIN material_segments s ON s.material_id = m.id WHERE m.id = ? AND m.workspace_id = ? AND s.id = ? AND s.workspace_id = ?",
            (citation["material_id"], workspace_id, citation["segment_id"], workspace_id),
        ).fetchone()
        if row is None:
            raise ApiError("INVALID_CITATION", "Citation source is no longer available", 409)
        lines.append(f"- {row['filename']} — {row['label']}: “{citation['quote']}”")
    return lines


def _authorize_native_artifact(store, identity: dict, version_row) -> None:
    try:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
    except (TypeError, ValueError) as error:
        raise ApiError("ARTIFACT_SNAPSHOT_UNREADABLE", "The saved result context is unreadable", 409) from error
    kind = snapshot.get("kind") if isinstance(snapshot, dict) else None
    if kind == "document":
        from .documents_api import authorize_document_artifact

        authorize_document_artifact(store, identity, version_row)
    elif isinstance(kind, str) and kind in {"presentation", "spreadsheet", "pdf_document"}:
        from .office_api import authorize_office_artifact

        authorize_office_artifact(store, identity, version_row)


def _render_export(
    format_name: str, title: str, body: str, citations: list[str], *,
    store=None, version_row: sqlite3.Row | dict | None = None, identity: dict | None = None,
) -> bytes:
    snapshot = {}
    if version_row is not None:
        try:
            snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise ApiError("ARTIFACT_SNAPSHOT_UNREADABLE", "The saved result context is unreadable", 409) from exc
        kind = snapshot.get("kind") if isinstance(snapshot, dict) else None
        if kind == "document" and format_name in {"docx", "html"}:
            from .documents_api import render_document_export
            content = render_document_export(store, version_row, identity=identity)
            if format_name == "docx":
                return content
            from .documents import inspect_document
            body = "\n\n".join(item["text"] for item in inspect_document(content)["objects"])
        elif kind == "document" and format_name in {"pptx", "xlsx", "pdf"}:
            raise ApiError(
                "UNSUPPORTED_EXPORT_FORMAT",
                "This original Word Artifact has no confirmed structure for that output format",
                415,
            )
        elif kind == "presentation" and format_name != "pptx":
            raise ApiError(
                "UNSUPPORTED_EXPORT_FORMAT",
                "This original presentation is available as PPTX until a supported conversion is selected",
                415,
            )
        elif kind == "spreadsheet" and format_name != "xlsx":
            raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "This workbook requires a supported conversion for that format", 415)
        elif kind == "pdf_document" and format_name != "pdf":
            raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "This PDF requires a supported conversion for that format", 415)
    if format_name == "html":
        from .html_delivery import render_html
        return render_html(title, body, citations)
    if format_name == "md":
        suffix = "\n\n## Sources\n" + "\n".join(citations) if citations else ""
        return f"# {title}\n\n{body}{suffix}\n".encode("utf-8")
    if format_name == "pptx":
        if isinstance(snapshot, dict) and snapshot.get("kind") == "presentation":
            from .office_api import render_presentation_export

            return render_presentation_export(store, version_row, identity=identity)
        from .document_generation import GenerationError, render_pptx

        try:
            return render_pptx(title, body, citations, snapshot)
        except GenerationError as exc:
            raise ApiError(exc.code, exc.message, 422) from exc
    if format_name == "xlsx":
        if isinstance(snapshot, dict) and snapshot.get("kind") == "spreadsheet":
            from .office_api import render_spreadsheet_export

            return render_spreadsheet_export(store, version_row, identity=identity)
        from .document_generation import GenerationError, render_xlsx

        try:
            return render_xlsx(title, body, citations, snapshot)
        except GenerationError as exc:
            raise ApiError(exc.code, exc.message, 422) from exc
    if format_name == "pdf":
        if isinstance(snapshot, dict) and snapshot.get("kind") == "pdf_document":
            from .office_api import render_pdf_revision_export

            return render_pdf_revision_export(store, version_row, identity=identity)
        from .document_generation import GenerationError, render_pdf

        try:
            return render_pdf(title, body, citations, snapshot)
        except GenerationError as exc:
            raise ApiError(exc.code, exc.message, 422) from exc
    if format_name == "docx":
        from .document_generation import GenerationError, render_docx

        try:
            return render_docx(title, body, citations)
        except GenerationError as exc:
            raise ApiError(exc.code, exc.message, 422) from exc
    raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "The requested output format is not available", 415)


def _validate_export_bytes(
    format_name: str, content: bytes, title: str, body: str, citations: list[str], *,
    version_row: sqlite3.Row | dict | None = None, store=None, identity: dict | None = None,
) -> bool:
    if version_row is not None:
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}")
        if snapshot.get("kind") == "document" and format_name == "docx":
            return hashlib.sha256(content).hexdigest() == snapshot["document"]["file"]["sha256"]
    if format_name == "html":
        return content == _render_export("html", title, body, citations, store=store, version_row=version_row, identity=identity)
    if format_name == "md":
        return content == _render_export("md", title, body, citations)
    if format_name == "pptx":
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}") if version_row is not None else {}
        if isinstance(snapshot, dict) and snapshot.get("kind") == "presentation":
            from .office_api import validate_presentation_export

            return validate_presentation_export(content, version_row)
        from .document_generation import validate_pptx

        return validate_pptx(content, title, body, citations, snapshot)
    if format_name == "xlsx":
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}") if version_row is not None else {}
        if isinstance(snapshot, dict) and snapshot.get("kind") == "spreadsheet":
            from .office_api import validate_spreadsheet_export

            return validate_spreadsheet_export(content, version_row)
        from .document_generation import validate_xlsx

        return validate_xlsx(content, title, body, citations, snapshot)
    if format_name == "pdf":
        snapshot = json.loads(version_row["input_snapshot_json"] or "{}") if version_row is not None else {}
        if isinstance(snapshot, dict) and snapshot.get("kind") == "pdf_document":
            from .office_api import validate_pdf_revision_export

            return validate_pdf_revision_export(content, version_row)
        from .document_generation import validate_pdf

        return validate_pdf(content, title, body, citations)
    if format_name == "docx":
        from .document_generation import validate_docx

        return validate_docx(content, title, body, citations)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None:
                return False
        from docx import Document

        document = Document(io.BytesIO(content))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        return title in paragraphs and all(part in "\n".join(paragraphs) for part in body.splitlines() if part) and all(citation[2:] in "\n".join(paragraphs) for citation in citations)
    except Exception:
        return False


def _export_data(row: sqlite3.Row, audience: str = "internal", title: str | None = None) -> dict:
    return {
        "id": row["id"], "artifact_id": row["artifact_id"], "version": row["version"],
        "format": row["format"], "audience": audience, "title": title, "status": row["status"], "sha256": row["sha256"],
        "size_bytes": row["size_bytes"], "download_url": f"/api/exports/{row['id']}/file",
        "filename": PurePosixPath(row["relative_path"]).name,
        "mime_type": _EXPORT_MIME_TYPES.get(row["format"], "application/octet-stream"),
        "created_at": row["created_at"],
    }


def _write_export_bytes(store, export_row: sqlite3.Row | dict, content: bytes) -> tuple[Path, bytes]:
    """Write a derived export without replacing or blessing an unknown existing file."""
    if not isinstance(content, bytes):
        raise ApiError("EXPORT_READBACK_FAILED", "Export renderer did not return bytes", 500)
    target = _resource_path(store, export_row["relative_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_digest = hashlib.sha256(content).hexdigest()
    if target.exists():
        if not target.is_file() or target.stat().st_size != len(content):
            raise ApiError("FILE_INTEGRITY_ERROR", "An existing export file was preserved because it does not match the saved result", 409, {"export_id": export_row["id"]})
        readback = target.read_bytes()
        if hashlib.sha256(readback).hexdigest() != expected_digest:
            raise ApiError("FILE_INTEGRITY_ERROR", "An existing export file was preserved because it does not match the saved result", 409, {"export_id": export_row["id"]})
        return target, readback

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

    checked_target = _resource_path(store, export_row["relative_path"])
    if not checked_target.is_file() or checked_target.stat().st_size != len(content):
        raise ApiError("EXPORT_READBACK_FAILED", "Export file did not pass size readback", 500, {"export_id": export_row["id"]})
    readback = checked_target.read_bytes()
    if hashlib.sha256(readback).hexdigest() != expected_digest:
        raise ApiError("EXPORT_READBACK_FAILED", "Export file did not pass hash readback", 500, {"export_id": export_row["id"]})
    return checked_target, readback


def _complete_export_record(store, export_id: str, workspace_id: str, content: bytes) -> sqlite3.Row:
    digest = hashlib.sha256(content).hexdigest()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'completed', sha256 = ?, size_bytes = ?, error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('writing', 'interrupted', 'completed')",
            (digest, len(content), utc_now(), export_id, workspace_id),
        )
        row = connection.execute(
            "SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id),
        ).fetchone()
        if row is None or row["status"] != "completed" or row["sha256"] != digest or row["size_bytes"] != len(content):
            raise ApiError("EXPORT_STATE_CONFLICT", "Export completion could not be confirmed", 409, {"export_id": export_id})
        return row


def _mark_export_interrupted(store, export_id: str, workspace_id: str, code: str) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'interrupted', error_code = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (code, utc_now(), export_id, workspace_id),
        )


def _download_presentation_export(store, row: sqlite3.Row):
    from .office import MAX_INPUT_BYTES

    workspace_id = row["workspace_id"]
    version_row = store.row(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (row["artifact_id"], workspace_id, row["version"]),
    )
    if version_row is None:
        raise ApiError("PRESENTATION_RECOVERY_REQUIRED", "The saved presentation version is unavailable", 409, {"export_id": row["id"]})
    from .business_execution import saved_export_view
    version_row = saved_export_view(store, row["id"], version_row)
    target = _resource_path(store, row["relative_path"])
    if target.is_file():
        if target.stat().st_size > MAX_INPUT_BYTES:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise ApiError("FILE_INTEGRITY_ERROR", "The saved PPTX exceeds its supported size and was preserved", 409, {"export_id": row["id"]})
        content = target.read_bytes()
        valid = _validate_export_bytes("pptx", content, version_row["title"], version_row["body"], [], version_row=version_row)
        if not valid:
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise ApiError("FILE_INTEGRITY_ERROR", "The saved PPTX does not match its immutable presentation version; the file was preserved", 409, {"export_id": row["id"]})
        _complete_export_record(store, row["id"], workspace_id, content)
        path = target
    else:
        if target.exists():
            _mark_export_interrupted(store, row["id"], workspace_id, "FILE_INTEGRITY_ERROR")
            raise ApiError("FILE_INTEGRITY_ERROR", "The saved PPTX path is not a regular file; its contents were preserved", 409, {"export_id": row["id"]})
        _mark_export_interrupted(store, row["id"], workspace_id, "FILE_UNAVAILABLE")
        content = _render_export("pptx", version_row["title"], version_row["body"], [], store=store, version_row=version_row, identity=g.identity)
        if not _validate_export_bytes("pptx", content, version_row["title"], version_row["body"], [], version_row=version_row):
            raise ApiError("PRESENTATION_RECOVERY_REQUIRED", "The presentation source could not reproduce this saved version", 409, {"export_id": row["id"]})
        export_row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id))
        path, readback = _write_export_bytes(store, export_row, content)
        _complete_export_record(store, row["id"], workspace_id, readback)

    response = send_file(
        path, as_attachment=True, download_name=path.name,
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        conditional=False,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _create_export(store, artifact_id: str, data: dict, *, identity: dict | None = None, idempotency_key: str | None = None):
    identity = identity if identity is not None else _require_write()
    from .collaboration import require_artifact_access
    from .business_execution import business_export_view
    audience = data.get("audience", "internal")
    if not isinstance(audience, str) or audience not in {"internal", "client"}:
        raise ApiError("INVALID_EXPORT_AUDIENCE", "Choose the internal or client delivery view", 400)
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
    format_name = data.get("format")
    if not isinstance(format_name, str) or format_name not in {"md", "docx", "pptx", "html", "xlsx", "pdf"}:
        raise ApiError("INVALID_INPUT", "format must be md, docx, pptx, html, xlsx or pdf", 400)
    key = idempotency_key if idempotency_key is not None else _idempotency_key()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for export", 400)
    workspace_id = identity["workspace"]["id"]
    fingerprint = _request_fingerprint({"artifact_id": artifact_id, "version": version, "format": format_name, **({"audience": audience} if audience != "internal" else {})})
    export_id = str(uuid.uuid4())
    now = utc_now()
    created = False
    with store.transaction() as connection:
        artifact = require_artifact_access(connection, identity, artifact_id)
        if artifact is None:
            raise _not_found()
        selected = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, version),
        ).fetchone()
        if selected is None:
            raise _not_found()
        _authorize_native_artifact(store, identity, selected)
        selected = business_export_view(selected, audience)
        try:
            snapshot = json.loads(selected["input_snapshot_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise ApiError("ARTIFACT_SNAPSHOT_UNREADABLE", "This Artifact version has no readable output context", 409) from exc
        kind = snapshot.get("kind") if isinstance(snapshot, dict) else None
        native_format = {"presentation": "pptx", "spreadsheet": "xlsx", "pdf_document": "pdf"}.get(kind)
        if native_format and format_name != native_format:
            raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "This original Office result requires a supported conversion for that format", 415)
        if format_name in {"pptx", "xlsx", "pdf"}:
            try:
                snapshot = json.loads(selected["input_snapshot_json"] or "{}")
            except (TypeError, ValueError) as exc:
                raise ApiError("ARTIFACT_SNAPSHOT_UNREADABLE", "This Artifact version has no readable output context", 409) from exc
            kind = snapshot.get("kind") if isinstance(snapshot, dict) else None
            if kind in {"document", "presentation"} and not (kind == "presentation" and format_name == "pptx"):
                raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "This original Office Artifact does not include confirmed data for that conversion", 415)
            if kind == "presentation":
                presentation = snapshot.get("presentation")
                file_reference = presentation.get("file") if isinstance(presentation, dict) else None
                if not isinstance(file_reference, dict):
                    raise ApiError("PRESENTATION_FILE_UNAVAILABLE", "The saved presentation file reference is incomplete", 409)
                if (file_reference.get("kind") != "export" or file_reference.get("format") != "pptx"
                        or not isinstance(file_reference.get("export_id"), str)
                        or not isinstance(file_reference.get("sha256"), str)
                        or type(file_reference.get("size_bytes")) is not int
                        or file_reference.get("size_bytes") <= 0):
                    raise ApiError("PRESENTATION_FILE_UNAVAILABLE", "The saved presentation file reference is incomplete", 409)
        existing = connection.execute("SELECT * FROM exports WHERE workspace_id = ? AND idempotency_key = ?", (workspace_id, key)).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different export", 409)
            export_id = existing["id"]
            relative_path = existing["relative_path"]
            existing_status = existing["status"]
            existing_digest = existing["sha256"]
            existing_size = existing["size_bytes"]
            export_snapshot = dict(selected)
            if audience == "client":
                saved_view = connection.execute("SELECT view_json FROM business_export_views WHERE export_id=? AND audience='client'", (export_id,)).fetchone()
                if saved_view is None:
                    raise ApiError("EXPORT_VIEW_UNAVAILABLE", "The saved client delivery view is unavailable; no internal content was substituted", 409)
                export_snapshot.update(json.loads(saved_view["view_json"]))
        else:
            relative_path = f"objects/{workspace_id}/exports/{export_id}.{format_name}"
            connection.execute(
                "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'writing', ?, ?, ?)",
                (export_id, workspace_id, artifact_id, version, format_name, key, fingerprint, relative_path, now, now),
            )
            existing_status = "writing"
            existing_digest = None
            existing_size = None
            created = True
            export_snapshot = dict(selected)
            if audience == "client":
                view = {field: export_snapshot[field] for field in ("title", "body", "citations_json", "input_snapshot_json")}
                connection.execute("INSERT INTO business_export_views VALUES(?,'client',?)", (export_id, json.dumps(view, ensure_ascii=False)))
    citations = json.loads(export_snapshot["citations_json"])
    target = _resource_path(store, relative_path)
    try:
        native_presentation = False
        if format_name == "pptx":
            output_snapshot = json.loads(export_snapshot["input_snapshot_json"] or "{}")
            native_presentation = isinstance(output_snapshot, dict) and output_snapshot.get("kind") == "presentation"
        if native_presentation:
            citation_lines = []
        else:
            with store.transaction(immediate=False) as connection:
                citation_lines = _citation_lines(connection, workspace_id, citations)
        if target.is_file():
            readback = target.read_bytes()
            digest = hashlib.sha256(readback).hexdigest()
            if existing_status == "completed":
                valid_existing = _validate_export_bytes(
                    format_name, readback, export_snapshot["title"], export_snapshot["body"], citation_lines,
                    store=store, version_row=export_snapshot, identity=identity,
                )
                if valid_existing:
                    if digest != existing_digest or existing_size != len(readback):
                        _complete_export_record(store, export_id, workspace_id, readback)
                    row = store.row("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id))
                    return _success(_export_data(row, audience, export_snapshot["title"]), 200)
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE exports SET status = 'interrupted', error_code = 'FILE_INTEGRITY_ERROR', updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'completed'",
                        (utc_now(), export_id, workspace_id),
                    )
                raise ApiError("EXPORT_RECOVERY_REQUIRED", "Saved export failed integrity verification; the existing file was preserved", 409, {"export_id": export_id})
            if _validate_export_bytes(
                format_name, readback, export_snapshot["title"], export_snapshot["body"], citation_lines,
                store=store, version_row=export_snapshot, identity=identity,
            ):
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE exports SET status = 'completed', sha256 = ?, size_bytes = ?, error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('writing', 'interrupted')",
                        (digest, len(readback), utc_now(), export_id, workspace_id),
                    )
                    row = connection.execute("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id)).fetchone()
                    if row["status"] != "completed" or row["sha256"] != digest:
                        raise ApiError("EXPORT_STATE_CONFLICT", "Export recovery could not be confirmed", 409, {"export_id": export_id})
                return _success(_export_data(row, audience, export_snapshot["title"]), 200)
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE exports SET status = 'interrupted', error_code = 'UNVERIFIED_OUTPUT_PRESERVED', updated_at = ? WHERE id = ? AND workspace_id = ? AND status != 'completed'",
                    (utc_now(), export_id, workspace_id),
                )
            raise ApiError("EXPORT_RECOVERY_REQUIRED", "An existing export file could not be verified and was preserved", 409, {"export_id": export_id})

        with store.transaction() as connection:
            connection.execute(
                "UPDATE exports SET status = 'writing', error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('writing', 'interrupted', 'completed')",
                (utc_now(), export_id, workspace_id),
            )
            export_row = connection.execute("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id)).fetchone()
            if export_row is None:
                raise _not_found()
            if export_row["status"] != "writing":
                raise ApiError("EXPORT_STATE_CONFLICT", "Export recovery is already in progress", 409, {"export_id": export_id})

        content = _render_export(
            format_name, export_snapshot["title"], export_snapshot["body"], citation_lines,
            store=store, version_row=export_snapshot, identity=identity,
        )
        target, readback = _write_export_bytes(store, export_row, content)
        if not _validate_export_bytes(
            format_name, readback, export_snapshot["title"], export_snapshot["body"], citation_lines,
            store=store, version_row=export_snapshot, identity=identity,
        ):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE exports SET status = 'interrupted', error_code = 'UNVERIFIED_OUTPUT_PRESERVED', updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'writing'",
                    (utc_now(), export_id, workspace_id),
                )
            raise ApiError("EXPORT_READBACK_FAILED", "Export did not pass content readback; the file and recovery record were preserved", 500, {"export_id": export_id})
        digest = hashlib.sha256(readback).hexdigest()
        with store.transaction() as connection:
            connection.execute("UPDATE exports SET status = 'completed', sha256 = ?, size_bytes = ?, error_code = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'writing'", (digest, len(readback), utc_now(), export_id, workspace_id))
            row = connection.execute("SELECT * FROM exports WHERE id = ? AND workspace_id = ?", (export_id, workspace_id)).fetchone()
            if row is None or row["status"] != "completed" or row["sha256"] != digest:
                raise ApiError("EXPORT_STATE_CONFLICT", "Export completion could not be confirmed", 409, {"export_id": export_id})
    except Exception as exc:
        with store.transaction() as connection:
            connection.execute("UPDATE exports SET status = 'interrupted', error_code = 'EXPORT_FAILED', updated_at = ? WHERE id = ? AND status = 'writing'", (utc_now(), export_id))
        if isinstance(exc, ApiError):
            raise
        raise ApiError("EXPORT_FAILED", "Export was interrupted; its recovery record is preserved", 500, {"export_id": export_id}) from exc
    return _success(_export_data(row, audience, export_snapshot["title"]), 201 if created else 200)
