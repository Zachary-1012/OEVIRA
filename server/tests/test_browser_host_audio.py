from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server.oevira.browser_host_audio import browser_audio_track


class BrowserAudioStartupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows owned helper lifecycle")
    def test_stop_during_helper_activation_reaps_the_owned_process(self):
        root = Path(__file__).resolve().parents[2]
        if not (root / "app/build/windows/x64/runner/Release/oevira_browser_audio.exe").is_file():
            self.skipTest("Build the pinned browser audio helper first")

        async def exercise():
            created = asyncio.Event()
            processes = []
            actual_create_process = asyncio.create_subprocess_exec

            async def delayed_activation(*_command, **kwargs):
                # Real disposable child; it simulates a native activation that
                # has not returned its header, without capturing any audio.
                process = await actual_create_process(
                    sys.executable, "-I", "-c", "import time; time.sleep(20)", **kwargs,
                )
                processes.append(process)
                created.set()
                return process

            cdp = SimpleNamespace(send=lambda _command: {"processInfo": [{"type": "browser", "id": 1}]}, detach=lambda: None)
            worker = SimpleNamespace(sessions={"test-session": object()}, call=lambda callback: callback(),
                                     browser=SimpleNamespace(new_browser_cdp_session=lambda: cdp))
            stream = SimpleNamespace(worker=worker, session_id="test-session", _session_is_live=lambda: True, _emit=lambda _event: None)
            track = browser_audio_track(stream)
            self.assertIsNotNone(track)
            with patch("asyncio.create_subprocess_exec", side_effect=delayed_activation):
                receiving = asyncio.create_task(track.recv())
                try:
                    await asyncio.wait_for(created.wait(), 5)
                    await asyncio.wait_for(track.aclose(), 3)
                    self.assertEqual(track.readyState, "ended")
                    self.assertIsNotNone(processes[0].returncode)
                    self.assertTrue(track._cleanup_task.done())
                    await asyncio.gather(receiving, return_exceptions=True)
                finally:
                    for process in processes:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    receiving.cancel()
                    await asyncio.gather(receiving, return_exceptions=True)
                    await track.aclose()

        asyncio.run(exercise())
