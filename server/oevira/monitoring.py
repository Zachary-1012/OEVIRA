"""Workspace-scoped, user-enabled monitoring over the shared public URL reader.

This module owns monitor configuration and observation history, but deliberately
uses the existing ``jobs`` table, ``acquire_url`` worker path and ``materials``
records. ``tick_monitoring`` is a hook for the existing worker loop; it is not a
second worker or scheduler process.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import re
import sqlite3
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
import uuid

from flask import g, request

from .api import (
    ApiError,
    _body,
    _brief,
    _idempotency_key,
    _not_found,
    _optional_string,
    _require_write,
    _request_fingerprint,
    _string,
    _success,
    _work_data,
    _work_row,
    _resource_path,
)
from .auth import utc_now


MONITORING_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_rules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    work_id TEXT REFERENCES works(id),
    follow_source_links INTEGER NOT NULL DEFAULT 0 CHECK(follow_source_links IN (0, 1)),
    keywords_json TEXT NOT NULL DEFAULT '[]',
    exclude_keywords_json TEXT NOT NULL DEFAULT '[]',
    frequency_seconds INTEGER NOT NULL CHECK(frequency_seconds BETWEEN 60 AND 31536000),
    stop_at TEXT,
    per_run_limit INTEGER NOT NULL CHECK(per_run_limit BETWEEN 1 AND 100),
    status TEXT NOT NULL CHECK(status IN ('paused', 'enabled', 'stopped')),
    version INTEGER NOT NULL,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(id, workspace_id)
);
CREATE INDEX IF NOT EXISTS monitor_rules_due ON monitor_rules(status, next_run_at);
CREATE INDEX IF NOT EXISTS monitor_rules_workspace ON monitor_rules(workspace_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS monitor_versions (
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(monitor_id, version)
);
CREATE TABLE IF NOT EXISTS monitor_sources (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    url TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source_origin TEXT NOT NULL DEFAULT 'user' CHECK(source_origin IN ('user', 'discovered')),
    discovery_suppressed INTEGER NOT NULL DEFAULT 0 CHECK(discovery_suppressed IN (0, 1)),
    position INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'not_checked',
    status_reason TEXT,
    last_check_at TEXT,
    last_success_at TEXT,
    last_sha256 TEXT,
    last_material_id TEXT REFERENCES materials(id),
    last_etag TEXT,
    last_modified TEXT,
    last_config_version INTEGER,
    pending_job_id TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(monitor_id, url)
);
CREATE INDEX IF NOT EXISTS monitor_sources_active ON monitor_sources(monitor_id, active, position, id);
CREATE TABLE IF NOT EXISTS monitor_source_discoveries (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    parent_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
    child_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
    parent_material_id TEXT NOT NULL REFERENCES materials(id),
    config_version INTEGER NOT NULL,
    link_kind TEXT NOT NULL CHECK(link_kind IN ('page_link', 'declared_feed', 'robots_sitemap', 'feed_entry', 'next_page', 'sitemap_index', 'sitemap_url')),
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_native_id TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    UNIQUE(monitor_id, parent_material_id, url, link_kind, source_native_id, config_version)
);
CREATE INDEX IF NOT EXISTS monitor_source_discoveries_child
    ON monitor_source_discoveries(monitor_id, child_source_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS monitor_runs (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    config_version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('manual', 'scheduled')),
    trigger_key TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'partial', 'failed', 'cancelled')),
    source_count INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    not_covered_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(monitor_id, trigger_key)
);
CREATE INDEX IF NOT EXISTS monitor_runs_recent ON monitor_runs(monitor_id, created_at DESC);
CREATE TABLE IF NOT EXISTS monitor_run_sources (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES monitor_runs(id),
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_id TEXT NOT NULL REFERENCES monitor_sources(id),
    job_id TEXT REFERENCES jobs(id),
    config_version INTEGER NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('queued', 'running', 'completed', 'partial', 'failed', 'cancelled', 'not_covered')),
    outcome TEXT NOT NULL DEFAULT 'not_checked',
    status_reason TEXT,
    material_id TEXT REFERENCES materials(id),
    sha256 TEXT,
    captured_at TEXT,
    published_at TEXT,
    changed_from_previous INTEGER CHECK(changed_from_previous IN (0, 1) OR changed_from_previous IS NULL),
    included_matches_json TEXT NOT NULL DEFAULT '[]',
    excluded_matches_json TEXT NOT NULL DEFAULT '[]',
    suppress_signals INTEGER NOT NULL DEFAULT 0 CHECK(suppress_signals IN (0, 1)),
    suppress_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, source_id)
);
CREATE INDEX IF NOT EXISTS monitor_run_sources_pending ON monitor_run_sources(lifecycle_status, created_at);
CREATE TABLE IF NOT EXISTS monitor_observations (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_id TEXT NOT NULL REFERENCES monitor_sources(id),
    run_id TEXT NOT NULL REFERENCES monitor_runs(id),
    run_source_id TEXT NOT NULL UNIQUE REFERENCES monitor_run_sources(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    published_at TEXT,
    published_at_origin TEXT,
    content_status TEXT NOT NULL,
    changed_from_previous INTEGER CHECK(changed_from_previous IN (0, 1) OR changed_from_previous IS NULL),
    previous_sha256 TEXT,
    config_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS monitor_observations_recent ON monitor_observations(source_id, captured_at DESC);
CREATE TABLE IF NOT EXISTS monitor_signals (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_id TEXT NOT NULL REFERENCES monitor_sources(id),
    run_id TEXT NOT NULL REFERENCES monitor_runs(id),
    run_source_id TEXT NOT NULL REFERENCES monitor_run_sources(id),
    observation_id TEXT NOT NULL REFERENCES monitor_observations(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    config_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    matched_keywords_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    previous_sha256 TEXT,
    current_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_source_id, config_version)
);
CREATE INDEX IF NOT EXISTS monitor_signals_recent ON monitor_signals(monitor_id, created_at DESC);
CREATE TABLE IF NOT EXISTS monitor_signal_works (
    signal_id TEXT PRIMARY KEY REFERENCES monitor_signals(id),
    work_id TEXT NOT NULL REFERENCES works(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL
);
"""


