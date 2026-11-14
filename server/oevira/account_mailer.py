"""Standard-library SMTP delivery for account verification and invitations."""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
import html
import ipaddress
import math
import os
import smtplib
import ssl
from urllib.parse import quote, urlsplit


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_public_base_url(value: str | None) -> str | None:
    if type(value) is not str or not value.strip():
        return None
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or any(character in value for character in "\r\n")
    ):
        return None
    if parsed.scheme != "https" and not _loopback_host(parsed.hostname):
        return None
    return value


@dataclass(frozen=True)
class SMTPEmailDelivery:
    """SMTP delivery that treats server acceptance as queued, never as inbox delivery."""

    host: str
    port: int
    sender: str
    sender_name: str = "OEVIRA"
    username: str | None = None
    password: str | None = None
    security: str = "starttls"
    public_base_url: str | None = None
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if type(self.host) is not str or not self.host.strip() or any(c in self.host for c in "\r\n \t"):
            raise ValueError("SMTP host is invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SMTP port is invalid")
        if type(self.security) is not str or self.security not in {"starttls", "tls", "none"}:
            raise ValueError("SMTP security must be starttls, tls or none")
        if self.security == "none" and not _loopback_host(self.host):
            raise ValueError("SMTP without TLS is allowed only for an explicit loopback server")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP username and password must be configured together")
        if self.username is not None and (not self.username or any(c in self.username for c in "\r\n")):
            raise ValueError("SMTP username is invalid")
        if self.password is not None and any(c in self.password for c in "\r\n"):
            raise ValueError("SMTP password is invalid")
        if type(self.sender) is not str:
            raise ValueError("SMTP sender address is invalid")
        parsed = parseaddr(self.sender)
        if not parsed[1] or parsed[1] != self.sender.strip() or any(c in self.sender for c in "\r\n"):
            raise ValueError("SMTP sender address is invalid")
        if not self.sender_name.strip() or any(c in self.sender_name for c in "\r\n"):
            raise ValueError("SMTP sender name is invalid")
        if type(self.timeout) not in {int, float} or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("SMTP timeout must be positive")
        normalized_base = _valid_public_base_url(self.public_base_url)
        if self.public_base_url and normalized_base is None:
            raise ValueError("OEVIRA_PUBLIC_BASE_URL must be an HTTPS product URL or loopback HTTP URL without query or fragment")
        object.__setattr__(self, "public_base_url", normalized_base)

    @property
    def configured(self) -> bool:
        return True

    @property
    def invitation_link_configured(self) -> bool:
        return self.public_base_url is not None

    @classmethod
    def from_environment(cls, environ=None) -> SMTPEmailDelivery | None:
        source = os.environ if environ is None else environ
        smtp_names = (
            "OEVIRA_SMTP_HOST",
            "OEVIRA_SMTP_PORT",
            "OEVIRA_SMTP_FROM",
            "OEVIRA_SMTP_USERNAME",
            "OEVIRA_SMTP_PASSWORD",
            "OEVIRA_SMTP_SECURITY",
            "OEVIRA_SMTP_FROM_NAME",
        )
        values = {name: source.get(name, "").strip() for name in smtp_names}
        values["OEVIRA_PUBLIC_BASE_URL"] = source.get("OEVIRA_PUBLIC_BASE_URL", "").strip()
        if not values["OEVIRA_SMTP_HOST"]:
            if any(values[name] for name in smtp_names[1:]):
                raise ValueError("SMTP account delivery settings are incomplete")
            return None
        if not values["OEVIRA_SMTP_FROM"] or not values["OEVIRA_SMTP_PORT"]:
            raise ValueError("SMTP host, port and sender are all required")
        try:
            port = int(values["OEVIRA_SMTP_PORT"])
        except ValueError as exc:
            raise ValueError("SMTP port must be an integer") from exc
        return cls(
            host=values["OEVIRA_SMTP_HOST"],
            port=port,
            sender=values["OEVIRA_SMTP_FROM"],
            sender_name=values["OEVIRA_SMTP_FROM_NAME"] or "OEVIRA",
            username=values["OEVIRA_SMTP_USERNAME"] or None,
            password=values["OEVIRA_SMTP_PASSWORD"] or None,
            security=values["OEVIRA_SMTP_SECURITY"] or "starttls",
            public_base_url=values["OEVIRA_PUBLIC_BASE_URL"] or None,
        )

    def _send(self, recipient: str, subject: str, text_body: str, html_body: str) -> bool:
        message = EmailMessage()
        message["From"] = formataddr((self.sender_name, self.sender))
        message["To"] = recipient
        message["Subject"] = subject
        message["X-OEVIRA-Delivery-Semantics"] = "smtp-accepted-is-not-proof-of-inbox-delivery"
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        client = None
        try:
            if self.security == "tls":
                client = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=float(self.timeout), context=context
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=float(self.timeout))
            client.ehlo()
            if self.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self.username is not None:
                client.login(self.username, self.password or "")
            refused = client.send_message(message)
            return not refused
        finally:
            if client is not None:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()

    def send_code(self, email: str, purpose: str, code: str, expires_at: str) -> bool:
        labels = {
            "register": ("Verify your OEVIRA account", "Verify your email address"),
            "email_login": ("Sign in to OEVIRA", "Sign in with this email code"),
            "password_reset": ("Reset your OEVIRA password", "Reset your password"),
            "email_change": ("Confirm your OEVIRA email change", "Confirm your new email address"),
            "delete_confirm": ("Confirm OEVIRA account deletion", "Confirm account deletion"),
            "delete_cancel": ("Cancel OEVIRA account deletion", "Cancel scheduled account deletion"),
        }
        label, heading = labels.get(purpose, ("OEVIRA verification code", "Verify your request"))
        safe_code = html.escape(code)
        safe_expiry = html.escape(expires_at)
        text_body = f"{heading}\n\nYour OEVIRA verification code is: {code}\n\nIt expires at {expires_at}. If you did not request this, you can ignore this email."
        html_body = (
            f"<p>{html.escape(heading)}</p><p>Your OEVIRA verification code is "
            f"<strong>{safe_code}</strong>.</p><p>It expires at {safe_expiry}. "
            "If you did not request this, you can ignore this email.</p>"
        )
        return self._send(email, label, text_body, html_body)

    def invitation_url(self, token: str) -> str:
        if self.public_base_url is None:
            raise RuntimeError("Workspace invitation links require OEVIRA_PUBLIC_BASE_URL")
        return f"{self.public_base_url}/#invite={quote(token, safe='')}"

    def send_invitation(
        self,
        email: str,
        token: str,
        expires_at: str,
        workspace_name: str,
    ) -> bool:
        url = self.invitation_url(token)
        safe_name = html.escape(workspace_name)
        safe_url = html.escape(url, quote=True)
        safe_expiry = html.escape(expires_at)
        text_body = (
            f"You were invited to join {workspace_name} in OEVIRA.\n\n"
            f"Review and accept the invitation: {url}\n\n"
            f"The invitation expires at {expires_at}. You must sign in with the invited verified email and accept it in OEVIRA."
        )
        html_body = (
            f"<p>You were invited to join <strong>{safe_name}</strong> in OEVIRA.</p>"
            f'<p><a href="{safe_url}">Review workspace invitation</a></p>'
            f"<p>The invitation expires at {safe_expiry}. Sign in with the invited verified email and accept it in OEVIRA.</p>"
        )
        return self._send(email, "OEVIRA workspace invitation", text_body, html_body)
