from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import struct
import unittest
import wave

import test_business_execution as business_test
from oevira.community import (_publication_content_hash, _publication_rights_digest,
                              install_community_schema, prepare_account_deletion, register_community_routes)
from oevira.collaboration import register_review_routes, require_artifact_approval
from oevira.api import ApiError
from oevira.auth import utc_now
from oevira.community_media import install_community_media_schema
from oevira import community as community_domain
from oevira import community_media as community_media_domain
from oevira.media import install_media_schema
from oevira.store import Store
from oevira.worker import run_once


class CommunityTests(unittest.TestCase):
    setUp = business_test.BusinessExecutionTests.setUp
    tearDown = business_test.BusinessExecutionTests.tearDown
    request = business_test.BusinessExecutionTests.request
    post = business_test.BusinessExecutionTests.post
    make_result = business_test.BusinessExecutionTests.make_result
    add_member = business_test.BusinessExecutionTests.add_member

    def new_app(self):
        app = business_test.BusinessExecutionTests.new_app(self)
        install_community_schema(app.extensions["oevira.store"])
        register_community_routes(app)
        register_review_routes(app)
        return app

    def public_content(self):
        return {"title": "活动复盘方案模板", "summary": "按问题、依据和行动整理活动复盘", "body": "供使用者按自身材料改写的复盘结构。",
                "license": {"view": True, "download": True, "adapt": True, "commercial": True, "redistribute": False, "training": False, "attribution": "注明原作者", "expires_at": None},
                "privacy_review": {"selected_content_only": True}, "dependencies": [], "tags": ["活动复盘"],
                "template": {"schema": "oevira-document-template/1", "blocks": [{"id": "goal", "kind": "heading", "content": "{{brand}}活动复盘"}, {"id": "method", "kind": "paragraph", "content": "先保原始观察和时间范围，再解释行动与结果。"}],
                             "slots": [{"name": "brand", "label": "品牌名", "required": True, "default": "", "max_length": 100}]}}

    def upload_still_image(self, title: str):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        uploaded = self.request(
            "post", "/api/materials",
            data={"file": (io.BytesIO(image), f"{title}.png"), "work_id": self.work["id"]},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        return uploaded.json["data"], image

    def test_native_private_post_revision_and_restore_share_one_artifact_version_owner(self):
        first_content = {
            "title": "",
            "summary": "",
            "body": "今天在花园里看到第一朵花。",
            "blocks": [{"block_id": "p-intro", "kind": "paragraph", "text": "今天在花园里看到第一朵花。"}],
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "private", "content": first_content,
        }, key="native-short-private-01", status=(201,))
        self.assertEqual(draft["source"]["kind"], "artifact")
        self.assertEqual(draft["artifact_version"], 1)
        artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}").json["data"]
        self.assertEqual(artifact["body"], first_content["body"])
        self.assertEqual(artifact["input_snapshot"]["kind"], "community_native")
        self.assertEqual(artifact["input_snapshot"]["community_native"]["publication_id"], draft["id"])

        revised_content = {
            **first_content,
            "body": "今天在花园里看到第一朵白色的花。",
            "blocks": [{"block_id": "p-intro", "kind": "paragraph", "text": "今天在花园里看到第一朵白色的花。"}],
        }
        revised = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": 1, "content": revised_content},
            headers={"Idempotency-Key": "native-short-revision-02"},
        )
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        publication_v2 = revised.json["data"]
        self.assertEqual(publication_v2["artifact_version"], 2)
        self.assertEqual(publication_v2["version"], 2)
        artifact_v2 = self.request("get", f"/api/artifacts/{draft['artifact_id']}").json["data"]
        self.assertEqual(artifact_v2["version"], 2)
        self.assertEqual(artifact_v2["body"], revised_content["body"])
        self.assertEqual(artifact_v2["input_snapshot"]["community_native"]["blocks"], revised_content["blocks"])
        revision_replay = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": 1, "content": revised_content},
            headers={"Idempotency-Key": "native-short-revision-02"},
        )
        self.assertEqual(revision_replay.status_code, 200, revision_replay.get_data(as_text=True))
        self.assertEqual(revision_replay.json["data"]["version"], publication_v2["version"])
        self.assertEqual(revision_replay.json["data"]["content"]["body"], revised_content["body"])

        generic_revision = self.request(
            "post", f"/api/artifacts/{draft['artifact_id']}/versions",
            json={"base_version": 2, "title": artifact_v2["title"], "body": "旁路改动", "citations": artifact_v2["citations"]},
        )
        self.assertEqual(generic_revision.status_code, 409)
        self.assertEqual(generic_revision.json["error"]["code"], "COMMUNITY_REVISION_REQUIRED")

        restored = self.post(
            f"/api/publications/{draft['id']}/restore",
            {"base_version": 2, "restore_version": 1},
            key="native-short-restore-01", status=(200,),
        )
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["artifact_version"], 3)
        self.assertEqual(restored["content"]["body"], first_content["body"])
        restored_artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}").json["data"]
        self.assertEqual(restored_artifact["version"], 3)
        self.assertEqual(restored_artifact["body"], first_content["body"])
        restored_version = self.store.row(
            "SELECT parent_version FROM artifact_versions WHERE artifact_id=? AND version=3",
            (draft["artifact_id"],),
        )
        self.assertEqual(restored_version["parent_version"], 2)
        generic_restore = self.request(
            "post", f"/api/artifacts/{draft['artifact_id']}/restore",
            json={"base_version": 3, "restore_version": 1},
        )
        self.assertEqual(generic_restore.status_code, 409)
        self.assertEqual(generic_restore.json["error"]["code"], "COMMUNITY_REVISION_REQUIRED")
        replay = self.post(
            f"/api/publications/{draft['id']}/restore",
            {"base_version": 2, "restore_version": 1},
            key="native-short-restore-01", status=(200,),
        )
        self.assertEqual(replay["version"], 3)
        self.assertEqual(self.request("get", f"/api/publications/{draft['id']}").json["data"]["content"]["body"], first_content["body"])

    def test_team_release_is_shared_but_native_artifact_and_draft_remain_author_only(self):
        content = {
            "title": "团队作品",
            "summary": "团队可阅读的已发布版。",
            "body": "发布版沿Publication的team授权可读。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "发布版沿Publication的team授权可读。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "team", "content": content,
        }, key="native-team-release-draft", status=(201,))
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": draft["version"]},
            key="native-team-release-publish", status=(201,),
        )
        self.assertEqual(release["visibility"], "team")

        _member_id, member_token = self.add_member()
        self.token = member_token
        team_read = self.request("get", f"/api/releases/{release['id']}")
        self.assertEqual(team_read.status_code, 200, team_read.get_data(as_text=True))
        self.assertEqual(team_read.json["data"]["content"]["body"], content["body"])

        publication_read = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(publication_read.status_code, 404)
        artifact_list = self.request("get", "/api/artifacts")
        self.assertEqual(artifact_list.status_code, 200)
        self.assertNotIn(draft["artifact_id"], {item["id"] for item in artifact_list.json["data"]})
        for method, path, kwargs in (
            ("get", f"/api/artifacts/{draft['artifact_id']}", {}),
            ("get", f"/api/artifacts/{draft['artifact_id']}?version=1", {}),
            ("get", f"/api/artifacts/{draft['artifact_id']}/comments", {}),
            ("post", f"/api/artifacts/{draft['artifact_id']}/versions", {"json": {"base_version": 1, "title": "旁路", "body": "旁路", "citations": []}}),
            ("post", f"/api/artifacts/{draft['artifact_id']}/restore", {"json": {"base_version": 1, "restore_version": 1}}),
            ("post", f"/api/artifacts/{draft['artifact_id']}/exports", {"json": {"version": 1, "format": "html"}}),
        ):
            denied = self.request(method, path, **kwargs)
            self.assertEqual(denied.status_code, 404, f"{path}: {denied.get_data(as_text=True)}")

    def test_simple_original_work_publishes_after_automatic_checks_without_human_review(self):
        content = {
            "title": "一则花园短文",
            "summary": "记录初开的花。",
            "body": "今天在花园里看见第一朵花。",
            "blocks": [{"block_id": "short", "kind": "paragraph", "text": "今天在花园里看见第一朵花。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": content,
        }, key="automatic-short-publication-draft", status=(201,))
        publication = self.store.row("SELECT * FROM publications WHERE id=?", (draft["id"],))
        with self.store.transaction(immediate=False) as connection:
            content_digest = _publication_content_hash(connection, publication, draft["content"])
            rights_digest = _publication_rights_digest(
                publication, draft["content"], connection=connection, identity=self.identity,
                media_members=[],
            )
        mismatched = self.request(
            "post", f"/api/publications/{draft['id']}/publish",
            json={"base_version": 1, "content_digest": "0" * 64},
            headers={"Idempotency-Key": "automatic-short-bad-digest"},
        )
        self.assertEqual(mismatched.status_code, 409)
        self.assertEqual(mismatched.json["error"]["code"], "OPERATION_DIGEST_MISMATCH")
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": draft["version"], "operation_id": "auto-short-operation-01",
             "content_digest": content_digest, "rights_digest": rights_digest, "visibility": "public"},
            key="automatic-short-publication-01", status=(201,),
        )
        self.assertEqual(release["review_mode"], "automatic")
        self.assertEqual(release["policy_version"], "publication-source-rights-privacy/1")
        self.assertEqual(release["operation"]["id"], "auto-short-operation-01")
        self.assertEqual(release["operation"]["status"], "completed")
        self.assertEqual(release["content"]["body"], content["body"])
        release_record = self.store.row("SELECT operation_id,intent_json,receipt_json FROM publication_releases WHERE id=?", (release["id"],))
        intent = json.loads(release_record["intent_json"])
        receipt = json.loads(release_record["receipt_json"])
        self.assertEqual(release_record["operation_id"], "auto-short-operation-01")
        self.assertEqual(intent["publication"], {"id": draft["id"], "base_version": 1})
        self.assertEqual(intent["content_digest"], content_digest)
        self.assertEqual(intent["rights_digest"], rights_digest)
        self.assertEqual(receipt["release_id"], release["id"])
        self.assertEqual(receipt["delivery"], "local_review_only")
        public_review = self.store.row("SELECT review_mode,policy_version FROM publication_reviews WHERE publication_id=?", (draft["id"],))
        self.assertEqual(public_review["review_mode"], "automatic")
        self.assertEqual(public_review["policy_version"], release["policy_version"])

        replay = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": draft["version"], "operation_id": "auto-short-operation-01",
             "content_digest": content_digest, "rights_digest": rights_digest, "visibility": "public"},
            key="automatic-short-publication-01", status=(200,),
        )
        self.assertEqual(replay["id"], release["id"])

        ordinary_work = self.post("/api/works", {"title": "普通创作工作", "goal": "为非客户工作校对来源范围"}, key="ordinary-artifact-work")
        ordinary_source = self.post(f"/api/works/{ordinary_work['id']}/business-results", {
            "operation": "service_response", "inputs": {
                "goal": "记录一项可直接发布的原创内容", "as_of": "2026-11-10T10:00:00+08:00",
                "coverage": [{"source": "作者输入", "status": "complete"}],
                "claims": [{"id": "ordinary-claim", "target": "花园", "text": "花园里有一朵初开的花", "status": "unverified", "severity": "material"}],
            }, "citations": [],
        }, key="ordinary-artifact-result")
        ordinary_draft = self.post("/api/publications", {
            "artifact_id": ordinary_source["id"], "artifact_version": ordinary_source["version"],
            "kind": "work", "visibility": "public", "content": {
                **content, "title": "既有工作中的普通原创短文", "body": "已核对来源用途后，可直接发布的原创短文。",
                "blocks": [{"block_id": "body", "kind": "paragraph", "text": "已核对来源用途后，可直接发布的原创短文。"}],
            },
        }, key="ordinary-artifact-publication-draft", status=(201,))
        ordinary_release = self.post(
            f"/api/publications/{ordinary_draft['id']}/publish",
            {"base_version": ordinary_draft["version"]},
            key="ordinary-artifact-publication-publish", status=(201,),
        )
        self.assertEqual(ordinary_release["review_mode"], "automatic")

        commission = self.post("/api/business/commissions", {"name": "客户委托", "scope": {}}, key="auto-review-commission", status=(201,))
        assigned = self.request("post", f"/api/business/commissions/{commission['id']}/works/{self.work['id']}", json={})
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
        sourced = self.make_result()
        sourced_draft = self.post("/api/publications", {
            "artifact_id": sourced["id"], "artifact_version": sourced["version"],
            "kind": "work", "visibility": "public", "content": {
                **content, "title": "基于已有成果的稿件", "body": "引用已有Artifact。",
                "blocks": [{"block_id": "body", "kind": "paragraph", "text": "引用已有Artifact。"}],
                "privacy_review": {"selected_content_only": True, "client_permission": True},
            },
        }, key="manual-review-required-draft", status=(201,))
        review_required = self.request(
            "post", f"/api/publications/{sourced_draft['id']}/publish",
            json={"base_version": sourced_draft["version"]},
            headers={"Idempotency-Key": "manual-review-required-publish"},
        )
        self.assertEqual(review_required.status_code, 202, review_required.get_data(as_text=True))
        queued_job = review_required.json["data"]
        self.assertEqual(queued_job["kind"], "community_publish")
        self.assertEqual(queued_job["status"], "queued")
        self.assertEqual(queued_job["publication_operation"]["state"], "review_required")
        self.assertIn("客户委托", queued_job["publication_operation"]["reason"])
        review_replay = self.request(
            "post", f"/api/publications/{sourced_draft['id']}/publish",
            json={"base_version": sourced_draft["version"]},
            headers={"Idempotency-Key": "manual-review-required-publish"},
        )
        self.assertEqual(review_replay.status_code, 202)
        self.assertEqual(review_replay.json["data"]["id"], queued_job["id"])
        self.assertIsNone(self.store.row("SELECT id FROM publication_releases WHERE publication_id=?", (sourced_draft["id"],)))

        self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
        waiting_job = self.request("get", f"/api/jobs/{queued_job['id']}").json["data"]
        self.assertEqual(waiting_job["status"], "waiting_for_input")
        submitted = self.store.row("SELECT status,version FROM publications WHERE id=?", (sourced_draft["id"],))
        self.assertEqual((submitted["status"], submitted["version"]), ("submitted", sourced_draft["version"] + 1))
        paused_job = self.request(
            "post", f"/api/jobs/{queued_job['id']}/pause",
            json={"operation_id": "pause-publication-pending-review", "base_control_revision": 0},
        )
        self.assertEqual(paused_job.status_code, 200, paused_job.get_data(as_text=True))
        self.assertEqual(paused_job.json["data"]["status"], "paused")

        moderator_id, moderator_token = self.add_member()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'reviewer',1)",
                (commission["id"], moderator_id),
            )
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator_id}
        self.token = moderator_token
        submissions = self.request("get", "/api/moderation/submissions")
        self.assertEqual(submissions.status_code, 200, submissions.get_data(as_text=True))
        selected = next(item for item in submissions.json["data"] if item["id"] == sourced_draft["id"])
        self.assertEqual(selected["publish_intent"]["state"], "pending")
        self.assertEqual(selected["publish_intent"]["operation_id"], queued_job["publication_operation"]["operation_id"])

        guest_id, guest_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {guest_id}
        self.token = guest_token
        restricted_queue = self.request("get", "/api/moderation/submissions")
        self.assertEqual(restricted_queue.status_code, 200)
        self.assertNotIn(sourced_draft["id"], {item["id"] for item in restricted_queue.json["data"]})
        unauthorized_review = self.request(
            "post", f"/api/publications/{sourced_draft['id']}/review",
            json={"base_version": submitted["version"], "decision": "approved", "reason": "未获委托范围审核权。"},
            headers={"Idempotency-Key": "unauthorized-client-review"},
        )
        self.assertEqual(unauthorized_review.status_code, 404)

        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator_id}
        self.token = moderator_token
        reviewed = self.request(
            "post", f"/api/publications/{sourced_draft['id']}/review",
            json={"base_version": submitted["version"], "decision": "approved", "reason": "已核对当前客户授权与选中范围。"},
            headers={"Idempotency-Key": "auto-review-required-human-approval"},
        )
        self.assertEqual(reviewed.status_code, 201, reviewed.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (queued_job["id"],))["status"], "paused")
        self.token = self.identity["token"]
        resumed_job = self.request(
            "post", f"/api/jobs/{queued_job['id']}/resume",
            json={"operation_id": "resume-approved-publication-intent", "base_control_revision": 1},
        )
        self.assertEqual(resumed_job.status_code, 200, resumed_job.get_data(as_text=True))
        self.assertEqual(resumed_job.json["data"]["status"], "queued")
        self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
        completed_job = self.request("get", f"/api/jobs/{queued_job['id']}")
        self.assertEqual(completed_job.status_code, 200)
        self.assertEqual(completed_job.json["data"]["status"], "completed")
        release_ref = next(item for item in completed_job.json["data"]["result_refs"] if item["kind"] == "release")
        release_row = self.store.row("SELECT * FROM publication_releases WHERE id=?", (release_ref["id"],))
        self.assertIsNotNone(release_row)
        self.assertEqual(json.loads(release_row["receipt_json"])["review_mode"], "human")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (sourced_draft["id"],))["count"], 1)
        completed_replay = self.request(
            "post", f"/api/publications/{sourced_draft['id']}/publish",
            json={"base_version": sourced_draft["version"]},
            headers={"Idempotency-Key": "manual-review-required-publish"},
        )
        self.assertEqual(completed_replay.status_code, 200, completed_replay.get_data(as_text=True))
        self.assertEqual(completed_replay.json["data"]["id"], release_ref["id"])

    def test_async_media_publication_pauses_cold_resumes_and_commits_one_release(self):
        material, _image = self.upload_still_image("publish-paused-image")
        content = {
            "title": "带图作品", "summary": "异步校验原图。", "body": "一张花园里的花。",
            "blocks": [
                {"block_id": "caption", "kind": "paragraph", "text": "一张花园里的花。"},
                {"block_id": "image", "kind": "image", "item_id": "garden", "alt_text": "花园里的一朵花", "decorative": False, "caption": ""},
            ],
        }
        draft = self.post("/api/publications", {
            "source_kind": "media_material", "source_id": material["id"], "source_version": 1,
            "media_selection": {"items": [{"item_id": "garden", "material_id": material["id"]}]},
            "kind": "work", "visibility": "private", "content": content,
        }, key="async-media-private-draft", status=(201,))
        public_content = {
            **draft["content"],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "dependencies": [{
                "id": material["id"], "version": 1, "sha256": material["sha256"],
                "included": True, "description": "作者上传的花园照片",
                "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                            "redistribute": True, "training": False, "attribution": "作者上传", "expires_at": None},
            }],
            "privacy_review": {"selected_content_only": True},
        }
        ready = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": draft["version"], "content": public_content, "visibility": "public"},
        )
        self.assertEqual(ready.status_code, 200, ready.get_data(as_text=True))

        prior_threshold = community_domain._COMMUNITY_PUBLISH_INLINE_VERIFY_BYTES
        prior_verify = community_media_domain._verify_member_source
        community_domain._COMMUNITY_PUBLISH_INLINE_VERIFY_BYTES = 1
        paused = {"done": False}

        def pause_during_verify(store, member, check_running=None):
            if not paused["done"]:
                paused["done"] = True
                from oevira.job_control import control_job

                control_job(
                    self.app, self.store, self.identity, job["id"], "pause",
                    {"operation_id": "pause-publish-mid-media", "base_control_revision": 0},
                )
            return prior_verify(store, member, check_running=check_running)

        try:
            queued = self.request(
                "post", f"/api/publications/{draft['id']}/publish",
                json={"base_version": ready.json["data"]["version"]},
                headers={"Idempotency-Key": "async-media-publication-intent"},
            )
            self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
            job = queued.json["data"]
            self.assertEqual(job["kind"], "community_publish")
            self.assertEqual(job["status"], "queued")
            community_media_domain._verify_member_source = pause_during_verify
            self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
            stopped = self.store.row("SELECT status,pause_requested FROM jobs WHERE id=?", (job["id"],))
            self.assertEqual((stopped["status"], stopped["pause_requested"]), ("paused", 1))
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (draft["id"],))["count"], 0)

            self.app = self.new_app()
            self.store = self.app.extensions["oevira.store"]
            self.client = self.app.test_client()
            self.token = self.identity["token"]
            resumed = self.request(
                "post", f"/api/jobs/{job['id']}/resume",
                json={"operation_id": "resume-publish-after-cold-open", "base_control_revision": 1},
            )
            self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
            self.assertEqual(resumed.json["data"]["status"], "queued")
            community_media_domain._verify_member_source = prior_verify
            self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
            completed = self.request("get", f"/api/jobs/{job['id']}")
            self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
            self.assertEqual(completed.json["data"]["status"], "completed")
            release_ref = next(item for item in completed.json["data"]["result_refs"] if item["kind"] == "release")
            release = self.request("get", f"/api/releases/{release_ref['id']}")
            self.assertEqual(release.status_code, 200, release.get_data(as_text=True))
            self.assertEqual(release.json["data"]["content"]["blocks"], ready.json["data"]["content"]["blocks"])
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (draft["id"],))["count"], 1)
            replay = self.request(
                "post", f"/api/publications/{draft['id']}/publish",
                json={"base_version": ready.json["data"]["version"]},
                headers={"Idempotency-Key": "async-media-publication-intent"},
            )
            self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
            self.assertEqual(replay.json["data"]["id"], release_ref["id"])

            cancelled_draft = self.post("/api/publications", {
                "source_kind": "media_material", "source_id": material["id"], "source_version": 1,
                "media_selection": {"items": [{"item_id": "garden", "material_id": material["id"]}]},
                "kind": "work", "visibility": "private", "content": content,
            }, key="cancelled-async-media-private-draft", status=(201,))
            cancel_ready_content = {
                **content,
                "license": public_content["license"],
                "dependencies": public_content["dependencies"],
                "privacy_review": public_content["privacy_review"],
            }
            cancel_ready = self.request(
                "patch", f"/api/publications/{cancelled_draft['id']}",
                json={"base_version": cancelled_draft["version"], "content": cancel_ready_content, "visibility": "public"},
            )
            self.assertEqual(cancel_ready.status_code, 200, cancel_ready.get_data(as_text=True))
            cancelled_publish = self.request(
                "post", f"/api/publications/{cancelled_draft['id']}/publish",
                json={"base_version": cancel_ready.json["data"]["version"]},
                headers={"Idempotency-Key": "cancelled-async-media-intent"},
            )
            self.assertEqual(cancelled_publish.status_code, 202, cancelled_publish.get_data(as_text=True))
            cancelled_job = cancelled_publish.json["data"]
            cancellation = {"done": False}

            def cancel_during_verify(store, member, check_running=None):
                if not cancellation["done"]:
                    cancellation["done"] = True
                    from oevira.job_control import control_job

                    control_job(
                        self.app, self.store, self.identity, cancelled_job["id"], "cancel",
                        {"operation_id": "cancel-publish-mid-media", "base_control_revision": 0},
                    )
                return prior_verify(store, member, check_running=check_running)

            community_media_domain._verify_member_source = cancel_during_verify
            self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
            cancelled_state = self.store.row("SELECT status FROM jobs WHERE id=?", (cancelled_job["id"],))["status"]
            self.assertEqual(cancelled_state, "cancelled")
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (cancelled_draft["id"],))["count"], 0)
            no_resume = self.request(
                "post", f"/api/jobs/{cancelled_job['id']}/resume",
                json={"operation_id": "reject-resume-cancelled-publication", "base_control_revision": 1},
            )
            self.assertEqual(no_resume.status_code, 409)
            self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (cancelled_job["id"],))["status"], "cancelled")
        finally:
            community_domain._COMMUNITY_PUBLISH_INLINE_VERIFY_BYTES = prior_threshold
            community_media_domain._verify_member_source = prior_verify

    def test_approved_publish_intent_cannot_resume_after_client_scope_changes(self):
        commission = self.post("/api/business/commissions", {"name": "授权范围测试", "scope": {"deliverable": "公开短文"}}, key="publish-scope-commission")
        assigned = self.request("post", f"/api/business/commissions/{commission['id']}/works/{self.work['id']}", json={})
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
        source = self.make_result()
        content = {
            "title": "委托成果", "summary": "仅发布获得批准的内容。", "body": "已按当前授权选中这一段。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "已按当前授权选中这一段。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True, "client_permission": True},
        }
        draft = self.post("/api/publications", {
            "artifact_id": source["id"], "artifact_version": source["version"],
            "kind": "work", "visibility": "public", "content": content,
        }, key="scope-change-publication-draft", status=(201,))
        queued = self.request(
            "post", f"/api/publications/{draft['id']}/publish", json={"base_version": draft["version"]},
            headers={"Idempotency-Key": "scope-change-publish-intent"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job = queued.json["data"]
        self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"], "waiting_for_input")
        submitted = self.store.row("SELECT version FROM publications WHERE id=?", (draft["id"],))["version"]
        paused = self.request(
            "post", f"/api/jobs/{job['id']}/pause",
            json={"operation_id": "pause-scope-change-review", "base_control_revision": 0},
        )
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))

        reviewer_id, reviewer_token = self.add_member()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'reviewer',1)",
                (commission["id"], reviewer_id),
            )
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {reviewer_id}
        self.token = reviewer_token
        decision = self.request(
            "post", f"/api/publications/{draft['id']}/review",
            json={"base_version": submitted, "decision": "approved", "reason": "当前内容符合当时记录的范围。"},
            headers={"Idempotency-Key": "approve-before-scope-change"},
        )
        self.assertEqual(decision.status_code, 201, decision.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"], "paused")

        self.token = self.identity["token"]
        scope_changed = self.request(
            "patch", f"/api/business/commissions/{commission['id']}",
            json={"base_version": commission["version"], "scope": {"deliverable": "不得公开传播"}},
        )
        self.assertEqual(scope_changed.status_code, 200, scope_changed.get_data(as_text=True))
        resume = self.request(
            "post", f"/api/jobs/{job['id']}/resume",
            json={"operation_id": "resume-after-scope-change", "base_control_revision": 1},
        )
        self.assertEqual(resume.status_code, 409, resume.get_data(as_text=True))
        self.assertEqual(resume.json["error"]["code"], "PUBLICATION_INPUT_CHANGED")
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"], "paused")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (draft["id"],))["count"], 0)

    def test_review_required_publish_job_ends_without_release_when_authorized_reviewer_rejects(self):
        commission = self.post("/api/business/commissions", {"name": "内容审核委托", "scope": {"publication_review": True}}, key="rejected-publish-commission")
        assigned = self.request("post", f"/api/business/commissions/{commission['id']}/works/{self.work['id']}", json={})
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
        source = self.make_result()
        draft = self.post("/api/publications", {
            "artifact_id": source["id"], "artifact_version": source["version"],
            "kind": "work", "visibility": "public", "content": {
                "title": "待审委托稿", "summary": "具体选中内容。", "body": "需要按客户意见修改。",
                "blocks": [{"block_id": "body", "kind": "paragraph", "text": "需要按客户意见修改。"}],
                "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                            "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
                "privacy_review": {"selected_content_only": True, "client_permission": True},
            },
        }, key="rejected-publish-draft", status=(201,))
        queued = self.request(
            "post", f"/api/publications/{draft['id']}/publish", json={"base_version": draft["version"]},
            headers={"Idempotency-Key": "rejected-publish-intent"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job = queued.json["data"]
        self.assertTrue(run_once(self.app, allowed_kinds={"community_publish"}))
        submitted = self.store.row("SELECT version FROM publications WHERE id=?", (draft["id"],))["version"]
        reviewer_id, reviewer_token = self.add_member()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'reviewer',1)",
                (commission["id"], reviewer_id),
            )
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {reviewer_id}
        self.token = reviewer_token
        rejected = self.request(
            "post", f"/api/publications/{draft['id']}/review",
            json={"base_version": submitted, "decision": "rejected", "reason": "此稿尚未获得本次内容确认。"},
            headers={"Idempotency-Key": "reject-publish-job-review"},
        )
        self.assertEqual(rejected.status_code, 201, rejected.get_data(as_text=True))
        job_row = self.store.row("SELECT status,error_code FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual((job_row["status"], job_row["error_code"]), ("failed", "PUBLICATION_REVIEW_REJECTED"))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=?", (draft["id"],))["count"], 0)

    def test_subject_references_resolve_exact_business_version_and_publicly_hide_internal_ids(self):
        created = self.request(
            "post", "/api/business/subjects",
            json={"kind": "brand", "name": "寰意关联测试品牌", "profile": {"aliases": ["关联品牌"]}},
            headers={"Idempotency-Key": "publication-subject-ref-brand"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        subject = created.json["data"]["subject"]
        content = {
            "title": "提及相关主体的短文", "summary": "作者选择了一个相关主体。", "body": "这是一段关于花园的原创短文。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "这是一段关于花园的原创短文。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
            "subject_refs": [{
                "ref_id": "brand-inspiration", "subject_id": subject["id"],
                "subject_version": subject["version"], "work_id": None,
                "relationship": "作者选定的创作对象关系",
            }],
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "private", "content": content,
        }, key="publication-subject-ref-draft", status=(201,))
        self.assertEqual(draft["content"]["subject_refs"][0]["subject_id"], subject["id"])
        release_content = {
            **draft["content"],
            "license": content["license"],
            "privacy_review": content["privacy_review"],
        }
        ready = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": draft["version"], "content": release_content, "visibility": "public"},
        )
        self.assertEqual(ready.status_code, 200, ready.get_data(as_text=True))
        release = self.post(
            f"/api/publications/{draft['id']}/publish", {"base_version": ready.json["data"]["version"]},
            key="publication-subject-ref-release", status=(201,),
        )
        public_ref = release["content"]["subject_refs"][0]
        self.assertEqual(public_ref["subject"], {"kind": "brand", "name": "寰意关联测试品牌"})
        self.assertEqual(public_ref["relationship_origin"], "author_selected_unverified")
        self.assertNotIn("subject_id", public_ref)
        self.assertNotIn("work_id", public_ref)
        saved_ref = self.store.row(
            "SELECT subject_id,subject_version,relationship FROM publication_release_subject_refs WHERE release_id=? AND ref_id='brand-inspiration'",
            (release["id"],),
        )
        self.assertEqual(saved_ref["subject_id"], subject["id"])
        self.assertEqual(saved_ref["subject_version"], 1)

        changed = self.request(
            "patch", f"/api/business/subjects/{subject['id']}",
            json={"base_version": subject["version"], "name": "改名后的品牌"},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        stale = self.request(
            "post", f"/api/publications/{draft['id']}/restore",
            json={"base_version": self.request("get", f"/api/publications/{draft['id']}").json["data"]["version"], "restore_version": 1},
            headers={"Idempotency-Key": "publication-subject-ref-stale-restore"},
        )
        self.assertEqual(stale.status_code, 409, stale.get_data(as_text=True))
        self.assertEqual(stale.json["error"]["code"], "SUBJECT_VERSION_CONFLICT")

    def test_exact_coauthor_acceptance_grants_scoped_read_and_release_attribution(self):
        content = {
            "title": "共同署名短文", "summary": "署名只在确切接受的版本有效。", "body": "作者与接受署名的共同作者完成这篇短文。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "作者与接受署名的共同作者完成这篇短文。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "署名作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": content,
        }, key="coauthor-native-public-draft", status=(201,))
        author_token = self.token
        coauthor_id, coauthor_token = self.add_member()
        invite = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors",
            json={"base_version": draft["version"], "user_id": coauthor_id},
            headers={"Idempotency-Key": "coauthor-exact-version-invite"},
        )
        self.assertEqual(invite.status_code, 201, invite.get_data(as_text=True))
        decliner_id, decliner_token = self.add_member()
        second_invite = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors",
            json={"base_version": draft["version"], "user_id": decliner_id},
            headers={"Idempotency-Key": "coauthor-explicit-decline-invite"},
        )
        self.assertEqual(second_invite.status_code, 201, second_invite.get_data(as_text=True))
        self.token = decliner_token
        declined = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors/{decliner_id}/decline",
            json={"base_version": draft["version"]},
            headers={"Idempotency-Key": "coauthor-explicit-decline"},
        )
        self.assertEqual(declined.status_code, 201, declined.get_data(as_text=True))
        self.assertEqual(declined.json["data"]["status"], "declined")
        self.token = coauthor_token
        invitation_list = self.request("get", "/api/publications/coauthor-invitations")
        self.assertEqual(invitation_list.status_code, 200)
        self.assertEqual(invitation_list.json["data"][0]["publication_id"], draft["id"])
        invitation_view = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(invitation_view.status_code, 200, invitation_view.get_data(as_text=True))
        self.assertNotIn("artifact_id", invitation_view.json["data"])
        self.assertEqual(invitation_view.json["data"]["content"]["body"], content["body"])
        private_artifact_before = self.request("get", f"/api/artifacts/{draft['artifact_id']}")
        self.assertEqual(private_artifact_before.status_code, 404)

        accepted = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors/{coauthor_id}/accept",
            json={"base_version": draft["version"]},
            headers={"Idempotency-Key": "coauthor-exact-version-accept"},
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))
        self.assertEqual(accepted.json["data"]["status"], "accepted")
        shared_artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}")
        self.assertEqual(shared_artifact.status_code, 200, shared_artifact.get_data(as_text=True))
        coauthor_read = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(coauthor_read.status_code, 200)
        self.assertEqual(coauthor_read.json["data"]["coauthors"][0]["user_id"], coauthor_id)
        accepted_history = self.request("get", f"/api/publications/{draft['id']}?version={draft['version']}")
        self.assertEqual(accepted_history.status_code, 200, accepted_history.get_data(as_text=True))
        self.assertEqual(accepted_history.json["data"]["coauthors"][0]["user_id"], coauthor_id)
        forbidden_edit = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": draft["version"], "content": content},
        )
        self.assertEqual(forbidden_edit.status_code, 404)

        self.token = author_token
        revised_content = {
            **content,
            "body": "作者与接受署名的共同作者完成了修订后的短文。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "作者与接受署名的共同作者完成了修订后的短文。"}],
        }
        revised = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": draft["version"], "content": revised_content},
        )
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        self.assertEqual(revised.json["data"]["coauthors"], [])
        self.token = coauthor_token
        stale_access = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(stale_access.status_code, 404)
        stale_artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}")
        self.assertEqual(stale_artifact.status_code, 404)
        self.token = author_token
        reinvite = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors",
            json={"base_version": revised.json["data"]["version"], "user_id": coauthor_id},
            headers={"Idempotency-Key": "coauthor-revision-reinvite"},
        )
        self.assertEqual(reinvite.status_code, 201, reinvite.get_data(as_text=True))
        self.token = coauthor_token
        reaccepted = self.request(
            "post", f"/api/publications/{draft['id']}/coauthors/{coauthor_id}/accept",
            json={"base_version": revised.json["data"]["version"]},
            headers={"Idempotency-Key": "coauthor-revision-reaccept"},
        )
        self.assertEqual(reaccepted.status_code, 201, reaccepted.get_data(as_text=True))
        self.token = author_token
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": revised.json["data"]["version"]},
            key="coauthor-exact-version-publish", status=(201,),
        )
        self.assertEqual(release["creator"]["user_id"], self.identity["user"]["id"])
        self.assertEqual(release["coauthors"], [{"display_name": "另一位客户成员"}])
        profile = self.request("get", f"/api/creators/{coauthor_id}")
        self.assertEqual(profile.status_code, 200, profile.get_data(as_text=True))
        self.assertIn(release["id"], {item["id"] for item in profile.json["data"]["releases"]})
        discovery = self.request("get", f"/api/discovery?creator={coauthor_id}")
        self.assertEqual(discovery.status_code, 200, discovery.get_data(as_text=True))
        self.assertIn(release["id"], {item["id"] for item in discovery.json["data"]["items"]})
        credited = self.store.row("SELECT user_id,accepted_version FROM publication_release_coauthors WHERE release_id=?", (release["id"],))
        self.assertEqual((credited["user_id"], credited["accepted_version"]), (coauthor_id, revised.json["data"]["version"]))
        publication_now = self.request("get", f"/api/publications/{draft['id']}").json["data"]
        revoked = self.request(
            "delete", f"/api/publications/{draft['id']}/coauthors/{coauthor_id}",
            json={"base_version": publication_now["version"]},
            headers={"Idempotency-Key": "coauthor-revoke-future-attribution"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        self.assertEqual(self.request("get", f"/api/publications/{draft['id']}").json["data"]["coauthors"], [])
        self.token = coauthor_token
        revoked_private_source = self.request("get", f"/api/artifacts/{draft['artifact_id']}")
        self.assertEqual(revoked_private_source.status_code, 404)
        retained_release = self.request("get", f"/api/releases/{release['id']}")
        self.assertEqual(retained_release.status_code, 200, retained_release.get_data(as_text=True))
        self.assertEqual(retained_release.json["data"]["coauthors"], [{"display_name": "另一位客户成员"}])

    def test_release_operation_receipt_columns_upgrade_without_losing_release(self):
        content = {
            "title": "迁移检查短文", "summary": "发布回执保全。", "body": "发布后保存操作回执。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "发布后保存操作回执。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": content,
        }, key="publication-receipt-upgrade-draft", status=(201,))
        release = self.post(
            f"/api/publications/{draft['id']}/publish", {"base_version": 1},
            key="publication-receipt-upgrade-publish", status=(201,),
        )
        before = self.store.row("SELECT id,operation_id,intent_json,receipt_json,authors_json,content_hash FROM publication_releases WHERE id=?", (release["id"],))
        self.assertTrue(before["operation_id"])
        self.assertEqual(json.loads(before["intent_json"])["action"], "publication.publish")
        self.assertEqual(json.loads(before["receipt_json"])["release_id"], release["id"])
        with self.store.transaction() as connection:
            connection.execute("DROP INDEX publication_release_operation_id")
            for column in ("authors_json", "receipt_json", "intent_json", "operation_id"):
                connection.execute(f"ALTER TABLE publication_releases DROP COLUMN {column}")
        install_community_schema(self.store)
        after = self.store.row("SELECT id,operation_id,intent_json,receipt_json,authors_json,content_hash FROM publication_releases WHERE id=?", (release["id"],))
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertIsNone(after["operation_id"])
        self.assertEqual(json.loads(after["intent_json"]), {})
        self.assertEqual(json.loads(after["receipt_json"]), {})
        self.assertEqual(json.loads(after["authors_json"]), {})

    def test_restoring_published_native_work_creates_new_draft_without_changing_release(self):
        first_content = {
            "title": "初稿", "summary": "第一次公开的摘要。", "body": "第一次公开的正文。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "第一次公开的正文。"}],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": True, "training": False, "attribution": "作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
            "tags": ["创作"],
        }
        draft = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": first_content,
        }, key="native-publication-release-01", status=(201,))
        submitted = self.post(
            f"/api/publications/{draft['id']}/submit",
            {"base_version": draft["version"], "preview_acknowledged": True},
        )
        moderator_id, moderator_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator_id}
        author_token = self.token
        self.token = moderator_token
        reviewed = self.post(
            f"/api/publications/{draft['id']}/review",
            {"base_version": submitted["version"], "decision": "approved", "reason": "已核对所选正文和发布范围"},
        )
        self.token = author_token
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": reviewed["version"], "review_id": reviewed["id"]},
            key="native-publication-release-commit",
        )
        original_release = self.request("get", f"/api/releases/{release['id']}").json["data"]
        self.assertEqual(original_release["content"]["body"], first_content["body"])
        published_publication = self.request("get", f"/api/publications/{draft['id']}").json["data"]

        changed_content = {
            **first_content,
            "title": "后来修订的草稿",
            "summary": "供下一次发布使用。",
            "body": "后来修订的草稿正文。",
            "blocks": [{"block_id": "body", "kind": "paragraph", "text": "后来修订的草稿正文。"}],
        }
        changed = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": published_publication["version"], "content": changed_content},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        self.assertEqual(changed.json["data"]["status"], "draft")
        self.assertEqual(changed.json["data"]["artifact_version"], 2)

        restored = self.post(
            f"/api/publications/{draft['id']}/restore",
            {"base_version": changed.json["data"]["version"], "restore_version": draft["version"]},
            key="native-publication-restore-after-release",
            status=(200,),
        )
        self.assertEqual(restored["status"], "draft")
        self.assertEqual(restored["content"]["body"], first_content["body"])
        self.assertEqual(restored["artifact_version"], 3)
        current_release = self.request("get", f"/api/releases/{release['id']}").json["data"]
        self.assertEqual(current_release["content"]["body"], first_content["body"])
        self.assertEqual(current_release["content_hash"], original_release["content_hash"])

    def test_native_private_experience_uses_the_same_artifact_revision_and_restore_owner(self):
        experience = {
            "problem": "活动讨论容易停留在互动数量。",
            "mechanism": "把原话与可核结果分开记录。",
            "steps": ["先保留原始问题", "再记录行动与观察窗口"],
            "conditions": ["适用于有实际后续观察的活动"],
            "counterexamples": ["单次活动不应据此推断长期效果"],
            "results": [],
            "limitations": "当前没有独立验证结果。",
            "evidence": [],
            "claim_type": "unknown",
        }
        content = {
            "title": "活动复盘经验",
            "summary": "保留问题、方法和局限。",
            "body": "先分开记录问题、行动、观察和未知。",
            "experience": experience,
        }
        saved_experience = {
            **experience,
            "independent_validation": "not_established_by_publication",
            "training_eligibility": "not_granted",
        }
        draft = self.post("/api/publications", {
            "kind": "experience", "visibility": "private", "content": content,
        }, key="native-experience-private-01", status=(201,))
        artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}").json["data"]
        self.assertEqual(artifact["input_snapshot"]["kind"], "community_native")
        self.assertEqual(artifact["input_snapshot"]["community_native"]["experience"], saved_experience)

        updated_experience = {**experience, "mechanism": "先分开记录作者主张、实际观察与未知。"}
        changed = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": 1, "content": {**content, "experience": updated_experience}},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        self.assertEqual(changed.json["data"]["artifact_version"], 2)
        self.assertEqual(changed.json["data"]["content"]["experience"]["mechanism"], updated_experience["mechanism"])

        restored = self.post(
            f"/api/publications/{draft['id']}/restore",
            {"base_version": 2, "restore_version": 1},
            key="native-experience-restore-01", status=(200,),
        )
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["content"]["experience"], saved_experience)
        restored_artifact = self.request("get", f"/api/artifacts/{draft['artifact_id']}").json["data"]
        self.assertEqual(restored_artifact["version"], 3)
        self.assertEqual(restored_artifact["input_snapshot"]["community_native"]["experience"], saved_experience)

        public_content = {
            **content,
            "experience": saved_experience,
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "经验作者", "expires_at": None},
            "privacy_review": {"selected_content_only": True},
        }
        ready = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": 3, "content": public_content, "visibility": "public"},
        )
        self.assertEqual(ready.status_code, 200, ready.get_data(as_text=True))
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": ready.json["data"]["version"]},
            key="native-experience-public-release", status=(201,),
        )
        self.assertEqual(release["kind"], "experience")
        self.assertEqual(release["review_mode"], "automatic")
        self.assertEqual(release["content"]["experience"]["independent_validation"], "not_established_by_publication")
        self.assertEqual(release["content"]["experience"]["training_eligibility"], "not_granted")

    def test_series_versions_pin_or_follow_public_releases_without_leaking_unavailable_members(self):
        public_license = {
            "view": True, "download": False, "adapt": False, "commercial": False,
            "redistribute": False, "training": False, "attribution": "作者", "expires_at": None,
        }
        review_member, moderator_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {review_member}
        author_token = self.token

        def publish(publication_id: str, base_version: int, key_prefix: str):
            submitted = self.post(
                f"/api/publications/{publication_id}/submit",
                {"base_version": base_version, "preview_acknowledged": True},
            )
            self.token = moderator_token
            reviewed = self.post(
                f"/api/publications/{publication_id}/review",
                {"base_version": submitted["version"], "decision": "approved", "reason": "已检查确切作品版"},
                key=f"{key_prefix}-review",
            )
            self.token = author_token
            return self.post(
                f"/api/publications/{publication_id}/publish",
                {"base_version": reviewed["version"], "review_id": reviewed["id"]},
                key=f"{key_prefix}-publish",
            )

        child_content = {
            "title": "观察一", "summary": "第一版观察。", "body": "观察到第一种情况。",
            "blocks": [{"block_id": "child-body", "kind": "paragraph", "text": "观察到第一种情况。"}],
            "license": public_license, "privacy_review": {"selected_content_only": True},
        }
        child = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": child_content,
        }, key="series-child-create", status=(201,))
        pinned_release = publish(child["id"], child["version"], "series-child-v1")

        series_content = {
            "title": "观察系列", "summary": "固定一篇并跟随另一篇的最新公开版。",
            "body": "", "blocks": [], "license": public_license,
            "privacy_review": {"selected_content_only": True},
            "series": {"members": [
                {"mode": "pinned_release", "release_id": pinned_release["id"]},
                {"mode": "follow_current_public", "publication_id": child["id"]},
            ]},
        }
        series = self.post("/api/publications", {
            "kind": "work", "visibility": "public", "content": series_content,
        }, key="series-create", status=(201,))
        series_id = series["content"]["series"]["series_id"]
        original_members = series["content"]["series"]["members"]
        self.assertEqual([item["mode"] for item in original_members], ["pinned_release", "follow_current_public"])

        reordered_content = {
            **series_content,
            "series": {"series_id": series_id, "members": list(reversed(original_members))},
        }
        reordered = self.request(
            "patch", f"/api/publications/{series['id']}",
            json={"base_version": 1, "content": reordered_content},
        )
        self.assertEqual(reordered.status_code, 200, reordered.get_data(as_text=True))
        self.assertEqual(reordered.json["data"]["content"]["series"]["series_id"], series_id)
        reordered_members = reordered.json["data"]["content"]["series"]["members"]
        self.assertEqual([item["member_id"] for item in reordered_members], [item["member_id"] for item in reversed(original_members)])
        historical_series = self.request("get", f"/api/publications/{series['id']}?version=1")
        self.assertEqual(historical_series.status_code, 200)
        self.assertEqual(historical_series.json["data"]["content"]["series"]["series_id"], series_id)
        self.assertEqual([item["member_id"] for item in historical_series.json["data"]["content"]["series"]["members"]], [item["member_id"] for item in original_members])

        series_release = self.post(
            f"/api/publications/{series['id']}/publish",
            {"base_version": reordered.json["data"]["version"]},
            key="series-publish-v1", status=(201,),
        )
        self.assertEqual(series_release["review_mode"], "automatic")
        series_view = self.request("get", f"/api/releases/{series_release['id']}").json["data"]
        public_members = series_view["content"]["series"]["members"]
        self.assertEqual([item["resolved_release_id"] for item in public_members], [pinned_release["id"], pinned_release["id"]])
        self.assertTrue(all(item["availability"] == "available" for item in public_members))
        self.assertNotIn("publication_id", json.dumps(series_view["content"]["series"], ensure_ascii=False))

        current_child = self.request("get", f"/api/publications/{child['id']}").json["data"]
        next_content = {
            **child_content,
            "title": "观察二", "summary": "新的公开观察。", "body": "观察到第二种情况。",
            "blocks": [{"block_id": "child-body", "kind": "paragraph", "text": "观察到第二种情况。"}],
        }
        next_draft = self.request(
            "patch", f"/api/publications/{child['id']}",
            json={"base_version": current_child["version"], "content": next_content},
        )
        self.assertEqual(next_draft.status_code, 200, next_draft.get_data(as_text=True))
        next_release = publish(child["id"], next_draft.json["data"]["version"], "series-child-v2")
        self.assertEqual(self.request("get", f"/api/releases/{pinned_release['id']}").status_code, 200)

        refreshed_series = self.request("get", f"/api/releases/{series_release['id']}").json["data"]
        refreshed_members = refreshed_series["content"]["series"]["members"]
        self.assertEqual(refreshed_members[0]["mode"], "follow_current_public")
        self.assertEqual(refreshed_members[0]["resolved_release_id"], next_release["id"])
        self.assertEqual(refreshed_members[1]["mode"], "pinned_release")
        self.assertEqual(refreshed_members[1]["resolved_release_id"], pinned_release["id"])
        next_release_row = self.store.row("SELECT publication_version,content_hash FROM publication_releases WHERE id=?", (next_release["id"],))
        self.assertEqual(refreshed_members[0]["observed_publication_version"], next_release_row["publication_version"])
        self.assertEqual(refreshed_members[0]["observed_content_hash"], next_release_row["content_hash"])

        current_child = self.request("get", f"/api/publications/{child['id']}").json["data"]
        private_content = {
            **next_content,
            "title": "作者私稿",
            "summary": "最新版本已转私有。",
            "body": "这份内容不应通过系列关系泄露。",
            "blocks": [{"block_id": "child-body", "kind": "paragraph", "text": "这份内容不应通过系列关系泄露。"}],
        }
        private_draft = self.request(
            "patch", f"/api/publications/{child['id']}",
            json={"base_version": current_child["version"], "content": private_content, "visibility": "private"},
        )
        self.assertEqual(private_draft.status_code, 200, private_draft.get_data(as_text=True))
        private_release = publish(child["id"], private_draft.json["data"]["version"], "series-child-private-v3")
        private_follow = self.request("post", "/api/publications", json={
            "kind": "work", "visibility": "public",
            "content": {
                "title": "不可引用私稿", "summary": "不应透出私有成员。", "body": "", "blocks": [],
                "license": public_license, "privacy_review": {"selected_content_only": True},
                "series": {"members": [{"mode": "follow_current_public", "publication_id": child["id"]}]},
            },
        })
        self.assertEqual(private_follow.status_code, 409)
        self.assertEqual(private_follow.json["error"]["code"], "SERIES_MEMBER_UNAVAILABLE")
        self.assertNotIn(child["id"], private_follow.get_data(as_text=True))
        hidden_current = self.request("get", f"/api/releases/{series_release['id']}").json["data"]["content"]["series"]["members"]
        self.assertEqual(hidden_current[0]["availability"], "unavailable")
        self.assertNotIn("resolved_release_id", hidden_current[0])
        self.assertNotIn(private_release["id"], json.dumps(hidden_current, ensure_ascii=False))
        self.assertEqual(hidden_current[1]["resolved_release_id"], pinned_release["id"])

        withdrawn_private = self.request(
            "post", f"/api/publications/{child['id']}/withdraw", json={"release_id": private_release["id"]},
        )
        self.assertEqual(withdrawn_private.status_code, 200, withdrawn_private.get_data(as_text=True))
        no_current_release = self.request("get", f"/api/releases/{series_release['id']}").json["data"]["content"]["series"]["members"]
        self.assertEqual(no_current_release[0]["availability"], "unavailable")
        self.assertNotIn("resolved_release_id", no_current_release[0])

        withdrawn = self.request(
            "post", f"/api/publications/{child['id']}/withdraw", json={"release_id": pinned_release["id"]},
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.get_data(as_text=True))
        self.assertEqual(self.request("get", f"/api/releases/{pinned_release['id']}").status_code, 404)
        still_current = self.request("get", f"/api/releases/{series_release['id']}").json["data"]["content"]["series"]["members"]
        self.assertEqual(still_current[0]["availability"], "unavailable")
        self.assertNotIn("resolved_release_id", still_current[0])
        self.assertEqual(still_current[1]["availability"], "unavailable")
        self.assertNotIn("resolved_release_id", still_current[1])
        self.request("post", f"/api/publications/{child['id']}/withdraw", json={"release_id": next_release["id"]})
        no_current_release = self.request("get", f"/api/releases/{series_release['id']}").json["data"]["content"]["series"]["members"]
        self.assertTrue(all(item["availability"] == "unavailable" for item in no_current_release))
        self.assertTrue(all("resolved_release_id" not in item for item in no_current_release))

    def test_ordered_multi_image_private_draft_retains_members_across_edit_reopen_and_restore(self):
        first, first_bytes = self.upload_still_image("第一张")
        second, second_bytes = self.upload_still_image("第二张")
        original_file = self.request("get", f"/api/materials/{first['id']}/file")
        self.assertEqual(original_file.data, first_bytes)
        original_file.close()
        original_blocks = [
            {"block_id": "lead", "kind": "paragraph", "text": "花园里的两张照片。"},
            {"block_id": "image-a", "kind": "image", "item_id": "garden-a", "alt_text": "第一朵花", "decorative": False, "caption": "上午的花。"},
            {"block_id": "image-b", "kind": "image", "item_id": "garden-b", "alt_text": "第二朵花", "decorative": False, "caption": "午后的花。"},
        ]
        original_content = {
            "title": "春日记录", "summary": "两张花园照片。",
            "body": "花园里的两张照片。\n\n上午的花。\n\n午后的花。",
            "blocks": original_blocks,
        }
        draft = self.post("/api/publications", {
            "source_kind": "media_material", "source_id": first["id"], "source_version": 1,
            "media_selection": {"items": [
                {"item_id": "garden-a", "material_id": first["id"]},
                {"item_id": "garden-b", "material_id": second["id"]},
            ]},
            "kind": "work", "visibility": "private", "content": original_content,
        }, key="native-multiphoto-private-01", status=(201,))
        self.assertEqual([item["item_id"] for item in draft["content"]["blocks"] if item["kind"] == "image"], ["garden-a", "garden-b"])
        initial_members = self.store.rows(
            "SELECT member_id,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=1 AND role='rendition' ORDER BY member_id",
            (draft["id"],),
        )
        initial_ids = {json.loads(item["metadata_json"])["item_id"]: item["member_id"] for item in initial_members}
        self.assertEqual(set(initial_ids), {"garden-a", "garden-b"})

        revised_blocks = [original_blocks[0], original_blocks[2], original_blocks[1]]
        revised_content = {
            **original_content,
            "body": "花园里的两张照片。\n\n午后的花。\n\n上午的花。",
            "blocks": revised_blocks,
        }
        revised = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": 1, "content": revised_content},
        )
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        self.assertEqual([item["item_id"] for item in revised.json["data"]["content"]["blocks"] if item["kind"] == "image"], ["garden-b", "garden-a"])
        revision_members = self.store.rows(
            "SELECT member_id,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=2 AND role='rendition' ORDER BY member_id",
            (draft["id"],),
        )
        revised_ids = {json.loads(item["metadata_json"])["item_id"]: item["member_id"] for item in revision_members}
        self.assertEqual(revised_ids, initial_ids)
        self.assertEqual({item["metadata"]["item_id"]: item["metadata"]["position"] for item in revised.json["data"]["content"]["media"]["images"]}, {"garden-b": 0, "garden-a": 1})

        self.app = self.new_app()
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        reopened = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json["data"]["content"]["blocks"], revised_blocks)
        self.assertEqual({item["metadata"]["item_id"]: item["metadata"]["position"] for item in reopened.json["data"]["content"]["media"]["images"]}, {"garden-b": 0, "garden-a": 1})

        restored = self.post(
            f"/api/publications/{draft['id']}/restore",
            {"base_version": 2, "restore_version": 1},
            key="native-multiphoto-restore-01", status=(200,),
        )
        self.assertEqual(restored["version"], 3)
        self.assertEqual([item["item_id"] for item in restored["content"]["blocks"] if item["kind"] == "image"], ["garden-a", "garden-b"])
        restored_members = self.store.rows(
            "SELECT member_id,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=3 AND role='rendition' ORDER BY member_id",
            (draft["id"],),
        )
        restored_ids = {json.loads(item["metadata_json"])["item_id"]: item["member_id"] for item in restored_members}
        self.assertEqual(restored_ids, initial_ids)
        author_token = self.token
        _member_id, other_member_token = self.add_member()
        self.token = other_member_token
        hidden = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(hidden.status_code, 404)
        self.token = author_token

        dependencies = []
        for material, description in ((first, "第一张作者上传的照片"), (second, "第二张作者上传的照片")):
            dependencies.append({
                "id": material["id"], "version": 1, "sha256": material["sha256"],
                "included": True, "description": description,
                "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                            "redistribute": True, "training": False, "attribution": "作者上传", "expires_at": None},
            })
        public_content = {
            **restored["content"],
            "license": {"view": True, "download": False, "adapt": False, "commercial": False,
                        "redistribute": False, "training": False, "attribution": "作者", "expires_at": None},
            "dependencies": dependencies,
            "privacy_review": {"selected_content_only": True},
        }
        rights_ready = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": restored["version"], "content": public_content, "visibility": "public"},
        )
        self.assertEqual(rights_ready.status_code, 200, rights_ready.get_data(as_text=True))
        self.assertEqual(rights_ready.json["data"]["content"]["media"]["rights_state"], "author_attested_source_permissions")
        published = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": rights_ready.json["data"]["version"]},
            key="native-multiphoto-automatic-publish", status=(201,),
        )
        self.assertEqual(published["review_mode"], "automatic")
        self.assertEqual(len(published["content"]["media"]["images"]), 2)


    def upload_audio_material(self):
        memory = io.BytesIO()
        sample_rate = 8000
        frames = [int(7000 * math.sin(2 * math.pi * 440 * index / sample_rate)) for index in range(sample_rate // 4)]
        with wave.open(memory, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(struct.pack("<" + "h" * len(frames), *frames))
        content = memory.getvalue()
        upload = self.request(
            "post", "/api/uploads",
            json={
                "kind": "media", "title": "原始播客录音", "filename": "podcast.wav",
                "expected_size": len(content), "expected_sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        upload_id = upload.json["data"]["id"]
        chunk = self.request(
            "patch", f"/api/uploads/{upload_id}", data=content,
            content_type="application/octet-stream",
            headers={"Upload-Offset": "0", "X-Chunk-SHA256": hashlib.sha256(content).hexdigest()},
        )
        self.assertEqual(chunk.status_code, 204, chunk.get_data(as_text=True))
        completed = self.request("post", f"/api/uploads/{upload_id}/complete")
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        return completed.json["data"]["material"], content

    def publish_template(self):
        artifact = self.make_result()
        draft = self.post("/api/publications", {"artifact_id": artifact["id"], "artifact_version": artifact["version"], "kind": "template", "visibility": "public", "content": self.public_content()})
        self.assertEqual(self.request("get", "/api/discovery").json["data"]["items"], [])
        submitted = self.post(f"/api/publications/{draft['id']}/submit", {"base_version": 1, "preview_acknowledged": True})
        moderator, token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator}
        author = self.token
        self.token = token
        queue = self.request("get", "/api/moderation/submissions").json["data"]
        self.assertEqual(len(queue), 1)
        self.assertNotIn("artifact_id", queue[0])
        review_payload = {"base_version": submitted["version"], "decision": "approved", "reason": "已核对选中内容与用途许可"}
        review = self.post(f"/api/publications/{draft['id']}/review", review_payload, key="review-independent-template")
        replay = self.post(f"/api/publications/{draft['id']}/review", review_payload, key="review-independent-template")
        self.assertEqual(replay["id"], review["id"])
        self.assertEqual(replay["version"], review["version"])
        conflict = self.request(
            "post", f"/api/publications/{draft['id']}/review",
            json={**review_payload, "reason": "不同审核意见"},
            headers={"Idempotency-Key": "review-independent-template"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.token = author
        self.assertEqual(self.request("get", "/api/discovery").json["data"]["items"], [])
        release = self.post(f"/api/publications/{draft['id']}/publish", {"base_version": review["version"], "review_id": review["id"]}, key="publish-one")
        return draft, release

    def test_publication_copy_modify_export_withdraw_preserves_private_copy(self):
        draft, release = self.publish_template()
        self.assertEqual(release["delivery"], "local_review_only")
        self.assertFalse(release["official_creator"])
        self.assertNotIn("input_snapshot", release["content"])
        self.assertNotIn("artifact_id", release)
        self.assertFalse(release["content"]["license"]["training"])
        visible = self.request("get", "/api/discovery?q=复盘").json["data"]["items"]
        self.assertEqual(len(visible), 1)
        copy = self.post(f"/api/templates/{release['id']}/instantiate", {"work_id": self.work["id"], "purpose": "commercial", "parameters": {"brand": "青屿"}}, key="copy-one")
        self.assertIn("青屿活动复盘", copy["body"])
        changed = self.post(f"/api/artifacts/{copy['id']}/versions", {"base_version": 1, "title": copy["title"], "body": copy["body"] + "\n\n我的客户修改。", "citations": []})
        source = self.request("get", f"/api/releases/{release['id']}").json["data"]
        self.assertNotIn("我的客户修改", source["content"]["body"])
        self.post(f"/api/publications/{draft['id']}/withdraw", {})
        self.assertEqual(self.request("get", "/api/discovery").json["data"]["items"], [])
        self.assertEqual(self.request("get", f"/api/releases/{release['id']}").status_code, 404)
        self.assertEqual(self.request("get", f"/api/artifacts/{copy['id']}").json["data"]["body"], changed["body"])
        replay = self.post(f"/api/templates/{release['id']}/instantiate", {"work_id": self.work["id"], "purpose": "commercial", "parameters": {"brand": "青屿"}}, key="copy-one")
        self.assertEqual(replay["id"], copy["id"])
        self.assertEqual(replay["version"], 1)
        self.assertEqual(self.request("post", f"/api/templates/{release['id']}/instantiate", json={"work_id": self.work["id"], "purpose": "commercial", "parameters": {"brand": "另一个"}}).status_code, 404)

    def test_unreviewed_publication_and_spoofed_official_cannot_publish(self):
        artifact = self.make_result()
        payload = {"artifact_id": artifact["id"], "artifact_version": 1, "kind": "template", "visibility": "public", "content": self.public_content()}
        invalid = self.request("post", "/api/publications", json={**payload, "official": True})
        self.assertEqual(invalid.status_code, 400)
        draft = self.post("/api/publications", payload)
        invalid = self.request("post", f"/api/publications/{draft['id']}/publish", json={"base_version": 1, "review_id": "invented"})
        self.assertEqual(invalid.status_code, 409)
        self.post(f"/api/publications/{draft['id']}/submit", {"base_version": 1, "preview_acknowledged": True})
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {self.identity["user"]["id"]}
        invalid = self.request("post", f"/api/publications/{draft['id']}/review", json={"base_version": 2, "decision": "approved", "reason": "自我批准"})
        self.assertEqual(invalid.status_code, 403)

    def test_uploaded_podcast_source_publishes_with_exact_license_and_http_range_playback(self):
        material, original = self.upload_audio_material()
        content = {
            "title": "街区声音记录",
            "summary": "一段由作者上传的播客录音。",
            "body": "",
            "tags": ["播客", "声音"],
            "license": {
                "view": True, "download": False, "adapt": False,
                "commercial": False, "redistribute": False, "training": False,
                "attribution": "请注明原作者", "expires_at": None,
            },
            "privacy_review": {"selected_content_only": True},
            "dependencies": [{
                "id": material["id"], "version": 1, "sha256": material["sha256"],
                "license": {
                    "view": True, "download": False, "adapt": False,
                    "commercial": False, "redistribute": True, "training": False,
                    "attribution": "作者上传的原始录音", "expires_at": None,
                },
                "included": True,
                "description": "作者上传的播客原始音频",
            }],
        }
        draft = self.post("/api/publications", {
            "source_kind": "media_material", "source_id": material["id"],
            "source_version": 1, "media_selection": {}, "kind": "work",
            "visibility": "public", "content": content,
        })
        self.assertEqual(draft["source"], {"kind": "media_material", "id": material["id"], "version": 1})
        self.assertEqual(draft["content"]["media"]["kind"], "audio")
        self.assertEqual(len(draft["content"]["media"]["renditions"]), 1)
        self.assertNotIn(material["id"], draft["content"]["media"]["renditions"][0]["filename"])

        submitted = self.post(f"/api/publications/{draft['id']}/submit", {"base_version": 1, "preview_acknowledged": True})
        moderator, moderator_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator}
        author_token = self.token
        self.token = moderator_token
        queue = self.request("get", "/api/moderation/submissions").json["data"]
        self.assertEqual(len(queue), 1)
        self.assertEqual(len(queue[0]["media_members"]), 1)
        self.assertNotIn(material["id"], json.dumps(queue[0]))
        reviewed = self.post(
            f"/api/publications/{draft['id']}/review",
            {"base_version": submitted["version"], "decision": "approved", "reason": "已按所选录音和分发许可核对"},
        )
        self.token = author_token
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": reviewed["version"], "review_id": reviewed["id"]},
            key="publish-audio-source",
        )
        self.assertEqual(release["delivery"], "local_review_only")
        public_json = json.dumps(release, ensure_ascii=False)
        self.assertNotIn(material["id"], public_json)
        member_id = release["content"]["media"]["renditions"][0]["id"]
        anon = self.app.test_client()
        grant = anon.post(
            f"/api/releases/{release['id']}/media/{member_id}/playback",
            json={"purpose": "view"},
            base_url=business_test.BASE,
        )
        self.assertEqual(grant.status_code, 200, grant.get_data(as_text=True))
        stream_path = grant.json["data"]["stream_url"]
        ranged = anon.get(stream_path, headers={"Range": "bytes=0-63"}, base_url=business_test.BASE)
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.data, original[:64])
        self.assertEqual(ranged.headers["Accept-Ranges"], "bytes")
        self.assertIn("bytes 0-63/", ranged.headers["Content-Range"])
        self.assertEqual(ranged.headers["Cache-Control"], "private, no-store")
        ranged.close()
        saved_queue = self.post(
            f"/api/releases/{release['id']}/items",
            {"kind": "queue", "progress_seconds": 0.1},
        )
        self.assertEqual(saved_queue["progress_seconds"], 0.1)
        resumed_items = self.request("get", "/api/community/items").json["data"]
        saved_item = next(item for item in resumed_items if item["kind"] == "queue")
        self.assertEqual(saved_item["progress_seconds"], 0.1)
        self.assertEqual(saved_item["release"]["id"], release["id"])

        self.post(f"/api/publications/{draft['id']}/withdraw", {})
        revoked = anon.get(stream_path, base_url=business_test.BASE)
        self.assertEqual(revoked.status_code, 404)

    def test_media_template_copy_and_three_way_update_preserve_private_edits(self):
        source_material, _original = self.upload_audio_material()
        replacement_material, _replacement = self.upload_audio_material()
        project = self.request(
            "post", "/api/media/projects", json={"title": "播客节目结构", "kind": "podcast"},
            headers={"Idempotency-Key": "community-template-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        attached = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": source_material["id"]},
            headers={"Idempotency-Key": "community-template-source"},
        )
        self.assertEqual(attached.status_code, 200, attached.get_data(as_text=True))
        source = attached.json["data"]["sources"][0]
        edited = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [
                {"op": "insert", "source_id": source["asset_id"], "track_id": "voiceover",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 4},
                 "audio_role": "voiceover", "audio_stream_index": 0,
                 "timeline_start": {"num": 0, "den": 1}},
                {"op": "add_chapter", "title": "Opening", "start_sec": {"num": 1, "den": 8}},
            ]},
            headers={"Idempotency-Key": "community-template-first-edit"},
        )
        self.assertEqual(edited.status_code, 200, edited.get_data(as_text=True))
        original_version = edited.json["data"]["version"]
        original_chapter = edited.json["data"]["chapters"][0]

        def media_template_content(chapter_label="Opening"):
            return {
                "title": "播客节目结构",
                "summary": "可替换节目素材的可编辑播客时间线。",
                "body": "按时间线替换素材，并在预览后调整节目结构。",
                "tags": ["播客", "时间线"],
                "license": {
                    "view": True, "download": False, "adapt": True,
                    "commercial": False, "redistribute": False, "training": False,
                    "attribution": "节目模板作者", "expires_at": None,
                },
                "privacy_review": {"selected_content_only": True},
                "dependencies": [{
                    "id": source_material["id"], "version": 1,
                    "sha256": source_material["sha256"], "included": False,
                    "description": "原始节目音频仅用于校验源槽位，不随模板副本发布。",
                    "license": {
                        "view": True, "download": False, "adapt": True,
                        "commercial": False, "redistribute": True,
                        "training": False, "attribution": "节目原始音频", "expires_at": None,
                    },
                }],
            }

        draft_response = self.request(
            "post", "/api/publications", json={
                "source_kind": "media_project", "source_id": project_id,
                "source_version": original_version, "media_selection": {
                    "include_captions": True, "include_chapters": True,
                },
                "kind": "template", "visibility": "public",
                "content": media_template_content(),
            },
        )
        self.assertEqual(draft_response.status_code, 201, draft_response.get_data(as_text=True))
        draft = draft_response.json["data"]
        template = draft["content"]["template"]
        self.assertEqual(template["schema"], "oevira-media-template/1")
        self.assertEqual(len(template["slots"]), 1)
        moderator_id, moderator_token = self.add_member()
        recipient_id, recipient_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator_id}
        author_token = self.token

        def submit_and_publish(publication, *, base_version, key):
            submit_key = f"{key}-submit"
            submitted = self.post(
                f"/api/publications/{publication['id']}/submit",
                {"base_version": base_version, "preview_acknowledged": True},
                key=submit_key,
            )
            submitted_replay = self.post(
                f"/api/publications/{publication['id']}/submit",
                {"base_version": base_version, "preview_acknowledged": True},
                key=submit_key, status=(200,),
            )
            self.assertEqual(submitted_replay["version"], submitted["version"])
            self.token = moderator_token
            reviewed = self.post(
                f"/api/publications/{publication['id']}/review",
                {"base_version": submitted["version"], "decision": "approved", "reason": "已核对媒体时间线、替换槽和单项许可"},
            )
            self.token = author_token
            release = self.post(
                f"/api/publications/{publication['id']}/publish",
                {"base_version": reviewed["version"], "review_id": reviewed["id"]}, key=key,
            )
            return release

        first_release = submit_and_publish(draft, base_version=draft["version"], key="publish-media-template-first")
        self.assertEqual(first_release["delivery"], "local_review_only")
        self.assertNotIn(source_material["id"], json.dumps(first_release, ensure_ascii=False))
        self.assertNotIn("filename", json.dumps(first_release["content"]["template"], ensure_ascii=False))
        slots = first_release["content"]["template"]["slots"]
        self.token = recipient_token
        copy = self.post(
            f"/api/templates/{first_release['id']}/instantiate",
            {"purpose": "personal", "parameters": {slots[0]["id"]: replacement_material["id"]}},
            key="instantiate-community-media-template",
        )
        copied_project = copy["project"]
        self.assertEqual(copied_project["version"], 1)
        self.assertEqual(copied_project["sources"][0]["source_material_id"], replacement_material["id"])
        copied_chapter = copied_project["chapters"][0]
        self.assertEqual(copied_chapter["title"], "Opening")
        update_inventory = self.request("get", "/api/community/media/template-instances")
        self.assertEqual(update_inventory.status_code, 200, update_inventory.get_data(as_text=True))
        self.assertEqual(update_inventory.json["data"][0]["updates"], [])
        edited_copy = self.request(
            "post", f"/api/media/projects/{copied_project['id']}/edits",
            json={"base_version": 1, "operations": [{
                "op": "update_chapter", "chapter_id": copied_chapter["chapter_id"],
                "title": "我的节目开场", "start_sec": copied_chapter["start_sec"],
            }]},
            headers={"Idempotency-Key": "edit-community-template-copy"},
        )
        self.assertEqual(edited_copy.status_code, 200, edited_copy.get_data(as_text=True))

        self.token = author_token
        changed_source = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": original_version, "operations": [{
                "op": "update_chapter", "chapter_id": original_chapter["chapter_id"],
                "title": "作者更新的节目开场", "start_sec": original_chapter["start_sec"],
            }]},
            headers={"Idempotency-Key": "update-community-template-source"},
        )
        self.assertEqual(changed_source.status_code, 200, changed_source.get_data(as_text=True))
        published_publication = self.request("get", f"/api/publications/{draft['id']}")
        self.assertEqual(published_publication.status_code, 200)
        changed_publication = self.request(
            "patch", f"/api/publications/{draft['id']}",
            json={"base_version": published_publication.json["data"]["version"]},
        )
        self.assertEqual(changed_publication.status_code, 200, changed_publication.get_data(as_text=True))
        second_draft = changed_publication.json["data"]
        source_revision = self.request(
            "post", f"/api/publications/{draft['id']}/media-source-revisions",
            json={"base_version": second_draft["version"], "source_version": changed_source.json["data"]["version"],
                  "media_selection": {"include_captions": True, "include_chapters": True},
                  "content": media_template_content("作者更新的节目开场")},
        )
        self.assertEqual(source_revision.status_code, 200, source_revision.get_data(as_text=True))
        second_release = submit_and_publish(
            source_revision.json["data"], base_version=source_revision.json["data"]["version"],
            key="publish-media-template-second",
        )
        self.assertEqual(second_release["delivery"], "local_review_only")
        update_inventory = self.request("get", "/api/community/media/template-instances")
        self.assertEqual(update_inventory.status_code, 200, update_inventory.get_data(as_text=True))
        self.assertEqual(update_inventory.json["data"][0]["updates"][0]["id"], second_release["id"])

        self.token = recipient_token
        preview = self.post(
            f"/api/media/projects/{copied_project['id']}/template-updates/preview",
            {"base_version": edited_copy.json["data"]["version"], "release_id": second_release["id"]},
        )
        self.assertEqual(len(preview["conflicts"]), 1, json.dumps(preview["conflicts"], ensure_ascii=False))
        conflict = preview["conflicts"][0]
        self.assertIn("作者更新的节目开场", json.dumps(conflict, ensure_ascii=False))
        unresolved = self.request(
            "post", f"/api/media/projects/{copied_project['id']}/template-updates/apply",
            json={"base_version": preview["current_version"], "release_id": second_release["id"],
                  "preview_hash": preview["preview_hash"], "resolutions": {}},
            headers={"Idempotency-Key": "community-template-update-unresolved"},
        )
        self.assertEqual(unresolved.status_code, 409, unresolved.get_data(as_text=True))
        applied = self.request(
            "post", f"/api/media/projects/{copied_project['id']}/template-updates/apply",
            json={"base_version": preview["current_version"], "release_id": second_release["id"],
                  "preview_hash": preview["preview_hash"], "resolutions": {conflict["path"]: "keep_current"}},
            headers={"Idempotency-Key": "community-template-update-keep-edit"},
        )
        self.assertEqual(applied.status_code, 200, applied.get_data(as_text=True))
        self.assertTrue(applied.json["data"]["user_edits_preserved"])
        self.assertEqual(applied.json["data"]["project"]["chapters"][0]["title"], "我的节目开场")
        self.assertEqual(applied.json["data"]["project"]["version"], 3)
        replayed_copy = self.post(
            f"/api/templates/{first_release['id']}/instantiate",
            {"purpose": "personal", "parameters": {slots[0]["id"]: replacement_material["id"]}},
            key="instantiate-community-media-template",
        )
        self.assertEqual(replayed_copy["project"]["id"], copied_project["id"])
        self.assertEqual(replayed_copy["project"]["version"], 1)

        connection = self.store.connect()
        try:
            connection.executescript("""
                PRAGMA foreign_keys=OFF;
                BEGIN IMMEDIATE;
                CREATE TABLE community_media_template_instances_legacy (
                    media_project_id TEXT PRIMARY KEY REFERENCES media_projects(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    release_id TEXT NOT NULL REFERENCES publication_releases(id),
                    release_content_hash TEXT NOT NULL,
                    source_project_id TEXT NOT NULL,
                    source_project_version INTEGER NOT NULL CHECK(source_project_version > 0),
                    source_manifest_json TEXT NOT NULL,
                    slot_asset_map_json TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                INSERT INTO community_media_template_instances_legacy
                SELECT media_project_id,workspace_id,release_id,release_content_hash,source_project_id,
                       source_project_version,source_manifest_json,slot_asset_map_json,created_by,created_at
                FROM community_media_template_instances;
                DROP TABLE community_media_template_instances;
                ALTER TABLE community_media_template_instances_legacy RENAME TO community_media_template_instances;
                COMMIT;
                PRAGMA foreign_keys=ON;
            """)
        finally:
            connection.close()
        install_community_media_schema(self.store)
        source_project_column = next(
            row for row in self.store.rows("PRAGMA table_info(community_media_template_instances)")
            if row["name"] == "source_project_id"
        )
        self.assertEqual(source_project_column["notnull"], 0)
        self.assertEqual(self.store.rows("PRAGMA foreign_key_check"), [])

        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                ("community-erasure-tombstone", "已删除账号", utc_now()),
            )
            prepare_account_deletion(
                connection,
                self.identity["workspace"]["id"],
                self.identity["user"]["id"],
                recipient_id,
                "community-erasure-tombstone",
            )
        lineage = self.store.row(
            "SELECT source_project_id,source_manifest_json,release_id FROM community_media_template_instances WHERE media_project_id=?",
            (copied_project["id"],),
        )
        self.assertIsNotNone(lineage)
        self.assertIsNone(lineage["source_project_id"])
        retained_lineage = json.loads(lineage["source_manifest_json"])
        self.assertTrue(retained_lineage["source_manifest_erased"])
        self.assertTrue(retained_lineage["license"]["adapt"])
        self.assertNotIn("manifest", retained_lineage)
        self.assertNotIn(project_id, lineage["source_manifest_json"])
        self.assertNotIn(source_material["id"], lineage["source_manifest_json"])
        self.assertIsNotNone(self.store.row(
            "SELECT id FROM media_projects WHERE id=? AND owner_user_id=?",
            (copied_project["id"], recipient_id),
        ))
        self.assertIsNotNone(self.store.row(
            "SELECT 1 FROM community_media_template_update_history WHERE media_project_id=?",
            (copied_project["id"],),
        ))
        erased_release = self.store.row(
            "SELECT snapshot_json,withdrawn_at FROM publication_releases WHERE id=?",
            (lineage["release_id"],),
        )
        erased_snapshot = json.loads(erased_release["snapshot_json"])
        self.assertEqual(erased_snapshot["title"], "已撤回作品")
        self.assertTrue(erased_snapshot["retained_license"]["view"])
        self.assertNotIn(source_material["id"], erased_release["snapshot_json"])
        self.assertEqual(self.store.row(
            "SELECT COUNT(*) AS count FROM publication_media_releases WHERE release_id=?",
            (lineage["release_id"],),
        )["count"], 0)

    def test_exact_comments_and_approval_become_stale_after_revision(self):
        artifact = self.make_result()
        sections = artifact["input_snapshot"]["business"]["result"]["sections"]
        purpose = next(item for item in sections if item["id"] == "purpose")
        purpose_quote = purpose["body"][:24]
        comment = self.post(f"/api/artifacts/{artifact['id']}/comments", {
            "artifact_version": 1, "body": "请把本次业务问题说得更具体",
            "anchor": {"kind": "business_analysis", "section_id": "purpose", "property": "body",
                       "start": 0, "end": len(purpose_quote), "quote": purpose_quote},
        })
        unchanged_title_quote = "本次业务问题"
        title_start = artifact["body"].index(unchanged_title_quote)
        title_comment = self.post(f"/api/artifacts/{artifact['id']}/comments", {
            "artifact_version": 1, "body": "标题没有变化",
            "anchor": {"start": title_start, "end": title_start + len(unchanged_title_quote), "quote": unchanged_title_quote},
        })
        approval = self.post(f"/api/artifacts/{artifact['id']}/approvals", {"artifact_version": 1, "purpose": "client_delivery", "scope": {"format": "pdf"}})
        with self.store.transaction(immediate=False) as connection:
            require_artifact_approval(connection, self.identity, approval["id"], artifact["id"], 1, "client_delivery", {"format": "pdf"})
        revised = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "input_changes": {"goal": "核对活动当日的排班与等候时间"}})
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaises(ApiError) as error:
                require_artifact_approval(connection, self.identity, approval["id"], artifact["id"], 1, "client_delivery", {"format": "pdf"})
            self.assertEqual(error.exception.code, "APPROVAL_NOT_CURRENT")
        resolved = self.request("patch", f"/api/artifacts/{artifact['id']}/comments/{comment['id']}", json={"base_version": 1, "state": "resolved", "resolution": {"artifact_version": revised["version"], "disposition": "adopted", "reason": "问题已改为确切排班与等候时间"}})
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.json["data"]["artifact_version"], 1)
        self.assertEqual(resolved.json["data"]["resolution"]["artifact_version"], 2)
        unchanged_resolution = self.request(
            "patch", f"/api/artifacts/{artifact['id']}/comments/{title_comment['id']}",
            json={"base_version": 1, "state": "resolved", "resolution": {"artifact_version": revised["version"], "disposition": "adopted", "reason": "标题已处理"}},
        )
        self.assertEqual(unchanged_resolution.status_code, 409, unchanged_resolution.get_data(as_text=True))
        self.assertEqual(unchanged_resolution.json["error"]["code"], "COMMENT_TARGET_UNCHANGED")

    def test_legacy_artifact_publications_migrate_to_source_union_without_losing_history(self):
        store = Store(self.root / "legacy-publication-store")
        store.initialize()
        install_media_schema(store)
        now = utc_now()
        with store.transaction() as connection:
            connection.execute("INSERT INTO users(id,name,created_at) VALUES('legacy-author','Legacy Author',?)", (now,))
            connection.execute("INSERT INTO workspaces(id,name,created_at) VALUES('legacy-workspace','Legacy Workspace',?)", (now,))
            connection.execute("INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES('legacy-workspace','legacy-author','owner',1,?)", (now,))
            connection.execute("INSERT INTO artifacts(id,workspace_id,work_id,owner_user_id,title,current_version,created_at,updated_at) VALUES('legacy-artifact','legacy-workspace',NULL,'legacy-author','Legacy Artifact',1,?,?)", (now, now))
            connection.execute("INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by) VALUES('legacy-artifact','legacy-workspace',1,'Legacy Artifact','Body','[]','{}','',?,'legacy-author')", (now,))
            connection.execute("""CREATE TABLE publications(
                id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                author_id TEXT NOT NULL REFERENCES users(id),artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                artifact_version INTEGER NOT NULL,kind TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,
                visibility TEXT NOT NULL,content_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE publication_versions(
                publication_id TEXT NOT NULL REFERENCES publications(id),version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,created_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
                PRIMARY KEY(publication_id,version)
            )""")
            connection.execute("INSERT INTO publications VALUES('legacy-publication','legacy-workspace','legacy-author','legacy-artifact',1,'work',1,'draft','private','{}',?,?)", (now, now))
            connection.execute("INSERT INTO publication_versions VALUES('legacy-publication',1,'{\"legacy\":true}','legacy-author',?)", (now,))

        install_community_schema(store)
        install_community_schema(store)
        migrated = store.row("SELECT source_kind,source_id,source_version,artifact_id,artifact_version FROM publications WHERE id='legacy-publication'")
        self.assertEqual(dict(migrated), {
            "source_kind": "artifact", "source_id": "legacy-artifact", "source_version": 1,
            "artifact_id": "legacy-artifact", "artifact_version": 1,
        })
        history = store.row("SELECT snapshot_json FROM publication_versions WHERE publication_id='legacy-publication' AND version=1")
        self.assertEqual(history["snapshot_json"], '{"legacy":true}')
        self.assertEqual(store.rows("PRAGMA foreign_key_check"), [])

    def test_publication_origin_configuration_does_not_claim_hosted_delivery(self):
        self.app.config["PUBLICATION_ORIGIN"] = "https://public.example.test"
        capabilities = self.request("get", "/api/community/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(capabilities.json["data"]["delivery"], "local_review_only")

    def test_uploaded_media_sidecars_are_sanitized_and_range_playable(self):
        media, original = self.upload_audio_material()
        caption_text = "WEBVTT\n\n00:00:00.000 --> 00:00:00.200\nDoor opens\n"
        caption = self.post("/api/materials", {
            "title": "节目字幕原件", "filename": "captions.vtt", "text": caption_text,
        })
        chapters_payload = {
            "schema": "oevira.media.chapters.v1",
            "time_reference": "rational_timeline_seconds",
            "chapters": [{
                "chapter_id": "private-chapter-id", "title": "开场",
                "start_sec": {"num": 0, "den": 1}, "end_sec": {"num": 1, "den": 4},
                "source_ranges": [{"source_material_id": media["id"], "filename": "podcast.wav"}],
            }],
        }
        chapters = self.post("/api/materials", {
            "title": "章节表原件", "filename": "chapters.json",
            "text": json.dumps(chapters_payload, ensure_ascii=False),
        })
        dependency_records = []
        for item, description in (
            (media, "作者上传的播客音频"),
            (caption, "独立字幕原件"),
            (chapters, "独立章节表原件"),
        ):
            dependency_records.append({
                "id": item["id"], "version": 1, "sha256": item["sha256"],
                "included": True, "description": description,
                "license": {
                    "view": True, "download": False, "adapt": False,
                    "commercial": False, "redistribute": True, "training": False,
                    "attribution": "作者提供", "expires_at": None,
                },
            })
        content = {
            "title": "带字幕和章节的播客",
            "summary": "按真实上传的音频、字幕和章节播放。",
            "body": "",
            "tags": ["播客"],
            "license": {
                "view": True, "download": False, "adapt": False,
                "commercial": False, "redistribute": False, "training": False,
                "attribution": "节目作者", "expires_at": None,
            },
            "privacy_review": {"selected_content_only": True},
            "dependencies": dependency_records,
        }
        out_of_range = dict(chapters_payload)
        out_of_range["chapters"] = [{
            "title": "超出节目范围", "start_sec": {"num": 1, "den": 4},
            "end_sec": {"num": 1, "den": 2},
        }]
        invalid_chapters = self.post("/api/materials", {
            "title": "无效章节原件", "filename": "bad-chapters.json",
            "text": json.dumps(out_of_range, ensure_ascii=False),
        })
        invalid_records = [*dependency_records[:2], {
            **dependency_records[2], "id": invalid_chapters["id"],
            "sha256": invalid_chapters["sha256"],
        }]
        rejected = self.request(
            "post", "/api/publications", json={
                "source_kind": "media_material", "source_id": media["id"],
                "source_version": 1,
                "media_selection": {"chapter_material_id": invalid_chapters["id"]},
                "kind": "work", "visibility": "public",
                "content": {**content, "dependencies": invalid_records},
            },
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json["error"]["code"], "MEDIA_CHAPTERS_INVALID")

        invalid_caption = self.post("/api/materials", {
            "title": "无效字幕原件", "filename": "bad-captions.vtt",
            "text": "WEBVTT\n\n00:00:00.200 --> 00:00:00.100\n时间倒序\n",
        })
        invalid_caption_records = [
            {**dependency_records[0]},
            {**dependency_records[1], "id": invalid_caption["id"], "sha256": invalid_caption["sha256"]},
        ]
        rejected_caption = self.request(
            "post", "/api/publications", json={
                "source_kind": "media_material", "source_id": media["id"],
                "source_version": 1,
                "media_selection": {"caption_material_id": invalid_caption["id"]},
                "kind": "work", "visibility": "public",
                "content": {**content, "dependencies": invalid_caption_records},
            },
        )
        self.assertEqual(rejected_caption.status_code, 400)
        self.assertEqual(rejected_caption.json["error"]["code"], "MEDIA_CAPTIONS_INVALID")

        draft = self.post("/api/publications", {
            "source_kind": "media_material", "source_id": media["id"],
            "source_version": 1,
            "media_selection": {
                "caption_material_id": caption["id"],
                "chapter_material_id": chapters["id"],
                "caption_language": "en",
            },
            "kind": "work", "visibility": "public", "content": content,
        })
        self.assertEqual(len(draft["content"]["media"]["captions"]), 1)
        self.assertEqual(draft["content"]["media"]["chapters"][0]["metadata"]["chapters"][0]["title"], "开场")
        self.assertNotIn(media["id"], json.dumps(draft["content"]["media"], ensure_ascii=False))
        self.assertNotIn("private-chapter-id", json.dumps(draft["content"]["media"], ensure_ascii=False))
        submitted = self.post(
            f"/api/publications/{draft['id']}/submit",
            {"base_version": draft["version"], "preview_acknowledged": True},
        )
        moderator_id, moderator_token = self.add_member()
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {moderator_id}
        author_token = self.token
        self.token = moderator_token
        reviewed = self.post(
            f"/api/publications/{draft['id']}/review",
            {"base_version": submitted["version"], "decision": "approved", "reason": "已核对音频、字幕、章节和分发许可"},
        )
        self.token = author_token
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": reviewed["version"], "review_id": reviewed["id"]},
            key="publish-uploaded-sidecars",
        )
        self.assertNotIn(media["id"], json.dumps(release, ensure_ascii=False))
        self.assertNotIn(caption["id"], json.dumps(release, ensure_ascii=False))
        self.assertNotIn(chapters["id"], json.dumps(release, ensure_ascii=False))
        self.assertNotIn("source_ranges", json.dumps(release["content"]["media"]["chapters"], ensure_ascii=False))

        anonymous = self.app.test_client()
        caption_id = release["content"]["media"]["captions"][0]["id"]
        caption_grant = anonymous.post(
            f"/api/releases/{release['id']}/media/{caption_id}/playback",
            json={"purpose": "view"}, base_url=business_test.BASE,
        )
        self.assertEqual(caption_grant.status_code, 200, caption_grant.get_data(as_text=True))
        caption_read = anonymous.get(
            caption_grant.json["data"]["stream_url"], base_url=business_test.BASE,
        )
        self.assertEqual(caption_read.status_code, 200)
        self.assertEqual(caption_read.data.decode("utf-8"), caption_text)
        caption_read.close()
        chapter_id = release["content"]["media"]["chapters"][0]["id"]
        chapter_grant = anonymous.post(
            f"/api/releases/{release['id']}/media/{chapter_id}/playback",
            json={"purpose": "view"}, base_url=business_test.BASE,
        )
        self.assertEqual(chapter_grant.status_code, 200, chapter_grant.get_data(as_text=True))
        chapter_read = anonymous.get(
            chapter_grant.json["data"]["stream_url"], base_url=business_test.BASE,
        )
        self.assertEqual(chapter_read.status_code, 200)
        chapter_bytes = json.loads(chapter_read.data)
        self.assertEqual(chapter_bytes["chapters"][0]["title"], "开场")
        self.assertNotIn("source_ranges", chapter_bytes["chapters"][0])
        chapter_read.close()

    def test_follow_feature_report_review_collection_and_queue_are_real_user_actions(self):
        original_token = self.token
        creator_id, creator_token = self.add_member()
        self.token = creator_token
        artifact = self.request(
            "post", "/api/artifacts",
            json={"title": "创作者的活动复盘", "body": "作者公开说明的具体内容。", "citations": []},
        ).json["data"]
        payload = {
            "artifact_id": artifact["id"], "artifact_version": artifact["version"],
            "kind": "work", "visibility": "public",
            "content": {
                "title": "公开的活动复盘作品", "summary": "一份由作者明确发布的复盘。",
                "body": "本次复盘的主要观察。", "tags": ["复盘"],
                "license": {"view": True, "download": True, "adapt": False, "commercial": False,
                            "redistribute": False, "training": False, "attribution": "社区作者", "expires_at": None},
                "privacy_review": {"selected_content_only": True}, "dependencies": [],
            },
        }
        draft = self.post("/api/publications", payload)
        submitted = self.post(f"/api/publications/{draft['id']}/submit", {"base_version": 1, "preview_acknowledged": True})
        self.app.config["COMMUNITY_MODERATOR_USER_IDS"] = {self.identity["user"]["id"]}
        self.token = original_token
        review = self.post(
            f"/api/publications/{draft['id']}/review",
            {"base_version": submitted["version"], "decision": "approved", "reason": "范围、内容和许可已核对"},
        )
        self.token = creator_token
        release = self.post(
            f"/api/publications/{draft['id']}/publish",
            {"base_version": review["version"], "review_id": review["id"]},
            key="publish-creator-feed",
        )

        self.token = original_token
        followed = self.post(f"/api/creators/{creator_id}/follow", {"follow": True})
        self.assertTrue(followed["following"])
        follows = self.request("get", "/api/community/follows").json["data"]
        self.assertEqual(follows[0]["creator_id"], creator_id)
        profile = self.request("get", f"/api/creators/{creator_id}")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json["data"]["followed"], True)
        self.assertEqual(profile.json["data"]["display_name"], "社区作者")
        following_feed = self.request("get", "/api/discovery?order=following").json["data"]
        self.assertEqual([item["id"] for item in following_feed["items"]], [release["id"]])

        start = datetime.now(timezone.utc)
        end = start + timedelta(days=1)
        curated = self.post(
            f"/api/moderation/releases/{release['id']}/feature",
            {"reason": "编辑部按本期主题人工精选", "rank": 1,
             "starts_at": start.isoformat(), "expires_at": end.isoformat()},
        )
        self.assertEqual(curated["state"], "featured")
        featured = self.request("get", "/api/discovery?order=featured").json["data"]
        self.assertEqual(featured["items"][0]["curation"]["basis"], "moderator_curated")
        self.assertEqual(featured["items"][0]["curation"]["reason"], "编辑部按本期主题人工精选")

        collection = self.post(f"/api/releases/{release['id']}/items", {"kind": "collection"})
        queue = self.post(f"/api/releases/{release['id']}/items", {"kind": "queue"})
        self.assertTrue(collection["saved"])
        self.assertIsNone(queue["progress_seconds"])
        library = self.request("get", "/api/community/items").json["data"]
        self.assertEqual({item["kind"] for item in library}, {"collection", "queue"})
        invalid_progress = self.request(
            "post", f"/api/releases/{release['id']}/items",
            json={"kind": "queue", "progress_seconds": float("inf")},
        )
        self.assertEqual(invalid_progress.status_code, 400)

        report = self.post(
            f"/api/releases/{release['id']}/reports",
            {"reason": "举报原因由用户填写，等待人工核验"}, key="report-release-one",
        )
        self.assertEqual(report["state"], "open")
        moderation_queue = self.request("get", "/api/moderation/reports").json["data"]
        self.assertEqual(moderation_queue[0]["id"], report["id"])
        resolved = self.post(
            f"/api/moderation/reports/{report['id']}/resolve",
            {"expected_state": "open", "decision": "dismiss", "reason": "已按当前发布版本人工核验"},
        )
        self.assertEqual(resolved["state"], "dismissed")
        self.post(f"/api/creators/{creator_id}/follow", {"follow": False})
        self.assertEqual(self.request("get", "/api/discovery?order=following").json["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
