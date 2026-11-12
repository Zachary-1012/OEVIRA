"""Single-use local capabilities and opaque, hashed product sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
import uuid

from .store import Store


CAPABILITY_LIFETIME = timedelta(minutes=5)
SESSION_LIFETIME = timedelta(days=7)


class AuthenticationError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_local_capability(store: Store, destination: str | Path) -> None:
    """Rotate the short-lived launch capability without exposing it to logs."""
    path = Path(destination).expanduser()
    if not path.is_absolute():
        path = store.root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = path.parent.resolve()
    if not resolved_parent.is_relative_to(store.root):
        raise ValueError("Bootstrap capability file must be inside the data root")
    path = resolved_parent / path.name
    if path.is_symlink() or path.is_dir():
        raise ValueError("Bootstrap capability destination must be a regular file path")

    capability = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    created_at = utc_now()
    expires_at = (now + CAPABILITY_LIFETIME).isoformat(timespec="seconds").replace("+00:00", "Z")
    fd, temporary_name = tempfile.mkstemp(prefix=".capability-", dir=resolved_parent)
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="") as stream:
            stream.write(capability)
            stream.flush()
            os.fsync(stream.fileno())
        with store.transaction() as connection:
            connection.execute(
                "UPDATE bootstrap_capabilities SET revoked_at = ? WHERE consumed_at IS NULL AND revoked_at IS NULL",
                (created_at,),
            )
            connection.execute(
                "INSERT INTO bootstrap_capabilities(token_hash, created_at, expires_at) VALUES(?, ?, ?)",
                (_digest(capability), created_at, expires_at),
            )
            os.replace(temporary_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _safe_display_name(value: object) -> str:
    if value is None:
        return "本机用户"
    if not isinstance(value, str):
        raise AuthenticationError("INVALID_INPUT", "display_name must be a string", 400)
    name = "".join(character for character in value.strip() if character.isprintable())
    if not name or len(name) > 80:
        raise AuthenticationError("INVALID_INPUT", "display_name must contain 1 to 80 characters", 400)
    return name


def exchange_local_capability(store: Store, capability: object, display_name: object = None) -> tuple[str, dict]:
    if not isinstance(capability, str) or len(capability) < 32 or len(capability) > 256:
        raise AuthenticationError("INVALID_CAPABILITY", "Local setup capability is invalid or expired")
    name = _safe_display_name(display_name)
    now = utc_now()
    token = secrets.token_urlsafe(32)
    user_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    expires_at = (datetime.now(timezone.utc) + SESSION_LIFETIME).isoformat(timespec="seconds").replace("+00:00", "Z")
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT token_hash FROM bootstrap_capabilities WHERE token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (_digest(capability), now),
        ).fetchone()
        if row is None:
            raise AuthenticationError("INVALID_CAPABILITY", "Local setup capability is invalid or expired")
        connection.execute(
            "UPDATE bootstrap_capabilities SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL",
            (now, row["token_hash"]),
        )
        identity = connection.execute(
            "SELECT user_id, workspace_id FROM local_identity WHERE singleton = 1"
        ).fetchone()
        if identity is None:
            user_id = str(uuid.uuid4())
            workspace_id = str(uuid.uuid4())
            workspace_name = f"{name}的工作空间" if name != "本机用户" else "我的工作空间"
            connection.execute(
                "INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, name, now)
            )
            connection.execute(
                "INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, workspace_name, now)
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, user_id, now),
            )
            connection.execute(
                "INSERT INTO local_identity(singleton, user_id, workspace_id, created_at) VALUES(1, ?, ?, ?)",
                (user_id, workspace_id, now),
            )
        else:
            user_id, workspace_id = identity["user_id"], identity["workspace_id"]
            has_account_deletions = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_deletions'"
            ).fetchone()
            if has_account_deletions is not None:
                pending_deletion = connection.execute(
                    "SELECT scheduled_at FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
                    (user_id,),
                ).fetchone()
                if pending_deletion is not None:
                    raise AuthenticationError(
                        "ACCOUNT_DELETION_PENDING",
                        "This local identity has a scheduled account deletion. Cancel it by email before signing in again.",
                        403,
                    )
            membership = connection.execute(
                "SELECT active FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id),
            ).fetchone()
            if membership is None or not membership["active"]:
                raise AuthenticationError("ACCESS_REVOKED", "Local workspace access is unavailable", 403)
        connection.execute(
            "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
            (session_id, _digest(token), user_id, workspace_id, now, expires_at),
        )
        user = connection.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return token, {"user": dict(user), "workspace": dict(workspace)}


def issue_session(
    store: Store,
    user_id: str,
    workspace_id: str,
    *,
    now: str | None = None,
    lifetime: timedelta = SESSION_LIFETIME,
) -> tuple[str, dict]:
    """Create the same opaque, hashed session used by local bootstrap.

    External account flows call this only after their proof has been verified.
    The active membership is rechecked inside the session-creation transaction.
    """
    created_at = now or utc_now()
    with store.transaction() as connection:
        return issue_session_in_transaction(
            connection, user_id, workspace_id, now=created_at, lifetime=lifetime
        )


def issue_session_in_transaction(
    connection,
    user_id: str,
    workspace_id: str,
    *,
    now: str,
    lifetime: timedelta = SESSION_LIFETIME,
) -> tuple[str, dict]:
    """Insert a session inside a caller-owned transaction."""
    if not isinstance(user_id, str) or not user_id or not isinstance(workspace_id, str) or not workspace_id:
        raise AuthenticationError("INVALID_INPUT", "A user and workspace are required", 400)
    if not isinstance(lifetime, timedelta) or lifetime <= timedelta(0):
        raise ValueError("Session lifetime must be a positive timedelta")

    created_at = now
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("Session timestamp must be an ISO-8601 string") from exc
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("Session timestamp must include a time zone")

    token = secrets.token_urlsafe(32)
    expires_at = (created + lifetime).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    session_id = str(uuid.uuid4())
    membership = connection.execute(
        "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id = ? AND active = 1",
        (workspace_id, user_id),
    ).fetchone()
    user = connection.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
    workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if membership is None or user is None or workspace is None:
        raise AuthenticationError("WORKSPACE_NOT_FOUND", "No active access exists for that workspace", 404)
    connection.execute(
        "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
        (session_id, _digest(token), user_id, workspace_id, created_at, expires_at),
    )
    return token, {"user": dict(user), "workspace": dict(workspace)}


def list_sessions(store: Store, user_id: str) -> list[dict]:
    """List a user's sessions without returning credentials or token hashes."""
    if not isinstance(user_id, str) or not user_id:
        raise AuthenticationError("UNAUTHENTICATED", "A valid user is required")
    now = utc_now()
    rows = store.rows(
        """SELECT s.id AS session_id, s.workspace_id, w.name AS workspace_name,
                  s.created_at, s.expires_at
           FROM sessions s JOIN workspaces w ON w.id = s.workspace_id
           JOIN memberships m ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
           WHERE s.user_id = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND m.active = 1
           ORDER BY s.created_at DESC, s.id""",
        (user_id, now),
    )
    return [dict(row) for row in rows]


