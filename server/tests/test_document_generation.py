from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
import pdfplumber

from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.document_generation import render_pdf, render_pptx, render_xlsx
from oevira.store import Store


BASE_URL = "http://127.0.0.1:8321"
BODY = """# Monthly sales

This saved report summarizes confirmed month-level values for review.
本月经营情况已按原始数据逐项核对。
이 보고서는 확인된 월별 수치를 함께 보여 줍니다.

| Month | Revenue | Units |
| --- | ---: | ---: |
| January | 120 | 3 |
| February | 180 | 4 |
| March | 150 | 5 |
"""


class GeneratedArtifactExportsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-generated-exports-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8321)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        capability = self.root / "launch.capability"
        issue_local_capability(self.store, capability)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability.read_text(encoding="ascii"), "display_name": "成果导出验证"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        kwargs.setdefault("headers", {})
        if "Authorization" not in kwargs["headers"] and hasattr(self, "token"):
            kwargs["headers"]["Authorization"] = f"Bearer {self.token}"
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def test_saved_artifact_exports_reopen_as_docx_pptx_xlsx_pdf_and_html(self) -> None:
        created = self.request(
            "post", "/api/artifacts",
            json={"title": "Monthly performance", "body": BODY, "citations": []},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        artifact_id = created.json["data"]["id"]
        version = self.store.row(
            "SELECT input_snapshot_json FROM artifact_versions WHERE artifact_id = ? AND version = 1",
            (artifact_id,),
        )
        snapshot = json.loads(version["input_snapshot_json"])
        # This typed calculation is already-confirmed source data; the renderer
        # validates and computes the bounded formula independently.
        snapshot["spreadsheet"] = {
            "version": 1,
            "formulas": [{"table_index": 1, "cell": "B5", "function": "SUM", "range": "B2:B4"}],
        }
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE artifact_versions SET input_snapshot_json = ? WHERE artifact_id = ? AND version = 1",
                (json.dumps(snapshot, ensure_ascii=False), artifact_id),
            )

        self.assertEqual(render_pptx("Monthly performance", BODY, [], snapshot),
                         render_pptx("Monthly performance", BODY, [], snapshot))
        self.assertEqual(render_xlsx("Monthly performance", BODY, [], snapshot),
                         render_xlsx("Monthly performance", BODY, [], snapshot))
        self.assertEqual(render_pdf("Monthly performance", BODY, [], snapshot),
                         render_pdf("Monthly performance", BODY, [], snapshot))

        outputs = {}
        mimetypes = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf",
            "html": "text/html",
        }
        for format_name in ("docx", "pptx", "xlsx", "pdf", "html"):
            export = self.request(
                "post", f"/api/artifacts/{artifact_id}/exports",
                json={"version": 1, "format": format_name},
                headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": f"generated-{format_name}-001"},
            )
            self.assertEqual(export.status_code, 201, export.get_data(as_text=True))
            download = self.request("get", export.json["data"]["download_url"])
            self.assertEqual(download.status_code, 200, download.data[:200])
            self.assertEqual(download.mimetype, mimetypes[format_name])
            outputs[format_name] = download.data

        word = Document(io.BytesIO(outputs["docx"]))
        word_text = "\n".join(p.text for p in word.paragraphs)
        self.assertIn("Monthly sales", word_text)
        self.assertEqual([cell.text for cell in word.tables[0].rows[0].cells], ["Month", "Revenue", "Units"])
        self.assertEqual(word.tables[0].cell(2, 1).text, "180")

        slides = Presentation(io.BytesIO(outputs["pptx"]))
        self.assertGreaterEqual(len(slides.slides), 3)
        slide_text = "\n".join(
            shape.text for slide in slides.slides for shape in slide.shapes if getattr(shape, "has_text_frame", False)
        )
        slide_tables = [shape.table for slide in slides.slides for shape in slide.shapes if getattr(shape, "has_table", False)]
        charts = [shape.chart for slide in slides.slides for shape in slide.shapes if getattr(shape, "has_chart", False)]
        self.assertIn("Monthly sales", slide_text)
        self.assertTrue(slide_tables)
        self.assertGreaterEqual(len(charts), 2)
        self.assertEqual(tuple(charts[0].series[0].values), (120.0, 180.0, 150.0))
        self.assertEqual(tuple(charts[0].plots[0].categories[i].label for i in range(3)),
                         ("January", "February", "March"))

        formula_book = load_workbook(io.BytesIO(outputs["xlsx"]), data_only=False)
        values_book = load_workbook(io.BytesIO(outputs["xlsx"]), data_only=True)
        table_sheet = next(sheet for sheet in formula_book.worksheets if sheet.title != "Overview")
        values_sheet = values_book[table_sheet.title]
        self.assertEqual(table_sheet["B2"].value, 120)
        self.assertEqual(table_sheet["B5"].value, "=SUM(B2:B4)")
        self.assertEqual(values_sheet["B5"].value, 450)
        self.assertGreaterEqual(len(table_sheet._charts), 2)
        formula_book.close()
        values_book.close()

        with pdfplumber.open(io.BytesIO(outputs["pdf"])) as pdf:
            extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
            self.assertGreaterEqual(len(pdf.pages), 1)
            self.assertIn("Monthly sales", extracted)
            self.assertIn("월별 수치", extracted)
            self.assertIn("本月经营情况", extracted)
            self.assertIn("February", extracted)
            self.assertIn("180", extracted)
            embedded_fonts = {character["fontname"] for page in pdf.pages for character in page.chars}
            self.assertTrue(any("NotoSansSC" in name for name in embedded_fonts))
            self.assertTrue(any("NotoSansKR" in name for name in embedded_fonts))
        self.assertIn(b"/FontFile2", outputs["pdf"])

        html = outputs["html"].decode("utf-8")
        self.assertIn("Monthly sales", html)
        self.assertIn("February", html)
        self.assertIn("table", html)

        # Removing one isolated export recovers the same immutable bytes on a
        # retry with the original request identity.
        pdf_export = self.store.row(
            "SELECT id, relative_path FROM exports WHERE artifact_id = ? AND format = 'pdf'",
            (artifact_id,),
        )
        pdf_path = self.root.joinpath(*Path(pdf_export["relative_path"]).parts)
        pdf_path.unlink()
        recovered = self.request(
            "post", f"/api/artifacts/{artifact_id}/exports",
            json={"version": 1, "format": "pdf"},
            headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": "generated-pdf-001"},
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        recovered_file = self.request("get", recovered.json["data"]["download_url"])
        self.assertEqual(recovered_file.status_code, 200, recovered_file.data[:200])
        self.assertEqual(recovered_file.data, outputs["pdf"])


if __name__ == "__main__":
    unittest.main()
