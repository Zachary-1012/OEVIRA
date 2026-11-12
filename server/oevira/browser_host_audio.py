"""Audio from one isolated Chromium process tree, without desktop loopback."""
from __future__ import annotations

import asyncio
from fractions import Fraction
import json
import hashlib
import os
from pathlib import Path
import sys
import uuid


def browser_audio_track(stream):
    """Return an audio track only when the owned Windows capture helper exists."""
    if os.name != "nt":
        return None
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(sys.executable).parent / "oevira_browser_audio.exe",
                  root / "app/build/windows/x64/runner/Release/oevira_browser_audio.exe"]
    helper = next((path for path in candidates if path.is_file()), None)
    if helper is None:
        return None
    if helper.is_relative_to(root / "app/build"):
        # Development verification must not lock the shared linker output.
        content = helper.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        folder = root / ".runtime/browser-host/audio-bin" / digest
        folder.mkdir(parents=True, exist_ok=True)
        runtime_helper = folder / "oevira_browser_audio.exe"
        if not runtime_helper.exists():
            staged = folder / f".audio-{uuid.uuid4().hex}.exe"
            staged.write_bytes(content)
            try:
                os.link(staged, runtime_helper)
            except FileExistsError:
                pass
            finally:
                staged.unlink(missing_ok=True)
        if hashlib.sha256(runtime_helper.read_bytes()).hexdigest() != digest:
            return None
        helper = runtime_helper
    worker = stream.worker

    def process_id():
        # The manager gives each session a separate browser process. Do not
        # capture if an old/custom runtime still shares that process.
        if set(worker.sessions) != {stream.session_id}:
            return None
        cdp = worker.browser.new_browser_cdp_session()
        try:
            processes = cdp.send("SystemInfo.getProcessInfo").get("processInfo", [])
            matches = [item["id"] for item in processes if item.get("type") == "browser"]
            return int(matches[0]) if len(matches) == 1 else None
        finally:
            cdp.detach()

    pid = worker.call(process_id)
    if pid is None:
        return None

    from aiortc import AudioStreamTrack, MediaStreamError
    from av import AudioFrame

    class ChromiumAudio(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.process = None
            self.samples = 0
            self.diagnostics = None
            self._cleanup_task = None
            self._start_task = None
            self._reader_task = None
            self._frames = asyncio.Queue(maxsize=3)
            self._captured_samples = 0
            self.dropped_frames = 0

        async def _start_capture(self):
            self.process = await asyncio.create_subprocess_exec(
                str(helper), "--pid", str(pid),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000,
            )
            if self.readyState != "live":
                self.process.terminate()
                await self.process.wait()
                raise MediaStreamError
            line = await asyncio.wait_for(self.process.stderr.readline(), 8)
            ready = json.loads(line)
            if ready.get("event") != "ready":
                raise MediaStreamError
            self.diagnostics = asyncio.create_task(self._drain_diagnostics())
            self._reader_task = asyncio.create_task(self._read_pcm())

        async def _read_pcm(self):
            try:
                while self.readyState == "live":
                    raw = await asyncio.wait_for(self.process.stdout.readexactly(3840), 5)
                    frame = (self._captured_samples, raw)
                    self._captured_samples += 960
                    if self._frames.full():
                        self._frames.get_nowait()
                        self.dropped_frames += 1
                    self._frames.put_nowait(frame)
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                while not self._frames.empty():
                    self._frames.get_nowait()
                self._frames.put_nowait(None)

        async def recv(self):
            if self.readyState != "live" or not await asyncio.to_thread(stream._session_is_live):
                raise MediaStreamError
            try:
                if self._start_task is None:
                    self._start_task = asyncio.create_task(self._start_capture())
                await asyncio.shield(self._start_task)
                if self.readyState != "live":
                    raise MediaStreamError
                captured = await asyncio.wait_for(self._frames.get(), 5)
                if captured is None:
                    raise MediaStreamError
                pts, raw = captured
                if self.samples == 0:
                    stream._emit({"type": "audio", "available": True, "enabled": False})
                frame = AudioFrame(format="s16", layout="stereo", samples=960)
                frame.planes[0].update(raw)
                frame.sample_rate = 48000
                frame.time_base = Fraction(1, 48000)
                frame.pts = pts
                self.samples = pts + 960
                return frame
            except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, MediaStreamError):
                stream._emit({"type": "audio", "available": False, "enabled": False,
                              "reason": "BROWSER_AUDIO_UNAVAILABLE"})
                self.stop()
                raise MediaStreamError

        async def _drain_diagnostics(self):
            while self.process is not None and await self.process.stderr.read(1024):
                pass

        def stop(self):
            if self.readyState != "live":
                return
            super().stop()
            self._cleanup_task = asyncio.create_task(self._stop_process())

        async def aclose(self):
            self.stop()
            if self._cleanup_task is not None:
                await asyncio.shield(self._cleanup_task)

        async def _stop_process(self):
            if (self._start_task is not None and not self._start_task.done()
                    and self.process is not None and self.process.returncode is None):
                # A user may stop while native audio activation has not replied.
                # This is only our helper, never the browser or its audio session.
                self.process.terminate()
            if self._start_task is not None:
                await asyncio.gather(self._start_task, return_exceptions=True)
            if self._reader_task is not None:
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
            process = self.process
            if process is None:
                return
            if process.returncode is None:
                try:
                    process.stdin.write(b"q\n")
                    await process.stdin.drain()
                    # Keep draining PCM while the helper stops, avoiding pipe backpressure.
                    drain = asyncio.create_task(process.stdout.read())
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                    except asyncio.TimeoutError:
                        process.terminate()
                        await process.wait()
                    await drain
                except (BrokenPipeError, ConnectionResetError):
                    await process.wait()
            if self.diagnostics is not None:
                await self.diagnostics

    return ChromiumAudio()
