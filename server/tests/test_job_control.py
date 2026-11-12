"""Real Job control transitions, persisted receipts, and worker stop boundaries."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from server.oevira import create_app
from server.oevira.worker import run_once
from server.tests import test_core as core_test


class JobControlTest(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request
    create_work = core_test.CoreApiTest.create_work
    create_text_material = core_test.CoreApiTest.create_text_material

    def comparison(self):
        work = self.create_work()
        materials = [self.create_text_material(work["id"], title, text) for title, text in (
            ("方案甲", "预算为120万元，周期为30天。"),
            ("方案乙", "预算为100万元，周期为45天。"),
        )]
        response = self.request("post", f"/api/works/{work['id']}/compare",
                                json={"material_ids": [item["id"] for item in materials], "criteria": ["预算"]},
                                headers={"Idempotency-Key": "control-comparison"})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["data"], materials

    def control(self, job, action, operation, revision):
        return self.request("post", f"/api/jobs/{job['id']}/{action}",
                            json={"operation_id": operation, "base_control_revision": revision})

    def test_pause_cold_reopen_resume_and_late_receipt_preserve_new_control(self):
        job, _ = self.comparison()
        paused = self.control(job, "pause", "pause-1", 0)
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        self.assertEqual(paused.json["data"]["status"], "paused")
        self.assertFalse(run_once(self.app))
        self.app = create_app(self.root, expected_port=8317)
        self.client = self.app.test_client()
        self.client.set_cookie("oevira_session", self.token, domain="127.0.0.1")
        resumed = self.control(job, "resume", "resume-1", 1)
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["status"], "queued")
        replay = self.control(job, "pause", "pause-1", 0)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"]["status"], "queued")
        self.assertEqual(replay.json["data"]["control_revision"], 2)
        self.assertEqual(replay.json["data"]["control_receipt"]["status"], "paused")
        self.assertTrue(run_once(self.app))
        done = self.request("get", f"/api/jobs/{job['id']}").json["data"]
        self.assertEqual(done["status"], "completed")
        artifact = self.request("get", f"/api/artifacts/{done['artifact_id']}")
        self.assertIn("120", artifact.json["data"]["body"])
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM artifacts")["n"], 1)

    def test_running_pause_is_only_confirmed_after_worker_leaves(self):
        from server.oevira import worker

        job, _ = self.comparison()
        entered, release = threading.Event(), threading.Event()
        original = worker._advance
        errors = []

        def blocked_advance(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Test did not release the worker boundary")
            return original(*args, **kwargs)

        def execute():
            try:
                run_once(self.app)
            except BaseException as error:
                errors.append(error)

        with patch.object(worker, "_advance", side_effect=blocked_advance):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                requested = self.control(job, "pause", "pause-running", 0)
                self.assertEqual(requested.status_code, 200)
                self.assertEqual(requested.json["data"]["status"], "pause_requested")
                early_resume = self.control(job, "resume", "too-early", 1)
                self.assertEqual(early_resume.status_code, 409)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        row = self.store.row("SELECT * FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(row["status"], "paused")
        self.assertIsNone(row["artifact_id"])
        previous_generation = row["lease_generation"]
        self.assertEqual(self.control(job, "resume", "continue-running", 1).status_code, 200)
        self.assertTrue(run_once(self.app))
        row = self.store.row("SELECT * FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(row["status"], "completed")
        self.assertGreater(row["lease_generation"], previous_generation)

    def test_conflicting_controls_and_completed_task_do_not_replay_work(self):
        job, _ = self.comparison()
        self.assertEqual(self.control(job, "pause", "same-key", 0).status_code, 200)
        collision = self.control(job, "resume", "same-key", 1)
        self.assertEqual(collision.json["error"]["code"], "IDEMPOTENCY_CONFLICT")
        stale = self.control(job, "resume", "stale-key", 0)
        self.assertEqual(stale.json["error"]["code"], "JOB_CONTROL_CONFLICT")
        self.assertEqual(self.control(job, "resume", "resume", 1).status_code, 200)
        self.assertTrue(run_once(self.app))
        completed = self.control(job, "pause", "late-pause", 2)
        self.assertEqual(completed.json["data"]["status"], "completed")
        self.assertFalse(run_once(self.app))
        self.assertEqual(self.store.row("SELECT attempts FROM jobs WHERE id=?", (job["id"],))["attempts"], 1)

    def test_cancelled_task_needs_explicit_supported_retry_and_keeps_history(self):
        job, _ = self.comparison()
        self.assertEqual(self.control(job, "pause", "pause", 0).status_code, 200)
        cancelled = self.control(job, "cancel", "cancel", 1)
        self.assertEqual(cancelled.json["data"]["status"], "cancelled")
        self.assertEqual(self.control(job, "resume", "resume", 2).status_code, 409)
        self.assertFalse(run_once(self.app))
        self.assertEqual(self.control(job, "retry", "retry", 2).status_code, 202)
        history = self.store.row("SELECT status FROM job_control_receipts WHERE operation_id='cancel'")
        self.assertEqual(history["status"], "cancelled")
        self.assertTrue(run_once(self.app))

    def test_changed_source_blocks_resume_without_losing_pause(self):
        job, materials = self.comparison()
        self.assertEqual(self.control(job, "pause", "pause", 0).status_code, 200)
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET sha256=? WHERE id=?", ("0" * 64, materials[0]["id"]))
        response = self.control(job, "resume", "resume", 1)
        self.assertEqual(response.json["error"]["code"], "JOB_INPUT_CHANGED")
        row = self.store.row("SELECT status,control_revision FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(tuple(row), ("paused", 1))

    def test_waiting_pause_resume_restores_wait_without_scheduling(self):
        job, _ = self.comparison()
        with self.store.transaction() as connection:
            connection.execute("UPDATE jobs SET status='waiting_for_auth' WHERE id=?", (job["id"],))
        self.assertEqual(self.control(job, "pause", "pause", 0).status_code, 200)
        resumed = self.control(job, "resume", "resume", 1)
        self.assertEqual(resumed.json["data"]["status"], "waiting_for_auth")
        self.assertFalse(run_once(self.app))

    def test_crash_while_pausing_needs_explicit_resume_and_fences_old_lease(self):
        from server.oevira.worker import _advance, _claim_job

        job, _ = self.comparison()
        claim = _claim_job(self.store)
        self.assertEqual(self.control(job, "pause", "pause", 0).json["data"]["status"], "pause_requested")
        with self.store.transaction() as connection:
            connection.execute("UPDATE jobs SET lease_heartbeat_at='2000-01-01T00:00:00Z' WHERE id=?", (job["id"],))
        self.assertIsNone(_claim_job(self.store))
        interrupted = self.request("get", f"/api/jobs/{job['id']}").json["data"]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertTrue(interrupted["pause_requested"])
        self.assertEqual(self.control(job, "retry", "retry", 1).status_code, 409)
        resumed = self.control(job, "resume", "resume", 1)
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertFalse(_advance(self.store, job["id"], claim["generation"], 1, 1, "stale"))
        self.assertTrue(run_once(self.app))
        self.assertFalse(_advance(self.store, job["id"], claim["generation"], 1, 1, "late"))
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM artifacts")["n"], 1)

    def test_cancel_replacing_pause_at_executor_exit_is_acknowledged(self):
        from server.oevira import job_control

        job, _ = self.comparison()
        original = job_control.acknowledge_stop

        def replace_pending_pause(store, claim):
            self.assertEqual(self.control(job, "pause", "pause-at-exit", 0).status_code, 200)
            self.assertEqual(self.control(job, "cancel", "cancel-at-exit", 1).status_code, 200)
            original(store, claim)

        with patch("server.oevira.worker._process_comparison"), patch.object(job_control, "acknowledge_stop", side_effect=replace_pending_pause):
            self.assertTrue(run_once(self.app))
        row = self.store.row("SELECT status,pause_requested,artifact_id FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(tuple(row), ("cancelled", 0, None))


if __name__ == "__main__":
    unittest.main()
