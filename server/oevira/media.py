"""Media editing core adapted into OEVIRA's existing Media owner.

Reuse source: historical self-owned Apache-2.0 snapshot
``D:/LUMENIS_REBUILD/序/序境/lumenis/shared/content_creation/video_editing.py``
(SHA-256 ``FEDA9B4988B081FB1C542196CD83522D931A433BC74134C3F9EC2FCFDEDA6B1B``).
Adaptations bind source assets to workspace Materials, persist project versions
through the Media schema, use exact rational time, and keep rendering behind
the existing permission and Worker boundaries. The old runtime is not imported.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit


class VideoEditingError(ValueError):
    """Raised for invalid local media or timeline operations."""


ALLOWED_FORMAT_RATIOS = ("9:16", "4:5", "3:4", "16:9", "1:1")
ALLOWED_OPERATIONS = frozenset({"trim", "split", "reorder", "delete", "transition", "hold", "word_cut"})
MAX_EDIT_TIME_SEC = 24 * 60 * 60
MAX_TRANSITION_DURATION_SEC = 30.0
MAX_SERIALIZED_INTEGER = 1_000_000_000
SUPPORTED_VIDEO_TRANSITION_TYPES = frozenset({
    "cut", "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "circlecrop",
    "rectcrop", "distance", "fadeblack", "fadewhite", "radial",
    "smoothleft", "smoothright", "smoothup", "smoothdown", "circleopen",
    "circleclose", "vertopen", "vertclose", "horzopen", "horzclose",
    "dissolve", "pixelize", "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice", "hblur", "fadegrays",
    "wipetl", "wipetr", "wipebl", "wipebr", "squeezeh", "squeezev",
    "zoomin", "fadefast", "fadeslow", "hlwind", "hrwind", "vuwind",
    "vdwind", "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
})
_EDIT_BATCH_COMMAND_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "add_track": (frozenset({"op", "track_id", "kind"}), frozenset({"layout"})),
    "insert": (frozenset({"op", "source_id", "track_id"}),
               frozenset({"source_in", "source_out", "display_duration_sec", "timeline_start", "purpose",
                          "audio_role", "audio_stream_index", "video_stream_index"})),
    "set_display_duration": (frozenset({"op", "clip_id", "display_duration_sec"}), frozenset()),
    "trim": (frozenset({"op", "clip_id", "source_in", "source_out"}), frozenset()),
    "replace_clip_source": (
        frozenset({"op", "clip_id", "source_id", "source_in"}),
        frozenset({"audio_stream_index", "display_duration_sec"}),
    ),
    "replace_audio_source": (
        frozenset({"op", "clip_id", "source_id", "source_in", "source_out", "ripple_track_ids"}),
        frozenset({"audio_stream_index", "overlay_ranges"}),
    ),
    "split": (frozenset({"op", "clip_id", "at_sec"}), frozenset()),
    "delete": (frozenset({"op", "clip_id"}), frozenset({"ripple", "synchronize"})),
    "sync_cut": (frozenset({"op", "cuts"}), frozenset({"synchronize"})),
    "reorder": (frozenset({"op", "clip_id", "new_index"}), frozenset()),
    "transition": (frozenset({"op", "from_clip", "to_clip", "reason"}),
                   frozenset({"transition_type", "duration_sec"})),
    "audio_mix": (frozenset({"op", "clip_id", "mix"}), frozenset()),
    "set_audio_bus": (frozenset({"op", "bus"}), frozenset()),
    "remove_audio_bus": (frozenset({"op", "bus_id"}), frozenset()),
    "set_transcript": (frozenset({"op", "transcript"}), frozenset()),
    "remove_transcript": (frozenset({"op", "transcript_id"}), frozenset()),
    "set_video_color_interpretation": (frozenset({"op", "interpretation"}), frozenset()),
    "remove_video_color_interpretation": (
        frozenset({"op", "asset_id", "video_stream_index"}), frozenset(),
    ),
    "track_audio_routing": (frozenset({"op", "track_id"}), frozenset({"gain", "pan", "sends"})),
    "move_audio_clip": (frozenset({"op", "clip_id", "timeline_start"}), frozenset()),
    "move_video_clip": (frozenset({"op", "clip_id", "timeline_start"}), frozenset()),
    "playback_rate": (frozenset({"op", "clip_id", "playback_rate"}), frozenset()),
    "track_controls": (frozenset({"op", "track_id"}), frozenset({"locked","muted","solo","hidden"})),
    "add_caption": (
        frozenset({"op", "start_sec", "end_sec", "text"}),
        frozenset({"layout_profile", "style"}),
    ),
    "update_caption": (
        frozenset({"op", "overlay_id", "start_sec", "end_sec", "text"}),
        frozenset({"layout_profile", "style"}),
    ),
    "remove_overlay": (frozenset({"op", "overlay_id"}), frozenset()),
    "add_chapter": (frozenset({"op", "title", "start_sec"}), frozenset({"chapter_id"})),
    "update_chapter": (frozenset({"op", "chapter_id", "title", "start_sec"}), frozenset()),
    "remove_chapter": (frozenset({"op", "chapter_id"}), frozenset()),
    "set_format": (frozenset({"op", "format_ratio"}), frozenset()),
    "frame_crop": (frozenset({"op", "clip_id", "crop"}), frozenset()),
    "video_placement": (frozenset({"op", "clip_id", "placement"}), frozenset()),
    "set_video_effects": (frozenset({"op", "clip_id", "effects"}), frozenset()),
    "set_transform_keyframes": (frozenset({"op", "clip_id", "keyframes"}), frozenset()),
}
_EDIT_BATCH_AUDIO_MIX_FIELDS = frozenset(
    {"volume", "fade_in_sec", "fade_out_sec", "ducking"}
)
_MAX_EDIT_BATCH_COMMANDS = 128


# File-only protocols still allow playlist demuxers to open other local files.
# User sources are self-contained media; generated concat lists belong only to
# the renderer's private working directory, never to this input boundary.
MEDIA_INPUT_DEMUXERS = "mov,matroska,avi,wav,mp3,flac,ogg,aac,aiff,mpegts,png_pipe,jpeg_pipe,webp_pipe,bmp_pipe"
STATIC_IMAGE_DEMUXERS = {"png_pipe", "jpeg_pipe", "webp_pipe", "bmp_pipe"}
_MEDIA_ASSET_FACTORY_TOKEN = object()
_MEDIA_TOOLS_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "tools" / "ffmpeg-9.0.2-full_build" / "bin"
_EXPECTED_FFMPEG_SHA256 = "589E50B766D251AFDF181DD664D40BD94407E200B019989FD7468C7D118A28D0"
_EXPECTED_FFPROBE_SHA256 = "02264DFA4F56BAAA31E443ECE8AA16D397F6B008200E7FD184D22B9009E75FD3"
_TOOL_PATHS: Dict[str, str | None] = {}

# Additive owner tables; core Store/versioning remains owned by its existing module.
MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_projects (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('video', 'podcast')),
    current_version INTEGER NOT NULL CHECK(current_version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS media_projects_workspace_updated
    ON media_projects(workspace_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS media_project_versions (
    project_id TEXT NOT NULL REFERENCES media_projects(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 0),
    parent_version INTEGER,
    operation TEXT NOT NULL,
    state_json TEXT NOT NULL,
    undo_stack_json TEXT NOT NULL DEFAULT '[]',
    redo_stack_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(project_id, version)
);
CREATE TABLE IF NOT EXISTS media_uploads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    expected_size INTEGER NOT NULL CHECK(expected_size > 0),
    expected_sha256 TEXT,
    received_offset INTEGER NOT NULL DEFAULT 0 CHECK(received_offset >= 0),
    material_id TEXT NOT NULL UNIQUE,
    stage_relative_path TEXT NOT NULL,
    original_relative_path TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('receiving', 'finalizing', 'completed', 'cancelled', 'failed', 'interrupted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, idempotency_key),
    CHECK(received_offset <= expected_size)
);
CREATE INDEX IF NOT EXISTS media_uploads_recovery
    ON media_uploads(status, updated_at);
CREATE TABLE IF NOT EXISTS media_upload_chunks (
    upload_id TEXT NOT NULL REFERENCES media_uploads(id),
    offset INTEGER NOT NULL CHECK(offset >= 0),
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(upload_id, offset)
);
CREATE TABLE IF NOT EXISTS media_renders (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    project_id TEXT NOT NULL REFERENCES media_projects(id),
    project_version INTEGER NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    output_kind TEXT NOT NULL CHECK(output_kind IN ('video', 'audio')),
    format TEXT NOT NULL CHECK(format IN ('mp4', 'wav', 'mp3', 'm4a')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'writing', 'completed', 'failed', 'cancelled', 'interrupted')),
    relative_path TEXT NOT NULL,
    staging_path TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id, project_version) REFERENCES media_project_versions(project_id, version)
);
CREATE INDEX IF NOT EXISTS media_renders_project_version
    ON media_renders(workspace_id, project_id, project_version, created_at DESC);
CREATE TABLE IF NOT EXISTS media_transcriptions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    project_id TEXT NOT NULL REFERENCES media_projects(id),
    project_version INTEGER NOT NULL,
    source_asset_id TEXT NOT NULL,
    source_material_id TEXT NOT NULL REFERENCES materials(id),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    audio_stream_index INTEGER NOT NULL CHECK(audio_stream_index >= 0),
    audio_channel_index INTEGER NOT NULL CHECK(audio_channel_index >= 0),
    source_sample_rate INTEGER NOT NULL CHECK(source_sample_rate BETWEEN 8000 AND 192000),
    source_start_pts INTEGER,
    source_time_base_num INTEGER,
    source_time_base_den INTEGER,
    language_hint TEXT,
    task TEXT NOT NULL CHECK(task IN ('transcribe', 'translate')),
    normalized_sample_rate INTEGER NOT NULL CHECK(normalized_sample_rate = 16000),
    model_version TEXT NOT NULL,
    model_sha256 TEXT NOT NULL CHECK(length(model_sha256) = 64),
    result_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id, project_version)
        REFERENCES media_project_versions(project_id, version)
);
CREATE INDEX IF NOT EXISTS media_transcriptions_project
    ON media_transcriptions(workspace_id, project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS media_transcription_chunks (
    job_id TEXT NOT NULL REFERENCES media_transcriptions(job_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    normalized_start_sample INTEGER NOT NULL CHECK(normalized_start_sample >= 0),
    normalized_end_sample INTEGER NOT NULL CHECK(normalized_end_sample > normalized_start_sample),
    source_start_sample INTEGER NOT NULL CHECK(source_start_sample >= 0),
    source_end_sample INTEGER NOT NULL CHECK(source_end_sample > source_start_sample),
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    words_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS media_recording_sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    project_id TEXT NOT NULL REFERENCES media_projects(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    take_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    track_kind TEXT NOT NULL CHECK(track_kind IN ('voiceover', 'bgm', 'sfx')),
    cue_id TEXT,
    speaker_label TEXT,
    base_project_version INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL CHECK(sample_rate BETWEEN 8000 AND 192000),
    channels INTEGER NOT NULL CHECK(channels IN (1, 2, 6, 8)),
    sample_format TEXT NOT NULL CHECK(sample_format IN ('pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'flac')),
    timeline_start_num INTEGER NOT NULL,
    timeline_start_den INTEGER NOT NULL CHECK(timeline_start_den > 0),
    capture_epoch_id TEXT,
    interrupt_reason TEXT,
    latest_sequence INTEGER NOT NULL DEFAULT -1 CHECK(latest_sequence >= -1),
    status TEXT NOT NULL CHECK(status IN ('armed', 'awaiting_first_segment', 'recording', 'paused', 'interrupted', 'stopping', 'completed', 'cancelled', 'failed')),
    project_version INTEGER,
    completion_idempotency_key TEXT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    stopped_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS media_recording_sessions_project
    ON media_recording_sessions(workspace_id, project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS media_recording_segments (
    session_id TEXT NOT NULL REFERENCES media_recording_sessions(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    material_id TEXT NOT NULL REFERENCES materials(id),
    source_sha256 TEXT NOT NULL,
    audio_stream_index INTEGER NOT NULL CHECK(audio_stream_index >= 0),
    codec_name TEXT NOT NULL,
    sample_rate INTEGER NOT NULL CHECK(sample_rate BETWEEN 8000 AND 192000),
    channels INTEGER NOT NULL CHECK(channels IN (1, 2, 6, 8)),
    sample_count INTEGER NOT NULL CHECK(sample_count > 0),
    capture_epoch_id TEXT NOT NULL,
    capture_sample_start INTEGER NOT NULL CHECK(capture_sample_start >= 0),
    timeline_start_num INTEGER NOT NULL,
    timeline_start_den INTEGER NOT NULL CHECK(timeline_start_den > 0),
    gap_samples_before INTEGER NOT NULL DEFAULT 0 CHECK(gap_samples_before >= 0),
    clock_relation TEXT NOT NULL CHECK(clock_relation IN ('epoch_anchor', 'sample_contiguous', 'sample_gap', 'explicit_epoch_mapping')),
    source_start_pts INTEGER,
    source_time_base_num INTEGER,
    source_time_base_den INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, sequence),
    UNIQUE(workspace_id, session_id, material_id)
);
CREATE INDEX IF NOT EXISTS media_recording_segments_material
    ON media_recording_segments(workspace_id, material_id);
"""


def install_media_schema(store) -> None:
    """Install Media-owned rows additively without seeding project data."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + MEDIA_SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def sha256_media_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def restore_media_asset(
    path: Path,
    stored: Mapping[str, Any],
    material_id: str,
    *,
    verify_file: bool = False,
) -> "MediaAsset":
    """Rebind a frozen project source snapshot to its current authorized Material file."""
    if not isinstance(stored, Mapping):
        raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
    if stored.get("source_material_id") != material_id:
        raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
    try:
        resolved = path.resolve(strict=verify_file)
    except OSError as exc:
        raise VideoEditingError("SOURCE_MEDIA_NOT_READABLE") from exc
    if verify_file and (not resolved.is_file() or sha256_media_file(resolved) != stored.get("sha256")):
        raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
    speech_source = None
    wav_source = (
        resolved.suffix.lower() == ".wav"
        or Path(str(stored.get("filename", ""))).suffix.lower() == ".wav"
    )
    if wav_source and resolved.is_file() and resolved.stat().st_size <= 10 * 1024 * 1024:
        from .media_speech import MediaSpeechError, _read_tts_source
        try:
            content = resolved.read_bytes()
            if hashlib.sha256(content).hexdigest() != stored.get("sha256"):
                raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
            speech_source = _read_tts_source(content)
        except MediaSpeechError as exc:
            raise VideoEditingError("MEDIA_SPEECH_SOURCE_DECLARATION_INVALID") from exc
        except OSError as exc:
            raise VideoEditingError("SOURCE_MEDIA_NOT_READABLE") from exc
    asset = MediaAsset(
        asset_id=stored["asset_id"],
        filename=stored["filename"],
        sha256=stored["sha256"],
        duration_sec=(None if stored["duration_sec"] is None
                      else _fraction_time(stored["duration_sec"])),
        path=str(resolved),
        evidence_refs=tuple(stored.get("evidence_refs", [])),
        rights_status=stored.get("rights_status", "unknown"),
        has_video=stored["has_video"],
        audio_stream_count=stored["audio_stream_count"],
        source_material_id=material_id,
        stream_metadata=tuple(stored.get("streams", [])),
        format_metadata=stored.get("format", {}),
        speech_source=speech_source,
        _factory_token=_MEDIA_ASSET_FACTORY_TOKEN,
    )
    actual = asset.to_dict()
    stored_value = dict(stored)
    if "speech_source" not in stored_value:
        actual.pop("speech_source", None)
    if actual != stored_value:
        raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
    return asset


@dataclass(frozen=True)
class _MediaProbe:
    duration_sec: Optional[Fraction]
    has_video: bool
    audio_stream_count: int
    streams: Tuple[Dict[str, Any], ...]
    format: Dict[str, Any]


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _fraction_time(value: Any, *, error_code: str = "MEDIA_TIME_INVALID") -> Fraction:
    """Parse exact seconds from an integer, decimal string, or {num, den}."""
    try:
        if isinstance(value, bool):
            raise ValueError("boolean time")
        if isinstance(value, Fraction):
            result = value
        elif isinstance(value, dict):
            if set(value) != {"num", "den"}:
                raise ValueError("invalid rational fields")
            numerator, denominator = value["num"], value["den"]
            if (isinstance(numerator, bool) or not isinstance(numerator, int)
                    or isinstance(denominator, bool) or not isinstance(denominator, int)
                    or denominator <= 0):
                raise ValueError("invalid rational values")
            result = Fraction(numerator, denominator)
        elif isinstance(value, int):
            result = Fraction(value, 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite time")
            result = Fraction(str(value))
        elif isinstance(value, str):
            result = Fraction(value)
        else:
            raise ValueError("unsupported time value")
    except (OverflowError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise VideoEditingError(error_code) from exc
    if result.denominator > 10**12 or abs(result.numerator) > MAX_SERIALIZED_INTEGER * result.denominator:
        raise VideoEditingError(error_code)
    return result


def _time_json(value: Any) -> Dict[str, int]:
    exact = _fraction_time(value)
    return {"num": exact.numerator, "den": exact.denominator}


def _bounded_time(
    value: Any,
    *,
    error_code: str,
    minimum: Fraction | int | None = None,
    maximum: Fraction | int | None = None,
) -> Fraction:
    try:
        exact = _fraction_time(value, error_code=error_code)
    except VideoEditingError as exc:
        raise VideoEditingError(error_code) from exc
    if minimum is not None and exact < minimum:
        raise VideoEditingError(error_code)
    if maximum is not None and exact > maximum:
        raise VideoEditingError(error_code)
    return exact


def _bounded_number(
    value: Any,
    *,
    error_code: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Accept only finite, bounded JSON numeric values (never bool or coercions)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoEditingError(error_code)
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise VideoEditingError(error_code) from exc
    if not math.isfinite(number):
        raise VideoEditingError(error_code)
    if minimum is not None and number < minimum:
        raise VideoEditingError(error_code)
    if maximum is not None and number > maximum:
        raise VideoEditingError(error_code)
    return number


def _json_safe_copy(value: Any) -> Any:
    """Validate and copy nested EDL metadata before it crosses serialization."""
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int:
        if abs(value) > MAX_SERIALIZED_INTEGER:
            raise VideoEditingError("EDL_METADATA_NUMBER_OUT_OF_RANGE")
        return value
    if type(value) is float:
        return _bounded_number(
            value,
            error_code="EDL_METADATA_NUMBER_INVALID",
            minimum=-MAX_EDIT_TIME_SEC,
            maximum=MAX_EDIT_TIME_SEC,
        )
    if isinstance(value, Mapping):
        copied: Dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise VideoEditingError("EDL_METADATA_KEY_INVALID")
            copied[key] = _json_safe_copy(nested)
        return copied
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(nested) for nested in value]
    raise VideoEditingError("EDL_METADATA_NOT_JSON_SAFE")


def _is_supported_static_image(path: Path, demuxer: str) -> bool:
    """Reject animated and multi-picture containers instead of inventing duration."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if demuxer == "png_pipe":
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    return False
                saw_header = False
                saw_data = False
                for _ in range(100_000):
                    header = stream.read(8)
                    if len(header) != 8:
                        return False
                    length = int.from_bytes(header[:4], "big")
                    chunk_type = header[4:]
                    if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
                        return False
                    if length > size or stream.tell() + length + 4 > size:
                        return False
                    if chunk_type == b"IHDR":
                        if saw_header or length != 13:
                            return False
                        saw_header = True
                    elif chunk_type == b"IDAT":
                        if not saw_header:
                            return False
                        saw_data = True
                    if chunk_type == b"IEND":
                        return length == 0 and saw_header and saw_data and stream.tell() + 4 == size
                    stream.seek(length + 4, os.SEEK_CUR)
                return False
            if demuxer == "jpeg_pipe":
                if stream.read(2) != b"\xff\xd8":
                    return False
                saw_frame = False
                for _ in range(100_000):
                    prefix = stream.read(1)
                    if not prefix:
                        return False
                    if prefix != b"\xff":
                        continue
                    marker_byte = stream.read(1)
                    while marker_byte == b"\xff":
                        marker_byte = stream.read(1)
                    if not marker_byte:
                        return False
                    marker = marker_byte[0]
                    if marker in {0xD9, 0xDA}:
                        return saw_frame
                    if marker in {0xD8, 0x01, *range(0xD0, 0xD8)}:
                        continue
                    raw_length = stream.read(2)
                    if len(raw_length) != 2:
                        return False
                    segment_length = int.from_bytes(raw_length, "big")
                    if segment_length < 2 or stream.tell() + segment_length - 2 > size:
                        return False
                    payload_length = segment_length - 2
                    if marker == 0xE2:
                        payload = stream.read(payload_length)
                        if payload.startswith(b"MPF\x00"):
                            return False
                    else:
                        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                            saw_frame = True
                        stream.seek(payload_length, os.SEEK_CUR)
                return False
            if demuxer == "bmp_pipe":
                return stream.read(2) == b"BM"
            if demuxer == "webp_pipe":
                header = stream.read(12)
                if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WEBP":
                    return False
                riff_end = min(size, int.from_bytes(header[4:8], "little") + 8)
                saw_image = False
                while stream.tell() + 8 <= riff_end:
                    chunk = stream.read(8)
                    chunk_type = chunk[:4]
                    length = int.from_bytes(chunk[4:], "little")
                    if stream.tell() + length + (length & 1) > riff_end:
                        return False
                    if chunk_type in {b"ANIM", b"ANMF"}:
                        return False
                    if chunk_type == b"VP8X":
                        if length < 1:
                            return False
                        flags = stream.read(1)
                        if not flags or flags[0] & 0x02:
                            return False
                        stream.seek(length - 1, os.SEEK_CUR)
                        saw_image = True
                    else:
                        if chunk_type in {b"VP8 ", b"VP8L"}:
                            saw_image = True
                        stream.seek(length, os.SEEK_CUR)
                    if length & 1:
                        stream.seek(1, os.SEEK_CUR)
                return saw_image and stream.tell() == riff_end
    except OSError:
        return False
    return False


def _static_image_color_profile(path: Path, demuxer: str) -> str:
    """Record whether an sRGB assumption is used or an unsupported profile exists."""
    try:
        with path.open("rb") as stream:
            if demuxer == "png_pipe":
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    return "unknown"
                profile = None
                for _ in range(100_000):
                    header = stream.read(8)
                    if len(header) != 8:
                        break
                    length = int.from_bytes(header[:4], "big")
                    chunk_type = header[4:]
                    if length > path.stat().st_size or stream.tell() + length + 4 > path.stat().st_size:
                        return "unknown"
                    if chunk_type == b"iCCP":
                        return "embedded_icc_unsupported"
                    if chunk_type == b"sRGB":
                        profile = "srgb_tagged"
                    elif chunk_type == b"gAMA" and profile is None:
                        profile = "gamma_tagged_unsupported"
                    elif chunk_type == b"cICP":
                        payload = stream.read(length)
                        if payload == bytes((1, 13, 0, 1)):
                            profile = "srgb_tagged"
                        else:
                            return "cicp_profile_unsupported"
                        stream.seek(4, os.SEEK_CUR)
                        if chunk_type == b"IEND":
                            break
                        continue
                    stream.seek(length + 4, os.SEEK_CUR)
                    if chunk_type == b"IEND":
                        break
                return profile or "srgb_assumed"
            if demuxer == "jpeg_pipe":
                if stream.read(2) != b"\xff\xd8":
                    return "unknown"
                for _ in range(100_000):
                    prefix = stream.read(1)
                    if not prefix:
                        break
                    if prefix != b"\xff":
                        continue
                    marker_byte = stream.read(1)
                    while marker_byte == b"\xff":
                        marker_byte = stream.read(1)
                    if not marker_byte or marker_byte[0] in {0xD9, 0xDA}:
                        break
                    marker = marker_byte[0]
                    if marker in {0xD8, 0x01, *range(0xD0, 0xD8)}:
                        continue
                    raw_length = stream.read(2)
                    if len(raw_length) != 2:
                        return "unknown"
                    length = int.from_bytes(raw_length, "big") - 2
                    if length < 0:
                        return "unknown"
                    if marker == 0xE2:
                        payload = stream.read(min(length, 32))
                        if payload.startswith(b"ICC_PROFILE\x00"):
                            return "embedded_icc_unsupported"
                        stream.seek(length - len(payload), os.SEEK_CUR)
                    else:
                        stream.seek(length, os.SEEK_CUR)
                return "srgb_assumed"
            if demuxer == "webp_pipe":
                header = stream.read(12)
                if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WEBP":
                    return "unknown"
                riff_end = min(path.stat().st_size, int.from_bytes(header[4:8], "little") + 8)
                while stream.tell() + 8 <= riff_end:
                    chunk = stream.read(8)
                    chunk_type = chunk[:4]
                    length = int.from_bytes(chunk[4:], "little")
                    if chunk_type == b"ICCP":
                        return "embedded_icc_unsupported"
                    stream.seek(length + (length & 1), os.SEEK_CUR)
                return "srgb_assumed"
            if demuxer == "bmp_pipe":
                return "srgb_assumed" if stream.read(2) == b"BM" else "unknown"
    except OSError:
        return "unknown"
    return "unknown"


