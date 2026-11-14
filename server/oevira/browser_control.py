"""User-owned handoff between manual browser control and the assistant."""
from __future__ import annotations

from flask import g

from .api import _body, _require_write, _success


def register_browser_control_routes(app) -> None:
    if "browser_set_control" in app.view_functions:
        return
    browser = app.extensions.get("oevira.browser")
    if browser is None:
        raise RuntimeError("Browser control requires the existing BrowserService")

    from .browser_api import BrowserError, _browser_error

    @app.post("/api/browser-sessions/<session_id>/control")
    def browser_set_control(session_id: str):
        identity = g.identity
        data = _body()
        try:
            return _success(browser.set_control(identity, session_id, data))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.get("/api/browser-sessions/<session_id>/control")
    def browser_get_control(session_id: str):
        try:
            return _success(browser.get_control_state(g.identity, session_id))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/work")
    def browser_bind_work(session_id: str):
        try:
            return _success(browser.bind_work(_require_write(), session_id, _body()))
        except BrowserError as exc:
            raise _browser_error(exc)

    @app.post("/api/browser-sessions/<session_id>/stop-loading")
    def browser_stop_loading(session_id: str):
        try:
            return _success(browser.stop_loading(_require_write(), session_id, _body()))
        except BrowserError as exc:
            raise _browser_error(exc)
