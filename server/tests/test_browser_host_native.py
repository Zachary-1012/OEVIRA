from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

from server.oevira import create_app
from server.oevira.acquisition import AcquisitionError
from server.oevira.auth import issue_local_capability


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".runtime" / "browser-native-tests"
BASE_URL = "http://127.0.0.1:8317"
FIXTURE_HOST = "native-browser.fixture.test"


class NativeBrowserFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/redirect-first":
            self.send_response(302)
            self.send_header("Location", "/same-title?tab=first")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path != "/same-title":
            self.send_error(404)
            return
        tab = parse_qs(parsed.query).get("tab", ["unknown"])[0]
        body = (
            "<!doctype html><meta charset=utf-8><title>Shared native tab title</title>"
            f"<main><h1>Shared native tab title</h1><p id='marker'>Owned page marker: {tab}</p></main>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class BrowserHostNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        cls.previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
        for key in cls.previous_temp:
            os.environ[key] = str(RUNTIME_ROOT)
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), NativeBrowserFixture)
        cls.site_thread = threading.Thread(target=cls.site.serve_forever, daemon=True)
        cls.site_thread.start()
        cls.origin = f"http://{FIXTURE_HOST}:{cls.site.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()
        cls.site_thread.join(timeout=5)
        for key, value in cls.previous_temp.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        if os.name != "nt":
            self.skipTest("The native Chrome HWND probe requires Windows")
        self.temporary = tempfile.TemporaryDirectory(prefix="native-", dir=RUNTIME_ROOT)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / "data", expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=False)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.host = self.app.extensions["oevira.browser_host"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        capability_file = self.browser.store.root / "launch.capability"
        issue_local_capability(self.browser.store, capability_file)
        response = self.client.post(
            "/api/local-session",
            base_url=BASE_URL,
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "Isolated native Chrome test"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        created = self.request("post", "/api/browser-sessions", data={})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.session = created.json["data"]
        self.session["page_id"] = self.host.runtime.tabs("chrome", self.session["id"], "list")["active_page_id"]
        self.additional_streams = []
        self.stream = None
        self.native_window_probe = None

    def tearDown(self):
        for stream in self.additional_streams:
            stream.close()
        if self.stream is not None:
            self.stream.close()
        self.browser.runtime.shutdown()
        self.temporary.cleanup()

    def request(self, method, path, *, data=None, headers=None):
        request_headers = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": uuid.uuid4().hex}
        if headers:
            request_headers.update(headers)
        kwargs = {"base_url": BASE_URL, "headers": request_headers}
        if data is not None:
            kwargs["json"] = data
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def allow_url(self, url):
        if urlsplit(url).netloc == urlsplit(self.origin).netloc:
            return url
        raise AcquisitionError("BROWSER_TARGET_DENIED", "The isolated native browser fixture rejected the target.")

    def resolve_url(self, url):
        if urlsplit(url).netloc != urlsplit(self.origin).netloc:
            raise AcquisitionError("BROWSER_TARGET_DENIED", "The isolated native browser fixture rejected the target.")
        return url, FIXTURE_HOST, self.site.server_port, "127.0.0.1"

    def navigate(self, session_id: str, page_id: str, url: str):
        current = self.request("get", f"/api/browser-sessions/{session_id}")
        self.assertEqual(current.status_code, 200, current.get_data(as_text=True))
        state = current.json["data"]
        response = self.request(
            "post", f"/api/browser-sessions/{session_id}/navigate",
            data={"base_version": state["version"], "page_revision": state["page_revision"], "page_id": page_id, "url": url},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["data"]

    def pump_until(self, stream, page_id: str, *, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            stream.pump()
            if stream.page_id == page_id:
                return
            time.sleep(0.025)
        self.fail(f"Native active-tab probe did not select owned page {page_id!r}; got {stream.page_id!r}")

    def test_native_tabstrip_switch_ignores_world_spoof_and_capture_uses_visible_page(self):
        first_url = f"{self.origin}/redirect-first"
        first = self.navigate(self.session["id"], self.session["page_id"], first_url)
        first_page_id = first["page_id"]
        self.stream = self.host.runtime.start_stream(
            "chrome", self.session["id"], first_page_id, native=True,
        )
        first_events = self.stream.drain_events()
        self.assertEqual(first_events[0]["type"], "page")
        self.assertEqual(first_events[0]["page_id"], first_page_id)
        self.assertIn("tab=first", first_events[0]["url"])

        tabs = self.host.runtime.tabs("chrome", self.session["id"], "new")
        second_page_id = tabs["active_page_id"]
        worker = self.browser.runtime._workers[("chrome", self.session["id"])]

        def navigate_second_page():
            live = worker.sessions[self.session["id"]]
            second = worker._page(live, second_page_id).page
            second.goto(f"{self.origin}/same-title?tab=second", wait_until="domcontentloaded", timeout=15_000)
            first_page = worker._page(live, first_page_id).page
            return first_page, second

        first_page, second_page = worker.call(navigate_second_page, timeout=20)
        page_metadata = worker.call(lambda: (
            first_page.title(), second_page.title(), first_page.url, second_page.url,
        ))
        self.assertEqual(page_metadata[0], page_metadata[1], "The tab titles intentionally collide")
        self.assertNotEqual(page_metadata[2], page_metadata[3])

        # A hostile page-world override must not make this hidden tab look
        # selected to the isolated-world probe.
        spoof = worker.call(lambda: first_page.evaluate("""() => {
            Object.defineProperty(Document.prototype, 'visibilityState', {
                configurable: true, get: () => 'visible'
            });
            Object.defineProperty(Document.prototype, 'hasFocus', {
                configurable: true, value: () => true
            });
            return {visibility: document.visibilityState, focused: document.hasFocus()};
        }"""), timeout=10)
        self.assertEqual(spoof, {"visibility": "visible", "focused": True})
        self.stream.pump()
        self.assertEqual(self.stream.page_id, second_page_id)

        # This is a native browser activation through the owned Playwright page;
        # it does not call BrowserHostRuntime.tabs/select.
        worker.call(first_page.bring_to_front, timeout=10)
        self.pump_until(self.stream, first_page_id)
        self.assertEqual(worker.sessions[self.session["id"]].primary_page_id, first_page_id)

        page_events = [item for item in self.stream.drain_events() if item.get("type") == "page"]
        self.assertTrue(page_events)
        self.assertEqual(page_events[-1]["page_id"], first_page_id)
        self.assertEqual(sum(item["selected"] for item in page_events[-1]["tabs"]), 1)

        # The ordinary capture consumer uses the synchronized primary page
        # when the caller does not supply page_id.
        capture = self.browser.runtime.capture("chrome", self.session["id"], "page")
        snapshot = capture["snapshot"]
        self.assertEqual(snapshot["page_id"], first_page_id)
        self.assertIn("tab=first", snapshot["url"])
        self.assertIn("Owned page marker: first", snapshot["text"])
        self.assertNotIn("Owned page marker: second", snapshot["text"])

        first_native = self.host.runtime.native_session("chrome", self.session["id"])
        second_native = self.host.runtime.native_session("chrome", self.session["id"], second_page_id)
        self.native_window_probe = {
            "active_page_id": first_native["page_id"],
            "active_hwnd": first_native["handle"],
            "active_pid": first_native["pid"],
            "active_window_id": first_native["window_id"],
            "active_association": first_native["association"],
            "second_page_id": second_native["page_id"],
            "second_hwnd": second_native["handle"],
            "second_window_id": second_native["window_id"],
            "second_association": second_native["association"],
        }
        type(self).last_native_window_probe = dict(self.native_window_probe)
        self.assertEqual(first_native["page_id"], first_page_id)
        self.assertEqual(second_native["page_id"], second_page_id)
        self.assertIsInstance(first_native["pid"], int)
        self.assertGreater(first_native["pid"], 0)
        self.assertTrue(first_native["native_window_available"], self.native_window_probe)
        self.assertEqual(first_native["association"], "unique_process_window", self.native_window_probe)
        self.assertEqual(second_native["pid"], first_native["pid"])
        if first_native["window_id"] == second_native["window_id"]:
            self.assertEqual(first_native["handle"], second_native["handle"], self.native_window_probe)
        else:
            self.assertNotEqual(first_native["handle"], second_native["handle"], self.native_window_probe)

    def test_second_owned_session_cannot_change_first_stream_or_native_target(self):
        first = self.navigate(
            self.session["id"], self.session["page_id"], f"{self.origin}/same-title?tab=first-session",
        )
        self.stream = self.host.runtime.start_stream("chrome", self.session["id"], first["page_id"], native=True)

        created = self.request("post", "/api/browser-sessions", data={})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        other_session = created.json["data"]
        other_session["page_id"] = self.host.runtime.tabs("chrome", other_session["id"], "list")["active_page_id"]
        other = self.navigate(
            other_session["id"], other_session["page_id"], f"{self.origin}/same-title?tab=second-session",
        )
        other_stream = self.host.runtime.start_stream("chrome", other_session["id"], other["page_id"], native=True)
        self.additional_streams.append(other_stream)
        other_worker = self.browser.runtime._workers[("chrome", other_session["id"])]
        other_worker.call(other_worker._page(other_worker.sessions[other_session["id"]], other["page_id"]).page.bring_to_front,
                          timeout=10)
        self.pump_until(other_stream, other["page_id"])
        self.stream.pump()

        first_worker = self.browser.runtime._workers[("chrome", self.session["id"])]
        self.assertEqual(self.stream.page_id, first["page_id"])
        self.assertEqual(first_worker.sessions[self.session["id"]].primary_page_id, first["page_id"])
        self.assertEqual(other_stream.page_id, other["page_id"])
        self.assertEqual(other_worker.sessions[other_session["id"]].primary_page_id, other["page_id"])

        first_native = self.host.runtime.native_session("chrome", self.session["id"])
        other_native = self.host.runtime.native_session("chrome", other_session["id"])
        self.assertEqual(first_native["page_id"], first["page_id"])
        self.assertEqual(other_native["page_id"], other["page_id"])
        self.assertNotEqual(first_native["page_id"], other_native["page_id"])


if __name__ == "__main__":
    unittest.main()
