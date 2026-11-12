from __future__ import annotations

import hashlib
import io
from pathlib import Path
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
import uuid

from pptx import Presentation
from oevira.office import inspect_presentation
from oevira.office_api import render_presentation_export, validate_presentation_export
from oevira import create_app
from oevira.api import ApiError
from oevira.auth import _digest, issue_local_capability, utc_now
from oevira.store import Store
from test_office import _members, _pptx


BASE_URL = "http://127.0.0.1:8317"


def _eighty_slide_pptx() -> bytes:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    presentation = Presentation()
    for index in range(80):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = f"Original slide {index + 1}"
        slide.placeholders[1].text = f"Content on slide {index + 1} stays"
        slide.notes_slide.notes_text_frame.text = f"Speaker note {index + 1} stays"

    title = presentation.slides[2].shapes.title
    title.text_frame.clear()
    paragraph = title.text_frame.paragraphs[0]
    first = paragraph.add_run()
    first.text = "Original"
    first.font.bold = True
    second = paragraph.add_run()
    second.text = " slide 3"
    second.font.italic = True

    chart_data = CategoryChartData()
    chart_data.categories = ["A", "B"]
    chart_data.add_series("Original chart", (12, 18))
    presentation.slides[3].shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(1), Inches(2), Inches(4), Inches(3), chart_data,
    )
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


class PresentationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-office-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True)
        office_paths = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/api/materials/<material_id>/presentation", office_paths)
        self.assertIn("/api/artifacts/<artifact_id>/presentation-revisions", office_paths)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "演示文稿编辑者"},
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

    def create_work(self, *, title: str) -> dict:
        response = self.request(
            "post", "/api/works", json={"title": title, "goal": "修订客户选定的演示文稿对象", "brief": {}},
            headers=self.auth_headers(),
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

    def upload_presentation(self, content: bytes | None = None, *, work_id: str | None = None) -> tuple[dict, bytes]:
        original = content or _pptx()
        data = {"title": "客户方案", "file": (io.BytesIO(original), "客户方案.pptx")}
        if work_id:
            data["work_id"] = work_id
        response = self.request(
            "post", "/api/materials", data=data, headers=self.auth_headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"], original

    def change(self, inspected: dict, slide: int, text: str) -> dict:
        page = next(item for item in inspected["slides"] if item["number"] == slide)
        editable = next(item for item in page["objects"] if item["editable"] and item["text"])
        return {"slide": slide, "object_id": editable["id"], "text": text}

    def test_presentation_versions_preserve_source_support_restore_and_recovery(self) -> None:
        work = self.create_work(title="客户方案修改")
        material, original = self.upload_presentation(work_id=work["id"])
        source_hash = hashlib.sha256(original).hexdigest()

        material_inspection = self.request(
            "get", f"/api/materials/{material['id']}/presentation", headers=self.auth_headers(),
        )
        self.assertEqual(material_inspection.status_code, 200, material_inspection.get_data(as_text=True))
        original_manifest = {page["number"]: page for page in material_inspection.json["data"]["slides"]}
        self.assertEqual(set(original_manifest), {1, 2, 3, 4, 5, 6})
        first_changes = [
            self.change(material_inspection.json["data"], 1, "客户批准的第一页标题"),
            self.change(material_inspection.json["data"], 6, "客户批准的第六页标题"),
        ]
        created = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"work_id": work["id"], "allowed_slides": [1, 6], "changes": first_changes,
                  "revision_note": "只修订客户选定对象"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-initial-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        first = created.json["data"]
        artifact_id = first["artifact"]["id"]
        self.assertEqual(first["artifact"]["version"], 1)
        self.assertTrue(first["unchanged_verified"])
        self.assertEqual(len(first["diff"]), 2)

        snapshot = first["artifact"]["input_snapshot"]
        self.assertEqual(snapshot["kind"], "presentation")
        presentation = snapshot["presentation"]
        self.assertEqual(presentation["source"]["material_id"], material["id"])
        self.assertEqual(presentation["source"]["sha256"], source_hash)
        self.assertEqual(presentation["source"]["size_bytes"], len(original))
        self.assertEqual(presentation["base_file"]["kind"], "material")
        self.assertEqual(presentation["file"]["kind"], "export")
        self.assertEqual(presentation["file"]["format"], "pptx")
        self.assertEqual(presentation["file"]["sha256"], first["sha256"])
        self.assertEqual(presentation["file"]["size_bytes"], first["size_bytes"])
        first_export_id = presentation["file"]["export_id"]
        first_bytes = self.request("get", first["file"]["download_url"], headers=self.auth_headers())
        self.assertEqual(first_bytes.status_code, 200)
        self.assertEqual(first_bytes.mimetype, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertEqual(hashlib.sha256(first_bytes.data).hexdigest(), first["sha256"])
        first_open = inspect_presentation(first_bytes.data)
        self.assertIn("客户批准的第一页标题", [obj["text"] for obj in first_open["slides"][0]["objects"]])
        self.assertIn("客户批准的第六页标题", [obj["text"] for obj in first_open["slides"][5]["objects"]])

        original_download = self.request("get", f"/api/materials/{material['id']}/file", headers=self.auth_headers())
        self.assertEqual(original_download.status_code, 200)
        self.assertEqual(original_download.data, original)
        self.assertEqual(hashlib.sha256(original_download.data).hexdigest(), source_hash)

        # Generic text editing remains a separate summary/body revision. It does
        # not silently claim to have edited the presentation objects or file.
        text_revision = self.request(
            "post", f"/api/artifacts/{artifact_id}/versions",
            json={"base_version": 1, "title": "客户方案备注", "body": "人工备注，与幻灯片对象分开保存。",
                  "citations": [], "revision_note": "补充工作备注"},
            headers=self.auth_headers(),
        )
        self.assertEqual(text_revision.status_code, 200, text_revision.get_data(as_text=True))
        self.assertEqual(text_revision.json["data"]["version"], 2)
        self.assertEqual(text_revision.json["data"]["input_snapshot"]["kind"], "presentation")
        self.assertEqual(text_revision.json["data"]["input_snapshot"]["presentation"]["file"]["export_id"], first_export_id)
        same_presentation = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation?version=2", headers=self.auth_headers(),
        )
        self.assertEqual(same_presentation.status_code, 200, same_presentation.get_data(as_text=True))
        self.assertEqual(same_presentation.json["data"]["sha256"], first["sha256"])
        text_version_export = self.request(
            "post", f"/api/artifacts/{artifact_id}/exports",
            json={"version": 2, "format": "pptx"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-generic-export-v2"},
        )
        self.assertEqual(text_version_export.status_code, 201, text_version_export.get_data(as_text=True))
        second_export = text_version_export.json["data"]
        second_export_download = self.request("get", second_export["download_url"], headers=self.auth_headers())
        self.assertEqual(second_export_download.status_code, 200)
        self.assertEqual(second_export_download.data, first_bytes.data)
        second_export_row = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (second_export["id"],))
        second_export_path = self.root.joinpath(*Path(second_export_row["relative_path"]).parts)
        second_export_path.unlink()
        recovered_export = self.request("get", second_export["download_url"], headers=self.auth_headers())
        self.assertEqual(recovered_export.status_code, 200)
        self.assertEqual(recovered_export.data, first_bytes.data)
        repeated_export = self.request(
            "post", f"/api/artifacts/{artifact_id}/exports",
            json={"version": 2, "format": "pptx"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-generic-export-v2"},
        )
        self.assertEqual(repeated_export.status_code, 200)
        self.assertEqual(repeated_export.json["data"]["id"], second_export["id"])

        second_change = self.change(first_open, 6, "客户批准的第六页修订版")
        followup = self.request(
            "post", f"/api/artifacts/{artifact_id}/presentation-revisions",
            json={"base_version": 2, "allowed_slides": [6], "changes": [second_change],
                  "revision_note": "继续只修改第六页对象"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-followup-002"},
        )
        self.assertEqual(followup.status_code, 200, followup.get_data(as_text=True))
        second = followup.json["data"]
        self.assertEqual(second["artifact"]["version"], 3)
        second_snapshot = second["artifact"]["input_snapshot"]["presentation"]
        self.assertEqual(second_snapshot["source"]["sha256"], source_hash)
        self.assertEqual(second_snapshot["base_file"]["export_id"], first_export_id)
        second_version_row = self.store.row(
            "SELECT parent_version FROM artifact_versions WHERE artifact_id = ? AND version = 3",
            (artifact_id,),
        )
        self.assertEqual(second_version_row["parent_version"], 2)
        second_bytes = self.request("get", second["file"]["download_url"], headers=self.auth_headers())
        self.assertEqual(second_bytes.status_code, 200)
        self.assertEqual(hashlib.sha256(second_bytes.data).hexdigest(), second["sha256"])
        self.assertTrue(validate_presentation_export(second_bytes.data, self.store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = 3", (artifact_id,),
        )))
        version_three_row = self.store.row(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = 3", (artifact_id,),
        )
        with self.assertRaises(ApiError) as missing_actor:
            render_presentation_export(self.store, version_three_row)
        self.assertEqual(missing_actor.exception.code, "IDENTITY_REQUIRED")
        rendered = render_presentation_export(self.store, version_three_row, identity=self.identity)
        self.assertEqual(rendered, second_bytes.data)

        stale = self.request(
            "post", f"/api/artifacts/{artifact_id}/presentation-revisions",
            json={"base_version": 2, "allowed_slides": [1],
                  "changes": [self.change(first_open, 1, "不得覆盖更新版本")]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-stale-003"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "VERSION_CONFLICT")

        restored = self.request(
            "post", f"/api/artifacts/{artifact_id}/restore",
            json={"base_version": 3, "restore_version": 1}, headers=self.auth_headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(restored.json["data"]["version"], 4)
        restored_snapshot = restored.json["data"]["input_snapshot"]["presentation"]
        self.assertEqual(restored_snapshot["file"]["export_id"], first_export_id)
        restored_open = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation?version=4", headers=self.auth_headers(),
        )
        self.assertEqual(restored_open.status_code, 200, restored_open.get_data(as_text=True))
        self.assertEqual(restored_open.json["data"]["sha256"], first["sha256"])
        restored_export = self.request(
            "post", f"/api/artifacts/{artifact_id}/exports",
            json={"version": 4, "format": "pptx"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-restored-export-v4"},
        )
        self.assertEqual(restored_export.status_code, 201, restored_export.get_data(as_text=True))
        restored_export_file = self.request(
            "get", restored_export.json["data"]["download_url"], headers=self.auth_headers(),
        )
        self.assertEqual(restored_export_file.status_code, 200)
        self.assertEqual(restored_export_file.data, first_bytes.data)

        # A missing derived file can be regenerated from its immutable base and
        # recorded object diff. The stored output hash must match exactly.
        output_row = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (second["file"]["id"],))
        output_path = self.root.joinpath(*Path(output_row["relative_path"]).parts)
        output_path.unlink()
        reopened = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation?version=3", headers=self.auth_headers(),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["sha256"], second["sha256"])
        self.assertTrue(output_path.is_file())
        self.assertEqual(hashlib.sha256(output_path.read_bytes()).hexdigest(), second["sha256"])

    def test_eighty_slide_client_revision_reopens_only_selected_pages_and_keeps_native_parts(self) -> None:
        original = _eighty_slide_pptx()
        original_parts = _members(original)
        material, original_bytes = self.upload_presentation(original)
        self.assertEqual(original_bytes, original)

        inspected = self.request(
            "get", f"/api/materials/{material['id']}/presentation", headers=self.auth_headers(),
        )
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        source = inspected.json["data"]
        self.assertEqual(len(source["slides"]), 80)
        title_three = self.change(source, 3, "Unused whole-shape replacement")
        title_four = self.change(source, 4, "Revised slide 4")
        title_five = self.change(source, 5, "Revised slide 5")
        title_three = {
            "slide": 3,
            "object_id": title_three["object_id"],
            "before": "Original",
            "after": "Revised",
        }
        saved = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={
                "allowed_slides": [3, 4, 5],
                "changes": [title_three, title_four, title_five],
                "revision_note": "只改选定页标题，保留图表、备注和未选文字",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-eighty-slide-001"},
        )
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        revision = saved.json["data"]
        self.assertEqual(revision["artifact"]["version"], 1)
        self.assertTrue(revision["unchanged_verified"])
        self.assertEqual(len(revision["diff"]), 3)
        self.assertEqual(revision["diff"][0]["selection"], {"before": "Original", "after": "Revised"})

        downloaded = self.request("get", revision["file"]["download_url"], headers=self.auth_headers())
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(
            downloaded.mimetype,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        self.assertEqual(hashlib.sha256(downloaded.data).hexdigest(), revision["sha256"])
        output_parts = _members(downloaded.data)
        changed_parts = {name for name in original_parts if original_parts[name] != output_parts[name]}
        self.assertEqual(changed_parts, {
            "ppt/slides/slide3.xml", "ppt/slides/slide4.xml", "ppt/slides/slide5.xml",
        })
        self.assertEqual(set(output_parts), set(original_parts))
        self.assertTrue(all(
            original_parts[name] == output_parts[name]
            for name in original_parts if name not in changed_parts
        ))

        reopened = Presentation(io.BytesIO(downloaded.data))
        self.assertEqual(len(reopened.slides), 80)
        self.assertEqual(reopened.slides[2].shapes.title.text, "Revised slide 3")
        self.assertTrue(reopened.slides[2].shapes.title.text_frame.paragraphs[0].runs[0].font.bold)
        self.assertTrue(reopened.slides[2].shapes.title.text_frame.paragraphs[0].runs[1].font.italic)
        self.assertEqual(reopened.slides[3].shapes.title.text, "Revised slide 4")
        self.assertEqual(reopened.slides[4].shapes.title.text, "Revised slide 5")
        self.assertEqual(reopened.slides[2].notes_slide.notes_text_frame.text, "Speaker note 3 stays")
        self.assertEqual(reopened.slides[3].notes_slide.notes_text_frame.text, "Speaker note 4 stays")
        self.assertEqual(reopened.slides[79].notes_slide.notes_text_frame.text, "Speaker note 80 stays")
        chart_shapes = [shape for shape in reopened.slides[3].shapes if getattr(shape, "has_chart", False)]
        self.assertEqual(len(chart_shapes), 1)
        self.assertEqual(tuple(chart_shapes[0].chart.series[0].values), (12.0, 18.0))

        original_download = self.request(
            "get", f"/api/materials/{material['id']}/file", headers=self.auth_headers(),
        )
        self.assertEqual(original_download.status_code, 200)
        self.assertEqual(original_download.data, original)

        reopened_version = self.request(
            "get", f"/api/artifacts/{revision['artifact']['id']}/presentation?version=1",
            headers=self.auth_headers(),
        )
        self.assertEqual(reopened_version.status_code, 200, reopened_version.get_data(as_text=True))
        self.assertEqual(reopened_version.json["data"]["sha256"], revision["sha256"])

    def test_revision_is_scoped_idempotent_and_requires_material_object_changes(self) -> None:
        material, original = self.upload_presentation()
        inspected = self.request(
            "get", f"/api/materials/{material['id']}/presentation", headers=self.auth_headers(),
        )
        self.assertEqual(inspected.status_code, 200)
        first_change = self.change(inspected.json["data"], 1, "修订后标题")
        payload = {"allowed_slides": [1], "changes": [first_change]}

        missing_key = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json=payload, headers=self.auth_headers(),
        )
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json["error"]["code"], "IDEMPOTENCY_KEY_REQUIRED")

        duplicate_scope = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1, 1], "changes": [first_change]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-001"},
        )
        self.assertEqual(duplicate_scope.status_code, 400)

        created = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json=payload, headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        replay = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json=payload, headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-001"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["artifact"]["id"], created.json["data"]["artifact"]["id"])
        self.assertEqual(replay.json["data"]["artifact"]["version"], 1)

        conflicting = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [dict(first_change, text="另一种标题")]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-001"},
        )
        self.assertEqual(conflicting.status_code, 409)
        self.assertEqual(conflicting.json["error"]["code"], "IDEMPOTENCY_CONFLICT")

        not_selected = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [self.change(inspected.json["data"], 6, "范围外")]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-002"},
        )
        self.assertEqual(not_selected.status_code, 422)

        original_object = next(
            item for item in inspected.json["data"]["slides"][0]["objects"]
            if item["editable"] and item["text"]
        )
        no_material_change = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [self.change(inspected.json["data"], 1, original_object["text"])]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-scope-003"},
        )
        self.assertEqual(no_material_change.status_code, 400)
        self.assertEqual(no_material_change.json["error"]["code"], "NO_PRESENTATION_CHANGES")

        output = self.request("get", f"/api/materials/{material['id']}/file", headers=self.auth_headers())
        self.assertEqual(output.data, original)

        text_artifact = self.request(
            "post", "/api/artifacts", json={"title": "普通文稿", "body": "只有文本，没有PPTX原件。", "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(text_artifact.status_code, 201, text_artifact.get_data(as_text=True))
        generated_pptx = self.request(
            "post", f"/api/artifacts/{text_artifact.json['data']['id']}/exports",
            json={"version": 1, "format": "pptx"},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-text-content-001"},
        )
        self.assertEqual(generated_pptx.status_code, 201, generated_pptx.get_data(as_text=True))
        generated_file = self.request("get", generated_pptx.json["data"]["download_url"], headers=self.auth_headers())
        self.assertEqual(generated_file.status_code, 200)
        generated_deck = Presentation(io.BytesIO(generated_file.data))
        self.assertGreaterEqual(len(generated_deck.slides), 2)
        generated_text = "\n".join(
            shape.text for slide in generated_deck.slides for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
        )
        self.assertIn("只有文本，没有PPTX原件。", generated_text)

    def test_workspace_access_and_corrupt_files_fail_closed(self) -> None:
        material, _original = self.upload_presentation()
        inspected = self.request(
            "get", f"/api/materials/{material['id']}/presentation", headers=self.auth_headers(),
        )
        change = self.change(inspected.json["data"], 1, "仅当前空间可改")
        created = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [change]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-access-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        artifact_id = created.json["data"]["artifact"]["id"]
        file_ref = created.json["data"]["artifact"]["input_snapshot"]["presentation"]["file"]

        other_token, _other_workspace = self.create_other_workspace()
        other_headers = self.auth_headers(other_token)
        self.assertEqual(self.request("get", f"/api/materials/{material['id']}/presentation", headers=other_headers).status_code, 404)
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact_id}/presentation", headers=other_headers).status_code, 404)

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET role = 'viewer' WHERE workspace_id = ? AND user_id = ?",
                (self.identity["workspace"]["id"], self.identity["user"]["id"]),
            )
        denied = self.request(
            "post", f"/api/artifacts/{artifact_id}/presentation-revisions",
            json={"base_version": 1, "allowed_slides": [1], "changes": [change]},
            headers={**self.auth_headers(), "Idempotency-Key": "pptx-access-002"},
        )
        self.assertEqual(denied.status_code, 403)

        export_row = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (file_ref["export_id"],))
        output_path = self.root.joinpath(*Path(export_row["relative_path"]).parts)
        output_path.write_bytes(b"tampered payload")
        tampered = output_path.read_bytes()
        result = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation", headers=other_headers,
        )
        self.assertEqual(result.status_code, 404)
        # The authorized read sees a hash failure and the suspicious file is preserved.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET role = 'owner' WHERE workspace_id = ? AND user_id = ?",
                (self.identity["workspace"]["id"], self.identity["user"]["id"]),
            )
        integrity = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation", headers=self.auth_headers(),
        )
        self.assertEqual(integrity.status_code, 409)
        self.assertEqual(output_path.read_bytes(), tampered)
        self.assertEqual(
            self.store.row("SELECT status FROM exports WHERE id = ?", (file_ref["export_id"],))["status"],
            "interrupted",
        )


if __name__ == "__main__":
    unittest.main()
