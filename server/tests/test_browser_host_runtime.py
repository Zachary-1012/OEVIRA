from __future__ import annotations

import base64
from dataclasses import dataclass
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from server.oevira.browser import BrowserError, browser_safe_url
from server.oevira.browser_host_runtime import BrowserHostRuntime, _match_window_bounds


class FakeEventTarget:
    def __init__(self):
        self._listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback) -> None:
        listeners = self._listeners.get(event, [])
        if callback in listeners:
            listeners.remove(callback)

    def emit(self, event: str, payload=None) -> None:
        for callback in list(self._listeners.get(event, [])):
            callback(payload)


class FakePage(FakeEventTarget):
    def __init__(self, url: str = "about:blank", title: str = "New tab"):
        super().__init__()
        self.url = url
        self._title = title
        self.main_frame = object()
        self.closed = False
        self.fronted = False
        self.wait_count = 0
        self.clipboard = "fixture clipboard"
        self.frames = [self]
        self.viewport = {"width": 1280, "height": 800}
        self.context = None
        self.main_frame_id = "frame-" + str(id(self))
        self.native_visibility = "visible"
        self.native_has_focus = True
        self.main_world_visibility_override = None
        self.main_world_focus_override = None

    def title(self) -> str:
        return self._title

    def is_closed(self) -> bool:
        return self.closed

    def bring_to_front(self) -> None:
        self.fronted = True
        if self.context is not None:
            for page in self.context.pages:
                page.native_visibility = "hidden"
                page.native_has_focus = False
            self.native_visibility = "visible"
            self.native_has_focus = True

    def wait_for_timeout(self, timeout: int) -> None:
        self.wait_count += 1
        self.last_wait_ms = timeout

    def close(self) -> None:
        self.closed = True
        self.emit("close")

    def evaluate(self, expression: str, argument=None):
        if "window.getSelection()" in expression and "navigator.clipboard" not in expression:
            return self.clipboard
        raise AssertionError("The remote OS clipboard must not be accessed.")


class FakeCdpSession(FakeEventTarget):
    def __init__(self, page=None):
        super().__init__()
        self.page = page
        self.commands: list[tuple[str, dict]] = []
        self.detached = False

    def send(self, method: str, params: dict | None = None):
        params = params or {}
        self.commands.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": self.page.main_frame_id}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 1}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": {
                "visibility": self.page.native_visibility,
                "focused": self.page.native_has_focus,
            }}}
        return {}

    def detach(self) -> None:
        self.detached = True


class FakeContext(FakeEventTarget):
    def __init__(self):
        super().__init__()
        self.pages: list[FakePage] = []
        self.cdp_sessions: dict[int, FakeCdpSession] = {}
        self.grants: list[tuple[list[str], str | None]] = []
        self.new_page_count = 0

    def new_page(self):
        self.new_page_count += 1
        page = FakePage()
        page.context = self
        self.pages.append(page)
        self.emit("page", page)
        return page

    def new_cdp_session(self, page):
        session = FakeCdpSession(page)
        self.cdp_sessions[id(page)] = session
        return session

    def grant_permissions(self, permissions, *, origin=None):
        self.grants.append((list(permissions), origin))


@dataclass
class FakePageState:
    page_id: str
    page: FakePage
    revision: int = 0
    last_text_hash: str | None = None


