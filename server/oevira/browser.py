"""Isolated browser sessions backed by the existing workspace and Material store."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import ctypes
import hashlib
import json
import logging
import os
from queue import Empty, Queue
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
import uuid

from .acquisition import AcquisitionError, public_target
from .auth import utc_now
from .browser_transport import PinnedBrowserProxy


LOCKED_PLAYWRIGHT_VERSION = "1.64.0"
ENGINE_CHANNELS = {"chrome": "chrome", "edge": "msedge"}
BROWSER_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    membership_created_at TEXT NOT NULL,
    authorized_by_session_id TEXT NOT NULL REFERENCES sessions(id),
    work_id TEXT REFERENCES works(id),
    engine TEXT NOT NULL CHECK(engine IN ('chrome', 'edge')),
    status TEXT NOT NULL CHECK(status IN ('starting', 'open', 'reopening', 'suspended', 'closed', 'error')),
    version INTEGER NOT NULL CHECK(version > 0),
    page_revision INTEGER NOT NULL DEFAULT 0 CHECK(page_revision >= 0),
    control_mode TEXT NOT NULL DEFAULT 'user' CHECK(control_mode IN ('user', 'assistant')),
    control_generation INTEGER NOT NULL DEFAULT 1 CHECK(control_generation >= 1),
    current_url TEXT NOT NULL DEFAULT 'about:blank',
    current_title TEXT NOT NULL DEFAULT '',
    browser_version TEXT,
    state_ciphertext BLOB,
    state_protection TEXT,
    operation_id TEXT,
    operation_expires_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(id, workspace_id)
);
CREATE INDEX IF NOT EXISTS browser_sessions_owner
    ON browser_sessions(workspace_id, owner_user_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS browser_session_versions (
    session_id TEXT NOT NULL REFERENCES browser_sessions(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 0),
    operation TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(session_id, version)
);
CREATE TABLE IF NOT EXISTS browser_session_operations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES browser_sessions(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'unknown', 'failed')),
    result_json TEXT,
    preview_json TEXT,
    preview_expires_at TEXT,
    preview_discarded_at TEXT,
    material_id TEXT REFERENCES materials(id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS browser_session_operations_recent
    ON browser_session_operations(workspace_id, session_id, created_at DESC);
"""

MAX_CAPTURE_CHARACTERS = 200_000
MAX_ACTION_TEXT_CHARACTERS = 2_000
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
SESSION_LEASE_SECONDS = 120
NAVIGATION_TIMEOUT_MS = 30_000
BROWSER_COMMAND_TIMEOUT_SECONDS = 45
PAGE_READ_TIMEOUT_MS = 5_000
SESSION_CHECKPOINT_INTERVAL_SECONDS = 15
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 15
_SENSITIVE_QUERY_NAME = re.compile(r"(?:token|secret|password|passwd|credential|session|cookie|auth|code|nonce|signature|sig)$", re.I)
_ALLOWED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowDown", "ArrowLeft",
    "ArrowRight", "ArrowUp", "Home", "End", "PageDown", "PageUp", "Control+A", "Control+C",
    "Control+V", "Control+X", "Shift+Tab", "Alt+ArrowLeft", "Alt+ArrowRight",
}


class BrowserError(Exception):
    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


def install_browser_schema(store) -> None:
    """Install browser tables and add control fences to existing sessions once."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + BROWSER_SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(browser_sessions)")}
        if "control_mode" not in columns:
            connection.execute(
                "ALTER TABLE browser_sessions ADD COLUMN control_mode TEXT NOT NULL DEFAULT 'user' "
                "CHECK(control_mode IN ('user', 'assistant'))"
            )
        if "control_generation" not in columns:
            connection.execute(
                "ALTER TABLE browser_sessions ADD COLUMN control_generation INTEGER NOT NULL DEFAULT 1 "
                "CHECK(control_generation >= 1)"
            )
        operation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(browser_session_operations)")}
        for column in ("preview_json", "preview_expires_at", "preview_discarded_at"):
            if column not in operation_columns:
                connection.execute(f"ALTER TABLE browser_session_operations ADD COLUMN {column} TEXT")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _state_entropy(workspace_id: str, owner_user_id: str, session_id: str) -> bytes:
    material = f"oevira-browser-state-v1\0{workspace_id}\0{owner_user_id}\0{session_id}".encode("utf-8")
    return hashlib.sha256(material).digest()


class WindowsUserStateProtector:
    """Protect browser state with the current Windows user's DPAPI scope."""

    protection_name = "windows_dpapi_current_user_v1"

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt(data, entropy, protect=True)

    def unprotect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt(data, entropy, protect=False)

    @staticmethod
    def _crypt(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
        if os.name != "nt":
            raise BrowserError("BROWSER_PROTECTED_STORAGE_UNAVAILABLE", 503)

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(value: bytes):
            storage = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
            return DataBlob(len(value), ctypes.cast(storage, ctypes.POINTER(ctypes.c_ubyte))), storage

        input_blob, input_storage = blob(data)
        entropy_blob, entropy_storage = blob(entropy)
        output_blob = DataBlob()
        crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        if protect:
            function = crypt32.CryptProtectData
            function.argtypes = [
                ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.POINTER(DataBlob), ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob),
            ]
            function.restype = ctypes.c_bool
            succeeded = function(
                ctypes.byref(input_blob), "OEVIRA browser session state", ctypes.byref(entropy_blob),
                None, None, 0x1, ctypes.byref(output_blob),
            )
        else:
            description = ctypes.c_wchar_p()
            function = crypt32.CryptUnprotectData
            function.argtypes = [
                ctypes.POINTER(DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(DataBlob),
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob),
            ]
            function.restype = ctypes.c_bool
            succeeded = function(
                ctypes.byref(input_blob), ctypes.byref(description), ctypes.byref(entropy_blob),
                None, None, 0x1, ctypes.byref(output_blob),
            )
            if description:
                kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        # Retain the input buffers through the native call and output copy.
        _ = input_storage, entropy_storage
        if not succeeded:
            raise BrowserError("BROWSER_PROTECTED_STORAGE_FAILED", 503)
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def validate_browser_url(url: str) -> str:
    """Validate an external browser target through the existing public-source policy."""
    try:
        public_target(url)
    except AcquisitionError as exc:
        raise BrowserError("BROWSER_TARGET_DENIED", 403) from exc
    return url


