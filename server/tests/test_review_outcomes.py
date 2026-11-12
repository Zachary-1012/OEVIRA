from __future__ import annotations

import io
import secrets
import unittest
import uuid

from docx import Document

from server.oevira.api import ApiError
from server.oevira.auth import _digest, utc_now
from server.tests import test_core as core_test


class ReviewOutcomeTests(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request

    def comment(self, artifact_id, version, anchor, *, key="review"):
        response = self.request("post", f"/api/artifacts/{artifact_id}/comments", json={"artifact_version": version, "anchor": anchor, "body": "请核对并精确修改这里"}, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def resolve(self, artifact_id, comment_id, result_version, *, disposition="adopted"):
        return self.request("patch", f"/api/artifacts/{artifact_id}/comments/{comment_id}", json={"base_version": 1, "state": "resolved", "resolution": {"artifact_version": result_version, "disposition": disposition, "reason": "按选定对象核对实际结果"}})

    def test_later_unrelated_version_cannot_mark_selected_text_adopted(self):
        body = "Price 100 RMB. Keep the conclusion."
        artifact = self.request("post", "/api/artifacts", json={"title": "提案", "body": body, "citations": []}).json["data"]
        comment = self.comment(artifact["id"], 1, {"start": 6, "end": 9, "quote": "100"})
        first = self.request("post", f"/api/artifacts/{artifact['id']}/versions", json={"base_version": 1, "title": "提案", "body": "Price 100 RMB. Updated unrelated conclusion.", "citations": []})
        self.assertEqual(first.status_code, 200)
        rejected = self.resolve(artifact["id"], comment["id"], 2)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json["error"]["code"], "COMMENT_TARGET_UNCHANGED")
        open_comment = self.request("get", f"/api/artifacts/{artifact['id']}/comments").json["data"][0]
        self.assertEqual(open_comment["state"], "open")
        changed = self.request("post", f"/api/artifacts/{artifact['id']}/versions", json={"base_version": 2, "title": "提案", "body": "Price 120 RMB. Updated unrelated conclusion.", "citations": []})
        self.assertEqual(changed.status_code, 200)
        accepted = self.resolve(artifact["id"], comment["id"], 3)
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        evidence = accepted.json["data"]["resolution"]["change_evidence"]
        self.assertEqual((evidence["source_version"], evidence["result_version"]), (1, 3))
        self.assertTrue(evidence["changes"])

    def test_not_adopted_can_explain_original_without_manufacturing_a_revision(self):
        artifact = self.request("post", "/api/artifacts", json={"title": "原稿", "body": "已核对", "citations": []}).json["data"]
        comment = self.comment(artifact["id"], 1, {"start": 0, "end": 3, "quote": "已核对"})
        result = self.resolve(artifact["id"], comment["id"], 1, disposition="not_adopted")
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertNotIn("change_evidence", result.json["data"]["resolution"])
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 1)

    def test_resolution_replay_returns_its_saved_version_without_closing_reopened_comment(self):
        artifact = self.request("post", "/api/artifacts", json={"title": "价格", "body": "Price 100 RMB", "citations": []}).json["data"]
        comment = self.comment(artifact["id"], 1, {"start": 6, "end": 9, "quote": "100"})
        revised = self.request("post", f"/api/artifacts/{artifact['id']}/versions", json={"base_version": 1, "title": "价格", "body": "Price 120 RMB", "citations": []})
        self.assertEqual(revised.status_code, 200)
        path = f"/api/artifacts/{artifact['id']}/comments/{comment['id']}"
        decision = {"base_version": 1, "state": "resolved", "resolution": {"artifact_version": 2, "disposition": "adopted", "reason": "价格已精确修改"}}
        saved = self.request("patch", path, json=decision, headers={"Idempotency-Key": "resolve-once"})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.json["data"]["version"], 2)
        reopened = self.request("patch", path, json={"base_version": 2, "state": "open"})
        self.assertEqual(reopened.status_code, 200)
        replay = self.request("patch", path, json=decision, headers={"Idempotency-Key": "resolve-once"})
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["version"], 2)
        current = self.request("get", f"/api/artifacts/{artifact['id']}/comments").json["data"][0]
        self.assertEqual((current["version"], current["state"], current["resolution"]), (3, "open", None))

    def test_unrelated_prefix_is_not_a_change_to_the_selected_word(self):
        artifact = self.request("post", "/api/artifacts", json={"title": "原稿", "body": "Price 100 RMB", "citations": []}).json["data"]
        comment = self.comment(artifact["id"], 1, {"start": 0, "end": 5, "quote": "Price"})
        self.request("post", f"/api/artifacts/{artifact['id']}/versions", json={"base_version": 1, "title": "原稿", "body": "Unrelated preface. Price 100 RMB", "citations": []})
        result = self.resolve(artifact["id"], comment["id"], 2)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json["error"]["code"], "COMMENT_TARGET_UNCHANGED")

    def test_native_word_anchor_tracks_selected_object_and_preserves_other_edits(self):
        document, buffer = Document(), io.BytesIO()
        document.add_paragraph("Price 100 RMB.")
        document.add_paragraph("Original footer.")
        document.save(buffer)
        material = self.request("post", "/api/materials", data={"title": "客户原稿", "file": (io.BytesIO(buffer.getvalue()), "review.docx")}, content_type="multipart/form-data").json["data"]
        inspection = self.request("get", f"/api/materials/{material['id']}/document").json["data"]
        footer = next(item for item in inspection["objects"] if item["text"] == "Original footer.")
        first = self.request("post", f"/api/materials/{material['id']}/document-revisions", json={"changes": [{"object_id": footer["id"], "before": "Original footer.", "after": "Confirmed footer."}]}, headers={"Idempotency-Key": "word-first"})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        artifact_id = first.json["data"]["artifact"]["id"]
        opened = self.request("get", f"/api/artifacts/{artifact_id}/document").json["data"]["document"]
        price = next(item for item in opened["objects"] if item["text"] == "Price 100 RMB.")
        comment = self.comment(artifact_id, 1, {"kind": "document", "object_id": price["id"], "property": "text", "quote": "100"})
        footer = next(item for item in opened["objects"] if item["text"] == "Confirmed footer.")
        unrelated = self.request("post", f"/api/artifacts/{artifact_id}/document-revisions", json={"base_version": 1, "changes": [{"object_id": footer["id"], "before": "Confirmed footer.", "after": "Human confirmed footer."}]}, headers={"Idempotency-Key": "word-footer"})
        self.assertEqual(unrelated.status_code, 200, unrelated.get_data(as_text=True))
        self.assertEqual(self.resolve(artifact_id, comment["id"], 2).status_code, 409)
        opened = self.request("get", f"/api/artifacts/{artifact_id}/document").json["data"]["document"]
        price = next(item for item in opened["objects"] if item["text"] == "Price 100 RMB.")
        revised = self.request("post", f"/api/artifacts/{artifact_id}/document-revisions", json={"base_version": 2, "changes": [{"object_id": price["id"], "before": "100", "after": "120"}]}, headers={"Idempotency-Key": "word-price"})
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        accepted = self.resolve(artifact_id, comment["id"], 3)
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.json["data"]["resolution"]["change_evidence"]["target"]["kind"], "document")

    def test_native_private_artifact_is_hidden_from_other_workspace_members(self):
        from server.oevira.community import require_native_artifact_access

        created = self.request("post", "/api/publications", json={"kind": "work", "visibility": "private", "content": {"title": "私稿", "body": "只供作者继续创作。", "summary": ""}}, headers={"Idempotency-Key": "native-private"})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        publication = created.json["data"]
        artifact_id = publication["artifact_id"]
        export = self.request("post", f"/api/artifacts/{artifact_id}/exports", json={"version": 1, "format": "md"}, headers={"Idempotency-Key": "native-private-export"})
        self.assertEqual(export.status_code, 201, export.get_data(as_text=True))
        ordinary = self.request("post", "/api/artifacts", json={"title": "共享成果", "body": "Workspace内普通共享作品", "citations": []}).json["data"]
        user_id, token, now = str(uuid.uuid4()), secrets.token_urlsafe(32), utc_now()
        workspace_id = self.identity["workspace"]["id"]
        guest = {"user": {"id": user_id}, "workspace": {"id": workspace_id}, "role": "editor"}
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)", (user_id, "同空间成员", now))
            connection.execute("INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?,'editor',1,?)", (workspace_id, user_id, now))
            connection.execute("INSERT INTO sessions(id,token_hash,user_id,workspace_id,created_at,expires_at) VALUES(?,?,?,?,?,'2099-01-01T00:00:00Z')", (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now))
            with self.assertRaises(ApiError) as denied:
                require_native_artifact_access(connection, guest, artifact_id)
            self.assertEqual(denied.exception.status, 404)
            self.assertFalse(require_native_artifact_access(connection, guest, ordinary["id"]))
        headers = {"Authorization": "Bearer " + token}
        for path in (f"/api/artifacts/{artifact_id}", f"/api/artifacts/{artifact_id}?version=1", export.json["data"]["download_url"], f"/api/artifacts/{artifact_id}/comments"):
            response = self.request("get", path, headers=headers)
            self.assertEqual(response.status_code, 404, f"{path}: {response.status_code}")
        rows = self.request("get", "/api/artifacts", headers=headers).json["data"]
        self.assertNotIn(artifact_id, {item["id"] for item in rows})
        self.assertIn(ordinary["id"], {item["id"] for item in rows})
        for suffix, data in (
            ("versions", {"base_version": 1, "title": "不得覆盖", "body": "其它成员的内容", "citations": []}),
            ("restore", {"base_version": 1, "restore_version": 1}),
            ("exports", {"version": 1, "format": "md"}),
            ("comments", {"artifact_version": 1, "anchor": {"start": 0, "end": 2, "quote": "只供"}, "body": "不应能评论"}),
        ):
            response = self.request("post", f"/api/artifacts/{artifact_id}/{suffix}", json=data, headers={**headers, "Idempotency-Key": "deny-" + suffix})
            self.assertEqual(response.status_code, 404, f"{suffix}: {response.get_data(as_text=True)}")


if __name__ == "__main__":
    unittest.main()
