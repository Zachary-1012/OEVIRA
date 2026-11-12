"""Account lifecycle services on the product's existing users and sessions.

This module never creates a second identity database. Email delivery is an
injected existing transport; absent a configured transport, email operations
fail closed. Google and Apple login use fixed OIDC providers and locally verify
their signed ID tokens before any identity is created or linked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import time
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .auth import (
    AuthenticationError,
    _digest,
    issue_session_in_transaction,
    list_sessions as list_product_sessions,
    revoke_all_sessions,
    revoke_session as revoke_product_session,
    utc_now,
)
from .account_mailer import _valid_public_base_url
from .store import Store


ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_emails (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    email_normalized TEXT NOT NULL UNIQUE,
    email_display TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_password_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_external_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL CHECK(provider IN ('google', 'apple')),
    subject TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    last_authenticated_at TEXT NOT NULL,
    UNIQUE(provider, subject)
);
CREATE INDEX IF NOT EXISTS account_external_identity_user
    ON account_external_identities(user_id, provider);
CREATE TABLE IF NOT EXISTS account_challenges (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK(purpose IN ('register', 'email_login', 'password_reset', 'email_change', 'delete_confirm', 'delete_cancel')),
    subject_hash TEXT NOT NULL,
    user_id TEXT REFERENCES users(id),
    code_salt TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    invalidated_at TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK(failed_attempts >= 0),
    delivery_status TEXT NOT NULL CHECK(delivery_status IN ('pending', 'sent', 'failed', 'unknown'))
);
CREATE INDEX IF NOT EXISTS account_challenges_subject
    ON account_challenges(subject_hash, purpose, created_at DESC);
CREATE TABLE IF NOT EXISTS account_login_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    local_user_id TEXT,
    local_workspace_id TEXT,
    local_session_id TEXT
);
CREATE TABLE IF NOT EXISTS account_oauth_transactions (
    state_hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(provider IN ('google', 'apple')),
    intent TEXT NOT NULL CHECK(intent IN ('login', 'link', 'cancel_deletion')),
    user_id TEXT,
    workspace_id TEXT,
    session_id TEXT,
    local_user_id TEXT,
    local_workspace_id TEXT,
    local_session_id TEXT,
    code_challenge TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'consumed', 'failed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS account_oauth_transactions_expiry
    ON account_oauth_transactions(expires_at, status);
CREATE TABLE IF NOT EXISTS account_local_merge_choices (
    token_hash TEXT PRIMARY KEY,
    local_user_id TEXT NOT NULL,
    local_workspace_id TEXT NOT NULL,
    local_session_id TEXT NOT NULL,
    account_user_id TEXT NOT NULL,
    account_workspace_id TEXT NOT NULL,
    account_session_id TEXT,
    local_return_session_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    result TEXT CHECK(result IN ('keep_separate', 'merged'))
);
CREATE INDEX IF NOT EXISTS account_local_merge_choices_expiry
    ON account_local_merge_choices(expires_at, consumed_at);
CREATE TABLE IF NOT EXISTS account_workspace_invitations (
    invitation_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    email_normalized TEXT NOT NULL,
    email_display TEXT NOT NULL,
    invited_role TEXT NOT NULL CHECK(invited_role IN ('owner', 'editor', 'viewer')),
    invited_by TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'revoked', 'expired', 'delivery_failed', 'delivery_unknown')),
    delivery_status TEXT NOT NULL CHECK(delivery_status IN ('pending', 'sent', 'failed', 'unknown', 'revoked')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_user_id TEXT REFERENCES users(id),
    revoked_at TEXT,
    revoked_by TEXT REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS account_workspace_invitation_pending
    ON account_workspace_invitations(workspace_id, email_normalized)
    WHERE status IN ('pending', 'delivery_unknown');
CREATE INDEX IF NOT EXISTS account_workspace_invitations_recent
    ON account_workspace_invitations(workspace_id, created_at DESC, invitation_id);
CREATE TABLE IF NOT EXISTS account_membership_events (
    event_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    subject_user_id TEXT REFERENCES users(id),
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    invitation_id TEXT REFERENCES account_workspace_invitations(invitation_id),
    action TEXT NOT NULL CHECK(action IN ('invited', 'invitation_resent', 'invitation_revoked', 'invitation_accepted', 'role_changed', 'owner_transferred', 'member_removed', 'workspace_left')),
    previous_role TEXT,
    new_role TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_membership_events_recent
    ON account_membership_events(workspace_id, occurred_at DESC, event_id);
CREATE TABLE IF NOT EXISTS account_preferences (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    model_improvement_consent TEXT NOT NULL DEFAULT 'not_asked'
        CHECK(model_improvement_consent IN ('not_asked', 'granted', 'declined', 'withdrawn')),
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);
CREATE TABLE IF NOT EXISTS account_exports (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS account_exports_user_expiry
    ON account_exports(user_id, expires_at DESC);
CREATE TABLE IF NOT EXISTS account_login_failures (
    subject_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_login_failures_subject
    ON account_login_failures(subject_hash, purpose, occurred_at);
CREATE TABLE IF NOT EXISTS account_deletions (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('awaiting_confirmation', 'scheduled', 'cancelled', 'completed')),
    confirmation_requested_at TEXT NOT NULL,
    export_reference TEXT,
    requested_at TEXT,
    scheduled_at TEXT,
    cancelled_at TEXT,
    completed_at TEXT,
    completion_code TEXT
);
CREATE INDEX IF NOT EXISTS account_deletions_due
    ON account_deletions(status, scheduled_at);
CREATE TABLE IF NOT EXISTS account_deletion_file_cleanup (
    deletion_user_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'removed', 'blocked')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(deletion_user_id, relative_path)
);
CREATE INDEX IF NOT EXISTS account_deletion_file_cleanup_pending
    ON account_deletion_file_cleanup(status, updated_at);
CREATE TABLE IF NOT EXISTS account_events (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_events_user_time
    ON account_events(user_id, occurred_at);
"""


SUPPORTED_PROVIDERS = frozenset({"google", "apple"})
PASSWORD_ITERATIONS = 600_000
CODE_ITERATIONS = 200_000
PASSWORD_MIN_CHARACTERS = 15
PASSWORD_MAX_CHARACTERS = 1024
PASSWORD_MAX_UTF8_BYTES = 4096
CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
CODE_COOLDOWN = timedelta(seconds=60)
CODE_RATE_WINDOW = timedelta(minutes=5)
CODE_RATE_MAX = 3
CODE_MAX_ATTEMPTS = 5
OAUTH_STATE_TTL = timedelta(minutes=5)
LOGIN_CHALLENGE_TTL = timedelta(minutes=5)
LOCAL_MERGE_CHOICE_TTL = timedelta(minutes=10)
ACCOUNT_EXPORT_LIFETIME = timedelta(hours=24)
DELETION_GRACE = timedelta(days=30)
RECENT_AUTH_WINDOW = timedelta(minutes=15)
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_FAILURE_LIMIT = 5

_COMMON_PASSWORDS = frozenset({
    "123456789012345", "1234567890123456", "passwordpassword", "password123456",
    "qwertyuiopasdfg", "letmeinletmein1", "welcome12345678", "iloveyou123456",
    "changeme1234567", "correcthorsebattery", "adminadminadmin", "oevira2026account",
})

_PROVIDER_METADATA = {
    "google": {
        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
        "expected_issuer": "https://accounts.google.com",
        "allowed_hosts": frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}),
        "scope": "openid email profile",
        "response_mode": None,
        "env_prefix": "OEVIRA_GOOGLE",
    },
    "apple": {
        "discovery_url": "https://appleid.apple.com/.well-known/openid-configuration",
        "expected_issuer": "https://appleid.apple.com",
        "allowed_hosts": frozenset({"appleid.apple.com"}),
        "scope": "openid email",
        "response_mode": "form_post",
        "env_prefix": "OEVIRA_APPLE",
    },
}


class AccountError(AuthenticationError):
    """A safe, non-secret account lifecycle failure."""

    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(code, message, status)
        self.details = details or {}


