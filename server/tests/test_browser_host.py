from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from urllib.parse import urlsplit

from aiortc import RTCConfiguration, RTCPeerConnection

from server.oevira import create_app
from server.oevira.auth import issue_local_capability, issue_session
from server.oevira.browser import BrowserError
from server.oevira.browser_host_webrtc import close_webrtc_connection
from server.oevira.acquisition import AcquisitionError


ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8317"
DOCUMENT = b"An independently retrieved browser download.\n"


class BrowserSite(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/document.txt":
            body = DOCUMENT
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", 'attachment; filename="source.txt"')
        else:
            body = """<!doctype html><meta charset=utf-8><title>Live browser input</title>
<style>body{margin:0;font:20px sans-serif}input{position:absolute;left:20px;top:20px;width:380px;height:32px}
a{position:absolute;left:20px;top:100px}p{position:absolute;left:20px;top:180px}</style>
<input id=entry><a id=download href=/document.txt>Download source</a>
<p>独立的原文证据。Original source context.</p>
<button id=dialog style="position:absolute;left:20px;top:270px" onclick="window.dialogResult=confirm('Continue this isolated test?')">Confirm</button>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class BrowserHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), BrowserSite)
        cls.site_thread = threading.Thread(target=cls.site.serve_forever, daemon=True)
        cls.site_thread.start()
        cls.origin = f"http://host-fixture.test:{cls.site.server_port}"
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
        self.directory = tempfile.TemporaryDirectory(prefix="host-", dir=self.test_root)
        self.app = create_app(self.directory.name, expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.host = self.app.extensions["oevira.browser_host"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        capability = Path(self.directory.name) / "launch.capability"
        issue_local_capability(self.browser.store, capability)
        response = self.request("post", "/api/local-session", {"capability": capability.read_text(), "display_name": "隔离宿主验收"}, auth=False)
        self.token = response.json["data"]["token"]
        response = self.request("post", "/api/browser-sessions", {})
        self.assertEqual(response.status_code, 201, response.json)
        self.session = response.json["data"]
        self.assertEqual(self.session["engine"], "chrome")

    def tearDown(self):
        self.browser.runtime.shutdown()
        self.directory.cleanup()

    def request(self, method, path, data=None, *, auth=True, key=None):
        headers = {"Idempotency-Key": key or uuid.uuid4().hex}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
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
            raise AcquisitionError("BROWSER_TARGET_DENIED", "The target is outside this isolated fixture.")
        return url, "host-fixture.test", self.site.server_port, "127.0.0.1"

    def test_one_page_has_one_tab_and_tab_changes_checkpoint(self):
        path = f"/api/browser-sessions/{self.session['id']}/host/tabs"
        tabs = self.request("get", path).json["data"]
        self.assertEqual(len(tabs["tabs"]), 1)
        first = tabs["active_page_id"]
        created = self.request("post", path, {"action": "new", "base_version": self.session["version"]})
        self.assertEqual(created.status_code, 200, created.json)
        result = created.json["data"]
        self.assertEqual(len(result["tabs"]), 2)
        self.assertNotEqual(first, result["active_page_id"])
        closed = self.request("post", path, {"action": "close", "page_id": result["active_page_id"], "base_version": result["version"]})
        self.assertEqual(closed.status_code, 200, closed.json)
        self.assertEqual(closed.json["data"]["active_page_id"], first)

    def test_real_webrtc_input_download_capture_and_revocation(self):
        response = self.request("post", f"/api/browser-sessions/{self.session['id']}/navigate", {
            "base_version": self.session["version"], "page_revision": self.session["page_revision"], "url": self.origin,
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.session = response.json["data"]
        asyncio.run(self._exercise_stream())

    async def _exercise_stream(self):
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer.addTransceiver("video", direction="recvonly")
        channel = peer.createDataChannel("input", ordered=True)
        events = asyncio.Queue()
        track_ready = asyncio.Future()

        @peer.on("track")
        def track_received(track):
            if not track_ready.done():
                track_ready.set_result(track)

        @channel.on("message")
        def received(data):
            events.put_nowait(json.loads(data))

        async def event_of(kind):
            async def wait():
                while True:
                    event = await events.get()
                    if event.get("type") == kind:
                        return event
            return await asyncio.wait_for(wait(), 15)

        await peer.setLocalDescription(await peer.createOffer())
        prefix = f"/api/browser-sessions/{self.session['id']}"
        response = await asyncio.to_thread(self.request, "post", prefix + "/host/offer", {
            "type": "offer", "sdp": peer.localDescription.sdp,
            "page_id": self.session["page_id"], "viewport": {"width": 800, "height": 600},
        })
        self.assertEqual(response.status_code, 200, response.json)
        answer = response.json["data"]
        attachment = await asyncio.to_thread(self.request, "get", prefix + "/host")
        self.assertEqual(attachment.status_code, 200, attachment.json)
        self.assertEqual(attachment.json["data"], {"attached": True, "host_id": answer["host_id"], "transport": "webrtc", "state": "active"})
        owner = self.browser.store.row("SELECT owner_user_id, workspace_id FROM browser_sessions WHERE id=?", (self.session["id"],))
        other_token, _ = issue_session(self.browser.store, owner["owner_user_id"], owner["workspace_id"])
        hidden = self.client.get(prefix + "/host", base_url=BASE, headers={"Authorization": "Bearer " + other_token})
        self.assertEqual(hidden.status_code, 409, hidden.json)
        self.assertNotIn(answer["host_id"], hidden.get_data(as_text=True))
        try:
            from aiortc import RTCSessionDescription
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
            state = await event_of("page")
            track = await asyncio.wait_for(track_ready, 10)
            frame = await asyncio.wait_for(track.recv(), 15)
            self.assertEqual((frame.width, frame.height), (800, 600))
            sequence = 0

            def send(kind, **values):
                nonlocal sequence
                sequence += 1
                channel.send(json.dumps({"sequence": sequence, "page_id": state["page_id"],
                                         "page_revision": state["page_revision"], "type": kind, **values}))

            send("pointer", phase="down", x=70, y=35, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=70, y=35, button="left", buttons=0, click_count=1, modifiers=0)
            send("text", text="中文输入 한국어 입력")
            send("input_barrier")
            acknowledgement = await event_of("input_ack")
            self.assertEqual(acknowledgement["sequence"], sequence)
            worker = self.browser.runtime._workers[("chrome", self.session["id"])]
            entered = await asyncio.to_thread(worker.call, lambda: worker._page(worker.sessions[self.session['id']]).page.locator("#entry").input_value())
            self.assertEqual(entered, "中文输入 한국어 입력")
            send("pointer", phase="down", x=50, y=280, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=50, y=280, button="left", buttons=0, click_count=1, modifiers=0)
            dialog = await event_of("dialog")
            send("dialog", dialog_id=dialog["dialog_id"], accept=False)
            resolved = await event_of("dialog")
            self.assertEqual(resolved["status"], "dismissed")
            send("pointer", phase="down", x=70, y=112, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=70, y=112, button="left", buttons=0, click_count=1, modifiers=0)
            download = await event_of("download")
            download_id = download["download_id"]
            path = prefix + f"/host/{answer['host_id']}/downloads/{download_id}"
            file_response = await asyncio.to_thread(self.request, "get", path)
            self.assertEqual(file_response.status_code, 200, file_response.get_data())
            self.assertEqual(file_response.get_data(), DOCUMENT)
            file_response.close()
            saved = await asyncio.to_thread(self.request, "post", path + "/save", {})
            self.assertEqual(saved.status_code, 201, saved.json)
            material = saved.json["data"]
            self.assertIn("independently retrieved", material["text"])
            reopened = await asyncio.to_thread(self.request, "get", "/api/materials/" + material["id"])
            self.assertEqual(reopened.json["data"]["sha256"], material["sha256"])
            self.assertEqual(reopened.json["data"]["source"]["kind"], "controlled_browser_download")
            # Invalidated authority must stop the already established channel too.
            with self.browser.store.transaction() as connection:
                connection.execute("UPDATE sessions SET revoked_at='2026-01-01T00:00:00+00:00'")
            failure = await event_of("error")
            self.assertEqual(failure["code"], "BROWSER_REAUTH_REQUIRED")
        finally:
            receipt = await close_webrtc_connection(peer)
            self.assertTrue(receipt["closed"])
            self.assertEqual(peer.iceConnectionState, "closed")
            self.host.close(answer["host_id"])


if __name__ == "__main__":
    unittest.main()
