from __future__ import annotations

import hashlib
import io
from pathlib import Path
from decimal import Decimal
import tempfile
import unittest
import zipfile

from openpyxl import load_workbook
import xlsxwriter
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.office_spreadsheet import (
    SpreadsheetError,
    calculate_spreadsheet_cells,
    inspect_spreadsheet,
    revise_spreadsheet,
)
from oevira.store import Store


BASE_URL = "http://127.0.0.1:8321"


def _xlsx(*, unsupported_formula: bool = False, with_chart: bool = True) -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    data = workbook.add_worksheet("Data")
    summary = workbook.add_worksheet("Summary")
    amount_format = workbook.add_format({"num_format": "0.00", "bold": True})
    data.write_string("A1", "Month")
    data.write_string("B1", "Amount")
    data.write_string("A2", "Repeated label")
    data.write_number("B2", 10, amount_format)
    data.write_string("A3", "Repeated label")
    data.write_number("B3", 20, amount_format)
    data.write_string("D1", "Repeated label")
    data.write_string("D2", "Repeated label")
    data.write_formula("B4", "=SUM(B2:B3)", amount_format, 999)
    workbook.define_name("Totals", "=Data!$B$2:$B$3")
    data.write_formula("B5", "=SUM(Totals)", amount_format, 999)
    summary.write_formula("A1", "=Data!B4*2", None, 1998)
    if unsupported_formula:
        data.write_formula("B6", "=UNKNOWNFUNC(B2:B3)", None, 10)
    if with_chart:
        chart = workbook.add_chart({"type": "column"})
        chart.add_series({"name": "Amount", "categories": "=Data!$A$2:$A$3",
                          "values": "=Data!$B$2:$B$3"})
        data.insert_chart("F2", chart)
    workbook.close()
    return output.getvalue()


def _aggregate_xlsx() -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = workbook.add_worksheet("Data")
    sheet.write_number("A1", 1)
    sheet.write_string("A2", "not a number")
    sheet.write_boolean("A3", True)
    sheet.write_formula("A4", "=A1/0", None, "#DIV/0!")
    sheet.write_formula("B1", '=SUM(A1,TRUE,"2")', None, 4)
    sheet.write_formula("B2", '=COUNT(A1,TRUE,"2")', None, 3)
    sheet.write_formula("B3", '=AVERAGE(A1,TRUE,"2")', None, 1)
    sheet.write_formula("B4", '=MIN(A1,TRUE,"2")', None, 1)
    sheet.write_formula("B5", '=MAX(A1,TRUE,"2")', None, 2)
    sheet.write_formula("B6", "=SUM(A1:A3)", None, 1)
    sheet.write_formula("B7", "=COUNT(A1:A3)", None, 1)
    sheet.write_formula("B8", "=COUNTA(A1:A4)", None, 4)
    sheet.write_formula("B9", "=SUM(A4)", None, "#DIV/0!")
    sheet.write_formula("B10", "=COUNT(A4)", None, 0)
    sheet.write_formula("B11", "=COUNTA(A4)", None, 1)
    workbook.close()
    return output.getvalue()


def _precedence_xlsx() -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = workbook.add_worksheet("Calc")
    sheet.write_number("A1", 2)
    sheet.write_formula("B1", "=A1^2^3", None, 64)
    sheet.write_formula("C1", "=A1^(2^3)", None, 256)
    sheet.write_formula("D1", "=-2^2", None, 4)
    sheet.write_formula("E1", "=-(2^2)", None, -4)
    sheet.write_formula("F1", "=50%^2", None, 0.25)
    sheet.write_formula("G1", "=50^2%", None, 50 ** 0.02)
    workbook.close()
    return output.getvalue()


