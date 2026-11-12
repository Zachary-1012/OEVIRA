"""Bounded, read-only extraction of user-provided research materials.

Offsets are Unicode codepoint offsets into the returned normalized ``text``
projection. A segment's text is always a verbatim substring of that projection;
its label records the source structure used for a human-readable citation.
The original input bytes are never changed by this module.
"""

from __future__ import annotations

import csv
import io
import os
import posixpath
import re
import stat
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET


MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_SEGMENTS = 100_000
MAX_CSV_ROWS = 100_000
MAX_WORKBOOK_CELLS = 100_000
MAX_WORKBOOK_SHEETS = 256
MAX_PDF_PAGES = 500
MAX_ZIP_ENTRIES = 4_096
MAX_ZIP_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 80 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
_XML_FORBIDDEN = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


class _Unavailable(Exception):
    """The input is valid enough to identify but this parser cannot read it."""


class _Malformed(Exception):
    """The bytes do not form a valid supported material."""


class _SegmentBuilder:
    def __init__(self) -> None:
        self.segments: list[dict] = []
        self._parts: list[str] = []
        self._offset = 0
        self.truncated = False

    def add(self, label: str, value: str, kind: str) -> bool:
        if not isinstance(value, str):
            value = str(value)
        if len(self.segments) >= MAX_SEGMENTS:
            self.truncated = True
            return False
        separator = "\n" if self.segments else ""
        remaining = MAX_TEXT_CHARS - self._offset - len(separator)
        if remaining < 0:
            self.truncated = True
            return False
        if len(value) > remaining:
            value = value[:remaining]
            self.truncated = True
        self._parts.append(separator)
        self._offset += len(separator)
        start = self._offset
        self._parts.append(value)
        self._offset += len(value)
        self.segments.append({
            "id": f"seg-{len(self.segments) + 1:06d}",
            "label": label,
            "text": value,
            "start": start,
            "end": self._offset,
            "kind": kind,
        })
        return not self.truncated

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _result(status: str, reason: str | None, format_name: str,
            builder: _SegmentBuilder | None = None) -> dict:
    builder = builder or _SegmentBuilder()
    if builder.truncated:
        status = "partial" if builder.text.strip() else "unavailable"
        limit_reason = "已达到安全解析上限，后续内容未读取。"
        reason = (reason if "安全解析上限" in reason else f"{reason}；{limit_reason}") if reason else limit_reason
    if status in {"ready", "partial"} and not builder.text.strip():
        status = "unavailable"
        reason = reason or "文件中没有可读取的非空文字。"
    return {
        "status": status,
        "reason": reason,
        "format": format_name,
        "text": builder.text,
        "segments": builder.segments,
    }


def _failure(status: str, reason: str, format_name: str) -> dict:
    return _result(status, reason, format_name)


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _finish(builder: _SegmentBuilder, reasons: list[str], format_name: str) -> dict:
    if builder.truncated:
        _append_reason(reasons, "已达到安全解析上限，后续内容未读取。")
    if not builder.text.strip():
        return _result("unavailable", "；".join(reasons) or "文件中没有可读取的非空文字。",
                       format_name, builder)
    return _result("partial" if reasons else "ready", "；".join(reasons) or None,
                   format_name, builder)


def parse_material(filename: str, content: bytes) -> dict:
    """Parse one in-memory material without filesystem, database or network I/O."""
    if not isinstance(filename, str) or not filename.strip():
        return _failure("error", "文件名缺失。", "unknown")
    if not isinstance(content, bytes):
        return _failure("error", "材料内容必须是原始字节。", _format_from_name(filename))

    format_name = _format_from_name(filename)
    if len(content) > MAX_INPUT_BYTES:
        return _failure("unavailable", "文件超过当前安全解析大小上限。", format_name)

    try:
        if format_name in {"txt", "md"}:
            return _parse_text(content, format_name)
        if format_name in {"html", "htm", "xhtml"}:
            return _parse_html(content, format_name)
        if format_name == "csv":
            return _parse_csv(content)
        if format_name == "docx":
            return _parse_docx(content)
        if format_name == "pdf":
            return _parse_pdf(content)
        if format_name == "xlsx":
            return _parse_xlsx(content)
        if format_name == "pptx":
            return _parse_pptx(content)
        return _failure("unavailable", f"暂不支持 {format_name} 格式解析。", format_name)
    except _Unavailable as exc:
        return _failure("unavailable", str(exc), format_name)
    except _Malformed as exc:
        return _failure("error", str(exc), format_name)
    except Exception as exc:  # malformed user files are a per-file result, not a batch crash
        return _failure("error", f"文件解析失败（{type(exc).__name__}）。", format_name)


