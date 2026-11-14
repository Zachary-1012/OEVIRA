"""In-process Chrome host for the existing isolated browser sessions.

This module deliberately attaches to ``BrowserRuntimeManager``'s existing
worker and context. It does not launch a second browser, create a persistent
profile, expose a DevTools endpoint, or accept client-supplied filesystem
paths.
"""
from __future__ import annotations

import base64
import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import math
import os
from queue import Empty, Full, Queue
import re
import threading
import time
import uuid
from typing import Any

from .browser import BrowserError, ENGINE_CHANNELS, MAX_ACTION_TEXT_CHARACTERS, browser_safe_url


MAX_EVENT_QUEUE_SIZE = 512
MAX_STREAM_FRAME_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 24 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 48 * 1024 * 1024
MAX_FILES_PER_CHOOSER = 20
MAX_CLIPBOARD_CHARACTERS = 16_000
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
HOST_INPUT_TIMEOUT_SECONDS = 10
HOST_PUMP_INTERVAL_MS = 8
PAGE_EVENT_INTERVAL_SECONDS = 0.25
NATIVE_ACTIVE_PAGE_POLL_SECONDS = 0.15
NATIVE_PAGE_PROBE_TIMEOUT_SECONDS = 0.15
NATIVE_MAX_PAGES_PER_POLL = 32
NATIVE_ACTIVE_WORLD_NAME = "oevira-native-active-page-v1"
MIN_VIEWPORT_WIDTH = 320
MAX_VIEWPORT_WIDTH = 4096
MIN_VIEWPORT_HEIGHT = 240
MAX_VIEWPORT_HEIGHT = 2160
_SAFE_FILENAME = re.compile(r"^[^\x00-\x1f\x7f/\\:]{1,255}$")
_SAFE_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

_MOUSE_BUTTONS = {"none", "left", "middle", "right", "back", "forward"}
_INPUT_MODIFIERS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "shift": 8}


@dataclass
class _PageBinding:
    page_id: str
    state: Any
    page: Any
    cdp: Any | None = None
    screencast_started: bool = False
    listeners: list[tuple[str, Any]] = field(default_factory=list)


@dataclass
class _NativePageProbe:
    cdp: Any
    frame_id: str | None = None
    context_id: int | None = None
    dialog_open: bool = False
    listeners: list[tuple[str, Any]] = field(default_factory=list)


@dataclass
class _DialogEntry:
    page_id: str
    dialog: Any


@dataclass
class _FileChooserEntry:
    page_id: str
    chooser: Any


@dataclass
class _DownloadEntry:
    page_id: str
    download: Any
    name: str
    source_url: str
    created_at: float


class BrowserHostRuntime:
    """Host the actual pages owned by an existing ``BrowserRuntimeManager``."""

    def __init__(self, manager):
        self.manager = manager
        self._lock = threading.RLock()
        self._streams: dict[tuple[str, str], HostStream] = {}

    def _worker(self, engine: str, session_id: str):
        if engine not in ENGINE_CHANNELS:
            raise BrowserError("BROWSER_ENGINE_INVALID", 400)
        if self.manager.is_stopping(session_id):
            raise BrowserError("BROWSER_RUNTIME_STOPPING", 409)

        # Do not call manager._worker() here: it lazily launches a browser. A
        # host may only attach to a session the session owner already opened.
        workers = getattr(self.manager, "_workers", None)
        worker = workers.get((engine, session_id)) if isinstance(workers, dict) else None
        if worker is None or not callable(getattr(worker, "runtime_is_live", None)):
            raise BrowserError("BROWSER_SESSION_REOPEN_REQUIRED", 409)
        if not getattr(getattr(worker, "thread", None), "is_alive", lambda: True)():
            raise BrowserError("BROWSER_RUNTIME_UNAVAILABLE", 503)
        return worker

    @staticmethod
    def _live(worker, session_id: str):
        try:
            authorized = bool(worker.runtime_is_live(session_id))
        except Exception as exc:
            raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409) from exc
        if not authorized:
            raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
        live = worker.sessions.get(session_id)
        if live is None:
            raise BrowserError("BROWSER_SESSION_REOPEN_REQUIRED", 409)
        return live

    def _call(self, engine: str, session_id: str, callback, *, timeout: float = HOST_INPUT_TIMEOUT_SECONDS):
        worker = self._worker(engine, session_id)

        def operation():
            live = self._live(worker, session_id)
            return callback(worker, live)

        return worker.call(operation, timeout=timeout)

    def tabs(self, engine: str, session_id: str, action: str, page_id: str | None = None) -> dict:
        """List, create, select, or close tabs in the existing session context."""
        if action not in {"list", "new", "select", "close"}:
            raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)

        worker = self._worker(engine, session_id)
        with self._lock:
            stream = self._streams.get((engine, session_id))

        def operation():
            live = self._live(worker, session_id)
            if action == "new":
                page = live.context.new_page()
                state = _find_state(live, page)
                if state is None:
                    state = worker._register_page(live, page)
                live.primary_page_id = state.page_id
                if stream is not None and not stream.closed:
                    stream._attach_page(worker, live, state)
                    stream._activate_page(worker, live, state.page_id, emit=False)
                return _tabs_result(worker, live, state.page_id)

            if action == "select":
                state = _require_page_state(worker, live, page_id)
                live.primary_page_id = state.page_id
                try:
                    state.page.bring_to_front()
                except Exception as exc:
                    raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409) from exc
                if stream is not None and not stream.closed:
                    stream._attach_page(worker, live, state)
                    stream._activate_page(worker, live, state.page_id, emit=False)
                return _tabs_result(worker, live, state.page_id)

            if action == "close":
                state = _require_page_state(worker, live, page_id)
                closing_id = state.page_id
                try:
                    state.page.close()
                except Exception as exc:
                    raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409) from exc
                worker._page_closed(live, closing_id)
                if not live.pages:
                    replacement = live.context.new_page()
                    replacement_state = _find_state(live, replacement)
                    if replacement_state is None:
                        replacement_state = worker._register_page(live, replacement)
                    live.primary_page_id = replacement_state.page_id
                active_id = live.primary_page_id or next(iter(live.pages), None)
                if active_id and stream is not None and not stream.closed:
                    active = _require_page_state(worker, live, active_id)
                    stream._attach_page(worker, live, active)
                    stream._activate_page(worker, live, active_id, emit=False)
                return _tabs_result(worker, live, active_id)

            return _tabs_result(worker, live, live.primary_page_id)

        result = worker.call(operation)
        if stream is not None and not stream.closed:
            stream._emit_page_from_runtime()
        return result

    def command(self, engine: str, session_id: str, command: str | dict, **arguments) -> dict:
        """Execute a small typed host command; arbitrary JS/CDP is not exposed."""
        if isinstance(command, dict):
            if arguments:
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            payload = dict(command)
            kind = payload.pop("type", None)
        elif isinstance(command, str):
            payload = dict(arguments)
            kind = command
        else:
            raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)

        if kind == "snapshot":
            page_id = payload.get("page_id")
            if set(payload) - {"page_id"}:
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self._call(engine, session_id, lambda worker, live: _page_event(worker, live, page_id))
        if kind == "window":
            page_id = payload.get("page_id")
            if set(payload) - {"page_id"}:
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self.native_session(engine, session_id, page_id)
        if kind == "tabs.list":
            if set(payload):
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self.tabs(engine, session_id, "list")
        if kind == "tabs.new":
            if set(payload):
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self.tabs(engine, session_id, "new")
        if kind == "tabs.select":
            page_id = payload.get("page_id")
            if set(payload) != {"page_id"}:
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self.tabs(engine, session_id, "select", page_id)
        if kind == "tabs.close":
            page_id = payload.get("page_id")
            if set(payload) != {"page_id"}:
                raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
            return self.tabs(engine, session_id, "close", page_id)
        if kind == "window.resize":
            raise BrowserError("BROWSER_ACTION_UNSUPPORTED", 400)
        raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)

    def native_session(self, engine: str, session_id: str, page_id: str | None = None) -> dict:
        """Return metadata and, on Windows, a uniquely matched owned HWND."""
        worker = self._worker(engine, session_id)

        def operation():
            live = self._live(worker, session_id)
            state = _require_page_state(worker, live, page_id)
            page = _page_event(worker, live, state.page_id, include_window=True)
            window = page.pop("native_window")
            page.pop("type", None)
            return {
                **page,
                "engine": engine,
                "handle": window["handle"],
                "pid": window["pid"],
                "window_id": window["window_id"],
                "bounds": window["bounds"],
                "association": window["association"],
                "native_window_available": window["handle"] is not None,
            }

        return worker.call(operation)

    def start_stream(self, engine: str, session_id: str, page_id: str | None = None,
                     viewport: dict | None = None, *, native: bool = False) -> "HostStream":
        if not isinstance(native, bool):
            raise BrowserError("BROWSER_HOST_COMMAND_INVALID", 400)
        worker = self._worker(engine, session_id)
        key = (engine, session_id)
        stream = HostStream(self, worker, engine, session_id, viewport or {}, native=native)
        with self._lock:
            existing = self._streams.get(key)
            if existing is not None and not existing.closed:
                raise BrowserError("BROWSER_STREAM_ALREADY_ACTIVE", 409)
            self._streams[key] = stream

        try:
            def operation():
                live = self._live(worker, session_id)
                state = _require_page_state(worker, live, page_id)
                live.primary_page_id = state.page_id
                stream._attach_context(worker, live)
                for page_state in list(live.pages.values()):
                    if not page_state.page.is_closed():
                        stream._attach_page(worker, live, page_state)
                stream._activate_page(worker, live, state.page_id, emit=False)
                stream._emit_page(worker, live, state.page_id, force=True, include_window=native)
            worker.call(operation)
            return stream
        except Exception:
            with self._lock:
                if self._streams.get(key) is stream:
                    self._streams.pop(key, None)
            stream._closed = True
            raise

    def _forget_stream(self, stream: "HostStream") -> None:
        key = (stream.engine, stream.session_id)
        with self._lock:
            if self._streams.get(key) is stream:
                self._streams.pop(key, None)


