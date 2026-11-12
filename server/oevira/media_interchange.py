"""Version-bound, portable Media timeline interchange packages.

The module writes interchange files as Media-owned projections of an immutable
Media project version.  Materials remain the only source-byte authority and
Jobs remain the only asynchronous execution authority.
"""
from __future__ import annotations

import base64
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
import uuid
import zipfile
from xml.etree import ElementTree as ET
from urllib.parse import quote

from flask import g, send_file

from .api import ApiError, _body, _idempotency_key, _not_found, _resource_path, _success
from .auth import utc_now
from .collaboration import require_work_access, require_workspace_access, work_commission
from .media import Timeline, VideoEditingError, _fraction_time, _time_json, sha256_media_file


_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_interchanges (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    project_id TEXT NOT NULL REFERENCES media_projects(id),
    project_version INTEGER NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    target_profile TEXT NOT NULL CHECK(target_profile IN ('otio', 'fcpx_xml', 'cmx_3600')),
    adapter_revision TEXT NOT NULL,
    target_settings_json TEXT NOT NULL,
    accepted_loss_item_ids_json TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL CHECK(length(source_snapshot_sha256) = 64),
    loss_report_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'writing', 'completed', 'failed', 'cancelled', 'interrupted')),
    relative_path TEXT,
    staging_path TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    checkpoint_members_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id, project_version) REFERENCES media_project_versions(project_id, version)
);
CREATE INDEX IF NOT EXISTS media_interchanges_project_version
    ON media_interchanges(workspace_id, project_id, project_version, created_at DESC);
