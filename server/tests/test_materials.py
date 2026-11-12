from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import patch

from oevira.comparison import ComparisonInputLimitError, build_comparison
from oevira.material_parser import parse_material


def _zip_bytes(parts: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def _xlsx(*, external_relationship: bool = False) -> bytes:
    if external_relationship:
        relation = (
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="https://example.invalid/sheet.xml" TargetMode="External"/>'
        )
    else:
        relation = (
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
        )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>品牌</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>收入</t></is></c>'
        '</row><row r="2">'
        '<c r="A2" t="inlineStr"><is><t>寰意</t></is></c>'
        '<c r="B2"><v>120000</v></c>'
        '<c r="C2"><f>SUM(B1:B2)</f><v>120000</v></c>'
        '</row></sheetData></worksheet>'
    )
    parts = {
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="运营" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relation}</Relationships>"
        ).encode(),
        "xl/worksheets/sheet1.xml": sheet.encode(),
    }
    return _zip_bytes(parts)


def _pptx() -> bytes:
    return _zip_bytes({
        "ppt/presentation.xml": (
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
        ).encode(),
        "ppt/_rels/presentation.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            'Target="slides/slide1.xml"/></Relationships>'
        ).encode(),
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="标题"/>'
            '</p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>三份材料比较</a:t>'
            '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
        ).encode(),
    })


def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


class MaterialParserTests(unittest.TestCase):
    def test_utf8_chinese_lines_have_exact_unicode_offsets(self):
        result = parse_material("报告.md", "品牌收入上升\n2025 年销量下降".encode())
        self.assertEqual(result["status"], "ready")
        self.assertEqual([segment["label"] for segment in result["segments"]], ["第 1 行", "第 2 行"])
        for segment in result["segments"]:
            self.assertEqual(result["text"][segment["start"]:segment["end"]], segment["text"])
        self.assertIn("品牌收入上升", result["text"])

    def test_csv_quotes_multiline_cells_and_coordinates(self):
        source = '品牌,收入,说明\n寰意,"120,000 元","第一行\n第二行"\n'.encode()
        result = parse_material("运营.csv", source)
        self.assertEqual(result["status"], "ready")
        self.assertIn("分隔符", result["reason"])  # Disclosure does not imply data loss.
        by_label = {item["label"]: item["text"] for item in result["segments"]}
        self.assertEqual(by_label["第 2 行，第 2 列（B2）；首个非空行的候选列名“收入”"], "120,000 元")
        self.assertEqual(by_label["第 2 行，第 3 列（C2）；首个非空行的候选列名“说明”"], "第一行\n第二行")

    def test_docx_paragraphs_and_table_cells_are_citable(self):
        from docx import Document
        output = io.BytesIO()
        document = Document()
        document.add_paragraph("品牌收入为 120 万元")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "指标"
        table.cell(0, 1).text = "销量"
        table.cell(1, 0).text = "本期"
        table.cell(1, 1).text = "下降 5%"
        document.save(output)
        result = parse_material("材料.docx", output.getvalue())
        self.assertEqual(result["status"], "ready")
        self.assertIn("品牌收入为 120 万元", result["text"])
        self.assertTrue(any(item["kind"] == "docx_table_cell" and item["text"] == "下降 5%"
                            for item in result["segments"]))

    def test_pdf_page_text_is_citable_and_scanned_pdf_is_unavailable(self):
        import pdfplumber  # Required runtime dependency; absence must fail CI.
        result = parse_material("简报.pdf", _minimal_pdf("Revenue 120 units"))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["segments"][0]["label"], "第 1 页")
        self.assertIn("Revenue 120 units", result["segments"][0]["text"])
        scanned = parse_material("扫描件.pdf", _minimal_pdf(""))
        self.assertEqual(scanned["status"], "unavailable")
        self.assertIn("不执行 OCR", scanned["reason"])

    def test_xlsx_and_pptx_extract_real_structure_without_external_execution(self):
        workbook = parse_material("运营.xlsx", _xlsx())
        self.assertEqual(workbook["status"], "partial")  # formula is retained, never calculated
        self.assertTrue(any(item["label"].startswith("运营!A2；") and item["text"] == "寰意"
                            for item in workbook["segments"]))
        formula = next(item for item in workbook["segments"] if item["kind"] == "xlsx_formula")
        self.assertIn("未重算", formula["text"])
        aligned = build_comparison([{"material_id": "book", "parsed": workbook}], ["收入"], "")
        income_cell = next(item for item in aligned["rows"][0]["cells"][0]["excerpts"]
                           if "候选列名“收入”" in item["label"])
        self.assertEqual(income_cell["quote"], "120000")
        formula_comparison = build_comparison([{"material_id": "book", "parsed": workbook}], ["公式"], "")
        formula_cell = next(item for item in formula_comparison["rows"][0]["cells"])
        self.assertEqual(formula_cell["numbers"], [])

        presentation = parse_material("方案.pptx", _pptx())
        self.assertEqual(presentation["status"], "ready")
        self.assertTrue(any(item["kind"] == "pptx_text" and item["text"] == "三份材料比较"
                            for item in presentation["segments"]))

        external = parse_material("外链.xlsx", _xlsx(external_relationship=True))
        self.assertEqual(external["status"], "unavailable")
        self.assertIn("未访问外部目标", external["reason"])

    def test_corruption_invalid_encoding_entity_and_zip_limits_are_honest(self):
        self.assertEqual(parse_material("损坏.docx", b"not a zip")["status"], "error")
        self.assertEqual(parse_material("乱码.txt", b"\xff\xfe\x81")["status"], "error")
        entity = _xlsx()
        with zipfile.ZipFile(io.BytesIO(entity), "r") as source:
            parts = {name: source.read(name) for name in source.namelist()}
        parts["xl/workbook.xml"] = (
            '<!DOCTYPE x [<!ENTITY test "扩展">]>'.encode() + parts["xl/workbook.xml"]
        )
        self.assertEqual(parse_material("entity.xlsx", _zip_bytes(parts))["status"], "error")

        traversal = _xlsx()
        with zipfile.ZipFile(io.BytesIO(traversal), "r") as source:
            parts = {name: source.read(name) for name in source.namelist()}
        parts["../outside.xml"] = b"<x/>"
        self.assertEqual(parse_material("path.xlsx", _zip_bytes(parts))["status"], "error")

        with patch("oevira.material_parser.MAX_ZIP_TOTAL_BYTES", 32):
            limited = parse_material("large.xlsx", _xlsx())
        self.assertEqual(limited["status"], "unavailable")

    def test_malformed_csv_and_unsupported_formats_do_not_become_empty_success(self):
        malformed = parse_material("坏.csv", b'a,b\n"unterminated,2\n')
        self.assertEqual(malformed["status"], "error")
        self.assertFalse(malformed["text"])
        unsupported = parse_material("input.bin", b"\x00\x01")
        self.assertEqual(unsupported["status"], "unavailable")