def install_monitoring_schema(store) -> None:
    """Idempotently install monitoring tables and additive monitoring fields."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + MONITORING_SCHEMA)
        rule_columns = {row["name"] for row in connection.execute("PRAGMA table_info(monitor_rules)").fetchall()}
        if "work_id" not in rule_columns:
            connection.execute("ALTER TABLE monitor_rules ADD COLUMN work_id TEXT REFERENCES works(id)")
        if "follow_source_links" not in rule_columns:
            connection.execute("ALTER TABLE monitor_rules ADD COLUMN follow_source_links INTEGER NOT NULL DEFAULT 0 CHECK(follow_source_links IN (0, 1))")
        source_columns = {row["name"] for row in connection.execute("PRAGMA table_info(monitor_sources)").fetchall()}
        if "source_origin" not in source_columns:
            connection.execute("ALTER TABLE monitor_sources ADD COLUMN source_origin TEXT NOT NULL DEFAULT 'user' CHECK(source_origin IN ('user', 'discovered'))")
        if "discovery_suppressed" not in source_columns:
            connection.execute("ALTER TABLE monitor_sources ADD COLUMN discovery_suppressed INTEGER NOT NULL DEFAULT 0 CHECK(discovery_suppressed IN (0, 1))")
        discovery_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'monitor_source_discoveries'",
        ).fetchone()
        required_link_kinds = ("declared_feed", "robots_sitemap", "sitemap_index", "sitemap_url")
        if discovery_schema and not all(kind in (discovery_schema["sql"] or "") for kind in required_link_kinds):
            connection.execute(
                """CREATE TABLE monitor_source_discoveries_rebuild (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    parent_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
                    child_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
                    parent_material_id TEXT NOT NULL REFERENCES materials(id),
                    config_version INTEGER NOT NULL,
                    link_kind TEXT NOT NULL CHECK(link_kind IN ('page_link', 'declared_feed', 'robots_sitemap', 'feed_entry', 'next_page', 'sitemap_index', 'sitemap_url')),
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source_native_id TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    UNIQUE(monitor_id, parent_material_id, url, link_kind, source_native_id, config_version)
                )"""
            )
            connection.execute(
                """INSERT INTO monitor_source_discoveries_rebuild(
                       id, monitor_id, workspace_id, parent_source_id, child_source_id,
                       parent_material_id, config_version, link_kind, url, title, source_native_id, observed_at
                   ) SELECT id, monitor_id, workspace_id, parent_source_id, child_source_id,
                            parent_material_id, config_version, link_kind, url, title, source_native_id, observed_at
                     FROM monitor_source_discoveries"""
            )
            connection.execute("DROP TABLE monitor_source_discoveries")
            connection.execute("ALTER TABLE monitor_source_discoveries_rebuild RENAME TO monitor_source_discoveries")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS monitor_source_discoveries_child ON monitor_source_discoveries(monitor_id, child_source_id, observed_at DESC)"
            )
        work_indexes = connection.execute("PRAGMA index_list('monitor_signal_works')").fetchall()
        unique_work_index = False
        for index in work_indexes:
            if not index["unique"]:
                continue
            quoted_name = index["name"].replace('"', '""')
            indexed_columns = [row["name"] for row in connection.execute(f'PRAGMA index_info("{quoted_name}")').fetchall()]
            if indexed_columns == ["work_id"]:
                unique_work_index = True
                break
        if unique_work_index:
            connection.execute(
                """CREATE TABLE monitor_signal_works_rebuild (
                    signal_id TEXT PRIMARY KEY REFERENCES monitor_signals(id),
                    work_id TEXT NOT NULL REFERENCES works(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO monitor_signal_works_rebuild(signal_id, work_id, workspace_id, created_at)
                   SELECT signal_id, work_id, workspace_id, created_at FROM monitor_signal_works"""
            )
            connection.execute("DROP TABLE monitor_signal_works")
            connection.execute("ALTER TABLE monitor_signal_works_rebuild RENAME TO monitor_signal_works")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _now(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _capture_text(value: str, fallback: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return fallback
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (AttributeError, ValueError):
        return fallback


def _parse_stop_at(value, field: str = "stop_at") -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ApiError("INVALID_INPUT", f"{field} must be an ISO-8601 timestamp or null", 400)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", f"{field} must be an ISO-8601 timestamp or null", 400) from exc
    if parsed.tzinfo is None:
        raise ApiError("INVALID_INPUT", f"{field} must include a timezone", 400)
    return _time_text(parsed.astimezone(timezone.utc))


def _text_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise ApiError("INVALID_INPUT", f"{field} must be an array of at most 50 strings", 400)
    result = []
    seen = set()
    for item in value:
        normalized = _string(item, f"{field}[]", maximum=200)
        key = normalized.casefold()
        if key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _source_list(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ApiError("INVALID_INPUT", "sources must contain 1 to 100 public URLs", 400)
    result = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            url, label = item, ""
        elif isinstance(item, dict) and not (set(item) - {"url", "label"}):
            url = item.get("url")
            label = _optional_string(item.get("label"), "sources[].label", maximum=120) or ""
        else:
            raise ApiError("INVALID_INPUT", "Each source must be a URL or an object with url and label", 400)
        url = _string(url, "sources[].url", maximum=4096)
        if any(ord(character) < 32 for character in url):
            raise ApiError("INVALID_INPUT", "A source URL contains a control character", 400)
        try:
            parsed = urlsplit(url)
            port = parsed.port
            loopback_or_literal = False
            if parsed.hostname:
                host = parsed.hostname.lower().rstrip(".")
                try:
                    loopback_or_literal = not ipaddress.ip_address(host).is_global
                except ValueError:
                    loopback_or_literal = host == "localhost" or host.endswith(".localhost")
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "A source must be a valid public HTTP(S) URL", 400) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 80, 443}
            or loopback_or_literal
        ):
            raise ApiError("INVALID_INPUT", "A source must be a public HTTP(S) URL without credentials on a standard port", 400)
        if url in seen:
            raise ApiError("INVALID_INPUT", "Duplicate source URLs are not allowed", 400)
        seen.add(url)
        result.append({"url": url, "label": label})
    return result


def _monitorable_material_source(value) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "missing_url"
    try:
        query = parse_qsl(urlsplit(value).query, keep_blank_values=True)
    except ValueError:
        return None, "invalid_url"
    if any(_SENSITIVE_MONITOR_QUERY_NAME.search(name) for name, _ in query):
        return None, "redacted_query"
    try:
        validated = _source_list([value])
    except ApiError:
        return None, "invalid_url"
    parsed = urlsplit(validated[0]["url"])
    canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return canonical, None


def _config_from_row(connection, row: sqlite3.Row) -> dict:
    sources = connection.execute(
        "SELECT url, label FROM monitor_sources WHERE monitor_id = ? AND active = 1 ORDER BY position, id",
        (row["id"],),
    ).fetchall()
    return {
        "title": row["title"],
        "scope": row["scope"],
        "subject_id": row["subject_id"],
        "work_id": row["work_id"],
        "follow_source_links": bool(row["follow_source_links"]),
        "sources": [{"url": item["url"], "label": item["label"]} for item in sources],
        "keywords": json.loads(row["keywords_json"]),
        "exclude_keywords": json.loads(row["exclude_keywords_json"]),
        "frequency_seconds": row["frequency_seconds"],
        "stop_at": row["stop_at"],
        "per_run_limit": row["per_run_limit"],
    }


def _validated_config(value: dict, *, create: bool) -> dict:
    allowed = {"title", "scope", "subject_id", "work_id", "follow_source_links", "sources", "keywords", "exclude_keywords", "frequency_seconds", "stop_at", "per_run_limit"}
    unknown = set(value) - allowed
    if unknown:
        raise ApiError("INVALID_INPUT", "Monitor configuration contains an unsupported field", 400, {"fields": sorted(unknown)})
    config = {}
    if create or "title" in value:
        config["title"] = _string(value.get("title"), "title", maximum=200)
    if create or "scope" in value:
        config["scope"] = _optional_string(value.get("scope"), "scope", maximum=2000) or ""
    if create or "subject_id" in value:
        subject_id = value.get("subject_id")
        if subject_id is not None:
            subject_id = _string(subject_id, "subject_id", maximum=200)
        config["subject_id"] = subject_id
    if create or "work_id" in value:
        work_id = value.get("work_id")
        if work_id is not None:
            work_id = _string(work_id, "work_id", maximum=80)
        config["work_id"] = work_id
    if create or "follow_source_links" in value:
        follow_source_links = value.get("follow_source_links", False)
        if not isinstance(follow_source_links, bool):
            raise ApiError("INVALID_INPUT", "follow_source_links must be a boolean", 400)
        config["follow_source_links"] = follow_source_links
    if create or "sources" in value:
        config["sources"] = _source_list(value.get("sources"))
    if create or "keywords" in value:
        config["keywords"] = _text_list(value.get("keywords", []), "keywords")
    if create or "exclude_keywords" in value:
        config["exclude_keywords"] = _text_list(value.get("exclude_keywords", []), "exclude_keywords")
    if create or "frequency_seconds" in value:
        frequency = value.get("frequency_seconds")
        if isinstance(frequency, bool) or not isinstance(frequency, int) or not 60 <= frequency <= 31_536_000:
            raise ApiError("INVALID_INPUT", "frequency_seconds must be an integer from 60 to 31536000", 400)
        config["frequency_seconds"] = frequency
    if create or "stop_at" in value:
        config["stop_at"] = _parse_stop_at(value.get("stop_at"))
    if create or "per_run_limit" in value:
        limit = value.get("per_run_limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ApiError("INVALID_INPUT", "per_run_limit must be an integer from 1 to 100", 400)
        config["per_run_limit"] = limit
    return config


def _business_subject(connection, workspace_id: str, subject_id: str | None):
    if subject_id is None:
        return None
    return connection.execute(
        "SELECT id, workspace_id, kind, name, directory_state, identity_review_status, version FROM business_subjects WHERE id = ? AND workspace_id = ?",
        (subject_id, workspace_id),
    ).fetchone()


def _work_reference(store, workspace_id: str, work_id: str | None) -> dict | None:
    if not work_id:
        return None
    work = store.row(
        "SELECT id, title, status, version FROM works WHERE id = ? AND workspace_id = ?",
        (work_id, workspace_id),
    )
    if work is None:
        return None
    return {"id": work["id"], "title": work["title"], "status": work["status"], "version": work["version"]}


def _subject_reference(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "directory_state": row["directory_state"],
        "identity_review_status": row["identity_review_status"],
        "version": row["version"],
    }


def _require_live_subject(connection, workspace_id: str, subject_id: str | None):
    if subject_id is None:
        return None
    subject = _business_subject(connection, workspace_id, subject_id)
    if subject is None:
        raise _not_found()
    if subject["directory_state"] != "active":
        raise ApiError("SUBJECT_REMOVED", "The selected subject was removed; restore and review it before monitoring", 409)
    return subject


def _require_work_access(connection, identity: dict, work_id: str | None, *, write: bool = False):
    if work_id is None:
        return None
    from .collaboration import require_work_access

    return require_work_access(connection, identity, work_id, write=write)


def _monitor_identity(row) -> dict:
    return {
        "workspace": {"id": row["workspace_id"]},
        "user": {"id": row["owner_user_id"]},
    }


def _require_active_work(connection, identity: dict, work_id: str | None):
    return _require_work_access(connection, identity, work_id, write=True)


def _material_is_in_monitor_scope(connection, identity: dict, material_id: str, work_id: str | None) -> bool:
    material = connection.execute(
        "SELECT id, work_id FROM materials WHERE id = ? AND workspace_id = ?",
        (material_id, identity["workspace"]["id"]),
    ).fetchone()
    if material is None:
        return False
    if material["work_id"] is not None and work_id is None:
        return False
    if work_id is not None and material["work_id"] not in {None, work_id}:
        explicitly_linked = connection.execute(
            "SELECT 1 FROM work_materials WHERE work_id = ? AND material_id = ? AND workspace_id = ?",
            (work_id, material_id, identity["workspace"]["id"]),
        ).fetchone()
        if explicitly_linked is None:
            return False
    try:
        from .collaboration import require_material_access

        require_material_access(connection, identity, material_id, target_work_id=work_id)
    except ApiError:
        return False
    return True


def _write_version(connection, row, version: int, operation: str, user_id: str, now: str) -> None:
    sources = connection.execute(
        "SELECT url, label FROM monitor_sources WHERE monitor_id = ? AND active = 1 ORDER BY position, id",
        (row["id"],),
    ).fetchall()
    subject = _business_subject(connection, row["workspace_id"], row["subject_id"]) if row["subject_id"] else None
    work = _work_row(connection, row["workspace_id"], row["work_id"]) if row["work_id"] else None
    snapshot = {
        "title": row["title"],
        "scope": row["scope"],
        "subject_id": row["subject_id"],
        "subject": _subject_reference(subject),
        "work_id": row["work_id"],
        "work": {"id": work["id"], "title": work["title"], "status": work["status"], "version": work["version"]} if work else None,
        "follow_source_links": bool(row["follow_source_links"]),
        "sources": [{"url": item["url"], "label": item["label"]} for item in sources],
        "keywords": json.loads(row["keywords_json"]),
        "exclude_keywords": json.loads(row["exclude_keywords_json"]),
        "frequency_seconds": row["frequency_seconds"],
        "stop_at": row["stop_at"],
        "per_run_limit": row["per_run_limit"],
    }
    connection.execute(
        "INSERT INTO monitor_versions(monitor_id, workspace_id, version, operation, status, configuration_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["workspace_id"], version, operation, row["status"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now, user_id),
    )


def _sync_sources(connection, monitor_id: str, workspace_id: str, sources: list[dict], now: str) -> None:
    existing = connection.execute("SELECT * FROM monitor_sources WHERE monitor_id = ?", (monitor_id,)).fetchall()
    by_url = {item["url"]: item for item in existing}
    wanted = {item["url"] for item in sources}
    for row in existing:
        if row["url"] not in wanted and row["active"]:
            connection.execute(
                "UPDATE monitor_sources SET active = 0, position = 0, discovery_suppressed = CASE WHEN source_origin = 'discovered' THEN 1 ELSE discovery_suppressed END, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
    for position, source in enumerate(sources):
        row = by_url.get(source["url"])
        if row:
            connection.execute(
                "UPDATE monitor_sources SET active = 1, label = ?, source_origin = 'user', discovery_suppressed = 0, position = ?, updated_at = ? WHERE id = ?",
                (source["label"], position, now, row["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO monitor_sources(id, monitor_id, workspace_id, url, label, source_origin, discovery_suppressed, position, active, status, updated_at) VALUES(?, ?, ?, ?, ?, 'user', 0, ?, 1, 'not_checked', ?)",
                (str(uuid.uuid4()), monitor_id, workspace_id, source["url"], source["label"], position, now),
            )


def _deactivate_discovered_sources(connection, monitor_id: str, now: str) -> None:
    rows = connection.execute(
        "SELECT id FROM monitor_sources WHERE monitor_id = ? AND active = 1 AND source_origin = 'discovered'",
        (monitor_id,),
    ).fetchall()
    source_ids = {row["id"] for row in rows}
    if not source_ids:
        return
    _cancel_pending(
        connection, monitor_id, now,
        "Source-link following was turned off; discovered sources were paused and in-flight results are suppressed.",
        source_ids=source_ids,
    )
    placeholders = ",".join("?" for _ in source_ids)
    connection.execute(
        f"UPDATE monitor_sources SET active = 0, position = 0, status_reason = 'Source-link following was turned off.', updated_at = ? WHERE id IN ({placeholders})",
        (now, *tuple(source_ids)),
    )


def _cancel_pending(connection, monitor_id: str, now: str, reason: str, *, source_ids: set[str] | None = None) -> None:
    sql = "SELECT id, pending_job_id FROM monitor_sources WHERE monitor_id = ? AND pending_job_id IS NOT NULL"
    params: tuple = (monitor_id,)
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        sql += f" AND id IN ({placeholders})"
        params += tuple(source_ids)
    touched_runs = set()
    for source in connection.execute(sql, params).fetchall():
        job = connection.execute("SELECT status FROM jobs WHERE id = ?", (source["pending_job_id"],)).fetchone()
        touched_runs.update(row["run_id"] for row in connection.execute("SELECT run_id FROM monitor_run_sources WHERE job_id = ?", (source["pending_job_id"],)).fetchall())
        connection.execute(
            "UPDATE monitor_run_sources SET suppress_signals = 1, suppress_reason = ? WHERE job_id = ? AND lifecycle_status IN ('queued', 'running')",
            (reason, source["pending_job_id"]),
        )
        if job is None:
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = 'failed', outcome = 'interrupted', status_reason = 'The source job record is missing; no content was counted.', completed_at = ? WHERE job_id = ? AND lifecycle_status IN ('queued', 'running')",
                (now, source["pending_job_id"]),
            )
            connection.execute("UPDATE monitor_sources SET pending_job_id = NULL WHERE id = ?", (source["id"],))
        elif job["status"] == "queued":
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, updated_at = ? WHERE id = ? AND status = 'queued'",
                (now, source["pending_job_id"]),
            )
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = 'cancelled', outcome = 'cancelled', status_reason = ?, completed_at = ? WHERE job_id = ? AND lifecycle_status = 'queued'",
                (reason, now, source["pending_job_id"]),
            )
            connection.execute(
                "UPDATE monitor_sources SET pending_job_id = NULL, status = 'cancelled', status_reason = ?, updated_at = ? WHERE id = ? AND pending_job_id = ?",
                (reason, now, source["id"], source["pending_job_id"]),
            )
        elif job["status"] in {"running", "cancel_requested"}:
            connection.execute(
                "UPDATE jobs SET status = 'cancel_requested', cancel_requested = 1, updated_at = ? WHERE id = ? AND status IN ('running', 'cancel_requested')",
                (now, source["pending_job_id"]),
            )
            connection.execute(
                "UPDATE monitor_sources SET status = 'cancelling', status_reason = ?, updated_at = ? WHERE id = ?",
                (reason, now, source["id"]),
            )
        elif job["status"] in {"completed", "partial", "failed", "interrupted", "cancelled"}:
            # Leave the pointer until tick_monitoring harvests the durable result.
            pass
    for run_id in touched_runs:
        _refresh_run(connection, run_id, now)


def stop_monitors_for_removed_subject(
    connection, workspace_id: str, subject_id: str, user_id: str, now: str,
) -> int:
    """Pause linked monitors in the subject-removal transaction, retaining their history."""
    rows = connection.execute(
        "SELECT * FROM monitor_rules WHERE workspace_id = ? AND subject_id = ? ORDER BY id",
        (workspace_id, subject_id),
    ).fetchall()
    changed = 0
    reason = (
        "The linked subject was removed; queued checks were cancelled and in-flight results will not create signals. "
        "Saved source materials and prior monitoring history are preserved."
    )
    for row in rows:
        if row["status"] != "stopped":
            version = row["version"] + 1
            connection.execute(
                "UPDATE monitor_rules SET status = 'paused', version = ?, next_run_at = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (version, now, row["id"], workspace_id, row["version"]),
            )
            updated = connection.execute(
                "SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?",
                (row["id"], workspace_id),
            ).fetchone()
            _write_version(connection, updated, version, "subject_removed", user_id, now)
            changed += 1
        _cancel_pending(connection, row["id"], now, reason)
    return changed


def _source_data(row: sqlite3.Row, retry_not_before: str | None = None,
                 baseline_material: dict | None = None,
                 discovery_origins: list[dict] | None = None) -> dict:
    material_info = baseline_material or {}
    selected_saved_material = bool(
        row["last_material_id"]
        and row["status"] == "baseline_established"
        and not row["last_check_at"]
        and isinstance(row["status_reason"], str)
        and row["status_reason"].startswith("Baseline uses the selected saved public HTTP material")
    )
    return {
        "id": row["id"], "url": row["url"], "label": row["label"],
        "source_origin": row["source_origin"],
        "discovery_suppressed": bool(row["discovery_suppressed"]),
        "discovery_origins": discovery_origins or [],
        "active": bool(row["active"]), "status": row["status"], "reason": row["status_reason"],
        "last_check_at": row["last_check_at"], "last_success_at": row["last_success_at"],
        "last_material_id": row["last_material_id"],
        "source_cursor": {"sha256": row["last_sha256"], "last_success_at": row["last_success_at"]},
        "retry_not_before": retry_not_before,
        "evaluation_cursor": {
            "status": "established" if row["last_material_id"] else "pending",
            "material_id": row["last_material_id"],
            "config_version": row["last_config_version"],
            "material_created_at": material_info.get("created_at"),
        },
        "baseline": {
            "status": "established" if row["last_material_id"] else "pending",
            "kind": "selected_saved_material" if selected_saved_material else "verified_source_observation" if row["last_material_id"] else "pending",
            "material_id": row["last_material_id"],
            "material_sha256": row["last_sha256"],
            "captured_at": material_info.get("captured_at"),
            "material_created_at": material_info.get("created_at"),
            "text_sha256": material_info.get("text_sha256"),
            "coverage": material_info.get("coverage"),
        },
        "pending": row["pending_job_id"] is not None,
    }


def _monitor_data(app, store, row: sqlite3.Row, identity: dict | None = None) -> dict:
    identity = identity or {"workspace": {"id": row["workspace_id"]}, "user": {"id": "unverified-reader"}}
    sources = store.rows("SELECT * FROM monitor_sources WHERE monitor_id = ? ORDER BY active DESC, position, id", (row["id"],))
    with store.transaction(immediate=False) as connection:
        retry_times = {
            item["id"]: _source_retry_not_before(connection, item["id"], utc_now())
            for item in sources
        }
        evaluated_materials = {}
        for material in connection.execute(
                """SELECT ms.last_material_id, m.created_at, m.id AS material_id, m.work_id AS material_work_id,
                          m.status AS material_status, m.sha256, m.source_json
                   FROM monitor_sources ms JOIN materials m
                     ON m.id = ms.last_material_id AND m.workspace_id = ms.workspace_id
                   WHERE ms.monitor_id = ? AND ms.workspace_id = ?""",
                (row["id"], row["workspace_id"]),
            ).fetchall():
            if not _material_is_in_monitor_scope(connection, identity, material["material_id"], row["work_id"]):
                continue
            try:
                metadata = json.loads(material["source_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            captured_at = metadata.get("captured_at")
            if not isinstance(captured_at, str) or not _capture_text(captured_at, ""):
                captured_at = None
            evaluated_materials[material["last_material_id"]] = {
                "created_at": material["created_at"],
                "captured_at": captured_at or None,
                "text_sha256": metadata.get("text_sha256") if isinstance(metadata.get("text_sha256"), str) else None,
                "coverage": {
                    "material_status": material["material_status"],
                    "source_status": metadata.get("status"),
                    "extraction_scope": metadata.get("extraction_scope"),
                },
            }
        discovery_origins: dict[str, list[dict]] = {}
        if sources:
            edges = connection.execute(
                f"""SELECT d.child_source_id, d.parent_source_id, parent.url AS parent_url,
                           d.parent_material_id, d.config_version, d.link_kind, d.url,
                           d.title, d.source_native_id, d.observed_at
                    FROM monitor_source_discoveries d
                    JOIN monitor_sources parent ON parent.id = d.parent_source_id AND parent.workspace_id = d.workspace_id
                    JOIN monitor_sources child ON child.id = d.child_source_id AND child.monitor_id = d.monitor_id AND child.workspace_id = d.workspace_id
                    WHERE d.monitor_id = ?
                    ORDER BY d.observed_at, d.id""",
                (row["id"],),
            ).fetchall()
            for edge in edges:
                if not _material_is_in_monitor_scope(connection, identity, edge["parent_material_id"], row["work_id"]):
                    continue
                discovery_origins.setdefault(edge["child_source_id"], []).append({
                    "parent_source_id": edge["parent_source_id"],
                    "parent_url": edge["parent_url"],
                    "parent_material_id": edge["parent_material_id"],
                    "config_version": edge["config_version"],
                    "kind": edge["link_kind"],
                    "url": edge["url"],
                    "title": edge["title"],
                    "native_id": edge["source_native_id"] or None,
                    "observed_at": edge["observed_at"],
                })
        signal_rows = connection.execute(
            "SELECT * FROM monitor_signals WHERE monitor_id = ? AND workspace_id = ?",
            (row["id"], row["workspace_id"]),
        ).fetchall()
        signal_count = sum(
            _monitor_signal_is_accessible(connection, identity, row, signal)
            for signal in signal_rows
        )
        visible_sources = []
        for item in sources:
            if item["source_origin"] == "discovered" and not discovery_origins.get(item["id"]):
                continue
            if item["last_material_id"] and item["last_material_id"] not in evaluated_materials:
                sanitized = dict(item)
                sanitized.update({
                    "status": "unknown", "status_reason": "The prior source material is outside the current monitor scope.",
                    "last_check_at": None, "last_success_at": None, "last_sha256": None,
                    "last_material_id": None, "last_etag": None, "last_modified": None, "last_config_version": None,
                })
                visible_sources.append(sanitized)
            else:
                visible_sources.append(item)
    subject_row = store.row(
        "SELECT id, workspace_id, kind, name, directory_state, identity_review_status, version FROM business_subjects WHERE id = ? AND workspace_id = ?",
        (row["subject_id"], row["workspace_id"]),
    ) if row["subject_id"] else None
    worker = app.extensions.get("oevira.worker_state", {})
    checks = [item["last_check_at"] for item in visible_sources if item["last_check_at"]]
    successes = [item["last_success_at"] for item in visible_sources if item["last_success_at"]]
    return {
        "id": row["id"], "title": row["title"], "scope": row["scope"],
        "subject_id": row["subject_id"], "subject": _subject_reference(subject_row),
        "work_id": row["work_id"],
        "work": _work_reference(store, row["workspace_id"], row["work_id"]),
        "follow_source_links": bool(row["follow_source_links"]),
        "baseline_mode": "selected_saved_material" if any(
            item["last_material_id"] and item["status"] == "baseline_established"
            and not item["last_check_at"] and isinstance(item["status_reason"], str)
            and item["status_reason"].startswith("Baseline uses the selected saved public HTTP material")
            for item in visible_sources
        ) else "first_verified_source_snapshot",
        "signal_basis": "any_verified_content_change" if not json.loads(row["keywords_json"]) else "literal_keyword_match",
        "keywords": json.loads(row["keywords_json"]),
        "exclude_keywords": json.loads(row["exclude_keywords_json"]),
        "frequency_seconds": row["frequency_seconds"], "stop_at": row["stop_at"],
        "per_run_limit": row["per_run_limit"], "status": row["status"],
        "version": row["version"], "next_run_at": row["next_run_at"],
        "last_check_at": max(checks) if checks else None,
        "last_success_at": max(successes) if successes else None,
        "source_count": sum(1 for item in visible_sources if item["active"]),
        "signal_count": signal_count,
        "sources": [
            _source_data(
                item,
                retry_times.get(item["id"]),
                evaluated_materials.get(item["last_material_id"]),
                discovery_origins.get(item["id"], []),
            )
            for item in visible_sources
        ],
        "runtime": {"worker_status": worker.get("status", "not_started"), "last_heartbeat_at": worker.get("last_heartbeat_at")},
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _monitor_run_is_accessible(connection, identity: dict, monitor: sqlite3.Row, run: sqlite3.Row) -> bool:
    version = connection.execute(
        "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND workspace_id = ? AND version = ?",
        (run["monitor_id"], run["workspace_id"], run["config_version"]),
    ).fetchone()
    if version is None:
        return False
    try:
        configuration = json.loads(version["configuration_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(configuration, dict) or "work_id" not in configuration:
        return False
    target_work_id = configuration.get("work_id")
    if target_work_id:
        try:
            _require_work_access(connection, identity, target_work_id, write=False)
        except ApiError:
            return False
    sources = connection.execute(
        """SELECT rs.material_id, j.material_id AS job_material_id
           FROM monitor_run_sources rs
           LEFT JOIN jobs j ON j.id = rs.job_id AND j.workspace_id = rs.workspace_id
           WHERE rs.run_id = ? AND rs.workspace_id = ?""",
        (run["id"], run["workspace_id"]),
    ).fetchall()
    for source in sources:
        material_id = source["material_id"] or source["job_material_id"]
        if material_id and not _material_is_in_monitor_scope(connection, identity, material_id, target_work_id):
            return False
    return True


def _run_data(store, row: sqlite3.Row, identity: dict | None = None, monitor: sqlite3.Row | None = None) -> dict:
    if identity is not None:
        monitor = monitor or _monitor_row(store, identity["workspace"]["id"], row["monitor_id"])
        if monitor is None:
            raise _not_found()
        with store.transaction(immediate=False) as connection:
            from .collaboration import require_workspace_access

            require_workspace_access(connection, identity)
            if not _monitor_run_is_accessible(connection, identity, monitor, row):
                raise _not_found()
    sources = store.rows(
        """SELECT rs.*, ms.url, ms.label, j.status AS job_status, j.error_code, j.error_message,
                  j.material_id AS job_material_id, obs.id AS observation_id, obs.published_at_origin
           FROM monitor_run_sources rs
           JOIN monitor_sources ms ON ms.id = rs.source_id AND ms.workspace_id = rs.workspace_id
           LEFT JOIN jobs j ON j.id = rs.job_id AND j.workspace_id = rs.workspace_id
           LEFT JOIN monitor_observations obs ON obs.run_source_id = rs.id AND obs.workspace_id = rs.workspace_id
           WHERE rs.run_id = ? AND rs.workspace_id = ? ORDER BY ms.position, ms.id""",
        (row["id"], row["workspace_id"]),
    )
    checked = sum(item["outcome"] in {"baseline_established", "no_change", "signal", "excluded", "no_signal", "stale_configuration"} for item in sources)
    attempted = sum(item["lifecycle_status"] in {"completed", "partial", "failed"} for item in sources)
    signals = store.row("SELECT COUNT(*) AS count FROM monitor_signals WHERE run_id = ?", (row["id"],))["count"]
    version_row = store.row(
        "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND workspace_id = ? AND version = ?",
        (row["monitor_id"], row["workspace_id"], row["config_version"]),
    )
    configuration = json.loads(version_row["configuration_json"]) if version_row else {}
    visible_status = row["status"]
    if visible_status in {"queued", "running"} and any(item["job_status"] in {"running", "cancel_requested"} for item in sources):
        visible_status = "running"
    return {
        "id": row["id"], "monitor_id": row["monitor_id"], "config_version": row["config_version"],
        "subject_id": configuration.get("subject_id"), "subject": configuration.get("subject"),
        "work_id": configuration.get("work_id"), "work": configuration.get("work"),
        "kind": row["kind"], "status": visible_status, "created_at": row["created_at"],
        "started_at": row["started_at"], "completed_at": row["completed_at"],
        "coverage": {
            "total_sources": row["source_count"], "selected_sources": row["selected_count"],
            "not_covered": row["not_covered_count"], "attempted": attempted, "checked": checked, "signals": signals,
        },
        "sources": [{
            "source_id": item["source_id"], "url": item["url"], "label": item["label"],
            "status": item["outcome"], "lifecycle_status": item["lifecycle_status"],
            "job_id": item["job_id"], "job_status": item["job_status"],
            "reason": item["status_reason"] or item["error_message"],
            "error_code": item["error_code"], "material_id": item["material_id"] or item["job_material_id"],
            "observation_id": item["observation_id"], "captured_at": item["captured_at"],
            "published_at": item["published_at"], "published_at_origin": item["published_at_origin"],
            "changed": None if item["changed_from_previous"] is None else bool(item["changed_from_previous"]),
        } for item in sources],
    }


def _refresh_run(connection, run_id: str, now: str) -> None:
    run = connection.execute("SELECT * FROM monitor_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return
    items = connection.execute("SELECT lifecycle_status, outcome FROM monitor_run_sources WHERE run_id = ?", (run_id,)).fetchall()
    if any(item["lifecycle_status"] in {"queued", "running"} for item in items):
        status = "running" if any(item["lifecycle_status"] == "running" for item in items) else "queued"
        completed_at = None
    elif items and all(item["lifecycle_status"] in {"cancelled", "not_covered"} for item in items) and not any(item["lifecycle_status"] == "not_covered" for item in items):
        status, completed_at = "cancelled", now
    else:
        failures = sum(item["lifecycle_status"] in {"partial", "failed", "cancelled"} or item["outcome"] in {"partial", "unknown", "unauthorized", "rate_limited", "blocked", "unavailable", "interrupted"} for item in items)
        succeeded = sum(item["lifecycle_status"] in {"completed", "partial"} for item in items)
        if run["not_covered_count"] or (failures and succeeded):
            status = "partial"
        elif failures:
            status = "failed"
        elif not succeeded:
            status = "partial"
        else:
            status = "completed"
        completed_at = now
    connection.execute("UPDATE monitor_runs SET status = ?, completed_at = ? WHERE id = ?", (status, completed_at, run_id))


def _run_config(connection, monitor_id: str, version: int) -> dict:
    row = connection.execute(
        "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND version = ?",
        (monitor_id, version),
    ).fetchone()
    return json.loads(row["configuration_json"]) if row else {}


def _failure_details(payload_json: str | None) -> dict:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    details = payload.get("_acquisition_failure") if isinstance(payload, dict) else None
    return details if isinstance(details, dict) else {}


def _retry_after_time(value, failed_at: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = int(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    if seconds < 0:
        return None
    failed_time = _now(failed_at)
    remaining = int((datetime.max.replace(tzinfo=timezone.utc) - failed_time).total_seconds())
    if seconds >= remaining:
        return datetime.max.replace(tzinfo=timezone.utc)
    return failed_time + timedelta(seconds=seconds)


def _failure_backoff_at(failed_at: str, failure_count: int, details: dict | None = None,
                        source_id: str = "") -> str:
    exponent = min(max(failure_count - 1, 0), 20)
    base_delay = min(_MONITOR_BACKOFF_BASE_SECONDS * (2 ** exponent), _MONITOR_BACKOFF_MAX_SECONDS)
    # Stable per-source jitter spreads retries without making restart recovery
    # nondeterministic; Retry-After remains a lower bound.
    jitter = hashlib.sha256(f"{source_id}:{failure_count}".encode("utf-8")).digest()[0] / 255
    delay = min(int(base_delay * (0.8 + 0.4 * jitter)), _MONITOR_BACKOFF_MAX_SECONDS)
    failed_time = _now(failed_at)
    retry_after = _retry_after_time((details or {}).get("retry_after"), failed_at)
    backoff_at = failed_time + timedelta(seconds=delay)
    return _time_text(max(backoff_at, retry_after or backoff_at))


def _consecutive_source_failures(connection, source_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        """SELECT rs.outcome, rs.lifecycle_status, j.updated_at, j.payload_json
           FROM monitor_run_sources rs JOIN jobs j ON j.id = rs.job_id AND j.workspace_id = rs.workspace_id
           WHERE rs.source_id = ? AND rs.lifecycle_status IN ('completed', 'partial', 'failed', 'cancelled')
           ORDER BY j.updated_at DESC, rs.created_at DESC, rs.id DESC LIMIT 20""",
        (source_id,),
    ).fetchall()
    failures = []
    for row in rows:
        if row["outcome"] in _RETRYABLE_MONITOR_OUTCOMES:
            failures.append(row)
            continue
        break
    return failures


def _source_retry_not_before(connection, source_id: str, now: str) -> str | None:
    failures = _consecutive_source_failures(connection, source_id)
    if not failures:
        return None
    latest = failures[0]
    retry_at = _failure_backoff_at(
        latest["updated_at"] or now,
        len(failures),
        _failure_details(latest["payload_json"]),
        source_id,
    )
    return retry_at if retry_at > now else None


def _create_run(connection, monitor: sqlite3.Row, *, kind: str, trigger_key: str, trigger_hash: str, now: str, created_by: str) -> str:
    existing = connection.execute(
        "SELECT id, trigger_hash FROM monitor_runs WHERE monitor_id = ? AND trigger_key = ?",
        (monitor["id"], trigger_key),
    ).fetchone()
    if existing:
        if existing["trigger_hash"] != trigger_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different monitor check", 409)
        return existing["id"]
    _require_live_subject(connection, monitor["workspace_id"], monitor["subject_id"])
    _require_active_work(connection, {"workspace": {"id": monitor["workspace_id"]}, "user": {"id": created_by}}, monitor["work_id"])
    run_id = str(uuid.uuid4())
    all_sources = connection.execute(
        "SELECT * FROM monitor_sources WHERE monitor_id = ? AND workspace_id = ? AND active = 1 ORDER BY last_check_at IS NOT NULL, last_check_at, position, id",
        (monitor["id"], monitor["workspace_id"]),
    ).fetchall()
    candidates = []
    not_covered_reasons: dict[str, str] = {}
    source_use_identity = {
        "user": {"id": created_by},
        "workspace": {"id": monitor["workspace_id"]},
        "session_id": created_by,
        "source_use_worker": True,
    }
    for source in all_sources:
        if source["pending_job_id"]:
            not_covered_reasons[source["id"]] = "The previous source result is still awaiting durable reconciliation."
            continue
        retry_not_before = _source_retry_not_before(connection, source["id"], now)
        if retry_not_before:
            not_covered_reasons[source["id"]] = f"Source retry is deferred until {retry_not_before} after a prior read failure."
            continue
        try:
            from .source_use import SourceUseError, authorize_monitor_source_refresh
            decision = authorize_monitor_source_refresh(
                connection, source_use_identity,
                {"kind": "monitor_source", "id": source["id"]},
                work_id=monitor["work_id"], now=now,
            )
        except SourceUseError as exc:
            not_covered_reasons[source["id"]] = exc.code
            continue
        if not decision.allowed:
            not_covered_reasons[source["id"]] = decision.reason_code or "SOURCE_USE_NOT_ALLOWED"
            continue
        candidates.append(source)
    selected_ids = {item["id"] for item in candidates[:monitor["per_run_limit"]]}
    not_covered_count = len(all_sources) - len(selected_ids)
    connection.execute(
        "INSERT INTO monitor_runs(id, monitor_id, workspace_id, config_version, kind, trigger_key, trigger_hash, status, source_count, selected_count, not_covered_count, created_at, started_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
        (run_id, monitor["id"], monitor["workspace_id"], monitor["version"], kind, trigger_key, trigger_hash, len(all_sources), len(selected_ids), not_covered_count, now, now if selected_ids else None),
    )
    for source in all_sources:
        run_source_id = str(uuid.uuid4())
        if source["id"] not in selected_ids:
            reason = not_covered_reasons.get(source["id"], "This source was outside the user-set per-run limit.")
            outcome = "cooldown" if "deferred until" in reason else "not_covered"
            connection.execute(
                "INSERT INTO monitor_run_sources(id, run_id, monitor_id, workspace_id, source_id, config_version, lifecycle_status, outcome, status_reason, created_at, completed_at) VALUES(?, ?, ?, ?, ?, ?, 'not_covered', ?, ?, ?, ?)",
                (run_source_id, run_id, monitor["id"], monitor["workspace_id"], source["id"], monitor["version"], outcome, reason, now, now),
            )
            continue
        job_id = str(uuid.uuid4())
        payload = {"url": source["url"], "work_id": monitor["work_id"]}
        fingerprint = _request_fingerprint(payload)
        idempotency_key = f"monitor:{run_id}:{source['id']}"
        connection.execute(
            "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) VALUES(?, ?, ?, ?, 'acquire_url', 'queued', ?, ?, ?, 1, 'Waiting for monitored source check', ?, ?)",
            (job_id, monitor["workspace_id"], created_by, monitor["work_id"], json.dumps(payload, ensure_ascii=False), fingerprint, idempotency_key, now, now),
        )
        connection.execute(
            "INSERT INTO monitor_run_sources(id, run_id, monitor_id, workspace_id, source_id, job_id, config_version, lifecycle_status, outcome, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', 'not_checked', ?)",
            (run_source_id, run_id, monitor["id"], monitor["workspace_id"], source["id"], job_id, monitor["version"], now),
        )
        connection.execute(
            "UPDATE monitor_sources SET pending_job_id = ?, status = 'checking', status_reason = NULL, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (job_id, now, source["id"], monitor["workspace_id"]),
        )
    _refresh_run(connection, run_id, now)
    return run_id


def _assert_version(row: sqlite3.Row, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    if value != row["version"]:
        raise ApiError("VERSION_CONFLICT", "Monitor configuration changed; reload it before applying this action", 409, {"expected": value, "current": row["version"]})
    return value


def _monitor_row(store, workspace_id: str, monitor_id: str):
    return store.row("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (monitor_id, workspace_id))


def _require_monitor_work_read(connection, identity: dict, monitor_row) -> None:
    if monitor_row["work_id"]:
        _require_work_access(connection, identity, monitor_row["work_id"], write=False)


def _require_monitor_work_mutation(connection, identity: dict, monitor_row, *, allow_archived: bool = False) -> None:
    work_id = monitor_row["work_id"]
    if not work_id:
        return
    if not allow_archived:
        _require_active_work(connection, identity, work_id)
        return
    work = _require_work_access(connection, identity, work_id, write=False)
    from .collaboration import require_commission_access, work_commission

    commission_id = work_commission(connection, work_id)
    if commission_id:
        require_commission_access(connection, identity, commission_id, write=True)


def _signal_data(store, row: sqlite3.Row) -> dict:
    source = store.row("SELECT url, label FROM monitor_sources WHERE id = ? AND workspace_id = ?", (row["source_id"], row["workspace_id"]))
    observation = store.row("SELECT published_at, published_at_origin FROM monitor_observations WHERE id = ? AND workspace_id = ?", (row["observation_id"], row["workspace_id"]))
    linked = store.row("SELECT work_id FROM monitor_signal_works WHERE signal_id = ?", (row["id"],))
    version = store.row(
        "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND workspace_id = ? AND version = ?",
        (row["monitor_id"], row["workspace_id"], row["config_version"]),
    )
    configuration = json.loads(version["configuration_json"]) if version else {}
    return {
        "id": row["id"], "monitor_id": row["monitor_id"], "source_id": row["source_id"],
        "subject_id": configuration.get("subject_id"), "subject": configuration.get("subject"),
        "work_id": linked["work_id"] if linked else configuration.get("work_id"),
        "monitor_work_id": configuration.get("work_id"), "work": configuration.get("work"),
        "url": source["url"] if source else None, "source_label": source["label"] if source else "",
        "run_id": row["run_id"], "observation_id": row["observation_id"], "material_id": row["material_id"],
        "config_version": row["config_version"], "title": row["title"], "summary": row["summary"],
        "matched_keywords": json.loads(row["matched_keywords_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "captured_at": row["captured_at"], "published_at": observation["published_at"] if observation else None,
        "published_at_origin": observation["published_at_origin"] if observation else None,
        "linked_work_id": linked["work_id"] if linked else None, "created_at": row["created_at"],
    }


def _signal_material_references(signal: sqlite3.Row) -> set[str] | None:
    evidence_json = signal["evidence_json"] or "[]"
    if not isinstance(evidence_json, str) or len(evidence_json) > 1_000_000:
        return None
    try:
        evidence = json.loads(evidence_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(evidence, list):
        return None
    references = {signal["material_id"]} if isinstance(signal["material_id"], str) else set()
    pending = list(evidence)
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            material_id = item.get("material_id")
            if material_id is not None:
                if not isinstance(material_id, str):
                    return None
                references.add(material_id)
            pending.extend(value for key, value in item.items() if key != "material_id")
        elif isinstance(item, list):
            pending.extend(item)
    return references or None


def _monitor_signal_scope(connection, monitor: sqlite3.Row, signal: sqlite3.Row) -> tuple[bool, str | None]:
    mapped = connection.execute(
        "SELECT work_id FROM monitor_signal_works WHERE signal_id = ? AND workspace_id = ?",
        (signal["id"], signal["workspace_id"]),
    ).fetchone()
    if mapped:
        return True, mapped["work_id"]
    version = connection.execute(
        "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND workspace_id = ? AND version = ?",
        (signal["monitor_id"], signal["workspace_id"], signal["config_version"]),
    ).fetchone()
    if version is None:
        if signal["config_version"] == monitor["version"]:
            return True, monitor["work_id"]
        return False, None
    try:
        configuration = json.loads(version["configuration_json"])
    except (TypeError, json.JSONDecodeError):
        return False, None
    if not isinstance(configuration, dict) or "work_id" not in configuration:
        return False, None
    return True, configuration.get("work_id")


def _monitor_signal_is_accessible(connection, identity: dict, monitor: sqlite3.Row, signal: sqlite3.Row) -> bool:
    scope_is_valid, target_work_id = _monitor_signal_scope(connection, monitor, signal)
    if not scope_is_valid:
        return False
    if target_work_id:
        try:
            _require_work_access(connection, identity, target_work_id, write=False)
        except ApiError:
            return False
    material_ids = _signal_material_references(signal)
    if material_ids is None:
        return False
    return all(
        _material_is_in_monitor_scope(connection, identity, material_id, target_work_id)
        for material_id in material_ids
    )


def _link_signal_to_work(connection, identity: dict, signal: sqlite3.Row, work_id: str, now: str) -> sqlite3.Row:
    from .collaboration import require_material_access, require_work_access

    work = require_work_access(connection, identity, work_id, write=True)
    material_id = signal["material_id"]
    if not material_id:
        raise ApiError("SIGNAL_MATERIAL_MISSING", "This monitoring change has no saved source material", 409)
    material = require_material_access(connection, identity, material_id, target_work_id=work_id)
    if material["status"] not in {"ready", "partial"}:
        raise ApiError("MATERIAL_UNAVAILABLE", "The source material is no longer available for this work", 409)

    mapped = connection.execute(
        "SELECT work_id FROM monitor_signal_works WHERE signal_id = ? AND workspace_id = ?",
        (signal["id"], identity["workspace"]["id"]),
    ).fetchone()
    if mapped and mapped["work_id"] != work_id:
        raise ApiError("SIGNAL_WORK_ALREADY_LINKED", "This monitoring change is already linked to another work", 409)

    connection.execute(
        "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
        (work_id, material_id, identity["workspace"]["id"], now),
    )
    connection.execute(
        "INSERT OR IGNORE INTO monitor_signal_works(signal_id, work_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
        (signal["id"], work_id, identity["workspace"]["id"], now),
    )
    return work


def _validate_monitor_acquisition_resume(connection, actor: dict, job: sqlite3.Row, resume_context: dict) -> None:
    if resume_context:
        raise ApiError("INVALID_INPUT", "Continue with the original source request", 400)
    try:
        payload = json.loads(job["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved source request cannot be verified", 409) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise ApiError("JOB_RECONCILIATION_REQUIRED", "The saved source request cannot be verified", 409)

    rows = connection.execute(
        """SELECT rs.*, ms.url AS source_url, ms.active AS source_active, ms.pending_job_id,
                  mr.version AS monitor_version, mr.status AS monitor_status, mr.subject_id,
                  mr.work_id AS monitor_work_id, r.config_version AS run_config_version
           FROM monitor_run_sources rs
           JOIN monitor_sources ms ON ms.id = rs.source_id AND ms.workspace_id = rs.workspace_id
           JOIN monitor_rules mr ON mr.id = rs.monitor_id AND mr.workspace_id = rs.workspace_id
           JOIN monitor_runs r ON r.id = rs.run_id AND r.workspace_id = rs.workspace_id
           WHERE rs.job_id = ? AND rs.workspace_id = ?""",
        (job["id"], job["workspace_id"]),
    ).fetchall()
    if not rows:
        if str(job["idempotency_key"] or "").startswith("monitor:"):
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "The monitoring source check is no longer linked to its run", 409)
        return
    if len(rows) != 1:
        raise ApiError("JOB_RECONCILIATION_REQUIRED", "The monitoring source check has an ambiguous run link", 409)
    run_source = rows[0]
    stage = payload.get("_staged_acquisition")
    if isinstance(stage, dict):
        staged_source = stage.get("source")
        if (not isinstance(stage.get("material_id"), str)
                or stage["material_id"] != job["material_id"]
                or not isinstance(stage.get("sha256"), str)
                or not isinstance(staged_source, dict)
                or staged_source.get("requested_url") != payload["url"]):
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "The captured source checkpoint does not match this monitoring request", 409)
        return

    current = (
        run_source["lifecycle_status"] in {"queued", "running"}
        and not run_source["suppress_signals"]
        and bool(run_source["source_active"])
        and run_source["pending_job_id"] == job["id"]
        and run_source["source_url"] == payload["url"]
        and run_source["config_version"] == run_source["monitor_version"]
        and run_source["config_version"] == run_source["run_config_version"]
        and run_source["monitor_status"] != "stopped"
        and run_source["monitor_work_id"] == job["work_id"]
    )
    if not current:
        raise ApiError(
            "MONITOR_SOURCE_REVISION_CHANGED",
            "This source check is no longer current. Start a new check with the current monitor settings.",
            409,
        )
    if run_source["subject_id"]:
        _require_live_subject(connection, job["workspace_id"], run_source["subject_id"])
    if run_source["monitor_work_id"]:
        _require_active_work(connection, actor, run_source["monitor_work_id"])


def _register_acquisition_resume_validator(app) -> None:
    validators = app.extensions.setdefault("oevira.job_resume_validators", {})
    current = validators.get("acquire_url")
    if current is _validate_monitor_acquisition_resume or getattr(current, "_includes_monitor_resume", False):
        return
    if current is None:
        validators["acquire_url"] = _validate_monitor_acquisition_resume
        return

    def validate_acquisition_resume(connection, actor, job, resume_context):
        current(connection, actor, job, resume_context)
        _validate_monitor_acquisition_resume(connection, actor, job, resume_context)

    validate_acquisition_resume._includes_monitor_resume = True
    validators["acquire_url"] = validate_acquisition_resume


def register_monitoring_routes(app) -> None:
    store = app.extensions["oevira.store"]
    _register_acquisition_resume_validator(app)

    @app.get("/api/works/<work_id>/monitor-source-candidates")
    def list_work_monitor_source_candidates(work_id: str):
        identity = g.identity
        workspace_id = identity["workspace"]["id"]
        if set(request.args) - {"limit", "offset"}:
            raise ApiError("INVALID_INPUT", "Source candidate listing contains an unsupported query field", 400)
        try:
            limit = int(request.args.get("limit", "200"))
            offset = int(request.args.get("offset", "0"))
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "limit and offset must be integers", 400) from exc
        if not 1 <= limit <= 500 or offset < 0:
            raise ApiError("INVALID_INPUT", "limit must be 1 to 500 and offset must be nonnegative", 400)

        candidates = []
        page_by_url = {}
        seen_urls = set()
        total_candidates = 0
        excluded_count = 0
        excluded_reasons = {"missing_url": 0, "redacted_query": 0, "invalid_url": 0, "inaccessible_material": 0}
        with store.transaction(immediate=False) as connection:
            work = _require_work_access(connection, identity, work_id, write=False)
            if work["status"] != "active":
                raise ApiError("WORK_ARCHIVED", "Reopen the work before selecting monitored sources", 409)
            total_linked_materials = 0
            rows = connection.execute(
                """SELECT m.id, m.title, m.source_url,
                          CASE WHEN m.source_url IS NULL AND json_valid(m.source_json)
                               THEN COALESCE(json_extract(m.source_json, '$.source_url'),
                                             json_extract(m.source_json, '$.requested_url'),
                                             json_extract(m.source_json, '$.url'))
                          END AS metadata_url
                   FROM work_materials wm
                   JOIN materials m ON m.id = wm.material_id AND m.workspace_id = wm.workspace_id
                   WHERE wm.work_id = ? AND wm.workspace_id = ?
                   ORDER BY m.created_at DESC, m.id DESC""",
                (work_id, workspace_id),
            )
            for material in rows:
                if not _material_is_in_monitor_scope(connection, identity, material["id"], work_id):
                    excluded_count += 1
                    excluded_reasons["inaccessible_material"] += 1
                    continue
                total_linked_materials += 1
                source_url = material["source_url"] or material["metadata_url"]
                safe_url, excluded_reason = _monitorable_material_source(source_url)
                if excluded_reason:
                    excluded_count += 1
                    excluded_reasons[excluded_reason] += 1
                    continue
                if safe_url in seen_urls:
                    existing = page_by_url.get(safe_url)
                    if existing is not None:
                        existing["material_count"] += 1
                    continue
                seen_urls.add(safe_url)
                if offset <= total_candidates < offset + limit:
                    title = _clean_discovery_text(material["title"])
                    candidate = {
                        "url": safe_url,
                        "title": title,
                        "material_id": material["id"],
                        "material_count": 1,
                    }
                    candidates.append(candidate)
                    page_by_url[safe_url] = candidate
                total_candidates += 1

        next_offset = offset + len(candidates) if offset + len(candidates) < total_candidates else None
        return _success({
            "work_id": work_id,
            "candidates": candidates,
            "excluded_count": excluded_count,
            "excluded_reasons": excluded_reasons,
            "total_candidates": total_candidates,
            "total_linked_materials": total_linked_materials,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
        })

    @app.get("/api/monitors")
    def list_monitors():
        identity = g.identity
        rows = store.rows("SELECT * FROM monitor_rules WHERE workspace_id = ? ORDER BY updated_at DESC, id", (identity["workspace"]["id"],))
        visible = []
        with store.transaction(immediate=False) as connection:
            for row in rows:
                try:
                    _require_monitor_work_read(connection, identity, row)
                except ApiError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                visible.append(row)
        return _success([_monitor_data(app, store, row, identity) for row in visible])

    @app.post("/api/monitors")
    def create_monitor():
        identity = _require_write()
        data = _body()
        config = _validated_config(data, create=True)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to create a monitor", 400)
        fingerprint = _request_fingerprint(config)
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        monitor_id = str(uuid.uuid4())
        with store.transaction() as connection:
            _require_live_subject(connection, workspace_id, config["subject_id"])
            _require_active_work(connection, identity, config["work_id"])
            previous = connection.execute(
                "SELECT request_hash, resource_type, resource_id FROM idempotency_records WHERE workspace_id = ? AND operation = 'monitor.create' AND key = ?",
                (workspace_id, key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint or previous["resource_type"] != "monitor":
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different operation", 409)
                row = connection.execute("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (previous["resource_id"], workspace_id)).fetchone()
                if row is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The monitor creation result is unavailable", 409)
                response = _monitor_data(app, store, row, identity)
                return _success(response, 200)
            stop_at = config["stop_at"]
            if stop_at and stop_at <= now:
                raise ApiError("INVALID_INPUT", "stop_at must be in the future", 400)
            connection.execute(
                "INSERT INTO monitor_rules(id, workspace_id, owner_user_id, title, scope, subject_id, work_id, follow_source_links, keywords_json, exclude_keywords_json, frequency_seconds, stop_at, per_run_limit, status, version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paused', 1, ?, ?)",
                (monitor_id, workspace_id, identity["user"]["id"], config["title"], config["scope"], config["subject_id"], config["work_id"], int(config["follow_source_links"]), json.dumps(config["keywords"], ensure_ascii=False), json.dumps(config["exclude_keywords"], ensure_ascii=False), config["frequency_seconds"], stop_at, config["per_run_limit"], now, now),
            )
            _sync_sources(connection, monitor_id, workspace_id, config["sources"], now)
            row = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (monitor_id,)).fetchone()
            _seed_work_material_baselines(store, connection, identity, row, now)
            _write_version(connection, row, 1, "created_paused", identity["user"]["id"], now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'monitor.create', ?, ?, 'monitor', ?, ?)",
                (workspace_id, key, fingerprint, monitor_id, now),
            )
        return _success(_monitor_data(app, store, store.row("SELECT * FROM monitor_rules WHERE id = ?", (monitor_id,)), identity), 201)

    @app.get("/api/monitors/<monitor_id>")
    def get_monitor(monitor_id: str):
        identity = g.identity
        row = _monitor_row(store, identity["workspace"]["id"], monitor_id)
        if row is None:
            raise _not_found()
        with store.transaction(immediate=False) as connection:
            _require_monitor_work_read(connection, identity, row)
        return _success(_monitor_data(app, store, row, identity))

    @app.get("/api/monitors/<monitor_id>/versions")
    def list_monitor_versions(monitor_id: str):
        identity = g.identity
        workspace_id = identity["workspace"]["id"]
        monitor = _monitor_row(store, workspace_id, monitor_id)
        if monitor is None:
            raise _not_found()
        from .collaboration import require_workspace_access

        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, identity)
            rows = connection.execute(
                "SELECT version, operation, status, configuration_json, created_at FROM monitor_versions WHERE monitor_id = ? AND workspace_id = ? ORDER BY version DESC",
                (monitor_id, workspace_id),
            ).fetchall()
            visible_rows = []
            for version in rows:
                try:
                    configuration = json.loads(version["configuration_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(configuration, dict) or "work_id" not in configuration:
                    continue
                if configuration["work_id"]:
                    try:
                        _require_work_access(connection, identity, configuration["work_id"], write=False)
                    except ApiError:
                        continue
                visible_rows.append(version)
        return _success([{
            "version": row["version"], "operation": row["operation"], "status": row["status"],
            "configuration": json.loads(row["configuration_json"]), "created_at": row["created_at"],
        } for row in visible_rows])

    @app.patch("/api/monitors/<monitor_id>")
    def patch_monitor(monitor_id: str):
        identity = _require_write()
        data = _body()
        if "base_version" not in data:
            raise ApiError("INVALID_INPUT", "base_version is required", 400)
        base_version = data.pop("base_version")
        if not data:
            raise ApiError("INVALID_INPUT", "At least one monitor setting must change", 400)
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (monitor_id, workspace_id)).fetchone()
            if row is None:
                raise _not_found()
            _require_monitor_work_mutation(connection, identity, row, allow_archived=True)
            _assert_version(row, base_version)
            config = _config_from_row(connection, row)
            changes = _validated_config(data, create=False)
            config.update(changes)
            config = _validated_config(config, create=True)
            _require_live_subject(connection, workspace_id, config["subject_id"])
            _require_active_work(connection, identity, config["work_id"])
            if config["stop_at"] and config["stop_at"] <= now and row["status"] != "stopped":
                raise ApiError("INVALID_INPUT", "stop_at must be in the future", 400)
            version = row["version"] + 1
            next_run = _time_text(_now(now) + timedelta(seconds=config["frequency_seconds"])) if row["status"] == "enabled" else None
            connection.execute(
                "UPDATE monitor_rules SET title = ?, scope = ?, subject_id = ?, work_id = ?, follow_source_links = ?, keywords_json = ?, exclude_keywords_json = ?, frequency_seconds = ?, stop_at = ?, per_run_limit = ?, version = ?, next_run_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (config["title"], config["scope"], config["subject_id"], config["work_id"], int(config["follow_source_links"]), json.dumps(config["keywords"], ensure_ascii=False), json.dumps(config["exclude_keywords"], ensure_ascii=False), config["frequency_seconds"], config["stop_at"], config["per_run_limit"], version, next_run, now, monitor_id, workspace_id, base_version),
            )
            if "sources" in data:
                _sync_sources(connection, monitor_id, workspace_id, config["sources"], now)
            _cancel_pending(connection, monitor_id, now, "Monitor settings changed; this result belongs to an earlier configuration.")
            if not config["follow_source_links"]:
                _deactivate_discovered_sources(connection, monitor_id, now)
            updated = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (monitor_id,)).fetchone()
            if "sources" in data or "work_id" in data:
                _seed_work_material_baselines(store, connection, identity, updated, now)
            _write_version(connection, updated, version, "configuration_updated", identity["user"]["id"], now)
        return _success(_monitor_data(app, store, _monitor_row(store, workspace_id, monitor_id), identity))

    def change_monitor_state(monitor_id: str, requested: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "State changes require only base_version", 400)
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (monitor_id, workspace_id)).fetchone()
            if row is None:
                raise _not_found()
            _assert_version(row, data["base_version"])
            if requested == "enabled":
                _require_live_subject(connection, workspace_id, row["subject_id"])
                _require_active_work(connection, identity, row["work_id"])
            else:
                _require_monitor_work_mutation(connection, identity, row, allow_archived=True)
            if row["status"] == requested:
                pending = connection.execute(
                    "SELECT 1 FROM monitor_sources WHERE monitor_id = ? AND pending_job_id IS NOT NULL LIMIT 1",
                    (monitor_id,),
                ).fetchone()
                if requested != "paused" or pending is None:
                    return _success(_monitor_data(app, store, row, identity))
            if row["status"] == "stopped":
                raise ApiError("MONITOR_STOPPED", "A stopped monitor cannot be restarted; create a new monitor", 409)
            if requested == "enabled" and row["stop_at"] and row["stop_at"] <= now:
                raise ApiError("MONITOR_STOPPED", "The monitor stop time has passed", 409)
            version = row["version"] + 1
            next_run = now if requested == "enabled" else None
            connection.execute(
                "UPDATE monitor_rules SET status = ?, version = ?, next_run_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (requested, version, next_run, now, monitor_id, workspace_id, data["base_version"]),
            )
            if requested != "enabled":
                _cancel_pending(connection, monitor_id, now, "Monitor was paused; no new signal will be created from this run.")
            updated = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (monitor_id,)).fetchone()
            _write_version(connection, updated, version, requested, identity["user"]["id"], now)
        return _success(_monitor_data(app, store, _monitor_row(store, workspace_id, monitor_id), identity))

    @app.post("/api/monitors/<monitor_id>/enable")
    def enable_monitor(monitor_id: str):
        return change_monitor_state(monitor_id, "enabled")

    @app.post("/api/monitors/<monitor_id>/pause")
    def pause_monitor(monitor_id: str):
        return change_monitor_state(monitor_id, "paused")

    @app.post("/api/monitors/<monitor_id>/stop")
    def stop_monitor(monitor_id: str):
        return change_monitor_state(monitor_id, "stopped")

    @app.post("/api/monitors/<monitor_id>/check")
    def check_monitor(monitor_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "An immediate check requires base_version", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for an immediate monitor check", 400)
        workspace_id = identity["workspace"]["id"]
        trigger_key = "manual:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        fingerprint = _request_fingerprint({"monitor_id": monitor_id, "base_version": data["base_version"]})
        now = utc_now()
        with store.transaction() as connection:
            monitor = connection.execute("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (monitor_id, workspace_id)).fetchone()
            if monitor is None:
                raise _not_found()
            _require_live_subject(connection, workspace_id, monitor["subject_id"])
            _require_active_work(connection, identity, monitor["work_id"])
            existing = connection.execute("SELECT * FROM monitor_runs WHERE monitor_id = ? AND trigger_key = ?", (monitor_id, trigger_key)).fetchone()
            if existing:
                if existing["trigger_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different monitor check", 409)
                response = _run_data(store, existing, identity, monitor)
                return _success(response, 200)
            _assert_version(monitor, data["base_version"])
            if monitor["status"] == "stopped" or (monitor["stop_at"] and monitor["stop_at"] <= now):
                raise ApiError("MONITOR_STOPPED", "The monitor stop time has passed or the monitor was stopped", 409)
            run_id = _create_run(connection, monitor, kind="manual", trigger_key=trigger_key, trigger_hash=fingerprint, now=now, created_by=identity["user"]["id"])
        row = store.row("SELECT * FROM monitor_runs WHERE id = ? AND workspace_id = ?", (run_id, workspace_id))
        return _success(_run_data(store, row, identity, monitor), 202)

    @app.get("/api/monitors/<monitor_id>/runs")
    def list_monitor_runs(monitor_id: str):
        identity = g.identity
        workspace_id = identity["workspace"]["id"]
        monitor = _monitor_row(store, workspace_id, monitor_id)
        if monitor is None:
            raise _not_found()
        from .collaboration import require_workspace_access

        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, identity)
            rows = connection.execute(
                "SELECT * FROM monitor_runs WHERE monitor_id = ? AND workspace_id = ? ORDER BY created_at DESC, id LIMIT 100",
                (monitor_id, workspace_id),
            ).fetchall()
            visible_rows = [
                row for row in rows
                if _monitor_run_is_accessible(connection, identity, monitor, row)
            ]
        return _success([_run_data(store, row, identity, monitor) for row in visible_rows])

    @app.get("/api/monitor-runs/<run_id>")
    def get_monitor_run(run_id: str):
        identity = g.identity
        row = store.row("SELECT * FROM monitor_runs WHERE id = ? AND workspace_id = ?", (run_id, identity["workspace"]["id"]))
        if row is None:
            raise _not_found()
        monitor = _monitor_row(store, identity["workspace"]["id"], row["monitor_id"])
        if monitor is None:
            raise _not_found()
        return _success(_run_data(store, row, identity, monitor))

    @app.get("/api/monitors/<monitor_id>/signals")
    def list_monitor_signals(monitor_id: str):
        identity = g.identity
        workspace_id = identity["workspace"]["id"]
        monitor = _monitor_row(store, workspace_id, monitor_id)
        if monitor is None:
            raise _not_found()
        with store.transaction(immediate=False) as connection:
            from .collaboration import require_workspace_access

            require_workspace_access(connection, identity)
            rows = connection.execute(
                "SELECT * FROM monitor_signals WHERE monitor_id = ? AND workspace_id = ? ORDER BY created_at DESC, id LIMIT 200",
                (monitor_id, workspace_id),
            ).fetchall()
            visible_rows = [
                row for row in rows
                if _monitor_signal_is_accessible(connection, identity, monitor, row)
            ]
        return _success([_signal_data(store, row) for row in visible_rows])

    @app.post("/api/monitor-signals/<signal_id>/work")
    def convert_signal_to_work(signal_id: str):
        identity = _require_write()
        data = _body()
        if set(data) - {"work_id", "title", "goal", "brief"}:
            raise ApiError("INVALID_INPUT", "Signal conversion contains an unsupported field", 400)
        target_work_id = _string(data["work_id"], "work_id", maximum=80) if data.get("work_id") is not None else None
        has_new_work_fields = any(field in data for field in ("title", "goal", "brief"))
        if target_work_id is not None and has_new_work_fields:
            raise ApiError("INVALID_INPUT", "Choose an existing work or provide new-work details", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required to route a monitoring change to a work", 400)
        workspace_id = identity["workspace"]["id"]
        title_value = data.get("title")
        goal_value = data.get("goal")
        brief = _brief(data.get("brief", {}))
        create_request_hash = _request_fingerprint({"signal_id": signal_id, "title": title_value, "goal": goal_value, "brief": brief})
        now = utc_now()
        with store.transaction() as connection:
            signal = connection.execute("SELECT * FROM monitor_signals WHERE id = ? AND workspace_id = ?", (signal_id, workspace_id)).fetchone()
            if signal is None:
                raise _not_found()
            monitor = connection.execute(
                "SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?",
                (signal["monitor_id"], workspace_id),
            ).fetchone()
            if monitor is None:
                raise _not_found()
            _require_monitor_work_mutation(connection, identity, monitor)
            mapped = connection.execute("SELECT work_id FROM monitor_signal_works WHERE signal_id = ? AND workspace_id = ?", (signal_id, workspace_id)).fetchone()
            if target_work_id and monitor["work_id"] and target_work_id != monitor["work_id"]:
                raise ApiError("MONITOR_WORK_SELECTED", "This monitor is already scoped to a different work", 409)
            resolved_work_id = target_work_id or monitor["work_id"] or (mapped["work_id"] if mapped else None)
            request_hash = (
                _request_fingerprint({"signal_id": signal_id, "work_id": target_work_id})
                if target_work_id is not None else create_request_hash
            )
            prior_key = connection.execute(
                "SELECT request_hash, resource_id FROM idempotency_records WHERE workspace_id = ? AND operation = 'monitor.signal_work' AND key = ?",
                (workspace_id, key),
            ).fetchone()
            if prior_key:
                if prior_key["request_hash"] != request_hash:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for another signal-to-work request", 409)
                prior_work = _work_row(connection, workspace_id, prior_key["resource_id"])
                if prior_work is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The work linked to this signal request is unavailable", 409)
                if resolved_work_id is not None and prior_work["id"] != resolved_work_id:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for another signal-to-work request", 409)
                linked_work = _link_signal_to_work(connection, identity, signal, prior_work["id"], now)
                return _success(_work_data(connection, linked_work), 200)
            if mapped:
                if resolved_work_id and mapped["work_id"] != resolved_work_id:
                    raise ApiError("SIGNAL_WORK_ALREADY_LINKED", "This monitoring change is already linked to another work", 409)
                if not target_work_id and monitor["work_id"] and mapped["work_id"] != monitor["work_id"]:
                    raise ApiError("MONITOR_WORK_SELECTED", "This monitoring change belongs to its selected work", 409)
                work = _link_signal_to_work(connection, identity, signal, mapped["work_id"], now)
                if target_work_id is not None:
                    connection.execute(
                        "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'monitor.signal_work', ?, ?, 'work', ?, ?)",
                        (workspace_id, key, request_hash, work["id"], now),
                    )
                return _success(_work_data(connection, work), 200)
            if target_work_id is not None:
                work = _link_signal_to_work(connection, identity, signal, target_work_id, now)
                connection.execute(
                    "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'monitor.signal_work', ?, ?, 'work', ?, ?)",
                    (workspace_id, key, request_hash, work["id"], now),
                )
                return _success(_work_data(connection, work), 200)
            if monitor["work_id"]:
                if has_new_work_fields:
                    raise ApiError("MONITOR_WORK_SELECTED", "This signal is already scoped to its selected work; open that work to revise it.", 409)
                work = _link_signal_to_work(connection, identity, signal, monitor["work_id"], now)
                return _success(_work_data(connection, work), 200)
            source = connection.execute("SELECT url, label FROM monitor_sources WHERE id = ? AND workspace_id = ?", (signal["source_id"], workspace_id)).fetchone()
            title = _string(title_value, "title", maximum=200) if title_value is not None else signal["title"][:200]
            default_goal = f"复核公开来源变化：{source['url'] if source else '来源不可用'}。补充期间、口径明确且获准使用的内部数据，再形成有限分析；不推断无依据因果。"
            goal = _string(goal_value, "goal", maximum=5000) if goal_value is not None else default_goal
            work_id = str(uuid.uuid4())
            version = 1
            connection.execute(
                "INSERT INTO works(id, workspace_id, owner_user_id, title, goal, brief_json, status, version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (work_id, workspace_id, identity["user"]["id"], title, goal, json.dumps(brief, ensure_ascii=False), version, now, now),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id, version, title, goal, brief_json, status, created_at, created_by) VALUES(?, ?, ?, ?, ?, 'active', ?, ?)",
                (work_id, version, title, goal, json.dumps(brief, ensure_ascii=False), now, identity["user"]["id"]),
            )
            work = _link_signal_to_work(connection, identity, signal, work_id, now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, 'monitor.signal_work', ?, ?, 'work', ?, ?)",
                (workspace_id, key, create_request_hash, work_id, now),
            )
            response = _work_data(connection, work)
        return _success(response, 201)


_TERMINAL_JOB_STATES = {"completed", "partial", "failed", "cancelled", "interrupted"}
_RETRYABLE_MONITOR_OUTCOMES = {
    "rate_limited", "unauthorized", "blocked", "unavailable", "interrupted", "partial",
}
_COMPLETED_EVALUATION_OUTCOMES = {
    "baseline_established", "no_change", "signal", "excluded", "no_signal", "stale_configuration",
}
_MONITOR_BACKOFF_BASE_SECONDS = 60
_MONITOR_BACKOFF_MAX_SECONDS = 6 * 60 * 60
_MAX_SIGNAL_CHANGES = 100
_MAX_READABLE_UNITS = 4000
_MAX_DISCOVERED_LINKS_PER_CHECK = 1000
_MAX_ACTIVE_MONITOR_SOURCES = 100
_SENSITIVE_MONITOR_QUERY_NAME = re.compile(
    r"(?:token|secret|password|passwd|credential|session|cookie|auth|code|nonce|signature|sig)$", re.I,
)


def _failure_outcome(code: str | None, job_status: str) -> tuple[str, str]:
    if job_status in {"cancelled", "cancel_requested"}:
        return "cancelled", "The check was cancelled before it could publish a new signal."
    if job_status == "interrupted" or code == "WORKER_RESTARTED":
        return "interrupted", "The service stopped before this check could be confirmed; retry is available."
    if code == "SOURCE_RATE_LIMITED":
        return "rate_limited", "The source limited requests; no zero or no-change result was recorded."
    if code == "SOURCE_AUTH_REQUIRED":
        return "unauthorized", "The public source requires authorization; this monitor does not sign in or reuse browser sessions."
    if code == "SOURCE_ACCESS_DENIED":
        return "unauthorized", "The source denied access; this monitor does not bypass the denial."
    if code == "SOURCE_POLICY_DENIED":
        return "blocked", "The source policy does not permit this automated read."
    if code == "SOURCE_POLICY_UNAVAILABLE":
        return "unavailable", "The source policy could not be confirmed; no content check was counted."
    if code == "ACCESS_REVOKED":
        return "unauthorized", "Workspace permission changed before the source could be checked."
    return "unavailable", "The source could not be checked; no zero or no-change result was recorded."


def _verified_material(store, workspace_id: str, material_id: str | None, expected_url: str,
                      identity: dict, target_work_id: str | None) -> tuple[dict | None, str | None]:
    if not material_id:
        return None, "The acquisition job completed without a saved material."
    material = store.row("SELECT * FROM materials WHERE id = ? AND workspace_id = ?", (material_id, workspace_id))
    if material is None or material["source_url"] != expected_url:
        return None, "The saved source material is missing or does not match this monitor URL."
    try:
        with store.transaction(immediate=False) as connection:
            current = connection.execute(
                "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                (material_id, workspace_id),
            ).fetchone()
            if current is None or current["source_url"] != expected_url:
                return None, "The saved source material is no longer available."
            if not _material_is_in_monitor_scope(connection, identity, material_id, target_work_id):
                return None, "Current work or client permissions do not permit this source material for monitoring."
            try:
                metadata = json.loads(current["source_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict) or metadata.get("acquisition_method") != "public_http":
                return None, "The saved source material has no trusted public HTTP acquisition record."
            if any(key in metadata for key in ("source_use", "source_use_decision", "source_use_decision_id")):
                return None, "The saved material has an unresolved source-use record for monitoring."
            if metadata.get("status") not in {"ready", "partial"}:
                return None, "The saved source acquisition receipt is not complete enough to verify."
            requested_url, requested_error = _monitorable_material_source(metadata.get("requested_url"))
            expected_canonical, expected_error = _monitorable_material_source(expected_url)
            if requested_error or expected_error or requested_url != expected_canonical:
                return None, "The saved public HTTP request does not match this monitor source."
            receipts = connection.execute(
                """SELECT user_id, work_id, status, payload_json, updated_at FROM jobs
                   WHERE workspace_id = ? AND kind = 'acquire_url' AND material_id = ?
                   ORDER BY updated_at DESC, id DESC""",
                (workspace_id, material_id),
            ).fetchall()
            trusted_receipt = False
            trusted_completed_receipt_at = None
            for receipt in receipts:
                if (receipt["status"] not in {"completed", "partial"} or receipt["user_id"] != current["owner_user_id"]
                        or receipt["work_id"] != current["work_id"]):
                    continue
                try:
                    receipt_payload = json.loads(receipt["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                receipt_url, receipt_error = _monitorable_material_source(receipt_payload.get("url"))
                if not receipt_error and receipt_url == expected_canonical:
                    trusted_receipt = True
                    if receipt["status"] == "completed":
                        trusted_completed_receipt_at = receipt["updated_at"]
            if not trusted_receipt:
                return None, "The saved source material has no matching completed public HTTP job receipt."
            material = current
    except ApiError:
        return None, "Current work or client permissions do not permit this source material for monitoring."
    try:
        path = _resource_path(store, material["relative_path"])
        if not path.is_file():
            return None, "The saved source original is missing."
        raw = path.read_bytes()
    except (OSError, ApiError):
        return None, "The saved source original could not be verified."
    digest = hashlib.sha256(raw).hexdigest()
    if digest != material["sha256"]:
        return None, "The saved source original failed SHA-256 verification."
    try:
        metadata = json.loads(material["source_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    projection = material["source_text"]
    if not isinstance(projection, str):
        projection = material["text"] if isinstance(material["text"], str) else ""
    extracted_sha = metadata.get("text_sha256")
    if isinstance(extracted_sha, str) and extracted_sha != hashlib.sha256(projection.encode("utf-8")).hexdigest():
        return None, "The saved readable source text failed its extraction-digest verification."
    return {
        "id": material["id"], "title": material["title"], "status": material["status"],
        "work_id": material["work_id"],
        "reason": material["reason"], "sha256": digest, "source_url": material["source_url"],
        "etag": material["etag"], "last_modified": material["last_modified"], "created_at": material["created_at"],
        "receipt_completed_at": trusted_completed_receipt_at,
        "source_text": projection,
        "text_sha256": hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "source_metadata": metadata,
    }, None


def _seed_work_material_baselines(store, connection, identity: dict, monitor: sqlite3.Row, now: str) -> int:
    work_id = monitor["work_id"]
    if not work_id or monitor["status"] == "stopped" or (monitor["stop_at"] and monitor["stop_at"] <= now):
        return 0
    materials = connection.execute(
        """SELECT m.* FROM work_materials wm
           JOIN materials m ON m.id = wm.material_id AND m.workspace_id = wm.workspace_id
           WHERE wm.work_id = ? AND wm.workspace_id = ?
           ORDER BY m.created_at DESC, m.id DESC""",
        (work_id, monitor["workspace_id"]),
    ).fetchall()
    sources = connection.execute(
        "SELECT * FROM monitor_sources WHERE monitor_id = ? AND workspace_id = ? AND active = 1 AND last_material_id IS NULL ORDER BY position, id",
        (monitor["id"], monitor["workspace_id"]),
    ).fetchall()
    seeded = 0
    for source in sources:
        selected = None
        for material in materials:
            raw_url = material["source_url"]
            if not raw_url:
                try:
                    metadata = json.loads(material["source_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                if isinstance(metadata, dict):
                    raw_url = metadata.get("source_url") or metadata.get("requested_url") or metadata.get("url")
            material_url, material_url_error = _monitorable_material_source(raw_url)
            if not material_url_error and material_url == source["url"]:
                selected = material
                break
        if selected is None or selected["status"] != "ready":
            continue
        if not _material_is_in_monitor_scope(connection, identity, selected["id"], work_id):
            continue
        baseline, verification_error = _verified_material(
            store, monitor["workspace_id"], selected["id"], selected["source_url"],
            identity, work_id,
        )
        if verification_error or baseline is None:
            continue
        metadata = baseline["source_metadata"]
        if (
            baseline["status"] != "ready"
            or metadata.get("acquisition_method") != "public_http"
            or metadata.get("status") != "ready"
            or not isinstance(metadata.get("text_sha256"), str)
            or metadata["text_sha256"] != baseline["text_sha256"]
            or not baseline.get("receipt_completed_at")
            or any(key in metadata for key in ("source_use", "source_use_decision", "source_use_decision_id"))
        ):
            continue
        captured_at = metadata.get("captured_at")
        normalized_captured_at = _capture_text(captured_at, "") if isinstance(captured_at, str) else ""
        if not normalized_captured_at:
            continue
        try:
            captured_time = datetime.fromisoformat(normalized_captured_at.replace("Z", "+00:00"))
            current_time = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if captured_time > current_time + timedelta(minutes=5):
            continue
        reason = (
            f"Baseline uses the selected saved public HTTP material captured at {captured_at}. "
            "No current source check has run; a later check will revalidate current source policy."
        )
        connection.execute(
            """UPDATE monitor_sources SET status = 'baseline_established', status_reason = ?,
                      last_sha256 = ?, last_material_id = ?, last_etag = ?, last_modified = ?,
                      last_config_version = ?, updated_at = ?
               WHERE id = ? AND workspace_id = ? AND last_material_id IS NULL""",
            (reason, baseline["sha256"], baseline["id"], baseline["etag"], baseline["last_modified"],
             monitor["version"], now, source["id"], monitor["workspace_id"]),
        )
        seeded += 1
    return seeded


def _matches(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    return [term for term in terms if term.casefold() in lowered]


def _normalize_readable_text(text: str) -> str:
    """Ignore whitespace-only changes while preserving all visible characters."""
    return " ".join(text.split())


def _readable_units(text: str) -> list[dict]:
    """Split extracted text on line boundaries and retain exact source offsets."""
    units = []
    offset = 0
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), 1):
        line = raw_line.rstrip("\r\n")
        leading = len(line) - len(line.lstrip())
        trailing = len(line.rstrip())
        value = line[leading:trailing]
        normalized = _normalize_readable_text(value)
        if normalized:
            units.append({
                "line_number": line_number,
                "text": value,
                "normalized": normalized,
                "start": offset + leading,
                "end": offset + trailing,
            })
        offset += len(raw_line)
    if not text:
        return []
    if not text.endswith(("\n", "\r")):
        return units
    # splitlines(keepends=True) already includes every non-empty source line.
    return units


def _readable_changes(previous_text: str, current_text: str) -> list[dict] | None:
    if _normalize_readable_text(previous_text) == _normalize_readable_text(current_text):
        return []
    before = _readable_units(previous_text)
    after = _readable_units(current_text)
    if max(len(before), len(after)) > _MAX_READABLE_UNITS:
        return None
    matcher = SequenceMatcher(
        a=[item["normalized"] for item in before],
        b=[item["normalized"] for item in after],
        autojunk=False,
    )
    changes = []
    for operation, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        removed = before[before_start:before_end]
        added = after[after_start:after_end]
        if operation == "replace":
            paired = min(len(removed), len(added))
            changes.extend({"change": "changed", "before": removed[index], "after": added[index]} for index in range(paired))
            removed = removed[paired:]
            added = added[paired:]
        changes.extend({"change": "removed", "before": item, "after": None} for item in removed)
        changes.extend({"change": "added", "before": None, "after": item} for item in added)
    return changes


def _evidence_reference(connection, workspace_id: str, material_id: str, unit: dict) -> dict:
    segments = connection.execute(
        "SELECT id, label, text FROM material_segments WHERE material_id = ? AND workspace_id = ? ORDER BY start_offset, id",
        (material_id, workspace_id),
    ).fetchall()
    expected_label = f"第 {unit['line_number']} 行"
    segment = next((item for item in segments if item["label"] == expected_label), None)
    if segment is None or _normalize_readable_text(segment["text"]) != unit["normalized"]:
        segment = next(
            (item for item in segments if _normalize_readable_text(item["text"]) == unit["normalized"]),
            None,
        )
    return {
        "material_id": material_id,
        "segment_id": segment["id"] if segment is not None else None,
        "label": segment["label"] if segment is not None else expected_label,
        "quote": unit["text"],
        "start_offset": unit["start"],
        "end_offset": unit["end"],
        "location_state": "material_segment" if segment is not None else "source_text_offset",
    }


def _change_evidence(connection, workspace_id: str, changes: list[dict],
                     previous: dict | None, current: dict, keywords: list[str]) -> tuple[list[dict], int]:
    evidence = []
    total = len(changes)
    for change in changes[:_MAX_SIGNAL_CHANGES]:
        before = _evidence_reference(connection, workspace_id, previous["id"], change["before"]) if previous and change["before"] else None
        after = _evidence_reference(connection, workspace_id, current["id"], change["after"]) if change["after"] else None
        corpus = " ".join(item["quote"] for item in (before, after) if item)
        matches = _matches(corpus, keywords)
        primary = after or before
        evidence.append({
            **primary,
            "change": change["change"],
            "before": before,
            "after": after,
            "matched_keywords": matches,
            "matched_keyword": matches[0] if matches else None,
        })
    if total > _MAX_SIGNAL_CHANGES:
        evidence.append({"change": "evidence_truncated", "omitted_changes": total - _MAX_SIGNAL_CHANGES,
                         "location_state": "not_included"})
    return evidence, total


def _rule_match_evidence(connection, workspace_id: str, material: dict, keywords: list[str]) -> list[dict]:
    matches_by_unit = []
    for unit in _readable_units(material["source_text"]):
        matches = _matches(unit["text"], keywords)
        if matches:
            matches_by_unit.append((unit, matches))
    result = []
    for unit, matches in matches_by_unit[:_MAX_SIGNAL_CHANGES]:
        reference = _evidence_reference(connection, workspace_id, material["id"], unit)
        result.append({
            **reference,
            "change": "rule_match",
            "before": None,
            "after": reference,
            "matched_keywords": matches,
            "matched_keyword": matches[0],
        })
    if len(matches_by_unit) > _MAX_SIGNAL_CHANGES:
        result.append({"change": "evidence_truncated", "omitted_changes": len(matches_by_unit) - _MAX_SIGNAL_CHANGES,
                       "location_state": "not_included"})
    return result


def _same_origin(left: str, right: str) -> bool:
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        left_port = left_parts.port or (443 if left_parts.scheme.lower() == "https" else 80)
        right_port = right_parts.port or (443 if right_parts.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    if any((parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password)
           for parts in (left_parts, right_parts)):
        return False
    return (
        left_parts.scheme.lower() == right_parts.scheme.lower()
        and left_parts.hostname.lower().rstrip(".") == right_parts.hostname.lower().rstrip(".")
        and left_port == right_port
    )


def _same_origin_public_url(parent_url: str, candidate_url: str) -> str | None:
    if not isinstance(candidate_url, str) or not candidate_url or len(candidate_url) > 4096:
        return None
    if any(ord(char) < 32 for char in candidate_url):
        return None
    try:
        resolved = urljoin(parent_url, candidate_url)
        parts = urlsplit(resolved)
        port = parts.port
    except ValueError:
        return None
    if len(resolved) > 4096 or any(ord(char) < 32 for char in resolved):
        return None
    if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or port not in {None, 80, 443}):
        return None
    if not _same_origin(parent_url, resolved):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _clean_discovery_text(value, maximum: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char.isprintable()).strip()[:maximum]


def _discovery_candidates(source_metadata: dict) -> tuple[list[dict], bool]:
    candidates = []
    incomplete = bool(source_metadata.get("entries_truncated") or source_metadata.get("links_truncated"))
    for link in source_metadata.get("discovered_links", []) if isinstance(source_metadata.get("discovered_links"), list) else []:
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            candidate_kind = link.get("kind")
            kind = candidate_kind if isinstance(candidate_kind, str) and candidate_kind in {"page_link", "declared_feed", "robots_sitemap"} else "page_link"
            candidates.append({
                "url": link["url"], "title": _clean_discovery_text(link.get("title")),
                "native_id": "", "kind": kind,
            })
    for entry in source_metadata.get("entries", []) if isinstance(source_metadata.get("entries"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            native_id = _clean_discovery_text(entry.get("native_id"), 500)
            candidate_kind = entry.get("kind")
            kind = candidate_kind if isinstance(candidate_kind, str) and candidate_kind in {"feed_entry", "sitemap_index", "sitemap_url"} else "feed_entry"
            title = _clean_discovery_text(entry.get("title"))
            if not title and kind in {"sitemap_index", "sitemap_url"}:
                title = "Site map" if kind == "sitemap_index" else "Site page"
            candidates.append({
                "url": entry["url"], "title": title,
                "native_id": native_id, "kind": kind,
            })
    next_url = source_metadata.get("next_url")
    if isinstance(next_url, str):
        candidates.append({"url": next_url, "title": "", "native_id": "", "kind": "next_page"})

    result = []
    seen = set()
    for item in candidates:
        key = (item["url"], item["kind"], item["native_id"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    if len(result) > _MAX_DISCOVERED_LINKS_PER_CHECK:
        incomplete = True
        result = result[:_MAX_DISCOVERED_LINKS_PER_CHECK]
    return result, incomplete


def _record_discovered_sources(connection, monitor, parent_source, material: dict,
                               config_version: int, now: str) -> dict:
    candidates, incomplete = _discovery_candidates(material["source_metadata"])
    if not candidates and not incomplete:
        return {"added": 0, "omitted": 0, "incomplete": False, "ignored": 0}

    parent_url = material["source_metadata"].get("url")
    if not isinstance(parent_url, str) or not parent_url:
        parent_url = parent_source["url"]
    active_count = connection.execute(
        "SELECT COUNT(*) AS count FROM monitor_sources WHERE monitor_id = ? AND workspace_id = ? AND active = 1",
        (monitor["id"], monitor["workspace_id"]),
    ).fetchone()["count"]
    position = connection.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM monitor_sources WHERE monitor_id = ? AND active = 1",
        (monitor["id"],),
    ).fetchone()["position"]
    added = 0
    omitted = 0
    ignored = 0
    for candidate in candidates:
        url = _same_origin_public_url(parent_source["url"], candidate["url"])
        final_parent_url = _same_origin_public_url(parent_source["url"], parent_url)
        if url is None or (parent_url and final_parent_url is None):
            ignored += 1
            continue
        existing = connection.execute(
            "SELECT id, active, source_origin, discovery_suppressed FROM monitor_sources WHERE monitor_id = ? AND url = ?",
            (monitor["id"], url),
        ).fetchone()
        if existing is None:
            if active_count >= _MAX_ACTIVE_MONITOR_SOURCES:
                omitted += 1
                incomplete = True
                continue
            child_source_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO monitor_sources(id, monitor_id, workspace_id, url, label, source_origin, discovery_suppressed, position, active, status, updated_at) VALUES(?, ?, ?, ?, ?, 'discovered', 0, ?, 1, 'not_checked', ?)",
                (child_source_id, monitor["id"], monitor["workspace_id"], url, candidate["title"], position, now),
            )
            active_count += 1
            position += 1
            added += 1
        else:
            child_source_id = existing["id"]
            if (not existing["active"] and existing["source_origin"] == "discovered"
                    and not existing["discovery_suppressed"]):
                if active_count < _MAX_ACTIVE_MONITOR_SOURCES:
                    connection.execute(
                        "UPDATE monitor_sources SET active = 1, position = ?, status_reason = NULL, updated_at = ? WHERE id = ?",
                        (position, now, child_source_id),
                    )
                    active_count += 1
                    position += 1
                    added += 1
                else:
                    omitted += 1
                    incomplete = True
        connection.execute(
            """INSERT OR IGNORE INTO monitor_source_discoveries(
                   id, monitor_id, workspace_id, parent_source_id, child_source_id,
                   parent_material_id, config_version, link_kind, url, title,
                   source_native_id, observed_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), monitor["id"], monitor["workspace_id"], parent_source["id"],
             child_source_id, material["id"], config_version, candidate["kind"], url,
             candidate["title"], candidate["native_id"], now),
        )
    return {"added": added, "omitted": omitted, "incomplete": incomplete, "ignored": ignored}


