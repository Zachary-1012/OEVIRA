"""Author-private workspace notes (便笺) that sit beside Artifact, Work and Scene.

Notes are not a second document store. A note is a personal draft the author can
hide, reopen, organize, and adopt into a formal artifact. Adoption rechecks
client-commission scope and SourceUse, never grants training rights, and never
invents Evidence for a sourceless personal note. Closing hides the note; there
is no delete. Un-adopting does not erase the original note. Moving a note
across commissions does not move the underlying materials.
"""
from __future__ import annotations

import json
import uuid

from flask import g, request

from .api import (
    ApiError,
    _artifact_data,
    _artifact_row,
    _body,
    _idempotency_key,
    _idempotent_resource,
    _input_snapshot,
    _not_found,
    _optional_string,
    _raw_text,
    _request_fingerprint,
    _require_write,
    _string,
    _success,
    _validate_citations,
)
from .auth import utc_now
from .collaboration import require_artifact_access
from .source_use import SourceUseError, resolve_source_use


_BODY_MAXIMUM = 100_000
_EXCERPT_MAXIMUM = 100_000
_OBJECT_MAXIMUM = 8_000
_STATUSES = {"draft", "saved"}
_KINDS = {"original", "organized_candidate"}
_SOURCE_REF_KEYS = {
    "kind", "id", "material_id", "segment_id", "quote", "revision", "url", "work_id",
}
_SPECIAL_SNAPSHOTS = {
    "business_analysis": ("BUSINESS_REVISION_REQUIRED", "Revise the selected business sections through their calculation and review owner"),
    "document": ("DOCUMENT_REVISION_REQUIRED", "Word documents must be changed through a document revision"),
    "spreadsheet": ("SPREADSHEET_REVISION_REQUIRED", "Revise the selected workbook cells to preserve its formulas and structure"),
    "pdf_document": ("PDF_REVISION_REQUIRED", "Revise this PDF through its exact page and source anchors"),
    "research": ("RESEARCH_REVISION_REQUIRED", "Revise the selected research propositions and their evidence"),
    "community_native": ("COMMUNITY_REVISION_REQUIRED", "Revise this native work through its publication draft"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    work_id TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'saved')),
    hidden INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL CHECK(kind IN ('original', 'organized_candidate')),
    parent_note_id TEXT,
    source_ref_json TEXT NOT NULL,
    anchor_json TEXT NOT NULL,
    excerpt_kind TEXT,
    version INTEGER NOT NULL,
    adopted_artifact_id TEXT,
    adopted_artifact_version INTEGER,
    adopted_excerpt TEXT,
    adopted_at TEXT,
    adoption_revoked_at TEXT,
    training_rights INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note_versions (
    note_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    hidden INTEGER NOT NULL,
    source_ref_json TEXT NOT NULL,
    anchor_json TEXT NOT NULL,
    excerpt_kind TEXT,
    work_id TEXT,
    adopted_artifact_id TEXT,
    adopted_artifact_version INTEGER,
    adopted_excerpt TEXT,
    adopted_at TEXT,
    adoption_revoked_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (note_id, version)
);
CREATE INDEX IF NOT EXISTS idx_notes_author ON notes(workspace_id, author_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_notes_work ON notes(workspace_id, work_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_notes_parent ON notes(parent_note_id);
"""


def install_notes_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value, default):
    if not value:
        return default
    loaded = json.loads(value)
    return loaded if isinstance(loaded, type(default)) else default


def _optional_object(value, field: str, allowed: set[str]) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", f"{field} must be an object", 400, {"field": field})
    unknown = set(value) - allowed
    if unknown:
        raise ApiError("INVALID_INPUT", f"{field} contains an unsupported field", 400, {"field": field, "fields": sorted(unknown)})
    result = {}
    for key, item in value.items():
        if item is None:
            continue
        if not isinstance(item, str):
            raise ApiError("INVALID_INPUT", f"{field}.{key} must be a string", 400, {"field": f"{field}.{key}"})
        text = item.strip() if key != "quote" else item
        if not text:
            continue
        if len(text) > (10000 if key == "quote" else 2000):
            raise ApiError("INVALID_INPUT", f"{field}.{key} is too long", 400, {"field": f"{field}.{key}"})
        result[key] = text
    encoded = _dumps(result)
    if len(encoded.encode("utf-8")) > _OBJECT_MAXIMUM:
        raise ApiError("INVALID_INPUT", f"{field} is too large", 400, {"field": field})
    return result


def _status(value, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    status = _string(value, "status", maximum=20)
    if status not in _STATUSES:
        raise ApiError("INVALID_INPUT", "status must be draft or saved", 400, {"field": "status"})
    return status


def _bool_field(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError("INVALID_INPUT", f"{field} must be a boolean", 400, {"field": field})
    return value


def _work_id(value, field: str = "work_id") -> str | None:
    if value is None:
        return None
    return _string(value, field, maximum=80)


def _commission_id(connection, workspace_id: str, work_id: str | None) -> str | None:
    if not work_id:
        return None
    row = connection.execute(
        "SELECT c.id FROM client_commissions c "
        "JOIN work_commissions w ON w.commission_id = c.id "
        "WHERE w.work_id = ? AND c.workspace_id = ?",
        (work_id, workspace_id),
    ).fetchone()
    return row["id"] if row is not None else None


def _require_work(connection, workspace_id: str, work_id: str):
    work = connection.execute(
        "SELECT id, status FROM works WHERE id = ? AND workspace_id = ?",
        (work_id, workspace_id),
    ).fetchone()
    if work is None:
        raise _not_found()
    if work["status"] != "active":
        raise ApiError("WORK_ARCHIVED", "Reopen this work before changing its notes", 409)
    return work


def _note_data(row) -> dict:
    source_ref = _loads(row["source_ref_json"], {})
    anchor = _loads(row["anchor_json"], {})
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "author_id": row["author_id"],
        "work_id": row["work_id"],
        "body": row["body"],
        "status": row["status"],
        "hidden": bool(row["hidden"]),
        "kind": row["kind"],
        "parent_note_id": row["parent_note_id"],
        "source_ref": source_ref or None,
        "anchor": anchor or None,
        "excerpt_kind": row["excerpt_kind"],
        "version": row["version"],
        "adopted_artifact_id": row["adopted_artifact_id"],
        "adopted_artifact_version": row["adopted_artifact_version"],
        "adopted_excerpt": row["adopted_excerpt"],
        "adopted_at": row["adopted_at"],
        "adoption_revoked_at": row["adoption_revoked_at"],
        "training_rights": False,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _owned_note(connection, identity: dict, note_id: str):
    workspace_id = identity["workspace"]["id"]
    author_id = identity["user"]["id"]
    row = connection.execute(
        "SELECT * FROM notes WHERE id = ? AND workspace_id = ? AND author_id = ?",
        (note_id, workspace_id, author_id),
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


def _insert_version(connection, row, *, now: str) -> None:
    connection.execute(
        "INSERT INTO note_versions("
        "note_id, workspace_id, version, body, status, hidden, source_ref_json, "
        "anchor_json, excerpt_kind, work_id, adopted_artifact_id, adopted_artifact_version, "
        "adopted_excerpt, adopted_at, adoption_revoked_at, created_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"], row["workspace_id"], row["version"], row["body"], row["status"],
            int(row["hidden"]), row["source_ref_json"], row["anchor_json"], row["excerpt_kind"],
            row["work_id"], row["adopted_artifact_id"], row["adopted_artifact_version"],
            row["adopted_excerpt"], row["adopted_at"], row["adoption_revoked_at"], now,
        ),
    )


def _reload(connection, workspace_id: str, note_id: str):
    row = connection.execute(
        "SELECT * FROM notes WHERE id = ? AND workspace_id = ?",
        (note_id, workspace_id),
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


def _remember(connection, workspace_id: str, operation: str, key: str | None,
              fingerprint: str, resource_type: str, resource_id: str, now: str) -> None:
    if not key:
        return
    connection.execute(
        "INSERT INTO idempotency_records("
        "workspace_id, operation, key, request_hash, resource_type, resource_id, created_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
        (workspace_id, operation, key, fingerprint, resource_type, resource_id, now),
    )


def _citation_from_source_ref(source_ref: dict | None) -> dict | None:
    if not isinstance(source_ref, dict):
        return None
    material_id = source_ref.get("material_id") or (source_ref.get("id") if source_ref.get("kind") == "material" else None)
    segment_id = source_ref.get("segment_id")
    quote = source_ref.get("quote")
    if not (isinstance(material_id, str) and material_id and isinstance(segment_id, str) and segment_id and isinstance(quote, str) and quote):
        if isinstance(material_id, str) and material_id:
            raise ApiError(
                "INVALID_CITATION",
                "A sourced note must carry material_id, segment_id and a quote that matches saved source text",
                400,
            )
        return None
    return {"material_id": material_id, "segment_id": segment_id, "quote": quote}


def _merge_citations(existing: list[dict], added: dict | None) -> list[dict]:
    if added is None:
        return list(existing)
    for item in existing:
        if item.get("material_id") == added["material_id"] and item.get("segment_id") == added["segment_id"] and item.get("quote") == added["quote"]:
            return list(existing)
    return [*existing, added]


def _resolve_adopt_source_use(connection, identity: dict, source_ref: dict | None, *,
                              workspace_id: str, work_id: str | None) -> dict | None:
    citation = _citation_from_source_ref(source_ref)
    if citation is None:
        return None
    material = connection.execute(
        "SELECT id, sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
        (citation["material_id"], workspace_id),
    ).fetchone()
    if material is None:
        raise ApiError("SOURCE_UNAVAILABLE", "The cited material is no longer available", 409)
    try:
        decision = resolve_source_use(
            connection, identity,
            {"kind": "material", "id": material["id"], "revision": material["sha256"]},
            field_paths=["text"], purpose="note.adopt", action="cite",
            recipient={"kind": "local", "id": workspace_id}, work_id=work_id,
        )
    except SourceUseError as exc:
        details = getattr(exc, "details", None) or {}
        raise ApiError(exc.code, "This source cannot be used for this adoption", getattr(exc, "status", 400), details) from exc
    if not decision.allowed:
        raise ApiError(
            decision.reason_code or "SOURCE_USE_DENIED",
            "Source use is not allowed for this adoption",
            403,
            {"source_use": decision.to_dict()},
        )
    return citation


def _reject_special_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        return
    kind = snapshot.get("kind")
    if kind in _SPECIAL_SNAPSHOTS:
        code, message = _SPECIAL_SNAPSHOTS[kind]
        raise ApiError(code, message, 409)
    if kind == "presentation":
        raise ApiError("PRESENTATION_SNAPSHOT_CONFLICT", "Text revisions must preserve the saved PPTX version reference", 409)
    if kind == "media":
        raise ApiError("MEDIA_REVISION_REQUIRED", "Revise this media project through its media owner", 409)


def _adopt_into_artifact(connection, identity: dict, *, artifact_id: str, base_artifact_version: int,
                         excerpt: str, citation: dict | None, revision_note: str, now: str) -> dict:
    workspace_id = identity["workspace"]["id"]
    row = require_artifact_access(connection, identity, artifact_id, write=True)
    if row["current_version"] != base_artifact_version:
        raise ApiError(
            "VERSION_CONFLICT",
            "Artifact has changed since it was opened",
            409,
            {"current": _artifact_data(connection, row)},
        )
    current = connection.execute(
        "SELECT title, body, citations_json, input_snapshot_json FROM artifact_versions "
        "WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, base_artifact_version),
    ).fetchone()
    if current is None:
        raise _not_found()
    prior_snapshot = json.loads(current["input_snapshot_json"] or "{}")
    _reject_special_snapshot(prior_snapshot)
    existing_citations = json.loads(current["citations_json"] or "[]")
    if not isinstance(existing_citations, list):
        existing_citations = []
    citations = _validate_citations(connection, workspace_id, _merge_citations(existing_citations, citation))
    snapshot = _input_snapshot(connection, workspace_id, row["work_id"], citations, prior_snapshot)
    body = current["body"] or ""
    if excerpt not in body:
        body = (body + "\n\n" if body and not body.endswith("\n\n") else body) + excerpt
        body = _raw_text(body, "body", maximum=200000, allow_empty=True)
    version = base_artifact_version + 1
    connection.execute(
        "INSERT INTO artifact_versions("
        "artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, "
        "revision_note, created_at, created_by, parent_version"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id, workspace_id, version, current["title"], body,
            _dumps(citations), _dumps(snapshot), revision_note, now,
            identity["user"]["id"], base_artifact_version,
        ),
    )
    connection.execute(
        "UPDATE artifacts SET current_version = ?, updated_at = ? "
        "WHERE id = ? AND workspace_id = ? AND current_version = ?",
        (version, now, artifact_id, workspace_id, base_artifact_version),
    )
    updated = _artifact_row(connection, workspace_id, artifact_id)
    return _artifact_data(connection, updated)


def register_notes_routes(app) -> None:
    store = app.extensions["oevira.store"]

    @app.post("/api/notes")
    def create_note():
        identity = _require_write()
        data = _body()
        allowed = {"body", "status", "work_id", "source_ref", "anchor", "excerpt_kind"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Note contains an unsupported field", 400, {"fields": sorted(set(data) - allowed)})
        body = _raw_text(data.get("body"), "body", maximum=_BODY_MAXIMUM)
        status = _status(data.get("status"), default="draft")
        work_id = _work_id(data.get("work_id"))
        source_ref = _optional_object(data.get("source_ref"), "source_ref", _SOURCE_REF_KEYS) or {}
        anchor = _optional_object(data.get("anchor"), "anchor", {"kind", "id", "path", "start", "end", "label"}) or {}
        excerpt_kind = _optional_string(data.get("excerpt_kind"), "excerpt_kind", maximum=80)
        # A sourced create stores the reference; citation validity is checked on adopt.
        if source_ref:
            _citation_from_source_ref(source_ref)
        key = _idempotency_key()
        fingerprint = _request_fingerprint({
            "body": body, "status": status, "work_id": work_id,
            "source_ref": source_ref, "anchor": anchor, "excerpt_kind": excerpt_kind,
        })
        workspace_id = identity["workspace"]["id"]
        author_id = identity["user"]["id"]
        note_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as connection:
            if work_id:
                _require_work(connection, workspace_id, work_id)
            existing = _idempotent_resource(connection, workspace_id, "note.create", key, fingerprint)
            if existing:
                return _success(_note_data(_owned_note(connection, identity, existing["resource_id"])))
            connection.execute(
                "INSERT INTO notes("
                "id, workspace_id, author_id, work_id, body, status, hidden, kind, parent_note_id, "
                "source_ref_json, anchor_json, excerpt_kind, version, training_rights, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, 0, 'original', NULL, ?, ?, ?, 1, 0, ?, ?)",
                (
                    note_id, workspace_id, author_id, work_id, body, status,
                    _dumps(source_ref), _dumps(anchor), excerpt_kind, now, now,
                ),
            )
            row = _reload(connection, workspace_id, note_id)
            _insert_version(connection, row, now=now)
            _remember(connection, workspace_id, "note.create", key, fingerprint, "note", note_id, now)
        return _success(_note_data(row), 201)

    @app.get("/api/notes")
    def list_notes():
        identity = g.identity
        workspace_id = identity["workspace"]["id"]
        author_id = identity["user"]["id"]
        work_id = request.args.get("work_id")
        kind = request.args.get("kind")
        include_hidden = request.args.get("include_hidden") in {"1", "true", "yes"}
        if kind is not None and kind not in _KINDS:
            raise ApiError("INVALID_INPUT", "kind must be original or organized_candidate", 400, {"field": "kind"})
        clauses = ["workspace_id = ?", "author_id = ?"]
        params: list = [workspace_id, author_id]
        if not include_hidden:
            clauses.append("hidden = 0")
        if work_id:
            clauses.append("work_id = ?")
            params.append(_string(work_id, "work_id", maximum=80))
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        with store.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM notes WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC, id DESC LIMIT 200",
                tuple(params),
            ).fetchall()
        return _success([_note_data(row) for row in rows])

    @app.get("/api/notes/<note_id>")
    def get_note(note_id: str):
        identity = g.identity
        with store.transaction(immediate=False) as connection:
            row = _owned_note(connection, identity, note_id)
        return _success(_note_data(row))

    @app.patch("/api/notes/<note_id>")
    def update_note(note_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"base_version", "body", "status", "hidden", "work_id", "source_ref", "anchor", "excerpt_kind"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Note update contains an unsupported field", 400, {"fields": sorted(set(data) - allowed)})
        base_version = data.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400, {"field": "base_version"})
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = _owned_note(connection, identity, note_id)
            if row["version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "Note has changed since it was opened", 409, {"current": _note_data(row)})
            body = row["body"]
            if "body" in data:
                body = _raw_text(data.get("body"), "body", maximum=_BODY_MAXIMUM)
            status = _status(data["status"]) if "status" in data else row["status"]
            hidden = int(_bool_field(data["hidden"], "hidden")) if "hidden" in data else row["hidden"]
            if "work_id" in data:
                work_id = _work_id(data.get("work_id"))
                if work_id:
                    _require_work(connection, workspace_id, work_id)
            else:
                work_id = row["work_id"]
            if "source_ref" in data:
                source_ref = _optional_object(data.get("source_ref"), "source_ref", _SOURCE_REF_KEYS) or {}
                if source_ref:
                    _citation_from_source_ref(source_ref)
            else:
                source_ref = _loads(row["source_ref_json"], {})
            if "anchor" in data:
                anchor = _optional_object(data.get("anchor"), "anchor", {"kind", "id", "path", "start", "end", "label"}) or {}
            else:
                anchor = _loads(row["anchor_json"], {})
            excerpt_kind = row["excerpt_kind"]
            if "excerpt_kind" in data:
                excerpt_kind = _optional_string(data.get("excerpt_kind"), "excerpt_kind", maximum=80)
            version = base_version + 1
            connection.execute(
                "UPDATE notes SET body=?, status=?, hidden=?, work_id=?, source_ref_json=?, "
                "anchor_json=?, excerpt_kind=?, version=?, updated_at=?, training_rights=0 "
                "WHERE id=? AND workspace_id=? AND version=?",
                (
                    body, status, hidden, work_id, _dumps(source_ref), _dumps(anchor),
                    excerpt_kind, version, now, note_id, workspace_id, base_version,
                ),
            )
            updated = _reload(connection, workspace_id, note_id)
            _insert_version(connection, updated, now=now)
        return _success(_note_data(updated))

    @app.post("/api/notes/<note_id>/organize")
    def organize_note(note_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"body", "source_ref", "anchor", "excerpt_kind", "status"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Organize request contains an unsupported field", 400, {"fields": sorted(set(data) - allowed)})
        organized_body = _raw_text(data.get("body"), "body", maximum=_BODY_MAXIMUM)
        status = _status(data.get("status"), default="saved")
        key = _idempotency_key()
        fingerprint = _request_fingerprint({
            "note_id": note_id, "body": organized_body,
            "source_ref": data.get("source_ref"), "anchor": data.get("anchor"),
            "excerpt_kind": data.get("excerpt_kind"), "status": status,
        })
        workspace_id = identity["workspace"]["id"]
        candidate_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as connection:
            original = _owned_note(connection, identity, note_id)
            existing = _idempotent_resource(connection, workspace_id, f"note.organize:{note_id}", key, fingerprint)
            if existing:
                return _success(_note_data(_owned_note(connection, identity, existing["resource_id"])))
            if "source_ref" in data:
                source_ref = _optional_object(data.get("source_ref"), "source_ref", _SOURCE_REF_KEYS) or {}
                if source_ref:
                    _citation_from_source_ref(source_ref)
            else:
                source_ref = _loads(original["source_ref_json"], {})
            if "anchor" in data:
                anchor = _optional_object(data.get("anchor"), "anchor", {"kind", "id", "path", "start", "end", "label"}) or {}
            else:
                anchor = {}
            excerpt_kind = _optional_string(data.get("excerpt_kind"), "excerpt_kind", maximum=80) if "excerpt_kind" in data else original["excerpt_kind"]
            connection.execute(
                "INSERT INTO notes("
                "id, workspace_id, author_id, work_id, body, status, hidden, kind, parent_note_id, "
                "source_ref_json, anchor_json, excerpt_kind, version, training_rights, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, 0, 'organized_candidate', ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    candidate_id, workspace_id, original["author_id"], original["work_id"],
                    organized_body, status, original["id"], _dumps(source_ref), _dumps(anchor),
                    excerpt_kind, now, now,
                ),
            )
            # The original note is left untouched: organize produces a child candidate only.
            row = _reload(connection, workspace_id, candidate_id)
            _insert_version(connection, row, now=now)
            _remember(connection, workspace_id, f"note.organize:{note_id}", key, fingerprint, "note", candidate_id, now)
        return _success(_note_data(row), 201)

    @app.post("/api/notes/<note_id>/adopt")
    def adopt_note(note_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"artifact_id", "base_artifact_version", "excerpt", "revision_note"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Adopt request contains an unsupported field", 400, {"fields": sorted(set(data) - allowed)})
        artifact_id = _string(data.get("artifact_id"), "artifact_id", maximum=80)
        base_artifact_version = data.get("base_artifact_version")
        if isinstance(base_artifact_version, bool) or not isinstance(base_artifact_version, int) or base_artifact_version < 1:
            raise ApiError("INVALID_INPUT", "base_artifact_version must be a positive integer", 400, {"field": "base_artifact_version"})
        excerpt = _raw_text(data.get("excerpt"), "excerpt", maximum=_EXCERPT_MAXIMUM)
        revision_note = _optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "采用自便笺"
        key = _idempotency_key()
        fingerprint = _request_fingerprint({
            "note_id": note_id, "artifact_id": artifact_id,
            "base_artifact_version": base_artifact_version, "excerpt": excerpt,
            "revision_note": revision_note,
        })
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            note = _owned_note(connection, identity, note_id)
            operation = f"note.adopt:{note_id}"
            existing = _idempotent_resource(connection, workspace_id, operation, key, fingerprint)
            if existing:
                current_note = _owned_note(connection, identity, note_id)
                artifact_row = _artifact_row(connection, workspace_id, current_note["adopted_artifact_id"] or artifact_id)
                return _success({
                    "note": _note_data(current_note),
                    "artifact": _artifact_data(connection, artifact_row) if artifact_row is not None else None,
                })
            if excerpt not in note["body"]:
                raise ApiError("INVALID_INPUT", "excerpt must be a portion of the note body", 400, {"field": "excerpt"})
            artifact_row = require_artifact_access(connection, identity, artifact_id, write=True)
            note_commission = _commission_id(connection, workspace_id, note["work_id"])
            artifact_commission = _commission_id(connection, workspace_id, artifact_row["work_id"])
            if note_commission != artifact_commission:
                raise ApiError(
                    "CLIENT_SCOPE_MISMATCH",
                    "Adopting a note cannot move materials across client commissions",
                    409,
                    {"note_commission_id": note_commission, "artifact_commission_id": artifact_commission},
                )
            source_ref = _loads(note["source_ref_json"], {})
            citation = _resolve_adopt_source_use(
                connection, identity, source_ref or None,
                workspace_id=workspace_id,
                work_id=artifact_row["work_id"] or note["work_id"],
            )
            artifact = _adopt_into_artifact(
                connection, identity,
                artifact_id=artifact_id,
                base_artifact_version=base_artifact_version,
                excerpt=excerpt,
                citation=citation,
                revision_note=revision_note,
                now=now,
            )
            version = note["version"] + 1
            connection.execute(
                "UPDATE notes SET adopted_artifact_id=?, adopted_artifact_version=?, adopted_excerpt=?, "
                "adopted_at=?, adoption_revoked_at=NULL, training_rights=0, version=?, updated_at=? "
                "WHERE id=? AND workspace_id=? AND version=?",
                (
                    artifact["id"], artifact["version"], excerpt, now, version, now,
                    note_id, workspace_id, note["version"],
                ),
            )
            updated = _reload(connection, workspace_id, note_id)
            _insert_version(connection, updated, now=now)
            _remember(connection, workspace_id, operation, key, fingerprint, "note_adoption", note_id, now)
        return _success({"note": _note_data(updated), "artifact": artifact})

    @app.delete("/api/notes/<note_id>")
    def delete_note(note_id: str):
        identity = _require_write()
        with store.transaction(immediate=False) as connection:
            _owned_note(connection, identity, note_id)
        raise ApiError("METHOD_NOT_ALLOWED", "Notes are hidden, not deleted", 405)

    @app.post("/api/notes/<note_id>/revoke-adoption")
    def revoke_note_adoption(note_id: str):
        identity = _require_write()
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            note = _owned_note(connection, identity, note_id)
            if not note["adopted_artifact_id"] or note["adoption_revoked_at"]:
                raise ApiError("ADOPTION_NOT_ACTIVE", "This note has no active adoption to revoke", 409)
            version = note["version"] + 1
            connection.execute(
                "UPDATE notes SET adoption_revoked_at=?, training_rights=0, version=?, updated_at=? "
                "WHERE id=? AND workspace_id=? AND version=?",
                (now, version, now, note_id, workspace_id, note["version"]),
            )
            updated = _reload(connection, workspace_id, note_id)
            _insert_version(connection, updated, now=now)
        return _success(_note_data(updated))