class HostStream:
    """A page-bound CDP screencast plus typed input and event channels."""

    def __init__(self, runtime: BrowserHostRuntime, worker, engine: str, session_id: str,
                 viewport: dict, *, native: bool = False):
        self.runtime = runtime
        self.worker = worker
        self.engine = engine
        self.session_id = session_id
        self.native = native
        self.events: Queue = Queue(maxsize=MAX_EVENT_QUEUE_SIZE)
        self._closed = False
        self._page_id: str | None = None
        self._viewport = _validate_viewport(viewport)
        self._bindings: dict[str, _PageBinding] = {}
        self._native_page_probes: dict[str, _NativePageProbe] = {}
        self._last_native_page_poll = 0.0
        self._native_page_cursor = 0
        self._context_handler = None
        self._download_lock = threading.RLock()
        self._dialogs: dict[str, _DialogEntry] = {}
        self._file_choosers: dict[str, _FileChooserEntry] = {}
        self._downloads: dict[str, _DownloadEntry] = {}
        self._latest_frame: bytes | None = None
        self._frame_lock = threading.Lock()
        self._event_overflow = False
        self._last_page_event: tuple | None = None
        self._last_page_event_at = 0.0
        self._last_sequence = 0
        self._input_lock = threading.Lock()
        self._pending_inputs = 0
        self.input_authorizer = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def page_id(self) -> str | None:
        return self._page_id

    def latest_frame(self) -> bytes | None:
        """Return the most recently received JPEG frame, if one has arrived."""
        if not self._session_is_live():
            with self._frame_lock:
                self._latest_frame = None
            return None
        with self._frame_lock:
            return self._latest_frame

    def drain_events(self) -> list[dict]:
        if not self._session_is_live():
            while True:
                try:
                    self.events.get_nowait()
                except Empty:
                    break
            self._event_overflow = False
            return [{"type": "error", "code": "BROWSER_SESSION_NOT_OPEN"}]
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        if self._event_overflow:
            self._event_overflow = False
            result.append({"type": "error", "code": "BROWSER_HOST_EVENT_OVERFLOW"})
        return result

    def _session_is_live(self) -> bool:
        if self._closed:
            return False
        try:
            if self.runtime.manager.is_stopping(self.session_id):
                return False
            return bool(self.worker.runtime_is_live(self.session_id))
        except Exception:
            return False

    def initial_state(self) -> dict:
        """Return current safe page metadata; start_stream queues it first."""
        if self._closed:
            raise BrowserError("BROWSER_STREAM_CLOSED", 409)

        def operation(worker, live):
            if self.native:
                self._sync_native_active_page(worker, live, emit=False, force=True)
            page_id = self._page_id or live.primary_page_id
            return _page_event(worker, live, page_id, include_window=self.native)

        return self.runtime._call(self.engine, self.session_id, operation)

    def pump(self) -> None:
        """Pump sync Playwright's event loop so CDP frames and popups dispatch."""
        if self._closed:
            return

        def operation():
            live = self.runtime._live(self.worker, self.session_id)
            if self.native:
                self._sync_native_active_page(self.worker, live, emit=True)
            state = live.pages.get(self._page_id) if self._page_id else None
            if state is None or state.page.is_closed():
                active_id = live.primary_page_id or next(iter(live.pages), None)
                if active_id is None:
                    return
                self._activate_page(self.worker, live, active_id, emit=True)
                return
            probe = self._native_page_probes.get(self._page_id) if self.native else None
            if probe is None or not probe.dialog_open:
                state.page.wait_for_timeout(HOST_PUMP_INTERVAL_MS)
            if self._closed:
                return
            if self.native:
                self._sync_native_active_page(self.worker, live, emit=True)
            now = time.monotonic()
            if now - self._last_page_event_at >= PAGE_EVENT_INTERVAL_SECONDS:
                self._emit_page(self.worker, live, self._page_id, include_window=False)

        self.worker.call(operation, timeout=HOST_INPUT_TIMEOUT_SECONDS)

    def _sync_native_active_page(self, worker, live, *, emit: bool, force: bool = False) -> str | None:
        """Align native Chrome's selected tab with the existing live-page owner.

        Chromium tab visibility is read from an isolated CDP world, so page
        script overrides of ``document.visibilityState`` or ``hasFocus`` do not
        select the host page. Missing/ambiguous observations preserve the last
        active page instead of guessing from title, URL, or dictionary order.
        """
        if not self.native or self._closed:
            return self._page_id
        now = time.monotonic()
        if not force and now - self._last_native_page_poll < NATIVE_ACTIVE_PAGE_POLL_SECONDS:
            return self._page_id
        self._last_native_page_poll = now

        page_items = list(live.pages.items())
        if not page_items:
            return None
        if len(page_items) > NATIVE_MAX_PAGES_PER_POLL:
            current = next((item for item in page_items if item[0] == self._page_id), None)
            remaining = [item for item in page_items if item[0] != self._page_id]
            start = self._native_page_cursor % len(remaining)
            rotated = remaining[start:] + remaining[:start]
            page_items = ([current] if current is not None else []) + rotated[:NATIVE_MAX_PAGES_PER_POLL - (1 if current else 0)]
            self._native_page_cursor = (start + max(1, NATIVE_MAX_PAGES_PER_POLL - 1)) % len(remaining)
        probed_page_ids = {page_id for page_id, _state in page_items}
        for page_id in list(self._native_page_probes):
            if page_id not in probed_page_ids:
                self._drop_native_page_probe(page_id)

        visible_pages = []
        focused_pages = []
        dialog_pages = []
        observed_all = True
        for page_id, state in page_items:
            if state.page.is_closed():
                self._drop_native_page_probe(page_id)
                continue
            binding = self._attach_page(worker, live, state)
            observation = self._native_page_visibility(live, binding)
            if observation is None:
                observed_all = False
                continue
            if observation["dialog_open"]:
                dialog_pages.append(page_id)
                continue
            if observation["visibility"] == "visible":
                visible_pages.append(page_id)
                if observation["focused"]:
                    focused_pages.append(page_id)

        # Playwright's --disable-backgrounding-occluded-windows (and an
        # off-screen owned window) make every tab report visibilityState
        # "visible". Unique document.hasFocus() is the remaining signal
        # for which tab Chrome actually selected, and it is only truthful
        # after the probe disables focus emulation below.
        target = None
        if len(dialog_pages) == 1:
            # A native modal belongs to its page. Keep that page in the host
            # without resolving the dialog on the user's behalf.
            target = dialog_pages[0]
        elif observed_all and len(focused_pages) == 1:
            target = focused_pages[0]
        elif observed_all and len(visible_pages) == 1:
            target = visible_pages[0]
        elif self._page_id in visible_pages:
            target = self._page_id

        if target is None:
            return self._page_id if self._page_id in live.pages else live.primary_page_id
        if target != self._page_id:
            self._activate_page(worker, live, target, emit=emit)
        else:
            previous_primary = live.primary_page_id
            live.primary_page_id = target
            if emit and previous_primary != target:
                self._emit_page(worker, live, target, force=True, include_window=True)
        return target

    def _native_page_visibility(self, live, binding: _PageBinding) -> dict | None:
        page_id = binding.page_id
        probe = self._native_page_probes.get(page_id)
        if probe is None:
            try:
                probe = _NativePageProbe(live.context.new_cdp_session(binding.page))
                self._native_page_probes[page_id] = probe

                def dialog_opened(_payload, entry=probe):
                    entry.dialog_open = True

                def dialog_closed(_payload, entry=probe):
                    entry.dialog_open = False

                def navigated(payload, entry=probe):
                    frame = payload.get("frame", {}) if isinstance(payload, dict) else {}
                    if not isinstance(frame, dict):
                        return
                    if not frame.get("parentId") or frame.get("id") == entry.frame_id:
                        frame_id = frame.get("id")
                        entry.frame_id = frame_id if isinstance(frame_id, str) else None
                        entry.context_id = None

                for event, callback in (
                    ("Page.javascriptDialogOpening", dialog_opened),
                    ("Page.javascriptDialogClosed", dialog_closed),
                    ("Page.frameNavigated", navigated),
                ):
                    probe.cdp.on(event, callback)
                    probe.listeners.append((event, callback))
                if self._bounded_native_cdp_send(probe.cdp, "Page.enable", {}) is None:
                    return None
            except Exception:
                self._drop_native_page_probe(page_id)
                return None

        if probe.dialog_open:
            return {"visibility": "dialog", "focused": False, "dialog_open": True}

        if probe.context_id is None:
            tree = self._bounded_native_cdp_send(probe.cdp, "Page.getFrameTree", {})
            frame_tree = tree.get("frameTree", {}) if isinstance(tree, dict) else {}
            frame = frame_tree.get("frame", {}) if isinstance(frame_tree, dict) else {}
            frame_id = frame.get("id") if isinstance(frame, dict) else None
            if not isinstance(frame_id, str) or not frame_id:
                return None
            probe.frame_id = frame_id
            world = self._bounded_native_cdp_send(probe.cdp, "Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": NATIVE_ACTIVE_WORLD_NAME,
                "grantUniveralAccess": False,
            })
            context_id = world.get("executionContextId") if isinstance(world, dict) else None
            if isinstance(context_id, bool) or not isinstance(context_id, int):
                return None
            probe.context_id = context_id

        # Playwright enables Emulation.setFocusEmulationEnabled on every
        # page, which forces document.hasFocus() to true in every tab.
        # The setting is per target, so it must be cleared on this CDP
        # session before the isolated-world read, then restored so
        # Playwright input continues to work.
        self._bounded_native_cdp_send(
            probe.cdp, "Emulation.setFocusEmulationEnabled", {"enabled": False},
        )
        try:
            result = self._bounded_native_cdp_send(probe.cdp, "Runtime.evaluate", {
                "expression": "({visibility: document.visibilityState, focused: document.hasFocus()})",
                "contextId": probe.context_id,
                "returnByValue": True,
                "awaitPromise": False,
                "silent": True,
            }, timeout=NATIVE_PAGE_PROBE_TIMEOUT_SECONDS)
        finally:
            self._bounded_native_cdp_send(
                probe.cdp, "Emulation.setFocusEmulationEnabled", {"enabled": True},
            )
        if result is None:
            return {"visibility": "dialog", "focused": False, "dialog_open": True} if probe.dialog_open else None
        if result.get("exceptionDetails"):
            probe.context_id = None
            return None
        payload = result.get("result", {}).get("value", {})
        visibility = payload.get("visibility") if isinstance(payload, dict) else None
        focused = payload.get("focused") if isinstance(payload, dict) else None
        if visibility not in {"visible", "hidden", "prerender"} or not isinstance(focused, bool):
            probe.context_id = None
            return None
        return {"visibility": visibility, "focused": focused, "dialog_open": False}

    @staticmethod
    def _bounded_native_cdp_send(cdp, method: str, params: dict, *, timeout: float = NATIVE_PAGE_PROBE_TIMEOUT_SECONDS):
        """Bound a read-only native focus probe; a modal or stalled renderer cannot pin the worker."""
        implementation = getattr(cdp, "_impl_obj", None)
        sync = getattr(cdp, "_sync", None)
        if implementation is None or not callable(sync):
            return cdp.send(method, params)
        try:
            return sync(asyncio.wait_for(implementation.send(method, params), timeout=timeout))
        except (asyncio.TimeoutError, TimeoutError):
            return None
        except Exception:
            return None

    def _drop_native_page_probe(self, page_id: str) -> None:
        probe = self._native_page_probes.pop(page_id, None)
        if probe is None:
            return
        for event, callback in probe.listeners:
            _remove_listener(probe.cdp, event, callback)
        try:
            probe.cdp.detach()
        except Exception:
            pass

    def send_input(self, event: dict) -> dict | None:
        """Validate and dispatch one ordered, typed input event."""
        error = self._claim_input(event)
        if error is not None:
            return error
        with self._input_lock:
            self._pending_inputs += 1
        try:
            return self.worker.call(lambda: self._dispatch_input(event), timeout=HOST_INPUT_TIMEOUT_SECONDS)
        except BrowserError as exc:
            if exc.code == "BROWSER_PAGE_CHANGED":
                self._emit_page_from_runtime()
            return {"type": "error", "code": exc.code, **exc.details}
        except Exception:
            return {"type": "error", "code": "BROWSER_OPERATION_FAILED"}
        finally:
            with self._input_lock:
                self._pending_inputs -= 1

    def _claim_input(self, event: dict) -> dict | None:
        if self._closed:
            return {"type": "error", "code": "BROWSER_STREAM_CLOSED"}
        if not isinstance(event, dict):
            return {"type": "error", "code": "BROWSER_INPUT_INVALID"}

        sequence = event.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            return {"type": "error", "code": "BROWSER_INPUT_SEQUENCE_INVALID"}

        with self._input_lock:
            if sequence != self._last_sequence + 1:
                return {
                    "type": "error",
                    "code": "BROWSER_INPUT_SEQUENCE_INVALID",
                    "expected_sequence": self._last_sequence + 1,
                }
            self._last_sequence = sequence
        return None

    @property
    def has_open_dialog(self) -> bool:
        return bool(self._dialogs)

    def interrupt_dialog(self, event: dict) -> dict:
        """Resolve a modal while Chromium is still waiting to finish the click.

        Playwright 1.64's sync CDP send blocks until the modal is answered. Its
        existing protocol loop must receive this one fixed command directly;
        enqueueing it behind that click deadlocks. No raw CDP is exposed.
        """
        error = self._claim_input(event)
        if error is not None:
            return error
        if not self._session_is_live():
            return {"type": "error", "code": "BROWSER_REAUTH_REQUIRED"}
        if self.input_authorizer is not None:
            try:
                self.input_authorizer(event)
            except BrowserError as exc:
                return {"type": "error", "code": exc.code, **exc.details}
        event = {key: value for key, value in event.items() if key != "control_generation"}
        if event.get("type") != "dialog":
            return {"type": "error", "code": "BROWSER_DIALOG_OPEN"}
        fields = {"type", "sequence", "page_id", "page_revision", "dialog_id", "accept"}
        if set(event) not in (fields, fields | {"prompt_text"}) or not isinstance(event.get("accept"), bool):
            return {"type": "error", "code": "BROWSER_INPUT_INVALID"}
        entry = self._dialogs.get(event["dialog_id"])
        binding = self._bindings.get(event.get("page_id"))
        if entry is None or binding is None or entry.page_id != event["page_id"] or binding.state.revision != event.get("page_revision"):
            return {"type": "error", "code": "BROWSER_PAGE_CHANGED"}
        prompt = event.get("prompt_text")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > MAX_ACTION_TEXT_CHARACTERS):
            return {"type": "error", "code": "BROWSER_INPUT_INVALID"}
        params = {"accept": event["accept"]}
        if prompt is not None:
            params["promptText"] = prompt
        try:
            implementation = binding.cdp._impl_obj
            future = asyncio.run_coroutine_threadsafe(implementation.send("Page.handleJavaScriptDialog", params), implementation._loop)
            future.result(HOST_INPUT_TIMEOUT_SECONDS)
            self._dialogs.pop(event["dialog_id"], None)
            return {"type": "dialog", "dialog_id": event["dialog_id"], "status": "accepted" if event["accept"] else "dismissed"}
        except Exception:
            return {"type": "error", "code": "BROWSER_DIALOG_UNAVAILABLE"}

    def _dispatch_input(self, event: dict) -> dict | None:
        control = None
        if self.input_authorizer is not None:
            control = self.input_authorizer(event)
        event = {key: value for key, value in event.items() if key != "control_generation"}
        live = self.runtime._live(self.worker, self.session_id)
        kind = event.get("type")
        page_id = event.get("page_id")
        revision = event.get("page_revision")
        if not isinstance(page_id, str) or not page_id:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise BrowserError("BROWSER_PAGE_REVISION_INVALID", 400)

        envelope = {"sequence", "page_id", "page_revision", "type"}
        if kind == "pointer":
            _require_fields(event, envelope | {"phase", "x", "y", "button", "buttons", "click_count", "modifiers"})
        elif kind == "wheel":
            _require_fields(event, envelope | {"x", "y", "delta_x", "delta_y", "modifiers"})
        elif kind == "key":
            _require_fields(event, envelope | {"phase", "key", "code", "key_code", "modifiers"})
        elif kind == "text":
            _require_fields(event, envelope | {"text"})
        elif kind == "resize":
            fields = set(event)
            if fields != envelope | {"width", "height"} and fields != envelope | {"width", "height", "device_scale_factor"}:
                raise BrowserError("BROWSER_INPUT_INVALID", 400)
        elif kind == "input_barrier":
            _require_fields(event, envelope)
        elif kind == "clipboard_read":
            if set(event) not in (envelope, envelope | {"cut"}) or not isinstance(event.get("cut", False), bool):
                raise BrowserError("BROWSER_INPUT_INVALID", 400)
        elif kind == "dialog":
            fields = set(event)
            required = envelope | {"dialog_id", "accept"}
            if fields != required and fields != required | {"prompt_text"}:
                raise BrowserError("BROWSER_INPUT_INVALID", 400)
        else:
            raise BrowserError("BROWSER_INPUT_UNSUPPORTED", 400)

        state = _require_page_state(self.worker, live, page_id)
        if state.revision != revision:
            raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
        if self._page_id != page_id:
            raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_id": self._page_id})
        binding = self._bindings.get(page_id)
        if binding is None or binding.cdp is None:
            raise BrowserError("BROWSER_STREAM_UNAVAILABLE", 503)

        if kind == "input_barrier":
            return {"type": "input_ack", "sequence": event["sequence"], "page_id": page_id, "page_revision": state.revision,
                    **({"control_generation": control["control_generation"]} if control is not None else {})}
        if kind == "pointer":
            self._dispatch_pointer(binding, event)
        elif kind == "wheel":
            self._dispatch_wheel(binding, event)
        elif kind == "key":
            self._dispatch_key(binding, event)
        elif kind == "text":
            text = event.get("text")
            if not isinstance(text, str) or len(text) > MAX_CLIPBOARD_CHARACTERS:
                raise BrowserError("BROWSER_INPUT_INVALID", 400)
            binding.cdp.send("Input.insertText", {"text": text})
        elif kind == "resize":
            self._resize(binding, event)
        elif kind == "clipboard_read":
            return self._clipboard_read(binding, cut=event.get("cut", False))
        elif kind == "dialog":
            return self._resolve_dialog(page_id, event)
        return None

    def _dispatch_pointer(self, binding: _PageBinding, event: dict) -> None:
        phase = event.get("phase")
        if phase not in {"move", "down", "up"}:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        x = _finite_number(event.get("x"), "BROWSER_INPUT_INVALID", minimum=0, maximum=self._viewport["width"])
        y = _finite_number(event.get("y"), "BROWSER_INPUT_INVALID", minimum=0, maximum=self._viewport["height"])
        button = event.get("button", "none")
        if button not in _MOUSE_BUTTONS:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        buttons = event.get("buttons", 0)
        if isinstance(buttons, bool) or not isinstance(buttons, int) or not 0 <= buttons <= 31:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        click_count = event.get("click_count", 1)
        if isinstance(click_count, bool) or not isinstance(click_count, int) or not 0 <= click_count <= 3:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        modifiers = _modifiers(event.get("modifiers", 0))
        cdp_type = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}[phase]
        params = {"type": cdp_type, "x": x, "y": y, "button": button, "buttons": buttons, "modifiers": modifiers}
        if phase in {"down", "up"} and click_count:
            params["clickCount"] = click_count
        binding.cdp.send("Input.dispatchMouseEvent", params)

    def _dispatch_wheel(self, binding: _PageBinding, event: dict) -> None:
        x = _finite_number(event.get("x"), "BROWSER_INPUT_INVALID", minimum=0, maximum=self._viewport["width"])
        y = _finite_number(event.get("y"), "BROWSER_INPUT_INVALID", minimum=0, maximum=self._viewport["height"])
        delta_x = _finite_number(event.get("delta_x"), "BROWSER_INPUT_INVALID", minimum=-10000, maximum=10000)
        delta_y = _finite_number(event.get("delta_y"), "BROWSER_INPUT_INVALID", minimum=-10000, maximum=10000)
        modifiers = _modifiers(event.get("modifiers", 0))
        binding.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": delta_x, "deltaY": delta_y, "modifiers": modifiers,
        })

    def _dispatch_key(self, binding: _PageBinding, event: dict) -> None:
        phase = event.get("phase")
        if phase not in {"down", "up"}:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        key = event.get("key")
        code = event.get("code")
        key_code = event.get("key_code", 0)
        if not isinstance(key, str) or not 1 <= len(key) <= 64 or not isinstance(code, str) or len(code) > 64:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        if isinstance(key_code, bool) or not isinstance(key_code, int) or not 0 <= key_code <= 0xFFFF:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        modifiers = _modifiers(event.get("modifiers", 0))
        params = {"type": "keyDown" if phase == "down" else "keyUp", "key": key,
                  "code": code, "windowsVirtualKeyCode": key_code, "modifiers": modifiers}
        if phase == "down" and len(key) == 1:
            params["text"] = key
            params["unmodifiedText"] = key
        binding.cdp.send("Input.dispatchKeyEvent", params)

    def _resize(self, binding: _PageBinding, event: dict) -> None:
        if self.native:
            raise BrowserError("BROWSER_ACTION_UNSUPPORTED", 400)
        self._viewport = _validate_viewport({
            "width": event.get("width"),
            "height": event.get("height"),
            "device_scale_factor": event.get("device_scale_factor", 1),
        })
        self._apply_viewport(binding)
        self._start_screencast(binding)
        live = self.worker.sessions.get(self.session_id)
        if live is not None:
            self._emit_page(self.worker, live, self._page_id, force=True)

    def _clipboard_read(self, binding: _PageBinding, *, cut: bool = False) -> dict:
        # Remote users copy the selected page text, never the server's OS clipboard.
        text = ""
        for frame in binding.page.frames:
            try:
                value = frame.evaluate("""() => {
                    if (!document.hasFocus()) return '';
                    const active = document.activeElement;
                    if (active && active.type === 'password') return '';
                    if (active && typeof active.selectionStart === 'number')
                        return active.value.slice(active.selectionStart, active.selectionEnd);
                    return window.getSelection()?.toString() || '';
                }""")
                if isinstance(value, str) and value:
                    text = value
                    break
            except Exception:
                continue
        if len(text) > MAX_CLIPBOARD_CHARACTERS:
            raise BrowserError("BROWSER_CLIPBOARD_TOO_LARGE", 413)
        if cut and text:
            binding.cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46})
            binding.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46})
        return {"type": "clipboard", "text": text}

    def _resolve_dialog(self, page_id: str, event: dict) -> dict:
        dialog_id = event.get("dialog_id")
        accept = event.get("accept")
        prompt_text = event.get("prompt_text")
        if not isinstance(dialog_id, str) or not isinstance(accept, bool):
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        if prompt_text is not None and (not isinstance(prompt_text, str) or len(prompt_text) > MAX_ACTION_TEXT_CHARACTERS):
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        entry = self._dialogs.get(dialog_id)
        if entry is None or entry.page_id != page_id:
            raise BrowserError("BROWSER_DIALOG_NOT_FOUND", 404)
        try:
            if accept:
                entry.dialog.accept(prompt_text) if prompt_text is not None else entry.dialog.accept()
            else:
                entry.dialog.dismiss()
        except Exception as exc:
            raise BrowserError("BROWSER_DIALOG_UNAVAILABLE", 409) from exc
        self._dialogs.pop(dialog_id, None)
        return {"type": "dialog", "dialog_id": dialog_id, "status": "accepted" if accept else "dismissed"}

    def set_files(self, chooser_id: str, files: list[dict]) -> dict:
        """Set authorized file bytes on the matching chooser; paths are rejected."""
        if self._closed or not isinstance(chooser_id, str):
            raise BrowserError("BROWSER_FILE_CHOOSER_UNAVAILABLE", 409)
        payloads = _validate_file_payloads(files)

        def operation():
            live = self.runtime._live(self.worker, self.session_id)
            entry = self._file_choosers.get(chooser_id)
            if entry is None or entry.page_id not in live.pages:
                raise BrowserError("BROWSER_FILE_CHOOSER_UNAVAILABLE", 409)
            try:
                if not entry.chooser.is_multiple() and len(payloads) > 1:
                    raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
                entry.chooser.set_files(payloads)
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError("BROWSER_FILE_CHOOSER_UNAVAILABLE", 409) from exc
            self._file_choosers.pop(chooser_id, None)
            return {"chooser_id": chooser_id, "file_count": len(payloads)}

        return self.worker.call(operation, timeout=HOST_INPUT_TIMEOUT_SECONDS)

    def cancel_file_chooser(self, chooser_id: str) -> dict:
        def operation():
            self.runtime._live(self.worker, self.session_id)
            if chooser_id not in self._file_choosers:
                raise BrowserError("BROWSER_FILE_CHOOSER_UNAVAILABLE", 409)
            self._file_choosers.pop(chooser_id)
            return {"chooser_id": chooser_id, "status": "cancelled"}

        return self.worker.call(operation, timeout=HOST_INPUT_TIMEOUT_SECONDS)

    def download(self, download_id: str) -> dict:
        """Resolve a user-started download for the internal authorized store only.

        The returned native temp path is an internal callback value. It must
        never be serialized to or accepted from a browser client.
        """
        if self._closed or not isinstance(download_id, str):
            raise BrowserError("BROWSER_DOWNLOAD_UNAVAILABLE", 409)

        def operation():
            self.runtime._live(self.worker, self.session_id)
            entry = self._downloads.get(download_id)
            if entry is None:
                raise BrowserError("BROWSER_DOWNLOAD_NOT_FOUND", 404)
            try:
                failure = entry.download.failure()
                if failure:
                    raise BrowserError("BROWSER_DOWNLOAD_UNAVAILABLE", 409)
                path = entry.download.path()
                if path is None:
                    raise BrowserError("BROWSER_DOWNLOAD_UNAVAILABLE", 409)
                native_path = os.fspath(path)
                if not os.path.isfile(native_path) or os.path.getsize(native_path) > MAX_DOWNLOAD_BYTES:
                    raise BrowserError("BROWSER_DOWNLOAD_UNAVAILABLE", 413)
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError("BROWSER_DOWNLOAD_UNAVAILABLE", 409) from exc
            return {
                "download_id": download_id,
                "name": entry.name,
                "path": native_path,
                "url": entry.source_url,
                "status": "completed",
            }

        return self.worker.call(operation, timeout=HOST_INPUT_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Stop only this host stream; the owning browser session stays intact."""
        if self._closed:
            return
        self._closed = True
        self.runtime._forget_stream(self)
        for entry in list(self._dialogs.values()) if getattr(self, "_pending_inputs", 0) else []:
            binding = self._bindings.get(entry.page_id)
            if binding is not None and binding.cdp is not None:
                implementation = binding.cdp._impl_obj
                asyncio.run_coroutine_threadsafe(implementation.send("Page.handleJavaScriptDialog", {"accept": False}), implementation._loop)

        def cleanup():
            live = self.worker.sessions.get(self.session_id)
            if live is None:
                for page_id in list(self._native_page_probes):
                    self._drop_native_page_probe(page_id)
                return
            self._detach_context(live)
            for dialog_id, entry in list(self._dialogs.items()):
                try:
                    # Closing a host must never silently confirm page actions.
                    entry.dialog.dismiss()
                except Exception:
                    pass
                self._dialogs.pop(dialog_id, None)
            for page_id in list(self._bindings):
                self._detach_page(page_id, stop_screencast=True)
            for page_id in list(self._native_page_probes):
                self._drop_native_page_probe(page_id)
            self._file_choosers.clear()
            self._downloads.clear()

        try:
            self.worker.call(cleanup, timeout=HOST_INPUT_TIMEOUT_SECONDS)
        except Exception:
            pass

    def _attach_context(self, worker, live) -> None:
        if self._context_handler is not None:
            return

        def on_page(page):
            if self._closed:
                return
            state = _find_state(live, page)
            if state is None:
                try:
                    state = worker._register_page(live, page)
                except Exception:
                    self._emit({"type": "error", "code": "BROWSER_PAGE_UNAVAILABLE"})
                    return
            self._attach_page(worker, live, state)
            if live.primary_page_id == state.page_id:
                try:
                    self._activate_page(worker, live, state.page_id, emit=False)
                except BrowserError as exc:
                    self._emit({"type": "error", "code": exc.code})
            self._emit_page(worker, live, state.page_id, force=True, include_window=self.native)

        self._context_handler = on_page
        live.context.on("page", on_page)

    def _attach_page(self, worker, live, state) -> _PageBinding:
        existing = self._bindings.get(state.page_id)
        if existing is not None:
            return existing
        page = state.page
        binding = _PageBinding(state.page_id, state, page)

        def on_navigated(frame):
            if frame == page.main_frame:
                self._drop_page_choosers(state.page_id)
                self._emit_page(worker, live, state.page_id, force=True)

        def on_load(*_args):
            self._emit_page(worker, live, state.page_id, force=True)

        def on_close(*_args):
            self._on_page_closed(worker, live, state.page_id)

        def on_dialog(dialog):
            dialog_id = "dialog_" + uuid.uuid4().hex
            self._dialogs[dialog_id] = _DialogEntry(state.page_id, dialog)
            self._emit({
                "type": "dialog",
                "dialog_id": dialog_id,
                "page_id": state.page_id,
                "page_revision": state.revision,
                "url": browser_safe_url(page.url or "about:blank")[0],
                "dialog_type": str(getattr(dialog, "type", ""))[:32],
                "message": str(getattr(dialog, "message", ""))[:MAX_ACTION_TEXT_CHARACTERS],
                "default_value": str(getattr(dialog, "default_value", ""))[:MAX_ACTION_TEXT_CHARACTERS],
            })

        def on_filechooser(chooser):
            chooser_id = "chooser_" + uuid.uuid4().hex
            self._file_choosers[chooser_id] = _FileChooserEntry(state.page_id, chooser)
            try:
                multiple = bool(chooser.is_multiple())
            except Exception:
                multiple = False
            self._emit({"type": "file_chooser", "chooser_id": chooser_id, "page_id": state.page_id, "multiple": multiple})

        def on_download(download):
            if len(self._downloads) >= 64:
                try:
                    download.cancel()
                except Exception:
                    pass
                self._emit({"type": "error", "code": "BROWSER_DOWNLOAD_LIMIT_REACHED", "page_id": state.page_id})
                return
            download_id = "download_" + uuid.uuid4().hex
            name = _safe_download_name(getattr(download, "suggested_filename", "download"))
            source_url = getattr(download, "url", "")
            if not isinstance(source_url, str):
                source_url = ""
            self._downloads[download_id] = _DownloadEntry(state.page_id, download, name, source_url, time.monotonic())
            self._emit({"type": "download", "download_id": download_id, "page_id": state.page_id,
                        "name": name, "status": "started"})

        listeners = [
            ("framenavigated", on_navigated),
            ("domcontentloaded", on_load),
            ("load", on_load),
            ("close", on_close),
            ("download", on_download),
        ]
        if not self.native:
            listeners.extend([("dialog", on_dialog), ("filechooser", on_filechooser)])
        binding.listeners.extend(listeners)
        for name, handler in listeners:
            page.on(name, handler)
        self._bindings[state.page_id] = binding
        return binding

    def _activate_page(self, worker, live, page_id: str, *, emit: bool) -> None:
        state = _require_page_state(worker, live, page_id)
        previous = self._page_id
        if previous and previous != page_id:
            self._detach_page(previous, stop_screencast=True)
        binding = self._attach_page(worker, live, state)
        if binding.cdp is None:
            try:
                binding.cdp = live.context.new_cdp_session(binding.page)
                binding.cdp.on("Page.screencastFrame", lambda payload, bound=binding: self._on_screencast_frame(bound, payload))
                binding.cdp.send("Page.enable")
            except Exception as exc:
                binding.cdp = None
                raise BrowserError("BROWSER_STREAM_UNAVAILABLE", 503) from exc
        self._page_id = page_id
        live.primary_page_id = page_id
        try:
            binding.page.bring_to_front()
        except Exception:
            pass
        if self.native:
            self._read_native_viewport(binding)
        else:
            self._apply_viewport(binding)
            self._start_screencast(binding)
        if emit:
            self._emit_page(worker, live, page_id, force=True, include_window=self.native)

    def _read_native_viewport(self, binding: _PageBinding) -> None:
        if binding.cdp is None:
            return
        try:
            metrics = binding.cdp.send("Page.getLayoutMetrics")
            viewport = metrics.get("cssLayoutViewport", {})
            width = viewport.get("clientWidth")
            height = viewport.get("clientHeight")
            if isinstance(width, (int, float)) and not isinstance(width, bool) and width > 0:
                self._viewport["width"] = int(width)
            if isinstance(height, (int, float)) and not isinstance(height, bool) and height > 0:
                self._viewport["height"] = int(height)
        except Exception:
            # A missing layout readback does not authorize us to emulate a
            # viewport or report one; keep the previous coordinate bounds.
            return

    def _apply_viewport(self, binding: _PageBinding) -> None:
        viewport = self._viewport
        binding.cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": viewport["width"],
            "height": viewport["height"],
            "deviceScaleFactor": viewport["device_scale_factor"],
            "mobile": False,
            "screenWidth": viewport["width"],
            "screenHeight": viewport["height"],
        })
        self._await_applied_viewport(binding)

    def _await_applied_viewport(self, binding: _PageBinding, *, timeout: float = 2.0) -> None:
        """Block screencast start until Chromium actually has the requested viewport.

        Page.startScreencast scales the current page into maxWidth/maxHeight
        while preserving aspect ratio. If this runs while Playwright's default
        1280x800 context is still in effect, the first frames are 800x500 for
        a requested 800x600 viewport.
        """
        expected_width = self._viewport["width"]
        expected_height = self._viewport["height"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                metrics = binding.cdp.send("Page.getLayoutMetrics")
            except Exception:
                return
            layout = metrics.get("cssLayoutViewport") if isinstance(metrics, dict) else {}
            if not isinstance(layout, dict):
                layout = {}
            width = layout.get("clientWidth")
            height = layout.get("clientHeight")
            # If the session cannot report layout, waiting cannot converge. Do
            # not spin, and do not use page.wait_for_timeout — that API is the
            # stream pump interval, not a layout barrier.
            if width is None or height is None:
                return
            try:
                if int(width) == expected_width and int(height) == expected_height:
                    return
            except (TypeError, ValueError):
                return
            time.sleep(0.016)

    def _start_screencast(self, binding: _PageBinding) -> None:
        if binding.cdp is None:
            raise BrowserError("BROWSER_STREAM_UNAVAILABLE", 503)
        try:
            if binding.screencast_started:
                binding.cdp.send("Page.stopScreencast")
            binding.cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 80,
                "maxWidth": self._viewport["width"],
                "maxHeight": self._viewport["height"],
                "everyNthFrame": 1,
            })
            binding.screencast_started = True
        except Exception as exc:
            binding.screencast_started = False
            raise BrowserError("BROWSER_STREAM_UNAVAILABLE", 503) from exc

    def _on_screencast_frame(self, binding: _PageBinding, payload: dict) -> None:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        try:
            encoded = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(encoded, str):
                raise ValueError("missing frame data")
            frame = base64.b64decode(encoded, validate=True)
            if not frame or len(frame) > MAX_STREAM_FRAME_BYTES:
                raise ValueError("invalid frame size")
            dimensions = _jpeg_dimensions(frame)
            # Drop frames captured before the requested viewport took effect.
            # Chromium still needs the ACK so the next frame can arrive.
            if dimensions is not None and dimensions != (self._viewport["width"], self._viewport["height"]):
                return
            with self._frame_lock:
                self._latest_frame = frame
        except Exception:
            self._emit({"type": "error", "code": "BROWSER_FRAME_INVALID"})
        finally:
            if isinstance(session_id, int) and not isinstance(session_id, bool) and binding.cdp is not None:
                try:
                    # ACK every top-level Page.screencastFrame so Chromium can
                    # immediately deliver the next continuous frame.
                    binding.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception:
                    self._emit({"type": "error", "code": "BROWSER_STREAM_UNAVAILABLE"})

    def _emit_page(self, worker, live, page_id: str | None, *, force: bool = False,
                   include_window: bool = False) -> dict:
        if self._dialogs:
            return {}
        page_id = self._page_id or live.primary_page_id
        event = _page_event(worker, live, page_id, include_window=include_window)
        fingerprint = (event.get("page_id"), event.get("page_revision"), event.get("url"),
                       event.get("title"), tuple((item["page_id"], item["page_revision"], item["url"], item["title"], item["selected"]) for item in event.get("tabs", [])))
        now = time.monotonic()
        if force or fingerprint != self._last_page_event:
            self._emit(event)
            self._last_page_event = fingerprint
            self._last_page_event_at = now
        elif now - self._last_page_event_at >= PAGE_EVENT_INTERVAL_SECONDS:
            self._last_page_event_at = now
        return event

    def _emit_page_from_runtime(self) -> None:
        if self._closed:
            return
        try:
            self.runtime._call(
                self.engine, self.session_id,
                lambda worker, live: self._emit_page(worker, live, self._page_id, force=True, include_window=self.native),
            )
        except BrowserError:
            return

    def _on_page_closed(self, worker, live, page_id: str) -> None:
        self._detach_page(page_id, stop_screencast=True)
        self._drop_native_page_probe(page_id)
        self._dialogs = {key: value for key, value in self._dialogs.items() if value.page_id != page_id}
        self._drop_page_choosers(page_id)
        self._downloads = {key: value for key, value in self._downloads.items() if value.page_id != page_id}
        if self._page_id == page_id:
            replacement = live.primary_page_id or next(iter(live.pages), None)
            if replacement:
                try:
                    self._activate_page(worker, live, replacement, emit=False)
                    self._emit_page(worker, live, replacement, force=True, include_window=self.native)
                    return
                except BrowserError:
                    pass
            self._page_id = None
            with self._frame_lock:
                self._latest_frame = None
        self._emit_page(worker, live, live.primary_page_id, force=True)

    def _drop_page_choosers(self, page_id: str) -> None:
        self._file_choosers = {key: value for key, value in self._file_choosers.items() if value.page_id != page_id}

    def _detach_context(self, live) -> None:
        if self._context_handler is not None:
            _remove_listener(live.context, "page", self._context_handler)
            self._context_handler = None

    def _detach_page(self, page_id: str, *, stop_screencast: bool) -> None:
        binding = self._bindings.pop(page_id, None)
        if binding is None:
            return
        if binding.cdp is not None:
            if stop_screencast and binding.screencast_started:
                try:
                    binding.cdp.send("Page.stopScreencast")
                except Exception:
                    pass
            binding.screencast_started = False
            try:
                binding.cdp.detach()
            except Exception:
                pass
            binding.cdp = None
        for name, handler in binding.listeners:
            _remove_listener(binding.page, name, handler)
        binding.listeners.clear()

    def _emit(self, event: dict) -> None:
        try:
            self.events.put_nowait(event)
        except Full:
            self._event_overflow = True


def _require_fields(event: dict, allowed: set[str]) -> None:
    if set(event) != allowed:
        raise BrowserError("BROWSER_INPUT_INVALID", 400)


def _finite_number(value, code: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserError(code, 400)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise BrowserError(code, 400)
    return number


def _modifiers(value) -> int:
    if isinstance(value, bool):
        raise BrowserError("BROWSER_INPUT_INVALID", 400)
    if isinstance(value, int):
        if 0 <= value <= 15:
            return value
        raise BrowserError("BROWSER_INPUT_INVALID", 400)
    if not isinstance(value, list) or len(value) > 4:
        raise BrowserError("BROWSER_INPUT_INVALID", 400)
    bits = 0
    for item in value:
        if not isinstance(item, str) or item.lower() not in _INPUT_MODIFIERS:
            raise BrowserError("BROWSER_INPUT_INVALID", 400)
        bits |= _INPUT_MODIFIERS[item.lower()]
    return bits


def _validate_viewport(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"width", "height", "device_scale_factor"}:
        raise BrowserError("BROWSER_VIEWPORT_INVALID", 400)
    width = value.get("width", 1280)
    height = value.get("height", 800)
    scale = value.get("device_scale_factor", 1)
    if isinstance(width, bool) or not isinstance(width, int) or not MIN_VIEWPORT_WIDTH <= width <= MAX_VIEWPORT_WIDTH:
        raise BrowserError("BROWSER_VIEWPORT_INVALID", 400)
    if isinstance(height, bool) or not isinstance(height, int) or not MIN_VIEWPORT_HEIGHT <= height <= MAX_VIEWPORT_HEIGHT:
        raise BrowserError("BROWSER_VIEWPORT_INVALID", 400)
    scale = _finite_number(scale, "BROWSER_VIEWPORT_INVALID", minimum=0.5, maximum=3)
    if width * height > 8_000_000:
        raise BrowserError("BROWSER_VIEWPORT_INVALID", 400)
    return {"width": width, "height": height, "device_scale_factor": scale}


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width and height from a JPEG SOF marker without decoding pixels."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    index = 2
    length = len(data)
    while index + 9 <= length:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xDA:
            return None
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if segment < 7:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        index += 2 + segment
    return None



def _validate_file_payloads(files: list[dict]) -> list[dict]:
    if not isinstance(files, list) or len(files) > MAX_FILES_PER_CHOOSER:
        raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
    result = []
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"name", "mimeType", "buffer"}:
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
        name = item["name"]
        mime_type = item["mimeType"]
        buffer = item["buffer"]
        if not isinstance(name, str) or not _SAFE_FILENAME.fullmatch(name) or name in {".", ".."}:
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
        if not isinstance(mime_type, str) or not _SAFE_MIME_TYPE.fullmatch(mime_type):
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
        if not isinstance(buffer, (bytes, bytearray)):
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 400)
        size = len(buffer)
        if size > MAX_FILE_BYTES:
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 413)
        total += size
        if total > MAX_TOTAL_FILE_BYTES:
            raise BrowserError("BROWSER_FILE_CHOOSER_INVALID", 413)
        result.append({"name": name, "mimeType": mime_type, "buffer": bytes(buffer)})
    return result


def _safe_download_name(value) -> str:
    if not isinstance(value, str):
        return "download"
    name = value.replace("/", "_").replace("\\", "_").replace(":", "_").strip()
    name = "".join(char for char in name if char >= " " and char != "\x7f")[:255]
    return name or "download"


def _find_state(live, page):
    for state in live.pages.values():
        try:
            if state.page == page and not page.is_closed():
                return state
        except Exception:
            continue
    return None


def _require_page_state(worker, live, page_id: str | None):
    if page_id is not None and (not isinstance(page_id, str) or not page_id):
        raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409)
    identifier = page_id or live.primary_page_id
    state = live.pages.get(identifier) if identifier else None
    if state is None:
        raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409)
    try:
        if state.page.is_closed():
            raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409)
    except BrowserError:
        raise
    except Exception as exc:
        raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409) from exc
    return state


def _tab_metadata(worker, live) -> list[dict]:
    result = []
    for page_id, state in list(live.pages.items()):
        try:
            if state.page.is_closed():
                continue
            snapshot = worker._snapshot(live, page_id, include_text=False)
        except Exception:
            continue
        result.append({
            "page_id": page_id,
            "page_revision": snapshot["page_revision"],
            "url": snapshot.get("restore_url") or snapshot["url"],
            "title": snapshot.get("restore_title") or snapshot["title"],
            "runtime_url": snapshot["url"],
            "restore_required": bool(snapshot.get("restore_required")),
            "selected": page_id == live.primary_page_id,
        })
    return result


def _tabs_result(worker, live, active_page_id: str | None) -> dict:
    active = None
    if active_page_id and active_page_id in live.pages:
        active = worker._snapshot(live, active_page_id, include_text=False)
    return {"active_page_id": active_page_id, "page": active, "tabs": _tab_metadata(worker, live),
            "restore_required": bool(active and active.get("restore_required")),
            "restore_url": active.get("restore_url") if active else None}


def _page_event(worker, live, page_id: str | None, *, include_window: bool = False) -> dict:
    if page_id is None:
        page_id = live.primary_page_id
    if page_id is None:
        return {"type": "page", "page_id": None, "page_revision": None, "url": "", "title": "", "tabs": _tab_metadata(worker, live)}
    state = _require_page_state(worker, live, page_id)
    snapshot = worker._snapshot(live, page_id, include_text=False)
    result = {"type": "page", "page_id": snapshot["page_id"],
              "page_revision": snapshot["page_revision"], "url": snapshot["url"],
              "title": snapshot["title"], "tabs": _tab_metadata(worker, live),
              "restore_required": bool(snapshot.get("restore_required")), "restore_url": snapshot.get("restore_url")}
    if include_window:
        result["native_window"] = _native_window_metadata(worker, live, state)
    return result


def _native_window_metadata(worker, live, state) -> dict:
    unavailable = {"handle": None, "pid": None, "window_id": None, "bounds": None, "association": "unsupported"}
    if os.name != "nt":
        return unavailable

    page_cdp = None
    browser_cdp = None
    try:
        page_cdp = live.context.new_cdp_session(state.page)
        page_target = page_cdp.send("Target.getTargetInfo")
        target_info = page_target.get("targetInfo", {})
        target_id = target_info.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            return {**unavailable, "association": "target_unavailable"}

        browser = worker.browser
        browser_cdp = browser.new_browser_cdp_session()
        window = browser_cdp.send("Browser.getWindowForTarget", {"targetId": target_id})
        window_id = window.get("windowId")
        bounds = window.get("bounds")
        process_result = browser_cdp.send("SystemInfo.getProcessInfo")
        browser_processes = [item for item in process_result.get("processInfo", []) if item.get("type") == "browser"]
        if len(browser_processes) != 1:
            return {"handle": None, "pid": None, "window_id": window_id,
                    "bounds": _plain_bounds(bounds), "association": "browser_process_ambiguous"}
        pid = browser_processes[0].get("id")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return {"handle": None, "pid": None, "window_id": window_id,
                    "bounds": _plain_bounds(bounds), "association": "browser_process_unavailable"}

        candidates = _owned_windows_for_process(pid)
        matches = _match_window_bounds(candidates, bounds, state.page.title())
        if len(matches) != 1:
            return {"handle": None, "pid": pid, "window_id": window_id,
                    "bounds": _plain_bounds(bounds),
                    "association": "window_ambiguous" if matches else "window_not_found"}
        candidate = matches[0]
        return {"handle": candidate["hwnd"], "pid": pid, "window_id": window_id,
                "bounds": _plain_bounds(bounds), "association": "unique_process_window"}
    except Exception:
        return {**unavailable, "association": "native_lookup_unavailable"}
    finally:
        if browser_cdp is not None:
            try:
                browser_cdp.detach()
            except Exception:
                pass
        if page_cdp is not None:
            try:
                page_cdp.detach()
            except Exception:
                pass


def _plain_bounds(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    keys = ("left", "top", "width", "height", "windowState")
    return {key: value[key] for key in keys if key in value and isinstance(value[key], (str, int, float))}


def _owned_windows_for_process(pid: int) -> list[dict]:
    """Enumerate HWNDs, reading details only for the Playwright-owned PID."""
    if os.name != "nt":
        return []

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    candidates: list[dict] = []

    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL

    enum_proc_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value != pid:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(min(max(title_length + 1, 1), 2048))
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        candidates.append({
            "hwnd": int(ctypes.cast(hwnd, ctypes.c_void_p).value or 0),
            "pid": int(process_id.value),
            "left": int(rect.left),
            "top": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
            "title": title_buffer.value,
            "class_name": class_buffer.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
        })
        return True

    user32.EnumWindows.argtypes = (enum_proc_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    callback = enum_proc_type(visit)
    user32.EnumWindows(callback, 0)
    return candidates



def _match_window_bounds(candidates: list[dict], bounds, page_title: str) -> list[dict]:
    if not isinstance(bounds, dict):
        return []
    left = bounds.get("left")
    top = bounds.get("top")
    width = bounds.get("width")
    height = bounds.get("height")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (left, top, width, height)):
        return []

    result = []
    for window in candidates:
        if not isinstance(window, dict):
            continue
        try:
            differs = any(abs(float(window.get(field, float("inf"))) - float(expected)) > 16 for field, expected in (
                ("left", left), ("top", top), ("width", width), ("height", height),
            ))
        except (TypeError, ValueError):
            differs = True
        if differs:
            continue
        title = window.get("title", "")
        if page_title and isinstance(title, str) and title and not title.startswith(page_title):
            continue
        result.append(window)
    return result


def _remove_listener(target, event: str, handler) -> None:
    for method_name in ("remove_listener", "off"):
        method = getattr(target, method_name, None)
        if callable(method):
            try:
                method(event, handler)
            except Exception:
                pass
            return
