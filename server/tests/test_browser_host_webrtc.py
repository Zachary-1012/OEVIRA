from __future__ import annotations

import asyncio
from collections import deque
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import math
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import wave
from urllib.parse import urlsplit

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from server.oevira import create_app
from server.oevira.acquisition import AcquisitionError
from server.oevira import browser_host_audio
from server.oevira.auth import authenticate, issue_local_capability, utc_now
from server.oevira.browser_host_webrtc import _Peer, close_webrtc_connection


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".runtime" / "browser-webrtc-tests"
BASE_URL = "http://127.0.0.1:8317"
FIXTURE_HOST = "webrtc-stability.fixture.test"


class WebRTCFixtureHandler(BaseHTTPRequestHandler):
    tone_wav = b""

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/dialog-navigation":
            body = b"""<!doctype html><meta charset=utf-8><title>Dialog destination</title>
<script>alert('The browser user must dismiss this dialog');</script>
<main><h1>Dialog destination</h1><p id='after-dialog'>Navigation resumed after dismissal.</p></main>"""
        elif parsed.path == "/tone.wav":
            body = self.tone_wav
            content_type = "audio/wav"
        else:
            body = """<!doctype html><meta charset=utf-8><title>WebRTC worker stability</title>
<style>
body{margin:0;font:18px sans-serif} #entry{position:absolute;left:20px;top:20px;width:380px;height:32px}
#play{position:absolute;left:20px;top:60px;width:160px;height:32px}
#drag{position:absolute;left:20px;top:100px;width:500px;height:38px;user-select:text}
#pulse{position:absolute;left:20px;top:170px;width:40px;height:24px;background:#345;animation:blink .2s steps(2,end) infinite alternate}
@keyframes blink{to{opacity:.15}}
</style>
<main><input id='entry'><audio id='tone' src='/tone.wav' loop></audio>
<button id='play' onclick="document.getElementById('tone').play()">Play isolated tone</button>
<p id='drag'>Drag this real source text across the browser page for selection evidence.</p>
<div id='pulse'></div><p id='status'>The source tab is live.</p></main>""".encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type if parsed.path != "/dialog-navigation" else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _EventEmitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback=None):
        if callback is None:
            return lambda handler: self.on(event, handler)
        self.listeners.setdefault(event, []).append(callback)
        return callback

    def emit(self, event, *args):
        for callback in list(self.listeners.get(event, [])):
            callback(*args)


class _BlockedPumpStream:
    def __init__(self):
        self.closed = False
        self.has_open_dialog = False
        self.events = deque()
        self.pump_started = threading.Event()
        self.release_pump = threading.Event()
        self.interrupt_started = threading.Event()
        self.close_called = False

    def initial_state(self):
        return {"page_id": "page-test", "page_revision": 1, "url": "https://fixture.test/"}

    def drain_events(self):
        result = list(self.events)
        self.events.clear()
        return result

    def pump(self):
        self.pump_started.set()
        self.release_pump.wait(5)

    def send_input(self, _event):
        return None

    def interrupt_dialog(self, event):
        self.interrupt_started.set()
        return {"type": "dialog", "dialog_id": event["dialog_id"], "status": "dismissed"}

    def close(self):
        self.closed = True
        self.close_called = True


class _FakeDataChannel(_EventEmitter):
    label = "input"
    ordered = True
    readyState = "open"
    bufferedAmount = 0

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))


class _FakeConnection(_EventEmitter):
    connectionState = "connected"
    iceConnectionState = "connected"
    signalingState = "stable"
    sctp = None

    def getTransceivers(self):
        return []

    async def close(self):
        self.connectionState = "closed"


