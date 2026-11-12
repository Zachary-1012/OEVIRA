"""Authenticated Media project, resumable Material upload, and render routes.

Media projects use immutable Media-owned versions. Source bytes remain in the
existing Material object store; render outputs are derived files bound to one
project version and one durable Worker job.
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
import os
import copy
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from fractions import Fraction

from flask import current_app, g, has_request_context, make_response, request, send_file

from .api import (
    ApiError,
    _body,
    _idempotency_key,
    _not_found,
    _request_fingerprint,
    _require_write,
    _resource_path,
    _string,
    _success,
    _work_row,
)
from .auth import utc_now
from .collaboration import require_artifact_access, require_material_access, require_work_access, require_workspace_access, work_commission
from .media import (
    AudioMix,
    CaptionStyle,
    FrameCrop,
    MediaAsset,
    Timeline,
    TimelineTrack,
    TranscriptWord,
    VideoEditingError,
    VideoPlacement,
    _MEDIA_ASSET_FACTORY_TOKEN,
    _fraction_time,
    media_stream_input_origin,
    _time_json,
    fingerprint_local_media,
    install_media_schema,
    restore_media_asset,
    sha256_media_file,
)
from .media_rendering import (
    encode_audio_delivery,
    chapter_document,
    extract_waveform,
    preview_audio_timeline_change,
    preview_audio_processing,
    render_timeline,
    resolve_output_spec,
    subtitle_documents,
    validate_render_options,
)
from .media_speech import (
    ASR_CHUNK_SECONDS,
    ASR_PADDING_SECONDS,
    ASR_SAMPLE_RATE,
    MAX_ASR_PCM_BYTES,
    MediaSpeechError,
    inspect_audio_packet_clock,
    normalize_audio_channel,
    preserve_audio_packet_clock_gaps,
    speech_capabilities,
    synthesize_espeak_speech,
    synthesize_flite_speech,
    verify_whisper_runtime,
)

MEDIA_CHUNK_BYTES = 8 * 1024 * 1024
MEDIA_UPLOAD_RESERVE_BYTES = 64 * 1024 * 1024
MEDIA_PROJECT_SCHEMA = "oevira.media.project.v1"
MEDIA_PROJECT_EXPORT_SCHEMA = "oevira.media.project.export.v1"
MEDIA_PROJECT_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_TARGET_WORK_UNSET = object()
_MEDIA_SOURCE_MIME_TYPES = {
    ".aac": "audio/aac",
    ".aiff": "audio/aiff",
    ".avi": "video/x-msvideo",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpegts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ogg": "audio/ogg",
    ".ts": "video/mp2t",
    ".wav": "audio/wav",
    ".webm": "video/webm",
}
_MEDIA_OUTPUT_MIME_TYPES = {
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "wav": "audio/wav",
}


def _json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value) -> dict:
    if isinstance(value, Fraction):
        return {"num": value.numerator, "den": value.denominator}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _fingerprint(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _required_idempotency_key() -> str:
    key = _idempotency_key()
    if key is None:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required", 400)
    return key


def _workspace_id() -> str:
    return g.identity["workspace"]["id"]


def _request_identity_for_workspace(workspace_id: str) -> dict | None:
    if not has_request_context():
        return None
    identity = g.get("identity")
    if (not isinstance(identity, dict) or not isinstance(identity.get("workspace"), dict)
            or identity["workspace"].get("id") != workspace_id):
        return None
    return identity


def _require_media_project_access(store, identity: dict, project, *, write: bool = False) -> None:
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        if project["workspace_id"] != workspace_id:
            raise _not_found()
        if project["work_id"]:
            require_work_access(connection, identity, project["work_id"], write=write)
        else:
            require_workspace_access(connection, identity, write=write)


def _require_media_target_work_access(store, identity: dict, work_id: str | None, *, write: bool) -> None:
    with store.transaction(immediate=False) as connection:
        if work_id is None:
            require_workspace_access(connection, identity, write=write)
        else:
            require_work_access(connection, identity, work_id, write=write)


def _require_media_material_access_in_transaction(
    connection,
    identity: dict,
    material_id: str,
    *,
    target_work_id: str | None,
) -> dict:
    material = require_material_access(
        connection, identity, material_id, target_work_id=target_work_id,
    )
    source_scope = work_commission(connection, material["work_id"])
    target_scope = work_commission(connection, target_work_id)
    if source_scope is not None and source_scope != target_scope:
        raise ApiError(
            "CLIENT_SCOPE_MISMATCH",
            "Client-restricted source material cannot be used outside its commission",
            403,
        )
    return dict(material)


def _require_media_material_access(
    store,
    identity: dict,
    material_id: str,
    *,
    target_work_id: str | None,
) -> dict:
    with store.transaction(immediate=False) as connection:
        return _require_media_material_access_in_transaction(
            connection, identity, material_id, target_work_id=target_work_id,
        )


_MEDIA_STORYBOARD_SCHEMA = "oevira.media.storyboard.v1"
_MEDIA_STORYBOARD_KINDS = {
    "topic", "guest_research", "question", "story_beat", "scene", "shot", "cue",
    "sponsor_read", "intro", "outro", "note",
}
_MEDIA_STORYBOARD_STATUSES = {"draft", "planned", "ready", "recorded", "edited", "confirmed"}
_MEDIA_STORYBOARD_NODE_FIELDS = {
    "beat_id", "kind", "title", "question", "script_text", "purpose", "planned_duration_sec",
    "parent_beat_id", "cue_id", "speaker_label", "status", "source_refs", "clip_refs", "recording_refs",
}


def _empty_media_storyboard() -> dict:
    return {"schema": _MEDIA_STORYBOARD_SCHEMA, "nodes": []}


def _validate_media_storyboard_shape(storyboard: object, timeline_data: dict | None = None) -> dict:
    if storyboard is None:
        storyboard = _empty_media_storyboard()
    if (type(storyboard) is not dict or set(storyboard) != {"schema", "nodes"}
            or storyboard.get("schema") != _MEDIA_STORYBOARD_SCHEMA
            or not isinstance(storyboard.get("nodes"), list)):
        raise ApiError("MEDIA_STORYBOARD_INVALID", "The saved Media storyboard is invalid", 409)
    nodes = storyboard["nodes"]
    seen = set()
    node_by_id = {}
    clips = {
        clip.get("clip_id"): {"track": track, "clip": clip}
        for track in (timeline_data.get("tracks", []) if isinstance(timeline_data, dict) else [])
        for clip in (track.get("clips", []) if isinstance(track, dict) else [])
        if isinstance(clip, dict) and isinstance(clip.get("clip_id"), str)
    }
    for node in nodes:
        if (type(node) is not dict or set(node) != _MEDIA_STORYBOARD_NODE_FIELDS
                or not isinstance(node.get("beat_id"), str) or not node["beat_id"]
                or node["beat_id"] in seen or node.get("kind") not in _MEDIA_STORYBOARD_KINDS
                or node.get("status") not in _MEDIA_STORYBOARD_STATUSES):
            raise ApiError("MEDIA_STORYBOARD_INVALID", "A storyboard node is malformed or duplicated", 409)
        seen.add(node["beat_id"])
        node_by_id[node["beat_id"]] = node
        for field, maximum, required in (("title", 200, True), ("question", 5000, False),
                                         ("script_text", 10000, False), ("purpose", 2000, False),
                                         ("cue_id", 128, False), ("speaker_label", 128, False)):
            value = node.get(field)
            if value is None and not required:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(not char.isprintable() for char in value):
                raise ApiError("MEDIA_STORYBOARD_INVALID", f"Storyboard {field} is invalid", 400)
        duration = node.get("planned_duration_sec")
        if duration is not None:
            try:
                seconds = _fraction_time(duration, error_code="MEDIA_STORYBOARD_DURATION_INVALID")
            except VideoEditingError as exc:
                raise ApiError("MEDIA_STORYBOARD_DURATION_INVALID", "Storyboard planned duration must be rational seconds", 400) from exc
            if seconds < 0:
                raise ApiError("MEDIA_STORYBOARD_DURATION_INVALID", "Storyboard planned duration must not be negative", 400)
        parent = node.get("parent_beat_id")
        if parent is not None and (not isinstance(parent, str) or parent not in seen and parent not in {item.get("beat_id") for item in nodes if isinstance(item, dict)}):
            raise ApiError("MEDIA_STORYBOARD_PARENT_MISSING", "A storyboard parent node does not exist", 409)
        source_refs = node.get("source_refs")
        clip_refs = node.get("clip_refs")
        recording_refs = node.get("recording_refs")
        if not isinstance(source_refs, list) or not isinstance(clip_refs, list) or not isinstance(recording_refs, list):
            raise ApiError("MEDIA_STORYBOARD_INVALID", "Storyboard source, clip and take relations must be lists", 409)
        source_ids, clip_ids, recording_ids = set(), set(), set()
        for ref in source_refs:
            if (type(ref) is not dict or set(ref) != {"ref_id", "kind", "role", "material_id", "sha256", "artifact_id", "artifact_version"}
                    or not isinstance(ref.get("ref_id"), str) or not ref["ref_id"] or ref["ref_id"] in source_ids
                    or ref.get("kind") not in {"material", "artifact"} or not isinstance(ref.get("role"), str)):
                raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard source reference is malformed", 409)
            source_ids.add(ref["ref_id"])
            if ref["kind"] == "material":
                if (not isinstance(ref.get("material_id"), str) or not ref["material_id"]
                        or not isinstance(ref.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None
                        or ref.get("artifact_id") is not None or ref.get("artifact_version") is not None):
                    raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard Material reference is malformed", 409)
            elif (not isinstance(ref.get("artifact_id"), str) or not ref["artifact_id"]
                  or type(ref.get("artifact_version")) is not int or ref["artifact_version"] < 1
                  or not isinstance(ref.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None
                  or ref.get("material_id") is not None):
                raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard Artifact reference is malformed", 409)
        for ref in clip_refs:
            if (type(ref) is not dict or set(ref) != {"link_id", "clip_id", "role", "source_sha256"}
                    or not isinstance(ref.get("link_id"), str) or not ref["link_id"] or ref["link_id"] in clip_ids
                    or not isinstance(ref.get("clip_id"), str) or not ref["clip_id"]
                    or not isinstance(ref.get("role"), str) or not ref["role"]
                    or not isinstance(ref.get("source_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", ref["source_sha256"]) is None):
                raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_INVALID", "A storyboard clip relation is malformed", 409)
            if ref["clip_id"] not in clips:
                raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING", "A storyboard node references a clip absent from this version", 409,
                               {"clip_id": ref["clip_id"]})
            clip_ids.add(ref["link_id"])
        for ref in recording_refs:
            if (type(ref) is not dict or set(ref) != {"link_id", "session_id", "take_id", "project_version", "role", "clip_ids"}
                    or not isinstance(ref.get("link_id"), str) or not ref["link_id"] or ref["link_id"] in recording_ids
                    or not isinstance(ref.get("session_id"), str) or not ref["session_id"]
                    or not isinstance(ref.get("take_id"), str) or not ref["take_id"]
                    or type(ref.get("project_version")) is not int or ref["project_version"] < 1
                    or not isinstance(ref.get("role"), str) or not ref["role"]
                    or not isinstance(ref.get("clip_ids"), list)
                    or any(not isinstance(clip_id, str) or not clip_id for clip_id in ref["clip_ids"])
                    or len(ref["clip_ids"]) != len(set(ref["clip_ids"]))):
                raise ApiError("MEDIA_STORYBOARD_RECORDING_REFERENCE_INVALID", "A storyboard take relation is malformed", 409)
            recording_ids.add(ref["link_id"])
        if node["status"] == "recorded" and not recording_refs:
            raise ApiError("MEDIA_STORYBOARD_STATUS_REQUIRES_TAKE", "Mark a node recorded only after linking a completed take", 409)
        if node["status"] == "edited" and not clip_refs:
            raise ApiError("MEDIA_STORYBOARD_STATUS_REQUIRES_CLIP", "Mark a node edited only after linking an exact timeline clip", 409)
    for node in nodes:
        parent = node.get("parent_beat_id")
        visited = {node["beat_id"]}
        while parent is not None:
            if parent in visited:
                raise ApiError("MEDIA_STORYBOARD_PARENT_CYCLE", "Storyboard nesting cannot contain a cycle", 409)
            visited.add(parent)
            parent = node_by_id[parent].get("parent_beat_id")
    try:
        if len(_json(storyboard).encode("utf-8")) > MEDIA_PROJECT_MANIFEST_MAX_BYTES:
            raise ApiError("MEDIA_STORYBOARD_TOO_LARGE", "The saved storyboard exceeds the current project snapshot limit", 413)
    except (TypeError, ValueError) as exc:
        raise ApiError("MEDIA_STORYBOARD_INVALID", "The saved storyboard is not valid JSON data", 409) from exc
    return copy.deepcopy(storyboard)


def _require_media_storyboard_sources(connection, identity: dict, project,
                                      storyboard: object, *, target_work_id: str | None,
                                      timeline_data: dict | None = None) -> dict:
    value = _validate_media_storyboard_shape(storyboard, timeline_data)
    current_assets = {}
    if isinstance(timeline_data, dict):
        current_assets = {item.get("asset_id"): item for item in timeline_data.get("assets", []) if isinstance(item, dict)}
    current_clips = {
        clip.get("clip_id"): {"track": track, "clip": clip}
        for track in (timeline_data.get("tracks", []) if isinstance(timeline_data, dict) else [])
        for clip in (track.get("clips", []) if isinstance(track, dict) else [])
        if isinstance(clip, dict) and isinstance(clip.get("clip_id"), str)
    }
    for node in value["nodes"]:
        for ref in node["source_refs"]:
            if ref["kind"] == "material":
                material = _require_media_material_access_in_transaction(
                    connection, identity, ref["material_id"], target_work_id=target_work_id,
                )
                if material["sha256"] != ref["sha256"]:
                    raise ApiError("MEDIA_STORYBOARD_SOURCE_CHANGED", "A storyboard Material source changed from its frozen hash", 409,
                                   {"beat_id": node["beat_id"], "source_ref_id": ref["ref_id"]})
            else:
                artifact = require_artifact_access(connection, identity, ref["artifact_id"])
                source_scope, target_scope = work_commission(connection, artifact["work_id"]), work_commission(connection, target_work_id)
                if source_scope is not None and source_scope != target_scope:
                    raise ApiError("CLIENT_SCOPE_MISMATCH", "A storyboard Artifact reference belongs to a different client", 403)
                version = connection.execute(
                    "SELECT title,body,citations_json,input_snapshot_json,parent_version FROM artifact_versions "
                    "WHERE artifact_id=? AND workspace_id=? AND version=?",
                    (ref["artifact_id"], identity["workspace"]["id"], ref["artifact_version"]),
                ).fetchone()
                if version is None or _fingerprint(dict(version)) != ref["sha256"]:
                    raise ApiError("MEDIA_STORYBOARD_SOURCE_CHANGED", "A storyboard Artifact version is unavailable or changed", 409,
                                   {"beat_id": node["beat_id"], "source_ref_id": ref["ref_id"]})
        for ref in node["clip_refs"]:
            current = current_clips.get(ref["clip_id"])
            if current is None:
                raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING", "A storyboard node references a clip absent from this version", 409,
                               {"beat_id": node["beat_id"], "clip_id": ref["clip_id"]})
            asset = current_assets.get(current["clip"].get("media_asset_id"))
            if asset is None or asset.get("sha256") != ref["source_sha256"]:
                raise ApiError("MEDIA_STORYBOARD_CLIP_SOURCE_CHANGED", "A storyboard clip link now points at a different source; relink it explicitly", 409,
                               {"beat_id": node["beat_id"], "clip_id": ref["clip_id"]})
        for ref in node["recording_refs"]:
            session = connection.execute(
                "SELECT * FROM media_recording_sessions WHERE id=? AND workspace_id=? AND project_id=?",
                (ref["session_id"], identity["workspace"]["id"], project["id"]),
            ).fetchone()
            if (session is None or session["status"] != "completed" or session["take_id"] != ref["take_id"]
                    or session["project_version"] != ref["project_version"]):
                raise ApiError("MEDIA_STORYBOARD_RECORDING_REFERENCE_MISSING", "A storyboard take reference no longer resolves to its completed session", 409,
                               {"beat_id": node["beat_id"], "session_id": ref["session_id"]})
            if ref["clip_ids"]:
                for clip_id in ref["clip_ids"]:
                    current = current_clips.get(clip_id)
                    if (current is None or current["track"].get("recording_session_id") != ref["session_id"]
                            or current["track"].get("take_id") != ref["take_id"]):
                        raise ApiError("MEDIA_STORYBOARD_TAKE_CLIP_MAPPING_CHANGED",
                                       "A linked take clip no longer maps to the same completed recording session", 409,
                                       {"beat_id": node["beat_id"], "clip_id": clip_id})
            segments = connection.execute(
                "SELECT material_id,source_sha256 FROM media_recording_segments WHERE session_id=? AND workspace_id=? ORDER BY sequence",
                (ref["session_id"], identity["workspace"]["id"]),
            ).fetchall()
            for segment in segments:
                material = _require_media_material_access_in_transaction(
                    connection, identity, segment["material_id"], target_work_id=target_work_id,
                )
                if material["sha256"] != segment["source_sha256"]:
                    raise ApiError("MEDIA_STORYBOARD_RECORDING_SOURCE_CHANGED", "A linked take segment no longer matches its saved hash", 409,
                                   {"beat_id": node["beat_id"], "session_id": ref["session_id"]})
    return value


def _storyboard_text(value: object, field: str, maximum: int, *, required: bool = False):
    if value is None and not required:
        return None
    if (not isinstance(value, str) or required and not value.strip() or len(value) > maximum
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)):
        raise ApiError("MEDIA_STORYBOARD_INVALID", f"Storyboard {field} is invalid", 400)
    return value


def _normalize_storyboard_node_fields(value: dict, *, beat_id: str) -> dict:
    allowed = {"kind", "title", "question", "script_text", "purpose", "planned_duration_sec",
               "parent_beat_id", "cue_id", "speaker_label", "status"}
    if type(value) is not dict or set(value) - allowed:
        raise ApiError("MEDIA_STORYBOARD_INVALID", "The storyboard node contains unsupported fields", 400)
    kind = value.get("kind")
    if kind not in _MEDIA_STORYBOARD_KINDS:
        raise ApiError("MEDIA_STORYBOARD_INVALID", "Choose a supported storyboard node kind", 400)
    title = _storyboard_text(value.get("title"), "title", 200, required=True)
    question = _storyboard_text(value.get("question"), "question", 5000)
    script_text = _storyboard_text(value.get("script_text"), "script_text", 10000)
    purpose = _storyboard_text(value.get("purpose"), "purpose", 2000)
    cue_id = _storyboard_text(value.get("cue_id"), "cue_id", 128)
    speaker_label = _storyboard_text(value.get("speaker_label"), "speaker_label", 128)
    status = value.get("status", "draft")
    if status not in _MEDIA_STORYBOARD_STATUSES:
        raise ApiError("MEDIA_STORYBOARD_INVALID", "The storyboard status is unsupported", 400)
    parent_beat_id = value.get("parent_beat_id")
    if parent_beat_id is not None and (not isinstance(parent_beat_id, str) or not parent_beat_id):
        raise ApiError("MEDIA_STORYBOARD_PARENT_MISSING", "parent_beat_id must identify an existing node", 400)
    duration = value.get("planned_duration_sec")
    if duration is not None:
        try:
            fraction = _fraction_time(duration, error_code="MEDIA_STORYBOARD_DURATION_INVALID")
        except VideoEditingError as exc:
            raise ApiError("MEDIA_STORYBOARD_DURATION_INVALID", "planned_duration_sec must be rational seconds", 400) from exc
        if fraction < 0:
            raise ApiError("MEDIA_STORYBOARD_DURATION_INVALID", "planned_duration_sec cannot be negative", 400)
        duration = _time_json(fraction)
    return {
        "beat_id": beat_id, "kind": kind, "title": title,
        "question": question, "script_text": script_text, "purpose": purpose,
        "planned_duration_sec": duration, "parent_beat_id": parent_beat_id,
        "cue_id": cue_id, "speaker_label": speaker_label, "status": status,
        "source_refs": [], "clip_refs": [], "recording_refs": [],
    }


def _storyboard_source_ref(connection, identity: dict, project, value: dict,
                           *, ref_id: str | None = None) -> dict:
    if type(value) is not dict:
        raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard source reference must be an object", 400)
    kind = value.get("kind")
    role = _storyboard_text(value.get("role", "reference"), "source role", 100, required=True)
    identifier = value.get("material_id") if kind == "material" else value.get("artifact_id")
    if not isinstance(identifier, str) or not identifier:
        raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard source must identify a Material or Artifact", 400)
    reference_id = ref_id or value.get("ref_id") or str(uuid.uuid4())
    if not isinstance(reference_id, str) or not reference_id:
        raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "A storyboard source reference needs a stable ref_id", 400)
    if kind == "material":
        if set(value) - {"kind", "material_id", "role", "ref_id", "sha256"}:
            raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "The storyboard Material reference contains unsupported fields", 400)
        material = _require_media_material_access_in_transaction(
            connection, identity, identifier, target_work_id=project["work_id"],
        )
        if value.get("sha256") is not None and value["sha256"] != material["sha256"]:
            raise ApiError("MEDIA_STORYBOARD_SOURCE_CHANGED", "The selected Material no longer has the reviewed source hash", 409)
        return {"ref_id": reference_id, "kind": "material", "role": role,
                "material_id": material["id"], "sha256": material["sha256"],
                "artifact_id": None, "artifact_version": None}
    if kind == "artifact":
        if set(value) - {"kind", "artifact_id", "artifact_version", "role", "ref_id", "sha256"}:
            raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "The storyboard Artifact reference contains unsupported fields", 400)
        artifact_version = value.get("artifact_version")
        if type(artifact_version) is not int or artifact_version < 1:
            raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "Select an exact saved Artifact version", 400)
        artifact = require_artifact_access(connection, identity, identifier)
        source_scope = work_commission(connection, artifact["work_id"])
        target_scope = work_commission(connection, project["work_id"])
        if source_scope is not None and source_scope != target_scope:
            raise ApiError("CLIENT_SCOPE_MISMATCH", "The storyboard Artifact source belongs to a different client", 403)
        version_row = connection.execute(
            "SELECT title,body,citations_json,input_snapshot_json,parent_version FROM artifact_versions "
            "WHERE artifact_id=? AND workspace_id=? AND version=?",
            (identifier, identity["workspace"]["id"], artifact_version),
        ).fetchone()
        if version_row is None:
            raise ApiError("MEDIA_STORYBOARD_ARTIFACT_VERSION_UNAVAILABLE", "The selected Artifact version is unavailable", 409)
        digest = _fingerprint(dict(version_row))
        if value.get("sha256") is not None and value["sha256"] != digest:
            raise ApiError("MEDIA_STORYBOARD_SOURCE_CHANGED", "The selected Artifact no longer matches the reviewed version hash", 409)
        return {"ref_id": reference_id, "kind": "artifact", "role": role,
                "material_id": None, "sha256": digest,
                "artifact_id": identifier, "artifact_version": artifact_version}
    raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_INVALID", "Storyboard sources must be Materials or saved Artifacts", 400)


def _storyboard_clip_ref(timeline: Timeline, clip_id: str, role: str, *, link_id: str | None = None) -> dict:
    role = _storyboard_text(role, "clip relation role", 100, required=True)
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.clip_id == clip_id:
                asset = timeline._asset(clip.media_asset_id)
                return {"link_id": link_id or str(uuid.uuid4()), "clip_id": clip_id,
                        "role": role, "source_sha256": asset.sha256}
    raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING", "Select a clip in this Media version", 409)


def _storyboard_recording_ref(connection, identity: dict, project, session_id: str,
                              role: str, timeline: Timeline, *, node: dict,
                              link_id: str | None = None) -> dict:
    role = _storyboard_text(role, "take relation role", 100, required=True)
    session = connection.execute(
        "SELECT * FROM media_recording_sessions WHERE id=? AND workspace_id=? AND project_id=?",
        (session_id, identity["workspace"]["id"], project["id"]),
    ).fetchone()
    if session is None or session["status"] != "completed" or type(session["project_version"]) is not int:
        raise ApiError("MEDIA_STORYBOARD_TAKE_NOT_READY", "Link a completed take from this Media project", 409)
    if node.get("cue_id") is not None and session["cue_id"] != node["cue_id"]:
        raise ApiError("MEDIA_STORYBOARD_TAKE_CUE_MISMATCH",
                       "The completed take belongs to a different script cue; choose its matching cue or an exact take",
                       409, {"expected_cue_id": node["cue_id"], "take_cue_id": session["cue_id"]})
    if node.get("speaker_label") is not None and session["speaker_label"] != node["speaker_label"]:
        raise ApiError("MEDIA_STORYBOARD_TAKE_SPEAKER_MISMATCH",
                       "The completed take belongs to a different speaker label",
                       409, {"expected_speaker_label": node["speaker_label"],
                             "take_speaker_label": session["speaker_label"]})
    clip_ids = [clip.clip_id for track in timeline.tracks
                if track.recording_session_id == session["id"] and track.take_id == session["take_id"]
                for clip in track.clips]
    return {"link_id": link_id or str(uuid.uuid4()), "session_id": session["id"],
            "take_id": session["take_id"], "project_version": session["project_version"],
            "role": role, "clip_ids": clip_ids}


def _apply_media_storyboard_operations(store, identity: dict, project, storyboard: object,
                                       timeline: Timeline, operations: list[dict],
                                       *, base_timeline: Timeline | None = None) -> dict:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 128:
        raise ApiError("MEDIA_STORYBOARD_OPERATIONS_INVALID", "A storyboard edit needs between one and 128 named operations", 400)
    initial_timeline_data = (base_timeline or timeline).to_dict()
    value = _validate_media_storyboard_shape(storyboard, initial_timeline_data)
    with store.transaction(immediate=False) as connection:
        _require_media_storyboard_sources(connection, identity, project, value,
                                          target_work_id=project["work_id"],
                                          timeline_data=initial_timeline_data)
        for operation in operations:
            if type(operation) is not dict or not isinstance(operation.get("op"), str):
                raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "Each storyboard operation must be named", 400)
            name = operation["op"]
            nodes = value["nodes"]
            by_id = {node["beat_id"]: node for node in nodes}
            if name == "add_node":
                if set(operation) - {"op", "node", "position"} or type(operation.get("node")) is not dict:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "add_node needs a node and optional position", 400)
                node = _normalize_storyboard_node_fields(operation["node"], beat_id=str(uuid.uuid4()))
                position = operation.get("position", len(nodes))
                if type(position) is not int or not 0 <= position <= len(nodes):
                    raise ApiError("MEDIA_STORYBOARD_POSITION_INVALID", "Storyboard position must be in the current ordered list", 400)
                nodes.insert(position, node)
            elif name == "update_node":
                if set(operation) != {"op", "beat_id", "changes"} or not isinstance(operation["changes"], dict):
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "update_node needs beat_id and changes", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                fields = set(operation["changes"])
                editable = {"kind", "title", "question", "script_text", "purpose", "planned_duration_sec",
                            "parent_beat_id", "cue_id", "speaker_label", "status"}
                if not fields or fields - editable:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "update_node contains an unsupported field", 400)
                saved_sources = node["source_refs"]
                saved_clips = node["clip_refs"]
                saved_recordings = node["recording_refs"]
                node.update(_normalize_storyboard_node_fields({**{key: node[key] for key in editable},
                                                               **operation["changes"]}, beat_id=node["beat_id"]))
                # Content edits do not detach the already saved source/take/clip relations.
                node["source_refs"], node["clip_refs"], node["recording_refs"] = (
                    saved_sources, saved_clips, saved_recordings)
            elif name == "move_node":
                if set(operation) - {"op", "beat_id", "position", "parent_beat_id"} or not {"beat_id", "position"} <= set(operation):
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "move_node needs beat_id and position", 400)
                node = by_id.get(operation["beat_id"])
                position = operation["position"]
                if node is None or type(position) is not int or not 0 <= position < len(nodes):
                    raise ApiError("MEDIA_STORYBOARD_POSITION_INVALID", "The storyboard node or new position is invalid", 400)
                nodes.remove(node)
                nodes.insert(position, node)
                if "parent_beat_id" in operation:
                    node["parent_beat_id"] = operation["parent_beat_id"]
            elif name == "remove_node":
                if set(operation) != {"op", "beat_id"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "remove_node needs one beat_id", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                if any(item.get("parent_beat_id") == node["beat_id"] for item in nodes):
                    raise ApiError("MEDIA_STORYBOARD_NODE_HAS_CHILDREN", "Move or remove child nodes before removing this section", 409)
                nodes.remove(node)
            elif name == "add_source_ref":
                if set(operation) != {"op", "beat_id", "source_ref"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "add_source_ref needs a beat and an exact source", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                ref = _storyboard_source_ref(connection, identity, project, operation["source_ref"])
                if any(item["ref_id"] == ref["ref_id"] for item in node["source_refs"]):
                    raise ApiError("MEDIA_STORYBOARD_REFERENCE_DUPLICATE", "The storyboard node already has this source reference", 409)
                node["source_refs"].append(ref)
            elif name == "remove_source_ref":
                if set(operation) != {"op", "beat_id", "ref_id"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "remove_source_ref needs a stable ref_id", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                matches = [item for item in node["source_refs"] if item["ref_id"] == operation["ref_id"]]
                if len(matches) != 1:
                    raise ApiError("MEDIA_STORYBOARD_SOURCE_REFERENCE_MISSING", "The source reference is unavailable", 409)
                node["source_refs"].remove(matches[0])
            elif name == "link_clip":
                if set(operation) != {"op", "beat_id", "clip_id", "role"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "link_clip needs an exact clip and relationship role", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                ref = _storyboard_clip_ref(timeline, operation["clip_id"], operation["role"])
                if any(item["clip_id"] == ref["clip_id"] for item in node["clip_refs"]):
                    raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_DUPLICATE", "This clip is already linked to the storyboard node", 409)
                node["clip_refs"].append(ref)
            elif name == "relink_clip":
                if set(operation) != {"op", "beat_id", "link_id", "clip_id"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "relink_clip needs the existing link and exact replacement clip", 400)
                node = by_id.get(operation["beat_id"])
                ref = None if node is None else next((item for item in node["clip_refs"] if item["link_id"] == operation["link_id"]), None)
                if ref is None:
                    raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING", "The selected storyboard clip link is unavailable", 409)
                updated = _storyboard_clip_ref(timeline, operation["clip_id"], ref["role"], link_id=ref["link_id"])
                node["clip_refs"][node["clip_refs"].index(ref)] = updated
            elif name == "unlink_clip":
                if set(operation) != {"op", "beat_id", "link_id"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "unlink_clip needs a stable link_id", 400)
                node = by_id.get(operation["beat_id"])
                ref = None if node is None else next((item for item in node["clip_refs"] if item["link_id"] == operation["link_id"]), None)
                if ref is None:
                    raise ApiError("MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING", "The selected storyboard clip link is unavailable", 409)
                node["clip_refs"].remove(ref)
            elif name == "link_take":
                if set(operation) != {"op", "beat_id", "session_id", "role"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "link_take needs an exact recording session and role", 400)
                node = by_id.get(operation["beat_id"])
                if node is None:
                    raise ApiError("MEDIA_STORYBOARD_NODE_MISSING", "The selected storyboard node is unavailable", 409)
                ref = _storyboard_recording_ref(connection, identity, project, operation["session_id"],
                                                operation["role"], timeline, node=node)
                if any(item["session_id"] == ref["session_id"] for item in node["recording_refs"]):
                    raise ApiError("MEDIA_STORYBOARD_RECORDING_REFERENCE_DUPLICATE", "This take is already linked to the storyboard node", 409)
                node["recording_refs"].append(ref)
            elif name == "unlink_take":
                if set(operation) != {"op", "beat_id", "link_id"}:
                    raise ApiError("MEDIA_STORYBOARD_OPERATION_INVALID", "unlink_take needs a stable link_id", 400)
                node = by_id.get(operation["beat_id"])
                ref = None if node is None else next((item for item in node["recording_refs"] if item["link_id"] == operation["link_id"]), None)
                if ref is None:
                    raise ApiError("MEDIA_STORYBOARD_RECORDING_REFERENCE_MISSING", "The selected storyboard take link is unavailable", 409)
                node["recording_refs"].remove(ref)
            else:
                raise ApiError("MEDIA_STORYBOARD_OPERATION_UNSUPPORTED", "The storyboard operation is not supported", 400)
        result = _require_media_storyboard_sources(
            connection, identity, project, value,
            target_work_id=project["work_id"], timeline_data=timeline.to_dict(),
        )
    return result


def _require_media_project_version_sources(
    connection,
    identity: dict,
    project,
    version_row,
    *,
    target_work_id: str | None,
) -> dict:
    try:
        state = json.loads(version_row["state_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version cannot be read", 409) from exc
    timeline = state.get("timeline") if isinstance(state, dict) else None
    assets = timeline.get("assets") if isinstance(timeline, dict) else None
    if (not isinstance(state, dict) or state.get("schema") != MEDIA_PROJECT_SCHEMA
            or not isinstance(assets, list)):
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version has no source manifest", 409)
    _require_media_storyboard_sources(
        connection, identity, project, state.get("storyboard"),
        target_work_id=target_work_id, timeline_data=timeline,
    )
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ApiError("MEDIA_SOURCE_REFERENCE_INVALID", "A saved source reference is incomplete", 409)
        material_id = asset.get("source_material_id")
        expected_sha256 = asset.get("sha256")
        if (not isinstance(material_id, str) or not material_id
                or not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
            raise ApiError("MEDIA_SOURCE_REFERENCE_INVALID", "A saved source reference is incomplete", 409)
        if material_id in seen:
            continue
        seen.add(material_id)
        material = _require_media_material_access_in_transaction(
            connection, identity, material_id, target_work_id=target_work_id,
        )
        if material["sha256"] != expected_sha256:
            raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A selected media source no longer matches its saved version", 409)
    return state


def _require_media_render_access_in_transaction(
    connection,
    identity: dict,
    render_id: str,
    *,
    target_work_id: str | None | object = _TARGET_WORK_UNSET,
    expected_job_kind: str | None = None,
):
    workspace_id = identity["workspace"]["id"]
    if target_work_id is not _TARGET_WORK_UNSET:
        if target_work_id:
            require_work_access(connection, identity, target_work_id)
        else:
            require_workspace_access(connection, identity)
    render = connection.execute(
        "SELECT r.*, j.kind AS job_kind, j.status AS job_status, j.work_id AS job_work_id, "
        "j.payload_json AS job_payload_json "
        "FROM media_renders r JOIN jobs j ON j.id = r.job_id AND j.workspace_id = r.workspace_id "
        "WHERE r.id = ? AND r.workspace_id = ?",
        (render_id, workspace_id),
    ).fetchone()
    if render is None:
        raise _not_found()
    if expected_job_kind is not None and render["job_kind"] != expected_job_kind:
        raise _not_found()
    try:
        payload = json.loads(render["job_payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The saved Media render request cannot be read", 409) from exc
    if (not isinstance(payload, dict) or payload.get("project_id") != render["project_id"]
            or payload.get("version") != render["project_version"]
            or payload.get("format") != render["format"]
            or payload.get("output_kind") != render["output_kind"]):
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The saved Media render is not bound to its frozen request", 409)
    if expected_job_kind is not None and payload.get("purpose") != (
        "preview" if expected_job_kind == "media_preview" else "render"
    ):
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The render purpose differs from its job kind", 409)
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
        (render["project_id"], workspace_id),
    ).fetchone()
    if project is None:
        raise _not_found()
    if render["job_work_id"] != project["work_id"]:
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The render and Media project scope no longer match", 409)
    if project["work_id"]:
        require_work_access(connection, identity, project["work_id"])
    else:
        require_workspace_access(connection, identity)
    material_scope = project["work_id"]
    if target_work_id is not _TARGET_WORK_UNSET:
        source_scope = work_commission(connection, project["work_id"])
        target_scope = work_commission(connection, target_work_id)
        if source_scope is not None and source_scope != target_scope:
            raise ApiError("CLIENT_SCOPE_MISMATCH", "This Media render belongs to a different client", 403)
        material_scope = target_work_id

    version_row = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
        (render["project_id"], workspace_id, render["project_version"]),
    ).fetchone()
    if version_row is None:
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The rendered project version is unavailable", 409)
    _require_media_project_version_sources(
        connection, identity, project, version_row, target_work_id=material_scope,
    )
    return render


def require_media_delivery_access(
    connection,
    identity: dict,
    render_id: str,
    *,
    target_work_id: str,
):
    """Authorize one exact completed final render for a DeliveryBatch Work."""
    if not isinstance(target_work_id, str) or not target_work_id:
        raise _not_found()
    render = _require_media_render_access_in_transaction(
        connection, identity, render_id, target_work_id=target_work_id,
        expected_job_kind="media_render",
    )
    if (render["status"] != "completed" or render["job_status"] != "completed"
            or not render["sha256"] or type(render["size_bytes"]) is not int
            or render["size_bytes"] <= 0):
        raise ApiError("MEDIA_RENDER_NOT_DELIVERABLE", "Only a completed final Media render can be delivered", 409)
    try:
        payload = json.loads(render["job_payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The saved Media render request cannot be read", 409) from exc
    if payload.get("purpose") != "render":
        raise ApiError("MEDIA_RENDER_NOT_DELIVERABLE", "A preview cannot be added to a DeliveryBatch", 409)
    return render


def resolve_media_delivery_file(
    store,
    identity: dict,
    render_id: str,
    *,
    target_work_id: str,
) -> dict:
    with store.transaction(immediate=False) as connection:
        render = dict(require_media_delivery_access(
            connection, identity, render_id, target_work_id=target_work_id,
        ))
    path = _resource_path(store, render["relative_path"])
    if not path.is_file() or path.is_symlink():
        raise ApiError("MEDIA_RENDER_FILE_UNAVAILABLE", "The completed Media render file is unavailable", 409)
    initial_size = path.stat().st_size
    digest = sha256_media_file(path)
    final_size = path.stat().st_size
    if (initial_size != final_size or digest != render["sha256"]
            or final_size != render["size_bytes"]):
        raise ApiError("MEDIA_RENDER_FILE_INTEGRITY_ERROR", "The completed Media render failed integrity verification", 409)
    mime_type = _MEDIA_OUTPUT_MIME_TYPES.get(render["format"])
    if mime_type is None:
        raise ApiError("MEDIA_RENDER_FORMAT_UNSUPPORTED", "The completed Media render format cannot be delivered", 415)
    return {
        "path": path,
        "filename": f"media-{render['id']}.{render['format']}",
        "mime_type": mime_type,
        "sha256": digest,
        "size_bytes": final_size,
        "project_id": render["project_id"],
        "project_version": render["project_version"],
    }


def _project_row(store, workspace_id: str, project_id: str, *, identity: dict | None = None, write: bool | None = None):
    row = store.row(
        "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
        (project_id, workspace_id),
    )
    if row is None:
        raise _not_found()
    identity = identity or _request_identity_for_workspace(workspace_id)
    if identity is not None:
        if write is None:
            write = has_request_context() and request.method not in {"GET", "HEAD", "OPTIONS"}
        _require_media_project_access(store, identity, row, write=bool(write))
    return row


def _version_row(store, workspace_id: str, project_id: str, version: int, *, identity: dict | None = None, write: bool | None = None):
    _project_row(store, workspace_id, project_id, identity=identity, write=write)
    row = store.row(
        "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
        (project_id, workspace_id, version),
    )
    if row is None:
        raise _not_found()
    return row


def _default_tracks(kind: str) -> tuple[TimelineTrack, ...]:
    if kind == "video":
        return (
            TimelineTrack("video-main", "video", layout="sequence"),
            TimelineTrack("video-overlay", "video", layout="free"),
            TimelineTrack("voiceover", "voiceover", layout="free"),
            TimelineTrack("bgm", "bgm", layout="free"),
            TimelineTrack("sfx", "sfx", layout="free"),
        )
    return (
        TimelineTrack("voiceover", "voiceover", layout="free"),
        TimelineTrack("bgm", "bgm", layout="free"),
        TimelineTrack("sfx", "sfx", layout="free"),
    )


def _project_state(*, title: str, kind: str, timeline: Timeline) -> dict:
    return {
        "schema": MEDIA_PROJECT_SCHEMA,
        "title": title,
        "kind": kind,
        "clock": {"unit": "rational_seconds", "source_clocks_preserved": True},
        "timeline": timeline.to_dict(),
        "storyboard": _empty_media_storyboard(),
    }


def _load_timeline(
    store,
    workspace_id: str,
    state: dict,
    *,
    verify_files: bool = False,
    project_id: str | None = None,
    identity: dict | None = None,
    target_work_id: str | None | object = _TARGET_WORK_UNSET,
    write: bool | None = None,
):
    if not isinstance(state, dict) or state.get("schema") != MEDIA_PROJECT_SCHEMA:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version cannot be read", 409)
    identity = identity or _request_identity_for_workspace(workspace_id)
    if project_id is not None:
        project = _project_row(store, workspace_id, project_id, identity=identity, write=write)
        target_work_id = project["work_id"]
    elif identity is not None:
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, identity, write=bool(write))
            if target_work_id is not _TARGET_WORK_UNSET and target_work_id:
                require_work_access(connection, identity, target_work_id, write=bool(write))
    timeline_data = state.get("timeline")
    if not isinstance(timeline_data, dict):
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media timeline is unavailable", 409)
    assets = []
    availability = {}
    for snapshot in timeline_data.get("assets", []):
        material_id = snapshot.get("source_material_id") if isinstance(snapshot, dict) else None
        if not isinstance(material_id, str) or not material_id:
            raise ApiError("MEDIA_SOURCE_REFERENCE_INVALID", "A saved source reference is incomplete", 409)
        if identity is not None and target_work_id is not _TARGET_WORK_UNSET:
            existing_material = store.row(
                "SELECT id FROM materials WHERE id = ? AND workspace_id = ?",
                (material_id, workspace_id),
            )
            if existing_material is not None:
                _require_media_material_access(
                    store, identity, material_id, target_work_id=target_work_id,
                )
        material = store.row(
            "SELECT id, title, filename, sha256, relative_path, size_bytes, status FROM materials "
            "WHERE id = ? AND workspace_id = ?",
            (material_id, workspace_id),
        )
        if material is None:
            if verify_files:
                raise ApiError("MEDIA_SOURCE_UNAVAILABLE", "A selected Material source is no longer available", 409)
            # Keep the project timeline readable while marking the source offline.
            source_path = store.objects / workspace_id / "materials" / material_id / "original"
            availability[material_id] = "unavailable"
        else:
            source_path = _resource_path(store, material["relative_path"])
            metadata_matches = (
                material["sha256"] == snapshot.get("sha256")
                and material["filename"] == snapshot.get("filename")
            )
            file_present = source_path.is_file() and not source_path.is_symlink()
            file_matches = file_present and metadata_matches
            if verify_files and file_matches:
                file_matches = sha256_media_file(source_path) == material["sha256"]
            availability[material_id] = (
                "available" if verify_files and file_matches else
                "present_unverified" if file_matches else
                "integrity_error" if file_present else "unavailable"
            )
            if verify_files and availability[material_id] != "available":
                raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A selected media source is unavailable or changed", 409)
        try:
            assets.append(restore_media_asset(source_path, snapshot, material_id, verify_file=False))
        except VideoEditingError as exc:
            raise ApiError("MEDIA_SOURCE_REFERENCE_INVALID", "A saved source reference failed validation", 409) from exc
    try:
        timeline = Timeline.from_dict(timeline_data, assets=assets, verify_local_assets=False)
    except VideoEditingError as exc:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media project failed validation", 409) from exc
    return timeline, availability


def _project_view(
    store,
    workspace_id: str,
    project_id: str,
    version: int | None = None,
    *,
    identity: dict | None = None,
) -> dict:
    project = _project_row(store, workspace_id, project_id, identity=identity)
    selected_version = project["current_version"] if version is None else version
    if type(selected_version) is not int or selected_version < 1:
        raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
    saved = _version_row(store, workspace_id, project_id, selected_version, identity=identity)
    state = json.loads(saved["state_json"])
    timeline, availability = _load_timeline(
        store, workspace_id, state, project_id=project_id, identity=identity,
    )
    if identity is not None:
        with store.transaction(immediate=False) as connection:
            source_state = _require_media_project_version_sources(
                connection, identity, project, saved, target_work_id=project["work_id"],
            )
            state["storyboard"] = source_state.get("storyboard", _empty_media_storyboard())
    history = store.rows(
        "SELECT version, parent_version, operation, created_at, state_json FROM media_project_versions "
        "WHERE project_id = ? AND workspace_id = ? ORDER BY version",
        (project_id, workspace_id),
    )
    timeline_data = timeline.to_dict()
    return {
        "id": project["id"],
        "title": state["title"],
        "kind": state["kind"],
        "work_id": project["work_id"],
        "version": selected_version,
        "current_version": project["current_version"],
        "versions": [
            {
                "version": item["version"],
                "parent_version": item["parent_version"],
                "operation": item["operation"],
                "created_at": item["created_at"],
                "source_origin": json.loads(item["state_json"]).get("source_origin"),
            }
            for item in history
        ],
        "source_origin": state.get("source_origin"),
        "clock": state["clock"],
        "storyboard": copy.deepcopy(state.get("storyboard", _empty_media_storyboard())),
        "sources": [
            {
                **asset.to_dict(),
                "availability": availability.get(asset.source_material_id, "unavailable"),
            }
            for asset in timeline.assets
        ],
        "tracks": timeline_data["tracks"],
        "audio_buses": timeline_data.get("audio_buses", []),
        "video_color_interpretations": timeline_data.get("video_color_interpretations", []),
        "transcript_sources": [
            {
                **{key: value for key, value in transcript.to_dict().items() if key != "words"},
                "word_count": len(transcript.words),
                "transcript_sha256": _fingerprint(transcript.to_dict()),
            }
            for transcript in timeline.transcripts
        ],
        "transitions": timeline_data["transitions"],
        "overlays": timeline_data["overlays"],
        "chapters": timeline_data.get("chapters", []),
        "format_ratio": timeline_data["format_ratio"],
        "duration": {"num": timeline.duration_sec.numerator, "den": timeline.duration_sec.denominator},
    }


def _project_export_document(store, workspace_id: str, project_id: str, version: int) -> dict:
    project = _project_row(store, workspace_id, project_id)
    version_row = _version_row(store, workspace_id, project_id, version)
    state = json.loads(version_row["state_json"])
    identity = _request_identity_for_workspace(workspace_id)
    if identity is not None:
        with store.transaction(immediate=False) as connection:
            project_row = connection.execute(
                "SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (project_id, workspace_id),
            ).fetchone()
            if project_row is None:
                raise _not_found()
            _require_media_project_version_sources(
                connection, identity, project_row, version_row, target_work_id=project_row["work_id"],
            )
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    source_manifest = []
    slot_requirements = {
        asset.asset_id: {
            "video_stream_count": 0,
            "audio_stream_count": 0,
            "minimum_source_duration_sec": Fraction(0),
        }
        for asset in timeline.assets
    }
    assets_by_id = {asset.asset_id: asset for asset in timeline.assets}
    for track in timeline.tracks:
        for clip in track.clips:
            requirement = slot_requirements[clip.media_asset_id]
            if track.kind == "video":
                stream_index = 0 if clip.video_stream_index is None else clip.video_stream_index
                requirement["video_stream_count"] = max(requirement["video_stream_count"], stream_index + 1)
                asset = assets_by_id[clip.media_asset_id]
                if clip.audio_role == "original" and asset.audio_stream_count:
                    audio_index = 0 if clip.audio_stream_index is None else clip.audio_stream_index
                    requirement["audio_stream_count"] = max(requirement["audio_stream_count"], audio_index + 1)
            else:
                audio_index = clip.audio_stream_index if clip.audio_stream_index is not None else 0
                requirement["audio_stream_count"] = max(requirement["audio_stream_count"], audio_index + 1)
            requirement["minimum_source_duration_sec"] = max(
                requirement["minimum_source_duration_sec"], clip.source_out,
            )
    for asset in timeline.assets:
        material = store.row(
            "SELECT id, filename, format, sha256, size_bytes, status FROM materials "
            "WHERE id = ? AND workspace_id = ?",
            (asset.source_material_id, workspace_id),
        )
        if material is None or material["sha256"] != asset.sha256:
            raise ApiError("MEDIA_SOURCE_UNAVAILABLE", "A selected Material source is no longer available", 409)
        source_manifest.append({
            "asset_id": asset.asset_id,
            "material_id": material["id"],
            "filename": material["filename"],
            "format": material["format"],
            "sha256": material["sha256"],
            "size_bytes": material["size_bytes"],
            "material_status": material["status"],
            "source_state": "saved_media_original",
            "original_bytes_included": False,
            "original_bytes_verified": True,
            "rights_state": "unknown",
        })
    source_slots = []
    for asset in timeline.assets:
        requirement = slot_requirements[asset.asset_id]
        source_slots.append({
            "slot_id": asset.asset_id,
            "asset_id": asset.asset_id,
            "label": asset.filename,
            "media_kind": asset.media_kind,
            "required_streams": {
                "video": requirement["video_stream_count"],
                "audio": requirement["audio_stream_count"],
            },
            "source_inventory": {
                "video_streams": asset.video_stream_count,
                "audio_streams": asset.audio_stream_count,
            },
            "minimum_source_duration_sec": _time_json(requirement["minimum_source_duration_sec"]),
            "source_sha256": asset.sha256,
            "rights_state": "unknown",
        })

    return {
        "schema": MEDIA_PROJECT_EXPORT_SCHEMA,
        "project": {
            "title": state["title"],
            "kind": state["kind"],
            "format_ratio": timeline.format_ratio,
            "version": version,
            "created_at": project["created_at"],
            "version_created_at": version_row["created_at"],
            "parent_version": version_row["parent_version"],
            "operation": version_row["operation"],
            "source_origin": state.get("source_origin"),
        },
        "timeline": timeline.to_dict(),
        "storyboard": copy.deepcopy(state.get("storyboard", _empty_media_storyboard())),
        "sources": source_manifest,
        "source_slots": source_slots,
        "source_package_state": "material_references_only",
        "rights_state": "unknown",
        "publication_state": "not_performed",
    }


def _validate_source_origin(store, workspace_id: str, value: Any, *, version_index: int | None = None) -> dict:
    detail = {} if version_index is None else {"version_index": version_index}
    allowed = {
        "source_project_id", "source_package_sha256", "source_snapshot_sha256",
        "source_version", "source_created_at",
        "historical_state", "approval_status", "rights_state", "publication_state",
        "render_material_id", "render_sha256",
    }
    required = {"source_package_sha256", "source_version", "rights_state", "publication_state"}
    if type(value) is not dict or set(value) - allowed or not required <= set(value):
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "A source revision reference is malformed", 400, detail)
    package_hash = value.get("source_package_sha256")
    snapshot_hash = value.get("source_snapshot_sha256")
    source_project_id = value.get("source_project_id")
    source_version = value.get("source_version")
    created_at = value.get("source_created_at")
    historical_state = value.get("historical_state")
    approval_status = value.get("approval_status")
    render_hash = value.get("render_sha256")
    render_material_id = value.get("render_material_id")
    if (source_project_id is not None
            and (not isinstance(source_project_id, str) or not source_project_id or len(source_project_id) > 255)
            or not isinstance(package_hash, str) or re.fullmatch(r"[0-9a-f]{64}", package_hash) is None
            or snapshot_hash is not None
            and (not isinstance(snapshot_hash, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_hash) is None)
            or type(source_version) is not int or source_version < 1
            or created_at is not None and (not isinstance(created_at, str) or len(created_at) > 64)
            or historical_state is not None and (not isinstance(historical_state, str) or len(historical_state) > 128)
            or approval_status is not None and (not isinstance(approval_status, str) or len(approval_status) > 128)
            or value.get("rights_state") != "unknown"
            or value.get("publication_state") != "not_performed"
            or render_hash is not None and (
                not isinstance(render_hash, str) or re.fullmatch(r"[0-9a-f]{64}", render_hash) is None
            )
            or render_material_id is not None and (not isinstance(render_material_id, str) or not render_hash)):
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "A source revision reference failed validation", 400, detail)
    if render_material_id is not None:
        material = store.row(
            "SELECT sha256 FROM materials WHERE id = ? AND workspace_id = ?",
            (render_material_id, workspace_id),
        )
        if material is None or material["sha256"] != render_hash:
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "A historical render Material is unavailable or changed", 409,
                           detail)
    return copy.deepcopy(value)


def _remap_media_storyboard_import(store, identity: dict, work_id: str | None, storyboard: object,
                                   timeline: Timeline, material_id_map: dict[str, str | None],
                                   source_map: dict[str, dict]) -> dict:
    value = _validate_media_storyboard_shape(storyboard, timeline.to_dict())
    workspace_id = identity["workspace"]["id"]
    project_stub = {"id": str(uuid.uuid4()), "workspace_id": workspace_id, "work_id": work_id}
    clip_sources = {clip.clip_id: timeline._asset(clip.media_asset_id)
                    for track in timeline.tracks for clip in track.clips}
    known_ref_ids = {ref["ref_id"] for node in value["nodes"] for ref in node["source_refs"]}
    if set(source_map) - known_ref_ids:
        raise ApiError("MEDIA_STORYBOARD_IMPORT_SOURCE_MAP_UNKNOWN", "The storyboard source map includes an unknown reference ID", 400,
                       {"source_ref_ids": sorted(set(source_map) - known_ref_ids)})
    with store.transaction(immediate=False) as connection:
        for node in value["nodes"]:
            if node["recording_refs"]:
                raise ApiError("MEDIA_STORYBOARD_IMPORT_TAKE_RELINK_REQUIRED",
                               "Recreate or explicitly relink completed recording sessions after importing the project", 409,
                               {"beat_id": node["beat_id"]})
            for index, ref in enumerate(node["source_refs"]):
                replacement = source_map.get(ref["ref_id"])
                if replacement is not None:
                    replacement = dict(replacement)
                    replacement.setdefault("role", ref["role"])
                    node["source_refs"][index] = _storyboard_source_ref(
                        connection, identity, project_stub, replacement, ref_id=ref["ref_id"],
                    )
                    continue
                if ref["kind"] == "material":
                    candidate_id = material_id_map.get(ref["material_id"], ref["material_id"])
                    material = connection.execute(
                        "SELECT id,sha256 FROM materials WHERE id=? AND workspace_id=?",
                        (candidate_id, workspace_id),
                    ).fetchone()
                    if material is None or material["sha256"] != ref["sha256"]:
                        raise ApiError("MEDIA_STORYBOARD_IMPORT_SOURCE_REMAP_REQUIRED",
                                       "Map each imported storyboard source to a Material with the same reviewed SHA-256", 409,
                                       {"beat_id": node["beat_id"], "source_ref_id": ref["ref_id"],
                                        "source_sha256": ref["sha256"]})
                    normalized = _storyboard_source_ref(
                        connection, identity, project_stub,
                        {"kind": "material", "material_id": candidate_id, "role": ref["role"],
                         "sha256": ref["sha256"]}, ref_id=ref["ref_id"],
                    )
                    node["source_refs"][index] = normalized
                else:
                    # Artifact identity and version are workspace-scoped. The normalizer
                    # checks both the saved hash and current permission before preserving it.
                    node["source_refs"][index] = _storyboard_source_ref(
                        connection, identity, project_stub,
                        {"kind": "artifact", "artifact_id": ref["artifact_id"],
                         "artifact_version": ref["artifact_version"], "role": ref["role"],
                         "sha256": ref["sha256"]}, ref_id=ref["ref_id"],
                    )
        for node in value["nodes"]:
            for ref in node["clip_refs"]:
                asset = clip_sources.get(ref["clip_id"])
                if asset is None:
                    raise ApiError("MEDIA_STORYBOARD_IMPORT_CLIP_MISSING", "A storyboard clip no longer exists after source relinking", 409,
                                   {"beat_id": node["beat_id"], "clip_id": ref["clip_id"]})
                # The project importer already requires an explicit Material source
                # selection when a clip's original hash cannot be rebound.
                ref["source_sha256"] = asset.sha256
        normalized = _require_media_storyboard_sources(
            connection, identity, project_stub, value,
            target_work_id=work_id, timeline_data=timeline.to_dict(),
        )
    return normalized


def _import_media_project(store, identity: dict, data: dict, *, persist: bool = True):
    if set(data) - {"manifest", "title", "work_id", "source_material_map", "source_slot_map",
                    "storyboard_source_map"} or "manifest" not in data:
        raise ApiError("INVALID_INPUT", "Media project import fields are invalid", 400)
    manifest = data["manifest"]
    if not isinstance(manifest, dict):
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The project manifest must be a JSON object", 400)
    if len(_json(manifest).encode("utf-8")) > MEDIA_PROJECT_MANIFEST_MAX_BYTES:
        raise ApiError("MEDIA_PROJECT_MANIFEST_TOO_LARGE", "The editable project manifest exceeds the import limit", 413)
    required_manifest_fields = {
        "schema", "project", "timeline", "sources", "source_package_state", "rights_state", "publication_state",
    }
    manifest_fields = set(manifest)
    allowed_manifest_fields = {
        frozenset(required_manifest_fields),
        frozenset(required_manifest_fields | {"source_slots"}),
        frozenset(required_manifest_fields | {"storyboard"}),
        frozenset(required_manifest_fields | {"source_slots", "storyboard"}),
    }
    if (frozenset(manifest_fields) not in allowed_manifest_fields
            or manifest.get("schema") != MEDIA_PROJECT_EXPORT_SCHEMA):
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The project manifest schema is not supported", 400)
    if manifest.get("source_package_state") != "material_references_only":
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "This manifest does not declare its Material source references", 400)
    project_data = manifest.get("project")
    timeline_data = manifest.get("timeline")
    source_entries = manifest.get("sources")
    if (type(project_data) is not dict or type(timeline_data) is not dict
            or type(source_entries) is not list):
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The project manifest is incomplete", 400)
    kind = project_data.get("kind")
    if kind not in {"video", "podcast"}:
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The project kind is not supported", 400)
    title = _string(data.get("title", project_data.get("title")), "title", maximum=200)
    source_origin = (_validate_source_origin(store, identity["workspace"]["id"], project_data["source_origin"])
                     if "source_origin" in project_data and project_data["source_origin"] is not None else None)
    work_id = data.get("work_id")
    if work_id is not None and not isinstance(work_id, str):
        raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
    workspace_id = identity["workspace"]["id"]
    _require_media_target_work_access(store, identity, work_id, write=True)
    raw_source_map = data.get("source_material_map", {})
    if (not isinstance(raw_source_map, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in raw_source_map.items())):
        raise ApiError("INVALID_INPUT", "source_material_map must map asset IDs to Material IDs", 400)
    raw_source_slot_map = data.get("source_slot_map", {})
    if (not isinstance(raw_source_slot_map, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in raw_source_slot_map.items())):
        raise ApiError("INVALID_INPUT", "source_slot_map must map source slot IDs to Material IDs", 400)
    storyboard_source_map = data.get("storyboard_source_map", {})
    if (not isinstance(storyboard_source_map, dict)
            or any(not isinstance(key, str) or type(value) is not dict
                   for key, value in storyboard_source_map.items())):
        raise ApiError("INVALID_INPUT", "storyboard_source_map must map stable storyboard ref IDs to source references", 400)
    manifest_storyboard = manifest.get("storyboard", _empty_media_storyboard())

    if persist:
        key = _required_idempotency_key()
        fingerprint = _fingerprint({
            "manifest": manifest, "title": title, "work_id": work_id,
            "source_material_map": raw_source_map, "source_slot_map": raw_source_slot_map,
            "storyboard_source_map": storyboard_source_map,
        })
        prior = store.row(
            "SELECT request_hash, resource_id FROM idempotency_records "
            "WHERE workspace_id = ? AND operation = 'import_media_project' AND key = ?",
            (workspace_id, key),
        )
        if prior is not None:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different Media project import", 409)
            return _success(_project_view(store, workspace_id, prior["resource_id"]), 200)

    timeline = copy.deepcopy(timeline_data)
    asset_documents = timeline.get("assets")
    if not isinstance(asset_documents, list):
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The project timeline has no source manifest", 400)
    manifest_by_asset = {}
    for entry in source_entries:
        if (type(entry) is not dict or not isinstance(entry.get("asset_id"), str)
                or entry["asset_id"] in manifest_by_asset):
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source manifest contains an invalid or duplicate asset", 400)
        manifest_by_asset[entry["asset_id"]] = entry
    asset_ids = {item.get("asset_id") for item in asset_documents if isinstance(item, dict)}
    if set(manifest_by_asset) != asset_ids or not set(raw_source_map) <= asset_ids:
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source manifest does not match its editable timeline", 400)

    has_source_slots = "source_slots" in manifest
    source_slot_entries = manifest.get("source_slots", [])
    if type(source_slot_entries) is not list:
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source slot manifest is invalid", 400)
    source_slots_by_id = {}
    for slot in source_slot_entries:
        if (type(slot) is not dict
                or set(slot) != {"slot_id", "asset_id", "label", "media_kind", "required_streams",
                                 "source_inventory", "minimum_source_duration_sec", "source_sha256", "rights_state"}
                or not isinstance(slot.get("slot_id"), str) or not isinstance(slot.get("asset_id"), str)
                or slot["slot_id"] != slot["asset_id"] or slot["asset_id"] in source_slots_by_id):
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source slot manifest contains an invalid or duplicate slot", 400)
        required_streams = slot.get("required_streams")
        inventory = slot.get("source_inventory")
        if (type(required_streams) is not dict or set(required_streams) != {"video", "audio"}
                or type(inventory) is not dict or set(inventory) != {"video_streams", "audio_streams"}
                or any(type(value) is not int or value < 0 or value > 256
                       for value in (*required_streams.values(), *inventory.values()))
                or required_streams["video"] > inventory["video_streams"]
                or required_streams["audio"] > inventory["audio_streams"]
                or not isinstance(slot.get("media_kind"), str)
                or slot["media_kind"] not in {"video", "video_audio", "audio", "image"}
                or not isinstance(slot.get("label"), str) or not slot["label"] or len(slot["label"]) > 255
                or slot.get("rights_state") != "unknown"
                or slot.get("source_sha256") != manifest_by_asset.get(slot["asset_id"], {}).get("sha256")):
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source slot requirements are invalid", 400)
        expected_kind = ("image" if slot["media_kind"] == "image" and inventory["video_streams"] == 1
                         and inventory["audio_streams"] == 0 else
                         "video_audio" if inventory["video_streams"] and inventory["audio_streams"]
                         else "video" if inventory["video_streams"] else "audio")
        if slot["media_kind"] != expected_kind:
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source slot media kind differs from its stream inventory", 400)
        try:
            minimum_duration = _fraction_time(slot.get("minimum_source_duration_sec"))
        except VideoEditingError as exc:
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "A source slot has an invalid minimum duration", 400) from exc
        if minimum_duration < 0:
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "A source slot has an invalid minimum duration", 400)
        if slot["media_kind"] == "image" and minimum_duration != 0:
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "A still image slot cannot declare an intrinsic source duration", 400)
        source_slots_by_id[slot["slot_id"]] = slot
    if has_source_slots and set(source_slots_by_id) != asset_ids:
        raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source slot manifest does not match its project assets", 400)
    if (not set(raw_source_slot_map) <= set(source_slots_by_id)
            or set(raw_source_slot_map) & set(raw_source_map)):
        raise ApiError("INVALID_INPUT", "The selected source slot map is invalid or conflicts with an exact source map", 400)

    imported_assets = []
    asset_id_map = {}
    material_id_map = {}
    missing_sources = []
    ambiguous_sources = []
    for stored_asset in asset_documents:
        if type(stored_asset) is not dict:
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "The source timeline is invalid", 400)
        old_asset_id = stored_asset.get("asset_id")
        old_material_id = stored_asset.get("source_material_id")
        expected_hash = stored_asset.get("sha256")
        source_entry = manifest_by_asset[old_asset_id]
        if (not isinstance(expected_hash, str) or source_entry.get("sha256") != expected_hash
                or source_entry.get("material_id") != stored_asset.get("source_material_id")):
            raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "Source identity differs between the project and its manifest", 400)
        slot_replacement = old_asset_id in raw_source_slot_map
        selected_material_id = raw_source_map.get(
            old_asset_id,
            raw_source_slot_map.get(old_asset_id, stored_asset.get("source_material_id")),
        )
        material = store.row(
            "SELECT id, filename, sha256, relative_path, size_bytes FROM materials "
            "WHERE id = ? AND workspace_id = ?",
            (selected_material_id, workspace_id),
        )
        if slot_replacement and material is None:
            raise ApiError("MEDIA_TEMPLATE_SLOT_MATERIAL_MISSING", "The selected Material for this source slot is unavailable", 409,
                           {"slot_id": old_asset_id})
        if not slot_replacement and (material is None or material["sha256"] != expected_hash):
            if old_asset_id in raw_source_map:
                raise ApiError("MEDIA_IMPORT_SOURCE_MISMATCH", "The selected Material does not match the project source hash", 409,
                               {"asset_id": old_asset_id, "expected_sha256": expected_hash})
            matches = store.rows(
                "SELECT id FROM materials WHERE workspace_id = ? AND sha256 = ? ORDER BY id",
                (workspace_id, expected_hash),
            )
            accessible_matches = []
            for candidate in matches:
                try:
                    _require_media_material_access(
                        store, identity, candidate["id"], target_work_id=work_id,
                    )
                except ApiError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                accessible_matches.append(candidate)
                if len(accessible_matches) == 2:
                    break
            if len(accessible_matches) == 1:
                selected_material_id = accessible_matches[0]["id"]
                material = store.row(
                    "SELECT id, filename, sha256, relative_path, size_bytes FROM materials "
                    "WHERE id = ? AND workspace_id = ?",
                    (selected_material_id, workspace_id),
                )
            elif len(accessible_matches) > 1:
                ambiguous_sources.append({"asset_id": old_asset_id, "sha256": expected_hash})
                continue
            else:
                missing_sources.append({
                    "asset_id": old_asset_id, "filename": stored_asset.get("filename"),
                    "sha256": expected_hash, "size_bytes": source_entry.get("size_bytes"),
                })
                continue
        _require_media_material_access(
            store, identity, selected_material_id, target_work_id=work_id,
        )
        material_record, path = _store_material_source(store, workspace_id, selected_material_id, verify=True)
        if not slot_replacement and material_record["sha256"] != expected_hash:
            raise ApiError("MEDIA_IMPORT_SOURCE_MISMATCH", "The selected Material failed source hash verification", 409,
                           {"asset_id": old_asset_id, "expected_sha256": expected_hash})
        try:
            asset = fingerprint_local_media(
                str(path), source_material_id=selected_material_id,
                display_filename=(material_record["filename"] if slot_replacement else stored_asset["filename"]),
            )
        except VideoEditingError as exc:
            raise ApiError(exc.args[0] if exc.args else "MEDIA_SOURCE_INVALID",
                           "A project source Material is not readable media", 415) from exc
        if not slot_replacement and asset.sha256 != expected_hash:
            raise ApiError("MEDIA_IMPORT_SOURCE_MISMATCH", "The selected Material changed during project import", 409,
                           {"asset_id": old_asset_id, "expected_sha256": expected_hash})
        if slot_replacement:
            slot = source_slots_by_id[old_asset_id]
            inventory = slot["source_inventory"]
            requirements = slot["required_streams"]
            minimum_duration = _fraction_time(slot["minimum_source_duration_sec"])
            if (asset.media_kind != slot["media_kind"]
                    or asset.video_stream_count != inventory["video_streams"]
                    or asset.audio_stream_count != inventory["audio_streams"]
                    or asset.video_stream_count < requirements["video"]
                    or asset.audio_stream_count < requirements["audio"]):
                raise ApiError("MEDIA_TEMPLATE_SLOT_STREAMS_INCOMPATIBLE",
                               "The selected Material does not match this source slot's media and stream inventory", 409,
                               {"slot_id": old_asset_id, "required_streams": requirements,
                                "actual_streams": {"video": asset.video_stream_count,
                                                   "audio": asset.audio_stream_count}})
            if slot["media_kind"] == "image":
                if not asset.is_still_image:
                    raise ApiError("MEDIA_TEMPLATE_SLOT_DURATION_INSUFFICIENT",
                                   "The selected Material is not a static image source", 409,
                                   {"slot_id": old_asset_id})
            elif asset.duration_sec is None or asset.duration_sec < minimum_duration:
                raise ApiError("MEDIA_TEMPLATE_SLOT_DURATION_INSUFFICIENT",
                               "The selected Material is shorter than the source range used by this template", 409,
                               {"slot_id": old_asset_id, "minimum_source_duration_sec": _time_json(minimum_duration),
                                "actual_source_duration_sec": _time_json(asset.duration_sec)})
        asset_id_map[old_asset_id] = asset.asset_id
        if old_material_id:
            if old_material_id not in material_id_map:
                material_id_map[old_material_id] = selected_material_id
            elif material_id_map[old_material_id] != selected_material_id:
                material_id_map[old_material_id] = None
        imported_assets.append(asset)
        stored_asset.clear()
        stored_asset.update(asset.to_dict())
    if ambiguous_sources:
        raise ApiError("MEDIA_IMPORT_SOURCE_AMBIGUOUS", "Select a Material for each source hash used more than once", 409,
                       {"sources": ambiguous_sources})
    if missing_sources:
        raise ApiError("MEDIA_IMPORT_SOURCE_MISSING", "Upload or select the missing source Materials before importing", 409,
                       {"sources": missing_sources})
    for track in timeline.get("tracks", []):
        for clip in track.get("clips", []):
            old_asset_id = clip.get("media_asset_id")
            if old_asset_id not in asset_id_map:
                raise ApiError("MEDIA_PROJECT_IMPORT_INVALID", "A clip references a missing source asset", 400)
            clip["media_asset_id"] = asset_id_map[old_asset_id]
    timeline["version"] = 1
    try:
        timeline_value = Timeline.from_dict(timeline, assets=imported_assets, verify_local_assets=False)
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "MEDIA_PROJECT_IMPORT_INVALID",
                       "The imported editable project failed source and timeline validation", 400) from exc
    state = _project_state(title=title, kind=kind, timeline=timeline_value)
    state["storyboard"] = _remap_media_storyboard_import(
        store, identity, work_id, manifest_storyboard, timeline_value,
        material_id_map, storyboard_source_map,
    )
    if source_origin is not None:
        state["source_origin"] = source_origin
    storyboard_material_ids = {
        ref["material_id"] for node in state["storyboard"]["nodes"] for ref in node["source_refs"]
        if ref["kind"] == "material"
    }
    storyboard_artifact_ids = {
        ref["artifact_id"] for node in state["storyboard"]["nodes"] for ref in node["source_refs"]
        if ref["kind"] == "artifact"
    }
    required_material_ids = {asset.source_material_id for asset in timeline_value.assets} | storyboard_material_ids
    if not persist:
        return {
            "state": state,
            "kind": kind,
            "title": title,
            "material_ids": sorted(required_material_ids),
            "artifact_ids": sorted(storyboard_artifact_ids),
        }
    project_id = str(uuid.uuid4())
    now = utc_now()
    created = False
    with store.transaction() as connection:
        prior = connection.execute(
            "SELECT request_hash, resource_id FROM idempotency_records "
            "WHERE workspace_id = ? AND operation = 'import_media_project' AND key = ?",
            (workspace_id, key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different Media project import", 409)
            project_id = prior["resource_id"]
        else:
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            for material_id in required_material_ids:
                _require_media_material_access_in_transaction(
                    connection, identity, material_id, target_work_id=work_id,
                )
            project_stub = {"id": project_id, "workspace_id": workspace_id, "work_id": work_id}
            _require_media_storyboard_sources(
                connection, identity, project_stub, state["storyboard"],
                target_work_id=work_id, timeline_data=timeline_value.to_dict(),
            )
            connection.execute(
                "INSERT INTO media_projects(id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (project_id, workspace_id, identity["user"]["id"], work_id, title, kind, now, now, identity["user"]["id"]),
            )
            connection.execute(
                "INSERT INTO media_project_versions(project_id, workspace_id, version, parent_version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) "
                "VALUES(?, ?, 1, NULL, 'import', ?, '[]', '[]', ?, ?)",
                (project_id, workspace_id, _json(state), now, identity["user"]["id"]),
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
                "VALUES(?, 'import_media_project', ?, ?, 'media_project', ?, ?)",
                (workspace_id, key, fingerprint, project_id, now),
            )
            if work_id:
                connection.executemany(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(work_id, material_id, workspace_id, now) for material_id in sorted(required_material_ids)],
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(work_id, artifact_id, workspace_id, now) for artifact_id in sorted(storyboard_artifact_ids)],
                )
            created = True
    return _success(_project_view(store, workspace_id, project_id), 201 if created else 200)


def _import_media_project_history(store, identity: dict, data: dict):
    """Import one version DAG without flattening sibling edits into a false chain."""
    allowed = {"title", "kind", "work_id", "versions", "current_source_version_id"}
    if set(data) - allowed or not {"title", "kind", "versions", "current_source_version_id"} <= set(data):
        raise ApiError("INVALID_INPUT", "Media project history fields are invalid", 400)
    title = _string(data["title"], "title", maximum=200)
    kind = data["kind"]
    if kind not in {"video", "podcast"}:
        raise ApiError("INVALID_INPUT", "kind must be video or podcast", 400)
    work_id = data.get("work_id")
    if work_id is not None and not isinstance(work_id, str):
        raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
    versions = data["versions"]
    if not isinstance(versions, list) or not 1 <= len(versions) <= 100:
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "A project history needs between 1 and 100 versions", 400)
    current_source_version_id = data["current_source_version_id"]
    if (not isinstance(current_source_version_id, str) or not current_source_version_id
            or len(current_source_version_id) > 255):
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "The selected current source version is invalid", 400)

    workspace_id = identity["workspace"]["id"]
    _require_media_target_work_access(store, identity, work_id, write=True)
    key = _required_idempotency_key()
    fingerprint = _fingerprint(data)
    prior = store.row(
        "SELECT request_hash, resource_id FROM idempotency_records "
        "WHERE workspace_id = ? AND operation = 'import_media_project_history' AND key = ?",
        (workspace_id, key),
    )
    if prior is not None:
        if prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different Media history", 409)
        return _success(_project_view(store, workspace_id, prior["resource_id"]), 200)

    prepared_versions = []
    source_versions_by_id = {}
    parent_source_version_by_id = {}
    all_material_ids = set()
    all_artifact_ids = set()
    history_source_project_id = None
    history_has_source_project_id = None
    for index, item in enumerate(versions):
        allowed_version = {
            "source_version_id", "parent_source_version_id", "manifest",
            "source_material_map", "source_slot_map", "storyboard_source_map", "source_origin",
        }
        if (type(item) is not dict or set(item) - allowed_version
                or not {"source_version_id", "parent_source_version_id", "manifest", "source_origin"} <= set(item)):
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "A historical project version is malformed", 400,
                           {"version_index": index})
        source_version_id = item["source_version_id"]
        parent_source_version_id = item["parent_source_version_id"]
        if (not isinstance(source_version_id, str) or not source_version_id or len(source_version_id) > 255
                or source_version_id in source_versions_by_id
                or (parent_source_version_id is not None
                    and (not isinstance(parent_source_version_id, str)
                         or parent_source_version_id not in source_versions_by_id))
                or (index == 0 and parent_source_version_id is not None)
                or (index > 0 and parent_source_version_id is None)):
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "Source versions must be unique and ordered after their parent", 400,
                           {"version_index": index})
        source_origin = _validate_source_origin(
            store, workspace_id, item.get("source_origin"), version_index=index,
        )
        source_version = source_origin.get("source_version")
        source_project_id = source_origin.get("source_project_id")
        has_source_project_id = source_project_id is not None
        if history_has_source_project_id is None:
            history_has_source_project_id = has_source_project_id
            history_source_project_id = source_project_id
        elif (history_has_source_project_id != has_source_project_id
              or has_source_project_id and history_source_project_id != source_project_id):
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "All revisions must reference the same source project lineage", 400)
        render_material_id = source_origin.get("render_material_id")
        if render_material_id is not None:
            _require_media_material_access(
                store, identity, render_material_id, target_work_id=work_id,
            )
            all_material_ids.add(render_material_id)

        prepared = _import_media_project(
            store,
            identity,
            {
                "manifest": item["manifest"],
                "title": title,
                "work_id": work_id,
                "source_material_map": item.get("source_material_map", {}),
                "source_slot_map": item.get("source_slot_map", {}),
                "storyboard_source_map": item.get("storyboard_source_map", {}),
            },
            persist=False,
        )
        manifest_project = item["manifest"].get("project") if isinstance(item["manifest"], dict) else None
        if (prepared["kind"] != kind or type(manifest_project) is not dict
                or manifest_project.get("kind") != kind):
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "Versions in one project history must have the same kind", 400,
                           {"version_index": index})
        if source_version != manifest_project.get("version"):
            raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "The source version does not match its editable manifest", 400,
                           {"version_index": index})
        prepared["state"]["source_origin"] = source_origin
        prepared_versions.append({
            "source_version_id": source_version_id,
            "parent_source_version_id": parent_source_version_id,
            "state": prepared["state"],
            "material_ids": prepared["material_ids"],
            "artifact_ids": prepared["artifact_ids"],
        })
        source_versions_by_id[source_version_id] = prepared
        parent_source_version_by_id[source_version_id] = parent_source_version_id
        all_material_ids.update(material_id for material_id in prepared["material_ids"] if material_id)
        all_artifact_ids.update(prepared.get("artifact_ids", []))

    if current_source_version_id not in source_versions_by_id:
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "The selected current version is missing", 400)
    if any(parent_id == current_source_version_id for parent_id in parent_source_version_by_id.values()):
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "The selected current version must be a branch head", 400)
    current_index = next(index for index, item in enumerate(prepared_versions)
                         if item["source_version_id"] == current_source_version_id)
    if current_index != len(prepared_versions) - 1:
        prepared_versions.append(prepared_versions.pop(current_index))
    source_id_to_version = {
        item["source_version_id"]: index
        for index, item in enumerate(prepared_versions, start=1)
    }
    def undo_lineage(source_version_id: str) -> list[int]:
        ancestry = []
        parent_id = parent_source_version_by_id[source_version_id]
        while parent_id is not None:
            ancestry.append(source_id_to_version[parent_id])
            parent_id = parent_source_version_by_id[parent_id]
        ancestry.reverse()
        return ancestry

    project_id = str(uuid.uuid4())
    now = utc_now()
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT request_hash, resource_id FROM idempotency_records "
            "WHERE workspace_id = ? AND operation = 'import_media_project_history' AND key = ?",
            (workspace_id, key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different Media history", 409)
            project_id = existing["resource_id"]
        else:
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            for material_id in all_material_ids:
                _require_media_material_access_in_transaction(
                    connection, identity, material_id, target_work_id=work_id,
                )
            connection.execute(
                "INSERT INTO media_projects(id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, workspace_id, identity["user"]["id"], work_id, title, kind,
                 source_id_to_version[current_source_version_id], now, now, identity["user"]["id"]),
            )
            connection.executemany(
                "INSERT INTO media_project_versions(project_id, workspace_id, version, parent_version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)",
                [
                    (
                        project_id, workspace_id, version,
                        (source_id_to_version[item["parent_source_version_id"]]
                         if item["parent_source_version_id"] is not None else None),
                        "import" if item["parent_source_version_id"] is None else "import_revision",
                        _json(item["state"]), _json(undo_lineage(item["source_version_id"])),
                        now, identity["user"]["id"],
                    )
                    for version, item in enumerate(prepared_versions, start=1)
                ],
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
                "VALUES(?, 'import_media_project_history', ?, ?, 'media_project', ?, ?)",
                (workspace_id, key, fingerprint, project_id, now),
            )
            if work_id:
                connection.executemany(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(work_id, material_id, workspace_id, now) for material_id in sorted(all_material_ids)],
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(work_id, artifact_id, workspace_id, now) for artifact_id in sorted(all_artifact_ids)],
                )
    return _success(_project_view(store, workspace_id, project_id), 201)


def _reopen_project_version(store, workspace_id: str, project_id: str, version: int, *, verify_files: bool = False):
    row = _version_row(store, workspace_id, project_id, version)
    state = json.loads(row["state_json"])
    timeline, availability = _load_timeline(
        store, workspace_id, state, verify_files=verify_files, project_id=project_id,
    )
    return row, state, timeline, availability


def _require_active_work(store, workspace_id: str, work_id: str | None) -> None:
    if not work_id:
        return
    work = store.row("SELECT id, status FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id))
    if work is None:
        raise _not_found()
    if work["status"] != "active":
        raise ApiError("WORK_ARCHIVED", "Reopen this work before editing its Media project", 409)


def _append_project_version(
    store,
    identity: dict,
    project_id: str,
    *,
    base_version: int,
    state: dict,
    operation: str,
    idempotency_key: str,
    request_hash: str,
    undo_stack: list[int],
    redo_stack: list[int],
    after_version: Callable[[sqlite3.Connection, int], None] | None = None,
) -> tuple[int, bool]:
    workspace_id = identity["workspace"]["id"]
    now = utc_now()
    result_version = None
    created = False
    with store.transaction() as connection:
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        ).fetchone()
        if project is None:
            raise _not_found()
        existing = connection.execute(
            "SELECT * FROM idempotency_records WHERE workspace_id = ? AND operation = 'media_project_version' AND key = ?",
            (workspace_id, idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different Media edit", 409)
            resource_project, separator, raw_version = existing["resource_id"].rpartition(":")
            if not separator or resource_project != project_id:
                raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media edit is unavailable", 409)
            result_version = int(raw_version)
        else:
            if project["current_version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                               {"current_version": project["current_version"]})
            if project["work_id"]:
                require_work_access(connection, identity, project["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            result_version = base_version + 1
            timeline_data = state.get("timeline")
            if not isinstance(timeline_data, dict):
                raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The Media project state is invalid", 400)
            storyboard = _require_media_storyboard_sources(
                connection, identity, project, state.get("storyboard"),
                target_work_id=project["work_id"], timeline_data=timeline_data,
            )
            source_ids = [
                item["source_material_id"] for item in timeline_data.get("assets", [])
                if isinstance(item, dict) and isinstance(item.get("source_material_id"), str)
            ]
            source_ids.extend(
                ref["material_id"] for node in storyboard["nodes"] for ref in node["source_refs"]
                if ref["kind"] == "material"
            )
            for node in storyboard["nodes"]:
                for ref in node["recording_refs"]:
                    source_ids.extend(row["material_id"] for row in connection.execute(
                        "SELECT material_id FROM media_recording_segments WHERE session_id=? AND workspace_id=?",
                        (ref["session_id"], workspace_id),
                    ).fetchall())
            for material_id in set(source_ids):
                _require_media_material_access_in_transaction(
                    connection, identity, material_id, target_work_id=project["work_id"],
                )
            timeline_data["version"] = result_version
            connection.execute(
                "INSERT INTO media_project_versions(project_id, workspace_id, version, parent_version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, workspace_id, result_version, base_version, operation, _json(state),
                 _json(undo_stack), _json(redo_stack), now, identity["user"]["id"]),
            )
            updated = connection.execute(
                "UPDATE media_projects SET title = ?, current_version = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND current_version = ?",
                (state["title"], result_version, now, project_id, workspace_id, base_version),
            )
            if updated.rowcount != 1:
                raise ApiError("VERSION_CONFLICT", "Media project changed during save", 409)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
                "VALUES(?, 'media_project_version', ?, ?, 'media_project_version', ?, ?)",
                (workspace_id, idempotency_key, request_hash, f"{project_id}:{result_version}", now),
            )
            if project["work_id"]:
                connection.executemany(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(project["work_id"], material_id, workspace_id, now) for material_id in source_ids],
                )
                artifact_ids = {
                    ref["artifact_id"] for node in storyboard["nodes"] for ref in node["source_refs"]
                    if ref["kind"] == "artifact"
                }
                connection.executemany(
                    "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    [(project["work_id"], artifact_id, workspace_id, now) for artifact_id in sorted(artifact_ids)],
                )
            created = True
        if after_version is not None:
            after_version(connection, result_version)
    return result_version, created


def _store_material_source(store, workspace_id: str, material_id: str, *, verify: bool) -> tuple[dict, Path]:
    row = store.row(
        "SELECT id, owner_user_id, work_id, title, filename, status, sha256, relative_path, size_bytes FROM materials "
        "WHERE id = ? AND workspace_id = ?",
        (material_id, workspace_id),
    )
    if row is None:
        raise _not_found()
    path = _resource_path(store, row["relative_path"])
    if not path.is_file() or path.is_symlink():
        raise ApiError("FILE_UNAVAILABLE", "The selected Material original is unavailable", 409)
    if verify and sha256_media_file(path) != row["sha256"]:
        raise ApiError("FILE_INTEGRITY_ERROR", "The selected Material original failed integrity verification", 409)
    return dict(row), path


def _new_media_asset(path: Path, material: dict) -> MediaAsset:
    try:
        asset = fingerprint_local_media(
            str(path), source_material_id=material["id"], display_filename=material["filename"]
        )
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "MEDIA_SOURCE_UNAVAILABLE", "The selected file is not a readable media source", 415) from exc
    if asset.sha256 != material["sha256"]:
        raise ApiError("FILE_INTEGRITY_ERROR", "The selected Material original changed during media inspection", 409)
    return asset


def _initial_timeline(kind: str) -> Timeline:
    return Timeline(
        assets=(),
        tracks=_default_tracks(kind),
        version=1,
    )


def _version_stack(row, name: str) -> list[int]:
    try:
        value = json.loads(row[name])
    except (TypeError, ValueError) as exc:
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "Media edit history could not be read", 409) from exc
    if not isinstance(value, list) or any(type(item) is not int or item < 1 for item in value):
        raise ApiError("MEDIA_PROJECT_HISTORY_INVALID", "Media edit history could not be read", 409)
    return value


def _copy_project_state(state: dict, timeline: Timeline, *, title: str | None = None) -> dict:
    copied = {
        "schema": MEDIA_PROJECT_SCHEMA,
        "title": state["title"] if title is None else title,
        "kind": state["kind"],
        "clock": state.get("clock", {"unit": "rational_seconds", "source_clocks_preserved": True}),
        "timeline": timeline.to_dict(),
        "storyboard": copy.deepcopy(state.get("storyboard", _empty_media_storyboard())),
    }
    if "source_origin" in state:
        copied["source_origin"] = copy.deepcopy(state["source_origin"])
    return copied


def _read_version_state(
    store,
    workspace_id: str,
    project_id: str,
    version: int,
    *,
    identity: dict | None = None,
    write: bool | None = None,
) -> tuple[dict, object]:
    row = _version_row(store, workspace_id, project_id, version, identity=identity, write=write)
    try:
        state = json.loads(row["state_json"])
    except (TypeError, ValueError) as exc:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version could not be read", 409) from exc
    return state, row


def _media_job_data(row) -> dict:
    error = None if row["error_code"] is None else {
        "code": row["error_code"],
        "message": row["error_message"] or "Media render failed",
    }
    return {
        "id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "progress": {
            "completed": row["progress_completed"],
            "total": row["progress_total"],
            "label": row["progress_label"],
        },
        "error": error,
    }


def _resolve_idempotent_version(store, workspace_id: str, project_id: str, key: str, fingerprint: str):
    row = store.row(
        "SELECT * FROM idempotency_records WHERE workspace_id = ? AND operation = 'media_project_version' AND key = ?",
        (workspace_id, key),
    )
    if row is None:
        return None
    if row["request_hash"] != fingerprint:
        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different Media edit", 409)
    resource_project, separator, raw_version = row["resource_id"].rpartition(":")
    if not separator or resource_project != project_id:
        raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media edit is unavailable", 409)
    return int(raw_version)


def _recording_session_row(store, workspace_id: str, session_id: str):
    row = store.row(
        "SELECT * FROM media_recording_sessions WHERE id = ? AND workspace_id = ?",
        (session_id, workspace_id),
    )
    if row is None:
        raise _not_found()
    return row


def _recording_session_view(store, session_row, *, identity: dict | None = None) -> dict:
    segments = store.rows(
        "SELECT * FROM media_recording_segments WHERE session_id = ? AND workspace_id = ? ORDER BY sequence",
        (session_row["id"], session_row["workspace_id"]),
    )
    identity = identity or _request_identity_for_workspace(session_row["workspace_id"])
    if identity is not None:
        project = _project_row(
            store, session_row["workspace_id"], session_row["project_id"],
            identity=identity, write=False,
        )
        with store.transaction(immediate=False) as connection:
            for segment in segments:
                _require_media_material_access_in_transaction(
                    connection, identity, segment["material_id"],
                    target_work_id=project["work_id"],
                )
    return {
        "id": session_row["id"],
        "project_id": session_row["project_id"],
        "take_id": session_row["take_id"],
        "track_id": session_row["track_id"],
        "track_kind": session_row["track_kind"],
        "cue_id": session_row["cue_id"],
        "speaker_label": session_row["speaker_label"],
        "base_project_version": session_row["base_project_version"],
        "project_version": session_row["project_version"],
        "status": session_row["status"],
        "sample_rate": session_row["sample_rate"],
        "channels": session_row["channels"],
        "sample_format": session_row["sample_format"],
        "interrupt_reason": session_row["interrupt_reason"],
        "timeline_start_sec": {"num": session_row["timeline_start_num"],
                                "den": session_row["timeline_start_den"]},
        "capture_epoch_id": session_row["capture_epoch_id"],
        "latest_sequence": session_row["latest_sequence"],
        "started_at": session_row["started_at"],
        "stopped_at": session_row["stopped_at"],
        "completed_at": session_row["completed_at"],
        "segments": [
            {
                "sequence": item["sequence"],
                "material_id": item["material_id"],
                "sha256": item["source_sha256"],
                "audio_stream_index": item["audio_stream_index"],
                "codec_name": item["codec_name"],
                "sample_rate": item["sample_rate"],
                "channels": item["channels"],
                "sample_count": item["sample_count"],
                "duration_sec": _time_json(Fraction(item["sample_count"], item["sample_rate"])),
                "capture_epoch_id": item["capture_epoch_id"],
                "capture_sample_start": item["capture_sample_start"],
                "timeline_start_sec": {"num": item["timeline_start_num"],
                                        "den": item["timeline_start_den"]},
                "gap_samples_before": item["gap_samples_before"],
                "clock_relation": item["clock_relation"],
                "source_pts": item["source_start_pts"],
                "source_time_base": (None if item["source_time_base_num"] is None else {
                    "num": item["source_time_base_num"], "den": item["source_time_base_den"],
                }),
                "playback_url": f"/api/media/recordings/{session_row['id']}/segments/{item['sequence']}/file",
            }
            for item in segments
        ],
        "recovery": {"segments_preserved": len(segments), "last_confirmed_sequence": session_row["latest_sequence"],
                     "uncaptured_tail": "unknown" if session_row["status"] in {
                         "awaiting_first_segment", "recording", "interrupted",
                     } else "none"},
    }


def _recording_segment_asset(
    store,
    workspace_id: str,
    material_id: str,
    *,
    stream_index: int,
    expected_sample_rate: int,
    expected_channels: int,
    expected_sample_format: str,
    identity: dict | None = None,
    target_work_id: str | None | object = _TARGET_WORK_UNSET,
):
    if identity is not None and target_work_id is not _TARGET_WORK_UNSET:
        _require_media_material_access(
            store, identity, material_id, target_work_id=target_work_id,
        )
    material, path = _store_material_source(store, workspace_id, material_id, verify=True)
    asset = _new_media_asset(path, material)
    if asset.has_video or asset.audio_stream_count < 1:
        raise ApiError("RECORDING_SEGMENT_AUDIO_REQUIRED", "A take segment must be an audio-only Material", 415)
    audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
    if not 0 <= stream_index < len(audio_streams):
        raise ApiError("AUDIO_STREAM_INDEX_OUT_OF_RANGE", "The selected recording audio stream does not exist", 400)
    stream = audio_streams[stream_index]
    codec_name = stream.get("codec_name")
    sample_rate = stream.get("sample_rate")
    channels = stream.get("channels")
    if type(sample_rate) is not int or type(channels) is not int:
        raise ApiError("RECORDING_SEGMENT_FORMAT_UNAVAILABLE", "The recording segment sample format could not be measured", 415)
    if codec_name != expected_sample_format:
        raise ApiError("RECORDING_SEGMENT_FORMAT_MISMATCH", "The recorded segment codec differs from the prepared take", 409,
                       {"expected": expected_sample_format, "actual": codec_name})
    if sample_rate != expected_sample_rate or channels != expected_channels:
        raise ApiError("RECORDING_SEGMENT_CLOCK_MISMATCH", "The recorded segment sample rate or channel count changed", 409,
                       {"expected_sample_rate": expected_sample_rate, "actual_sample_rate": sample_rate,
                        "expected_channels": expected_channels, "actual_channels": channels})
    if expected_sample_format.startswith("pcm_") and asset.format_metadata.get("format_name") != "wav":
        raise ApiError("RECORDING_SEGMENT_CONTAINER_UNSUPPORTED", "PCM take segments must be WAV files", 415)
    if expected_sample_format == "flac" and "flac" not in str(asset.format_metadata.get("format_name", "")).split(","):
        raise ApiError("RECORDING_SEGMENT_CONTAINER_UNSUPPORTED", "FLAC take segments must use a FLAC container", 415)
    exact_count = asset.duration_sec * sample_rate
    if exact_count.denominator != 1 or exact_count.numerator <= 0:
        raise ApiError("RECORDING_SEGMENT_SAMPLE_COUNT_UNVERIFIED", "The saved segment does not expose an exact decoded sample count", 409)
    return material, asset, stream, exact_count.numerator


def _create_media_recording_session(store, identity: dict, project_id: str, data: dict):
    allowed = {"base_version", "cue_id", "speaker_label", "track_kind", "timeline_start_sec",
               "sample_rate", "channels", "sample_format"}
    required = {"base_version", "timeline_start_sec", "sample_rate", "channels", "sample_format"}
    if set(data) - allowed or not required <= set(data):
        raise ApiError("INVALID_INPUT", "Recording setup fields are invalid", 400)
    base_version, sample_rate, channels = data["base_version"], data["sample_rate"], data["channels"]
    sample_format, track_kind = data["sample_format"], data.get("track_kind", "voiceover")
    if type(base_version) is not int or base_version < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
        raise ApiError("INVALID_INPUT", "sample_rate is outside the supported range", 400)
    if type(channels) is not int or channels not in {1, 2, 6, 8}:
        raise ApiError("INVALID_INPUT", "channels must be mono, stereo, 5.1 or 7.1", 400)
    if sample_format not in {"pcm_s16le", "pcm_s24le", "pcm_f32le", "flac"}:
        raise ApiError("INVALID_INPUT", "sample_format must be lossless PCM WAV or FLAC", 400)
    if track_kind not in {"voiceover", "bgm", "sfx"}:
        raise ApiError("AUDIO_TRACK_ROLE_NOT_ALLOWED", "Recording track kind is invalid", 400)
    try:
        timeline_start = _fraction_time(data["timeline_start_sec"], error_code="MEDIA_RECORDING_TIME_INVALID")
    except VideoEditingError as exc:
        raise ApiError("MEDIA_RECORDING_TIME_INVALID", "Recording position is invalid", 400) from exc
    if timeline_start < 0:
        raise ApiError("MEDIA_RECORDING_TIME_INVALID", "Recording position cannot be negative", 400)
    cue_id = _string(data["cue_id"], "cue_id", maximum=128) if data.get("cue_id") is not None else None
    speaker_label = _string(data["speaker_label"], "speaker_label", maximum=128) if data.get("speaker_label") is not None else None
    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "Media project changed since recording setup", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    key = _required_idempotency_key()
    fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version, "cue_id": cue_id,
                                "speaker_label": speaker_label, "track_kind": track_kind,
                                "timeline_start_sec": _time_json(timeline_start), "sample_rate": sample_rate,
                                "channels": channels, "sample_format": sample_format})
    existing = store.row(
        "SELECT * FROM media_recording_sessions WHERE workspace_id = ? AND project_id = ? AND idempotency_key = ?",
        (workspace_id, project_id, key),
    )
    if existing is not None:
        if existing["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different recording session", 409)
        return _success(_recording_session_view(store, existing), 200)
    session_id = str(uuid.uuid4())
    take_id = f"take-{uuid.uuid4().hex}"
    now = utc_now()
    with store.transaction() as connection:
        current = connection.execute(
            "SELECT current_version, work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        ).fetchone()
        if current is None:
            raise _not_found()
        if current["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Media project changed during recording setup", 409,
                           {"current_version": current["current_version"]})
        if current["work_id"]:
            require_work_access(connection, identity, current["work_id"], write=True)
        else:
            require_workspace_access(connection, identity, write=True)
        connection.execute(
            "INSERT INTO media_recording_sessions(id, workspace_id, project_id, owner_user_id, work_id, take_id, track_id, "
            "track_kind, cue_id, speaker_label, base_project_version, sample_rate, channels, sample_format, "
            "timeline_start_num, timeline_start_den, status, idempotency_key, request_hash, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?, ?, ?)",
            (session_id, workspace_id, project_id, identity["user"]["id"], current["work_id"], take_id, take_id,
             track_kind, cue_id, speaker_label, base_version, sample_rate, channels, sample_format,
             timeline_start.numerator, timeline_start.denominator, key, fingerprint, now, now),
        )
    return _success(_recording_session_view(store, _recording_session_row(store, workspace_id, session_id)), 201)


def _set_media_recording_state(store, identity: dict, session_id: str, action: str, data: dict):
    workspace_id = identity["workspace"]["id"]
    session = _recording_session_row(store, workspace_id, session_id)
    project = _project_row(store, workspace_id, session["project_id"])
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    key = _required_idempotency_key()
    fingerprint = _fingerprint({"session_id": session_id, "action": action, "data": data})
    operation = f"media_recording_{action}"
    prior = store.row(
        "SELECT request_hash FROM idempotency_records WHERE workspace_id = ? AND operation = ? AND key = ?",
        (workspace_id, operation, key),
    )
    if prior is not None:
        if prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different recording action", 409)
        return _success(_recording_session_view(store, session), 200)

    now = utc_now()
    with store.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM media_recording_sessions WHERE id = ? AND workspace_id = ?",
            (session_id, workspace_id),
        ).fetchone()
        if current is None:
            raise _not_found()
        current_project = connection.execute(
            "SELECT work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
            (current["project_id"], workspace_id),
        ).fetchone()
        if current_project is None:
            raise _not_found()
        if current_project["work_id"]:
            require_work_access(connection, identity, current_project["work_id"], write=True)
        else:
            require_workspace_access(connection, identity, write=True)
        status = current["status"]
        if action == "start":
            if status not in {"armed", "interrupted", "awaiting_first_segment"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "This take is not ready to start or resume", 409,
                               {"status": status})
            epoch_id = data.get("capture_epoch_id")
            if not isinstance(epoch_id, str) or not epoch_id or len(epoch_id) > 128:
                raise ApiError("INVALID_INPUT", "capture_epoch_id is required", 400)
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'awaiting_first_segment', capture_epoch_id = ?, "
                "interrupt_reason = NULL, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (epoch_id, now, session_id, workspace_id),
            )
        elif action == "interrupt":
            if status not in {"recording", "awaiting_first_segment", "paused"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "Only an active capture can be marked interrupted", 409,
                               {"status": status})
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'interrupted', interrupt_reason = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (data.get("reason"), now, session_id, workspace_id),
            )
        elif action == "pause":
            if status not in {"recording", "awaiting_first_segment"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "Only an active take can be paused", 409,
                               {"status": status})
            sequence = data.get("last_confirmed_sequence")
            if type(sequence) is not int or sequence != current["latest_sequence"]:
                raise ApiError("MEDIA_RECORDING_SEGMENT_SEQUENCE_CONFLICT", "The pause receipt does not match the last saved segment", 409,
                               {"last_confirmed_sequence": current["latest_sequence"]})
            if data.get("native_capture_paused") is not True:
                raise ApiError("MEDIA_RECORDING_PAUSE_UNCONFIRMED", "The client has not confirmed that native capture paused", 409)
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'paused', updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (now, session_id, workspace_id),
            )
        elif action == "resume":
            if status != "paused":
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "Only a paused take can resume the same clock epoch", 409,
                               {"status": status})
            if data.get("capture_epoch_id") != current["capture_epoch_id"] or data.get("native_capture_resumed") is not True:
                raise ApiError("MEDIA_RECORDING_CLOCK_EPOCH_MISMATCH", "Resume the same recorder clock or use explicit recovery for a new epoch", 409)
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'recording', updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (now, session_id, workspace_id),
            )
        elif action == "stop":
            if status not in {"awaiting_first_segment", "recording", "paused", "interrupted"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "This take is not recording or interrupted", 409,
                               {"status": status})
            sequence = data.get("last_confirmed_sequence")
            if type(sequence) is not int or sequence != current["latest_sequence"]:
                raise ApiError("MEDIA_RECORDING_SEGMENT_SEQUENCE_CONFLICT", "The stop receipt does not match the last saved segment", 409,
                               {"last_confirmed_sequence": current["latest_sequence"]})
            if data.get("native_capture_stopped") is not True:
                raise ApiError("MEDIA_RECORDING_STOP_UNCONFIRMED", "The client has not confirmed that capture stopped", 409)
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'stopping', stopped_at = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (now, now, session_id, workspace_id),
            )
        elif action == "cancel":
            if status not in {"armed", "awaiting_first_segment", "recording", "paused", "interrupted", "stopping"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "This take cannot be cancelled in its current state", 409,
                               {"status": status})
            if status in {"awaiting_first_segment", "recording", "paused", "stopping"} and data.get("native_capture_stopped") is not True:
                raise ApiError("MEDIA_RECORDING_STOP_UNCONFIRMED", "Stop the device before cancelling the take session", 409)
            connection.execute(
                "UPDATE media_recording_sessions SET status = 'cancelled', stopped_at = COALESCE(stopped_at, ?), "
                "updated_at = ? WHERE id = ? AND workspace_id = ?",
                (now, now, session_id, workspace_id),
            )
        elif action == "rebase":
            if status not in {"stopping", "interrupted", "paused"}:
                raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "Only a stopped or interrupted take can be rebased", 409,
                               {"status": status})
            version = data.get("base_version")
            if type(version) is not int or version != project["current_version"]:
                raise ApiError("VERSION_CONFLICT", "Choose the currently open version before completing this take", 409,
                               {"current_version": project["current_version"]})
            connection.execute(
                "UPDATE media_recording_sessions SET base_project_version = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (version, now, session_id, workspace_id),
            )
        else:
            raise ApiError("MEDIA_RECORDING_ACTION_UNSUPPORTED", "The recording action is unsupported", 400)
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
            "VALUES(?, ?, ?, ?, 'media_recording_session', ?, ?)",
            (workspace_id, operation, key, fingerprint, session_id, now),
        )
    return _success(_recording_session_view(store, _recording_session_row(store, workspace_id, session_id)))


def _append_media_recording_segment(store, identity: dict, session_id: str, data: dict):
    allowed = {"sequence", "material_id", "capture_epoch_id", "capture_sample_start",
               "sample_count", "audio_stream_index", "timeline_start_sec"}
    required = {"sequence", "material_id", "capture_epoch_id", "capture_sample_start"}
    if set(data) - allowed or not required <= set(data):
        raise ApiError("INVALID_INPUT", "Recording segment fields are invalid", 400)
    sequence = data["sequence"]
    material_id = data["material_id"]
    epoch_id = data["capture_epoch_id"]
    sample_start = data["capture_sample_start"]
    audio_stream_index = data.get("audio_stream_index", 0)
    if type(sequence) is not int or sequence < 0:
        raise ApiError("INVALID_INPUT", "sequence must be a nonnegative integer", 400)
    if not isinstance(material_id, str) or not material_id:
        raise ApiError("INVALID_INPUT", "material_id is required", 400)
    if not isinstance(epoch_id, str) or not epoch_id or len(epoch_id) > 128:
        raise ApiError("INVALID_INPUT", "capture_epoch_id is invalid", 400)
    if type(sample_start) is not int or sample_start < 0:
        raise ApiError("INVALID_INPUT", "capture_sample_start must be a nonnegative integer", 400)
    if type(audio_stream_index) is not int or audio_stream_index < 0:
        raise ApiError("AUDIO_STREAM_INDEX_OUT_OF_RANGE", "audio_stream_index must be a nonnegative integer", 400)
    try:
        declared_count = data.get("sample_count")
        if declared_count is not None and (type(declared_count) is not int or declared_count <= 0):
            raise VideoEditingError("RECORDING_SEGMENT_SAMPLE_COUNT_INVALID")
        explicit_timeline_start = (_fraction_time(data["timeline_start_sec"],
                                                  error_code="MEDIA_RECORDING_TIME_INVALID")
                                   if "timeline_start_sec" in data else None)
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "RECORDING_SEGMENT_SAMPLE_COUNT_INVALID",
                       "Recording segment timing is invalid", 400) from exc
    if explicit_timeline_start is not None and explicit_timeline_start < 0:
        raise ApiError("MEDIA_RECORDING_TIME_INVALID", "A recording segment cannot start before project time zero", 400)

    workspace_id = identity["workspace"]["id"]
    session = _recording_session_row(store, workspace_id, session_id)
    project = _project_row(store, workspace_id, session["project_id"])
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    if session["status"] not in {"recording", "awaiting_first_segment"}:
        raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "Only a capture session awaiting or receiving audio accepts segments", 409,
                       {"status": session["status"]})
    if epoch_id != session["capture_epoch_id"]:
        raise ApiError("MEDIA_RECORDING_CLOCK_EPOCH_MISMATCH", "Start or resume the device clock epoch before appending its segment", 409,
                       {"expected_capture_epoch_id": session["capture_epoch_id"]})

    _material, asset, stream, actual_sample_count = _recording_segment_asset(
        store, workspace_id, material_id, stream_index=audio_stream_index,
        expected_sample_rate=session["sample_rate"], expected_channels=session["channels"],
        expected_sample_format=session["sample_format"], identity=identity,
        target_work_id=project["work_id"],
    )
    if declared_count is not None and declared_count != actual_sample_count:
        raise ApiError("RECORDING_SEGMENT_SAMPLE_COUNT_MISMATCH", "The segment sample count differs from decoded media", 409,
                       {"declared_sample_count": declared_count, "decoded_sample_count": actual_sample_count})
    key = _required_idempotency_key()
    fingerprint = _fingerprint({
        "session_id": session_id, "sequence": sequence, "material_id": material_id,
        "source_sha256": asset.sha256, "capture_epoch_id": epoch_id,
        "capture_sample_start": sample_start, "sample_count": actual_sample_count,
        "audio_stream_index": audio_stream_index,
        "timeline_start_sec": _time_json(explicit_timeline_start) if explicit_timeline_start is not None else None,
    })
    prior = store.row(
        "SELECT request_hash FROM idempotency_records WHERE workspace_id = ? "
        "AND operation = 'media_recording_segment' AND key = ?",
        (workspace_id, key),
    )
    if prior is not None:
        if prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different recording segment", 409)
        return _success(_recording_session_view(store, _recording_session_row(store, workspace_id, session_id)), 200)

    duration = Fraction(actual_sample_count, session["sample_rate"])
    stream_time_base = stream.get("time_base")
    source_pts = stream.get("start_pts") if type(stream.get("start_pts")) is int else None
    time_base_num = stream_time_base.get("num") if isinstance(stream_time_base, dict) else None
    time_base_den = stream_time_base.get("den") if isinstance(stream_time_base, dict) else None
    now = utc_now()
    with store.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM media_recording_sessions WHERE id = ? AND workspace_id = ?",
            (session_id, workspace_id),
        ).fetchone()
        if current is None:
            raise _not_found()
        current_project = connection.execute(
            "SELECT work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
            (current["project_id"], workspace_id),
        ).fetchone()
        if current_project is None:
            raise _not_found()
        if current_project["work_id"]:
            require_work_access(connection, identity, current_project["work_id"], write=True)
        else:
            require_workspace_access(connection, identity, write=True)
        _require_media_material_access_in_transaction(
            connection, identity, material_id, target_work_id=current_project["work_id"],
        )
        if current["status"] not in {"recording", "awaiting_first_segment"} or current["capture_epoch_id"] != epoch_id:
            raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "The capture session changed before this segment was saved", 409,
                           {"status": current["status"], "capture_epoch_id": current["capture_epoch_id"]})
        duplicate = connection.execute(
            "SELECT * FROM media_recording_segments WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        ).fetchone()
        if duplicate is not None:
            same = (duplicate["material_id"] == material_id and duplicate["source_sha256"] == asset.sha256
                    and duplicate["capture_epoch_id"] == epoch_id
                    and duplicate["capture_sample_start"] == sample_start
                    and duplicate["sample_count"] == actual_sample_count)
            if not same:
                raise ApiError("MEDIA_RECORDING_SEGMENT_SEQUENCE_CONFLICT", "This take sequence already contains a different segment", 409,
                               {"latest_sequence": current["latest_sequence"]})
            return _success(_recording_session_view(store, current), 200)
        expected_sequence = current["latest_sequence"] + 1
        if sequence != expected_sequence:
            raise ApiError("MEDIA_RECORDING_SEGMENT_SEQUENCE_CONFLICT", "Append the next unsaved segment sequence", 409,
                           {"expected_sequence": expected_sequence})
        previous = connection.execute(
            "SELECT * FROM media_recording_segments WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        gap_samples = 0
        if previous is None:
            if sample_start != 0:
                raise ApiError("MEDIA_RECORDING_CLOCK_ANCHOR_REQUIRED", "The first saved segment must begin at the recorder epoch sample zero", 409)
            timeline_start = Fraction(current["timeline_start_num"], current["timeline_start_den"])
            if explicit_timeline_start is not None and explicit_timeline_start != timeline_start:
                raise ApiError("MEDIA_RECORDING_TIMELINE_CLOCK_MISMATCH", "The first segment must use the prepared take timeline position", 409)
            clock_relation = "epoch_anchor"
        else:
            previous_start = Fraction(previous["timeline_start_num"], previous["timeline_start_den"])
            previous_end = previous_start + Fraction(previous["sample_count"], previous["sample_rate"])
            same_epoch = previous["capture_epoch_id"] == epoch_id
            if same_epoch:
                expected_sample_start = previous["capture_sample_start"] + previous["sample_count"]
                if sample_start < expected_sample_start:
                    raise ApiError("MEDIA_RECORDING_SAMPLE_OVERLAP", "A segment overlaps samples already saved in this capture epoch", 409,
                                   {"minimum_capture_sample_start": expected_sample_start})
                gap_samples = sample_start - expected_sample_start
                timeline_start = previous_end + Fraction(gap_samples, session["sample_rate"])
                if explicit_timeline_start is not None and explicit_timeline_start != timeline_start:
                    raise ApiError("MEDIA_RECORDING_TIMELINE_CLOCK_MISMATCH", "The declared position differs from the sample clock mapping", 409,
                                   {"expected_timeline_start_sec": _time_json(timeline_start)})
                clock_relation = "sample_gap" if gap_samples else "sample_contiguous"
            else:
                if explicit_timeline_start is None:
                    raise ApiError("MEDIA_RECORDING_CLOCK_EPOCH_MAPPING_REQUIRED",
                                   "A resumed device epoch needs an explicit timeline position; continuity cannot be inferred", 409)
                if explicit_timeline_start < previous_end:
                    raise ApiError("MEDIA_RECORDING_SAMPLE_OVERLAP", "A resumed device epoch overlaps already saved audio", 409,
                                   {"minimum_timeline_start_sec": _time_json(previous_end)})
                timeline_start = explicit_timeline_start
                sample_gap = (timeline_start - previous_end) * session["sample_rate"]
                gap_samples = sample_gap.numerator // sample_gap.denominator if sample_gap.denominator == 1 else 0
                clock_relation = "explicit_epoch_mapping"

        connection.execute(
            "INSERT INTO media_recording_segments(session_id, workspace_id, sequence, material_id, source_sha256, "
            "audio_stream_index, codec_name, sample_rate, channels, sample_count, capture_epoch_id, capture_sample_start, "
            "timeline_start_num, timeline_start_den, gap_samples_before, clock_relation, source_start_pts, "
            "source_time_base_num, source_time_base_den, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, workspace_id, sequence, material_id, asset.sha256, audio_stream_index,
             stream.get("codec_name"), session["sample_rate"], session["channels"], actual_sample_count,
             epoch_id, sample_start, timeline_start.numerator, timeline_start.denominator, gap_samples,
             clock_relation, source_pts, time_base_num, time_base_den, now),
        )
        connection.execute(
            "UPDATE media_recording_sessions SET latest_sequence = ?, status = 'recording', "
            "started_at = COALESCE(started_at, ?), updated_at = ? "
            "WHERE id = ? AND workspace_id = ?",
            (sequence, now, now, session_id, workspace_id),
        )
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
            "VALUES(?, 'media_recording_segment', ?, ?, 'media_recording_segment', ?, ?)",
            (workspace_id, key, fingerprint, f"{session_id}:{sequence}", now),
        )
    return _success(_recording_session_view(store, _recording_session_row(store, workspace_id, session_id)), 201)


def _recording_completion_version(store, workspace_id: str, project_id: str, session) -> tuple[int, int] | None:
    key_prefix = f"media-recording-complete-{session['id']}-"
    records = store.rows(
        "SELECT key, request_hash, resource_id FROM idempotency_records "
        "WHERE workspace_id = ? AND operation = 'media_project_version' AND key LIKE ?",
        (workspace_id, f"{key_prefix}%"),
    )
    committed = []
    for record in records:
        raw_base_version = record["key"][len(key_prefix):]
        if re.fullmatch(r"[1-9][0-9]*", raw_base_version) is None:
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved take completion identity is invalid", 409)
        base_version = int(raw_base_version)
        expected_hash = _fingerprint({
            "session_id": session["id"],
            "base_project_version": base_version,
        })
        if record["request_hash"] != expected_hash:
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved take completion identity changed", 409)
        recorded_project_id, separator, raw_version = record["resource_id"].rpartition(":")
        if not separator or recorded_project_id != project_id or not raw_version.isdigit():
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved take completion version is unavailable", 409)
        version = int(raw_version)
        version_row = store.row(
            "SELECT parent_version, operation, state_json FROM media_project_versions "
            "WHERE project_id = ? AND workspace_id = ? AND version = ?",
            (project_id, workspace_id, version),
        )
        if (version_row is None or version_row["operation"] != "record_take"
                or version_row["parent_version"] != base_version):
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved take project version is inconsistent", 409)
        try:
            state = json.loads(version_row["state_json"])
            tracks = state.get("timeline", {}).get("tracks", [])
        except (TypeError, ValueError, AttributeError) as exc:
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved take project version cannot be read", 409) from exc
        if not isinstance(tracks, list) or not any(
            isinstance(track, dict)
            and track.get("track_id") == session["track_id"]
            and track.get("take_id") == session["take_id"]
            and track.get("take_type") == "recorded"
            and track.get("recording_session_id") == session["id"]
            for track in tracks
        ):
            raise ApiError("MEDIA_RECORDING_COMPLETION_INVALID", "The saved project version does not contain this take", 409)
        committed.append((base_version, version))
    if len(committed) > 1:
        raise ApiError("MEDIA_RECORDING_COMPLETION_AMBIGUOUS", "More than one project version claims this recording take", 409)
    return committed[0] if committed else None


def _record_media_take_completion(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    session_id: str,
    project_version: int,
    request_key: str,
    request_hash: str,
    now: str,
    expected_base_version: int | None = None,
) -> None:
    current = connection.execute(
        "SELECT status, base_project_version, project_version FROM media_recording_sessions "
        "WHERE id = ? AND workspace_id = ?",
        (session_id, workspace_id),
    ).fetchone()
    if current is None:
        raise _not_found()
    if current["status"] == "completed":
        if current["project_version"] != project_version:
            raise ApiError("MEDIA_RECORDING_COMPLETION_CONFLICT", "The take was completed in another project version", 409)
    else:
        if current["status"] != "stopping":
            raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "The take changed before its project version was committed", 409,
                           {"status": current["status"]})
        if expected_base_version is not None and current["base_project_version"] != expected_base_version:
            raise ApiError("VERSION_CONFLICT", "The recording base changed before the take was completed", 409,
                           {"current_version": current["base_project_version"]})
        updated = connection.execute(
            "UPDATE media_recording_sessions SET status = 'completed', project_version = ?, "
            "completion_idempotency_key = ?, completed_at = COALESCE(completed_at, ?), updated_at = ? "
            "WHERE id = ? AND workspace_id = ? AND status = 'stopping'",
            (project_version, request_key, now, now, session_id, workspace_id),
        )
        if updated.rowcount != 1:
            raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "The take changed before its completion receipt was saved", 409)

    resource_id = f"{project_id}:{project_version}"
    prior = connection.execute(
        "SELECT request_hash, resource_id FROM idempotency_records "
        "WHERE workspace_id = ? AND operation = 'media_recording_complete' AND key = ?",
        (workspace_id, request_key),
    ).fetchone()
    if prior is not None:
        if prior["request_hash"] != request_hash or prior["resource_id"] != resource_id:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different take completion", 409)
    else:
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
            "VALUES(?, 'media_recording_complete', ?, ?, 'media_project_version', ?, ?)",
            (workspace_id, request_key, request_hash, resource_id, now),
        )


def _complete_media_recording_take(store, identity: dict, session_id: str):
    workspace_id = identity["workspace"]["id"]
    session = _recording_session_row(store, workspace_id, session_id)
    project_id = session["project_id"]
    project = _project_row(store, workspace_id, project_id)
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    key = _required_idempotency_key()
    fingerprint = _fingerprint({"session_id": session_id, "base_project_version": session["base_project_version"]})
    route_prior = store.row(
        "SELECT request_hash, resource_id FROM idempotency_records WHERE workspace_id = ? "
        "AND operation = 'media_recording_complete' AND key = ?",
        (workspace_id, key),
    )
    if route_prior is not None:
        if route_prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different take completion", 409)
        raw_project, separator, raw_version = route_prior["resource_id"].rpartition(":")
        if not separator or raw_project != session["project_id"]:
            raise ApiError("IDEMPOTENCY_CONFLICT", "The completed take receipt is unavailable", 409)
        return _success({
            "recording": _recording_session_view(store, _recording_session_row(store, workspace_id, session_id)),
            "project": _project_view(store, workspace_id, session["project_id"], int(raw_version)),
        }, 200)
    committed_completion = _recording_completion_version(
        store, workspace_id, session["project_id"], session,
    )
    if committed_completion is not None:
        _committed_base_version, committed_version = committed_completion
        now = utc_now()
        with store.transaction() as connection:
            _record_media_take_completion(
                connection,
                workspace_id=workspace_id,
                project_id=session["project_id"],
                session_id=session_id,
                project_version=committed_version,
                request_key=key,
                request_hash=fingerprint,
                now=now,
            )
        return _success({
            "recording": _recording_session_view(
                store, _recording_session_row(store, workspace_id, session_id),
            ),
            "project": _project_view(store, workspace_id, session["project_id"], committed_version),
        }, 200)
    if session["status"] != "stopping":
        if session["status"] == "completed" and session["project_version"] is not None:
            return _success({
                "recording": _recording_session_view(store, session),
                "project": _project_view(store, workspace_id, session["project_id"], session["project_version"]),
            }, 200)
        raise ApiError("MEDIA_RECORDING_STATE_CONFLICT", "A take can be finalized only after native capture has stopped", 409,
                       {"status": session["status"]})
    if project["current_version"] != session["base_project_version"]:
        raise ApiError("VERSION_CONFLICT", "The project changed during recording; rebase the stopped take after review", 409,
                       {"current_version": project["current_version"],
                        "recording_base_version": session["base_project_version"]})
    segments = store.rows(
        "SELECT * FROM media_recording_segments WHERE session_id = ? AND workspace_id = ? ORDER BY sequence",
        (session_id, workspace_id),
    )
    if not segments:
        raise ApiError("MEDIA_RECORDING_HAS_NO_SEGMENTS", "No verified recording segments are available for this take", 409)
    state, version_row = _read_version_state(store, workspace_id, project_id, session["base_project_version"])
    timeline, _availability = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    if any(track.track_id == session["track_id"] for track in timeline.tracks):
        raise ApiError("MEDIA_RECORDING_TAKE_ALREADY_EXISTS", "This take track is already present in the selected project version", 409)
    try:
        for segment in segments:
            material, asset, _stream, actual_sample_count = _recording_segment_asset(
                store, workspace_id, segment["material_id"], stream_index=segment["audio_stream_index"],
                expected_sample_rate=session["sample_rate"], expected_channels=session["channels"],
                expected_sample_format=session["sample_format"],
                identity=identity, target_work_id=project["work_id"],
            )
            if (asset.sha256 != segment["source_sha256"]
                    or actual_sample_count != segment["sample_count"]):
                raise ApiError("RECORDING_SEGMENT_SOURCE_CHANGED", "A saved take segment no longer matches its verified source", 409,
                               {"sequence": segment["sequence"]})
            timeline = timeline.add_asset(asset)
        timeline = timeline.add_audio_track(
            session["track_id"], audio_role=session["track_kind"], take_id=session["take_id"],
            take_type="recorded", recording_session_id=session_id,
            cue_id=session["cue_id"], speaker_label=session["speaker_label"],
        )
        for segment in segments:
            asset = next(asset for asset in timeline.assets if asset.source_material_id == segment["material_id"])
            duration = Fraction(segment["sample_count"], segment["sample_rate"])
            mix = AudioMix()
            timeline = timeline.add_clip(
                asset.asset_id,
                source_in=0,
                source_out=duration,
                track_id=session["track_id"],
                purpose=f"recording:{session['take_id']}:segment:{segment['sequence']}",
                audio_role=session["track_kind"],
                timeline_start=Fraction(segment["timeline_start_num"], segment["timeline_start_den"]),
                audio_stream_index=segment["audio_stream_index"],
            )
            clip = timeline.tracks[-1].clips[-1]
            timeline = timeline.set_audio_mix(clip.clip_id, mix)
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "MEDIA_RECORDING_TAKE_INVALID",
                       "The recorded take could not be bound to this Media timeline", 409) from exc

    next_state = _copy_project_state(state, timeline)
    undo_stack = _version_stack(version_row, "undo_stack_json") + [session["base_project_version"]]
    completion_key = f"media-recording-complete-{session_id}-{session['base_project_version']}"

    def finalize_take(connection: sqlite3.Connection, project_version: int) -> None:
        _record_media_take_completion(
            connection,
            workspace_id=workspace_id,
            project_id=session["project_id"],
            session_id=session_id,
            project_version=project_version,
            request_key=key,
            request_hash=fingerprint,
            now=utc_now(),
            expected_base_version=session["base_project_version"],
        )

    new_version, created = _append_project_version(
        store, identity, session["project_id"], base_version=session["base_project_version"],
        state=next_state, operation="record_take", idempotency_key=completion_key,
        request_hash=fingerprint, undo_stack=undo_stack, redo_stack=[],
        after_version=finalize_take,
    )
    return _success({
        "recording": _recording_session_view(store, _recording_session_row(store, workspace_id, session_id)),
        "project": _project_view(store, workspace_id, session["project_id"], new_version),
    }, 201 if created else 200)


def _create_media_take_comp(store, identity: dict, project_id: str, data: dict):
    allowed = {"base_version", "cue_id", "track_id", "speaker_label", "segments"}
    if set(data) - allowed or not {"base_version", "cue_id", "track_id", "segments"} <= set(data):
        raise ApiError("INVALID_INPUT", "Take comp requires a parent version, cue, track and selected audio ranges", 400)
    base_version, cue_id = data["base_version"], data["cue_id"]
    track_id = _string(data["track_id"], "track_id", maximum=128)
    speaker_label = _string(data["speaker_label"], "speaker_label", maximum=128) if data.get("speaker_label") is not None else None
    raw_segments = data["segments"]
    if type(base_version) is not int or base_version < 1 or not isinstance(cue_id, str) or not cue_id:
        raise ApiError("INVALID_INPUT", "Take comp identity is invalid", 400)
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 10000:
        raise ApiError("INVALID_INPUT", "A take comp needs between 1 and 10000 selected ranges", 400)
    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "Media project changed since the comp was planned", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    key = _required_idempotency_key()
    fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version, "cue_id": cue_id,
                                "track_id": track_id, "speaker_label": speaker_label, "segments": raw_segments})
    prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
    if prior_version is not None:
        return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
    state, version_row = _read_version_state(store, workspace_id, project_id, base_version)
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    if any(track.track_id == track_id for track in timeline.tracks):
        raise ApiError("TIMELINE_TRACK_ALREADY_EXISTS", "The comp track already exists in this version", 409)
    try:
        timeline = timeline.add_audio_track(
            track_id, audio_role="voiceover", take_id=f"comp-{uuid.uuid4().hex}",
            take_type="comp", cue_id=cue_id, speaker_label=speaker_label,
        )
        for index, selection in enumerate(raw_segments):
            required = {"recording_session_id", "sequence", "source_in_sample", "source_out_sample", "timeline_start_sec"}
            optional = {"fade_in_samples", "fade_out_samples"}
            if type(selection) is not dict or not required <= set(selection) or set(selection) - required - optional:
                raise ApiError("INVALID_INPUT", "A comp selection is malformed", 400, {"selection_index": index})
            session_id = selection["recording_session_id"]
            sequence = selection["sequence"]
            if not isinstance(session_id, str) or type(sequence) is not int or sequence < 0:
                raise ApiError("INVALID_INPUT", "A comp selection source identity is invalid", 400,
                               {"selection_index": index})
            source_take = _recording_session_row(store, workspace_id, session_id)
            if (source_take["project_id"] != project_id or source_take["status"] != "completed"
                    or source_take["cue_id"] != cue_id or source_take["track_kind"] != "voiceover"):
                raise ApiError("TAKE_COMP_SOURCE_NOT_AVAILABLE", "Selected source is not a completed take for this cue", 409,
                               {"selection_index": index})
            segment = store.row(
                "SELECT * FROM media_recording_segments WHERE session_id = ? AND workspace_id = ? AND sequence = ?",
                (session_id, workspace_id, sequence),
            )
            if segment is None:
                raise ApiError("TAKE_COMP_SOURCE_NOT_AVAILABLE", "The selected take segment is no longer available", 409,
                               {"selection_index": index})
            source_track = next((item for item in timeline.tracks
                                 if item.recording_session_id == session_id and item.take_type == "recorded"), None)
            source_clip = None
            if source_track is not None:
                purpose = f"recording:{source_take['take_id']}:segment:{sequence}"
                source_clip = next((item for item in source_track.clips if item.purpose == purpose), None)
            if source_track is None or source_clip is None or source_track.locked:
                raise ApiError("TAKE_COMP_SOURCE_NOT_AVAILABLE", "The source take segment is absent or locked in this project version", 409,
                               {"selection_index": index})
            source_asset = timeline._asset(source_clip.media_asset_id)
            if source_asset.source_material_id != segment["material_id"] or source_asset.sha256 != segment["source_sha256"]:
                raise ApiError("TAKE_COMP_SOURCE_CHANGED", "The selected take source changed", 409,
                               {"selection_index": index})
            sample_rate = segment["sample_rate"]
            source_in_sample = selection["source_in_sample"]
            source_out_sample = selection["source_out_sample"]
            if (type(source_in_sample) is not int or type(source_out_sample) is not int
                    or not 0 <= source_in_sample < source_out_sample <= segment["sample_count"]):
                raise ApiError("TAKE_COMP_RANGE_INVALID", "A comp range must be a nonempty half-open sample interval", 400,
                               {"selection_index": index, "sample_count": segment["sample_count"]})
            source_in = Fraction(source_in_sample, sample_rate)
            source_out = Fraction(source_out_sample, sample_rate)
            if source_in < source_clip.source_in or source_out > source_clip.source_out:
                raise ApiError("TAKE_COMP_RANGE_OUTSIDE_SELECTED_CLIP", "The sample range is outside the saved take clip", 409,
                               {"selection_index": index})
            try:
                timeline_start = _fraction_time(selection["timeline_start_sec"],
                                                error_code="TAKE_COMP_TIMELINE_POSITION_INVALID")
            except VideoEditingError as exc:
                raise ApiError("TAKE_COMP_TIMELINE_POSITION_INVALID", "The comp segment position is invalid", 400,
                               {"selection_index": index}) from exc
            if timeline_start < 0:
                raise ApiError("TAKE_COMP_TIMELINE_POSITION_INVALID", "The comp segment position cannot be negative", 400,
                               {"selection_index": index})
            fade_in_samples = selection.get("fade_in_samples", 0)
            fade_out_samples = selection.get("fade_out_samples", 0)
            if (type(fade_in_samples) is not int or type(fade_out_samples) is not int
                    or fade_in_samples < 0 or fade_out_samples < 0
                    or fade_in_samples + fade_out_samples > source_out_sample - source_in_sample):
                raise ApiError("TAKE_COMP_FADE_INVALID", "Comp fades exceed the selected source sample range", 400,
                               {"selection_index": index})
            timeline = timeline.add_clip(
                source_clip.media_asset_id, source_in=source_in, source_out=source_out,
                track_id=track_id, purpose=f"comp:{source_take['take_id']}:{sequence}:{index}",
                audio_role="voiceover", timeline_start=timeline_start,
                audio_stream_index=source_clip.audio_stream_index,
            )
            comp_clip = timeline.tracks[-1].clips[-1]
            if source_clip.playback_rate != 1:
                timeline = timeline.set_playback_rate(comp_clip.clip_id, playback_rate=source_clip.playback_rate)
            comp_mix = AudioMix(
                volume=source_clip.audio_mix.volume,
                fade_in_sec=Fraction(fade_in_samples, sample_rate) / source_clip.playback_rate,
                fade_out_sec=Fraction(fade_out_samples, sample_rate) / source_clip.playback_rate,
                ducking=source_clip.audio_mix.ducking,
            )
            timeline = timeline.set_audio_mix(comp_clip.clip_id, comp_mix)
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "TAKE_COMP_INVALID", "The selected comp ranges could not be saved", 400) from exc
    state = _copy_project_state(state, timeline)
    undo_stack = _version_stack(version_row, "undo_stack_json") + [base_version]
    version, _ = _append_project_version(
        store, identity, project_id, base_version=base_version, state=state,
        operation="take_comp", idempotency_key=key, request_hash=fingerprint,
        undo_stack=undo_stack, redo_stack=[],
    )
    return _success(_project_view(store, workspace_id, project_id, version))


def revise_media_project(
    store,
    identity: dict,
    project_id: str,
    data: dict,
    idempotency_key: str,
) -> dict:
    """Commit timeline and/or editable storyboard changes in one Media project version."""
    allowed_fields = {"base_version", "operations", "storyboard_operations"}
    if not isinstance(data, dict) or set(data) - allowed_fields or "base_version" not in data:
        raise ApiError("INVALID_INPUT", "Media revision requires base_version and named timeline or storyboard operations", 400)
    base_version = data["base_version"]
    operations = data.get("operations", [])
    storyboard_operations = data.get("storyboard_operations", [])
    if (type(base_version) is not int or base_version < 1
            or not isinstance(operations, list) or not isinstance(storyboard_operations, list)
            or not operations and not storyboard_operations):
        raise ApiError("INVALID_INPUT", "Media edit input is invalid", 400)
    key = _string(idempotency_key, "Idempotency-Key", maximum=200)
    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id, identity=identity, write=True)
    fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version,
                                "operations": operations, "storyboard_operations": storyboard_operations})
    prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
    if prior_version is not None:
        return _project_view(store, workspace_id, project_id, prior_version, identity=identity)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                       {"current_version": project["current_version"]})
    state, version_row = _read_version_state(
        store, workspace_id, project_id, base_version, identity=identity, write=True,
    )
    timeline, _ = _load_timeline(
        store, workspace_id, state, project_id=project_id, identity=identity, write=True,
    )
    updated = timeline
    if operations:
        try:
            updated = timeline.apply_edit_batch(operations, expected_version=timeline.version)
        except VideoEditingError as exc:
            raise ApiError(exc.args[0] if exc.args else "MEDIA_EDIT_INVALID", "Media edit was not saved", 400) from exc
    next_state = _copy_project_state(state, updated)
    if storyboard_operations:
        next_state["storyboard"] = _apply_media_storyboard_operations(
            store, identity, project, state.get("storyboard"), updated, storyboard_operations,
            base_timeline=timeline,
        )
    undo_stack = _version_stack(version_row, "undo_stack_json") + [base_version]
    operation_name = ("edit_and_storyboard" if operations and storyboard_operations else
                      "edit" if operations else "storyboard_edit")
    new_version, _ = _append_project_version(
        store, identity, project_id, base_version=base_version, state=next_state,
        operation=operation_name, idempotency_key=key, request_hash=fingerprint,
        undo_stack=undo_stack, redo_stack=[],
    )
    return _project_view(store, workspace_id, project_id, new_version, identity=identity)


def require_media_review_access(connection, identity: dict, project_id: str, version: int,
                                *, write: bool = False):
    """Short SQL-only permission check shared by native Media comments."""
    workspace_id = identity["workspace"]["id"]
    if type(version) is not int or version < 1:
        raise ApiError("INVALID_INPUT", "A Media review must name a positive saved version", 400)
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (project_id, workspace_id),
    ).fetchone()
    if project is None:
        raise _not_found()
    if project["work_id"]:
        require_work_access(connection, identity, project["work_id"], write=write)
    else:
        require_workspace_access(connection, identity, write=write)
    version_row = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
        (project_id, workspace_id, version),
    ).fetchone()
    if version_row is None:
        raise ApiError("MEDIA_PROJECT_VERSION_UNAVAILABLE", "The selected Media review version is unavailable", 409)
    _require_media_project_version_sources(
        connection, identity, project, version_row, target_work_id=project["work_id"],
    )
    return version_row


def _media_review_source_clock(asset: MediaAsset, clip=None, track_kind: str | None = None) -> dict:
    if track_kind == "video" and asset.has_video:
        stream_kind = "video"
        ordinal = clip.video_stream_index if clip is not None and clip.video_stream_index is not None else 0
    elif asset.audio_stream_count:
        stream_kind = "audio"
        ordinal = clip.audio_stream_index if clip is not None and clip.audio_stream_index is not None else 0
    elif asset.has_video:
        stream_kind, ordinal = "video", 0
    else:
        stream_kind, ordinal = "unknown", 0
    streams = [item for item in asset.stream_metadata if item.get("codec_type") == stream_kind]
    selected = streams[ordinal] if stream_kind != "unknown" and ordinal < len(streams) else None
    return {
        "coordinate_space": "media_relative_rational_seconds",
        "material_id": asset.source_material_id,
        "source_sha256": asset.sha256,
        "stream_kind": stream_kind,
        "stream_ordinal": ordinal,
        "stream_index": None if selected is None else selected.get("index"),
        "start_pts": None if selected is None else selected.get("start_pts"),
        "start_time": None if selected is None else selected.get("start_time"),
        "time_base": None if selected is None else selected.get("time_base"),
        "sample_rate": None if selected is None else selected.get("sample_rate"),
        "format_start_time": asset.format_metadata.get("start_time"),
    }


def _media_review_value(document: dict, kind: str, property_name: str, object_id: str,
                        *, word_id: str | None = None):
    if kind == "media_transcript" and property_name.startswith("word."):
        word_doc = next((item for item in document.get("words", []) if item.get("word_id") == word_id), None)
        if word_doc is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected transcript word is unavailable in this version", 409)
        field = property_name.split(".", 1)[1]
        if field == "time":
            return {"start_sample": word_doc["start_sample"], "end_sample": word_doc["end_sample"]}
        if field not in {"text", "speaker_label", "token_probability"}:
            raise ApiError("MEDIA_REVIEW_PROPERTY_UNSUPPORTED", "The selected transcript word property cannot be reviewed", 400)
        return word_doc.get(field)
    roots = {
        "media_clip": {"clip_id", "media_asset_id", "track_id", "purpose", "audio_role", "audio_mix",
                       "audio_stream_index", "video_stream_index", "source_in", "source_out", "timeline_start",
                       "playback_rate", "display_duration_sec", "frame_crop", "video_placement", "effects",
                       "transform_keyframes"},
        "media_track": {"track_id", "kind", "layout", "locked", "muted", "solo", "hidden", "take_id",
                        "take_type", "recording_session_id", "cue_id", "speaker_label", "gain", "pan", "audio_bus_sends"},
        "media_caption": {"overlay_id", "kind", "start_sec", "end_sec", "text", "layout_profile", "style"},
        "media_chapter": {"chapter_id", "title", "start_sec"},
        "media_transcript": {"transcript_id", "source_asset_id", "source_sha256", "audio_stream_index",
                             "audio_channel_index", "sample_rate", "language", "alignment_source", "words",
                             "audio_stream_start_pts", "audio_stream_time_base", "task", "review_state"},
        "media_audio_bus": {"bus_id", "name", "output_bus_id", "gain", "pan", "effects"},
        "media_color_interpretation": {"asset_id", "video_stream_index", "source_sha256", "input_profile",
                                       "working_profile", "look_id", "look_sha256"},
        "media_storyboard_node": {"beat_id", "kind", "title", "question", "script_text", "purpose",
                                  "planned_duration_sec", "parent_beat_id", "cue_id", "speaker_label",
                                  "status", "source_refs", "clip_refs", "recording_refs"},
    }
    if kind not in roots:
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "The native Media review target is unsupported", 400)
    parts = property_name.split(".")
    if parts[0] not in roots[kind]:
        raise ApiError("MEDIA_REVIEW_PROPERTY_UNSUPPORTED", "The selected Media property cannot be reviewed", 400)
    if kind in {"media_clip", "media_audio_bus"} and parts[0] == "effects":
        effects = document.get("effects") or []
        if len(parts) == 1:
            return effects
        if len(parts) != 2:
            raise ApiError("MEDIA_REVIEW_PROPERTY_UNSUPPORTED", "Select one saved effect object rather than an array index", 400)
        matches = [item for item in effects if item.get("effect_id") == parts[1]]
        if len(matches) != 1:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media effect is unavailable in this version", 409)
        return matches[0]
    omitted_defaults = {
        "media_clip": {"playback_rate": {"num": 1, "den": 1}, "effects": [],
                       "transform_keyframes": [], "display_duration_sec": None,
                       "audio_stream_index": None, "video_stream_index": None,
                       "frame_crop": None, "video_placement": None},
        "media_track": {"layout": "sequence", "locked": False, "muted": False, "solo": False,
                         "hidden": False, "take_id": None, "take_type": None,
                         "recording_session_id": None, "cue_id": None, "speaker_label": None,
                         "gain": 1.0, "pan": 0.0, "audio_bus_sends": []},
    }
    if len(parts) == 1 and parts[0] not in document and parts[0] in omitted_defaults.get(kind, {}):
        return omitted_defaults[kind][parts[0]]
    value = document
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            raise ApiError("MEDIA_REVIEW_PROPERTY_UNSUPPORTED", "The selected Media property cannot be resolved", 400)
        value = value[part]
    return value


def _media_review_text_selection(value: str, anchor: dict, canonical: dict) -> None:
    quote_text = anchor.get("quote")
    start, end = anchor.get("start"), anchor.get("end")
    if quote_text is None and start is None and end is None:
        return
    if anchor.get("unit", "unicode_codepoint") != "unicode_codepoint":
        raise ApiError("MEDIA_REVIEW_ANCHOR_UNIT_MISMATCH", "Media text selections use Unicode code points", 409)
    if start is None and end is None and isinstance(quote_text, str) and quote_text:
        positions = [match.start() for match in re.finditer(re.escape(quote_text), value)]
        if len(positions) != 1:
            raise ApiError("MEDIA_REVIEW_ANCHOR_AMBIGUOUS", "Specify the exact occurrence of this text", 409)
        start, end = positions[0], positions[0] + len(quote_text)
    if (not isinstance(quote_text, str) or not quote_text or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(value) or value[start:end] != quote_text):
        raise ApiError("MEDIA_REVIEW_ANCHOR_MISMATCH", "The selected text does not match the saved Media version", 409)
    canonical.update({"start": start, "end": end, "quote": quote_text, "unit": "unicode_codepoint"})


def _media_review_mapped_text_selection(anchor: dict, canonical: dict) -> None:
    quote_text = anchor.get("quote")
    start, end = anchor.get("start"), anchor.get("end")
    if quote_text is None and start is None and end is None:
        return
    if (anchor.get("unit", "unicode_codepoint") != "unicode_codepoint"
            or not isinstance(quote_text, str) or not quote_text
            or type(start) is not int or type(end) is not int or start < 0 or end <= start):
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "The saved Media text range is invalid", 409)
    canonical.update({"start": start, "end": end, "quote": quote_text, "unit": "unicode_codepoint"})


def read_media_review_value(store, identity: dict, project_id: str, version: int,
                            anchor: dict, *, mapped: bool = False) -> tuple[dict, object]:
    """Read one native Media object/property and exact clock without a render or source hash pass."""
    if type(anchor) is not dict:
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "A Media review target must be an object", 400)
    allowed_fields = {"schema", "kind", "object_id", "property", "locator", "source_sha256",
                      "word_id", "quote", "start", "end", "unit", "sample_range", "timebase"}
    if set(anchor) - allowed_fields:
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "The Media review target contains an unsupported field", 400)
    kind, object_id, property_name = anchor.get("kind"), anchor.get("object_id"), anchor.get("property")
    kinds = {"media_clip", "media_track", "media_caption", "media_chapter", "media_transcript",
             "media_audio_bus", "media_color_interpretation", "media_storyboard_node"}
    if (kind not in kinds or not isinstance(object_id, str) or not object_id
            or not isinstance(property_name, str) or not property_name):
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "A Media review target needs a stable object identity and property", 400)
    if mapped:
        locator = anchor.get("locator")
        if (anchor.get("schema") != "oevira.media.review-anchor.v1" or type(locator) is not dict
                or locator.get("kind") != kind or locator.get("object_id") != object_id):
            raise ApiError("MEDIA_REVIEW_MAPPING_UNAVAILABLE", "The saved Media comment has no stable native locator", 409)
    elif anchor.get("schema") is not None or anchor.get("locator") is not None:
        raise ApiError("MEDIA_REVIEW_ANCHOR_INVALID", "A new Media review target must name its native object directly", 400)
    with store.transaction(immediate=False) as connection:
        version_row = dict(require_media_review_access(connection, identity, project_id, version))
    state = _project_state_from_version(version_row)
    timeline, _ = _load_timeline(
        store, identity["workspace"]["id"], state, project_id=project_id,
        identity=identity, verify_files=False, write=False,
    )
    clips = {clip.clip_id: (track, clip) for track in timeline.tracks for clip in track.clips}
    tracks = {track.track_id: track for track in timeline.tracks}
    overlays = {item.overlay_id: item for item in timeline.overlays}
    chapters = {item.chapter_id: item for item in timeline.chapters}
    transcripts = {item.transcript_id: item for item in timeline.transcripts}
    buses = {item.bus_id: item for item in timeline.audio_buses}
    colors = {item.asset_id: item for item in timeline.video_color_interpretations}
    storyboard_nodes = {item["beat_id"]: item for item in state.get("storyboard", _empty_media_storyboard())["nodes"]}
    locator = {"kind": kind, "object_id": object_id, "project_id": project_id}
    source_sha256 = None
    timebase = {"selected_value_domain": "native_media_parameter"}
    word_id = anchor.get("word_id")
    if kind == "media_clip":
        selected = clips.get(object_id)
        if selected is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media clip is unavailable in this version", 409)
        track, clip = selected
        asset = timeline._asset(clip.media_asset_id)
        source_sha256 = asset.sha256
        document = clip.to_dict()
        locator.update({"track_id": track.track_id, "asset_id": asset.asset_id,
                        "material_id": asset.source_material_id, "source_sha256": source_sha256})
        timebase = {"timeline": "rational_seconds",
                    "source": _media_review_source_clock(asset, clip, track.kind)}
        if property_name == "duration":
            value = _time_json(clip.duration_sec)
            timebase["selected_value_domain"] = "timeline_rational_seconds"
        elif property_name == "source_range":
            value = {"source_in": _time_json(clip.source_in), "source_out": _time_json(clip.source_out),
                     "playback_rate": _time_json(clip.playback_rate),
                     "display_duration_sec": (None if clip.display_duration_sec is None else _time_json(clip.display_duration_sec))}
            timebase["selected_value_domain"] = "source_media_relative_rational_seconds"
        elif property_name == "timeline_start":
            value = _time_json(clip.timeline_start)
            timebase["selected_value_domain"] = "timeline_rational_seconds"
        else:
            value = _media_review_value(document, kind, property_name, object_id)
            if property_name in {"audio_mix.fade_in_sec", "audio_mix.fade_out_sec"}:
                timebase["selected_value_domain"] = "clip_local_rational_seconds"
    elif kind == "media_track":
        track = tracks.get(object_id)
        if track is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media track is unavailable in this version", 409)
        value = _media_review_value(track.to_dict(), kind, property_name, object_id)
        source_materials = sorted({
            (timeline._asset(clip.media_asset_id).source_material_id,
             timeline._asset(clip.media_asset_id).sha256) for clip in track.clips
        }, key=lambda item: (item[0] or "", item[1]))
        locator.update({"track_id": track.track_id,
                        "source_materials": [{"material_id": material_id, "sha256": digest}
                                             for material_id, digest in source_materials]})
        timebase = {"selected_value_domain": "native_track_parameter", "timeline": "rational_seconds"}
    elif kind == "media_caption":
        overlay = overlays.get(object_id)
        if overlay is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media text overlay is unavailable in this version", 409)
        value = _media_review_value(overlay.to_dict(), kind, property_name, object_id)
        locator["overlay_id"] = object_id
        timebase = {"selected_value_domain": "timeline_rational_seconds", "timeline": "rational_seconds"}
    elif kind == "media_chapter":
        chapter = chapters.get(object_id)
        if chapter is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media chapter is unavailable in this version", 409)
        value = _media_review_value(chapter.to_dict(), kind, property_name, object_id)
        locator["chapter_id"] = object_id
        timebase = {"selected_value_domain": "timeline_rational_seconds", "timeline": "rational_seconds"}
    elif kind == "media_transcript":
        transcript = transcripts.get(object_id)
        if transcript is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media transcript is unavailable in this version", 409)
        value = _media_review_value(transcript.to_dict(), kind, property_name, object_id, word_id=word_id)
        source_sha256 = transcript.source_sha256
        locator.update({"transcript_id": transcript.transcript_id, "source_asset_id": transcript.source_asset_id,
                        "source_sha256": source_sha256, "word_id": word_id})
        timebase = {"selected_value_domain": "audio_samples", "sample_rate": transcript.sample_rate,
                    "audio_stream_index": transcript.audio_stream_index,
                    "audio_channel_index": transcript.audio_channel_index,
                    "source_sha256": transcript.source_sha256,
                    "source_start_pts": transcript.audio_stream_start_pts,
                    "source_time_base": (None if transcript.audio_stream_time_base is None else
                                          {"num": transcript.audio_stream_time_base[0], "den": transcript.audio_stream_time_base[1]})}
    elif kind == "media_audio_bus":
        bus = buses.get(object_id)
        if bus is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media audio bus is unavailable in this version", 409)
        value = _media_review_value(bus.to_dict(), kind, property_name, object_id)
        locator["bus_id"] = object_id
    elif kind == "media_storyboard_node":
        node = storyboard_nodes.get(object_id)
        if node is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected storyboard node is unavailable in this version", 409)
        value = _media_review_value(node, kind, property_name, object_id)
        source_refs = [{"ref_id": ref["ref_id"], "kind": ref["kind"],
                        "material_id": ref.get("material_id"), "artifact_id": ref.get("artifact_id"),
                        "version": ref.get("artifact_version"), "sha256": ref["sha256"]}
                       for ref in node["source_refs"]]
        clip_refs = [{"link_id": ref["link_id"], "clip_id": ref["clip_id"],
                      "source_sha256": ref["source_sha256"]} for ref in node["clip_refs"]]
        recording_refs = [{"link_id": ref["link_id"], "session_id": ref["session_id"],
                           "take_id": ref["take_id"], "project_version": ref["project_version"],
                           "clip_ids": list(ref["clip_ids"])}
                          for ref in node["recording_refs"]]
        locator.update({"beat_id": object_id, "source_refs": source_refs,
                        "clip_refs": clip_refs, "recording_refs": recording_refs})
        timebase = {"selected_value_domain": ("timeline_rational_seconds" if property_name == "planned_duration_sec"
                                               else "native_storyboard_property"),
                    "timeline": "rational_seconds"}
    else:
        interpretation = colors.get(object_id)
        if interpretation is None:
            raise ApiError("MEDIA_REVIEW_TARGET_MISSING", "The selected Media color interpretation is unavailable in this version", 409)
        value = _media_review_value(interpretation.to_dict(), kind, property_name, object_id)
        source_sha256 = interpretation.source_sha256
        locator.update({"asset_id": interpretation.asset_id, "source_sha256": source_sha256})
    if mapped:
        for field in ("track_id", "asset_id", "material_id", "source_sha256", "source_materials",
                      "chapter_id", "overlay_id", "transcript_id", "source_asset_id", "word_id", "bus_id"):
            if field in anchor["locator"] and anchor["locator"][field] != locator.get(field):
                raise ApiError("MEDIA_REVIEW_MAPPING_UNAVAILABLE", "The saved Media target no longer maps to the same native object or source", 409)
        for field in ("beat_id", "source_refs", "clip_refs", "recording_refs"):
            if field in anchor["locator"] and anchor["locator"][field] != locator.get(field):
                raise ApiError("MEDIA_REVIEW_MAPPING_UNAVAILABLE", "The saved Media target no longer maps to the same native object or source", 409)
    if anchor.get("source_sha256") is not None and anchor["source_sha256"] != source_sha256:
        raise ApiError("MEDIA_REVIEW_SOURCE_CHANGED", "The review anchor belongs to a different Media source", 409)
    canonical = {"schema": "oevira.media.review-anchor.v1", "kind": kind,
                 "object_id": object_id, "property": property_name,
                 "locator": locator, "source_sha256": source_sha256, "timebase": timebase}
    if word_id is not None:
        canonical["word_id"] = word_id
    if isinstance(value, str):
        if mapped:
            _media_review_mapped_text_selection(anchor, canonical)
        else:
            _media_review_text_selection(value, anchor, canonical)
    if kind == "media_clip" and "sample_range" in anchor:
        sample_range = anchor["sample_range"]
        asset = timeline._asset(clip.media_asset_id)
        stream_index = clip.audio_stream_index if clip.audio_stream_index is not None else 0
        streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
        expected_sample_fields = {"start_sample", "end_sample", "sample_rate"}
        if mapped:
            expected_sample_fields = expected_sample_fields | {"audio_stream_index", "source_sha256"}
        if (type(sample_range) is not dict or set(sample_range) != expected_sample_fields
                or any(type(sample_range.get(field)) is not int for field in ("start_sample", "end_sample", "sample_rate"))
                or sample_range["start_sample"] < 0 or sample_range["end_sample"] <= sample_range["start_sample"]
                or stream_index >= len(streams) or streams[stream_index].get("sample_rate") != sample_range["sample_rate"]):
            raise ApiError("MEDIA_REVIEW_SAMPLE_CLOCK_INVALID", "The selected sample range does not match its saved audio stream", 409)
        start_seconds = Fraction(sample_range["start_sample"], sample_range["sample_rate"])
        end_seconds = Fraction(sample_range["end_sample"], sample_range["sample_rate"])
        if (start_seconds < _fraction_time(clip.source_in)
                or end_seconds > _fraction_time(clip.source_out)):
            raise ApiError("MEDIA_REVIEW_SAMPLE_RANGE_OUTSIDE_CLIP", "The selected samples fall outside this clip's source range", 409)
        canonical["sample_range"] = {**sample_range, "audio_stream_index": stream_index,
                                     "source_sha256": asset.sha256}
        canonical["timebase"] = {"selected_value_domain": "audio_samples", "sample_rate": sample_range["sample_rate"],
                                 "audio_stream_index": stream_index, "source_sha256": asset.sha256}
        sample_selection = {"source_start_sample": sample_range["start_sample"],
                            "source_end_sample": sample_range["end_sample"],
                            "source_start_seconds": _time_json(start_seconds),
                            "source_end_seconds": _time_json(end_seconds),
                            "clip_source_in": _time_json(clip.source_in),
                            "clip_source_out": _time_json(clip.source_out)}
        if mapped and anchor.get("sample_range") != canonical["sample_range"]:
            raise ApiError("MEDIA_REVIEW_MAPPING_UNAVAILABLE", "The saved sample selection no longer maps to this source", 409)
        value = {"selected_value": value, "sample_selection": sample_selection}
    return canonical, value


def _project_state_from_version(version_row) -> dict:
    try:
        state = json.loads(version_row["state_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The selected Media version cannot be read", 409) from exc
    if not isinstance(state, dict):
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The selected Media version cannot be read", 409)
    return state


def _podcast_audio_projection(state: dict) -> dict:
    timeline = state.get("timeline") if isinstance(state, dict) else None
    assets = timeline.get("assets") if isinstance(timeline, dict) else None
    tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
    buses = timeline.get("audio_buses", []) if isinstance(timeline, dict) else None
    if not isinstance(assets, list) or not isinstance(tracks, list) or not isinstance(buses, list):
        raise ApiError("MEDIA_PODCAST_AUDIO_STATE_INVALID", "The selected Media version has no audio timeline snapshot", 409)
    assets_by_id = {item.get("asset_id"): item for item in assets if isinstance(item, dict)}
    selected_asset_ids = set()
    rendered_tracks = []
    duration = Fraction(0)
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("clips"), list):
            raise ApiError("MEDIA_PODCAST_AUDIO_STATE_INVALID", "The selected Media track snapshot is invalid", 409)
        for clip in track["clips"]:
            if not isinstance(clip, dict):
                raise ApiError("MEDIA_PODCAST_AUDIO_STATE_INVALID", "The selected Media clip snapshot is invalid", 409)
            source_in = _fraction_time(clip.get("source_in"), error_code="MEDIA_PODCAST_AUDIO_STATE_INVALID")
            source_out = _fraction_time(clip.get("source_out"), error_code="MEDIA_PODCAST_AUDIO_STATE_INVALID")
            rate = _fraction_time(clip.get("playback_rate", {"num": 1, "den": 1}),
                                  error_code="MEDIA_PODCAST_AUDIO_STATE_INVALID")
            start = _fraction_time(clip.get("timeline_start"), error_code="MEDIA_PODCAST_AUDIO_STATE_INVALID")
            raw_display = clip.get("display_duration_sec")
            clip_duration = (_fraction_time(raw_display, error_code="MEDIA_PODCAST_AUDIO_STATE_INVALID")
                             if raw_display is not None else (source_out - source_in) / rate)
            duration = max(duration, start + clip_duration)
        track_clips = []
        for clip in track["clips"]:
            asset_id = clip.get("media_asset_id")
            asset = assets_by_id.get(asset_id)
            if asset is None:
                raise ApiError("MEDIA_PODCAST_AUDIO_STATE_INVALID", "An audio track references a missing Media source", 409)
            audio_streams = [item for item in asset.get("streams", [])
                             if isinstance(item, dict) and item.get("codec_type") == "audio"]
            is_audio_track = track.get("kind") in {"voiceover", "bgm", "sfx"}
            is_original_video_audio = (track.get("kind") == "video" and clip.get("audio_role") == "original"
                                       and bool(audio_streams))
            if not is_audio_track and not is_original_video_audio:
                continue
            if clip.get("audio_role") == "muted" or not audio_streams:
                continue
            stream_index = clip.get("audio_stream_index")
            if stream_index is None:
                if len(audio_streams) != 1:
                    raise ApiError("MEDIA_PODCAST_AUDIO_STREAM_UNAVAILABLE", "The selected audio clip has no unique source stream", 409)
                stream_index = 0
            if type(stream_index) is not int or not 0 <= stream_index < len(audio_streams):
                raise ApiError("MEDIA_PODCAST_AUDIO_STREAM_UNAVAILABLE", "The selected audio stream is not present in the saved source", 409)
            selected_asset_ids.add(asset_id)
            track_clips.append({
                "clip_id": clip["clip_id"],
                "media_asset_id": asset_id,
                "source_material_id": asset.get("source_material_id"),
                "source_sha256": asset.get("sha256"),
                "track_id": track.get("track_id"),
                "track_kind": track.get("kind"),
                "timeline_start": clip["timeline_start"],
                "source_in": clip["source_in"],
                "source_out": clip["source_out"],
                "playback_rate": clip.get("playback_rate", {"num": 1, "den": 1}),
                "audio_role": clip.get("audio_role"),
                "audio_stream_index": stream_index,
                "audio_mix": clip.get("audio_mix"),
                "track_controls": {name: track.get(name, default) for name, default in
                                   (("muted", False), ("solo", False), ("gain", 1.0), ("pan", 0.0),
                                    ("audio_bus_sends", []))},
                "source_stream": audio_streams[stream_index],
            })
        controls = {name: track.get(name, default) for name, default in
                    (("track_id", None), ("kind", None), ("muted", False), ("solo", False),
                     ("gain", 1.0), ("pan", 0.0), ("audio_bus_sends", []))}
        rendered_tracks.append({"controls": controls, "audio_clips": track_clips})
    selected_assets = [
        {"asset_id": asset_id, "source_material_id": assets_by_id[asset_id].get("source_material_id"),
         "sha256": assets_by_id[asset_id].get("sha256"), "streams": assets_by_id[asset_id].get("streams", [])}
        for asset_id in sorted(selected_asset_ids)
    ]
    return {
        "project_kind": state.get("kind"),
        "timeline_duration": _time_json(duration),
        "tracks": rendered_tracks,
        "assets": selected_assets,
        "audio_buses": buses,
    }


def require_media_episode_sidecars_access(connection, identity: dict, render_id: str,
                                          *, target_work_id: str | None = None,
                                          project_version: int | None = None) -> dict:
    """Authorize a Podcast sidecar snapshot against one exact completed audio render."""
    if target_work_id is None:
        render = _require_media_render_access_in_transaction(
            connection, identity, render_id, expected_job_kind="media_render",
        )
        if (render["status"] != "completed" or render["job_status"] != "completed"
                or not render["sha256"] or type(render["size_bytes"]) is not int or render["size_bytes"] <= 0):
            raise ApiError("MEDIA_RENDER_NOT_DELIVERABLE", "Podcast sidecars need a completed audio render", 409)
        if json.loads(render["job_payload_json"]).get("purpose") != "render":
            raise ApiError("MEDIA_RENDER_NOT_DELIVERABLE", "A preview cannot anchor Podcast sidecars", 409)
    else:
        render = require_media_delivery_access(
            connection, identity, render_id, target_work_id=target_work_id,
        )
    if render["output_kind"] != "audio":
        raise ApiError("MEDIA_EPISODE_RENDER_REQUIRED", "Podcast sidecars must be tied to a completed audio render", 409)
    workspace_id = identity["workspace"]["id"]
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
        (render["project_id"], workspace_id),
    ).fetchone()
    if project is None or project["kind"] != "podcast":
        raise ApiError("MEDIA_EPISODE_PROJECT_REQUIRED", "Podcast sidecars need a saved podcast project", 409)
    selected_version = project["current_version"] if project_version is None else project_version
    if type(selected_version) is not int or selected_version < 1:
        raise ApiError("INVALID_INPUT", "project_version must be a positive saved Media version", 400)
    render_version = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
        (render["project_id"], workspace_id, render["project_version"]),
    ).fetchone()
    sidecar_version = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
        (render["project_id"], workspace_id, selected_version),
    ).fetchone()
    if render_version is None or sidecar_version is None:
        raise ApiError("MEDIA_PROJECT_VERSION_UNAVAILABLE", "The audio or sidecar version is unavailable", 409)
    source_scope = project["work_id"] if target_work_id is None else target_work_id
    render_state = _require_media_project_version_sources(
        connection, identity, project, render_version, target_work_id=source_scope,
    )
    sidecar_state = _require_media_project_version_sources(
        connection, identity, project, sidecar_version, target_work_id=source_scope,
    )
    render_projection = _podcast_audio_projection(render_state)
    sidecar_projection = _podcast_audio_projection(sidecar_state)
    render_projection_sha = _fingerprint(render_projection)
    sidecar_projection_sha = _fingerprint(sidecar_projection)
    if render_projection_sha != sidecar_projection_sha:
        raise ApiError("MEDIA_EPISODE_AUDIO_REVISION_INCOMPATIBLE",
                       "The selected transcript or chapter version does not match this rendered audio revision", 409,
                       {"render_project_version": render["project_version"], "sidecar_project_version": selected_version,
                        "render_audio_projection_sha256": render_projection_sha,
                        "sidecar_audio_projection_sha256": sidecar_projection_sha})
    return {"render": render, "project": dict(project), "render_version": dict(render_version),
            "sidecar_version": dict(sidecar_version), "render_state": render_state,
            "sidecar_state": sidecar_state, "episode_ref": f"urn:oevira:media-episode:{project['id']}",
            "audio_projection_sha256": render_projection_sha}


def _audio_render_duration(render: dict) -> Fraction:
    try:
        metadata = json.loads(render["metadata_json"] or "{}")
        encoding = metadata.get("encoding")
        measured = metadata.get("render", {}).get("measurement", {}).get("actual_duration_sec")
        if type(encoding) is dict and type(encoding.get("duration_sec")) in {int, float}:
            measured = encoding["duration_sec"]
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError) as exc:
        raise ApiError("MEDIA_EPISODE_RENDER_METADATA_UNAVAILABLE", "The completed podcast render has no verified duration", 409) from exc
    if type(measured) not in {int, float} or not math.isfinite(measured) or measured <= 0:
        raise ApiError("MEDIA_EPISODE_RENDER_METADATA_UNAVAILABLE", "The completed podcast render has no verified duration", 409)
    return Fraction(str(measured))


def _vtt_stamp(seconds: Fraction) -> tuple[str, Fraction]:
    scaled = seconds * 1000
    milliseconds = (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)
    actual = Fraction(milliseconds, 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{ms:03d}", actual


def _podcast_caption_documents(timeline: Timeline, duration: Fraction) -> tuple[dict[str, bytes], list[dict]]:
    import html as html_module

    overlays = sorted((item for item in timeline.overlays if item.kind == "caption"),
                      key=lambda item: (item.start_sec, item.overlay_id))
    if not overlays:
        return {}, []
    lines = ["WEBVTT", ""]
    mappings = []
    for overlay in overlays:
        start, end = _fraction_time(overlay.start_sec), _fraction_time(overlay.end_sec)
        if end > duration or start < 0:
            raise ApiError("MEDIA_EPISODE_SIDECAR_OUTSIDE_RENDER", "A caption extends beyond the selected audio render", 409,
                           {"overlay_id": overlay.overlay_id})
        start_text, quantized_start = _vtt_stamp(start)
        end_text, quantized_end = _vtt_stamp(end)
        if quantized_end <= quantized_start:
            raise ApiError("MEDIA_EPISODE_SIDECAR_TIME_COLLAPSED", "A caption became zero-length at WebVTT millisecond precision", 409,
                           {"overlay_id": overlay.overlay_id})
        lines.extend([overlay.overlay_id, f"{start_text} --> {end_text}", html_module.escape(overlay.text, quote=False), ""])
        mappings.append({"overlay_id": overlay.overlay_id,
                         "source_start_sec": _time_json(start), "source_end_sec": _time_json(end),
                         "vtt_start_sec": _time_json(quantized_start), "vtt_end_sec": _time_json(quantized_end),
                         "start_error_sec": _time_json(quantized_start - start),
                         "end_error_sec": _time_json(quantized_end - end),
                         "style": (None if overlay.style is None else overlay.style.to_dict()),
                         "representation": "literal_text_only; style and position remain in native project"})
    return {"captions.vtt": ("\n".join(lines) + "\n").encode("utf-8")}, mappings


def _podcast_transcript_documents(timeline: Timeline, duration: Fraction) -> tuple[dict[str, bytes], list[dict]]:
    import html as html_module

    transcripts = [item.to_dict() for item in timeline.transcripts]
    if not transcripts:
        return {}, []
    assets = {asset.asset_id: asset for asset in timeline.assets}
    audible_track_ids = {track.track_id for track in timeline.audible_tracks()}
    vtt_lines = ["WEBVTT", ""]
    all_occurrences, unavailable_clocks = [], []
    for transcript in timeline.transcripts:
        asset = assets[transcript.source_asset_id]
        audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
        if (transcript.audio_stream_index >= len(audio_streams)
                or transcript.sample_rate != audio_streams[transcript.audio_stream_index].get("sample_rate")):
            raise ApiError("MEDIA_EPISODE_TRANSCRIPT_SOURCE_MISMATCH", "A transcript stream differs from its frozen source", 409)
        stream = audio_streams[transcript.audio_stream_index]
        raw_tb, stream_start_pts = stream.get("time_base"), stream.get("start_pts")
        valid_time_base = (isinstance(raw_tb, dict) and type(raw_tb.get("num")) is int
                           and type(raw_tb.get("den")) is int and raw_tb["num"] > 0 and raw_tb["den"] > 0)
        raw_start_time = stream.get("start_time")
        valid_start_time = (isinstance(raw_start_time, dict) and type(raw_start_time.get("num")) is int
                            and type(raw_start_time.get("den")) is int and raw_start_time["den"] > 0)
        sample_rate = stream.get("sample_rate")
        pcm_file_clock = (type(sample_rate) is int and stream.get("codec_name", "").startswith("pcm_")
                          and valid_time_base and raw_tb["num"] == 1 and raw_tb["den"] == sample_rate)
        if (not valid_time_base or type(stream_start_pts) is not int and not valid_start_time and not pcm_file_clock):
            unavailable_clocks.append({"transcript_id": transcript.transcript_id,
                                       "reason": "source_pts_time_base_unavailable"})
            continue
        stream_origin = media_stream_input_origin(asset, "audio", transcript.audio_stream_index)
        origin_method = ("verified_stream_pts" if type(stream_start_pts) is int else
                         "verified_stream_start_time" if valid_start_time else "pcm_file_sample_zero")
        transcript_occurrences = []
        for track in timeline.tracks:
            if track.track_id not in audible_track_ids:
                continue
            for clip in track.clips:
                if (clip.media_asset_id != transcript.source_asset_id
                        or clip.audio_role == "muted" or clip.audio_mix.volume <= 0):
                    continue
                selected_stream = clip.audio_stream_index
                if selected_stream is None and asset.audio_stream_count == 1:
                    selected_stream = 0
                if selected_stream != transcript.audio_stream_index:
                    continue
                for word in transcript.words:
                    source_start = stream_origin + Fraction(word.start_sample, transcript.sample_rate)
                    source_end = stream_origin + Fraction(word.end_sample, transcript.sample_rate)
                    visible_start = max(source_start, _fraction_time(clip.source_in))
                    visible_end = min(source_end, _fraction_time(clip.source_out))
                    if visible_start >= visible_end:
                        continue
                    rate = _fraction_time(clip.playback_rate)
                    timeline_start = _fraction_time(clip.timeline_start) + (visible_start - _fraction_time(clip.source_in)) / rate
                    timeline_end = _fraction_time(clip.timeline_start) + (visible_end - _fraction_time(clip.source_in)) / rate
                    if timeline_end > duration:
                        raise ApiError("MEDIA_EPISODE_SIDECAR_OUTSIDE_RENDER", "A transcript word maps beyond the selected audio render", 409,
                                       {"transcript_id": transcript.transcript_id, "word_id": word.word_id})
                    occurrence = {
                        "transcript_id": transcript.transcript_id, "word_id": word.word_id,
                        "clip_id": clip.clip_id, "track_id": track.track_id,
                        "source_material_id": asset.source_material_id, "source_sha256": asset.sha256,
                        "source_start_sample": word.start_sample, "source_end_sample": word.end_sample,
                        "source_stream_origin": _time_json(stream_origin),
                        "source_clock_origin_method": origin_method,
                        "timeline_start_sec": _time_json(timeline_start),
                        "timeline_end_sec": _time_json(timeline_end),
                        "text": word.text, "speaker_label": word.speaker_label,
                    }
                    transcript_occurrences.append(occurrence)
        all_occurrences.extend(transcript_occurrences)
        for cue_index, occurrence in enumerate(transcript_occurrences, 1):
            start = _fraction_time(occurrence["timeline_start_sec"])
            end = _fraction_time(occurrence["timeline_end_sec"])
            start_text, start_quantized = _vtt_stamp(start)
            end_text, end_quantized = _vtt_stamp(end)
            if end_quantized <= start_quantized:
                # Keep the exact sample range in JSON; VTT cannot safely express this cue.
                continue
            text = html_module.escape(occurrence["text"], quote=False)
            speaker = occurrence.get("speaker_label")
            if speaker:
                text = f"<v {html_module.escape(speaker, quote=False)}>{text}</v>"
            cue_id = f"{transcript.transcript_id}-{occurrence['word_id']}-{cue_index}"
            vtt_lines.extend([cue_id, f"{start_text} --> {end_text}", text, ""])
            occurrence["vtt_start_sec"] = _time_json(start_quantized)
            occurrence["vtt_end_sec"] = _time_json(end_quantized)
            occurrence["start_error_sec"] = _time_json(start_quantized - start)
            occurrence["end_error_sec"] = _time_json(end_quantized - end)
    documents = {
        "transcript.json": (_json({"schema": "oevira.media.podcast.transcripts.v1",
                                   "time_reference": "timeline_rational_seconds",
                                   "transcripts": transcripts,
                                   "mapped_word_occurrences": all_occurrences,
                                   "unmapped_transcripts": unavailable_clocks}) + "\n").encode("utf-8"),
    }
    if len(vtt_lines) > 2:
        documents["transcript.vtt"] = ("\n".join(vtt_lines) + "\n").encode("utf-8")
    return documents, all_occurrences


def _podcast_storyboard_document(timeline: Timeline, project_id: str, project_version: int,
                                 episode_ref: str, audio_revision: dict,
                                 transcript_occurrences: list[dict],
                                 planned_storyboard: dict | None = None) -> dict:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    nodes = []
    for track in timeline.tracks:
        for clip in sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id)):
            asset = assets[clip.media_asset_id]
            start = _fraction_time(clip.timeline_start)
            end = start + clip.duration_sec
            caption_refs = [overlay.overlay_id for overlay in timeline.overlays
                            if max(start, _fraction_time(overlay.start_sec)) <
                            min(end, _fraction_time(overlay.end_sec))]
            chapter_refs = [chapter.chapter_id for chapter in timeline.chapters
                            if start <= _fraction_time(chapter.start_sec) < end]
            linked_words = [
                {"transcript_id": item["transcript_id"], "word_id": item["word_id"],
                 "timeline_start_sec": item["timeline_start_sec"],
                 "timeline_end_sec": item["timeline_end_sec"],
                 "text": item["text"], "speaker_label": item["speaker_label"]}
                for item in transcript_occurrences if item["clip_id"] == clip.clip_id
            ]
            nodes.append({
                "story_node_id": clip.clip_id,
                "node_kind": "timeline_clip",
                "track_id": track.track_id,
                "track_kind": track.kind,
                "cue_id": track.cue_id,
                "speaker_label": track.speaker_label,
                "take_id": track.take_id,
                "take_type": track.take_type,
                "recording_session_id": track.recording_session_id,
                "purpose": clip.purpose,
                "audio_role": clip.audio_role,
                "source": {"asset_id": asset.asset_id, "material_id": asset.source_material_id,
                           "filename": asset.filename, "sha256": asset.sha256,
                           "clock": _media_review_source_clock(asset, clip, track.kind)},
                "timeline_range": {"start_sec": _time_json(start), "end_sec": _time_json(end),
                                   "duration_sec": _time_json(clip.duration_sec)},
                "source_range": {"in_sec": _time_json(clip.source_in), "out_sec": _time_json(clip.source_out),
                                 "playback_rate": _time_json(clip.playback_rate)},
                "caption_refs": caption_refs,
                "chapter_refs": chapter_refs,
                "transcript_word_refs": linked_words,
                "effects": [item.to_dict() for item in clip.effects],
                "frame_crop": None if clip.frame_crop is None else clip.frame_crop.to_dict(),
                "video_placement": None if clip.video_placement is None else clip.video_placement.to_dict(),
                "transform_keyframes": [item.to_dict() for item in clip.transform_keyframes],
                "audio_mix": clip.audio_mix.to_dict(),
            })
    return {
        "schema": "oevira.media.podcast.storyboard-projection.v1",
        "project_id": project_id,
        "project_version": project_version,
        "episode_ref": episode_ref,
        "audio_revision": audio_revision,
        "source": "saved Media timeline object relationships",
        "inference_performed": False,
        "story_nodes": nodes,
        "planned_nodes": copy.deepcopy((planned_storyboard or _empty_media_storyboard()).get("nodes", [])),
    }


def _package_media_episode_sidecars(files: dict[str, bytes], manifest: dict) -> bytes:
    members = dict(files)
    receipts = {name: {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
                       "mime_type": {"chapters.json": "application/json+chapters",
                                     "chapters.provenance.json": "application/json",
                                     "captions.vtt": "text/vtt", "transcript.vtt": "text/vtt",
                                     "transcript.json": "application/json"}.get(name, "application/octet-stream")}
                for name, content in sorted(files.items())}
    manifest = {**manifest, "members": receipts}
    members["sidecar-manifest.json"] = (_json(manifest) + "\n").encode("utf-8")
    package_bytes = io.BytesIO()
    with zipfile.ZipFile(package_bytes, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for name, content in sorted(members.items()):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ApiError("MEDIA_EPISODE_SIDECAR_PATH_INVALID", "A Podcast sidecar member name is invalid", 500)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100600 & 0xFFFF) << 16
            info.extra = b""
            info.comment = b""
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    content = package_bytes.getvalue()
    if len(content) > 128 * 1024 * 1024:
        raise ApiError("MEDIA_EPISODE_SIDECAR_PACKAGE_TOO_LARGE", "The Podcast sidecar package exceeds the current safe package size", 413)
    return content


def resolve_media_episode_sidecars(store, identity: dict, render_id: str, *,
                                   target_work_id: str | None = None,
                                   project_version: int | None = None) -> dict:
    """Build chapter/transcript/caption files for one compatible Podcast audio revision."""
    with store.transaction(immediate=False) as connection:
        resolved = require_media_episode_sidecars_access(
            connection, identity, render_id, target_work_id=target_work_id,
            project_version=project_version,
        )
    project = resolved["project"]
    sidecar_version = resolved["sidecar_version"]
    state = resolved["sidecar_state"]
    timeline, _ = _load_timeline(
        store, identity["workspace"]["id"], state, project_id=project["id"],
        identity=identity, verify_files=False, target_work_id=target_work_id,
    )
    render = resolved["render"]
    audio_duration = _audio_render_duration(render)
    files: dict[str, bytes] = {}
    try:
        chapters_provenance = chapter_document(timeline)
    except VideoEditingError as exc:
        raise ApiError("MEDIA_EPISODE_CHAPTERS_INVALID", "The selected Podcast chapter sidecar is invalid", 409) from exc
    if chapters_provenance:
        chapter_rows = chapters_provenance["chapters"]
        standard_chapters = []
        for index, item in enumerate(chapter_rows):
            start = _fraction_time(item["start_sec"])
            if start >= audio_duration:
                raise ApiError("MEDIA_EPISODE_SIDECAR_OUTSIDE_RENDER", "A chapter falls outside the selected audio revision", 409,
                               {"chapter_id": item["chapter_id"]})
            chapter = {"startTime": float(start), "title": item["title"], "toc": True}
            if index + 1 < len(chapter_rows):
                chapter["endTime"] = float(_fraction_time(chapter_rows[index + 1]["start_sec"]))
            standard_chapters.append(chapter)
        chapter_document_value = {
            "version": "1.2.0",
            "title": state.get("title", project["title"]),
            "fileName": f"media-{render['id']}.{render['format']}",
            "chapters": standard_chapters,
        }
        files["chapters.json"] = (_json(chapter_document_value) + "\n").encode("utf-8")
        files["chapters.provenance.json"] = (_json({
            "project_id": project["id"], "project_version": sidecar_version["version"],
            "audio_render_id": render["id"], "audio_project_version": render["project_version"],
            "audio_sha256": render["sha256"], "chapter_revision_sha256": _fingerprint(chapter_rows),
            "exact_chapters": chapters_provenance,
        }) + "\n").encode("utf-8")
    caption_files, caption_mapping = _podcast_caption_documents(timeline, audio_duration)
    files.update(caption_files)
    transcript_files, transcript_occurrences = _podcast_transcript_documents(timeline, audio_duration)
    files.update(transcript_files)
    timeline_document = timeline.to_dict()
    transcripts = timeline_document.get("transcripts", [])
    overlays = timeline_document.get("overlays", [])
    chapter_rows = chapters_provenance.get("chapters", []) if chapters_provenance else []
    audio_revision = {
        "render_id": render["id"], "project_version": render["project_version"],
        "sha256": render["sha256"], "size_bytes": render["size_bytes"],
        "format": render["format"], "duration_sec": _time_json(audio_duration),
        "output_spec": json.loads(render["job_payload_json"]).get("resolved_output_spec"),
    }
    storyboard = _podcast_storyboard_document(
        timeline, project["id"], sidecar_version["version"], resolved["episode_ref"],
        audio_revision, transcript_occurrences, state.get("storyboard"),
    )
    files["storyboard.json"] = (_json(storyboard) + "\n").encode("utf-8")
    manifest = {
        "schema": "oevira.media.podcast.sidecar-bundle.v1",
        "episode_ref": resolved["episode_ref"],
        "project_id": project["id"], "sidecar_project_version": sidecar_version["version"],
        "audio_revision": audio_revision,
        "audio_projection_sha256": resolved["audio_projection_sha256"],
        "transcript_revision": (None if not transcripts else _fingerprint(transcripts)),
        "chapter_revision": (None if not chapter_rows else _fingerprint(chapter_rows)),
        "caption_revision": (None if not overlays else _fingerprint(overlays)),
        "storyboard_revision": _fingerprint(state.get("storyboard", _empty_media_storyboard())),
        "storyboard_revision": _fingerprint(storyboard),
        "time_reference": "rational_timeline_seconds",
        "caption_time_quantum": "1 millisecond WebVTT; exact rational times and errors are in provenance",
        "caption_mappings": caption_mapping,
        "transcript_mapped_word_count": len(transcript_occurrences),
        "rights_state": "unknown", "publication_state": "not_performed",
        "external_subscription_state": "not_performed",
    }
    content = _package_media_episode_sidecars(files, manifest)
    return {
        "content": content,
        "filename": f"episode-{project['id'][:8]}-sidecars-v{sidecar_version['version']}.zip",
        "mime_type": "application/zip", "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content), "episode_ref": resolved["episode_ref"],
        "project_id": project["id"], "project_version": sidecar_version["version"],
        "audio_revision": audio_revision,
        "transcript_revision": manifest["transcript_revision"],
        "chapter_revision": manifest["chapter_revision"],
        "caption_revision": manifest["caption_revision"],
        "audio_projection_sha256": resolved["audio_projection_sha256"],
    }


def _register_project_routes(app, store) -> None:
    @app.get("/api/media/projects")
    def media_list_projects():
        rows = store.rows(
            "SELECT id, title, kind, work_id, current_version, created_at, updated_at "
            "FROM media_projects WHERE workspace_id = ? ORDER BY updated_at DESC, id",
            (_workspace_id(),),
        )
        return _success([dict(row) for row in rows])

    @app.post("/api/media/projects")
    def media_create_project():
        identity = _require_write()
        data = _body()
        if set(data) - {"title", "kind", "work_id", "format_ratio"}:
            raise ApiError("INVALID_INPUT", "Media project contains an unsupported field", 400)
        title = _string(data.get("title"), "title", maximum=200)
        kind = data.get("kind")
        if kind not in {"video", "podcast"}:
            raise ApiError("INVALID_INPUT", "kind must be video or podcast", 400)
        work_id = data.get("work_id")
        if work_id is not None and not isinstance(work_id, str):
            raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
        _require_media_target_work_access(store, identity, work_id, write=True)
        format_ratio = data.get("format_ratio", "16:9" if kind == "video" else "16:9")
        if not isinstance(format_ratio, str) or format_ratio not in {"9:16", "4:5", "3:4", "16:9", "1:1"}:
            raise ApiError("INVALID_INPUT", "format_ratio is not supported", 400)
        key = _required_idempotency_key()
        payload = {"title": title, "kind": kind, "work_id": work_id, "format_ratio": format_ratio}
        fingerprint = _fingerprint(payload)
        workspace_id = identity["workspace"]["id"]
        project_id = str(uuid.uuid4())
        now = utc_now()
        timeline = _initial_timeline(kind)
        timeline = replace(timeline, format_ratio=format_ratio)
        state = _project_state(title=title, kind=kind, timeline=timeline)
        with store.transaction() as connection:
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            prior = connection.execute(
                "SELECT * FROM idempotency_records WHERE workspace_id = ? AND operation = 'create_media_project' AND key = ?",
                (workspace_id, key),
            ).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for another Media project", 409)
                project_id = prior["resource_id"]
                existing = connection.execute(
                    "SELECT id FROM media_projects WHERE id = ? AND workspace_id = ?",
                    (project_id, workspace_id),
                ).fetchone()
                if existing is None:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media project is unavailable", 409)
            else:
                connection.execute(
                    "INSERT INTO media_projects(id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by) "
                    "VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (project_id, workspace_id, identity["user"]["id"], work_id, title, kind, now, now, identity["user"]["id"]),
                )
                connection.execute(
                    "INSERT INTO media_project_versions(project_id, workspace_id, version, parent_version, operation, state_json, undo_stack_json, redo_stack_json, created_at, created_by) "
                    "VALUES(?, ?, 1, NULL, 'create', ?, '[]', '[]', ?, ?)",
                    (project_id, workspace_id, _json(state), now, identity["user"]["id"]),
                )
                connection.execute(
                    "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
                    "VALUES(?, 'create_media_project', ?, ?, 'media_project', ?, ?)",
                    (workspace_id, key, fingerprint, project_id, now),
                )
        return _success(_project_view(store, workspace_id, project_id), 201 if not prior else 200)

    @app.post("/api/media/projects/import")
    def media_import_project():
        return _import_media_project(store, _require_write(), _body())

    @app.post("/api/media/projects/import-history")
    def media_import_project_history():
        return _import_media_project_history(store, _require_write(), _body())

    @app.get("/api/media/projects/<project_id>")
    def media_get_project(project_id: str):
        selected = request.args.get("version")
        if selected is None:
            version = None
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
        return _success(_project_view(store, _workspace_id(), project_id, version))

    @app.get("/api/media/projects/<project_id>/storyboard")
    def media_get_storyboard(project_id: str):
        selected = request.args.get("version")
        if selected is None:
            version = None
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
        view = _project_view(store, _workspace_id(), project_id, version)
        return _success({"project_id": project_id, "project_version": view["version"],
                         "current_version": view["current_version"],
                         "storyboard": view["storyboard"]})

    @app.post("/api/media/projects/<project_id>/storyboard/edits")
    def media_edit_storyboard(project_id: str):
        data = _body()
        if set(data) != {"base_version", "operations"}:
            raise ApiError("INVALID_INPUT", "Storyboard revision requires base_version and operations", 400)
        result = revise_media_project(
            store, _require_write(), project_id,
            {"base_version": data["base_version"], "storyboard_operations": data["operations"]},
            _required_idempotency_key(),
        )
        return _success({"project_id": project_id, "project_version": result["version"],
                         "current_version": result["current_version"],
                         "storyboard": result["storyboard"]})

    @app.get("/api/media/projects/<project_id>/recordings")
    def media_list_recording_takes(project_id: str):
        workspace_id = _workspace_id()
        _project_row(store, workspace_id, project_id)
        rows = store.rows(
            "SELECT * FROM media_recording_sessions WHERE project_id = ? AND workspace_id = ? ORDER BY created_at, id",
            (project_id, workspace_id),
        )
        return _success([_recording_session_view(store, row) for row in rows])

    @app.post("/api/media/projects/<project_id>/recordings")
    def media_create_recording_take(project_id: str):
        return _create_media_recording_session(store, _require_write(), project_id, _body())

    @app.get("/api/media/recordings/<session_id>")
    def media_get_recording_take(session_id: str):
        session = _recording_session_row(store, _workspace_id(), session_id)
        _project_row(store, session["workspace_id"], session["project_id"])
        return _success(_recording_session_view(store, session))

    @app.post("/api/media/recordings/<session_id>/start")
    def media_start_recording_take(session_id: str):
        data = _body()
        if set(data) != {"capture_epoch_id"}:
            raise ApiError("INVALID_INPUT", "Start requires the client capture clock epoch", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "start", data)

    @app.post("/api/media/recordings/<session_id>/interrupt")
    def media_interrupt_recording_take(session_id: str):
        data = _body()
        reasons = {"device_disconnected", "permission_revoked", "application_interrupted", "storage_pressure"}
        if set(data) - {"reason"} or ("reason" in data and data["reason"] not in reasons):
            raise ApiError("INVALID_INPUT", "The recording interruption reason is invalid", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "interrupt", data)

    @app.post("/api/media/recordings/<session_id>/pause")
    def media_pause_recording_take(session_id: str):
        data = _body()
        if set(data) != {"last_confirmed_sequence", "native_capture_paused"}:
            raise ApiError("INVALID_INPUT", "Pause requires a segment receipt and native pause confirmation", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "pause", data)

    @app.post("/api/media/recordings/<session_id>/resume")
    def media_resume_recording_take(session_id: str):
        data = _body()
        if set(data) != {"capture_epoch_id", "native_capture_resumed"}:
            raise ApiError("INVALID_INPUT", "Resume requires the same epoch and native resume confirmation", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "resume", data)

    @app.post("/api/media/recordings/<session_id>/segments")
    def media_append_recording_segment(session_id: str):
        return _append_media_recording_segment(store, _require_write(), session_id, _body())

    @app.get("/api/media/recordings/<session_id>/segments/<int:sequence>/file")
    def media_play_recording_segment(session_id: str, sequence: int):
        workspace_id = _workspace_id()
        session = _recording_session_row(store, workspace_id, session_id)
        project = _project_row(store, workspace_id, session["project_id"])
        segment = store.row(
            "SELECT material_id, source_sha256 FROM media_recording_segments "
            "WHERE session_id = ? AND workspace_id = ? AND sequence = ?",
            (session_id, workspace_id, sequence),
        )
        if segment is None:
            raise _not_found()
        _require_media_material_access(
            store, g.identity, segment["material_id"], target_work_id=project["work_id"],
        )
        material, path = _store_material_source(store, workspace_id, segment["material_id"], verify=True)
        if material["sha256"] != segment["source_sha256"]:
            raise ApiError("RECORDING_SEGMENT_SOURCE_CHANGED", "The saved take segment failed integrity verification", 409)
        filename = Path(material["filename"])
        response = send_file(path, as_attachment=False, download_name=filename.name,
                             mimetype=_MEDIA_SOURCE_MIME_TYPES.get(filename.suffix.lower(), "application/octet-stream"),
                             conditional=True, etag=segment["source_sha256"])
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Media-Recording-Session"] = session_id
        response.headers["X-Media-Recording-Sequence"] = str(sequence)
        return response

    @app.post("/api/media/recordings/<session_id>/stop")
    def media_stop_recording_take(session_id: str):
        data = _body()
        if set(data) != {"last_confirmed_sequence", "native_capture_stopped"}:
            raise ApiError("INVALID_INPUT", "Stop requires the last saved sequence and native stop confirmation", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "stop", data)

    @app.post("/api/media/recordings/<session_id>/cancel")
    def media_cancel_recording_take(session_id: str):
        data = _body()
        if set(data) - {"native_capture_stopped"}:
            raise ApiError("INVALID_INPUT", "Cancel accepts only native stop confirmation", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "cancel", data)

    @app.post("/api/media/recordings/<session_id>/rebase")
    def media_rebase_recording_take(session_id: str):
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Rebase requires a version reviewed against this stopped take", 400)
        return _set_media_recording_state(store, _require_write(), session_id, "rebase", data)

    @app.post("/api/media/recordings/<session_id>/complete")
    def media_complete_recording_take(session_id: str):
        if _body():
            raise ApiError("INVALID_INPUT", "Take completion does not accept timeline edits", 400)
        return _complete_media_recording_take(store, _require_write(), session_id)

    @app.post("/api/media/projects/<project_id>/takes/comp")
    def media_create_take_comp(project_id: str):
        return _create_media_take_comp(store, _require_write(), project_id, _body())

    @app.get("/api/media/projects/<project_id>/export")
    def media_export_project(project_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected = request.args.get("version")
        if selected is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        content = _json(_project_export_document(store, workspace_id, project_id, version)).encode("utf-8")
        response = make_response(content)
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="media-project-{project_id}-v{version}.json"'
        response.headers["ETag"] = f'"{hashlib.sha256(content).hexdigest()}"'
        response.headers["X-Media-Project-Version"] = str(version)
        response.headers["X-Media-Source-Package-State"] = "material-references-only"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/media/projects/<project_id>/sources/<asset_id>/file")
    def media_stream_project_source(project_id: str, asset_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected = request.args.get("version")
        if selected is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=False, project_id=project_id,
        )
        try:
            asset = timeline._asset(asset_id)
        except VideoEditingError as exc:
            raise _not_found() from exc
        if not asset.source_material_id:
            raise _not_found()
        material, path = _store_material_source(store, workspace_id, asset.source_material_id, verify=True)
        if material["sha256"] != asset.sha256:
            raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "The project source no longer matches its saved version", 409)
        mimetype = _MEDIA_SOURCE_MIME_TYPES.get(Path(asset.filename).suffix.lower(), "application/octet-stream")
        response = send_file(
            path,
            as_attachment=False,
            download_name=asset.filename,
            mimetype=mimetype,
            conditional=True,
            etag=asset.sha256,
        )
        response.headers["X-Media-Project-Version"] = str(version)
        response.headers["X-Media-Source-SHA256"] = asset.sha256
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/media/projects/<project_id>/transcripts")
    def media_read_project_transcripts(project_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected_version = request.args.get("version")
        if selected_version is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected_version)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "200"))
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "offset and limit must be integers", 400) from exc
        if offset < 0 or not 1 <= limit <= 500:
            raise ApiError("INVALID_INPUT", "offset must be nonnegative and limit must be 1 to 500", 400)
        source_asset_id = request.args.get("source_asset_id")
        transcript_id = request.args.get("transcript_id")
        state, transcript_version_row = _read_version_state(store, workspace_id, project_id, version)
        timeline, _availability = _load_timeline(
            store, workspace_id, state, verify_files=True, project_id=project_id,
        )
        try:
            result = timeline.read_transcript_words(
                source_asset_id=source_asset_id,
                transcript_id=transcript_id,
            )
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_TRANSCRIPT_READ_FAILED"
            status = 404 if code == "MEDIA_TRANSCRIPT_NOT_FOUND" else 409 if code.endswith("MISMATCH") else 400
            raise ApiError(code, "The version-bound transcript could not be read for its current source Material", status) from exc
        words = result["words"]
        return _success({
            "project_id": project_id,
            "project_version": version,
            "project_state_sha256": hashlib.sha256(
                transcript_version_row["state_json"].encode("utf-8")
            ).hexdigest(),
            "timeline_digest": result["timeline_digest"],
            "source_material_refs": sorted({
                item.source_material_id for item in timeline.assets
                if item.source_material_id and any(doc["source_asset_id"] == item.asset_id
                                                   for doc in result["transcripts"])
            }),
            "transcripts": result["transcripts"],
            "words": words[offset:offset + limit],
            "word_count": len(words),
            "offset": offset,
            "limit": limit,
            "external_fetch": False,
            "source_media_modified": False,
        })

    @app.post("/api/media/projects/<project_id>/word-cut-previews")
    def media_preview_word_cuts(project_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        if project["work_id"]:
            _require_active_work(store, workspace_id, project["work_id"])
        data = _body()
        allowed = {"version", "source_selections", "synchronize", "context_before_sec", "context_after_sec"}
        if set(data) - allowed or not {"version", "source_selections"} <= set(data):
            raise ApiError("INVALID_INPUT", "Word-cut preview fields are invalid", 400)
        version = data["version"]
        synchronize = data.get("synchronize", True)
        if type(version) is not int or version < 1 or type(synchronize) is not bool:
            raise ApiError("INVALID_INPUT", "Word-cut preview version or synchronization choice is invalid", 400)
        raw_selections = data["source_selections"]
        if not isinstance(raw_selections, list) or not 1 <= len(raw_selections) <= 256:
            raise ApiError("MEDIA_TRANSCRIPT_SELECTION_INVALID", "Select at least one timed word instance", 400)
        try:
            context_before = _fraction_time(data.get("context_before_sec", 1),
                                            error_code="MEDIA_TRANSCRIPT_PREVIEW_CONTEXT_INVALID")
            context_after = _fraction_time(data.get("context_after_sec", 1),
                                           error_code="MEDIA_TRANSCRIPT_PREVIEW_CONTEXT_INVALID")
        except VideoEditingError as exc:
            raise ApiError("MEDIA_TRANSCRIPT_PREVIEW_CONTEXT_INVALID", "Preview context must use valid seconds", 400) from exc
        if not 0 <= context_before <= 30 or not 0 <= context_after <= 30:
            raise ApiError("MEDIA_TRANSCRIPT_PREVIEW_CONTEXT_INVALID", "Preview context is limited to 30 seconds on each side", 400)
        state, version_row = _read_version_state(store, workspace_id, project_id, version)
        timeline, _availability = _load_timeline(
            store, workspace_id, state, verify_files=True, project_id=project_id,
        )
        readback = timeline.read_transcript_words()
        word_rows = readback["words"]
        transcript_hashes = {
            (item["source_asset_id"], item["audio_stream_index"]): item["transcript_sha256"]
            for item in readback["transcripts"]
        }
        normalized_selections = []
        selected_words = []
        cuts = []
        seen_instances = set()
        selected_clip_ids = set()
        for selection in raw_selections:
            required = {"transcript_asset_id", "expected_transcript_sha256", "word_ids", "clip_ids"}
            if type(selection) is not dict or set(selection) != required:
                raise ApiError("MEDIA_TRANSCRIPT_SELECTION_INVALID", "Each word selection must bind its source transcript and clip occurrences", 400)
            asset_id = selection["transcript_asset_id"]
            digest = selection["expected_transcript_sha256"]
            word_ids = selection["word_ids"]
            clip_ids = selection["clip_ids"]
            if (not isinstance(asset_id, str) or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not isinstance(word_ids, list) or not word_ids or len(word_ids) > 100000
                    or any(not isinstance(item, str) or not item for item in word_ids)
                    or len(set(word_ids)) != len(word_ids)
                    or not isinstance(clip_ids, list) or not clip_ids
                    or any(not isinstance(item, str) or not item for item in clip_ids)
                    or len(set(clip_ids)) != len(clip_ids)):
                raise ApiError("MEDIA_TRANSCRIPT_SELECTION_INVALID", "Word ids and clip ids must be unique nonempty selections", 400)
            matching_transcripts = [
                item for item in readback["transcripts"]
                if item["source_asset_id"] == asset_id and item["transcript_sha256"] == digest
            ]
            if not matching_transcripts:
                raise ApiError("MEDIA_TRANSCRIPT_STALE", "The selected transcript no longer matches this source version", 409,
                               {"source_asset_id": asset_id})
            if any(item.get("task", "transcribe") == "translate" for item in matching_transcripts):
                raise ApiError("MEDIA_TRANSLATION_WORD_CUT_UNSUPPORTED", "Translated words do not provide source-word timing for an audio cut", 409,
                               {"source_asset_id": asset_id})
            transcript_streams = {item["audio_stream_index"] for item in matching_transcripts}
            normalized_selections.append(dict(selection))
            matched_clip_ids = set()
            for word_id in word_ids:
                occurrences = [row for row in word_rows
                               if row["source_asset_id"] == asset_id
                               and row["transcript_sha256"] == digest
                               and row["word_id"] == word_id
                               and row["clip_id"] in clip_ids]
                if not occurrences:
                    known_word = any(row["source_asset_id"] == asset_id
                                     and row["transcript_sha256"] == digest
                                     and row["word_id"] == word_id for row in word_rows)
                    code = "MEDIA_TRANSCRIPT_WORD_NOT_IN_SELECTED_CLIP" if known_word else "MEDIA_TRANSCRIPT_WORD_NOT_FOUND"
                    raise ApiError(code, "The selected word has no matching occurrence in the chosen clip", 409,
                                   {"source_asset_id": asset_id, "word_id": word_id})
                if len(occurrences) > len(clip_ids):
                    raise ApiError("MEDIA_TRANSCRIPT_OCCURRENCE_AMBIGUOUS", "Choose exact clip occurrences for repeated words", 409,
                                   {"source_asset_id": asset_id, "word_id": word_id})
                for row in occurrences:
                    instance_key = (asset_id, digest, word_id, row["clip_id"])
                    if instance_key in seen_instances:
                        raise ApiError("MEDIA_TRANSCRIPT_SELECTION_DUPLICATE", "The same word occurrence was selected twice", 400)
                    seen_instances.add(instance_key)
                    matched_clip_ids.add(row["clip_id"])
                    selected_clip_ids.add(row["clip_id"])
                    selected_words.append(row)
                    cuts.append({
                        "clip_id": row["clip_id"],
                        "source_in": row["source_start_sec"],
                        "source_out": row["source_end_sec"],
                    })
            if not set(clip_ids) <= matched_clip_ids:
                raise ApiError("MEDIA_TRANSCRIPT_CLIP_SELECTION_INVALID", "A selected clip does not contain one of these timed words", 400,
                               {"source_asset_id": asset_id})
            if len(transcript_streams) > 1:
                raise ApiError("MEDIA_TRANSCRIPT_STREAM_SELECTION_INVALID", "Select a transcript hash bound to one audio stream", 409)

        try:
            candidate = timeline.cut_source_intervals(cuts, synchronize=synchronize)
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_TRANSCRIPT_CUT_INVALID"
            raise ApiError(code, "The selected sample-clock word ranges could not be applied to a candidate timeline", 409) from exc

        removed_intervals = []
        for row in selected_words:
            removed_intervals.append({
                "source_asset_id": row["source_asset_id"],
                "source_sha256": row["source_sha256"],
                "transcript_id": row["transcript_id"],
                "transcript_sha256": row["transcript_sha256"],
                "audio_stream_index": row["audio_stream_index"],
                "word_id": row["word_id"],
                "clip_id": row["clip_id"],
                "source_start_sample": row["source_start_sample"],
                "source_end_sample": row["source_end_sample"],
                "source_in": row["source_start_sec"],
                "source_out": row["source_end_sec"],
                "timeline_start_sec": row["timeline_start_sec"],
                "timeline_end_sec": row["timeline_end_sec"],
            })
        timeline_intervals = sorted(
            (_fraction_time(row["timeline_start_sec"]), _fraction_time(row["timeline_end_sec"]))
            for row in selected_words
        )
        merged_intervals = []
        for begin, end in timeline_intervals:
            if merged_intervals and begin <= merged_intervals[-1][1]:
                merged_intervals[-1] = (merged_intervals[-1][0], max(merged_intervals[-1][1], end))
            else:
                merged_intervals.append((begin, end))
        base_start = max(Fraction(0), min(item[0] for item in merged_intervals) - context_before)
        base_end = min(timeline.duration_sec, max(item[1] for item in merged_intervals) + context_after)
        if synchronize:
            def shifted(point: Fraction) -> Fraction:
                removed = Fraction(0)
                for begin, end in merged_intervals:
                    if point <= begin:
                        break
                    removed += min(point, end) - begin
                    if point < end:
                        break
                return point - removed

            candidate_start, candidate_end = shifted(base_start), shifted(base_end)
        else:
            candidate_start, candidate_end = base_start, base_end
        candidate_end = min(candidate_end, candidate.duration_sec)
        if candidate_start >= candidate_end:
            raise ApiError("MEDIA_TRANSCRIPT_PREVIEW_RANGE_REMOVED", "The selected cut removes its entire preview context; widen the context and retry", 400)
        try:
            comparison = preview_audio_timeline_change(
                timeline, candidate,
                base_start_sec=base_start, base_end_sec=base_end,
                candidate_start_sec=candidate_start, candidate_end_sec=candidate_end,
            )
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_AUDIO_PREVIEW_FAILED"
            status = 503 if code == "MEDIA_RENDER_TOOLS_UNAVAILABLE" else 409 if code == "SOURCE_MEDIA_CHANGED" else 400
            raise ApiError(code, "The candidate word-cut audio could not be decoded; the saved project remains unchanged", status) from exc
        return _success({
            "project_id": project_id,
            "project_version": version,
            "project_state_sha256": hashlib.sha256(version_row["state_json"].encode("utf-8")).hexdigest(),
            "timeline_digest": timeline._view_digest(),
            "transcript_edit": {
                "state": "candidate",
                "edit_kind": "cut_original",
                "source_selections": normalized_selections,
                "selected_words": selected_words,
                "word_mappings": selected_words,
                "removed_source_intervals": removed_intervals,
                "cuts": cuts,
                "synchronize": synchronize,
                "preview_timeline": candidate.to_dict(),
                "parent_timeline_version": timeline.version,
                "parent_timeline_digest": timeline._view_digest(),
                "automatic_application": False,
                "requires_user_review": True,
            },
            "audio_comparison": comparison,
            "automatic_application": False,
            "persisted": False,
            "export_performed": False,
            "external_fetch": False,
        })

    @app.get("/api/media/speech/capabilities")
    def media_speech_read_capabilities():
        _workspace_id()
        return _success(speech_capabilities())

    @app.post("/api/media/speech/synthesize")
    def media_speech_synthesize():
        _require_write()
        data = _body()
        if set(data) - {"engine", "text", "voice", "language"} or not {"text", "voice", "language"} <= set(data):
            raise ApiError("INVALID_INPUT", "Speech synthesis requires text, voice and language, with an optional local engine", 400)
        engine = data.get("engine", "ffmpeg_libflite")
        if (engine not in {"ffmpeg_libflite", "espeak_ng"}
                or not isinstance(data["text"], str) or not isinstance(data["voice"], str)
                or not isinstance(data["language"], str)):
            raise ApiError("INVALID_INPUT", "Speech synthesis engine, text, voice or language is invalid", 400)
        try:
            if engine == "ffmpeg_libflite":
                result = synthesize_flite_speech(data["text"], voice=data["voice"], language=data["language"])
            else:
                result = synthesize_espeak_speech(data["text"], voice=data["voice"], language=data["language"])
        except MediaSpeechError as exc:
            code = exc.code
            status = 503 if code in {
                "MEDIA_TTS_RUNTIME_UNAVAILABLE", "MEDIA_TTS_PROCESS_FAILED",
                "MEDIA_TTS_SYNTHESIS_FAILED", "MEDIA_TTS_DECODE_MISMATCH",
                "MEDIA_TTS_OUTPUT_INVALID", "MEDIA_TTS_ESPEAK_RUNTIME_UNAVAILABLE",
                "MEDIA_TTS_ESPEAK_DATA_UNAVAILABLE", "MEDIA_TTS_ESPEAK_RUNTIME_INTEGRITY_ERROR",
                "MEDIA_TTS_ESPEAK_LICENSES_UNAVAILABLE", "MEDIA_TTS_ESPEAK_ASCII_PATH_REQUIRED",
                "MEDIA_TTS_ESPEAK_RUNTIME_FAILED", "MEDIA_TTS_ESPEAK_NO_TEMPORARY_PATH_ALIAS",
                "MEDIA_TTS_ESPEAK_PATH_ALIAS_FAILED", "MEDIA_TTS_ESPEAK_PATH_ALIAS_RELEASE_FAILED",
            } else 400
            messages = {
                "MEDIA_TTS_RUNTIME_UNAVAILABLE": "The approved local English speech runtime is unavailable.",
                "MEDIA_TTS_ESPEAK_RUNTIME_UNAVAILABLE": "The approved local multilingual speech runtime is unavailable.",
                "MEDIA_TTS_ESPEAK_LICENSES_UNAVAILABLE": "The local multilingual voice license files failed integrity verification.",
                "MEDIA_TTS_ESPEAK_ASCII_PATH_REQUIRED": "The current platform cannot provide the local voice runtime path safely.",
                "MEDIA_TTS_ESPEAK_RUNTIME_INTEGRITY_ERROR": "The local multilingual speech binary or voice data changed; no audio was generated.",
                "MEDIA_TTS_ESPEAK_RUNTIME_FAILED": "The local multilingual speech runtime could not load its pinned voices.",
                "MEDIA_TTS_LANGUAGE_UNAVAILABLE": "This local speech voice supports ASCII English only; the complete text was left unchanged.",
                "MEDIA_TTS_ESPEAK_PATH_ALIAS_FAILED": "The local multilingual voice runtime could not open its product-local paths.",
                "MEDIA_TTS_ESPEAK_PATH_ALIAS_RELEASE_FAILED": "The temporary local runtime path could not be released safely.",
                "MEDIA_TTS_ESPEAK_NO_TEMPORARY_PATH_ALIAS": "No temporary path is available to open the local multilingual voice runtime.",
                "MEDIA_TTS_VOICE_UNAVAILABLE": "The selected local speech voice is unavailable.",
                "MEDIA_TTS_TEXT_INVALID": "The speech text is empty, too long or contains unsupported control characters.",
                "MEDIA_TTS_OUTPUT_LIMIT_EXCEEDED": "The complete utterance exceeds the current local speech limit; no truncated audio was returned.",
                "MEDIA_TTS_AUDIO_SILENT": "The selected voice produced no audible samples.",
            }
            if engine == "espeak_ng":
                messages["MEDIA_TTS_LANGUAGE_UNAVAILABLE"] = "The pinned local runtime does not have a voice for this exact language selection."
            raise ApiError(code, messages.get(code, "The local speech result failed verification."), status) from exc
        return _success({
            "audio_base64": base64.b64encode(result["audio"]).decode("ascii"),
            "report": result["report"],
            "persisted": False,
            "material_created": False,
            "project_changed": False,
        })

    @app.post("/api/media/projects/<project_id>/transcriptions")
    def media_start_transcription(project_id: str):
        return _enqueue_media_transcription(store, _require_write(), project_id, _body())

    @app.get("/api/media/transcriptions/<transcription_id>")
    def media_get_transcription(transcription_id: str):
        workspace_id = _workspace_id()
        try:
            offset = int(request.args.get("word_offset", "0"))
            limit = int(request.args.get("word_limit", "500"))
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "Transcription word page must use integer offset and limit", 400) from exc
        if offset < 0 or not 1 <= limit <= 2000:
            raise ApiError("INVALID_INPUT", "Transcription word page is out of range", 400)
        return _media_transcription_view(store, workspace_id, transcription_id, word_offset=offset, word_limit=limit)

    @app.post("/api/media/transcriptions/<transcription_id>/resume")
    def media_resume_transcription(transcription_id: str):
        return _resume_media_transcription(store, _require_write(), transcription_id)

    @app.post("/api/media/projects/<project_id>/transcriptions/<transcription_id>/adopt")
    def media_adopt_transcription(project_id: str, transcription_id: str):
        return _adopt_media_transcription(store, _require_write(), project_id, transcription_id, _body())

    @app.get("/api/media/projects/<project_id>/sources/<asset_id>/waveform")
    def media_read_project_source_waveform(project_id: str, asset_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected_version = request.args.get("version")
        if selected_version is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected_version)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=False, project_id=project_id,
        )
        try:
            asset = timeline._asset(asset_id)
        except VideoEditingError as exc:
            raise _not_found() from exc

        def query_integer(name: str, default: int | None = None) -> int | None:
            value = request.args.get(name)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", f"{name} must be an integer", 400) from exc

        try:
            start = _fraction_time(request.args.get("start_sec", "0"), error_code="WAVEFORM_RANGE_INVALID")
            raw_end = request.args.get("end_sec")
            end = _fraction_time(raw_end, error_code="WAVEFORM_RANGE_INVALID") if raw_end is not None else None
        except VideoEditingError as exc:
            raise ApiError("WAVEFORM_RANGE_INVALID", "Waveform source range is invalid", 400) from exc
        audio_stream_index = query_integer("audio_stream_index")
        video_stream_index = query_integer("video_stream_index")
        sample_rate = query_integer("sample_rate")
        channel_index = query_integer("channel_index")
        bins = query_integer("bins", 256)
        try:
            waveform = extract_waveform(
                asset,
                start_sec=start,
                end_sec=end,
                bins=bins,
                audio_stream_index=audio_stream_index,
                sample_rate=sample_rate,
                channel_index=channel_index,
                video_stream_index=video_stream_index,
            )
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "WAVEFORM_FAILED"
            status = (503 if code == "MEDIA_RENDER_TOOLS_UNAVAILABLE" else
                      415 if code == "WAVEFORM_AUDIO_REQUIRED" else
                      409 if code in {"SOURCE_MEDIA_CHANGED", "SOURCE_MEDIA_NOT_READABLE"} else 400)
            raise ApiError(code, "The source waveform could not be measured", status) from exc
        return _success({"project_id": project_id, "project_version": version,
                         "asset_id": asset_id, "waveform": waveform})

    @app.get("/api/media/projects/<project_id>/subtitles/<subtitle_format>")
    def media_export_subtitles(project_id: str, subtitle_format: str):
        if subtitle_format not in {"srt", "vtt", "ass"}:
            raise ApiError("SUBTITLE_FORMAT_NOT_SUPPORTED", "Choose SRT, WebVTT or ASS", 415)
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected_version = request.args.get("version")
        if selected_version is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected_version)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        raw_num = request.args.get("frame_rate_num")
        raw_den = request.args.get("frame_rate_den")
        if (raw_num is None) != (raw_den is None):
            raise ApiError("SUBTITLE_FRAME_RATE_INVALID", "Provide both frame_rate_num and frame_rate_den", 400)
        if raw_num is None:
            frame_rate = Fraction(30, 1)
        else:
            try:
                numerator, denominator = int(raw_num), int(raw_den)
                frame_rate = Fraction(numerator, denominator)
            except (ValueError, ZeroDivisionError) as exc:
                raise ApiError("SUBTITLE_FRAME_RATE_INVALID", "Subtitle frame rate is invalid", 400) from exc
            if not 1 <= frame_rate <= 120:
                raise ApiError("SUBTITLE_FRAME_RATE_INVALID", "Subtitle frame rate must be between 1 and 120", 400)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=False, project_id=project_id,
        )
        try:
            documents = subtitle_documents(timeline, frame_rate=frame_rate)
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "SUBTITLE_EXPORT_FAILED"
            raise ApiError(code, "The saved captions could not be exported for this project version", 409) from exc
        filename = f"captions.{subtitle_format}"
        document = documents.get(filename)
        if document is None:
            raise ApiError("MEDIA_SUBTITLES_UNAVAILABLE", "This project version has no exportable captions", 409)
        content = document.encode("utf-8")
        response = make_response(content)
        response.headers["Content-Type"] = {
            "ass": "text/x-ssa; charset=utf-8",
            "srt": "application/x-subrip; charset=utf-8",
            "vtt": "text/vtt; charset=utf-8",
        }[subtitle_format]
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["ETag"] = f'"{hashlib.sha256(content).hexdigest()}"'
        response.headers["X-Media-Project-Version"] = str(version)
        response.headers["X-Media-Frame-Rate"] = f"{frame_rate.numerator}/{frame_rate.denominator}"
        response.headers["X-Media-Subtitle-Style-Preservation"] = (
            "bounded-caption-style-preserved" if subtitle_format == "ass" else "styles-not-preserved"
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/media/projects/<project_id>/chapters")
    def media_get_chapters(project_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected = request.args.get("version")
        if selected is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=False, project_id=project_id,
        )
        try:
            document = chapter_document(timeline)
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_CHAPTER_EXPORT_FAILED"
            raise ApiError(code, "The saved chapter positions do not match this programme version", 409) from exc
        if not document:
            raise ApiError("MEDIA_CHAPTERS_UNAVAILABLE", "This project version has no chapter markers", 409)
        return _success(document)

    @app.get("/api/media/projects/<project_id>/chapters/file")
    def media_export_chapter_file(project_id: str):
        workspace_id = _workspace_id()
        project = _project_row(store, workspace_id, project_id)
        selected = request.args.get("version")
        if selected is None:
            version = project["current_version"]
        else:
            try:
                version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
            if version < 1:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=False, project_id=project_id,
        )
        try:
            document = chapter_document(timeline)
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_CHAPTER_EXPORT_FAILED"
            raise ApiError(code, "The saved chapter positions do not match this programme version", 409) from exc
        if not document:
            raise ApiError("MEDIA_CHAPTERS_UNAVAILABLE", "This project version has no chapter markers", 409)
        content = _json(document).encode("utf-8")
        response = make_response(content)
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = 'attachment; filename="chapters.json"'
        response.headers["ETag"] = f'"{hashlib.sha256(content).hexdigest()}"'
        response.headers["X-Media-Project-Version"] = str(version)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.patch("/api/media/projects/<project_id>")
    def media_rename_project(project_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version", "title"}:
            raise ApiError("INVALID_INPUT", "Rename requires base_version and title", 400)
        base_version = data["base_version"]
        if type(base_version) is not int or base_version < 1:
            raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
        title = _string(data["title"], "title", maximum=200)
        key = _required_idempotency_key()
        fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version, "title": title})
        workspace_id = identity["workspace"]["id"]
        prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
        if prior_version is not None:
            return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
        project = _project_row(store, workspace_id, project_id)
        if project["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                           {"current_version": project["current_version"]})
        if project["work_id"]:
            _require_active_work(store, workspace_id, project["work_id"])
        state, version_row = _read_version_state(store, workspace_id, project_id, base_version)
        state["title"] = title
        timeline = Timeline.from_dict(
            state["timeline"],
            assets=_load_timeline(store, workspace_id, state, project_id=project_id)[0].assets,
            verify_local_assets=False,
        )
        state["timeline"] = timeline.to_dict()
        undo_stack = _version_stack(version_row, "undo_stack_json") + [base_version]
        new_version, _ = _append_project_version(
            store, identity, project_id, base_version=base_version, state=state,
            operation="rename", idempotency_key=key, request_hash=fingerprint,
            undo_stack=undo_stack, redo_stack=[],
        )
        return _success(_project_view(store, workspace_id, project_id, new_version))

    @app.post("/api/media/projects/<project_id>/sources")
    def media_add_project_source(project_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version", "material_id"}:
            raise ApiError("INVALID_INPUT", "Adding a source requires base_version and material_id", 400)
        base_version, material_id = data["base_version"], data["material_id"]
        if type(base_version) is not int or base_version < 1 or not isinstance(material_id, str):
            raise ApiError("INVALID_INPUT", "Media source input is invalid", 400)
        key = _required_idempotency_key()
        workspace_id = identity["workspace"]["id"]
        fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version, "material_id": material_id})
        prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
        if prior_version is not None:
            return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
        project = _project_row(store, workspace_id, project_id)
        if project["current_version"] != base_version:
            raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                           {"current_version": project["current_version"]})
        if project["work_id"]:
            _require_active_work(store, workspace_id, project["work_id"])
        _require_media_material_access(
            store, identity, material_id, target_work_id=project["work_id"],
        )
        material, path = _store_material_source(store, workspace_id, material_id, verify=False)
        asset = _new_media_asset(path, material)
        state, version_row = _read_version_state(store, workspace_id, project_id, base_version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, project_id=project_id,
        )
        if any(item.source_material_id == material_id for item in timeline.assets):
            raise ApiError("MEDIA_SOURCE_ALREADY_ATTACHED", "This Material is already attached to the project", 409)
        try:
            updated = timeline.add_asset(asset)
        except VideoEditingError as exc:
            raise ApiError("MEDIA_SOURCE_INVALID", "The Material could not be added to this Media project", 400) from exc
        next_state = _copy_project_state(state, updated)
        undo_stack = _version_stack(version_row, "undo_stack_json") + [base_version]
        new_version, _ = _append_project_version(
            store, identity, project_id, base_version=base_version, state=next_state,
            operation="add_source", idempotency_key=key, request_hash=fingerprint,
            undo_stack=undo_stack, redo_stack=[],
        )
        return _success(_project_view(store, workspace_id, project_id, new_version))

    @app.post("/api/media/projects/<project_id>/edits")
    def media_edit_project(project_id: str):
        identity = _require_write()
        data = _body()
        return _success(
            revise_media_project(store, identity, project_id, data, _required_idempotency_key()),
        )

    @app.post("/api/media/projects/<project_id>/undo")
    def media_undo_project(project_id: str):
        return _move_project_history(store, _require_write(), project_id, _body(), direction="undo")

    @app.post("/api/media/projects/<project_id>/redo")
    def media_redo_project(project_id: str):
        return _move_project_history(store, _require_write(), project_id, _body(), direction="redo")

    @app.post("/api/media/projects/<project_id>/restore")
    def media_restore_project(project_id: str):
        return _restore_project_version(store, _require_write(), project_id, _body())

    @app.post("/api/media/projects/<project_id>/renders")
    def media_start_render(project_id: str):
        return _enqueue_media_render(store, _require_write(), project_id, _body())

    @app.post("/api/media/projects/<project_id>/previews")
    def media_start_preview(project_id: str):
        return _enqueue_media_render(store, _require_write(), project_id, _body(), purpose="preview")

    @app.post("/api/media/projects/<project_id>/audio-previews")
    def media_preview_audio(project_id: str):
        data = _body()
        allowed = {"version", "start_sec", "end_sec", "render_options"}
        if set(data) - allowed or not {"version", "start_sec", "end_sec"} <= set(data):
            raise ApiError("INVALID_INPUT", "Audio preview requires a version and an exact time range", 400)
        version = data["version"]
        if type(version) is not int or version < 1:
            raise ApiError("INVALID_INPUT", "version must be a positive integer", 400)
        try:
            start = _fraction_time(data["start_sec"], error_code="AUDIO_COMPARISON_RANGE_INVALID")
            end = _fraction_time(data["end_sec"], error_code="AUDIO_COMPARISON_RANGE_INVALID")
        except VideoEditingError as exc:
            raise ApiError("AUDIO_COMPARISON_RANGE_INVALID", "Audio preview time range is invalid", 400) from exc
        if start < 0 or end <= start or end - start > 60:
            raise ApiError("AUDIO_COMPARISON_RANGE_INVALID", "Audio preview range must be at most 60 seconds", 400)
        options = data.get("render_options")
        if options is not None and not isinstance(options, dict):
            raise ApiError("INVALID_INPUT", "render_options must be an object", 400)
        workspace_id = _workspace_id()
        _project_row(store, workspace_id, project_id)
        state, _ = _read_version_state(store, workspace_id, project_id, version)
        timeline, _ = _load_timeline(
            store, workspace_id, state, verify_files=True, project_id=project_id,
        )
        try:
            preview = preview_audio_processing(
                timeline, start_sec=start, end_sec=end, render_options=options,
            )
        except VideoEditingError as exc:
            code = exc.args[0] if exc.args else "MEDIA_AUDIO_PREVIEW_FAILED"
            status = 503 if code == "MEDIA_RENDER_TOOLS_UNAVAILABLE" else 409 if code == "SOURCE_MEDIA_CHANGED" else 400
            raise ApiError(code, "The audio comparison preview could not be completed; the saved project was preserved.", status) from exc
        return _success({
            "project_id": project_id,
            "version": version,
            "state": preview["state"],
            "base_digest": preview["base_digest"],
            "start_sec": _time_json(start),
            "end_sec": _time_json(end),
            "programme_duration_sec": _time_json(preview["programme_duration_sec"]),
            "sample_rate": preview["sample_rate"],
            "channels": preview["channels"],
            "sample_format": preview["sample_format"],
            "render_options": preview["render_options"],
            "samples": preview["samples"],
            "audio_source_clock_mappings": preview["audio_source_clock_mappings"],
            "programme_measurements": preview["programme_measurements"],
            "persisted": False,
            "export_performed": False,
            "user_adoption_required": True,
        })

    @app.get("/api/media/renders/<render_id>")
    def media_get_render(render_id: str):
        workspace_id = _workspace_id()
        with store.transaction(immediate=False) as connection:
            row = _require_media_render_access_in_transaction(
                connection, g.identity, render_id, expected_job_kind="media_render",
            )
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND workspace_id = ?",
                (row["job_id"], workspace_id),
            ).fetchone()
        return _success(_render_view(row, job))

    @app.get("/api/media/previews/<preview_id>")
    def media_get_preview(preview_id: str):
        workspace_id = _workspace_id()
        with store.transaction(immediate=False) as connection:
            row = _require_media_render_access_in_transaction(
                connection, g.identity, preview_id, expected_job_kind="media_preview",
            )
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND workspace_id = ?",
                (row["job_id"], workspace_id),
            ).fetchone()
        return _success(_render_view(row, job))

    @app.get("/api/media/renders/<render_id>/file")
    def media_download_render(render_id: str):
        return _media_output_file(store, g.identity, render_id, expected_job_kind="media_render", inline=False)

    @app.get("/api/media/renders/<render_id>/podcast-sidecars")
    def media_download_podcast_sidecars(render_id: str):
        selected = request.args.get("project_version")
        if selected is None:
            sidecar_version = None
        else:
            try:
                sidecar_version = int(selected)
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "project_version must be a positive integer", 400) from exc
        bundle = resolve_media_episode_sidecars(
            store, g.identity, render_id, project_version=sidecar_version,
        )
        response = send_file(io.BytesIO(bundle["content"]), as_attachment=True,
                             download_name=bundle["filename"], mimetype=bundle["mime_type"],
                             conditional=False, max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["ETag"] = f'"{bundle["sha256"]}"'
        response.headers["X-Media-Episode-Ref"] = bundle["episode_ref"]
        response.headers["X-Media-Render-ID"] = bundle["audio_revision"]["render_id"]
        response.headers["X-Media-Audio-Project-Version"] = str(bundle["audio_revision"]["project_version"])
        response.headers["X-Media-Sidecar-Project-Version"] = str(bundle["project_version"])
        return response

    @app.get("/api/media/previews/<preview_id>/file")
    def media_play_preview(preview_id: str):
        return _media_output_file(store, g.identity, preview_id, expected_job_kind="media_preview", inline=True)

    _register_media_upload_routes(app, store)


def _move_project_history(store, identity: dict, project_id: str, data: dict, *, direction: str):
    if set(data) != {"base_version"}:
        raise ApiError("INVALID_INPUT", f"{direction} requires base_version", 400)
    base_version = data["base_version"]
    if type(base_version) is not int or base_version < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    key = _required_idempotency_key()
    workspace_id = identity["workspace"]["id"]
    fingerprint = _fingerprint({"project_id": project_id, "base_version": base_version, "direction": direction})
    prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
    if prior_version is not None:
        return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    current_state, current_version = _read_version_state(store, workspace_id, project_id, base_version)
    undo_stack = _version_stack(current_version, "undo_stack_json")
    redo_stack = _version_stack(current_version, "redo_stack_json")
    if direction == "undo":
        if not undo_stack:
            raise ApiError("MEDIA_UNDO_UNAVAILABLE", "There is no saved edit to undo", 409)
        target_version = undo_stack.pop()
        redo_stack.append(base_version)
    else:
        if not redo_stack:
            raise ApiError("MEDIA_REDO_UNAVAILABLE", "There is no edit available to redo", 409)
        target_version = redo_stack.pop()
        undo_stack.append(base_version)
    state, _ = _read_version_state(store, workspace_id, project_id, target_version)
    new_version, _ = _append_project_version(
        store, identity, project_id, base_version=base_version, state=state,
        operation=direction, idempotency_key=key, request_hash=fingerprint,
        undo_stack=undo_stack, redo_stack=redo_stack,
    )
    return _success(_project_view(store, workspace_id, project_id, new_version))


def _restore_project_version(store, identity: dict, project_id: str, data: dict):
    if set(data) != {"base_version", "restore_version"}:
        raise ApiError("INVALID_INPUT", "Restore requires base_version and restore_version", 400)
    base_version, restore_version = data["base_version"], data["restore_version"]
    if any(type(value) is not int or value < 1 for value in (base_version, restore_version)):
        raise ApiError("INVALID_INPUT", "Version values must be positive integers", 400)
    key = _required_idempotency_key()
    workspace_id = identity["workspace"]["id"]
    fingerprint = _fingerprint({
        "project_id": project_id, "base_version": base_version,
        "restore_version": restore_version,
    })
    prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
    if prior_version is not None:
        return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "Media project changed since it was opened", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    target_state, _ = _read_version_state(store, workspace_id, project_id, restore_version)
    _, current_version = _read_version_state(store, workspace_id, project_id, base_version)
    undo_stack = _version_stack(current_version, "undo_stack_json") + [base_version]
    new_version, _ = _append_project_version(
        store, identity, project_id, base_version=base_version, state=target_state,
        operation="restore", idempotency_key=key, request_hash=fingerprint,
        undo_stack=undo_stack, redo_stack=[],
    )
    return _success(_project_view(store, workspace_id, project_id, new_version))


def _render_view(row, job) -> dict:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    purpose = "preview" if job["kind"] == "media_preview" else "render"
    status = row["status"]
    if status != "completed":
        job_status = job["status"]
        if job_status in {"queued", "running", "cancel_requested", "cancelled", "failed", "interrupted"}:
            if job_status != "running" or status not in {"running", "writing"}:
                status = job_status
    file_url = None
    if status == "completed":
        route = "previews" if purpose == "preview" else "renders"
        file_url = f"/api/media/{route}/{row['id']}/file"
    error_code = row["error_code"] or job["error_code"]
    error_message = row["error_message"] or job["error_message"]
    return {
        "id": row["id"], "project_id": row["project_id"],
        "version": row["project_version"], "job": _media_job_data(job),
        "purpose": purpose,
        "status": status, "output_kind": row["output_kind"], "format": row["format"],
        "sha256": row["sha256"], "size_bytes": row["size_bytes"],
        "metadata": metadata,
        "download_url": file_url,
        "playback_url": file_url if purpose == "preview" else None,
        "error": None if error_code is None else {
            "code": error_code, "message": error_message or "Media render failed",
        },
    }


def _media_output_file(store, identity: dict, output_id: str, *, expected_job_kind: str, inline: bool):
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        row = _require_media_render_access_in_transaction(
            connection, identity, output_id, expected_job_kind=expected_job_kind,
        )
        if row["status"] != "completed":
            raise _not_found()
    path = _resource_path(store, row["relative_path"])
    if not path.is_file() or path.is_symlink():
        raise ApiError("MEDIA_RENDER_FILE_UNAVAILABLE", "The rendered file is unavailable", 409)
    initial_size = path.stat().st_size
    digest = sha256_media_file(path)
    final_size = path.stat().st_size
    if (initial_size != final_size or final_size != row["size_bytes"]
            or digest != row["sha256"]):
        raise ApiError("MEDIA_RENDER_INTEGRITY_ERROR", "The rendered file failed integrity verification", 409)
    response = send_file(
        path,
        as_attachment=not inline,
        download_name=path.name,
        mimetype=_MEDIA_OUTPUT_MIME_TYPES.get(row["format"], "application/octet-stream"),
        conditional=True,
        etag=digest,
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _render_spec_api_error(exc: VideoEditingError, *, fallback_message: str) -> ApiError:
    code = exc.args[0] if exc.args else "RENDER_OUTPUT_SPEC_INVALID"
    messages = {
        "MEDIA_HDR_RENDER_UNSUPPORTED": "The original HDR Material is preserved, but this renderer cannot export its HDR transfer function yet.",
        "MEDIA_HIGH_BIT_DEPTH_RENDER_UNSUPPORTED": "The original high bit-depth Material is preserved; this renderer currently exports 8-bit video.",
        "MEDIA_VIDEO_PIXEL_FORMAT_UNSUPPORTED": "The original Material uses a pixel format this renderer cannot preserve; no output was created.",
        "MEDIA_COLOR_SIGNAL_UNKNOWN": "Color primaries, transfer, matrix or range are unknown; choose a source interpretation and working space before rendering. Original source tags remain unchanged.",
        "MEDIA_COLOR_RANGE_UNSUPPORTED": "The source color range is not supported by this renderer; the original Material is unchanged.",
        "MEDIA_COLOR_PROFILE_UNSUPPORTED": "The source color profile is not supported by this renderer; the original Material is unchanged.",
        "MEDIA_COLOR_PROFILE_MISMATCH": "This project mixes source profiles without one selected working space; choose an output working space before rendering.",
        "MEDIA_COLOR_TRANSFORM_UNSUPPORTED": "The requested output profile would require an unsupported color transform; the project and originals are unchanged.",
        "MEDIA_IMAGE_COLOR_PROFILE_UNSUPPORTED": "This still image has an embedded or tagged color profile this renderer cannot convert safely; the original Material is preserved.",
        "MEDIA_IMAGE_PIXEL_FORMAT_UNSUPPORTED": "This still image pixel format is not supported by the current 8-bit SDR compositor; the original Material is preserved.",
        "VIDEO_COLOR_INTERPRETATION_SOURCE_UNSUPPORTED": "Manual color interpretation is limited to verified 8-bit SDR yuv420p video sources.",
        "VIDEO_COLOR_HDR_INTERPRETATION_FORBIDDEN": "A detected HDR transfer function cannot be overridden by an SDR interpretation.",
        "VIDEO_COLOR_HDR_STATE_UNKNOWN": "The source's BT.2020 primaries have no transfer tag, so its SDR or HDR state cannot be safely interpreted.",
        "VIDEO_COLOR_INTERPRETATION_CONFLICTS_SOURCE_METADATA": "The selected interpretation conflicts with source tags that were present in the original Material.",
        "VIDEO_COLOR_WORKING_SPACE_MISMATCH": "Use one working color space for all explicit source interpretations in this project.",
    }
    return ApiError(code, messages.get(code, fallback_message), 400)


def _enqueue_media_render(store, identity: dict, project_id: str, data: dict, *, purpose: str = "render"):
    if purpose not in {"render", "preview"}:
        raise ValueError("Unsupported Media job purpose")
    job_kind = "media_preview" if purpose == "preview" else "media_render"
    allowed = {"version", "output_kind", "format", "output_spec", "render_options"}
    if set(data) - allowed or not {"version", "output_kind"} <= set(data):
        raise ApiError("INVALID_INPUT", f"{purpose.title()} request fields are invalid", 400)
    version = data["version"]
    output_kind = data["output_kind"]
    if type(version) is not int or version < 1 or output_kind not in {"video", "audio"}:
        raise ApiError("INVALID_INPUT", "Render version or output kind is invalid", 400)
    format_name = data.get("format", "mp4" if output_kind == "video" else "wav")
    if output_kind == "video" and format_name != "mp4":
        raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "Video projects currently render to MP4", 415)
    if output_kind == "audio" and format_name not in {"wav", "mp3", "m4a"}:
        raise ApiError("UNSUPPORTED_EXPORT_FORMAT", "Audio output must be WAV, MP3 or M4A", 415)
    output_spec = data.get("output_spec")
    render_options = data.get("render_options")
    if output_spec is not None and not isinstance(output_spec, dict):
        raise ApiError("INVALID_INPUT", "output_spec must be an object", 400)
    if render_options is not None and not isinstance(render_options, dict):
        raise ApiError("INVALID_INPUT", "render_options must be an object", 400)
    if purpose == "preview" and render_options:
        raise ApiError(
            "MEDIA_PREVIEW_CANNOT_CHANGE_AUDIO_PROCESSING",
            "Preview keeps the saved project audio processing; change edits on a new project version instead.",
            400,
        )
    try:
        resolved_render_options = validate_render_options(render_options)
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "RENDER_OPTIONS_INVALID", "Render processing options are not supported", 400) from exc
    key = _required_idempotency_key()
    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id)
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    version_row = _version_row(store, workspace_id, project_id, version)
    state = json.loads(version_row["state_json"])
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    try:
        resolved_spec = resolve_output_spec(timeline, output_kind, output_spec)
    except VideoEditingError as exc:
        raise _render_spec_api_error(exc, fallback_message="Render output settings are not supported") from exc
    worker_output_spec = {
        "sample_rate": resolved_spec["sample_rate"],
        "audio_channels": resolved_spec["audio_channels"],
    }
    if output_kind == "video":
        worker_output_spec.update({
            "frame_rate": resolved_spec["frame_rate"],
            "width": resolved_spec["width"],
            "height": resolved_spec["height"],
            "video_signal": {key: resolved_spec["video_signal"][key] for key in (
                "pixel_format", "bit_depth", "color_range", "color_space", "color_transfer", "color_primaries",
            )},
        })
    fingerprint = _fingerprint({
        "project_id": project_id, "version": version, "output_kind": output_kind,
        "format": format_name, "output_spec": resolved_spec,
        "render_options": resolved_render_options, "purpose": purpose,
    })
    job_id, render_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = utc_now()
    directory = "media-previews" if purpose == "preview" else "media-renders"
    relative_path = f"objects/{workspace_id}/{directory}/{render_id}.{format_name}"
    payload = {
        "render_id": render_id, "project_id": project_id, "version": version,
        "output_kind": output_kind, "format": format_name,
        "output_spec": worker_output_spec, "resolved_output_spec": resolved_spec,
        "render_options": resolved_render_options, "purpose": purpose,
    }
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? AND kind = ? AND idempotency_key = ?",
            (workspace_id, job_kind, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different render", 409)
            render_row = connection.execute("SELECT * FROM media_renders WHERE job_id = ?", (existing["id"],)).fetchone()
            if render_row is None:
                raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The original render record is missing", 409)
            render_id, job_id = render_row["id"], existing["id"]
        else:
            current_project = connection.execute(
                "SELECT current_version, work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
                (project_id, workspace_id),
            ).fetchone()
            if current_project is None:
                raise _not_found()
            if current_project["work_id"]:
                require_work_access(connection, identity, current_project["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'queued', ?, ?, ?, 3, ?, ?, ?)",
                (job_id, workspace_id, identity["user"]["id"], current_project["work_id"], job_kind,
                 _json(payload), fingerprint, key,
                 "Waiting to preview Media version" if purpose == "preview" else "Waiting to render Media version",
                 now, now),
            )
            connection.execute(
                "INSERT INTO media_renders(id, workspace_id, project_id, project_version, job_id, output_kind, format, status, relative_path, staging_path, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?)",
                (render_id, workspace_id, project_id, version, job_id, output_kind, format_name, relative_path, now, now),
            )
            render_row = connection.execute("SELECT * FROM media_renders WHERE id = ?", (render_id,)).fetchone()
            existing = None
    job = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, workspace_id))
    result = {"job": _media_job_data(job), "purpose": purpose}
    result["preview_id" if purpose == "preview" else "render_id"] = render_id
    return _success(result, 200 if existing else 202)


def _enqueue_media_transcription(store, identity: dict, project_id: str, data: dict):
    allowed = {
        "version", "source_asset_id", "audio_stream_index", "audio_channel_index",
        "language_hint", "task",
    }
    required = {"version", "source_asset_id", "audio_stream_index", "audio_channel_index", "task"}
    if set(data) - allowed or not required <= set(data):
        raise ApiError("INVALID_INPUT", "Transcription requires a saved source version, stream, channel and task", 400)
    version = data["version"]
    asset_id = data["source_asset_id"]
    stream_index = data["audio_stream_index"]
    channel_index = data["audio_channel_index"]
    task = data["task"]
    language_hint = data.get("language_hint")
    if (type(version) is not int or version < 1 or not isinstance(asset_id, str)
            or type(stream_index) is not int or stream_index < 0
            or type(channel_index) is not int or channel_index < 0
            or task not in {"transcribe", "translate"}
            or (language_hint is not None and (
                not isinstance(language_hint, str)
                or re.fullmatch(r"[A-Za-z]{2,3}", language_hint) is None
            ))):
        raise ApiError("INVALID_INPUT", "Transcription source, stream, channel, language or task is invalid", 400)
    if language_hint is not None:
        language_hint = language_hint.lower()
    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != version:
        raise ApiError("VERSION_CONFLICT", "Choose the currently open Media version before starting transcription", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    version_row = _version_row(store, workspace_id, project_id, version)
    state = json.loads(version_row["state_json"])
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    try:
        asset = timeline._asset(asset_id)
    except VideoEditingError as exc:
        raise _not_found() from exc
    if asset.is_still_image:
        raise ApiError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH", "A still image has no audio stream to transcribe", 400)
    audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
    if stream_index >= len(audio_streams):
        raise ApiError("MEDIA_TRANSCRIPT_STREAM_SELECTION_INVALID", "The selected source has no such audio stream", 400)
    stream = audio_streams[stream_index]
    channels = stream.get("channels")
    sample_rate = stream.get("sample_rate")
    if (type(channels) is not int or type(sample_rate) is not int
            or not 0 <= channel_index < channels or not 8000 <= sample_rate <= 192000):
        raise ApiError("MEDIA_TRANSCRIPT_CHANNEL_SELECTION_INVALID", "The selected source channel is not available", 400)
    material, _ = _store_material_source(store, workspace_id, asset.source_material_id, verify=True)
    if material["sha256"] != asset.sha256:
        raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "The selected source Material no longer matches this Media version", 409)
    try:
        runtime = verify_whisper_runtime()
    except MediaSpeechError as exc:
        raise ApiError(exc.code, "The approved local CPU transcription runtime is unavailable or changed", 503) from exc
    raw_time_base = stream.get("time_base")
    time_base = (
        (raw_time_base["num"], raw_time_base["den"])
        if isinstance(raw_time_base, dict) and type(raw_time_base.get("num")) is int
        and type(raw_time_base.get("den")) is int else None
    )
    normalized = {
        "version": version,
        "source_asset_id": asset.asset_id,
        "source_material_id": material["id"],
        "source_sha256": asset.sha256,
        "audio_stream_index": stream_index,
        "audio_channel_index": channel_index,
        "source_sample_rate": sample_rate,
        "language_hint": language_hint,
        "task": task,
        "model_version": runtime["version"],
        "model_sha256": runtime["model_sha256"],
    }
    fingerprint = _fingerprint({"project_id": project_id, **normalized})
    key = _required_idempotency_key()
    now = utc_now()
    job_id = str(uuid.uuid4())
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? AND kind = 'media_transcribe' AND idempotency_key = ?",
            (workspace_id, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different transcription", 409)
            transcription = connection.execute(
                "SELECT * FROM media_transcriptions WHERE job_id = ? AND workspace_id = ?",
                (existing["id"], workspace_id),
            ).fetchone()
            if transcription is None:
                raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The original transcription job has no Media source record", 409)
            job_id = existing["id"]
            created = False
        else:
            current_project = connection.execute(
                "SELECT current_version, work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
                (project_id, workspace_id),
            ).fetchone()
            if current_project is None:
                raise _not_found()
            if current_project["current_version"] != version:
                raise ApiError("VERSION_CONFLICT", "Media project changed while transcription was being prepared", 409,
                               {"current_version": current_project["current_version"]})
            if current_project["work_id"]:
                require_work_access(connection, identity, current_project["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            payload = {"transcription_id": job_id, "project_id": project_id, "version": version}
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'media_transcribe', 'queued', ?, ?, ?, 0, 'Waiting to transcribe the selected source channel', ?, ?)",
                (job_id, workspace_id, identity["user"]["id"], current_project["work_id"],
                 _json(payload), fingerprint, key, now, now),
            )
            connection.execute(
                "INSERT INTO media_transcriptions(job_id, workspace_id, project_id, project_version, source_asset_id, source_material_id, source_sha256, audio_stream_index, audio_channel_index, source_sample_rate, source_start_pts, source_time_base_num, source_time_base_den, language_hint, task, normalized_sample_rate, model_version, model_sha256, result_metadata_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 16000, ?, ?, '{}', ?, ?)",
                (job_id, workspace_id, project_id, version, asset.asset_id, material["id"], asset.sha256,
                 stream_index, channel_index, sample_rate, stream.get("start_pts"),
                 None if time_base is None else time_base[0], None if time_base is None else time_base[1],
                 language_hint, task, runtime["version"], runtime["model_sha256"], now, now),
            )
            created = True
    job = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, workspace_id))
    return _success({
        "transcription_id": job_id,
        "job": _media_job_data(job),
        "source": {
            "project_id": project_id,
            "project_version": version,
            "source_asset_id": asset.asset_id,
            "source_material_id": material["id"],
            "source_sha256": asset.sha256,
            "audio_stream_index": stream_index,
            "audio_channel_index": channel_index,
            "source_sample_rate": sample_rate,
            "language_hint": language_hint,
            "task": task,
        },
        "automatic_application": False,
        "material_modified": False,
    }, 202 if created else 200)


def _media_transcription_view(
    store,
    workspace_id: str,
    transcription_id: str,
    *,
    word_offset: int = 0,
    word_limit: int = 500,
    identity: dict | None = None,
    control_result: dict | None = None,
):
    row = store.row(
        "SELECT t.*, j.status AS job_status, j.progress_completed, j.progress_total, "
        "j.progress_label, j.error_code, j.error_message, j.cancel_requested, "
        "j.control_revision, j.pause_requested "
        "FROM media_transcriptions t JOIN jobs j ON j.id = t.job_id "
        "WHERE t.job_id = ? AND t.workspace_id = ? AND j.workspace_id = ?",
        (transcription_id, workspace_id, workspace_id),
    )
    if row is None:
        raise _not_found()
    identity = identity or _request_identity_for_workspace(workspace_id)
    if identity is not None:
        project = _project_row(
            store, workspace_id, row["project_id"], identity=identity, write=False,
        )
        version_row = _version_row(
            store, workspace_id, row["project_id"], row["project_version"],
            identity=identity, write=False,
        )
        try:
            state = json.loads(version_row["state_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE", "The transcription's saved Media version is unavailable", 409) from exc
        timeline, _availability = _load_timeline(
            store, workspace_id, state, project_id=row["project_id"], identity=identity,
        )
        try:
            source_asset = timeline._asset(row["source_asset_id"])
        except VideoEditingError as exc:
            raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE", "The transcription source is no longer in its saved version", 409) from exc
        if (source_asset.source_material_id != row["source_material_id"]
                or source_asset.sha256 != row["source_sha256"]):
            raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE", "The transcription source no longer matches its saved version", 409)
    chunks = store.rows(
        "SELECT chunk_index, normalized_start_sample, normalized_end_sample, source_start_sample, "
        "source_end_sample, language, text, segments_json, words_json "
        "FROM media_transcription_chunks WHERE job_id = ? ORDER BY chunk_index",
        (transcription_id,),
    )
    try:
        metadata = json.loads(row["result_metadata_json"])
        if not isinstance(metadata, dict):
            raise ValueError("invalid result metadata")
        all_words = []
        segments = []
        coverage = []
        chunk_text = []
        for chunk in chunks:
            words = json.loads(chunk["words_json"])
            chunk_segments = json.loads(chunk["segments_json"])
            if not isinstance(words, list) or not isinstance(chunk_segments, list):
                raise ValueError("invalid transcription chunk")
            all_words.extend(words)
            segments.extend(chunk_segments)
            chunk_text.append(chunk["text"])
            coverage.append({
                "chunk_index": chunk["chunk_index"],
                "normalized_start_sample": chunk["normalized_start_sample"],
                "normalized_end_sample": chunk["normalized_end_sample"],
                "source_start_sample": chunk["source_start_sample"],
                "source_end_sample": chunk["source_end_sample"],
                "language": chunk["language"],
            })
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_RESULT_INVALID", "The saved transcription candidate could not be read", 409) from exc
    word_count = len(all_words)
    page = all_words[word_offset:word_offset + word_limit]
    detected_languages = metadata.get("detected_languages", {})
    if not isinstance(detected_languages, dict):
        detected_languages = {}
    language = (
        "en" if row["task"] == "translate" else
        row["language_hint"] or metadata.get("language") or next(iter(detected_languages), "und")
    )
    try:
        time_base = (None if row["source_time_base_num"] is None else {
            "num": row["source_time_base_num"], "den": row["source_time_base_den"],
        })
        if time_base is not None and time_base["den"] <= 0:
            raise ValueError("invalid source timebase")
    except (TypeError, ValueError) as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_CLOCK_INVALID", "The saved source clock is invalid", 409) from exc
    status = row["job_status"]
    error = None if row["error_code"] is None else {
        "code": row["error_code"],
        "message": row["error_message"] or "The transcription did not complete.",
    }
    result = {
        "transcription_id": transcription_id,
        "job": {
            "id": transcription_id,
            "kind": "media_transcribe",
            "status": status,
            "progress": {
                "completed": row["progress_completed"],
                "total": row["progress_total"],
                "label": row["progress_label"],
            },
            "error": error,
            "cancel_requested": bool(row["cancel_requested"]),
            "pause_requested": bool(row["pause_requested"]),
            "control_revision": row["control_revision"],
        },
        "source": {
            "project_id": row["project_id"],
            "project_version": row["project_version"],
            "source_asset_id": row["source_asset_id"],
            "source_material_id": row["source_material_id"],
            "source_sha256": row["source_sha256"],
            "audio_stream_index": row["audio_stream_index"],
            "audio_channel_index": row["audio_channel_index"],
            "sample_rate": row["source_sample_rate"],
            "audio_stream_start_pts": row["source_start_pts"],
            "audio_stream_time_base": time_base,
            "language_hint": row["language_hint"],
            "task": row["task"],
        },
        "candidate": {
            "transcript_id": f"ASR-{transcription_id}",
            "alignment_source": "whisper_base_unreviewed",
            "review_state": "unreviewed",
            "language": language,
            "task": row["task"],
            "model": {
                "engine": "openai-whisper",
                "version": row["model_version"],
                "model": "base",
                "sha256": row["model_sha256"],
                "device": "cpu",
                "quality_state": "NOT_EVALUATED",
                "timing_state": "estimated_not_independently_verified",
            },
            "text": "".join(chunk_text),
            "words": page,
            "word_offset": word_offset,
            "word_limit": word_limit,
            "word_count": word_count,
            "has_more_words": word_offset + len(page) < word_count,
            "segments": segments,
            "coverage": coverage,
            "coverage_state": metadata.get("coverage_state", "partial"),
            "detected_languages": detected_languages,
            "packet_clock_check": metadata.get("packet_clock_check"),
            "normalized_sample_rate": ASR_SAMPLE_RATE,
            "ffmpeg_version": metadata.get("ffmpeg_version", "NOT_REPORTED"),
            "word_cut_eligible": row["task"] == "transcribe" and word_count > 0,
        },
        "can_adopt": status in {"completed", "cancelled", "interrupted"} and word_count > 0,
        "automatic_application": False,
        "source_media_modified": False,
        "published": False,
    }
    if control_result is not None and control_result.get("control_receipt") is not None:
        result["control_receipt"] = control_result["control_receipt"]
    return _success(result)


def _resume_media_transcription(store, identity: dict, transcription_id: str):
    from .job_control import control_job
    from .worker import install_worker_schema

    install_worker_schema(store)
    workspace_id = identity["workspace"]["id"]
    row = store.row(
        "SELECT t.*, j.status AS job_status, j.pause_requested AS job_pause_requested, j.control_revision "
        "FROM media_transcriptions t JOIN jobs j ON j.id = t.job_id "
        "WHERE t.job_id = ? AND t.workspace_id = ? AND j.workspace_id = ?",
        (transcription_id, workspace_id, workspace_id),
    )
    if row is None:
        raise _not_found()
    status = row["job_status"]
    if status in {"queued", "running", "pause_requested", "cancel_requested"}:
        return _media_transcription_view(store, workspace_id, transcription_id, identity=identity)
    if status == "completed":
        raise ApiError("MEDIA_TRANSCRIPTION_NOT_RESUMABLE", "Open the completed transcription candidate instead of resuming it", 409,
                       {"status": status})
    if status == "paused" or status == "interrupted" and row["job_pause_requested"]:
        action = "resume"
    elif status in {"cancelled", "interrupted", "failed"}:
        action = "retry"
    else:
        raise ApiError("MEDIA_TRANSCRIPTION_NOT_RESUMABLE", "The transcription cannot continue from its current state", 409,
                       {"status": status})
    control_revision = row["control_revision"]
    operation_id = _idempotency_key() or (
        f"media-transcription-{action}:{transcription_id}:{control_revision}"
    )
    controlled = control_job(
        current_app, store, identity, transcription_id, action,
        {"operation_id": operation_id, "base_control_revision": control_revision},
    )
    return _media_transcription_view(
        store, workspace_id, transcription_id, identity=identity, control_result=controlled,
    )


def _adopt_media_transcription(store, identity: dict, project_id: str, transcription_id: str, data: dict):
    allowed = {"base_version", "words", "word_corrections", "review_state"}
    if set(data) - allowed or type(data.get("base_version")) is not int:
        raise ApiError("INVALID_INPUT", "Adopting a transcription requires base_version and optional word corrections", 400)
    if "words" in data and "word_corrections" in data:
        raise ApiError("INVALID_INPUT", "Choose a complete reviewed word list or a correction list", 400)
    workspace_id = identity["workspace"]["id"]
    record = store.row(
        "SELECT t.*, j.status AS job_status FROM media_transcriptions t JOIN jobs j ON j.id = t.job_id "
        "WHERE t.job_id = ? AND t.workspace_id = ? AND j.workspace_id = ?",
        (transcription_id, workspace_id, workspace_id),
    )
    if record is None or record["project_id"] != project_id:
        raise _not_found()
    if record["job_status"] not in {"completed", "cancelled", "interrupted"}:
        raise ApiError("MEDIA_TRANSCRIPTION_NOT_READY", "Wait for a stable completed, cancelled or interrupted candidate before adopting it", 409,
                       {"status": record["job_status"]})
    project = _project_row(store, workspace_id, project_id)
    if project["current_version"] != data["base_version"]:
        raise ApiError("VERSION_CONFLICT", "Media project changed since the transcript was opened", 409,
                       {"current_version": project["current_version"]})
    if project["work_id"]:
        _require_active_work(store, workspace_id, project["work_id"])
    material, _ = _store_material_source(store, workspace_id, record["source_material_id"], verify=True)
    if material["sha256"] != record["source_sha256"]:
        raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "The original source changed; the candidate remains available for review", 409)
    state, version_row = _read_version_state(store, workspace_id, project_id, data["base_version"])
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True, project_id=project_id,
    )
    try:
        asset = timeline._asset(record["source_asset_id"])
    except VideoEditingError as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE", "The candidate source is no longer attached to this Media version", 409) from exc
    if asset.sha256 != record["source_sha256"]:
        raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "The attached source does not match the transcription candidate", 409)
    chunks = store.rows(
        "SELECT chunk_index, words_json FROM media_transcription_chunks WHERE job_id = ? ORDER BY chunk_index",
        (transcription_id,),
    )
    generated_words = []
    try:
        for chunk in chunks:
            part = json.loads(chunk["words_json"])
            if not isinstance(part, list):
                raise ValueError("invalid word list")
            generated_words.extend(part)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_RESULT_INVALID", "The saved candidate words could not be read", 409) from exc
    if not generated_words:
        raise ApiError("MEDIA_ASR_NO_WORD_TIMESTAMPS", "This candidate has no word time estimates to adopt", 409)
    generated_ids = [item.get("word_id") for item in generated_words if isinstance(item, dict)]
    if (len(generated_ids) != len(generated_words)
            or any(not isinstance(word_id, str) for word_id in generated_ids)
            or len(set(generated_ids)) != len(generated_ids)):
        raise ApiError("MEDIA_TRANSCRIPTION_RESULT_INVALID", "The saved candidate contains duplicate or invalid word identities", 409)
    words_by_id = {item["word_id"]: item for item in generated_words}
    selected_words = data.get("words")
    if selected_words is None:
        selected_words = [dict(item) for item in generated_words]
    if not isinstance(selected_words, list) or not 1 <= len(selected_words) <= 100000:
        raise ApiError("MEDIA_TRANSCRIPT_INVALID", "Reviewed transcript words are invalid", 400)
    normalized_words = []
    try:
        for item in selected_words:
            normalized_words.append(TranscriptWord.from_dict(item).to_dict())
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "MEDIA_TRANSCRIPT_INVALID", "A reviewed transcript word is invalid", 400) from exc
    if [item["word_id"] for item in normalized_words] != generated_ids:
        raise ApiError("MEDIA_TRANSCRIPT_WORD_SET_INCOMPLETE", "Adoption must preserve every candidate word; edit one word by ID or adopt the full ordered list", 409,
                       {"candidate_word_count": len(generated_ids), "submitted_word_count": len(normalized_words)})
    if "word_corrections" in data:
        corrections = data["word_corrections"]
        if not isinstance(corrections, list) or len(corrections) > len(generated_ids):
            raise ApiError("MEDIA_TRANSCRIPT_CORRECTIONS_INVALID", "Word corrections must be a bounded list", 400)
        seen_corrections = set()
        for correction in corrections:
            allowed_correction_fields = {"word_id", "text", "start_sample", "end_sample"}
            if (not isinstance(correction, dict) or set(correction) - allowed_correction_fields
                    or not isinstance(correction.get("word_id"), str)
                    or correction["word_id"] not in words_by_id
                    or correction["word_id"] in seen_corrections
                    or not ("text" in correction or "start_sample" in correction or "end_sample" in correction)
                    or ("start_sample" in correction) != ("end_sample" in correction)):
                raise ApiError("MEDIA_TRANSCRIPT_CORRECTIONS_INVALID", "A correction must name one saved word and provide text or a complete sample range", 400)
            seen_corrections.add(correction["word_id"])
            word = dict(words_by_id[correction["word_id"]])
            if "text" in correction:
                word["text"] = correction["text"]
            if "start_sample" in correction:
                word["start_sample"] = correction["start_sample"]
                word["end_sample"] = correction["end_sample"]
            try:
                normalized = TranscriptWord.from_dict(word).to_dict()
            except VideoEditingError as exc:
                raise ApiError(exc.args[0] if exc.args else "MEDIA_TRANSCRIPT_CORRECTIONS_INVALID", "A corrected word is invalid", 400) from exc
            words_by_id[correction["word_id"]] = normalized
        normalized_words = [words_by_id[word_id] for word_id in generated_ids]
    words = normalized_words
    review_state = data.get("review_state")
    if review_state is None:
        review_state = "human_corrected" if "words" in data or data.get("word_corrections") else "unreviewed"
    if review_state not in {"unreviewed", "human_corrected", "human_reviewed"}:
        raise ApiError("MEDIA_TRANSCRIPT_REVIEW_STATE_INVALID", "Transcript review state is invalid", 400)
    if record["source_time_base_den"] is None:
        time_base = None
    else:
        time_base = {"num": record["source_time_base_num"], "den": record["source_time_base_den"]}
    metadata = json.loads(record["result_metadata_json"])
    language = record["language_hint"] or metadata.get("language") or "und"
    model_provenance = {
        "engine": "openai-whisper",
        "version": record["model_version"],
        "model": "base",
        "model_sha256": record["model_sha256"],
        "task": record["task"],
        "review_state": review_state,
        "timing_method": "whisper_word_timestamp_estimate",
        "quality_state": "NOT_EVALUATED",
        "coverage_state": metadata.get("coverage_state", "partial"),
        "covered_chunks": metadata.get("completed_chunks", len(chunks)),
        "total_chunks": metadata.get("total_chunks"),
        "normalized_sample_rate": ASR_SAMPLE_RATE,
        "packet_clock_check": metadata.get("packet_clock_check"),
        "ffmpeg_version": metadata.get("ffmpeg_version", "NOT_REPORTED"),
        "detected_languages": metadata.get("detected_languages", {}),
    }
    try:
        updated_timeline = timeline.set_transcript({
            "transcript_id": f"ASR-{transcription_id}",
            "source_asset_id": record["source_asset_id"],
            "audio_stream_index": record["audio_stream_index"],
            "audio_channel_index": record["audio_channel_index"],
            "language": language,
            "alignment_source": "whisper_base_unreviewed",
            "task": record["task"],
            "review_state": review_state,
            "model_provenance": model_provenance,
            "words": words,
        })
    except VideoEditingError as exc:
        raise ApiError(exc.args[0] if exc.args else "MEDIA_TRANSCRIPT_INVALID", "The transcript candidate does not match this source clock", 409) from exc
    next_state = _copy_project_state(state, updated_timeline)
    fingerprint = _fingerprint({
        "project_id": project_id,
        "base_version": data["base_version"],
        "transcription_id": transcription_id,
        "words": words,
        "review_state": review_state,
    })
    key = _required_idempotency_key()
    prior_version = _resolve_idempotent_version(store, workspace_id, project_id, key, fingerprint)
    if prior_version is not None:
        return _success(_project_view(store, workspace_id, project_id, prior_version), 200)
    undo_stack = _version_stack(version_row, "undo_stack_json") + [data["base_version"]]
    new_version, _ = _append_project_version(
        store, identity, project_id, base_version=data["base_version"], state=next_state,
        operation="adopt_transcript_candidate", idempotency_key=key, request_hash=fingerprint,
        undo_stack=undo_stack, redo_stack=[],
    )
    return _success({
        "project": _project_view(store, workspace_id, project_id, new_version),
        "transcription_id": transcription_id,
        "transcript_id": f"ASR-{transcription_id}",
        "review_state": review_state,
        "word_count": len(words),
        "automatic_application": False,
        "source_media_modified": False,
    })


class _MediaTranscriptionStopped(RuntimeError):
    pass


def _set_media_transcription_error(store, job_id: str, generation: int, code: str, message: str) -> None:
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT status, cancel_requested FROM jobs WHERE id = ? AND lease_generation = ?",
            (job_id, generation),
        ).fetchone()
        if row is None or row["status"] not in {"running", "cancel_requested"}:
            return
        if row["cancel_requested"] or row["status"] == "cancel_requested":
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND lease_generation = ?",
                (utc_now(), job_id, generation),
            )
            return
        connection.execute(
            "UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?, progress_label = ?, updated_at = ? "
            "WHERE id = ? AND lease_generation = ? AND status = 'running'",
            (code, message, "Transcription stopped; saved candidate chunks remain available", utc_now(), job_id, generation),
        )


def _media_transcription_source(store, transcription_row, identity: dict | None = None):
    workspace_id = transcription_row["workspace_id"]
    state, _ = _read_version_state(
        store, workspace_id, transcription_row["project_id"], transcription_row["project_version"],
    )
    timeline, _ = _load_timeline(
        store, workspace_id, state, verify_files=True,
        project_id=transcription_row["project_id"], identity=identity,
    )
    try:
        asset = timeline._asset(transcription_row["source_asset_id"])
    except VideoEditingError as exc:
        raise MediaSpeechError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE") from exc
    if (asset.sha256 != transcription_row["source_sha256"]
            or asset.source_material_id != transcription_row["source_material_id"]):
        raise MediaSpeechError("SOURCE_FILE_INTEGRITY_ERROR")
    audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
    stream_index = transcription_row["audio_stream_index"]
    if stream_index >= len(audio_streams):
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_MISMATCH")
    stream = audio_streams[stream_index]
    if (stream.get("sample_rate") != transcription_row["source_sample_rate"]
            or type(stream.get("channels")) is not int
            or transcription_row["audio_channel_index"] >= stream["channels"]
            or stream.get("start_pts") != transcription_row["source_start_pts"]):
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_MISMATCH")
    material, path = _store_material_source(store, workspace_id, transcription_row["source_material_id"], verify=True)
    if material["sha256"] != transcription_row["source_sha256"]:
        raise MediaSpeechError("SOURCE_FILE_INTEGRITY_ERROR")
    raw_time_base = stream.get("time_base")
    time_base = (
        (raw_time_base["num"], raw_time_base["den"])
        if isinstance(raw_time_base, dict) and type(raw_time_base.get("num")) is int
        and type(raw_time_base.get("den")) is int else None
    )
    saved_time_base = (
        None if transcription_row["source_time_base_den"] is None else
        (transcription_row["source_time_base_num"], transcription_row["source_time_base_den"])
    )
    if time_base != saved_time_base:
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_MISMATCH")
    return asset, path, stream, time_base


def process_media_transcription_job(app, claim: dict) -> None:
    """Run bounded CPU Whisper chunks and persist source-bound candidate text for review."""
    from .worker import _advance

    store = app.extensions["oevira.store"]
    job_id = claim["id"]
    generation = claim["generation"]
    row = store.row(
        "SELECT * FROM media_transcriptions WHERE job_id = ? AND workspace_id = ?",
        (job_id, claim["workspace_id"]),
    )
    if row is None:
        _set_media_transcription_error(store, job_id, generation, "MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE",
                                       "The transcription source binding could not be read.")
        return
    stage_root = Path(__file__).resolve().parents[2] / ".runtime" / "media-tmp" / "transcriptions"
    process = None
    stage_directory = None
    progress = {"completed": 0, "total": 1, "label": "Rechecking the selected source"}
    last_heartbeat = 0.0

    def check_continue(*, force: bool = False) -> None:
        nonlocal last_heartbeat
        now = time.monotonic()
        if not force and now - last_heartbeat < 2.0:
            return
        if not _advance(
            store, job_id, generation,
            progress["completed"], progress["total"], progress["label"],
        ):
            raise _MediaTranscriptionStopped("MEDIA_TRANSCRIPTION_STOPPED")
        last_heartbeat = now

    try:
        project = _project_row(store, row["workspace_id"], row["project_id"])
        if project["work_id"]:
            _require_active_work(store, row["workspace_id"], project["work_id"])
        job_identity = {
            "workspace": {"id": claim["workspace_id"]},
            "user": {"id": claim["user_id"]},
        }
        asset, source_path, stream, time_base = _media_transcription_source(store, row, job_identity)
        runtime = verify_whisper_runtime()
        if (runtime["version"] != row["model_version"] or runtime["model_sha256"] != row["model_sha256"]):
            raise MediaSpeechError("MEDIA_ASR_MODEL_VERSION_CHANGED")
        check_continue(force=True)
        progress["label"] = "Checking source audio packet timing"
        raw_stream_start_time = stream.get("start_time")
        stream_start_time = (
            (raw_stream_start_time["num"], raw_stream_start_time["den"])
            if isinstance(raw_stream_start_time, dict)
            and type(raw_stream_start_time.get("num")) is int
            and type(raw_stream_start_time.get("den")) is int
            and raw_stream_start_time["den"] > 0 else None
        )
        packet_clock = inspect_audio_packet_clock(
            source_path,
            audio_stream_index=row["audio_stream_index"],
            sample_rate=row["source_sample_rate"],
            time_base=time_base,
            stream_start_pts=row["source_start_pts"],
            stream_start_time=stream_start_time,
            check_continue=check_continue,
        )
        check_continue(force=True)
        stage_root.mkdir(parents=True, exist_ok=True)
        stage_directory = stage_root / f"{job_id}-{generation}"
        stage_directory.mkdir(parents=True, exist_ok=False)
        progress["label"] = "Decoding the selected source channel"
        normalized_path, normalized_sample_count, ffmpeg_version = normalize_audio_channel(
            source_path,
            audio_stream_index=row["audio_stream_index"],
            audio_channel_index=row["audio_channel_index"],
            sample_rate=row["source_sample_rate"],
            working_directory=stage_directory,
            check_continue=check_continue,
        )
        normalized_path, normalized_sample_count, packet_clock = preserve_audio_packet_clock_gaps(
            normalized_path,
            normalized_sample_count,
            packet_clock,
            working_directory=stage_directory,
            check_continue=check_continue,
        )
        total_chunks = (normalized_sample_count + ASR_CHUNK_SECONDS * ASR_SAMPLE_RATE - 1) // (
            ASR_CHUNK_SECONDS * ASR_SAMPLE_RATE
        )
        saved_rows = store.rows(
            "SELECT chunk_index FROM media_transcription_chunks WHERE job_id = ? ORDER BY chunk_index",
            (job_id,),
        )
        next_chunk = 0
        for saved in saved_rows:
            if saved["chunk_index"] != next_chunk:
                raise MediaSpeechError("MEDIA_TRANSCRIPTION_CHUNK_SEQUENCE_INVALID")
            next_chunk += 1
        if next_chunk > total_chunks:
            raise MediaSpeechError("MEDIA_TRANSCRIPTION_CHUNK_SEQUENCE_INVALID")
        progress["completed"] = next_chunk
        progress["total"] = total_chunks
        progress["label"] = "Transcribing speech on CPU"
        check_continue(force=True)
        events_path = stage_directory / "whisper-events.jsonl"
        with events_path.open("xb"):
            pass
        worker_path = Path(__file__).with_name("media_speech_worker.py")
        python_path = Path(runtime["python"])
        command = [
            str(python_path), "-I", "-X", "utf8", str(worker_path),
            "--model", str(_SPEECH_MODEL_PATH()),
            "--model-sha256", row["model_sha256"],
            "--audio", str(normalized_path),
            "--events", str(events_path),
            "--task", row["task"],
            "--chunk-seconds", str(ASR_CHUNK_SECONDS),
            "--padding-seconds", str(ASR_PADDING_SECONDS),
            "--start-chunk", str(next_chunk),
            "--threads", "2",
        ]
        if row["language_hint"]:
            command.extend(["--language", row["language_hint"]])
        process = subprocess.Popen(
            command, cwd=stage_directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        ready = False
        complete_event = None
        expected_chunk = next_chunk
        with events_path.open("r", encoding="utf-8") as events:
            while True:
                line = events.readline()
                if line:
                    if len(line.encode("utf-8")) > 4 * 1024 * 1024:
                        raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_TOO_LARGE")
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID") from exc
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                    if event["type"] == "ready":
                        if (ready or event.get("engine") != "openai-whisper"
                                or event.get("version") != row["model_version"]
                                or event.get("model_sha256") != row["model_sha256"]
                                or event.get("torch") != runtime["torch"]
                                or event.get("device") != "cpu"
                                or event.get("sample_rate") != ASR_SAMPLE_RATE
                                or event.get("total_samples") != normalized_sample_count
                                or event.get("total_chunks") != total_chunks
                                or event.get("start_chunk") != next_chunk):
                            raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                        ready = True
                    elif event["type"] == "chunk":
                        if (not ready or event.get("chunk_index") != expected_chunk
                                or type(event.get("core_start_sample")) is not int
                                or type(event.get("core_end_sample")) is not int
                                or type(event.get("words")) is not list
                                or type(event.get("segments")) is not list
                                or not isinstance(event.get("language"), str)):
                            raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                        check_continue(force=True)
                        normalized_start = event["core_start_sample"]
                        normalized_end = event["core_end_sample"]
                        if (normalized_start != expected_chunk * ASR_CHUNK_SECONDS * ASR_SAMPLE_RATE
                                or not normalized_start < normalized_end <= normalized_sample_count):
                            raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                        source_start = (normalized_start * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE
                        source_end = (normalized_end * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE
                        chunk_words = []
                        for word_index, raw_word in enumerate(event["words"]):
                            if not isinstance(raw_word, dict):
                                raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                            start = raw_word.get("normalized_start_sample")
                            end = raw_word.get("normalized_end_sample")
                            text = raw_word.get("text")
                            if (type(start) is not int or type(end) is not int or start < normalized_start
                                    or end <= start or end > normalized_sample_count or not isinstance(text, str)
                                    or not text.strip() or len(text) > 1000):
                                raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                            source_word_start = (start * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE
                            source_word_end = (end * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE
                            if source_word_end <= source_word_start:
                                continue
                            token_probability = raw_word.get("token_probability")
                            if token_probability is not None and (
                                type(token_probability) not in (int, float)
                                or not 0 <= token_probability <= 1
                            ):
                                token_probability = None
                            chunk_words.append({
                                "word_id": f"asr:{job_id[:8]}:{expected_chunk}:{word_index}",
                                "text": text,
                                "start_sample": source_word_start,
                                "end_sample": source_word_end,
                                **({"token_probability": token_probability}
                                   if token_probability is not None else {}),
                            })
                        chunk_segments = []
                        for segment_index, segment in enumerate(event["segments"]):
                            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                                raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                            segment_start = segment.get("start_sample")
                            segment_end = segment.get("end_sample")
                            if type(segment_start) is not int or type(segment_end) is not int or segment_end < segment_start:
                                raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                            chunk_segments.append({
                                "segment_id": f"asr:{job_id[:8]}:{expected_chunk}:segment:{segment_index}",
                                "text": segment["text"],
                                "source_start_sample": (segment_start * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE,
                                "source_end_sample": (segment_end * row["source_sample_rate"] + ASR_SAMPLE_RATE // 2) // ASR_SAMPLE_RATE,
                            })
                        with store.transaction() as connection:
                            current = connection.execute(
                                "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
                                (job_id, row["workspace_id"]),
                            ).fetchone()
                            if (current is None or current["lease_generation"] != generation
                                    or current["status"] != "running" or current["cancel_requested"]):
                                raise _MediaTranscriptionStopped("MEDIA_TRANSCRIPTION_STOPPED")
                            connection.execute(
                                "INSERT INTO media_transcription_chunks(job_id, chunk_index, normalized_start_sample, normalized_end_sample, source_start_sample, source_end_sample, language, text, segments_json, words_json, created_at) "
                                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (job_id, expected_chunk, normalized_start, normalized_end, source_start, source_end,
                                 event["language"], event.get("text", ""), _json(chunk_segments), _json(chunk_words), utc_now()),
                            )
                            connection.execute(
                                "UPDATE jobs SET progress_completed = ?, progress_total = ?, progress_label = ?, updated_at = ?, lease_heartbeat_at = ? "
                                "WHERE id = ? AND lease_generation = ? AND status = 'running' AND cancel_requested = 0",
                                (expected_chunk + 1, total_chunks, f"Saved transcript audio chunk {expected_chunk + 1} of {total_chunks}",
                                 utc_now(), utc_now(), job_id, generation),
                            )
                            connection.execute(
                                "UPDATE media_transcriptions SET updated_at = ? WHERE job_id = ? AND workspace_id = ?",
                                (utc_now(), job_id, row["workspace_id"]),
                            )
                        expected_chunk += 1
                        progress["completed"] = expected_chunk
                        progress["total"] = total_chunks
                        progress["label"] = "Transcribing speech on CPU"
                        last_heartbeat = time.monotonic()
                    elif event["type"] == "complete":
                        if (not ready or complete_event is not None
                                or event.get("total_chunks") != total_chunks
                                or event.get("processed_chunks") != total_chunks - next_chunk
                                or expected_chunk != total_chunks
                                or not isinstance(event.get("language_counts"), dict)):
                            raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                        complete_event = event
                    elif event["type"] == "error":
                        code = event.get("code")
                        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,96}", code):
                            code = "MEDIA_ASR_WORKER_FAILED"
                        raise MediaSpeechError(code)
                    else:
                        raise MediaSpeechError("MEDIA_ASR_WORKER_RESULT_INVALID")
                elif process.poll() is not None:
                    if not complete_event:
                        raise MediaSpeechError("MEDIA_ASR_WORKER_FAILED")
                    break
                else:
                    check_continue()
                    time.sleep(0.2)
        if process is not None:
            exit_code = process.wait(timeout=10)
            process = None
            if exit_code != 0 or complete_event is None:
                raise MediaSpeechError("MEDIA_ASR_WORKER_FAILED")
        check_continue(force=True)
        material, _ = _store_material_source(store, row["workspace_id"], row["source_material_id"], verify=True)
        if material["sha256"] != row["source_sha256"]:
            raise MediaSpeechError("SOURCE_FILE_INTEGRITY_ERROR")
        language_counts = complete_event["language_counts"]
        language = (
            "en" if row["task"] == "translate" else
            row["language_hint"] or next(iter(language_counts), "und")
        )
        metadata = {
            "engine": "openai-whisper",
            "version": row["model_version"],
            "model": "base",
            "model_sha256": row["model_sha256"],
            "device": "cpu",
            "task": row["task"],
            "language_hint": row["language_hint"],
            "language": language,
            "detected_languages": language_counts,
            "normalized_sample_rate": ASR_SAMPLE_RATE,
            "normalized_sample_count": normalized_sample_count,
            "total_chunks": total_chunks,
            "completed_chunks": expected_chunk,
            "coverage_state": "complete",
            "timing_method": "whisper_word_timestamp_estimate",
            "quality_state": "NOT_EVALUATED",
            "packet_clock_check": packet_clock,
            "ffmpeg_version": ffmpeg_version,
            "source_sha256": row["source_sha256"],
        }
        now = utc_now()
        with store.transaction() as connection:
            current = connection.execute(
                "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
                (job_id, row["workspace_id"]),
            ).fetchone()
            if (current is None or current["lease_generation"] != generation
                    or current["status"] != "running" or current["cancel_requested"]):
                raise _MediaTranscriptionStopped("MEDIA_TRANSCRIPTION_STOPPED")
            connection.execute(
                "UPDATE media_transcriptions SET result_metadata_json = ?, updated_at = ? WHERE job_id = ? AND workspace_id = ?",
                (_json(metadata), now, job_id, row["workspace_id"]),
            )
            connection.execute(
                "UPDATE jobs SET status = 'completed', progress_completed = ?, progress_total = ?, progress_label = ?, error_code = NULL, error_message = NULL, updated_at = ?, lease_heartbeat_at = ? "
                "WHERE id = ? AND workspace_id = ? AND lease_generation = ? AND status = 'running' AND cancel_requested = 0",
                (total_chunks, total_chunks, "Transcript candidate saved and ready for review", now, now,
                 job_id, row["workspace_id"], generation),
            )
    except _MediaTranscriptionStopped:
        return
    except MediaSpeechError as exc:
        messages = {
            "SOURCE_FILE_INTEGRITY_ERROR": "The source Material changed; no transcript was adopted.",
            "MEDIA_ASR_DISCONTINUOUS_AUDIO_NOT_SUPPORTED": "The selected audio stream has PTS gaps or overlaps; no flattened transcript was created.",
            "MEDIA_ASR_OVERLAPPING_AUDIO_NOT_SUPPORTED": "The selected audio stream has overlapping packet times that cannot be mapped safely; no transcript was saved.",
            "MEDIA_ASR_PACKET_CLOCK_UNVERIFIABLE": "Packet timestamps are incomplete; no transcript with unverified source timing was saved.",
            "MEDIA_ASR_SOURCE_CLOCK_UNVERIFIABLE": "The source has no reliable clock origin for mapping speech times; no transcript was saved.",
            "MEDIA_ASR_SOURCE_CLOCK_INVALID": "The source clock conflicts with decoded audio samples; no transcript was saved.",
            "MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED": "The decoded analysis audio does not match its packet-clock segments; no flattened transcript was saved.",
            "MEDIA_ASR_CLOCKED_AUDIO_FAILED": "The discontinuous audio analysis copy could not be preserved; the original source was unchanged.",
            "MEDIA_ASR_AUDIO_LIMIT_EXCEEDED": "The complete selected channel exceeds the current four-hour local ASR limit.",
            "MEDIA_ASR_RUNTIME_UNAVAILABLE": "The pinned local CPU transcription runtime is unavailable.",
            "MEDIA_ASR_MODEL_VERSION_CHANGED": "The pinned Whisper model or runtime changed during the job.",
            "MEDIA_ASR_NO_AUDIO_PACKETS": "The selected audio stream has no decodable packets.",
        }
        _set_media_transcription_error(
            store, job_id, generation, exc.code,
            messages.get(exc.code, "The local transcription did not finish; saved candidate chunks remain available."),
        )
    except ApiError as exc:
        _set_media_transcription_error(
            store, job_id, generation, exc.code,
            "Workspace access or the selected source changed before the transcript could be saved.",
        )
    except (OSError, subprocess.SubprocessError):
        _set_media_transcription_error(
            store, job_id, generation, "MEDIA_ASR_WORKER_FAILED",
            "The local transcription process stopped; saved candidate chunks remain available.",
        )
    except Exception:
        _set_media_transcription_error(
            store, job_id, generation, "MEDIA_ASR_RESULT_SAVE_FAILED",
            "The transcription candidate could not be saved; no project edit was applied.",
        )
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
        if stage_directory is not None:
            try:
                shutil.rmtree(stage_directory)
            except OSError:
                pass


def _SPEECH_MODEL_PATH() -> Path:
    return Path(__file__).resolve().parents[2] / ".runtime" / "models" / "openai-whisper-20250625" / "base.pt"


def _media_resume_project_version(connection, actor: dict, job, project_id: str, version: int):
    workspace_id = job["workspace_id"]
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
        (project_id, workspace_id),
    ).fetchone()
    if project is None:
        raise ApiError("MEDIA_PROJECT_NOT_FOUND", "The saved Media project is unavailable", 404)
    if job["work_id"] != project["work_id"]:
        raise ApiError("MEDIA_JOB_SCOPE_CHANGED", "The saved Media task and project no longer share the same Work", 409)
    if project["work_id"]:
        require_work_access(connection, actor, project["work_id"], write=True)
    else:
        require_workspace_access(connection, actor, write=True)
    version_row = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
        (project_id, workspace_id, version),
    ).fetchone()
    if version_row is None:
        raise ApiError("MEDIA_PROJECT_VERSION_UNAVAILABLE", "The task's frozen Media version is unavailable", 409)
    state = _require_media_project_version_sources(
        connection, actor, project, version_row, target_work_id=project["work_id"],
    )
    return project, version_row, state


def _require_empty_media_resume_context(resume_context: dict) -> None:
    if resume_context:
        raise ApiError(
            "INVALID_INPUT",
            "Continue the saved Media task with its original project, source selection and processing options",
            400,
        )


def validate_media_render_resume(connection, actor: dict, job, resume_context: dict) -> None:
    """Validate a render/preview checkpoint without reading or rendering media."""
    _require_empty_media_resume_context(resume_context)
    expected_kind = job["kind"]
    if expected_kind not in {"media_render", "media_preview"}:
        raise ApiError("MEDIA_JOB_KIND_MISMATCH", "This is not a resumable Media render task", 409)
    try:
        payload = json.loads(job["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The saved Media render request cannot be read", 409) from exc
    purpose = "preview" if expected_kind == "media_preview" else "render"
    if (not isinstance(payload, dict) or payload.get("purpose") != purpose
            or not isinstance(payload.get("project_id"), str)
            or type(payload.get("version")) is not int or payload["version"] < 1):
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The saved Media render request is not resumable", 409)
    render = connection.execute(
        "SELECT * FROM media_renders WHERE id = ? AND workspace_id = ? AND job_id = ?",
        (payload.get("render_id"), job["workspace_id"], job["id"]),
    ).fetchone()
    if (render is None or render["project_id"] != payload["project_id"]
            or render["project_version"] != payload["version"]
            or render["format"] != payload.get("format")
            or render["output_kind"] != payload.get("output_kind")):
        raise ApiError("MEDIA_RENDER_STATE_UNAVAILABLE", "The Media render record does not match its frozen request", 409)
    project, _version_row_value, _state = _media_resume_project_version(
        connection, actor, job, render["project_id"], render["project_version"],
    )
    if render["status"] == "completed":
        if not render["sha256"] or type(render["size_bytes"]) is not int or render["size_bytes"] <= 0:
            raise ApiError("MEDIA_RENDER_CHECKPOINT_INVALID", "The confirmed Media render has no complete file receipt", 409)
        return
    # A render is bound to its frozen project_version. Later project edits do
    # not invalidate that snapshot; retry completes the frozen request.
    if render["status"] not in {"queued", "running", "writing", "interrupted", "cancelled"}:
        raise ApiError("MEDIA_RENDER_NOT_RESUMABLE", "The saved Media render state needs review before continuing", 409,
                       {"status": render["status"]})
    if render["status"] == "writing" and (
        not isinstance(render["staging_path"], str) or not render["staging_path"]
        or not isinstance(render["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", render["sha256"]) is None
        or type(render["size_bytes"]) is not int or render["size_bytes"] <= 0
    ):
        raise ApiError("MEDIA_RENDER_CHECKPOINT_INVALID", "The Media render's staged output receipt is incomplete", 409)


def validate_media_transcription_resume(connection, actor: dict, job, resume_context: dict) -> None:
    """Validate the exact source and contiguous saved ASR chunks before retry."""
    _require_empty_media_resume_context(resume_context)
    if job["kind"] != "media_transcribe":
        raise ApiError("MEDIA_JOB_KIND_MISMATCH", "This is not a resumable Media transcription task", 409)
    transcription = connection.execute(
        "SELECT * FROM media_transcriptions WHERE job_id = ? AND workspace_id = ?",
        (job["id"], job["workspace_id"]),
    ).fetchone()
    if transcription is None:
        raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The saved transcription source record is missing", 409)
    try:
        payload = json.loads(job["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The saved transcription request cannot be read", 409) from exc
    if (not isinstance(payload, dict) or payload.get("project_id") != transcription["project_id"]
            or payload.get("version") != transcription["project_version"]):
        raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The transcription is not bound to its frozen Media version", 409)
    project, _version_row_value, state = _media_resume_project_version(
        connection, actor, job, transcription["project_id"], transcription["project_version"],
    )
    if project["current_version"] != transcription["project_version"]:
        raise ApiError(
            "VERSION_CONFLICT",
            "The project changed while transcription was paused; review the saved candidate before continuing",
            409,
            {"current_version": project["current_version"], "transcription_version": transcription["project_version"]},
        )
    timeline = state.get("timeline")
    assets = timeline.get("assets") if isinstance(timeline, dict) else None
    source = next((item for item in assets or []
                   if isinstance(item, dict) and item.get("asset_id") == transcription["source_asset_id"]), None)
    if (source is None or source.get("source_material_id") != transcription["source_material_id"]
            or source.get("sha256") != transcription["source_sha256"]):
        raise ApiError("MEDIA_TRANSCRIPTION_SOURCE_UNAVAILABLE", "The transcription source is no longer in its frozen version", 409)
    try:
        metadata = json.loads(transcription["result_metadata_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The saved transcription checkpoint metadata cannot be read", 409) from exc
    if not isinstance(metadata, dict):
        raise ApiError("MEDIA_TRANSCRIPTION_STATE_UNAVAILABLE", "The saved transcription checkpoint metadata is invalid", 409)
    chunks = connection.execute(
        "SELECT chunk_index, normalized_start_sample, normalized_end_sample, source_start_sample, "
        "source_end_sample, segments_json, words_json FROM media_transcription_chunks "
        "WHERE job_id = ? ORDER BY chunk_index",
        (job["id"],),
    ).fetchall()
    previous_normalized_end = None
    for index, chunk in enumerate(chunks):
        if (chunk["chunk_index"] != index
                or chunk["normalized_start_sample"] < 0
                or chunk["normalized_end_sample"] <= chunk["normalized_start_sample"]
                or chunk["source_start_sample"] < 0
                or chunk["source_end_sample"] <= chunk["source_start_sample"]
                or previous_normalized_end is not None
                and chunk["normalized_start_sample"] < previous_normalized_end):
            raise ApiError("MEDIA_TRANSCRIPTION_CHECKPOINT_INVALID", "Saved transcription chunks are not a safe contiguous checkpoint", 409)
        try:
            segments = json.loads(chunk["segments_json"])
            words = json.loads(chunk["words_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError("MEDIA_TRANSCRIPTION_CHECKPOINT_INVALID", "A saved transcription chunk cannot be read", 409) from exc
        if not isinstance(segments, list) or not isinstance(words, list):
            raise ApiError("MEDIA_TRANSCRIPTION_CHECKPOINT_INVALID", "A saved transcription chunk has invalid content", 409)
        previous_normalized_end = chunk["normalized_end_sample"]
    if (metadata.get("completed_chunks") is not None
            and metadata.get("completed_chunks") != len(chunks)):
        raise ApiError("MEDIA_TRANSCRIPTION_CHECKPOINT_INVALID", "The saved chunk count differs from its checkpoint receipt", 409)
    if job["progress_completed"] != len(chunks):
        raise ApiError("MEDIA_TRANSCRIPTION_CHECKPOINT_INVALID", "The task progress differs from its saved audio chunks", 409)


def register_media_routes(app) -> None:
    store = app.extensions.get("oevira.store")
    if store is None:
        raise RuntimeError("Media routes require the product Store extension")
    install_media_schema(store)
    recover_media_uploads(store)
    app.extensions.setdefault("oevira.job_resume_validators", {}).update({
        "media_render": validate_media_render_resume,
        "media_preview": validate_media_render_resume,
        "media_transcribe": validate_media_transcription_resume,
    })
    cancelled_retry = app.extensions.setdefault("oevira.job_cancelled_retry_kinds", set())
    cancelled_retry.update({"media_transcribe", "media_render", "media_preview"})
    app.extensions.setdefault("oevira.revision_handlers", {})["media_project"] = revise_media_project
    _register_project_routes(app, store)
    from .media_interchange import register_media_interchange_routes
    register_media_interchange_routes(app, store)


def process_media_interchange_job(app, claim: dict) -> None:
    """Worker entry for Media-owned OTIO/FCPXML/CMX3600 package exchange."""
    from .media_interchange import process_media_interchange_job as process

    process(app, claim)


def _upload_view(row) -> dict:
    return {
        "id": row["id"],
        "kind": "media",
        "title": row["title"],
        "filename": row["filename"],
        "expected_size": row["expected_size"],
        "received_offset": row["received_offset"],
        "status": row["status"],
        "chunk_size": MEDIA_CHUNK_BYTES,
        "material_id": row["material_id"] if row["status"] == "completed" else None,
    }


def _safe_filename(value: object) -> str:
    filename = Path(str(value or "").replace("\\", "/")).name
    filename = "".join(char for char in filename if char.isprintable())[:255]
    if not filename or filename in {".", ".."}:
        raise ApiError("INVALID_INPUT", "filename is invalid", 400)
    return filename


def _finish_upload(store, row, *, identity: dict | None = None) -> dict:
    if row["status"] == "completed":
        material = store.row(
            "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
            (row["material_id"], row["workspace_id"]),
        )
        if material is None:
            raise ApiError("UPLOAD_RECOVERY_REQUIRED", "The completed media upload has no Material record", 409)
        if identity is not None:
            _require_media_material_access(
                store, identity, material["id"], target_work_id=material["work_id"],
            )
        return dict(material)
    if row["status"] not in {"receiving", "finalizing"} or row["received_offset"] != row["expected_size"]:
        raise ApiError("UPLOAD_INCOMPLETE", "The media upload has not received every byte", 409,
                       {"received_offset": row["received_offset"], "expected_size": row["expected_size"]})
    stage = _resource_path(store, row["stage_relative_path"])
    target = _resource_path(store, row["original_relative_path"])
    source = target if target.is_file() else stage
    if not source.is_file() or source.is_symlink():
        raise ApiError("UPLOAD_STORAGE_INTERRUPTED", "The staged media file is unavailable; resume the same upload", 409)
    size = source.stat().st_size
    if size != row["expected_size"]:
        raise ApiError("UPLOAD_SIZE_MISMATCH", "The received media file does not match its declared size", 409)
    digest = sha256_media_file(source)
    if row["expected_sha256"] and digest != row["expected_sha256"]:
        raise ApiError("UPLOAD_CHECKSUM_MISMATCH", "The completed media file did not match its declared checksum", 409)
    target.parent.mkdir(parents=True, exist_ok=True)

    now = utc_now()
    extension = Path(row["filename"]).suffix.lower().lstrip(".") or None
    material_status = "unavailable"
    reason = "The original media is saved; a text projection is not available for this format."
    with store.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?",
            (row["id"], row["workspace_id"]),
        ).fetchone()
        if current is None:
            raise _not_found()
        if current["status"] == "completed":
            existing = connection.execute(
                "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
                (current["material_id"], current["workspace_id"]),
            ).fetchone()
            if existing is None:
                raise ApiError("UPLOAD_RECOVERY_REQUIRED", "The completed media upload has no Material record", 409)
            actor = identity or {
                "workspace": {"id": current["workspace_id"]},
                "user": {"id": current["user_id"]},
            }
            _require_media_material_access_in_transaction(
                connection, actor, existing["id"], target_work_id=existing["work_id"],
            )
            return dict(existing)
        if current["status"] not in {"receiving", "finalizing"} or current["received_offset"] != current["expected_size"]:
            raise ApiError("UPLOAD_STATE_CONFLICT", "The upload changed while completion was being confirmed", 409)
        actor = identity or {
            "workspace": {"id": current["workspace_id"]},
            "user": {"id": current["user_id"]},
        }
        work_id = current["work_id"]
        if work_id:
            work = connection.execute(
                "SELECT id, status FROM works WHERE id = ? AND workspace_id = ?",
                (work_id, current["workspace_id"]),
            ).fetchone()
            if work is None:
                raise _not_found()
            if work["status"] != "active" and work_commission(connection, work_id) is None:
                # Preserve the historical workspace fallback only for unscoped work.
                work_id = None
            else:
                require_work_access(connection, actor, work_id, write=True)
        else:
            require_workspace_access(connection, actor, write=True)
        if target.is_file():
            if target.is_symlink() or target.stat().st_size != size or sha256_media_file(target) != digest:
                raise ApiError("UPLOAD_RECOVERY_REQUIRED", "A conflicting original file is preserved", 409)
            if stage.exists() and not stage.is_symlink():
                stage.unlink(missing_ok=True)
        else:
            if not stage.is_file() or stage.is_symlink():
                raise ApiError("UPLOAD_STORAGE_INTERRUPTED", "The staged media file is unavailable; resume the same upload", 409)
            os.replace(stage, target)
        connection.execute(
            "UPDATE media_uploads SET status = 'finalizing', updated_at = ? WHERE id = ? AND workspace_id = ?",
            (now, current["id"], current["workspace_id"]),
        )
        connection.execute(
            "INSERT OR IGNORE INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason, text, sha256, relative_path, size_bytes, source_json, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (current["material_id"], current["workspace_id"], current["user_id"], work_id,
             current["title"], current["filename"], extension, material_status, reason,
             digest, current["original_relative_path"], size,
             _json({"source_kind": "media_upload", "upload_id": current["id"]}), now),
        )
        if work_id:
            connection.execute(
                "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_id, current["material_id"], current["workspace_id"], now),
            )
        connection.execute(
            "UPDATE media_uploads SET status = 'completed', received_offset = expected_size, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (now, current["id"], current["workspace_id"]),
        )
        material = connection.execute(
            "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
            (current["material_id"], current["workspace_id"]),
        ).fetchone()
        if (material is None or material["sha256"] != digest
                or material["relative_path"] != current["original_relative_path"]):
            raise ApiError("UPLOAD_RECOVERY_REQUIRED", "The Material commit did not match the verified original", 409)
    return dict(material)


def recover_media_uploads(store) -> None:
    """Reconcile chunk files to the last SQLite-confirmed offset at startup."""
    rows = store.rows(
        "SELECT * FROM media_uploads WHERE status IN ('receiving', 'finalizing') ORDER BY created_at, id"
    )
    for row in rows:
        if row["status"] == "receiving" and row["received_offset"] == row["expected_size"]:
            target = _resource_path(store, row["original_relative_path"])
            if target.is_file() and not target.is_symlink():
                try:
                    _finish_upload(store, row)
                    continue
                except ApiError:
                    pass
        if row["status"] == "finalizing":
            try:
                _finish_upload(store, row)
                continue
            except ApiError:
                pass
        try:
            stage = _resource_path(store, row["stage_relative_path"])
            stage.parent.mkdir(parents=True, exist_ok=True)
            if not stage.exists():
                stage.touch(exist_ok=True)
            if stage.is_symlink() or not stage.is_file():
                raise OSError("unsafe staging file")
            file_size = stage.stat().st_size
            actual_offset = min(file_size, row["received_offset"])
            with stage.open("r+b") as stream:
                if file_size != actual_offset:
                    stream.truncate(actual_offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE media_uploads SET status = 'receiving', received_offset = ?, updated_at = ? WHERE id = ? AND status IN ('receiving', 'finalizing')",
                    (actual_offset, utc_now(), row["id"]),
                )
                connection.execute(
                    "DELETE FROM media_upload_chunks WHERE upload_id = ? AND offset + size_bytes > ?",
                    (row["id"], actual_offset),
                )
        except (ApiError, OSError):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE media_uploads SET status = 'interrupted', updated_at = ? WHERE id = ? AND status IN ('receiving', 'finalizing')",
                    (utc_now(), row["id"]),
                )


class _MediaRenderCancelled(Exception):
    pass


class _MediaRenderPaused(Exception):
    pass


def _render_actor_can_write(
    store,
    workspace_id: str,
    user_id: str,
    project_id: str,
    *,
    connection=None,
) -> bool:
    identity = {"workspace": {"id": workspace_id}, "user": {"id": user_id}}

    def check(current_connection) -> bool:
        project = current_connection.execute(
            "SELECT work_id FROM media_projects WHERE id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        ).fetchone()
        if project is None:
            return False
        if project["work_id"]:
            require_work_access(current_connection, identity, project["work_id"], write=True)
        else:
            require_workspace_access(current_connection, identity, write=True)
        return True

    try:
        if connection is not None:
            return check(connection)
        with store.transaction(immediate=False) as current_connection:
            return check(current_connection)
    except ApiError:
        return False


def _job_cancel_check(store, claim: dict, project_id: str) -> None:
    job = store.row(
        "SELECT status, cancel_requested, pause_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
        (claim["id"], claim["workspace_id"]),
    )
    if job is None or job["lease_generation"] != claim["generation"]:
        raise _MediaRenderCancelled("The Media render lease is no longer current")
    if job["status"] == "pause_requested" and job["pause_requested"]:
        raise _MediaRenderPaused("The Media render reached a pause boundary")
    if job["cancel_requested"] or job["status"] in {"cancel_requested", "cancelled"}:
        raise _MediaRenderCancelled("The Media render was cancelled")
    if job["status"] != "running":
        raise _MediaRenderPaused("The Media render is no longer running")
    if not _render_actor_can_write(store, claim["workspace_id"], claim["user_id"], project_id):
        raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")


def _finish_render_job(store, claim: dict, render_id: str, *, status: str, code: str | None = None,
                       message: str | None = None, progress_completed: int = 0,
                       progress_label: str = "Media render did not complete") -> None:
    now = utc_now()
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None or job["lease_generation"] != claim["generation"]:
            return
        if job["cancel_requested"] or job["status"] == "cancel_requested":
            status = "cancelled"
            code = None
            message = None
        connection.execute(
            "UPDATE media_renders SET status = ?, error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (status, code, message, now, render_id, claim["workspace_id"]),
        )
        connection.execute(
            "UPDATE jobs SET status = ?, error_code = ?, error_message = ?, progress_completed = ?, progress_label = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND lease_generation = ?",
            (status, code, message, progress_completed, progress_label, now, claim["id"],
             claim["workspace_id"], claim["generation"]),
        )


def _finalize_media_render(store, claim: dict, render_row, target: Path, stage: Path) -> bool:
    workspace_id = claim["workspace_id"]
    now = utc_now()
    _job_cancel_check(store, claim, render_row["project_id"])
    expected_hash = render_row["sha256"]
    expected_size = render_row["size_bytes"]
    stage_valid = (
        stage.is_file() and not stage.is_symlink()
        and stage.stat().st_size == expected_size and sha256_media_file(stage) == expected_hash
    )
    target_valid = (
        target.is_file() and not target.is_symlink()
        and target.stat().st_size == expected_size and sha256_media_file(target) == expected_hash
    )
    if not stage_valid and not target_valid:
        raise VideoEditingError("MEDIA_RENDER_RECOVERY_REQUIRED")
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, cancel_requested, pause_requested, lease_generation, kind, work_id "
            "FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], workspace_id),
        ).fetchone()
        if job is None or job["lease_generation"] != claim["generation"]:
            return False
        if job["status"] == "pause_requested" and job["pause_requested"]:
            return False
        if job["cancel_requested"] or job["status"] == "cancel_requested":
            connection.execute(
                "UPDATE media_renders SET status = 'cancelled', error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ?",
                (now, render_row["id"]),
            )
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', progress_label = ?, updated_at = ? WHERE id = ? AND lease_generation = ?",
                ("Media preview cancelled" if job["kind"] == "media_preview" else "Media render cancelled",
                 now, claim["id"], claim["generation"]),
            )
            return False
        if job["status"] != "running" or not _render_actor_can_write(
            store, workspace_id, claim["user_id"], render_row["project_id"], connection=connection,
        ):
            raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
            (render_row["project_id"], workspace_id),
        ).fetchone()
        if project is None or job["work_id"] != project["work_id"]:
            raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")
        version_row = connection.execute(
            "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
            (render_row["project_id"], workspace_id, render_row["project_version"]),
        ).fetchone()
        if version_row is None:
            raise VideoEditingError("MEDIA_RENDER_STATE_UNAVAILABLE")
        _require_media_project_version_sources(
            connection,
            {"workspace": {"id": workspace_id}, "user": {"id": claim["user_id"]}},
            project,
            version_row,
            target_work_id=project["work_id"],
        )
        if stage_valid:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target_valid:
                    raise VideoEditingError("MEDIA_RENDER_OUTPUT_CONFLICT")
                stage.unlink(missing_ok=True)
            else:
                os.replace(stage, target)
        connection.execute(
            "UPDATE media_renders SET status = 'completed', relative_path = ?, staging_path = NULL, error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (render_row["relative_path"], now, render_row["id"], workspace_id),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed', error_code = NULL, error_message = NULL, progress_completed = 3, progress_total = 3, progress_label = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND lease_generation = ?",
            ("Media preview verified and saved" if job["kind"] == "media_preview" else "Media render verified and saved",
             now, claim["id"], workspace_id, claim["generation"]),
        )
    return True


def _reuse_completed_render_for_preview(
    store, claim: dict, render_id: str, project_id: str, payload: dict,
) -> bool:
    """Reuse a verified same-version render when its processing recipe matches."""
    candidates = store.rows(
        "SELECT r.*, j.payload_json FROM media_renders r "
        "JOIN jobs j ON j.id = r.job_id AND j.workspace_id = r.workspace_id "
        "WHERE r.workspace_id = ? AND r.project_id = ? AND r.project_version = ? "
        "AND r.output_kind = ? AND r.format = ? AND r.status = 'completed' "
        "AND j.kind = 'media_render' AND j.status = 'completed' "
        "ORDER BY r.updated_at DESC, r.id LIMIT 16",
        (claim["workspace_id"], project_id, payload["version"], payload["output_kind"], payload["format"]),
    )
    candidate = None
    candidate_path = None
    for row in candidates:
        try:
            original_payload = json.loads(row["payload_json"])
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if (original_payload.get("project_id") != project_id
                or original_payload.get("version") != payload["version"]
                or original_payload.get("output_kind") != payload["output_kind"]
                or original_payload.get("format") != payload["format"]
                or original_payload.get("resolved_output_spec") != payload["resolved_output_spec"]
                or original_payload.get("render_options") != payload["render_options"]):
            continue
        path = _resource_path(store, row["relative_path"])
        if (not path.is_file() or path.is_symlink() or row["sha256"] is None
                or path.stat().st_size != row["size_bytes"]
                or sha256_media_file(path) != row["sha256"]):
            continue
        candidate = {**dict(row), "metadata": metadata}
        candidate_path = path
        break
    if candidate is None or candidate_path is None:
        return False

    metadata = dict(candidate["metadata"])
    metadata["purpose"] = "preview"
    metadata["preview_reuse"] = {
        "reused_render_id": candidate["id"],
        "project_version": payload["version"],
        "requested_output_spec": payload["resolved_output_spec"],
        "rendered_output_spec": metadata.get("output_spec"),
        "render_options_match": True,
    }
    stage = _resource_path(
        store,
        f"staging/media-preview-reuse/.{render_id}.{claim['generation']}.partial",
    )
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, cancel_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None or job["lease_generation"] != claim["generation"]:
            return False
        if job["cancel_requested"] or job["status"] == "cancel_requested":
            raise _MediaRenderCancelled("The Media preview was cancelled before reuse")
        if job["status"] == "pause_requested":
            raise _MediaRenderPaused("The Media preview reached a pause boundary")
        if job["status"] != "running":
            return False
        if not _render_actor_can_write(
            store, claim["workspace_id"], claim["user_id"], project_id, connection=connection,
        ):
            raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
            (project_id, claim["workspace_id"]),
        ).fetchone()
        version_row = connection.execute(
            "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
            (project_id, claim["workspace_id"], payload["version"]),
        ).fetchone()
        if project is None or version_row is None:
            raise VideoEditingError("MEDIA_RENDER_STATE_UNAVAILABLE")
        _require_media_project_version_sources(
            connection,
            {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}},
            project,
            version_row,
            target_work_id=project["work_id"],
        )
        connection.execute(
            "UPDATE media_renders SET status = 'writing', relative_path = ?, staging_path = NULL, "
            "sha256 = ?, size_bytes = ?, metadata_json = ?, error_code = NULL, error_message = NULL, updated_at = ? "
            "WHERE id = ? AND workspace_id = ? AND job_id = ?",
            (candidate["relative_path"], candidate["sha256"], candidate["size_bytes"],
             _json(metadata), utc_now(), render_id, claim["workspace_id"], claim["id"]),
        )
        connection.execute(
            "UPDATE jobs SET progress_completed = 2, progress_total = 3, "
            "progress_label = 'Reusing verified same-version Media render', updated_at = ? "
            "WHERE id = ? AND workspace_id = ? AND lease_generation = ? AND status = 'running'",
            (utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
        )
    render_row = store.row("SELECT * FROM media_renders WHERE id = ? AND workspace_id = ?", (render_id, claim["workspace_id"]))
    _finalize_media_render(store, claim, render_row, candidate_path, stage)
    return True


def _pause_media_render_job(store, claim: dict, render_id: str, checkpoint: dict | None = None) -> bool:
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, pause_requested, lease_generation FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None or job["lease_generation"] != claim["generation"]:
            return False
        if job["status"] != "pause_requested" or not job["pause_requested"]:
            return False
        render = connection.execute(
            "SELECT status FROM media_renders WHERE id = ? AND workspace_id = ? AND job_id = ?",
            (render_id, claim["workspace_id"], claim["id"]),
        ).fetchone()
        if render is None or render["status"] == "completed":
            return True
        if checkpoint is None:
            connection.execute(
                "UPDATE media_renders SET status = 'interrupted', error_code = NULL, error_message = NULL, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND job_id = ?",
                (utc_now(), render_id, claim["workspace_id"], claim["id"]),
            )
        else:
            connection.execute(
                "UPDATE media_renders SET status = 'interrupted', staging_path = ?, sha256 = ?, size_bytes = ?, "
                "metadata_json = ?, error_code = NULL, error_message = NULL, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND job_id = ?",
                (checkpoint["staging_path"], checkpoint["sha256"], checkpoint["size_bytes"],
                 _json(checkpoint["metadata"]), utc_now(), render_id, claim["workspace_id"], claim["id"]),
            )
    return True


def _confirm_completed_media_render_job(store, claim: dict, render_row) -> bool:
    workspace_id = claim["workspace_id"]
    identity = {"workspace": {"id": workspace_id}, "user": {"id": claim["user_id"]}}
    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, pause_requested, cancel_requested, lease_generation, work_id "
            "FROM jobs WHERE id = ? AND workspace_id = ?",
            (claim["id"], workspace_id),
        ).fetchone()
        if job is None or job["lease_generation"] != claim["generation"] or job["status"] != "running":
            return False
        if job["pause_requested"] or job["cancel_requested"]:
            return False
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id = ? AND workspace_id = ?",
            (render_row["project_id"], workspace_id),
        ).fetchone()
        if project is None or project["work_id"] != job["work_id"]:
            raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")
        if project["work_id"]:
            require_work_access(connection, identity, project["work_id"], write=True)
        else:
            require_workspace_access(connection, identity, write=True)
        version_row = connection.execute(
            "SELECT * FROM media_project_versions WHERE project_id = ? AND workspace_id = ? AND version = ?",
            (render_row["project_id"], workspace_id, render_row["project_version"]),
        ).fetchone()
        if version_row is None:
            raise VideoEditingError("MEDIA_RENDER_STATE_UNAVAILABLE")
        _require_media_project_version_sources(
            connection, identity, project, version_row, target_work_id=project["work_id"],
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed', progress_completed = 3, progress_total = 3, "
            "error_code = NULL, error_message = NULL, progress_label = 'Media render verified and saved', updated_at = ? "
            "WHERE id = ? AND workspace_id = ? AND lease_generation = ? AND status = 'running' "
            "AND cancel_requested = 0 AND pause_requested = 0",
            (utc_now(), claim["id"], workspace_id, claim["generation"]),
        )
    return True


def process_media_render_job(app, claim: dict) -> None:
    """Worker entry: render one frozen, authorized Media project version."""
    store = app.extensions["oevira.store"]
    job_identity = {
        "workspace": {"id": claim["workspace_id"]},
        "user": {"id": claim["user_id"]},
    }
    job = store.row(
        "SELECT * FROM jobs WHERE id = ? AND workspace_id = ?",
        (claim["id"], claim["workspace_id"]),
    )
    if job is None:
        return
    last_heartbeat = [time.monotonic()]

    def check_continue() -> None:
        _job_cancel_check(store, claim, project_id)
        now_monotonic = time.monotonic()
        if now_monotonic - last_heartbeat[0] >= 5:
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ? AND workspace_id = ? AND lease_generation = ? AND status = 'running' AND cancel_requested = 0",
                    (utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
                )
            last_heartbeat[0] = now_monotonic
    try:
        payload = json.loads(job["payload_json"])
        purpose = "preview" if job["kind"] == "media_preview" else "render"
        render_id = payload["render_id"]
        project_id = payload["project_id"]
        render_row = store.row(
            "SELECT * FROM media_renders WHERE id = ? AND workspace_id = ? AND job_id = ?",
            (render_id, claim["workspace_id"], claim["id"]),
        )
        if render_row is None:
            raise VideoEditingError("MEDIA_RENDER_STATE_UNAVAILABLE")
        _job_cancel_check(store, claim, project_id)
        target = _resource_path(store, render_row["relative_path"])
        if render_row["status"] == "completed":
            if (not target.is_file() or target.is_symlink()
                    or target.stat().st_size != render_row["size_bytes"]
                    or sha256_media_file(target) != render_row["sha256"]):
                raise VideoEditingError("MEDIA_RENDER_RECOVERY_REQUIRED")
            _confirm_completed_media_render_job(store, claim, render_row)
            return
        if render_row["staging_path"]:
            stage = _resource_path(store, render_row["staging_path"] or "")
            if target.is_file() and not target.is_symlink() and sha256_media_file(target) == render_row["sha256"]:
                _finalize_media_render(store, claim, render_row, target, stage)
                return
            if stage.is_file() and not stage.is_symlink() and sha256_media_file(stage) == render_row["sha256"]:
                timeline_row = _version_row(store, claim["workspace_id"], project_id, render_row["project_version"])
                _load_timeline(
                    store, claim["workspace_id"], json.loads(timeline_row["state_json"]),
                    verify_files=True, project_id=project_id, identity=job_identity,
                )
                _job_cancel_check(store, claim, project_id)
                _finalize_media_render(store, claim, render_row, target, stage)
                return
            raise VideoEditingError("MEDIA_RENDER_RECOVERY_REQUIRED")
        if not _render_actor_can_write(store, claim["workspace_id"], claim["user_id"], project_id):
            raise VideoEditingError("MEDIA_RENDER_ACCESS_REVOKED")
        version_row = _version_row(store, claim["workspace_id"], project_id, payload["version"])
        state = json.loads(version_row["state_json"])
        timeline, _ = _load_timeline(
            store, claim["workspace_id"], state, verify_files=True,
            project_id=project_id, identity=job_identity,
        )
        _job_cancel_check(store, claim, project_id)
        if payload.get("purpose") == "preview" and _reuse_completed_render_for_preview(
            store, claim, render_id, project_id, payload,
        ):
            return
        with store.transaction() as connection:
            connection.execute(
                "UPDATE media_renders SET status = 'running', error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (utc_now(), render_id, claim["workspace_id"]),
            )
            connection.execute(
                "UPDATE jobs SET progress_completed = 1, progress_total = 3, progress_label = ?, updated_at = ? WHERE id = ? AND lease_generation = ? AND status = 'running'",
                ("Rendering frozen Media preview" if purpose == "preview" else "Rendering frozen Media version",
                 utc_now(), claim["id"], claim["generation"]),
            )
        with tempfile.TemporaryDirectory(prefix="oevira-media-render-") as temporary:
            work = Path(temporary)
            intermediate = work / ("render.mp4" if payload["output_kind"] == "video" else "render.wav")
            result = render_timeline(
                timeline,
                intermediate,
                output_kind=payload["output_kind"],
                output_spec=payload["output_spec"],
                render_options=payload["render_options"],
                check_continue=check_continue,
            )
            result["output_spec"] = payload["resolved_output_spec"]
            output = intermediate
            encoding_result = None
            if payload["format"] in {"mp3", "m4a"}:
                encoding_result = encode_audio_delivery(
                    intermediate,
                    format=payload["format"],
                    duration=float(timeline.duration_sec),
                    sample_rate=payload["output_spec"]["sample_rate"],
                    audio_channels=payload["output_spec"]["audio_channels"],
                    check_continue=check_continue,
                    stream_files=True,
                )
                output = Path(encoding_result["files"][f"episode.{payload['format']}"])
            _load_timeline(
                store, claim["workspace_id"], state, verify_files=True,
                project_id=project_id, identity=job_identity,
            )
            check_continue()
            digest = sha256_media_file(output)
            size_bytes = output.stat().st_size
            metadata = {
                "purpose": payload.get("purpose", "render"),
                "renderer": result["renderer"],
                "output_kind": payload["output_kind"],
                "format": payload["format"],
                "project_version": payload["version"],
                "output_spec": payload["resolved_output_spec"],
                "render": result,
                "encoding": encoding_result["report"] if encoding_result else None,
                "source_material_ids": [asset.source_material_id for asset in timeline.assets],
                "rights_state": "unknown",
                "publish_state": "not_performed",
            }
            stage_relative = f"objects/{claim['workspace_id']}/media-renders/.{render_id}.{claim['generation']}.partial"
            stage = _resource_path(store, stage_relative)
            stage.parent.mkdir(parents=True, exist_ok=True)
            if stage.exists():
                raise VideoEditingError("MEDIA_RENDER_STAGING_CONFLICT")
            with output.open("rb") as source, stage.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            with store.transaction() as connection:
                current_job = connection.execute(
                    "SELECT status, cancel_requested, pause_requested, lease_generation "
                    "FROM jobs WHERE id = ? AND workspace_id = ?",
                    (claim["id"], claim["workspace_id"]),
                ).fetchone()
                if (current_job is not None and current_job["lease_generation"] == claim["generation"]
                        and current_job["status"] == "pause_requested" and current_job["pause_requested"]):
                    raise _MediaRenderPaused("The Media render paused with a verified staged output")
                if (current_job is None or current_job["lease_generation"] != claim["generation"]
                        or current_job["status"] != "running" or current_job["cancel_requested"]):
                    raise _MediaRenderCancelled("The Media render was cancelled before file publication")
                connection.execute(
                    "UPDATE media_renders SET status = 'writing', staging_path = ?, sha256 = ?, size_bytes = ?, metadata_json = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                    (stage_relative, digest, size_bytes, _json(metadata), utc_now(), render_id, claim["workspace_id"]),
                )
                connection.execute(
                    "UPDATE jobs SET progress_completed = 2, progress_total = 3, progress_label = 'Verifying rendered output', updated_at = ? WHERE id = ? AND lease_generation = ?",
                    (utc_now(), claim["id"], claim["generation"]),
                )
            _job_cancel_check(store, claim, project_id)
            _finalize_media_render(store, claim, store.row("SELECT * FROM media_renders WHERE id = ?", (render_id,)), target, stage)
    except _MediaRenderPaused:
        render_id = locals().get("render_id", "")
        checkpoint = None
        staging_path = locals().get("stage_relative")
        digest = locals().get("digest")
        size_bytes = locals().get("size_bytes")
        metadata = locals().get("metadata")
        stage = locals().get("stage")
        if (isinstance(staging_path, str) and isinstance(digest, str)
                and type(size_bytes) is int and isinstance(metadata, dict)
                and isinstance(stage, Path) and stage.is_file() and not stage.is_symlink()
                and stage.stat().st_size == size_bytes):
            checkpoint = {
                "staging_path": staging_path,
                "sha256": digest,
                "size_bytes": size_bytes,
                "metadata": metadata,
            }
        if render_id and _pause_media_render_job(store, claim, render_id, checkpoint):
            return
        current = store.row("SELECT status FROM jobs WHERE id = ? AND workspace_id = ?", (claim["id"], claim["workspace_id"]))
        if current is not None and current["status"] in {"cancel_requested", "cancelled"}:
            _finish_render_job(
                store, claim, render_id, status="cancelled",
                progress_label=("Media preview cancelled; the project version was preserved"
                                if locals().get("purpose") == "preview"
                                else "Media render cancelled; the project version was preserved"),
            )
    except _MediaRenderCancelled:
        _finish_render_job(
            store, claim, locals().get("render_id", ""), status="cancelled",
            progress_label=("Media preview cancelled; the project version was preserved"
                            if locals().get("purpose") == "preview"
                            else "Media render cancelled; the project version was preserved"),
        )
    except (ApiError, VideoEditingError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, ApiError) else (exc.args[0] if exc.args else "MEDIA_RENDER_FAILED")
        message = {
            "MEDIA_RENDER_TOOLS_UNAVAILABLE": "The approved media rendering runtime is unavailable.",
            "SOURCE_MEDIA_CHANGED": "A selected source changed while this render was running.",
            "MEDIA_RENDER_ACCESS_REVOKED": "Workspace access changed before the rendered result could be saved.",
            "MEDIA_RENDER_FAILED": "The Media render did not complete; the source project was preserved.",
        }.get(code, "The Media render did not complete; the project version and source Materials were preserved.")
        render_id = locals().get("render_id", "")
        if render_id:
            _finish_render_job(
                store, claim, render_id, status="failed", code=code, message=message,
                progress_label=("Media preview failed; the project version was preserved"
                                if locals().get("purpose") == "preview"
                                else "Media render failed; the project version was preserved"),
            )
    except Exception:
        render_id = locals().get("render_id", "")
        if render_id:
            _finish_render_job(
                store, claim, render_id, status="failed", code="MEDIA_RENDER_FAILED",
                message="The Media render could not be completed; the project version was preserved.",
                progress_label=("Media preview failed; the project version was preserved"
                                if locals().get("purpose") == "preview"
                                else "Media render failed; the project version was preserved"),
            )


def _register_media_upload_routes(app, store) -> None:
    @app.post("/api/uploads")
    def media_start_upload():
        identity = _require_write()
        data = _body()
        allowed = {"kind", "title", "filename", "expected_size", "expected_sha256", "work_id"}
        if set(data) - allowed or data.get("kind") != "media":
            raise ApiError("INVALID_INPUT", "Only media uploads are supported by this upload route", 400)
        title = _string(data.get("title"), "title", maximum=200)
        filename = _safe_filename(data.get("filename"))
        expected_size = data.get("expected_size")
        if type(expected_size) is not int or expected_size <= 0:
            raise ApiError("INVALID_INPUT", "expected_size must be a positive integer", 400)
        expected_sha256 = data.get("expected_sha256")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
        ):
            raise ApiError("INVALID_INPUT", "expected_sha256 must be a SHA-256 hexadecimal digest", 400)
        work_id = data.get("work_id")
        if work_id is not None and not isinstance(work_id, str):
            raise ApiError("INVALID_INPUT", "work_id must be a string", 400)
        workspace_id = identity["workspace"]["id"]
        _require_media_target_work_access(store, identity, work_id, write=True)
        key = _required_idempotency_key()
        fingerprint = _fingerprint({
            "kind": "media", "title": title, "filename": filename,
            "expected_size": expected_size, "expected_sha256": expected_sha256,
            "work_id": work_id,
        })
        upload_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        now = utc_now()
        stage_relative = f"staging/media-uploads/{upload_id}.part"
        original_relative = f"objects/{workspace_id}/materials/{material_id}/original"
        free_bytes = shutil.disk_usage(store.root).free
        reservations = store.row(
            "SELECT COALESCE(SUM(expected_size - received_offset), 0) AS bytes FROM media_uploads "
            "WHERE status IN ('receiving', 'finalizing')"
        )["bytes"]
        if expected_size + reservations + MEDIA_UPLOAD_RESERVE_BYTES > free_bytes:
            raise ApiError("MEDIA_STORAGE_UNAVAILABLE", "There is not enough local storage reserved for this upload", 507,
                           {"expected_size": expected_size, "available_bytes": max(0, free_bytes - reservations - MEDIA_UPLOAD_RESERVE_BYTES)})
        stage = _resource_path(store, stage_relative)
        stage.parent.mkdir(parents=True, exist_ok=True)
        try:
            with store.transaction() as connection:
                if work_id:
                    require_work_access(connection, identity, work_id, write=True)
                else:
                    require_workspace_access(connection, identity, write=True)
                prior = connection.execute(
                    "SELECT * FROM media_uploads WHERE workspace_id = ? AND idempotency_key = ?",
                    (workspace_id, key),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != fingerprint:
                        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for different upload input", 409)
                    existing = dict(prior)
                else:
                    with stage.open("xb") as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    connection.execute(
                        "INSERT INTO media_uploads(id, workspace_id, user_id, work_id, title, filename, expected_size, expected_sha256, received_offset, material_id, stage_relative_path, original_relative_path, idempotency_key, request_hash, status, created_at, updated_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'receiving', ?, ?)",
                        (upload_id, workspace_id, identity["user"]["id"], work_id, title, filename,
                         expected_size, expected_sha256.lower() if expected_sha256 else None, material_id,
                         stage_relative, original_relative, key, fingerprint, now, now),
                    )
                    existing = None
        except Exception:
            # Only remove a stage file created by this request and not owned by a durable row.
            row = store.row("SELECT id FROM media_uploads WHERE id = ?", (upload_id,))
            if row is None:
                stage.unlink(missing_ok=True)
            raise
        if existing is not None:
            return _success(_upload_view(existing), 200)
        row = store.row("SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?", (upload_id, workspace_id))
        return _success(_upload_view(row), 201)

    @app.route("/api/uploads/<upload_id>", methods=["HEAD"])
    def media_head_upload(upload_id: str):
        row = store.row("SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?", (upload_id, _workspace_id()))
        if row is None:
            raise _not_found()
        response = make_response("", 204)
        response.headers["Upload-Offset"] = str(row["received_offset"])
        response.headers["Upload-Length"] = str(row["expected_size"])
        response.headers["Upload-State"] = row["status"]
        response.headers["Upload-Chunk-Size"] = str(MEDIA_CHUNK_BYTES)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.patch("/api/uploads/<upload_id>")
    def media_append_upload(upload_id: str):
        _require_write()
        workspace_id = _workspace_id()
        offset_text = request.headers.get("Upload-Offset")
        checksum = request.headers.get("X-Chunk-SHA256", "").strip().lower()
        try:
            offset = int(offset_text) if offset_text is not None else -1
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "Upload-Offset must be a non-negative integer", 400) from exc
        if offset < 0 or len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise ApiError("INVALID_INPUT", "Chunk offset and SHA-256 are required", 400)
        if request.mimetype != "application/octet-stream" or request.content_length is None:
            raise ApiError("INVALID_INPUT", "Upload chunks must use application/octet-stream with Content-Length", 400)
        if request.content_length <= 0 or request.content_length > MEDIA_CHUNK_BYTES:
            raise ApiError("MEDIA_CHUNK_SIZE_INVALID", "Chunk size is outside the accepted bound", 413,
                           {"maximum_chunk_bytes": MEDIA_CHUNK_BYTES})
        chunk = request.stream.read(MEDIA_CHUNK_BYTES + 1)
        if len(chunk) != request.content_length or len(chunk) > MEDIA_CHUNK_BYTES:
            raise ApiError("MEDIA_CHUNK_SIZE_INVALID", "The received chunk length does not match Content-Length", 400)
        actual_checksum = hashlib.sha256(chunk).hexdigest()
        if actual_checksum != checksum:
            raise ApiError("MEDIA_CHUNK_CHECKSUM_MISMATCH", "The chunk checksum did not match; no offset was advanced", 409)
        wrote = False
        committed = False
        stage = None
        try:
            with store.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?",
                    (upload_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise _not_found()
                receipt = connection.execute(
                    "SELECT size_bytes, sha256 FROM media_upload_chunks WHERE upload_id = ? AND offset = ?",
                    (upload_id, offset),
                ).fetchone()
                if receipt is not None:
                    if receipt["size_bytes"] == len(chunk) and receipt["sha256"] == actual_checksum:
                        response = make_response("", 204)
                        response.headers["Upload-Offset"] = str(row["received_offset"])
                        return response
                    raise ApiError("MEDIA_CHUNK_CONFLICT", "The upload offset was already committed with different bytes", 409)
                if row["status"] != "receiving":
                    raise ApiError("MEDIA_UPLOAD_STATE_CONFLICT", "The upload is no longer receiving chunks", 409)
                if row["received_offset"] != offset:
                    raise ApiError("MEDIA_UPLOAD_OFFSET_CONFLICT", "Resume from the server-confirmed offset", 409,
                                   {"received_offset": row["received_offset"]})
                if offset + len(chunk) > row["expected_size"]:
                    raise ApiError("MEDIA_UPLOAD_LENGTH_EXCEEDED", "The chunk would exceed expected_size", 413)
                stage = _resource_path(store, row["stage_relative_path"])
                stage.parent.mkdir(parents=True, exist_ok=True)
                if stage.is_symlink():
                    raise ApiError("FILE_UNAVAILABLE", "The upload staging path is unsafe", 409)
                if not stage.exists():
                    stage.touch(exist_ok=False)
                if stage.stat().st_size != offset:
                    if stage.stat().st_size > offset:
                        with stage.open("r+b") as stream:
                            stream.truncate(offset)
                            stream.flush()
                            os.fsync(stream.fileno())
                    else:
                        raise ApiError("MEDIA_UPLOAD_RECOVERY_REQUIRED", "Staging data is shorter than the committed offset", 409,
                                       {"received_offset": row["received_offset"]})
                with stage.open("ab") as stream:
                    stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                wrote = True
                next_offset = offset + len(chunk)
                connection.execute(
                    "INSERT INTO media_upload_chunks(upload_id, offset, size_bytes, sha256, created_at) VALUES(?, ?, ?, ?, ?)",
                    (upload_id, offset, len(chunk), actual_checksum, utc_now()),
                )
                connection.execute(
                    "UPDATE media_uploads SET received_offset = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND received_offset = ?",
                    (next_offset, utc_now(), upload_id, workspace_id, offset),
                )
            committed = True
            response = make_response("", 204)
            response.headers["Upload-Offset"] = str(offset + len(chunk))
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception:
            if wrote and not committed and stage is not None and stage.is_file() and not stage.is_symlink():
                try:
                    with stage.open("r+b") as stream:
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError:
                    pass
            raise

    @app.post("/api/uploads/<upload_id>/complete")
    def media_complete_upload(upload_id: str):
        identity = _require_write()
        row = store.row(
            "SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?",
            (upload_id, identity["workspace"]["id"]),
        )
        if row is None:
            raise _not_found()
        material = _finish_upload(store, row, identity=identity)
        return _success({
            "upload": _upload_view(store.row("SELECT * FROM media_uploads WHERE id = ?", (upload_id,))),
            "material": {
                "id": material["id"], "title": material["title"], "filename": material["filename"],
                "status": material["status"], "sha256": material["sha256"],
                "size_bytes": material["size_bytes"], "source_state": "saved_media_original",
            },
        }, 200 if row["status"] == "completed" else 201)

    @app.post("/api/uploads/<upload_id>/cancel")
    def media_cancel_upload(upload_id: str):
        _require_write()
        workspace_id = _workspace_id()
        with store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM media_uploads WHERE id = ? AND workspace_id = ?",
                (upload_id, workspace_id),
            ).fetchone()
            if row is None:
                raise _not_found()
            if row["status"] == "completed":
                raise ApiError("MEDIA_UPLOAD_ALREADY_COMPLETED", "A completed Material cannot be cancelled as an upload", 409)
            if row["status"] == "finalizing":
                raise ApiError("MEDIA_UPLOAD_FINALIZING", "The completed file is being committed; retry status readback", 409)
            connection.execute(
                "UPDATE media_uploads SET status = 'cancelled', updated_at = ? WHERE id = ? AND workspace_id = ? AND status IN ('receiving', 'interrupted', 'failed')",
                (utc_now(), upload_id, workspace_id),
            )
            updated = connection.execute("SELECT * FROM media_uploads WHERE id = ?", (upload_id,)).fetchone()
        stage = _resource_path(store, updated["stage_relative_path"])
        if stage.is_file() and not stage.is_symlink():
            stage.unlink(missing_ok=True)
        return _success(_upload_view(updated))