def _format_from_name(filename: str) -> str:
    extension = os.path.splitext(filename)[1].lower().lstrip(".")
    return extension or "unknown"


def _parse_text(content: bytes, format_name: str) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _Malformed("文本不是有效 UTF-8，未生成空白解析结果。") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    builder = _SegmentBuilder()
    for line_number, line in enumerate(text.split("\n"), 1):
        if line.strip() and not builder.add(f"第 {line_number} 行", line, "text_line"):
            break
    return _finish(builder, [], format_name)


def _parse_html(content: bytes, format_name: str) -> dict:
    from html.parser import HTMLParser
    from .acquisition import _readable_html

    class EncodingDeclaration(HTMLParser):
        encoding = None

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "meta" or self.encoding:
                return
            values = dict(attrs)
            if values.get("charset"):
                self.encoding = values["charset"]
            elif (values.get("http-equiv") or "").lower() == "content-type":
                match = re.search(r"charset\s*=\s*([\w.-]+)", values.get("content") or "", re.I)
                if match:
                    self.encoding = match.group(1)

        def handle_pi(self, data):
            if not self.encoding and data.lower().startswith("xml"):
                match = re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", data, re.I)
                if match:
                    self.encoding = match.group(1)

    declaration = EncodingDeclaration()
    declaration.feed(content[:65536].decode("ascii", errors="ignore"))
    encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else declaration.encoding or "utf-8-sig"
    try:
        decoded = content.decode(encoding)
    except (LookupError, UnicodeError) as error:
        raise _Malformed("HTML编码声明无法正确解码，原件保留，未把乱码当正文。") from error
    projection = _readable_html(decoded, "")
    builder = _SegmentBuilder()
    for number, line in enumerate(projection["text"].splitlines(), 1):
        if line.strip() and not builder.add(f"HTML静态文本第 {number} 行", line, "html_static_text"):
            break
    reasons = []
    if projection["extraction_scope"] != "static_document_body":
        reasons.append("本次解析页面声明的主内容区域，未把其它区域或动态内容冒充已读取。")
    return _finish(builder, reasons, format_name)


def _csv_scan(text: str, delimiter: str) -> tuple[set[int], int, bool]:
    widths: set[int] = set()
    rows = 0
    has_multiple = False
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
            rows += 1
            if rows > MAX_CSV_ROWS:
                raise _Unavailable("CSV 超过当前安全行数上限。")
            if row:
                widths.add(len(row))
                has_multiple = has_multiple or len(row) > 1
    except csv.Error as exc:
        raise _Malformed("CSV 引号或行结构无效。") from exc
    return widths, rows, has_multiple


def _column_name(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _parse_csv(content: bytes) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _Malformed("CSV 不是有效 UTF-8，未生成空白解析结果。") from exc

    candidates: list[str] = []
    for delimiter in (",", ";", "\t"):
        try:
            widths, _, multiple = _csv_scan(text, delimiter)
        except _Malformed:
            continue
        if multiple and len(widths) == 1:
            candidates.append(delimiter)
    if len(candidates) > 1:
        raise _Unavailable("CSV 分隔符存在多种同样合理的解释，需先明确格式。")
    if candidates:
        delimiter = candidates[0]
        inferred = True
    else:
        delimiter = ","
        widths, _, multiple = _csv_scan(text, delimiter)
        if multiple and len(widths) > 1:
            raise _Malformed("CSV 各行列数不一致，未静默补齐或扁平化。")
        inferred = False

    header: list[str] | None = None
    header_index: int | None = None
    for row_index, row in enumerate(
            csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True), 1):
        if row and sum(bool(cell.strip()) for cell in row) >= 2:
            header = row
            header_index = row_index
            break

    builder = _SegmentBuilder()
    try:
        rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        for row_index, row in enumerate(rows, 1):
            for column_index, cell in enumerate(row, 1):
                address = f"{_column_name(column_index)}{row_index}"
                label = f"第 {row_index} 行，第 {column_index} 列（{address}）"
                if (header and header_index is not None and row_index > header_index
                        and column_index <= len(header) and header[column_index - 1].strip()):
                    label += f"；首个非空行的候选列名“{header[column_index - 1]}”"
                if not builder.add(label,
                                   cell, "csv_cell"):
                    break
            if builder.truncated:
                break
    except csv.Error as exc:
        raise _Malformed("CSV 引号或行结构无效。") from exc
    note = "CSV 分隔符依据表格结构推定，来源未声明分隔符。" if inferred else None
    # Format detection is an explained assumption, not missing source content.
    return _result("ready", note, "csv", builder)


