"""User-scoped workspace backup and isolated restoration over the current Store.

The package is a filtered record snapshot plus verified files, never a copy of
the server database. Imported SQLite bytes are read-only data; this module does
not execute SQL, scripts, extensions, or caller-provided paths from a package.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from flask import g, request, send_file

from .api import ApiError, _body, _idempotency_key, _request_fingerprint, _success
from .auth import issue_session_in_transaction, utc_now
from .collaboration import require_commission_access, require_work_access, require_workspace_access


PACKAGE_FORMAT = "oevira.workspace-backup"
PACKAGE_VERSION = 1
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 100_000
MAX_PACKAGE_ROW_BYTES = 8 * 1024 * 1024
MAX_PROGRESS_LABEL = 200
PACKAGE_LIFETIME = timedelta(hours=24)
BACKUP_PROTECTION_LIFETIME = timedelta(hours=2)
_PACKAGE_FILE_RE = re.compile(r"^files/[a-f0-9]{64}$")
_PACKAGE_DB_MEMBER = "records/workspace.sqlite3"


WORKSPACE_DATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_data_operations (
    operation_id TEXT PRIMARY KEY,
    source_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    target_workspace_id TEXT,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL CHECK(kind IN ('backup', 'restore')),
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    job_id TEXT,
    package_export_id TEXT,
    package_sha256 TEXT,
    package_size_bytes INTEGER,
    target_name TEXT,
    resolutions_json TEXT NOT NULL DEFAULT '{}',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    progress_completed INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    progress_label TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    protection_generation INTEGER NOT NULL DEFAULT 0,
    protection_state TEXT NOT NULL DEFAULT 'released',
    protection_expires_at TEXT,
    protected_files_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_data_operations_idempotency
    ON workspace_data_operations(source_workspace_id, owner_user_id, kind, idempotency_key);
CREATE INDEX IF NOT EXISTS workspace_data_operations_source
    ON workspace_data_operations(source_workspace_id, owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS workspace_data_operations_target
    ON workspace_data_operations(target_workspace_id, owner_user_id, updated_at DESC);
"""


# These are account, device, authentication, migration, or one-time execution
# records. The package keeps user work while requiring current login, member,
# source-use, and external-effect decisions on the destination.
_EXCLUDED_TABLE_REASONS = {
    "account_challenges": "Account credentials and verification records are not workspace data.",
    "account_deletion_file_cleanup": "Account deletion cleanup is not restored.",
    "account_deletions": "Account lifecycle and deletion authorization are not restored.",
    "account_emails": "Account identity credentials are not restored.",
    "account_events": "Account security events are not workspace content.",
    "account_exports": "Temporary account export delivery records are recreated by the destination.",
    "account_external_identities": "External identity-provider credentials are not restored.",
    "account_local_merge_choices": "One-time local/account merge choices are not restored.",
    "account_login_challenges": "Authentication challenges are not restored.",
    "account_login_failures": "Account security counters are not workspace content.",
    "account_membership_events": "Membership authorization history is re-evaluated on the destination.",
    "account_oauth_transactions": "OAuth state and transactions are not restored.",
    "account_password_credentials": "Passwords and password verifiers are never backed up.",
    "account_preferences": "Account-wide preferences remain with the account, not the workspace.",
    "account_workspace_invitations": "Old invitations and invitation tokens are not reactivated.",
    "bootstrap_capabilities": "One-time local bootstrap capabilities are never restored.",
    "community_follows": "Cross-workspace community follows are not workspace data.",
    "community_items": "Cross-workspace community saves are not workspace data.",
    "idempotency_records": "Old request replays must not be revived by restore.",
    "intelligence_model_admissions": "Model and data-use admissions must be reviewed on the destination.",
    "legacy_import_files": "Private migration source copies are not included in ordinary user backups.",
    "legacy_import_items": "Developer migration mappings are not workspace product data.",
    "media_upload_chunks": "Uncommitted upload chunks are temporary transfer state and are not resumed by restore.",
    "media_uploads": "Uncommitted uploads are not resumable from a backup; finalized Materials are preserved.",
    "local_identity": "The source device's active workspace selection is not restored.",
    "memberships": "Existing workspace members are not automatically re-granted access.",
    "workspace_data_operations": "Backup/restore jobs are not recursively packaged.",
}

_EXCLUDED_TABLES = set(_EXCLUDED_TABLE_REASONS)
_OWNER_ONLY_TABLES = {
    "browser_sessions", "browser_session_operations", "browser_bookmarks", "browser_history", "acquisition_query_plans",
    "intelligence_memories",
    "intelligence_conversations", "intelligence_model_connections", "intelligence_model_events",
    "intelligence_github_connections", "intelligence_mcp_connections", "intelligence_skill_candidates", "intelligence_skill_events",
    "intelligence_skills", "intelligence_turns", "monitor_rules",
}
_FILE_COLUMNS = {
    "materials": {"relative_path"},
    "exports": {"relative_path"},
    "media_renders": {"relative_path", "staging_path"},
    "media_uploads": {"stage_relative_path", "original_relative_path"},
    "delivery_batches": {"relative_path"},
    "media_interchanges": {"relative_path", "staging_path"},
}
_TEXT_JSON_COLUMNS = {
    name for name in (
        "payload_json", "brief_json", "scope_json", "profile_json", "source_json", "source_metadata_json",
        "citations_json", "input_snapshot_json", "snapshot_json", "details_json", "review_evidence_json",
        "legacy_source_json", "metric_values_json", "source_refs_json", "evidence_refs_json", "outputs_json",
        "context_json", "material_ids_json", "skill_snapshots_json", "tool_results_json", "request_json",
        "manifest_json", "preview_json", "resolutions_json", "files_json", "file_json", "license_json", "anchor_json",
        "resolution_json", "scope_lineage", "source_snapshot", "content_json", "scope_source_references",
        "scope_json", "outputs_json", "choices_json", "completed_steps_json", "payload_json",
    )
}
_URL_COLUMNS = {"url", "source_url", "current_url", "base_url"}
_SECRET_KEY_PARTS = (
    "token", "secret", "password", "cookie", "credential", "authorization", "bearer",
    "privatekey", "clientsecret", "codeverifier", "ciphertext", "noncehash", "apikey",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(xsec_token|access_token|refresh_token|api[_-]?key|client_secret)=([^&\s]+)"),
)
_SESSION_REFERENCE_TABLES = {"browser_sessions", "intelligence_turns"}
_SESSION_REFERENCE_COLUMNS = {
    "browser_sessions": ("authorized_by_session_id",),
    "intelligence_turns": ("session_id",),
}
_ALLOWED_GLOBAL_CHILD_TABLES = {
    "work_versions", "work_commissions", "commission_members", "commission_versions",
    "artifact_comment_versions", "business_export_views", "media_upload_chunks",
    "media_transcription_chunks",
    "publication_versions", "publication_reviews", "publication_releases",
    "publication_appeals", "publication_reports", "release_comments",
}


class WorkspaceDataError(Exception):
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


