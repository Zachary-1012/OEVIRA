from __future__ import annotations

import unittest

from server.oevira import create_app
from server.tests import test_core as core_test


class MediaReviewTests(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request
    create_work = core_test.CoreApiTest.create_work
    create_other_workspace = core_test.CoreApiTest.create_other_workspace

    def project(self):
        response = self.request("post", "/api/media/projects", json={"title": "音轨意见", "kind": "podcast"}, headers={"Idempotency-Key": "review-media"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        project_id = response.json["data"]["id"]
        response = self.request("post", f"/api/media/projects/{project_id}/edits", json={"base_version": 1, "operations": [{"op": "add_track", "track_id": "voice", "kind": "voiceover"}, {"op": "add_track", "track_id": "music", "kind": "bgm"}]}, headers={"Idempotency-Key": "review-tracks"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["data"]

    def comment(self, project):
        response = self.request("post", f"/api/media/projects/{project['id']}/comments", json={"project_version": project["version"], "anchor": {"kind": "media_track", "object_id": "voice", "property": "gain"}, "body": "只调整人声轨音量，保留其它轨道"}, headers={"Idempotency-Key": "media-comment"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def test_native_media_comment_requires_selected_property_change_and_cold_reopens(self):
        project = self.project()
        comment = self.comment(project)
        self.assertIsNone(comment["artifact_id"])
        self.assertEqual(comment["target_ref"], {"kind": "media_project", "id": project["id"], "version": 2})
        changed_music = self.request("post", f"/api/media/projects/{project['id']}/edits", json={"base_version": 2, "operations": [{"op": "track_audio_routing", "track_id": "music", "gain": 0.3}]}, headers={"Idempotency-Key": "only-music"})
        self.assertEqual(changed_music.status_code, 200, changed_music.get_data(as_text=True))
        path = f"/api/media/projects/{project['id']}/comments/{comment['id']}"
        rejected = self.request("patch", path, json={"base_version": 1, "state": "resolved", "resolution": {"project_version": 3, "disposition": "adopted", "reason": "音量已修改"}})
        self.assertEqual(rejected.status_code, 409, rejected.get_data(as_text=True))
        self.assertEqual(rejected.json["error"]["code"], "COMMENT_TARGET_UNCHANGED")
        changed_voice = self.request("post", f"/api/media/projects/{project['id']}/edits", json={"base_version": 3, "operations": [{"op": "track_audio_routing", "track_id": "voice", "gain": 0.5}]}, headers={"Idempotency-Key": "only-voice"})
        self.assertEqual(changed_voice.status_code, 200, changed_voice.get_data(as_text=True))
        payload = {"base_version": 1, "state": "resolved", "resolution": {"project_version": 4, "disposition": "adopted", "reason": "人声轨设置为0.5，其它内容保持"}}
        saved = self.request("patch", path, json=payload, headers={"Idempotency-Key": "media-resolve"})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        evidence = saved.json["data"]["resolution"]["change_evidence"]
        self.assertEqual((evidence["source_version"], evidence["result_version"]), (2, 4))
        self.assertEqual(evidence["changes"], [{"property": "gain", "before": 1.0, "after": 0.5}])
        self.app = create_app(self.root, expected_port=8317)
        self.client = self.app.test_client()
        self.client.set_cookie("oevira_session", self.token, domain="127.0.0.1")
        reread = self.request("get", f"/api/media/projects/{project['id']}/comments")
        self.assertEqual(reread.status_code, 200, reread.get_data(as_text=True))
        self.assertEqual(reread.json["data"][0], saved.json["data"])
        replay = self.request("patch", path, json=payload, headers={"Idempotency-Key": "media-resolve"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"], saved.json["data"])
        with self.store.transaction(immediate=False) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_comment_versions WHERE comment_id=?", (comment["id"],)).fetchone()[0], 2)

    def test_wrong_target_version_and_workspace_do_not_disclose_media_comments(self):
        project = self.project()
        comment = self.comment(project)
        invalid = self.request("post", f"/api/media/projects/{project['id']}/comments", json={"project_version": 1, "anchor": {"kind": "media_track", "object_id": "voice", "property": "gain"}, "body": "此轨在旧版并不存在"}, headers={"Idempotency-Key": "bad-old-target"})
        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(invalid.json["error"]["code"], "MEDIA_REVIEW_TARGET_MISSING")
        token, _ = self.create_other_workspace()
        headers = {"Authorization": "Bearer " + token}
        denied = self.request("get", f"/api/media/projects/{project['id']}/comments", headers=headers)
        self.assertEqual(denied.status_code, 404)
        denied_write = self.request("patch", f"/api/media/projects/{project['id']}/comments/{comment['id']}", json={"base_version": 1, "body": "不得覆盖"}, headers=headers)
        self.assertEqual(denied_write.status_code, 404)
        self.assertEqual(self.request("get", f"/api/media/projects/{project['id']}/comments").json["data"][0]["version"], 1)


if __name__ == "__main__":
    unittest.main()
