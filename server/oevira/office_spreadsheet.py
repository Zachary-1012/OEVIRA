"""Bounded, original-backed XLSX inspection and exact cell revision.

This module edits OOXML package members directly so unsupported workbook
objects remain byte-for-byte intact. It never launches an Office host,
executes macros, follows external links, or treats a formula cache as a fresh
calculation result.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import math
import posixpath
import re
import stat
import zipfile
from collections import defaultdict
from lxml import etree


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 24 * 1024 * 1024
MAX_ZIP_ENTRIES = 5_000
MAX_WORKBOOK_SHEETS = 1_000
MAX_WORKBOOK_CELLS = 250_000
MAX_CHANGES = 500
MAX_CHANGE_TEXT_CHARS = 32_767
MAX_FORMULA_RANGE_CELLS = 100_000
CALCULATION_ENGINE = "oevira-xlsx-formula"
CALCULATION_REVISION = "2026-11-10.13"

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_XML = "http://www.w3.org/XML/1998/namespace"
NS_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS = {"x": NS_MAIN, "r": NS_REL, "rel": NS_PACKAGE_REL}
XML_FORBIDDEN = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
CELL_ADDRESS = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")
DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


class SpreadsheetError(ValueError):
    """A safe, user-actionable XLSX processing failure."""

    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _fail(code: str, **details) -> None:
    raise SpreadsheetError(code, details)


def _q(name: str) -> str:
    return f"{{{NS_MAIN}}}{name}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_xml(raw: bytes, part: str):
    if len(raw) > MAX_XML_BYTES or b"\x00" in raw or XML_FORBIDDEN.search(raw):
        _fail("XLSX_XML_UNSAFE_OR_TOO_LARGE", part=part)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, remove_blank_text=False)
    try:
        root = etree.fromstring(raw, parser)
    except (etree.XMLSyntaxError, ValueError, RecursionError, UnicodeError) as exc:
        raise SpreadsheetError("XLSX_XML_INVALID", {"part": part}) from exc
    if root.getroottree().docinfo.doctype:
        _fail("XLSX_XML_UNSAFE_OR_TOO_LARGE", part=part)
    return root


def _read_package(content: bytes) -> tuple[list[zipfile.ZipInfo], dict[str, bytes], bytes]:
    if not isinstance(content, bytes):
        _fail("XLSX_CONTENT_MUST_BE_BYTES")
    if len(content) > MAX_INPUT_BYTES:
        _fail("XLSX_INPUT_TOO_LARGE")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise SpreadsheetError("XLSX_PACKAGE_INVALID") from exc

    infos: list[zipfile.ZipInfo] = []
    parts: dict[str, bytes] = {}
    names: set[str] = set()
    folded_names: set[str] = set()
    declared_total = 0
    try:
        listed = archive.infolist()
        if len(listed) > MAX_ZIP_ENTRIES:
            _fail("XLSX_ZIP_ENTRY_LIMIT")
        for info in listed:
            name = info.filename
            components = name.split("/")
            if (not name or name.startswith("/") or "\\" in name
                    or re.match(r"^[A-Za-z]:", name)
                    or any(component == ".." for component in components)):
                _fail("XLSX_MEMBER_PATH_UNSAFE")
            folded = name.casefold()
            if name in names or folded in folded_names:
                _fail("XLSX_MEMBER_NAME_AMBIGUOUS")
            names.add(name)
            folded_names.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                _fail("XLSX_MEMBER_SYMLINK_FORBIDDEN")
            if info.flag_bits & 0x1:
                _fail("XLSX_ENCRYPTED_MEMBER_UNSUPPORTED")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _fail("XLSX_COMPRESSION_METHOD_UNSUPPORTED")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                _fail("XLSX_MEMBER_SIZE_LIMIT", part=name)
            declared_total += info.file_size
            if declared_total > MAX_PACKAGE_BYTES:
                _fail("XLSX_EXPANDED_SIZE_LIMIT")

        actual_total = 0
        for info in listed:
            try:
                with archive.open(info, "r") as stream:
                    data = bytearray()
                    while True:
                        chunk = stream.read(min(1024 * 1024, info.file_size + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_MEMBER_BYTES:
                            _fail("XLSX_MEMBER_SIZE_LIMIT", part=info.filename)
            except SpreadsheetError:
                raise
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError) as exc:
                raise SpreadsheetError("XLSX_MEMBER_CORRUPT", {"part": info.filename}) from exc
            if len(data) != info.file_size:
                _fail("XLSX_MEMBER_SIZE_MISMATCH", part=info.filename)
            actual_total += len(data)
            if actual_total > MAX_PACKAGE_BYTES:
                _fail("XLSX_EXPANDED_SIZE_LIMIT")
            infos.append(info)
            parts[info.filename] = bytes(data)
        if "xl/workbook.xml" not in parts or "xl/_rels/workbook.xml.rels" not in parts:
            _fail("XLSX_REQUIRED_PART_MISSING")
        return infos, parts, archive.comment
    except Exception:
        archive.close()
        raise
    finally:
        if archive.fp is not None:
            archive.close()


def _internal_target(source_part: str, target: str) -> str | None:
    if not target or "\\" in target or "\x00" in target:
        return None
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if resolved in {".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        return None
    return resolved


def _worksheet_map(parts: dict[str, bytes]) -> tuple[dict[int, dict], dict[str, dict], list[str]]:
    workbook = _parse_xml(parts["xl/workbook.xml"], "xl/workbook.xml")
    rels = _parse_xml(parts["xl/_rels/workbook.xml.rels"], "xl/_rels/workbook.xml.rels")
    targets: dict[str, tuple[str, str]] = {}
    for relation in rels.findall(f"{{{NS_PACKAGE_REL}}}Relationship"):
        rel_id = relation.get("Id")
        target = relation.get("Target", "")
        kind = relation.get("Type", "").rsplit("/", 1)[-1]
        if not rel_id:
            continue
        if relation.get("TargetMode", "").lower() == "external":
            targets[rel_id] = (kind, "")
        else:
            resolved = _internal_target("xl/workbook.xml", target)
            targets[rel_id] = (kind, resolved or "")

    sheets_node = workbook.find(_q("sheets"))
    if sheets_node is None:
        _fail("XLSX_WORKSHEET_DIRECTORY_MISSING")
    sheet_nodes = sheets_node.findall(_q("sheet"))
    if not sheet_nodes or len(sheet_nodes) > MAX_WORKBOOK_SHEETS:
        _fail("XLSX_WORKSHEET_COUNT_UNSUPPORTED")

    sheets: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    warnings: list[str] = []
    for node in sheet_nodes:
        try:
            sheet_id = int(node.get("sheetId", "0"))
        except ValueError:
            _fail("XLSX_WORKSHEET_ID_INVALID")
        name = node.get("name", "")
        relation_id = node.get(f"{{{NS_REL}}}id")
        relation = targets.get(relation_id or "")
        if not name or sheet_id <= 0 or sheet_id in sheets or name.casefold() in by_name:
            _fail("XLSX_WORKSHEET_ID_AMBIGUOUS")
        if relation is None or relation[0] != "worksheet" or not relation[1] or relation[1] not in parts:
            _fail("XLSX_WORKSHEET_PART_UNAVAILABLE", sheet=name)
        sheet = {"sheet_id": sheet_id, "name": name, "part": relation[1], "state": node.get("state", "visible")}
        sheets[sheet_id] = sheet
        by_name[name.casefold()] = sheet
    if any(name.startswith("xl/externalLinks/") for name in parts):
        warnings.append("工作簿含有外部链接；未访问外部目标，外链内容与缓存状态未验证。")
    if any("vbaproject" in name.casefold() or "/activex/" in name.casefold() for name in parts):
        warnings.append("工作簿含有宏或 ActiveX 内容；未执行，原成员会保留。")
    if any(name.startswith("xl/pivotCache/") or name.startswith("xl/pivotTables/") for name in parts):
        warnings.append("工作簿含有数据透视对象；透视缓存和刷新结果尚未核验。")
    return sheets, by_name, warnings


def _shared_strings(parts: dict[str, bytes]):
    raw = parts.get("xl/sharedStrings.xml")
    if raw is None:
        return None, []
    root = _parse_xml(raw, "xl/sharedStrings.xml")
    return root, root.findall(_q("si"))


def _cell_text(cell, shared_strings: list, *, allow_formula: bool = False) -> dict:
    formula = cell.find(_q("f"))
    value = cell.find(_q("v"))
    kind = cell.get("t", "n")
    if formula is not None:
        formula_text = formula.text or ""
        if formula_text.startswith("="):
            formula_text = formula_text[1:]
        cached = None
        if value is not None and value.text is not None:
            cached_kind = {"str": "text", "b": "boolean", "e": "error"}.get(kind, "number")
            cached_value = value.text
            if cached_kind == "boolean":
                cached_value = cached_value == "1"
            cached = {"kind": cached_kind, "value": cached_value, "verified": False}
        return {"kind": "formula", "value": "=" + formula_text, "cached": cached}
    if kind == "s":
        if value is None or value.text is None:
            _fail("XLSX_SHARED_STRING_REFERENCE_INVALID")
        try:
            index = int(value.text)
            item = shared_strings[index]
        except (ValueError, IndexError) as exc:
            raise SpreadsheetError("XLSX_SHARED_STRING_REFERENCE_INVALID") from exc
        return {"kind": "text", "value": _rich_text(item)}
    if kind == "inlineStr":
        inline = cell.find(_q("is"))
        return {"kind": "text", "value": _rich_text(inline) if inline is not None else ""}
    if kind in {"str", "inlineStr"}:
        return {"kind": "text", "value": value.text if value is not None and value.text is not None else ""}
    if kind == "b":
        if value is None or value.text not in {"0", "1"}:
            _fail("XLSX_BOOLEAN_VALUE_INVALID")
        return {"kind": "boolean", "value": value.text == "1"}
    if kind == "e":
        return {"kind": "error", "value": value.text if value is not None else "#VALUE!"}
    if value is None or value.text is None:
        return {"kind": "blank"}
    if not NUMBER.fullmatch(value.text):
        _fail("XLSX_NUMERIC_VALUE_INVALID")
    return {"kind": "number", "value": value.text}


def _rich_text(node) -> str:
    return "".join(item.text or "" for item in node.xpath(".//x:t[not(ancestor::x:rPh)]", namespaces={"x": NS_MAIN}))


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _cell_position(address: str) -> tuple[int, int]:
    match = CELL_ADDRESS.fullmatch(address or "")
    if match is None:
        _fail("XLSX_CELL_ADDRESS_INVALID", address=address)
    row = int(match.group(2))
    column = _column_number(match.group(1))
    if row > 1_048_576 or column > 16_384:
        _fail("XLSX_CELL_ADDRESS_INVALID", address=address)
    return row, column


def _canonical(node) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=True, with_comments=True)


@dataclass
class _Workbook:
    infos: list[zipfile.ZipInfo]
    parts: dict[str, bytes]
    comment: bytes
    sheets: dict[int, dict]
    sheets_by_name: dict[str, dict]
    warnings: list[str]
    shared_root: object | None
    shared_items: list
    sheet_roots: dict[int, object]
    cells: dict[tuple[int, str], object]
    formulas: dict[tuple[int, str], str]
    formula_modes: dict[tuple[int, str], str | None]
    names: dict
    date_1904: bool


def _defined_names(parts: dict[str, bytes], sheets: dict[int, dict]) -> dict:
    workbook = _parse_xml(parts["xl/workbook.xml"], "xl/workbook.xml")
    result = {}
    name_list = workbook.find(_q("definedNames"))
    if name_list is None:
        return result
    for item in name_list.findall(_q("definedName")):
        name = item.get("name")
        if not name or item.get("hidden") == "1":
            continue
        local_id = item.get("localSheetId")
        scope = None
        if local_id is not None:
            try:
                order = int(local_id)
                scope = list(sheets.values())[order]["sheet_id"]
            except (ValueError, IndexError):
                continue
        result[(scope, name.casefold())] = (item.text or "").removeprefix("=")
    return result


def _load_workbook(content: bytes) -> _Workbook:
    infos, parts, comment = _read_package(content)
    sheets, by_name, warnings = _worksheet_map(parts)
    shared_root, shared_items = _shared_strings(parts)
    workbook_root = _parse_xml(parts["xl/workbook.xml"], "xl/workbook.xml")
    properties = workbook_root.find(_q("workbookPr"))
    date_1904 = properties is not None and properties.get("date1904", "0").lower() in {"1", "true"}
    sheet_roots = {}
    cells = {}
    formulas = {}
    formula_modes = {}
    cell_count = 0
    for sheet_id, sheet in sheets.items():
        root = _parse_xml(parts[sheet["part"]], sheet["part"])
        sheet_roots[sheet_id] = root
        sheet_data = root.find(_q("sheetData"))
        if sheet_data is None:
            _fail("XLSX_SHEET_DATA_MISSING", sheet=sheet["name"])
        for cell in sheet_data.iter(_q("c")):
            cell_count += 1
            if cell_count > MAX_WORKBOOK_CELLS:
                _fail("XLSX_CELL_COUNT_UNSUPPORTED", limit=MAX_WORKBOOK_CELLS)
            address = cell.get("r")
            if not address:
                _fail("XLSX_CELL_ADDRESS_INVALID", sheet=sheet["name"])
            position = _cell_position(address)
            normalized_address = f"{_column_letters(position[1])}{position[0]}"
            key = (sheet_id, normalized_address)
            if key in cells:
                _fail("XLSX_CELL_ADDRESS_AMBIGUOUS", sheet=sheet["name"], address=normalized_address)
            cells[key] = cell
            formula = cell.find(_q("f"))
            if formula is not None:
                formula_modes[key] = formula.get("t")
                text = formula.text or ""
                if formula.get("t") == "shared" and not text:
                    continue
                formulas[key] = text.removeprefix("=")
    _expand_shared_formulas(sheet_roots, sheets, cells, formulas, formula_modes)
    return _Workbook(infos, parts, comment, sheets, by_name, warnings, shared_root,
                     shared_items, sheet_roots, cells, formulas, formula_modes,
                     _defined_names(parts, sheets), date_1904)


def _column_letters(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _expand_shared_formulas(sheet_roots, sheets, cells, formulas, formula_modes) -> None:
    try:
        from openpyxl.formula.translate import Translator
    except ImportError:
        Translator = None
    for sheet_id, sheet in sheets.items():
        groups = {}
        for key, cell in cells.items():
            if key[0] != sheet_id:
                continue
            formula = cell.find(_q("f"))
            if formula is not None and formula.get("t") == "shared" and formula.text:
                groups[formula.get("si", "")] = (key, formula.text, formula.get("ref"))
        for key, cell in cells.items():
            if key[0] != sheet_id:
                continue
            formula = cell.find(_q("f"))
            if formula is None or formula.get("t") != "shared" or formula.text:
                continue
            master = groups.get(formula.get("si", ""))
            if master is None:
                _fail("XLSX_SHARED_FORMULA_MASTER_MISSING", sheet=sheet["name"], address=key[1])
            if Translator is None:
                _fail("XLSX_SHARED_FORMULA_UNSUPPORTED", sheet=sheet["name"], address=key[1])
            master_key, master_formula, _ref = master
            try:
                translated = Translator("=" + master_formula, origin=master_key[1]).translate_formula(key[1])
            except Exception as exc:
                raise SpreadsheetError("XLSX_SHARED_FORMULA_UNSUPPORTED",
                                       {"sheet": sheet["name"], "address": key[1]}) from exc
            formulas[key] = translated.removeprefix("=")


def _style_index(cell) -> int:
    try:
        return int(cell.get("s", "0"))
    except ValueError:
        _fail("XLSX_STYLE_REFERENCE_INVALID")


def _date_style_ids(workbook: _Workbook) -> set[int]:
    raw = workbook.parts.get("xl/styles.xml")
    if raw is None:
        return set()
    root = _parse_xml(raw, "xl/styles.xml")
    custom = {}
    formats = root.find(_q("numFmts"))
    if formats is not None:
        for item in formats.findall(_q("numFmt")):
            try:
                num_fmt = int(item.get("numFmtId", "-1"))
            except ValueError:
                continue
            code = item.get("formatCode", "")
            stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"|\\.|\[[^]]*\]', "", code)
            custom[num_fmt] = bool(re.search(r"[ydhms]", stripped, re.IGNORECASE))
    styles = root.find(_q("cellXfs"))
    found = set()
    if styles is not None:
        for index, item in enumerate(styles.findall(_q("xf"))):
            try:
                num_fmt = int(item.get("numFmtId", "0"))
            except ValueError:
                continue
            if num_fmt in DATE_FORMATS or custom.get(num_fmt, False):
                found.add(index)
    return found


def _object_id(workbook_hash: str, sheet_id: int, address: str) -> str:
    return f"xlsx:{workbook_hash}:{sheet_id}:{address}"


def inspect_spreadsheet(content: bytes) -> dict:
    workbook = _load_workbook(content)
    digest = _sha256(content)
    shared_root = workbook.shared_root
    date_styles = _date_style_ids(workbook)
    sheet_results = []
    for sheet_id, sheet in workbook.sheets.items():
        cell_results = []
        for (cell_sheet_id, address), cell in workbook.cells.items():
            if cell_sheet_id != sheet_id:
                continue
            value = _cell_text(cell, workbook.shared_items)
            if value["kind"] == "blank" and cell.find(_q("f")) is None:
                continue
            formula = cell.find(_q("f"))
            item = {
                "object_id": _object_id(digest, sheet_id, address),
                "address": address,
                "value": value,
                "style_id": _style_index(cell),
                "kind": value["kind"],
            }
            if formula is not None:
                item["formula_type"] = formula.get("t", "normal")
                if formula.get("ref"):
                    item["formula_range"] = formula.get("ref")
            elif value["kind"] == "number" and _style_index(cell) in date_styles:
                item["kind"] = "date_serial"
                item["date_system"] = "1904" if workbook.date_1904 else "1900"
            cell_results.append(item)
        cell_results.sort(key=lambda item: _cell_position(item["address"]))
        root = workbook.sheet_roots[sheet_id]
        merges = root.find(_q("mergeCells"))
        merged_ranges = [node.get("ref") for node in merges.findall(_q("mergeCell"))] if merges is not None else []
        sheet_results.append({
            "sheet_id": sheet_id,
            "name": sheet["name"],
            "state": sheet["state"],
            "cells": cell_results,
            "merged_ranges": merged_ranges,
        })
    parts = set(workbook.parts)
    if any(name.startswith("xl/charts/") for name in parts):
        workbook.warnings.append("工作簿含有图表；目标工作簿宿主中的视觉呈现尚未验证。")
    return {
        "sha256": digest,
        "size_bytes": len(content),
        "sheets": sheet_results,
        "warnings": list(dict.fromkeys(workbook.warnings)),
    }


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class _CellRef:
    sheet_id: int
    address: str


@dataclass(frozen=True)
class _CellRange:
    sheet_id: int
    first_row: int
    first_column: int
    last_row: int
    last_column: int


@dataclass(frozen=True)
class _Value:
    kind: str
    value: object = None


class _FormulaSyntax(ValueError):
    pass


class _FormulaFailure(ValueError):
    def __init__(self, code: str, address: str | None = None):
        self.code = code
        self.address = address
        super().__init__(code)


_TOKEN_PATTERN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<string>\"(?:[^\"]|\"\")*\")"
    r"|(?P<quoted_sheet>'(?:[^']|'')*'!)"
    r"|(?P<sheet>[A-Za-z_][A-Za-z0-9_.]*!)"
    r"|(?P<error>\#[A-Za-z0-9/?!]+)"
    r"|(?P<cell>\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)"
    r"|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)"
    r"|(?P<identifier>[\w.\\]+)"
    r"|(?P<operator><=|>=|<>|[+\-*/^&%=(),:<>!])",
    re.UNICODE,
)


class _FormulaParser:
    def __init__(self, formula: str, workbook: _Workbook, current_sheet: int,
                 active_names: frozenset[tuple[int | None, str]] = frozenset()):
        self.workbook = workbook
        self.current_sheet = current_sheet
        self.active_names = active_names
        self.tokens = self._tokenize(formula.removeprefix("="))
        self.index = 0

    @staticmethod
    def _tokenize(formula: str) -> list[_Token]:
        tokens = []
        cursor = 0
        while cursor < len(formula):
            match = _TOKEN_PATTERN.match(formula, cursor)
            if match is None:
                raise _FormulaSyntax("token")
            cursor = match.end()
            kind = match.lastgroup
            if kind != "space":
                tokens.append(_Token(kind or "", match.group(kind or "")))
        tokens.append(_Token("end", ""))
        return tokens

    def parse(self):
        value = self._comparison()
        if self._peek().kind != "end":
            raise _FormulaSyntax("trailing")
        return value

    def _peek(self) -> _Token:
        return self.tokens[self.index]

    def _take(self, kind: str | None = None, value: str | None = None) -> _Token:
        token = self._peek()
        if kind is not None and token.kind != kind:
            raise _FormulaSyntax("expected " + kind)
        if value is not None and token.value != value:
            raise _FormulaSyntax("expected " + value)
        self.index += 1
        return token

    def _comparison(self):
        left = self._concat()
        while self._peek().kind == "operator" and self._peek().value in {"=", "<>", "<", "<=", ">", ">="}:
            operator = self._take().value
            left = ("binary", operator, left, self._concat())
        return left

    def _concat(self):
        left = self._addition()
        while self._peek().kind == "operator" and self._peek().value == "&":
            self._take()
            left = ("binary", "&", left, self._addition())
        return left

    def _addition(self):
        left = self._multiplication()
        while self._peek().kind == "operator" and self._peek().value in {"+", "-"}:
            operator = self._take().value
            left = ("binary", operator, left, self._multiplication())
        return left

    def _multiplication(self):
        left = self._power()
        while self._peek().kind == "operator" and self._peek().value in {"*", "/"}:
            operator = self._take().value
            left = ("binary", operator, left, self._power())
        return left

    def _power(self):
        left = self._unary()
        while self._peek().kind == "operator" and self._peek().value == "^":
            self._take()
            left = ("binary", "^", left, self._unary())
        return left

    def _unary(self):
        if self._peek().kind == "operator" and self._peek().value in {"+", "-"}:
            operator = self._take().value
            return ("unary", operator, self._unary())
        value = self._primary()
        while self._peek().kind == "operator" and self._peek().value == "%":
            self._take()
            value = ("unary", "%", value)
        return value

    def _primary(self):
        token = self._peek()
        if token.kind == "operator" and token.value == "(":
            self._take()
            value = self._comparison()
            self._take("operator", ")")
            return value
        if token.kind == "string":
            self._take()
            return ("literal", _Value("text", token.value[1:-1].replace('""', '"')))
        if token.kind == "number":
            self._take()
            try:
                return ("literal", _Value("number", Decimal(token.value)))
            except InvalidOperation as exc:
                raise _FormulaSyntax("number") from exc
        if token.kind == "error":
            self._take()
            return ("literal", _Value("error", token.value.upper()))
        if token.kind == "sheet" or token.kind == "quoted_sheet":
            sheet_name = token.value[:-1]
            if token.kind == "quoted_sheet":
                sheet_name = sheet_name[1:-1].replace("''", "'")
            self._take()
            if self._peek().kind != "cell":
                raise _FormulaSyntax("sheet reference")
            first_address = self._take().value
            return self._reference(sheet_name, first_address)
        if token.kind == "cell":
            self._take()
            return self._reference(None, token.value)
        if token.kind == "identifier":
            name = self._take().value
            if name.casefold() in {"true", "false"} and not self._is("operator", "("):
                return ("literal", _Value("boolean", name.casefold() == "true"))
            if self._is("operator", "("):
                self._take()
                arguments = []
                if not self._is("operator", ")"):
                    while True:
                        arguments.append(self._comparison())
                        if self._is("operator", ","):
                            self._take()
                            continue
                        break
                self._take("operator", ")")
                normalized_name = name.upper()
                if normalized_name.startswith("_XLFN."):
                    normalized_name = normalized_name.removeprefix("_XLFN.")
                if normalized_name.startswith("_XLWS."):
                    normalized_name = normalized_name.removeprefix("_XLWS.")
                return ("function", normalized_name, tuple(arguments))
            return self._named_reference(name)
        raise _FormulaSyntax("expression")

    def _is(self, kind: str, value: str) -> bool:
        return self._peek().kind == kind and self._peek().value == value

    def _reference(self, sheet_name: str | None, first_address: str):
        sheet_id = self.current_sheet
        if sheet_name is not None:
            found = self.workbook.sheets_by_name.get(sheet_name.casefold())
            if found is None:
                raise _FormulaSyntax("unknown sheet")
            sheet_id = found["sheet_id"]
        first = _cell_position(first_address)
        if not self._is("operator", ":"):
            return ("reference", _CellRef(sheet_id, f"{_column_letters(first[1])}{first[0]}"))
        self._take()
        if self._peek().kind in {"sheet", "quoted_sheet"}:
            next_token = self._take()
            next_sheet = next_token.value[:-1]
            if next_token.kind == "quoted_sheet":
                next_sheet = next_sheet[1:-1].replace("''", "'")
            if self._peek().kind == "cell":
                second_address = self._take().value
            else:
                raise _FormulaSyntax("range endpoint")
            resolved = self.workbook.sheets_by_name.get(next_sheet.casefold())
            if resolved is None or resolved["sheet_id"] != sheet_id:
                raise _FormulaSyntax("cross-sheet range")
        else:
            second_address = self._take("cell").value
        last = _cell_position(second_address)
        if last[0] < first[0] or last[1] < first[1]:
            raise _FormulaSyntax("reversed range")
        return ("range", _CellRange(sheet_id, first[0], first[1], last[0], last[1]))

    def _named_reference(self, name: str):
        for key in ((self.current_sheet, name.casefold()), (None, name.casefold())):
            formula = self.workbook.names.get(key)
            if formula is None:
                continue
            if key in self.active_names:
                raise _FormulaSyntax("recursive name")
            parser = _FormulaParser(formula, self.workbook, key[0] if key[0] is not None else self.current_sheet,
                                    self.active_names | {key})
            return parser.parse()
        return ("unresolved_name", name)


def _ast_references(node) -> list[_CellRef | _CellRange] | None:
    kind = node[0]
    if kind == "reference":
        return [node[1]]
    if kind == "range":
        return [node[1]]
    if kind in {"unresolved_name"}:
        return None
    if kind in {"literal"}:
        return []
    if kind == "unary":
        return _ast_references(node[2])
    if kind == "binary":
        left = _ast_references(node[2])
        right = _ast_references(node[3])
        if left is None or right is None:
            return None
        return left + right
    if kind == "function":
        values = []
        for argument in node[2]:
            refs = _ast_references(argument)
            if refs is None:
                return None
            values.extend(refs)
        return values
    return None


def _formula_models(workbook: _Workbook, overrides: dict[tuple[int, str], str | None]):
    formulas = dict(workbook.formulas)
    for key, value in overrides.items():
        if value is None:
            formulas.pop(key, None)
        else:
            formulas[key] = value
    models = {}
    for key, formula in formulas.items():
        try:
            tree = _FormulaParser(formula, workbook, key[0]).parse()
        except _FormulaSyntax:
            models[key] = (None, None)
        else:
            models[key] = (tree, _ast_references(tree))
    return formulas, models


def _range_contains(reference: _CellRef | _CellRange, key: tuple[int, str]) -> bool:
    if isinstance(reference, _CellRef):
        return reference.sheet_id == key[0] and reference.address == key[1]
    if reference.sheet_id != key[0]:
        return False
    row, column = _cell_position(key[1])
    return (reference.first_row <= row <= reference.last_row
            and reference.first_column <= column <= reference.last_column)


def _reference_cells(reference: _CellRef | _CellRange):
    if isinstance(reference, _CellRef):
        yield (reference.sheet_id, reference.address)
        return
    count = (reference.last_row - reference.first_row + 1) * (reference.last_column - reference.first_column + 1)
    if count > MAX_FORMULA_RANGE_CELLS:
        _fail("XLSX_FORMULA_RANGE_TOO_LARGE")
    for row in range(reference.first_row, reference.last_row + 1):
        for column in range(reference.first_column, reference.last_column + 1):
            yield (reference.sheet_id, f"{_column_letters(column)}{row}")


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        return Decimal(int(value))
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str) and value.strip() == "":
        return Decimal(0)
    elif isinstance(value, str) and NUMBER.fullmatch(value.strip()):
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise _FormulaFailure("#VALUE!") from exc
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        raise _FormulaFailure("#VALUE!")
    if not number.is_finite():
        raise _FormulaFailure("#NUM!")
    return number


def _excel_round(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal(0)
    if abs(value.adjusted()) > 300:
        raise _FormulaFailure("#NUM!")
    quantum = Decimal(1).scaleb(value.adjusted() - 14)
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise _FormulaFailure("#NUM!") from exc


def _read_value(workbook: _Workbook, key: tuple[int, str], formulas: dict, models: dict,
                value_overrides: dict, calculated: dict, active: set,
                evaluation_count: list) -> _Value:
    if key in value_overrides:
        return value_overrides[key]
    if key in calculated:
        return calculated[key]
    cell = workbook.cells.get(key)
    formula = formulas.get(key)
    if formula is None:
        if cell is None:
            return _Value("blank")
        raw = _cell_text(cell, workbook.shared_items)
        kind = raw["kind"]
        if kind == "number":
            return _Value(kind, Decimal(raw["value"]))
        return _Value(kind, raw.get("value"))
    if key in active:
        raise _FormulaFailure("#CIRCULAR!")
    evaluation_count[0] += 1
    if evaluation_count[0] > MAX_FORMULA_RANGE_CELLS:
        _fail("XLSX_CALCULATION_LIMIT")
    active.add(key)
    try:
        tree, _references = models.get(key, (None, None))
        if tree is None:
            _fail("XLSX_FORMULA_UNSUPPORTED", sheet_id=key[0], address=key[1])
        result = _evaluate(tree, workbook, formulas, models, value_overrides,
                           calculated, active, evaluation_count, key)
        if not isinstance(result, _Value):
            _fail("XLSX_FORMULA_RESULT_INVALID")
        calculated[key] = result
        return result
    except _FormulaSyntax as exc:
        raise SpreadsheetError("XLSX_FORMULA_UNSUPPORTED", {"sheet_id": key[0], "address": key[1]}) from exc
    except _FormulaFailure as exc:
        calculated[key] = _Value("error", str(exc))
        return calculated[key]
    finally:
        active.remove(key)


def _truth(value: _Value) -> bool:
    if value.kind == "error":
        raise _FormulaFailure(str(value.value))
    if value.kind == "blank":
        return False
    if value.kind == "boolean":
        return bool(value.value)
    if value.kind == "number":
        return value.value != 0
    if value.kind == "text":
        raise _FormulaFailure("#VALUE!")
    raise _FormulaFailure("#VALUE!")


def _text_value(value: _Value) -> str:
    if value.kind == "error":
        raise _FormulaFailure(str(value.value))
    if value.kind == "blank":
        return ""
    if value.kind == "boolean":
        return "TRUE" if value.value else "FALSE"
    if value.kind == "number":
        return format(value.value, "f")
    return str(value.value)


def _aggregate_formula(name: str, arguments: tuple, workbook: _Workbook, formulas: dict,
                       models: dict, value_overrides: dict, calculated: dict, active: set,
                       evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    if len(arguments) > 255:
        raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
    numeric: list[Decimal] = []
    count = 0
    nonblank = 0
    for argument in arguments:
        value = _evaluate(argument, workbook, formulas, models, value_overrides,
                          calculated, active, evaluation_count, formula_key)
        from_reference = argument[0] in {"reference", "range"}
        items = value if isinstance(value, list) else [value]
        if not from_reference and len(items) != 1:
            raise _FormulaFailure("#VALUE!")
        for item in items:
            current = _coerce_value(item)
            if current.kind != "blank":
                nonblank += 1
            if name == "COUNTA":
                continue
            if current.kind == "error":
                if name == "COUNT":
                    continue
                raise _FormulaFailure(str(current.value))
            if current.kind == "blank":
                continue
            if current.kind == "number":
                numeric.append(current.value)
                count += 1
                continue
            if from_reference:
                # COUNT excludes references with logical, text and error values;
                # SUM/AVERAGE/MIN/MAX ignore reference text and logical values.
                continue
            if current.kind == "boolean":
                if name in {"SUM", "MIN", "MAX", "COUNT"}:
                    numeric.append(Decimal(1 if current.value else 0))
                    count += 1
                # AVERAGE ignores directly supplied logical arguments.
                continue
            if current.kind == "text":
                text = str(current.value)
                if name == "AVERAGE" and NUMBER.fullmatch(text.strip()):
                    # Excel's AVERAGE ignores directly supplied numeric text.
                    continue
                if NUMBER.fullmatch(text.strip()) and name in {"SUM", "MIN", "MAX", "COUNT"}:
                    number = _decimal(text.strip())
                    numeric.append(number)
                    count += 1
                    continue
                if name == "COUNT":
                    continue
                raise _FormulaFailure("#VALUE!")

    if name == "COUNTA":
        return _Value("number", Decimal(nonblank))
    if name == "COUNT":
        return _Value("number", Decimal(count))
    if name == "SUM":
        return _Value("number", _excel_round(sum(numeric, Decimal(0))))
    if not numeric:
        if name in {"MIN", "MAX"}:
            return _Value("number", Decimal(0))
        raise _FormulaFailure("#DIV/0!")
    if name == "AVERAGE":
        return _Value("number", _excel_round(sum(numeric, Decimal(0)) / len(numeric)))
    return _Value("number", min(numeric) if name == "MIN" else max(numeric))


def _criteria_reference(argument, workbook: _Workbook, formulas: dict, models: dict,
                        value_overrides: dict, calculated: dict, active: set,
                        evaluation_count: list, formula_key: tuple[int, str]) -> list[_Value]:
    if argument[0] not in {"reference", "range"}:
        raise SpreadsheetError("XLSX_FORMULA_RANGE_ARGUMENT_UNSUPPORTED",
                               {"sheet_id": formula_key[0], "address": formula_key[1]})
    value = _evaluate(argument, workbook, formulas, models, value_overrides,
                      calculated, active, evaluation_count, formula_key)
    return value if isinstance(value, list) else [value]


def _criterion_parts(value: _Value) -> tuple[str, _Value]:
    current = _coerce_value(value)
    if current.kind != "text":
        return "=", current
    text = str(current.value)
    match = re.match(r"^(<=|>=|<>|=|<|>)(.*)$", text, re.DOTALL)
    if match:
        operator, operand = match.group(1), match.group(2)
    else:
        operator, operand = "=", text
    if operand == "":
        return operator, _Value("blank")
    if NUMBER.fullmatch(operand.strip()):
        return operator, _Value("number", _decimal(operand.strip()))
    if operand.casefold() in {"true", "false"}:
        return operator, _Value("boolean", operand.casefold() == "true")
    return operator, _Value("text", operand)


def _wildcard_pattern(value: str) -> str | None:
    if not any(character in value for character in "*?~"):
        return None
    pieces = ["^"]
    index = 0
    while index < len(value):
        character = value[index]
        if character == "~" and index + 1 < len(value):
            index += 1
            pieces.append(re.escape(value[index]))
        elif character == "*":
            pieces.append(".*")
        elif character == "?":
            pieces.append(".")
        else:
            pieces.append(re.escape(character))
        index += 1
    pieces.append("$")
    return "".join(pieces)


def _criterion_matches(candidate: _Value, criteria: _Value) -> bool:
    current = _coerce_value(candidate)
    operator, expected = _criterion_parts(criteria)
    if current.kind == "error":
        return False
    if expected.kind == "blank":
        equal = current.kind == "blank" or current.kind == "text" and current.value == ""
        return not equal if operator == "<>" else equal if operator == "=" else False
    if current.kind == expected.kind == "number":
        left, right = current.value, expected.value
    elif current.kind == expected.kind == "boolean":
        left, right = bool(current.value), bool(expected.value)
    elif current.kind == expected.kind == "text":
        pattern = _wildcard_pattern(str(expected.value)) if operator in {"=", "<>"} else None
        if pattern is not None:
            equal = re.fullmatch(pattern, str(current.value), re.IGNORECASE | re.DOTALL) is not None
            return not equal if operator == "<>" else equal
        left, right = str(current.value).casefold(), str(expected.value).casefold()
    elif current.kind == "text" and expected.kind == "number" and NUMBER.fullmatch(str(current.value).strip()):
        left, right = _decimal(str(current.value).strip()), expected.value
    else:
        left, right = current.kind, expected.kind
    return {"=": left == right, "<>": left != right, "<": left < right,
            "<=": left <= right, ">": left > right, ">=": left >= right}[operator]


def _criteria_result(name: str, criteria_ranges: list[list[_Value]],
                     criteria_values: list[_Value], sum_range: list[_Value] | None,
                     formula_key: tuple[int, str]) -> _Value:
    if not criteria_ranges or len(criteria_ranges) != len(criteria_values):
        raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
    cell_count = len(criteria_ranges[0])
    if not cell_count or any(len(values) != cell_count for values in criteria_ranges):
        raise SpreadsheetError("XLSX_FORMULA_RANGE_SHAPE_MISMATCH", {"function": name})
    if sum_range is not None and len(sum_range) != cell_count:
        raise SpreadsheetError("XLSX_FORMULA_RANGE_SHAPE_MISMATCH", {"function": name})
    numeric = []
    matched = 0
    for position in range(cell_count):
        if all(_criterion_matches(values[position], criteria_values[index])
               for index, values in enumerate(criteria_ranges)):
            matched += 1
            if name in {"COUNTIF", "COUNTIFS"}:
                continue
            value = _coerce_value(sum_range[position] if sum_range is not None else criteria_ranges[0][position])
            if value.kind == "error":
                raise _FormulaFailure(str(value.value))
            if value.kind == "number":
                numeric.append(value.value)
    if name in {"COUNTIF", "COUNTIFS"}:
        return _Value("number", Decimal(matched))
    if name == "SUMIF" or name == "SUMIFS":
        return _Value("number", _excel_round(sum(numeric, Decimal(0))))
    if not numeric:
        raise _FormulaFailure("#DIV/0!")
    return _Value("number", _excel_round(sum(numeric, Decimal(0)) / len(numeric)))


def _conditional_aggregate(name: str, arguments: tuple, workbook: _Workbook, formulas: dict,
                           models: dict, value_overrides: dict, calculated: dict, active: set,
                           evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    if name in {"SUMIF", "AVERAGEIF", "COUNTIF"}:
        if len(arguments) not in ({2, 3} if name != "COUNTIF" else {2}):
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        criteria_range = _criteria_reference(arguments[0], workbook, formulas, models,
                                             value_overrides, calculated, active,
                                             evaluation_count, formula_key)
        criteria = _scalar(_evaluate(arguments[1], workbook, formulas, models,
                                     value_overrides, calculated, active,
                                     evaluation_count, formula_key))
        if not isinstance(criteria, _Value):
            criteria = _coerce_value(criteria)
        sum_range = (_criteria_reference(arguments[2], workbook, formulas, models,
                                         value_overrides, calculated, active,
                                         evaluation_count, formula_key)
                     if len(arguments) == 3 else None)
        criteria_ranges = [criteria_range]
        values = [criteria]
        operation = {"SUMIF": "SUMIF", "AVERAGEIF": "AVERAGEIF", "COUNTIF": "COUNTIF"}[name]
        return _criteria_result(operation, criteria_ranges, values, sum_range, formula_key)

    if name == "SUMIFS":
        if len(arguments) < 3 or (len(arguments) - 1) % 2:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        sum_range = _criteria_reference(arguments[0], workbook, formulas, models,
                                        value_overrides, calculated, active,
                                        evaluation_count, formula_key)
        criteria_args = arguments[1:]
        range_args = criteria_args[::2]
        value_args = criteria_args[1::2]
    elif name == "AVERAGEIFS":
        if len(arguments) < 3 or (len(arguments) - 1) % 2:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        sum_range = _criteria_reference(arguments[0], workbook, formulas, models,
                                        value_overrides, calculated, active,
                                        evaluation_count, formula_key)
        criteria_args = arguments[1:]
        range_args = criteria_args[::2]
        value_args = criteria_args[1::2]
    elif name == "COUNTIFS":
        if len(arguments) < 2 or len(arguments) % 2:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        sum_range = None
        range_args = arguments[::2]
        value_args = arguments[1::2]
    else:
        raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name})
    criteria_ranges = [
        _criteria_reference(argument, workbook, formulas, models, value_overrides,
                            calculated, active, evaluation_count, formula_key)
        for argument in range_args
    ]
    criteria_values = []
    for argument in value_args:
        value = _scalar(_evaluate(argument, workbook, formulas, models, value_overrides,
                                  calculated, active, evaluation_count, formula_key))
        criteria_values.append(_coerce_value(value))
    operation = "COUNTIFS" if name == "COUNTIFS" else "AVERAGEIFS" if name == "AVERAGEIFS" else "SUMIFS"
    return _criteria_result(operation, criteria_ranges, criteria_values, sum_range, formula_key)


def _reference_matrix(argument, workbook: _Workbook, formulas: dict, models: dict,
                      value_overrides: dict, calculated: dict, active: set,
                      evaluation_count: list, formula_key: tuple[int, str]):
    if argument[0] == "reference":
        reference = argument[1]
        value = _read_value(workbook, (reference.sheet_id, reference.address), formulas,
                            models, value_overrides, calculated, active, evaluation_count)
        return [[value]]
    if argument[0] == "range":
        reference = argument[1]
        rows = reference.last_row - reference.first_row + 1
        columns = reference.last_column - reference.first_column + 1
        if rows * columns > MAX_FORMULA_RANGE_CELLS:
            _fail("XLSX_LOOKUP_RANGE_TOO_LARGE")
        matrix = []
        for row in range(reference.first_row, reference.last_row + 1):
            matrix.append([
                _read_value(workbook, (reference.sheet_id, f"{_column_letters(column)}{row}"),
                            formulas, models, value_overrides, calculated, active, evaluation_count)
                for column in range(reference.first_column, reference.last_column + 1)
            ])
        return matrix
    raise SpreadsheetError("XLSX_FORMULA_RANGE_ARGUMENT_UNSUPPORTED",
                           {"sheet_id": formula_key[0], "address": formula_key[1]})


def _lookup_order(left: _Value, right: _Value) -> int:
    first, second = _coerce_value(left), _coerce_value(right)
    if first.kind == "error":
        raise _FormulaFailure(str(first.value))
    if second.kind == "error":
        raise _FormulaFailure(str(second.value))
    if first.kind == second.kind == "blank":
        return 0
    if first.kind == "blank":
        first = _Value("number", Decimal(0))
    if second.kind == "blank":
        second = _Value("number", Decimal(0))
    if first.kind == second.kind == "number":
        return (first.value > second.value) - (first.value < second.value)
    if first.kind == second.kind == "text":
        first_text, second_text = str(first.value).casefold(), str(second.value).casefold()
        return (first_text > second_text) - (first_text < second_text)
    if first.kind == second.kind == "boolean":
        return (bool(first.value) > bool(second.value)) - (bool(first.value) < bool(second.value))
    raise SpreadsheetError("XLSX_LOOKUP_MIXED_TYPES_UNSUPPORTED")


def _lookup_equal(left: _Value, right: _Value, *, wildcard: bool = False) -> bool:
    first, second = _coerce_value(left), _coerce_value(right)
    if wildcard and first.kind == second.kind == "text":
        return _criterion_matches(first, _Value("text", "=" + str(second.value)))
    if first.kind != second.kind:
        return False
    return _lookup_order(first, second) == 0


def _lookup_vector(matrix: list[list[_Value]]):
    row_count, column_count = len(matrix), len(matrix[0]) if matrix else 0
    if row_count == 1:
        return matrix[0], "horizontal", row_count, column_count
    if column_count == 1:
        return [row[0] for row in matrix], "vertical", row_count, column_count
    raise SpreadsheetError("XLSX_LOOKUP_VECTOR_REQUIRED")


def _find_lookup_index(values: list[_Value], lookup: _Value, match_mode: int,
                       search_mode: int, *, wildcard: bool = False,
                       require_sorted: bool = False) -> int | None:
    if require_sorted and match_mode in {-1, 1}:
        orders = [_lookup_order(values[index], values[index + 1])
                  for index in range(len(values) - 1)]
        if match_mode == -1 and any(order > 0 for order in orders):
            raise SpreadsheetError("XLSX_LOOKUP_SORT_UNSUPPORTED")
        if match_mode == 1 and any(order < 0 for order in orders):
            raise SpreadsheetError("XLSX_LOOKUP_SORT_UNSUPPORTED")
    order = range(len(values) - 1, -1, -1) if search_mode == -1 else range(len(values))
    for index in order:
        if _lookup_equal(values[index], lookup,
                         wildcard=wildcard and match_mode in {0, 2}):
            return index
    if match_mode not in {-1, 1}:
        return None
    candidates = []
    for index, value in enumerate(values):
        try:
            comparison = _lookup_order(value, lookup)
        except SpreadsheetError:
            continue
        if match_mode == -1 and comparison < 0:
            candidates.append((value, index))
        elif match_mode == 1 and comparison > 0:
            candidates.append((value, index))
    if not candidates:
        return None
    if match_mode == -1:
        return max(candidates, key=lambda pair: pair[0].value)[1]
    return min(candidates, key=lambda pair: pair[0].value)[1]


def _lookup_function(name: str, arguments: tuple, workbook: _Workbook, formulas: dict,
                     models: dict, value_overrides: dict, calculated: dict, active: set,
                     evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    def scalar(argument):
        return _coerce_value(_scalar(_evaluate(argument, workbook, formulas, models,
                                               value_overrides, calculated, active,
                                               evaluation_count, formula_key)))

    def integer(value) -> int:
        number = _decimal_value(value)
        if number != number.to_integral_value():
            raise _FormulaFailure("#VALUE!")
        return int(number)

    if name in {"MATCH", "XMATCH"}:
        if not 2 <= len(arguments) <= (3 if name == "MATCH" else 4):
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        lookup = scalar(arguments[0])
        vector, _orientation, _rows, _columns = _lookup_vector(
            _reference_matrix(arguments[1], workbook, formulas, models, value_overrides,
                              calculated, active, evaluation_count, formula_key))
        if name == "MATCH":
            match_value = _decimal_value(scalar(arguments[2])) if len(arguments) == 3 else Decimal(1)
            if match_value not in {Decimal(-1), Decimal(0), Decimal(1)}:
                raise SpreadsheetError("XLSX_LOOKUP_MODE_UNSUPPORTED")
            match_mode = int(match_value)
            search_mode = 1
            wildcard = True
        else:
            match_value = _decimal_value(scalar(arguments[2])) if len(arguments) >= 3 else Decimal(0)
            search_value = _decimal_value(scalar(arguments[3])) if len(arguments) == 4 else Decimal(1)
            if match_value not in {Decimal(-1), Decimal(0), Decimal(1), Decimal(2)} or search_value not in {Decimal(-1), Decimal(1)}:
                raise SpreadsheetError("XLSX_LOOKUP_MODE_UNSUPPORTED")
            match_mode, search_mode, wildcard = int(match_value), int(search_value), True
        found = _find_lookup_index(vector, lookup, match_mode, search_mode, wildcard=wildcard,
                                   require_sorted=(name == "MATCH"))
        if found is None:
            raise _FormulaFailure("#N/A")
        return _Value("number", Decimal(found + 1))

    if name == "INDEX":
        if not 2 <= len(arguments) <= 3:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        matrix = _reference_matrix(arguments[0], workbook, formulas, models, value_overrides,
                                   calculated, active, evaluation_count, formula_key)
        row_num = integer(scalar(arguments[1]))
        column_num = integer(scalar(arguments[2])) if len(arguments) == 3 else 1
        if row_num <= 0 or column_num <= 0 or row_num > len(matrix) or column_num > len(matrix[0]):
            raise _FormulaFailure("#REF!")
        return matrix[row_num - 1][column_num - 1]

    if name in {"VLOOKUP", "HLOOKUP"}:
        if not 3 <= len(arguments) <= 4:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        lookup = scalar(arguments[0])
        matrix = _reference_matrix(arguments[1], workbook, formulas, models, value_overrides,
                                   calculated, active, evaluation_count, formula_key)
        if name == "VLOOKUP":
            vector = [row[0] for row in matrix]
            return_index = integer(scalar(arguments[2]))
            is_exact = len(arguments) == 4 and not _truth(scalar(arguments[3]))
            match_mode = 0 if is_exact else -1
            found = _find_lookup_index(vector, lookup, match_mode, 1, wildcard=is_exact,
                                       require_sorted=not is_exact)
            if found is None or return_index <= 0 or return_index > len(matrix[0]):
                raise _FormulaFailure("#N/A")
            return matrix[found][return_index - 1]
        vector = matrix[0]
        return_index = integer(scalar(arguments[2]))
        is_exact = len(arguments) == 4 and not _truth(scalar(arguments[3]))
        match_mode = 0 if is_exact else -1
        found = _find_lookup_index(vector, lookup, match_mode, 1, wildcard=is_exact,
                                   require_sorted=not is_exact)
        if found is None or return_index <= 0 or return_index > len(matrix):
            raise _FormulaFailure("#N/A")
        return matrix[return_index - 1][found]

    if name == "XLOOKUP":
        if not 3 <= len(arguments) <= 6:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        lookup = scalar(arguments[0])
        lookup_vector, orientation, _rows, _columns = _lookup_vector(
            _reference_matrix(arguments[1], workbook, formulas, models, value_overrides,
                              calculated, active, evaluation_count, formula_key))
        return_matrix = _reference_matrix(arguments[2], workbook, formulas, models, value_overrides,
                                          calculated, active, evaluation_count, formula_key)
        return_vector, return_orientation, return_rows, return_columns = _lookup_vector(return_matrix)
        if orientation != return_orientation or len(lookup_vector) != len(return_vector):
            raise SpreadsheetError("XLSX_LOOKUP_RANGE_SHAPE_MISMATCH")
        not_found = arguments[3] if len(arguments) >= 4 else None
        match_mode = int(_decimal_value(scalar(arguments[4]))) if len(arguments) >= 5 else 0
        search_mode = int(_decimal_value(scalar(arguments[5]))) if len(arguments) >= 6 else 1
        if match_mode not in {-1, 0, 1, 2} or search_mode not in {-1, 1}:
            raise SpreadsheetError("XLSX_LOOKUP_MODE_UNSUPPORTED")
        found = _find_lookup_index(lookup_vector, lookup, match_mode, search_mode, wildcard=True)
        if found is None:
            if not_found is None:
                raise _FormulaFailure("#N/A")
            return _evaluate(not_found, workbook, formulas, models, value_overrides,
                             calculated, active, evaluation_count, formula_key)
        return return_vector[found]

    raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name})


def _predicate_function(name: str, argument, workbook: _Workbook, formulas: dict,
                        models: dict, value_overrides: dict, calculated: dict, active: set,
                        evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    try:
        evaluated = _evaluate(argument, workbook, formulas, models, value_overrides,
                              calculated, active, evaluation_count, formula_key)
        value = _coerce_value(_scalar(evaluated))
    except _FormulaFailure as exc:
        value = _Value("error", str(exc))
    if name == "ISERROR":
        result = value.kind == "error"
    elif name == "ISNA":
        result = value.kind == "error" and value.value == "#N/A"
    elif name == "ISNUMBER":
        result = value.kind == "number"
    elif name == "ISTEXT":
        result = value.kind == "text"
    elif name == "ISLOGICAL":
        result = value.kind == "boolean"
    elif name == "ISBLANK":
        result = argument[0] == "reference" and value.kind == "blank"
    else:
        raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name})
    return _Value("boolean", result)


def _sumproduct(arguments: tuple, workbook: _Workbook, formulas: dict, models: dict,
                value_overrides: dict, calculated: dict, active: set,
                evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    if not 1 <= len(arguments) <= 255:
        raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": "SUMPRODUCT"})
    arrays = []
    array_length = 1
    for argument in arguments:
        value = _evaluate(argument, workbook, formulas, models, value_overrides,
                          calculated, active, evaluation_count, formula_key)
        if isinstance(value, list):
            values = [_coerce_value(item) for item in value]
            if array_length != 1 and len(values) not in {1, array_length}:
                raise SpreadsheetError("XLSX_FORMULA_RANGE_SHAPE_MISMATCH", {"function": "SUMPRODUCT"})
            array_length = max(array_length, len(values))
            arrays.append(values)
        else:
            arrays.append([_coerce_value(value)])
    if array_length * len(arrays) > MAX_FORMULA_RANGE_CELLS:
        _fail("XLSX_CALCULATION_LIMIT")
    total = Decimal(0)
    for index in range(array_length):
        product = Decimal(1)
        for values in arrays:
            value = values[0 if len(values) == 1 else index]
            if value.kind == "error":
                raise _FormulaFailure(str(value.value))
            if value.kind in {"blank", "text"}:
                factor = Decimal(0)
            else:
                factor = _decimal_value(value)
            product *= factor
        total += product
    return _Value("number", _excel_round(total))


def _month_start(year: int, month: int) -> date:
    month_index = year * 12 + month - 1
    normalized_year, zero_month = divmod(month_index, 12)
    if normalized_year < 1 or normalized_year > 9999:
        raise _FormulaFailure("#NUM!")
    return date(normalized_year, zero_month + 1, 1)


def _date_serial_parts(value: object, date_1904: bool) -> tuple[int, int, int, int]:
    serial = _decimal_value(value)
    if serial < 0 or serial > Decimal(2_958_465):
        raise _FormulaFailure("#NUM!")
    days = int(serial.to_integral_value(rounding="ROUND_FLOOR"))
    fraction = serial - Decimal(days)
    total_seconds = int((fraction * Decimal(86_400)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if total_seconds >= 86_400:
        days += 1
        total_seconds = 0
    if date_1904:
        try:
            current_date = date(1904, 1, 1) + timedelta(days=days)
        except OverflowError as exc:
            raise _FormulaFailure("#NUM!") from exc
        return current_date.year, current_date.month, current_date.day, total_seconds
    if days == 60:
        return 1900, 2, 29, total_seconds
    adjusted_days = days - 1 if days > 60 else days
    try:
        current_date = date(1899, 12, 31) + timedelta(days=adjusted_days)
    except OverflowError as exc:
        raise _FormulaFailure("#NUM!") from exc
    return current_date.year, current_date.month, current_date.day, total_seconds


def _date_serial_value(value: date, date_1904: bool) -> Decimal:
    if date_1904:
        return Decimal((value - date(1904, 1, 1)).days)
    if value == date(1900, 2, 28):
        return Decimal(59)
    serial = (value - date(1899, 12, 31)).days
    if value >= date(1900, 3, 1):
        serial += 1
    return Decimal(serial)


def _date_formula(name: str, arguments: tuple, workbook: _Workbook, formulas: dict,
                  models: dict, value_overrides: dict, calculated: dict, active: set,
                  evaluation_count: list, formula_key: tuple[int, str]) -> _Value:
    def number(argument) -> Decimal:
        value = _evaluate(argument, workbook, formulas, models, value_overrides,
                          calculated, active, evaluation_count, formula_key)
        return _decimal_value(_scalar(value))

    if name in {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "WEEKDAY"}:
        if len(arguments) not in ({1, 2} if name == "WEEKDAY" else {1}):
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        serial = number(arguments[0])
        if not workbook.date_1904 and serial == 60 and name == "WEEKDAY":
            raise SpreadsheetError("XLSX_1900_LEAP_DAY_WEEKDAY_UNSUPPORTED")
        year, month, day, total_seconds = _date_serial_parts(serial, workbook.date_1904)
        if name == "YEAR":
            return _Value("number", Decimal(year))
        if name == "MONTH":
            return _Value("number", Decimal(month))
        if name == "DAY":
            return _Value("number", Decimal(day))
        if name == "HOUR":
            return _Value("number", Decimal(total_seconds // 3600))
        if name == "MINUTE":
            return _Value("number", Decimal((total_seconds % 3600) // 60))
        if name == "SECOND":
            return _Value("number", Decimal(total_seconds % 60))
        weekday = date(year, month, day).weekday()
        return_type = int(number(arguments[1])) if len(arguments) == 2 else 1
        if return_type == 1:
            return _Value("number", Decimal(((weekday + 1) % 7) + 1))
        if return_type == 2:
            return _Value("number", Decimal(weekday + 1))
        if return_type == 3:
            return _Value("number", Decimal(weekday))
        raise SpreadsheetError("XLSX_DATE_RETURN_TYPE_UNSUPPORTED", {"function": name})

    if name == "DATE":
        if len(arguments) != 3:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        year, month, day = [int(number(argument)) for argument in arguments]
        if year < 0:
            raise _FormulaFailure("#NUM!")
        if year < 1900:
            year += 1900
        first = _month_start(year, month)
        if not workbook.date_1904 and first == date(1900, 2, 1) and day == 29:
            return _Value("number", Decimal(60))
        try:
            target = first + timedelta(days=day - 1)
        except (OverflowError, ValueError) as exc:
            raise _FormulaFailure("#NUM!") from exc
        return _Value("number", _date_serial_value(target, workbook.date_1904))

    if name == "TIME":
        if len(arguments) != 3:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        hours, minutes, seconds = [int(number(argument)) for argument in arguments]
        if min(hours, minutes, seconds) < 0:
            raise _FormulaFailure("#NUM!")
        seconds_in_day = (hours * 3600 + minutes * 60 + seconds) % 86_400
        return _Value("number", _excel_round(Decimal(seconds_in_day) / Decimal(86_400)))

    if name in {"EDATE", "EOMONTH"}:
        if len(arguments) != 2:
            raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
        serial = number(arguments[0])
        months_decimal = number(arguments[1])
        if months_decimal != months_decimal.to_integral_value():
            raise _FormulaFailure("#VALUE!")
        if not workbook.date_1904 and serial == 60:
            raise SpreadsheetError("XLSX_1900_LEAP_DAY_OFFSET_UNSUPPORTED", {"function": name})
        year, month, day, seconds = _date_serial_parts(serial, workbook.date_1904)
        base = date(year, month, day)
        first_target = _month_start(base.year, base.month + int(months_decimal))
        if name == "EOMONTH":
            next_month = _month_start(first_target.year, first_target.month + 1)
            target = next_month - timedelta(days=1)
        else:
            next_month = _month_start(first_target.year, first_target.month + 1)
            target = first_target + timedelta(days=min(day, (next_month - timedelta(days=1)).day) - 1)
        return _Value("number", _date_serial_value(target, workbook.date_1904)
                      + (Decimal(seconds) / Decimal(86_400) if name == "EDATE" else Decimal(0)))

    raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name})


def _evaluate(node, workbook: _Workbook, formulas: dict, models: dict,
              value_overrides: dict, calculated: dict, active: set,
              evaluation_count: list, formula_key: tuple[int, str]):
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "reference":
        reference = node[1]
        return _read_value(workbook, (reference.sheet_id, reference.address), formulas, models,
                           value_overrides, calculated, active, evaluation_count)
    if kind == "range":
        reference = node[1]
        count = (reference.last_row - reference.first_row + 1) * (reference.last_column - reference.first_column + 1)
        if count > MAX_FORMULA_RANGE_CELLS:
            raise SpreadsheetError("XLSX_FORMULA_RANGE_TOO_LARGE")
        return [
            _read_value(workbook, (reference.sheet_id, f"{_column_letters(column)}{row}"),
                        formulas, models, value_overrides, calculated, active, evaluation_count)
            for row in range(reference.first_row, reference.last_row + 1)
            for column in range(reference.first_column, reference.last_column + 1)
        ]
    if kind == "unresolved_name":
        raise SpreadsheetError("XLSX_FORMULA_NAME_UNSUPPORTED", {"name": node[1]})
    if kind == "unary":
        value = _evaluate(node[2], workbook, formulas, models, value_overrides,
                          calculated, active, evaluation_count, formula_key)
        operator = node[1]
        if operator == "%":
            return _Value("number", _decimal_value(_scalar(value)) / Decimal(100))
        number = _decimal_value(_scalar(value))
        return _Value("number", number if operator == "+" else -number)
    if kind == "binary":
        operator = node[1]
        left = _evaluate(node[2], workbook, formulas, models, value_overrides,
                         calculated, active, evaluation_count, formula_key)
        right = _evaluate(node[3], workbook, formulas, models, value_overrides,
                          calculated, active, evaluation_count, formula_key)
        left_value = _scalar(left)
        right_value = _scalar(right)
        if isinstance(left_value, _Value) and left_value.kind == "error":
            raise _FormulaFailure(str(left_value.value))
        if isinstance(right_value, _Value) and right_value.kind == "error":
            raise _FormulaFailure(str(right_value.value))
        if operator == "&":
            return _Value("text", _text_value(_coerce_value(left_value)) + _text_value(_coerce_value(right_value)))
        if operator in {"=", "<>", "<", "<=", ">", ">="}:
            a, b = _coerce_value(left_value), _coerce_value(right_value)
            if a.kind == b.kind == "number":
                first, second = a.value, b.value
            elif a.kind == b.kind == "text":
                first, second = str(a.value).casefold(), str(b.value).casefold()
            elif a.kind == b.kind == "boolean":
                first, second = bool(a.value), bool(b.value)
            else:
                first, second = a.kind, b.kind
            compare = {"=": first == second, "<>": first != second,
                       "<": first < second, "<=": first <= second,
                       ">": first > second, ">=": first >= second}[operator]
            return _Value("boolean", compare)
        first, second = _decimal_value(left_value), _decimal_value(right_value)
        try:
            if operator == "+":
                result = first + second
            elif operator == "-":
                result = first - second
            elif operator == "*":
                result = first * second
            elif operator == "/":
                if second == 0:
                    raise _FormulaFailure("#DIV/0!")
                result = first / second
            else:
                if abs(second) > 1000:
                    raise _FormulaFailure("#NUM!")
                result = first ** int(second) if second == second.to_integral_value() else Decimal(str(float(first) ** float(second)))
        except _FormulaFailure:
            raise
        except (InvalidOperation, OverflowError, ValueError) as exc:
            raise _FormulaFailure("#NUM!") from exc
        if not result.is_finite():
            raise _FormulaFailure("#NUM!")
        return _Value("number", _excel_round(result))
    if kind == "function":
        name, arguments = node[1], node[2]
        supported = {"IF", "IFERROR", "IFNA", "ISERROR", "ISNA", "ISNUMBER", "ISTEXT", "ISLOGICAL",
                     "ISBLANK", "COUNTBLANK", "SUMPRODUCT", "SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA",
                     "SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS",
                     "MATCH", "XMATCH", "INDEX", "VLOOKUP", "HLOOKUP", "XLOOKUP",
                     "DATE", "TIME", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
                     "WEEKDAY", "EDATE", "EOMONTH",
                     "AND", "OR", "NOT", "ABS", "INT", "MOD", "POWER", "ROUND"}
        if name not in supported:
            raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name,
                                                                         "sheet_id": formula_key[0],
                                                                         "address": formula_key[1]})
        if name == "IF":
            if len(arguments) not in {2, 3}:
                raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
            condition = _evaluate(arguments[0], workbook, formulas, models, value_overrides,
                                  calculated, active, evaluation_count, formula_key)
            selected = arguments[1] if _truth(_scalar(condition)) else (arguments[2] if len(arguments) == 3 else None)
            if selected is None:
                return _Value("boolean", False)
            return _evaluate(selected, workbook, formulas, models, value_overrides, calculated,
                             active, evaluation_count, formula_key)
        if name in {"IFERROR", "IFNA"}:
            if len(arguments) != 2:
                raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
            try:
                value = _evaluate(arguments[0], workbook, formulas, models, value_overrides,
                                  calculated, active, evaluation_count, formula_key)
            except _FormulaFailure as exc:
                if name == "IFERROR" or str(exc) == "#N/A":
                    return _evaluate(arguments[1], workbook, formulas, models, value_overrides,
                                     calculated, active, evaluation_count, formula_key)
                raise
            if isinstance(value, _Value) and value.kind == "error":
                if name == "IFERROR" or value.value == "#N/A":
                    return _evaluate(arguments[1], workbook, formulas, models, value_overrides,
                                     calculated, active, evaluation_count, formula_key)
            return value
        if name in {"ISERROR", "ISNA", "ISNUMBER", "ISTEXT", "ISLOGICAL", "ISBLANK"}:
            if len(arguments) != 1:
                raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
            return _predicate_function(name, arguments[0], workbook, formulas, models,
                                       value_overrides, calculated, active,
                                       evaluation_count, formula_key)
        if name == "COUNTBLANK":
            if len(arguments) != 1:
                raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
            values = _criteria_reference(arguments[0], workbook, formulas, models, value_overrides,
                                         calculated, active, evaluation_count, formula_key)
            count = sum(item.kind == "blank" or item.kind == "text" and item.value == "" for item in values)
            return _Value("number", Decimal(count))
        if name == "SUMPRODUCT":
            return _sumproduct(arguments, workbook, formulas, models, value_overrides,
                               calculated, active, evaluation_count, formula_key)
        if name in {"DATE", "TIME", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
                    "WEEKDAY", "EDATE", "EOMONTH"}:
            return _date_formula(name, arguments, workbook, formulas, models, value_overrides,
                                  calculated, active, evaluation_count, formula_key)
        if name in {"SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS"}:
            return _conditional_aggregate(name, arguments, workbook, formulas, models,
                                          value_overrides, calculated, active,
                                          evaluation_count, formula_key)
        if name in {"MATCH", "XMATCH", "INDEX", "VLOOKUP", "HLOOKUP", "XLOOKUP"}:
            return _lookup_function(name, arguments, workbook, formulas, models,
                                    value_overrides, calculated, active,
                                    evaluation_count, formula_key)
        if name in {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA"}:
            return _aggregate_formula(name, arguments, workbook, formulas, models, value_overrides,
                                      calculated, active, evaluation_count, formula_key)
        if name in {"AND", "OR"}:
            if not arguments or len(arguments) > 255:
                raise SpreadsheetError("XLSX_FORMULA_ARGUMENTS_INVALID", {"function": name})
            flags = []
            for argument in arguments:
                value = _evaluate(argument, workbook, formulas, models, value_overrides,
                                  calculated, active, evaluation_count, formula_key)
                from_reference = argument[0] in {"reference", "range"}
                items = value if isinstance(value, list) else [value]
                if not from_reference and len(items) != 1:
                    raise _FormulaFailure("#VALUE!")
                for item in items:
                    current = _coerce_value(item)
                    if current.kind == "error":
                        raise _FormulaFailure(str(current.value))
                    if current.kind in {"blank", "text"}:
                        if from_reference:
                            continue
                        raise _FormulaFailure("#VALUE!")
                    flags.append(_truth(current))
            if not flags:
                raise _FormulaFailure("#VALUE!")
            return _Value("boolean", all(flags) if name == "AND" else any(flags))
        evaluated = [_evaluate(item, workbook, formulas, models, value_overrides,
                               calculated, active, evaluation_count, formula_key)
                     for item in arguments]
        scalar_args = [_scalar(item) for item in evaluated]
        if name == "NOT" and len(scalar_args) == 1:
            return _Value("boolean", not _truth(_coerce_value(scalar_args[0])))
        if name == "ABS" and len(scalar_args) == 1:
            return _Value("number", abs(_decimal_value(scalar_args[0])))
        if name == "INT" and len(scalar_args) == 1:
            return _Value("number", _decimal_value(scalar_args[0]).to_integral_value(rounding="ROUND_FLOOR"))
        if name == "MOD" and len(scalar_args) == 2:
            divisor = _decimal_value(scalar_args[1])
            if divisor == 0:
                raise _FormulaFailure("#DIV/0!")
            dividend = _decimal_value(scalar_args[0])
            return _Value("number", dividend - (dividend / divisor).to_integral_value(rounding="ROUND_FLOOR") * divisor)
        if name == "POWER" and len(scalar_args) == 2:
            return _evaluate(("binary", "^", ("literal", _coerce_value(scalar_args[0])),
                              ("literal", _coerce_value(scalar_args[1]))), workbook, formulas, models,
                             value_overrides, calculated, active, evaluation_count, formula_key)
        if name == "ROUND" and len(scalar_args) == 2:
            number = _decimal_value(scalar_args[0])
            places = _decimal_value(scalar_args[1])
            if places != places.to_integral_value() or abs(places) > 100:
                raise _FormulaFailure("#NUM!")
            quantum = Decimal(1).scaleb(-int(places))
            try:
                return _Value("number", number.quantize(quantum, rounding=ROUND_HALF_UP))
            except InvalidOperation as exc:
                raise _FormulaFailure("#NUM!") from exc
        raise SpreadsheetError("XLSX_FORMULA_FUNCTION_UNSUPPORTED", {"function": name,
                                                                     "sheet_id": formula_key[0],
                                                                     "address": formula_key[1]})
    raise SpreadsheetError("XLSX_FORMULA_UNSUPPORTED", {"sheet_id": formula_key[0], "address": formula_key[1]})


def _scalar(value):
    if isinstance(value, list):
        if len(value) != 1:
            raise _FormulaFailure("#VALUE!")
        return value[0]
    return value


def _coerce_value(value) -> _Value:
    if isinstance(value, _Value):
        return value
    if value is None:
        return _Value("blank")
    if isinstance(value, bool):
        return _Value("boolean", value)
    if isinstance(value, Decimal):
        return _Value("number", value)
    if isinstance(value, str):
        return _Value("text", value)
    raise _FormulaFailure("#VALUE!")


def _decimal_value(value) -> Decimal:
    normalized = _coerce_value(value)
    if normalized.kind == "error":
        raise _FormulaFailure(str(normalized.value))
    if normalized.kind == "boolean":
        return Decimal(1 if normalized.value else 0)
    if normalized.kind == "blank":
        return Decimal(0)
    return _decimal(normalized.value)


def _formula_tree(workbook: _Workbook, formula: str, key: tuple[int, str]):
    try:
        return _FormulaParser(formula, workbook, key[0]).parse()
    except _FormulaSyntax as exc:
        raise SpreadsheetError("XLSX_FORMULA_UNSUPPORTED", {"sheet_id": key[0], "address": key[1]}) from exc


def _make_dependency_graph(workbook: _Workbook, formulas: dict, models: dict):
    dependents: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
    formula_keys = set(formulas)
    total_edges = 0
    unknown = []
    for formula_key, (_tree, refs) in models.items():
        if refs is None:
            unknown.append(formula_key)
            continue
        for reference in refs:
            for referenced_cell in _reference_cells(reference):
                total_edges += 1
                if total_edges > 2_000_000:
                    _fail("XLSX_DEPENDENCY_GRAPH_TOO_LARGE")
                dependents[referenced_cell].add(formula_key)
    return dependents, formula_keys, unknown


def _affected_formulas(changed_keys: set[tuple[int, str]], dependents: dict):
    queue = list(changed_keys)
    affected = set()
    while queue:
        key = queue.pop()
        for formula_key in dependents.get(key, ()):
            if formula_key not in affected:
                affected.add(formula_key)
                queue.append(formula_key)
    return affected


def _needed_formula_closure(affected: set, models: dict, formula_keys: set):
    needed = set(affected)
    pending = list(affected)
    while pending:
        key = pending.pop()
        _tree, refs = models.get(key, (None, None))
        if _tree is None or refs is None:
            _fail("XLSX_FORMULA_UNSUPPORTED", sheet_id=key[0], address=key[1])
        for reference in refs:
            for cell_key in _reference_cells(reference):
                if cell_key in formula_keys and cell_key not in needed:
                    needed.add(cell_key)
                    pending.append(cell_key)
    return needed


def _calculation_results(workbook: _Workbook, formula_overrides: dict[tuple[int, str], str | None],
                         value_overrides: dict[tuple[int, str], _Value],
                         changed_keys: set[tuple[int, str]]):
    formulas, models = _formula_models(workbook, formula_overrides)
    dependents, formula_keys, unknown = _make_dependency_graph(workbook, formulas, models)
    if unknown:
        _fail("XLSX_FORMULA_DEPENDENCIES_UNSUPPORTED", formulas=len(unknown))
    affected = _affected_formulas(changed_keys, dependents)
    affected.update(key for key in changed_keys if key in formula_keys)
    needed = _needed_formula_closure(affected, models, formula_keys)
    for key in needed:
        tree, refs = models.get(key, (None, None))
        if tree is None or refs is None:
            _fail("XLSX_FORMULA_UNSUPPORTED", sheet_id=key[0], address=key[1])
        if key in affected and workbook.formula_modes.get(key) == "dataTable":
            _fail("XLSX_DATA_TABLE_RECALC_UNSUPPORTED", sheet_id=key[0], address=key[1])
        if key in affected and workbook.formula_modes.get(key) == "array":
            cell = workbook.cells.get(key)
            formula = cell.find(_q("f")) if cell is not None else None
            bounds = _range_bounds(formula.get("ref", "")) if formula is not None else None
            if bounds is None or (bounds[2] - bounds[0] + 1) * (bounds[3] - bounds[1] + 1) != 1:
                _fail("XLSX_ARRAY_FORMULA_RECALC_UNSUPPORTED", sheet_id=key[0], address=key[1])

    calculated = {}
    active = set()
    counter = [0]
    results = {}
    for key in sorted(needed):
        try:
            result = _evaluate(models[key][0], workbook, formulas, models, value_overrides,
                               calculated, active, counter, key)
            if not isinstance(result, _Value):
                _fail("XLSX_FORMULA_RESULT_INVALID", sheet_id=key[0], address=key[1])
        except _FormulaFailure as exc:
            result = _Value("error", str(exc))
        results[key] = result
    return formulas, results, affected


def calculate_spreadsheet_cells(content: bytes, cell_refs: list[str]) -> dict:
    """Calculate requested cells from workbook formulas, never from old caches.

    Each cell reference is the stable object id returned by
    :func:`inspect_spreadsheet`: ``xlsx:<source_sha256>:<sheet_id>:<A1>``.
    Unsupported formula paths are returned per cell so independent requested
    values remain usable.
    """
    workbook = _load_workbook(content)
    source_hash = _sha256(content)
    if not isinstance(cell_refs, list) or not 1 <= len(cell_refs) <= MAX_CHANGES:
        _fail("XLSX_CALCULATION_REQUEST_INVALID", maximum=MAX_CHANGES)
    formulas, models = _formula_models(workbook, {})
    computed = []
    unresolved = []
    seen = set()
    for index, object_id in enumerate(cell_refs):
        if not isinstance(object_id, str) or len(object_id) > 200:
            _fail("XLSX_CALCULATION_REFERENCE_INVALID", index=index)
        match = re.fullmatch(r"xlsx:([0-9a-f]{64}):([1-9][0-9]*):([A-Z]{1,3}[1-9][0-9]*)", object_id)
        if match is None or match.group(1) != source_hash:
            _fail("XLSX_CALCULATION_REFERENCE_STALE", index=index)
        sheet_id, raw_address = int(match.group(2)), match.group(3)
        if sheet_id not in workbook.sheets:
            _fail("XLSX_CALCULATION_REFERENCE_INVALID", index=index)
        row, column = _cell_position(raw_address)
        address = f"{_column_letters(column)}{row}"
        key = (sheet_id, address)
        if key in seen:
            _fail("XLSX_CALCULATION_REFERENCE_DUPLICATED", index=index)
        seen.add(key)
        base = {"object_id": object_id, "sheet_id": sheet_id,
                "sheet": workbook.sheets[sheet_id]["name"], "address": address,
                "formula": "=" + formulas[key] if key in formulas else None}
        if key in formulas:
            tree, refs = models.get(key, (None, None))
            cell = workbook.cells.get(key)
            formula = cell.find(_q("f")) if cell is not None else None
            if formula is not None and formula.get("t") == "dataTable":
                unresolved.append({**base, "reason": "XLSX_ARRAY_FORMULA_UNSUPPORTED"})
                continue
            if formula is not None and formula.get("t") == "array":
                bounds = _range_bounds(formula.get("ref", ""))
                if bounds is None or (bounds[2] - bounds[0] + 1) * (bounds[3] - bounds[1] + 1) != 1:
                    unresolved.append({**base, "reason": "XLSX_ARRAY_FORMULA_UNSUPPORTED"})
                    continue
            if tree is None or refs is None:
                unresolved.append({**base, "reason": "XLSX_FORMULA_DEPENDENCIES_UNSUPPORTED"})
                continue
        calculated_values = {}
        try:
            value = _read_value(workbook, key, formulas, models, {}, calculated_values, set(), [0])
        except (SpreadsheetError, _FormulaFailure) as exc:
            reason = getattr(exc, "code", None) or str(exc)
            unresolved.append({**base, "reason": reason})
            continue
        cycle_cell = next((cell_key for cell_key, result in calculated_values.items()
                           if result.kind == "error" and result.value == "#CIRCULAR!"), None)
        if value.kind == "error" and value.value == "#CIRCULAR!" or cycle_cell is not None:
            unresolved.append({**base, "reason": "XLSX_FORMULA_CIRCULAR_DEPENDENCY",
                               "cycle_cell": ({"sheet_id": cycle_cell[0], "address": cycle_cell[1]}
                                              if cycle_cell else None)})
            continue
        computed.append({**base, "value": _typed_json(value, verified=True)})
    return {
        "source_sha256": source_hash,
        "calculation_engine": CALCULATION_ENGINE,
        "calculation_revision": CALCULATION_REVISION,
        "computed": computed,
        "unresolved": unresolved,
    }


def _cell_text_nodes(node) -> list:
    return node.xpath(".//x:t[not(ancestor::x:rPh)]", namespaces={"x": NS_MAIN})


def _set_text_value(node, before: str, after: str) -> None:
    text_nodes = _cell_text_nodes(node)
    current = "".join(item.text or "" for item in text_nodes)
    if current != before:
        _fail("XLSX_CHANGE_READBACK_FAILED")
    if node.find(_q("rPh")) is not None:
        _fail("XLSX_PHONETIC_TEXT_EDIT_UNSUPPORTED")
    if not text_nodes:
        if not after:
            return
        inline = node
        text = etree.SubElement(inline, _q("t"))
        text.text = after
        if after[:1].isspace() or after[-1:].isspace():
            text.set(f"{{{NS_XML}}}space", "preserve")
        return
    from difflib import SequenceMatcher
    run_values = [item.text or "" for item in text_nodes]
    run_for_character = [run_index for run_index, value in enumerate(run_values) for _ in value]
    assigned_runs: list[int] = []

    def inherited_run(position: int) -> int:
        if run_for_character:
            if position > 0:
                return run_for_character[min(position - 1, len(run_for_character) - 1)]
            return run_for_character[0]
        return 0

    matcher = SequenceMatcher(None, before, after, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            assigned_runs.extend(run_for_character[index] for index in range(old_start, old_end))
        elif tag in {"replace", "insert"}:
            assigned_runs.extend([inherited_run(old_start)] * (new_end - new_start))
    if len(assigned_runs) != len(after):
        _fail("XLSX_TEXT_RANGE_INVALID")
    values = [[] for _ in text_nodes]
    for character, run_index in zip(after, assigned_runs):
        values[run_index].append(character)
    for node_text, characters in zip(text_nodes, values):
        value = "".join(characters)
        node_text.text = value
        if value[:1].isspace() or value[-1:].isspace():
            node_text.set(f"{{{NS_XML}}}space", "preserve")
        else:
            node_text.attrib.pop(f"{{{NS_XML}}}space", None)


def _xml_text_valid(value: str) -> bool:
    return all(character in "\t\n\r" or ord(character) >= 0x20
               and not 0xD800 <= ord(character) <= 0xDFFF
               and ord(character) <= 0x10FFFF for character in value)


def _typed_input(value, *, field: str) -> _Value:
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        _fail("XLSX_TYPED_VALUE_INVALID", field=field)
    kind, raw = value["kind"], value["value"]
    if kind == "text":
        if not isinstance(raw, str) or len(raw) > MAX_CHANGE_TEXT_CHARS or not _xml_text_valid(raw):
            _fail("XLSX_TEXT_VALUE_INVALID", field=field)
        return _Value("text", raw)
    if kind == "number":
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            _fail("XLSX_NUMBER_VALUE_INVALID", field=field)
        text = str(raw)
        if len(text) > 100 or not NUMBER.fullmatch(text):
            _fail("XLSX_NUMBER_VALUE_INVALID", field=field)
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise SpreadsheetError("XLSX_NUMBER_VALUE_INVALID", {"field": field}) from exc
        if not number.is_finite() or len(number.as_tuple().digits) > 15 or abs(number.adjusted()) > 300:
            _fail("XLSX_NUMBER_PRECISION_UNSUPPORTED", field=field)
        return _Value("number", number)
    if kind == "boolean" and isinstance(raw, bool):
        return _Value("boolean", raw)
    if kind == "blank" and raw is None:
        return _Value("blank")
    if kind == "formula":
        if not isinstance(raw, str) or not raw.startswith("=") or len(raw) > MAX_CHANGE_TEXT_CHARS:
            _fail("XLSX_FORMULA_VALUE_INVALID", field=field)
        return _Value("formula", raw.removeprefix("="))
    if kind == "date" and isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw) if "T" in raw else date.fromisoformat(raw)
        except ValueError as exc:
            raise SpreadsheetError("XLSX_DATE_VALUE_INVALID", {"field": field}) from exc
        if isinstance(parsed, datetime) and parsed.tzinfo is not None:
            _fail("XLSX_DATE_TIMEZONE_UNSPECIFIED", field=field)
        return _Value("date", parsed)
    _fail("XLSX_TYPED_VALUE_INVALID", field=field)


def _typed_json(value: _Value, *, verified: bool | None = None) -> dict:
    result = {"kind": value.kind}
    if value.kind == "number":
        result["value"] = format(value.value.normalize(), "f")
    elif value.kind == "date":
        result["value"] = value.value.isoformat()
    elif value.kind == "formula":
        result["value"] = "=" + str(value.value).removeprefix("=")
    elif value.kind != "blank":
        result["value"] = value.value
    if verified is not None:
        result["verified"] = verified
    return result


def _date_serial(value: date | datetime, date_1904: bool) -> Decimal:
    if isinstance(value, datetime):
        days = Decimal((value - datetime(value.year, value.month, value.day)).total_seconds()) / Decimal(86400)
        date_value = value.date()
    else:
        days = Decimal(0)
        date_value = value
    base = date(1904, 1, 1) if date_1904 else date(1899, 12, 31)
    serial = Decimal((date_value - base).days) + days
    if not date_1904 and date_value >= date(1900, 3, 1):
        serial += 1
    return serial


def _serialize_number(value: Decimal) -> str:
    if value == 0:
        return "0"
    normalized = value.normalize()
    if normalized.adjusted() >= 12 or normalized.adjusted() <= -8:
        return format(normalized, "E").replace("E+", "E")
    return format(normalized, "f")


def _is_formula_member(workbook: _Workbook, key: tuple[int, str]) -> bool:
    cell = workbook.cells.get(key)
    formula = cell.find(_q("f")) if cell is not None else None
    return formula is not None


def _range_bounds(value: str) -> tuple[int, int, int, int] | None:
    endpoints = value.split(":")
    if len(endpoints) == 1:
        endpoints *= 2
    if len(endpoints) != 2:
        return None
    try:
        first = _cell_position(endpoints[0])
        last = _cell_position(endpoints[1])
    except SpreadsheetError:
        return None
    return first[0], first[1], last[0], last[1]


def _inside_range(address: str, bounds: tuple[int, int, int, int]) -> bool:
    row, column = _cell_position(address)
    first_row, first_column, last_row, last_column = bounds
    return first_row <= row <= last_row and first_column <= column <= last_column


def _guard_edit(workbook: _Workbook, key: tuple[int, str], after: _Value) -> None:
    cell = workbook.cells.get(key)
    if cell is not None:
        formula = cell.find(_q("f"))
        if formula is not None and formula.get("t") in {"array", "dataTable"}:
            _fail("XLSX_ARRAY_FORMULA_EDIT_UNSUPPORTED", sheet_id=key[0], address=key[1])
        if formula is not None and formula.get("t") == "shared":
            _fail("XLSX_SHARED_FORMULA_EDIT_UNSUPPORTED", sheet_id=key[0], address=key[1])

    root = workbook.sheet_roots[key[0]]
    merges = root.find(_q("mergeCells"))
    if merges is not None:
        for merged in merges.findall(_q("mergeCell")):
            bounds = _range_bounds(merged.get("ref", ""))
            if bounds and _inside_range(key[1], bounds):
                anchor = merged.get("ref", "").split(":", 1)[0].replace("$", "").upper()
                if anchor != key[1]:
                    _fail("XLSX_MERGED_CELL_NOT_EDITABLE", sheet_id=key[0], address=key[1])

    for part, raw in workbook.parts.items():
        if not part.startswith("xl/tables/") or not part.endswith(".xml"):
            continue
        table = _parse_xml(raw, part)
        bounds = _range_bounds(table.get("ref", ""))
        if bounds is None:
            continue
        # A table belongs to the worksheet through its relationship. Match the
        # table part against the worksheet's table relationship before applying
        # table-specific edit guards.
        rel_part = posixpath.join(posixpath.dirname(workbook.sheets[key[0]]["part"]), "_rels",
                                  posixpath.basename(workbook.sheets[key[0]]["part"]) + ".rels")
        rel_raw = workbook.parts.get(rel_part)
        if rel_raw is None:
            continue
        rels = _parse_xml(rel_raw, rel_part)
        targets = {_internal_target(workbook.sheets[key[0]]["part"], relation.get("Target", ""))
                   for relation in rels.findall(f"{{{NS_PACKAGE_REL}}}Relationship")
                   if relation.get("Type", "").endswith("/table")}
        if part not in targets or not _inside_range(key[1], bounds):
            continue
        first_row, first_column, _last_row, last_column = bounds
        row, column = _cell_position(key[1])
        if row == first_row and after.kind != "text":
            _fail("XLSX_TABLE_HEADER_TYPE_UNSUPPORTED", sheet_id=key[0], address=key[1])
        if row == first_row and after.kind == "text":
            _fail("XLSX_TABLE_HEADER_EDIT_UNSUPPORTED", sheet_id=key[0], address=key[1])
        if table.get("totalsRowCount", "0") not in {"0", ""} and row == bounds[2]:
            _fail("XLSX_TABLE_TOTALS_EDIT_UNSUPPORTED", sheet_id=key[0], address=key[1])
        columns = table.find(_q("tableColumns"))
        if columns is not None:
            table_column = columns.findall(_q("tableColumn"))[column - first_column]
            if table_column.find(_q("calculatedColumnFormula")) is not None:
                _fail("XLSX_TABLE_CALCULATED_COLUMN_EDIT_UNSUPPORTED", sheet_id=key[0], address=key[1])


def _replace_or_create_cell(workbook: _Workbook, key: tuple[int, str], value: _Value,
                            *, shared_item_index: int | None = None) -> None:
    sheet_id, address = key
    root = workbook.sheet_roots[sheet_id]
    sheet_data = root.find(_q("sheetData"))
    if sheet_data is None:
        _fail("XLSX_SHEET_DATA_MISSING", sheet=workbook.sheets[sheet_id]["name"])
    cell = workbook.cells.get(key)
    if cell is None:
        cell = etree.Element(_q("c"), r=address)
        position = _cell_position(address)
        row_number = position[0]
        row_node = None
        for candidate in sheet_data.findall(_q("row")):
            try:
                candidate_row = int(candidate.get("r", "0"))
            except ValueError:
                _fail("XLSX_ROW_ADDRESS_INVALID", sheet=workbook.sheets[sheet_id]["name"])
            if candidate_row == row_number:
                row_node = candidate
                break
            if candidate_row > row_number:
                row_node = etree.Element(_q("row"), r=str(row_number))
                sheet_data.insert(sheet_data.index(candidate), row_node)
                break
        if row_node is None:
            row_node = etree.SubElement(sheet_data, _q("row"), r=str(row_number))
        cell_position = position[1]
        inserted = False
        for candidate in row_node.findall(_q("c")):
            if _cell_position(candidate.get("r", "A1"))[1] > cell_position:
                row_node.insert(row_node.index(candidate), cell)
                inserted = True
                break
        if not inserted:
            row_node.append(cell)
        workbook.cells[key] = cell
    formula = cell.find(_q("f"))
    inline = cell.find(_q("is"))
    value_node = cell.find(_q("v"))
    if formula is not None:
        cell.remove(formula)
    if inline is not None:
        cell.remove(inline)
    if value_node is not None:
        cell.remove(value_node)

    if value.kind == "blank":
        cell.attrib.pop("t", None)
        if not cell.attrib.get("s") and len(cell) == 0:
            parent = cell.getparent()
            parent.remove(cell)
            workbook.cells.pop(key, None)
        return
    if value.kind == "text":
        if shared_item_index is not None:
            cell.set("t", "s")
            etree.SubElement(cell, _q("v")).text = str(shared_item_index)
        else:
            cell.set("t", "inlineStr")
            inline = etree.SubElement(cell, _q("is"))
            text = etree.SubElement(inline, _q("t"))
            text.text = str(value.value)
            if value.value[:1].isspace() or value.value[-1:].isspace():
                text.set(f"{{{NS_XML}}}space", "preserve")
        return
    if value.kind == "formula":
        cell.attrib.pop("t", None)
        etree.SubElement(cell, _q("f")).text = str(value.value)
        etree.SubElement(cell, _q("v"))
        return
    if value.kind == "boolean":
        cell.set("t", "b")
        etree.SubElement(cell, _q("v")).text = "1" if value.value else "0"
        return
    if value.kind in {"number", "date"}:
        cell.attrib.pop("t", None)
        number = _date_serial(value.value, False) if value.kind == "date" else value.value
        etree.SubElement(cell, _q("v")).text = _serialize_number(number)
        return
    _fail("XLSX_TYPED_VALUE_INVALID")


def _write_formula_cache(cell, value: _Value) -> None:
    if value.kind == "number":
        cell.attrib.pop("t", None)
        serialized = _serialize_number(value.value)
    elif value.kind == "text":
        cell.set("t", "str")
        serialized = str(value.value)
    elif value.kind == "boolean":
        cell.set("t", "b")
        serialized = "1" if value.value else "0"
    elif value.kind == "error":
        cell.set("t", "e")
        serialized = str(value.value)
    elif value.kind == "blank":
        cell.attrib.pop("t", None)
        serialized = ""
    else:
        _fail("XLSX_FORMULA_RESULT_INVALID")
    value_node = cell.find(_q("v"))
    if value_node is None:
        value_node = etree.SubElement(cell, _q("v"))
    value_node.text = serialized


def _count_shared_string_uses(workbook: _Workbook) -> int:
    total = 0
    for cell in workbook.cells.values():
        if cell.get("t") == "s":
            total += 1
    return total


def _serialize_xml(root) -> bytes:
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def _write_package(workbook: _Workbook, changed_parts: set[str]) -> bytes:
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            archive.comment = workbook.comment
            for info in workbook.infos:
                archive.writestr(info, workbook.parts[info.filename])
            if "xl/sharedStrings.xml" in changed_parts and "xl/sharedStrings.xml" not in workbook.parts:
                archive.writestr("xl/sharedStrings.xml", workbook.parts["xl/sharedStrings.xml"],
                                 compress_type=zipfile.ZIP_DEFLATED)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise SpreadsheetError("XLSX_PACKAGE_WRITE_FAILED") from exc
    return output.getvalue()


def _shared_item_for_cell(workbook: _Workbook, cell, before: str, after: str) -> int | None:
    if cell is None:
        return None
    if cell.get("t") != "s":
        return None
    if workbook.shared_root is None:
        _fail("XLSX_SHARED_STRING_TABLE_MISSING")
    value_node = cell.find(_q("v"))
    try:
        index = int(value_node.text if value_node is not None else "")
        original_item = workbook.shared_items[index]
    except (ValueError, IndexError, AttributeError) as exc:
        raise SpreadsheetError("XLSX_SHARED_STRING_REFERENCE_INVALID") from exc
    clone = copy.deepcopy(original_item)
    _set_text_value(clone, before, after)
    workbook.shared_root.append(clone)
    workbook.shared_items.append(clone)
    return len(workbook.shared_items) - 1


def _remove_calc_chain(workbook: _Workbook) -> None:
    target = "xl/calcChain.xml"
    if target not in workbook.parts:
        return
    rel_part = "xl/_rels/workbook.xml.rels"
    rels = _parse_xml(workbook.parts[rel_part], rel_part)
    for relation in list(rels.findall(f"{{{NS_PACKAGE_REL}}}Relationship")):
        resolved = _internal_target("xl/workbook.xml", relation.get("Target", ""))
        if resolved == target or relation.get("Type", "").endswith("/calcChain"):
            rels.remove(relation)
    workbook.parts[rel_part] = _serialize_xml(rels)

    types_part = "[Content_Types].xml"
    if types_part in workbook.parts:
        types = _parse_xml(workbook.parts[types_part], types_part)
        for item in list(types):
            if item.get("PartName") == "/xl/calcChain.xml":
                types.remove(item)
        workbook.parts[types_part] = _serialize_xml(types)
    workbook.parts.pop(target, None)
    workbook.infos = [info for info in workbook.infos if info.filename != target]


def _set_full_recalculation(workbook: _Workbook) -> None:
    part = "xl/workbook.xml"
    root = _parse_xml(workbook.parts[part], part)
    calc = root.find(_q("calcPr"))
    if calc is None:
        calc = etree.SubElement(root, _q("calcPr"))
    calc.set("calcMode", "auto")
    calc.set("fullCalcOnLoad", "1")
    calc.set("forceFullCalc", "1")
    workbook.parts[part] = _serialize_xml(root)


def _chart_references(workbook: _Workbook, formula: str):
    try:
        tree = _FormulaParser(formula, workbook, next(iter(workbook.sheets))).parse()
    except _FormulaSyntax as exc:
        raise SpreadsheetError("XLSX_CHART_CACHE_SCOPE_UNSUPPORTED") from exc
    references = _ast_references(tree)
    if references is None or len(references) != 1:
        _fail("XLSX_CHART_CACHE_SCOPE_UNSUPPORTED")
    return references[0]


def _chart_reference_cells(reference: _CellRef | _CellRange):
    if isinstance(reference, _CellRef):
        return [((reference.sheet_id, reference.address), 0)]
    count = (reference.last_row - reference.first_row + 1) * (reference.last_column - reference.first_column + 1)
    if count > MAX_FORMULA_RANGE_CELLS:
        _fail("XLSX_CHART_RANGE_TOO_LARGE")
    return [
        ((reference.sheet_id, f"{_column_letters(column)}{row}"), index)
        for index, (row, column) in enumerate(
            ( (row, column)
              for row in range(reference.first_row, reference.last_row + 1)
              for column in range(reference.first_column, reference.last_column + 1) )
        )
    ]


def _update_chart_cache(cache_owner, cache_name: str, points: list[tuple[int, str]],
                        point_count: int) -> None:
    namespace = {"c": NS_CHART}
    existing = cache_owner.find(f"{{{NS_CHART}}}{cache_name}")
    cache = copy.deepcopy(existing) if existing is not None else etree.Element(f"{{{NS_CHART}}}{cache_name}")
    for child in list(cache):
        if child.tag in {f"{{{NS_CHART}}}ptCount", f"{{{NS_CHART}}}pt"}:
            cache.remove(child)
    count = etree.Element(f"{{{NS_CHART}}}ptCount")
    count.set("val", str(point_count))
    format_code = cache.find(f"{{{NS_CHART}}}formatCode")
    if cache_name == "numCache":
        if format_code is None:
            format_code = etree.Element(f"{{{NS_CHART}}}formatCode")
        insertion = cache.index(format_code) + 1 if format_code.getparent() is cache else 0
    else:
        insertion = 0
    cache.insert(insertion, count)
    for offset, (index, value) in enumerate(points, insertion + 1):
        point = etree.Element(f"{{{NS_CHART}}}pt", idx=str(index))
        node = etree.SubElement(point, f"{{{NS_CHART}}}v")
        node.text = value
        cache.insert(offset, point)
    if existing is None:
        formula_node = cache_owner.find(f"{{{NS_CHART}}}f")
        if formula_node is None:
            _fail("XLSX_CHART_FORMULA_MISSING")
        cache_owner.insert(cache_owner.index(formula_node) + 1, cache)
    else:
        cache_owner.replace(existing, cache)


def _refresh_chart_caches(workbook: _Workbook, formulas: dict,
                          models: dict, value_overrides: dict, calculated: dict,
                          changed_keys: set[tuple[int, str]], affected: set[tuple[int, str]]) -> list[str]:
    updated_parts = []
    changed_targets = changed_keys | affected
    for part in sorted(name for name in workbook.parts if name.startswith("xl/charts/") and name.endswith(".xml")):
        root = _parse_xml(workbook.parts[part], part)
        changed = False
        for owner in root.iter():
            local_name = etree.QName(owner).localname
            if local_name not in {"numRef", "strRef"}:
                continue
            formula_node = owner.find(f"{{{NS_CHART}}}f")
            if formula_node is None or not (formula_node.text or "").strip():
                continue
            reference = _chart_references(workbook, formula_node.text.strip())
            cells = _chart_reference_cells(reference)
            if not any(_range_contains(reference, key) for key in changed_targets):
                continue
            values = []
            for key, index in cells:
                value = _read_value(workbook, key, formulas, models, value_overrides,
                                    calculated, set(), [0])
                if value.kind == "error":
                    _fail("XLSX_CHART_SOURCE_ERROR", sheet_id=key[0], address=key[1])
                if value.kind == "blank":
                    continue
                if local_name == "numRef":
                    if value.kind != "number":
                        continue
                    rendered = _serialize_number(value.value)
                else:
                    if value.kind == "text":
                        rendered = str(value.value)
                    elif value.kind == "number":
                        rendered = _serialize_number(value.value)
                    elif value.kind == "boolean":
                        rendered = "TRUE" if value.value else "FALSE"
                    else:
                        continue
                values.append((index, rendered))
            cache_name = "numCache" if local_name == "numRef" else "strCache"
            _update_chart_cache(owner, cache_name, values, len(cells))
            changed = True
        if changed:
            workbook.parts[part] = _serialize_xml(root)
            updated_parts.append(part)
    return updated_parts


def _without_chart_caches(root):
    cloned = copy.deepcopy(root)
    for reference in cloned.iter():
        if etree.QName(reference).localname not in {"numRef", "strRef"}:
            continue
        for cache_name in ("numCache", "strCache"):
            cache = reference.find(f"{{{NS_CHART}}}{cache_name}")
            if cache is not None:
                reference.remove(cache)
    return cloned


def _verify_package_preservation(original: _Workbook, updated: _Workbook,
                                 changed_cells: set[tuple[int, str]],
                                 recalculated: set[tuple[int, str]],
                                 changed_parts: set[str]) -> None:
    allowed_parts = set(changed_parts) | {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"}
    for name, payload in original.parts.items():
        if name not in allowed_parts and updated.parts.get(name) != payload:
            _fail("XLSX_UNSELECTED_PACKAGE_PART_CHANGED", part=name)
    for name in updated.parts:
        if name not in original.parts and name not in allowed_parts:
            _fail("XLSX_PACKAGE_PART_ADDED_UNEXPECTEDLY", part=name)
    for name in changed_parts:
        if not name.startswith("xl/charts/") or not name.endswith(".xml"):
            continue
        if name not in original.parts or name not in updated.parts:
            _fail("XLSX_CHART_PART_MISSING", part=name)
        before = _without_chart_caches(_parse_xml(original.parts[name], name))
        after = _without_chart_caches(_parse_xml(updated.parts[name], name))
        if _canonical(before) != _canonical(after):
            _fail("XLSX_UNSELECTED_CHART_STRUCTURE_CHANGED", part=name)

    for sheet_id, original_root in original.sheet_roots.items():
        updated_root = updated.sheet_roots[sheet_id]
        original_data = original_root.find(_q("sheetData"))
        updated_data = updated_root.find(_q("sheetData"))
        if original_data is None or updated_data is None:
            _fail("XLSX_SHEET_DATA_MISSING")
        for child in original_root:
            if child is original_data:
                continue
            other = updated_root.find(child.tag)
            if other is None or _canonical(child) != _canonical(other):
                _fail("XLSX_UNSELECTED_SHEET_STRUCTURE_CHANGED", sheet_id=sheet_id)
        original_cells = {cell.get("r"): cell for cell in original_data.iter(_q("c"))}
        updated_cells = {cell.get("r"): cell for cell in updated_data.iter(_q("c"))}
        mutable = {address for sid, address in changed_cells | recalculated if sid == sheet_id}
        for address in set(original_cells) | set(updated_cells):
            if address in mutable:
                continue
            before, after = original_cells.get(address), updated_cells.get(address)
            if before is None or after is None or _canonical(before) != _canonical(after):
                _fail("XLSX_UNSELECTED_CELL_CHANGED", sheet_id=sheet_id, address=address)
        for key in recalculated:
            if key[0] != sheet_id or key in changed_cells:
                continue
            before, after = original_cells.get(key[1]), updated_cells.get(key[1])
            if before is None or after is None:
                _fail("XLSX_RECALCULATED_FORMULA_MISSING", sheet_id=sheet_id, address=key[1])
            before_formula, after_formula = before.find(_q("f")), after.find(_q("f"))
            if (before_formula is None or after_formula is None
                    or _canonical(before_formula) != _canonical(after_formula)):
                _fail("XLSX_RECALCULATED_FORMULA_CHANGED", sheet_id=sheet_id, address=key[1])

    original_shared = original.shared_items
    updated_shared = updated.shared_items
    if len(updated_shared) < len(original_shared):
        _fail("XLSX_SHARED_STRING_REMOVED")
    for index, item in enumerate(original_shared):
        if _canonical(item) != _canonical(updated_shared[index]):
            _fail("XLSX_SHARED_STRING_UNSELECTED_CHANGED", index=index)


def _normalize_changes(workbook: _Workbook, content_hash: str, changes: list[dict]):
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        _fail("XLSX_CHANGES_INVALID", maximum=MAX_CHANGES)
    normalized = []
    changed_keys = set()
    formulas = {}
    values = {}
    for index, item in enumerate(changes):
        if not isinstance(item, dict) or set(item) != {"object_id", "before", "after"}:
            _fail("XLSX_CHANGE_INVALID", index=index)
        object_id, before_input, after_input = item["object_id"], item["before"], item["after"]
        if not isinstance(object_id, str) or len(object_id) > 200:
            _fail("XLSX_OBJECT_ID_INVALID", index=index)
        match = re.fullmatch(r"xlsx:([0-9a-f]{64}):([1-9][0-9]*):([A-Z]{1,3}[1-9][0-9]*)", object_id)
        if match is None:
            _fail("XLSX_OBJECT_ID_INVALID", index=index)
        if match.group(1) != content_hash:
            _fail("XLSX_OBJECT_BASE_MISMATCH", index=index)
        sheet_id, address = int(match.group(2)), match.group(3)
        if sheet_id not in workbook.sheets:
            _fail("XLSX_OBJECT_NOT_FOUND", index=index)
        position = _cell_position(address)
        address = f"{_column_letters(position[1])}{position[0]}"
        key = (sheet_id, address)
        if key in changed_keys:
            _fail("XLSX_OBJECT_DUPLICATED", index=index)
        changed_keys.add(key)
        cell = workbook.cells.get(key)
        current = _cell_text(cell, workbook.shared_items) if cell is not None else {"kind": "blank"}
        if not isinstance(before_input, dict) or set(before_input) != {"kind", "value"}:
            _fail("XLSX_BEFORE_VALUE_INVALID", index=index)
        # Formula cache values are deliberately excluded from the anchor; only
        # the exact original formula or literal cell value can authorize a edit.
        before_value = _typed_input(before_input, field=f"changes[{index}].before")
        actual = _Value(current["kind"], current.get("value"))
        if actual.kind == "number":
            actual = _Value("number", Decimal(str(actual.value)))
        elif actual.kind == "formula":
            actual = _Value("formula", str(actual.value).removeprefix("="))
        if before_value != actual:
            _fail("XLSX_BEFORE_VALUE_MISMATCH", index=index, sheet_id=sheet_id, address=address)
        after_value = _typed_input(after_input, field=f"changes[{index}].after")
        if after_value == before_value:
            _fail("XLSX_NO_CHANGES", index=index)
        if after_value.kind == "date":
            style_ids = _date_style_ids(workbook)
            if cell is None or _style_index(cell) not in style_ids:
                _fail("XLSX_DATE_STYLE_REQUIRED", index=index, sheet_id=sheet_id, address=address)
            after_value = _Value("number", _date_serial(after_value.value, workbook.date_1904))
        _guard_edit(workbook, key, after_value)
        if after_value.kind == "formula":
            formulas[key] = after_value.value
        else:
            formulas[key] = None
            values[key] = after_value
        normalized.append({"object_id": object_id, "sheet_id": sheet_id, "sheet": workbook.sheets[sheet_id]["name"],
                           "address": address, "before": _typed_json(before_value),
                           "after": _typed_json(after_value)})
    return normalized, changed_keys, formulas, values


def revise_spreadsheet(content: bytes, changes: list[dict]) -> dict:
    """Apply typed, exact-cell changes and recalculate supported dependents."""
    workbook = _load_workbook(content)
    original_hash = _sha256(content)
    normalized, changed_cells, formula_overrides, value_overrides = _normalize_changes(
        workbook, original_hash, changes,
    )
    formulas, calculated, affected = _calculation_results(
        workbook, formula_overrides, value_overrides, changed_cells,
    )

    changed_parts = set()
    original_shared_count = len(workbook.shared_items)
    original_shared_uses = _count_shared_string_uses(workbook)
    for change in normalized:
        key = (change["sheet_id"], change["address"])
        sheet = workbook.sheets[key[0]]
        cell = workbook.cells.get(key)
        after_value = value_overrides.get(key)
        if key in formula_overrides and formula_overrides[key] is not None:
            after_value = _Value("formula", formula_overrides[key])
        elif after_value is None:
            after_value = _Value("blank")
        shared_index = None
        if after_value.kind == "text" and cell is not None and cell.get("t") == "s":
            shared_index = _shared_item_for_cell(workbook, cell, change["before"]["value"], after_value.value)
        _replace_or_create_cell(workbook, key, after_value, shared_item_index=shared_index)
        changed_parts.add(sheet["part"])
        if after_value.kind == "formula":
            change["after"] = _typed_json(after_value)

    # Formula values are calculated from the source values and the proposed
    # changes, then written into OOXML caches. These values are independently
    # checked by reading the rebuilt package below.
    for key, result in calculated.items():
        cell = workbook.cells.get(key)
        if cell is None:
            _fail("XLSX_RECALCULATED_FORMULA_MISSING", sheet_id=key[0], address=key[1])
        formula = cell.find(_q("f"))
        if formula is None:
            continue
        _write_formula_cache(cell, result)
        changed_parts.add(workbook.sheets[key[0]]["part"])

    charts_updated = _refresh_chart_caches(
        workbook, formulas, _formula_models(workbook, formula_overrides)[1],
        value_overrides, calculated, changed_cells, affected,
    )
    changed_parts.update(charts_updated)

    current_shared_uses = _count_shared_string_uses(workbook)
    if (workbook.shared_root is not None
            and (len(workbook.shared_items) > original_shared_count
                 or current_shared_uses != original_shared_uses)):
        workbook.shared_root.set("count", str(current_shared_uses))
        workbook.shared_root.set("uniqueCount", str(len(workbook.shared_items)))
        workbook.parts["xl/sharedStrings.xml"] = _serialize_xml(workbook.shared_root)
        changed_parts.add("xl/sharedStrings.xml")
    elif workbook.shared_root is None and any(cell.get("t") == "s" for cell in workbook.cells.values()):
        _fail("XLSX_SHARED_STRING_TABLE_MISSING")

    if affected or any(value is not None for value in formula_overrides.values()):
        _remove_calc_chain(workbook)
        _set_full_recalculation(workbook)
        changed_parts.update({"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"})

    for sheet_id, root in workbook.sheet_roots.items():
        part = workbook.sheets[sheet_id]["part"]
        if part in changed_parts:
            workbook.parts[part] = _serialize_xml(root)
    revised = _write_package(workbook, changed_parts)
    reopened = _load_workbook(revised)
    if reopened.shared_root is not None:
        count_attr = reopened.shared_root.get("count")
        unique_attr = reopened.shared_root.get("uniqueCount")
        if count_attr is not None and int(count_attr) != _count_shared_string_uses(reopened):
            _fail("XLSX_SHARED_STRING_COUNT_READBACK_FAILED")
        if unique_attr is not None and int(unique_attr) != len(reopened.shared_items):
            _fail("XLSX_SHARED_STRING_COUNT_READBACK_FAILED")

    for change in normalized:
        key = (change["sheet_id"], change["address"])
        saved_cell = reopened.cells.get(key)
        expected = formula_overrides.get(key)
        if expected is not None:
            actual = reopened.formulas.get(key)
            if actual != expected:
                _fail("XLSX_CHANGE_READBACK_FAILED", sheet_id=key[0], address=key[1])
            change["after"]["calculated"] = _typed_json(calculated[key], verified=True)
            saved_value = _cell_text(saved_cell, reopened.shared_items).get("cached") if saved_cell is not None else None
            expected_value = calculated[key]
            if saved_value is None or saved_value.get("kind") != expected_value.kind:
                _fail("XLSX_FORMULA_CACHE_READBACK_FAILED", sheet_id=key[0], address=key[1])
            cached_value = saved_value.get("value")
            if expected_value.kind == "number":
                try:
                    cache_matches = Decimal(str(cached_value)) == expected_value.value
                except InvalidOperation:
                    cache_matches = False
            elif expected_value.kind == "boolean":
                cache_matches = cached_value is expected_value.value
            else:
                cache_matches = cached_value == expected_value.value
            if not cache_matches:
                _fail("XLSX_FORMULA_CACHE_READBACK_FAILED", sheet_id=key[0], address=key[1])
        else:
            saved = _cell_text(saved_cell, reopened.shared_items) if saved_cell is not None else {"kind": "blank"}
            expected_value = value_overrides.get(key, _Value("blank"))
            actual_value = _Value(saved["kind"], saved.get("value"))
            if actual_value.kind == "number":
                actual_value = _Value("number", Decimal(str(actual_value.value)))
            if actual_value != expected_value:
                _fail("XLSX_CHANGE_READBACK_FAILED", sheet_id=key[0], address=key[1])
            if actual_value.kind == "number":
                change["after"] = _typed_json(actual_value, verified=True)

    _verify_package_preservation(
        _load_workbook(content), reopened, changed_cells, set(calculated), changed_parts,
    )
    warnings = list(dict.fromkeys(workbook.warnings))
    if any(name.startswith("xl/pivotCache/") or name.startswith("xl/pivotTables/") for name in workbook.parts):
        warnings.append("透视表和透视缓存保留；本次没有验证其刷新结果。")
    return {
        "content": revised,
        "changes": normalized,
        "recalculated_cells": [
            {"sheet_id": key[0], "sheet": workbook.sheets[key[0]]["name"], "address": key[1],
             "formula": "=" + formulas[key], "value": _typed_json(value, verified=True)}
            for key, value in sorted(calculated.items())
        ],
        "charts_updated": charts_updated,
        "calculation_engine": CALCULATION_ENGINE,
        "calculation_revision": CALCULATION_REVISION,
        "unchanged_verified": True,
        "warnings": warnings,
    }



