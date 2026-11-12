"""Isolated, CPU-only Whisper process for one Media transcription job."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys


SAMPLE_RATE = 16000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit(event_path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    with event_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--language")
    parser.add_argument("--task", required=True, choices=("transcribe", "translate"))
    parser.add_argument("--chunk-seconds", type=int, default=30)
    parser.add_argument("--padding-seconds", type=float, default=1.5)
    parser.add_argument("--start-chunk", type=int, default=0)
    parser.add_argument("--threads", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    phase = "preflight"
    model_path = Path(args.model)
    audio_path = Path(args.audio)
    event_path = Path(args.events)
    if (not model_path.is_file() or not audio_path.is_file()
            or not event_path.is_file()
            or _sha256(model_path) != args.model_sha256
            or args.chunk_seconds != 30
            or not 0 <= args.padding_seconds <= 5
            or args.start_chunk < 0 or not 1 <= args.threads <= 4
            or args.task not in {"transcribe", "translate"}):
        sys.stderr.write("MEDIA_ASR_INPUT_INVALID\n")
        return 2

    try:
        phase = "imports"
        import numpy as np
        import torch
        import whisper

        if torch.cuda.is_available() or torch.version.cuda is not None:
            sys.stderr.write("MEDIA_ASR_CPU_RUNTIME_REQUIRED\n")
            return 2
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        phase = "model_load"
        model = whisper.load_model(str(model_path), device="cpu")
        phase = "language_validation"
        if args.language is not None:
            languages = getattr(whisper.tokenizer, "LANGUAGES", {})
            if args.language not in languages:
                sys.stderr.write("MEDIA_ASR_LANGUAGE_UNSUPPORTED\n")
                return 2
        total_bytes = audio_path.stat().st_size
        if total_bytes <= 0 or total_bytes % 2:
            sys.stderr.write("MEDIA_ASR_NORMALIZED_AUDIO_INVALID\n")
            return 2
        total_samples = total_bytes // 2
        chunk_samples = args.chunk_seconds * SAMPLE_RATE
        total_chunks = math.ceil(total_samples / chunk_samples)
        padding_samples = round(args.padding_seconds * SAMPLE_RATE)
        _emit(event_path, {
            "type": "ready",
            "engine": "openai-whisper",
            "version": whisper.__version__,
            "model": "base",
            "model_sha256": args.model_sha256,
            "task": args.task,
            "torch": torch.__version__,
            "sample_rate": SAMPLE_RATE,
            "total_samples": total_samples,
            "total_chunks": total_chunks,
            "start_chunk": args.start_chunk,
            "device": "cpu",
            "threads": args.threads,
        })

        phase = "audio_mapping"
        audio = np.memmap(audio_path, dtype="<i2", mode="r", shape=(total_samples,))
        processed = 0
        language_counts: dict[str, int] = {}
        for chunk_index in range(args.start_chunk, total_chunks):
            core_start = chunk_index * chunk_samples
            core_end = min(total_samples, core_start + chunk_samples)
            read_start = max(0, core_start - padding_samples)
            read_end = min(total_samples, core_end + padding_samples)
            pcm = np.asarray(audio[read_start:read_end], dtype=np.float32)
            pcm *= 1.0 / 32768.0
            phase = "inference"
            result = model.transcribe(
                pcm,
                language=args.language,
                task=args.task,
                word_timestamps=True,
                fp16=False,
                verbose=False,
                temperature=0,
            )
            language = result.get("language")
            if not isinstance(language, str) or len(language) > 16:
                language = "und"
            language_counts[language] = language_counts.get(language, 0) + 1
            words = []
            segments = []
            phase = "word_projection"
            for segment_index, segment in enumerate(result.get("segments", [])):
                segment_start = read_start + round(float(segment.get("start", 0.0)) * SAMPLE_RATE)
                segment_end = read_start + round(float(segment.get("end", 0.0)) * SAMPLE_RATE)
                segments.append({
                    "segment_index": segment_index,
                    "start_sample": max(read_start, min(read_end, segment_start)),
                    "end_sample": max(read_start, min(read_end, segment_end)),
                    "text": str(segment.get("text", "")),
                })
                for raw_word in segment.get("words", []):
                    start = read_start + round(float(raw_word.get("start", 0.0)) * SAMPLE_RATE)
                    end = read_start + round(float(raw_word.get("end", 0.0)) * SAMPLE_RATE)
                    start = max(read_start, min(read_end, start))
                    end = max(read_start, min(read_end, end))
                    if end <= start:
                        continue
                    midpoint = (start + end) // 2
                    if not core_start <= midpoint < core_end:
                        continue
                    probability = raw_word.get("probability")
                    if (type(probability) not in (int, float)
                            or not math.isfinite(probability) or not 0 <= probability <= 1):
                        probability = None
                    words.append({
                        "normalized_start_sample": start,
                        "normalized_end_sample": end,
                        "text": str(raw_word.get("word", "")),
                        "token_probability": probability,
                    })
            phase = "chunk_persistence"
            _emit(event_path, {
                "type": "chunk",
                "chunk_index": chunk_index,
                "core_start_sample": core_start,
                "core_end_sample": core_end,
                "read_start_sample": read_start,
                "read_end_sample": read_end,
                "language": language,
                "text": str(result.get("text", "")),
                "segments": segments,
                "words": words,
            })
            processed += 1

        phase = "completion_event"
        _emit(event_path, {
            "type": "complete",
            "processed_chunks": processed,
            "total_chunks": total_chunks,
            "language_counts": language_counts,
        })
        return 0
    except Exception as exc:
        sys.stderr.write("MEDIA_ASR_WORKER_FAILED\n")
        try:
            _emit(event_path, {
                "type": "error",
                "code": "MEDIA_ASR_WORKER_FAILED",
                "phase": phase,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:256],
            })
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