@dataclass(frozen=True)
class MediaAsset:
    asset_id: str
    filename: str
    sha256: str
    duration_sec: Optional[Fraction | float]
    path: str
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    rights_status: str = "unknown"
    has_video: bool = True
    audio_stream_count: int = 0
    source_material_id: Optional[str] = None
    stream_metadata: Tuple[Dict[str, Any], ...] = field(default_factory=tuple, repr=False, compare=False)
    format_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    speech_source: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.validate()
        if self.duration_sec is not None:
            object.__setattr__(self, "duration_sec", _fraction_time(self.duration_sec))

    @property
    def is_still_image(self) -> bool:
        return self.duration_sec is None

    def validate(self) -> None:
        if self._factory_token is not _MEDIA_ASSET_FACTORY_TOKEN:
            raise VideoEditingError("MEDIA_ASSET_MUST_BE_FINGERPRINTED_FROM_LOCAL_FILE")
        if self.duration_sec is None:
            if not self.has_video or self.audio_stream_count != 0:
                raise VideoEditingError("MEDIA_ASSET_STREAM_INVENTORY_INVALID")
            if self.format_metadata.get("format_name") not in STATIC_IMAGE_DEMUXERS:
                raise VideoEditingError("STATIC_IMAGE_CONTAINER_NOT_SUPPORTED")
        else:
            _bounded_time(
                self.duration_sec,
                error_code="MEDIA_ASSET_REQUIRES_FINGERPRINT_AND_DURATION",
                minimum=Fraction(1, 1_000_000),
                maximum=MAX_EDIT_TIME_SEC,
            )
        if not self.asset_id or not self.sha256:
            raise VideoEditingError("MEDIA_ASSET_REQUIRES_FINGERPRINT_AND_DURATION")
        if (not isinstance(self.filename, str) or not self.filename
                or any(not char.isprintable() for char in self.filename)):
            raise VideoEditingError("MEDIA_ASSET_FILENAME_INVALID")
        if (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256.lower())
            or (self.source_material_id is None and self.asset_id != f"MEDIA-{self.sha256[:16]}")
            or (self.source_material_id is not None and self.asset_id != f"MEDIA-{self.sha256[:16]}-{self.source_material_id}")
        ):
            raise VideoEditingError("MEDIA_ASSET_FINGERPRINT_INVALID")
        if self.source_material_id is not None and (
            not isinstance(self.source_material_id, str) or not self.source_material_id.strip()
        ):
            raise VideoEditingError("MEDIA_ASSET_SOURCE_REFERENCE_INVALID")
        if (
            type(self.has_video) is not bool
            or type(self.audio_stream_count) is not int
            or self.audio_stream_count < 0
            or self.audio_stream_count > 256
            or (not self.has_video and self.audio_stream_count == 0)
        ):
            raise VideoEditingError("MEDIA_ASSET_STREAM_INVENTORY_INVALID")
        if self.evidence_refs:
            raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
        if self.rights_status != "unknown":
            raise VideoEditingError("RIGHTS_STATUS_REQUIRES_CANONICAL_REVIEW")
        if not isinstance(self.stream_metadata, tuple) or any(not isinstance(item, dict) for item in self.stream_metadata):
            raise VideoEditingError("MEDIA_ASSET_STREAM_INVENTORY_INVALID")
        if len(self.stream_metadata) > 256:
            raise VideoEditingError("MEDIA_ASSET_STREAM_INVENTORY_INVALID")
        if self.speech_source is not None:
            if (not isinstance(self.speech_source, dict)
                    or self.speech_source.get("schema") != "oevira.media.speech-source.v1"
                    or self.speech_source.get("synthetic") is not True
                    or self.speech_source.get("canonical_fact") is not False):
                raise VideoEditingError("MEDIA_SPEECH_SOURCE_DECLARATION_INVALID")
            _json_safe_copy(self.speech_source)
        stream_ids = [item.get("index") for item in self.stream_metadata]
        if stream_ids and (
            any(type(index) is not int or index < 0 for index in stream_ids)
            or len(stream_ids) != len(set(stream_ids))
            or sum(item.get("codec_type") == "video" for item in self.stream_metadata) != int(self.has_video)
            or sum(item.get("codec_type") == "audio" for item in self.stream_metadata) != self.audio_stream_count
        ):
            raise VideoEditingError("MEDIA_ASSET_STREAM_INVENTORY_INVALID")
        _json_safe_copy(dict(self.format_metadata))

    @property
    def media_kind(self) -> str:
        if self.is_still_image:
            return "image"
        if self.has_video:
            return "video_audio" if self.audio_stream_count else "video"
        return "audio"

    @property
    def video_stream_count(self) -> int:
        if self.stream_metadata:
            return sum(item.get("codec_type") == "video" for item in self.stream_metadata)
        return int(self.has_video)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        # Never expose an absolute local path through the source-backed UI contract.
        return {
            "asset_id": self.asset_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "duration_sec": None if self.duration_sec is None else _time_json(self.duration_sec),
            **({"duration_basis": "none"} if self.is_still_image else {}),
            "media_kind": self.media_kind,
            "has_video": self.has_video,
            "audio_stream_count": self.audio_stream_count,
            "evidence_refs": list(self.evidence_refs),
            "rights_status": self.rights_status,
            "source_material_id": self.source_material_id,
            "streams": [_json_safe_copy(item) for item in self.stream_metadata],
            "format": _json_safe_copy(dict(self.format_metadata)),
            **({"speech_source": _json_safe_copy(self.speech_source)}
               if self.speech_source is not None else {}),
        }


def media_stream_origin(asset: MediaAsset, codec_type: str, stream_ordinal: int) -> Fraction:
    """Return the selected stream's absolute source PTS time when available."""
    streams = [item for item in asset.stream_metadata if item.get("codec_type") == codec_type]
    if not streams:
        return Fraction(0)
    if type(stream_ordinal) is not int or not 0 <= stream_ordinal < len(streams):
        raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID")
    stream = streams[stream_ordinal]
    start_pts = stream.get("start_pts")
    time_base = stream.get("time_base")
    if type(start_pts) is int and isinstance(time_base, dict):
        stream_start = Fraction(start_pts * time_base["num"], time_base["den"])
    elif isinstance(stream.get("start_time"), dict):
        value = stream["start_time"]
        stream_start = Fraction(value["num"], value["den"])
    else:
        return Fraction(0)
    return stream_start


def media_format_origin(asset: MediaAsset) -> Fraction:
    format_start = asset.format_metadata.get("start_time")
    return (Fraction(format_start["num"], format_start["den"])
            if isinstance(format_start, dict) else Fraction(0))


def media_stream_input_origin(asset: MediaAsset, codec_type: str, stream_ordinal: int) -> Fraction:
    """Return the selected stream's start relative to its container clock."""
    if not asset.stream_metadata:
        return Fraction(0)
    return media_stream_origin(asset, codec_type, stream_ordinal) - media_format_origin(asset)


def fingerprint_local_media(
    path: str,
    *,
    duration_sec: Optional[float] = None,
    evidence_refs: Iterable[str] = (),
    rights_status: Optional[str] = None,
    source_material_id: Optional[str] = None,
    display_filename: Optional[str] = None,
) -> MediaAsset:
    """Fingerprint local bytes and probe duration; caller-supplied claims fail closed.

    Evidence and rights stay unasserted until their canonical owners can bind
    them to these exact bytes. Legacy metadata parameters remain temporarily
    accepted only to return explicit errors instead of silently trusting them.
    """
    if duration_sec is not None:
        raise VideoEditingError("CALLER_SUPPLIED_MEDIA_DURATION_NOT_TRUSTED")
    if tuple(evidence_refs):
        raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
    if rights_status not in (None, "", "unknown"):
        raise VideoEditingError("RIGHTS_STATUS_REQUIRES_CANONICAL_REVIEW")
    if source_material_id is not None and (not isinstance(source_material_id, str) or not source_material_id.strip()):
        raise VideoEditingError("MEDIA_ASSET_SOURCE_REFERENCE_INVALID")
    if display_filename is not None and (
        not isinstance(display_filename, str) or not display_filename
        or any(not char.isprintable() for char in display_filename)
    ):
        raise VideoEditingError("MEDIA_ASSET_FILENAME_INVALID")

    raw = str(path or "").strip()
    if raw.startswith(("//", "\\\\")):
        raise VideoEditingError("LOCAL_MEDIA_ONLY")
    parsed = urlsplit(raw)
    windows_drive = len(raw) >= 3 and raw[1] == ":" and raw[2] in {"\\", "/"}
    if not raw or (parsed.scheme and not windows_drive) or parsed.netloc:
        raise VideoEditingError("LOCAL_MEDIA_ONLY")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise VideoEditingError("LOCAL_MEDIA_MUST_BE_ABSOLUTE")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VideoEditingError("LOCAL_MEDIA_NOT_FOUND") from exc
    if not resolved.is_file():
        raise VideoEditingError("LOCAL_MEDIA_NOT_FILE")

    try:
        before = resolved.stat()
    except OSError as exc:
        raise VideoEditingError("LOCAL_MEDIA_NOT_READABLE") from exc
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VideoEditingError("LOCAL_MEDIA_NOT_READABLE") from exc
    probe = _probe_media_info(resolved)
    duration = probe.duration_sec
    try:
        after = resolved.stat()
    except OSError as exc:
        raise VideoEditingError("LOCAL_MEDIA_CHANGED_DURING_FINGERPRINT") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise VideoEditingError("LOCAL_MEDIA_CHANGED_DURING_FINGERPRINT")

    media_hash = digest.hexdigest()
    asset_id = f"MEDIA-{media_hash[:16]}"
    if source_material_id is not None:
        asset_id = f"{asset_id}-{source_material_id}"
    speech_source = None
    probed_formats = set(str(probe.format.get("format_name", "")).lower().split(","))
    wav_source = (
        resolved.suffix.lower() == ".wav"
        or Path(display_filename or "").suffix.lower() == ".wav"
        or "wav" in probed_formats
    )
    if wav_source and before.st_size <= 10 * 1024 * 1024:
        from .media_speech import MediaSpeechError, _read_tts_source
        try:
            declaration_source = resolved.read_bytes()
            if hashlib.sha256(declaration_source).hexdigest() != media_hash:
                raise VideoEditingError("LOCAL_MEDIA_CHANGED_DURING_FINGERPRINT")
            speech_source = _read_tts_source(declaration_source)
        except MediaSpeechError as exc:
            raise VideoEditingError("MEDIA_SPEECH_SOURCE_DECLARATION_INVALID") from exc
        except OSError as exc:
            raise VideoEditingError("LOCAL_MEDIA_NOT_READABLE") from exc
    return MediaAsset(
        asset_id=asset_id,
        filename=display_filename or resolved.name,
        sha256=media_hash,
        duration_sec=duration,
        path=str(resolved),
        evidence_refs=(),
        rights_status="unknown",
        has_video=probe.has_video,
        audio_stream_count=probe.audio_stream_count,
        source_material_id=source_material_id,
        stream_metadata=probe.streams,
        format_metadata=probe.format,
        speech_source=speech_source,
        _factory_token=_MEDIA_ASSET_FACTORY_TOKEN,
    )


def _ffprobe_path() -> Optional[str]:
    """Return only the hash-verified ffprobe kept under this product root."""
    return _installed_media_tool("ffprobe.exe", _EXPECTED_FFPROBE_SHA256)


def _ffmpeg_path() -> Optional[str]:
    """Return only the hash-verified FFmpeg kept under this product root."""
    return _installed_media_tool("ffmpeg.exe", _EXPECTED_FFMPEG_SHA256)


def _installed_media_tool(filename: str, expected_sha256: str) -> Optional[str]:
    cached = _TOOL_PATHS.get(filename)
    if cached is not None and Path(cached).is_file():
        return cached
    candidate = _MEDIA_TOOLS_DIR / filename
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(_MEDIA_TOOLS_DIR.resolve()) or not resolved.is_file():
            _TOOL_PATHS[filename] = None
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().upper() != expected_sha256:
            _TOOL_PATHS[filename] = None
            return None
        _TOOL_PATHS[filename] = str(resolved)
        return _TOOL_PATHS[filename]
    except OSError:
        _TOOL_PATHS[filename] = None
        return None


def _probe_media_info(path: Path) -> _MediaProbe:
    """Read real local audio/video streams and duration using the existing FFprobe."""
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise VideoEditingError("MEDIA_DURATION_PROBE_UNAVAILABLE")
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-protocol_whitelist", "file",
                "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,time_base,start_pts,start_time,duration,sample_rate,channels,channel_layout,r_frame_rate,avg_frame_rate,pix_fmt,bits_per_raw_sample,color_range,color_space,color_transfer,color_primaries,chroma_location:format=format_name,start_time,duration,bit_rate",
                "-of", "json",
                "-i", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if len(completed.stdout.encode("utf-8")) > 4 * 1024 * 1024:
            raise VideoEditingError("MEDIA_DURATION_PROBE_RESULT_TOO_LARGE")
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoEditingError("MEDIA_DURATION_PROBE_FAILED") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or len(streams) > 256:
        raise VideoEditingError("MEDIA_DURATION_PROBE_FAILED")
    video_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not video_streams and not audio_streams:
        raise VideoEditingError("LOCAL_MEDIA_HAS_NO_AUDIO_OR_VIDEO_STREAM")
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    format_names = set(str(format_info.get("format_name") or "").split(","))
    image_demuxer = next((item for item in STATIC_IMAGE_DEMUXERS if item in format_names), None)
    is_still_image = image_demuxer is not None
    if is_still_image:
        if (len(video_streams) != 1 or audio_streams
                or not _is_supported_static_image(path, image_demuxer)):
            raise VideoEditingError("STATIC_IMAGE_CONTAINER_NOT_SUPPORTED")
        duration = None
    else:
        duration_stream = video_streams[0] if video_streams else audio_streams[0]
        raw_duration = duration_stream.get("duration")
        if raw_duration in (None, "", "N/A"):
            raw_duration = format_info.get("duration")
        try:
            duration = _fraction_time(str(raw_duration), error_code="MEDIA_DURATION_PROBE_FAILED")
        except (TypeError, ValueError, VideoEditingError) as exc:
            raise VideoEditingError("MEDIA_DURATION_PROBE_FAILED") from exc
        if duration <= 0 or duration > MAX_EDIT_TIME_SEC:
            raise VideoEditingError("MEDIA_DURATION_PROBE_FAILED")
    def normalized_stream(stream: dict) -> Dict[str, Any]:
        index = stream.get("index")
        if type(index) is not int or index < 0:
            raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID")
        result = {key: value for key, value in stream.items() if value not in (None, "N/A")}
        if is_still_image:
            for key in ("duration", "duration_ts", "start_pts", "start_time", "time_base",
                        "r_frame_rate", "avg_frame_rate"):
                result.pop(key, None)
        for key in ("index", "width", "height", "channels", "start_pts", "sample_rate", "bits_per_raw_sample"):
            if key in result and isinstance(result[key], str):
                try:
                    result[key] = int(result[key])
                except ValueError:
                    result.pop(key, None)
        for key in ("time_base", "r_frame_rate", "avg_frame_rate"):
            raw = result.get(key)
            if isinstance(raw, str) and raw not in {"", "0/0"}:
                try:
                    result[key] = _time_json(Fraction(raw))
                except (ValueError, ZeroDivisionError) as exc:
                    raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID") from exc
        for key in ("start_time", "duration"):
            raw = result.get(key)
            if isinstance(raw, str):
                try:
                    result[key] = _time_json(_fraction_time(raw))
                except VideoEditingError:
                    result.pop(key, None)
        return _json_safe_copy(result)

    normalized_format: Dict[str, Any] = {
        key: format_info[key] for key in ("format_name", "bit_rate")
        if key in format_info and format_info[key] not in (None, "N/A")
    }
    if is_still_image:
        normalized_format["image_color_profile"] = _static_image_color_profile(path, image_demuxer)
    if not is_still_image and format_info.get("start_time") not in (None, "", "N/A"):
        try:
            normalized_format["start_time"] = _time_json(_fraction_time(format_info["start_time"]))
        except VideoEditingError:
            pass
    if not is_still_image and format_info.get("duration") not in (None, "", "N/A"):
        try:
            normalized_format["duration"] = _time_json(_fraction_time(format_info["duration"]))
        except VideoEditingError:
            pass
    if format_info.get("bit_rate") not in (None, "", "N/A"):
        try:
            normalized_format["bit_rate"] = int(format_info["bit_rate"])
        except (TypeError, ValueError):
            normalized_format.pop("bit_rate", None)
    return _MediaProbe(
        duration_sec=duration,
        has_video=bool(video_streams),
        audio_stream_count=len(audio_streams),
        streams=tuple(normalized_stream(stream) for stream in streams),
        format=normalized_format,
    )


def _probe_media_duration(path: Path) -> Optional[float]:
    """Return measured duration, or None for a verified still image."""
    duration = _probe_media_info(path).duration_sec
    return None if duration is None else float(duration)


@dataclass(frozen=True)
class AudioMix:
    volume: float = 1.0
    fade_in_sec: Fraction | float = 0
    fade_out_sec: Fraction | float = 0
    ducking: bool = False

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "fade_in_sec", _fraction_time(self.fade_in_sec, error_code="INVALID_AUDIO_MIX"))
        object.__setattr__(self, "fade_out_sec", _fraction_time(self.fade_out_sec, error_code="INVALID_AUDIO_MIX"))

    def validate(self, *, duration_sec: Optional[float] = None) -> None:
        _bounded_number(self.volume, error_code="INVALID_AUDIO_MIX", minimum=0, maximum=2)
        fade_in = _bounded_time(
            self.fade_in_sec,
            error_code="INVALID_AUDIO_MIX",
            minimum=0,
            maximum=MAX_EDIT_TIME_SEC,
        )
        fade_out = _bounded_time(
            self.fade_out_sec,
            error_code="INVALID_AUDIO_MIX",
            minimum=0,
            maximum=MAX_EDIT_TIME_SEC,
        )
        if type(self.ducking) is not bool:
            raise VideoEditingError("INVALID_AUDIO_MIX")
        if duration_sec is not None:
            duration = _bounded_time(
                duration_sec,
                error_code="INVALID_AUDIO_MIX",
                minimum=Fraction(1, 1_000_000),
                maximum=MAX_EDIT_TIME_SEC,
            )
            if fade_in > duration or fade_out > duration:
                raise VideoEditingError("INVALID_AUDIO_MIX")
            if fade_in + fade_out > duration:
                raise VideoEditingError("INVALID_AUDIO_MIX")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "volume": self.volume,
            "fade_in_sec": _time_json(self.fade_in_sec),
            "fade_out_sec": _time_json(self.fade_out_sec),
            "ducking": self.ducking,
        }


@dataclass(frozen=True)
class AudioBusSend:
    bus_id: str
    gain: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, str) or not self.bus_id.strip() or len(self.bus_id) > 128:
            raise VideoEditingError("AUDIO_BUS_SEND_INVALID")
        _bounded_number(self.gain, error_code="AUDIO_BUS_SEND_INVALID", minimum=0, maximum=4)

    @classmethod
    def from_dict(cls, value: Any) -> "AudioBusSend":
        if type(value) is not dict or set(value) - {"bus_id", "gain"} or "bus_id" not in value:
            raise VideoEditingError("AUDIO_BUS_SEND_INVALID")
        return cls(value["bus_id"], value.get("gain", 1))

    def to_dict(self) -> dict[str, Any]:
        self.__post_init__()
        return {"bus_id": self.bus_id, "gain": self.gain}


