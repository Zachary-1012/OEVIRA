"""Local speech tools owned by the existing Media source and transcript flow.

The runtime is installed under this product root and checked by exact version
and digest. API requests never trigger package or model downloads.
"""
from __future__ import annotations

from array import array
from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from fractions import Fraction
from typing import Any, Callable

if os.name == "nt":
    import msvcrt

from .media import _EXPECTED_FFMPEG_SHA256, _ffmpeg_path

_PRODUCT_ROOT = Path(__file__).resolve().parents[2]
_SPEECH_RUNTIME = _PRODUCT_ROOT / ".runtime" / "tools" / "media-speech-whisper-20250625"
_SPEECH_MODEL = _PRODUCT_ROOT / ".runtime" / "models" / "openai-whisper-20250625" / "base.pt"
_ESPEAK_RUNTIME = _PRODUCT_ROOT / ".runtime" / "tools" / "espeak-ng-1.52.0-cmake"
_ESPEAK_MANIFEST = _ESPEAK_RUNTIME / "media-speech-runtime.json"
WHISPER_MODEL_SHA256 = "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e"
WHISPER_VERSION = "20250625"
TORCH_VERSION = "2.14.1+cpu"
FLITE_VOICES = frozenset({"awb", "kal", "kal16", "rms", "slt"})
FLITE_LANGUAGES = frozenset({"en", "en-US"})
ESPEAK_LANGUAGE_VOICES = {
    "zh-cn": "cmn",
    "en": "en-us",
    "en-us": "en-us",
    "ko": "ko",
    "ja": "ja",
    "fr": "fr-fr",
    "de": "de",
    "es": "es",
    "pt": "pt-br",
    "pt-pt": "pt",
    "pt-br": "pt-br",
    "ru": "ru",
    "ar": "ar",
    "hi": "hi",
    "id": "id",
}
ESPEAK_LANGUAGE_LABELS = {
    "zh-cn": "zh-CN", "en": "en", "en-us": "en-US", "ko": "ko", "ja": "ja",
    "fr": "fr", "de": "de", "es": "es", "pt": "pt-BR", "pt-pt": "pt-PT",
    "pt-br": "pt-BR", "ru": "ru", "ar": "ar", "hi": "hi", "id": "id",
}
MAX_TTS_TEXT_CHARACTERS = 4000
MAX_TTS_SECONDS = 300
TTS_SAMPLE_RATE = 16000
MAX_TTS_PCM_BYTES = MAX_TTS_SECONDS * TTS_SAMPLE_RATE * 2
MAX_TTS_WAV_BYTES = MAX_TTS_PCM_BYTES + 64 * 1024
ASR_SAMPLE_RATE = 16000
ASR_CHUNK_SECONDS = 30
ASR_PADDING_SECONDS = 1.5
MAX_ASR_SECONDS = 4 * 60 * 60
MAX_ASR_PCM_BYTES = MAX_ASR_SECONDS * ASR_SAMPLE_RATE * 2
_ESPEAK_CACHE_LOCK = threading.Lock()
_ESPEAK_RUNTIME_CACHE: tuple[float, tuple[Any, ...], dict[str, Any]] | None = None
_ESPEAK_VOICE_CACHE: tuple[str, str, set[str], str] | None = None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_whisper_runtime() -> dict[str, Any]:
    """Return exact local runtime facts, failing closed on a missing or changed model."""
    python = _SPEECH_RUNTIME / "Scripts" / "python.exe"
    ffmpeg = _ffmpeg_path()
    manifest_path = _SPEECH_RUNTIME / "media-speech-runtime.json"
    if not python.is_file() or not _SPEECH_MODEL.is_file() or not manifest_path.is_file() or ffmpeg is None:
        raise MediaSpeechError("MEDIA_ASR_RUNTIME_UNAVAILABLE")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MediaSpeechError("MEDIA_ASR_RUNTIME_MANIFEST_INVALID") from exc
    if (not isinstance(manifest, dict)
            or manifest.get("schema") != "oevira.media.whisper-runtime.v1"
            or manifest.get("engine") != "openai-whisper"
            or manifest.get("engine_version") != WHISPER_VERSION
            or manifest.get("model") != "base"
            or manifest.get("model_sha256") != WHISPER_MODEL_SHA256
            or manifest.get("torch_version") != TORCH_VERSION
            or manifest.get("device") != "cpu"
            or manifest.get("torch_cuda_version") is not None
            or manifest.get("automatic_download") is not False
            or manifest.get("external_network_at_inference") is not False):
        raise MediaSpeechError("MEDIA_ASR_RUNTIME_MANIFEST_INVALID")
    if _sha256_file(_SPEECH_MODEL) != WHISPER_MODEL_SHA256:
        raise MediaSpeechError("MEDIA_ASR_MODEL_INTEGRITY_ERROR")
    return {
        "engine": "openai-whisper",
        "version": WHISPER_VERSION,
        "model": "base",
        "model_sha256": WHISPER_MODEL_SHA256,
        "device": "cpu",
        "torch": TORCH_VERSION,
        "python": str(python),
        "ffmpeg": ffmpeg,
        "manifest": str(manifest_path),
        "automatic_download": False,
        "external_network": False,
    }


