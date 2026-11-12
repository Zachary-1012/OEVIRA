from __future__ import annotations

import hashlib
import io
from pathlib import Path
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
import uuid
import zipfile

from docx import Document

from oevira import create_app
from oevira.api import ApiError
from oevira.auth import _digest, issue_local_capability, utc_now
from oevira.store import Store
from test_documents import _docx, _docx_with_shared_images, _member_payloads, _png


BASE_URL = "http://127.0.0.1:8319"


class DocumentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-documents-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8319)
        self.app.config.update(TESTING=True)
        self.assertTrue(self.app.extensions.get("oevira.documents_api.registered"))
        route_paths = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/api/materials/<material_id>/document", route_paths)
        self.assertIn("/api/artifacts/<artifact_id>/document-revisions", route_paths)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "文稿编辑者"},
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

    def upload_document(self, content: bytes | None = None, *, work_id: str | None = None) -> tuple[dict, bytes]:
        original = content or _docx()
        form = {"title": "客户原稿", "file": (io.BytesIO(original), "客户原稿.docx")}
        if work_id is not None:
            form["work_id"] = work_id
        response = self.request(
            "post", "/api/materials",
            data=form,
            headers=self.auth_headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"], original

    def upload_image(self, filename: str, content: bytes, *, work_id: str | None = None) -> dict:
        form = {"title": filename, "file": (io.BytesIO(content), filename)}
        if work_id is not None:
            form["work_id"] = work_id
        response = self.request(
            "post", "/api/materials",
            data=form,
            headers=self.auth_headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_private_work(self, label: str, commission_id: str) -> str:
        work_id = str(uuid.uuid4())
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'{}','active',1,?,?)",
                (work_id, workspace_id, user_id, label, "测试同客户素材权限", now, now),
            )
            connection.execute(
                "INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)",
                (work_id, commission_id, now),
            )
        return work_id

    def create_commission(self, label: str) -> str:
        commission_id = str(uuid.uuid4())
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                (commission_id, workspace_id, label, user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'owner',1)",
                (commission_id, user_id),
            )
        return commission_id

    def create_other_workspace(self) -> tuple[str, str]:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "其他用户", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "其他空间", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, user_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now, expiry),
            )
        return token, workspace_id

    def test_original_word_local_revision_reopens_exports_and_recovers_by_version(self) -> None:
        material, original = self.upload_document()
        original_hash = hashlib.sha256(original).hexdigest()
        inspected_response = self.request(
            "get", f"/api/materials/{material['id']}/document", headers=self.auth_headers(),
        )
        self.assertEqual(inspected_response.status_code, 200, inspected_response.get_data(as_text=True))
        inspection = inspected_response.json["data"]
        self.assertEqual(inspection["sha256"], original_hash)
        selected_paragraph = next(item for item in inspection["objects"] if item["text"] == "前缀 客户 的原稿")
        selected_cell = next(item for item in inspection["objects"] if item["text"] == "待改表格单元")

        initial_request = {
            "revision_note": "只改客户选定的文字",
            "changes": [
                {"object_id": selected_paragraph["id"], "before": "客户", "after": "用户"},
                {"object_id": selected_cell["id"], "before": "待改", "after": "已改"},
            ],
        }
        initial = self.request(
            "post", f"/api/materials/{material['id']}/document-revisions",
            json=initial_request,
            headers={**self.auth_headers(), "Idempotency-Key": "docx-initial-001"},
        )
        self.assertEqual(initial.status_code, 201, initial.get_data(as_text=True))
        first = initial.json["data"]
        artifact_id = first["artifact"]["id"]
        self.assertEqual(first["artifact"]["version"], 1)
        self.assertEqual(first["document"]["source"]["sha256"], original_hash)
        self.assertTrue(first["document"]["unchanged_verified"])

        first_download = self.request("get", first["document"]["download_url"], headers=self.auth_headers())
        self.assertEqual(first_download.status_code, 200, first_download.data[:200])
        self.assertEqual(first_download.mimetype, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(hashlib.sha256(first_download.data).hexdigest(), first["document"]["sha256"])
        from oevira.documents_api import render_document_export

        version_row = self.store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = 1", (artifact_id,),
        )
        with self.assertRaises(ApiError) as missing_actor:
            render_document_export(self.store, version_row)
        self.assertEqual(missing_actor.exception.code, "IDENTITY_REQUIRED")
        self.assertEqual(render_document_export(self.store, version_row, identity=self.identity),
                         first_download.data)
        first_open = Document(io.BytesIO(first_download.data))
        self.assertEqual(first_open.paragraphs[0].text, "前缀 用户 的原稿")
        self.assertTrue(first_open.paragraphs[0].runs[1].italic)
        self.assertEqual(first_open.tables[0].cell(0, 0).text, "已改表格单元")
        self.assertEqual(first_open.tables[0].cell(0, 1).text, "未选表格单元")
        self.assertEqual(first_open.sections[0].header.paragraphs[0].text, "保留页眉")
        self.assertEqual(_member_payloads(first_download.data)["custom/preserved.bin"], b"custom package member")

        reopened = self.request("get", f"/api/artifacts/{artifact_id}/document", headers=self.auth_headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["artifact"]["version"], 1)
        live_objects = {item["id"]: item["text"] for item in reopened.json["data"]["document"]["objects"]}
        self.assertIn("前缀 用户 的原稿", live_objects.values())

        generic_revision = self.request(
            "post", f"/api/artifacts/{artifact_id}/versions",
            json={"base_version": 1, "title": "正文替代", "body": "不能替代Word对象", "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(generic_revision.status_code, 409)
        self.assertEqual(generic_revision.json["error"]["code"], "DOCUMENT_REVISION_REQUIRED")
        unchanged = self.request("get", f"/api/artifacts/{artifact_id}/document", headers=self.auth_headers())
        self.assertEqual(unchanged.status_code, 200, unchanged.get_data(as_text=True))
        self.assertEqual(unchanged.json["data"]["artifact"]["version"], 1)
        self.assertEqual(unchanged.json["data"]["document"]["sha256"], first["document"]["sha256"])

        updated_cell = next(item for item in reopened.json["data"]["document"]["objects"]
                            if item["text"] == "已改表格单元")
        second = self.request(
            "post", f"/api/artifacts/{artifact_id}/document-revisions",
            json={"base_version": 1, "revision_note": "继续修改表格单元", "changes": [
                {"object_id": updated_cell["id"], "before": "已改", "after": "复核"},
            ]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-followup-002"},
        )
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        second_data = second.json["data"]
        self.assertEqual(second_data["artifact"]["version"], 2)
        self.assertEqual(second_data["document"]["base_file"]["export_id"], first["document"]["export_id"])
        parent = self.store.row(
            "SELECT parent_version FROM artifact_versions WHERE artifact_id = ? AND version = 2",
            (artifact_id,),
        )
        self.assertEqual(parent["parent_version"], 1)

        second_download = self.request("get", second_data["document"]["download_url"], headers=self.auth_headers())
        self.assertEqual(second_download.status_code, 200, second_download.data[:200])
        second_open = Document(io.BytesIO(second_download.data))
        self.assertEqual(second_open.paragraphs[0].text, "前缀 用户 的原稿")
        self.assertEqual(second_open.tables[0].cell(0, 0).text, "复核表格单元")
        self.assertEqual(second_open.tables[0].cell(0, 1).text, "未选表格单元")
        self.assertEqual(_member_payloads(second_download.data)["custom/preserved.bin"], b"custom package member")

        restored = self.request(
            "post", f"/api/artifacts/{artifact_id}/restore",
            json={"base_version": 2, "restore_version": 1}, headers=self.auth_headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        restored_artifact = restored.json["data"]
        self.assertEqual(restored_artifact["version"], 3)
        self.assertEqual([item["version"] for item in restored_artifact["versions"]], [1, 2, 3])
        self.assertNotEqual(
            restored_artifact["input_snapshot"]["document"]["file"]["export_id"],
            first["document"]["export_id"],
        )
        restored_export_id = restored_artifact["input_snapshot"]["document"]["file"]["export_id"]
        restored_export = self.store.row(
            "SELECT version, status, sha256, size_bytes, relative_path FROM exports WHERE id = ? AND artifact_id = ?",
            (restored_export_id, artifact_id),
        )
        self.assertEqual(restored_export["version"], 3)
        self.assertEqual(restored_export["status"], "completed")
        self.assertEqual(restored_export["sha256"], first["document"]["sha256"])
        restore_row = self.store.row(
            "SELECT parent_version, restore_version FROM artifact_versions WHERE artifact_id = ? AND version = 3",
            (artifact_id,),
        )
        self.assertEqual(restore_row["parent_version"], 2)
        self.assertEqual(restore_row["restore_version"], 1)

        restored_reopen = self.request("get", f"/api/artifacts/{artifact_id}/document", headers=self.auth_headers())
        self.assertEqual(restored_reopen.status_code, 200, restored_reopen.get_data(as_text=True))
        restored_document = restored_reopen.json["data"]["document"]
        self.assertEqual(restored_document["version"], 3)
        self.assertEqual(restored_document["sha256"], first["document"]["sha256"])
        restored_download = self.request("get", restored_document["download_url"], headers=self.auth_headers())
        self.assertEqual(restored_download.status_code, 200, restored_download.data[:200])
        self.assertEqual(restored_download.data, first_download.data)
        self.assertEqual(Document(io.BytesIO(restored_download.data)).tables[0].cell(0, 0).text, "已改表格单元")

        # A fresh app/store instance must recover the restored file from its
        # parent export after the new version's export file is lost.
        restored_export_path = self.root.joinpath(*Path(restored_export["relative_path"]).parts)
        restored_export_path.unlink()
        cold_app = create_app(self.root, expected_port=8319)
        cold_app.config.update(TESTING=True)
        cold_client = cold_app.test_client()
        cold_reopen = cold_client.get(
            f"/api/artifacts/{artifact_id}/document?version=3",
            base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(cold_reopen.status_code, 200, cold_reopen.get_data(as_text=True))
        cold_document = cold_reopen.json["data"]["document"]
        cold_reopen.close()
        cold_download = cold_client.get(cold_document["download_url"], base_url=BASE_URL, headers=self.auth_headers())
        cold_download.get_data()
        self.assertEqual(cold_download.status_code, 200, cold_download.data[:200])
        self.assertEqual(cold_download.data, first_download.data)
        self.assertEqual(Document(io.BytesIO(cold_download.data)).paragraphs[0].text, "前缀 用户 的原稿")
        cold_download.close()
        restored_export = self.store.row("SELECT status FROM exports WHERE id = ?", (restored_export_id,))
        self.assertEqual(restored_export["status"], "completed")

        stale = self.request(
            "post", f"/api/artifacts/{artifact_id}/document-revisions",
            json={"base_version": 2, "changes": [
                {"object_id": selected_paragraph["id"], "before": "用户", "after": "不得覆盖"},
            ]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-stale-003"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(stale.json["error"]["details"]["current"]["version"], 3)

        prior = self.request("get", f"/api/artifacts/{artifact_id}/document?version=1", headers=self.auth_headers())
        self.assertEqual(prior.status_code, 200, prior.get_data(as_text=True))
        self.assertEqual(prior.json["data"]["document"]["sha256"], first["document"]["sha256"])

        replay = self.request(
            "post", f"/api/materials/{material['id']}/document-revisions",
            json=initial_request,
            headers={**self.auth_headers(), "Idempotency-Key": "docx-initial-001"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["artifact"]["id"], artifact_id)
        self.assertEqual(replay.json["data"]["artifact"]["version"], 1)

        export = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (second_data["document"]["export_id"],))
        export_path = self.root.joinpath(*Path(export["relative_path"]).parts)
        export_path.unlink()
        recovered_download = self.request("get", second_data["document"]["download_url"], headers=self.auth_headers())
        self.assertEqual(recovered_download.status_code, 200, recovered_download.data[:200])
        self.assertEqual(recovered_download.data, second_download.data)
        self.assertEqual(Document(io.BytesIO(recovered_download.data)).tables[0].cell(0, 0).text, "复核表格单元")
        recovered_export = self.store.row("SELECT status, sha256 FROM exports WHERE id = ?", (second_data["document"]["export_id"],))
        self.assertEqual(recovered_export["status"], "completed")
        self.assertEqual(recovered_export["sha256"], second_data["document"]["sha256"])

        original_download = self.request("get", f"/api/materials/{material['id']}/file", headers=self.auth_headers())
        self.assertEqual(original_download.status_code, 200)
        self.assertEqual(original_download.data, original)

    def test_document_objects_and_files_are_hidden_across_workspaces(self) -> None:
        material, _original = self.upload_document()
        inspected = self.request("get", f"/api/materials/{material['id']}/document", headers=self.auth_headers())
        paragraph = next(item for item in inspected.json["data"]["objects"] if item["text"] == "前缀 客户 的原稿")
        saved = self.request(
            "post", f"/api/materials/{material['id']}/document-revisions",
            json={"changes": [{"object_id": paragraph["id"], "before": "客户", "after": "用户"}]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-private-001"},
        )
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        artifact_id = saved.json["data"]["artifact"]["id"]
        export_id = saved.json["data"]["document"]["export_id"]
        other_token, _workspace_id = self.create_other_workspace()

        for path in (
            f"/api/materials/{material['id']}/document",
            f"/api/artifacts/{artifact_id}/document",
            f"/api/document-exports/{export_id}/file",
        ):
            response = self.request("get", path, headers=self.auth_headers(other_token))
            self.assertEqual(response.status_code, 404, path)

        rejected = self.request(
            "post", f"/api/artifacts/{artifact_id}/document-revisions",
            json={"base_version": 1, "changes": [{"object_id": paragraph["id"], "before": "用户", "after": "其他"}]},
            headers={**self.auth_headers(other_token), "Idempotency-Key": "docx-cross-workspace-001"},
        )
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(rejected.json["error"]["code"], "NOT_FOUND")

    def test_header_footnote_and_comment_revisions_are_cold_recoverable(self) -> None:
        material, original = self.upload_document()
        inspected = self.request("get", f"/api/materials/{material['id']}/document",
                                  headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        header = next(item for item in inspected.json["data"]["objects"]
                      if item["kind"] == "header_paragraph" and item["text"] == "保留页眉")
        footnote = next(item for item in inspected.json["data"]["objects"]
                        if item["kind"] == "footnote_paragraph" and item["text"] == "脚注原文")
        comment = next(item for item in inspected.json["data"]["objects"]
                       if item["kind"] == "comment_paragraph" and item["text"] == "评论原文")
        revision = next(item for item in inspected.json["data"]["objects"]
                        if item["kind"] == "tracked_change"
                        and item["location"]["change_type"] == "insertion")
        self.assertEqual(header["location"]["part"], "word/header1.xml")
        self.assertEqual(header["location"]["used_by"], ["section[1].header.default"])

        created = self.request(
            "post", f"/api/materials/{material['id']}/document-revisions",
            json={"changes": [
                {"object_id": header["id"], "before": "保留", "after": "新版"},
                {"object_id": footnote["id"], "before": "脚注", "after": "来源注"},
                {"object_id": comment["id"], "before": "评论", "after": "评审"},
                {"object_id": revision["id"], "operation": "accept"},
            ]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-header-revision-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        artifact_id = created.json["data"]["artifact"]["id"]
        self.assertEqual({item["part"] for item in created.json["data"]["document"]["changes"]},
                         {"word/header1.xml", "word/footnotes.xml", "word/comments.xml",
                          "word/document.xml"})
        first_download = self.request("get", created.json["data"]["document"]["download_url"],
                                      headers=self.auth_headers())
        self.assertEqual(first_download.status_code, 200)
        reopened = Document(io.BytesIO(first_download.data))
        self.assertEqual(reopened.sections[0].header.paragraphs[0].text, "新版页眉")
        self.assertEqual(reopened.sections[0].header.paragraphs[1].text, "未选页眉段")
        self.assertEqual(reopened.sections[0].footer.paragraphs[0].text, "保留页脚")
        self.assertEqual(reopened.paragraphs[2].text, "正文前新增文字正文中正文后")
        from lxml import etree

        exported_parts = _member_payloads(first_download.data)
        footnotes = etree.fromstring(exported_parts["word/footnotes.xml"])
        note_text = footnotes.xpath(".//w:footnote[@w:id='1']/w:p/w:r/w:t", namespaces={
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        })[0]
        self.assertEqual(note_text.text, "来源注原文")
        comments = etree.fromstring(exported_parts["word/comments.xml"])
        comment_text = comments.xpath(".//w:comment[@w:id='7']/w:p/w:r/w:t/text()", namespaces={
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        })
        self.assertEqual("".join(comment_text), "评审原文")

        export = self.store.row("SELECT relative_path FROM exports WHERE artifact_id = ? AND version = 1 AND format = 'docx'",
                                (artifact_id,))
        (self.root / export["relative_path"]).unlink()
        cold_app = create_app(self.root, expected_port=8319)
        cold_app.config.update(TESTING=True)
        cold_client = cold_app.test_client()
        recovered = cold_client.get(
            f"/api/artifacts/{artifact_id}/document?version=1", base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        cold_document = recovered.json["data"]["document"]
        recovered.close()
        download = cold_client.get(cold_document["download_url"], base_url=BASE_URL,
                                   headers=self.auth_headers())
        download.get_data()
        self.assertEqual(download.status_code, 200)
        recovered_open = Document(io.BytesIO(download.data))
        self.assertEqual(recovered_open.sections[0].header.paragraphs[0].text, "新版页眉")
        self.assertEqual(recovered_open.sections[0].footer.paragraphs[0].text, "保留页脚")
        self.assertEqual(recovered_open.paragraphs[2].text, "正文前新增文字正文中正文后")
        recovered_parts = _member_payloads(download.data)
        recovered_footnotes = etree.fromstring(recovered_parts["word/footnotes.xml"])
        recovered_note_text = recovered_footnotes.xpath(
            ".//w:footnote[@w:id='1']/w:p/w:r/w:t",
            namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )[0]
        self.assertEqual(recovered_note_text.text, "来源注原文")
        recovered_comments = etree.fromstring(recovered_parts["word/comments.xml"])
        recovered_comment_text = recovered_comments.xpath(
            ".//w:comment[@w:id='7']/w:p/w:r/w:t/text()",
            namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )
        self.assertEqual("".join(recovered_comment_text), "评审原文")
        original_download = self.request("get", f"/api/materials/{material['id']}/file",
                                         headers=self.auth_headers())
        self.assertEqual(original_download.data, original)
        download.close()

    def test_word_image_material_replacement_reopens_versions_and_cold_recovers(self) -> None:
        original_image = _png((230, 20, 20))
        first_replacement = _png((20, 220, 30), (80, 48))
        second_replacement = _png((20, 30, 230), (64, 64))
        original_document = _docx_with_shared_images(original_image)
        document_material, _ = self.upload_document(original_document)
        first_asset = self.upload_image("first-artwork.png", first_replacement)
        second_asset = self.upload_image("second-artwork.png", second_replacement)

        inspected = self.request("get", f"/api/materials/{document_material['id']}/document",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        images = [item for item in inspected.json["data"]["objects"] if item["kind"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(item["editable"] for item in images))
        self.assertEqual(images[0]["media"]["sha256"], images[1]["media"]["sha256"])

        created = self.request(
            "post", f"/api/materials/{document_material['id']}/document-revisions",
            json={"changes": [{
                "object_id": images[0]["id"], "image_material_id": first_asset["id"],
                "alt_text": "First replacement",
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-image-initial-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        artifact_id = created.json["data"]["artifact"]["id"]
        material_ids = {item["id"] for item in created.json["data"]["artifact"]["input_snapshot"]["materials"]}
        self.assertIn(first_asset["id"], material_ids)

        first_page = self.request("get", f"/api/artifacts/{artifact_id}/document?version=1",
                                  headers=self.auth_headers())
        self.assertEqual(first_page.status_code, 200, first_page.get_data(as_text=True))
        first_images = [item for item in first_page.json["data"]["document"]["objects"]
                        if item["kind"] == "image"]
        self.assertEqual(first_images[0]["media"]["sha256"], hashlib.sha256(first_replacement).hexdigest())
        self.assertEqual(first_images[0]["text"], "First replacement")
        self.assertEqual(first_images[1]["media"]["sha256"], hashlib.sha256(original_image).hexdigest())

        handler = self.app.extensions["oevira.revision_handlers"]["document"]
        second = handler(self.store, self.identity, artifact_id, {
            "base_version": 1,
            "changes": [{"object_id": first_images[1]["id"],
                         "image_material_id": second_asset["id"]}],
        }, "docx-image-pure-followup-002")
        self.assertEqual(second["artifact"]["version"], 2)
        followup_material_ids = {item["id"] for item in
                                 second["artifact"]["input_snapshot"]["materials"]}
        self.assertIn(first_asset["id"], followup_material_ids)
        self.assertIn(second_asset["id"], followup_material_ids)
        export = self.store.row(
            "SELECT relative_path FROM exports WHERE artifact_id = ? AND version = 2 AND format = 'docx'",
            (artifact_id,),
        )
        (self.root / export["relative_path"]).unlink()

        cold_app = create_app(self.root, expected_port=8319)
        cold_app.config.update(TESTING=True)
        cold_client = cold_app.test_client()
        recovered = cold_client.get(
            f"/api/artifacts/{artifact_id}/document?version=2", base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        download = cold_client.get(recovered.json["data"]["document"]["download_url"],
                                   base_url=BASE_URL, headers=self.auth_headers())
        download.get_data()
        recovered.close()
        self.assertEqual(download.status_code, 200)
        reopened = Document(io.BytesIO(download.data))
        image_blobs = [reopened.part.related_parts[shape._inline.graphic.graphicData.pic.blipFill.blip.embed].blob
                       for shape in reopened.inline_shapes]
        self.assertEqual(image_blobs, [first_replacement, second_replacement])
        for image_id, expected in ((first_asset["id"], first_replacement),
                                   (second_asset["id"], second_replacement)):
            source_download = self.request("get", f"/api/materials/{image_id}/file",
                                           headers=self.auth_headers())
            self.assertEqual(source_download.data, expected)
        original_download = self.request("get", f"/api/materials/{document_material['id']}/file",
                                         headers=self.auth_headers())
        self.assertEqual(original_download.data, original_document)
        download.close()

    def test_nested_word_material_access_rechecks_client_scope_target_work_and_revocation(self) -> None:
        client_a = self.create_commission("客户甲")
        work_a1 = self.create_private_work("甲客户工作一", client_a)
        work_a2 = self.create_private_work("甲客户工作二", client_a)
        client_b = self.create_commission("客户乙")
        work_b = self.create_private_work("乙客户工作", client_b)

        original_image = _png((210, 10, 10))
        same_client_image_bytes = _png((10, 210, 10))
        other_client_image_bytes = _png((10, 10, 210))
        document, _ = self.upload_document(_docx_with_shared_images(original_image), work_id=work_a1)
        same_client_image = self.upload_image("same-client.png", same_client_image_bytes, work_id=work_a2)
        other_client_image = self.upload_image("other-client.png", other_client_image_bytes, work_id=work_b)

        inspected = self.request("get", f"/api/materials/{document['id']}/document",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        target_image = next(item for item in inspected.json["data"]["objects"] if item["kind"] == "image")
        cross_client = self.request(
            "post", f"/api/materials/{document['id']}/document-revisions",
            json={"changes": [{"object_id": target_image["id"],
                                "image_material_id": other_client_image["id"]}]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-scope-cross-client-001"},
        )
        self.assertEqual(cross_client.status_code, 403, cross_client.get_data(as_text=True))
        self.assertEqual(cross_client.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

        same_client = self.request(
            "post", f"/api/materials/{document['id']}/document-revisions",
            json={"changes": [{"object_id": target_image["id"],
                                "image_material_id": same_client_image["id"]}]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-scope-same-client-002"},
        )
        self.assertEqual(same_client.status_code, 201, same_client.get_data(as_text=True))
        artifact_id = same_client.json["data"]["artifact"]["id"]
        self.assertEqual(same_client.json["data"]["artifact"]["work_id"], work_a1)
        shared_export_id = same_client.json["data"]["document"]["export_id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET work_id=? WHERE id=?",
                               (work_b, same_client_image["id"]))
        cached_export = self.request("get", f"/api/exports/{shared_export_id}/file",
                                     headers=self.auth_headers())
        self.assertEqual(cached_export.status_code, 403, cached_export.get_data(as_text=True))
        self.assertEqual(cached_export.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET work_id=? WHERE id=?",
                               (work_a2, same_client_image["id"]))

        unscoped_material, _ = self.upload_document(_docx_with_shared_images(original_image))
        unscoped_asset = self.upload_image("unscoped-image.png", same_client_image_bytes)
        unscoped_inspection = self.request(
            "get", f"/api/materials/{unscoped_material['id']}/document",
            headers=self.auth_headers(),
        )
        unscoped_image = next(item for item in unscoped_inspection.json["data"]["objects"]
                              if item["kind"] == "image")
        unscoped_artifact = self.request(
            "post", f"/api/materials/{unscoped_material['id']}/document-revisions",
            json={"changes": [{"object_id": unscoped_image["id"],
                                "image_material_id": unscoped_asset["id"]}]},
            headers={**self.auth_headers(), "Idempotency-Key": "docx-scope-unscoped-003"},
        )
        self.assertEqual(unscoped_artifact.status_code, 201, unscoped_artifact.get_data(as_text=True))
        unscoped_id = unscoped_artifact.json["data"]["artifact"]["id"]
        self.assertIsNone(unscoped_artifact.json["data"]["artifact"]["work_id"])
        unscoped_revision_image = next(
            item for item in unscoped_artifact.json["data"]["document"]["objects"]
            if item["kind"] == "image"
        )
        handler = self.app.extensions["oevira.revision_handlers"]["document"]
        with self.assertRaises(ApiError) as no_scope:
            handler(self.store, self.identity, unscoped_id, {
                "base_version": 1,
                "changes": [{"object_id": unscoped_revision_image["id"],
                             "image_material_id": other_client_image["id"]}],
            }, "docx-scope-private-to-no-work-004")
        self.assertEqual(no_scope.exception.code, "CLIENT_SCOPE_MISMATCH")

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE commission_members SET active=0 WHERE commission_id=? AND user_id=?",
                (client_a, self.identity["user"]["id"]),
            )
        current_target_image = next(
            item for item in same_client.json["data"]["document"]["objects"]
            if item["kind"] == "image"
        )
        with self.assertRaises(ApiError) as revoked:
            handler(self.store, self.identity, artifact_id, {
                "base_version": 1,
                "changes": [{"object_id": current_target_image["id"],
                             "image_material_id": same_client_image["id"]}],
            }, "docx-scope-revoked-actor-005")
        self.assertEqual(revoked.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