def _validate_zip(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise _Unavailable("Office 压缩包成员数量超过安全上限。")
    by_name: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        name = info.filename
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if (not normalized or normalized.startswith("/") or "\\" in name
                or re.match(r"^[A-Za-z]:", normalized)
                or any(part == ".." for part in parts)):
            raise _Malformed("Office 压缩包含有不安全的成员路径。")
        if name in by_name:
            raise _Malformed("Office 压缩包含有重复成员路径。")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise _Malformed("Office 压缩包包含符号链接成员。")
        if info.flag_bits & 0x1:
            raise _Unavailable("Office 压缩包已加密，无法安全读取。")
        if info.file_size < 0 or info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise _Unavailable("Office 压缩包单个成员超过安全解压上限。")
        total_size += info.file_size
        if total_size > MAX_ZIP_TOTAL_BYTES:
            raise _Unavailable("Office 压缩包展开总量超过安全上限。")
        by_name[name] = info
    return by_name


def _open_office(content: bytes, required: tuple[str, ...]) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise _Malformed("Office 文件不是有效的 ZIP/OOXML 文档。") from exc
    try:
        by_name = _validate_zip(archive)
        if any(name not in by_name for name in required):
            raise _Malformed("Office 文件缺少必要的 OOXML 部件。")
        return archive, by_name
    except Exception:
        archive.close()
        raise


def _read_zip_member(archive: zipfile.ZipFile, by_name: dict[str, zipfile.ZipInfo],
                     name: str, *, xml: bool = False) -> bytes:
    info = by_name.get(name)
    if info is None:
        raise _Malformed(f"Office 文件缺少部件 {name}。")
    limit = min(MAX_XML_BYTES, MAX_ZIP_MEMBER_BYTES) if xml else MAX_ZIP_MEMBER_BYTES
    if info.file_size > limit:
        raise _Unavailable("Office 文件中的单个内容部件超过安全读取上限。")
    raw = bytearray()
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(min(64 * 1024, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > limit:
                    raise _Unavailable("Office 文件中的内容部件超过安全读取上限。")
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise _Malformed("Office 文件中的压缩部件损坏。") from exc
    if len(raw) != info.file_size:
        raise _Malformed("Office 文件中的压缩部件长度与目录不一致。")
    return bytes(raw)


def _xml_part(archive: zipfile.ZipFile, by_name: dict[str, zipfile.ZipInfo], name: str) -> ET.Element:
    raw = _read_zip_member(archive, by_name, name, xml=True)
    if b"\x00" in raw or _XML_FORBIDDEN.search(raw):
        raise _Malformed("Office XML 含有不允许的编码、DTD 或实体声明。")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise _Malformed("Office XML 结构无效。") from exc


def _has_external_relationships(archive: zipfile.ZipFile,
                               by_name: dict[str, zipfile.ZipInfo]) -> bool:
    for name in by_name:
        if not name.lower().endswith(".rels"):
            continue
        root = _xml_part(archive, by_name, name)
        for relationship in root.iter():
            if (relationship.tag.endswith("Relationship")
                    and relationship.attrib.get("TargetMode", "").lower() == "external"):
                return True
    return False


def _internal_target(source_part: str, target: str) -> str | None:
    if not target or target.startswith("//") or target.startswith("\\") or "\\" in target:
        return None
    if target.startswith("/"):
        joined = posixpath.normpath(target.lstrip("/"))
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None
    return joined


def _docx_text(content: bytes, builder: _SegmentBuilder, reasons: list[str]) -> None:
    archive, by_name = _open_office(content, ("word/document.xml",))
    try:
        document_root = _xml_part(archive, by_name, "word/document.xml")
        if any(node.tag.rsplit("}", 1)[-1] in {"ins", "del", "moveFrom", "moveTo"}
               for node in document_root.iter()):
            _append_reason(reasons, "文档含有修订标记；解析结果不代表已接受或拒绝修订的最终视图。")
        for name in by_name:
            if name.lower().endswith((".xml", ".rels")):
                raw = _read_zip_member(archive, by_name, name, xml=True)
                if b"\x00" in raw or _XML_FORBIDDEN.search(raw):
                    raise _Malformed("DOCX XML 含有不允许的编码、DTD 或实体声明。")
        if any(name in by_name for name in (
                "word/comments.xml", "word/footnotes.xml", "word/endnotes.xml")):
            _append_reason(reasons, "文档含有未纳入正文解析的批注、脚注或尾注。")
        if _has_external_relationships(archive, by_name):
            _append_reason(reasons, "文档含有外部关系；解析器未访问外部目标。")
    finally:
        archive.close()
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
        from docx.table import Table
    except ImportError as exc:
        raise _Unavailable("当前运行环境没有已规划的 DOCX 解析组件。") from exc

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise _Malformed("DOCX 文档结构损坏或不受支持。") from exc

    paragraph_index = 0
    table_index = 0
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph_index += 1
            paragraph = Paragraph(child, document)
            if paragraph.text.strip():
                builder.add(f"第 {paragraph_index} 段", paragraph.text, "docx_paragraph")
            if child.xpath(".//w:drawing | .//w:pict | .//w:object"):
                _append_reason(reasons, "文档含有未提取的图像或嵌入对象。")
        elif child.tag == qn("w:tbl"):
            table_index += 1
            table = Table(child, document)
            header = [cell.text for cell in table.rows[0].cells] if table.rows else []
            for row_index, row in enumerate(table.rows, 1):
                for column_index, cell in enumerate(row.cells, 1):
                    value = cell.text
                    if value.strip():
                        label = f"表格 {table_index} 第 {row_index} 行第 {column_index} 列"
                        if row_index > 1 and column_index <= len(header) and header[column_index - 1].strip():
                            label += f"；首行候选列名“{header[column_index - 1]}”"
                        builder.add(label,
                                    value, "docx_table_cell")
                    if cell._tc.xpath(".//w:tbl"):
                        _append_reason(reasons, "文档含有嵌套表格，部分层级未展开。")
        if builder.truncated:
            break
    if document.sections and any(
            section.header.paragraphs and any(p.text.strip() for p in section.header.paragraphs)
            or section.footer.paragraphs and any(p.text.strip() for p in section.footer.paragraphs)
            for section in document.sections):
        _append_reason(reasons, "页眉或页脚文字未纳入正文解析。")


def _parse_docx(content: bytes) -> dict:
    builder = _SegmentBuilder()
    reasons: list[str] = []
    _docx_text(content, builder, reasons)
    return _finish(builder, reasons, "docx")


def _xlsx_date_style_ids(archive: zipfile.ZipFile,
                         by_name: dict[str, zipfile.ZipInfo]) -> set[int]:
    if "xl/styles.xml" not in by_name:
        return set()
    root = _xml_part(archive, by_name, "xl/styles.xml")
    custom: dict[int, bool] = {}
    num_fmts = root.find(f"{{{_NS_MAIN}}}numFmts")
    if num_fmts is not None:
        for item in num_fmts.findall(f"{{{_NS_MAIN}}}numFmt"):
            try:
                identifier = int(item.attrib.get("numFmtId", "-1"))
            except ValueError:
                continue
            code = item.attrib.get("formatCode", "")
            stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"|\\.|\[[^]]*\]', "", code)
            custom[identifier] = bool(re.search(r"[ydhms]", stripped, re.IGNORECASE))

    built_in = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
    result: set[int] = set()
    cell_xfs = root.find(f"{{{_NS_MAIN}}}cellXfs")
    if cell_xfs is not None:
        for style_index, xf in enumerate(cell_xfs.findall(f"{{{_NS_MAIN}}}xf")):
            try:
                num_format = int(xf.attrib.get("numFmtId", "0"))
            except ValueError:
                continue
            if num_format in built_in or custom.get(num_format, False):
                result.add(style_index)
    return result


def _excel_date(raw_value: str, date_1904: bool) -> str | None:
    try:
        serial = Decimal(raw_value)
    except InvalidOperation:
        return None
    if not serial.is_finite() or serial < 0:
        return None
    days = int(serial)
    fraction = serial - Decimal(days)
    seconds = int((fraction * Decimal(86400)).quantize(Decimal("1")))
    if seconds >= 86400:
        days += 1
        seconds -= 86400
    if date_1904:
        base = datetime(1904, 1, 1)
        value = base + timedelta(days=days, seconds=seconds)
    else:
        # Excel's serial 60 is its non-existent 1900-02-29; keep it uninterpreted.
        if days == 60:
            return None
        adjusted_days = days - 1 if days > 60 else days
        value = datetime(1899, 12, 31) + timedelta(days=adjusted_days, seconds=seconds)
    if fraction:
        return value.isoformat(timespec="seconds")
    return value.date().isoformat()


def _parse_xlsx(content: bytes) -> dict:
    archive, by_name = _open_office(content, ("xl/workbook.xml", "xl/_rels/workbook.xml.rels"))
    builder = _SegmentBuilder()
    reasons: list[str] = []
    try:
        workbook = _xml_part(archive, by_name, "xl/workbook.xml")
        relationships = _xml_part(archive, by_name, "xl/_rels/workbook.xml.rels")
        if _has_external_relationships(archive, by_name):
            _append_reason(reasons, "工作簿含有外部关系；解析器未访问外部目标。")
        rel_targets: dict[str, str] = {}
        for relationship in relationships.findall(f"{{{_NS_PACKAGE_REL}}}Relationship"):
            rel_id = relationship.attrib.get("Id")
            target = relationship.attrib.get("Target", "")
            if relationship.attrib.get("TargetMode", "").lower() == "external":
                _append_reason(reasons, "工作簿存在外部链接；解析器未访问外部目标。")
                continue
            resolved = _internal_target("xl/workbook.xml", target)
            if rel_id and resolved:
                rel_targets[rel_id] = resolved

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in by_name:
            shared_root = _xml_part(archive, by_name, "xl/sharedStrings.xml")
            for item in shared_root.findall(f"{{{_NS_MAIN}}}si"):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{_NS_MAIN}}}t")))

        date_styles = _xlsx_date_style_ids(archive, by_name)
        workbook_properties = workbook.find(f"{{{_NS_MAIN}}}workbookPr")
        date_1904 = (workbook_properties is not None
                     and workbook_properties.attrib.get("date1904", "0").lower() in {"1", "true"})
        sheets = workbook.find(f"{{{_NS_MAIN}}}sheets")
        if sheets is None:
            raise _Malformed("XLSX 工作簿没有工作表目录。")
        sheet_nodes = sheets.findall(f"{{{_NS_MAIN}}}sheet")
        if len(sheet_nodes) > MAX_WORKBOOK_SHEETS:
            raise _Unavailable("XLSX 工作表数量超过安全上限。")

        cells_read = 0
        for sheet_index, sheet in enumerate(sheet_nodes, 1):
            sheet_name = sheet.attrib.get("name", f"工作表 {sheet_index}")
            rel_id = sheet.attrib.get(f"{{{_NS_REL}}}id", "")
            part = rel_targets.get(rel_id)
            if not part or part not in by_name:
                _append_reason(reasons, f"工作表“{sheet_name}”的内部数据部件不可用。")
                continue
            root = _xml_part(archive, by_name, part)
            records: list[dict] = []
            for cell in root.iter(f"{{{_NS_MAIN}}}c"):
                cells_read += 1
                if cells_read > MAX_WORKBOOK_CELLS:
                    builder.truncated = True
                    break
                address = cell.attrib.get("r", f"第 {cells_read} 格")
                address_match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
                column = address_match.group(1) if address_match else ""
                row_number = int(address_match.group(2)) if address_match else cells_read
                value_node = cell.find(f"{{{_NS_MAIN}}}v")
                formula_node = cell.find(f"{{{_NS_MAIN}}}f")
                cell_type = cell.attrib.get("t", "")
                raw_value = value_node.text if value_node is not None else None
                if formula_node is not None:
                    formula = formula_node.text or ""
                    display = f"公式 ={formula}"
                    if raw_value not in (None, ""):
                        display += f"；缓存值（未重算）={raw_value}"
                    else:
                        display += "；无缓存计算值"
                    kind = "xlsx_formula"
                    _append_reason(reasons, "电子表格公式只保留原式，不执行或重算；缓存值可能过期。")
                    if re.search(r"\[[^\]]+\][^!]*!", formula):
                        _append_reason(reasons, "公式含有外部工作簿引用；没有访问或计算外部内容。")
                elif cell_type == "s" and raw_value is not None:
                    try:
                        display = shared_strings[int(raw_value)]
                    except (ValueError, IndexError):
                        _append_reason(reasons, f"单元格 {sheet_name}!{address} 的共享字符串索引无效。")
                        continue
                    kind = "xlsx_cell"
                elif cell_type == "inlineStr":
                    inline = cell.find(f"{{{_NS_MAIN}}}is")
                    display = "".join(node.text or "" for node in inline.iter(f"{{{_NS_MAIN}}}t")) if inline is not None else ""
                    kind = "xlsx_cell"
                elif cell_type == "b" and raw_value is not None:
                    display = "TRUE" if raw_value == "1" else "FALSE" if raw_value == "0" else raw_value
                    kind = "xlsx_boolean"
                elif cell_type == "e" and raw_value is not None:
                    display = raw_value
                    kind = "xlsx_error"
                elif raw_value is not None:
                    display = raw_value
                    kind = "xlsx_number" if cell_type in {"", "n"} else "xlsx_cell"
                    if kind == "xlsx_number":
                        try:
                            style_index = int(cell.attrib.get("s", "0"))
                        except ValueError:
                            style_index = -1
                        if style_index in date_styles:
                            interpreted = _excel_date(raw_value, date_1904)
                            if interpreted is not None:
                                display = f"{interpreted}（Excel 序列值 {raw_value}）"
                                kind = "xlsx_date"
                            else:
                                _append_reason(reasons, f"单元格 {sheet_name}!{address} 的日期序列无法无歧义解释。")
                else:
                    continue
                if display.strip():
                    records.append({"address": address, "column": column, "row": row_number,
                                    "text": display, "kind": kind})
            text_cells_by_row: dict[int, dict[str, str]] = {}
            for record in records:
                if record["kind"] == "xlsx_cell" and record["column"]:
                    text_cells_by_row.setdefault(record["row"], {})[record["column"]] = record["text"]
            header_row = next((row for row in sorted(text_cells_by_row)
                               if sum(bool(value.strip()) for value in text_cells_by_row[row].values()) >= 2), None)
            header = text_cells_by_row.get(header_row, {}) if header_row is not None else {}
            for record in records:
                label = f"{sheet_name}!{record['address']}"
                if (header_row is not None and record["row"] > header_row
                        and record["column"] in header and header[record["column"]].strip()):
                    label += f"；首个文本行的候选列名“{header[record['column']]}”"
                if not builder.add(label, record["text"], record["kind"]):
                    break
            if builder.truncated:
                break

        if any(name.startswith(("xl/charts/", "xl/drawings/", "xl/externalLinks/", "xl/embeddings/"))
               for name in by_name):
            _append_reason(reasons, "工作簿还含有未展开的图表、绘图、外部链接或嵌入对象。")
        return _finish(builder, reasons, "xlsx")
    finally:
        archive.close()


def _pptx_shape_text(shape: ET.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in shape.iter(f"{{{_NS_A}}}p"):
        value = "".join(node.text or "" for node in paragraph.iter(f"{{{_NS_A}}}t"))
        if value:
            paragraphs.append(value)
    return "\n".join(paragraphs)


def _parse_pptx(content: bytes) -> dict:
    archive, by_name = _open_office(content, ("ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"))
    builder = _SegmentBuilder()
    reasons: list[str] = []
    try:
        presentation = _xml_part(archive, by_name, "ppt/presentation.xml")
        relationships = _xml_part(archive, by_name, "ppt/_rels/presentation.xml.rels")
        if _has_external_relationships(archive, by_name):
            _append_reason(reasons, "演示文稿含有外部关系；解析器未访问外部目标。")
        rel_targets: dict[str, str] = {}
        for relationship in relationships.findall(f"{{{_NS_PACKAGE_REL}}}Relationship"):
            rel_id = relationship.attrib.get("Id")
            target = relationship.attrib.get("Target", "")
            if relationship.attrib.get("TargetMode", "").lower() == "external":
                _append_reason(reasons, "演示文稿存在外部关系；解析器未访问外部目标。")
                continue
            resolved = _internal_target("ppt/presentation.xml", target)
            if rel_id and resolved:
                rel_targets[rel_id] = resolved

        slide_ids = presentation.find(f"{{{_NS_P}}}sldIdLst")
        if slide_ids is None:
            raise _Malformed("PPTX 演示文稿没有幻灯片目录。")
        slides: list[tuple[int, str]] = []
        for index, slide_id in enumerate(slide_ids.findall(f"{{{_NS_P}}}sldId"), 1):
            target = rel_targets.get(slide_id.attrib.get(f"{{{_NS_REL}}}id", ""))
            if target and target in by_name:
                slides.append((index, target))
            else:
                _append_reason(reasons, f"第 {index} 页的幻灯片部件不可用。")
        if len(slides) > MAX_WORKBOOK_SHEETS:
            raise _Unavailable("PPTX 幻灯片数量超过安全上限。")

        for slide_number, part in slides:
            root = _xml_part(archive, by_name, part)
            shape_tree = root.find(f".//{{{_NS_P}}}spTree")
            if shape_tree is None:
                _append_reason(reasons, f"第 {slide_number} 页没有可读取的对象树。")
                continue
            shape_index = 0
            for child in shape_tree.iter():
                if child.tag == f"{{{_NS_P}}}sp":
                    shape_index += 1
                    properties = child.find(f"{{{_NS_P}}}nvSpPr/{{{_NS_P}}}cNvPr")
                    shape_name = properties.attrib.get("name", f"对象 {shape_index}") if properties is not None else f"对象 {shape_index}"
                    value = _pptx_shape_text(child)
                    if value.strip():
                        builder.add(f"第 {slide_number} 页，{shape_name}", value, "pptx_text")
                elif child.tag == f"{{{_NS_P}}}graphicFrame":
                    table = child.find(f".//{{{_NS_A}}}tbl")
                    if table is not None:
                        table_rows = [
                            [_pptx_shape_text(cell)
                             for cell in row.findall(f"{{{_NS_A}}}tc")]
                            for row in table.findall(f"{{{_NS_A}}}tr")
                        ]
                        header = next((row for row in table_rows
                                       if sum(bool(value.strip()) for value in row) >= 2), [])
                        for row_index, row in enumerate(table_rows, 1):
                            for column_index, value in enumerate(row, 1):
                                if value.strip():
                                    label = f"第 {slide_number} 页，表格第 {row_index} 行第 {column_index} 列"
                                    if row_index > 1 and column_index <= len(header) and header[column_index - 1].strip():
                                        label += f"；候选列名“{header[column_index - 1]}”"
                                    builder.add(label, value, "pptx_table_cell")
                    else:
                        _append_reason(reasons, f"第 {slide_number} 页含未解析的嵌入对象。")
                if builder.truncated:
                    break
            if builder.truncated:
                break

        if any(name.startswith(("ppt/charts/", "ppt/media/", "ppt/embeddings/", "ppt/notesSlides/"))
               for name in by_name):
            _append_reason(reasons, "演示文稿含有未展开的图表、媒体、备注或嵌入对象。")
        return _finish(builder, reasons, "pptx")
    finally:
        archive.close()


def _parse_pdf(content: bytes) -> dict:
    if not content.startswith(b"%PDF-"):
        raise _Malformed("PDF 文件头无效或扩展名与内容不匹配。")
    try:
        import pdfplumber
    except ImportError as exc:
        raise _Unavailable("当前运行环境没有已规划的 PDF 解析组件。") from exc

    builder = _SegmentBuilder()
    reasons: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise _Unavailable("PDF 页数超过当前安全解析上限。")
            empty_pages: list[int] = []
            for page_number, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    if not builder.add(f"第 {page_number} 页", text, "pdf_page"):
                        break
                else:
                    empty_pages.append(page_number)
            if empty_pages:
                if builder.text.strip():
                    _append_reason(reasons, "部分页面没有可提取文字；未对其内容作判断。")
                else:
                    raise _Unavailable("PDF 没有可提取文字；当前解析路径不执行 OCR。")
    except _Unavailable:
        raise
    except Exception as exc:
        name = type(exc).__name__.lower()
        if "password" in name or "encrypt" in name:
            raise _Unavailable("PDF 已加密或需要密码，当前路径无法读取。") from exc
        raise _Malformed(f"PDF 页面读取失败（{type(exc).__name__}）。") from exc
    return _finish(builder, reasons, "pdf")

