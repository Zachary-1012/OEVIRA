from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from server.oevira import create_app
from server.oevira.worker import run_forever


class _RetentionService:
    def __init__(self):
        self.deletion_sweeps = 0
        self.export_sweeps = 0
        self.browser_sweeps = 0
        self.completed_first_sweep = threading.Event()

    def close_scheduled_account_browsers(self):
        self.browser_sweeps += 1
        return {"closed": 0, "pending": 0}

    def purge_due_deletions(self):
        self.deletion_sweeps += 1
        return {"state": "no_due_accounts", "completed": 0, "blocked": 0}

    def purge_expired_account_exports(self):
        self.export_sweeps += 1
        self.completed_first_sweep.set()
        return {"expired_exports_revoked_or_removed": 0}


class AccountWorkerTests(unittest.TestCase):
    def test_retention_sweep_runs_periodically_alongside_the_existing_worker(self):
        temporary = tempfile.TemporaryDirectory(prefix="oevira-account-worker-")
        self.addCleanup(temporary.cleanup)
        app = create_app(Path(temporary.name) / "data")
        retention = _RetentionService()
        app.extensions["oevira.account_service"] = retention
        stop = threading.Event()
        worker = threading.Thread(target=run_forever, args=(app, stop), daemon=True)
        worker.start()
        self.assertTrue(retention.completed_first_sweep.wait(timeout=10))
        stop.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(retention.deletion_sweeps, 1)
        self.assertEqual(retention.export_sweeps, 1)
        self.assertEqual(retention.browser_sweeps, 1)
        self.assertEqual(app.extensions["oevira.worker_state"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