class FakeWorker:
    def __init__(self):
        self.thread = SimpleNamespace(is_alive=lambda: True)
        self.sessions = {}
        self.live = True
        self.call_count = 0
        self.call_thread_ids: list[int] = []
        self.browser = SimpleNamespace(version="fixture-chromium")

    def runtime_is_live(self, _session_id: str) -> bool:
        return self.live

    def call(self, callback, timeout=45):
        self.call_count += 1
        self.call_thread_ids.append(threading.get_ident())
        return callback()

    def register_session(self, session_id: str, page: FakePage) -> FakePageState:
        context = FakeContext()
        page.context = context
        context.pages.append(page)
        live = SimpleNamespace(
            session_id=session_id,
            workspace_id="workspace-fixture",
            owner_user_id="user-fixture",
            engine="chrome",
            context=context,
            pages={},
            primary_page_id=None,
            blocked_requests=0,
            restore_url=None,
        )
        self.sessions[session_id] = live
        return self._register_page(live, page, page_id="page_fixture")

    def _register_page(self, live, page, *, page_id: str | None = None):
        requested_page_id = page_id
        page_id = page_id or "page_" + str(len(live.pages) + 1)
        state = FakePageState(page_id, page)
        live.pages[page_id] = state
        if live.primary_page_id is None or requested_page_id is None:
            live.primary_page_id = page_id

        def navigated(frame):
            if frame == page.main_frame:
                state.revision += 1

        page.on("framenavigated", navigated)
        page.on("close", lambda *_args: self._page_closed(live, page_id))
        return state

    @staticmethod
    def _page_closed(live, page_id):
        live.pages.pop(page_id, None)
        if live.primary_page_id == page_id:
            live.primary_page_id = next(iter(live.pages), None)

    def _snapshot(self, live, page_id=None, *, include_text=True):
        identifier = page_id or live.primary_page_id
        state = live.pages[identifier]
        url, redacted = browser_safe_url(state.page.url or "about:blank")
        title = state.page.title()[:500]
        return {
            "page_id": identifier,
            "url": url,
            "title": title,
            "browser_version": self.browser.version,
            "page_revision": state.revision,
            "text": "" if not include_text else "fixture page",
            "text_sha256": None,
            "text_characters": 0,
            "text_truncated": False,
            "blocked_request_count": live.blocked_requests,
            "redacted_query_keys": redacted,
        }


class FakeManager:
    def __init__(self, worker: FakeWorker):
        self._workers = {("chrome", "session_fixture"): worker}

    def is_stopping(self, _session_id):
        return False


class FakeFileChooser:
    def __init__(self, multiple=False):
        self.multiple = multiple
        self.files = None

    def is_multiple(self):
        return self.multiple

    def set_files(self, files):
        self.files = files


class FakeDialog:
    type = "confirm"
    message = "Continue?"
    default_value = ""

    def __init__(self):
        self.accepted = False
        self.dismissed = False

    def accept(self, _prompt_text=None):
        self.accepted = True

    def dismiss(self):
        self.dismissed = True


class FakeDownload:
    suggested_filename = "report.txt"
    url = "https://example.test/report?access_token=secret-value"

    def failure(self):
        return None

    def path(self):
        return "C:\\runtime-temp\\playwright-download"


class BrowserHostRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = FakeWorker()
        self.page = FakePage("https://example.test/report?access_token=secret-value", "Report")
        self.state = self.worker.register_session("session_fixture", self.page)
        self.live = self.worker.sessions["session_fixture"]
        self.runtime = BrowserHostRuntime(FakeManager(self.worker))
        self.stream = None
        self._window_patch = patch(
            "server.oevira.browser_host_runtime._native_window_metadata",
            return_value={"handle": None, "pid": None, "window_id": None, "bounds": None, "association": "unsupported"},
        )
        self._window_patch.start()
        self.addCleanup(self._window_patch.stop)

    def tearDown(self) -> None:
        if self.stream is not None:
            self.stream.close()

    def start_stream(self):
        self.stream = self.runtime.start_stream(
            "chrome", "session_fixture", "page_fixture",
            {"width": 640, "height": 480, "device_scale_factor": 1},
        )
        return self.stream

    def test_stream_uses_live_context_safe_metadata_and_acks_latest_frame(self) -> None:
        stream = self.start_stream()
        self.assertIs(self.runtime.manager._workers[("chrome", "session_fixture")], self.worker)
        self.assertIs(self.worker.sessions["session_fixture"].context, self.live.context)
        self.assertEqual(self.live.context.new_page_count, 0)
        self.assertEqual(stream._viewport, {"width": 640, "height": 480, "device_scale_factor": 1.0})

        events = stream.drain_events()
        self.assertEqual(events[0]["type"], "page")
        self.assertEqual(events[0]["page_id"], "page_fixture")
        self.assertNotIn("native_window", events[0])
        self.assertIn("%5Bredacted%5D", events[0]["url"])
        self.assertNotIn("secret-value", events[0]["url"])
        self.assertEqual(events[0]["tabs"][0]["page_revision"], 0)

        cdp = stream._bindings["page_fixture"].cdp
        frame = b"fake-jpeg-frame"
        cdp.emit("Page.screencastFrame", {"sessionId": 7, "data": base64.b64encode(frame).decode("ascii")})
        self.assertEqual(stream.latest_frame(), frame)
        self.assertIn(("Page.screencastFrameAck", {"sessionId": 7}), cdp.commands)
        stream.pump()
        self.assertEqual(self.page.wait_count, 1)
        self.assertEqual(self.page.last_wait_ms, 8)
        self.assertTrue(any(method == "Page.startScreencast" for method, _ in cdp.commands))

    def test_typed_input_uses_cdp_and_rejects_stale_revisions_and_extra_fields(self) -> None:
        stream = self.start_stream()
        stream.drain_events()
        cdp = stream._bindings["page_fixture"].cdp

        self.assertIsNone(stream.send_input({
            "sequence": 1, "page_id": "page_fixture", "page_revision": 0,
            "type": "pointer", "phase": "down", "x": 10, "y": 20,
            "button": "left", "buttons": 1, "click_count": 1, "modifiers": 0,
        }))
        self.assertTrue(any(method == "Input.dispatchMouseEvent" for method, _ in cdp.commands))

        stale = stream.send_input({"sequence": 2, "page_id": "page_fixture", "page_revision": 3, "type": "text", "text": "x"})
        self.assertEqual(stale, {"type": "error", "code": "BROWSER_PAGE_CHANGED", "current_page_revision": 0})
        before = len(cdp.commands)
        invalid = stream.send_input({
            "sequence": 3, "page_id": "page_fixture", "page_revision": 0,
            "type": "pointer", "phase": "move", "x": 10, "y": 20,
            "button": "none", "buttons": 0, "click_count": 1, "modifiers": 0,
            "path": "C:\\not-accepted.txt",
        })
        self.assertEqual(invalid["code"], "BROWSER_INPUT_INVALID")
        self.assertEqual(len(cdp.commands), before)

        self.assertIsNone(stream.send_input({
            "sequence": 4, "page_id": "page_fixture", "page_revision": 0,
            "type": "text", "text": "IME text",
        }))
        self.assertIn(("Input.insertText", {"text": "IME text"}), cdp.commands)
        out_of_order = stream.send_input({
            "sequence": 6, "page_id": "page_fixture", "page_revision": 0,
            "type": "text", "text": "duplicate?",
        })
        self.assertEqual(out_of_order["expected_sequence"], 5)

    def test_tab_lifecycle_stays_in_the_same_session_and_updates_stream(self) -> None:
        stream = self.start_stream()
        result = self.runtime.tabs("chrome", "session_fixture", "new")
        new_page_id = result["active_page_id"]
        self.assertNotEqual(new_page_id, "page_fixture")
        self.assertEqual(len(result["tabs"]), 2)
        self.assertEqual(stream.page_id, new_page_id)
        self.assertEqual(self.worker.sessions["session_fixture"].context.new_page_count, 1)

        selected = self.runtime.tabs("chrome", "session_fixture", "select", "page_fixture")
        self.assertEqual(selected["active_page_id"], "page_fixture")
        self.assertTrue(self.page.fronted)

        closed = self.runtime.tabs("chrome", "session_fixture", "close", "page_fixture")
        self.assertNotIn("page_fixture", [tab["page_id"] for tab in closed["tabs"]])
        self.assertEqual(closed["active_page_id"], new_page_id)
        self.assertEqual(len(self.worker.sessions["session_fixture"].pages), 1)

        final_close = self.runtime.tabs("chrome", "session_fixture", "close", new_page_id)
        self.assertEqual(len(final_close["tabs"]), 1)
        self.assertEqual(final_close["active_page_id"], stream.page_id)
        self.assertIsNotNone(stream.page_id)
        self.assertEqual(len(self.worker.sessions["session_fixture"].pages), 1)

    def test_file_chooser_uses_bytes_only_and_dialog_waits_for_user_choice(self) -> None:
        stream = self.start_stream()
        chooser = FakeFileChooser(multiple=True)
        self.page.emit("filechooser", chooser)
        event = next(item for item in stream.drain_events() if item["type"] == "file_chooser")
        chooser_id = event["chooser_id"]
        result = stream.set_files(chooser_id, [{"name": "brief.txt", "mimeType": "text/plain", "buffer": b"authorized"}])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(chooser.files[0]["buffer"], b"authorized")
        with self.assertRaises(BrowserError) as path_error:
            stream.set_files("missing", [{"name": "brief.txt", "mimeType": "text/plain", "buffer": b"x", "path": "C:\\secret"}])
        self.assertEqual(path_error.exception.code, "BROWSER_FILE_CHOOSER_INVALID")

        dialog = FakeDialog()
        self.page.emit("dialog", dialog)
        dialog_event = next(item for item in stream.drain_events() if item["type"] == "dialog")
        self.assertFalse(dialog.accepted)
        self.assertFalse(dialog.dismissed)
        answer = stream.send_input({
            "sequence": 1, "page_id": "page_fixture", "page_revision": 0,
            "type": "dialog", "dialog_id": dialog_event["dialog_id"], "accept": False,
        })
        self.assertEqual(answer["status"], "dismissed")
        self.assertFalse(dialog.accepted)
        self.assertTrue(dialog.dismissed)

    def test_copy_reads_page_selection_without_granting_system_clipboard_access(self) -> None:
        stream = self.start_stream()
        stream.drain_events()
        read = stream.send_input({
            "sequence": 1, "page_id": "page_fixture", "page_revision": 0, "type": "clipboard_read",
        })
        self.assertEqual(read, {"type": "clipboard", "text": "fixture clipboard"})
        self.assertEqual(self.live.context.grants, [])
        self.assertEqual(stream.drain_events(), [])

        written = stream.send_input({
            "sequence": 2, "page_id": "page_fixture", "page_revision": 0,
            "type": "clipboard_write", "text": "local test value",
        })
        self.assertEqual(written["code"], "BROWSER_INPUT_UNSUPPORTED")
        self.assertEqual(self.page.clipboard, "fixture clipboard")
        self.assertEqual(self.live.context.grants, [])

    def test_native_stream_does_not_emulate_viewport_or_start_screencast(self) -> None:
        stream = self.runtime.start_stream("chrome", "session_fixture", "page_fixture", native=True)
        self.stream = stream
        cdp = stream._bindings["page_fixture"].cdp
        commands = [method for method, _ in cdp.commands]
        self.assertIn("native_window", stream.drain_events()[0])
        self.assertIn("Page.enable", commands)
        self.assertIn("Page.getLayoutMetrics", commands)
        self.assertNotIn("Emulation.setDeviceMetricsOverride", commands)
        self.assertNotIn("Page.startScreencast", commands)
        self.assertNotIn("dialog", self.page._listeners)
        self.assertNotIn("filechooser", self.page._listeners)
        self.assertIn("download", self.page._listeners)
        self.assertIsNone(stream.latest_frame())
        resize = stream.send_input({
            "sequence": 1, "page_id": "page_fixture", "page_revision": 0,
            "type": "resize", "width": 900, "height": 600,
        })
        self.assertEqual(resize["code"], "BROWSER_ACTION_UNSUPPORTED")

    def test_native_tab_activation_updates_primary_page_without_tabs_api(self) -> None:
        stream = self.runtime.start_stream("chrome", "session_fixture", "page_fixture", native=True)
        self.stream = stream
        stream.drain_events()

        second = self.worker.call(lambda: self.live.context.new_page())
        second_state = next(state for state in self.live.pages.values() if state.page is second)
        second.url = "https://example.test/report?tab=second"
        second._title = self.page.title()
        # A page script can lie in its own world; the host probe only reads
        # Chromium's selected-tab visibility through the isolated CDP world.
        second.main_world_visibility_override = "visible"
        second.main_world_focus_override = True
        stream.drain_events()
        self.assertEqual(self.live.primary_page_id, second_state.page_id)

        self.worker.call(self.page.bring_to_front)
        stream.pump()
        self.assertEqual(stream.page_id, "page_fixture")
        self.assertEqual(self.live.primary_page_id, "page_fixture")
        self.assertEqual(self.worker._snapshot(self.live)["url"], browser_safe_url(self.page.url)[0])
        page_events = [event for event in stream.drain_events() if event.get("type") == "page"]
        self.assertTrue(page_events)
        self.assertEqual(page_events[-1]["page_id"], "page_fixture")
        self.assertEqual(sum(tab["selected"] for tab in page_events[-1]["tabs"]), 1)

        # When the native host hides the browser, no visible page is guessed
        # from tab order; the last page remains the target.
        self.worker.call(lambda: [setattr(page, "native_visibility", "hidden") for page in self.live.context.pages])
        stream.pump()
        self.assertEqual(stream.page_id, "page_fixture")
        self.assertEqual(self.live.primary_page_id, "page_fixture")

        probe = stream._native_page_probes["page_fixture"]
        evaluations_before = sum(method == "Runtime.evaluate" for method, _ in probe.cdp.commands)
        wait_count_before = self.page.wait_count
        probe.cdp.emit("Page.javascriptDialogOpening", {"type": "alert"})
        stream.pump()
        evaluations_after = sum(method == "Runtime.evaluate" for method, _ in probe.cdp.commands)
        self.assertEqual(evaluations_after, evaluations_before, "A native modal must not run a page probe")
        self.assertEqual(self.page.wait_count, wait_count_before, "A native modal must not block in a page wait")
        probe.cdp.emit("Page.javascriptDialogClosed", {})

    def test_native_tab_selection_is_scoped_to_its_owned_session(self) -> None:
        self.runtime.manager._workers[("chrome", "session_other")] = FakeWorker()
        other_worker = self.runtime.manager._workers[("chrome", "session_other")]
        other_page = FakePage("https://example.test/other", "Report")
        other_worker.register_session("session_other", other_page)

        first_stream = self.runtime.start_stream("chrome", "session_fixture", "page_fixture", native=True)
        self.stream = first_stream
        other_stream = self.runtime.start_stream("chrome", "session_other", "page_fixture", native=True)
        try:
            first_active = first_stream.page_id
            other_worker.call(lambda: setattr(other_page, "native_visibility", "hidden"))
            other_worker.call(lambda: setattr(other_page, "native_has_focus", False))
            other_stream.pump()
            self.assertEqual(other_stream.page_id, "page_fixture")
            self.assertEqual(first_stream.page_id, first_active)
            self.assertEqual(self.live.primary_page_id, first_active)
            self.assertEqual(other_worker.sessions["session_other"].primary_page_id, "page_fixture")
        finally:
            other_stream.close()

    def test_download_handle_and_source_are_retained_for_retries(self) -> None:
        stream = self.start_stream()
        stream.drain_events()
        self.page.emit("download", FakeDownload())
        event = next(item for item in stream.drain_events() if item["type"] == "download")
        self.assertNotIn("url", event)
        with patch("server.oevira.browser_host_runtime.os.path.isfile", return_value=True), patch(
            "server.oevira.browser_host_runtime.os.path.getsize", return_value=12,
        ):
            first = stream.download(event["download_id"])
            second = stream.download(event["download_id"])
        self.assertEqual(first, second)
        self.assertEqual(first["path"], "C:\\runtime-temp\\playwright-download")
        self.assertIn("access_token=secret-value", first["url"])
        self.assertIn(event["download_id"], stream._downloads)

    def test_runtime_liveness_gates_stream_data_and_native_session_metadata(self) -> None:
        stream = self.start_stream()
        stream.drain_events()
        self.worker.live = False
        self.assertIsNone(stream.latest_frame())
        self.assertEqual(stream.drain_events()[0]["code"], "BROWSER_SESSION_NOT_OPEN")
        result = stream.send_input({
            "sequence": 1, "page_id": "page_fixture", "page_revision": 0,
            "type": "text", "text": "blocked",
        })
        self.assertEqual(result["code"], "BROWSER_SESSION_NOT_OPEN")
        with self.assertRaises(BrowserError):
            self.runtime.native_session("chrome", "session_fixture", "page_fixture")

    def test_native_window_association_requires_one_matching_owned_window(self) -> None:
        bounds = {"left": 10, "top": 20, "width": 800, "height": 600}
        matching = {"hwnd": 101, "left": 11, "top": 20, "width": 800, "height": 600, "title": "Report - Chrome"}
        unrelated_title = {"hwnd": 202, "left": 10, "top": 20, "width": 800, "height": 600, "title": "Other - Chrome"}
        self.assertEqual(_match_window_bounds([matching, unrelated_title], bounds, "Report"), [matching])
        duplicate = {**matching, "hwnd": 303}
        self.assertEqual(len(_match_window_bounds([matching, duplicate], bounds, "Report")), 2)


if __name__ == "__main__":
    unittest.main()