def _harvest_one(store, run_source_id: str, now: str) -> bool:
    work = store.row(
        """SELECT rs.*, j.status AS job_status, j.error_code, j.error_message,
                  j.material_id AS job_material_id, j.updated_at AS job_updated_at, j.progress_label AS job_progress_label,
                  j.payload_json AS job_payload_json, j.work_id AS acquisition_work_id,
                  ms.url, ms.last_sha256, ms.last_material_id, ms.last_config_version, ms.last_check_at AS previous_check_at,
                  ms.pending_job_id, mr.title AS monitor_title, mr.owner_user_id AS monitor_owner_user_id,
                  mr.work_id AS monitor_work_id, r.created_at AS run_created_at
           FROM monitor_run_sources rs
           JOIN jobs j ON j.id = rs.job_id AND j.workspace_id = rs.workspace_id
           JOIN monitor_sources ms ON ms.id = rs.source_id AND ms.workspace_id = rs.workspace_id
           JOIN monitor_rules mr ON mr.id = rs.monitor_id AND mr.workspace_id = rs.workspace_id
           JOIN monitor_runs r ON r.id = rs.run_id AND r.workspace_id = rs.workspace_id
           WHERE rs.id = ?""",
        (run_source_id,),
    )
    if work is None or work["job_status"] not in _TERMINAL_JOB_STATES or work["lifecycle_status"] not in {"queued", "running"}:
        return False
    monitor_identity = {
        "workspace": {"id": work["workspace_id"]},
        "user": {"id": work["monitor_owner_user_id"]},
    }
    material, material_error = (None, None)
    if work["job_status"] in {"completed", "partial"}:
        material, material_error = _verified_material(
            store, work["workspace_id"], work["job_material_id"], work["url"],
            monitor_identity, work["monitor_work_id"],
        )
    previous, previous_error = (None, None)
    if work["last_material_id"]:
        if material is not None and material["id"] == work["last_material_id"]:
            previous = material
        else:
            previous, previous_error = _verified_material(
                store, work["workspace_id"], work["last_material_id"], work["url"],
                monitor_identity, work["monitor_work_id"],
            )

    with store.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM monitor_run_sources WHERE id = ? AND workspace_id = ?",
            (run_source_id, work["workspace_id"]),
        ).fetchone()
        job = connection.execute("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (current["job_id"], work["workspace_id"])).fetchone() if current else None
        source = connection.execute("SELECT * FROM monitor_sources WHERE id = ? AND workspace_id = ?", (work["source_id"], work["workspace_id"])).fetchone()
        monitor = connection.execute("SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?", (work["monitor_id"], work["workspace_id"])).fetchone()
        if current is None or job is None or source is None or monitor is None or current["lifecycle_status"] not in {"queued", "running"} or job["status"] not in _TERMINAL_JOB_STATES:
            return False
        if source["pending_job_id"] != current["job_id"]:
            reason = "The source job completed after its reconciliation pointer changed; this result was not admitted as a current observation."
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = 'failed', outcome = 'interrupted', status_reason = ?, material_id = ?, completed_at = ? WHERE id = ?",
                (reason, job["material_id"], now, current["id"]),
            )
            _refresh_run(connection, current["run_id"], now)
            return True
        subject = _business_subject(connection, work["workspace_id"], monitor["subject_id"]) if monitor["subject_id"] else None
        if monitor["subject_id"] and (subject is None or subject["directory_state"] != "active"):
            reason = (
                "The linked subject was removed before this result could be admitted as a monitoring observation; "
                "any already-saved source material is preserved."
            )
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = 'cancelled', outcome = 'cancelled', status_reason = ?, material_id = ?, completed_at = ? WHERE id = ? AND lifecycle_status IN ('queued', 'running')",
                (reason, job["material_id"], now, current["id"]),
            )
            connection.execute(
                "UPDATE monitor_sources SET status = 'cancelled', status_reason = ?, pending_job_id = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND pending_job_id = ?",
                (reason, now, current["source_id"], work["workspace_id"], current["job_id"]),
            )
            _refresh_run(connection, current["run_id"], now)
            return True
        if monitor["work_id"]:
            try:
                _require_active_work(connection, _monitor_identity(monitor), monitor["work_id"])
            except ApiError as exc:
                if exc.code not in {"NOT_FOUND", "FORBIDDEN", "WORK_ARCHIVED", "COMMISSION_ARCHIVED"}:
                    raise
                reason = "The linked work or its client permission became unavailable before this result was admitted; any saved material remains preserved."
                connection.execute(
                    "UPDATE monitor_run_sources SET lifecycle_status = 'cancelled', outcome = 'cancelled', status_reason = ?, material_id = ?, completed_at = ? WHERE id = ?",
                    (reason, job["material_id"], now, current["id"]),
                )
                connection.execute(
                    "UPDATE monitor_sources SET status = 'cancelled', status_reason = ?, pending_job_id = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND pending_job_id = ?",
                    (reason, now, current["source_id"], work["workspace_id"], current["job_id"]),
                )
                _refresh_run(connection, current["run_id"], now)
                return True
        captured_at = job["updated_at"] or now
        config_is_current = monitor["version"] == current["config_version"]
        can_evaluate = config_is_current and not current["suppress_signals"]
        previous_sha = previous["sha256"] if previous is not None else None
        observation_id = None
        signal_id = None

        if job["status"] not in {"completed", "partial"}:
            outcome, reason = _failure_outcome(job["error_code"], job["status"])
            lifecycle = "cancelled" if outcome == "cancelled" else "failed"
            if outcome in _RETRYABLE_MONITOR_OUTCOMES:
                prior_failures = len(_consecutive_source_failures(connection, current["source_id"]))
                retry_not_before = _failure_backoff_at(
                    job["updated_at"] or now,
                    prior_failures + 1,
                    _failure_details(job["payload_json"]),
                    current["source_id"],
                )
                reason = f"{reason} The next automatic source attempt is no earlier than {retry_not_before}."
            if current["suppress_signals"] and outcome not in {"cancelled", "interrupted"}:
                reason = f"{reason} No signal was eligible because the monitor configuration changed or was paused."
        elif material_error:
            outcome, reason, lifecycle = "unavailable", material_error, "failed"
        else:
            config = _run_config(connection, current["monitor_id"], current["config_version"])
            source_status = material["source_metadata"].get("status")
            is_unavailable = material["status"] in {"unavailable", "error"} or source_status not in {"ready", "partial"} or not material["source_text"].strip()
            is_partial = not is_unavailable and (
                material["status"] == "partial" or source_status == "partial"
            )
            content_complete = not is_partial and not is_unavailable
            current_sha = material["sha256"]
            metadata_capture = material["source_metadata"].get("captured_at")
            if work["job_progress_label"] != "Source unchanged; verified saved material reused" and isinstance(metadata_capture, str):
                captured_at = _capture_text(metadata_capture, now)
            else:
                captured_at = now
            changes = []
            comparison_unknown = False
            comparison_reason = None
            if not content_complete:
                changed = None
            elif source["last_material_id"] is None:
                changed = None
                comparison_unknown = source["last_sha256"] is not None
                if comparison_unknown:
                    comparison_reason = "The raw source cursor existed without a verifiable evaluated material."
            elif previous is None:
                changed = None
                comparison_unknown = True
                comparison_reason = previous_error or "The prior evaluated source material could not be verified."
            else:
                changes = _readable_changes(previous["source_text"], material["source_text"])
                if changes is None:
                    changed = None
                    comparison_unknown = True
                    comparison_reason = f"Readable source comparison exceeded the {_MAX_READABLE_UNITS}-line safety bound."
                    changes = []
                else:
                    changed = int(_normalize_readable_text(previous["source_text"]) != _normalize_readable_text(material["source_text"]))
            config_changed_since_evaluation = (
                source["last_material_id"] is not None
                and source["last_config_version"] != current["config_version"]
            )
            change_text = " ".join(
                unit["text"]
                for change in changes
                for unit in (change["before"], change["after"])
                if unit is not None
            )
            current_text = material["source_text"]
            evaluation_text = f"{current_text} {change_text}" if config_changed_since_evaluation else change_text
            included = _matches(evaluation_text, config.get("keywords", []))
            if config_changed_since_evaluation:
                excluded = _matches(current_text, config.get("exclude_keywords", []))
            else:
                excluded = _matches(
                    " ".join(change["after"]["text"] for change in changes if change["after"] is not None),
                    config.get("exclude_keywords", []),
                )
            published_at = material["source_metadata"].get("published_at")
            published_origin = material["source_metadata"].get("published_at_origin")
            observation_id = str(uuid.uuid4())
            connection.execute(
                "INSERT OR IGNORE INTO monitor_observations(id, monitor_id, workspace_id, source_id, run_id, run_source_id, material_id, sha256, captured_at, published_at, published_at_origin, content_status, changed_from_previous, previous_sha256, config_version, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (observation_id, current["monitor_id"], current["workspace_id"], current["source_id"], current["run_id"], current["id"], material["id"], current_sha, captured_at, published_at, published_origin, material["status"], changed, previous_sha, current["config_version"], now),
            )
            evaluated = False
            if is_unavailable:
                outcome, reason, lifecycle = "unavailable", material["reason"] or "The captured original yielded no readable source text.", "failed"
            elif is_partial:
                outcome, reason, lifecycle = "partial", material["reason"] or "The source text was only partially available; no complete-coverage signal was emitted.", "partial"
            elif not can_evaluate:
                outcome = "stale_configuration"
                reason = current["suppress_reason"] or "The source check completed under an earlier configuration; no signal was emitted."
                lifecycle = "completed"
            elif comparison_unknown:
                outcome = "unknown"
                reason = f"{comparison_reason or 'The prior evaluated source version could not be verified.'} This readable snapshot is now the next comparison baseline; the gap remains unknown."
                lifecycle = "partial"
                evaluated = True
            elif source["last_material_id"] is None:
                outcome, reason, lifecycle = "baseline_established", "The first verified source snapshot is the baseline; it is not reported as a change.", "completed"
                evaluated = True
            elif changed and excluded:
                outcome, reason, lifecycle = "excluded", f"Readable content changed but an updated text block matched excluded terms: {', '.join(excluded)}.", "completed"
                evaluated = True
            elif changed and config.get("keywords") and not included:
                outcome, reason, lifecycle = "no_signal", "Readable content changed, but no changed text block matched an included literal keyword.", "completed"
                evaluated = True
            elif config_changed_since_evaluation and excluded:
                outcome, reason, lifecycle = "excluded", f"The current rule version re-evaluated saved text and found excluded terms: {', '.join(excluded)}.", "completed"
                evaluated = True
            elif config_changed_since_evaluation and config.get("keywords") and not included:
                outcome, reason, lifecycle = "no_signal", "The current rule version evaluated the saved text; no included literal keyword matched it.", "completed"
                evaluated = True
            elif config_changed_since_evaluation and config.get("keywords") and included and not changed:
                outcome, lifecycle = "signal", "completed"
                evidence = _rule_match_evidence(connection, current["workspace_id"], material, included)
                summary = (
                    f"规则版本 {current['config_version']} 的字面关键词在已保存的可读正文中命中：{', '.join(included)}。"
                    "本次来源正文未变化；这是规则复评结果，不是新的来源事件。"
                )
                signal_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT OR IGNORE INTO monitor_signals(id, monitor_id, workspace_id, source_id, run_id, run_source_id, observation_id, material_id, config_version, title, summary, matched_keywords_json, evidence_json, previous_sha256, current_sha256, captured_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (signal_id, current["monitor_id"], current["workspace_id"], current["source_id"], current["run_id"], current["id"], observation_id, material["id"], current["config_version"], f"规则复评匹配：{source['label'] or material['title']}", summary, json.dumps(included, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), previous_sha, current_sha, captured_at, now),
                )
                reason = summary
                evaluated = True
            elif not changed:
                outcome, reason, lifecycle = "no_change", "The readable source text is unchanged after whitespace normalization.", "completed"
                evaluated = True
            else:
                outcome, lifecycle = "signal", "completed"
                evaluated = True
                signal_id = str(uuid.uuid4())
                change_counts = {name: sum(item["change"] == name for item in changes) for name in ("added", "removed", "changed")}
                detail = f"{change_counts['added']} added, {change_counts['removed']} removed, {change_counts['changed']} changed readable text block(s)"
                basis = f"; matched literal keywords: {', '.join(included)}" if included else "; no include keywords were configured"
                if config_changed_since_evaluation:
                    summary = f"规则版本 {current['config_version']} 重新评价了已保存的可读正文：新增 {change_counts['added']} 段、删除 {change_counts['removed']} 段、改写 {change_counts['changed']} 段{basis}。这不构成事实或因果结论。"
                else:
                    summary = f"已核验的可读正文发生变化：新增 {change_counts['added']} 段、删除 {change_counts['removed']} 段、改写 {change_counts['changed']} 段{basis}。这不构成事实或因果结论。"
                if len(changes) > _MAX_SIGNAL_CHANGES:
                    summary += f" Evidence includes the first {_MAX_SIGNAL_CHANGES} changes and omits {len(changes) - _MAX_SIGNAL_CHANGES}."
                evidence, _ = _change_evidence(
                    connection, current["workspace_id"], changes, previous, material, config.get("keywords", []),
                )
                if config_changed_since_evaluation and included:
                    existing_evidence = {(item.get("material_id"), item.get("quote")) for item in evidence}
                    for item in _rule_match_evidence(connection, current["workspace_id"], material, included):
                        marker = (item.get("material_id"), item.get("quote"))
                        if marker not in existing_evidence:
                            evidence.append(item)
                            existing_evidence.add(marker)
                        if len(evidence) >= _MAX_SIGNAL_CHANGES:
                            break
                connection.execute(
                    "INSERT OR IGNORE INTO monitor_signals(id, monitor_id, workspace_id, source_id, run_id, run_source_id, observation_id, material_id, config_version, title, summary, matched_keywords_json, evidence_json, previous_sha256, current_sha256, captured_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (signal_id, current["monitor_id"], current["workspace_id"], current["source_id"], current["run_id"], current["id"], observation_id, material["id"], current["config_version"], f"来源有变化：{source['label'] or material['title']}", summary, json.dumps(included, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), previous_sha, current_sha, captured_at, now),
                )
                reason = summary

            discovery = {"added": 0, "omitted": 0, "incomplete": False, "ignored": 0}
            if not is_unavailable and can_evaluate and config.get("follow_source_links", False):
                discovery = _record_discovered_sources(
                    connection, monitor, source, material, current["config_version"], now,
                )
                if discovery["added"]:
                    reason = f"{reason} Discovered {discovery['added']} same-origin source(s); they await a later bounded check."
                    if lifecycle == "completed":
                        lifecycle = "partial"
                if discovery["ignored"]:
                    reason = f"{reason} Ignored {discovery['ignored']} invalid or out-of-scope link(s); discovery remains same-origin."
                if discovery["incomplete"]:
                    reason = f"{reason} Link discovery was bounded or truncated; the source list is not claimed exhaustive."
                    if lifecycle == "completed":
                        lifecycle = "partial"

            advance_evaluation = content_complete and evaluated
            # Raw source progress and rule evaluation progress are separate. A
            # stale or suppressed check can advance ETag/byte cursors without
            # moving the last material/configuration evaluated by the current rule.
            connection.execute(
                "UPDATE monitor_sources SET status = ?, status_reason = ?, last_check_at = ?, last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, last_sha256 = CASE WHEN ? THEN ? ELSE last_sha256 END, last_material_id = CASE WHEN ? THEN ? ELSE last_material_id END, last_etag = CASE WHEN ? THEN ? ELSE last_etag END, last_modified = CASE WHEN ? THEN ? ELSE last_modified END, last_config_version = CASE WHEN ? THEN ? ELSE last_config_version END, pending_job_id = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND pending_job_id = ?",
                (outcome, reason, captured_at, int(content_complete), captured_at, int(content_complete), current_sha, int(advance_evaluation), material["id"], int(content_complete), material["etag"], int(content_complete), material["last_modified"], int(advance_evaluation), current["config_version"], now, current["source_id"], current["workspace_id"], current["job_id"]),
            )
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = ?, outcome = ?, status_reason = ?, material_id = ?, sha256 = ?, captured_at = ?, published_at = ?, changed_from_previous = ?, included_matches_json = ?, excluded_matches_json = ?, completed_at = ? WHERE id = ?",
                (lifecycle, outcome, reason, material["id"], current_sha, captured_at, published_at, changed, json.dumps(included, ensure_ascii=False), json.dumps(excluded, ensure_ascii=False), now, current["id"]),
            )
        if job["status"] not in {"completed", "partial"} or material_error:
            lifecycle = "cancelled" if job["status"] == "cancelled" else "failed"
            connection.execute(
                "UPDATE monitor_run_sources SET lifecycle_status = ?, outcome = ?, status_reason = ?, material_id = ?, completed_at = ? WHERE id = ?",
                (lifecycle, outcome, reason, job["material_id"], now, current["id"]),
            )
            connection.execute(
                "UPDATE monitor_sources SET status = ?, status_reason = ?, last_check_at = ?, pending_job_id = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND pending_job_id = ?",
                (outcome, reason, captured_at, now, current["source_id"], current["workspace_id"], current["job_id"]),
            )
        connection.execute(
            "UPDATE monitor_runs SET started_at = COALESCE(started_at, ?), completed_at = NULL WHERE id = ?",
            (work["job_updated_at"], current["run_id"]),
        )
        _refresh_run(connection, current["run_id"], now)
    return True


