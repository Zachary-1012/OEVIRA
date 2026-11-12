"""User-owned, encrypted model connections and exact scoped inference grants."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import time
import uuid

from flask import Flask, g, has_request_context, request

from . import api as _api
from .auth import utc_now
from .intelligence_base import IntelligenceError, identity_scope
from .intelligence_provider import (
    CHAT_COMPLETIONS_PROTOCOL,
    MAX_RESPONSE_BYTES,
    RESPONSES_PROTOCOL,
    UserConfiguredChatGateway,
    UserConfiguredModelConnection,
    _open_pinned_https,
    _connection_destination,
)


_MODEL_NAME = re.compile(r"^[^\x00-\x20\x7f]{1,200}$")
_PROFILE_NAME_MAX = 80
_CONNECTION_TEST_TIMEOUT = 60


class ModelSettingsError(RuntimeError):
    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


INTELLIGENCE_MODEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_model_connections (
    connection_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK(protocol IN ('openai_chat_completions', 'openai_responses')),
    token_field TEXT NOT NULL DEFAULT 'max_tokens' CHECK(token_field IN ('max_tokens', 'max_completion_tokens', 'not_applicable')),
    model_id TEXT NOT NULL,
    credential_ciphertext BLOB,
    credential_protection TEXT,
    allow_scoped_data INTEGER NOT NULL DEFAULT 0 CHECK(allow_scoped_data IN (0, 1)),
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('configured', 'revoked')),
    revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    CHECK((status = 'configured' AND credential_ciphertext IS NOT NULL AND credential_protection IS NOT NULL)
       OR (status = 'revoked' AND credential_ciphertext IS NULL AND credential_protection IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS intelligence_model_connection_selected
    ON intelligence_model_connections(workspace_id, owner_user_id)
    WHERE is_selected = 1 AND status = 'configured';
CREATE INDEX IF NOT EXISTS intelligence_model_connections_scope
    ON intelligence_model_connections(workspace_id, owner_user_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS intelligence_model_events (
    event_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    connection_id TEXT,
    action TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('completed', 'failed', 'unknown', 'pending')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, owner_user_id, operation_key)
);
CREATE INDEX IF NOT EXISTS intelligence_model_events_connection
    ON intelligence_model_events(workspace_id, connection_id, created_at DESC);

CREATE TABLE IF NOT EXISTS intelligence_model_admissions (
    admission_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    connection_id TEXT NOT NULL REFERENCES intelligence_model_connections(connection_id),
    selection_revision TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('inference', 'provider_request')),
    recipient_id TEXT NOT NULL,
    source_ref_sha256 TEXT NOT NULL,
    request_sha256 TEXT,
    state TEXT NOT NULL CHECK(state IN ('source_authorized', 'request_approved')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS intelligence_model_admissions_recent
    ON intelligence_model_admissions(workspace_id, owner_user_id, connection_id, created_at DESC);
"""


def install_intelligence_model_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + INTELLIGENCE_MODEL_SCHEMA + "\nCOMMIT;")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(intelligence_model_connections)")}
        if "token_field" not in columns:
            connection.execute(
                "ALTER TABLE intelligence_model_connections ADD COLUMN token_field TEXT NOT NULL DEFAULT 'max_tokens'"
            )
    finally:
        connection.close()


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelSettingsError("MODEL_SETTINGS_REQUEST_INVALID", 400) from exc


def _sha256(value) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entropy(workspace_id: str, user_id: str, connection_id: str) -> bytes:
    return hashlib.sha256(
        f"oevira-model-credential-v1\0{workspace_id}\0{user_id}\0{connection_id}".encode("utf-8"),
    ).digest()


