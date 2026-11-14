"""Versioned, object-scoped SourceUse decisions and a side-effect-free resolver."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any, Iterable

from flask import Flask, g, request

from . import api as _api
from .auth import utc_now
from .collaboration import (
    require_artifact_access,
    require_material_access,
    require_work_access,
    require_workspace_access,
    work_commission,
)


SOURCE_USE_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_use_decisions (
    decision_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    object_kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_revision TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('workspace','work','client')),
    work_id TEXT REFERENCES works(id),
    client_id TEXT,
    field_paths_json TEXT NOT NULL,
    purpose TEXT NOT NULL,
    action TEXT NOT NULL,
    recipient_kind TEXT NOT NULL,
    recipient_ref TEXT NOT NULL,
    basis_status TEXT NOT NULL CHECK(basis_status IN (
        'contract_verified','user_declared','unknown','denied','unavailable','pending_review'
    )),
    decision_status TEXT NOT NULL CHECK(decision_status IN (
        'allowed','denied','pending_review','unavailable','revoked','requires_reauthorization'
    )),
    allow_external_processing INTEGER NOT NULL DEFAULT 0 CHECK(allow_external_processing IN (0,1)),
    allow_redistribution INTEGER NOT NULL DEFAULT 0 CHECK(allow_redistribution IN (0,1)),
    allow_training INTEGER NOT NULL DEFAULT 0 CHECK(allow_training IN (0,1)),
    retention_until TEXT,
    refresh_after TEXT,
    expires_at TEXT,
    basis_ref_json TEXT NOT NULL DEFAULT '{}',
    policy_scope_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    revoked_at TEXT,
    revoked_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, owner_user_id, policy_scope_sha256)
);
CREATE INDEX IF NOT EXISTS source_use_object_recent
    ON source_use_decisions(workspace_id, owner_user_id, object_kind, object_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS source_use_scope_recent
    ON source_use_decisions(workspace_id, owner_user_id, work_id, client_id, purpose, action);
CREATE TABLE IF NOT EXISTS source_use_decision_versions (
    decision_id TEXT NOT NULL REFERENCES source_use_decisions(decision_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version >= 1),
    snapshot_json TEXT NOT NULL,
    changed_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(decision_id, version)
);
CREATE INDEX IF NOT EXISTS source_use_versions_workspace
    ON source_use_decision_versions(workspace_id, decision_id, version);
"""


_BASIS_STATES = {
    "contract_verified", "user_declared", "unknown", "denied", "unavailable", "pending_review",
}
_PUBLIC_BASIS_STATES = {"user_declared", "unknown", "denied", "unavailable", "pending_review"}
_DECISION_STATES = {
    "allowed", "denied", "pending_review", "unavailable", "revoked", "requires_reauthorization",
}
_LOCAL_ACTIONS = {
    "read", "read_history", "parse", "index", "search", "analyze", "cite", "derive", "export_local",
}
_SENSITIVE_ACTIONS = {"refresh", "external_process", "redistribute", "publish", "train", "evaluate"}
_ACTIONS = _LOCAL_ACTIONS | _SENSITIVE_ACTIONS
_PURPOSE = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_FIELD = re.compile(r"^(?:\*|[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,199})$")
_UTC = timezone.utc


class SourceUseError(Exception):
    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class SourceUseDecision:
    allowed: bool
    state: str
    basis_status: str
    decision_id: str | None
    version: int | None
    workspace_id: str
    actor_user_id: str
    object_kind: str
    object_id: str
    object_revision: str | None
    field_paths: tuple[str, ...]
    purpose: str
    action: str
    recipient_kind: str
    recipient_ref: str
    work_id: str | None
    client_id: str | None
    source_ref_sha256: str
    policy_sha256: str
    valid_until: str | None
    reason_code: str | None
    lineage_decision_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["field_paths"] = list(self.field_paths)
        return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _loads(value: Any, expected: type, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, expected) else default


