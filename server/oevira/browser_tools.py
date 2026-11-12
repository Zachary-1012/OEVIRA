"""Browser, acquisition, and monitoring tools bound through Intelligence's registry."""
from __future__ import annotations

import hashlib
import json

from .api import ApiError, _job_data
from .auth import utc_now
from .browser import BrowserError, browser_safe_url
from .intelligence_base import IntelligenceError
from .intelligence_tools import OwnerToolBinding, ToolExecutionContext


def _work_id(context: ToolExecutionContext) -> str:
    if context.caller != "model":
        raise IntelligenceError("INTELLIGENCE_TOOL_CALLER_INVALID", 403)
    work_id = context.work_id
    if not isinstance(work_id, str) or not work_id:
        raise IntelligenceError("INTELLIGENCE_TOOL_SCOPE_INVALID", 409)
    return work_id


def _browser(app):
    browser = app.extensions.get("oevira.browser")
    if browser is None:
        raise IntelligenceError("BROWSER_RUNTIME_UNAVAILABLE", 503)
    return browser


def _intelligence(app):
    service = app.extensions.get("oevira.intelligence")
    if service is None or not hasattr(service, "tools"):
        raise IntelligenceError("INTELLIGENCE_OWNER_NOT_BOUND", 503)
    return service


def _assert_current_work_access(app, context: ToolExecutionContext, *, write: bool = False) -> str:
    work_id = _work_id(context)
    browser = _browser(app)
    identity = context.identity
    with browser.store.transaction(immediate=False) as connection:
        auth_session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (identity["session_id"], identity["workspace"]["id"], identity["user"]["id"]),
        ).fetchone()
        if (auth_session is None or auth_session["revoked_at"] is not None
                or auth_session["expires_at"] <= utc_now()):
            raise IntelligenceError("UNAUTHENTICATED", 401)
        from .collaboration import require_work_access

        require_work_access(connection, identity, work_id, write=write)
    return work_id