def browser_safe_url(url: str) -> tuple[str, list[str]]:
    """Return a display/provenance URL without query values that look like secrets."""
    try:
        parsed = urlsplit(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise BrowserError("BROWSER_TARGET_INVALID", 400) from exc
    redacted = []
    safe_query = []
    for key, value in query:
        if _SENSITIVE_QUERY_NAME.search(key):
            redacted.append(key[:80])
            safe_query.append((key, "[redacted]"))
        else:
            safe_query.append((key, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query, doseq=True), "")), redacted


def browser_safe_title(title: str, url: str) -> str:
    """Do not persist a page title that reflects a secret navigation parameter."""
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if value and _SENSITIVE_QUERY_NAME.search(key):
            for representation in {value, quote(value, safe=""), quote_plus(value)}:
                title = title.replace(representation, "[redacted]")
    return title[:500]


def _serialize_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class _LivePage:
    page_id: str
    page: Any
    revision: int = 0
    last_text_hash: str | None = None
    metadata_session: Any | None = None
    dialog_open: bool = False
    loading_frames: set[str] = field(default_factory=set)
    loading_stop_generation: int = 0
    restore_url: str | None = None
    saved_title: str = ""


@dataclass
class _LiveSession:
    session_id: str
    workspace_id: str
    owner_user_id: str
    engine: str
    context: Any
    transport: PinnedBrowserProxy
    pages: dict[str, _LivePage] = field(default_factory=dict)
    primary_page_id: str | None = None
    blocked_requests: int = 0
    restore_url: str | None = None