def _time_text(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise SourceUseError("INVALID_INPUT", 400, {"field": field})
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceUseError("INVALID_INPUT", 400, {"field": field}) from exc
    if parsed.tzinfo is None:
        raise SourceUseError("INVALID_INPUT", 400, {"field": field})
    return parsed.astimezone(_UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _future(value: str | None, now: str) -> bool:
    if value is None:
        return True
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


def _actor_scope(connection, identity: dict, work_id: str | None):
    user = identity.get("user") if isinstance(identity, dict) else None
    workspace = identity.get("workspace") if isinstance(identity, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
    session_id = identity.get("session_id") if isinstance(identity, dict) else None
    worker = bool(isinstance(identity, dict) and identity.get("source_use_worker"))
    if not all(isinstance(value, str) and value for value in (user_id, workspace_id)):
        raise SourceUseError("UNAUTHENTICATED", 401)
    if not worker and not (isinstance(session_id, str) and session_id):
        raise SourceUseError("UNAUTHENTICATED", 401)
    member = connection.execute(
        "SELECT active,role FROM memberships WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    ).fetchone()
    now = utc_now()
    if member is None or member["active"] != 1:
        raise SourceUseError("ACCESS_REVOKED", 403)
    if not worker:
        session = connection.execute(
            "SELECT revoked_at,expires_at FROM sessions WHERE id=? AND workspace_id=? AND user_id=?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        if (session is None or session["revoked_at"] is not None or session["expires_at"] <= now):
            raise SourceUseError("ACCESS_REVOKED", 403)
    client_id = None
    if work_id is not None:
        if not isinstance(work_id, str) or not work_id:
            raise SourceUseError("INVALID_INPUT", 400, {"field": "work_id"})
        try:
            require_work_access(connection, identity, work_id)
        except _api.ApiError as exc:
            raise SourceUseError("NOT_FOUND", 404) from exc
        client_id = work_commission(connection, work_id)
    else:
        try:
            require_workspace_access(connection, identity)
        except _api.ApiError as exc:
            raise SourceUseError("NOT_FOUND", 404) from exc
    return workspace_id, user_id, member["role"], client_id


def install_source_use_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SOURCE_USE_SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def _ref_value(source_ref: dict, *keys: str):
    for key in keys:
        value = source_ref.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _source_context(connection, identity: dict, source_ref: dict, work_id: str | None):
    if not isinstance(source_ref, dict):
        raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
    kind = source_ref.get("kind")
    if not isinstance(kind, str) or not _IDENTIFIER.fullmatch(kind):
        raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
    owner_user_id = identity["user"]["id"]
    source_owner_user_id = owner_user_id
    workspace_id = identity["workspace"]["id"]
    object_id = _ref_value(source_ref, "id", "object_id")
    source_work_id = source_ref.get("work_id") if isinstance(source_ref.get("work_id"), str) else None
    private_source = False
    explicit_marker = False
    current_revision = None
    resolved_kind = kind

    material_id = _ref_value(source_ref, "material_id")
    if kind in {"material", "material_segment", "media_project_source", "artifact_citation", "material_body_candidate"}:
        material_id = material_id or object_id
        row = connection.execute(
            "SELECT id,workspace_id,owner_user_id,work_id,sha256,status,source_json,source_url "
            "FROM materials WHERE id=? AND workspace_id=?",
            (material_id, workspace_id),
        ).fetchone()
        if row is None:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404)
        try:
            require_material_access(
                connection, identity, material_id,
                target_work_id=work_id or source_work_id or row["work_id"],
            )
        except _api.ApiError as exc:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        resolved_kind = "material"
        object_id = material_id
        source_work_id = row["work_id"]
        source_owner_user_id = row["owner_user_id"]
        current_revision = row["sha256"]
        metadata = _loads(row["source_json"], dict, {})
        explicit_marker = any(
            key in metadata for key in ("source_use", "source_use_decision", "source_use_decision_id")
        )
        origin = metadata.get("acquisition_method")
        private_source = bool(
            row["owner_user_id"] == owner_user_id
            and not row["source_url"]
            and origin not in {"public_http", "browser_capture", "platform", "connected_source"}
        )
        expected_revision = _ref_value(source_ref, "revision", "material_sha256", "sha256")
        if expected_revision is not None and expected_revision != current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
    elif kind in {"artifact", "artifact_version"}:
        artifact_id = _ref_value(source_ref, "artifact_id") or object_id
        if not artifact_id:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        try:
            artifact = require_artifact_access(connection, identity, artifact_id)
        except _api.ApiError as exc:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        version = source_ref.get("version")
        if version is None:
            version = artifact.get("version") if isinstance(artifact, dict) else artifact["version"]
        version_row = connection.execute(
            "SELECT title,body,citations_json,input_snapshot_json FROM artifact_versions "
            "WHERE artifact_id=? AND workspace_id=? AND version=?",
            (artifact_id, workspace_id, version),
        ).fetchone()
        if version_row is None:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        resolved_kind, object_id = "artifact", artifact_id
        source_work_id = artifact.get("work_id") if isinstance(artifact, dict) else artifact["work_id"]
        source_owner_user_id = artifact.get("owner_user_id") if isinstance(artifact, dict) else artifact["owner_user_id"]
        current_revision = _digest({
            "id": artifact_id, "version": version, "title": version_row["title"],
            "body": version_row["body"], "citations": _loads(version_row["citations_json"], list, []),
            "input_snapshot": _loads(version_row["input_snapshot_json"], dict, {}),
        })
        if source_ref.get("content_sha256") and source_ref["content_sha256"] != current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        private_source = artifact["owner_user_id"] == owner_user_id
    elif kind in {"work", "work_version"}:
        source_work_id = _ref_value(source_ref, "work_id") or object_id
        if not source_work_id:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        try:
            work = require_work_access(connection, identity, source_work_id)
        except _api.ApiError as exc:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        version = source_ref.get("version")
        if version is None:
            version = work["version"]
        version_row = connection.execute(
            "SELECT title,goal,brief_json FROM work_versions WHERE work_id=? AND version=?",
            (source_work_id, version),
        ).fetchone()
        if version_row is None:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        resolved_kind, object_id = "work", source_work_id
        source_owner_user_id = work.get("owner_user_id") if isinstance(work, dict) else work["owner_user_id"]
        current_revision = _digest({
            "work_id": source_work_id, "version": version, "title": version_row["title"],
            "goal": version_row["goal"], "brief": _loads(version_row["brief_json"], dict, {}),
        })
        if source_ref.get("content_sha256") and source_ref["content_sha256"] != current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        private_source = work["owner_user_id"] == owner_user_id
    elif kind in {"media_project", "media_project_version"}:
        project_id = _ref_value(source_ref, "media_project_id") or object_id
        if not project_id:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
            (project_id, workspace_id),
        ).fetchone()
        version = source_ref.get("version")
        if version is None and project is not None:
            version = project["current_version"]
        version_row = connection.execute(
            "SELECT state_json FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
            (project_id, workspace_id, version),
        ).fetchone()
        if project is None or version_row is None:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404)
        if project["work_id"]:
            try:
                require_work_access(connection, identity, project["work_id"])
            except _api.ApiError as exc:
                raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        else:
            try:
                require_workspace_access(connection, identity)
            except _api.ApiError as exc:
                raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        resolved_kind, object_id = "media_project", project_id
        source_work_id = project["work_id"]
        source_owner_user_id = project["owner_user_id"]
        current_revision = _digest({"project_id": project_id, "version": version,
                                    "state_json": _loads(version_row["state_json"], dict, {})})
        if source_ref.get("content_sha256") and source_ref["content_sha256"] != current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        private_source = project["owner_user_id"] == owner_user_id
    elif kind in {"browser_capture", "browser_page"}:
        object_id = _ref_value(source_ref, "capture_id", "operation_id", "id")
        current_revision = _ref_value(source_ref, "revision", "preview_sha256", "snapshot_sha256")
        if not object_id or not current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        operation = connection.execute(
            "SELECT workspace_id,owner_user_id,result_json FROM browser_session_operations "
            "WHERE id=? AND workspace_id=? AND operation='context_preview' AND status='completed'",
            (object_id, workspace_id),
        ).fetchone()
        if operation is None:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404)
        receipt = _loads(operation["result_json"], dict, {})
        capture_ref = receipt.get("capture_ref") if isinstance(receipt, dict) else None
        if (not isinstance(capture_ref, dict) or capture_ref.get("capture_id") != object_id
                or capture_ref.get("preview_sha256") != current_revision
                or (source_ref.get("work_id") is not None
                    and source_ref.get("work_id") != capture_ref.get("work_id"))):
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        resolved_kind = "browser_capture"
        source_work_id = capture_ref.get("work_id") if isinstance(capture_ref.get("work_id"), str) else None
        source_owner_user_id = operation["owner_user_id"]
        private_source = False
    elif kind in {"user_input", "conversation_message", "user_memory", "skill_package"}:
        object_id = object_id or _ref_value(source_ref, "turn_id", "memory_id", "skill_id")
        current_revision = _ref_value(source_ref, "revision", "version", "content_sha256") or _digest(source_ref)
        resolved_kind = kind
        source_owner_user_id = owner_user_id
        private_source = True
    elif kind in {"monitor_source"}:
        source_id = object_id or _ref_value(source_ref, "source_id", "monitor_source_id")
        if not source_id:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        row = connection.execute(
            "SELECT * FROM monitor_sources WHERE id=? AND workspace_id=?",
            (source_id, workspace_id),
        ).fetchone()
        if row is None:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404)
        rule = connection.execute(
            "SELECT * FROM monitor_rules WHERE id=? AND workspace_id=?",
            (row["monitor_id"], workspace_id),
        ).fetchone()
        if rule is None:
            raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404)
        if rule["work_id"]:
            try:
                require_work_access(connection, identity, rule["work_id"])
            except _api.ApiError as exc:
                raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        else:
            try:
                require_workspace_access(connection, identity)
            except _api.ApiError as exc:
                raise SourceUseError("SOURCE_USE_SOURCE_UNAVAILABLE", 404) from exc
        resolved_kind, object_id = "monitor_source", row["id"]
        source_work_id = rule["work_id"]
        source_owner_user_id = rule["owner_user_id"]
        # Identity is the source row and its URL. Operational timestamps (last_check_at, pending_job_id, updated_at) must not invalidate a grant.
        current_revision = _digest({"id": row["id"], "url": row["url"]})
        expected_revision = _ref_value(source_ref, "revision", "url_sha256")
        if expected_revision is not None and expected_revision != current_revision:
            raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
        private_source = False
    elif kind in {"url", "public_url"}:
        url = _ref_value(source_ref, "url") or object_id
        if not isinstance(url, str) or not url or len(url) > 2000:
            raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
        resolved_kind, object_id = "url", url
        source_work_id = source_ref.get("work_id") if isinstance(source_ref.get("work_id"), str) else source_work_id
        source_owner_user_id = owner_user_id
        current_revision = _ref_value(source_ref, "revision") or _digest({"url": url})
        private_source = False
    else:
        raise SourceUseError("SOURCE_USE_OBJECT_KIND_UNAVAILABLE", 409, {"object_kind": kind})

    if not isinstance(object_id, str) or not object_id:
        raise SourceUseError("SOURCE_USE_SOURCE_REF_INVALID", 400)
    current_work_id = work_id or source_work_id
    if source_work_id and work_id and source_work_id != work_id:
        source_client = work_commission(connection, source_work_id)
        target_client = work_commission(connection, work_id)
        if source_client is not None and target_client is not None and source_client != target_client:
            raise SourceUseError("SOURCE_USE_CLIENT_SCOPE_MISMATCH", 403)
    _workspace_id, actor_user_id, role, client_id = _actor_scope(connection, identity, current_work_id)
    source_ref_revision = _ref_value(source_ref, "revision", "material_sha256", "sha256",
                                     "content_sha256", "snapshot_sha256", "preview_sha256")
    if source_ref_revision is not None and str(source_ref_revision) != str(current_revision):
        raise SourceUseError("SOURCE_USE_SOURCE_REVISION_STALE", 409)
    return {
        "workspace_id": workspace_id,
        "owner_user_id": owner_user_id,
        "source_owner_user_id": source_owner_user_id,
        "actor_user_id": actor_user_id,
        "role": role,
        "object_kind": resolved_kind,
        "object_id": object_id,
        "object_revision": str(current_revision or "unversioned"),
        "work_id": current_work_id,
        "client_id": client_id,
        "private_source": private_source,
        "explicit_marker": explicit_marker,
    }


