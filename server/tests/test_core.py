from __future__ import annotations

import hashlib
import io
import json
import secrets
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import uuid

from docx import Document

from server.oevira import create_app
from server.oevira.auth import _digest, issue_local_capability, utc_now
from server.oevira.store import Store
from server.oevira.worker import run_once


BASE_URL = "http://127.0.0.1:8317"
ORIGIN = BASE_URL


class CoreApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-core-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, self.capability_file)
        self.capability = self.capability_file.read_text(encoding="ascii")
        response = self.request(
            "post",
            "/api/local-session",
            json={"capability": self.capability, "display_name": "研究者"},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.login_response = response
        self.token = response.json["data"]["token"]
        self.identity = response.json["data"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def create_work(self, *, title: str = "资料比较", brief: dict | None = None) -> dict:
        response = self.request(
            "post",
            "/api/works",
            json={"title": title, "goal": "核对各材料中的预算和时间", "brief": brief or {}},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_text_material(self, work_id: str, title: str, text: str) -> dict:
        response = self.request(
            "post",
            "/api/materials",
            json={"title": title, "filename": f"{title}.txt", "text": text, "work_id": work_id},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_other_workspace(self) -> tuple[str, str]:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "另一位用户", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "另一个空间", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, user_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now, expiry),
            )
        return token, workspace_id

    def test_comparison_rejects_duplicate_and_unknown_material_ids(self) -> None:
        work = self.create_work(title="输入身份核对")
        material = self.create_text_material(work["id"], "可用材料", "预算为100万元。")

        duplicate = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": [material["id"], material["id"]], "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "duplicate-material-id"},
        )
        missing = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": [material["id"], str(uuid.uuid4())], "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "unknown-material-id"},
        )

        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.json["error"]["code"], "INVALID_INPUT")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json["error"]["code"], "NOT_FOUND")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'material_comparison'")["count"], 0)

    def test_comparison_metadata_loads_all_ids_across_sqlite_batches(self) -> None:
        work = self.create_work(title="SQLite批次核对")
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        material_ids = [str(uuid.uuid4()) for _ in range(501)]
        rows = [
            (
                material_id, workspace_id, user_id, f"材料{index}", f"material-{index}.txt",
                "ready", "0" * 64,
                f"objects/{workspace_id}/materials/{material_id}/original", 1, now,
            )
            for index, material_id in enumerate(material_ids)
        ]
        with self.store.transaction() as connection:
            connection.executemany(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, sha256, relative_path, size_bytes, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        queued = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": material_ids, "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "sqlite-batch-501"},
        )

        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        payload = json.loads(self.store.row("SELECT payload_json FROM jobs WHERE id = ?", (job_id,))["payload_json"])
        snapshotted_ids = [item["material_id"] for item in payload["input_snapshot"]["materials"]]
        self.assertEqual(snapshotted_ids, material_ids)
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM work_materials WHERE work_id = ?", (work["id"],))["count"],
            len(material_ids),
        )

    def test_comparison_result_budget_reports_unassessed_selection(self) -> None:
        work = self.create_work(title="成果长度预算")
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        material_ids = [str(uuid.uuid4()) for _ in range(800)]
        rows = []
        for index, material_id in enumerate(material_ids):
            base_name = f"material-{index:04d}.txt"
            filename = base_name + "x" * (255 - len(base_name))
            rows.append((
                material_id, workspace_id, user_id, f"材料{index}", filename, "ready",
                "0" * 64, f"objects/{workspace_id}/materials/{material_id}/original", 1, now,
            ))
        with self.store.transaction() as connection:
            connection.executemany(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, sha256, relative_path, size_bytes, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        queued = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": material_ids, "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "comparison-result-budget"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))

        self.assertTrue(run_once(self.app))

        job = self.request("get", f"/api/jobs/{queued.json['data']['id']}").json["data"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "COMPARISON_RESULT_TOO_LARGE")
        self.assertIn("at least 213600 characters", job["error"]["message"])
        self.assertIn("0 selected materials were analyzed", job["error"]["message"])
        self.assertIsNone(job["artifact_id"])

    def test_comparison_segment_budget_fails_before_loading_large_segment_sets(self) -> None:
        work = self.create_work(title="片段处理预算")
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        material_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        material_rows = [
            (
                material_id, workspace_id, user_id, f"材料{index}", f"material-{index}.txt",
                "ready", "0" * 64,
                f"objects/{workspace_id}/materials/{material_id}/original", 1, now,
            )
            for index, material_id in enumerate(material_ids)
        ]
        segment_rows = [
            (material_ids[index % 2], workspace_id, f"segment-{index:05d}", "预算位置", "预算", 0, 2, "text_line")
            for index in range(50_001)
        ]
        with self.store.transaction() as connection:
            connection.executemany(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, sha256, relative_path, size_bytes, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                material_rows,
            )
            connection.executemany(
                "INSERT INTO material_segments(material_id, workspace_id, id, label, text, start_offset, end_offset, kind) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                segment_rows,
            )
        queued = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": material_ids, "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "comparison-segment-budget"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))

        self.assertTrue(run_once(self.app))

        job = self.request("get", f"/api/jobs/{queued.json['data']['id']}").json["data"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "COMPARISON_SEGMENT_BUDGET_EXCEEDED")
        self.assertIn("50001 parsed segments", job["error"]["message"])
        self.assertIn("0 selected materials were read", job["error"]["message"])
        self.assertEqual(job["progress"]["completed"], 0)
        self.assertIsNone(job["artifact_id"])

    def test_saved_artifact_accepts_more_than_five_hundred_verified_citations(self) -> None:
        work = self.create_work(title="完整引文保全")
        text = "\n".join(f"预算依据第{index}项，{index}万元。" for index in range(501))
        material = self.create_text_material(work["id"], "多项预算依据", text)
        citations = [
            {"material_id": material["id"], "segment_id": segment["id"], "quote": segment["text"]}
            for segment in material["segments"]
        ]
        self.assertEqual(len(citations), 501)

        response = self.request(
            "post",
            "/api/artifacts",
            json={"title": "逐项预算依据", "body": "逐条保留501项引文。", "citations": citations, "work_id": work["id"]},
        )

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(len(response.json["data"]["citations"]), 501)

    def test_comparison_worker_saves_more_than_twenty_materials(self) -> None:
        work = self.create_work(title="完整材料比较")
        materials = []
        for index in range(21):
            if index == 0:
                text = "\n".join(f"预算增加为{100 + item}万元。" for item in range(60))
            else:
                direction = "减少" if index == 20 else "变化"
                text = f"预算{direction}为{100 + index}万元。"
            materials.append(self.create_text_material(work["id"], f"材料{index:02d}", text))
        queued = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": [item["id"] for item in materials], "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "comparison-21-materials"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))

        self.assertTrue(run_once(self.app))

        job = self.request("get", f"/api/jobs/{queued.json['data']['id']}").json["data"]
        self.assertEqual(job["status"], "completed")
        result = self.request("get", f"/api/artifacts/{job['artifact_id']}").json["data"]
        expected_ids = {item["id"] for item in materials}
        self.assertIn("当前输入：21 份", result["body"])
        self.assertEqual({item["material_id"] for item in result["citations"]}, expected_ids)
        self.assertIn("预算增加", result["body"])
        self.assertIn("预算减少", result["body"])
        self.assertIn("共找到 60 条线索，已展开5条", result["body"])
        self.assertIn("共有 10 个匹配片段", result["body"])

    def test_running_comparison_cancel_and_retry_recover_without_stale_artifact(self) -> None:
        from server.oevira.comparison import build_comparison

        work = self.create_work(title="取消后继续")
        materials = [
            self.create_text_material(work["id"], f"取消材料{index}", f"预算为{100 + index}万元。")
            for index in range(2)
        ]
        queued = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": [item["id"] for item in materials], "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "running-cancel-retry"},
        )
        self.assertEqual(queued.status_code, 202)
        job_id = queued.json["data"]["id"]
        entered, release = threading.Event(), threading.Event()

        def wait_before_comparison(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("The cancellation check did not release the worker")
            return build_comparison(*args, **kwargs)

        with patch("server.oevira.comparison.build_comparison", side_effect=wait_before_comparison):
            thread = threading.Thread(target=run_once, args=(self.app,), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5), "Worker did not enter comparison")
                cancellation = self.request("post", f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancellation.json["data"]["status"], "cancel_requested")
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "The cancelled worker should finish")

        cancelled = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["artifact_id"])
        retry = self.request("post", f"/api/jobs/{job_id}/retry")
        self.assertEqual(retry.status_code, 202)
        self.assertTrue(run_once(self.app))
        completed = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["artifact_id"])

        reopened_app = create_app(self.root, expected_port=8317)
        reopened_client = reopened_app.test_client()
        reopened_job = reopened_client.get(
            f"/api/jobs/{job_id}", base_url=BASE_URL, headers=self.auth_headers()
        )
        self.assertEqual(reopened_job.status_code, 200)
        self.assertEqual(reopened_job.json["data"]["status"], "completed")
        self.assertEqual(reopened_job.json["data"]["artifact_id"], completed["artifact_id"])

    def test_local_auth_origin_and_single_use_capability(self) -> None:
        status = self.request("get", "/api/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["data"]["product"], "OEVIRA")
        self.assertEqual(status.json["data"]["version"], "0.1.0")
        self.assertEqual(status.json["data"]["runtime"]["api"], "available")
        self.assertEqual(status.json["data"]["ai"]["status"], "unbound")

        cookie = self.client.get_cookie("oevira_session", domain="127.0.0.1")
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie.value, self.token)
        set_cookie = self.login_response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertEqual(self.request("get", "/api/session").json["data"]["workspace"], self.identity["workspace"])
        stored_hash = self.store.row("SELECT token_hash FROM sessions WHERE id IS NOT NULL")
        self.assertEqual(stored_hash["token_hash"], _digest(self.token))

        replay = self.request("post", "/api/local-session", json={"capability": self.capability})
        self.assertEqual(replay.status_code, 401)
        bad_origin = self.request("get", "/api/status", headers={"Origin": "http://attacker.invalid"})
        self.assertEqual(bad_origin.status_code, 403)
        bad_host = self.client.get("/api/status", base_url=BASE_URL, headers={"Host": "attacker.invalid:8317"})
        self.assertEqual(bad_host.status_code, 400)
        wrong_port = self.client.get("/api/status", base_url=BASE_URL, headers={"Host": "127.0.0.1:9999"})
        self.assertEqual(wrong_port.status_code, 400)
        forwarded_host = self.request("get", "/api/status", headers={"X-Forwarded-Host": "127.0.0.1:8317"})
        self.assertEqual(forwarded_host.status_code, 400)

        rotated = self.root / "second.capability"
        output = io.StringIO()
        from server.oevira.__main__ import main

        argv = ["oevira", "--data-root", str(self.root), "--bootstrap-file", str(rotated), "--issue-session"]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(output.getvalue(), "")
        rotated_capability = rotated.read_text(encoding="ascii")
        login = self.request("post", "/api/local-session", json={"capability": rotated_capability})
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
        self.assertNotEqual(login.json["data"]["workspace"]["id"], "")

    def test_real_material_comparison_snapshot_reopen_and_workspace_isolation(self) -> None:
        brief = {
            "audience": "品牌运营团队",
            "deliverable": "预算核对摘要",
            "budget": {"notes": "金额口径尚未确认"},
            "required_facts": ["预算金额"],
            "forbidden_claims": ["不能将候选差异写成已确认冲突"],
            "customer_requests": ["逐条显示材料位置"],
        }
        work = self.create_work(brief=brief)
        readback = self.request("get", f"/api/works/{work['id']}")
        self.assertEqual(readback.json["data"]["brief"], brief)
        updated = self.request(
            "patch",
            f"/api/works/{work['id']}",
            json={"base_version": work["version"], "brief": {**brief, "audience": "项目负责人"}},
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        conflict = self.request("patch", f"/api/works/{work['id']}", json={"base_version": 1, "title": "覆盖旧稿"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(conflict.json["error"]["details"]["current"]["version"], 2)

        sources = [
            self.create_text_material(work["id"], "甲方方案", "预算金额为120万元，计划于2026年10月开始。"),
            self.create_text_material(work["id"], "乙方方案", "项目预算为100万元，计划于2026年11月开始。"),
        ]
        upload = self.request(
            "post",
            "/api/materials",
            data={"file": (io.BytesIO(b"item,value\nbudget,120\n"), "third-party.csv"), "work_id": work["id"]},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        sources.append(upload.json["data"])
        for material in sources:
            self.assertIn(material["status"], {"ready", "partial"})
            self.assertTrue(material["segments"])
            source_file = self.request("get", f"/api/materials/{material['id']}/file")
            self.assertEqual(source_file.status_code, 200)
            self.assertEqual(hashlib.sha256(source_file.data).hexdigest(), material["sha256"])

        payload = {"material_ids": [item["id"] for item in sources], "criteria": ["预算", "计划时间"]}
        headers = {**self.auth_headers(), "Idempotency-Key": "j01-comparison-01"}
        queued = self.request("post", f"/api/works/{work['id']}/compare", json=payload, headers=headers)
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        self.assertEqual(queued.json["data"]["status"], "queued")
        self.assertTrue(run_once(self.app))
        job_response = self.request("get", f"/api/jobs/{job_id}")
        self.assertEqual(job_response.status_code, 200)
        job = job_response.json["data"]
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["artifact_id"])
        self.assertIsNone(job["error"])
        artifact_response = self.request("get", f"/api/artifacts/{job['artifact_id']}")
        self.assertEqual(artifact_response.status_code, 200)
        artifact = artifact_response.json["data"]
        self.assertEqual(artifact["input_snapshot"]["work"]["goal"], work["goal"])
        self.assertEqual(artifact["input_snapshot"]["work"]["brief"], updated.json["data"]["brief"])
        self.assertLessEqual({item["material_id"] for item in artifact["citations"]}, {item["id"] for item in sources})
        self.assertEqual(len(artifact["input_snapshot"]["materials"]), 3)
        for source in sources:
            self.assertIn(source["filename"], artifact["body"])
        self.assertTrue(any("120万元" in item["quote"] for item in artifact["citations"]))
        self.assertIn(artifact["id"], self.request("get", f"/api/works/{work['id']}").json["data"]["artifact_ids"])

        source_to_corrupt = sources[2]
        source_record = self.store.row("SELECT relative_path FROM materials WHERE id = ?", (source_to_corrupt["id"],))
        source_path = self.root.joinpath(*Path(source_record["relative_path"]).parts)
        intact_original = source_path.read_bytes()
        source_path.write_bytes(b"changed after the source hash was recorded")
        integrity_job_response = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={"material_ids": [item["id"] for item in sources], "criteria": ["预算"]},
            headers={**self.auth_headers(), "Idempotency-Key": "j01-original-integrity"},
        )
        self.assertEqual(integrity_job_response.status_code, 202)
        self.assertTrue(run_once(self.app))
        integrity_job = self.request("get", f"/api/jobs/{integrity_job_response.json['data']['id']}").json["data"]
        self.assertEqual(integrity_job["status"], "partial")
        integrity_artifact = self.request("get", f"/api/artifacts/{integrity_job['artifact_id']}").json["data"]
        self.assertNotIn(source_to_corrupt["id"], {item["material_id"] for item in integrity_artifact["citations"]})
        self.assertIn(source_to_corrupt["filename"], integrity_artifact["body"])
        source_path.write_bytes(intact_original)

        broken_upload = self.request(
            "post",
            "/api/materials",
            data={"file": (io.BytesIO(b"\\x00\\x01unreadable"), "unreadable.bin"), "work_id": work["id"]},
            content_type="multipart/form-data",
        )
        self.assertEqual(broken_upload.status_code, 201)
        broken = broken_upload.json["data"]
        self.assertEqual(broken["status"], "unavailable")
        broken_file = self.request("get", f"/api/materials/{broken['id']}/file")
        self.assertEqual(hashlib.sha256(broken_file.data).hexdigest(), broken["sha256"])
        partial_payload = {"material_ids": [sources[0]["id"], sources[1]["id"], broken["id"]], "criteria": ["预算"]}
        partial_job_response = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json=partial_payload,
            headers={**self.auth_headers(), "Idempotency-Key": "j01-comparison-partial"},
        )
        self.assertEqual(partial_job_response.status_code, 202)
        partial_job_id = partial_job_response.json["data"]["id"]
        self.assertTrue(run_once(self.app))
        partial_job = self.request("get", f"/api/jobs/{partial_job_id}").json["data"]
        self.assertEqual(partial_job["status"], "partial")
        self.assertEqual(partial_job["error"]["code"], "SOME_MATERIALS_UNAVAILABLE")
        self.assertTrue(partial_job["artifact_id"])
        partial_artifact = self.request("get", f"/api/artifacts/{partial_job['artifact_id']}").json["data"]
        self.assertIn("unreadable.bin", partial_artifact["body"])
        self.assertIn("内容不可用", partial_artifact["body"])

        same_request = self.request("post", f"/api/works/{work['id']}/compare", json=payload, headers=headers)
        self.assertEqual(same_request.status_code, 200)
        self.assertEqual(same_request.json["data"]["id"], job_id)
        changed_request = self.request(
            "post",
            f"/api/works/{work['id']}/compare",
            json={**payload, "criteria": ["预算", "履约周期"]},
            headers=headers,
        )
        self.assertEqual(changed_request.status_code, 409)

        archived = self.request("patch", f"/api/works/{work['id']}", json={"base_version": 2, "status": "archived"})
        self.assertEqual(archived.status_code, 200)
        replay_after_archive = self.request("post", f"/api/works/{work['id']}/compare", json=payload, headers=headers)
        self.assertEqual(replay_after_archive.status_code, 200)
        self.assertEqual(replay_after_archive.json["data"]["id"], job_id)

        other_token, _ = self.create_other_workspace()
        for path in (
            f"/api/works/{work['id']}",
            f"/api/materials/{sources[0]['id']}",
            f"/api/jobs/{job_id}",
            f"/api/artifacts/{artifact['id']}",
        ):
            denied = self.request("get", path, headers=self.auth_headers(other_token))
            self.assertEqual(denied.status_code, 404, f"{path}: {denied.get_data(as_text=True)}")

    def test_artifact_cas_restore_and_export_integrity_recovery(self) -> None:
        work = self.create_work(title="导出与恢复")
        material = self.create_text_material(work["id"], "事实来源", "确认金额为120万元，发布日期为2026年10月29日。")
        segment = material["segments"][0]
        quote = "确认金额为120万元"
        artifact_response = self.request(
            "post",
            "/api/artifacts",
            json={
                "title": "客户可编辑摘要",
                "body": "金额候选：120万元。请确认与口径一致。",
                "citations": [{"material_id": material["id"], "segment_id": segment["id"], "quote": quote}],
                "work_id": work["id"],
            },
            headers={"Idempotency-Key": "artifact-create-01"},
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        artifact = artifact_response.json["data"]
        bad_citation = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/versions",
            json={"base_version": 1, "title": "错误版本", "body": "草稿", "citations": [{"material_id": material["id"], "segment_id": segment["id"], "quote": "900万元"}]},
        )
        self.assertEqual(bad_citation.status_code, 400)
        missing_citations = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/versions",
            json={"base_version": 1, "title": "丢来源", "body": "用户修订稿"},
        )
        self.assertEqual(missing_citations.status_code, 400)
        saved = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/versions",
            json={
                "base_version": 1,
                "title": "人工修订摘要",
                "body": "用户保留的修订稿：金额仍为120万元。",
                "citations": artifact["citations"],
                "revision_note": "按客户口径改写，并保留来源。",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.json["data"]["version"], 2)
        stale = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/versions",
            json={"base_version": 1, "title": "陈旧覆盖", "body": "不得覆盖", "citations": artifact["citations"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["details"]["current"]["body"], "用户保留的修订稿：金额仍为120万元。")
        self.assertEqual(stale.json["error"]["details"]["current"]["version"], 2)
        version_one = self.request("get", f"/api/artifacts/{artifact['id']}?version=1")
        self.assertEqual(version_one.json["data"]["body"], artifact["body"])
        restored = self.request("post", f"/api/artifacts/{artifact['id']}/restore", json={"base_version": 2, "restore_version": 1})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json["data"]["version"], 3)
        self.assertEqual(restored.json["data"]["body"], artifact["body"])
        self.assertIn("Restored from version 1", restored.json["data"]["revision_note"])

        md = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/exports",
            json={"version": 3, "format": "md"},
            headers={"Idempotency-Key": "export-md-01"},
        )
        self.assertEqual(md.status_code, 201, md.get_data(as_text=True))
        md_export = md.json["data"]
        downloaded = self.request("get", md_export["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("120万元".encode(), downloaded.data)
        self.assertEqual(hashlib.sha256(downloaded.data).hexdigest(), md_export["sha256"])
        repeated = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/exports",
            json={"version": 3, "format": "md"},
            headers={"Idempotency-Key": "export-md-01"},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["data"]["id"], md_export["id"])

        export_path = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (md_export["id"],))["relative_path"]
        file_path = self.root.joinpath(*Path(export_path).parts)
        with self.store.transaction() as connection:
            connection.execute("UPDATE exports SET status = 'writing', sha256 = NULL, size_bytes = NULL WHERE id = ?", (md_export["id"],))
        verified_existing = self.request("get", md_export["download_url"])
        self.assertEqual(verified_existing.status_code, 200)
        confirmed = self.store.row("SELECT status, sha256 FROM exports WHERE id = ?", (md_export["id"],))
        self.assertEqual(confirmed["status"], "completed")
        self.assertEqual(confirmed["sha256"], hashlib.sha256(verified_existing.data).hexdigest())

        file_path.unlink()
        recovered = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/exports",
            json={"version": 3, "format": "md"},
            headers={"Idempotency-Key": "export-md-01"},
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        self.assertEqual(recovered.json["data"]["id"], md_export["id"])
        self.assertEqual(self.request("get", md_export["download_url"]).status_code, 200)

        docx_response = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/exports",
            json={"version": 3, "format": "docx"},
            headers={"Idempotency-Key": "export-docx-01"},
        )
        self.assertEqual(docx_response.status_code, 201, docx_response.get_data(as_text=True))
        docx_export = docx_response.json["data"]
        docx_file = self.request("get", docx_export["download_url"])
        self.assertEqual(docx_file.status_code, 200)
        document = Document(io.BytesIO(docx_file.data))
        self.assertTrue(any("120万元" in paragraph.text for paragraph in document.paragraphs))

        docx_path = self.root.joinpath(*Path(self.store.row("SELECT relative_path FROM exports WHERE id = ?", (docx_export["id"],))["relative_path"]).parts)
        docx_path.write_bytes(b"unverified altered file")
        integrity_error = self.request("get", docx_export["download_url"])
        self.assertEqual(integrity_error.status_code, 409)
        self.assertEqual(docx_path.read_bytes(), b"unverified altered file")
        retry_corrupt = self.request(
            "post",
            f"/api/artifacts/{artifact['id']}/exports",
            json={"version": 3, "format": "docx"},
            headers={"Idempotency-Key": "export-docx-01"},
        )
        self.assertEqual(retry_corrupt.status_code, 409)
        self.assertEqual(docx_path.read_bytes(), b"unverified altered file")
        status = self.store.row("SELECT status FROM exports WHERE id = ?", (docx_export["id"],))["status"]
        self.assertEqual(status, "interrupted")

    def test_version_and_restore_replays_return_the_exact_saved_revision(self):
        artifact = self.request("post", "/api/artifacts", json={"title": "原稿", "body": "保留原文", "citations": []}).json["data"]
        path = f"/api/artifacts/{artifact['id']}"
        revision = {"base_version": 1, "title": "人工改稿", "body": "已确认修改", "citations": []}
        saved = self.request("post", path + "/versions", json=revision, headers={"Idempotency-Key": "save-once"})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        restored = self.request("post", path + "/restore", json={"base_version": 2, "restore_version": 1}, headers={"Idempotency-Key": "restore-once"})
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        replay = self.request("post", path + "/versions", json=revision, headers={"Idempotency-Key": "save-once"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"]["version"], 2)
        self.assertEqual(replay.json["data"]["body"], "已确认修改")
        replay_restore = self.request("post", path + "/restore", json={"base_version": 2, "restore_version": 1}, headers={"Idempotency-Key": "restore-once"})
        self.assertEqual(replay_restore.status_code, 200)
        self.assertEqual(replay_restore.json["data"]["version"], 3)
        conflict = self.request("post", path + "/versions", json={**revision, "body": "不同的编辑"}, headers={"Idempotency-Key": "save-once"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "IDEMPOTENCY_CONFLICT")
        current = self.request("get", path).json["data"]
        self.assertEqual(current["version"], 3)
        self.assertEqual(current["body"], "保留原文")

    def test_authorized_web_origin_can_send_and_read_chunk_upload_headers(self):
        web_origin = "http://127.0.0.1:5338"
        app = create_app(self.root, expected_port=8317, allowed_origins=[web_origin])
        response = app.test_client().options("/api/uploads/pending", base_url=BASE_URL, headers={
            "Origin": web_origin, "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type,upload-offset,x-chunk-sha256",
        })
        self.assertEqual(response.status_code, 204)
        allowed = {item.strip().lower() for item in response.headers["Access-Control-Allow-Headers"].split(",")}
        self.assertTrue({"upload-offset", "x-chunk-sha256", "content-type"} <= allowed)
        exposed = {item.strip().lower() for item in response.headers["Access-Control-Expose-Headers"].split(",")}
        self.assertIn("upload-offset", exposed)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], web_origin)
        denied = app.test_client().options("/api/uploads/pending", base_url=BASE_URL, headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "PATCH"})
        self.assertEqual(denied.status_code, 403)

    def test_url_acquisition_job_preserves_source_and_recovers_staged_result(self) -> None:
        from server.oevira.acquisition import AcquisitionError

        work = self.create_work(title="网页资料")
        url = "https://news.example/articles/market-plan"
        original = "<!doctype html><html><head><title>Market Plan</title></head><body><p>2026年预算为120万元。</p></body></html>".encode("utf-8")
        projection = "2026年预算为120万元。"
        acquired = {
            "status": "ready",
            "reason": None,
            "url": url,
            "requested_url": url,
            "title": "Market Plan",
            "content": original,
            "text": projection,
            "mime_type": "text/html; charset=utf-8",
            "captured_at": "2026-10-29T09:00:00+00:00",
            "published_at": "2026-10-28T12:00:00Z",
            "published_at_origin": "source_metadata",
            "author": "Editorial team",
            "etag": '"rev-1"',
            "last_modified": "Wed, 28 Oct 2026 12:00:00 GMT",
            "acquisition_method": "public_http",
            "limitations": ["Static page text only."],
        }
        payload = {"url": url, "work_id": work["id"]}
        headers = {**self.auth_headers(), "Idempotency-Key": "url-job-01"}
        queued = self.request("post", "/api/materials/url", json=payload, headers=headers)
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]

        with patch("server.oevira.acquisition.acquire_url", return_value=acquired) as read_source, patch(
            "server.oevira.worker._write_staged_original", side_effect=OSError("interrupted before final file write")
        ):
            self.assertTrue(run_once(self.app))
        read_source.assert_called_once_with(url, etag=None, last_modified=None)
        interrupted = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(interrupted["status"], "failed")
        self.assertEqual(interrupted["error"]["code"], "SOURCE_SAVE_FAILED")
        material_id = interrupted["material_id"]
        self.assertTrue(material_id, "durable job should retain its source recovery pointer")
        self.assertEqual(self.request("get", f"/api/materials/{material_id}").status_code, 404)

        retry = self.request("post", f"/api/jobs/{job_id}/retry")
        self.assertEqual(retry.status_code, 202)
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("staged results must recover without rereading the source")):
            self.assertTrue(run_once(self.app))
        completed = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["material_id"], material_id)
        material_response = self.request("get", f"/api/materials/{material_id}")
        self.assertEqual(material_response.status_code, 200)
        material = material_response.json["data"]
        self.assertEqual(material["format"], "html")
        self.assertEqual(material["source_text"], projection)
        self.assertEqual(material["source"]["url"], url)
        self.assertEqual(material["source"]["captured_at"], acquired["captured_at"])
        self.assertEqual(material["source"]["published_at"], acquired["published_at"])
        self.assertEqual(material["source"]["acquisition_method"], "public_http")
        self.assertEqual(material["text"], projection)
        downloaded = self.request("get", f"/api/materials/{material_id}/file")
        self.assertEqual(downloaded.data, original)
        self.assertEqual(hashlib.sha256(downloaded.data).hexdigest(), material["sha256"])
        self.assertTrue(material["segments"])

        same_key = self.request("post", "/api/materials/url", json=payload, headers=headers)
        self.assertEqual(same_key.status_code, 200)
        self.assertEqual(same_key.json["data"]["id"], job_id)
        unchanged = self.request(
            "post",
            "/api/materials/url",
            json=payload,
            headers={**self.auth_headers(), "Idempotency-Key": "url-job-unchanged"},
        )
        self.assertEqual(unchanged.status_code, 202)
        with patch(
            "server.oevira.acquisition.acquire_url",
            return_value={"status": "unchanged", "url": url, "captured_at": "2026-10-29T10:00:00+00:00", "etag": '"rev-1"', "last_modified": acquired["last_modified"]},
        ) as read_unchanged:
            self.assertTrue(run_once(self.app))
        read_unchanged.assert_called_once_with(url, etag='"rev-1"', last_modified=acquired["last_modified"])
        unchanged_job = self.request("get", f"/api/jobs/{unchanged.json['data']['id']}").json["data"]
        self.assertEqual(unchanged_job["status"], "completed")
        self.assertEqual(unchanged_job["material_id"], material_id)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials WHERE workspace_id = ?", (self.identity["workspace"]["id"],))["count"], 1)

        denied = self.request(
            "post",
            "/api/materials/url",
            json={"url": "http://127.0.0.1/private"},
            headers={**self.auth_headers(), "Idempotency-Key": "url-private-01"},
        )
        self.assertEqual(denied.status_code, 202)
        private_job_id = denied.json["data"]["id"]
        with patch("server.oevira.acquisition.acquire_url", side_effect=AcquisitionError("PRIVATE_SOURCE_DENIED", "Local and private destinations are denied.")):
            self.assertTrue(run_once(self.app))
        private_job = self.request("get", f"/api/jobs/{private_job_id}").json["data"]
        self.assertEqual(private_job["status"], "failed")
        self.assertEqual(private_job["error"]["code"], "PRIVATE_SOURCE_DENIED")
        self.assertIsNone(private_job["material_id"])

    def test_cancelled_url_job_does_not_read_or_create_a_material(self) -> None:
        work = self.create_work(title="取消来源读取")
        queued = self.request(
            "post",
            "/api/materials/url",
            json={"url": "https://news.example/unused", "work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "url-cancel-01"},
        )
        self.assertEqual(queued.status_code, 202)
        job_id = queued.json["data"]["id"]
        cancelled = self.request("post", f"/api/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.json["data"]["status"], "cancelled")
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("cancelled work must never reach the source")):
            self.assertFalse(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials WHERE workspace_id = ?", (self.identity["workspace"]["id"],))["count"], 0)

    def test_startup_recovers_or_closes_interrupted_material_uploads(self) -> None:
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        content = "上传后中断，原文仍可恢复。".encode("utf-8")
        recovered_id = str(uuid.uuid4())
        recovered_relative = f"objects/{workspace_id}/materials/{recovered_id}/original"
        recovered_target = self.root.joinpath(*Path(recovered_relative).parts)
        recovered_target.parent.mkdir(parents=True, exist_ok=True)
        staging_file = recovered_target.parent / ".material-recovery"
        staging_file.write_bytes(content)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, reason, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, ?, ?, 'pending', 'Material is being processed', ?, ?, ?, ?)",
                (recovered_id, workspace_id, user_id, "恢复原件", "recovered.txt", hashlib.sha256(content).hexdigest(), recovered_relative, len(content), now),
            )

        missing_id = str(uuid.uuid4())
        missing_relative = f"objects/{workspace_id}/materials/{missing_id}/original"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, reason, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, ?, ?, 'pending', 'Material is being processed', ?, ?, ?, ?)",
                (missing_id, workspace_id, user_id, "无法恢复原件", "missing.txt", hashlib.sha256(content).hexdigest(), missing_relative, len(content), now),
            )

        unsafe_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, title, filename, status, reason, sha256, relative_path, size_bytes, created_at) VALUES(?, ?, ?, ?, ?, 'pending', 'Material is being processed', ?, ?, ?, ?)",
                (unsafe_id, workspace_id, user_id, "路径不可恢复", "unsafe.txt", hashlib.sha256(content).hexdigest(), "../outside/original", len(content), now),
            )

        create_app(self.root, expected_port=8317)
        recovered = self.request("get", f"/api/materials/{recovered_id}")
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        self.assertEqual(recovered.json["data"]["status"], "ready")
        self.assertEqual(recovered.json["data"]["text"], "上传后中断，原文仍可恢复。")
        self.assertTrue(recovered.json["data"]["segments"])
        self.assertTrue(recovered_target.is_file())
        self.assertFalse(staging_file.exists())

        missing = self.request("get", f"/api/materials/{missing_id}")
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json["data"]["status"], "error")
        self.assertIn("no matching staged file remains", missing.json["data"]["reason"])
        self.assertNotEqual(missing.json["data"]["status"], "pending")

        unsafe = self.request("get", f"/api/materials/{unsafe_id}")
        self.assertEqual(unsafe.status_code, 200)
        self.assertEqual(unsafe.json["data"]["status"], "error")
        self.assertIn("could not be safely located", unsafe.json["data"]["reason"])

    def test_store_schema_migration_preserves_jobs_and_adds_recovery_fields(self) -> None:
        legacy_root = self.root / "legacy-store"
        legacy_root.mkdir()
        database = legacy_root / "oevira.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE works (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, title TEXT NOT NULL,
                goal TEXT NOT NULL, brief_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, work_id TEXT, owner_user_id TEXT NOT NULL,
                title TEXT NOT NULL, current_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE materials (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, work_id TEXT,
                title TEXT NOT NULL, filename TEXT NOT NULL, format TEXT, status TEXT NOT NULL, reason TEXT,
                text TEXT, sha256 TEXT NOT NULL, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, work_id TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, request_hash TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, progress_completed INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0, progress_label TEXT NOT NULL DEFAULT '', artifact_id TEXT,
                error_code TEXT, error_message TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, lease_generation INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace_id, kind, idempotency_key)
            );
            CREATE INDEX jobs_runnable ON jobs(status, created_at);
            INSERT INTO users VALUES ('user-1', 'User', '2026-01-01T00:00:00Z');
            INSERT INTO workspaces VALUES ('workspace-1', 'Workspace', '2026-01-01T00:00:00Z');
            INSERT INTO works VALUES ('work-1', 'workspace-1', 'user-1', 'Work', 'Goal', '{}', 'active', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash,
                             idempotency_key, progress_total, created_at, updated_at)
            VALUES('job-1', 'workspace-1', 'user-1', 'work-1', 'material_comparison', 'queued', '{}', 'hash',
                   'key', 3, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            PRAGMA user_version = 1;
            """
        )
        connection.close()

        migrated = Store(legacy_root)
        row = migrated.row("SELECT id, work_id, material_id, kind, status FROM jobs WHERE id = 'job-1'")
        self.assertEqual(dict(row), {"id": "job-1", "work_id": "work-1", "material_id": None, "kind": "material_comparison", "status": "queued"})
        version = migrated.row("PRAGMA user_version")["user_version"]
        self.assertEqual(version, 5)
        job_columns = {item["name"]: item for item in migrated.rows("PRAGMA table_info(jobs)")}
        self.assertEqual(job_columns["work_id"]["notnull"], 0)
        self.assertEqual(job_columns["material_id"]["notnull"], 0)
        material_columns = {item["name"] for item in migrated.rows("PRAGMA table_info(materials)")}
        self.assertTrue({"source_url", "etag", "last_modified", "source_json", "source_text"} <= material_columns)
        self.assertEqual(migrated.rows("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main()