class IntelligenceModelSettings:
    """One model connection list per authenticated workspace user."""

    def __init__(self, store, *, protector=None, chat_test_runner=None):
        self.store = store
        self.protector = protector
        self.chat_test_runner = chat_test_runner

    @staticmethod
    def credential_protection_status() -> str:
        return "WINDOWS_DPAPI_CURRENT_USER" if os.name == "nt" else "UNAVAILABLE_REQUIRES_OS_KEY_STORE"

    @staticmethod
    def _identity(connection, identity: dict) -> None:
        from .intelligence import _verify_actor
        _verify_actor(connection, identity)

    def _protector(self):
        if self.protector is not None:
            return self.protector
        try:
            # This is the existing Windows DPAPI current-user protector. The
            # import defines the primitive only; it does not launch a browser.
            from .browser import WindowsUserStateProtector
            protector = WindowsUserStateProtector()
        except Exception as exc:
            raise ModelSettingsError("MODEL_CREDENTIAL_PROTECTION_UNAVAILABLE", 503) from exc
        self.protector = protector
        return protector

    def _protect(self, value: str, identity: dict, connection_id: str) -> bytes:
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            return self._protector().protect(value.encode("utf-8"), _entropy(workspace_id, user_id, connection_id))
        except ModelSettingsError:
            raise
        except Exception as exc:
            raise ModelSettingsError("MODEL_CREDENTIAL_PROTECTION_FAILED", 503) from exc

    def _unprotect(self, value: bytes, identity: dict, connection_id: str) -> str:
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            raw = self._protector().unprotect(bytes(value), _entropy(workspace_id, user_id, connection_id))
            text = raw.decode("utf-8")
        except Exception as exc:
            raise ModelSettingsError("MODEL_CREDENTIAL_DECRYPTION_FAILED", 503) from exc
        if not text or len(text) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ModelSettingsError("MODEL_CREDENTIAL_DECRYPTION_FAILED", 503)
        return text

    @staticmethod
    def _require_write(identity: dict) -> None:
        if identity.get("role") not in {"owner", "editor"}:
            raise ModelSettingsError("WRITE_ACCESS_DENIED", 403)

    @staticmethod
    def _normalize_configuration(data: dict, *, require_key: bool) -> dict:
        allowed = {"name", "base_url", "model_id", "api_key", "protocol", "token_field"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ModelSettingsError("MODEL_SETTINGS_FIELDS_INVALID", 400)
        name, base_url, model_id = data.get("name"), data.get("base_url"), data.get("model_id")
        protocol = data.get("protocol", CHAT_COMPLETIONS_PROTOCOL)
        api_key = data.get("api_key")
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > _PROFILE_NAME_MAX
                or any(ord(c) < 32 for c in name)):
            raise ModelSettingsError("MODEL_PROFILE_NAME_INVALID", 400)
        if not isinstance(model_id, str) or not _MODEL_NAME.fullmatch(model_id) or model_id != model_id.strip():
            raise ModelSettingsError("USER_MODEL_MODEL_INVALID", 400)
        if protocol not in {CHAT_COMPLETIONS_PROTOCOL, RESPONSES_PROTOCOL}:
            raise ModelSettingsError("USER_MODEL_PROTOCOL_NOT_ADAPTED", 409)
        endpoint = "responses" if protocol == RESPONSES_PROTOCOL else "chat/completions"
        try:
            destination, hostname, _port, endpoint_path = _connection_destination(base_url, endpoint=endpoint)
        except IntelligenceError as exc:
            raise ModelSettingsError(exc.code, exc.status, exc.details) from exc
        token_field = data.get("token_field")
        if protocol == RESPONSES_PROTOCOL:
            if token_field not in {None, "not_applicable"}:
                raise ModelSettingsError("USER_MODEL_TOKEN_FIELD_NOT_APPLICABLE", 400)
            token_field = "not_applicable"
        elif token_field is None:
            token_field = {
                "api.openai.com": "max_completion_tokens",
                "api.groq.com": "max_tokens",
                "api.deepseek.com": "max_tokens",
            }.get(hostname, "max_tokens")
        if protocol == CHAT_COMPLETIONS_PROTOCOL and token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ModelSettingsError("USER_MODEL_TOKEN_FIELD_NOT_ADAPTED", 409)
        base_path = endpoint_path[:-len("/" + endpoint)]
        canonical_base = "https://" + hostname + base_path
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key or len(api_key) > 8192
            or any(ord(c) < 32 or ord(c) == 127 for c in api_key)
        ):
            raise ModelSettingsError("USER_MODEL_KEY_INVALID", 400)
        if require_key and not api_key:
            raise ModelSettingsError("USER_MODEL_KEY_REQUIRED", 400)
        return {
            "name": name.strip(), "base_url": canonical_base, "destination": destination,
            "model_id": model_id, "api_key": api_key, "protocol": protocol,
            "recipient_id": destination, "token_field": token_field,
        }

    @staticmethod
    def _safe_request_fingerprint(action: str, data: dict) -> str:
        safe = {key: value for key, value in data.items() if key != "api_key"}
        if "api_key" in data:
            safe["api_key_supplied"] = bool(data["api_key"])
        return _sha256(_canonical({"action": action, "request": safe}))

    def _event_replay(self, connection, identity: dict, operation_key: str, request_sha: str):
        workspace_id, user_id, _ = identity_scope(identity)
        existing = connection.execute(
            "SELECT request_sha256, state, result_json FROM intelligence_model_events "
            "WHERE workspace_id = ? AND owner_user_id = ? AND operation_key = ?",
            (workspace_id, user_id, operation_key),
        ).fetchone()
        if existing is None:
            return None
        if existing["request_sha256"] != request_sha:
            raise ModelSettingsError("IDEMPOTENCY_CONFLICT", 409)
        try:
            result = json.loads(existing["result_json"])
        except (ValueError, TypeError) as exc:
            raise ModelSettingsError("MODEL_SETTINGS_EVENT_UNAVAILABLE", 409) from exc
        if existing["state"] == "pending":
            raise ModelSettingsError("MODEL_CONNECTION_TEST_OUTCOME_UNKNOWN", 409)
        return result | {"replayed": True}

    @staticmethod
    def _record_event(connection, identity: dict, connection_id: str | None, action: str,
                      operation_key: str, request_sha: str, result: dict, *, state: str = "completed") -> None:
        workspace_id, user_id, _ = identity_scope(identity)
        now = utc_now()
        connection.execute(
            "INSERT INTO intelligence_model_events(event_id, workspace_id, owner_user_id, connection_id, action, "
            "operation_key, request_sha256, state, result_json, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), workspace_id, user_id, connection_id, action, operation_key, request_sha,
             state, json.dumps(result, ensure_ascii=False, sort_keys=True), now, now),
        )

    def _mutate(self, identity: dict, action: str, data: dict, operation_key: str, handler) -> dict:
        self._require_write(identity)
        if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 200:
            raise ModelSettingsError("IDEMPOTENCY_KEY_REQUIRED", 400)
        workspace_id, user_id, _ = identity_scope(identity)
        request_sha = self._safe_request_fingerprint(action, data)
        with self.store.transaction() as connection:
            self._identity(connection, identity)
            replay = self._event_replay(connection, identity, operation_key, request_sha)
            if replay is not None:
                return replay
            result = handler(connection)
            if not isinstance(result, dict):
                raise ModelSettingsError("MODEL_SETTINGS_RESULT_INVALID", 500)
            self._record_event(connection, identity, result.get("connection_id"), action,
                               operation_key, request_sha, result)
            return result | {"replayed": False}

    def create(self, identity: dict, data: dict, *, operation_key: str) -> dict:
        normalized = self._normalize_configuration(data, require_key=True)
        connection_id = str(uuid.uuid4())
        ciphertext = self._protect(normalized["api_key"], identity, connection_id)
        workspace_id, user_id, _ = identity_scope(identity)
        revision = str(uuid.uuid4())
        def apply(connection):
            duplicate = connection.execute(
                "SELECT 1 FROM intelligence_model_connections WHERE workspace_id = ? AND owner_user_id = ? "
                "AND name = ? AND status = 'configured' LIMIT 1",
                (workspace_id, user_id, normalized["name"]),
            ).fetchone()
            if duplicate:
                raise ModelSettingsError("MODEL_PROFILE_NAME_ALREADY_USED", 409)
            now = utc_now()
            connection.execute(
                "INSERT INTO intelligence_model_connections(connection_id, workspace_id, owner_user_id, name, base_url, "
                "recipient_id, protocol, token_field, model_id, credential_ciphertext, credential_protection, allow_scoped_data, "
                "is_selected, status, revision, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'configured', ?, ?, ?, ?)",
                (connection_id, workspace_id, user_id, normalized["name"], normalized["base_url"],
                 normalized["recipient_id"], normalized["protocol"], normalized["token_field"],
                 normalized["model_id"], ciphertext,
                 "windows_dpapi_current_user_v1", revision, now, now, user_id),
            )
            return {"connection_id": connection_id, "name": normalized["name"], "model_id": normalized["model_id"],
                    "protocol": normalized["protocol"], "status": "configured", "selected": False,
                    "allow_scoped_data": False, "credential_present": True, "revision": revision,
                    "token_field": normalized["token_field"]}
        return self._mutate(identity, "create", data, operation_key, apply)

    def update(self, identity: dict, connection_id: str, data: dict, *, operation_key: str) -> dict:
        normalized = self._normalize_configuration(data, require_key=False)
        workspace_id, user_id, _ = identity_scope(identity)
        def apply(connection):
            row = self._owned_row(connection, identity, connection_id)
            if row["status"] != "configured":
                raise ModelSettingsError("MODEL_CONNECTION_REVOKED", 409)
            if normalized["base_url"] != row["base_url"] and not normalized["api_key"]:
                raise ModelSettingsError("MODEL_KEY_REQUIRED_FOR_DESTINATION_CHANGE", 400)
            ciphertext = row["credential_ciphertext"]
            protection = row["credential_protection"]
            if normalized["api_key"]:
                ciphertext = self._protect(normalized["api_key"], identity, connection_id)
                protection = "windows_dpapi_current_user_v1"
            if normalized["name"] != row["name"]:
                duplicate = connection.execute(
                    "SELECT 1 FROM intelligence_model_connections WHERE workspace_id = ? AND owner_user_id = ? "
                    "AND name = ? AND connection_id != ? AND status = 'configured' LIMIT 1",
                    (workspace_id, user_id, normalized["name"], connection_id),
                ).fetchone()
                if duplicate:
                    raise ModelSettingsError("MODEL_PROFILE_NAME_ALREADY_USED", 409)
            revision = str(uuid.uuid4())
            connection.execute(
                "UPDATE intelligence_model_connections SET name = ?, base_url = ?, recipient_id = ?, protocol = ?, token_field = ?, "
                "model_id = ?, credential_ciphertext = ?, credential_protection = ?, revision = ?, updated_at = ? "
                "WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ? AND status = 'configured'",
                (normalized["name"], normalized["base_url"], normalized["recipient_id"], normalized["protocol"], normalized["token_field"],
                 normalized["model_id"], ciphertext, protection, revision, utc_now(), connection_id, workspace_id, user_id),
            )
            return {"connection_id": connection_id, "name": normalized["name"], "model_id": normalized["model_id"],
                    "protocol": normalized["protocol"], "status": "configured", "selected": row["is_selected"] == 1,
                    "allow_scoped_data": row["allow_scoped_data"] == 1, "credential_present": True, "revision": revision,
                    "token_field": normalized["token_field"]}
        return self._mutate(identity, "update", {"connection_id": connection_id, **data}, operation_key, apply)

    def select(self, identity: dict, connection_id: str, *, operation_key: str) -> dict:
        workspace_id, user_id, _ = identity_scope(identity)
        data = {"connection_id": connection_id}
        def apply(connection):
            row = self._owned_row(connection, identity, connection_id)
            if row["status"] != "configured" or row["credential_ciphertext"] is None:
                raise ModelSettingsError("MODEL_CONNECTION_UNAVAILABLE", 409)
            connection.execute(
                "UPDATE intelligence_model_connections SET is_selected = 0, revision = ?, updated_at = ? "
                "WHERE workspace_id = ? AND owner_user_id = ? AND status = 'configured' AND is_selected = 1",
                (str(uuid.uuid4()), utc_now(), workspace_id, user_id),
            )
            revision = str(uuid.uuid4())
            connection.execute(
                "UPDATE intelligence_model_connections SET is_selected = 1, revision = ?, updated_at = ? "
                "WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ? AND status = 'configured'",
                (revision, utc_now(), connection_id, workspace_id, user_id),
            )
            return {"connection_id": connection_id, "selected": True, "revision": revision,
                    "model_id": row["model_id"], "recipient_id": row["recipient_id"]}
        return self._mutate(identity, "select", data, operation_key, apply)

    def set_data_use(self, identity: dict, connection_id: str, allowed: bool, *, operation_key: str) -> dict:
        if type(allowed) is not bool:
            raise ModelSettingsError("MODEL_DATA_PERMISSION_INVALID", 400)
        workspace_id, user_id, _ = identity_scope(identity)
        data = {"connection_id": connection_id, "allow_scoped_data": allowed}
        def apply(connection):
            row = self._owned_row(connection, identity, connection_id)
            if row["status"] != "configured":
                raise ModelSettingsError("MODEL_CONNECTION_REVOKED", 409)
            revision = str(uuid.uuid4())
            connection.execute(
                "UPDATE intelligence_model_connections SET allow_scoped_data = ?, revision = ?, updated_at = ? "
                "WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ? AND status = 'configured'",
                (int(allowed), revision, utc_now(), connection_id, workspace_id, user_id),
            )
            return {"connection_id": connection_id, "allow_scoped_data": allowed, "revision": revision,
                    "selection_invalidated": row["is_selected"] == 1}
        return self._mutate(identity, "set_data_use", data, operation_key, apply)

    def revoke(self, identity: dict, connection_id: str, *, operation_key: str) -> dict:
        workspace_id, user_id, _ = identity_scope(identity)
        data = {"connection_id": connection_id}
        def apply(connection):
            row = self._owned_row(connection, identity, connection_id)
            if row["status"] == "revoked":
                return {"connection_id": connection_id, "status": "revoked", "selected": False,
                        "credential_removed": True}
            connection.execute(
                "UPDATE intelligence_model_connections SET credential_ciphertext = NULL, credential_protection = NULL, "
                "allow_scoped_data = 0, is_selected = 0, status = 'revoked', revision = ?, updated_at = ? "
                "WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ?",
                (str(uuid.uuid4()), utc_now(), connection_id, workspace_id, user_id),
            )
            return {"connection_id": connection_id, "status": "revoked", "selected": False,
                    "credential_removed": True, "future_requests_denied": True}
        return self._mutate(identity, "revoke", data, operation_key, apply)

    def list_connections(self, identity: dict) -> list[dict]:
        workspace_id, user_id, _ = identity_scope(identity)
        with self.store.transaction(immediate=False) as connection:
            self._identity(connection, identity)
            rows = connection.execute(
                "SELECT * FROM intelligence_model_connections WHERE workspace_id = ? AND owner_user_id = ? "
                "ORDER BY is_selected DESC, updated_at DESC, name COLLATE NOCASE",
                (workspace_id, user_id),
            ).fetchall()
            result = []
            for row in rows:
                latest_test = connection.execute(
                    "SELECT state, result_json, created_at FROM intelligence_model_events "
                    "WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ? AND action = 'test' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["connection_id"], workspace_id, user_id),
                ).fetchone()
                result.append(self._summary(row, latest_test))
            return result

    @staticmethod
    def _summary(row, latest_test=None) -> dict:
        test = None
        if latest_test is not None:
            try:
                payload = json.loads(latest_test["result_json"])
            except (TypeError, ValueError):
                payload = {}
            test = {
                "state": payload.get("state", latest_test["state"]),
                "created_at": latest_test["created_at"],
                "reason": payload.get("reason"),
            }
        return {
            "connection_id": row["connection_id"], "name": row["name"], "base_url": row["base_url"],
            "recipient_id": row["recipient_id"], "protocol": row["protocol"], "model_id": row["model_id"],
            "token_field": row["token_field"],
            "credential_present": row["credential_ciphertext"] is not None,
            "credential_protection": row["credential_protection"],
            "allow_scoped_data": row["allow_scoped_data"] == 1,
            "selected": row["is_selected"] == 1, "status": row["status"],
            "revision": row["revision"], "connection_test": test,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def resolve_selection(self, identity: dict, selection_id: str | None):
        from .intelligence import ExplicitModelSelection
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            with self.store.transaction(immediate=False) as connection:
                self._identity(connection, identity)
                row = connection.execute(
                    "SELECT * FROM intelligence_model_connections WHERE workspace_id = ? AND owner_user_id = ? "
                    "AND is_selected = 1 AND status = 'configured'",
                    (workspace_id, user_id),
                ).fetchone()
                if row is None or selection_id is not None and row["connection_id"] != selection_id:
                    return None
                if row["credential_ciphertext"] is None:
                    return None
                return ExplicitModelSelection(
                    route="user_configured", selection_id=row["connection_id"], revision=row["revision"],
                    model_id=row["model_id"], recipient_id=row["recipient_id"], selected_by_user=True,
                    protocol=row["protocol"],
                )
        except IntelligenceError:
            raise
        except Exception as exc:
            raise ModelSettingsError("MODEL_SELECTION_UNAVAILABLE", 503) from exc

    def resolve_connection(self, selection, identity: dict | None = None):
        from .intelligence import ExplicitModelSelection
        identity = identity or (getattr(g, "identity", None) if has_request_context() else None)
        if type(selection) is not ExplicitModelSelection or not isinstance(identity, dict):
            raise ModelSettingsError("MODEL_CONNECTION_CONTEXT_REQUIRED", 403)
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            row = self.store.row(
                "SELECT * FROM intelligence_model_connections WHERE connection_id = ? AND workspace_id = ? "
                "AND owner_user_id = ? AND status = 'configured' AND is_selected = 1",
                (selection.selection_id, workspace_id, user_id),
            )
            if (row is None or row["revision"] != selection.revision or row["model_id"] != selection.model_id
                    or row["recipient_id"] != selection.recipient_id or row["protocol"] != selection.protocol
                    or row["credential_ciphertext"] is None):
                raise ModelSettingsError("USER_MODEL_SETTINGS_CHANGED", 409)
            key = self._unprotect(row["credential_ciphertext"], identity, row["connection_id"])
            return UserConfiguredModelConnection(
                selection_id=row["connection_id"], revision=row["revision"], model_id=row["model_id"],
                recipient_id=row["recipient_id"], base_url=row["base_url"], api_key=key, protocol=row["protocol"],
                token_field=None if row["token_field"] == "not_applicable" else row["token_field"],
            )
        except ModelSettingsError:
            raise
        except Exception as exc:
            raise ModelSettingsError("MODEL_CONNECTION_UNAVAILABLE", 503) from exc

    def authorize_model_input(self, identity, selection, source_refs, *, purpose):
        from .intelligence import ExplicitModelSelection, ModelInputAuthorization
        if (purpose != "inference" or not isinstance(source_refs, (list, tuple))
                or type(selection) is not ExplicitModelSelection):
            raise ModelSettingsError("MODEL_INPUT_PURPOSE_NOT_ALLOWED", 403)
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            with self.store.transaction() as connection:
                self._identity(connection, identity)
                row = self._selected_row(connection, identity, selection)
                if row["allow_scoped_data"] != 1:
                    raise ModelSettingsError("MODEL_DATA_USE_NOT_AUTHORIZED", 403)
                if len(source_refs) > 20_000 or any(not isinstance(item, dict) for item in source_refs):
                    raise ModelSettingsError("MODEL_INPUT_SCOPE_INVALID", 409)
                allowed_kinds = {
                    "user_input", "conversation_message", "work_version", "material", "material_segment",
                    "artifact_version", "artifact_citation", "user_memory", "skill_package",
                }
                if any(item.get("kind") not in allowed_kinds for item in source_refs):
                    raise ModelSettingsError("MODEL_INPUT_SCOPE_INVALID", 409)
                source_hash = _sha256(_canonical(list(source_refs)))
                authorization_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc)
                expires = (now + timedelta(minutes=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
                connection.execute(
                    "INSERT INTO intelligence_model_admissions(admission_id, workspace_id, owner_user_id, connection_id, "
                    "selection_revision, purpose, recipient_id, source_ref_sha256, request_sha256, state, created_at, expires_at) "
                    "VALUES(?, ?, ?, ?, ?, 'inference', ?, ?, NULL, 'source_authorized', ?, ?)",
                    (authorization_id, workspace_id, user_id, row["connection_id"], row["revision"], row["recipient_id"],
                     source_hash, utc_now(), expires),
                )
                return ModelInputAuthorization(
                    allowed=True, workspace_id=workspace_id, user_id=user_id,
                    selection_id=row["connection_id"], selection_revision=row["revision"],
                    route="user_configured", model_id=row["model_id"], source_ref_sha256=source_hash,
                    purpose="inference", recipient=row["recipient_id"], authorization_id=authorization_id,
                    expires_at=expires,
                )
        except ModelSettingsError:
            raise
        except IntelligenceError:
            raise
        except Exception as exc:
            raise ModelSettingsError("MODEL_INPUT_PERMISSION_UNAVAILABLE", 503) from exc

    def authorize_provider_request(self, identity, selection, authorization, payload, request_digest):
        from .intelligence import ExternalModelApproval, ModelInputAuthorization
        if (type(authorization) is not ModelInputAuthorization or authorization.allowed is not True
                or authorization.purpose != "inference" or not isinstance(payload, dict)
                or not isinstance(request_digest, str) or _sha256(_canonical(payload)) != request_digest):
            raise ModelSettingsError("MODEL_REQUEST_APPROVAL_INVALID", 403)
        workspace_id, user_id, _ = identity_scope(identity)
        try:
            with self.store.transaction() as connection:
                self._identity(connection, identity)
                row = self._selected_row(connection, identity, selection)
                auth_row = connection.execute(
                    "SELECT * FROM intelligence_model_admissions WHERE admission_id = ? AND workspace_id = ? "
                    "AND owner_user_id = ? AND connection_id = ? AND selection_revision = ? "
                    "AND purpose = 'inference' AND source_ref_sha256 = ? AND expires_at > ?",
                    (authorization.authorization_id, workspace_id, user_id, row["connection_id"], row["revision"],
                     authorization.source_ref_sha256, utc_now()),
                ).fetchone()
                if auth_row is None or row["allow_scoped_data"] != 1:
                    raise ModelSettingsError("MODEL_DATA_USE_NOT_AUTHORIZED", 403)
                approval_id = str(uuid.uuid4())
                expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
                connection.execute(
                    "INSERT INTO intelligence_model_admissions(admission_id, workspace_id, owner_user_id, connection_id, "
                    "selection_revision, purpose, recipient_id, source_ref_sha256, request_sha256, state, created_at, expires_at) "
                    "VALUES(?, ?, ?, ?, ?, 'provider_request', ?, ?, ?, 'request_approved', ?, ?)",
                    (approval_id, workspace_id, user_id, row["connection_id"], row["revision"], row["recipient_id"],
                     authorization.source_ref_sha256, request_digest, utc_now(), expires),
                )
                return ExternalModelApproval(
                    allowed=True, workspace_id=workspace_id, user_id=user_id,
                    selection_id=row["connection_id"], selection_revision=row["revision"],
                    recipient=row["recipient_id"], request_sha256=request_digest,
                    approval_id=approval_id, expires_at=expires,
                )
        except ModelSettingsError:
            raise
        except IntelligenceError:
            raise
        except Exception as exc:
            raise ModelSettingsError("MODEL_REQUEST_APPROVAL_UNAVAILABLE", 503) from exc

    @staticmethod
    def _test_responses_endpoint(connection_value: UserConfiguredModelConnection) -> dict:
        try:
            destination, hostname, _port, _path = _connection_destination(
                connection_value.base_url, endpoint="responses",
            )
            body = _canonical({
                "model": connection_value.model_id,
                "input": "Return exactly the word READY.",
                "stream": False,
            })
            with _open_pinned_https(
                destination, hostname, body, connection_value.api_key, timeout=60,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ModelSettingsError("USER_MODEL_RESPONSE_TOO_LARGE", 502)
        except ModelSettingsError:
            raise
        except IntelligenceError as exc:
            raise ModelSettingsError(exc.code, exc.status, exc.details) from exc
        except Exception as exc:
            raise ModelSettingsError("USER_MODEL_TRANSPORT_UNAVAILABLE", 503) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ModelSettingsError("USER_MODEL_RESPONSE_INVALID", 502) from exc
        if not isinstance(data, dict) or data.get("error") or data.get("status") not in {None, "completed"}:
            raise ModelSettingsError("USER_MODEL_RESPONSE_INVALID", 502)
        text = data.get("output_text")
        if not isinstance(text, str):
            parts = []
            outputs = data.get("output")
            for item in outputs if isinstance(outputs, list) else []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                contents = item.get("content")
                for content in contents if isinstance(contents, list) else []:
                    if (isinstance(content, dict) and content.get("type") == "output_text"
                            and isinstance(content.get("text"), str)):
                        parts.append(content["text"])
            text = "".join(parts)
        if not text.strip():
            raise ModelSettingsError("USER_MODEL_RESPONSE_INVALID", 502)
        return {"status": "completed", "text": "READY"}

    def _selected_row(self, connection, identity, selection):
        workspace_id, user_id, _ = identity_scope(identity)
        row = connection.execute(
            "SELECT * FROM intelligence_model_connections WHERE connection_id = ? AND workspace_id = ? "
            "AND owner_user_id = ? AND status = 'configured' AND is_selected = 1",
            (selection.selection_id, workspace_id, user_id),
        ).fetchone()
        if (row is None or row["revision"] != selection.revision or row["model_id"] != selection.model_id
                or row["recipient_id"] != selection.recipient_id or row["protocol"] != selection.protocol):
            raise ModelSettingsError("MODEL_SELECTION_CHANGED", 409)
        return row

    def test_connection(self, identity: dict, connection_id: str, *, operation_key: str, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ModelSettingsError("MODEL_CONNECTION_TEST_CONFIRMATION_REQUIRED", 400)
        self._require_write(identity)
        data = {"connection_id": connection_id, "confirmed": True}
        request_sha = _sha256(_canonical({"action": "test", "request": data}))
        workspace_id, user_id, _ = identity_scope(identity)
        with self.store.transaction() as connection:
            self._identity(connection, identity)
            replay = self._event_replay(connection, identity, operation_key, request_sha)
            if replay is not None:
                return replay
            row = self._owned_row(connection, identity, connection_id)
            if row["status"] != "configured" or row["credential_ciphertext"] is None:
                raise ModelSettingsError("MODEL_CONNECTION_UNAVAILABLE", 409)
            self._record_event(
                connection, identity, connection_id, "test", operation_key, request_sha,
                {"state": "pending", "connection_id": connection_id, "model_id": row["model_id"]}, state="pending",
            )
        started = time.monotonic()
        try:
            secret = self._unprotect(row["credential_ciphertext"], identity, connection_id)
            connection_value = UserConfiguredModelConnection(
                selection_id=connection_id, revision=row["revision"], model_id=row["model_id"],
                recipient_id=row["recipient_id"], base_url=row["base_url"], api_key=secret, protocol=row["protocol"],
                token_field=None if row["token_field"] == "not_applicable" else row["token_field"],
            )
            from .intelligence import ExplicitModelSelection
            selection = ExplicitModelSelection(
                route="user_configured", selection_id=connection_id, revision=row["revision"],
                model_id=row["model_id"], recipient_id=row["recipient_id"], selected_by_user=True,
                protocol=row["protocol"],
            )
            payload = {
                "goal": "explicit_connection_test", "query": "Return the exact text READY.",
                "work": None, "artifact": None, "evidence": {"sources": [], "evidence": [], "citations": []},
                "memory": {"items": []}, "requested_output_formats": [], "tool_results": [],
                "conversation_history": [], "skills": [], "tools": [],
            }
            runner = self.chat_test_runner
            if runner is not None:
                result = runner(selection, connection_value, payload)
            elif row["protocol"] == CHAT_COMPLETIONS_PROTOCOL:
                result = UserConfiguredChatGateway(lambda _selection: connection_value)(selection, payload)
            else:
                result = self._test_responses_endpoint(connection_value)
            if not isinstance(result, dict) or result.get("status") != "completed" or not isinstance(result.get("text"), str):
                raise ModelSettingsError("MODEL_CONNECTION_TEST_FAILED", 502)
            response_state = "CONNECTED"
            reason = None
        except (ModelSettingsError, IntelligenceError) as exc:
            response_state = "UNKNOWN" if exc.code in {
                "USER_MODEL_TRANSPORT_UNAVAILABLE", "USER_MODEL_RESPONSE_INVALID", "USER_MODEL_RESPONSE_TOO_LARGE",
            } else "FAILED"
            reason = exc.code
        except Exception:
            response_state = "UNKNOWN"
            reason = "MODEL_CONNECTION_TEST_OUTCOME_UNKNOWN"
        duration_ms = int((time.monotonic() - started) * 1000)
        result_data = {
            "state": response_state, "connection_id": connection_id, "model_id": row["model_id"],
            "recipient_id": row["recipient_id"], "duration_ms": duration_ms,
            "reason": reason, "customer_data_sent": False,
            "adapter_state": "provider_endpoint_only" if row["protocol"] == RESPONSES_PROTOCOL else "chat_completions_transport",
        }
        final_state = "completed" if response_state == "CONNECTED" else "unknown" if response_state == "UNKNOWN" else "failed"
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE intelligence_model_events SET state = ?, result_json = ?, updated_at = ? "
                "WHERE workspace_id = ? AND owner_user_id = ? AND operation_key = ? AND action = 'test' AND state = 'pending'",
                (final_state, json.dumps(result_data, ensure_ascii=False, sort_keys=True), utc_now(),
                 workspace_id, user_id, operation_key),
            )
        return result_data

    def _owned_row(self, connection, identity: dict, connection_id: str):
        workspace_id, user_id, _ = identity_scope(identity)
        row = connection.execute(
            "SELECT * FROM intelligence_model_connections WHERE connection_id = ? AND workspace_id = ? AND owner_user_id = ?",
            (connection_id, workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise ModelSettingsError("MODEL_CONNECTION_NOT_FOUND", 404)
        return row


def _api_error(exc: ModelSettingsError):
    raise _api.ApiError(exc.code, "The model settings operation could not be completed", exc.status, exc.details) from exc


def _operation_key() -> str:
    key = _api._idempotency_key()
    if not key:
        raise _api.ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for this model settings action", 400)
    return key


def register_routes(app: Flask, store, service_provider) -> None:
    if app.extensions.get("oevira.intelligence_models_api.registered"):
        return
    def service():
        current = service_provider() if callable(service_provider) else service_provider
        if current is None:
            raise _api.ApiError("INTELLIGENCE_SERVICE_NOT_BOUND", "Intelligence is not available", 503)
        return current

    @app.get("/api/intelligence/models")
    def list_model_connections():
        try:
            return _api._success(service().model_settings.list_connections(g.identity))
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.post("/api/intelligence/models")
    def create_model_connection():
        data = _api._body()
        try:
            result = service().model_settings.create(g.identity, data, operation_key=_operation_key())
            return _api._success(result, 201)
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.patch("/api/intelligence/models/<connection_id>")
    def update_model_connection(connection_id: str):
        data = _api._body()
        try:
            return _api._success(service().model_settings.update(
                g.identity, connection_id, data, operation_key=_operation_key(),
            ))
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.post("/api/intelligence/models/<connection_id>/select")
    def select_model_connection(connection_id: str):
        data = _api._body()
        if data:
            raise _api.ApiError("INVALID_INPUT", "Model selection takes no additional fields", 400)
        try:
            return _api._success(service().model_settings.select(
                g.identity, connection_id, operation_key=_operation_key(),
            ))
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.post("/api/intelligence/models/<connection_id>/data-use")
    def set_model_data_use(connection_id: str):
        data = _api._body()
        if set(data) != {"allow_scoped_data"} or type(data.get("allow_scoped_data")) is not bool:
            raise _api.ApiError("INVALID_INPUT", "allow_scoped_data must be a boolean", 400)
        try:
            return _api._success(service().model_settings.set_data_use(
                g.identity, connection_id, data["allow_scoped_data"], operation_key=_operation_key(),
            ))
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.post("/api/intelligence/models/<connection_id>/test")
    def test_model_connection(connection_id: str):
        data = _api._body()
        if data != {"confirm_no_customer_data": True}:
            raise _api.ApiError("MODEL_CONNECTION_TEST_CONFIRMATION_REQUIRED", "Confirm that a minimal test request contains no customer materials", 400)
        try:
            return _api._success(service().model_settings.test_connection(
                g.identity, connection_id, operation_key=_operation_key(), confirmed=True,
            ))
        except ModelSettingsError as exc:
            _api_error(exc)

    @app.delete("/api/intelligence/models/<connection_id>")
    def revoke_model_connection(connection_id: str):
        try:
            return _api._success(service().model_settings.revoke(
                g.identity, connection_id, operation_key=_operation_key(),
            ))
        except ModelSettingsError as exc:
            _api_error(exc)

    app.extensions["oevira.intelligence_models_api.registered"] = True
