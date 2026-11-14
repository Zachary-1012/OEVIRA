"""Authenticated native and WebRTC views of the existing product browser sessions."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from flask import g, request, send_file

from .api import ApiError, _body, _idempotency_key, _material_data, _parse_material, _require_write, _resource_path, _success
from .auth import utc_now
from .browser import BrowserError, browser_safe_url
from .browser_api import _base_version, _browser_error
from .browser_host_runtime import BrowserHostRuntime
from .browser_host_webrtc import BrowserWebRTC


@dataclass
class BrowserView:
    id: str
    session_id: str
    identity: dict
    stream: object
    native: bool
    last_seen: float
    history_marker: tuple | None = None
    initial_control_generation: int = 1


class BrowserHostService:
    """Presentation leases reuse BrowserService's identity and operation authority."""

    def __init__(self, app):
        self.app = app
        self.browser = app.extensions["oevira.browser"]
        self.store = self.browser.store
        self.runtime = BrowserHostRuntime(self.browser.runtime)
        self.views: dict[str, BrowserView] = {}
        self._lock = threading.RLock()
        self._webrtc = None
        self._stopped = threading.Event()
        self._reaper = threading.Thread(target=self._reap, name="oevira-browser-host-lifecycle", daemon=True)
        self._reaper.start()
        self.browser.runtime.add_shutdown_callback(self.shutdown)

    def _session(self, identity, session_id, *, writable=False):
        if self.browser.runtime.shutdown_requested or self.browser.runtime.is_stopping(session_id):
            raise BrowserError("BROWSER_RUNTIME_STOPPING", 409)
        with self.store.transaction(immediate=False) as connection:
            row = self.browser._owned_session(connection, identity, session_id)
            if writable and self.browser._membership(connection, identity)["role"] == "viewer":
                raise BrowserError("WRITE_FORBIDDEN", 403)
            if row["status"] != "open":
                raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
            return dict(row)

    def _authorized(self, view: BrowserView) -> bool:
        try:
            self._session(view.identity, view.session_id, writable=True)
            return self.views.get(view.id) is view and not view.stream.closed
        except Exception:
            return False

    def _view(self, identity, session_id, host_id):
        self._session(identity, session_id, writable=True)
        with self._lock:
            view = self.views.get(host_id)
            if (view is None or view.session_id != session_id
                    or view.identity["session_id"] != identity["session_id"]
                    or view.identity["user"]["id"] != identity["user"]["id"]):
                raise BrowserError("BROWSER_HOST_NOT_FOUND", 404)
            view.last_seen = time.monotonic()
            return view

    def current_attachment(self, identity, session_id):
        self._session(identity, session_id, writable=True)
        with self._lock:
            view = next((view for view in self.views.values() if view.session_id == session_id), None)
            if view is None:
                return {"attached": False}
            if view.identity["session_id"] != identity["session_id"] or view.identity["user"]["id"] != identity["user"]["id"]:
                raise BrowserError("BROWSER_HOST_NOT_FOUND", 404)
            return {"attached": True, "host_id": view.id, "transport": "native_window" if view.native else "webrtc",
                    "state": "closing" if view.stream.closed else "active"}

    def attach(self, identity, session_id, data, *, native=False):
        allowed = {"page_id", "viewport", "replace_host_id"} if native else {"page_id", "viewport", "replace_host_id", "type", "sdp"}
        if set(data) - allowed or (not native and data.get("type") != "offer"):
            raise BrowserError("BROWSER_HOST_OFFER_INVALID", 400)
        if native and os.name != "nt":
            raise BrowserError("BROWSER_NATIVE_HOST_UNAVAILABLE", 503)
        row = self._session(identity, session_id, writable=True)
        if row["operation_id"]:
            raise BrowserError("BROWSER_SESSION_BUSY", 409)
        with self._lock:
            previous = next((view for view in self.views.values() if view.session_id == session_id), None)
            if previous is not None:
                if data.get("replace_host_id") != previous.id:
                    raise BrowserError("BROWSER_HOST_ALREADY_ATTACHED", 409)
                self._view(identity, session_id, previous.id)
                self.close(previous.id)
            stream = self.runtime.start_stream(row["engine"], session_id, data.get("page_id"), data.get("viewport", {}), native=native)
            host_id = uuid.uuid4().hex
            view = BrowserView(host_id, session_id, identity, stream, native, time.monotonic())
            view.initial_control_generation = row.get("control_generation", 1)
            stream.input_authorizer = lambda event: self._authorize_input(view, event)
            self.views[host_id] = view
        try:
            if native:
                details = self.runtime.native_session(row["engine"], session_id, data.get("page_id"))
                return {"host_id": host_id, **details, **self._control_state(view)}
            with self._lock:
                if self._webrtc is None:
                    self._webrtc = BrowserWebRTC()
            answer = self._webrtc.offer(host_id, stream, data.get("sdp"),
                                        lambda: self._authorized(view), lambda: self._forget(host_id),
                                        on_page=lambda snapshot: self._record_visit(view, snapshot),
                                        control_state=lambda: self._control_state(view))
            return {**answer, "page_id": stream.initial_state()["page_id"], **self._control_state(view)}
        except BaseException:
            self.close(host_id)
            raise

    def _forget(self, host_id):
        with self._lock:
            self.views.pop(host_id, None)

    def close(self, host_id):
        with self._lock:
            view = self.views.pop(host_id, None)
        if view is None:
            return
        # Release the existing stream before acknowledging detach/replacement.
        # ICE teardown may continue asynchronously; it cannot retain the page lease.
        view.stream.close()
        if not view.native and self._webrtc is not None:
            self._webrtc.close(host_id)

    def _reap(self):
        while not self._stopped.wait(0.25):
            with self._lock:
                views = list(self.views.values())
            for view in views:
                # The WebRTC peer reports revoked authority before closing its
                # channel. Do not race that receipt by closing it from here.
                if view.native and (not self._authorized(view) or time.monotonic() - view.last_seen > 30):
                    self.close(view.id)
                    continue
                if view.native:
                    try:
                        view.stream.pump()
                    except BrowserError:
                        self.close(view.id)

    def shutdown(self):
        self._stopped.set()
        self._reaper.join(5)
        for host_id in list(self.views):
            self.close(host_id)
        if self._webrtc is not None:
            self._webrtc.shutdown()

    def heartbeat(self, identity, session_id, host_id):
        view = self._view(identity, session_id, host_id)
        snapshot = view.stream.initial_state()
        events = view.stream.drain_events()
        failure = self._record_visit(view, snapshot)
        if failure:
            events.append(failure)
        return {"host_id": host_id, **snapshot, **self._control_state(view), "events": events}

    def _control_state(self, view):
        return self.browser.get_control_state(view.identity, view.session_id)

    def _authorize_input(self, view, event):
        self._view(view.identity, view.session_id, view.id)
        generation = event.get("control_generation", view.initial_control_generation)
        if event.get("type") == "resize":
            return self._control_state(view)
        if type(generation) is not int or generation < 1:
            raise BrowserError("BROWSER_CONTROL_GENERATION_CONFLICT", 400)
        return self.browser.authorize_host_input(view.identity, view.session_id, generation)

    def _record_visit(self, view, snapshot):
        marker = (snapshot.get("page_id"), snapshot.get("url"), snapshot.get("title"))
        if marker == view.history_marker:
            return None
        try:
            self.app.extensions["oevira.browser_history"].record_visit(view.identity, view.session_id, snapshot)
            view.history_marker = marker
        except Exception:
            return {"type": "error", "code": "BROWSER_HISTORY_SAVE_FAILED"}
        return None

    def tab_operation(self, identity, session_id, data, key):
        if set(data) - {"action", "page_id", "base_version"} or data.get("action") not in {"new", "select", "close"}:
            raise BrowserError("BROWSER_TAB_INPUT_INVALID", 400)
        version = _base_version(data.get("base_version"))
        operation_id, row, replay = self.browser._reserve(identity, session_id, "tab", key, data, version)
        if replay is not None:
            return replay
        try:
            result = self.runtime.tabs(row["engine"], session_id, data["action"], data.get("page_id"))
            payload = self.browser.runtime.storage_payload(row["engine"], session_id)
            return self.browser._complete_operation(identity, row, operation_id, "tab", snapshot=result.get("page") or {},
                                                    storage_payload=payload, result=result)
        except BrowserError as exc:
            self.browser._fail_operation(identity, row, operation_id, exc.code, uncertain=True)
            raise

    def upload(self, identity, session_id, host_id, chooser_id, uploads):
        view = self._view(identity, session_id, host_id)
        maximum = self.app.config["MAX_MATERIAL_BYTES"]
        total = 0
        files = []
        for upload in uploads:
            content = upload.stream.read(maximum - total + 1)
            total += len(content)
            if total > maximum or len(files) >= 20:
                raise BrowserError("BROWSER_UPLOAD_TOO_LARGE", 413)
            name = Path((upload.filename or "file").replace("\\", "/")).name
            if not name or name in {".", ".."} or any(ord(ch) < 32 for ch in name):
                raise BrowserError("BROWSER_UPLOAD_INVALID", 400)
            files.append({"name": name[:255], "mimeType": upload.mimetype or "application/octet-stream", "buffer": content})
        # Revalidate after receiving bytes, before an external page receives them.
        self._view(identity, session_id, host_id)
        view.stream.set_files(chooser_id, files)
        return {"file_count": len(files), "size_bytes": total}

    def download(self, identity, session_id, host_id, download_id):
        view = self._view(identity, session_id, host_id)
        result = view.stream.download(download_id)
        self._view(identity, session_id, host_id)
        return result

    def save_download(self, identity, session_id, host_id, download_id, key):
        row = self._session(identity, session_id, writable=True)
        payload = {"download_id": download_id, "host_id": host_id}
        operation_id, row, replay = self.browser._reserve(identity, session_id, "save_download", key, payload, row["version"])
        if replay is not None:
            material = self.store.row("SELECT * FROM materials WHERE id=? AND workspace_id=?", (replay["material_id"], row["workspace_id"]))
            with self.store.transaction(immediate=False) as connection:
                return _material_data(connection, material)
        material_id = str(uuid.uuid4())
        staged = None
        try:
            download = self.download(identity, session_id, host_id, download_id)
            source = Path(download["path"])
            maximum = self.app.config["MAX_MATERIAL_BYTES"]
            with source.open("rb") as handle:
                content = handle.read(maximum + 1)
            if len(content) > maximum:
                raise BrowserError("BROWSER_DOWNLOAD_MATERIAL_TOO_LARGE", 413)
            filename = Path(download["name"].replace("\\", "/")).name[:255] or "download"
            parsed = _parse_material(filename, content)
            relative = f"objects/{row['workspace_id']}/materials/{material_id}/original"
            target = _resource_path(self.store, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(".browser-download-" + uuid.uuid4().hex)
            with staged.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            url, redacted = browser_safe_url(download.get("url") or row["current_url"])
            metadata = {"kind": "controlled_browser_download", "source_url": url,
                        "redacted_query_keys": redacted, "captured_at": utc_now(),
                        "content_trust": "UNTRUSTED_WEB_CONTENT", "source_admission": "NOT_ADMITTED"}
            with self.store.transaction() as connection:
                current = self.browser._owned_session(connection, identity, session_id)
                if current["version"] != row["version"] or current["operation_id"] != operation_id:
                    raise BrowserError("BROWSER_OPERATION_UNKNOWN", 409)
                if row["work_id"]:
                    work = connection.execute("SELECT status FROM works WHERE id=? AND workspace_id=?", (row["work_id"], row["workspace_id"])).fetchone()
                    if work is None or work["status"] != "active":
                        raise BrowserError("BROWSER_WORK_ARCHIVED", 409)
                os.replace(staged, target)
                staged = None
                connection.execute(
                    "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,reason,text,sha256,relative_path,size_bytes,source_url,source_json,source_text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (material_id, row["workspace_id"], row["owner_user_id"], row["work_id"], filename[:200], filename,
                     parsed["format"], parsed["status"], parsed["reason"], parsed["text"], hashlib.sha256(content).hexdigest(),
                     relative, len(content), url, json.dumps(metadata, ensure_ascii=False), parsed["text"], utc_now()),
                )
                for segment_id, label, text, start, end, kind in parsed["segments"]:
                    connection.execute("INSERT INTO material_segments(id,material_id,workspace_id,label,text,start_offset,end_offset,kind) VALUES(?,?,?,?,?,?,?,?)",
                                       (segment_id, material_id, row["workspace_id"], label, text, start, end, kind))
                if row["work_id"]:
                    connection.execute("INSERT OR IGNORE INTO work_materials(work_id,material_id,workspace_id,created_at) VALUES(?,?,?,?)",
                                       (row["work_id"], material_id, row["workspace_id"], utc_now()))
                # Bind a durable material receipt before completing the browser checkpoint.
                connection.execute("UPDATE browser_session_operations SET material_id=?, result_json=? WHERE id=?",
                                   (material_id, json.dumps({"material_id": material_id}), operation_id))
            snapshot = self.runtime.tabs(row["engine"], session_id, "list").get("page") or {}
            self.browser._complete_operation(identity, row, operation_id, "save_download", snapshot=snapshot,
                storage_payload=self.browser.runtime.storage_payload(row["engine"], session_id),
                result={"material_id": material_id}, material_id=material_id)
            with self.store.transaction(immediate=False) as connection:
                material = connection.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
                return _material_data(connection, material)
        except BaseException:
            if staged is not None:
                staged.unlink(missing_ok=True)
            self.browser._fail_operation(identity, row, operation_id, "BROWSER_DOWNLOAD_SAVE_FAILED", uncertain=True)
            raise