def _operation_key(context: ToolExecutionContext, tool_name: str, arguments: dict) -> str:
    scope = context.operation_scope or context.prepared_context_sha256
    if not isinstance(scope, str) or not scope:
        raise IntelligenceError("BROWSER_TOOL_OPERATION_SCOPE_REQUIRED", 409)
    try:
        encoded = json.dumps(
            {"scope": scope, "tool": tool_name, "arguments": arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntelligenceError("BROWSER_TOOL_INPUT_INVALID", 400) from exc
    return "assistant-" + hashlib.sha256(encoded).hexdigest()


def _current_session(browser, context: ToolExecutionContext, session_id: str,
                     base_version: int) -> dict:
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        raise IntelligenceError("BROWSER_SESSION_VERSION_INVALID", 400)
    work_id = _assert_current_work_access(browser.app, context)
    sessions = browser.list_work_sessions(context.identity, work_id, limit=50)
    current = next((item for item in sessions if item["id"] == session_id), None)
    if current is None:
        raise IntelligenceError("BROWSER_SESSION_NOT_FOUND", 404)
    if current["version"] != base_version:
        raise IntelligenceError("BROWSER_SESSION_VERSION_CONFLICT", 409, {
            "current_version": current["version"],
        })
    if current["control_mode"] != "assistant":
        raise IntelligenceError("BROWSER_CONTROL_MODE_CONFLICT", 409, {
            "control_mode": current["control_mode"],
            "control_generation": current["control_generation"],
        })
    if current["status"] != "open":
        raise IntelligenceError("BROWSER_SESSION_NOT_OPEN", 409, {"status": current["status"]})
    return current


def _read_assistant_context(browser, context: ToolExecutionContext, session_id: str,
                            session: dict, page_id: str | None, *, retain_preview: bool = False) -> dict:
    return browser.read_context(
        context.identity, session_id,
        {"base_version": session["version"], "page_id": page_id, "include_frames": True,
         "retain_preview": retain_preview},
        actor="assistant", expected_control_generation=session["control_generation"],
    )


def _reader_frame_summary(frame: dict) -> dict:
    summary = {
        key: frame.get(key)
        for key in (
            "frame_id", "parent_frame_id", "url", "redacted_query_keys", "url_state",
            "title", "status", "frame_sha256", "text_sha256", "bounds",
            "bounds_coordinate_space", "coverage",
        )
    }
    frame_url, frame_redacted_keys = browser_safe_url(
        frame.get("url") if isinstance(frame.get("url"), str) else "about:blank",
    )
    summary["url"] = frame_url
    summary["redacted_query_keys"] = sorted({
        *[key for key in frame.get("redacted_query_keys", []) if isinstance(key, str)],
        *frame_redacted_keys,
    })
    summary["blocks"] = [
        {key: block.get(key) for key in (
            "block_id", "frame_id", "tag", "role", "aria_label", "heading_level",
            "bounds", "truncated", "start_offset", "end_offset",
        )}
        for block in frame.get("blocks", []) if isinstance(block, dict)
    ]
    summary["links"] = [
        _reader_link_summary(link)
        for link in frame.get("links", []) if isinstance(link, dict)
    ]
    summary["controls"] = [
        {key: control.get(key) for key in (
            "control_id", "frame_id", "locator", "tag", "type", "role", "label",
            "disabled", "bounds", "coordinate_space", "frame_sha256", "page_revision",
            "snapshot_sha256",
        )}
        for control in frame.get("controls", []) if isinstance(control, dict)
    ]
    return summary


def _reader_link_summary(link: dict) -> dict:
    safe_url, redacted_query_keys = browser_safe_url(
        link.get("url") if isinstance(link.get("url"), str) else "about:blank",
    )
    summary = {key: link.get(key) for key in (
            "frame_id", "url", "redacted_query_keys", "url_state", "title", "text",
            "bounds", "coordinate_space", "frame_sha256", "page_revision", "snapshot_sha256",
        )}
    summary["url"] = safe_url
    summary["redacted_query_keys"] = sorted({
        *[key for key in link.get("redacted_query_keys", []) if isinstance(key, str)],
        *redacted_query_keys,
    })
    return summary


def _reader_context_summary(reader: dict, session: dict) -> dict:
    version = reader.get("snapshot_version")
    frames = reader.get("frames", [])
    main_frame = next((
        frame for frame in frames
        if isinstance(frame, dict) and frame.get("frame_id") == "main"
    ), {})
    raw_page_url = main_frame.get("url")
    if not isinstance(raw_page_url, str):
        raw_page_url = "about:blank"
    safe_page_url, page_query_keys = browser_safe_url(raw_page_url)
    page_title = main_frame.get("title")
    if not isinstance(page_title, str):
        page_title = ""
    redacted_query_keys = {
        key for key in [
            *reader.get("redacted_query_keys", []),
            *main_frame.get("redacted_query_keys", []),
            *page_query_keys,
        ] if isinstance(key, str)
    }
    return {
        "session_id": session["id"],
        "version": reader["version"],
        "page_id": reader["page_id"],
        "page_revision": reader["page_revision"],
        "url": safe_page_url,
        "title": page_title,
        "redacted_query_keys": sorted(redacted_query_keys),
        "reader_snapshot_sha256": version.get("sha256") if isinstance(version, dict) else None,
        "capture_ref": reader.get("capture_ref"),
        "scope": reader.get("scope"),
        "frames": [
            _reader_frame_summary(frame)
            for frame in frames if isinstance(frame, dict)
        ],
        "links": [
            _reader_link_summary(link)
            for link in reader.get("links", []) if isinstance(link, dict)
        ],
        "controls": [
            {key: control.get(key) for key in (
                "control_id", "frame_id", "locator", "tag", "type", "role", "label",
                "disabled", "bounds", "coordinate_space", "frame_sha256", "page_revision",
                "snapshot_sha256",
            )}
            for control in reader.get("controls", []) if isinstance(control, dict)
        ],
        "coverage": reader.get("coverage"),
        "content_trust": "UNTRUSTED_WEB_CONTENT",
        "limitations": reader.get("coverage", {}).get("limitations", [])
        if isinstance(reader.get("coverage"), dict) else [],
    }


def _browser_exception(function):
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BrowserError as exc:
            raise IntelligenceError(exc.code, exc.status, exc.details) from exc
    return wrapped


@_browser_exception
def _list_browser_sessions(app, arguments: dict, context: ToolExecutionContext) -> list[dict]:
    work_id = _assert_current_work_access(app, context)
    return _browser(app).list_work_sessions(
        context.identity, work_id, limit=arguments.get("limit", 30),
    )


@_browser_exception
def _read_browser_page(app, arguments: dict, context: ToolExecutionContext) -> dict:
    browser = _browser(app)
    session_id = arguments["browser_session_id"]
    session = _current_session(browser, context, session_id, arguments["base_version"])
    reader = _read_assistant_context(
        browser, context, session_id, session, arguments.get("page_id"),
        retain_preview=arguments.get("retain_preview", False),
    )
    summary = _reader_context_summary(reader, session)
    summary["body_available_via"] = "browser.page.capture"
    return summary


@_browser_exception
def _navigate_browser_page(app, arguments: dict, context: ToolExecutionContext) -> dict:
    browser = _browser(app)
    session_id = arguments["browser_session_id"]
    session = _current_session(browser, context, session_id, arguments["base_version"])
    payload = {
        "base_version": arguments["base_version"],
        "page_revision": session["page_revision"],
        "url": arguments["url"],
    }
    result = browser.navigate(
        context.identity, session_id, payload,
        _operation_key(context, "browser.page.navigate", arguments),
        actor="assistant", expected_control_generation=session["control_generation"],
    )
    return result


@_browser_exception
def _capture_browser_page(app, arguments: dict, context: ToolExecutionContext) -> dict:
    browser = _browser(app)
    intelligence = _intelligence(app)
    session_id = arguments["browser_session_id"]
    session = _current_session(browser, context, session_id, arguments["base_version"])
    if "capture_id" in arguments:
        reader = browser.get_context_preview(context.identity, session_id, arguments["capture_id"],
                                             field_paths=("text",), purpose="source_capture", action="derive")
        if arguments.get("page_id") is not None and arguments["page_id"] != reader["page_id"]:
            raise IntelligenceError("BROWSER_CAPTURE_INVALID", 400)
    else:
        reader = _read_assistant_context(browser, context, session_id, session, arguments.get("page_id"))
    frames = reader.get("frames", [])
    requested_frame_id = arguments.get("frame_id")
    if requested_frame_id is None:
        requested_frame_id = "main"
    if not isinstance(requested_frame_id, str) or not requested_frame_id:
        raise IntelligenceError("BROWSER_CAPTURE_INVALID", 400)
    frame = next((item for item in frames if isinstance(item, dict)
                  and item.get("frame_id") == requested_frame_id), None)
    if frame is None or frame.get("status") not in {"read", "empty", "partial"}:
        raise IntelligenceError("BROWSER_PAGE_READ_UNAVAILABLE", 409)
    fresh_frame_sha256 = frame.get("frame_sha256")
    if not isinstance(fresh_frame_sha256, str) or len(fresh_frame_sha256) != 64:
        raise IntelligenceError("BROWSER_PAGE_READ_UNAVAILABLE", 409)
    supplied_frame_sha256 = arguments.get("frame_sha256")
    if (supplied_frame_sha256 is not None
            and (not isinstance(supplied_frame_sha256, str)
                 or supplied_frame_sha256.lower() != fresh_frame_sha256.lower())):
        raise IntelligenceError("BROWSER_PAGE_CHANGED", 409, {
            "current_page_revision": reader["page_revision"],
            "current_frame_sha256": fresh_frame_sha256,
        })
    block_id = arguments.get("block_id")
    if block_id is not None and not any(
        isinstance(block, dict) and block.get("block_id") == block_id
        for block in frame.get("blocks", [])
    ):
        raise IntelligenceError("BROWSER_READ_INVALID", 400)
    scope = "reader_block" if block_id is not None else "reader_frame"
    capture_request = {
        "base_version": arguments["base_version"],
        "page_revision": reader["page_revision"],
        "page_id": reader["page_id"],
        "scope": scope,
        "frame_id": requested_frame_id,
        "frame_sha256": fresh_frame_sha256,
    }
    if block_id is not None:
        capture_request["block_id"] = block_id
    if "capture_id" in arguments:
        capture_request["capture_id"] = arguments["capture_id"]
    captured, http_status = browser.capture(
        context.identity, session_id, capture_request,
        _operation_key(context, "browser.page.capture", arguments),
        actor="assistant", expected_control_generation=session["control_generation"],
    )
    intelligence.tools._register_material(context, captured["material"])
    return {
        "browser": captured["browser"],
        "material": captured["material"],
        "capture_status": http_status,
        "content_trust": "UNTRUSTED_WEB_CONTENT",
        "source_use": "RECHECKED_BEFORE_NEXT_MODEL_INPUT",
    }


@_browser_exception
def _reconcile_browser_session(app, arguments: dict, context: ToolExecutionContext) -> dict:
    browser = _browser(app)
    session_id = arguments["browser_session_id"]
    session = _current_session(browser, context, session_id, arguments["base_version"])
    return browser.reconcile_session(
        context.identity, session_id, {"base_version": arguments["base_version"]},
        _operation_key(context, "browser.session.reconcile", arguments),
        actor="assistant", expected_control_generation=session["control_generation"],
    )


def _acquisition_enqueue(app, arguments: dict, context: ToolExecutionContext) -> dict:
    work_id = _assert_current_work_access(app, context, write=True)
    from .worker import create_url_acquisition_job

    key = _operation_key(context, "acquisition.enqueue_url", arguments)
    browser = _browser(app)
    job, _status = create_url_acquisition_job(
        browser.store, context.identity, {"url": arguments["url"], "work_id": work_id}, key, app=app,
    )
    return job


def _acquisition_job_read(app, arguments: dict, context: ToolExecutionContext) -> dict:
    work_id = _assert_current_work_access(app, context)
    browser = _browser(app)
    row = browser.store.row(
        "SELECT * FROM jobs WHERE id = ? AND workspace_id = ? AND user_id = ? AND work_id = ? AND kind = 'acquire_url'",
        (arguments["job_id"], context.identity["workspace"]["id"], context.identity["user"]["id"], work_id),
    )
    if row is None:
        raise IntelligenceError("JOB_NOT_FOUND", 404)
    result = _job_data(row)
    if not row["material_id"]:
        return result
    intelligence = _intelligence(app)
    material = intelligence.read_material(context.identity, row["material_id"])
    if material.get("work_id") != work_id:
        raise IntelligenceError("INTELLIGENCE_TOOL_SCOPE_CHANGED", 403)
    intelligence.tools._register_material(context, material)
    result["material"] = material
    return result


def _monitoring_list(app, arguments: dict, context: ToolExecutionContext) -> dict:
    work_id = _assert_current_work_access(app, context)
    store = _browser(app).store
    limit = arguments.get("limit", 30)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise IntelligenceError("INTELLIGENCE_TOOL_INPUT_INVALID", 400)
    from .collaboration import require_work_access
    from . import monitoring

    with store.transaction(immediate=False) as connection:
        require_work_access(connection, context.identity, work_id)
        rows = connection.execute(
            "SELECT * FROM monitor_rules WHERE workspace_id = ? AND work_id = ? "
            "ORDER BY updated_at DESC, id LIMIT ?",
            (context.identity["workspace"]["id"], work_id, limit),
        ).fetchall()
        for row in rows:
            monitoring._require_monitor_work_read(connection, context.identity, row)
    items = []
    for row in rows:
        data = monitoring._monitor_data(app, store, row, context.identity)
        items.append({
            "id": data["id"], "title": row["title"], "work_id": work_id, "status": data["status"],
            "version": data["version"], "source_count": data["source_count"],
            "signal_count": data["signal_count"], "last_check_at": data["last_check_at"],
            "last_success_at": data["last_success_at"],
        })
    return {"work_id": work_id, "monitors": items, "truncated": len(items) == limit}


def _monitoring_signals(app, arguments: dict, context: ToolExecutionContext) -> dict:
    work_id = _assert_current_work_access(app, context)
    store = _browser(app).store
    limit = arguments.get("limit", 30)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise IntelligenceError("INTELLIGENCE_TOOL_INPUT_INVALID", 400)
    from .collaboration import require_material_access, require_work_access
    from . import monitoring

    with store.transaction(immediate=False) as connection:
        require_work_access(connection, context.identity, work_id)
        monitor_rows = connection.execute(
            "SELECT * FROM monitor_rules WHERE workspace_id = ? AND work_id = ?",
            (context.identity["workspace"]["id"], work_id),
        ).fetchall()
        for monitor in monitor_rows:
            monitoring._require_monitor_work_read(connection, context.identity, monitor)
        monitor_ids = [row["id"] for row in monitor_rows]
        monitors_by_id = {row["id"]: row for row in monitor_rows}
        if not monitor_ids:
            return {"work_id": work_id, "signals": [], "truncated": False}
        placeholders = ",".join("?" for _ in monitor_ids)
        rows = connection.execute(
            f"""SELECT DISTINCT s.* FROM monitor_signals s
                LEFT JOIN monitor_signal_works sw ON sw.signal_id = s.id
                WHERE s.workspace_id = ? AND s.monitor_id IN ({placeholders})
                  AND (sw.work_id IS NULL OR sw.work_id = ?)
                ORDER BY s.created_at DESC, s.id LIMIT ?""",
            (context.identity["workspace"]["id"], *monitor_ids, work_id, limit),
        ).fetchall()
        materials = {}
        visible_rows = []
        for row in rows:
            if not monitoring._monitor_signal_is_accessible(connection, context.identity, monitors_by_id[row["monitor_id"]], row):
                continue
            material_id = row["material_id"]
            if material_id:
                try:
                    require_material_access(connection, context.identity, material_id, target_work_id=work_id)
                except ApiError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                material = connection.execute(
                    "SELECT id, sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
                    (material_id, context.identity["workspace"]["id"]),
                ).fetchone()
                if material is None or material["status"] not in {"ready", "partial"}:
                    continue
                materials[material_id] = dict(material)
            visible_rows.append(row)

    signal_list = []
    for row in visible_rows:
        safe = monitoring._signal_data(store, row)
        source_url, _redacted = browser_safe_url(safe["url"] or "about:blank")
        signal_list.append({
            "id": safe["id"], "monitor_id": safe["monitor_id"],
            "work_id": work_id, "source_url": source_url,
            "material_id": safe["material_id"], "created_at": safe["created_at"],
            "captured_at": safe["captured_at"], "published_at": safe["published_at"],
            "published_at_origin": safe["published_at_origin"],
        })
    for material_id, material in materials.items():
        context.allowed_material_ids.add(material_id)
        context.source_refs.append({
            "kind": "material", "material_id": material_id,
            "material_sha256": material["sha256"], "status": material["status"],
        })
    return {"work_id": work_id, "signals": signal_list, "truncated": len(signal_list) == limit}


def create_browser_owner_tool_bindings(app) -> tuple[OwnerToolBinding, ...]:
    """Return bindings for the central IntelligenceToolRouter after BrowserService registration."""
    return (
        OwnerToolBinding(
            "browser.sessions.list", "Acquisition", frozenset(), frozenset({"limit"}),
            "read", lambda arguments, context: _list_browser_sessions(app, arguments, context),
        ),
        OwnerToolBinding(
            "browser.page.read", "Acquisition", frozenset({"browser_session_id", "base_version"}), frozenset({"page_id", "retain_preview"}),
            "read", lambda arguments, context: _read_browser_page(app, arguments, context),
        ),
        OwnerToolBinding(
            "browser.page.navigate", "Acquisition", frozenset({"browser_session_id", "base_version", "url"}), frozenset(),
            "read", lambda arguments, context: _navigate_browser_page(app, arguments, context),
        ),
        OwnerToolBinding(
            "browser.page.capture", "Acquisition", frozenset({"browser_session_id", "base_version"}),
            frozenset({"page_id", "frame_id", "frame_sha256", "block_id", "capture_id"}),
            "draft_write", lambda arguments, context: _capture_browser_page(app, arguments, context),
        ),
        OwnerToolBinding(
            "browser.session.reconcile", "Acquisition", frozenset({"browser_session_id", "base_version"}), frozenset(),
            "read", lambda arguments, context: _reconcile_browser_session(app, arguments, context),
        ),
        OwnerToolBinding(
            "acquisition.enqueue_url", "Acquisition", frozenset({"url"}), frozenset(),
            "draft_write", lambda arguments, context: _acquisition_enqueue(app, arguments, context),
        ),
        OwnerToolBinding(
            "acquisition.job.read", "Acquisition", frozenset({"job_id"}), frozenset(),
            "read", lambda arguments, context: _acquisition_job_read(app, arguments, context),
        ),
        OwnerToolBinding(
            "monitoring.list", "Monitoring", frozenset(), frozenset({"limit"}),
            "read", lambda arguments, context: _monitoring_list(app, arguments, context),
        ),
        OwnerToolBinding(
            "monitoring.signals", "Monitoring", frozenset(), frozenset({"limit"}),
            "read", lambda arguments, context: _monitoring_signals(app, arguments, context),
        ),
    )
