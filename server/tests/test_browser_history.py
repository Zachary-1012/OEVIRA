from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from server.oevira import create_app
from server.oevira.acquisition import AcquisitionError
from server.oevira.auth import (
    authenticate,
    issue_local_capability,
    issue_session,
    utc_now,
)
from server.oevira.browser import BrowserError
from server.oevira.browser_history import (
    MAX_HISTORY_ITEMS,
    BrowserHistoryService,
    install_browser_history_schema,
    register_browser_history_routes,
)

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8319"
BODY_A = "Only live page metadata belongs in browser history."


class BrowserHistorySite(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        title = "History fixture B" if path.endswith("/b") else "History fixture A"
        body = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>{title}</title><p>{BODY_A}</p>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class BrowserHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), BrowserHistorySite)
        cls.site_thread = threading.Thread(target=cls.site.serve_forever, daemon=True)
        cls.site_thread.start()
        cls.origin = f"http://history-fixture.test:{cls.site.server_port}"
        cls.test_root = ROOT / ".runtime" / "browser-host" / "tests"
        cls.test_root.mkdir(parents=True, exist_ok=True)
        cls.previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
        for key in cls.previous_temp:
            os.environ[key] = str(cls.test_root)

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()
        cls.site_thread.join(5)
        for key, value in cls.previous_temp.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="history-", dir=self.test_root)
        self.app = create_app(self.directory.name, expected_port=8319)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        register_browser_history_routes(self.app)
        self.history: BrowserHistoryService = self.app.extensions["oevira.browser_history"]

        capability = Path(self.directory.name) / "launch.capability"
        issue_local_capability(self.browser.store, capability)
        response = self.request(
            "post",
            "/api/local-session",
            {"capability": capability.read_text(encoding="utf-8"), "display_name": "History Owner"},
            auth=False,
        )
        self.token_a = response.json["data"]["token"]
        self.identity_a = authenticate(self.browser.store, self.token_a)
        self.workspace_id = self.identity_a["workspace"]["id"]

        self.user_b = uuid.uuid4().hex
        membership_created_at = utc_now()
        with self.browser.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)",
                (self.user_b, "History Teammate", membership_created_at),
            )
            connection.execute(
                """INSERT INTO memberships(workspace_id, user_id, role, active, created_at)
                   VALUES(?, ?, 'editor', 1, ?)""",
                (self.workspace_id, self.user_b, membership_created_at),
            )
        self.token_b, _ = issue_session(
            self.browser.store, self.user_b, self.workspace_id
        )
        self.identity_b = authenticate(self.browser.store, self.token_b)

        self.session_a = self.create_browser_session(self.token_a)
        self.session_b = self.create_browser_session(self.token_b)

    def tearDown(self):
        self.browser.runtime.shutdown()
        self.directory.cleanup()

    def request(self, method, path, data=None, *, token=None, auth=True):
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        if auth:
            headers["Authorization"] = "Bearer " + (token or self.token_a)
        kwargs = {"base_url": BASE, "headers": headers}
        if data is not None:
            kwargs["json"] = data
        return getattr(self.client, method)(path, **kwargs)

    def allow_url(self, url):
        if urlsplit(url).netloc == urlsplit(self.origin).netloc:
            return url
        raise BrowserError("BROWSER_TARGET_DENIED", 403)

    def resolve_url(self, url):
        if urlsplit(url).netloc != urlsplit(self.origin).netloc:
            raise AcquisitionError(
                "BROWSER_TARGET_DENIED", "The target is outside this isolated fixture."
            )
        return url, "history-fixture.test", self.site.server_port, "127.0.0.1"

    def create_browser_session(self, token):
        response = self.request("post", "/api/browser-sessions", {}, token=token)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["data"]

    def navigate(self, token, session, path, secret):
        url = f"{self.origin}/{path}?access_token={secret}"
        response = self.request(
            "post",
            f"/api/browser-sessions/{session['id']}/navigate",
            {
                "base_version": session["version"],
                "page_revision": session["page_revision"],
                "url": url,
            },
            token=token,
        )
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["data"]

    def record_navigation(self, identity, session, snapshot):
        return self.history.record_visit(identity, session["id"], snapshot)

    def test_safe_visits_dedupe_per_user_and_never_store_source_body(self):
        page_a = self.navigate(
            self.token_a, self.session_a, "a", "secret-A-not-for-storage"
        )
        page_b = self.navigate(
            self.token_b, self.session_b, "a", "secret-B-not-for-storage"
        )

        first_a = self.record_navigation(self.identity_a, self.session_a, page_a)
        second_a = self.record_navigation(self.identity_a, self.session_a, page_a)
        self.record_navigation(self.identity_b, self.session_b, page_b)

        self.assertEqual(first_a["item"]["id"], second_a["item"]["id"])
        self.assertTrue(second_a["deduplicated"])
        self.assertEqual(first_a["item"]["url"], page_a["url"])
        self.assertEqual(first_a["item"]["redacted_query_keys"], ["access_token"])
        self.assertEqual(first_a["item"]["page_id"], page_a["page_id"])
        self.assertEqual(first_a["item"]["session_id"], self.session_a["id"])

        history_a = self.request("get", "/api/browser-history", token=self.token_a)
        history_b = self.request("get", "/api/browser-history", token=self.token_b)
        self.assertEqual(history_a.status_code, 200, history_a.json)
        self.assertEqual(history_b.status_code, 200, history_b.json)
        items_a = history_a.json["data"]["items"]
        items_b = history_b.json["data"]["items"]
        self.assertEqual(len(items_a), 1)
        self.assertEqual(len(items_b), 1)
        self.assertNotEqual(items_a[0]["id"], items_b[0]["id"])
        serialized = json.dumps(history_a.json, ensure_ascii=False)
        self.assertNotIn("secret-A-not-for-storage", serialized)
        self.assertNotIn(BODY_A, serialized)
        self.assertNotIn("text", items_a[0])

        self.history.record_visit(
            self.identity_a,
            self.session_a["id"],
            {"page_id": "page_blank", "url": "about:blank", "title": "Blank"},
        )
        install_browser_history_schema(self.browser.store)
        install_browser_history_schema(self.browser.store)
        after_install = self.request("get", "/api/browser-history", token=self.token_a)
        self.assertEqual(after_install.json["data"]["items"][0]["id"], items_a[0]["id"])

    def test_bookmark_uses_current_page_deduplicates_and_deletes_with_cas(self):
        page = self.navigate(
            self.token_a, self.session_a, "b", "bookmark-secret-not-for-storage"
        )
        path = f"/api/browser-sessions/{self.session_a['id']}/bookmark"
        first = self.request(
            "post", path, {"page_id": page["page_id"]}, token=self.token_a
        )
        self.assertEqual(first.status_code, 201, first.json)
        bookmark = first.json["data"]["item"]
        duplicate = self.request(
            "post", path,
            {"page_id": page["page_id"], "title": "A different uncommitted title"},
            token=self.token_a,
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.json)
        self.assertTrue(duplicate.json["data"]["deduplicated"])
        self.assertEqual(duplicate.json["data"]["item"]["id"], bookmark["id"])
        self.assertEqual(duplicate.json["data"]["item"]["title"], bookmark["title"])

        list_a = self.request("get", "/api/browser-bookmarks", token=self.token_a)
        list_b = self.request("get", "/api/browser-bookmarks", token=self.token_b)
        self.assertEqual(len(list_a.json["data"]["items"]), 1)
        self.assertEqual(list_b.json["data"]["items"], [])
        self.assertNotIn("bookmark-secret-not-for-storage", json.dumps(list_a.json))

        stale = self.request(
            "delete", f"/api/browser-bookmarks/{bookmark['id']}",
            {"base_version": bookmark["version"] + 1}, token=self.token_a,
        )
        self.assertEqual(stale.status_code, 409, stale.json)
        forbidden_owner = self.request(
            "delete", f"/api/browser-bookmarks/{bookmark['id']}",
            {"base_version": bookmark["version"]}, token=self.token_b,
        )
        self.assertEqual(forbidden_owner.status_code, 404)
        removed = self.request(
            "delete", f"/api/browser-bookmarks/{bookmark['id']}",
            {"base_version": bookmark["version"]}, token=self.token_a,
        )
        self.assertEqual(removed.status_code, 200, removed.json)
        self.assertEqual(self.request("get", "/api/browser-bookmarks", token=self.token_a).json["data"]["items"], [])

    def test_clear_history_is_actor_exact_and_session_close_preserves_bookmarks(self):
        page_a = self.navigate(self.token_a, self.session_a, "a", "clear-secret-A")
        page_b = self.navigate(self.token_b, self.session_b, "b", "clear-secret-B")
        self.record_navigation(self.identity_a, self.session_a, page_a)
        self.record_navigation(self.identity_b, self.session_b, page_b)
        created = self.request(
            "post",
            f"/api/browser-sessions/{self.session_a['id']}/bookmark",
            {"page_id": page_a["page_id"]},
            token=self.token_a,
        )
        self.assertEqual(created.status_code, 201, created.json)

        current_b = self.request(
            "get", f"/api/browser-sessions/{self.session_b['id']}", token=self.token_b
        )
        self.assertEqual(current_b.status_code, 200, current_b.json)
        closed_b = self.request(
            "post",
            f"/api/browser-sessions/{self.session_b['id']}/close",
            {"base_version": current_b.json["data"]["version"]},
            token=self.token_b,
        )
        self.assertEqual(closed_b.status_code, 200, closed_b.json)
        self.assertEqual(
            len(self.request("get", "/api/browser-history", token=self.token_b).json["data"]["items"]),
            1,
        )

        clear = self.request("post", "/api/browser-history/clear", {}, token=self.token_a)
        self.assertEqual(clear.status_code, 200, clear.json)
        self.assertEqual(
            self.request("get", "/api/browser-history", token=self.token_a).json["data"]["items"],
            [],
        )
        self.assertEqual(
            len(self.request("get", "/api/browser-history", token=self.token_b).json["data"]["items"]),
            1,
        )
        bookmarks_a = self.request("get", "/api/browser-bookmarks", token=self.token_a)
        self.assertEqual(len(bookmarks_a.json["data"]["items"]), 1)

        current = self.request(
            "get", f"/api/browser-sessions/{self.session_a['id']}", token=self.token_a
        )
        self.assertEqual(current.status_code, 200, current.json)
        close = self.request(
            "post",
            f"/api/browser-sessions/{self.session_a['id']}/close",
            {"base_version": current.json["data"]["version"]},
            token=self.token_a,
        )
        self.assertEqual(close.status_code, 200, close.json)
        persisted = self.request("get", "/api/browser-bookmarks", token=self.token_a)
        self.assertEqual(len(persisted.json["data"]["items"]), 1)

    def test_history_keeps_the_latest_1000_and_concurrent_duplicate_visits_are_stable(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        workspace_id = self.identity_a["workspace"]["id"]
        user_id = self.identity_a["user"]["id"]
        with self.browser.store.transaction() as connection:
            for index in range(MAX_HISTORY_ITEMS + 1):
                connection.execute(
                    """INSERT INTO browser_history(
                           id, workspace_id, user_id, url, title, redacted_query_keys_json,
                           visited_at, page_id, browser_session_id
                       ) VALUES(?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
                    (
                        f"seed-{index}",
                        workspace_id,
                        user_id,
                        f"https://history-fixture.test/seed/{index}",
                        f"Seed {index}",
                        (base + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
                        f"page_seed_{index}",
                        self.session_a["id"],
                    ),
                )

        page = self.navigate(self.token_a, self.session_a, "a", "concurrent-secret")
        self.history.record_visit(self.identity_a, self.session_a["id"], page)

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(
                workers.map(
                    lambda _: self.history.record_visit(
                        self.identity_a, self.session_a["id"], page
                    ),
                    range(16),
                )
            )
        same_url = [item for item in results if item["item"]["url"] == page["url"]]
        self.assertEqual(len({item["item"]["id"] for item in same_url}), 1)
        items = self.request("get", "/api/browser-history?limit=1000", token=self.token_a).json["data"]["items"]
        self.assertEqual(len(items), MAX_HISTORY_ITEMS)
        self.assertEqual(items[0]["url"], page["url"])
        self.assertNotIn("https://history-fixture.test/seed/0", {item["url"] for item in items})
        self.assertNotIn("https://history-fixture.test/seed/1", {item["url"] for item in items})


if __name__ == "__main__":
    unittest.main()