class _EngineWorker:
    """Own one synchronous Playwright engine thread and its isolated contexts."""

    def __init__(self, engine: str, *, headless: bool, launch_args: list[str], authorize_request: Callable,
                 runtime_is_live: Callable, expired: Callable,
                 target_resolver: Callable[[str], tuple[str, str, int, str]], checkpointed: Callable | None = None):
        self.engine = engine
        self.channel = ENGINE_CHANNELS[engine]
        self.headless = headless
        self.launch_args = launch_args
        self.authorize_request = authorize_request
        self.runtime_is_live = runtime_is_live
        self.expired = expired
        self.target_resolver = target_resolver
        self.checkpointed = checkpointed
        self._last_checkpoint = time.monotonic()
        self.queue: Queue = Queue()
        self.ready = threading.Event()
        self.disconnected = threading.Event()
        self._stop_requested = threading.Event()
        self.start_error: BrowserError | None = None
        self.browser = None
        self.sessions: dict[str, _LiveSession] = {}
        self.thread = threading.Thread(target=self._run, name=f"oevira-browser-{engine}", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=60):
            raise BrowserError("BROWSER_ENGINE_START_TIMEOUT", 503)
        if self.start_error:
            raise self.start_error

    def _run(self) -> None:
        try:
            from importlib.metadata import version
            from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright
            if version("playwright") != LOCKED_PLAYWRIGHT_VERSION:
                raise BrowserError("BROWSER_RUNTIME_VERSION_MISMATCH", 503)
            self._playwright = sync_playwright().start()
            try:
                self.browser = self._playwright.chromium.launch(
                    channel=self.channel,
                    headless=self.headless,
                    ignore_default_args=["--mute-audio"],
                    args=[
                        "--disable-background-networking", "--disable-component-update",
                        "--disable-sync", "--no-first-run",
                        # A product-owned headed window stays off the desktop until
                        # the native host embeds it. Web uses the same isolated page.
                        *(["--window-position=-32000,-32000"] if os.name == "nt" and not self.headless else []),
                        *self.launch_args,
                    ],
                )
                self.browser.on("disconnected", lambda: self.disconnected.set())
            except Exception as exc:
                raise BrowserError("BROWSER_ENGINE_UNAVAILABLE", 503) from exc
            self._playwright_error = PlaywrightError
            self._playwright_timeout = PlaywrightTimeoutError
        except BrowserError as exc:
            self.start_error = exc
            self.ready.set()
            return
        except Exception as exc:
            self.start_error = BrowserError("BROWSER_RUNTIME_UNAVAILABLE", 503)
            self.ready.set()
            return
        self.ready.set()
        stop = False
        while not stop:
            try:
                task = self.queue.get(timeout=1.0)
            except Empty:
                self._reap_expired_sessions()
                self._checkpoint_sessions()
                continue
            if task is None:
                stop = True
                continue
            callback, future = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(callback())
            except Exception as exc:
                if isinstance(exc, BrowserError):
                    future.set_exception(exc)
                elif isinstance(exc, self._playwright_error):
                    future.set_exception(BrowserError(
                        "BROWSER_OPERATION_STATE_UNKNOWN" if self._stop_requested.is_set() else "BROWSER_OPERATION_FAILED", 503,
                    ))
                else:
                    future.set_exception(BrowserError("BROWSER_OPERATION_FAILED", 503))
            self._checkpoint_sessions()
        self._checkpoint_sessions(stopped=True)
        for session_id in list(self.sessions):
            self._close_context(session_id)
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

    def _checkpoint_sessions(self, *, stopped: bool = False) -> None:
        if self.checkpointed is None:
            return
        now = time.monotonic()
        if not stopped and now - self._last_checkpoint < SESSION_CHECKPOINT_INTERVAL_SECONDS:
            return
        self._last_checkpoint = now
        for session_id, live in list(self.sessions.items()):
            try:
                if self.disconnected.is_set() or not live.pages:
                    continue
                if not self.runtime_is_live(session_id):
                    continue
                if any(page.dialog_open for page in live.pages.values()):
                    continue
                snapshot = self._snapshot(live, include_text=False)
                payload = self._storage_payload(live)
                self.checkpointed(session_id, payload, snapshot, stopped=stopped)
            except Exception as exc:
                # Keep the last valid protected checkpoint. A failed checkpoint
                # must never replace it with an empty or partly read state.
                logging.getLogger(__name__).warning("Browser checkpoint failed: %s", type(exc).__name__)

    def _reap_expired_sessions(self) -> None:
        for session_id, live in list(self.sessions.items()):
            try:
                authorized = self.runtime_is_live(session_id)
            except Exception:
                authorized = False
            if authorized:
                continue
            try:
                state = self._storage_payload(live)
            except Exception:
                state = None
            self._close_context(session_id)
            try:
                self.expired(session_id, state)
            except Exception:
                pass

    def call(self, callback: Callable[[], Any], timeout: float = BROWSER_COMMAND_TIMEOUT_SECONDS,
             *, before_dispatch: Callable[[], None] | None = None):
        if self._stop_requested.is_set():
            raise BrowserError("BROWSER_RUNTIME_STOPPING", 409, {"dispatch_state": "not_started"})
        if not self.thread.is_alive():
            raise BrowserError("BROWSER_RUNTIME_UNAVAILABLE", 503)
        future = Future()
        def dispatch():
            if self._stop_requested.is_set():
                raise BrowserError("BROWSER_RUNTIME_STOPPING", 409, {"dispatch_state": "not_started"})
            if before_dispatch is not None:
                try:
                    before_dispatch()
                except BrowserError as exc:
                    exc.details = {**exc.details, "dispatch_state": "not_started"}
                    raise
            return callback()
        self.queue.put((dispatch, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            if future.cancel():
                raise BrowserError("BROWSER_OPERATION_NOT_STARTED", 504, {"dispatch_state": "not_started"}) from exc
            raise BrowserError("BROWSER_OPERATION_STATE_UNKNOWN", 504) from exc

    def _guard_http_request(self, live: _LiveSession, route) -> None:
        url = route.request.url
        scheme = urlsplit(url).scheme.lower()
        if scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        try:
            allowed = self.authorize_request(live.session_id, url)
        except Exception:
            allowed = False
        if not allowed:
            live.blocked_requests += 1
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _guard_websocket(self, live: _LiveSession, route) -> None:
        url = route.url
        parsed = urlsplit(url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme.lower())
        if scheme is None:
            live.blocked_requests += 1
            route.close(code=1008, reason="Unsupported browser connection target")
            return
        check_url = urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
        try:
            allowed = self.authorize_request(live.session_id, check_url)
        except Exception:
            allowed = False
        if not allowed:
            live.blocked_requests += 1
            route.close(code=1008, reason="Browser connection target denied")
            return
        route.connect_to_server()

    def _register_page(self, live: _LiveSession, page, *, page_id: str | None = None) -> _LivePage:
        for existing in list(live.pages.values()):
            if existing.page is page:
                if page_id is not None and existing.page_id != page_id:
                    previous_id = existing.page_id
                    live.pages.pop(previous_id)
                    existing.page_id = page_id
                    live.pages[page_id] = existing
                    if live.primary_page_id == previous_id:
                        live.primary_page_id = page_id
                return existing
        identifier = page_id or ("page_" + uuid.uuid4().hex)
        if identifier in live.pages:
            return live.pages[identifier]
        state = _LivePage(identifier, page)
        live.pages[identifier] = state
        if live.primary_page_id is None or page_id is None:
            live.primary_page_id = identifier

        def navigated(frame):
            if frame == page.main_frame:
                state.revision += 1
                state.last_text_hash = None
                if page.url != "about:blank":
                    state.restore_url = None
                    state.saved_title = ""

        page.on("framenavigated", navigated)
        page.on("close", lambda: self._page_closed(live, state.page_id))
        return state

    @staticmethod
    def _page_closed(live: _LiveSession, page_id: str) -> None:
        live.pages.pop(page_id, None)
        if live.primary_page_id == page_id:
            live.primary_page_id = next(iter(live.pages), None)

    def _page(self, live: _LiveSession, page_id: str | None = None) -> _LivePage:
        identifier = page_id or live.primary_page_id
        page = live.pages.get(identifier) if identifier else None
        if page is None or page.page.is_closed():
            raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409)
        return page

    def _snapshot(self, live: _LiveSession, page_id: str | None = None, *, include_text: bool = True) -> dict:
        state = self._page(live, page_id)
        try:
            raw_url = state.page.url or "about:blank"
            if include_text:
                title = state.page.title()[:500]
            else:
                if state.metadata_session is None:
                    state.metadata_session = live.context.new_cdp_session(state.page)
                    state.metadata_session.on("Page.javascriptDialogOpening", lambda _event: setattr(state, "dialog_open", True))
                    state.metadata_session.on("Page.javascriptDialogClosed", lambda _event: setattr(state, "dialog_open", False))
                    state.metadata_session.on("Page.frameStartedLoading", lambda event: state.loading_frames.add(event["frameId"]))
                    state.metadata_session.on("Page.frameStoppedLoading", lambda event: state.loading_frames.discard(event["frameId"]))
                    state.metadata_session.on("Page.frameDetached", lambda event: state.loading_frames.discard(event["frameId"]))
                    state.metadata_session.send("Page.enable")
                target = state.metadata_session.send("Target.getTargetInfo").get("targetInfo", {})
                title = str(target.get("title", ""))[:500]
            if include_text:
                text = state.page.locator("body").inner_text(timeout=PAGE_READ_TIMEOUT_MS) or ""
                truncated = len(text) > MAX_CAPTURE_CHARACTERS
                if truncated:
                    text = text[:MAX_CAPTURE_CHARACTERS]
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if state.last_text_hash is not None and digest != state.last_text_hash:
                    state.revision += 1
                state.last_text_hash = digest
            else:
                text, truncated, digest = "", False, state.last_text_hash
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("BROWSER_PAGE_READ_UNAVAILABLE", 503) from exc
        safe_url, redacted_query_keys = browser_safe_url(raw_url)
        title = browser_safe_title(title, raw_url)
        if not state.restore_url:
            state.saved_title = title
        return {
            "page_id": state.page_id,
            "url": safe_url,
            "title": title,
            "browser_version": self.browser.version,
            "page_revision": state.revision,
            "text": text,
            "text_sha256": digest,
            "text_characters": len(text),
            "text_truncated": truncated,
            "blocked_request_count": live.blocked_requests + live.transport.blocked_target_count,
            "scope": "main_frame_visible_body_text",
            "content_trust": "UNTRUSTED_WEB_CONTENT",
            "limitations": [
                "Page text covers the top-level document only; iframe, media, hidden or unloaded content is not claimed.",
                "Service workers are disabled so browser requests remain under the source-access policy.",
                "Authentication and source-use rights are not verified by a successful page read.",
            ],
            "redacted_query_keys": redacted_query_keys,
            "login_state": "NOT_VERIFIED",
            "restore_required": bool(state.restore_url),
            "restore_url": browser_safe_url(state.restore_url)[0] if state.restore_url else None,
            "restore_title": browser_safe_title(state.saved_title, state.restore_url) if state.restore_url else None,
        }

    def open_session(self, session_id: str, workspace_id: str, owner_user_id: str, storage_state: dict | None) -> dict:
        def operation():
            existing = self.sessions.get(session_id)
            if existing:
                if not existing.pages:
                    page = existing.context.new_page()
                    self._register_page(existing, page)
                return {
                    "snapshot": self._snapshot(existing, include_text=False),
                    "storage_payload": self._storage_payload(existing),
                }
            live = self._new_live_session(session_id, workspace_id, owner_user_id, storage_state)
            try:
                state = self._page(live)
                result = {
                    "snapshot": self._snapshot(live, state.page_id, include_text=False),
                    "storage_payload": self._storage_payload(live),
                }
            except Exception:
                self._close_live_context(live)
                raise
            self.sessions[session_id] = live
            return result

        return self.call(operation)

    def reopen_session(self, session_id: str, workspace_id: str, owner_user_id: str, payload: dict | None) -> dict:
        storage_state = payload.get("storage_state") if isinstance(payload, dict) else None
        saved_revision = payload.get("page_revision", 0) if isinstance(payload, dict) else 0
        restore_url = payload.get("restore_url") if isinstance(payload, dict) else None
        if not restore_url and isinstance(payload, dict):
            pages = payload.get("pages", [])
            if isinstance(pages, list) and pages:
                first = pages[0]
                restore_url = first.get("url") if isinstance(first, dict) else None
        saved_pages = payload.get("pages", []) if isinstance(payload, dict) else []
        if not isinstance(saved_pages, list):
            raise BrowserError("BROWSER_SESSION_STATE_UNAVAILABLE", 409)
        page_ids = set()
        for saved in saved_pages:
            if (not isinstance(saved, dict) or not isinstance(saved.get("page_id"), str)
                    or not 1 <= len(saved["page_id"]) <= 80 or saved["page_id"] in page_ids
                    or not isinstance(saved.get("url"), str)
                    or type(saved.get("revision", 0)) is not int or saved.get("revision", 0) < 0):
                raise BrowserError("BROWSER_SESSION_STATE_UNAVAILABLE", 409)
            page_ids.add(saved["page_id"])

        def restore_tabs(live):
            initial = self._page(live)
            if saved_pages:
                for index, saved in enumerate(saved_pages):
                    page = initial.page if index == 0 else live.context.new_page()
                    state = self._register_page(live, page, page_id=saved["page_id"])
                    state.revision = saved.get("revision", 0) + 1
                    state.restore_url = saved["url"] if saved["url"] != "about:blank" else None
                    state.saved_title = str(saved.get("title") or "")[:500]
                active_id = payload.get("active_page_id")
                live.primary_page_id = active_id if active_id in page_ids else saved_pages[0]["page_id"]
                self._page(live).page.bring_to_front()
            else:
                initial.revision = max(initial.revision, int(saved_revision or 0)) + 1
                initial.restore_url = restore_url if isinstance(restore_url, str) and restore_url != "about:blank" else None
            live.restore_url = self._page(live).restore_url

        def operation():
            if session_id in self.sessions:
                live = self.sessions[session_id]
                if not live.pages:
                    page = live.context.new_page()
                    self._register_page(live, page)
                    restore_tabs(live)
                return {"snapshot": self._snapshot(live, include_text=False), "storage_payload": self._storage_payload(live)}
            live = self._new_live_session(session_id, workspace_id, owner_user_id, storage_state)
            try:
                restore_tabs(live)
                state = self._page(live)
                result = {
                    "snapshot": self._snapshot(live, state.page_id, include_text=False),
                    "storage_payload": self._storage_payload(live),
                }
            except Exception:
                self._close_live_context(live)
                raise
            self.sessions[session_id] = live
            return result

        return self.call(operation, timeout=NAVIGATION_TIMEOUT_MS / 1000.0 * 8)

    def _new_live_session(self, session_id: str, workspace_id: str, owner_user_id: str,
                          storage_state: dict | None) -> _LiveSession:
        transport = PinnedBrowserProxy(self.target_resolver)
        context = None
        try:
            context = self.browser.new_context(
                storage_state=storage_state,
                accept_downloads=True,
                service_workers="block",
                **({"no_viewport": True} if os.name == "nt" and not self.headless
                   else {"viewport": {"width": 1280, "height": 800}}),
                proxy=transport.playwright_settings,
            )
            live = _LiveSession(session_id, workspace_id, owner_user_id, self.engine, context, transport)
            context.route("**/*", lambda route: self._guard_http_request(live, route))
            context.route_web_socket("**/*", lambda route: self._guard_websocket(live, route))
            context.on("page", lambda page: self._register_page(live, page))
            self._register_page(live, context.new_page())
            return live
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            transport.close()
            raise

    @staticmethod
    def _close_live_context(live: _LiveSession) -> None:
        try:
            live.context.close()
        except Exception:
            pass
        try:
            live.transport.close()
        except Exception:
            pass

    def navigate(self, session_id: str, url: str, page_id: str | None = None, expected_page_revision: int | None = None,
                 *, before_dispatch: Callable[[], None] | None = None) -> dict:
        def operation():
            live = self.sessions.get(session_id)
            if live is None:
                raise BrowserError("BROWSER_SESSION_REOPEN_REQUIRED", 409)
            page_state = self._page(live, page_id)
            if expected_page_revision is not None and page_state.revision != expected_page_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": page_state.revision})
            self.authorize_request(session_id, url)
            outcome = "complete"
            stop_generation = page_state.loading_stop_generation
            deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
            try:
                page_state.page.goto(url, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
                outcome = self._wait_for_page_load(page_state, stop_generation, deadline)
            except self._playwright_timeout:
                outcome = "unknown"
                # Read the actual current page. Never blindly repeat a timed-out navigation.
            live.primary_page_id = page_state.page_id
            live.restore_url = page_state.page.url or None
            result = self._snapshot(live, page_state.page_id)
            result["operation_state"] = outcome
            return {"snapshot": result, "storage_payload": self._storage_payload(live)}

        return self.call(operation, timeout=(NAVIGATION_TIMEOUT_MS / 1000.0) + 10, before_dispatch=before_dispatch)

    def _wait_for_page_load(self, state: _LivePage, stop_generation: int, deadline: float) -> str:
        while state.loading_stop_generation == stop_generation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state.page.wait_for_load_state("domcontentloaded", timeout=min(250, remaining * 1000))
                return "complete"
            except self._playwright_timeout:
                continue
        return "unknown"

    def stop_loading(self, session_id: str, page_id: str, expected_page_revision: int,
                     before_dispatch: Callable[[], None]) -> dict:
        """Interrupt the selected load on Playwright's loop, not behind its goto."""
        live = self.sessions.get(session_id)
        state = live.pages.get(page_id) if live is not None else None
        if state is None or state.metadata_session is None:
            raise BrowserError("BROWSER_PAGE_UNAVAILABLE", 409)
        implementation = state.metadata_session._impl_obj

        async def interrupt():
            before_dispatch()
            if state.revision != expected_page_revision or live.primary_page_id != page_id:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
            was_loading = bool(state.loading_frames)
            await implementation.send("Page.stopLoading")
            state.loading_stop_generation += 1
            deadline = time.monotonic() + 2
            while state.loading_frames and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            return {"page_id": page_id, "page_revision": state.revision,
                    "loading_state": "unknown" if state.loading_frames else "stopped" if was_loading else "not_loading",
                    "previous_effects": "not_reversed"}

        future = asyncio.run_coroutine_threadsafe(interrupt(), implementation._loop)
        try:
            return future.result(5)
        except FutureTimeout as exc:
            raise BrowserError("BROWSER_OPERATION_STATE_UNKNOWN", 504) from exc

    def read_page(self, session_id: str, page_id: str | None = None,
                  *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self.call(lambda: self._snapshot(self._require_live(session_id), page_id), before_dispatch=before_dispatch)

    def act(self, session_id: str, action: dict, page_id: str | None = None, expected_page_revision: int | None = None,
            *, before_dispatch: Callable[[], None] | None = None) -> dict:
        def operation():
            live = self._require_live(session_id)
            state = self._page(live, page_id)
            if expected_page_revision is not None and state.revision != expected_page_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
            starting_revision = state.revision
            stop_generation = state.loading_stop_generation
            deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
            outcome = "complete"
            kind = action["type"]
            page = state.page
            if kind == "click":
                page.mouse.click(action["x"], action["y"])
            elif kind == "drag_select":
                page.mouse.move(action["x1"], action["y1"])
                page.mouse.down()
                page.mouse.move(action["x2"], action["y2"], steps=8)
                page.mouse.up()
            elif kind == "type":
                page.keyboard.insert_text(action["text"])
            elif kind == "press":
                page.keyboard.press(action["key"])
            elif kind == "scroll":
                page.mouse.wheel(action["delta_x"], action["delta_y"])
            elif kind in {"back", "forward", "reload"}:
                navigation = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[kind]
                try:
                    navigation(wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
                    outcome = self._wait_for_page_load(state, stop_generation, deadline)
                except self._playwright_timeout:
                    outcome = "unknown"
            else:
                raise BrowserError("BROWSER_ACTION_UNSUPPORTED", 400)
            state.last_text_hash = None
            live.primary_page_id = state.page_id
            result = self._snapshot(live, state.page_id)
            if state.revision == starting_revision:
                state.revision += 1
                result["page_revision"] = state.revision
            result["action_type"] = kind
            result["operation_state"] = outcome
            live.restore_url = state.page.url or live.restore_url
            return {"snapshot": result, "storage_payload": self._storage_payload(live)}

        return self.call(operation, timeout=(NAVIGATION_TIMEOUT_MS / 1000.0) + 10, before_dispatch=before_dispatch)

    def screenshot(self, session_id: str, page_id: str | None = None) -> tuple[bytes, dict]:
        def operation():
            live = self._require_live(session_id)
            state = self._page(live, page_id)
            image = state.page.screenshot(type="png", full_page=False, animations="disabled", timeout=PAGE_READ_TIMEOUT_MS)
            if not isinstance(image, bytes) or len(image) > MAX_SCREENSHOT_BYTES:
                raise BrowserError("BROWSER_FRAME_TOO_LARGE", 413)
            metadata = self._snapshot(live, state.page_id, include_text=False)
            return image, metadata

        return self.call(operation)

    def capture(self, session_id: str, scope: str, page_id: str | None = None, expected_page_revision: int | None = None,
                *, before_dispatch: Callable[[], None] | None = None) -> dict:
        def operation():
            live = self._require_live(session_id)
            result = self._snapshot(live, page_id)
            state = live.pages[result["page_id"]]
            if expected_page_revision is not None and state.revision != expected_page_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
            starting_revision = state.revision
            if scope == "selection":
                page_text = result["text"]
                page_text_sha256 = result["text_sha256"]
                selected = state.page.evaluate("() => (window.getSelection()?.toString() || '')")
                if not isinstance(selected, str) or not selected.strip():
                    raise BrowserError("BROWSER_SELECTION_EMPTY", 409)
                first = page_text.find(selected)
                match_count = page_text.count(selected)
                result["page_text_sha256"] = page_text_sha256
                result["selection_context"] = {
                    "frame_scope": "main_frame",
                    "quote_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                    "visible_text_match_count": match_count,
                    "start_offset": first if match_count == 1 else None,
                    "end_offset": first + len(selected) if match_count == 1 else None,
                    "location_state": "unique_visible_text_match" if match_count == 1 else "ambiguous_or_unresolved",
                }
                result["text"] = selected
                result["text_sha256"] = hashlib.sha256(selected.encode("utf-8")).hexdigest()
                result["scope"] = "user_selection_in_main_frame"
                result["text_characters"] = len(selected)
                result["text_truncated"] = False
            elif scope != "page":
                raise BrowserError("BROWSER_CAPTURE_SCOPE_INVALID", 400)
            else:
                result["page_text_sha256"] = result["text_sha256"]
                result["selection_context"] = None
            if not result["text"].strip():
                raise BrowserError("BROWSER_PAGE_TEXT_UNAVAILABLE", 409)
            if result["text_truncated"] and scope == "selection":
                raise BrowserError("BROWSER_CAPTURE_TOO_LARGE", 413)
            result["capture_scope"] = scope
            final = self._snapshot(live, state.page_id, include_text=True)
            if state.revision != starting_revision or final["page_revision"] != starting_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
            live.restore_url = state.page.url or live.restore_url
            return {"snapshot": result, "storage_payload": self._storage_payload(live)}

        return self.call(operation, before_dispatch=before_dispatch)

    def storage_payload(self, session_id: str) -> dict:
        return self.call(lambda: self._storage_payload(self._require_live(session_id)))

    def checkpoint(self, session_id: str, page_id: str | None = None, expected_page_revision: int | None = None) -> dict:
        def operation():
            live = self._require_live(session_id)
            snapshot = self._snapshot(live, page_id, include_text=False)
            if expected_page_revision is not None and snapshot["page_revision"] != expected_page_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": snapshot["page_revision"]})
            return {"snapshot": snapshot, "storage_payload": self._storage_payload(live)}

        return self.call(operation)

    def restore_page(self, session_id: str, page_id: str | None = None,
                     expected_page_revision: int | None = None, *, before_dispatch: Callable[[], None] | None = None) -> dict:
        def operation():
            live = self._require_live(session_id)
            state = self._page(live, page_id)
            target_url = state.restore_url or state.page.url
            if not isinstance(target_url, str) or not target_url.startswith(("http://", "https://")):
                raise BrowserError("BROWSER_RESTORE_TARGET_UNAVAILABLE", 409)
            if expected_page_revision is not None and state.revision != expected_page_revision:
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": state.revision})
            if not self.authorize_request(session_id, target_url):
                raise BrowserError("BROWSER_TARGET_DENIED", 403)
            outcome = "complete"
            stop_generation = state.loading_stop_generation
            deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
            try:
                state.page.goto(target_url, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
                outcome = self._wait_for_page_load(state, stop_generation, deadline)
            except self._playwright_timeout:
                outcome = "unknown"
            live.restore_url = state.page.url or live.restore_url
            result = self._snapshot(live, state.page_id)
            result["operation_state"] = outcome
            return {"snapshot": result, "storage_payload": self._storage_payload(live)}

        return self.call(operation, timeout=(NAVIGATION_TIMEOUT_MS / 1000.0) + 10, before_dispatch=before_dispatch)

    def suspend(self, session_id: str) -> dict:
        def operation():
            live = self._require_live(session_id)
            payload = self._storage_payload(live)
            snapshot = self._snapshot(live, include_text=False)
            self._close_context(session_id)
            return {"storage_payload": payload, "snapshot": snapshot}

        return self.call(operation)

    def close_session(self, session_id: str) -> None:
        self.call(lambda: self._close_context(session_id))

    def has_session(self, session_id: str) -> bool:
        live = self.sessions.get(session_id)
        return bool(not self._stop_requested.is_set() and self.thread.is_alive()
                    and not self.disconnected.is_set() and live is not None and live.pages)

    def shutdown(self) -> None:
        self._stop_requested.set()
        if self.thread.is_alive():
            self.queue.put(None)
            self.thread.join(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        if self.thread.is_alive():
            raise BrowserError("BROWSER_SHUTDOWN_UNCONFIRMED", 503)

    def _require_live(self, session_id: str) -> _LiveSession:
        live = self.sessions.get(session_id)
        if live is None:
            raise BrowserError("BROWSER_SESSION_REOPEN_REQUIRED", 409)
        return live

    def _storage_payload(self, live: _LiveSession) -> dict:
        try:
            state = live.context.storage_state(indexed_db=True)
        except TypeError as exc:
            raise BrowserError("BROWSER_STATE_VERSION_UNSUPPORTED", 503) from exc
        pages = []
        for page_state in live.pages.values():
            if page_state.page.is_closed():
                continue
            raw_url = page_state.page.url or "about:blank"
            pages.append({
                "page_id": page_state.page_id,
                "url": page_state.restore_url or raw_url,
                "title": page_state.saved_title,
                "revision": page_state.revision,
            })
        return {
            "storage_state": state,
            "pages": pages,
            "active_page_id": live.primary_page_id,
            "page_revision": max((item["revision"] for item in pages), default=0),
            "restore_url": next((item["url"] for item in pages
                                 if item["page_id"] == live.primary_page_id and item["url"].startswith(("https://", "http://"))), live.restore_url),
            "saved_at": utc_now(),
        }

    def _close_context(self, session_id: str) -> None:
        live = self.sessions.pop(session_id, None)
        if live is None:
            return
        self._close_live_context(live)


class BrowserRuntimeManager:
    """Lazy Playwright manager; every session owns its own browser process and non-persistent context."""

    def __init__(self, *, headless_resolver: Callable[[], bool], launch_args_resolver: Callable[[], list[str]] | None = None,
                 authorize_request: Callable,
                 runtime_is_live: Callable, on_runtime_expired: Callable,
                 target_resolver: Callable[[str], tuple[str, str, int, str]] | None = None,
                 on_runtime_checkpoint: Callable | None = None):
        self.headless_resolver = headless_resolver
        self.launch_args_resolver = launch_args_resolver or (lambda: [])
        self.authorize_request = authorize_request
        self.runtime_is_live = runtime_is_live
        self.on_runtime_expired = on_runtime_expired
        self.on_runtime_checkpoint = on_runtime_checkpoint
        self.target_resolver = target_resolver or public_target
        self._workers: dict[tuple[str, str], _EngineWorker] = {}
        self._lock = threading.RLock()
        self._shutdown_callbacks: list[Callable[[], None]] = []
        self.shutdown_requested = False

    def is_stopping(self, session_id: str) -> bool:
        with self._lock:
            return any(identifier == session_id and worker.thread.is_alive()
                       and (self.shutdown_requested or worker._stop_requested.is_set())
                       for (_engine, identifier), worker in self._workers.items())

    def add_shutdown_callback(self, callback: Callable[[], None]) -> None:
        self._shutdown_callbacks.append(callback)

    def _worker(self, engine: str, session_id: str) -> _EngineWorker:
        if engine not in ENGINE_CHANNELS:
            raise BrowserError("BROWSER_ENGINE_INVALID", 400)
        with self._lock:
            if self.shutdown_requested:
                raise BrowserError("BROWSER_RUNTIME_UNAVAILABLE", 503)
            worker = self._workers.get((engine, session_id))
            if worker is not None and (worker.disconnected.is_set() or not worker.thread.is_alive()):
                self._workers.pop((engine, session_id), None)
                worker.shutdown()
                worker = None
            if worker is not None and worker._stop_requested.is_set():
                raise BrowserError("BROWSER_RUNTIME_STOPPING", 409)
            if worker is None:
                worker = _EngineWorker(
                    engine, headless=bool(self.headless_resolver()),
                    launch_args=self._launch_args(),
                    authorize_request=self.authorize_request,
                    runtime_is_live=self.runtime_is_live,
                    expired=self.on_runtime_expired,
                    target_resolver=self.target_resolver,
                    checkpointed=self.on_runtime_checkpoint,
                )
                self._workers[(engine, session_id)] = worker
            return worker

    def _launch_args(self) -> list[str]:
        value = self.launch_args_resolver()
        if not isinstance(value, list) or len(value) > 12 or any(not isinstance(item, str) or len(item) > 300 for item in value):
            raise BrowserError("BROWSER_RUNTIME_CONFIG_INVALID", 500)
        return value

    def open(self, session_id: str, workspace_id: str, owner_user_id: str, engine: str, payload: dict | None = None) -> dict:
        worker = self._worker(engine, session_id)
        if payload and payload.get("pages"):
            return worker.reopen_session(session_id, workspace_id, owner_user_id, payload)
        return worker.open_session(
            session_id, workspace_id, owner_user_id,
            payload.get("storage_state") if payload else None,
        )

    def reopen(self, session_id: str, workspace_id: str, owner_user_id: str, engine: str, payload: dict | None) -> dict:
        return self._worker(engine, session_id).reopen_session(session_id, workspace_id, owner_user_id, payload)

    def navigate(self, engine: str, session_id: str, url: str, page_id: str | None = None,
                 expected_page_revision: int | None = None, *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self._worker(engine, session_id).navigate(session_id, url, page_id, expected_page_revision, before_dispatch=before_dispatch)

    def stop_loading(self, engine: str, session_id: str, page_id: str, expected_page_revision: int,
                     before_dispatch: Callable[[], None]) -> dict:
        with self._lock:
            worker = self._workers.get((engine, session_id))
        if worker is None:
            raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
        return worker.stop_loading(session_id, page_id, expected_page_revision, before_dispatch)

    def read_page(self, engine: str, session_id: str, page_id: str | None = None,
                  *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self._worker(engine, session_id).read_page(session_id, page_id, before_dispatch=before_dispatch)

    def act(self, engine: str, session_id: str, action: dict, page_id: str | None = None,
            expected_page_revision: int | None = None, *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self._worker(engine, session_id).act(session_id, action, page_id, expected_page_revision, before_dispatch=before_dispatch)

    def screenshot(self, engine: str, session_id: str, page_id: str | None = None) -> tuple[bytes, dict]:
        return self._worker(engine, session_id).screenshot(session_id, page_id)

    def restore_page(self, engine: str, session_id: str, page_id: str | None = None,
                     expected_page_revision: int | None = None, *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self._worker(engine, session_id).restore_page(session_id, page_id, expected_page_revision, before_dispatch=before_dispatch)

    def capture(self, engine: str, session_id: str, scope: str, page_id: str | None = None,
                expected_page_revision: int | None = None, *, before_dispatch: Callable[[], None] | None = None) -> dict:
        return self._worker(engine, session_id).capture(session_id, scope, page_id, expected_page_revision, before_dispatch=before_dispatch)

    def storage_payload(self, engine: str, session_id: str) -> dict:
        return self._worker(engine, session_id).storage_payload(session_id)

    def checkpoint(self, engine: str, session_id: str, page_id: str | None = None,
                   expected_page_revision: int | None = None) -> dict:
        return self._worker(engine, session_id).checkpoint(session_id, page_id, expected_page_revision)

    def suspend(self, engine: str, session_id: str) -> dict:
        return self._worker(engine, session_id).suspend(session_id)

    def close(self, engine: str, session_id: str) -> None:
        with self._lock:
            worker = self._workers.get((engine, session_id))
        if worker:
            worker.shutdown()
            with self._lock:
                if self._workers.get((engine, session_id)) is worker:
                    self._workers.pop((engine, session_id), None)

    def is_open(self, engine: str, session_id: str) -> bool:
        worker = self._workers.get((engine, session_id))
        if not worker:
            return False
        return not self.shutdown_requested and worker.has_session(session_id)

    def shutdown(self) -> None:
        self.shutdown_requested = True
        errors = []
        for callback in self._shutdown_callbacks:
            try:
                callback()
            except Exception as exc:
                errors.append(type(exc).__name__)
        with self._lock:
            workers = list(self._workers.items())
        for key, worker in workers:
            try:
                worker.shutdown()
            except BrowserError as exc:
                errors.append(exc.code)
            finally:
                with self._lock:
                    if not worker.thread.is_alive() and self._workers.get(key) is worker:
                        self._workers.pop(key, None)
        if errors:
            raise BrowserError("BROWSER_SHUTDOWN_UNCONFIRMED", 503, {"pending_sessions": len(self._workers), "cleanup_errors": errors})