@dataclass(frozen=True)
class AudioBusEffect:
    effect_id: str
    kind: str
    parameters: Mapping[str, Any]
    enabled: bool = True

    def __post_init__(self) -> None:
        if (not isinstance(self.effect_id, str) or not self.effect_id.strip() or len(self.effect_id) > 128
                or self.kind not in {"highpass", "lowpass", "equalizer", "compressor", "limiter"}
                or type(self.enabled) is not bool or not isinstance(self.parameters, Mapping)):
            raise VideoEditingError("AUDIO_BUS_EFFECT_INVALID")
        raw = dict(self.parameters)
        allowed = {
            "highpass": {"frequency_hz", "poles"},
            "lowpass": {"frequency_hz", "poles"},
            "equalizer": {"frequency_hz", "gain_db", "q"},
            "compressor": {"threshold_db", "ratio", "attack_ms", "release_ms", "makeup_db", "knee_db"},
            "limiter": {"limit_dbfs", "attack_ms", "release_ms"},
        }[self.kind]
        if set(raw) - allowed:
            raise VideoEditingError("AUDIO_BUS_EFFECT_PARAMETERS_INVALID")
        if self.kind in {"highpass", "lowpass"}:
            poles = raw.get("poles", 2)
            if type(poles) is not int or not 1 <= poles <= 8:
                raise VideoEditingError("AUDIO_BUS_EFFECT_PARAMETERS_INVALID")
            normalized = {
                "frequency_hz": _bounded_number(raw.get("frequency_hz"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                                minimum=10, maximum=20000),
                "poles": poles,
            }
        elif self.kind == "equalizer":
            normalized = {
                "frequency_hz": _bounded_number(raw.get("frequency_hz"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                                minimum=20, maximum=20000),
                "gain_db": _bounded_number(raw.get("gain_db"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                            minimum=-24, maximum=24),
                "q": _bounded_number(raw.get("q", 1), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                     minimum=0.1, maximum=10),
            }
        elif self.kind == "compressor":
            normalized = {
                "threshold_db": _bounded_number(raw.get("threshold_db"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                                 minimum=-60, maximum=0),
                "ratio": _bounded_number(raw.get("ratio"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                         minimum=1, maximum=20),
                "attack_ms": _bounded_number(raw.get("attack_ms", 20), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                              minimum=0.1, maximum=500),
                "release_ms": _bounded_number(raw.get("release_ms", 250), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                               minimum=1, maximum=5000),
                "makeup_db": _bounded_number(raw.get("makeup_db", 0), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                              minimum=-12, maximum=24),
                "knee_db": _bounded_number(raw.get("knee_db", 2), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                             minimum=1, maximum=8),
            }
        else:
            normalized = {
                "limit_dbfs": _bounded_number(raw.get("limit_dbfs"), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                               minimum=-12, maximum=0),
                "attack_ms": _bounded_number(raw.get("attack_ms", 5), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                              minimum=0.1, maximum=100),
                "release_ms": _bounded_number(raw.get("release_ms", 50), error_code="AUDIO_BUS_EFFECT_PARAMETERS_INVALID",
                                               minimum=1, maximum=500),
            }
        object.__setattr__(self, "parameters", normalized)

    @classmethod
    def from_dict(cls, value: Any) -> "AudioBusEffect":
        if type(value) is not dict or set(value) - {"effect_id", "kind", "parameters", "enabled"}:
            raise VideoEditingError("AUDIO_BUS_EFFECT_INVALID")
        if not {"effect_id", "kind", "parameters"} <= set(value):
            raise VideoEditingError("AUDIO_BUS_EFFECT_INVALID")
        return cls(value["effect_id"], value["kind"], value["parameters"], value.get("enabled", True))

    def to_dict(self) -> dict[str, Any]:
        return {"effect_id": self.effect_id, "kind": self.kind,
                "parameters": _json_safe_copy(self.parameters), "enabled": self.enabled}


@dataclass(frozen=True)
class AudioBus:
    bus_id: str
    name: str
    output_bus_id: Optional[str] = None
    gain: float = 1.0
    pan: float = 0.0
    effects: Tuple[AudioBusEffect, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "effects", tuple(self.effects))

    def validate(self) -> None:
        if (not isinstance(self.bus_id, str) or not self.bus_id.strip() or len(self.bus_id) > 128
                or not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128):
            raise VideoEditingError("AUDIO_BUS_INVALID")
        if self.bus_id == "master":
            if self.output_bus_id is not None:
                raise VideoEditingError("AUDIO_MASTER_BUS_CANNOT_ROUTE")
        elif self.output_bus_id not in (None, "master"):
            raise VideoEditingError("AUDIO_BUS_ROUTE_NOT_SUPPORTED")
        _bounded_number(self.gain, error_code="AUDIO_BUS_GAIN_INVALID", minimum=0, maximum=4)
        _bounded_number(self.pan, error_code="AUDIO_BUS_PAN_INVALID", minimum=-1, maximum=1)
        if not isinstance(self.effects, tuple) or len(self.effects) > 16:
            raise VideoEditingError("AUDIO_BUS_EFFECT_LIMIT_EXCEEDED")
        ids = set()
        for effect in self.effects:
            if not isinstance(effect, AudioBusEffect):
                raise VideoEditingError("AUDIO_BUS_EFFECT_INVALID")
            if effect.effect_id in ids:
                raise VideoEditingError("AUDIO_BUS_EFFECT_ID_ALREADY_EXISTS")
            ids.add(effect.effect_id)

    @classmethod
    def from_dict(cls, value: Any) -> "AudioBus":
        if type(value) is not dict or set(value) - {"bus_id", "name", "output_bus_id", "gain", "pan", "effects"}:
            raise VideoEditingError("AUDIO_BUS_INVALID")
        if not {"bus_id", "name"} <= set(value):
            raise VideoEditingError("AUDIO_BUS_INVALID")
        return cls(
            value["bus_id"], value["name"], value.get("output_bus_id"), value.get("gain", 1), value.get("pan", 0),
            tuple(AudioBusEffect.from_dict(item) for item in value.get("effects", [])),
        )

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {"bus_id": self.bus_id, "name": self.name,
                **({"output_bus_id": self.output_bus_id} if self.output_bus_id is not None else {}),
                "gain": self.gain, "pan": self.pan,
                "effects": [effect.to_dict() for effect in self.effects]}


@dataclass(frozen=True)
class FrameCrop:
    """User-selected normalized region in the display-oriented source frame."""
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        for value in (self.x,self.y):
            _bounded_number(value,error_code="FRAME_CROP_INVALID",minimum=0,maximum=1)
        for value in (self.width,self.height):
            _bounded_number(value,error_code="FRAME_CROP_INVALID",minimum=.05,maximum=1)
        if self.x+self.width>1+1e-9 or self.y+self.height>1+1e-9:
            raise VideoEditingError("FRAME_CROP_OUTSIDE_SOURCE")

    def to_dict(self):
        self.__post_init__();return dict(self.__dict__)

    @classmethod
    def from_dict(cls,value):
        if type(value) is not dict or set(value)!={"x","y","width","height"}:
            raise VideoEditingError("FRAME_CROP_INVALID")
        return cls(**value)


@dataclass(frozen=True)
class VideoPlacement:
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0
    opacity: float = 1.0

    def __post_init__(self):
        for value in (self.x, self.y, self.opacity):
            _bounded_number(value, error_code="VIDEO_PLACEMENT_INVALID", minimum=0, maximum=1)
        for value in (self.width, self.height):
            _bounded_number(value, error_code="VIDEO_PLACEMENT_INVALID", minimum=.01, maximum=1)
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise VideoEditingError("VIDEO_PLACEMENT_OUTSIDE_CANVAS")

    def to_dict(self):
        self.__post_init__()
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {"x", "y", "width", "height", "opacity"}:
            raise VideoEditingError("VIDEO_PLACEMENT_INVALID")
        return cls(**value)


@dataclass(frozen=True)
class VideoEffect:
    effect_id: str
    kind: str
    parameters: Mapping[str, Any]
    enabled: bool = True
    keyframes: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "parameters", self._normalized_parameters())
        object.__setattr__(self, "keyframes", self._normalized_keyframes())

    def _normalized_parameters(self, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(self.parameters if parameters is None else parameters)
        allowed = {
            "exposure": {"exposure_stops", "black_level"},
            "white_balance": {"temperature_kelvin", "tint", "mix", "preserve_lightness"},
            "color_balance": {"shadows", "midtones", "highlights", "preserve_lightness"},
            "curves": {"master", "red", "green", "blue", "interpolation"},
            "unsharp": {"luma_size_x", "luma_size_y", "luma_amount", "chroma_size_x", "chroma_size_y", "chroma_amount"},
            "mask": {"shape", "x", "y", "width", "height", "feather", "invert"},
        }.get(self.kind)
        if allowed is None:
            raise VideoEditingError("VIDEO_EFFECT_NOT_SUPPORTED")
        if set(raw) - allowed:
            raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
        if self.kind == "exposure":
            if "exposure_stops" not in raw:
                raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
            return {
                "exposure_stops": _bounded_number(raw["exposure_stops"], error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-3, maximum=3),
                "black_level": _bounded_number(raw.get("black_level", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-1, maximum=1),
            }
        if self.kind == "white_balance":
            if "temperature_kelvin" not in raw:
                raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
            temperature = _bounded_number(raw["temperature_kelvin"], error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=1000, maximum=40000)
            return {
                "temperature_kelvin": temperature,
                "tint": _bounded_number(raw.get("tint", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-1, maximum=1),
                "mix": _bounded_number(raw.get("mix", 1), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=0, maximum=1),
                "preserve_lightness": _bounded_number(raw.get("preserve_lightness", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=0, maximum=1),
            }
        if self.kind == "color_balance":
            normalized = {}
            for region in ("shadows", "midtones", "highlights"):
                values = raw.get(region, {})
                if not isinstance(values, Mapping) or set(values) - {"red", "green", "blue"}:
                    raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
                normalized[region] = {
                    channel: _bounded_number(values.get(channel, 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-1, maximum=1)
                    for channel in ("red", "green", "blue")
                }
            if not any(any(value != 0 for value in region.values()) for region in normalized.values()):
                raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
            if type(raw.get("preserve_lightness", False)) is not bool:
                raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
            normalized["preserve_lightness"] = raw.get("preserve_lightness", False)
            return normalized
        if self.kind == "curves":
            normalized = {}
            for channel in ("master", "red", "green", "blue"):
                if channel not in raw:
                    continue
                points = raw[channel]
                if type(points) is not list or not 2 <= len(points) <= 32:
                    raise VideoEditingError("VIDEO_EFFECT_CURVE_INVALID")
                curve = []
                previous_x = Fraction(-1)
                for pair in points:
                    if type(pair) is not list or len(pair) != 2:
                        raise VideoEditingError("VIDEO_EFFECT_CURVE_INVALID")
                    x = _bounded_number(pair[0], error_code="VIDEO_EFFECT_CURVE_INVALID", minimum=0, maximum=1)
                    y = _bounded_number(pair[1], error_code="VIDEO_EFFECT_CURVE_INVALID", minimum=0, maximum=1)
                    if Fraction(str(x)) <= previous_x:
                        raise VideoEditingError("VIDEO_EFFECT_CURVE_INVALID")
                    previous_x = Fraction(str(x))
                    curve.append([x, y])
                if curve[0][0] != 0 or curve[-1][0] != 1:
                    raise VideoEditingError("VIDEO_EFFECT_CURVE_ENDPOINTS_REQUIRED")
                normalized[channel] = curve
            if not normalized:
                raise VideoEditingError("VIDEO_EFFECT_CURVE_INVALID")
            interpolation = raw.get("interpolation", "natural")
            if not isinstance(interpolation, str) or interpolation not in {"natural", "pchip"}:
                raise VideoEditingError("VIDEO_EFFECT_CURVE_INTERPOLATION_INVALID")
            normalized["interpolation"] = interpolation
            return normalized
        if self.kind == "unsharp":
            def size(name: str, default: int) -> int:
                value = raw.get(name, default)
                if type(value) is not int or value < 3 or value > 23 or value % 2 == 0:
                    raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
                return value
            return {
                "luma_size_x": size("luma_size_x", 5),
                "luma_size_y": size("luma_size_y", 5),
                "luma_amount": _bounded_number(raw.get("luma_amount", 1), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-2, maximum=5),
                "chroma_size_x": size("chroma_size_x", 5),
                "chroma_size_y": size("chroma_size_y", 5),
                "chroma_amount": _bounded_number(raw.get("chroma_amount", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=-2, maximum=5),
            }
        if self.kind == "mask":
            shape = raw.get("shape")
            if not isinstance(shape, str) or shape not in {"rectangle", "ellipse"}:
                raise VideoEditingError("VIDEO_MASK_SHAPE_NOT_SUPPORTED")
            x = _bounded_number(raw.get("x", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=0, maximum=1)
            y = _bounded_number(raw.get("y", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=0, maximum=1)
            width = _bounded_number(raw.get("width", 1), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=.01, maximum=1)
            height = _bounded_number(raw.get("height", 1), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=.01, maximum=1)
            if x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
                raise VideoEditingError("VIDEO_MASK_OUTSIDE_SOURCE")
            invert = raw.get("invert", False)
            if type(invert) is not bool:
                raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
            return {
                "shape": shape, "x": x, "y": y, "width": width, "height": height,
                "feather": _bounded_number(raw.get("feather", 0), error_code="VIDEO_EFFECT_PARAMETERS_INVALID", minimum=0, maximum=.25),
                "invert": invert,
            }
        raise VideoEditingError("VIDEO_EFFECT_NOT_SUPPORTED")

    def _normalized_keyframes(self) -> Tuple[Dict[str, Any], ...]:
        if not isinstance(self.keyframes, (tuple, list)) or len(self.keyframes) > 256:
            raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_LIMIT_EXCEEDED")
        result = []
        previous_time = None
        for index, item in enumerate(self.keyframes):
            if (not isinstance(item, Mapping) or set(item) != {"time_sec", "parameters", "interpolation"}
                    or not isinstance(item["parameters"], Mapping)):
                raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_INVALID")
            time_sec = _bounded_time(item["time_sec"], error_code="VIDEO_EFFECT_KEYFRAME_INVALID",
                                     minimum=0, maximum=MAX_EDIT_TIME_SEC)
            if (previous_time is not None and time_sec <= previous_time
                    or previous_time is None and time_sec != 0):
                raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_ORDER_INVALID")
            interpolation = item["interpolation"]
            if not isinstance(interpolation, str) or interpolation not in {"hold", "linear"}:
                raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_INTERPOLATION_UNSUPPORTED")
            if interpolation == "linear" and self.kind not in {
                "exposure", "white_balance", "color_balance", "curves",
            }:
                raise VideoEditingError("VIDEO_EFFECT_LINEAR_INTERPOLATION_UNSUPPORTED")
            result.append({
                "time_sec": _time_json(time_sec),
                "parameters": self._normalized_parameters(item["parameters"]),
                "interpolation": interpolation,
            })
            previous_time = time_sec
        for left, right in zip(result, result[1:]):
            if left["interpolation"] != "linear" or self.kind != "curves":
                continue
            left_parameters = left["parameters"]
            right_parameters = right["parameters"]
            left_channels = set(left_parameters) - {"interpolation"}
            right_channels = set(right_parameters) - {"interpolation"}
            if left_channels != right_channels or left_parameters["interpolation"] != right_parameters["interpolation"]:
                raise VideoEditingError("VIDEO_EFFECT_CURVE_KEYFRAME_TOPOLOGY_MISMATCH")
            for channel in left_channels:
                left_points, right_points = left_parameters[channel], right_parameters[channel]
                if (len(left_points) != len(right_points)
                        or any(left_point[0] != right_point[0]
                               for left_point, right_point in zip(left_points, right_points, strict=True))):
                    raise VideoEditingError("VIDEO_EFFECT_CURVE_KEYFRAME_TOPOLOGY_MISMATCH")
        return tuple(result)

    def validate(self) -> None:
        if (not isinstance(self.effect_id, str) or not self.effect_id.strip() or len(self.effect_id) > 128
                or type(self.enabled) is not bool or not isinstance(self.kind, str)
                or self.kind not in {"exposure", "white_balance", "color_balance", "curves", "unsharp", "mask"}
                or not isinstance(self.parameters, Mapping)):
            raise VideoEditingError("VIDEO_EFFECT_INVALID")
        parameters = dict(self.parameters)
        allowed = {
            "exposure": {"exposure_stops", "black_level"},
            "white_balance": {"temperature_kelvin", "tint", "mix", "preserve_lightness"},
            "color_balance": {"shadows", "midtones", "highlights", "preserve_lightness"},
            "curves": {"master", "red", "green", "blue", "interpolation"},
            "unsharp": {"luma_size_x", "luma_size_y", "luma_amount", "chroma_size_x", "chroma_size_y", "chroma_amount"},
            "mask": {"shape", "x", "y", "width", "height", "feather", "invert"},
        }[self.kind]
        if set(parameters) - allowed:
            raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
        if self.kind == "exposure" and "exposure_stops" not in parameters:
            raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
        if self.kind == "white_balance" and "temperature_kelvin" not in parameters:
            raise VideoEditingError("VIDEO_EFFECT_PARAMETERS_INVALID")
        self._normalized_parameters()
        self._normalized_keyframes()

    @classmethod
    def from_dict(cls, value: Any) -> "VideoEffect":
        if type(value) is not dict or set(value) - {"effect_id", "kind", "parameters", "enabled", "keyframes"}:
            raise VideoEditingError("VIDEO_EFFECT_INVALID")
        if not {"effect_id", "kind", "parameters"} <= set(value):
            raise VideoEditingError("VIDEO_EFFECT_INVALID")
        return cls(value["effect_id"], value["kind"], value["parameters"], value.get("enabled", True),
                   value.get("keyframes", ()))

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "effect_id": self.effect_id,
            "kind": self.kind,
            "parameters": _json_safe_copy(self.parameters),
            "enabled": self.enabled,
            **({"keyframes": _json_safe_copy(self.keyframes)} if self.keyframes else {}),
        }


def _interpolate_effect_parameters(kind: str, left: Mapping[str, Any], right: Mapping[str, Any],
                                   ratio: Fraction) -> Dict[str, Any]:
    def number(a: float, b: float) -> Fraction:
        return (Fraction(str(a)) + (Fraction(str(b)) - Fraction(str(a))) * ratio).limit_denominator(10**12)

    if kind in {"exposure", "white_balance"}:
        return {key: number(left[key], right[key]) for key in left}
    if kind == "color_balance":
        return {
            **{
                region: {
                    channel: number(left[region][channel], right[region][channel])
                    for channel in ("red", "green", "blue")
                }
                for region in ("shadows", "midtones", "highlights")
            },
            "preserve_lightness": left["preserve_lightness"],
        }
    if kind == "curves":
        return {
            **{
                channel: [[x, number(left_points[index][1], right_points[index][1])]
                          for index, (x, _y) in enumerate(left_points)]
                for channel, left_points in left.items() if channel != "interpolation"
                for right_points in (right[channel],)
            },
            "interpolation": left["interpolation"],
        }
    raise VideoEditingError("VIDEO_EFFECT_LINEAR_INTERPOLATION_UNSUPPORTED")


def _effect_parameters_at(effect: VideoEffect, time_sec: Fraction) -> Mapping[str, Any]:
    if not effect.keyframes:
        return effect.parameters
    keyframes = effect.keyframes
    if time_sec <= _fraction_time(keyframes[0]["time_sec"]):
        return keyframes[0]["parameters"]
    for left, right in zip(keyframes, keyframes[1:]):
        right_time = _fraction_time(right["time_sec"])
        if time_sec < right_time:
            if left["interpolation"] == "hold":
                return left["parameters"]
            left_time = _fraction_time(left["time_sec"])
            ratio = (time_sec - left_time) / (right_time - left_time)
            return _interpolate_effect_parameters(effect.kind, left["parameters"], right["parameters"], ratio)
    return keyframes[-1]["parameters"]


def _effect_interpolation_at(effect: VideoEffect, time_sec: Fraction) -> str:
    if not effect.keyframes:
        return "hold"
    keyframes = effect.keyframes
    if time_sec <= _fraction_time(keyframes[0]["time_sec"]):
        return keyframes[0]["interpolation"]
    for left, right in zip(keyframes, keyframes[1:]):
        if time_sec < _fraction_time(right["time_sec"]):
            return left["interpolation"]
    return keyframes[-1]["interpolation"]


def _effect_parameters_json(value: Any) -> Any:
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _effect_parameters_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_effect_parameters_json(item) for item in value]
    return _json_safe_copy(value)


def _slice_effect_keyframes(
    effect: VideoEffect, start_sec: Fraction, end_sec: Fraction,
) -> Tuple[Dict[str, Any], ...]:
    if not effect.keyframes:
        return ()
    start = _fraction_time(start_sec, error_code="VIDEO_EFFECT_KEYFRAME_INVALID")
    end = _fraction_time(end_sec, error_code="VIDEO_EFFECT_KEYFRAME_INVALID")
    if start < 0 or end <= start:
        raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_INVALID")
    selected = _effect_parameters_at(effect, start)
    interpolation = _effect_interpolation_at(effect, start)
    exact_start = next((item for item in effect.keyframes
                        if _fraction_time(item["time_sec"]) == start), None)
    if exact_start is not None:
        interpolation = exact_start["interpolation"]
    result = [{"time_sec": _time_json(Fraction(0)), "parameters": _effect_parameters_json(selected),
               "interpolation": interpolation}]
    exact_end = None
    for keyframe in effect.keyframes:
        time_sec = _fraction_time(keyframe["time_sec"])
        if start < time_sec < end:
            result.append({"time_sec": _time_json(time_sec - start),
                           "parameters": _json_safe_copy(keyframe["parameters"]),
                           "interpolation": keyframe["interpolation"]})
        elif time_sec == end:
            exact_end = keyframe
            break
    if exact_end is not None:
        result.append({"time_sec": _time_json(end - start),
                       "parameters": _json_safe_copy(exact_end["parameters"]),
                       "interpolation": exact_end["interpolation"]})
    elif any(left["interpolation"] == "linear"
             and _fraction_time(left["time_sec"]) < end < _fraction_time(right["time_sec"])
             for left, right in zip(effect.keyframes, effect.keyframes[1:])):
        result.append({"time_sec": _time_json(end - start),
                       "parameters": _effect_parameters_json(_effect_parameters_at(effect, end)),
                       "interpolation": "hold"})
    return tuple(result)


@dataclass(frozen=True)
class VideoTransformKeyframe:
    time_sec: Fraction | float
    x: float
    y: float
    opacity: float
    interpolation: str = "linear"

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "time_sec", _fraction_time(self.time_sec, error_code="VIDEO_KEYFRAME_INVALID"))

    def validate(self) -> None:
        _bounded_time(self.time_sec, error_code="VIDEO_KEYFRAME_INVALID", minimum=0, maximum=MAX_EDIT_TIME_SEC)
        for value in (self.x, self.y, self.opacity):
            _bounded_number(value, error_code="VIDEO_KEYFRAME_INVALID", minimum=0, maximum=1)
        if not isinstance(self.interpolation, str) or self.interpolation not in {"linear", "hold"}:
            raise VideoEditingError("VIDEO_KEYFRAME_INTERPOLATION_INVALID")

    @classmethod
    def from_dict(cls, value: Any) -> "VideoTransformKeyframe":
        if type(value) is not dict or set(value) - {"time_sec", "x", "y", "opacity", "interpolation"}:
            raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
        if not {"time_sec", "x", "y", "opacity"} <= set(value):
            raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {"time_sec": _time_json(self.time_sec), "x": self.x, "y": self.y, "opacity": self.opacity,
                "interpolation": self.interpolation}


def _transform_state_at(
    keyframes: Tuple[VideoTransformKeyframe, ...], time_sec: Fraction,
) -> tuple[float, float, float, str]:
    if not keyframes:
        raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
    time_sec = _fraction_time(time_sec, error_code="VIDEO_KEYFRAME_INVALID")
    if time_sec <= _fraction_time(keyframes[0].time_sec):
        item = keyframes[0]
        return item.x, item.y, item.opacity, item.interpolation
    for left, right in zip(keyframes, keyframes[1:]):
        left_time, right_time = _fraction_time(left.time_sec), _fraction_time(right.time_sec)
        if time_sec <= right_time:
            if left.interpolation == "hold":
                return left.x, left.y, left.opacity, left.interpolation
            ratio = float((time_sec - left_time) / (right_time - left_time))
            return (
                left.x + (right.x - left.x) * ratio,
                left.y + (right.y - left.y) * ratio,
                left.opacity + (right.opacity - left.opacity) * ratio,
                left.interpolation,
            )
    item = keyframes[-1]
    return item.x, item.y, item.opacity, item.interpolation


def _slice_transform_keyframes(
    keyframes: Tuple[VideoTransformKeyframe, ...], start_sec: Fraction, end_sec: Fraction,
) -> Tuple[VideoTransformKeyframe, ...]:
    """Keep animated transform values continuous across trims and splits."""
    if not keyframes:
        return ()
    start = _fraction_time(start_sec, error_code="VIDEO_KEYFRAME_INVALID")
    end = _fraction_time(end_sec, error_code="VIDEO_KEYFRAME_INVALID")
    if start < 0 or end <= start:
        raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
    x, y, opacity, interpolation = _transform_state_at(keyframes, start)
    at_start = next((item for item in keyframes if _fraction_time(item.time_sec) == start), None)
    if at_start is not None:
        interpolation = at_start.interpolation
    result = [VideoTransformKeyframe(Fraction(0), x, y, opacity, interpolation)]
    exact_end = None
    for item in keyframes:
        item_time = _fraction_time(item.time_sec)
        if item_time <= start:
            continue
        if item_time < end:
            result.append(VideoTransformKeyframe(item_time - start, item.x, item.y, item.opacity,
                                                 item.interpolation))
        elif item_time == end:
            exact_end = item
            break
        else:
            break
    if exact_end is not None:
        result.append(VideoTransformKeyframe(end - start, exact_end.x, exact_end.y, exact_end.opacity,
                                             exact_end.interpolation))
    elif result[-1].time_sec != end - start:
        x, y, opacity, interpolation = _transform_state_at(keyframes, end)
        result.append(VideoTransformKeyframe(end - start, x, y, opacity, interpolation))
    return tuple(result)


@dataclass(frozen=True)
class TimelineClip:
    clip_id: str
    media_asset_id: str
    source_in: Fraction | float
    source_out: Fraction | float
    timeline_start: Fraction | float
    track_id: str = "video"
    purpose: str = ""
    audio_role: str = "original"
    audio_mix: AudioMix = field(default_factory=AudioMix)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    audio_stream_index: Optional[int] = None
    video_stream_index: Optional[int] = None
    frame_crop: Optional[FrameCrop] = None
    video_placement: VideoPlacement | None = None
    playback_rate: Fraction | float = 1
    effects: Tuple[VideoEffect, ...] = field(default_factory=tuple)
    transform_keyframes: Tuple[VideoTransformKeyframe, ...] = field(default_factory=tuple)
    display_duration_sec: Fraction | float | None = None

    def __post_init__(self) -> None:
        self.validate()
        for name in ("source_in", "source_out", "timeline_start", "playback_rate"):
            object.__setattr__(self, name, _fraction_time(getattr(self, name), error_code="INVALID_CLIP_RANGE"))
        if self.display_duration_sec is not None:
            object.__setattr__(self, "display_duration_sec",
                               _fraction_time(self.display_duration_sec, error_code="INVALID_CLIP_RANGE"))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "transform_keyframes", tuple(self.transform_keyframes))

    @property
    def duration_sec(self) -> Fraction:
        if self.display_duration_sec is not None:
            return _bounded_time(
                self.display_duration_sec, error_code="INVALID_CLIP_RANGE",
                minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC,
            )
        source_in = _bounded_time(
            self.source_in, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        source_out = _bounded_time(
            self.source_out, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        rate = _bounded_time(self.playback_rate, error_code="CLIP_PLAYBACK_RATE_INVALID", minimum=Fraction(1, 16), maximum=16)
        duration = (source_out - source_in) / rate
        if duration < Fraction(1, 1_000_000):
            raise VideoEditingError("INVALID_CLIP_RANGE")
        return duration

    def validate(self) -> None:
        rate = _bounded_time(self.playback_rate, error_code="CLIP_PLAYBACK_RATE_INVALID", minimum=Fraction(1, 16), maximum=16)
        source_in = _bounded_time(
            self.source_in, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        source_out = _bounded_time(
            self.source_out, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        timeline_start = _bounded_time(
            self.timeline_start, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        duration = ((source_out - source_in) / rate if self.display_duration_sec is None else
                    _bounded_time(self.display_duration_sec, error_code="INVALID_CLIP_RANGE",
                                  minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC))
        if (
            (self.display_duration_sec is None and duration < Fraction(1, 1_000_000))
            or (self.display_duration_sec is not None
                and (source_in != 0 or source_out != 0 or rate != 1))
            or timeline_start + duration > MAX_EDIT_TIME_SEC
            or not self.clip_id
            or not self.media_asset_id
            or not self.track_id
        ):
            raise VideoEditingError("INVALID_CLIP_RANGE")
        if self.audio_role not in {"original", "muted", "voiceover", "bgm", "sfx"}:
            raise VideoEditingError("AUDIO_TRACK_ROLE_NOT_ALLOWED")
        if self.audio_stream_index is not None and (
            type(self.audio_stream_index) is not int or self.audio_stream_index < 0
        ):
            raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
        if self.video_stream_index is not None and (
            type(self.video_stream_index) is not int or self.video_stream_index < 0
        ):
            raise VideoEditingError("VIDEO_STREAM_INDEX_OUT_OF_RANGE")
        if self.frame_crop is not None:
            if not isinstance(self.frame_crop,FrameCrop):raise VideoEditingError("FRAME_CROP_INVALID")
            self.frame_crop.to_dict()
        if self.video_placement is not None:
            if not isinstance(self.video_placement, VideoPlacement):
                raise VideoEditingError("VIDEO_PLACEMENT_INVALID")
            self.video_placement.to_dict()
        if not isinstance(self.effects, tuple) or len(self.effects) > 16:
            raise VideoEditingError("VIDEO_EFFECT_STACK_INVALID")
        effect_ids = set()
        effect_keyframe_count = 0
        mask_stack_started = False
        for effect in self.effects:
            if not isinstance(effect, VideoEffect):
                raise VideoEditingError("VIDEO_EFFECT_INVALID")
            effect.validate()
            if effect.effect_id in effect_ids:
                raise VideoEditingError("VIDEO_EFFECT_ID_ALREADY_EXISTS")
            if effect.kind == "mask":
                mask_stack_started = True
            elif mask_stack_started:
                raise VideoEditingError("VIDEO_MASKS_MUST_FOLLOW_COLOR_EFFECTS")
            effect_ids.add(effect.effect_id)
            effect_keyframe_count += len(effect.keyframes)
            for keyframe in effect.keyframes:
                if _fraction_time(keyframe["time_sec"], error_code="VIDEO_EFFECT_KEYFRAME_INVALID") > duration:
                    raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_OUTSIDE_CLIP")
        if effect_keyframe_count > 256:
            raise VideoEditingError("VIDEO_EFFECT_KEYFRAME_LIMIT_EXCEEDED")
        if not isinstance(self.transform_keyframes, tuple) or len(self.transform_keyframes) > 256:
            raise VideoEditingError("VIDEO_KEYFRAME_LIMIT_EXCEEDED")
        keyframe_times = []
        placement = self.video_placement or VideoPlacement()
        for keyframe in self.transform_keyframes:
            if not isinstance(keyframe, VideoTransformKeyframe):
                raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
            keyframe.validate()
            keyframe_time = _fraction_time(keyframe.time_sec, error_code="VIDEO_KEYFRAME_INVALID")
            if keyframe_time > duration:
                raise VideoEditingError("VIDEO_KEYFRAME_OUTSIDE_CLIP")
            if keyframe.x + placement.width > 1 + 1e-9 or keyframe.y + placement.height > 1 + 1e-9:
                raise VideoEditingError("VIDEO_KEYFRAME_OUTSIDE_CANVAS")
            keyframe_times.append(keyframe_time)
        if keyframe_times:
            if keyframe_times[0] != 0 or keyframe_times != sorted(keyframe_times) or len(set(keyframe_times)) != len(keyframe_times):
                raise VideoEditingError("VIDEO_KEYFRAME_ORDER_INVALID")
        if self.evidence_refs:
            raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
        if not isinstance(self.audio_mix, AudioMix):
            raise VideoEditingError("INVALID_AUDIO_MIX")
        self.audio_mix.validate(duration_sec=duration)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "clip_id": self.clip_id,
            "media_asset_id": self.media_asset_id,
            "source_in": _time_json(self.source_in),
            "source_out": _time_json(self.source_out),
            "timeline_start": _time_json(self.timeline_start),
            "track_id": self.track_id,
            "purpose": self.purpose,
            "audio_role": self.audio_role,
            "audio_mix": self.audio_mix.to_dict(),
            "evidence_refs": list(self.evidence_refs),
            "audio_stream_index": self.audio_stream_index,
            "video_stream_index": self.video_stream_index,
            **({"frame_crop":self.frame_crop.to_dict()} if self.frame_crop is not None else {}),
            **({"video_placement": self.video_placement.to_dict()} if self.video_placement is not None else {}),
            **({"playback_rate": _time_json(self.playback_rate)} if self.playback_rate != 1 else {}),
            **({"effects": [effect.to_dict() for effect in self.effects]} if self.effects else {}),
            **({"transform_keyframes": [item.to_dict() for item in self.transform_keyframes]}
               if self.transform_keyframes else {}),
            **({"display_duration_sec": _time_json(self.display_duration_sec)}
               if self.display_duration_sec is not None else {}),
        }


@dataclass(frozen=True)
class TimelineTrack:
    track_id: str
    kind: str
    clips: Tuple[TimelineClip, ...] = field(default_factory=tuple)
    layout: str = "sequence"
    locked: bool = False
    muted: bool = False
    solo: bool = False
    hidden: bool = False
    take_id: Optional[str] = None
    take_type: Optional[str] = None
    recording_session_id: Optional[str] = None
    cue_id: Optional[str] = None
    speaker_label: Optional[str] = None
    gain: float = 1.0
    pan: float = 0.0
    audio_bus_sends: Tuple[AudioBusSend, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.track_id, str) or not self.track_id.strip():
            raise VideoEditingError("TIMELINE_TRACK_ID_REQUIRED")
        if not isinstance(self.kind, str) or self.kind not in {"video", "voiceover", "bgm", "sfx"}:
            raise VideoEditingError("TIMELINE_TRACK_KIND_NOT_ALLOWED")
        if not isinstance(self.clips, tuple):
            raise VideoEditingError("TIMELINE_TRACK_CLIPS_INVALID")
        if self.layout not in {"sequence", "free"}:
            raise VideoEditingError("TIMELINE_TRACK_LAYOUT_INVALID")
        if any(type(value) is not bool for value in (self.locked,self.muted,self.solo,self.hidden)):
            raise VideoEditingError('TRACK_CONTROL_INVALID')
        _bounded_number(self.gain, error_code="AUDIO_TRACK_GAIN_INVALID", minimum=0, maximum=4)
        _bounded_number(self.pan, error_code="AUDIO_TRACK_PAN_INVALID", minimum=-1, maximum=1)
        if not isinstance(self.audio_bus_sends, tuple):
            raise VideoEditingError("AUDIO_TRACK_BUS_SENDS_INVALID")
        send_bus_ids = set()
        for send in self.audio_bus_sends:
            if not isinstance(send, AudioBusSend):
                raise VideoEditingError("AUDIO_BUS_SEND_INVALID")
            if send.bus_id in send_bus_ids:
                raise VideoEditingError("AUDIO_BUS_SEND_DUPLICATE")
            send_bus_ids.add(send.bus_id)
        if (self.take_id is not None or self.take_type is not None or self.recording_session_id is not None
                or self.cue_id is not None or self.speaker_label is not None):
            if self.kind == "video" or not isinstance(self.take_id, str) or not self.take_id.strip():
                raise VideoEditingError("AUDIO_TAKE_TRACK_METADATA_INVALID")
            if self.take_type not in {"recorded", "comp"}:
                raise VideoEditingError("AUDIO_TAKE_TRACK_METADATA_INVALID")
            if (self.take_type == "recorded") != (isinstance(self.recording_session_id, str)
                                                    and bool(self.recording_session_id)):
                raise VideoEditingError("AUDIO_TAKE_TRACK_METADATA_INVALID")
            if self.cue_id is not None and (
                not isinstance(self.cue_id, str) or not self.cue_id.strip() or len(self.cue_id) > 128
            ):
                raise VideoEditingError("AUDIO_TAKE_TRACK_METADATA_INVALID")
            if self.speaker_label is not None and (
                not isinstance(self.speaker_label, str) or not self.speaker_label.strip()
                or len(self.speaker_label) > 128 or any(not char.isprintable() for char in self.speaker_label)
            ):
                raise VideoEditingError("AUDIO_TAKE_TRACK_METADATA_INVALID")
        if self.hidden and self.kind != 'video':
            raise VideoEditingError('TRACK_VISIBILITY_VIDEO_ONLY')
        for clip in self.clips:
            if not isinstance(clip, TimelineClip):
                raise VideoEditingError("TIMELINE_TRACK_CLIPS_INVALID")
            clip.validate()
            if clip.frame_crop is not None and self.kind != "video":
                raise VideoEditingError("FRAME_CROP_VIDEO_ONLY")
            if clip.video_placement is not None and self.kind != "video":
                raise VideoEditingError("VIDEO_PLACEMENT_VIDEO_ONLY")
            if (clip.effects or clip.transform_keyframes) and self.kind != "video":
                raise VideoEditingError("VIDEO_EFFECTS_VIDEO_ONLY")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {"track_id": self.track_id, "kind": self.kind, "clips": [clip.to_dict() for clip in self.clips],
                **({"layout": self.layout} if self.layout != "sequence" else {}),
                **{name:True for name in ('locked','muted','solo','hidden') if getattr(self,name)},
                **{name: getattr(self, name) for name in
                   ("take_id", "take_type", "recording_session_id", "cue_id", "speaker_label")
                   if getattr(self, name) is not None},
                **({"gain": self.gain} if self.gain != 1 else {}),
                **({"pan": self.pan} if self.pan != 0 else {}),
                **({"audio_bus_sends": [item.to_dict() for item in self.audio_bus_sends]}
                   if self.audio_bus_sends else {})}


@dataclass(frozen=True)
class Transition:
    from_clip: str
    to_clip: str
    transition_type: str
    duration_sec: Fraction | float
    reason: str

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "duration_sec", _fraction_time(self.duration_sec, error_code="INVALID_TRANSITION_DURATION"))

    def validate(self) -> None:
        if (not isinstance(self.from_clip, str) or not self.from_clip
                or not isinstance(self.to_clip, str) or not self.to_clip
                or self.from_clip == self.to_clip):
            raise VideoEditingError("INVALID_TRANSITION")
        if (not isinstance(self.transition_type, str)
                or self.transition_type not in SUPPORTED_VIDEO_TRANSITION_TYPES):
            raise VideoEditingError("INVALID_TRANSITION")
        _bounded_time(
            self.duration_sec,
            error_code="INVALID_TRANSITION_DURATION",
            minimum=0,
            maximum=MAX_TRANSITION_DURATION_SEC,
        )
        if self.transition_type != "cut":
            if self.duration_sec <= 0:
                raise VideoEditingError("INVALID_TRANSITION_DURATION")
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise VideoEditingError("TRANSITION_REASON_REQUIRED")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "from_clip": self.from_clip,
            "to_clip": self.to_clip,
            "transition_type": self.transition_type,
            "duration_sec": _time_json(self.duration_sec),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CaptionStyle:
    """Bounded literal subtitle styling; no arbitrary FFmpeg/ASS expressions."""
    font_size: int = 36
    color: str = "#FFFFFF"
    background_color: str = "#000000"
    background_opacity: float = 0.6
    max_chars_per_line: int = 14
    max_lines: int = 2
    position: str = "bottom"
    safe_margin_ratio: float = 0.06

    def __post_init__(self):
        if type(self.font_size) is not int or not 16 <= self.font_size <= 96:
            raise VideoEditingError("CAPTION_STYLE_FONT_SIZE_INVALID")
        if any(not isinstance(value,str) or re.fullmatch(r"#[0-9A-Fa-f]{6}",value) is None
               for value in (self.color,self.background_color)):
            raise VideoEditingError("CAPTION_STYLE_COLOR_INVALID")
        _bounded_number(self.background_opacity,error_code="CAPTION_STYLE_OPACITY_INVALID",minimum=0,maximum=1)
        _bounded_number(self.safe_margin_ratio,error_code="CAPTION_STYLE_MARGIN_INVALID",minimum=.04,maximum=.15)
        if type(self.max_chars_per_line) is not int or not 1 <= self.max_chars_per_line <= 14:
            raise VideoEditingError("CAPTION_STYLE_LINE_LIMIT_INVALID")
        if type(self.max_lines) is not int or not 1 <= self.max_lines <= 4 or self.position not in {"top","bottom"}:
            raise VideoEditingError("CAPTION_STYLE_LAYOUT_INVALID")

    def to_dict(self):
        self.__post_init__()
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls,value):
        if type(value) is not dict or set(value)-set(cls.__dataclass_fields__):
            raise VideoEditingError("CAPTION_STYLE_INVALID")
        try:return cls(**value)
        except TypeError as exc:raise VideoEditingError("CAPTION_STYLE_INVALID") from exc


@dataclass(frozen=True)
class TimedOverlay:
    overlay_id: str
    kind: str
    start_sec: Fraction | float
    end_sec: Fraction | float
    text: str
    layout_profile: Optional[str] = None
    style: Optional[CaptionStyle] = None

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "start_sec", _fraction_time(self.start_sec, error_code="INVALID_TIMED_OVERLAY"))
        object.__setattr__(self, "end_sec", _fraction_time(self.end_sec, error_code="INVALID_TIMED_OVERLAY"))

    def validate(self) -> None:
        if self.style is not None:
            if not isinstance(self.style,CaptionStyle) or self.kind != "caption" or self.layout_profile is not None:
                raise VideoEditingError("CAPTION_STYLE_INVALID")
            self.style.to_dict()
        if self.layout_profile is not None and (
            not isinstance(self.layout_profile, str)
            or self.layout_profile != "literal-wide-720p-v2"
            or self.kind != "caption"
        ):
            raise VideoEditingError("OVERLAY_LAYOUT_PROFILE_NOT_SUPPORTED")
        start = _bounded_time(
            self.start_sec, error_code="INVALID_TIMED_OVERLAY", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        end = _bounded_time(
            self.end_sec, error_code="INVALID_TIMED_OVERLAY", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        if (
            end - start < Fraction(1, 1_000_000)
            or not isinstance(self.kind, str)
            or self.kind not in {"caption", "text"}
            or not str(self.text or "").strip()
        ):
            raise VideoEditingError("INVALID_TIMED_OVERLAY")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "overlay_id": self.overlay_id,
            "kind": self.kind,
            "start_sec": _time_json(self.start_sec),
            "end_sec": _time_json(self.end_sec),
            "text": self.text,
            **({"layout_profile": self.layout_profile} if self.layout_profile is not None else {}),
            **({"style": self.style.to_dict()} if self.style is not None else {}),
        }


@dataclass(frozen=True)
class TimelineChapter:
    chapter_id: str
    title: str
    start_sec: Fraction | float

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "start_sec", _fraction_time(self.start_sec, error_code="TIMELINE_CHAPTER_TIME_INVALID"))

    def validate(self) -> None:
        if (not isinstance(self.chapter_id, str) or not self.chapter_id.strip() or len(self.chapter_id) > 128
                or not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200
                or any(not char.isprintable() for char in self.title)):
            raise VideoEditingError("TIMELINE_CHAPTER_INVALID")
        _bounded_time(
            self.start_sec,
            error_code="TIMELINE_CHAPTER_TIME_INVALID",
            minimum=0,
            maximum=MAX_EDIT_TIME_SEC,
        )

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "start_sec": _time_json(self.start_sec),
        }


@dataclass(frozen=True)
class TranscriptWord:
    word_id: str
    text: str
    start_sample: int
    end_sample: int
    speaker_label: Optional[str] = None
    token_probability: Optional[float] = None

    def validate(self) -> None:
        if (not isinstance(self.word_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", self.word_id)
                or not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 1000
                or any(unicodedata.category(char).startswith("C") for char in self.text)
                or type(self.start_sample) is not int or type(self.end_sample) is not int
                or self.start_sample < 0 or self.end_sample <= self.start_sample):
            raise VideoEditingError("MEDIA_TRANSCRIPT_WORD_INVALID")
        if self.speaker_label is not None and (
            not isinstance(self.speaker_label, str) or not self.speaker_label.strip()
            or len(self.speaker_label) > 128
            or any(unicodedata.category(char).startswith("C") for char in self.speaker_label)
        ):
            raise VideoEditingError("MEDIA_TRANSCRIPT_SPEAKER_INVALID")
        if (self.token_probability is not None
                and (type(self.token_probability) not in (int, float)
                     or not math.isfinite(self.token_probability)
                     or not 0 <= self.token_probability <= 1)):
            raise VideoEditingError("MEDIA_TRANSCRIPT_TOKEN_PROBABILITY_INVALID")

    @classmethod
    def from_dict(cls, value: Any) -> "TranscriptWord":
        fields = {"word_id", "text", "start_sample", "end_sample"}
        if (type(value) is not dict or not fields <= set(value)
                or set(value) - fields - {"speaker_label", "token_probability"}):
            raise VideoEditingError("MEDIA_TRANSCRIPT_WORD_INVALID")
        word = cls(
            value["word_id"], value["text"], value["start_sample"], value["end_sample"],
            value.get("speaker_label"), value.get("token_probability"),
        )
        word.validate()
        return word

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "word_id": self.word_id,
            "text": self.text,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            **({"speaker_label": self.speaker_label} if self.speaker_label is not None else {}),
            **({"token_probability": self.token_probability}
               if self.token_probability is not None else {}),
        }


@dataclass(frozen=True)
class TimelineTranscript:
    transcript_id: str
    source_asset_id: str
    source_sha256: str
    audio_stream_index: int
    sample_rate: int
    language: str
    alignment_source: str
    words: Tuple[TranscriptWord, ...]
    audio_stream_start_pts: Optional[int] = None
    audio_stream_time_base: Optional[Tuple[int, int]] = None
    audio_channel_index: Optional[int] = None
    task: str = "transcribe"
    model_provenance: Optional[dict[str, Any]] = None
    review_state: str = "unreviewed"

    def validate(self) -> None:
        if (not isinstance(self.transcript_id, str) or not self.transcript_id.strip()
                or len(self.transcript_id) > 128
                or not isinstance(self.source_asset_id, str) or not self.source_asset_id.strip()
                or not isinstance(self.source_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
                or type(self.audio_stream_index) is not int or self.audio_stream_index < 0
                or type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 192000
                or not isinstance(self.language, str)
                or re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", self.language) is None
                or self.alignment_source not in {
                    "human_supplied_unverified", "whisper_base_unreviewed",
                }
                or self.task not in {"transcribe", "translate"}
                or self.review_state not in {"unreviewed", "human_corrected", "human_reviewed"}
                or not isinstance(self.words, tuple) or not 1 <= len(self.words) <= 100000):
            raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
        if self.audio_channel_index is not None and (
            type(self.audio_channel_index) is not int or self.audio_channel_index < 0
        ):
            raise VideoEditingError("MEDIA_TRANSCRIPT_CHANNEL_INVALID")
        if self.alignment_source == "whisper_base_unreviewed":
            provenance = self.model_provenance
            if (not isinstance(provenance, dict)
                    or provenance.get("engine") != "openai-whisper"
                    or provenance.get("version") != "20250625"
                    or provenance.get("model") != "base"
                    or provenance.get("model_sha256") !=
                       "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e"
                    or provenance.get("task") != self.task
                    or provenance.get("review_state") != self.review_state
                    or self.review_state not in {"unreviewed", "human_corrected", "human_reviewed"}
                    or provenance.get("timing_method") != "whisper_word_timestamp_estimate"
                    or provenance.get("quality_state") != "NOT_EVALUATED"):
                raise VideoEditingError("MEDIA_TRANSCRIPT_MODEL_PROVENANCE_INVALID")
        elif self.model_provenance is not None:
            raise VideoEditingError("MEDIA_TRANSCRIPT_MODEL_PROVENANCE_INVALID")
        if self.audio_stream_start_pts is not None and type(self.audio_stream_start_pts) is not int:
            raise VideoEditingError("MEDIA_TRANSCRIPT_CLOCK_INVALID")
        if self.audio_stream_time_base is not None and (
            not isinstance(self.audio_stream_time_base, tuple) or len(self.audio_stream_time_base) != 2
            or any(type(value) is not int for value in self.audio_stream_time_base)
            or self.audio_stream_time_base[0] <= 0 or self.audio_stream_time_base[1] <= 0
        ):
            raise VideoEditingError("MEDIA_TRANSCRIPT_CLOCK_INVALID")
        seen = set()
        previous_start = -1
        for word in self.words:
            if not isinstance(word, TranscriptWord):
                raise VideoEditingError("MEDIA_TRANSCRIPT_WORD_INVALID")
            word.validate()
            if word.word_id in seen or word.start_sample < previous_start:
                raise VideoEditingError("MEDIA_TRANSCRIPT_WORD_ORDER_INVALID")
            seen.add(word.word_id)
            previous_start = word.start_sample

    @classmethod
    def from_dict(cls, value: Any) -> "TimelineTranscript":
        required = {"transcript_id", "source_asset_id", "source_sha256", "audio_stream_index",
                    "sample_rate", "language", "alignment_source", "words",
                    "audio_stream_start_pts", "audio_stream_time_base"}
        allowed = required | {"audio_channel_index", "task", "model_provenance", "review_state"}
        if (type(value) is not dict or not required <= set(value) or set(value) - allowed
                or type(value["words"]) is not list):
            raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
        time_base = value["audio_stream_time_base"]
        if time_base is not None:
            if (type(time_base) is not dict or set(time_base) != {"num", "den"}
                    or type(time_base["num"]) is not int or type(time_base["den"]) is not int):
                raise VideoEditingError("MEDIA_TRANSCRIPT_CLOCK_INVALID")
            time_base_value = (time_base["num"], time_base["den"])
        else:
            time_base_value = None
        transcript = cls(
            value["transcript_id"], value["source_asset_id"], value["source_sha256"],
            value["audio_stream_index"], value["sample_rate"], value["language"],
            value["alignment_source"], tuple(TranscriptWord.from_dict(item) for item in value["words"]),
            value["audio_stream_start_pts"], time_base_value,
            value.get("audio_channel_index"), value.get("task", "transcribe"),
            value.get("model_provenance"), value.get("review_state", "unreviewed"),
        )
        transcript.validate()
        return transcript

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        result = {
            "transcript_id": self.transcript_id,
            "source_asset_id": self.source_asset_id,
            "source_sha256": self.source_sha256,
            "audio_stream_index": self.audio_stream_index,
            "sample_rate": self.sample_rate,
            "language": self.language,
            "alignment_source": self.alignment_source,
            "audio_stream_start_pts": self.audio_stream_start_pts,
            "audio_stream_time_base": (None if self.audio_stream_time_base is None else
                {"num": self.audio_stream_time_base[0], "den": self.audio_stream_time_base[1]}),
            "words": [word.to_dict() for word in self.words],
        }
        if self.audio_channel_index is not None:
            result["audio_channel_index"] = self.audio_channel_index
        if self.model_provenance is not None:
            result["task"] = self.task
            result["model_provenance"] = self.model_provenance
            result["review_state"] = self.review_state
        return result


@dataclass(frozen=True)
class VideoColorProfile:
    color_range: str
    color_space: str
    color_transfer: str
    color_primaries: str

    def validate(self) -> None:
        ranges = {"tv", "pc"}
        spaces = {"bt709", "fcc", "smpte170m", "bt470bg", "smpte240m", "bt2020nc", "bt2020c"}
        transfers = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "gamma22", "gamma28",
                     "linear", "log", "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1"}
        primaries = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film",
                     "bt2020", "smpte431", "smpte432", "jedec-p22"}
        if (self.color_range not in ranges or self.color_space not in spaces
                or self.color_transfer not in transfers or self.color_primaries not in primaries):
            raise VideoEditingError("VIDEO_COLOR_PROFILE_INVALID")

    @classmethod
    def from_dict(cls, value: Any) -> "VideoColorProfile":
        fields = {"color_range", "color_space", "color_transfer", "color_primaries"}
        if type(value) is not dict or set(value) != fields:
            raise VideoEditingError("VIDEO_COLOR_PROFILE_INVALID")
        profile = cls(value["color_range"], value["color_space"],
                      value["color_transfer"], value["color_primaries"])
        profile.validate()
        return profile

    def to_dict(self) -> Dict[str, str]:
        self.validate()
        return {"color_range": self.color_range, "color_space": self.color_space,
                "color_transfer": self.color_transfer, "color_primaries": self.color_primaries}


@dataclass(frozen=True)
class VideoColorInterpretation:
    asset_id: str
    source_sha256: str
    video_stream_index: int
    input_profile: VideoColorProfile
    working_profile: VideoColorProfile
    selected_by: str = "user_selected"

    def validate(self) -> None:
        if (not isinstance(self.asset_id, str) or not self.asset_id
                or not isinstance(self.source_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
                or type(self.video_stream_index) is not int or self.video_stream_index < 0
                or not isinstance(self.input_profile, VideoColorProfile)
                or not isinstance(self.working_profile, VideoColorProfile)
                or self.selected_by not in {"user_selected", "explicit_project_default"}):
            raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_INVALID")
        self.input_profile.validate()
        self.working_profile.validate()

    @classmethod
    def from_dict(cls, value: Any) -> "VideoColorInterpretation":
        fields = {"asset_id", "source_sha256", "video_stream_index", "input_profile",
                  "working_profile", "selected_by"}
        if type(value) is not dict or set(value) != fields:
            raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_INVALID")
        interpretation = cls(
            value["asset_id"], value["source_sha256"], value["video_stream_index"],
            VideoColorProfile.from_dict(value["input_profile"]),
            VideoColorProfile.from_dict(value["working_profile"]), value["selected_by"],
        )
        interpretation.validate()
        return interpretation

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {"asset_id": self.asset_id, "source_sha256": self.source_sha256,
                "video_stream_index": self.video_stream_index,
                "input_profile": self.input_profile.to_dict(),
                "working_profile": self.working_profile.to_dict(), "selected_by": self.selected_by}


@dataclass(frozen=True)
class _TimelineSnapshot:
    assets: Tuple[MediaAsset, ...]
    tracks: Tuple[TimelineTrack, ...]
    audio_buses: Tuple[AudioBus, ...]
    transcripts: Tuple[TimelineTranscript, ...]
    video_color_interpretations: Tuple[VideoColorInterpretation, ...]
    transitions: Tuple[Transition, ...]
    overlays: Tuple[TimedOverlay, ...]
    format_ratio: str
    chapters: Tuple[TimelineChapter, ...]


@dataclass(frozen=True)
class Timeline:
    assets: Tuple[MediaAsset, ...]
    tracks: Tuple[TimelineTrack, ...] = field(default_factory=lambda: (TimelineTrack("video", "video"),))
    transitions: Tuple[Transition, ...] = field(default_factory=tuple)
    overlays: Tuple[TimedOverlay, ...] = field(default_factory=tuple)
    format_ratio: str = "9:16"
    version: int = 1
    _history: Tuple[_TimelineSnapshot, ...] = field(default_factory=tuple, repr=False, compare=False)
    chapters: Tuple[TimelineChapter, ...] = field(default_factory=tuple)
    audio_buses: Tuple[AudioBus, ...] = field(default_factory=tuple)
    transcripts: Tuple[TimelineTranscript, ...] = field(default_factory=tuple)
    video_color_interpretations: Tuple[VideoColorInterpretation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.version) is not int or not 1 <= self.version <= MAX_SERIALIZED_INTEGER:
            raise VideoEditingError("TIMELINE_VERSION_INVALID")
        if self.format_ratio not in ALLOWED_FORMAT_RATIOS:
            raise VideoEditingError("FORMAT_RATIO_NOT_SUPPORTED")
        if not isinstance(self.assets, tuple):
            raise VideoEditingError("TIMELINE_REQUIRES_MEDIA_ASSET")
        if not isinstance(self.tracks, tuple) or not self.tracks:
            raise VideoEditingError("TIMELINE_TRACKS_INVALID")
        if (not isinstance(self.audio_buses, tuple) or not isinstance(self.transcripts, tuple)
                or not isinstance(self.video_color_interpretations, tuple)
                or not isinstance(self.transitions, tuple) or not isinstance(self.overlays, tuple)
                or not isinstance(self.chapters, tuple)):
            raise VideoEditingError("TIMELINE_COMPONENTS_INVALID")
        if len(self.audio_buses) > 128:
            raise VideoEditingError("AUDIO_BUS_LIMIT_EXCEEDED")
        if len(self.transcripts) > 256:
            raise VideoEditingError("MEDIA_TRANSCRIPT_LIMIT_EXCEEDED")
        if len(self.video_color_interpretations) > 128:
            raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_LIMIT_EXCEEDED")
        audio_buses: Dict[str, AudioBus] = {}
        for bus in self.audio_buses:
            if not isinstance(bus, AudioBus):
                raise VideoEditingError("AUDIO_BUS_INVALID")
            bus.validate()
            if bus.bus_id in audio_buses:
                raise VideoEditingError("AUDIO_BUS_ALREADY_EXISTS")
            audio_buses[bus.bus_id] = bus

        assets: Dict[str, MediaAsset] = {}
        for asset in self.assets:
            if not isinstance(asset, MediaAsset):
                raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
            asset.validate()
            if asset.asset_id in assets:
                raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
            assets[asset.asset_id] = asset

        transcript_ids = set()
        transcript_sources = set()
        for transcript in self.transcripts:
            if not isinstance(transcript, TimelineTranscript):
                raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
            transcript.validate()
            asset = assets.get(transcript.source_asset_id)
            if (asset is None or asset.is_still_image or asset.sha256 != transcript.source_sha256
                    or transcript.audio_stream_index >= asset.audio_stream_count):
                raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH")
            audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
            if transcript.audio_stream_index >= len(audio_streams):
                raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH")
            stream = audio_streams[transcript.audio_stream_index]
            sample_rate = stream.get("sample_rate")
            if type(sample_rate) is not int or sample_rate != transcript.sample_rate:
                raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_CLOCK_MISMATCH")
            channel_count = stream.get("channels")
            if transcript.audio_channel_index is not None and (
                type(channel_count) is not int or transcript.audio_channel_index >= channel_count
            ):
                raise VideoEditingError("MEDIA_TRANSCRIPT_CHANNEL_INVALID")
            stream_start_pts = stream.get("start_pts")
            if transcript.audio_stream_start_pts != stream_start_pts:
                raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_CLOCK_MISMATCH")
            raw_time_base = stream.get("time_base")
            expected_time_base = (
                (raw_time_base["num"], raw_time_base["den"])
                if isinstance(raw_time_base, dict) and type(raw_time_base.get("num")) is int
                and type(raw_time_base.get("den")) is int else None
            )
            if transcript.audio_stream_time_base != expected_time_base:
                raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_CLOCK_MISMATCH")
            if asset.duration_sec is not None:
                maximum_sample = asset.duration_sec * transcript.sample_rate
                if any(word.end_sample > maximum_sample for word in transcript.words):
                    raise VideoEditingError("MEDIA_TRANSCRIPT_WORD_OUTSIDE_SOURCE")
            source_stream = (
                transcript.source_asset_id,
                transcript.audio_stream_index,
                transcript.audio_channel_index,
            )
            if transcript.transcript_id in transcript_ids or source_stream in transcript_sources:
                raise VideoEditingError("MEDIA_TRANSCRIPT_DUPLICATE_SOURCE")
            transcript_ids.add(transcript.transcript_id)
            transcript_sources.add(source_stream)

        color_keys = set()
        working_profiles = set()
        for interpretation in self.video_color_interpretations:
            if not isinstance(interpretation, VideoColorInterpretation):
                raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_INVALID")
            interpretation.validate()
            asset = assets.get(interpretation.asset_id)
            if asset is None or asset.is_still_image or asset.sha256 != interpretation.source_sha256:
                raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_SOURCE_MISMATCH")
            video_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
            if interpretation.video_stream_index >= len(video_streams):
                raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_SOURCE_MISMATCH")
            stream = video_streams[interpretation.video_stream_index]
            pixel_format = stream.get("pix_fmt")
            bit_depth = stream.get("bits_per_raw_sample")
            if bit_depth is None:
                bit_depth = 8 if pixel_format == "yuv420p" else None
            if pixel_format != "yuv420p" or bit_depth != 8:
                raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_SOURCE_UNSUPPORTED")
            if stream.get("color_transfer") in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12", "smpte428"}:
                raise VideoEditingError("VIDEO_COLOR_HDR_INTERPRETATION_FORBIDDEN")
            if (stream.get("color_primaries") == "bt2020"
                    and stream.get("color_transfer") in (None, "", "unknown", "unspecified")):
                raise VideoEditingError("VIDEO_COLOR_HDR_STATE_UNKNOWN")
            for name in ("color_range", "color_space", "color_transfer", "color_primaries"):
                observed = stream.get(name)
                if observed not in (None, "", "unknown", "unspecified") and observed != getattr(interpretation.input_profile, name):
                    raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_CONFLICTS_SOURCE_METADATA")
            key = (interpretation.asset_id, interpretation.video_stream_index)
            if key in color_keys:
                raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_DUPLICATE_SOURCE")
            color_keys.add(key)
            working_profiles.add(interpretation.working_profile)
        if len(working_profiles) > 1:
            raise VideoEditingError("VIDEO_COLOR_WORKING_SPACE_MISMATCH")

        track_ids = set()
        clip_ids = set()
        take_ids = set()
        recording_session_ids = set()
        clips: Dict[str, TimelineClip] = {}
        for track in self.tracks:
            if not isinstance(track, TimelineTrack):
                raise VideoEditingError("TIMELINE_TRACKS_INVALID")
            track.validate()
            if track.track_id in track_ids:
                raise VideoEditingError("TIMELINE_TRACK_ALREADY_EXISTS")
            track_ids.add(track.track_id)
            if any(send.bus_id != "master" and send.bus_id not in audio_buses
                   for send in track.audio_bus_sends):
                raise VideoEditingError("AUDIO_BUS_SEND_TARGET_NOT_FOUND")
            if track.take_id is not None:
                if track.take_id in take_ids:
                    raise VideoEditingError("AUDIO_TAKE_ID_ALREADY_EXISTS")
                take_ids.add(track.take_id)
            if track.recording_session_id is not None:
                if track.recording_session_id in recording_session_ids:
                    raise VideoEditingError("AUDIO_RECORDING_SESSION_ALREADY_ATTACHED")
                recording_session_ids.add(track.recording_session_id)
            for clip in track.clips:
                if clip.track_id != track.track_id or clip.media_asset_id not in assets:
                    raise VideoEditingError("TIMELINE_CLIP_SOURCE_INVALID")
                asset = assets[clip.media_asset_id]
                if asset.is_still_image:
                    if (track.kind != "video" or clip.display_duration_sec is None
                            or clip.source_in != 0 or clip.source_out != 0
                            or clip.playback_rate != 1 or clip.audio_role != "muted"):
                        raise VideoEditingError("STILL_IMAGE_CLIP_CLOCK_INVALID")
                elif (clip.display_duration_sec is not None or asset.duration_sec is None
                      or clip.source_out > asset.duration_sec):
                    raise VideoEditingError("CLIP_RANGE_OUTSIDE_MEDIA_ASSET")
                if track.kind == "video" and not asset.has_video:
                    raise VideoEditingError("VIDEO_TRACK_REQUIRES_VIDEO_ASSET")
                if track.kind == "video":
                    if clip.video_stream_index is None and asset.video_stream_count > 1:
                        raise VideoEditingError("MULTIPLE_SOURCE_VIDEO_STREAMS_REQUIRE_EXPLICIT_SELECTION")
                    if clip.video_stream_index is not None and clip.video_stream_index >= asset.video_stream_count:
                        raise VideoEditingError("VIDEO_STREAM_INDEX_OUT_OF_RANGE")
                elif clip.video_stream_index is not None:
                    raise VideoEditingError("VIDEO_STREAM_SELECTION_VIDEO_TRACK_ONLY")
                if track.kind != "video":
                    if asset.audio_stream_count < 1:
                        raise VideoEditingError("AUDIO_TRACK_REQUIRES_AUDIO_ASSET")
                    if clip.audio_role != track.kind:
                        raise VideoEditingError("AUDIO_TRACK_ROLE_MISMATCH")
                    if clip.audio_stream_index is None:
                        raise VideoEditingError("AUDIO_STREAM_SELECTION_REQUIRED")
                elif clip.audio_role not in {"original", "muted"}:
                    raise VideoEditingError("AUDIO_TRACK_ROLE_NOT_ALLOWED")
                elif clip.audio_role == "muted" and clip.audio_stream_index is not None:
                    raise VideoEditingError("MUTED_VIDEO_CLIP_CANNOT_SELECT_AUDIO")
                if clip.audio_stream_index is not None and clip.audio_stream_index >= asset.audio_stream_count:
                    raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
                if (
                    track.kind == "video"
                    and clip.audio_role == "original"
                    and asset.audio_stream_count > 1
                    and clip.audio_stream_index is None
                ):
                    raise VideoEditingError("MULTIPLE_SOURCE_AUDIO_TRACKS_REQUIRE_EXPLICIT_SELECTION")
                if clip.clip_id in clip_ids:
                    raise VideoEditingError("TIMELINE_DUPLICATE_CLIP")
                clip_ids.add(clip.clip_id)
                clips[clip.clip_id] = clip

        if not assets and clip_ids:
            raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")

        for transition in self.transitions:
            if not isinstance(transition, Transition):
                raise VideoEditingError("TIMELINE_TRANSITIONS_INVALID")
            transition.validate()
            if transition.from_clip not in clips or transition.to_clip not in clips:
                raise VideoEditingError("TRANSITION_CLIP_NOT_FOUND")
            if transition.duration_sec > min(
                clips[transition.from_clip].duration_sec,
                clips[transition.to_clip].duration_sec,
            ):
                raise VideoEditingError("TRANSITION_DURATION_EXCEEDS_CLIP")

        for overlay in self.overlays:
            if not isinstance(overlay, TimedOverlay):
                raise VideoEditingError("TIMELINE_OVERLAYS_INVALID")
            overlay.validate()
        chapter_ids = set()
        for chapter in self.chapters:
            if not isinstance(chapter, TimelineChapter):
                raise VideoEditingError("TIMELINE_CHAPTERS_INVALID")
            chapter.validate()
            if chapter.chapter_id in chapter_ids:
                raise VideoEditingError("TIMELINE_CHAPTER_ALREADY_EXISTS")
            chapter_ids.add(chapter.chapter_id)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        assets: Iterable[MediaAsset],
        verify_local_assets: bool = True,
    ) -> "Timeline":
        """Reopen a sanitized timeline against freshly fingerprinted local assets.

        The document supplies edit intent only. Asset paths, factory tokens,
        Evidence, rights claims, and other unrecognized fields are never loaded
        from stored data.
        """

        def document_object(value: Any, fields: Tuple[str, ...]) -> Dict[str, Any]:
            if type(value) is not dict or set(value) != set(fields):
                raise VideoEditingError("TIMELINE_DOCUMENT_FIELDS_INVALID")
            return value

        def document_list(value: Any) -> List[Any]:
            if type(value) is not list:
                raise VideoEditingError("TIMELINE_DOCUMENT_LIST_INVALID")
            return value

        def document_string(value: Any) -> str:
            if not isinstance(value, str):
                raise VideoEditingError("TIMELINE_DOCUMENT_VALUE_INVALID")
            return value

        timeline_fields = {"version", "format_ratio", "assets", "tracks", "transitions", "overlays"}
        if (type(payload) is not dict or not timeline_fields <= set(payload)
                or set(payload) - timeline_fields - {"approval_required", "chapters", "audio_buses", "transcripts",
                                                     "video_color_interpretations"}):
            raise VideoEditingError("TIMELINE_DOCUMENT_FIELDS_INVALID")
        root = payload
        if type(root["version"]) is not int or root["version"] < 1:
            raise VideoEditingError("TIMELINE_VERSION_INVALID")
        if "approval_required" in root and type(root["approval_required"]) is not bool:
            raise VideoEditingError("TIMELINE_DOCUMENT_VALUE_INVALID")

        fresh_assets: Dict[str, MediaAsset] = {}
        for asset in assets:
            if not isinstance(asset, MediaAsset):
                raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
            asset.validate()
            if asset.asset_id in fresh_assets:
                raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
            if verify_local_assets:
                try:
                    confirmed = fingerprint_local_media(
                        asset.path,
                        source_material_id=asset.source_material_id,
                    display_filename=asset.filename,
                    )
                except VideoEditingError as exc:
                    raise VideoEditingError("TIMELINE_REOPEN_ASSET_UNAVAILABLE") from exc
            else:
                confirmed = asset
            if confirmed.to_dict() != asset.to_dict():
                raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
            fresh_assets[asset.asset_id] = confirmed

        stored_assets = document_list(root["assets"])
        ordered_assets: List[MediaAsset] = []
        stored_asset_ids = set()
        asset_fields = (
            "asset_id", "filename", "sha256", "duration_sec", "media_kind",
            "has_video", "audio_stream_count", "evidence_refs", "rights_status",
            "source_material_id", "streams", "format",
        )
        for stored in stored_assets:
            asset_optional_fields = tuple(
                key for key in ("duration_basis", "speech_source")
                if type(stored) is dict and key in stored
            )
            item = document_object(stored, asset_fields + asset_optional_fields)
            asset_id = document_string(item["asset_id"])
            if asset_id in stored_asset_ids:
                raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
            stored_asset_ids.add(asset_id)
            asset = fresh_assets.get(asset_id)
            if asset is None:
                raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISSING")
            if item["duration_sec"] is None:
                duration = None
                if (not asset.is_still_image or item.get("duration_basis") != "none"):
                    raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
            else:
                duration = _bounded_time(
                    item["duration_sec"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID",
                    minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC,
                )
                if item.get("duration_basis") not in (None, "intrinsic"):
                    raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
            actual_streams = asset.to_dict()["streams"]
            stored_streams = item["streams"]
            stream_metadata_matches = (
                type(stored_streams) is list and len(stored_streams) == len(actual_streams)
                and all(
                    type(stored_stream) is dict
                    and all(actual_stream.get(key) == value for key, value in stored_stream.items())
                    for stored_stream, actual_stream in zip(stored_streams, actual_streams, strict=True)
                )
            )
            if (
                item["filename"] != asset.filename
                or item["sha256"] != asset.sha256
                or duration != asset.duration_sec
                or item["media_kind"] != asset.media_kind
                or type(item["has_video"]) is not bool
                or item["has_video"] is not asset.has_video
                or type(item["audio_stream_count"]) is not int
                or item["audio_stream_count"] != asset.audio_stream_count
                or item["evidence_refs"] != []
                or item["rights_status"] != "unknown"
                or item["source_material_id"] != asset.source_material_id
                or not stream_metadata_matches
                or item["format"] != asset.to_dict()["format"]
                or ("speech_source" in item
                    and item["speech_source"] != asset.to_dict().get("speech_source"))
            ):
                raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")
            ordered_assets.append(asset)
        if stored_asset_ids != set(fresh_assets):
            raise VideoEditingError("TIMELINE_REOPEN_ASSET_MISMATCH")

        tracks: List[TimelineTrack] = []
        track_fields = ("track_id", "kind", "clips")
        clip_fields = (
            "clip_id", "media_asset_id", "source_in", "source_out", "timeline_start",
            "track_id", "purpose", "audio_role", "audio_mix", "evidence_refs",
            "audio_stream_index", "video_stream_index",
        )
        mix_fields = ("volume", "fade_in_sec", "fade_out_sec", "ducking")
        for stored_track in document_list(root["tracks"]):
            controls = tuple(key for key in ('layout','locked','muted','solo','hidden',
                                             'take_id','take_type','recording_session_id','cue_id','speaker_label',
                                             'gain','pan','audio_bus_sends')
                             if type(stored_track) is dict and key in stored_track)
            track_data = document_object(stored_track, track_fields + controls)
            track_id = document_string(track_data["track_id"])
            kind = document_string(track_data["kind"])
            clips: List[TimelineClip] = []
            for stored_clip in document_list(track_data["clips"]):
                has_crop = type(stored_clip) is dict and "frame_crop" in stored_clip
                has_placement = type(stored_clip) is dict and "video_placement" in stored_clip
                has_rate = type(stored_clip) is dict and "playback_rate" in stored_clip
                has_effects = type(stored_clip) is dict and "effects" in stored_clip
                has_keyframes = type(stored_clip) is dict and "transform_keyframes" in stored_clip
                has_display_duration = type(stored_clip) is dict and "display_duration_sec" in stored_clip
                clip_data = document_object(
                    stored_clip,
                    clip_fields
                    + (("frame_crop",) if has_crop else ())
                    + (("video_placement",) if has_placement else ())
                    + (("playback_rate",) if has_rate else ())
                    + (("effects",) if has_effects else ())
                    + (("transform_keyframes",) if has_keyframes else ())
                    + (("display_duration_sec",) if has_display_duration else ()),
                )
                mix_data = document_object(clip_data["audio_mix"], mix_fields)
                if type(clip_data["evidence_refs"]) is not list or clip_data["evidence_refs"]:
                    raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
                if type(mix_data["ducking"]) is not bool:
                    raise VideoEditingError("TIMELINE_DOCUMENT_VALUE_INVALID")
                audio_index = clip_data["audio_stream_index"]
                if audio_index is not None and type(audio_index) is not int:
                    raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
                try:
                    mix = AudioMix(
                        volume=mix_data["volume"],
                        fade_in_sec=mix_data["fade_in_sec"],
                        fade_out_sec=mix_data["fade_out_sec"],
                        ducking=mix_data["ducking"],
                    )
                    clips.append(TimelineClip(
                        clip_id=document_string(clip_data["clip_id"]),
                        media_asset_id=document_string(clip_data["media_asset_id"]),
                        source_in=_fraction_time(clip_data["source_in"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                        source_out=_fraction_time(clip_data["source_out"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                        timeline_start=_fraction_time(clip_data["timeline_start"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                        track_id=document_string(clip_data["track_id"]),
                        purpose=document_string(clip_data["purpose"]),
                        audio_role=document_string(clip_data["audio_role"]),
                        audio_mix=mix,
                        evidence_refs=(),
                        audio_stream_index=audio_index,
                        video_stream_index=clip_data["video_stream_index"],
                        frame_crop=FrameCrop.from_dict(clip_data["frame_crop"]) if has_crop else None,
                        video_placement=VideoPlacement.from_dict(clip_data["video_placement"]) if has_placement else None,
                        playback_rate=_fraction_time(clip_data.get("playback_rate", {"num": 1, "den": 1}), error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                        effects=tuple(VideoEffect.from_dict(item) for item in document_list(clip_data.get("effects", []))),
                        transform_keyframes=tuple(VideoTransformKeyframe.from_dict(item)
                                                  for item in document_list(clip_data.get("transform_keyframes", []))),
                        display_duration_sec=(
                            _fraction_time(clip_data["display_duration_sec"],
                                           error_code="TIMELINE_DOCUMENT_VALUE_INVALID")
                            if has_display_duration else None
                        ),
                    ))
                except (TypeError, ValueError) as exc:
                    if isinstance(exc, VideoEditingError):
                        raise
                    raise VideoEditingError("TIMELINE_DOCUMENT_VALUE_INVALID") from exc
            tracks.append(TimelineTrack(track_id=track_id, kind=kind, clips=tuple(clips), layout=track_data.get("layout", "sequence"),
                **{key:track_data.get(key,False) for key in ('locked','muted','solo','hidden')},
                **{key:track_data[key] for key in ('take_id','take_type','recording_session_id','cue_id','speaker_label')
                   if key in track_data},
                gain=track_data.get("gain", 1.0), pan=track_data.get("pan", 0.0),
                audio_bus_sends=tuple(AudioBusSend.from_dict(item)
                                      for item in document_list(track_data.get("audio_bus_sends", [])))))

        transitions: List[Transition] = []
        transition_fields = ("from_clip", "to_clip", "transition_type", "duration_sec", "reason")
        for stored_transition in document_list(root["transitions"]):
            item = document_object(stored_transition, transition_fields)
            transitions.append(Transition(
                from_clip=document_string(item["from_clip"]),
                to_clip=document_string(item["to_clip"]),
                transition_type=document_string(item["transition_type"]),
                duration_sec=_fraction_time(item["duration_sec"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                reason=document_string(item["reason"]),
            ))

        overlays: List[TimedOverlay] = []
        overlay_fields = ("overlay_id", "kind", "start_sec", "end_sec", "text")
        for stored_overlay in document_list(root["overlays"]):
            has_profile = type(stored_overlay) is dict and "layout_profile" in stored_overlay
            has_style = type(stored_overlay) is dict and "style" in stored_overlay
            item = document_object(stored_overlay,
                                   overlay_fields + (("layout_profile",) if has_profile else ()) + (("style",) if has_style else ()))
            overlays.append(TimedOverlay(
                overlay_id=document_string(item["overlay_id"]),
                kind=document_string(item["kind"]),
                start_sec=_fraction_time(item["start_sec"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                end_sec=_fraction_time(item["end_sec"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
                text=document_string(item["text"]),
                layout_profile=document_string(item["layout_profile"]) if has_profile else None,
                style=CaptionStyle.from_dict(item["style"]) if has_style else None,
            ))

        chapters: List[TimelineChapter] = []
        chapter_fields = ("chapter_id", "title", "start_sec")
        for stored_chapter in document_list(root.get("chapters", [])):
            item = document_object(stored_chapter, chapter_fields)
            chapters.append(TimelineChapter(
                chapter_id=document_string(item["chapter_id"]),
                title=document_string(item["title"]),
                start_sec=_fraction_time(item["start_sec"], error_code="TIMELINE_DOCUMENT_VALUE_INVALID"),
            ))

        audio_buses = tuple(
            AudioBus.from_dict(item)
            for item in document_list(root.get("audio_buses", []))
        )
        transcripts = tuple(
            TimelineTranscript.from_dict(item)
            for item in document_list(root.get("transcripts", []))
        )
        video_color_interpretations = tuple(
            VideoColorInterpretation.from_dict(item)
            for item in document_list(root.get("video_color_interpretations", []))
        )

        return cls(
            assets=tuple(ordered_assets),
            tracks=tuple(tracks),
            audio_buses=audio_buses,
            transcripts=transcripts,
            video_color_interpretations=video_color_interpretations,
            transitions=tuple(transitions),
            overlays=tuple(overlays),
            format_ratio=document_string(root["format_ratio"]),
            version=root["version"],
            chapters=tuple(chapters),
        )

    @classmethod
    def from_assets(cls, assets: Iterable[MediaAsset], format_ratio: str = "9:16") -> "Timeline":
        assets_by_id: Dict[str, MediaAsset] = {}
        for asset in assets:
            if not isinstance(asset, MediaAsset):
                raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
            asset.validate()
            existing = assets_by_id.get(asset.asset_id)
            if existing and (
                existing.sha256 != asset.sha256 or existing.duration_sec != asset.duration_sec
            ):
                raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
            assets_by_id.setdefault(asset.asset_id, asset)
        assets_tuple = tuple(assets_by_id.values())
        if format_ratio not in ALLOWED_FORMAT_RATIOS:
            raise VideoEditingError("FORMAT_RATIO_NOT_SUPPORTED")
        return cls(assets=assets_tuple, format_ratio=format_ratio)

    def add_asset(self, asset: MediaAsset) -> "Timeline":
        """Bind an already-authorized Material source without duplicating its file."""
        self.validate()
        if not isinstance(asset, MediaAsset):
            raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
        asset.validate()
        existing = next((item for item in self.assets if item.asset_id == asset.asset_id), None)
        if existing is not None:
            if existing.to_dict() != asset.to_dict():
                raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
            return self
        return self._commit(assets=(*self.assets, asset))

    def _asset(self, asset_id: str) -> MediaAsset:
        for asset in self.assets:
            if asset.asset_id == asset_id:
                return asset
        raise VideoEditingError("MEDIA_ASSET_NOT_IN_TIMELINE")

    def _track(self, track_id: str) -> TimelineTrack:
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        raise VideoEditingError("TIMELINE_TRACK_NOT_FOUND")

    def add_video_track(self, track_id: str) -> Timeline:
        self.validate()
        if any(track.track_id == track_id for track in self.tracks):
            raise VideoEditingError("TIMELINE_TRACK_ALREADY_EXISTS")
        return self._commit(tracks=(*self.tracks, TimelineTrack(track_id, "video", layout="free")))

    def set_video_placement(self, clip_id: str, placement: VideoPlacement) -> Timeline:
        track, clip = self._find_clip(clip_id)
        if track.kind != "video":
            raise VideoEditingError("VIDEO_PLACEMENT_VIDEO_ONLY")
        return self._replace_clip(track, replace(clip, video_placement=placement))

    def set_video_effects(self, clip_id: str, effects: Any) -> Timeline:
        track, clip = self._find_clip(clip_id)
        if track.kind != "video":
            raise VideoEditingError("VIDEO_EFFECTS_VIDEO_ONLY")
        if not isinstance(effects, (tuple, list)) or len(effects) > 16:
            raise VideoEditingError("VIDEO_EFFECT_STACK_INVALID")
        normalized = tuple(
            VideoEffect.from_dict(item) if isinstance(item, Mapping) else item
            for item in effects
        )
        if any(not isinstance(item, VideoEffect) for item in normalized):
            raise VideoEditingError("VIDEO_EFFECT_INVALID")
        return self._replace_clip(track, replace(clip, effects=normalized))

    def set_transform_keyframes(self, clip_id: str, keyframes: Any) -> Timeline:
        track, clip = self._find_clip(clip_id)
        if track.kind != "video":
            raise VideoEditingError("VIDEO_KEYFRAMES_VIDEO_ONLY")
        if not isinstance(keyframes, (tuple, list)) or len(keyframes) > 256:
            raise VideoEditingError("VIDEO_KEYFRAME_LIMIT_EXCEEDED")
        normalized = tuple(
            VideoTransformKeyframe.from_dict(item) if isinstance(item, Mapping) else item
            for item in keyframes
        )
        if any(not isinstance(item, VideoTransformKeyframe) for item in normalized):
            raise VideoEditingError("VIDEO_KEYFRAME_INVALID")
        return self._replace_clip(track, replace(clip, transform_keyframes=normalized))

    def move_video_clip(self, clip_id: str, *, timeline_start: float) -> Timeline:
        track, clip = self._find_clip(clip_id)
        if track.kind != "video" or track.layout != "free":
            raise VideoEditingError("VIDEO_POSITION_REQUIRES_FREE_TRACK")
        return self._replace_clip(track, replace(clip, timeline_start=timeline_start))

    def add_audio_track(self, track_id: str, *, audio_role: str,
                        take_id: str | None = None, take_type: str | None = None,
                        recording_session_id: str | None = None, cue_id: str | None = None,
                        speaker_label: str | None = None) -> "Timeline":
        """Add an explicit voice-over, BGM, or SFX track to the timeline."""
        normalized_role = str(audio_role or "").strip().lower()
        if normalized_role not in {"voiceover", "bgm", "sfx"}:
            raise VideoEditingError("AUDIO_TRACK_ROLE_NOT_ALLOWED")
        if any(track.track_id == track_id for track in self.tracks):
            raise VideoEditingError("TIMELINE_TRACK_ALREADY_EXISTS")
        track = TimelineTrack(
            str(track_id).strip(), normalized_role,
            take_id=take_id, take_type=take_type, recording_session_id=recording_session_id,
            cue_id=cue_id, speaker_label=speaker_label,
        )
        if not track.track_id:
            raise VideoEditingError("TIMELINE_TRACK_ID_REQUIRED")
        return self._commit(tracks=(*self.tracks, track))

    def set_audio_bus(self, bus: AudioBus | Mapping[str, Any]) -> "Timeline":
        normalized = AudioBus.from_dict(dict(bus)) if isinstance(bus, Mapping) else bus
        if not isinstance(normalized, AudioBus):
            raise VideoEditingError("AUDIO_BUS_INVALID")
        normalized.validate()
        existing = {item.bus_id: item for item in self.audio_buses}
        existing[normalized.bus_id] = normalized
        return self._commit(audio_buses=tuple(existing.values()))

    def remove_audio_bus(self, bus_id: str) -> "Timeline":
        if not isinstance(bus_id, str) or not bus_id.strip():
            raise VideoEditingError("AUDIO_BUS_INVALID")
        if bus_id == "master":
            raise VideoEditingError("AUDIO_MASTER_BUS_CANNOT_BE_REMOVED")
        if bus_id not in {item.bus_id for item in self.audio_buses}:
            raise VideoEditingError("AUDIO_BUS_NOT_FOUND")
        if any(send.bus_id == bus_id for track in self.tracks for send in track.audio_bus_sends):
            raise VideoEditingError("AUDIO_BUS_STILL_ROUTED")
        return self._commit(audio_buses=tuple(item for item in self.audio_buses if item.bus_id != bus_id))

    def set_transcript(self, value: Mapping[str, Any]) -> "Timeline":
        allowed = {"transcript_id", "source_asset_id", "audio_stream_index", "language",
                   "alignment_source", "words", "audio_channel_index", "task",
                   "model_provenance", "review_state"}
        required = {"source_asset_id", "audio_stream_index", "language", "words"}
        if not isinstance(value, Mapping) or set(value) - allowed or not required <= set(value):
            raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
        asset = self._asset(value["source_asset_id"])
        stream_index = value["audio_stream_index"]
        if (asset.is_still_image or type(stream_index) is not int
                or not 0 <= stream_index < asset.audio_stream_count):
            raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH")
        audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
        if stream_index >= len(audio_streams):
            raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH")
        stream = audio_streams[stream_index]
        sample_rate = stream.get("sample_rate")
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_CLOCK_MISMATCH")
        channel_index = value.get("audio_channel_index")
        channel_count = stream.get("channels")
        if channel_index is not None and (
            type(channel_index) is not int or type(channel_count) is not int
            or not 0 <= channel_index < channel_count
        ):
            raise VideoEditingError("MEDIA_TRANSCRIPT_CHANNEL_INVALID")
        raw_time_base = stream.get("time_base")
        time_base = (
            (raw_time_base["num"], raw_time_base["den"])
            if isinstance(raw_time_base, dict) and type(raw_time_base.get("num")) is int
            and type(raw_time_base.get("den")) is int else None
        )
        existing = next((item for item in self.transcripts
                         if item.source_asset_id == asset.asset_id
                         and item.audio_stream_index == stream_index
                         and item.audio_channel_index == channel_index), None)
        transcript_id = value.get("transcript_id", existing.transcript_id if existing else _id("TRANSCRIPT"))
        words_value = value["words"]
        if not isinstance(words_value, (tuple, list)):
            raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
        transcript = TimelineTranscript(
            transcript_id=transcript_id,
            source_asset_id=asset.asset_id,
            source_sha256=asset.sha256,
            audio_stream_index=stream_index,
            sample_rate=sample_rate,
            language=value["language"],
            alignment_source=value.get(
                "alignment_source", existing.alignment_source if existing else "human_supplied_unverified",
            ),
            words=tuple(TranscriptWord.from_dict(item) if isinstance(item, Mapping) else item
                        for item in words_value),
            audio_stream_start_pts=stream.get("start_pts"),
            audio_stream_time_base=time_base,
            audio_channel_index=channel_index,
            task=value.get("task", existing.task if existing else "transcribe"),
            model_provenance=value.get(
                "model_provenance", existing.model_provenance if existing else None,
            ),
            review_state=value.get("review_state", existing.review_state if existing else "unreviewed"),
        )
        transcript.validate()
        transcripts = tuple(item for item in self.transcripts
                            if (item.source_asset_id, item.audio_stream_index, item.audio_channel_index) !=
                            (asset.asset_id, stream_index, channel_index)) + (transcript,)
        return self._commit(transcripts=transcripts)

    def set_video_color_interpretation(self, value: Mapping[str, Any]) -> "Timeline":
        interpretation = VideoColorInterpretation.from_dict(dict(value))
        existing = {(item.asset_id, item.video_stream_index): item
                    for item in self.video_color_interpretations}
        existing[(interpretation.asset_id, interpretation.video_stream_index)] = interpretation
        return self._commit(video_color_interpretations=tuple(existing.values()))

    def remove_video_color_interpretation(self, asset_id: str, video_stream_index: int) -> "Timeline":
        key = (asset_id, video_stream_index)
        if key not in {(item.asset_id, item.video_stream_index) for item in self.video_color_interpretations}:
            raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_NOT_FOUND")
        return self._commit(video_color_interpretations=tuple(
            item for item in self.video_color_interpretations
            if (item.asset_id, item.video_stream_index) != key
        ))

    def remove_transcript(self, transcript_id: str) -> "Timeline":
        if not isinstance(transcript_id, str) or not transcript_id:
            raise VideoEditingError("MEDIA_TRANSCRIPT_NOT_FOUND")
        if transcript_id not in {item.transcript_id for item in self.transcripts}:
            raise VideoEditingError("MEDIA_TRANSCRIPT_NOT_FOUND")
        return self._commit(transcripts=tuple(item for item in self.transcripts
                                             if item.transcript_id != transcript_id))

    def read_transcript_words(
        self,
        *,
        source_asset_id: str | None = None,
        transcript_id: str | None = None,
    ) -> dict[str, Any]:
        """Project saved sample-clock words onto every matching clip occurrence."""
        self.validate()
        if source_asset_id is not None and not isinstance(source_asset_id, str):
            raise VideoEditingError("MEDIA_TRANSCRIPT_SOURCE_MISMATCH")
        if transcript_id is not None and not isinstance(transcript_id, str):
            raise VideoEditingError("MEDIA_TRANSCRIPT_NOT_FOUND")
        selected = [item for item in self.transcripts
                    if (source_asset_id is None or item.source_asset_id == source_asset_id)
                    and (transcript_id is None or item.transcript_id == transcript_id)]
        if transcript_id is not None and not selected:
            raise VideoEditingError("MEDIA_TRANSCRIPT_NOT_FOUND")
        assets = {asset.asset_id: asset for asset in self.assets}
        instances = []
        transcript_documents = []
        for transcript in selected:
            asset = assets[transcript.source_asset_id]
            transcript_bytes = json.dumps(
                transcript.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            transcript_sha256 = hashlib.sha256(transcript_bytes).hexdigest()
            transcript_documents.append({**transcript.to_dict(), "transcript_sha256": transcript_sha256})
            audio_origin = media_stream_input_origin(asset, "audio", transcript.audio_stream_index)
            for word in transcript.words:
                audio_start = Fraction(word.start_sample, transcript.sample_rate)
                audio_end = Fraction(word.end_sample, transcript.sample_rate)
                container_start, container_end = audio_origin + audio_start, audio_origin + audio_end
                matching = []
                for track in self.tracks:
                    for clip in track.clips:
                        if (clip.media_asset_id != asset.asset_id or clip.audio_role == "muted"
                                or (0 if clip.audio_stream_index is None else clip.audio_stream_index)
                                   != transcript.audio_stream_index):
                            continue
                        if track.kind == "video":
                            video_index = 0 if clip.video_stream_index is None else clip.video_stream_index
                            video_origin = media_stream_input_origin(asset, "video", video_index)
                            source_start, source_end = container_start - video_origin, container_end - video_origin
                        else:
                            source_start, source_end = audio_start, audio_end
                        if clip.source_in <= source_start and source_end <= clip.source_out:
                            rate = _fraction_time(clip.playback_rate)
                            timeline_start = _fraction_time(clip.timeline_start) + (
                                source_start - _fraction_time(clip.source_in)
                            ) / rate
                            timeline_end = _fraction_time(clip.timeline_start) + (
                                source_end - _fraction_time(clip.source_in)
                            ) / rate
                            matching.append({
                                "clip_id": clip.clip_id,
                                "track_id": track.track_id,
                                "source_start_sec": _time_json(source_start),
                                "source_end_sec": _time_json(source_end),
                                "timeline_start_sec": _time_json(timeline_start),
                                "timeline_end_sec": _time_json(timeline_end),
                                "playback_rate": _time_json(rate),
                            })
                base = {
                    "source_asset_id": asset.asset_id,
                    "source_sha256": asset.sha256,
                    "transcript_id": transcript.transcript_id,
                    "transcript_sha256": transcript_sha256,
                    "alignment_source": transcript.alignment_source,
                    "language": transcript.language,
                    "audio_stream_index": transcript.audio_stream_index,
                    "audio_channel_index": transcript.audio_channel_index,
                    "task": transcript.task,
                    "sample_rate": transcript.sample_rate,
                    "source_start_sample": word.start_sample,
                    "source_end_sample": word.end_sample,
                    "source_audio_start_sec": _time_json(audio_start),
                    "source_audio_end_sec": _time_json(audio_end),
                    "word_id": word.word_id,
                    "text": word.text,
                    "speaker_label": word.speaker_label,
                }
                if matching:
                    for match in matching:
                        instances.append({
                            **base,
                            **match,
                            "qualified_id": f"{asset.asset_id}:{word.word_id}:{match['clip_id']}",
                        })
                else:
                    instances.append({**base, "clip_id": None, "qualified_id": None,
                                      "mapping_state": "no_matching_clip_occurrence"})
        instances.sort(key=lambda item: (
            item.get("timeline_start_sec", {}).get("num", 0) /
            item.get("timeline_start_sec", {}).get("den", 1),
            item["source_asset_id"], item["word_id"], item.get("clip_id") or "",
        ))
        return {
            "timeline_version": self.version,
            "timeline_digest": self._view_digest(),
            "transcripts": transcript_documents,
            "words": instances,
            "word_instances": len(instances),
            "alignment_claim": "user_supplied_alignment_bound_to_source_sample_clock_accuracy_not_independently_verified",
            "source_media_modified": False,
        }

    def set_track_audio_routing(
        self,
        track_id: str,
        *,
        gain: float | None = None,
        pan: float | None = None,
        sends: Any = None,
    ) -> "Timeline":
        if gain is None and pan is None and sends is None:
            raise VideoEditingError("AUDIO_TRACK_ROUTING_EMPTY")
        selected = self._track(track_id)
        normalized_sends = selected.audio_bus_sends
        if sends is not None:
            if not isinstance(sends, (tuple, list)):
                raise VideoEditingError("AUDIO_TRACK_BUS_SENDS_INVALID")
            normalized_sends = tuple(
                AudioBusSend.from_dict(item) if isinstance(item, Mapping) else item
                for item in sends
            )
        updated = replace(
            selected,
            gain=selected.gain if gain is None else gain,
            pan=selected.pan if pan is None else pan,
            audio_bus_sends=normalized_sends,
        )
        return self._commit(tracks=tuple(updated if item.track_id == track_id else item
                                          for item in self.tracks))

    def _snapshot(self) -> _TimelineSnapshot:
        return _TimelineSnapshot(
            self.assets, self.tracks, self.audio_buses, self.transcripts, self.video_color_interpretations,
            self.transitions,
            self.overlays, self.format_ratio, self.chapters,
        )

    def _commit(
        self,
        *,
        assets: Optional[Tuple[MediaAsset, ...]] = None,
        tracks: Optional[Tuple[TimelineTrack, ...]] = None,
        audio_buses: Optional[Tuple[AudioBus, ...]] = None,
        transcripts: Optional[Tuple[TimelineTranscript, ...]] = None,
        video_color_interpretations: Optional[Tuple[VideoColorInterpretation, ...]] = None,
        transitions: Optional[Tuple[Transition, ...]] = None,
        overlays: Optional[Tuple[TimedOverlay, ...]] = None,
        format_ratio: Optional[str] = None,
        chapters: Optional[Tuple[TimelineChapter, ...]] = None,
    ) -> "Timeline":
        if tracks is not None:
            changed = {track.track_id:track for track in tracks}
            for old in self.tracks:
                current = changed.get(old.track_id)
                if old.locked and (current is None or
                                   (old.kind, old.layout, old.clips, old.gain, old.pan, old.audio_bus_sends) !=
                                   (current.kind, current.layout, current.clips,
                                    current.gain, current.pan, current.audio_bus_sends)):
                    raise VideoEditingError('TIMELINE_TRACK_LOCKED')
        if transitions is not None and transitions != self.transitions:
            locked = {clip.clip_id for track in self.tracks if track.locked for clip in track.clips}
            changes = set(transitions).symmetric_difference(self.transitions)
            if any(item.from_clip in locked or item.to_clip in locked for item in changes):
                raise VideoEditingError('TIMELINE_TRACK_LOCKED')
        return replace(
            self,
            assets=assets if assets is not None else self.assets,
            tracks=tracks if tracks is not None else self.tracks,
            audio_buses=audio_buses if audio_buses is not None else self.audio_buses,
            transcripts=transcripts if transcripts is not None else self.transcripts,
            video_color_interpretations=(video_color_interpretations if video_color_interpretations is not None
                                         else self.video_color_interpretations),
            transitions=transitions if transitions is not None else self.transitions,
            overlays=overlays if overlays is not None else self.overlays,
            format_ratio=format_ratio if format_ratio is not None else self.format_ratio,
            chapters=chapters if chapters is not None else self.chapters,
            version=self.version + 1,
            _history=self._history + (self._snapshot(),),
        )

    def set_track_controls(self, track_id, **controls):
        """Lock edits; hide only video; mute/solo the track's own audio."""
        if (not controls or set(controls) - {'locked','muted','solo','hidden'}
                or any(type(value) is not bool for value in controls.values())):
            raise VideoEditingError('TRACK_CONTROL_INVALID')
        selected = self._track(track_id)
        updated = replace(selected,**controls)
        return self._commit(tracks=tuple(updated if track.track_id == track_id else track for track in self.tracks))

    def audible_tracks(self):
        solo = any(track.solo for track in self.tracks)
        return tuple(track for track in self.tracks if not track.muted and (not solo or track.solo))

    @staticmethod
    def _reflow(track: TimelineTrack, clips: Iterable[TimelineClip]) -> TimelineTrack:
        if track.layout == "free":
            return replace(track, clips=tuple(clips))
        cursor = Fraction(0)
        result: List[TimelineClip] = []
        for clip in clips:
            updated = replace(clip, timeline_start=cursor)
            updated.validate()
            result.append(updated)
            cursor += updated.duration_sec
            if not math.isfinite(cursor) or cursor > MAX_EDIT_TIME_SEC:
                raise VideoEditingError("TIMELINE_DURATION_OUT_OF_RANGE")
        return replace(track, clips=tuple(result))

    def add_clip(
        self,
        media_asset_id: str,
        *,
        source_in: float = 0.0,
        source_out: Optional[float] = None,
        display_duration_sec: Optional[float] = None,
        track_id: str = "video",
        purpose: str = "",
        audio_role: str = "original",
        timeline_start: Optional[float] = None,
        audio_stream_index: Optional[int] = None,
        video_stream_index: Optional[int] = None,
    ) -> "Timeline":
        self.validate()
        asset = self._asset(media_asset_id)
        track = self._track(track_id)
        if asset.is_still_image:
            if track.kind != "video" or audio_role != "muted":
                raise VideoEditingError("STILL_IMAGE_VIDEO_TRACK_ONLY")
            if display_duration_sec is None:
                raise VideoEditingError("STILL_IMAGE_DISPLAY_DURATION_REQUIRED")
            source_start = out = Fraction(0)
            display_duration = _bounded_time(
                display_duration_sec, error_code="STILL_IMAGE_DISPLAY_DURATION_INVALID",
                minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC,
            )
            image_source_start = _bounded_time(
                source_in, error_code="STILL_IMAGE_HAS_NO_SOURCE_TIME_RANGE",
                minimum=0, maximum=MAX_EDIT_TIME_SEC,
            )
            image_source_end = Fraction(0) if source_out is None else _bounded_time(
                source_out, error_code="STILL_IMAGE_HAS_NO_SOURCE_TIME_RANGE",
                minimum=0, maximum=MAX_EDIT_TIME_SEC,
            )
            if image_source_start != 0 or image_source_end != 0:
                raise VideoEditingError("STILL_IMAGE_HAS_NO_SOURCE_TIME_RANGE")
        else:
            if display_duration_sec is not None or asset.duration_sec is None:
                raise VideoEditingError("DISPLAY_DURATION_IMAGE_ONLY")
            source_start = _bounded_time(
                source_in, error_code="CLIP_RANGE_OUTSIDE_MEDIA_ASSET", minimum=0, maximum=MAX_EDIT_TIME_SEC
            )
            out = asset.duration_sec if source_out is None else _bounded_time(
                source_out,
                error_code="CLIP_RANGE_OUTSIDE_MEDIA_ASSET",
                minimum=0,
                maximum=MAX_EDIT_TIME_SEC,
            )
            if out > asset.duration_sec or out <= source_start:
                raise VideoEditingError("CLIP_RANGE_OUTSIDE_MEDIA_ASSET")
            display_duration = None
        if track.kind == "video":
            if not asset.has_video:
                raise VideoEditingError("VIDEO_TRACK_REQUIRES_VIDEO_ASSET")
            if video_stream_index is None:
                if asset.video_stream_count > 1:
                    raise VideoEditingError("MULTIPLE_SOURCE_VIDEO_STREAMS_REQUIRE_EXPLICIT_SELECTION")
                video_stream_index = 0
            if type(video_stream_index) is not int or not 0 <= video_stream_index < asset.video_stream_count:
                raise VideoEditingError("VIDEO_STREAM_INDEX_OUT_OF_RANGE")
            if timeline_start is not None and track.layout == "sequence":
                raise VideoEditingError("VIDEO_TRACK_TIMELINE_POSITION_AUTOMATIC")
            if audio_role not in {"original", "muted"}:
                raise VideoEditingError("AUDIO_TRACK_ROLE_NOT_ALLOWED")
            if audio_role == "original" and asset.audio_stream_count > 1 and audio_stream_index is None:
                raise VideoEditingError("MULTIPLE_SOURCE_AUDIO_TRACKS_REQUIRE_EXPLICIT_SELECTION")
            if audio_role == "original" and audio_stream_index is None and asset.audio_stream_count == 1:
                audio_stream_index = 0
            if audio_role == "muted" and audio_stream_index is not None:
                raise VideoEditingError("MUTED_VIDEO_CLIP_CANNOT_SELECT_AUDIO")
            timeline_position = Fraction(0) if timeline_start is None else _bounded_time(
                timeline_start, error_code="INVALID_CLIP_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
            )
            clip_audio_role = audio_role
        else:
            if video_stream_index is not None:
                raise VideoEditingError("VIDEO_STREAM_SELECTION_VIDEO_TRACK_ONLY")
            if asset.audio_stream_count < 1:
                raise VideoEditingError("AUDIO_TRACK_REQUIRES_AUDIO_ASSET")
            if audio_role not in {"original", track.kind}:
                raise VideoEditingError("AUDIO_TRACK_ROLE_MISMATCH")
            if audio_stream_index is None:
                if asset.audio_stream_count > 1:
                    raise VideoEditingError("MULTIPLE_SOURCE_AUDIO_TRACKS_REQUIRE_EXPLICIT_SELECTION")
                audio_stream_index = 0
            if type(audio_stream_index) is not int or not 0 <= audio_stream_index < asset.audio_stream_count:
                raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
            timeline_position = Fraction(0) if timeline_start is None else _bounded_time(
                timeline_start,
                error_code="INVALID_CLIP_RANGE",
                minimum=0,
                maximum=MAX_EDIT_TIME_SEC,
            )
            if timeline_position + (display_duration or out - source_start) > MAX_EDIT_TIME_SEC:
                raise VideoEditingError("TIMELINE_DURATION_OUT_OF_RANGE")
            clip_audio_role = track.kind
        clip = TimelineClip(
            clip_id=_id("CLIP"),
            media_asset_id=media_asset_id,
            source_in=source_start,
            source_out=out,
            timeline_start=timeline_position,
            track_id=track_id,
            purpose=purpose,
            audio_role=clip_audio_role,
            evidence_refs=asset.evidence_refs,
            audio_stream_index=audio_stream_index,
            video_stream_index=video_stream_index,
            display_duration_sec=display_duration,
        )
        updated = (
            self._reflow(track, (*track.clips, clip))
            if track.kind == "video"
            else replace(track, clips=(*track.clips, clip))
        )
        tracks = tuple(updated if item.track_id == track_id else item for item in self.tracks)
        return self._commit(tracks=tracks)

    def set_display_duration(self, clip_id: str, duration_sec: Any) -> "Timeline":
        track, clip = self._find_clip(clip_id)
        asset = self._asset(clip.media_asset_id)
        if not asset.is_still_image or track.kind != "video":
            raise VideoEditingError("DISPLAY_DURATION_IMAGE_ONLY")
        duration = _bounded_time(
            duration_sec, error_code="STILL_IMAGE_DISPLAY_DURATION_INVALID",
            minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC,
        )
        return self._replace_clip(track, replace(clip, display_duration_sec=duration))

    def _replace_clip(self, track: TimelineTrack, clip: TimelineClip) -> "Timeline":
        clips = tuple(clip if item.clip_id == clip.clip_id else item for item in track.clips)
        updated = self._reflow(track, clips) if track.kind == "video" else replace(track, clips=clips)
        tracks = tuple(updated if item.track_id == track.track_id else item for item in self.tracks)
        return self._commit(tracks=tracks)

    def _find_clip(self, clip_id: str) -> Tuple[TimelineTrack, TimelineClip]:
        for track in self.tracks:
            for clip in track.clips:
                if clip.clip_id == clip_id:
                    return track, clip
        raise VideoEditingError("TIMELINE_CLIP_NOT_FOUND")

    def replace_clip_source(
        self, clip_id: str, *, asset: MediaAsset, source_in: float,
        audio_stream_index: Optional[int] = None,
        display_duration_sec: Optional[float] = None,
    ) -> "Timeline":
        """Replace one inspected source while retaining the complete edit geometry.

        The Artifact caller still resolves scope, permissions and source refs.
        A longer utterance is a script/sequence revision, not this duration-
        preserving operation. Existing renderer rules verify any transitions.
        """
        self.validate()
        track, clip = self._find_clip(clip_id)
        if not isinstance(asset, MediaAsset):
            raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
        asset.validate()
        if track.kind == "video" and not asset.has_video:
            raise VideoEditingError("VIDEO_TRACK_REQUIRES_VIDEO_ASSET")
        if asset.is_still_image:
            if track.kind != "video" or clip.audio_role != "muted" or audio_stream_index is not None:
                raise VideoEditingError("STILL_IMAGE_VIDEO_TRACK_ONLY")
            if source_in not in (0, 0.0):
                raise VideoEditingError("STILL_IMAGE_HAS_NO_SOURCE_TIME_RANGE")
            start = end = Fraction(0)
            image_duration = _bounded_time(
                clip.duration_sec if display_duration_sec is None else display_duration_sec,
                error_code="STILL_IMAGE_DISPLAY_DURATION_INVALID",
                minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC,
            )
            audio_stream_index = None
        else:
            if display_duration_sec is not None or asset.duration_sec is None:
                raise VideoEditingError("DISPLAY_DURATION_IMAGE_ONLY")
            start = _bounded_time(source_in, error_code="REPLACEMENT_RANGE_OUTSIDE_MEDIA_ASSET",
                                  minimum=0, maximum=MAX_EDIT_TIME_SEC)
            old_source_length = (clip.duration_sec * clip.playback_rate
                                 if clip.display_duration_sec is not None else
                                 clip.source_out - clip.source_in)
            end = start + old_source_length
            if end > asset.duration_sec:
                raise VideoEditingError("REPLACEMENT_RANGE_OUTSIDE_MEDIA_ASSET")
            image_duration = None
            if clip.audio_role == "muted":
                if audio_stream_index is not None:
                    raise VideoEditingError("MUTED_VIDEO_CLIP_CANNOT_SELECT_AUDIO")
            elif track.kind != "video" or asset.audio_stream_count:
                if asset.audio_stream_count < 1:
                    raise VideoEditingError("AUDIO_TRACK_REQUIRES_AUDIO_ASSET")
                if audio_stream_index is None:
                    if asset.audio_stream_count > 1:
                        raise VideoEditingError("MULTIPLE_SOURCE_AUDIO_STREAMS_REQUIRE_EXPLICIT_SELECTION")
                    audio_stream_index = 0
                if type(audio_stream_index) is not int or not 0 <= audio_stream_index < asset.audio_stream_count:
                    raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
            elif self._asset(clip.media_asset_id).audio_stream_count or audio_stream_index is not None:
                raise VideoEditingError("REPLACEMENT_WOULD_REMOVE_SOURCE_AUDIO")

        existing = next((item for item in self.assets if item.asset_id == asset.asset_id), None)
        if existing is not None and (
            existing.sha256 != asset.sha256 or existing.duration_sec != asset.duration_sec
            or existing.has_video != asset.has_video or existing.audio_stream_count != asset.audio_stream_count
        ):
            raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
        assets = self.assets if existing is not None else (*self.assets, asset)
        updated_clip = replace(
            clip, media_asset_id=asset.asset_id, source_in=start, source_out=end,
            audio_role="muted" if asset.is_still_image else clip.audio_role,
            audio_stream_index=audio_stream_index,
            video_stream_index=0 if asset.is_still_image else clip.video_stream_index,
            display_duration_sec=image_duration, evidence_refs=asset.evidence_refs,
        )
        if updated_clip == clip and assets == self.assets:
            return self
        tracks = tuple(
            replace(item, clips=tuple(updated_clip if old.clip_id == clip_id else old for old in item.clips))
            if item.track_id == track.track_id else item for item in self.tracks
        )
        candidate = replace(self, assets=assets, tracks=tracks)
        if self.transitions:
            # Reuse the current 30fps centred-handle contract, including boundary
            # conflicts and unsupported types. Do not weaken the export guard.
            from .media_rendering import _video_transition_plan
            for layer in candidate.tracks:
                if layer.kind == "video":
                    _video_transition_plan(candidate, sorted(layer.clips, key=lambda item: item.timeline_start))
        return self._commit(assets=assets, tracks=tracks)

    def replace_audio_source(
        self, clip_id: str, *, asset: MediaAsset, source_in: float, source_out: float,
        ripple_track_ids: Iterable[str], audio_stream_index: Optional[int] = None,
        overlay_ranges: Optional[Mapping[str, Tuple[float, float]]] = None,
    ) -> "Timeline":
        """Revise a full audio range using an explicit, bounded ripple scope.

        Later selected tracks move by the duration difference. Intersecting
        clips require a separate replan; intersecting overlays need explicit
        authored ranges. Source audio is never stretched, padded or shortened
        to fit an old slot. The caller keeps and reviews the resulting time map.
        """
        self.validate()
        target_track, target = self._find_clip(clip_id)
        if target_track.kind == "video":
            raise VideoEditingError("AUDIO_SOURCE_REVISION_REQUIRES_AUDIO_CLIP")
        if not isinstance(asset, MediaAsset):
            raise VideoEditingError("TIMELINE_MEDIA_ASSETS_INVALID")
        asset.validate()
        start = _bounded_time(source_in, error_code="REPLACEMENT_RANGE_OUTSIDE_MEDIA_ASSET",
                              minimum=0, maximum=MAX_EDIT_TIME_SEC)
        end = _bounded_time(source_out, error_code="REPLACEMENT_RANGE_OUTSIDE_MEDIA_ASSET",
                            minimum=0, maximum=MAX_EDIT_TIME_SEC)
        if end <= start or end > asset.duration_sec:
            raise VideoEditingError("REPLACEMENT_RANGE_OUTSIDE_MEDIA_ASSET")
        if asset.audio_stream_count < 1:
            raise VideoEditingError("AUDIO_TRACK_REQUIRES_AUDIO_ASSET")
        if audio_stream_index is None:
            if asset.audio_stream_count > 1:
                raise VideoEditingError("MULTIPLE_SOURCE_AUDIO_TRACKS_REQUIRE_EXPLICIT_SELECTION")
            audio_stream_index = 0
        if type(audio_stream_index) is not int or not 0 <= audio_stream_index < asset.audio_stream_count:
            raise VideoEditingError("AUDIO_STREAM_INDEX_OUT_OF_RANGE")
        if not isinstance(ripple_track_ids, (tuple, list)) or any(type(item) is not str for item in ripple_track_ids):
            raise VideoEditingError("AUDIO_REVISION_RIPPLE_SCOPE_INVALID")
        selected = set(ripple_track_ids)
        known = {track.track_id for track in self.tracks}
        if len(selected) != len(ripple_track_ids) or not selected <= known or target_track.track_id not in selected:
            raise VideoEditingError("AUDIO_REVISION_RIPPLE_SCOPE_INVALID")
        overrides = {} if overlay_ranges is None else overlay_ranges
        if not isinstance(overrides, Mapping) or not set(overrides) <= {item.overlay_id for item in self.overlays}:
            raise VideoEditingError("AUDIO_REVISION_OVERLAY_RANGES_INVALID")

        existing = next((item for item in self.assets if item.asset_id == asset.asset_id), None)
        if existing is not None and (
            existing.sha256 != asset.sha256 or existing.duration_sec != asset.duration_sec
            or existing.has_video != asset.has_video or existing.audio_stream_count != asset.audio_stream_count
        ):
            raise VideoEditingError("TIMELINE_DUPLICATE_MEDIA_ASSET")
        assets = self.assets if existing is not None else (*self.assets, asset)
        replacement = replace(target, media_asset_id=asset.asset_id, source_in=start, source_out=end,
                              audio_stream_index=audio_stream_index, evidence_refs=asset.evidence_refs)
        # Compare the stored decimal clock, without treating binary addition
        # noise as an overlap or discarding a user's fractional position.
        delta = replacement.duration_sec - target.duration_sec
        target_start = _fraction_time(target.timeline_start)
        old_end = target_start + target.duration_sec
        tracks = []
        for track in self.tracks:
            clips = []
            for clip in track.clips:
                if clip.clip_id == clip_id:
                    clips.append(replacement)
                    continue
                if delta and track.track_id in selected:
                    clip_start = _fraction_time(clip.timeline_start)
                    clip_end = clip_start + clip.duration_sec
                    if clip_start < old_end and clip_end > target_start:
                        raise VideoEditingError("AUDIO_REVISION_INTERSECTING_CLIP_REQUIRES_REPLAN")
                    if clip_start >= old_end:
                        clip = replace(clip, timeline_start=clip_start + delta)
                clips.append(clip)
            tracks.append(replace(track, clips=tuple(clips)))
        overlays = []
        for overlay in self.overlays:
            if overlay.overlay_id in overrides:
                value = overrides[overlay.overlay_id]
                if not isinstance(value, (tuple, list)) or len(value) != 2:
                    raise VideoEditingError("AUDIO_REVISION_OVERLAY_RANGES_INVALID")
                overlay = replace(overlay, start_sec=value[0], end_sec=value[1])
            elif delta:
                overlay_start = _fraction_time(overlay.start_sec)
                overlay_end = _fraction_time(overlay.end_sec)
                if overlay_start < old_end and overlay_end > target_start:
                    raise VideoEditingError("AUDIO_REVISION_INTERSECTING_OVERLAY_REQUIRES_RANGE")
                if overlay_start >= old_end:
                    overlay = replace(overlay, start_sec=overlay_start + delta, end_sec=overlay_end + delta)
            overlays.append(overlay)
        candidate = replace(self, assets=assets, tracks=tuple(tracks), overlays=tuple(overlays))
        if self.transitions:
            from .media_rendering import _video_transition_plan
            for layer in candidate.tracks:
                if layer.kind == "video":
                    _video_transition_plan(candidate, sorted(layer.clips, key=lambda item: item.timeline_start))
        if candidate.to_dict() == self.to_dict():
            return self
        return self._commit(assets=assets, tracks=tuple(tracks), overlays=tuple(overlays))

    def trim(self, clip_id: str, *, source_in: float, source_out: float) -> "Timeline":
        self.validate()
        track, clip = self._find_clip(clip_id)
        asset = self._asset(clip.media_asset_id)
        if asset.is_still_image:
            raise VideoEditingError("STILL_IMAGE_HAS_NO_SOURCE_TIME_RANGE")
        source_start = _bounded_time(
            source_in, error_code="TRIM_RANGE_OUTSIDE_MEDIA_ASSET", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        source_end = _bounded_time(
            source_out, error_code="TRIM_RANGE_OUTSIDE_MEDIA_ASSET", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        if asset.duration_sec is None or source_end > asset.duration_sec or source_end <= source_start:
            raise VideoEditingError("TRIM_RANGE_OUTSIDE_MEDIA_ASSET")
        keyframe_start = (source_start - clip.source_in) / clip.playback_rate
        keyframe_end = (source_end - clip.source_in) / clip.playback_rate
        keyframes = _slice_transform_keyframes(clip.transform_keyframes, keyframe_start, keyframe_end)
        effects = tuple(replace(effect, keyframes=_slice_effect_keyframes(effect, keyframe_start,
                                                                           keyframe_end))
                        for effect in clip.effects)
        return self._replace_clip(track, replace(clip, source_in=source_start, source_out=source_end,
                                                transform_keyframes=keyframes, effects=effects))

    def split(self, clip_id: str, *, at_sec: float) -> "Timeline":
        self.validate()
        track, clip = self._find_clip(clip_id)
        at = _bounded_time(
            at_sec, error_code="SPLIT_POINT_OUTSIDE_CLIP", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        if clip.display_duration_sec is not None:
            if not 0 < at < clip.display_duration_sec:
                raise VideoEditingError("SPLIT_POINT_OUTSIDE_CLIP")
            first_duration = at
            second_duration = clip.display_duration_sec - at
        else:
            if not clip.source_in < at < clip.source_out:
                raise VideoEditingError("SPLIT_POINT_OUTSIDE_CLIP")
            first_duration = (at - clip.source_in) / clip.playback_rate
            second_duration = (clip.source_out - at) / clip.playback_rate
        first_keyframes = _slice_transform_keyframes(clip.transform_keyframes, Fraction(0), first_duration)
        second_keyframes = _slice_transform_keyframes(clip.transform_keyframes, first_duration,
                                                       first_duration + second_duration)
        first_effects = tuple(replace(effect, keyframes=_slice_effect_keyframes(
            effect, Fraction(0), first_duration,
        )) for effect in clip.effects)
        second_effects = tuple(replace(effect, keyframes=_slice_effect_keyframes(
            effect, first_duration, first_duration + second_duration,
        )) for effect in clip.effects)
        first_mix = replace(
            clip.audio_mix,
            fade_in_sec=min(clip.audio_mix.fade_in_sec, first_duration),
            fade_out_sec=0.0,
        )
        second_mix = replace(
            clip.audio_mix,
            fade_in_sec=0.0,
            fade_out_sec=min(clip.audio_mix.fade_out_sec, second_duration),
        )
        if clip.display_duration_sec is not None:
            first = replace(
                clip, clip_id=f"{clip.clip_id}-a", display_duration_sec=first_duration,
                audio_mix=first_mix, transform_keyframes=first_keyframes, effects=first_effects,
            )
            second = replace(
                clip, clip_id=f"{clip.clip_id}-b", display_duration_sec=second_duration,
                timeline_start=clip.timeline_start + first_duration, audio_mix=second_mix,
                transform_keyframes=second_keyframes, effects=second_effects,
            )
        else:
            first = replace(
                clip, clip_id=f"{clip.clip_id}-a", source_out=at, audio_mix=first_mix,
                transform_keyframes=first_keyframes, effects=first_effects,
            )
            second = replace(
                clip,
                clip_id=f"{clip.clip_id}-b",
                source_in=at,
                timeline_start=clip.timeline_start + (at - clip.source_in) / clip.playback_rate,
                audio_mix=second_mix,
                transform_keyframes=second_keyframes,
                effects=second_effects,
            )
        clips: List[TimelineClip] = []
        for item in track.clips:
            clips.extend((first, second) if item.clip_id == clip_id else (item,))
        updated = self._reflow(track, clips) if track.kind == "video" else replace(track, clips=tuple(clips))
        tracks = tuple(updated if item.track_id == track.track_id else item for item in self.tracks)
        transitions = tuple(
            replace(item, from_clip=second.clip_id if item.from_clip == clip_id else item.from_clip,
                    to_clip=first.clip_id if item.to_clip == clip_id else item.to_clip)
            for item in self.transitions
        )
        return self._commit(tracks=tracks, transitions=transitions)

    def set_playback_rate(self, clip_id: str, *, playback_rate: float) -> "Timeline":
        """Change source speed explicitly while keeping the source and stream identity."""
        track, clip = self._find_clip(clip_id)
        if clip.display_duration_sec is not None:
            raise VideoEditingError("STILL_IMAGE_HAS_NO_PLAYBACK_RATE")
        rate = _bounded_time(playback_rate, error_code="CLIP_PLAYBACK_RATE_INVALID", minimum=Fraction(1, 16), maximum=16)
        duration = (clip.source_out - clip.source_in) / rate
        mix = replace(clip.audio_mix, fade_in_sec=min(clip.audio_mix.fade_in_sec, duration / 2),
                      fade_out_sec=min(clip.audio_mix.fade_out_sec, duration / 2))
        keyframes = _slice_transform_keyframes(clip.transform_keyframes, Fraction(0), duration)
        effects = tuple(replace(effect, keyframes=_slice_effect_keyframes(
            effect, Fraction(0), duration,
        )) for effect in clip.effects)
        return self._replace_clip(track, replace(clip, playback_rate=rate, audio_mix=mix,
                                                 transform_keyframes=keyframes, effects=effects))

    def cut_source_intervals(self, cuts, *, synchronize: bool = True) -> "Timeline":
        """Apply a reviewed word selection as one immutable, synchronized transaction.

        Cuts are source coordinates of explicitly selected clip occurrences. The
        resulting timeline intervals remove matching video/audio/subtitle spans
        across tracks when synchronization is selected. Source media never change.
        """
        from fractions import Fraction
        from bisect import bisect_right
        self.validate()
        if (type(cuts) is not list or not cuts or len(cuts) > 100000
                or type(synchronize) is not bool):
            raise VideoEditingError("WORD_EDIT_SELECTION_INVALID")
        coordinates = []
        selected_ids = set()
        for cut in cuts:
            if type(cut) is not dict or set(cut) != {"clip_id", "source_in", "source_out"}:
                raise VideoEditingError("WORD_EDIT_SELECTION_INVALID")
            _track, clip = self._find_clip(cut["clip_id"])
            begin = _bounded_time(cut["source_in"], error_code="WORD_EDIT_OUTSIDE_CLIP")
            end = _bounded_time(cut["source_out"], error_code="WORD_EDIT_OUTSIDE_CLIP")
            if not clip.source_in <= begin < end <= clip.source_out:
                raise VideoEditingError("WORD_EDIT_OUTSIDE_CLIP")
            source_in, origin, rate = map(_fraction_time,
                                           (clip.source_in, clip.timeline_start, clip.playback_rate))
            coordinates.append((origin + (_fraction_time(begin) - source_in) / rate,
                                origin + (_fraction_time(end) - source_in) / rate))
            selected_ids.add(clip.clip_id)
        intervals = []
        for begin, end in sorted(coordinates):
            if intervals and begin <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
            else:
                intervals.append((begin, end))
        starts = [begin for begin, _end in intervals]
        ends = [end for _begin, end in intervals]
        removed_prefix = [Fraction(0)]
        for begin, end in intervals:
            removed_prefix.append(removed_prefix[-1] + end - begin)

        def shifted(point):
            completed = bisect_right(ends, point)
            removed = removed_prefix[completed]
            if completed < len(intervals) and starts[completed] < point:
                removed += point - starts[completed]
            return point - removed

        def surviving(begin, end):
            cursor = begin
            for index in range(bisect_right(ends, begin), len(intervals)):
                left, right = intervals[index]
                if left >= end:
                    break
                if cursor < left:
                    yield cursor, min(left, end)
                cursor = max(cursor, right)
                if cursor >= end:
                    return
            if cursor < end:
                yield cursor, end

        for transition in self.transitions:
            _track, right = self._find_clip(transition.to_clip)
            boundary = _fraction_time(right.timeline_start)
            half = _fraction_time(transition.duration_sec) / 2
            if any(begin < boundary + half and end > boundary - half for begin, end in intervals):
                raise VideoEditingError("WORD_EDIT_TRANSITION_CONFLICT")
        tracks, replacements = [], {}
        for track in self.tracks:
            clips = []
            for clip in track.clips:
                begin = _fraction_time(clip.timeline_start)
                rate = _fraction_time(clip.playback_rate)
                end = (begin + clip.duration_sec if clip.display_duration_sec is not None else
                       begin + (_fraction_time(clip.source_out) - _fraction_time(clip.source_in)) / rate)
                if not synchronize and clip.clip_id not in selected_ids:
                    clips.append(clip)
                    replacements[clip.clip_id] = [clip]
                    continue
                pieces = list(surviving(begin, end))
                result = []
                for index, (left, right) in enumerate(pieces):
                    # Time is exact until serialization. No 30fps word-boundary floor.
                    if right - left < Fraction(1, 1000000):
                        raise VideoEditingError("WORD_EDIT_REMAINDER_TOO_SHORT")
                    unchanged = len(pieces) == 1 and (left, right) == (begin, end)
                    identity = clip.clip_id if unchanged else clip.clip_id + "-w" + hashlib.sha256(
                        f"{left}:{right}".encode()).hexdigest()[:10]
                    duration = right - left
                    fade_in = min(clip.audio_mix.fade_in_sec, duration) if left == begin else 0.0
                    fade_out = min(clip.audio_mix.fade_out_sec, duration - fade_in) if right == end else Fraction(0)
                    if clip.display_duration_sec is not None:
                        part = replace(
                            clip, clip_id=identity, display_duration_sec=duration,
                            timeline_start=shifted(left),
                            audio_mix=replace(clip.audio_mix, fade_in_sec=fade_in, fade_out_sec=fade_out),
                        )
                    else:
                        part = replace(clip, clip_id=identity,
                            source_in=_fraction_time(clip.source_in) + (left - begin) * rate,
                            source_out=_fraction_time(clip.source_in) + (right - begin) * rate,
                            timeline_start=shifted(left),
                            audio_mix=replace(clip.audio_mix, fade_in_sec=fade_in, fade_out_sec=fade_out))
                    result.append(part)
                    clips.append(part)
                replacements[clip.clip_id] = result
            tracks.append(replace(track, clips=tuple(clips)))
        if not any(track.clips for track in tracks):
            raise VideoEditingError("WORD_EDIT_WOULD_REMOVE_ALL_AUDIO")
        overlays = []
        for overlay in self.overlays:
            if not synchronize:
                overlays.append(overlay)
                continue
            pieces = list(surviving(_fraction_time(overlay.start_sec), _fraction_time(overlay.end_sec)))
            for index, (begin, end) in enumerate(pieces):
                overlays.append(replace(overlay,
                    overlay_id=overlay.overlay_id if len(pieces) == 1 else f"{overlay.overlay_id}-w{index}",
                    start_sec=shifted(begin), end_sec=shifted(end)))
        transitions = []
        for transition in self.transitions:
            left, right = replacements[transition.from_clip], replacements[transition.to_clip]
            if not left or not right:
                raise VideoEditingError("WORD_EDIT_TRANSITION_CONFLICT")
            transitions.append(replace(transition, from_clip=left[-1].clip_id, to_clip=right[0].clip_id))
        return self._commit(tracks=tuple(tracks), overlays=tuple(overlays), transitions=tuple(transitions))

    def reorder(self, clip_id: str, *, new_index: int) -> "Timeline":
        self.validate()
        track, clip = self._find_clip(clip_id)
        if track.kind != "video" or track.layout != "sequence":
            raise VideoEditingError("REORDER_VIDEO_TRACK_ONLY")
        clips = [item for item in track.clips if item.clip_id != clip_id]
        if type(new_index) is not int or not 0 <= new_index <= len(clips):
            raise VideoEditingError("REORDER_INDEX_OUT_OF_RANGE")
        clips.insert(new_index, clip)
        updated = self._reflow(track, clips)
        tracks = tuple(updated if item.track_id == track.track_id else item for item in self.tracks)
        return self._commit(tracks=tracks)

    def delete(self, clip_id: str, *, ripple: bool = True, synchronize: bool = False) -> "Timeline":
        self.validate()
        if type(ripple) is not bool or type(synchronize) is not bool:
            raise VideoEditingError("DELETE_RIPPLE_FLAG_INVALID")
        track, removed = self._find_clip(clip_id)
        if synchronize:
            if not ripple:
                raise VideoEditingError("SYNCHRONIZED_DELETE_REQUIRES_RIPPLE")
            return self.cut_source_intervals(
                [{"clip_id": clip_id, "source_in": removed.source_in, "source_out": removed.source_out}],
                synchronize=True,
            )
        clips = tuple(item for item in track.clips if item.clip_id != clip_id)
        if ripple and (track.kind != "video" or track.layout == "free"):
            end = removed.timeline_start + removed.duration_sec
            if any(item.timeline_start < end and item.timeline_start + item.duration_sec > removed.timeline_start for item in clips):
                raise VideoEditingError("AUDIO_RIPPLE_OVERLAP_NOT_SUPPORTED")
            clips = tuple(replace(item, timeline_start=item.timeline_start - removed.duration_sec)
                          if item.timeline_start >= end else item for item in clips)
        updated = self._reflow(track, clips) if ripple and track.kind == "video" else replace(track, clips=clips)
        tracks = tuple(updated if item.track_id == track.track_id else item for item in self.tracks)
        transitions = tuple(item for item in self.transitions if clip_id not in {item.from_clip, item.to_clip})
        return self._commit(tracks=tracks, transitions=transitions)

    def add_transition(
        self,
        from_clip: str,
        to_clip: str,
        *,
        transition_type: str = "dissolve",
        duration_sec: Fraction | float = Fraction(3, 10),
        reason: str,
    ) -> "Timeline":
        self.validate()
        track, from_item = self._find_clip(from_clip)
        other, to_item = self._find_clip(to_clip)
        if track.track_id != other.track_id or track.kind != "video":
            raise VideoEditingError("TRANSITION_REQUIRES_ONE_VIDEO_TRACK")
        ids = [clip.clip_id for clip in track.clips]
        if ids.index(to_clip) != ids.index(from_clip) + 1:
            raise VideoEditingError("TRANSITION_REQUIRES_ADJACENT_CLIPS")
        if transition_type != "cut" and (not isinstance(reason, str) or not reason.strip()):
            raise VideoEditingError("TRANSITION_REASON_REQUIRED")
        duration = _bounded_time(
            duration_sec,
            error_code="INVALID_TRANSITION_DURATION",
            minimum=0,
            maximum=MAX_TRANSITION_DURATION_SEC,
        )
        if duration > min(from_item.duration_sec, to_item.duration_sec):
            raise VideoEditingError("TRANSITION_DURATION_EXCEEDS_CLIP")
        transition = Transition(from_clip, to_clip, transition_type, duration,
                                reason.strip() if isinstance(reason, str) else "")
        return self._commit(transitions=(*self.transitions, transition))

    def set_audio_mix(self, clip_id: str, mix: AudioMix) -> "Timeline":
        self.validate()
        if not isinstance(mix, AudioMix):
            raise VideoEditingError("INVALID_AUDIO_MIX")
        track, clip = self._find_clip(clip_id)
        mix.validate(duration_sec=clip.duration_sec)
        return self._replace_clip(track, replace(clip, audio_mix=mix))

    def move_audio_clip(self, clip_id: str, *, timeline_start: float) -> "Timeline":
        """Move one manually positioned audio-track clip without changing its source range."""
        self.validate()
        track, clip = self._find_clip(clip_id)
        if track.kind == "video":
            raise VideoEditingError("MOVE_AUDIO_CLIP_AUDIO_TRACK_ONLY")
        start = _bounded_time(
            timeline_start,
            error_code="INVALID_CLIP_RANGE",
            minimum=0,
            maximum=MAX_EDIT_TIME_SEC,
        )
        if start + clip.duration_sec > MAX_EDIT_TIME_SEC:
            raise VideoEditingError("TIMELINE_DURATION_OUT_OF_RANGE")
        return self._replace_clip(track, replace(clip, timeline_start=start))

    def add_caption(self, start_sec: Fraction | float, end_sec: Fraction | float, text: str, *,
                    layout_profile: Optional[str] = None, style: Optional[CaptionStyle] = None) -> "Timeline":
        return self._add_overlay("caption", start_sec, end_sec, text, layout_profile=layout_profile,style=style)

    def add_text_overlay(self, start_sec: Fraction | float, end_sec: Fraction | float, text: str) -> "Timeline":
        return self._add_overlay("text", start_sec, end_sec, text)

    def _add_overlay(self, kind: str, start_sec: Fraction | float, end_sec: Fraction | float, text: str, *,
                     layout_profile: Optional[str] = None, style: Optional[CaptionStyle] = None) -> "Timeline":
        self.validate()
        start = _bounded_time(
            start_sec, error_code="INVALID_TIMED_OVERLAY", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        end = _bounded_time(
            end_sec, error_code="INVALID_TIMED_OVERLAY", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        if end <= start or not str(text).strip():
            raise VideoEditingError("INVALID_TIMED_OVERLAY")
        overlay = TimedOverlay(_id("OVERLAY"), kind, start, end, str(text).strip(), layout_profile,style)
        return self._commit(overlays=(*self.overlays, overlay))

    def update_caption(self, overlay_id: str, start_sec: float, end_sec: float,
                       text: str, *, layout_profile: Optional[str] = None, style: Optional[CaptionStyle] = None) -> "Timeline":
        """Replace one caption through the normal version/undo operation."""
        self.validate()
        matches = [item for item in self.overlays if item.overlay_id == overlay_id]
        if not isinstance(overlay_id, str) or not overlay_id or not matches:
            raise VideoEditingError("OVERLAY_NOT_FOUND")
        if len(matches) != 1:
            raise VideoEditingError("OVERLAY_ID_AMBIGUOUS")
        if matches[0].kind != "caption":
            raise VideoEditingError("CAPTION_OVERLAY_REQUIRED")
        if not isinstance(text, str):
            raise VideoEditingError("INVALID_TIMED_OVERLAY")
        updated = replace(matches[0], start_sec=start_sec, end_sec=end_sec,
                          text=text, layout_profile=layout_profile,style=style)
        return self._commit(overlays=tuple(updated if item.overlay_id == overlay_id else item
                                           for item in self.overlays))

    def remove_overlay(self, overlay_id: str) -> "Timeline":
        """Remove one explicitly selected overlay, retaining every other edit."""
        self.validate()
        matches = [item for item in self.overlays if item.overlay_id == overlay_id]
        if not isinstance(overlay_id, str) or not overlay_id or not matches:
            raise VideoEditingError("OVERLAY_NOT_FOUND")
        if len(matches) != 1:
            raise VideoEditingError("OVERLAY_ID_AMBIGUOUS")
        return self._commit(overlays=tuple(item for item in self.overlays if item.overlay_id != overlay_id))

    def add_chapter(self, title: str, start_sec: Any, *, chapter_id: str | None = None) -> "Timeline":
        self.validate()
        chapter = TimelineChapter(chapter_id or _id("CHAPTER"), title, start_sec)
        if any(item.chapter_id == chapter.chapter_id for item in self.chapters):
            raise VideoEditingError("TIMELINE_CHAPTER_ALREADY_EXISTS")
        chapters = tuple(sorted((*self.chapters, chapter), key=lambda item: (item.start_sec, item.chapter_id)))
        return self._commit(chapters=chapters)

    def update_chapter(self, chapter_id: str, title: str, start_sec: Any) -> "Timeline":
        self.validate()
        matches = [item for item in self.chapters if item.chapter_id == chapter_id]
        if len(matches) != 1:
            raise VideoEditingError("TIMELINE_CHAPTER_NOT_FOUND")
        updated = TimelineChapter(chapter_id, title, start_sec)
        chapters = tuple(sorted(
            (updated if item.chapter_id == chapter_id else item for item in self.chapters),
            key=lambda item: (item.start_sec, item.chapter_id),
        ))
        return self._commit(chapters=chapters)

    def remove_chapter(self, chapter_id: str) -> "Timeline":
        self.validate()
        if not any(item.chapter_id == chapter_id for item in self.chapters):
            raise VideoEditingError("TIMELINE_CHAPTER_NOT_FOUND")
        return self._commit(chapters=tuple(item for item in self.chapters if item.chapter_id != chapter_id))

    def set_frame_crop(self, clip_id: str, crop: Optional[FrameCrop]) -> "Timeline":
        self.validate()
        track,clip=self._find_clip(clip_id)
        if track.kind != "video":raise VideoEditingError("FRAME_CROP_VIDEO_ONLY")
        if crop is not None:
            if not isinstance(crop,FrameCrop):raise VideoEditingError("FRAME_CROP_INVALID")
            crop.to_dict()
        return self._replace_clip(track,replace(clip,frame_crop=crop))

    def set_format(self, format_ratio: str) -> "Timeline":
        if format_ratio not in ALLOWED_FORMAT_RATIOS:
            raise VideoEditingError("FORMAT_RATIO_NOT_SUPPORTED")
        return self._commit(format_ratio=format_ratio)

    def apply_edit_batch(self, commands: Any, *, expected_version: int) -> Timeline:
        """Reuse the domain edit operations as one atomic user-action version."""
        self.validate()
        if type(expected_version) is not int or expected_version != self.version:
            raise VideoEditingError("TIMELINE_VERSION_MISMATCH")
        if not isinstance(commands, (tuple, list)):
            raise VideoEditingError("EDIT_COMMANDS_INVALID")
        if not commands:
            raise VideoEditingError("EDIT_COMMANDS_REQUIRED")
        if len(commands) > _MAX_EDIT_BATCH_COMMANDS:
            raise VideoEditingError("EDIT_COMMAND_COUNT_EXCEEDED")

        updated = self
        for raw_command in commands:
            if not isinstance(raw_command, Mapping):
                raise VideoEditingError("EDIT_COMMAND_INVALID")
            command = dict(raw_command)
            op = command.get("op")
            if not isinstance(op, str) or op not in _EDIT_BATCH_COMMAND_FIELDS:
                raise VideoEditingError("EDIT_OPERATION_NOT_SUPPORTED")
            required, optional = _EDIT_BATCH_COMMAND_FIELDS[op]
            fields = set(command)
            if not required <= fields or fields - required - optional:
                raise VideoEditingError("EDIT_COMMAND_FIELDS_INVALID")

            if op == "add_track":
                kind = command["kind"]
                layout = command.get("layout", "sequence" if kind == "video" else "free")
                if layout not in {"sequence", "free"}:
                    raise VideoEditingError("TIMELINE_TRACK_LAYOUT_INVALID")
                if any(track.track_id == command["track_id"] for track in updated.tracks):
                    raise VideoEditingError("TIMELINE_TRACK_ALREADY_EXISTS")
                updated = updated._commit(tracks=(
                    *updated.tracks,
                    TimelineTrack(command["track_id"], kind, layout=layout),
                ))
            elif op == "insert":
                updated = updated.add_clip(
                    command["source_id"],
                    source_in=command.get("source_in", {"num": 0, "den": 1}),
                    source_out=command.get("source_out"),
                    display_duration_sec=command.get("display_duration_sec"),
                    track_id=command["track_id"],
                    purpose=command.get("purpose", ""),
                    audio_role=command.get("audio_role", "original"),
                    timeline_start=command.get("timeline_start"),
                    audio_stream_index=command.get("audio_stream_index"),
                    video_stream_index=command.get("video_stream_index"),
                )
            elif op == "trim":
                updated = updated.trim(
                    command["clip_id"],
                    source_in=command["source_in"],
                    source_out=command["source_out"],
                )
            elif op == "replace_clip_source":
                updated = updated.replace_clip_source(
                    command["clip_id"],
                    asset=updated._asset(command["source_id"]),
                    source_in=command["source_in"],
                    audio_stream_index=command.get("audio_stream_index"),
                    display_duration_sec=command.get("display_duration_sec"),
                )
            elif op == "replace_audio_source":
                updated = updated.replace_audio_source(
                    command["clip_id"],
                    asset=updated._asset(command["source_id"]),
                    source_in=command["source_in"],
                    source_out=command["source_out"],
                    ripple_track_ids=command["ripple_track_ids"],
                    audio_stream_index=command.get("audio_stream_index"),
                    overlay_ranges=command.get("overlay_ranges"),
                )
            elif op == "split":
                updated = updated.split(command["clip_id"], at_sec=command["at_sec"])
            elif op == "delete":
                updated = updated.delete(
                    command["clip_id"], ripple=command.get("ripple", True),
                    synchronize=command.get("synchronize", False),
                )
            elif op == "sync_cut":
                updated = updated.cut_source_intervals(
                    command["cuts"], synchronize=command.get("synchronize", True)
                )
            elif op == "reorder":
                updated = updated.reorder(
                    command["clip_id"], new_index=command["new_index"]
                )
            elif op == "audio_mix":
                value = command["mix"]
                if not isinstance(value, AudioMix):
                    if (
                        not isinstance(value, Mapping)
                        or set(value) - _EDIT_BATCH_AUDIO_MIX_FIELDS
                    ):
                        raise VideoEditingError("INVALID_AUDIO_MIX")
                    try:
                        value = AudioMix(**dict(value))
                    except (TypeError, ValueError) as exc:
                        raise VideoEditingError("INVALID_AUDIO_MIX") from exc
                updated = updated.set_audio_mix(command["clip_id"], value)
            elif op == "set_display_duration":
                updated = updated.set_display_duration(
                    command["clip_id"], command["display_duration_sec"],
                )
            elif op == "set_audio_bus":
                bus = command["bus"]
                if not isinstance(bus, Mapping):
                    raise VideoEditingError("AUDIO_BUS_INVALID")
                updated = updated.set_audio_bus(bus)
            elif op == "remove_audio_bus":
                updated = updated.remove_audio_bus(command["bus_id"])
            elif op == "set_transcript":
                transcript = command["transcript"]
                if not isinstance(transcript, Mapping):
                    raise VideoEditingError("MEDIA_TRANSCRIPT_INVALID")
                updated = updated.set_transcript(transcript)
            elif op == "remove_transcript":
                updated = updated.remove_transcript(command["transcript_id"])
            elif op == "set_video_color_interpretation":
                interpretation = command["interpretation"]
                if not isinstance(interpretation, Mapping):
                    raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_INVALID")
                updated = updated.set_video_color_interpretation(interpretation)
            elif op == "remove_video_color_interpretation":
                updated = updated.remove_video_color_interpretation(
                    command["asset_id"], command["video_stream_index"],
                )
            elif op == "track_audio_routing":
                if not any(key in command for key in ("gain", "pan", "sends")):
                    raise VideoEditingError("AUDIO_TRACK_ROUTING_EMPTY")
                updated = updated.set_track_audio_routing(
                    command["track_id"],
                    gain=command.get("gain"),
                    pan=command.get("pan"),
                    sends=command.get("sends"),
                )
            elif op == "move_video_clip":
                updated = updated.move_video_clip(command["clip_id"], timeline_start=command["timeline_start"])
            elif op == "playback_rate":
                updated = updated.set_playback_rate(command["clip_id"], playback_rate=command["playback_rate"])
            elif op == 'track_controls':
                updated = updated.set_track_controls(command['track_id'],
                    **{key:command[key] for key in ('locked','muted','solo','hidden') if key in command})
            elif op == "move_audio_clip":
                updated = updated.move_audio_clip(
                    command["clip_id"], timeline_start=command["timeline_start"]
                )
            elif op == "transition":
                updated = updated.add_transition(
                    command["from_clip"], command["to_clip"],
                    transition_type=command.get("transition_type", "dissolve"),
                    duration_sec=command.get("duration_sec", {"num": 3, "den": 10}),
                    reason=command["reason"],
                )
            elif op == "frame_crop":
                crop = command["crop"]
                updated = updated.set_frame_crop(
                    command["clip_id"], FrameCrop.from_dict(crop) if crop is not None else None
                )
            elif op == "video_placement":
                placement = command["placement"]
                updated = updated.set_video_placement(
                    command["clip_id"], VideoPlacement.from_dict(placement) if placement is not None else None
                )
            elif op == "set_video_effects":
                updated = updated.set_video_effects(command["clip_id"], command["effects"])
            elif op == "set_transform_keyframes":
                updated = updated.set_transform_keyframes(command["clip_id"], command["keyframes"])
            elif op == "add_caption":
                style_value = command.get("style")
                if "style" in command and style_value is not None and not isinstance(style_value, Mapping):
                    raise VideoEditingError("CAPTION_STYLE_INVALID")
                updated = updated.add_caption(
                    command["start_sec"],
                    command["end_sec"],
                    command["text"],
                    layout_profile=command.get("layout_profile"),
                    style=CaptionStyle.from_dict(dict(style_value)) if isinstance(style_value, Mapping) else None,
                )
            elif op == "update_caption":
                profile = command.get("layout_profile")
                if "layout_profile" not in command:
                    profile = next(
                        (
                            overlay.layout_profile
                            for overlay in updated.overlays
                            if overlay.overlay_id == command["overlay_id"]
                        ),
                        None,
                    )
                if "style" in command:
                    style_value = command["style"]
                    if style_value is not None and not isinstance(style_value, Mapping):
                        raise VideoEditingError("CAPTION_STYLE_INVALID")
                    style = CaptionStyle.from_dict(dict(style_value)) if isinstance(style_value, Mapping) else None
                elif "layout_profile" in command:
                    style = None
                else:
                    style = next((item.style for item in updated.overlays if item.overlay_id == command["overlay_id"]), None)
                updated = updated.update_caption(
                    command["overlay_id"],
                    command["start_sec"],
                    command["end_sec"],
                    command["text"],
                    layout_profile=profile,
                    style=style,
                )
            elif op == "remove_overlay":
                updated = updated.remove_overlay(command["overlay_id"])
            elif op == "add_chapter":
                updated = updated.add_chapter(
                    command["title"], command["start_sec"], chapter_id=command.get("chapter_id"),
                )
            elif op == "update_chapter":
                updated = updated.update_chapter(
                    command["chapter_id"], command["title"], command["start_sec"],
                )
            elif op == "remove_chapter":
                updated = updated.remove_chapter(command["chapter_id"])
            elif op == "set_format":
                updated = updated.set_format(command["format_ratio"])
            else:
                raise VideoEditingError("EDIT_OPERATION_NOT_SUPPORTED")

        committed = replace(
            updated,
            version=self.version + 1,
            _history=self._history + (self._snapshot(),),
        )
        committed.validate()
        return committed

    def undo(self) -> "Timeline":
        if not self._history:
            raise VideoEditingError("TIMELINE_NO_UNDO")
        snapshot = self._history[-1]
        return replace(
            self,
            assets=snapshot.assets,
            tracks=snapshot.tracks,
            audio_buses=snapshot.audio_buses,
            transcripts=snapshot.transcripts,
            video_color_interpretations=snapshot.video_color_interpretations,
            transitions=snapshot.transitions,
            overlays=snapshot.overlays,
            format_ratio=snapshot.format_ratio,
            chapters=snapshot.chapters,
            version=self.version + 1,
            _history=self._history[:-1],
        )

    @property
    def duration_sec(self) -> Fraction:
        """Return the existing clip extent, independently of the editor view."""
        self.validate()
        return max(
            (clip.timeline_start + clip.duration_sec for track in self.tracks for clip in track.clips),
            default=Fraction(0),
        )

    def _view_digest(self) -> str:
        document = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(document.encode("utf-8")).hexdigest()

    def describe_window(
        self, *, start_sec: Fraction | float = 0, span_sec: Fraction | float = 30, playhead_sec: Fraction | float = 0,
    ) -> Dict[str, Any]:
        """Project an unchanged timeline into a bounded horizontal editor view.

        Ranges use this model's seconds, not inferred source PTS or word times.
        The caller still owns Artifact scope, source resolution and playback.
        """
        self.validate()
        requested_start = _bounded_time(start_sec, error_code="TIMELINE_VIEW_INVALID",
                                        minimum=0, maximum=MAX_EDIT_TIME_SEC)
        requested_span = _bounded_time(span_sec, error_code="TIMELINE_VIEW_INVALID",
                                       minimum=Fraction(1, 1_000_000), maximum=MAX_EDIT_TIME_SEC)
        duration = self.duration_sec
        playhead = _bounded_time(playhead_sec, error_code="TIMELINE_PLAYHEAD_OUT_OF_RANGE",
                                 minimum=0, maximum=duration)
        span = min(requested_span, duration) if duration else requested_span
        start = min(requested_start, max(Fraction(0), duration - span))
        end = start + span
        assets = {asset.asset_id: asset for asset in self.assets}
        active = []
        tracks = []
        visible_clip_ids = set()
        for track in self.tracks:
            visible = []
            for clip in track.clips:
                clip_start, clip_end = clip.timeline_start, clip.timeline_start + clip.duration_sec
                asset = assets[clip.media_asset_id]
                if clip_start <= playhead < clip_end:
                    active.append({
                        "clip_id": clip.clip_id, "track_id": track.track_id,
                        "media_asset_id": clip.media_asset_id, "source_sha256": asset.sha256,
                        "source_position": (None if clip.display_duration_sec is not None else
                                            _time_json(clip.source_in + (playhead - clip_start) * clip.playback_rate)),
                        "source_time_state": ("no_intrinsic_duration" if clip.display_duration_sec is not None
                                              else "source_clock_position"),
                        "audio_role": clip.audio_role, "audio_stream_index": clip.audio_stream_index,
                        "stream_index_kind": ("no_audio_stream" if clip.display_duration_sec is not None else
                                              "audio_type_relative_ordinal"),
                        "mapping_method": ("still_frame_display_time" if clip.display_duration_sec is not None else
                                           "current_unit_rate_timeline_seconds" if clip.playback_rate == 1 else
                                           "source_in_plus_timeline_offset_times_playback_rate"),
                        **({"playback_rate":_time_json(clip.playback_rate)} if clip.playback_rate != 1 else {}),
                    })
                visible_start, visible_end = max(start, clip_start), min(end, clip_end)
                if visible_start >= visible_end:
                    continue
                visible_clip_ids.add(clip.clip_id)
                visible.append({
                    "clip": clip.to_dict(), "source_sha256": asset.sha256,
                    "media_kind": asset.media_kind,
                    "visible_timeline_range": [_time_json(visible_start), _time_json(visible_end)],
                    "visible_source_range": (None if clip.display_duration_sec is not None else [
                        _time_json(clip.source_in + (visible_start - clip_start) * clip.playback_rate),
                        _time_json(clip.source_in + (visible_end - clip_start) * clip.playback_rate),
                    ]),
                    "start_fraction": float((visible_start - start) / span),
                    "width_fraction": float((visible_end - visible_start) / span),
                    "clipped_at_left": clip_start < start, "clipped_at_right": clip_end > end,
                })
            tracks.append({"track_id": track.track_id, "kind": track.kind, "clips": visible,
                           **{name:getattr(track,name) for name in ('locked','muted','solo','hidden')}})
        overlays = []
        for overlay in self.overlays:
            a, b = max(start, overlay.start_sec), min(end, overlay.end_sec)
            if a < b:
                overlays.append({
                    "overlay": overlay.to_dict(), "visible_range": [_time_json(a), _time_json(b)],
                    "start_fraction": float((a - start) / span), "width_fraction": float((b - a) / span),
                })
        chapter_markers = [
            {"chapter": chapter.to_dict(),
             "timeline_fraction": float((chapter.start_sec - start) / span)}
            for chapter in self.chapters if start <= chapter.start_sec <= end
        ]
        active_chapter = max(
            (chapter for chapter in self.chapters if chapter.start_sec <= playhead),
            key=lambda chapter: chapter.start_sec,
            default=None,
        )
        transitions = [transition.to_dict() for transition in self.transitions
                       if transition.from_clip in visible_clip_ids or transition.to_clip in visible_clip_ids]
        state = {
            "timeline_version": self.version, "timeline_digest": self._view_digest(),
            "window_start": _time_json(start), "window_span": _time_json(span),
            "playhead": _time_json(playhead),
        }
        return {
            "schema": "oevira.media.timeline-window.v1", "state": state, "duration": _time_json(duration),
            "window": {
                "start": _time_json(start), "end": _time_json(end), "span": _time_json(span),
                "clamped": start != requested_start or span != requested_span,
                "scroll_fraction": float(start / (duration - span)) if duration > span else 0.0,
            },
            "playhead": {
                "time": _time_json(playhead),
                "timeline_fraction": float(playhead / duration) if duration else None,
                "window_fraction": float((playhead - start) / span) if start <= playhead <= end else None,
                "inside_window": start <= playhead <= end,
                "at_timeline_end": bool(duration and playhead == duration),
                "active_clips": active,
                "active_chapter": active_chapter.to_dict() if active_chapter else None,
            },
            "tracks": tracks, "overlays": overlays, "chapters": chapter_markers,
            "related_transition_plans": transitions,
            "range_convention": "half_open_clip_and_overlay_ranges",
            "source_pts_mapping": "RATIONAL_SOURCE_CLOCKS_STORED; PREVIEW_DECODE_PTS_NOT_YET_MEASURED",
            "waveform": None, "waveform_state": "NOT_COMPUTED",
            "render_and_playback_acceptance": "REQUIRES_ACTUAL_ARTIFACT_AND_PLAYER_REVIEW",
        }

    def navigate_window(
        self, state: Mapping[str, Any], *, action: str, value: float,
        anchor_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Pan, zoom or seek without committing an edit or changing source media."""
        self.validate()
        fields = {"timeline_version", "timeline_digest", "window_start", "window_span", "playhead"}
        if not isinstance(state, Mapping) or set(state) != fields:
            raise VideoEditingError("TIMELINE_VIEW_STATE_INVALID")
        if (type(state["timeline_version"]) is not int or state["timeline_version"] != self.version
                or state["timeline_digest"] != self._view_digest()):
            raise VideoEditingError("TIMELINE_VIEW_SOURCE_CHANGED")
        current = self.describe_window(start_sec=state["window_start"], span_sec=state["window_span"],
                                       playhead_sec=state["playhead"])
        if current["state"] != dict(state):
            raise VideoEditingError("TIMELINE_VIEW_STATE_INVALID")
        start = _fraction_time(state["window_start"])
        span = _fraction_time(state["window_span"])
        playhead = _fraction_time(state["playhead"])
        if action == "pan":
            if anchor_sec is not None:
                raise VideoEditingError("TIMELINE_VIEW_INVALID")
            delta = _bounded_time(value, error_code="TIMELINE_VIEW_INVALID",
                                  minimum=-MAX_EDIT_TIME_SEC, maximum=MAX_EDIT_TIME_SEC)
            start = min(max(Fraction(0), start + delta), max(Fraction(0), self.duration_sec - span))
        elif action == "zoom":
            factor = _bounded_number(value, error_code="TIMELINE_VIEW_INVALID",
                                     minimum=0.000001, maximum=MAX_SERIALIZED_INTEGER)
            anchor = start + span / 2 if anchor_sec is None else _bounded_time(
                anchor_sec, error_code="TIMELINE_VIEW_INVALID", minimum=start, maximum=start + span,
            )
            fraction = (anchor - start) / span
            new_span = max(Fraction(1, 1_000_000), min(Fraction(MAX_EDIT_TIME_SEC), span / Fraction(str(factor))))
            if self.duration_sec:
                new_span = min(new_span, self.duration_sec)
            start = min(max(Fraction(0), anchor - fraction * new_span), max(Fraction(0), self.duration_sec - new_span))
            span = new_span
        elif action == "seek":
            if anchor_sec is not None:
                raise VideoEditingError("TIMELINE_VIEW_INVALID")
            playhead = _bounded_time(value, error_code="TIMELINE_PLAYHEAD_OUT_OF_RANGE",
                                     minimum=0, maximum=self.duration_sec)
        else:
            raise VideoEditingError("TIMELINE_VIEW_ACTION_NOT_SUPPORTED")
        return self.describe_window(start_sec=start, span_sec=span, playhead_sec=playhead)

    def to_export_request(self) -> Dict[str, Any]:
        self.validate()
        return {
            "state": "render_ready" if any(track.clips for track in self.tracks) else "empty",
            "render_owner": "oevira.media_rendering.render_timeline",
            "limitations": ["Final output is not created until a version-bound render job completes."],
            "external_fetch": False,
            "format_ratio": self.format_ratio,
            "timeline_version": self.version,
        }

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "version": self.version,
            "format_ratio": self.format_ratio,
            "assets": [asset.to_dict() for asset in self.assets],
            "tracks": [track.to_dict() for track in self.tracks],
            **({"audio_buses": [item.to_dict() for item in self.audio_buses]}
               if self.audio_buses else {}),
            **({"transcripts": [item.to_dict() for item in self.transcripts]}
               if self.transcripts else {}),
            **({"video_color_interpretations": [item.to_dict() for item in self.video_color_interpretations]}
               if self.video_color_interpretations else {}),
            "transitions": [item.to_dict() for item in self.transitions],
            "overlays": [item.to_dict() for item in self.overlays],
            "chapters": [item.to_dict() for item in self.chapters],
        }


@dataclass(frozen=True)
class EditDecision:
    source_clip_ref: str
    in_sec: Optional[float]
    out_sec: Optional[float]
    operation: str
    reason: str
    confidence: float
    narrative_beat: str
    affected_audio: Tuple[str, ...] = field(default_factory=tuple)
    affected_text: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.operation, str) or self.operation not in ALLOWED_OPERATIONS:
            raise VideoEditingError("EDL_OPERATION_NOT_ALLOWED")
        if (
            not isinstance(self.source_clip_ref, str)
            or not self.source_clip_ref.strip()
            or not isinstance(self.reason, str)
            or not self.reason.strip()
            or not isinstance(self.narrative_beat, str)
            or not self.narrative_beat.strip()
        ):
            raise VideoEditingError("EDL_REQUIRES_SOURCE_REASON_AND_NARRATIVE_BEAT")
        _bounded_number(
            self.confidence, error_code="EDL_CONFIDENCE_MUST_BE_0_TO_1", minimum=0, maximum=1
        )
        in_sec = None if self.in_sec is None else _bounded_number(
            self.in_sec, error_code="EDL_TIME_OUT_OF_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        out_sec = None if self.out_sec is None else _bounded_number(
            self.out_sec, error_code="EDL_TIME_OUT_OF_RANGE", minimum=0, maximum=MAX_EDIT_TIME_SEC
        )
        if out_sec is not None and in_sec is not None and out_sec <= in_sec:
            raise VideoEditingError("EDL_OUT_MUST_FOLLOW_IN")
        if self.operation == "trim" and (in_sec is None or out_sec is None):
            raise VideoEditingError("EDL_TRIM_REQUIRES_IN_AND_OUT")
        if self.operation == "split" and in_sec is None:
            raise VideoEditingError("EDL_SPLIT_REQUIRES_IN")
        if not isinstance(self.metadata, Mapping):
            raise VideoEditingError("EDL_METADATA_NOT_JSON_SAFE")
        metadata = _json_safe_copy(self.metadata)
        if self.operation == "reorder":
            new_index = metadata.get("new_index")
            if type(new_index) is not int or not 0 <= new_index <= MAX_SERIALIZED_INTEGER:
                raise VideoEditingError("EDL_REORDER_INDEX_INVALID")
        if self.operation == "delete" and "ripple" in metadata and type(metadata["ripple"]) is not bool:
            raise VideoEditingError("EDL_DELETE_RIPPLE_INVALID")
        if self.operation == "word_cut":
            if (set(metadata) != {"cuts", "synchronize"} or type(metadata["cuts"]) is not list
                    or not metadata["cuts"] or len(metadata["cuts"]) > 100000
                    or type(metadata["synchronize"]) is not bool):
                raise VideoEditingError("WORD_EDIT_SELECTION_INVALID")
        if self.operation == "transition":
            duration = _bounded_number(
                metadata.get("duration_sec", 0.3),
                error_code="INVALID_TRANSITION_DURATION",
                minimum=0,
                maximum=MAX_TRANSITION_DURATION_SEC,
            )
            if duration <= 0 or not str(metadata.get("to_clip") or "").strip():
                raise VideoEditingError("EDL_TRANSITION_METADATA_INVALID")
            transition_type = metadata.get("transition_type", "dissolve")
            if (not isinstance(transition_type, str)
                    or transition_type not in SUPPORTED_VIDEO_TRANSITION_TYPES):
                raise VideoEditingError("TRANSITION_RENDER_NOT_SUPPORTED")
        for values in (self.affected_audio, self.affected_text):
            if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
                raise VideoEditingError("EDL_AFFECTED_FIELDS_INVALID")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "source_clip_ref": self.source_clip_ref,
            "in_sec": self.in_sec,
            "out_sec": self.out_sec,
            "operation": self.operation,
            "reason": self.reason,
            "confidence": self.confidence,
            "affected_narrative_beat": self.narrative_beat,
            "affected_audio": list(self.affected_audio),
            "affected_text": list(self.affected_text),
            "metadata": _json_safe_copy(self.metadata),
        }


@dataclass(frozen=True)
class EditDecisionList:
    candidate_id: str
    source_evidence_refs: Tuple[str, ...]
    decisions: Tuple[EditDecision, ...]
    status: str = "candidate"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise VideoEditingError("EDL_CANDIDATE_ID_REQUIRED")
        if not isinstance(self.source_evidence_refs, tuple) or not isinstance(self.decisions, tuple):
            raise VideoEditingError("EDL_CANDIDATE_SHAPE_INVALID")
        if self.source_evidence_refs:
            raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
        if not self.decisions:
            raise VideoEditingError("EDL_REQUIRES_DECISIONS")
        for decision in self.decisions:
            if not isinstance(decision, EditDecision):
                raise VideoEditingError("EDL_CANDIDATE_SHAPE_INVALID")
            decision.validate()

    def review(self, *, reviewer: str, approved: bool, reviewed_at: str) -> "EditDecisionList":
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise VideoEditingError("EDL_HUMAN_REVIEWER_REQUIRED")
        raise VideoEditingError("CANONICAL_EDL_APPROVAL_NOT_CONNECTED")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        if self.status != "candidate" or self.reviewed_by is not None or self.reviewed_at is not None:
            raise VideoEditingError("EDL_APPROVAL_STATE_NOT_TRUSTED")
        return {
            "candidate_id": self.candidate_id,
            "source_evidence_refs": list(self.source_evidence_refs),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "status": "candidate",
            "reviewed_by": None,
            "reviewed_at": None,
            "approval_required": True,
            "canonical_evidence_binding": "unavailable",
            "ai_is_derivative": True,
        }


def propose_edl(source_evidence_refs: Iterable[str], decisions: Iterable[EditDecision]) -> EditDecisionList:
    refs = tuple(str(ref).strip() for ref in source_evidence_refs if str(ref).strip())
    if refs:
        raise VideoEditingError("CANONICAL_EVIDENCE_BINDING_UNAVAILABLE")
    candidate = EditDecisionList(_id("EDL"), (), tuple(decisions))
    candidate.validate()
    return candidate


def _apply_edl_operations(timeline: Timeline, edl: EditDecisionList) -> Timeline:
    """Pure immutable preview; authorization is owned by the Artifact operation."""
    timeline.validate()
    edl.to_dict()  # Reject client-forged approved/reviewer fields.
    result = timeline
    for decision in edl.decisions:
        if decision.operation == "trim":
            result = result.trim(decision.source_clip_ref, source_in=decision.in_sec, source_out=decision.out_sec)
        elif decision.operation == "split":
            result = result.split(decision.source_clip_ref, at_sec=decision.in_sec)
        elif decision.operation == "delete":
            result = result.delete(decision.source_clip_ref, ripple=decision.metadata.get("ripple", True))
        elif decision.operation == "reorder":
            result = result.reorder(decision.source_clip_ref, new_index=decision.metadata["new_index"])
        elif decision.operation == "transition":
            result = result.add_transition(decision.source_clip_ref, decision.metadata["to_clip"],
                duration_sec=decision.metadata.get("duration_sec", .3), reason=decision.reason)
        elif decision.operation == "word_cut":
            result = result.cut_source_intervals(decision.metadata["cuts"], synchronize=decision.metadata["synchronize"])
        else:
            raise VideoEditingError("EDL_OPERATION_NOT_IMPLEMENTED")
    return result


def apply_edl_suggestion(timeline: Timeline, edl: EditDecisionList, *, actor=None, scope=None,
                         project_id=None, request_id=None, expected_resource=None) -> Timeline:
    """Candidate operations must go through a versioned, authorized Media API."""
    if (not isinstance(actor, dict) or actor.get("role") not in {"owner", "editor"}
            or not isinstance(actor.get("workspace"), dict)
            or actor["workspace"].get("id") != scope
            or not isinstance(project_id, str) or not project_id
            or not isinstance(request_id, str) or not request_id
            or expected_resource != timeline.version):
        raise VideoEditingError("MEDIA_EDL_PARENT_VERSION_REQUIRED")
    return _apply_edl_operations(timeline, edl)


__all__ = [
    "ALLOWED_FORMAT_RATIOS",
    "AudioMix",
    "AudioBus",
    "AudioBusEffect",
    "AudioBusSend",
    "CaptionStyle",
    "FrameCrop",
    "EditDecision",
    "EditDecisionList",
    "MediaAsset",
    "Timeline",
    "TimelineClip",
    "TimelineTranscript",
    "TimelineTrack",
    "TranscriptWord",
    "VideoColorInterpretation",
    "VideoColorProfile",
    "VideoEditingError",
    "apply_edl_suggestion",
    "fingerprint_local_media",
    "media_stream_input_origin",
    "propose_edl",
]
