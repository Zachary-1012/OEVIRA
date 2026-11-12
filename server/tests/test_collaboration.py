from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid

from oevira import create_app
from oevira.api import ApiError
from oevira.auth import _digest, issue_local_capability, utc_now
from oevira.collaboration import require_artifact_access, require_artifact_control_access


BASE_URL = "http://127.0.0.1:8322"


class CollaborationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-collaboration-boundary-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8322)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "协作权限测试"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

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

    def test_archived_client_work_allows_pause_but_keeps_write_and_membership_checks(self) -> None:
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        commission_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                (commission_id, workspace_id, "暂停边界隔离客户", user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'editor',1)",
                (commission_id, user_id),
            )
        created_work = self.request(
            "post", "/api/works",
            json={"title": "隔离待暂停工作", "goal": "验证委托范围的任务控制", "commission_id": commission_id},
            headers={**self.auth_headers(), "Idempotency-Key": "pause-boundary-work"},
        )
        self.assertEqual(created_work.status_code, 201, created_work.get_data(as_text=True))
        work_id = created_work.json["data"]["id"]
        queued = self.request(
            "post", f"/api/works/{work_id}/business-jobs",
            json={
                "operation": "service_response",
                "inputs": {"goal": "记录暂停前的服务回应分析", "as_of": "2026-11-10T00:00:00Z"},
            },
            headers={**self.auth_headers(), "Idempotency-Key": "pause-boundary-job"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE works SET status='archived',version=version+1,updated_at=? WHERE id=?", (utc_now(), work_id))

        paused = self.request(
            "post", f"/api/jobs/{job_id}/pause",
            json={"operation_id": "pause-archived-client-job", "base_control_revision": 0},
            headers=self.auth_headers(),
        )
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        self.assertEqual(paused.json["data"]["status"], "paused")
        self.assertEqual(paused.json["data"]["control_receipt"]["action"], "pause")

        ordinary_write = self.request(
            "patch", f"/api/works/{work_id}",
            json={"base_version": 2, "title": "归档工作仍不得写入"},
            headers=self.auth_headers(),
        )
        self.assertEqual(ordinary_write.status_code, 409, ordinary_write.get_data(as_text=True))
        self.assertEqual(ordinary_write.json["error"]["code"], "WORK_ARCHIVED")

        outsider_id = str(uuid.uuid4())
        outsider_token = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)", (outsider_id, "未获委托访问用户", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?, 'editor',1,?)",
                (workspace_id, outsider_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id,token_hash,user_id,workspace_id,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), _digest(outsider_token), outsider_id, workspace_id, now, expiry),
            )
        denied = self.request(
            "post", f"/api/jobs/{job_id}/pause",
            json={"operation_id": "outsider-cannot-pause-client-job", "base_control_revision": 1},
            headers=self.auth_headers(outsider_token),
        )
        self.assertEqual(denied.status_code, 404, denied.get_data(as_text=True))

    def test_business_plan_control_allows_archived_owner_editor_and_own_reviewer_approval_only(self) -> None:
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        commission_id, work_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        approval_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','archived',1,?,?,?)",
                (commission_id, workspace_id, "归档计划客户", user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'editor',1)",
                (commission_id, user_id),
            )
            connection.execute(
                "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'{}','archived',1,?,?)",
                (work_id, workspace_id, user_id, "归档的执行计划", "取消未执行动作", now, now),
            )
            connection.execute("INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)", (work_id, commission_id, now))
            connection.execute(
                "INSERT INTO artifacts(id,workspace_id,work_id,owner_user_id,title,current_version,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                (artifact_id, workspace_id, work_id, user_id, "已审批的计划", now, now),
            )
            connection.execute(
                "INSERT INTO artifact_approvals(id,artifact_id,artifact_version,workspace_id,approved_by,purpose,scope_json,created_at) VALUES(?,?,1,?,?,?,'{}',?)",
                (approval_id, artifact_id, workspace_id, user_id, "approve_action", now),
            )

        with self.store.transaction(immediate=False) as connection:
            control = require_artifact_control_access(connection, self.identity, artifact_id)
            self.assertEqual(control["id"], artifact_id)
            with self.assertRaises(ApiError) as ordinary_write:
                require_artifact_access(connection, self.identity, artifact_id, write=True)
            self.assertEqual(ordinary_write.exception.status, 409)

        with self.store.transaction() as connection:
            connection.execute("UPDATE client_commissions SET status='active' WHERE id=?", (commission_id,))
            connection.execute("UPDATE works SET status='active' WHERE id=?", (work_id,))
            connection.execute("UPDATE commission_members SET role='reviewer' WHERE commission_id=? AND user_id=?", (commission_id, user_id))
        with self.store.transaction(immediate=False) as connection:
            stored_approval = connection.execute(
                "SELECT approved_by FROM artifact_approvals WHERE id=? AND artifact_id=? AND workspace_id=?",
                (approval_id, artifact_id, workspace_id),
            ).fetchone()
            reviewer_control = require_artifact_control_access(
                connection, self.identity, artifact_id, approval_owner_id=stored_approval["approved_by"],
            )
            self.assertEqual(reviewer_control["id"], artifact_id)
            with self.assertRaises(ApiError) as reviewer_cannot_edit:
                require_artifact_access(connection, self.identity, artifact_id, write=True)
            self.assertEqual(reviewer_cannot_edit.exception.status, 403)
            with self.assertRaises(ApiError) as cannot_withdraw_other:
                require_artifact_control_access(
                    connection, self.identity, artifact_id, approval_owner_id=str(uuid.uuid4()),
                )
            self.assertEqual(cannot_withdraw_other.exception.status, 403)

        with self.store.transaction() as connection:
            connection.execute("UPDATE commission_members SET role='editor' WHERE commission_id=? AND user_id=?", (commission_id, user_id))
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaises(ApiError) as editor_cannot_withdraw_review:
                require_artifact_control_access(
                    connection, self.identity, artifact_id, approval_owner_id=user_id,
                )
            self.assertEqual(editor_cannot_withdraw_review.exception.status, 403)

        with self.store.transaction() as connection:
            connection.execute("UPDATE commission_members SET active=0 WHERE commission_id=? AND user_id=?", (commission_id, user_id))
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaises(ApiError) as revoked_membership:
                require_artifact_control_access(connection, self.identity, artifact_id)
            self.assertEqual(revoked_membership.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
