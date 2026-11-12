from __future__ import annotations

import ctypes
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

import pypdfium2 as pdfium
from pypdfium2 import raw
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from oevira.office_pdf import PdfRevisionError, inspect_pdf, revise_pdf
from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.store import Store


BASE_URL = "http://127.0.0.1:8322"


def _pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792))
    document.drawString(72, 720, "Budget revenue increased.")
    document.drawString(72, 690, "A repeated note.")
    document.drawString(72, 660, "A repeated note.")
    document.showPage()
    document.setPageRotation(90)
    document.drawString(100, 600, "Rotated crop anchor")
    document.showPage()
    document.save()
    return output.getvalue()


def _add_existing_comment(content: bytes) -> bytes:
    document = pdfium.PdfDocument(content)
    page = document.get_page(0)
    textpage = page.get_textpage()
    searcher = textpage.search("A repeated note.", match_case=True)
    start, count = searcher.get_next()
    boxes = [textpage.get_charbox(index) for index in range(start, start + count)]
    left = min(box[0] for box in boxes)
    bottom = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    top = max(box[3] for box in boxes)
    annotation = raw.FPDFPage_CreateAnnot(page.raw, raw.FPDF_ANNOT_HIGHLIGHT)
    rectangle = raw.FS_RECTF(left, top, right, bottom)
    assert raw.FPDFAnnot_SetRect(annotation, ctypes.byref(rectangle))
    quad = raw.FS_QUADPOINTSF(left, top, right, top, left, bottom, right, bottom)
    assert raw.FPDFAnnot_AppendAttachmentPoints(annotation, ctypes.byref(quad))
    key = ctypes.create_string_buffer(b"Contents\0")
    value_bytes = "existing comment".encode("utf-16-le") + b"\0\0"
    value = (ctypes.c_ushort * (len(value_bytes) // 2)).from_buffer_copy(value_bytes)
    assert raw.FPDFAnnot_SetStringValue(annotation, key,
                                        ctypes.cast(value, ctypes.POINTER(ctypes.c_ushort)))
    raw.FPDFPage_CloseAnnot(annotation)
    textpage.close()
    page.close()
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _scanned_pdf() -> bytes:
    image_bytes = io.BytesIO()
    Image.new("RGB", (64, 64), color=(190, 190, 190)).save(image_bytes, format="PNG")
    image_bytes.seek(0)
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792))
    document.drawImage(ImageReader(image_bytes), 100, 500, width=128, height=128)
    document.showPage()
    document.save()
    return output.getvalue()


def _crop_rotated_pdf(content: bytes) -> bytes:
    document = pdfium.PdfDocument(content)
    page = document.get_page(1)
    page.set_cropbox(48, 54, 520, 728)
    page.set_rotation(90)
    page.close()
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def _docmdp_pdf(permission: int) -> bytes:
    content_stream = b"BT /F1 12 Tf 72 720 Td (Signed anchor) Tj ET"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R /AcroForm 6 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R /Annots [7 0 R] >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: b"<< /Length " + str(len(content_stream)).encode() + b" >>\nstream\n" + content_stream + b"\nendstream",
        6: b"<< /SigFlags 3 /Fields [7 0 R] >>",
        7: b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (Sig1) /Rect [0 0 0 0] /F 4 /P 3 0 R /V 8 0 R >>",
        8: b"<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached /ByteRange [0000000000 0000000000 0000000000 0000000000] /Contents <" + b"00" * 64 + b"> /Reference [9 0 R] >>",
        9: b"<< /Type /SigRef /TransformMethod /DocMDP /TransformParams 10 0 R >>",
        10: f"<< /Type /TransformParams /P {permission} /V /1.2 >>".encode(),
    }
    parts = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets = {}
    for number, body in objects.items():
        offsets[number] = sum(map(len, parts))
        parts.extend((f"{number} 0 obj\n".encode(), body, b"\nendobj\n"))
    xref_offset = sum(map(len, parts))
    parts.append(b"xref\n0 11\n0000000000 65535 f \n")
    for number in range(1, 11):
        parts.append(f"{offsets[number]:010d} 00000 n \n".encode())
    parts.append(b"trailer\n<< /Size 11 /Root 1 0 R /ID [<11111111111111111111111111111111><22222222222222222222222222222222>] >>\nstartxref\n"
                 + str(xref_offset).encode() + b"\n%%EOF\n")
    result = b"".join(parts)
    contents_open = result.index(b"/Contents <") + len(b"/Contents ")
    contents_close = result.index(b">", contents_open) + 1
    byte_range = f"[0000000000 {contents_open:010d} {contents_close:010d} {len(result) - contents_close:010d}]".encode()
    return result.replace(b"[0000000000 0000000000 0000000000 0000000000]", byte_range)


