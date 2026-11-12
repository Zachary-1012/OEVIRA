"""Persistent browser history and bookmarks owned by the existing Store/Auth."""
from __future__ import annotations

import json
import sqlite3
import uuid
from functools import wraps
from urllib.parse import urlsplit

from flask import g, request

from .api import ApiError, _body, _optional_string, _require_write, _string, _success
from .auth import utc_now
from .browser import BrowserError, browser_safe_url
from .browser_api import _browser_error

MAX_HISTORY_ITEMS = 1000
DEFAULT_HISTORY_PAGE_SIZE = 100
MAX_BOOKMARK_TITLE_CHARACTERS = 200


def install_browser_history_schema(store) -> None:
    """Idempotently install metadata tables in the selected product database."""
    with store.transaction() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS browser_history (
                   id TEXT PRIMARY KEY,
                   workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                   user_id TEXT NOT NULL REFERENCES users(id),
                   url TEXT NOT NULL,
                   title TEXT NOT NULL,
                   redacted_query_keys_json TEXT NOT NULL DEFAULT '[]',
                   visited_at TEXT NOT NULL,
                   page_id TEXT NOT NULL,
                   browser_session_id TEXT NOT NULL,
                   UNIQUE(workspace_id, user_id, url)
               )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS browser_history_recent
               ON browser_history(workspace_id, user_id, visited_at DESC, id DESC)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS browser_bookmarks (
                   id TEXT PRIMARY KEY,
                   workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                   user_id TEXT NOT NULL REFERENCES users(id),
                   url TEXT NOT NULL,
                   title TEXT NOT NULL,
                   redacted_query_keys_json TEXT NOT NULL DEFAULT '[]',
                   added_at TEXT NOT NULL,
                   page_id TEXT NOT NULL,
                   browser_session_id TEXT NOT NULL,
                   version INTEGER NOT NULL CHECK(version > 0),
                   UNIQUE(workspace_id, user_id, url)
               )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS browser_bookmarks_recent
               ON browser_bookmarks(workspace_id, user_id, added_at DESC, id DESC)"""
        )


def _clean_title(value, fallback: str) -> str:
    if isinstance(value, str):
        cleaned = "".join(character for character in value[:1024] if character.isprintable()).strip()
        if cleaned:
            return cleaned[:MAX_BOOKMARK_TITLE_CHARACTERS]
    return fallback[:MAX_BOOKMARK_TITLE_CHARACTERS]


def _safe_page_metadata(snapshot: dict, *, title_override: str | None = None) -> dict | None:
    """Extract safe navigation metadata, never page body or a secret URL."""
    raw_url = snapshot.get("url")
    page_id = snapshot.get("page_id")
    if not isinstance(raw_url, str) or not raw_url or not isinstance(page_id, str) or not page_id:
        return None

    try:
        safe_url, computed_redactions = browser_safe_url(raw_url)
        parsed = urlsplit(safe_url)
    except (BrowserError, ValueError) as exc:
        raise BrowserError("BROWSER_TARGET_INVALID", 400) from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or len(safe_url) > 2048
    ):
        return None

    provided_redactions = snapshot.get("redacted_query_keys")
    redacted_keys: list[str] = []
    if isinstance(provided_redactions, list):
        for item in provided_redactions:
            if isinstance(item, str) and item and item not in redacted_keys:
                redacted_keys.append(item[:80])
    for item in computed_redactions:
        if item not in redacted_keys:
            redacted_keys.append(item[:80])

    title = _clean_title(title_override or snapshot.get("title"), safe_url)
    return {
        "url": safe_url,
        "title": title,
        "redacted_query_keys": redacted_keys[:64],
        "page_id": page_id[:80],
    }


def _row_json(row: sqlite3.Row) -> dict:
    try:
        redacted_query_keys = json.loads(row["redacted_query_keys_json"] or "[]")
    except (TypeError, ValueError):
        redacted_query_keys = []
    if not isinstance(redacted_query_keys, list):
        redacted_query_keys = []
    columns = set(row.keys())
    result = {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "redacted_query_keys": redacted_query_keys,
        "page_id": row["page_id"],
        "session_id": row["browser_session_id"],
    }
    if "visited_at" in columns:
        result["visited_at"] = row["visited_at"]
    if "added_at" in columns:
        result["added_at"] = row["added_at"]
    if "version" in columns:
        result["version"] = row["version"]
    return result


class BrowserHistoryService:
    """History and bookmark storage under the existing BrowserService owner."""

    def __init__(self, browser):
        self.browser = browser
        self.store = browser.store

    def _require_membership(self, identity: dict) -> None:
        with self.store.transaction(immediate=False) as connection:
            self.browser._membership(connection, identity)

    def _owned_open_session(self, connection, identity: dict, session_id: str):
        row = self.browser._owned_session(connection, identity, session_id)
        if row["status"] != "open":
            raise BrowserError("BROWSER_SESSION_NOT_OPEN", 409)
        return row

    def record_visit(self, identity: dict, session_id: str, snapshot: dict) -> dict:
        """Record actual server-side page metadata, deduplicated by safe URL."""
        if not isinstance(identity, dict) or not isinstance(snapshot, dict):
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        if not isinstance(session_id, str) or not session_id:
            raise BrowserError("BROWSER_SESSION_INPUT_INVALID", 400)
        metadata = _safe_page_metadata(snapshot)
        if metadata is None:
            return {"recorded": False, "reason": "url_or_page_unavailable"}

        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        visited_at = utc_now()
        redactions_json = json.dumps(
            metadata["redacted_query_keys"], ensure_ascii=False, separators=(",", ":")
        )
        with self.store.transaction() as connection:
            self._owned_open_session(connection, identity, session_id)
            existing = connection.execute(
                """SELECT id FROM browser_history
                   WHERE workspace_id = ? AND user_id = ? AND url = ?""",
                (workspace_id, user_id, metadata["url"]),
            ).fetchone()
            if existing is None:
                history_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO browser_history(
                           id, workspace_id, user_id, url, title, redacted_query_keys_json,
                           visited_at, page_id, browser_session_id
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        history_id,
                        workspace_id,
                        user_id,
                        metadata["url"],
                        metadata["title"],
                        redactions_json,
                        visited_at,
                        metadata["page_id"],
                        session_id,
                    ),
                )
                deduplicated = False
            else:
                history_id = existing["id"]
                connection.execute(
                    """UPDATE browser_history
                       SET title = ?, redacted_query_keys_json = ?, visited_at = ?,
                           page_id = ?, browser_session_id = ?
                       WHERE id = ? AND workspace_id = ? AND user_id = ?""",
                    (
                        metadata["title"],
                        redactions_json,
                        visited_at,
                        metadata["page_id"],
                        session_id,
                        history_id,
                        workspace_id,
                        user_id,
                    ),
                )
                deduplicated = True

            connection.execute(
                """DELETE FROM browser_history
                   WHERE workspace_id = ? AND user_id = ? AND id NOT IN (
                       SELECT id FROM browser_history
                       WHERE workspace_id = ? AND user_id = ?
                       ORDER BY visited_at DESC, rowid DESC LIMIT ?
                   )""",
                (workspace_id, user_id, workspace_id, user_id, MAX_HISTORY_ITEMS),
            )
            saved = connection.execute(
                "SELECT * FROM browser_history WHERE id = ? AND workspace_id = ? AND user_id = ?",
                (history_id, workspace_id, user_id),
            ).fetchone()
        return {"recorded": True, "deduplicated": deduplicated, "item": _row_json(saved)}

    def list_history(self, identity: dict, limit: int = DEFAULT_HISTORY_PAGE_SIZE) -> list[dict]:
        self._require_membership(identity)
        rows = self.store.rows(
            """SELECT * FROM browser_history
               WHERE workspace_id = ? AND user_id = ?
               ORDER BY visited_at DESC, rowid DESC LIMIT ?""",
            (identity["workspace"]["id"], identity["user"]["id"], limit),
        )
        return [_row_json(row) for row in rows]

    def clear_history(self, identity: dict) -> dict:
        with self.store.transaction() as connection:
            self.browser._membership(connection, identity)
            result = connection.execute(
                "DELETE FROM browser_history WHERE workspace_id = ? AND user_id = ?",
                (identity["workspace"]["id"], identity["user"]["id"]),
            )
            return {"cleared": result.rowcount}

    def list_bookmarks(self, identity: dict) -> list[dict]:
        self._require_membership(identity)
        rows = self.store.rows(
            """SELECT * FROM browser_bookmarks
               WHERE workspace_id = ? AND user_id = ?
               ORDER BY added_at DESC, rowid DESC""",
            (identity["workspace"]["id"], identity["user"]["id"]),
        )
        return [_row_json(row) for row in rows]

    def bookmark_current_page(
        self,
        identity: dict,
        session_id: str,
        page_id: str,
        title_override: str | None = None,
    ) -> dict:
        if not isinstance(page_id, str) or not page_id or len(page_id) > 80:
            raise BrowserError("BROWSER_PAGE_READ_UNAVAILABLE", 400)
        snapshot = self.browser.read_page(identity, session_id, {"page_id": page_id})
        if snapshot.get("page_id") != page_id:
            raise BrowserError("BROWSER_PAGE_CHANGED", 409)
        metadata = _safe_page_metadata(snapshot, title_override=title_override)
        if metadata is None:
            raise BrowserError("BROWSER_TARGET_INVALID", 400)

        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        added_at = utc_now()
        redactions_json = json.dumps(
            metadata["redacted_query_keys"], ensure_ascii=False, separators=(",", ":")
        )
        with self.store.transaction() as connection:
            current_session = self._owned_open_session(connection, identity, session_id)
            if (
                current_session["page_revision"] != snapshot.get("page_revision")
                or current_session["current_url"] != snapshot["url"]
            ):
                raise BrowserError(
                    "BROWSER_PAGE_CHANGED", 409,
                    {"current_page_revision": current_session["page_revision"]},
                )
            existing = connection.execute(
                """SELECT * FROM browser_bookmarks
                   WHERE workspace_id = ? AND user_id = ? AND url = ?""",
                (workspace_id, user_id, metadata["url"]),
            ).fetchone()
            if existing is not None:
                return {"deduplicated": True, "item": _row_json(existing)}

            bookmark_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO browser_bookmarks(
                       id, workspace_id, user_id, url, title, redacted_query_keys_json,
                       added_at, page_id, browser_session_id, version
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    bookmark_id,
                    workspace_id,
                    user_id,
                    metadata["url"],
                    metadata["title"],
                    redactions_json,
                    added_at,
                    metadata["page_id"],
                    session_id,
                ),
            )
            saved = connection.execute(
                "SELECT * FROM browser_bookmarks WHERE id = ? AND workspace_id = ? AND user_id = ?",
                (bookmark_id, workspace_id, user_id),
            ).fetchone()
        return {"deduplicated": False, "item": _row_json(saved)}

    def remove_bookmark(self, identity: dict, bookmark_id: str, base_version: int) -> dict:
        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        with self.store.transaction() as connection:
            self.browser._membership(connection, identity)
            row = connection.execute(
                """SELECT version FROM browser_bookmarks
                   WHERE id = ? AND workspace_id = ? AND user_id = ?""",
                (bookmark_id, workspace_id, user_id),
            ).fetchone()
            if row is None:
                raise BrowserError("BROWSER_BOOKMARK_NOT_FOUND", 404)
            if row["version"] != base_version:
                raise ApiError(
                    "BROWSER_BOOKMARK_VERSION_CONFLICT",
                    "This bookmark changed. Refresh the bookmark list before removing it.",
                    409,
                    {"current_version": row["version"]},
                )
            deleted = connection.execute(
                """DELETE FROM browser_bookmarks
                   WHERE id = ? AND workspace_id = ? AND user_id = ? AND version = ?""",
                (bookmark_id, workspace_id, user_id, base_version),
            )
            if deleted.rowcount != 1:
                raise ApiError(
                    "BROWSER_BOOKMARK_VERSION_CONFLICT",
                    "This bookmark changed. Refresh the bookmark list before removing it.",
                    409,
                )
        return {"deleted": True, "id": bookmark_id, "version": base_version}


def register_browser_history_routes(app) -> None:
    """Register history/bookmark routes under BrowserService and the shared Store."""
    if "oevira.browser_history" in app.extensions:
        return
    browser = app.extensions["oevira.browser"]
    install_browser_history_schema(browser.store)
    service = BrowserHistoryService(browser)
    app.extensions["oevira.browser_history"] = service

    def checked(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BrowserError as exc:
                raise _browser_error(exc) from exc
        return wrapped

    @app.get("/api/browser-history")
    @checked
    def browser_history_list():
        identity = g.identity
        raw_limit = request.args.get("limit")
        if raw_limit is None:
            limit = DEFAULT_HISTORY_PAGE_SIZE
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError) as exc:
                raise ApiError("INVALID_INPUT", "limit must be an integer", 400) from exc
            if not 1 <= limit <= MAX_HISTORY_ITEMS:
                raise ApiError(
                    "INVALID_INPUT", f"limit must be between 1 and {MAX_HISTORY_ITEMS}", 400
                )
        return _success({"items": service.list_history(identity, limit), "limit": limit})

    @app.post("/api/browser-history/clear")
    @checked
    def browser_history_clear():
        data = _body()
        if data:
            raise ApiError("INVALID_INPUT", "Clear history does not accept resource IDs", 400)
        return _success(service.clear_history(_require_write()))

    @app.get("/api/browser-bookmarks")
    @checked
    def browser_bookmarks_list():
        return _success({"items": service.list_bookmarks(g.identity)})

    @app.post("/api/browser-sessions/<session_id>/bookmark")
    @checked
    def browser_bookmark_current_page(session_id):
        identity = _require_write()
        data = _body()
        if set(data) - {"page_id", "title"} or "page_id" not in data:
            raise ApiError("INVALID_INPUT", "Bookmark requires the current page_id and optional title", 400)
        page_id = _string(data.get("page_id"), "page_id", maximum=80)
        title = _optional_string(data.get("title"), "title", maximum=MAX_BOOKMARK_TITLE_CHARACTERS)
        result = service.bookmark_current_page(identity, session_id, page_id, title)
        return _success(result, 200 if result["deduplicated"] else 201)

    @app.delete("/api/browser-bookmarks/<bookmark_id>")
    @checked
    def browser_bookmark_remove(bookmark_id):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Removing a bookmark requires its base_version", 400)
        base_version = data.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
        if not bookmark_id or len(bookmark_id) > 80:
            raise ApiError("INVALID_INPUT", "bookmark_id is invalid", 400)
        return _success(service.remove_bookmark(identity, bookmark_id, base_version))
