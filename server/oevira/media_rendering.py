"""FFmpeg renderer adapted into OEVIRA's Media owner.

Reuse source: historical self-owned Apache-2.0 snapshot
``D:/LUMENIS_REBUILD/序/序境/lumenis/shared/content_creation/rendering.py``
(SHA-256 ``A992E24477095124072DC2BA2441B5501352A074F5FDE32434EBE5515A8592EA``).
The adapter resolves files only through verified Material references and runs
the hash-verified FFmpeg/ffprobe kept under this product root. It never imports
or calls the former Shared artifact store.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import unicodedata
from bisect import bisect_left
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from threading import RLock
from typing import Any

from .media import (
    AudioBus, AudioBusEffect, AudioBusSend, CaptionStyle, MAX_EDIT_TIME_SEC, MEDIA_INPUT_DEMUXERS,
    SUPPORTED_VIDEO_TRANSITION_TYPES, Timeline, TimelineClip,
    VideoEditingError, VideoPlacement, _ffmpeg_path, _ffprobe_path, _fraction_time,
    _effect_parameters_at, fingerprint_local_media, media_format_origin,
    media_stream_input_origin, media_stream_origin,
)

SAMPLE_RATE = 48000
FRAME_RATE: Fraction = Fraction(30, 1)
AUDIO_CHANNELS = 2
RENDER_INPUT_BATCH_SIZE = 16
SILENCE_SCAN_WINDOW_SECONDS = 600
SILENCE_SCAN_WINDOW_STEP_SECONDS = 588  # Preserve a 12 second overlap at window boundaries.
MAX_SILENCE_SCAN_WINDOWS = 256
MAX_SILENCE_CANDIDATES = 20_000
MIX_SAMPLE_PEAK_LIMIT = 0.891251  # -1 dBFS sample peak; not a true-peak/LUFS guarantee.
FINAL_TRUE_PEAK_LIMIT_DBTP = -1.0
PEAK_CORRECTION_HEADROOM_DB = .5  # Codec response can change with gain; remeasure, never assume.
DEFAULT_FRAME_SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "3:4": (810, 1080), "4:5": (864, 1080), "1:1": (1080, 1080)}
FRAME_SIZES = dict(DEFAULT_FRAME_SIZES)
# Installed host fonts only; no user-controlled path or automatic download.
# Keep the exact selected face and verify every requested glyph before rendering.
_CAPTION_FONTS = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)
CAPTION_FONT = next((font for font in _CAPTION_FONTS if font.is_file()), _CAPTION_FONTS[0])
_RENDER_LOCK = RLock()


def _video_bit_depth(stream: dict[str, Any]) -> int:
    value = stream.get("bits_per_raw_sample")
    if type(value) is int and value > 0:
        return value
    pixel_format = stream.get("pix_fmt")
    if pixel_format == "yuv420p":
        return 8
    if pixel_format in {"rgb24", "bgr24", "rgba", "bgra", "argb", "abgr", "rgb0", "bgr0",
                        "gbrp", "gbrap"}:
        return 8
    if pixel_format in {"rgb48le", "rgb48be", "bgr48le", "bgr48be", "rgba64le", "rgba64be",
                        "gbrp16le", "gbrp16be", "gbrap16le", "gbrap16be"}:
        return 16
    match = re.search(r"p(\d+)(?:le|be)$", str(pixel_format or ""))
    if match:
        return int(match.group(1))
    if pixel_format in {"yuv422p", "yuv444p", "yuvj420p", "yuvj422p", "yuvj444p"}:
        return 8
    return 0


def _video_signal_for_timeline(timeline: Timeline) -> dict[str, Any] | None:
    supported_primaries = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film",
                           "bt2020", "smpte428", "smpte431", "smpte432", "jedec-p22"}
    supported_transfers = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "gamma22", "gamma28",
                           "linear", "log", "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1",
                           "smpte2084", "arib-std-b67"}
    supported_spaces = {"bt709", "fcc", "smpte170m", "bt470bg", "smpte240m", "bt2020nc", "bt2020c",
                        "smpte2085", "chroma-derived-nc", "chroma-derived-c", "ictcp"}
    assets = {asset.asset_id: asset for asset in timeline.assets}
    video_sources = []
    still_image_profiles = []
    interpretation_by_source = {
        (item.asset_id, item.video_stream_index): item
        for item in timeline.video_color_interpretations
    }
    for track in timeline.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = assets[clip.media_asset_id]
            stream_index = 0 if clip.video_stream_index is None else clip.video_stream_index
            stream = _stream_metadata_by_ordinal(asset, "video", stream_index)
            if stream is None:
                raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
            pixel_format = stream.get("pix_fmt")
            bit_depth = _video_bit_depth(stream)
            if asset.is_still_image:
                profile_evidence = asset.format_metadata.get("image_color_profile")
                if profile_evidence not in {"srgb_tagged", "srgb_assumed"}:
                    raise VideoEditingError("MEDIA_IMAGE_COLOR_PROFILE_UNSUPPORTED")
                if bit_depth != 8:
                    raise VideoEditingError("MEDIA_HIGH_BIT_DEPTH_RENDER_UNSUPPORTED")
                if pixel_format not in {"rgb24", "bgr24", "rgba", "bgra", "argb", "abgr",
                                        "rgb0", "bgr0", "gbrp", "gbrap"}:
                    raise VideoEditingError("MEDIA_IMAGE_PIXEL_FORMAT_UNSUPPORTED")
                still_image_profiles.append({
                    "asset_id": asset.asset_id,
                    "source_material_id": asset.source_material_id,
                    "source_sha256": asset.sha256,
                    "video_stream_index": stream_index,
                    "pixel_format": pixel_format,
                    "bit_depth": bit_depth,
                    "range": stream.get("color_range", "pc"),
                    "matrix": "gbr",
                    "transfer": "iec61966-2-1",
                    "primaries": "bt709",
                    "profile_evidence": profile_evidence,
                    "assumption": "sRGB for untagged RGB still" if profile_evidence == "srgb_assumed" else None,
                })
                continue
            color_transfer = stream.get("color_transfer")
            if color_transfer in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12"}:
                raise VideoEditingError("MEDIA_HDR_RENDER_UNSUPPORTED")
            if bit_depth > 8:
                raise VideoEditingError("MEDIA_HIGH_BIT_DEPTH_RENDER_UNSUPPORTED")
            if bit_depth != 8:
                raise VideoEditingError("MEDIA_HIGH_BIT_DEPTH_RENDER_UNSUPPORTED")
            if pixel_format != "yuv420p":
                raise VideoEditingError("MEDIA_VIDEO_PIXEL_FORMAT_UNSUPPORTED")
            observed = {name: stream.get(name) for name in (
                "color_range", "color_space", "color_transfer", "color_primaries",
            )}
            if ((observed["color_primaries"] == "bt2020" or observed["color_space"] == "bt2020nc")
                    and observed["color_transfer"] in (None, "", "unknown", "unspecified")):
                raise VideoEditingError("VIDEO_COLOR_HDR_STATE_UNKNOWN")
            known = {name: value for name, value in observed.items()
                     if value not in (None, "", "unknown", "unspecified")}
            if observed["color_transfer"] in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12", "smpte428"}:
                raise VideoEditingError("MEDIA_HDR_RENDER_UNSUPPORTED")
            interpretation = interpretation_by_source.get((asset.asset_id, stream_index))
            if interpretation is None:
                if set(known) != set(observed):
                    raise VideoEditingError("MEDIA_COLOR_SIGNAL_UNKNOWN")
                source_profile = observed
            else:
                source_profile = interpretation.input_profile.to_dict()
                for name, value in known.items():
                    if source_profile[name] != value:
                        raise VideoEditingError("VIDEO_COLOR_INTERPRETATION_CONFLICTS_SOURCE_METADATA")
            if source_profile["color_transfer"] in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12", "smpte428"}:
                raise VideoEditingError("MEDIA_HDR_RENDER_UNSUPPORTED")
            if source_profile["color_range"] not in {"tv", "pc"}:
                raise VideoEditingError("MEDIA_COLOR_RANGE_UNSUPPORTED")
            if (source_profile["color_primaries"] not in supported_primaries
                    or source_profile["color_transfer"] not in supported_transfers
                    or source_profile["color_space"] not in supported_spaces):
                raise VideoEditingError("MEDIA_COLOR_PROFILE_UNSUPPORTED")
            video_sources.append({
                "asset_id": asset.asset_id,
                "source_material_id": asset.source_material_id,
                "source_sha256": asset.sha256,
                "video_stream_index": stream_index,
                "pixel_format": pixel_format,
                "bit_depth": bit_depth,
                "observed_source_signal": observed,
                "effective_source_signal": source_profile,
                "interpretation": (None if interpretation is None else interpretation.to_dict()),
            })
    if not video_sources and not still_image_profiles:
        return None
    selected_working = {item.working_profile
                        for item in timeline.video_color_interpretations}
    if len(selected_working) > 1:
        raise VideoEditingError("VIDEO_COLOR_WORKING_SPACE_MISMATCH")
    if selected_working:
        output = next(iter(selected_working)).to_dict()
    elif video_sources:
        output = video_sources[0]["effective_source_signal"]
        if any(item["effective_source_signal"] != output for item in video_sources[1:]):
            raise VideoEditingError("MEDIA_COLOR_PROFILE_MISMATCH")
    else:
        output = {
            "color_range": "tv",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
        }
    first = {
        "pixel_format": "yuv420p",
        "bit_depth": 8,
        **output,
    }
    source_transforms = []
    for item in video_sources:
        source_transforms.append({
            **item,
            "output_signal": output,
            "transform_required": item["effective_source_signal"] != output,
        })
    for item in still_image_profiles:
        source_transforms.append({
            **item,
            "effective_source_signal": {
                "color_range": "pc",
                "color_space": "gbr",
                "color_transfer": "iec61966-2-1",
                "color_primaries": "bt709",
            },
            "output_signal": output,
            "transform_required": True,
        })
    return {
        **first,
        "signal_class": "sdr",
        "output_pixel_format": "yuv420p",
        "output_bit_depth": 8,
        "output_color_range": output["color_range"],
        "output_color_space": output["color_space"],
        "output_color_transfer": output["color_transfer"],
        "output_color_primaries": output["color_primaries"],
        "transform": ("user_selected_source_interpretation_and_working_space"
                      if timeline.video_color_interpretations else
                      "sRGB_stills_converted_to_working_space" if still_image_profiles else
                      "source_signal_preserved_without_color_space_conversion" if not any(
                          item["transform_required"] for item in source_transforms
                      ) else "source_profiles_converted_to_working_space"),
        "source_profiles_checked": len(video_sources) + len(still_image_profiles),
        "source_transforms": source_transforms,
        "color_interpretations_applied": bool(timeline.video_color_interpretations),
        **({"still_image_source_profiles": still_image_profiles} if still_image_profiles else {}),
        **({"video_source_profiles": video_sources} if still_image_profiles and video_sources else {}),
        "working_space_selection_source": ("user_selected" if timeline.video_color_interpretations else
                                            "first_video_source_profile" if video_sources else
                                            "explicit_srgb_still_to_bt709_default"),
    }


def _audio_layout() -> str:
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}[AUDIO_CHANNELS]


def resolve_output_spec(timeline: Timeline, output_kind: str, value: Any = None) -> dict[str, Any]:
    if value is None:
        value = {}
    allowed = {"frame_rate", "sample_rate", "audio_channels", "width", "height", "video_signal"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise VideoEditingError("RENDER_OUTPUT_SPEC_INVALID")
    if output_kind not in {"video", "audio"}:
        raise VideoEditingError("OUTPUT_KIND_NOT_SUPPORTED")
    video_assets = [asset for asset in timeline.assets if asset.has_video]
    source_rate = None
    if output_kind == "video" and video_assets:
        for stream in video_assets[0].stream_metadata:
            if stream.get("codec_type") == "video" and stream.get("avg_frame_rate"):
                try:
                    source_rate = _fraction_time(stream["avg_frame_rate"])
                except VideoEditingError:
                    source_rate = None
                if source_rate and 1 <= source_rate <= 120:
                    break
                source_rate = None
    requested_rate = value.get("frame_rate")
    if output_kind == "video":
        frame_rate = _fraction_time(requested_rate) if requested_rate is not None else (source_rate or Fraction(30, 1))
        if not 1 <= frame_rate <= 120:
            raise VideoEditingError("RENDER_FRAME_RATE_INVALID")
        rate_basis = "user_selected" if requested_rate is not None else (
            "first_video_stream_average" if source_rate else "standard_30fps_default"
        )
    else:
        if requested_rate is not None or "width" in value or "height" in value or "video_signal" in value:
            raise VideoEditingError("AUDIO_RENDER_HAS_NO_VIDEO_PROFILE")
        frame_rate = Fraction(30, 1)
        rate_basis = "not_applicable"

    audio_rates = {
        int(stream["sample_rate"])
        for asset in timeline.assets
        for stream in asset.stream_metadata
        if stream.get("codec_type") == "audio" and str(stream.get("sample_rate", "")).isdigit()
    }
    sample_rate = value.get("sample_rate")
    if sample_rate is None:
        sample_rate = next(iter(audio_rates)) if len(audio_rates) == 1 else 48_000
        sample_rate_basis = "matching_sources" if len(audio_rates) == 1 else "standard_48khz_default"
    else:
        sample_rate_basis = "user_selected"
    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
        raise VideoEditingError("RENDER_SAMPLE_RATE_INVALID")
    audio_channels = value.get("audio_channels", 2)
    if type(audio_channels) is not int or audio_channels not in {1, 2, 6, 8}:
        raise VideoEditingError("RENDER_CHANNEL_COUNT_UNSUPPORTED")

    width = height = None
    if output_kind == "video":
        if ("width" in value) != ("height" in value):
            raise VideoEditingError("RENDER_FRAME_SIZE_INVALID")
        if "width" in value:
            width, height = value["width"], value["height"]
        else:
            width, height = DEFAULT_FRAME_SIZES[timeline.format_ratio]
        if (type(width) is not int or type(height) is not int
                or width < 16 or height < 16 or width > 7680 or height > 4320
                or width % 2 or height % 2):
            raise VideoEditingError("RENDER_FRAME_SIZE_INVALID")
        video_signal = _video_signal_for_timeline(timeline)
        requested_signal = value.get("video_signal")
        if requested_signal is not None:
            signal_fields = {"pixel_format", "bit_depth", "color_range", "color_space", "color_transfer", "color_primaries"}
            actual_output_signal = (None if video_signal is None else {
                "pixel_format": video_signal["output_pixel_format"],
                "bit_depth": video_signal["output_bit_depth"],
                "color_range": video_signal.get("output_color_range", video_signal["color_range"]),
                "color_space": video_signal.get("output_color_space", video_signal["color_space"]),
                "color_transfer": video_signal.get("output_color_transfer", video_signal["color_transfer"]),
                "color_primaries": video_signal.get("output_color_primaries", video_signal["color_primaries"]),
            })
            if (type(requested_signal) is not dict or set(requested_signal) != signal_fields
                    or requested_signal != actual_output_signal):
                raise VideoEditingError("MEDIA_COLOR_TRANSFORM_UNSUPPORTED")
    else:
        video_signal = None
    return {
        "output_kind": output_kind,
        "frame_rate": {"num": frame_rate.numerator, "den": frame_rate.denominator},
        "frame_rate_basis": rate_basis,
        "sample_rate": sample_rate,
        "sample_rate_basis": sample_rate_basis,
        "audio_channels": audio_channels,
        "width": width,
        "height": height,
        "video_signal": video_signal,
        "variable_frame_rate_preserved": False if output_kind == "video" else None,
    }


def _video_signal_args(video_signal: dict[str, Any] | None) -> list[str]:
    if video_signal is None:
        raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
    return [
        "-pix_fmt", video_signal["output_pixel_format"],
        "-color_range", video_signal.get("output_color_range", video_signal["color_range"]),
        "-colorspace", video_signal.get("output_color_space", video_signal["color_space"]),
        "-color_trc", video_signal.get("output_color_transfer", video_signal["color_transfer"]),
        "-color_primaries", video_signal.get("output_color_primaries", video_signal["color_primaries"]),
    ]


def _video_scale_filter(
    width: int,
    height: int,
    video_signal: dict[str, Any] | None,
    *,
    rgb_input: bool = False,
) -> str:
    if video_signal is None:
        raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
    if rgb_input:
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease"
    matrix = video_signal.get("output_color_space", video_signal["color_space"])
    color_range = video_signal.get("output_color_range", video_signal["color_range"])
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"in_color_matrix={matrix}:out_color_matrix={matrix}:"
        f"in_range={color_range}:out_range={color_range}"
    )


def _still_image_color_transform_filter(
    asset,
    video_signal: dict[str, Any] | None,
) -> str:
    if video_signal is None:
        raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
    if asset.format_metadata.get("image_color_profile") not in {"srgb_tagged", "srgb_assumed"}:
        raise VideoEditingError("MEDIA_IMAGE_COLOR_PROFILE_UNSUPPORTED")
    output_primaries = video_signal.get("output_color_primaries", video_signal["color_primaries"])
    output_transfer = video_signal.get("output_color_transfer", video_signal["color_transfer"])
    return (
        "zscale=matrixin=gbr:rangein=full:primariesin=bt709:transferin=iec61966-2-1:"
        f"matrix=gbr:range=full:primaries={output_primaries}:transfer={output_transfer},format=gbrap"
    )


def _video_color_transform_filter(
    asset,
    video_stream_index: int,
    video_signal: dict[str, Any] | None,
) -> str | None:
    if video_signal is None:
        raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
    sources = video_signal.get("source_transforms", [])
    source = next((item for item in sources
                   if item.get("asset_id") == asset.asset_id
                   and item.get("video_stream_index") == video_stream_index), None)
    if source is None:
        raise VideoEditingError("MEDIA_VIDEO_SOURCE_PROFILE_MISSING")
    input_profile = source["effective_source_signal"]
    output_profile = source["output_signal"]
    if not source.get("transform_required"):
        return None
    source_range = "full" if input_profile["color_range"] == "pc" else "limited"
    return (
        f"zscale=matrixin={input_profile['color_space']}:rangein={source_range}:"
        f"primariesin={input_profile['color_primaries']}:transferin={input_profile['color_transfer']}:"
        f"matrix=gbr:range=full:primaries={output_profile['color_primaries']}:"
        f"transfer={output_profile['color_transfer']}:dither=error_diffusion,format=gbrap"
    )


def _video_signal_filter(video_signal: dict[str, Any] | None) -> str:
    if video_signal is None:
        raise VideoEditingError("MEDIA_VIDEO_SIGNAL_UNAVAILABLE")
    return (
        "setparams="
        f"color_primaries={video_signal.get('output_color_primaries', video_signal['color_primaries'])}:"
        f"color_trc={video_signal.get('output_color_transfer', video_signal['color_transfer'])}:"
        f"colorspace={video_signal.get('output_color_space', video_signal['color_space'])}:"
        f"range={video_signal.get('output_color_range', video_signal['color_range'])}"
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_asset_path(asset) -> Path:
    try:
        path = Path(asset.path).resolve(strict=True)
        if path.is_symlink() or not path.is_file() or _hash_file(path) != asset.sha256:
            raise ValueError("source mismatch")
        return path
    except (OSError, ValueError) as exc:
        raise VideoEditingError("SOURCE_MEDIA_NOT_READABLE") from exc


def _run(command: list[str], *, cwd: Path, timeout: float = 60,
         check_continue: Callable[[], None] | None = None,
         environment: dict[str,str] | None = None) -> str:
    if os.name == "nt" and len(subprocess.list2cmdline(command)) > 30000:
        raise VideoEditingError("RENDER_COMMAND_TOO_LARGE")
    callback_error: BaseException | None = None

    def check_status() -> None:
        nonlocal callback_error
        if check_continue:
            try:
                check_continue()
            except BaseException as exc:
                callback_error = exc
                raise

    try:
        check_status()
        deadline = time.monotonic() + timeout
        with subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False, env=environment,
        ) as process:
            try:
                while True:
                    check_status()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        _, stderr = process.communicate(timeout=min(.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                check_status()
            except BaseException:
                # Reap the encoder before its private working directory is removed.
                process.kill()
                process.communicate()
                raise
    except (OSError, subprocess.SubprocessError) as exc:
        # Permission/cancellation callbacks are not encoder failures, even when
        # their exception happens to inherit from a process/OS error class.
        if exc is callback_error:
            raise
        raise VideoEditingError("MEDIA_RENDER_PROCESS_FAILED") from exc
    if process.returncode != 0:
        # Decoder messages can contain private source paths. Keep them out of API errors.
        raise VideoEditingError("MEDIA_RENDER_FAILED")
    return stderr


def _number(value: float) -> str:
    exact = _fraction_time(value)
    rendered = Decimal(exact.numerator) / Decimal(exact.denominator)
    return format(rendered, ".12f").rstrip("0").rstrip(".") or "0"


def _frame_at(seconds: float) -> int:
    # Round shared boundaries once so adjacent clips cannot accumulate drift.
    return int(_fraction_time(seconds) * FRAME_RATE + Fraction(1, 2))


def _video_transition_plan(timeline: Timeline, clips: list[TimelineClip]) -> dict[int, dict[str, Any]]:
    """Resolve centred transitions using real handles, without rippling other edits."""
    assets = {asset.asset_id: asset for asset in timeline.assets}
    boundaries = {(left.clip_id, right.clip_id): index
                  for index, (left, right) in enumerate(zip(clips, clips[1:]), 1)}
    seen: set[int] = set()
    plan: dict[int, dict[str, Any]] = {}
    clip_ids = {clip.clip_id for clip in clips}
    for transition in timeline.transitions:
        if transition.from_clip not in clip_ids and transition.to_clip not in clip_ids:
            continue
        if transition.transition_type not in SUPPORTED_VIDEO_TRANSITION_TYPES:
            raise VideoEditingError("TRANSITION_RENDER_NOT_SUPPORTED")
        index = boundaries.get((transition.from_clip, transition.to_clip))
        if index is None:
            raise VideoEditingError("TRANSITION_REQUIRES_ADJACENT_VIDEO_CLIPS")
        if index in seen:
            raise VideoEditingError("TRANSITION_BOUNDARY_CONFLICT")
        seen.add(index)
        if transition.transition_type == "cut":
            continue
        if transition.duration_sec < 1 / FRAME_RATE:
            raise VideoEditingError("TRANSITION_SHORTER_THAN_OUTPUT_FRAME")
        frames = _frame_at(transition.duration_sec)
        before, after = frames // 2, frames - frames // 2
        left, right = clips[index - 1], clips[index]
        cut_frame = _frame_at(right.timeline_start)
        if cut_frame != _frame_at(left.timeline_start + left.duration_sec):
            raise VideoEditingError("TRANSITION_REQUIRES_CONTIGUOUS_VIDEO_CLIPS")
        start, end = cut_frame - before, cut_frame + after
        if (start < _frame_at(left.timeline_start)
                or end > _frame_at(right.timeline_start + right.duration_sec)):
            raise VideoEditingError("TRANSITION_WINDOW_EXCEEDS_CLIP")
        left_frames = cut_frame - _frame_at(left.timeline_start)
        outgoing_end = left.source_in + (left_frames + after) * left.playback_rate / FRAME_RATE
        left_asset = assets[left.media_asset_id]
        right_asset = assets[right.media_asset_id]
        if ((not left_asset.is_still_image
             and (left_asset.duration_sec is None or outgoing_end > left_asset.duration_sec))
                or (not right_asset.is_still_image
                    and right.source_in < before * right.playback_rate / FRAME_RATE)):
            raise VideoEditingError("TRANSITION_MEDIA_HANDLES_UNAVAILABLE")
        plan[index] = {
            "from_clip": left.clip_id, "to_clip": right.clip_id, "type": transition.transition_type,
            "requested_duration_sec": transition.duration_sec, "duration_frames": frames,
            "before_cut_frames": before, "after_cut_frames": after,
            "start_frame": start, "end_frame": end,
            "outgoing_extra_source_range": (None if left_asset.is_still_image else
                [left.source_in + left_frames * left.playback_rate / FRAME_RATE, outgoing_end]),
            "incoming_extra_source_range": (None if right_asset.is_still_image else
                [right.source_in - before * right.playback_rate / FRAME_RATE, right.source_in]),
            "static_frame_repeated": [clip_id for clip_id, still in (
                (left.clip_id, left_asset.is_still_image), (right.clip_id, right_asset.is_still_image),
            ) if still],
        }
    previous_end = -1
    for item in sorted(plan.values(), key=lambda item: item["start_frame"]):
        if item["start_frame"] < previous_end:
            raise VideoEditingError("TRANSITION_WINDOWS_OVERLAP")
        previous_end = item["end_frame"]
    return plan


def _verify_caption_glyphs(text: str) -> None:
    """Check the exact TTC face used by drawtext, without font fallback or new libraries."""
    required = {ord(char) for char in text if char != "\n"}
    supported: set[int] = set()
    try:
        data = CAPTION_FONT.read_bytes()
        face = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        table_count = struct.unpack_from(">H", data, face + 4)[0]
        records = [face + 12 + index * 16 for index in range(table_count)]
        cmap_record = next(pos for pos in records if data[pos:pos + 4] == b"cmap")
        cmap = struct.unpack_from(">I", data, cmap_record + 8)[0]
        for index in range(struct.unpack_from(">H", data, cmap + 2)[0]):
            platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + index * 8)
            if platform != 0 and (platform, encoding) not in {(3, 1), (3, 10)}:
                continue
            sub = cmap + offset
            form = struct.unpack_from(">H", data, sub)[0]
            if form == 12:
                count = struct.unpack_from(">I", data, sub + 12)[0]
                groups = [struct.unpack_from(">III", data, sub + 16 + i * 12) for i in range(count)]
                ends = [group[1] for group in groups]
                for code in required:
                    i = bisect_left(ends, code)
                    if i < count and groups[i][0] <= code and groups[i][2] + code - groups[i][0] > 0:
                        supported.add(code)
            elif form == 4:
                count = struct.unpack_from(">H", data, sub + 6)[0] // 2
                ends = struct.unpack_from(f">{count}H", data, sub + 14)
                starts = struct.unpack_from(f">{count}H", data, sub + 16 + count * 2)
                deltas = struct.unpack_from(f">{count}h", data, sub + 16 + count * 4)
                ranges_at = sub + 16 + count * 6
                ranges = struct.unpack_from(f">{count}H", data, ranges_at)
                for code in required:
                    i = bisect_left(ends, code)
                    if i >= count or code < starts[i]:
                        continue
                    glyph = (struct.unpack_from(">H", data, ranges_at + i * 2 + ranges[i] +
                                                2 * (code - starts[i]))[0] if ranges[i] else code)
                    if (not ranges[i] or glyph) and (glyph + deltas[i]) & 0xffff:
                        supported.add(code)
    except (OSError, struct.error, StopIteration, ValueError) as exc:
        raise VideoEditingError("CAPTION_FONT_UNAVAILABLE") from exc
    if required - supported:
        raise VideoEditingError("OVERLAY_GLYPH_UNAVAILABLE")


def _wide_caption_lines(text: str) -> list[str]:
    """Keep every character and prefer a space boundary within 48 base characters."""
    lines = []
    for original in text.split("\n"):
        clusters: list[str] = []
        for char in original:
            if clusters and unicodedata.category(char).startswith("M"):
                clusters[-1] += char
            else:
                clusters.append(char)
        while len(clusters) > 48:
            spaces = [index + 1 for index, cluster in enumerate(clusters[:48]) if cluster == " "]
            boundary = spaces[-1] if spaces else 48
            lines.append("".join(clusters[:boundary]))
            clusters = clusters[boundary:]
        lines.append("".join(clusters))
    return lines


def _measure_caption_box(ffmpeg: str, *, work: Path, filename: str,
                         font: str, check_continue: Callable[[], None] | None = None,
                         font_size: int = 35, line_spacing: int = 8,
                         width: int = 1280, height: int = 720) -> tuple[float, float]:
    """Fail closed if the actual installed drawtext cannot report stable dimensions."""
    report = _run([ffmpeg, "-v", "info", "-nostats", "-nostdin", "-filter_threads", "1",
                   "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30:d=0.04", "-vf",
                   f"drawtext={font}:textfile={filename}:expansion=none:reload=0:"
                   f"fontsize={font_size}:line_spacing={line_spacing}:x='print(text_w)':y='print(text_h)'",
                   "-frames:v", "1", "-f", "null", "-"], cwd=work, check_continue=check_continue)
    numbers = [float(value) for value in re.findall(
        r"^(?:\[Eval @ [^\]]+\] )?([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$",
        report, flags=re.MULTILINE)]
    if not numbers or len(numbers) % 3 or len(numbers) > 24:
        raise VideoEditingError("CAPTION_LAYOUT_MEASUREMENT_UNAVAILABLE")
    boxes = []
    for index in range(0, len(numbers), 3):
        width, height, width_again = numbers[index:index + 3]
        if (not all(math.isfinite(value) and value > 0 for value in (width, height, width_again))
                or width != width_again):
            raise VideoEditingError("CAPTION_LAYOUT_MEASUREMENT_UNAVAILABLE")
        boxes.append((width, height))
    if any(box != boxes[0] for box in boxes):
        raise VideoEditingError("CAPTION_LAYOUT_MEASUREMENT_UNAVAILABLE")
    return boxes[0]


def _display_overlay_text(overlay) -> str:
    if not isinstance(overlay.text, str) or len(overlay.text) > 1024:
        raise VideoEditingError("OVERLAY_TEXT_NOT_SUPPORTED")
    text = overlay.text.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(char).startswith("C") and char != "\n" for char in text):
        raise VideoEditingError("OVERLAY_TEXT_NOT_SUPPORTED")
    profile = overlay.layout_profile
    style = overlay.style
    columns_per_line = style.max_chars_per_line if style else (48 if profile is not None else 28)
    lines = []
    for original_line in text.split("\n"):
        line, columns = "", 0
        for char in original_line:
            is_mark = unicodedata.category(char).startswith("M")
            if columns == columns_per_line and not is_mark:
                lines.append(line)
                line, columns = "", 0
            line += char
            columns += int(not is_mark)
        lines.append(line)
    if profile is not None:
        lines = _wide_caption_lines(text)
    if len(lines) > (style.max_lines if style else 4):
        raise VideoEditingError("CAPTION_STYLE_LINE_COUNT_EXCEEDED" if style else "OVERLAY_LAYOUT_EXCEEDS_FOUR_LINES")
    return "\n".join(lines)


def _overlay_filters(timeline: Timeline, *, duration: float, width: int, height: int,
                     ffmpeg: str, work: Path,
                     check_continue: Callable[[], None] | None = None,
                     layout_readback: list[dict[str, Any]] | None = None) -> list[str]:
    if not timeline.overlays:
        return []
    if len(timeline.overlays) > 64:
        raise VideoEditingError("OVERLAY_COUNT_EXCEEDED")
    prepared = []
    previous_end: dict[str, float] = {}
    for overlay in sorted(timeline.overlays, key=lambda item: (item.start_sec, item.end_sec)):
        if check_continue:
            check_continue()
        if overlay.end_sec > duration:
            raise VideoEditingError("OVERLAY_EXTENDS_BEYOND_VIDEO")
        start = math.ceil(round(overlay.start_sec * FRAME_RATE, 6))
        end = min(math.ceil(round(overlay.end_sec * FRAME_RATE, 6)), _frame_at(duration))
        if start >= end:
            raise VideoEditingError("OVERLAY_SHORTER_THAN_OUTPUT_FRAME")
        if overlay.start_sec < previous_end.get(overlay.kind, 0):
            raise VideoEditingError("OVERLAY_SAME_KIND_OVERLAP_NOT_SUPPORTED")
        previous_end[overlay.kind] = overlay.end_sec
        text = _display_overlay_text(overlay)
        profile = overlay.layout_profile
        if profile is not None and (profile != "literal-wide-720p-v2" or overlay.kind != "caption"):
            raise VideoEditingError("OVERLAY_LAYOUT_PROFILE_NOT_SUPPORTED")
        if profile is not None and (width, height) != (1280, 720):
            raise VideoEditingError("CAPTION_LAYOUT_REQUIRES_1280_720")
        style = overlay.style
        prepared.append((overlay.kind, start, end, text, profile, overlay.overlay_id,style))
    if not CAPTION_FONT.is_file():
        raise VideoEditingError("CAPTION_FONT_UNAVAILABLE")
    _verify_caption_glyphs("\n".join(item[3] for item in prepared))
    # Private working copy is never exported. A constant filename avoids escaping
    # drive letters, spaces or filter metacharacters in installed host paths.
    shutil.copyfile(CAPTION_FONT, work / "caption-font.ttc")
    font = "fontfile=caption-font.ttc"
    cancelled: list[BaseException] = []
    def check_probe() -> None:
        if check_continue:
            try:
                check_continue()
            except BaseException as exc:
                cancelled.append(exc)
                raise
    try:
        _run([ffmpeg, "-v", "error", "-nostdin", "-filter_threads", "1", "-f", "lavfi", "-i",
              "color=c=black:s=32x32:r=30:d=0.04", "-vf",
              f"drawtext={font}:text=中文:expansion=none", "-frames:v", "1", "-f", "null", "-"],
             cwd=work, check_continue=check_probe)
    except Exception as exc:
        if cancelled:
            raise cancelled[0]
        if isinstance(exc, VideoEditingError) and str(exc) in {"MEDIA_RENDER_FAILED", "MEDIA_RENDER_PROCESS_FAILED"}:
            raise VideoEditingError("CAPTION_RENDER_UNAVAILABLE") from exc
        raise
    filters = []
    for index, (kind, start, end, text, profile, overlay_id,style) in enumerate(prepared):
        size = style.font_size if style else max(16,width//36)
        margin = math.ceil(height*style.safe_margin_ratio) if style else max(16,height//24)
        horizontal_margin = math.ceil(width*style.safe_margin_ratio) if style else margin
        if check_continue:
            check_continue()
        filename = f"caption-{index:03d}.txt"
        with (work / filename).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if profile is not None or style is not None:
            measured_options = {"font_size":size,"line_spacing":size//4,"width":width,"height":height} if style else {}
            box_width, box_height = _measure_caption_box(ffmpeg, work=work, filename=filename,
                                                          font=font, check_continue=check_continue,**measured_options)
            if box_width + 16 > width - 2 * horizontal_margin or box_height + 16 > height * .3:
                raise VideoEditingError("CAPTION_LAYOUT_EXCEEDS_PIXEL_BOUNDS")
            if layout_readback is not None:
                layout_readback.append({"overlay_id": overlay_id, "profile": profile or "bounded-caption-style-v1",
                                        **({"style":style.to_dict(),"display_text":text} if style else {}),
                                        "text_width_px": box_width, "text_height_px": box_height,
                                        "box_border_px": 8, "horizontal_margin_px": horizontal_margin,
                                        "maximum_box_height_ratio": .3})
        bottom = style.position == "bottom" if style else kind == "caption"
        y = f"h-text_h-{margin}" if bottom else str(margin)
        color = "0x"+style.color[1:] if style else "white"
        background = f"0x{style.background_color[1:]}@{style.background_opacity}" if style else "black@0.6"
        filters.append(f"drawtext={font}:textfile={filename}:expansion=none:reload=0:"
                       f"fontsize={size}:fontcolor={color}:box=1:boxcolor={background}:boxborderw=8:"
                       f"line_spacing={size // 4}:x=(w-text_w)/2:y={y}:"
                       f"enable='gte(n,{start})*lt(n,{end})'")
    return filters


def _stream_origin(asset, codec_type: str, stream_ordinal: int) -> Fraction:
    return media_stream_origin(asset, codec_type, stream_ordinal)


def _stream_metadata_by_ordinal(asset, codec_type: str, stream_ordinal: int) -> dict[str, Any] | None:
    streams = [item for item in asset.stream_metadata if item.get("codec_type") == codec_type]
    return streams[stream_ordinal] if 0 <= stream_ordinal < len(streams) else None


def _rational_json(value: Fraction) -> dict[str, int]:
    exact = _fraction_time(value)
    return {"num": exact.numerator, "den": exact.denominator}


def _format_origin(asset) -> Fraction:
    return media_format_origin(asset)


def _stream_input_origin(asset, codec_type: str, stream_ordinal: int) -> Fraction:
    return media_stream_input_origin(asset, codec_type, stream_ordinal)


def _audio_source_window(asset, clip: TimelineClip, track_kind: str) -> tuple[Fraction, Fraction, Fraction]:
    """Map a clip window onto FFmpeg's container-relative selected-audio clock."""
    if not asset.stream_metadata:
        raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID")
    audio_ordinal = 0 if clip.audio_stream_index is None else clip.audio_stream_index
    audio_origin = _stream_input_origin(asset, "audio", audio_ordinal)
    if track_kind == "video" and clip.audio_role == "original":
        video_ordinal = 0 if clip.video_stream_index is None else clip.video_stream_index
        video_origin = _stream_input_origin(asset, "video", video_ordinal)
        desired_start = video_origin + clip.source_in
        desired_end = video_origin + clip.source_out
    else:
        desired_start = audio_origin + clip.source_in
        desired_end = audio_origin + clip.source_out
    source_start = max(audio_origin, desired_start)
    source_end = max(source_start, desired_end)
    initial_silence = max(Fraction(0), audio_origin - desired_start) / clip.playback_rate
    return source_start, source_end, initial_silence