class PdfOriginalRevisionTests(unittest.TestCase):
    def test_text_anchored_annotation_preserves_pages_rotation_crop_existing_comments_and_source(self) -> None:
        original = _add_existing_comment(_pdf())
        original_hash = hashlib.sha256(original).hexdigest()
        inspected = inspect_pdf(original)
        self.assertEqual(inspected["page_count"], 2)
        self.assertEqual(inspected["pages"][1]["rotation"], 90)
        self.assertEqual(inspected["pages"][0]["annotations"][0]["contents"], "existing comment")
        self.assertIn("Budget revenue increased.", inspected["pages"][0]["text"])

        changes = [
            {"page": 1, "quote": "Budget revenue increased.", "comment": "Please verify the cited period."},
            {"page": 2, "quote": "Rotated crop anchor", "comment": "Keep the source rotation."},
        ]
        first = revise_pdf(original, changes, author="Reviewer", annotated_at="D:20261110120000Z")
        second = revise_pdf(original, changes, author="Reviewer", annotated_at="D:20261110120000Z")
        self.assertEqual(first["content"], second["content"])
        self.assertTrue(first["content"].startswith(original))
        self.assertTrue(first["unchanged_verified"])
        self.assertEqual(len(first["changes"]), 2)
        self.assertEqual(hashlib.sha256(original).hexdigest(), original_hash)

        result = inspect_pdf(first["content"])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["pages"][0]["rotation"], 0)
        self.assertEqual(result["pages"][1]["rotation"], 90)
        self.assertEqual(result["pages"][0]["text"], inspected["pages"][0]["text"])
        self.assertEqual(result["pages"][1]["text"], inspected["pages"][1]["text"])
        self.assertEqual(result["pages"][0]["annotations"][0]["contents"], "existing comment")
        self.assertEqual(result["pages"][0]["annotations"][1]["contents"], "Please verify the cited period.")
        self.assertEqual(result["pages"][1]["annotations"][0]["contents"], "Keep the source rotation.")
        for old_page, new_page in zip(inspected["pages"], result["pages"]):
            self.assertEqual(old_page["bbox"], new_page["bbox"])
            self.assertEqual(old_page["mediabox"], new_page["mediabox"])
            self.assertEqual(old_page["cropbox"], new_page["cropbox"])

    def test_repeated_text_requires_an_occurrence_and_original_remains_unmodified(self) -> None:
        original = _pdf()
        digest = hashlib.sha256(original).hexdigest()
        with self.assertRaises(PdfRevisionError) as ambiguous:
            revise_pdf(original, [{"page": 1, "quote": "A repeated note.", "comment": "Review"}],
                       author="Reviewer", annotated_at="D:20261110120000Z")
        self.assertEqual(ambiguous.exception.code, "PDF_ANCHOR_AMBIGUOUS")
        self.assertEqual(ambiguous.exception.details["occurrences"], 2)
        result = revise_pdf(original, [{"page": 1, "quote": "A repeated note.", "occurrence": 1,
                                        "comment": "This occurrence only."}],
                            author="Reviewer", annotated_at="D:20261110120000Z")
        self.assertEqual(hashlib.sha256(original).hexdigest(), digest)
        output = inspect_pdf(result["content"])
        comments = [item["contents"] for item in output["pages"][0]["annotations"]]
        self.assertEqual(comments, ["This occurrence only."])

    def test_scan_page_rect_and_rotated_cropped_page_quad_use_original_pdf_coordinates(self) -> None:
        scan = _scanned_pdf()
        scan_info = inspect_pdf(scan)
        self.assertFalse(scan_info["pages"][0]["text_available"])
        bbox = scan_info["pages"][0]["bbox"]
        rect = [bbox[0] + 120, bbox[1] + 520, bbox[0] + 170, bbox[1] + 570]
        scan_revision = revise_pdf(scan, [{"page": 1, "rect": rect,
                                           "comment": "Review this scanned region."}],
                                   author="Reviewer", annotated_at="D:20261110120000Z")
        scan_result = inspect_pdf(scan_revision["content"])
        self.assertFalse(scan_result["pages"][0]["text_available"])
        self.assertEqual(scan_result["pages"][0]["annotations"][0]["contents"],
                         "Review this scanned region.")
        self.assertTrue(scan_revision["content"].startswith(scan))

        cropped = _crop_rotated_pdf(_pdf())
        cropped_info = inspect_pdf(cropped)
        page = cropped_info["pages"][1]
        self.assertEqual(page["rotation"], 90)
        self.assertNotEqual(page["bbox"], page["mediabox"])
        left, bottom, right, top = page["bbox"]
        quad = [[left + 10, top - 10, left + 50, top - 10,
                 left + 10, top - 30, left + 50, top - 30]]
        revision = revise_pdf(cropped, [{"page": 2, "quads": quad,
                                         "comment": "Precise rotated crop selection."}],
                              author="Reviewer", annotated_at="D:20261110120000Z")
        result = inspect_pdf(revision["content"])
        self.assertEqual(result["pages"][1]["rotation"], 90)
        self.assertEqual(result["pages"][1]["cropbox"], page["cropbox"])
        self.assertEqual(result["pages"][1]["annotations"][0]["contents"],
                         "Precise rotated crop selection.")

    def test_docmdp_permissions_restrict_changes_and_p3_status_never_claims_signature_validation(self) -> None:
        for permission in (1, 2):
            signed = _docmdp_pdf(permission)
            with self.assertRaises(PdfRevisionError) as denied:
                revise_pdf(signed, [{"page": 1, "quote": "Signed anchor", "comment": "Review"}],
                           author="Reviewer", annotated_at="D:20261110120000Z")
            self.assertEqual(denied.exception.code, "PDF_DOCMDP_CHANGE_FORBIDDEN")
            self.assertEqual(denied.exception.details["permission"], permission)

        signed = _docmdp_pdf(3)
        result = revise_pdf(signed, [{"page": 1, "quote": "Signed anchor", "comment": "Review"}],
                            author="Reviewer", annotated_at="D:20261110120000Z")
        self.assertTrue(result["signature_status"]["original_signed_bytes_preserved"])
        self.assertTrue(result["signature_status"]["modified_after_signature"])
        self.assertEqual(result["signature_status"]["cryptographic_validation"], "not_performed")
        self.assertEqual(result["signature_status"]["signatures"][0]["docmdp_permission"], 3)


class PdfApiRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-pdf-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8322)
        self.app.config.update(TESTING=True)
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/api/materials/<material_id>/pdf", routes)
        self.assertIn("/api/artifacts/<artifact_id>/pdf-revisions", routes)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "PDF审阅者"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, content: bytes) -> dict:
        response = self.request("post", "/api/materials",
                                data={"title": "客户PDF", "file": (io.BytesIO(content), "评审稿.pdf")},
                                headers=self.auth_headers(), content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def test_original_feedback_revision_restore_download_and_cold_recovery(self) -> None:
        original = _pdf()
        material = self.upload(original)
        original_hash = hashlib.sha256(original).hexdigest()
        inspected = self.request("get", f"/api/materials/{material['id']}/pdf",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        self.assertEqual(inspected.json["data"]["sha256"], original_hash)
        self.assertEqual(inspected.json["data"]["page_count"], 2)

        first = self.request("post", f"/api/materials/{material['id']}/pdf-revisions",
                             json={"changes": [{"page": 1, "quote": "Budget revenue increased.",
                                                 "comment": "Please check the revenue period."}]},
                             headers={**self.auth_headers(), "Idempotency-Key": "pdf-initial-001"})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        artifact_id = first.json["data"]["artifact"]["id"]
        pdf_data = first.json["data"]["pdf"]
        self.assertEqual(pdf_data["source"]["sha256"], original_hash)
        self.assertEqual(pdf_data["base_file"]["kind"], "material")
        self.assertTrue(pdf_data["unchanged_verified"])
        first_file = self.request("get", pdf_data["download_url"], headers=self.auth_headers())
        self.assertEqual(first_file.status_code, 200, first_file.get_data()[:100])
        self.assertEqual(first_file.mimetype, "application/pdf")
        self.assertTrue(first_file.data.startswith(original))
        self.assertEqual(inspect_pdf(first_file.data)["pages"][0]["annotations"][0]["contents"],
                         "Please check the revenue period.")

        followup = self.request("post", f"/api/artifacts/{artifact_id}/pdf-revisions",
                                json={"base_version": 1,
                                      "changes": [{"page": 1, "quote": "A repeated note.",
                                                   "occurrence": 1, "comment": "This instance only."}]},
                                headers={**self.auth_headers(), "Idempotency-Key": "pdf-followup-001"})
        self.assertEqual(followup.status_code, 200, followup.get_data(as_text=True))
        self.assertEqual(followup.json["data"]["artifact"]["version"], 2)
        second_data = followup.json["data"]["pdf"]
        second_file = self.request("get", second_data["download_url"], headers=self.auth_headers())
        self.assertEqual(second_file.status_code, 200, second_file.get_data()[:100])
        second_annotations = inspect_pdf(second_file.data)["pages"][0]["annotations"]
        self.assertEqual([item["contents"] for item in second_annotations],
                         ["Please check the revenue period.", "This instance only."])

        restored = self.request("post", f"/api/artifacts/{artifact_id}/restore",
                                json={"base_version": 2, "restore_version": 1},
                                headers=self.auth_headers())
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        latest = self.request("get", f"/api/artifacts/{artifact_id}/pdf", headers=self.auth_headers())
        self.assertEqual(latest.status_code, 200, latest.get_data(as_text=True))
        restored_data = latest.json["data"]["pdf"]
        self.assertEqual(latest.json["data"]["artifact"]["version"], 3)
        self.assertEqual(restored_data["restored_from_version"], 1)
        export = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (restored_data["export_id"],))
        (self.root / export["relative_path"]).unlink()

        reopened_app = create_app(self.root, expected_port=8322)
        reopened_app.config.update(TESTING=True)
        self.app = reopened_app
        self.client = reopened_app.test_client()
        cold_open = self.request("get", f"/api/artifacts/{artifact_id}/pdf", headers=self.auth_headers())
        self.assertEqual(cold_open.status_code, 200, cold_open.get_data(as_text=True))
        recovered_file = self.request("get", cold_open.json["data"]["pdf"]["download_url"],
                                      headers=self.auth_headers())
        self.assertEqual(recovered_file.status_code, 200, recovered_file.get_data()[:100])
        self.assertEqual([item["contents"] for item in inspect_pdf(recovered_file.data)["pages"][0]["annotations"]],
                         ["Please check the revenue period."])

        original_download = self.request("get", f"/api/materials/{material['id']}/file",
                                         headers=self.auth_headers())
        self.assertEqual(original_download.status_code, 200)
        self.assertEqual(original_download.data, original)
        self.assertEqual(hashlib.sha256(original_download.data).hexdigest(), original_hash)

    def test_scan_pdf_accepts_explicit_crop_bounded_rect_without_ocr_text(self) -> None:
        original = _scanned_pdf()
        material = self.upload(original)
        inspected = self.request("get", f"/api/materials/{material['id']}/pdf",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        page = inspected.json["data"]["pages"][0]
        self.assertFalse(page["text_available"])
        left, bottom, right, top = page["bbox"]
        revision = self.request(
            "post", f"/api/materials/{material['id']}/pdf-revisions",
            json={"changes": [{"page": 1,
                                "rect": [left + 100, bottom + 500, left + 180, bottom + 580],
                                "comment": "Review the image region."}]},
            headers={**self.auth_headers(), "Idempotency-Key": "pdf-scan-rect-001"},
        )
        self.assertEqual(revision.status_code, 201, revision.get_data(as_text=True))
        saved_pdf = revision.json["data"]["pdf"]
        downloaded = self.request("get", saved_pdf["download_url"], headers=self.auth_headers())
        self.assertEqual(downloaded.status_code, 200, downloaded.get_data()[:100])
        result = inspect_pdf(downloaded.data)
        self.assertFalse(result["pages"][0]["text_available"])
        self.assertEqual(result["pages"][0]["annotations"][0]["contents"], "Review the image region.")


if __name__ == "__main__":
    unittest.main()