def install_workspace_data_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + WORKSPACE_DATA_SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()
    with store.transaction() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(workspace_data_operations)")}
        for name, declaration in (
            ("protection_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("protection_state", "TEXT NOT NULL DEFAULT 'released'"),
            ("protection_expires_at", "TEXT"),
            ("protected_files_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE workspace_data_operations ADD COLUMN {name} {declaration}")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _safe_object_path(store, value: object, workspace_id: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkspaceDataError("WORKSPACE_FILE_REFERENCE_INVALID")
    if not isinstance(workspace_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", workspace_id):
        raise WorkspaceDataError("WORKSPACE_FILE_REFERENCE_INVALID")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkspaceDataError("WORKSPACE_FILE_REFERENCE_INVALID")
    if len(relative.parts) < 3 or relative.parts[0] != "objects" or relative.parts[1] != workspace_id:
        raise WorkspaceDataError("WORKSPACE_FILE_REFERENCE_INVALID")
    path = store.root.joinpath(*relative.parts)
    expected_root = store.objects / workspace_id
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != store.root and parent != store.objects.parent):
        raise WorkspaceDataError("WORKSPACE_FILE_SYMLINK_BLOCKED")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(expected_root.resolve()) or not resolved.is_file():
        raise WorkspaceDataError("WORKSPACE_FILE_UNAVAILABLE")
    return resolved


def _safe_account_export_path(store, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or "\\" in relative_path or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
    if not relative.parts or relative.parts[0] != "account-exports":
        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
    export_root = store.root / "account-exports"
    if export_root.is_symlink():
        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
    candidate = store.root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to((store.root / "account-exports").resolve()) or not resolved.is_file():
        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
    return resolved


def _safe_url(value: str) -> tuple[str, bool]:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "", True
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return "", True
    query = []
    removed = False
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if any(part in normalized for part in _SECRET_KEY_PARTS):
            removed = True
            continue
        query.append((key, item))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")), removed or bool(parsed.fragment)


def _safe_text(value: str) -> tuple[str, int]:
    result = value
    redactions = 0
    for pattern in _SECRET_TEXT_PATTERNS:
        result, count = pattern.subn("[credential omitted from workspace backup]", result)
        redactions += count
    return result, redactions


def _safe_json(value: object, *, field_name: str | None = None) -> tuple[object, int]:
    redactions = 0
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                redactions += 1
                continue
            safe, count = _safe_json(item, field_name=str(key))
            cleaned[key] = safe
            redactions += count
        return cleaned, redactions
    if isinstance(value, list):
        cleaned = []
        for item in value:
            safe, count = _safe_json(item, field_name=field_name)
            cleaned.append(safe)
            redactions += count
        return cleaned, redactions
    if isinstance(value, str):
        if field_name and (
            field_name.casefold() in {"url", "source_url", "current_url"}
            or field_name.casefold().endswith(("_url", "_urls"))
        ):
            safe, changed = _safe_url(value)
            return safe, redactions + int(changed)
        safe, count = _safe_text(value)
        return safe, redactions + count
    if isinstance(value, bytes):
        return {"$binary_base64": __import__("base64").b64encode(value).decode("ascii")}, redactions
    return value, redactions


def _without_browser_query_preview(row: dict, *, now: str) -> bool:
    """Keep Browser path lineage while dropping its volatile page-result body."""
    if row.get("path_kind") != "browser_page":
        return False
    try:
        prior_result = json.loads(row.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        prior_result = {}
    try:
        selected_ref = json.loads(row.get("source_ref_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        selected_ref = {}
    try:
        request = json.loads(row.get("request_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        request = {}
    if not isinstance(prior_result, dict):
        prior_result = {}
    if not isinstance(selected_ref, dict):
        selected_ref = {}
    if not isinstance(request, dict):
        request = {}
    result_ref = prior_result.get("source_ref")
    if not isinstance(result_ref, dict):
        result_ref = {}
    target = request.get("target") if isinstance(request.get("target"), dict) else {}
    source_ref = {
        key: value for key, value in {
            **selected_ref,
            **result_ref,
            "work_id": result_ref.get("work_id") or request.get("query_work_id") or request.get("work_id"),
        }.items()
        if key in {"kind", "session_id", "page_id", "page_revision", "snapshot_sha256", "work_id", "captured_at"}
        and value is not None
    }
    for key in ("session_id", "page_id", "page_revision"):
        if key not in source_ref and target.get(key) is not None:
            source_ref[key] = target[key]

    original_status = row.get("status")
    expires_at = row.get("preview_expires_at")
    expired = isinstance(expires_at, str) and expires_at <= now
    reason = "BROWSER_PREVIEW_EXPIRED" if expired else "BROWSER_PREVIEW_NOT_RESTORED"
    if original_status in {"not_started", "running", "writing", "complete", "completed", "partial"}:
        row["status"] = "unavailable"
        row["stop_reason"] = reason
    try:
        coverage = json.loads(row.get("coverage_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        coverage = {}
    if not isinstance(coverage, dict):
        coverage = {}
    coverage["browser_preview_state"] = "expired_not_restored" if expired else "not_restored"
    row["coverage_json"] = _json(coverage)
    row["result_json"] = _json({
        "status": row.get("status", "unavailable"),
        "results": [],
        "coverage": {
            "status": "expired" if expired else "not_restored",
            "snapshot_sha256": source_ref.get("snapshot_sha256"),
        },
        "source_ref": source_ref,
        "source_use_state": "NOT_EVALUATED",
    })
    return True


def _file_hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _table_names(connection) -> list[str]:
    return [row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def _table_schema(connection, table: str) -> dict:
    info = connection.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
    fks = connection.execute(f"PRAGMA foreign_key_list({_identifier(table)})").fetchall()
    return {
        "columns": [row["name"] for row in info],
        "types": {row["name"]: row["type"] for row in info},
        "not_null": {row["name"]: bool(row["notnull"]) for row in info},
        "primary_key": [row["name"] for row in sorted(info, key=lambda item: item["pk"]) if row["pk"]],
        "foreign_keys": [dict(row) for row in fks],
    }


def _expiry_at(now: str, lifetime: timedelta) -> str:
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (current + lifetime).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _progress(store, operation_id: str, claim: dict, *, completed: int, total: int, label: str) -> bool:
    from .worker import claim_can_commit

    now = utc_now()
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT j.status,j.cancel_requested,j.lease_generation,o.kind,o.protection_state,o.protection_generation FROM jobs j "
            "JOIN workspace_data_operations o ON o.operation_id=? "
            "WHERE j.id=? AND j.workspace_id=?",
            (operation_id, claim["id"], claim["workspace_id"]),
        ).fetchone()
        if row is None or not claim_can_commit(connection, claim, allow_stopping=True):
            return False
        if row["status"] == "cancel_requested" or row["cancel_requested"]:
            connection.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? "
                "WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested')",
                (now, claim["id"], claim["generation"]),
            )
            connection.execute(
                "UPDATE workspace_data_operations SET state='cancelled',updated_at=? WHERE operation_id=?",
                (now, operation_id),
            )
            return False
        if row["status"] != "running" or not claim_can_commit(connection, claim):
            return False
        bounded_label = label[:MAX_PROGRESS_LABEL]
        connection.execute(
            "UPDATE jobs SET progress_completed=?,progress_total=?,progress_label=?,updated_at=? "
            "WHERE id=? AND lease_generation=? AND status='running'",
            (max(0, int(completed)), max(0, int(total)), bounded_label, now, claim["id"], claim["generation"]),
        )
        connection.execute(
            "UPDATE workspace_data_operations SET progress_completed=?,progress_total=?,progress_label=?,updated_at=? "
            "WHERE operation_id=?",
            (max(0, int(completed)), max(0, int(total)), bounded_label, now, operation_id),
        )
        if (row["kind"] == "backup" and row["protection_state"] in {"pending", "active"}
                and row["protection_generation"] == claim["generation"]):
            connection.execute(
                "UPDATE workspace_data_operations SET protection_expires_at=? WHERE operation_id=? "
                "AND protection_state IN ('pending','active') AND protection_generation=?",
                (_expiry_at(now, BACKUP_PROTECTION_LIFETIME), operation_id, claim["generation"]),
            )
        return True


class WorkspaceDataService:
    def __init__(self, store):
        self.store = store

    def _identity(self, identity: dict) -> tuple[str, str]:
        user_id = identity.get("user", {}).get("id")
        workspace_id = identity.get("workspace", {}).get("id")
        if not isinstance(user_id, str) or not user_id or not isinstance(workspace_id, str) or not workspace_id:
            raise ApiError("UNAUTHENTICATED", "An active workspace session is required", 401)
        return user_id, workspace_id

    def _require_workspace_read(self, connection, identity: dict, workspace_id: str) -> dict:
        scoped = {**identity, "workspace": {**identity.get("workspace", {}), "id": workspace_id}}
        require_workspace_access(connection, scoped, write=False)
        membership = connection.execute(
            "SELECT role, active FROM memberships WHERE workspace_id=? AND user_id=?",
            (workspace_id, identity["user"]["id"]),
        ).fetchone()
        if membership is None or not membership["active"]:
            raise ApiError("NOT_FOUND", "The requested workspace is unavailable", 404)
        return dict(membership)

    @staticmethod
    def _operation_data(row, package_available: bool = False) -> dict:
        try:
            preview = json.loads(row["preview_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            preview = {}
        try:
            manifest = json.loads(row["manifest_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            manifest = {}
        return {
            "id": row["operation_id"],
            "kind": row["kind"],
            "state": row["state"],
            "phase": row["phase"],
            "source_workspace_id": row["source_workspace_id"],
            "target_workspace_id": row["target_workspace_id"],
            "target_name": row["target_name"],
            "package_sha256": row["package_sha256"],
            "package_size_bytes": row["package_size_bytes"],
            "package_expires_at": manifest.get("package_expires_at"),
            "progress": {
                "completed": row["progress_completed"],
                "total": row["progress_total"],
                "label": row["progress_label"],
            },
            "error_code": row["error_code"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "preview": preview,
            "summary": {
                "record_counts": manifest.get("record_counts", {}),
                "file_count": manifest.get("file_count", 0),
                "file_bytes": manifest.get("file_bytes", 0),
                "coverage": manifest.get("coverage", {}),
                "protected_or_unavailable": manifest.get("protected_or_unavailable", {}),
            },
            "package_available": bool(package_available),
            "download_url": f"/api/workspace-data/operations/{row['operation_id']}/package"
            if package_available else None,
        }

    def _resume_operation(self, connection, actor: dict, job, resume_context: dict,
                          *, expected_kind: str, expected_phases: set[str]):
        if resume_context:
            raise ApiError("INVALID_INPUT", "Workspace backup and restore continue with their saved operation", 400)
        try:
            payload = json.loads(job["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved workspace operation needs review", 409) from exc
        allowed_payload = {"operation_id", "phase"}
        if expected_kind == "restore":
            allowed_payload.update({"target_name", "resolutions"})
        if (not isinstance(payload, dict) or not {"operation_id", "phase"}.issubset(payload)
                or set(payload) - allowed_payload
                or not isinstance(payload.get("operation_id"), str)
                or payload.get("phase") not in expected_phases):
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved workspace operation does not match its job", 409)
        operation = connection.execute(
            "SELECT * FROM workspace_data_operations WHERE operation_id=? AND owner_user_id=? "
            "AND source_workspace_id=? AND job_id=? AND kind=?",
            (payload["operation_id"], actor["user"]["id"], job["workspace_id"], job["id"], expected_kind),
        ).fetchone()
        if operation is None or operation["phase"] != payload["phase"]:
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved workspace operation changed; reopen its result", 409,
                           {"operation_id": payload["operation_id"]})
        if expected_kind == "restore" and payload["phase"] == "execute":
            try:
                saved_resolutions = json.loads(operation["resolutions_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ApiError("WORKSPACE_RESTORE_RESOLUTION_INVALID", "The saved restore choices need review", 409) from exc
            if (payload.get("target_name") != operation["target_name"]
                    or payload.get("resolutions") != saved_resolutions):
                raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved restore destination or choices changed", 409,
                               {"operation_id": payload["operation_id"]})
        return payload, operation

    def validate_backup_resume(self, connection, actor: dict, job, resume_context: dict) -> None:
        payload, operation = self._resume_operation(
            connection, actor, job, resume_context, expected_kind="backup", expected_phases={"backup"},
        )
        if not operation["package_export_id"]:
            raise ApiError("WORKSPACE_BACKUP_STATE_UNAVAILABLE", "The backup package identity is unavailable", 409)
        if operation["state"] in {"backup_ready", "complete"}:
            raise ApiError("WORKSPACE_BACKUP_RESULT_ALREADY_COMMITTED", "Reopen the completed backup instead of replaying it", 409)
        if connection.execute(
            "SELECT 1 FROM account_exports WHERE id=? AND user_id=?",
            (operation["package_export_id"], operation["owner_user_id"]),
        ).fetchone() is not None:
            raise ApiError("WORKSPACE_BACKUP_RESULT_ALREADY_COMMITTED", "Reopen the saved backup package instead of replaying it", 409)

    def validate_restore_resume(self, connection, actor: dict, job, resume_context: dict) -> None:
        payload, operation = self._resume_operation(
            connection, actor, job, resume_context,
            expected_kind="restore", expected_phases={"preview", "execute"},
        )
        now = utc_now()
        package = connection.execute(
            "SELECT user_id,expires_at,revoked_at,sha256,size_bytes FROM account_exports WHERE id=?",
            (operation["package_export_id"],),
        ).fetchone()
        if (package is None or package["user_id"] != operation["owner_user_id"]
                or package["revoked_at"] is not None or package["expires_at"] <= now):
            raise ApiError("WORKSPACE_PACKAGE_EXPIRED", "The saved restore package is no longer available", 410)
        if (package["sha256"] != operation["package_sha256"]
                or package["size_bytes"] != operation["package_size_bytes"]):
            raise ApiError("WORKSPACE_PACKAGE_INTEGRITY_ERROR", "The saved restore package metadata changed", 409)
        if payload["phase"] == "execute":
            target_workspace_id = operation["target_workspace_id"]
            if not isinstance(target_workspace_id, str) or not target_workspace_id:
                raise ApiError("WORKSPACE_RESTORE_TARGET_INVALID", "The saved restore destination is unavailable", 409)
            if connection.execute("SELECT 1 FROM workspaces WHERE id=?", (target_workspace_id,)).fetchone():
                raise ApiError("WORKSPACE_RESTORE_ALREADY_COMMITTED", "Reopen the restored workspace instead of replaying it", 409)

    def create_backup(self, identity: dict, idempotency_key: str) -> dict:
        user_id, workspace_id = self._identity(identity)
        fingerprint = _request_fingerprint({"workspace_id": workspace_id, "kind": "backup"})
        now = utc_now()
        operation_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            self._require_workspace_read(connection, identity, workspace_id)
            prior = connection.execute(
                "SELECT * FROM workspace_data_operations WHERE source_workspace_id=? AND owner_user_id=? "
                "AND kind='backup' AND idempotency_key=?",
                (workspace_id, user_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different workspace backup", 409)
                job = None if not prior["job_id"] else connection.execute(
                    "SELECT id,status FROM jobs WHERE id=? AND workspace_id=?",
                    (prior["job_id"], workspace_id),
                ).fetchone()
                return {"operation": self._operation_data(
                            prior, prior["state"] == "backup_ready" and bool(prior["package_export_id"])
                        ),
                        "job": None if job is None else dict(job), "replayed": True}
            job_id, job_fingerprint = self._insert_job(
                connection,
                workspace_id,
                user_id,
                "workspace_backup",
                f"workspace-backup:{idempotency_key}",
                {"operation_id": operation_id, "phase": "backup"},
                "Preparing workspace backup",
                now,
            )
            connection.execute(
                "INSERT INTO workspace_data_operations(operation_id,source_workspace_id,owner_user_id,kind,state,phase,"
                "idempotency_key,request_hash,job_id,package_export_id,created_at,updated_at) "
                "VALUES(?,?,?,'backup','queued','backup',?,?,?,?,?,?)",
                (operation_id, workspace_id, user_id, idempotency_key, fingerprint, job_id,
                 str(uuid.uuid4()), now, now),
            )
        return {
            "operation": self._operation_data(self.store.row(
                "SELECT * FROM workspace_data_operations WHERE operation_id=?", (operation_id,)
            )),
            "job": {"id": job_id, "status": "queued"},
            "replayed": False,
        }

    def _insert_job(self, connection, workspace_id: str, user_id: str, kind: str,
                    idempotency_key: str, payload: dict, progress_label: str, now: str) -> tuple[str, str]:
        request_hash = _request_fingerprint(payload)
        row = connection.execute(
            "SELECT id,request_hash FROM jobs WHERE workspace_id=? AND kind=? AND idempotency_key=?",
            (workspace_id, kind, idempotency_key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This workspace data request key was reused with different input", 409)
            return row["id"], request_hash
        job_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,"
            "progress_completed,progress_total,progress_label,created_at,updated_at) "
            "VALUES(?,?,?,NULL,?,'queued',?,?,?,0,0,?,?,?)",
            (job_id, workspace_id, user_id, kind, _json(payload), request_hash, idempotency_key,
             progress_label[:MAX_PROGRESS_LABEL], now, now),
        )
        return job_id, request_hash

    def upload_restore(self, identity: dict, idempotency_key: str) -> dict:
        user_id, workspace_id = self._identity(identity)
        connection = self.store.connect()
        try:
            self._require_workspace_read(connection, identity, workspace_id)
        finally:
            connection.close()

        try:
            request.max_content_length = MAX_PACKAGE_BYTES + 1024 * 1024
        except (AttributeError, TypeError):
            pass
        package = request.files.get("package")
        if package is None or not package.filename:
            raise ApiError("WORKSPACE_PACKAGE_REQUIRED", "Choose a workspace backup package to restore", 400)
        package_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        export_relative = f"account-exports/{package_id}.zip"
        export_root = self.store.root / "account-exports"
        if export_root.is_symlink():
            raise ApiError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE", "The workspace package storage is unavailable", 503)
        export_root.mkdir(parents=True, exist_ok=True)
        if export_root.is_symlink() or not export_root.resolve().is_relative_to(self.store.root):
            raise ApiError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE", "The workspace package storage is unavailable", 503)
        target = self.store.root / export_relative
        temporary = target.with_name(f".{package_id}.upload.partial")
        digest = hashlib.sha256()
        size = 0
        moved = False
        try:
            with temporary.open("xb") as output:
                while True:
                    block = package.stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_PACKAGE_BYTES:
                        raise ApiError("WORKSPACE_PACKAGE_TOO_LARGE", "The workspace package exceeds the allowed size", 413)
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if size < 4 or temporary.read_bytes()[:4] != b"PK\x03\x04":
                raise ApiError("WORKSPACE_PACKAGE_INVALID", "The selected file is not a workspace backup ZIP", 400)
            file_hash = digest.hexdigest()
            request_payload = {"workspace_id": workspace_id, "package_sha256": file_hash, "package_size_bytes": size}
            fingerprint = _request_fingerprint(request_payload)
            now = utc_now()
            expires = _expiry_at(now, PACKAGE_LIFETIME)
            package_name = PurePosixPath(package.filename).name[:255]
            with self.store.transaction() as connection:
                self._require_workspace_read(connection, identity, workspace_id)
                prior = connection.execute(
                    "SELECT * FROM workspace_data_operations WHERE source_workspace_id=? AND owner_user_id=? "
                    "AND kind='restore' AND idempotency_key=?",
                    (workspace_id, user_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["request_hash"] != fingerprint:
                        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different workspace package", 409)
                    job = None if not prior["job_id"] else connection.execute(
                        "SELECT id,status FROM jobs WHERE id=? AND workspace_id=?", (prior["job_id"], workspace_id)
                    ).fetchone()
                    return {"operation": self._operation_data(prior, True),
                            "job": None if job is None else dict(job), "replayed": True}
                job_id, _ = self._insert_job(
                    connection, workspace_id, user_id, "workspace_restore",
                    f"workspace-restore-preview:{idempotency_key}",
                    {"operation_id": operation_id, "phase": "preview"},
                    "Checking workspace backup package", now,
                )
                os.replace(temporary, target)
                moved = True
                connection.execute(
                    "INSERT INTO account_exports(id,user_id,created_at,expires_at,relative_path,sha256,size_bytes) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (package_id, user_id, now, expires, export_relative, file_hash, size),
                )
                connection.execute(
                    "INSERT INTO workspace_data_operations(operation_id,source_workspace_id,owner_user_id,kind,state,phase,"
                    "idempotency_key,request_hash,job_id,package_export_id,package_sha256,package_size_bytes,manifest_json,created_at,updated_at) "
                    "VALUES(?,?,?,'restore','queued','preview',?,?,?,?,?,?,?, ?,?)",
                    (operation_id, workspace_id, user_id, idempotency_key, fingerprint, job_id,
                     package_id, file_hash, size, _json({"uploaded_filename": package_name, "package_expires_at": expires}), now, now),
                )
            op = self.store.row("SELECT * FROM workspace_data_operations WHERE operation_id=?", (operation_id,))
            return {"operation": self._operation_data(op, True), "job": {"id": job_id, "status": "queued"}, "replayed": False}
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if moved:
                try:
                    referenced = self.store.row(
                        "SELECT 1 FROM account_exports WHERE id=? AND user_id=?",
                        (package_id, user_id),
                    )
                    if referenced is None:
                        target.unlink(missing_ok=True)
                except (OSError, sqlite3.Error):
                    pass
            raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def operation(self, identity: dict, operation_id: str) -> dict:
        user_id, current_workspace_id = self._identity(identity)
        row = self.store.row(
            "SELECT o.* FROM workspace_data_operations o WHERE o.operation_id=? AND o.owner_user_id=? "
            "AND (o.source_workspace_id=? OR o.target_workspace_id=?)",
            (operation_id, user_id, current_workspace_id, current_workspace_id),
        )
        if row is None:
            raise ApiError("NOT_FOUND", "The workspace data operation is unavailable", 404)
        with self.store.transaction(immediate=False) as connection:
            self._require_workspace_read(connection, identity, current_workspace_id)
            active = connection.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
                (row["source_workspace_id"], user_id),
            ).fetchone()
            target = None if row["target_workspace_id"] is None else connection.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
                (row["target_workspace_id"], user_id),
            ).fetchone()
            if active is None and target is None:
                raise ApiError("NOT_FOUND", "The workspace data operation is unavailable", 404)
            job = None
            if row["job_id"]:
                job_columns = {column["name"] for column in connection.execute("PRAGMA table_info(jobs)")}
                control_revision = "control_revision" if "control_revision" in job_columns else "0 AS control_revision"
                pause_requested = "pause_requested" if "pause_requested" in job_columns else "0 AS pause_requested"
                suspended_from = "suspended_from" if "suspended_from" in job_columns else "NULL AS suspended_from"
                job = connection.execute(
                    f"SELECT id,status,{control_revision},{pause_requested},{suspended_from} FROM jobs "
                    "WHERE id=? AND workspace_id=?",
                    (row["job_id"], row["source_workspace_id"]),
                ).fetchone()
        package_available = self._package_available(row, user_id)
        result = self._operation_data(row, package_available)
        if job is not None:
            job_status = job["status"]
            result["job"] = {
                "id": job["id"],
                "status": job_status,
                "control_revision": job["control_revision"] if "control_revision" in job.keys() else 0,
                "pause_requested": bool(job["pause_requested"]) if "pause_requested" in job.keys() else False,
                "suspended_from": job["suspended_from"] if "suspended_from" in job.keys() else None,
            }
            if result["state"] not in {"backup_ready", "preview_ready", "preview_conflicts", "complete"}:
                result["state"] = {
                    "pause_requested": "pause_requested",
                    "cancel_requested": "cancelling",
                }.get(job_status, job_status)
        return result

    def list_operations(self, identity: dict) -> list[dict]:
        user_id, workspace_id = self._identity(identity)
        connection = self.store.connect()
        try:
            self._require_workspace_read(connection, identity, workspace_id)
            rows = connection.execute(
                "SELECT * FROM workspace_data_operations WHERE owner_user_id=? "
                "AND (source_workspace_id=? OR target_workspace_id=?) ORDER BY created_at DESC,operation_id LIMIT 50",
                (user_id, workspace_id, workspace_id),
            ).fetchall()
        finally:
            connection.close()
        return [self._operation_data(row, self._package_available(row, user_id)) for row in rows]

    def _package_available(self, operation, user_id: str) -> bool:
        package_id = operation["package_export_id"]
        if not package_id:
            return False
        now = utc_now()
        row = self.store.row(
            "SELECT relative_path,sha256,size_bytes FROM account_exports WHERE id=? AND user_id=? "
            "AND revoked_at IS NULL AND expires_at>?",
            (package_id, user_id, now),
        )
        if row is None:
            return False
        try:
            path = _safe_account_export_path(self.store, row["relative_path"])
            return path.stat().st_size == row["size_bytes"]
        except (OSError, WorkspaceDataError):
            return False

    def download_package(self, identity: dict, operation_id: str):
        data = self.operation(identity, operation_id)
        if not data["package_available"]:
            raise ApiError("WORKSPACE_PACKAGE_EXPIRED", "The workspace backup package is unavailable or expired", 410)
        row = self.store.row(
            "SELECT e.relative_path,e.sha256,e.size_bytes FROM workspace_data_operations o "
            "JOIN account_exports e ON e.id=o.package_export_id "
            "WHERE o.operation_id=? AND o.owner_user_id=? AND e.user_id=? AND e.revoked_at IS NULL AND e.expires_at>?",
            (operation_id, identity["user"]["id"], identity["user"]["id"], utc_now()),
        )
        if row is None:
            raise ApiError("WORKSPACE_PACKAGE_EXPIRED", "The workspace backup package is unavailable or expired", 410)
        try:
            path = _safe_account_export_path(self.store, row["relative_path"])
            digest, size = _file_hash(path)
        except (OSError, WorkspaceDataError) as exc:
            raise ApiError("WORKSPACE_PACKAGE_UNAVAILABLE", "The saved workspace package could not be read", 410) from exc
        if digest != row["sha256"] or size != row["size_bytes"]:
            raise ApiError("WORKSPACE_PACKAGE_INTEGRITY_ERROR", "The saved workspace package failed integrity verification", 409)
        response = send_file(path, as_attachment=True, download_name="oevira-workspace-backup.zip",
                             mimetype="application/zip", conditional=False)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def execute_restore(self, identity: dict, operation_id: str, data: dict, idempotency_key: str) -> dict:
        user_id, workspace_id = self._identity(identity)
        allowed = {"target_name", "resolutions"}
        if set(data) - allowed or not isinstance(data.get("resolutions", {}), dict):
            raise ApiError("INVALID_INPUT", "Restore input contains unsupported fields", 400)
        target_name = data.get("target_name")
        if not isinstance(target_name, str) or not 1 <= len(target_name.strip()) <= 200:
            raise ApiError("INVALID_INPUT", "Provide a name for the restored workspace", 400)
        resolutions = data.get("resolutions", {})
        allowed_resolutions = {"map_missing_authors_to_current_user", "restore_partial_content", "accept_restricted_scope"}
        if set(resolutions) - allowed_resolutions or any(type(v) is not bool for v in resolutions.values()):
            raise ApiError("INVALID_INPUT", "Restore resolutions contain unsupported choices", 400)
        now = utc_now()
        with self.store.transaction() as connection:
            self._require_workspace_read(connection, identity, workspace_id)
            row = connection.execute(
                "SELECT * FROM workspace_data_operations WHERE operation_id=? AND owner_user_id=? "
                "AND source_workspace_id=? AND kind='restore'",
                (operation_id, user_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ApiError("NOT_FOUND", "The workspace restore preview is unavailable", 404)
            if row["state"] not in {"preview_ready", "preview_conflicts"} or row["phase"] != "preview":
                raise ApiError("WORKSPACE_RESTORE_PREVIEW_REQUIRED", "Preview this package before restoring it", 409)
            try:
                preview = json.loads(row["preview_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                preview = {}
            if preview.get("missing_author_ids") and not resolutions.get("map_missing_authors_to_current_user"):
                raise ApiError(
                    "WORKSPACE_RESTORE_AUTHOR_MAPPING_REQUIRED",
                    "Some historical authors are not present in this account. Choose whether to attribute them to the current user.",
                    409,
                    {"missing_author_count": len(preview.get("missing_author_ids", []))},
                )
            if preview.get("missing_files") and not resolutions.get("restore_partial_content"):
                raise ApiError(
                    "WORKSPACE_RESTORE_MISSING_FILES",
                    "Some saved originals are missing from the package. Choose whether to restore the remaining content with those files unavailable.",
                    409,
                    {"missing_file_count": len(preview.get("missing_files", []))},
                )
            if preview.get("restricted_scope_count") and not resolutions.get("accept_restricted_scope"):
                raise ApiError(
                    "WORKSPACE_RESTORE_SCOPE_RESTRICTED",
                    "The backup contains a restricted portion. Review and accept the accessible scope before restore.",
                    409,
                    {"restricted_scope_count": preview["restricted_scope_count"]},
                )
            if preview.get("blocking_conflicts"):
                raise ApiError(
                    "WORKSPACE_RESTORE_CONFLICTS_UNRESOLVED",
                    "Resolve the listed package conflicts before restore.", 409,
                    {"conflicts": preview["blocking_conflicts"][:20]},
                )
            request_payload = {
                "operation_id": operation_id, "target_name": target_name.strip(),
                "resolutions": resolutions,
            }
            fingerprint = _request_fingerprint(request_payload)
            job_payload = {**request_payload, "phase": "execute"}
            job_fingerprint = _request_fingerprint(job_payload)
            existing_job = connection.execute(
                "SELECT id,request_hash FROM jobs WHERE workspace_id=? AND kind='workspace_restore' AND idempotency_key=?",
                (workspace_id, f"workspace-restore-execute:{operation_id}:{idempotency_key}"),
            ).fetchone()
            if existing_job is not None:
                if existing_job["request_hash"] != job_fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This restore key was used for a different operation", 409)
                return {"operation": self._operation_data(row, True), "job": dict(existing_job), "replayed": True}
            target_workspace_id = row["target_workspace_id"] or str(uuid.uuid4())
            job_id, _ = self._insert_job(
                connection, workspace_id, user_id, "workspace_restore",
                f"workspace-restore-execute:{operation_id}:{idempotency_key}",
                job_payload,
                "Restoring workspace data into an isolated workspace", now,
            )
            connection.execute(
                "UPDATE workspace_data_operations SET state='restore_queued',phase='execute',job_id=?,target_workspace_id=?,"
                "target_name=?,resolutions_json=?,request_hash=?,updated_at=? WHERE operation_id=? AND state IN ('preview_ready','preview_conflicts')",
                (job_id, target_workspace_id, target_name.strip(), _json(resolutions), fingerprint, now, operation_id),
            )
        return {"operation": self.operation(identity, operation_id), "job": {"id": job_id, "status": "queued"}, "replayed": False}

    def activate_restored_workspace(self, identity: dict, operation_id: str) -> dict:
        user_id, current_workspace_id = self._identity(identity)
        now = utc_now()
        with self.store.transaction() as connection:
            op = connection.execute(
                "SELECT * FROM workspace_data_operations WHERE operation_id=? AND owner_user_id=? AND state='complete'",
                (operation_id, user_id),
            ).fetchone()
            if op is None or not op["target_workspace_id"]:
                raise ApiError("WORKSPACE_RESTORE_NOT_COMPLETE", "The restored workspace is not ready to open", 409)
            member = connection.execute(
                "SELECT active FROM memberships WHERE workspace_id=? AND user_id=?",
                (op["target_workspace_id"], user_id),
            ).fetchone()
            if member is None or not member["active"]:
                raise ApiError("WORKSPACE_RESTORE_ACCESS_UNAVAILABLE", "Current access to the restored workspace is unavailable", 403)
            local = connection.execute(
                "SELECT user_id,workspace_id FROM local_identity WHERE singleton=1"
            ).fetchone()
            if local is None or local["user_id"] != user_id or local["workspace_id"] != current_workspace_id:
                return {"state": "account_workspace_switch_required", "workspace_id": op["target_workspace_id"]}
            session_id = identity.get("session_id")
            if isinstance(session_id, str) and session_id:
                connection.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND workspace_id=? AND revoked_at IS NULL",
                    (now, session_id, user_id, current_workspace_id),
                )
            connection.execute(
                "UPDATE local_identity SET workspace_id=? WHERE singleton=1 AND user_id=? AND workspace_id=?",
                (op["target_workspace_id"], user_id, current_workspace_id),
            )
            token, session_data = issue_session_in_transaction(
                connection, user_id, op["target_workspace_id"], now=now,
            )
        return {"state": "workspace_activated", "token": token, **session_data}

    @staticmethod
    def _require_member(connection, identity: dict, workspace_id: str, *, write: bool = False) -> None:
        scoped = {**identity, "workspace": {**identity["workspace"], "id": workspace_id}}
        require_workspace_access(connection, scoped, write=write)

    def _access_scope(self, connection, identity: dict, workspace_id: str) -> dict:
        self._require_workspace_read(connection, identity, workspace_id)
        user_id = identity["user"]["id"]
        workspace = connection.execute("SELECT id,name,created_at FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if workspace is None:
            raise WorkspaceDataError("WORKSPACE_NOT_FOUND")
        role = connection.execute(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
            (workspace_id, user_id),
        ).fetchone()["role"]
        commission_ids = set()
        if "client_commissions" in _table_names(connection) and "commission_members" in _table_names(connection):
            commission_ids = {
                row["id"] for row in connection.execute(
                    "SELECT c.id FROM client_commissions c JOIN commission_members m ON m.commission_id=c.id "
                    "WHERE c.workspace_id=? AND m.user_id=? AND m.active=1",
                    (workspace_id, user_id),
                )
            }
        all_works = [row["id"] for row in connection.execute(
            "SELECT id FROM works WHERE workspace_id=? ORDER BY id", (workspace_id,)
        )]
        scoped_identity = {**identity, "workspace": {**identity["workspace"], "id": workspace_id}}
        accessible_works = set()
        for work_id in all_works:
            try:
                require_work_access(connection, scoped_identity, work_id, write=False)
            except ApiError:
                continue
            accessible_works.add(work_id)

        connection.execute("CREATE TEMP TABLE IF NOT EXISTS wd_allowed_work_ids(id TEXT PRIMARY KEY)")
        connection.execute("DELETE FROM wd_allowed_work_ids")
        connection.executemany("INSERT INTO wd_allowed_work_ids(id) VALUES(?)", [(value,) for value in accessible_works])
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS wd_allowed_commission_ids(id TEXT PRIMARY KEY)")
        connection.execute("DELETE FROM wd_allowed_commission_ids")
        connection.executemany("INSERT INTO wd_allowed_commission_ids(id) VALUES(?)", [(value,) for value in commission_ids])
        active_members = connection.execute(
            "SELECT COUNT(*) AS n FROM memberships WHERE workspace_id=? AND active=1", (workspace_id,)
        ).fetchone()["n"]
        source_member_count = connection.execute(
            "SELECT COUNT(*) AS n FROM memberships WHERE workspace_id=? AND active=1 AND user_id<>?",
            (workspace_id, user_id),
        ).fetchone()["n"]
        all_commissions = connection.execute(
            "SELECT COUNT(*) AS n FROM client_commissions WHERE workspace_id=?", (workspace_id,)
        ).fetchone()["n"] if "client_commissions" in _table_names(connection) else 0
        return {
            "workspace": dict(workspace),
            "workspace_role": role,
            "accessible_work_ids": accessible_works,
            "accessible_commission_ids": commission_ids,
            "restricted_work_count": max(0, len(all_works) - len(accessible_works)),
            "restricted_commission_count": max(0, int(all_commissions) - len(commission_ids)),
            "other_active_member_count": int(source_member_count),
            "active_member_count": int(active_members),
        }

    @staticmethod
    def _excluded_table(table: str) -> bool:
        return (
            table in _EXCLUDED_TABLES
            or table.startswith("account_")
            or table.startswith("legacy_import_")
            or table.startswith("community_")
            or table.startswith("sqlite_")
        )

    def _backup_table_names(self, connection) -> tuple[list[str], dict[str, str]]:
        names = _table_names(connection)
        schemas = {table: _table_schema(connection, table) for table in names}
        included = {
            table for table in names
            if not self._excluded_table(table) and "workspace_id" in schemas[table]["columns"]
        }
        included.update({"workspaces", "users", "sessions"})
        changed = True
        while changed:
            changed = False
            for table in names:
                if table in included or self._excluded_table(table):
                    continue
                if "workspace_id" not in schemas[table]["columns"] and table not in _ALLOWED_GLOBAL_CHILD_TABLES:
                    continue
                for fk in schemas[table]["foreign_keys"]:
                    if fk["table"] in included:
                        included.add(table)
                        changed = True
                        break
        ddl = {}
        for table in sorted(included):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None or not isinstance(row["sql"], str) or "VIRTUAL TABLE" in row["sql"].upper():
                continue
            ddl[table] = row["sql"]
        return sorted(ddl), ddl

    @staticmethod
    def _scope_name(table: str) -> str:
        return "wd_scope_" + hashlib.sha256(table.encode("utf-8")).hexdigest()[:16]

    def _prepare_workspace_scopes(self, connection, identity: dict, workspace_id: str,
                                  schemas: dict[str, dict], table_names: list[str]) -> tuple[list[str], dict[str, int]]:
        base_tables = [
            table for table in table_names
            if table not in {"workspaces", "users", "sessions"}
            and "workspace_id" in schemas[table]["columns"]
            and not self._excluded_table(table)
        ]
        dependencies = {}
        for table in base_tables:
            parents = {
                fk["table"] for fk in schemas[table]["foreign_keys"]
                if fk["table"] in base_tables and fk["table"] != table
            }
            if table == "publications":
                parents.update(name for name in ("artifacts", "materials", "media_projects", "media_renders") if name in base_tables)
            dependencies[table] = parents
        order = []
        pending = set(base_tables)
        while pending:
            ready = sorted(table for table in pending if dependencies.get(table, set()).issubset(order))
            if not ready:
                # A schema cycle cannot be safely treated as a complete export.
                # Keep the affected tables out and report the concrete scope gap.
                break
            for table in ready:
                order.append(table)
                pending.remove(table)
        cycles = pending
        row_counts = {}

        # The source Workspace is the only shared parent admitted by identity.
        connection.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {_identifier(self._scope_name('workspaces'))}(k0 TEXT PRIMARY KEY)"
        )
        connection.execute(f"DELETE FROM {_identifier(self._scope_name('workspaces'))}")
        connection.execute(f"INSERT INTO {_identifier(self._scope_name('workspaces'))}(k0) VALUES(?)", (workspace_id,))

        for table in order:
            schema = schemas[table]
            primary_key = schema["primary_key"]
            if not primary_key:
                cycles.add(table)
                continue
            scope_table = self._scope_name(table)
            column_sql = ",".join(f"k{index} {schema['types'].get(column) or 'BLOB'}" for index, column in enumerate(primary_key))
            pk_sql = ",".join(f"k{index}" for index in range(len(primary_key)))
            connection.execute(
                f"CREATE TEMP TABLE IF NOT EXISTS {_identifier(scope_table)}({column_sql}, PRIMARY KEY({pk_sql}))"
            )
            connection.execute(f"DELETE FROM {_identifier(scope_table)}")
            query, parameters = self._workspace_row_query(table, schema, workspace_id, identity)
            clauses = []
            for fk_id in sorted({item["id"] for item in schema["foreign_keys"]}):
                group = sorted((item for item in schema["foreign_keys"] if item["id"] == fk_id), key=lambda item: item["seq"])
                parent_table = group[0]["table"]
                parent_cols = [item.get("to") or "id" for item in group]
                child_cols = [item["from"] for item in group]
                if table in {"business_case_sources", "business_evidence", "business_metric_observations"} and parent_table == "materials":
                    # The evidence row remains user-owned even when a source
                    # Material is outside this actor's export scope; its link
                    # is removed by the row adapter below.
                    continue
                if parent_table not in schemas or parent_table == table:
                    continue
                if parent_table in {"users", "sessions"}:
                    continue
                if parent_table not in base_tables and parent_table != "workspaces":
                    continue
                parent_pk = schemas[parent_table]["primary_key"]
                if parent_table in base_tables and parent_table not in order:
                    # The parent is in a cycle; do not export rows whose ownership
                    # depends on a scope we could not prove.
                    clauses.append("0 = 1")
                    cycles.add(table)
                    continue
                if set(parent_cols) != set(parent_pk):
                    # All current cross-owner FKs target the full primary key. A
                    # different shape needs an explicit owner adapter.
                    clauses.append("0 = 1")
                    cycles.add(table)
                    continue
                parent_scope = self._scope_name(parent_table)
                equalities = " AND ".join(
                    f"p.k{index} = {_identifier(child_cols[index])}"
                    for index in range(len(child_cols))
                )
                nullable = " OR ".join(f"{_identifier(column)} IS NULL" for column in child_cols)
                scope_condition = f"EXISTS (SELECT 1 FROM {_identifier(parent_scope)} p WHERE {equalities})"
                if any(schema["not_null"].get(column, False) for column in child_cols):
                    clauses.append(scope_condition)
                else:
                    clauses.append(f"(({nullable}) OR {scope_condition})")
            if clauses:
                query += " AND " + " AND ".join(clauses)
            selected = connection.execute(
                f"SELECT {','.join(_identifier(column) for column in primary_key)} FROM ({query}) scoped_rows",
                parameters,
            )
            values = [tuple(row[index] for index in range(len(primary_key))) for row in selected]
            if values:
                connection.executemany(
                    f"INSERT OR IGNORE INTO {_identifier(scope_table)}({','.join(f'k{i}' for i in range(len(primary_key)))}) "
                    f"VALUES({','.join('?' for _ in primary_key)})",
                    values,
                )
            row_counts[table] = len(values)
        restricted_media_projects = 0
        if "media_projects" in order and "media_project_versions" in order and "materials" in order:
            material_scope = self._scope_name("materials")
            project_scope = self._scope_name("media_projects")
            project_ids = [row[0] for row in connection.execute(
                f"SELECT k0 FROM {_identifier(project_scope)}"
            )]
            for project_id in project_ids:
                versions = connection.execute(
                    "SELECT state_json FROM media_project_versions WHERE workspace_id=? AND project_id=?",
                    (workspace_id, project_id),
                ).fetchall()
                inaccessible = False
                for version in versions:
                    try:
                        state = json.loads(version["state_json"] or "{}")
                        assets = state.get("timeline", {}).get("assets", [])
                        material_ids = [asset.get("source_material_id") for asset in assets if isinstance(asset, dict)]
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        inaccessible = True
                        break
                    for material_id in material_ids:
                        if not isinstance(material_id, str) or not connection.execute(
                            f"SELECT 1 FROM {_identifier(material_scope)} WHERE k0=?", (material_id,)
                        ).fetchone():
                            inaccessible = True
                            break
                    if inaccessible:
                        break
                if inaccessible:
                    connection.execute(
                        f"DELETE FROM {_identifier(project_scope)} WHERE k0=?", (project_id,)
                    )
                    restricted_media_projects += 1
            if restricted_media_projects:
                row_counts["restricted_media_projects"] = restricted_media_projects
        if "publications" in order:
            publication_scope = self._scope_name("publications")
            parent_scopes = {
                "media_material": ("materials", "source_id"),
                "media_render": ("media_renders", "source_id"),
                "media_project": ("media_projects", "source_id"),
            }
            if "source_kind" in schemas["publications"]["columns"]:
                for publication in connection.execute(
                    "SELECT id,source_kind,source_id FROM publications WHERE workspace_id=?",
                    (workspace_id,),
                ):
                    target = parent_scopes.get(publication["source_kind"])
                    if target is None:
                        continue
                    parent_table, _ = target
                    if parent_table not in order:
                        connection.execute(
                            f"DELETE FROM {_identifier(publication_scope)} WHERE k0=?",
                            (publication["id"],),
                        )
                        continue
                    parent_scope = self._scope_name(parent_table)
                    if not connection.execute(
                        f"SELECT 1 FROM {_identifier(parent_scope)} WHERE k0=?",
                        (publication["source_id"],),
                    ).fetchone():
                        connection.execute(
                            f"DELETE FROM {_identifier(publication_scope)} WHERE k0=?",
                            (publication["id"],),
                        )
        return order, {"scope_cycle_tables": len(cycles), **{f"scope_cycle:{table}": 1 for table in sorted(cycles)}, **row_counts}

    def _workspace_row_query(self, table: str, schema: dict, workspace_id: str,
                             identity: dict, selected_columns: list[str] | None = None) -> tuple[str, tuple]:
        columns = set(schema["columns"])
        clauses = ["workspace_id = ?"]
        parameters: list = [workspace_id]
        if table == "works":
            clauses.append("id IN (SELECT id FROM wd_allowed_work_ids)")
        elif "work_id" in columns:
            clauses.append("(work_id IS NULL OR work_id IN (SELECT id FROM wd_allowed_work_ids))")
        if table == "client_commissions":
            clauses.append("id IN (SELECT id FROM wd_allowed_commission_ids)")
        elif "commission_id" in columns:
            clauses.append("commission_id IN (SELECT id FROM wd_allowed_commission_ids)")
        if table == "publications" and {"source_kind", "source_id"} <= columns:
            clauses.append(
                "((source_kind='artifact' AND artifact_id IN (SELECT k0 FROM "
                f"{_identifier(self._scope_name('artifacts'))})) OR "
                "(source_kind='media_material' AND source_id IN (SELECT k0 FROM "
                f"{_identifier(self._scope_name('materials'))})) OR "
                "(source_kind='media_render' AND source_id IN (SELECT k0 FROM "
                f"{_identifier(self._scope_name('media_renders'))})) OR "
                "(source_kind='media_project' AND source_id IN (SELECT k0 FROM "
                f"{_identifier(self._scope_name('media_projects'))})))"
            )
        owner_user_id = identity["user"]["id"]
        if table in _OWNER_ONLY_TABLES:
            owner_column = next((name for name in (
                "owner_user_id", "created_by", "actor_user_id", "user_id",
            ) if name in columns), None)
            if owner_column:
                clauses.append(f"{_identifier(owner_column)} = ?")
                parameters.append(owner_user_id)
        if table in {"jobs", "media_uploads", "media_recording_sessions"}:
            actor_column = "user_id" if "user_id" in columns else "owner_user_id"
            if "work_id" in columns:
                clauses.append(f"({actor_column} = ? OR work_id IN (SELECT id FROM wd_allowed_work_ids))")
            else:
                clauses.append(f"{actor_column} = ?")
            parameters.append(owner_user_id)
        if table == "jobs":
            clauses.append("kind NOT IN ('workspace_backup','workspace_restore')")
        projection = "*" if selected_columns is None else ",".join(_identifier(column) for column in selected_columns)
        query = f"SELECT {projection} FROM {_identifier(table)} WHERE " + " AND ".join(clauses)
        return query, tuple(parameters)

    def _workspace_scoped_query(self, table: str, schema: dict, workspace_id: str,
                                identity: dict, base_tables: set[str], ordered_tables: set[str],
                                schemas: dict[str, dict],
                                selected_columns: list[str] | None = None) -> tuple[str, tuple]:
        query, parameters = self._workspace_row_query(
            table, schema, workspace_id, identity, selected_columns=selected_columns,
        )
        clauses = []
        if table in ordered_tables:
            own_pk = schema["primary_key"]
            own_scope = self._scope_name(table)
            equalities = " AND ".join(
                f"p.k{index} = {_identifier(column)}" for index, column in enumerate(own_pk)
            )
            if own_pk:
                clauses.append(f"EXISTS (SELECT 1 FROM {_identifier(own_scope)} p WHERE {equalities})")
        for fk_id in sorted({item["id"] for item in schema["foreign_keys"]}):
            group = sorted((item for item in schema["foreign_keys"] if item["id"] == fk_id), key=lambda item: item["seq"])
            parent_table = group[0]["table"]
            parent_cols = [item.get("to") or "id" for item in group]
            child_cols = [item["from"] for item in group]
            if parent_table == table or parent_table in {"users", "sessions"}:
                continue
            if table in {"business_case_sources", "business_evidence", "business_metric_observations"} and parent_table == "materials":
                # Preserve an evidence/reference row without carrying private
                # customer source material into another actor's backup scope.
                continue
            if parent_table not in base_tables and parent_table != "workspaces":
                continue
            if parent_table in base_tables and parent_table not in ordered_tables:
                clauses.append("0 = 1")
                continue
            # Scope tables encode each parent PK as k0, k1, ... .
            scope_table = self._scope_name(parent_table)
            parent_scope_schema = schemas[parent_table]["primary_key"] if parent_table in schemas else []
            if len(parent_cols) != len(parent_scope_schema) or set(parent_cols) != set(parent_scope_schema):
                clauses.append("0 = 1")
                continue
            parent_positions = [parent_scope_schema.index(column) for column in parent_cols]
            equalities = " AND ".join(
                f"p.k{parent_positions[index]} = {_identifier(child_cols[index])}"
                for index in range(len(child_cols))
            )
            match = f"EXISTS (SELECT 1 FROM {_identifier(scope_table)} p WHERE {equalities})"
            if any(schema["not_null"].get(column, False) for column in child_cols):
                clauses.append(match)
            else:
                null_condition = " OR ".join(f"{_identifier(column)} IS NULL" for column in child_cols)
                clauses.append(f"(({null_condition}) OR {match})")
        if clauses:
            query += " AND " + " AND ".join(clauses)
        return query, parameters

    def _prepare_backup_row(self, table: str, source_row: dict, workspace_id: str,
                            actor_id: str, file_specs: dict[str, dict], warnings: Counter,
                            missing_files: list[dict], scope: dict) -> dict | None:
        if table == "job_control_receipts":
            # Preserve the table's schema in the snapshot, but never revive an
            # old pause/cancel/retry operation against restored Job history.
            warnings["job_control_receipts_not_restored"] += 1
            return None
        row = dict(source_row)
        if table == "browser_session_operations":
            warnings["browser_context_preview_bodies_not_backed_up"] += int(row.get("preview_json") is not None)
            row["preview_json"] = None
        if table == "delivery_batches" and row.get("status") != "completed":
            # A preparing batch has no completed ZIP yet. Preserve its member
            # records and source files without calling the absent parent ZIP a
            # missing user original.
            row["status"] = "partial"
            row["sha256"] = None
            row["size_bytes"] = None
            row["relative_path"] = "files/unbuilt/" + hashlib.sha256(
                f"delivery:{row.get('id')}".encode("utf-8")
            ).hexdigest()
        if table == "media_interchanges":
            if row.get("staging_path"):
                warnings["unfinished_media_interchange_staging_not_exported"] += 1
            row["staging_path"] = None
            if row.get("status") != "completed":
                if row.get("status") in {"queued", "running", "writing"}:
                    row["status"] = "interrupted"
                row["relative_path"] = None
                row["sha256"] = None
                row["size_bytes"] = None
            elif (not row.get("relative_path") or not isinstance(row.get("sha256"), str)
                  or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
                  or isinstance(row.get("size_bytes"), bool) or not isinstance(row.get("size_bytes"), int)
                  or row["size_bytes"] < 0):
                raise WorkspaceDataError("WORKSPACE_MEDIA_INTERCHANGE_OUTPUT_INVALID")
        if table == "acquisition_query_paths" and _without_browser_query_preview(row, now=utc_now()):
            warnings["browser_query_preview_bodies_not_backed_up"] += 1
        schema_columns = set(row)
        accessible_materials = scope.get("accessible_material_ids", set())
        if table == "business_case_sources" and row.get("material_id") is not None and row["material_id"] not in accessible_materials:
            row["material_id"] = None
            row["source_title"] = None
            row["source_url"] = None
            row["source_sha256"] = None
            row["link_status"] = "source_material_not_in_backup_scope"
            warnings["restricted_case_sources_preserved_as_unlinked"] += 1
        if table == "business_evidence" and row.get("material_id") is not None and row["material_id"] not in accessible_materials:
            row["material_id"] = None
            row["material_sha256"] = None
            row["source_title"] = None
            row["source_url"] = None
            row["excerpt"] = "[source material not included in this workspace backup scope]"
            row["source_state"] = "source_unavailable_in_restored_scope"
            warnings["restricted_business_evidence_source_unlinked"] += 1
        if table == "business_metric_observations" and row.get("material_id") is not None and row["material_id"] not in accessible_materials:
            row["material_id"] = None
            if "review_status" in row:
                row["review_status"] = "needs_review"
            if "review_note" in row:
                row["review_note"] = "The historical source Material was outside the backed-up access scope."
            warnings["restricted_metric_source_unlinked"] += 1
        for column in _FILE_COLUMNS.get(table, set()) & schema_columns:
            value = row.get(column)
            if not value:
                continue
            if table == "media_uploads" and column == "stage_relative_path":
                row[column] = None
                warnings["unfinished_upload_staging_not_exported"] += 1
                continue
            if table == "media_renders" and column == "staging_path":
                # A render staging file is an uncommitted intermediate, not a saved output.
                row[column] = None
                warnings["unfinished_render_staging_not_exported"] += 1
                continue
            if table == "media_interchanges" and column == "staging_path":
                row[column] = None
                continue
            if table == "delivery_batches" and column == "relative_path" and row.get("status") != "completed":
                continue
            try:
                source_path = _safe_object_path(self.store, value, workspace_id)
                stat = source_path.stat()
                if stat.st_size > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                    raise WorkspaceDataError("WORKSPACE_FILE_TOO_LARGE")
                sha256, size = _file_hash(source_path)
                member = f"files/{sha256}"
                expected = None
                if table == "materials":
                    expected = row.get("sha256")
                elif table in {"exports", "media_renders", "delivery_batches", "media_interchanges"}:
                    expected = row.get("sha256")
                elif table == "media_uploads" and column == "original_relative_path":
                    expected = row.get("expected_sha256")
                file_specs.setdefault(member, {
                    "member": member,
                    "source_path": source_path,
                    "source_paths": [source_path],
                    "sha256": sha256,
                    "size_bytes": size,
                    "expected_sha256": expected,
                    "hash_matches": bool(expected and expected == sha256) if expected else None,
                    "references": 0,
                })
                if source_path not in file_specs[member].setdefault("source_paths", []):
                    file_specs[member]["source_paths"].append(source_path)
                file_specs[member]["references"] += 1
                row[column] = member
                if expected and expected != sha256:
                    warnings["saved_file_hash_mismatch_preserved"] += 1
                if table == "media_uploads" and row.get("status") in {"receiving", "finalizing"}:
                    row["status"] = "interrupted"
            except (OSError, ValueError, WorkspaceDataError):
                marker = f"files/missing/{hashlib.sha256(f'{table}:{value}'.encode()).hexdigest()}"
                row[column] = marker
                warnings["file_reference_missing_or_invalid"] += 1
                source_key = next((str(source_row[name]) for name in (
                    "id", "artifact_id", "subject_id", "case_id", "project_id", "material_id",
                    "session_id", "operation_id",
                ) if source_row.get(name) is not None), table)
                missing_files.append({
                    "table": table,
                    "column": column,
                    "record_fingerprint": hashlib.sha256(source_key.encode()).hexdigest(),
                    "reason": "file_missing_or_path_invalid",
                })

        if table == "browser_sessions":
            # Preserve the session record without copying authentication or live operation state.
            row["state_ciphertext"] = None
            row["state_protection"] = None
            row["operation_id"] = None
            row["operation_expires_at"] = None
            warnings["browser_auth_state_not_exported"] += int(source_row.get("state_ciphertext") is not None)
            if isinstance(row.get("current_url"), str):
                row["current_url"], changed = _safe_url(row["current_url"])
                warnings["browser_url_query_redacted"] += int(changed)
        if table == "delivery_members" and isinstance(row.get("file_json"), str) and row["file_json"]:
            try:
                member_file = json.loads(row["file_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID") from exc
            if not isinstance(member_file, dict):
                raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID")
            relative_path = member_file.get("relative_path")
            if relative_path:
                expected_sha = member_file.get("sha256")
                expected_size = member_file.get("size_bytes")
                if (not isinstance(expected_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)
                        or isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0):
                    raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID")
                try:
                    source_path = _safe_object_path(self.store, relative_path, workspace_id)
                    stat = source_path.stat()
                    if stat.st_size > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                        raise WorkspaceDataError("WORKSPACE_FILE_TOO_LARGE")
                    sha256, size = _file_hash(source_path)
                    member = f"files/{sha256}"
                    file_specs.setdefault(member, {
                        "member": member,
                        "source_path": source_path,
                        "source_paths": [source_path],
                        "sha256": sha256,
                        "size_bytes": size,
                        "expected_sha256": expected_sha,
                        "hash_matches": expected_sha == sha256 and expected_size == size,
                        "references": 0,
                    })
                    if source_path not in file_specs[member].setdefault("source_paths", []):
                        file_specs[member]["source_paths"].append(source_path)
                    file_specs[member]["references"] += 1
                    member_file["relative_path"] = member
                    if (expected_sha, expected_size) != (sha256, size):
                        warnings["delivery_member_file_hash_mismatch_preserved"] += 1
                except (OSError, ValueError, WorkspaceDataError):
                    marker = "files/missing/" + hashlib.sha256(
                        f"delivery_members:{row.get('batch_id')}:{row.get('member_id')}:{relative_path}".encode("utf-8")
                    ).hexdigest()
                    member_file["relative_path"] = marker
                    missing_files.append({
                        "table": "delivery_members",
                        "column": "file_json",
                        "record_fingerprint": hashlib.sha256(
                            f"{row.get('batch_id')}:{row.get('member_id')}".encode("utf-8")
                        ).hexdigest(),
                        "reason": "file_missing_or_path_invalid",
                    })
                    warnings["file_reference_missing_or_invalid"] += 1
            row["file_json"] = _json(member_file)
        if table in {"intelligence_model_connections", "intelligence_github_connections", "intelligence_mcp_connections"}:
            row["credential_ciphertext"] = None
            row["credential_protection"] = None
            row["status"] = "revoked"
            if "allow_scoped_data" in row:
                row["allow_scoped_data"] = 0
            if "is_selected" in row:
                row["is_selected"] = 0
            if "provider_login" in row:
                row["provider_login"] = None
            if "validation_status" in row:
                row["validation_status"] = "invalid"
            if "authorized_use_confirmed" in row:
                row["authorized_use_confirmed"] = 0
            if "tool_count" in row:
                row["tool_count"] = 0
            if "tool_list_sha256" in row:
                row["tool_list_sha256"] = ""
            if "server_info_json" in row:
                row["server_info_json"] = "{}"
            if "capabilities_json" in row:
                row["capabilities_json"] = "{}"
            if "instructions_text" in row:
                row["instructions_text"] = None
            if isinstance(row.get("base_url"), str):
                row["base_url"] = _safe_url(row["base_url"])[0]
            warnings["provider_credentials_require_reconnection"] += 1
        if table == "publication_releases":
            row["share_token_hash"] = None
            warnings["public_share_tokens_not_exported"] += int(source_row.get("share_token_hash") is not None)
        if table == "jobs":
            if "payload_json" in row:
                row["payload_json"] = self._sanitize_json_column(row["payload_json"], "payload_json", warnings)
        if table in {"intelligence_conversations", "intelligence_turns"}:
            for column in ("request_json", "context_json", "source_refs_json", "citations_json", "tool_results_json"):
                if column in row and row[column] is not None:
                    row[column] = self._sanitize_json_column(row[column], column, warnings)
            if table == "intelligence_conversations":
                row["codex_thread_id"] = None

        for column, value in list(row.items()):
            if (column in _TEXT_JSON_COLUMNS or column.endswith("_json")) and isinstance(value, str) and table not in {
                "jobs", "intelligence_conversations", "intelligence_turns",
            }:
                row[column] = self._sanitize_json_column(value, column, warnings)
            elif isinstance(value, str) and column in _URL_COLUMNS:
                row[column], changed = _safe_url(value)
                warnings["url_query_redacted"] += int(changed)
            elif isinstance(value, str) and column not in _TEXT_JSON_COLUMNS:
                row[column], redactions = _safe_text(value)
                warnings["credential_like_text_redacted"] += redactions
        return row

    @staticmethod
    def _sanitize_json_column(value: str, column: str, warnings: Counter) -> str:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            cleaned, count = _safe_text(value)
            warnings["credential_like_text_redacted"] += count
            return cleaned
        cleaned, count = _safe_json(parsed, field_name=column)
        warnings["credential_fields_omitted"] += count
        return _json(cleaned)

    def _create_filtered_record_db(self, operation_id: str, claim: dict, staging_dir: Path) -> tuple[dict, Path, dict]:
        source_workspace_id = claim["workspace_id"]
        actor_id = claim["user_id"]
        connection = self.store.connect()
        target_db = staging_dir / "workspace.sqlite3"
        target = sqlite3.connect(target_db)
        target.row_factory = sqlite3.Row
        file_specs: dict[str, dict] = {}
        warnings: Counter[str] = Counter()
        counts: Counter[str] = Counter()
        restricted: Counter[str] = Counter()
        missing_files: list[dict] = []
        created_at = utc_now()
        try:
            connection.execute("BEGIN")
            identity = {"user": {"id": actor_id}, "workspace": {"id": source_workspace_id}}
            scope = self._access_scope(connection, identity, source_workspace_id)
            scope_role = scope["workspace_role"]
            table_names, ddl = self._backup_table_names(connection)
            schemas = {table: _table_schema(connection, table) for table in table_names}
            ordered_base, scope_report = self._prepare_workspace_scopes(
                connection, identity, source_workspace_id, schemas, table_names,
            )
            base_tables = set(ordered_base)
            scope["accessible_material_ids"] = {
                row[0] for row in connection.execute(
                    f"SELECT k0 FROM {_identifier(self._scope_name('materials'))}"
                )
            } if "materials" in base_tables else set()
            protected_paths: dict[str, dict[str, str]] = {}
            for index, table in enumerate(ordered_base, start=1):
                if table not in base_tables or table in {"workspaces", "users", "sessions"}:
                    continue
                schema = schemas[table]
                if table == "delivery_members" and "file_json" in schema["columns"]:
                    selected = list(dict.fromkeys(schema["primary_key"] + ["file_json"]))
                else:
                    columns = sorted(_FILE_COLUMNS.get(table, set()) & set(schema["columns"]))
                    if not columns:
                        continue
                    selected = list(dict.fromkeys(schema["primary_key"] + columns))
                    if table in {"delivery_batches", "media_interchanges"} and "status" in schema["columns"]:
                        selected.append("status")
                if not _progress(
                    self.store, operation_id, claim, completed=index - 1,
                    total=max(1, len(ordered_base)), label="Protecting workspace backup files",
                ):
                    raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                query, params = self._workspace_scoped_query(
                    table, schema, source_workspace_id, identity, base_tables,
                    set(ordered_base), schemas, selected_columns=selected,
                )
                for row_index, raw in enumerate(connection.execute(query, params), start=1):
                    row = dict(raw)
                    if table in {"delivery_batches", "media_interchanges"} and row.get("status") != "completed":
                        continue
                    values = []
                    if table == "delivery_members":
                        try:
                            member_file = json.loads(row.get("file_json") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            member_file = {}
                        if isinstance(member_file, dict):
                            values.append(member_file.get("relative_path"))
                    else:
                        for column in _FILE_COLUMNS[table]:
                            if table == "media_renders" and column == "staging_path":
                                continue
                            if table == "media_interchanges" and column == "staging_path":
                                continue
                            if table == "media_uploads" and column == "stage_relative_path":
                                continue
                            values.append(row.get(column))
                    for value in values:
                        if not value:
                            continue
                        try:
                            source_path = _safe_object_path(self.store, value, source_workspace_id)
                        except (OSError, ValueError, WorkspaceDataError):
                            continue
                        relative = source_path.relative_to(self.store.root).as_posix()
                        protected_paths[relative] = {"relative_path": relative}
                    if row_index % 500 == 0 and not _progress(
                        self.store, operation_id, claim, completed=index - 1,
                        total=max(1, len(ordered_base)), label="Protecting workspace backup files",
                    ):
                        raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
            if not self._set_backup_protected_files(
                operation_id, claim, [protected_paths[key] for key in sorted(protected_paths)],
            ):
                raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
            target.execute("PRAGMA foreign_keys=OFF")
            for table in table_names:
                target.execute(ddl[table])
            target.execute(f"PRAGMA user_version={connection.execute('PRAGMA user_version').fetchone()[0]}")
            workspace_row = connection.execute(
                "SELECT id,name,created_at FROM workspaces WHERE id=?", (source_workspace_id,)
            ).fetchone()
            target.execute(
                "INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)",
                (workspace_row["id"], workspace_row["name"], workspace_row["created_at"]),
            )
            selected_table_names = set()
            for index, table in enumerate(ordered_base, start=1):
                if table not in base_tables or table in {"workspaces", "users", "sessions"} or "workspace_id" not in schemas[table]["columns"]:
                    continue
                if not _progress(
                    self.store, operation_id, claim, completed=index, total=max(1, len(table_names) + 1),
                    label=f"Snapshotting workspace data: {table}",
                ):
                    raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                query, params = self._workspace_scoped_query(
                    table, schemas[table], source_workspace_id, identity,
                    base_tables, set(ordered_base), schemas,
                )
                cursor = connection.execute(query, params)
                columns = schemas[table]["columns"]
                placeholders = ",".join("?" for _ in columns)
                insert = (
                    f"INSERT INTO {_identifier(table)} ({','.join(_identifier(name) for name in columns)}) "
                    f"VALUES ({placeholders})"
                )
                for raw in cursor:
                    row = self._prepare_backup_row(
                        table, dict(raw), source_workspace_id, actor_id, file_specs, warnings, missing_files, scope,
                    )
                    if row is None:
                        continue
                    target.execute(insert, tuple(row.get(name) for name in columns))
                    counts[table] += 1
                    selected_table_names.add(table)

            # Add parent/child records that do not carry workspace_id by following
            # current-schema foreign keys from already scoped rows.
            child_tables = [
                table for table in table_names
                if table not in {"workspaces", "users", "sessions"}
                and "workspace_id" not in schemas[table]["columns"]
                and table in _ALLOWED_GLOBAL_CHILD_TABLES
                and not self._excluded_table(table)
            ]
            pending = set(child_tables)
            changed = True
            while pending and changed:
                changed = False
                for table in sorted(tuple(pending)):
                    if not _progress(
                        self.store, operation_id, claim,
                        completed=len(table_names) - len(pending), total=max(1, len(table_names) + len(child_tables) + 1),
                        label=f"Snapshotting related workspace history: {table}",
                    ):
                        raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                    rows, ready = self._related_child_rows(connection, target, table, schemas[table], actor_id)
                    if not ready:
                        continue
                    columns = schemas[table]["columns"]
                    placeholders = ",".join("?" for _ in columns)
                    insert = (
                        f"INSERT INTO {_identifier(table)} ({','.join(_identifier(name) for name in columns)}) "
                        f"VALUES ({placeholders})"
                    )
                    for raw in rows:
                        row = self._prepare_backup_row(
                            table, raw, source_workspace_id, actor_id, file_specs, warnings, missing_files, scope,
                        )
                        if row is None:
                            continue
                        target.execute(insert, tuple(row.get(name) for name in columns))
                        counts[table] += 1
                    pending.remove(table)
                    if rows:
                        selected_table_names.add(table)
                    changed = True
            for table in pending:
                unscoped_count = connection.execute(
                    f"SELECT COUNT(*) FROM {_identifier(table)}"
                ).fetchone()[0]
                if unscoped_count:
                    restricted[f"unresolved_global_child:{table}"] += int(unscoped_count)

            # Keep only non-secret, revoked session tombstones required by the
            # historical Browser/Intelligence rows. No original token hash is read.
            referenced_users = set()
            session_ids = set()
            for table, columns in _SESSION_REFERENCE_COLUMNS.items():
                if table not in selected_table_names:
                    continue
                for column in columns:
                    session_ids.update(
                        row[0] for row in target.execute(
                            f"SELECT DISTINCT {_identifier(column)} FROM {_identifier(table)} "
                            f"WHERE {_identifier(column)} IS NOT NULL"
                        )
                    )
            if session_ids and "sessions" in table_names:
                session_columns = schemas["sessions"]["columns"]
                now = utc_now()
                session_insert = (
                    f"INSERT INTO sessions ({','.join(_identifier(name) for name in session_columns)}) "
                    f"VALUES ({','.join('?' for _ in session_columns)})"
                )
                for session_id in sorted(session_ids):
                    old = connection.execute(
                        "SELECT id,user_id,workspace_id,created_at,expires_at FROM sessions WHERE id=? AND workspace_id=?",
                        (session_id, source_workspace_id),
                    ).fetchone()
                    if old is None:
                        continue
                    tombstone = {
                        "id": old["id"],
                        "token_hash": hashlib.sha256(f"revoked-restore-session:{operation_id}:{session_id}".encode()).hexdigest(),
                        "user_id": old["user_id"],
                        "workspace_id": source_workspace_id,
                        "created_at": old["created_at"],
                        "expires_at": old["expires_at"],
                        "revoked_at": now,
                    }
                    target.execute(session_insert, tuple(tombstone.get(name) for name in session_columns))
                    referenced_users.add(tombstone["user_id"])
                    counts["sessions_revoked_tombstones"] += 1

            # The package's users table is an opaque FK stub table, not account
            # data. It contains no credentials, email or external identity.
            for table in selected_table_names:
                for fk in schemas[table]["foreign_keys"]:
                    if fk["table"] == "users":
                        child_value = fk["from"]
                        referenced_users.update(
                            row[0] for row in target.execute(
                                f"SELECT DISTINCT {_identifier(child_value)} FROM {_identifier(table)} "
                                f"WHERE {_identifier(child_value)} IS NOT NULL"
                            )
                        )
            if "users" in table_names:
                user_columns = schemas["users"]["columns"]
                user_insert = (
                    f"INSERT INTO users ({','.join(_identifier(name) for name in user_columns)}) "
                    f"VALUES ({','.join('?' for _ in user_columns)})"
                )
                for user_id in sorted(referenced_users):
                    source_user = connection.execute(
                        "SELECT id,created_at FROM users WHERE id=?", (user_id,)
                    ).fetchone()
                    if source_user is None:
                        restricted["missing_historical_user"] += 1
                        continue
                    stub = {"id": user_id, "name": "Historical contributor", "created_at": source_user["created_at"]}
                    target.execute(user_insert, tuple(stub.get(name) for name in user_columns))

            target.commit()
            target.execute("PRAGMA foreign_keys=ON")
            fk_issues = target.execute("PRAGMA foreign_key_check").fetchall()
            if fk_issues:
                raise WorkspaceDataError("WORKSPACE_BACKUP_REFERENCE_CONFLICT", {"count": len(fk_issues)})
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise WorkspaceDataError("WORKSPACE_BACKUP_DATABASE_INTEGRITY_FAILED")
            record_db_sha, record_db_size = _file_hash(target_db)
            packaged_counts = {
                table: target.execute(f"SELECT COUNT(*) FROM {_identifier(table)}").fetchone()[0]
                for table in table_names
            }
            packaged_counts = {table: int(count) for table, count in packaged_counts.items() if count}
            if scope_report.get("scope_cycle_tables", 0):
                restricted["workspace_scope_dependency_cycle"] += int(scope_report["scope_cycle_tables"])
            if missing_files:
                restricted["missing_or_invalid_files"] = len(missing_files)
            if warnings.get("saved_file_hash_mismatch_preserved", 0):
                restricted["saved_file_hash_mismatches"] = int(warnings["saved_file_hash_mismatch_preserved"])
            coverage = {
                "status": (
                    "complete_for_accessible_workspace_scope"
                    if not scope["restricted_work_count"] and not scope["restricted_commission_count"]
                    and not scope_report.get("scope_cycle_tables", 0)
                    and not missing_files
                    and not warnings.get("saved_file_hash_mismatch_preserved", 0)
                    else "restricted_scope"
                ),
                "source_workspace_role": scope_role,
                "restricted_work_count": scope["restricted_work_count"],
                "restricted_commission_count": scope["restricted_commission_count"],
                "other_active_member_count": scope["other_active_member_count"],
                "memberships_not_reactivated": True,
            }
            excluded = dict(_EXCLUDED_TABLE_REASONS)
            for table in sorted(self._tables_for_workspace(connection, source_workspace_id)):
                if self._excluded_table(table) and table not in excluded:
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {_identifier(table)} WHERE workspace_id=?",
                        (source_workspace_id,),
                    ).fetchone()[0]
                    if count:
                        excluded[table] = f"Protected or internal owner state ({count} rows) is not copied."
            manifest = {
                "format": PACKAGE_FORMAT,
                "format_version": PACKAGE_VERSION,
                "created_at": created_at,
                "source": {
                    "workspace_id": source_workspace_id,
                    "workspace_name": scope["workspace"]["name"],
                    "actor_user_id": actor_id,
                    "membership_role": scope_role,
                },
                "schema": {
                    "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                    "table_names": sorted(table_names),
                    "tables": {table: {"rows": count, "columns": schemas[table]["columns"]}
                               for table, count in sorted(packaged_counts.items())},
                },
                "record_database": {"member": _PACKAGE_DB_MEMBER, "sha256": record_db_sha, "size_bytes": record_db_size},
                "files": [
                    {key: value for key, value in item.items() if key not in {"source_path", "source_paths"}}
                    for item in sorted(file_specs.values(), key=lambda item: item["member"])
                ],
                "missing_files": missing_files,
                "file_count": len(file_specs),
                "file_bytes": sum(item["size_bytes"] for item in file_specs.values()),
                "record_counts": dict(sorted(packaged_counts.items())),
                "coverage": coverage,
                "protected_or_unavailable": {
                    "excluded_tables": excluded,
                    "warnings": dict(warnings),
                    "restricted": dict(restricted),
                    "missing_files": missing_files,
                    "running_jobs_not_replayed": int(counts.get("jobs_restored_history", 0)),
                    "active_monitors_paused_on_restore": int(counts.get("monitor_rules", 0)),
                    "model_connections_revoked_on_restore": int(counts.get("intelligence_model_connections", 0)),
                },
                "package_expires_at": None,
            }
            return manifest, target_db, file_specs
        except Exception:
            target.close()
            connection.rollback()
            raise
        finally:
            try:
                target.close()
            except Exception:
                pass
            connection.close()

    def _tables_for_workspace(self, connection, workspace_id: str) -> set[str]:
        result = set()
        for table in _table_names(connection):
            schema = _table_schema(connection, table)
            if "workspace_id" in schema["columns"]:
                result.add(table)
        return result

    def _related_child_rows(self, connection, target, table: str, schema: dict, actor_id: str):
        groups = []
        for fk_id in sorted({item["id"] for item in schema["foreign_keys"]}):
            groups.append(sorted(
                (item for item in schema["foreign_keys"] if item["id"] == fk_id),
                key=lambda item: item["seq"],
            ))
        anchors = []
        for group in groups:
            parent_table = group[0]["table"]
            if parent_table in {"users", "sessions"}:
                continue
            if not target.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (parent_table,),
            ).fetchone():
                continue
            parent_column = group[0].get("to") or "id"
            values = [row[0] for row in target.execute(
                f"SELECT DISTINCT {_identifier(parent_column)} FROM {_identifier(parent_table)} "
                f"WHERE {_identifier(parent_column)} IS NOT NULL"
            )]
            if values:
                anchors.append((group, values))
        if not anchors:
            return [], False

        candidates = {}
        anchor_group, anchor_values = anchors[0]
        anchor_column = anchor_group[0]["from"]
        for offset in range(0, len(anchor_values), 400):
            batch = anchor_values[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            query = f"SELECT * FROM {_identifier(table)} WHERE {_identifier(anchor_column)} IN ({placeholders})"
            for row in connection.execute(query, tuple(batch)):
                materialized = dict(row)
                candidates[tuple(materialized.get(column) for column in schema["primary_key"])] = materialized

        accepted = {}
        for key, row in candidates.items():
            valid = True
            for group in groups:
                parent_table = group[0]["table"]
                child_values = [row[item["from"]] for item in group]
                if all(value is None for value in child_values):
                    continue
                if any(value is None for value in child_values):
                    valid = False
                    break
                if parent_table in {"users", "sessions"}:
                    continue
                if not target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (parent_table,),
                ).fetchone():
                    valid = False
                    break
                clauses = []
                parameters = []
                for item in group:
                    clauses.append(f"{_identifier(item.get('to') or 'id')} = ?")
                    parameters.append(row[item["from"]])
                if not target.execute(
                    f"SELECT 1 FROM {_identifier(parent_table)} WHERE {' AND '.join(clauses)} LIMIT 1",
                    tuple(parameters),
                ).fetchone():
                    valid = False
                    break
            if not valid:
                continue
            if table == "commission_members" and row.get("user_id") != actor_id:
                continue
            accepted[key] = row
        return list(accepted.values()), True

    def _write_workspace_package(self, target_path: Path, manifest: dict, records_db: Path,
                                 file_specs: dict[str, dict], operation_id: str, claim: dict) -> None:
        if target_path.is_symlink():
            raise WorkspaceDataError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            raise WorkspaceDataError("WORKSPACE_PACKAGE_CONFLICT")
        progress_total = max(1, len(file_specs) + 1)
        if not _progress(self.store, operation_id, claim, completed=0, total=progress_total,
                         label="Writing workspace database snapshot"):
            raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
        with zipfile.ZipFile(target_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
            archive.write(records_db, _PACKAGE_DB_MEMBER, compress_type=zipfile.ZIP_DEFLATED)
            for file_index, (member, item) in enumerate(sorted(file_specs.items()), start=1):
                source_path = item["source_path"]
                before = source_path.stat()
                digest = hashlib.sha256()
                copied = 0
                bytes_since_progress = 0
                with source_path.open("rb") as stream, archive.open(member, "w", force_zip64=True) as output:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        copied += len(block)
                        bytes_since_progress += len(block)
                        digest.update(block)
                        output.write(block)
                        if bytes_since_progress >= 16 * 1024 * 1024:
                            if not _progress(
                                self.store, operation_id, claim,
                                completed=file_index, total=progress_total,
                                label="Copying verified workspace files",
                            ):
                                raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                            bytes_since_progress = 0
                after = source_path.stat()
                if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                        or copied != item["size_bytes"] or digest.hexdigest() != item["sha256"]):
                    raise WorkspaceDataError("WORKSPACE_SOURCE_CHANGED_DURING_BACKUP")
                if not _progress(
                    self.store, operation_id, claim, completed=file_index,
                    total=progress_total, label="Copying verified workspace files",
                ):
                    raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
        with target_path.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        with zipfile.ZipFile(target_path, "r") as archive:
            self._validate_archive_names(archive, manifest)

    @staticmethod
    def _validate_archive_names(archive: zipfile.ZipFile, manifest: dict) -> None:
        entries = archive.infolist()
        if len(entries) > MAX_PACKAGE_ENTRIES:
            raise WorkspaceDataError("WORKSPACE_PACKAGE_ENTRY_LIMIT")
        names = set()
        total = 0
        expected_files = {item.get("member") for item in manifest.get("files", []) if isinstance(item, dict)}
        expected = {"manifest.json", _PACKAGE_DB_MEMBER, *expected_files}
        for info in entries:
            if info.is_dir() or not info.filename or "\\" in info.filename:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
            path = PurePosixPath(info.filename)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
            if info.filename in names:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_DUPLICATE_MEMBER")
            names.add(info.filename)
            if info.flag_bits & 0x1:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_ENCRYPTED_MEMBER")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_SYMLINK_BLOCKED")
            total += info.file_size
            if total > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_UNCOMPRESSED_LIMIT")
            if info.file_size > 1024 * 1024 and info.compress_size > 0 and info.file_size / info.compress_size > 500:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_COMPRESSION_RATIO_LIMIT")
            if info.filename.startswith("files/") and not _PACKAGE_FILE_RE.fullmatch(info.filename):
                raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
            if info.filename not in expected:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_MEMBER_UNEXPECTED")
        if names != expected:
            raise WorkspaceDataError("WORKSPACE_PACKAGE_MEMBER_MISSING")

    def _inspect_workspace_package(self, package_path: Path, records_db: Path,
                                   claim: dict) -> tuple[dict, dict]:
        try:
            with zipfile.ZipFile(package_path, "r") as archive:
                info_by_name = {item.filename: item for item in archive.infolist()}
                for info in info_by_name.values():
                    member = PurePosixPath(info.filename)
                    if (not info.filename or "\\" in info.filename or member.is_absolute()
                            or any(part in {"", ".", ".."} for part in member.parts)):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_PATH_INVALID")
                manifest_info = info_by_name.get("manifest.json")
                if manifest_info is None or manifest_info.file_size > MAX_PACKAGE_ROW_BYTES:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                try:
                    manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID") from exc
                if not isinstance(manifest, dict) or manifest.get("format") != PACKAGE_FORMAT or manifest.get("format_version") != PACKAGE_VERSION:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_VERSION_UNSUPPORTED")
                schema_manifest = manifest.get("schema")
                record_manifest = manifest.get("record_database")
                source_manifest = manifest.get("source")
                files = manifest.get("files")
                if (not isinstance(schema_manifest, dict) or not isinstance(record_manifest, dict)
                        or not isinstance(source_manifest, dict) or not isinstance(files, list)):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                source_workspace_id = source_manifest.get("workspace_id")
                source_actor_id = source_manifest.get("actor_user_id")
                table_names = schema_manifest.get("table_names")
                if (not isinstance(source_workspace_id, str) or not source_workspace_id
                        or not isinstance(source_actor_id, str) or not source_actor_id
                        or not isinstance(table_names, list)
                        or any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in table_names)
                        or len(table_names) != len(set(table_names))):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                if table_names != sorted(table_names):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                files_by_member = {}
                total_file_bytes = 0
                for item in files:
                    if not isinstance(item, dict):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                    member, digest, size = item.get("member"), item.get("sha256"), item.get("size_bytes")
                    if (not isinstance(member, str) or not _PACKAGE_FILE_RE.fullmatch(member)
                            or member != f"files/{digest}" or not isinstance(digest, str)
                            or not re.fullmatch(r"[a-f0-9]{64}", digest)
                            or isinstance(size, bool) or not isinstance(size, int) or size < 0
                            or size > MAX_PACKAGE_UNCOMPRESSED_BYTES):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                    if member in files_by_member:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_DUPLICATE_MEMBER")
                    expected_sha = item.get("expected_sha256")
                    if expected_sha is not None and (not isinstance(expected_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                    references = item.get("references")
                    if isinstance(references, bool) or not isinstance(references, int) or references < 1:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                    files_by_member[member] = item
                    total_file_bytes += size
                    if total_file_bytes > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_UNCOMPRESSED_LIMIT")
                missing_files = manifest.get("missing_files", [])
                if not isinstance(missing_files, list) or any(not isinstance(item, dict) for item in missing_files):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                self._validate_archive_names(archive, manifest)

                db_member = record_manifest.get("member")
                db_sha = record_manifest.get("sha256")
                db_size = record_manifest.get("size_bytes")
                if (db_member != _PACKAGE_DB_MEMBER or not isinstance(db_sha, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", db_sha)
                        or isinstance(db_size, bool) or not isinstance(db_size, int)
                        or db_size < 1 or db_size > MAX_PACKAGE_UNCOMPRESSED_BYTES):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                records_db.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied = 0
                with archive.open(db_member, "r") as source, records_db.open("xb") as target:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        copied += len(block)
                        if copied > db_size or copied > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                            raise WorkspaceDataError("WORKSPACE_PACKAGE_DATABASE_SIZE_INVALID")
                        digest.update(block)
                        target.write(block)
                if copied != db_size or digest.hexdigest() != db_sha:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_DATABASE_INTEGRITY_ERROR")

                for member, item in files_by_member.items():
                    hasher = hashlib.sha256()
                    size = 0
                    with archive.open(member, "r") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(block)
                            if size > item["size_bytes"]:
                                raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_SIZE_INVALID")
                            hasher.update(block)
                    if size != item["size_bytes"] or hasher.hexdigest() != item["sha256"]:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_INTEGRITY_ERROR")

            source_uri = records_db.resolve().as_uri() + "?mode=ro"
            records = sqlite3.connect(source_uri, uri=True, timeout=10)
            records.row_factory = sqlite3.Row
            try:
                records.execute("PRAGMA query_only=ON")
                records.execute("PRAGMA trusted_schema=OFF")
                record_tables = _table_names(records)
                if record_tables != table_names:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_SCHEMA_MISMATCH", {
                        "missing_tables": sorted(set(table_names) - set(record_tables)),
                        "unexpected_tables": sorted(set(record_tables) - set(table_names)),
                    })
                objects = records.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                ).fetchall()
                if any(
                    item["type"] != "table"
                    or not isinstance(item["sql"], str)
                    or "VIRTUAL TABLE" in item["sql"].upper()
                    or "GENERATED ALWAYS" in item["sql"].upper()
                    for item in objects
                ):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_EXECUTABLE_SCHEMA_BLOCKED")
                if records.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_DATABASE_INTEGRITY_ERROR")
                if records.execute("PRAGMA user_version").fetchone()[0] != schema_manifest.get("user_version"):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_SCHEMA_MISMATCH")
                fk_issues = records.execute("PRAGMA foreign_key_check").fetchall()
                if fk_issues:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_CONFLICT", {"count": len(fk_issues)})
                local = self.store.connect()
                try:
                    local_tables, _ = self._backup_table_names(local)
                    local_schemas = {table: _table_schema(local, table) for table in local_tables}
                    package_schemas = {table: _table_schema(records, table) for table in record_tables}
                    unsupported = sorted(set(record_tables) - set(local_tables))
                    incompatible = []
                    for table in sorted(set(record_tables) & set(local_tables)):
                        left, right = package_schemas[table], local_schemas[table]
                        if (left["columns"] != right["columns"] or left["primary_key"] != right["primary_key"]
                                or left["foreign_keys"] != right["foreign_keys"]):
                            incompatible.append(table)
                    if unsupported or incompatible:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_SCHEMA_MISMATCH", {
                            "unsupported_tables": unsupported,
                            "incompatible_tables": incompatible,
                        })
                finally:
                    local.close()

                declared_counts = manifest.get("record_counts")
                schema_counts = schema_manifest.get("tables")
                if not isinstance(declared_counts, dict) or not isinstance(schema_counts, dict):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MANIFEST_INVALID")
                actual_counts = {}
                for table in record_tables:
                    count = int(records.execute(f"SELECT COUNT(*) FROM {_identifier(table)}").fetchone()[0])
                    actual_counts[table] = count
                    if count != int(declared_counts.get(table, 0)):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_ROW_COUNT_INVALID", {"table": table})

                file_references = Counter()
                file_problems = []
                scanned_missing_file_refs = []
                hash_mismatch_count = 0
                for table, columns in _FILE_COLUMNS.items():
                    if table not in record_tables:
                        continue
                    for column in columns:
                        if column not in package_schemas[table]["columns"]:
                            continue
                        primary_key = package_schemas[table]["primary_key"]
                        expected_column = None
                        if table in {"materials", "exports", "media_renders", "delivery_batches", "media_interchanges"} and column == "relative_path":
                            expected_column = "sha256"
                        elif table == "media_uploads" and column == "original_relative_path":
                            expected_column = "expected_sha256"
                        selected = [*primary_key, column]
                        if table in {"delivery_batches", "media_interchanges"} and column == "relative_path" and "status" in package_schemas[table]["columns"]:
                            selected.append("status")
                        if expected_column and expected_column in package_schemas[table]["columns"]:
                            selected.append(expected_column)
                        for row in records.execute(
                            f"SELECT {','.join(_identifier(name) for name in dict.fromkeys(selected))} "
                            f"FROM {_identifier(table)} WHERE {_identifier(column)} IS NOT NULL"
                        ):
                            value = row[column]
                            if isinstance(value, str) and _PACKAGE_FILE_RE.fullmatch(value) and value in files_by_member:
                                file_references[value] += 1
                                expected = row[expected_column] if expected_column else None
                                if isinstance(expected, str) and expected != files_by_member[value]["sha256"]:
                                    hash_mismatch_count += 1
                            elif (table == "delivery_batches" and column == "relative_path"
                                  and row["status"] != "completed"
                                  and isinstance(value, str)
                                  and re.fullmatch(r"files/unbuilt/[a-f0-9]{64}", value)):
                                continue
                            elif isinstance(value, str) and re.fullmatch(r"files/missing/[a-f0-9]{64}", value):
                                scanned_missing_file_refs.append({
                                    "table": table,
                                    "column": column,
                                    "record_fingerprint": hashlib.sha256(_json(tuple(row[key] for key in primary_key)).encode()).hexdigest(),
                                    "reason": "file_missing_or_path_invalid",
                                })
                                continue
                            else:
                                file_problems.append({"table": table, "column": column})
                if "delivery_members" in record_tables and "file_json" in package_schemas["delivery_members"]["columns"]:
                    for row in records.execute(
                        "SELECT batch_id,member_id,file_json FROM delivery_members WHERE file_json IS NOT NULL"
                    ):
                        try:
                            member_file = json.loads(row["file_json"])
                        except (TypeError, json.JSONDecodeError):
                            file_problems.append({"table": "delivery_members", "column": "file_json"})
                            continue
                        if not isinstance(member_file, dict):
                            file_problems.append({"table": "delivery_members", "column": "file_json"})
                            continue
                        value = member_file.get("relative_path")
                        if value is None:
                            continue
                        if isinstance(value, str) and _PACKAGE_FILE_RE.fullmatch(value) and value in files_by_member:
                            file_references[value] += 1
                            expected = member_file.get("sha256")
                            if isinstance(expected, str) and expected != files_by_member[value]["sha256"]:
                                hash_mismatch_count += 1
                        elif isinstance(value, str) and re.fullmatch(r"files/missing/[a-f0-9]{64}", value):
                            scanned_missing_file_refs.append({
                                "table": "delivery_members",
                                "column": "file_json",
                                "record_fingerprint": hashlib.sha256(
                                    f"{row['batch_id']}:{row['member_id']}".encode("utf-8")
                                ).hexdigest(),
                                "reason": "file_missing_or_path_invalid",
                            })
                        else:
                            file_problems.append({"table": "delivery_members", "column": "file_json"})
                if file_problems:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_REFERENCE_INVALID", {"count": len(file_problems)})
                if len(scanned_missing_file_refs) != len(missing_files):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MISSING_FILE_MANIFEST_MISMATCH")
                if set(file_references) - set(files_by_member):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_MEMBER_MISSING")
                for member, item in files_by_member.items():
                    if file_references.get(member, 0) != item["references"]:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_REFERENCE_COUNT_INVALID")

                referenced_user_ids = set()
                for table in record_tables:
                    schema = package_schemas[table]
                    for fk in schema["foreign_keys"]:
                        if fk["table"] != "users":
                            continue
                        child_column = fk["from"]
                        referenced_user_ids.update(
                            row[0] for row in records.execute(
                                f"SELECT DISTINCT {_identifier(child_column)} FROM {_identifier(table)} "
                                f"WHERE {_identifier(child_column)} IS NOT NULL"
                            )
                        )
                source_user = source_actor_id
                missing_author_ids = sorted(value for value in referenced_user_ids if value != source_user)
                coverage = manifest.get("coverage") if isinstance(manifest.get("coverage"), dict) else {}
                protected = manifest.get("protected_or_unavailable")
                protected = protected if isinstance(protected, dict) else {}
                restricted = protected.get("restricted") if isinstance(protected.get("restricted"), dict) else {}
                restricted_count = (
                    int(coverage.get("restricted_work_count", 0) or 0)
                    + int(coverage.get("restricted_commission_count", 0) or 0)
                    + int(coverage.get("other_active_member_count", 0) or 0)
                    + sum(int(value or 0) for value in restricted.values())
                )
                missing_file_refs = list(missing_files)
                preview = {
                    "record_counts": actual_counts,
                    "file_count": len(files_by_member),
                    "file_bytes": total_file_bytes,
                    "missing_files": missing_file_refs,
                    "missing_file_count": len(missing_file_refs),
                    "hash_mismatch_count": hash_mismatch_count,
                    "missing_author_ids": [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in missing_author_ids],
                    "missing_author_count": len(missing_author_ids),
                    "restricted_scope_count": restricted_count,
                    "source_scope_status": coverage.get("status", "unknown"),
                    "protected_or_unavailable": protected,
                    "blocking_conflicts": [],
                }
                return manifest, preview
            finally:
                records.close()
        except WorkspaceDataError:
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, ValueError, TypeError, OverflowError) as exc:
            raise WorkspaceDataError("WORKSPACE_PACKAGE_INVALID") from exc

    def _execute_restore_package(self, operation_id: str, claim: dict, operation,
                                 manifest: dict, preview: dict, records_db: Path,
                                 package_path: Path) -> None:
        if preview.get("blocking_conflicts"):
            raise WorkspaceDataError("WORKSPACE_RESTORE_CONFLICTS_UNRESOLVED")
        try:
            resolutions = json.loads(operation["resolutions_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceDataError("WORKSPACE_RESTORE_RESOLUTION_INVALID") from exc
        source = manifest["source"]
        source_workspace_id = source["workspace_id"]
        source_actor_id = source["actor_user_id"]
        target_workspace_id = operation["target_workspace_id"]
        user_id = operation["owner_user_id"]
        if (not isinstance(target_workspace_id, str) or not target_workspace_id
                or not isinstance(operation["target_name"], str) or not operation["target_name"].strip()):
            raise WorkspaceDataError("WORKSPACE_RESTORE_TARGET_INVALID")
        if preview.get("missing_author_ids") and not resolutions.get("map_missing_authors_to_current_user"):
            raise WorkspaceDataError("WORKSPACE_RESTORE_AUTHOR_MAPPING_REQUIRED")
        if preview.get("missing_files") and not resolutions.get("restore_partial_content"):
            raise WorkspaceDataError("WORKSPACE_RESTORE_MISSING_FILES")
        if preview.get("hash_mismatch_count") and not resolutions.get("restore_partial_content"):
            raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_HASH_MISMATCH")
        if preview.get("restricted_scope_count") and not resolutions.get("accept_restricted_scope"):
            raise WorkspaceDataError("WORKSPACE_RESTORE_SCOPE_RESTRICTED")

        record_uri = records_db.resolve().as_uri() + "?mode=ro"
        records = sqlite3.connect(record_uri, uri=True, timeout=10)
        records.row_factory = sqlite3.Row
        records.execute("PRAGMA query_only=ON")
        records.execute("PRAGMA trusted_schema=OFF")
        staged_object_paths = []
        now = utc_now()
        try:
            with zipfile.ZipFile(package_path, "r") as archive:
                self._validate_archive_names(archive, manifest)
                files_by_member = {item["member"]: item for item in manifest["files"]}
                source_tables = _table_names(records)
                schemas = {table: _table_schema(records, table) for table in source_tables}
                order = self._restore_table_order(schemas)
                id_maps = self._restore_id_maps(
                    records, source_tables, schemas, order, target_workspace_id,
                    source_actor_id, user_id, operation_id,
                )
                media_asset_ids = self._media_asset_id_map(records, source_tables, id_maps)
                staged_object_paths = []
                restored_paths, missing_file_refs, hash_mismatch_refs = self._restore_files(
                    archive, records, source_tables, schemas, id_maps,
                    target_workspace_id, operation_id, claim, files_by_member,
                    len(preview.get("missing_files", [])), staged_object_paths,
                )
                if not _progress(self.store, operation_id, claim, completed=1, total=2,
                                 label="Installing saved workspace files"):
                    self._remove_created_files(staged_object_paths)
                    return
                self._insert_restored_records(
                    operation_id, claim, records, source_tables, schemas, order,
                    id_maps, target_workspace_id, source_workspace_id,
                    source_actor_id, user_id, operation["target_name"].strip(), now,
                    preview, resolutions, restored_paths, missing_file_refs,
                    hash_mismatch_refs, media_asset_ids,
                )
        except WorkspaceDataError:
            self._remove_created_files(staged_object_paths)
            raise
        except Exception:
            self._remove_created_files(staged_object_paths)
            raise
        finally:
            records.close()

    def _restore_table_order(self, schemas: dict[str, dict]) -> list[str]:
        tables = set(schemas)
        dependencies = {}
        for table, schema in schemas.items():
            dependencies[table] = {
                fk["table"] for fk in schema["foreign_keys"]
                if fk["table"] in tables and fk["table"] != table
            }
        if "publications" in dependencies:
            dependencies["publications"].update(
                name for name in ("artifacts", "materials", "media_projects", "media_renders") if name in tables
            )
        order = []
        pending = set(tables)
        while pending:
            ready = sorted(table for table in pending if dependencies[table].issubset(order))
            if not ready:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_CYCLE", {"tables": sorted(pending)})
            order.extend(ready)
            pending.difference_update(ready)
        return order

    def _restore_id_maps(self, records, tables: list[str], schemas: dict[str, dict], order: list[str],
                         target_workspace_id: str, source_actor_id: str, user_id: str,
                         operation_id: str) -> dict[str, dict[tuple, tuple]]:
        id_maps: dict[str, dict[tuple, tuple]] = defaultdict(dict)
        for table in order:
            schema = schemas[table]
            primary_key = schema["primary_key"]
            if not primary_key:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_PRIMARY_KEY_MISSING", {"table": table})
            fks = schema["foreign_keys"]
            groups = []
            for fk_id in sorted({fk["id"] for fk in fks}):
                group = sorted((fk for fk in fks if fk["id"] == fk_id), key=lambda item: item["seq"])
                groups.append(group)
            rows = records.execute(f"SELECT * FROM {_identifier(table)}")
            for raw in rows:
                old_pk = tuple(raw[column] for column in primary_key)
                new_pk = []
                for index, column in enumerate(primary_key):
                    value = raw[column]
                    if table == "workspaces" and column == "id":
                        new_pk.append(target_workspace_id)
                        continue
                    if table == "users" and column == "id":
                        # All historical authors are mapped to the restoring
                        # actor only after the explicit preview resolution.
                        new_pk.append(user_id)
                        continue
                    parent_match = None
                    for group in groups:
                        if not any(item["from"] == column for item in group):
                            continue
                        parent_table = group[0]["table"]
                        parent_schema = schemas.get(parent_table)
                        if parent_schema is None:
                            continue
                        parent_pk = parent_schema["primary_key"]
                        old_parent = [None] * len(parent_pk)
                        for item in group:
                            parent_column = item.get("to") or "id"
                            if parent_column not in parent_pk:
                                continue
                            parent_value = raw[item["from"]]
                            old_parent[parent_pk.index(parent_column)] = parent_value
                        if any(part is None for part in old_parent):
                            continue
                        parent_map = id_maps.get(parent_table, {}).get(tuple(old_parent))
                        if parent_map is None:
                            continue
                        fk = next(item for item in group if item["from"] == column)
                        parent_match = parent_map[parent_pk.index(fk.get("to") or "id")]
                        break
                    if parent_match is not None:
                        new_pk.append(parent_match)
                    elif column == "workspace_id":
                        new_pk.append(target_workspace_id)
                    elif schema["types"].get(column, "").upper().startswith(("INT", "REAL", "NUM", "BOOL")):
                        new_pk.append(value)
                    elif value is None:
                        new_pk.append(None)
                    else:
                        if schema["types"].get(column, "").upper().startswith(("INT", "REAL", "NUM", "BOOL")) and len(primary_key) == 1:
                            raise WorkspaceDataError(
                                "WORKSPACE_PACKAGE_NUMERIC_ID_UNSUPPORTED", {"table": table, "column": column},
                            )
                        namespace = uuid.UUID(target_workspace_id)
                        new_pk.append(str(uuid.uuid5(namespace, f"{operation_id}:{table}:{_json(old_pk)}")))
                id_maps[table][old_pk] = tuple(new_pk)
        return id_maps

    def _media_asset_id_map(self, records, tables: list[str],
                            id_maps: dict[str, dict[tuple, tuple]]) -> dict[str, str]:
        if "media_project_versions" not in tables:
            return {}
        mapping = {}
        for row in records.execute("SELECT state_json FROM media_project_versions"):
            try:
                state = json.loads(row["state_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_MEDIA_STATE_INVALID") from exc
            timeline = state.get("timeline") if isinstance(state, dict) else None
            assets = timeline.get("assets") if isinstance(timeline, dict) else None
            if not isinstance(assets, list):
                raise WorkspaceDataError("WORKSPACE_PACKAGE_MEDIA_STATE_INVALID")
            for asset in assets:
                if not isinstance(asset, dict):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MEDIA_STATE_INVALID")
                old_id = asset.get("asset_id")
                old_material_id = asset.get("source_material_id")
                digest = asset.get("sha256")
                if old_material_id is None:
                    continue
                material_map = id_maps.get("materials", {}).get((old_material_id,))
                if (not isinstance(old_id, str) or material_map is None
                        or not isinstance(digest, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_MEDIA_SOURCE_MAPPING_INVALID")
                mapping[old_id] = f"MEDIA-{digest[:16]}-{material_map[0]}"
        return mapping

    def _restore_files(self, archive: zipfile.ZipFile, records, tables: list[str], schemas: dict[str, dict],
                       id_maps: dict[str, dict[tuple, tuple]], target_workspace_id: str,
                       operation_id: str, claim: dict, files_by_member: dict[str, dict],
                       missing_reference_total: int,
                       created_paths: list[Path]) -> tuple[dict, set, set]:
        paths = {}
        missing_refs = set()
        hash_mismatches = set()
        target_root = self.store.objects.resolve()
        progress_total = max(
            1,
            sum(item.get("references", 0) for item in files_by_member.values())
            + missing_reference_total,
        )
        progress_completed = 0
        for table, columns in _FILE_COLUMNS.items():
            if table not in tables:
                continue
            schema = schemas[table]
            primary_key = schema["primary_key"]
            if not primary_key:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_PRIMARY_KEY_MISSING", {"table": table})
            selected_columns = primary_key + [column for column in columns if column in schema["columns"]]
            if table in {"delivery_batches", "media_interchanges"} and "status" in schema["columns"]:
                selected_columns.append("status")
            if table == "media_renders" and "job_id" in schema["columns"]:
                selected_columns.append("job_id")
            if table == "media_uploads" and "material_id" in schema["columns"]:
                selected_columns.append("material_id")
            query = f"SELECT {','.join(_identifier(name) for name in dict.fromkeys(selected_columns))} FROM {_identifier(table)}"
            for raw in records.execute(query):
                if not _progress(
                    self.store, operation_id, claim, completed=progress_completed,
                    total=progress_total + 1, label="Installing saved workspace files",
                ):
                    raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                old_pk = tuple(raw[column] for column in primary_key)
                new_pk = id_maps[table].get(old_pk)
                if new_pk is None:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_ROW_MAPPING_MISSING", {"table": table})
                row = dict(raw)
                if table == "workspaces" or table == "users":
                    continue
                if "workspace_id" in row:
                    row["workspace_id"] = target_workspace_id
                for column in columns:
                    if column not in row or not row[column]:
                        continue
                    if table == "delivery_batches" and column == "relative_path" and row.get("status") != "completed":
                        restored_relative = (
                            f"objects/{target_workspace_id}/restore/{operation_id}/{claim['generation']}/"
                            f"deliveries/{new_pk[0]}.zip"
                        )
                        paths[(table, old_pk, column)] = restored_relative
                        progress_completed += 1
                        continue
                    member = row[column]
                    if isinstance(member, str) and re.fullmatch(r"files/missing/([a-f0-9]{64})", member):
                        missing_refs.add((table, old_pk, column))
                        marker = member.rsplit("/", 1)[-1]
                        if table == "media_uploads" and column == "stage_relative_path":
                            restored_relative = f"objects/{target_workspace_id}/restore/{operation_id}/{claim['generation']}/media-uploads/{new_pk[0]}.part"
                        else:
                            restored_relative = f"objects/{target_workspace_id}/restore/{operation_id}/{claim['generation']}/missing/{marker}"
                        paths[(table, old_pk, column)] = restored_relative
                        progress_completed += 1
                        continue
                    if not isinstance(member, str) or not _PACKAGE_FILE_RE.fullmatch(member):
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_REFERENCE_INVALID", {"table": table})
                    metadata = files_by_member.get(member)
                    if metadata is None:
                        raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_MEMBER_MISSING", {"table": table})
                    expected_column = (
                        "sha256" if table in {"materials", "exports", "media_renders"} and column == "relative_path"
                        else "expected_sha256" if table == "media_uploads" and column == "original_relative_path"
                        else None
                    )
                    expected_hash = row.get(expected_column) if expected_column else None
                    if isinstance(expected_hash, str) and expected_hash != metadata["sha256"]:
                        hash_mismatches.add((table, old_pk, column))
                    restored_relative = self._restored_file_relative_path(
                        table, column, row, new_pk, target_workspace_id, operation_id, claim["generation"], records,
                        id_maps, old_pk,
                    )
                    target = target_root.joinpath(*PurePosixPath(restored_relative).parts[1:])
                    if not target.is_relative_to(target_root):
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
                    current = target_root
                    for part in target.relative_to(target_root).parts[:-1]:
                        current = current / part
                        if current.is_symlink():
                            raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_SYMLINK_BLOCKED")
                        current.mkdir(exist_ok=True)
                    if not target.parent.resolve().is_relative_to(target_root):
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
                    if target.is_symlink():
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_SYMLINK_BLOCKED")
                    if target.exists():
                        actual_hash, actual_size = _file_hash(target)
                        if actual_hash != metadata["sha256"] or actual_size != metadata["size_bytes"]:
                            raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                    else:
                        partial = target.with_name(f".{target.name}.{operation_id}.partial")
                        if partial.is_symlink():
                            raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                        if partial.exists():
                            # A prior attempt may have stopped while writing
                            # this operation's private temporary file.
                            partial.unlink()
                        digest = hashlib.sha256()
                        size = 0
                        try:
                            with archive.open(member, "r") as source, partial.open("xb") as output:
                                bytes_since_progress = 0
                                for block in iter(lambda: source.read(1024 * 1024), b""):
                                    size += len(block)
                                    bytes_since_progress += len(block)
                                    if size > metadata["size_bytes"]:
                                        raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_SIZE_INVALID")
                                    digest.update(block)
                                    output.write(block)
                                    if bytes_since_progress >= 16 * 1024 * 1024:
                                        if not _progress(
                                            self.store, operation_id, claim,
                                            completed=progress_completed,
                                            total=progress_total + 1,
                                            label="Installing saved workspace files",
                                        ):
                                            raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                                        bytes_since_progress = 0
                                output.flush()
                                os.fsync(output.fileno())
                            if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                                raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_INTEGRITY_ERROR")
                            # O_EXCL link creation prevents replacing another
                            # writer's object at this location.
                            try:
                                os.link(partial, target)
                                created_paths.append(target)
                            except FileExistsError:
                                actual_hash, actual_size = _file_hash(target)
                                if actual_hash != metadata["sha256"] or actual_size != metadata["size_bytes"]:
                                    raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                        finally:
                            partial.unlink(missing_ok=True)
                    paths[(table, old_pk, column)] = restored_relative
                    progress_completed += 1
        if "delivery_members" in tables:
            table = "delivery_members"
            schema = schemas[table]
            primary_key = schema["primary_key"]
            selected_columns = list(dict.fromkeys(primary_key + [
                column for column in ("batch_id", "file_json") if column in schema["columns"]
            ]))
            query = f"SELECT {','.join(_identifier(name) for name in selected_columns)} FROM delivery_members"
            for raw in records.execute(query):
                if not _progress(
                    self.store, operation_id, claim, completed=progress_completed,
                    total=progress_total + 1, label="Restoring saved delivery member files",
                ):
                    raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                row = dict(raw)
                old_pk = tuple(row[column] for column in primary_key)
                new_pk = id_maps[table].get(old_pk)
                if new_pk is None:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_ROW_MAPPING_MISSING", {"table": table})
                raw_file = row.get("file_json")
                if not isinstance(raw_file, str) or not raw_file:
                    continue
                try:
                    file_json = json.loads(raw_file)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID") from exc
                if not isinstance(file_json, dict):
                    raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID")
                member = file_json.get("relative_path")
                if not member:
                    continue
                key = (table, old_pk, "file_json")
                new_batch_id = new_pk[0]
                member_key = hashlib.sha256(_json(list(new_pk)).encode("utf-8")).hexdigest()
                restored_relative = (
                    f"objects/{target_workspace_id}/restore/{operation_id}/{claim['generation']}/"
                    f"deliveries/members/{new_batch_id}/{member_key}"
                )
                if isinstance(member, str) and re.fullmatch(r"files/missing/([a-f0-9]{64})", member):
                    missing_refs.add(key)
                    file_json["relative_path"] = restored_relative
                    paths[key] = _json(file_json)
                    progress_completed += 1
                    continue
                if not isinstance(member, str) or not _PACKAGE_FILE_RE.fullmatch(member):
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_REFERENCE_INVALID", {"table": table})
                metadata = files_by_member.get(member)
                if metadata is None:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_MEMBER_MISSING", {"table": table})
                expected_hash = file_json.get("sha256")
                expected_size = file_json.get("size_bytes")
                if (not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
                        or isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0):
                    raise WorkspaceDataError("WORKSPACE_DELIVERY_MEMBER_METADATA_INVALID")
                if (expected_hash, expected_size) != (metadata["sha256"], metadata["size_bytes"]):
                    hash_mismatches.add(key)
                target = target_root.joinpath(*PurePosixPath(restored_relative).parts[1:])
                if not target.is_relative_to(target_root):
                    raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
                current = target_root
                for part in target.relative_to(target_root).parts[:-1]:
                    current = current / part
                    if current.is_symlink():
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_SYMLINK_BLOCKED")
                    current.mkdir(exist_ok=True)
                if not target.parent.resolve().is_relative_to(target_root) or target.is_symlink():
                    raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
                if target.exists():
                    actual_hash, actual_size = _file_hash(target)
                    if (actual_hash, actual_size) != (metadata["sha256"], metadata["size_bytes"]):
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                else:
                    partial = target.with_name(f".{target.name}.{operation_id}.partial")
                    if partial.is_symlink():
                        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                    if partial.exists():
                        partial.unlink()
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        with archive.open(member, "r") as source, partial.open("xb") as output:
                            bytes_since_progress = 0
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                size += len(block)
                                bytes_since_progress += len(block)
                                if size > metadata["size_bytes"]:
                                    raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_SIZE_INVALID")
                                digest.update(block)
                                output.write(block)
                                if bytes_since_progress >= 16 * 1024 * 1024:
                                    if not _progress(
                                        self.store, operation_id, claim, completed=progress_completed,
                                        total=progress_total + 1, label="Restoring saved delivery member files",
                                    ):
                                        raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                                    bytes_since_progress = 0
                            output.flush()
                            os.fsync(output.fileno())
                        if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                            raise WorkspaceDataError("WORKSPACE_PACKAGE_FILE_INTEGRITY_ERROR")
                        try:
                            os.link(partial, target)
                            created_paths.append(target)
                        except FileExistsError:
                            actual_hash, actual_size = _file_hash(target)
                            if (actual_hash, actual_size) != (metadata["sha256"], metadata["size_bytes"]):
                                raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_CONFLICT", {"table": table})
                    finally:
                        partial.unlink(missing_ok=True)
                file_json["relative_path"] = restored_relative
                paths[key] = _json(file_json)
                progress_completed += 1
        return paths, missing_refs, hash_mismatches

    def _restored_file_relative_path(self, table: str, column: str, row: dict,
                                     new_pk: tuple, target_workspace_id: str,
                                     operation_id: str, claim_generation: int,
                                     records, id_maps: dict[str, dict[tuple, tuple]],
                                     old_pk: tuple) -> str:
        recovery_root = f"objects/{target_workspace_id}/restore/{operation_id}/{claim_generation}"
        if table == "materials":
            return f"{recovery_root}/materials/{new_pk[0]}/original"
        if table == "exports":
            extension = row.get("format")
            if not isinstance(extension, str) or not re.fullmatch(r"[a-z0-9]{1,12}", extension.lower()):
                raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
            return f"{recovery_root}/exports/{new_pk[0]}.{extension.lower()}"
        if table == "media_renders":
            purpose = "render"
            job_id = row.get("job_id")
            if job_id:
                source_job = records.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                if source_job is not None:
                    try:
                        purpose = json.loads(source_job["payload_json"] or "{}").get("purpose", "render")
                    except (TypeError, json.JSONDecodeError):
                        purpose = "render"
            directory = "media-previews" if purpose == "preview" else "media-renders"
            extension = row.get("format")
            if not isinstance(extension, str) or not re.fullmatch(r"[a-z0-9]{1,12}", extension.lower()):
                raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
            return f"{recovery_root}/{directory}/{new_pk[0]}.{extension.lower()}"
        if table == "media_uploads":
            if column == "stage_relative_path":
                return f"{recovery_root}/media-uploads/{new_pk[0]}.part"
            material_id = row.get("material_id")
            if not isinstance(material_id, str):
                raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
            material_map = id_maps.get("materials", {}).get((material_id,))
            if material_map is None:
                raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID")
            return f"{recovery_root}/materials/{material_map[0]}/original"
        if table == "delivery_batches":
            return f"{recovery_root}/deliveries/{new_pk[0]}.zip"
        if table == "media_interchanges":
            return f"{recovery_root}/media-interchanges/{new_pk[0]}.zip"
        raise WorkspaceDataError("WORKSPACE_RESTORE_FILE_PATH_INVALID", {"table": table})

    @staticmethod
    def _remove_created_files(paths: list[Path]) -> None:
        for path in reversed(paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _insert_restored_records(self, operation_id: str, claim: dict, records,
                                 tables: list[str], schemas: dict[str, dict], order: list[str],
                                 id_maps: dict[str, dict[tuple, tuple]], target_workspace_id: str,
                                 source_workspace_id: str, source_actor_id: str, user_id: str,
                                 target_name: str, now: str, preview: dict, resolutions: dict,
                                 restored_paths: dict, missing_file_refs: set,
                                 hash_mismatch_refs: set, media_asset_ids: dict[str, str]) -> None:
        from .worker import claim_can_commit

        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT o.*,j.status AS job_status,j.cancel_requested,j.lease_generation "
                "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                "WHERE o.operation_id=? AND j.id=? AND j.workspace_id=?",
                (operation_id, claim["id"], claim["workspace_id"]),
            ).fetchone()
            if current is None or not claim_can_commit(connection, claim, allow_stopping=True):
                raise WorkspaceDataError("WORKSPACE_RESTORE_JOB_LEASE_LOST")
            if current["job_status"] == "pause_requested":
                raise WorkspaceDataError("WORKSPACE_OPERATION_PAUSED")
            if current["cancel_requested"] or current["job_status"] == "cancel_requested":
                connection.execute(
                    "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=?",
                    (now, claim["id"], claim["generation"]),
                )
                connection.execute(
                    "UPDATE workspace_data_operations SET state='cancelled',updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
                raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
            if connection.execute("SELECT 1 FROM workspaces WHERE id=?", (target_workspace_id,)).fetchone():
                raise WorkspaceDataError("WORKSPACE_RESTORE_TARGET_CONFLICT")
            actor = connection.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            if actor is None:
                raise WorkspaceDataError("WORKSPACE_RESTORE_ACTOR_UNAVAILABLE")
            connection.execute(
                "INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)",
                (target_workspace_id, target_name, now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?,'owner',1,?)",
                (target_workspace_id, user_id, now),
            )
            row_total = sum(int(value) for value in preview.get("record_counts", {}).values())
            completed = 0
            for table in order:
                if table in {"workspaces", "users", "memberships", "local_identity", "workspace_data_operations"}:
                    continue
                schema = schemas[table]
                columns = schema["columns"]
                primary_key = schema["primary_key"]
                insert = (
                    f"INSERT INTO {_identifier(table)} ({','.join(_identifier(column) for column in columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})"
                )
                cursor = records.execute(f"SELECT * FROM {_identifier(table)}")
                for raw in cursor:
                    source_row = dict(raw)
                    old_pk = tuple(source_row[column] for column in primary_key)
                    row = dict(source_row)
                    for column, value in zip(primary_key, id_maps[table][old_pk]):
                        row[column] = value
                    for fk_id in sorted({item["id"] for item in schema["foreign_keys"]}):
                        group = sorted(
                            (item for item in schema["foreign_keys"] if item["id"] == fk_id),
                            key=lambda item: item["seq"],
                        )
                        if all(source_row[item["from"]] is None for item in group):
                            continue
                        if any(source_row[item["from"]] is None for item in group):
                            raise WorkspaceDataError("WORKSPACE_PACKAGE_PARTIAL_FOREIGN_KEY", {"table": table})
                        parent_table = group[0]["table"]
                        parent_schema = schemas.get(parent_table)
                        if parent_schema is None:
                            raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_CONFLICT", {"table": table})
                        parent_pk = parent_schema["primary_key"]
                        old_parent = [None] * len(parent_pk)
                        for item in group:
                            parent_column = item.get("to") or "id"
                            if parent_column not in parent_pk:
                                raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_SHAPE_UNSUPPORTED", {"table": table})
                            old_parent[parent_pk.index(parent_column)] = source_row[item["from"]]
                        new_parent = id_maps.get(parent_table, {}).get(tuple(old_parent))
                        if new_parent is None:
                            raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_CONFLICT", {"table": table})
                        for item in group:
                            parent_column = item.get("to") or "id"
                            row[item["from"]] = new_parent[parent_pk.index(parent_column)]
                    if table == "acquisition_query_paths":
                        parent_path_id = source_row.get("parent_path_id")
                        if isinstance(parent_path_id, str):
                            parent_map = id_maps.get("acquisition_query_paths", {}).get((parent_path_id,))
                            row["parent_path_id"] = None if parent_map is None else parent_map[0]
                            if parent_map is None:
                                row["status"] = "unavailable"
                                row["stop_reason"] = "QUERY_PATH_PARENT_NOT_IN_BACKUP"
                        child_job_id = source_row.get("child_job_id")
                        if isinstance(child_job_id, str):
                            child_job_map = id_maps.get("jobs", {}).get((child_job_id,))
                            row["child_job_id"] = None if child_job_map is None else child_job_map[0]
                            if child_job_map is None:
                                row["status"] = "unavailable"
                                row["stop_reason"] = "ACQUISITION_JOB_HISTORY_NOT_IN_BACKUP"
                    for column in _FILE_COLUMNS.get(table, set()) & set(columns):
                        replacement = restored_paths.get((table, old_pk, column))
                        if replacement is not None:
                            row[column] = replacement
                        if (table, old_pk, column) in missing_file_refs:
                            if table == "materials":
                                row["status"] = "error"
                                row["reason"] = "The original file is unavailable in this backup; saved text and metadata were retained."
                            elif table in {"exports", "media_renders"}:
                                row["status"] = "failed"
                                row["error_code"] = "WORKSPACE_FILE_UNAVAILABLE"
                                row["error_message"] = "The original output file is unavailable in this backup."
                            elif table == "media_interchanges":
                                row["status"] = "failed"
                                row["error_code"] = "WORKSPACE_FILE_UNAVAILABLE"
                                row["error_message"] = "The completed Media interchange ZIP was unavailable in this backup."
                        if (table, old_pk, column) in hash_mismatch_refs:
                            if table == "materials":
                                row["status"] = "error"
                                row["reason"] = "The restored original bytes differ from the saved checksum; manual review is required."
                            elif table in {"exports", "media_renders"}:
                                row["status"] = "failed"
                                row["error_code"] = "WORKSPACE_FILE_HASH_MISMATCH"
                                row["error_message"] = "The restored output bytes differ from the saved checksum."
                            elif table == "media_interchanges":
                                row["status"] = "failed"
                                row["error_code"] = "WORKSPACE_FILE_HASH_MISMATCH"
                                row["error_message"] = "The restored Media interchange ZIP differs from its saved checksum."
                            elif table == "delivery_batches":
                                row["status"] = "partial"
                                row["sha256"] = None
                                row["size_bytes"] = None
                    if table == "delivery_members":
                        restored_file = restored_paths.get((table, old_pk, "file_json"))
                        if restored_file is not None:
                            row["file_json"] = restored_file
                        if (table, old_pk, "file_json") in missing_file_refs:
                            row["status"] = "failed"
                            row["error_code"] = "DELIVERY_MEMBER_FILE_UNAVAILABLE"
                        if (table, old_pk, "file_json") in hash_mismatch_refs:
                            row["status"] = "failed"
                            row["error_code"] = "DELIVERY_MEMBER_FILE_HASH_MISMATCH"
                    for column, value in list(row.items()):
                        if not isinstance(value, str) or not (column.endswith("_json") or column in _TEXT_JSON_COLUMNS):
                            continue
                        try:
                            parsed = json.loads(value)
                        except json.JSONDecodeError:
                            continue
                        rewrite_context = column
                        if table == "acquisition_query_plans" and column == "request_json":
                            rewrite_context = "browser_query_plan"
                        elif table == "acquisition_query_paths" and row.get("path_kind") == "browser_page":
                            rewrite_context = "browser_query_path"
                        elif table == "browser_session_operations":
                            rewrite_context = "browser_operation"
                        rewritten = self._rewrite_json_references(parsed, rewrite_context, id_maps)
                        if table == "jobs" and column == "payload_json" and isinstance(rewritten, dict):
                            batch_table = {
                                "delivery_batch": "delivery_batches",
                                "revision_batch": "revision_batches",
                            }.get(row.get("kind"))
                            if batch_table is not None:
                                old_batch_id = rewritten.get("batch_id")
                                batch_map = id_maps.get(batch_table, {}).get((old_batch_id,)) if isinstance(old_batch_id, str) else None
                                if batch_map is None:
                                    raise WorkspaceDataError("WORKSPACE_PACKAGE_REFERENCE_CONFLICT", {"table": batch_table})
                                rewritten["batch_id"] = batch_map[0]
                        if table == "media_project_versions" and column == "state_json":
                            rewritten = self._rewrite_media_project_state(rewritten, media_asset_ids)
                        row[column] = _json(rewritten)
                    if table == "media_transcriptions" and isinstance(row.get("source_asset_id"), str):
                        row["source_asset_id"] = media_asset_ids.get(
                            row["source_asset_id"], row["source_asset_id"],
                        )
                    row = self._sanitize_restored_row(table, row, now, operation_id)
                    connection.execute(insert, tuple(row.get(column) for column in columns))
                    if table == "delivery_members" and row.get("status") != "completed":
                        connection.execute(
                            "UPDATE delivery_batches SET status='partial',sha256=NULL,size_bytes=NULL "
                            "WHERE id=? AND workspace_id=?",
                            (row.get("batch_id"), target_workspace_id),
                        )
                    completed += 1
                    if completed % 500 == 0:
                        state = connection.execute(
                            "SELECT status,cancel_requested,lease_generation FROM jobs WHERE id=?",
                            (claim["id"],),
                        ).fetchone()
                        if state is None or not claim_can_commit(connection, claim):
                            raise WorkspaceDataError("WORKSPACE_RESTORE_JOB_LEASE_LOST")
                        if state["status"] == "cancel_requested" or state["cancel_requested"]:
                            raise WorkspaceDataError("WORKSPACE_OPERATION_CANCELLED")
                        progress_label = f"Restoring workspace records: {table}"[:MAX_PROGRESS_LABEL]
                        connection.execute(
                            "UPDATE jobs SET progress_completed=?,progress_total=?,progress_label=?,updated_at=? "
                            "WHERE id=? AND lease_generation=? AND status='running'",
                            (completed, max(1, row_total + 2), progress_label, utc_now(), claim["id"], claim["generation"]),
                        )
                        connection.execute(
                            "UPDATE workspace_data_operations SET progress_completed=?,progress_total=?,progress_label=?,updated_at=? "
                            "WHERE operation_id=?",
                            (completed, max(1, row_total + 2), progress_label, utc_now(), operation_id),
                        )
            fk_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
            if fk_issues:
                raise WorkspaceDataError("WORKSPACE_RESTORE_REFERENCE_CONFLICT", {"count": len(fk_issues)})
            connection.execute(
                "UPDATE workspace_data_operations SET state='complete',phase='complete',progress_completed=?,"
                "progress_total=?,progress_label='Workspace restored and ready to open',error_code=NULL,"
                "updated_at=?,completed_at=? WHERE operation_id=? AND target_workspace_id=?",
                (completed, max(1, row_total + 2), now, now, operation_id, target_workspace_id),
            )
            connection.execute(
                "UPDATE jobs SET status='completed',pause_requested=0,suspended_from=NULL,progress_completed=?,progress_total=?,"
                "progress_label='Workspace restored and ready to open',error_code=NULL,error_message=NULL,updated_at=? "
                "WHERE id=? AND lease_generation=? AND status='running'",
                (completed, max(1, row_total + 2), now, claim["id"], claim["generation"]),
            )

    def _sanitize_restored_row(self, table: str, row: dict, now: str, operation_id: str) -> dict:
        result = dict(row)
        if table == "sessions":
            result["revoked_at"] = now
            result["token_hash"] = hashlib.sha256(
                f"workspace-restore-session:{operation_id}:{result.get('id')}".encode("utf-8")
            ).hexdigest()
        elif table == "browser_sessions":
            result["status"] = "closed"
            result["state_ciphertext"] = None
            result["state_protection"] = None
            result["operation_id"] = None
            result["operation_expires_at"] = None
            result["closed_at"] = result.get("closed_at") or now
            result["current_url"] = _safe_url(result.get("current_url") or "about:blank")[0]
            # The restored session is closed and has no authentication state.
            # Bind its read-only history metadata to the new owner membership
            # epoch so its owning user can still inspect the tombstone.
            if "membership_created_at" in result:
                result["membership_created_at"] = now
        elif table == "browser_session_operations":
            result["preview_json"] = None
            result["idempotency_key"] = f"restored-history:{operation_id}:{result.get('id')}"
            if result.get("status") == "pending":
                result["status"] = "unknown"
                result["error_code"] = "BROWSER_RESTORED_HISTORY_ONLY"
        elif table in {"intelligence_model_connections", "intelligence_github_connections", "intelligence_mcp_connections"}:
            result["credential_ciphertext"] = None
            result["credential_protection"] = None
            result["status"] = "revoked"
            for column in ("allow_scoped_data", "is_selected"):
                if column in result:
                    result[column] = 0
            if "provider_login" in result:
                result["provider_login"] = None
            if "validation_status" in result:
                result["validation_status"] = "invalid"
            if "authorized_use_confirmed" in result:
                result["authorized_use_confirmed"] = 0
            if "tool_count" in result:
                result["tool_count"] = 0
            if "tool_list_sha256" in result:
                result["tool_list_sha256"] = ""
            if "server_info_json" in result:
                result["server_info_json"] = "{}"
            if "capabilities_json" in result:
                result["capabilities_json"] = "{}"
            if "instructions_text" in result:
                result["instructions_text"] = None
            if isinstance(result.get("base_url"), str):
                result["base_url"] = _safe_url(result["base_url"])[0]
        elif table == "jobs":
            if result.get("status") in {"queued", "running", "cancel_requested", "retry_wait"}:
                result["status"] = "interrupted"
            if result.get("status") in {"failed", "cancelled", "interrupted"}:
                result["error_code"] = "WORKSPACE_RESTORE_HISTORY_ONLY"
                result["error_message"] = "This saved job is historical and cannot be replayed from a workspace restore."
            result["cancel_requested"] = 0
            result["lease_heartbeat_at"] = None
            if "control_revision" in result:
                result["control_revision"] = 0
            if "pause_requested" in result:
                result["pause_requested"] = 0
            if "suspended_from" in result:
                result["suspended_from"] = None
            result["idempotency_key"] = f"restored-history:{operation_id}:{result.get('id')}"
        elif table == "delivery_batches":
            if result.get("status") != "completed":
                result["status"] = "partial"
                result["sha256"] = None
                result["size_bytes"] = None
        elif table == "media_interchanges":
            result["staging_path"] = None
            if result.get("status") in {"queued", "running", "writing"}:
                result["status"] = "interrupted"
            if result.get("status") != "completed":
                result["relative_path"] = None
                result["sha256"] = None
                result["size_bytes"] = None
        elif table == "acquisition_query_paths":
            _without_browser_query_preview(result, now=now)
        elif table == "monitor_rules":
            result["status"] = "paused"
            result["next_run_at"] = None
        elif table == "monitor_sources":
            result["active"] = 0
            result["pending_job_id"] = None
        elif table == "media_recording_sessions":
            if result.get("status") not in {"interrupted", "completed", "cancelled", "failed"}:
                result["status"] = "interrupted"
            result["completion_idempotency_key"] = None
            result["idempotency_key"] = f"restored-history:{operation_id}:{result.get('id')}"
        elif table == "publications":
            # A restored item is a private draft; original revision/review
            # history is preserved without carrying publication authorization.
            result["status"] = "draft"
        elif table == "publication_releases":
            result["share_token_hash"] = None
        elif table == "intelligence_conversations":
            result["codex_thread_id"] = None
        elif table == "intelligence_skills":
            result["status"] = "disabled"
        elif table == "intelligence_skill_versions":
            result["lifecycle_state"] = "retired"
        elif table == "source_use_decisions":
            from .source_use import prepare_restored_source_use_decision
            result = prepare_restored_source_use_decision(result, now=now)
        for column, value in list(result.items()):
            if value is None:
                continue
            if (column.endswith("_json") or column in _TEXT_JSON_COLUMNS) and isinstance(value, str):
                result[column] = self._sanitize_json_column(value, column, Counter())
            elif isinstance(value, str) and column in _URL_COLUMNS:
                result[column] = _safe_url(value)[0]
            elif isinstance(value, str):
                result[column] = _safe_text(value)[0]
        return result

    def _rewrite_json_references(self, value, root_field: str,
                                 id_maps: dict[str, dict[tuple, tuple]], field_name: str | None = None):
        field_tables = {
            "artifact_id": "artifacts", "comment_id": "artifact_comments",
            "interchange_id": "media_interchanges",
            "conversation_id": "intelligence_conversations", "created_by": "users",
            "recorded_by": "users", "author_id": "users", "owner_user_id": "users",
            "browser_session_id": "browser_sessions",
            "capture_id": "browser_session_operations",
            "export_id": "exports", "job_id": "jobs", "media_project_id": "media_projects",
            "project_id": "media_projects", "material_id": "materials",
            "source_material_id": "materials", "render_material_id": "materials",
            "parent_material_id": "materials", "work_id": "works", "subject_id": "business_subjects",
            "query_work_id": "works", "query_id": "acquisition_query_plans",
            "parent_path_id": "acquisition_query_paths", "child_job_id": "jobs",
            "brand_focus_subject_id": "business_subjects", "related_subject_id": "business_subjects",
            "case_id": "business_cases", "relation_id": "business_relations",
            "publication_id": "publications", "render_id": "media_renders",
            "session_id": "sessions", "workspace_id": "workspaces",
            "evidence_id": "business_evidence", "creation_evidence_id": "business_evidence",
            "source_id": "monitor_sources", "parent_source_id": "monitor_sources",
            "child_source_id": "monitor_sources", "run_id": "monitor_runs",
            "run_source_id": "monitor_run_sources", "observation_id": "monitor_observations",
            "connection_id": "intelligence_model_connections", "skill_id": "intelligence_skills",
        }

        def map_reference(table: str, item):
            if not isinstance(item, str):
                return item
            mapped = id_maps.get(table, {}).get((item,))
            if table == "browser_sessions" and mapped is None:
                return None
            return mapped[0] if mapped else item

        if isinstance(value, dict):
            rewritten = {}
            object_table = None
            if root_field in {"target_json", "result_json"}:
                object_table = {
                    "artifact": "artifacts",
                    "media_project": "media_projects",
                }.get(value.get("kind"))
            for key, item in value.items():
                normalized = str(key).casefold()
                table = (
                    "browser_sessions"
                    if normalized == "session_id" and root_field in {"browser_query_plan", "browser_query_path", "browser_operation"}
                    else field_tables.get(normalized)
                )
                if table is not None and isinstance(item, str):
                    rewritten[key] = map_reference(table, item)
                elif normalized == "id" and object_table is not None and isinstance(item, str):
                    rewritten[key] = map_reference(object_table, item)
                elif normalized == "id" and field_name in id_maps and isinstance(item, str):
                    rewritten[key] = map_reference(field_name, item)
                else:
                    nested_context = {
                        "work": "works", "source_work": "works", "current_work": "works",
                        "material": "materials", "source_material": "materials",
                        "artifact": "artifacts", "subject": "business_subjects",
                        "subject_ref": "business_subjects",
                        "case": "business_cases", "relation": "business_relations",
                        "evidence": "business_evidence", "publication": "publications",
                        "project": "media_projects", "job": "jobs",
                        "conversation": "intelligence_conversations", "session": "sessions",
                        "render": "media_renders", "export": "exports",
                    }.get(normalized, normalized)
                    rewritten[key] = self._rewrite_json_references(item, root_field, id_maps, nested_context)
            return rewritten
        if isinstance(value, list):
            normalized = (field_name or root_field).casefold()
            if normalized in {"material_ids_json", "material_ids", "selected_material_ids", "allowed_material_ids"}:
                return [map_reference("materials", item) for item in value]
            if normalized in {"work_ids_json", "work_ids"}:
                return [map_reference("works", item) for item in value]
            return [self._rewrite_json_references(item, root_field, id_maps, field_name) for item in value]
        return value

    @staticmethod
    def _rewrite_media_project_state(value, media_asset_ids: dict[str, str]):
        if not media_asset_ids:
            return value
        media_reference_fields = {"asset_id", "media_asset_id", "source_asset_id"}

        def rewrite(item):
            if isinstance(item, dict):
                result = {}
                for key, nested in item.items():
                    if key in media_reference_fields and isinstance(nested, str):
                        result[key] = media_asset_ids.get(nested, nested)
                    else:
                        result[key] = rewrite(nested)
                return result
            if isinstance(item, list):
                return [rewrite(nested) for nested in item]
            return item

        return rewrite(value)


    def _build_workspace_snapshot(self, operation_id: str, claim: dict,
                                  staging_dir: Path) -> tuple[dict, Path, dict]:
        return self._create_filtered_record_db(operation_id, claim, staging_dir)

    def _begin_backup_protection(self, operation_id: str, claim: dict) -> bool:
        now = utc_now()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT o.owner_user_id,o.source_workspace_id,o.protection_generation,j.user_id,j.workspace_id,"
                "j.status,j.cancel_requested,j.lease_generation "
                "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                "WHERE o.operation_id=? AND o.job_id=? AND j.workspace_id=?",
                (operation_id, claim["id"], claim["workspace_id"]),
            ).fetchone()
            if (current is None or current["user_id"] != claim["user_id"]
                    or current["owner_user_id"] != claim["user_id"]
                    or current["source_workspace_id"] != claim["workspace_id"]
                    or current["workspace_id"] != claim["workspace_id"]
                    or current["lease_generation"] != claim["generation"]
                    or current["status"] != "running" or current["cancel_requested"]):
                return False
            connection.execute(
                "UPDATE workspace_data_operations SET protection_generation=?,protection_state='pending',"
                "protection_expires_at=?,protected_files_json='[]',updated_at=? WHERE operation_id=? AND job_id=?",
                (claim["generation"], _expiry_at(now, BACKUP_PROTECTION_LIFETIME), now, operation_id, claim["id"]),
            )
        return True

    def _set_backup_protected_files(self, operation_id: str, claim: dict, files: list[dict]) -> bool:
        now = utc_now()
        with self.store.transaction() as connection:
            if not self._claim_can_commit(connection, claim):
                return False
            cursor = connection.execute(
                "UPDATE workspace_data_operations SET protection_state='active',protected_files_json=?,"
                "protection_expires_at=?,updated_at=? WHERE operation_id=? AND job_id=? "
                "AND protection_generation=? AND protection_state='pending'",
                (_json(files), _expiry_at(now, BACKUP_PROTECTION_LIFETIME), now, operation_id,
                 claim["id"], claim["generation"]),
            )
        return cursor.rowcount == 1

    def _release_backup_protection_after_attempt(self, operation_id: str, claim: dict) -> None:
        now = utc_now()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT o.owner_user_id,o.source_workspace_id,o.protection_generation,j.user_id,j.workspace_id,"
                "j.status,j.lease_generation FROM workspace_data_operations o "
                "JOIN jobs j ON j.id=o.job_id WHERE o.operation_id=? AND j.id=?",
                (operation_id, claim["id"]),
            ).fetchone()
            if (current is None or current["user_id"] != claim["user_id"]
                    or current["owner_user_id"] != claim["user_id"]
                    or current["source_workspace_id"] != claim["workspace_id"]
                    or current["workspace_id"] != claim["workspace_id"]
                    or current["lease_generation"] != claim["generation"]
                    or current["protection_generation"] != claim["generation"]
                    or current["status"] not in {"completed", "failed", "cancelled", "cancel_requested"}):
                return
            connection.execute(
                "UPDATE workspace_data_operations SET protection_state='released',protection_expires_at=NULL,"
                "protected_files_json='[]',updated_at=? WHERE operation_id=? AND protection_generation=?",
                (now, operation_id, claim["generation"]),
            )

    @staticmethod
    def _claim_can_commit(connection, claim: dict, *, allow_stopping: bool = False) -> bool:
        from .worker import claim_can_commit

        return claim_can_commit(connection, claim, allow_stopping=allow_stopping)

    @staticmethod
    def _backup_snapshot_incomplete(manifest: dict) -> bool:
        protected = manifest.get("protected_or_unavailable")
        if not isinstance(protected, dict):
            return True
        warnings = protected.get("warnings") if isinstance(protected.get("warnings"), dict) else {}
        restricted = protected.get("restricted") if isinstance(protected.get("restricted"), dict) else {}
        return bool(
            manifest.get("missing_files")
            or warnings.get("saved_file_hash_mismatch_preserved")
            or warnings.get("delivery_member_file_hash_mismatch_preserved")
            or restricted.get("workspace_scope_dependency_cycle")
        )

    def process_backup_job(self, app, claim: dict) -> None:
        operation_id = None
        staging_dir = None
        target = None
        partial = None
        package_id = None
        try:
            job = self.store.row("SELECT payload_json FROM jobs WHERE id=? AND workspace_id=?",
                                 (claim["id"], claim["workspace_id"]))
            if job is None:
                return
            payload = json.loads(job["payload_json"] or "{}")
            operation_id = payload.get("operation_id")
            if not isinstance(operation_id, str) or payload.get("phase") != "backup":
                raise WorkspaceDataError("WORKSPACE_BACKUP_JOB_INVALID")
            operation = self.store.row(
                "SELECT * FROM workspace_data_operations WHERE operation_id=? AND kind='backup'",
                (operation_id,),
            )
            if operation is None or operation["job_id"] != claim["id"]:
                raise WorkspaceDataError("WORKSPACE_BACKUP_OPERATION_UNAVAILABLE")
            if not self._begin_backup_protection(operation_id, claim):
                return
            package_id = operation["package_export_id"]
            staging_candidate = self._operation_staging_dir(operation_id, claim["generation"])
            if staging_candidate.exists():
                raise WorkspaceDataError("WORKSPACE_STAGING_CONFLICT")
            staging_candidate.mkdir(parents=True, exist_ok=False)
            staging_dir = staging_candidate
            target = self._planned_account_export_path(operation["package_export_id"], claim["generation"])
            if target.is_symlink() or target.exists():
                raise WorkspaceDataError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE")
            partial = target.with_name(f".{target.name}.{claim['generation']}.partial")
            if partial.exists():
                partial.unlink()
            manifest, staged_db, package_files = self._build_workspace_snapshot(
                operation_id, claim, staging_dir,
            )
            if self._backup_snapshot_incomplete(manifest):
                self._fail_workspace_job(
                    claim, operation_id, "WORKSPACE_BACKUP_INCOMPLETE", manifest=manifest,
                )
                return
            if not _progress(self.store, operation_id, claim, completed=1, total=2,
                             label="Writing verified workspace package"):
                self._cleanup_operation_files(operation_id, staging_dir, partial)
                return
            expires_at = _expiry_at(utc_now(), PACKAGE_LIFETIME)
            manifest["package_expires_at"] = expires_at
            self._write_workspace_package(partial, manifest, staged_db, package_files, operation_id, claim)
            digest, size = _file_hash(partial)
            with zipfile.ZipFile(partial, "r") as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_READBACK_FAILED", {"member": bad_member})
                self._validate_archive_names(archive, manifest)
            created_at = utc_now()
            from .worker import claim_can_commit

            with self.store.transaction() as connection:
                current = connection.execute(
                    "SELECT o.*,j.status AS job_status,j.cancel_requested,j.pause_requested,j.lease_generation "
                    "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                    "WHERE o.operation_id=? AND j.workspace_id=?",
                    (operation_id, claim["workspace_id"]),
                ).fetchone()
                if (
                    current is None or not claim_can_commit(connection, claim, allow_stopping=True)
                    or current["cancel_requested"] or current["job_status"] == "cancel_requested"
                ):
                    connection.execute(
                        "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=?",
                        (created_at, claim["id"], claim["generation"]),
                    )
                    connection.execute(
                        "UPDATE workspace_data_operations SET state='cancelled',protection_state='released',"
                        "protection_expires_at=NULL,protected_files_json='[]',updated_at=? WHERE operation_id=?",
                        (created_at, operation_id),
                    )
                    return
                if target.is_symlink():
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE")
                try:
                    os.link(partial, target)
                except FileExistsError as exc:
                    raise WorkspaceDataError("WORKSPACE_PACKAGE_CONFLICT") from exc
                partial.unlink(missing_ok=True)
                connection.execute(
                    "INSERT INTO account_exports(id,user_id,created_at,expires_at,relative_path,sha256,size_bytes) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (current["package_export_id"], current["owner_user_id"], created_at, expires_at,
                     target.relative_to(self.store.root).as_posix(), digest, size),
                )
                manifest["package_sha256"] = digest
                manifest["package_size_bytes"] = size
                connection.execute(
                    "UPDATE workspace_data_operations SET state='backup_ready',phase='complete',package_sha256=?,"
                    "package_size_bytes=?,manifest_json=?,progress_completed=2,progress_total=2,"
                    "progress_label='Workspace backup verified and ready',error_code=NULL,protection_state='released',"
                    "protection_expires_at=NULL,protected_files_json='[]',updated_at=?,completed_at=? "
                    "WHERE operation_id=?",
                    (digest, size, _json(manifest), created_at, created_at, operation_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='completed',pause_requested=0,suspended_from=NULL,progress_completed=2,progress_total=2,"
                    "progress_label='Workspace backup verified and ready',error_code=NULL,error_message=NULL,updated_at=? "
                    "WHERE id=? AND lease_generation=?",
                    (created_at, claim["id"], claim["generation"]),
                )
        except WorkspaceDataError as exc:
            if operation_id:
                self._fail_workspace_job(claim, operation_id, exc.code)
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, TypeError):
            if operation_id:
                self._fail_workspace_job(claim, operation_id, "WORKSPACE_BACKUP_FAILED")
        finally:
            if operation_id and staging_dir is not None:
                self._cleanup_operation_files(operation_id, staging_dir, partial)
            if operation_id:
                self._release_backup_protection_after_attempt(operation_id, claim)
            if package_id:
                try:
                    export = self.store.row(
                        "SELECT 1 FROM account_exports WHERE id=? AND relative_path=?",
                        (package_id, None if target is None else target.relative_to(self.store.root).as_posix()),
                    )
                    if export is None:
                        if target is not None and target.is_file() and not target.is_symlink():
                            target.unlink(missing_ok=True)
                except (OSError, sqlite3.Error, WorkspaceDataError):
                    pass

    def process_restore_job(self, app, claim: dict) -> None:
        operation_id = None
        staging_dir = None
        try:
            job = self.store.row("SELECT payload_json FROM jobs WHERE id=? AND workspace_id=?",
                                 (claim["id"], claim["workspace_id"]))
            if job is None:
                return
            payload = json.loads(job["payload_json"] or "{}")
            operation_id = payload.get("operation_id")
            phase = payload.get("phase")
            if not isinstance(operation_id, str) or phase not in {"preview", "execute"}:
                raise WorkspaceDataError("WORKSPACE_RESTORE_JOB_INVALID")
            operation = self.store.row(
                "SELECT * FROM workspace_data_operations WHERE operation_id=? AND kind='restore'",
                (operation_id,),
            )
            if operation is None or operation["job_id"] != claim["id"]:
                raise WorkspaceDataError("WORKSPACE_RESTORE_OPERATION_UNAVAILABLE")
            package_row = self._package_export_row(operation["package_export_id"], operation["owner_user_id"])
            package_path = _safe_account_export_path(self.store, package_row["relative_path"])
            actual_hash, actual_size = _file_hash(package_path)
            if actual_hash != package_row["sha256"] or actual_size != package_row["size_bytes"]:
                raise WorkspaceDataError("WORKSPACE_PACKAGE_INTEGRITY_ERROR")
            staging_candidate = self._operation_staging_dir(operation_id, claim["generation"])
            staging_candidate.mkdir(parents=True, exist_ok=False)
            staging_dir = staging_candidate
            records_db = staging_dir / "restore-records.sqlite3"
            manifest, preview = self._inspect_workspace_package(package_path, records_db, claim)
            if not _progress(self.store, operation_id, claim, completed=1, total=2,
                             label="Workspace backup validated"):
                self._cleanup_operation_files(operation_id, staging_dir)
                return
            if phase == "preview":
                state = "preview_conflicts" if (
                    preview.get("blocking_conflicts") or preview.get("missing_files")
                    or preview.get("missing_author_ids") or preview.get("restricted_scope_count")
                    or preview.get("hash_mismatch_count")
                ) else "preview_ready"
                now = utc_now()
                from .worker import claim_can_commit

                with self.store.transaction() as connection:
                    current = connection.execute(
                        "SELECT o.*,j.status AS job_status,j.cancel_requested,j.lease_generation "
                        "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                        "WHERE o.operation_id=? AND j.workspace_id=?",
                        (operation_id, claim["workspace_id"]),
                    ).fetchone()
                    if current is None or not claim_can_commit(connection, claim, allow_stopping=True):
                        return
                    if current["cancel_requested"] or current["job_status"] == "cancel_requested":
                        connection.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=?",
                                           (now, claim["id"], claim["generation"]))
                        connection.execute("UPDATE workspace_data_operations SET state='cancelled',updated_at=? WHERE operation_id=?",
                                           (now, operation_id))
                        return
                    connection.execute(
                        "UPDATE workspace_data_operations SET state=?,phase='preview',manifest_json=?,preview_json=?,"
                        "progress_completed=2,progress_total=2,progress_label=?,error_code=NULL,updated_at=?,completed_at=? "
                        "WHERE operation_id=?",
                        (state, _json(manifest), _json(preview), "Workspace restore preview is ready", now, now, operation_id),
                    )
                    connection.execute(
                        "UPDATE jobs SET status='completed',pause_requested=0,suspended_from=NULL,progress_completed=2,progress_total=2,"
                        "progress_label='Workspace restore preview is ready',error_code=NULL,error_message=NULL,updated_at=? "
                        "WHERE id=? AND lease_generation=?",
                        (now, claim["id"], claim["generation"]),
                    )
                return
            if preview.get("blocking_conflicts"):
                raise WorkspaceDataError("WORKSPACE_RESTORE_CONFLICTS_UNRESOLVED")
            self._execute_restore_package(operation_id, claim, operation, manifest, preview, records_db, package_path)
        except WorkspaceDataError as exc:
            if operation_id:
                self._fail_workspace_job(claim, operation_id, exc.code)
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, TypeError):
            if operation_id:
                self._fail_workspace_job(claim, operation_id, "WORKSPACE_RESTORE_FAILED")
        finally:
            if operation_id and staging_dir is not None:
                self._cleanup_operation_files(operation_id, staging_dir)

    def _operation_staging_dir(self, operation_id: str, generation: int | None = None) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", operation_id):
            raise WorkspaceDataError("WORKSPACE_OPERATION_ID_INVALID")
        if generation is not None and (isinstance(generation, bool) or not isinstance(generation, int) or generation < 1):
            raise WorkspaceDataError("WORKSPACE_OPERATION_GENERATION_INVALID")
        parent = self.store.root / "tmp" / "workspace-data"
        operation_dir = parent / operation_id
        for directory in (self.store.root / "tmp", parent, operation_dir):
            if directory.is_symlink():
                raise WorkspaceDataError("WORKSPACE_STAGING_UNAVAILABLE")
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.resolve().is_relative_to(self.store.root):
                raise WorkspaceDataError("WORKSPACE_STAGING_UNAVAILABLE")
        if generation is None:
            return operation_dir.resolve()
        attempt_dir = operation_dir / str(generation)
        if attempt_dir.is_symlink():
            raise WorkspaceDataError("WORKSPACE_STAGING_UNAVAILABLE")
        return attempt_dir

    def _planned_account_export_path(self, export_id: str | None, generation: int | None = None) -> Path:
        if not isinstance(export_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", export_id):
            raise WorkspaceDataError("WORKSPACE_PACKAGE_ID_INVALID")
        if generation is not None and (isinstance(generation, bool) or not isinstance(generation, int) or generation < 1):
            raise WorkspaceDataError("WORKSPACE_PACKAGE_GENERATION_INVALID")
        parent = self.store.root / "account-exports"
        if parent.is_symlink():
            raise WorkspaceDataError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE")
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.resolve().is_relative_to(self.store.root):
            raise WorkspaceDataError("WORKSPACE_PACKAGE_STORAGE_UNAVAILABLE")
        suffix = "" if generation is None else f".{generation}"
        return parent.resolve() / f"{export_id}{suffix}.zip"

    def _package_export_row(self, export_id: str | None, user_id: str):
        if not export_id:
            raise WorkspaceDataError("WORKSPACE_PACKAGE_UNAVAILABLE")
        row = self.store.row(
            "SELECT * FROM account_exports WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?",
            (export_id, user_id, utc_now()),
        )
        if row is None:
            raise WorkspaceDataError("WORKSPACE_PACKAGE_EXPIRED")
        return row

    def _cleanup_operation_files(self, operation_id: str, staging_dir: Path | None,
                                 partial: Path | None = None) -> None:
        for candidate in [partial]:
            if candidate is None:
                continue
            try:
                expected_parent = (self.store.root / "account-exports").resolve()
                resolved_parent = candidate.parent.resolve()
                if (resolved_parent == expected_parent and candidate.name.startswith(".")
                        and candidate.name.endswith(".partial") and not candidate.is_symlink()):
                    candidate.unlink(missing_ok=True)
            except OSError:
                pass
        if staging_dir is not None:
            try:
                expected = self._operation_staging_dir(operation_id).resolve()
                resolved = staging_dir.resolve(strict=False)
                if (resolved.parent == expected and resolved.name.isdigit()
                        and resolved.is_relative_to(expected)):
                    shutil.rmtree(resolved, ignore_errors=True)
            except (OSError, ValueError):
                pass

    def _fail_workspace_job(self, claim: dict, operation_id: str, code: str,
                            *, manifest: dict | None = None) -> None:
        from .worker import claim_can_commit

        now = utc_now()
        with self.store.transaction() as connection:
            job = connection.execute(
                "SELECT status,cancel_requested,pause_requested,lease_generation FROM jobs WHERE id=? AND workspace_id=?",
                (claim["id"], claim["workspace_id"]),
            ).fetchone()
            if job is None or not claim_can_commit(connection, claim, allow_stopping=True):
                return
            if job["cancel_requested"] or job["status"] == "cancel_requested":
                connection.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=?",
                                   (now, claim["id"], claim["generation"]))
                connection.execute("UPDATE workspace_data_operations SET state='cancelled',protection_state='released',"
                                   "protection_expires_at=NULL,protected_files_json='[]',updated_at=? WHERE operation_id=?",
                                   (now, operation_id))
                return
            if job["pause_requested"] or job["status"] == "pause_requested":
                return
            if not claim_can_commit(connection, claim):
                return
            message = "The workspace backup/restore step failed. The source workspace was preserved; review and resume the operation."
            connection.execute(
                "UPDATE jobs SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND lease_generation=?",
                (code, message, now, claim["id"], claim["generation"]),
            )
            if manifest is None:
                connection.execute(
                    "UPDATE workspace_data_operations SET state='failed',error_code=?,protection_state='released',"
                    "protection_expires_at=NULL,protected_files_json='[]',updated_at=? WHERE operation_id=?",
                    (code, now, operation_id),
                )
            else:
                connection.execute(
                    "UPDATE workspace_data_operations SET state='failed',error_code=?,manifest_json=?,"
                    "protection_state='released',protection_expires_at=NULL,protected_files_json='[]',"
                    "updated_at=? WHERE operation_id=?",
                    (code, _json(manifest), now, operation_id),
                )


def register_workspace_data_routes(app, store) -> None:
    install_workspace_data_schema(store)
    service = WorkspaceDataService(store)
    app.extensions["oevira.workspace_data"] = service
    resume_validators = app.extensions.setdefault("oevira.job_resume_validators", {})
    resume_validators["workspace_backup"] = service.validate_backup_resume
    resume_validators["workspace_restore"] = service.validate_restore_resume
    app.extensions.setdefault("oevira.job_cancelled_retry_kinds", set()).add("workspace_backup")

    def control_operation_job(operation_id: str, requested_action: str, base_control_revision: Any) -> dict:
        from .job_control import control_job
        from .worker import install_worker_schema

        install_worker_schema(store)
        operation = service.operation(g.identity, operation_id)
        job_data = operation.get("job")
        if not isinstance(job_data, dict) or not isinstance(job_data.get("id"), str):
            raise ApiError("WORKSPACE_OPERATION_STATE_UNAVAILABLE", "The operation job is unavailable", 409)
        job = store.row(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
            (job_data["id"], operation["source_workspace_id"]),
        )
        if job is None:
            raise ApiError("WORKSPACE_OPERATION_STATE_UNAVAILABLE", "The operation job is unavailable", 409)
        if type(base_control_revision) is not int or base_control_revision < 0:
            raise ApiError("INVALID_INPUT", "base_control_revision must be a nonnegative integer", 400)
        control_operation_id = _idempotency_key()
        action = requested_action
        prior_receipt = store.row(
            "SELECT action FROM job_control_receipts WHERE workspace_id=? AND operation_id=?",
            (operation["source_workspace_id"], control_operation_id),
        )
        if prior_receipt is not None:
            if requested_action == "resume" and prior_receipt["action"] in {"resume", "retry"}:
                action = prior_receipt["action"]
        elif requested_action == "resume":
            if job["status"] == "paused" or (job["status"] == "interrupted" and bool(job["pause_requested"])):
                action = "resume"
            elif job["status"] in {"failed", "interrupted"} and not bool(job["pause_requested"]):
                action = "retry"
            elif job["status"] == "cancelled" and operation["kind"] == "backup":
                action = "retry"
            else:
                raise ApiError("WORKSPACE_OPERATION_NOT_RESUMABLE", "The saved operation is not safely resumable", 409)
        elif requested_action == "cancel":
            if job["status"] not in {"queued", "running", "pause_requested", "paused", "cancel_requested"}:
                raise ApiError("WORKSPACE_OPERATION_NOT_CANCELLABLE", "The workspace data operation is already terminal", 409)
        else:
            raise ApiError("INVALID_INPUT", "Unknown workspace data control action", 400)
        controlled = control_job(
            app, store, g.identity, job["id"], action,
            {"operation_id": control_operation_id, "base_control_revision": base_control_revision},
        )
        updated = service.operation(g.identity, operation_id)
        return {"operation": updated, "job": controlled, "replayed": controlled.get("control_replayed") is True}

    @app.post("/api/workspace-data/backups")
    def create_workspace_backup():
        return _success(service.create_backup(g.identity, _idempotency_key()), 202)

    @app.post("/api/workspace-data/restores")
    def upload_workspace_restore():
        return _success(service.upload_restore(g.identity, _idempotency_key()), 202)

    @app.get("/api/workspace-data/operations")
    def list_workspace_data_operations():
        return _success(service.list_operations(g.identity))

    @app.get("/api/workspace-data/operations/<operation_id>")
    def get_workspace_data_operation(operation_id: str):
        return _success(service.operation(g.identity, operation_id))

    @app.get("/api/workspace-data/operations/<operation_id>/package")
    def download_workspace_data_package(operation_id: str):
        return service.download_package(g.identity, operation_id)

    @app.post("/api/workspace-data/operations/<operation_id>/cancel")
    def cancel_workspace_data_operation(operation_id: str):
        data = _body()
        if set(data) - {"base_control_revision"}:
            raise ApiError("INVALID_INPUT", "Workspace data control contains an unsupported field", 400)
        return _success(control_operation_job(operation_id, "cancel", data.get("base_control_revision")))

    @app.post("/api/workspace-data/operations/<operation_id>/resume")
    def resume_workspace_data_operation(operation_id: str):
        data = _body()
        if set(data) - {"base_control_revision"}:
            raise ApiError("INVALID_INPUT", "Workspace data control contains an unsupported field", 400)
        return _success(control_operation_job(operation_id, "resume", data.get("base_control_revision")), 202)

    @app.post("/api/workspace-data/operations/<operation_id>/restore")
    def execute_workspace_restore(operation_id: str):
        return _success(
            service.execute_restore(g.identity, operation_id, _body(), _idempotency_key()),
            202,
        )

    @app.post("/api/workspace-data/operations/<operation_id>/activate")
    def activate_restored_workspace(operation_id: str):
        return _success(service.activate_restored_workspace(g.identity, operation_id))

