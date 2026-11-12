"""Authenticated HTTP operations for isolated, user-controlled browser sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from flask import Response, g, jsonify, request

from .api import (
    ApiError,
    _body,
    _idempotency_key,
    _material_data,
    _not_found,
    _parse_material,
    _request_fingerprint,
    _require_write,
    _resource_path,
    _success,
)
from .auth import utc_now
from .collaboration import require_material_access, require_work_access
from .browser import (
    BrowserError,
    BrowserRuntimeManager,
    ENGINE_CHANNELS,
    MAX_ACTION_TEXT_CHARACTERS,
    MAX_CAPTURE_CHARACTERS,
    SESSION_LEASE_SECONDS,
    WindowsUserStateProtector,
    _serialize_json,
    _state_entropy,
    browser_safe_url,
    validate_browser_url,
)


_ERROR_MESSAGES = {
    "ACCESS_REVOKED": "Workspace access is unavailable.",
    "BROWSER_ACTION_INVALID": "The browser action is invalid.",
    "BROWSER_ACTION_UNSUPPORTED": "The browser action is not supported.",
    "BROWSER_CONTROL_GENERATION_CONFLICT": "Browser control changed. Refresh its current owner before continuing.",
    "BROWSER_CONTROL_MODE_CONFLICT": "Browser control belongs to the other participant. Change control before continuing.",
    "BROWSER_CAPTURE_TOO_LARGE": "The selected page text exceeds the current material limit.",
    "BROWSER_ENGINE_INVALID": "Choose Chrome or Edge.",
    "BROWSER_ENGINE_START_TIMEOUT": "The browser engine did not start in time.",
    "BROWSER_ENGINE_UNAVAILABLE": "The selected installed browser could not be started.",
    "BROWSER_OPERATION_FAILED": "The browser operation could not be confirmed.",
    "BROWSER_OPERATION_IN_PROGRESS": "The previous browser operation is still running.",
    "BROWSER_OPERATION_NOT_STARTED": "The browser request expired in the queue without starting. Read the current page before trying again.",
    "BROWSER_OPERATION_STATE_UNKNOWN": "The browser operation result is unknown; inspect the current page before continuing.",
    "BROWSER_PAGE_CHANGED": "The page changed after it was read. Read the current page before continuing.",
    "BROWSER_PAGE_READ_UNAVAILABLE": "The current page text could not be read.",
    "BROWSER_PAGE_TEXT_UNAVAILABLE": "The current page has no readable text in the selected scope.",
    "BROWSER_PROTECTED_STORAGE_FAILED": "Protected browser session storage failed.",
    "BROWSER_PROTECTED_STORAGE_UNAVAILABLE": "No approved protected browser-session storage is configured for this runtime.",
    "BROWSER_REAUTH_REQUIRED": "Reconnect the product session before using this browser session.",
    "BROWSER_RESTORE_TARGET_UNAVAILABLE": "There is no saved page to restore.",
    "BROWSER_RUNTIME_UNAVAILABLE": "The browser runtime is unavailable.",
    "BROWSER_RUNTIME_STOPPING": "The browser is stopping. New operations have not started.",
    "BROWSER_SHUTDOWN_UNCONFIRMED": "The browser has not yet confirmed that it stopped. Its session is still tracked.",
    "BROWSER_RUNTIME_VERSION_MISMATCH": "The installed browser runtime does not match the locked version.",
    "BROWSER_SELECTION_EMPTY": "Select page text before saving a selection.",
    "BROWSER_SESSION_BUSY": "This browser session is handling another operation.",
    "BROWSER_SESSION_CLOSED": "This browser session was closed and cannot be reopened.",
    "BROWSER_SESSION_NOT_OPEN": "Reopen this browser session before continuing.",
    "BROWSER_SESSION_NOT_FOUND": "The browser session was not found.",
    "BROWSER_SESSION_REOPEN_REQUIRED": "Reopen this browser session before continuing.",
    "BROWSER_SESSION_STATE_UNAVAILABLE": "The saved browser session cannot be restored; sign in again in a new isolated session.",
    "BROWSER_SESSION_VERSION_CONFLICT": "The browser session changed. Read its current version before continuing.",
    "BROWSER_SOURCE_SAVE_FAILED": "The selected page content could not be saved as a material.",
    "BROWSER_TARGET_DENIED": "This browser request is outside the allowed public web target boundary.",
    "BROWSER_TARGET_INVALID": "Enter a valid HTTP(S) page address.",
    "BROWSER_WORK_ARCHIVED": "Reopen the selected work before saving a browser material.",
    "BROWSER_WORK_NOT_FOUND": "The selected work was not found.",
    "BROWSER_OPERATION_IDEMPOTENCY_CONFLICT": "This Idempotency-Key was already used for a different browser operation.",
    "BROWSER_OPERATION_UNKNOWN": "The previous browser operation may have completed; do not repeat it. Reconcile the session before trying again.",
    "BROWSER_OPERATION_PENDING": "The previous browser operation is still being reconciled.",
    "BROWSER_OPERATION_NOT_FOUND": "The browser operation was not found.",
    "BROWSER_PAGE_REVISION_INVALID": "Read the current page before sending a page revision.",
    "BROWSER_READ_INVALID": "The browser read request is invalid.",
    "BROWSER_PREVIEW_NOT_FOUND": "The browser preview was not found.",
    "BROWSER_PREVIEW_EXPIRED": "This preview expired. Read the page again to create a new preview.",
    "BROWSER_PREVIEW_DISCARDED": "This preview was discarded. Saved materials remain available under their own permissions.",
    "BROWSER_PREVIEW_UNAVAILABLE": "The preview body is no longer available. Its receipt does not restore the body.",
    "BROWSER_PREVIEW_INTEGRITY_ERROR": "The stored preview no longer matches its capture receipt.",
    "BROWSER_NAVIGATION_INVALID": "The browser navigation request is invalid.",
    "BROWSER_CAPTURE_INVALID": "The browser capture request is invalid.",
    "BROWSER_CAPTURE_SCOPE_INVALID": "Choose the current page or a selected text range.",
    "BROWSER_SESSION_INPUT_INVALID": "The browser session request is invalid.",
    "BROWSER_SESSION_VERSION_INVALID": "The browser session version is invalid.",
}


def _browser_error(exc: BrowserError) -> ApiError:
    details = dict(exc.details)
    details.pop("operation_id", None)
    if exc.code in {"BROWSER_OPERATION_UNKNOWN", "BROWSER_OPERATION_PENDING"}:
        details["reconcile_required"] = True
    return ApiError(exc.code, _ERROR_MESSAGES.get(exc.code, "The browser operation could not be completed."), exc.status, details)


def _base_version(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BrowserError("BROWSER_SESSION_VERSION_INVALID", 400)
    return value


def _page_revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BrowserError("BROWSER_PAGE_REVISION_INVALID", 400)
    return value


def _safe_action(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise BrowserError("BROWSER_ACTION_INVALID", 400)
    kind = value["type"]
    if kind == "click":
        if set(value) != {"type", "x", "y"}:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        x, y = value["x"], value["y"]
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (x, y)):
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        if not (0 <= x <= 10000 and 0 <= y <= 10000):
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        return {"type": kind, "x": float(x), "y": float(y)}
    if kind == "type":
        if set(value) != {"type", "text"} or not isinstance(value.get("text"), str):
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        if len(value["text"]) > MAX_ACTION_TEXT_CHARACTERS:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        return {"type": kind, "text": value["text"]}
    if kind == "drag_select":
        if set(value) != {"type", "x1", "y1", "x2", "y2"}:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        points = [value[key] for key in ("x1", "y1", "x2", "y2")]
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 10000 for item in points):
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        return {"type": kind, "x1": float(points[0]), "y1": float(points[1]), "x2": float(points[2]), "y2": float(points[3])}
    if kind == "press":
        if set(value) != {"type", "key"} or value.get("key") not in {
            "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowDown", "ArrowLeft",
            "ArrowRight", "ArrowUp", "Home", "End", "PageDown", "PageUp", "Control+A", "Control+C",
            "Control+V", "Control+X", "Shift+Tab", "Alt+ArrowLeft", "Alt+ArrowRight",
        }:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        return {"type": kind, "key": value["key"]}
    if kind == "scroll":
        if set(value) != {"type", "delta_x", "delta_y"}:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        deltas = (value["delta_x"], value["delta_y"])
        if any(isinstance(item, bool) or not isinstance(item, int) or abs(item) > 2000 for item in deltas):
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        return {"type": kind, "delta_x": deltas[0], "delta_y": deltas[1]}
    if kind in {"back", "forward", "reload"} and set(value) == {"type"}:
        return {"type": kind}
    raise BrowserError("BROWSER_ACTION_UNSUPPORTED", 400)


def _plain_snapshot(snapshot: dict) -> dict:
    """Keep operation receipts free of page text, tokens, and cookie material."""
    return {
        key: snapshot[key]
        for key in (
            "page_id", "url", "title", "page_revision", "text_sha256", "text_characters",
            "text_truncated", "blocked_request_count", "scope", "limitations", "redacted_query_keys",
            "login_state", "operation_state", "action_type", "browser_version", "restore_url",
            "runtime_url", "restore_required", "restore_title",
        )
        if key in snapshot
    }


class BrowserService:
    def __init__(self, app, store):
        self.app = app
        self.store = store
        self.protector = app.config.get("BROWSER_STATE_PROTECTOR") or WindowsUserStateProtector()
        self.url_validator = app.config.get("BROWSER_URL_AUTHORIZER") or validate_browser_url
        self.runtime = BrowserRuntimeManager(
            headless_resolver=lambda: bool(app.config.get("BROWSER_HEADLESS", app.config.get("TESTING", os.name != "nt"))),
            launch_args_resolver=lambda: list(app.config.get("BROWSER_LAUNCH_ARGS", [])),
            authorize_request=self._authorize_page_request,
            runtime_is_live=self._runtime_is_live,
            on_runtime_expired=self._runtime_expired,
            on_runtime_checkpoint=self._runtime_checkpoint,
        )

    def _membership(self, connection, identity: dict):
        member = connection.execute(
            "SELECT active, created_at, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        if member is None or not member["active"]:
            raise BrowserError("ACCESS_REVOKED", 403)
        return member

    def _owned_session(self, connection, identity: dict, session_id: str, *, allow_reauthorize: bool = False,
                       check_work_access: bool = True):
        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        row = connection.execute(
            "SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ? AND owner_user_id = ?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise BrowserError("BROWSER_SESSION_NOT_FOUND", 404)
        auth_session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (identity["session_id"], workspace_id, user_id),
        ).fetchone()
        if auth_session is None or auth_session["revoked_at"] is not None or auth_session["expires_at"] <= utc_now():
            raise BrowserError("UNAUTHENTICATED", 401)
        member = self._membership(connection, identity)
        if member["created_at"] != row["membership_created_at"]:
            raise BrowserError("BROWSER_SESSION_NOT_FOUND", 404)
        if not allow_reauthorize and row["authorized_by_session_id"] != identity["session_id"]:
            raise BrowserError("BROWSER_REAUTH_REQUIRED", 409)
        if check_work_access and row["work_id"]:
            try:
                require_work_access(connection, identity, row["work_id"])
            except ApiError as exc:
                raise BrowserError(exc.code, exc.status, exc.details) from exc
        return row

    @staticmethod
    def _require_control_actor(row, actor: str | None, expected_generation: int | None = None) -> None:
        if actor not in {None, "user", "assistant"}:
            raise BrowserError("BROWSER_CONTROL_MODE_CONFLICT", 400)
        if (expected_generation is not None
                and (isinstance(expected_generation, bool) or not isinstance(expected_generation, int)
                     or expected_generation < 1)):
            raise BrowserError("BROWSER_CONTROL_GENERATION_CONFLICT", 400)
        if expected_generation is not None and row["control_generation"] != expected_generation:
            raise BrowserError("BROWSER_CONTROL_GENERATION_CONFLICT", 409, {
                "control_mode": row["control_mode"],
                "control_generation": row["control_generation"],
            })
        if actor is not None and row["control_mode"] != actor:
            raise BrowserError("BROWSER_CONTROL_MODE_CONFLICT", 409, {
                "control_mode": row["control_mode"],
                "control_generation": row["control_generation"],
            })

    def assert_control_state(self, identity: dict, session_id: str, actor: str | None,
                             expected_generation: int | None = None, *, allow_pending_read: bool = False) -> dict:
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id)
            self._require_control_actor(row, actor, expected_generation)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409, {"status": row["status"]})
            if row["operation_id"] and not (allow_pending_read and actor is None):
                raise BrowserError("BROWSER_SESSION_BUSY", 409)
            if actor == "assistant":
                unknown = self._unreconciled_operation(connection, session_id, identity)
                if unknown is not None:
                    raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"reconcile_required": True})
        return row

    @staticmethod
    def _unreconciled_operation(connection, session_id: str, identity: dict):
        unknown = connection.execute(
            "SELECT rowid, id FROM browser_session_operations WHERE session_id = ? AND workspace_id = ? "
            "AND owner_user_id = ? AND status = 'unknown' ORDER BY rowid DESC LIMIT 1",
            (session_id, identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        if unknown is None:
            return None
        reconciled = connection.execute(
            "SELECT rowid FROM browser_session_operations WHERE session_id = ? AND workspace_id = ? "
            "AND owner_user_id = ? AND operation IN ('reconcile', 'assistant:reconcile') "
            "AND status = 'completed' ORDER BY rowid DESC LIMIT 1",
            (session_id, identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        return unknown if reconciled is None or reconciled["rowid"] < unknown["rowid"] else None

    def assert_control_operation(self, identity: dict, session_id: str, row, operation_id: str,
                                  actor: str | None) -> None:
        """Fence a reserved browser call immediately before its Playwright effect."""
        with self.store.transaction(immediate=False) as connection:
            current = self._owned_session(connection, identity, session_id)
            self._require_control_actor(current, actor, row["control_generation"])
            if self._membership(connection, identity)["role"] not in {"owner", "editor"}:
                raise BrowserError("ACCESS_REVOKED", 403)
            if current["work_id"]:
                try:
                    require_work_access(connection, identity, current["work_id"], write=True)
                except ApiError as exc:
                    raise BrowserError(exc.code, exc.status, exc.details) from exc
            if current["operation_id"] != operation_id:
                raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"reconcile_required": True})

    def _authorize_page_request(self, session_id: str, url: str) -> bool:
        if self.runtime.shutdown_requested or self.runtime.is_stopping(session_id):
            return False
        if url.startswith(("about:", "data:", "blob:")):
            return True
        try:
            self.url_validator(url)
            return self._runtime_is_live(session_id)
        except Exception:
            return False

    def _runtime_is_live(self, session_id: str) -> bool:
        now = utc_now()
        row = self.store.row(
            """SELECT bs.status, bs.workspace_id, bs.owner_user_id, bs.membership_created_at, bs.work_id,
                      m.active, m.created_at AS current_membership_created_at,
                      s.revoked_at, s.expires_at
               FROM browser_sessions bs
               JOIN memberships m ON m.workspace_id = bs.workspace_id AND m.user_id = bs.owner_user_id
               JOIN sessions s ON s.id = bs.authorized_by_session_id AND s.workspace_id = bs.workspace_id AND s.user_id = bs.owner_user_id
               WHERE bs.id = ?""",
            (session_id,),
        )
        authorized = bool(
            row
            and row["status"] in {"starting", "open", "reopening"}
            and row["active"]
            and row["membership_created_at"] == row["current_membership_created_at"]
            and row["revoked_at"] is None
            and row["expires_at"] > now
        )
        if authorized and row["work_id"]:
            try:
                with self.store.transaction(immediate=False) as connection:
                    require_work_access(connection, {"workspace": {"id": row["workspace_id"]}, "user": {"id": row["owner_user_id"]}}, row["work_id"], write=True)
            except ApiError:
                return False
        return authorized

    def _runtime_expired(self, session_id: str, payload: dict | None) -> None:
        now = utc_now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None or row["status"] not in {"starting", "open", "reopening"}:
                return
            member = connection.execute(
                "SELECT active, created_at FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (row["workspace_id"], row["owner_user_id"]),
            ).fetchone()
            membership_current = bool(member and member["active"] and member["created_at"] == row["membership_created_at"])
            if membership_current and payload is not None:
                try:
                    ciphertext = self._protect_payload(row, payload)
                    status, code = "suspended", "BROWSER_PRODUCT_SESSION_EXPIRED"
                except BrowserError:
                    ciphertext, status, code = None, "closed", "BROWSER_PROTECTED_STORAGE_FAILED"
            else:
                ciphertext, status, code = None, "closed", "BROWSER_PERMISSION_REVOKED"
            if row["operation_id"]:
                connection.execute(
                    "UPDATE browser_session_operations SET status = 'unknown', error_code = 'BROWSER_OPERATION_STATE_UNKNOWN', updated_at = ? WHERE id = ? AND session_id = ? AND status = 'pending'",
                    (now, row["operation_id"], session_id),
                )
            version = row["version"] + 1
            connection.execute(
                "UPDATE browser_sessions SET status = ?, version = ?, state_ciphertext = ?, state_protection = ?, operation_id = NULL, operation_expires_at = NULL, last_error_code = ?, updated_at = ?, closed_at = CASE WHEN ? = 'closed' THEN ? ELSE closed_at END WHERE id = ? AND version = ?",
                (status, version, ciphertext, self.protector.protection_name if ciphertext else None, code, now, status, now, session_id, row["version"]),
            )
            updated = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            self._write_version(connection, updated, "authorization_expired", row["owner_user_id"], now)

    def _runtime_checkpoint(self, session_id: str, payload: dict, snapshot: dict, *, stopped: bool = False) -> None:
        """Preserve manual browsing without exporting its protected session state."""
        now = utc_now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None or row["status"] != "open" or row["operation_id"]:
                return
            if not self._runtime_is_live(session_id):
                return
            try:
                ciphertext = self._protect_payload(row, payload)
            except BrowserError:
                connection.execute("UPDATE browser_sessions SET last_error_code = 'BROWSER_PROTECTED_STORAGE_FAILED' WHERE id = ?", (session_id,))
                return
            connection.execute(
                "UPDATE browser_sessions SET state_ciphertext=?, state_protection=?, current_url=?, current_title=?, "
                "page_revision=?, updated_at=?, status=?, version=version+?, control_generation=control_generation+?, "
                "control_mode=CASE WHEN ? THEN 'user' ELSE control_mode END WHERE id=? AND version=?",
                (ciphertext, self.protector.protection_name, snapshot["url"], snapshot["title"], snapshot["page_revision"], now,
                 "suspended" if stopped else "open", int(stopped), int(stopped), stopped, session_id, row["version"]),
            )
            if stopped:
                updated = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
                self._write_version(connection, updated, "runtime_stopped", row["owner_user_id"], now)

    def _protect_payload(self, row, payload: dict) -> bytes:
        return self.protector.protect(
            _serialize_json(payload),
            _state_entropy(row["workspace_id"], row["owner_user_id"], row["id"]),
        )

    def _unprotect_payload(self, row) -> dict | None:
        if not row["state_ciphertext"]:
            return None
        try:
            raw = self.protector.unprotect(
                bytes(row["state_ciphertext"]),
                _state_entropy(row["workspace_id"], row["owner_user_id"], row["id"]),
            )
            value = json.loads(raw.decode("utf-8"))
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("BROWSER_SESSION_STATE_UNAVAILABLE", 409) from exc
        if not isinstance(value, dict):
            raise BrowserError("BROWSER_SESSION_STATE_UNAVAILABLE", 409)
        return value

    def _write_version(self, connection, row, operation: str, user_id: str, now: str) -> None:
        snapshot = {
            "id": row["id"], "engine": row["engine"], "status": row["status"],
            "version": row["version"], "page_revision": row["page_revision"],
            "control_mode": row["control_mode"], "control_generation": row["control_generation"],
            "current_url": row["current_url"], "current_title": row["current_title"],
            "browser_version": row["browser_version"], "work_id": row["work_id"],
            "login_state": "NOT_VERIFIED", "state_protection": row["state_protection"],
            "last_error_code": row["last_error_code"],
        }
        connection.execute(
            "INSERT INTO browser_session_versions(session_id, workspace_id, version, operation, metadata_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row["workspace_id"], row["version"], operation, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now, user_id),
        )

    def _session_data(self, row, *, runtime_state: str | None = None,
                      inspect_runtime: bool = True) -> dict:
        live = (
            self.runtime.is_open(row["engine"], row["id"])
            if inspect_runtime else
            True if runtime_state == "open" else False if runtime_state == "not_loaded" else None
        )
        resolved_runtime_state = runtime_state or (
            ("stopping" if self.runtime.is_stopping(row["id"]) else "open" if live else "not_loaded")
            if inspect_runtime else "not_checked"
        )
        return {
            "id": row["id"], "engine": row["engine"], "engine_version": row["browser_version"],
            "status": row["status"], "runtime_state": resolved_runtime_state,
            "version": row["version"], "page_revision": row["page_revision"],
            "control_mode": row["control_mode"], "control_generation": row["control_generation"],
            "url": row["current_url"], "checkpoint_url": row["current_url"],
            "title": row["current_title"], "work_id": row["work_id"],
            "restore_required": (
                bool(not live and row["status"] not in {"closed", "error"})
                if live is not None else None
            ),
            "login_state": "NOT_VERIFIED", "state_protection": row["state_protection"],
            "last_error_code": row["last_error_code"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "closed_at": row["closed_at"],
        }

    def list_sessions(self, identity: dict) -> list[dict]:
        with self.store.transaction(immediate=False) as connection:
            self._membership(connection, identity)
            rows = connection.execute(
                "SELECT * FROM browser_sessions WHERE workspace_id = ? AND owner_user_id = ? ORDER BY updated_at DESC, id",
                (identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchall()
        return [self._session_data(row) for row in rows]

    def get_session(self, identity: dict, session_id: str) -> dict:
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id, allow_reauthorize=True)
        return self._session_data(row)

    def list_work_sessions(self, identity: dict, work_id: str, *, limit: int = 50) -> list[dict]:
        if not isinstance(work_id, str) or not work_id or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        with self.store.transaction(immediate=False) as connection:
            self._membership(connection, identity)
            require_work_access(connection, identity, work_id)
            rows = connection.execute(
                "SELECT * FROM browser_sessions WHERE workspace_id = ? AND owner_user_id = ? AND work_id = ? "
                "ORDER BY updated_at DESC, id LIMIT ?",
                (identity["workspace"]["id"], identity["user"]["id"], work_id, limit),
            ).fetchall()
            for row in rows:
                self._owned_session(
                    connection, identity, row["id"], allow_reauthorize=True,
                    check_work_access=False,
                )
        return [self._session_data(row, inspect_runtime=False) for row in rows]

    def get_control_state(self, identity: dict, session_id: str) -> dict:
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(
                connection, identity, session_id, allow_reauthorize=True, check_work_access=False,
            )
        return {
            "session_id": session_id,
            "control_mode": row["control_mode"],
            "control_generation": row["control_generation"],
            "version": row["version"],
            "status": row["status"],
            "work_id": row["work_id"],
        }

    def stop_loading(self, identity: dict, session_id: str, data: dict) -> dict:
        if set(data) != {"page_id", "page_revision", "control_generation"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        page_id = data["page_id"]
        if not isinstance(page_id, str) or not page_id or len(page_id) > 80:
            raise BrowserError("BROWSER_READ_INVALID", 400)
        revision = _page_revision(data["page_revision"])
        generation = data["control_generation"]
        self.authorize_host_input(identity, session_id, generation)
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id)
        result = self.runtime.stop_loading(
            row["engine"], session_id, page_id, revision,
            lambda: self.authorize_host_input(identity, session_id, generation),
        )
        self.authorize_host_input(identity, session_id, generation)
        return {"session_id": session_id, "control_generation": generation, **result}

    def bind_work(self, identity: dict, session_id: str, data: dict) -> dict:
        if set(data) != {"work_id", "base_version"} or not isinstance(data.get("work_id"), str):
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        version = _base_version(data["base_version"])
        with self.store.transaction() as connection:
            row = self._owned_session(connection, identity, session_id)
            require_work_access(connection, identity, data["work_id"], write=True)
            if row["work_id"] == data["work_id"]:
                return self._session_data(row, inspect_runtime=False)
            if row["work_id"] is not None:
                raise BrowserError("BROWSER_WORK_SCOPE_MISMATCH", 409)
            if row["version"] != version:
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409)
            self._require_control_actor(row, "user")
            if row["operation_id"] or row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_BUSY", 409)
            connection.execute(
                "UPDATE browser_sessions SET work_id=?,version=version+1,control_generation=control_generation+1,updated_at=? WHERE id=? AND version=?",
                (data["work_id"], utc_now(), session_id, version),
            )
            updated = connection.execute("SELECT * FROM browser_sessions WHERE id=?", (session_id,)).fetchone()
            self._write_version(connection, updated, "work_bound", identity["user"]["id"], utc_now())
        return self._session_data(updated, runtime_state="open", inspect_runtime=False)

    def authorize_host_input(self, identity: dict, session_id: str,
                             expected_control_generation: int) -> dict:
        """Authorize one human host-input dispatch against the current control fence."""
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(
                connection, identity, session_id, allow_reauthorize=True, check_work_access=False,
            )
            self._require_control_actor(row, "user", expected_control_generation)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409, {"status": row["status"]})
            if row["work_id"]:
                require_work_access(connection, identity, row["work_id"], write=True)
        return {
            "session_id": session_id,
            "authorized": True,
            "control_mode": row["control_mode"],
            "control_generation": row["control_generation"],
            "version": row["version"],
        }

    def set_control(self, identity: dict, session_id: str, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"mode", "base_version"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        mode = data.get("mode")
        if not isinstance(mode, str) or mode not in {"user", "assistant"}:
            raise BrowserError("BROWSER_CONTROL_MODE_CONFLICT", 400)
        base_version = _base_version(data.get("base_version"))
        unchanged = None
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(
                connection, identity, session_id, check_work_access=mode == "assistant",
            )
            member = self._membership(connection, identity)
            if row["version"] != base_version:
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": row["version"]})
            if row["control_mode"] == mode:
                unchanged = row
            if mode == "assistant" and unchanged is None:
                if member["role"] not in {"owner", "editor"}:
                    raise BrowserError("ACCESS_REVOKED", 403)
                if row["work_id"]:
                    require_work_access(connection, identity, row["work_id"], write=True)
                if row["status"] != "open" or row["operation_id"]:
                    raise BrowserError("BROWSER_SESSION_BUSY" if row["operation_id"] else "BROWSER_SESSION_NOT_OPEN", 409)
        if unchanged is not None:
            runtime_state = "open" if unchanged["status"] == "open" else "not_loaded"
            return self._session_data(unchanged, runtime_state=runtime_state, inspect_runtime=False)
        if mode == "assistant" and unchanged is None and not self.runtime.is_open(row["engine"], session_id):
            raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)

        now = utc_now()
        with self.store.transaction() as connection:
            current = self._owned_session(
                connection, identity, session_id, check_work_access=mode == "assistant",
            )
            member = self._membership(connection, identity)
            if current["version"] != base_version:
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            no_change = current["control_mode"] == mode
            if not no_change and mode == "assistant" and (current["status"] != "open" or current["operation_id"]):
                raise BrowserError("BROWSER_SESSION_BUSY" if current["operation_id"] else "BROWSER_SESSION_NOT_OPEN", 409)
            if not no_change and mode == "assistant":
                if member["role"] not in {"owner", "editor"}:
                    raise BrowserError("ACCESS_REVOKED", 403)
                if current["work_id"]:
                    require_work_access(connection, identity, current["work_id"], write=True)
            if not no_change and current["operation_id"]:
                connection.execute(
                    "UPDATE browser_session_operations SET status = 'unknown', error_code = 'BROWSER_OPERATION_STATE_UNKNOWN', updated_at = ? "
                    "WHERE id = ? AND session_id = ? AND status = 'pending'",
                    (now, current["operation_id"], session_id),
                )
            if no_change:
                updated = current
            else:
                connection.execute(
                    "UPDATE browser_sessions SET control_mode = ?, control_generation = control_generation + 1, "
                    "version = version + 1, operation_id = NULL, operation_expires_at = NULL, updated_at = ? "
                    "WHERE id = ? AND workspace_id = ? AND version = ?",
                    (mode, now, session_id, identity["workspace"]["id"], base_version),
                )
                updated = connection.execute(
                    "SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ?",
                    (session_id, identity["workspace"]["id"]),
                ).fetchone()
                self._write_version(connection, updated, f"control_{mode}", identity["user"]["id"], now)
        runtime_state = "open" if updated["status"] == "open" else "not_loaded"
        return self._session_data(updated, runtime_state=runtime_state, inspect_runtime=False)

    def get_versions(self, identity: dict, session_id: str) -> list[dict]:
        with self.store.transaction(immediate=False) as connection:
            self._owned_session(connection, identity, session_id, allow_reauthorize=True)
            rows = connection.execute(
                "SELECT version, operation, metadata_json, created_at, created_by FROM browser_session_versions WHERE session_id = ? AND workspace_id = ? ORDER BY version",
                (session_id, identity["workspace"]["id"]),
            ).fetchall()
        return [{
            "version": item["version"], "operation": item["operation"],
            "metadata": json.loads(item["metadata_json"]),
            "created_at": item["created_at"], "created_by": item["created_by"],
        } for item in rows]

    def create_session(self, identity: dict, data: dict, key: str) -> tuple[dict, int]:
        if set(data) - {"engine", "work_id"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        engine = data.get("engine", "chrome")
        if engine not in ENGINE_CHANNELS:
            raise BrowserError("BROWSER_ENGINE_INVALID", 400)
        work_id = data.get("work_id")
        if work_id is not None and (not isinstance(work_id, str) or not work_id.strip()):
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        work_id = work_id.strip() if isinstance(work_id, str) else None
        payload = {"engine": engine, "work_id": work_id}
        fingerprint = _request_fingerprint(payload)
        workspace_id, user_id = identity["workspace"]["id"], identity["user"]["id"]
        now = utc_now()
        session_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            member = self._membership(connection, identity)
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
                work = connection.execute("SELECT id, status FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id)).fetchone()
                if work is None:
                    raise BrowserError("BROWSER_WORK_NOT_FOUND", 404)
                if work["status"] != "active":
                    raise BrowserError("BROWSER_WORK_ARCHIVED", 409)
            previous = connection.execute(
                "SELECT request_hash, resource_type, resource_id FROM idempotency_records WHERE workspace_id = ? AND operation = 'browser.session.create' AND key = ?",
                (workspace_id, key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint or previous["resource_type"] != "browser_session":
                    raise BrowserError("BROWSER_OPERATION_IDEMPOTENCY_CONFLICT", 409)
                row = connection.execute("SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ? AND owner_user_id = ?", (previous["resource_id"], workspace_id, user_id)).fetchone()
                if row is None:
                    raise BrowserError("BROWSER_OPERATION_RESULT_MISSING", 409)
                return self._session_data(row), 200
            probe_entropy = _state_entropy(workspace_id, user_id, session_id)
            probe_ciphertext = self.protector.protect(b"browser-session-protection-check", probe_entropy)
            if self.protector.unprotect(probe_ciphertext, probe_entropy) != b"browser-session-protection-check":
                raise BrowserError("BROWSER_PROTECTED_STORAGE_FAILED", 503)
            connection.execute(
                "INSERT INTO browser_sessions(id, workspace_id, owner_user_id, membership_created_at, authorized_by_session_id, work_id, engine, status, version, page_revision, current_url, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'starting', 1, 0, 'about:blank', ?, ?)",
                (session_id, workspace_id, user_id, member["created_at"], identity["session_id"], work_id, engine, now, now),
            )
            row = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            self._write_version(connection, row, "created", user_id, now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'browser.session.create', ?, ?, 'browser_session', ?, ?)",
                (workspace_id, key, fingerprint, session_id, now),
            )
        try:
            opened = self.runtime.open(session_id, workspace_id, user_id, engine)
            snapshot = opened["snapshot"]
            ciphertext = self._protect_payload(row, opened["storage_payload"])
        except BrowserError as exc:
            self.runtime.close(engine, session_id)
            with self.store.transaction() as connection:
                failed = connection.execute("SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ?", (session_id, workspace_id)).fetchone()
                if failed:
                    connection.execute(
                        "UPDATE browser_sessions SET status = 'error', last_error_code = ?, version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                        (exc.code, utc_now(), session_id, workspace_id, failed["version"]),
                    )
                    updated = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
                    self._write_version(connection, updated, "start_failed", user_id, updated["updated_at"])
            raise
        now = utc_now()
        with self.store.transaction() as connection:
            current = self._owned_session(connection, identity, session_id)
            connection.execute(
                "UPDATE browser_sessions SET status = 'open', browser_version = ?, page_revision = ?, current_url = ?, current_title = ?, state_ciphertext = ?, state_protection = ?, version = version + 1, updated_at = ?, last_error_code = NULL WHERE id = ? AND version = ?",
                (snapshot.get("browser_version"), snapshot["page_revision"], snapshot["url"], snapshot["title"], ciphertext, self.protector.protection_name, now, session_id, current["version"]),
            )
            row = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            self._write_version(connection, row, "opened", user_id, now)
        return self._session_data(row), 201

    def _reserve(
        self, identity: dict, session_id: str, operation: str, key: str,
        request_payload: dict, base_version: int, *, allow_reauthorize: bool = False,
        allowed_statuses: set[str] | None = None, transition_status: str | None = None,
        actor: str | None = None, expected_control_generation: int | None = None,
        check_work_access: bool = True,
    ):
        workspace_id, user_id = identity["workspace"]["id"], identity["user"]["id"]
        request_hash = _request_fingerprint(request_payload)
        now = utc_now()
        operation_id = str(uuid.uuid4())
        stale_operation_id = None
        replay_result = None
        with self.store.transaction() as connection:
            row = self._owned_session(
                connection, identity, session_id, allow_reauthorize=allow_reauthorize,
                check_work_access=check_work_access,
            )
            self._require_control_actor(row, actor, expected_control_generation)
            if actor == "assistant" and operation != "reconcile":
                unknown = self._unreconciled_operation(connection, session_id, identity)
                if unknown is not None:
                    raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"reconcile_required": True})
            if check_work_access and row["work_id"]:
                require_work_access(connection, identity, row["work_id"], write=True)
            if row["operation_id"] and row["operation_expires_at"] and row["operation_expires_at"] > now:
                raise BrowserError("BROWSER_SESSION_BUSY", 409)
            if row["operation_id"]:
                stale_operation_id = row["operation_id"]
                connection.execute(
                    "UPDATE browser_session_operations SET status = 'unknown', error_code = 'BROWSER_OPERATION_STATE_UNKNOWN', updated_at = ? WHERE id = ? AND session_id = ? AND status = 'pending'",
                    (now, stale_operation_id, session_id),
                )
                connection.execute(
                    "UPDATE browser_sessions SET operation_id = NULL, operation_expires_at = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND operation_id = ?",
                    (now, session_id, workspace_id, stale_operation_id),
                )
            if allow_reauthorize and stale_operation_id is None and row["authorized_by_session_id"] != identity["session_id"]:
                connection.execute(
                    "UPDATE browser_sessions SET authorized_by_session_id = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                    (identity["session_id"], now, session_id, workspace_id),
                )
                row = connection.execute("SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ?", (session_id, workspace_id)).fetchone()
            if stale_operation_id is None:
                prior = connection.execute(
                    "SELECT * FROM browser_session_operations WHERE session_id = ? AND operation = ? AND idempotency_key = ?",
                    (session_id, f"assistant:{operation}" if actor == "assistant" else operation, key),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise BrowserError("BROWSER_OPERATION_IDEMPOTENCY_CONFLICT", 409)
                    if prior["status"] == "completed":
                        replay_result = json.loads(prior["result_json"] or "{}")
                        replay_result["replayed"] = True
                    elif prior["status"] == "unknown":
                        raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"operation_id": prior["id"]})
                    elif prior["status"] == "pending":
                        raise BrowserError("BROWSER_OPERATION_PENDING", 409, {"operation_id": prior["id"]})
                    else:
                        raise BrowserError(prior["error_code"] or "BROWSER_OPERATION_FAILED", 409, {"operation_id": prior["id"]})
            if stale_operation_id is None and replay_result is None:
                statuses = allowed_statuses or {"open"}
                if row["status"] not in statuses:
                    raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409, {"status": row["status"]})
                if row["version"] != base_version:
                    raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": row["version"]})
                connection.execute(
                    "INSERT INTO browser_session_operations(id, session_id, workspace_id, owner_user_id, operation, idempotency_key, request_hash, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (operation_id, session_id, workspace_id, user_id,
                     f"assistant:{operation}" if actor == "assistant" else operation,
                     key, request_hash, now, now),
                )
                connection.execute(
                    "UPDATE browser_sessions SET status = COALESCE(?, status), authorized_by_session_id = ?, operation_id = ?, operation_expires_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                    (transition_status, identity["session_id"], operation_id, _expiry_text(SESSION_LEASE_SECONDS), now, session_id, workspace_id, base_version),
                )
        if stale_operation_id is not None:
            raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"operation_id": stale_operation_id})
        if replay_result is not None:
            return None, row, replay_result
        return operation_id, row, None

    def _complete_operation(self, identity: dict, row, operation_id: str, operation: str, *, snapshot: dict,
                            storage_payload: dict | None, result: dict, material_id: str | None = None,
                            operation_status: str = "completed", session_status: str = "open",
                            clear_state: bool = False, allow_reauthorize: bool = False,
                            check_work_access: bool = True) -> dict:
        if clear_state:
            ciphertext, protection = None, None
        elif storage_payload is not None:
            ciphertext, protection = self._protect_payload(row, storage_payload), self.protector.protection_name
        else:
            ciphertext, protection = row["state_ciphertext"], row["state_protection"]
        now = utc_now()
        workspace_id = identity["workspace"]["id"]
        safe_url = snapshot.get("url", row["current_url"])
        title = snapshot.get("title", row["current_title"])
        page_revision = snapshot.get("page_revision", row["page_revision"])
        safe_result = dict(result)
        safe_result["id"] = row["id"]
        safe_result["session_id"] = row["id"]
        safe_result["version"] = row["version"] + 1
        safe_result["status"] = session_status
        safe_result["page_revision"] = page_revision
        safe_result["url"] = safe_url
        safe_result["title"] = title
        safe_result["control_mode"] = row["control_mode"]
        safe_result["control_generation"] = row["control_generation"]
        safe_result["operation_state"] = operation_status
        with self.store.transaction() as connection:
            current = self._owned_session(
                connection, identity, row["id"], allow_reauthorize=allow_reauthorize,
                check_work_access=check_work_access,
            )
            if (current["version"] != row["version"]
                    or current["operation_id"] != operation_id
                    or current["control_generation"] != row["control_generation"]
                    or current["control_mode"] != row["control_mode"]):
                raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"operation_id": operation_id})
            connection.execute(
                "UPDATE browser_sessions SET status = ?, version = version + 1, page_revision = ?, current_url = ?, current_title = ?, browser_version = COALESCE(?, browser_version), state_ciphertext = ?, state_protection = ?, authorized_by_session_id = ?, operation_id = NULL, operation_expires_at = NULL, last_error_code = NULL, updated_at = ?, closed_at = CASE WHEN ? = 'closed' THEN ? ELSE closed_at END WHERE id = ? AND workspace_id = ? AND version = ?",
                (session_status, page_revision, safe_url, title, snapshot.get("browser_version"), ciphertext, protection, identity["session_id"], now, session_status, now, row["id"], workspace_id, row["version"]),
            )
            connection.execute(
                "UPDATE browser_session_operations SET status = ?, result_json = ?, material_id = ?, error_code = ?, updated_at = ? WHERE id = ? AND session_id = ? AND status = 'pending'",
                (operation_status, json.dumps(safe_result, ensure_ascii=False, sort_keys=True), material_id, "BROWSER_NAVIGATION_UNKNOWN" if operation_status == "unknown" else None, now, operation_id, row["id"]),
            )
            updated = connection.execute("SELECT * FROM browser_sessions WHERE id = ? AND workspace_id = ?", (row["id"], workspace_id)).fetchone()
            self._write_version(connection, updated, operation, identity["user"]["id"], now)
        result.update(safe_result)
        result["replayed"] = False
        return result

    def _fail_operation(self, identity: dict, row, operation_id: str, code: str, *, uncertain: bool = False,
                        fallback_status: str | None = None, allow_reauthorize: bool = False,
                        check_work_access: bool = True, not_started: bool = False) -> None:
        now = utc_now()
        status = "unknown" if uncertain else "failed"
        with self.store.transaction() as connection:
            if not not_started:
                self._owned_session(
                    connection, identity, row["id"], allow_reauthorize=allow_reauthorize,
                    check_work_access=check_work_access,
                )
            connection.execute(
                "UPDATE browser_session_operations SET status = ?, error_code = ?, updated_at = ? "
                "WHERE id = ? AND session_id = ? AND workspace_id = ? AND owner_user_id = ? "
                "AND (status = 'pending' OR (? AND status = 'unknown'))",
                (status, code, now, operation_id, row["id"], identity["workspace"]["id"], identity["user"]["id"], not_started),
            )
            if not uncertain:
                connection.execute(
                    "UPDATE browser_sessions SET status = COALESCE(?, status), operation_id = NULL, operation_expires_at = NULL, authorized_by_session_id = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND operation_id = ?",
                    (fallback_status, identity["session_id"], now, row["id"], identity["workspace"]["id"], operation_id),
                )

    def reopen_session(self, identity: dict, session_id: str, data: dict, key: str, *,
                        actor: str | None = None) -> dict:
        if set(data) - {"base_version", "restore_page"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        restore_page = data.get("restore_page", False)
        if not isinstance(restore_page, bool):
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        request_payload = {"base_version": base_version, "restore_page": restore_page}
        operation_id, row, replay = self._reserve(
            identity, session_id, "reopen", key, request_payload, base_version,
            allow_reauthorize=True, allowed_statuses={"suspended", "open", "error"},
            transition_status="reopening", actor=actor,
        )
        if replay is not None:
            return replay
        if row["status"] == "closed":
            raise BrowserError("BROWSER_SESSION_CLOSED", 409)
        try:
            if actor is not None:
                self.assert_control_operation(identity, session_id, row, operation_id, actor)
            payload = self._unprotect_payload(row)
            result = self.runtime.reopen(row["id"], row["workspace_id"], row["owner_user_id"], row["engine"], payload)
            if restore_page:
                if actor is not None:
                    self.assert_control_operation(identity, session_id, row, operation_id, actor)
                result = self.runtime.restore_page(
                    row["engine"], session_id,
                    before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
                )
            snapshot = result["snapshot"]
            if snapshot.get("restore_required"):
                snapshot["runtime_url"] = snapshot.get("url")
                snapshot["url"] = snapshot["restore_url"]
                snapshot["title"] = snapshot.get("restore_title") or row["current_title"]
            result_payload = {
                **_plain_snapshot(snapshot),
                "restore_required": bool(snapshot.get("restore_required")),
                "login_state": "NOT_VERIFIED",
            }
            return self._complete_operation(
                identity, row, operation_id, "reopened", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=result_payload,
                allow_reauthorize=True,
                operation_status="unknown" if snapshot.get("operation_state") == "unknown" else "completed",
            )
        except BrowserError as exc:
            self._fail_operation(identity, row, operation_id, exc.code, allow_reauthorize=True, fallback_status="suspended",
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            raise

    def restore_page(self, identity: dict, session_id: str, data: dict, key: str, *,
                     actor: str | None = None) -> dict:
        allowed = {"base_version", "page_revision", "page_id"}
        if set(data) - allowed:
            raise BrowserError("BROWSER_NAVIGATION_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        page_revision = _page_revision(data.get("page_revision"))
        page_id = data.get("page_id")
        payload = {"base_version": base_version, "page_revision": page_revision, "page_id": page_id}
        operation_id, row, replay = self._reserve(
            identity, session_id, "restore_page", key, payload, base_version, actor=actor,
        )
        if replay is not None:
            return replay
        try:
            if actor is not None:
                self.assert_control_operation(identity, session_id, row, operation_id, actor)
            result = self.runtime.restore_page(
                row["engine"], session_id, page_id, page_revision,
                before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
            )
            snapshot = result["snapshot"]
            status = "unknown" if snapshot.get("operation_state") == "unknown" else "completed"
            return self._complete_operation(
                identity, row, operation_id, "page_restored", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=_plain_snapshot(snapshot),
                operation_status=status,
            )
        except BrowserError as exc:
            self._fail_operation(identity, row, operation_id, exc.code, uncertain=exc.code == "BROWSER_OPERATION_STATE_UNKNOWN",
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            raise

    def checkpoint(self, identity: dict, session_id: str, data: dict, key: str) -> dict:
        allowed = {"base_version", "page_revision", "page_id"}
        if set(data) - allowed:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        page_revision = _page_revision(data.get("page_revision"))
        page_id = data.get("page_id")
        payload = {"base_version": base_version, "page_revision": page_revision, "page_id": page_id}
        operation_id, row, replay = self._reserve(identity, session_id, "checkpoint", key, payload, base_version)
        if replay is not None:
            return replay
        try:
            result = self.runtime.checkpoint(row["engine"], session_id, page_id, page_revision)
            snapshot = result["snapshot"]
            return self._complete_operation(
                identity, row, operation_id, "checkpointed", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=_plain_snapshot(snapshot),
            )
        except BrowserError as exc:
            self._fail_operation(identity, row, operation_id, exc.code, uncertain=exc.code == "BROWSER_OPERATION_STATE_UNKNOWN",
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            raise

    def suspend(self, identity: dict, session_id: str, data: dict, key: str) -> dict:
        if set(data) != {"base_version"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        payload = {"base_version": base_version}
        operation_id, row, replay = self._reserve(
            identity, session_id, "suspend", key, payload, base_version,
            allow_reauthorize=True, check_work_access=False,
        )
        if replay is not None:
            return replay
        try:
            result = self.runtime.suspend(row["engine"], session_id)
            snapshot = result["snapshot"]
            return self._complete_operation(
                identity, row, operation_id, "suspended", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=_plain_snapshot(snapshot),
                session_status="suspended", allow_reauthorize=True, check_work_access=False,
            )
        except BrowserError as exc:
            self._fail_operation(
                identity, row, operation_id, exc.code,
                uncertain=exc.code == "BROWSER_OPERATION_STATE_UNKNOWN",
                allow_reauthorize=True, check_work_access=False,
            )
            raise

    def close_session(self, identity: dict, session_id: str, data: dict, key: str) -> dict:
        if set(data) != {"base_version"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        payload = {"base_version": base_version}
        operation_id, row, replay = self._reserve(
            identity, session_id, "close", key, payload, base_version,
            allow_reauthorize=True, allowed_statuses={"open", "suspended", "error", "starting", "reopening"},
            check_work_access=False,
        )
        if replay is not None:
            return replay
        self.runtime.close(row["engine"], session_id)
        snapshot = {
            "session_id": session_id, "engine_version": row["browser_version"],
            "url": row["current_url"], "title": row["current_title"],
            "page_revision": row["page_revision"], "login_state": "NOT_VERIFIED",
        }
        return self._complete_operation(
            identity, row, operation_id, "closed", snapshot=snapshot,
            storage_payload=None, result=_plain_snapshot(snapshot),
            session_status="closed", clear_state=True, allow_reauthorize=True,
            check_work_access=False,
        )

    def get_operation(self, identity: dict, session_id: str, operation_id: str) -> dict:
        with self.store.transaction(immediate=False) as connection:
            session = self._owned_session(connection, identity, session_id, allow_reauthorize=True)
            row = connection.execute(
                "SELECT id, operation, status, result_json, material_id, error_code, created_at, updated_at FROM browser_session_operations WHERE id = ? AND session_id = ? AND workspace_id = ? AND owner_user_id = ?",
                (operation_id, session_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
            if row is None:
                raise BrowserError("BROWSER_OPERATION_NOT_FOUND", 404)
            return {
                "operation": row["operation"], "status": row["status"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "material_id": row["material_id"], "error_code": row["error_code"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "control_mode": session["control_mode"],
                "control_generation": session["control_generation"],
            }

    def get_operation_status(self, identity: dict, session_id: str) -> dict:
        """Read the latest session operation without exposing its opaque ID."""
        with self.store.transaction(immediate=False) as connection:
            session = self._owned_session(connection, identity, session_id, allow_reauthorize=True)
            row = connection.execute(
                "SELECT operation, status, result_json, material_id, error_code, created_at, updated_at "
                "FROM browser_session_operations WHERE session_id = ? AND workspace_id = ? AND owner_user_id = ? "
                "AND operation != 'context_preview' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (session_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
            if row is None:
                return {"operation": None, "status": "none", "result": None, "material_id": None,
                        "error_code": None, "created_at": None, "updated_at": None,
                        "control_mode": session["control_mode"],
                        "control_generation": session["control_generation"]}
            result = json.loads(row["result_json"]) if row["result_json"] else None
            if isinstance(result, dict):
                result = {key: value for key, value in result.items() if key not in {"operation_id", "id"}}
            return {
                "operation": row["operation"], "status": row["status"], "result": result,
                "material_id": row["material_id"], "error_code": row["error_code"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "control_mode": session["control_mode"],
                "control_generation": session["control_generation"],
            }

    def reconcile_session(self, identity: dict, session_id: str, data: dict, key: str, *,
                          actor: str | None = None,
                          expected_control_generation: int | None = None) -> dict:
        """Inspect current browser reality and recover the session without replaying an unknown action."""
        if set(data) != {"base_version"}:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        payload = {"base_version": base_version}
        reserve_args = (
            identity, session_id, "reconcile", key, payload, base_version,
        )
        reserve_options = {
            "allow_reauthorize": True,
            "allowed_statuses": {"open", "suspended", "error", "starting", "reopening"},
            "actor": actor,
            "expected_control_generation": expected_control_generation,
        }
        previous_operation = None
        try:
            operation_id, row, replay = self._reserve(*reserve_args, **reserve_options)
        except BrowserError as exc:
            if exc.code != "BROWSER_OPERATION_UNKNOWN":
                raise
            stale_id = exc.details.get("operation_id")
            if not isinstance(stale_id, str):
                raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"reconcile_required": True}) from exc
            with self.store.transaction(immediate=False) as connection:
                stale = connection.execute(
                    "SELECT operation, status, error_code, updated_at FROM browser_session_operations "
                    "WHERE id = ? AND session_id = ? AND workspace_id = ? AND owner_user_id = ?",
                    (stale_id, session_id, identity["workspace"]["id"], identity["user"]["id"]),
                ).fetchone()
            if stale is None or stale["operation"] == "reconcile":
                raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409, {"reconcile_required": True}) from exc
            previous_operation = {
                "operation": stale["operation"], "status": stale["status"],
                "error_code": stale["error_code"], "updated_at": stale["updated_at"],
            }
            # The first reserve recorded the previous effect as unknown and
            # cleared only its expired lease. This second reserve uses the new
            # reconciliation key; it never retries the old operation.
            operation_id, row, replay = self._reserve(*reserve_args, **reserve_options)
        if replay is not None:
            return replay

        if previous_operation is None:
            previous_operation = self._latest_unknown_operation(identity, session_id)
        try:
            if actor is not None:
                self.assert_control_operation(identity, session_id, row, operation_id, actor)
            if self.runtime.is_open(row["engine"], session_id):
                snapshot = self.runtime.read_page(row["engine"], session_id)
                storage_payload = self.runtime.storage_payload(row["engine"], session_id)
                recovery_state = "live_page_inspected"
                restore_required = False
            else:
                if actor is not None:
                    self.assert_control_operation(identity, session_id, row, operation_id, actor)
                saved = self._unprotect_payload(row)
                reopened = self.runtime.reopen(
                    row["id"], row["workspace_id"], row["owner_user_id"], row["engine"], saved,
                )
                snapshot = reopened["snapshot"]
                storage_payload = reopened["storage_payload"]
                snapshot["runtime_url"] = snapshot.get("url")
                snapshot["restore_url"] = row["current_url"]
                snapshot["url"] = row["current_url"]
                recovery_state = "session_reopened_without_navigation"
                restore_required = bool(saved and saved.get("restore_url"))
            snapshot["restore_required"] = restore_required
            response = {
                **_plain_snapshot(snapshot),
                "recovery_state": recovery_state,
                "previous_operation": previous_operation,
            }
            return self._complete_operation(
                identity, row, operation_id, "reconciled", snapshot=snapshot,
                storage_payload=storage_payload, result=response,
                allow_reauthorize=True,
            )
        except BrowserError as exc:
            # Reconciliation itself has no remote side effect. Preserve the
            # saved session state and make a fresh inspection possible.
            try:
                runtime_live = self.runtime.is_open(row["engine"], session_id)
            except Exception:
                runtime_live = False
            self._fail_operation(
                identity, row, operation_id, exc.code, uncertain=False,
                allow_reauthorize=True, fallback_status="open" if runtime_live else "suspended",
            )
            raise

    def _latest_unknown_operation(self, identity: dict, session_id: str) -> dict | None:
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute(
                "SELECT operation, status, error_code, updated_at FROM browser_session_operations "
                "WHERE session_id = ? AND workspace_id = ? AND owner_user_id = ? AND status = 'unknown' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (session_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
        return dict(row) if row is not None else None


    def navigate(self, identity: dict, session_id: str, data: dict, key: str, *,
                 actor: str | None = None, expected_control_generation: int | None = None) -> dict:
        allowed = {"base_version", "page_revision", "url", "page_id"}
        if set(data) - allowed:
            raise BrowserError("BROWSER_NAVIGATION_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        page_revision = _page_revision(data.get("page_revision"))
        url = data.get("url")
        if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 32 for char in url):
            raise BrowserError("BROWSER_TARGET_INVALID", 400)
        self.url_validator(url)
        page_id = data.get("page_id")
        if page_id is not None and (not isinstance(page_id, str) or len(page_id) > 80):
            raise BrowserError("BROWSER_NAVIGATION_INVALID", 400)
        payload = {"base_version": base_version, "page_revision": page_revision, "url": url.strip(), "page_id": page_id}
        operation_id, row, replay = self._reserve(
            identity, session_id, "navigate", key, payload, base_version,
            actor=actor, expected_control_generation=expected_control_generation,
        )
        if replay is not None:
            return replay
        try:
            if actor is not None:
                self.assert_control_operation(identity, session_id, row, operation_id, actor)
            result = self.runtime.navigate(
                row["engine"], session_id, url.strip(), page_id, page_revision,
                before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
            )
            snapshot = result["snapshot"]
            status = "unknown" if snapshot.get("operation_state") == "unknown" else "completed"
            response = self._complete_operation(
                identity, row, operation_id, "navigated", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=_plain_snapshot(snapshot),
                operation_status=status,
            )
            return response
        except BrowserError as exc:
            uncertain = exc.code in {"BROWSER_OPERATION_STATE_UNKNOWN", "BROWSER_NAVIGATION_UNKNOWN"}
            self._fail_operation(identity, row, operation_id, exc.code, uncertain=uncertain,
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            raise

    def read_page(self, identity: dict, session_id: str, data: dict, *,
                  actor: str | None = None, expected_control_generation: int | None = None) -> dict:
        if set(data) - {"page_revision", "page_id", "base_version"}:
            raise BrowserError("BROWSER_READ_INVALID", 400)
        expected_version = _base_version(data["base_version"]) if "base_version" in data else None
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id)
            self._require_control_actor(row, actor, expected_control_generation)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
            if expected_version is not None and row["version"] != expected_version:
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": row["version"]})
            if actor is not None and row["operation_id"]:
                raise BrowserError("BROWSER_SESSION_BUSY", 409)
        page_id = data.get("page_id")
        if actor is not None:
            self.assert_control_state(identity, session_id, actor, row["control_generation"])
        result = self.runtime.read_page(
            row["engine"], session_id, page_id,
            before_dispatch=lambda: self.assert_control_state(identity, session_id, actor, row["control_generation"]),
        )
        expected = data.get("page_revision")
        if expected is not None and _page_revision(expected) != result["page_revision"]:
            raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": result["page_revision"]})
        with self.store.transaction() as connection:
            current = self._owned_session(connection, identity, session_id)
            self._require_control_actor(current, actor, row["control_generation"])
            if (current["version"] != row["version"] or current["status"] != "open"
                    or expected_version is not None and current["version"] != expected_version):
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            connection.execute(
                "UPDATE browser_sessions SET page_revision = ?, current_url = ?, current_title = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (result["page_revision"], result["url"], result["title"], utc_now(), session_id, identity["workspace"]["id"], current["version"]),
            )
        return {"session_id": session_id, "version": row["version"], **result}

    def read_context(self, identity: dict, session_id: str, data: dict, *,
                     actor: str | None = None, expected_control_generation: int | None = None) -> dict:
        """Read the selected live page with its owner, revision and coverage intact."""
        from .browser_host_reader import read_browser_context

        if set(data) - {"page_id", "page_revision", "base_version", "include_frames", "retain_preview"}:
            raise BrowserError("BROWSER_READ_INVALID", 400)
        include_frames = data.get("include_frames", True)
        retain_preview = data.get("retain_preview", False)
        if not isinstance(include_frames, bool) or not isinstance(retain_preview, bool):
            raise BrowserError("BROWSER_READ_INVALID", 400)
        expected_revision = _page_revision(data["page_revision"]) if "page_revision" in data else None
        expected_version = _base_version(data["base_version"]) if "base_version" in data else None
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id)
            self._require_control_actor(row, actor, expected_control_generation)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
            if expected_version is not None and row["version"] != expected_version:
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": row["version"]})
        result = read_browser_context(
            self.app.extensions["oevira.browser_host"].runtime, row["engine"], session_id, data.get("page_id"),
            include_frames=include_frames,
            before_dispatch=lambda: self.assert_control_state(
                identity, session_id, actor, row["control_generation"], allow_pending_read=actor is None,
            ),
        )
        if expected_revision is not None and result["page_revision"] != expected_revision:
            raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": result["page_revision"]})
        with self.store.transaction() as connection:
            current = self._owned_session(connection, identity, session_id)
            self._require_control_actor(current, actor, row["control_generation"])
            if current["version"] != row["version"] or current["status"] != "open":
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            connection.execute(
                "UPDATE browser_sessions SET page_revision=?,current_url=?,current_title=?,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (result["page_revision"], result["url"], result["title"], utc_now(), session_id, identity["workspace"]["id"], current["version"]),
            )
            if retain_preview:
                result["capture_ref"] = self._retain_context_preview(connection, identity, current, result)
        return {**result, "session_id": session_id, "version": row["version"],
                "work_id": row["work_id"], "control_generation": row["control_generation"]}

    def _retain_context_preview(self, connection, identity: dict, session, snapshot: dict) -> dict:
        now = utc_now()
        capture_id = str(uuid.uuid4())
        reference = {
            "capture_id": capture_id, "session_id": session["id"], "page_id": snapshot["page_id"],
            "page_revision": snapshot["page_revision"], "work_id": session["work_id"],
            "captured_at": snapshot["snapshot_version"]["captured_at"],
            "expires_at": _expiry_text(15 * 60),
            "snapshot_sha256": snapshot["snapshot_version"]["sha256"],
            "preview_sha256": _request_fingerprint(snapshot),
            "projection_kind": "sanitized_reader", "projection_version": 1,
            "coverage": snapshot["coverage"], "source_admission": "NOT_ADMITTED",
        }
        connection.execute(
            "UPDATE browser_session_operations SET preview_json=NULL WHERE session_id=? AND preview_expires_at<=?",
            (session["id"], now),
        )
        # Keep receipts after eviction; only bounded, explicitly retained preview bodies occupy the cache.
        connection.execute(
            "UPDATE browser_session_operations SET preview_json=NULL WHERE session_id=? AND preview_json IS NOT NULL "
            "AND id NOT IN (SELECT id FROM browser_session_operations WHERE session_id=? AND preview_json IS NOT NULL "
            "ORDER BY created_at DESC,rowid DESC LIMIT 19)", (session["id"], session["id"]),
        )
        connection.execute(
            "INSERT INTO browser_session_operations(id,session_id,workspace_id,owner_user_id,operation,idempotency_key,"
            "request_hash,status,result_json,preview_json,preview_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?, 'context_preview',?,?,'completed',?,?,?,?,?)",
            (capture_id, session["id"], identity["workspace"]["id"], identity["user"]["id"], capture_id,
             reference["snapshot_sha256"], json.dumps({"capture_ref": reference}, ensure_ascii=False),
             json.dumps(snapshot, ensure_ascii=False), reference["expires_at"], now, now),
        )
        return reference

    def _context_preview(self, connection, identity: dict, session_id: str, capture_id: str, *,
                         field_paths=("text", "links", "controls"), purpose="knowledge_retrieval", action="read"):
        if not isinstance(capture_id, str) or not capture_id or len(capture_id) > 80:
            raise BrowserError("BROWSER_PREVIEW_NOT_FOUND", 404)
        row = connection.execute(
            "SELECT * FROM browser_session_operations WHERE id=? AND session_id=? AND workspace_id=? "
            "AND owner_user_id=? AND operation='context_preview'",
            (capture_id, session_id, identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        if row is None:
            raise BrowserError("BROWSER_PREVIEW_NOT_FOUND", 404)
        try:
            reference = json.loads(row["result_json"])["capture_ref"]
            if not isinstance(reference, dict) or reference.get("capture_id") != capture_id or reference.get("session_id") != session_id:
                raise ValueError("Preview receipt identity changed")
        except (ValueError, KeyError, TypeError):
            raise BrowserError("BROWSER_PREVIEW_INTEGRITY_ERROR", 409) from None
        if reference.get("work_id"):
            require_work_access(connection, identity, reference["work_id"])
        if row["preview_discarded_at"]:
            raise BrowserError("BROWSER_PREVIEW_DISCARDED", 410)
        if not row["preview_expires_at"] or row["preview_expires_at"] <= utc_now():
            raise BrowserError("BROWSER_PREVIEW_EXPIRED", 410)
        if row["preview_json"] is None:
            raise BrowserError("BROWSER_PREVIEW_UNAVAILABLE", 410)
        try:
            snapshot = json.loads(row["preview_json"])
            valid = (_request_fingerprint(snapshot) == reference.get("preview_sha256")
                     and snapshot["session_id"] == session_id
                     and snapshot["page_id"] == reference["page_id"]
                     and snapshot["page_revision"] == reference["page_revision"]
                     and snapshot["snapshot_version"]["sha256"] == reference["snapshot_sha256"])
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise BrowserError("BROWSER_PREVIEW_INTEGRITY_ERROR", 409)
        from .source_use import SourceUseError, resolve_source_use

        if not isinstance(field_paths, (list, tuple)) or not field_paths or set(field_paths) - {"text", "links", "controls"}:
            raise BrowserError("BROWSER_READ_INVALID", 400)
        try:
            decision = resolve_source_use(
                connection, identity,
                {"kind": "browser_capture", "id": capture_id, "revision": reference["preview_sha256"],
                 "work_id": reference.get("work_id")},
                field_paths=field_paths, purpose=purpose, action=action,
                recipient={"kind": "local", "id": identity["workspace"]["id"]}, work_id=reference.get("work_id"),
            )
        except SourceUseError as exc:
            raise BrowserError(exc.code, exc.status, exc.details) from exc
        if not decision.allowed:
            raise BrowserError(decision.reason_code or "SOURCE_USE_DENIED", 403, {"source_use_state": decision.state})
        reference = {**reference, "source_use": decision.to_dict()}
        return row, reference

    def get_context_preview(self, identity: dict, session_id: str, capture_id: str, *,
                            field_paths=("text", "links", "controls"), purpose="knowledge_retrieval", action="read") -> dict:
        with self.store.transaction(immediate=False) as connection:
            session = self._owned_session(connection, identity, session_id)
            row, reference = self._context_preview(connection, identity, session_id, capture_id,
                                                   field_paths=field_paths, purpose=purpose, action=action)
            snapshot = json.loads(row["preview_json"])
            for target in (snapshot, *snapshot.get("frames", [])):
                for field in {"text", "links", "controls"} - set(field_paths):
                    target.pop(field, None)
                    if field == "text":
                        target.pop("blocks", None)
                        target.pop("selection", None)
            return {**snapshot, "capture_ref": reference, "version": session["version"],
                    "work_id": reference.get("work_id"), "control_generation": session["control_generation"]}

    def discard_context_preview(self, identity: dict, session_id: str, capture_id: str) -> dict:
        with self.store.transaction() as connection:
            self._owned_session(connection, identity, session_id, check_work_access=False)
            row = connection.execute(
                "SELECT id FROM browser_session_operations WHERE id=? AND session_id=? AND workspace_id=? "
                "AND owner_user_id=? AND operation='context_preview'",
                (capture_id, session_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
            if row is None:
                raise BrowserError("BROWSER_PREVIEW_NOT_FOUND", 404)
            connection.execute(
                "UPDATE browser_session_operations SET preview_json=NULL,preview_discarded_at=COALESCE(preview_discarded_at,?),"
                "updated_at=? WHERE id=?", (utc_now(), utc_now(), capture_id),
            )
        return {"capture_id": capture_id, "preview_state": "discarded", "receipt_retained": True,
                "saved_materials_affected": False, "physical_erasure": "NOT_VERIFIED"}

    def screenshot(self, identity: dict, session_id: str, page_id: str | None) -> tuple[bytes, dict]:
        with self.store.transaction(immediate=False) as connection:
            row = self._owned_session(connection, identity, session_id)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
        image, metadata = self.runtime.screenshot(row["engine"], session_id, page_id)
        with self.store.transaction() as connection:
            current = self._owned_session(connection, identity, session_id)
            if current["version"] != row["version"] or current["status"] != "open":
                raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current["version"]})
            connection.execute(
                "UPDATE browser_sessions SET page_revision = ?, current_url = ?, current_title = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (metadata["page_revision"], metadata["url"], metadata["title"], utc_now(), session_id, identity["workspace"]["id"], current["version"]),
            )
        return image, {"session_id": session_id, "version": row["version"], **metadata}

    def act(self, identity: dict, session_id: str, data: dict, key: str, *,
            actor: str | None = None, expected_control_generation: int | None = None) -> dict:
        allowed = {"base_version", "page_revision", "page_id", "action"}
        if set(data) - allowed:
            raise BrowserError("BROWSER_ACTION_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        page_revision = _page_revision(data.get("page_revision"))
        action = _safe_action(data.get("action"))
        page_id = data.get("page_id")
        payload = {"base_version": base_version, "page_revision": page_revision, "page_id": page_id, "action": action}
        operation_id, row, replay = self._reserve(
            identity, session_id, "action", key, payload, base_version,
            actor=actor, expected_control_generation=expected_control_generation,
        )
        if replay is not None:
            return replay
        try:
            if actor is not None:
                self.assert_control_operation(identity, session_id, row, operation_id, actor)
            result = self.runtime.act(
                row["engine"], session_id, action, page_id, page_revision,
                before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
            )
            snapshot = result["snapshot"]
            status = "unknown" if snapshot.get("operation_state") == "unknown" else "completed"
            response = self._complete_operation(
                identity, row, operation_id, "action", snapshot=snapshot,
                storage_payload=result["storage_payload"], result=_plain_snapshot(snapshot),
                operation_status=status,
            )
            return response
        except BrowserError as exc:
            self._fail_operation(identity, row, operation_id, exc.code, uncertain=exc.code == "BROWSER_OPERATION_STATE_UNKNOWN",
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            raise

    def capture(self, identity: dict, session_id: str, data: dict, key: str, *,
                actor: str | None = None, expected_control_generation: int | None = None) -> tuple[dict, int]:
        if set(data) - {"base_version", "page_revision", "page_id", "scope", "frame_id", "frame_sha256", "block_id", "capture_id"}:
            raise BrowserError("BROWSER_CAPTURE_INVALID", 400)
        base_version = _base_version(data.get("base_version"))
        page_revision = _page_revision(data.get("page_revision"))
        scope = data.get("scope", "selection")
        if scope not in {"selection", "page", "reader_frame", "reader_block"}:
            raise BrowserError("BROWSER_CAPTURE_SCOPE_INVALID", 400)
        page_id = data.get("page_id")
        capture_id = data.get("capture_id")
        if "capture_id" in data and (not isinstance(capture_id, str) or not capture_id or len(capture_id) > 80
                                     or not scope.startswith("reader_")):
            raise BrowserError("BROWSER_CAPTURE_INVALID", 400)
        request_payload = {"base_version": base_version, "page_revision": page_revision, "page_id": page_id, "scope": scope}
        if capture_id:
            request_payload["capture_id"] = capture_id
        if scope.startswith("reader_"):
            request_payload.update({key: data.get(key) for key in ("frame_id", "frame_sha256", "block_id")})
            if scope == "reader_block" and not isinstance(data.get("block_id"), str):
                raise BrowserError("BROWSER_CAPTURE_INVALID", 400)
        elif set(data) & {"frame_id", "frame_sha256", "block_id"}:
            raise BrowserError("BROWSER_CAPTURE_INVALID", 400)
        operation_id, row, replay = self._reserve(
            identity, session_id, "capture", key, request_payload, base_version,
            actor=actor, expected_control_generation=expected_control_generation,
            allowed_statuses={"open", "suspended", "closed", "error"} if capture_id else None,
        )
        if replay is not None:
            material = self.store.row(
                "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                (replay["material_id"], identity["workspace"]["id"]),
            )
            if material is None:
                raise BrowserError("BROWSER_OPERATION_RESULT_MISSING", 409)
            with self.store.transaction(immediate=False) as connection:
                require_material_access(connection, identity, material["id"])
                result = _material_data(connection, material)
            return {"material": result, "browser": replay, "replayed": True}, 200
        temporary_path = None
        material_id = str(uuid.uuid4())
        now = utc_now()
        try:
            self.assert_control_operation(identity, session_id, row, operation_id, actor)
            if capture_id:
                from .browser_host_reader import select_reader_snapshot

                preview = self.get_context_preview(identity, session_id, capture_id, field_paths=("text",),
                                                   purpose="source_capture", action="derive")
                if preview["page_id"] != page_id:
                    raise BrowserError("BROWSER_CAPTURE_INVALID", 400)
                snapshot = select_reader_snapshot(
                    preview, data.get("frame_id"), data.get("frame_sha256"),
                    block_id=data.get("block_id") if scope == "reader_block" else None,
                    expected_page_revision=page_revision,
                )
                capture = {"snapshot": snapshot, "storage_payload": None}
            elif scope.startswith("reader_"):
                from .browser_host_reader import capture_reader_context

                capture = capture_reader_context(
                    self.app.extensions["oevira.browser_host"].runtime, row["engine"], session_id,
                    page_id, data.get("frame_id"), data.get("frame_sha256"),
                    block_id=data.get("block_id") if scope == "reader_block" else None,
                    expected_page_revision=page_revision,
                    before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
                )
            else:
                capture = self.runtime.capture(
                    row["engine"], session_id, scope, page_id, page_revision,
                    before_dispatch=lambda: self.assert_control_operation(identity, session_id, row, operation_id, actor),
                )
            self.assert_control_operation(identity, session_id, row, operation_id, actor)
            snapshot = capture["snapshot"]
            source_url = snapshot.get("source_url", snapshot["url"])
            text = snapshot["text"]
            if not isinstance(text, str) or not text.strip():
                raise BrowserError("BROWSER_PAGE_TEXT_UNAVAILABLE", 409)
            if len(text) > MAX_CAPTURE_CHARACTERS:
                raise BrowserError("BROWSER_CAPTURE_TOO_LARGE", 413)
            content = text.encode("utf-8")
            title = (snapshot.get("source_title") or snapshot.get("title") or urlsplit(source_url).hostname or "Browser capture").strip()[:200]
            filename = _material_filename(title)
            parsed = _parse_material(filename, content)
            if parsed["status"] not in {"ready", "partial"}:
                raise BrowserError("BROWSER_SOURCE_SAVE_FAILED", 422)
            partial = scope in {"page", "reader_frame", "reader_block"} and snapshot.get("text_truncated", False)
            material_status = "partial" if partial or parsed["status"] == "partial" else "ready"
            reasons = []
            if partial:
                reasons.append("The requested page text exceeded the bounded read; coverage is partial.")
            if parsed["reason"]:
                reasons.append(parsed["reason"])
            reason = "; ".join(reasons) or None
            digest = hashlib.sha256(content).hexdigest()
            relative_path = f"objects/{identity['workspace']['id']}/materials/{material_id}/original"
            target = _resource_path(self.store, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=".browser-material-", dir=target.parent)
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

            metadata = {
                "kind": "controlled_browser_snapshot",
                "capture_mode": "user_selection" if scope == "selection" else scope if scope.startswith("reader_") else "visible_page_text",
                "source_url": source_url,
                "redacted_query_keys": snapshot.get("redacted_query_keys", []),
                "captured_at": snapshot.get("captured_at", now),
                "capture_id": capture_id or operation_id,
                "capture_origin": "frozen_preview" if capture_id else "live_page",
                "snapshot_sha256": snapshot.get("snapshot_sha256"),
                "projection_kind": "sanitized_reader" if scope.startswith("reader_") else "visible_page_text",
                "projection_version": 1,
                "actual_access_class": "controlled_browser_session",
                "authentication_state": "NOT_VERIFIED",
                "source_admission": "NOT_ADMITTED",
                "content_trust": snapshot["content_trust"],
                "page_revision": snapshot["page_revision"],
                "scope": snapshot["scope"],
                "page_text_sha256": snapshot.get("page_text_sha256"),
                "frame_id": snapshot.get("frame_id"),
                "frame_sha256": snapshot.get("frame_sha256"),
                "selection_context": snapshot.get("selection_context"),
                "text_sha256": digest,
                "original_resource_sha256": None,
                "limitations": snapshot["limitations"] + ([reason] if reason else []),
            }
            if scope.startswith("reader_"):
                metadata["coverage"] = snapshot.get("coverage")
            elif scope == "page" and snapshot.get("text_truncated"):
                metadata["coverage"] = "partial_visible_page_text"
            else:
                metadata["coverage"] = "selected_page_text" if scope == "selection" else "visible_page_text"
            if capture_id:
                metadata["source_use_source_ref"] = {
                    "kind": "browser_capture", "id": capture_id,
                    "revision": preview["capture_ref"]["preview_sha256"],
                    "work_id": preview["capture_ref"].get("work_id"),
                }
            ciphertext = row["state_ciphertext"] if capture_id else self._protect_payload(row, capture["storage_payload"])
            protection = row["state_protection"] if capture_id else self.protector.protection_name

            # Material bytes are staged first; the shared Material row and browser
            # operation become visible together, using the existing file layout.
            with self.store.transaction() as connection:
                current = self._owned_session(connection, identity, session_id)
                self._require_control_actor(current, actor, row["control_generation"])
                if self._membership(connection, identity)["role"] not in {"owner", "editor"}:
                    raise BrowserError("ACCESS_REVOKED", 403)
                if current["work_id"]:
                    try:
                        require_work_access(connection, identity, current["work_id"], write=True)
                    except ApiError as exc:
                        raise BrowserError(exc.code, exc.status, exc.details) from exc
                if capture_id:
                    self._context_preview(connection, identity, session_id, capture_id, field_paths=("text",),
                                          purpose="source_capture", action="derive")
                if (current["version"] != base_version or current["operation_id"] != operation_id
                        or current["control_generation"] != row["control_generation"]):
                    raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current["version"]})
                if current["work_id"]:
                    work = connection.execute(
                        "SELECT status FROM works WHERE id = ? AND workspace_id = ?",
                        (current["work_id"], identity["workspace"]["id"]),
                    ).fetchone()
                    if work is None:
                        raise BrowserError("BROWSER_WORK_NOT_FOUND", 404)
                    if work["status"] != "active":
                        raise BrowserError("BROWSER_WORK_ARCHIVED", 409)
                connection.execute(
                    "INSERT INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason, text, sha256, relative_path, size_bytes, source_url, etag, last_modified, source_json, source_text, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                    (
                        material_id, identity["workspace"]["id"], identity["user"]["id"], current["work_id"],
                        title, filename, parsed["format"], "Browser capture file is being finalized.",
                        parsed["text"], digest, relative_path, len(content), source_url,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True), parsed["text"], now,
                    ),
                )
                # A pending row is left recoverable if the following atomic file move fails.
                session_snapshot = ({"page_revision": current["page_revision"], "url": current["current_url"],
                                     "title": current["current_title"]} if capture_id else
                                    {key: snapshot[key] for key in ("page_revision", "url", "title")})
                connection.execute(
                    "UPDATE browser_sessions SET version = version + 1, page_revision = ?, current_url = ?, current_title = ?, state_ciphertext = ?, state_protection = ?, authorized_by_session_id = ?, operation_id = NULL, operation_expires_at = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ? AND operation_id = ?",
                    (session_snapshot["page_revision"], session_snapshot["url"], session_snapshot["title"], ciphertext, protection, identity["session_id"], now, session_id, identity["workspace"]["id"], base_version, operation_id),
                )
                session_row = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
                self._write_version(connection, session_row, "material_captured", identity["user"]["id"], now)
                result = {
                    "session_id": session_id, "version": session_row["version"], **session_snapshot,
                    "material_id": material_id, "capture_id": capture_id or operation_id,
                    "captured_page_revision": snapshot["page_revision"],
                    "control_mode": session_row["control_mode"], "control_generation": session_row["control_generation"],
                    "coverage": metadata["coverage"], "source_admission": "NOT_ADMITTED",
                }
                connection.execute(
                    "UPDATE browser_session_operations SET status = 'pending', material_id = ?, result_json = ?, updated_at = ? WHERE id = ? AND session_id = ?",
                    (material_id, json.dumps(result, ensure_ascii=False, sort_keys=True), now, operation_id, session_id),
                )
            os.replace(temporary_path, target)
            temporary_path = None
            readback = target.read_bytes()
            if hashlib.sha256(readback).hexdigest() != digest:
                raise BrowserError("BROWSER_SOURCE_SAVE_FAILED", 500)
            with self.store.transaction() as connection:
                material = connection.execute("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, identity["workspace"]["id"])).fetchone()
                if material is None:
                    raise BrowserError("BROWSER_SOURCE_SAVE_FAILED", 500)
                current_session = self._owned_session(connection, identity, session_id)
                self._require_control_actor(current_session, actor, row["control_generation"])
                if self._membership(connection, identity)["role"] not in {"owner", "editor"}:
                    raise BrowserError("ACCESS_REVOKED", 403)
                if current_session["work_id"]:
                    try:
                        require_work_access(connection, identity, current_session["work_id"], write=True)
                    except ApiError as exc:
                        raise BrowserError(exc.code, exc.status, exc.details) from exc
                if (current_session["version"] != base_version + 1 or current_session["status"] != row["status"]
                        or current_session["control_generation"] != row["control_generation"]):
                    raise BrowserError("BROWSER_SESSION_VERSION_CONFLICT", 409, {"current_version": current_session["version"]})
                if capture_id:
                    self._context_preview(connection, identity, session_id, capture_id, field_paths=("text",),
                                          purpose="source_capture", action="derive")
                connection.execute(
                    "UPDATE materials SET status = ?, reason = ?, format = ?, text = ? WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                    (material_status, reason, parsed["format"], parsed["text"], material_id, identity["workspace"]["id"]),
                )
                connection.executemany(
                    "INSERT INTO material_segments(material_id, workspace_id, id, label, text, start_offset, end_offset, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    [(material_id, identity["workspace"]["id"], *segment) for segment in parsed["segments"]],
                )
                if current_session["work_id"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                        (current_session["work_id"], material_id, identity["workspace"]["id"], utc_now()),
                    )
                connection.execute(
                    "UPDATE browser_session_operations SET status = 'completed', error_code = NULL, updated_at = ? WHERE id = ? AND session_id = ? AND status = 'pending'",
                    (utc_now(), operation_id, session_id),
                )
                response_material = connection.execute("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, identity["workspace"]["id"])).fetchone()
                material_data = _material_data(connection, response_material)
            response = {"material": material_data, "browser": {**result, "replayed": False}}
            if partial:
                return response, 206
            return response, 201
        except BrowserError as exc:
            if exc.status in {401, 403} or (capture_id and (exc.status == 410 or exc.code == "BROWSER_PREVIEW_INTEGRITY_ERROR")):
                _abort_uncommitted_capture(self.store, identity, session_id, operation_id, material_id, exc.code)
                raise
            self._fail_operation(identity, row, operation_id, exc.code, uncertain=False,
                                 not_started=exc.details.get("dispatch_state") == "not_started")
            if material_id:
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE materials SET status = 'error', reason = 'Browser capture did not complete; its original is retained for recovery.' WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                        (material_id, identity["workspace"]["id"]),
                    )
            raise
        except OSError as exc:
            self._fail_operation(identity, row, operation_id, "BROWSER_SOURCE_SAVE_FAILED", uncertain=False)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE materials SET status = 'error', reason = 'Browser capture file readback failed; the source record was preserved.' WHERE id = ? AND workspace_id = ? AND status = 'pending'",
                    (material_id, identity["workspace"]["id"]),
                )
            raise BrowserError("BROWSER_SOURCE_SAVE_FAILED", 500, {"operation_id": operation_id}) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _abort_uncommitted_capture(store, identity: dict, session_id: str, operation_id: str,
                           material_id: str, code: str) -> None:
    """Withdraw only this operation's uncommitted material when its source loses access."""
    relative_path = None
    with store.transaction() as connection:
        operation = connection.execute(
            "SELECT * FROM browser_session_operations WHERE id=? AND session_id=? AND workspace_id=? AND owner_user_id=?",
            (operation_id, session_id, identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        if operation is None or operation["status"] == "completed":
            return
        material = connection.execute(
            "SELECT * FROM materials WHERE id=? AND workspace_id=? AND owner_user_id=? AND status='pending'",
            (material_id, identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        connection.execute(
            "UPDATE browser_session_operations SET status='failed',error_code=?,material_id=NULL,result_json=NULL,updated_at=? WHERE id=?",
            (code, utc_now(), operation_id),
        )
        if material is not None and operation["material_id"] == material_id:
            relative_path = material["relative_path"]
            connection.execute("DELETE FROM material_segments WHERE material_id=?", (material_id,))
            connection.execute("DELETE FROM work_materials WHERE material_id=?", (material_id,))
            connection.execute("DELETE FROM materials WHERE id=? AND status='pending'", (material_id,))
        connection.execute(
            "UPDATE browser_sessions SET operation_id=NULL,operation_expires_at=NULL,updated_at=? "
            "WHERE id=? AND workspace_id=? AND operation_id=?",
            (utc_now(), session_id, identity["workspace"]["id"], operation_id),
        )
    if relative_path:
        try:
            _resource_path(store, relative_path).unlink(missing_ok=True)
        except OSError:
            # The row is already inaccessible; a failed unlink is not physical erasure.
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE browser_session_operations SET result_json=? WHERE id=?",
                    (json.dumps({"cleanup_state": "file_cleanup_pending", "physical_erasure": "NOT_VERIFIED"}), operation_id),
                )


def _write_startup_recovery_version(connection, session, operation: str, now: str) -> None:
    metadata = {
        "id": session["id"], "engine": session["engine"], "status": session["status"],
        "version": session["version"], "page_revision": session["page_revision"],
        "current_url": session["current_url"], "current_title": session["current_title"],
        "browser_version": session["browser_version"], "work_id": session["work_id"],
        "login_state": "NOT_VERIFIED", "state_protection": session["state_protection"],
        "last_error_code": session["last_error_code"],
    }
    connection.execute(
        "INSERT INTO browser_session_versions(session_id, workspace_id, version, operation, metadata_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (session["id"], session["workspace_id"], session["version"], operation, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, session["owner_user_id"]),
    )


def recover_browser_sessions(store) -> dict[str, int]:
    """Close volatile runtime owners and reconcile saved Material results on startup."""
    now = utc_now()
    recovered = {"sessions_suspended": 0, "operations_completed": 0, "operations_failed": 0, "operations_unknown": 0}
    # A pre-commit preview save never becomes a completed save merely because its file exists.
    # Discard/expiry/revocation may have happened while the process was absent.
    interrupted_previews = store.rows(
        "SELECT o.id,o.session_id,o.workspace_id,o.owner_user_id,o.material_id,m.source_json "
        "FROM browser_session_operations o JOIN materials m ON m.id=o.material_id AND m.workspace_id=o.workspace_id "
        "WHERE o.status='pending' AND m.status='pending'"
    )
    for operation in interrupted_previews:
        try:
            source = json.loads(operation["source_json"] or "{}")
        except (ValueError, TypeError):
            source = {}
        if isinstance(source, dict) and source.get("capture_origin") == "frozen_preview":
            _abort_uncommitted_capture(
                store, {"workspace": {"id": operation["workspace_id"]}, "user": {"id": operation["owner_user_id"]}},
                operation["session_id"], operation["id"], operation["material_id"], "BROWSER_PREVIEW_SAVE_INTERRUPTED",
            )
            recovered["operations_failed"] += 1
    with store.transaction() as connection:
        pending = connection.execute(
            "SELECT * FROM browser_session_operations WHERE status = 'pending' ORDER BY created_at, id"
        ).fetchall()
        for operation in pending:
            material = None
            if operation["material_id"]:
                material = connection.execute(
                    "SELECT status FROM materials WHERE id = ? AND workspace_id = ?",
                    (operation["material_id"], operation["workspace_id"]),
                ).fetchone()
            if material and material["status"] in {"ready", "partial"}:
                status = "completed"
                error_code = None
                recovered["operations_completed"] += 1
            elif material and material["status"] == "error":
                status = "failed"
                error_code = "BROWSER_SOURCE_SAVE_FAILED"
                recovered["operations_failed"] += 1
            else:
                status = "unknown"
                error_code = "BROWSER_OPERATION_STATE_UNKNOWN"
                recovered["operations_unknown"] += 1
            connection.execute(
                "UPDATE browser_session_operations SET status = ?, error_code = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (status, error_code, now, operation["id"]),
            )

        rows = connection.execute(
            "SELECT * FROM browser_sessions WHERE status IN ('starting', 'open', 'reopening') ORDER BY id"
        ).fetchall()
        for row in rows:
            status = "suspended" if row["state_ciphertext"] else "error"
            error_code = "BROWSER_RUNTIME_RESTARTED" if row["state_ciphertext"] else "BROWSER_SESSION_STATE_UNAVAILABLE"
            connection.execute(
                "UPDATE browser_sessions SET status = ?, version = version + 1, operation_id = NULL, operation_expires_at = NULL, last_error_code = ?, updated_at = ? WHERE id = ? AND version = ?",
                (status, error_code, now, row["id"], row["version"]),
            )
            updated = connection.execute("SELECT * FROM browser_sessions WHERE id = ?", (row["id"],)).fetchone()
            _write_startup_recovery_version(connection, updated, "runtime_restarted", now)
            recovered["sessions_suspended"] += int(status == "suspended")
    return recovered

def _expiry_text(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _material_filename(title: str) -> str:
    normalized = "".join(character for character in title if character.isprintable() and character not in "\\/:*?\"<>|").strip()
    if not normalized:
        normalized = "Browser capture"
    return f"{normalized[:180]}.txt"


def register_browser_routes(app) -> None:
    """Register authenticated browser-session consumers on the product app."""
    store = app.extensions.get("oevira.store")
    if store is None:
        raise RuntimeError("Browser routes require the product Store extension")
    if "browser_list_sessions" in app.view_functions:
        return
    service = BrowserService(app, store)
    app.extensions["oevira.browser"] = service
    app.extensions["oevira.browser_recovery"] = recover_browser_sessions(store)

    @app.get("/api/browser-sessions")
    def browser_list_sessions():
        try:
            return _success(service.list_sessions(g.identity))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions")
    def browser_create_session():
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to create a browser session", 400)
        try:
            result, status = service.create_session(identity, data, key)
            return _success(result, status)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>")
    def browser_get_session(session_id: str):
        try:
            return _success(service.get_session(g.identity, session_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/versions")
    def browser_list_versions(session_id: str):
        try:
            return _success(service.get_versions(g.identity, session_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/reopen")
    def browser_reopen_session(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to reopen a browser session", 400)
        try:
            result = service.reopen_session(identity, session_id, data, key, actor="user")
            return _success(result, 200)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/restore-page")
    def browser_restore_page(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to restore the saved page", 400)
        try:
            result = service.restore_page(identity, session_id, data, key, actor="user")
            return _success(result, 202 if result.get("operation_state") == "unknown" else 200)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/navigate")
    def browser_navigate(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for navigation", 400)
        try:
            result = service.navigate(identity, session_id, data, key, actor="user")
            return _success(result, 202 if result.get("operation_state") == "unknown" else 200)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/read")
    def browser_read_page(session_id: str):
        identity = _require_write()
        data = _body()
        try:
            response = _success(service.read_page(identity, session_id, data))
            response[0].headers["Cache-Control"] = "no-store, private"
            response[0].headers["Pragma"] = "no-cache"
            return response
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/screenshot")
    def browser_screenshot(session_id: str):
        page_id = request.args.get("page_id") or None
        try:
            content, metadata = service.screenshot(g.identity, session_id, page_id)
            response = Response(content, mimetype="image/png")
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-OEVIRA-Page-Revision"] = str(metadata["page_revision"])
            return response
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/actions")
    def browser_action(session_id: str):
        identity = _require_write()
        if request.content_length is not None and request.content_length > 16_384:
            raise ApiError("REQUEST_TOO_LARGE", "Browser action request exceeds the size limit", 413)
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for browser actions", 400)
        try:
            result = service.act(identity, session_id, data, key, actor="user")
            return _success(result, 202 if result.get("operation_state") == "unknown" else 200)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/capture")
    def browser_capture(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to save a browser capture", 400)
        try:
            result, status = service.capture(identity, session_id, data, key, actor="user")
            return _success(result, status)
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/context-previews")
    def browser_retain_context_preview(session_id: str):
        try:
            data = _body()
            data["retain_preview"] = True
            response = _success(service.read_context(g.identity, session_id, data), 201)
            response[0].headers["Cache-Control"] = "no-store, private"
            return response
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/context-previews/<capture_id>")
    def browser_get_context_preview(session_id: str, capture_id: str):
        try:
            if set(request.args) - {"fields"}:
                raise BrowserError("BROWSER_READ_INVALID", 400)
            fields = tuple(request.args.get("fields", "text,links,controls").split(","))
            response = _success(service.get_context_preview(g.identity, session_id, capture_id, field_paths=fields))
            response[0].headers["Cache-Control"] = "no-store, private"
            return response
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.delete("/api/browser-sessions/<session_id>/context-previews/<capture_id>")
    def browser_discard_context_preview(session_id: str, capture_id: str):
        try:
            return _success(service.discard_context_preview(g.identity, session_id, capture_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/checkpoint")
    def browser_checkpoint(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to checkpoint browser state", 400)
        try:
            return _success(service.checkpoint(identity, session_id, data, key))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/suspend")
    def browser_suspend(session_id: str):
        identity = g.identity
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to suspend a browser session", 400)
        try:
            return _success(service.suspend(identity, session_id, data, key))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/close")
    def browser_close_session(session_id: str):
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to revoke a browser session", 400)
        try:
            return _success(service.close_session(g.identity, session_id, data, key))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/reconcile")
    def browser_reconcile_session(session_id: str):
        identity = _require_write()
        data = _body()
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to reconcile a browser session", 400)
        try:
            return _success(service.reconcile_session(identity, session_id, data, key, actor="user"))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/operation-status")
    def browser_get_operation_status(session_id: str):
        try:
            return _success(service.get_operation_status(g.identity, session_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/operations/<operation_id>")
    def browser_get_operation(session_id: str, operation_id: str):
        try:
            return _success(service.get_operation(g.identity, session_id, operation_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    from .browser_control import register_browser_control_routes
    from .browser_history import register_browser_history_routes
    from .browser_host import register_browser_host_routes

    register_browser_control_routes(app)
    register_browser_history_routes(app)
    register_browser_host_routes(app)
    from .browser_host_reader import register_browser_reader_routes

    register_browser_reader_routes(app)