class ComparisonTests(unittest.TestCase):
    def test_criteria_return_exact_citations_numeric_and_conflict_candidates(self):
        sources = [
            ("a", "2024年收入 120 万元，销量增长 8%。"),
            ("b", "2025年收入 90 万元，销量下降 3%。"),
            ("c", "收入未披露；销量数据缺失。"),
        ]
        materials = [{"material_id": material_id,
                      "parsed": parse_material(f"{material_id}.txt", body.encode())}
                     for material_id, body in sources]
        result = build_comparison(materials, ["收入", "销量"], "比较经营表现")
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(result["rows"][0]["numeric_difference_candidates"])
        self.assertTrue(result["rows"][1]["conflict_candidates"])
        self.assertTrue(any(item["material_id"] == "a" and "收入 120 万元" in item["quote"]
                            for item in result["citations"]))
        self.assertTrue(any("不判断语义等价" in value for value in result["limitations"]))

        for row in result["rows"]:
            for cell in row["cells"]:
                for excerpt in cell["excerpts"]:
                    material = next(item for item in materials if item["material_id"] == cell["material_id"])
                    parsed = material["parsed"]
                    segment = next(item for item in parsed["segments"] if item["id"] == excerpt["segment_id"])
                    self.assertEqual(segment["text"][excerpt["quote_start"]:excerpt["quote_end"]], excerpt["quote"])

    def test_not_found_unavailable_and_partial_are_distinct(self):
        materials = [
            {"material_id": "readable", "parsed": parse_material("a.txt", b"only the original text")},
            {"material_id": "broken", "parsed": parse_material("b.docx", b"bad zip")},
            {"material_id": "partial", "parsed": {
                "status": "partial", "reason": "部分内容未提取。", "format": "txt",
                "text": "available segment", "segments": [{"id": "seg-000001", "label": "第 1 行",
                    "text": "available segment", "start": 0, "end": 17, "kind": "text_line"}],
            }},
        ]
        result = build_comparison(materials, ["missing term"], "检查缺失项")
        statuses = {cell["material_id"]: cell["status"] for cell in result["rows"][0]["cells"]}
        self.assertEqual(statuses, {"readable": "not_found", "broken": "read_error", "partial": "partial_unassessed"})
        self.assertTrue(any("部分内容未提取" in value for value in result["limitations"]))

    def test_user_can_change_criteria_or_remove_input_by_recomputing(self):
        first = {"material_id": "one", "parsed": parse_material("one.txt", "价格 10 元".encode())}
        second = {"material_id": "two", "parsed": parse_material("two.txt", "价格 20 元".encode())}
        initial = build_comparison([first, second], ["价格"], "比较")
        changed = build_comparison([first], ["不存在"], "比较")
        self.assertTrue(initial["rows"][0]["numeric_difference_candidates"])
        self.assertEqual(changed["rows"][0]["cells"][0]["status"], "not_found")
        self.assertEqual(len(changed["rows"][0]["cells"]), 1)

    def test_table_header_alignment_returns_the_actual_cell_value(self):
        first = parse_material("one.csv", "品牌,收入\n甲,120000元\n".encode())
        second = parse_material("two.csv", "品牌,收入\n乙,90000元\n".encode())
        result = build_comparison([
            {"material_id": "one", "parsed": first},
            {"material_id": "two", "parsed": second},
        ], ["收入"], "比较收入字段")
        row = result["rows"][0]
        self.assertTrue(row["numeric_difference_candidates"])
        values = [excerpt for cell in row["cells"] for excerpt in cell["excerpts"]
                  if "候选列名“收入”" in excerpt["label"]]
        self.assertEqual({excerpt["quote"] for excerpt in values}, {"120000元", "90000元"})
        self.assertTrue(all(excerpt["match_type"] == "structured_field_candidate" for excerpt in values))

    def test_comparison_keeps_all_materials_and_cross_input_candidates_above_twenty(self):
        materials = []
        for index in range(21):
            material_id = f"material-{index:02d}"
            direction = "预算增加" if index == 0 else "预算减少" if index == 20 else "预算变化"
            parsed = parse_material(f"{material_id}.txt", f"{direction} {100 + index} 万元。".encode())
            materials.append({"material_id": material_id, "parsed": parsed})

        result = build_comparison(materials, ["预算"], "比较全部输入")

        row = result["rows"][0]
        self.assertEqual([cell["material_id"] for cell in row["cells"]], [item["material_id"] for item in materials])
        self.assertEqual({citation["material_id"] for citation in result["citations"]}, {item["material_id"] for item in materials})
        numeric_candidate = row["numeric_difference_candidates"][0]
        self.assertEqual({item["material_id"] for item in numeric_candidate["evidence"]}, {item["material_id"] for item in materials})
        conflict_candidate = next(item for item in row["conflict_candidates"] if item["positive_cue"] == "增加" and item["negative_cue"] == "减少")
        self.assertEqual({item["material_id"] for item in conflict_candidate["evidence"]}, {"material-00", "material-20"})

    def test_excerpts_report_exact_match_count_beyond_the_per_cell_evidence_budget(self):
        material = {
            "material_id": "many-matches",
            "parsed": parse_material("many-matches.txt", "\n".join(f"预算项目{index}" for index in range(60)).encode()),
        }

        result = build_comparison([material], ["预算"], "统计命中")

        cell = result["rows"][0]["cells"][0]
        self.assertEqual(len(cell["excerpts"]), 50)
        self.assertEqual(cell["matched_segment_count"], 60)
        self.assertEqual(cell["omitted_match_count"], 10)
        self.assertTrue(any("共有 10 个匹配片段" in item for item in result["limitations"]))

    def test_comparison_text_budget_reports_exact_unassessed_size(self):
        parsed = {"status": "ready", "text": "x" * 10_000_001, "segments": []}
        materials = [
            {"material_id": "large-a", "parsed": parsed},
            {"material_id": "large-b", "parsed": parsed},
        ]

        with self.assertRaises(ComparisonInputLimitError) as raised:
            build_comparison(materials, ["budget"], "check size")

        self.assertEqual(raised.exception.actual_chars, 20_000_002)
        self.assertEqual(raised.exception.limit_chars, 20_000_000)


if __name__ == "__main__":
    unittest.main()
