"""Deterministic renderers for already-saved Artifact content.

This module does not create business claims or infer formula meaning from
prose. New Word, presentation, workbook and PDF files are generated from saved
Artifact content using the approved, pinned libraries. Original-backed Office
packages remain on their preservation renderer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
import io
import math
from pathlib import Path
import re
import threading
import unicodedata
import zipfile


class GenerationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code)


_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_NUMBER = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9]\d*)$")
_CELL_RANGE = re.compile(r"^([A-Z]{1,3})([1-9]\d*):([A-Z]{1,3})([1-9]\d*)$")


def _cells(line: str) -> list[str]:
    result: list[str] = []
    value: list[str] = []
    escaped = False
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|") and not raw.endswith(r"\|"):
        raw = raw[:-1]
    for char in raw:
        if escaped:
            value.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            result.append("".join(value).strip())
            value.clear()
        else:
            value.append(char)
    if escaped:
        value.append("\\")
    result.append("".join(value).strip())
    return result


def parse_markdown(body: str, citations: list[str] | None = None) -> list[dict]:
    """Read a bounded Markdown subset without dropping non-empty source lines."""
    if not isinstance(body, str):
        raise GenerationError("ARTIFACT_BODY_INVALID", "The saved Artifact body is not readable text.")
    lines = body.splitlines()
    blocks: list[dict] = []
    paragraph: list[str] = []
    code_lines: list[str] | None = None
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "text": " ".join(part.strip() for part in paragraph)})
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            flush_paragraph()
            if code_lines is None:
                code_lines = []
            else:
                blocks.append({"type": "code", "text": "\n".join(code_lines)})
                code_lines = None
            index += 1
            continue
        if code_lines is not None:
            code_lines.append(line)
            index += 1
            continue
        heading = re.fullmatch(r"\s*(#{1,6})\s+(.+?)\s*#*\s*", line)
        if heading:
            flush_paragraph()
            blocks.append({"type": "heading", "level": len(heading.group(1)), "text": heading.group(2)})
            index += 1
            continue
        if (index + 1 < len(lines) and "|" in line
                and _MARKDOWN_TABLE_SEPARATOR.fullmatch(lines[index + 1]) is not None):
            flush_paragraph()
            header = _cells(line)
            if not header:
                paragraph.append(line)
                index += 1
                continue
            rows = [header]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = _cells(lines[index])
                if len(row) != len(header):
                    break
                rows.append(row)
                index += 1
            blocks.append({"type": "table", "rows": rows})
            continue
        if not line.strip():
            flush_paragraph()
        else:
            list_item = re.match(r"^\s*(?:[-+*]|\d+[.)])\s+(.+)$", line)
            paragraph.append(("• " + list_item.group(1)) if list_item else line)
        index += 1

    if code_lines is not None:
        blocks.append({"type": "code", "text": "\n".join(code_lines)})
    flush_paragraph()
    if citations:
        blocks.append({"type": "heading", "level": 2, "text": "Sources"})
        blocks.extend({"type": "paragraph", "text": value.removeprefix("- ")} for value in citations)
    return blocks


def _dedupe_title_heading(blocks: list[dict], title: str) -> list[dict]:
    remaining = list(blocks)
    if (remaining and remaining[0]["type"] == "heading" and remaining[0]["level"] == 1
            and remaining[0]["text"].strip() == title.strip()):
        remaining.pop(0)
    return remaining


def _required_text(blocks: list[dict]) -> list[str]:
    required: list[str] = []
    for block in blocks:
        if block["type"] == "table":
            required.extend(cell for row in block["rows"] for cell in row if cell)
        elif block["text"].strip():
            required.append(block["text"])
    return required


def render_docx(title: str, body: str, citations: list[str]) -> bytes:
    from docx import Document

    blocks = _dedupe_title_heading(parse_markdown(body), title)
    document = Document()
    document.core_properties.title = title
    document.add_heading(title, level=0)
    for block in blocks:
        if block["type"] == "heading":
            document.add_heading(block["text"], level=min(max(block["level"], 1), 3))
        elif block["type"] == "table":
            rows = block["rows"]
            if not rows:
                continue
            table = document.add_table(rows=1, cols=len(rows[0]))
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, rows[0]):
                cell.text = value
            for row in rows[1:]:
                cells = table.add_row().cells
                for cell, value in zip(cells, row):
                    cell.text = value
        elif block["type"] == "code":
            document.add_paragraph(block["text"], style="No Spacing")
        else:
            document.add_paragraph(block["text"])
    if citations:
        document.add_heading("Sources", level=1)
        for citation in citations:
            document.add_paragraph(citation.removeprefix("- "), style="List Bullet")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def validate_docx(content: bytes, title: str, body: str, citations: list[str]) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None:
                return False
        from docx import Document

        document = Document(io.BytesIO(content))
        observed = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        )
        normalized = re.sub(r"\s+", "", observed)
        blocks = _dedupe_title_heading(parse_markdown(body), title)
        required = [title, *_required_text(blocks), *(item.removeprefix("- ") for item in citations)]
        return all(re.sub(r"\s+", "", value) in normalized for value in required if value)
    except Exception:
        return False


def _col_number(value: str) -> int:
    result = 0
    for char in value:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _cell_reference(value: str) -> tuple[int, int]:
    match = _CELL_REFERENCE.fullmatch(value)
    if match is None:
        raise GenerationError("SPREADSHEET_FORMULA_REFERENCE_INVALID", "A workbook calculation uses an invalid cell reference.")
    row, column = int(match.group(2)), _col_number(match.group(1))
    if row > 1_048_576 or column > 16_384:
        raise GenerationError("SPREADSHEET_FORMULA_REFERENCE_INVALID", "A workbook calculation points outside the supported sheet range.")
    return row, column


def _number_value(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        return None
    if len(text) > 1 and text[0] == "0" and text[1].isdigit():
        return None
    if len(text) > 2 and text[:2] in {"-0", "+0"} and text[2].isdigit():
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _spreadsheet_formulas(input_snapshot: dict | None) -> list[dict]:
    if not isinstance(input_snapshot, dict) or input_snapshot.get("spreadsheet") is None:
        return []
    specification = input_snapshot["spreadsheet"]
    if not isinstance(specification, dict) or specification.get("version") != 1:
        raise GenerationError("SPREADSHEET_SPEC_UNSUPPORTED", "The saved spreadsheet calculation format is not supported.")
    formulas = specification.get("formulas", [])
    if not isinstance(formulas, list):
        raise GenerationError("SPREADSHEET_FORMULA_INVALID", "Workbook formulas must be a list of typed calculations.")
    seen_targets: set[tuple[int, str]] = set()
    for item in formulas:
        if (not isinstance(item, dict) or set(item) != {"table_index", "cell", "function", "range"}
                or type(item.get("table_index")) is not int or item["table_index"] < 1):
            raise GenerationError("SPREADSHEET_FORMULA_INVALID", "Each calculation requires table_index, cell, function and range.")
        if not isinstance(item["cell"], str) or not isinstance(item["range"], str):
            raise GenerationError("SPREADSHEET_FORMULA_INVALID", "Calculation cell and range must be text references.")
        if not isinstance(item["function"], str) or item["function"] not in {"SUM", "AVERAGE", "MIN", "MAX", "COUNT"}:
            raise GenerationError("SPREADSHEET_FORMULA_UNSUPPORTED", "Only SUM, AVERAGE, MIN, MAX and COUNT calculations are supported.")
        target = (item["table_index"], item["cell"])
        if target in seen_targets:
            raise GenerationError("SPREADSHEET_FORMULA_TARGET_DUPLICATED", "Each workbook calculation cell can be specified once.")
        seen_targets.add(target)
    return formulas


def _formula_results(table_number: int, rows: list[list[str]], formulas: list[dict]) -> list[dict]:
    results: list[dict] = []
    occupied = {(row, column) for row, values in enumerate(rows, start=1)
                for column in range(1, len(values) + 1)}
    for item in formulas:
        if item["table_index"] != table_number:
            continue
        function = item["function"]
        if function not in {"SUM", "AVERAGE", "MIN", "MAX", "COUNT"}:
            raise GenerationError("SPREADSHEET_FORMULA_UNSUPPORTED", "Only SUM, AVERAGE, MIN, MAX and COUNT calculations are supported.")
        cell = item["cell"]
        target = _cell_reference(cell)
        if target in occupied:
            raise GenerationError("SPREADSHEET_FORMULA_TARGET_OCCUPIED", "A calculation cannot replace supplied table data.")
        match = _CELL_RANGE.fullmatch(item["range"])
        if match is None:
            raise GenerationError("SPREADSHEET_FORMULA_RANGE_INVALID", "A calculation must use a rectangular A1 range.")
        first = _cell_reference(match.group(1) + match.group(2))
        last = _cell_reference(match.group(3) + match.group(4))
        if last[0] < first[0] or last[1] < first[1] or (last[0] - first[0] + 1) * (last[1] - first[1] + 1) > 100_000:
            raise GenerationError("SPREADSHEET_FORMULA_RANGE_INVALID", "A calculation range is reversed or too large.")
        if first[0] < 2 or last[0] > len(rows) or last[1] > len(rows[0]):
            raise GenerationError("SPREADSHEET_FORMULA_RANGE_INVALID", "A calculation range must stay inside supplied data rows.")
        values: list[Decimal] = []
        for row_number in range(first[0], last[0] + 1):
            for column_number in range(first[1], last[1] + 1):
                if row_number <= len(rows) and column_number <= len(rows[row_number - 1]):
                    value = _number_value(rows[row_number - 1][column_number - 1])
                    if value is not None:
                        values.append(value)
        if function == "COUNT":
            value = Decimal(len(values))
        elif not values:
            raise GenerationError("SPREADSHEET_FORMULA_EMPTY_RANGE", "A calculation range has no numeric values.")
        elif function == "SUM":
            value = sum(values, Decimal(0))
        elif function == "AVERAGE":
            value = sum(values, Decimal(0)) / len(values)
        elif function == "MIN":
            value = min(values)
        else:
            value = max(values)
        if len(value.as_tuple().digits) > 15:
            raise GenerationError("SPREADSHEET_FORMULA_PRECISION_UNSUPPORTED", "A calculation exceeds Excel's reliable numeric precision.")
        results.append({
            "cell": cell,
            "row": target[0],
            "column": target[1],
            "formula": f"{function}({item['range']})",
            "value": value,
        })
    return results


def _safe_sheet_name(name: str, used: set[str]) -> str:
    value = re.sub(r"[\\/*?:\[\]]", " ", name).strip().strip("'") or "Sheet"
    value = value[:31]
    candidate = value
    suffix = 2
    while candidate.casefold() in used:
        ending = f" {suffix}"
        candidate = value[:31 - len(ending)] + ending
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _numeric_columns(rows: list[list[str]]) -> list[int]:
    if len(rows) < 3 or not rows[0]:
        return []
    columns = []
    for column in range(1, len(rows[0])):
        values = [_number_value(row[column]) for row in rows[1:] if column < len(row) and row[column].strip()]
        if len(values) >= 2 and all(value is not None for value in values):
            columns.append(column)
    return columns


def _workbook_structure(title: str, body: str, citations: list[str]) -> tuple[list[dict], list[dict]]:
    blocks = _dedupe_title_heading(parse_markdown(body), title)
    used = {"overview"}
    overview: list[tuple[str, str]] = [("heading", title)]
    tables = []
    heading = ""
    table_index = 0
    for block in blocks:
        if block["type"] == "heading":
            heading = block["text"]
            overview.append(("heading", heading))
        elif block["type"] == "table":
            table_index += 1
            name = _safe_sheet_name(heading or f"Data {table_index}", used)
            tables.append({"table_index": table_index, "name": name, "rows": block["rows"]})
            overview.append(("paragraph", f"Table {table_index}: {name}"))
            heading = ""
        else:
            overview.extend(("paragraph", line) for line in block["text"].splitlines() or [""])
    if citations:
        overview.append(("heading", "Sources"))
        overview.extend(("paragraph", value.removeprefix("- ")) for value in citations)
    if not tables:
        tables = []
    return overview, tables


def render_xlsx(title: str, body: str, citations: list[str], input_snapshot: dict | None = None) -> bytes:
    import xlsxwriter

    overview, tables = _workbook_structure(title, body, citations)
    formulas = _spreadsheet_formulas(input_snapshot)
    known_tables = {table["table_index"] for table in tables}
    if any(item["table_index"] not in known_tables for item in formulas):
        raise GenerationError("SPREADSHEET_FORMULA_TABLE_UNAVAILABLE", "A calculation refers to a table that is not present in this Artifact.")
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {
        "in_memory": True,
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    workbook.set_properties({"title": title, "author": "", "created": datetime(2000, 1, 1)})
    workbook.set_calc_mode("auto")
    heading_format = workbook.add_format({"bold": True, "font_size": 14, "text_wrap": True})
    body_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    header_format = workbook.add_format({"bold": True, "text_wrap": True, "valign": "top"})
    numeric_format = workbook.add_format({"num_format": "0.###############", "valign": "top"})

    summary = workbook.add_worksheet("Overview")
    summary.set_column("A:A", 88)
    for row_index, (kind, text) in enumerate(overview):
        summary.write_string(row_index, 0, text, heading_format if kind == "heading" else body_format)
    summary.freeze_panes(1, 0)

    expected_formulas: dict[int, list[dict]] = {}
    for table in tables:
        rows = table["rows"]
        if not rows or not rows[0]:
            raise GenerationError("SPREADSHEET_TABLE_EMPTY", "A spreadsheet table has no header row.")
        columns = len(rows[0])
        if any(len(row) != columns for row in rows):
            raise GenerationError("SPREADSHEET_TABLE_SHAPE_INVALID", "Every row in a spreadsheet table must match its header.")
        if any(len(cell) > 32_767 for row in rows for cell in row):
            raise GenerationError("SPREADSHEET_CELL_TOO_LARGE", "A spreadsheet cell exceeds Excel's text limit.")
        sheet = workbook.add_worksheet(table["name"])
        sheet.freeze_panes(1, 0)
        sheet.set_default_row(18)
        for column, header in enumerate(rows[0]):
            width = min(max(len(header) + 2, 12), 36)
            sheet.set_column(column, column, width, body_format)
        data_rows = []
        for row in rows[1:]:
            data_row = []
            for value in row:
                numeric = _number_value(value)
                if numeric is None or len(numeric.as_tuple().digits) > 15:
                    data_row.append(value)
                else:
                    data_row.append(float(numeric))
            data_rows.append(data_row)
        table_name = f"OeviraTable{table['table_index']}"
        if data_rows:
            sheet.add_table(0, 0, len(data_rows), columns - 1, {
                "name": table_name,
                "style": "Table Style Medium 2",
                "columns": [{"header": value} for value in rows[0]],
                "data": data_rows,
                "autofilter": True,
            })
        else:
            sheet.write_row(0, 0, rows[0], header_format)
        results = _formula_results(table["table_index"], rows, formulas)
        expected_formulas[table["table_index"]] = results
        for formula in results:
            if formula["row"] <= len(rows):
                raise GenerationError("SPREADSHEET_FORMULA_TARGET_OCCUPIED", "A calculation cell must be below or outside the supplied table.")
            cached = float(formula["value"])
            if not math.isfinite(cached):
                raise GenerationError("SPREADSHEET_FORMULA_INVALID_RESULT", "A calculation returned a non-finite number.")
            sheet.write_formula(formula["row"] - 1, formula["column"] - 1,
                                "=" + formula["formula"], numeric_format, cached)

        for column in _numeric_columns(rows):
            chart = workbook.add_chart({"type": "column"})
            chart.add_series({
                "name": [table["name"], 0, column],
                "categories": [table["name"], 1, 0, len(rows) - 1, 0],
                "values": [table["name"], 1, column, len(rows) - 1, column],
            })
            chart.set_title({"name": rows[0][column]})
            chart.set_x_axis({"name": rows[0][0]})
            chart.set_y_axis({"name": rows[0][column], "major_gridlines": {"visible": True}})
            chart.set_legend({"none": True})
            chart.set_size({"width": 720, "height": 360})
            chart.show_blanks_as("gap")
            sheet.insert_chart("J2", chart, {"x_offset": (column - 1) * 24})
    workbook.close()
    content = output.getvalue()
    if not content.startswith(b"PK"):
        raise GenerationError("XLSX_WRITE_FAILED", "The workbook writer did not produce a valid Office package.")
    return content


def validate_xlsx(content: bytes, title: str, body: str, citations: list[str],
                  input_snapshot: dict | None = None) -> bool:
    try:
        from openpyxl import load_workbook

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None or "xl/workbook.xml" not in archive.namelist():
                return False
        formula_book = load_workbook(io.BytesIO(content), data_only=False, read_only=False)
        value_book = load_workbook(io.BytesIO(content), data_only=True, read_only=False)
        overview, tables = _workbook_structure(title, body, citations)
        formulas = _spreadsheet_formulas(input_snapshot)
        required = [title, *(text for _kind, text in overview),
                    *(cell for table in tables for row in table["rows"] for cell in row if cell)]
        observed = []
        for sheet in formula_book.worksheets:
            for row in sheet.iter_rows():
                observed.extend(str(cell.value) for cell in row if cell.value is not None)
        normalized = re.sub(r"\s+", "", "\n".join(observed))
        if not all(re.sub(r"\s+", "", text) in normalized for text in required if text):
            return False
        formula_count = 0
        for table in tables:
            worksheet = formula_book[table["name"]]
            values_sheet = value_book[table["name"]]
            results = _formula_results(table["table_index"], table["rows"], formulas)
            formula_count += len(results)
            for result in results:
                if worksheet[result["cell"]].value != "=" + result["formula"]:
                    return False
                cached = values_sheet[result["cell"]].value
                if cached is None or Decimal(str(cached)) != result["value"]:
                    return False
            if _numeric_columns(table["rows"]) and not worksheet._charts:
                return False
        if formula_count != sum(len(_formula_results(table["table_index"], table["rows"], formulas)) for table in tables):
            return False
        formula_book.close()
        value_book.close()
        return True
    except Exception:
        return False


def _display_width(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1


def _wrap_text(value: str, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in value.splitlines() or [""]:
        current: list[str] = []
        used = 0
        for character in paragraph:
            size = _display_width(character)
            if current and used + size > width:
                lines.append("".join(current))
                current = []
                used = 0
            current.append(character)
            used += size
        lines.append("".join(current))
    return lines


def _pptx_textbox(slide, text: str, left: float, top: float, width: float, height: float,
                  *, size: int = 16, bold: bool = False, color=None):
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches, Pt

    shape = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = shape.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = Inches(0.04)
    frame.margin_right = Inches(0.04)
    frame.margin_top = Inches(0.02)
    frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = MSO_ANCHOR.TOP
    for index, line in enumerate(text.splitlines() or [""]):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = line
        paragraph.alignment = PP_ALIGN.LEFT
        paragraph.font.size = Pt(size)
        paragraph.font.bold = bold
        if color is not None:
            paragraph.font.color.rgb = RGBColor(*color)
    return shape


def _pptx_new_slide(presentation, title: str | None = None):
    from pptx.util import Inches, Pt

    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    if title:
        _pptx_textbox(slide, title, 0.6, 0.35, 12.1, 0.72, size=24, bold=True)
    return slide


def _add_pptx_text_slides(presentation, title: str, paragraphs: list[str]) -> None:
    current_title = title
    page = 1
    chunks: list[str] = []
    line_count = 0
    for text in paragraphs:
        wrapped = _wrap_text(text, 88)
        if not wrapped:
            wrapped = [""]
        offset = 0
        while offset < len(wrapped):
            available = max(1, 16 - line_count)
            selected = wrapped[offset:offset + available]
            if line_count + len(selected) > 16 or (line_count and len(selected) == 0):
                slide = _pptx_new_slide(presentation, current_title if page == 1 else f"{current_title} · {page}")
                _pptx_textbox(slide, "\n\n".join(chunks), 0.7, 1.35, 11.9, 5.55, size=15)
                page += 1
                chunks = []
                line_count = 0
                continue
            chunks.append("\n".join(selected))
            line_count += len(selected) + 1
            offset += len(selected)
            if offset < len(wrapped):
                slide = _pptx_new_slide(presentation, f"{current_title} · {page}")
                _pptx_textbox(slide, "\n\n".join(chunks), 0.7, 1.35, 11.9, 5.55, size=15)
                page += 1
                chunks = []
                line_count = 0
    if chunks:
        slide = _pptx_new_slide(presentation, current_title if page == 1 else f"{current_title} · {page}")
        _pptx_textbox(slide, "\n\n".join(chunks), 0.7, 1.35, 11.9, 5.55, size=15)


def _pptx_table_slides(presentation, title: str, table: list[list[str]], table_index: int) -> None:
    from pptx.util import Inches, Pt

    if not table:
        return
    columns = len(table[0])
    # Wide tables repeat their row key and continue on later slides; no cells are discarded.
    column_groups = [list(range(columns))] if columns <= 7 else [
        [0, *range(start, min(start + 6, columns))] for start in range(1, columns, 6)
    ]
    row_start = 1
    while row_start < len(table) or row_start == 1:
        row_end = min(row_start + 7, len(table))
        slide_title = f"{title} · Table {table_index}"
        if len(column_groups) > 1:
            slide_title += f" · 列 {column_groups[0][0] + 1}–{column_groups[0][-1] + 1}/{columns}"
        if row_start > 1:
            slide_title += f" · 行 {row_start}–{row_end - 1}"
        for group_index, group in enumerate(column_groups):
            if group_index:
                slide_title = f"{title} · Table {table_index} · 列 {group[0] + 1}–{group[-1] + 1}/{columns}"
                if row_start > 1:
                    slide_title += f" · 行 {row_start}–{row_end - 1}"
            slide = _pptx_new_slide(presentation, slide_title)
            rows = [table[0], *table[row_start:row_end]]
            ppt_table = slide.shapes.add_table(
                len(rows), len(group), Inches(0.55), Inches(1.35), Inches(12.2), Inches(5.55),
            ).table
            for column_index, original_column in enumerate(group):
                ppt_table.columns[column_index].width = Inches(12.2 / len(group))
            for row_index, row in enumerate(rows):
                max_lines = 1
                rendered: list[str] = []
                for original_column in group:
                    value = row[original_column] if original_column < len(row) else ""
                    wrapped = _wrap_text(value, max(6, int(86 / len(group))))
                    rendered.append("\n".join(wrapped))
                    max_lines = max(max_lines, len(wrapped))
                ppt_table.rows[row_index].height = Inches(min(1.15, max(0.42, 0.2 + max_lines * 0.18)))
                for column_index, value in enumerate(rendered):
                    cell = ppt_table.cell(row_index, column_index)
                    cell.text = value
                    cell.margin_left = Inches(0.05)
                    cell.margin_right = Inches(0.05)
                    cell.margin_top = Inches(0.02)
                    cell.margin_bottom = Inches(0.02)
                    cell.text_frame.word_wrap = True
                    for paragraph in cell.text_frame.paragraphs:
                        paragraph.font.size = Pt(10 if row_index else 11)
                        paragraph.font.bold = row_index == 0
        if row_end >= len(table):
            break
        row_start = row_end


def _pptx_chart_slides(presentation, title: str, table: list[list[str]], table_index: int) -> int:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Inches, Pt

    if len(table) < 3 or not table[0]:
        return 0
    categories = [row[0] if row else "" for row in table[1:]]
    added = 0
    for column in _numeric_columns(table):
        values = [_number_value(row[column]) if column < len(row) else None for row in table[1:]]
        if any(value is None or len(value.as_tuple().digits) > 15 for value in values):
            continue
        if len(categories) > 30:
            continue
        data = CategoryChartData()
        data.categories = categories
        data.add_series(table[0][column], [float(value) for value in values])
        slide = _pptx_new_slide(presentation, f"{title} · Table {table_index} · {table[0][column]}")
        chart_shape = slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.8), Inches(1.25), Inches(11.7), Inches(5.55), data,
        )
        chart = chart_shape.chart
        chart.has_title = True
        chart.chart_title.text_frame.text = table[0][column]
        chart.has_legend = False
        chart.value_axis.has_major_gridlines = True
        chart.value_axis.tick_labels.font.size = Pt(10)
        chart.category_axis.tick_labels.font.size = Pt(10)
        added += 1
    return added


def render_pptx(title: str, body: str, citations: list[str], input_snapshot: dict | None = None) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    blocks = _dedupe_title_heading(parse_markdown(body, citations), title)
    if not _required_text(blocks):
        raise GenerationError("PPTX_CONTENT_EMPTY", "A presentation needs saved content beyond its title.")
    presentation = Presentation()
    presentation.slide_width = Inches(13.333333)
    presentation.slide_height = Inches(7.5)
    presentation.core_properties.title = title
    presentation.core_properties.author = ""
    presentation.core_properties.last_modified_by = ""
    presentation.core_properties.created = datetime(2000, 1, 1)
    presentation.core_properties.modified = datetime(2000, 1, 1)

    cover = _pptx_new_slide(presentation)
    _pptx_textbox(cover, title, 0.75, 1.4, 11.8, 1.2, size=32, bold=True)

    section_title = title
    paragraphs: list[str] = []
    table_index = 0

    def flush_paragraphs() -> None:
        nonlocal paragraphs
        if paragraphs:
            _add_pptx_text_slides(presentation, section_title, paragraphs)
            paragraphs = []

    for block in blocks:
        if block["type"] == "heading":
            flush_paragraphs()
            section_title = block["text"]
        elif block["type"] == "table":
            flush_paragraphs()
            table_index += 1
            _pptx_chart_slides(presentation, section_title, block["rows"], table_index)
            _pptx_table_slides(presentation, section_title, block["rows"], table_index)
        else:
            paragraphs.append(block["text"])
    flush_paragraphs()
    if len(presentation.slides) < 2:
        raise GenerationError("PPTX_CONTENT_INCOMPLETE", "The saved content did not produce a readable presentation body.")
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def validate_pptx(content: bytes, title: str, body: str, citations: list[str],
                  input_snapshot: dict | None = None) -> bool:
    try:
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(content))
        if len(presentation.slides) < 2:
            return False
        observed = []
        charts = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    observed.append(shape.text_frame.text)
                if getattr(shape, "has_table", False):
                    observed.extend(cell.text for row in shape.table.rows for cell in row.cells)
                if getattr(shape, "has_chart", False):
                    charts.append(shape.chart)
        flattened = re.sub(r"\s+", "", "\n".join(observed))
        required = [title, *_required_text(parse_markdown(body, citations))]
        if not all(re.sub(r"\s+", "", text) in flattened for text in required if text):
            return False
        expected_charts = []
        for block in _dedupe_title_heading(parse_markdown(body, citations), title):
            if block["type"] != "table" or len(block["rows"]) - 1 > 30:
                continue
            rows = block["rows"]
            for column in _numeric_columns(rows):
                values = [_number_value(row[column]) for row in rows[1:]]
                if all(value is not None and len(value.as_tuple().digits) <= 15 for value in values):
                    expected_charts.append(([row[0] for row in rows[1:]], values))
        if len(charts) != len(expected_charts):
            return False
        for chart, (categories, values) in zip(charts, expected_charts):
            if len(chart.series) != 1 or len(chart.series[0].values) != len(values):
                return False
            actual_categories = [chart.plots[0].categories[index].label for index in range(len(categories))]
            if actual_categories != categories:
                return False
            if any(abs(Decimal(str(actual)) - expected) > Decimal("1e-12")
                   for actual, expected in zip(chart.series[0].values, values)):
                return False
        return True
    except Exception:
        return False


_PDF_EXPORT_LOCK = threading.RLock()
_PDF_FONT_DIRECTORY = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_PDF_FONTS = (("OEVIRA_NotoSC", "NotoSansSC.ttf"), ("OEVIRA_NotoKR", "NotoSansKR.ttf"))


def _pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts = []
    for name, filename in _PDF_FONTS:
        path = _PDF_FONT_DIRECTORY / filename
        if not path.is_file():
            raise GenerationError("PDF_FONT_UNAVAILABLE", f"The licensed PDF font asset is unavailable: {filename}.")
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        fonts.append(pdfmetrics.getFont(name))
    return fonts


def _pdf_markup(value: str, fonts) -> str:
    pieces: list[str] = []
    current_name = None
    current_chars: list[str] = []

    def flush() -> None:
        nonlocal current_name, current_chars
        if current_chars:
            pieces.append(f'<font name="{current_name}">{html_escape("".join(current_chars))}</font>')
            current_name = None
            current_chars = []

    for character in str(value).replace("\t", "    "):
        if character in "\r\n":
            flush()
            if character == "\n":
                pieces.append("<br/>")
            continue
        font = next((item for item in fonts if item.face.charToGlyph.get(ord(character), 0)), None)
        if font is None:
            raise GenerationError("PDF_GLYPH_UNAVAILABLE", f"No approved PDF font contains U+{ord(character):04X}.")
        if current_name == font.fontName:
            current_chars.append(character)
        else:
            flush()
            current_name = font.fontName
            current_chars = [character]
    flush()
    return "".join(pieces) or "&#160;"


def _pdf_table_chunks(table: list[list[str]], max_columns: int = 6) -> list[tuple[list[list[str]], str]]:
    if not table:
        return []
    columns = len(table[0])
    if columns <= max_columns:
        return [(table, "")]
    chunks = []
    for start in range(1, columns, max_columns - 1):
        selected = [0, *range(start, min(start + max_columns - 1, columns))]
        chunk = [[row[index] if index < len(row) else "" for index in selected] for row in table]
        chunks.append((chunk, f"Columns {selected[0] + 1}–{selected[-1] + 1} of {columns}"))
    return chunks


def _pdf_chart(table: list[list[str]], numeric_column: int, fonts):
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing

    rows = table[1:]
    categories = [row[0] for row in rows]
    values = [_number_value(row[numeric_column]) for row in rows]
    if len(values) < 2 or any(value is None or len(value.as_tuple().digits) > 15 for value in values):
        return None
    drawing = Drawing(480, 235)
    chart = VerticalBarChart()
    chart.x = 65
    chart.y = 42
    chart.width = 385
    chart.height = 160
    chart.data = [[float(value) for value in values]]
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontName = fonts[0].fontName
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.labels.fontName = fonts[0].fontName
    chart.valueAxis.labels.fontSize = 8
    chart.valueAxis.valueMin = min(0, min(float(value) for value in values))
    chart.valueAxis.valueMax = max(0, max(float(value) for value in values))
    if chart.valueAxis.valueMax == chart.valueAxis.valueMin:
        chart.valueAxis.valueMax = chart.valueAxis.valueMin + 1
    chart.valueAxis.valueStep = (chart.valueAxis.valueMax - chart.valueAxis.valueMin) / 5
    chart.barWidth = min(28, 280 / len(values))
    chart.barSpacing = max(2, min(12, 270 / len(values) - chart.barWidth))
    chart.bars[0].fillColor = None
    drawing.add(chart)
    return drawing


def render_pdf(title: str, body: str, citations: list[str], input_snapshot: dict | None = None) -> bytes:
    from reportlab import rl_config
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import LongTable, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, TableStyle

    blocks = _dedupe_title_heading(parse_markdown(body, citations), title)
    if not _required_text(blocks):
        raise GenerationError("PDF_CONTENT_EMPTY", "A PDF needs saved content beyond its title.")
    output = io.BytesIO()
    with _PDF_EXPORT_LOCK:
        fonts = _pdf_fonts()
        previous_invariant = rl_config.invariant
        rl_config.invariant = 1
        try:
            normal = ParagraphStyle("OeviraBody", fontName=fonts[0].fontName, fontSize=10,
                                    leading=15, wordWrap="CJK", spaceAfter=7)
            heading1 = ParagraphStyle("OeviraHeading1", parent=normal, fontSize=17,
                                      leading=23, spaceBefore=12, spaceAfter=8, keepWithNext=True)
            heading2 = ParagraphStyle("OeviraHeading2", parent=normal, fontSize=13,
                                      leading=18, spaceBefore=9, spaceAfter=6, keepWithNext=True)
            heading3 = ParagraphStyle("OeviraHeading3", parent=normal, fontSize=11,
                                      leading=16, spaceBefore=7, spaceAfter=5, keepWithNext=True)
            title_style = ParagraphStyle("OeviraTitle", parent=normal, fontSize=23,
                                         leading=30, spaceAfter=18, keepWithNext=True)
            code_style = ParagraphStyle("OeviraCode", parent=normal, fontName=fonts[0].fontName,
                                        fontSize=8, leading=11, leftIndent=10, rightIndent=10,
                                        backColor=colors.HexColor("#F2F4F7"), borderPadding=6)
            story = [Paragraph(_pdf_markup(title, fonts), title_style)]
            data_table_number = 0
            for block in blocks:
                if block["type"] == "heading":
                    style = heading1 if block["level"] <= 1 else heading2 if block["level"] == 2 else heading3
                    story.append(Paragraph(_pdf_markup(block["text"], fonts), style))
                elif block["type"] == "table":
                    data_table_number += 1
                    for table_part, column_note in _pdf_table_chunks(block["rows"]):
                        if column_note:
                            story.append(Paragraph(_pdf_markup(f"Table {data_table_number} · {column_note}", fonts), heading3))
                        cells = [[Paragraph(_pdf_markup(value, fonts), normal) for value in row] for row in table_part]
                        width = A4[0] - 3 * cm
                        table = LongTable(cells, colWidths=[width / len(table_part[0])] * len(table_part[0]),
                                          repeatRows=1, splitByRow=1)
                        table.setStyle(TableStyle([
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EDF2")),
                            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9AA3AF")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ]))
                        story.extend([table, Spacer(1, 8)])
                    if len(block["rows"]) >= 3:
                        for column in _numeric_columns(block["rows"]):
                            chart = _pdf_chart(block["rows"], column, fonts)
                            if chart is not None:
                                story.extend([Paragraph(_pdf_markup(block["rows"][0][column], fonts), heading3), chart, Spacer(1, 8)])
                elif block["type"] == "code":
                    story.append(Preformatted(block["text"], code_style, maxLineLength=100))
                elif block["text"]:
                    story.append(Paragraph(_pdf_markup(block["text"], fonts), normal))
                else:
                    story.append(Spacer(1, 6))

            def page_decor(canvas, document):
                canvas.saveState()
                canvas.setFont(fonts[0].fontName, 8)
                canvas.drawRightString(A4[0] - 1.5 * cm, 0.8 * cm, str(document.page))
                canvas.restoreState()

            pdf = SimpleDocTemplate(output, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                                    topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                                    title=title, author="")
            pdf.build(story, onFirstPage=page_decor, onLaterPages=page_decor)
        finally:
            rl_config.invariant = previous_invariant
    return output.getvalue()


def validate_pdf(content: bytes, title: str, body: str, citations: list[str]) -> bool:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not pdf.pages:
                return False
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        normalized = re.sub(r"\s+", "", text)
        required = [title, *_required_text(parse_markdown(body, citations))]
        return all(re.sub(r"\s+", "", value) in normalized for value in required if value)
    except Exception:
        return False


