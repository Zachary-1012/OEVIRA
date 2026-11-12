from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from server.oevira.account import (
    AccountError,
    AccountService,
    OAuthProviderConfig,
    _b64url_encode,
    install_account_schema,
)
from server.oevira.account_membership import AccountWorkspaceMemberships
from server.oevira.auth import AuthenticationError, authenticate, exchange_local_capability, issue_local_capability, issue_session
from server.oevira.browser import install_browser_schema
from server.oevira.business import install_business_schema
from server.oevira.business_execution import install_business_execution_schema
from server.oevira.collaboration import install_collaboration_schema
from server.oevira.community import install_community_schema
from server.oevira.media import install_media_schema
from server.oevira.store import Store


class MutableClock:
    def __init__(self):
        # Session authentication uses the real clock. A frozen past start
        # makes newly issued sessions already expired (default lifetime is 7 days).
        self.value = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


class MemoryMailer:
    configured = True
    invitation_link_configured = True

    def __init__(self):
        self.messages: list[dict] = []
        self.invitations: list[dict] = []

    def send_code(self, email: str, purpose: str, code: str, expires_at: str) -> bool:
        self.messages.append({"email": email, "purpose": purpose, "code": code, "expires_at": expires_at})
        return True

    def latest_code(self, purpose: str, email: str | None = None) -> str:
        rows = [row for row in self.messages if row["purpose"] == purpose and (email is None or row["email"].casefold() == email.casefold())]
        if not rows:
            raise AssertionError(f"No test delivery for {purpose}")
        return rows[-1]["code"]

    def send_invitation(self, email: str, token: str, expires_at: str, workspace_name: str) -> bool:
        self.invitations.append({"email": email, "token": token, "expires_at": expires_at, "workspace_name": workspace_name})
        return True

    def latest_invitation_token(self, email: str) -> str:
        rows = [item for item in self.invitations if item["email"].casefold() == email.casefold()]
        if not rows:
            raise AssertionError("No test invitation was sent")
        return rows[-1]["token"]