class _DeletionBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EmailDelivery(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def invitation_link_configured(self) -> bool: ...

    def send_code(self, email: str, purpose: str, code: str, expires_at: str) -> bool: ...

    def send_invitation(self, email: str, token: str, expires_at: str, workspace_name: str) -> bool: ...


@dataclass(frozen=True)
class AccountPolicy:
    password_iterations: int = PASSWORD_ITERATIONS
    code_iterations: int = CODE_ITERATIONS
    code_length: int = CODE_LENGTH
    code_ttl: timedelta = CODE_TTL
    code_cooldown: timedelta = CODE_COOLDOWN
    code_rate_window: timedelta = CODE_RATE_WINDOW
    code_rate_max: int = CODE_RATE_MAX
    code_max_attempts: int = CODE_MAX_ATTEMPTS
    login_failure_window: timedelta = LOGIN_FAILURE_WINDOW
    login_failure_limit: int = LOGIN_FAILURE_LIMIT
    oauth_state_ttl: timedelta = OAUTH_STATE_TTL
    login_challenge_ttl: timedelta = LOGIN_CHALLENGE_TTL
    deletion_grace: timedelta = DELETION_GRACE
    recent_auth_window: timedelta = RECENT_AUTH_WINDOW


@dataclass(frozen=True)
class OAuthProviderConfig:
    provider: str
    client_id: str
    client_secret: str
    redirect_uri: str
    discovery_url: str
    expected_issuer: str
    allowed_hosts: frozenset[str]
    scope: str
    response_mode: str | None

    def __post_init__(self):
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError("OAuth provider is not supported")
        metadata = _PROVIDER_METADATA[self.provider]
        redirect = urlsplit(self.redirect_uri) if isinstance(self.redirect_uri, str) else None
        apple_redirect_valid = True
        if self.provider == "apple":
            try:
                if redirect is None or redirect.scheme != "https" or not redirect.hostname:
                    apple_redirect_valid = False
                else:
                    ipaddress.ip_address(redirect.hostname)
                    apple_redirect_valid = False
            except ValueError:
                apple_redirect_valid = (
                    redirect is not None
                    and redirect.scheme == "https"
                    and redirect.hostname not in {None, "localhost"}
                    and "." in redirect.hostname
                )
        if (
            not isinstance(self.client_id, str) or not self.client_id.strip()
            or not isinstance(self.client_secret, str) or not self.client_secret.strip()
            or not _valid_redirect_uri(self.redirect_uri)
            or self.discovery_url != metadata["discovery_url"]
            or self.expected_issuer != metadata["expected_issuer"]
            or self.allowed_hosts != metadata["allowed_hosts"]
            or "openid" not in self.scope.split()
            or "email" not in self.scope.split()
            or self.response_mode != metadata["response_mode"]
            or not apple_redirect_valid
        ):
            raise ValueError("OAuth provider configuration does not match the official provider profile")

    @classmethod
    def from_environment(cls, provider: str) -> OAuthProviderConfig | None:
        if provider not in SUPPORTED_PROVIDERS:
            raise AccountError("UNSUPPORTED_PROVIDER", "This login provider is not supported", 400)
        metadata = _PROVIDER_METADATA[provider]
        prefix = metadata["env_prefix"]
        client_id = os.environ.get(f"{prefix}_CLIENT_ID", "").strip()
        client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip()
        redirect_uri = os.environ.get(f"{prefix}_REDIRECT_URI", "").strip()
        if not client_id or not client_secret or not redirect_uri:
            return None
        if not _valid_redirect_uri(redirect_uri):
            return None
        return cls(
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            discovery_url=metadata["discovery_url"],
            expected_issuer=metadata["expected_issuer"],
            allowed_hosts=metadata["allowed_hosts"],
            scope=metadata["scope"],
            response_mode=metadata["response_mode"],
        )


def install_account_schema(store: Store) -> None:
    """Install additive account tables in the existing Store database."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + ACCOUNT_SCHEMA + "\nCOMMIT;")
        # Account tables were introduced additively. Upgrade databases created
        # during the transition without rebuilding users or session records.
        for table, columns in {
            "account_oauth_transactions": {
                "local_user_id": "TEXT",
                "local_workspace_id": "TEXT",
                "local_session_id": "TEXT",
            },
            "account_local_merge_choices": {
                "account_session_id": "TEXT",
                "local_return_session_id": "TEXT",
            },
        }.items():
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    finally:
        connection.close()


def _valid_redirect_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            return False
        if parsed.scheme == "https":
            return parsed.port in (None, 443)
        if parsed.scheme == "http" and parsed.hostname.lower() in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            return parsed.port in (None, 80, 8317)
        return False
    except ValueError:
        return False


def _email(value: Any) -> tuple[str, str]:
    if type(value) is not str or len(value) > 254 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise AccountError("INVALID_EMAIL", "Enter a valid email address", 400)
    value = value.strip()
    if value.count("@") != 1:
        raise AccountError("INVALID_EMAIL", "Enter a valid email address", 400)
    local, domain = value.rsplit("@", 1)
    if (
        not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
    ):
        raise AccountError("INVALID_EMAIL", "Enter a valid email address", 400)
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise AccountError("INVALID_EMAIL", "Enter a valid email address", 400) from exc
    labels = ascii_domain.split(".")
    if (
        len(ascii_domain) > 253 or len(labels) < 2
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
               or not re.fullmatch(r"[a-z0-9-]+", label) for label in labels)
    ):
        raise AccountError("INVALID_EMAIL", "Enter a valid email address", 400)
    normalized = f"{local.casefold()}@{ascii_domain}"
    return normalized, value


def _email_hint(email: str) -> str:
    local, domain = email.split("@", 1)
    visible = local[:1] if len(local) < 3 else local[:2]
    return f"{visible}{'*' * max(1, min(8, len(local) - len(visible)))}@{domain}"


def _display_name(value: Any, email: str) -> str:
    if value is None:
        return email.split("@", 1)[0][:80] or "用户"
    if type(value) is not str:
        raise AccountError("INVALID_INPUT", "Display name must be text", 400)
    name = "".join(character for character in value.strip() if character.isprintable())
    if not name or len(name) > 80:
        raise AccountError("INVALID_INPUT", "Display name must contain 1 to 80 characters", 400)
    return name


def _normalize_password(value: Any, blocklist: frozenset[str] = _COMMON_PASSWORDS) -> str:
    if type(value) is not str:
        raise AccountError("INVALID_PASSWORD", "Password must be text", 400)
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise AccountError("INVALID_PASSWORD", "Password contains unsupported control characters", 400)
    if len(normalized) < PASSWORD_MIN_CHARACTERS:
        raise AccountError("PASSWORD_TOO_SHORT", f"Password must contain at least {PASSWORD_MIN_CHARACTERS} characters", 400)
    if len(normalized) > PASSWORD_MAX_CHARACTERS or len(normalized.encode("utf-8")) > PASSWORD_MAX_UTF8_BYTES:
        raise AccountError("PASSWORD_TOO_LONG", "Password exceeds the supported length", 400)
    if normalized.casefold() in blocklist:
        raise AccountError("PASSWORD_BLOCKLISTED", "Choose a less common password", 400)
    return normalized


def _hash_secret(value: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, iterations, dklen=32)


def _hash_password(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = _hash_secret(password, salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iteration_text, salt_text, digest_text = stored.split("$", 3)
        iterations = int(iteration_text)
        if scheme != "pbkdf2_sha256" or not PASSWORD_ITERATIONS <= iterations <= 5_000_000:
            return False
        salt, expected = _b64url_decode(salt_text), _b64url_decode(digest_text)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = _hash_secret(password, salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (AttributeError, ValueError, TypeError, UnicodeError):
        return False


def _subject_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if type(value) is not str or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    if _b64url_encode(decoded) != value:
        raise ValueError("Non-canonical base64url value")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _json_loads(raw: bytes) -> dict[str, Any]:
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")),
    )
    if not isinstance(value, dict):
        raise ValueError("JSON value must be an object")
    return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class ProviderTransport:
    """Bounded HTTPS transport for the fixed Google and Apple OIDC hosts."""

    def __init__(self, *, timeout_seconds: float = 5.0, maximum_bytes: int = 1_048_576):
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes = maximum_bytes
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @staticmethod
    def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise AccountError("OAUTH_ENDPOINT_REJECTED", "The identity provider endpoint is not allowed", 503)

    def _read_json(self, request: urllib.request.Request, allowed_hosts: frozenset[str]) -> dict[str, Any]:
        self._validate_url(request.full_url, allowed_hosts)
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                self._validate_url(final_url, allowed_hosts)
                body = response.read(self.maximum_bytes + 1)
                if len(body) > self.maximum_bytes:
                    raise AccountError("OAUTH_RESPONSE_TOO_LARGE", "The identity provider response is too large", 503)
                return _json_loads(body)
        except AccountError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise AccountError("OAUTH_PROVIDER_UNAVAILABLE", "The identity provider could not be reached", 503) from exc

    def get_json(self, url: str, allowed_hosts: frozenset[str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "OEVIRA account login"})
        return self._read_json(request, allowed_hosts)

    def post_form(self, url: str, values: dict[str, str], allowed_hosts: frozenset[str]) -> dict[str, Any]:
        payload = urllib.parse.urlencode(values).encode("ascii")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "OEVIRA account login"},
            method="POST",
        )
        return self._read_json(request, allowed_hosts)


class OIDCVerifier:
    """OIDC discovery, code exchange, and RS256 ID-token verification."""

    def __init__(self, transport: Any | None = None, *, clock: Callable[[], float] = time.time):
        self.transport = transport or ProviderTransport()
        self.clock = clock
        self._metadata: dict[str, tuple[float, dict[str, Any]]] = {}
        self._jwks: dict[str, tuple[float, dict[str, Any]]] = {}

    def _get_metadata(self, config: OAuthProviderConfig) -> dict[str, Any]:
        cached = self._metadata.get(config.provider)
        now = self.clock()
        if cached is not None and cached[0] > now:
            return cached[1]
        metadata = self.transport.get_json(config.discovery_url, config.allowed_hosts)
        if metadata.get("issuer") != config.expected_issuer:
            raise AccountError("OAUTH_METADATA_INVALID", "The identity provider metadata is invalid", 503)
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = metadata.get(field)
            try:
                ProviderTransport._validate_url(endpoint, config.allowed_hosts)
            except (TypeError, ValueError, AccountError) as exc:
                raise AccountError("OAUTH_METADATA_INVALID", "The identity provider metadata is invalid", 503) from exc
        algorithms = metadata.get("id_token_signing_alg_values_supported")
        if not isinstance(algorithms, list) or "RS256" not in algorithms:
            raise AccountError("OAUTH_ALGORITHM_UNSUPPORTED", "The identity provider does not advertise RS256", 503)
        methods = metadata.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise AccountError("OAUTH_PKCE_UNAVAILABLE", "The identity provider does not advertise PKCE S256", 503)
        self._metadata[config.provider] = (now + 300.0, metadata)
        return metadata

    def authorization_url(
        self,
        config: OAuthProviderConfig,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        metadata = self._get_metadata(config)
        values = {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": config.scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if config.response_mode:
            values["response_mode"] = config.response_mode
        return metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(values)

    def exchange_code(self, config: OAuthProviderConfig, *, code: str, code_verifier: str) -> str:
        metadata = self._get_metadata(config)
        response = self.transport.post_form(
            metadata["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": code_verifier,
            },
            config.allowed_hosts,
        )
        id_token = response.get("id_token")
        if type(id_token) is not str or not id_token or len(id_token) > 16_384:
            raise AccountError("OAUTH_ID_TOKEN_MISSING", "The identity provider did not return an ID token", 401)
        return id_token

    def _get_jwks(self, config: OAuthProviderConfig, *, force_refresh: bool = False) -> dict[str, Any]:
        cached = self._jwks.get(config.provider)
        now = self.clock()
        if not force_refresh and cached is not None and cached[0] > now:
            return cached[1]
        metadata = self._get_metadata(config)
        result = self.transport.get_json(metadata["jwks_uri"], config.allowed_hosts)
        keys = result.get("keys")
        if not isinstance(keys, list) or not keys:
            raise AccountError("OAUTH_JWKS_INVALID", "The identity provider signing keys are invalid", 503)
        self._jwks[config.provider] = (now + 300.0, result)
        return result

    def _key_for(self, config: OAuthProviderConfig, kid: str) -> rsa.RSAPublicKey:
        jwks = self._get_jwks(config)
        match = self._find_rsa_key(jwks, kid)
        if match is None:
            jwks = self._get_jwks(config, force_refresh=True)
            match = self._find_rsa_key(jwks, kid)
        if match is None:
            raise AccountError("OAUTH_SIGNING_KEY_UNKNOWN", "The identity provider signing key is not recognized", 401)
        try:
            modulus = int.from_bytes(_b64url_decode(match["n"]), "big")
            exponent = int.from_bytes(_b64url_decode(match["e"]), "big")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountError("OAUTH_JWK_INVALID", "The identity provider signing key is invalid", 503) from exc
        if public_key.key_size < 2048:
            raise AccountError("OAUTH_JWK_WEAK", "The identity provider signing key is too small", 503)
        return public_key

    @staticmethod
    def _find_rsa_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
        for key in jwks.get("keys", []):
            if not isinstance(key, dict) or key.get("kid") != kid or key.get("kty") != "RSA":
                continue
            if key.get("use", "sig") != "sig" or key.get("alg", "RS256") != "RS256":
                continue
            operations = key.get("key_ops")
            if operations is not None and (not isinstance(operations, list) or "verify" not in operations):
                continue
            if type(key.get("n")) is not str or type(key.get("e")) is not str:
                continue
            return key
        return None

    def verify_id_token(
        self,
        config: OAuthProviderConfig,
        token: str,
        *,
        expected_nonce_hash: str,
    ) -> dict[str, Any]:
        if type(token) is not str or len(token) > 16_384:
            raise AccountError("OAUTH_ID_TOKEN_INVALID", "The identity provider token is invalid", 401)
        parts = token.split(".")
        if len(parts) != 3:
            raise AccountError("OAUTH_ID_TOKEN_INVALID", "The identity provider token is invalid", 401)
        try:
            header = _json_loads(_b64url_decode(parts[0]))
            claims = _json_loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, UnicodeError) as exc:
            raise AccountError("OAUTH_ID_TOKEN_INVALID", "The identity provider token is invalid", 401) from exc
        if header.get("alg") != "RS256" or type(header.get("kid")) is not str:
            raise AccountError("OAUTH_ALGORITHM_UNSUPPORTED", "The identity provider token algorithm is not allowed", 401)
        if any(name in header for name in ("jku", "x5u", "crit")):
            raise AccountError("OAUTH_ID_TOKEN_INVALID", "The identity provider token header is not allowed", 401)
        public_key = self._key_for(config, header["kid"])
        try:
            public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError) as exc:
            raise AccountError("OAUTH_SIGNATURE_INVALID", "The identity provider token signature is invalid", 401) from exc
        return _validate_oidc_claims(config, claims, expected_nonce_hash=expected_nonce_hash, now=self.clock())


def _validate_oidc_claims(
    config: OAuthProviderConfig,
    claims: dict[str, Any],
    *,
    expected_nonce_hash: str,
    now: float,
) -> dict[str, Any]:
    issuer = claims.get("iss")
    allowed_issuers = {config.expected_issuer}
    if config.provider == "google":
        allowed_issuers.add("accounts.google.com")
    if issuer not in allowed_issuers:
        raise AccountError("OAUTH_ISSUER_INVALID", "The identity provider issuer is invalid", 401)
    audience = claims.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and all(type(item) is str for item in audience):
        audiences = audience
    else:
        audiences = []
    if config.client_id not in audiences:
        raise AccountError("OAUTH_AUDIENCE_INVALID", "The identity provider token was issued for another client", 401)
    if len(audiences) > 1 and claims.get("azp") != config.client_id:
        raise AccountError("OAUTH_AUTHORIZED_PARTY_INVALID", "The identity provider token presenter is invalid", 401)
    if "azp" in claims and claims.get("azp") != config.client_id:
        raise AccountError("OAUTH_AUTHORIZED_PARTY_INVALID", "The identity provider token presenter is invalid", 401)
    exp, issued = claims.get("exp"), claims.get("iat")
    if type(exp) not in (int, float) or type(exp) is bool or not math.isfinite(float(exp)):
        raise AccountError("OAUTH_EXPIRATION_INVALID", "The identity provider token expiration is invalid", 401)
    if type(issued) not in (int, float) or type(issued) is bool or not math.isfinite(float(issued)):
        raise AccountError("OAUTH_ISSUED_AT_INVALID", "The identity provider token issue time is invalid", 401)
    if float(exp) <= now - 60 or float(exp) <= float(issued) or float(issued) > now + 60 or float(exp) - float(issued) > 7200:
        raise AccountError("OAUTH_TOKEN_EXPIRED", "The identity provider token is expired or outside its valid time", 401)
    not_before = claims.get("nbf")
    if not_before is not None:
        if type(not_before) not in (int, float) or type(not_before) is bool or not math.isfinite(float(not_before)) or float(not_before) > now + 60:
            raise AccountError("OAUTH_TOKEN_NOT_YET_VALID", "The identity provider token is not yet valid", 401)
    nonce = claims.get("nonce")
    if type(nonce) is not str or not hmac.compare_digest(_subject_hash(nonce), expected_nonce_hash):
        raise AccountError("OAUTH_NONCE_INVALID", "The identity provider nonce is invalid", 401)
    subject = claims.get("sub")
    if type(subject) is not str or not subject or len(subject) > 255 or any(ord(char) < 33 for char in subject):
        raise AccountError("OAUTH_SUBJECT_INVALID", "The identity provider subject is invalid", 401)
    email = claims.get("email")
    verified = claims.get("email_verified")
    email_verified = verified is True or (type(verified) is str and verified.lower() == "true")
    if email is not None and type(email) is not str:
        raise AccountError("OAUTH_EMAIL_INVALID", "The identity provider email is invalid", 401)
    name = claims.get("name")
    if name is not None and type(name) is not str:
        name = None
    return {
        "provider": config.provider,
        "subject": subject,
        "email": email,
        "email_verified": email_verified,
        "name": name,
        "issuer": issuer,
        "audience": audiences,
        "expiration": float(exp),
    }


class AccountService:
    """Persistence and policy for email, password, OIDC, sessions, and deletion."""

    def __init__(
        self,
        store: Store,
        *,
        mailer: EmailDelivery | None = None,
        provider_configs: dict[str, OAuthProviderConfig | None] | None = None,
        oauth_transport: Any | None = None,
        policy: AccountPolicy = AccountPolicy(),
        clock: Callable[[], datetime] | None = None,
        account_exporter: Callable[[str], Any] | None = None,
        user_data_purger: Callable[[Any, str], bool] | None = None,
        password_blocklist: set[str] | frozenset[str] | None = None,
        public_base_url: str | None = None,
    ):
        self.store = store
        self.mailer = mailer
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.account_exporter = account_exporter
        self.user_data_purger = user_data_purger or self._purge_account_owned_data_tx
        self.public_base_url = _valid_public_base_url(public_base_url)
        if public_base_url and self.public_base_url is None:
            raise ValueError("public_base_url must be an HTTPS URL or loopback HTTP URL without query or fragment")
        self.browser_session_closer: Callable[[str, str], None] | None = None
        self.password_blocklist = frozenset(
            item.casefold() for item in (password_blocklist if password_blocklist is not None else _COMMON_PASSWORDS)
            if type(item) is str and item
        )
        self.password_blocklist_configured = password_blocklist is not None
        self.provider_configs = provider_configs if provider_configs is not None else {
            provider: OAuthProviderConfig.from_environment(provider)
            for provider in SUPPORTED_PROVIDERS
        }
        self.oidc = OIDCVerifier(oauth_transport, clock=lambda: self._now().timestamp())

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("AccountService clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _stamp(self, value: datetime | None = None) -> str:
        return (value or self._now()).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _mailer_ready(self) -> bool:
        return self.mailer is not None and getattr(self.mailer, "configured", False) is True

    def _require_mailer(self) -> None:
        if not self._mailer_ready():
            raise AccountError("EMAIL_DELIVERY_UNAVAILABLE", "Email verification is unavailable because no delivery service is configured", 503)

    def _invitation_mailer_ready(self) -> bool:
        return (
            self._mailer_ready()
            and callable(getattr(self.mailer, "send_invitation", None))
            and getattr(self.mailer, "invitation_link_configured", False) is True
        )

    def oauth_callback_ready(self, provider: str) -> bool:
        config = self.provider_configs.get(provider)
        if config is None or self.public_base_url is None:
            return False
        base = urlsplit(self.public_base_url)
        if base.path:
            return False
        try:
            loopback_callback = ipaddress.ip_address(base.hostname or "").is_loopback
        except ValueError:
            loopback_callback = (base.hostname or "").casefold() == "localhost"
        if not loopback_callback:
            return False
        return config.redirect_uri == (
            f"{self.public_base_url}/api/account/oauth/{provider}/callback"
        )

    def _event(self, connection, user_id: str | None, event_type: str, now: str) -> None:
        connection.execute(
            "INSERT INTO account_events(id, user_id, event_type, occurred_at) VALUES(?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, event_type, now),
        )

    def _check_code_request_limit(self, connection, email_normalized: str, purpose: str, now: datetime) -> str:
        subject_hash = _subject_hash(email_normalized)
        now_text = self._stamp(now)
        window_start = self._stamp(now - self.policy.code_rate_window)
        recent = connection.execute(
            "SELECT COUNT(*) AS count FROM account_challenges WHERE subject_hash = ? AND purpose = ? AND created_at >= ?",
            (subject_hash, purpose, window_start),
        ).fetchone()["count"]
        if recent >= self.policy.code_rate_max:
            oldest = connection.execute(
                "SELECT created_at FROM account_challenges WHERE subject_hash = ? AND purpose = ? AND created_at >= ? ORDER BY created_at LIMIT 1",
                (subject_hash, purpose, window_start),
            ).fetchone()
            retry_after = max(
                1,
                int((datetime.fromisoformat(oldest["created_at"].replace("Z", "+00:00")) + self.policy.code_rate_window - now).total_seconds()),
            )
            raise AccountError("EMAIL_CODE_RATE_LIMITED", f"Too many code requests; try again in {retry_after} seconds", 429)
        latest = connection.execute(
            "SELECT created_at FROM account_challenges WHERE subject_hash = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
            (subject_hash, purpose),
        ).fetchone()
        if latest is not None:
            previous = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
            remaining = self.policy.code_cooldown - (now - previous)
            if remaining.total_seconds() > 0:
                raise AccountError("EMAIL_CODE_COOLDOWN", f"Wait {max(1, int(remaining.total_seconds()))} seconds before requesting another email code", 429)
        return subject_hash

    def _issue_code_tx(
        self,
        connection,
        *,
        purpose: str,
        email_normalized: str,
        user_id: str | None,
        now: datetime,
    ) -> tuple[str, str, str, str]:
        if purpose not in {"register", "email_login", "password_reset", "email_change", "delete_confirm", "delete_cancel"}:
            raise ValueError("Unsupported account challenge purpose")
        subject_hash = self._check_code_request_limit(connection, email_normalized, purpose, now)
        now_text = self._stamp(now)
        code = f"{secrets.randbelow(10 ** self.policy.code_length):0{self.policy.code_length}d}"
        salt = secrets.token_bytes(16)
        digest = _hash_secret(code, salt, self.policy.code_iterations)
        challenge_id = str(uuid.uuid4())
        expires_at = self._stamp(now + self.policy.code_ttl)
        connection.execute(
            "UPDATE account_challenges SET invalidated_at = ?, delivery_status = 'failed' "
            "WHERE subject_hash = ? AND purpose = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (now_text, subject_hash, purpose),
        )
        connection.execute(
            """INSERT INTO account_challenges(
                   id, purpose, subject_hash, user_id, code_salt, code_hash,
                   created_at, expires_at, delivery_status
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                challenge_id,
                purpose,
                subject_hash,
                user_id,
                _b64url_encode(salt),
                _b64url_encode(digest),
                now_text,
                expires_at,
            ),
        )
        return challenge_id, code, expires_at, self._stamp(now + self.policy.code_cooldown)

    def _record_no_delivery_tx(
        self,
        connection,
        *,
        purpose: str,
        email_normalized: str,
        user_id: str | None,
        now: datetime,
    ) -> tuple[str, str, str]:
        """Apply identical cooldown/rate behavior without disclosing account existence."""
        if purpose not in {"register", "email_login", "password_reset", "delete_cancel"}:
            raise ValueError("No-delivery challenge purpose is not supported")
        subject_hash = self._check_code_request_limit(connection, email_normalized, purpose, now)
        now_text = self._stamp(now)
        salt = secrets.token_bytes(16)
        code = f"{secrets.randbelow(10 ** self.policy.code_length):0{self.policy.code_length}d}"
        digest = _hash_secret(code, salt, self.policy.code_iterations)
        challenge_id = str(uuid.uuid4())
        expires_at = self._stamp(now + self.policy.code_ttl)
        connection.execute(
            "UPDATE account_challenges SET invalidated_at = ?, delivery_status = 'failed' "
            "WHERE subject_hash = ? AND purpose = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (now_text, subject_hash, purpose),
        )
        connection.execute(
            """INSERT INTO account_challenges(
                   id, purpose, subject_hash, user_id, code_salt, code_hash,
                   created_at, expires_at, invalidated_at, delivery_status
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')""",
            (challenge_id, purpose, subject_hash, user_id, _b64url_encode(salt), _b64url_encode(digest), now_text, expires_at, now_text),
        )
        return challenge_id, expires_at, self._stamp(now + self.policy.code_cooldown)

    def _deliver_code(self, challenge_id: str, email: str, purpose: str, code: str, expires_at: str) -> None:
        try:
            accepted = self.mailer.send_code(email, purpose, code, expires_at) if self.mailer else False
        except Exception as exc:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_challenges SET delivery_status = 'unknown', invalidated_at = ? WHERE id = ? AND consumed_at IS NULL",
                    (utc_now(), challenge_id),
                )
            raise AccountError("EMAIL_DELIVERY_UNKNOWN", "The email result is unknown; request a fresh code after the cooldown", 503) from exc
        if accepted is not True:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_challenges SET delivery_status = 'failed', invalidated_at = ? WHERE id = ? AND consumed_at IS NULL",
                    (utc_now(), challenge_id),
                )
            raise AccountError("EMAIL_DELIVERY_FAILED", "The email transport did not accept the message", 503)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE account_challenges SET delivery_status = 'sent' WHERE id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (challenge_id,),
            )

    def _verify_code_tx(
        self,
        connection,
        *,
        reference: Any,
        purpose: str,
        code: Any,
        now: datetime,
        user_id: str | None = None,
        email_normalized: str | None = None,
    ) -> tuple[Any | None, AccountError | None]:
        if type(code) is not str or not re.fullmatch(rf"[0-9]{{{self.policy.code_length}}}", code):
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if type(reference) is not str or not 1 <= len(reference) <= 80:
            _hash_secret(code, b"\x00" * 16, self.policy.code_iterations)
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        row = connection.execute(
            """SELECT * FROM account_challenges
               WHERE id = ? AND purpose = ? AND consumed_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (reference, purpose),
        ).fetchone()
        if row is None or (user_id is not None and row["user_id"] != user_id):
            # Equalize the expensive path for nonexistent/expired identifiers.
            _hash_secret(code, b"\x00" * 16, self.policy.code_iterations)
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if email_normalized is not None and row["subject_hash"] != _subject_hash(email_normalized):
            _hash_secret(code, b"\x00" * 16, self.policy.code_iterations)
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if row["expires_at"] <= self._stamp(now):
            connection.execute(
                "UPDATE account_challenges SET invalidated_at = ? WHERE id = ? AND consumed_at IS NULL",
                (self._stamp(now), row["id"]),
            )
            return None, AccountError("VERIFICATION_CODE_EXPIRED", "The verification code has expired; request a new code", 410)
        if row["delivery_status"] != "sent" or row["invalidated_at"] is not None:
            _hash_secret(code, b"\x00" * 16, self.policy.code_iterations)
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        try:
            salt = _b64url_decode(row["code_salt"])
            expected = _b64url_decode(row["code_hash"])
        except ValueError:
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        actual = _hash_secret(code, salt, self.policy.code_iterations)
        if not hmac.compare_digest(actual, expected):
            attempts = row["failed_attempts"] + 1
            invalidated = self._stamp(now) if attempts >= self.policy.code_max_attempts else None
            connection.execute(
                "UPDATE account_challenges SET failed_attempts = ?, invalidated_at = COALESCE(?, invalidated_at) WHERE id = ? AND consumed_at IS NULL",
                (attempts, invalidated, row["id"]),
            )
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        cursor = connection.execute(
            "UPDATE account_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (self._stamp(now), row["id"]),
        )
        if cursor.rowcount != 1:
            return None, AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        return row, None

    def _ensure_personal_workspace(self, connection, user_id: str, now: str) -> dict:
        current = connection.execute(
            """SELECT w.id, w.name FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.user_id = ? AND m.active = 1 ORDER BY m.created_at, m.workspace_id""",
            (user_id,),
        ).fetchall()
        if current:
            return dict(current[0])
        user = connection.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise AccountError("ACCOUNT_NOT_FOUND", "The account is unavailable", 404)
        workspace_id = str(uuid.uuid4())
        workspace_name = f"{user['name']}的工作空间"
        connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, workspace_name, now))
        connection.execute(
            "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
            (workspace_id, user_id, now),
        )
        return {"id": workspace_id, "name": workspace_name}

    def _deletion_pending(self, connection, user_id: str) -> Any | None:
        row = connection.execute("SELECT * FROM account_deletions WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None and row["status"] == "scheduled":
            return row
        return None

    def _active_workspaces(self, connection, user_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT w.id, w.name, m.role FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
               WHERE m.user_id = ? AND m.active = 1 ORDER BY m.created_at, m.workspace_id""",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _local_source_values(identity: Any) -> dict[str, str] | None:
        if not isinstance(identity, dict):
            return None
        user = identity.get("user")
        workspace = identity.get("workspace")
        if not isinstance(user, dict) or not isinstance(workspace, dict):
            return None
        values = {
            "local_user_id": user.get("id"),
            "local_workspace_id": workspace.get("id"),
            "local_session_id": identity.get("session_id"),
        }
        if not all(type(value) is str and value for value in values.values()):
            return None
        return values

    def _valid_local_source_tx(self, connection, source: dict | None, now: datetime) -> dict | None:
        if not isinstance(source, dict):
            return None
        values = {
            key: source.get(key)
            for key in ("local_user_id", "local_workspace_id", "local_session_id")
        }
        if not all(type(value) is str and value for value in values.values()):
            return None
        local = connection.execute(
            "SELECT user_id, workspace_id FROM local_identity WHERE singleton = 1"
        ).fetchone()
        if local is None or (local["user_id"], local["workspace_id"]) != (
            values["local_user_id"], values["local_workspace_id"]
        ):
            return None
        session = connection.execute(
            """SELECT m.role FROM sessions s JOIN memberships m
                   ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
               WHERE s.id = ? AND s.user_id = ? AND s.workspace_id = ?
                 AND s.revoked_at IS NULL AND s.expires_at > ? AND m.active = 1""",
            (
                values["local_session_id"], values["local_user_id"],
                values["local_workspace_id"], self._stamp(now),
            ),
        ).fetchone()
        if session is None or session["role"] != "owner":
            return None
        return values

    @staticmethod
    def _table_exists(connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def _revoke_workspace_connections(self, connection, workspace_id: str, user_id: str, now: str) -> None:
        """Revoke user-owned provider credentials in the same transaction as access loss."""
        skill_connections: list[tuple[str, str]] = []
        if self._table_exists(connection, "intelligence_github_connections"):
            rows = connection.execute(
                "SELECT connection_id FROM intelligence_github_connections WHERE workspace_id=? AND owner_user_id=?",
                (workspace_id, user_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE intelligence_github_connections SET status='revoked',credential_ciphertext=NULL,"
                    "credential_protection=NULL,provider_login=NULL,validation_status='invalid',revision=revision+1,"
                    "updated_at=? WHERE connection_id=? AND workspace_id=? AND owner_user_id=? "
                    "AND (status<>'revoked' OR credential_ciphertext IS NOT NULL OR credential_protection IS NOT NULL "
                    "OR provider_login IS NOT NULL OR validation_status<>'invalid')",
                    (now, row["connection_id"], workspace_id, user_id),
                )
                skill_connections.append(("github", row["connection_id"]))
        if self._table_exists(connection, "intelligence_mcp_connections"):
            rows = connection.execute(
                "SELECT connection_id FROM intelligence_mcp_connections WHERE workspace_id=? AND owner_user_id=?",
                (workspace_id, user_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE intelligence_mcp_connections SET status='revoked',credential_ciphertext=NULL,"
                    "credential_protection=NULL,validation_status='invalid',revision=revision+1,updated_at=? "
                    "WHERE connection_id=? AND workspace_id=? AND owner_user_id=? "
                    "AND (status<>'revoked' OR credential_ciphertext IS NOT NULL "
                    "OR credential_protection IS NOT NULL OR validation_status<>'invalid')",
                    (now, row["connection_id"], workspace_id, user_id),
                )
                skill_connections.append(("mcp", row["connection_id"]))
        if self._table_exists(connection, "intelligence_model_connections"):
            rows = connection.execute(
                "SELECT connection_id FROM intelligence_model_connections WHERE workspace_id=? AND owner_user_id=?",
                (workspace_id, user_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE intelligence_model_connections SET credential_ciphertext=NULL,credential_protection=NULL,"
                    "allow_scoped_data=0,is_selected=0,status='revoked',revision=?,updated_at=? "
                    "WHERE connection_id=? AND workspace_id=? AND owner_user_id=? "
                    "AND (status<>'revoked' OR credential_ciphertext IS NOT NULL OR credential_protection IS NOT NULL "
                    "OR allow_scoped_data<>0 OR is_selected<>0)",
                    (str(uuid.uuid4()), now, row["connection_id"], workspace_id, user_id),
                )
                if self._table_exists(connection, "intelligence_model_admissions"):
                    connection.execute(
                        "DELETE FROM intelligence_model_admissions WHERE workspace_id=? AND owner_user_id=? AND connection_id=?",
                        (workspace_id, user_id, row["connection_id"]),
                    )
        if skill_connections and self._table_exists(connection, "intelligence_skills") and self._table_exists(
            connection, "intelligence_skill_versions"
        ):
            from .intelligence_skills import IntelligenceSkillStore

            for connection_type, connection_id in skill_connections:
                IntelligenceSkillStore._disable_connection_skills(
                    connection, workspace_id, connection_id, connection_type=connection_type,
                )

    def _invalidate_workspace_backup_protections(self, connection, workspace_id: str,
                                                user_id: str, now: str) -> int:
        if not self._table_exists(connection, "workspace_data_operations"):
            return 0
        changed = connection.execute(
            "UPDATE workspace_data_operations SET state='cancelled',error_code='WORKSPACE_BACKUP_INVALIDATED',"
            "protection_state='invalidated',protection_expires_at=?,protected_files_json='[]',updated_at=? "
            "WHERE source_workspace_id=? AND owner_user_id=? AND kind='backup' "
            "AND protection_state IN ('pending','active') AND state NOT IN ('backup_ready','complete','cancelled')",
            (now, now, workspace_id, user_id),
        )
        return changed.rowcount

    def _workspace_backup_protects_file(self, relative_path: str) -> bool:
        try:
            with self.store.transaction(immediate=False) as connection:
                if not self._table_exists(connection, "workspace_data_operations"):
                    return False
                rows = connection.execute(
                    "SELECT o.owner_user_id,o.source_workspace_id,o.protection_state,o.protected_files_json,"
                    "j.user_id AS job_user_id,j.workspace_id AS job_workspace_id,j.status AS job_status "
                    "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                    "WHERE o.kind='backup' AND o.protection_state IN ('pending','active')"
                ).fetchall()
        except sqlite3.Error:
            return False
        for row in rows:
            if (row["job_user_id"] != row["owner_user_id"]
                    or row["job_workspace_id"] != row["source_workspace_id"]
                    or row["job_status"] not in {"queued", "running", "pause_requested", "paused", "interrupted", "cancel_requested"}):
                continue
            if row["protection_state"] == "pending":
                if relative_path.startswith(f"objects/{row['source_workspace_id']}/"):
                    return True
                continue
            try:
                protected = json.loads(row["protected_files_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(protected, list) and any(
                isinstance(item, dict) and item.get("relative_path") == relative_path
                for item in protected
            ):
                return True
        return False

    def _release_expired_backup_protections(self, now: datetime) -> int:
        now_text = self._stamp(now)
        changed = 0
        with self.store.transaction() as connection:
            if not self._table_exists(connection, "workspace_data_operations"):
                return 0
            rows = connection.execute(
                "SELECT o.operation_id,o.state,o.owner_user_id,o.protection_generation,o.source_workspace_id,"
                "j.status,j.lease_generation,j.user_id AS job_user_id,j.workspace_id AS job_workspace_id "
                "FROM workspace_data_operations o JOIN jobs j ON j.id=o.job_id "
                "WHERE o.kind='backup' AND o.protection_state IN ('pending','active') "
                "AND o.protection_expires_at IS NOT NULL AND o.protection_expires_at<=?",
                (now_text,),
            ).fetchall()
            for row in rows:
                if (row["job_user_id"] != row["owner_user_id"]
                        or row["job_workspace_id"] != row["source_workspace_id"]):
                    continue
                if row["status"] in {"running", "pause_requested", "cancel_requested"}:
                    continue
                error_code = None
                if row["state"] not in {"backup_ready", "complete", "cancelled", "failed"}:
                    error_code = "WORKSPACE_BACKUP_PROTECTION_EXPIRED"
                connection.execute(
                    "UPDATE workspace_data_operations SET protection_state='expired',protection_expires_at=NULL,"
                    "protected_files_json='[]',error_code=COALESCE(?,error_code),updated_at=? "
                    "WHERE operation_id=? AND protection_generation=? AND protection_state IN ('pending','active')",
                    (error_code, now_text, row["operation_id"], row["protection_generation"]),
                )
                changed += 1
        return changed

    def _local_merge_preview_tx(
        self, connection, local_user_id: str, local_workspace_id: str, account_user_id: str,
    ) -> dict[str, Any]:
        def count(table: str, where: str, parameters: tuple[Any, ...]) -> int:
            if not self._table_exists(connection, table):
                return 0
            return int(connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", parameters
            ).fetchone()["count"])

        counts = {
            "works": count("works", "workspace_id = ? AND owner_user_id = ?", (local_workspace_id, local_user_id)),
            "materials": count("materials", "workspace_id = ? AND owner_user_id = ?", (local_workspace_id, local_user_id)),
            "artifacts": count("artifacts", "workspace_id = ? AND owner_user_id = ?", (local_workspace_id, local_user_id)),
            "media_projects": count("media_projects", "workspace_id = ? AND owner_user_id = ?", (local_workspace_id, local_user_id)),
            "recording_history": count(
                "media_recording_sessions", "workspace_id = ? AND owner_user_id = ? AND status IN ('completed', 'cancelled', 'failed')",
                (local_workspace_id, local_user_id),
            ),
        }
        conflicts: list[dict[str, Any]] = []
        browser_count = count(
            "browser_sessions",
            "workspace_id = ? AND owner_user_id = ? AND status IN ('starting', 'open', 'reopening')",
            (local_workspace_id, local_user_id),
        )
        browser_operation_count = 0
        if self._table_exists(connection, "browser_session_operations") and self._table_exists(connection, "browser_sessions"):
            browser_operation_count = int(connection.execute(
                """SELECT COUNT(*) AS count FROM browser_session_operations o
                   JOIN browser_sessions s ON s.id = o.session_id AND s.workspace_id = o.workspace_id
                   WHERE o.workspace_id = ? AND o.owner_user_id = ?
                     AND (o.status = 'pending' OR (o.status = 'unknown' AND s.status IN ('starting', 'open', 'reopening', 'suspended', 'error')))""",
                (local_workspace_id, local_user_id),
            ).fetchone()["count"])
        if browser_count or browser_operation_count:
            conflicts.append({
                "kind": "browser_write_session",
                "count": browser_count,
                "uncertain_operations": browser_operation_count,
                "resolution": "Close this user's active browser sessions and resolve pending browser actions, then preview again.",
            })
        recording_count = count(
            "media_recording_sessions",
            "workspace_id = ? AND owner_user_id = ? AND status IN ('armed', 'awaiting_first_segment', 'recording', 'paused', 'interrupted', 'stopping')",
            (local_workspace_id, local_user_id),
        )
        if recording_count:
            conflicts.append({
                "kind": "recording_write_session",
                "count": recording_count,
                "resolution": "Finish or cancel this user's recording sessions, preserve the segments, then preview again.",
            })
        commission_role_conflicts = 0
        if self._table_exists(connection, "client_commissions") and self._table_exists(connection, "commission_members"):
            commission_role_conflicts = int(connection.execute(
                """SELECT COUNT(*) AS count FROM client_commissions c
                   JOIN commission_members source ON source.commission_id = c.id
                   JOIN commission_members target ON target.commission_id = c.id
                   WHERE c.workspace_id = ? AND source.user_id = ? AND source.active = 1
                     AND target.user_id = ? AND target.active = 1
                     AND source.role <> target.role
                     AND source.role <> 'owner' AND target.role <> 'owner'""",
                (local_workspace_id, local_user_id, account_user_id),
            ).fetchone()["count"])
        if commission_role_conflicts:
            conflicts.append({
                "kind": "commission_role_conflict",
                "count": commission_role_conflicts,
                "resolution": "Resolve each different client role explicitly, then preview again.",
            })
        return {
            "can_merge": not conflicts,
            "source": {"user_id": local_user_id, "workspace_id": local_workspace_id},
            "target_account_user_id": account_user_id,
            "local_identity_will_become_account_identity": True,
            "platform_sessions_transferred": False,
            "browser_platform_sessions": "ended_and_platform_authentication_state_removed",
            "counts": counts,
            "conflicts": conflicts,
        }

    def _create_local_choice_tx(
        self,
        connection,
        *,
        account_user_id: str,
        account_workspace_id: str,
        account_session_id: str,
        source: dict | None,
        now: datetime,
    ) -> dict | None:
        local = self._valid_local_source_tx(connection, source, now)
        if local is None or local["local_user_id"] == account_user_id:
            return None
        reference = secrets.token_urlsafe(32)
        expires_at = self._stamp(now + LOCAL_MERGE_CHOICE_TTL)
        connection.execute(
            """INSERT INTO account_local_merge_choices(
                   token_hash, local_user_id, local_workspace_id, local_session_id,
                   account_user_id, account_workspace_id, account_session_id,
                   created_at, expires_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _digest(reference), local["local_user_id"], local["local_workspace_id"],
                local["local_session_id"], account_user_id, account_workspace_id,
                account_session_id, self._stamp(now), expires_at,
            ),
        )
        local_token, _ = issue_session_in_transaction(
            connection, local["local_user_id"], local["local_workspace_id"], now=self._stamp(now)
        )
        local_session = connection.execute(
            "SELECT id FROM sessions WHERE token_hash = ?", (_digest(local_token),)
        ).fetchone()
        connection.execute(
            "UPDATE account_local_merge_choices SET local_return_session_id = ? WHERE token_hash = ?",
            (local_session["id"], _digest(reference)),
        )
        preview = self._local_merge_preview_tx(
            connection, local["local_user_id"], local["local_workspace_id"], account_user_id
        )
        return {
            "reference": reference,
            "expires_at": expires_at,
            "preview": preview,
            "local_session_token": local_token,
        }

    def _new_login_challenge(
        self,
        connection,
        user_id: str,
        now: datetime,
        workspaces: list[dict],
        local_source: dict | None = None,
    ) -> dict:
        challenge = secrets.token_urlsafe(32)
        local = self._valid_local_source_tx(connection, local_source, now)
        connection.execute(
            """INSERT INTO account_login_challenges(
                   token_hash, user_id, created_at, expires_at,
                   local_user_id, local_workspace_id, local_session_id
               ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (
                _digest(challenge),
                user_id,
                self._stamp(now),
                self._stamp(now + self.policy.login_challenge_ttl),
                None if local is None else local["local_user_id"],
                None if local is None else local["local_workspace_id"],
                None if local is None else local["local_session_id"],
            ),
        )
        return {
            "state": "workspace_selection_required",
            "login_challenge": challenge,
            "expires_at": self._stamp(now + self.policy.login_challenge_ttl),
            "workspaces": [{"id": item["id"], "name": item["name"], "role": item["role"]} for item in workspaces],
        }

    def _finish_login(
        self,
        user_id: str,
        workspace_id: str | None = None,
        *,
        local_identity: dict | None = None,
        local_source: dict | None = None,
    ) -> dict:
        now = self._now()
        source = self._local_source_values(local_identity) if local_identity is not None else local_source
        with self.store.transaction() as connection:
            if self._deletion_pending(connection, user_id) is not None:
                deletion = self._deletion_pending(connection, user_id)
                raise AccountError(
                    "ACCOUNT_DELETION_PENDING",
                    f"Account deletion is scheduled for {deletion['scheduled_at']}; cancel deletion to sign in",
                    403,
                )
            user = connection.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise AccountError("ACCOUNT_NOT_FOUND", "The account is unavailable", 404)
            workspaces = self._active_workspaces(connection, user_id)
            if not workspaces:
                raise AccountError("NO_ACTIVE_WORKSPACE", "The account has no active workspace", 403)
            if workspace_id is not None:
                selected = next((item for item in workspaces if item["id"] == workspace_id), None)
                if selected is None:
                    raise AccountError("WORKSPACE_NOT_FOUND", "No active access exists for that workspace", 404)
            elif len(workspaces) > 1:
                return self._new_login_challenge(connection, user_id, now, workspaces, source)
            else:
                selected = workspaces[0]
            token, identity = issue_session_in_transaction(
                connection, user_id, selected["id"], now=self._stamp(now)
            )
            session = connection.execute("SELECT id FROM sessions WHERE token_hash = ?", (_digest(token),)).fetchone()
            result = {"state": "authenticated", "token": token, **identity}
            choice = self._create_local_choice_tx(
                connection,
                account_user_id=user_id,
                account_workspace_id=selected["id"],
                account_session_id=session["id"],
                source=source,
                now=now,
            )
            if choice is not None:
                result["local_data_choice"] = choice
            return result

    def complete_login_challenge(self, challenge: Any, workspace_id: Any) -> dict:
        if type(challenge) is not str or not 32 <= len(challenge) <= 256:
            raise AccountError("INVALID_LOGIN_CHALLENGE", "The login challenge is invalid or expired", 401)
        if type(workspace_id) is not str or not workspace_id:
            raise AccountError("INVALID_INPUT", "A workspace selection is required", 400)
        now = self._now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_login_challenges WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?",
                (_digest(challenge), self._stamp(now)),
            ).fetchone()
            if row is None or self._deletion_pending(connection, row["user_id"]) is not None:
                raise AccountError("INVALID_LOGIN_CHALLENGE", "The login challenge is invalid or expired", 401)
            membership = connection.execute(
                "SELECT 1 FROM memberships WHERE user_id = ? AND workspace_id = ? AND active = 1",
                (row["user_id"], workspace_id),
            ).fetchone()
            if membership is None:
                raise AccountError("WORKSPACE_NOT_FOUND", "No active access exists for that workspace", 404)
            cursor = connection.execute(
                "UPDATE account_login_challenges SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL",
                (self._stamp(now), _digest(challenge)),
            )
            if cursor.rowcount != 1:
                raise AccountError("INVALID_LOGIN_CHALLENGE", "The login challenge is invalid or expired", 401)
            token, identity = issue_session_in_transaction(
                connection, row["user_id"], workspace_id, now=self._stamp(now)
            )
            session = connection.execute("SELECT id FROM sessions WHERE token_hash = ?", (_digest(token),)).fetchone()
            result = {"state": "authenticated", "token": token, **identity}
            source = {
                "local_user_id": row["local_user_id"],
                "local_workspace_id": row["local_workspace_id"],
                "local_session_id": row["local_session_id"],
            }
            choice = self._create_local_choice_tx(
                connection,
                account_user_id=row["user_id"],
                account_workspace_id=workspace_id,
                account_session_id=session["id"],
                source=source,
                now=now,
            )
            if choice is not None:
                result["local_data_choice"] = choice
        return result

    def preview_local_data_choice(self, identity: dict, reference: Any) -> dict:
        active = self._active_identity(identity)
        if type(reference) is not str or not 32 <= len(reference) <= 256:
            raise AccountError("LOCAL_DATA_CHOICE_INVALID", "The local-data choice is invalid or expired", 401)
        now = self._now()
        with self.store.transaction(immediate=False) as connection:
            choice = connection.execute(
                """SELECT * FROM account_local_merge_choices
                   WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?""",
                (_digest(reference), self._stamp(now)),
            ).fetchone()
            if (
                choice is None
                or choice["account_user_id"] != active["user_id"]
                or choice["account_workspace_id"] != active["workspace_id"]
                or choice["account_session_id"] != active["session_id"]
            ):
                raise AccountError("LOCAL_DATA_CHOICE_INVALID", "The local-data choice is invalid or expired", 401)
            if self._valid_local_source_tx(connection, dict(choice), now) is None:
                raise AccountError("LOCAL_IDENTITY_CHANGED", "The local identity changed; sign in to the local workspace again", 409)
            preview = self._local_merge_preview_tx(
                connection, choice["local_user_id"], choice["local_workspace_id"], choice["account_user_id"]
            )
        return {"state": "local_data_choice_ready", "expires_at": choice["expires_at"], "preview": preview}

    def complete_local_data_choice(self, identity: dict, reference: Any, choice_value: Any) -> dict:
        active = self._active_identity(identity)
        if type(reference) is not str or not 32 <= len(reference) <= 256:
            raise AccountError("LOCAL_DATA_CHOICE_INVALID", "The local-data choice is invalid or expired", 401)
        if type(choice_value) is not str or choice_value not in {"keep_separate", "merge_local_data"}:
            raise AccountError("INVALID_INPUT", "Choose whether to keep local data separate or merge it", 400)
        now = self._now()
        now_text = self._stamp(now)
        with self.store.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM account_local_merge_choices
                   WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?""",
                (_digest(reference), now_text),
            ).fetchone()
            if (
                row is None
                or row["account_user_id"] != active["user_id"]
                or row["account_workspace_id"] != active["workspace_id"]
                or row["account_session_id"] != active["session_id"]
            ):
                raise AccountError("LOCAL_DATA_CHOICE_INVALID", "The local-data choice is invalid or expired", 401)
            source = self._valid_local_source_tx(connection, dict(row), now)
            if source is None:
                raise AccountError("LOCAL_IDENTITY_CHANGED", "The local identity changed; sign in to the local workspace again", 409)
            preview = self._local_merge_preview_tx(
                connection, source["local_user_id"], source["local_workspace_id"], active["user_id"]
            )
            if choice_value == "keep_separate":
                cursor = connection.execute(
                    "UPDATE account_local_merge_choices SET consumed_at = ?, result = 'keep_separate' WHERE token_hash = ? AND consumed_at IS NULL",
                    (now_text, _digest(reference)),
                )
                if cursor.rowcount != 1:
                    raise AccountError("LOCAL_DATA_CHOICE_INVALID", "The local-data choice was already completed", 409)
                prior_local_session_id = row["local_return_session_id"]
                if prior_local_session_id:
                    connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND workspace_id = ? AND revoked_at IS NULL",
                        (now_text, prior_local_session_id, source["local_user_id"], source["local_workspace_id"]),
                    )
                local_token, local_identity = issue_session_in_transaction(
                    connection, source["local_user_id"], source["local_workspace_id"], now=now_text
                )
                local_session = connection.execute(
                    "SELECT id FROM sessions WHERE token_hash = ?", (_digest(local_token),)
                ).fetchone()
                connection.execute(
                    "UPDATE account_local_merge_choices SET local_return_session_id = ? WHERE token_hash = ?",
                    (local_session["id"], _digest(reference)),
                )
                self._event(connection, active["user_id"], "local_data_kept_separate", now_text)
                return {
                    "state": "local_and_account_data_separate",
                    "local_workspace_id": source["local_workspace_id"],
                    "account_workspace_id": active["workspace_id"],
                    "local_session_token": local_token,
                    "local_user": local_identity["user"],
                    "local_workspace": local_identity["workspace"],
                    "platform_sessions_transferred": False,
                }
            if not preview["can_merge"]:
                return {"state": "local_data_merge_blocked", "preview": preview}

            local_workspace_id = source["local_workspace_id"]
            local_user_id = source["local_user_id"]
            account_user_id = active["user_id"]
            owner = connection.execute(
                "SELECT role, active FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (local_workspace_id, local_user_id),
            ).fetchone()
            if owner is None or owner["active"] != 1 or owner["role"] != "owner":
                raise AccountError("LOCAL_MERGE_ACCESS_INVALID", "Only the active owner of this local workspace can transfer its identity", 403)

            # Add the destination's active workspace membership before moving
            # commission authority, as required by the shared access owner.
            connection.execute(
                """INSERT INTO memberships(workspace_id, user_id, role, active, created_at)
                   VALUES(?, ?, 'owner', 1, ?)
                   ON CONFLICT(workspace_id, user_id) DO UPDATE SET role = 'owner', active = 1""",
                (local_workspace_id, account_user_id, now_text),
            )

            from .api import ApiError
            from .collaboration import (
                require_artifact_access,
                require_material_access,
                require_work_access,
                transfer_local_commission_membership,
            )

            local_access = {
                "user": {"id": local_user_id},
                "workspace": {"id": local_workspace_id},
            }
            work_ids = []
            if self._table_exists(connection, "works"):
                work_ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM works WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (local_workspace_id, local_user_id),
                ).fetchall()]
                for work_id in work_ids:
                    require_work_access(connection, local_access, work_id)
            material_ids = []
            if self._table_exists(connection, "materials"):
                material_ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM materials WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (local_workspace_id, local_user_id),
                ).fetchall()]
                for material_id in material_ids:
                    require_material_access(connection, local_access, material_id)
            artifact_ids = []
            if self._table_exists(connection, "artifacts"):
                artifact_ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM artifacts WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (local_workspace_id, local_user_id),
                ).fetchall()]
                for artifact_id in artifact_ids:
                    require_artifact_access(connection, local_access, artifact_id)
            media_projects = []
            if self._table_exists(connection, "media_projects"):
                media_projects = connection.execute(
                    "SELECT id, work_id FROM media_projects WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (local_workspace_id, local_user_id),
                ).fetchall()
                for project in media_projects:
                    if project["work_id"] is not None:
                        require_work_access(connection, local_access, project["work_id"])

            try:
                transfer_result = transfer_local_commission_membership(
                    connection, local_workspace_id, local_user_id, account_user_id
                )
            except ApiError as exc:
                raise AccountError(exc.code, str(exc), exc.status) from exc

            if work_ids:
                connection.execute(
                    f"UPDATE works SET owner_user_id = ? WHERE id IN ({','.join('?' for _ in work_ids)})",
                    (account_user_id, *work_ids),
                )
            if material_ids:
                connection.execute(
                    f"UPDATE materials SET owner_user_id = ? WHERE id IN ({','.join('?' for _ in material_ids)})",
                    (account_user_id, *material_ids),
                )
            if artifact_ids:
                connection.execute(
                    f"UPDATE artifacts SET owner_user_id = ? WHERE id IN ({','.join('?' for _ in artifact_ids)})",
                    (account_user_id, *artifact_ids),
                )
            if media_projects:
                connection.execute(
                    "UPDATE media_projects SET owner_user_id = ? WHERE workspace_id = ? AND owner_user_id = ?",
                    (account_user_id, local_workspace_id, local_user_id),
                )
            if self._table_exists(connection, "media_recording_sessions"):
                connection.execute(
                    "UPDATE media_recording_sessions SET owner_user_id = ? WHERE workspace_id = ? AND owner_user_id = ?",
                    (account_user_id, local_workspace_id, local_user_id),
                )
            if self._table_exists(connection, "media_uploads"):
                connection.execute(
                    "UPDATE media_uploads SET user_id = ?, updated_at = ? WHERE workspace_id = ? AND user_id = ? AND status IN ('receiving', 'finalizing', 'completed', 'interrupted')",
                    (account_user_id, now_text, local_workspace_id, local_user_id),
                )

            # Do not inherit a browser's signed-in platform state. End saved
            # browser contexts and erase protected cookies; keep version authorship.
            browser_rows = []
            if self._table_exists(connection, "browser_sessions"):
                browser_rows = connection.execute(
                    "SELECT id, version FROM browser_sessions WHERE workspace_id = ? AND owner_user_id = ?",
                    (local_workspace_id, local_user_id),
                ).fetchall()
                for browser in browser_rows:
                    next_version = browser["version"] + 1
                    connection.execute(
                        """UPDATE browser_sessions SET owner_user_id = ?, status = 'closed',
                               state_ciphertext = NULL, state_protection = NULL,
                               operation_id = NULL, operation_expires_at = NULL,
                               closed_at = COALESCE(closed_at, ?), updated_at = ?, version = ?,
                               last_error_code = 'ACCOUNT_IDENTITY_TRANSFERRED'
                           WHERE id = ? AND workspace_id = ? AND owner_user_id = ?""",
                        (account_user_id, now_text, now_text, next_version, browser["id"], local_workspace_id, local_user_id),
                    )
                    if self._table_exists(connection, "browser_session_versions"):
                        connection.execute(
                            "INSERT INTO browser_session_versions(session_id, workspace_id, version, operation, metadata_json, created_at, created_by) VALUES(?, ?, ?, 'account_identity_transfer', '{\"platform_state\":\"removed\"}', ?, ?)",
                            (browser["id"], local_workspace_id, next_version, now_text, account_user_id),
                        )

            # A stale worker lease cannot publish after this transaction. Its
            # source artifacts stay as history and can be resumed as new work.
            cancelled_jobs = 0
            if self._table_exists(connection, "jobs"):
                cancelled_jobs = connection.execute(
                    """UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                           lease_generation = lease_generation + 1,
                           error_code = 'LOCAL_IDENTITY_TRANSFER',
                           error_message = 'Local access changed during account merge; resume from the saved source.',
                           updated_at = ?
                       WHERE workspace_id = ? AND user_id = ? AND status IN ('queued', 'running', 'cancel_requested')""",
                    (now_text, local_workspace_id, local_user_id),
                ).rowcount
                if self._table_exists(connection, "media_renders"):
                    connection.execute(
                        """UPDATE media_renders SET status = 'cancelled', error_code = 'LOCAL_IDENTITY_TRANSFER',
                               error_message = 'Local access changed during account merge.', updated_at = ?
                           WHERE workspace_id = ? AND status IN ('queued', 'running', 'writing')
                             AND job_id IN (SELECT id FROM jobs WHERE workspace_id = ? AND user_id = ? AND error_code = 'LOCAL_IDENTITY_TRANSFER')""",
                        (now_text, local_workspace_id, local_workspace_id, local_user_id),
                    )

            # Compare-and-swap the local identity, then revoke only the old
            # identity's sessions in this transferred workspace.
            cursor = connection.execute(
                "UPDATE local_identity SET user_id = ? WHERE singleton = 1 AND user_id = ? AND workspace_id = ?",
                (account_user_id, local_user_id, local_workspace_id),
            )
            if cursor.rowcount != 1:
                raise AccountError("LOCAL_IDENTITY_CHANGED", "The local identity changed before the merge completed", 409)
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND workspace_id = ? AND revoked_at IS NULL",
                (now_text, local_user_id, local_workspace_id),
            )
            connection.execute(
                "UPDATE memberships SET active = 0 WHERE workspace_id = ? AND user_id = ? AND active = 1",
                (local_workspace_id, local_user_id),
            )
            connection.execute(
                "UPDATE account_local_merge_choices SET consumed_at = ?, result = 'merged' WHERE token_hash = ? AND consumed_at IS NULL",
                (now_text, _digest(reference)),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (now_text, active["session_id"], account_user_id),
            )
            new_token, account_identity = issue_session_in_transaction(
                connection, account_user_id, local_workspace_id, now=now_text
            )
            self._event(connection, account_user_id, "local_data_merged", now_text)
            self._event(connection, local_user_id, "local_identity_transferred", now_text)
        return {
            "state": "local_data_merged",
            "token": new_token,
            **account_identity,
            "summary": {
                "works": len(work_ids),
                "materials": len(material_ids),
                "artifacts": len(artifact_ids),
                "media_projects": len(media_projects),
                "browser_sessions_ended": len(browser_rows),
                "jobs_cancelled_for_safe_resume": cancelled_jobs,
                "commission_transfers": len(transfer_result["commission_transfers"]),
                "historical_authorship": "preserved",
                "platform_sessions_transferred": False,
            },
        }

    def register_email(self, email: Any, *, display_name: Any = None, password: Any = None) -> dict:
        self._require_mailer()
        normalized, display = _email(email)
        name = _display_name(display_name, normalized)
        secret = _normalize_password(password, self.password_blocklist) if password is not None else None
        now = self._now()
        created_user = False
        challenge: tuple[str, str, str, str] | None = None
        send_code = False
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT user_id, verified_at FROM account_emails WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
            if existing is None:
                user_id = str(uuid.uuid4())
                connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, name, self._stamp(now)))
                connection.execute(
                    "INSERT INTO account_emails(user_id, email_normalized, email_display, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                    (user_id, normalized, display, self._stamp(now), self._stamp(now)),
                )
                created_user = True
            else:
                user_id = existing["user_id"]
            if existing is not None and existing["verified_at"] is not None:
                reference, expires_at, resend_at = self._record_no_delivery_tx(
                    connection, purpose="register", email_normalized=normalized, user_id=user_id, now=now
                )
            else:
                if secret is not None:
                    has_password = connection.execute(
                        "SELECT 1 FROM account_password_credentials WHERE user_id = ?", (user_id,)
                    ).fetchone()
                    if has_password is None:
                        connection.execute(
                            "INSERT INTO account_password_credentials(user_id, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                            (user_id, _hash_password(secret, self.policy.password_iterations), self._stamp(now), self._stamp(now)),
                        )
                challenge = self._issue_code_tx(
                    connection, purpose="register", email_normalized=normalized, user_id=user_id, now=now
                )
                reference, expires_at, resend_at = challenge[0], challenge[2], challenge[3]
                send_code = True
        if send_code and challenge is not None:
            try:
                self._deliver_code(challenge[0], display, "register", challenge[1], challenge[2])
            except AccountError:
                if created_user:
                    with self.store.transaction() as connection:
                        connection.execute("DELETE FROM account_challenges WHERE id = ?", (challenge[0],))
                        connection.execute("DELETE FROM account_password_credentials WHERE user_id = ?", (user_id,))
                        connection.execute("DELETE FROM account_emails WHERE user_id = ? AND verified_at IS NULL", (user_id,))
                        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
                raise
        return {
            "state": "accepted_if_eligible",
            "reference": reference,
            "display_email": _email_hint(normalized),
            "expires_at": expires_at,
            "resend_at": resend_at,
        }

    def _ensure_delivery_before_lookup(self) -> None:
        # Avoid making provider configuration an account-existence oracle.
        self._require_mailer()

    def verify_email(self, reference: Any, code: Any, *, local_identity: dict | None = None) -> dict:
        now = self._now()
        error: AccountError | None = None
        user_id: str | None = None
        verified_user: dict | None = None
        verified_workspace: dict | None = None
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection, reference=reference, purpose="register", code=code, now=now
            )
            if error is None and challenge is not None and challenge["user_id"] is not None:
                user_id = challenge["user_id"]
                email_row = connection.execute(
                    "SELECT email_normalized, verified_at FROM account_emails WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if email_row is None or email_row["verified_at"] is not None or challenge["subject_hash"] != _subject_hash(email_row["email_normalized"]):
                    error = AccountError("ACCOUNT_NOT_FOUND", "The registration cannot be completed", 404)
                else:
                    connection.execute(
                        "UPDATE account_emails SET verified_at = COALESCE(verified_at, ?), updated_at = ? WHERE user_id = ?",
                        (self._stamp(now), self._stamp(now), user_id),
                    )
                    workspace = self._ensure_personal_workspace(connection, user_id, self._stamp(now))
                    user = connection.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
                    self._event(connection, user_id, "email_verified", self._stamp(now))
                    verified_user = dict(user)
                    verified_workspace = dict(workspace)
            elif error is None:
                error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if error is not None:
            raise error
        return {
            "state": "email_verified",
            "user": verified_user,
            "workspace": verified_workspace,
        }

    def request_email_login(self, email: Any) -> dict:
        self._ensure_delivery_before_lookup()
        normalized, display = _email(email)
        now = self._now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT user_id, verified_at FROM account_emails WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
            if row is None or row["verified_at"] is None or self._deletion_pending(connection, row["user_id"]) is not None:
                reference, expires_at, resend_at = self._record_no_delivery_tx(
                    connection,
                    purpose="email_login",
                    email_normalized=normalized,
                    user_id=None if row is None else row["user_id"],
                    now=now,
                )
                challenge = None
            else:
                challenge = self._issue_code_tx(
                    connection, purpose="email_login", email_normalized=normalized, user_id=row["user_id"], now=now
                )
                reference, expires_at, resend_at = challenge[0], challenge[2], challenge[3]
        if challenge is not None:
            self._deliver_code(challenge[0], display, "email_login", challenge[1], challenge[2])
        return {
            "state": "accepted_if_eligible",
            "reference": reference,
            "display_email": _email_hint(normalized),
            "expires_at": expires_at,
            "resend_at": resend_at,
        }

    def complete_email_login(self, reference: Any, code: Any, *, workspace_id: Any = None, local_identity: dict | None = None) -> dict:
        now = self._now()
        error: AccountError | None = None
        user_id: str | None = None
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection, reference=reference, purpose="email_login", code=code, now=now
            )
            if error is None and challenge is not None and challenge["user_id"] is not None:
                user_id = challenge["user_id"]
                valid_email = connection.execute(
                    "SELECT email_normalized, verified_at FROM account_emails WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if (
                    valid_email is None
                    or valid_email["verified_at"] is None
                    or challenge["subject_hash"] != _subject_hash(valid_email["email_normalized"])
                ):
                    error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
            elif error is None:
                error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if error is not None:
            raise error
        return self._finish_login(user_id, workspace_id, local_identity=local_identity)

    def login_password(self, email: Any, password: Any, *, workspace_id: Any = None, local_identity: dict | None = None) -> dict:
        normalized, _ = _email(email)
        if type(password) is not str or len(password) > PASSWORD_MAX_UTF8_BYTES:
            raise AccountError("INVALID_CREDENTIALS", "Email or password is incorrect", 401)
        password = unicodedata.normalize("NFC", password)
        now = self._now()
        email_hash = _subject_hash(normalized)
        error: AccountError | None = None
        user_id: str | None = None
        with self.store.transaction() as connection:
            cutoff = self._stamp(now - self.policy.login_failure_window)
            failures = connection.execute(
                "SELECT COUNT(*) AS count FROM account_login_failures WHERE subject_hash = ? AND purpose = 'password' AND occurred_at >= ?",
                (email_hash, cutoff),
            ).fetchone()["count"]
            if failures >= self.policy.login_failure_limit:
                error = AccountError("LOGIN_RATE_LIMITED", "Too many sign-in attempts; wait before trying again", 429)
            else:
                row = connection.execute(
                    """SELECT e.user_id, e.verified_at, p.password_hash
                       FROM account_emails e LEFT JOIN account_password_credentials p ON p.user_id = e.user_id
                       WHERE e.email_normalized = ?""",
                    (normalized,),
                ).fetchone()
                if row is None or row["verified_at"] is None or row["password_hash"] is None:
                    dummy_salt = b"\x00" * 16
                    expected = f"pbkdf2_sha256${self.policy.password_iterations}${_b64url_encode(dummy_salt)}${_b64url_encode(_hash_secret('unavailable', dummy_salt, self.policy.password_iterations))}"
                    _verify_password(password, expected)
                    valid = False
                else:
                    valid = _verify_password(password, row["password_hash"])
                if not valid:
                    connection.execute(
                        "INSERT INTO account_login_failures(subject_hash, purpose, occurred_at) VALUES(?, 'password', ?)",
                        (email_hash, self._stamp(now)),
                    )
                    error = AccountError("INVALID_CREDENTIALS", "Email or password is incorrect", 401)
                else:
                    user_id = row["user_id"]
                    if self._deletion_pending(connection, user_id) is not None:
                        error = AccountError("ACCOUNT_DELETION_PENDING", "Account deletion is scheduled; cancel it before signing in", 403)
                    else:
                        connection.execute(
                            "DELETE FROM account_login_failures WHERE subject_hash = ? AND purpose = 'password'",
                            (email_hash,),
                        )
        if error is not None:
            raise error
        return self._finish_login(user_id, workspace_id, local_identity=local_identity)

    def request_password_reset(self, email: Any) -> dict:
        self._ensure_delivery_before_lookup()
        normalized, display = _email(email)
        now = self._now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT user_id, verified_at FROM account_emails WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
            if row is None or row["verified_at"] is None or self._deletion_pending(connection, row["user_id"]) is not None:
                reference, expires_at, resend_at = self._record_no_delivery_tx(
                    connection,
                    purpose="password_reset",
                    email_normalized=normalized,
                    user_id=None if row is None else row["user_id"],
                    now=now,
                )
                challenge = None
            else:
                challenge = self._issue_code_tx(
                    connection, purpose="password_reset", email_normalized=normalized, user_id=row["user_id"], now=now
                )
                reference, expires_at, resend_at = challenge[0], challenge[2], challenge[3]
        if challenge is not None:
            self._deliver_code(challenge[0], display, "password_reset", challenge[1], challenge[2])
        return {
            "state": "accepted_if_eligible",
            "reference": reference,
            "display_email": _email_hint(normalized),
            "expires_at": expires_at,
            "resend_at": resend_at,
        }

    def reset_password(self, reference: Any, code: Any, new_password: Any) -> dict:
        password = _normalize_password(new_password, self.password_blocklist)
        now = self._now()
        error: AccountError | None = None
        result: dict[str, Any] = {"state": "password_not_changed"}
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection, reference=reference, purpose="password_reset", code=code, now=now
            )
            if error is None and challenge is not None and challenge["user_id"] is not None:
                user_id = challenge["user_id"]
                email_row = connection.execute(
                    "SELECT email_normalized, verified_at FROM account_emails WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if (
                    email_row is None
                    or email_row["verified_at"] is None
                    or challenge["subject_hash"] != _subject_hash(email_row["email_normalized"])
                    or self._deletion_pending(connection, user_id) is not None
                ):
                    error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
                else:
                    digest = _hash_password(password, self.policy.password_iterations)
                    connection.execute(
                        """INSERT INTO account_password_credentials(user_id, password_hash, created_at, updated_at)
                           VALUES(?, ?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash, updated_at = excluded.updated_at""",
                        (user_id, digest, self._stamp(now), self._stamp(now)),
                    )
                    revoked = connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                        (self._stamp(now), user_id),
                    ).rowcount
                    self._event(connection, user_id, "password_reset", self._stamp(now))
                    result = {"state": "password_changed", "revoked_sessions": revoked}
            elif error is None:
                error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if error is not None:
            raise error
        return result

    def _active_identity(self, identity: Any, *, recent: bool = False) -> dict:
        if not isinstance(identity, dict) or not isinstance(identity.get("user"), dict) or not isinstance(identity.get("workspace"), dict):
            raise AccountError("UNAUTHENTICATED", "A valid account session is required", 401)
        user_id = identity["user"].get("id")
        workspace_id = identity["workspace"].get("id")
        session_id = identity.get("session_id")
        if not all(type(value) is str and value for value in (user_id, workspace_id, session_id)):
            raise AccountError("UNAUTHENTICATED", "A valid account session is required", 401)
        row = self.store.row(
            """SELECT s.created_at FROM sessions s JOIN memberships m
                   ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
               WHERE s.id = ? AND s.user_id = ? AND s.workspace_id = ?
                 AND s.revoked_at IS NULL AND s.expires_at > ? AND m.active = 1""",
            (session_id, user_id, workspace_id, self._stamp()),
        )
        if row is None:
            raise AccountError("ACCESS_REVOKED", "The account session is no longer active", 401)
        if recent:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if self._now() - created.astimezone(timezone.utc) > self.policy.recent_auth_window:
                raise AccountError("RECENT_AUTH_REQUIRED", "Sign in again before this account change", 403)
        pending = self.store.row(
            "SELECT scheduled_at FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
            (user_id,),
        )
        if pending is not None:
            raise AccountError("ACCOUNT_DELETION_PENDING", "Cancel the scheduled account deletion before changing login methods", 403)
        return {"user_id": user_id, "workspace_id": workspace_id, "session_id": session_id, "created_at": row["created_at"]}

    def begin_oauth(
        self,
        provider: Any,
        *,
        code_verifier: Any,
        intent: str = "login",
        identity: dict | None = None,
    ) -> dict:
        if type(provider) is not str or provider not in SUPPORTED_PROVIDERS:
            raise AccountError("UNSUPPORTED_PROVIDER", "This login provider is not supported", 400)
        config = self.provider_configs.get(provider)
        if config is None:
            raise AccountError("OAUTH_PROVIDER_NOT_CONFIGURED", f"{provider.title()} sign-in is unavailable because no client configuration is installed", 503)
        if type(intent) is not str or intent not in {"login", "link"}:
            raise AccountError("INVALID_INPUT", "OAuth intent is not supported", 400)
        if type(code_verifier) is not str or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
            raise AccountError("INVALID_INPUT", "A PKCE code verifier is required", 400)
        acting_identity = self._active_identity(identity, recent=True) if intent == "link" else None
        local_source = self._local_source_values(identity) if intent == "login" else None
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_challenge = _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        authorization_url = self.oidc.authorization_url(
            config, state=state, nonce=nonce, code_challenge=code_challenge
        )
        now = self._now()
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO account_oauth_transactions(
                       state_hash, provider, intent, user_id, workspace_id, session_id,
                       local_user_id, local_workspace_id, local_session_id,
                       code_challenge, nonce_hash, redirect_uri, created_at, expires_at, status
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    _digest(state), provider, intent,
                    None if acting_identity is None else acting_identity["user_id"],
                    None if acting_identity is None else acting_identity["workspace_id"],
                    None if acting_identity is None else acting_identity["session_id"],
                    None if local_source is None else local_source["local_user_id"],
                    None if local_source is None else local_source["local_workspace_id"],
                    None if local_source is None else local_source["local_session_id"],
                    code_challenge, _subject_hash(nonce), config.redirect_uri,
                    self._stamp(now), self._stamp(now + self.policy.oauth_state_ttl),
                ),
            )
            if acting_identity is not None:
                self._event(connection, acting_identity["user_id"], "oauth_link_started", self._stamp(now))
        return {
            "state": "authorization_started",
            "provider": provider,
            "authorization_url": authorization_url,
            "state_token": state,
            "code_verifier_storage": "client secure transient storage; never URL or localStorage",
            "expires_at": self._stamp(now + self.policy.oauth_state_ttl),
        }

    def complete_oauth(
        self,
        provider: Any,
        *,
        code: Any,
        state: Any,
        code_verifier: Any,
        identity: dict | None = None,
    ) -> dict:
        if type(provider) is not str or provider not in SUPPORTED_PROVIDERS:
            raise AccountError("UNSUPPORTED_PROVIDER", "This login provider is not supported", 400)
        config = self.provider_configs.get(provider)
        if config is None:
            raise AccountError("OAUTH_PROVIDER_NOT_CONFIGURED", f"{provider.title()} sign-in is unavailable because no client configuration is installed", 503)
        if type(code) is not str or not 1 <= len(code) <= 4096 or type(state) is not str or not 32 <= len(state) <= 256:
            raise AccountError("OAUTH_RESPONSE_INVALID", "The identity provider response is invalid", 400)
        if type(code_verifier) is not str or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
            raise AccountError("OAUTH_PKCE_INVALID", "The PKCE verifier is invalid", 400)
        state_hash = _digest(state)
        now = self._now()
        pkce_invalid = False
        callback_mismatch = False
        with self.store.transaction() as connection:
            transaction = connection.execute(
                "SELECT * FROM account_oauth_transactions WHERE state_hash = ? AND provider = ? AND status = 'pending' AND expires_at > ?",
                (state_hash, provider, self._stamp(now)),
            ).fetchone()
            if transaction is None:
                raise AccountError("OAUTH_STATE_INVALID", "The sign-in request is invalid or expired", 401)
            if transaction["redirect_uri"] != config.redirect_uri:
                connection.execute(
                    "UPDATE account_oauth_transactions SET status = 'failed', consumed_at = ? WHERE state_hash = ? AND status = 'pending'",
                    (self._stamp(now), state_hash),
                )
                callback_mismatch = True
            else:
                calculated_challenge = _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
                if not hmac.compare_digest(calculated_challenge, transaction["code_challenge"]):
                    connection.execute(
                        "UPDATE account_oauth_transactions SET status = 'failed', consumed_at = ? WHERE state_hash = ? AND status = 'pending'",
                        (self._stamp(now), state_hash),
                    )
                    pkce_invalid = True
                else:
                    connection.execute(
                        "UPDATE account_oauth_transactions SET status = 'consumed', consumed_at = ? WHERE state_hash = ? AND status = 'pending'",
                        (self._stamp(now), state_hash),
                    )
            transaction = dict(transaction)
        if callback_mismatch:
            raise AccountError("OAUTH_CALLBACK_MISMATCH", "The provider callback changed during sign-in; start again", 409)
        if pkce_invalid:
            raise AccountError("OAUTH_PKCE_INVALID", "The PKCE verifier is invalid", 401)

        # The state is single-use before contacting the provider. A transient
        # exchange error requires a new sign-in request; replay is never allowed.
        id_token = self.oidc.exchange_code(config, code=code, code_verifier=code_verifier)
        claims = self.oidc.verify_id_token(config, id_token, expected_nonce_hash=transaction["nonce_hash"])
        intent = transaction["intent"]
        if intent == "link":
            if identity is not None:
                acting_identity = self._active_identity(identity)
                if (
                    acting_identity["user_id"] != transaction["user_id"]
                    or acting_identity["session_id"] != transaction["session_id"]
                    or acting_identity["workspace_id"] != transaction["workspace_id"]
                ):
                    raise AccountError("OAUTH_SESSION_CHANGED", "The account session changed during sign-in; start again", 409)
            return self._link_verified_identity(transaction, claims)
        return self._login_verified_identity(transaction, claims)

    def _login_verified_identity(self, transaction: dict, claims: dict[str, Any]) -> dict:
        provider, subject = claims["provider"], claims["subject"]
        now = self._now()
        user_id = None
        with self.store.transaction() as connection:
            linked = connection.execute(
                "SELECT user_id FROM account_external_identities WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if linked is not None:
                user_id = linked["user_id"]
                if self._deletion_pending(connection, user_id) is not None:
                    raise AccountError("ACCOUNT_DELETION_PENDING", "Cancel the scheduled account deletion before signing in", 403)
                connection.execute(
                    "UPDATE account_external_identities SET last_authenticated_at = ? WHERE provider = ? AND subject = ?",
                    (self._stamp(now), provider, subject),
                )
                self._event(connection, user_id, "oauth_login", self._stamp(now))
            else:
                if not claims.get("email_verified") or type(claims.get("email")) is not str:
                    raise AccountError("OAUTH_VERIFIED_EMAIL_REQUIRED", "A verified provider email is required to create an account", 403)
                normalized, email_display = _email(claims["email"])
                existing = connection.execute(
                    "SELECT user_id, verified_at FROM account_emails WHERE email_normalized = ?",
                    (normalized,),
                ).fetchone()
                if existing is not None:
                    # Matching email is not proof that two login identities are the same account.
                    raise AccountError("IDENTITY_LINK_REQUIRED", "Sign in to the existing account and explicitly link this provider", 409)
                name = _display_name(claims.get("name"), normalized)
                user_id = str(uuid.uuid4())
                connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, name, self._stamp(now)))
                connection.execute(
                    "INSERT INTO account_emails(user_id, email_normalized, email_display, verified_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (user_id, normalized, email_display, self._stamp(now), self._stamp(now), self._stamp(now)),
                )
                workspace = self._ensure_personal_workspace(connection, user_id, self._stamp(now))
                connection.execute(
                    "INSERT INTO account_external_identities(id, user_id, provider, subject, linked_at, last_authenticated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), user_id, provider, subject, self._stamp(now), self._stamp(now)),
                )
                self._event(connection, user_id, "oauth_account_created", self._stamp(now))
        return self._finish_login(
            user_id,
            transaction.get("workspace_id"),
            local_source=transaction,
        )

    def _link_verified_identity(self, transaction: dict, claims: dict[str, Any]) -> dict:
        provider, subject, user_id = claims["provider"], claims["subject"], transaction["user_id"]
        now = self._now()
        normalized_provider_email = None
        if claims.get("email_verified") and type(claims.get("email")) is str:
            normalized_provider_email, _ = _email(claims["email"])
        with self.store.transaction() as connection:
            session = connection.execute(
                """SELECT 1 FROM sessions s JOIN memberships m ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
                   WHERE s.id = ? AND s.user_id = ? AND s.workspace_id = ?
                     AND s.revoked_at IS NULL AND s.expires_at > ? AND m.active = 1""",
                (transaction["session_id"], user_id, transaction["workspace_id"], self._stamp(now)),
            ).fetchone()
            if session is None or self._deletion_pending(connection, user_id) is not None:
                raise AccountError("ACCESS_REVOKED", "The account session is no longer active", 401)
            existing_identity = connection.execute(
                "SELECT user_id FROM account_external_identities WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if existing_identity is not None and existing_identity["user_id"] != user_id:
                raise AccountError("IDENTITY_ALREADY_LINKED", "This provider identity is already linked to another account", 409)
            if normalized_provider_email is not None:
                owner = connection.execute(
                    "SELECT user_id FROM account_emails WHERE email_normalized = ?",
                    (normalized_provider_email,),
                ).fetchone()
                if owner is not None and owner["user_id"] != user_id:
                    raise AccountError("IDENTITY_EMAIL_CONFLICT", "The provider email belongs to another account; accounts were not merged", 409)
            if existing_identity is None:
                connection.execute(
                    "INSERT INTO account_external_identities(id, user_id, provider, subject, linked_at, last_authenticated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), user_id, provider, subject, self._stamp(now), self._stamp(now)),
                )
            else:
                connection.execute(
                    "UPDATE account_external_identities SET last_authenticated_at = ? WHERE provider = ? AND subject = ? AND user_id = ?",
                    (self._stamp(now), provider, subject, user_id),
                )
            self._event(connection, user_id, "oauth_identity_linked", self._stamp(now))
        return {"state": "identity_linked", "provider": provider}

    def login_methods(self, identity: dict) -> dict:
        active = self._active_identity(identity)
        user_id = active["user_id"]
        email = self.store.row(
            "SELECT email_display, verified_at FROM account_emails WHERE user_id = ?", (user_id,)
        )
        password = self.store.row(
            "SELECT 1 AS present FROM account_password_credentials WHERE user_id = ?", (user_id,)
        )
        external = self.store.rows(
            "SELECT id, provider, linked_at, last_authenticated_at FROM account_external_identities WHERE user_id = ? ORDER BY provider, id",
            (user_id,),
        )
        verified_email = email is not None and email["verified_at"] is not None
        return {
            "email": None if email is None else {
                "address": email["email_display"],
                "verified": verified_email,
                "email_code_login_available": verified_email and self._mailer_ready(),
            },
            "password_enabled": password is not None,
            "external_identities": [
                {**dict(row), "provider_configured": self.provider_configs.get(row["provider"]) is not None}
                for row in external
            ],
            "email_delivery_configured": self._mailer_ready(),
            "oauth_providers_configured": sorted(provider for provider, config in self.provider_configs.items() if config is not None),
        }

    def _usable_method_count(self, connection, user_id: str) -> int:
        methods = connection.execute(
            "SELECT COUNT(*) AS count FROM account_password_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()["count"]
        linked = connection.execute(
            "SELECT provider FROM account_external_identities WHERE user_id = ?", (user_id,)
        ).fetchall()
        methods += sum(self.provider_configs.get(row["provider"]) is not None for row in linked)
        email = connection.execute(
            "SELECT verified_at FROM account_emails WHERE user_id = ?", (user_id,)
        ).fetchone()
        if email is not None and email["verified_at"] is not None and self._mailer_ready():
            methods += 1
        return methods

    def unlink_external_identity(self, identity: dict, external_identity_id: Any) -> dict:
        active = self._active_identity(identity, recent=True)
        if type(external_identity_id) is not str or not external_identity_id:
            raise AccountError("INVALID_INPUT", "An identity identifier is required", 400)
        now = self._stamp()
        with self.store.transaction() as connection:
            target = connection.execute(
                "SELECT provider FROM account_external_identities WHERE id = ? AND user_id = ?",
                (external_identity_id, active["user_id"]),
            ).fetchone()
            if target is None:
                raise AccountError("IDENTITY_NOT_FOUND", "The login method was not found", 404)
            target_is_usable = self.provider_configs.get(target["provider"]) is not None
            if self._usable_method_count(connection, active["user_id"]) - int(target_is_usable) < 1:
                raise AccountError("LAST_LOGIN_METHOD", "Add and verify another login method before removing this one", 409)
            connection.execute(
                "DELETE FROM account_external_identities WHERE id = ? AND user_id = ?",
                (external_identity_id, active["user_id"]),
            )
            self._event(connection, active["user_id"], "oauth_identity_unlinked", now)
        return {"state": "identity_unlinked", "provider": target["provider"]}

    def unlink_email(self, identity: dict) -> dict:
        active = self._active_identity(identity, recent=True)
        with self.store.transaction() as connection:
            email = connection.execute(
                "SELECT email_normalized, verified_at FROM account_emails WHERE user_id = ?", (active["user_id"],)
            ).fetchone()
            if email is None:
                return {"state": "email_not_linked"}
            email_is_usable = email["verified_at"] is not None and self._mailer_ready()
            if self._usable_method_count(connection, active["user_id"]) - int(email_is_usable) < 1:
                raise AccountError("LAST_LOGIN_METHOD", "Add and verify another login method before removing email", 409)
            connection.execute("DELETE FROM account_challenges WHERE user_id = ?", (active["user_id"],))
            connection.execute("DELETE FROM account_emails WHERE user_id = ?", (active["user_id"],))
            self._event(connection, active["user_id"], "email_unlinked", self._stamp())
        return {"state": "email_unlinked"}

    def request_email_change(self, identity: dict, new_email: Any) -> dict:
        self._require_mailer()
        active = self._active_identity(identity, recent=True)
        normalized, display = _email(new_email)
        now = self._now()
        with self.store.transaction() as connection:
            owner = connection.execute(
                "SELECT user_id FROM account_emails WHERE email_normalized = ?", (normalized,)
            ).fetchone()
            if owner is not None and owner["user_id"] != active["user_id"]:
                raise AccountError("EMAIL_ALREADY_LINKED", "This email is already associated with an account", 409)
            challenge = self._issue_code_tx(
                connection, purpose="email_change", email_normalized=normalized, user_id=active["user_id"], now=now
            )
        self._deliver_code(challenge[0], display, "email_change", challenge[1], challenge[2])
        return {
            "state": "email_change_pending",
            "reference": challenge[0],
            "display_email": _email_hint(normalized),
            "expires_at": challenge[2],
            "resend_at": challenge[3],
        }

    def confirm_email_change(self, identity: dict, reference: Any, new_email: Any, code: Any) -> dict:
        active = self._active_identity(identity)
        normalized, display = _email(new_email)
        now = self._now()
        error: AccountError | None = None
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection,
                reference=reference,
                email_normalized=normalized,
                purpose="email_change",
                code=code,
                now=now,
                user_id=active["user_id"],
            )
            if error is None and challenge is not None:
                owner = connection.execute(
                    "SELECT user_id FROM account_emails WHERE email_normalized = ?", (normalized,)
                ).fetchone()
                if owner is not None and owner["user_id"] != active["user_id"]:
                    error = AccountError("EMAIL_ALREADY_LINKED", "This email is already associated with an account", 409)
                else:
                    connection.execute(
                        """INSERT INTO account_emails(user_id, email_normalized, email_display, verified_at, created_at, updated_at)
                           VALUES(?, ?, ?, ?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET email_normalized = excluded.email_normalized,
                               email_display = excluded.email_display, verified_at = excluded.verified_at, updated_at = excluded.updated_at""",
                        (active["user_id"], normalized, display, self._stamp(now), self._stamp(now), self._stamp(now)),
                    )
                    self._event(connection, active["user_id"], "email_changed", self._stamp(now))
            elif error is None:
                error = AccountError("INVALID_VERIFICATION_CODE", "The verification code is invalid or expired", 400)
        if error is not None:
            raise error
        return {"state": "email_changed", "email": display, "verified": True}

    def sessions(self, identity: dict) -> list[dict]:
        active = self._active_identity(identity)
        return list_product_sessions(self.store, active["user_id"])

    def revoke_one_session(self, identity: dict, session_id: Any) -> dict:
        active = self._active_identity(identity)
        if type(session_id) is not str or not session_id:
            raise AccountError("INVALID_INPUT", "A session identifier is required", 400)
        revoked = revoke_product_session(self.store, active["user_id"], session_id, now=self._stamp())
        if not revoked:
            raise AccountError("SESSION_NOT_FOUND", "The session is not active for this account", 404)
        return {"state": "session_revoked", "session_id": session_id}

    def revoke_all_user_sessions(self, identity: dict) -> dict:
        active = self._active_identity(identity)
        revoked = revoke_all_sessions(self.store, active["user_id"], now=self._stamp())
        return {"state": "sessions_revoked", "revoked_count": revoked}

    def logout_current_session(self, identity: dict) -> dict:
        active = self._active_identity(identity)
        revoked = revoke_product_session(
            self.store, active["user_id"], active["session_id"], now=self._stamp()
        )
        return {"state": "session_revoked" if revoked else "session_already_inactive"}

    def change_password(self, identity: dict, current_password: Any, new_password: Any) -> dict:
        active = self._active_identity(identity, recent=True)
        password = _normalize_password(new_password, self.password_blocklist)
        now = self._stamp()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT password_hash FROM account_password_credentials WHERE user_id = ?",
                (active["user_id"],),
            ).fetchone()
            if current is not None:
                if type(current_password) is not str or not _verify_password(unicodedata.normalize("NFC", current_password), current["password_hash"]):
                    raise AccountError("INVALID_CREDENTIALS", "The current password is incorrect", 401)
            elif current_password not in (None, ""):
                raise AccountError("INVALID_CREDENTIALS", "The current password is incorrect", 401)
            connection.execute(
                """INSERT INTO account_password_credentials(user_id, password_hash, created_at, updated_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash, updated_at = excluded.updated_at""",
                (active["user_id"], _hash_password(password, self.policy.password_iterations), now, now),
            )
            revoked = connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, active["user_id"]),
            ).rowcount
            self._event(connection, active["user_id"], "password_changed", now)
        return {"state": "password_changed", "revoked_sessions": revoked}

    @staticmethod
    def _export_safe_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
        return value

    def account_preferences(self, identity: dict) -> dict[str, Any]:
        active = self._active_identity(identity)
        row = self.store.row(
            "SELECT model_improvement_consent, updated_at, version FROM account_preferences WHERE user_id = ?",
            (active["user_id"],),
        )
        if row is None:
            return {"model_improvement_consent": "not_asked", "updated_at": None, "version": 0}
        return dict(row)

    def set_model_improvement_consent(self, identity: dict, consent: Any, expected_version: Any) -> dict[str, Any]:
        active = self._active_identity(identity)
        if type(consent) is not str or consent not in {"granted", "declined", "withdrawn"}:
            raise AccountError("INVALID_INPUT", "Choose whether to allow model-improvement use", 400)
        if type(expected_version) is not int or expected_version < 0:
            raise AccountError("INVALID_INPUT", "A valid preference version is required", 400)
        now = self._stamp()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT model_improvement_consent, version FROM account_preferences WHERE user_id = ?",
                (active["user_id"],),
            ).fetchone()
            current_version = 0 if current is None else current["version"]
            if current_version != expected_version:
                raise AccountError("PREFERENCE_VERSION_CONFLICT", "Account preferences changed; reload before saving", 409)
            if consent == "withdrawn" and (current is None or current["model_improvement_consent"] != "granted"):
                raise AccountError("CONSENT_NOT_GRANTED", "There is no model-improvement consent to withdraw", 409)
            next_version = current_version + 1
            connection.execute(
                """INSERT INTO account_preferences(user_id, model_improvement_consent, updated_at, version)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET model_improvement_consent = excluded.model_improvement_consent,
                       updated_at = excluded.updated_at, version = excluded.version""",
                (active["user_id"], consent, now, next_version),
            )
            self._event(connection, active["user_id"], f"model_improvement_consent_{consent}", now)
        return {"model_improvement_consent": consent, "updated_at": now, "version": next_version, "training_started": False}

    def _account_export_records(self, connection, user_id: str, now: datetime) -> tuple[dict[str, Any], list[tuple[str, str, str | None]]]:
        def rows(table: str, where: str = "1 = 0", parameters: tuple = ()) -> list[dict[str, Any]]:
            if not self._table_exists(connection, table):
                return []
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE {where}", parameters
            ).fetchall()]

        memberships = rows("memberships", "user_id = ?", (user_id,))
        workspace_ids = sorted({row["workspace_id"] for row in memberships})
        workspaces = rows("workspaces", "id IN (%s)" % (",".join("?" for _ in workspace_ids) or "NULL"), tuple(workspace_ids))
        user = connection.execute("SELECT id, name, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        email = connection.execute(
            "SELECT email_display, verified_at, created_at, updated_at FROM account_emails WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        password = connection.execute(
            "SELECT 1 FROM account_password_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()
        external = rows(
            "account_external_identities", "user_id = ?", (user_id,)
        )
        external = [
            {key: value for key, value in row.items() if key not in {"subject"}}
            for row in external
        ]
        preference = rows("account_preferences", "user_id = ?", (user_id,))
        events = rows("account_events", "user_id = ?", (user_id,))
        workspace_invites = rows("account_workspace_invitations", "invited_by = ? OR accepted_user_id = ?", (user_id, user_id))
        membership_events = rows("account_membership_events", "actor_user_id = ? OR subject_user_id = ?", (user_id, user_id))

        def owned(table: str, owner_column: str = "owner_user_id") -> list[dict[str, Any]]:
            if not self._table_exists(connection, table):
                return []
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if owner_column not in columns:
                return []
            if "workspace_id" in columns and workspace_ids:
                return rows(table, f"{owner_column} = ? AND workspace_id IN ({','.join('?' for _ in workspace_ids)})", (user_id, *workspace_ids))
            return rows(table, f"{owner_column} = ?", (user_id,))

        def owned_in_member_workspaces(table: str, owner_column: str) -> list[dict[str, Any]]:
            if not workspace_ids or not self._table_exists(connection, table):
                return []
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if owner_column not in columns or "workspace_id" not in columns:
                return []
            return rows(
                table,
                f"{owner_column} = ? AND workspace_id IN ({','.join('?' for _ in workspace_ids)})",
                (user_id, *workspace_ids),
            )

        works = owned("works")
        materials = owned("materials")
        artifacts = owned("artifacts")
        media_projects = owned("media_projects")
        recordings = owned("media_recording_sessions")
        uploads = owned("media_uploads", "user_id")
        acquisition_query_plans = owned_in_member_workspaces("acquisition_query_plans", "owner_user_id")
        acquisition_query_ids = [row["query_id"] for row in acquisition_query_plans]
        acquisition_query_paths = []
        if acquisition_query_ids and self._table_exists(connection, "acquisition_query_paths"):
            acquisition_query_paths = rows(
                "acquisition_query_paths",
                "query_id IN (%s) AND workspace_id IN (%s) "
                "AND EXISTS (SELECT 1 FROM acquisition_query_plans p "
                "WHERE p.query_id=acquisition_query_paths.query_id "
                "AND p.workspace_id=acquisition_query_paths.workspace_id AND p.owner_user_id=?)"
                % (",".join("?" for _ in acquisition_query_ids),
                   ",".join("?" for _ in workspace_ids)),
                (*acquisition_query_ids, *workspace_ids, user_id),
            )

        def sanitize_acquisition_json(records: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
            from .workspace_data import _safe_json

            for record in records:
                for column in columns:
                    value = record.get(column)
                    if not isinstance(value, str):
                        continue
                    try:
                        parsed = json.loads(value)
                    except (TypeError, json.JSONDecodeError):
                        record[column] = None
                        continue
                    sanitized, _redactions = _safe_json(parsed, field_name=column)
                    record[column] = json.dumps(
                        sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    )

        for path in acquisition_query_paths:
            if path.get("path_kind") != "browser_page":
                continue
            try:
                prior_result = json.loads(path.get("result_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_result = {}
            try:
                selected_ref = json.loads(path.get("source_ref_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                selected_ref = {}
            result_ref = prior_result.get("source_ref") if isinstance(prior_result, dict) else None
            request = {}
            try:
                request = json.loads(path.get("request_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            if not isinstance(selected_ref, dict):
                selected_ref = {}
            if not isinstance(result_ref, dict):
                result_ref = {}
            if not isinstance(request, dict):
                request = {}
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
            if "session_id" not in source_ref and isinstance(target.get("session_id"), str):
                source_ref["session_id"] = target["session_id"]
            if "page_id" not in source_ref and isinstance(target.get("page_id"), str):
                source_ref["page_id"] = target["page_id"]
            if "page_revision" not in source_ref and type(target.get("page_revision")) is int:
                source_ref["page_revision"] = target["page_revision"]
            expires_at = path.get("preview_expires_at")
            preview_expired = isinstance(expires_at, str) and expires_at <= self._stamp(now)
            reason = "BROWSER_PREVIEW_EXPIRED" if preview_expired else "BROWSER_PREVIEW_NOT_RESTORED"
            if path.get("status") in {"not_started", "running", "completed", "complete", "partial"}:
                path["status"] = "unavailable"
                path["stop_reason"] = reason
            path["result_json"] = json.dumps({
                "status": path.get("status", "unavailable"),
                "results": [],
                "coverage": {
                    "status": "expired" if preview_expired else "not_restored",
                    "snapshot_sha256": source_ref.get("snapshot_sha256"),
                },
                "source_ref": source_ref,
                "source_use_state": "NOT_EVALUATED",
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        sanitize_acquisition_json(
            acquisition_query_plans, ("request_json",),
        )
        sanitize_acquisition_json(
            acquisition_query_paths,
            ("source_ref_json", "request_json", "material_refs_json", "result_json",
             "coverage_json", "checkpoint_ref_json"),
        )

        work_ids = [row["id"] for row in works]
        material_ids = [row["id"] for row in materials]
        artifact_ids = [row["id"] for row in artifacts]
        media_ids = [row["id"] for row in media_projects]
        recording_ids = [row["id"] for row in recordings]

        def by_ids(table: str, column: str, ids: list[str]) -> list[dict[str, Any]]:
            if not ids or not self._table_exists(connection, table):
                return []
            return rows(table, f"{column} IN ({','.join('?' for _ in ids)})", tuple(ids))

        def linked_rows(table: str, left_column: str, left_ids: list[str], right_column: str, right_ids: list[str]) -> list[dict[str, Any]]:
            if not self._table_exists(connection, table):
                return []
            clauses, parameters = [], []
            if left_ids:
                clauses.append(f"{left_column} IN ({','.join('?' for _ in left_ids)})")
                parameters.extend(left_ids)
            if right_ids:
                clauses.append(f"{right_column} IN ({','.join('?' for _ in right_ids)})")
                parameters.extend(right_ids)
            return rows(table, " OR ".join(clauses) if clauses else "1 = 0", tuple(parameters))

        work_versions = by_ids("work_versions", "work_id", work_ids)
        material_segments = by_ids("material_segments", "material_id", material_ids)
        work_materials = linked_rows("work_materials", "work_id", work_ids, "material_id", material_ids)
        work_artifacts = linked_rows("work_artifacts", "work_id", work_ids, "artifact_id", artifact_ids)
        artifact_versions = by_ids("artifact_versions", "artifact_id", artifact_ids)
        media_versions = by_ids("media_project_versions", "project_id", media_ids)
        recording_segments = by_ids("media_recording_segments", "session_id", recording_ids)
        media_renders = by_ids("media_renders", "project_id", media_ids)
        artifact_exports = by_ids("exports", "artifact_id", artifact_ids)
        business_export_views = by_ids(
            "business_export_views", "export_id", [row["id"] for row in artifact_exports]
        )
        jobs = []
        if self._table_exists(connection, "jobs"):
            if workspace_ids:
                jobs = rows("jobs", "user_id = ? AND workspace_id IN (%s)" % ",".join("?" for _ in workspace_ids), (user_id, *workspace_ids))
            else:
                jobs = rows("jobs", "user_id = ?", (user_id,))
        transcription_job_ids = [item["id"] for item in jobs if item.get("kind") == "media_transcribe"]
        media_transcriptions = by_ids("media_transcriptions", "job_id", transcription_job_ids)
        media_transcription_chunks = by_ids("media_transcription_chunks", "job_id", transcription_job_ids)

        browser_sessions = []
        for item in owned("browser_sessions"):
            # External platform cookies and per-session authorization material
            # are deliberately never exported. Remove query strings because
            # some destinations use them for credentials.
            raw_url = item.get("current_url") or ""
            try:
                parsed = urlsplit(raw_url)
                item["current_url"] = parsed._replace(query="", fragment="").geturl()
            except ValueError:
                item["current_url"] = ""
            item = {
                key: item[key]
                for key in ("id", "workspace_id", "work_id", "engine", "status", "version", "current_url", "current_title", "created_at", "updated_at", "closed_at", "last_error_code")
                if key in item
            }
            browser_sessions.append(item)

        browser_session_ids = {item["id"] for item in browser_sessions}
        browser_operations = []
        for item in owned("browser_session_operations"):
            if item.get("session_id") not in browser_session_ids:
                continue
            item["preview_json"] = None
            browser_operations.append(item)

        commission_members = rows("commission_members", "user_id = ?", (user_id,))
        commission_ids = [row["commission_id"] for row in commission_members]
        commissions = by_ids("client_commissions", "id", commission_ids)
        commission_versions = by_ids("commission_versions", "commission_id", commission_ids)
        commission_members = [row for row in commission_members if row["commission_id"] in set(commission_ids)]
        publication_rows = rows("publications", "author_id = ?", (user_id,))
        publication_ids = [row["id"] for row in publication_rows]
        publication_versions = by_ids("publication_versions", "publication_id", publication_ids)
        publication_releases = by_ids("publication_releases", "publication_id", publication_ids)
        comments = rows("artifact_comments", "author_id = ?", (user_id,))
        comment_versions = by_ids("artifact_comment_versions", "comment_id", [row["id"] for row in comments])

        files: list[tuple[str, str, str | None]] = []

        def add_file_records(records: list[dict[str, Any]], source_column: str, name: str, expected_column: str | None) -> None:
            for item in records:
                raw_path = item.get(source_column)
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                archive_name = name.format(id=item["id"], filename=Path(item.get("filename") or "source.bin").name)
                item[source_column] = archive_name
                expected = item.get(expected_column) if expected_column else None
                files.append((raw_path, archive_name, expected if isinstance(expected, str) else None))

        add_file_records(materials, "relative_path", "files/materials/{id}/{filename}", "sha256")
        add_file_records(media_renders, "relative_path", "files/media-renders/{id}.%s" % "bin", "sha256")
        add_file_records(artifact_exports, "relative_path", "files/artifact-exports/{id}.bin", "sha256")
        for upload in uploads:
            add_file_records([upload], "stage_relative_path", "files/upload-staging/{id}.part", None)
            add_file_records([upload], "original_relative_path", "files/upload-originals/{id}.bin", None)

        safe_records = {
            "account": {
                "user": None if user is None else dict(user),
                "email": None if email is None else dict(email),
                "password_login_enabled": password is not None,
                "external_login_methods": external,
                "preferences": preference,
                "events": events,
            "workspace_invitations": workspace_invites,
            "membership_events": membership_events,
            },
            "workspaces": workspaces,
            "memberships": memberships,
            "works": works,
            "work_versions": work_versions,
            "materials": materials,
            "material_segments": material_segments,
            "work_materials": work_materials,
            "artifacts": artifacts,
            "artifact_versions": artifact_versions,
            "work_artifacts": work_artifacts,
            "jobs": jobs,
            "artifact_exports": artifact_exports,
            "business_export_views": business_export_views,
            "media_projects": media_projects,
            "media_project_versions": media_versions,
            "media_recording_sessions": recordings,
            "media_recording_segments": recording_segments,
            "media_renders": media_renders,
            "media_transcriptions": media_transcriptions,
            "media_transcription_chunks": media_transcription_chunks,
            "media_uploads": uploads,
            "browser_sessions": browser_sessions,
            "browser_session_operations": browser_operations,
            "acquisition_query_plans": acquisition_query_plans,
            "acquisition_query_paths": acquisition_query_paths,
            "client_commissions": commissions,
            "commission_memberships": commission_members,
            "commission_versions": commission_versions,
            "publications": publication_rows,
            "publication_versions": publication_versions,
            "publication_releases": publication_releases,
            "artifact_comments": comments,
            "artifact_comment_versions": comment_versions,
        }
        secrets_to_exclude = {
            "token_hash", "code_hash", "code_salt", "password_hash", "subject", "email_normalized",
            "state_ciphertext", "state_protection", "nonce_hash", "access_token",
            "refresh_token", "client_secret", "code_verifier",
        }

        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items() if key not in secrets_to_exclude}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return self._export_safe_value(value)

        manifest = {
            "schema": "oevira.account-export.v1",
            "created_at": self._stamp(now),
            "expires_at": self._stamp(now + ACCOUNT_EXPORT_LIFETIME),
            "scope": "account_owned_records_and_files_in_member_workspaces",
            "excluded": [
                "passwords_and_verification_codes", "session_tokens",
                "external_platform_browser_authentication_state", "browser_page_query_preview_bodies", "browser_context_preview_bodies",
            ],
        }
        return {"manifest": manifest, **scrub(safe_records)}, files

    def _write_account_export(self, user_id: str) -> dict[str, Any]:
        now = self._now()
        self._prune_expired_account_exports(user_id, now)
        export_id = str(uuid.uuid4())
        relative_path = f"account-exports/{export_id}.zip"
        target_candidate = self.store.root / relative_path
        if target_candidate.is_symlink():
            raise AccountError("ACCOUNT_EXPORT_UNAVAILABLE", "The account export path is unavailable", 503)
        target = target_candidate.resolve()
        if not target.is_relative_to(self.store.root):
            raise AccountError("ACCOUNT_EXPORT_UNAVAILABLE", "The account export path is unavailable", 503)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.store.transaction(immediate=False) as connection:
            records, files = self._account_export_records(connection, user_id, now)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{export_id}-", suffix=".tmp", dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", json.dumps(records["manifest"], ensure_ascii=False, indent=2).encode("utf-8"))
                for name, value in records.items():
                    if name == "manifest":
                        continue
                    archive.writestr(f"records/{name}.json", json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
                for stored_path, archive_name, expected_sha256 in files:
                    source_candidate = self.store.root / stored_path
                    if source_candidate.is_symlink():
                        raise AccountError("ACCOUNT_EXPORT_SOURCE_UNAVAILABLE", "An owned source file is unavailable; the incomplete export was discarded", 409)
                    source = source_candidate.resolve()
                    if not source.is_relative_to(self.store.root) or not source.is_file():
                        raise AccountError("ACCOUNT_EXPORT_SOURCE_UNAVAILABLE", "An owned source file is unavailable; the incomplete export was discarded", 409)
                    digest = hashlib.sha256()
                    with source.open("rb") as input_stream, archive.open(archive_name, "w") as output_stream:
                        while chunk := input_stream.read(1024 * 1024):
                            digest.update(chunk)
                            output_stream.write(chunk)
                    if expected_sha256 and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                        raise AccountError("ACCOUNT_EXPORT_SOURCE_CHANGED", "An owned source changed while the export was being prepared", 409)
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            file_hash = hashlib.sha256()
            size_bytes = 0
            with temporary.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    file_hash.update(chunk)
                    size_bytes += len(chunk)
            os.replace(temporary, target)
            created_at = self._stamp(now)
            expires_at = self._stamp(now + ACCOUNT_EXPORT_LIFETIME)
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO account_exports(id, user_id, created_at, expires_at, relative_path, sha256, size_bytes) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (export_id, user_id, created_at, expires_at, relative_path, file_hash.hexdigest(), size_bytes),
                )
                self._event(connection, user_id, "account_export_created", created_at)
            return {
                "state": "export_ready",
                "export_id": export_id,
                "download_url": f"/api/account/exports/{export_id}/download",
                "created_at": created_at,
                "expires_at": expires_at,
                "sha256": file_hash.hexdigest(),
                "size_bytes": size_bytes,
                "scope": records["manifest"]["scope"],
            }
        except AccountError:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise AccountError("ACCOUNT_EXPORT_UNAVAILABLE", "The account export could not be prepared", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def create_account_export(self, identity: dict) -> dict:
        active = self._active_identity(identity, recent=True)
        return self._write_account_export(active["user_id"])

    def _prune_expired_account_exports(self, user_id: str, now: datetime) -> int:
        now_text = self._stamp(now)
        expired = self.store.rows(
            "SELECT id, relative_path FROM account_exports WHERE user_id = ? AND expires_at <= ?",
            (user_id, now_text),
        )
        if not expired:
            return 0
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE account_exports SET revoked_at = ? WHERE user_id = ? AND expires_at <= ? AND revoked_at IS NULL",
                (now_text, user_id, now_text),
            )
        for row in expired:
            candidate = self.store.root / row["relative_path"]
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved.is_relative_to(self.store.root) and resolved.is_file():
                try:
                    resolved.unlink()
                except OSError:
                    # The expired link is already revoked; a later sweep can
                    # retry removing its inaccessible file.
                    pass
        return len(expired)

    def purge_expired_account_exports(self, *, now: datetime | None = None) -> dict[str, int]:
        current = now or self._now()
        cutoff = self._stamp(current)
        protections_released = self._release_expired_backup_protections(current)
        users = self.store.rows(
            "SELECT DISTINCT user_id FROM account_exports WHERE expires_at <= ?",
            (cutoff,),
        )
        removed = sum(self._prune_expired_account_exports(row["user_id"], current) for row in users)
        return {
            "expired_exports_revoked_or_removed": removed,
            "expired_backup_protections_released": protections_released,
        }

    def account_export_download(self, identity: dict, export_id: Any) -> dict:
        active = self._active_identity(identity)
        self._prune_expired_account_exports(active["user_id"], self._now())
        if type(export_id) is not str or not re.fullmatch(r"[0-9a-f-]{36}", export_id):
            raise AccountError("ACCOUNT_EXPORT_NOT_FOUND", "The account export is unavailable", 404)
        row = self.store.row(
            "SELECT * FROM account_exports WHERE id = ? AND user_id = ? AND revoked_at IS NULL AND expires_at > ?",
            (export_id, active["user_id"], self._stamp()),
        )
        if row is None:
            raise AccountError("ACCOUNT_EXPORT_NOT_FOUND", "The account export is unavailable or expired", 404)
        path_candidate = self.store.root / row["relative_path"]
        if path_candidate.is_symlink():
            raise AccountError("ACCOUNT_EXPORT_FILE_UNAVAILABLE", "The account export file is unavailable", 410)
        path = path_candidate.resolve()
        if not path.is_relative_to(self.store.root) or not path.is_file():
            raise AccountError("ACCOUNT_EXPORT_FILE_UNAVAILABLE", "The account export file is unavailable", 410)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size != row["size_bytes"] or not hmac.compare_digest(digest.hexdigest(), row["sha256"]):
            raise AccountError("ACCOUNT_EXPORT_FILE_CHANGED", "The account export failed its integrity check", 410)
        return {"path": path, "expires_at": row["expires_at"], "sha256": row["sha256"], "size_bytes": size}

    def account_export_metadata(self, identity: dict) -> dict:
        """Describe the owned-data export, excluding secrets and platform auth state."""
        active = self._active_identity(identity)
        user = self.store.row("SELECT id, name, created_at FROM users WHERE id = ?", (active["user_id"],))
        return {
            "scope": "account_owned_records_and_files_in_member_workspaces",
            "user": dict(user) if user is not None else None,
            "export_lifetime_seconds": int(ACCOUNT_EXPORT_LIFETIME.total_seconds()),
            "external_platform_browser_authentication_state": "EXCLUDED",
        }

    def _deletion_preflight(self, connection, user_id: str) -> list[dict[str, Any]]:
        _successors, workspace_ids = self._workspace_successors(connection, user_id)

        blockers: list[dict[str, Any]] = []
        local_identity = connection.execute(
            "SELECT user_id, workspace_id FROM local_identity WHERE singleton = 1"
        ).fetchone()
        for workspace_id in sorted(workspace_ids):
            active_others = connection.execute(
                "SELECT user_id FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 ORDER BY role = 'owner' DESC, created_at, user_id",
                (workspace_id, user_id),
            ).fetchall()
            if (
                not active_others
                and local_identity is not None
                and local_identity["workspace_id"] == workspace_id
                and local_identity["user_id"] != user_id
            ):
                blockers.append({
                    "kind": "local_identity_owned_by_another_user",
                    "workspace_id": workspace_id,
                    "resolution": "Resolve the existing local identity before deleting this private workspace.",
                })
            if active_others:
                remaining_owner = connection.execute(
                    "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' LIMIT 1",
                    (workspace_id, user_id),
                ).fetchone()
                if remaining_owner is None:
                    blockers.append({
                        "kind": "last_workspace_owner",
                        "workspace_id": workspace_id,
                        "resolution": "Transfer workspace ownership to another active member before requesting account deletion.",
                    })

        if self._table_exists(connection, "client_commissions") and self._table_exists(connection, "commission_members"):
            from .api import ApiError
            from .collaboration import prepare_account_deletion as prepare_commission_deletion

            for workspace_id in sorted(workspace_ids):
                other_members = connection.execute(
                    "SELECT 1 FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 LIMIT 1",
                    (workspace_id, user_id),
                ).fetchone()
                if other_members is None:
                    continue
                replacement = connection.execute(
                    "SELECT user_id FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' ORDER BY created_at, user_id LIMIT 1",
                    (workspace_id, user_id),
                ).fetchone()
                if replacement is None:
                    continue
                try:
                    prepare_commission_deletion(
                        connection, workspace_id, user_id, replacement["user_id"], dry_run=True
                    )
                except ApiError as exc:
                    blockers.append({
                        "kind": "commission_ownership_transfer",
                        "workspace_id": workspace_id,
                        "reason": exc.code,
                        "resolution": str(exc),
                    })

        if self._table_exists(connection, "browser_sessions"):
            sessions = connection.execute(
                "SELECT COUNT(*) AS count FROM browser_sessions WHERE owner_user_id = ? AND status IN ('starting', 'open', 'reopening')",
                (user_id,),
            ).fetchone()["count"]
            close_pending = connection.execute(
                "SELECT COUNT(*) AS count FROM browser_sessions WHERE owner_user_id = ? AND last_error_code = 'ACCOUNT_DELETION_CLOSE_PENDING'",
                (user_id,),
            ).fetchone()["count"]
            unknown_operations = 0
            if self._table_exists(connection, "browser_session_operations"):
                unknown_operations = connection.execute(
                    """SELECT COUNT(*) AS count FROM browser_session_operations o
                       JOIN browser_sessions s ON s.id = o.session_id AND s.workspace_id = o.workspace_id
                       WHERE o.owner_user_id = ? AND (o.status = 'pending'
                         OR (o.status = 'unknown' AND s.status IN ('starting', 'open', 'reopening', 'suspended', 'error')))""",
                    (user_id,),
                ).fetchone()["count"]
            if sessions or unknown_operations or close_pending:
                blockers.append({
                    "kind": "active_browser_write",
                    "count": sessions,
                    "uncertain_operations": unknown_operations,
                    "close_pending": close_pending,
                    "resolution": "Close your active browser sessions, reconcile uncertain actions and confirm browser shutdown before scheduling or finalizing deletion.",
                })
        if self._table_exists(connection, "media_recording_sessions"):
            recordings = connection.execute(
                "SELECT COUNT(*) AS count FROM media_recording_sessions WHERE owner_user_id = ? AND status IN ('armed', 'awaiting_first_segment', 'recording', 'paused', 'interrupted', 'stopping')",
                (user_id,),
            ).fetchone()["count"]
            if recordings:
                blockers.append({
                    "kind": "active_recording_write",
                    "count": recordings,
                    "resolution": "Finish or cancel your recording sessions before scheduling deletion.",
                })
        if self._table_exists(connection, "media_uploads"):
            uploads = connection.execute(
                "SELECT COUNT(*) AS count FROM media_uploads WHERE user_id = ? AND status IN ('receiving', 'finalizing', 'interrupted')",
                (user_id,),
            ).fetchone()["count"]
            if uploads:
                blockers.append({
                    "kind": "unfinished_upload",
                    "count": uploads,
                    "resolution": "Complete or cancel your unfinished uploads before scheduling deletion.",
                })
        return blockers

    @staticmethod
    def _sql_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def _delete_related_rows(self, connection, roots: list[tuple[str, Any]]) -> list[str]:
        """Delete one exact object graph using declared SQLite foreign keys."""
        catalog: dict[str, dict[str, Any]] = {}
        children: dict[str, list[tuple[str, tuple[tuple[str, str], ...]]]] = {}
        for item in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            name, create_sql = item["name"], item["sql"] or ""
            if "VIRTUAL TABLE" in create_sql.upper():
                continue
            quoted = self._sql_identifier(name)
            info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            primary_key = tuple(row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"])
            foreign_keys: dict[int, list[Any]] = {}
            for foreign_key in connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                foreign_keys.setdefault(foreign_key["id"], []).append(foreign_key)
            grouped = []
            for foreign_key_rows in foreign_keys.values():
                ordered = sorted(foreign_key_rows, key=lambda row: row["seq"])
                parent = ordered[0]["table"]
                parent_columns = tuple(row["to"] or "" for row in ordered)
                child_columns = tuple(row["from"] for row in ordered)
                catalog[name] = {"pk": primary_key, "columns": {row["name"] for row in info}}
                grouped.append((parent, tuple(zip(child_columns, parent_columns))))
                children.setdefault(parent, []).append((name, tuple(zip(child_columns, parent_columns))))
            catalog.setdefault(name, {"pk": primary_key, "columns": {row["name"] for row in info}})

        selected: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}

        def include(table: str, raw_row: Any, depth: int) -> None:
            if table == "users" or table not in catalog:
                return
            row = dict(raw_row)
            metadata = catalog[table]
            pk = metadata["pk"]
            if pk:
                key = tuple(row[column] for column in pk)
                predicate = " AND ".join(f"{self._sql_identifier(column)} IS ?" for column in pk)
                parameters = key
            else:
                key = (row["__oevira_rowid"],)
                predicate = "rowid IS ?"
                parameters = key
            table_rows = selected.setdefault(table, {})
            existing = table_rows.get(key)
            if existing is not None:
                existing["depth"] = max(existing["depth"], depth)
                return
            table_rows[key] = {"row": row, "depth": depth, "predicate": predicate, "parameters": parameters}
            for child_table, column_pairs in children.get(table, []):
                if child_table == "users" or child_table not in catalog:
                    continue
                child_predicate = " AND ".join(
                    f"{self._sql_identifier(child_column)} IS ?" for child_column, _ in column_pairs
                )
                parent_values = tuple(
                    row[parent_column] if parent_column else row[catalog[table]["pk"][index]]
                    for index, (_child_column, parent_column) in enumerate(column_pairs)
                )
                try:
                    child_rows = connection.execute(
                        f"SELECT rowid AS __oevira_rowid, * FROM {self._sql_identifier(child_table)} WHERE {child_predicate}",
                        parent_values,
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for child_row in child_rows:
                    include(child_table, child_row, depth + 1)

        for table, root in roots:
            if table not in catalog:
                continue
            if isinstance(root, dict):
                include(table, root, 0)
            else:
                include(table, root, 0)

        file_columns = {
            "materials": ("relative_path",),
            "exports": ("relative_path",),
            "media_renders": ("relative_path", "staging_path"),
            "media_uploads": ("stage_relative_path", "original_relative_path"),
        }
        files: set[str] = set()
        for table, entries in selected.items():
            for entry in entries.values():
                row = entry["row"]
                for column in file_columns.get(table, ()):
                    path = row.get(column)
                    if isinstance(path, str) and path:
                        files.add(path)

        connection.execute("PRAGMA defer_foreign_keys = ON")
        ordered_rows = sorted(
            (
                (table, entry)
                for table, entries in selected.items()
                for entry in entries.values()
            ),
            key=lambda item: (-item[1]["depth"], item[0], repr(item[1]["parameters"])),
        )
        for table, entry in ordered_rows:
            connection.execute(
                f"DELETE FROM {self._sql_identifier(table)} WHERE {entry['predicate']}",
                entry["parameters"],
            )
        return sorted(files)

    def _workspace_successors(self, connection, user_id: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
        workspace_ids = {
            row["workspace_id"]
            for row in connection.execute("SELECT workspace_id FROM memberships WHERE user_id = ?", (user_id,)).fetchall()
        }
        tables = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for table in tables:
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({self._sql_identifier(table)})")}
            if "workspace_id" not in columns:
                continue
            user_columns = {
                row["from"] for row in connection.execute(
                    f"PRAGMA foreign_key_list({self._sql_identifier(table)})"
                ).fetchall() if row["table"] == "users"
            }
            for user_column in user_columns:
                workspace_ids.update(
                    row["workspace_id"] for row in connection.execute(
                        f"SELECT DISTINCT workspace_id FROM {self._sql_identifier(table)} WHERE {self._sql_identifier(user_column)} = ?",
                        (user_id,),
                    ).fetchall()
                )
        successors: dict[str, dict[str, Any]] = {}
        for workspace_id in sorted(workspace_ids):
            owners = connection.execute(
                "SELECT user_id, created_at FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' ORDER BY created_at, user_id",
                (workspace_id, user_id),
            ).fetchall()
            others = connection.execute(
                "SELECT user_id FROM memberships WHERE workspace_id = ? AND user_id <> ? AND active = 1 ORDER BY created_at, user_id",
                (workspace_id, user_id),
            ).fetchall()
            successors[workspace_id] = {
                "shared": bool(others),
                "owner_user_id": None if not owners else owners[0]["user_id"],
            }
        return successors, workspace_ids

    def _purge_account_owned_data_tx(self, connection, user_id: str) -> dict[str, Any]:
        """Remove private workspaces and transfer shared current ownership safely."""
        blockers = self._deletion_preflight(connection, user_id)
        if blockers:
            raise _DeletionBlocked("workspace_or_session_handoff_required")
        if self._table_exists(connection, "acquisition_query_plans"):
            # Query paths cascade from their private plan. Remove both before
            # workspaces or the tombstoned user are processed.
            connection.execute(
                "DELETE FROM acquisition_query_plans WHERE owner_user_id = ?",
                (user_id,),
            )
        successors, workspace_ids = self._workspace_successors(connection, user_id)
        local_identity = connection.execute(
            "SELECT user_id, workspace_id FROM local_identity WHERE singleton = 1"
        ).fetchone()
        if local_identity is not None and local_identity["user_id"] == user_id:
            connection.execute("DELETE FROM local_identity WHERE singleton = 1")

        files: set[str] = set()
        private_workspaces = [
            workspace_id for workspace_id, item in successors.items() if not item["shared"]
        ]
        for workspace_id in private_workspaces:
            row = connection.execute("SELECT rowid AS __oevira_rowid, * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if row is not None:
                files.update(self._delete_related_rows(connection, [("workspaces", row)]))

        shared_workspaces = [
            workspace_id for workspace_id, item in successors.items() if item["shared"]
        ]
        community_cleanup_done = False
        for workspace_id in shared_workspaces:
            successor = successors[workspace_id]["owner_user_id"]
            if not successor:
                raise _DeletionBlocked("shared_workspace_last_owner")

            self._revoke_workspace_connections(connection, workspace_id, user_id, self._stamp())

            if self._table_exists(connection, "account_workspace_invitations"):
                connection.execute(
                    """UPDATE account_workspace_invitations SET status = 'revoked',
                           delivery_status = 'revoked', revoked_at = COALESCE(revoked_at, ?), revoked_by = ?
                       WHERE workspace_id = ? AND invited_by = ?
                         AND status IN ('pending', 'delivery_failed', 'delivery_unknown')""",
                    (self._stamp(), successor, workspace_id, user_id),
                )

            from .api import ApiError
            from .collaboration import prepare_account_deletion as prepare_commission_deletion
            from .community import prepare_account_deletion as prepare_community_deletion

            try:
                if self._table_exists(connection, "client_commissions"):
                    prepare_commission_deletion(connection, workspace_id, user_id, successor)
                if self._table_exists(connection, "publications"):
                    prepare_community_deletion(
                        connection,
                        workspace_id,
                        user_id,
                        successor,
                        user_id,
                        private_workspace=False,
                    )
                    community_cleanup_done = True
            except ApiError as exc:
                raise _DeletionBlocked(exc.code) from exc

            def work_successor(work_id: str | None) -> str:
                if work_id and self._table_exists(connection, "work_commissions") and self._table_exists(connection, "commission_members"):
                    commission = connection.execute(
                        "SELECT commission_id FROM work_commissions WHERE work_id = ?", (work_id,)
                    ).fetchone()
                    if commission is not None:
                        owner = connection.execute(
                            "SELECT user_id FROM commission_members WHERE commission_id = ? AND user_id <> ? AND active = 1 AND role = 'owner' ORDER BY user_id LIMIT 1",
                            (commission["commission_id"], user_id),
                        ).fetchone()
                        if owner is None:
                            raise _DeletionBlocked("commission_successor_missing")
                        return owner["user_id"]
                return successor

            for row in connection.execute(
                "SELECT id FROM works WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                (workspace_id, user_id),
            ).fetchall():
                target_user = work_successor(row["id"])
                connection.execute("UPDATE works SET owner_user_id = ? WHERE id = ?", (target_user, row["id"]))
            for row in connection.execute(
                "SELECT id, work_id FROM materials WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                (workspace_id, user_id),
            ).fetchall():
                connection.execute(
                    "UPDATE materials SET owner_user_id = ? WHERE id = ?",
                    (work_successor(row["work_id"]), row["id"]),
                )
            for row in connection.execute(
                "SELECT id, work_id FROM artifacts WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                (workspace_id, user_id),
            ).fetchall():
                connection.execute(
                    "UPDATE artifacts SET owner_user_id = ? WHERE id = ?",
                    (work_successor(row["work_id"]), row["id"]),
                )
            if self._table_exists(connection, "media_projects"):
                for row in connection.execute(
                    "SELECT id, work_id FROM media_projects WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (workspace_id, user_id),
                ).fetchall():
                    connection.execute(
                        "UPDATE media_projects SET owner_user_id = ? WHERE id = ?",
                        (work_successor(row["work_id"]), row["id"]),
                    )
            if self._table_exists(connection, "media_recording_sessions"):
                for row in connection.execute(
                    "SELECT id, work_id FROM media_recording_sessions WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (workspace_id, user_id),
                ).fetchall():
                    connection.execute(
                        "UPDATE media_recording_sessions SET owner_user_id = ? WHERE id = ?",
                        (work_successor(row["work_id"]), row["id"]),
                    )
            if self._table_exists(connection, "monitor_rules"):
                for row in connection.execute(
                    "SELECT id, work_id FROM monitor_rules WHERE workspace_id = ? AND owner_user_id = ? ORDER BY id",
                    (workspace_id, user_id),
                ).fetchall():
                    connection.execute(
                        "UPDATE monitor_rules SET owner_user_id = ? WHERE id = ?",
                        (work_successor(row["work_id"]), row["id"]),
                    )
            if self._table_exists(connection, "media_uploads"):
                connection.execute(
                    "UPDATE media_uploads SET user_id = ?, updated_at = ? WHERE workspace_id = ? AND user_id = ? AND status = 'completed'",
                    (successor, self._stamp(), workspace_id, user_id),
                )
                incomplete = connection.execute(
                    "SELECT rowid AS __oevira_rowid, * FROM media_uploads WHERE workspace_id = ? AND user_id = ? AND status IN ('cancelled', 'failed')",
                    (workspace_id, user_id),
                ).fetchall()
                files.update(self._delete_related_rows(connection, [("media_uploads", row) for row in incomplete]))

            for table in ("browser_sessions", "browser_history", "browser_bookmarks"):
                if not self._table_exists(connection, table):
                    continue
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({self._sql_identifier(table)})")}
                user_column = next((column for column in ("owner_user_id", "user_id", "follower_id", "author_id") if column in columns), None)
                if user_column is None:
                    continue
                roots = connection.execute(
                    f"SELECT rowid AS __oevira_rowid, * FROM {self._sql_identifier(table)} WHERE {self._sql_identifier(user_column)} = ?",
                    (user_id,),
                ).fetchall()
                files.update(self._delete_related_rows(connection, [(table, row) for row in roots]))

            for table, owner_column in (
                ("intelligence_conversations", "owner_user_id"),
                ("intelligence_memories", "owner_user_id"),
                ("intelligence_model_connections", "owner_user_id"),
                ("intelligence_model_events", "owner_user_id"),
                ("intelligence_skill_candidates", "created_by"),
            ):
                if not self._table_exists(connection, table):
                    continue
                rows = connection.execute(
                    f"SELECT rowid AS __oevira_rowid, * FROM {self._sql_identifier(table)} WHERE {self._sql_identifier(owner_column)} = ?",
                    (user_id,),
                ).fetchall()
                files.update(self._delete_related_rows(connection, [(table, row) for row in rows]))

            if self._table_exists(connection, "intelligence_model_admissions") and self._table_exists(connection, "intelligence_model_connections"):
                connections = connection.execute(
                    "SELECT connection_id FROM intelligence_model_connections WHERE workspace_id = ? AND owner_user_id = ?",
                    (workspace_id, user_id),
                ).fetchall()
                if connections:
                    ids = [row["connection_id"] for row in connections]
                    connection.execute(
                        f"DELETE FROM intelligence_model_admissions WHERE connection_id IN ({','.join('?' for _ in ids)})",
                        tuple(ids),
                    )

            if self._table_exists(connection, "jobs"):
                connection.execute(
                    """UPDATE jobs SET status = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN 'cancelled' ELSE status END,
                           cancel_requested = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN 1 ELSE cancel_requested END,
                           lease_generation = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN lease_generation + 1 ELSE lease_generation END,
                           payload_json = '{}', error_message = NULL,
                           progress_label = 'Processing details removed with account deletion', updated_at = ?
                       WHERE workspace_id = ? AND user_id = ?""",
                    (self._stamp(), workspace_id, user_id),
                )
                if self._table_exists(connection, "media_transcriptions"):
                    transcription_job_ids = [
                        row["id"] for row in connection.execute(
                            "SELECT id FROM jobs WHERE workspace_id = ? AND user_id = ?",
                            (workspace_id, user_id),
                        ).fetchall()
                    ]
                    if transcription_job_ids:
                        placeholders = ",".join("?" for _ in transcription_job_ids)
                        if self._table_exists(connection, "media_transcription_chunks"):
                            connection.execute(
                                f"DELETE FROM media_transcription_chunks WHERE job_id IN ({placeholders})",
                                tuple(transcription_job_ids),
                            )
                        connection.execute(
                            f"DELETE FROM media_transcriptions WHERE job_id IN ({placeholders})",
                            tuple(transcription_job_ids),
                        )


        if not community_cleanup_done and self._table_exists(connection, "publications"):
            from .community import prepare_account_deletion as prepare_community_deletion
            prepare_community_deletion(
                connection, "", user_id, user_id, user_id, private_workspace=True
            )

        # Shared-workspace media captures are current records, while individual
        # edit/version creators remain the original user tombstone.
        tombstone = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if tombstone is None:
            raise _DeletionBlocked("account_identity_missing")
        connection.execute("UPDATE users SET name = '已删除用户' WHERE id = ?", (user_id,))
        return {"confirmed": True, "files_to_delete": sorted(files)}

    def _remove_user_session_dependents(self, connection, user_id: str) -> None:
        if self._table_exists(connection, "sessions"):
            session_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM sessions WHERE user_id = ?", (user_id,)
                ).fetchall()
            ]
        else:
            session_ids = []
        if self._table_exists(connection, "browser_sessions"):
            browser_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM browser_sessions WHERE owner_user_id = ?",
                    (user_id,),
                ).fetchall()
            ]
            if browser_ids:
                placeholders = ",".join("?" for _ in browser_ids)
                if self._table_exists(connection, "browser_session_operations"):
                    connection.execute(
                        f"DELETE FROM browser_session_operations WHERE session_id IN ({placeholders})",
                        tuple(browser_ids),
                    )
                if self._table_exists(connection, "browser_session_versions"):
                    connection.execute(
                        f"DELETE FROM browser_session_versions WHERE session_id IN ({placeholders})",
                        tuple(browser_ids),
                    )
                connection.execute(
                    f"DELETE FROM browser_sessions WHERE id IN ({placeholders})", tuple(browser_ids)
                )
        if self._table_exists(connection, "intelligence_turns"):
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                connection.execute(
                    f"DELETE FROM intelligence_turns WHERE owner_user_id = ? OR session_id IN ({placeholders})",
                    (user_id, *session_ids),
                )
            else:
                connection.execute("DELETE FROM intelligence_turns WHERE owner_user_id = ?", (user_id,))

    def _unlink_account_file(self, relative_path: str) -> bool:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        if self._workspace_backup_protects_file(relative.as_posix()):
            return False
        candidate = self.store.root.joinpath(*relative.parts)
        try:
            if candidate.is_symlink():
                candidate.unlink()
                return True
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.store.root):
                return False
            if resolved.is_file():
                resolved.unlink()
            elif resolved.exists():
                return False
            return True
        except OSError:
            return False

    def _drain_account_deletion_files(self, user_id: str, now: datetime) -> int:
        now_text = self._stamp(now)
        pending = self.store.rows(
            "SELECT relative_path FROM account_deletion_file_cleanup WHERE deletion_user_id = ? AND status <> 'removed' ORDER BY relative_path",
            (user_id,),
        )
        for row in pending:
            removed = self._unlink_account_file(row["relative_path"])
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_deletion_file_cleanup SET status = ?, attempts = attempts + 1, error_code = ?, updated_at = ? WHERE deletion_user_id = ? AND relative_path = ? AND status <> 'removed'",
                    (
                        "removed" if removed else "blocked",
                        None if removed else "FILE_REMOVAL_FAILED",
                        now_text,
                        user_id,
                        row["relative_path"],
                    ),
                )
        remaining = self.store.row(
            "SELECT COUNT(*) AS count FROM account_deletion_file_cleanup WHERE deletion_user_id = ? AND status <> 'removed'",
            (user_id,),
        )["count"]
        if remaining == 0:
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE account_deletions SET status = 'completed', completed_at = ?,
                           completion_code = 'ACCOUNT_DATA_PURGED', export_reference = NULL
                       WHERE user_id = ? AND status = 'scheduled'
                         AND completion_code = 'ACCOUNT_DATA_PURGED_FILES_PENDING'""",
                    (now_text, user_id),
                )
        return int(remaining)

    def request_account_deletion(self, identity: dict) -> dict:
        active = self._active_identity(identity, recent=True)
        self._require_mailer()
        email_status = self.store.row(
            "SELECT email_normalized, email_display, verified_at FROM account_emails WHERE user_id = ?",
            (active["user_id"],),
        )
        if email_status is None or email_status["verified_at"] is None:
            raise AccountError("VERIFIED_EMAIL_REQUIRED", "Verify an email address before requesting account deletion", 409)
        with self.store.transaction(immediate=False) as connection:
            blockers = self._deletion_preflight(connection, active["user_id"])
        if blockers:
            raise AccountError(
                "ACCOUNT_DELETION_BLOCKED",
                "Resolve the listed workspace ownership and active-session items before scheduling account deletion",
                409,
                {"blockers": blockers},
            )
        existing_request = self.store.row(
            "SELECT status, export_reference, scheduled_at FROM account_deletions WHERE user_id = ?",
            (active["user_id"],),
        )
        if existing_request is not None and existing_request["status"] == "scheduled":
            return {
                "state": "deletion_scheduled",
                "scheduled_at": existing_request["scheduled_at"],
                "export_reference": existing_request["export_reference"],
            }
        if existing_request is not None and existing_request["status"] == "awaiting_confirmation":
            export_reference_id = existing_request["export_reference"]
            export_row = None if export_reference_id is None else self.store.row(
                "SELECT expires_at, sha256, size_bytes FROM account_exports WHERE id = ? AND user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (export_reference_id, active["user_id"], self._stamp()),
            )
            if self.account_exporter is None and export_row is not None:
                export_info = {
                    "state": "export_ready",
                    "export_id": export_reference_id,
                    "download_url": f"/api/account/exports/{export_reference_id}/download",
                    "expires_at": export_row["expires_at"],
                    "sha256": export_row["sha256"],
                    "size_bytes": export_row["size_bytes"],
                }
            elif self.account_exporter is None:
                export_info = self._write_account_export(active["user_id"])
                export_reference_id = export_info["export_id"]
            else:
                export_info = None
        else:
            exported = (
                self._write_account_export(active["user_id"])
                if self.account_exporter is None
                else self.account_exporter(active["user_id"])
            )
            if (
                not isinstance(exported, dict)
                or exported.get("state") not in {"completed", "export_ready"}
                or (type(exported.get("artifact_id")) is not str and type(exported.get("export_id")) is not str)
                or not (exported.get("artifact_id") or exported.get("export_id"))
            ):
                raise AccountError("ACCOUNT_EXPORT_UNAVAILABLE", "The account-data export could not be prepared", 503)
            export_reference_id = exported.get("artifact_id") or exported.get("export_id")
            export_info = exported
        if self.user_data_purger is None:
            raise AccountError("ACCOUNT_DELETION_UNAVAILABLE", "Account deletion is unavailable until the shared-data deletion and ownership-transfer handler is connected", 503)
        now = self._now()
        with self.store.transaction() as connection:
            email = connection.execute(
                "SELECT email_normalized, email_display, verified_at FROM account_emails WHERE user_id = ?",
                (active["user_id"],),
            ).fetchone()
            if email is None or email["verified_at"] is None:
                raise AccountError("VERIFIED_EMAIL_REQUIRED", "Verify an email address before requesting account deletion", 409)
            deletion = connection.execute(
                "SELECT status, scheduled_at FROM account_deletions WHERE user_id = ?",
                (active["user_id"],),
            ).fetchone()
            if deletion is not None and deletion["status"] == "scheduled":
                return {
                    "state": "deletion_scheduled",
                    "scheduled_at": deletion["scheduled_at"],
                    "export_reference": export_reference_id,
                }
            connection.execute(
                """INSERT INTO account_deletions(user_id, status, confirmation_requested_at, export_reference)
                   VALUES(?, 'awaiting_confirmation', ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET status = 'awaiting_confirmation',
                       confirmation_requested_at = excluded.confirmation_requested_at,
                       export_reference = excluded.export_reference,
                       requested_at = NULL, scheduled_at = NULL, cancelled_at = NULL, completed_at = NULL""",
                (active["user_id"], self._stamp(now), export_reference_id),
            )
            challenge = self._issue_code_tx(
                connection,
                purpose="delete_confirm",
                email_normalized=email["email_normalized"],
                user_id=active["user_id"],
                now=now,
            )
        try:
            self._deliver_code(challenge[0], email["email_display"], "delete_confirm", challenge[1], challenge[2])
        except AccountError:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE account_deletions SET status = 'cancelled', cancelled_at = ? WHERE user_id = ? AND status = 'awaiting_confirmation'",
                    (self._stamp(), active["user_id"]),
                )
            raise
        return {
            "state": "deletion_confirmation_required",
            "reference": challenge[0],
            "display_email": _email_hint(email["email_normalized"]),
            "expires_at": challenge[2],
            "resend_at": challenge[3],
            "export_reference": export_reference_id,
            "export_download_url": None if export_info is None else export_info.get("download_url"),
            "export_expires_at": None if export_info is None else export_info.get("expires_at"),
        }

    def close_scheduled_account_browsers(self) -> dict[str, int]:
        if self.store.row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'browser_sessions'") is None:
            return {"closed": 0, "pending": 0}
        rows = self.store.rows(
            """SELECT b.id, b.engine, b.owner_user_id FROM browser_sessions b
               JOIN account_deletions d ON d.user_id = b.owner_user_id
               WHERE d.status = 'scheduled' AND b.last_error_code = 'ACCOUNT_DELETION_CLOSE_PENDING'
               ORDER BY b.updated_at, b.id"""
        )
        closed, pending = 0, 0
        now = self._stamp()
        for row in rows:
            try:
                if self.browser_session_closer is None:
                    raise RuntimeError("browser session closer unavailable")
                self.browser_session_closer(row["engine"], row["id"])
            except Exception:
                pending += 1
                continue
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE browser_sessions SET last_error_code = 'ACCOUNT_DELETION_CLOSED', updated_at = ? WHERE id = ? AND owner_user_id = ? AND last_error_code = 'ACCOUNT_DELETION_CLOSE_PENDING'",
                    (now, row["id"], row["owner_user_id"]),
                )
            closed += 1
        return {"closed": closed, "pending": pending}

    def close_workspace_member_browsers(
        self,
        workspace_id: str,
        user_id: str,
        reason: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, int]:
        if self.store.row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'browser_sessions'") is None:
            return {"closed": 0, "pending": 0}
        now = self._stamp()
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT id, engine, version FROM browser_sessions
                   WHERE workspace_id = ? AND owner_user_id = ?
                     AND (status <> 'closed' OR state_ciphertext IS NOT NULL)
                   ORDER BY id""",
                (workspace_id, user_id),
            ).fetchall()
            for row in rows:
                next_version = row["version"] + 1
                if self._table_exists(connection, "browser_session_operations"):
                    connection.execute(
                        """UPDATE browser_session_operations SET status = 'unknown', error_code = ?, updated_at = ?
                           WHERE session_id = ? AND workspace_id = ? AND status = 'pending'""",
                        (reason, now, row["id"], workspace_id),
                    )
                connection.execute(
                    """UPDATE browser_sessions SET status = 'closed', state_ciphertext = NULL,
                           state_protection = NULL, operation_id = NULL, operation_expires_at = NULL,
                           closed_at = COALESCE(closed_at, ?), updated_at = ?, version = ?,
                           last_error_code = 'MEMBERSHIP_CLOSE_PENDING'
                       WHERE id = ? AND workspace_id = ? AND owner_user_id = ?""",
                    (now, now, next_version, row["id"], workspace_id, user_id),
                )
                if self._table_exists(connection, "browser_session_versions"):
                    metadata = json.dumps({"reason": reason, "platform_state": "removed"}, ensure_ascii=False, sort_keys=True)
                    connection.execute(
                        """INSERT INTO browser_session_versions(
                               session_id, workspace_id, version, operation, metadata_json, created_at, created_by
                           ) VALUES(?, ?, ?, 'workspace_membership_revoked', ?, ?, ?)""",
                        (row["id"], workspace_id, next_version, metadata, now, actor_user_id or user_id),
                    )
        closed, pending = 0, 0
        for row in rows:
            try:
                if self.browser_session_closer is None:
                    raise RuntimeError("browser session closer unavailable")
                self.browser_session_closer(row["engine"], row["id"])
            except Exception:
                pending += 1
                continue
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE browser_sessions SET last_error_code = 'MEMBERSHIP_SESSION_CLOSED', updated_at = ? WHERE id = ? AND workspace_id = ? AND owner_user_id = ? AND last_error_code = 'MEMBERSHIP_CLOSE_PENDING'",
                    (self._stamp(), row["id"], workspace_id, user_id),
                )
            closed += 1
        return {"closed": closed, "pending": pending}

    def close_pending_member_browsers(self) -> dict[str, int]:
        if self.store.row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'browser_sessions'") is None:
            return {"closed": 0, "pending": 0}
        rows = self.store.rows(
            """SELECT b.id, b.workspace_id, b.owner_user_id, b.engine FROM browser_sessions b
               JOIN memberships m ON m.workspace_id = b.workspace_id AND m.user_id = b.owner_user_id
               WHERE b.last_error_code = 'MEMBERSHIP_CLOSE_PENDING'
                 AND (m.active = 0 OR m.role = 'viewer')
               ORDER BY b.updated_at, b.id"""
        )
        closed, pending = 0, 0
        for row in rows:
            try:
                if self.browser_session_closer is None:
                    raise RuntimeError("browser session closer unavailable")
                self.browser_session_closer(row["engine"], row["id"])
            except Exception:
                pending += 1
                continue
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE browser_sessions SET last_error_code = 'MEMBERSHIP_SESSION_CLOSED', updated_at = ? WHERE id = ? AND last_error_code = 'MEMBERSHIP_CLOSE_PENDING'",
                    (self._stamp(), row["id"]),
                )
            closed += 1
        return {"closed": closed, "pending": pending}

    def confirm_account_deletion(self, reference: Any, code: Any) -> dict:
        now = self._now()
        error: AccountError | None = None
        result: dict | None = None
        revoked_sessions = 0
        cancelled_jobs = 0
        paused_monitors = 0
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection, reference=reference, purpose="delete_confirm", code=code, now=now
            )
            if error is None and challenge is not None and challenge["user_id"]:
                user_id = challenge["user_id"]
                deletion = connection.execute(
                    "SELECT status FROM account_deletions WHERE user_id = ?", (user_id,)
                ).fetchone()
                if deletion is None or deletion["status"] != "awaiting_confirmation":
                    error = AccountError("DELETION_CONFIRMATION_INVALID", "The deletion confirmation is invalid or expired", 400)
                else:
                    blockers = self._deletion_preflight(connection, user_id)
                    if blockers:
                        raise AccountError(
                            "ACCOUNT_DELETION_BLOCKED",
                            "Resolve the listed workspace ownership and active-session items before confirming deletion",
                            409,
                            {"blockers": blockers},
                        )
                    scheduled_at = self._stamp(now + self.policy.deletion_grace)
                    now_text = self._stamp(now)
                    connection.execute(
                        "UPDATE account_deletions SET status = 'scheduled', requested_at = ?, scheduled_at = ? WHERE user_id = ? AND status = 'awaiting_confirmation'",
                        (now_text, scheduled_at, user_id),
                    )
                    revoked_sessions = connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                        (now_text, user_id),
                    ).rowcount
                    if self._table_exists(connection, "browser_sessions"):
                        browser_rows = connection.execute(
                            "SELECT id, workspace_id, version FROM browser_sessions WHERE owner_user_id = ? ORDER BY id",
                            (user_id,),
                        ).fetchall()
                        for browser in browser_rows:
                            next_version = browser["version"] + 1
                            connection.execute(
                                """UPDATE browser_sessions SET status = 'closed', state_ciphertext = NULL,
                                       state_protection = NULL, operation_id = NULL, operation_expires_at = NULL,
                                       closed_at = COALESCE(closed_at, ?), updated_at = ?, version = ?,
                                       last_error_code = 'ACCOUNT_DELETION_CLOSE_PENDING'
                                   WHERE id = ? AND workspace_id = ? AND owner_user_id = ?""",
                                (now_text, now_text, next_version, browser["id"], browser["workspace_id"], user_id),
                            )
                            if self._table_exists(connection, "browser_session_versions"):
                                connection.execute(
                                    "INSERT INTO browser_session_versions(session_id, workspace_id, version, operation, metadata_json, created_at, created_by) VALUES(?, ?, ?, 'account_deletion_close', '{\"platform_state\":\"removed\"}', ?, ?)",
                                    (browser["id"], browser["workspace_id"], next_version, now_text, user_id),
                                )
                    if self._table_exists(connection, "jobs"):
                        if self._table_exists(connection, "media_renders"):
                            connection.execute(
                                """UPDATE media_renders SET status = 'cancelled', error_code = 'ACCOUNT_DELETION',
                                       error_message = 'Account deletion was scheduled.', updated_at = ?
                                   WHERE status IN ('queued', 'running', 'writing')
                                     AND job_id IN (SELECT id FROM jobs WHERE user_id = ? AND status IN ('queued', 'running', 'cancel_requested'))""",
                                (now_text, user_id),
                            )
                        cancelled_jobs = connection.execute(
                            """UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                                   lease_generation = lease_generation + 1,
                                   error_code = 'ACCOUNT_DELETION_SCHEDULED',
                                   error_message = 'Processing stopped while account deletion is pending.',
                                   updated_at = ?
                               WHERE user_id = ? AND status IN ('queued', 'running', 'cancel_requested')""",
                            (now_text, user_id),
                        ).rowcount
                        if self._table_exists(connection, "delivery_batches"):
                            connection.execute(
                                "UPDATE delivery_batches SET status='partial',updated_at=? WHERE status='preparing' "
                                "AND job_id IN (SELECT id FROM jobs WHERE user_id=? AND status='cancelled' "
                                "AND error_code='ACCOUNT_DELETION_SCHEDULED')",
                                (now_text, user_id),
                            )
                        if self._table_exists(connection, "media_interchanges"):
                            connection.execute(
                                "UPDATE media_interchanges SET status='cancelled',staging_path=NULL,"
                                "error_code='ACCOUNT_DELETION_SCHEDULED',error_message='Account deletion was scheduled.',updated_at=? "
                                "WHERE status IN ('queued','running','writing') AND job_id IN ("
                                "SELECT id FROM jobs WHERE user_id=? AND status='cancelled' AND error_code='ACCOUNT_DELETION_SCHEDULED')",
                                (now_text, user_id),
                            )
                    if self._table_exists(connection, "monitor_rules"):
                        from .monitoring import _write_version
                        monitor_rows = connection.execute(
                            "SELECT * FROM monitor_rules WHERE owner_user_id = ? AND status = 'enabled' ORDER BY id",
                            (user_id,),
                        ).fetchall()
                        for monitor in monitor_rows:
                            next_version = monitor["version"] + 1
                            connection.execute(
                                "UPDATE monitor_rules SET status = 'paused', next_run_at = NULL, version = ?, updated_at = ? WHERE id = ? AND version = ?",
                                (next_version, now_text, monitor["id"], monitor["version"]),
                            )
                            updated_monitor = connection.execute(
                                "SELECT * FROM monitor_rules WHERE id = ?", (monitor["id"],)
                            ).fetchone()
                            _write_version(
                                connection, updated_monitor, next_version,
                                "account_deletion_scheduled", user_id, now_text,
                            )
                            if self._table_exists(connection, "monitor_runs"):
                                connection.execute(
                                    "UPDATE monitor_runs SET status = 'cancelled', completed_at = ? WHERE monitor_id = ? AND status IN ('queued', 'running')",
                                    (now_text, monitor["id"]),
                                )
                            if self._table_exists(connection, "monitor_run_sources"):
                                connection.execute(
                                    "UPDATE monitor_run_sources SET lifecycle_status = 'cancelled', completed_at = ? WHERE monitor_id = ? AND lifecycle_status IN ('queued', 'running')",
                                    (now_text, monitor["id"]),
                                )
                            paused_monitors += 1
                    if self._table_exists(connection, "workspace_data_operations"):
                        backup_workspaces = connection.execute(
                            "SELECT DISTINCT source_workspace_id FROM workspace_data_operations WHERE owner_user_id=? "
                            "AND kind='backup' AND protection_state IN ('pending','active')",
                            (user_id,),
                        ).fetchall()
                        for backup_workspace in backup_workspaces:
                            self._invalidate_workspace_backup_protections(
                                connection, backup_workspace["source_workspace_id"], user_id, now_text,
                            )
                    self._event(connection, user_id, "account_deletion_scheduled", now_text)
                    result = {
                        "state": "deletion_scheduled",
                        "scheduled_at": scheduled_at,
                        "revoked_sessions": revoked_sessions,
                        "cancelled_jobs": cancelled_jobs,
                        "paused_monitors": paused_monitors,
                        "platform_sessions_closed": 0,
                        "platform_session_close_pending": 0,
                        "export_reference": connection.execute(
                            "SELECT export_reference FROM account_deletions WHERE user_id = ?", (user_id,)
                        ).fetchone()["export_reference"],
                    }
            elif error is None:
                error = AccountError("DELETION_CONFIRMATION_INVALID", "The deletion confirmation is invalid or expired", 400)
        if error is not None:
            raise error
        browser_cleanup = self.close_scheduled_account_browsers()
        if result is not None:
            result["platform_sessions_closed"] = browser_cleanup["closed"]
            result["platform_session_close_pending"] = browser_cleanup["pending"]
        return result or {"state": "deletion_scheduled", **browser_cleanup}

    def request_deletion_cancellation(self, email: Any) -> dict:
        self._require_mailer()
        normalized, display = _email(email)
        now = self._now()
        with self.store.transaction() as connection:
            account = connection.execute(
                "SELECT user_id, verified_at FROM account_emails WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
            if account is None or account["verified_at"] is None:
                reference, expires_at, resend_at = self._record_no_delivery_tx(
                    connection, purpose="delete_cancel", email_normalized=normalized, user_id=None, now=now
                )
                challenge = None
            else:
                deletion = connection.execute(
                    "SELECT scheduled_at FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
                    (account["user_id"],),
                ).fetchone()
                if deletion is None or deletion["scheduled_at"] <= self._stamp(now):
                    reference, expires_at, resend_at = self._record_no_delivery_tx(
                        connection, purpose="delete_cancel", email_normalized=normalized, user_id=account["user_id"], now=now
                    )
                    challenge = None
                else:
                    challenge = self._issue_code_tx(
                        connection,
                        purpose="delete_cancel",
                        email_normalized=normalized,
                        user_id=account["user_id"],
                        now=now,
                    )
                    reference, expires_at, resend_at = challenge[0], challenge[2], challenge[3]
        if challenge is not None:
            self._deliver_code(challenge[0], display, "delete_cancel", challenge[1], challenge[2])
        return {
            "state": "accepted_if_eligible",
            "reference": reference,
            "display_email": _email_hint(normalized),
            "expires_at": expires_at,
            "resend_at": resend_at,
        }

    def cancel_account_deletion(self, reference: Any, code: Any) -> dict:
        now = self._now()
        error: AccountError | None = None
        result: dict | None = None
        with self.store.transaction() as connection:
            challenge, error = self._verify_code_tx(
                connection, reference=reference, purpose="delete_cancel", code=code, now=now
            )
            if error is None and challenge is not None and challenge["user_id"]:
                user_id = challenge["user_id"]
                deletion = connection.execute(
                    "SELECT scheduled_at FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
                    (user_id,),
                ).fetchone()
                if deletion is None or deletion["scheduled_at"] <= self._stamp(now):
                    error = AccountError("DELETION_CANCELLATION_EXPIRED", "The account deletion can no longer be cancelled", 410)
                else:
                    connection.execute(
                        "UPDATE account_deletions SET status = 'cancelled', cancelled_at = ? WHERE user_id = ? AND status = 'scheduled'",
                        (self._stamp(now), user_id),
                    )
                    self._event(connection, user_id, "account_deletion_cancelled", self._stamp(now))
                    result = {"state": "deletion_cancelled"}
            elif error is None:
                error = AccountError("DELETION_CANCELLATION_EXPIRED", "The account deletion can no longer be cancelled", 410)
        if error is not None:
            raise error
        return result or {"state": "deletion_cancelled"}

    def account_deletion_status(self, identity: dict) -> dict:
        active = self._active_identity(identity)
        row = self.store.row(
            "SELECT status, requested_at, scheduled_at, cancelled_at, completed_at, export_reference FROM account_deletions WHERE user_id = ?",
            (active["user_id"],),
        )
        return {"state": "none"} if row is None else dict(row)

    def purge_due_deletions(self, *, now: datetime | None = None) -> dict:
        """Purge due accounts through the configured/current domain owner in one transaction."""
        current = now or self._now()
        now_text = self._stamp(current)
        due = self.store.rows(
            "SELECT user_id FROM account_deletions WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at, user_id",
            (now_text,),
        )
        if not due:
            return {"state": "no_due_accounts", "completed": 0, "blocked": 0}
        completed = 0
        blocked = 0
        blocked_reasons: dict[str, int] = {}
        for item in due:
            user_id = item["user_id"]
            did_complete = False
            existing_cleanup = self.store.row(
                "SELECT completion_code FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
                (user_id,),
            )
            if existing_cleanup is not None and existing_cleanup["completion_code"] == "ACCOUNT_DATA_PURGED_FILES_PENDING":
                remaining = self._drain_account_deletion_files(user_id, current)
                if remaining:
                    blocked += 1
                    blocked_reasons["account_files_pending_removal"] = blocked_reasons.get("account_files_pending_removal", 0) + remaining
                else:
                    completed += 1
                continue
            try:
                with self.store.transaction() as connection:
                    deletion = connection.execute(
                        "SELECT scheduled_at, completion_code FROM account_deletions WHERE user_id = ? AND status = 'scheduled'",
                        (user_id,),
                    ).fetchone()
                    if deletion is None or deletion["scheduled_at"] > now_text:
                        continue
                    if deletion["completion_code"] == "ACCOUNT_DATA_PURGED_FILES_PENDING":
                        raise _DeletionBlocked("account_files_pending_removal")
                    blockers = self._deletion_preflight(connection, user_id)
                    if blockers:
                        raise _DeletionBlocked("account_handoff_or_active_write_required")

                    # The account owner already confirmed deletion. Stop writes
                    # and remove device/platform state before dropping sessions.
                    self._remove_user_session_dependents(connection, user_id)
                    if self._table_exists(connection, "jobs"):
                        if self._table_exists(connection, "media_renders"):
                            connection.execute(
                                """UPDATE media_renders SET status = 'cancelled', error_code = 'ACCOUNT_DELETION',
                                       error_message = 'Account data was deleted.', updated_at = ?
                                   WHERE status IN ('queued', 'running', 'writing')
                                     AND job_id IN (SELECT id FROM jobs WHERE user_id = ? AND status IN ('queued', 'running', 'cancel_requested'))""",
                                (now_text, user_id),
                            )
                        connection.execute(
                            """UPDATE jobs SET status = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN 'cancelled' ELSE status END,
                                   cancel_requested = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN 1 ELSE cancel_requested END,
                                   lease_generation = CASE WHEN status IN ('queued', 'running', 'cancel_requested') THEN lease_generation + 1 ELSE lease_generation END,
                                   payload_json = '{}', error_message = NULL,
                                   progress_label = 'Processing details removed with account deletion', updated_at = ?
                               WHERE user_id = ?""",
                            (now_text, user_id),
                        )
                    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                    connection.execute("DELETE FROM local_identity WHERE user_id = ?", (user_id,))
                    purge_result = self.user_data_purger(connection, user_id)
                    if purge_result is True:
                        domain_files: list[str] = []
                    elif (
                        isinstance(purge_result, dict)
                        and purge_result.get("confirmed") is True
                        and isinstance(purge_result.get("files_to_delete", []), list)
                        and all(type(path) is str for path in purge_result.get("files_to_delete", []))
                    ):
                        domain_files = purge_result.get("files_to_delete", [])
                    else:
                        raise _DeletionBlocked("data_purge_not_confirmed")
                    connection.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
                    remaining_membership = connection.execute(
                        "SELECT 1 FROM memberships WHERE user_id = ? LIMIT 1", (user_id,)
                    ).fetchone()
                    if remaining_membership is not None:
                        # The caller must transfer shared workspace ownership or
                        # remove this account's memberships before account purge.
                        raise _DeletionBlocked("workspace_membership_not_resolved")
                    if connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                        raise _DeletionBlocked("user_record_missing_before_purge")
                    email = connection.execute(
                        "SELECT email_normalized FROM account_emails WHERE user_id = ?", (user_id,)
                    ).fetchone()
                    export_paths = [row["relative_path"] for row in connection.execute(
                        "SELECT relative_path FROM account_exports WHERE user_id = ?", (user_id,)
                    ).fetchall()]
                    file_paths = sorted(set([*domain_files, *export_paths]))
                    for relative_path in file_paths:
                        connection.execute(
                            """INSERT OR IGNORE INTO account_deletion_file_cleanup(
                                   deletion_user_id, relative_path, status, created_at, updated_at
                               ) VALUES(?, ?, 'pending', ?, ?)""",
                            (user_id, relative_path, now_text, now_text),
                        )
                    connection.execute("DELETE FROM account_exports WHERE user_id = ?", (user_id,))
                    connection.execute("DELETE FROM account_preferences WHERE user_id = ?", (user_id,))
                    connection.execute(
                        "DELETE FROM account_local_merge_choices WHERE account_user_id = ? OR local_user_id = ?",
                        (user_id, user_id),
                    )
                    connection.execute("DELETE FROM account_challenges WHERE user_id = ?", (user_id,))
                    connection.execute(
                        "DELETE FROM account_login_challenges WHERE user_id = ? OR local_user_id = ?",
                        (user_id, user_id),
                    )
                    connection.execute(
                        "DELETE FROM account_oauth_transactions WHERE user_id = ? OR local_user_id = ?",
                        (user_id, user_id),
                    )
                    connection.execute("DELETE FROM account_password_credentials WHERE user_id = ?", (user_id,))
                    connection.execute("DELETE FROM account_external_identities WHERE user_id = ?", (user_id,))
                    connection.execute("DELETE FROM account_emails WHERE user_id = ?", (user_id,))
                    connection.execute("DELETE FROM account_login_failures WHERE subject_hash = ?", (_subject_hash(email["email_normalized"]),) if email else ("",))
                    connection.execute("UPDATE account_events SET user_id = NULL WHERE user_id = ?", (user_id,))
                    # Keep this opaque UUID only as a non-login authorship
                    # tombstone. Credentials, email, sessions and memberships
                    # are gone; other authors remain distinct.
                    connection.execute("UPDATE users SET name = '已删除用户' WHERE id = ?", (user_id,))
                    if file_paths:
                        connection.execute(
                            "UPDATE account_deletions SET completion_code = 'ACCOUNT_DATA_PURGED_FILES_PENDING', export_reference = NULL WHERE user_id = ? AND status = 'scheduled'",
                            (user_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE account_deletions SET status = 'completed', completed_at = ?, completion_code = 'ACCOUNT_DATA_PURGED', export_reference = NULL WHERE user_id = ? AND status = 'scheduled'",
                            (now_text, user_id),
                        )
                        did_complete = True
            except _DeletionBlocked as exc:
                blocked += 1
                blocked_reasons[exc.reason] = blocked_reasons.get(exc.reason, 0) + 1
            except sqlite3.IntegrityError:
                blocked += 1
                blocked_reasons["foreign_key_reference_remains"] = blocked_reasons.get("foreign_key_reference_remains", 0) + 1
            else:
                if not did_complete:
                    remaining = self._drain_account_deletion_files(user_id, current)
                    if remaining:
                        blocked += 1
                        blocked_reasons["account_files_pending_removal"] = blocked_reasons.get("account_files_pending_removal", 0) + remaining
                    else:
                        completed += 1
                else:
                    completed += 1
        if blocked and completed == 0:
            state = "blocked"
        elif blocked:
            state = "partial"
        else:
            state = "completed"
        return {"state": state, "completed": completed, "blocked": blocked, "blockers": blocked_reasons}
