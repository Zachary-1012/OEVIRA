from __future__ import annotations

import io
import hashlib
import math
import struct
import unittest
from unittest.mock import patch
import zipfile
import wave

from docx import Document

from server.oevira import create_app
from server.oevira.api import ApiError
from server.oevira.worker import _claim_job, _run_claim_safely, run_once
from server.tests import test_core as core_test


class DeliveryTest(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request
    create_work = core_test.CoreApiTest.create_work
    create_other_workspace = core_test.CoreApiTest.create_other_workspace
    create_text_material = core_test.CoreApiTest.create_text_material

    def prepare(self):
        work = self.create_work(title="客户第二版交付")
        response = self.request("post", "/api/artifacts", json={"work_id": work["id"], "title": "已确认提案", "body": "确认预算120万元。人工保留结尾。", "citations": []})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        artifact = response.json["data"]
        return work, artifact

    def members(self, artifact, *, version=1):
        return [
            {"member_id": "proposal", "kind": "artifact", "artifact_id": artifact["id"], "version": version, "format": "docx", "filename": "客户提案.docx"},
            {"member_id": "notes", "kind": "artifact", "artifact_id": artifact["id"], "version": 1, "format": "md", "filename": "核对说明.md"},
        ]

    def enqueue(self, work, members, *, key="delivery-one", parent=None):
        data = {"title": "方案与核对说明", "members": members}
        if parent:
            data["parent_batch_id"] = parent
        response = self.request("post", f"/api/works/{work['id']}/deliveries", json=data, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["data"]

    def read(self, batch):
        response = self.request("get", f"/api/deliveries/{batch['id']}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["data"]

    def retry(self, batch):
        current = self.read(batch)
        response = self.request("post", f"/api/jobs/{current['job']['id']}/retry",
                                json={"operation_id": "retry-delivery", "base_control_revision": current["job"]["control_revision"]})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["data"]

    def test_real_files_frozen_versions_cold_reopen_and_incremental_redelivery(self):
        work, artifact = self.prepare()
        first = self.enqueue(work, self.members(artifact))
        self.assertTrue(run_once(self.app))
        ready = self.read(first)
        self.assertEqual(ready["status"], "completed", ready)
        download = self.request("get", ready["download_url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"客户提案.docx", "核对说明.md", "交付说明.txt"})
            document = Document(io.BytesIO(archive.read("客户提案.docx")))
            self.assertIn("确认预算120万元。人工保留结尾。", "\n".join(p.text for p in document.paragraphs))
            original_notes = archive.read("核对说明.md")
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM exports")["n"], 2)
        revised = self.request("post", f"/api/artifacts/{artifact['id']}/versions",
                                json={"base_version": 1, "title": "已确认提案", "body": "确认预算150万元。人工保留结尾。", "citations": []})
        self.assertEqual(revised.status_code, 200)
        second = self.enqueue(work, self.members(artifact, version=2), key="delivery-two", parent=first["id"])
        self.assertTrue(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM exports")["n"], 3)
        self.app = create_app(self.root, expected_port=8317)
        self.client = self.app.test_client()
        self.client.set_cookie("oevira_session", self.token, domain="127.0.0.1")
        second = self.read(second)
        new_file = self.request("get", second["download_url"])
        with zipfile.ZipFile(io.BytesIO(new_file.data)) as archive:
            self.assertEqual(archive.read("核对说明.md"), original_notes)
            document = Document(io.BytesIO(archive.read("客户提案.docx")))
            self.assertIn("150万元", "\n".join(p.text for p in document.paragraphs))
        self.assertEqual(self.request("get", ready["download_url"]).data, download.data)

    def test_failed_member_retries_without_regenerating_completed_file(self):
        from server.oevira import delivery

        work, artifact = self.prepare()
        batch = self.enqueue(work, self.members(artifact))
        original = delivery._create_export
        calls = []

        def fail_notes(store, artifact_id, data, **kwargs):
            calls.append(data["format"])
            if data["format"] == "md":
                raise ApiError("TEST_STORAGE_BUSY", "The selected output is temporarily unavailable", 409)
            return original(store, artifact_id, data, **kwargs)

        with patch.object(delivery, "_create_export", side_effect=fail_notes):
            self.assertTrue(run_once(self.app))
        partial = self.read(batch)
        self.assertEqual(partial["status"], "partial")
        self.assertIsNone(partial["download_url"])
        self.assertEqual([item["status"] for item in partial["members"]], ["completed", "failed"])
        saved_export = partial["members"][0]["file"]["export_id"]
        self.retry(batch)
        with patch.object(delivery, "_create_export", wraps=original) as exports:
            self.assertTrue(run_once(self.app))
        self.assertEqual(exports.call_count, 1)
        final = self.read(batch)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["members"][0]["file"]["export_id"], saved_export)
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM exports")["n"], 2)

    def test_published_package_survives_commit_interruption_without_duplicate_exports(self):
        from server.oevira import delivery

        work, artifact = self.prepare()
        batch = self.enqueue(work, self.members(artifact))
        original = delivery._package

        def interrupt_after_publish(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Injected process interruption after file publication")

        with patch.object(delivery, "_package", side_effect=interrupt_after_publish):
            _run_claim_safely(self.app, _claim_job(self.store))
        self.assertEqual(self.read(batch)["job"]["status"], "interrupted")
        self.retry(batch)
        with patch.object(delivery, "_create_export", side_effect=AssertionError("Completed member must not be re-exported")):
            self.assertTrue(run_once(self.app))
        self.assertEqual(self.read(batch)["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM exports")["n"], 2)

    def test_old_worker_error_cannot_damage_newer_completed_delivery(self):
        from server.oevira import delivery

        work, artifact = self.prepare()
        batch = self.enqueue(work, self.members(artifact))
        claim = _claim_job(self.store)
        original = delivery._create_export

        def delayed_failure(*args, **kwargs):
            original(*args, **kwargs)
            newer = {**claim, "generation": claim["generation"] + 1}
            with self.store.transaction() as connection:
                connection.execute("UPDATE jobs SET lease_generation=? WHERE id=?", (newer["generation"], claim["id"]))
            with patch.object(delivery, "_create_export", original):
                delivery.process_delivery_job(self.app, newer)
            raise ApiError("OLD_WORKER_FAILED", "A late old attempt failed", 409)

        with self.app.app_context(), patch.object(delivery, "_create_export", side_effect=delayed_failure):
            delivery.process_delivery_job(self.app, claim)
        final = self.read(batch)
        self.assertEqual(final["status"], "completed")
        self.assertEqual([member["status"] for member in final["members"]], ["completed", "completed"])

    def test_cross_workspace_and_unsafe_names_are_rejected(self):
        work, artifact = self.prepare()
        bad = self.members(artifact)
        bad[0]["filename"] = "../escape.docx"
        response = self.request("post", f"/api/works/{work['id']}/deliveries",
                                json={"title": "越界文件名", "members": bad}, headers={"Idempotency-Key": "bad"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM delivery_batches")["n"], 0)
        for field in ("format", "audience"):
            invalid = self.members(artifact)
            invalid[0][field] = []
            result = self.request("post", f"/api/works/{work['id']}/deliveries",
                                  json={"title": "格式校验", "members": invalid}, headers={"Idempotency-Key": "invalid-" + field})
            self.assertEqual(result.status_code, 400, result.get_data(as_text=True))
        batch = self.enqueue(work, self.members(artifact))
        self.assertTrue(run_once(self.app))
        token, _ = self.create_other_workspace()
        for path in (f"/api/deliveries/{batch['id']}", f"/api/deliveries/{batch['id']}/file"):
            denied = self.request("get", path, headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(denied.status_code, 404, denied.get_data(as_text=True))

    def test_client_scope_and_corrupted_package_cannot_be_delivered(self):
        work, artifact = self.prepare()
        commission = self.request("post", "/api/business/commissions", json={"name": "客户甲", "scope": {}},
                                  headers={"Idempotency-Key": "client-one"})
        self.assertEqual(commission.status_code, 201)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO work_commissions VALUES(?,?,?)", (work["id"], commission.json["data"]["id"], "2026-11-10T00:00:00Z"))
        other = self.create_work(title="另一个未委托工作")
        denied = self.request("post", f"/api/works/{other['id']}/deliveries",
                              json={"title": "不应跨客户", "members": self.members(artifact)}, headers={"Idempotency-Key": "cross-client"})
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        batch = self.enqueue(work, self.members(artifact))
        self.assertTrue(run_once(self.app))
        listing = self.request("get", f"/api/works/{work['id']}/deliveries").json["data"]
        self.assertEqual([item["id"] for item in listing["items"]], [batch["id"]])
        row = self.store.row("SELECT relative_path FROM delivery_batches WHERE id=?", (batch["id"],))
        path = self.root / row["relative_path"]
        path.write_bytes(b"altered outside the immutable delivery")
        download = self.request("get", f"/api/deliveries/{batch['id']}/file")
        self.assertEqual(download.status_code, 409)
        self.assertEqual(download.json["error"]["code"], "DELIVERY_INTEGRITY_ERROR")
        self.assertEqual(path.read_bytes(), b"altered outside the immutable delivery")

    def test_native_workbook_research_and_real_audio_share_one_frozen_delivery(self):
        import xlsxwriter
        from openpyxl import load_workbook

        work, document = self.prepare()
        source = io.BytesIO()
        book = xlsxwriter.Workbook(source, {"in_memory": True})
        sheet = book.add_worksheet("预算")
        sheet.write_number("A1", 100)
        sheet.write_number("A2", 20)
        sheet.write_formula("A3", "=SUM(A1:A2)", None, 120)
        book.close()
        uploaded = self.request("post", "/api/materials", data={"title": "预算原件", "work_id": work["id"], "file": (io.BytesIO(source.getvalue()), "预算.xlsx")}, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        material_id = uploaded.json["data"]["id"]
        inspected = self.request("get", f"/api/materials/{material_id}/spreadsheet").json["data"]
        cell = next(item for item in inspected["sheets"][0]["cells"] if item["address"] == "A1")
        revised = self.request("post", f"/api/materials/{material_id}/spreadsheet-revisions", json={"work_id": work["id"], "changes": [{"object_id": cell["object_id"], "before": {"kind": "number", "value": "100"}, "after": {"kind": "number", "value": "130"}}]}, headers={"Idempotency-Key": "mixed-budget"})
        self.assertEqual(revised.status_code, 201, revised.get_data(as_text=True))
        spreadsheet = revised.json["data"]["artifact"]
        report = self.create_text_material(work["id"], "确认口径", "本次预算合计150万元。")
        segment = report["segments"][0]
        quote = "本次预算合计150万元"
        research = self.request("post", f"/api/works/{work['id']}/research", json={"question": "本次确认预算是多少？", "material_ids": [report["id"]], "propositions": [{"statement": "本次预算为150万元。", "kind": "source_statement", "supporting_refs": [{"material_id": report["id"], "segment_id": segment["id"], "quote": quote, "source_start": 0, "source_end": len(quote)}]}]}, headers={"Idempotency-Key": "mixed-research"})
        self.assertEqual(research.status_code, 202, research.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("material_research",)))
        research_job = self.request("get", f"/api/jobs/{research.json['data']['id']}").json["data"]
        self.assertIsNotNone(research_job["artifact_id"], research_job)

        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"".join(struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * index / 16000))) for index in range(16000)))
        content = audio.getvalue()
        upload = self.request("post", "/api/uploads", json={"kind": "media", "title": "原声验收", "filename": "source.wav", "work_id": work["id"], "expected_size": len(content), "expected_sha256": hashlib.sha256(content).hexdigest()}, headers={"Idempotency-Key": "mixed-audio-upload"})
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        upload_id = upload.json["data"]["id"]
        chunk = self.request("patch", f"/api/uploads/{upload_id}", data=content, content_type="application/octet-stream", headers={"Upload-Offset": "0", "X-Chunk-SHA256": hashlib.sha256(content).hexdigest()})
        self.assertEqual(chunk.status_code, 204)
        saved = self.request("post", f"/api/uploads/{upload_id}/complete")
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        project = self.request("post", "/api/media/projects", json={"title": "成套节目母版", "kind": "podcast", "work_id": work["id"]}, headers={"Idempotency-Key": "mixed-project"})
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        bound = self.request("post", f"/api/media/projects/{project_id}/sources", json={"base_version": 1, "material_id": saved.json["data"]["material"]["id"]}, headers={"Idempotency-Key": "mixed-source"})
        self.assertEqual(bound.status_code, 200, bound.get_data(as_text=True))
        asset = bound.json["data"]["sources"][0]
        edited = self.request("post", f"/api/media/projects/{project_id}/edits", json={"base_version": 2, "operations": [{"op": "add_track", "track_id": "voice", "kind": "voiceover"}, {"op": "insert", "source_id": asset["asset_id"], "track_id": "voice", "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1}, "audio_role": "voiceover", "audio_stream_index": 0}]}, headers={"Idempotency-Key": "mixed-edit"})
        self.assertEqual(edited.status_code, 200, edited.get_data(as_text=True))
        render = self.request("post", f"/api/media/projects/{project_id}/renders", json={"version": edited.json["data"]["version"], "output_kind": "audio", "format": "wav", "output_spec": {"sample_rate": 16000, "audio_channels": 2}}, headers={"Idempotency-Key": "mixed-render"})
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))
        render_id = render.json["data"]["render_id"]
        media = self.request("get", f"/api/media/renders/{render_id}").json["data"]
        self.assertEqual(media["status"], "completed", media)
        batch = self.enqueue(work, [
            {"member_id": "budget", "kind": "artifact", "artifact_id": spreadsheet["id"], "version": 1, "format": "xlsx", "filename": "预算.xlsx"},
            {"member_id": "research", "kind": "artifact", "artifact_id": research_job["artifact_id"], "version": 1, "format": "md", "filename": "依据.md"},
            {"member_id": "audio", "kind": "media_render", "render_id": render_id, "filename": "节目母版.wav"},
        ], key="mixed-delivery")
        self.assertTrue(run_once(self.app, allowed_kinds=("delivery_batch",)))
        ready = self.read(batch)
        self.assertEqual(ready["status"], "completed", ready)
        package = self.request("get", ready["download_url"])
        self.assertEqual(package.status_code, 200, package.data[:100])
        with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
            workbook = load_workbook(io.BytesIO(archive.read("预算.xlsx")), data_only=True)
            self.assertEqual(workbook["预算"]["A3"].value, 150)
            workbook.close()
            self.assertIn("150万元", archive.read("依据.md").decode("utf-8"))
            with wave.open(io.BytesIO(archive.read("节目母版.wav")), "rb") as master:
                self.assertEqual(master.getframerate(), 16000)
                self.assertEqual(master.getnchannels(), 2)
                self.assertEqual(master.getnframes(), 16000)
        opened = self.request("get", f"/api/artifacts/{spreadsheet['id']}/spreadsheet").json["data"]["spreadsheet"]
        next_cell = next(item for item in opened["sheets"][0]["cells"] if item["address"] == "A1")
        revision = self.request("post", f"/api/works/{work['id']}/revision-batches", json={"title": "只改预算与节目音量", "members": [
            {"member_id": "budget", "target": {"kind": "artifact", "id": spreadsheet["id"], "base_version": 1},
             "command": {"changes": [{"object_id": next_cell["object_id"], "before": {"kind": "number", "value": "130"}, "after": {"kind": "number", "value": "140"}}]}},
            {"member_id": "document", "target": {"kind": "artifact", "id": document["id"], "base_version": 1},
             "command": {"title": document["title"], "body": "确认预算160万元。人工保留结尾。", "citations": []}},
            {"member_id": "audio", "target": {"kind": "media_project", "id": project_id, "base_version": edited.json["data"]["version"]},
             "command": {"operations": [{"op": "track_audio_routing", "track_id": "voice", "gain": 0.5}]}},
        ]}, headers={"Idempotency-Key": "mixed-revision"})
        self.assertEqual(revision.status_code, 202, revision.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("revision_batch",)))
        revised_batch = self.request("get", f"/api/revision-batches/{revision.json['data']['id']}").json["data"]
        self.assertEqual(revised_batch["status"], "completed", revised_batch)
        self.assertEqual([item["result_ref"]["version"] for item in revised_batch["members"]], [2, 2, 4])
        new_workbook = self.request("get", f"/api/artifacts/{spreadsheet['id']}/spreadsheet").json["data"]["spreadsheet"]
        total = next(item for item in new_workbook["sheets"][0]["cells"] if item["address"] == "A3")
        self.assertEqual(total["value"]["cached"]["value"], "160")
        project_view = self.request("get", f"/api/media/projects/{project_id}").json["data"]
        self.assertEqual(next(track for track in project_view["tracks"] if track["track_id"] == "voice")["gain"], 0.5)
        self.assertEqual(self.request("get", ready["download_url"]).data, package.data)
        latest_render = self.request("post", f"/api/media/projects/{project_id}/renders", json={"version": 4, "output_kind": "audio", "format": "wav", "output_spec": {"sample_rate": 16000, "audio_channels": 2}}, headers={"Idempotency-Key": "mixed-revised-render"})
        self.assertEqual(latest_render.status_code, 202, latest_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))
        latest_render_id = latest_render.json["data"]["render_id"]
        chapter = self.request("post", f"/api/media/projects/{project_id}/edits", json={"base_version": 4, "operations": [{"op": "add_chapter", "chapter_id": "opening", "title": "开场", "start_sec": {"num": 0, "den": 1}}]}, headers={"Idempotency-Key": "mixed-chapter"})
        self.assertEqual(chapter.status_code, 200, chapter.get_data(as_text=True))
        attachments = self.enqueue(work, [
            {"member_id": "master", "kind": "media_render", "render_id": latest_render_id, "filename": "节目母版.wav"},
            {"member_id": "chapters", "kind": "media_podcast_sidecars", "render_id": latest_render_id, "project_version": 5, "filename": "节目章节.zip"},
        ], key="mixed-sidecars")
        with patch("server.oevira.media_api.render_timeline", side_effect=AssertionError("Chapter delivery must reuse the confirmed audio")):
            self.assertTrue(run_once(self.app, allowed_kinds=("delivery_batch",)))
        attachments = self.read(attachments)
        self.assertEqual(attachments["status"], "completed", attachments)
        receipt = attachments["members"][1]
        self.assertEqual(receipt["reference"]["audio_project_version"], 4)
        self.assertEqual(receipt["reference"]["project_version"], 5)
        attachment_package = self.request("get", attachments["download_url"])
        with zipfile.ZipFile(io.BytesIO(attachment_package.data)) as archive:
            with zipfile.ZipFile(io.BytesIO(archive.read("节目章节.zip"))) as sidecars:
                import json

                chapters = json.loads(sidecars.read("chapters.json"))
                self.assertEqual(chapters["chapters"][0]["title"], "开场")
                self.assertEqual(chapters["chapters"][0]["startTime"], 0)
                manifest = json.loads(sidecars.read("sidecar-manifest.json"))
                self.assertEqual(manifest["audio_revision"]["project_version"], 4)
                self.assertEqual(manifest["sidecar_project_version"], 5)
        self.assertEqual(self.store.row("SELECT COUNT(*) n FROM media_renders WHERE project_id=?", (project_id,))["n"], 2)


if __name__ == "__main__":
    unittest.main()