def _conditional_xlsx() -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = workbook.add_worksheet("Data")
    sheet.write_string("A1", "Channel")
    sheet.write_string("B1", "Revenue")
    sheet.write_string("C1", "Approved")
    rows = (("Media", 100, "Yes"), ("Retail", 50, "Yes"), ("Media", 200, "No"))
    for row_index, (channel, revenue, approved) in enumerate(rows, start=2):
        sheet.write_string(f"A{row_index}", channel)
        sheet.write_number(f"B{row_index}", revenue)
        sheet.write_string(f"C{row_index}", approved)
    sheet.write_formula("D1", '=SUMIF(A2:A4,"Media",B2:B4)', None, 300)
    sheet.write_formula("D2", '=COUNTIF(C2:C4,"<>No")', None, 2)
    sheet.write_formula("D3", '=AVERAGEIF(A2:A4,"Media",B2:B4)', None, 150)
    sheet.write_formula("D4", '=SUMIFS(B2:B4,A2:A4,"Media",C2:C4,"Yes")', None, 100)
    sheet.write_formula("D5", '=COUNTIFS(A2:A4,"Media",C2:C4,"No")', None, 1)
    sheet.write_formula("D6", '=AVERAGEIFS(B2:B4,A2:A4,"Media")', None, 150)
    workbook.close()
    return output.getvalue()


def _lookup_xlsx() -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = workbook.add_worksheet("Data")
    for row, (key, value) in enumerate(((1, 10), (2, 20), (3, 30)), start=1):
        sheet.write_number(row - 1, 0, key)
        sheet.write_number(row - 1, 1, value)
    sheet.write_string("C1", "Media")
    sheet.write_string("C2", "Retail")
    sheet.write_string("C3", "Creator")
    sheet.write_formula("D1", "=MATCH(2,A1:A3,0)", None, 2)
    sheet.write_formula("D2", "=INDEX(B1:B3,MATCH(2,A1:A3,0))", None, 20)
    sheet.write_formula("D3", "=VLOOKUP(3,A1:B3,2,FALSE)", None, 30)
    sheet.write_formula("D4", '=XLOOKUP(4,A1:A3,B1:B3,"missing",-1)', None, 30)
    sheet.write_formula("D5", '=XLOOKUP(0,A1:A3,B1:B3,"missing",1)', None, 10)
    sheet.write_formula("D6", "=XMATCH(3,A1:A3,0)", None, 3)
    sheet.write_formula("D7", '=MATCH("M*",C1:C3,0)', None, 1)
    sheet.write_formula("D8", '=XLOOKUP("C*",C1:C3,B1:B3,"missing",2)', None, 30)
    workbook.close()
    return output.getvalue()


def _date_xlsx(*, date_1904: bool = False) -> bytes:
    from datetime import datetime

    workbook = Workbook()
    workbook.epoch = CALENDAR_MAC_1904 if date_1904 else workbook.epoch
    sheet = workbook.active
    sheet.title = "Dates"
    sheet["A1"] = datetime(2020, 3, 15)
    sheet["A1"].number_format = "yyyy-mm-dd"
    sheet["B1"] = "=YEAR(A1)"
    sheet["C1"] = "=MONTH(A1)"
    sheet["D1"] = "=DAY(A1)"
    sheet["E1"] = "=DATE(2020,2,29)"
    sheet["F1"] = "=EOMONTH(A1,0)"
    sheet["G1"] = "=TIME(27,30,0)"
    sheet["H1"] = "=HOUR(G1)"
    sheet["I1"] = "=MINUTE(G1)"
    sheet["J1"] = "=WEEKDAY(A1,2)"
    sheet["K1"] = "=DATE(1900,2,29)"
    sheet["L1"] = "=DAY(K1)"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _safe_functions_xlsx() -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = workbook.add_worksheet("Data")
    sheet.write_number("A1", 1)
    sheet.write_formula("A3", '=IF(TRUE,"",0)', None, "")
    sheet.write_number("B1", 2)
    sheet.write_blank("B2", None)
    sheet.write_number("B3", 5)
    sheet.write_formula("C1", "=SUMPRODUCT(A1:A3,B1:B3)", None, 2)
    sheet.write_formula("C2", "=COUNTBLANK(A1:A3)", None, 2)
    sheet.write_formula("C3", '=IFERROR(1/0,"fallback")', None, "fallback")
    sheet.write_formula("C4", '=IFNA(XLOOKUP(99,A1:A3,B1:B3),"missing")', None, "missing")
    sheet.write_formula("C5", "=ISERROR(1/0)", None, True)
    sheet.write_formula("C6", "=ISNA(XLOOKUP(99,A1:A3,B1:B3))", None, True)
    workbook.close()
    return output.getvalue()


