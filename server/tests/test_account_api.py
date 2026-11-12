from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from server.oevira import create_app
from server.oevira.account import OAuthProviderConfig
from server.oevira.auth import issue_session, utc_now


class MemoryMailer:
    configured = True
    invitation_link_configured = True

    def __init__(self):
        self.messages = []
        self.invitations = []

    def send_code(self, email, purpose, code, expires_at):
        self.messages.append({"email": email, "purpose": purpose, "code": code, "expires_at": expires_at})
        return True

    def code(self, purpose):
        return [item["code"] for item in self.messages if item["purpose"] == purpose][-1]

    def send_invitation(self, email, token, expires_at, workspace_name):
        self.invitations.append({"email": email, "token": token, "expires_at": expires_at, "workspace_name": workspace_name})
        return True

    def invitation_token(self, email):
        return [item["token"] for item in self.invitations if item["email"].casefold() == email.casefold()][-1]


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-account-api-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / "data")
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def test_public_account_gate_clears_stale_cookie_but_rejects_invalid_bearer(self):
        self.client.set_cookie("oevira_session", "expired-cookie-token")
        response = self.client.get("/api/account/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("oevira_session=;" in value for value in response.headers.getlist("Set-Cookie")))
        self.assertEqual(response.json["data"]["email_registration"], "NOT_CONFIGURED")
        self.assertEqual(response.json["data"]["account_export"], "AVAILABLE")
        self.assertEqual(response.json["data"]["workspace_invitations"], "NOT_CONFIGURED")

        invalid_bearer = self.app.test_client().get(
            "/api/account/capabilities", headers={"Authorization": "Bearer invalid-token"}
        )
        self.assertEqual(invalid_bearer.status_code, 401)
        self.assertEqual(invalid_bearer.json["error"]["code"], "UNAUTHENTICATED")

        protected_stale = self.app.test_client()
        protected_stale.set_cookie("oevira_session", "expired-cookie-token")
        rejected_protected = protected_stale.get("/api/account/sessions")
        self.assertEqual(rejected_protected.status_code, 401)

        community_anonymous = self.app.test_client().get("/api/discovery")
        self.assertEqual(community_anonymous.status_code, 200)
        community_stale = self.app.test_client()
        community_stale.set_cookie("oevira_session", "expired-cookie-token")
        rejected = community_stale.get("/api/discovery")
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.json["error"]["code"], "UNAUTHENTICATED")

    def test_stale_cookie_does_not_block_public_password_login_and_local_bootstrap_remains_compatible(self):
        self.client.set_cookie("oevira_session", "stale")
        response = self.client.post(
            "/api/account/password/login",
            json={"email": "nobody@example.test", "password": "An Arbitrary Long Passphrase 2026!"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json["error"]["code"], "INVALID_CREDENTIALS")
        self.assertTrue(any("oevira_session=;" in value for value in response.headers.getlist("Set-Cookie")))

        bootstrap_file = self.root / "data" / "local-bootstrap.txt"
        app = create_app(self.root / "data", bootstrap_file=bootstrap_file)
        app.config.update(TESTING=True)
        client = app.test_client()
        capability = bootstrap_file.read_text(encoding="ascii")
        local = client.post("/api/local-session", json={"capability": capability, "display_name": "Local"})
        self.assertEqual(local.status_code, 200)
        self.assertTrue(local.json["data"]["token"])
        self.assertEqual(client.get("/api/session").status_code, 200)
        self.assertEqual(client.get("/api/account/capabilities").status_code, 200)
        stale_cookie_with_valid_bearer = app.test_client()
        stale_cookie_with_valid_bearer.set_cookie("oevira_session", "stale")
        valid_bearer = stale_cookie_with_valid_bearer.get(
            "/api/account/capabilities",
            headers={"Authorization": f"Bearer {local.json['data']['token']}"},
        )
        self.assertEqual(valid_bearer.status_code, 200)
        self.assertTrue(any("oevira_session=;" in value for value in valid_bearer.headers.getlist("Set-Cookie")))

    def test_email_verification_contract_export_download_and_user_scope(self):
        temporary = tempfile.TemporaryDirectory(prefix="oevira-account-api-mail-")
        self.addCleanup(temporary.cleanup)
        mailer = MemoryMailer()
        data_root = Path(temporary.name) / "data"
        bootstrap_file = data_root / "local-bootstrap.txt"
        app = create_app(data_root, bootstrap_file=bootstrap_file, account_mailer=mailer)
        app.config.update(TESTING=True)
        client = app.test_client()
        local_response = client.post(
            "/api/local-session",
            json={"capability": bootstrap_file.read_text(encoding="ascii"), "display_name": "Local Person"},
        )
        self.assertEqual(local_response.status_code, 200)
        local_user_id = local_response.json["data"]["user"]["id"]

        registered = client.post(
            "/api/account/email/register",
            json={"email": "new@example.test", "display_name": "New Person", "password": "A Correct Long Phrase 2026!"},
        )
        self.assertEqual(registered.status_code, 202)
        reference = registered.json["data"]["reference"]
        verified = client.post(
            "/api/account/email/verify",
            json={"reference": reference, "code": mailer.code("register")},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json["data"]["state"], "email_verified")
        self.assertFalse(any("oevira_session=" in value for value in verified.headers.getlist("Set-Cookie")))

        login = client.post(
            "/api/account/password/login",
            json={"email": "new@example.test", "password": "A Correct Long Phrase 2026!"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["data"]["state"], "authenticated")
        data_choice = login.json["data"]["local_data_choice"]
        self.assertEqual(data_choice["preview"]["source"]["user_id"], local_user_id)
        preview = client.post(
            "/api/account/local-data/preview", json={"reference": data_choice["reference"]}
        )
        self.assertEqual(preview.status_code, 200)
        separate = client.post(
            "/api/account/local-data/choice",
            json={"reference": data_choice["reference"], "choice": "keep_separate"},
        )
        self.assertEqual(separate.status_code, 200)
        local_return_token = separate.json["data"]["local_session_token"]
        self.assertEqual(client.get("/api/session").json["data"]["user"]["id"], login.json["data"]["user"]["id"])
        local_return = client.get(
            "/api/session", headers={"Authorization": f"Bearer {local_return_token}"}
        )
        self.assertEqual(local_return.status_code, 200)
        self.assertEqual(local_return.json["data"]["user"]["id"], local_user_id)
        store = app.extensions["oevira.store"]
        self.assertIsNone(store.row(
            "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (local_response.json["data"]["workspace"]["id"], login.json["data"]["user"]["id"]),
        ))
        self.assertEqual(client.get("/api/session").status_code, 200)

        consent = client.get("/api/account/preferences")
        self.assertEqual(consent.status_code, 200)
        self.assertEqual(consent.json["data"]["model_improvement_consent"], "not_asked")
        deletion_status = client.get("/api/account/deletion/status")
        self.assertEqual(deletion_status.status_code, 200)
        self.assertEqual(deletion_status.json["data"]["state"], "none")
        saved_consent = client.patch(
            "/api/account/preferences",
            json={"model_improvement_consent": "declined", "expected_version": 0},
        )
        self.assertEqual(saved_consent.status_code, 200)
        self.assertFalse(saved_consent.json["data"]["training_started"])

        exported = client.post("/api/account/exports")
        self.assertEqual(exported.status_code, 201)
        download_url = exported.json["data"]["download_url"]
        downloaded = client.get(download_url)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "application/zip")
        self.assertEqual(downloaded.headers["Cache-Control"], "no-store")
        self.assertEqual(hashlib.sha256(downloaded.data).hexdigest(), exported.json["data"]["sha256"])
        downloaded.close()

        store = app.extensions["oevira.store"]
        now = utc_now()
        with store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES('foreign-user', 'Foreign', ?)", (now,))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES('foreign-workspace', 'Foreign', ?)", (now,))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES('foreign-workspace', 'foreign-user', 'owner', 1, ?)", (now,))
        other_token, _ = issue_session(store, "foreign-user", "foreign-workspace", now=now)
        other = app.test_client()
        other.set_cookie("oevira_session", other_token)
        foreign_download = other.get(download_url)
        self.assertEqual(foreign_download.status_code, 404)
        self.assertEqual(foreign_download.json["error"]["code"], "ACCOUNT_EXPORT_NOT_FOUND")

    def test_workspace_member_and_invitation_routes_create_real_membership_rows(self):
        temporary = tempfile.TemporaryDirectory(prefix="oevira-account-members-api-")
        self.addCleanup(temporary.cleanup)
        mailer = MemoryMailer()
        app = create_app(Path(temporary.name) / "data", account_mailer=mailer)
        app.config.update(TESTING=True)
        owner_client = app.test_client()
        self.assertEqual(
            owner_client.get("/api/account/capabilities").json["data"]["workspace_invitations"],
            "AVAILABLE",
        )

        owner_register = owner_client.post(
            "/api/account/email/register",
            json={"email": "owner@example.test", "display_name": "Workspace Owner", "password": "Owner Correct Horse 2026!"},
        )
        owner_client.post(
            "/api/account/email/verify",
            json={"reference": owner_register.json["data"]["reference"], "code": mailer.code("register")},
        )
        owner_login = owner_client.post(
            "/api/account/password/login",
            json={"email": "owner@example.test", "password": "Owner Correct Horse 2026!"},
        )
        workspace_id = owner_login.json["data"]["workspace"]["id"]
        spaces = owner_client.get("/api/account/workspaces")
        self.assertEqual(spaces.status_code, 200)
        self.assertTrue(any(item["id"] == workspace_id for item in spaces.json["data"]["workspaces"]))
        members_before = owner_client.get(f"/api/account/workspaces/{workspace_id}/members")
        self.assertEqual(len(members_before.json["data"]["members"]), 1)

        owner_role_invite = owner_client.post(
            f"/api/account/workspaces/{workspace_id}/invitations",
            json={"email": "member@example.test", "role": "owner"},
        )
        self.assertEqual(owner_role_invite.status_code, 400)
        self.assertEqual(owner_role_invite.json["error"]["code"], "INVALID_INPUT")
        self.assertEqual(mailer.invitations, [])

        invite = owner_client.post(
            f"/api/account/workspaces/{workspace_id}/invitations",
            json={"email": "member@example.test", "role": "editor"},
        )
        self.assertEqual(invite.status_code, 202)
        self.assertEqual(invite.json["data"]["state"], "invitation_sent")
        self.assertNotIn("token", json.dumps(invite.json["data"]))
        self.assertEqual(
            invite.json["data"]["delivery_semantics"],
            "smtp_accepted_not_proof_of_inbox_delivery",
        )
        token = mailer.invitation_token("member@example.test")
        preview = app.test_client().post("/api/account/invitations/preview", json={"token": token})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json["data"]["workspace"]["name"], "Workspace Owner的工作空间")
        self.assertEqual(preview.json["data"]["invited_email_hint"], "m*****@example.test")
        self.assertNotIn("member@example.test", json.dumps(preview.json["data"]))
        mismatch = owner_client.post("/api/account/invitations/accept", json={"token": token})
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(mismatch.json["error"]["code"], "INVITATION_EMAIL_MISMATCH")
        listing = owner_client.get(f"/api/account/workspaces/{workspace_id}/invitations")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json["data"]["invitations"][0]["role"], "editor")

        member_client = app.test_client()
        member_register = member_client.post(
            "/api/account/email/register",
            json={"email": "member@example.test", "display_name": "Invited Editor", "password": "Member Correct Horse 2026!"},
        )
        member_client.post(
            "/api/account/email/verify",
            json={"reference": member_register.json["data"]["reference"], "code": mailer.code("register")},
        )
        member_login = member_client.post(
            "/api/account/password/login",
            json={"email": "member@example.test", "password": "Member Correct Horse 2026!"},
        )
        member_home_workspace = member_login.json["data"]["workspace"]["id"]
        accepted = member_client.post("/api/account/invitations/accept", json={"token": token})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["data"]["workspace"]["id"], workspace_id)
        personal_membership = member_client.get("/api/account/workspaces")
        self.assertEqual(len(personal_membership.json["data"]["workspaces"]), 2)
        switched_to_personal = member_client.post(
            f"/api/account/workspaces/{member_home_workspace}/switch", json={}
        )
        self.assertEqual(switched_to_personal.status_code, 200)
        self.assertEqual(switched_to_personal.json["data"]["workspace"]["id"], member_home_workspace)
        switched_to_team = member_client.post(
            f"/api/account/workspaces/{workspace_id}/switch", json={}
        )
        self.assertEqual(switched_to_team.status_code, 200)
        self.assertEqual(switched_to_team.json["data"]["workspace"]["id"], workspace_id)
        members_after = owner_client.get(f"/api/account/workspaces/{workspace_id}/members")
        self.assertEqual(
            {item["name"] for item in members_after.json["data"]["members"]},
            {"Workspace Owner", "Invited Editor"},
        )
        owner_move = owner_client.post(
            f"/api/account/workspaces/{workspace_id}/owner-transfer",
            json={
                "expected_owner_user_id": owner_login.json["data"]["user"]["id"],
                "new_owner_user_id": accepted.json["data"]["user"]["id"],
            },
        )
        self.assertEqual(owner_move.status_code, 200)
        self.assertEqual(owner_move.json["data"]["owner_user_id"], accepted.json["data"]["user"]["id"])
        forbidden_invites = owner_client.get(f"/api/account/workspaces/{workspace_id}/invitations")
        self.assertEqual(forbidden_invites.status_code, 403)
        role_change = member_client.patch(
            f"/api/account/workspaces/{workspace_id}/members/{owner_login.json['data']['user']['id']}",
            json={"role": "viewer", "expected_role": "editor"},
        )
        self.assertEqual(role_change.status_code, 200)
        blocked_leave = member_client.post(f"/api/account/workspaces/{workspace_id}/leave")
        self.assertEqual(blocked_leave.status_code, 409)
        self.assertEqual(blocked_leave.json["error"]["code"], "LAST_WORKSPACE_OWNER")
        restore_owner = member_client.post(
            f"/api/account/workspaces/{workspace_id}/owner-transfer",
            json={
                "expected_owner_user_id": accepted.json["data"]["user"]["id"],
                "new_owner_user_id": owner_login.json["data"]["user"]["id"],
            },
        )
        self.assertEqual(restore_owner.status_code, 200)
        left = member_client.post(f"/api/account/workspaces/{workspace_id}/leave")
        self.assertEqual(left.status_code, 200)
        self.assertEqual(left.json["data"]["state"], "workspace_left")
        final_members = owner_client.get(f"/api/account/workspaces/{workspace_id}/members")
        self.assertEqual(len(final_members.json["data"]["members"]), 1)

    def test_google_oauth_callback_returns_provider_result_to_client_fragment_only(self):
        temporary = tempfile.TemporaryDirectory(prefix="oevira-oauth-callback-api-")
        self.addCleanup(temporary.cleanup)
        app = create_app(Path(temporary.name) / "data")
        app.config.update(TESTING=True)
        service = app.extensions["oevira.account_service"]
        service.public_base_url = "http://127.0.0.1:8317"
        service.provider_configs["google"] = OAuthProviderConfig(
            provider="google",
            client_id="test-client.apps.googleusercontent.com",
            client_secret="test-client-secret",
            redirect_uri=f"{service.public_base_url}/api/account/oauth/google/callback",
            discovery_url="https://accounts.google.com/.well-known/openid-configuration",
            expected_issuer="https://accounts.google.com",
            allowed_hosts=frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}),
            scope="openid email profile",
            response_mode=None,
        )
        state = "state-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        with service.store.transaction() as connection:
            connection.execute(
                """INSERT INTO account_oauth_transactions(
                       state_hash, provider, intent, code_challenge, nonce_hash,
                       redirect_uri, created_at, expires_at, status
                   ) VALUES(?, 'google', 'login', ?, ?, ?, ?, ?, 'pending')""",
                (
                    hashlib.sha256(state.encode("utf-8")).hexdigest(),
                    "challenge-placeholder",
                    "nonce-placeholder",
                    service.provider_configs["google"].redirect_uri,
                    service._stamp(),
                    "2999-01-01T00:00:00Z",
                ),
            )

        client = app.test_client()
        response = client.get(
            "/api/account/oauth/google/callback",
            query_string={"code": "one-time-code-value", "state": state},
            base_url="http://127.0.0.1:8317",
        )
        self.assertEqual(response.status_code, 303)
        target = urlsplit(response.headers["Location"])
        self.assertEqual(target.scheme, "http")
        self.assertEqual(target.netloc, "127.0.0.1:8317")
        self.assertEqual(target.path, "/")
        self.assertEqual(target.query, "")
        self.assertEqual(
            parse_qs(target.fragment),
            {"oauth_provider": ["google"], "oauth_state": [state], "oauth_code": ["one-time-code-value"]},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(
            service.store.row("SELECT status FROM account_oauth_transactions WHERE state_hash = ?", (hashlib.sha256(state.encode("utf-8")).hexdigest(),))["status"],
            "pending",
        )

        invalid = client.get(
            "/api/account/oauth/google/callback",
            query_string={"code": "code", "state": "missing-state-012345678901234567890"},
            base_url="http://127.0.0.1:8317",
        )
        self.assertEqual(invalid.status_code, 401)
        mismatched_host = client.get(
            "/api/account/oauth/google/callback",
            query_string={"code": "code", "state": state},
            base_url="http://127.0.0.1:8318",
        )
        self.assertEqual(mismatched_host.status_code, 400)

    def test_create_app_binds_environment_smtp_only_with_explicit_delivery_and_link_settings(self):
        temporary = tempfile.TemporaryDirectory(prefix="oevira-smtp-config-api-")
        self.addCleanup(temporary.cleanup)
        configured = {
            "OEVIRA_SMTP_HOST": "127.0.0.1",
            "OEVIRA_SMTP_PORT": "2525",
            "OEVIRA_SMTP_FROM": "account@example.test",
            "OEVIRA_SMTP_SECURITY": "none",
            "OEVIRA_PUBLIC_BASE_URL": "http://127.0.0.1:8317",
        }
        with patch.dict("os.environ", configured, clear=True):
            app = create_app(Path(temporary.name) / "configured")
        service = app.extensions["oevira.account_service"]
        self.assertTrue(service._mailer_ready())
        self.assertTrue(service._invitation_mailer_ready())
        self.assertEqual(service.public_base_url, "http://127.0.0.1:8317")
        capabilities = app.test_client().get("/api/account/capabilities").json["data"]
        self.assertEqual(capabilities["email_registration"], "AVAILABLE")
        self.assertEqual(capabilities["workspace_invitations"], "AVAILABLE")
        self.assertEqual(capabilities["oauth"]["google"], "NOT_CONFIGURED")

        without_product_url = dict(configured)
        without_product_url.pop("OEVIRA_PUBLIC_BASE_URL")
        with patch.dict("os.environ", without_product_url, clear=True):
            app_without_url = create_app(Path(temporary.name) / "no-product-url")
        self.assertTrue(app_without_url.extensions["oevira.account_service"]._mailer_ready())
        self.assertFalse(app_without_url.extensions["oevira.account_service"]._invitation_mailer_ready())
        self.assertEqual(
            app_without_url.test_client().get("/api/account/capabilities").json["data"]["workspace_invitations"],
            "NOT_CONFIGURED",
        )

        with patch.dict("os.environ", {"OEVIRA_SMTP_HOST": "smtp.example.test"}, clear=True):
            with self.assertRaises(ValueError):
                create_app(Path(temporary.name) / "partial-smtp")


if __name__ == "__main__":
    unittest.main()
