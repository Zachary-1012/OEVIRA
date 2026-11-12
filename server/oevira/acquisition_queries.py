"""Owner-private QueryPlans over current Materials, selected pages and URL Jobs."""
from __future__ import annotations

from collections import Counter
import base64
import copy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g, request

from . import api as _api
from .auth import utc_now
from .collaboration import require_material_access, require_work_access, work_commission
from .intelligence_base import IntelligenceError
from .intelligence_retrieval import _passage_ranges, _tokens


_MAX_SELECTED_MATERIALS = 500
_MAX_BROWSER_TARGETS = 8
_MAX_URL_TARGETS = 12
_MAX_QUERY_PATHS = 100
_MAX_BROWSER_HITS = 10
_MAX_PREVIEW_SECONDS = 15 * 60
_QUERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS acquisition_query_plans (
    query_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_id TEXT REFERENCES works(id),
    query_revision INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, owner_user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS acquisition_query_owner_recent
    ON acquisition_query_plans(workspace_id, owner_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS acquisition_query_paths (
    path_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES acquisition_query_plans(query_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    path_order INTEGER NOT NULL,
    parent_path_id TEXT,
    path_kind TEXT NOT NULL,
    data_surface TEXT NOT NULL,
    source_ref_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    material_refs_json TEXT NOT NULL DEFAULT '[]',
    child_job_id TEXT,
    result_json TEXT,
    status TEXT NOT NULL,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_ref_json TEXT,
    stop_reason TEXT,
    preview_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(query_id, path_order)
);
CREATE INDEX IF NOT EXISTS acquisition_query_paths_order
    ON acquisition_query_paths(query_id, path_order);
"""


def install_query_schema(store) -> None:
    with store.transaction() as connection:
        connection.executescript(_QUERY_SCHEMA)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value, expected_type=dict):
    try:
        result = json.loads(value or "null")
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, expected_type) else None


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _work_reference(connection, identity: dict, work_id: str) -> dict:
    work = require_work_access(connection, identity, work_id)
    commission_id = work_commission(connection, work_id)
    commission = None if commission_id is None else connection.execute(
        "SELECT version FROM client_commissions WHERE id=? AND workspace_id=?",
        (commission_id, identity["workspace"]["id"]),
    ).fetchone()
    return {
        "kind": "work", "work_id": work_id, "work_revision": work["version"],
        "client_scope": None if commission_id is None else {
            "commission_id": commission_id, "revision": commission["version"] if commission else None,
        },
    }


def _work_reference_for_read(store, identity: dict, work_id: str) -> dict:
    with store.transaction(immediate=False) as connection:
        return _work_reference(connection, identity, work_id)


def _encode_cursor(query: dict, path_id: str, offset: int) -> str:
    payload = {"query_id": query["query_id"], "query_revision": query["query_revision"],
               "request_hash": query["request_hash"], "path_id": path_id, "offset": offset}
    return base64.urlsafe_b64encode(_json(payload).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None, query: dict) -> tuple[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise _api.ApiError("QUERY_CURSOR_INVALID", "The query cursor is invalid", 400)
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise _api.ApiError("QUERY_CURSOR_INVALID", "The query cursor is invalid", 400) from exc
    if (not isinstance(payload, dict) or payload.get("query_id") != query["query_id"]
            or payload.get("query_revision") != query["query_revision"]
            or payload.get("request_hash") != query["request_hash"]
            or not isinstance(payload.get("path_id"), str)
            or isinstance(payload.get("offset"), bool) or not isinstance(payload.get("offset"), int)
            or payload["offset"] < 0):
        raise _api.ApiError("QUERY_CURSOR_INVALID", "The query cursor does not match this saved query", 409)
    return payload["path_id"], payload["offset"]


def _read_ref(value, field: str, *, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise _api.ApiError("INVALID_INPUT", f"{field} has an unsupported shape", 400, {"field": field})
    if len(_json(value)) > 4_000:
        raise _api.ApiError("INVALID_INPUT", f"{field} is too large", 400, {"field": field})
    return value


def _as_integer(value, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _api.ApiError("INVALID_INPUT", f"{field} must be an integer between {minimum} and {maximum}", 400,
                            {"field": field})
    return value


def _normalise_period(value, business_timezone: str) -> dict | None:
    if value is None:
        return None
    period = _read_ref(value, "period", allowed={"start", "end", "field"}, required={"start", "end"})
    field = period.get("field", "published_at")
    if field not in {"published_at", "modified_at", "event_at", "observed_at", "captured_at"}:
        raise _api.ApiError("INVALID_INPUT", "period.field is not a supported source time", 400,
                            {"field": "period.field"})
    try:
        zone = ZoneInfo(business_timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise _api.ApiError("INVALID_INPUT", "business_timezone must be a known IANA timezone", 400,
                            {"field": "business_timezone"}) from exc

    def parse_boundary(raw, name):
        if not isinstance(raw, str) or not raw or len(raw) > 64:
            raise _api.ApiError("INVALID_INPUT", f"period.{name} must be an ISO date or timestamp", 400,
                                {"field": f"period.{name}"})
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                day = date.fromisoformat(raw)
                if name == "end":
                    day += timedelta(days=1)
                parsed = datetime.combine(day, time.min).replace(tzinfo=zone)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
            return parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError) as exc:
            raise _api.ApiError("INVALID_INPUT", f"period.{name} is not a valid ISO date or timestamp", 400,
                                {"field": f"period.{name}"}) from exc

    start, end = parse_boundary(period["start"], "start"), parse_boundary(period["end"], "end")
    if start >= end:
        raise _api.ApiError("INVALID_INPUT", "period.start must be before period.end", 400)
    return {"start": period["start"], "end": period["end"], "field": field,
            "business_timezone": business_timezone,
            "normalized_start_utc": start.isoformat().replace("+00:00", "Z"),
            "normalized_end_exclusive_utc": end.isoformat().replace("+00:00", "Z")}


def _normalise_request(data: dict) -> dict:
    allowed = {
        "query", "work_context", "work_id", "subject_ref", "language", "allowed_sources",
        "business_timezone", "period", "required_data", "purpose", "freshness", "page", "budget",
    }
    if set(data) - allowed:
        raise _api.ApiError("INVALID_INPUT", "Search contains an unsupported field", 400,
                            {"fields": sorted(set(data) - allowed)})
    query = _api._raw_text(data.get("query"), "query", maximum=512)
    work_context = data.get("work_context")
    if work_context is not None:
        work_context = _read_ref(work_context, "work_context", allowed={"work_id"}, required={"work_id"})
        context_work_id = _api._string(work_context.get("work_id"), "work_context.work_id", maximum=80)
        if data.get("work_id") is not None and data.get("work_id") != context_work_id:
            raise _api.ApiError("INVALID_INPUT", "work_id conflicts with work_context.work_id", 400)
        work_id = context_work_id
    else:
        work_id = _api._optional_string(data.get("work_id"), "work_id", maximum=80)
    subject_ref = data.get("subject_ref")
    if subject_ref is not None:
        subject_ref = _read_ref(subject_ref, "subject_ref", allowed={
            "subject_id", "id", "version", "revision", "name", "kind", "workspace_id", "work_id",
        })
    language = _api._optional_string(data.get("language"), "language", maximum=40)
    timezone_name = _api._optional_string(data.get("business_timezone"), "business_timezone", maximum=80) or "UTC"
    period = _normalise_period(data.get("period"), timezone_name)
    required_data = data.get("required_data", ["text"])
    if (not isinstance(required_data, list) or len(required_data) > 32
            or any(not isinstance(item, str) or not item or len(item) > 80 for item in required_data)
            or len(set(required_data)) != len(required_data)):
        raise _api.ApiError("INVALID_INPUT", "required_data must contain distinct data-surface names", 400)
    purpose = _api._string(data.get("purpose", "knowledge_retrieval"), "purpose", maximum=120)
    freshness = data.get("freshness", {})
    freshness = _read_ref(freshness, "freshness", allowed={"max_age_seconds", "require_latest"})
    if "max_age_seconds" in freshness:
        freshness["max_age_seconds"] = _as_integer(
            freshness["max_age_seconds"], "freshness.max_age_seconds", minimum=0, maximum=365 * 24 * 60 * 60,
        )
    if "require_latest" in freshness and type(freshness["require_latest"]) is not bool:
        raise _api.ApiError("INVALID_INPUT", "freshness.require_latest must be a boolean", 400)
    page = _read_ref(data.get("page", {}), "page", allowed={"size", "cursor"})
    page_size = _as_integer(page.get("size", 10), "page.size", minimum=1, maximum=50)
    if page.get("cursor") not in (None, ""):
        raise _api.ApiError("QUERY_CURSOR_UNAVAILABLE", "This query has no compatible continuation cursor", 409)
    budget = _read_ref(data.get("budget", {}), "budget", allowed={"max_paths", "max_browser_hits"})
    max_paths = _as_integer(budget.get("max_paths", _MAX_QUERY_PATHS), "budget.max_paths", minimum=1,
                             maximum=_MAX_QUERY_PATHS)
    max_browser_hits = _as_integer(budget.get("max_browser_hits", _MAX_BROWSER_HITS),
                                   "budget.max_browser_hits", minimum=1, maximum=_MAX_BROWSER_HITS)

    sources = data.get("allowed_sources")
    if not isinstance(sources, dict):
        raise _api.ApiError("INVALID_INPUT", "allowed_sources must explicitly select source scopes", 400)
    source_keys = {"workspace", "public", "connected", "material_ids", "public_urls", "browser_targets"}
    if set(sources) - source_keys:
        raise _api.ApiError("INVALID_INPUT", "allowed_sources contains an unsupported scope", 400,
                            {"fields": sorted(set(sources) - source_keys)})
    for flag in ("workspace", "public", "connected"):
        if flag in sources and type(sources[flag]) is not bool:
            raise _api.ApiError("INVALID_INPUT", f"allowed_sources.{flag} must be a boolean", 400)
    material_ids = sources.get("material_ids", [])
    if (not isinstance(material_ids, list) or len(material_ids) > _MAX_SELECTED_MATERIALS
            or any(not isinstance(item, str) or not item or len(item) > 80 for item in material_ids)
            or len(set(material_ids)) != len(material_ids)):
        raise _api.ApiError("INVALID_INPUT", "allowed_sources.material_ids must contain distinct Material IDs", 400)
    material_ids = sorted(material_ids)
    public_urls = sources.get("public_urls", [])
    if (not isinstance(public_urls, list) or len(public_urls) > _MAX_URL_TARGETS
            or any(not isinstance(item, str) or not item or len(item) > 4096 for item in public_urls)
            or len(set(public_urls)) != len(public_urls)):
        raise _api.ApiError("INVALID_INPUT", "allowed_sources.public_urls must contain distinct public URLs", 400)
    for url in public_urls:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise _api.ApiError("INVALID_INPUT", "A selected URL is malformed", 400) from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or port not in {None, 80, 443} or any(ord(character) < 32 for character in url)):
            raise _api.ApiError("INVALID_INPUT", "A selected URL must be public HTTP(S) on a standard port", 400)
    browser_targets = sources.get("browser_targets", [])
    if not isinstance(browser_targets, list) or len(browser_targets) > _MAX_BROWSER_TARGETS:
        raise _api.ApiError("INVALID_INPUT", "allowed_sources.browser_targets must contain at most 8 pages", 400)
    normalized_targets = []
    target_keys = set()
    for index, target in enumerate(browser_targets):
        target = _read_ref(target, f"allowed_sources.browser_targets[{index}]", allowed={
            "session_id", "base_version", "page_id", "page_revision", "include_frames",
            "expected_control_generation",
        }, required={"session_id", "base_version", "page_id", "page_revision"})
        session_id = _api._string(target["session_id"], "browser_target.session_id", maximum=80)
        page_id = _api._string(target["page_id"], "browser_target.page_id", maximum=80)
        base_version = _as_integer(target["base_version"], "browser_target.base_version", minimum=1,
                                   maximum=2**31 - 1)
        page_revision = _as_integer(target["page_revision"], "browser_target.page_revision", minimum=0,
                                    maximum=2**31 - 1)
        include_frames = target.get("include_frames", True)
        if type(include_frames) is not bool:
            raise _api.ApiError("INVALID_INPUT", "browser_target.include_frames must be boolean", 400)
        control_generation = target.get("expected_control_generation")
        if control_generation is not None:
            control_generation = _as_integer(control_generation, "browser_target.expected_control_generation",
                                             minimum=1, maximum=2**31 - 1)
        key = (session_id, page_id, page_revision)
        if key in target_keys:
            raise _api.ApiError("INVALID_INPUT", "browser_targets contains a duplicate page", 400)
        target_keys.add(key)
        normalized_targets.append({
            "session_id": session_id, "base_version": base_version, "page_id": page_id,
            "page_revision": page_revision, "include_frames": include_frames,
            "expected_control_generation": control_generation,
        })
    if not any((sources.get("workspace", False), sources.get("public", False),
                sources.get("connected", False), material_ids, public_urls, normalized_targets)):
        raise _api.ApiError("SOURCE_SCOPE_REQUIRED", "Select at least one source scope", 400)
    sources = {
        "workspace": sources.get("workspace", False), "public": sources.get("public", False),
        "connected": sources.get("connected", False), "material_ids": material_ids,
        "public_urls": public_urls, "browser_targets": normalized_targets,
    }
    return {
        "query": query, "work_id": work_id, "subject_ref": subject_ref, "language": language,
        "allowed_sources": sources, "business_timezone": timezone_name, "period": period,
        "required_data": required_data, "purpose": purpose, "freshness": freshness,
        "page": {"size": page_size, "cursor": None},
        "budget": {"max_paths": max_paths, "max_browser_hits": max_browser_hits},
    }


def _source_use_for_local_search(connection, identity: dict, row, work_id: str | None,
                                 purpose: str) -> dict:
    source = _loads(row["source_json"], dict) or {}
    has_declared_use = any(key in source for key in (
        "source_use", "source_use_decision", "source_use_decision_id",
    ))
    try:
        from .source_use import resolve_source_use
    except ImportError:
        return {"allowed": False, "state": "unavailable", "reason_code": "SOURCE_USE_OWNER_UNAVAILABLE"}
    try:
        decision = resolve_source_use(
            connection, identity,
            {"kind": "material", "id": row["id"], "revision": row["sha256"]},
            field_paths=["text"], purpose=purpose, action="search",
            recipient={"kind": "local", "id": identity["workspace"]["id"]}, work_id=work_id,
        )
        fields = {
            "allowed": getattr(decision, "allowed", None),
            "state": getattr(decision, "state", None),
            "basis_status": getattr(decision, "basis_status", None),
            "decision_id": getattr(decision, "decision_id", None),
            "version": getattr(decision, "version", None),
            "source_ref_sha256": getattr(decision, "source_ref_sha256", None),
            "policy_sha256": getattr(decision, "policy_sha256", None),
            "valid_until": getattr(decision, "valid_until", None),
            "reason_code": getattr(decision, "reason_code", None),
        }
        if type(fields["allowed"]) is not bool or not isinstance(fields["state"], str):
            return {"allowed": False, "state": "unavailable", "reason_code": "SOURCE_USE_DECISION_INVALID"}
        if has_declared_use and fields["allowed"] is True and fields["decision_id"] is None:
            return {**fields, "allowed": False, "state": "unresolved",
                    "reason_code": "SOURCE_USE_DECISION_UNRESOLVED"}
        return fields
    except Exception:
        return {"allowed": False, "state": "unavailable", "reason_code": "SOURCE_USE_CHECK_UNAVAILABLE"}


def _material_signature(connection, row, identity: dict, work_id: str | None,
                        purpose: str = "knowledge_retrieval") -> dict:
    segments = connection.execute(
        "SELECT id,label,text,start_offset,end_offset,kind FROM material_segments "
        "WHERE material_id=? AND workspace_id=? ORDER BY start_offset,id",
        (row["id"], row["workspace_id"]),
    ).fetchall()
    source_use = _source_use_for_local_search(connection, identity, row, work_id, purpose)
    return {
        "material_id": row["id"], "source_work_id": row["work_id"], "sha256": row["sha256"],
        "projection_sha256": hashlib.sha256((row["text"] or "").encode("utf-8")).hexdigest(),
        "segments_sha256": _hash([dict(segment) for segment in segments]),
        "source_use_state": source_use["state"], "source_use_decision": source_use,
    }


def _source_use_allowed(signature: dict) -> bool:
    decision = signature.get("source_use_decision")
    return isinstance(decision, dict) and decision.get("allowed") is True


def _source_use_block_reason(signature: dict) -> str:
    decision = signature.get("source_use_decision")
    if isinstance(decision, dict) and isinstance(decision.get("reason_code"), str) and decision["reason_code"]:
        return decision["reason_code"]
    return "SOURCE_USE_OWNER_NOT_BOUND" if signature.get("source_use_state") == "unresolved" else "SOURCE_USE_NOT_AUTHORIZED"


def _stamp_source_use(result: dict | None, material_refs: list[dict]) -> None:
    if not isinstance(result, dict) or not material_refs:
        return
    decisions = {
        item["material_id"]: item.get("source_use_decision", {})
        for item in material_refs if isinstance(item, dict) and isinstance(item.get("material_id"), str)
    }
    groups = [result.get("evidence", []), result.get("citations", []), result.get("sources", [])]
    material_result = result.get("material_result")
    if isinstance(material_result, dict):
        groups.extend([material_result.get("evidence", []), material_result.get("citations", []),
                       material_result.get("sources", [])])
    for items in groups:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("material_id") not in decisions:
                continue
            decision = decisions[item["material_id"]]
            item["source_use_state"] = decision.get("state")
            item["source_use_decision"] = decision


def _date_value(raw, timezone_name: str):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.combine(date.fromisoformat(raw), time.min).replace(tzinfo=ZoneInfo(timezone_name))
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return parsed.astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError, OverflowError):
        return None


def _trusted_source_time(store, identity: dict, material: dict, target_work_id: str | None, field: str):
    """Only use a source time for filtering after a persisted public HTTP receipt verifies it."""
    if not material.get("source_url"):
        return None, "unverified"
    try:
        from .monitoring import _verified_material
        verified, _reason = _verified_material(
            store, identity["workspace"]["id"], material["id"], material["source_url"],
            identity, target_work_id,
        )
    except Exception:
        return None, "unverified"
    if verified is None:
        return None, "unverified"
    metadata = verified.get("source_metadata", {})
    if field == "observed_at":
        raw = verified.get("receipt_completed_at")
    else:
        raw = metadata.get(field)
    if raw is None:
        return None, "unknown"
    return raw, "verified_receipt" if field == "observed_at" else "source_reported_in_verified_capture"


def _workspace_scope_paths(store, identity: dict, request_data: dict) -> tuple[list[dict], dict]:
    workspace_id = identity["workspace"]["id"]
    target_work_id = request_data["work_id"]
    selected_ids = request_data["allowed_sources"]["material_ids"]
    workspace_enabled = request_data["allowed_sources"]["workspace"]
    if target_work_id:
        with store.transaction(immediate=False) as connection:
            try:
                work_ref = _work_reference(connection, identity, target_work_id)
            except _api.ApiError as exc:
                raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404) from exc
            if not workspace_enabled and not selected_ids:
                return [], {"work_id": target_work_id, "work_ref": work_ref,
                            "workspace": False, "material_ids": []}
            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                rows = connection.execute(
                    "SELECT m.* FROM materials m JOIN work_materials wm ON wm.material_id=m.id "
                    "AND wm.workspace_id=m.workspace_id WHERE wm.workspace_id=? AND wm.work_id=? "
                    f"AND m.id IN ({placeholders}) ORDER BY m.id",
                    [workspace_id, target_work_id, *selected_ids],
                ).fetchall()
                if {row["id"] for row in rows} != set(selected_ids):
                    raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404)
            else:
                rows = connection.execute(
                    "SELECT m.* FROM materials m JOIN work_materials wm ON wm.material_id=m.id "
                    "AND wm.workspace_id=m.workspace_id WHERE wm.workspace_id=? AND wm.work_id=? "
                    "ORDER BY m.id", (workspace_id, target_work_id),
                ).fetchall()
            authorized = []
            blocked_use = 0
            blocked_reason = None
            for row in rows:
                try:
                    require_material_access(connection, identity, row["id"], target_work_id=target_work_id)
                except _api.ApiError:
                    if selected_ids:
                        raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404)
                    continue
                signature = _material_signature(connection, row, identity, target_work_id,
                                                request_data["purpose"])
                if not _source_use_allowed(signature):
                    blocked_use += 1
                    blocked_reason = blocked_reason or _source_use_block_reason(signature)
                else:
                    authorized.append((dict(row), signature))
        paths = _prepare_material_paths(
            store, identity, request_data, target_work_id, authorized,
            source_ref=work_ref, blocked_use=blocked_use, blocked_reason=blocked_reason,
        )
        return paths, {"work_id": target_work_id, "work_ref": work_ref, "workspace": workspace_enabled,
                       "material_ids": selected_ids}

    if selected_ids:
        with store.transaction(immediate=False) as connection:
            placeholders = ",".join("?" for _ in selected_ids)
            rows = connection.execute(
                f"SELECT * FROM materials WHERE workspace_id=? AND id IN ({placeholders}) ORDER BY id",
                [workspace_id, *selected_ids],
            ).fetchall()
            if {row["id"] for row in rows} != set(selected_ids):
                raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404)
            groups: dict[str | None, list] = {}
            blocked: Counter[str | None] = Counter()
            blocked_reason_by_work: dict[str | None, str] = {}
            for row in rows:
                try:
                    require_material_access(connection, identity, row["id"])
                except _api.ApiError as exc:
                    raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404) from exc
                signature = _material_signature(connection, row, identity, row["work_id"],
                                                request_data["purpose"])
                key = row["work_id"]
                if not _source_use_allowed(signature):
                    blocked[key] += 1
                    blocked_reason_by_work.setdefault(key, _source_use_block_reason(signature))
                    continue
                groups.setdefault(key, []).append((dict(row), signature))
            for work_id, values in groups.items():
                if work_id:
                    try:
                        require_work_access(connection, identity, work_id)
                    except _api.ApiError:
                        continue
        paths = []
        for source_work_id, values in groups.items():
            source_ref = (_work_reference_for_read(store, identity, source_work_id) if source_work_id else
                          {"kind": "unbound_materials", "work_id": None})
            group_paths = _prepare_material_paths(
                store, identity, request_data, None, values,
                source_ref=source_ref,
                blocked_use=blocked[source_work_id],
                blocked_reason=blocked_reason_by_work.get(source_work_id),
            )
            paths.extend(group_paths)
        for source_work_id, count in blocked.items():
            if source_work_id not in groups and count:
                source_ref = (_work_reference_for_read(store, identity, source_work_id) if source_work_id else
                              {"kind": "unbound_materials", "work_id": None})
                paths.append(_blocked_material_path(
                    source_work_id, count, source_ref=source_ref,
                    reason=blocked_reason_by_work.get(source_work_id),
                ))
        return paths, {"work_id": None, "workspace": False, "material_ids": selected_ids}

    if not workspace_enabled:
        return [], {"work_id": None, "workspace": False, "material_ids": []}

    with store.transaction(immediate=False) as connection:
        rows = connection.execute(
            "SELECT * FROM materials WHERE workspace_id=? ORDER BY work_id, id", (workspace_id,),
        ).fetchall()
        groups: dict[str | None, list] = {}
        blocked: Counter[str | None] = Counter()
        blocked_reason_by_work: dict[str | None, str] = {}
        for row in rows:
            try:
                require_material_access(connection, identity, row["id"])
            except _api.ApiError:
                continue
            signature = _material_signature(connection, row, identity, row["work_id"],
                                            request_data["purpose"])
            work_id = row["work_id"]
            if not _source_use_allowed(signature):
                blocked[work_id] += 1
                blocked_reason_by_work.setdefault(work_id, _source_use_block_reason(signature))
                continue
            groups.setdefault(work_id, []).append((dict(row), signature))
    paths = []
    for source_work_id, values in groups.items():
        if source_work_id:
            try:
                source_ref = _work_reference_for_read(store, identity, source_work_id)
            except _api.ApiError:
                continue
        else:
            source_ref = {"kind": "unbound_materials", "work_id": None}
        group_paths = _prepare_material_paths(
            store, identity, request_data, None, values,
            source_ref=source_ref,
            blocked_use=blocked[source_work_id],
            blocked_reason=blocked_reason_by_work.get(source_work_id),
        )
        paths.extend(group_paths)
    for source_work_id, count in blocked.items():
        if source_work_id not in groups and count:
            source_ref = (_work_reference_for_read(store, identity, source_work_id) if source_work_id else
                          {"kind": "unbound_materials", "work_id": None})
            paths.append(_blocked_material_path(
                source_work_id, count, source_ref=source_ref,
                reason=blocked_reason_by_work.get(source_work_id),
            ))
    if not paths:
        paths.append(_prepare_material_path(
            store, identity, request_data, None, [],
            source_ref={"kind": "workspace_materials", "work_id": None}, blocked_use=0,
        ))
    return paths, {"work_id": None, "workspace": True, "material_ids": []}


def _blocked_material_path(source_work_id: str | None, count: int, *, source_ref: dict | None = None,
                           reason: str | None = None) -> dict:
    return {
        "path_kind": "material_search", "data_surface": "material_text",
        "source_ref": source_ref or {"kind": "work" if source_work_id else "unbound_materials",
                                     "work_id": source_work_id},
        "request": {"work_id": None, "material_ids": [], "withheld_source_use": count},
        "material_refs": [], "status": "blocked", "coverage": {"withheld_for_source_use": count},
        "stop_reason": reason or "SOURCE_USE_OWNER_NOT_BOUND",
    }


def _prepare_material_paths(store, identity: dict, request_data: dict, target_work_id: str | None,
                            material_rows: list[tuple[dict, dict]], *, source_ref: dict,
                            blocked_use: int, blocked_reason: str | None = None) -> list[dict]:
    if not material_rows:
        path = _prepare_material_path(store, identity, request_data, target_work_id, [],
                                      source_ref=source_ref, blocked_use=blocked_use,
                                      blocked_reason=blocked_reason)
        return [path] if path else []
    chunk_size = _MAX_SELECTED_MATERIALS
    chunks = [material_rows[offset:offset + chunk_size]
              for offset in range(0, len(material_rows), chunk_size)]
    paths = []
    for index, chunk in enumerate(chunks):
        path = _prepare_material_path(
            store, identity, request_data, target_work_id, chunk,
            source_ref=source_ref, blocked_use=blocked_use if index == 0 else 0,
            blocked_reason=blocked_reason if index == 0 else None,
        )
        if path:
            path["request"]["chunk_index"] = index
            if len(chunks) > 1:
                path["coverage"]["chunk_index"] = index
                path["coverage"]["chunk_count"] = len(chunks)
            paths.append(path)
    return paths


def _prepare_material_path(store, identity: dict, request_data: dict, target_work_id: str | None,
                           material_rows: list[tuple[dict, dict]], *, source_ref: dict,
                           blocked_use: int, blocked_reason: str | None = None) -> dict | None:
    allowed: list[tuple[dict, dict]] = []
    unknown_time = stale_time = freshness_unverified = freshness_stale = 0
    period = request_data["period"]
    freshness = request_data["freshness"]
    now = datetime.now(timezone.utc)
    for material, signature in material_rows:
        if period:
            raw, time_state = _trusted_source_time(
                store, identity, material, target_work_id, period["field"],
            )
            parsed = _date_value(raw, request_data["business_timezone"]) if raw else None
            start = _date_value(period["normalized_start_utc"], "UTC")
            end = _date_value(period["normalized_end_exclusive_utc"], "UTC")
            if parsed is None:
                unknown_time += 1
                continue
            if not start <= parsed < end:
                stale_time += 1
                continue
            signature = {**signature, "time_value": raw, "time_state": time_state}
        if freshness.get("require_latest"):
            freshness_unverified += 1
            continue
        if "max_age_seconds" in freshness:
            raw, time_state = _trusted_source_time(store, identity, material, target_work_id, "observed_at")
            parsed = _date_value(raw, "UTC") if raw else None
            max_age = freshness.get("max_age_seconds")
            if parsed is None:
                freshness_unverified += 1
                continue
            if max_age is not None and (now - parsed).total_seconds() > max_age:
                freshness_stale += 1
                continue
            signature = {**signature, "time_value": raw, "time_state": time_state}
        allowed.append((material, signature))
    material_refs = [signature for _, signature in allowed]
    status = "not_started" if material_refs else "blocked" if blocked_use else (
        "partial" if unknown_time or freshness_unverified or freshness_stale else "no_matches"
    )
    stop_reason = ((blocked_reason or "SOURCE_USE_OWNER_NOT_BOUND") if blocked_use and not material_refs else
                   "FRESHNESS_REQUIRES_NEW_OBSERVATION" if freshness.get("require_latest") and not material_refs else
                   "FRESHNESS_UNVERIFIED" if freshness_unverified and not material_refs else
                   "FRESHNESS_WINDOW_MISSED" if freshness_stale and not material_refs else
                   "TIME_BASIS_UNVERIFIED" if unknown_time and not material_refs else
                   "NO_MATERIALS_IN_PERIOD" if stale_time and not material_refs else
                   "NO_MATERIALS_IN_SCOPE" if not material_refs and not unknown_time and not blocked_use else None)
    unsupported = sorted(set(request_data["required_data"]) - {"text"})
    coverage = {
        "scoped_materials": len(material_refs), "withheld_for_source_use": blocked_use,
        "unknown_time_materials": unknown_time, "outside_period_materials": stale_time,
        "freshness_unverified_materials": freshness_unverified,
        "freshness_stale_materials": freshness_stale,
        "requested_data": request_data["required_data"], "supported_data": ["text"],
        "missing_data": unsupported,
        "subject_scope_state": "not_resolved" if request_data["subject_ref"] else "not_requested",
        "freshness_state": ("not_satisfied" if freshness.get("require_latest") else
                            "receipt_checked" if "max_age_seconds" in freshness else "saved_material_only"),
    }
    return {
        "path_kind": "material_search", "data_surface": "material_text",
        "source_ref": source_ref,
        "request": {"query": request_data["query"], "language": request_data["language"],
                    "work_id": target_work_id, "material_ids": [row["id"] for row, _ in allowed],
                    "limit": request_data["page"]["size"], "purpose": request_data["purpose"]},
        "material_refs": material_refs, "status": status, "coverage": coverage,
        "stop_reason": stop_reason or ("REQUIRED_DATA_SURFACE_UNAVAILABLE" if unsupported else None),
    }


def _browser_query_result(snapshot: dict, query: str, limit: int) -> dict:
    query_tokens = set(_tokens(query))
    if not query_tokens:
        return {"status": "blocked", "coverage": {"status": "unavailable"}, "results": [],
                "stop_reason": "QUERY_HAS_NO_SEARCHABLE_TERMS"}
    frames = snapshot.get("frames", [])
    text_units = [("main", snapshot.get("text", ""), "main", snapshot.get("coverage", {}))]
    for frame in frames:
        if frame.get("frame_id") == "main" or frame.get("status") not in {"read", "empty", "partial"}:
            continue
        text_units.append((frame.get("frame_id"), frame.get("text", ""), frame.get("url"), frame.get("coverage", {})))
    found = []
    for frame_id, text, frame_url, frame_coverage in text_units:
        if not isinstance(text, str) or not text:
            continue
        for start, end in _passage_ranges(text):
            quote = text[start:end]
            counts = Counter(_tokens(quote))
            matched = sorted(query_tokens & counts.keys())
            if not matched:
                continue
            found.append({
                "frame_id": frame_id, "frame_url": frame_url, "start_offset": start,
                "end_offset": end, "offset_unit": "unicode_codepoint", "quote": quote,
                "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                "matched_terms": matched, "match_count": sum(counts[item] for item in matched),
                "evidence_state": "UNVERIFIED_SOURCE_TEXT",
            })
    found.sort(key=lambda item: (-item["match_count"], item["frame_id"], item["start_offset"]))
    coverage = snapshot.get("coverage", {})
    status = "complete" if coverage.get("status") == "complete" else "partial"
    if not found and status == "complete":
        status = "no_matches"
    more_available = len(found) > limit
    if more_available:
        status = "partial"
    return {
        "status": status,
        "retrieval": {"method": "reader_token_overlap_v1", "ranked_by": "matched query token count",
                      "search_text_only": True},
        "coverage": {**coverage, "matched_windows": len(found), "returned_windows": min(len(found), limit),
                     "more_available": more_available,
                     "complete_page_scope_scanned": coverage.get("status") == "complete"},
        "results": found[:limit],
        "stop_reason": "BROWSER_HIT_BUDGET_REACHED" if more_available else None,
        "limitations": list(coverage.get("limitations", [])) + [
            "Reader results are bounded visible DOM text; this path did not save a Material or verify source rights for reuse.",
        ],
        "source_use_state": "NOT_EVALUATED_FOR_EXTERNAL_PROCESSING",
    }


def _browser_time_eligibility(snapshot: dict, plan: dict) -> tuple[bool, dict, str | None]:
    captured_at = snapshot.get("snapshot_version", {}).get("captured_at")
    needs_observation = bool(plan.get("period") or plan.get("freshness"))
    if not needs_observation:
        return True, {"freshness_state": "not_requested", "time_basis_state": "not_requested"}, None
    captured = _date_value(captured_at, "UTC") if isinstance(captured_at, str) else None
    if captured is None:
        return False, {"freshness_state": "unknown", "time_basis_state": "unknown"}, "BROWSER_OBSERVATION_TIME_UNAVAILABLE"
    coverage = {"observed_at": captured_at, "freshness_state": "observed_now"}
    period = plan.get("period")
    if period:
        if period.get("field") not in {"observed_at", "captured_at"}:
            coverage["time_basis_state"] = "unavailable_for_selected_browser_page"
            return False, coverage, "TIME_BASIS_UNAVAILABLE_FOR_SELECTED_BROWSER_PAGE"
        start = _date_value(period.get("normalized_start_utc"), "UTC")
        end = _date_value(period.get("normalized_end_exclusive_utc"), "UTC")
        if start is None or end is None:
            coverage["time_basis_state"] = "invalid"
            return False, coverage, "TIME_BASIS_UNAVAILABLE_FOR_SELECTED_BROWSER_PAGE"
        if not start <= captured < end:
            coverage["time_basis_state"] = "outside_requested_period"
            return False, coverage, "BROWSER_OBSERVATION_OUTSIDE_PERIOD"
        coverage["time_basis_state"] = "matched_observation_time"
    freshness = plan.get("freshness", {})
    if "max_age_seconds" in freshness and (
        datetime.now(timezone.utc) - captured
    ).total_seconds() > freshness["max_age_seconds"]:
        coverage["freshness_state"] = "outside_max_age"
        return False, coverage, "BROWSER_OBSERVATION_OUTSIDE_FRESHNESS_WINDOW"
    return True, coverage, None


def _project_browser_preview(preview: dict, plan: dict, capture_ref: dict, *, limit: int,
                             source_use: dict) -> tuple[dict, str, str | None]:
    result = _browser_query_result(preview, plan["query"], limit)
    time_eligible, time_coverage, time_stop = _browser_time_eligibility(preview, plan)
    result["coverage"].update(time_coverage)
    stop_reason = None
    if not time_eligible:
        result["status"] = "partial"
        result["results"] = []
        stop_reason = time_stop
    if plan.get("subject_ref"):
        result["coverage"]["subject_scope_state"] = "not_resolved"
        result.setdefault("limitations", []).append(
            "The selected SubjectRef was retained but not resolved on this query path."
        )
        result["status"] = "partial"
        stop_reason = "SUBJECT_ASSOCIATION_OWNER_NOT_BOUND"
    missing_data = sorted(set(plan.get("required_data", [])) - {"text"})
    if missing_data:
        result["coverage"]["missing_data"] = missing_data
        result["status"] = "partial"
        stop_reason = "REQUIRED_DATA_SURFACE_UNAVAILABLE"
    result["source_use_state"] = source_use.get("state", "unavailable")
    result["source_use_decision"] = source_use
    result["source_ref"] = {**capture_ref, "kind": "browser_page",
                            "url": preview.get("url"), "title": preview.get("title")}
    return result, result.get("status", "partial"), stop_reason


def _source_public_ref(url: str) -> dict:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    host = parsed.hostname.encode("idna").decode("ascii") if parsed.hostname else ""
    return {"kind": "public_url", "host": host, "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest()}


def _plan_paths(store, identity: dict, request_data: dict) -> tuple[list[dict], dict]:
    paths, scope_used = _workspace_scope_paths(store, identity, request_data)
    sources = request_data["allowed_sources"]
    if sources["public"]:
        paths.append({
            "path_kind": "public_search", "data_surface": "web_search",
            "source_ref": {"kind": "authorized_search_provider", "provider": None},
            "request": {"query": request_data["query"], "language": request_data["language"],
                        "period": request_data["period"]},
            "material_refs": [], "status": "unavailable",
            "coverage": {"status": "unavailable", "reason": "No authorized external search provider is connected."},
            "stop_reason": "SEARCH_PROVIDER_NOT_BOUND",
        })
    if sources["connected"]:
        paths.append({
            "path_kind": "connected_source_search", "data_surface": "connected_platform",
            "source_ref": {"kind": "connected_sources", "provider": None},
            "request": {"query": request_data["query"], "purpose": request_data["purpose"]},
            "material_refs": [], "status": "unavailable",
            "coverage": {"status": "unavailable", "reason": "No authorized connected-source query adapter is bound."},
            "stop_reason": "CONNECTED_SOURCE_QUERY_NOT_BOUND",
        })
    for index, target in enumerate(sources["browser_targets"]):
        paths.append({
            "path_kind": "browser_page", "data_surface": "selected_browser_page",
            "source_ref": {"kind": "browser_page", "session_id": target["session_id"],
                           "page_id": target["page_id"], "page_revision": target["page_revision"]},
            "request": {"query": request_data["query"], "target_index": index, "target": target,
                        "query_work_id": request_data["work_id"], "limit": request_data["budget"]["max_browser_hits"]},
            "material_refs": [], "status": "not_started", "coverage": {}, "stop_reason": None,
            "preview_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=_MAX_PREVIEW_SECONDS))
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        })
    for index, url in enumerate(sources["public_urls"]):
        paths.append({
            "path_kind": "public_url_acquisition", "data_surface": "public_http",
            "source_ref": _source_public_ref(url),
            "request": {"url": url, "query": request_data["query"], "query_work_id": request_data["work_id"],
                        "target_index": index},
            "material_refs": [], "status": "not_started", "coverage": {}, "stop_reason": None,
        })
    max_paths = request_data["budget"]["max_paths"]
    # User-selected Browser/URL routes are explicit roots and must remain
    # visible even when workspace expansion reaches its path budget.
    material_paths = [path for path in paths if path["path_kind"] == "material_search"]
    selected_paths = [path for path in paths if path["path_kind"] != "material_search"]
    scope_budget_path = None
    if len(material_paths) > max_paths:
        material_paths = material_paths[:max_paths]
        scope_budget_path = {
            "path_kind": "scope_budget", "data_surface": "query_plan",
            "source_ref": {"kind": "query_scope"}, "request": {}, "material_refs": [],
            "status": "partial", "coverage": {"status": "partial"}, "stop_reason": "PATH_BUDGET_REACHED",
        }
    paths = [*material_paths, *selected_paths]
    if scope_budget_path:
        paths.append(scope_budget_path)
    return paths, scope_used


def _persist_query_plan(store, identity: dict, normalized: dict, idempotency_key: str):
    workspace_id = identity["workspace"]["id"]
    user_id = identity["user"]["id"]
    fingerprint = _hash(normalized)
    now = utc_now()
    existing = store.row(
        "SELECT * FROM acquisition_query_plans WHERE workspace_id=? AND owner_user_id=? AND idempotency_key=?",
        (workspace_id, user_id, idempotency_key),
    )
    if existing is not None:
        if existing["request_hash"] != fingerprint:
            raise _api.ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different query", 409)
        return dict(existing), False
    # Scope discovery opens its own read transactions. Keep it outside the write
    # transaction, then recheck the selected Work before committing the plan.
    planned_paths, scope_used = _plan_paths(store, identity, normalized)
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM acquisition_query_plans WHERE workspace_id=? AND owner_user_id=? AND idempotency_key=?",
            (workspace_id, user_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != fingerprint:
                raise _api.ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different query", 409)
            return dict(existing), False
        if normalized["work_id"]:
            try:
                require_work_access(connection, identity, normalized["work_id"])
            except _api.ApiError as exc:
                raise _api.ApiError("NOT_FOUND", "The requested resource was not found", 404) from exc
        if not planned_paths:
            planned_paths.append({
                "path_kind": "scope", "data_surface": "query_plan", "source_ref": {"kind": "selected_scope"},
                "request": {}, "material_refs": [], "status": "unavailable", "coverage": {},
                "stop_reason": "SELECTED_SCOPE_HAS_NO_AVAILABLE_LOCAL_PATH",
            })
        query_id = str(uuid.uuid4())
        saved_request = {**normalized, "scope_used": scope_used}
        connection.execute(
            "INSERT INTO acquisition_query_plans(query_id,workspace_id,owner_user_id,work_id,query_revision,"
            "idempotency_key,request_hash,request_json,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?,?,?)",
            (query_id, workspace_id, user_id, normalized["work_id"], idempotency_key, fingerprint,
             _json(saved_request), now, now),
        )
        for order, path in enumerate(planned_paths):
            path_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,status,coverage_json,"
                "stop_reason,preview_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (path_id, query_id, workspace_id, order, None, path["path_kind"], path["data_surface"],
                 _json(path["source_ref"]), _json(path["request"]), _json(path.get("material_refs", [])),
                 path["status"], _json(path.get("coverage", {})), path.get("stop_reason"),
                 path.get("preview_expires_at"), now, now),
            )
        row = connection.execute("SELECT * FROM acquisition_query_plans WHERE query_id=?", (query_id,)).fetchone()
        return dict(row), True


def _update_path(store, path_id: str, workspace_id: str, *, status: str, coverage: dict | None = None,
                 result: dict | None = None, stop_reason: str | None = None, child_job_id: str | None = None,
                 checkpoint_ref: dict | None = None, source_ref: dict | None = None,
                 preview_expires_at: str | None = None) -> None:
    now = utc_now()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE acquisition_query_paths SET status=?,coverage_json=?,result_json=?,stop_reason=?,"
            "child_job_id=COALESCE(?,child_job_id),checkpoint_ref_json=?,source_ref_json=COALESCE(?,source_ref_json),"
            "preview_expires_at=COALESCE(?,preview_expires_at),updated_at=? "
            "WHERE path_id=? AND workspace_id=?",
            (status, _json(coverage or {}), _json(result) if result is not None else None, stop_reason,
             child_job_id, _json(checkpoint_ref) if checkpoint_ref is not None else None,
             _json(source_ref) if source_ref is not None else None,
             preview_expires_at,
             now, path_id, workspace_id),
        )


def _source_work_matches_target(connection, identity: dict, source_work_id: str | None,
                                target_work_id: str | None) -> bool:
    if not source_work_id or not target_work_id:
        return True
    source_scope = work_commission(connection, source_work_id)
    target_scope = work_commission(connection, target_work_id)
    if source_scope is not None and source_scope != target_scope:
        return False
    return True


def _execute_material_path(app, identity: dict, row: dict, plan: dict) -> None:
    store = app.extensions["oevira.store"]
    request_data = _loads(row["request_json"], dict) or {}
    refs = _loads(row["material_refs_json"], list) or []
    target_work_id = request_data.get("work_id")
    material_ids = [item.get("material_id") for item in refs if isinstance(item, dict)]
    if not material_ids:
        result = {
            "status": row["status"], "query": plan["query"], "scope": {"work_id": target_work_id,
            "material_ids": []}, "coverage": _loads(row["coverage_json"], dict) or {},
            "evidence": [], "citations": [], "limitations": [],
        }
        return _update_path(store, row["path_id"], identity["workspace"]["id"], status=row["status"],
                            coverage=result["coverage"], result=result, stop_reason=row["stop_reason"])
    try:
        evidence = app.extensions["oevira.intelligence"].search_materials(
            identity, plan["query"], work_id=target_work_id, material_ids=material_ids,
            limit=50,
        )
    except IntelligenceError as exc:
        status = "blocked" if exc.status in {403, 404} else "unavailable"
        return _update_path(store, row["path_id"], identity["workspace"]["id"], status=status,
                            coverage={"complete_scope_scanned": False}, result={"status": status, "evidence": [],
                            "citations": [], "error": {"code": exc.code}}, stop_reason=exc.code)
    source_work_by_id = {item["material_id"]: item.get("source_work_id") for item in refs}
    source_use_by_id = {item["material_id"]: item.get("source_use_decision", {}) for item in refs}
    for evidence_item in evidence.get("evidence", []):
        evidence_item["source_work_id"] = source_work_by_id.get(evidence_item.get("material_id"))
        decision = source_use_by_id.get(evidence_item.get("material_id"), {})
        evidence_item["source_use_state"] = decision.get("state", "unavailable")
        evidence_item["source_use_decision"] = decision
    for source in evidence.get("sources", []):
        source["source_work_id"] = source_work_by_id.get(source.get("material_id"))
        decision = source_use_by_id.get(source.get("material_id"), {})
        source["source_use_state"] = decision.get("state", "unavailable")
        source["source_use_decision"] = decision
    required_missing = sorted(set(plan.get("required_data", [])) - {"text"})
    if required_missing:
        evidence.setdefault("coverage", {})["missing_data"] = required_missing
        evidence.setdefault("coverage", {})["supported_data"] = ["text"]
        evidence["status"] = "partial" if evidence.get("status") in {"complete", "no_matches"} else evidence.get("status")
        evidence.setdefault("limitations", []).append("Requested non-text data surfaces were not available on this local Material path.")
    if plan.get("subject_ref"):
        evidence.setdefault("coverage", {})["subject_scope_state"] = "not_resolved"
        evidence.setdefault("limitations", []).append("The selected SubjectRef was retained but no current association resolver was bound to this query path.")
    evidence["source_use_state"] = "LOCAL_MATERIAL_ACCESS_ONLY"
    status = evidence.get("status", "partial")
    coverage = evidence.get("coverage", {})
    stop = "REQUIRED_DATA_SURFACE_UNAVAILABLE" if required_missing else (
        "SUBJECT_ASSOCIATION_OWNER_NOT_BOUND" if plan.get("subject_ref") else None
    )
    if coverage.get("matched_passages", 0) > 50:
        coverage["more_available"] = True
        evidence["status"] = status = "partial"
        stop = "LOCAL_RESULT_CAP_REACHED"
    else:
        coverage["more_available"] = False
    _update_path(store, row["path_id"], identity["workspace"]["id"], status=status,
                 coverage=coverage, result=evidence, stop_reason=stop)


def _execute_browser_path(app, identity: dict, row: dict, plan: dict) -> None:
    store = app.extensions["oevira.store"]
    path_request = _loads(row["request_json"], dict) or {}
    target = path_request.get("target", {})
    service = app.extensions.get("oevira.browser")
    if (service is None or not callable(getattr(service, "read_context", None))
            or not callable(getattr(service, "get_context_preview", None))):
        return _update_path(store, row["path_id"], identity["workspace"]["id"], status="unavailable",
                            coverage={"status": "unavailable"}, stop_reason="BROWSER_OWNER_NOT_BOUND")
    try:
        session = service.get_session(identity, target["session_id"])
        with store.transaction(immediate=False) as connection:
            source_work_id = session.get("work_id")
            try:
                if source_work_id:
                    require_work_access(connection, identity, source_work_id)
                if not _source_work_matches_target(connection, identity, source_work_id, plan.get("work_id")):
                    raise _api.ApiError("CLIENT_SCOPE_MISMATCH", "The selected browser page is outside this client scope", 403)
                if plan.get("work_id"):
                    require_work_access(connection, identity, plan["work_id"])
            except _api.ApiError:
                raise
        result = service.read_context(
            identity, target["session_id"],
            {"base_version": target["base_version"], "page_id": target["page_id"],
             "page_revision": target["page_revision"], "include_frames": target["include_frames"],
             "retain_preview": True},
            actor=None, expected_control_generation=target.get("expected_control_generation"),
        )
        capture_ref = result.get("capture_ref")
        if (not isinstance(capture_ref, dict) or not isinstance(capture_ref.get("capture_id"), str)
                or capture_ref.get("session_id") != target["session_id"]
                or capture_ref.get("page_id") != result.get("page_id")
                or capture_ref.get("page_revision") != result.get("page_revision")
                or capture_ref.get("source_admission") != "NOT_ADMITTED"):
            raise _api.ApiError("BROWSER_PREVIEW_NOT_RETAINED", "The selected page preview could not be retained", 409)
        capture_ref = {key: value for key, value in capture_ref.items() if key != "source_use"}
        source_ref = {**capture_ref, "kind": "browser_page", "url": result.get("url"),
                      "title": result.get("title")}
        coverage = dict(capture_ref.get("coverage", {}))
        status = "complete" if coverage.get("status") == "complete" else "partial"
        stop_reason = None
        if plan.get("subject_ref"):
            coverage["subject_scope_state"] = "not_resolved"
            stop_reason = "SUBJECT_ASSOCIATION_OWNER_NOT_BOUND"
            status = "partial"
        if set(plan.get("required_data", [])) - {"text"}:
            missing = sorted(set(plan["required_data"]) - {"text"})
            coverage["missing_data"] = missing
            stop_reason = "REQUIRED_DATA_SURFACE_UNAVAILABLE"
            status = "partial"
        if stop_reason:
            coverage["stop_reason"] = stop_reason
        _update_path(store, row["path_id"], identity["workspace"]["id"], status=status,
                     coverage=coverage,
                     result={"status": "preview_retained", "results": [], "coverage": coverage,
                             "source_ref": source_ref, "source_use_state": "NOT_EVALUATED"},
                     stop_reason=stop_reason,
                     checkpoint_ref={"browser_session_id": target["session_id"],
                                     "page_id": capture_ref["page_id"],
                                     "page_revision": capture_ref["page_revision"],
                                     "snapshot_sha256": capture_ref["snapshot_sha256"],
                                     "capture_ref": capture_ref},
                     source_ref=source_ref, preview_expires_at=capture_ref.get("expires_at"))
    except Exception as exc:
        code = getattr(exc, "code", "BROWSER_READ_FAILED")
        status = "blocked" if code in {"NOT_FOUND", "ACCESS_REVOKED", "CLIENT_SCOPE_MISMATCH",
                                       "BROWSER_SESSION_ACCESS_REVOKED"} else "partial"
        _update_path(store, row["path_id"], identity["workspace"]["id"], status=status,
                     coverage={"status": "unavailable"}, result={"status": status, "results": [],
                     "coverage": {"status": "unavailable"}, "source_use_state": "NOT_EVALUATED"},
                     stop_reason=code)


def _enqueue_child_job(app, identity: dict, url: str, work_id: str | None, idempotency_key: str) -> dict:
    """Call the existing acquisition owner and its own Job idempotency contract."""
    from .worker import create_url_acquisition_job

    job, _status = create_url_acquisition_job(
        app.extensions["oevira.store"], identity,
        {"url": url, **({"work_id": work_id} if work_id else {})}, idempotency_key, app=app,
    )
    return job


def _execute_url_path(app, identity: dict, plan: dict, row: dict) -> None:
    store = app.extensions["oevira.store"]
    path_request = _loads(row["request_json"], dict) or {}
    url = path_request.get("url")
    work_id = path_request.get("query_work_id")
    if identity.get("role") not in {"owner", "editor"}:
        return _update_path(store, row["path_id"], identity["workspace"]["id"], status="blocked",
                            coverage={"status": "not_started"}, stop_reason="WRITE_ACCESS_DENIED")
    if work_id:
        try:
            with store.transaction(immediate=False) as connection:
                work = require_work_access(connection, identity, work_id, write=True)
                if work["status"] != "active":
                    raise _api.ApiError("WORK_ARCHIVED", "Reopen this work before acquiring a source for it", 409)
        except _api.ApiError as exc:
            return _update_path(store, row["path_id"], identity["workspace"]["id"], status="blocked",
                                coverage={"status": "not_started"}, stop_reason=exc.code)
    try:
        job = _enqueue_child_job(app, identity, url, work_id, f"query:{row['path_id']}")
    except _api.ApiError as exc:
        return _update_path(store, row["path_id"], identity["workspace"]["id"], status="blocked",
                            coverage={"status": "not_started"}, stop_reason=exc.code)
    _update_path(store, row["path_id"], identity["workspace"]["id"], status=job.get("status", "queued"),
                 coverage={"status": job.get("status", "queued")}, child_job_id=job.get("id"),
                 checkpoint_ref={"job_id": job.get("id"), "kind": "acquire_url"})


def _resume_query_paths(app, identity: dict, query: dict, *, include_synchronous: bool) -> None:
    store = app.extensions["oevira.store"]
    plan = _loads(query["request_json"], dict) or {}
    rows = store.rows(
        "SELECT * FROM acquisition_query_paths WHERE query_id=? AND workspace_id=? ORDER BY path_order",
        (query["query_id"], identity["workspace"]["id"]),
    )
    for row_value in rows:
        row = dict(row_value)
        pending = row["status"] == "not_started" or (
            row["path_kind"] == "material_search" and row["result_json"] is None
        )
        if not pending:
            continue
        if row["path_kind"] == "public_url_acquisition":
            _execute_url_path(app, identity, plan, row)
        elif not include_synchronous:
            continue
        elif row["path_kind"] == "material_search":
            _execute_material_path(app, identity, row, plan)
        elif row["path_kind"] == "browser_page":
            _execute_browser_path(app, identity, row, plan)


def _verified_query_acquisition(store, identity: dict, job, target_work_id: str | None,
                                purpose: str) -> dict | None:
    """Verify the existing acquire_url receipt and immutable bytes for query readback."""
    if (job["status"] not in {"completed", "partial"} or not job["material_id"]
            or job["user_id"] != identity["user"]["id"] or job["work_id"] != target_work_id):
        return None
    payload = _loads(job["payload_json"], dict) or {}
    expected_url = payload.get("url")
    if not isinstance(expected_url, str) or not expected_url or payload.get("work_id") != target_work_id:
        return None
    try:
        with store.transaction(immediate=False) as connection:
            material = connection.execute(
                "SELECT * FROM materials WHERE id=? AND workspace_id=?",
                (job["material_id"], identity["workspace"]["id"]),
            ).fetchone()
            if material is None or material["owner_user_id"] != job["user_id"] or material["source_url"] != expected_url:
                return None
            require_material_access(connection, identity, material["id"], target_work_id=target_work_id)
            metadata = _loads(material["source_json"], dict) or {}
            if (metadata.get("acquisition_method") != "public_http"
                    or metadata.get("requested_url") != expected_url
                    or metadata.get("status") not in {"ready", "partial"}):
                return None
            if material["status"] not in {"ready", "partial"} or not isinstance(material["source_text"], str):
                return None
            source_text_digest = hashlib.sha256(material["source_text"].encode("utf-8")).hexdigest()
            if (isinstance(metadata.get("text_sha256"), str)
                    and metadata["text_sha256"] != source_text_digest):
                return None
            signature = _material_signature(connection, material, identity, target_work_id, purpose)
    except _api.ApiError:
        return None
    if (not isinstance(material["sha256"], str) or material["size_bytes"] > 3 * 1024 * 1024):
        return None
    try:
        original_path = _api._resource_path(store, material["relative_path"])
        if not original_path.is_file():
            return None
        original = original_path.read_bytes()
    except (OSError, _api.ApiError):
        return None
    actual_digest = hashlib.sha256(original).hexdigest()
    if actual_digest != material["sha256"]:
        return None
    return {
        "material_ref": signature, "sha256": actual_digest, "source_metadata": metadata,
        "source_text_sha256": source_text_digest, "receipt_completed_at": job["updated_at"],
    }


def _query_acquisition_time_eligible(plan: dict, query: dict, job, verified: dict) -> tuple[bool, dict, str | None]:
    observed_at = job["updated_at"]
    observed = _date_value(observed_at, "UTC") if isinstance(observed_at, str) else None
    coverage = {"observed_at": observed_at, "freshness_state": "receipt_verified"}
    if plan.get("freshness", {}).get("require_latest"):
        query_created = _date_value(query["created_at"], "UTC")
        if observed is None or query_created is None or observed < query_created:
            coverage["freshness_state"] = "not_satisfied"
            return False, coverage, "FRESHNESS_REQUIRES_NEW_OBSERVATION"
    max_age = plan.get("freshness", {}).get("max_age_seconds")
    if max_age is not None:
        if observed is None:
            coverage["freshness_state"] = "unknown"
            return False, coverage, "FRESHNESS_UNVERIFIED"
        if (datetime.now(timezone.utc) - observed).total_seconds() > max_age:
            coverage["freshness_state"] = "outside_max_age"
            return False, coverage, "FRESHNESS_WINDOW_MISSED"
    period = plan.get("period")
    if period:
        raw = observed_at if period["field"] == "observed_at" else verified["source_metadata"].get(period["field"])
        timestamp = _date_value(raw, plan.get("business_timezone", "UTC")) if isinstance(raw, str) else None
        start = _date_value(period.get("normalized_start_utc"), "UTC")
        end = _date_value(period.get("normalized_end_exclusive_utc"), "UTC")
        if timestamp is None or start is None or end is None:
            coverage["time_basis_state"] = "unknown"
            return False, coverage, "TIME_BASIS_UNVERIFIED"
        if not start <= timestamp < end:
            coverage["time_basis_state"] = "outside_requested_period"
            return False, coverage, "NO_MATERIALS_IN_PERIOD"
        coverage["time_basis_state"] = "matched_source_time"
    return True, coverage, None


def _visible_path(app, identity: dict, query: dict, row: dict, *, allow_expired: bool = True) -> dict | None:
    store = app.extensions["oevira.store"]
    plan = _loads(query["request_json"], dict) or {}
    source_ref = _loads(row["source_ref_json"], dict) or {}
    path_request = _loads(row["request_json"], dict) or {}
    projected_scope = ({key: value for key, value in path_request.items() if key != "url"}
                       if row["path_kind"] == "public_url_acquisition" else path_request)
    result = _loads(row["result_json"], dict)
    materials = _loads(row["material_refs_json"], list) or []
    status = row["status"]
    stop_reason = row["stop_reason"]
    coverage = _loads(row["coverage_json"], dict) or {}

    source_work_id = source_ref.get("work_id")
    target_work_id = path_request.get("work_id") or path_request.get("query_work_id") or query["work_id"]
    try:
        with store.transaction(immediate=False) as connection:
            if target_work_id:
                require_work_access(connection, identity, target_work_id)
            if source_work_id:
                current_source_ref = _work_reference(connection, identity, source_work_id)
                saved_scope = source_ref.get("client_scope")
                current_scope = current_source_ref.get("client_scope")
                if (isinstance(saved_scope, dict) and current_scope is not None
                        and saved_scope.get("commission_id") != current_scope.get("commission_id")):
                    return None
                if not _source_work_matches_target(connection, identity, source_work_id, target_work_id):
                    return None
            authorized_refs = []
            stale_refs = False
            for signature in materials:
                row_material = connection.execute(
                    "SELECT * FROM materials WHERE id=? AND workspace_id=?",
                    (signature.get("material_id"), identity["workspace"]["id"]),
                ).fetchone()
                if row_material is None:
                    stale_refs = True
                    continue
                try:
                    require_material_access(connection, identity, row_material["id"],
                                            target_work_id=target_work_id)
                except _api.ApiError:
                    continue
                current_signature = _material_signature(
                    connection, row_material, identity, target_work_id or row_material["work_id"],
                    plan.get("purpose", "knowledge_retrieval"),
                )
                if not _source_use_allowed(current_signature):
                    continue
                if any(current_signature.get(key) != signature.get(key) for key in (
                    "sha256", "projection_sha256", "segments_sha256", "source_work_id",
                )):
                    stale_refs = True
                    continue
                authorized_refs.append(signature)
    except _api.ApiError:
        return None

    if row["path_kind"] == "browser_page":
        target = path_request.get("target", {})
        browser = app.extensions.get("oevira.browser")
        checkpoint = _loads(row["checkpoint_ref_json"], dict) or {}
        capture_ref = checkpoint.get("capture_ref")
        if not isinstance(capture_ref, dict):
            capture_ref = (result or {}).get("source_ref", {})
        capture_id = capture_ref.get("capture_id") if isinstance(capture_ref, dict) else None
        unavailable_reason = None
        preview = None
        if (not capture_id or browser is None
                or not callable(getattr(browser, "get_context_preview", None))):
            unavailable_reason = "BROWSER_PREVIEW_NOT_RESTORED"
        else:
            try:
                preview = browser.get_context_preview(
                    identity, target.get("session_id"), capture_id,
                    field_paths=("text",), purpose=plan.get("purpose", "knowledge_retrieval"), action="search",
                )
            except Exception as exc:
                code = getattr(exc, "code", "BROWSER_PREVIEW_UNAVAILABLE")
                unavailable_reason = (code if code in {"BROWSER_PREVIEW_EXPIRED", "BROWSER_PREVIEW_DISCARDED",
                                                       "BROWSER_PREVIEW_UNAVAILABLE"}
                                      or code.startswith("SOURCE_USE_")
                                      else "BROWSER_SESSION_ACCESS_UNAVAILABLE")
        if preview is None:
            page_ref = {**(capture_ref if isinstance(capture_ref, dict) else {}), **source_ref}
            result = {"status": "unavailable", "results": [],
                      "coverage": {"status": unavailable_reason.lower(),
                                   "snapshot_sha256": page_ref.get("snapshot_sha256")},
                      "source_ref": {key: page_ref.get(key) for key in (
                          "kind", "session_id", "page_id", "page_revision", "work_id",
                          "capture_id", "captured_at", "expires_at", "snapshot_sha256",
                          "projection_kind", "projection_version", "source_admission",
                      ) if key in page_ref},
                      "source_use_state": "NOT_EVALUATED"}
            status = "blocked" if unavailable_reason.startswith("SOURCE_USE_") else "unavailable"
            stop_reason = unavailable_reason
            coverage = result["coverage"]
        else:
            current_capture_ref = preview.get("capture_ref", {})
            if (current_capture_ref.get("capture_id") != capture_id
                    or current_capture_ref.get("snapshot_sha256") != capture_ref.get("snapshot_sha256")
                    or (capture_ref.get("preview_sha256") is not None
                        and current_capture_ref.get("preview_sha256") != capture_ref.get("preview_sha256"))
                    or current_capture_ref.get("page_revision") != capture_ref.get("page_revision")):
                result = {"status": "unavailable", "results": [],
                          "coverage": {"status": "capture_ref_mismatch"},
                          "source_use_state": "NOT_EVALUATED"}
                status, stop_reason = "unavailable", "BROWSER_CAPTURE_REF_MISMATCH"
                coverage = result["coverage"]
            elif (not isinstance(current_capture_ref.get("source_use"), dict)
                  or current_capture_ref["source_use"].get("allowed") is not True):
                source_use = current_capture_ref.get("source_use", {})
                result = {"status": "blocked", "results": [],
                          "coverage": {"complete_page_scope_scanned": False,
                                       "source_use_state": source_use.get("state")},
                          "source_ref": {key: value for key, value in source_ref.items() if key != "title"},
                          "source_use_state": source_use.get("state"),
                          "source_use_decision": source_use}
                status = "blocked"
                stop_reason = source_use.get("reason_code", "SOURCE_USE_NOT_AUTHORIZED")
                coverage = result["coverage"]
            else:
                result, status, stop_reason = _project_browser_preview(
                    preview, plan, current_capture_ref,
                    limit=path_request.get("limit", _MAX_BROWSER_HITS),
                    source_use=current_capture_ref["source_use"],
                )
                coverage = result.get("coverage", {})
    elif stale_refs:
        result = {"status": "stale", "results": [], "evidence": [], "citations": [],
                  "coverage": {"complete_scope_scanned": False},
                  "limitations": ["A saved Material projection changed; run this query again to read the current version."]}
        status, stop_reason = "stale", "MATERIAL_PROJECTION_CHANGED"
    elif materials and len(authorized_refs) != len(materials):
        if not authorized_refs:
            return None
        material_ids = [item["material_id"] for item in authorized_refs]
        try:
            fresh = app.extensions["oevira.intelligence"].search_materials(
                identity, plan["query"], work_id=target_work_id,
                material_ids=material_ids, limit=50,
            )
        except IntelligenceError:
            return None
        result = fresh
        status = "partial" if len(authorized_refs) != len(materials) else fresh.get("status", status)
        stop_reason = "CURRENT_MATERIAL_ACCESS_CHANGED" if status == "partial" else None
        coverage = fresh.get("coverage", {})
    elif row["path_kind"] == "material_search" and result is None:
        replay_row = {**row, "material_refs_json": _json(authorized_refs)}
        _execute_material_path(app, identity, replay_row, plan)
        refreshed = store.row(
            "SELECT * FROM acquisition_query_paths WHERE path_id=? AND workspace_id=?",
            (row["path_id"], identity["workspace"]["id"]),
        )
        if refreshed is not None:
            row = dict(refreshed)
            result = _loads(row["result_json"], dict)
            status = row["status"]
            coverage = _loads(row["coverage_json"], dict) or {}
            stop_reason = row["stop_reason"]
    if row["path_kind"] == "material_search":
        _stamp_source_use(result, authorized_refs)

    # A URL child Job is authoritative for the acquisition state; no local shadow state is read as truth.
    child_job_id = row["child_job_id"]
    if child_job_id:
        job = store.row(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=? AND user_id=? AND kind='acquire_url'",
            (child_job_id, identity["workspace"]["id"], identity["user"]["id"]),
        )
        if job is None or job["work_id"] != target_work_id:
            return None
        status = job["status"]
        coverage = {"status": status, "progress_completed": job["progress_completed"],
                    "progress_total": job["progress_total"]}
        checkpoint_ref = {"job_id": child_job_id, "kind": "acquire_url"}
        stop_reason = job["error_code"] if job["error_code"] else None
        result = {"job": {"id": child_job_id, "kind": "acquire_url", "status": status,
                           "error": (None if not job["error_code"] else {"code": job["error_code"]}),
                           "progress_completed": job["progress_completed"],
                           "progress_total": job["progress_total"]}}
        material_id = job["material_id"]
        verified_material_id = None
        if status in {"completed", "partial"} and material_id:
            verified = _verified_query_acquisition(
                store, identity, job, target_work_id, plan.get("purpose", "knowledge_retrieval"),
            )
            if verified is None:
                result = {"job": result["job"], "material_result": {"status": "unavailable", "evidence": [],
                         "citations": [], "coverage": {"complete_scope_scanned": False}}}
                stop_reason = "ACQUISITION_RECEIPT_OR_MATERIAL_UNAVAILABLE"
            else:
                time_eligible, time_coverage, time_stop = _query_acquisition_time_eligible(
                    plan, query, job, verified,
                )
                verified_material_id = material_id
                coverage.update(time_coverage)
                coverage["material_id"] = material_id
                if not time_eligible:
                    result = {"job": result["job"], "material_result": {
                        "status": "partial", "evidence": [], "citations": [],
                        "coverage": {**time_coverage, "complete_scope_scanned": False},
                        "stop_reason": time_stop,
                    }}
                    stop_reason = time_stop
                else:
                    signature = verified["material_ref"]
                    if not _source_use_allowed(signature):
                        result = {"job": result["job"], "material_result": {
                            "status": "blocked", "evidence": [], "citations": [],
                            "source_use_state": signature.get("source_use_state"),
                            "source_use_decision": signature.get("source_use_decision"),
                            "coverage": {"complete_scope_scanned": False},
                        }}
                        stop_reason = _source_use_block_reason(signature)
                        verified_material_id = None
                    else:
                        searched = app.extensions["oevira.intelligence"].search_materials(
                            identity, plan["query"], work_id=target_work_id,
                            material_ids=[material_id], limit=50,
                        )
                        for evidence_item in searched.get("evidence", []):
                            evidence_item["source_work_id"] = signature.get("source_work_id")
                            evidence_item["source_use_state"] = signature["source_use_state"]
                            evidence_item["source_use_decision"] = signature["source_use_decision"]
                        for source in searched.get("sources", []):
                            source["source_work_id"] = signature.get("source_work_id")
                            source["source_use_state"] = signature["source_use_state"]
                            source["source_use_decision"] = signature["source_use_decision"]
                        searched["source_use_state"] = "LOCAL_MATERIAL_ACCESS_ONLY"
                        searched["material_ref"] = signature
                        searched["acquisition_receipt"] = {
                            "kind": "acquire_url", "job_id": child_job_id,
                            "material_sha256": verified["sha256"],
                            "text_sha256": verified["source_text_sha256"],
                            "observed_at": verified["receipt_completed_at"],
                        }
                        required_missing = sorted(set(plan.get("required_data", [])) - {"text"})
                        if required_missing:
                            searched.setdefault("coverage", {})["missing_data"] = required_missing
                            searched.setdefault("coverage", {})["supported_data"] = ["text"]
                            searched["status"] = "partial"
                            searched.setdefault("limitations", []).append(
                                "Requested non-text data surfaces were not available on this acquired Material path."
                            )
                            stop_reason = "REQUIRED_DATA_SURFACE_UNAVAILABLE"
                        if plan.get("subject_ref"):
                            searched.setdefault("coverage", {})["subject_scope_state"] = "not_resolved"
                            searched.setdefault("limitations", []).append(
                                "The selected SubjectRef was retained but not resolved on this query path."
                            )
                            searched["status"] = "partial"
                            stop_reason = "SUBJECT_ASSOCIATION_OWNER_NOT_BOUND"
                        result = {"job": result["job"], "material_result": searched}
        base = {
            "path_id": row["path_id"], "parent_path_id": row["parent_path_id"],
            "path_kind": row["path_kind"], "data_surface": row["data_surface"],
            "source_ref": source_ref, "query": plan["query"], "scope": projected_scope,
            "native_refs": ([{"kind": "job", "id": child_job_id}] +
                            ([{"kind": "material", "id": verified_material_id}] if verified_material_id else [])),
            "status": status, "coverage": coverage, "checkpoint_ref": checkpoint_ref,
            "stop_reason": stop_reason, "result": result,
        }
        return base

    if not allow_expired and row["preview_expires_at"] and row["preview_expires_at"] <= utc_now():
        result = None
    native_refs = [{"kind": "material", "id": item["material_id"],
                    "sha256": item["sha256"], "source_work_id": item.get("source_work_id")}
                   for item in authorized_refs]
    if row["path_kind"] == "browser_page":
        page_ref = (result or {}).get("source_ref", {})
        native_refs = [{"kind": "browser_page", "session_id": page_ref.get("session_id"),
                        "page_id": page_ref.get("page_id"), "page_revision": page_ref.get("page_revision"),
                        "snapshot_sha256": page_ref.get("snapshot_sha256")}]
    return {
        "path_id": row["path_id"], "parent_path_id": row["parent_path_id"],
        "path_kind": row["path_kind"], "data_surface": row["data_surface"],
        "source_ref": source_ref, "query": plan["query"], "scope": projected_scope,
        "native_refs": native_refs, "status": status, "coverage": coverage,
        "checkpoint_ref": _loads(row["checkpoint_ref_json"], dict), "stop_reason": stop_reason,
        "cursor": None, "result": result,
    }


def _query_data(app, identity: dict, query: dict, cursor_value: str | None = None) -> dict:
    store = app.extensions["oevira.store"]
    plan = _loads(query["request_json"], dict) or {}
    cursor = _decode_cursor(cursor_value, query)
    visible_paths = []
    seen_objects: dict[tuple[str, str], str] = {}
    rows = store.rows(
        "SELECT * FROM acquisition_query_paths WHERE query_id=? AND workspace_id=? ORDER BY path_order",
        (query["query_id"], identity["workspace"]["id"]),
    )
    for row_value in rows:
        path = _visible_path(app, identity, dict(query), dict(row_value))
        if path is None:
            continue
        result = path.get("result")
        if isinstance(result, dict):
            evidence_groups = [result.get("evidence", []), result.get("results", [])]
            material_result = result.get("material_result")
            if isinstance(material_result, dict):
                evidence_groups.append(material_result.get("evidence", []))
            path_request = path.get("scope", {})
            result_source = result.get("source_ref", {})
            path_work_id = (path_request.get("work_id") or path_request.get("query_work_id")
                            or path.get("source_ref", {}).get("work_id") or result_source.get("work_id"))
            if path_work_id:
                with store.transaction(immediate=False) as connection:
                    commission_id = work_commission(connection, path_work_id)
                scope_key = f"commission:{commission_id}" if commission_id else f"work:{path_work_id}"
            else:
                scope_key = "workspace:unbound"
            for items in evidence_groups:
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    object_id = item.get("material_id") or item.get("id")
                    if object_id:
                        object_key = (scope_key, object_id)
                        previous_path = seen_objects.get(object_key)
                        if previous_path and previous_path != path["path_id"]:
                            item["discovery_relation"] = {
                                "kind": "same_object_also_discovered", "path_id": previous_path,
                            }
                        else:
                            seen_objects[object_key] = path["path_id"]
        visible_paths.append(path)
    saved_request = _loads(query["request_json"], dict) or {}
    scope_used = dict(saved_request.get("scope_used", {}))
    scope_used.pop("material_ids", None)
    work_context = None
    if query["work_id"]:
        try:
            current_work_ref = _work_reference_for_read(store, identity, query["work_id"])
            work_context = {"work_id": query["work_id"], "work_ref": current_work_ref}
            scope_used["work_id"] = query["work_id"]
            scope_used["work_ref"] = current_work_ref
        except _api.ApiError:
            # The user still owns this private QueryPlan but no longer has the
            # selected Work scope; do not reveal its identifier or source count.
            scope_used = {"work_id": None, "workspace": False, "scope_state": "access_revoked"}

    if cursor and not any(path["path_id"] == cursor[0] for path in visible_paths):
        raise _api._not_found()
    page_size = plan.get("page", {}).get("size", 10)

    def page_list(path: dict, container: dict, field: str, offset: int) -> str | None:
        items = container.get(field)
        if not isinstance(items, list):
            return None
        if offset > len(items):
            raise _api.ApiError("QUERY_CURSOR_STALE", "The query result page is no longer available", 409)
        end = min(len(items), offset + page_size)
        container[field] = items[offset:end]
        path["page"] = {"offset": offset, "size": len(container[field]), "total_saved": len(items)}
        next_offset = end
        if next_offset < len(items):
            return _encode_cursor(query, path["path_id"], next_offset)
        return None

    next_cursors = []
    for path in visible_paths:
        offset = cursor[1] if cursor and cursor[0] == path["path_id"] else 0
        result = path.get("result")
        next_cursor = None
        if isinstance(result, dict):
            material_result = result.get("material_result")
            if isinstance(material_result, dict):
                next_cursor = page_list(path, material_result, "evidence", offset)
                if isinstance(material_result.get("citations"), list):
                    material_result["citations"] = material_result["citations"][offset:offset + page_size]
            elif isinstance(result.get("evidence"), list):
                next_cursor = page_list(path, result, "evidence", offset)
                if isinstance(result.get("citations"), list):
                    result["citations"] = result["citations"][offset:offset + page_size]
            elif isinstance(result.get("results"), list):
                next_cursor = page_list(path, result, "results", offset)
        path["cursor"] = next_cursor
        if next_cursor:
            next_cursors.append({"path_id": path["path_id"], "cursor": next_cursor})

    def path_items(path: dict) -> list:
        result = path.get("result")
        if not isinstance(result, dict):
            return []
        material_result = result.get("material_result")
        if isinstance(material_result, dict):
            return material_result.get("evidence", [])
        return result.get("evidence", []) or result.get("results", [])

    return {
        "query_id": query["query_id"], "operation_id": query["query_id"],
        "query_revision": query["query_revision"], "query": plan.get("query"),
        "language": plan.get("language"), "work_context": work_context,
        "subject_ref": plan.get("subject_ref"), "scope_used": scope_used,
        "purpose": plan.get("purpose"), "freshness": plan.get("freshness"),
        "business_timezone": plan.get("business_timezone"), "period": plan.get("period"),
        "required_data": plan.get("required_data"), "query_paths": visible_paths,
        "results": [{"path_id": path["path_id"], "items": path_items(path)} for path in visible_paths],
        "cursor": next_cursors or None, "errors": [], "created_at": query["created_at"],
    }


def register_query_routes(app) -> None:
    if "acquisition_query_create" in app.view_functions:
        return
    store = app.extensions.get("oevira.store")
    if store is None:
        raise RuntimeError("Query routes require the existing OEVIRA Store")
    if "oevira.intelligence" not in app.extensions:
        raise RuntimeError("Query routes require the current Intelligence retrieval service")

    @app.post("/api/search", endpoint="acquisition_query_create")
    def create_query():
        identity = g.identity
        normalized = _normalise_request(_api._body())
        idempotency_key = _api._idempotency_key()
        if not idempotency_key:
            raise _api.ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for a saved QueryPlan", 400)
        query, created = _persist_query_plan(store, identity, normalized, idempotency_key)
        saved_paths = store.rows(
            "SELECT path_kind,status,result_json FROM acquisition_query_paths WHERE query_id=?",
            (query["query_id"],),
        )
        include_synchronous = created or any(
            row["path_kind"] in {"material_search", "browser_page"}
            and (row["status"] == "not_started" or row["result_json"] is None)
            for row in saved_paths
        )
        _resume_query_paths(app, identity, query, include_synchronous=include_synchronous)
        current = store.row("SELECT * FROM acquisition_query_plans WHERE query_id=?", (query["query_id"],))
        return _api._success(_query_data(app, identity, dict(current)), 201 if created else 200)

    @app.get("/api/search/<query_id>", endpoint="acquisition_query_get")
    def get_query(query_id: str):
        if set(request.args) - {"cursor"}:
            raise _api.ApiError("INVALID_INPUT", "Query read contains an unsupported parameter", 400)
        query = store.row(
            "SELECT * FROM acquisition_query_plans WHERE query_id=? AND workspace_id=? AND owner_user_id=?",
            (query_id, g.identity["workspace"]["id"], g.identity["user"]["id"]),
        )
        if query is None:
            raise _api._not_found()
        return _api._success(_query_data(app, g.identity, dict(query), request.args.get("cursor")))