def _cell(inspection: dict, sheet_name: str, address: str) -> dict:
    sheet = next(item for item in inspection["sheets"] if item["name"] == sheet_name)
    return next(item for item in sheet["cells"] if item["address"] == address)


def _parts(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class SpreadsheetOriginalTests(unittest.TestCase):
    def test_exact_cell_revision_clones_shared_string_and_recalculates_named_cross_sheet_formulas(self) -> None:
        original = _xlsx()
        inspected = inspect_spreadsheet(original)
        first = _cell(inspected, "Data", "B2")
        shared = _cell(inspected, "Data", "A2")
        before_parts = _parts(original)
        original_book = load_workbook(io.BytesIO(original), data_only=False)
        self.assertEqual(original_book["Data"]["A2"].value, original_book["Data"]["A3"].value)
        self.assertEqual(original_book["Data"]["B2"].number_format, "0.00")
        original_book.close()

        result = revise_spreadsheet(original, [
            {"object_id": first["object_id"], "before": {"kind": "number", "value": "10"},
             "after": {"kind": "number", "value": "15"}},
            {"object_id": shared["object_id"], "before": {"kind": "text", "value": "Repeated label"},
             "after": {"kind": "text", "value": "Selected label"}},
        ])
        revised = result["content"]
        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(len(result["recalculated_cells"]), 3)
        self.assertEqual({item["value"]["value"] for item in result["recalculated_cells"]}, {"35", "70"})
        self.assertEqual(result["charts_updated"], ["xl/charts/chart1.xml"])

        formula_book = load_workbook(io.BytesIO(revised), data_only=False)
        value_book = load_workbook(io.BytesIO(revised), data_only=True)
        self.assertEqual(formula_book["Data"]["A2"].value, "Selected label")
        self.assertEqual(formula_book["Data"]["A3"].value, "Repeated label")
        self.assertEqual(formula_book["Data"]["B2"].number_format, "0.00")
        self.assertEqual(formula_book["Data"]["B4"].value, "=SUM(B2:B3)")
        self.assertEqual(formula_book["Data"]["B5"].value, "=SUM(Totals)")
        self.assertEqual(formula_book["Summary"]["A1"].value, "=Data!B4*2")
        self.assertEqual(value_book["Data"]["B4"].value, 35)
        self.assertEqual(value_book["Data"]["B5"].value, 35)
        self.assertEqual(value_book["Summary"]["A1"].value, 70)
        self.assertEqual(len(formula_book["Data"]._charts), 1)
        formula_book.close()
        value_book.close()

        after_parts = _parts(revised)
        self.assertEqual(set(before_parts), set(after_parts))
        for name, payload in before_parts.items():
            if name not in {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml",
                            "xl/sharedStrings.xml", "xl/workbook.xml", "xl/charts/chart1.xml"}:
                self.assertEqual(after_parts[name], payload, name)
        chart_xml = __import__("lxml.etree", fromlist=["etree"]).fromstring(after_parts["xl/charts/chart1.xml"])
        chart_ns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
        number_points = [node.text for node in chart_xml.xpath(".//c:numRef/c:numCache/c:pt/c:v", namespaces=chart_ns)]
        string_points = [node.text for node in chart_xml.xpath(".//c:strRef/c:strCache/c:pt/c:v", namespaces=chart_ns)]
        self.assertIn("15", number_points)
        self.assertIn("20", number_points)
        self.assertIn("Selected label", string_points)
        self.assertIn("Repeated label", string_points)
        reopened = inspect_spreadsheet(revised)
        self.assertEqual(_cell(reopened, "Data", "A3")["value"],
                         {"kind": "text", "value": "Repeated label"})

    def test_stale_anchor_and_unsupported_formula_fail_without_changing_original(self) -> None:
        original = _xlsx(unsupported_formula=True, with_chart=False)
        inspected = inspect_spreadsheet(original)
        target = _cell(inspected, "Data", "B2")
        with self.assertRaises(SpreadsheetError) as unsupported:
            revise_spreadsheet(original, [{
                "object_id": target["object_id"],
                "before": {"kind": "number", "value": "10"},
                "after": {"kind": "number", "value": "11"},
            }])
        self.assertEqual(unsupported.exception.code, "XLSX_FORMULA_FUNCTION_UNSUPPORTED")
        with self.assertRaises(SpreadsheetError) as stale:
            revise_spreadsheet(original, [{
                "object_id": target["object_id"].replace(inspected["sha256"], "0" * 64),
                "before": {"kind": "number", "value": "10"},
                "after": {"kind": "number", "value": "11"},
            }])
        self.assertEqual(stale.exception.code, "XLSX_OBJECT_BASE_MISMATCH")
        self.assertEqual(hashlib.sha256(original).hexdigest(), inspected["sha256"])

    def test_aggregates_distinguish_direct_values_from_references_and_propagate_errors_per_function(self) -> None:
        original = _aggregate_xlsx()
        inspected = inspect_spreadsheet(original)
        anchor = _cell(inspected, "Data", "A1")
        result = revise_spreadsheet(original, [{
            "object_id": anchor["object_id"],
            "before": {"kind": "number", "value": "1"},
            "after": {"kind": "number", "value": "2"},
        }])
        self.assertTrue(result["unchanged_verified"])
        values = load_workbook(io.BytesIO(result["content"]), data_only=True)
        sheet = values["Data"]
        self.assertEqual(sheet["B1"].value, 5)       # direct TRUE and "2" count in SUM
        self.assertEqual(sheet["B2"].value, 3)       # direct TRUE and "2" count in COUNT
        self.assertEqual(sheet["B3"].value, 2)       # AVERAGE ignores direct logical/text
        self.assertEqual(sheet["B4"].value, 1)       # MIN counts direct logical/text
        self.assertEqual(sheet["B5"].value, 2)       # MAX counts direct logical/text
        self.assertEqual(sheet["B6"].value, 2)       # referenced text/logical are ignored
        self.assertEqual(sheet["B7"].value, 1)       # COUNT ignores referenced logical/text
        self.assertEqual(sheet["B8"].value, 4)       # COUNTA counts text, logical and errors
        self.assertEqual(sheet["B9"].value, "#DIV/0!")  # SUM propagates a referenced error
        self.assertEqual(sheet["B10"].value, 0)      # COUNT ignores referenced errors
        self.assertEqual(sheet["B11"].value, 1)     # COUNTA counts referenced errors
        values.close()

    def test_read_only_calculator_ignores_stale_formula_caches_and_keeps_independent_results(self) -> None:
        original = _xlsx(unsupported_formula=True, with_chart=False)
        inspection = inspect_spreadsheet(original)
        references = [
            _cell(inspection, "Data", "B4")["object_id"],
            _cell(inspection, "Data", "B5")["object_id"],
            _cell(inspection, "Summary", "A1")["object_id"],
            _cell(inspection, "Data", "A2")["object_id"],
            _cell(inspection, "Data", "B6")["object_id"],
        ]
        result = calculate_spreadsheet_cells(original, references)
        self.assertEqual(result["calculation_engine"], "oevira-xlsx-formula")
        self.assertEqual(result["calculation_revision"], "2026-11-10.13")
        computed = {item["address"] + ":" + str(item["sheet"]): item["value"]
                    for item in result["computed"]}
        self.assertEqual(computed["B4:Data"]["value"], "30")
        self.assertEqual(computed["B5:Data"]["value"], "30")
        self.assertEqual(computed["A1:Summary"]["value"], "60")
        self.assertEqual(computed["A2:Data"]["value"], "Repeated label")
        self.assertEqual(result["unresolved"], [{
            "object_id": references[-1], "sheet_id": 1, "sheet": "Data", "address": "B6",
            "formula": "=UNKNOWNFUNC(B2:B3)", "reason": "XLSX_FORMULA_FUNCTION_UNSUPPORTED",
        }])

    def test_excel_power_percent_and_negation_precedence_is_independent_of_python_rules(self) -> None:
        original = _precedence_xlsx()
        inspection = inspect_spreadsheet(original)
        anchor = _cell(inspection, "Calc", "A1")
        revised = revise_spreadsheet(original, [{
            "object_id": anchor["object_id"],
            "before": {"kind": "number", "value": "2"},
            "after": {"kind": "number", "value": "3"},
        }])
        values = load_workbook(io.BytesIO(revised["content"]), data_only=True)
        sheet = values["Calc"]
        self.assertEqual(sheet["B1"].value, 729)
        self.assertEqual(sheet["C1"].value, 6561)
        values.close()

        references = [_cell(inspection, "Calc", address)["object_id"] for address in ("D1", "E1", "F1", "G1")]
        calculated = calculate_spreadsheet_cells(original, references)
        results = {item["address"]: Decimal(item["value"]["value"])
                   for item in calculated["computed"]}
        self.assertEqual(results["D1"], Decimal("4"))
        self.assertEqual(results["E1"], Decimal("-4"))
        self.assertEqual(results["F1"], Decimal("0.25"))
        self.assertEqual(results["G1"], Decimal("1.08138265680029"))

    def test_conditional_aggregation_recalculates_only_when_its_source_matches_change(self) -> None:
        original = _conditional_xlsx()
        inspection = inspect_spreadsheet(original)
        target = _cell(inspection, "Data", "A4")
        revised = revise_spreadsheet(original, [{
            "object_id": target["object_id"],
            "before": {"kind": "text", "value": "Media"},
            "after": {"kind": "text", "value": "Retail"},
        }])
        output = load_workbook(io.BytesIO(revised["content"]), data_only=True)
        sheet = output["Data"]
        self.assertEqual(sheet["D1"].value, 100)
        self.assertEqual(sheet["D2"].value, 2)
        self.assertEqual(sheet["D3"].value, 100)
        self.assertEqual(sheet["D4"].value, 100)
        self.assertEqual(sheet["D5"].value, 0)
        self.assertEqual(sheet["D6"].value, 100)
        output.close()

    def test_lookup_and_reference_formulas_recalculate_against_the_selected_version(self) -> None:
        original = _lookup_xlsx()
        inspection = inspect_spreadsheet(original)
        target = _cell(inspection, "Data", "B2")
        revised = revise_spreadsheet(original, [{
            "object_id": target["object_id"],
            "before": {"kind": "number", "value": "20"},
            "after": {"kind": "number", "value": "25"},
        }])
        workbook = load_workbook(io.BytesIO(revised["content"]), data_only=True)
        sheet = workbook["Data"]
        self.assertEqual(sheet["D1"].value, 2)
        self.assertEqual(sheet["D2"].value, 25)
        self.assertEqual(sheet["D3"].value, 30)
        self.assertEqual(sheet["D4"].value, 30)
        self.assertEqual(sheet["D5"].value, 10)
        self.assertEqual(sheet["D6"].value, 3)
        self.assertEqual(sheet["D7"].value, 1)
        self.assertEqual(sheet["D8"].value, 30)
        workbook.close()

    def test_date_and_time_formulas_keep_the_workbook_1900_or_1904_system(self) -> None:
        for use_1904, expected_leap_day, expected_month_end in ((False, 43890, 43921),
                                                                 (True, 42428, 42459)):
            content = _date_xlsx(date_1904=use_1904)
            inspection = inspect_spreadsheet(content)
            refs = [_cell(inspection, "Dates", address)["object_id"]
                    for address in ("B1", "C1", "D1", "E1", "F1", "G1", "H1", "I1", "J1", "K1", "L1")]
            result = calculate_spreadsheet_cells(content, refs)
            values = {item["address"]: item["value"] for item in result["computed"]}
            self.assertEqual(values["B1"]["value"], "2020")
            self.assertEqual(values["C1"]["value"], "3")
            self.assertEqual(values["D1"]["value"], "15")
            self.assertEqual(values["E1"]["value"], str(expected_leap_day))
            self.assertEqual(values["F1"]["value"], str(expected_month_end))
            self.assertEqual(values["G1"]["value"], "0.145833333333333")
            self.assertEqual(values["H1"]["value"], "3")
            self.assertEqual(values["I1"]["value"], "30")
            self.assertEqual(values["J1"]["value"], "7")
            if not use_1904:
                self.assertEqual(values["K1"]["value"], "60")
                self.assertEqual(values["L1"]["value"], "29")
            else:
                self.assertEqual(values["L1"], {"kind": "error", "value": "#NUM!", "verified": True})

    def test_error_wrappers_blank_counts_and_sumproduct_use_typed_source_values(self) -> None:
        content = _safe_functions_xlsx()
        inspection = inspect_spreadsheet(content)
        refs = [_cell(inspection, "Data", f"C{row}")["object_id"] for row in range(1, 7)]
        result = calculate_spreadsheet_cells(content, refs)
        values = {item["address"]: item["value"] for item in result["computed"]}
        self.assertEqual(values["C1"]["value"], "2")
        self.assertEqual(values["C2"]["value"], "2")
        self.assertEqual(values["C3"], {"kind": "text", "value": "fallback", "verified": True})
        self.assertEqual(values["C4"], {"kind": "text", "value": "missing", "verified": True})
        self.assertEqual(values["C5"], {"kind": "boolean", "value": True, "verified": True})
        self.assertEqual(values["C6"], {"kind": "boolean", "value": True, "verified": True})


class SpreadsheetApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-xlsx-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8321)
        self.app.config.update(TESTING=True)
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/api/materials/<material_id>/spreadsheet", routes)
        self.assertIn("/api/artifacts/<artifact_id>/spreadsheet-revisions", routes)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "工作簿修订者"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]
        self.assertTrue({"document", "presentation", "spreadsheet", "pdf_document"} <=
                        set(self.app.extensions["oevira.revision_handlers"]))

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
                                data={"title": "客户工作簿", "file": (io.BytesIO(content), "预算.xlsx")},
                                headers=self.auth_headers(), content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def test_initial_followup_restore_download_and_cold_export_recovery(self) -> None:
        original = _xlsx(with_chart=False)
        material = self.upload(original)
        original_hash = hashlib.sha256(original).hexdigest()
        inspected = self.request("get", f"/api/materials/{material['id']}/spreadsheet",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        first_cell = _cell(inspected.json["data"], "Data", "B2")
        first = self.request(
            "post", f"/api/materials/{material['id']}/spreadsheet-revisions",
            json={"changes": [{"object_id": first_cell["object_id"],
                                "before": {"kind": "number", "value": "10"},
                                "after": {"kind": "number", "value": "15"}}]},
            headers={**self.auth_headers(), "Idempotency-Key": "xlsx-initial-001"},
        )
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        first_data = first.json["data"]["spreadsheet"]
        artifact_id = first.json["data"]["artifact"]["id"]
        self.assertEqual(first_data["source"]["sha256"], original_hash)
        self.assertEqual(first_data["base_file"]["kind"], "material")
        self.assertEqual(first_data["calculation_engine"], "oevira-xlsx-formula")
        self.assertEqual(first_data["calculation_revision"], "2026-11-10.13")
        self.assertTrue(first_data["unchanged_verified"])

        download = self.request("get", first_data["download_url"], headers=self.auth_headers())
        self.assertEqual(download.status_code, 200, download.get_data()[:200])
        self.assertEqual(download.mimetype, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        first_bytes = download.data
        self.assertEqual(hashlib.sha256(first_bytes).hexdigest(), first_data["sha256"])
        values = load_workbook(io.BytesIO(first_bytes), data_only=True)
        self.assertEqual(values["Data"]["B4"].value, 35)
        values.close()

        reopened = self.request("get", f"/api/artifacts/{artifact_id}/spreadsheet?version=1",
                                headers=self.auth_headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        next_cell = _cell(reopened.json["data"]["spreadsheet"], "Data", "A2")
        followup = self.request(
            "post", f"/api/artifacts/{artifact_id}/spreadsheet-revisions",
            json={"base_version": 1,
                  "changes": [{"object_id": next_cell["object_id"],
                               "before": {"kind": "text", "value": "Repeated label"},
                               "after": {"kind": "text", "value": "January"}}]},
            headers={**self.auth_headers(), "Idempotency-Key": "xlsx-followup-001"},
        )
        self.assertEqual(followup.status_code, 200, followup.get_data(as_text=True))
        second = followup.json["data"]["spreadsheet"]
        self.assertEqual(followup.json["data"]["artifact"]["version"], 2)
        self.assertEqual(second["base_file"]["export_id"], first_data["export_id"])
        second_download = self.request("get", second["download_url"], headers=self.auth_headers())
        self.assertEqual(second_download.status_code, 200, second_download.get_data()[:200])
        second_book = load_workbook(io.BytesIO(second_download.data), data_only=True)
        self.assertEqual(second_book["Data"]["A2"].value, "January")
        self.assertEqual(second_book["Data"]["A3"].value, "Repeated label")
        self.assertEqual(second_book["Data"]["B4"].value, 35)
        second_book.close()

        restored = self.request("post", f"/api/artifacts/{artifact_id}/restore",
                                json={"base_version": 2, "restore_version": 1},
                                headers=self.auth_headers())
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        latest = self.request("get", f"/api/artifacts/{artifact_id}/spreadsheet",
                              headers=self.auth_headers())
        self.assertEqual(latest.status_code, 200, latest.get_data(as_text=True))
        restored_data = latest.json["data"]["spreadsheet"]
        self.assertEqual(latest.json["data"]["artifact"]["version"], 3)
        self.assertEqual(restored_data["restored_from_version"], 1)
        self.assertEqual(restored_data["changes"], [])
        self.assertEqual(restored_data["base_file"]["export_id"], first_data["export_id"])

        export = self.store.row("SELECT relative_path FROM exports WHERE id = ?", (restored_data["export_id"],))
        export_path = self.root / export["relative_path"]
        export_path.unlink()
        reopened_app = create_app(self.root, expected_port=8321)
        reopened_app.config.update(TESTING=True)
        self.app = reopened_app
        self.client = reopened_app.test_client()
        cold_open = self.request("get", f"/api/artifacts/{artifact_id}/spreadsheet",
                                 headers=self.auth_headers())
        self.assertEqual(cold_open.status_code, 200, cold_open.get_data(as_text=True))
        recovered_file = self.request("get", cold_open.json["data"]["spreadsheet"]["download_url"],
                                      headers=self.auth_headers())
        self.assertEqual(recovered_file.status_code, 200, recovered_file.get_data()[:200])
        restored_book = load_workbook(io.BytesIO(recovered_file.data), data_only=True)
        self.assertEqual(restored_book["Data"]["A2"].value, "Repeated label")
        self.assertEqual(restored_book["Data"]["B4"].value, 35)
        restored_book.close()

        original_download = self.request("get", f"/api/materials/{material['id']}/file",
                                         headers=self.auth_headers())
        self.assertEqual(original_download.status_code, 200)
        self.assertEqual(hashlib.sha256(original_download.data).hexdigest(), original_hash)
        self.assertEqual(original_download.data, original)

    def test_registered_pure_revision_callback_uses_the_same_workbook_version_owner(self) -> None:
        material = self.upload(_xlsx(with_chart=False))
        inspected = self.request("get", f"/api/materials/{material['id']}/spreadsheet",
                                 headers=self.auth_headers())
        cell = _cell(inspected.json["data"], "Data", "B2")
        first = self.request("post", f"/api/materials/{material['id']}/spreadsheet-revisions",
                             json={"changes": [{"object_id": cell["object_id"],
                                                 "before": {"kind": "number", "value": "10"},
                                                 "after": {"kind": "number", "value": "15"}}]},
                             headers={**self.auth_headers(), "Idempotency-Key": "xlsx-pure-initial"})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        artifact_id = first.json["data"]["artifact"]["id"]
        reopened = self.request("get", f"/api/artifacts/{artifact_id}/spreadsheet?version=1",
                                headers=self.auth_headers())
        second_cell = _cell(reopened.json["data"]["spreadsheet"], "Data", "A2")
        handler = self.app.extensions["oevira.revision_handlers"]["spreadsheet"]
        result = handler(self.store, self.identity, artifact_id,
                         {"base_version": 1, "changes": [{"object_id": second_cell["object_id"],
                                                            "before": {"kind": "text", "value": "Repeated label"},
                                                            "after": {"kind": "text", "value": "Pure callback"}}]},
                         "xlsx-pure-followup")
        self.assertEqual(result["artifact"]["version"], 2)
        self.assertTrue(result["spreadsheet"]["unchanged_verified"])


if __name__ == "__main__":
    unittest.main()