def _field_paths(value: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple, set)):
        raise SourceUseError("SOURCE_USE_FIELDS_INVALID", 400)
    fields = tuple(sorted(set(value)))
    if (not fields or len(fields) > 100
            or any(not isinstance(item, str) or not _FIELD.fullmatch(item) for item in fields)):
        raise SourceUseError("SOURCE_USE_FIELDS_INVALID", 400)
    return fields


def _recipient(value: dict) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "id"}:
        raise SourceUseError("SOURCE_USE_RECIPIENT_INVALID", 400)
    kind, recipient_ref = value.get("kind"), value.get("id")
    if (not isinstance(kind, str) or not _IDENTIFIER.fullmatch(kind)
            or not isinstance(recipient_ref, str) or not _IDENTIFIER.fullmatch(recipient_ref)):
        raise SourceUseError("SOURCE_USE_RECIPIENT_INVALID", 400)
    return kind, recipient_ref


def _scope_matches(row, work_id: str | None, client_id: str | None) -> bool:
    if row["scope_kind"] == "workspace":
        return row["work_id"] is None and row["client_id"] is None
    if row["scope_kind"] == "work":
        return bool(work_id and row["work_id"] == work_id)
    if row["scope_kind"] == "client":
        return bool(client_id and row["client_id"] == client_id)
    return False


def _fields_overlap(stored: list[str], requested: tuple[str, ...]) -> bool:
    return "*" in stored or "*" in requested or bool(set(stored).intersection(requested))


def _fields_cover(stored: list[str], requested: tuple[str, ...]) -> bool:
    return "*" in stored or ("*" not in requested and set(requested).issubset(set(stored)))


def _result(decision: bool, state: str, basis: str, *, context: dict, fields: tuple[str, ...],
            purpose: str, action: str, recipient_kind: str, recipient_ref: str,
            decision_id: str | None = None, version: int | None = None,
            policy_sha256: str | None = None, valid_until: str | None = None,
            reason_code: str | None = None, lineage_decision_ids: tuple[str, ...] = ()) -> SourceUseDecision:
    object_ref_hash = _digest({
        "workspace_id": context["workspace_id"], "actor_user_id": context["actor_user_id"],
        "object_kind": context["object_kind"], "object_id": context["object_id"],
        "object_revision": context["object_revision"], "field_paths": list(fields),
        "work_id": context["work_id"], "client_id": context["client_id"],
        "purpose": purpose, "action": action,
        "recipient": {"kind": recipient_kind, "id": recipient_ref},
    })
    return SourceUseDecision(
        allowed=decision, state=state, basis_status=basis, decision_id=decision_id,
        version=version, workspace_id=context["workspace_id"], actor_user_id=context["actor_user_id"],
        object_kind=context["object_kind"], object_id=context["object_id"],
        object_revision=context["object_revision"], field_paths=fields, purpose=purpose,
        action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
        work_id=context["work_id"], client_id=context["client_id"], source_ref_sha256=object_ref_hash,
        policy_sha256=policy_sha256 or _digest({"state": state, "basis_status": basis}),
        valid_until=valid_until, reason_code=reason_code, lineage_decision_ids=lineage_decision_ids,
    )


