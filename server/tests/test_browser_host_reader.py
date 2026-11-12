from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import os
import tempfile
import threading
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

from server.oevira import create_app
from server.oevira.auth import authenticate, issue_local_capability, issue_session, utc_now
from server.oevira.browser import BrowserError
from server.oevira.browser_host_reader import (
    _READABLE_CONTEXT_SCRIPT,
    _safe_browser_url,
    _safe_display_text,
    capture_reader_context,
    register_browser_reader_routes,
)
from server.oevira.acquisition import AcquisitionError


ROOT = Path(__file__).resolve().parents[2]
API_BASE = "http://127.0.0.1:8325"
MAIN_HOST = "reader-main.fixture.test"
FRAME_HOST = "reader-frame.fixture.test"
MAIN_TOKEN = "MAIN_READER_SECRET_7d1f"
FRAME_TOKEN = "FRAME_READER_SECRET_2a9c"
LINK_TOKEN = "LINK_READER_SECRET_6b80"
PASSWORD_VALUE = "READER_PASSWORD_SENTINEL_308a"
COOKIE_VALUE = "READER_COOKIE_SENTINEL_118d"
QUOTE = "📚 e\u0301vidence"


class ReaderFixtureHandler(BaseHTTPRequestHandler):
    frame_origin = ""

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/frame":
            token = query.get("token", [""])[0]
            html = f"""<!doctype html><meta charset="utf-8"><title>Frame evidence</title>
<main><p id="frame-quote">Frame before {QUOTE} frame after. {token}</p>
<a id="frame-link" href="/reference?token={LINK_TOKEN}" title="Reference {LINK_TOKEN}">Reference {LINK_TOKEN}</a>
<input type="password" value="{PASSWORD_VALUE}"></main>"""
            body = html.encode("utf-8")
        elif parsed.path == "/hidden":
            body = b"<!doctype html><title>Hidden frame title</title><p>Hidden frame content</p>"
        elif parsed.path == "/other":
            body = b"<!doctype html><meta charset=utf-8><title>Other page</title><main><p>Fresh page content.</p></main>"
        else:
            token = query.get("token", [""])[0]
            html = f"""<!doctype html><meta charset="utf-8"><title>Main {token}</title>
<style>body{{font:18px sans-serif}} iframe{{width:540px;height:130px;border:1px solid}} #hidden{{display:none}}</style>
<main aria-label="Reader fixture"><h1>Local source page</h1>
<p id="main-quote">Main before {QUOTE} main after. {token}</p>
<a id="main-link" href="/reference?token={LINK_TOKEN}" title="Citation {LINK_TOKEN}">Citation {LINK_TOKEN}</a>
<form><label>Protected field <input type="password" value="{PASSWORD_VALUE}"></label></form>
<input type="text" value="{PASSWORD_VALUE}">
<iframe id="visible-frame" src="{self.frame_origin}/frame?token={FRAME_TOKEN}"></iframe>
<iframe id="hidden" src="{self.frame_origin}/hidden"></iframe></main>"""
            body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"reader_fixture={COOKIE_VALUE}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class BrowserHostReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_root = ROOT / ".runtime" / "browser-reader" / "tests"
        cls.test_root.mkdir(parents=True, exist_ok=True)
        cls.previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
        for key in cls.previous_temp:
            os.environ[key] = str(cls.test_root)

        cls.main_site = ThreadingHTTPServer(("127.0.0.1", 0), ReaderFixtureHandler)
        cls.frame_site = ThreadingHTTPServer(("127.0.0.1", 0), ReaderFixtureHandler)
        cls.main_origin = f"http://{MAIN_HOST}:{cls.main_site.server_port}"
        cls.frame_origin = f"http://{FRAME_HOST}:{cls.frame_site.server_port}"
        ReaderFixtureHandler.frame_origin = cls.frame_origin
        cls.main_thread = threading.Thread(target=cls.main_site.serve_forever, daemon=True)
        cls.frame_thread = threading.Thread(target=cls.frame_site.serve_forever, daemon=True)
        cls.main_thread.start()
        cls.frame_thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.main_site, cls.frame_site):
            server.shutdown()
            server.server_close()
        cls.main_thread.join(5)
        cls.frame_thread.join(5)
        for key, value in cls.previous_temp.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="reader-", dir=self.test_root)
        self.app = create_app(self.directory.name, expected_port=8325)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.host = self.app.extensions["oevira.browser_host"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        register_browser_reader_routes(self.app)

        capability = Path(self.directory.name) / "launch.capability"
        issue_local_capability(self.browser.store, capability)
        response = self.request(
            "post", "/api/local-session",
            {"capability": capability.read_text(encoding="ascii"), "display_name": "隔离 Reader 测试"},
            auth=False,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        response = self.request("post", "/api/browser-sessions", {})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.session = response.json["data"]

    def tearDown(self):
        self.browser.runtime.shutdown()
        self.directory.cleanup()

    def request(self, method, path, data=None, *, auth=True):
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        kwargs = {"base_url": API_BASE, "headers": headers}
        if data is not None:
            kwargs["json"] = data
        return getattr(self.client, method)(path, **kwargs)

    def allow_url(self, url):
        parsed = urlsplit(url)
        allowed = {MAIN_HOST: self.main_site.server_port, FRAME_HOST: self.frame_site.server_port}
        if parsed.hostname in allowed and parsed.port == allowed[parsed.hostname]:
            return url
        raise BrowserError("BROWSER_TARGET_DENIED", 403)

    def resolve_url(self, url):
        parsed = urlsplit(url)
        allowed = {MAIN_HOST: self.main_site.server_port, FRAME_HOST: self.frame_site.server_port}
        port = allowed.get(parsed.hostname)
        if port is None or parsed.port != port:
            raise AcquisitionError("BROWSER_TARGET_DENIED", "The target is outside this isolated Reader fixture.")
        return url, parsed.hostname, port, "127.0.0.1"

    def navigate(self, url=None):
        url = url or f"{self.main_origin}/?token={MAIN_TOKEN}"
        response = self.request("post", f"/api/browser-sessions/{self.session['id']}/navigate", {
            "base_version": self.session["version"],
            "page_revision": self.session["page_revision"],
            "url": url,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.session = response.json["data"]
        return self.session

    def set_selection(self, target: str):
        script = """(element, quote) => {
            const node = element.firstChild;
            const text = node && node.nodeValue ? node.nodeValue : '';
            const start = text.indexOf(quote);
            if (start < 0) throw new Error('quote not found');
            const range = document.createRange();
            range.setStart(node, start);
            range.setEnd(node, start + quote.length);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }"""

        def select(worker, live):
            page = worker._page(live, self.session["page_id"]).page
            if target == "main":
                page.locator("#main-quote").evaluate(script, QUOTE)
                return page.evaluate("() => window.getSelection().toString()")
            else:
                frame = next(frame for frame in page.frames if FRAME_HOST in frame.url)
                frame.locator("#frame-quote").wait_for(timeout=10_000)
                frame.locator("#frame-quote").evaluate(script, QUOTE)
                return frame.evaluate("() => window.getSelection().toString()")

        selected = self.host.runtime._call("chrome", self.session["id"], select, timeout=15)
        self.assertEqual(selected, QUOTE)

    def test_reader_accepts_current_page_after_manual_navigation_not_yet_checkpointed(self):
        self.navigate()
        worker = self.browser.runtime._workers[("chrome", self.session["id"])]
        cached = self.browser.store.row("SELECT page_revision FROM browser_sessions WHERE id=?", (self.session["id"],))["page_revision"]
        current_url = self.main_origin + "/manual-navigation"

        def manual_navigation():
            live = worker.sessions[self.session["id"]]
            page = worker._page(live)
            page.page.goto(current_url, wait_until="domcontentloaded")
            return worker._snapshot(live, page.page_id, include_text=False)

        observed = worker.call(manual_navigation)
        self.assertGreater(observed["page_revision"], cached)
        response = self.request("get", f"/api/browser-sessions/{self.session['id']}/context?page_id={observed['page_id']}&page_revision={observed['page_revision']}")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["data"]["url"], current_url)
        self.assertEqual(response.json["data"]["page_revision"], observed["page_revision"])
        self.assertTrue(response.json["data"]["frames"])
        saved = self.browser.store.row("SELECT page_revision,current_url FROM browser_sessions WHERE id=?", (self.session["id"],))
        self.assertEqual((saved["page_revision"], saved["current_url"]), (observed["page_revision"], current_url))

    def test_reader_returns_frame_attributed_unicode_quotes_and_safe_urls(self):
        self.navigate()
        self.set_selection("main")
        def read_selection(worker, live):
            page = worker._page(live, self.session["page_id"]).page
            return page.locator("body").evaluate(_READABLE_CONTEXT_SCRIPT, {
                "max_nodes": 4096,
                "max_characters": 198_002,
                "max_blocks": 1000,
                "max_block_characters": 2000,
                "max_links": 200,
                "max_link_characters": 10_000,
                "max_controls": 200,
                "max_control_characters": 10_000,
                "max_control_label_nodes": 512,
                "max_selection_characters": 10_000,
            })

        direct = self.host.runtime._call("chrome", self.session["id"], read_selection, timeout=15)
        self.assertEqual(direct["selected_text"], QUOTE, direct)
        path = f"/api/browser-sessions/{self.session['id']}/context?page_id={self.session['page_id']}"
        before = self.browser.store.row("SELECT COUNT(*) AS total FROM materials")["total"]
        response = self.request("get", path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.json["data"]
        self.set_selection("frame")
        frame_response = self.request("get", path)
        self.assertEqual(frame_response.status_code, 200, frame_response.get_data(as_text=True))
        frame_result = frame_response.json["data"]
        after = self.browser.store.row("SELECT COUNT(*) AS total FROM materials")["total"]
        self.assertEqual(after, before, "A Reader request must not create a Material automatically.")

        self.assertEqual(result["scope"], "main_frame_and_visible_iframes")
        self.assertEqual(result["offset_unit"], "unicode_codepoints")
        self.assertEqual(result["offset_basis"], "sanitized_reader_frame_text")
        self.assertEqual(result["snapshot_version"]["page_revision"], result["page_revision"])
        self.assertEqual(len(result["snapshot_version"]["sha256"]), 64)
        self.assertEqual(result["coverage"]["status"], "partial", "A hidden iframe must be represented as uncovered.")
        password_controls = [control for control in result["controls"] if control["type"] == "password"]
        self.assertTrue(password_controls)
        self.assertTrue(any("Protected field" in control["label"] for control in password_controls))
        for control in password_controls:
            self.assertNotIn("value", control)
            self.assertTrue(control["locator"].startswith("body/"))
            self.assertIn("bounds", control)

        frames = {frame["frame_id"]: frame for frame in result["frames"]}
        main = frames["main"]
        visible = next(
            (frame for frame in result["frames"] if frame["status"] in {"read", "partial"} and frame["frame_id"] != "main"),
            None,
        )
        self.assertIsNotNone(visible, result["frames"])
        hidden = next(frame for frame in result["frames"] if frame["status"] == "not_visible")
        self.assertTrue(visible["source_url"].startswith(self.frame_origin))
        self.assertIn("token", visible["redacted_query_keys"])
        self.assertEqual(parse_qs(urlsplit(visible["source_url"]).query)["token"], ["[redacted]"])
        self.assertEqual(hidden["title"], "")
        self.assertEqual(hidden["coverage"]["scope"], "visible_frames_only")

        for frame in (main, visible):
            for block in frame["blocks"]:
                start, end = block["start_offset"], block["end_offset"]
                self.assertEqual(frame["text"][start:end], block["text"])
                self.assertGreaterEqual(start, 0)
                self.assertGreater(end, start)
        main_selection = main["selection"]
        self.assertEqual(main_selection["location_state"], "unique_reader_snapshot_match")
        self.assertEqual(main_selection["quote"], QUOTE)
        self.assertEqual(main["text"][main_selection["start_offset"]:main_selection["end_offset"]], QUOTE)
        self.assertIn("before", main_selection["context_before"])
        self.assertIn("after", main_selection["context_after"])
        selected_frame = next(frame for frame in frame_result["frames"] if frame["frame_id"] == visible["frame_id"])
        frame_selection = selected_frame["selection"]
        self.assertEqual(frame_selection["location_state"], "unique_reader_snapshot_match")
        self.assertEqual(frame_selection["quote"], QUOTE)
        self.assertEqual(selected_frame["text"][frame_selection["start_offset"]:frame_selection["end_offset"]], QUOTE)
        self.assertIn("before", frame_selection["context_before"])
        self.assertIn("after", frame_selection["context_after"])

        frame_roles = {block["role"] for block in visible["blocks"]}
        self.assertIn("main", frame_roles)
        safe_links = result["links"]
        self.assertTrue(any(
            "[redacted]" in parse_qs(urlsplit(link["url"]).query).get("token", [])
            for link in safe_links
        ))
        response_text = response.get_data(as_text=True) + frame_response.get_data(as_text=True)
        for secret in (MAIN_TOKEN, FRAME_TOKEN, LINK_TOKEN, PASSWORD_VALUE, COOKIE_VALUE):
            self.assertNotIn(secret, response_text)

        unsupported = self.request("get", path + "&script=window.alert(1)")
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.json["error"]["code"], "INVALID_INPUT")

    def test_reader_checks_identity_revision_and_closed_page(self):
        self.navigate()
        path = f"/api/browser-sessions/{self.session['id']}/context"
        unauthenticated = self.app.test_client().get(path, base_url=API_BASE)
        self.assertEqual(unauthenticated.status_code, 401)

        identity = authenticate(self.browser.store, self.token)
        other_user_id = str(uuid.uuid4())
        with self.browser.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)",
                (other_user_id, "Reader fixture viewer", utc_now()),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'viewer', 1, ?)",
                (identity["workspace"]["id"], other_user_id, utc_now()),
            )
        other_token, _ = issue_session(self.browser.store, other_user_id, identity["workspace"]["id"])
        wrong_owner = self.client.get(
            path,
            base_url=API_BASE,
            headers={"Authorization": "Bearer " + other_token},
        )
        self.assertEqual(wrong_owner.status_code, 404)
        self.assertEqual(wrong_owner.json["error"]["code"], "BROWSER_SESSION_NOT_FOUND")

        stale = self.request("get", path + f"?page_revision={self.session['page_revision'] - 1}")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "BROWSER_PAGE_CHANGED")

        old_revision = self.session["page_revision"]
        previous_version = self.session["version"]
        navigation = self.request("post", f"/api/browser-sessions/{self.session['id']}/navigate", {
            "base_version": previous_version,
            "page_revision": old_revision,
            "url": f"{self.main_origin}/other",
        })
        self.assertEqual(navigation.status_code, 200, navigation.get_data(as_text=True))
        self.session = navigation.json["data"]
        changed = self.request("get", path + f"?page_revision={old_revision}")
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json["error"]["code"], "BROWSER_PAGE_CHANGED")

        closed = self.request("post", f"/api/browser-sessions/{self.session['id']}/close", {
            "base_version": self.session["version"],
        })
        self.assertEqual(closed.status_code, 200, closed.get_data(as_text=True))
        after_close = self.request("get", path)
        self.assertEqual(after_close.status_code, 409)
        self.assertEqual(after_close.json["error"]["code"], "BROWSER_SESSION_NOT_OPEN")

    def test_main_frame_only_read_reports_unrequested_frames(self):
        self.navigate()
        path = f"/api/browser-sessions/{self.session['id']}/context?include_frames=false"
        response = self.request("get", path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.json["data"]
        self.assertEqual(result["scope"], "main_frame_only")
        self.assertEqual(len(result["frames"]), 1)
        self.assertGreater(result["coverage"]["frames_not_requested"], 0)
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_reader_block_saves_as_existing_material_without_changing_parent_address(self):
        self.navigate()
        prefix = f"/api/browser-sessions/{self.session['id']}"
        response = self.request("get", prefix + "/context")
        self.assertEqual(response.status_code, 200, response.json)
        reader = response.json["data"]
        frame = next(item for item in reader["frames"] if item["frame_id"] != "main" and item["status"] == "read")
        block = next(item for item in frame["blocks"] if QUOTE in item["text"])

        # Layout movement must not turn an unchanged quote into a different source.
        self.host.runtime._call("chrome", self.session["id"], lambda worker, live:
            worker._page(live).page.locator("#visible-frame").evaluate("element => element.style.marginLeft='17px'"))
        current = self.request("get", prefix).json["data"]
        saved = self.request("post", prefix + "/capture", {
            "base_version": current["version"], "page_revision": reader["page_revision"],
            "page_id": reader["page_id"], "scope": "reader_block", "frame_id": frame["frame_id"],
            "frame_sha256": frame["frame_sha256"], "block_id": block["block_id"],
        })
        self.assertEqual(saved.status_code, 201, saved.json)
        material = saved.json["data"]["material"]
        self.assertEqual(material["text"], block["text"])
        self.assertEqual(material["source"]["source_url"], frame["source_url"])
        self.assertEqual(material["source"]["scope"], "reader_block")
        self.assertEqual(saved.json["data"]["browser"]["url"], reader["url"])
        reopened = self.request("get", "/api/materials/" + material["id"])
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json["data"]["text"], block["text"])
        self.assertEqual(reopened.json["data"]["source"]["selection_context"]["offset_unit"], "unicode_codepoints")
        for secret in (MAIN_TOKEN, FRAME_TOKEN, PASSWORD_VALUE, COOKIE_VALUE):
            self.assertNotIn(secret, reopened.get_data(as_text=True))

    def test_capture_reader_block_revalidates_hash_and_uses_live_text(self):
        self.navigate()
        response = self.request("get", f"/api/browser-sessions/{self.session['id']}/context")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        reader = response.json["data"]
        frame = next(item for item in reader["frames"] if item["frame_id"] != "main" and item["status"] == "read")
        block = next(item for item in frame["blocks"] if QUOTE in item["text"])

        frame_capture = capture_reader_context(
            self.host.runtime,
            "chrome",
            self.session["id"],
            self.session["page_id"],
            frame["frame_id"],
            frame["frame_sha256"],
            expected_page_revision=reader["page_revision"],
        )
        self.assertEqual(frame_capture["snapshot"]["scope"], "reader_frame")
        self.assertEqual(frame_capture["snapshot"]["text"], frame["text"])
        self.assertEqual(frame_capture["snapshot"]["source_url"], frame["source_url"])

        captured = capture_reader_context(
            self.host.runtime,
            "chrome",
            self.session["id"],
            self.session["page_id"],
            frame["frame_id"],
            frame["frame_sha256"],
            block_id=block["block_id"],
            expected_page_revision=reader["page_revision"],
        )
        snapshot = captured["snapshot"]
        self.assertIsInstance(captured["storage_payload"], dict)
        self.assertEqual(snapshot["scope"], "reader_block")
        self.assertEqual(snapshot["text"], block["text"])
        self.assertEqual(snapshot["url"], reader["url"])
        self.assertEqual(snapshot["title"], reader["title"])
        self.assertEqual(snapshot["source_url"], frame["source_url"])
        self.assertEqual(snapshot["source_title"], frame["source_title"])
        self.assertEqual(snapshot["text_sha256"], hashlib.sha256(block["text"].encode("utf-8")).hexdigest())
        self.assertEqual(snapshot["selection_context"]["location_state"], "reader_block_match")
        self.assertEqual(
            frame["text"][snapshot["selection_context"]["start_offset"]:snapshot["selection_context"]["end_offset"]],
            block["text"],
        )

        def change_frame(worker, live):
            page = worker._page(live, self.session["page_id"]).page
            child = next(frame for frame in page.frames if FRAME_HOST in frame.url)
            child.locator("#frame-quote").evaluate("(element, text) => { element.textContent = text; }", "Changed iframe evidence")

        self.host.runtime._call("chrome", self.session["id"], change_frame, timeout=10)
        with self.assertRaises(BrowserError) as changed:
            capture_reader_context(
                self.host.runtime,
                "chrome",
                self.session["id"],
                self.session["page_id"],
                frame["frame_id"],
                frame["frame_sha256"],
                block_id=block["block_id"],
                expected_page_revision=reader["page_revision"],
            )
        self.assertEqual(changed.exception.code, "BROWSER_PAGE_CHANGED")
        self.assertIn("current_frame_sha256", changed.exception.details)


class BrowserReaderRedactionTests(unittest.TestCase):
    def test_sensitive_path_and_query_values_are_removed_from_urls_and_text(self):
        path_secret = "READER_PATH_SECRET_3a4c"
        path_url = f"https://fixture.example/token/{path_secret}"
        safe_path, path_keys, path_state = _safe_browser_url(path_url)
        self.assertNotIn(path_secret, safe_path)
        self.assertIn("token", path_keys)
        self.assertEqual(path_state, "redacted")
        safe_path_text = _safe_display_text(f"Page echoed {path_secret} here", path_url, 200)
        self.assertNotIn(path_secret, safe_path_text)

        query_secret = "READER_QUERY_SECRET_59be"
        query_url = f"https://fixture.example/page?access_token={query_secret}"
        safe_query, query_keys, _ = _safe_browser_url(query_url)
        self.assertNotIn(query_secret, safe_query)
        self.assertIn("access_token", query_keys)
        safe_query_text = _safe_display_text(f"Page echoed {query_secret} here", query_url, 200)
        self.assertNotIn(query_secret, safe_query_text)


if __name__ == "__main__":
    unittest.main()
