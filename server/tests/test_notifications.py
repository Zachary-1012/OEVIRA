from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from oevira import create_app
from oevira.auth import utc_now, issue_local_capability
from oevira.notifications import emit_result_notification


BASE_URL = "http://127.0.0.1:8332"


class ResultNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-notifications-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8332)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        self.token = None
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post",
            "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "通知接收人"},
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

    def create_work(self, title: str = "通知工作", *, commission_id: str | None = None, key: str | None = None):
        payload = {"title": title, "goal": "验证结果通知合同"}
        if commission_id:
            payload["commission_id"] = commission_id
        response = self.request(
            "post", "/api/works", json=payload,
            headers={"Idempotency-Key": key or f"work-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_artifact(self, work_id: str, *, title: str = "成果", body: str = "成果正文。", key: str | None = None):
        response = self.request(
            "post", "/api/artifacts",
            json={"title": title, "body": body, "citations": [], "work_id": work_id},
            headers={"Idempotency-Key": key or f"artifact-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def insert_commission(self, name: str) -> str:
        commission_id = str(uuid.uuid4())
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                (commission_id, workspace_id, name, user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'editor',1)",
                (commission_id, user_id),
            )
        return commission_id

    def insert_job(self, *, work_id: str | None, kind: str, status: str = "completed") -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        with self.store.transaction() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()]
            fields = {
                "id": job_id,
                "workspace_id": workspace_id,
                "status": status,
                "created_at": now,
                "updated_at": now,
            }
            if "kind" in columns:
                fields["kind"] = kind
            if "work_id" in columns and work_id:
                fields["work_id"] = work_id
            if "user_id" in columns:
                fields["user_id"] = user_id
            if "owner_user_id" in columns:
                fields["owner_user_id"] = user_id
            if "lease_generation" in columns:
                fields["lease_generation"] = 1
            if "attempts" in columns:
                fields["attempts"] = 1
            if "progress_completed" in columns:
                fields["progress_completed"] = 1
            if "progress_total" in columns:
                fields["progress_total"] = 1
            if "payload_json" in columns:
                fields["payload_json"] = "{}"
            if "request_hash" in columns:
                fields["request_hash"] = "0" * 64
            if "idempotency_key" in columns:
                fields["idempotency_key"] = f"job-{job_id}"
            names = [name for name in fields if name in columns]
            placeholders = ", ".join("?" for _ in names)
            connection.execute(
                f"INSERT INTO jobs({', '.join(names)}) VALUES({placeholders})",
                tuple(fields[name] for name in names),
            )
        return job_id

    def emit(self, *, source_kind: str, source_ref: dict, title: str, body: str,
             job_id: str | None = None, send_status: str = "recorded",
             work_id: str | None = None) -> dict:
        if work_id and "work_id" not in source_ref:
            source_ref = {**source_ref, "work_id": work_id}
        with self.store.transaction() as connection:
            return emit_result_notification(
                connection,
                workspace_id=self.identity["workspace"]["id"],
                recipient_user_id=self.identity["user"]["id"],
                source_kind=source_kind,
                source_ref=source_ref,
                title=title,
                body=body,
                job_id=job_id,
                send_status=send_status,
            )

    def job_row(self, job_id: str):
        with self.store.transaction(immediate=False) as connection:
            return dict(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def test_list_is_empty_before_any_result(self) -> None:
        response = self.request("get", "/api/notifications")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["data"], [])

    def test_media_business_and_monitoring_notifications_point_to_exact_versions(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        media = self.emit(
            source_kind="media",
            source_ref={"kind": "media_render", "id": "render-1", "revision": "3", "version": 3, "work_id": work["id"]},
            title="节目成片已完成",
            body="成片第 3 版已可预览。",
            work_id=work["id"],
        )
        business = self.emit(
            source_kind="business",
            source_ref={
                "kind": "artifact", "id": artifact["id"], "revision": str(artifact["version"]),
                "version": artifact["version"], "work_id": work["id"],
            },
            title="经营分析结果已完成",
            body="分析回到当前稿第 1 版。",
            work_id=work["id"],
        )
        monitoring = self.emit(
            source_kind="monitoring",
            source_ref={"kind": "monitor_signal", "id": "signal-1", "revision": "1", "version": 1, "work_id": work["id"]},
            title="监测发现新信号",
            body="信号第 1 版已入库。",
            work_id=work["id"],
        )
        listed = self.request("get", "/api/notifications")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        items = listed.json["data"]
        self.assertEqual({item["source_kind"] for item in items}, {"media", "business", "monitoring"})
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id[media["id"]]["source_ref"]["version"], 3)
        self.assertEqual(by_id[media["id"]]["source_ref"]["id"], "render-1")
        self.assertEqual(by_id[business["id"]]["source_ref"]["id"], artifact["id"])
        self.assertEqual(by_id[business["id"]]["source_ref"]["version"], artifact["version"])
        self.assertEqual(by_id[monitoring["id"]]["source_ref"]["id"], "signal-1")
        self.assertEqual(by_id[monitoring["id"]]["source_ref"]["version"], 1)
        fetched = self.request("get", f"/api/notifications/{business['id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json["data"]["body"], "分析回到当前稿第 1 版。")
        self.assertIsNone(fetched.json["data"]["read_at"])
        self.assertEqual(fetched.json["data"]["access"], "granted")

    def test_marking_read_does_not_change_job_final_state(self) -> None:
        work = self.create_work()
        job_id = self.insert_job(work_id=work["id"], kind="business_analysis", status="completed")
        before = self.job_row(job_id)
        note = self.emit(
            source_kind="business",
            source_ref={"kind": "job", "id": job_id, "revision": "1", "version": 1, "work_id": work["id"]},
            title="业务计算已完成",
            body="结果已写入当前版。",
            job_id=job_id,
            work_id=work["id"],
        )
        marked = self.request("post", f"/api/notifications/{note['id']}/read", json={})
        self.assertEqual(marked.status_code, 200, marked.get_data(as_text=True))
        self.assertIsNotNone(marked.json["data"]["read_at"])
        after = self.job_row(job_id)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        again = self.request("post", f"/api/notifications/{note['id']}/read", json={})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json["data"]["read_at"], marked.json["data"]["read_at"])
        listed = self.request("get", "/api/notifications?unread=1")
        self.assertEqual(listed.json["data"], [])

    def test_unknown_send_retry_does_not_reexecute_the_job(self) -> None:
        work = self.create_work()
        job_id = self.insert_job(work_id=work["id"], kind="media_render", status="completed")
        before = self.job_row(job_id)
        note = self.emit(
            source_kind="media",
            source_ref={"kind": "job", "id": job_id, "revision": "2", "version": 2, "work_id": work["id"]},
            title="成片通知发送结果未知",
            body="成片第 2 版已完成，对外发送结果未知。",
            job_id=job_id,
            send_status="unknown",
            work_id=work["id"],
        )
        retried = self.request("post", f"/api/notifications/{note['id']}/retry-delivery", json={})
        self.assertEqual(retried.status_code, 200, retried.get_data(as_text=True))
        data = retried.json["data"]
        notification = data["notification"] if "notification" in data else data
        self.assertGreaterEqual(notification["delivery_attempts"], 2)
        self.assertEqual(notification["job_id"], job_id)
        after = self.job_row(job_id)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["updated_at"], before["updated_at"])
        with self.store.transaction(immediate=False) as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE workspace_id = ?", (self.identity["workspace"]["id"],)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_revoked_access_does_not_leak_historical_body(self) -> None:
        commission_id = self.insert_commission("机密客户")
        work = self.create_work("机密工作", commission_id=commission_id, key="secret-work")
        secret = "合同金额 120万元，仅限甲客户。"
        note = self.emit(
            source_kind="monitoring",
            source_ref={"kind": "monitor_signal", "id": "secret-signal", "revision": "4", "version": 4, "work_id": work["id"]},
            title="监测到机密报价变化",
            body=secret,
            work_id=work["id"],
        )
        visible = self.request("get", f"/api/notifications/{note['id']}")
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json["data"]["body"], secret)
        with self.store.transaction() as connection:
            connection.execute("UPDATE commission_members SET active=0 WHERE commission_id=?", (commission_id,))
        redacted = self.request("get", f"/api/notifications/{note['id']}")
        self.assertEqual(redacted.status_code, 200, redacted.get_data(as_text=True))
        payload = redacted.json["data"]
        self.assertEqual(payload["id"], note["id"])
        self.assertEqual(payload["access"], "revoked")
        self.assertIsNone(payload["body"])
        self.assertIsNone(payload["title"])
        self.assertIsNone(payload["source_ref"])
        raw = redacted.get_data(as_text=True)
        self.assertNotIn("120万元", raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("机密报价", raw)
        listed = self.request("get", "/api/notifications")
        listed_item = next(item for item in listed.json["data"] if item["id"] == note["id"])
        self.assertEqual(listed_item["access"], "revoked")
        self.assertIsNone(listed_item["body"])
        self.assertIsNone(listed_item["title"])
        self.assertNotIn("120万元", listed.get_data(as_text=True))
        marked = self.request("post", f"/api/notifications/{note['id']}/read", json={})
        self.assertEqual(marked.status_code, 200)
        self.assertIsNone(marked.json["data"]["body"])
        self.assertNotIn("120万元", marked.get_data(as_text=True))

    def test_other_user_cannot_read_a_notification(self) -> None:
        foreign_id = str(uuid.uuid4())
        workspace_id = self.identity["workspace"]["id"]
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO result_notifications("
                "id, workspace_id, recipient_user_id, source_kind, source_ref_json, title, body, "
                "job_id, send_status, delivery_attempts, read_at, created_at, updated_at"
                ") VALUES(?, ?, ?, 'business', '{}', '别人的通知', '不应泄露的正文', NULL, 'recorded', 1, NULL, ?, ?)",
                (foreign_id, workspace_id, "other-user", now, now),
            )
        fetched = self.request("get", f"/api/notifications/{foreign_id}")
        self.assertEqual(fetched.status_code, 404)
        self.assertNotIn("不应泄露的正文", fetched.get_data(as_text=True))
        listed = self.request("get", "/api/notifications")
        self.assertEqual(listed.json["data"], [])

    def test_finish_media_render_emits_a_notification_to_the_exact_render_version(self) -> None:
        from oevira.media_api import _finish_render_job

        work = self.create_work()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        render_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO media_projects("
                "id, workspace_id, owner_user_id, work_id, title, kind, current_version, created_at, updated_at, created_by"
                ") VALUES(?, ?, ?, ?, '通知节目', 'video', 2, ?, ?, ?)",
                (project_id, workspace_id, user_id, work["id"], now, now, user_id),
            )
            connection.execute(
                "INSERT INTO media_project_versions("
                "project_id, workspace_id, version, parent_version, operation, state_json, created_at, created_by"
                ") VALUES(?, ?, 2, NULL, 'create', '{}', ?, ?)",
                (project_id, workspace_id, now, user_id),
            )
            connection.execute(
                "INSERT INTO jobs("
                "id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, "
                "progress_completed, progress_total, cancel_requested, attempts, lease_generation, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, 'media_render', 'running', '{}', ?, ?, 0, 1, 0, 1, 1, ?, ?)",
                (job_id, workspace_id, user_id, work["id"], "0" * 64, f"job-{job_id}", now, now),
            )
            connection.execute(
                "INSERT INTO media_renders("
                "id, workspace_id, project_id, project_version, job_id, output_kind, format, status, "
                "relative_path, created_at, updated_at"
                ") VALUES(?, ?, ?, 2, ?, 'video', 'mp4', 'running', 'renders/pending.mp4', ?, ?)",
                (render_id, workspace_id, project_id, job_id, now, now),
            )
        claim = {"id": job_id, "workspace_id": workspace_id, "generation": 1, "user_id": user_id}
        _finish_render_job(self.store, claim, render_id, status="completed", progress_completed=1, progress_label="Media render completed")
        listed = self.request("get", "/api/notifications")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        media_items = [item for item in listed.json["data"] if item["source_kind"] == "media"]
        self.assertEqual(len(media_items), 1, listed.get_data(as_text=True))
        item = media_items[0]
        self.assertEqual(item["source_ref"]["id"], render_id)
        self.assertEqual(item["job_id"], job_id)
        self.assertIn(item["source_ref"].get("version") or item["source_ref"].get("revision"), {2, "2"})
        self.assertEqual(self.job_row(job_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