def register_browser_host_routes(app):
    if "oevira.browser_host" in app.extensions:
        return
    service = BrowserHostService(app)
    app.extensions["oevira.browser_host"] = service

    def checked(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BrowserError as exc:
                raise _browser_error(exc) from exc
        return wrapped

    @app.get("/api/browser-sessions/<session_id>/host")
    @checked
    def browser_host_current(session_id):
        return _success(service.current_attachment(g.identity, session_id))

    @app.post("/api/browser-sessions/<session_id>/host/offer")
    @checked
    def browser_host_offer(session_id):
        return _success(service.attach(_require_write(), session_id, _body()))

    @app.post("/api/browser-sessions/<session_id>/host/native")
    @checked
    def browser_host_native(session_id):
        return _success(service.attach(_require_write(), session_id, _body(), native=True))

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/heartbeat")
    @checked
    def browser_host_heartbeat(session_id, host_id):
        return _success(service.heartbeat(_require_write(), session_id, host_id))

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/detach")
    @checked
    def browser_host_detach(session_id, host_id):
        service._view(_require_write(), session_id, host_id)
        service.close(host_id)
        return _success({"detached": True})

    @app.get("/api/browser-sessions/<session_id>/host/tabs")
    @checked
    def browser_host_tabs(session_id):
        row = service._session(g.identity, session_id)
        return _success(service.runtime.tabs(row["engine"], session_id, "list"))

    @app.post("/api/browser-sessions/<session_id>/host/tabs")
    @checked
    def browser_host_change_tab(session_id):
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "An operation key is required", 400)
        return _success(service.tab_operation(_require_write(), session_id, _body(), key))

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/input")
    @checked
    def browser_host_input(session_id, host_id):
        view = service._view(_require_write(), session_id, host_id)
        return _success(view.stream.send_input(_body()) or {"accepted": True})

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/uploads")
    @checked
    def browser_host_upload(session_id, host_id):
        return _success(service.upload(_require_write(), session_id, host_id, request.form.get("chooser_id"), request.files.getlist("file")))

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/uploads/cancel")
    @checked
    def browser_host_cancel_upload(session_id, host_id):
        view = service._view(_require_write(), session_id, host_id)
        data = _body()
        if set(data) != {"chooser_id"} or not isinstance(data["chooser_id"], str):
            raise BrowserError("BROWSER_UPLOAD_INVALID", 400)
        return _success(view.stream.cancel_file_chooser(data["chooser_id"]))

    @app.get("/api/browser-sessions/<session_id>/host/<host_id>/downloads/<download_id>")
    @checked
    def browser_host_download(session_id, host_id, download_id):
        download = service.download(_require_write(), session_id, host_id, download_id)
        response = send_file(download["path"], as_attachment=True, download_name=download["name"], mimetype="application/octet-stream", conditional=False)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/browser-sessions/<session_id>/host/<host_id>/downloads/<download_id>/save")
    @checked
    def browser_host_save_download(session_id, host_id, download_id):
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "An operation key is required", 400)
        return _success(service.save_download(_require_write(), session_id, host_id, download_id, key), 201)