def _audio_filter(
    clip: TimelineClip,
    *,
    source_start: Fraction,
    source_end: Fraction,
    initial_silence: Fraction,
    track_gain: float = 1.0,
    track_pan: float = 0.0,
    send_gain: float = 1.0,
) -> str:
    duration = clip.duration_sec
    mix = clip.audio_mix
    filters = [
        f"atrim=start={_number(source_start)}:end={_number(source_end)}",
        # The clip clock is relative to the selected video stream. Keep any
        # audio lead/lag as a trim or initial silence before timeline placement.
        "asetpts=PTS-STARTPTS", f"aresample={SAMPLE_RATE}:first_pts=0",
        f"aformat=sample_fmts=fltp:channel_layouts={_audio_layout()}",
    ]
    if clip.playback_rate != 1:
        rate = clip.playback_rate
        tempos = []
        while rate > 2:
            tempos.append("atempo=2")
            rate /= 2
        while rate < .5:
            tempos.append("atempo=0.5")
            rate *= 2
        tempos.append(f"atempo={_number(rate)}")
        filters.extend(tempos)
    if initial_silence:
        filters.append(f"adelay={round(initial_silence * SAMPLE_RATE)}S:all=1")
    filters.append(f"volume={_number(mix.volume * track_gain * send_gain)}:precision=double")
    if track_pan:
        filters.append(_audio_pan_filter(track_pan))
    if mix.fade_in_sec:
        filters.append(f"afade=t=in:st=0:d={_number(mix.fade_in_sec)}")
    if mix.fade_out_sec:
        filters.append(f"afade=t=out:st={_number(duration - mix.fade_out_sec)}:d={_number(mix.fade_out_sec)}")
    filters.extend([
        f"apad=whole_len={round(duration * SAMPLE_RATE)}", f"atrim=end_sample={round(duration * SAMPLE_RATE)}",
        "asetpts=N/SR/TB",
        f"adelay={round(clip.timeline_start * SAMPLE_RATE)}S:all=1",
        "asetpts=N/SR/TB",
    ])
    return ",".join(filters)


def _verify_sources(timeline: Timeline) -> None:
    used = {clip.media_asset_id for track in timeline.tracks for clip in track.clips}
    assets = {asset.asset_id: asset for asset in timeline.assets}
    for asset_id in used:
        asset = assets[asset_id]
        try:
            path = _verified_asset_path(asset)
            current = fingerprint_local_media(
                str(path), source_material_id=asset.source_material_id, display_filename=asset.filename
            )
        except (OSError, ValueError) as exc:
            raise VideoEditingError("SOURCE_MEDIA_NOT_READABLE") from exc
        if (current.sha256, current.duration_sec, current.has_video, current.audio_stream_count) != (
            asset.sha256, asset.duration_sec, asset.has_video, asset.audio_stream_count
        ):
            raise VideoEditingError("SOURCE_MEDIA_CHANGED")


