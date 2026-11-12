"""User-correctable workspace context, separate from facts and training data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from .intelligence_base import IntelligenceError, identity_scope


INTELLIGENCE_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_memories (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    work_version INTEGER,
    kind TEXT NOT NULL CHECK(kind IN ('preference', 'working_context', 'correction')),
    content TEXT,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'forgotten')),
    use_enabled INTEGER NOT NULL DEFAULT 1 CHECK(use_enabled IN (0, 1)),
    canonical_state TEXT NOT NULL DEFAULT 'NOT_CANONICAL' CHECK(canonical_state = 'NOT_CANONICAL'),
    training_use TEXT NOT NULL DEFAULT 'NOT_GRANTED' CHECK(training_use = 'NOT_GRANTED'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(memory_id, version),
    CHECK((status = 'forgotten' AND content IS NULL AND source_refs_json = '[]')
       OR (status != 'forgotten' AND content IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS intelligence_memories_one_active
    ON intelligence_memories(memory_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS intelligence_memories_owner_scope
    ON intelligence_memories(workspace_id, owner_user_id, work_id, status, updated_at DESC);
"""

_ALLOWED_KINDS = {"preference", "working_context", "correction"}
_MAX_MEMORY_CHARACTERS = 8_000
_MAX_SOURCE_REFS = 32


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def install_intelligence_memory_schema(store) -> None:
    """Add only the current Memory owner tables; never import or seed old rows."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + INTELLIGENCE_MEMORY_SCHEMA + "\nCOMMIT;")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(intelligence_memories)")}
        if "use_enabled" not in columns:
            connection.execute(
                "ALTER TABLE intelligence_memories ADD COLUMN use_enabled INTEGER NOT NULL DEFAULT 1 "
                "CHECK(use_enabled IN (0, 1))"
            )
    finally:
        connection.close()


def _verify_actor(connection, identity: dict, *, write: bool = False) -> tuple[str, str, str | None]:
    workspace_id, user_id, session_id = identity_scope(identity)
    member = connection.execute(
        "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if member is None or member["active"] != 1:
        raise IntelligenceError("ACCESS_REVOKED", 403)
    if write and member["role"] not in {"owner", "editor"}:
        raise IntelligenceError("WRITE_ACCESS_DENIED", 403)
    if session_id is not None:
        from .auth import utc_now
        session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        if session is None or session["revoked_at"] is not None or session["expires_at"] <= utc_now():
            raise IntelligenceError("UNAUTHENTICATED", 401)
    return workspace_id, user_id, session_id


def _work_version(connection, workspace_id: str, work_id: str | None, *, require_active: bool) -> int | None:
    if work_id is None:
        return None
    if not isinstance(work_id, str) or not work_id:
        raise IntelligenceError("MEMORY_WORK_SCOPE_INVALID", 400)
    row = connection.execute(
        "SELECT version, status FROM works WHERE id = ? AND workspace_id = ?",
        (work_id, workspace_id),
    ).fetchone()
    if row is None:
        raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
    if require_active and row["status"] != "active":
        raise IntelligenceError("WORK_ARCHIVED", 409)
    return row["version"]


def _validate_source_refs(connection, workspace_id: str, work_id: str | None, source_refs) -> list[dict]:
    if (not isinstance(source_refs, list) or len(source_refs) > _MAX_SOURCE_REFS
            or any(not isinstance(item, dict) for item in source_refs)):
        raise IntelligenceError("MEMORY_SOURCE_REFS_INVALID", 400)
    if source_refs and work_id is None:
        raise IntelligenceError("MEMORY_SOURCE_SCOPE_REQUIRED", 400)
    try:
        from .api import _validate_citations
        citations = _validate_citations(connection, workspace_id, source_refs)
    except Exception as exc:
        raise IntelligenceError("MEMORY_SOURCE_REF_INVALID", 409) from exc
    validated = []
    for citation in citations:
        material = connection.execute(
            "SELECT sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
            (citation["material_id"], workspace_id),
        ).fetchone()
        if material is None:
            raise IntelligenceError("MEMORY_SOURCE_REF_INVALID", 404)
        if material["status"] not in {"ready", "partial"}:
            raise IntelligenceError("MEMORY_SOURCE_UNAVAILABLE", 409)
        relation = connection.execute(
            "SELECT 1 FROM work_materials WHERE work_id = ? AND material_id = ? AND workspace_id = ?",
            (work_id, citation["material_id"], workspace_id),
        ).fetchone()
        if relation is None:
            raise IntelligenceError("MEMORY_SOURCE_OUTSIDE_WORK", 404)
        validated.append({
            **citation,
            "material_sha256": material["sha256"],
            "source_state": "USER_ASSERTED_CONTEXT",
        })
    return validated


def _load_refs(value) -> list[dict]:
    try:
        refs = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        raise IntelligenceError("MEMORY_SOURCE_STATE_UNAVAILABLE", 409) from exc
    if not isinstance(refs, list):
        raise IntelligenceError("MEMORY_SOURCE_STATE_UNAVAILABLE", 409)
    return refs


def _source_state(connection, workspace_id: str, refs: list[dict]) -> tuple[str, list[dict]]:
    current_refs = []
    for ref in refs:
        material_id = ref.get("material_id") if isinstance(ref, dict) else None
        segment_id = ref.get("segment_id") if isinstance(ref, dict) else None
        quote = ref.get("quote") if isinstance(ref, dict) else None
        if not all(isinstance(item, str) and item for item in (material_id, segment_id, quote)):
            return "stale_source", []
        row = connection.execute(
            "SELECT m.sha256, m.status, s.text FROM materials m JOIN material_segments s "
            "ON s.material_id = m.id AND s.workspace_id = m.workspace_id "
            "WHERE m.id = ? AND m.workspace_id = ? AND s.id = ?",
            (material_id, workspace_id, segment_id),
        ).fetchone()
        if (row is None or row["status"] not in {"ready", "partial"}
                or row["sha256"] != ref.get("material_sha256") or quote not in row["text"]):
            return "stale_source", []
        current_refs.append(dict(ref))
    return "current" if refs else "no_external_source", current_refs


def _memory_data(connection, row, *, include_history: bool = False) -> dict:
    refs = _load_refs(row["source_refs_json"])
    source_state, refs = _source_state(connection, row["workspace_id"], refs)
    current_work_version = _work_version(
        connection, row["workspace_id"], row["work_id"], require_active=False,
    ) if row["work_id"] else None
    parent_state = (
        "current" if row["work_id"] is None or current_work_version == row["work_version"]
        else "parent_version_changed"
    )
    return {
        "memory_id": row["memory_id"], "version": row["version"],
        "work_id": row["work_id"], "work_version": row["work_version"],
        "kind": row["kind"], "content": row["content"], "source_refs": refs,
        "status": row["status"], "use_enabled": bool(row["use_enabled"]),
        "source_state": source_state,
        "parent_state": parent_state,
        "context_eligible": (
            row["status"] == "active" and row["use_enabled"] == 1
            and source_state in {"current", "no_external_source"}
            and parent_state == "current"
        ),
        "canonical_state": "NOT_CANONICAL", "training_use": "NOT_GRANTED",
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "history_included": include_history,
    }


class CorrectableMemory:
    """One Store-backed, actor-scoped memory owner with immutable correction history."""

    def __init__(self, store):
        self.store = store

    def create(self, identity: dict, *, kind: str, content: str, work_id: str | None = None,
               source_refs: list[dict] | None = None) -> dict:
        if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
            raise IntelligenceError("MEMORY_KIND_INVALID", 400)
        if not isinstance(content, str) or not content.strip() or len(content) > _MAX_MEMORY_CHARACTERS:
            raise IntelligenceError("MEMORY_CONTENT_INVALID", 400)
        if kind == "correction" and work_id is None:
            raise IntelligenceError("MEMORY_WORK_SCOPE_REQUIRED", 400)
        memory_id = str(uuid.uuid4())
        now = _now()
        with self.store.transaction() as connection:
            workspace_id, user_id, _session_id = _verify_actor(connection, identity, write=True)
            parent_version = _work_version(connection, workspace_id, work_id, require_active=True)
            refs = _validate_source_refs(
                connection, workspace_id, work_id,
                source_refs if source_refs is not None else [],
            )
            connection.execute(
                "INSERT INTO intelligence_memories(memory_id, version, workspace_id, owner_user_id, work_id, "
                "work_version, kind, content, source_refs_json, status, created_at, updated_at, created_by) "
                "VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (memory_id, workspace_id, user_id, work_id, parent_version, kind, content.strip(),
                 json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now, now, user_id),
            )
            row = connection.execute(
                "SELECT * FROM intelligence_memories WHERE memory_id = ? AND version = 1",
                (memory_id,),
            ).fetchone()
            return _memory_data(connection, row)

    def list(self, identity: dict, *, work_id: str | None = None, include_history: bool = False) -> list[dict]:
        if type(include_history) is not bool:
            raise IntelligenceError("MEMORY_QUERY_INVALID", 400)
        with self.store.transaction(immediate=False) as connection:
            workspace_id, user_id, _session_id = _verify_actor(connection, identity)
            if work_id is not None:
                _work_version(connection, workspace_id, work_id, require_active=False)
            if include_history:
                rows = connection.execute(
                    "SELECT * FROM intelligence_memories WHERE workspace_id = ? AND owner_user_id = ? "
                    "AND (work_id = ? OR work_id IS NULL) ORDER BY memory_id, version DESC",
                    (workspace_id, user_id, work_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM intelligence_memories WHERE workspace_id = ? AND owner_user_id = ? "
                    "AND status = 'active' AND (work_id = ? OR work_id IS NULL) ORDER BY updated_at DESC, memory_id",
                    (workspace_id, user_id, work_id),
                ).fetchall()
            return [_memory_data(connection, row, include_history=include_history) for row in rows]

    def context(self, identity: dict, *, work_id: str | None) -> dict:
        memories = self.list(identity, work_id=work_id, include_history=False)
        eligible = [item for item in memories if item["context_eligible"]]
        return {
            "items": eligible,
            "excluded_count": len(memories) - len(eligible),
            "status": "ready" if eligible else "empty",
            "canonical_state": "NOT_CANONICAL",
            "parameter_training": "NOT_GRANTED",
        }

    def set_use_enabled(self, identity: dict, memory_id: str, *, base_version: int,
                        enabled: bool) -> dict:
        if (not isinstance(memory_id, str) or not memory_id
                or isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1
                or type(enabled) is not bool):
            raise IntelligenceError("MEMORY_VERSION_INVALID", 400)
        now = _now()
        with self.store.transaction() as connection:
            workspace_id, user_id, _session_id = _verify_actor(connection, identity, write=True)
            current = connection.execute(
                "SELECT * FROM intelligence_memories WHERE memory_id = ? AND workspace_id = ? "
                "AND owner_user_id = ? AND status = 'active'",
                (memory_id, workspace_id, user_id),
            ).fetchone()
            if current is None:
                raise IntelligenceError("MEMORY_NOT_FOUND", 404)
            if current["version"] != base_version:
                raise IntelligenceError("MEMORY_VERSION_CONFLICT", 409,
                                        {"current_version": current["version"]})
            if bool(current["use_enabled"]) == enabled:
                return _memory_data(connection, current)

            if current["work_id"] is None:
                work_version = None
            else:
                work_version = _work_version(
                    connection, workspace_id, current["work_id"], require_active=enabled,
                )
            refs = _load_refs(current["source_refs_json"])
            if enabled:
                source_state, _current_refs = _source_state(connection, workspace_id, refs)
                if source_state == "stale_source":
                    raise IntelligenceError("MEMORY_SOURCE_STALE", 409)

            new_version = base_version + 1
            connection.execute(
                "UPDATE intelligence_memories SET status = 'superseded', updated_at = ? "
                "WHERE memory_id = ? AND workspace_id = ? AND owner_user_id = ? "
                "AND version = ? AND status = 'active'",
                (now, memory_id, workspace_id, user_id, base_version),
            )
            connection.execute(
                "INSERT INTO intelligence_memories(memory_id, version, workspace_id, owner_user_id, work_id, "
                "work_version, kind, content, source_refs_json, status, use_enabled, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (memory_id, new_version, workspace_id, user_id, current["work_id"], work_version,
                 current["kind"], current["content"], current["source_refs_json"], int(enabled),
                 current["created_at"], now, user_id),
            )
            row = connection.execute(
                "SELECT * FROM intelligence_memories WHERE memory_id = ? AND version = ?",
                (memory_id, new_version),
            ).fetchone()
            return _memory_data(connection, row)

    def correct(self, identity: dict, memory_id: str, *, base_version: int,
                content: str, source_refs: list[dict] | None = None) -> dict:
        if (not isinstance(memory_id, str) or not memory_id
                or isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1):
            raise IntelligenceError("MEMORY_VERSION_INVALID", 400)
        if not isinstance(content, str) or not content.strip() or len(content) > _MAX_MEMORY_CHARACTERS:
            raise IntelligenceError("MEMORY_CONTENT_INVALID", 400)
        now = _now()
        with self.store.transaction() as connection:
            workspace_id, user_id, _session_id = _verify_actor(connection, identity, write=True)
            current = connection.execute(
                "SELECT * FROM intelligence_memories WHERE memory_id = ? AND workspace_id = ? "
                "AND owner_user_id = ? AND status = 'active'",
                (memory_id, workspace_id, user_id),
            ).fetchone()
            if current is None:
                raise IntelligenceError("MEMORY_NOT_FOUND", 404)
            if current["version"] != base_version:
                raise IntelligenceError("MEMORY_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            work_version = _work_version(connection, workspace_id, current["work_id"], require_active=True)
            if source_refs is None:
                old_refs = _load_refs(current["source_refs_json"])
                source_state, _current_refs = _source_state(connection, workspace_id, old_refs)
                if source_state == "stale_source":
                    raise IntelligenceError("MEMORY_SOURCE_STALE", 409)
                normalized = [{key: value for key, value in ref.items()
                               if key in {"material_id", "segment_id", "quote"}}
                              for ref in old_refs]
            else:
                normalized = source_refs
            refs = _validate_source_refs(connection, workspace_id, current["work_id"], normalized)
            new_version = base_version + 1
            connection.execute(
                "UPDATE intelligence_memories SET status = 'superseded', updated_at = ? "
                "WHERE memory_id = ? AND workspace_id = ? AND owner_user_id = ? AND version = ? AND status = 'active'",
                (now, memory_id, workspace_id, user_id, base_version),
            )
            connection.execute(
                "INSERT INTO intelligence_memories(memory_id, version, workspace_id, owner_user_id, work_id, "
                "work_version, kind, content, source_refs_json, status, use_enabled, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (memory_id, new_version, workspace_id, user_id, current["work_id"], work_version,
                 current["kind"], content.strip(),
                 json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 int(current["use_enabled"]), now, now, user_id),
            )
            row = connection.execute(
                "SELECT * FROM intelligence_memories WHERE memory_id = ? AND version = ?",
                (memory_id, new_version),
            ).fetchone()
            return _memory_data(connection, row)

    def forget(self, identity: dict, memory_id: str, *, base_version: int) -> dict:
        if (not isinstance(memory_id, str) or not memory_id
                or isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1):
            raise IntelligenceError("MEMORY_VERSION_INVALID", 400)
        now = _now()
        with self.store.transaction() as connection:
            workspace_id, user_id, _session_id = _verify_actor(connection, identity, write=True)
            current = connection.execute(
                "SELECT version FROM intelligence_memories WHERE memory_id = ? AND workspace_id = ? "
                "AND owner_user_id = ? AND status = 'active'",
                (memory_id, workspace_id, user_id),
            ).fetchone()
            if current is None:
                existing = connection.execute(
                    "SELECT 1 FROM intelligence_memories WHERE memory_id = ? AND workspace_id = ? AND owner_user_id = ?",
                    (memory_id, workspace_id, user_id),
                ).fetchone()
                if existing is None:
                    raise IntelligenceError("MEMORY_NOT_FOUND", 404)
                return {"memory_id": memory_id, "status": "forgotten", "already_forgotten": True}
            if current["version"] != base_version:
                raise IntelligenceError("MEMORY_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            connection.execute(
                "UPDATE intelligence_memories SET status = 'forgotten', content = NULL, source_refs_json = '[]', "
                "use_enabled = 0, updated_at = ? WHERE memory_id = ? AND workspace_id = ? AND owner_user_id = ?",
                (now, memory_id, workspace_id, user_id),
            )
            return {"memory_id": memory_id, "status": "forgotten", "content_retained": False}