class BrowserWebRTCLoopTests(unittest.TestCase):
    def test_event_loop_and_direct_dialog_interrupt_continue_while_pump_is_queued(self):
        async def exercise():
            stream = _BlockedPumpStream()
            connection = _FakeConnection()
            channel = _FakeDataChannel()
            closed = threading.Event()
            closures = []
            peer = _Peer(
                "host-fixture", connection, stream, lambda: True,
                lambda: closed.set(), video_track=None,
                on_closure=lambda host_id, detail: closures.append((host_id, detail)),
            )
            peer.task = asyncio.create_task(peer.run())
            connection.emit("datachannel", channel)
            self.assertTrue(await asyncio.to_thread(stream.pump_started.wait, 3))
            self.assertTrue(peer.pump_task is not None and not peer.pump_task.done())

            stream.events.append({"type": "page", "page_id": "page-test", "page_revision": 2})
            stream.has_open_dialog = True
            channel.emit("message", json.dumps({
                "sequence": 1,
                "type": "dialog",
                "page_id": "page-test",
                "page_revision": 2,
                "dialog_id": "dialog-fixture",
                "accept": False,
            }))
            self.assertTrue(await asyncio.to_thread(stream.interrupt_started.wait, 2),
                            "The dialog must bypass the queued pump/worker operation")
            await asyncio.sleep(0.05)
            self.assertTrue(any(item.get("type") == "page" for item in channel.sent))
            self.assertTrue(any(item.get("status") == "dismissed" for item in channel.sent))
            self.assertFalse(peer.closed)

            stream.release_pump.set()
            await peer.close(reason="test_complete")
            self.assertTrue(stream.close_called)
            self.assertTrue(closed.is_set())
            self.assertEqual(len(closures), 1)
            diagnostic = closures[0][1]
            self.assertEqual(diagnostic["cause"], "test_complete")
            self.assertNotIn("url", diagnostic)
            self.assertNotIn("text", diagnostic)
            self.assertNotIn("token", diagnostic)

        asyncio.run(exercise())


class BrowserWebRTCStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        cls.previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
        for key in cls.previous_temp:
            os.environ[key] = str(RUNTIME_ROOT)
        samples = array("h")
        sample_rate = 48_000
        for index in range(sample_rate * 6):
            value = int(0.15 * 32767 * math.sin(2 * math.pi * 440 * index / sample_rate))
            samples.extend((value, value))
        audio = io.BytesIO()
        with wave.open(audio, "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(samples.tobytes())
        WebRTCFixtureHandler.tone_wav = audio.getvalue()
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), WebRTCFixtureHandler)
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
            self.skipTest("The isolated Chrome WebRTC probe runs on Windows")
        self._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(RUNTIME_ROOT)
        self.temporary = tempfile.TemporaryDirectory(prefix="peer-", dir=RUNTIME_ROOT)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / "data", expected_port=8317)
        self.engine = "chrome"
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.host = self.app.extensions["oevira.browser_host"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        capability = self.browser.store.root / "launch.capability"
        issue_local_capability(self.browser.store, capability)
        local = self.client.post(
            "/api/local-session", base_url=BASE_URL,
            json={"capability": capability.read_text(encoding="ascii"), "display_name": "WebRTC stability test"},
        )
        self.assertEqual(local.status_code, 200, local.get_data(as_text=True))
        self.token = local.json["data"]["token"]
        created = self.request("post", "/api/browser-sessions", data={})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.session = created.json["data"]
        self.host_id = None

    def tearDown(self):
        if self.host_id is not None:
            self.host.close(self.host_id)
        self.browser.runtime.shutdown()
        self.temporary.cleanup()
        tempfile.tempdir = self._previous_tempdir

    def request(self, method: str, path: str, *, data=None, headers=None):
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
        raise AcquisitionError("BROWSER_TARGET_DENIED", "The isolated WebRTC fixture rejected this target.")

    def resolve_url(self, url):
        if urlsplit(url).netloc != urlsplit(self.origin).netloc:
            raise AcquisitionError("BROWSER_TARGET_DENIED", "The isolated WebRTC fixture rejected this target.")
        return url, FIXTURE_HOST, self.site.server_port, "127.0.0.1"

    def navigate_first_page(self):
        response = self.request(
            "post", f"/api/browser-sessions/{self.session['id']}/navigate", data={
                "base_version": self.session["version"],
                "page_revision": self.session["page_revision"],
                "url": f"{self.origin}/page",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.session = response.json["data"]
        return self.session

    def switch_to_headed_browser(self, engine="chrome"):
        """Reopen only this test's isolated data root with a real visible Chrome process."""
        self.browser.runtime.shutdown()
        self.app = create_app(self.root / "data", expected_port=8317)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=False)
        self.client = self.app.test_client()
        self.browser = self.app.extensions["oevira.browser"]
        self.host = self.app.extensions["oevira.browser_host"]
        self.browser.url_validator = self.allow_url
        self.browser.runtime.target_resolver = self.resolve_url
        self.engine = engine
        created = self.request("post", "/api/browser-sessions", data={"engine": engine})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.session = created.json["data"]

    async def _connect_peer(self, *, with_audio: bool = False):
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer.addTransceiver("video", direction="recvonly")
        audio_track_ready = asyncio.get_running_loop().create_future()
        if with_audio:
            peer.addTransceiver("audio", direction="recvonly")
        channel = peer.createDataChannel("input", ordered=True)
        received = asyncio.Queue()
        track_ready = asyncio.get_running_loop().create_future()
        state = {"page": None, "control_generation": None, "audio_events": []}
        connection_states = []

        @peer.on("track")
        def on_track(track):
            if track.kind == "audio":
                if not audio_track_ready.done():
                    audio_track_ready.set_result(track)
            elif not track_ready.done():
                track_ready.set_result(track)

        @peer.on("connectionstatechange")
        def on_connection_state_change():
            connection_states.append((time.monotonic(), peer.connectionState, peer.iceConnectionState))

        @channel.on("message")
        def on_message(data):
            try:
                event = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                event = {"type": "error", "code": "INVALID_TEST_EVENT"}
            if event.get("type") == "page":
                state["page"] = event
            elif event.get("type") == "control":
                state["control_generation"] = event.get("control_generation")
            elif event.get("type") == "audio":
                state["audio_events"].append(event)
            received.put_nowait(event)

        await peer.setLocalDescription(await peer.createOffer())
        response = await asyncio.to_thread(
            self.request,
            "post",
            f"/api/browser-sessions/{self.session['id']}/host/offer",
            data={"type": "offer", "sdp": peer.localDescription.sdp,
                  "page_id": self.session["page_id"], "viewport": {"width": 800, "height": 600}},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        answer = response.json["data"]
        self.host_id = answer["host_id"]
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))

        async def wait_for_event(kind: str, timeout: float = 10.0, *, fail_on_error: bool = True):
            async def next_matching():
                while True:
                    event = await received.get()
                    if event.get("type") == "error" and fail_on_error and kind != "error":
                        raise AssertionError(f"Peer reported {event!r} while waiting for {kind}")
                    if event.get("type") == kind:
                        return event
            return await asyncio.wait_for(next_matching(), timeout)

        first_page = await wait_for_event("page", timeout=15)
        track = await asyncio.wait_for(track_ready, 15)
        return peer, channel, received, state, connection_states, wait_for_event, track, first_page, audio_track_ready

    async def _stress_peer(self, *, stress_seconds: float, exercise_navigation_dialog: bool):
        peer, channel, messages, state, states, wait_for_event, track, page_event, _audio_ready = await self._connect_peer()
        frames = {"count": 0, "last_pts": None, "sizes": set(), "error": None}
        stop_frames = asyncio.Event()

        async def consume_video():
            try:
                while not stop_frames.is_set():
                    frame = await track.recv()
                    frames["count"] += 1
                    frames["last_pts"] = frame.pts
                    frames["sizes"].add((frame.width, frame.height))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                frames["error"] = type(exc).__name__

        video_task = asyncio.create_task(consume_video())
        sequence = 0

        def send(kind: str, **fields):
            nonlocal sequence
            current = state["page"]
            self.assertIsNotNone(current, "An active page event is required before input")
            sequence += 1
            event = {
                "sequence": sequence,
                "page_id": current["page_id"],
                "page_revision": current["page_revision"],
                "type": kind,
                **fields,
            }
            if state["control_generation"] is not None:
                event["control_generation"] = state["control_generation"]
            channel.send(json.dumps(event, separators=(",", ":")))
            return sequence

        try:
            first_frame_deadline = time.monotonic() + 10
            while frames["count"] < 3 and time.monotonic() < first_frame_deadline:
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(frames["count"], 3, "The peer must receive actual Chromium video frames")
            self.assertEqual(frames["sizes"], {(800, 600)})

            send("pointer", phase="down", x=80, y=35, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=80, y=35, button="left", buttons=0, click_count=1, modifiers=0)
            send("text", text="steady peer input")
            send("input_barrier")
            barrier = await wait_for_event("input_ack", timeout=10)
            self.assertEqual(barrier["page_id"], state["page"]["page_id"])
            server_peer = self.host._webrtc._peers[self.host_id]
            worker = self.browser.runtime._workers[(self.engine, self.session["id"])]
            entered = await asyncio.to_thread(
                worker.call,
                lambda: worker._page(worker.sessions[self.session["id"]], self.session["page_id"]).page.locator("#entry").input_value(),
            )
            input_errors = [item for item in list(messages._queue) if item.get("type") == "error"]
            self.assertEqual(entered, "steady peer input", {"input_errors": input_errors, "page_state": state["page"],
                                                             "control_generation": state["control_generation"],
                                                             "peer_input_messages": server_peer.input_messages,
                                                             "peer_input_dispatched": server_peer.input_dispatched,
                                                             "peer_input_rejected": server_peer.input_rejected,
                                                             "peer_last_error_code": server_peer.last_error_code,
                                                             "pending_inputs": server_peer.inputs.qsize() if hasattr(server_peer.inputs, "qsize") else len(server_peer.inputs)})

            send("pointer", phase="down", x=25, y=115, button="left", buttons=1, click_count=1, modifiers=0)
            for x in (75, 130, 190, 255):
                send("pointer", phase="move", x=x, y=115, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=270, y=115, button="left", buttons=0, click_count=1, modifiers=0)
            send("input_barrier")
            await wait_for_event("input_ack", timeout=10)
            selection = await asyncio.to_thread(
                worker.call,
                lambda: worker._page(worker.sessions[self.session["id"]], self.session["page_id"]).page.evaluate("() => window.getSelection().toString()"),
            )
            self.assertTrue(selection, "A real pointer drag should select page text")

            stress_end = time.monotonic() + stress_seconds
            next_text = time.monotonic()
            move_index = 0
            unique_frames = set()
            host_stream = self.host.views[self.host_id].stream
            while time.monotonic() < stress_end:
                if peer.connectionState != "connected" or peer.iceConnectionState not in {"connected", "completed"}:
                    break
                move_index = (move_index + 1) % 250
                send("pointer", phase="move", x=20 + move_index, y=220, button="none", buttons=0, click_count=1, modifiers=0)
                if time.monotonic() >= next_text:
                    send("text", text=".")
                    next_text = time.monotonic() + 2.0
                latest = host_stream.latest_frame()
                if latest:
                    unique_frames.add(hashlib.sha256(latest).hexdigest())
                await asyncio.sleep(0.25)
            self.assertEqual(peer.connectionState, "connected", f"peer states: {states!r}")
            self.assertIn(peer.iceConnectionState, {"connected", "completed"}, f"peer states: {states!r}")
            minimum_frames = max(30, int((stress_seconds + 1) * 8))
            self.assertGreater(frames["count"], minimum_frames, f"frames={frames['count']}, states={states!r}")
            self.assertGreater(len(unique_frames), 1, "The receiver should observe changing real Chromium frames")
            self.assertFalse(
                [item for item in list(messages._queue) if item.get("type") == "error"],
                f"Peer input/frame errors occurred during stress: {[item for item in list(messages._queue) if item.get('type') == 'error']!r}",
            )

            if exercise_navigation_dialog:
                # Drain all earlier real pointer/text events before the external
                # API navigation opens its modal, preserving the input sequence.
                send("input_barrier")
                await wait_for_event("input_ack", timeout=10)
                # Trigger a native JavaScript dialog through the existing BrowserService
                # navigation route while the peer remains live. Its reply must use the
                # fixed interrupt path, since the owner thread is inside page.goto.
                current_session = await asyncio.to_thread(self.request, "get", f"/api/browser-sessions/{self.session['id']}")
                self.assertEqual(current_session.status_code, 200, current_session.get_data(as_text=True))
                session_state = current_session.json["data"]
                navigation_task = asyncio.create_task(asyncio.to_thread(
                    self.request,
                    "post",
                    f"/api/browser-sessions/{self.session['id']}/navigate",
                    data={
                        "base_version": session_state["version"],
                        "page_revision": session_state["page_revision"],
                        "page_id": state["page"]["page_id"],
                        "url": f"{self.origin}/dialog-navigation",
                    },
                ))
                dialog = await wait_for_event("dialog", timeout=15)
                frame_count_at_dialog = frames["count"]
                await asyncio.sleep(0.4)
                self.assertGreater(frames["count"], frame_count_at_dialog, "Video delivery should continue while a native dialog waits")
                active_page = state["page"]
                sequence += 1
                reply = {
                    "sequence": sequence,
                    "page_id": dialog["page_id"],
                    "page_revision": dialog.get("page_revision", active_page["page_revision"]),
                    "type": "dialog",
                    "dialog_id": dialog["dialog_id"],
                    "accept": False,
                }
                if state["control_generation"] is not None:
                    reply["control_generation"] = state["control_generation"]
                channel.send(json.dumps(reply, separators=(",", ":")))
                resolved = await wait_for_event("dialog", timeout=10)
                self.assertEqual(resolved.get("status"), "dismissed", resolved)
                navigation = await asyncio.wait_for(navigation_task, 10)
                self.assertEqual(navigation.status_code, 200, navigation.get_data(as_text=True))
                self.assertEqual(peer.connectionState, "connected", f"peer states: {states!r}")
                self.assertIn(peer.iceConnectionState, {"connected", "completed"}, f"peer states: {states!r}")

            stats = await peer.getStats()
            inbound_video = [item for item in stats.values() if getattr(item, "type", None) == "inbound-rtp"
                             and getattr(item, "kind", None) == "video"]
            self.assertTrue(inbound_video, f"The peer should report an inbound video track; reports={list(stats.values())!r}")
            self.assertNotIn("BROWSER_INPUT_BACKPRESSURE", [item.get("code") for item in list(messages._queue) if item.get("type") == "error"])
        finally:
            stop_frames.set()
            video_task.cancel()
            await asyncio.gather(video_task, return_exceptions=True)
            await close_webrtc_connection(peer)
            self.host.close(self.host_id)

    async def _verify_reconnect_and_auth(self):
        async def wait_peer_closed(host_id: str, timeout: float = 10.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                server = self.host._webrtc
                if server is None or host_id not in server._peers:
                    return
                await asyncio.sleep(0.05)
            self.fail(f"WebRTC host {host_id} did not close within the bounded wait")

        first_peer, _channel, _messages, _state, _states, _wait, first_track, _page, _audio_ready = await self._connect_peer()
        first_frame = await asyncio.wait_for(first_track.recv(), 10)
        self.assertEqual((first_frame.width, first_frame.height), (800, 600))
        first_host_id = self.host_id
        self.host.close(first_host_id)
        await close_webrtc_connection(first_peer)
        await wait_peer_closed(first_host_id)
        closed_state = self.host._webrtc.last_closure
        self.assertEqual(closed_state["host_id"], first_host_id)
        self.assertIn(closed_state["cause"], {"host_detach", "authorization_revoked", "connection_closed"})
        self.assertEqual(closed_state["pending_inputs"], 0)
        self.assertFalse(closed_state["input_task_pending"])
        self.assertFalse(closed_state["pump_task_pending"])
        self.assertNotIn("url", closed_state)
        self.assertNotIn("text", closed_state)

        reopened_peer, _channel, _messages, _state, _states, reopened_wait, reopened_track, _page, _audio_ready = await self._connect_peer()
        reopened_host_id = self.host_id
        reopened_frame = await asyncio.wait_for(reopened_track.recv(), 10)
        self.assertEqual((reopened_frame.width, reopened_frame.height), (800, 600))
        self.assertEqual(reopened_peer.connectionState, "connected")

        auth = authenticate(self.browser.store, self.token)
        with self.browser.store.transaction() as connection:
            connection.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (utc_now(), auth["session_id"]))
        failure = await reopened_wait("error", timeout=15, fail_on_error=False)
        self.assertEqual(failure["code"], "BROWSER_REAUTH_REQUIRED")
        await wait_peer_closed(reopened_host_id)
        auth_closed_state = self.host._webrtc.last_closure
        self.assertEqual(auth_closed_state["host_id"], reopened_host_id)
        self.assertEqual(auth_closed_state["pending_inputs"], 0)
        self.assertFalse(auth_closed_state["input_task_pending"])
        self.assertFalse(auth_closed_state["pump_task_pending"])
        await close_webrtc_connection(reopened_peer)

    async def _exercise_process_loopback_audio(self):
        play_clicked = threading.Event()
        raw_capture = {"mono_samples": [], "silence_samples": [], "helper_pid": None}
        raw_capture_lock = threading.Lock()
        original_audio_factory = browser_host_audio.browser_audio_track

        def record_owned_process_audio(stream):
            track = original_audio_factory(stream)
            if track is None:
                return None
            raw_capture["track"] = track
            original_recv = track.recv

            async def capture_raw_frame():
                frame = await original_recv()
                values = array("h")
                values.frombytes(bytes(frame.planes[0])[:frame.samples * 2 * 2])
                if play_clicked.is_set():
                    with raw_capture_lock:
                        if len(raw_capture["mono_samples"]) < 144_000:
                            raw_capture["mono_samples"].extend(values[0::2][:max(0, 144_000 - len(raw_capture["mono_samples"]))])
                        if track.process is not None:
                            raw_capture["helper_pid"] = track.process.pid
                elif len(raw_capture["silence_samples"]) < 9_600:
                    raw_capture["silence_samples"].extend(values[0::2][:9_600 - len(raw_capture["silence_samples"])])
                return frame

            track.recv = capture_raw_frame
            return track

        with patch("server.oevira.browser_host_audio.browser_audio_track", side_effect=record_owned_process_audio):
            peer, channel, messages, state, states, wait_for_event, video_track, _page, audio_ready = await self._connect_peer(with_audio=True)
        video_frames = {"count": 0, "error": None}
        audio_frames = {
            "count": 0, "nonzero": False, "error": None, "mono_samples": [], "pts": [],
            "silence_samples": [],
            "samples_per_frame": [], "sample_rates": [],
        }
        first_audio = asyncio.Event()
        enough_audio = asyncio.Event()
        silence_ready = asyncio.Event()
        stop = asyncio.Event()

        async def consume_video():
            try:
                while not stop.is_set():
                    await video_track.recv()
                    video_frames["count"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                video_frames["error"] = type(exc).__name__

        audio_track = await asyncio.wait_for(audio_ready, 10)
        worker = self.browser.runtime._workers[(self.engine, self.session["id"])]

        def owned_browser_details():
            cdp = worker.browser.new_browser_cdp_session()
            try:
                try:
                    arguments = cdp.send("Browser.getBrowserCommandLine").get("arguments", [])
                    mute_audio_flag_present = any(
                        isinstance(argument, str) and argument.lower().split("=", 1)[0] == "--mute-audio"
                        for argument in arguments
                    )
                    commandline_status = "read"
                except Exception:
                    mute_audio_flag_present = None
                    commandline_status = "unavailable"
                process_info = cdp.send("SystemInfo.getProcessInfo").get("processInfo", [])
                browser_pids = [item.get("id") for item in process_info if item.get("type") == "browser"]
                process_type_counts = {}
                for item in process_info:
                    kind = item.get("type")
                    if isinstance(kind, str):
                        process_type_counts[kind] = process_type_counts.get(kind, 0) + 1
                return {
                    "commandline_status": commandline_status,
                    "mute_audio_flag_present": mute_audio_flag_present,
                    "browser_pids": browser_pids,
                    "process_type_counts": process_type_counts,
                }
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass

        owned_details = await asyncio.to_thread(worker.call, owned_browser_details)
        if owned_details["mute_audio_flag_present"] is not None:
            self.assertFalse(owned_details["mute_audio_flag_present"], owned_details)
        self.assertEqual(len(owned_details["browser_pids"]), 1, owned_details)

        async def consume_audio():
            try:
                while not stop.is_set():
                    frame = await audio_track.recv()
                    self.assertEqual(frame.format.name, "s16")
                    self.assertEqual(len(frame.layout.channels), 2)
                    # FFmpeg planes include allocation padding beyond valid PCM.
                    # Counting that padding can finish a three-second probe before
                    # Chrome has produced even one second of actual audio.
                    samples = bytes(frame.planes[0])[:frame.samples * 2 * 2]
                    audio_frames["count"] += 1
                    audio_frames["pts"].append(frame.pts)
                    audio_frames["samples_per_frame"].append(frame.samples)
                    audio_frames["sample_rates"].append(frame.sample_rate)
                    if play_clicked.is_set():
                        decoded = array("h")
                        decoded.frombytes(samples)
                        if len(audio_frames["mono_samples"]) < 144_000:
                            audio_frames["mono_samples"].extend(
                                decoded[0::2][:max(0, 144_000 - len(audio_frames["mono_samples"]))]
                            )
                    elif len(audio_frames["silence_samples"]) < 9_600:
                        decoded = array("h")
                        decoded.frombytes(samples)
                        audio_frames["silence_samples"].extend(decoded[0::2][:9_600 - len(audio_frames["silence_samples"])])
                        if len(audio_frames["silence_samples"]) >= 9_600:
                            silence_ready.set()
                    if play_clicked.is_set() and any(samples):
                        audio_frames["nonzero"] = True
                        first_audio.set()
                    if (len(audio_frames["mono_samples"]) >= 144_000
                            and len(raw_capture["mono_samples"]) >= 144_000):
                        enough_audio.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                audio_frames["error"] = type(exc).__name__

        video_task = asyncio.create_task(consume_video())
        audio_task = asyncio.create_task(consume_audio())
        sequence = 0

        def send(kind: str, **fields):
            nonlocal sequence
            current = state["page"]
            sequence += 1
            event = {
                "sequence": sequence,
                "page_id": current["page_id"],
                "page_revision": current["page_revision"],
                "type": kind,
                **fields,
            }
            if state["control_generation"] is not None:
                event["control_generation"] = state["control_generation"]
            channel.send(json.dumps(event, separators=(",", ":")))

        try:
            video_deadline = time.monotonic() + 10
            while video_frames["count"] < 2 and time.monotonic() < video_deadline:
                await asyncio.sleep(0.05)
            self.assertGreater(video_frames["count"], 0, "Video should remain a real live track with audio negotiated")
            await asyncio.wait_for(silence_ready.wait(), 10)

            send("pointer", phase="down", x=80, y=75, button="left", buttons=1, click_count=1, modifiers=0)
            send("pointer", phase="up", x=80, y=75, button="left", buttons=0, click_count=1, modifiers=0)
            send("input_barrier")
            await wait_for_event("input_ack", timeout=10)
            play_clicked.set()
            worker = self.browser.runtime._workers[(self.engine, self.session["id"])]
            audio_playback = await asyncio.to_thread(
                worker.call,
                lambda: worker._page(worker.sessions[self.session["id"]], self.session["page_id"]).page.locator("#tone").evaluate(
                    "element => ({paused: element.paused, currentTime: element.currentTime, readyState: element.readyState, "
                    "networkState: element.networkState, muted: element.muted, volume: element.volume, duration: element.duration})"
                ),
            )
            await asyncio.wait_for(first_audio.wait(), 10)
            try:
                await asyncio.wait_for(enough_audio.wait(), 10)
            except asyncio.TimeoutError as exc:
                helper_track = raw_capture.get("track")
                reader = getattr(helper_track, "_reader_task", None)
                raise AssertionError({
                    "audio_frames": audio_frames["count"], "audio_error": audio_frames.get("error"),
                    "decoded_samples": len(audio_frames["mono_samples"]),
                    "raw_samples": len(raw_capture["mono_samples"]),
                    "helper_samples": getattr(helper_track, "samples", None),
                    "captured_samples": getattr(helper_track, "_captured_samples", None),
                    "reader_done": reader.done() if reader is not None else None,
                    "reader_error": type(reader.exception()).__name__ if reader is not None and reader.done() and not reader.cancelled() else None,
                    "audio_events": state["audio_events"],
                }) from exc
            self.assertGreater(audio_frames["count"], 0)
            self.assertTrue(audio_frames["nonzero"], "The receiver should get PCM captured from the owned Chrome process")
            self.assertTrue(all(value == 960 for value in audio_frames["samples_per_frame"]))
            self.assertTrue(all(value == 48_000 for value in audio_frames["sample_rates"]))
            window = 9_600
            samples = audio_frames["mono_samples"]
            windows = [samples[start:start + window] for start in range(0, len(samples) - window + 1, window // 2)]
            def window_stats(part):
                rms_value = math.sqrt(sum(sample * sample for sample in part) / max(1, len(part)))
                crossings = sum((left < 0 <= right) or (left >= 0 > right) for left, right in zip(part, part[1:]))
                frequency = crossings * 48_000 / (2 * max(1, len(part) - 1))
                return rms_value, frequency

            measured_windows = [window_stats(part) for part in windows]
            rms, measured_frequency = max(measured_windows, default=(0.0, 0.0), key=lambda item: item[0])
            raw_samples = raw_capture["mono_samples"][:144_000]
            raw_rms = math.sqrt(sum(sample * sample for sample in raw_samples) / max(1, len(raw_samples)))
            raw_windows = [raw_samples[start:start + window] for start in range(0, len(raw_samples) - window + 1, window // 2)]
            raw_measured_windows = [window_stats(part) for part in raw_windows]
            raw_best_rms, raw_frequency = max(raw_measured_windows, default=(0.0, 0.0), key=lambda item: item[0])
            helper_track = raw_capture.get("track")
            helper_process = getattr(helper_track, "process", None)
            helper_stats = {
                "helper_samples": getattr(helper_track, "samples", 0),
                "helper_ready_state": getattr(helper_track, "readyState", None),
                "helper_returncode": getattr(helper_process, "returncode", None),
                "diagnostic_drain_done": bool(getattr(getattr(helper_track, "diagnostics", None), "done", lambda: True)()),
                "dropped_capture_frames": getattr(helper_track, "dropped_frames", 0),
            }
            raw_noise = math.sqrt(sum(value * value for value in raw_capture["silence_samples"]) / len(raw_capture["silence_samples"]))
            decoded_noise = math.sqrt(sum(value * value for value in audio_frames["silence_samples"]) / len(audio_frames["silence_samples"]))
            # Device/session gain can scale a correct tone below an arbitrary
            # PCM amplitude. Require 40 dB above independently observed silence,
            # the known source frequency, and preservation of captured level.
            self.assertGreater(raw_best_rms, 100 * max(1, raw_noise))
            self.assertGreater(rms, 100 * max(1, decoded_noise), {
                "decoded_audio_rms": rms,
                "raw_helper_audio_rms": raw_rms,
                "raw_helper_best_window_rms": raw_best_rms,
                "raw_helper_frequency_hz": raw_frequency,
                "helper_process_pid": raw_capture["helper_pid"],
                **helper_stats,
                "audio_playback": audio_playback,
                "sample_count": len(samples),
                "raw_sample_count": len(raw_samples),
                "first_samples": samples[:32],
            })
            self.assertAlmostEqual(rms / raw_best_rms, 1, delta=0.2)
            self.assertAlmostEqual(measured_frequency, 440, delta=30, msg={
                "crossing_estimate_hz": measured_frequency,
                "audio_rms": rms,
                "raw_helper_audio_rms": raw_rms,
                "raw_helper_frequency_hz": raw_frequency,
                "audio_playback": audio_playback,
                "sample_count": len(samples),
                "first_samples": samples[:32],
            })
            self.assertFalse(audio_playback["paused"], audio_playback)
            self.assertGreater(audio_playback["currentTime"], 0, audio_playback)
            helper_command = getattr(self, "audio_helper_command", [])
            self.assertTrue(helper_command, "The process-loopback helper should have started for this isolated Chrome")
            self.assertIn("--pid", helper_command)
            self.assertEqual(helper_command[helper_command.index("--pid") + 1], str(owned_details["browser_pids"][0]))
            type(self).last_audio_process_probe = {
                **owned_details,
                "helper_target_pid": helper_command[helper_command.index("--pid") + 1],
                "helper_process_started": True,
            }
            self.assertEqual(audio_frames["pts"], sorted(audio_frames["pts"]))
            self.assertGreater(audio_frames["pts"][-1] - audio_frames["pts"][0], 4_800)
            self.assertEqual(peer.connectionState, "connected", f"peer states: {states!r}")
            self.assertIn(peer.iceConnectionState, {"connected", "completed"}, f"peer states: {states!r}")
            audio_events = state["audio_events"]
            self.assertTrue(any(item.get("available") is True for item in audio_events), audio_events)
        finally:
            stop.set()
            for task in (video_task, audio_task):
                task.cancel()
            await asyncio.gather(video_task, audio_task, return_exceptions=True)
            await close_webrtc_connection(peer)
            self.host.close(self.host_id)

    def test_audio_track_captures_played_test_tone_from_owned_chrome_process(self):
        self._verify_process_audio("chrome")

    def test_audio_track_captures_played_test_tone_from_owned_edge_process(self):
        self._verify_process_audio("edge")

    def _verify_process_audio(self, engine):
        helper_paths = [
            Path(os.sys.executable).parent / "oevira_browser_audio.exe",
            ROOT / "app" / "build" / "windows" / "x64" / "runner" / "Release" / "oevira_browser_audio.exe",
        ]
        if not any(path.is_file() for path in helper_paths):
            self.skipTest("The isolated process-loopback helper is not packaged in this workstation build")
        self.switch_to_headed_browser(engine)
        self.navigate_first_page()
        helper_command = []
        actual_create_process = asyncio.create_subprocess_exec

        async def record_owned_audio_helper(*command, **kwargs):
            if command and Path(str(command[0])).name.lower() == "oevira_browser_audio.exe":
                helper_command.extend(str(item) for item in command)
            return await actual_create_process(*command, **kwargs)

        self.audio_helper_command = helper_command
        with patch("asyncio.create_subprocess_exec", side_effect=record_owned_audio_helper):
            asyncio.run(self._exercise_process_loopback_audio())

    def test_live_peer_stays_connected_during_bounded_input_stress(self):
        self.navigate_first_page()
        asyncio.run(self._stress_peer(stress_seconds=45.0, exercise_navigation_dialog=False))

    def test_external_navigation_dialog_uses_peer_interrupt_path(self):
        self.navigate_first_page()
        asyncio.run(self._stress_peer(stress_seconds=2.0, exercise_navigation_dialog=True))


if __name__ == "__main__":
    unittest.main()