def _render_audio(timeline: Timeline, *, duration: float, ffmpeg: str, work: Path,
                  check_continue: Callable[[], None] | None = None,
                  alignment_readback: list[dict[str, Any]] | None = None) -> Path | None:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    selected = [clip for track in timeline.audible_tracks() for clip in track.clips
                if clip.audio_role != "muted" and assets[clip.media_asset_id].audio_stream_count]
    source_windows = {}
    for track in timeline.audible_tracks():
        for clip in track.clips:
            asset = assets[clip.media_asset_id]
            if clip.audio_role == "muted" or not asset.audio_stream_count:
                continue
            audio_index = 0 if clip.audio_stream_index is None else clip.audio_stream_index
            source_window = _audio_source_window(asset, clip, track.kind)
            source_windows[clip.clip_id] = source_window
            if alignment_readback is not None:
                audio_stream = _stream_metadata_by_ordinal(asset, "audio", audio_index)
                video_stream_index = 0 if clip.video_stream_index is None else clip.video_stream_index
                video_stream = (_stream_metadata_by_ordinal(asset, "video", video_stream_index)
                                if track.kind == "video" else None)
                source_start, source_end, initial_silence = source_window
                alignment_readback.append({
                    "clip_id": clip.clip_id,
                    "track_id": track.track_id,
                    "track_kind": track.kind,
                    "source_material_id": asset.source_material_id,
                    "source_sha256": asset.sha256,
                    "source_in_sec": _rational_json(clip.source_in),
                    "source_out_sec": _rational_json(clip.source_out),
                    "playback_rate": _rational_json(clip.playback_rate),
                    "audio_stream_index": audio_index,
                    "audio_stream_start_pts": (audio_stream or {}).get("start_pts"),
                    "audio_stream_time_base": (audio_stream or {}).get("time_base"),
                    "audio_stream_start_time": (audio_stream or {}).get("start_time"),
                    "video_stream_index": video_stream_index if video_stream is not None else None,
                    "video_stream_start_pts": (video_stream or {}).get("start_pts"),
                    "video_stream_time_base": (video_stream or {}).get("time_base"),
                    "video_stream_start_time": (video_stream or {}).get("start_time"),
                    "container_start_time": asset.format_metadata.get("start_time"),
                    "ffmpeg_input_start_sec": _rational_json(source_start),
                    "ffmpeg_input_end_sec": _rational_json(source_end),
                    "timeline_initial_silence_sec": _rational_json(initial_silence),
                    "clock_basis": ("selected_video_and_audio_stream_pts"
                                    if video_stream is not None and audio_stream is not None
                                    else "selected_audio_stream_pts" if audio_stream is not None
                                    else "source_stream_start_not_verified"),
                })
    if not selected and any(clip.audio_role != "muted" and assets[clip.media_asset_id].audio_stream_count
                            for track in timeline.tracks for clip in track.clips):
        output = work / 'audio.wav'
        samples = round(duration * SAMPLE_RATE)
        _run([ffmpeg,'-v','error','-nostdin','-n','-f','lavfi','-i',
              f'anullsrc=r={SAMPLE_RATE}:cl={_audio_layout()}','-af',f'atrim=end_sample={samples},asetpts=N/SR/TB',
              '-ar',str(SAMPLE_RATE),'-ac',str(AUDIO_CHANNELS),'-c:a','pcm_s24le','-rf64','auto','-f','wav',str(output)],
             cwd=work,timeout=max(60,duration*4),**({'check_continue':check_continue} if check_continue else {}))
        return output
    routed_audio = bool(timeline.audio_buses) or any(
        track.gain != 1 or track.pan != 0 or track.audio_bus_sends
        for track in timeline.tracks
    )
    if routed_audio:
        return _render_routed_audio(
            timeline, selected, assets, duration=duration, ffmpeg=ffmpeg,
            work=work, source_windows=source_windows, check_continue=check_continue,
        )
    if len(selected) > RENDER_INPUT_BATCH_SIZE:
        return _render_batched_audio(selected, assets, duration=duration, ffmpeg=ffmpeg,
                                     work=work, source_windows=source_windows,
                                     check_continue=check_continue)
    command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_threads", "1",
               "-filter_complex_threads", "1", "-threads", "1"]
    graph: list[str] = []
    labels: list[str] = []
    voice_labels: list[str] = []
    ducked_labels: list[str] = []
    sample_count = round(duration * SAMPLE_RATE)
    input_index = 0
    voice_key_available = any(clip.audio_role == 'voiceover' and clip.audio_mix.volume > 0 for clip in selected)
    for track in timeline.audible_tracks():
        for clip in track.clips:
            asset = assets[clip.media_asset_id]
            if clip.audio_role == "muted" or asset.audio_stream_count == 0:
                continue
            stream = clip.audio_stream_index
            if stream is None:
                if asset.audio_stream_count != 1:
                    raise VideoEditingError("AUDIO_STREAM_SELECTION_REQUIRED")
                stream = 0
            command += ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                        "-i", asset.path]
            label = f"clip{input_index}"
            source_start, source_end, initial_silence = source_windows[clip.clip_id]
            # Match frame boundaries across split sidechain consumers. Differently
            # sized decoded/delayed frames can lose a second BGM tail in FFmpeg;
            # final output is still trimmed to the exact requested sample count.
            graph.append(f"[{input_index}:a:{stream}]" + _audio_filter(
                clip,
                source_start=source_start,
                source_end=source_end,
                initial_silence=initial_silence,
            ) +
                         f",apad=whole_len={sample_count},atrim=end_sample={sample_count},asetnsamples=n=1024:p=1[{label}]")
            labels.append(label)
            if clip.audio_role == "voiceover" and clip.audio_mix.volume > 0:
                voice_labels.append(label)
            if clip.audio_mix.ducking and voice_key_available:
                ducked_labels.append(label)
            input_index += 1

    if not labels:
        return None

    if ducked_labels:
        # Split explicit voiceover stems only. Original sound can contain music;
        # treating it as detected speech would misrepresent source separation.
        for label in voice_labels:
            graph.append(f"[{label}]asplit=2[{label}mix][{label}key]")
            labels[labels.index(label)] = f"{label}mix"
        graph.append("".join(f"[{label}key]" for label in voice_labels) +
                     f"amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0:duration=longest[voicekey]")
        graph.append(f"[voicekey]asplit={len(ducked_labels)}" +
                     "".join(f"[key{index}]" for index in range(len(ducked_labels))))
        for index, label in enumerate(ducked_labels):
            graph.append(f"[{label}][key{index}]sidechaincompress=threshold=0.025:ratio=8:"
                         f"attack=10:release=250:makeup=1:link=maximum:detection=rms[{label}ducked]")
            labels[labels.index(label)] = f"{label}ducked"

    if len(labels) == 1:
        combined = labels[0]
    else:
        combined = "mixed"
        graph.append("".join(f"[{label}]" for label in labels) +
                     f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0:duration=longest[{combined}]")
    graph.append(f"[{combined}]alimiter=limit={MIX_SAMPLE_PEAK_LIMIT}:attack=5:release=50:level=0:latency=1,"
                 f"apad=whole_len={sample_count},atrim=end_sample={sample_count},asetpts=N/SR/TB[out]")

    output = work / "audio.wav"
    _run(command + ["-filter_complex", ";".join(graph), "-map", "[out]", "-ar", str(SAMPLE_RATE),
                    "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s24le", "-threads:a", "1", "-rf64", "auto", "-f", "wav", str(output)],
         cwd=work, timeout=max(60, duration * 4), **({"check_continue": check_continue} if check_continue else {}))
    return output


def _render_batched_audio(clips, assets, *, duration, ffmpeg, work, source_windows=None, check_continue=None,
                          clip_mix_options=None, apply_limiter=True):
    options = {"check_continue": check_continue} if check_continue else {}
    counter = 0

    def encode(inputs, filters, output_label, sample_count):
        nonlocal counter
        if check_continue:
            check_continue()
        if shutil.disk_usage(work).free < sample_count * 8 + 16 * 1024 * 1024:
            raise VideoEditingError("MEDIA_RENDER_STORAGE_UNAVAILABLE")
        counter += 1
        output = work / f"audio-stem-{counter:06d}.wav"
        command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_complex_threads", "1",
                   "-threads", "1"]
        for path in inputs:
            command += ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                        "-i", str(path)]
        _run(command + ["-filter_complex", ";".join(filters), "-map", f"[{output_label}]",
                        "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_f32le", "-rf64", "auto",
                        "-t", _number(sample_count / SAMPLE_RATE), str(output)],
             cwd=work, timeout=max(60, sample_count / SAMPLE_RATE * 4), **options)
        return output

    def mix_batches(stems):
        while len(stems) > 1:
            reduced = []
            for start in range(0, len(stems), RENDER_INPUT_BATCH_SIZE):
                group = stems[start:start + RENDER_INPUT_BATCH_SIZE]
                if len(group) == 1:
                    reduced.extend(group)
                    continue
                begin, end = min(s[1] for s in group), max(s[2] for s in group)
                graph = [f"[{i}:a:0]asetpts=N/SR/TB,adelay={s[1] - begin}S:all=1[a{i}]"
                         for i, s in enumerate(group)]
                graph.append("".join(f"[a{i}]" for i in range(len(group))) +
                             f"amix=inputs={len(group)}:normalize=0:dropout_transition=0:duration=longest,"
                             f"apad=whole_len={end - begin},atrim=end_sample={end - begin}[mixed]")
                output = encode([s[0] for s in group], graph, "mixed", end - begin)
                reduced.append((output, begin, end))
                for path, _, _ in group:
                    path.unlink()
            stems = reduced
        return stems[0] if stems else None

    stems_by_role = {}
    categories = {"voice": [], "ducked": [], "other": []}
    voice_key_available = any(clip.audio_role == "voiceover" and clip.audio_mix.volume > 0
                              for clip in clips)
    for clip in clips:
        role = ("ducked" if clip.audio_mix.ducking and voice_key_available else
                "voice" if clip.audio_role == "voiceover" and clip.audio_mix.volume > 0 else "other")
        categories[role].append(clip)
    for role, members in categories.items():
        levels = [[]]
        members.sort(key=lambda clip: (clip.timeline_start, clip.clip_id))
        for start in range(0, len(members), RENDER_INPUT_BATCH_SIZE):
            group = members[start:start + RENDER_INPUT_BATCH_SIZE]
            begin = min(round(clip.timeline_start * SAMPLE_RATE) for clip in group)
            end = max(round(clip.timeline_start * SAMPLE_RATE) + round(clip.duration_sec * SAMPLE_RATE)
                      for clip in group)
            graph, paths = [], []
            for i, clip in enumerate(group):
                asset = assets[clip.media_asset_id]
                stream = clip.audio_stream_index
                if stream is None:
                    if asset.audio_stream_count != 1:
                        raise VideoEditingError("AUDIO_STREAM_SELECTION_REQUIRED")
                    stream = 0
                shifted = replace(clip, timeline_start=(round(clip.timeline_start * SAMPLE_RATE) - begin) / SAMPLE_RATE)
                paths.append(asset.path)
                source_window = (source_windows or {}).get(clip.clip_id)
                if source_window is None:
                    source_window = _audio_source_window(asset, clip, "audio")
                source_start, source_end, initial_silence = source_window
                mix_options = (clip_mix_options or {}).get(clip.clip_id, {})
                graph.append(f"[{i}:a:{stream}]" + _audio_filter(
                    shifted,
                    source_start=source_start,
                    source_end=source_end,
                    initial_silence=initial_silence,
                    **mix_options,
                ) + f"[a{i}]")
            graph.append("".join(f"[a{i}]" for i in range(len(group))) +
                         f"amix=inputs={len(group)}:normalize=0:dropout_transition=0:duration=longest,"
                         f"apad=whole_len={end - begin},atrim=end_sample={end - begin}[mixed]")
            stem = (encode(paths, graph, "mixed", end - begin), begin, end)
            level = 0
            while True:
                if level == len(levels):
                    levels.append([])
                levels[level].append(stem)
                if len(levels[level]) < RENDER_INPUT_BATCH_SIZE:
                    break
                stem = mix_batches(levels[level])
                levels[level] = []
                level += 1
        stem = mix_batches([stem for level in levels for stem in level])
        if stem:
            stems_by_role[role] = stem
    if not stems_by_role:
        return None
    sample_count = round(duration * SAMPLE_RATE)
    command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_complex_threads", "1"]
    graph, labels = [], []
    for index, (role, (path, begin, _)) in enumerate(stems_by_role.items()):
        command += ["-protocol_whitelist", "file", "-format_whitelist", "wav", "-i", str(path)]
        graph.append(f"[{index}:a:0]asetpts=N/SR/TB,adelay={begin}S:all=1,"
                     f"apad=whole_len={sample_count},atrim=end_sample={sample_count},"
                     f"asetnsamples=n=1024:p=1[{role}]")
        labels.append(role)
    if "ducked" in stems_by_role:
        if "voice" not in stems_by_role:
            raise VideoEditingError("AUDIO_DUCKING_REQUIRES_VOICEOVER")
        graph.append("[voice]asplit=2[voicemix][voicekey]")
        graph.append("[ducked][voicekey]sidechaincompress=threshold=0.025:ratio=8:"
                     "attack=10:release=250:makeup=1:link=maximum:detection=rms[duckedmix]")
        labels = ["voicemix" if x == "voice" else "duckedmix" if x == "ducked" else x for x in labels]
    limiter = (f"alimiter=limit={MIX_SAMPLE_PEAK_LIMIT}:attack=5:release=50:level=0:latency=1,"
               if apply_limiter else "")
    graph.append("".join(f"[{label}]" for label in labels) +
                 f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0:duration=longest,"
                 f"{limiter}apad=whole_len={sample_count},atrim=end_sample={sample_count},asetpts=N/SR/TB[out]")
    output = work / "audio.wav"
    if shutil.disk_usage(work).free < sample_count * (8 if not apply_limiter else 6) + 16 * 1024 * 1024:
        raise VideoEditingError("MEDIA_RENDER_STORAGE_UNAVAILABLE")
    _run(command + ["-filter_complex", ";".join(graph), "-map", "[out]", "-ar", str(SAMPLE_RATE),
                    "-ac", str(AUDIO_CHANNELS), "-c:a",
                    "pcm_f32le" if not apply_limiter else "pcm_s24le", "-rf64", "auto", str(output)],
         cwd=work, timeout=max(60, duration * 4), **options)
    for path, _, _ in stems_by_role.values():
        path.unlink()
    return output


def _audio_bus_effect_filters(effect: AudioBusEffect) -> list[str]:
    if not effect.enabled:
        return []
    parameters = effect.parameters
    if effect.kind in {"highpass", "lowpass"}:
        stages = []
        remaining = parameters["poles"]
        while remaining > 0:
            poles = min(2, remaining)
            stages.append(
                f"{effect.kind}=f={_number(parameters['frequency_hz'])}:poles={poles}"
            )
            remaining -= poles
        return stages
    if effect.kind == "equalizer":
        return [
            f"equalizer=f={_number(parameters['frequency_hz'])}:width_type=q:"
            f"width={_number(parameters['q'])}:gain={_number(parameters['gain_db'])}"
        ]
    if effect.kind == "compressor":
        threshold = Fraction(str(10 ** (parameters["threshold_db"] / 20))).limit_denominator(10**9)
        makeup = Fraction(str(10 ** (parameters["makeup_db"] / 20))).limit_denominator(10**9)
        return [
            f"acompressor=threshold={_number(threshold)}:ratio={_number(parameters['ratio'])}:"
            f"attack={_number(parameters['attack_ms'])}:release={_number(parameters['release_ms'])}:"
            f"makeup={_number(makeup)}:knee={_number(parameters['knee_db'])}:link=maximum:detection=rms"
        ]
    if effect.kind == "limiter":
        limit = Fraction(str(10 ** (parameters["limit_dbfs"] / 20))).limit_denominator(10**9)
        return [
            f"alimiter=limit={_number(limit)}:attack={_number(parameters['attack_ms'])}:"
            f"release={_number(parameters['release_ms'])}:level=0:latency=1"
        ]
    raise VideoEditingError("AUDIO_BUS_EFFECT_NOT_SUPPORTED")


def _audio_bus_processing_filters(bus: AudioBus | None) -> list[str]:
    if bus is None:
        return []
    filters = []
    if bus.gain != 1:
        filters.append(f"volume={_number(bus.gain)}:precision=double")
    if bus.pan:
        filters.append(_audio_pan_filter(bus.pan))
    for effect in bus.effects:
        filters.extend(_audio_bus_effect_filters(effect))
    return filters


def _audio_pan_filter(pan: float) -> str:
    if AUDIO_CHANNELS == 1:
        raise VideoEditingError("AUDIO_PAN_REQUIRES_STEREO_OR_MULTICHANNEL_OUTPUT")
    if AUDIO_CHANNELS == 2:
        return f"stereotools=balance_out={_number(pan)}:bmode_out=power"
    if AUDIO_CHANNELS == 6:
        channels = ("FL", "FR", "FC", "LFE", "BL", "BR")
    elif AUDIO_CHANNELS == 8:
        channels = ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR")
    else:
        raise VideoEditingError("AUDIO_PAN_CHANNEL_LAYOUT_UNSUPPORTED")
    left_gain = 1 - max(0, pan)
    right_gain = 1 + min(0, pan)
    left_channels = {"FL", "BL", "SL"}
    right_channels = {"FR", "BR", "SR"}
    mapping = []
    for channel in channels:
        gain = left_gain if channel in left_channels else right_gain if channel in right_channels else 1
        mapping.append(f"{channel}={_number(gain)}*{channel}" if gain != 1 else f"{channel}={channel}")
    return f"pan={_audio_layout()}|" + "|".join(mapping)


