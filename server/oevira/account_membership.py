"""Account access to the existing workspace membership model.

Invitations are short-lived email proofs. Accepted users become rows in the
existing ``memberships`` table; no second user/session authority is created.
"""
from __future__ import annotations

from datetime import timedelta
import secrets
import sqlite3
import uuid
from typing import Any

from .account import AccountError, AccountService, _email
from .auth import _digest, issue_session_in_transaction
from .collaboration import prepare_account_deletion


INVITATION_LIFETIME = timedelta(days=7)
INVITATION_RATE_WINDOW = timedelta(minutes=10)
INVITATION_RATE_LIMIT = 5
_MEMBERSHIP_ROLES = frozenset({"owner", "editor", "viewer"})
_ASSIGNABLE_ROLES = frozenset({"editor", "viewer"})


class AccountWorkspaceMemberships:
    """Membership lifecycle operations backed by the account Store."""

    def __init__(self, account: AccountService):
        self.account = account
        self.store = account.store

    def _stamp(self, value=None) -> str:
        return self.account._stamp(value)

    @staticmethod
    def _not_found() -> AccountError:
        return AccountError("WORKSPACE_NOT_FOUND", "The workspace or member is unavailable", 404)

    def _membership(self, connection, workspace_id: str, user_id: str):
        row = connection.execute(
            """SELECT m.role, m.active, m.created_at, w.name AS workspace_name
               FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.workspace_id = ? AND m.user_id = ?""",
            (workspace_id, user_id),
        ).fetchone()
        if row is None or row["active"] != 1:
            raise self._not_found()
        return row

    def _require_owner(self, connection, identity: dict, workspace_id: str):
        member = self._membership(connection, workspace_id, identity["user_id"])
        if member["role"] != "owner":
            raise AccountError("WORKSPACE_OWNER_REQUIRED", "Only an active workspace owner can manage membership", 403)
        return member

    def _require_local_choice_resolved(self, connection, active: dict) -> None:
        pending = connection.execute(
            """SELECT 1 FROM account_local_merge_choices
               WHERE account_user_id = ? AND account_session_id = ?
                 AND consumed_at IS NULL AND expires_at > ? LIMIT 1""",
            (active["user_id"], active["session_id"], self._stamp()),
        ).fetchone()
        if pending is not None:
            raise AccountError(
                "LOCAL_DATA_CHOICE_REQUIRED",
                "Choose whether to keep or merge the current local workspace before switching account workspaces",
                409,
            )

    def _event(
        self,
        connection,
        workspace_id: str,
        actor_user_id: str,
        action: str,
        *,
        subject_user_id: str | None = None,
        invitation_id: str | None = None,
        previous_role: str | None = None,
        new_role: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO account_membership_events(
                   event_id, workspace_id, subject_user_id, actor_user_id,
                   invitation_id, action, previous_role, new_role, occurred_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), workspace_id, subject_user_id, actor_user_id,
                invitation_id, action, previous_role, new_role,
                occurred_at or self._stamp(),
            ),
        )

    def workspaces(self, identity: dict) -> dict[str, Any]:
        active = self.account._active_identity(identity)
        rows = self.store.rows(
            """SELECT w.id, w.name, m.role, m.created_at
               FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.user_id = ? AND m.active = 1
               ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, m.created_at, w.id""",
            (active["user_id"],),
        )
        return {
            "workspaces": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "role": row["role"],
                    "joined_at": row["created_at"],
                    "current": row["id"] == active["workspace_id"],
                }
                for row in rows
            ]
        }

    def switch_workspace(self, identity: dict, workspace_id: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(workspace_id) is not str or not 1 <= len(workspace_id) <= 80:
            raise AccountError("INVALID_INPUT", "Choose a workspace from your active memberships", 400)
        now = self.account._stamp()
        with self.store.transaction() as connection:
            self._require_local_choice_resolved(connection, active)
            target = connection.execute(
                """SELECT w.id, w.name, m.role FROM memberships m
                   JOIN workspaces w ON w.id = m.workspace_id
                   WHERE m.workspace_id = ? AND m.user_id = ? AND m.active = 1""",
                (workspace_id, active["user_id"]),
            ).fetchone()
            if target is None:
                raise self._not_found()
            if workspace_id == active["workspace_id"]:
                return {
                    "state": "workspace_unchanged",
                    "workspace": {"id": target["id"], "name": target["name"]},
                    "membership": {"workspace_id": target["id"], "role": target["role"]},
                }
            revoked = connection.execute(
                """UPDATE sessions SET revoked_at = ? WHERE id = ? AND user_id = ?
                   AND workspace_id = ? AND revoked_at IS NULL AND expires_at > ?""",
                (now, active["session_id"], active["user_id"], active["workspace_id"], now),
            ).rowcount
            if revoked != 1:
                raise AccountError("ACCESS_REVOKED", "The current account session expired; sign in again", 401)
            token, session = issue_session_in_transaction(
                connection, active["user_id"], workspace_id, now=now
            )
        return {
            "state": "workspace_switched",
            "token": token,
            **session,
            "workspace": {"id": target["id"], "name": target["name"]},
            "membership": {"workspace_id": target["id"], "role": target["role"]},
        }

    def members(self, identity: dict, workspace_id: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity)
        if type(workspace_id) is not str or not 1 <= len(workspace_id) <= 80:
            raise AccountError("INVALID_INPUT", "A workspace id is required", 400)
        with self.store.transaction(immediate=False) as connection:
            caller = self._membership(connection, workspace_id, active["user_id"])
            workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if workspace is None:
                raise self._not_found()
            rows = connection.execute(
                """SELECT u.id AS user_id, u.name, m.role, m.created_at AS joined_at
                   FROM memberships m JOIN users u ON u.id = m.user_id
                   WHERE m.workspace_id = ? AND m.active = 1
                   ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, u.name COLLATE NOCASE, u.id""",
                (workspace_id,),
            ).fetchall()
        return {
            "workspace": {"id": workspace["id"], "name": workspace["name"]},
            "current_user_role": caller["role"],
            "members": [
                {
                    "user_id": row["user_id"],
                    "name": row["name"],
                    "role": row["role"],
                    "joined_at": row["joined_at"],
                    "current": row["user_id"] == active["user_id"],
                }
                for row in rows
            ],
        }

    def _invitation_data(self, row, now) -> dict[str, Any]:
        status = row["status"]
        if status in {"pending", "delivery_unknown"} and row["expires_at"] <= self._stamp(now):
            status = "expired"
        return {
            "id": row["invitation_id"],
            "workspace_id": row["workspace_id"],
            "email": row["email_display"],
            "role": row["invited_role"],
            "status": status,
            "delivery_status": row["delivery_status"],
            "invited_by": row["invited_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "accepted_at": row["accepted_at"],
            "revoked_at": row["revoked_at"],
        }

    def invitations(self, identity: dict, workspace_id: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity)
        if type(workspace_id) is not str or not 1 <= len(workspace_id) <= 80:
            raise AccountError("INVALID_INPUT", "A workspace id is required", 400)
        now = self.account._now()
        with self.store.transaction(immediate=False) as connection:
            caller = self._require_owner(connection, active, workspace_id)
            workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            rows = connection.execute(
                "SELECT * FROM account_workspace_invitations WHERE workspace_id = ? ORDER BY created_at DESC, invitation_id",
                (workspace_id,),
            ).fetchall()
        return {
            "workspace": {"id": workspace["id"], "name": workspace["name"]},
            "current_user_role": caller["role"],
            "invitations": [self._invitation_data(row, now) for row in rows],
        }

    def preview_invitation(self, token: Any) -> dict[str, Any]:
        if type(token) is not str or not 32 <= len(token) <= 256:
            raise AccountError("INVITATION_INVALID", "The workspace invitation is invalid or expired", 401)
        row = self.store.row(
            """SELECT i.*, w.name AS workspace_name FROM account_workspace_invitations i
               JOIN workspaces w ON w.id = i.workspace_id WHERE i.token_hash = ?""",
            (_digest(token),),
        )
        if row is None:
            raise AccountError("INVITATION_INVALID", "The workspace invitation is invalid or expired", 401)
        status = row["status"]
        if status != "accepted" and row["expires_at"] <= self._stamp():
            status = "expired"
        if status not in {"pending", "delivery_unknown", "accepted", "expired"}:
            raise AccountError("INVITATION_INVALID", "The workspace invitation is unavailable", 401)
        if row["delivery_status"] not in {"sent", "unknown"}:
            raise AccountError("INVITATION_INVALID", "The workspace invitation is unavailable", 401)
        local, _, domain = row["email_display"].partition("@")
        visible_local = f"{local[:1]}{'*' * min(6, max(3, len(local) - 1))}" if local else "***"
        return {
            "workspace": {"id": row["workspace_id"], "name": row["workspace_name"]},
            "invited_email_hint": f"{visible_local}@{domain}" if domain else "***",
            "role": row["invited_role"],
            "status": status,
            "expires_at": row["expires_at"],
            "delivery_semantics": "smtp_accepted_not_proof_of_inbox_delivery",
        }

    def _rate_limit(self, connection, workspace_id: str, actor_user_id: str, now) -> None:
        cutoff = self._stamp(now - INVITATION_RATE_WINDOW)
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM account_workspace_invitations WHERE workspace_id = ? AND invited_by = ? AND created_at >= ?",
            (workspace_id, actor_user_id, cutoff),
        ).fetchone()["count"]
        if count >= INVITATION_RATE_LIMIT:
            raise AccountError("INVITATION_RATE_LIMITED", "Too many workspace invitations were requested; try again later", 429)

    def _new_invitation_tx(
        self,
        connection,
        *,
        workspace_id: str,
        email_normalized: str,
        email_display: str,
        role: str,
        invited_by: str,
        now,
    ) -> tuple[str, str, str]:
        invitation_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now_text = self._stamp(now)
        expires_at = self._stamp(now + INVITATION_LIFETIME)
        connection.execute(
            """INSERT INTO account_workspace_invitations(
                   invitation_id, workspace_id, email_normalized, email_display,
                   invited_role, invited_by, token_hash, status, delivery_status,
                   created_at, expires_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)""",
            (
                invitation_id, workspace_id, email_normalized, email_display,
                role, invited_by, _digest(token), now_text, expires_at,
            ),
        )
        self._event(
            connection, workspace_id, invited_by, "invited",
            invitation_id=invitation_id, new_role=role, occurred_at=now_text,
        )
        return invitation_id, token, expires_at

    def _deliver(self, invitation_id: str, workspace_id: str, email: str, token: str, expires_at: str) -> dict[str, Any]:
        mailer = self.account.mailer
        now = self.account._stamp()
        workspace = self.store.row("SELECT name FROM workspaces WHERE id = ?", (workspace_id,))
        if workspace is None:
            raise self._not_found()
        try:
            accepted = mailer.send_invitation(email, token, expires_at, workspace["name"])
        except Exception as exc:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_workspace_invitations SET status = 'delivery_unknown', delivery_status = 'unknown' WHERE invitation_id = ? AND status = 'pending'",
                    (invitation_id,),
                )
            raise AccountError(
                "INVITATION_DELIVERY_UNKNOWN",
                "The invitation email result is unknown. Check its state before rotating or resending it.",
                503,
                {"invitation_id": invitation_id},
            ) from exc
        if accepted is not True:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_workspace_invitations SET status = 'delivery_failed', delivery_status = 'failed' WHERE invitation_id = ? AND status = 'pending'",
                    (invitation_id,),
                )
            raise AccountError(
                "INVITATION_DELIVERY_FAILED",
                "The email transport did not accept the invitation. The invitation remains unavailable.",
                503,
                {"invitation_id": invitation_id},
            )
        with self.store.transaction() as connection:
            updated = connection.execute(
                "UPDATE account_workspace_invitations SET delivery_status = 'sent' WHERE invitation_id = ? AND status = 'pending'",
                (invitation_id,),
            )
            if updated.rowcount != 1:
                raise AccountError("INVITATION_CHANGED_DURING_DELIVERY", "The invitation changed while its email was being sent", 409)
            row = connection.execute(
                "SELECT * FROM account_workspace_invitations WHERE invitation_id = ?", (invitation_id,)
            ).fetchone()
        return {
            "state": "invitation_sent",
            "invitation": self._invitation_data(row, self.account._now()),
            "workspace_name": workspace["name"],
            "delivery_semantics": "smtp_accepted_not_proof_of_inbox_delivery",
            "transport_accepted_at": now,
        }

    def invite(self, identity: dict, workspace_id: Any, email: Any, role: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(workspace_id) is not str or not 1 <= len(workspace_id) <= 80:
            raise AccountError("INVALID_INPUT", "A workspace id is required", 400)
        if type(role) is not str or role not in _ASSIGNABLE_ROLES:
            raise AccountError("INVALID_INPUT", "Choose editor or viewer access; owner status is transferred explicitly", 400)
        if not self.account._invitation_mailer_ready():
            raise AccountError("INVITATION_DELIVERY_UNAVAILABLE", "Workspace invitations are unavailable without a configured invitation mailer", 503)
        normalized, display = _email(email)
        now = self.account._now()
        with self.store.transaction() as connection:
            self._require_owner(connection, active, workspace_id)
            existing_account = connection.execute(
                "SELECT user_id FROM account_emails WHERE email_normalized = ? AND verified_at IS NOT NULL",
                (normalized,),
            ).fetchone()
            if existing_account is not None:
                membership = connection.execute(
                    "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
                    (workspace_id, existing_account["user_id"]),
                ).fetchone()
                if membership is not None and membership["active"] == 1:
                    raise AccountError("WORKSPACE_MEMBER_ALREADY_ACTIVE", "This account is already an active workspace member", 409)
            pending = connection.execute(
                """SELECT invitation_id, status, expires_at FROM account_workspace_invitations
                   WHERE workspace_id = ? AND email_normalized = ? AND status IN ('pending', 'delivery_unknown')
                   ORDER BY created_at DESC LIMIT 1""",
                (workspace_id, normalized),
            ).fetchone()
            if pending is not None and pending["expires_at"] > self._stamp(now):
                raise AccountError(
                    "INVITATION_ALREADY_PENDING",
                    "An invitation for this email is already pending; inspect it or rotate it explicitly.",
                    409,
                    {"invitation_id": pending["invitation_id"], "status": pending["status"]},
                )
            if pending is not None:
                connection.execute(
                    "UPDATE account_workspace_invitations SET status = 'expired' WHERE invitation_id = ? AND status IN ('pending', 'delivery_unknown')",
                    (pending["invitation_id"],),
                )
            self._rate_limit(connection, workspace_id, active["user_id"], now)
            invitation_id, token, expires_at = self._new_invitation_tx(
                connection,
                workspace_id=workspace_id,
                email_normalized=normalized,
                email_display=display,
                role=role,
                invited_by=active["user_id"],
                now=now,
            )
        return self._deliver(invitation_id, workspace_id, display, token, expires_at)

    def resend_invitation(self, identity: dict, workspace_id: Any, invitation_id: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(workspace_id) is not str or type(invitation_id) is not str:
            raise AccountError("INVALID_INPUT", "A workspace and invitation id are required", 400)
        if not self.account._invitation_mailer_ready():
            raise AccountError("INVITATION_DELIVERY_UNAVAILABLE", "Workspace invitations are unavailable without a configured invitation mailer", 503)
        now = self.account._now()
        with self.store.transaction() as connection:
            self._require_owner(connection, active, workspace_id)
            previous = connection.execute(
                "SELECT * FROM account_workspace_invitations WHERE invitation_id = ? AND workspace_id = ?",
                (invitation_id, workspace_id),
            ).fetchone()
            if previous is None:
                raise self._not_found()
            if previous["status"] not in {"pending", "delivery_failed", "delivery_unknown"}:
                raise AccountError("INVITATION_NOT_RESENDABLE", "Only a pending or unconfirmed invitation can be rotated", 409)
            if previous["expires_at"] <= self._stamp(now):
                connection.execute(
                    "UPDATE account_workspace_invitations SET status = 'expired' WHERE invitation_id = ?",
                    (invitation_id,),
                )
                raise AccountError("INVITATION_EXPIRED", "This invitation expired; create a new invitation", 410)
            self._rate_limit(connection, workspace_id, active["user_id"], now)
            connection.execute(
                "UPDATE account_workspace_invitations SET status = 'revoked', delivery_status = 'revoked', revoked_at = ?, revoked_by = ? WHERE invitation_id = ? AND workspace_id = ?",
                (self._stamp(now), active["user_id"], invitation_id, workspace_id),
            )
            new_id, token, expires_at = self._new_invitation_tx(
                connection,
                workspace_id=workspace_id,
                email_normalized=previous["email_normalized"],
                email_display=previous["email_display"],
                role=previous["invited_role"],
                invited_by=active["user_id"],
                now=now,
            )
            self._event(
                connection, workspace_id, active["user_id"], "invitation_resent",
                subject_user_id=None, invitation_id=new_id,
                previous_role=previous["invited_role"], new_role=previous["invited_role"],
                occurred_at=self._stamp(now),
            )
        return self._deliver(new_id, workspace_id, previous["email_display"], token, expires_at)

    def revoke_invitation(self, identity: dict, workspace_id: Any, invitation_id: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(workspace_id) is not str or type(invitation_id) is not str:
            raise AccountError("INVALID_INPUT", "A workspace and invitation id are required", 400)
        now = self.account._stamp()
        with self.store.transaction() as connection:
            self._require_owner(connection, active, workspace_id)
            row = connection.execute(
                "SELECT * FROM account_workspace_invitations WHERE invitation_id = ? AND workspace_id = ?",
                (invitation_id, workspace_id),
            ).fetchone()
            if row is None:
                raise self._not_found()
            if row["status"] == "accepted":
                raise AccountError("INVITATION_ALREADY_ACCEPTED", "An accepted invitation cannot be revoked; remove the active member explicitly", 409)
            if row["status"] == "revoked":
                return {"state": "invitation_revoked", "invitation_id": invitation_id}
            connection.execute(
                "UPDATE account_workspace_invitations SET status = 'revoked', delivery_status = 'revoked', revoked_at = ?, revoked_by = ? WHERE invitation_id = ?",
                (now, active["user_id"], invitation_id),
            )
            self._event(
                connection, workspace_id, active["user_id"], "invitation_revoked",
                invitation_id=invitation_id, previous_role=row["invited_role"],
                occurred_at=now,
            )
        return {"state": "invitation_revoked", "invitation_id": invitation_id}

    def accept_invitation(self, identity: dict, token: Any) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(token) is not str or not 32 <= len(token) <= 256:
            raise AccountError("INVITATION_INVALID", "The workspace invitation is invalid or expired", 401)
        email = self.store.row(
            "SELECT email_normalized, verified_at FROM account_emails WHERE user_id = ?",
            (active["user_id"],),
        )
        if email is None or email["verified_at"] is None:
            raise AccountError("VERIFIED_EMAIL_REQUIRED", "Verify an account email before accepting a workspace invitation", 403)
        now = self.account._now()
        with self.store.transaction() as connection:
            self._require_local_choice_resolved(connection, active)
            row = connection.execute(
                "SELECT * FROM account_workspace_invitations WHERE token_hash = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                raise AccountError("INVITATION_INVALID", "The workspace invitation is invalid or expired", 401)
            if row["status"] == "accepted" and row["accepted_user_id"] == active["user_id"]:
                member = self._membership(connection, row["workspace_id"], active["user_id"])
                workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (row["workspace_id"],)).fetchone()
                session_token, session_identity = issue_session_in_transaction(
                    connection, active["user_id"], row["workspace_id"], now=self._stamp(now)
                )
                return {
                    "state": "workspace_invitation_already_accepted",
                    "token": session_token,
                    **session_identity,
                    "membership": {"workspace_id": row["workspace_id"], "role": member["role"]},
                    "workspace": {"id": workspace["id"], "name": workspace["name"]},
                }
            if row["status"] not in {"pending", "delivery_unknown"} or row["delivery_status"] not in {"sent", "unknown"}:
                raise AccountError("INVITATION_INVALID", "The workspace invitation is unavailable", 401)
            if row["expires_at"] <= self._stamp(now):
                raise AccountError("INVITATION_EXPIRED", "The workspace invitation has expired", 410)
            if not secrets.compare_digest(row["email_normalized"], email["email_normalized"]):
                raise AccountError("INVITATION_EMAIL_MISMATCH", "Sign in with the verified email address this invitation was sent to", 403)
            workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (row["workspace_id"],)).fetchone()
            if workspace is None:
                raise AccountError("INVITATION_INVALID", "The workspace invitation is unavailable", 410)
            current = connection.execute(
                "SELECT role, active FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (row["workspace_id"], active["user_id"]),
            ).fetchone()
            if current is not None and current["active"] == 1 and current["role"] != row["invited_role"]:
                raise AccountError("WORKSPACE_MEMBER_ROLE_CONFLICT", "This account already has a different active workspace role; ask an owner to change it explicitly", 409)
            if current is None or current["active"] != 1:
                connection.execute(
                    """INSERT INTO memberships(workspace_id, user_id, role, active, created_at)
                       VALUES(?, ?, ?, 1, ?)
                       ON CONFLICT(workspace_id, user_id) DO UPDATE SET role = excluded.role,
                           active = 1, created_at = excluded.created_at""",
                    (row["workspace_id"], active["user_id"], row["invited_role"], self._stamp(now)),
                )
            connection.execute(
                "UPDATE account_workspace_invitations SET status = 'accepted', accepted_at = ?, accepted_user_id = ? WHERE invitation_id = ? AND status IN ('pending', 'delivery_unknown')",
                (self._stamp(now), active["user_id"], row["invitation_id"]),
            )
            self._event(
                connection, row["workspace_id"], active["user_id"], "invitation_accepted",
                subject_user_id=active["user_id"], invitation_id=row["invitation_id"],
                new_role=row["invited_role"], occurred_at=self._stamp(now),
            )
            session_token, session_identity = issue_session_in_transaction(
                connection, active["user_id"], row["workspace_id"], now=self._stamp(now)
            )
        return {
            "state": "workspace_invitation_accepted",
            "token": session_token,
            **session_identity,
            "workspace": {"id": workspace["id"], "name": workspace["name"]},
            "membership": {"workspace_id": row["workspace_id"], "role": row["invited_role"]},
        }

    def set_member_role(
        self,
        identity: dict,
        workspace_id: Any,
        target_user_id: Any,
        role: Any,
        expected_role: Any,
    ) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if not all(type(value) is str and value for value in (workspace_id, target_user_id)):
            raise AccountError("INVALID_INPUT", "A workspace and member selection are required", 400)
        if type(role) is not str or role not in _ASSIGNABLE_ROLES or type(expected_role) is not str or expected_role not in _MEMBERSHIP_ROLES:
            raise AccountError("INVALID_INPUT", "Choose editor or viewer access and confirm the current role", 400)
        now = self._stamp()
        with self.store.transaction() as connection:
            self._require_owner(connection, active, workspace_id)
            target = self._membership(connection, workspace_id, target_user_id)
            if target["role"] != expected_role:
                raise AccountError("MEMBERSHIP_VERSION_CONFLICT", "The member role changed; refresh the member list before applying this change", 409)
            if role == expected_role:
                return {"state": "membership_unchanged", "workspace_id": workspace_id, "user_id": target_user_id, "role": role}
            if expected_role == "owner" and role != "owner":
                other_owner = connection.execute(
                    "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' LIMIT 1",
                    (workspace_id, target_user_id),
                ).fetchone()
                if other_owner is None:
                    raise AccountError("LAST_WORKSPACE_OWNER", "Transfer ownership to another active member before changing the last owner role", 409)
            cursor = connection.execute(
                "UPDATE memberships SET role = ? WHERE workspace_id = ? AND user_id = ? AND active = 1 AND role = ?",
                (role, workspace_id, target_user_id, expected_role),
            )
            if cursor.rowcount != 1:
                raise AccountError("MEMBERSHIP_VERSION_CONFLICT", "The member role changed; refresh the member list before applying this change", 409)
            self._event(
                connection, workspace_id, active["user_id"], "role_changed",
                subject_user_id=target_user_id, previous_role=expected_role,
                new_role=role, occurred_at=now,
            )
            if role == "viewer":
                self._cancel_member_writes_tx(connection, workspace_id, target_user_id, now, active["user_id"])
        if role == "viewer":
            browser_cleanup = self.account.close_workspace_member_browsers(
                workspace_id,
                target_user_id,
                "WORKSPACE_ROLE_CHANGED_TO_VIEWER",
                actor_user_id=active["user_id"],
            )
        else:
            browser_cleanup = {"closed": 0, "pending": 0}
        return {
            "state": "membership_role_changed",
            "workspace_id": workspace_id,
            "user_id": target_user_id,
            "previous_role": expected_role,
            "role": role,
            "browser_sessions_closed": browser_cleanup["closed"],
            "browser_session_close_pending": browser_cleanup["pending"],
        }

    def transfer_workspace_owner(
        self,
        identity: dict,
        workspace_id: Any,
        new_owner_user_id: Any,
        expected_owner_user_id: Any,
    ) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if not all(type(value) is str and value for value in (workspace_id, new_owner_user_id, expected_owner_user_id)):
            raise AccountError("INVALID_INPUT", "Choose a workspace, the current owner and a new owner", 400)
        if expected_owner_user_id != active["user_id"] or new_owner_user_id == active["user_id"]:
            raise AccountError("OWNER_TRANSFER_INVALID", "Confirm the current account and choose another active member", 400)
        now = self._stamp()
        with self.store.transaction() as connection:
            caller = self._require_owner(connection, active, workspace_id)
            if active["user_id"] != expected_owner_user_id:
                raise AccountError("OWNER_TRANSFER_INVALID", "The current owner changed; refresh the workspace members", 409)
            target = self._membership(connection, workspace_id, new_owner_user_id)
            if target["role"] == "owner":
                changed_source = connection.execute(
                    "UPDATE memberships SET role = 'editor' WHERE workspace_id = ? AND user_id = ? AND active = 1 AND role = 'owner'",
                    (workspace_id, active["user_id"]),
                ).rowcount
                if changed_source != 1:
                    raise AccountError("MEMBERSHIP_VERSION_CONFLICT", "Workspace ownership changed during transfer; refresh and retry", 409)
                self._event(
                    connection, workspace_id, active["user_id"], "owner_transferred",
                    subject_user_id=new_owner_user_id, previous_role="owner",
                    new_role="owner", occurred_at=now,
                )
                self._event(
                    connection, workspace_id, active["user_id"], "role_changed",
                    subject_user_id=active["user_id"], previous_role="owner",
                    new_role="editor", occurred_at=now,
                )
                return {
                    "state": "workspace_owner_transferred",
                    "workspace_id": workspace_id,
                    "previous_owner_user_id": active["user_id"],
                    "owner_user_id": new_owner_user_id,
                }
            if caller["role"] != "owner":
                raise AccountError("WORKSPACE_OWNER_REQUIRED", "Only an active owner can transfer workspace ownership", 403)
            changed_target = connection.execute(
                "UPDATE memberships SET role = 'owner' WHERE workspace_id = ? AND user_id = ? AND active = 1 AND role = ?",
                (workspace_id, new_owner_user_id, target["role"]),
            ).rowcount
            changed_source = connection.execute(
                "UPDATE memberships SET role = 'editor' WHERE workspace_id = ? AND user_id = ? AND active = 1 AND role = 'owner'",
                (workspace_id, active["user_id"]),
            ).rowcount
            if changed_target != 1 or changed_source != 1:
                raise AccountError("MEMBERSHIP_VERSION_CONFLICT", "Workspace ownership changed during transfer; refresh and retry", 409)
            self._event(
                connection, workspace_id, active["user_id"], "owner_transferred",
                subject_user_id=new_owner_user_id, previous_role=target["role"],
                new_role="owner", occurred_at=now,
            )
            self._event(
                connection, workspace_id, active["user_id"], "role_changed",
                subject_user_id=active["user_id"], previous_role="owner",
                new_role="editor", occurred_at=now,
            )
        return {"state": "workspace_owner_transferred", "workspace_id": workspace_id, "previous_owner_user_id": active["user_id"], "owner_user_id": new_owner_user_id}

    def remove_member(self, identity: dict, workspace_id: Any, target_user_id: Any, *, leaving: bool = False) -> dict[str, Any]:
        active = self.account._active_identity(identity, recent=True)
        if type(workspace_id) is not str or not workspace_id:
            raise AccountError("INVALID_INPUT", "A workspace is required", 400)
        if leaving:
            target_user_id = active["user_id"]
        elif type(target_user_id) is not str or not target_user_id:
            raise AccountError("INVALID_INPUT", "A workspace member is required", 400)
        if not leaving and active["user_id"] == target_user_id:
            raise AccountError("USE_WORKSPACE_LEAVE", "Use the workspace leave action for your own membership", 400)
        now = self._stamp()
        new_personal_workspace = None
        with self.store.transaction() as connection:
            if leaving:
                self._require_local_choice_resolved(connection, active)
            actor = self._membership(connection, workspace_id, active["user_id"])
            if not leaving and actor["role"] != "owner":
                raise AccountError("WORKSPACE_OWNER_REQUIRED", "Only an active owner can remove a workspace member", 403)
            target = self._membership(connection, workspace_id, target_user_id)
            if target["role"] == "owner":
                remaining_owner = connection.execute(
                    "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' LIMIT 1",
                    (workspace_id, target_user_id),
                ).fetchone()
                if remaining_owner is None:
                    raise AccountError("LAST_WORKSPACE_OWNER", "Transfer ownership to another active member before removing or leaving", 409)
            successor = connection.execute(
                "SELECT user_id FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' ORDER BY created_at, user_id LIMIT 1",
                (workspace_id, target_user_id),
            ).fetchone()
            if successor is not None and self.account._table_exists(connection, "client_commissions"):
                from .api import ApiError

                try:
                    prepare_account_deletion(connection, workspace_id, target_user_id, successor["user_id"])
                except ApiError as exc:
                    raise AccountError(exc.code, str(exc), exc.status, getattr(exc, "details", {})) from exc
            if self.account._table_exists(connection, "account_workspace_invitations"):
                pending_invites = connection.execute(
                    "SELECT invitation_id, invited_role FROM account_workspace_invitations WHERE workspace_id = ? AND invited_by = ? AND status IN ('pending', 'delivery_failed', 'delivery_unknown')",
                    (workspace_id, target_user_id),
                ).fetchall()
                for invitation in pending_invites:
                    connection.execute(
                        "UPDATE account_workspace_invitations SET status = 'revoked', delivery_status = 'revoked', revoked_at = ?, revoked_by = ? WHERE invitation_id = ?",
                        (now, active["user_id"], invitation["invitation_id"]),
                    )
                    self._event(
                        connection, workspace_id, active["user_id"], "invitation_revoked",
                        subject_user_id=None, invitation_id=invitation["invitation_id"],
                        previous_role=invitation["invited_role"], occurred_at=now,
                    )
            connection.execute(
                "UPDATE memberships SET active = 0 WHERE workspace_id = ? AND user_id = ? AND active = 1",
                (workspace_id, target_user_id),
            )
            remaining_memberships = connection.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND active = 1 LIMIT 1",
                (target_user_id,),
            ).fetchone()
            if remaining_memberships is None:
                new_personal_workspace = self.account._ensure_personal_workspace(
                    connection, target_user_id, now
                )
                connection.execute(
                    "UPDATE local_identity SET workspace_id = ? WHERE singleton = 1 AND user_id = ? AND workspace_id = ?",
                    (new_personal_workspace["id"], target_user_id, workspace_id),
                )
            else:
                next_local_workspace = connection.execute(
                    "SELECT workspace_id FROM memberships WHERE user_id = ? AND active = 1 ORDER BY created_at, workspace_id LIMIT 1",
                    (target_user_id,),
                ).fetchone()
                if next_local_workspace is not None:
                    connection.execute(
                        "UPDATE local_identity SET workspace_id = ? WHERE singleton = 1 AND user_id = ? AND workspace_id = ?",
                        (next_local_workspace["workspace_id"], target_user_id, workspace_id),
                    )
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE workspace_id = ? AND user_id = ? AND revoked_at IS NULL",
                (now, workspace_id, target_user_id),
            )
            action = "workspace_left" if leaving else "member_removed"
            self._event(
                connection, workspace_id, active["user_id"], action,
                subject_user_id=target_user_id, previous_role=target["role"],
                occurred_at=now,
            )
            self.account._revoke_workspace_connections(connection, workspace_id, target_user_id, now)
            self._cancel_member_writes_tx(connection, workspace_id, target_user_id, now, active["user_id"])
        browser_cleanup = self.account.close_workspace_member_browsers(
            workspace_id,
            target_user_id,
            "WORKSPACE_MEMBERSHIP_REVOKED",
            actor_user_id=active["user_id"],
        )
        if leaving and workspace_id == active["workspace_id"]:
            if new_personal_workspace is not None:
                with self.store.transaction() as connection:
                    session_token, session_identity = issue_session_in_transaction(
                        connection, target_user_id, new_personal_workspace["id"], now=now
                    )
                return {
                    "state": "workspace_left_new_personal_workspace_created",
                    "token": session_token,
                    **session_identity,
                    "left_workspace_id": workspace_id,
                    "new_workspace": new_personal_workspace,
                    "browser_sessions_closed": browser_cleanup["closed"],
                    "browser_session_close_pending": browser_cleanup["pending"],
                }
            remaining = self.store.rows(
                "SELECT id, name, role FROM memberships JOIN workspaces ON workspaces.id = memberships.workspace_id WHERE memberships.user_id = ? AND memberships.active = 1 ORDER BY memberships.created_at, memberships.workspace_id",
                (target_user_id,),
            )
            with self.store.transaction() as connection:
                session_token, session_identity = issue_session_in_transaction(
                    connection, target_user_id, remaining[0]["id"], now=now
                )
            return {
                "state": "workspace_left",
                "token": session_token,
                **session_identity,
                "left_workspace_id": workspace_id,
                "workspace": {"id": remaining[0]["id"], "name": remaining[0]["name"]},
                "browser_sessions_closed": browser_cleanup["closed"],
                "browser_session_close_pending": browser_cleanup["pending"],
            }
        result = {
            "state": action,
            "workspace_id": workspace_id,
            "user_id": target_user_id,
            "role": target["role"],
            "browser_sessions_closed": browser_cleanup["closed"],
            "browser_session_close_pending": browser_cleanup["pending"],
        }
        if new_personal_workspace is not None:
            result["personal_workspace_created"] = new_personal_workspace
        return result

    def _cancel_member_writes(self, workspace_id: str, user_id: str, now: str, actor_user_id: str) -> None:
        with self.store.transaction() as connection:
            self._cancel_member_writes_tx(connection, workspace_id, user_id, now, actor_user_id)

    def _cancel_member_writes_tx(self, connection, workspace_id: str, user_id: str,
                                 now: str, actor_user_id: str) -> None:
        if self.account._table_exists(connection, "jobs"):
            if self.account._table_exists(connection, "media_renders"):
                connection.execute(
                    """UPDATE media_renders SET status = 'cancelled', error_code = 'MEMBERSHIP_REVOKED',
                           error_message = 'Workspace access was revoked.', updated_at = ?
                       WHERE status IN ('queued', 'running', 'writing')
                         AND job_id IN (SELECT id FROM jobs WHERE workspace_id = ? AND user_id = ? AND status IN ('queued', 'running', 'cancel_requested'))""",
                    (now, workspace_id, user_id),
                )
            connection.execute(
                """UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                       lease_generation = lease_generation + 1,
                       error_code = 'MEMBERSHIP_REVOKED',
                       error_message = 'Workspace access changed before this task completed.',
                       updated_at = ?
                   WHERE workspace_id = ? AND user_id = ? AND status IN ('queued', 'running', 'cancel_requested')""",
                (now, workspace_id, user_id),
            )
            if self.account._table_exists(connection, "delivery_batches"):
                connection.execute(
                    "UPDATE delivery_batches SET status='partial',updated_at=? WHERE workspace_id=? AND status='preparing' "
                    "AND job_id IN (SELECT id FROM jobs WHERE workspace_id=? AND user_id=? "
                    "AND status='cancelled' AND error_code='MEMBERSHIP_REVOKED')",
                    (now, workspace_id, workspace_id, user_id),
                )
            if self.account._table_exists(connection, "media_interchanges"):
                connection.execute(
                    "UPDATE media_interchanges SET status='cancelled',staging_path=NULL,"
                    "error_code='WORKSPACE_MEMBERSHIP_REVOKED',error_message='Workspace access was revoked.',updated_at=? "
                    "WHERE workspace_id=? AND status IN ('queued','running','writing') "
                    "AND job_id IN (SELECT id FROM jobs WHERE workspace_id=? AND user_id=? "
                    "AND status='cancelled' AND error_code='MEMBERSHIP_REVOKED')",
                    (now, workspace_id, workspace_id, user_id),
                )
        if self.account._table_exists(connection, "media_recording_sessions"):
            connection.execute(
                """UPDATE media_recording_sessions SET status = 'interrupted',
                       interrupt_reason = 'WORKSPACE_ACCESS_REVOKED', capture_epoch_id = NULL, updated_at = ?
                   WHERE workspace_id = ? AND owner_user_id = ?
                     AND status IN ('armed', 'awaiting_first_segment', 'recording', 'paused', 'stopping')""",
                (now, workspace_id, user_id),
            )
        if self.account._table_exists(connection, "monitor_rules"):
            from .monitoring import _write_version

            rows = connection.execute(
                "SELECT * FROM monitor_rules WHERE workspace_id = ? AND owner_user_id = ? AND status = 'enabled' ORDER BY id",
                (workspace_id, user_id),
            ).fetchall()
            for row in rows:
                next_version = row["version"] + 1
                connection.execute(
                    "UPDATE monitor_rules SET status = 'paused', next_run_at = NULL, version = ?, updated_at = ? WHERE id = ? AND version = ?",
                    (next_version, now, row["id"], row["version"]),
                )
                updated = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (row["id"],)).fetchone()
                _write_version(connection, updated, next_version, "workspace_membership_revoked", actor_user_id, now)
        self.account._invalidate_workspace_backup_protections(connection, workspace_id, user_id, now)
