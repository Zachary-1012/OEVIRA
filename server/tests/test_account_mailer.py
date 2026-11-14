from __future__ import annotations

from email import message_from_bytes
from email.policy import default
import re
import socketserver
import threading
import unittest

from server.oevira.account_mailer import SMTPEmailDelivery


class _ControlledSMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 controlled.test ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("ascii", errors="replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in {"EHLO", "HELO", "MAIL", "RCPT", "RSET", "NOOP"}:
                self.wfile.write(b"250 controlled.test accepted\r\n")
            elif verb == "DATA":
                self.wfile.write(b"354 end with dot\r\n")
                message = bytearray()
                while True:
                    data_line = self.rfile.readline()
                    if data_line in {b".\r\n", b".\n"}:
                        break
                    if data_line.startswith(b".."):
                        data_line = data_line[1:]
                    message.extend(data_line)
                self.server.messages.append(bytes(message))
                self.wfile.write(b"250 queued by controlled test server\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return
            else:
                self.wfile.write(b"500 command not supported\r\n")


class _ControlledSMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _ControlledSMTPHandler)
        self.messages: list[bytes] = []


class AccountMailerTests(unittest.TestCase):
    def setUp(self):
        self.server = _ControlledSMTPServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_smtp_transport_submits_code_and_clickable_invite_without_claiming_delivery(self):
        mailer = SMTPEmailDelivery(
            host="127.0.0.1",
            port=self.server.server_address[1],
            sender="account@example.test",
            security="none",
            public_base_url="http://127.0.0.1:8317",
        )
        self.assertTrue(mailer.configured)
        self.assertTrue(mailer.invitation_link_configured)
        self.assertTrue(mailer.send_code("person@example.test", "register", "123456", "2026-11-03T13:00:00Z"))
        token = "opaque-invitation-token-that-is-not-an-account-session"
        self.assertTrue(
            mailer.send_invitation(
                "invitee@example.test", token, "2026-11-10T12:00:00Z", "Client Workspace"
            )
        )
        self.assertEqual(len(self.server.messages), 2)
        code_message = message_from_bytes(self.server.messages[0], policy=default)
        invitation_message = message_from_bytes(self.server.messages[1], policy=default)
        self.assertEqual(code_message["To"], "person@example.test")
        self.assertIn("123456", "\n".join(part.get_content() for part in code_message.walk() if part.get_content_type() == "text/plain"))
        self.assertEqual(
            code_message["X-OEVIRA-Delivery-Semantics"],
            "smtp-accepted-is-not-proof-of-inbox-delivery",
        )
        self.assertEqual(invitation_message["To"], "invitee@example.test")
        plain = "\n".join(
            part.get_content()
            for part in invitation_message.walk()
            if part.get_content_type() == "text/plain"
        )
        invite_url = mailer.invitation_url(token)
        self.assertIn("Client Workspace", plain)
        self.assertIn(invite_url, plain)
        self.assertRegex(plain, re.escape(invite_url))
        self.assertIn(f"#invite={token}", invite_url)
        self.assertNotIn("/api/account/invitations/accept?", invite_url)

    def test_configuration_fails_closed_for_partial_or_insecure_remote_smtp(self):
        with self.assertRaises(ValueError):
            SMTPEmailDelivery.from_environment(
                {"OEVIRA_SMTP_HOST": "smtp.example.test", "OEVIRA_SMTP_FROM": "account@example.test"}
            )
        with self.assertRaises(ValueError):
            SMTPEmailDelivery(
                host="smtp.example.test",
                port=25,
                sender="account@example.test",
                security="none",
            )
        with self.assertRaises(ValueError):
            SMTPEmailDelivery(
                host="127.0.0.1",
                port=25,
                sender="account@example.test",
                username="smtp-user",
            )
        self.assertIsNone(SMTPEmailDelivery.from_environment({"OEVIRA_PUBLIC_BASE_URL": "https://app.example.test"}))

    def test_invitation_links_require_an_explicit_valid_product_url(self):
        mailer = SMTPEmailDelivery(
            host="127.0.0.1",
            port=self.server.server_address[1],
            sender="account@example.test",
            security="none",
        )
        self.assertFalse(mailer.invitation_link_configured)
        with self.assertRaises(RuntimeError):
            mailer.invitation_url("a" * 40)
        with self.assertRaises(ValueError):
            SMTPEmailDelivery(
                host="127.0.0.1",
                port=25,
                sender="account@example.test",
                security="none",
                public_base_url="http://public.example.test",
            )


if __name__ == "__main__":
    unittest.main()