def _harvest_finished_jobs(store, now: str, limit: int = 200) -> int:
    rows = store.rows(
        """SELECT rs.id FROM monitor_run_sources rs
           JOIN jobs j ON j.id = rs.job_id AND j.workspace_id = rs.workspace_id
           WHERE rs.lifecycle_status IN ('queued', 'running') AND j.status IN ('completed', 'partial', 'failed', 'cancelled', 'interrupted')
           ORDER BY j.updated_at, rs.id LIMIT ?""",
        (limit,),
    )
    return sum(_harvest_one(store, row["id"], now) for row in rows)


def _expire_monitors(store, now: str) -> int:
    changed = 0
    with store.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM monitor_rules WHERE status IN ('enabled', 'paused') AND stop_at IS NOT NULL AND stop_at <= ? ORDER BY stop_at, id LIMIT 100",
            (now,),
        ).fetchall()
        for row in rows:
            version = row["version"] + 1
            connection.execute("UPDATE monitor_rules SET status = 'stopped', version = ?, next_run_at = NULL, updated_at = ? WHERE id = ? AND version = ?", (version, now, row["id"], row["version"]))
            _cancel_pending(connection, row["id"], now, "The configured stop time passed; no new source requests will be issued.")
            updated = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (row["id"],)).fetchone()
            _write_version(connection, updated, version, "stop_time_reached", row["owner_user_id"], now)
            changed += 1
    return changed