def _render_routed_audio(
    timeline: Timeline,
    selected: list[TimelineClip],
    assets: dict[str, Any],
    *,
    duration: float,
    ffmpeg: str,
    work: Path,
    source_windows: dict[str, tuple[Fraction, Fraction, Fraction]],
    check_continue: Callable[[], None] | None = None,
) -> Path | None:
    """Render track sends into group buses, then the master bus, at exact programme length."""
    tracks_by_clip = {
        clip.clip_id: track
        for track in timeline.audible_tracks()
        for clip in track.clips
        if clip in selected
    }
    if set(tracks_by_clip) != {clip.clip_id for clip in selected}:
        raise VideoEditingError("AUDIO_TRACK_ROUTING_SOURCE_MISMATCH")

    voice_clips = [clip for clip in selected
                   if clip.audio_role == "voiceover" and clip.audio_mix.volume > 0
                   and tracks_by_clip[clip.clip_id].gain > 0]
    has_voice_key = bool(voice_clips)
    grouped: dict[str, dict[str, list[TimelineClip]]] = {}
    mix_options: dict[str, dict[str, dict[str, float]]] = {}
    for clip in selected:
        track = tracks_by_clip[clip.clip_id]
        role = ("ducked" if clip.audio_mix.ducking and has_voice_key else
                "voice" if clip in voice_clips else "other")
        sends = track.audio_bus_sends or (AudioBusSend("master"),)
        for send in sends:
            grouped.setdefault(send.bus_id, {}).setdefault(role, []).append(clip)
            mix_options.setdefault(send.bus_id, {})[clip.clip_id] = {
                "track_gain": track.gain,
                "track_pan": track.pan,
                "send_gain": send.gain,
            }

    bus_catalog = {bus.bus_id: bus for bus in timeline.audio_buses}
    stem_groups: dict[tuple[str, str], Path] = {}

    def render_group(bus_id: str, role: str, clips: list[TimelineClip]):
        if check_continue:
            check_continue()
        subdir = work / f"bus-stem-{len(stem_groups):04d}"
        subdir.mkdir(parents=True, exist_ok=False)
        result = _render_batched_audio(
            clips, assets, duration=duration, ffmpeg=ffmpeg, work=subdir,
            source_windows=source_windows,
            check_continue=check_continue,
            clip_mix_options=mix_options.get(bus_id, {}),
            apply_limiter=False,
        )
        if result is not None:
            stem_groups[(bus_id, role)] = result

    # Each group stem is already aligned to the programme start and padded to
    # its exact sample count by _render_batched_audio.
    for bus_id, roles in grouped.items():
        for role in ("voice", "ducked", "other"):
            clips = roles.get(role)
            if clips:
                render_group(bus_id, role, clips)

    voice_key_stem = None
    if has_voice_key:
        key_work = work / "voice-sidechain"
        key_work.mkdir(parents=True, exist_ok=False)
        key_options = {
            clip.clip_id: {
                "track_gain": tracks_by_clip[clip.clip_id].gain,
                "track_pan": tracks_by_clip[clip.clip_id].pan,
            }
            for clip in voice_clips
        }
        voice_key_stem = _render_batched_audio(
            voice_clips, assets, duration=duration, ffmpeg=ffmpeg, work=key_work,
            source_windows=source_windows,
            check_continue=check_continue,
            clip_mix_options=key_options,
            apply_limiter=False,
        )

    if not stem_groups:
        return None

    command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_complex_threads", "1"]
    graph: list[str] = []
    sample_count = round(duration * SAMPLE_RATE)
    input_index = 0
    role_labels: dict[tuple[str, str], str] = {}

    def add_stem(key: tuple[str, str], path: Path) -> str:
        nonlocal input_index
        command.extend(["-protocol_whitelist", "file", "-format_whitelist", "wav", "-i", str(path)])
        label = f"route{input_index}"
        graph.append(
            f"[{input_index}:a:0]asetpts=N/SR/TB,"
            f"apad=whole_len={sample_count},atrim=end_sample={sample_count},asetnsamples=n=1024:p=1[{label}]"
        )
        input_index += 1
        role_labels[key] = label
        return label

    for key, stem in stem_groups.items():
        add_stem(key, stem)

    ducked_buses = [bus_id for bus_id, roles in grouped.items()
                    if "ducked" in roles and (bus_id, "ducked") in role_labels]
    if ducked_buses:
        if voice_key_stem is None:
            raise VideoEditingError("AUDIO_DUCKING_REQUIRES_VOICEOVER")
        key_label = add_stem(("__sidechain__", "voice"), voice_key_stem)
        graph.append(f"[{key_label}]asplit={len(ducked_buses)}" +
                     "".join(f"[side{index}]" for index in range(len(ducked_buses))))
        for index, bus_id in enumerate(ducked_buses):
            ducked_label = role_labels[(bus_id, "ducked")]
            output_label = f"ducked{index}"
            graph.append(
                f"[{ducked_label}][side{index}]sidechaincompress=threshold=0.025:ratio=8:"
                f"attack=10:release=250:makeup=1:link=maximum:detection=rms[{output_label}]"
            )
            role_labels[(bus_id, "ducked")] = output_label

    master_inputs: list[str] = []
    for bus_id, roles in grouped.items():
        labels = [role_labels[(bus_id, role)] for role in ("voice", "ducked", "other")
                  if (bus_id, role) in role_labels]
        if not labels:
            continue
        if bus_id == "master":
            master_inputs.extend(labels)
            continue
        bus_input = labels[0]
        if len(labels) > 1:
            bus_input = f"busmix{len(master_inputs)}"
            graph.append("".join(f"[{label}]" for label in labels) +
                         f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0:duration=longest[{bus_input}]")
        filters = _audio_bus_processing_filters(bus_catalog.get(bus_id))
        if filters:
            bus_output = f"busout{len(master_inputs)}"
            graph.append(f"[{bus_input}]{','.join(filters)}[{bus_output}]")
        else:
            bus_output = bus_input
        master_inputs.append(bus_output)

    if not master_inputs:
        raise VideoEditingError("AUDIO_BUS_MASTER_INPUT_MISSING")
    master_input = master_inputs[0]
    if len(master_inputs) > 1:
        master_input = "mastermix"
        graph.append("".join(f"[{label}]" for label in master_inputs) +
                     f"amix=inputs={len(master_inputs)}:normalize=0:dropout_transition=0:duration=longest[{master_input}]")
    master_filters = _audio_bus_processing_filters(bus_catalog.get("master"))
    master_filters.append(
        f"alimiter=limit={MIX_SAMPLE_PEAK_LIMIT}:attack=5:release=50:level=0:latency=1"
    )
    master_filters.extend([
        f"apad=whole_len={sample_count}",
        f"atrim=end_sample={sample_count}",
        "asetpts=N/SR/TB",
    ])
    graph.append(f"[{master_input}]{','.join(master_filters)}[out]")
    output = work / "audio.wav"
    if shutil.disk_usage(work).free < sample_count * 6 + 16 * 1024 * 1024:
        raise VideoEditingError("MEDIA_RENDER_STORAGE_UNAVAILABLE")
    _run(command + ["-filter_complex", ";".join(graph), "-map", "[out]", "-ar", str(SAMPLE_RATE),
                    "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s24le", "-threads:a", "1", "-rf64", "auto",
                    "-f", "wav", str(output)],
         cwd=work, timeout=max(60, duration * 4),
         **({"check_continue": check_continue} if check_continue else {}))
    return output


def _programme_timeout(duration, minimum=60.0):
    if duration is None:
        return minimum
    if (type(duration) not in (int, float, Fraction) or not math.isfinite(duration)
            or not 0 < duration <= MAX_EDIT_TIME_SEC):
        raise VideoEditingError("TIMELINE_DURATION_OUT_OF_RANGE")
    return max(minimum, float(duration) * 4.0)


def _probe_json(command, *, cwd, duration, check_continue=None,
                error_code="RENDER_OUTPUT_VERIFICATION_FAILED"):
    callback_error = None
    def check_status():
        nonlocal callback_error
        if check_continue:
            try:
                check_continue()
            except BaseException as exc:
                callback_error = exc
                raise
    try:
        check_status()
        deadline = time.monotonic() + _programme_timeout(duration, 30.0)
        with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace", shell=False) as process:
            try:
                while True:
                    check_status()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, _programme_timeout(duration, 30.0))
                    try:
                        stdout, _ = process.communicate(timeout=min(.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                check_status()
            except BaseException:
                process.kill()
                process.communicate()
                raise
    except (OSError, subprocess.SubprocessError) as exc:
        if exc is callback_error:
            raise
        raise VideoEditingError(error_code) from exc
    if process.returncode != 0:
        raise VideoEditingError(error_code)
    try:
        return json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise VideoEditingError(error_code) from exc


def _probe_output(path: Path, ffprobe: str, *, output_kind: str, duration: float,
                  has_audio: bool, preview: bool = False,
                  expected_video_signal: dict[str, Any] | None = None,
                  expected_width: int | None = None, expected_height: int | None = None,
                  check_continue: Callable[[], None] | None = None) -> dict[str, Any]:
    probe = _probe_json(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", "mov,wav",
         "-count_frames", "-show_entries",
         "stream=codec_type,codec_name,width,height,start_time,nb_read_frames,avg_frame_rate,sample_rate,channels,pix_fmt,bits_per_raw_sample,color_range,color_space,color_transfer,color_primaries:format=duration,format_name",
         "-of", "json", "-i", str(path)], cwd=path.parent, duration=duration, check_continue=check_continue,
    )
    try:
        streams = probe["streams"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        actual = float(probe["format"]["duration"])
        format_name = str(probe["format"]["format_name"])
        starts = [float(stream.get("start_time", 0)) for stream in audio + video]
        frames = int(video[0]["nb_read_frames"]) if video else 0
        video_width = int(video[0]["width"]) if video else None
        video_height = int(video[0]["height"]) if video else None
        video_frame_rate = Fraction(video[0]["avg_frame_rate"]) if video else None
        video_bit_depth = _video_bit_depth(video[0]) if video else None
        video_signal_readback = ({
            "pixel_format": video[0].get("pix_fmt"),
            "bit_depth": video_bit_depth,
            "color_range": video[0].get("color_range"),
            "color_space": video[0].get("color_space"),
            "color_transfer": video[0].get("color_transfer"),
            "color_primaries": video[0].get("color_primaries"),
        } if video else None)
        expected_video = int(output_kind == "video")
        expected_video_frames = _frame_at(duration) if output_kind == "video" else 0
        expected_audio = int(has_audio or output_kind == "audio")
        duration_tolerance = (2 / FRAME_RATE) if output_kind == "video" else (2 / SAMPLE_RATE)
        correct_format = (
            "mp4" in format_name.split(",") if output_kind == "video" else format_name == "wav"
        )
        audio_metadata_valid = not audio or (
            int(audio[0]["sample_rate"]) == SAMPLE_RATE
            and int(audio[0]["channels"]) == AUDIO_CHANNELS
            and audio[0].get("codec_name") == ("aac" if output_kind == "video" else
                                              "pcm_s16le" if preview else "pcm_s24le")
        )
        if (len(video) != expected_video or len(audio) != expected_audio
                or (video and (frames != expected_video_frames or video[0].get("codec_name") != "h264"))
                or (video and (video_frame_rate != FRAME_RATE
                               or expected_width is not None and video_width != expected_width
                               or expected_height is not None and video_height != expected_height))
                or (video and (expected_video_signal is None
                               or video_signal_readback["pixel_format"] != expected_video_signal["output_pixel_format"]
                               or video_signal_readback["bit_depth"] != expected_video_signal["output_bit_depth"]
                               or any(video_signal_readback[name] != expected_video_signal.get(
                                      f"output_{name}", expected_video_signal[name])
                                      for name in ("color_range", "color_space", "color_transfer", "color_primaries"))))
                or not correct_format or not audio_metadata_valid
                or not math.isfinite(actual) or abs(actual - duration) > duration_tolerance
                or any(not math.isfinite(start) or abs(start) > 2 / FRAME_RATE for start in starts)):
            raise ValueError("output differs from timeline")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise VideoEditingError("RENDER_OUTPUT_VERIFICATION_FAILED") from exc
    return {"requested_duration_sec": duration, "actual_duration_sec": actual,
            "audio_streams": len(audio), "video_streams": len(video),
            "requested_video_frames": expected_video_frames,
            "video_frames": frames, "video_width": video_width, "video_height": video_height,
            "video_frame_rate": (None if video_frame_rate is None else
                                 {"num": video_frame_rate.numerator, "den": video_frame_rate.denominator}),
            "video_signal": video_signal_readback,
            "stream_start_seconds": starts}


def _validate_output_path(output: Path, *, output_kind: str,
                          artifact_workspace: Path | None = None) -> Path:
    expected_suffix = ".mp4" if output_kind == "video" else ".wav"
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        parent = output.parent.resolve(strict=True)
        if artifact_workspace is not None:
            workspace = Path(artifact_workspace)
            allowed_parent = (
                workspace.is_absolute()
                and workspace.resolve(strict=True) == workspace.absolute()
                and parent == workspace
            )
        else:
            allowed_parent = parent != temporary_root and parent.is_relative_to(temporary_root)
        if (not output.is_absolute() or output.suffix.lower() != expected_suffix
                or output.exists() or output.is_symlink()
                or parent != output.parent.absolute() or not allowed_parent):
            raise ValueError("not a new private temporary output")
    except (OSError, ValueError) as exc:
        raise VideoEditingError("NEW_PRIVATE_TEMP_OUTPUT_REQUIRED") from exc
    return output


def _measure_loudness(path: Path, ffmpeg: str, *,
                      duration: float | None = None,
                      check_continue: Callable[[], None] | None = None) -> dict[str, Any]:
    # Analyze the final encoded file. The transformed stream goes to null; only
    # input_* statistics describe this file, not loudnorm's hypothetical output.
    diagnostic = _run([
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info",
        "-protocol_whitelist", "file", "-format_whitelist", "mov,wav,mp3", "-i", str(path),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", "loudnorm=print_format=json", "-f", "null", "-",
    ], cwd=path.parent, timeout=_programme_timeout(duration),
        **({"check_continue": check_continue} if check_continue else {}))
    try:
        start = diagnostic.rfind("{")
        if start < 0:
            raise ValueError("missing measurement")
        values, _ = json.JSONDecoder().raw_decode(diagnostic[start:])
        integrated = float(values["input_i"])
        peak = float(values["input_tp"])
        loudness_range = float(values["input_lra"])
        if (any(not math.isfinite(value) and value != -math.inf for value in (integrated, peak))
                or not math.isfinite(loudness_range) or loudness_range < 0
                or (peak == -math.inf and integrated != -math.inf)):
            raise ValueError("invalid measurement")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise VideoEditingError("AUDIO_LOUDNESS_MEASUREMENT_FAILED") from exc
    return {
        "state": "silent" if peak == -math.inf else "below_integrated_gate" if integrated == -math.inf else "measured",
        "integrated_lufs": integrated if math.isfinite(integrated) else None,
        "true_peak_dbtp": peak if math.isfinite(peak) else None,
        "loudness_range_lu": loudness_range,
        "method": "ffmpeg_loudnorm_input_ebu_r128",
        "target_lufs": None,
    }


def _verify_final_audio_peak(rendered: Path, audio: Path, *, ffmpeg: str,
                             output_kind: str, duration: float,
                             check_continue: Callable[[], None] | None = None
                             ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_options = {"check_continue": check_continue} if check_continue else {}
    loudness = _measure_loudness(rendered, ffmpeg, duration=duration, **run_options)
    initial = dict(loudness)
    peak = loudness["true_peak_dbtp"]
    correction_db = 0.0
    if peak is not None and peak > FINAL_TRUE_PEAK_LIMIT_DBTP:
        # One constant attenuation of the lossless mix preserves relative gains,
        # fades and dynamics. Never cascade lossy generations or boost quiet mixes.
        correction_db = FINAL_TRUE_PEAK_LIMIT_DBTP - peak - PEAK_CORRECTION_HEADROOM_DB
        corrected = rendered.with_name("peak-safe" + rendered.suffix)
        command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_threads", "1", "-threads", "1"]
        if output_kind == "video":
            command += ["-protocol_whitelist", "file", "-format_whitelist", "mov", "-i", str(rendered)]
        command += ["-protocol_whitelist", "file", "-format_whitelist", "wav", "-i", str(audio)]
        if output_kind == "video":
            command += ["-map", "0:v:0", "-c:v", "copy", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart"]
        else:
            command += ["-map", "0:a:0", "-c:a", "pcm_s24le", "-rf64", "auto"]
        samples = round(duration * SAMPLE_RATE)
        command += ["-af", f"volume={_number(correction_db)}dB,apad=whole_len={samples},"
                    f"atrim=end_sample={samples},asetpts=N/SR/TB",
                    "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-threads:a", "1", "-t", _number(duration),
                    "-map_metadata", "-1", str(corrected)]
        _run(command, cwd=rendered.parent, timeout=_programme_timeout(duration), **run_options)
        rendered = corrected
        loudness = _measure_loudness(rendered, ffmpeg, duration=duration, **run_options)
        peak = loudness["true_peak_dbtp"]
    if (peak is None and loudness["state"] != "silent"
            or peak is not None and (not math.isfinite(peak) or peak > FINAL_TRUE_PEAK_LIMIT_DBTP)):
        raise VideoEditingError("AUDIO_TRUE_PEAK_VERIFICATION_FAILED")
    return rendered, loudness, {
        "true_peak_verified": True,
        "true_peak_status": "silent" if peak is None else "verified",
        "final_true_peak_limit_dbtp": FINAL_TRUE_PEAK_LIMIT_DBTP,
        "initial_true_peak_dbtp": initial["true_peak_dbtp"],
        "initial_loudness": initial,
        "peak_correction_db": correction_db,
        "peak_correction_passes": int(correction_db < 0),
    }


def _timeline_render_plan(timeline: Timeline, output_kind: str):
    clips = [clip for track in timeline.tracks for clip in track.clips]
    if not clips:
        raise VideoEditingError("RENDER_REQUIRES_CLIPS")
    audible = [clip for track in timeline.audible_tracks() for clip in track.clips]
    ducked_clips = [clip for clip in audible if clip.audio_mix.ducking and clip.audio_role != 'muted']
    if any(clip.audio_role != "bgm" for clip in ducked_clips):
        raise VideoEditingError("AUDIO_DUCKING_REQUIRES_BACKGROUND_MUSIC")
    assets = {asset.asset_id: asset for asset in timeline.assets}
    if any(not assets[clip.media_asset_id].audio_stream_count for clip in ducked_clips):
        raise VideoEditingError("AUDIO_DUCKING_REQUIRES_AUDIO_SOURCE")
    if ducked_clips and not any(
        clip.audio_role == "voiceover" and clip.audio_mix.volume > 0
        and assets[clip.media_asset_id].audio_stream_count for clip in audible
    ):
        if any(clip.audio_role == 'voiceover' and assets[clip.media_asset_id].audio_stream_count for clip in clips):
            ducked_clips = []  # The user muted or unsoloed its key; preserve the configured mix.
        else:
            raise VideoEditingError("AUDIO_DUCKING_REQUIRES_VOICEOVER")
    all_video = ([track for track in timeline.tracks if track.kind == 'video' and track.clips]
                 if output_kind == "video" else [])
    video_tracks = [track for track in all_video if not track.hidden]
    if output_kind == "video" and not all_video:
        raise VideoEditingError("RENDER_REQUIRES_ONE_VIDEO_TRACK")

    duration = max(clip.timeline_start + clip.duration_sec for clip in clips)
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_EDIT_TIME_SEC:
        raise VideoEditingError("TIMELINE_DURATION_OUT_OF_RANGE")
    transitions = {}
    if output_kind == "video":
        clip_tracks = {clip.clip_id: track.track_id for track in video_tracks for clip in track.clips}
        hidden_ids = {clip.clip_id for track in all_video if track.hidden for clip in track.clips}
        for transition in timeline.transitions:
            if transition.from_clip in hidden_ids and transition.to_clip in hidden_ids:
                continue
            if (transition.from_clip not in clip_tracks or transition.to_clip not in clip_tracks
                    or clip_tracks[transition.from_clip] != clip_tracks[transition.to_clip]):
                raise VideoEditingError("TRANSITION_REQUIRES_ONE_VIDEO_TRACK")
        for track in video_tracks:
            ordered = sorted(track.clips, key=lambda clip: clip.timeline_start)
            if any(_frame_at(a.timeline_start + a.duration_sec) > _frame_at(b.timeline_start)
                   for a, b in pairwise(ordered)):
                raise VideoEditingError("VIDEO_TRACK_OVERLAP_REQUIRES_SEPARATE_LAYER")
            plan = _video_transition_plan(timeline, ordered)
            for index, item in plan.items():
                transitions[index if len(video_tracks) == 1 else (track.track_id, index)] = item

    audio_clips = [clip for track in timeline.audible_tracks() for clip in track.clips
                   if clip.audio_role != "muted"
                   and next(asset for asset in timeline.assets if asset.asset_id == clip.media_asset_id).audio_stream_count]
    if output_kind in {"audio", "audio_preview"} and not audio_clips and not any(
            assets[clip.media_asset_id].audio_stream_count for clip in clips):
        raise VideoEditingError("AUDIO_EXPORT_REQUIRES_AUDIO_SOURCE")

    return duration, video_tracks, ducked_clips, audio_clips, transitions


def _effect_runtime_commands(effect, instance: str, parameters: dict[str, Any], *, time_sec: Fraction) -> list[str]:
    when = _number(time_sec)

    def command(target: str, name: str, value: Any) -> str:
        rendered = value if isinstance(value, str) and name in {"master", "red", "green", "blue"} else _number(value)
        return f"{when} {target} {name} {rendered};"

    if effect.kind == "exposure":
        target = f"exposure@{instance}"
        return [command(target, "exposure", parameters["exposure_stops"]),
                command(target, "black", parameters["black_level"])]
    if effect.kind == "white_balance":
        target = f"colortemperature@{instance}"
        commands = [command(target, "temperature", parameters["temperature_kelvin"]),
                    command(target, "mix", parameters["mix"]),
                    command(target, "pl", parameters["preserve_lightness"])]
        if any(frame["parameters"]["tint"] != 0 for frame in effect.keyframes) or effect.parameters["tint"] != 0:
            commands.append(command(f"colorbalance@{instance}tint", "gm", -parameters["tint"]))
            commands.append(command(f"colorbalance@{instance}tint", "pl", 1))
        return commands
    if effect.kind == "color_balance":
        target = f"colorbalance@{instance}"
        options = []
        for region, suffix in (("shadows", "s"), ("midtones", "m"), ("highlights", "h")):
            for channel, prefix in (("red", "r"), ("green", "g"), ("blue", "b")):
                options.append(command(target, f"{prefix}{suffix}", parameters[region][channel]))
        options.append(command(target, "pl", int(parameters["preserve_lightness"])))
        return options
    if effect.kind == "curves":
        target = f"curves@{instance}"
        commands = []
        for channel in ("master", "red", "green", "blue"):
            if channel not in parameters:
                continue
            points = " ".join(f"{_number(x)}/{_number(y)}" for x, y in parameters[channel])
            commands.append(f"{when} {target} {channel} '{points}';")
        return commands
    return []


def _video_effect_filters(
    clip: TimelineClip,
    clip_time: Fraction = Fraction(0),
    *,
    frame_count: int = 0,
    prefix: str = "effect",
) -> tuple[list[str], list[str]]:
    filters = []
    commands = []
    for effect_index, effect in enumerate(clip.effects):
        if not effect.enabled:
            continue
        parameters = _effect_parameters_at(effect, clip_time)
        linear = any(item["interpolation"] == "linear" for item in effect.keyframes[:-1])
        instance = f"{prefix}{effect_index}"
        if effect.kind == "exposure":
            name = f"exposure@{instance}" if linear else "exposure"
            filters.append(
                f"{name}=exposure={_number(parameters['exposure_stops'])}:black={_number(parameters['black_level'])}"
            )
        elif effect.kind == "white_balance":
            temperature_name = f"colortemperature@{instance}" if linear else "colortemperature"
            filters.append(
                f"{temperature_name}=temperature={_number(parameters['temperature_kelvin'])}:"
                f"mix={_number(parameters['mix'])}:pl={_number(parameters['preserve_lightness'])}"
            )
            has_tint = (parameters["tint"] != 0 or effect.parameters["tint"] != 0
                        or any(item["parameters"]["tint"] != 0 for item in effect.keyframes))
            if has_tint:
                tint_name = f"colorbalance@{instance}tint" if linear else "colorbalance"
                filters.append(f"{tint_name}=gm={_number(-parameters['tint'])}:pl=1")
        elif effect.kind == "color_balance":
            options = []
            for region, suffix in (("shadows", "s"), ("midtones", "m"), ("highlights", "h")):
                for channel, prefix in (("red", "r"), ("green", "g"), ("blue", "b")):
                    value = parameters[region][channel]
                    if value or linear:
                        options.append(f"{prefix}{suffix}={_number(value)}")
            if parameters["preserve_lightness"]:
                options.append("pl=1")
            filters.append((f"colorbalance@{instance}=" if linear else "colorbalance=") + ":".join(options))
        elif effect.kind == "curves":
            options = []
            for channel in ("master", "red", "green", "blue"):
                if channel in parameters:
                    points = " ".join(f"{_number(x)}/{_number(y)}" for x, y in parameters[channel])
                    options.append(f"{channel}='{points}'")
            options.append(f"interp={parameters['interpolation']}")
            filters.append((f"curves@{instance}=" if linear else "curves=") + ":".join(options))
        elif effect.kind == "unsharp":
            filters.append(
                "unsharp="
                f"luma_msize_x={parameters['luma_size_x']}:luma_msize_y={parameters['luma_size_y']}:"
                f"luma_amount={_number(parameters['luma_amount'])}:"
                f"chroma_msize_x={parameters['chroma_size_x']}:chroma_msize_y={parameters['chroma_size_y']}:"
                f"chroma_amount={_number(parameters['chroma_amount'])}"
            )
        elif effect.kind == "mask":
            x = parameters["x"]
            y = parameters["y"]
            width = parameters["width"]
            height = parameters["height"]
            normalized_x = f"(((X/W)-({_number(x)}))/({_number(width)}))"
            normalized_y = f"(((Y/H)-({_number(y)}))/({_number(height)}))"
            if parameters["shape"] == "rectangle":
                edge = f"min(min({normalized_x},1-({normalized_x})),min({normalized_y},1-({normalized_y})))"
            elif parameters["shape"] == "ellipse":
                edge = (f"(1-sqrt((2*({normalized_x})-1)*(2*({normalized_x})-1)+"
                        f"(2*({normalized_y})-1)*(2*({normalized_y})-1)))")
            else:
                raise VideoEditingError("VIDEO_MASK_SHAPE_NOT_SUPPORTED")
            feather = parameters["feather"]
            coverage = (f"clip(({edge})/{_number(feather)},0,1)" if feather > 0
                        else f"clip(({edge})*1000000,0,1)")
            if parameters["invert"]:
                coverage = f"(1-({coverage}))"
            filters.extend([
                "format=rgba",
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({coverage})'",
            ])
        else:
            raise VideoEditingError("VIDEO_EFFECT_NOT_SUPPORTED")
        if linear:
            for frame_index in range(frame_count):
                at = clip_time + Fraction(frame_index, 1) / FRAME_RATE
                frame_parameters = _effect_parameters_at(effect, at)
                commands.extend(_effect_runtime_commands(
                    effect, instance, frame_parameters,
                    time_sec=Fraction(frame_index, 1) / FRAME_RATE,
                ))
    return filters, commands


def _keyframe_expression(
    clip: TimelineClip,
    name: str,
    interval_offset: Fraction,
    *,
    time_variable: str = "T",
) -> str:
    placement = clip.video_placement or VideoPlacement()
    if not clip.transform_keyframes:
        return _number(getattr(placement, name))
    time = f"({time_variable}+({_number(interval_offset)}))"
    keyframes = clip.transform_keyframes
    result = _number(getattr(keyframes[-1], name))
    for left, right in reversed(list(zip(keyframes, keyframes[1:]))):
        left_time, right_time = _number(left.time_sec), _number(right.time_sec)
        left_value, right_value = _number(getattr(left, name)), _number(getattr(right, name))
        if left.interpolation == "hold":
            segment = left_value
        else:
            segment = f"({left_value}+({right_value}-{left_value})*({time}-{left_time})/({right_time}-{left_time}))"
        result = f"if(lt({time},{right_time}),{segment},{result})"
    first_time = _number(keyframes[0].time_sec)
    first_value = _number(getattr(keyframes[0], name))
    return f"max(0,min(1,if(lt({time},{first_time}),{first_value},{result})))"


def _clip_placement_expression(
    clip: TimelineClip,
    name: str,
    interval_offset: Fraction,
    *,
    time_variable: str = "t",
) -> str:
    value = _keyframe_expression(
        clip, name, interval_offset, time_variable=time_variable,
    )
    if name == "x":
        return f"trunc(main_w*({value})/2)*2"
    if name == "y":
        return f"trunc(main_h*({value})/2)*2"
    raise VideoEditingError("VIDEO_KEYFRAME_PROPERTY_INVALID")


def _clip_opacity_filter(clip: TimelineClip, interval_offset: Fraction) -> str | None:
    if clip.transform_keyframes:
        opacity = _keyframe_expression(clip, "opacity", interval_offset, time_variable="T")
        return f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({opacity})'"
    placement = clip.video_placement or VideoPlacement()
    if placement.opacity == 1:
        return None
    return f"colorchannelmixer=aa={_number(placement.opacity)}"


def _render_layered_video(timeline, tracks, *, duration, width, height, ffmpeg, work,
                          audio, rendered, overlays, video_signal, check_continue=None):
    from .media import VideoPlacement

    options = {"check_continue": check_continue} if check_continue else {}
    end_frame = _frame_at(duration)
    frames_per_chunk = max(1, math.ceil(float(FRAME_RATE * 30)))
    boundaries = {0, end_frame, *range(frames_per_chunk, end_frame, frames_per_chunk)}
    spans = []
    assets = {asset.asset_id: asset for asset in timeline.assets}
    for layer, track in enumerate(tracks):
        clips = sorted(track.clips, key=lambda clip: clip.timeline_start)
        plan = _video_transition_plan(timeline, clips)
        for index, clip in enumerate(clips):
            start = _frame_at(clip.timeline_start)
            end = _frame_at(clip.timeline_start + clip.duration_sec)
            if end <= start:
                raise VideoEditingError("VIDEO_CLIP_SHORTER_THAN_OUTPUT_FRAME")
            incoming, outgoing = plan.get(index), plan.get(index + 1)
            visible_start = incoming["start_frame"] if incoming else start
            visible_end = outgoing["end_frame"] if outgoing else end
            spans.append((layer, clip, start, visible_start, visible_end, incoming))
            boundaries.update((visible_start, visible_end))
            if incoming:
                boundaries.add(incoming["end_frame"])
            if outgoing:
                boundaries.add(outgoing["start_frame"])
            for effect in clip.effects:
                if not effect.enabled:
                    continue
                for keyframe in effect.keyframes:
                    frame = _frame_at(clip.timeline_start + _fraction_time(keyframe["time_sec"]))
                    if visible_start < frame < visible_end:
                        boundaries.add(frame)
    ordered = sorted(boundaries)
    segments = []
    maximum_inputs = 0
    maximum_encoder_inputs = 0
    composition_passes = 0
    for interval, (begin, end) in enumerate(pairwise(ordered)):
        if check_continue:
            check_continue()
        frames = end - begin
        active = [item for item in spans if item[3] <= begin and item[4] >= end]
        maximum_inputs = max(maximum_inputs, len(active))
        layers = {}
        for item in active:
            layers.setdefault(item[0], []).append(item)
        batches, pending = [], []
        for items in layers.values():
            if len(items) > 2:
                raise VideoEditingError("VIDEO_LAYER_COMPOSITION_CONFLICT")
            capacity = RENDER_INPUT_BATCH_SIZE - int(bool(batches))
            if pending and len(pending) + len(items) > capacity:
                batches.append(pending)
                pending = []
            pending.extend(items)
        if pending or not batches:
            batches.append(pending)
        previous = None
        for batch_index, batch in enumerate(batches):
            final = batch_index == len(batches) - 1
            effect_command_files = []
            command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_threads", "1",
                       "-filter_complex_threads", "1", "-threads", "1"]
            if previous:
                command += ["-protocol_whitelist", "file", "-format_whitelist", "matroska,webm", "-i", str(previous)]
                graph = [f"[0:v:0]trim=end_frame={frames},settb=1/{FRAME_RATE},setpts=N[canvas]"]
            else:
                graph = [(f"color=c=black:s={width}x{height}:r={FRAME_RATE},trim=end_frame={frames},"
                          f"settb=1/{FRAME_RATE},setpts=N[canvas]")]
            maximum_encoder_inputs = max(maximum_encoder_inputs, len(batch) + int(previous is not None))
            by_layer = {}
            for index, (layer, clip, cut_start, _, _, incoming) in enumerate(batch, int(previous is not None)):
                asset = assets[clip.media_asset_id]
                source_start = (Fraction(0) if asset.is_still_image else
                                Fraction(str(clip.source_in)) +
                                Fraction(begin - cut_start, FRAME_RATE) * Fraction(str(clip.playback_rate)))
                if source_start < 0 and not asset.is_still_image:
                    raise VideoEditingError("TRANSITION_MEDIA_HANDLES_UNAVAILABLE")
                video_stream = 0 if clip.video_stream_index is None else clip.video_stream_index
                video_input_origin = (_stream_input_origin(asset, "video", video_stream)
                                      if not asset.is_still_image else Fraction(0))
                if not asset.is_still_image:
                    command += ["-ss", _number(video_input_origin + source_start)]
                command += ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                            "-i", asset.path]
                placement = clip.video_placement or VideoPlacement()
                w = max(2, int(width * placement.width) // 2 * 2)
                h = max(2, int(height * placement.height) // 2 * 2)
                crop = clip.frame_crop
                crop_filter = (f"crop=w=trunc(iw*{crop.width:.9f}/2)*2:h=trunc(ih*{crop.height:.9f}/2)*2:"
                               f"x=trunc(iw*{crop.x:.9f}/2)*2:y=trunc(ih*{crop.y:.9f}/2)*2,") if crop else ""
                color_transform_filter = (
                    _still_image_color_transform_filter(asset, video_signal)
                    if asset.is_still_image else
                    _video_color_transform_filter(asset, video_stream, video_signal)
                )
                scale_filter = _video_scale_filter(
                    w, h, video_signal, rgb_input=color_transform_filter is not None,
                )
                interval_offset = Fraction(begin - cut_start, 1) / FRAME_RATE
                effect_filters, effect_commands = _video_effect_filters(
                    clip, interval_offset, frame_count=frames,
                    prefix=f"e{interval}b{batch_index}i{index}",
                )
                if asset.is_still_image:
                    clip_filters = [
                        f"loop=loop={max(0, frames - 1)}:size=1:start=0",
                        f"settb=1/{FRAME_RATE}", "setpts=N", f"trim=end_frame={frames}",
                    ]
                else:
                    clip_filters = [
                        f"setpts=(PTS-STARTPTS)/{_number(clip.playback_rate)}",
                        f"fps={FRAME_RATE}", f"trim=end_frame={frames}",
                    ]
                if color_transform_filter is not None:
                    clip_filters.append(color_transform_filter)
                if effect_commands:
                    command_file = work / f"effect-commands-{interval:06d}-{batch_index:04d}-{index:04d}.txt"
                    command_file.write_text("\n".join(effect_commands), encoding="ascii")
                    effect_command_files.append(command_file)
                    clip_filters.append(f"sendcmd=f={command_file.name}")
                clip_filters.extend(effect_filters)
                if crop_filter:
                    clip_filters.append(crop_filter.rstrip(","))
                clip_filters.extend([
                    scale_filter,
                    "format=rgba",
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0",
                ])
                opacity_filter = _clip_opacity_filter(clip, interval_offset)
                if opacity_filter:
                    clip_filters.append(opacity_filter)
                clip_filters.extend(["setsar=1", f"settb=1/{FRAME_RATE}", "setpts=N"])
                graph.append(f"[{index}:v:{video_stream}]" + ",".join(clip_filters) + f"[v{index}]")
                by_layer.setdefault(layer, []).append((f"v{index}", incoming, clip, interval_offset))
            current = "canvas"
            for layer, items in by_layer.items():
                top = items[0][0]
                if len(items) == 2:
                    incoming = items[1][1]
                    if incoming is None or not incoming["start_frame"] <= begin < end <= incoming["end_frame"]:
                        raise VideoEditingError("VIDEO_LAYER_COMPOSITION_CONFLICT")
                    graph.append(f"[{current}]split=2[baseout{layer}][basein{layer}]")
                    for side, item in zip(("out", "in"), items, strict=True):
                        clip, offset = item[2], item[3]
                        x = _clip_placement_expression(clip, "x", offset)
                        y = _clip_placement_expression(clip, "y", offset)
                        graph.append(f"[base{side}{layer}][{item[0]}]overlay=x='{x}':y='{y}':eval=frame:"
                                     f"eof_action=pass:repeatlast=0:shortest=0:format=yuv420[composite{side}{layer}]")
                    graph.append(f"[compositeout{layer}]tpad=start={begin - incoming['start_frame']}:"
                                 f"start_mode=clone[transitionout{layer}]")
                    graph.append(f"[compositein{layer}]tpad=start={begin - incoming['start_frame']}:"
                                 f"start_mode=clone[transitionin{layer}]")
                    phase_start = begin - incoming["start_frame"]
                    phase_end = phase_start + frames
                    transition_duration = _number(Fraction(incoming["duration_frames"], 1) / FRAME_RATE)
                    graph.append(
                        f"[transitionout{layer}][transitionin{layer}]"
                        f"xfade=transition={incoming['type']}:duration={transition_duration}:offset=0,"
                        f"trim=start_frame={phase_start}:end_frame={phase_end},settb=1/{FRAME_RATE},setpts=N[layer{layer}]"
                    )
                else:
                    clip, offset = items[0][2], items[0][3]
                    x = _clip_placement_expression(clip, "x", offset)
                    y = _clip_placement_expression(clip, "y", offset)
                    graph.append(f"[{current}][{top}]overlay=x='{x}':y='{y}':eval=frame:"
                                 f"eof_action=pass:repeatlast=0:shortest=0:format=yuv420[layer{layer}]")
                current = f"layer{layer}"
            graph.append(f"[{current}]trim=end_frame={frames},setpts=N/({FRAME_RATE}*TB),"
                         f"{_video_signal_filter(video_signal)}[outv]")
            segment = work / (f"interval-{interval:06d}.mp4" if final else
                              f"interval-{interval:06d}-pass-{batch_index:04d}.mkv")
            codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] if final else ["-c:v", "ffv1", "-level", "3"]
            if shutil.disk_usage(work).free < frames * width * height * 2 + 16 * 1024 * 1024:
                raise VideoEditingError("MEDIA_RENDER_STORAGE_UNAVAILABLE")
            _run(command + ["-filter_complex", ";".join(graph), "-map", "[outv]", "-an",
                            "-frames:v", str(frames), *codec, "-threads:v", "1",
                            *_video_signal_args(video_signal), str(segment)],
                 cwd=work, timeout=max(60, frames / FRAME_RATE * 4), **options)
            for command_file in effect_command_files:
                command_file.unlink(missing_ok=True)
            composition_passes += 1
            if previous:
                previous.unlink()
            previous = segment
        segments.append(segment)
    manifest = work / "intervals.ffconcat"
    manifest.write_text("ffconcat version 1.0\n" + "".join(f"file {item.name}\n" for item in segments), encoding="utf-8")
    command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_threads", "1", "-f", "concat",
               "-safe", "1", "-protocol_whitelist", "file", "-i", str(manifest)]
    if audio is not None:
        command += ["-protocol_whitelist", "file", "-format_whitelist", "wav", "-i", str(audio)]
    command += ["-map", "0:v:0"]
    if overlays:
        command += ["-vf", ",".join([*overlays, _video_signal_filter(video_signal)]),
                    "-c:v", "libx264", "-preset", "veryfast", "-threads:v", "1",
                    *_video_signal_args(video_signal)]
    else:
        command += ["-c:v", "copy"]
    command += (["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE)]
                if audio is not None else ["-an"])
    command += ["-t", _number(duration), "-map_metadata", "-1", "-movflags", "+faststart", str(rendered)]
    _run(command, cwd=work, timeout=max(60, duration * 4), **options)
    return {"mode": "interval_video_layers", "video_layers": len(tracks), "intervals": len(segments),
            "maximum_active_video_inputs": maximum_inputs, "layer_order": [track.track_id for track in tracks],
            "maximum_encoder_inputs": maximum_encoder_inputs, "composition_passes": composition_passes,
            "output_timebase": {"num": FRAME_RATE.denominator, "den": FRAME_RATE.numerator},
            "boundary_rounding": "rational_half_up",
            "gap_fill": "black", "placements": [{"clip_id": c.clip_id, **c.video_placement.to_dict()}
                                                   for track in tracks for c in track.clips if c.video_placement],
            "animated_placements": [{
                "clip_id": clip.clip_id,
                "base_placement": (clip.video_placement or VideoPlacement()).to_dict(),
                "keyframes": [item.to_dict() for item in clip.transform_keyframes],
                "interpolation": "linear_or_hold_per_keyframe",
            } for track in tracks for clip in track.clips if clip.transform_keyframes]}


def render_timeline(
    timeline: Timeline,
    output: Path,
    *,
    output_kind: str = "video",
    check_continue: Callable[[], None] | None = None,
    render_options: dict[str, Any] | None = None,
    output_spec: dict[str, Any] | None = None,
    artifact_workspace: Path | None = None,
) -> dict[str, Any]:
    """Render one immutable timeline using a resolved, recorded output profile."""
    global AUDIO_CHANNELS, FRAME_RATE, FRAME_SIZES, SAMPLE_RATE
    spec = resolve_output_spec(timeline, output_kind, output_spec)
    previous_rate, previous_sample, previous_channels, previous_sizes = (
        FRAME_RATE, SAMPLE_RATE, AUDIO_CHANNELS, FRAME_SIZES,
    )
    with _RENDER_LOCK:
        FRAME_RATE = Fraction(spec["frame_rate"]["num"], spec["frame_rate"]["den"])
        SAMPLE_RATE = spec["sample_rate"]
        AUDIO_CHANNELS = spec["audio_channels"]
        if spec["width"] is not None:
            FRAME_SIZES = {**FRAME_SIZES, timeline.format_ratio: (spec["width"], spec["height"])}
        try:
            result = _render_timeline_impl(
                timeline,
                output,
                output_kind=output_kind,
                check_continue=check_continue,
                render_options=render_options,
                video_signal=spec.get("video_signal"),
                artifact_workspace=artifact_workspace,
            )
        finally:
            FRAME_RATE, SAMPLE_RATE, AUDIO_CHANNELS, FRAME_SIZES = (
                previous_rate, previous_sample, previous_channels, previous_sizes,
            )
    result["output_spec"] = spec
    return result


def _render_timeline_impl(timeline: Timeline, output: Path, *, output_kind: str = "video",
                          check_continue: Callable[[], None] | None = None,
                          render_options: dict[str, Any] | None = None,
                          video_signal: dict[str, Any] | None = None,
                          artifact_workspace: Path | None = None) -> dict[str, Any]:
    """Render to a new private workspace file; never authorize export."""
    timeline.validate()
    render_options = validate_render_options(render_options)
    if check_continue:
        check_continue()
    run_options = {"check_continue": check_continue} if check_continue else {}
    if output_kind not in {"video", "audio"}:
        raise VideoEditingError("OUTPUT_KIND_NOT_SUPPORTED")
    output = _validate_output_path(Path(output), output_kind=output_kind,
                                   artifact_workspace=artifact_workspace)
    if timeline.format_ratio not in FRAME_SIZES:
        raise VideoEditingError("FORMAT_RATIO_NOT_SUPPORTED")
    duration, video_tracks, ducked_clips, audio_clips, transitions = _timeline_render_plan(timeline, output_kind)

    ffmpeg = _ffmpeg_path()
    ffprobe = _ffprobe_path() if ffmpeg else None
    if not ffmpeg or not ffprobe:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    _verify_sources(timeline)
    width, height = FRAME_SIZES[timeline.format_ratio]
    has_audio = bool(audio_clips) or output_kind in {'audio','audio_preview'}

    with tempfile.TemporaryDirectory(prefix="timeline-render-", dir=output.parent) as temporary:
        work = Path(temporary)
        audio_clock_readback: list[dict[str, Any]] = []
        caption_layouts: list[dict[str, Any]] = []
        overlays = (_overlay_filters(timeline, duration=duration, width=width, height=height,
                                     ffmpeg=ffmpeg, work=work, layout_readback=caption_layouts, **run_options)
                    if output_kind == "video" else [])
        # Bind provenance to the exact private font bytes consumed by FFmpeg,
        # before publishing output or deleting the working directory.
        font_readback = ({"font_name": CAPTION_FONT.name,
                          "font_sha256": hashlib.sha256((work / "caption-font.ttc").read_bytes()).hexdigest()}
                         if overlays else {})
        audio = _render_audio(
            timeline, duration=duration, ffmpeg=ffmpeg, work=work,
            alignment_readback=audio_clock_readback, **run_options,
        )
        audio, mastering, denoising = _process_audio(audio, ffmpeg, render_options, duration=duration, **run_options)
        rendered = work / ("media.mp4" if output_kind == "video" else "media.wav")
        render_assets = {asset.asset_id: asset for asset in timeline.assets}
        layered = output_kind == 'video' and (not video_tracks or (
            len(video_tracks) > 1 or any(len(track.clips) > RENDER_INPUT_BATCH_SIZE for track in video_tracks)
            or any(track.layout == "free" for track in video_tracks)
            or bool(timeline.video_color_interpretations)
            or any(render_assets[clip.media_asset_id].is_still_image
                   for track in video_tracks for clip in track.clips)
            or any(clip.video_placement is not None for track in video_tracks for clip in track.clips)
            or any(clip.effects or clip.transform_keyframes for track in video_tracks for clip in track.clips)
            or any(abs(clip.timeline_start - sum(c.duration_sec for c in track.clips[:index])) > 1e-6
                   for track in video_tracks for index, clip in enumerate(track.clips))
            or duration > max(clip.timeline_start + clip.duration_sec for track in video_tracks for clip in track.clips) + 1e-6
        ))
        composition = {}
        if layered:
            composition = _render_layered_video(timeline, video_tracks, duration=duration, width=width,
                                                height=height, ffmpeg=ffmpeg, work=work, audio=audio,
                                                rendered=rendered, overlays=overlays,
                                                video_signal=video_signal, **run_options)
        elif video_tracks:
            segments: list[Path] = []
            asset_paths = {asset.asset_id: asset.path for asset in timeline.assets}
            media_assets = {asset.asset_id: asset for asset in timeline.assets}
            for index, clip in enumerate(video_tracks[0].clips):
                frame_count = _frame_at(clip.timeline_start + clip.duration_sec) - _frame_at(clip.timeline_start)
                if frame_count < 1:
                    raise VideoEditingError("VIDEO_CLIP_SHORTER_THAN_OUTPUT_FRAME")
                before = transitions.get(index, {}).get("before_cut_frames", 0)
                after = transitions.get(index + 1, {}).get("after_cut_frames", 0)
                frame_count += before + after
                segment = work / f"clip-{index:03d}.mp4"
                crop=clip.frame_crop
                crop_filter=(f"crop=w=trunc(iw*{crop.width:.9f}/2)*2:h=trunc(ih*{crop.height:.9f}/2)*2:"
                             f"x=trunc(iw*{crop.x:.9f}/2)*2:y=trunc(ih*{crop.y:.9f}/2)*2,") if crop else ""
                scale_filter = _video_scale_filter(width, height, video_signal)
                video_stream_index = 0 if clip.video_stream_index is None else clip.video_stream_index
                video_input_origin = _stream_input_origin(media_assets[clip.media_asset_id], "video", video_stream_index)
                source_seek = video_input_origin + clip.source_in - before * clip.playback_rate / FRAME_RATE
                _run([ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_threads", "1",
                      "-filter_complex_threads", "1", "-threads", "1", "-ss", _number(source_seek),
                      "-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                      "-i", asset_paths[clip.media_asset_id], "-frames:v", str(frame_count), "-map", f"0:v:{video_stream_index}",
                      "-an", "-vf", f"setpts=(PTS-STARTPTS)/{_number(clip.playback_rate)},{crop_filter}{scale_filter},"
                      f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FRAME_RATE},"
                      f"{_video_signal_filter(video_signal)}",
                      "-c:v", "libx264", "-preset", "veryfast", "-threads:v", "1", *_video_signal_args(video_signal),
                      str(segment)], cwd=work,
                     timeout=_programme_timeout(frame_count / FRAME_RATE), **run_options)
                segments.append(segment)

            command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-filter_complex_threads", "1"]
            for segment in segments:
                command += ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                            "-i", str(segment)]
            if audio is not None:
                command += ["-protocol_whitelist", "file", "-format_whitelist", MEDIA_INPUT_DEMUXERS,
                            "-i", str(audio)]
            if transitions:
                graph = [f"[{index}:v:0]settb=1/{FRAME_RATE},setpts=PTS-STARTPTS,fps={FRAME_RATE}[v{index}]"
                         for index in range(len(segments))]
                current = "v0"
                for index in range(1, len(segments)):
                    item = transitions.get(index)
                    merge = (f"xfade=transition={item['type']}:duration={_number(item['duration_frames'] / FRAME_RATE)}:"
                             f"offset={_number(item['start_frame'] / FRAME_RATE)}" if item else
                             f"concat=n=2:v=1:a=0,settb=1/{FRAME_RATE},setpts=PTS-STARTPTS,fps={FRAME_RATE}")
                    graph.append(f"[{current}][v{index}]{merge}[merged{index}]")
                    current = f"merged{index}"
                graph.append(f"[{current}]trim=end_frame={_frame_at(duration)},setpts=PTS-STARTPTS" +
                             ("," + ",".join(overlays) if overlays else "") +
                             f",{_video_signal_filter(video_signal)}[outv]")
            else:
                graph = [f"[{index}:v:0]setpts=PTS-STARTPTS[v{index}]" for index in range(len(segments))]
                graph.append("".join(f"[v{index}]" for index in range(len(segments))) +
                             f"concat=n={len(segments)}:v=1:a=0" +
                             ("," + ",".join(overlays) if overlays else "") +
                             f",{_video_signal_filter(video_signal)}[outv]")
            command += ["-filter_complex", ";".join(graph), "-map", "[outv]"]
            if audio is not None:
                command += ["-map", f"{len(segments)}:a:0", "-c:a", "aac", "-b:a", "192k",
                            "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS)]
            else:
                command += ["-an"]
            command += ["-c:v", "libx264", "-preset", "veryfast", "-threads:v", "1",
                        *_video_signal_args(video_signal),
                        "-t", _number(duration), "-map_metadata", "-1", "-movflags", "+faststart",
                        str(rendered)]
            # A complete programme can take longer than the command helper's
            # short default. Keep a finite duration-based budget and the same
            # cancellation callback, rather than discarding a valid long render.
            _run(command, cwd=work, timeout=max(60.0, duration * 4.0), **run_options)
        elif audio is not None:
            shutil.copyfile(audio, rendered)
        else:
            raise VideoEditingError("AUDIO_EXPORT_REQUIRES_AUDIO_SOURCE")

        peak_processing = {"true_peak_verified": False, "true_peak_status": "not_applicable",
                           "final_true_peak_limit_dbtp": None, "initial_true_peak_dbtp": None,
                           "initial_loudness": None, "peak_correction_db": 0.0, "peak_correction_passes": 0}
        if has_audio:
            rendered, loudness, peak_processing = _verify_final_audio_peak(
                rendered, audio, ffmpeg=ffmpeg, output_kind=output_kind, duration=duration, **run_options,
            )
        if mastering and (loudness["integrated_lufs"] is None or
                          abs(loudness["integrated_lufs"] - mastering["target_lufs"]) > 1):
            raise VideoEditingError("AUDIO_MASTER_TARGET_NOT_REACHED")
        measured = _probe_output(rendered, ffprobe, output_kind=output_kind,
                                 duration=duration, has_audio=has_audio,
                                 expected_video_signal=video_signal,
                                 expected_width=width if output_kind == "video" else None,
                                 expected_height=height if output_kind == "video" else None,
                                 **run_options)
        if has_audio:
            measured["loudness"] = loudness
        _verify_sources(timeline)
        if check_continue:
            check_continue()
        created = False
        try:
            with output.open("xb") as destination, rendered.open("rb") as source:
                created = True
                shutil.copyfileobj(source, destination)
        except Exception:
            if created:
                output.unlink(missing_ok=True)
            raise

    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"state": "rendered_verified", "output_kind": output_kind, "timeline_version": timeline.version,
            "media_sha256": digest.hexdigest(), "size_bytes": output.stat().st_size,
            "measurement": measured, "renderer": "oevira-media-adapted-v1",
            "video_signal_processing": ({
                "source_profiles_checked": video_signal["source_profiles_checked"],
                **({
                    "source_profiles": video_signal["source_transforms"],
                    "selected_interpretations": [item["interpretation"]
                        for item in video_signal["source_transforms"] if item.get("interpretation") is not None],
                    "output_profile": {
                        "pixel_format": video_signal["output_pixel_format"],
                        "bit_depth": video_signal["output_bit_depth"],
                        "color_range": video_signal.get("output_color_range", video_signal["color_range"]),
                        "color_space": video_signal.get("output_color_space", video_signal["color_space"]),
                        "color_transfer": video_signal.get("output_color_transfer", video_signal["color_transfer"]),
                        "color_primaries": video_signal.get("output_color_primaries", video_signal["color_primaries"]),
                    },
                } if ("still_image_source_profiles" in video_signal
                      or video_signal.get("color_interpretations_applied")) else {
                    "source_and_output_profile": {key: video_signal[key] for key in (
                        "pixel_format", "bit_depth", "color_range", "color_space", "color_transfer", "color_primaries",
                    )}
                }),
                "output_readback": measured["video_signal"],
                "transform": video_signal["transform"],
            } if output_kind == "video" and video_signal is not None else None),
            "video_effect_processing": [
                {"clip_id": clip.clip_id, "effect_order": [effect.effect_id for effect in clip.effects],
                 "effects": [effect.to_dict() for effect in clip.effects],
                 "applied_effect_ids": [effect.effect_id for effect in clip.effects if effect.enabled]}
                for track in timeline.tracks for clip in track.clips if clip.effects
            ],
            "transform_keyframe_processing": [
                {"clip_id": clip.clip_id,
                 "base_placement": (clip.video_placement.to_dict() if clip.video_placement else VideoPlacement().to_dict()),
                 "keyframes": [keyframe.to_dict() for keyframe in clip.transform_keyframes],
                 "interpolation": "linear_or_hold_per_keyframe"}
                for track in timeline.tracks for clip in track.clips if clip.transform_keyframes
            ],
            "composition": composition,
            "track_processing":{'muted_audio_tracks':[track.track_id for track in timeline.tracks if track.muted],
                'solo_audio_tracks':[track.track_id for track in timeline.tracks if track.solo],
                'hidden_video_tracks':[track.track_id for track in timeline.tracks if track.hidden],
                'locked_tracks':[track.track_id for track in timeline.tracks if track.locked]},
            "overlay_processing": {"count": len(timeline.overlays),
                                   "status": ("burned_into_video" if output_kind == "video" and timeline.overlays
                                              else "not_applicable_to_audio" if output_kind == "audio"
                                              else "no_overlays"),
                                   **font_readback,
                                   "profile": ("mixed-literal-installed-font-v3" if caption_layouts else
                                               "literal-cjk-installed-font-v2" if timeline.overlays else None),
                                   **({"measured_layouts": caption_layouts} if caption_layouts else {})},
            "frame_processing": {"mode":"explicit_user_region_then_aspect_preserving_pad",
                                 "automatic_subject_detection":False,"coordinate_space":"display_oriented_source",
                                 "pixel_rounding":"even_pixels_floor",
                                 "crops":[{"clip_id":clip.clip_id,**clip.frame_crop.to_dict()}
                                          for track in timeline.tracks for clip in track.clips if clip.frame_crop]},
            "transition_processing": {"profile": "centred-handles-output-framerate-v2" if transitions else None,
                                      "output_frame_rate": _rational_json(FRAME_RATE) if transitions else None,
                                      "visual_only": True, "applied": list(transitions.values())},
            "audio_source_clock_mappings": audio_clock_readback,
            "audio_processing": {"pre_encode_sample_peak_limit_dbfs": -1 if has_audio else None,
                                 "limiter_latency_compensated": has_audio,
                                 "ducked_music_clips": len(ducked_clips),
                                 "loudness_normalized": bool(mastering), "mastering": mastering,
                                 "noise_reduction_applied":bool(denoising),"denoising":denoising, **peak_processing},
            "rights_state": "unknown", "publish_status": "not_performed"}


def _process_audio(audio, ffmpeg, options, *, duration=None, check_continue=None):
    mastering, denoising = {}, {}
    if options and audio is None:
        raise VideoEditingError("AUDIO_MASTER_REQUIRES_AUDIO")
    if "noise_reduction_db" in options:
        audio, denoising = _denoise_audio(
            audio, ffmpeg=ffmpeg, reduction=options["noise_reduction_db"],
            noise_floor=options["noise_floor_db"], duration=duration, check_continue=check_continue,
        )
    if "loudness_target_lufs" in options:
        audio, mastering = _master_audio(
            audio, ffmpeg=ffmpeg, target=options["loudness_target_lufs"], duration=duration, check_continue=check_continue,
        )
    return audio, mastering, denoising


def preview_audio_processing(timeline: Timeline, *, start_sec: Any, end_sec: Any,
                             render_options=None, check_continue=None) -> dict[str, Any]:
    with _RENDER_LOCK:
        return _preview_audio_processing_impl(
            timeline,
            start_sec=start_sec,
            end_sec=end_sec,
            render_options=render_options,
            check_continue=check_continue,
        )


def _audio_window_timeline(timeline: Timeline, start: Fraction, end: Fraction) -> Timeline:
    assets = {asset.asset_id: asset for asset in timeline.assets}
    tracks = []
    for track in timeline.tracks:
        clips = []
        for clip in track.clips:
            asset = assets[clip.media_asset_id]
            if clip.audio_role == "muted" or not asset.audio_stream_count:
                continue
            clip_start = _fraction_time(clip.timeline_start)
            clip_end = clip_start + clip.duration_sec
            visible_start, visible_end = max(start, clip_start), min(end, clip_end)
            if visible_start >= visible_end:
                continue
            offset_start = visible_start - clip_start
            offset_end = visible_end - clip_start
            rate = _fraction_time(clip.playback_rate)
            source_start = _fraction_time(clip.source_in) + offset_start * rate
            source_end = _fraction_time(clip.source_in) + offset_end * rate
            duration = offset_end - offset_start
            fade_in = max(Fraction(0), min(clip.audio_mix.fade_in_sec, offset_end) - offset_start)
            fade_out_start = clip.duration_sec - clip.audio_mix.fade_out_sec
            fade_out = max(Fraction(0), offset_end - max(offset_start, fade_out_start))
            fade_in = min(fade_in, duration)
            fade_out = min(fade_out, duration - fade_in)
            clips.append(replace(
                clip,
                source_in=source_start,
                source_out=source_end,
                timeline_start=visible_start - start,
                audio_mix=replace(clip.audio_mix, fade_in_sec=fade_in, fade_out_sec=fade_out),
                effects=(),
                transform_keyframes=(),
            ))
        tracks.append(replace(track, clips=tuple(clips)))
    window = replace(
        timeline,
        tracks=tuple(tracks),
        transitions=(),
        overlays=(),
        chapters=(),
        _history=(),
    )
    window.validate()
    if not any(clip.audio_role != "muted" and assets[clip.media_asset_id].audio_stream_count
               for track in window.tracks for clip in track.clips):
        raise VideoEditingError("AUDIO_PREVIEW_RANGE_HAS_NO_AUDIO")
    return window


def preview_audio_timeline_change(
    base_timeline: Timeline,
    candidate_timeline: Timeline,
    *,
    base_start_sec: Any,
    base_end_sec: Any,
    candidate_start_sec: Any,
    candidate_end_sec: Any,
    check_continue=None,
) -> dict[str, Any]:
    """Render real before/after audio for an unpersisted timeline candidate."""
    with _RENDER_LOCK:
        return _preview_audio_timeline_change_impl(
            base_timeline, candidate_timeline,
            base_start_sec=base_start_sec, base_end_sec=base_end_sec,
            candidate_start_sec=candidate_start_sec, candidate_end_sec=candidate_end_sec,
            check_continue=check_continue,
        )


def _preview_audio_timeline_change_impl(
    base_timeline: Timeline,
    candidate_timeline: Timeline,
    *,
    base_start_sec: Any,
    base_end_sec: Any,
    candidate_start_sec: Any,
    candidate_end_sec: Any,
    check_continue=None,
) -> dict[str, Any]:
    import base64

    base_timeline.validate()
    candidate_timeline.validate()
    if candidate_timeline.version <= base_timeline.version:
        raise VideoEditingError("AUDIO_CANDIDATE_VERSION_INVALID")
    base_duration = _timeline_render_plan(base_timeline, "audio_preview")[0]
    candidate_duration = _timeline_render_plan(candidate_timeline, "audio_preview")[0]
    try:
        base_start = _fraction_time(base_start_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        base_end = _fraction_time(base_end_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        candidate_start = _fraction_time(candidate_start_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        candidate_end = _fraction_time(candidate_end_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
    except VideoEditingError as exc:
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID") from exc
    if (not 0 <= base_start < base_end <= base_duration
            or not 0 <= candidate_start < candidate_end <= candidate_duration
            or base_end - base_start > 60 or candidate_end - candidate_start > 60):
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID")
    ffmpeg, ffprobe = _ffmpeg_path(), _ffprobe_path()
    if not ffmpeg or not ffprobe:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    if check_continue:
        check_continue()
    _verify_sources(base_timeline)
    _verify_sources(candidate_timeline)
    clock_readbacks = {"base": [], "candidate": []}
    windows = {
        "base": (base_timeline, base_duration, base_start, base_end),
        "candidate": (candidate_timeline, candidate_duration, candidate_start, candidate_end),
    }
    samples = {}
    with tempfile.TemporaryDirectory(prefix="audio-timeline-candidate-") as temporary:
        root = Path(temporary)
        for name, (timeline, duration, start, end) in windows.items():
            if check_continue:
                check_continue()
            work = root / name
            work.mkdir()
            source = _render_audio(
                timeline, duration=float(duration), ffmpeg=ffmpeg, work=work,
                check_continue=check_continue, alignment_readback=clock_readbacks[name],
            )
            if source is None:
                raise VideoEditingError("AUDIO_EXPORT_REQUIRES_AUDIO_SOURCE")
            sample = work / "sample.wav"
            sample_duration = end - start
            _run([
                ffmpeg, "-v", "error", "-nostdin", "-n", "-protocol_whitelist", "file",
                "-format_whitelist", "wav", "-i", str(source), "-map", "0:a:0", "-af",
                f"atrim=start={_number(start)}:end={_number(end)},asetpts=PTS-STARTPTS",
                "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s16le", str(sample),
            ], cwd=work, check_continue=check_continue)
            _probe_output(
                sample, ffprobe, output_kind="audio", duration=float(sample_duration),
                has_audio=True, preview=True, check_continue=check_continue,
            )
            data = sample.read_bytes()
            if len(data) > 12 * 1024 * 1024:
                raise VideoEditingError("AUDIO_COMPARISON_TOO_LARGE")
            samples[name] = {
                "wav_base64": base64.b64encode(data).decode("ascii"),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "start_sec": _rational_json(start),
                "end_sec": _rational_json(end),
                "duration_sec": _rational_json(sample_duration),
            }
    _verify_sources(base_timeline)
    _verify_sources(candidate_timeline)
    if check_continue:
        check_continue()
    return {
        "state": "candidate_preview",
        "base_version": base_timeline.version,
        "base_digest": base_timeline._view_digest(),
        "candidate_version": candidate_timeline.version,
        "candidate_digest": candidate_timeline._view_digest(),
        "base_programme_duration_sec": _rational_json(base_duration),
        "candidate_programme_duration_sec": _rational_json(candidate_duration),
        "sample_rate": SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "sample_format": "pcm_s16le",
        "samples": samples,
        "audio_source_clock_mappings": clock_readbacks,
        "candidate_timeline": candidate_timeline.to_dict(),
        "persisted": False,
        "export_performed": False,
        "user_adoption_required": True,
    }


def _preview_audio_processing_impl(timeline: Timeline, *, start_sec: Any, end_sec: Any,
                                   render_options=None, check_continue=None) -> dict[str, Any]:
    import base64

    timeline.validate()
    options = validate_render_options(render_options)
    duration, _, _, _, _ = _timeline_render_plan(timeline, "audio_preview")
    try:
        start = _fraction_time(start_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        end = _fraction_time(end_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
    except VideoEditingError as exc:
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID") from exc
    if not 0 <= start < end <= duration or end - start > 60:
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID")
    ffmpeg, ffprobe = _ffmpeg_path(), _ffprobe_path()
    if not ffmpeg or not ffprobe:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    if check_continue:
        check_continue()
    _verify_sources(timeline)
    audio_clock_readback: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="audio-comparison-") as temporary:
        work = Path(temporary)
        original = _render_audio(
            timeline, duration=duration, ffmpeg=ffmpeg, work=work,
            check_continue=check_continue, alignment_readback=audio_clock_readback,
        )
        before = _measure_loudness(original, ffmpeg, duration=duration, check_continue=check_continue)
        processed, mastering, _ = _process_audio(original, ffmpeg, options, duration=duration, check_continue=check_continue)
        processed, after, _ = _verify_final_audio_peak(
            processed, processed, ffmpeg=ffmpeg, output_kind="audio", duration=duration,
            check_continue=check_continue,
        )
        if mastering and (after["integrated_lufs"] is None or
                          abs(after["integrated_lufs"] - mastering["target_lufs"]) > 1):
            raise VideoEditingError("AUDIO_MASTER_TARGET_NOT_REACHED")
        samples = {}
        for name, source in (("original", original), ("processed", processed)):
            output = work / f"{name}-sample.wav"
            _run([ffmpeg, "-v", "error", "-nostdin", "-n", "-protocol_whitelist", "file",
                  "-format_whitelist", "wav", "-i", str(source), "-map", "0:a:0", "-af",
                  f"atrim=start={_number(start)}:end={_number(end)},asetpts=PTS-STARTPTS",
                  "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s16le", str(output)],
                 cwd=work, check_continue=check_continue)
            _probe_output(output, ffprobe, output_kind="audio", duration=end-start,
                          has_audio=True, preview=True, check_continue=check_continue)
            data = output.read_bytes()
            if len(data) > 12 * 1024 * 1024:
                raise VideoEditingError("AUDIO_COMPARISON_TOO_LARGE")
            samples[name] = {"wav_base64": base64.b64encode(data).decode("ascii"),
                             "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        _verify_sources(timeline)
        if check_continue:
            check_continue()
    return {"state": "preview", "base_version": timeline.version, "base_digest": timeline._view_digest(),
            "start_sec": start, "end_sec": end, "programme_duration_sec": duration,
            "sample_rate": SAMPLE_RATE, "channels": AUDIO_CHANNELS, "sample_format": "pcm_s16le",
            "render_options": options, "samples": samples,
            "audio_source_clock_mappings": audio_clock_readback,
            "programme_measurements": {"original": before, "processed": after},
            "persisted": False, "export_performed": False, "user_adoption_required": True}


def preview_audio_timeline_change(
    base_timeline: Timeline,
    candidate_timeline: Timeline,
    *,
    base_start_sec: Any,
    base_end_sec: Any,
    candidate_start_sec: Any,
    candidate_end_sec: Any,
    check_continue=None,
) -> dict[str, Any]:
    """Decode original and candidate timeline audio for a bounded A/B audition."""
    with _RENDER_LOCK:
        return _preview_audio_timeline_change_impl(
            base_timeline, candidate_timeline,
            base_start_sec=base_start_sec, base_end_sec=base_end_sec,
            candidate_start_sec=candidate_start_sec, candidate_end_sec=candidate_end_sec,
            check_continue=check_continue,
        )


def _preview_audio_timeline_change_impl(
    base_timeline: Timeline,
    candidate_timeline: Timeline,
    *,
    base_start_sec: Any,
    base_end_sec: Any,
    candidate_start_sec: Any,
    candidate_end_sec: Any,
    check_continue=None,
) -> dict[str, Any]:
    import base64

    base_timeline.validate()
    candidate_timeline.validate()
    if candidate_timeline.version <= base_timeline.version:
        raise VideoEditingError("AUDIO_CANDIDATE_VERSION_INVALID")
    base_duration = _timeline_render_plan(base_timeline, "audio_preview")[0]
    candidate_duration = _timeline_render_plan(candidate_timeline, "audio_preview")[0]
    try:
        base_start = _fraction_time(base_start_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        base_end = _fraction_time(base_end_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        candidate_start = _fraction_time(candidate_start_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
        candidate_end = _fraction_time(candidate_end_sec, error_code="AUDIO_COMPARISON_RANGE_INVALID")
    except VideoEditingError as exc:
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID") from exc
    if (not 0 <= base_start < base_end <= base_duration
            or not 0 <= candidate_start < candidate_end <= candidate_duration
            or base_end - base_start > 60 or candidate_end - candidate_start > 60):
        raise VideoEditingError("AUDIO_COMPARISON_RANGE_INVALID")
    ffmpeg, ffprobe = _ffmpeg_path(), _ffprobe_path()
    if not ffmpeg or not ffprobe:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    if check_continue:
        check_continue()
    _verify_sources(base_timeline)
    _verify_sources(candidate_timeline)
    base_window = _audio_window_timeline(base_timeline, base_start, base_end)
    candidate_window = _audio_window_timeline(candidate_timeline, candidate_start, candidate_end)
    clock_readbacks: dict[str, list[dict[str, Any]]] = {"base": [], "candidate": []}
    ranges = {
        "base": (base_window, base_end - base_start, Fraction(0), base_end - base_start, base_start),
        "candidate": (candidate_window, candidate_end - candidate_start, Fraction(0),
                      candidate_end - candidate_start, candidate_start),
    }
    samples = {}
    with tempfile.TemporaryDirectory(prefix="audio-timeline-candidate-") as temporary:
        root = Path(temporary)
        for name, (timeline, duration, start, end, timeline_origin) in ranges.items():
            if check_continue:
                check_continue()
            work = root / name
            work.mkdir()
            rendered = _render_audio(
                timeline, duration=float(duration), ffmpeg=ffmpeg, work=work,
                check_continue=check_continue, alignment_readback=clock_readbacks[name],
            )
            if rendered is None:
                raise VideoEditingError("AUDIO_EXPORT_REQUIRES_AUDIO_SOURCE")
            sample = work / "sample.wav"
            sample_duration = end - start
            _run([
                ffmpeg, "-v", "error", "-nostdin", "-n", "-protocol_whitelist", "file",
                "-format_whitelist", "wav", "-i", str(rendered), "-map", "0:a:0", "-af",
                f"atrim=start={_number(start)}:end={_number(end)},asetpts=PTS-STARTPTS",
                "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s16le", str(sample),
            ], cwd=work, check_continue=check_continue)
            _probe_output(
                sample, ffprobe, output_kind="audio", duration=float(sample_duration),
                has_audio=True, preview=True, check_continue=check_continue,
            )
            data = sample.read_bytes()
            if len(data) > 12 * 1024 * 1024:
                raise VideoEditingError("AUDIO_COMPARISON_TOO_LARGE")
            samples[name] = {
                "wav_base64": base64.b64encode(data).decode("ascii"),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "start_sec": _rational_json(timeline_origin),
                "end_sec": _rational_json(timeline_origin + sample_duration),
                "duration_sec": _rational_json(sample_duration),
            }
    _verify_sources(base_timeline)
    _verify_sources(candidate_timeline)
    if check_continue:
        check_continue()
    return {
        "state": "candidate_preview",
        "base_version": base_timeline.version,
        "base_digest": base_timeline._view_digest(),
        "candidate_version": candidate_timeline.version,
        "candidate_digest": candidate_timeline._view_digest(),
        "base_programme_duration_sec": _rational_json(base_duration),
        "candidate_programme_duration_sec": _rational_json(candidate_duration),
        "sample_rate": SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "sample_format": "pcm_s16le",
        "samples": samples,
        "audio_source_clock_mappings": clock_readbacks,
        "candidate_timeline": candidate_timeline.to_dict(),
        "persisted": False,
        "export_performed": False,
        "user_adoption_required": True,
    }


def validate_render_options(options: Any = None) -> dict[str, Any]:
    """Only explicit processing: no automatic denoise or gain changes."""
    if options is None:return {}
    if not isinstance(options,dict) or set(options)-{"loudness_target_lufs","noise_reduction_db","noise_floor_db"}:
        raise VideoEditingError("RENDER_OPTIONS_INVALID")
    normalized={}
    if "loudness_target_lufs" in options:
        value=options["loudness_target_lufs"]
        if type(value) not in (int,float) or value not in {-16,-14,-23}:
            raise VideoEditingError("LOUDNESS_TARGET_NOT_SUPPORTED")
        normalized["loudness_target_lufs"]=float(value)
    if "noise_reduction_db" in options:
        reduction=options["noise_reduction_db"];floor=options.get("noise_floor_db",-50)
        if (type(reduction) not in (int,float) or not math.isfinite(reduction) or not 3<=reduction<=18
                or type(floor) not in (int,float) or not math.isfinite(floor) or not -80<=floor<=-20):
            raise VideoEditingError("NOISE_REDUCTION_OPTIONS_INVALID")
        normalized.update(noise_reduction_db=float(reduction),noise_floor_db=float(floor))
    elif "noise_floor_db" in options:
        raise VideoEditingError("NOISE_REDUCTION_EXPLICIT_REQUEST_REQUIRED")
    return normalized


def _denoise_audio(audio:Path,*,ffmpeg:str,reduction:float,noise_floor:float,duration=None,
                   check_continue:Callable[[],None]|None=None):
    """Opt-in FFT denoising, not speech restoration or a perceptual quality claim.

    Filter contract: https://www.ffmpeg.org/ffmpeg-filters.html#afftdn
    """
    output=audio.with_name("denoised.wav")
    before=_measure_loudness(audio,ffmpeg,duration=duration,check_continue=check_continue)
    _run([ffmpeg,"-v","error","-nostdin","-n","-protocol_whitelist","file","-format_whitelist","wav",
          "-i",str(audio),"-map","0:a:0","-af",f"afftdn=nr={reduction}:nf={noise_floor}:tn=0:om=o",
          "-ar",str(SAMPLE_RATE),"-ac",str(AUDIO_CHANNELS),"-c:a","pcm_s24le","-rf64","auto",str(output)],
         cwd=audio.parent,timeout=_programme_timeout(duration),check_continue=check_continue)
    after=_measure_loudness(output,ffmpeg,duration=duration,check_continue=check_continue)
    return output,{"method":"ffmpeg_afftdn","noise_reduction_db":reduction,"noise_floor_db":noise_floor,
                   "automatic_noise_tracking":False,"input_measurement":before,"output_measurement":after,
                   "perceptual_quality_verified":False,"original_sources_modified":False}


def _master_audio(audio: Path, *, ffmpeg: str, target: float,
                  duration: float | None = None,
                  check_continue: Callable[[], None] | None = None) -> tuple[Path, dict[str, Any]]:
    """Two-pass linear EBU R128 normalization, with measured feasibility checks."""
    def measure(arguments: str, destination: list[str]) -> dict[str, Any]:
        raw = _run([ffmpeg, "-v", "info", "-nostdin", "-nostats", "-n",
                    "-protocol_whitelist", "file", "-format_whitelist", "wav", "-i", str(audio),
                    "-map", "0:a:0", "-af", arguments, *destination], cwd=audio.parent,
                   timeout=_programme_timeout(duration), check_continue=check_continue)
        try:
            parsed, _ = json.JSONDecoder().raw_decode(raw[raw.rfind("{"):])
            return parsed
        except (ValueError, TypeError) as exc:
            raise VideoEditingError("AUDIO_MASTER_MEASUREMENT_FAILED") from exc
    settings = f"loudnorm=I={target}:TP=-1:LRA=11"
    measured = measure(settings + ":print_format=json", ["-f", "null", "-"])
    try:
        values = {name: float(measured[name]) for name in
                  ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("silent or unmeasurable")
        if values["input_lra"] > 11 or values["input_tp"] + target - values["input_i"] > -1:
            raise VideoEditingError("LINEAR_MASTER_TARGET_NOT_FEASIBLE")
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, VideoEditingError):
            raise
        raise VideoEditingError("AUDIO_MASTER_REQUIRES_MEASURABLE_AUDIO") from exc
    output = audio.with_name("mastered.wav")
    method = "ffmpeg_loudnorm_two_pass_linear"
    if values["input_lra"] == 0:
        # FFmpeg can choose dynamic mode for zero measured LRA despite linear=true.
        # Constant-gain scaling is exactly linear: retain truthful measurements,
        # never invent a nonzero LRA to trick loudnorm or accept dynamic output.
        method = "ffmpeg_loudnorm_measured_constant_gain"
        _run([ffmpeg, "-v", "error", "-nostdin", "-n", "-protocol_whitelist", "file",
              "-format_whitelist", "wav", "-i", str(audio), "-map", "0:a:0", "-af",
              f"volume={target - values['input_i']}dB:precision=double",
              "-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s24le", "-rf64", "auto", str(output)],
             cwd=audio.parent, timeout=_programme_timeout(duration), check_continue=check_continue)
    else:
        second = measure(settings + f":measured_I={values['input_i']}:measured_TP={values['input_tp']}:"
                         f"measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}:"
                         f"offset={values['target_offset']}:linear=true:print_format=json",
                         ["-ar", str(SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-c:a", "pcm_s24le", "-rf64", "auto", str(output)])
        if second.get("normalization_type") != "linear":
            raise VideoEditingError("LINEAR_MASTER_NOT_CONFIRMED")
    verified = _measure_loudness(output, ffmpeg, duration=duration, check_continue=check_continue)
    if (verified["integrated_lufs"] is None or abs(verified["integrated_lufs"] - target) > 1
            or verified["true_peak_dbtp"] is None or verified["true_peak_dbtp"] > -1):
        raise VideoEditingError("AUDIO_MASTER_TARGET_NOT_REACHED")
    return output, {"method": method, "target_lufs": target,
                    "passes": 2, "input_measurement": values, "output_measurement": verified}


def extract_waveform(asset: Any, *, start_sec: Any = 0, end_sec: Any = None,
                     bins: int = 256, audio_stream_index: int | None = None,
                     sample_rate: int | None = None, channel_index: int | None = None,
                     video_stream_index: int | None = None,
                     check_continue: Callable[[], None] | None = None) -> dict[str, Any]:
    """Decode a bounded source interval and report real per-channel PCM bins."""
    import sys
    from array import array

    asset.validate()
    if asset.audio_stream_count == 0:
        raise VideoEditingError("WAVEFORM_AUDIO_REQUIRED")
    if audio_stream_index is None:
        if asset.audio_stream_count != 1:
            raise VideoEditingError("WAVEFORM_AUDIO_STREAM_REQUIRED")
        audio_stream_index = 0
    if type(audio_stream_index) is not int or not 0 <= audio_stream_index < asset.audio_stream_count:
        raise VideoEditingError("WAVEFORM_AUDIO_STREAM_INVALID")
    stream = _stream_metadata_by_ordinal(asset, "audio", audio_stream_index)
    if stream is None:
        raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID")
    native_rate = stream.get("sample_rate")
    source_channels = stream.get("channels")
    if (type(native_rate) is not int or native_rate < 1 or type(source_channels) is not int
            or not 1 <= source_channels <= 8):
        raise VideoEditingError("MEDIA_STREAM_METADATA_INVALID")
    stream_duration = stream.get("duration")
    try:
        duration = (_fraction_time(stream_duration, error_code="WAVEFORM_RANGE_INVALID")
                    if stream_duration is not None else _fraction_time(asset.duration_sec))
        start = _fraction_time(start_sec, error_code="WAVEFORM_RANGE_INVALID")
        end = min(duration, start + 60) if end_sec is None else _fraction_time(
            end_sec, error_code="WAVEFORM_RANGE_INVALID",
        )
    except VideoEditingError as exc:
        raise VideoEditingError("WAVEFORM_RANGE_INVALID") from exc
    if (type(bins) is not int or not 1 <= bins <= 2048 or
            not 0 <= start < end <= duration or end - start > 60):
        raise VideoEditingError("WAVEFORM_RANGE_INVALID")
    output_rate = native_rate if sample_rate is None else sample_rate
    if type(output_rate) is not int or not 1_000 <= output_rate <= 384_000:
        raise VideoEditingError("WAVEFORM_SAMPLE_RATE_INVALID")
    if channel_index is not None and (type(channel_index) is not int or not 0 <= channel_index < source_channels):
        raise VideoEditingError("WAVEFORM_CHANNEL_INDEX_INVALID")
    decoded_channels = 1 if channel_index is not None else source_channels
    expected_bytes = math.ceil(float(end - start) * output_rate) * decoded_channels * 4
    if expected_bytes > 64 * 1024 * 1024:
        raise VideoEditingError("WAVEFORM_DECODE_LIMIT_EXCEEDED")
    input_start = _stream_input_origin(asset, "audio", audio_stream_index) + start
    path = _verified_asset_path(asset)

    def verify():
        if check_continue:
            check_continue()
        current = fingerprint_local_media(
            str(path), source_material_id=asset.source_material_id, display_filename=asset.filename
        )
        if current.to_dict() != asset.to_dict():
            raise VideoEditingError("SOURCE_MEDIA_CHANGED")

    verify()
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="source-waveform-") as temporary:
        output = Path(temporary) / "decoded.f32"
        command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-threads", "1",
                   "-ss", _number(input_start), "-protocol_whitelist", "file",
                   "-format_whitelist", MEDIA_INPUT_DEMUXERS, "-i", str(path),
                   "-map", f"0:a:{audio_stream_index}", "-t", _number(end - start)]
        if channel_index is not None:
            command += ["-af", f"pan=mono|c0=c{channel_index}"]
        command += ["-ac", str(decoded_channels), "-ar", str(output_rate),
                    "-c:a", "pcm_f32le", "-f", "f32le", str(output)]
        _run(command, cwd=Path(temporary), check_continue=check_continue)
        byte_size = output.stat().st_size
        if byte_size <= 0 or byte_size > 64 * 1024 * 1024 or byte_size % (4 * decoded_channels):
            raise VideoEditingError("WAVEFORM_DECODE_LIMIT_EXCEEDED")
        samples = array("f")
        samples.frombytes(output.read_bytes())
        if sys.byteorder != "little":
            samples.byteswap()
    frame_count = len(samples) // decoded_channels
    if not frame_count or any(not math.isfinite(value) for value in samples):
        raise VideoEditingError("WAVEFORM_DECODE_INVALID")
    count = min(bins, frame_count)
    channel_results = []
    source_channel_indices = [channel_index] if channel_index is not None else list(range(source_channels))
    for output_channel, source_channel in enumerate(source_channel_indices):
        channel_bins = []
        for index in range(count):
            first = index * frame_count // count
            after = (index + 1) * frame_count // count
            chunk = samples[first * decoded_channels + output_channel:after * decoded_channels:decoded_channels]
            channel_bins.append({
                "start_sample": first,
                "end_sample": after,
                "start_sec": _rational_json(start + Fraction(first, output_rate)),
                "end_sec": _rational_json(start + Fraction(after, output_rate)),
                "min": min(chunk),
                "max": max(chunk),
                "rms": math.sqrt(sum(value * value for value in chunk) / len(chunk)),
            })
        channel_results.append({"channel_index": source_channel, "bins": channel_bins})
    verify()
    mapping = None
    if video_stream_index is not None:
        if not asset.has_video or type(video_stream_index) is not int or video_stream_index >= asset.video_stream_count:
            raise VideoEditingError("WAVEFORM_VIDEO_STREAM_INVALID")
        mapping = {
            "video_stream_start_pts": (_stream_metadata_by_ordinal(asset, "video", video_stream_index) or {}).get("start_pts"),
            "video_stream_time_base": (_stream_metadata_by_ordinal(asset, "video", video_stream_index) or {}).get("time_base"),
            "video_stream_start_time": (_stream_metadata_by_ordinal(asset, "video", video_stream_index) or {}).get("start_time"),
            "audio_to_video_offset_sec": _rational_json(
                _stream_origin(asset, "audio", audio_stream_index)
                - _stream_origin(asset, "video", video_stream_index)
            ),
        }
    return {
        "state": "measured",
        "source_material_id": asset.source_material_id,
        "source_sha256": asset.sha256,
        "audio_stream_index": audio_stream_index,
        "source_sample_rate": native_rate,
        "sample_rate": output_rate,
        "source_channels": source_channels,
        "decoded_channels": decoded_channels,
        "channel_layout": stream.get("channel_layout"),
        "selected_channel_index": channel_index,
        "start_sec": _rational_json(start),
        "end_sec": _rational_json(end),
        "source_stream_start_pts": stream.get("start_pts"),
        "source_stream_time_base": stream.get("time_base"),
        "source_stream_start_time": stream.get("start_time"),
        "format_start_time": asset.format_metadata.get("start_time"),
        "container_relative_seek_sec": _rational_json(input_start),
        "decoded_samples_per_channel": frame_count,
        "coverage": {"source_start_sec": _rational_json(start),
                     "source_end_sec": _rational_json(start + Fraction(frame_count, output_rate)),
                     "bins": count},
        "channels": channel_results,
        "video_alignment": mapping,
        "stage": "source_pre_mix",
        "method": "ffmpeg_decoded_interleaved_pcm_f32le",
    }


def _ass_colour(value: str, alpha: int = 0) -> str:
    red, green, blue = value.lstrip("#")[0:2], value.lstrip("#")[2:4], value.lstrip("#")[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def _ass_font_name() -> str:
    name = CAPTION_FONT.stem.lower()
    if name == "msyh":
        return "Microsoft YaHei"
    if "notosanscjk" in name:
        return "Noto Sans CJK SC"
    if "pingfang" in name:
        return "PingFang SC"
    return "Arial"


def chapter_document(timeline: Timeline) -> dict[str, Any]:
    """Export editable timeline chapters with exact time and source-range readback."""
    timeline.validate()
    chapters = sorted(timeline.chapters, key=lambda chapter: (chapter.start_sec, chapter.chapter_id))
    if not chapters:
        return {}
    assets = {asset.asset_id: asset for asset in timeline.assets}
    result = []
    for index, chapter in enumerate(chapters):
        if chapter.start_sec >= timeline.duration_sec:
            raise VideoEditingError("TIMELINE_CHAPTER_OUTSIDE_PROGRAMME")
        end = chapters[index + 1].start_sec if index + 1 < len(chapters) else timeline.duration_sec
        if end <= chapter.start_sec:
            raise VideoEditingError("TIMELINE_CHAPTER_RANGE_INVALID")
        source_ranges = []
        for track in timeline.tracks:
            for clip in track.clips:
                clip_start = clip.timeline_start
                clip_end = clip_start + clip.duration_sec
                visible_start = max(chapter.start_sec, clip_start)
                visible_end = min(end, clip_end)
                if visible_start >= visible_end:
                    continue
                asset = assets[clip.media_asset_id]
                source_ranges.append({
                    "clip_id": clip.clip_id,
                    "track_id": track.track_id,
                    "media_asset_id": asset.asset_id,
                    "source_material_id": asset.source_material_id,
                    "source_sha256": asset.sha256,
                    "timeline_range": [_rational_json(visible_start), _rational_json(visible_end)],
                    "source_range": [
                        _rational_json(clip.source_in + (visible_start - clip_start) * clip.playback_rate),
                        _rational_json(clip.source_in + (visible_end - clip_start) * clip.playback_rate),
                    ],
                    "audio_stream_index": clip.audio_stream_index,
                    "video_stream_index": clip.video_stream_index,
                })
        result.append({
            "chapter_id": chapter.chapter_id,
            "title": chapter.title,
            "start_sec": _rational_json(chapter.start_sec),
            "end_sec": _rational_json(end),
            "source_ranges": source_ranges,
        })
    return {
        "schema": "oevira.media.chapters.v1",
        "timeline_version": timeline.version,
        "timeline_digest": timeline._view_digest(),
        "time_reference": "rational_timeline_seconds",
        "chapters": result,
        "rights_state": "unknown",
        "publication_state": "not_performed",
    }


def subtitle_documents(timeline: Timeline, *, frame_rate: Any = None) -> dict[str, str]:
    """Export SRT/WebVTT and styled ASS cues at a selected exact frame rate."""
    with _RENDER_LOCK:
        return _subtitle_documents_locked(timeline, frame_rate=frame_rate)


def _subtitle_documents_locked(timeline: Timeline, *, frame_rate: Any = None) -> dict[str, str]:
    import html

    timeline.validate()
    rate = FRAME_RATE if frame_rate is None else _fraction_time(frame_rate, error_code="SUBTITLE_FRAME_RATE_INVALID")
    if not 1 <= rate <= 120:
        raise VideoEditingError("SUBTITLE_FRAME_RATE_INVALID")
    captions = sorted((item for item in timeline.overlays if item.kind == "caption"), key=lambda x: x.start_sec)
    if not captions:
        return {}

    def frame_at(seconds: Fraction) -> int:
        return int(_fraction_time(seconds) * rate + Fraction(1, 2))

    def frame_for_cue(seconds: Fraction) -> int:
        return math.ceil(_fraction_time(seconds) * rate)

    def stamp(frame: int, separator: str) -> str:
        milliseconds = round(Fraction(frame * 1000, 1) / rate)
        hours, rest = divmod(milliseconds, 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, ms = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{ms:03d}"

    def ass_stamp(frame: int) -> str:
        centiseconds = round(Fraction(frame * 100, 1) / rate)
        hours, rest = divmod(centiseconds, 360000)
        minutes, rest = divmod(rest, 6000)
        seconds, cs = divmod(rest, 100)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"

    srt, vtt = [], ["WEBVTT\n"]
    styles, events = [], []
    previous_end = 0
    end_limit = frame_at(timeline.duration_sec)
    for index, item in enumerate(captions, 1):
        start = frame_for_cue(item.start_sec)
        end = min(frame_for_cue(item.end_sec), end_limit)
        if start < previous_end or end <= start:
            raise VideoEditingError("SUBTITLE_TIMING_INVALID")
        previous_end = end
        text = _display_overlay_text(item)
        if "\n\n" in text:
            raise VideoEditingError("SUBTITLE_TEXT_NOT_SUPPORTED")
        literal = html.escape(text, quote=False)
        srt.append(f"{index}\n{stamp(start, ',')} --> {stamp(end, ',')}\n{literal}\n")
        vtt.append(f"{index}\n{stamp(start, '.')} --> {stamp(end, '.')}\n{literal}\n")

        style = item.style or CaptionStyle()
        style_name = f"Caption{index}"
        background_alpha = round((1 - style.background_opacity) * 255)
        alignment = 8 if style.position == "top" else 2
        margin_lr = round(1280 * style.safe_margin_ratio)
        margin_v = round(720 * style.safe_margin_ratio)
        styles.append(
            f"Style: {style_name},{_ass_font_name()},{style.font_size},"
            f"{_ass_colour(style.color)},{_ass_colour(style.color)},&H00000000,"
            f"{_ass_colour(style.background_color, background_alpha)},0,0,0,0,100,100,0,0,3,0,0,"
            f"{alignment},{margin_lr},{margin_lr},{margin_v},1"
        )
        ass_text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{ass_stamp(start)},{ass_stamp(end)},{style_name},,0,0,0,,{ass_text}"
        )

    ass = [
        "[Script Info]", "Title: OEVIRA Media captions", "ScriptType: v4.00+",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "PlayResX: 1280", "PlayResY: 720", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, "",
    ]
    return {"captions.srt": "\n".join(srt), "captions.vtt": "\n".join(vtt), "captions.ass": "\n".join(ass)}


def encode_audio_delivery(audio: Path, *, format: str, duration: float,
                          target_lufs: float | None = None,
                          sample_rate: int = 48000, audio_channels: int = 2,
                          check_continue: Callable[[], None] | None = None,
                          stream_files: bool = False) -> dict[str, Any]:
    global AUDIO_CHANNELS, SAMPLE_RATE
    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
        raise VideoEditingError("RENDER_SAMPLE_RATE_INVALID")
    if type(audio_channels) is not int or audio_channels not in {1, 2, 6, 8}:
        raise VideoEditingError("RENDER_CHANNEL_COUNT_UNSUPPORTED")
    with _RENDER_LOCK:
        old_sample_rate, old_channels = SAMPLE_RATE, AUDIO_CHANNELS
        SAMPLE_RATE, AUDIO_CHANNELS = sample_rate, audio_channels
        try:
            return _encode_audio_delivery_impl(
                audio, format=format, duration=duration, target_lufs=target_lufs,
                check_continue=check_continue, stream_files=stream_files,
            )
        finally:
            SAMPLE_RATE, AUDIO_CHANNELS = old_sample_rate, old_channels


def _encode_audio_delivery_impl(audio: Path, *, format: str, duration: float,
                                target_lufs: float | None = None,
                                check_continue: Callable[[], None] | None = None,
                                stream_files: bool = False) -> dict[str, Any]:
    """Encode and fully decode an offline delivery copy; never alter the master."""
    if format not in {"mp3", "m4a"}:
        raise VideoEditingError("AUDIO_DELIVERY_FORMAT_INVALID")
    ffmpeg, ffprobe = _ffmpeg_path(), _ffprobe_path()
    if not ffmpeg or not ffprobe:
        raise VideoEditingError("MEDIA_RENDER_TOOLS_UNAVAILABLE")
    output = audio.with_name("episode." + format)
    _run([ffmpeg,"-v","error","-nostdin","-n","-protocol_whitelist","file","-format_whitelist","wav",
          "-i",str(audio),"-map","0:a:0","-vn","-map_metadata","-1","-c:a",
          "libmp3lame" if format=='mp3' else 'aac',"-b:a","192k","-ar",str(SAMPLE_RATE),"-ac",str(AUDIO_CHANNELS),str(output)],
         cwd=audio.parent,timeout=_programme_timeout(duration),check_continue=check_continue)
    probe = _probe_json([ffprobe,"-v","error","-protocol_whitelist","file","-format_whitelist","mov,mp3",
                         "-show_streams","-show_format","-of","json",str(output)],
                        cwd=audio.parent,duration=duration,check_continue=check_continue,
                        error_code="AUDIO_DELIVERY_VERIFICATION_FAILED")
    try:
        streams=probe['streams'];stream=streams[0]
        actual_duration = float(probe['format']['duration'])
        if (len(streams)!=1 or stream['codec_type']!='audio' or stream['codec_name']!=('mp3' if format=='mp3' else 'aac')
                or int(stream['sample_rate'])!=SAMPLE_RATE or int(stream['channels'])!=AUDIO_CHANNELS
                or not math.isfinite(actual_duration) or abs(actual_duration-duration)>.1):
            raise ValueError('delivery probe mismatch')
    except (KeyError,ValueError,TypeError,IndexError) as exc:
        raise VideoEditingError('AUDIO_DELIVERY_VERIFICATION_FAILED') from exc
    _run([ffmpeg,"-v","error","-nostdin","-protocol_whitelist","file","-format_whitelist","mov,mp3",
          "-i",str(output),"-f","null","-"],cwd=audio.parent,
         timeout=_programme_timeout(duration),check_continue=check_continue)
    loudness=_measure_loudness(output,ffmpeg,duration=duration,check_continue=check_continue)
    if loudness['true_peak_dbtp'] is not None and loudness['true_peak_dbtp']>-1:
        raise VideoEditingError('AUDIO_DELIVERY_TRUE_PEAK_EXCEEDED')
    if target_lufs is not None and (loudness['integrated_lufs'] is None or abs(loudness['integrated_lufs']-target_lufs)>1):
        raise VideoEditingError('AUDIO_DELIVERY_LOUDNESS_TARGET_NOT_REACHED')
    data=output if stream_files else output.read_bytes()
    return {'files':{output.name:data},'report':{'format':format,'codec':stream['codec_name'],
            'duration_sec':actual_duration,'size_bytes':output.stat().st_size,'sha256':_hash_file(output),
            'loudness':loudness,'decode_all_frames':'passed','publish_status':'not_performed'}}


def propose_silence_edits(asset: Any, *, min_silence_sec: float = .5, threshold_db: float = -40,
                          margin_sec: float = .15, audio_stream_index: int | None = None,
                          check_continue: Callable[[], None] | None = None) -> dict[str, Any]:
    """Read-only measured candidates. No word-safety claim and no automatic edit."""
    asset.validate()
    if (any(type(x) not in (int,float) or not math.isfinite(x) for x in (min_silence_sec, threshold_db, margin_sec))
            or not .1 <= min_silence_sec <= 30 or not -80 <= threshold_db <= -10 or not 0 <= margin_sec <= 2):
        raise VideoEditingError('SILENCE_DETECTION_OPTIONS_INVALID')
    if asset.audio_stream_count == 0:
        raise VideoEditingError('SILENCE_DETECTION_AUDIO_REQUIRED')
    if audio_stream_index is None:
        if asset.audio_stream_count != 1:
            raise VideoEditingError('WAVEFORM_AUDIO_STREAM_REQUIRED')
        audio_stream_index=0
    if type(audio_stream_index) is not int or not 0 <= audio_stream_index < asset.audio_stream_count:
        raise VideoEditingError('WAVEFORM_AUDIO_STREAM_INVALID')
    path=_verified_asset_path(asset)
    def verify():
        if check_continue:check_continue()
        if fingerprint_local_media(
            str(path), source_material_id=asset.source_material_id, display_filename=asset.filename
        ).to_dict()!=asset.to_dict():raise VideoEditingError('SOURCE_MEDIA_CHANGED')
    verify();ffmpeg=_ffmpeg_path()
    if not ffmpeg:raise VideoEditingError('MEDIA_RENDER_TOOLS_UNAVAILABLE')
    with tempfile.TemporaryDirectory(prefix='silence-proposal-') as temporary:
        windows=[]
        if asset.duration_sec <= SILENCE_SCAN_WINDOW_SECONDS:
            windows.append((0.0,asset.duration_sec))
        else:
            start_sec=0.0
            while start_sec < asset.duration_sec:
                end_sec=min(start_sec+SILENCE_SCAN_WINDOW_SECONDS,asset.duration_sec)
                windows.append((start_sec,end_sec))
                if end_sec >= asset.duration_sec:
                    break
                start_sec += SILENCE_SCAN_WINDOW_STEP_SECONDS
                if len(windows) >= MAX_SILENCE_SCAN_WINDOWS:
                    raise VideoEditingError('SILENCE_DETECTION_RESOURCE_LIMIT')
        closed=[];open_at_boundary=[]
        for index,(window_start,window_end) in enumerate(windows):
            if check_continue:check_continue()
            window_duration=window_end-window_start
            report=_run([ffmpeg,'-v','info','-nostdin','-nostats','-ss',_number(window_start),
                         '-t',_number(window_duration),'-protocol_whitelist','file',
                         '-format_whitelist',MEDIA_INPUT_DEMUXERS,'-i',str(path),
                         '-map',f'0:a:{audio_stream_index}','-af',
                         f'asetpts=PTS-STARTPTS,silencedetect=noise={threshold_db}dB:d={min_silence_sec}',
                         '-f','null','-'],cwd=Path(temporary),
                         timeout=max(60,window_duration*2),check_continue=check_continue)
            current_start=None
            for kind,value in re.findall(r'silence_(start|end):\s*([-+]?\d+(?:\.\d+)?)',report):
                try:
                    relative=float(value)
                except (TypeError,ValueError,OverflowError) as exc:
                    raise VideoEditingError('SILENCE_DETECTION_RESULT_INVALID') from exc
                if not math.isfinite(relative):
                    raise VideoEditingError('SILENCE_DETECTION_RESULT_INVALID')
                relative=min(max(relative,0.0),window_duration)
                if kind=='start':
                    if current_start is None:current_start=relative
                elif current_start is not None:
                    begin=window_start+current_start;end=window_start+relative
                    if end>begin:closed.append((begin,end))
                    current_start=None
                if len(closed)+len(open_at_boundary)>MAX_SILENCE_CANDIDATES:
                    raise VideoEditingError('SILENCE_DETECTION_CANDIDATE_LIMIT_EXCEEDED')
            if current_start is not None:
                item=(window_start+current_start,window_end)
                if index == len(windows)-1:
                    closed.append(item)
                else:
                    open_at_boundary.append(item)
            if len(closed)+len(open_at_boundary)>MAX_SILENCE_CANDIDATES:
                raise VideoEditingError('SILENCE_DETECTION_CANDIDATE_LIMIT_EXCEEDED')
        # Earlier windows cannot know whether a silence continues beyond their
        # end. A later overlapping window supplies the real end; use that end
        # instead of stretching a truncated event to the chunk boundary.
        for begin,partial_end in open_at_boundary:
            continuations=[end for other_begin,end in closed
                           if other_begin <= partial_end+.02 and end >= begin-.02]
            closed.append((begin,max(continuations)) if continuations else (begin,asset.duration_sec))
        intervals=[]
        for begin,end in sorted(closed):
            if intervals and begin <= intervals[-1][1]+.02:
                intervals[-1]=(intervals[-1][0],max(intervals[-1][1],end))
            else:
                intervals.append((begin,end))
    verify()
    return {'state':'proposal','source_sha256':asset.sha256,'audio_stream_index':audio_stream_index,
            'method':'ffmpeg_silencedetect','threshold_db':threshold_db,'minimum_silence_sec':min_silence_sec,
            'margin_sec':margin_sec,'time_reference':'selected_audio_stream_elapsed_seconds',
            'scan_window_count':len(windows),
            'scan_strategy':'overlapping_bounded_windows_v1' if len(windows)>1 else 'single_bounded_window_v1',
            'scan_window_seconds':SILENCE_SCAN_WINDOW_SECONDS,
            'scan_overlap_seconds':SILENCE_SCAN_WINDOW_SECONDS-SILENCE_SCAN_WINDOW_STEP_SECONDS if len(windows)>1 else 0,
            'scan_windows':[{'start_sec':start,'end_sec':end} for start,end in windows],
            'scanned_source_duration_sec':asset.duration_sec,
            'word_alignment_verified':False,'automatic_application':False,'requires_user_review':True,
            'candidates':[{'silence_start_sec':a,'silence_end_sec':b,'suggested_remove_start_sec':a+margin_sec,
                           'suggested_remove_end_sec':b-margin_sec} for a,b in intervals if b-a>2*margin_sec]}