def revoke_session(store: Store, user_id: str, session_id: str, *, now: str | None = None) -> bool:
    """Revoke one session only when it belongs to the authenticated user."""
    if not isinstance(user_id, str) or not user_id or not isinstance(session_id, str) or not session_id:
        raise AuthenticationError("INVALID_INPUT", "A session identifier is required", 400)
    with store.transaction() as connection:
        cursor = connection.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (now or utc_now(), session_id, user_id),
        )
    return cursor.rowcount == 1


def revoke_all_sessions(store: Store, user_id: str, *, now: str | None = None) -> int:
    """Revoke every active workspace session for a user in one transaction."""
    if not isinstance(user_id, str) or not user_id:
        raise AuthenticationError("INVALID_INPUT", "A user identifier is required", 400)
    with store.transaction() as connection:
        cursor = connection.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now or utc_now(), user_id),
        )
    return cursor.rowcount


def bearer_or_cookie_token(request, cookie_name: str) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        return token or None
    return request.cookies.get(cookie_name)


def authenticate(store: Store, token: str | None) -> dict | None:
    if not token:
        return None
    row = store.row(
        """SELECT s.id AS session_id, s.user_id, s.workspace_id, u.name AS user_name,
                  w.name AS workspace_name, m.role
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           JOIN workspaces w ON w.id = s.workspace_id
           JOIN memberships m ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
           WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND m.active = 1""",
        (_digest(token), utc_now()),
    )
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "user": {"id": row["user_id"], "name": row["user_name"]},
        "workspace": {"id": row["workspace_id"], "name": row["workspace_name"]},
        "role": row["role"],
    }
