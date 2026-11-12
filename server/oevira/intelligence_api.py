"""Authenticated Intelligence conversation APIs and the existing Job consumer."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid

from flask import Flask, g, request

from . import api as _api
from .auth import utc_now
from .intelligence import ExplicitModelSelection, IntelligenceService, _canonical, _digest, _verify_actor
from .intelligence_base import IntelligenceError, INTELLIGENCE_OUTPUT_FORMATS, identity_scope
from .intelligence_conversation import install_intelligence_conversation_schema
from .intelligence_skills import SkillLifecycleError, register_routes as register_skill_routes
from .intelligence_models import ModelSettingsError, register_routes as register_model_routes


_ALLOWED_FORMATS = INTELLIGENCE_OUTPUT_FORMATS
_MAX_CONVERSATION_LIST_LIMIT = 50
_MAX_HISTORY_TURNS = 40
_MAX_MATERIAL_IDS = 500
_MAX_MEMORY_SOURCE_REFS = 32
_MAX_MEMORY_CHARACTERS = 8_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _verify_write(identity: dict) -> None:
    if identity.get("role") not in {"owner", "editor"}:
        raise _api.ApiError("WRITE_ACCESS_DENIED", "Your workspace role cannot start an AI task", 403)


def _load_conversation(store, identity: dict, conversation_id: str):
    workspace_id = identity["workspace"]["id"]
    user_id = identity["user"]["id"]
    row = store.row(
        "SELECT * FROM intelligence_conversations WHERE conversation_id = ? "
        "AND workspace_id = ? AND owner_user_id = ?",
        (conversation_id, workspace_id, user_id),
    )
    if row is None:
        raise _api._not_found()
    return row


def _read_json(value, code: str, expected_type):
    try:
        result = json.loads(value or "null")
    except (TypeError, ValueError) as exc:
        raise _api.ApiError(code, "Saved Intelligence conversation state is unreadable", 409) from exc
    if not isinstance(result, expected_type):
        raise _api.ApiError(code, "Saved Intelligence conversation state is unavailable", 409)
    return result


def _normalize_request(data: dict) -> dict:
    allowed = {
        "message", "selection_id", "work_id", "material_ids", "query", "artifact_id",
        "artifact_version", "media_project_id", "include_memory", "requested_output_formats",
        "artifact_write_intent", "skill_ids",
    }
    if set(data) - allowed:
        raise _api.ApiError("INVALID_INPUT", "Intelligence turn contains an unsupported field", 400,
                            {"fields": sorted(set(data) - allowed)})
    message = _api._raw_text(data.get("message"), "message", maximum=20_000)
    selection_id = _api._optional_string(data.get("selection_id"), "selection_id", maximum=200)
    work_id = _api._optional_string(data.get("work_id"), "work_id", maximum=80)
    material_ids = data.get("material_ids")
    if material_ids is not None:
        if (not isinstance(material_ids, list) or len(material_ids) > _MAX_MATERIAL_IDS
                or any(not isinstance(item, str) or not item or len(item) > 80 for item in material_ids)
                or len(set(material_ids)) != len(material_ids)):
            raise _api.ApiError("INVALID_INPUT", "material_ids must contain distinct saved Material IDs", 400)
        material_ids = sorted(material_ids)
    query = _api._optional_string(data.get("query"), "query", maximum=512) or message[:512]
    artifact_id = _api._optional_string(data.get("artifact_id"), "artifact_id", maximum=80)
    media_project_id = _api._optional_string(data.get("media_project_id"), "media_project_id", maximum=80)
    artifact_version = data.get("artifact_version")
    if artifact_version is not None and (
        isinstance(artifact_version, bool) or not isinstance(artifact_version, int) or artifact_version < 1
    ):
        raise _api.ApiError("INVALID_INPUT", "artifact_version must be a positive integer", 400)
    include_memory = data.get("include_memory", True)
    write_intent = data.get("artifact_write_intent", True)
    if type(include_memory) is not bool or type(write_intent) is not bool:
        raise _api.ApiError("INVALID_INPUT", "Conversation options must be boolean", 400)
    requested_formats = data.get("requested_output_formats", [])
    if (not isinstance(requested_formats, list) or len(requested_formats) > len(_ALLOWED_FORMATS)
            or any(not isinstance(item, str) or item not in _ALLOWED_FORMATS for item in requested_formats)
            or len(set(requested_formats)) != len(requested_formats)):
        raise _api.ApiError("INVALID_INPUT", "requested_output_formats contains an unsupported format", 400)
    skill_ids = data.get("skill_ids")
    if skill_ids is not None:
        if (not isinstance(skill_ids, list) or len(skill_ids) > 8
                or any(not isinstance(item, str) or not item or len(item) > 80 for item in skill_ids)
                or len(set(skill_ids)) != len(skill_ids)):
            raise _api.ApiError("INVALID_INPUT", "skill_ids must contain distinct enabled Skills", 400)
        skill_ids = sorted(skill_ids)
    return {
        "message": message, "selection_id": selection_id, "work_id": work_id,
        "material_ids": material_ids, "query": query, "artifact_id": artifact_id,
        "artifact_version": artifact_version, "media_project_id": media_project_id,
        "include_memory": include_memory,
        "requested_output_formats": sorted(requested_formats),
        "artifact_write_intent": write_intent,
        "skill_ids": skill_ids,
    }


def _normalize_memory_create(data: dict) -> dict:
    allowed = {"kind", "content", "work_id", "source_refs"}
    if set(data) - allowed:
        raise _api.ApiError("INVALID_INPUT", "Memory contains an unsupported field", 400,
                            {"fields": sorted(set(data) - allowed)})
    kind = _api._string(data.get("kind"), "kind", maximum=40)
    content = _api._raw_text(data.get("content"), "content", maximum=_MAX_MEMORY_CHARACTERS)
    work_id = _api._optional_string(data.get("work_id"), "work_id", maximum=80)
    source_refs = data.get("source_refs", [])
    if not isinstance(source_refs, list) or len(source_refs) > _MAX_MEMORY_SOURCE_REFS:
        raise _api.ApiError("INVALID_INPUT", "source_refs must contain at most 32 saved source references", 400,
                            {"field": "source_refs"})
    return {"kind": kind, "content": content, "work_id": work_id, "source_refs": source_refs}


def _normalize_memory_correction(data: dict) -> dict:
    allowed = {"base_version", "content", "source_refs"}
    if set(data) - allowed:
        raise _api.ApiError("INVALID_INPUT", "Memory correction contains an unsupported field", 400,
                            {"fields": sorted(set(data) - allowed)})
    base_version = data.get("base_version")
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        raise _api.ApiError("INVALID_INPUT", "base_version must be a positive integer", 400,
                            {"field": "base_version"})
    content = _api._raw_text(data.get("content"), "content", maximum=_MAX_MEMORY_CHARACTERS)
    source_refs = data.get("source_refs")
    if source_refs is not None and (
        not isinstance(source_refs, list) or len(source_refs) > _MAX_MEMORY_SOURCE_REFS
    ):
        raise _api.ApiError("INVALID_INPUT", "source_refs must contain at most 32 saved source references", 400,
                            {"field": "source_refs"})
    return {"base_version": base_version, "content": content, "source_refs": source_refs}


def _resolve_selection(service: IntelligenceService, identity: dict, selection_id: str | None):
    if service.model_selection_resolver is None:
        if selection_id is None:
            return None
        raise _api.ApiError("MODEL_SELECTION_OWNER_NOT_BOUND", "No selected model settings are available", 409)
    try:
        selection = service.model_selection_resolver(identity, selection_id)
    except ModelSettingsError as exc:
        raise _api.ApiError(exc.code, "Model settings could not be read", exc.status, exc.details) from exc
    except Exception as exc:
        raise _api.ApiError("MODEL_SELECTION_UNAVAILABLE", "The selected model settings could not be read", 503) from exc
    if (type(selection) is not ExplicitModelSelection or selection.selected_by_user is not True
            or selection_id is not None and selection.selection_id != selection_id):
        if selection is None and selection_id is None:
            return None
        raise _api.ApiError("MODEL_NOT_SELECTED", "Choose a model in Intelligence settings before using it", 409)
    return selection


def _catalog_hash(service: IntelligenceService, identity: dict | None = None,
                  skill_snapshots: list[dict] | None = None) -> str:
    if skill_snapshots:
        if identity is None:
            raise _api.ApiError("INTELLIGENCE_SKILL_SCOPE_UNAVAILABLE", "Selected Skills need a current user scope", 409)
        try:
            prompts, _refs = service.skills.load_turn_context(identity, skill_snapshots, service)
        except SkillLifecycleError as exc:
            raise _api.ApiError(exc.code, "A selected Skill is no longer available", exc.status, exc.details) from exc
        allowed = {name for item in prompts for name in item["allowed_tools"]}
        return _digest(service.tools.catalog(allowed))
    return _digest(service.tools.catalog())


def _material_scope(store, identity: dict, *, work_id: str | None, material_ids: list[str] | None) -> None:
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        _verify_actor(connection, identity)
        if work_id is not None:
            work = connection.execute(
                "SELECT status FROM works WHERE id = ? AND workspace_id = ?",
                (work_id, workspace_id),
            ).fetchone()
            if work is None:
                raise _api.ApiError("SOURCE_SCOPE_NOT_FOUND", "The selected Work is unavailable", 404)
            if work["status"] != "active":
                raise _api.ApiError("WORK_ARCHIVED", "Reopen this Work before starting another AI turn", 409)
        if material_ids:
            placeholders = ",".join("?" for _ in material_ids)
            rows = connection.execute(
                f"SELECT id FROM materials WHERE workspace_id = ? AND id IN ({placeholders})",
                (workspace_id, *material_ids),
            ).fetchall()
            if {row["id"] for row in rows} != set(material_ids):
                raise _api.ApiError("SOURCE_SCOPE_NOT_FOUND", "One or more selected Materials are unavailable", 404)
            if work_id is not None:
                linked = connection.execute(
                    f"SELECT material_id FROM work_materials WHERE workspace_id = ? AND work_id = ? "
                    f"AND material_id IN ({placeholders})",
                    (workspace_id, work_id, *material_ids),
                ).fetchall()
                if {row["material_id"] for row in linked} != set(material_ids):
                    raise _api.ApiError("SOURCE_SCOPE_NOT_FOUND", "A selected Material is outside this Work", 404)


def _safe_tool_summaries(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    summaries = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        safe = {"tool": item.get("tool"), "owner": item.get("owner"), "state": "completed"}
        arguments_sha256 = item.get("arguments_sha256")
        if (isinstance(arguments_sha256, str) and len(arguments_sha256) == 64
                and all(char in "0123456789abcdef" for char in arguments_sha256)):
            safe["arguments_sha256"] = arguments_sha256
        if isinstance(result, dict):
            if isinstance(item.get("tool"), str) and item["tool"].startswith("content."):
                publication_id = result.get("id")
                if isinstance(publication_id, str):
                    safe["publication"] = {
                        "id": publication_id,
                        "title": result.get("content", {}).get("title")
                        if isinstance(result.get("content"), dict) else None,
                        "version": result.get("version") if isinstance(result.get("version"), int) else None,
                        "status": result.get("status") if isinstance(result.get("status"), str) else None,
                        "provenance": "assistant_candidate",
                    }
            else:
                artifact_id = result.get("id") or result.get("artifact_id")
                if isinstance(artifact_id, str):
                    safe["artifact"] = {
                        "id": artifact_id,
                        "title": result.get("title") if isinstance(result.get("title"), str) else None,
                        "version": result.get("version") if isinstance(result.get("version"), int) else None,
                    }
            media_project = result.get("project")
            if isinstance(media_project, dict):
                safe["media_project"] = {
                    "title": media_project.get("title") if isinstance(media_project.get("title"), str) else None,
                    "kind": media_project.get("kind") if isinstance(media_project.get("kind"), str) else None,
                    "version": media_project.get("current_version")
                    if isinstance(media_project.get("current_version"), int) else media_project.get("version"),
                    "saved": True,
                }
            if item.get("tool") == "media.recordings.list" and isinstance(result.get("items"), list):
                recordings = result["items"]
                safe["completed_takes"] = sum(
                    1 for recording in recordings
                    if isinstance(recording, dict) and recording.get("status") == "completed"
                )
                safe["recording_sessions_read"] = len(recordings)
            if isinstance(result.get("status"), str):
                safe["result_status"] = result["status"]
            if type(result.get("returned_passages")) is int:
                safe["returned_passages"] = result["returned_passages"]
            if type(result.get("coverage")) is dict:
                coverage = result["coverage"]
                safe["coverage"] = {
                    key: coverage[key] for key in
                    ("authorized_materials", "scanned_materials", "matched_passages", "complete_scope_scanned")
                    if key in coverage and type(coverage[key]) in (int, bool)
                }
        summaries.append(safe)
    return summaries


def _conversation_summary(row) -> dict:
    skill_snapshots = _read_json(
        row["skill_snapshots_json"] if "skill_snapshots_json" in row.keys() else "[]",
        "INTELLIGENCE_CONVERSATION_SKILLS_UNAVAILABLE", list,
    )
    return {
        "conversation_id": row["conversation_id"], "title": row["title"],
        "work_id": row["work_id"],
        "media_project_id": row["media_project_id"] if "media_project_id" in row.keys() else None,
        "status": row["status"],
        "version": row["version"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "model": {"route": row["selection_route"], "model_id": row["selection_model_id"],
                  "protocol": row["selection_protocol"]} if row["selection_id"] else None,
        "skills": [
            {key: item[key] for key in ("skill_id", "version", "content_sha256") if key in item}
            for item in skill_snapshots if isinstance(item, dict)
        ],
    }


def _turn_data(row) -> dict:
    request_data = _read_json(row["request_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", dict)
    context = _read_json(row["context_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", dict)
    citations = _read_json(row["citations_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", list)
    tools = _read_json(row["tool_results_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", list)
    error = ({"code": row["error_code"], "message": row["error_message"]}
             if row["error_code"] else None)
    status = row["status"]
    cancellation_requested = False
    if "job_status" in row.keys():
        job_status = row["job_status"]
        if status in {"queued", "running"} and job_status in {
            "completed", "partial", "failed", "interrupted", "cancelled", "paused", "pause_requested",
        }:
            status = job_status
        cancellation_requested = job_status == "cancel_requested"
    return {
        "turn_id": row["turn_id"], "sequence": row["sequence"],
        "status": status, "cancellation_requested": cancellation_requested,
        "user_message": request_data["message"],
        "assistant_message": row["assistant_text"], "citations": citations,
        "operations": tools, "context": context,
        "error": error, "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _submit_turn(app: Flask, store, service: IntelligenceService,
                 conversation_id: str | None, data: dict):
    identity = g.identity
    _verify_write(identity)
    workspace_id, user_id, session_id = identity_scope(identity)
    if session_id is None:
        raise _api.ApiError("UNAUTHENTICATED", "A current workspace session is required for this turn", 401)
    normalized = _normalize_request(data)
    supplied_work_id = normalized["work_id"]
    supplied_material_ids = normalized["material_ids"]
    supplied_media_project_id = normalized["media_project_id"]
    request_key = _api._idempotency_key()
    if request_key is None:
        raise _api.ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for an Intelligence turn", 400)
    request_fingerprint = _api._request_fingerprint({
        "conversation_id": conversation_id, "request": normalized,
    })

    previous_job = store.row(
        "SELECT * FROM jobs WHERE workspace_id = ? AND kind = 'intelligence_turn' AND idempotency_key = ?",
        (workspace_id, request_key),
    )
    if previous_job is not None:
        if previous_job["user_id"] != user_id or previous_job["request_hash"] != request_fingerprint:
            raise _api.ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different Intelligence turn", 409)
        previous_turn = store.row(
            "SELECT t.*, j.status AS job_status FROM intelligence_turns t JOIN jobs j ON j.id = t.job_id "
            "WHERE t.job_id = ? AND t.workspace_id = ? AND t.owner_user_id = ?",
            (previous_job["id"], workspace_id, user_id),
        )
        previous_conversation = store.row(
            "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ?",
            (previous_turn["conversation_id"], workspace_id, user_id),
        ) if previous_turn else None
        if previous_turn is None or previous_conversation is None:
            raise _api.ApiError("INTELLIGENCE_IDEMPOTENCY_STATE_UNAVAILABLE", "The original turn cannot be read safely", 409)
        return _api._success({
            "conversation": _conversation_summary(previous_conversation),
            "turn": _turn_data(previous_turn), "job": _api._job_data(previous_job),
            "replayed": True,
        }, 200)

    selection_id = normalized["selection_id"]
    selected = _resolve_selection(service, identity, selection_id)
    new_conversation_id = conversation_id is None
    if new_conversation_id:
        try:
            skill_snapshots, _skill_prompts = service.skills.resolve_enabled(
                identity, normalized["skill_ids"] or [], service,
            )
        except SkillLifecycleError as exc:
            raise _api.ApiError(exc.code, "One or more selected Skills are unavailable", exc.status, exc.details) from exc
    else:
        existing_conversation = _load_conversation(store, identity, conversation_id)
        skill_snapshots = _read_json(
            existing_conversation["skill_snapshots_json"], "INTELLIGENCE_CONVERSATION_SKILLS_UNAVAILABLE", list,
        )
        existing_skill_ids = [item.get("skill_id") for item in skill_snapshots if isinstance(item, dict)]
        if normalized["skill_ids"] is not None and normalized["skill_ids"] != existing_skill_ids:
            raise _api.ApiError("INTELLIGENCE_CONVERSATION_SKILLS_CHANGED", "Start a new conversation to change its Skills", 409)
        try:
            service.skills.load_turn_context(identity, skill_snapshots, service)
        except SkillLifecycleError as exc:
            raise _api.ApiError(exc.code, "A selected Skill is no longer available", exc.status, exc.details) from exc
    normalized["skill_ids"] = [item["skill_id"] for item in skill_snapshots]
    catalog_hash = _catalog_hash(service, identity, skill_snapshots)
    now = utc_now()
    conversation_id = conversation_id or str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    # Validate actual current scope before a durable job is accepted.
    work_id = (supplied_work_id if supplied_work_id is not None else
               existing_conversation["work_id"] if not new_conversation_id else None)
    media_project_id = supplied_media_project_id
    if not new_conversation_id:
        saved_media_project_id = existing_conversation["media_project_id"]
        if saved_media_project_id is not None:
            if media_project_id is not None and media_project_id != saved_media_project_id:
                raise _api.ApiError(
                    "INTELLIGENCE_CONVERSATION_MEDIA_SCOPE_CHANGED",
                    "Start a new conversation to change its Media project scope", 409,
                )
            media_project_id = saved_media_project_id
        elif media_project_id is not None:
            prior_turns = store.row(
                "SELECT COUNT(*) AS count FROM intelligence_turns WHERE conversation_id=? AND workspace_id=?",
                (conversation_id, workspace_id),
            )
            if prior_turns is not None and prior_turns["count"] > 0:
                raise _api.ApiError(
                    "INTELLIGENCE_CONVERSATION_MEDIA_SCOPE_CHANGED",
                    "Start a new conversation to add a Media project scope", 409,
                )
    _material_scope(store, identity, work_id=work_id, material_ids=supplied_material_ids)
    if new_conversation_id and work_id is None and not supplied_material_ids and media_project_id is None:
        raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_REQUIRED", "Select a Work, Media project, or at least one authorized Material", 400)
    if supplied_material_ids is not None and len(supplied_material_ids) == 0:
        raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_REQUIRED", "Select at least one authorized Material", 400)
    if media_project_id is not None:
        try:
            service.read_media_project(identity, media_project_id, work_id=work_id)
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "The selected Media project is outside the current scope", exc.status, exc.details) from exc
    if normalized["artifact_id"] is not None:
        artifact = service.read_artifact(identity, normalized["artifact_id"], version=normalized["artifact_version"])
        if work_id is not None and artifact.get("work_id") != work_id:
            raise _api.ApiError("SOURCE_SCOPE_NOT_FOUND", "The selected Artifact is outside this Work", 404)
        if normalized["artifact_version"] is None:
            normalized["artifact_version"] = artifact["version"]

    try:
        with store.transaction() as connection:
            _verify_actor(connection, identity)
            conversation = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ? "
                "AND workspace_id = ? AND owner_user_id = ?",
                (conversation_id, workspace_id, user_id),
            ).fetchone()
            if new_conversation_id:
                title = normalized["message"].splitlines()[0][:80] or "新对话"
                scope_mode = (
                    "all" if work_id is not None and supplied_material_ids is None
                    else "selected" if supplied_material_ids
                    else "none"
                )
                materials = supplied_material_ids or []
                connection.execute(
                    "INSERT INTO intelligence_conversations(conversation_id, workspace_id, owner_user_id, work_id, media_project_id, "
                    "material_ids_json, material_scope_mode, title, selection_id, selection_revision, selection_route, "
                    "selection_protocol, selection_model_id, skill_snapshots_json, codex_thread_id, tool_catalog_sha256, source_refs_json, "
                    "citations_json, status, version, created_at, updated_at, created_by) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, '[]', '[]', 'active', 1, ?, ?, ?)",
                    (
                        conversation_id, workspace_id, user_id, work_id, media_project_id,
                        json.dumps(materials, ensure_ascii=False), scope_mode, title,
                        selected.selection_id if selected else None,
                        selected.revision if selected else None,
                        selected.route if selected else None,
                        selected.protocol if selected else None,
                        selected.model_id if selected else None,
                        json.dumps(skill_snapshots, ensure_ascii=False, sort_keys=True),
                        catalog_hash, now, now, user_id,
                    ),
                )
                conversation = connection.execute(
                    "SELECT * FROM intelligence_conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            else:
                if conversation is None:
                    raise _api._not_found()
                if conversation["status"] != "active":
                    raise _api.ApiError("INTELLIGENCE_CONVERSATION_ARCHIVED", "Reopen this conversation before sending another turn", 409)
                scope_mode = conversation["material_scope_mode"]
                if supplied_work_id is not None and supplied_work_id != conversation["work_id"]:
                    if conversation["work_id"] is not None:
                        raise _api.ApiError("INTELLIGENCE_CONVERSATION_WORK_CHANGED", "Start a new conversation to change its Work scope", 409)
                    work_id = supplied_work_id
                    if scope_mode == "none" and work_id is not None:
                        scope_mode = "selected" if supplied_material_ids else "all"
                else:
                    work_id = conversation["work_id"]
                stored_material_ids = _read_json(
                    conversation["material_ids_json"], "INTELLIGENCE_CONVERSATION_SCOPE_UNAVAILABLE", list,
                )
                if scope_mode == "none" and work_id is None and not supplied_material_ids and media_project_id is None:
                    raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_REQUIRED", "Select a Work, Media project, or at least one authorized Material", 400)
                if supplied_material_ids:
                    if scope_mode == "all":
                        raise _api.ApiError("INTELLIGENCE_CONVERSATION_SCOPE_CHANGED", "Start a new conversation to narrow an all-Work source scope", 409)
                    scope_mode = "selected"
                    stored_material_ids = sorted(set(stored_material_ids) | set(supplied_material_ids))
                if scope_mode == "selected" and work_id is None and not stored_material_ids and media_project_id is None:
                    raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_REQUIRED", "Select at least one Material", 400)
                if conversation["selection_id"]:
                    if selected is not None and (
                        selected.selection_id != conversation["selection_id"]
                        or selected.revision != conversation["selection_revision"]
                        or selected.route != conversation["selection_route"]
                        or selected.protocol != conversation["selection_protocol"]
                    ):
                        raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "Start a new conversation to change its selected model", 409)
                    selected = _resolve_selection(service, identity, conversation["selection_id"])
                    if (selected is None or selected.revision != conversation["selection_revision"]
                            or selected.model_id != conversation["selection_model_id"]):
                        raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "The saved model selection changed; start a new conversation", 409)
                elif selected is not None:
                    if conversation["codex_thread_id"]:
                        raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "Start a new conversation to select a model", 409)
                if (conversation["selection_protocol"] == "openai_responses"
                        and conversation["tool_catalog_sha256"] != catalog_hash):
                    raise _api.ApiError("INTELLIGENCE_CONVERSATION_TOOLS_CHANGED", "Start a new conversation to use the updated product tools", 409)
                if supplied_work_id is None:
                    work_id = conversation["work_id"]
                _material_scope(store, identity, work_id=work_id,
                                material_ids=stored_material_ids if scope_mode == "selected" else None)
                connection.execute(
                    "UPDATE intelligence_conversations SET work_id = ?, media_project_id = COALESCE(media_project_id, ?), "
                    "material_ids_json = ?, material_scope_mode = ?, "
                    "selection_id = COALESCE(selection_id, ?), selection_revision = COALESCE(selection_revision, ?), "
                    "selection_route = COALESCE(selection_route, ?), selection_protocol = COALESCE(selection_protocol, ?), "
                    "selection_model_id = COALESCE(selection_model_id, ?), version = version + 1, updated_at = ? "
                    "WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ? AND version = ? AND status = 'active'",
                    (
                        work_id, media_project_id, json.dumps(stored_material_ids, ensure_ascii=False), scope_mode,
                        selected.selection_id if selected else None, selected.revision if selected else None,
                        selected.route if selected else None, selected.protocol if selected else None,
                        selected.model_id if selected else None, now,
                        conversation_id, workspace_id, user_id, conversation["version"],
                    ),
                )
                conversation = connection.execute(
                    "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ?",
                    (conversation_id, workspace_id),
                ).fetchone()

            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM intelligence_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["sequence"]
            normalized.update({
                "work_id": work_id,
                "media_project_id": media_project_id,
                "material_ids": stored_material_ids if not new_conversation_id and scope_mode == "selected"
                else supplied_material_ids,
                "selection_id": selected.selection_id if selected else None,
            })
            job_payload = {"turn_id": turn_id, "conversation_id": conversation_id, "request": normalized}
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, "
                "idempotency_key, progress_total, progress_label, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'intelligence_turn', 'queued', ?, ?, ?, 1, '等待智能助手处理', ?, ?)",
                (job_id, workspace_id, user_id, work_id,
                 json.dumps(job_payload, ensure_ascii=False, sort_keys=True),
                 request_fingerprint, request_key, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_turns(turn_id, conversation_id, job_id, workspace_id, owner_user_id, "
                "session_id, sequence, request_json, context_json, source_refs_json, citations_json, tool_results_json, "
                "status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, '{}', '[]', '[]', '[]', 'queued', ?, ?)",
                (turn_id, conversation_id, job_id, workspace_id, user_id, session_id, sequence,
                 json.dumps(normalized, ensure_ascii=False, sort_keys=True), now, now),
            )
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            turn = connection.execute("SELECT * FROM intelligence_turns WHERE turn_id = ?", (turn_id,)).fetchone()
            conversation = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise _api.ApiError("INTELLIGENCE_CONVERSATION_CONFLICT", "The conversation changed while this turn was submitted; refresh and continue", 409) from exc

    return _api._success({
        "conversation": _conversation_summary(conversation),
        "turn": _turn_data(turn), "job": _api._job_data(job), "replayed": False,
    }, 202)


def register_routes(app: Flask, store=None, service: IntelligenceService | None = None) -> None:
    if app.extensions.get("oevira.intelligence_api.registered"):
        return
    store = store or app.extensions["oevira.store"]
    service = service or app.extensions.get("oevira.intelligence")
    if not isinstance(service, IntelligenceService):
        raise ValueError("IntelligenceService must be bound before registering Intelligence routes")
    def active_service() -> IntelligenceService:
        current = app.extensions.get("oevira.intelligence")
        return current if isinstance(current, IntelligenceService) else service

    def validate_intelligence_resume(connection, identity: dict, row, resume_context: dict) -> str | None:
        return _validate_intelligence_turn_resume(
            connection, identity, row, resume_context, active_service(),
        )

    app.extensions.setdefault("oevira.job_resume_validators", {})[
        "intelligence_turn"
    ] = validate_intelligence_resume
    install_intelligence_conversation_schema(store)
    register_skill_routes(app, store, active_service)
    register_model_routes(app, store, active_service)

    @app.get("/api/intelligence/status")
    def intelligence_status():
        current = active_service()
        status = current.status()
        try:
            connections = current.model_settings.list_connections(g.identity)
        except ModelSettingsError as exc:
            raise _api.ApiError(exc.code, "Model settings could not be read", exc.status, exc.details) from exc
        status["model_connections"] = connections
        status["selected_model"] = next((item for item in connections if item.get("selected") is True), None)
        return _api._success(status)

    @app.get("/api/intelligence/memories")
    def list_intelligence_memories():
        allowed_query = {"work_id", "include_history"}
        if set(request.args) - allowed_query:
            raise _api.ApiError("INVALID_INPUT", "Memory query contains an unsupported field", 400,
                                {"fields": sorted(set(request.args) - allowed_query)})
        if any(len(request.args.getlist(name)) != 1 for name in request.args):
            raise _api.ApiError("INVALID_INPUT", "Memory query fields cannot be repeated", 400)
        work_id = _api._optional_string(request.args.get("work_id"), "work_id", maximum=80)
        include_history_value = request.args.get("include_history")
        if include_history_value is None:
            include_history = False
        elif include_history_value in {"true", "false"}:
            include_history = include_history_value == "true"
        else:
            raise _api.ApiError("INVALID_INPUT", "include_history must be true or false", 400,
                                {"field": "include_history"})
        try:
            memories = active_service().memory.list(
                g.identity, work_id=work_id, include_history=include_history,
            )
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "Memories could not be read", exc.status, exc.details) from exc
        return _api._success(memories)

    @app.post("/api/intelligence/memories")
    def create_intelligence_memory():
        data = _normalize_memory_create(_api._body())
        try:
            memory = active_service().memory.create(g.identity, **data)
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "Memory could not be saved", exc.status, exc.details) from exc
        return _api._success(memory, 201)

    @app.post("/api/intelligence/memories/<memory_id>/correct")
    def correct_intelligence_memory(memory_id: str):
        memory_id = _api._string(memory_id, "memory_id", maximum=80)
        data = _normalize_memory_correction(_api._body())
        try:
            memory = active_service().memory.correct(g.identity, memory_id, **data)
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "Memory correction could not be saved", exc.status, exc.details) from exc
        return _api._success(memory)

    @app.post("/api/intelligence/memories/<memory_id>/use")
    def set_intelligence_memory_use(memory_id: str):
        memory_id = _api._string(memory_id, "memory_id", maximum=80)
        data = _api._body()
        if set(data) != {"base_version", "enabled"}:
            raise _api.ApiError("INVALID_INPUT", "Memory use change requires base_version and enabled", 400,
                                {"fields": sorted(set(data) ^ {"base_version", "enabled"})})
        base_version = data["base_version"]
        enabled = data["enabled"]
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise _api.ApiError("INVALID_INPUT", "base_version must be a positive integer", 400,
                                {"field": "base_version"})
        if type(enabled) is not bool:
            raise _api.ApiError("INVALID_INPUT", "enabled must be a boolean", 400,
                                {"field": "enabled"})
        try:
            memory = active_service().memory.set_use_enabled(
                g.identity, memory_id, base_version=base_version, enabled=enabled,
            )
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "Memory use could not be changed", exc.status, exc.details) from exc
        return _api._success(memory)

    @app.post("/api/intelligence/memories/<memory_id>/forget")
    def forget_intelligence_memory(memory_id: str):
        memory_id = _api._string(memory_id, "memory_id", maximum=80)
        data = _api._body()
        if set(data) != {"base_version"}:
            raise _api.ApiError("INVALID_INPUT", "Forgetting a memory requires only base_version", 400,
                                {"fields": sorted(set(data) - {"base_version"})})
        base_version = data["base_version"]
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
            raise _api.ApiError("INVALID_INPUT", "base_version must be a positive integer", 400,
                                {"field": "base_version"})
        try:
            result = active_service().memory.forget(g.identity, memory_id, base_version=base_version)
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "Memory could not be forgotten", exc.status, exc.details) from exc
        return _api._success(result)

    @app.get("/api/intelligence/conversations")
    def list_intelligence_conversations():
        identity = g.identity
        _verify_actor_with_service(active_service(), identity)
        selected_status = request.args.get("status", "active")
        if selected_status not in {"active", "archived", "all"}:
            raise _api.ApiError("INVALID_INPUT", "status must be active, archived, or all", 400)
        limit_value = request.args.get("limit", "30")
        try:
            limit = int(limit_value)
        except (TypeError, ValueError) as exc:
            raise _api.ApiError("INVALID_INPUT", "limit must be an integer", 400) from exc
        if not 1 <= limit <= _MAX_CONVERSATION_LIST_LIMIT:
            raise _api.ApiError("INVALID_INPUT", f"limit must be between 1 and {_MAX_CONVERSATION_LIST_LIMIT}", 400)
        status_filter = "" if selected_status == "all" else "AND status = ?"
        values = (identity["workspace"]["id"], identity["user"]["id"], limit) if selected_status == "all" else (
            identity["workspace"]["id"], identity["user"]["id"], selected_status, limit,
        )
        rows = store.rows(
            "SELECT * FROM intelligence_conversations WHERE workspace_id = ? AND owner_user_id = ? "
            + status_filter + " ORDER BY updated_at DESC, conversation_id LIMIT ?",
            values,
        )
        return _api._success([_conversation_summary(row) for row in rows])

    @app.get("/api/intelligence/conversations/<conversation_id>")
    def read_intelligence_conversation(conversation_id: str):
        _verify_actor_with_service(active_service(), g.identity)
        conversation = _load_conversation(store, g.identity, conversation_id)
        turns = store.rows(
            "SELECT t.*, j.status AS job_status FROM intelligence_turns t JOIN jobs j ON j.id = t.job_id "
            "WHERE t.conversation_id = ? AND t.workspace_id = ? AND t.owner_user_id = ? ORDER BY t.sequence",
            (conversation_id, g.identity["workspace"]["id"], g.identity["user"]["id"]),
        )
        return _api._success({
            "conversation": _conversation_summary(conversation),
            "turns": [_turn_data(row) for row in turns],
        })

    @app.post("/api/intelligence/turns")
    def start_intelligence_conversation():
        return _submit_turn(app, store, active_service(), None, _api._body())

    @app.post("/api/intelligence/conversations/<conversation_id>/turns")
    def continue_intelligence_conversation(conversation_id: str):
        return _submit_turn(app, store, active_service(), conversation_id, _api._body())

    @app.post("/api/intelligence/conversations/<conversation_id>/archive")
    def archive_intelligence_conversation(conversation_id: str):
        identity = g.identity
        _verify_write(identity)
        with store.transaction() as connection:
            _verify_actor(connection, identity)
            row = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ?",
                (conversation_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
            if row is None:
                raise _api._not_found()
            if row["status"] != "archived":
                connection.execute(
                    "UPDATE intelligence_conversations SET status = 'archived', version = version + 1, updated_at = ? "
                    "WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ? AND version = ?",
                    (utc_now(), conversation_id, identity["workspace"]["id"], identity["user"]["id"], row["version"]),
                )
            updated = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return _api._success(_conversation_summary(updated))

    @app.post("/api/intelligence/conversations/<conversation_id>/restore")
    def restore_intelligence_conversation(conversation_id: str):
        identity = g.identity
        _verify_write(identity)
        with store.transaction() as connection:
            _verify_actor(connection, identity)
            row = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ?",
                (conversation_id, identity["workspace"]["id"], identity["user"]["id"]),
            ).fetchone()
            if row is None:
                raise _api._not_found()
            if row["status"] == "archived":
                connection.execute(
                    "UPDATE intelligence_conversations SET status = 'active', version = version + 1, updated_at = ? "
                    "WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ? AND version = ? AND status = 'archived'",
                    (utc_now(), conversation_id, identity["workspace"]["id"], identity["user"]["id"], row["version"]),
                )
            updated = connection.execute(
                "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ?",
                (conversation_id, identity["workspace"]["id"]),
            ).fetchone()
        return _api._success(_conversation_summary(updated))

    app.extensions["oevira.intelligence_api.registered"] = True


def _verify_actor_with_service(service: IntelligenceService, identity: dict) -> None:
    try:
        with service.store.transaction(immediate=False) as connection:
            _verify_actor(connection, identity)
    except IntelligenceError as exc:
        raise _api.ApiError(exc.code, str(exc), exc.status, exc.details) from exc


def _worker_identity(store, claim: dict, turn) -> dict:
    workspace_id, user_id = claim["workspace_id"], claim["user_id"]
    session_id = turn["session_id"]
    with store.transaction(immediate=False) as connection:
        member = connection.execute(
            "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
        session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        user = connection.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        workspace = connection.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if (member is None or member["active"] != 1 or member["role"] not in {"owner", "editor"}
            or session is None or session["revoked_at"] is not None or session["expires_at"] <= utc_now()
            or user is None or workspace is None):
        raise IntelligenceError("ACCESS_REVOKED", 403)
    return {
        "user": dict(user), "workspace": dict(workspace),
        "session_id": session_id, "role": member["role"],
    }


def _previous_conversation_messages(store, conversation_id: str, current_sequence: int) -> list[dict]:
    rows = store.rows(
        "SELECT turn_id, sequence, request_json, assistant_text, status FROM intelligence_turns "
        "WHERE conversation_id = ? AND sequence < ? AND status IN ('completed', 'partial', 'failed', 'interrupted', 'cancelled') "
        "ORDER BY sequence DESC LIMIT ?",
        (conversation_id, current_sequence, _MAX_HISTORY_TURNS),
    )
    history = []
    for row in reversed(rows):
        request_data = _read_json(row["request_json"], "INTELLIGENCE_CONVERSATION_HISTORY_UNAVAILABLE", dict)
        message = request_data.get("message")
        if isinstance(message, str) and message:
            history.append({"turn_id": row["turn_id"], "role": "user", "content": message})
        if row["status"] in {"completed", "partial"} and isinstance(row["assistant_text"], str) and row["assistant_text"]:
            history.append({"turn_id": row["turn_id"], "role": "assistant", "content": row["assistant_text"]})
    return history


def _job_is_cancelled(store, job_id: str, workspace_id: str, generation: int) -> bool:
    row = store.row(
        "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
        (job_id, workspace_id),
    )
    return (row is None or row["lease_generation"] != generation
            or row["cancel_requested"] == 1 or row["status"] != "running")


def _validate_paused_context(connection, identity: dict, request_data: dict,
                             context_snapshot: dict, service: IntelligenceService) -> None:
    """Recheck the source-backed base context before continuing saved tool receipts."""
    work_id = request_data.get("work_id")
    saved_work = context_snapshot.get("work")
    if work_id is None:
        if saved_work is not None:
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The saved Work scope no longer matches this turn", 409)
    else:
        from .collaboration import require_material_access, require_work_access
        work = require_work_access(connection, identity, work_id)
        if (work is None or work["status"] != "active" or not isinstance(saved_work, dict)
                or saved_work.get("id") != work_id or saved_work.get("version") != work["version"]
                or saved_work.get("title") != work["title"] or saved_work.get("goal") != work["goal"]
                or _digest(saved_work.get("brief", {})) != _digest(json.loads(work["brief_json"] or "{}"))):
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The Work requirements changed while the task was paused", 409)

    evidence = context_snapshot.get("evidence")
    evidence_scope = evidence.get("scope") if isinstance(evidence, dict) else None
    saved_material_ids = evidence_scope.get("material_ids") if isinstance(evidence_scope, dict) else None
    if not isinstance(saved_material_ids, list) or any(not isinstance(item, str) for item in saved_material_ids):
        raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_UNAVAILABLE", "The paused task has no exact saved Material scope", 409)
    saved_tool_results = context_snapshot.get("tool_results", [])
    if not isinstance(saved_tool_results, list):
        raise _api.ApiError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", "Saved tool results cannot be read", 409)
    receipt_materials = {}
    for operation in saved_tool_results:
        if not isinstance(operation, dict) or not isinstance(operation.get("result"), dict):
            continue
        operation_result = operation["result"]
        material = operation_result.get("material")
        if isinstance(material, dict) and isinstance(material.get("id"), str):
            receipt_materials[material["id"]] = {
                "material_sha256": material.get("sha256"), "status": material.get("status"),
            }
        project_result = operation_result.get("project")
        sources = project_result.get("sources") if isinstance(project_result, dict) else None
        if isinstance(sources, list):
            for source in sources:
                material_id = source.get("source_material_id") if isinstance(source, dict) else None
                if isinstance(material_id, str) and material_id:
                    receipt_materials[material_id] = {
                        "material_sha256": source.get("sha256"), "status": source.get("status"),
                    }
    current_material_ids = set(request_data.get("material_ids") or [])
    if request_data.get("material_ids") is None and work_id is not None:
        current_material_ids.update(
            row["id"] for row in connection.execute(
                "SELECT m.id FROM work_materials wm JOIN materials m ON m.id=wm.material_id "
                "AND m.workspace_id=wm.workspace_id WHERE wm.workspace_id=? AND wm.work_id=?",
                (identity["workspace"]["id"], work_id),
            ).fetchall()
        )
    media_project_id = request_data.get("media_project_id")
    if media_project_id is not None:
        project = connection.execute(
            "SELECT current_version, work_id FROM media_projects WHERE id=? AND workspace_id=?",
            (media_project_id, identity["workspace"]["id"]),
        ).fetchone()
        if project is None or project["work_id"] is not None and project["work_id"] != work_id:
            raise _api.ApiError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", "The selected Media project changed scope", 409)
        saved_project = context_snapshot.get("media_project")
        saved_version = saved_project.get("version") if isinstance(saved_project, dict) else None
        for operation in saved_tool_results:
            result = operation.get("result") if isinstance(operation, dict) else None
            project_result = result.get("project") if isinstance(result, dict) else None
            if (isinstance(project_result, dict) and project_result.get("id") == media_project_id
                    and isinstance(project_result.get("current_version"), int)):
                saved_version = project_result["current_version"]
        if project["current_version"] != saved_version:
            raise _api.ApiError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", "The selected Media project changed while the task was paused", 409)
        version_row = connection.execute(
            "SELECT state_json FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
            (media_project_id, identity["workspace"]["id"], project["current_version"]),
        ).fetchone()
        if version_row is None:
            raise _api.ApiError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", "The saved Media project version is unavailable", 409)
        try:
            state = json.loads(version_row["state_json"])
        except (TypeError, ValueError) as exc:
            raise _api.ApiError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", "The saved Media project version cannot be read", 409) from exc
        def collect_material_ids(value):
            if isinstance(value, dict):
                material_id = value.get("source_material_id")
                if isinstance(material_id, str) and material_id:
                    current_material_ids.add(material_id)
                for item in value.values():
                    collect_material_ids(item)
            elif isinstance(value, list):
                for item in value:
                    collect_material_ids(item)
        collect_material_ids(state)
    current_material_ids.update(receipt_materials)
    saved_material_ids = set(saved_material_ids) | set(receipt_materials)
    if saved_material_ids != current_material_ids:
        raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The selected Material set changed while the task was paused", 409)

    saved_sources = evidence.get("sources", []) if isinstance(evidence, dict) else []
    if not isinstance(saved_sources, list):
        raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_UNAVAILABLE", "The saved evidence source list is invalid", 409)
    source_snapshots = {
        item.get("material_id"): item for item in saved_sources if isinstance(item, dict)
    }
    for material_id, receipt in receipt_materials.items():
        source_snapshots.setdefault(material_id, {"material_id": material_id, **receipt})
    if set(source_snapshots) != current_material_ids:
        raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_UNAVAILABLE", "The saved evidence does not cover the frozen source scope", 409)
    from .collaboration import require_material_access
    for source in source_snapshots.values():
        if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
            raise _api.ApiError("INTELLIGENCE_SOURCE_SCOPE_UNAVAILABLE", "The saved evidence source is invalid", 409)
        material = require_material_access(connection, identity, source["material_id"], target_work_id=work_id)
        if (source.get("material_sha256") is not None and material["sha256"] != source.get("material_sha256")
                or source.get("status") is not None and material["status"] != source.get("status")):
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "A selected Material changed while the task was paused", 409)

    if request_data.get("artifact_id") is not None:
        artifact_snapshot = context_snapshot.get("artifact")
        artifact_version = request_data.get("artifact_version")
        if (not isinstance(artifact_snapshot, dict) or artifact_snapshot.get("id") != request_data["artifact_id"]
                or artifact_snapshot.get("version") != artifact_version):
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The selected Artifact version is not pinned", 409)
        row = connection.execute(
            "SELECT title,body,citations_json,input_snapshot_json FROM artifact_versions "
            "WHERE artifact_id=? AND workspace_id=? AND version=?",
            (request_data["artifact_id"], identity["workspace"]["id"], artifact_version),
        ).fetchone()
        if row is None:
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The selected Artifact version is unavailable", 409)
        artifact_payload = {
            "id": request_data["artifact_id"], "version": artifact_version,
            "title": row["title"], "body": row["body"],
            "citations": json.loads(row["citations_json"] or "[]"),
            "input_snapshot": json.loads(row["input_snapshot_json"] or "{}"),
        }
        if (_digest(artifact_payload) != _digest({
            key: artifact_snapshot[key] for key in ("id", "version", "title", "body", "citations", "input_snapshot")
        } if all(key in artifact_snapshot for key in ("id", "version", "title", "body", "citations", "input_snapshot")) else {})):
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The selected Artifact version changed while the task was paused", 409)

    if request_data.get("include_memory", True):
        current_memory = service.memory.context(identity, work_id=work_id)
        if _digest(current_memory) != _digest(context_snapshot.get("memory")):
            raise _api.ApiError("INTELLIGENCE_CONTEXT_CHANGED", "The selected Work memory changed while the task was paused", 409)


def _validate_intelligence_turn_resume(connection, identity: dict, job,
                                        resume_context: dict, service: IntelligenceService) -> None:
    """Resume only an untouched queued turn or a turn with saved, verified tool receipts."""
    if resume_context:
        raise _api.ApiError("INVALID_INPUT", "Continue the saved Intelligence turn without changing its inputs", 400)
    if job["kind"] != "intelligence_turn" or job["status"] != "paused" or not job["pause_requested"]:
        raise _api.ApiError("JOB_NOT_RESUMABLE", "This Intelligence task is not in a confirmed paused state", 409)
    try:
        payload = json.loads(job["payload_json"])
        turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        request_data = payload.get("request") if isinstance(payload, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _api.ApiError("INTELLIGENCE_RESUME_STATE_UNAVAILABLE", "The saved turn request cannot be read", 409) from exc
    if (not isinstance(turn_id, str) or not isinstance(conversation_id, str)
            or not isinstance(request_data, dict)):
        raise _api.ApiError("INTELLIGENCE_RESUME_STATE_UNAVAILABLE", "The saved turn request is incomplete", 409)
    turn = connection.execute(
        "SELECT * FROM intelligence_turns WHERE turn_id=? AND job_id=? AND workspace_id=? AND owner_user_id=?",
        (turn_id, job["id"], job["workspace_id"], job["user_id"]),
    ).fetchone()
    conversation = connection.execute(
        "SELECT * FROM intelligence_conversations WHERE conversation_id=? AND workspace_id=? AND owner_user_id=?",
        (conversation_id, job["workspace_id"], job["user_id"]),
    ).fetchone()
    if turn is None or conversation is None or conversation["status"] != "active":
        raise _api.ApiError("INTELLIGENCE_CONVERSATION_UNAVAILABLE", "The saved conversation is unavailable", 409)
    saved_request = _read_json(turn["request_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", dict)
    if saved_request != request_data or request_data.get("work_id") != job["work_id"]:
        raise _api.ApiError("INTELLIGENCE_RESUME_STATE_MISMATCH", "The saved turn no longer matches its Job", 409)

    membership = connection.execute(
        "SELECT active,role FROM memberships WHERE workspace_id=? AND user_id=?",
        (job["workspace_id"], job["user_id"]),
    ).fetchone()
    session = connection.execute(
        "SELECT revoked_at,expires_at FROM sessions WHERE id=? AND workspace_id=? AND user_id=?",
        (turn["session_id"], job["workspace_id"], job["user_id"]),
    ).fetchone()
    if (membership is None or membership["active"] != 1 or membership["role"] not in {"owner", "editor"}
            or session is None or session["revoked_at"] is not None or session["expires_at"] <= utc_now()):
        raise _api.ApiError("UNAUTHENTICATED", "The original Intelligence session is no longer active", 401)
    actor = {
        "user": dict(identity["user"]), "workspace": dict(identity["workspace"]),
        "session_id": turn["session_id"], "role": membership["role"],
    }

    work_id = request_data.get("work_id")
    if work_id is not None:
        from .collaboration import require_material_access, require_work_access
        work = require_work_access(connection, actor, work_id, write=True)
        if work["status"] != "active":
            raise _api.ApiError("WORK_ARCHIVED", "Reopen this Work before continuing the Intelligence turn", 409)
        material_ids = request_data.get("material_ids")
        if material_ids is not None:
            if not isinstance(material_ids, list):
                raise _api.ApiError("INTELLIGENCE_CONVERSATION_SCOPE_UNAVAILABLE", "The saved Material scope is unreadable", 409)
            for material_id in material_ids:
                require_material_access(connection, actor, material_id, target_work_id=work_id)
    elif request_data.get("material_ids"):
        from .collaboration import require_material_access
        for material_id in request_data["material_ids"]:
            require_material_access(connection, actor, material_id)
    media_project_id = request_data.get("media_project_id")
    if media_project_id is not None:
        project = service.read_media_project(actor, media_project_id, work_id=work_id)
        if not isinstance(project, dict) or project.get("id") != media_project_id:
            raise _api.ApiError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", "The selected Media project is unavailable", 409)
    artifact_id = request_data.get("artifact_id")
    if artifact_id is not None:
        artifact = service.read_artifact(actor, artifact_id, version=request_data.get("artifact_version"))
        if work_id is not None and artifact.get("work_id") != work_id:
            raise _api.ApiError("SOURCE_SCOPE_NOT_FOUND", "The selected Artifact is outside this Work", 404)

    selection_id = request_data.get("selection_id")
    suspended_from = job["suspended_from"]
    if selection_id != conversation["selection_id"]:
        raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "The saved turn's model selection changed", 409)
    selection = None
    if service.model_selection_resolver is not None:
        try:
            selection = service.model_selection_resolver(actor, selection_id)
        except Exception as exc:
            raise _api.ApiError("MODEL_SELECTION_UNAVAILABLE", "The saved model selection could not be read", 503) from exc
        if selection_id is None:
            if selection is not None or conversation["selection_revision"] is not None:
                raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "A model was selected after this turn was saved; start a new turn", 409)
        elif (type(selection) is not ExplicitModelSelection
              or selection.selection_id != conversation["selection_id"]
              or selection.revision != conversation["selection_revision"]
              or selection.route != conversation["selection_route"]
              or selection.protocol != conversation["selection_protocol"]
              or selection.model_id != conversation["selection_model_id"]):
            raise _api.ApiError("INTELLIGENCE_CONVERSATION_MODEL_CHANGED", "The selected model changed; start a new turn", 409)

    if selection_id is not None and selection is None:
        if suspended_from == "waiting_for_auth":
            return "waiting_for_auth"
        raise _api.ApiError("MODEL_SELECTION_UNAVAILABLE", "The original model selection is unavailable", 409)
    if selection_id is None and selection is None and suspended_from == "waiting_for_input":
        return "waiting_for_input"
    if selection is None:
        raise _api.ApiError("MODEL_NOT_SELECTED", "This saved turn has no original model selection", 409)
    if selection is not None and (
        selection.route == "owned" and service.owned_inference_runner is None
        or selection.route == "user_configured" and (
            selection.protocol == RESPONSES_PROTOCOL and service.codex_gateway is None
            or selection.protocol != RESPONSES_PROTOCOL and service.model_gateway is None
        )
    ):
        if suspended_from == "waiting_for_auth":
            return "waiting_for_auth"
        raise _api.ApiError("SELECTED_MODEL_ROUTE_UNAVAILABLE", "The original model route is unavailable", 409)

    skill_snapshots = _read_json(
        conversation["skill_snapshots_json"], "INTELLIGENCE_CONVERSATION_SKILLS_UNAVAILABLE", list,
    )
    try:
        service.skills.assert_active(connection, actor, skill_snapshots, service)
    except Exception as exc:
        code = getattr(exc, "code", "INTELLIGENCE_SKILL_PERMISSION_RECHECK_UNAVAILABLE")
        status = getattr(exc, "status", 503)
        raise _api.ApiError(code, "A selected Skill is no longer available for this turn", status) from exc

    context_snapshot = _read_json(turn["context_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", dict)
    checkpoint = context_snapshot.get("resume_checkpoint")
    stored_tools = _read_json(turn["tool_results_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", list)
    never_started = (
        job["attempts"] == 0
        and job["suspended_from"] in {"queued", "waiting_for_input", "waiting_for_auth", "retry_wait"}
        and turn["status"] == "queued" and not stored_tools
        and turn["assistant_text"] is None and turn["provider"] is None and turn["model_id"] is None
        and not context_snapshot
    )
    if never_started:
        return "queued"
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
        raise _api.ApiError("JOB_RECONCILIATION_REQUIRED", "This turn has no safe saved resume checkpoint", 409,
                            {"job_id": job["id"], "kind": job["kind"]})
    stage = checkpoint.get("stage")
    if stage == "model_not_started":
        if (turn["status"] not in {"interrupted", "queued"} or stored_tools
                or checkpoint.get("tool_turn") != 0
                or turn["provider"] is not None or turn["model_id"] is not None
                or turn["assistant_text"] is not None):
            raise _api.ApiError("JOB_RECONCILIATION_REQUIRED", "The model may have started; review saved results before continuing", 409,
                                {"job_id": job["id"], "kind": job["kind"]})
        if context_snapshot.get("versions") is not None:
            _validate_paused_context(connection, actor, request_data, context_snapshot, service)
    elif stage == "after_saved_tools":
        receipts = context_snapshot.get("tool_results")
        if (not isinstance(receipts, list) or not receipts or not stored_tools
                or _digest(receipts) != checkpoint.get("tool_results_sha256")
                or len(receipts) != len(stored_tools)
                or [(item.get("tool"), item.get("arguments_sha256")) for item in receipts if isinstance(item, dict)]
                != [(item.get("tool"), item.get("arguments_sha256")) for item in stored_tools if isinstance(item, dict)]
                or isinstance(checkpoint.get("tool_turn"), bool)
                or not isinstance(checkpoint.get("tool_turn"), int)
                or not 1 <= checkpoint["tool_turn"] <= 8
                or turn["status"] != "partial"
                or turn["provider"] != conversation["selection_route"]
                or turn["model_id"] != conversation["selection_model_id"]):
            raise _api.ApiError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", "Saved tool receipts do not match the paused turn", 409)
        try:
            service.tools.validate_saved_tool_receipt_names(receipts)
        except IntelligenceError as exc:
            raise _api.ApiError(exc.code, "A completed operation needs result reconciliation before continuing", exc.status, exc.details) from exc
        _validate_paused_context(connection, actor, request_data, context_snapshot, service)
    else:
        raise _api.ApiError("JOB_RECONCILIATION_REQUIRED", "The paused turn has an unknown execution boundary; review its saved results", 409,
                            {"job_id": job["id"], "kind": job["kind"]})

    connection.execute(
        "UPDATE intelligence_turns SET status='queued', error_code=NULL, error_message=NULL, updated_at=? "
        "WHERE turn_id=? AND job_id=? AND workspace_id=? AND owner_user_id=?",
        (utc_now(), turn_id, job["id"], job["workspace_id"], job["user_id"]),
    )
    return "queued"


def _artifact_from_tool_results(store, workspace_id: str, tool_results: list[dict]) -> str | None:
    artifact_id = None
    for tool_result in tool_results:
        if not isinstance(tool_result, dict) or not isinstance(tool_result.get("result"), dict):
            continue
        result = tool_result["result"]
        candidate = result.get("id") or result.get("artifact_id")
        if isinstance(candidate, str):
            row = store.row(
                "SELECT id FROM artifacts WHERE id = ? AND workspace_id = ?",
                (candidate, workspace_id),
            )
            if row is not None:
                artifact_id = candidate
    return artifact_id


def _final_turn_state(result: dict, tool_results: list[dict]) -> tuple[str, str, str | None]:
    status = result.get("status")
    code = result.get("code")
    if status in {"completed", "complete"}:
        return "completed", "completed", None
    attempted = result.get("execution") == "ATTEMPTED"
    if tool_results:
        return "partial", "partial", code or "INTELLIGENCE_OPERATION_PARTIAL"
    if status == "cancelled" and not attempted:
        return "cancelled", "cancelled", code or "INTELLIGENCE_TURN_CANCELLED"
    if attempted or status in {"interrupted", "error", "failed"}:
        # A provider request or owner operation may have completed before its
        # receipt was lost. Do not expose generic Job retry for this turn.
        return "partial", "partial", code or "INTELLIGENCE_OUTCOME_UNKNOWN"
    return "partial", "partial", code or "INTELLIGENCE_BASE_CONTEXT_ONLY"


def process_intelligence_job(app: Flask, claim: dict) -> None:
    """Run one existing Job claim through the real Intelligence consumer."""
    from .worker import _advance, claim_can_commit

    store = app.extensions["oevira.store"]
    service = app.extensions.get("oevira.intelligence")
    if not isinstance(service, IntelligenceService):
        raise RuntimeError("INTELLIGENCE_SERVICE_NOT_BOUND")
    job = store.row(
        "SELECT * FROM jobs WHERE id = ? AND workspace_id = ?",
        (claim["id"], claim["workspace_id"]),
    )
    if job is None or job["lease_generation"] != claim["generation"]:
        return
    try:
        payload = json.loads(job["payload_json"])
        turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        request_data = payload.get("request") if isinstance(payload, dict) else None
        if not all(isinstance(value, str) and value for value in (turn_id, conversation_id)) or not isinstance(request_data, dict):
            raise IntelligenceError("INTELLIGENCE_JOB_INPUT_INVALID", 409)
        turn = store.row(
            "SELECT * FROM intelligence_turns WHERE turn_id = ? AND job_id = ? AND workspace_id = ? AND owner_user_id = ?",
            (turn_id, claim["id"], claim["workspace_id"], claim["user_id"]),
        )
        conversation = store.row(
            "SELECT * FROM intelligence_conversations WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ?",
            (conversation_id, claim["workspace_id"], claim["user_id"]),
        )
        if turn is None or conversation is None or conversation["status"] != "active":
            raise IntelligenceError("INTELLIGENCE_CONVERSATION_UNAVAILABLE", 409)
        identity = _worker_identity(store, claim, turn)
        saved_context = _read_json(turn["context_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", dict)
        resume_checkpoint = saved_context.get("resume_checkpoint")
        resume_tool_turn = 0
        saved_tool_results = []
        if resume_checkpoint is not None:
            if not isinstance(resume_checkpoint, dict) or resume_checkpoint.get("schema_version") != 1:
                raise IntelligenceError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", 409)
            if resume_checkpoint.get("stage") == "after_saved_tools":
                saved_tool_results = saved_context.get("tool_results")
                if (not isinstance(saved_tool_results, list) or not saved_tool_results
                        or _digest(saved_tool_results) != resume_checkpoint.get("tool_results_sha256")):
                    raise IntelligenceError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", 409)
                resume_tool_turn = resume_checkpoint.get("tool_turn")
                if isinstance(resume_tool_turn, bool) or not isinstance(resume_tool_turn, int) or not 1 <= resume_tool_turn <= 8:
                    raise IntelligenceError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", 409)
            elif resume_checkpoint.get("stage") != "model_not_started":
                raise IntelligenceError("INTELLIGENCE_RESUME_CHECKPOINT_INVALID", 409)
        if _job_is_cancelled(store, claim["id"], claim["workspace_id"], claim["generation"]):
            _finish_job_without_model(store, claim, turn_id, conversation_id, "cancelled", "INTELLIGENCE_TURN_CANCELLED")
            return
        if not _advance(store, claim["id"], claim["generation"], 0, 1, "读取已授权工作上下文"):
            _finish_job_without_model(store, claim, turn_id, conversation_id, "cancelled", "INTELLIGENCE_TURN_CANCELLED")
            return

        work_id = request_data.get("work_id", conversation["work_id"])
        material_ids = request_data.get("material_ids")
        if material_ids is not None and not isinstance(material_ids, list):
            raise IntelligenceError("INTELLIGENCE_CONVERSATION_SCOPE_UNAVAILABLE", 409)
        media_project_id = request_data.get("media_project_id", conversation["media_project_id"])
        prepared = service.prepare_context(
            identity, request_data["message"], work_id=work_id,
            material_ids=material_ids, query=request_data.get("query"),
            artifact_id=request_data.get("artifact_id"),
            artifact_version=request_data.get("artifact_version"),
            media_project_id=media_project_id,
            include_memory=request_data.get("include_memory", True),
            requested_output_formats=tuple(request_data.get("requested_output_formats", [])),
            artifact_write_intent=request_data.get("artifact_write_intent", True),
        )
        skill_snapshots = _read_json(
            conversation["skill_snapshots_json"], "INTELLIGENCE_CONVERSATION_SKILLS_UNAVAILABLE", list,
        )
        prepared = service.with_skill_context(prepared, skill_snapshots)
        history = _previous_conversation_messages(store, conversation_id, turn["sequence"])
        prior_source_refs = _read_json(
            conversation["source_refs_json"], "INTELLIGENCE_CONVERSATION_CONTEXT_UNAVAILABLE", list,
        )
        prior_citations = _read_json(
            conversation["citations_json"], "INTELLIGENCE_CONVERSATION_CONTEXT_UNAVAILABLE", list,
        )
        prepared = service.with_conversation_context(
            prepared, conversation_id=conversation_id, history=history,
            prior_source_refs=prior_source_refs, prior_citations=prior_citations,
            include_history=not (
                conversation["selection_protocol"] == "openai_responses"
                and conversation["codex_thread_id"] is not None
            ),
        )
        if resume_checkpoint is not None:
            if saved_context.get("versions") is not None:
                with store.transaction(immediate=False) as connection:
                    _validate_paused_context(connection, identity, request_data, saved_context, service)
            if resume_checkpoint.get("stage") == "after_saved_tools":
                saved_source_refs = _read_json(
                    turn["source_refs_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", list,
                )
                saved_citations = _read_json(
                    turn["citations_json"], "INTELLIGENCE_TURN_STATE_UNAVAILABLE", list,
                )
                prepared = service.resume_from_saved_tool_results(
                    prepared, tool_results=saved_tool_results,
                    source_refs=saved_source_refs, citations=saved_citations,
                    operation_scope=turn_id,
                    is_cancelled=lambda: _job_is_cancelled(
                        store, claim["id"], claim["workspace_id"], claim["generation"],
                    ),
                )
        context_json = service.public_context(prepared)
        context_json["resume_checkpoint"] = {
            "schema_version": 1, "stage": "model_attempt_possible", "tool_turn": resume_tool_turn,
        }
        with store.transaction() as connection:
            if claim_can_commit(connection, claim):
                connection.execute(
                    "UPDATE intelligence_turns SET status = 'running', context_json = ?, source_refs_json = ?, updated_at = ? "
                    "WHERE turn_id = ? AND job_id = ? AND status = 'queued'",
                    (json.dumps(context_json, ensure_ascii=False, sort_keys=True),
                     json.dumps(list(prepared.source_refs), ensure_ascii=False, sort_keys=True), utc_now(),
                     turn_id, claim["id"]),
                )
                connection.execute(
                    "UPDATE intelligence_conversations SET source_refs_json = ?, citations_json = ?, version = version + 1, updated_at = ? "
                    "WHERE conversation_id = ? AND workspace_id = ? AND owner_user_id = ? AND status = 'active'",
                    (json.dumps(list(prepared.source_refs), ensure_ascii=False, sort_keys=True),
                     json.dumps(list(prepared.citations), ensure_ascii=False, sort_keys=True), utc_now(),
                     conversation_id, claim["workspace_id"], claim["user_id"]),
                )
            else:
                stopping = connection.execute(
                    "SELECT status,pause_requested,cancel_requested,lease_generation FROM jobs "
                    "WHERE id=? AND workspace_id=?",
                    (claim["id"], claim["workspace_id"]),
                ).fetchone()
                if (stopping is not None and stopping["lease_generation"] == claim["generation"]
                        and stopping["status"] == "pause_requested" and stopping["pause_requested"] == 1
                        and not stopping["cancel_requested"]):
                    safe_context = service.public_context(prepared)
                    safe_context["resume_checkpoint"] = {
                        "schema_version": 1, "stage": "model_not_started", "tool_turn": 0,
                    }
                    connection.execute(
                        "UPDATE intelligence_turns SET status='interrupted', context_json=?, source_refs_json=?, "
                        "error_code='INTELLIGENCE_PAUSED_BEFORE_MODEL', error_message=?, updated_at=? "
                        "WHERE turn_id=? AND job_id=? AND status IN ('queued','running')",
                        (json.dumps(safe_context, ensure_ascii=False, sort_keys=True),
                         json.dumps(list(prepared.source_refs), ensure_ascii=False, sort_keys=True),
                         _safe_turn_error_message("INTELLIGENCE_PAUSED_BEFORE_MODEL"), utc_now(),
                         turn_id, claim["id"]),
                    )
                return

        def cancelled() -> bool:
            return _job_is_cancelled(store, claim["id"], claim["workspace_id"], claim["generation"])

        event_publisher = app.extensions.get("oevira.intelligence_event_publisher")

        def publish(event: dict) -> None:
            if callable(event_publisher):
                event_publisher(identity, conversation_id, turn_id, event)

        with app.test_request_context(
            "/api/intelligence/worker/turn",
            headers={"Idempotency-Key": "intelligence-turn-" + turn_id},
        ):
            g.identity = prepared.identity
            result = service.run_model(
                prepared, selection_id=request_data.get("selection_id"),
                _turn=resume_tool_turn,
                codex_thread_id=conversation["codex_thread_id"],
                conversation_id=conversation_id, operation_scope=turn_id,
                is_cancelled=cancelled, on_event=publish if callable(event_publisher) else None,
            )
        if not isinstance(result, dict):
            result = {"status": "error", "code": "INTELLIGENCE_RESULT_INVALID", "execution": "ATTEMPTED"}
        tool_results = result.get("tool_results", []) if isinstance(result.get("tool_results"), list) else []
        if not tool_results and prepared.tool_results:
            tool_results = list(prepared.tool_results)
        media_project_id = request_data.get("media_project_id")
        for operation in tool_results:
            if not isinstance(operation, dict) or operation.get("owner") != "Media":
                continue
            value = operation.get("result")
            project = value.get("project") if isinstance(value, dict) else None
            if isinstance(project, dict) and isinstance(project.get("id"), str):
                media_project_id = project["id"]
        turn_status, job_status, error_code = _final_turn_state(result, tool_results)
        final_source_refs = result.get("_source_refs")
        if not isinstance(final_source_refs, list):
            final_source_refs = list(prepared.source_refs)
        final_citations = result.get("_citations")
        if not isinstance(final_citations, list):
            final_citations = list(prepared.citations)
        final_context = result.get("_prepared_context")
        if isinstance(final_context, type(prepared)) and final_context._issuer is prepared._issuer:
            safe_context = service.public_context(final_context)
        else:
            safe_context = service.public_context(prepared)
            safe_context["tool_results"] = tool_results
        current_job = store.row(
            "SELECT status, pause_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], claim["workspace_id"]),
        )
        pause_checkpoint = None
        if (current_job is not None and current_job["lease_generation"] == claim["generation"]
                and current_job["status"] == "pause_requested" and current_job["pause_requested"] == 1
                and turn_status != "completed"):
            if result.get("execution") == "NOT_RUN":
                stage = "after_saved_tools" if tool_results else "model_not_started"
                saved_tool_turn = result.get("_tool_turn")
                if isinstance(saved_tool_turn, bool) or not isinstance(saved_tool_turn, int):
                    saved_tool_turn = resume_tool_turn
                pause_checkpoint = {
                    "schema_version": 1, "stage": stage,
                    "tool_turn": max(1, saved_tool_turn) if tool_results else 0,
                    "tool_results_sha256": _digest(tool_results) if tool_results else None,
                }
            else:
                pause_checkpoint = {
                    "schema_version": 1, "stage": "outcome_unknown",
                    "tool_results_sha256": _digest(tool_results) if tool_results else None,
                }
            safe_context["resume_checkpoint"] = pause_checkpoint
        assistant_text = result.get("text") if turn_status == "completed" and isinstance(result.get("text"), str) else None
        tool_summaries = _safe_tool_summaries(tool_results)
        safe_error_message = _safe_turn_error_message(error_code)
        artifact_id = _artifact_from_tool_results(store, claim["workspace_id"], tool_results)
        _finish_intelligence_turn(
            store, claim, turn_id, conversation_id, turn_status=turn_status,
            job_status=job_status, error_code=error_code,
            error_message=safe_error_message, assistant_text=assistant_text,
            context_json=safe_context, source_refs=final_source_refs,
            citations=final_citations, tool_results=tool_summaries,
            pause_checkpoint=pause_checkpoint,
            media_project_id=media_project_id,
            codex_thread_id=result.get("codex_thread_id"), artifact_id=artifact_id,
            provider=result.get("provider"), model_id=result.get("model_id"),
            model_revision=result.get("model_revision"),
            generation=claim["generation"], job_id=claim["id"], workspace_id=claim["workspace_id"],
        )
    except IntelligenceError as exc:
        _finish_job_without_model(
            store, claim, turn_id if "turn_id" in locals() else None,
            conversation_id if "conversation_id" in locals() else None,
            "failed", exc.code,
        )
    except Exception:
        _finish_job_without_model(
            store, claim, turn_id if "turn_id" in locals() else None,
            conversation_id if "conversation_id" in locals() else None,
            "failed", "INTELLIGENCE_WORKER_FAILED",
        )


def _safe_turn_error_message(code: str | None) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    safe_messages = {
        "MODEL_SELECTION_OWNER_NOT_BOUND": "选择的模型设置当前不可用。",
        "MODEL_NOT_SELECTED": "请先连接并选择一个模型。",
        "MODEL_DATA_USE_NOT_AUTHORIZED": "尚未允许将当前选定资料发送到此模型。",
        "MODEL_SELECTION_CHANGED": "模型设置已变化；请核对当前连接后重新开始。",
        "USER_MODEL_SETTINGS_CHANGED": "模型连接版本已变化；当前任务未继续使用旧设置。",
        "MODEL_NOT_SELECTED": "请先选择一个可用模型。",
        "SOURCE_USE_OWNER_NOT_BOUND": "当前来源用途权限尚未接通，未向模型发送材料。",
        "SOURCE_USE_NOT_AUTHORIZED": "当前来源用途未获准，未向模型发送材料。",
        "EXTERNAL_EFFECT_AUTHORIZER_NOT_BOUND": "外部模型请求授权尚未接通，未发送材料。",
        "EXTERNAL_MODEL_APPROVAL_REQUIRED": "此请求需要用户确认，尚未发送材料。",
        "CODEX_APP_SERVER_RUNTIME_NOT_BOUND": "Codex 推理运行时尚未接通。",
        "INTELLIGENCE_TASK_CANCELLED": "任务已取消，未开始模型推理。",
        "INTELLIGENCE_PAUSED_BEFORE_MODEL": "任务已在模型请求前安全暂停，可以继续。",
        "INTELLIGENCE_PAUSED_AFTER_TOOL_RESULTS": "专业操作结果已保存；继续时会先读回这些结果，不重复执行。",
        "INTELLIGENCE_PAUSE_OUTCOME_UNKNOWN": "暂停时存在未确认的模型或工具结果；任务保持暂停，请先核对已保存成果。",
        "CODEX_TURN_TIMEOUT": "模型推理超过当前任务时限，结果需要核对。",
        "MODEL_EXECUTION_FAILED": "模型请求未能完成，远端结果可能需要核对。",
        "INTELLIGENCE_OUTCOME_UNKNOWN": "模型结果尚未确认；请先检查已保存内容再继续。",
    }
    return safe_messages.get(code, "任务未能完成，已保留可读回的结果；请检查后再继续。")


def _finish_job_without_model(store, claim: dict, turn_id: str | None,
                              conversation_id: str | None, status: str, code: str) -> None:
    from .worker import claim_can_commit

    now = utc_now()
    try:
        with store.transaction() as connection:
            job = connection.execute(
                "SELECT status, cancel_requested, pause_requested, lease_generation "
                "FROM jobs WHERE id = ? AND workspace_id = ?",
                (claim["id"], claim["workspace_id"]),
            ).fetchone()
            if (job is None or job["lease_generation"] != claim["generation"]
                    or not claim_can_commit(connection, claim, allow_stopping=True)):
                return
            if job["status"] == "pause_requested" and job["pause_requested"] == 1:
                if turn_id:
                    turn = connection.execute(
                        "SELECT status,context_json,tool_results_json,provider,model_id,assistant_text "
                        "FROM intelligence_turns WHERE turn_id=? AND job_id=? AND workspace_id=? AND owner_user_id=?",
                        (turn_id, claim["id"], claim["workspace_id"], claim["user_id"]),
                    ).fetchone()
                    safely_not_started = False
                    if turn is not None and code == "INTELLIGENCE_TURN_CANCELLED" and turn["status"] == "queued":
                        try:
                            turn_context = json.loads(turn["context_json"] or "{}")
                            prior_tools = json.loads(turn["tool_results_json"] or "[]")
                        except (TypeError, ValueError):
                            turn_context, prior_tools = None, None
                        safely_not_started = (
                            turn_context == {} and prior_tools == [] and turn["provider"] is None
                            and turn["model_id"] is None and turn["assistant_text"] is None
                        )
                    if turn is not None:
                        if safely_not_started:
                            turn_context = {
                                "resume_checkpoint": {
                                    "schema_version": 1, "stage": "model_not_started", "tool_turn": 0,
                                },
                            }
                            code = "INTELLIGENCE_PAUSED_BEFORE_MODEL"
                        else:
                            turn_context = json.loads(turn["context_json"] or "{}")
                        connection.execute(
                            "UPDATE intelligence_turns SET status='interrupted', context_json=?, error_code=?, "
                            "error_message=?, updated_at=? WHERE turn_id=? AND job_id=? AND status IN ('queued','running')",
                            (json.dumps(turn_context, ensure_ascii=False, sort_keys=True),
                             code, _safe_turn_error_message(code), now, turn_id, claim["id"]),
                        )
                connection.execute(
                    "UPDATE jobs SET error_code=?, error_message=?, updated_at=? "
                    "WHERE id=? AND workspace_id=? AND lease_generation=? AND status='pause_requested'",
                    (code, _safe_turn_error_message(code), now, claim["id"], claim["workspace_id"], claim["generation"]),
                )
                return
            if job["status"] not in {"running", "cancel_requested"}:
                if turn_id and job["status"] in {"cancelled", "interrupted", "failed", "partial", "completed"}:
                    connection.execute(
                        "UPDATE intelligence_turns SET status = ?, error_code = COALESCE(error_code, ?), "
                        "error_message = COALESCE(error_message, ?), updated_at = ? "
                        "WHERE turn_id = ? AND job_id = ? AND status IN ('queued', 'running')",
                        (job["status"], code, _safe_turn_error_message(code), now, turn_id, claim["id"]),
                    )
                return
            actual_status = "cancelled" if job["cancel_requested"] and status == "failed" else status
            if turn_id:
                connection.execute(
                    "UPDATE intelligence_turns SET status = ?, error_code = ?, error_message = ?, updated_at = ? "
                    "WHERE turn_id = ? AND job_id = ? AND status IN ('queued', 'running')",
                    (actual_status, code, _safe_turn_error_message(code), now, turn_id, claim["id"]),
                )
            connection.execute(
                "UPDATE jobs SET status = ?, error_code = ?, error_message = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND lease_generation = ?",
                (actual_status, code, _safe_turn_error_message(code), now,
                 claim["id"], claim["workspace_id"], claim["generation"]),
            )
    except Exception:
        return


def _finish_intelligence_turn(store, claim: dict, turn_id: str, conversation_id: str, *,
                              turn_status: str, job_status: str, error_code: str | None,
                              error_message: str | None, assistant_text: str | None,
                              context_json: dict, source_refs: list[dict], citations: list[dict],
                              tool_results: list[dict], media_project_id: str | None,
                              codex_thread_id: str | None,
                              artifact_id: str | None, provider: str | None, model_id: str | None,
                              pause_checkpoint: dict | None = None,
                              model_revision: str | None, generation: int, job_id: str,
                              workspace_id: str) -> None:
    from .worker import claim_can_commit

    now = utc_now()
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, cancel_requested, pause_requested, suspended_from, lease_generation "
            "FROM jobs WHERE id = ? AND workspace_id = ?",
            (job_id, workspace_id),
        ).fetchone()
        if job is None or job["lease_generation"] != generation or not claim_can_commit(
            connection, claim, allow_stopping=True,
        ):
            return
        final_turn_status = turn_status
        final_job_status = job_status
        final_pause_requested = job["pause_requested"]
        final_suspended_from = job["suspended_from"]
        final_error_code = error_code
        final_error_message = error_message
        if job["status"] == "pause_requested" and job["pause_requested"] == 1:
            stage = pause_checkpoint.get("stage") if isinstance(pause_checkpoint, dict) else "outcome_unknown"
            if turn_status == "completed":
                final_job_status = "completed"
                final_pause_requested = 0
                final_suspended_from = None
            elif stage == "model_not_started":
                final_turn_status = "interrupted"
                final_job_status = "pause_requested"
                final_error_code = "INTELLIGENCE_PAUSED_BEFORE_MODEL"
                final_error_message = _safe_turn_error_message(final_error_code)
            elif stage == "after_saved_tools":
                final_turn_status = "partial"
                final_job_status = "pause_requested"
                final_error_code = "INTELLIGENCE_PAUSED_AFTER_TOOL_RESULTS"
                final_error_message = _safe_turn_error_message(final_error_code)
            else:
                final_turn_status = "partial" if tool_results else "interrupted"
                final_job_status = "pause_requested"
                final_error_code = "INTELLIGENCE_PAUSE_OUTCOME_UNKNOWN"
                final_error_message = _safe_turn_error_message(final_error_code)
        elif job["cancel_requested"] or job["status"] == "cancel_requested":
            final_pause_requested = 0
            final_suspended_from = None
            if tool_results:
                final_turn_status = final_job_status = "partial"
                final_error_code = final_error_code or "INTELLIGENCE_TURN_CANCELLED_AFTER_EFFECT"
                final_error_message = final_error_message or _safe_turn_error_message(final_error_code)
            else:
                final_turn_status = final_job_status = "cancelled"
                final_error_code = final_error_code or "INTELLIGENCE_TURN_CANCELLED"
                final_error_message = final_error_message or _safe_turn_error_message(final_error_code)
        connection.execute(
            "UPDATE intelligence_turns SET status = ?, context_json = ?, source_refs_json = ?, "
            "assistant_text = ?, citations_json = ?, tool_results_json = ?, provider = ?, model_id = ?, "
            "model_revision = ?, error_code = ?, error_message = ?, updated_at = ? "
            "WHERE turn_id = ? AND job_id = ? AND status IN ('queued', 'running')",
            (
                final_turn_status,
                json.dumps(context_json, ensure_ascii=False, sort_keys=True),
                json.dumps(source_refs, ensure_ascii=False, sort_keys=True),
                assistant_text,
                json.dumps(citations, ensure_ascii=False, sort_keys=True),
                json.dumps(tool_results, ensure_ascii=False, sort_keys=True),
                provider, model_id, model_revision, final_error_code, final_error_message, now,
                turn_id, job_id,
            ),
        )
        connection.execute(
            "UPDATE intelligence_conversations SET media_project_id = COALESCE(?, media_project_id), "
            "codex_thread_id = COALESCE(?, codex_thread_id), "
            "source_refs_json = ?, citations_json = ?, version = version + 1, updated_at = ? "
            "WHERE conversation_id = ? AND workspace_id = ? AND status = 'active'",
            (media_project_id, codex_thread_id, json.dumps(source_refs, ensure_ascii=False, sort_keys=True),
             json.dumps(citations, ensure_ascii=False, sort_keys=True), now,
             conversation_id, workspace_id),
        )
        connection.execute(
            "UPDATE jobs SET status = ?, pause_requested = ?, suspended_from = ?, progress_completed = 1, progress_total = 1, "
            "progress_label = ?, artifact_id = COALESCE(?, artifact_id), error_code = ?, "
            "error_message = ?, updated_at = ? "
            "WHERE id = ? AND workspace_id = ? AND lease_generation = ?",
            (
                final_job_status,
                final_pause_requested, final_suspended_from,
                "智能助手已完成并保存结果" if final_job_status == "completed"
                else "已保存工具结果，等待安全续接" if final_error_code == "INTELLIGENCE_PAUSED_AFTER_TOOL_RESULTS"
                else "模型尚未启动，任务已安全暂停" if final_error_code == "INTELLIGENCE_PAUSED_BEFORE_MODEL"
                else "执行结果需要核对；不会自动重放" if final_error_code == "INTELLIGENCE_PAUSE_OUTCOME_UNKNOWN"
                else "已保留可读回的部分结果" if final_job_status == "partial"
                else "任务已取消" if final_job_status == "cancelled"
                else "智能助手任务未完成",
                artifact_id, final_error_code, final_error_message, now,
                job_id, workspace_id, generation,
            ),
        )