class FakeOAuthTransport:
    def __init__(self, config: OAuthProviderConfig, jwks: dict):
        self.config = config
        self.jwks = jwks
        self.id_token = ""
        self.token_requests: list[dict] = []
        self.metadata = {
            "issuer": config.expected_issuer,
            "authorization_endpoint": (
                "https://accounts.google.com/o/oauth2/v2/auth"
                if config.provider == "google"
                else "https://appleid.apple.com/auth/authorize"
            ),
            "token_endpoint": (
                "https://oauth2.googleapis.com/token"
                if config.provider == "google"
                else "https://appleid.apple.com/auth/token"
            ),
            "jwks_uri": (
                "https://www.googleapis.com/oauth2/v3/certs"
                if config.provider == "google"
                else "https://appleid.apple.com/auth/keys"
            ),
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }

    def get_json(self, url: str, allowed_hosts: frozenset[str]) -> dict:
        if url == self.config.discovery_url:
            return self.metadata
        if url == self.metadata["jwks_uri"]:
            return self.jwks
        raise AssertionError(f"Unexpected provider URL: {url}")

    def post_form(self, url: str, values: dict[str, str], allowed_hosts: frozenset[str]) -> dict:
        self.token_requests.append(dict(values))
        return {"id_token": self.id_token, "access_token": "test-access-token-must-not-be-stored"}


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.private_key.public_key().public_numbers()
        cls.test_jwk = {
            "kty": "RSA",
            "kid": "test-rsa-1",
            "use": "sig",
            "alg": "RS256",
            "n": _b64url_encode(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64url_encode(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-account-")
        self.store = Store(Path(self.temporary.name))
        install_account_schema(self.store)
        install_business_schema(self.store)
        install_business_execution_schema(self.store)
        install_browser_schema(self.store)
        install_collaboration_schema(self.store)
        install_community_schema(self.store)
        install_media_schema(self.store)
        self.clock = MutableClock()
        self.mailer = MemoryMailer()
        self.service = AccountService(self.store, mailer=self.mailer, clock=self.clock)

    def tearDown(self):
        self.temporary.cleanup()

    def create_local_identity(self, name="Local Owner"):
        user_id = "local-user-1"
        workspace_id = "local-workspace-1"
        now = self.service._stamp()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, name, now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "Local Workspace", now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)", (workspace_id, user_id, now))
            connection.execute("INSERT INTO local_identity(singleton, user_id, workspace_id, created_at) VALUES(1, ?, ?, ?)", (user_id, workspace_id, now))
        token, _ = issue_session(self.store, user_id, workspace_id, now=now)
        return token, authenticate(self.store, token)

    def create_local_work(self, local_identity):
        work_id = "local-work-1"
        artifact_id = "local-artifact-1"
        material_id = "local-material-1"
        now = self.service._stamp()
        user_id = local_identity["user"]["id"]
        workspace_id = local_identity["workspace"]["id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO works(id, workspace_id, owner_user_id, title, goal, brief_json, status, version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, '{}', 'active', 1, ?, ?)",
                (work_id, workspace_id, user_id, "Local Work", "Preserve this work", now, now),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id, version, title, goal, brief_json, status, created_at, created_by) VALUES(?, 1, ?, ?, '{}', 'active', ?, ?)",
                (work_id, "Local Work", "Preserve this work", now, user_id),
            )
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason, text, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, ?, ?, ?, 'txt', 'ready', NULL, 'source text', ?, ?, 11, ?)",
                (material_id, workspace_id, user_id, work_id, "Local source", "source.txt", "0" * 64, "objects/local.txt", now),
            )
            connection.execute(
                "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                (artifact_id, workspace_id, work_id, user_id, "Local result", now, now),
            )
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by) VALUES(?, ?, 1, ?, 'Original result', '[]', '{}', '', ?, ?)",
                (artifact_id, workspace_id, "Local result", now, user_id),
            )
            connection.execute(
                "INSERT INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_id, material_id, workspace_id, now),
            )
            connection.execute(
                "INSERT INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_id, artifact_id, workspace_id, now),
            )
        return {"work": work_id, "material": material_id, "artifact": artifact_id}

    @staticmethod
    def google_config() -> OAuthProviderConfig:
        return OAuthProviderConfig(
            provider="google",
            client_id="test-client.apps.googleusercontent.com",
            client_secret="test-only-client-secret",
            redirect_uri="http://127.0.0.1:8317/api/account/oauth/google/callback",
            discovery_url="https://accounts.google.com/.well-known/openid-configuration",
            expected_issuer="https://accounts.google.com",
            allowed_hosts=frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}),
            scope="openid email profile",
            response_mode=None,
        )

    @staticmethod
    def apple_config() -> OAuthProviderConfig:
        return OAuthProviderConfig(
            provider="apple",
            client_id="com.example.oevira.web",
            client_secret="example-test-only-pre-signed-apple-client-secret",
            redirect_uri="https://accounts.example.test/api/account/oauth/apple/callback",
            discovery_url="https://appleid.apple.com/.well-known/openid-configuration",
            expected_issuer="https://appleid.apple.com",
            allowed_hosts=frozenset({"appleid.apple.com"}),
            scope="openid email",
            response_mode="form_post",
        )

    def oauth_service(self, provider: str = "google"):
        config = self.google_config() if provider == "google" else self.apple_config()
        transport = FakeOAuthTransport(config, {"keys": [self.test_jwk]})
        service = AccountService(
            self.store,
            mailer=self.mailer,
            provider_configs={"google": config if provider == "google" else None,
                              "apple": config if provider == "apple" else None},
            oauth_transport=transport,
            clock=self.clock,
        )
        return service, config, transport

    def signed_id_token(
        self,
        *,
        provider: str,
        nonce: str,
        email: str = "oauth.person@example.test",
        subject: str = "provider-subject-1",
        issuer: str | None = None,
        audience: str | list[str] | None = None,
        expires_at: int | None = None,
        issued_at: int | None = None,
        nonce_claim: str | None = None,
        email_verified: bool = True,
        corrupt_signature: bool = False,
    ) -> str:
        config = self.google_config() if provider == "google" else self.apple_config()
        now = int(self.clock.value.timestamp())
        header = {"alg": "RS256", "kid": "test-rsa-1", "typ": "JWT"}
        claims = {
            "iss": issuer or config.expected_issuer,
            "aud": audience or config.client_id,
            "sub": subject,
            "iat": now - 10 if issued_at is None else issued_at,
            "exp": now + 300 if expires_at is None else expires_at,
            "nonce": nonce if nonce_claim is None else nonce_claim,
            "email": email,
            "email_verified": email_verified,
            "name": "OAuth Test User",
        }
        header_part = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        claims_part = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signing_input = f"{header_part}.{claims_part}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        if corrupt_signature:
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        return f"{header_part}.{claims_part}.{_b64url_encode(signature)}"

    def start_oauth(self, service, transport, *, provider="google", intent="login", identity=None, verifier=None, **claims):
        verifier = verifier or _b64url_encode(b"v" * 48)
        start = service.begin_oauth(provider, code_verifier=verifier, intent=intent, identity=identity)
        query = parse_qs(urlsplit(start["authorization_url"]).query)
        nonce = query["nonce"][0]
        transport.id_token = self.signed_id_token(provider=provider, nonce=nonce, **claims)
        return start, verifier

    def create_verified_account(self, email="person@example.test", password="Correct Horse Battery 2026!"):
        registered = self.service.register_email(email, display_name="Test Person", password=password)
        self.assertEqual(registered["state"], "accepted_if_eligible")
        code = self.mailer.latest_code("register", email)
        verified = self.service.verify_email(registered["reference"], code)
        return verified, password

    def test_install_is_additive_and_preserves_local_bootstrap_and_session_shape(self):
        capability_file = Path(self.temporary.name) / "bootstrap.capability"
        issue_local_capability(self.store, capability_file)
        token, local_identity = exchange_local_capability(self.store, capability_file.read_text(encoding="ascii"), "本机用户")
        before = authenticate(self.store, token)

        install_account_schema(self.store)
        install_account_schema(self.store)

        self.assertEqual(authenticate(self.store, token), before)
        self.assertEqual(local_identity["user"], before["user"])
        self.assertEqual(local_identity["workspace"], before["workspace"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 1)
        self.assertEqual(self.store.row("PRAGMA user_version")["user_version"], 5)

    def test_missing_email_transport_is_unavailable_without_account_side_effect(self):
        service = AccountService(self.store, mailer=None, clock=self.clock)
        with self.assertRaises(AccountError) as result:
            service.register_email("new@example.test", display_name="New User")
        self.assertEqual(result.exception.code, "EMAIL_DELIVERY_UNAVAILABLE")
        with self.assertRaises(AccountError):
            service.request_password_reset("new@example.test")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_challenges")["count"], 0)

    def test_email_registration_verification_password_login_and_logout(self):
        result = self.service.register_email(
            "new.person@example.test", display_name="New Person", password="Correct Horse Battery 2026!"
        )
        code = self.mailer.latest_code("register", "new.person@example.test")
        self.assertEqual(result["state"], "accepted_if_eligible")
        self.assertNotIn(code, json.dumps(result))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM memberships")["count"], 0)
        with self.assertRaises(AccountError) as wrong:
            self.service.verify_email(result["reference"], "000000")
        self.assertEqual(wrong.exception.code, "INVALID_VERIFICATION_CODE")

        verified = self.service.verify_email(result["reference"], code)
        self.assertEqual(verified["state"], "email_verified")
        self.assertEqual(self.store.row("SELECT verified_at FROM account_emails")["verified_at"], self.service._stamp())
        membership = self.store.row("SELECT role, active FROM memberships WHERE user_id = ?", (verified["user"]["id"],))
        self.assertEqual((membership["role"], membership["active"]), ("owner", 1))

        login = self.service.login_password("new.person@example.test", "Correct Horse Battery 2026!")
        identity = authenticate(self.store, login["token"])
        self.assertEqual(login["state"], "authenticated")
        self.assertEqual(identity["user"]["id"], verified["user"]["id"])
        sessions = self.service.sessions(identity)
        self.assertEqual(len(sessions), 1)
        self.assertNotIn("token", sessions[0])
        self.assertNotIn("token_hash", sessions[0])

        self.assertEqual(self.service.logout_current_session(identity)["state"], "session_revoked")
        self.assertIsNone(authenticate(self.store, login["token"]))

    def test_email_login_code_and_password_reset_revoke_every_session(self):
        _, password = self.create_verified_account()
        first = self.service.login_password("person@example.test", password)
        email_login = self.service.request_email_login("person@example.test")
        code = self.mailer.latest_code("email_login", "person@example.test")
        second = self.service.complete_email_login(email_login["reference"], code)
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(len(self.service.sessions(authenticate(self.store, first["token"]))), 2)

        reset = self.service.request_password_reset("person@example.test")
        reset_code = self.mailer.latest_code("password_reset", "person@example.test")
        changed = self.service.reset_password(reset["reference"], reset_code, "A New Correct Phrase 2026!")
        self.assertEqual(changed["state"], "password_changed")
        self.assertEqual(changed["revoked_sessions"], 2)
        self.assertIsNone(authenticate(self.store, first["token"]))
        self.assertIsNone(authenticate(self.store, second["token"]))
        with self.assertRaises(AccountError):
            self.service.login_password("person@example.test", password)
        self.assertEqual(self.service.login_password("person@example.test", "A New Correct Phrase 2026!")["state"], "authenticated")

    def test_unknown_email_login_request_is_uniform_and_does_not_send(self):
        result = self.service.request_email_login("unknown@example.test")
        self.assertEqual(result["state"], "accepted_if_eligible")
        self.assertIn("resend_at", result)
        self.assertIn("expires_at", result)
        self.assertEqual(self.mailer.messages, [])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 0)

    def test_expired_and_incorrect_email_codes_return_distinct_service_states(self):
        self.create_verified_account()
        request = self.service.request_email_login("person@example.test")
        code = self.mailer.latest_code("email_login", "person@example.test")
        with self.assertRaises(AccountError) as incorrect:
            self.service.complete_email_login(request["reference"], "999999" if code != "999999" else "999998")
        self.assertEqual(incorrect.exception.code, "INVALID_VERIFICATION_CODE")
        self.clock.advance(minutes=11)
        with self.assertRaises(AccountError) as expired:
            self.service.complete_email_login(request["reference"], code)
        self.assertEqual(expired.exception.code, "VERIFICATION_CODE_EXPIRED")

    def test_email_code_cooldown_rate_limit_and_attempt_limit(self):
        self.create_verified_account()
        for expected_count in (1, 2, 3):
            self.service.request_email_login("person@example.test")
            self.assertEqual(len([m for m in self.mailer.messages if m["purpose"] == "email_login"]), expected_count)
            if expected_count < 3:
                self.clock.advance(seconds=61)
        self.clock.advance(seconds=61)
        with self.assertRaises(AccountError) as limited:
            self.service.request_email_login("person@example.test")
        self.assertEqual(limited.exception.code, "EMAIL_CODE_RATE_LIMITED")

        # A later window permits a fresh code; five failed entries invalidate it.
        self.clock.advance(minutes=5)
        request = self.service.request_email_login("person@example.test")
        for _ in range(5):
            with self.assertRaises(AccountError):
                self.service.complete_email_login(request["reference"], "000000")
        actual = self.mailer.latest_code("email_login", "person@example.test")
        with self.assertRaises(AccountError):
            self.service.complete_email_login(request["reference"], actual)

    def test_password_policy_rejects_short_and_known_weak_passwords(self):
        with self.assertRaises(AccountError) as short:
            self.service.register_email("short@example.test", password="short")
        self.assertEqual(short.exception.code, "PASSWORD_TOO_SHORT")
        with self.assertRaises(AccountError) as weak:
            self.service.register_email("weak@example.test", password="passwordpassword")
        self.assertEqual(weak.exception.code, "PASSWORD_BLOCKLISTED")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 0)

    def test_password_login_uses_generic_failure_for_unknown_and_wrong_password(self):
        _, password = self.create_verified_account()
        errors = []
        for email, value in (("missing@example.test", "Wrong Password 123456!"), ("person@example.test", "Wrong Password 123456!")):
            with self.assertRaises(AccountError) as result:
                self.service.login_password(email, value)
            errors.append((result.exception.code, str(result.exception)))
        self.assertEqual(errors[0], errors[1])
        self.assertEqual(self.service.login_password("person@example.test", password)["state"], "authenticated")

    def test_workspace_choice_is_limited_to_active_memberships(self):
        verified, password = self.create_verified_account()
        first_workspace = verified["workspace"]["id"]
        second_workspace = "workspace-second"
        foreign_workspace = "workspace-foreign"
        foreign_user = "user-foreign"
        now = self.service._stamp()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (second_workspace, "Second", now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'editor', 1, ?)", (second_workspace, verified["user"]["id"], now))
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (foreign_user, "Foreign", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (foreign_workspace, "Foreign", now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)", (foreign_workspace, foreign_user, now))

        choice = self.service.login_password("person@example.test", password)
        self.assertEqual(choice["state"], "workspace_selection_required")
        self.assertEqual({row["id"] for row in choice["workspaces"]}, {first_workspace, second_workspace})
        with self.assertRaises(AccountError) as denied:
            self.service.complete_login_challenge(choice["login_challenge"], foreign_workspace)
        self.assertEqual(denied.exception.code, "WORKSPACE_NOT_FOUND")
        selected = self.service.complete_login_challenge(choice["login_challenge"], second_workspace)
        self.assertEqual(selected["workspace"]["id"], second_workspace)

    def test_session_revocation_is_user_scoped_and_revokes_all_when_requested(self):
        verified, password = self.create_verified_account()
        first = self.service.login_password("person@example.test", password)
        second = self.service.login_password("person@example.test", password)
        other, other_password = self.create_verified_account("another@example.test", "Another Correct Phrase 2026!")
        other_session = self.service.login_password("another@example.test", other_password)
        other_identity = authenticate(self.store, other_session["token"])
        with self.assertRaises(AccountError) as denied:
            self.service.revoke_one_session(other_identity, authenticate(self.store, first["token"])["session_id"])
        self.assertEqual(denied.exception.code, "SESSION_NOT_FOUND")

        first_identity = authenticate(self.store, first["token"])
        self.assertEqual(self.service.revoke_one_session(first_identity, first_identity["session_id"])["state"], "session_revoked")
        second_identity = authenticate(self.store, second["token"])
        result = self.service.revoke_all_user_sessions(second_identity)
        self.assertEqual(result["revoked_count"], 1)
        self.assertIsNone(authenticate(self.store, second["token"]))
        self.assertIsNotNone(authenticate(self.store, other_session["token"]))

    def test_account_schema_does_not_auto_merge_same_email_provider_identity(self):
        existing, _ = self.create_verified_account()
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport, email="person@example.test")
        with self.assertRaises(AccountError) as result:
            service.complete_oauth("google", code="code-1", state=start["state_token"], code_verifier=verifier)
        self.assertEqual(result.exception.code, "IDENTITY_LINK_REQUIRED")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 1)

    def test_oauth_login_from_local_session_returns_the_same_explicit_data_choice(self):
        _local_token, local_identity = self.create_local_identity()
        service, _config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(
            service,
            transport,
            provider="google",
            identity=local_identity,
            email="oauth-new@example.test",
        )
        result = service.complete_oauth(
            "google",
            code="oauth-code",
            state=start["state_token"],
            code_verifier=verifier,
            identity=local_identity,
        )
        self.assertEqual(result["state"], "authenticated")
        self.assertEqual(result["local_data_choice"]["preview"]["source"]["user_id"], local_identity["user"]["id"])
        self.assertTrue(result["local_data_choice"]["local_session_token"])
        self.assertTrue(result["local_data_choice"]["preview"]["platform_sessions_transferred"] is False)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_external_identities")["count"], 1)

    def test_explicit_identity_link_is_session_bound_and_last_method_is_protected(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport, intent="link", identity=identity, email="person@example.test")
        result = service.complete_oauth(
            "google", code="code-link", state=start["state_token"], code_verifier=verifier, identity=None
        )
        self.assertEqual(result["state"], "identity_linked")
        self.assertTrue(any(item["provider"] == "google" for item in service.login_methods(identity)["external_identities"]))
        identity_id = self.store.row("SELECT id FROM account_external_identities WHERE user_id = ?", (verified["user"]["id"],))["id"]
        self.assertEqual(service.unlink_external_identity(identity, identity_id)["state"], "identity_unlinked")
        self.assertEqual(service.unlink_email(identity)["state"], "email_unlinked")

    def test_provider_subject_bound_to_one_user_cannot_be_linked_to_another(self):
        first, password = self.create_verified_account()
        first_session = self.service.login_password("person@example.test", password)
        first_identity = authenticate(self.store, first_session["token"])
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport, intent="link", identity=first_identity, email="person@example.test", subject="shared-subject")
        service.complete_oauth("google", code="link-first", state=start["state_token"], code_verifier=verifier, identity=first_identity)

        second, second_password = self.create_verified_account("second@example.test", "Second Correct Phrase 2026!")
        second_session = self.service.login_password("second@example.test", second_password)
        second_identity = authenticate(self.store, second_session["token"])
        start, verifier = self.start_oauth(service, transport, intent="link", identity=second_identity, email="second@example.test", subject="shared-subject")
        with self.assertRaises(AccountError) as conflict:
            service.complete_oauth("google", code="link-second", state=start["state_token"], code_verifier=verifier, identity=second_identity)
        self.assertEqual(conflict.exception.code, "IDENTITY_ALREADY_LINKED")
        identity = self.store.row("SELECT user_id FROM account_external_identities WHERE provider = 'google' AND subject = 'shared-subject'")
        self.assertEqual(identity["user_id"], first["user"]["id"])
        self.assertNotEqual(identity["user_id"], second["user"]["id"])

    def test_last_external_login_method_cannot_be_removed(self):
        config = self.google_config()
        transport = FakeOAuthTransport(config, {"keys": [self.test_jwk]})
        service = AccountService(
            self.store,
            mailer=None,
            provider_configs={"google": config, "apple": None},
            oauth_transport=transport,
            clock=self.clock,
        )
        start, verifier = self.start_oauth(service, transport, email="oauth.only@example.test")
        result = service.complete_oauth("google", code="oauth-only", state=start["state_token"], code_verifier=verifier)
        identity = authenticate(self.store, result["token"])
        linked = self.store.row("SELECT id FROM account_external_identities WHERE user_id = ?", (identity["user"]["id"],))
        with self.assertRaises(AccountError) as last:
            service.unlink_external_identity(identity, linked["id"])
        self.assertEqual(last.exception.code, "LAST_LOGIN_METHOD")

    def test_pkce_state_nonce_signature_issuer_audience_and_expiration_are_verified(self):
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport, email="valid.oauth@example.test")
        query = parse_qs(urlsplit(start["authorization_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        expected_challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(query["code_challenge"], [expected_challenge])
        stored = self.store.row("SELECT code_challenge, nonce_hash FROM account_oauth_transactions WHERE state_hash = ?", (hashlib.sha256(start["state_token"].encode()).hexdigest(),))
        self.assertEqual(stored["code_challenge"], expected_challenge)
        self.assertNotIn("code_verifier", {row["name"] for row in self.store.rows("PRAGMA table_info(account_oauth_transactions)")})
        result = service.complete_oauth("google", code="valid-code", state=start["state_token"], code_verifier=verifier)
        self.assertEqual(result["state"], "authenticated")
        self.assertIsNotNone(authenticate(self.store, result["token"]))
        self.assertEqual(len(transport.token_requests), 1)
        with self.assertRaises(AccountError) as replay:
            service.complete_oauth("google", code="valid-code", state=start["state_token"], code_verifier=verifier)
        self.assertEqual(replay.exception.code, "OAUTH_STATE_INVALID")

        invalid_cases = (
            {"corrupt_signature": True, "code": "signature"},
            {"issuer": "https://attacker.example", "code": "issuer"},
            {"audience": "another-client", "code": "audience"},
            {"expires_at": int(self.clock.value.timestamp()) - 300, "code": "expired"},
            {"nonce_claim": "wrong-nonce", "code": "nonce"},
        )
        for case in invalid_cases:
            start, verifier = self.start_oauth(service, transport, email=f"{case['code']}@example.test", **{k: v for k, v in case.items() if k != "code"})
            with self.assertRaises(AccountError):
                service.complete_oauth("google", code=case["code"], state=start["state_token"], code_verifier=verifier)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_external_identities")["count"], 1)

    def test_pkce_mismatch_consumes_failed_state_without_token_exchange(self):
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport)
        with self.assertRaises(AccountError) as result:
            service.complete_oauth("google", code="code", state=start["state_token"], code_verifier=verifier + "x")
        self.assertEqual(result.exception.code, "OAUTH_PKCE_INVALID")
        self.assertEqual(transport.token_requests, [])
        status = self.store.row("SELECT status FROM account_oauth_transactions WHERE state_hash = ?", (hashlib.sha256(start["state_token"].encode()).hexdigest(),))
        self.assertEqual(status["status"], "failed")

    def test_apple_provider_uses_its_own_issuer_audience_and_callback_mode(self):
        service, config, transport = self.oauth_service("apple")
        start, verifier = self.start_oauth(service, transport, provider="apple", email="apple.user@example.test")
        query = parse_qs(urlsplit(start["authorization_url"]).query)
        self.assertEqual(query["response_mode"], ["form_post"])
        result = service.complete_oauth("apple", code="apple-code", state=start["state_token"], code_verifier=verifier)
        self.assertEqual(result["state"], "authenticated")
        self.assertEqual(self.store.row("SELECT provider FROM account_external_identities")["provider"], "apple")

    def test_provider_not_configured_returns_unavailable_without_state_or_account(self):
        service = AccountService(
            self.store,
            provider_configs={"google": None, "apple": None},
            oauth_transport=object(),
            clock=self.clock,
        )
        with self.assertRaises(AccountError) as result:
            service.begin_oauth("google", code_verifier=_b64url_encode(b"v" * 48))
        self.assertEqual(result.exception.code, "OAUTH_PROVIDER_NOT_CONFIGURED")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_oauth_transactions")["count"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM users")["count"], 0)

    def test_oauth_state_expires_and_provider_callback_is_not_fabricated(self):
        service, config, transport = self.oauth_service("google")
        start, verifier = self.start_oauth(service, transport)
        self.clock.advance(minutes=6)
        with self.assertRaises(AccountError) as result:
            service.complete_oauth("google", code="late-code", state=start["state_token"], code_verifier=verifier)
        self.assertEqual(result.exception.code, "OAUTH_STATE_INVALID")
        self.assertEqual(transport.token_requests, [])

    def test_account_deletion_requires_export_and_email_then_has_a_reversible_window(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        service = AccountService(
            self.store,
            mailer=self.mailer,
            clock=self.clock,
            user_data_purger=lambda connection, user_id: False,
        )
        requested = service.request_account_deletion(identity)
        self.assertEqual(requested["state"], "deletion_confirmation_required")
        self.assertTrue(requested["export_download_url"].endswith("/download"))
        self.assertEqual(requested["export_expires_at"], self.service._stamp(self.clock.value + timedelta(hours=24)))
        confirm_code = self.mailer.latest_code("delete_confirm", "person@example.test")
        scheduled = service.confirm_account_deletion(requested["reference"], confirm_code)
        deadline = datetime.fromisoformat(scheduled["scheduled_at"].replace("Z", "+00:00"))
        self.assertEqual(deadline - self.clock.value, timedelta(days=30))
        self.assertIsNone(authenticate(self.store, login["token"]))

        cancel_request = service.request_deletion_cancellation("person@example.test")
        cancel_code = self.mailer.latest_code("delete_cancel", "person@example.test")
        self.assertEqual(service.cancel_account_deletion(cancel_request["reference"], cancel_code)["state"], "deletion_cancelled")
        self.assertEqual(service.login_password("person@example.test", password)["state"], "authenticated")

    def test_account_export_includes_owned_source_and_expires_with_integrity_scope(self):
        from server.oevira.acquisition_queries import install_query_schema

        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        user_id = verified["user"]["id"]
        workspace_id = identity["workspace"]["id"]
        material_id = "owned-material-export"
        relative_path = f"objects/{workspace_id}/materials/{material_id}/original"
        file_path = self.store.root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        original = b"the user's exact original source"
        file_path.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, format, status, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, 'Owned source', 'source.txt', 'txt', 'ready', ?, ?, ?, ?)",
                (material_id, workspace_id, user_id, digest, relative_path, len(original), self.service._stamp()),
            )
            now = self.service._stamp()
            project_id = "owned-media-export"
            transcription_id = "owned-media-asr-export"
            connection.execute(
                "INSERT INTO media_projects(id, workspace_id, owner_user_id, title, kind, current_version, created_at, updated_at, created_by) VALUES(?, ?, ?, 'Owned Media transcript', 'podcast', 1, ?, ?, ?)",
                (project_id, workspace_id, user_id, now, now, user_id),
            )
            connection.execute(
                "INSERT INTO media_project_versions(project_id, workspace_id, version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) VALUES(?, ?, 1, 'create', '{}', '[]', '[]', ?, ?)",
                (project_id, workspace_id, now, user_id),
            )
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, kind, status, payload_json, request_hash, idempotency_key, created_at, updated_at) VALUES(?, ?, ?, 'media_transcribe', 'completed', '{}', ?, ?, ?, ?)",
                (transcription_id, workspace_id, user_id, "1" * 64, "owned-media-asr-export-key", now, now),
            )
            connection.execute(
                "INSERT INTO media_transcriptions(job_id, workspace_id, project_id, project_version, source_asset_id, source_material_id, source_sha256, audio_stream_index, audio_channel_index, source_sample_rate, source_time_base_num, source_time_base_den, language_hint, task, normalized_sample_rate, model_version, model_sha256, result_metadata_json, created_at, updated_at) VALUES(?, ?, ?, 1, 'MEDIA-candidate-source', ?, ?, 0, 0, 16000, 1, 16000, 'en', 'transcribe', 16000, '20250625', ?, '{}', ?, ?)",
                (transcription_id, workspace_id, project_id, material_id, digest,
                 "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e", now, now),
            )
            connection.execute(
                "INSERT INTO media_transcription_chunks(job_id, chunk_index, normalized_start_sample, normalized_end_sample, source_start_sample, source_end_sample, language, text, segments_json, words_json, created_at) VALUES(?, 0, 0, 16000, 0, 16000, 'en', 'Unreviewed candidate', '[]', ?, ?)",
                (transcription_id, json.dumps([{"word_id": "asr:owned:0:0", "text": " candidate", "start_sample": 3200, "end_sample": 8000}], ensure_ascii=False), now),
            )
        install_query_schema(self.store)
        query_id, path_id, other_query_id, browser_session_id = (
            "owned-query-export", "owned-browser-path-export", "other-member-query-export",
            "private-browser-session-export",
        )
        preview_expiry = self.service._stamp(self.clock.value + timedelta(minutes=10))
        install_browser_schema(self.store)
        browser_preview = {
            "status": "complete",
            "results": [{"quote": "UNADMITTED_BROWSER_PREVIEW_BODY"}],
            "coverage": {"status": "complete"},
            "source_ref": {
                "kind": "browser_page", "session_id": browser_session_id, "page_id": "page-export",
                "page_revision": 4, "snapshot_sha256": "a" * 64, "work_id": None,
                "url": "https://browser.example.test/page?access_token=QUERY_PREVIEW_SECRET",
                "title": "Private page title", "captured_at": now,
            },
        }
        with self.store.transaction() as connection:
            if not connection.execute("SELECT 1 FROM browser_sessions WHERE id=?", (browser_session_id,)).fetchone():
                member = connection.execute("SELECT created_at FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)).fetchone()
                connection.execute(
                    "INSERT INTO browser_sessions(id,workspace_id,owner_user_id,membership_created_at,authorized_by_session_id,"
                    "engine,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'edge','closed',1,?,?)",
                    (browser_session_id, workspace_id, user_id, member["created_at"], identity["session_id"], now, now),
                )
            connection.execute(
                "INSERT INTO browser_session_operations(id,session_id,workspace_id,owner_user_id,operation,idempotency_key,"
                "request_hash,status,result_json,preview_json,preview_expires_at,created_at,updated_at) "
                "VALUES('account-frozen-preview',?,?,?,'context_preview','account-frozen-preview','digest','completed',?,?,?,?,?)",
                (browser_session_id, workspace_id, user_id,
                 json.dumps({"capture_ref": {"capture_id": "account-frozen-preview", "session_id": browser_session_id,
                                             "expires_at": preview_expiry}}),
                 json.dumps({"text": "FROZEN_BROWSER_BODY_NOT_FOR_ACCOUNT_EXPORT"}), preview_expiry, now, now),
            )
            connection.execute(
                "INSERT INTO users(id,name,created_at) VALUES(?,'Other member',?)",
                ("other-member-query-export", now),
            )
            for owner_id, current_query_id, idempotency in (
                (user_id, query_id, "owned-query-export-key"),
                ("other-member-query-export", other_query_id, "other-query-export-key"),
            ):
                connection.execute(
                    "INSERT INTO acquisition_query_plans(query_id,workspace_id,owner_user_id,work_id,query_revision,"
                    "idempotency_key,request_hash,request_json,created_at,updated_at) VALUES(?,?,?,NULL,1,?,?,?,?,?)",
                    (current_query_id, workspace_id, owner_id, idempotency, "c" * 64,
                     json.dumps({"query": "orchid", "public_urls": [
                         "https://search.example.test/?api_key=QUERY_URL_SECRET",
                     ]}), now, now),
                )
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,child_job_id,result_json,"
                "status,coverage_json,checkpoint_ref_json,stop_reason,preview_expires_at,created_at,updated_at) "
                "VALUES(?,?,?,0,NULL,'browser_page','selected_browser_page',?,?,?,NULL,?,'complete',?,?,NULL,?,?,?)",
                (path_id, query_id, workspace_id,
                 json.dumps({"kind": "browser_page", "session_id": browser_session_id,
                             "page_id": "page-export", "page_revision": 4}),
                 json.dumps({"query": "orchid", "target": {"session_id": browser_session_id,
                                                               "page_id": "page-export", "page_revision": 4}}),
                 "[]", json.dumps(browser_preview), json.dumps({"status": "complete"}),
                 json.dumps({"browser_session_id": browser_session_id, "page_id": "page-export",
                             "page_revision": 4, "snapshot_sha256": "a" * 64}),
                 preview_expiry, now, now),
            )
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,status,coverage_json,created_at,updated_at) "
                "VALUES('other-member-query-path',?,?,0,NULL,'material_search','local_materials','{}','{}','[]','complete','{}',?,?)",
                (other_query_id, workspace_id, now, now),
            )
        consent = self.service.account_preferences(identity)
        self.assertEqual((consent["model_improvement_consent"], consent["version"]), ("not_asked", 0))
        granted = self.service.set_model_improvement_consent(identity, "granted", 0)
        self.assertEqual((granted["model_improvement_consent"], granted["version"], granted["training_started"]), ("granted", 1, False))
        withdrawn = self.service.set_model_improvement_consent(identity, "withdrawn", 1)
        self.assertEqual(withdrawn["model_improvement_consent"], "withdrawn")
        export = self.service.create_account_export(identity)
        self.assertEqual(export["scope"], "account_owned_records_and_files_in_member_workspaces")
        self.assertEqual(export["expires_at"], self.service._stamp(self.clock.value + timedelta(hours=24)))
        download = self.service.account_export_download(identity, export["export_id"])
        self.assertEqual(download["sha256"], export["sha256"])
        with zipfile.ZipFile(download["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            account = json.loads(archive.read("records/account.json"))
            materials = json.loads(archive.read("records/materials.json"))
            media_transcriptions = json.loads(archive.read("records/media_transcriptions.json"))
            media_transcription_chunks = json.loads(archive.read("records/media_transcription_chunks.json"))
            query_plans = json.loads(archive.read("records/acquisition_query_plans.json"))
            query_paths = json.loads(archive.read("records/acquisition_query_paths.json"))
            browser_operations = json.loads(archive.read("records/browser_session_operations.json"))
            self.assertEqual(len(browser_operations), 1)
            self.assertIsNone(browser_operations[0]["preview_json"])
            self.assertEqual(browser_operations[0]["preview_expires_at"], preview_expiry)
            self.assertEqual(json.loads(browser_operations[0]["result_json"])["capture_ref"]["capture_id"], "account-frozen-preview")
            self.assertNotIn("FROZEN_BROWSER_BODY_NOT_FOR_ACCOUNT_EXPORT", json.dumps(browser_operations))
            self.assertEqual(manifest["excluded"], [
                "passwords_and_verification_codes", "session_tokens",
                "external_platform_browser_authentication_state", "browser_page_query_preview_bodies", "browser_context_preview_bodies",
            ])
            self.assertNotIn("password_hash", json.dumps(account))
            self.assertNotIn("email_normalized", json.dumps(account))
            self.assertIn("files/materials/owned-material-export/source.txt", {item.filename for item in archive.infolist()})
            self.assertEqual(archive.read("files/materials/owned-material-export/source.txt"), original)
            self.assertEqual(materials[0]["relative_path"], "files/materials/owned-material-export/source.txt")
            self.assertEqual([item["job_id"] for item in media_transcriptions], ["owned-media-asr-export"])
            self.assertIn("Unreviewed candidate", json.dumps(media_transcription_chunks, ensure_ascii=False))
            self.assertEqual([item["query_id"] for item in query_plans], [query_id])
            self.assertEqual([item["path_id"] for item in query_paths], [path_id])
            self.assertEqual(query_paths[0]["status"], "unavailable")
            self.assertEqual(query_paths[0]["stop_reason"], "BROWSER_PREVIEW_NOT_RESTORED")
            self.assertEqual(query_paths[0]["preview_expires_at"], preview_expiry)
            query_path_result = json.loads(query_paths[0]["result_json"])
            self.assertEqual(query_path_result["source_ref"]["session_id"], browser_session_id)
            self.assertEqual(query_path_result["source_ref"]["snapshot_sha256"], "a" * 64)
            self.assertEqual(query_path_result["results"], [])
            exported_query_json = json.dumps([query_plans, query_paths], ensure_ascii=False)
            self.assertNotIn("UNADMITTED_BROWSER_PREVIEW_BODY", exported_query_json)
            self.assertNotIn("QUERY_PREVIEW_SECRET", exported_query_json)
            self.assertNotIn("QUERY_URL_SECRET", exported_query_json)
            persisted = self.store.row(
                "SELECT result_json FROM acquisition_query_paths WHERE path_id=?", (path_id,),
            )
            self.assertIn("UNADMITTED_BROWSER_PREVIEW_BODY", persisted["result_json"])
        download["path"].write_bytes(b"tampered archive")
        with self.assertRaises(AccountError) as changed:
            self.service.account_export_download(identity, export["export_id"])
        self.assertEqual(changed.exception.code, "ACCOUNT_EXPORT_FILE_CHANGED")

    def test_shared_account_purge_erases_unadopted_media_transcript_candidates(self):
        _, identity = self.create_local_identity()
        user_id = identity["user"]["id"]
        workspace_id = identity["workspace"]["id"]
        successor_id = "media-purge-successor"
        now = self.service._stamp()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id, name, created_at) VALUES(?, 'Successor', ?)",
                (successor_id, now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, successor_id, now),
            )
        identifiers = self.create_local_work(identity)
        project_id = "media-project-with-unadopted-asr"
        transcription_id = "media-unadopted-asr-job"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO media_projects(id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by) VALUES(?, ?, ?, ?, 'Private candidate', 'podcast', 1, ?, ?, ?)",
                (project_id, workspace_id, user_id, identifiers["work"], now, now, user_id),
            )
            connection.execute(
                "INSERT INTO media_project_versions(project_id, workspace_id, version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) VALUES(?, ?, 1, 'create', '{}', '[]', '[]', ?, ?)",
                (project_id, workspace_id, now, user_id),
            )
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, created_at, updated_at) VALUES(?, ?, ?, ?, 'media_transcribe', 'completed', ?, ?, ?, ?, ?)",
                (transcription_id, workspace_id, user_id, identifiers["work"],
                 json.dumps({"private_candidate": "erase this after account deletion"}),
                 "3" * 64, "media-purge-asr", now, now),
            )
            connection.execute(
                "INSERT INTO media_transcriptions(job_id, workspace_id, project_id, project_version, source_asset_id, source_material_id, source_sha256, audio_stream_index, audio_channel_index, source_sample_rate, task, normalized_sample_rate, model_version, model_sha256, result_metadata_json, created_at, updated_at) VALUES(?, ?, ?, 1, 'MEDIA-private', ?, ?, 0, 0, 16000, 'transcribe', 16000, '20250625', ?, '{}', ?, ?)",
                (transcription_id, workspace_id, project_id, identifiers["material"], "4" * 64,
                 "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e", now, now),
            )
            connection.execute(
                "INSERT INTO media_transcription_chunks(job_id, chunk_index, normalized_start_sample, normalized_end_sample, source_start_sample, source_end_sample, language, text, segments_json, words_json, created_at) VALUES(?, 0, 0, 16000, 0, 16000, 'en', 'private transcript text', '[]', ?, ?)",
                (transcription_id, json.dumps([{"word_id": "asr:private", "text": "erase this phrase"}], ensure_ascii=False), now),
            )
        with self.store.transaction() as connection:
            result = self.service._purge_account_owned_data_tx(connection, user_id)
        self.assertTrue(result["confirmed"])
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM media_transcriptions WHERE job_id = ?", (transcription_id,))["count"],
            0,
        )
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM media_transcription_chunks WHERE job_id = ?", (transcription_id,))["count"],
            0,
        )
        retained_job = self.store.row("SELECT status, payload_json FROM jobs WHERE id = ?", (transcription_id,))
        self.assertEqual(retained_job["status"], "completed")
        self.assertEqual(retained_job["payload_json"], "{}")
        self.assertEqual(
            self.store.row("SELECT owner_user_id FROM media_projects WHERE id = ?", (project_id,))["owner_user_id"],
            successor_id,
        )

    def test_deletion_purge_waits_for_data_owner_and_workspace_resolution(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        service = AccountService(
            self.store,
            mailer=self.mailer,
            clock=self.clock,
            user_data_purger=lambda connection, user_id: False,
        )
        service.set_model_improvement_consent(identity, "declined", 0)
        deletion_request = service.request_account_deletion(identity)
        export_id = deletion_request["export_reference"]
        export_path = self.store.root / self.store.row("SELECT relative_path FROM account_exports WHERE id = ?", (export_id,))["relative_path"]
        code = self.mailer.latest_code("delete_confirm", "person@example.test")
        service.confirm_account_deletion(deletion_request["reference"], code)
        later = self.clock.value + timedelta(days=31)
        self.assertEqual(service.purge_due_deletions(now=later)["state"], "blocked")
        self.assertIsNotNone(self.store.row("SELECT id FROM users WHERE id = ?", (verified["user"]["id"],)))

        service.user_data_purger = lambda connection, user_id: False
        self.assertEqual(service.purge_due_deletions(now=later)["blocked"], 1)
        self.assertIsNotNone(self.store.row("SELECT id FROM users WHERE id = ?", (verified["user"]["id"],)))

        def remove_personal_workspace(connection, user_id):
            workspaces = [row["workspace_id"] for row in connection.execute("SELECT workspace_id FROM memberships WHERE user_id = ?", (user_id,)).fetchall()]
            connection.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
            for workspace_id in workspaces:
                connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
            return True

        service.user_data_purger = remove_personal_workspace
        outcome = service.purge_due_deletions(now=later)
        self.assertEqual((outcome["completed"], outcome["blocked"]), (1, 0))
        self.assertEqual(self.store.row("SELECT name FROM users WHERE id = ?", (verified["user"]["id"],))["name"], "已删除用户")
        tombstone = self.store.row("SELECT status, completion_code, export_reference FROM account_deletions WHERE completion_code = 'ACCOUNT_DATA_PURGED'")
        self.assertEqual(tombstone["status"], "completed")
        self.assertIsNone(tombstone["export_reference"])
        self.assertFalse(export_path.exists())
        self.assertIsNone(self.store.row("SELECT user_id FROM account_preferences WHERE user_id = ?", (verified["user"]["id"],)))

    def test_deletion_requires_recent_session_and_verified_email(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        service = AccountService(
            self.store,
            mailer=self.mailer,
            clock=self.clock,
            account_exporter=lambda user_id: {"state": "completed", "artifact_id": "EXP-TEST-3"},
        )
        self.clock.advance(minutes=16)
        with self.assertRaises(AccountError) as recent:
            service.request_account_deletion(identity)
        self.assertEqual(recent.exception.code, "RECENT_AUTH_REQUIRED")

    def test_account_deletion_preflight_requires_workspace_and_commission_handoffs(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        user_id = verified["user"]["id"]
        workspace_id = identity["workspace"]["id"]
        successor = "account-delete-successor"
        now = self.service._stamp()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, 'Successor', ?)", (successor, now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'editor', 1, ?)", (workspace_id, successor, now))
        with self.assertRaises(AccountError) as last_workspace_owner:
            self.service.request_account_deletion(identity)
        self.assertEqual(last_workspace_owner.exception.code, "ACCOUNT_DELETION_BLOCKED")
        self.assertEqual(last_workspace_owner.exception.details["blockers"][0]["kind"], "last_workspace_owner")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_exports WHERE user_id = ?", (user_id,))["count"], 0)

        with self.store.transaction() as connection:
            connection.execute("UPDATE memberships SET role = 'owner' WHERE workspace_id = ? AND user_id = ?", (workspace_id, successor))
            connection.execute(
                "INSERT INTO client_commissions(id, workspace_id, name, scope_json, status, version, created_by, created_at, updated_at) VALUES('delete-commission', ?, 'Private client', '{}', 'active', 1, ?, ?, ?)",
                (workspace_id, user_id, now, now),
            )
            connection.execute("INSERT INTO commission_members(commission_id, user_id, role, active) VALUES('delete-commission', ?, 'owner', 1)", (user_id,))
            connection.execute("INSERT INTO commission_members(commission_id, user_id, role, active) VALUES('delete-commission', ?, 'editor', 1)", (successor,))
        with self.assertRaises(AccountError) as last_commission_owner:
            self.service.request_account_deletion(identity)
        self.assertEqual(last_commission_owner.exception.code, "ACCOUNT_DELETION_BLOCKED")
        self.assertEqual(last_commission_owner.exception.details["blockers"][0]["kind"], "commission_ownership_transfer")

        with self.store.transaction() as connection:
            connection.execute("UPDATE commission_members SET role = 'owner' WHERE commission_id = 'delete-commission' AND user_id = ?", (successor,))
        requested = self.service.request_account_deletion(identity)
        self.assertEqual(requested["state"], "deletion_confirmation_required")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_exports WHERE user_id = ?", (user_id,))["count"], 1)

    def test_confirmed_deletion_revokes_sessions_stops_jobs_and_closes_saved_browser_state(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        user_id = verified["user"]["id"]
        workspace_id = identity["workspace"]["id"]
        now = self.service._stamp()
        membership = self.store.row(
            "SELECT created_at FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO browser_sessions(
                       id, workspace_id, owner_user_id, membership_created_at, authorized_by_session_id,
                       engine, status, version, current_url, current_title, created_at, updated_at,
                       state_ciphertext, state_protection
                   ) VALUES('delete-browser', ?, ?, ?, ?, 'chrome', 'suspended', 1, 'https://example.test', 'Saved', ?, ?, X'0102', 'test-protection')""",
                (workspace_id, user_id, membership["created_at"], identity["session_id"], now, now),
            )
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, kind, status, payload_json, request_hash, idempotency_key, created_at, updated_at) VALUES('delete-job', ?, ?, 'material_comparison', 'queued', '{}', ?, 'delete-key', ?, ?)",
                (workspace_id, user_id, "0" * 64, now, now),
            )
            connection.execute(
                """INSERT INTO monitor_rules(
                       id, workspace_id, owner_user_id, title, scope, work_id, follow_source_links,
                       keywords_json, exclude_keywords_json, frequency_seconds, stop_at,
                       per_run_limit, status, version, next_run_at, created_at, updated_at
                   ) VALUES('delete-monitor', ?, ?, 'Delete test', '', NULL, 0, '[]', '[]',
                            3600, NULL, 10, 'enabled', 1, ?, ?, ?)""",
                (workspace_id, user_id, now, now, now),
            )
        closed = []
        self.service.browser_session_closer = lambda engine, session_id: closed.append((engine, session_id))
        request = self.service.request_account_deletion(identity)
        result = self.service.confirm_account_deletion(
            request["reference"], self.mailer.latest_code("delete_confirm", "person@example.test")
        )
        self.assertEqual(result["state"], "deletion_scheduled")
        self.assertEqual(result["cancelled_jobs"], 1)
        self.assertEqual(result["platform_sessions_closed"], 1)
        self.assertEqual(result["paused_monitors"], 1)
        self.assertEqual(closed, [("chrome", "delete-browser")])
        browser = self.store.row("SELECT status, state_ciphertext, state_protection, last_error_code FROM browser_sessions WHERE id = 'delete-browser'")
        self.assertEqual((browser["status"], browser["state_ciphertext"], browser["state_protection"], browser["last_error_code"]), ("closed", None, None, "ACCOUNT_DELETION_CLOSED"))
        job = self.store.row("SELECT status, lease_generation, cancel_requested FROM jobs WHERE id = 'delete-job'")
        self.assertEqual((job["status"], job["lease_generation"], job["cancel_requested"]), ("cancelled", 1, 1))
        monitor = self.store.row("SELECT status, version, next_run_at FROM monitor_rules WHERE id = 'delete-monitor'")
        self.assertEqual((monitor["status"], monitor["version"], monitor["next_run_at"]), ("paused", 2, None))
        monitor_history = self.store.row("SELECT operation, status FROM monitor_versions WHERE monitor_id = 'delete-monitor' AND version = 2")
        self.assertEqual((monitor_history["operation"], monitor_history["status"]), ("account_deletion_scheduled", "paused"))
        self.assertIsNone(authenticate(self.store, login["token"]))

    def test_default_deletion_purger_removes_private_workspace_and_keeps_only_anonymous_author(self):
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        now = self.service._stamp()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO local_identity(singleton, user_id, workspace_id, created_at) VALUES(1, ?, ?, ?)",
                (user_id, workspace_id, now),
            )
        identifiers = self.create_local_work(identity)
        source_path = self.store.root / "objects" / "local.txt"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = b"private account source to remove"
        source_path.write_bytes(source_bytes)
        client_export_id = "private-client-export"
        client_export_path = f"objects/{workspace_id}/exports/{client_export_id}.docx"
        client_export_file = self.store.root / client_export_path
        client_export_file.parent.mkdir(parents=True, exist_ok=True)
        client_export_bytes = b"private client export"
        client_export_file.write_bytes(client_export_bytes)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET sha256 = ?, size_bytes = ? WHERE id = ?",
                (hashlib.sha256(source_bytes).hexdigest(), len(source_bytes), identifiers["material"]),
            )
            connection.execute(
                "INSERT INTO exports(id, workspace_id, artifact_id, version, format, idempotency_key, request_hash, status, relative_path, sha256, size_bytes, created_at, updated_at) VALUES(?, ?, ?, 1, 'docx', 'private-export', ?, 'completed', ?, ?, ?, ?, ?)",
                (client_export_id, workspace_id, identifiers["artifact"], "1" * 64, client_export_path, hashlib.sha256(client_export_bytes).hexdigest(), len(client_export_bytes), now, now),
            )
            connection.execute(
                "INSERT INTO business_export_views(export_id, audience, view_json) VALUES(?, 'client', ?)",
                (client_export_id, '{"approved_sections":[]}'),
            )
        request = self.service.request_account_deletion(identity)
        account_export_path = self.store.root / self.store.row(
            "SELECT relative_path FROM account_exports WHERE id = ?", (request["export_reference"],)
        )["relative_path"]
        with zipfile.ZipFile(account_export_path) as archive:
            export_views = json.loads(archive.read("records/business_export_views.json"))
            self.assertEqual(export_views[0]["export_id"], client_export_id)
        code = self.mailer.latest_code("delete_confirm", "person@example.test")
        scheduled = self.service.confirm_account_deletion(request["reference"], code)
        self.assertEqual(scheduled["state"], "deletion_scheduled")
        self.assertIsNone(authenticate(self.store, login["token"]))
        bootstrap = self.store.root / "bootstrap" / "local.capability"
        issue_local_capability(self.store, bootstrap)
        with self.assertRaises(AuthenticationError) as local_login_blocked:
            exchange_local_capability(self.store, bootstrap.read_text(encoding="ascii"))
        self.assertEqual(local_login_blocked.exception.code, "ACCOUNT_DELETION_PENDING")
        deletion_time = self.clock.value + timedelta(days=31)
        original_unlink = self.service._unlink_account_file
        self.service._unlink_account_file = lambda _path: False
        pending = self.service.purge_due_deletions(now=deletion_time)
        self.assertEqual((pending["completed"], pending["blocked"]), (0, 1))
        self.assertEqual(
            self.store.row("SELECT completion_code FROM account_deletions WHERE user_id = ?", (user_id,))["completion_code"],
            "ACCOUNT_DATA_PURGED_FILES_PENDING",
        )
        self.assertTrue(source_path.exists())
        self.service._unlink_account_file = original_unlink
        completion = self.service.purge_due_deletions(now=deletion_time)
        self.assertEqual((completion["completed"], completion["blocked"]), (1, 0))
        self.assertEqual(self.store.row("SELECT name FROM users WHERE id = ?", (user_id,))["name"], "已删除用户")
        self.assertIsNone(self.store.row("SELECT user_id FROM account_emails WHERE user_id = ?", (user_id,)))
        self.assertIsNone(self.store.row("SELECT user_id FROM account_password_credentials WHERE user_id = ?", (user_id,)))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM workspaces")["count"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM works")["count"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)
        self.assertFalse(source_path.exists())
        self.assertFalse(client_export_file.exists())
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM business_export_views")["count"], 0)
        self.assertEqual(self.store.row("SELECT status, completion_code FROM account_deletions")["status"], "completed")
        self.assertEqual(self.store.row("SELECT status, completion_code FROM account_deletions")["completion_code"], "ACCOUNT_DATA_PURGED")

    def test_shared_workspace_deletion_transfers_current_owners_and_erases_public_source_content(self):
        from server.oevira.acquisition_queries import install_query_schema
        from server.oevira.intelligence_models import install_intelligence_model_schema
        from server.oevira.intelligence_skills import install_intelligence_skill_schema

        install_query_schema(self.store)
        install_intelligence_model_schema(self.store)
        install_intelligence_skill_schema(self.store)
        verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        user_id = verified["user"]["id"]
        workspace_id = identity["workspace"]["id"]
        now = self.service._stamp()
        successor_id = "surviving-workspace-owner"
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, 'Successor', ?)", (successor_id, now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)", (workspace_id, successor_id, now))
            github_connection_id = "delete-owner-github-connection"
            mcp_connection_id = "delete-owner-mcp-connection"
            model_connection_id = "delete-owner-model-connection"
            connection.execute(
                "INSERT INTO intelligence_github_connections(connection_id,workspace_id,owner_user_id,name,status,revision,"
                "credential_ciphertext,credential_protection,provider_login,verified_at,validation_status,created_at,updated_at) "
                "VALUES(?,?,?,'Private GitHub','active',1,?,'test-protection','private-login',?,'valid',?,?)",
                (github_connection_id, workspace_id, user_id, b"GH_CREDENTIAL_SECRET", now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_mcp_connections(connection_id,workspace_id,owner_user_id,name,endpoint,status,revision,"
                "credential_ciphertext,credential_protection,protocol_version,server_info_json,capabilities_json,"
                "tool_list_sha256,tool_count,authorized_use_confirmed,validation_status,verified_at,created_at,updated_at) "
                "VALUES(?,?,?,'Private MCP','https://mcp.example.test/','active',1,?,'test-protection',"
                "'2025-11-25','{}','{}',?,1,1,'valid',?,?,?)",
                (mcp_connection_id, workspace_id, user_id, b"MCP_CREDENTIAL_SECRET", "a" * 64, now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_model_connections(connection_id,workspace_id,owner_user_id,name,base_url,"
                "recipient_id,protocol,token_field,model_id,credential_ciphertext,credential_protection,allow_scoped_data,"
                "is_selected,status,revision,created_at,updated_at,created_by) "
                "VALUES(?,?,?,'Private model','https://model.example.test/v1','model.example.test',"
                "'openai_responses','max_tokens','private-model',?,'test-protection',1,1,'configured','model-revision',?,?,?)",
                (model_connection_id, workspace_id, user_id, b"MODEL_CREDENTIAL_SECRET", now, now, user_id),
            )
            connection.execute(
                "INSERT INTO intelligence_model_admissions(admission_id,workspace_id,owner_user_id,connection_id,"
                "selection_revision,purpose,recipient_id,source_ref_sha256,state,created_at,expires_at) "
                "VALUES('delete-owner-model-admission',?,?,?,'model-revision','inference','model.example.test',?,'source_authorized',?,?)",
                (workspace_id, user_id, model_connection_id, "b" * 64, now, self.service._stamp(self.clock.value + timedelta(hours=1))),
            )
        identifiers = self.create_local_work(identity)
        query_id = "deleting-account-query-plan"
        query_path_id = "deleting-account-browser-path"
        successor_query_id = "successor-account-query-plan"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO acquisition_query_plans(query_id,workspace_id,owner_user_id,work_id,query_revision,"
                "idempotency_key,request_hash,request_json,created_at,updated_at) VALUES(?,?,?, ?,1,?,?,?, ?,?)",
                (query_id, workspace_id, user_id, identifiers["work"], "deleting-account-query-key",
                 "d" * 64, json.dumps({"query": "private query"}), now, now),
            )
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,result_json,status,"
                "coverage_json,created_at,updated_at) VALUES(?,?,?,0,NULL,'browser_page','selected_browser_page',"
                "'{}','{}','[]',?,'complete','{}',?,?)",
                (query_path_id, query_id, workspace_id,
                 json.dumps({"results": [{"quote": "UNADMITTED_DELETE_PRIVATE_PREVIEW"}]}), now, now),
            )
            connection.execute(
                "INSERT INTO acquisition_query_plans(query_id,workspace_id,owner_user_id,work_id,query_revision,"
                "idempotency_key,request_hash,request_json,created_at,updated_at) VALUES(?,?,?, ?,1,?,?,?, ?,?)",
                (successor_query_id, workspace_id, successor_id, identifiers["work"], "successor-query-key",
                 "e" * 64, json.dumps({"query": "successor query"}), now, now),
            )
        source_path = self.store.root / "objects" / "local.txt"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = b"shared workspace source retained for the successor"
        source_path.write_bytes(source_bytes)
        content = {
            "title": "Public source title",
            "summary": "Public source summary",
            "body": "Public source body to erase",
            "category": None,
            "tags": [],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False, "redistribute": False, "training": False, "attribution": "Creator", "expires_at": None},
        }
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET sha256 = ?, size_bytes = ? WHERE id = ?",
                (hashlib.sha256(source_bytes).hexdigest(), len(source_bytes), identifiers["material"]),
            )
            connection.execute(
                "INSERT INTO publications(id, workspace_id, author_id, artifact_id, artifact_version, source_kind, source_id, source_version, kind, version, status, visibility, content_json, created_at, updated_at) VALUES('published-source', ?, ?, ?, 1, 'artifact', ?, 1, 'work', 2, 'published', 'public', ?, ?, ?)",
                (workspace_id, user_id, identifiers["artifact"], identifiers["artifact"], json.dumps(content), now, now),
            )
            licensed_template = {
                "title": "Successor template",
                "summary": "Authorized template copy remains",
                "body": "Template body from another creator",
                "category": None,
                "tags": [],
                "license": {"view": True, "download": True, "adapt": True, "commercial": False, "redistribute": False, "training": False, "attribution": "Successor", "expires_at": None},
            }
            connection.execute(
                "INSERT INTO publications(id, workspace_id, author_id, artifact_id, artifact_version, source_kind, source_id, source_version, kind, version, status, visibility, content_json, created_at, updated_at) VALUES('successor-template', ?, ?, ?, 1, 'artifact', ?, 1, 'template', 1, 'published', 'public', ?, ?, ?)",
                (workspace_id, successor_id, identifiers["artifact"], identifiers["artifact"], json.dumps(licensed_template), now, now),
            )
            connection.execute(
                "INSERT INTO publication_versions(publication_id, version, snapshot_json, created_by, created_at) VALUES('successor-template', 1, ?, ?, ?)",
                (json.dumps({"content": licensed_template}), successor_id, now),
            )
            connection.execute(
                "INSERT INTO publication_versions(publication_id, version, snapshot_json, created_by, created_at) VALUES('published-source', 1, ?, ?, ?)",
                (json.dumps({"content": content}), user_id, now),
            )
            connection.execute(
                "INSERT INTO publication_reviews(id, publication_id, publication_version, content_hash, decision, reason, reviewer_id, created_at) VALUES('review-1', 'published-source', 2, ?, 'approved', 'Reviewed', ?, ?)",
                ("0" * 64, user_id, now),
            )
            connection.execute(
                "INSERT INTO publication_releases(id, publication_id, publication_version, review_id, author_id, kind, visibility, snapshot_json, content_hash, share_token_hash, published_at, withdrawn_at) VALUES('release-1', 'published-source', 2, 'review-1', ?, 'work', 'public', ?, ?, NULL, ?, NULL)",
                (user_id, json.dumps(content), "1" * 64, now),
            )
            connection.execute(
                "INSERT INTO publication_reviews(id, publication_id, publication_version, content_hash, decision, reason, reviewer_id, created_at) VALUES('review-other', 'successor-template', 1, ?, 'approved', 'Reviewed', ?, ?)",
                ("2" * 64, successor_id, now),
            )
            connection.execute(
                "INSERT INTO publication_releases(id, publication_id, publication_version, review_id, author_id, kind, visibility, snapshot_json, content_hash, share_token_hash, published_at, withdrawn_at) VALUES('release-other', 'successor-template', 1, 'review-other', ?, 'template', 'public', ?, ?, NULL, ?, NULL)",
                (successor_id, json.dumps(licensed_template), "3" * 64, now),
            )
            connection.execute("INSERT INTO release_comments(id, release_id, author_id, body, anchor_json, created_at) VALUES('comment-own', 'release-1', ?, 'My public comment', '{}', ?)", (user_id, now))
            connection.execute("INSERT INTO release_comments(id, release_id, author_id, body, anchor_json, created_at) VALUES('comment-other', 'release-1', ?, 'Another comment', '{}', ?)", (successor_id, now))
            connection.execute("INSERT INTO community_items(user_id, release_id, kind, position, updated_at) VALUES(?, 'release-1', 'collection', 0, ?)", (user_id, now))
            connection.execute("INSERT INTO community_follows(follower_id, creator_id, created_at) VALUES(?, ?, ?)", (user_id, successor_id, now))

        request = self.service.request_account_deletion(identity)
        code = self.mailer.latest_code("delete_confirm", "person@example.test")
        self.service.confirm_account_deletion(request["reference"], code)
        outcome = self.service.purge_due_deletions(now=self.clock.value + timedelta(days=31))
        self.assertEqual((outcome["completed"], outcome["blocked"]), (1, 0))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM workspaces")["count"], 1)
        self.assertEqual(self.store.row("SELECT owner_user_id FROM works WHERE id = ?", (identifiers["work"],))["owner_user_id"], successor_id)
        self.assertEqual(self.store.row("SELECT owner_user_id FROM artifacts WHERE id = ?", (identifiers["artifact"],))["owner_user_id"], successor_id)
        self.assertTrue(source_path.exists())
        self.assertEqual(self.store.row("SELECT created_by FROM artifact_versions WHERE artifact_id = ? AND version = 1", (identifiers["artifact"],))["created_by"], user_id)
        self.assertEqual(self.store.row("SELECT name FROM users WHERE id = ?", (user_id,))["name"], "已删除用户")
        publication = self.store.row("SELECT author_id, status, content_json FROM publications WHERE id = 'published-source'")
        self.assertEqual((publication["author_id"], publication["status"]), (user_id, "withdrawn"))
        self.assertNotIn("Public source body to erase", publication["content_json"])
        release = self.store.row("SELECT author_id, withdrawn_at, snapshot_json FROM publication_releases WHERE id = 'release-1'")
        self.assertEqual((release["author_id"], release["withdrawn_at"] is not None), (user_id, True))
        self.assertNotIn("Public source body to erase", release["snapshot_json"])
        retained_template = self.store.row("SELECT p.author_id, p.status, r.snapshot_json, r.withdrawn_at FROM publications p JOIN publication_releases r ON r.publication_id=p.id WHERE p.id='successor-template' AND r.id='release-other'")
        self.assertEqual((retained_template["author_id"], retained_template["status"], retained_template["withdrawn_at"]), (successor_id, "published", None))
        self.assertIn("Template body from another creator", retained_template["snapshot_json"])
        own_comment = self.store.row("SELECT body, withdrawn_at FROM release_comments WHERE id = 'comment-own'")
        other_comment = self.store.row("SELECT body, withdrawn_at FROM release_comments WHERE id = 'comment-other'")
        self.assertEqual((own_comment["body"], own_comment["withdrawn_at"] is not None), ("", True))
        self.assertEqual((other_comment["body"], other_comment["withdrawn_at"]), ("Another comment", None))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM community_items WHERE user_id = ?", (user_id,))["count"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM community_follows WHERE follower_id = ?", (user_id,))["count"], 0)
        self.assertEqual(self.store.row(
            "SELECT COUNT(*) AS count FROM acquisition_query_plans WHERE owner_user_id=?", (user_id,),
        )["count"], 0)
        self.assertIsNone(self.store.row(
            "SELECT 1 FROM acquisition_query_paths WHERE path_id=?", (query_path_id,),
        ))
        self.assertIsNotNone(self.store.row(
            "SELECT 1 FROM acquisition_query_plans WHERE query_id=? AND owner_user_id=?",
            (successor_query_id, successor_id),
        ))
        for table, connection_id in (
            ("intelligence_github_connections", github_connection_id),
            ("intelligence_mcp_connections", mcp_connection_id),
        ):
            credential = self.store.row(
                f"SELECT status,credential_ciphertext,credential_protection FROM {table} WHERE connection_id=?",
                (connection_id,),
            )
            self.assertEqual((credential["status"], credential["credential_ciphertext"], credential["credential_protection"]),
                             ("revoked", None, None))
        self.assertIsNone(self.store.row(
            "SELECT 1 FROM intelligence_model_connections WHERE connection_id=?", (model_connection_id,),
        ))
        self.assertEqual(self.store.row(
            "SELECT COUNT(*) AS count FROM intelligence_model_admissions WHERE connection_id=?",
            (model_connection_id,),
        )["count"], 0)

    def test_workspace_invitation_resend_accept_role_transfer_and_leave_use_real_memberships(self):
        owner, owner_password = self.create_verified_account("owner@example.test", "Owner Correct Horse 2026!")
        owner_login = self.service.login_password("owner@example.test", owner_password)
        owner_identity = authenticate(self.store, owner_login["token"])
        service = AccountWorkspaceMemberships(self.service)
        workspace_id = owner_identity["workspace"]["id"]
        listed = service.workspaces(owner_identity)["workspaces"]
        self.assertTrue(any(item["id"] == workspace_id and item["current"] for item in listed))

        with self.assertRaises(AccountError) as owner_invitation:
            service.invite(owner_identity, workspace_id, "member@example.test", "owner")
        self.assertEqual(owner_invitation.exception.code, "INVALID_INPUT")
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM account_workspace_invitations")["count"],
            0,
        )

        invite_method = self.mailer.send_invitation
        self.mailer.send_invitation = lambda _email, _token, _expires, _workspace: False
        with self.assertRaises(AccountError) as failed_delivery:
            service.invite(owner_identity, workspace_id, "member@example.test", "viewer")
        self.assertEqual(failed_delivery.exception.code, "INVITATION_DELIVERY_FAILED")
        failed_id = failed_delivery.exception.details["invitation_id"]
        self.mailer.send_invitation = invite_method
        resent = service.resend_invitation(owner_identity, workspace_id, failed_id)
        invitation_id = resent["invitation"]["id"]
        token = self.mailer.latest_invitation_token("member@example.test")
        self.assertNotIn("token", json.dumps(resent))
        with self.assertRaises(AccountError) as duplicate:
            service.invite(owner_identity, workspace_id, "MEMBER@example.test", "viewer")
        self.assertEqual(duplicate.exception.code, "INVITATION_ALREADY_PENDING")

        invitee, invitee_password = self.create_verified_account(
            "member@example.test", "Member Correct Horse 2026!"
        )
        invitee_login = self.service.login_password("member@example.test", invitee_password)
        invitee_identity = authenticate(self.store, invitee_login["token"])
        wrong_email, wrong_password = self.create_verified_account(
            "wrong@example.test", "Wrong Correct Horse 2026!"
        )
        wrong_login = self.service.login_password("wrong@example.test", wrong_password)
        wrong_identity = authenticate(self.store, wrong_login["token"])
        with self.assertRaises(AccountError) as email_mismatch:
            service.accept_invitation(wrong_identity, token)
        self.assertEqual(email_mismatch.exception.code, "INVITATION_EMAIL_MISMATCH")
        accepted = service.accept_invitation(invitee_identity, token)
        self.assertEqual(accepted["state"], "workspace_invitation_accepted")
        self.assertEqual(accepted["workspace"]["id"], workspace_id)
        member_identity = authenticate(self.store, accepted["token"])
        self.assertEqual(member_identity["role"], "viewer")
        member_list = service.members(member_identity, workspace_id)["members"]
        self.assertEqual({item["name"] for item in member_list}, {owner["user"]["name"], invitee["user"]["name"]})
        invite_list = service.invitations(owner_identity, workspace_id)["invitations"]
        self.assertEqual(
            {(item["id"], item["status"]) for item in invite_list if item["id"] in {failed_id, invitation_id}},
            {(failed_id, "revoked"), (invitation_id, "accepted")},
        )
        with self.assertRaises(AccountError) as viewer_denied:
            service.invitations(member_identity, workspace_id)
        self.assertEqual(viewer_denied.exception.code, "WORKSPACE_OWNER_REQUIRED")

        changed = service.set_member_role(
            owner_identity, workspace_id, invitee["user"]["id"], "editor", "viewer"
        )
        self.assertEqual(changed["state"], "membership_role_changed")
        with self.assertRaises(AccountError) as owner_role_rejected:
            service.set_member_role(
                owner_identity, workspace_id, invitee["user"]["id"], "owner", "editor"
            )
        self.assertEqual(owner_role_rejected.exception.code, "INVALID_INPUT")
        transferred = service.transfer_workspace_owner(
            owner_identity,
            workspace_id,
            invitee["user"]["id"],
            owner["user"]["id"],
        )
        self.assertEqual(transferred["owner_user_id"], invitee["user"]["id"])
        member_identity = authenticate(self.store, accepted["token"])
        with self.assertRaises(AccountError) as last_owner:
            service.remove_member(member_identity, workspace_id, None, leaving=True)
        self.assertEqual(last_owner.exception.code, "LAST_WORKSPACE_OWNER")

        service.transfer_workspace_owner(
            member_identity,
            workspace_id,
            owner["user"]["id"],
            invitee["user"]["id"],
        )
        member_identity = authenticate(self.store, accepted["token"])
        left = service.remove_member(member_identity, workspace_id, None, leaving=True)
        self.assertEqual(left["state"], "workspace_left")
        self.assertNotEqual(left["workspace"]["id"], workspace_id)
        self.assertIsNone(
            self.store.row(
                "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id = ? AND active = 1",
                (workspace_id, invitee["user"]["id"]),
            )
        )
        self.assertEqual(
            self.store.row(
                "SELECT active FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, owner["user"]["id"]),
            )["active"],
            1,
        )

    def test_membership_revocation_fences_owned_provider_credentials_and_skills(self):
        from server.oevira.intelligence_models import install_intelligence_model_schema
        from server.oevira.intelligence_skills import IntelligenceSkillStore, install_intelligence_skill_schema
        from server.oevira.worker import install_worker_schema
        from server.oevira.workspace_data import install_workspace_data_schema

        install_intelligence_model_schema(self.store)
        install_intelligence_skill_schema(self.store)
        install_worker_schema(self.store)
        install_workspace_data_schema(self.store)
        owner, owner_password = self.create_verified_account("owner@example.test", "Owner Correct Horse 2026!")
        member, _member_password = self.create_verified_account("member@example.test", "Member Correct Horse 2026!")
        owner_identity = authenticate(self.store, self.service.login_password(
            "owner@example.test", owner_password,
        )["token"])
        workspace_id = owner_identity["workspace"]["id"]
        now = self.service._stamp()
        github_id = "membership-revoke-github"
        mcp_id = "membership-revoke-mcp"
        model_id = "membership-revoke-model"
        github_skill_id = "membership-revoke-github-skill"
        mcp_skill_id = "membership-revoke-mcp-skill"
        backup_job_id = "membership-revoke-backup-job"
        backup_operation_id = "membership-revoke-backup-operation"
        protected_path = f"objects/{workspace_id}/materials/member-original/original"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?,'editor',1,?)",
                (workspace_id, member["user"]["id"], now),
            )
            connection.execute(
                "INSERT INTO intelligence_github_connections(connection_id,workspace_id,owner_user_id,name,status,revision,"
                "credential_ciphertext,credential_protection,provider_login,verified_at,validation_status,created_at,updated_at) "
                "VALUES(?,?,?,'Member GitHub','active',1,?,'test-protection','member-login',?,'valid',?,?)",
                (github_id, workspace_id, member["user"]["id"], b"GH_MEMBER_SECRET", now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_mcp_connections(connection_id,workspace_id,owner_user_id,name,endpoint,status,revision,"
                "credential_ciphertext,credential_protection,protocol_version,server_info_json,capabilities_json,"
                "tool_list_sha256,tool_count,authorized_use_confirmed,validation_status,verified_at,created_at,updated_at) "
                "VALUES(?,?,?,'Member MCP','https://mcp.example.test/','active',1,?,'test-protection',"
                "'2025-11-25','{}','{}',?,1,1,'valid',?,?,?)",
                (mcp_id, workspace_id, member["user"]["id"], b"MCP_MEMBER_SECRET", "a" * 64, now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_model_connections(connection_id,workspace_id,owner_user_id,name,base_url,"
                "recipient_id,protocol,token_field,model_id,credential_ciphertext,credential_protection,allow_scoped_data,"
                "is_selected,status,revision,created_at,updated_at,created_by) "
                "VALUES(?,?,?,'Member model','https://model.example.test/v1','model.example.test',"
                "'openai_responses','max_tokens','member-model',?,'test-protection',1,1,'configured','model-revision',?,?,?)",
                (model_id, workspace_id, member["user"]["id"], b"MODEL_MEMBER_SECRET", now, now, member["user"]["id"]),
            )
            connection.execute(
                "INSERT INTO intelligence_model_admissions(admission_id,workspace_id,owner_user_id,connection_id,"
                "selection_revision,purpose,recipient_id,source_ref_sha256,state,created_at,expires_at) "
                "VALUES('membership-revoke-admission',?,?,?,'model-revision','inference','model.example.test',?,'source_authorized',?,?)",
                (workspace_id, member["user"]["id"], model_id, "b" * 64, now,
                 self.service._stamp(self.clock.value + timedelta(hours=1))),
            )
            connection.execute(
                "INSERT INTO jobs(id,workspace_id,user_id,kind,status,payload_json,request_hash,idempotency_key,"
                "attempts,lease_generation,created_at,updated_at) VALUES(?,?,?,'workspace_backup','queued',?,?,?,0,2,?,?)",
                (backup_job_id, workspace_id, member["user"]["id"],
                 json.dumps({"operation_id": backup_operation_id, "phase": "backup"}), "1" * 64,
                 "membership-revoke-backup", now, now),
            )
            connection.execute(
                "INSERT INTO workspace_data_operations(operation_id,source_workspace_id,owner_user_id,kind,state,phase,"
                "idempotency_key,request_hash,job_id,package_export_id,protection_generation,protection_state,"
                "protection_expires_at,protected_files_json,created_at,updated_at) "
                "VALUES(?,?,?,'backup','queued','backup',?,?,?,?,2,'active',?,?,?,?)",
                (backup_operation_id, workspace_id, member["user"]["id"], "membership-revoke-backup",
                 "2" * 64, backup_job_id, "membership-revoke-package",
                 self.service._stamp(self.clock.value + timedelta(hours=1)),
                 json.dumps([{"relative_path": protected_path}]), now, now),
            )
            for skill_id, source_type, connection_id in (
                (github_skill_id, "github_skill", github_id),
                (mcp_skill_id, "mcp_http", mcp_id),
            ):
                connection.execute(
                    "INSERT INTO intelligence_skills(skill_id,workspace_id,created_by,source_type,github_repository,"
                    "skill_path,name,description,status,current_version,created_at,updated_at) "
                    "VALUES(?,?,?,?,'owner/repository','skills/member','Member tool','test tool','enabled',1,?,?)",
                    (skill_id, workspace_id, member["user"]["id"], source_type, now, now),
                )
                connection.execute(
                    "INSERT INTO intelligence_skill_versions(skill_id,workspace_id,version_number,source_type,"
                    "github_connection_id,github_connection_revision,mcp_connection_id,mcp_connection_revision,"
                    "mcp_tool_name,mcp_tool_schema_sha256,git_revision,content_sha256,tool_schema_sha256,manifest_json,"
                    "files_json,license_json,tool_names_json,rights_review_sha256,lifecycle_state,installed_at,installed_by) "
                    "VALUES(?,?,1,?,?,?, ?,?,'read',?,'member-revision',?,?, '{}','{}','{}','[]',?,'enabled',?,?)",
                    (skill_id, workspace_id, source_type,
                     github_id if source_type == "github_skill" else None,
                     1 if source_type == "github_skill" else None,
                     mcp_id if source_type == "mcp_http" else None,
                     1 if source_type == "mcp_http" else None,
                     "c" * 64, "d" * 64, "e" * 64, "f" * 64, now, member["user"]["id"]),
                )
        member_token, _ = issue_session(self.store, member["user"]["id"], workspace_id, now=now)
        member_identity = authenticate(self.store, member_token)
        self.assertIsNotNone(member_identity)

        AccountWorkspaceMemberships(self.service).remove_member(
            owner_identity, workspace_id, member["user"]["id"],
        )

        self.assertIsNone(authenticate(self.store, member_token))
        self.assertEqual(self.store.row("SELECT status,lease_generation FROM jobs WHERE id=?", (backup_job_id,))["status"],
                         "cancelled")
        invalidated_backup = self.store.row(
            "SELECT state,protection_state,protected_files_json FROM workspace_data_operations WHERE operation_id=?",
            (backup_operation_id,),
        )
        self.assertEqual((invalidated_backup["state"], invalidated_backup["protection_state"],
                          json.loads(invalidated_backup["protected_files_json"])),
                         ("cancelled", "invalidated", []))
        for table, connection_id in (
            ("intelligence_github_connections", github_id),
            ("intelligence_mcp_connections", mcp_id),
        ):
            revoked = self.store.row(
                f"SELECT status,revision,credential_ciphertext,credential_protection FROM {table} WHERE connection_id=?",
                (connection_id,),
            )
            self.assertEqual((revoked["status"], revoked["revision"], revoked["credential_ciphertext"],
                              revoked["credential_protection"]), ("revoked", 2, None, None))
        model = self.store.row(
            "SELECT status,credential_ciphertext,credential_protection,allow_scoped_data,is_selected "
            "FROM intelligence_model_connections WHERE connection_id=?",
            (model_id,),
        )
        self.assertEqual((model["status"], model["credential_ciphertext"], model["credential_protection"],
                          model["allow_scoped_data"], model["is_selected"]), ("revoked", None, None, 0, 0))
        self.assertEqual(self.store.row(
            "SELECT COUNT(*) AS count FROM intelligence_model_admissions WHERE connection_id=?", (model_id,),
        )["count"], 0)
        for skill_id in (github_skill_id, mcp_skill_id):
            self.assertEqual(self.store.row(
                "SELECT status FROM intelligence_skills WHERE skill_id=?", (skill_id,),
            )["status"], "disabled")
            self.assertEqual(self.store.row(
                "SELECT lifecycle_state FROM intelligence_skill_versions WHERE skill_id=? AND version_number=1",
                (skill_id,),
            )["lifecycle_state"], "disabled")

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET active=1 WHERE workspace_id=? AND user_id=?",
                (workspace_id, member["user"]["id"]),
            )
        rejoined_token, _ = issue_session(self.store, member["user"]["id"], workspace_id, now=now)
        rejoined_identity = authenticate(self.store, rejoined_token)
        self.assertEqual(IntelligenceSkillStore(self.store).list_connections(rejoined_identity)[0]["status"], "revoked")
        self.assertEqual(IntelligenceSkillStore(self.store).list_mcp_connections(rejoined_identity)[0]["status"], "revoked")
        self.assertEqual(IntelligenceSkillStore(self.store).list_skills(rejoined_identity)[0]["status"], "disabled")

    def test_owner_can_transfer_from_a_legacy_multiple_owner_workspace(self):
        first, first_password = self.create_verified_account("first@example.test", "First Correct Horse 2026!")
        second, _ = self.create_verified_account("second@example.test", "Second Correct Horse 2026!")
        first_session = self.service.login_password("first@example.test", first_password)
        first_identity = authenticate(self.store, first_session["token"])
        workspace_id = first_identity["workspace"]["id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, second["user"]["id"], self.service._stamp()),
            )

        transferred = AccountWorkspaceMemberships(self.service).transfer_workspace_owner(
            first_identity,
            workspace_id,
            second["user"]["id"],
            first["user"]["id"],
        )
        self.assertEqual(transferred["owner_user_id"], second["user"]["id"])
        self.assertEqual(
            self.store.row(
                "SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, first["user"]["id"]),
            )["role"],
            "editor",
        )

    def test_workspace_invitation_capability_requires_actual_invitation_mailer(self):
        owner, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        no_invitation_transport = AccountService(self.store, clock=self.clock)
        service = AccountWorkspaceMemberships(no_invitation_transport)
        with self.assertRaises(AccountError) as unavailable:
            service.invite(identity, identity["workspace"]["id"], "new@example.test", "editor")
        self.assertEqual(unavailable.exception.code, "INVITATION_DELIVERY_UNAVAILABLE")
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM account_workspace_invitations")["count"],
            0,
        )

    def test_workspace_invitation_requires_a_product_link_capable_mail_transport(self):
        _, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        mailer = MemoryMailer()
        mailer.invitation_link_configured = False
        service = AccountWorkspaceMemberships(
            AccountService(self.store, mailer=mailer, clock=self.clock)
        )
        with self.assertRaises(AccountError) as unavailable:
            service.invite(
                identity,
                identity["workspace"]["id"],
                "invitee@example.test",
                "editor",
            )
        self.assertEqual(unavailable.exception.code, "INVITATION_DELIVERY_UNAVAILABLE")
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM account_workspace_invitations")["count"],
            0,
        )

    def test_provider_identity_email_change_and_last_method_invariants(self):
        verified, _ = self.create_verified_account(password=None)
        login_request = self.service.request_email_login("person@example.test")
        login = self.service.complete_email_login(
            login_request["reference"], self.mailer.latest_code("email_login", "person@example.test")
        )
        identity = authenticate(self.store, login["token"])
        pending = self.service.request_email_change(identity, "new.person@example.test")
        self.assertEqual(pending["state"], "email_change_pending")
        code = self.mailer.latest_code("email_change", "new.person@example.test")
        changed = self.service.confirm_email_change(identity, pending["reference"], "new.person@example.test", code)
        self.assertEqual(changed["email"], "new.person@example.test")
        self.assertEqual(self.store.row("SELECT email_normalized FROM account_emails WHERE user_id = ?", (verified["user"]["id"],))["email_normalized"], "new.person@example.test")
        with self.assertRaises(AccountError) as last:
            self.service.unlink_email(identity)
        self.assertEqual(last.exception.code, "LAST_LOGIN_METHOD")

    def test_local_merge_preview_blocks_exact_active_sessions_and_transfers_current_owners(self):
        verified, password = self.create_verified_account()
        local_token, local_identity = self.create_local_identity()
        identifiers = self.create_local_work(local_identity)
        local_user_id = local_identity["user"]["id"]
        workspace_id = local_identity["workspace"]["id"]
        account_user_id = verified["user"]["id"]
        now = self.service._stamp()
        other_user_id = "other-workspace-member"
        media_project_id = "local-media-project"
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, 'Other', ?)", (other_user_id, now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'editor', 1, ?)", (workspace_id, other_user_id, now))
            connection.execute(
                """INSERT INTO browser_sessions(
                       id, workspace_id, owner_user_id, membership_created_at, authorized_by_session_id,
                       engine, status, version, current_url, current_title, created_at, updated_at,
                       state_ciphertext, state_protection
                   ) VALUES('local-open', ?, ?, ?, ?, 'chrome', 'open', 1, 'https://example.test', 'Local', ?, ?, X'0102', 'test-protection')""",
                (workspace_id, local_user_id, now, local_identity["session_id"], now, now),
            )
            connection.execute(
                """INSERT INTO browser_sessions(
                       id, workspace_id, owner_user_id, membership_created_at, authorized_by_session_id,
                       engine, status, version, current_url, current_title, created_at, updated_at,
                       state_ciphertext, state_protection
                   ) VALUES('other-open', ?, ?, ?, ?, 'edge', 'open', 1, 'https://example.test', 'Other', ?, ?, X'0304', 'test-protection')""",
                (workspace_id, other_user_id, now, local_identity["session_id"], now, now),
            )
            connection.execute(
                "INSERT INTO client_commissions(id, workspace_id, name, scope_json, status, version, created_by, created_at, updated_at) VALUES('commission-1', ?, 'Client', '{}', 'active', 1, ?, ?, ?)",
                (workspace_id, local_user_id, now, now),
            )
            connection.execute("INSERT INTO commission_members(commission_id, user_id, role, active) VALUES('commission-1', ?, 'reviewer', 1)", (local_user_id,))
            connection.execute("INSERT INTO commission_members(commission_id, user_id, role, active) VALUES('commission-1', ?, 'editor', 1)", (account_user_id,))
            connection.execute("INSERT INTO work_commissions(work_id, commission_id, created_at) VALUES(?, 'commission-1', ?)", (identifiers["work"], now))
            connection.execute(
                "INSERT INTO publications(id, workspace_id, author_id, artifact_id, artifact_version, source_kind, source_id, source_version, kind, version, status, visibility, content_json, created_at, updated_at) VALUES('local-publication', ?, ?, ?, 1, 'artifact', ?, 1, 'work', 2, 'draft', 'team', '{}', ?, ?)",
                (workspace_id, local_user_id, identifiers["artifact"], identifiers["artifact"], now, now),
            )
            connection.execute(
                "INSERT INTO publication_versions(publication_id, version, snapshot_json, created_by, created_at) VALUES('local-publication', 1, '{}', ?, ?)",
                (local_user_id, now),
            )
            connection.execute(
                "INSERT INTO media_projects(id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by) VALUES(?, ?, ?, ?, 'Local Media', 'podcast', 1, ?, ?, ?)",
                (media_project_id, workspace_id, local_user_id, identifiers["work"], now, now, local_user_id),
            )
            for session_id, owner_id in (("local-recording", local_user_id), ("other-recording", other_user_id)):
                connection.execute(
                    """INSERT INTO media_recording_sessions(
                           id, workspace_id, project_id, owner_user_id, work_id, take_id, track_id,
                           track_kind, base_project_version, sample_rate, channels, sample_format,
                           timeline_start_num, timeline_start_den, latest_sequence, status,
                           idempotency_key, request_hash, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, 'take-1', ?, 'voiceover', 1, 48000, 1, 'pcm_s16le',
                                0, 1, -1, 'recording', ?, ?, ?, ?)""",
                    (session_id, workspace_id, media_project_id, owner_id, identifiers["work"],
                     f"track-{session_id}", f"key-{session_id}", "0" * 64, now, now),
                )

        login = self.service.login_password("person@example.test", password, local_identity=local_identity)
        self.assertIn("local_data_choice", login)
        choice = login["local_data_choice"]
        account_identity = authenticate(self.store, login["token"])
        first_preview = self.service.preview_local_data_choice(account_identity, choice["reference"])["preview"]
        self.assertFalse(first_preview["can_merge"])
        self.assertEqual(first_preview["counts"]["works"], 1)
        self.assertEqual(
            {(item["kind"], item["count"]) for item in first_preview["conflicts"]},
            {("browser_write_session", 1), ("recording_write_session", 1), ("commission_role_conflict", 1)},
        )
        blocked = self.service.complete_local_data_choice(account_identity, choice["reference"], "merge_local_data")
        self.assertEqual(blocked["state"], "local_data_merge_blocked")
        self.assertEqual(
            self.store.row("SELECT user_id FROM local_identity WHERE singleton = 1")["user_id"],
            local_user_id,
        )

        # The user resolves their own live work and role conflict. Another
        # member's open browser session is not a merge blocker.
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_sessions SET status = 'closed', closed_at = ?, state_ciphertext = NULL WHERE id = 'local-open'", (now,))
            connection.execute("UPDATE commission_members SET role = 'reviewer' WHERE commission_id = 'commission-1' AND user_id = ?", (account_user_id,))
            connection.execute("UPDATE media_recording_sessions SET status = 'completed', completed_at = ?, stopped_at = ?, updated_at = ? WHERE id = 'local-recording'", (now, now, now))
        refreshed = self.service.preview_local_data_choice(account_identity, choice["reference"])["preview"]
        self.assertTrue(refreshed["can_merge"])
        self.assertEqual(refreshed["conflicts"], [])
        merged = self.service.complete_local_data_choice(account_identity, choice["reference"], "merge_local_data")
        self.assertEqual(merged["state"], "local_data_merged")
        self.assertEqual(merged["workspace"]["id"], workspace_id)
        self.assertEqual(merged["summary"]["platform_sessions_transferred"], False)
        self.assertIsNone(authenticate(self.store, local_token))
        self.assertEqual(
            self.store.row("SELECT user_id FROM local_identity WHERE singleton = 1")["user_id"],
            account_user_id,
        )
        self.assertEqual(authenticate(self.store, merged["token"])["user"]["id"], account_user_id)
        for kind, object_id, table in (
            ("work", identifiers["work"], "works"),
            ("material", identifiers["material"], "materials"),
            ("artifact", identifiers["artifact"], "artifacts"),
        ):
            self.assertEqual(self.store.row(f"SELECT owner_user_id FROM {table} WHERE id = ?", (object_id,))["owner_user_id"], account_user_id, kind)
        self.assertEqual(self.store.row("SELECT owner_user_id FROM media_projects WHERE id = ?", (media_project_id,))["owner_user_id"], account_user_id)
        self.assertEqual(self.store.row("SELECT owner_user_id FROM media_recording_sessions WHERE id = 'local-recording'")["owner_user_id"], account_user_id)
        self.assertEqual(self.store.row("SELECT owner_user_id, status FROM media_recording_sessions WHERE id = 'other-recording'")["owner_user_id"], other_user_id)
        self.assertEqual(self.store.row("SELECT author_id FROM publications WHERE id = 'local-publication'")["author_id"], account_user_id)
        self.assertEqual(self.store.row("SELECT created_by FROM publication_versions WHERE publication_id = 'local-publication' AND version = 1")["created_by"], local_user_id)
        self.assertEqual(
            self.store.row("SELECT created_by FROM work_versions WHERE work_id = ? AND version = 1", (identifiers["work"],))["created_by"],
            local_user_id,
        )
        source_member = self.store.row("SELECT active FROM commission_members WHERE commission_id = 'commission-1' AND user_id = ?", (local_user_id,))
        account_member = self.store.row("SELECT role, active FROM commission_members WHERE commission_id = 'commission-1' AND user_id = ?", (account_user_id,))
        self.assertEqual(source_member["active"], 0)
        self.assertEqual((account_member["role"], account_member["active"]), ("reviewer", 1))
        ended_browser = self.store.row("SELECT owner_user_id, status, state_ciphertext, state_protection FROM browser_sessions WHERE id = 'local-open'")
        self.assertEqual((ended_browser["owner_user_id"], ended_browser["status"], ended_browser["state_ciphertext"], ended_browser["state_protection"]), (account_user_id, "closed", None, None))
        other_browser = self.store.row("SELECT owner_user_id, status, state_ciphertext FROM browser_sessions WHERE id = 'other-open'")
        self.assertEqual((other_browser["owner_user_id"], other_browser["status"], bytes(other_browser["state_ciphertext"])), (other_user_id, "open", b"\x03\x04"))
        with self.assertRaises(AccountError) as reused:
            self.service.preview_local_data_choice(authenticate(self.store, merged["token"]), choice["reference"])
        self.assertEqual(reused.exception.code, "LOCAL_DATA_CHOICE_INVALID")

    def test_local_merge_keep_separate_does_not_add_membership_or_move_identity(self):
        verified, password = self.create_verified_account()
        local_token, local_identity = self.create_local_identity()
        account_user_id = verified["user"]["id"]
        local_user_id = local_identity["user"]["id"]
        login = self.service.login_password("person@example.test", password, local_identity=local_identity)
        account_identity = authenticate(self.store, login["token"])
        result = self.service.complete_local_data_choice(
            account_identity, login["local_data_choice"]["reference"], "keep_separate"
        )
        self.assertEqual(result["state"], "local_and_account_data_separate")
        returned_local = authenticate(self.store, result["local_session_token"])
        self.assertEqual(returned_local["user"]["id"], local_user_id)
        self.assertEqual(returned_local["workspace"]["id"], local_identity["workspace"]["id"])
        self.assertEqual(self.store.row("SELECT user_id FROM local_identity WHERE singleton = 1")["user_id"], local_user_id)
        self.assertIsNone(self.store.row("SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id = ?", (local_identity["workspace"]["id"], account_user_id)))
        self.assertIsNotNone(authenticate(self.store, local_token))
        self.assertEqual(authenticate(self.store, login["token"])["user"]["id"], account_user_id)

    def test_workspace_invitation_is_unavailable_without_mail_transport(self):
        _verified, password = self.create_verified_account()
        login = self.service.login_password("person@example.test", password)
        identity = authenticate(self.store, login["token"])
        unconfigured = AccountService(self.store, clock=self.clock)
        service = AccountWorkspaceMemberships(unconfigured)
        with self.assertRaises(AccountError) as unavailable:
            service.invite(identity, identity["workspace"]["id"], "new@example.test", "editor")
        self.assertEqual(unavailable.exception.code, "INVITATION_DELIVERY_UNAVAILABLE")
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM account_workspace_invitations")["count"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
