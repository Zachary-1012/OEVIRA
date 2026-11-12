from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from oevira import create_app
from oevira.auth import issue_local_capability, utc_now
from oevira.business_execution import process_business_analysis_job
from oevira.notifications import emit_monitor_signal_notification


BASE_URL = "http://127.0.0.1:8333"


class NotificationHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-notification-hooks-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8333)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        self.token = None
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "通知挂钩测试"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("Origin", BASE_URL)
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"
        kwargs["headers"] = headers
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def create_work(self, title: str = "通知挂钩工作"):
        response = self.request(
            "post", "/api/works",
            json={"title": title, "goal": "验证业务与监测结果通知"},
            headers={"Idempotency-Key": f"work-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def test_business_analysis_job_completion_emits_notification_to_exact_result_version(self) -> None:
        work = self.create_work()
        queued = self.request(
            "post", f"/api/works/{work['id']}/business-jobs",
            json={
                "operation": "service_response",
                "inputs": {
                    "goal": "记录服务回应并通知确切成果版",
                    "as_of": "2026-11-10T00:00:00+00:00",
                    "observations": [],
                    "claims": [],
                    "coverage": [],
                    "timeline": [],
                    "impacts": [],
                },
                "citations": [],
            },
            headers={"Idempotency-Key": "hook-business-job"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job = queued.json["data"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status='running', lease_generation=1, updated_at=? WHERE id=?",
                (utc_now(), job["id"]),
            )
        claim = {
            "id": job["id"],
            "workspace_id": self.identity["workspace"]["id"],
            "user_id": self.identity["user"]["id"],
            "work_id": work["id"],
            "generation": 1,
            "kind": "business_analysis",
        }
        process_business_analysis_job(self.app, claim)
        with self.store.transaction(immediate=False) as connection:
            finished = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
        self.assertEqual(finished["status"], "completed", finished)
        self.assertIsNotNone(finished["artifact_id"])
        listed = self.request("get", "/api/notifications")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        items = [item for item in listed.json["data"] if item["source_kind"] == "business" and item["job_id"] == job["id"]]
        self.assertEqual(len(items), 1, listed.get_data(as_text=True))
        item = items[0]
        self.assertEqual(item["source_ref"]["id"], finished["artifact_id"])
        self.assertEqual(item["source_ref"].get("version") or int(item["source_ref"]["revision"]), 1)
        self.assertEqual(item["source_ref"]["work_id"], work["id"])
        self.assertFalse(item.get("read_at"))
        # Completing again (same job) must not re-execute or duplicate the notice.
        before_updated = finished["updated_at"]
        process_business_analysis_job(self.app, claim)
        listed_again = self.request("get", "/api/notifications")
        items_again = [item for item in listed_again.json["data"] if item["job_id"] == job["id"]]
        self.assertEqual(len(items_again), 1)
        with self.store.transaction(immediate=False) as connection:
            after = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["updated_at"], before_updated)

    def test_monitor_signal_insert_emits_notification_to_exact_signal_version(self) -> None:
        work = self.create_work("监测通知工作")
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        signal_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            emitted = emit_monitor_signal_notification(
                connection,
                workspace_id=workspace_id,
                recipient_user_id=user_id,
                signal_id=signal_id,
                title="来源有可核变化",
                summary="已核验的可读正文发生变化：新增 1 段。这不构成事实或因果结论。",
                config_version=1,
                work_id=work["id"],
                now=now,
            )
        self.assertIsNotNone(emitted)
        listed = self.request("get", "/api/notifications")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        items = [item for item in listed.json["data"] if item["source_kind"] == "monitoring"]
        self.assertEqual(len(items), 1, listed.get_data(as_text=True))
        item = items[0]
        self.assertEqual(item["source_ref"]["id"], signal_id)
        self.assertEqual(item["source_ref"]["kind"], "monitor_signal")
        self.assertEqual(item["source_ref"].get("version") or int(item["source_ref"]["revision"]), 1)
        self.assertIn("可核变化", item["title"])
        with self.store.transaction() as connection:
            again = emit_monitor_signal_notification(
                connection,
                workspace_id=workspace_id,
                recipient_user_id=user_id,
                signal_id=signal_id,
                title="来源有可核变化",
                summary="重复入库不应再发通知。",
                config_version=1,
                work_id=work["id"],
                now=now,
            )
        self.assertEqual(again["id"], emitted["id"])
        listed_again = self.request("get", "/api/notifications")
        self.assertEqual(len([item for item in listed_again.json["data"] if item["source_kind"] == "monitoring"]), 1)

    def test_harvest_and_business_paths_call_the_emit_helpers(self) -> None:
        from oevira import business_execution, monitoring
        import inspect
        harvest = inspect.getsource(monitoring._harvest_one)
        self.assertIn("insert_monitor_signal", harvest)
        self.assertEqual(harvest.count("insert_monitor_signal("), 2)
        process = inspect.getsource(business_execution.process_business_analysis_job)
        self.assertIn("emit_business_job_notification", process)




if __name__ == "__main__":
    unittest.main()