def _resolve_single_source_use(connection, identity: dict, source_ref: dict, *, field_paths,
                               purpose: str, action: str, recipient: dict,
                               work_id: str | None = None, now: str | None = None) -> SourceUseDecision:
    """Resolve one owner-verified ObjectRef against current SourceUse and Core scope.

    This function is deliberately read-only and synchronous. Source owners first
    validate the concrete object, then call it with its stable kind/id/revision.
    """
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise SourceUseError("SOURCE_USE_PURPOSE_INVALID", 400)
    if action not in _ACTIONS:
        raise SourceUseError("SOURCE_USE_ACTION_UNSUPPORTED", 400)
    fields = _field_paths(field_paths)
    recipient_kind, recipient_ref = _recipient(recipient)
    context = _source_context(connection, identity, source_ref, work_id)
    now_text = now or utc_now()
    if not isinstance(now_text, str):
        raise SourceUseError("SOURCE_USE_CLOCK_INVALID", 500)
    if ((recipient_kind == "local" and recipient_ref != context["workspace_id"])
            or (action in _LOCAL_ACTIONS and recipient_kind != "local")):
        return _result(
            False, "recipient_mismatch", "unknown", context=context, fields=fields,
            purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
            reason_code="SOURCE_USE_RECIPIENT_MISMATCH",
        )

    try:
        source_metadata = {}
        if context["object_kind"] == "material":
            material = connection.execute(
                "SELECT source_json FROM materials WHERE id=? AND workspace_id=?",
                (context["object_id"], context["workspace_id"]),
            ).fetchone()
            source_metadata = _loads(material["source_json"], dict, {}) if material else {}
    except Exception:
        source_metadata = {}
    declared_id = source_metadata.get("source_use_decision_id")
    if not isinstance(declared_id, str):
        declared = source_metadata.get("source_use_decision")
        declared_id = declared.get("decision_id") if isinstance(declared, dict) else None
    if declared_id is not None and not isinstance(declared_id, str):
        declared_id = ""
    rows = connection.execute(
        "SELECT * FROM source_use_decisions WHERE workspace_id=? AND owner_user_id=? "
        "AND object_kind=? AND object_id=? ORDER BY version DESC,updated_at DESC,decision_id",
        (context["workspace_id"], context["source_owner_user_id"], context["object_kind"], context["object_id"]),
    ).fetchall()
    same_rule = []
    for row in rows:
        if declared_id and row["decision_id"] != declared_id:
            continue
        if row["purpose"] != purpose or row["action"] != action:
            continue
        same_rule.append(row)

    applicable = []
    if same_rule:
        current_revision = [row for row in same_rule if row["object_revision"] == context["object_revision"]]
        if not current_revision:
            stale = same_rule[0]
            # A revocation, denial or restored grant still binds the object. Do not
            # hide it behind STALE just because the URL identity changed.
            if (stale["decision_status"] in {"denied", "revoked", "requires_reauthorization"}
                    or stale["basis_status"] == "denied"):
                state = "revoked" if stale["decision_status"] == "revoked" else "denied"
                return _result(
                    False, state, stale["basis_status"], context=context, fields=fields,
                    purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                    decision_id=stale["decision_id"], version=stale["version"],
                    policy_sha256=_digest({key: stale[key] for key in stale.keys() if key != "updated_at"}),
                    reason_code="SOURCE_USE_REVOKED" if state == "revoked" else "SOURCE_USE_DENIED",
                    lineage_decision_ids=(stale["decision_id"],),
                )
            return _result(
                False, "stale", stale["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=stale["decision_id"], version=stale["version"],
                policy_sha256=_digest({key: stale[key] for key in stale.keys() if key != "updated_at"}),
                reason_code="SOURCE_USE_SOURCE_REVISION_STALE",
            )
        scope_matches = [row for row in current_revision if _scope_matches(row, context["work_id"], context["client_id"])]
        if not scope_matches:
            return _result(
                False, "scope_mismatch", "unknown", context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=current_revision[0]["decision_id"], version=current_revision[0]["version"],
                reason_code="SOURCE_USE_WORK_SCOPE_MISMATCH",
            )
        recipient_matches = [row for row in scope_matches if (
            row["recipient_kind"] == recipient_kind and row["recipient_ref"] == recipient_ref
        )]
        if not recipient_matches:
            return _result(
                False, "recipient_mismatch", "unknown", context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=scope_matches[0]["decision_id"], version=scope_matches[0]["version"],
                reason_code="SOURCE_USE_RECIPIENT_MISMATCH",
            )
        overlapping = []
        for row in recipient_matches:
            stored_fields = _loads(row["field_paths_json"], list, [])
            if _fields_overlap(stored_fields, fields):
                overlapping.append(row)
            if _fields_cover(stored_fields, fields):
                applicable.append(row)
        if not overlapping:
            return _result(
                False, "field_scope_mismatch", "unknown", context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=recipient_matches[0]["decision_id"], version=recipient_matches[0]["version"],
                reason_code="SOURCE_USE_FIELDS_NOT_ALLOWED",
            )
        # A narrower denied or unresolved field grant cannot be hidden by a
        # wider overlapping allow. Allowed rows must cover the complete request.
        applicable = overlapping

    if declared_id and not applicable:
        return _result(
            False, "unknown", "unknown", context=context, fields=fields, purpose=purpose,
            action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
            reason_code="SOURCE_USE_DECLARATION_UNAVAILABLE",
        )
    if applicable:
        # A matching deny or unresolved decision always wins over an overlapping grant.
        policy = applicable[0]
        policy_rows_hash = [
            {key: row[key] for key in row.keys() if key != "updated_at"}
            for row in applicable
        ]
        policy_hash = _digest(policy_rows_hash)
        if any(item["decision_status"] in {"denied", "revoked", "requires_reauthorization"}
               or item["basis_status"] == "denied" for item in applicable):
            denied = next(item for item in applicable if item["decision_status"] in {
                "denied", "revoked", "requires_reauthorization",
            } or item["basis_status"] == "denied")
            state = "revoked" if denied["decision_status"] == "revoked" else "denied"
            return _result(
                False, state, denied["basis_status"], context=context, fields=fields, purpose=purpose,
                action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=denied["decision_id"], version=denied["version"],
                policy_sha256=_digest({key: denied[key] for key in denied.keys() if key != "updated_at"}),
                reason_code="SOURCE_USE_REVOKED" if state == "revoked" else "SOURCE_USE_DENIED",
                lineage_decision_ids=(denied["decision_id"],),
            )
        if any(item["decision_status"] in {"pending_review", "unavailable"}
               or item["basis_status"] in {"unknown", "unavailable", "pending_review"}
               for item in applicable):
            unresolved = next(item for item in applicable if item["decision_status"] in {
                "pending_review", "unavailable",
            } or item["basis_status"] in {"unknown", "unavailable", "pending_review"})
            return _result(
                False, unresolved["decision_status"], unresolved["basis_status"], context=context,
                fields=fields, purpose=purpose, action=action, recipient_kind=recipient_kind,
                recipient_ref=recipient_ref, decision_id=unresolved["decision_id"],
                version=unresolved["version"],
                policy_sha256=_digest({key: unresolved[key] for key in unresolved.keys() if key != "updated_at"}),
                reason_code="SOURCE_USE_REVIEW_REQUIRED",
                lineage_decision_ids=(unresolved["decision_id"],),
            )
        if not all(_future(item["expires_at"], now_text) and _future(item["retention_until"], now_text)
                   for item in applicable):
            return _result(
                False, "expired", policy["basis_status"], context=context, fields=fields, purpose=purpose,
                action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_EXPIRED",
            )
        if any(item["refresh_after"] and not _future(item["refresh_after"], now_text)
               for item in applicable) and action != "read_history":
            return _result(
                False, "refresh_due", policy["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_REFRESH_REQUIRED",
            )
        if any(item["decision_status"] != "allowed" for item in applicable):
            return _result(
                False, "unavailable", policy["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_NOT_ALLOWED",
                lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
            )
        if "*" in fields:
            fields_covered = any("*" in _loads(item["field_paths_json"], list, []) for item in applicable)
        else:
            allowed_fields = set()
            for item in applicable:
                item_fields = _loads(item["field_paths_json"], list, [])
                if "*" in item_fields:
                    allowed_fields = set(fields)
                    break
                allowed_fields.update(item_fields)
            fields_covered = set(fields).issubset(allowed_fields)
        if not fields_covered:
            return _result(
                False, "field_scope_mismatch", policy["basis_status"], context=context,
                fields=fields, purpose=purpose, action=action, recipient_kind=recipient_kind,
                recipient_ref=recipient_ref, decision_id=policy["decision_id"], version=policy["version"],
                policy_sha256=policy_hash, reason_code="SOURCE_USE_FIELDS_NOT_ALLOWED",
                lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
            )
        if action == "external_process" and any(
            item["basis_status"] != "contract_verified" or not item["allow_external_processing"]
            for item in applicable
        ):
            return _result(
                False, "pending_review", policy["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_EXTERNAL_PROCESSING_NOT_VERIFIED",
                lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
            )
        if action in {"redistribute", "publish"} and any(
            item["basis_status"] != "contract_verified" or not item["allow_redistribution"]
            for item in applicable
        ):
            return _result(
                False, "pending_review", policy["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_REDISTRIBUTION_NOT_VERIFIED",
                lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
            )
        if action in {"train", "evaluate"} and any(
            item["basis_status"] != "contract_verified" or not item["allow_training"]
            for item in applicable
        ):
            return _result(
                False, "pending_review", policy["basis_status"], context=context, fields=fields,
                purpose=purpose, action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                decision_id=policy["decision_id"], version=policy["version"], policy_sha256=policy_hash,
                reason_code="SOURCE_USE_TRAINING_NOT_VERIFIED",
                lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
            )
        valid_until = min(
            (value for value in (policy["expires_at"], policy["retention_until"]) if value),
            default=None,
        )
        return _result(
            True, "allowed", policy["basis_status"], context=context, fields=fields, purpose=purpose,
            action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
            decision_id=policy["decision_id"], version=policy["version"],
            policy_sha256=policy_hash, valid_until=valid_until,
            lineage_decision_ids=tuple(sorted({item["decision_id"] for item in applicable})),
        )

    if context["explicit_marker"]:
        return _result(
            False, "unknown", "unknown", context=context, fields=fields, purpose=purpose,
            action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
            reason_code="SOURCE_USE_DECLARATION_UNAVAILABLE",
        )
    if action in _LOCAL_ACTIONS:
        return _result(
            True, "not_recorded_local", "unknown", context=context, fields=fields, purpose=purpose,
            action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
            reason_code="SOURCE_USE_NOT_REQUIRED_FOR_LOCAL_PROCESSING",
        )
    state = "not_recorded_private" if context["private_source"] else "unknown"
    return _result(
        False, state, "unknown", context=context, fields=fields, purpose=purpose,
        action=action, recipient_kind=recipient_kind, recipient_ref=recipient_ref,
        reason_code="SOURCE_USE_DECISION_REQUIRED",
    )



# Appended public SourceUse owner: resolver lineage, service, API, restore, model intersection.

def _row_dict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _lineage_key(kind: str, object_id: str, revision: str | None) -> tuple[str, str, str]:
    return (kind, object_id, revision or "unversioned")


def _parent_source_refs(connection, context: dict, source_ref: dict) -> list[dict]:
    collected: list[dict] = []

    def _take(value) -> None:
        if isinstance(value, dict) and value:
            collected.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict) and item:
                    collected.append(item)

    _take(source_ref.get("source_use_source_ref"))
    _take(source_ref.get("source_use_source_refs"))
    if context.get("object_kind") == "material":
        material = connection.execute(
            "SELECT source_json FROM materials WHERE id=? AND workspace_id=?",
            (context["object_id"], context["workspace_id"]),
        ).fetchone()
        metadata = _loads(material["source_json"], dict, {}) if material else {}
        _take(metadata.get("source_use_source_ref"))
        _take(metadata.get("source_use_source_refs"))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[dict] = []
    for item in collected:
        marker = _json(item)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


_MAX_LINEAGE_DEPTH = 16


def resolve_source_use(connection, identity: dict, source_ref: dict, *, field_paths,
                       purpose: str, action: str, recipient: dict,
                       work_id: str | None = None, now: str | None = None,
                       _seen: frozenset | None = None,
                       _depth: int = 0) -> SourceUseDecision:
    """Public, side-effect-free resolver, including derived-source lineage.

    Owners pass a concrete ObjectRef. Derived Materials may declare a parent
    through ``source_use_source_ref``; a child cannot exceed a denied, revoked,
    expired or missing parent grant for the same purpose, action and recipient.
    This function never writes decisions.
    """
    if _depth > _MAX_LINEAGE_DEPTH:
        raise SourceUseError("SOURCE_USE_LINEAGE_TOO_DEEP", 409)
    decision = _resolve_single_source_use(
        connection, identity, source_ref, field_paths=field_paths, purpose=purpose,
        action=action, recipient=recipient, work_id=work_id, now=now,
    )
    key = _lineage_key(decision.object_kind, decision.object_id, decision.object_revision)
    seen = _seen or frozenset()
    if key in seen:
        return replace(
            decision, allowed=False, state="unavailable",
            reason_code="SOURCE_USE_LINEAGE_CYCLE",
        )
    context = {
        "workspace_id": decision.workspace_id,
        "actor_user_id": decision.actor_user_id,
        "object_kind": decision.object_kind,
        "object_id": decision.object_id,
        "object_revision": decision.object_revision,
        "work_id": decision.work_id,
        "client_id": decision.client_id,
    }
    parents = _parent_source_refs(connection, {**context, "workspace_id": decision.workspace_id}, source_ref)
    lineage = list(decision.lineage_decision_ids)
    for parent_ref in parents:
        parent = resolve_source_use(
            connection, identity, parent_ref, field_paths=field_paths, purpose=purpose,
            action=action, recipient=recipient, work_id=work_id or decision.work_id,
            now=now, _seen=seen | {key}, _depth=_depth + 1,
        )
        if parent.decision_id:
            lineage.append(parent.decision_id)
        lineage.extend(parent.lineage_decision_ids)
        if not parent.allowed:
            return replace(
                decision, allowed=False, state=parent.state,
                basis_status=parent.basis_status,
                reason_code=parent.reason_code or "SOURCE_USE_PARENT_NOT_ALLOWED",
                valid_until=parent.valid_until,
                lineage_decision_ids=tuple(sorted({item for item in lineage if item})),
            )
        if parent.valid_until and (decision.valid_until is None or parent.valid_until < decision.valid_until):
            decision = replace(decision, valid_until=parent.valid_until)
    if lineage:
        decision = replace(decision, lineage_decision_ids=tuple(sorted({item for item in lineage if item})))
    return decision


def prepare_restored_source_use_decision(row: dict, *, now: str) -> dict:
    """A restored grant is not an active authorization. Revocations stay revoked."""
    result = dict(row)
    status = result.get("decision_status")
    if status in {"allowed", "pending_review", "unavailable"}:
        result["decision_status"] = "requires_reauthorization"
        result["updated_at"] = now
        try:
            result["version"] = int(result.get("version") or 1) + 1
        except (TypeError, ValueError):
            result["version"] = 2
    return result


def intersect_model_or_cpu_admission(decision: SourceUseDecision, admission: dict | None) -> SourceUseDecision:
    """Intersect a SourceUse decision with a model or CPU admission.

    A missing admission cannot authorize sensitive actions and cannot turn a
    denial into an allow. Local actions that SourceUse already allowed do not
    require a second admission gate.
    """
    if not decision.allowed:
        return decision
    if decision.action in _LOCAL_ACTIONS:
        return decision
    if not isinstance(admission, dict) or admission.get("status") not in {"admitted", "active"}:
        return replace(
            decision, allowed=False, state="unavailable",
            reason_code="SOURCE_USE_MODEL_ADMISSION_UNAVAILABLE",
        )
    purposes = admission.get("allowed_purposes")
    if isinstance(purposes, (list, tuple, set)) and purposes:
        if decision.purpose not in purposes and "*" not in purposes:
            return replace(
                decision, allowed=False, state="denied",
                reason_code="SOURCE_USE_MODEL_PURPOSE_NOT_ADMITTED",
            )
    expires = admission.get("expires_at")
    now = utc_now()
    if isinstance(expires, str) and not _future(expires, now):
        return replace(
            decision, allowed=False, state="expired",
            reason_code="SOURCE_USE_MODEL_ADMISSION_EXPIRED",
        )
    if decision.action in {"train", "evaluate"} and not admission.get("allow_training"):
        return replace(
            decision, allowed=False, state="pending_review",
            reason_code="SOURCE_USE_TRAINING_NOT_VERIFIED",
        )
    if decision.action == "external_process" and not admission.get("allow_external_processing"):
        return replace(
            decision, allowed=False, state="pending_review",
            reason_code="SOURCE_USE_EXTERNAL_PROCESSING_NOT_VERIFIED",
        )
    if decision.action in {"redistribute", "publish"} and not admission.get("allow_redistribution"):
        return replace(
            decision, allowed=False, state="pending_review",
            reason_code="SOURCE_USE_REDISTRIBUTION_NOT_VERIFIED",
        )
    if isinstance(expires, str) and (decision.valid_until is None or expires < decision.valid_until):
        return replace(decision, valid_until=expires)
    return decision


def _policy_scope_hash(context: dict, fields: tuple[str, ...], purpose: str, action: str,
                       recipient_kind: str, recipient_ref: str, scope_kind: str) -> str:
    return _digest({
        "object_kind": context["object_kind"], "object_id": context["object_id"],
        "object_revision": context["object_revision"], "scope_kind": scope_kind,
        "work_id": context["work_id"] if scope_kind == "work" else None,
        "client_id": context["client_id"] if scope_kind == "client" else None,
        "field_paths": list(fields), "purpose": purpose, "action": action,
        "recipient": {"kind": recipient_kind, "id": recipient_ref},
    })


def _write_decision_version(connection, row: dict, changed_by: str, now: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO source_use_decision_versions("
        "decision_id,workspace_id,version,snapshot_json,changed_by,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (row["decision_id"], row["workspace_id"], row["version"], _json(row), changed_by, now),
    )


def record_source_use_decision(
    connection, identity: dict, source_ref: dict, *, field_paths, purpose: str, action: str,
    recipient: dict, basis_status: str, decision_status: str = "allowed",
    allow_external_processing: bool = False, allow_redistribution: bool = False,
    allow_training: bool = False, retention_until: str | None = None,
    refresh_after: str | None = None, expires_at: str | None = None,
    basis_ref: dict | None = None, scope_kind: str = "workspace",
    work_id: str | None = None, now: str | None = None,
) -> SourceUseDecision:
    """Write a versioned SourceUse decision. Sensitive grants require a verified basis."""
    if basis_status not in _BASIS_STATES:
        raise SourceUseError("SOURCE_USE_BASIS_INVALID", 400, {"field": "basis_status"})
    if decision_status not in _DECISION_STATES:
        raise SourceUseError("SOURCE_USE_DECISION_STATUS_INVALID", 400, {"field": "decision_status"})
    if scope_kind not in {"workspace", "work", "client"}:
        raise SourceUseError("SOURCE_USE_SCOPE_INVALID", 400, {"field": "scope_kind"})
    if action not in _ACTIONS:
        raise SourceUseError("SOURCE_USE_ACTION_UNSUPPORTED", 400)
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise SourceUseError("SOURCE_USE_PURPOSE_INVALID", 400)
    fields = _field_paths(field_paths)
    recipient_kind, recipient_ref = _recipient(recipient)
    if action in _SENSITIVE_ACTIONS and (
        decision_status == "allowed"
        and (
            basis_status != "contract_verified"
            or (allow_training and action in {"train", "evaluate"} and basis_status != "contract_verified")
        )
        and action != "refresh"
    ):
        # Refresh of a user-chosen public monitor URL may be user_declared.
        # Train, redistribute, publish and external processing cannot.
        if action in {"train", "evaluate", "redistribute", "publish", "external_process"}:
            if basis_status != "contract_verified" and decision_status == "allowed":
                raise SourceUseError("SOURCE_USE_VERIFIED_BASIS_REQUIRED", 409, {"action": action})
    if any((allow_training, allow_redistribution, allow_external_processing)) and basis_status != "contract_verified":
        raise SourceUseError("SOURCE_USE_VERIFIED_BASIS_REQUIRED", 409, {"field": "basis_status"})
    if basis_status == "contract_verified" and not (isinstance(basis_ref, dict) and basis_ref):
        raise SourceUseError("SOURCE_USE_BASIS_REF_REQUIRED", 400, {"field": "basis_ref"})
    now_text = now or utc_now()
    expires_at = _time_text(expires_at, "expires_at")
    retention_until = _time_text(retention_until, "retention_until")
    refresh_after = _time_text(refresh_after, "refresh_after")
    context = _source_context(connection, identity, source_ref, work_id)
    if scope_kind == "work" and not context["work_id"]:
        raise SourceUseError("SOURCE_USE_WORK_SCOPE_REQUIRED", 400)
    if scope_kind == "client" and not context["client_id"]:
        raise SourceUseError("SOURCE_USE_CLIENT_SCOPE_REQUIRED", 400)
    stored_work = context["work_id"] if scope_kind == "work" else None
    stored_client = context["client_id"] if scope_kind == "client" else None
    policy_hash = _policy_scope_hash(context, fields, purpose, action, recipient_kind, recipient_ref, scope_kind)
    actor = context["actor_user_id"]
    existing = connection.execute(
        "SELECT * FROM source_use_decisions WHERE workspace_id=? AND owner_user_id=? AND policy_scope_sha256=?",
        (context["workspace_id"], context["source_owner_user_id"], policy_hash),
    ).fetchone()
    payload = {
        "workspace_id": context["workspace_id"],
        "owner_user_id": context["source_owner_user_id"],
        "object_kind": context["object_kind"],
        "object_id": context["object_id"],
        "object_revision": context["object_revision"],
        "scope_kind": scope_kind,
        "work_id": stored_work,
        "client_id": stored_client,
        "field_paths_json": _json(list(fields)),
        "purpose": purpose,
        "action": action,
        "recipient_kind": recipient_kind,
        "recipient_ref": recipient_ref,
        "basis_status": basis_status,
        "decision_status": decision_status,
        "allow_external_processing": 1 if allow_external_processing else 0,
        "allow_redistribution": 1 if allow_redistribution else 0,
        "allow_training": 1 if allow_training else 0,
        "retention_until": retention_until,
        "refresh_after": refresh_after,
        "expires_at": expires_at,
        "basis_ref_json": _json(basis_ref or {}),
        "policy_scope_sha256": policy_hash,
        "revoked_at": now_text if decision_status == "revoked" else None,
        "revoked_by": actor if decision_status == "revoked" else None,
        "updated_at": now_text,
    }
    if existing is None:
        decision_id = str(uuid.uuid4())
        row = {
            "decision_id": decision_id, **payload, "version": 1, "created_at": now_text,
        }
        connection.execute(
            "INSERT INTO source_use_decisions("
            "decision_id,workspace_id,owner_user_id,object_kind,object_id,object_revision,"
            "scope_kind,work_id,client_id,field_paths_json,purpose,action,recipient_kind,"
            "recipient_ref,basis_status,decision_status,allow_external_processing,"
            "allow_redistribution,allow_training,retention_until,refresh_after,expires_at,"
            "basis_ref_json,policy_scope_sha256,version,revoked_at,revoked_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["decision_id"], row["workspace_id"], row["owner_user_id"], row["object_kind"],
             row["object_id"], row["object_revision"], row["scope_kind"], row["work_id"],
             row["client_id"], row["field_paths_json"], row["purpose"], row["action"],
             row["recipient_kind"], row["recipient_ref"], row["basis_status"], row["decision_status"],
             row["allow_external_processing"], row["allow_redistribution"], row["allow_training"],
             row["retention_until"], row["refresh_after"], row["expires_at"], row["basis_ref_json"],
             row["policy_scope_sha256"], row["version"], row["revoked_at"], row["revoked_by"],
             row["created_at"], row["updated_at"]),
        )
        _write_decision_version(connection, row, actor, now_text)
    else:
        previous = _row_dict(existing)
        version = int(previous["version"]) + 1
        connection.execute(
            "UPDATE source_use_decisions SET object_revision=?,field_paths_json=?,basis_status=?,"
            "decision_status=?,allow_external_processing=?,allow_redistribution=?,allow_training=?,"
            "retention_until=?,refresh_after=?,expires_at=?,basis_ref_json=?,version=?,"
            "revoked_at=?,revoked_by=?,updated_at=? WHERE decision_id=? AND workspace_id=?",
            (payload["object_revision"], payload["field_paths_json"], payload["basis_status"],
             payload["decision_status"], payload["allow_external_processing"],
             payload["allow_redistribution"], payload["allow_training"], payload["retention_until"],
             payload["refresh_after"], payload["expires_at"], payload["basis_ref_json"], version,
             payload["revoked_at"], payload["revoked_by"], now_text,
             previous["decision_id"], previous["workspace_id"]),
        )
        decision_id = previous["decision_id"]
        row = {**previous, **payload, "decision_id": decision_id, "version": version}
        _write_decision_version(connection, row, actor, now_text)
    return resolve_source_use(
        connection, identity, source_ref, field_paths=fields, purpose=purpose,
        action=action, recipient=recipient, work_id=work_id, now=now_text,
    )


def revoke_source_use_decision(connection, identity: dict, decision_id: str, *,
                               now: str | None = None) -> SourceUseDecision:
    if not isinstance(decision_id, str) or not decision_id:
        raise SourceUseError("SOURCE_USE_DECISION_NOT_FOUND", 404)
    now_text = now or utc_now()
    workspace_id, user_id, role, _client = _actor_scope(connection, identity, None)
    row = connection.execute(
        "SELECT * FROM source_use_decisions WHERE decision_id=? AND workspace_id=?",
        (decision_id, workspace_id),
    ).fetchone()
    if row is None:
        raise SourceUseError("SOURCE_USE_DECISION_NOT_FOUND", 404)
    if row["owner_user_id"] != user_id and role not in {"owner", "admin"}:
        raise SourceUseError("SOURCE_USE_REVOKE_FORBIDDEN", 403)
    previous = _row_dict(row)
    version = int(previous["version"]) + 1
    connection.execute(
        "UPDATE source_use_decisions SET decision_status='revoked',revoked_at=?,revoked_by=?,"
        "version=?,updated_at=? WHERE decision_id=? AND workspace_id=? AND version=?",
        (now_text, user_id, version, now_text, decision_id, workspace_id, previous["version"]),
    )
    updated = connection.execute(
        "SELECT * FROM source_use_decisions WHERE decision_id=? AND workspace_id=?",
        (decision_id, workspace_id),
    ).fetchone()
    if updated is None or updated["version"] != version:
        raise SourceUseError("SOURCE_USE_VERSION_CONFLICT", 409)
    _write_decision_version(connection, _row_dict(updated), user_id, now_text)
    fields = tuple(_loads(updated["field_paths_json"], list, []))
    return _result(
        False, "revoked", updated["basis_status"],
        context={
            "workspace_id": updated["workspace_id"], "actor_user_id": user_id,
            "object_kind": updated["object_kind"], "object_id": updated["object_id"],
            "object_revision": updated["object_revision"], "work_id": updated["work_id"],
            "client_id": updated["client_id"],
        },
        fields=fields, purpose=updated["purpose"], action=updated["action"],
        recipient_kind=updated["recipient_kind"], recipient_ref=updated["recipient_ref"],
        decision_id=updated["decision_id"], version=updated["version"],
        policy_sha256=updated["policy_scope_sha256"], reason_code="SOURCE_USE_REVOKED",
        lineage_decision_ids=(updated["decision_id"],),
    )


def read_source_use_decision(connection, identity: dict, decision_id: str) -> dict:
    workspace_id, user_id, role, _client = _actor_scope(connection, identity, None)
    row = connection.execute(
        "SELECT * FROM source_use_decisions WHERE decision_id=? AND workspace_id=?",
        (decision_id, workspace_id),
    ).fetchone()
    if row is None:
        raise SourceUseError("SOURCE_USE_DECISION_NOT_FOUND", 404)
    if row["owner_user_id"] != user_id and role not in {"owner", "admin", "editor", "viewer"}:
        raise SourceUseError("SOURCE_USE_DECISION_NOT_FOUND", 404)
    return _decision_payload(_row_dict(row))


def list_source_use_decisions(connection, identity: dict, *, object_kind: str | None = None,
                              object_id: str | None = None, work_id: str | None = None,
                              limit: int = 100) -> list[dict]:
    workspace_id, user_id, role, _client = _actor_scope(connection, identity, work_id)
    if limit < 1 or limit > 200:
        raise SourceUseError("INVALID_INPUT", 400, {"field": "limit"})
    clauses = ["workspace_id=?"]
    params: list = [workspace_id]
    if role not in {"owner", "admin"}:
        clauses.append("owner_user_id=?")
        params.append(user_id)
    if object_kind:
        if not _IDENTIFIER.fullmatch(object_kind):
            raise SourceUseError("INVALID_INPUT", 400, {"field": "object_kind"})
        clauses.append("object_kind=?")
        params.append(object_kind)
    if object_id:
        clauses.append("object_id=?")
        params.append(object_id)
    if work_id:
        clauses.append("(work_id=? OR work_id IS NULL)")
        params.append(work_id)
    sql = (
        "SELECT * FROM source_use_decisions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY updated_at DESC, decision_id LIMIT ?"
    )
    params.append(limit)
    return [_decision_payload(_row_dict(row)) for row in connection.execute(sql, params).fetchall()]


def _decision_payload(row: dict) -> dict:
    return {
        "decision_id": row["decision_id"],
        "version": row["version"],
        "workspace_id": row["workspace_id"],
        "owner_user_id": row["owner_user_id"],
        "object_kind": row["object_kind"],
        "object_id": row["object_id"],
        "object_revision": row["object_revision"],
        "scope_kind": row["scope_kind"],
        "work_id": row["work_id"],
        "client_id": row["client_id"],
        "field_paths": _loads(row["field_paths_json"], list, []),
        "purpose": row["purpose"],
        "action": row["action"],
        "recipient": {"kind": row["recipient_kind"], "id": row["recipient_ref"]},
        "basis_status": row["basis_status"],
        "decision_status": row["decision_status"],
        "allow_external_processing": bool(row["allow_external_processing"]),
        "allow_redistribution": bool(row["allow_redistribution"]),
        "allow_training": bool(row["allow_training"]),
        "retention_until": row["retention_until"],
        "refresh_after": row["refresh_after"],
        "expires_at": row["expires_at"],
        "basis_ref": _loads(row["basis_ref_json"], dict, {}),
        "policy_sha256": row["policy_scope_sha256"],
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def authorize_monitor_source_refresh(connection, identity: dict, source_ref: dict, *,
                                     work_id: str | None = None, now: str | None = None,
                                     field_paths=("content",)) -> SourceUseDecision:
    """Write-capable entry used immediately before a monitor network check.

    A later Material grant is never treated as URL refresh authorization. If the
    actor owns an active monitor source and no current decision exists, a
    user_declared local refresh grant is recorded, then resolved.
    """
    now_text = now or utc_now()
    recipient = {"kind": "local", "id": identity["workspace"]["id"]}
    decision = resolve_source_use(
        connection, identity, source_ref, field_paths=field_paths,
        purpose="monitoring", action="refresh", recipient=recipient,
        work_id=work_id, now=now_text,
    )
    if decision.allowed or decision.reason_code not in {
        "SOURCE_USE_DECISION_REQUIRED", "SOURCE_USE_SOURCE_REVISION_STALE",
    }:
        return decision
    kind = source_ref.get("kind") if isinstance(source_ref, dict) else None
    if kind not in {"monitor_source", "url", "public_url"}:
        return decision
    context = _source_context(connection, identity, source_ref, work_id)
    if context["source_owner_user_id"] != context["actor_user_id"]:
        return decision
    if decision.reason_code == "SOURCE_USE_SOURCE_REVISION_STALE":
        # URL identity changed under an allowed grant. Record a new version.
        # Revoked, denied and restored grants must not be auto-regranted.
        if not decision.decision_id:
            return decision
        existing = connection.execute(
            "SELECT decision_status FROM source_use_decisions WHERE decision_id=? AND workspace_id=?",
            (decision.decision_id, context["workspace_id"]),
        ).fetchone()
        if existing is None or existing["decision_status"] != "allowed":
            return decision
    return record_source_use_decision(
        connection, identity, source_ref, field_paths=field_paths,
        purpose="monitoring", action="refresh", recipient=recipient,
        basis_status="user_declared", decision_status="allowed",
        work_id=work_id, now=now_text, scope_kind="work" if context["work_id"] else "workspace",
    )


class SourceUseService:
    """Durable SourceUse writes and the registered public resolver."""

    def __init__(self, store):
        self.store = store

    def resolve(self, identity: dict, source_ref: dict, **kwargs) -> SourceUseDecision:
        with self.store.transaction(immediate=False) as connection:
            return resolve_source_use(connection, identity, source_ref, **kwargs)

    def record(self, identity: dict, source_ref: dict, **kwargs) -> SourceUseDecision:
        with self.store.transaction() as connection:
            return record_source_use_decision(connection, identity, source_ref, **kwargs)

    def revoke(self, identity: dict, decision_id: str) -> SourceUseDecision:
        with self.store.transaction() as connection:
            return revoke_source_use_decision(connection, identity, decision_id)

    def get(self, identity: dict, decision_id: str) -> dict:
        with self.store.transaction(immediate=False) as connection:
            return read_source_use_decision(connection, identity, decision_id)

    def list(self, identity: dict, **kwargs) -> list[dict]:
        with self.store.transaction(immediate=False) as connection:
            return list_source_use_decisions(connection, identity, **kwargs)

    def authorize_monitor_refresh(self, identity: dict, source_ref: dict, **kwargs) -> SourceUseDecision:
        with self.store.transaction() as connection:
            return authorize_monitor_source_refresh(connection, identity, source_ref, **kwargs)


def _as_api_error(exc: SourceUseError):
    messages = {
        "UNAUTHENTICATED": "Authentication is required",
        "ACCESS_REVOKED": "Access to this workspace is no longer valid",
        "NOT_FOUND": "The requested object was not found",
        "INVALID_INPUT": "The request contains an invalid field",
        "SOURCE_USE_DECISION_NOT_FOUND": "The SourceUse decision was not found",
        "SOURCE_USE_REVOKE_FORBIDDEN": "This actor cannot revoke the SourceUse decision",
        "SOURCE_USE_VERIFIED_BASIS_REQUIRED": "A verified contract basis is required for this grant",
        "SOURCE_USE_BASIS_REF_REQUIRED": "A basis reference is required for a verified contract",
        "SOURCE_USE_VERSION_CONFLICT": "The SourceUse decision changed before this write could be confirmed",
    }
    message = messages.get(exc.code, "The SourceUse request could not be completed")
    return _api.ApiError(exc.code, message, exc.status, exc.details)


def register_source_use_routes(app: Flask) -> None:
    if app.extensions.get("oevira.source_use.registered"):
        return
    store = app.extensions.get("oevira.store")
    if store is None:
        raise RuntimeError("SourceUse routes require the existing OEVIRA Store")
    service = app.extensions.get("oevira.source_use")
    if service is None:
        service = SourceUseService(store)
        app.extensions["oevira.source_use"] = service

    def _resolve_body_args(data: dict) -> dict:
        source_ref = data.get("source_ref")
        if not isinstance(source_ref, dict):
            raise _api.ApiError("INVALID_INPUT", "source_ref must be an object", 400, {"field": "source_ref"})
        field_paths = data.get("field_paths")
        purpose = data.get("purpose")
        action = data.get("action")
        recipient = data.get("recipient")
        work_id = data.get("work_id")
        if work_id is not None and not isinstance(work_id, str):
            raise _api.ApiError("INVALID_INPUT", "work_id must be a string", 400, {"field": "work_id"})
        return {
            "source_ref": source_ref, "field_paths": field_paths, "purpose": purpose,
            "action": action, "recipient": recipient, "work_id": work_id,
        }

    @app.post("/api/source-use/resolve", endpoint="source_use_resolve")
    def resolve_route():
        data = _api._body()
        args = _resolve_body_args(data)
        try:
            decision = service.resolve(g.identity, **args)
        except SourceUseError as exc:
            raise _as_api_error(exc) from exc
        return _api._success(decision.to_dict())

    @app.post("/api/source-use/decisions", endpoint="source_use_record")
    def record_route():
        _api._require_write()
        data = _api._body()
        args = _resolve_body_args(data)
        basis_status = data.get("basis_status", "user_declared")
        decision_status = data.get("decision_status", "allowed")
        scope_kind = data.get("scope_kind", "workspace")
        try:
            decision = service.record(
                g.identity, args["source_ref"], field_paths=args["field_paths"],
                purpose=args["purpose"], action=args["action"], recipient=args["recipient"],
                work_id=args["work_id"], basis_status=basis_status,
                decision_status=decision_status,
                allow_external_processing=bool(data.get("allow_external_processing")),
                allow_redistribution=bool(data.get("allow_redistribution")),
                allow_training=bool(data.get("allow_training")),
                retention_until=data.get("retention_until"),
                refresh_after=data.get("refresh_after"),
                expires_at=data.get("expires_at"),
                basis_ref=data.get("basis_ref") if isinstance(data.get("basis_ref"), dict) else None,
                scope_kind=scope_kind,
            )
        except SourceUseError as exc:
            raise _as_api_error(exc) from exc
        return _api._success(decision.to_dict(), 201)

    @app.get("/api/source-use/decisions/<decision_id>", endpoint="source_use_get")
    def get_route(decision_id: str):
        try:
            return _api._success(service.get(g.identity, decision_id))
        except SourceUseError as exc:
            raise _as_api_error(exc) from exc

    @app.get("/api/source-use/decisions", endpoint="source_use_list")
    def list_route():
        allowed = {"object_kind", "object_id", "work_id", "limit"}
        if set(request.args) - allowed:
            raise _api.ApiError("INVALID_INPUT", "Decision list contains an unsupported parameter", 400)
        limit_text = request.args.get("limit")
        try:
            limit = int(limit_text) if limit_text is not None else 100
        except (TypeError, ValueError):
            raise _api.ApiError("INVALID_INPUT", "limit must be an integer", 400, {"field": "limit"}) from None
        try:
            return _api._success({
                "decisions": service.list(
                    g.identity, object_kind=request.args.get("object_kind"),
                    object_id=request.args.get("object_id"), work_id=request.args.get("work_id"),
                    limit=limit,
                )
            })
        except SourceUseError as exc:
            raise _as_api_error(exc) from exc

    @app.post("/api/source-use/decisions/<decision_id>/revoke", endpoint="source_use_revoke")
    def revoke_route(decision_id: str):
        _api._require_write()
        try:
            decision = service.revoke(g.identity, decision_id)
        except SourceUseError as exc:
            raise _as_api_error(exc) from exc
        return _api._success(decision.to_dict())

    app.extensions["oevira.source_use.registered"] = True