"""

_PROFILES = {"otio", "fcpx_xml", "cmx_3600"}
_ADAPTER_REVISIONS = {
    "otio": "oevira.otio-schema-1.0.0",
    "fcpx_xml": "oevira.fcpxml-1.10-1.0.0",
    "cmx_3600": "oevira.cmx-3600-1.0.0",
}
_FORMAT_VERSIONS = {"otio": "Timeline.1/Clip.2/Track.1", "fcpx_xml": "1.10", "cmx_3600": "CMX3600"}
_HOST_PROFILES = {"otio": "unspecified OTIO consumer", "fcpx_xml": "Final Cut Pro XML consumer", "cmx_3600": "CMX3600 EDL consumer"}
_MAX_JSON_NUMBER = (1 << 53) - 1
_CHUNK_BYTES = 1024 * 1024
_FRAME_RATES = {
    Fraction(24, 1), Fraction(25, 1), Fraction(30, 1), Fraction(50, 1), Fraction(60, 1),
    Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001),
}


class _InterchangeStop(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def install_media_interchange_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fraction_document(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _fraction(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, dict) and set(value) == {"num", "den"}:
        if type(value["num"]) is not int or type(value["den"]) is not int or value["den"] <= 0:
            raise ValueError("invalid rational time")
        return Fraction(value["num"], value["den"])
    return _fraction_time(value)


def _ratio(value: object, name: str) -> Fraction:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,8}(?:/[1-9][0-9]{0,8})?", value) is None:
        raise ApiError("INVALID_INPUT", f"{name} must be a positive rational such as 30000/1001", 400)
    parts = value.split("/")
    result = Fraction(int(parts[0]), int(parts[1]) if len(parts) == 2 else 1)
    if result < 1 or result > 120:
        raise ApiError("INVALID_INPUT", f"{name} is outside the supported media clock range", 400)
    return result


def _settings(profile: str, raw: object, timeline: Timeline, state: dict,
              confirmed_spec: dict | None = None) -> tuple[dict, list[str]]:
    if profile not in _PROFILES:
        raise ApiError("MEDIA_INTERCHANGE_PROFILE_UNSUPPORTED", "Choose OTIO, FCPXML or CMX3600 interchange", 400)
    if raw is None:
        raw = {}
    if type(raw) is not dict:
        raise ApiError("INVALID_INPUT", "target_settings must be an object", 400)
    missing: list[str] = []
    if profile == "otio":
        if raw:
            raise ApiError("INVALID_INPUT", "OTIO uses the saved rational project clock and has no target settings", 400)
        return {}, missing

    if profile == "fcpx_xml":
        allowed = {"frame_rate", "width", "height", "audio_rate"}
        if set(raw) - allowed:
            raise ApiError("INVALID_INPUT", "FCPXML target settings contain unsupported fields", 400)
        settings = {}
        derived = dict(confirmed_spec or {})
        if not {"frame_rate", "width", "height"} <= set(derived):
            source_derived = _derive_target_video_format(timeline, state)
            for key, value in source_derived.items():
                derived.setdefault(key, value)
        frame_rate = raw.get("frame_rate", derived.get("frame_rate"))
        width = raw.get("width", derived.get("width"))
        height = raw.get("height", derived.get("height"))
        if frame_rate is None:
            missing.append("frame_rate")
        else:
            settings["frame_rate"] = f"{_ratio(frame_rate, 'target_settings.frame_rate').numerator}/{_ratio(frame_rate, 'target_settings.frame_rate').denominator}"
        if width is None:
            missing.append("width")
        if height is None:
            missing.append("height")
        for name, value in (("width", width), ("height", height)):
            if value is not None:
                if type(value) is not int or not 16 <= value <= 16384:
                    raise ApiError("INVALID_INPUT", f"target_settings.{name} must be an integer from 16 to 16384", 400)
                settings[name] = value
        audio_rates = _audio_rates(timeline)
        audio_rate = raw.get("audio_rate", confirmed_spec.get("audio_rate") if confirmed_spec else
                             next(iter(audio_rates)) if len(audio_rates) == 1 else None)
        if audio_rates and audio_rate is None and len(audio_rates) > 1:
            missing.append("audio_rate")
        if audio_rate is not None:
            if type(audio_rate) is not int or audio_rate not in {22050, 32000, 44100, 48000, 88200, 96000, 192000}:
                raise ApiError("INVALID_INPUT", "target_settings.audio_rate is not a supported audio sample rate", 400)
            settings["audio_rate"] = audio_rate
        return settings, missing

    settings = {}
    allowed = {"frame_rate", "timecode_mode", "source_timecode_origins"}
    if set(raw) - allowed:
        raise ApiError("INVALID_INPUT", "CMX3600 target settings contain unsupported fields", 400)
    derived_rates = _project_video_frame_rates(timeline)
    confirmed_rate = (confirmed_spec or {}).get("frame_rate")
    frame_rate = raw.get("frame_rate", confirmed_rate if confirmed_rate is not None else
                         next(iter(derived_rates)) if len(derived_rates) == 1 else None)
    if frame_rate is None:
        missing.append("frame_rate")
    else:
        rate = _ratio(frame_rate, "target_settings.frame_rate")
        if rate not in _FRAME_RATES:
            raise ApiError("MEDIA_INTERCHANGE_FRAME_RATE_UNSUPPORTED", "CMX3600 supports only standard integer and 1000/1001 video clocks", 422)
        settings["frame_rate"] = f"{rate.numerator}/{rate.denominator}"
    if "timecode_mode" in raw:
        if raw["timecode_mode"] not in {"NDF", "DF"}:
            raise ApiError("INVALID_INPUT", "target_settings.timecode_mode must be NDF or DF", 400)
        settings["timecode_mode"] = raw["timecode_mode"]
    elif frame_rate is not None and _ratio(frame_rate, "target_settings.frame_rate") not in {Fraction(30000, 1001), Fraction(60000, 1001)}:
        settings["timecode_mode"] = "NDF"
    else:
        missing.append("timecode_mode")
    origins = raw.get("source_timecode_origins", {})
    if type(origins) is not dict or any(not isinstance(key, str) or not isinstance(value, str) for key, value in origins.items()):
        raise ApiError("INVALID_INPUT", "source_timecode_origins must map asset IDs to SMPTE timecode", 400)
    known_asset_ids = {asset.asset_id for asset in timeline.assets}
    if set(origins) - known_asset_ids:
        raise ApiError("INVALID_INPUT", "source_timecode_origins contains an asset outside this Media version", 400)
    origins_by_hash = {}
    for asset in timeline.assets:
        origin = origins.get(asset.asset_id)
        if origin is None:
            continue
        previous = origins_by_hash.setdefault(asset.sha256, origin)
        if previous != origin:
            raise ApiError("MEDIA_INTERCHANGE_TIMECODE_ORIGIN_CONFLICT", "Duplicate source bytes cannot use conflicting EDL timecode origins", 409)
    if frame_rate is not None and settings.get("timecode_mode"):
        numerator, denominator = settings["frame_rate"].split("/")
        rate = Fraction(int(numerator), int(denominator))
        for asset_id, timecode in origins.items():
            _parse_timecode(timecode, rate, settings["timecode_mode"])
    settings["source_timecode_origins"] = dict(sorted(origins.items()))
    return settings, missing


def _confirmed_render_spec(store, project_id: str, version: int) -> dict:
    rows = store.rows(
        "SELECT j.payload_json FROM media_renders r JOIN jobs j ON j.id=r.job_id "
        "WHERE r.project_id=? AND r.project_version=? AND r.status='completed' "
        "AND j.kind='media_render' AND j.status='completed' ORDER BY r.created_at DESC, r.id",
        (project_id, version),
    )
    candidates = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            spec = payload.get("resolved_output_spec")
            if (payload.get("purpose") != "render" or not isinstance(spec, dict)
                    or spec.get("output_kind") != "video"):
                continue
            rate = spec.get("frame_rate")
            if isinstance(rate, dict) and type(rate.get("num")) is int and type(rate.get("den")) is int and rate["den"] > 0:
                rate_text = f"{rate['num']}/{rate['den']}"
            elif isinstance(rate, str):
                rate_text = rate
            else:
                continue
            width, height = spec.get("width"), spec.get("height")
            if type(width) is not int or type(height) is not int:
                continue
            candidate = {"frame_rate": rate_text, "width": width, "height": height}
            sample_rate = spec.get("sample_rate")
            if type(sample_rate) is int:
                candidate["audio_rate"] = sample_rate
            candidates.add(_json(candidate))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if len(candidates) != 1:
        return {}
    try:
        return json.loads(next(iter(candidates)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _project_video_frame_rates(timeline: Timeline) -> set[Fraction]:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    rates = set()
    for track in timeline.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = assets.get(clip.media_asset_id)
            if asset is None or asset.is_still_image:
                continue
            streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
            if clip.video_stream_index is not None:
                streams = streams[clip.video_stream_index:clip.video_stream_index + 1]
            elif len(streams) > 1:
                return set()
            if not streams:
                return set()
            rate = _stream_rate(streams[0])
            if rate is None:
                return set()
            rates.add(rate)
    return rates


def _stream_rate(stream: dict) -> Fraction | None:
    for field in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(field)
        if isinstance(raw, dict) and type(raw.get("num")) is int and type(raw.get("den")) is int and raw["num"] > 0 and raw["den"] > 0:
            value = Fraction(raw["num"], raw["den"])
            if 1 <= value <= 120:
                return value
    return None


def _derive_target_video_format(timeline: Timeline, state: dict) -> dict:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    formats = set()
    for track in timeline.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = assets.get(clip.media_asset_id)
            if asset is None or asset.is_still_image:
                continue
            streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
            if clip.video_stream_index is not None:
                streams = streams[clip.video_stream_index:clip.video_stream_index + 1]
            elif len(streams) > 1:
                return {}
            if not streams:
                return {}
            stream = streams[0]
            rate = _stream_rate(stream)
            width, height = stream.get("width"), stream.get("height")
            if rate is None or type(width) is not int or type(height) is not int:
                return {}
            formats.add((rate, width, height))
    if len(formats) == 1:
        rate, width, height = next(iter(formats))
        if Fraction(width, height) == Fraction(*map(int, timeline.format_ratio.split(":"))):
            return {"frame_rate": f"{rate.numerator}/{rate.denominator}", "width": width, "height": height}
    return {}


def _audio_rates(timeline: Timeline) -> set[int]:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    result = set()
    for track in timeline.tracks:
        if track.kind == "video":
            continue
        for clip in track.clips:
            asset = assets.get(clip.media_asset_id)
            if asset is None:
                continue
            streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
            if clip.audio_stream_index is None or clip.audio_stream_index >= len(streams):
                return set()
            sample_rate = streams[clip.audio_stream_index].get("sample_rate")
            if type(sample_rate) is not int:
                return set()
            result.add(sample_rate)
    return result


def _parse_timecode(value: str, rate: Fraction, mode: str) -> int:
    match = re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})[:;]([0-9]{2})", value)
    if match is None:
        raise ApiError("INVALID_INPUT", "A source timecode must use HH:MM:SS:FF", 400)
    hours, minutes, seconds, frames = (int(item) for item in match.groups())
    nominal = int(math.ceil(float(rate)))
    if hours >= 24 or minutes >= 60 or seconds >= 60 or frames >= nominal:
        raise ApiError("INVALID_INPUT", "A source timecode contains an out-of-range field", 400)
    if mode == "DF":
        drop = 2 if rate == Fraction(30000, 1001) else 4 if rate == Fraction(60000, 1001) else 0
        if drop == 0 or (seconds == 0 and minutes % 10 != 0 and frames < drop):
            raise ApiError("MEDIA_INTERCHANGE_TIMECODE_INVALID", "Drop-frame timecode is not valid at this frame rate", 400)
        total_minutes = hours * 60 + minutes
        return ((hours * 3600 + minutes * 60 + seconds) * nominal + frames
                - drop * (total_minutes - total_minutes // 10))
    return (hours * 3600 + minutes * 60 + seconds) * nominal + frames


def _format_timecode(frame: int, rate: Fraction, mode: str) -> str:
    nominal = int(math.ceil(float(rate)))
    if mode == "DF":
        drop = 2 if rate == Fraction(30000, 1001) else 4
        frames_per_24h = nominal * 24 * 60 * 60 - drop * (24 * 60 - 24 * 6)
    else:
        frames_per_24h = nominal * 24 * 60 * 60
    frame %= max(1, frames_per_24h)
    separator = ";" if mode == "DF" else ":"
    if mode == "DF":
        drop = 2 if rate == Fraction(30000, 1001) else 4
        frames_per_10m = nominal * 60 * 10 - drop * 9
        frames_per_minute = nominal * 60 - drop
        ten_minute_blocks, remainder = divmod(frame, frames_per_10m)
        frame += drop * 9 * ten_minute_blocks
        if remainder >= drop:
            frame += drop * ((remainder - drop) // frames_per_minute)
    frames_per_hour = nominal * 60 * 60
    hours, remainder = divmod(frame, frames_per_hour)
    minutes, remainder = divmod(remainder, nominal * 60)
    seconds, frames = divmod(remainder, nominal)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frames:02d}"


def _round_half_up(value: Fraction) -> int:
    if value < 0:
        return -_round_half_up(-value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _otio_time(value: Fraction) -> tuple[dict, bool]:
    numerator, denominator = value.numerator, value.denominator
    exact_numbers = abs(numerator) <= _MAX_JSON_NUMBER and denominator <= _MAX_JSON_NUMBER
    serialized_num = numerator if exact_numbers else float(numerator)
    serialized_den = denominator if exact_numbers else float(denominator)
    result = {
        "OTIO_SCHEMA": "RationalTime.1",
        "value": serialized_num,
        "rate": serialized_den,
    }
    return result, exact_numbers


def _otio_range(start: Fraction, duration: Fraction) -> tuple[dict, bool]:
    start_time, exact_start = _otio_time(start)
    duration_time, exact_duration = _otio_time(duration)
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": start_time,
        "duration": duration_time,
    }, exact_start and exact_duration


def _loss_id(profile: str, object_id: str, property_name: str, disposition: str) -> str:
    digest = _digest_bytes(f"{profile}\0{object_id}\0{property_name}\0{disposition}".encode("utf-8"))[:16]
    return f"loss-{digest}"


def _clip_video_rate(asset, clip) -> Fraction | None:
    streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
    index = clip.video_stream_index if clip.video_stream_index is not None else 0
    if not streams or index >= len(streams):
        return None
    return _stream_rate(streams[index])


def _cmx_transition_map(timeline: Timeline, rate: Fraction) -> dict[tuple[str, str], dict]:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    clips = {clip.clip_id: clip for track in timeline.tracks for clip in track.clips}
    result = {}
    for transition in timeline.transitions:
        if transition.transition_type != "dissolve":
            continue
        for track in timeline.tracks:
            if track.kind != "video":
                continue
            ordered = sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id))
            for left, right in zip(ordered, ordered[1:]):
                if (left.clip_id, right.clip_id) != (transition.from_clip, transition.to_clip):
                    continue
                if _fraction(left.timeline_start) + _fraction(left.duration_sec) != _fraction(right.timeline_start):
                    continue
                frames = _round_half_up(_fraction(transition.duration_sec) * rate)
                if frames <= 0:
                    continue
                before, after = frames // 2, frames - frames // 2
                left_asset, right_asset = assets[left.media_asset_id], assets[right.media_asset_id]
                if (left_asset.is_still_image or right_asset.is_still_image
                        or left_asset.duration_sec is None or right_asset.duration_sec is None):
                    continue
                left_extra = Fraction(after, 1) * _fraction(left.playback_rate) / rate
                right_extra = Fraction(before, 1) * _fraction(right.playback_rate) / rate
                if (_fraction(left.source_out) + left_extra > _fraction(left_asset.duration_sec)
                        or _fraction(right.source_in) - right_extra < 0):
                    continue
                result[(left.clip_id, right.clip_id)] = {
                    "duration_frames": frames, "before_frames": before, "after_frames": after,
                }
    return result


def _make_loss_report(profile: str, timeline: Timeline, settings: dict, project_id: str,
                      version: int, snapshot_hash: str, missing_settings: list[str]) -> dict:
    items = []

    def add(disposition: str, object_type: str, object_id: str, property_name: str,
            source: str, target: str, reason: str) -> None:
        item_id = _loss_id(profile, object_id, property_name, disposition)
        items.append({
            "id": item_id,
            "disposition": disposition,
            "object_type": object_type,
            "object_id": object_id,
            "property": property_name,
            "source": source,
            "target": target,
            "reason": reason,
            "requires_acceptance": disposition != "exact",
        })

    add("exact", "project", project_id, "version",
        f"Media project version {version}", f"Frozen {profile} interchange version {version}",
        "The exchange is bound to the immutable project revision and its source hash manifest.")
    add("exact", "project", project_id, "dependencies",
        f"{len(timeline.assets)} exact Material source(s)", "Every source byte is included under a relative package path",
        "The package carries the original source dependency closure and records each SHA-256.")
    add("exact", "clock", project_id, "rational_seconds",
        "OEVIRA stores source, sample and timeline clocks as rational values",
        "Target-profile times use rational values or report target-boundary quantization",
        "Only the target adapter may quantize a value; its exact source value remains in the native project manifest.")
    if missing_settings:
        for setting in missing_settings:
            add("unsupported", "target_profile", profile, f"required_setting:{setting}",
                "No unique legal value exists in the selected project version",
                f"The exchange is not generated until {setting} is supplied or becomes derivable",
                "The adapter will not invent an edit rate, canvas or timecode base.")

    assets = {asset.asset_id: asset for asset in timeline.assets}
    add("exact", "project", project_id, "aspect_ratio",
        timeline.format_ratio, timeline.format_ratio,
        "The native project aspect ratio is preserved in the exchange package manifest.")
    for track_index, track in enumerate(timeline.tracks):
        add("exact", "track", track.track_id, "identity_and_order",
            f"{track.kind} track {track_index + 1}", f"Track identity retained in {profile} structure and native manifest",
            "Stable Media track identity and order remain available for relinking.")
        if (track.locked or track.muted or track.solo or track.hidden or track.gain != 1
                or track.pan != 0 or track.audio_bus_sends):
            add("unsupported", "track", track.track_id, "controls_and_routing",
                _json({"locked": track.locked, "muted": track.muted, "solo": track.solo,
                       "hidden": track.hidden, "gain": track.gain,
                       "pan": track.pan, "audio_bus_sends": [item.to_dict() for item in track.audio_bus_sends]}),
                "Host edit locks, playback controls and bus routing are not recreated by this adapter",
                "Control data remains in the editable Media project member; the target interchange does not emulate these controls.")
        if (track.take_id is not None or track.take_type is not None or track.recording_session_id is not None
                or track.cue_id is not None or track.speaker_label is not None):
            add("unsupported", "track", track.track_id, "recording_and_speaker_relationship",
                _json({"take_id": track.take_id, "take_type": track.take_type,
                       "recording_session_id": track.recording_session_id,
                       "cue_id": track.cue_id, "speaker_label": track.speaker_label}),
                "The target track has no mapped OEVIRA take, cue or speaker object",
                "Recording/take/source links remain in the native project member.")
        clips = sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id))
        for clip in clips:
            asset = assets[clip.media_asset_id]
            add("exact", "clip", clip.clip_id, "source_material",
                f"Material {asset.source_material_id} with SHA-256 {asset.sha256}",
                "Relative dependency reference with the exact source hash",
                "The exchange clip references a package member, never an old workstation path.")
            add("exact", "clip", clip.clip_id, "source_range",
                _json({"in": _time_json(clip.source_in), "out": _time_json(clip.source_out)}),
                "Source start and end are serialized with the target clock rules",
                "The original source interval is retained in exact rational seconds in the native manifest.")
            add("exact", "clip", clip.clip_id, "timeline_range",
                _json({"start": _time_json(clip.timeline_start), "duration": _time_json(clip.duration_sec)}),
                "Timeline start and duration are serialized with the target clock rules",
                "The original timeline interval is retained in exact rational seconds in the native manifest.")
            if asset.is_still_image and profile == "cmx_3600":
                add("unsupported", "clip", clip.clip_id, "still_image_source",
                    asset.filename, "A still image is omitted from the CMX3600 event list",
                    "This adapter does not synthesize image-sequence media from a still image.")
            if clip.playback_rate != 1:
                if profile == "otio":
                    add("exact", "clip", clip.clip_id, "linear_playback_rate",
                        str(clip.playback_rate), "OpenTimelineIO LinearTimeWarp time_scalar",
                        "Positive constant playback rate is represented by a LinearTimeWarp effect and exact source mapping metadata.")
                elif profile == "cmx_3600":
                    source_rate = _clip_video_rate(asset, clip) if track.kind == "video" else None
                    if source_rate is not None:
                        target_rate = source_rate * _fraction(clip.playback_rate)
                        add("approximate", "clip", clip.clip_id, "linear_playback_rate",
                            str(clip.playback_rate),
                            f"CMX3600 M2 motion event at {target_rate.numerator}/{target_rate.denominator} source frames per second",
                            "M2 carries the constant speed; target-host motion-control interpretation remains unverified and the exact rational rate stays in the native project.")
                    else:
                        add("unsupported", "clip", clip.clip_id, "linear_playback_rate",
                            str(clip.playback_rate), "No reliable source video frame rate exists for a CMX3600 M2 motion record",
                            "The native project preserves the exact speed and source clock; the EDL omits this clip.")
                else:
                    add("unsupported", "clip", clip.clip_id, "linear_playback_rate",
                        str(clip.playback_rate), "FCPXML profile does not emit speed effects",
                        "The clip is represented as a gap in the FCPXML timeline; the full source edit remains in the native member.")
            if clip.display_duration_sec is not None:
                if profile == "cmx_3600":
                    add("unsupported", "clip", clip.clip_id, "still_display_duration",
                        _json(_time_json(clip.display_duration_sec)), "No still event is emitted in the CMX3600 timeline",
                        "The still image and duration remain in the native Media project member.")
                elif profile == "otio":
                    add("approximate", "clip", clip.clip_id, "still_source_clock",
                        "A static image has no intrinsic source duration", "OTIO uses a finite clip duration with a still-image reference",
                        "OTIO can carry the still reference, but an external host may interpret the image duration differently.")
            if clip.audio_stream_index is not None:
                audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
                if len(audio_streams) > 1 or clip.audio_stream_index != 0:
                    add("unsupported", "clip", clip.clip_id, "audio_stream_selection",
                        f"Audio stream index {clip.audio_stream_index}", "The target host may attach its default stream instead",
                        "The selected stream remains explicit only in the native Media project member.")
            if clip.video_stream_index is not None:
                video_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
                if len(video_streams) > 1 or clip.video_stream_index != 0:
                    add("unsupported", "clip", clip.clip_id, "video_stream_selection",
                        f"Video stream index {clip.video_stream_index}", "The target host may attach its default stream instead",
                        "The selected stream remains explicit only in the native Media project member.")
            if clip.audio_mix.volume != 1 or clip.audio_mix.fade_in_sec or clip.audio_mix.fade_out_sec or clip.audio_mix.ducking:
                add("unsupported", "clip", clip.clip_id, "audio_mix",
                    _json(clip.audio_mix.to_dict()), "Per-clip gain, fades and ducking are not executed by this adapter",
                    "The original editable audio processing remains in the native Media project member.")
            if (profile != "cmx_3600" and track.kind == "video" and clip.audio_role == "muted"
                    and any(item.get("codec_type") == "audio" for item in asset.stream_metadata)):
                add("unsupported", "clip", clip.clip_id, "embedded_audio_mute",
                    "The Media video clip is muted while its source Material contains audio",
                    "The target asset reference may expose its embedded audio stream",
                    "No target clip-level audio mute is asserted; the exact mute state remains in the native project.")
            if clip.frame_crop is not None:
                add("unsupported", "clip", clip.clip_id, "frame_crop", _json(clip.frame_crop.to_dict()),
                    "The current adapter does not apply the Media crop", "The crop and exact parameters remain in the native project.")
            if clip.video_placement is not None:
                add("unsupported", "clip", clip.clip_id, "video_placement", _json(clip.video_placement.to_dict()),
                    "The current adapter does not apply the Media transform/opacity", "Placement parameters remain in the native project.")
            for effect in clip.effects:
                add("unsupported", "effect", effect.effect_id, "video_effect",
                    _json(effect.to_dict()), "The target host effect is not mapped or baked by this adapter",
                    "The original effect stack is retained and can be reopened in OEVIRA.")
            if clip.transform_keyframes:
                add("unsupported", "clip", clip.clip_id, "transform_keyframes",
                    _json([item.to_dict() for item in clip.transform_keyframes]), "Animated transforms are not evaluated by this adapter",
                    "The complete keyframe curve remains in the native project.")

    for index, transition in enumerate(timeline.transitions):
        if profile == "otio" and transition.transition_type == "dissolve" and _otio_transition_is_adjacent(timeline, transition):
            disposition, target = "exact", "OTIO Transition.1 SMPTE_Dissolve with exact in/out offsets"
        elif profile == "cmx_3600" and transition.transition_type == "cut":
            disposition, target = "exact", "The adjacent CMX3600 events are a cut with no added transition record"
        elif profile == "cmx_3600" and transition.transition_type == "dissolve":
            try:
                transition_rate = _target_rate(settings)
                eligible = (transition.from_clip, transition.to_clip) in _cmx_transition_map(timeline, transition_rate)
            except ApiError:
                eligible = False
            if eligible:
                window = _cmx_transition_map(timeline, transition_rate)[(transition.from_clip, transition.to_clip)]
                disposition = "approximate"
                target = f"CMX3600 D event with {window['duration_frames']} frames; host alignment may differ from OEVIRA's centered window"
            else:
                disposition, target = "unsupported", "No CMX3600 dissolve is emitted; handles or an exact frame clock are unavailable"
        else:
            disposition = "unsupported"
            target = "No target transition is emitted; the native Media transition is preserved in the project member"
        reason = ("CMX3600 carries an explicit dissolve duration; its host-specific transition alignment remains unverified."
                  if profile == "cmx_3600" and transition.transition_type == "dissolve" and disposition == "approximate"
                  else "The profile emits only the transition forms its target syntax can state; every other style/window remains explicit as a loss.")
        add(disposition, "transition", f"{transition.from_clip}:{transition.to_clip}:{index}", "transition_type_and_window",
            _json(transition.to_dict()), target, reason)

    for overlay in timeline.overlays:
        if profile == "otio":
            add("approximate", "overlay", overlay.overlay_id, "text_and_time_range",
                _json(overlay.to_dict()), "An OTIO marker preserves the literal text and time range, not subtitle rendering or style",
                "The target will not receive a baked caption; the user can inspect the marker while retaining the native project.")
        else:
            add("unsupported", "overlay", overlay.overlay_id, "caption_or_text_overlay",
                _json(overlay.to_dict()), "No target title/caption object is emitted",
                "The exact text and styling remain in the native Media project member.")
    for chapter in timeline.chapters:
        if profile in {"otio", "fcpx_xml"}:
            add("exact", "chapter", chapter.chapter_id, "marker_time_and_title",
                _json(chapter.to_dict()),
                "OTIO Marker.3" if profile == "otio" else "FCPXML chapter-marker element",
                "Chapter title and position are represented by the target's native marker object.")
        elif profile == "cmx_3600":
            add("approximate", "chapter", chapter.chapter_id, "marker_time_and_title",
                _json(chapter.to_dict()), "A CMX3600 locator comment and explicit OEVIRA locator timecode",
                "EDL parsers differ in whether locator comments become native markers; exact chapter metadata also remains in the package.")
        else:
            add("unsupported", "chapter", chapter.chapter_id, "marker_time_and_title",
                _json(chapter.to_dict()), "No chapter marker is emitted by this adapter",
                "The chapter object remains in the native Media project member.")
    for transcript in timeline.transcripts:
        add("unsupported", "transcript", transcript.transcript_id, "words_and_source_clock",
            f"{len(transcript.words)} word(s), {transcript.sample_rate} Hz, source {transcript.source_sha256}",
            "The target timeline does not receive a reviewed transcript/caption track",
            "Word timing, speaker labels and review state remain in the native project member.")
    for bus in timeline.audio_buses:
        add("unsupported", "audio_bus", bus.bus_id, "mix_bus_and_effects", _json(bus.to_dict()),
            "The target adapter does not execute OEVIRA bus routing or processing",
            "Bus definitions and effect parameters remain in the editable project member.")
    for interpretation in timeline.video_color_interpretations:
        add("unsupported", "color_interpretation", interpretation.asset_id, "color_pipeline",
            _json(interpretation.to_dict()), "The target adapter does not apply the OEVIRA color interpretation",
            "Source interpretation and working-space metadata remain in the native project member.")

    if profile == "fcpx_xml" and settings.get("width") and settings.get("height"):
        ratio = Fraction(settings["width"], settings["height"])
        source_ratio = Fraction(*map(int, timeline.format_ratio.split(":")))
        if ratio != source_ratio:
            add("approximate", "project", project_id, "target_canvas_ratio",
                timeline.format_ratio, f"{settings['width']}x{settings['height']} ({ratio})",
                "The selected FCPXML canvas aspect differs from the saved Media project; host framing has not been visually verified.")
    if profile == "fcpx_xml":
        video_tracks = [track for track in timeline.tracks if track.kind == "video" and track.clips]
        primary = sorted(video_tracks[0].clips, key=lambda item: (item.timeline_start, item.clip_id)) if video_tracks else []
        cursor = Fraction(0)
        fully_covered = bool(primary)
        for clip in primary:
            if _fraction(clip.timeline_start) != cursor:
                fully_covered = False
                break
            cursor = _fraction(clip.timeline_start) + _fraction(clip.duration_sec)
        fully_covered = fully_covered and cursor >= _timeline_duration(timeline)
        if not fully_covered:
            add("unsupported", "project", project_id, "background_gap_semantics",
                "Uncovered Media video intervals have transparent/empty source semantics",
                "FCPXML primary gap exposes the host's black background during those intervals",
                "FCPXML has no OEVIRA transparency contract for these intervals; the loss must be accepted explicitly.")
    if profile == "cmx_3600":
        video_tracks = [track for track in timeline.tracks if track.kind == "video" and track.clips]
        if len(video_tracks) > 1:
            for track in video_tracks[1:]:
                add("unsupported", "track", track.track_id, "additional_video_track",
                    f"{len(track.clips)} video clip(s)", "The CMX3600 profile has one video event track",
                    "The additional track remains available in the native project and is omitted from the EDL.")
        if timeline.tracks:
            add("approximate", "project", project_id, "cmx3600_background_gaps",
                "Transparent/empty Media timeline regions", "EDL contains record-time holes that hosts commonly display as black",
                "The EDL does not define OEVIRA's transparent-gap compositing; black fill is not asserted as equivalent.")
        add("unsupported", "project", project_id, "cmx3600_timeline_canvas",
            timeline.format_ratio, "CMX3600 does not carry a raster canvas or pixel aspect setting",
            "Configure the matching frame size/aspect in the target host; this adapter does not invent a canvas.")
        add("approximate", "project", project_id, "cmx3600_host_frame_rate",
            settings.get("frame_rate", "unknown"), "CMX3600 event text does not embed the project frame rate",
            "The EDL carries an OEVIRA rate comment; target host import must use the same rate and remains unverified.")
        origins = settings.get("source_timecode_origins", {})
        used = {clip.media_asset_id for track in timeline.tracks for clip in track.clips}
        for asset_id in sorted(used - set(origins)):
            add("approximate", "source", asset_id, "source_timecode_origin",
                "No embedded source timecode origin is stored in the selected Media version",
                "EDL source time starts at file-relative frame zero for this bundled media member",
                "This is not asserted to match a camera's embedded timecode; the source hash/reel map is included for relinking.")
        audio_tracks = [track for track in timeline.tracks if track.kind != "video" and track.clips]
        for track in audio_tracks[3:]:
            add("unsupported", "track", track.track_id, "audio_track_capacity",
                f"{len(track.clips)} audio clip(s)", "The current CMX adapter assigns only the first three independent audio lanes",
                "Additional audio tracks remain in the native project and are not silently merged.")
        target_rate = (Fraction(*map(int, settings["frame_rate"].split("/")))
                       if settings.get("frame_rate") else None)
        for track in timeline.tracks:
            if track.kind != "video":
                continue
            for clip in track.clips:
                asset = assets[clip.media_asset_id]
                streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
                stream_index = clip.video_stream_index if clip.video_stream_index is not None else 0
                if stream_index >= len(streams) or target_rate is None:
                    continue
                source_rate = _stream_rate(streams[stream_index])
                if source_rate is not None and source_rate != target_rate:
                    add("approximate", "clip", clip.clip_id, "source_video_frame_clock",
                        f"source rate {source_rate.numerator}/{source_rate.denominator}",
                        f"EDL event timecodes use {target_rate.numerator}/{target_rate.denominator}",
                        "Source frames may require host conforming; exact source timestamps remain in the native project and reel map.")
        for track in timeline.tracks:
            if track.kind != "video" or not track.clips:
                continue
            clips = sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id))
            for left, right in zip(clips, clips[1:]):
                if left.timeline_start + left.duration_sec > right.timeline_start:
                    add("unsupported", "clip_pair", f"{left.clip_id}:{right.clip_id}", "overlapping_same_track_clips",
                        f"{left.clip_id} overlaps {right.clip_id}", "Overlapping video events are omitted from the CMX3600 event list",
                        "The current EDL writer does not encode arbitrary same-track overlaps as transitions.")

    items.sort(key=lambda item: (item["object_type"], item["object_id"], item["property"], item["id"]))
    required = [item["id"] for item in items if item["requires_acceptance"]]
    return {
        "schema": "oevira.media.interchange.loss-report.v1",
        "project_id": project_id,
        "project_version": version,
        "source_snapshot_sha256": snapshot_hash,
        "target_profile": profile,
        "target_format_version": _FORMAT_VERSIONS[profile],
        "adapter_revision": _ADAPTER_REVISIONS[profile],
        "target_settings": settings,
        "missing_target_settings": missing_settings,
        "loss_items": items,
        "required_accepted_loss_item_ids": required,
        "target_host_verification": "NOT_VERIFIED",
        "target_host": {"profile": _HOST_PROFILES[profile], "version": "NOT_VERIFIED"},
        "native_project_preserved": True,
        "bake_required_items": [],
    }


def _add_storyboard_package_losses(report: dict, state: dict) -> None:
    storyboard = state.get("storyboard", {"nodes": []}) if isinstance(state, dict) else {"nodes": []}
    nodes = storyboard.get("nodes", []) if isinstance(storyboard, dict) else []
    if not nodes:
        return
    profile = report["target_profile"]
    for node in nodes:
        object_id = node["beat_id"]
        item_id = _loss_id(profile, object_id, "storyboard_node", "approximate")
        report["loss_items"].append({
            "id": item_id,
            "disposition": "approximate",
            "object_type": "storyboard_node",
            "object_id": object_id,
            "property": "editable_story_and_relationships",
            "source": _json({"kind": node["kind"], "title": node["title"],
                              "clip_refs": [item["clip_id"] for item in node["clip_refs"]],
                              "recording_refs": [item["session_id"] for item in node["recording_refs"]]}),
            "target": "A companion media/storyboard.json preserves the exact native node and links; third-party host editing objects are not created from this sidecar",
            "reason": "The full Media project and storyboard sidecar remain editable in OEVIRA, while the target host will not project these script/beat records into its timeline automatically.",
            "requires_acceptance": True,
        })
    report["loss_items"].sort(key=lambda item: (item["object_type"], item["object_id"], item["property"], item["id"]))
    report["required_accepted_loss_item_ids"] = [item["id"] for item in report["loss_items"]
                                                  if item["requires_acceptance"]]


def _otio_transition_is_adjacent(timeline: Timeline, transition) -> bool:
    for track in timeline.tracks:
        if track.kind != "video":
            continue
        ordered = sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id))
        if any(_fraction(left.timeline_start) + _fraction(left.duration_sec) > _fraction(right.timeline_start)
               for left, right in zip(ordered, ordered[1:])):
            continue
        for left, right in zip(ordered, ordered[1:]):
            if (left.clip_id == transition.from_clip and right.clip_id == transition.to_clip
                    and left.timeline_start + left.duration_sec == right.timeline_start):
                return True
    return False


def _get_project_snapshot(store, identity: dict, project_id: str, version: object,
                          profile: str, target_settings: object, *, require_current: bool) -> tuple[dict, object, dict, Timeline, dict, list[str]]:
    from . import media_api as media

    workspace_id = identity["workspace"]["id"]
    project = media._project_row(store, workspace_id, project_id, identity=identity, write=False)
    selected_version = project["current_version"] if version is None else version
    if type(selected_version) is not int or selected_version < 1:
        raise ApiError("INVALID_INPUT", "version must be a positive saved Media version", 400)
    with store.transaction(immediate=False) as connection:
        current_project = connection.execute(
            "SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (project_id, workspace_id),
        ).fetchone()
        version_row = connection.execute(
            "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
            (project_id, workspace_id, selected_version),
        ).fetchone()
        if current_project is None or version_row is None:
            raise _not_found()
        if require_current and current_project["current_version"] != selected_version:
            raise ApiError("MEDIA_INTERCHANGE_PROJECT_CHANGED", "The selected Media project version is no longer current; preview the current version again", 409)
        if current_project["work_id"]:
            require_work_access(connection, identity, current_project["work_id"], write=False)
        else:
            require_workspace_access(connection, identity, write=False)
        state = media._require_media_project_version_sources(
            connection, identity, current_project, version_row, target_work_id=current_project["work_id"],
        )
    try:
        timeline, availability = media._load_timeline(
            store, workspace_id, state, verify_files=True, project_id=project_id, identity=identity,
        )
    except (ApiError, VideoEditingError):
        raise
    confirmed_spec = _confirmed_render_spec(store, project_id, selected_version)
    normalized_settings, missing = _settings(profile, target_settings, timeline, state, confirmed_spec)
    source_hashes = [
        {"asset_id": asset.asset_id, "material_id": asset.source_material_id, "sha256": asset.sha256}
        for asset in sorted(timeline.assets, key=lambda item: item.asset_id)
    ]
    snapshot = {
        "project_id": project_id,
        "project_version": selected_version,
        "state_sha256": _digest_bytes(_json(state).encode("utf-8")),
        "source_hashes": source_hashes,
        "target_profile": profile,
        "adapter_revision": _ADAPTER_REVISIONS[profile],
        "target_settings": normalized_settings,
    }
    snapshot_hash = _digest_bytes(_json(snapshot).encode("utf-8"))
    report = _make_loss_report(profile, timeline, normalized_settings, project_id, selected_version,
                               snapshot_hash, missing)
    _add_storyboard_package_losses(report, state)
    _add_quantization_losses(report, timeline, profile, normalized_settings)
    return dict(current_project), version_row, state, timeline, report, missing


def _project_snapshot_hash(project, version_row, state: dict, timeline: Timeline, profile: str,
                           settings: dict) -> str:
    source_hashes = [
        {"asset_id": asset.asset_id, "material_id": asset.source_material_id, "sha256": asset.sha256}
        for asset in sorted(timeline.assets, key=lambda item: item.asset_id)
    ]
    snapshot = {
        "project_id": project["id"],
        "project_version": version_row["version"],
        "state_sha256": _digest_bytes(_json(state).encode("utf-8")),
        "source_hashes": source_hashes,
        "target_profile": profile,
        "adapter_revision": _ADAPTER_REVISIONS[profile],
        "target_settings": settings,
    }
    return _digest_bytes(_json(snapshot).encode("utf-8"))


def _preflight_response(project, version_row, state, timeline, report) -> dict:
    required = report["required_accepted_loss_item_ids"]
    missing = report["missing_target_settings"]
    return {
        "state": "requires_target_settings" if missing else "requires_loss_acceptance" if required else "ready",
        "project_id": project["id"],
        "project_version": version_row["version"],
        "target_profile": report["target_profile"],
        "target_format_version": report["target_format_version"],
        "adapter_revision": report["adapter_revision"],
        "target_settings": report["target_settings"],
        "source_snapshot_sha256": report["source_snapshot_sha256"],
        "source_count": len(timeline.assets),
        "loss_items": report["loss_items"],
        "required_accepted_loss_item_ids": required,
        "missing_target_settings": missing,
        "target_host_verification": "NOT_VERIFIED",
        "target_host": report["target_host"],
    }


def register_media_interchange_routes(app, store) -> None:
    from .worker import install_worker_schema

    install_media_interchange_schema(store)
    install_worker_schema(store)
    app.extensions.setdefault("oevira.job_resume_validators", {})["media_interchange"] = validate_media_interchange_resume
    app.extensions.setdefault("oevira.job_cancelled_retry_kinds", set()).add("media_interchange")

    @app.post("/api/media/projects/<project_id>/interchanges/preflight")
    def media_interchange_preflight(project_id: str):
        identity = g.identity
        data = _body()
        if set(data) - {"version", "target_profile", "target_settings"} or "target_profile" not in data:
            raise ApiError("INVALID_INPUT", "Preflight requires a target profile and optional saved project version/settings", 400)
        profile = data["target_profile"]
        if profile not in _PROFILES:
            raise ApiError("MEDIA_INTERCHANGE_PROFILE_UNSUPPORTED", "Choose OTIO, FCPXML or CMX3600 interchange", 400)
        version = data.get("version")
        project, version_row, state, timeline, report, _ = _get_project_snapshot(
            store, identity, project_id, version, profile, data.get("target_settings"), require_current=False,
        )
        report["source_snapshot_sha256"] = _project_snapshot_hash(
            project, version_row, state, timeline, profile, report["target_settings"],
        )
        report["project_id"] = project_id
        report["project_version"] = version_row["version"]
        return _success(_preflight_response(project, version_row, state, timeline, report))

    @app.post("/api/media/projects/<project_id>/interchanges")
    def media_create_interchange(project_id: str):
        identity = g.identity
        if identity.get("role") not in {"owner", "editor"}:
            raise ApiError("FORBIDDEN", "Your workspace role cannot create a Media exchange", 403)
        data = _body()
        allowed = {"version", "target_profile", "target_settings", "source_snapshot_sha256", "accepted_loss_item_ids"}
        required = {"version", "target_profile", "source_snapshot_sha256", "accepted_loss_item_ids"}
        if set(data) - allowed or not required <= set(data):
            raise ApiError("INVALID_INPUT", "Create an exchange from a current loss-preflight receipt", 400)
        profile = data["target_profile"]
        if profile not in _PROFILES:
            raise ApiError("MEDIA_INTERCHANGE_PROFILE_UNSUPPORTED", "Choose OTIO, FCPXML or CMX3600 interchange", 400)
        version = data["version"]
        project, version_row, state, timeline, report, missing = _get_project_snapshot(
            store, identity, project_id, version, profile, data.get("target_settings"), require_current=True,
        )
        snapshot_hash = _project_snapshot_hash(project, version_row, state, timeline, profile, report["target_settings"])
        if data["source_snapshot_sha256"] != snapshot_hash:
            raise ApiError("MEDIA_INTERCHANGE_PLAN_STALE", "The saved project, sources or adapter plan changed after preflight", 409,
                           {"current_source_snapshot_sha256": snapshot_hash})
        if missing:
            raise ApiError("MEDIA_INTERCHANGE_TARGET_SETTINGS_REQUIRED", "Choose the missing target format settings before creating the exchange", 409,
                           {"missing_target_settings": missing})
        accepted = data["accepted_loss_item_ids"]
        if (not isinstance(accepted, list) or any(not isinstance(item, str) for item in accepted)
                or len(accepted) != len(set(accepted))):
            raise ApiError("INVALID_INPUT", "accepted_loss_item_ids must be a unique list of preflight item IDs", 400)
        required_loss = set(report["required_accepted_loss_item_ids"])
        if set(accepted) != required_loss:
            unknown = sorted(set(accepted) - {item["id"] for item in report["loss_items"]})
            unaccepted = sorted(required_loss - set(accepted))
            if unknown:
                raise ApiError("MEDIA_INTERCHANGE_LOSS_ID_UNKNOWN", "One or more accepted losses are not in the current preflight", 409,
                               {"unknown_loss_item_ids": unknown})
            raise ApiError("MEDIA_INTERCHANGE_LOSS_ACCEPTANCE_REQUIRED", "Accept every listed compatibility loss or revise the project/profile", 409,
                           {"required_accepted_loss_item_ids": sorted(required_loss), "unaccepted_loss_item_ids": unaccepted})
        if report["bake_required_items"]:
            raise ApiError("MEDIA_INTERCHANGE_BAKE_REQUIRED", "A selected exchange item needs a real rendered bake before export", 409,
                           {"bake_required_items": report["bake_required_items"]})
        key = _idempotency_key()
        if key is None:
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for a Media exchange job", 400)
        source_snapshot = report["source_snapshot_sha256"] = snapshot_hash
        report["project_id"] = project_id
        report["project_version"] = version
        report["accepted_loss_item_ids"] = sorted(accepted)
        payload = {
            "interchange_id": str(uuid.uuid4()),
            "project_id": project_id,
            "version": version,
            "target_profile": profile,
            "adapter_revision": _ADAPTER_REVISIONS[profile],
            "target_settings": report["target_settings"],
            "source_snapshot_sha256": source_snapshot,
            "accepted_loss_item_ids": sorted(accepted),
        }
        fingerprint = _digest_bytes(_json(payload | {"interchange_id": None}).encode("utf-8"))
        workspace_id = identity["workspace"]["id"]
        job_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (project_id, workspace_id),
            ).fetchone()
            version_now = connection.execute(
                "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                (project_id, workspace_id, version),
            ).fetchone()
            if current is None or version_now is None:
                raise _not_found()
            if current["current_version"] != version:
                raise ApiError("MEDIA_INTERCHANGE_PROJECT_CHANGED", "The project changed after preflight; preview the current version again", 409)
            if current["work_id"]:
                require_work_access(connection, identity, current["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            stored_state = _get_version_state(version_now)
            from . import media_api as media
            media._require_media_project_version_sources(
                connection, identity, current, version_now, target_work_id=current["work_id"],
            )
            prior = connection.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND kind='media_interchange' AND idempotency_key=?",
                (workspace_id, key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for another Media exchange", 409)
                interchange = connection.execute("SELECT * FROM media_interchanges WHERE job_id=?", (prior["id"],)).fetchone()
                if interchange is None:
                    raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The original Media exchange record is unavailable", 409)
                return _success(_interchange_view(dict(interchange), dict(prior)), 200)
            if _digest_bytes(_json(stored_state).encode("utf-8")) != _digest_bytes(_json(state).encode("utf-8")):
                raise ApiError("MEDIA_INTERCHANGE_PLAN_STALE", "The saved project state changed after preflight", 409)
            payload["interchange_id"] = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'media_interchange', 'queued', ?, ?, ?, ?, 'Preparing portable Media exchange package', ?, ?)",
                (job_id, workspace_id, identity["user"]["id"], current["work_id"], _json(payload), fingerprint, key,
                 max(1, len(timeline.assets) + 3), now, now),
            )
            connection.execute(
                "INSERT INTO media_interchanges(id,workspace_id,project_id,project_version,job_id,target_profile,adapter_revision,target_settings_json,accepted_loss_item_ids_json,source_snapshot_sha256,loss_report_json,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
                (payload["interchange_id"], workspace_id, project_id, version, job_id, profile,
                 _ADAPTER_REVISIONS[profile], _json(report["target_settings"]), _json(sorted(accepted)),
                 snapshot_hash, _json(report), now, now),
            )
            row = connection.execute("SELECT * FROM media_interchanges WHERE id=?", (payload["interchange_id"],)).fetchone()
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _success(_interchange_view(dict(row), dict(job)), 202)

    @app.get("/api/media/interchanges/<interchange_id>")
    def media_get_interchange(interchange_id: str):
        identity = g.identity
        with store.transaction(immediate=False) as connection:
            row = connection.execute(
                "SELECT * FROM media_interchanges WHERE id=? AND workspace_id=?",
                (interchange_id, identity["workspace"]["id"]),
            ).fetchone()
            if row is None:
                raise _not_found()
            _require_interchange_scope(connection, identity, row, target_work_id=None)
            job = connection.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (row["job_id"], row["workspace_id"])).fetchone()
            if job is None:
                raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The Media exchange job is unavailable", 409)
            return _success(_interchange_view(dict(row), dict(job)))

    @app.get("/api/media/interchanges/<interchange_id>/file")
    def media_download_interchange(interchange_id: str):
        resolved = resolve_media_interchange_file(store, g.identity, interchange_id, target_work_id=None)
        response = send_file(resolved["path"], as_attachment=True, download_name=resolved["filename"],
                             mimetype=resolved["mime_type"], conditional=False)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["ETag"] = f'"{resolved["sha256"]}"'
        return response


def _get_version_state(version_row) -> dict:
    try:
        value = json.loads(version_row["state_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version cannot be read", 409) from exc
    if not isinstance(value, dict):
        raise ApiError("MEDIA_PROJECT_VERSION_INVALID", "The saved Media version cannot be read", 409)
    return value


def _interchange_view(row: dict, job: dict) -> dict:
    report = json.loads(row["loss_report_json"])
    return {
        "id": row["id"],
        "kind": "media_interchange",
        "project_id": row["project_id"],
        "project_version": row["project_version"],
        "target_profile": row["target_profile"],
        "adapter_revision": row["adapter_revision"],
        "status": row["status"],
        "job": {"id": job["id"], "status": job["status"], "progress_completed": job["progress_completed"],
                "progress_total": job["progress_total"], "progress_label": job["progress_label"],
                "control_revision": job["control_revision"], "pause_requested": bool(job["pause_requested"])},
        "loss_report": report,
        "file": ({"sha256": row["sha256"], "size_bytes": row["size_bytes"],
                  "download_url": f"/api/media/interchanges/{row['id']}/file"}
                 if row["status"] == "completed" and row["sha256"] else None),
        "error": ({"code": row["error_code"], "message": row["error_message"]}
                  if row["error_code"] else None),
    }


def _loss_entry(report: dict, object_id: str, property_name: str) -> dict | None:
    return next((item for item in report["loss_items"]
                 if item["object_id"] == object_id and item["property"] == property_name), None)


def _reel_name(asset) -> str:
    # CMX3600 reel labels are deliberately distinct from truncating user names.
    return base64.b32encode(bytes.fromhex(asset.sha256)).decode("ascii")[:8]


def _safe_leaf(filename: str) -> str:
    leaf = Path(str(filename or "source.bin").replace("\\", "/")).name
    leaf = "".join(char for char in leaf if char.isascii() and (char.isalnum() or char in "._-"))
    leaf = leaf.strip("._-")[:96]
    return leaf or "source.bin"


def _otio_clip(clip, asset, relative_path: str, track_index: int) -> tuple[dict, list[dict]]:
    source_start = _fraction(clip.source_in)
    timeline_duration = _fraction(clip.duration_sec)
    time_range, exact_range = _otio_range(source_start, timeline_duration)
    available_range = None
    if asset.duration_sec is not None:
        available_range, _ = _otio_range(Fraction(0), _fraction(asset.duration_sec))
    media_reference = {
        "OTIO_SCHEMA": "ExternalReference.1",
        "available_range": available_range,
        "metadata": {"oevira": {
            "asset_id": asset.asset_id,
            "material_id": asset.source_material_id,
            "sha256": asset.sha256,
            "source_streams": list(asset.stream_metadata),
            "source_clock_origin": asset.format_metadata.get("start_time"),
            "relative_dependency_path": relative_path,
        }},
        "name": asset.filename,
        "target_url": relative_path,
    }
    metadata = {"oevira": {
        "clip_id": clip.clip_id,
        "track_id": clip.track_id,
        "track_order": track_index,
        "timeline_start": _time_json(clip.timeline_start),
        "source_in": _time_json(clip.source_in),
        "source_out": _time_json(clip.source_out),
        "timeline_duration": _time_json(clip.duration_sec),
        "source_material_id": asset.source_material_id,
        "source_sha256": asset.sha256,
        "audio_stream_index": clip.audio_stream_index,
        "video_stream_index": clip.video_stream_index,
        "audio_role": clip.audio_role,
        "purpose": clip.purpose,
        "native_clip": clip.to_dict(),
    }}
    effects = []
    exact = [exact_range]
    if clip.playback_rate != 1:
        rate = _fraction(clip.playback_rate)
        scalar = float(rate)
        is_exact_scalar = Fraction(str(scalar)) == rate
        exact.append(is_exact_scalar)
        effects.append({
            "OTIO_SCHEMA": "LinearTimeWarp.1",
            "effect_name": "LinearTimeWarp",
            "enabled": True,
            "metadata": {"oevira": {"exact_time_scalar": _fraction_document(rate),
                                     "source_in": _time_json(clip.source_in),
                                     "source_out": _time_json(clip.source_out)}},
            "name": "OEVIRA linear playback rate",
            "time_scalar": scalar,
        })
    document = {
        "OTIO_SCHEMA": "Clip.2",
        "active_media_reference_key": "DEFAULT_MEDIA",
        "effects": effects,
        "markers": [],
        "enabled": True,
        "media_references": {"DEFAULT_MEDIA": media_reference},
        "metadata": metadata,
        "name": clip.clip_id,
        "source_range": time_range,
    }
    return document, exact


def _otio_gap(duration: Fraction, name: str, metadata: dict | None = None) -> dict:
    source_range, _ = _otio_range(Fraction(0), duration)
    return {
        "OTIO_SCHEMA": "Gap.1",
        "effects": [],
        "markers": [],
        "enabled": True,
        "metadata": metadata or {},
        "name": name,
        "source_range": source_range,
    }


def _otio_transition(transition) -> dict:
    duration = _fraction(transition.duration_sec)
    incoming = duration / 2
    outgoing = duration - incoming
    return {
        "OTIO_SCHEMA": "Transition.1",
        "enabled": True,
        "metadata": {"oevira": transition.to_dict()},
        "name": f"{transition.transition_type} {transition.from_clip} to {transition.to_clip}",
        "transition_type": "SMPTE_Dissolve",
        "parameters": {},
        "in_offset": _otio_time(incoming)[0],
        "out_offset": _otio_time(outgoing)[0],
    }


def _otio_markers(timeline: Timeline) -> list[dict]:
    markers = []
    for chapter in timeline.chapters:
        start = _fraction(chapter.start_sec)
        marked_range, _ = _otio_range(start, Fraction(0))
        markers.append({
            "OTIO_SCHEMA": "Marker.3", "color": None, "comment": "Media chapter",
            "marked_range": marked_range,
            "metadata": {"oevira": {"chapter_id": chapter.chapter_id}},
            "name": chapter.title,
        })
    for overlay in timeline.overlays:
        marked_range, _ = _otio_range(_fraction(overlay.start_sec), _fraction(overlay.end_sec) - _fraction(overlay.start_sec))
        markers.append({
            "OTIO_SCHEMA": "Marker.3", "color": None,
            "comment": f"{overlay.kind}: {overlay.text}",
            "marked_range": marked_range,
            "metadata": {"oevira": {"overlay_id": overlay.overlay_id,
                                     "native_overlay": overlay.to_dict(),
                                     "representation": "marker_only_not_subtitle_render"}},
            "name": overlay.text[:200],
        })
    return markers


def _timeline_duration(timeline: Timeline) -> Fraction:
    ends = [(_fraction(clip.timeline_start) + _fraction(clip.duration_sec))
            for track in timeline.tracks for clip in track.clips]
    return max(ends, default=Fraction(0))


def _otio_track(track, assets: dict, paths: dict[str, str], track_index: int,
                transitions: dict[tuple[str, str], object], end: Fraction,
                loss_report: dict) -> tuple[dict, list[bool]]:
    clips = sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id))
    overlap = any(_fraction(left.timeline_start) + _fraction(left.duration_sec) > _fraction(right.timeline_start)
                  for left, right in zip(clips, clips[1:]))
    exact = []

    def one_clip_track(clip, index: int) -> dict:
        begin = _fraction(clip.timeline_start)
        clip_doc, clip_exact = _otio_clip(clip, assets[clip.media_asset_id], paths[clip.media_asset_id], track_index)
        exact.extend(clip_exact)
        children = []
        if begin > 0:
            children.append(_otio_gap(begin, "Leading transparent gap"))
        children.append(clip_doc)
        clip_end = begin + _fraction(clip.duration_sec)
        if end > clip_end:
            children.append(_otio_gap(end - clip_end, "Trailing transparent gap"))
        return {
            "OTIO_SCHEMA": "Track.1", "children": children, "effects": [], "enabled": True,
            "kind": "Video" if track.kind == "video" else "Audio", "markers": [],
            "metadata": {"oevira": {"media_track_id": track.track_id,
                                     "media_track_order": track_index,
                                     "clip_id": clip.clip_id, "overlap_lane": index}},
            "name": f"{track.track_id} · {clip.clip_id}", "source_range": None,
        }

    if overlap:
        children = [one_clip_track(clip, index) for index, clip in enumerate(clips)]
        track_doc = {
            "OTIO_SCHEMA": "Stack.1", "children": children, "effects": [], "enabled": True,
            "markers": [], "metadata": {"oevira": {"track_id": track.track_id,
                                                     "track_order": track_index,
                                                     "layout": track.layout,
                                                     "overlapping_native_clips": True}},
            "name": track.track_id, "source_range": None,
        }
        return track_doc, exact

    children = []
    cursor = Fraction(0)
    for index, clip in enumerate(clips):
        begin = _fraction(clip.timeline_start)
        if begin > cursor:
            children.append(_otio_gap(begin - cursor, "Transparent timeline gap"))
        clip_doc, clip_exact = _otio_clip(clip, assets[clip.media_asset_id], paths[clip.media_asset_id], track_index)
        exact.extend(clip_exact)
        children.append(clip_doc)
        transition = transitions.get((clip.clip_id, clips[index + 1].clip_id)) if index + 1 < len(clips) else None
        if transition is not None:
            if begin + _fraction(clip.duration_sec) == _fraction(clips[index + 1].timeline_start):
                children.append(_otio_transition(transition))
            else:
                # A transition outside an exact contiguous boundary is not represented.
                existing = _loss_entry(loss_report, f"{transition.from_clip}:{transition.to_clip}", "transition_type_and_window")
                if existing is not None:
                    existing["disposition"] = "unsupported"
        cursor = max(cursor, begin + _fraction(clip.duration_sec))
    if end > cursor:
        children.append(_otio_gap(end - cursor, "Trailing transparent timeline gap"))
    track_doc = {
        "OTIO_SCHEMA": "Track.1", "children": children, "effects": [], "enabled": True,
        "kind": "Video" if track.kind == "video" else "Audio", "markers": [],
        "metadata": {"oevira": {"track_id": track.track_id, "track_order": track_index,
                                 "layout": track.layout,
                                 "track_controls": {"locked": track.locked, "muted": track.muted,
                                                    "solo": track.solo, "hidden": track.hidden,
                                                    "gain": track.gain, "pan": track.pan}}},
        "name": track.track_id, "source_range": None,
    }
    return track_doc, exact


def _build_otio(timeline: Timeline, title: str, project_id: str, version: int,
                paths: dict[str, str], loss_report: dict) -> bytes:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    transition_map = {(item.from_clip, item.to_clip): item for item in timeline.transitions
                      if item.transition_type == "dissolve" and _otio_transition_is_adjacent(timeline, item)}
    end = _timeline_duration(timeline)
    track_docs = []
    for index, track in enumerate(timeline.tracks):
        doc, exact_values = _otio_track(track, assets, paths, index, transition_map, end, loss_report)
        track_docs.append(doc)
        for clip in track.clips:
            source_range, exact_source = _otio_range(_fraction(clip.source_in),
                                                     _fraction(clip.duration_sec))
            timeline_range, exact_timeline = _otio_range(_fraction(clip.timeline_start),
                                                         _fraction(clip.duration_sec))
            del source_range, timeline_range
            if not exact_source:
                item = _loss_entry(loss_report, clip.clip_id, "source_range")
                if item is not None:
                    item["disposition"] = "approximate"
                    item["target"] = "OTIO RationalTime uses a JSON numeric rate/value that exceeds exact binary64 integer precision"
                    item["reason"] = "The exact source interval remains in native_clip metadata and media/native-project.json."
            if not exact_timeline:
                item = _loss_entry(loss_report, clip.clip_id, "timeline_range")
                if item is not None:
                    item["disposition"] = "approximate"
                    item["target"] = "OTIO RationalTime uses a JSON numeric rate/value that exceeds exact binary64 integer precision"
                    item["reason"] = "The exact timeline interval remains in native_clip metadata and media/native-project.json."
        if not all(exact_values):
            for clip in track.clips:
                if clip.playback_rate != 1:
                    item = _loss_entry(loss_report, clip.clip_id, "linear_playback_rate")
                    if item is not None:
                        item["disposition"] = "approximate"
                        item["target"] = "OTIO LinearTimeWarp stores the positive speed as a JSON float"
                        item["reason"] = "The exact rational speed remains in metadata, while a host may round the serialized scalar."
    loss_report["loss_items"].sort(key=lambda item: (item["object_type"], item["object_id"], item["property"], item["id"]))
    loss_report["required_accepted_loss_item_ids"] = [item["id"] for item in loss_report["loss_items"]
                                                      if item["requires_acceptance"]]
    tracks = {
        "OTIO_SCHEMA": "Stack.1", "children": track_docs, "effects": [], "enabled": True,
        "markers": _otio_markers(timeline),
        "metadata": {"oevira": {"project_id": project_id, "project_version": version,
                                 "format_ratio": timeline.format_ratio,
                                 "transitions": [item.to_dict() for item in timeline.transitions],
                                 "transcripts": [item.to_dict() for item in timeline.transcripts],
                                 "audio_buses": [item.to_dict() for item in timeline.audio_buses],
                                 "video_color_interpretations": [item.to_dict() for item in timeline.video_color_interpretations],
                                 "native_semantics_require_oevira": True}},
        "name": "tracks", "source_range": None,
    }
    document = {
        "OTIO_SCHEMA": "Timeline.1",
        "global_start_time": _otio_time(Fraction(0))[0],
        "metadata": {"oevira": {"project_id": project_id, "project_version": version,
                                 "target_format_version": _FORMAT_VERSIONS["otio"],
                                 "adapter_revision": _ADAPTER_REVISIONS["otio"],
                                 "target_host_verification": "NOT_VERIFIED"}},
        "name": title,
        "tracks": tracks,
    }
    return (_json(document) + "\n").encode("utf-8")


def _target_rate(settings: dict) -> Fraction:
    raw = settings.get("frame_rate")
    if not isinstance(raw, str):
        raise ApiError("MEDIA_INTERCHANGE_TARGET_SETTINGS_REQUIRED", "A target frame rate is required", 409)
    numerator, denominator = raw.split("/")
    return Fraction(int(numerator), int(denominator))


def _seconds(value: Fraction) -> str:
    value = Fraction(value)
    return f"{value.numerator}s" if value.denominator == 1 else f"{value.numerator}/{value.denominator}s"


def _quantize_frame(value: Fraction, rate: Fraction) -> tuple[Fraction, Fraction]:
    frames = _round_half_up(value * rate)
    actual = Fraction(frames, 1) / rate
    return actual, actual - value


def _add_quantization_losses(report: dict, timeline: Timeline, profile: str, settings: dict) -> None:
    if profile not in {"fcpx_xml", "cmx_3600"} or "frame_rate" not in settings:
        return
    rate = _target_rate(settings)
    for track in timeline.tracks:
        for clip in track.clips:
            if profile == "cmx_3600":
                for property_name, raw_value in (("source_in", _fraction(clip.source_in)),
                                                 ("source_out", _fraction(clip.source_out))):
                    _, error = _quantize_frame(raw_value, rate)
                    if error:
                        quantized, _ = _quantize_frame(raw_value, rate)
                        item_id = _loss_id(profile, clip.clip_id, property_name, "approximate")
                        report["loss_items"].append({
                            "id": item_id, "disposition": "approximate", "object_type": "clip",
                            "object_id": clip.clip_id, "property": property_name,
                            "source": _json(_fraction_document(raw_value)),
                            "target": _json({"value": _fraction_document(quantized),
                                             "frame_rate": settings["frame_rate"],
                                             "error_seconds": _fraction_document(error)}),
                            "reason": "CMX3600 source in/out values are frame-based; the exact source clock remains in the native project.",
                            "requires_acceptance": True,
                        })
            for property_name, raw_value in (
                ("timeline_start", _fraction(clip.timeline_start)),
                ("timeline_duration", _fraction(clip.duration_sec)),
            ):
                _, error = _quantize_frame(raw_value, rate)
                if error:
                    item_id = _loss_id(profile, clip.clip_id, property_name, "approximate")
                    report["loss_items"].append({
                        "id": item_id,
                        "disposition": "approximate",
                        "object_type": "clip",
                        "object_id": clip.clip_id,
                        "property": property_name,
                        "source": _json(_fraction_document(raw_value)),
                        "target": _json({"value": _fraction_document(_quantize_frame(raw_value, rate)[0]),
                                         "frame_rate": settings["frame_rate"],
                                         "error_seconds": _fraction_document(error)}),
                        "reason": "The target interchange timeline clock is frame-based; the exact rational value remains in the native project and the quantization error is reported.",
                        "requires_acceptance": True,
                    })
    if profile in {"fcpx_xml", "cmx_3600"}:
        for chapter in timeline.chapters:
            raw_value = _fraction(chapter.start_sec)
            _, error = _quantize_frame(raw_value, rate)
            if error:
                item_id = _loss_id(profile, chapter.chapter_id, "marker_time", "approximate")
                report["loss_items"].append({
                    "id": item_id, "disposition": "approximate", "object_type": "chapter",
                    "object_id": chapter.chapter_id, "property": "marker_time",
                    "source": _json(_fraction_document(raw_value)),
                    "target": _json({"value": _fraction_document(_quantize_frame(raw_value, rate)[0]),
                                     "frame_rate": settings["frame_rate"],
                                     "error_seconds": _fraction_document(error)}),
                    "reason": "The target marker uses the sequence frame grid; the exact chapter time remains in the native project.",
                    "requires_acceptance": True,
                })
    report["loss_items"].sort(key=lambda item: (item["object_type"], item["object_id"], item["property"], item["id"]))
    report["required_accepted_loss_item_ids"] = [item["id"] for item in report["loss_items"]
                                                  if item["requires_acceptance"]]


def _build_fcpxml(timeline: Timeline, title: str, project_id: str, version: int,
                  paths: dict[str, str], settings: dict, loss_report: dict) -> bytes:
    rate = _target_rate(settings)
    width, height = settings["width"], settings["height"]
    duration, _ = _quantize_frame(_timeline_duration(timeline), rate)
    if duration <= 0:
        raise ApiError("MEDIA_INTERCHANGE_EMPTY_TIMELINE", "An empty Media timeline cannot be exchanged as FCPXML", 409)
    root = ET.Element("fcpxml", {"version": "1.10"})
    resources = ET.SubElement(root, "resources")
    target_format_id = "r1"
    format_ids = {}
    ET.SubElement(resources, "format", {
        "id": target_format_id,
        "name": f"OEVIRA {width}x{height} {rate.numerator}/{rate.denominator}",
        "frameDuration": _seconds(1 / rate),
        "width": str(width), "height": str(height),
    })
    asset_ids = {}
    next_id = 2
    assets = {asset.asset_id: asset for asset in timeline.assets}
    usage = {}
    for track in timeline.tracks:
        for clip in track.clips:
            usage.setdefault(clip.media_asset_id, []).append(clip)
    for asset in timeline.assets:
        resource_id = f"r{next_id}"
        next_id += 1
        asset_ids[asset.asset_id] = resource_id
        video_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "video"]
        audio_streams = [item for item in asset.stream_metadata if item.get("codec_type") == "audio"]
        if video_streams and not asset.is_still_image:
            selected = usage.get(asset.asset_id, [])
            selected_indices = {clip.video_stream_index for clip in selected if clip.video_stream_index is not None}
            source_index = next(iter(selected_indices)) if len(selected_indices) == 1 else 0
            source_stream = video_streams[source_index] if source_index < len(video_streams) else video_streams[0]
            source_rate = _stream_rate(source_stream)
            source_width, source_height = source_stream.get("width"), source_stream.get("height")
            if source_rate is not None and type(source_width) is int and type(source_height) is int:
                signature = (source_width, source_height, source_rate)
                if signature not in format_ids:
                    format_id = f"r{next_id}"
                    next_id += 1
                    format_ids[signature] = format_id
                    ET.SubElement(resources, "format", {
                        "id": format_id,
                        "name": f"Source {source_width}x{source_height} {source_rate.numerator}/{source_rate.denominator}",
                        "frameDuration": _seconds(1 / source_rate),
                        "width": str(source_width), "height": str(source_height),
                    })
                source_format_id = format_ids[signature]
            else:
                source_format_id = target_format_id
        else:
            source_format_id = target_format_id if asset.is_still_image else None
        package_path = paths[asset.asset_id]
        media_rel = "/".join(PurePosixPath(package_path).parts[-2:])
        src = quote(media_rel.replace("\\", "/"), safe="/._-")
        source_duration = asset.duration_sec or max((_fraction(clip.duration_sec) for clip in usage.get(asset.asset_id, [])), default=Fraction(1, 1) / rate)
        asset_attrs = {
            "id": resource_id,
            "name": asset.filename,
            "start": "0s",
            "duration": _seconds(max(_fraction(source_duration), Fraction(1, 1) / rate)),
            "hasVideo": "1" if asset.has_video else "0",
            "hasAudio": "1" if asset.audio_stream_count else "0",
        }
        if source_format_id:
            asset_attrs["format"] = source_format_id
        if audio_streams:
            selected_audio = [clip.audio_stream_index for track in timeline.tracks if track.kind != "video"
                              for clip in track.clips if clip.media_asset_id == asset.asset_id and clip.audio_stream_index is not None]
            audio_index = selected_audio[0] if selected_audio else 0
            audio_stream = audio_streams[audio_index] if audio_index < len(audio_streams) else audio_streams[0]
            asset_attrs.update({
                "audioSources": str(len(audio_streams)),
                "audioChannels": str(sum(max(1, int(stream.get("channels") or 1)) for stream in audio_streams)),
                "audioRate": str(int(audio_stream.get("sample_rate") or settings.get("audio_rate") or 48000)),
            })
        asset_element = ET.SubElement(resources, "asset", asset_attrs)
        ET.SubElement(asset_element, "media-rep", {"kind": "original-media", "src": src})

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "OEVIRA Media Exchange"})
    project_element = ET.SubElement(event, "project", {"name": title, "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, project_id + f":{version}:fcpx"))})
    sequence_attrs = {
        "format": target_format_id,
        "duration": _seconds(duration),
        "tcStart": "0s",
        "tcFormat": "NDF",
    }
    if settings.get("audio_rate"):
        sequence_attrs["audioRate"] = f"{int(round(settings['audio_rate'] / 1000))}k"
    sequence = ET.SubElement(project_element, "sequence", sequence_attrs)
    spine = ET.SubElement(sequence, "spine")
    background = ET.SubElement(spine, "gap", {
        "name": "OEVIRA transparent timeline base (host background may differ)",
        "offset": "0s", "start": "0s", "duration": _seconds(duration),
    })
    for chapter in timeline.chapters:
        marker_time, _ = _quantize_frame(_fraction(chapter.start_sec), rate)
        ET.SubElement(background, "chapter-marker", {
            "start": _seconds(marker_time), "value": chapter.title,
        })
    track_lanes = {}
    next_video_lane, next_audio_lane = 1, -1
    for track in timeline.tracks:
        if track.kind == "video":
            track_lanes[track.track_id] = next_video_lane
            next_video_lane += 1
        else:
            track_lanes[track.track_id] = next_audio_lane
            next_audio_lane -= 1
    for track in timeline.tracks:
        lane = track_lanes[track.track_id]
        for clip in sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id)):
            asset = assets[clip.media_asset_id]
            if clip.playback_rate != 1:
                continue
            start = _fraction(clip.timeline_start)
            clip_duration = _fraction(clip.duration_sec)
            target_start, _ = _quantize_frame(start, rate)
            target_duration, _ = _quantize_frame(clip_duration, rate)
            if target_duration <= 0:
                continue
            source_start = _fraction(clip.source_in)
            if clip.display_duration_sec is None and _fraction(clip.source_out) - source_start != clip_duration:
                # A non-unit source/time relationship is never disguised as a normal-speed FCPXML clip.
                continue
            attrs = {
                "ref": asset_ids[asset.asset_id],
                "lane": str(lane),
                "offset": _seconds(target_start),
                "name": clip.clip_id,
                "start": _seconds(source_start),
                "duration": _seconds(target_duration),
            }
            audio_role = {"voiceover": "dialogue", "bgm": "music", "sfx": "effects"}.get(clip.audio_role)
            if audio_role:
                attrs["audioRole"] = audio_role
            ET.SubElement(background, "asset-clip", attrs)
    # FCPXML structure is intentionally portable: every URL is package-relative.
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + b"\n"


def _asset_source_tc_frame(asset, settings: dict, rate: Fraction) -> int:
    mode = settings["timecode_mode"]
    origin = settings.get("source_timecode_origins", {}).get(asset.asset_id)
    return _parse_timecode(origin, rate, mode) if origin else 0


def _cmx_event_line(number: int, reel: str, track: str, edit: str,
                    source_in: int, source_out: int, record_in: int, record_out: int,
                    rate: Fraction, mode: str, *, transition_frames: int | None = None) -> str:
    edit_field = f"{edit} {transition_frames:03d}" if transition_frames is not None else edit
    return (f"{number:03d} {reel:<8.8s} {track:<2.2s} {edit_field} "
            f"{_format_timecode(source_in, rate, mode)} {_format_timecode(source_out, rate, mode)} "
            f"{_format_timecode(record_in, rate, mode)} {_format_timecode(record_out, rate, mode)}")


def _build_cmx(timeline: Timeline, title: str, project_id: str, version: int,
               paths: dict[str, str], settings: dict, loss_report: dict) -> tuple[bytes, bytes]:
    rate = _target_rate(settings)
    mode = settings["timecode_mode"]
    assets = {asset.asset_id: asset for asset in timeline.assets}
    reels = {}
    hashes_by_reel = {}
    for asset_id, asset in assets.items():
        reel = _reel_name(asset)
        previous_hash = hashes_by_reel.setdefault(reel, asset.sha256)
        if previous_hash != asset.sha256:
            raise ApiError("MEDIA_INTERCHANGE_REEL_NAME_COLLISION", "Two source hashes resolve to the same CMX3600 reel label", 409)
        reels[asset_id] = reel
    transitions = _cmx_transition_map(timeline, rate)
    transitions_into = {to_clip: transition for (from_clip, to_clip), transition in transitions.items()}
    transitions_out = {from_clip: transition for (from_clip, to_clip), transition in transitions.items()}
    main_video = next((track for track in timeline.tracks if track.kind == "video" and track.clips), None)
    entries = []
    if main_video is not None:
        clips = sorted(main_video.clips, key=lambda item: (item.timeline_start, item.clip_id))
        overlapping = set()
        for left, right in zip(clips, clips[1:]):
            if _fraction(left.timeline_start) + _fraction(left.duration_sec) > _fraction(right.timeline_start):
                overlapping.update({left.clip_id, right.clip_id})
        for clip in clips:
            asset = assets[clip.media_asset_id]
            if clip.clip_id in overlapping or asset.is_still_image:
                continue
            source_rate = _clip_video_rate(asset, clip)
            if clip.playback_rate != 1 and source_rate is None:
                continue
            if clip.audio_role == "muted":
                source_track = "V"
            elif asset.audio_stream_count:
                source_track = "VA"
            else:
                source_track = "V"
            src_origin = _asset_source_tc_frame(asset, settings, rate)
            src_in = src_origin + _round_half_up(_fraction(clip.source_in) * rate)
            src_out = src_origin + _round_half_up(_fraction(clip.source_out) * rate)
            rec_in = _round_half_up(_fraction(clip.timeline_start) * rate)
            rec_out = _round_half_up((_fraction(clip.timeline_start) + _fraction(clip.duration_sec)) * rate)
            if clip.clip_id in transitions_into:
                entry = transitions_into[clip.clip_id]
                src_in -= _round_half_up(Fraction(entry["before_frames"], 1) * _fraction(clip.playback_rate))
            if clip.clip_id in transitions_out:
                entry = transitions_out[clip.clip_id]
                src_out += _round_half_up(Fraction(entry["after_frames"], 1) * _fraction(clip.playback_rate))
            if clip.playback_rate == 1 and src_out - src_in != rec_out - rec_in:
                src_out = src_in + (rec_out - rec_in)
            incoming = transitions_into.get(clip.clip_id)
            event_edit = "D" if incoming else "C"
            transition_frames = incoming["duration_frames"] if incoming else None
            entries.append((rec_in, rec_out, reels[asset.asset_id], source_track, src_in, src_out,
                            clip, main_video.track_id, event_edit, transition_frames, source_rate))

    audio_tracks = [track for track in timeline.tracks if track.kind != "video" and track.clips]
    audio_slots = {track.track_id: index for index, track in enumerate(audio_tracks)}
    for track in audio_tracks[:3]:
        slot = audio_slots[track.track_id]
        cmx_track = ("A", "AA", "AB")[slot]
        for clip in sorted(track.clips, key=lambda item: (item.timeline_start, item.clip_id)):
            asset = assets[clip.media_asset_id]
            if clip.playback_rate != 1 or asset.is_still_image:
                continue
            src_origin = _asset_source_tc_frame(asset, settings, rate)
            src_in = src_origin + _round_half_up(_fraction(clip.source_in) * rate)
            src_out = src_origin + _round_half_up(_fraction(clip.source_out) * rate)
            rec_in = _round_half_up(_fraction(clip.timeline_start) * rate)
            rec_out = _round_half_up((_fraction(clip.timeline_start) + _fraction(clip.duration_sec)) * rate)
            if src_out - src_in != rec_out - rec_in:
                src_out = src_in + (rec_out - rec_in)
            entries.append((rec_in, rec_out, reels[asset.asset_id], cmx_track, src_in, src_out,
                            clip, track.track_id, "C", None, None))

    entries.sort(key=lambda item: (item[0], item[1], item[3], item[6].clip_id))
    lines = [f"TITLE: {title[:80]}", f"FCM: {'DROP FRAME' if mode == 'DF' else 'NON-DROP FRAME'}",
             f"* OEVIRA PROJECT: {project_id}", f"* OEVIRA PROJECT VERSION: {version}",
             f"* OEVIRA SOURCE SNAPSHOT SHA256: {loss_report['source_snapshot_sha256']}",
             f"* OEVIRA ADAPTER: {_ADAPTER_REVISIONS['cmx_3600']}",
             f"* OEVIRA FRAME RATE: {settings['frame_rate']}",
             "* Source TC policy: explicit origin if supplied; otherwise file-relative frame zero."]
    reel_manifest = {}
    for asset_id, asset in sorted(assets.items()):
        reel_manifest.setdefault(reels[asset_id], {"sha256": asset.sha256, "assets": []})["assets"].append({
            "asset_id": asset_id,
            "material_id": asset.source_material_id,
            "filename": asset.filename,
            "relative_path": paths[asset_id],
            "source_timecode_origin": settings.get("source_timecode_origins", {}).get(asset_id),
            "file_relative_timecode": asset_id not in settings.get("source_timecode_origins", {}),
        })
    for number, entry in enumerate(entries, 1):
        if number > 9999:
            raise ApiError("MEDIA_INTERCHANGE_EVENT_COUNT_UNSUPPORTED", "CMX3600 event numbering exceeds the adapter's 4-digit portable range", 422)
        rec_in, rec_out, reel, source_track, source_in, source_out, clip, track_id, edit, transition_frames, source_rate = entry
        line = _cmx_event_line(number, reel, source_track, edit, source_in, source_out,
                               rec_in, rec_out, rate, mode, transition_frames=transition_frames)
        lines.append(line)
        if clip.playback_rate != 1 and source_rate is not None:
            speed = source_rate * _fraction(clip.playback_rate)
            speed_text = f"{float(speed):.6f}".rstrip("0").rstrip(".")
            lines.append(f"M2 {reel:<8.8s} {speed_text} {_format_timecode(source_in, rate, mode)}")
        lines.extend([f"* FROM CLIP NAME: {assets[clip.media_asset_id].filename[:80]}",
                      f"* OEVIRA CLIP ID: {clip.clip_id}", f"* OEVIRA TRACK ID: {track_id}"])
        if edit == "D" and transition_frames is not None:
            lines.append(f"* OEVIRA TRANSITION ALIGNMENT: centered handles {transition_frames} frames")
    for chapter in timeline.chapters:
        frame = _round_half_up(_fraction(chapter.start_sec) * rate)
        lines.append(f"* LOC: {chapter.title[:160]}")
        lines.append(f"* OEVIRA LOC TC: {_format_timecode(frame, rate, mode)}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8"), (_json({"schema": "oevira.media.cmx3600-reel-map.v1",
                                                                            "adapter_revision": _ADAPTER_REVISIONS["cmx_3600"],
                                                                            "frame_rate": settings["frame_rate"],
                                                                            "timecode_mode": mode,
                                                                            "reels": reel_manifest}) + "\n").encode("utf-8")


def _package_static_members(project: dict, version_row, state: dict, timeline: Timeline,
                            report: dict, profile: str, paths: dict[str, str]) -> dict[str, bytes]:
    title = project["title"]
    project_doc = {
        "schema": "oevira.media.interchange.native-project.v1",
        "project": {"id": project["id"], "title": title, "kind": project["kind"],
                    "project_version": version_row["version"],
                    "parent_version": version_row["parent_version"],
                    "operation": version_row["operation"],
                    "format_ratio": timeline.format_ratio},
        "state": state,
        "dependencies": [
            {"asset_id": asset.asset_id, "material_id": asset.source_material_id,
             "filename": asset.filename, "sha256": asset.sha256,
             "relative_path": paths[asset.asset_id]}
            for asset in sorted(timeline.assets, key=lambda item: item.asset_id)
        ],
        "source_package_state": "self_contained_original_bytes",
        "rights_state": "unknown",
        "publication_state": "not_performed",
    }
    members = {
        "media/native-project.json": (_json(project_doc) + "\n").encode("utf-8"),
        "media/loss-report.json": (_json(report) + "\n").encode("utf-8"),
    }
    storyboard = state.get("storyboard", {"nodes": []})
    if isinstance(storyboard, dict) and storyboard.get("nodes"):
        members["media/storyboard.json"] = (_json({
            "schema": "oevira.media.storyboard.exchange.v1",
            "project_id": project["id"], "project_version": version_row["version"],
            "storyboard": storyboard,
            "target_host_projection": "companion_sidecar_only",
        }) + "\n").encode("utf-8")
    title = title.strip() or "Media Project"
    if profile == "otio":
        members["timeline.otio"] = _build_otio(timeline, title, project["id"], version_row["version"], paths, report)
    elif profile == "fcpx_xml":
        bundle = f"{_safe_leaf(title)}-{project['id'][:8]}.fcpxmld"
        members[f"{bundle}/Info.fcpxml"] = _build_fcpxml(
            timeline, title, project["id"], version_row["version"], paths, report["target_settings"], report,
        )
        for asset_id, relative in paths.items():
            filename = PurePosixPath(relative).name
            members[f"{bundle}/media/{filename}"] = b""  # streamed from the original Material by the worker
    else:
        edl, reel_map = _build_cmx(timeline, title, project["id"], version_row["version"],
                                   paths, report["target_settings"], report)
        members["sequence.edl"] = edl
        members["reel-map.json"] = reel_map
    return members


def _dependency_paths(timeline: Timeline, profile: str, title: str, project_id: str) -> dict[str, str]:
    bundle_prefix = f"{_safe_leaf(title)}-{project_id[:8]}.fcpxmld/media/" if profile == "fcpx_xml" else "dependencies/"
    result = {}
    for index, asset in enumerate(sorted(timeline.assets, key=lambda item: item.asset_id), 1):
        result[asset.asset_id] = f"{bundle_prefix}{index:04d}-{asset.sha256[:12]}-{_safe_leaf(asset.filename)}"
    return result


def _member_manifest(static_members: dict[str, bytes], timeline: Timeline,
                     paths: dict[str, str]) -> dict[str, dict]:
    result = {name: {"sha256": _digest_bytes(data), "size_bytes": len(data), "kind": "interchange_metadata"}
              for name, data in static_members.items() if data}
    for asset in timeline.assets:
        result[paths[asset.asset_id]] = {"sha256": asset.sha256, "size_bytes": Path(asset.path).stat().st_size,
                                         "kind": "material_dependency", "material_id": asset.source_material_id,
                                         "asset_id": asset.asset_id}
    return result


def _complete_package_manifest(project: dict, version: int, report: dict,
                               member_manifest: dict[str, dict]) -> bytes:
    document = {
        "schema": "oevira.media.interchange-package.v1",
        "project_id": project["id"],
        "project_version": version,
        "target_profile": report["target_profile"],
        "target_format_version": report["target_format_version"],
        "adapter_revision": report["adapter_revision"],
        "source_snapshot_sha256": report["source_snapshot_sha256"],
        "accepted_loss_item_ids": report.get("accepted_loss_item_ids", []),
        "members": dict(sorted(member_manifest.items())),
        "target_host_verification": "NOT_VERIFIED",
        "target_host": report["target_host"],
        "no_absolute_source_paths": True,
    }
    return (_json(document) + "\n").encode("utf-8")


def _add_manifest_to_members(members: dict[str, bytes], project: dict, version: int,
                             report: dict, timeline: Timeline, paths: dict[str, str]) -> dict[str, dict]:
    manifest = _member_manifest(members, timeline, paths)
    manifest_bytes = _complete_package_manifest(project, version, report, manifest)
    members["media/package-manifest.json"] = manifest_bytes
    manifest["media/package-manifest.json"] = {
        "sha256": _digest_bytes(manifest_bytes), "size_bytes": len(manifest_bytes),
        "kind": "package_manifest",
    }
    return manifest


def _require_interchange_scope(connection, identity: dict, row, *, target_work_id: str | None) -> tuple[dict, dict]:
    workspace_id = identity["workspace"]["id"]
    if row["workspace_id"] != workspace_id:
        raise _not_found()
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
        (row["project_id"], workspace_id),
    ).fetchone()
    if project is None:
        raise _not_found()
    if target_work_id:
        require_work_access(connection, identity, target_work_id, write=False)
    elif project["work_id"]:
        require_work_access(connection, identity, project["work_id"], write=False)
    else:
        require_workspace_access(connection, identity, write=False)
    target_scope = target_work_id if target_work_id is not None else project["work_id"]
    source_commission = work_commission(connection, project["work_id"])
    target_commission = work_commission(connection, target_scope)
    if source_commission is not None and source_commission != target_commission:
        raise ApiError("CLIENT_SCOPE_MISMATCH", "This Media interchange belongs to a different client", 403)
    version_row = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
        (row["project_id"], workspace_id, row["project_version"]),
    ).fetchone()
    if version_row is None:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The frozen Media project version is unavailable", 409)
    from . import media_api as media

    state = media._require_media_project_version_sources(
        connection, identity, project, version_row, target_work_id=target_scope,
    )
    try:
        settings = json.loads(row["target_settings_json"])
        source_hashes = sorted((
            {"asset_id": item.get("asset_id"), "material_id": item.get("source_material_id"),
             "sha256": item.get("sha256")}
            for item in state.get("timeline", {}).get("assets", []) if isinstance(item, dict)
        ), key=lambda item: item["asset_id"] or "")
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The interchange source snapshot cannot be read", 409) from exc
    expected_snapshot = _digest_bytes(_json({
        "project_id": project["id"],
        "project_version": row["project_version"],
        "state_sha256": _digest_bytes(_json(state).encode("utf-8")),
        "source_hashes": source_hashes,
        "target_profile": row["target_profile"],
        "adapter_revision": row["adapter_revision"],
        "target_settings": settings,
    }).encode("utf-8"))
    if expected_snapshot != row["source_snapshot_sha256"]:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The interchange is not bound to the current saved source manifest", 409)
    return dict(project), state


def require_media_interchange_access(connection, identity: dict, interchange_id: str,
                                     *, target_work_id: str) -> dict:
    """Authorize one exact completed interchange package for a DeliveryBatch Work."""
    if not isinstance(target_work_id, str) or not target_work_id:
        raise _not_found()
    return _require_completed_interchange(connection, identity, interchange_id,
                                          target_work_id=target_work_id)


def _require_completed_interchange(connection, identity: dict, interchange_id: str,
                                   *, target_work_id: str | None) -> dict:
    row = connection.execute(
        "SELECT i.*, j.kind AS job_kind, j.status AS job_status, j.payload_json, j.work_id AS job_work_id "
        "FROM media_interchanges i JOIN jobs j ON j.id=i.job_id AND j.workspace_id=i.workspace_id "
        "WHERE i.id=? AND i.workspace_id=?",
        (interchange_id, identity["workspace"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    project, state = _require_interchange_scope(connection, identity, row, target_work_id=target_work_id)
    if row["job_kind"] != "media_interchange" or row["job_work_id"] != project["work_id"]:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The interchange job and Media project scope differ", 409)
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The interchange request cannot be read", 409) from exc
    if (not isinstance(payload, dict) or payload.get("interchange_id") != row["id"]
            or payload.get("project_id") != row["project_id"]
            or payload.get("version") != row["project_version"]
            or payload.get("target_profile") != row["target_profile"]
            or payload.get("adapter_revision") != row["adapter_revision"]
        or payload.get("source_snapshot_sha256") != row["source_snapshot_sha256"]
            or payload.get("target_settings") != json.loads(row["target_settings_json"])):
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The interchange result differs from its frozen request", 409)
    if (row["status"] != "completed" or row["job_status"] != "completed"
            or not row["relative_path"] or not row["sha256"]
            or type(row["size_bytes"]) is not int or row["size_bytes"] <= 0):
        raise ApiError("MEDIA_INTERCHANGE_NOT_DELIVERABLE", "Only a completed Media exchange package can be delivered", 409)
    report = json.loads(row["loss_report_json"])
    if (report.get("project_version") != row["project_version"]
            or report.get("source_snapshot_sha256") != row["source_snapshot_sha256"]
            or report.get("adapter_revision") != row["adapter_revision"]
            or report.get("accepted_loss_item_ids") != json.loads(row["accepted_loss_item_ids_json"])):
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved loss acceptance receipt does not match this package", 409)
    return dict(row)


def resolve_media_interchange_file(store, identity: dict, interchange_id: str,
                                   *, target_work_id: str | None) -> dict:
    with store.transaction(immediate=False) as connection:
        if target_work_id is None:
            row = _require_completed_interchange(
                connection, identity, interchange_id, target_work_id=None,
            )
        else:
            row = require_media_interchange_access(
                connection, identity, interchange_id, target_work_id=target_work_id,
            )
    path = _resource_path(store, row["relative_path"])
    if not path.is_file() or path.is_symlink():
        raise ApiError("MEDIA_INTERCHANGE_FILE_UNAVAILABLE", "The completed Media interchange package is unavailable", 409)
    initial_size = path.stat().st_size
    digest = sha256_media_file(path)
    final_size = path.stat().st_size
    if initial_size != final_size or digest != row["sha256"] or final_size != row["size_bytes"]:
        raise ApiError("MEDIA_INTERCHANGE_FILE_INTEGRITY_ERROR", "The completed Media exchange package failed integrity verification", 409)
    return {
        "path": path,
        "filename": f"media-{row['id']}-{row['target_profile']}.zip",
        "mime_type": "application/zip",
        "sha256": digest,
        "size_bytes": final_size,
        "project_id": row["project_id"],
        "project_version": row["project_version"],
        "target_profile": row["target_profile"],
        "adapter_revision": row["adapter_revision"],
    }


def _safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and ".." not in path.parts
                and "\\" not in name and not name.startswith("/") and path.as_posix() == name)


def _expected_materials(store, identity: dict, timeline: Timeline) -> dict[str, dict]:
    expected = {}
    workspace_id = identity["workspace"]["id"]
    for asset in timeline.assets:
        if not asset.source_material_id:
            raise ApiError("MEDIA_SOURCE_REFERENCE_INVALID", "An interchange source is not bound to a Material", 409)
        material = store.row(
            "SELECT id, filename, sha256, size_bytes, relative_path, status FROM materials WHERE id=? AND workspace_id=?",
            (asset.source_material_id, workspace_id),
        )
        if (material is None or material["status"] in {"pending", "error"}
                or material["sha256"] != asset.sha256 or type(material["size_bytes"]) is not int
                or material["size_bytes"] < 1):
            raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A frozen Media source Material is unavailable or changed", 409)
        path = _resource_path(store, material["relative_path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != material["size_bytes"]:
            raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A frozen Media source Material is unavailable or changed", 409)
        expected[asset.asset_id] = {"asset": asset, "material": dict(material), "path": path}
    return expected


def _checkpoint_entries(row) -> dict[str, dict]:
    try:
        entries = json.loads(row["checkpoint_members_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_CHECKPOINT_INVALID", "The saved interchange checkpoint cannot be read", 409) from exc
    if type(entries) is not dict:
        raise ApiError("MEDIA_INTERCHANGE_CHECKPOINT_INVALID", "The saved interchange checkpoint is invalid", 409)
    for name, item in entries.items():
        if (not _safe_zip_name(name) or type(item) is not dict
                or set(item) != {"sha256", "size_bytes"}
                or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))) is None
                or type(item.get("size_bytes")) is not int or item["size_bytes"] < 0):
            raise ApiError("MEDIA_INTERCHANGE_CHECKPOINT_INVALID", "The saved interchange checkpoint is invalid", 409)
    return entries


def _archive_receipt(path: Path, *, expected_sha256: str | None = None,
                     expected_size: int | None = None) -> tuple[str, int]:
    if not path.is_file() or path.is_symlink():
        raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange package checkpoint is unavailable", 409)
    size = path.stat().st_size
    digest = sha256_media_file(path)
    if expected_size is not None and size != expected_size or expected_sha256 is not None and digest != expected_sha256:
        raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange checkpoint failed integrity verification", 409)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(not _safe_zip_name(name) for name in names):
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange checkpoint contains an invalid member path", 409)
            if archive.testzip() is not None:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange checkpoint is corrupt", 409)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange checkpoint is corrupt", 409) from exc
    return digest, size


def _verify_checkpoint_members(path: Path, expected_entries: dict[str, dict],
                               expected_members: dict[str, dict]) -> None:
    digest, _ = _archive_receipt(path)
    del digest
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if not set(expected_entries) <= names:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A completed package member is missing from its checkpoint", 409)
            for name, receipt in expected_entries.items():
                if name not in expected_members or expected_members[name]["sha256"] != receipt["sha256"]:
                    raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A checkpoint member differs from the accepted exchange plan", 409)
                digest = hashlib.sha256()
                total = 0
                with archive.open(name, "r") as stream:
                    for block in iter(lambda: stream.read(_CHUNK_BYTES), b""):
                        digest.update(block)
                        total += len(block)
                if digest.hexdigest() != receipt["sha256"] or total != receipt["size_bytes"]:
                    raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A completed checkpoint member failed its hash check", 409)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved interchange checkpoint is corrupt", 409) from exc


def _identity_for_claim(claim: dict) -> dict:
    return {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}


def validate_media_interchange_resume(connection, actor: dict, job, resume_context: dict) -> None:
    if resume_context:
        raise ApiError("INVALID_INPUT", "Continue the original Media exchange request and accepted loss plan", 400)
    workspace_id = actor["workspace"]["id"]
    try:
        payload = json.loads(job["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved exchange request cannot be read", 409) from exc
    if (not isinstance(payload, dict) or payload.get("target_profile") not in _PROFILES
            or payload.get("adapter_revision") != _ADAPTER_REVISIONS.get(payload.get("target_profile"))
            or type(payload.get("version")) is not int):
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved exchange request cannot be read", 409)
    row = connection.execute(
        "SELECT * FROM media_interchanges WHERE id=? AND workspace_id=? AND job_id=?",
        (payload.get("interchange_id"), workspace_id, job["id"]),
    ).fetchone()
    project = connection.execute(
        "SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
        (payload["project_id"], workspace_id),
    ).fetchone()
    version_row = connection.execute(
        "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
        (payload["project_id"], workspace_id, payload["version"]),
    ).fetchone()
    if row is None or project is None or version_row is None or project["current_version"] != payload["version"]:
        raise ApiError("MEDIA_INTERCHANGE_INPUT_CHANGED", "The project version changed; create a fresh exchange preflight", 409)
    if project["work_id"]:
        require_work_access(connection, actor, project["work_id"], write=True)
    else:
        require_workspace_access(connection, actor, write=True)
    from . import media_api as media

    state = media._require_media_project_version_sources(
        connection, actor, project, version_row, target_work_id=project["work_id"],
    )
    profile = payload["target_profile"]
    settings = payload.get("target_settings")
    if type(settings) is not dict or settings != json.loads(row["target_settings_json"]):
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved target settings differ from the accepted plan", 409)
    timeline_assets = state.get("timeline", {}).get("assets", [])
    source_hashes = sorted((
        {"asset_id": item.get("asset_id"), "material_id": item.get("source_material_id"), "sha256": item.get("sha256")}
        for item in timeline_assets if isinstance(item, dict)
    ), key=lambda item: item["asset_id"] or "")
    snapshot = {
        "project_id": payload["project_id"],
        "project_version": payload["version"],
        "state_sha256": _digest_bytes(_json(state).encode("utf-8")),
        "source_hashes": source_hashes,
        "target_profile": profile,
        "adapter_revision": _ADAPTER_REVISIONS[profile],
        "target_settings": settings,
    }
    current_hash = _digest_bytes(_json(snapshot).encode("utf-8"))
    if current_hash != payload.get("source_snapshot_sha256") or current_hash != row["source_snapshot_sha256"]:
        raise ApiError("MEDIA_INTERCHANGE_INPUT_CHANGED", "The saved project, sources or adapter plan changed; create a fresh preflight", 409)
    accepted = payload.get("accepted_loss_item_ids")
    try:
        saved_accepted = json.loads(row["accepted_loss_item_ids_json"])
        report = json.loads(row["loss_report_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The accepted loss receipt cannot be read", 409) from exc
    if (not isinstance(accepted, list) or accepted != saved_accepted
            or report.get("source_snapshot_sha256") != current_hash
            or report.get("adapter_revision") != _ADAPTER_REVISIONS[profile]
            or set(accepted) != set(report.get("required_accepted_loss_item_ids", []))):
        raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved loss acceptance does not match this input version", 409)
    if row["status"] == "completed":
        if (not row["relative_path"] or not row["sha256"]
                or type(row["size_bytes"]) is not int or row["size_bytes"] <= 0):
            raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The completed exchange has no verified output receipt", 409)
        return
    if row["staging_path"]:
        if (not row["sha256"] or type(row["size_bytes"]) is not int or row["size_bytes"] <= 0
                or not row["checkpoint_members_json"]):
            raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved exchange checkpoint has no integrity receipt", 409)
        _checkpoint_entries(row)
    elif row["status"] == "writing":
        # No closed ZIP receipt was committed; no result exists to reuse.
        return


def _check_job_boundary(store, claim: dict, project_id: str, *, heartbeat: list[float],
                        access_check: list[float]) -> None:
    from .worker import claim_can_commit

    with store.transaction() as connection:
        job = connection.execute(
            "SELECT status, pause_requested, cancel_requested, lease_generation FROM jobs WHERE id=? AND workspace_id=?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None or not claim_can_commit(connection, claim, allow_stopping=True):
            raise _InterchangeStop("stale")
        if job["status"] == "pause_requested" and job["pause_requested"]:
            raise _InterchangeStop("interrupted")
        if job["cancel_requested"] or job["status"] in {"cancel_requested", "cancelled"}:
            raise _InterchangeStop("cancelled")
        if job["status"] != "running":
            raise _InterchangeStop("stale")
        now = time.monotonic()
        if now - heartbeat[0] >= 4:
            connection.execute(
                "UPDATE jobs SET lease_heartbeat_at=? WHERE id=? AND workspace_id=? AND lease_generation=? AND status='running'",
                (utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
            )
            heartbeat[0] = now
    if time.monotonic() - access_check[0] >= 20:
        identity = _identity_for_claim(claim)
        with store.transaction(immediate=False) as connection:
            project = connection.execute(
                "SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (project_id, claim["workspace_id"]),
            ).fetchone()
            if project is None or project["work_id"] != claim.get("work_id"):
                raise ApiError("MEDIA_INTERCHANGE_ACCESS_REVOKED", "Media project access changed during exchange", 403)
            if project["work_id"]:
                require_work_access(connection, identity, project["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
        access_check[0] = time.monotonic()


def _update_progress(store, claim: dict, completed: int, total: int, label: str) -> None:
    from .worker import claim_can_commit

    with store.transaction() as connection:
        if not claim_can_commit(connection, claim):
            return
        connection.execute(
            "UPDATE jobs SET progress_completed=?,progress_total=?,progress_label=?,updated_at=? "
            "WHERE id=? AND workspace_id=? AND lease_generation=? AND status='running' AND pause_requested=0 AND cancel_requested=0",
            (completed, total, label[:200], utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
        )


def _copy_archive_members(source_zip: Path, destination: zipfile.ZipFile,
                          receipts: dict[str, dict], expected: dict[str, dict],
                          check_continue) -> dict[str, dict]:
    copied = {}
    with zipfile.ZipFile(source_zip, "r") as source:
        for name in sorted(receipts):
            check_continue()
            if name not in expected:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The checkpoint contains an unexpected package member", 409)
            digest = hashlib.sha256()
            size = 0
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            with source.open(name, "r") as reader, destination.open(info, "w") as writer:
                for block in iter(lambda: reader.read(_CHUNK_BYTES), b""):
                    check_continue()
                    writer.write(block)
                    digest.update(block)
                    size += len(block)
            receipt = {"sha256": digest.hexdigest(), "size_bytes": size}
            if receipt != receipts[name] or receipt["sha256"] != expected[name]["sha256"]:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A saved package member differs from its checkpoint", 409)
            copied[name] = receipt
    return copied


def _zip_static_members(archive: zipfile.ZipFile, members: dict[str, bytes],
                        expected: dict[str, dict], copied: dict[str, dict], check_continue) -> None:
    for name, content in sorted(members.items()):
        if not content or name in copied:
            continue
        check_continue()
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_STORED
        with archive.open(info, "w") as writer:
            for offset in range(0, len(content), _CHUNK_BYTES):
                check_continue()
                writer.write(content[offset:offset + _CHUNK_BYTES])
        copied[name] = {"sha256": _digest_bytes(content), "size_bytes": len(content)}
        if (copied[name]["sha256"] != expected[name]["sha256"]
                or copied[name]["size_bytes"] != expected[name]["size_bytes"]):
            raise ApiError("MEDIA_INTERCHANGE_PACKAGE_CHANGED", "An interchange metadata member changed during package creation", 409)


def _zip_material(archive: zipfile.ZipFile, name: str, item: dict,
                  expected: dict[str, dict], copied: dict[str, dict], check_continue) -> None:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    digest = hashlib.sha256()
    size = 0
    initial = item["path"].stat()
    if initial.st_size != item["material"]["size_bytes"]:
        raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A Media dependency changed during exchange package creation", 409)
    with item["path"].open("rb") as source, archive.open(info, "w") as output:
        for block in iter(lambda: source.read(_CHUNK_BYTES), b""):
            check_continue()
            output.write(block)
            digest.update(block)
            size += len(block)
    final = item["path"].stat()
    receipt = {"sha256": digest.hexdigest(), "size_bytes": size}
    if (final.st_size != initial.st_size or receipt["sha256"] != item["asset"].sha256
            or receipt["size_bytes"] != item["material"]["size_bytes"]
            or receipt["sha256"] != expected[name]["sha256"]
            or receipt["size_bytes"] != expected[name]["size_bytes"]):
        raise ApiError("SOURCE_FILE_INTEGRITY_ERROR", "A Media dependency changed during exchange package creation", 409)
    copied[name] = receipt


def _persist_checkpoint(store, claim: dict, row, stage_path: Path,
                        relative_stage_path: str, members: dict[str, dict],
                        status: str) -> bool:
    from .worker import claim_can_commit

    if not stage_path.is_file() or stage_path.is_symlink():
        return False
    digest = sha256_media_file(stage_path)
    size = stage_path.stat().st_size
    with store.transaction() as connection:
        if not claim_can_commit(connection, claim, allow_stopping=True):
            return False
        job = connection.execute(
            "SELECT status, pause_requested, cancel_requested FROM jobs WHERE id=? AND workspace_id=?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None:
            return False
        if job["status"] == "cancel_requested" or job["cancel_requested"]:
            status = "cancelled"
        elif job["status"] == "pause_requested" and job["pause_requested"]:
            status = "interrupted"
        elif job["status"] != "running":
            return False
        connection.execute(
            "UPDATE media_interchanges SET status=?,staging_path=?,relative_path=NULL,sha256=?,size_bytes=?,"
            "checkpoint_members_json=?,updated_at=? WHERE id=? AND workspace_id=? AND job_id=?",
            (status, relative_stage_path, digest, size, _json(members), utc_now(),
             row["id"], claim["workspace_id"], claim["id"]),
        )
        return True


def _verify_completed_archive(path: Path, row) -> None:
    _archive_receipt(path, expected_sha256=row["sha256"], expected_size=row["size_bytes"])
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if "media/package-manifest.json" not in names or "media/loss-report.json" not in names:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The completed package manifest is incomplete", 409)
            manifest = json.loads(archive.read("media/package-manifest.json"))
            report = json.loads(archive.read("media/loss-report.json"))
            if (manifest.get("source_snapshot_sha256") != row["source_snapshot_sha256"]
                    or manifest.get("adapter_revision") != row["adapter_revision"]
                    or report.get("source_snapshot_sha256") != row["source_snapshot_sha256"]
                    or report.get("accepted_loss_item_ids") != json.loads(row["accepted_loss_item_ids_json"])):
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The package no longer matches the accepted project plan", 409)
            for name, entry in manifest.get("members", {}).items():
                if name not in names:
                    raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A package dependency is missing", 409)
                digest = hashlib.sha256()
                size = 0
                with archive.open(name, "r") as content:
                    for block in iter(lambda: content.read(_CHUNK_BYTES), b""):
                        digest.update(block)
                        size += len(block)
                if digest.hexdigest() != entry.get("sha256") or size != entry.get("size_bytes"):
                    raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "A package dependency failed its hash check", 409)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ApiError):
            raise
        raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The completed Media interchange package is corrupt", 409) from exc


def _finalize_interchange(store, claim: dict, record, project: dict, version_row,
                          stage_path: Path, relative_stage_path: str,
                          digest: str, size: int) -> bool:
    from .worker import claim_can_commit

    identity = _identity_for_claim(claim)
    with store.transaction() as connection:
        if not claim_can_commit(connection, claim):
            return False
        job = connection.execute(
            "SELECT status,pause_requested,cancel_requested,work_id FROM jobs WHERE id=? AND workspace_id=?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        current_project = connection.execute(
            "SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
            (record["project_id"], claim["workspace_id"]),
        ).fetchone()
        current_version = connection.execute(
            "SELECT * FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
            (record["project_id"], claim["workspace_id"], record["project_version"]),
        ).fetchone()
        if (job is None or job["status"] != "running" or job["pause_requested"] or job["cancel_requested"]):
            return False
        if (current_project is None or current_version is None
                or current_project["current_version"] != record["project_version"]
                or current_project["work_id"] != job["work_id"] or job["work_id"] != claim.get("work_id")):
            raise ApiError("MEDIA_INTERCHANGE_INPUT_CHANGED", "The project version or Work scope changed before exchange commit", 409)
        if current_project["work_id"]:
            require_work_access(connection, identity, current_project["work_id"], write=True)
        else:
            require_workspace_access(connection, identity, write=True)
        from . import media_api as media
        state = media._require_media_project_version_sources(
            connection, identity, current_project, current_version,
            target_work_id=current_project["work_id"],
        )
        if _digest_bytes(_json(state).encode("utf-8")) != _digest_bytes(_json(json.loads(version_row["state_json"])).encode("utf-8")):
            raise ApiError("MEDIA_INTERCHANGE_INPUT_CHANGED", "The frozen project changed before the package was saved", 409)
        output = connection.execute(
            "UPDATE media_interchanges SET status='completed',relative_path=?,staging_path=NULL,sha256=?,size_bytes=?,"
            "error_code=NULL,error_message=NULL,updated_at=? WHERE id=? AND workspace_id=? AND job_id=?",
            (relative_stage_path, digest, size, utc_now(), record["id"], claim["workspace_id"], claim["id"]),
        )
        if output.rowcount != 1:
            return False
        connection.execute(
            "UPDATE jobs SET status='completed',progress_completed=progress_total,progress_label='Media exchange package verified and saved',"
            "error_code=NULL,error_message=NULL,updated_at=? WHERE id=? AND workspace_id=? AND lease_generation=? "
            "AND status='running' AND pause_requested=0 AND cancel_requested=0",
            (utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
        )
    return True


def _finish_interchange_failure(store, claim: dict, record, code: str,
                                message: str) -> None:
    from .worker import claim_can_commit

    with store.transaction() as connection:
        if not claim_can_commit(connection, claim, allow_stopping=True):
            return
        job = connection.execute(
            "SELECT status,pause_requested,cancel_requested FROM jobs WHERE id=? AND workspace_id=?",
            (claim["id"], claim["workspace_id"]),
        ).fetchone()
        if job is None:
            return
        if job["status"] == "pause_requested" and job["pause_requested"]:
            connection.execute("UPDATE media_interchanges SET status='interrupted',updated_at=? WHERE id=? AND workspace_id=? AND job_id=?",
                               (utc_now(), record["id"], claim["workspace_id"], claim["id"]))
            return
        if job["status"] == "cancel_requested" or job["cancel_requested"]:
            connection.execute("UPDATE media_interchanges SET status='cancelled',updated_at=? WHERE id=? AND workspace_id=? AND job_id=?",
                               (utc_now(), record["id"], claim["workspace_id"], claim["id"]))
            return
        if job["status"] != "running":
            return
        connection.execute(
            "UPDATE media_interchanges SET status='failed',error_code=?,error_message=?,updated_at=? "
            "WHERE id=? AND workspace_id=? AND job_id=?",
            (code, message, utc_now(), record["id"], claim["workspace_id"], claim["id"]),
        )
        connection.execute(
            "UPDATE jobs SET status='failed',error_code=?,error_message=?,updated_at=? "
            "WHERE id=? AND workspace_id=? AND lease_generation=? AND status='running' AND pause_requested=0 AND cancel_requested=0",
            (code, message, utc_now(), claim["id"], claim["workspace_id"], claim["generation"]),
        )


def process_media_interchange_job(app, claim: dict) -> None:
    """Build one portable interchange package using the shared Media Job lease."""
    from .worker import claim_can_commit
    from . import media_api as media

    store = app.extensions["oevira.store"]
    identity = _identity_for_claim(claim)
    job = store.row("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (claim["id"], claim["workspace_id"]))
    if job is None or job["kind"] != "media_interchange":
        return
    try:
        payload = json.loads(job["payload_json"])
        record = store.row(
            "SELECT * FROM media_interchanges WHERE id=? AND workspace_id=? AND job_id=?",
            (payload.get("interchange_id"), claim["workspace_id"], claim["id"]),
        )
        if record is None:
            raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved Media exchange record is unavailable", 409)
        if (record["project_id"] != payload.get("project_id")
                or record["project_version"] != payload.get("version")
                or record["target_profile"] != payload.get("target_profile")
                or record["adapter_revision"] != payload.get("adapter_revision")):
            raise ApiError("MEDIA_INTERCHANGE_STATE_UNAVAILABLE", "The saved Media exchange record differs from its Job", 409)
        _check_job_boundary(store, claim, record["project_id"], heartbeat=[time.monotonic()],
                            access_check=[time.monotonic()])
        project, version_row, state, timeline, report, missing = _get_project_snapshot(
            store, identity, record["project_id"], record["project_version"],
            record["target_profile"], json.loads(record["target_settings_json"]), require_current=True,
        )
        snapshot_hash = _project_snapshot_hash(
            project, version_row, state, timeline, record["target_profile"], report["target_settings"],
        )
        if (missing or snapshot_hash != record["source_snapshot_sha256"]
                or snapshot_hash != payload.get("source_snapshot_sha256")):
            raise ApiError("MEDIA_INTERCHANGE_INPUT_CHANGED", "The accepted project, sources or target profile changed before exchange", 409)
        accepted = json.loads(record["accepted_loss_item_ids_json"])
        if set(accepted) != set(report["required_accepted_loss_item_ids"]):
            raise ApiError("MEDIA_INTERCHANGE_PLAN_STALE", "The adapter loss report differs from the accepted preflight", 409)
        report["source_snapshot_sha256"] = snapshot_hash
        report["project_id"] = record["project_id"]
        report["project_version"] = record["project_version"]
        report["accepted_loss_item_ids"] = accepted
        if report["bake_required_items"]:
            raise ApiError("MEDIA_INTERCHANGE_BAKE_REQUIRED", "The exchange plan requires a rendered bake not present in this request", 409)
        paths = _dependency_paths(timeline, record["target_profile"], project["title"], project["id"])
        static_members = _package_static_members(
            project, version_row, state, timeline, report, record["target_profile"], paths,
        )
        expected = _add_manifest_to_members(static_members, project, record["project_version"], report, timeline, paths)
        dependency_by_path = {}
        for asset_id, source in _expected_materials(store, identity, timeline).items():
            dependency_by_path[paths[asset_id]] = source
        checkpoint = _checkpoint_entries(record)
        previous = None
        if record["staging_path"]:
            previous = _resource_path(store, record["staging_path"])
            if not record["sha256"] or type(record["size_bytes"]) is not int:
                raise ApiError("MEDIA_INTERCHANGE_RECOVERY_REQUIRED", "The saved ZIP checkpoint has no integrity receipt", 409)
            _verify_checkpoint_members(previous, checkpoint, expected)
        # A complete checkpoint is already the exact output; adopt it without rebuilding.
        if previous is not None and set(checkpoint) == set(expected):
            _verify_completed_archive(previous, dict(record) | {"relative_path": record["staging_path"]})
            if _finalize_interchange(store, claim, record, project, version_row, previous,
                                     record["staging_path"], record["sha256"], record["size_bytes"]):
                return
            _finish_interchange_failure(store, claim, record, "MEDIA_INTERCHANGE_STOPPED",
                                        "The exchange reached a control boundary before its completion receipt.")
            raise _InterchangeStop("stale")

        relative_stage = f"objects/{claim['workspace_id']}/media-interchanges/{record['id']}/attempt-{claim['generation']}.zip"
        stage = _resource_path(store, relative_stage)
        if stage.exists() or stage.is_symlink():
            raise ApiError("MEDIA_INTERCHANGE_STAGE_COLLISION", "The exchange staging path is already occupied", 409)
        stage.parent.mkdir(parents=True, exist_ok=True)
        heartbeat = [time.monotonic()]
        access_check = [time.monotonic()]
        def check_continue():
            _check_job_boundary(store, claim, record["project_id"], heartbeat=heartbeat,
                                access_check=access_check)
        copied = {}
        total = max(1, len(expected))
        try:
            with zipfile.ZipFile(stage, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                if previous is not None:
                    copied.update(_copy_archive_members(previous, archive, checkpoint, expected, check_continue))
                _zip_static_members(archive, static_members, expected, copied, check_continue)
                completed = len(copied)
                _update_progress(store, claim, completed, total, "Writing exchange manifest and editable timeline")
                for name in sorted(dependency_by_path):
                    if name in copied:
                        continue
                    check_continue()
                    _zip_material(archive, name, dependency_by_path[name], expected, copied, check_continue)
                    completed = len(copied)
                    _update_progress(store, claim, completed, total, "Copying verified Media dependencies")
                check_continue()
        except _InterchangeStop as stop:
            # Closing the ZIP preserves all fully written members as a resumable checkpoint.
            try:
                _persist_checkpoint(store, claim, record, stage, relative_stage, copied, stop.status)
            except OSError:
                pass
            return
        _verify_completed_archive(stage, {
            "sha256": sha256_media_file(stage),
            "size_bytes": stage.stat().st_size,
            "source_snapshot_sha256": record["source_snapshot_sha256"],
            "adapter_revision": record["adapter_revision"],
            "accepted_loss_item_ids_json": record["accepted_loss_item_ids_json"],
        })
        digest = sha256_media_file(stage)
        size = stage.stat().st_size
        if not _persist_checkpoint(store, claim, record, stage, relative_stage, copied, "writing"):
            raise _InterchangeStop("stale")
        if not _finalize_interchange(store, claim, record, project, version_row, stage,
                                     relative_stage, digest, size):
            _finish_interchange_failure(store, claim, record, "MEDIA_INTERCHANGE_STOPPED",
                                        "The exchange reached a control boundary before its completion receipt.")
            raise _InterchangeStop("stale")
    except _InterchangeStop:
        return
    except ApiError as exc:
        code, message = exc.code, exc.message
        if code == "SOURCE_FILE_INTEGRITY_ERROR":
            message = "A frozen Media source no longer matches the accepted project version."
        _finish_interchange_failure(store, claim, record if "record" in locals() else {"id": payload.get("interchange_id")}, code, message)
    except Exception:
        app.logger.error("Media interchange job ended without a verified package")
        _finish_interchange_failure(
            store, claim, record if "record" in locals() else {"id": payload.get("interchange_id")},
            "MEDIA_INTERCHANGE_FAILED", "The Media exchange package could not be completed; the saved project is unchanged.",
        )



