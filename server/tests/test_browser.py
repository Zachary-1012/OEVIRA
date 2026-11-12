from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

from server.oevira import create_app
from server.oevira.acquisition import AcquisitionError, public_target
from server.oevira.api import _request_fingerprint
from server.oevira.auth import _digest, authenticate, issue_local_capability, utc_now
from server.oevira.browser import BrowserError, WindowsUserStateProtector, install_browser_schema
from server.oevira.browser_api import register_browser_routes
from server.oevira.browser_transport import PinnedBrowserProxy
from server.oevira.store import Store


BASE_URL = "http://127.0.0.1:8317"
COOKIE_SECRET = "browser-fixture-secret-never-real"
PASSWORD_FIXTURE = "not-a-real-password"
URL_TOKEN = "temporary-access-token-never-real"


class BrowserFixtureHandler(BaseHTTPRequestHandler):
    slow_started = threading.Event()
    slow_release = threading.Event()
    requested_paths = []

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.requested_paths.append(self.path)
        if path == "/slow-script":
            self.slow_started.set()
            self.slow_release.wait(10)
            body = b"document.title='Finished slow page';"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        if path == "/slow":
            body = b"<html><head><title>Loading fixture</title></head><body>Partial content<script src='/slow-script'></script></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket_echo()
            return
        if path == "/login":
            body = f"""<!doctype html><html><head><title>Local login fixture</title>
<style>body{{margin:0;font:20px Arial}} input,button{{display:block;position:absolute;left:20px;width:280px;height:32px}}
#user{{top:20px}} #password{{top:70px}} button{{top:120px;width:120px}} #status{{position:absolute;top:170px}}</style></head>
<body><form id="login"><input id="user" autocomplete="username"><input id="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button></form><p id="status">Please sign in</p>
<script>const status=document.getElementById('status');
if(document.cookie.includes('fixture_auth={COOKIE_SECRET}')){{status.textContent='Fixture session restored';}}
document.getElementById('login').addEventListener('submit',event=>{{event.preventDefault();document.cookie='fixture_auth={COOKIE_SECRET}; Path=/; SameSite=Lax';status.textContent='Fixture login complete';}});
</script><p id="excerpt" style="position:absolute;left:20px;top:240px">Capture only this excerpt.</p><p style="position:absolute;left:20px;top:280px">Sibling text stays outside the selected excerpt.</p>
</body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/set-cookie":
            body = b"<html><head><title>Fixture sign in</title></head><body>Fixture cookie set</body></html>"
            self.send_response(200)
            self.send_header("Set-Cookie", f"fixture_auth={COOKIE_SECRET}; Path=/; HttpOnly; SameSite=Lax")
        elif path == "/check-cookie":
            present = COOKIE_SECRET in self.headers.get("Cookie", "")
            text = "Fixture session restored" if present else "Fixture session absent"
            body = f"<html><head><title>Cookie check</title></head><body><p>{text}</p><p id='excerpt'>Capture only this excerpt.</p><p>Sibling text stays outside the selected excerpt.</p></body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/login":
            body = f"""<!doctype html><html><head><title>Local login fixture</title>
<style>body{{margin:0;font:20px Arial}} input,button{{display:block;position:absolute;left:20px;width:280px;height:32px}}
#user{{top:20px}} #password{{top:70px}} button{{top:120px;width:120px}} #status{{position:absolute;top:170px}}</style></head>
<body><form id="login"><input id="user" autocomplete="username"><input id="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button></form><p id="status">Please sign in</p>
<script>const status=document.getElementById('status');
if(document.cookie.includes('fixture_auth={COOKIE_SECRET}')){{status.textContent='Fixture session restored';}}
document.getElementById('login').addEventListener('submit',event=>{{event.preventDefault();document.cookie='fixture_auth={COOKIE_SECRET}; Path=/; SameSite=Lax';status.textContent='Fixture login complete';}});
</script></body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"<html><head><title>Browser fixture</title></head><body>fixture</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket_echo(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400)
            return
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.connection.settimeout(5)
        try:
            header = self.rfile.read(2)
            if len(header) != 2:
                return
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = int.from_bytes(self.rfile.read(2), "big")
            elif length == 127:
                length = int.from_bytes(self.rfile.read(8), "big")
            if not header[1] & 0x80 or length > 64 * 1024:
                return
            mask = self.rfile.read(4)
            payload = self.rfile.read(length)
            if len(mask) != 4 or len(payload) != length:
                return
            decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 1:
                frame = bytes((0x80 | opcode, len(decoded))) + decoded
                self.wfile.write(frame)
                self.wfile.flush()
        except OSError:
            return

    def log_message(self, _format: str, *_args) -> None:
        return


class BrowserApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserFixtureHandler)
        cls.web_thread = threading.Thread(target=cls.web_server.serve_forever, daemon=True)
        cls.web_thread.start()
        cls.web_origin = f"http://browser-fixture.test:{cls.web_server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.web_server.shutdown()
        cls.web_server.server_close()
        cls.web_thread.join(timeout=5)

    def setUp(self) -> None:
        runtime_tests = Path(__file__).resolve().parents[2] / ".runtime" / "browser-host" / "tests"
        runtime_tests.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-browser-", dir=runtime_tests)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.store = self.app.extensions["oevira.store"]
        install_browser_schema(self.store)
        if "browser_list_sessions" not in self.app.view_functions:
            register_browser_routes(self.app)
        self.service = self.app.extensions["oevira.browser"]
        self.service.url_validator = self.allow_test_target
        self.service.runtime.target_resolver = self.resolve_test_target
        self.app.config["BROWSER_LAUNCH_ARGS"] = [
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        ]
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "隔离浏览器测试"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        service = self.app.extensions.get("oevira.browser")
        if service:
            service.runtime.shutdown()
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self, token: str | None = None, *, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def allow_test_target(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme == "http" and parsed.hostname == "browser-fixture.test" and parsed.port == self.web_server.server_port:
            return url
        raise BrowserError("BROWSER_TARGET_DENIED", 403)

    def resolve_test_target(self, url: str) -> tuple[str, str, int, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "browser-fixture.test" or parsed.port != self.web_server.server_port:
            raise AcquisitionError("BROWSER_TARGET_DENIED", "The test target is outside the fixture boundary.")
        return url, parsed.hostname, parsed.port, "127.0.0.1"

    def create_browser_session(self, *, key: str | None = None) -> dict:
        response = self.request(
            "post", "/api/browser-sessions", json={"engine": "edge"},
            headers=self.auth_headers(key=key or ("browser-create-" + uuid.uuid4().hex)),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def navigate(self, session: dict, url: str, *, key: str) -> dict:
        response = self.request(
            "post", f"/api/browser-sessions/{session['id']}/navigate",
            json={"base_version": session["version"], "page_revision": session["page_revision"], "url": url},
            headers=self.auth_headers(key=key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["data"]

    def act(self, session: dict, action: dict, *, key: str) -> dict:
        response = self.request(
            "post", f"/api/browser-sessions/{session['id']}/actions",
            json={
                "base_version": session["version"],
                "page_revision": session["page_revision"],
                "action": action,
            },
            headers=self.auth_headers(key=key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["data"]

    def test_queued_assistant_operations_are_denied_after_user_takes_over(self):
        identity = authenticate(self.store, self.token)
        for kind in ("type", "navigate", "read", "capture"):
            with self.subTest(kind=kind):
                session = self.create_browser_session()
                session = self.navigate(session, self.web_origin + "/login", key=uuid.uuid4().hex)
                worker = self.service.runtime._workers[("edge", session["id"])]
                worker.call(lambda: worker._page(worker.sessions[session["id"]]).page.locator("#user").focus())
                handed = self.service.set_control(identity, session["id"], {"mode": "assistant", "base_version": session["version"]})
                entered, release = threading.Event(), threading.Event()

                def blocked():
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("test did not release its browser queue")

                def queued_operation():
                    data = {"base_version": handed["version"], "page_revision": session["page_revision"], "page_id": session["page_id"]}
                    options = {"actor": "assistant", "expected_control_generation": handed["control_generation"]}
                    if kind == "read":
                        return self.service.read_page(identity, session["id"], data, **options)
                    if kind == "type":
                        data["action"] = {"type": "type", "text": "LATE_INPUT_MUST_NOT_APPEAR"}
                        method = self.service.act
                    elif kind == "navigate":
                        data["url"] = self.web_origin + "/check-cookie"
                        method = self.service.navigate
                    else:
                        data["scope"] = "page"
                        method = self.service.capture
                    return method(identity, session["id"], data, uuid.uuid4().hex, **options)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    blocker = executor.submit(worker.call, blocked)
                    self.assertTrue(entered.wait(5))
                    operation = executor.submit(queued_operation)
                    try:
                        deadline = time.monotonic() + 5
                        while worker.queue.qsize() == 0 and not operation.done() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertGreater(worker.queue.qsize(), 0, "operation must actually be waiting in the Playwright queue")
                        self.service.set_control(identity, session["id"], {"mode": "user", "base_version": handed["version"]})
                        with patch.object(worker, "_snapshot", wraps=worker._snapshot) as snapshot:
                            release.set()
                            blocker.result(5)
                            with self.assertRaises(BrowserError) as denied:
                                operation.result(5)
                            self.assertIn(denied.exception.code, {"BROWSER_CONTROL_MODE_CONFLICT", "BROWSER_CONTROL_GENERATION_CONFLICT"})
                            self.assertEqual(denied.exception.details["dispatch_state"], "not_started")
                            snapshot.assert_not_called()
                    finally:
                        release.set()
                actual = worker.call(lambda: (worker._page(worker.sessions[session["id"]]).page.url,
                                              worker._page(worker.sessions[session["id"]]).page.locator("#user").input_value()))
                self.assertEqual(actual, (self.web_origin + "/login", ""))
                self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM materials")["n"], 0)
                if kind != "read":
                    receipt = self.store.row("SELECT status FROM browser_session_operations WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session["id"],))
                    self.assertEqual(receipt["status"], "failed")
                self.service.runtime.close("edge", session["id"])

    def test_queue_timeout_cancels_work_that_never_started(self):
        session = self.create_browser_session()
        worker = self.service.runtime._workers[("edge", session["id"])]
        entered, release, late_effect = threading.Event(), threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            blocker = executor.submit(worker.call, lambda: (entered.set(), release.wait(5)))
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(BrowserError) as timeout:
                    worker.call(late_effect.set, timeout=0.05)
                self.assertEqual(timeout.exception.code, "BROWSER_OPERATION_NOT_STARTED")
            finally:
                release.set()
                blocker.result(5)
        worker.call(lambda: None)
        self.assertFalse(late_effect.is_set())

    def test_manual_login_is_protected_on_shutdown_and_restored_after_cold_reopen(self):
        session = self.create_browser_session()
        worker = self.service.runtime._workers[("edge", session["id"])]
        # Manual page input bypasses the API's explicit checkpoint operation,
        # just as a user logging in through the embedded live view does.
        worker.call(lambda: worker._page(worker.sessions[session["id"]]).page.goto(self.web_origin + "/login"))
        worker.call(lambda: worker._page(worker.sessions[session["id"]]).page.locator("button").click())
        self.service.runtime.shutdown()
        saved = self.store.row("SELECT * FROM browser_sessions WHERE id = ?", (session["id"],))
        self.assertEqual(saved["status"], "suspended")
        self.assertEqual(saved["current_url"], self.web_origin + "/login")
        self.assertNotIn(COOKIE_SECRET.encode(), bytes(saved["state_ciphertext"]))
        self.assertEqual(saved["state_protection"], "windows_dpapi_current_user_v1")
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.store = self.app.extensions["oevira.store"]
        self.service = self.app.extensions["oevira.browser"]
        self.service.url_validator = self.allow_test_target
        self.service.runtime.target_resolver = self.resolve_test_target
        reopened = self.request("post", f"/api/browser-sessions/{session['id']}/reopen",
                                json={"base_version": saved["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(reopened.status_code, 200, reopened.json)
        restored = self.navigate(reopened.json["data"], self.web_origin + "/check-cookie", key=uuid.uuid4().hex)
        actual = self.request("post", f"/api/browser-sessions/{session['id']}/read", json={}, headers=self.auth_headers())
        self.assertEqual(actual.status_code, 200, actual.json)
        self.assertIn("Fixture session restored", actual.json["data"]["text"])
        self.assertNotIn(COOKIE_SECRET, json.dumps(restored))

    def test_stop_loading_interrupts_pending_navigation_without_waiting_for_its_queue(self):
        session = self.create_browser_session()
        identity = authenticate(self.store, self.token)
        session.update(self.service.read_page(identity, session["id"], {}, actor="user"))
        worker = self.service.runtime._workers[("edge", session["id"])]
        BrowserFixtureHandler.slow_started.clear()
        BrowserFixtureHandler.slow_release.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            navigation = executor.submit(self.service.navigate, identity, session["id"],
                                         {"base_version": session["version"], "page_revision": session["page_revision"],
                                          "page_id": session["page_id"], "url": self.web_origin + "/slow"}, uuid.uuid4().hex,
                                         actor="user", expected_control_generation=session["control_generation"])
            try:
                self.assertTrue(BrowserFixtureHandler.slow_started.wait(8))
                self.assertFalse(navigation.done())
                page = worker.sessions[session["id"]].pages[session["page_id"]]
                data = {"page_id": page.page_id, "page_revision": page.revision,
                        "control_generation": session["control_generation"]}
                denied = self.request("post", f"/api/browser-sessions/{session['id']}/stop-loading",
                                      json={**data, "control_generation": data["control_generation"] + 1}, headers=self.auth_headers())
                self.assertEqual(denied.status_code, 409, denied.json)
                started = time.monotonic()
                stopped = self.request("post", f"/api/browser-sessions/{session['id']}/stop-loading", json=data, headers=self.auth_headers())
                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(stopped.status_code, 200, stopped.json)
                self.assertEqual(stopped.json["data"]["loading_state"], "stopped")
                self.assertEqual(stopped.json["data"]["previous_effects"], "not_reversed")
                try:
                    navigation.result(5)
                except BrowserError:
                    pass  # Interrupted navigation does not become a success receipt.
                self.assertTrue(navigation.done())
                self.assertFalse(BrowserFixtureHandler.slow_release.is_set())
            finally:
                BrowserFixtureHandler.slow_release.set()
        actual = self.service.read_page(identity, session["id"], {}, actor="user")
        self.assertIn("Partial content", actual["text"])

    def test_closed_last_page_and_disconnected_owned_process_can_reopen(self):
        session = self.create_browser_session()
        identity = authenticate(self.store, self.token)
        worker = self.service.runtime._workers[("edge", session["id"])]
        worker.call(lambda: worker._page(worker.sessions[session["id"]]).page.close())
        self.assertFalse(self.service.runtime.is_open("edge", session["id"]))
        reopened = self.service.reopen_session(identity, session["id"],
                                               {"base_version": session["version"]}, uuid.uuid4().hex, actor="user")
        self.assertTrue(self.service.runtime.is_open("edge", session["id"]))
        self.assertTrue(reopened["page_id"])
        worker.call(worker.browser.close)
        self.assertFalse(self.service.runtime.is_open("edge", session["id"]))
        recovered = self.service.reopen_session(identity, session["id"],
                                                {"base_version": reopened["version"]}, uuid.uuid4().hex, actor="user")
        self.assertTrue(self.service.runtime.is_open("edge", session["id"]))
        self.assertIsNot(self.service.runtime._workers[("edge", session["id"])], worker)
        self.assertEqual(recovered["status"], "open")

    def test_shutdown_keeps_unconfirmed_worker_and_rejects_new_work(self):
        session = self.create_browser_session()
        manager = self.service.runtime
        worker = manager._workers[("edge", session["id"])]
        entered, release, late_effect = threading.Event(), threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(worker.call, lambda: (entered.set(), release.wait(8)))
            try:
                self.assertTrue(entered.wait(5))
                with patch("server.oevira.browser.WORKER_SHUTDOWN_TIMEOUT_SECONDS", 0.05):
                    with self.assertRaises(BrowserError) as stopping:
                        manager.shutdown()
                self.assertEqual(stopping.exception.code, "BROWSER_SHUTDOWN_UNCONFIRMED")
                self.assertIs(manager._workers[("edge", session["id"])], worker)
                self.assertTrue(worker.thread.is_alive())
                self.assertTrue(manager.is_stopping(session["id"]))
                with self.assertRaises(BrowserError) as rejected:
                    worker.call(late_effect.set)
                self.assertEqual(rejected.exception.details["dispatch_state"], "not_started")
                self.assertFalse(self.service._authorize_page_request(session["id"], self.web_origin))
            finally:
                release.set()
                running.result(5)
        manager.shutdown()
        self.assertFalse(worker.thread.is_alive())
        self.assertNotIn(("edge", session["id"]), manager._workers)
        self.assertFalse(late_effect.is_set())
        self.assertEqual(self.store.row("SELECT status FROM browser_sessions WHERE id=?", (session["id"],))["status"], "suspended")

    def test_cold_reopen_preserves_every_tab_and_restores_only_the_selected_page(self):
        session = self.create_browser_session()
        first_url = self.web_origin + "/check-cookie?access_token=" + URL_TOKEN
        session = self.navigate(session, first_url, key=uuid.uuid4().hex)
        first_page = session["page_id"]
        tabs_path = f"/api/browser-sessions/{session['id']}/host/tabs"
        added = self.request("post", tabs_path, json={"action": "new", "base_version": session["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(added.status_code, 200, added.json)
        session = self.navigate(added.json["data"], self.web_origin + "/login", key=uuid.uuid4().hex)
        second_page = session["page_id"]
        selected = self.request("post", tabs_path, json={"action": "select", "page_id": first_page, "base_version": session["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(selected.status_code, 200, selected.json)
        suspended = self.request("post", f"/api/browser-sessions/{session['id']}/suspend", json={"base_version": selected.json["data"]["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(suspended.status_code, 200, suspended.json)
        self.service.runtime.shutdown()
        before_requests = len(BrowserFixtureHandler.requested_paths)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.store = self.app.extensions["oevira.store"]
        self.service = self.app.extensions["oevira.browser"]
        self.service.url_validator = self.allow_test_target
        self.service.runtime.target_resolver = self.resolve_test_target
        reopened = self.request("post", f"/api/browser-sessions/{session['id']}/reopen", json={"base_version": suspended.json["data"]["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(reopened.status_code, 200, reopened.json)
        restored_tabs = self.request("get", tabs_path, headers=self.auth_headers()).json["data"]
        self.assertEqual(restored_tabs["active_page_id"], first_page)
        self.assertEqual({tab["page_id"] for tab in restored_tabs["tabs"]}, {first_page, second_page})
        self.assertTrue(all(tab["restore_required"] and tab["runtime_url"] == "about:blank" for tab in restored_tabs["tabs"]))
        self.assertNotIn(URL_TOKEN, json.dumps(restored_tabs))
        self.assertEqual(BrowserFixtureHandler.requested_paths[before_requests:], [])
        selected = self.request("post", tabs_path, json={"action": "select", "page_id": second_page, "base_version": reopened.json["data"]["version"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(selected.status_code, 200, selected.json)
        restored = self.request("post", f"/api/browser-sessions/{session['id']}/restore-page", json={"base_version": selected.json["data"]["version"], "page_id": second_page, "page_revision": selected.json["data"]["page_revision"]}, headers=self.auth_headers(key=uuid.uuid4().hex))
        self.assertEqual(restored.status_code, 200, restored.json)
        self.assertEqual(restored.json["data"]["url"], self.web_origin + "/login")
        final_tabs = self.request("get", tabs_path, headers=self.auth_headers()).json["data"]["tabs"]
        self.assertTrue(next(tab for tab in final_tabs if tab["page_id"] == first_page)["restore_required"])
        self.assertFalse(next(tab for tab in final_tabs if tab["page_id"] == second_page)["restore_required"])
        row = self.store.row("SELECT * FROM browser_sessions WHERE id=?", (session["id"],))
        saved_pages = self.service._unprotect_payload(row)["pages"]
        self.assertEqual(next(page for page in saved_pages if page["page_id"] == first_page)["url"], first_url)

    @unittest.skipUnless(os.name == "nt", "the current-user DPAPI browser session path is Windows-only")
    def test_frozen_reader_preview_saves_original_after_page_changes_and_closes(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-original")
        preview_url = f"/api/browser-sessions/{session['id']}/context-previews"
        observed = self.request("post", preview_url, json={"base_version": session["version"]}, headers=self.auth_headers())
        self.assertEqual(observed.status_code, 201, observed.json)
        preview = observed.json["data"]
        effect_status = self.service.get_operation_status(authenticate(self.store, self.token), session["id"])
        self.assertEqual(effect_status["operation"], "navigate")
        reference = preview["capture_ref"]
        frame = next(item for item in preview["frames"] if "Capture only this excerpt." in item["text"])
        block = next(item for item in frame["blocks"] if item["text"] == "Capture only this excerpt.")
        worker = self.service.runtime._worker("edge", session["id"])
        worker.call(lambda: worker.sessions[session["id"]].pages[preview["page_id"]].page.evaluate(
            "document.getElementById('excerpt').textContent='Changed after preview; never the saved quote.'"))
        latest = self.request("get", f"/api/browser-sessions/{session['id']}/context", headers=self.auth_headers())
        self.assertIn("Changed after preview", latest.json["data"]["text"])
        self.assertNotEqual(latest.json["data"]["snapshot_version"]["sha256"], reference["snapshot_sha256"])
        advanced = self.navigate(session, self.web_origin + "/later-page", key="preview-next-page")
        closed = self.request("post", f"/api/browser-sessions/{session['id']}/close",
                              json={"base_version": advanced["version"]}, headers=self.auth_headers(key="preview-close"))
        self.assertEqual(closed.status_code, 200, closed.json)
        body = {"base_version": closed.json["data"]["version"], "page_revision": preview["page_revision"],
                "page_id": preview["page_id"], "scope": "reader_block", "frame_id": frame["frame_id"],
                "frame_sha256": frame["frame_sha256"], "block_id": block["block_id"], "capture_id": reference["capture_id"]}
        with patch.object(self.service.runtime, "_worker", side_effect=AssertionError("saving a frozen preview must not reopen the browser")):
            saved = self.request("post", f"/api/browser-sessions/{session['id']}/capture", json=body,
                                 headers=self.auth_headers(key="save-original-preview"))
        self.assertEqual(saved.status_code, 201, saved.json)
        material = saved.json["data"]["material"]
        self.assertEqual(material["text"], "Capture only this excerpt.")
        self.assertEqual(material["source"]["captured_at"], reference["captured_at"])
        self.assertEqual(material["source"]["capture_id"], reference["capture_id"])
        self.assertEqual(material["source"]["snapshot_sha256"], reference["snapshot_sha256"])
        anchor = material["source"]["selection_context"]
        self.assertEqual(frame["text"][anchor["start_offset"]:anchor["end_offset"]], material["text"])
        current = self.store.row("SELECT * FROM browser_sessions WHERE id=?", (session["id"],))
        self.assertEqual(current["status"], "closed")
        self.assertEqual(current["current_url"], advanced["url"])
        self.assertEqual(current["page_revision"], advanced["page_revision"])
        self.assertIsNone(current["state_ciphertext"])
        self.assertNotIn("text", saved.json["data"]["browser"])
        persisted = self.store.row("SELECT relative_path FROM materials WHERE id=?", (material["id"],))
        self.assertEqual((self.store.root / persisted["relative_path"]).read_text(encoding="utf-8"), material["text"])
        discarded = self.request("delete", f"{preview_url}/{reference['capture_id']}", headers=self.auth_headers())
        self.assertEqual(discarded.status_code, 200, discarded.json)
        self.assertIsNone(self.store.row("SELECT preview_json FROM browser_session_operations WHERE id=?", (reference["capture_id"],))["preview_json"])
        replay = self.request("post", f"/api/browser-sessions/{session['id']}/capture", json=body,
                              headers=self.auth_headers(key="save-original-preview"))
        self.assertEqual(replay.status_code, 200, replay.json)
        self.assertEqual(replay.json["data"]["material"]["id"], material["id"])
        reopened = self.request("get", f"/api/materials/{material['id']}", headers=self.auth_headers())
        self.assertEqual(reopened.json["data"]["text"], material["text"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)

    def test_reader_preview_expiry_discard_and_revocation_do_not_resurrect_body(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-negatives")
        base = f"/api/browser-sessions/{session['id']}"
        previews = []
        for _ in range(3):
            result = self.request("post", base + "/context-previews", json={}, headers=self.auth_headers())
            self.assertEqual(result.status_code, 201, result.json)
            previews.append(result.json["data"])
        first = previews[0]
        frame = next(item for item in first["frames"] if item["text"])
        capture_id = first["capture_ref"]["capture_id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_session_operations SET preview_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (capture_id,))
        denied = self.request("get", base + f"/context-previews/{capture_id}", headers=self.auth_headers())
        self.assertEqual(denied.status_code, 410, denied.json)
        self.assertEqual(denied.json["error"]["code"], "BROWSER_PREVIEW_EXPIRED")
        invalid = self.request("post", base + "/capture", json={"capture_id": capture_id, "scope": "reader_frame",
                               "base_version": session["version"], "page_revision": first["page_revision"], "page_id": first["page_id"],
                               "frame_id": frame["frame_id"], "frame_sha256": frame["frame_sha256"]},
                               headers=self.auth_headers(key="expired-preview"))
        self.assertEqual(invalid.status_code, 410, invalid.json)
        receipt = self.request("get", base + f"/operations/{capture_id}", headers=self.auth_headers())
        self.assertEqual(receipt.status_code, 200, receipt.json)
        self.assertNotIn("Capture only this excerpt.", receipt.get_data(as_text=True))
        second_id = previews[1]["capture_ref"]["capture_id"]
        self.request("delete", base + f"/context-previews/{second_id}", headers=self.auth_headers())
        discarded = self.request("get", base + f"/context-previews/{second_id}", headers=self.auth_headers())
        self.assertEqual(discarded.status_code, 410, discarded.json)
        self.assertEqual(discarded.json["error"]["code"], "BROWSER_PREVIEW_DISCARDED")
        with self.store.transaction() as connection:
            connection.execute("UPDATE memberships SET active=0 WHERE workspace_id=? AND user_id=?",
                               (self.identity["workspace"]["id"], self.identity["user"]["id"]))
        revoked = self.request("get", base + f"/context-previews/{previews[2]['capture_ref']['capture_id']}", headers=self.auth_headers())
        self.assertIn(revoked.status_code, {401, 403}, revoked.json)
        self.assertNotIn("Capture only this excerpt.", revoked.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)

    def test_model_tool_captures_owner_preview_without_reading_changed_page(self) -> None:
        work = self.request("post", "/api/works", json={"title": "Frozen source", "goal": "Use the selected excerpt"},
                            headers=self.auth_headers()).json["data"]
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="model-preview-page")
        base = f"/api/browser-sessions/{session['id']}"
        bound = self.request("post", base + "/work", json={"work_id": work["id"], "base_version": session["version"]},
                             headers=self.auth_headers()).json["data"]
        controlled = self.request("post", base + "/control", json={"mode": "assistant", "base_version": bound["version"]},
                                  headers=self.auth_headers()).json["data"]
        identity = authenticate(self.store, self.token)
        intelligence = self.app.extensions["oevira.intelligence"]
        prepared = intelligence.prepare_context(identity, "Save this selected excerpt", work_id=work["id"],
                                                  include_memory=False, artifact_write_intent=True)
        context = intelligence.create_tool_context(prepared, caller="model", operation_scope="frozen-reader-tool")
        read = intelligence.tools.invoke(identity, "browser.page.read", {
            "browser_session_id": session["id"], "base_version": controlled["version"], "retain_preview": True,
        }, context)["result"]
        self.assertNotIn("Capture only this excerpt.", json.dumps(read))
        reference = read["capture_ref"]
        preview = self.service.get_context_preview(identity, session["id"], reference["capture_id"])
        frame = next(item for item in preview["frames"] if item["text"])
        block = next(item for item in frame["blocks"] if item["text"] == "Capture only this excerpt.")
        worker = self.service.runtime._worker("edge", session["id"])
        worker.call(lambda: worker.sessions[session["id"]].pages[preview["page_id"]].page.evaluate(
            "document.getElementById('excerpt').textContent='Changed source'"))
        with patch("server.oevira.browser_host_reader.read_browser_context", side_effect=AssertionError("must reuse the issued snapshot")), \
             patch("server.oevira.browser_host_reader.capture_reader_context", side_effect=AssertionError("must not recapture")):
            saved = intelligence.tools.invoke(identity, "browser.page.capture", {
                "browser_session_id": session["id"], "base_version": controlled["version"],
                "capture_id": reference["capture_id"], "frame_id": frame["frame_id"], "block_id": block["block_id"],
            }, context)["result"]
        self.assertEqual(saved["material"]["text"], "Capture only this excerpt.")
        self.assertEqual(saved["material"]["work_id"], work["id"])
        self.assertIn(saved["material"]["id"], context.allowed_material_ids)

    def test_preview_corruption_and_discard_during_save_cannot_leave_readable_material(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-integrity")
        base = f"/api/browser-sessions/{session['id']}"
        retained = self.request("post", base + "/context-previews", json={}, headers=self.auth_headers()).json["data"]
        capture_id = retained["capture_ref"]["capture_id"]
        original = self.store.row("SELECT preview_json FROM browser_session_operations WHERE id=?", (capture_id,))["preview_json"]
        corrupt = json.loads(original)
        corrupt["frames"][0]["text"] = "Fabricated text preserving the old hashes"
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_session_operations SET preview_json=? WHERE id=?", (json.dumps(corrupt), capture_id))
        rejected = self.request("get", base + f"/context-previews/{capture_id}", headers=self.auth_headers())
        self.assertEqual(rejected.status_code, 409, rejected.json)
        self.assertEqual(rejected.json["error"]["code"], "BROWSER_PREVIEW_INTEGRITY_ERROR")
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_session_operations SET preview_json=? WHERE id=?", (original, capture_id))
        frame = next(item for item in retained["frames"] if item["text"])
        identity = authenticate(self.store, self.token)
        targets = []
        replace = os.replace

        def replace_then_discard(source, target):
            replace(source, target)
            targets.append(Path(target))
            self.service.discard_context_preview(identity, session["id"], capture_id)

        with patch("server.oevira.browser_api.os.replace", side_effect=replace_then_discard):
            refused = self.request("post", base + "/capture", json={"capture_id": capture_id, "scope": "reader_frame",
                                   "base_version": session["version"], "page_revision": retained["page_revision"],
                                   "page_id": retained["page_id"], "frame_id": frame["frame_id"], "frame_sha256": frame["frame_sha256"]},
                                   headers=self.auth_headers(key="discard-before-commit"))
        self.assertEqual(refused.status_code, 410, refused.json)
        self.assertEqual(refused.json["error"]["code"], "BROWSER_PREVIEW_DISCARDED")
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].exists())
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)
        receipt = self.store.row("SELECT * FROM browser_session_operations WHERE idempotency_key='discard-before-commit'")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "BROWSER_PREVIEW_DISCARDED")
        self.assertIsNone(receipt["material_id"])
        self.assertIsNone(receipt["result_json"])

    def test_restart_does_not_promote_interrupted_preview_save_after_expiry(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-restart")
        base = f"/api/browser-sessions/{session['id']}"
        preview = self.request("post", base + "/context-previews", json={}, headers=self.auth_headers()).json["data"]
        capture_id = preview["capture_ref"]["capture_id"]
        frame = next(item for item in preview["frames"] if item["text"])
        targets = []
        replace = os.replace

        def interrupt_after_durable_file(source, target):
            replace(source, target)
            targets.append(Path(target))
            raise RuntimeError("simulated interruption before material commit")

        with patch("server.oevira.browser_api.os.replace", side_effect=interrupt_after_durable_file):
            interrupted = self.request("post", base + "/capture", json={"capture_id": capture_id, "scope": "reader_frame",
                                       "base_version": session["version"], "page_revision": preview["page_revision"],
                                       "page_id": preview["page_id"], "frame_id": frame["frame_id"], "frame_sha256": frame["frame_sha256"]},
                                       headers=self.auth_headers(key="interrupted-preview-save"))
        self.assertEqual(interrupted.status_code, 500, interrupted.json)
        self.assertEqual(self.store.row("SELECT status FROM materials")["status"], "pending")
        self.assertTrue(targets[0].is_file())
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_session_operations SET preview_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (capture_id,))
        self.service.runtime.shutdown()
        restarted = create_app(self.root, expected_port=8317)
        try:
            store = restarted.extensions["oevira.store"]
            self.assertEqual(store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)
            receipt = store.row("SELECT * FROM browser_session_operations WHERE idempotency_key='interrupted-preview-save'")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["error_code"], "BROWSER_PREVIEW_SAVE_INTERRUPTED")
            self.assertIsNone(receipt["material_id"])
            self.assertFalse(targets[0].exists())
        finally:
            restarted.extensions["oevira.browser"].runtime.shutdown()

    def test_source_use_fields_and_revocation_fence_frozen_preview_commit(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-source-use")
        base = f"/api/browser-sessions/{session['id']}"
        preview = self.request("post", base + "/context-previews", json={}, headers=self.auth_headers()).json["data"]
        capture_id = preview["capture_ref"]["capture_id"]
        workspace_id, user_id = self.identity["workspace"]["id"], self.identity["user"]["id"]
        with self.store.transaction() as connection:
            for decision_id, purpose, action in (("preview-read", "knowledge_retrieval", "read"),
                                                 ("preview-save", "source_capture", "derive")):
                connection.execute(
                    "INSERT INTO source_use_decisions(decision_id,workspace_id,owner_user_id,object_kind,object_id,object_revision,"
                    "scope_kind,field_paths_json,purpose,action,recipient_kind,recipient_ref,basis_status,decision_status,"
                    "policy_scope_sha256,version,created_at,updated_at) "
                    "VALUES(?,?,?,'browser_capture',?,?,'workspace','[\"text\"]',?,?,'local',?,'user_declared','allowed',?,1,?,?)",
                    (decision_id, workspace_id, user_id, capture_id, preview["capture_ref"]["preview_sha256"],
                     purpose, action, workspace_id, decision_id, utc_now(), utc_now()),
                )
        readable = self.request("get", base + f"/context-previews/{capture_id}?fields=text", headers=self.auth_headers())
        self.assertEqual(readable.status_code, 200, readable.json)
        self.assertIn("Capture only this excerpt.", readable.json["data"]["text"])
        for projected in (readable.json["data"], *readable.json["data"]["frames"]):
            self.assertNotIn("controls", projected)
            self.assertNotIn("links", projected)
        denied_fields = self.request("get", base + f"/context-previews/{capture_id}", headers=self.auth_headers())
        self.assertEqual(denied_fields.status_code, 403, denied_fields.json)
        frame = next(item for item in preview["frames"] if item["text"])
        targets = []
        replace = os.replace

        def replace_then_revoke(source, target):
            replace(source, target)
            targets.append(Path(target))
            with self.store.transaction() as connection:
                connection.execute("UPDATE source_use_decisions SET decision_status='revoked',version=version+1,revoked_at=? WHERE decision_id='preview-save'", (utc_now(),))

        with patch("server.oevira.browser_api.os.replace", side_effect=replace_then_revoke):
            refused = self.request("post", base + "/capture", json={"capture_id": capture_id, "scope": "reader_frame",
                                   "base_version": session["version"], "page_revision": preview["page_revision"],
                                   "page_id": preview["page_id"], "frame_id": frame["frame_id"], "frame_sha256": frame["frame_sha256"]},
                                   headers=self.auth_headers(key="source-use-revoked-at-commit"))
        self.assertEqual(refused.status_code, 403, refused.json)
        self.assertEqual(refused.json["error"]["code"], "SOURCE_USE_REVOKED")
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].exists())
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)

    def test_role_downgrade_during_preview_save_withdraws_uncommitted_material(self) -> None:
        session = self.navigate(self.create_browser_session(), self.web_origin + "/check-cookie", key="preview-role-change")
        base = f"/api/browser-sessions/{session['id']}"
        preview = self.request("post", base + "/context-previews", json={}, headers=self.auth_headers()).json["data"]
        frame = next(item for item in preview["frames"] if item["text"])
        replace = os.replace
        targets = []

        def replace_then_downgrade(source, target):
            replace(source, target)
            targets.append(Path(target))
            with self.store.transaction() as connection:
                connection.execute("UPDATE memberships SET role='viewer' WHERE workspace_id=? AND user_id=?",
                                   (self.identity["workspace"]["id"], self.identity["user"]["id"]))

        with patch("server.oevira.browser_api.os.replace", side_effect=replace_then_downgrade):
            refused = self.request("post", base + "/capture", json={
                "capture_id": preview["capture_ref"]["capture_id"], "scope": "reader_frame",
                "base_version": session["version"], "page_revision": preview["page_revision"],
                "page_id": preview["page_id"], "frame_id": frame["frame_id"], "frame_sha256": frame["frame_sha256"],
            }, headers=self.auth_headers(key="downgraded-during-preview-save"))
        self.assertEqual(refused.status_code, 403, refused.json)
        self.assertEqual(refused.json["error"]["code"], "ACCESS_REVOKED")
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].exists())
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)

    def test_real_edge_contexts_are_isolated_and_capture_reopens_as_same_material(self) -> None:
        first = self.create_browser_session(key="browser-first-create")
        replayed_create = self.request(
            "post", "/api/browser-sessions", json={"engine": "edge"},
            headers=self.auth_headers(key="browser-first-create"),
        )
        self.assertEqual(replayed_create.status_code, 200)
        self.assertEqual(replayed_create.json["data"]["id"], first["id"])
        self.assertEqual(first["engine"], "edge")
        self.assertEqual(first["login_state"], "NOT_VERIFIED")
        self.assertEqual(first["state_protection"], "windows_dpapi_current_user_v1")

        blocked_private = self.request(
            "post", f"/api/browser-sessions/{first['id']}/navigate",
            json={"base_version": first["version"], "page_revision": first["page_revision"], "url": "http://127.0.0.1:1/private"},
            headers=self.auth_headers(key="deny-private-target"),
        )
        self.assertEqual(blocked_private.status_code, 403)
        self.assertEqual(blocked_private.json["error"]["code"], "BROWSER_TARGET_DENIED")

        second = self.create_browser_session(key="browser-second-create")
        first = self.navigate(first, self.web_origin + "/login", key="browser-first-navigation")
        self.assertGreater(first["page_revision"], 0)
        first = self.act(first, {"type": "click", "x": 40, "y": 30}, key="login-click-user")
        first = self.act(first, {"type": "type", "text": "synthetic-browser-user"}, key="login-type-user")
        first = self.act(first, {"type": "press", "key": "Tab"}, key="login-tab-to-password")
        first = self.act(first, {"type": "type", "text": PASSWORD_FIXTURE}, key="login-type-password")
        password_result = self.act(first, {"type": "press", "key": "Enter"}, key="login-submit-fixture")
        self.assertNotIn(PASSWORD_FIXTURE, json.dumps(password_result, ensure_ascii=False))
        first = password_result
        first_page = self.request(
            "post", f"/api/browser-sessions/{first['id']}/read",
            json={"page_revision": first["page_revision"]}, headers=self.auth_headers(),
        )
        self.assertEqual(first_page.status_code, 200, first_page.get_data(as_text=True))
        self.assertIn("Fixture login complete", first_page.json["data"]["text"])
        self.assertNotIn(COOKIE_SECRET, first_page.get_data(as_text=True))

        other_page = self.navigate(second, self.web_origin + "/check-cookie", key="browser-second-cookie-check")
        other_read = self.request(
            "post", f"/api/browser-sessions/{second['id']}/read",
            json={"page_revision": other_page["page_revision"]}, headers=self.auth_headers(),
        )
        self.assertEqual(other_read.status_code, 200, other_read.get_data(as_text=True))
        self.assertIn("Fixture session absent", other_read.json["data"]["text"])

        screenshot = self.request("get", f"/api/browser-sessions/{first['id']}/screenshot", headers=self.auth_headers())
        self.assertEqual(screenshot.status_code, 200)
        self.assertEqual(screenshot.mimetype, "image/png")
        self.assertTrue(screenshot.data.startswith(b"\x89PNG\r\n\x1a\n"))

        first_read_state = {**first, **first_page.json["data"]}
        tokenized_url = self.web_origin + "/check-cookie?access_token=" + URL_TOKEN
        check_first = self.navigate(first_read_state, tokenized_url, key="browser-first-cookie-check")
        self.assertNotIn(URL_TOKEN, check_first["url"])
        self.assertEqual(check_first["redacted_query_keys"], ["access_token"])
        first_page = self.request(
            "post", f"/api/browser-sessions/{first['id']}/read",
            json={"page_revision": check_first["page_revision"]}, headers=self.auth_headers(),
        )
        self.assertEqual(first_page.status_code, 200, first_page.get_data(as_text=True))
        self.assertIn("Fixture session restored", first_page.json["data"]["text"])

        # Set an actual DOM selection in the live Edge page. The HTTP capture
        # endpoint reads the selection from the browser context; it does not accept client text.
        page_id = first_page.json["data"]["page_id"]
        worker = self.service.runtime._worker("edge", first["id"])

        def websocket_echo():
            live = worker.sessions[first["id"]]
            page = live.pages[page_id].page
            return page.evaluate("""url => new Promise(resolve => {
                const socket = new WebSocket(url);
                const timeout = setTimeout(() => { socket.close(); resolve('timeout'); }, 5000);
                socket.onopen = () => socket.send('browser-proxy-websocket');
                socket.onmessage = event => { clearTimeout(timeout); socket.close(); resolve(event.data); };
                socket.onerror = () => { clearTimeout(timeout); resolve('error'); };
            })""", f"ws://browser-fixture.test:{self.web_server.server_port}/ws")

        self.assertEqual(worker.call(websocket_echo), "browser-proxy-websocket")

        def select_exact_text():
            live = worker.sessions[first["id"]]
            page = live.pages[page_id].page
            return page.evaluate("""() => {
                const node = document.getElementById('excerpt').firstChild;
                const range = document.createRange();
                range.setStart(node, 0);
                range.setEnd(node, 'Capture only this excerpt.'.length);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                return selection.toString();
            }""")
        self.assertEqual(worker.call(select_exact_text), "Capture only this excerpt.")
        capture_body = {
            "base_version": first_page.json["data"]["version"],
            "page_revision": first_page.json["data"]["page_revision"],
            "page_id": page_id,
            "scope": "selection",
        }
        captured = self.request(
            "post", f"/api/browser-sessions/{first['id']}/capture", json=capture_body,
            headers=self.auth_headers(key="capture-exact-selection"),
        )
        self.assertEqual(captured.status_code, 201, captured.get_data(as_text=True))
        material = captured.json["data"]["material"]
        self.assertEqual(material["text"], "Capture only this excerpt.")
        self.assertEqual(material["source"]["kind"], "controlled_browser_snapshot")
        self.assertEqual(material["source"]["actual_access_class"], "controlled_browser_session")
        self.assertEqual(material["source"]["authentication_state"], "NOT_VERIFIED")
        self.assertEqual(material["source"]["source_admission"], "NOT_ADMITTED")
        self.assertEqual(material["source"]["content_trust"], "UNTRUSTED_WEB_CONTENT")
        self.assertEqual(material["source"]["source_url"], self.web_origin + "/check-cookie?access_token=%5Bredacted%5D")
        self.assertEqual(material["source"]["redacted_query_keys"], ["access_token"])
        self.assertEqual(material["source"]["page_revision"], first_page.json["data"]["page_revision"])
        self.assertNotIn(URL_TOKEN, json.dumps(material, ensure_ascii=False))
        self.assertEqual(material["source"]["selection_context"]["location_state"], "unique_visible_text_match")
        self.assertLess(material["source"]["selection_context"]["start_offset"], material["source"]["selection_context"]["end_offset"])
        self.assertIsNone(material["source"]["original_resource_sha256"])
        replay = self.request(
            "post", f"/api/browser-sessions/{first['id']}/capture", json=capture_body,
            headers=self.auth_headers(key="capture-exact-selection"),
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"]["material"]["id"], material["id"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)
        saved_source = self.store.row("SELECT source_url, source_json FROM materials WHERE id = ?", (material["id"],))
        self.assertEqual(saved_source["source_url"], material["source"]["source_url"])
        self.assertNotIn(URL_TOKEN, saved_source["source_json"])
        saved_browser_session = self.store.row(
            "SELECT current_url, state_ciphertext FROM browser_sessions WHERE id = ?", (first["id"],),
        )
        self.assertNotIn(URL_TOKEN, saved_browser_session["current_url"])
        self.assertNotIn(URL_TOKEN.encode("utf-8"), bytes(saved_browser_session["state_ciphertext"]))
        self.assertNotIn(URL_TOKEN, captured.get_data(as_text=True))
        reopened_material = self.request("get", f"/api/materials/{material['id']}", headers=self.auth_headers())
        self.assertEqual(reopened_material.status_code, 200, reopened_material.get_data(as_text=True))
        self.assertEqual(reopened_material.json["data"]["text"], "Capture only this excerpt.")
        self.assertNotIn(URL_TOKEN, reopened_material.get_data(as_text=True))

        session_row = self.store.row("SELECT state_ciphertext FROM browser_sessions WHERE id = ?", (first["id"],))
        self.assertNotIn(COOKIE_SECRET.encode("utf-8"), bytes(session_row["state_ciphertext"]))
        self.assertNotIn(PASSWORD_FIXTURE.encode("utf-8"), bytes(session_row["state_ciphertext"]))
        self.assertNotIn(COOKIE_SECRET, captured.get_data(as_text=True))

        latest = self.request("get", f"/api/browser-sessions/{first['id']}", headers=self.auth_headers()).json["data"]
        suspended = self.request(
            "post", f"/api/browser-sessions/{first['id']}/suspend",
            json={"base_version": latest["version"]}, headers=self.auth_headers(key="suspend-first"),
        )
        self.assertEqual(suspended.status_code, 200, suspended.get_data(as_text=True))
        self.assertEqual(suspended.json["data"]["status"], "suspended")
        refreshed_product_token = self.create_another_product_session()
        old_session_read = self.request(
            "post", f"/api/browser-sessions/{first['id']}/read",
            json={}, headers=self.auth_headers(),
        )
        self.assertEqual(old_session_read.status_code, 409)
        self.assertEqual(old_session_read.json["error"]["code"], "BROWSER_SESSION_NOT_OPEN")
        reopened = self.request(
            "post", f"/api/browser-sessions/{first['id']}/reopen",
            json={"base_version": suspended.json["data"]["version"], "restore_page": False},
            headers=self.auth_headers(refreshed_product_token, key="reopen-first"),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertTrue(reopened.json["data"]["restore_required"])
        stale_product_session = self.request(
            "post", f"/api/browser-sessions/{first['id']}/read", json={}, headers=self.auth_headers(),
        )
        self.assertEqual(stale_product_session.status_code, 409)
        self.assertEqual(stale_product_session.json["error"]["code"], "BROWSER_REAUTH_REQUIRED")
        restored_page = self.request(
            "post", f"/api/browser-sessions/{first['id']}/restore-page",
            json={"base_version": reopened.json["data"]["version"], "page_revision": reopened.json["data"]["page_revision"]},
            headers=self.auth_headers(refreshed_product_token, key="restore-last-page"),
        )
        self.assertEqual(restored_page.status_code, 200, restored_page.get_data(as_text=True))
        restored_read = self.request(
            "post", f"/api/browser-sessions/{first['id']}/read",
            json={"page_revision": restored_page.json["data"]["page_revision"]}, headers=self.auth_headers(refreshed_product_token),
        )
        self.assertIn("Fixture session restored", restored_read.json["data"]["text"])

        # A workspace peer cannot read this user's browser context or its session list.
        peer_token = self.create_peer()
        hidden = self.request("get", f"/api/browser-sessions/{first['id']}", headers=self.auth_headers(peer_token))
        self.assertEqual(hidden.status_code, 404)
        peer_sessions = self.request("get", "/api/browser-sessions", headers=self.auth_headers(peer_token))
        self.assertEqual(peer_sessions.status_code, 200)
        self.assertEqual(peer_sessions.json["data"], [])

        blocked_private = self.request(
            "post", f"/api/browser-sessions/{second['id']}/navigate",
            json={
                "base_version": other_page["version"],
                "page_revision": other_page["page_revision"],
                "url": "http://127.0.0.1:1/private",
            },
            headers=self.auth_headers(key="deny-private-browser-target"),
        )
        self.assertEqual(blocked_private.status_code, 403)
        self.assertEqual(blocked_private.json["error"]["code"], "BROWSER_TARGET_DENIED")

        closed = self.request(
            "post", f"/api/browser-sessions/{first['id']}/close",
            json={"base_version": restored_read.json["data"]["version"]},
            headers=self.auth_headers(refreshed_product_token, key="close-first"),
        )
        self.assertEqual(closed.status_code, 200, closed.get_data(as_text=True))
        self.assertEqual(closed.json["data"]["status"], "closed")
        self.assertIsNone(self.store.row("SELECT state_ciphertext FROM browser_sessions WHERE id = ?", (first["id"],))["state_ciphertext"])

        changed_membership = "2020-01-01T00:00:00Z"
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET created_at = ? WHERE workspace_id = ? AND user_id = ?",
                (changed_membership, self.identity["workspace"]["id"], self.identity["user"]["id"]),
            )
        revoked_generation = self.request("get", f"/api/browser-sessions/{second['id']}", headers=self.auth_headers())
        self.assertEqual(revoked_generation.status_code, 404)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            revoked_row = self.store.row("SELECT status, state_ciphertext FROM browser_sessions WHERE id = ?", (second["id"],))
            if revoked_row["status"] == "closed":
                break
            time.sleep(0.05)
        self.assertEqual(revoked_row["status"], "closed")
        self.assertIsNone(revoked_row["state_ciphertext"])

    @unittest.skipUnless(os.name == "nt", "the current-user DPAPI browser session path is Windows-only")
    def test_stale_reopen_stays_unknown_while_reconcile_restores_page_operations(self) -> None:
        session = self.create_browser_session(key="browser-recovery-create")
        page = self.navigate(session, self.web_origin + "/check-cookie", key="browser-recovery-first-navigation")
        current = self.store.row("SELECT version FROM browser_sessions WHERE id = ?", (session["id"],))
        old_operation_id = "00000000-0000-4000-8000-000000000001"
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute("UPDATE browser_session_operations SET updated_at=? WHERE session_id=?", (now, session["id"]))
            connection.execute(
                "INSERT INTO browser_session_operations(id, session_id, workspace_id, owner_user_id, operation, "
                "idempotency_key, request_hash, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'reopen', 'stale-reopen-original', ?, 'pending', ?, ?)",
                (old_operation_id, session["id"], self.identity["workspace"]["id"],
                 self.identity["user"]["id"],
                 _request_fingerprint({"base_version": current["version"], "restore_page": False}), now, now),
            )
            connection.execute(
                "UPDATE browser_sessions SET status = 'reopening', operation_id = ?, "
                "operation_expires_at = '2000-01-01T00:00:00Z' WHERE id = ? AND version = ?",
                (old_operation_id, session["id"], current["version"]),
            )

        stale_navigation = self.request(
            "post", f"/api/browser-sessions/{session['id']}/navigate",
            json={
                "base_version": current["version"],
                "page_revision": page["page_revision"],
                "url": self.web_origin + "/login",
            },
            headers=self.auth_headers(key="navigation-after-stale-reopen"),
        )
        self.assertEqual(stale_navigation.status_code, 409)
        self.assertEqual(stale_navigation.json["error"]["code"], "BROWSER_OPERATION_UNKNOWN")
        self.assertTrue(stale_navigation.json["error"]["details"]["reconcile_required"])
        self.assertNotIn("operation_id", stale_navigation.get_data(as_text=True))
        self.assertNotIn(old_operation_id, stale_navigation.get_data(as_text=True))

        operation_status = self.request(
            "get", f"/api/browser-sessions/{session['id']}/operation-status", headers=self.auth_headers(),
        )
        self.assertEqual(operation_status.status_code, 200)
        self.assertEqual(operation_status.json["data"]["operation"], "reopen")
        self.assertEqual(operation_status.json["data"]["status"], "unknown")
        self.assertNotIn("id", operation_status.json["data"])
        self.assertNotIn(old_operation_id, operation_status.get_data(as_text=True))

        runtime_reopen = self.service.runtime.reopen
        with patch.object(self.service.runtime, "reopen", wraps=runtime_reopen) as reopen_runtime:
            old_key_replay = self.request(
                "post", f"/api/browser-sessions/{session['id']}/reopen",
                json={"base_version": current["version"], "restore_page": False},
                headers=self.auth_headers(key="stale-reopen-original"),
            )
        self.assertEqual(old_key_replay.status_code, 409)
        self.assertEqual(old_key_replay.json["error"]["code"], "BROWSER_OPERATION_UNKNOWN")
        self.assertEqual(reopen_runtime.call_count, 0)

        reconciled = self.request(
            "post", f"/api/browser-sessions/{session['id']}/reconcile",
            json={"base_version": current["version"]},
            headers=self.auth_headers(key="browser-reconcile-stale-reopen"),
        )
        self.assertEqual(reconciled.status_code, 200, reconciled.get_data(as_text=True))
        recovery = reconciled.json["data"]
        self.assertEqual(recovery["recovery_state"], "live_page_inspected")
        self.assertEqual(recovery["previous_operation"], {
            "operation": "reopen", "status": "unknown",
            "error_code": "BROWSER_OPERATION_STATE_UNKNOWN",
            "updated_at": self.store.row(
                "SELECT updated_at FROM browser_session_operations WHERE id = ?", (old_operation_id,),
            )["updated_at"],
        })
        self.assertEqual(recovery["status"], "open")
        self.assertNotIn(old_operation_id, reconciled.get_data(as_text=True))

        inspected = self.request(
            "post", f"/api/browser-sessions/{session['id']}/read",
            json={"page_revision": recovery["page_revision"]}, headers=self.auth_headers(),
        )
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        self.assertIn("Fixture session absent", inspected.json["data"]["text"])

        continued = self.navigate(
            {**session, "version": recovery["version"], "page_revision": inspected.json["data"]["page_revision"]},
            self.web_origin + "/login", key="browser-navigation-after-reconcile",
        )
        self.assertGreater(continued["page_revision"], inspected.json["data"]["page_revision"])
        closed = self.request(
            "post", f"/api/browser-sessions/{session['id']}/close",
            json={"base_version": continued["version"]},
            headers=self.auth_headers(key="browser-close-after-reconcile"),
        )
        self.assertEqual(closed.status_code, 200, closed.get_data(as_text=True))
        self.assertEqual(closed.json["data"]["status"], "closed")
        preserved_old_operation = self.store.row(
            "SELECT status, error_code FROM browser_session_operations WHERE id = ?", (old_operation_id,),
        )
        self.assertEqual(preserved_old_operation["status"], "unknown")
        self.assertEqual(preserved_old_operation["error_code"], "BROWSER_OPERATION_STATE_UNKNOWN")

    def create_peer(self) -> str:
        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, '会话隔离同事', ?)", (user_id, now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'editor', 1, ?)",
                (self.identity["workspace"]["id"], user_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, self.identity["workspace"]["id"], now, expires),
            )
        return token

    def create_another_product_session(self) -> str:
        token = secrets.token_urlsafe(32)
        session_id = str(uuid.uuid4())
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (session_id, _digest(token), self.identity["user"]["id"], self.identity["workspace"]["id"], now, expires),
            )
        return token


class PinnedBrowserProxyTests(unittest.TestCase):
    def test_public_validation_followed_by_private_dns_rebinding_never_connects(self) -> None:
        proxy = PinnedBrowserProxy()
        proxy_settings = proxy.playwright_settings
        proxy_address = urlsplit(proxy_settings["server"])
        client = socket.create_connection((proxy_address.hostname, proxy_address.port), timeout=3)
        credential = base64.b64encode(
            f"{proxy_settings['username']}:{proxy_settings['password']}".encode("ascii")
        ).decode("ascii")
        dns_calls = 0
        connection_attempts = []
        original_getaddrinfo = socket.getaddrinfo

        def rebinding_dns(host, port, *args, **kwargs):
            nonlocal dns_calls
            if host == "rebind.example.test":
                dns_calls += 1
                address = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
            return original_getaddrinfo(host, port, *args, **kwargs)

        def deny_outbound_connect(_connection, address):
            connection_attempts.append(address)
            raise OSError("test guard blocked an upstream socket attempt")

        request = (
            "GET http://rebind.example.test/ HTTP/1.1\r\n"
            "Host: rebind.example.test\r\n"
            f"Proxy-Authorization: Basic {credential}\r\n\r\n"
        ).encode("ascii")
        try:
            with patch("server.oevira.acquisition.socket.getaddrinfo", side_effect=rebinding_dns), \
                    patch.object(socket.socket, "connect", new=deny_outbound_connect):
                checked = public_target("http://rebind.example.test/")
                self.assertEqual(checked[3], "93.184.216.34")
                client.sendall(request)
                response = client.recv(4096)
        finally:
            client.close()
            proxy.close()

        self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden"), response)
        self.assertEqual(dns_calls, 2, "policy validation and proxy pinning must observe the simulated rebind")
        self.assertEqual(connection_attempts, [], "a denied rebinding result must not reach socket.connect")
        self.assertEqual(proxy.blocked_target_count, 1)


class DpapiStateProtectionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is available only on Windows")
    def test_session_state_uses_user_scoped_protection_and_workspace_entropy(self) -> None:
        from server.oevira.browser import BrowserError

        protector = WindowsUserStateProtector()
        value = b"synthetic-session-state"
        entropy = b"one workspace/user/session"
        encrypted = protector.protect(value, entropy)
        self.assertNotEqual(encrypted, value)
        self.assertEqual(protector.unprotect(encrypted, entropy), value)
        with self.assertRaises(BrowserError):
            protector.unprotect(encrypted, b"another workspace/user/session")


if __name__ == "__main__":
    unittest.main()