def _schedule_due(store, now_dt: datetime) -> int:
    now = _time_text(now_dt)
    scheduled = 0
    with store.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM monitor_rules WHERE status = 'enabled' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at, id LIMIT 100",
            (now,),
        ).fetchall()
        for row in rows:
            if row["subject_id"]:
                subject = _business_subject(connection, row["workspace_id"], row["subject_id"])
                if subject is None or subject["directory_state"] != "active":
                    version = row["version"] + 1
                    connection.execute(
                        "UPDATE monitor_rules SET status = 'paused', version = ?, next_run_at = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                        (version, now, row["id"], row["workspace_id"], row["version"]),
                    )
                    updated = connection.execute(
                        "SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?",
                        (row["id"], row["workspace_id"]),
                    ).fetchone()
                    _write_version(connection, updated, version, "subject_unavailable", row["owner_user_id"], now)
                    _cancel_pending(
                        connection,
                        row["id"],
                        now,
                        "The linked subject is unavailable; no new monitoring run was started.",
                    )
                    continue
            if row["work_id"]:
                try:
                    _require_active_work(connection, _monitor_identity(row), row["work_id"])
                except ApiError as exc:
                    if exc.code not in {"NOT_FOUND", "FORBIDDEN", "WORK_ARCHIVED", "COMMISSION_ARCHIVED"}:
                        raise
                    version = row["version"] + 1
                    connection.execute(
                        "UPDATE monitor_rules SET status = 'paused', version = ?, next_run_at = NULL, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                        (version, now, row["id"], row["workspace_id"], row["version"]),
                    )
                    updated = connection.execute(
                        "SELECT * FROM monitor_rules WHERE id = ? AND workspace_id = ?",
                        (row["id"], row["workspace_id"]),
                    ).fetchone()
                    _write_version(connection, updated, version, "work_archived", row["owner_user_id"], now)
                    _cancel_pending(
                        connection, row["id"], now,
                        "The linked work or its client permission is unavailable; no new monitoring run was started.",
                    )
                    continue
            active_run = connection.execute(
                "SELECT 1 FROM monitor_runs WHERE monitor_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (row["id"],),
            ).fetchone()
            if active_run:
                continue
            slot = row["next_run_at"]
            trigger_key = "schedule:" + slot
            trigger_hash = _request_fingerprint({"monitor_id": row["id"], "version": row["version"], "slot": slot})
            _create_run(connection, row, kind="scheduled", trigger_key=trigger_key, trigger_hash=trigger_hash, now=now, created_by=row["owner_user_id"])
            next_run = _time_text(now_dt + timedelta(seconds=row["frequency_seconds"]))
            connection.execute("UPDATE monitor_rules SET next_run_at = ?, updated_at = ? WHERE id = ? AND version = ?", (next_run, now, row["id"], row["version"]))
            scheduled += 1
    return scheduled


def tick_monitoring(app, now: str | datetime | None = None) -> dict[str, int]:
    """Harvest shared-queue URL jobs and enqueue due source checks.

    Call this from the existing Worker loop. Source jobs use the existing
    ``acquire_url`` kind so they pass through the sole acquisition and Material
    persistence path; no parallel thread, network client or queue is introduced.
    """
    store = app.extensions["oevira.store"]
    current = _now(now)
    current_text = _time_text(current)
    observed_text = current.isoformat(timespec="microseconds").replace("+00:00", "Z")
    _expire_monitors(store, current_text)
    harvested = _harvest_finished_jobs(store, observed_text)
    scheduled = _schedule_due(store, current)
    return {"harvested": harvested, "scheduled": scheduled}
