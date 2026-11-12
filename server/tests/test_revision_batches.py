from __future__ import annotations

import io
import unittest
import zipfile

from server.oevira.api import ApiError
from server.oevira.worker import _claim_job, _run_claim_safely, run_once
from server.tests import test_core as core_test


class RevisionBatchTests(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request
    create_work = core_test.CoreApiTest.create_work

    def prepare(self):
        work = self.create_work(title="客户跨成果修改")
        artifacts = []
        for index in range(2):
            response = self.request("post", "/api/artifacts", json={"title": f"成果{index}", "body": "价格100元。保留人工结尾。", "citations": [], "work_id": work["id"]})
            self.assertEqual(response.status_code, 201)
            artifacts.append(response.json["data"])
        members = [{"member_id": f"member-{index}", "target": {"kind": "artifact", "id": artifact["id"], "base_version": 1},
                    "command": {"title": artifact["title"], "body": "价格120元。保留人工结尾。", "citations": []}}
                   for index, artifact in enumerate(artifacts)]
        response = self.request("post", f"/api/works/{work['id']}/revision-batches", json={"title": "两份成果同步改价", "members": members}, headers={"Idempotency-Key": "revision-batch"})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["data"], artifacts

    def read(self, batch):
        response = self.request("get", f"/api/revision-batches/{batch['id']}")
        self.assertEqual(response.status_code, 200)
        return response.json["data"]

    def retry(self, batch):
        response = self.request("post", f"/api/jobs/{batch['job']['id']}/retry", json={"operation_id": "batch-retry", "base_control_revision": 0})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))

    def test_partial_retry_keeps_completed_revision_and_unselected_history(self):
        batch, artifacts = self.prepare()
        original = self.app.extensions["oevira.revision_handlers"]["text"]

        def fail_second(store, identity, target_id, command, key):
            if target_id == artifacts[1]["id"]:
                raise ApiError("TEST_STORAGE_BUSY", "The second result is temporarily unavailable", 409)
            return original(store, identity, target_id, command, key)

        self.app.extensions["oevira.revision_handlers"]["text"] = fail_second
        self.assertTrue(run_once(self.app))
        partial = self.read(batch)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual([item["status"] for item in partial["members"]], ["completed", "failed"])
        self.app.extensions["oevira.revision_handlers"]["text"] = original
        self.retry(batch)
        self.assertTrue(run_once(self.app))
        complete = self.read(batch)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual([item["result_ref"]["version"] for item in complete["members"]], [2, 2])
        for artifact in artifacts:
            current = self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
            self.assertEqual(current["body"], "价格120元。保留人工结尾。")
            self.assertEqual(current["version"], 2)
            old = self.request("get", f"/api/artifacts/{artifact['id']}?version=1").json["data"]
            self.assertEqual(old["body"], "价格100元。保留人工结尾。")

    def test_domain_commit_before_batch_receipt_recovers_same_version(self):
        batch, artifacts = self.prepare()
        original = self.app.extensions["oevira.revision_handlers"]["text"]

        def crash_after_domain_commit(*args):
            original(*args)
            raise RuntimeError("Injected loss of the coordinator receipt")

        self.app.extensions["oevira.revision_handlers"]["text"] = crash_after_domain_commit
        _run_claim_safely(self.app, _claim_job(self.store))
        self.assertEqual(self.read(batch)["status"], "interrupted")
        self.app.extensions["oevira.revision_handlers"]["text"] = original
        self.retry(batch)
        self.assertTrue(run_once(self.app))
        self.assertEqual(self.read(batch)["status"], "completed")
        for artifact in artifacts:
            self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 2)

    def test_pause_after_one_domain_commit_preserves_it_and_stops_next_member(self):
        batch, artifacts = self.prepare()
        original = self.app.extensions["oevira.revision_handlers"]["text"]

        def pause_after_first(*args):
            result = original(*args)
            paused = self.request("post", f"/api/jobs/{batch['job']['id']}/pause", json={"operation_id": "pause-batch", "base_control_revision": 0})
            self.assertEqual(paused.json["data"]["status"], "pause_requested")
            return result

        self.app.extensions["oevira.revision_handlers"]["text"] = pause_after_first
        self.assertTrue(run_once(self.app))
        paused = self.read(batch)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual([item["status"] for item in paused["members"]], ["completed", "pending"])
        self.assertEqual(self.request("get", f"/api/artifacts/{artifacts[1]['id']}").json["data"]["version"], 1)
        self.app.extensions["oevira.revision_handlers"]["text"] = original
        response = self.request("post", f"/api/jobs/{batch['job']['id']}/resume", json={"operation_id": "resume-batch", "base_control_revision": 1})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        self.assertEqual(self.read(batch)["status"], "completed")

    def test_one_stale_parent_does_not_overwrite_human_work_or_discard_other_result(self):
        batch, artifacts = self.prepare()
        human = self.request("post", f"/api/artifacts/{artifacts[1]['id']}/versions", json={"base_version": 1, "title": "新人工版", "body": "价格另议，保留人工决定。", "citations": []})
        self.assertEqual(human.status_code, 200)
        self.assertTrue(run_once(self.app))
        partial = self.read(batch)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["members"][0]["status"], "completed")
        self.assertEqual(partial["members"][1]["error_code"], "VERSION_CONFLICT")
        current = self.request("get", f"/api/artifacts/{artifacts[1]['id']}").json["data"]
        self.assertEqual(current["body"], "价格另议，保留人工决定。")
        self.assertEqual(current["version"], 2)

    def test_domain_response_without_persisted_version_cannot_complete_a_member(self):
        batch, artifacts = self.prepare()
        self.app.extensions["oevira.revision_handlers"]["text"] = lambda store, identity, target_id, command, key: {"id": target_id, "version": 2}
        self.assertTrue(run_once(self.app))
        partial = self.read(batch)
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(all(item["result_ref"] is None for item in partial["members"]))
        self.assertTrue(all(item["error_code"] == "REVISION_RESULT_UNCONFIRMED" for item in partial["members"]))
        for artifact in artifacts:
            self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 1)

    def test_old_worker_error_cannot_replace_newer_completed_member_receipts(self):
        from server.oevira.revisions import process_revision_job

        batch, _ = self.prepare()
        claim = _claim_job(self.store)
        original = self.app.extensions["oevira.revision_handlers"]["text"]

        def delayed_failure(*args):
            original(*args)
            self.app.extensions["oevira.revision_handlers"]["text"] = original
            newer = {**claim, "generation": claim["generation"] + 1}
            with self.store.transaction() as connection:
                connection.execute("UPDATE jobs SET lease_generation=? WHERE id=?", (newer["generation"], claim["id"]))
            process_revision_job(self.app, newer)
            raise ApiError("OLD_WORKER_FAILED", "A late old attempt failed", 409)

        self.app.extensions["oevira.revision_handlers"]["text"] = delayed_failure
        with self.app.app_context():
            process_revision_job(self.app, claim)
        final = self.read(batch)
        self.assertEqual(final["status"], "completed")
        self.assertEqual([member["status"] for member in final["members"]], ["completed", "completed"])

    def test_pdf_feedback_revision_uses_native_owner_and_delivers_original_pages(self):
        import pdfplumber
        from reportlab.pdfgen import canvas

        work = self.create_work(title="PDF客户反馈")
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(400, 600))
        pdf.drawString(30, 500, "Budget 150 RMB.")
        pdf.showPage()
        pdf.drawString(30, 500, "Keep this page untouched.")
        pdf.save()
        original = output.getvalue()
        uploaded = self.request("post", "/api/materials", data={"title": "原定稿", "work_id": work["id"], "file": (io.BytesIO(original), "review.pdf")}, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        material_id = uploaded.json["data"]["id"]
        first = self.request("post", f"/api/materials/{material_id}/pdf-revisions", json={"work_id": work["id"], "changes": [{"page": 1, "quote": "Budget 150 RMB.", "comment": "请核对金额"}]}, headers={"Idempotency-Key": "pdf-first"})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        artifact_id = first.json["data"]["artifact"]["id"]
        batch = self.request("post", f"/api/works/{work['id']}/revision-batches", json={"title": "补充一条核对意见", "members": [{"member_id": "pdf", "target": {"kind": "artifact", "id": artifact_id, "base_version": 1}, "command": {"changes": [{"page": 1, "quote": "Budget 150 RMB.", "comment": "保留原批注，补充确认期间"}]}}]}, headers={"Idempotency-Key": "pdf-batch"})
        self.assertEqual(batch.status_code, 202, batch.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("revision_batch",)))
        revised = self.request("get", f"/api/revision-batches/{batch.json['data']['id']}").json["data"]
        self.assertEqual(revised["status"], "completed", revised)
        delivery = self.request("post", f"/api/works/{work['id']}/deliveries", json={"title": "PDF意见版交付", "members": [{"member_id": "pdf", "kind": "artifact", "artifact_id": artifact_id, "version": 2, "format": "pdf", "filename": "反馈.pdf"}]}, headers={"Idempotency-Key": "pdf-delivery"})
        self.assertEqual(delivery.status_code, 202, delivery.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("delivery_batch",)))
        ready = self.request("get", f"/api/deliveries/{delivery.json['data']['id']}").json["data"]
        self.assertEqual(ready["status"], "completed", ready)
        package = self.request("get", ready["download_url"])
        with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
            final = archive.read("反馈.pdf")
        self.assertTrue(final.startswith(original))
        with pdfplumber.open(io.BytesIO(final)) as reopened:
            self.assertEqual(len(reopened.pages), 2)
            self.assertEqual(reopened.pages[1].extract_text(), "Keep this page untouched.")
            comments = [annotation["contents"] for annotation in reopened.pages[0].annots]
            self.assertEqual(comments, ["请核对金额", "保留原批注，补充确认期间"])


if __name__ == "__main__":
    unittest.main()