def _espeak_data_tree_sha256(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_UNAVAILABLE")
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_DATA_UNAVAILABLE")
    for path in files:
        if path.is_symlink():
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_DATA_UNAVAILABLE")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def verify_espeak_runtime() -> dict[str, Any]:
    """Verify the source-built local eSpeak binary and its licensed voice data."""
    global _ESPEAK_RUNTIME_CACHE
    binary = _ESPEAK_RUNTIME / "bin" / "espeak-ng.exe"
    data_parent = _ESPEAK_RUNTIME / "share"
    data_root = data_parent / "espeak-ng-data"
    manifest_path = _ESPEAK_MANIFEST
    try:
        manifest = json.loads(_ESPEAK_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_UNAVAILABLE") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema") != "oevira.media.espeak-runtime.v1"
            or manifest.get("version") != "1.52.0"
            or manifest.get("source_commit") != "4870adfa25b1a32b4361592f1be8a40337c58d6c"
            or manifest.get("license") != "GPL-3.0-or-later"
            or not binary.is_file() or binary.is_symlink() or not data_root.is_dir()):
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_UNAVAILABLE")
    license_files = manifest.get("license_files")
    if not isinstance(license_files, list) or not license_files:
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_LICENSES_UNAVAILABLE")
    for item in license_files:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))):
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_LICENSES_UNAVAILABLE")
        license_path = (_ESPEAK_RUNTIME / item["path"]).resolve(strict=False)
        if (not license_path.is_relative_to(_ESPEAK_RUNTIME.resolve())
                or not license_path.is_file() or license_path.is_symlink()
                or _sha256_file(license_path) != item["sha256"]):
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_LICENSES_UNAVAILABLE")
    files = sorted(path for path in data_root.rglob("*") if path.is_file())
    signature = (
        binary.stat().st_size,
        binary.stat().st_mtime_ns,
        manifest_path.stat().st_size,
        manifest_path.stat().st_mtime_ns,
        tuple((path.relative_to(data_root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
              for path in files),
    )
    with _ESPEAK_CACHE_LOCK:
        if (_ESPEAK_RUNTIME_CACHE is not None
                and _ESPEAK_RUNTIME_CACHE[1] == signature
                and time.monotonic() - _ESPEAK_RUNTIME_CACHE[0] < 60):
            return dict(_ESPEAK_RUNTIME_CACHE[2])
        binary_sha256 = _sha256_file(binary)
        data_sha256 = _espeak_data_tree_sha256(data_root)
        if (binary_sha256 != manifest.get("binary_sha256")
                or data_sha256 != manifest.get("data_tree_sha256")):
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_INTEGRITY_ERROR")
        result = {
            "engine": "espeak_ng",
            "version": manifest["version"],
            "binary": binary,
            "data_parent": data_parent,
            "binary_sha256": binary_sha256,
            "data_tree_sha256": data_sha256,
            "license": manifest["license"],
            "source_commit": manifest["source_commit"],
            "license_file_count": len(license_files),
            "external_network": False,
        }
        _ESPEAK_RUNTIME_CACHE = (time.monotonic(), signature, result)
        return dict(result)


@contextmanager
def _ascii_product_root_alias():
    """Map only the current Chinese product root to a serialized temporary drive alias."""
    if os.name != "nt":
        if not str(_PRODUCT_ROOT).isascii():
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_ASCII_PATH_REQUIRED")
        yield _PRODUCT_ROOT
        return
    lock_root = _PRODUCT_ROOT / ".runtime" / "media-tmp"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / "espeak-path-alias.lock"
    alias = None
    alias_created = False
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    subst = Path(system_root) / "System32" / "subst.exe"
    try:
        with lock_path.open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                mask = ctypes.windll.kernel32.GetLogicalDrives()
                for letter in "ZYXWVUTSRQPONMLKJIHGFE":
                    bit = 1 << (ord(letter) - ord("A"))
                    if mask & bit:
                        continue
                    alias = letter + ":"
                    break
                if alias is None:
                    raise MediaSpeechError("MEDIA_TTS_ESPEAK_NO_TEMPORARY_PATH_ALIAS")
                result = subprocess.run(
                    [str(subst), alias, str(_PRODUCT_ROOT)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=10, check=False, shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode != 0:
                    raise MediaSpeechError("MEDIA_TTS_ESPEAK_PATH_ALIAS_FAILED")
                alias_created = True
                yield Path(alias + "\\")
            finally:
                if alias_created and alias is not None:
                    released = subprocess.run(
                        [str(subst), alias, "/D"], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=10,
                        check=False, shell=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if released.returncode != 0:
                        raise MediaSpeechError("MEDIA_TTS_ESPEAK_PATH_ALIAS_RELEASE_FAILED")
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    except MediaSpeechError:
        raise
    except (OSError, subprocess.SubprocessError, AttributeError) as exc:
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_PATH_ALIAS_FAILED") from exc


def _espeak_voice_inventory(runtime: dict[str, Any]) -> tuple[set[str], str]:
    global _ESPEAK_VOICE_CACHE
    binary = runtime["binary"]
    data_parent = runtime["data_parent"]
    cache_key = runtime["binary_sha256"]
    with _ESPEAK_CACHE_LOCK:
        if _ESPEAK_VOICE_CACHE is not None and _ESPEAK_VOICE_CACHE[0] == cache_key:
            return set(_ESPEAK_VOICE_CACHE[2]), _ESPEAK_VOICE_CACHE[3]
    with _ascii_product_root_alias() as root_alias:
        relative_binary = binary.relative_to(_PRODUCT_ROOT)
        relative_data = data_parent.relative_to(_PRODUCT_ROOT)
        alias_binary = root_alias / relative_binary
        alias_data_parent = root_alias / relative_data
        environment = os.environ.copy()
        environment["ESPEAK_DATA_PATH"] = str(alias_data_parent)
        try:
            version_result = subprocess.run(
                [str(alias_binary), "--version"], cwd=alias_data_parent,
                env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
                check=False, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            voices_result = subprocess.run(
                [str(alias_binary), f"--path={alias_data_parent}", "--voices"],
                cwd=alias_data_parent, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
                check=False, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_FAILED") from exc
    version_line = next((line for line in version_result.stdout.splitlines()
                         if line.startswith("eSpeak NG text-to-speech: ")), "")
    if (version_result.returncode != 0 or "1.52.0" not in version_line
            or voices_result.returncode != 0):
        raise MediaSpeechError("MEDIA_TTS_ESPEAK_RUNTIME_FAILED")
    voices = set()
    for line in voices_result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fields[1]):
            voices.add(fields[1].lower())
    with _ESPEAK_CACHE_LOCK:
        _ESPEAK_VOICE_CACHE = (cache_key, runtime["data_tree_sha256"], voices, version_line)
    return voices, version_line


def inspect_audio_packet_clock(
    path: Path,
    *,
    audio_stream_index: int,
    sample_rate: int,
    time_base: tuple[int, int] | None,
    stream_start_pts: int | None = None,
    stream_start_time: tuple[int, int] | None = None,
    check_continue: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Map packet-clock runs before analysis PCM can erase timestamp gaps."""
    from .media import _ffprobe_path

    ffprobe = _ffprobe_path()
    if ffprobe is None:
        raise MediaSpeechError("MEDIA_ASR_FFPROBE_UNAVAILABLE")
    command = [
        ffprobe, "-v", "error", "-protocol_whitelist", "file",
        "-select_streams", f"a:{audio_stream_index}", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp,nb_samples", "-of", "csv=p=0", "-i", str(path),
    ]
    try:
        output = _capture_speech_process(
            command, timeout=180, max_output_bytes=128 * 1024 * 1024,
            check_continue=check_continue,
        )
    except MediaSpeechError:
        raise
    except Exception as exc:
        raise MediaSpeechError("MEDIA_ASR_PACKET_PROBE_FAILED") from exc
    frame_count = 0
    timed_frame_count = 0
    first_pts: int | None = None
    run_start_pts: int | None = None
    run_sample_count = 0
    compact_sample_count = 0
    segments: list[dict[str, int]] = []
    gaps: list[dict[str, int]] = []
    prior_run_end_sample: int | None = None
    if time_base is not None:
        time_num, time_den = time_base
        if time_num <= 0 or time_den <= 0 or sample_rate <= 0:
            raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_INVALID")
        tolerance_ticks = 1
        if type(stream_start_pts) is int:
            origin_time = Fraction(stream_start_pts * time_num, time_den)
        elif (isinstance(stream_start_time, tuple) and len(stream_start_time) == 2
              and all(type(value) is int for value in stream_start_time)
              and stream_start_time[1] > 0):
            origin_time = Fraction(stream_start_time[0], stream_start_time[1])
        else:
            origin_time = None
    else:
        time_num, time_den, tolerance_ticks = 0, 1, 0
        origin_time = None

    def source_samples_at_pts(pts: int) -> int:
        if origin_time is None:
            raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_UNVERIFIABLE")
        return round((Fraction(pts * time_num, time_den) - origin_time) * sample_rate)

    def rounded_normalized_samples(source_samples: int) -> int:
        return round(Fraction(source_samples * ASR_SAMPLE_RATE, sample_rate))

    def close_run() -> int:
        nonlocal compact_sample_count, run_sample_count, prior_run_end_sample
        if run_start_pts is None or run_sample_count <= 0:
            raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_INVALID")
        source_start_sample = source_samples_at_pts(run_start_pts)
        if source_start_sample < 0:
            raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_INVALID")
        source_end_sample = source_start_sample + run_sample_count
        compact_start_sample = compact_sample_count
        compact_end_sample = compact_start_sample + run_sample_count
        normalized_source_start = rounded_normalized_samples(source_start_sample)
        normalized_source_end = rounded_normalized_samples(source_end_sample)
        normalized_compact_start = rounded_normalized_samples(compact_start_sample)
        normalized_compact_end = rounded_normalized_samples(compact_end_sample)
        if (source_end_sample <= source_start_sample
                or compact_end_sample <= compact_start_sample
                or normalized_source_end <= normalized_source_start
                or normalized_compact_end <= normalized_compact_start):
            raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_INVALID")
        gaps_before_samples = (source_start_sample - prior_run_end_sample) if prior_run_end_sample is not None else source_start_sample
        segments.append({
            "segment_index": len(segments),
            "source_start_pts": run_start_pts,
            "source_end_pts": round(Fraction(run_start_pts * sample_rate * time_num + run_sample_count * time_den,
                                              sample_rate * time_num)),
            "source_sample_count": run_sample_count,
            "source_start_sample": source_start_sample,
            "source_end_sample": source_end_sample,
            "compact_start_sample": compact_start_sample,
            "compact_end_sample": compact_end_sample,
            "normalized_source_start_sample": normalized_source_start,
            "normalized_source_end_sample": normalized_source_end,
            "normalized_compact_start_sample": normalized_compact_start,
            "normalized_compact_end_sample": normalized_compact_end,
            "gap_before_source_samples": max(0, gaps_before_samples),
        })
        compact_sample_count = compact_end_sample
        prior_run_end_sample = source_end_sample
        run_sample_count = 0
        return source_end_sample

    for line in output.splitlines():
        if check_continue is not None and frame_count % 4096 == 0:
            check_continue()
        fields = [part.strip() for part in line.split(",")]
        if len(fields) < 2:
            continue
        frame_count += 1
        try:
            pts = int(fields[0])
            frame_samples = int(fields[1])
        except (TypeError, ValueError):
            if time_base is not None:
                raise MediaSpeechError("MEDIA_ASR_PACKET_CLOCK_UNVERIFIABLE")
            continue
        if frame_samples <= 0:
            if time_base is not None:
                raise MediaSpeechError("MEDIA_ASR_PACKET_CLOCK_UNVERIFIABLE")
            continue
        timed_frame_count += 1
        if time_base is None:
            continue
        if first_pts is None:
            first_pts = pts
            run_start_pts = pts
            if origin_time is None:
                origin_time = Fraction(pts * time_num, time_den)
            run_sample_count = frame_samples
            continue
        assert run_start_pts is not None
        expected_start_pts = Fraction(
            run_start_pts * sample_rate * time_num + run_sample_count * time_den,
            sample_rate * time_num,
        )
        delta = Fraction(pts, 1) - expected_start_pts
        if delta < -tolerance_ticks:
            raise MediaSpeechError("MEDIA_ASR_OVERLAPPING_AUDIO_NOT_SUPPORTED")
        if delta > tolerance_ticks:
            prior_run_end_sample = close_run()
            if len(segments) >= 100000:
                raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_LIMIT_EXCEEDED")
            run_start_pts = pts
            run_sample_count = frame_samples
        else:
            run_sample_count += frame_samples
    if frame_count == 0:
        raise MediaSpeechError("MEDIA_ASR_NO_AUDIO_PACKETS")
    if time_base is None:
        return {
            "packet_count": frame_count,
            "timed_packet_count": timed_frame_count,
            "continuity_state": "source_time_base_unavailable",
            "time_base": None,
            "tolerance_ticks": 0,
            "segments": [],
            "gaps": [],
        }
    if timed_frame_count != frame_count:
        raise MediaSpeechError("MEDIA_ASR_PACKET_CLOCK_UNVERIFIABLE")
    if first_pts is None:
        raise MediaSpeechError("MEDIA_ASR_NO_AUDIO_PACKETS")
    if stream_start_pts is None and stream_start_time is None and first_pts != 0:
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_UNVERIFIABLE")
    if origin_time is None or Fraction(first_pts * time_num, time_den) < origin_time:
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_INVALID")
    prior_run_end_sample = close_run()
    gaps = []
    if segments[0]["source_start_sample"] > 0:
        gaps.append({
            "source_start_pts": stream_start_pts,
            "source_end_pts": first_pts,
            "source_start_sample": 0,
            "source_end_sample": segments[0]["source_start_sample"],
            "normalized_start_sample": 0,
            "normalized_end_sample": segments[0]["normalized_source_start_sample"],
        })
    for left, right in zip(segments, segments[1:]):
        if right["source_start_sample"] > left["source_end_sample"]:
            gaps.append({
                "source_start_pts": left["source_end_pts"],
                "source_end_pts": right["source_start_pts"],
                "source_start_sample": left["source_end_sample"],
                "source_end_sample": right["source_start_sample"],
                "normalized_start_sample": left["normalized_source_end_sample"],
                "normalized_end_sample": right["normalized_source_start_sample"],
            })
    compact_normalized_samples = rounded_normalized_samples(compact_sample_count)
    timeline_normalized_samples = rounded_normalized_samples(prior_run_end_sample)
    continuity = "verified_packet_gaps" if gaps else "verified_packet_continuity"
    if gaps and stream_start_pts is None and stream_start_time is None:
        raise MediaSpeechError("MEDIA_ASR_SOURCE_CLOCK_UNVERIFIABLE")
    return {
        "packet_count": frame_count,
        "timed_packet_count": timed_frame_count,
        "continuity_state": continuity,
        "time_base": {"num": time_num, "den": time_den},
        "stream_start_pts": stream_start_pts,
        "stream_origin_time": {"num": origin_time.numerator, "den": origin_time.denominator},
        "tolerance_ticks": tolerance_ticks,
        "compact_normalized_sample_count": compact_normalized_samples,
        "timeline_normalized_sample_count": timeline_normalized_samples,
        "normalized_sample_rate": ASR_SAMPLE_RATE,
        "segments": segments,
        "gaps": gaps,
        "gap_count": len(gaps),
    }


def preserve_audio_packet_clock_gaps(
    normalized_path: Path,
    normalized_sample_count: int,
    packet_clock: dict[str, Any],
    *,
    working_directory: Path,
    check_continue: Callable[[], None] | None = None,
) -> tuple[Path, int, dict[str, Any]]:
    """Insert verified timestamp gaps into the disposable mono ASR analysis copy."""
    result = dict(packet_clock)
    result["normalized_sample_count"] = normalized_sample_count
    if packet_clock.get("continuity_state") != "verified_packet_gaps":
        result["normalization_state"] = (
            "verified_contiguous" if packet_clock.get("continuity_state") == "verified_packet_continuity"
            else "source_time_base_unavailable"
        )
        return normalized_path, normalized_sample_count, result
    compact_expected = packet_clock.get("compact_normalized_sample_count")
    timeline_expected = packet_clock.get("timeline_normalized_sample_count")
    segments = packet_clock.get("segments")
    if (type(compact_expected) is not int or type(timeline_expected) is not int
            or not isinstance(segments, list) or not segments or timeline_expected < compact_expected):
        raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
    if timeline_expected * 2 > MAX_ASR_PCM_BYTES:
        raise MediaSpeechError("MEDIA_ASR_AUDIO_LIMIT_EXCEEDED")
    compact_delta = abs(normalized_sample_count - compact_expected)
    timeline_delta = abs(normalized_sample_count - timeline_expected)
    if compact_delta == 0 and timeline_expected > compact_expected:
        mode = "insert_clock_silence"
    elif timeline_delta == 0:
        mode = "decoder_preserved_clock_gaps"
    else:
        raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
    if mode == "decoder_preserved_clock_gaps":
        result["normalization_state"] = mode
        result["normalized_sample_count"] = normalized_sample_count
        return normalized_path, normalized_sample_count, result

    target = working_directory / "normalized-audio-clocked.pcm"
    if target.exists() or target.is_symlink():
        raise MediaSpeechError("MEDIA_ASR_CLOCKED_AUDIO_CONFLICT")
    output_samples = 0
    copy_position = 0
    timeline_position = 0
    zero_block = bytes(2 * 65536)

    def write_silence(destination, sample_count: int) -> None:
        nonlocal output_samples
        remaining = sample_count
        while remaining > 0:
            if check_continue is not None:
                check_continue()
            block_samples = min(remaining, len(zero_block) // 2)
            destination.write(zero_block[:block_samples * 2])
            output_samples += block_samples
            remaining -= block_samples

    def copy_audio(source, destination, start_sample: int, end_sample: int) -> None:
        nonlocal output_samples
        if start_sample < copy_position_holder[0] or end_sample <= start_sample:
            raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
        source.seek(start_sample * 2)
        remaining = (end_sample - start_sample) * 2
        while remaining > 0:
            if check_continue is not None:
                check_continue()
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
            destination.write(block)
            output_samples += len(block) // 2
            remaining -= len(block)
        copy_position_holder[0] = end_sample

    copy_position_holder = [0]
    created_by_this_attempt = False
    rounding_padding = 0
    try:
        with normalized_path.open("rb") as source:
            destination = target.open("xb")
            created_by_this_attempt = True
            with destination:
                for segment in segments:
                    compact_start = segment.get("normalized_compact_start_sample")
                    compact_end = segment.get("normalized_compact_end_sample")
                    timeline_start = segment.get("normalized_source_start_sample")
                    timeline_end = segment.get("normalized_source_end_sample")
                    if (any(type(value) is not int for value in (compact_start, compact_end, timeline_start, timeline_end))
                            or compact_start != copy_position_holder[0]
                            or timeline_start < timeline_position or timeline_end <= timeline_start):
                        raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
                    write_silence(destination, timeline_start - timeline_position)
                    copy_audio(source, destination, compact_start, compact_end)
                    timeline_position = timeline_end
                rounding_padding = timeline_expected - output_samples
                if rounding_padding > 0 and rounding_padding <= max(2, len(segments)):
                    write_silence(destination, rounding_padding)
                destination.flush()
                os.fsync(destination.fileno())
    except MediaSpeechError:
        if created_by_this_attempt:
            target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if created_by_this_attempt:
            target.unlink(missing_ok=True)
        raise MediaSpeechError("MEDIA_ASR_CLOCKED_AUDIO_FAILED") from exc
    if (copy_position_holder[0] != normalized_sample_count
            or output_samples != timeline_expected or rounding_padding < 0
            or output_samples * 2 > MAX_ASR_PCM_BYTES):
        target.unlink(missing_ok=True)
        raise MediaSpeechError("MEDIA_ASR_CLOCK_SEGMENT_MAPPING_UNVERIFIED")
    result["normalization_state"] = mode
    result["normalized_sample_count"] = output_samples
    result["rounding_padding_samples"] = rounding_padding
    return target, output_samples, result


def normalize_audio_channel(
    path: Path,
    *,
    audio_stream_index: int,
    audio_channel_index: int,
    sample_rate: int,
    working_directory: Path,
    check_continue: Callable[[], None] | None = None,
) -> tuple[Path, int, str]:
    """Decode one explicitly selected source channel to a bounded analysis PCM file."""
    from .media import _ffmpeg_path
    from .media_rendering import _run

    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise MediaSpeechError("MEDIA_ASR_FFMPEG_UNAVAILABLE")
    if (not path.is_file() or path.is_symlink() or not 8000 <= sample_rate <= 192000
            or audio_stream_index < 0 or audio_channel_index < 0):
        raise MediaSpeechError("MEDIA_ASR_SOURCE_INVALID")
    output = working_directory / "normalized-audio.pcm"
    try:
        log = _run(
            [
                ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n",
                "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file",
                "-i", str(path), "-map", f"0:a:{audio_stream_index}",
                "-af", f"pan=mono|c0=c{audio_channel_index},aresample={ASR_SAMPLE_RATE}:async=0",
                "-vn", "-sn", "-dn", "-map_metadata", "-1", "-ac", "1",
                "-ar", str(ASR_SAMPLE_RATE), "-c:a", "pcm_s16le", "-threads:a", "1",
                "-protocol_whitelist", "file", "-f", "s16le", output.name,
            ],
            cwd=working_directory,
            timeout=MAX_ASR_SECONDS * 3,
            check_continue=check_continue,
        )
        if not output.is_file() or output.is_symlink():
            raise MediaSpeechError("MEDIA_ASR_NORMALIZED_AUDIO_UNAVAILABLE")
        size = output.stat().st_size
        if size <= 0 or size > MAX_ASR_PCM_BYTES or size % 2:
            raise MediaSpeechError("MEDIA_ASR_AUDIO_LIMIT_EXCEEDED")
        if check_continue:
            check_continue()
    except MediaSpeechError:
        raise
    except Exception as exc:
        if check_continue is not None:
            try:
                check_continue()
            except BaseException as callback_error:
                raise callback_error
        raise MediaSpeechError("MEDIA_ASR_AUDIO_DECODE_FAILED") from exc
    version = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10, check=False, shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    version_line = next((line for line in version.stdout.splitlines()
                         if line.startswith("ffmpeg version ")), "NOT_REPORTED")
    return output, size // 2, version_line


def _capture_speech_process(
    command: list[str],
    *,
    timeout: float,
    max_output_bytes: int,
    check_continue: Callable[[], None] | None,
) -> str:
    callback_error: BaseException | None = None
    try:
        deadline = time.monotonic() + timeout
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process:
            try:
                while True:
                    if check_continue is not None:
                        try:
                            check_continue()
                        except BaseException as exc:
                            callback_error = exc
                            raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate()
                raise
    except (OSError, subprocess.SubprocessError) as exc:
        if exc is callback_error:
            raise
        raise MediaSpeechError("MEDIA_ASR_PACKET_PROBE_FAILED") from exc
    if process.returncode != 0:
        raise MediaSpeechError("MEDIA_ASR_PACKET_PROBE_FAILED")
    if len(stdout.encode("utf-8")) > max_output_bytes:
        raise MediaSpeechError("MEDIA_ASR_PACKET_PROBE_TOO_LARGE")
    return stdout


def speech_capabilities() -> dict[str, Any]:
    """Report only locally installed speech tools and voices."""
    ffmpeg = _ffmpeg_path()
    voices: list[str] = []
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-f", "lavfi", "-i", "flite=list_voices=1", "-f", "null", "-"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            voices = sorted({
                match.group(1)
                for match in re.finditer(r"\[Parsed_flite_\d+ @ [^\]]+\]\s+(\w+)\s*$", result.stderr, re.MULTILINE)
                if match.group(1) in FLITE_VOICES
            })
        except (OSError, subprocess.SubprocessError):
            voices = []
    asr: dict[str, Any]
    try:
        asr = {"available": True, **verify_whisper_runtime()}
    except MediaSpeechError as exc:
        asr = {"available": False, "state": exc.code}
    synthesis = [{
        "engine": "ffmpeg_libflite",
        "available": bool(voices),
        "voices": voices,
        "languages": sorted(FLITE_LANGUAGES) if voices else [],
        "synthetic": True,
        "naturalness": "NOT_EVALUATED",
        "pronunciation_accuracy": "NOT_EVALUATED",
        "external_network": False,
        "maximum_text_characters": MAX_TTS_TEXT_CHARACTERS,
        "maximum_audio_seconds": MAX_TTS_SECONDS,
        "limitations": ["ASCII English only", "naturalness not evaluated"],
    }]
    try:
        espeak = verify_espeak_runtime()
        espeak_voices, _ = _espeak_voice_inventory(espeak)
        language_voices = {
            language: voice for language, voice in ESPEAK_LANGUAGE_VOICES.items()
            if voice in espeak_voices
        }
        synthesis.append({
            "engine": "espeak_ng",
            "available": bool(language_voices),
            "version": espeak["version"],
            "binary_sha256": espeak["binary_sha256"],
            "license": espeak["license"],
            "license_file_count": espeak["license_file_count"],
            "languages": sorted({ESPEAK_LANGUAGE_LABELS[key] for key in language_voices}),
            "voices": sorted(set(language_voices.values())),
            "language_voice_map": {
                ESPEAK_LANGUAGE_LABELS[language]: voice
                for language, voice in sorted(language_voices.items())
            },
            "synthetic": True,
            "method": "formant_non_neural",
            "naturalness": "NOT_EVALUATED_EXPECT_MECHANICAL_VOICE",
            "pronunciation_accuracy": "NOT_EVALUATED",
            "external_network": False,
            "maximum_text_characters": MAX_TTS_TEXT_CHARACTERS,
            "maximum_audio_seconds": MAX_TTS_SECONDS,
            "language_specific_limitations": {
                "ar": "eSpeak Arabic requires fully diacritized input for intended pronunciation.",
                "ja": "This eSpeak Japanese voice supports hiragana and katakana; kanji reading is not inferred.",
            },
            "limitations": ["formant voice; naturalness not evaluated", "voice support is language specific"],
        })
    except MediaSpeechError as exc:
        synthesis.append({"engine": "espeak_ng", "available": False, "state": exc.code})
    return {
        "synthesis": synthesis,
        "recognition": [asr],
    }


def _read_wav_pcm(audio: bytes) -> tuple[bytes, int]:
    try:
        if (len(audio) < 44 or len(audio) > MAX_TTS_WAV_BYTES or audio[:4] != b"RIFF"
                or audio[8:12] != b"WAVE" or struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio)):
            raise ValueError("invalid WAV container")
        with wave.open(io.BytesIO(audio), "rb") as sound:
            count = sound.getnframes()
            if (sound.getnchannels() != 1 or sound.getsampwidth() != 2
                    or sound.getframerate() != TTS_SAMPLE_RATE or sound.getcomptype() != "NONE"
                    or count <= 0 or count > MAX_TTS_SECONDS * TTS_SAMPLE_RATE):
                raise ValueError("unsupported PCM format")
            pcm = sound.readframes(count + 1)
        if len(pcm) != count * 2:
            raise ValueError("incomplete PCM data")
        return pcm, count
    except (EOFError, OSError, ValueError, ZeroDivisionError, struct.error, wave.Error) as exc:
        raise MediaSpeechError("MEDIA_TTS_OUTPUT_INVALID") from exc


def _embed_tts_source(audio: bytes, report: dict[str, Any]) -> bytes:
    pcm, count = _read_wav_pcm(audio)
    declaration = {
        "schema": "oevira.media.speech-source.v1",
        "synthetic": True,
        "engine": report["engine"],
        "engine_version": report["runtime"].get("engine_version", "NOT_REPORTED"),
        "engine_sha256": report["runtime"].get("engine_sha256"),
        "engine_license": report["runtime"].get("engine_license", "NOT_REPORTED"),
        "voice": report["voice"],
        "language": report["language"],
        "text": report["source"]["text"],
        "text_sha256": report["source"]["sha256"],
        "pcm_sha256": _sha256_bytes(pcm),
        "samples": count,
        "identity_state": "file_embedded_declaration",
        "canonical_fact": False,
        "rights_state": "caller_must_verify",
    }
    comment = b"OEVIRA-MEDIA-SPEECH-V1\n" + json.dumps(
        declaration, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\0"
    if len(comment) > 48 * 1024:
        raise MediaSpeechError("MEDIA_TTS_SOURCE_DECLARATION_INVALID")
    info = b"INFOICMT" + struct.pack("<I", len(comment)) + comment
    if len(comment) % 2:
        info += b"\0"
    chunk = b"LIST" + struct.pack("<I", len(info)) + info
    if len(info) % 2:
        chunk += b"\0"
    result = audio + chunk
    result = result[:4] + struct.pack("<I", len(result) - 8) + result[8:]
    if _read_tts_source(result) != declaration:
        raise MediaSpeechError("MEDIA_TTS_SOURCE_DECLARATION_INVALID")
    return result


def _read_tts_source(audio: bytes) -> dict[str, Any] | None:
    if len(audio) > MAX_TTS_WAV_BYTES + 64 * 1024 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    found = None
    position = 12
    try:
        while position + 8 <= len(audio):
            kind, size = struct.unpack_from("<4sI", audio, position)
            end = position + 8 + size
            if end > len(audio):
                raise ValueError("chunk extends beyond WAV")
            if kind == b"LIST" and audio[position + 8:position + 12] == b"INFO":
                item = position + 12
                while item + 8 <= end:
                    tag, length = struct.unpack_from("<4sI", audio, item)
                    stop = item + 8 + length
                    if stop > end:
                        raise ValueError("INFO field extends beyond WAV")
                    prefix = b"OEVIRA-MEDIA-SPEECH-V1\n"
                    if tag == b"ICMT" and audio[item + 8:item + 8 + len(prefix)] == prefix:
                        if found is not None or length > 48 * 1024:
                            raise ValueError("duplicate or oversized declaration")
                        found = json.loads(audio[item + 8 + len(prefix):stop].rstrip(b"\0").decode("ascii"))
                    item = stop + length % 2
            position = end + size % 2
        if found is None:
            return None
        pcm, count = _read_wav_pcm(audio[:struct.unpack_from("<I", audio, 4)[0] + 8])
        base_fields = {
            "schema", "synthetic", "engine", "voice", "language", "text", "text_sha256",
            "pcm_sha256", "samples", "identity_state", "canonical_fact", "rights_state",
        }
        extended_fields = base_fields | {"engine_version", "engine_sha256", "engine_license"}
        if (not isinstance(found, dict)
                or frozenset(found) not in {frozenset(base_fields), frozenset(extended_fields)}
                or found["schema"] != "oevira.media.speech-source.v1" or found["synthetic"] is not True
                or found["canonical_fact"] is not False or found["identity_state"] != "file_embedded_declaration"
                or found["rights_state"] != "caller_must_verify"
                or not isinstance(found["text"], str) or not found["text"].strip()
                or hashlib.sha256(found["text"].encode("utf-8")).hexdigest() != found["text_sha256"]
                or _sha256_bytes(pcm) != found["pcm_sha256"]
                or type(found["samples"]) is not int or found["samples"] != count):
            raise ValueError("declaration does not match source audio")
        if "engine_version" in found and (
            not isinstance(found["engine_version"], str)
            or not isinstance(found["engine_license"], str)
            or (found["engine_sha256"] is not None and not re.fullmatch(r"[0-9a-f]{64}", found["engine_sha256"]))
        ):
            raise ValueError("engine provenance is invalid")
        if found["engine"] == "ffmpeg_libflite":
            _validate_flite_input(found["text"], found["voice"], found["language"])
        elif found["engine"] == "espeak_ng":
            _, canonical_language, selected_voice = _validate_espeak_input(
                found["text"], found["voice"], found["language"],
            )
            if selected_voice != found["voice"] or canonical_language != found["language"]:
                raise ValueError("eSpeak voice does not match its declared language")
        else:
            raise ValueError("speech engine is unknown")
        return found
    except (EOFError, OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError,
            struct.error, wave.Error, MediaSpeechError) as exc:
        raise MediaSpeechError("MEDIA_TTS_SOURCE_DECLARATION_INVALID") from exc


def _validate_flite_input(text: Any, voice: Any, language: Any) -> bytes:
    if not isinstance(language, str) or language not in FLITE_LANGUAGES:
        raise MediaSpeechError("MEDIA_TTS_LANGUAGE_UNAVAILABLE")
    if not isinstance(voice, str) or voice not in FLITE_VOICES:
        raise MediaSpeechError("MEDIA_TTS_VOICE_UNAVAILABLE")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TTS_TEXT_CHARACTERS
            or any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in text)
            or not text.isascii() or not any(char.isalnum() for char in text)):
        raise MediaSpeechError("MEDIA_TTS_LANGUAGE_UNAVAILABLE" if isinstance(text, str) and not text.isascii()
                               else "MEDIA_TTS_TEXT_INVALID")
    return text.encode("ascii")


def _validate_espeak_input(text: Any, voice: Any, language: Any) -> tuple[bytes, str, str]:
    if not isinstance(language, str):
        raise MediaSpeechError("MEDIA_TTS_LANGUAGE_UNAVAILABLE")
    canonical_language = ESPEAK_LANGUAGE_LABELS.get(language.strip().casefold())
    selected_voice = ESPEAK_LANGUAGE_VOICES.get(language.strip().casefold())
    if canonical_language is None or selected_voice is None:
        raise MediaSpeechError("MEDIA_TTS_LANGUAGE_UNAVAILABLE")
    if voice is not None and voice != selected_voice:
        raise MediaSpeechError("MEDIA_TTS_VOICE_UNAVAILABLE")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TTS_TEXT_CHARACTERS
            or any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in text)
            or any(marker in text for marker in ("[[", "]]", "<", ">"))):
        raise MediaSpeechError("MEDIA_TTS_TEXT_INVALID")
    return text.encode("utf-8"), canonical_language, selected_voice


def synthesize_flite_speech(
    text: str,
    *,
    voice: str = "slt",
    language: str = "en",
    check_continue: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Synthesize a complete, explicitly English synthetic WAV from the pinned local FFmpeg."""
    if check_continue:
        check_continue()
    text_bytes = _validate_flite_input(text, voice, language)
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise MediaSpeechError("MEDIA_TTS_RUNTIME_UNAVAILABLE")
    runtime_temp = _PRODUCT_ROOT / ".runtime" / "media-tmp" / "speech-synthesis"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="media-tts-", dir=runtime_temp) as directory:
            work = Path(directory)
            text_path = work / "speech.txt"
            wav_path = work / "speech.wav"
            decoded_path = work / "decoded.pcm"
            with text_path.open("xb") as stream:
                stream.write(text_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if os.name != "nt":
                    text_path.chmod(0o600)
                ffmpeg_log = _run_speech_process(
                    [ffmpeg, "-hide_banner", "-v", "info", "-xerror", "-nostdin", "-n",
                     "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file",
                     "-f", "lavfi", "-i", f"flite=textfile=speech.txt:voice={voice}",
                     "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                     "-ac", "1", "-ar", str(TTS_SAMPLE_RATE), "-c:a", "pcm_s16le",
                     "-threads:a", "1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                     "-protocol_whitelist", "file", "-f", "wav", "speech.wav"],
                    cwd=work, timeout=120, check_continue=check_continue,
                )
                if not wav_path.is_file() or wav_path.stat().st_size > MAX_TTS_WAV_BYTES:
                    raise MediaSpeechError("MEDIA_TTS_OUTPUT_LIMIT_EXCEEDED")
                audio = wav_path.read_bytes()
                pcm, count = _read_wav_pcm(audio)
                _run_speech_process(
                    [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n",
                     "-threads", "1", "-err_detect", "explode", "-protocol_whitelist", "file",
                     "-format_whitelist", "wav", "-i", "speech.wav", "-map", "0:a:0",
                     "-vn", "-sn", "-dn", "-map_metadata", "-1", "-c:a", "pcm_s16le",
                     "-threads:a", "1", "-protocol_whitelist", "file", "-f", "s16le", "decoded.pcm"],
                    cwd=work, timeout=30, check_continue=check_continue,
                )
                if not decoded_path.is_file() or decoded_path.read_bytes() != pcm:
                    raise MediaSpeechError("MEDIA_TTS_DECODE_MISMATCH")
            except MediaSpeechError:
                raise
            except (OSError, subprocess.SubprocessError) as exc:
                raise MediaSpeechError("MEDIA_TTS_SYNTHESIS_FAILED") from exc
            samples = array("h")
            samples.frombytes(pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            peak = max(abs(value) for value in samples)
            if peak == 0:
                raise MediaSpeechError("MEDIA_TTS_AUDIO_SILENT")
            report = {
                "schema": "oevira.media.speech-source-report.v1",
                "engine": "ffmpeg_libflite",
                "runtime": {
                    "engine_version": next(
                        (line for line in ffmpeg_log.splitlines() if line.startswith("ffmpeg version ")),
                        "NOT_REPORTED",
                    ),
                    "engine_sha256": _EXPECTED_FFMPEG_SHA256.lower(),
                    "engine_license": "FFmpeg GPL build; linked Flite has a permissive upstream license; exact linked Flite version NOT_REPORTED",
                    "flite_library_version": "NOT_REPORTED",
                },
                "voice": voice,
                "language": language,
                "voice_language": "en-US",
                "synthetic": True,
                "voice_cloned": False,
                "external_network": False,
                "source": {
                    "kind": "user_supplied_text",
                    "text": text,
                    "sha256": _sha256_bytes(text_bytes),
                    "characters": len(text),
                    "rights_state": "caller_must_verify",
                    "canonical_fact": False,
                },
                "word_boundaries": "unavailable",
                "naturalness": "NOT_EVALUATED",
                "pronunciation_accuracy": "NOT_EVALUATED",
                "published": False,
                "audio": {
                    "codec": "pcm_s16le", "sample_rate": TTS_SAMPLE_RATE,
                    "channels": 1, "samples_per_channel": count,
                    "duration_sec": {"num": count, "den": TTS_SAMPLE_RATE},
                    "sample_peak": peak / 32768,
                    "clipped_samples": sum(value in (-32768, 32767) for value in samples),
                    "true_peak": "NOT_MEASURED", "loudness_lufs": "NOT_MEASURED",
                    "pcm_sha256": _sha256_bytes(pcm), "full_decode": "passed",
                    "full_pcm_comparison": "passed",
                },
                "limits": {
                    "supported_text": "ASCII English only",
                    "maximum_text_characters": MAX_TTS_TEXT_CHARACTERS,
                    "maximum_audio_seconds": MAX_TTS_SECONDS,
                    "limit_behavior": "reject_complete_result_without_truncation",
                },
                "references": {
                    "ffmpeg_filter": "https://ffmpeg.org/ffmpeg-filters.html#flite",
                    "flite_license": "https://github.com/festvox/flite/blob/v2.2/COPYING",
                },
            }
            output = _embed_tts_source(audio, report)
            report["audio"]["wav_sha256"] = _sha256_bytes(output)
            report["audio"]["wav_size_bytes"] = len(output)
            report["audio"]["source_declaration"] = _read_tts_source(output)
            if check_continue:
                check_continue()
            return {"audio": output, "report": report}
    except MediaSpeechError:
        raise
    except OSError as exc:
        raise MediaSpeechError("MEDIA_TTS_STORAGE_FAILED") from exc


def synthesize_espeak_speech(
    text: str,
    *,
    language: str,
    voice: str | None = None,
    check_continue: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Synthesize one explicit local formant voice from the pinned eSpeak NG runtime."""
    if check_continue:
        check_continue()
    text_bytes, canonical_language, selected_voice = _validate_espeak_input(text, voice, language)
    runtime = verify_espeak_runtime()
    voices, version_line = _espeak_voice_inventory(runtime)
    if selected_voice not in voices:
        raise MediaSpeechError("MEDIA_TTS_VOICE_UNAVAILABLE")
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise MediaSpeechError("MEDIA_TTS_RUNTIME_UNAVAILABLE")
    runtime_temp = _PRODUCT_ROOT / ".runtime" / "media-tmp" / "speech-synthesis"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="media-espeak-", dir=runtime_temp) as directory:
            work = Path(directory)
            source = work / "speech.txt"
            native_wav = work / "speech-native.wav"
            normalized_wav = work / "speech.wav"
            decoded_pcm = work / "decoded.pcm"
            with source.open("xb") as stream:
                stream.write(text_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                with _ascii_product_root_alias() as alias_root:
                    alias_work = alias_root / work.relative_to(_PRODUCT_ROOT)
                    relative_binary = runtime["binary"].relative_to(_PRODUCT_ROOT)
                    relative_data = runtime["data_parent"].relative_to(_PRODUCT_ROOT)
                    binary = alias_root / relative_binary
                    data_parent = alias_root / relative_data
                    environment = os.environ.copy()
                    environment["ESPEAK_DATA_PATH"] = str(data_parent)
                    _run_speech_process(
                        [str(binary), f"--path={data_parent}", "-v", selected_voice,
                         "-b", "1", "-s", "175", "-f", "speech.txt", "-w", "speech-native.wav"],
                        cwd=alias_work, timeout=120, check_continue=check_continue,
                        environment=environment,
                    )
                    if not native_wav.is_file() or native_wav.stat().st_size > 30 * 1024 * 1024:
                        raise MediaSpeechError("MEDIA_TTS_OUTPUT_LIMIT_EXCEEDED")
                    try:
                        with wave.open(str(native_wav), "rb") as audio:
                            native_rate = audio.getframerate()
                            native_channels = audio.getnchannels()
                            native_width = audio.getsampwidth()
                            native_count = audio.getnframes()
                            if (native_rate <= 0 or native_channels != 1 or native_width != 2
                                    or not 0 < native_count / native_rate <= MAX_TTS_SECONDS):
                                raise MediaSpeechError("MEDIA_TTS_OUTPUT_INVALID")
                    except (wave.Error, OSError, EOFError) as exc:
                        raise MediaSpeechError("MEDIA_TTS_OUTPUT_INVALID") from exc
                    _run_speech_process(
                        [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n",
                         "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file",
                         "-format_whitelist", "wav", "-i", "speech-native.wav", "-map", "0:a:0",
                         "-vn", "-sn", "-dn", "-map_metadata", "-1", "-ac", "1", "-ar",
                         str(TTS_SAMPLE_RATE), "-c:a", "pcm_s16le", "-threads:a", "1",
                         "-fflags", "+bitexact", "-flags:a", "+bitexact",
                         "-protocol_whitelist", "file", "-f", "wav", "speech.wav"],
                        cwd=alias_work, timeout=60, check_continue=check_continue,
                    )
                    if not normalized_wav.is_file() or normalized_wav.stat().st_size > MAX_TTS_WAV_BYTES:
                        raise MediaSpeechError("MEDIA_TTS_OUTPUT_LIMIT_EXCEEDED")
                    audio = normalized_wav.read_bytes()
                    pcm, count = _read_wav_pcm(audio)
                    _run_speech_process(
                        [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-n",
                         "-threads", "1", "-err_detect", "explode", "-protocol_whitelist", "file",
                         "-format_whitelist", "wav", "-i", "speech.wav", "-map", "0:a:0",
                         "-vn", "-sn", "-dn", "-map_metadata", "-1", "-c:a", "pcm_s16le",
                         "-threads:a", "1", "-protocol_whitelist", "file", "-f", "s16le", "decoded.pcm"],
                        cwd=alias_work, timeout=30, check_continue=check_continue,
                    )
                    if not decoded_pcm.is_file() or decoded_pcm.read_bytes() != pcm:
                        raise MediaSpeechError("MEDIA_TTS_DECODE_MISMATCH")
            except MediaSpeechError:
                raise
            except (OSError, subprocess.SubprocessError) as exc:
                raise MediaSpeechError("MEDIA_TTS_SYNTHESIS_FAILED") from exc
            samples = array("h")
            samples.frombytes(pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            peak = max(abs(value) for value in samples)
            if peak == 0:
                raise MediaSpeechError("MEDIA_TTS_AUDIO_SILENT")
            report = {
                "schema": "oevira.media.speech-source-report.v1",
                "engine": "espeak_ng",
                "runtime": {
                    "engine_version": runtime["version"],
                    "engine_version_line": version_line,
                    "engine_sha256": runtime["binary_sha256"],
                    "data_tree_sha256": runtime["data_tree_sha256"],
                    "source_commit": runtime["source_commit"],
                    "engine_license": "GPL-3.0-or-later; UCD, BSD-2-Clause and Apache notices are retained with the local runtime",
                },
                "voice": selected_voice,
                "language": canonical_language,
                "voice_language": canonical_language,
                "synthesis_method": "formant_non_neural",
                "synthetic": True,
                "voice_cloned": False,
                "external_network": False,
                "source": {
                    "kind": "user_supplied_text",
                    "text": text,
                    "sha256": _sha256_bytes(text_bytes),
                    "characters": len(text),
                    "rights_state": "caller_must_verify",
                    "canonical_fact": False,
                },
                "word_boundaries": "unavailable",
                "naturalness": "NOT_EVALUATED_EXPECT_MECHANICAL_VOICE",
                "pronunciation_accuracy": "NOT_EVALUATED",
                "published": False,
                "audio": {
                    "codec": "pcm_s16le", "sample_rate": TTS_SAMPLE_RATE,
                    "channels": 1, "samples_per_channel": count,
                    "duration_sec": {"num": count, "den": TTS_SAMPLE_RATE},
                    "sample_peak": peak / 32768,
                    "clipped_samples": sum(value in (-32768, 32767) for value in samples),
                    "true_peak": "NOT_MEASURED", "loudness_lufs": "NOT_MEASURED",
                    "pcm_sha256": _sha256_bytes(pcm), "full_decode": "passed",
                    "full_pcm_comparison": "passed",
                },
                "limits": {
                    "supported_text": "Unicode text for the explicitly selected local eSpeak voice",
                    "maximum_text_characters": MAX_TTS_TEXT_CHARACTERS,
                    "maximum_audio_seconds": MAX_TTS_SECONDS,
                    "limit_behavior": "reject_complete_result_without_truncation",
                    "language_specific": {
                        "ar": "Fully diacritized input is required for intended Arabic pronunciation.",
                        "ja": "This voice supports hiragana and katakana; kanji reading is not inferred.",
                    },
                },
                "references": {
                    "source": "https://github.com/espeak-ng/espeak-ng/tree/1.52.0",
                    "languages": "https://github.com/espeak-ng/espeak-ng/blob/1.52.0/docs/languages.md",
                    "license": "https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING",
                },
            }
            output = _embed_tts_source(audio, report)
            report["audio"]["wav_sha256"] = _sha256_bytes(output)
            report["audio"]["wav_size_bytes"] = len(output)
            report["audio"]["source_declaration"] = _read_tts_source(output)
            if check_continue:
                check_continue()
            return {"audio": output, "report": report}
    except MediaSpeechError:
        raise
    except OSError as exc:
        raise MediaSpeechError("MEDIA_TTS_STORAGE_FAILED") from exc


def _run_speech_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    check_continue: Callable[[], None] | None,
    environment: dict[str, str] | None = None,
) -> str:
    from .media_rendering import _run
    try:
        return _run(
            command, cwd=cwd, timeout=timeout, check_continue=check_continue,
            environment=environment,
        )
    except Exception as exc:
        if check_continue is not None:
            try:
                check_continue()
            except BaseException as callback_error:
                raise callback_error
        raise MediaSpeechError("MEDIA_TTS_PROCESS_FAILED") from exc


class MediaSpeechError(RuntimeError):
    """A bounded local speech operation failed without altering its source project."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
