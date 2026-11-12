from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from oevira import create_app
from oevira.auth import authenticate, issue_local_capability, utc_now
from oevira.source_use import record_source_use_decision


BASE_URL = "http://127.0.0.1:8331"


class NotesContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-notes-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8331)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        self.token = None
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post",
            "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "便笺作者"},
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

    def create_work(self, title: str = "便笺工作", *, commission_id: str | None = None, key: str | None = None):
        payload = {"title": title, "goal": "验证便笺合同"}
        if commission_id:
            payload["commission_id"] = commission_id
        response = self.request(
            "post", "/api/works", json=payload,
            headers={"Idempotency-Key": key or f"work-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_artifact(self, work_id: str, *, title: str = "当前稿", body: str = "初稿正文。", key: str | None = None):
        response = self.request(
            "post", "/api/artifacts",
            json={"title": title, "body": body, "citations": [], "work_id": work_id},
            headers={"Idempotency-Key": key or f"artifact-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_material(self, work_id: str, *, text: str = "合同金额 120万元。交付日期不变。"):
        response = self.request(
            "post", "/api/materials",
            json={"title": "便笺来源材料", "filename": "note-source.txt", "text": text, "work_id": work_id},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_note(self, body: str, **payload):
        data = {"body": body, **payload}
        headers = {}
        key = data.pop("idempotency_key", None)
        expected = data.pop("expected_status", 201)
        if key:
            headers["Idempotency-Key"] = key
        response = self.request("post", "/api/notes", json=data, headers=headers)
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json["data"] if response.is_json and "data" in (response.json or {}) else response

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

    def test_create_and_get_note(self) -> None:
        work = self.create_work()
        note = self.create_note(
            "今天先记下对公开报价的疑问。",
            status="draft",
            work_id=work["id"],
            anchor={"kind": "paragraph", "label": "开场"},
        )
        self.assertEqual(note["body"], "今天先记下对公开报价的疑问。")
        self.assertEqual(note["status"], "draft")
        self.assertFalse(note["hidden"])
        self.assertEqual(note["kind"], "original")
        self.assertEqual(note["version"], 1)
        self.assertFalse(note["training_rights"])
        self.assertEqual(note["work_id"], work["id"])
        self.assertEqual(note["author_id"], self.identity["user"]["id"])
        fetched = self.request("get", f"/api/notes/{note['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.get_data(as_text=True))
        self.assertEqual(fetched.json["data"]["id"], note["id"])
        listed = self.request("get", f"/api/notes?work_id={work['id']}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["id"] for item in listed.json["data"]], [note["id"]])

    def test_hide_excludes_from_default_list_and_reopen_restores(self) -> None:
        note = self.create_note("关闭后仍可按标识打开。", status="saved")
        hidden = self.request("patch", f"/api/notes/{note['id']}", json={"base_version": 1, "hidden": True})
        self.assertEqual(hidden.status_code, 200, hidden.get_data(as_text=True))
        self.assertTrue(hidden.json["data"]["hidden"])
        self.assertEqual(hidden.json["data"]["version"], 2)
        self.assertEqual(hidden.json["data"]["body"], "关闭后仍可按标识打开。")
        listed = self.request("get", "/api/notes")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json["data"], [])
        still = self.request("get", f"/api/notes/{note['id']}")
        self.assertEqual(still.status_code, 200)
        self.assertTrue(still.json["data"]["hidden"])
        with_hidden = self.request("get", "/api/notes?include_hidden=1")
        self.assertEqual([item["id"] for item in with_hidden.json["data"]], [note["id"]])
        reopened = self.request("patch", f"/api/notes/{note['id']}", json={"base_version": 2, "hidden": False})
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertFalse(reopened.json["data"]["hidden"])
        listed_again = self.request("get", "/api/notes")
        self.assertEqual([item["id"] for item in listed_again.json["data"]], [note["id"]])
        deleted = self.request("delete", f"/api/notes/{note['id']}")
        self.assertEqual(deleted.status_code, 405, deleted.get_data(as_text=True))
        self.assertEqual(deleted.json["error"]["code"], "METHOD_NOT_ALLOWED")
        still_after_delete = self.request("get", f"/api/notes/{note['id']}")
        self.assertEqual(still_after_delete.status_code, 200)
        self.assertEqual(still_after_delete.json["data"]["body"], "关闭后仍可按标识打开。")

    def test_patch_version_conflict(self) -> None:
        note = self.create_note("需要按版本保存。")
        first = self.request("patch", f"/api/notes/{note['id']}", json={"base_version": 1, "body": "第一次保存。", "status": "saved"})
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.json["data"]["status"], "saved")
        conflict = self.request("patch", f"/api/notes/{note['id']}", json={"base_version": 1, "body": "过期再保存。"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(conflict.json["error"]["details"]["current"]["body"], "第一次保存。")
        self.assertEqual(conflict.json["error"]["details"]["current"]["version"], 2)

    def test_create_is_idempotent_and_rejects_conflicting_replay(self) -> None:
        first = self.create_note("同一键只创建一次。", idempotency_key="note-create-once")
        replay = self.request(
            "post", "/api/notes",
            json={"body": "同一键只创建一次。"},
            headers={"Idempotency-Key": "note-create-once"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["id"], first["id"])
        conflict = self.request(
            "post", "/api/notes",
            json={"body": "同一键却换了正文。"},
            headers={"Idempotency-Key": "note-create-once"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_organize_preserves_original_and_is_idempotent(self) -> None:
        original = self.create_note("原始摘录：报价包含差旅，但不含税费。", status="saved")
        organized = self.request(
            "post", f"/api/notes/{original['id']}/organize",
            json={"body": "整理后：报价含差旅、不含税费。", "status": "saved"},
            headers={"Idempotency-Key": "organize-once"},
        )
        self.assertEqual(organized.status_code, 201, organized.get_data(as_text=True))
        candidate = organized.json["data"]
        self.assertEqual(candidate["kind"], "organized_candidate")
        self.assertEqual(candidate["parent_note_id"], original["id"])
        self.assertEqual(candidate["body"], "整理后：报价含差旅、不含税费。")
        replay = self.request(
            "post", f"/api/notes/{original['id']}/organize",
            json={"body": "整理后：报价含差旅、不含税费。", "status": "saved"},
            headers={"Idempotency-Key": "organize-once"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["id"], candidate["id"])
        still = self.request("get", f"/api/notes/{original['id']}")
        self.assertEqual(still.json["data"]["body"], "原始摘录：报价包含差旅，但不含税费。")
        self.assertEqual(still.json["data"]["version"], 1)
        self.assertEqual(still.json["data"]["kind"], "original")

    def test_adopt_portion_adds_citation_and_never_grants_training_rights(self) -> None:
        work = self.create_work()
        material = self.create_material(work["id"], text="合同金额 120万元。交付日期不变。")
        segment = material["segments"][0]
        quote = "合同金额 120万元。"
        self.assertIn(quote, segment["text"])
        note = self.create_note(
            "摘录：合同金额 120万元。先记在便笺里。",
            status="saved",
            work_id=work["id"],
            source_ref={
                "kind": "material",
                "id": material["id"],
                "material_id": material["id"],
                "segment_id": segment["id"],
                "quote": quote,
            },
        )
        artifact = self.create_artifact(work["id"], body="当前稿开头。")
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={
                "artifact_id": artifact["id"],
                "base_artifact_version": artifact["version"],
                "excerpt": quote,
            },
            headers={"Idempotency-Key": "adopt-once"},
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        payload = adopted.json["data"]
        self.assertFalse(payload["note"]["training_rights"])
        self.assertEqual(payload["note"]["adopted_artifact_id"], artifact["id"])
        self.assertEqual(payload["note"]["adopted_excerpt"], quote)
        self.assertIsNone(payload["note"]["adoption_revoked_at"])
        self.assertIn(quote, payload["artifact"]["body"])
        self.assertEqual(payload["artifact"]["version"], artifact["version"] + 1)
        self.assertEqual(
            payload["artifact"]["citations"],
            [{"material_id": material["id"], "segment_id": segment["id"], "quote": quote}],
        )
        replay = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={
                "artifact_id": artifact["id"],
                "base_artifact_version": artifact["version"],
                "excerpt": quote,
            },
            headers={"Idempotency-Key": "adopt-once"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["artifact"]["version"], payload["artifact"]["version"])
        self.assertFalse(replay.json["data"]["note"]["training_rights"])

    def test_revoke_adoption_keeps_the_note_and_does_not_erase_the_artifact(self) -> None:
        work = self.create_work()
        note = self.create_note("个人判断：时间表应提前一周。", status="saved", work_id=work["id"])
        artifact = self.create_artifact(work["id"], body="当前稿。")
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={
                "artifact_id": artifact["id"],
                "base_artifact_version": 1,
                "excerpt": "个人判断：时间表应提前一周。",
            },
            headers={"Idempotency-Key": "adopt-personal"},
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        revoked = self.request("post", f"/api/notes/{note['id']}/revoke-adoption")
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        data = revoked.json["data"]
        self.assertEqual(data["body"], "个人判断：时间表应提前一周。")
        self.assertIsNotNone(data["adoption_revoked_at"])
        self.assertEqual(data["adopted_artifact_id"], artifact["id"])
        self.assertFalse(data["training_rights"])
        still = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(still.status_code, 200, still.get_data(as_text=True))
        self.assertIn("个人判断：时间表应提前一周。", still.json["data"]["body"])
        again = self.request("post", f"/api/notes/{note['id']}/revoke-adoption")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json["error"]["code"], "ADOPTION_NOT_ACTIVE")

    def test_sourceless_adopt_does_not_invent_evidence(self) -> None:
        work = self.create_work()
        note = self.create_note("这是我自己的判断，没有对应材料。", status="saved", work_id=work["id"])
        self.assertIsNone(note["source_ref"])
        artifact = self.create_artifact(work["id"], body="已有正文。")
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={
                "artifact_id": artifact["id"],
                "base_artifact_version": 1,
                "excerpt": "这是我自己的判断，没有对应材料。",
            },
            headers={"Idempotency-Key": "adopt-sourceless"},
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        self.assertEqual(adopted.json["data"]["artifact"]["citations"], [])
        self.assertFalse(adopted.json["data"]["note"]["training_rights"])

    def test_other_author_cannot_see_or_change_a_note(self) -> None:
        foreign_id = str(uuid.uuid4())
        workspace_id = self.identity["workspace"]["id"]
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO notes("
                "id, workspace_id, author_id, work_id, body, status, hidden, kind, parent_note_id, "
                "source_ref_json, anchor_json, excerpt_kind, version, training_rights, created_at, updated_at"
                ") VALUES(?, ?, ?, NULL, '别人的便笺', 'saved', 0, 'original', NULL, '{}', '{}', NULL, 1, 0, ?, ?)",
                (foreign_id, workspace_id, "other-author", now, now),
            )
        fetched = self.request("get", f"/api/notes/{foreign_id}")
        self.assertEqual(fetched.status_code, 404)
        self.assertEqual(fetched.json["error"]["code"], "NOT_FOUND")
        patched = self.request("patch", f"/api/notes/{foreign_id}", json={"base_version": 1, "body": "不应改到。"})
        self.assertEqual(patched.status_code, 404)
        listed = self.request("get", "/api/notes")
        self.assertEqual(listed.json["data"], [])

    def test_cross_commission_adopt_is_rejected_and_does_not_move_materials(self) -> None:
        first = self.insert_commission("甲客户")
        second = self.insert_commission("乙客户")
        work_a = self.create_work("甲工作", commission_id=first, key="work-a")
        work_b = self.create_work("乙工作", commission_id=second, key="work-b")
        material = self.create_material(work_a["id"], text="仅属于甲客户的原文。")
        segment = material["segments"][0]
        note = self.create_note(
            "仅属于甲客户的原文。",
            status="saved",
            work_id=work_a["id"],
            source_ref={
                "kind": "material",
                "material_id": material["id"],
                "segment_id": segment["id"],
                "quote": "仅属于甲客户的原文。",
            },
        )
        artifact = self.create_artifact(work_b["id"], body="乙客户当前稿。")
        before = self.request("get", f"/api/works/{work_a['id']}")
        self.assertIn(material["id"], before.json["data"]["material_ids"])
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={"artifact_id": artifact["id"], "base_artifact_version": 1, "excerpt": "仅属于甲客户的原文。"},
            headers={"Idempotency-Key": "adopt-cross"},
        )
        self.assertEqual(adopted.status_code, 409, adopted.get_data(as_text=True))
        self.assertEqual(adopted.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        after_a = self.request("get", f"/api/works/{work_a['id']}")
        after_b = self.request("get", f"/api/works/{work_b['id']}")
        self.assertIn(material["id"], after_a.json["data"]["material_ids"])
        self.assertNotIn(material["id"], after_b.json["data"].get("material_ids", []))
        still = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(still.json["data"]["body"], "乙客户当前稿。")
        self.assertEqual(still.json["data"]["version"], 1)

    def test_denied_source_use_blocks_adopt(self) -> None:
        work = self.create_work()
        material = self.create_material(work["id"], text="已被拒绝使用的原文。")
        segment = material["segments"][0]
        note = self.create_note(
            "已被拒绝使用的原文。",
            status="saved",
            work_id=work["id"],
            source_ref={
                "kind": "material",
                "material_id": material["id"],
                "segment_id": segment["id"],
                "quote": "已被拒绝使用的原文。",
            },
        )
        artifact = self.create_artifact(work["id"], body="当前稿。")
        actor = authenticate(self.store, self.token)
        self.assertIsNotNone(actor)
        with self.store.transaction() as connection:
            record_source_use_decision(
                connection, actor,
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="note.adopt", action="cite",
                recipient={"kind": "local", "id": self.identity["workspace"]["id"]},
                basis_status="denied", decision_status="denied",
                work_id=work["id"],
            )
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={"artifact_id": artifact["id"], "base_artifact_version": 1, "excerpt": "已被拒绝使用的原文。"},
            headers={"Idempotency-Key": "adopt-denied"},
        )
        self.assertEqual(adopted.status_code, 403, adopted.get_data(as_text=True))
        self.assertIn(adopted.json["error"]["code"], {"SOURCE_USE_DENIED", "SOURCE_USE_NOT_ALLOWED", "SOURCE_USE_REVOKED"})
        still = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(still.json["data"]["body"], "当前稿。")
        self.assertEqual(still.json["data"]["version"], 1)

    def test_adopt_into_research_artifact_is_rejected(self) -> None:
        work = self.create_work()
        note = self.create_note("不能直接改研究稿。", status="saved", work_id=work["id"])
        artifact = self.create_artifact(work["id"], body="研究结论草稿。")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE artifact_versions SET input_snapshot_json=? WHERE artifact_id=? AND version=1",
                (json.dumps({"kind": "research"}), artifact["id"]),
            )
        adopted = self.request(
            "post", f"/api/notes/{note['id']}/adopt",
            json={"artifact_id": artifact["id"], "base_artifact_version": 1, "excerpt": "不能直接改研究稿。"},
            headers={"Idempotency-Key": "adopt-research"},
        )
        self.assertEqual(adopted.status_code, 409, adopted.get_data(as_text=True))
        self.assertEqual(adopted.json["error"]["code"], "RESEARCH_REVISION_REQUIRED")

    def test_draft_can_be_saved_and_training_rights_is_not_writable(self) -> None:
        note = self.create_note("先作为草稿。", status="draft")
        self.assertEqual(note["status"], "draft")
        rejected = self.request(
            "post", "/api/notes",
            json={"body": "不应写入训练权。", "training_rights": True},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["error"]["code"], "INVALID_INPUT")
        saved = self.request("patch", f"/api/notes/{note['id']}", json={"base_version": 1, "status": "saved"})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.json["data"]["status"], "saved")
        self.assertFalse(saved.json["data"]["training_rights"])


if __name__ == "__main__":
    unittest.main()
