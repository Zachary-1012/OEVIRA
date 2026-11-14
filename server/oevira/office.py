"""Bounded PPTX object inspection and exact-scope revision.

Existing OOXML text runs, selected images, and supported chart data are revised.
The module never launches an Office application, evaluates macros/formulas,
follows external relationships, or opens embedded packages except the selected
chart workbook. All unrelated ZIP members retain their original payload bytes.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import posixpath
import re
import stat
import threading
import zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 24 * 1024 * 1024
MAX_ZIP_ENTRIES = 5_000
MAX_SLIDES = 1_000
MAX_OBJECTS_PER_SLIDE = 10_000
MAX_CHANGE_TEXT_CHARS = 32_000
MAX_REPLACEMENT_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REPLACEMENT_IMAGE_PIXELS = 100_000_000

_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_R_STRICT = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_NS_PACKAGE_R = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_XML = "http://www.w3.org/XML/1998/namespace"
_XML_FORBIDDEN = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_NAMESPACE_LOCK = threading.RLock()


class _PptxError(ValueError):
    pass


def _fail(code: str) -> None:
    raise _PptxError(code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_xml(data: bytes, part: str) -> tuple[ET.Element, list[tuple[str, str]]]:
    if len(data) > MAX_XML_BYTES or b"\x00" in data or _XML_FORBIDDEN.search(data):
        _fail("PPTX_XML_UNSAFE_OR_TOO_LARGE")
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        iterator = ET.iterparse(io.BytesIO(data), events=("start-ns", "start"), parser=parser)
        namespaces: list[tuple[str, str]] = []
        for event, value in iterator:
            if event == "start-ns":
                pair = (value[0] or "", value[1])
                if pair not in namespaces:
                    namespaces.append(pair)
        return iterator.root, namespaces
    except (ET.ParseError, RecursionError, UnicodeError, LookupError, ValueError) as exc:
        raise _PptxError(f"PPTX_XML_INVALID:{part}") from exc


def _xml_part(parts: dict[str, bytes], name: str) -> tuple[ET.Element, list[tuple[str, str]]]:
    data = parts.get(name)
    if data is None:
        _fail(f"PPTX_PART_MISSING:{name}")
    return _parse_xml(data, name)


def _validate_archive(content: bytes) -> tuple[list[zipfile.ZipInfo], dict[str, bytes], bytes, list[str]]:
    if not isinstance(content, bytes):
        _fail("PPTX_CONTENT_MUST_BE_BYTES")
    if len(content) > MAX_INPUT_BYTES:
        _fail("PPTX_INPUT_TOO_LARGE")
    try:
        source = zipfile.ZipFile(io.BytesIO(content), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise _PptxError("PPTX_PACKAGE_INVALID") from exc

    infos: list[zipfile.ZipInfo] = []
    parts: dict[str, bytes] = {}
    warnings: list[str] = []
    seen_names: set[str] = set()
    seen_casefold: set[str] = set()
    declared_total = 0
    try:
        listed = source.infolist()
        if len(listed) > MAX_ZIP_ENTRIES:
            _fail("PPTX_ZIP_ENTRY_LIMIT")
        for info in listed:
            name = info.filename
            parts_of_name = name.split("/")
            if (not name or name.startswith("/") or "\\" in name
                    or re.match(r"^[A-Za-z]:", name)
                    or any(part == ".." for part in parts_of_name)):
                _fail("PPTX_MEMBER_PATH_UNSAFE")
            folded = name.casefold()
            if name in seen_names or folded in seen_casefold:
                _fail("PPTX_MEMBER_NAME_AMBIGUOUS")
            seen_names.add(name)
            seen_casefold.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                _fail("PPTX_MEMBER_SYMLINK_FORBIDDEN")
            if info.flag_bits & 0x1:
                _fail("PPTX_ENCRYPTED_MEMBER_UNSUPPORTED")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _fail("PPTX_COMPRESSION_METHOD_UNSUPPORTED")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                _fail("PPTX_MEMBER_SIZE_LIMIT")
            declared_total += info.file_size
            if declared_total > MAX_PACKAGE_BYTES:
                _fail("PPTX_EXPANDED_SIZE_LIMIT")

        actual_total = 0
        for info in listed:
            try:
                with source.open(info, "r") as member:
                    data = bytearray()
                    while True:
                        chunk = member.read(min(1024 * 1024, info.file_size + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_MEMBER_BYTES:
                            _fail("PPTX_MEMBER_SIZE_LIMIT")
            except _PptxError:
                raise
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError) as exc:
                raise _PptxError("PPTX_MEMBER_CORRUPT") from exc
            if len(data) != info.file_size:
                _fail("PPTX_MEMBER_SIZE_MISMATCH")
            actual_total += len(data)
            if actual_total > MAX_PACKAGE_BYTES:
                _fail("PPTX_EXPANDED_SIZE_LIMIT")
            infos.append(info)
            parts[info.filename] = bytes(data)

        if "ppt/embeddings/" in "\n".join(parts):
            warnings.append("文件含有嵌入内容；未打开或递归解包，原成员会保留。")
        if any("vbaproject" in name.casefold() or "/activex/" in name.casefold()
               for name in parts):
            warnings.append("文件含有宏或 ActiveX 内容；未执行，原成员会保留。")
        if any(name.startswith("_xmlsignatures/") for name in parts):
            warnings.append("文件含有数字签名；修改会使原签名失效，因此拒绝保存修改。")
        return infos, parts, source.comment, warnings
    except Exception:
        source.close()
        raise
    finally:
        if source.fp is not None:
            source.close()


def _relationship_part(source_part: str) -> str:
    parent, basename = posixpath.split(source_part)
    return posixpath.join(parent, "_rels", basename + ".rels")


def _resolve_internal_target(source_part: str, target: str) -> str | None:
    if not target or "\\" in target or "\x00" in target:
        return None
    from urllib.parse import urlsplit
    uri = urlsplit(target)
    if uri.scheme or uri.netloc or uri.query or uri.fragment:
        return None
    path = uri.path
    if path.startswith("/"):
        resolved = posixpath.normpath(path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), path))
    if resolved in {".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        return None
    return resolved


def _relationships(parts: dict[str, bytes], source_part: str,
                   warnings: list[str]) -> dict[str, dict | None]:
    rel_part = _relationship_part(source_part)
    if rel_part not in parts:
        _fail(f"PPTX_RELATIONSHIP_PART_MISSING:{rel_part}")
    root, _ = _xml_part(parts, rel_part)
    if root.tag != f"{{{_NS_PACKAGE_R}}}Relationships":
        _fail("PPTX_RELATIONSHIP_XML_INVALID")
    result: dict[str, dict | None] = {}
    for node in root:
        if node.tag != f"{{{_NS_PACKAGE_R}}}Relationship":
            continue
        identity = node.attrib.get("Id")
        target = node.attrib.get("Target")
        mode = node.attrib.get("TargetMode")
        if not identity or not target or identity in result or mode not in {None, "Internal", "External"}:
            _fail("PPTX_RELATIONSHIP_INVALID")
        if mode == "External":
            result[identity] = None
            warning = "存在外部链接；不会访问外部目标。"
            if warning not in warnings:
                warnings.append(warning)
            continue
        resolved = _resolve_internal_target(source_part, target)
        if resolved is None:
            _fail("PPTX_RELATIONSHIP_TARGET_UNSAFE")
        result[identity] = {"target": resolved, "type": node.attrib.get("Type", "")}
    return result


def _slide_manifest(parts: dict[str, bytes], warnings: list[str]) -> list[dict]:
    presentation, _ = _xml_part(parts, "ppt/presentation.xml")
    relations = _relationships(parts, "ppt/presentation.xml", warnings)
    p_ns = presentation.tag[1:].split("}", 1)[0] if presentation.tag.startswith("{") else ""
    if p_ns not in {_NS_P, "http://purl.oclc.org/ooxml/presentationml/main"}:
        _fail("PPTX_PRESENTATION_NAMESPACE_UNSUPPORTED")
    r_ns = _NS_R if p_ns == _NS_P else _NS_R_STRICT
    slide_ids = presentation.find(f"{{{p_ns}}}sldIdLst")
    if slide_ids is None:
        _fail("PPTX_SLIDE_LIST_MISSING")
    nodes = slide_ids.findall(f"{{{p_ns}}}sldId")
    if len(nodes) > MAX_SLIDES:
        _fail("PPTX_SLIDE_LIMIT")
    manifest = []
    seen_parts: set[str] = set()
    for number, node in enumerate(nodes, 1):
        relationship_id = node.attrib.get(f"{{{r_ns}}}id")
        relation = relations.get(relationship_id) if relationship_id else None
        part = relation.get("target") if relation else None
        if relation and not relation.get("type", "").endswith("/slide"):
            part = None
        if part is not None:
            if not part.startswith("ppt/slides/") or part not in parts or part in seen_parts:
                _fail("PPTX_SLIDE_PART_INVALID")
            seen_parts.add(part)
        else:
            warnings.append(f"第 {number} 页的幻灯片部件不可用或关系外部化。")
        manifest.append({"number": number, "part": part, "p_ns": p_ns,
                         "a_ns": _NS_A if p_ns == _NS_P else "http://purl.oclc.org/ooxml/drawingml/main"})
    return manifest


def _canonical(element: ET.Element) -> str:
    xml = ET.tostring(element, encoding="unicode")
    try:
        return ET.canonicalize(xml_data=xml, strip_text=False)
    except (AttributeError, TypeError):
        return xml


def _text_independent_signature(element: ET.Element, a_ns: str) -> str:
    clone = copy.deepcopy(element)
    for node in clone.iter(f"{{{a_ns}}}t"):
        node.text = None
        node.attrib.pop(f"{{{_NS_XML}}}space", None)
    return _canonical(clone)


def _text_and_editability(
    element: ET.Element, a_ns: str,
) -> tuple[str, bool, str | None, list[ET.Element], bool]:
    tx_body = next((child for child in element
                    if child.tag.endswith("}txBody") or child.tag == "txBody"), None)
    if tx_body is None and element.tag.endswith("}tc"):
        tx_body = element
    if tx_body is None:
        return "", False, "对象没有可编辑文字内容。", [], False

    paragraphs = tx_body.findall(f"{{{a_ns}}}p")
    if not paragraphs:
        return "", False, "对象没有可编辑文字段落。", [], False
    paragraph_texts = []
    for paragraph in paragraphs:
        parts = []
        for child in paragraph.iter():
            if child.tag == f"{{{a_ns}}}t":
                parts.append(child.text or "")
            elif child.tag == f"{{{a_ns}}}br":
                parts.append("\n")
            elif child.tag == f"{{{a_ns}}}tab":
                parts.append("\t")
        paragraph_texts.append("".join(parts))
    text = "\n".join(paragraph_texts)
    if not text.strip():
        return text, False, "对象当前没有非空文字。", [], False
    if len(paragraphs) != 1:
        return text, False, "对象含多个段落；当前不会重建段落结构。", [], False
    paragraph = paragraphs[0]
    runs = paragraph.findall(f"{{{a_ns}}}r")
    if not runs:
        return text, False, "段落不是可保留样式的普通文字运行。", [], False
    permitted_paragraph_children = {
        f"{{{a_ns}}}pPr", f"{{{a_ns}}}r", f"{{{a_ns}}}endParaRPr",
    }
    if any(child.tag not in permitted_paragraph_children for child in paragraph):
        return text, False, "段落含有换行、字段或其他复杂标记。", []

    run_styles = []
    all_text_nodes: list[ET.Element] = []
    for run in runs:
        allowed_run_children = {f"{{{a_ns}}}rPr", f"{{{a_ns}}}t"}
        if any(child.tag not in allowed_run_children for child in run):
            return text, False, "文字运行含有复杂内容，无法保证格式不变。", [], False
        run_text_nodes = run.findall(f"{{{a_ns}}}t")
        if not run_text_nodes:
            return text, False, "文字运行没有可替换的文本节点。", [], False
        run_properties = run.find(f"{{{a_ns}}}rPr")
        style = _canonical(run_properties) if run_properties is not None else None
        run_styles.append(style)
        all_text_nodes.extend(run_text_nodes)
    same_run_style = len(set(run_styles)) <= 1
    reason = None if same_run_style else "混合字形对象仅支持按唯一原文锚点局部替换。"
    return text, True, reason, all_text_nodes, same_run_style


def _properties(element: ET.Element) -> tuple[str, str | None]:
    name = ""
    properties = None
    for node in element.iter():
        if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
            properties = node
            name = node.attrib.get("name", "")
            break
    identity = properties.attrib.get("id") if properties is not None else None
    return name, identity


def _object_id(slide_number: int, object_type: str, native_id: str | None,
               ordinal: int) -> str:
    suffix = native_id if native_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", native_id) else f"unidentified-{ordinal}"
    return f"slide-{slide_number}-{object_type}-{suffix}"


def _group_ancestors(element: ET.Element, parents: dict[ET.Element, ET.Element],
                     ordinals: dict[ET.Element, int], slide_number: int,
                     p_ns: str, shape_tree: ET.Element) -> list[str]:
    ancestors = []
    parent = parents.get(element)
    while parent is not None and parent is not shape_tree:
        if parent.tag == f"{{{p_ns}}}grpSp":
            _name, native_id = _properties(parent)
            ancestors.append(_object_id(slide_number, "group", native_id,
                                        ordinals.get(parent, 0)))
        parent = parents.get(parent)
    ancestors.reverse()
    return ancestors


def _picture_metadata(element: ET.Element, parts: dict[str, bytes] | None,
                      slide_part: str | None, slide_number: int, p_ns: str,
                      a_ns: str, group_ancestors: list[str]) -> tuple[dict, dict]:
    metadata = {"group_ancestors": group_ancestors}
    binding_data = {"group_ancestors": group_ancestors}
    if not parts or not slide_part:
        return metadata, binding_data
    blips = element.findall(f".//{{{a_ns}}}blip")
    if len(blips) != 1:
        metadata.update({"editable": False,
                         "reason": "图片包含多个或不明确的原生资源关系，不能安全替换。"})
        return metadata, binding_data
    blip = blips[0]
    relation_id = next((value for name, value in blip.attrib.items()
                        if name.endswith("}embed") or name == "embed"), None)
    if not relation_id:
        metadata.update({"editable": False,
                         "reason": "图片没有可确认的内部资源关系；不会访问外部图片链接。"})
        return metadata, binding_data
    try:
        relations = _relationships(parts, slide_part, [])
    except ValueError:
        metadata.update({"editable": False, "reason": "图片资源关系无法安全读取。"})
        return metadata, binding_data
    relation = relations.get(relation_id)
    if not isinstance(relation, dict) or not relation.get("type", "").endswith("/image"):
        metadata.update({"editable": False,
                         "reason": "图片关系指向外部目标或不是可替换的内部图片。"})
        return metadata, binding_data
    target = relation.get("target")
    original = parts.get(target) if isinstance(target, str) else None
    if not isinstance(original, bytes):
        metadata.update({"editable": False, "reason": "原图片部件不可用。"})
        return metadata, binding_data
    name = target.rsplit("/", 1)[-1]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "gif": "image/gif", "bmp": "image/bmp", "tif": "image/tiff",
                 "tiff": "image/tiff", "emf": "image/x-emf", "wmf": "image/x-wmf",
                 "svg": "image/svg+xml"}.get(extension, "application/octet-stream")
    media = {"sha256": _sha256(original), "size_bytes": len(original), "mime_type": mime_type}
    try:
        from PIL import Image

        with Image.open(io.BytesIO(original)) as image:
            media["dimensions"] = [int(image.width), int(image.height)]
            media["decoded_format"] = image.format
    except Exception:
        # Vector and legacy Office images may be valid package assets even when
        # Pillow cannot preview them; replacement still preserves their bytes.
        media["dimensions"] = None
    crop = element.find(f".//{{{a_ns}}}srcRect")
    crop_values = dict(crop.attrib) if crop is not None else {}
    transform = element.find(f".//{{{p_ns}}}spPr/{{{a_ns}}}xfrm")
    geometry = None
    if transform is not None:
        offset = transform.find(f"{{{a_ns}}}off")
        extent = transform.find(f"{{{a_ns}}}ext")
        if offset is not None and extent is not None:
            geometry = {"x": offset.get("x"), "y": offset.get("y"),
                        "cx": extent.get("cx"), "cy": extent.get("cy")}
    metadata.update({"editable": True, "edit_modes": ["replace_image"],
                     "media": media, "crop": crop_values, "geometry": geometry})
    binding_data.update({"image_blip": blip, "image_relation_id": relation_id,
                         "image_part": target, "image_crop": crop_values,
                         "image_geometry": geometry, "image_media": media})
    return metadata, binding_data


def _picture_structure_signature(element: ET.Element, a_ns: str, *, ignore_alt_text: bool = False) -> str:
    clone = copy.deepcopy(element)
    for blip in clone.iter(f"{{{a_ns}}}blip"):
        for name in list(blip.attrib):
            if name.endswith("}embed") or name.endswith("}link"):
                blip.attrib.pop(name, None)
    if ignore_alt_text:
        for node in clone.iter():
            if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
                node.attrib.pop("descr", None)
                node.attrib.pop("title", None)
    return _canonical(clone)


def _group_structure_signature(element: ET.Element, a_ns: str) -> str:
    clone = copy.deepcopy(element)
    for node in clone.iter(f"{{{a_ns}}}t"):
        node.text = None
        node.attrib.pop(f"{{{_NS_XML}}}space", None)
    for blip in clone.iter(f"{{{a_ns}}}blip"):
        for name in list(blip.attrib):
            if name.endswith("}embed") or name.endswith("}link"):
                blip.attrib.pop(name, None)
    for chart in clone.iter(f"{{{_NS_C}}}chart"):
        for name in list(chart.attrib):
            if name.endswith("}id"):
                chart.attrib.pop(name, None)
    for node in clone.iter():
        if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
            node.attrib.pop("descr", None)
            node.attrib.pop("title", None)
    return _canonical(clone)


def _chart_frame_signature(element: ET.Element) -> str:
    clone = copy.deepcopy(element)
    for chart in clone.iter(f"{{{_NS_C}}}chart"):
        for name in list(chart.attrib):
            if name.endswith("}id"):
                chart.attrib.pop(name, None)
    return _canonical(clone)


def _chart_cache_independent_signature(chart_root: ET.Element) -> str:
    clone = copy.deepcopy(chart_root)
    cache_names = {f"{{{_NS_C}}}{name}"
                   for name in ("strCache", "numCache", "multiLvlStrCache")}
    for parent in clone.iter():
        for child in list(parent):
            if child.tag in cache_names:
                parent.remove(child)
    return _canonical(clone)


def _chart_cell_address(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})", value or "")
    if match is None:
        return None
    column = 0
    for character in match.group(1).upper():
        column = column * 26 + ord(character) - ord("A") + 1
    row = int(match.group(2))
    return (row, column) if row <= 1_048_576 and column <= 16_384 else None


def _parse_chart_range(formula: str) -> dict | None:
    match = re.fullmatch(
        r"(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_.]*))!"
        r"(\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)(?::(\$?[A-Za-z]{1,3}\$?[1-9][0-9]*))?",
        (formula or "").strip().removeprefix("="),
    )
    if match is None:
        return None
    sheet = (match.group(1).replace("''", "'") if match.group(1) is not None else match.group(2))
    start = _chart_cell_address(match.group(3))
    end = _chart_cell_address(match.group(4) or match.group(3))
    if start is None or end is None or end[0] < start[0] or end[1] < start[1]:
        return None
    row_count = end[0] - start[0] + 1
    column_count = end[1] - start[1] + 1
    if row_count != 1 and column_count != 1:
        return None
    return {"sheet": sheet, "first": start, "last": end,
            "row_count": row_count, "column_count": column_count,
            "count": row_count * column_count}


def _chart_range_addresses(reference: dict) -> list[str]:
    first_row, first_column = reference["first"]
    last_row, last_column = reference["last"]
    coordinates = ([(first_row, column) for column in range(first_column, last_column + 1)]
                   if first_row == last_row else
                   [(row, first_column) for row in range(first_row, last_row + 1)])
    result = []
    for row, column in coordinates:
        letters = ""
        current = column
        while current:
            current, remainder = divmod(current - 1, 26)
            letters = chr(65 + remainder) + letters
        result.append(f"{letters}{row}")
    return result


def _relationship_source(relationship_part: str) -> str | None:
    if "/_rels/" not in relationship_part or not relationship_part.endswith(".rels"):
        return None
    directory, filename = relationship_part.rsplit("/_rels/", 1)
    source_name = filename.removesuffix(".rels")
    return posixpath.join(directory, source_name)


def _unique_package_part(parts: dict[str, bytes], folder: str, stem: str,
                         suffix: str) -> str:
    candidate = f"{folder}/{stem}.{suffix}"
    if candidate not in parts:
        return candidate
    index = 2
    while f"{folder}/{stem}-{index}.{suffix}" in parts:
        index += 1
    return f"{folder}/{stem}-{index}.{suffix}"


def _add_slide_chart_relationship(parts: dict[str, bytes], slide_part: str,
                                  chart_part: str, chart_hash: str, p_ns: str,
                                  relationship_roots: dict[str, tuple[ET.Element, list[tuple[str, str]]]],
                                  modified_parts: set[str]) -> str:
    relationship_part = _relationship_part(slide_part)
    if relationship_part in relationship_roots:
        root, namespaces = relationship_roots[relationship_part]
    elif relationship_part in parts:
        root, namespaces = _xml_part(parts, relationship_part)
        relationship_roots[relationship_part] = (root, namespaces)
    else:
        root = ET.Element(f"{{{_NS_PACKAGE_R}}}Relationships")
        namespaces = [("", _NS_PACKAGE_R)]
        relationship_roots[relationship_part] = (root, namespaces)
    ids = {node.get("Id") for node in root}
    prefix = "rIdOeviraChart" + chart_hash[:12]
    relation_id = prefix
    suffix = 2
    while relation_id in ids:
        relation_id = f"{prefix}-{suffix}"
        suffix += 1
    rel_ns = _NS_R if p_ns == _NS_P else _NS_R_STRICT
    target = posixpath.relpath(chart_part, posixpath.dirname(slide_part))
    ET.SubElement(root, f"{{{_NS_PACKAGE_R}}}Relationship",
                  {"Id": relation_id, "Type": f"{rel_ns}/chart", "Target": target})
    modified_parts.add(relationship_part)
    return relation_id


def _relocate_chart_workbook_relationship(chart_rel_root: ET.Element, chart_part: str,
                                          workbook_part: str) -> None:
    for relation in chart_rel_root.findall(f"{{{_NS_PACKAGE_R}}}Relationship"):
        if relation.get("Type", "").endswith("/package"):
            relation.set("Target", posixpath.relpath(workbook_part, posixpath.dirname(chart_part)))
            return
    _fail("PPTX_CHART_WORKBOOK_RELATION_MISSING")


def _chart_cell_changes(chart_info: dict, chart_data: dict) -> list[dict]:
    changes: dict[str, dict] = {}

    def add(cell: dict, after: dict) -> None:
        before = cell["value"]
        if before.get("kind") == "formula":
            _fail("PPTX_CHART_SOURCE_FORMULA_UNSUPPORTED", address=cell["address"])
        same_number = False
        if before.get("kind") == after.get("kind") == "number":
            try:
                same_number = Decimal(str(before.get("value"))) == Decimal(str(after.get("value")))
            except Exception:
                same_number = False
        if before == after or same_number:
            return
        object_id = cell["object_id"]
        candidate = {"object_id": object_id, "before": before, "after": after}
        existing = changes.get(object_id)
        if existing is not None and existing != candidate:
            _fail("PPTX_CHART_DATA_SOURCE_OVERLAP", address=cell["address"])
        changes[object_id] = candidate

    for cell, after in zip(chart_info["series"][0]["category_cells"], chart_data["categories"]):
        add(cell, after)
    for series, after_series in zip(chart_info["series"], chart_data["series"]):
        if series.get("name_cell") is not None:
            add(series["name_cell"], {"kind": "text", "value": after_series["name"]})
        for cell, after in zip(series["value_cells"], after_series["values"]):
            add(cell, after)
    if not changes:
        _fail("PPTX_CHART_NO_CHANGES")
    return list(changes.values())


def _chart_reference_values(chart_info: dict, workbook_content: bytes) -> dict[str, list[dict]]:
    from .office_spreadsheet import inspect_spreadsheet

    inspection = inspect_spreadsheet(workbook_content)
    result: dict[str, list[dict]] = {}
    for series in chart_info["series"]:
        references = []
        if series["name_formula"] is not None:
            references.append((series["name_formula"], [series["name_cell"]]))
        references.extend(((series["category_formula"], series["category_cells"]),
                           (series["value_formula"], series["value_cells"])))
        for formula, old_cells in references:
            sheet_id = old_cells[0]["sheet_id"]
            sheet = next((item for item in inspection["sheets"] if item["sheet_id"] == sheet_id), None)
            if sheet is None:
                _fail("PPTX_CHART_REOPEN_WORKBOOK_FAILED")
            by_address = {cell["address"]: cell for cell in sheet["cells"]}
            values = []
            for cell in old_cells:
                current = by_address.get(cell["address"])
                values.append(current["value"] if current is not None else {"kind": "blank", "value": None})
            previous = result.get(formula)
            if previous is not None and previous != values:
                _fail("PPTX_CHART_FORMULA_REUSED_WITH_DIFFERENT_VALUES")
            result[formula] = values
    return result


def _replace_chart_cache(owner: ET.Element, cache_name: str, values: list[dict]) -> None:
    existing = owner.find(f"{{{_NS_C}}}{cache_name}")
    cache = copy.deepcopy(existing) if existing is not None else ET.Element(f"{{{_NS_C}}}{cache_name}")
    for child in list(cache):
        if child.tag in {f"{{{_NS_C}}}ptCount", f"{{{_NS_C}}}pt"}:
            cache.remove(child)
    if cache_name == "numCache" and cache.find(f"{{{_NS_C}}}formatCode") is None:
        code = ET.Element(f"{{{_NS_C}}}formatCode")
        code.text = "General"
        cache.insert(0, code)
    count = ET.Element(f"{{{_NS_C}}}ptCount", {"val": str(len(values))})
    insertion = 1 if cache_name == "numCache" else 0
    cache.insert(insertion, count)
    for index, typed in enumerate(values):
        kind, value = typed.get("kind"), typed.get("value")
        if kind == "blank" or value is None:
            continue
        if cache_name == "numCache":
            if kind != "number":
                _fail("PPTX_CHART_CACHE_TYPE_MISMATCH")
            from .office_spreadsheet import _serialize_number
            text = _serialize_number(Decimal(str(value)))
        else:
            if kind == "date":
                _fail("PPTX_CHART_CACHE_TYPE_MISMATCH")
            text = str(value)
        point = ET.Element(f"{{{_NS_C}}}pt", {"idx": str(index)})
        value_node = ET.SubElement(point, f"{{{_NS_C}}}v")
        value_node.text = text
        extension_list = cache.find(f"{{{_NS_C}}}extLst")
        if extension_list is None:
            cache.append(point)
        else:
            cache.insert(cache.index(extension_list), point)
    if existing is None:
        formula_node = owner.find(f"{{{_NS_C}}}f")
        if formula_node is None:
            _fail("PPTX_CHART_FORMULA_MISSING")
        owner.insert(list(owner).index(formula_node) + 1, cache)
    else:
        owner.remove(existing)
        formula_node = owner.find(f"{{{_NS_C}}}f")
        owner.insert(list(owner).index(formula_node) + 1, cache)


def _chart_cache_projection(chart_root: ET.Element) -> list[dict]:
    projections = {}
    for reference in chart_root.iter():
        local_name = reference.tag.rsplit("}", 1)[-1]
        if local_name not in {"strRef", "numRef"}:
            continue
        formula_node = reference.find(f"{{{_NS_C}}}f")
        cache_name = "strCache" if local_name == "strRef" else "numCache"
        cache = reference.find(f"{{{_NS_C}}}{cache_name}")
        if formula_node is None or not formula_node.text or cache is None:
            continue
        count_node = cache.find(f"{{{_NS_C}}}ptCount")
        if count_node is None:
            _fail("PPTX_CHART_CACHE_POINT_COUNT_MISSING")
        try:
            count = int(count_node.get("val", ""))
        except (TypeError, ValueError):
            _fail("PPTX_CHART_CACHE_POINT_COUNT_INVALID")
        if count < 0 or count > 5_000:
            _fail("PPTX_CHART_CACHE_POINT_COUNT_INVALID")
        points = []
        for point in cache.findall(f"{{{_NS_C}}}pt"):
            index = point.get("idx")
            value_node = point.find(f"{{{_NS_C}}}v")
            try:
                parsed_index = int(index or "")
            except ValueError:
                _fail("PPTX_CHART_CACHE_POINT_INDEX_INVALID")
            if parsed_index < 0 or parsed_index >= count or value_node is None:
                _fail("PPTX_CHART_CACHE_POINT_INVALID")
            points.append([parsed_index, value_node.text or ""])
        projection = {"formula": formula_node.text.strip(), "kind": local_name,
                      "count": count, "points": sorted(points)}
        key = (projection["kind"], projection["formula"])
        existing = projections.get(key)
        if existing is not None and existing != projection:
            _fail("PPTX_CHART_CACHE_REFERENCE_CONFLICT")
        projections[key] = projection
    return [projections[key] for key in sorted(projections)]


def _expected_chart_cache_projection(chart_root: ET.Element,
                                     values_by_formula: dict[str, list[dict]]) -> list[dict]:
    from .office_spreadsheet import _serialize_number

    projections = {}
    for reference in chart_root.iter():
        local_name = reference.tag.rsplit("}", 1)[-1]
        if local_name not in {"strRef", "numRef"}:
            continue
        formula_node = reference.find(f"{{{_NS_C}}}f")
        if formula_node is None or not formula_node.text:
            continue
        formula = formula_node.text.strip()
        values = values_by_formula.get(formula)
        if values is None:
            continue
        points = []
        for index, typed in enumerate(values):
            kind, value = typed.get("kind"), typed.get("value")
            if kind == "blank" or value is None:
                continue
            if local_name == "numRef":
                if kind != "number":
                    _fail("PPTX_CHART_CACHE_TYPE_MISMATCH")
                text = _serialize_number(Decimal(str(value)))
            else:
                if kind != "text":
                    _fail("PPTX_CHART_CACHE_TYPE_MISMATCH")
                text = str(value)
            points.append([index, text])
        projection = {"formula": formula, "kind": local_name,
                      "count": len(values), "points": points}
        key = (local_name, formula)
        existing = projections.get(key)
        if existing is not None and existing != projection:
            _fail("PPTX_CHART_CACHE_REFERENCE_CONFLICT")
        projections[key] = projection
    return [projections[key] for key in sorted(projections)]


def _refresh_chart_caches(chart_root: ET.Element, values_by_formula: dict[str, list[dict]]) -> None:
    for owner in chart_root.iter():
        local_name = owner.tag.rsplit("}", 1)[-1]
        if local_name not in {"strRef", "numRef"}:
            continue
        formula_node = owner.find(f"{{{_NS_C}}}f")
        if formula_node is None or not formula_node.text:
            continue
        values = values_by_formula.get(formula_node.text.strip())
        if values is None:
            continue
        _replace_chart_cache(owner, "strCache" if local_name == "strRef" else "numCache", values)


def _chart_users(parts: dict[str, bytes], chart_part: str, workbook_part: str) -> tuple[int, int]:
    chart_usage = 0
    for slide_part in (name for name in parts if name.startswith("ppt/slides/") and name.endswith(".xml")):
        relationship_part = _relationship_part(slide_part)
        if relationship_part not in parts:
            continue
        relations = _relationships(parts, slide_part, [])
        root, _ = _xml_part(parts, slide_part)
        for chart_node in root.iter(f"{{{_NS_C}}}chart"):
            relation_id = next((value for name, value in chart_node.attrib.items()
                                if name.endswith("}id")), None)
            relation = relations.get(relation_id) if relation_id else None
            if isinstance(relation, dict) and relation.get("target") == chart_part:
                chart_usage += 1
    workbook_usage = 0
    for rel_part in (name for name in parts if name.startswith("ppt/charts/_rels/") and name.endswith(".rels")):
        source_part = _relationship_source(rel_part)
        if source_part is None:
            continue
        relations = _relationships(parts, source_part, [])
        workbook_usage += sum(1 for relation in relations.values()
                              if isinstance(relation, dict)
                              and relation.get("type", "").endswith("/package")
                              and relation.get("target") == workbook_part)
    return chart_usage, workbook_usage


def _ensure_chart_content_type(parts: dict[str, bytes], part_name: str,
                               content_type: str, modified_parts: set[str],
                               content_types_state: dict) -> None:
    state = content_types_state.get("state")
    if state is None:
        root, namespaces = _xml_part(parts, "[Content_Types].xml")
        state = {"root": root, "namespaces": namespaces}
        content_types_state["state"] = state
    types = state["root"]
    override_name = "/" + part_name
    existing = next((item for item in types.findall(f"{{{_NS_CT}}}Override")
                     if item.get("PartName") == override_name), None)
    if existing is not None:
        if existing.get("ContentType") != content_type:
            _fail("PPTX_CONTENT_TYPE_CONFLICT", part=part_name)
        return
    ET.SubElement(types, f"{{{_NS_CT}}}Override",
                  {"PartName": override_name, "ContentType": content_type})
    modified_parts.add("[Content_Types].xml")


def _apply_chart_data(parts: dict[str, bytes], slide_part: str, p_ns: str,
                      chart_info: dict, value: dict,
                      modified_parts: set[str], added_parts: set[str],
                      relationship_roots: dict[str, tuple[ET.Element, list[tuple[str, str]]]],
                      content_types_state: dict) -> dict:
    from .office_spreadsheet import inspect_spreadsheet, revise_spreadsheet

    chart_data = _normalize_chart_data(value, chart_info)
    edits = _chart_cell_changes(chart_info, chart_data)
    try:
        revised_workbook = revise_spreadsheet(chart_info["workbook_bytes"], edits)
    except ValueError as exc:
        _fail("PPTX_CHART_WORKBOOK_REVISION_FAILED", reason=getattr(exc, "code", str(exc)))
    new_workbook = revised_workbook["content"]
    values_by_formula = _chart_reference_values(chart_info, new_workbook)
    chart_root = chart_info["chart_root"]
    _refresh_chart_caches(chart_root, values_by_formula)
    expected_cache_projection = _expected_chart_cache_projection(chart_root, values_by_formula)

    chart_part = chart_info["chart_part"]
    workbook_part = chart_info["workbook_part"]
    slide_chart_node = chart_info["chart_node"]
    chart_usage, workbook_usage = _chart_users(parts, chart_part, workbook_part)
    clone_context = chart_usage > 1 or workbook_usage > 1
    chart_hash = hashlib.sha256(json.dumps(
        [slide_part, chart_info["chart_relationship_id"], chart_data],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    new_workbook_part = workbook_part
    new_chart_part = chart_part
    chart_rel_part = chart_info["chart_rel_part"]

    if clone_context:
        workbook_hash = hashlib.sha256(new_workbook).hexdigest()
        new_workbook_part = _unique_package_part(parts, "ppt/embeddings",
                                                 f"oevira-{workbook_hash[:20]}", "xlsx")
        parts[new_workbook_part] = new_workbook
        added_parts.add(new_workbook_part)
        _ensure_chart_content_type(parts, new_workbook_part,
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   modified_parts, content_types_state)
        new_chart_part = _unique_package_part(parts, "ppt/charts", f"oevira-{chart_hash[:20]}", "xml")
        parts[new_chart_part] = _serialize(chart_root, chart_info["chart_namespaces"])
        added_parts.add(new_chart_part)
        _ensure_chart_content_type(parts, new_chart_part,
                                   "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
                                   modified_parts, content_types_state)
        source_chart_rel = parts.get(chart_rel_part)
        if source_chart_rel is None:
            _fail("PPTX_CHART_RELATIONSHIP_PART_UNAVAILABLE")
        new_chart_rel_part = _unique_package_part(
            parts, "ppt/charts/_rels", f"oevira-{chart_hash[:20]}.xml", "rels",
        )
        new_chart_rel_root, new_chart_rel_namespaces = _xml_part(parts, chart_rel_part)
        _relocate_chart_workbook_relationship(new_chart_rel_root, new_chart_part, new_workbook_part)
        parts[new_chart_rel_part] = _serialize(new_chart_rel_root, new_chart_rel_namespaces)
        added_parts.add(new_chart_rel_part)
        new_relationship_id = _add_slide_chart_relationship(
            parts, slide_part, new_chart_part, chart_hash, p_ns,
            relationship_roots, modified_parts,
        )
        relationship_ns = _NS_R if p_ns == _NS_P else _NS_R_STRICT
        slide_chart_node.set(f"{{{relationship_ns}}}id", new_relationship_id)
        modified_parts.add(slide_part)
        chart_part = new_chart_part
        chart_rel_part = new_chart_rel_part
    else:
        parts[workbook_part] = new_workbook
        modified_parts.add(workbook_part)
        parts[chart_part] = _serialize(chart_root, chart_info["chart_namespaces"])
        modified_parts.add(chart_part)

    return {"before": chart_info["chart_data"], "after": chart_data,
            "embedded_workbook_sha256_before": chart_info["workbook_sha256"],
            "embedded_workbook_sha256_after": hashlib.sha256(new_workbook).hexdigest(),
            "embedded_workbook_calculation_engine": revised_workbook["calculation_engine"],
            "embedded_workbook_calculation_revision": revised_workbook["calculation_revision"],
            "chart_caches_updated": sorted(values_by_formula),
            "copy_on_write": clone_context,
            "chart_part": chart_part,
            "chart_rel_part": chart_rel_part,
            "workbook_part": new_workbook_part,
            "expected_cache_projection": expected_cache_projection,
            "unchanged_verified": bool(revised_workbook["unchanged_verified"])}


def _normalize_chart_data(value: dict, chart_info: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"categories", "series"}:
        _fail("PPTX_CHART_DATA_INVALID")
    expected_categories = len(chart_info["series"][0]["category_cells"])
    if (not isinstance(value["categories"], list) or not expected_categories
            or len(value["categories"]) != expected_categories or expected_categories > 5_000):
        _fail("PPTX_CHART_CATEGORY_COUNT_MISMATCH", expected=expected_categories)
    if not isinstance(value["series"], list) or len(value["series"]) != len(chart_info["series"]):
        _fail("PPTX_CHART_SERIES_COUNT_MISMATCH", expected=len(chart_info["series"]))

    def typed(value, allowed: set[str]) -> dict:
        if (not isinstance(value, dict) or set(value) != {"kind", "value"}
                or not isinstance(value["kind"], str) or value["kind"] not in allowed):
            _fail("PPTX_CHART_VALUE_INVALID")
        kind, raw = value["kind"], value["value"]
        if kind == "text":
            if (not isinstance(raw, str) or len(raw) > MAX_CHANGE_TEXT_CHARS
                    or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", raw)):
                _fail("PPTX_CHART_TEXT_INVALID")
            return {"kind": kind, "value": raw}
        if kind == "number":
            from decimal import Decimal, InvalidOperation

            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                _fail("PPTX_CHART_NUMBER_INVALID")
            try:
                number = Decimal(str(raw))
            except InvalidOperation as exc:
                raise _PptxError("PPTX_CHART_NUMBER_INVALID") from exc
            if not number.is_finite() or len(number.as_tuple().digits) > 15 or abs(number.adjusted()) > 300:
                _fail("PPTX_CHART_NUMBER_INVALID")
            return {"kind": kind, "value": format(number.normalize(), "f")}
        if kind == "date" and isinstance(raw, str):
            return {"kind": kind, "value": raw}
        if kind == "blank" and raw is None:
            return {"kind": kind, "value": None}
        _fail("PPTX_CHART_VALUE_INVALID")

    category_kind = chart_info["series"][0]["category_ref_type"]
    categories = [typed(item, {"text", "blank"} if category_kind == "text" else {"number", "blank"})
                  for item in value["categories"]]
    series = []
    for index, (item, existing) in enumerate(zip(value["series"], chart_info["series"])):
        if not isinstance(item, dict) or set(item) != {"name", "values"}:
            _fail("PPTX_CHART_SERIES_INVALID", index=index)
        name = item["name"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 255
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", name)):
            _fail("PPTX_CHART_SERIES_NAME_INVALID", index=index)
        if existing.get("name_cell") is None and name != existing.get("name", ""):
            _fail("PPTX_CHART_SERIES_NAME_SOURCE_UNSUPPORTED", index=index)
        if (not isinstance(item["values"], list)
                or len(item["values"]) != len(existing["value_cells"])):
            _fail("PPTX_CHART_VALUE_COUNT_MISMATCH", index=index)
        series.append({"name": name, "values": [typed(entry, {"number", "blank"})
                                               for entry in item["values"]]})
    return {"categories": categories, "series": series}


def _range_cells(reference: dict, inspection: dict) -> list[dict] | None:
    sheet = next((item for item in inspection["sheets"]
                  if item["name"].casefold() == reference["sheet"].casefold()), None)
    if sheet is None:
        return None
    by_address = {item["address"]: item for item in sheet["cells"]}
    result = []
    for address in _chart_range_addresses(reference):
        cell = by_address.get(address)
        result.append({"address": address, "sheet_id": sheet["sheet_id"],
                       "value": cell["value"] if cell is not None else {"kind": "blank", "value": None},
                       "object_id": (cell["object_id"] if cell is not None else
                                     f"xlsx:{inspection['sha256']}:{sheet['sheet_id']}:{address}")})
    return result


def _chart_cache_values(ref_node: ET.Element, value_type: str) -> list[str]:
    cache_name = "strCache" if value_type == "text" else "numCache"
    values = []
    cache = ref_node.find(f"{{{_NS_C}}}{cache_name}")
    if cache is not None:
        by_index = {int(point.get("idx", "0")): (point.find(f"{{{_NS_C}}}v").text or "")
                    for point in cache.findall(f"{{{_NS_C}}}pt")
                    if point.find(f"{{{_NS_C}}}v") is not None}
        count_node = cache.find(f"{{{_NS_C}}}ptCount")
        try:
            count = int(count_node.get("val", "0")) if count_node is not None else (max(by_index) + 1 if by_index else 0)
        except ValueError:
            return []
        values = [by_index.get(index, "") for index in range(count)]
    return values


def _chart_metadata(parts: dict[str, bytes] | None, slide_part: str | None,
                    element: ET.Element, p_ns: str) -> tuple[dict, dict] | None:
    if not parts or not slide_part:
        return None
    chart_nodes = list(element.iter(f"{{{_NS_C}}}chart"))
    if len(chart_nodes) != 1:
        return None
    chart_node = chart_nodes[0]
    relationship_ns = _NS_R if p_ns == _NS_P else _NS_R_STRICT
    chart_relation_id = chart_node.get(f"{{{relationship_ns}}}id")
    if not chart_relation_id:
        return None
    try:
        slide_relations = _relationships(parts, slide_part, [])
        chart_relation = slide_relations.get(chart_relation_id)
        if not isinstance(chart_relation, dict) or not chart_relation.get("type", "").endswith("/chart"):
            return None
        chart_part = chart_relation.get("target")
        if not isinstance(chart_part, str) or chart_part not in parts:
            return None
        chart_root, chart_namespaces = _xml_part(parts, chart_part)
        chart_relations = _relationships(parts, chart_part, [])
        workbook_relation = next((relation for relation in chart_relations.values()
                                  if isinstance(relation, dict)
                                  and relation.get("type", "").endswith("/package")
                                  and isinstance(relation.get("target"), str)), None)
        if workbook_relation is None:
            return None
        workbook_part = workbook_relation["target"]
        workbook_bytes = parts.get(workbook_part)
        if not isinstance(workbook_bytes, bytes):
            return None
        from .office_spreadsheet import inspect_spreadsheet

        workbook_inspection = inspect_spreadsheet(workbook_bytes)
        series = []
        for index, series_node in enumerate(chart_root.iter(f"{{{_NS_C}}}ser")):
            tx = series_node.find(f"{{{_NS_C}}}tx")
            name_ref = None
            name_cell = None
            name_literal = None
            if tx is not None:
                name_formula = tx.find(f".//{{{_NS_C}}}f")
                if name_formula is not None and name_formula.text:
                    name_ref = _parse_chart_range(name_formula.text)
                    if name_ref is None:
                        return None
                    name_cells = _range_cells(name_ref, workbook_inspection)
                    if name_cells is None or len(name_cells) != 1 or name_cells[0]["value"]["kind"] == "formula":
                        return None
                    name_cell = name_cells[0]
                else:
                    value_node = tx.find(f".//{{{_NS_C}}}v")
                    name_literal = value_node.text if value_node is not None else None

            category_node = series_node.find(f"{{{_NS_C}}}cat")
            value_node = series_node.find(f"{{{_NS_C}}}val")
            if category_node is None or value_node is None:
                return None
            category_ref_node = next((child for child in category_node
                                      if child.tag in {f"{{{_NS_C}}}strRef", f"{{{_NS_C}}}numRef"}), None)
            value_ref_node = next((child for child in value_node
                                   if child.tag == f"{{{_NS_C}}}numRef"), None)
            if category_ref_node is None or value_ref_node is None:
                return None
            category_formula_node = category_ref_node.find(f"{{{_NS_C}}}f")
            value_formula_node = value_ref_node.find(f"{{{_NS_C}}}f")
            if (category_formula_node is None or not category_formula_node.text
                    or value_formula_node is None or not value_formula_node.text):
                return None
            category_ref = _parse_chart_range(category_formula_node.text)
            value_ref = _parse_chart_range(value_formula_node.text)
            if category_ref is None or value_ref is None or category_ref["count"] != value_ref["count"]:
                return None
            category_cells = _range_cells(category_ref, workbook_inspection)
            value_cells = _range_cells(value_ref, workbook_inspection)
            if category_cells is None or value_cells is None:
                return None
            if any(item["value"]["kind"] == "formula" for item in category_cells + value_cells):
                return None
            name_text = (str(name_cell["value"].get("value", "")) if name_cell is not None else name_literal or "")
            series.append({
                "index": index, "name": name_text,
                "name_formula": name_formula.text if name_ref is not None else None,
                "name_cell": name_cell,
                "name_literal": name_literal,
                "category_formula": category_formula_node.text,
                "category_ref_type": "text" if category_ref_node.tag == f"{{{_NS_C}}}strRef" else "number",
                "category_ref": category_ref,
                "category_cells": category_cells,
                "category_formula_node": category_formula_node,
                "category_ref_node": category_ref_node,
                "value_formula": value_formula_node.text,
                "value_ref": value_ref,
                "value_cells": value_cells,
                "value_formula_node": value_formula_node,
                "value_ref_node": value_ref_node,
            })
        if not series:
            return None
        if any(item["name_cell"] is None and item["name_literal"] is None for item in series):
            return None
        category_formulae = {item["category_formula"] for item in series}
        if len(category_formulae) != 1:
            return None
        public = {"series": [{"name": item["name"],
                              "values": [cell["value"] for cell in item["value_cells"]]}
                             for item in series],
                  "categories": [cell["value"] for cell in series[0]["category_cells"]]}
        return {"editable": True, "edit_modes": ["replace_chart_data"], "chart_data": public}, {
            "chart_node": chart_node,
            "chart_relationship_id": chart_relation_id,
            "chart_part": chart_part,
            "chart_root": chart_root,
            "chart_namespaces": chart_namespaces,
            "chart_relations": chart_relations,
            "chart_rel_part": _relationship_part(chart_part),
            "workbook_part": workbook_part,
            "workbook_bytes": workbook_bytes,
            "workbook_sha256": workbook_inspection["sha256"],
            "series": series,
            "chart_data": public,
        }
    except (ValueError, KeyError, TypeError):
        return None


def _picture_alt(element: ET.Element) -> str:
    for node in element.iter():
        if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
            return node.attrib.get("descr", "") or node.attrib.get("title", "")
    return ""


def _set_picture_alt(element: ET.Element, value: str) -> None:
    for node in element.iter():
        if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
            node.set("descr", value)
            return
    _fail("PPTX_PICTURE_ALT_TEXT_UNAVAILABLE")


def _replacement_image(payload: dict) -> dict:
    if (not isinstance(payload, dict) or not {"content", "filename", "source"} <= payload.keys()
            or set(payload) - {"content", "filename", "source", "alt_text"}):
        _fail("PPTX_IMAGE_REPLACEMENT_INVALID")
    content, filename, source = payload["content"], payload["filename"], payload["source"]
    if not isinstance(content, bytes) or len(content) > MAX_REPLACEMENT_IMAGE_BYTES:
        _fail("PPTX_IMAGE_SIZE_UNSUPPORTED")
    if not isinstance(filename, str) or not filename or len(filename) > 500:
        _fail("PPTX_IMAGE_FILENAME_INVALID")
    if (not isinstance(source, dict) or source.get("kind") != "material"
            or not isinstance(source.get("material_id"), str)
            or not isinstance(source.get("sha256"), str)
            or source.get("size_bytes") != len(content)):
        _fail("PPTX_IMAGE_SOURCE_INVALID")
    digest = _sha256(content)
    if digest != source.get("sha256"):
        _fail("PPTX_IMAGE_SOURCE_HASH_MISMATCH")
    alt_text = payload.get("alt_text")
    if (alt_text is not None and (not isinstance(alt_text, str) or len(alt_text) > 2_000
                                  or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", alt_text))):
        _fail("PPTX_IMAGE_ALT_TEXT_INVALID")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_REPLACEMENT_IMAGE_PIXELS:
                _fail("PPTX_IMAGE_DIMENSIONS_UNSUPPORTED")
            if image_format not in {"PNG", "JPEG"}:
                _fail("PPTX_IMAGE_FORMAT_UNSUPPORTED")
            image.verify()
    except _PptxError:
        raise
    except Exception as exc:
        raise _PptxError("PPTX_IMAGE_INVALID") from exc
    extension = "png" if image_format == "PNG" else "jpg"
    content_type = "image/png" if image_format == "PNG" else "image/jpeg"
    return {"content": content, "filename": filename, "source": source,
            "sha256": digest, "size_bytes": len(content), "width": width,
            "height": height, "extension": extension, "content_type": content_type,
            "alt_text": alt_text}


def _picture_part(parts: dict[str, bytes], image: dict, added_parts: set[str]) -> str:
    for name, payload in parts.items():
        if (name.startswith("ppt/media/") and payload == image["content"]
                and name.lower().endswith("." + image["extension"])):
            return name
    stem = f"ppt/media/oevira-{image['sha256'][:24]}"
    candidate = f"{stem}.{image['extension']}"
    suffix = 2
    while candidate in parts and parts[candidate] != image["content"]:
        candidate = f"{stem}-{suffix}.{image['extension']}"
        suffix += 1
    if candidate not in parts:
        parts[candidate] = image["content"]
        added_parts.add(candidate)
    return candidate


def _picture_content_type(parts: dict[str, bytes], image: dict,
                          image_part: str, modified_parts: set[str],
                          content_types_state: dict) -> None:
    part = "[Content_Types].xml"
    state = content_types_state.get("state")
    if state is None:
        root, namespaces = _xml_part(parts, part)
        state = {"root": root, "namespaces": namespaces}
        content_types_state["state"] = state
    root = state["root"]
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    extension = image["extension"]
    defaults = [node for node in root.findall(f"{{{ct_ns}}}Default")
                if node.attrib.get("Extension", "").casefold() == extension.casefold()]
    if defaults:
        if any(node.attrib.get("ContentType") == image["content_type"] for node in defaults):
            return
        overrides = [node for node in root.findall(f"{{{ct_ns}}}Override")
                     if node.attrib.get("PartName") == "/" + image_part]
        if not overrides:
            ET.SubElement(root, f"{{{ct_ns}}}Override",
                          {"PartName": "/" + image_part, "ContentType": image["content_type"]})
            modified_parts.add(part)
        return
    ET.SubElement(root, f"{{{ct_ns}}}Default",
                  {"Extension": extension, "ContentType": image["content_type"]})
    modified_parts.add(part)


def _add_image_relationship(parts: dict[str, bytes], slide_part: str, target_part: str,
                           source_hash: str, p_ns: str,
                           relationship_roots: dict[str, tuple[ET.Element, list[tuple[str, str]]]],
                           modified_parts: set[str]) -> str:
    relationship_part = _relationship_part(slide_part)
    if relationship_part in relationship_roots:
        root, namespaces = relationship_roots[relationship_part]
    elif relationship_part in parts:
        root, namespaces = _xml_part(parts, relationship_part)
        relationship_roots[relationship_part] = (root, namespaces)
    else:
        root = ET.Element(f"{{{_NS_PACKAGE_R}}}Relationships")
        namespaces = [("", _NS_PACKAGE_R)]
        relationship_roots[relationship_part] = (root, namespaces)
        modified_parts.add(relationship_part)
    identifiers = {node.attrib.get("Id") for node in root}
    base_id = "rIdOevira" + source_hash[:16]
    relation_id = base_id
    suffix = 2
    while relation_id in identifiers:
        relation_id = f"{base_id}-{suffix}"
        suffix += 1
    relationship_ns = _NS_R if p_ns == _NS_P else _NS_R_STRICT
    target = posixpath.relpath(target_part, posixpath.dirname(slide_part))
    ET.SubElement(root, f"{{{_NS_PACKAGE_R}}}Relationship",
                  {"Id": relation_id, "Type": f"{relationship_ns}/image", "Target": target})
    modified_parts.add(relationship_part)
    return relation_id


def _slide_objects(root: ET.Element, slide_number: int, p_ns: str,
                   a_ns: str, *, parts: dict[str, bytes] | None = None,
                   slide_part: str | None = None) -> tuple[list[dict], dict[str, dict]]:
    shape_tree = root.find(f".//{{{p_ns}}}spTree")
    if shape_tree is None:
        return [], {}
    objects: list[dict] = []
    bindings: dict[str, dict] = {}
    seen: set[str] = set()
    parents = {child: parent for parent in root.iter() for child in parent}
    ordinals = {element: ordinal for ordinal, element in enumerate(shape_tree.iter(), 1)}

    def add(identity: str, kind: str, element: ET.Element, text: str,
            editable: bool, reason: str | None, name: str = "",
            edit_modes: list[str] | None = None) -> None:
        if identity in seen:
            _fail("PPTX_OBJECT_ID_AMBIGUOUS")
        seen.add(identity)
        group_ancestors = _group_ancestors(element, parents, ordinals, slide_number, p_ns, shape_tree)
        objects.append({"id": identity, "kind": kind, "text": text,
                        "editable": editable, "edit_modes": edit_modes or [],
                        "reason": reason, "group_ancestors": group_ancestors})
        bindings[identity] = {"element": element, "name": name,
                              "kind": kind, "editable": editable,
                              "text": text, "reason": reason,
                              "edit_modes": edit_modes or [],
                              "group_ancestors": group_ancestors}
        if len(objects) > MAX_OBJECTS_PER_SLIDE:
            _fail("PPTX_OBJECT_LIMIT")

    ordinal = 0
    for element in shape_tree.iter():
        ordinal += 1
        if element.tag == f"{{{p_ns}}}sp":
            name, native_id = _properties(element)
            identity = _object_id(slide_number, "shape", native_id, ordinal)
            text, editable, reason, text_nodes, whole_text_editable = _text_and_editability(element, a_ns)
            if native_id is None:
                editable, reason = False, "对象缺少稳定的 OOXML 对象 ID。"
            edit_modes = []
            if editable:
                edit_modes = ["exact_span"]
                if whole_text_editable:
                    edit_modes.insert(0, "whole_text")
            add(identity, "shape_text" if text else "shape", element, text,
                editable, reason, name, edit_modes)
            bindings[identity]["text_nodes"] = text_nodes
            bindings[identity]["whole_text_editable"] = whole_text_editable
        elif element.tag == f"{{{p_ns}}}graphicFrame":
            name, native_id = _properties(element)
            identity = _object_id(slide_number, "frame", native_id, ordinal)
            table = element.find(f".//{{{a_ns}}}tbl")
            if table is None:
                chart_metadata = _chart_metadata(parts, slide_part, element, p_ns)
                if chart_metadata is None:
                    add(identity, "graphic_frame", element, "", False,
                        "SmartArt、外链图表或嵌入对象不是可安全重算的图表数据。", name)
                else:
                    metadata, chart_binding = chart_metadata
                    add(identity, "chart", element, "", True, None, name,
                        metadata.get("edit_modes", []))
                    objects[-1]["chart_data"] = metadata["chart_data"]
                    bindings[identity].update(chart_binding)
                continue
            for row_number, row in enumerate(table.findall(f"{{{a_ns}}}tr"), 1):
                for column_number, cell in enumerate(row.findall(f"{{{a_ns}}}tc"), 1):
                    cell_id = f"{identity}-r{row_number}-c{column_number}"
                    text, editable, reason, text_nodes, whole_text_editable = _text_and_editability(cell, a_ns)
                    edit_modes = []
                    if editable:
                        edit_modes = ["exact_span"]
                        if whole_text_editable:
                            edit_modes.insert(0, "whole_text")
                    add(cell_id, "table_cell", cell, text, editable, reason, name, edit_modes)
                    bindings[cell_id]["text_nodes"] = text_nodes
                    bindings[cell_id]["whole_text_editable"] = whole_text_editable
        elif element.tag == f"{{{p_ns}}}pic":
            name, native_id = _properties(element)
            identity = _object_id(slide_number, "picture", native_id, ordinal)
            alt = ""
            for node in element.iter():
                if node.tag.endswith("}cNvPr") or node.tag == "cNvPr":
                    alt = node.attrib.get("descr", "") or node.attrib.get("title", "")
                    break
            ancestors = _group_ancestors(element, parents, ordinals, slide_number, p_ns, shape_tree)
            metadata, binding_data = _picture_metadata(
                element, parts, slide_part, slide_number, p_ns, a_ns, ancestors,
            )
            editable = bool(metadata.get("editable", False))
            reason = metadata.get("reason") if not editable else None
            modes = metadata.get("edit_modes", []) if editable else []
            add(identity, "picture", element, alt, editable, reason, name, modes)
            objects[-1].update({key: value for key, value in metadata.items()
                                if key not in {"editable", "edit_modes", "reason"}})
            bindings[identity].update(binding_data)
        elif element.tag == f"{{{p_ns}}}cxnSp":
            name, native_id = _properties(element)
            identity = _object_id(slide_number, "connector", native_id, ordinal)
            text, _, _, _, _ = _text_and_editability(element, a_ns)
            add(identity, "connector", element, text, False,
                "连接线对象可能依赖几何和连接关系；当前保留原对象。", name)
        elif element.tag == f"{{{p_ns}}}grpSp":
            name, native_id = _properties(element)
            identity = _object_id(slide_number, "group", native_id, ordinal)
            add(identity, "group", element, "", False,
                "组合对象保持原样；可单独检查其中支持的文本框。", name)
    return objects, bindings


def _presentation(content: bytes) -> dict:
    infos, parts, comment, warnings = _validate_archive(content)
    required = {"[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"}
    if not required.issubset(parts):
        _fail("PPTX_REQUIRED_PART_MISSING")
    content_types, _ = _xml_part(parts, "[Content_Types].xml")
    if content_types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
        _fail("PPTX_CONTENT_TYPES_INVALID")
    signed = (any(name.startswith("_xmlsignatures/") for name in parts)
              or any("digital-signature" in node.attrib.get("ContentType", "").casefold()
                     for node in content_types))
    if signed and not any("数字签名" in warning for warning in warnings):
        warnings.append("文件含有数字签名；修改会使原签名失效，因此拒绝保存修改。")

    for name in parts:
        if name.endswith(".rels"):
            root, _ = _xml_part(parts, name)
            if root.tag != f"{{{_NS_PACKAGE_R}}}Relationships":
                _fail("PPTX_RELATIONSHIP_XML_INVALID")
            for relation in root:
                if relation.attrib.get("TargetMode") == "External":
                    warning = "存在外部链接；不会访问外部目标。"
                    if warning not in warnings:
                        warnings.append(warning)

    manifest = _slide_manifest(parts, warnings)
    return {"infos": infos, "parts": parts, "comment": comment,
            "warnings": warnings, "manifest": manifest, "signed": signed}


def inspect_presentation(content: bytes) -> dict:
    """List slide objects and their safe text-edit capability."""
    package = _presentation(content)
    slides = []
    for slide in package["manifest"]:
        if slide["part"] is None:
            slides.append({"number": slide["number"], "objects": []})
            continue
        root, _ = _xml_part(package["parts"], slide["part"])
        objects, _ = _slide_objects(root, slide["number"], slide["p_ns"], slide["a_ns"],
                                    parts=package["parts"], slide_part=slide["part"])
        slides.append({"number": slide["number"], "objects": objects})
    return {"slides": slides, "warnings": package["warnings"]}


def _register_namespaces(namespaces: list[tuple[str, str]]) -> None:
    for prefix, uri in namespaces:
        if prefix == "xml":
            continue
        try:
            ET.register_namespace(prefix, uri)
        except ValueError as exc:
            raise _PptxError("PPTX_NAMESPACE_PREFIX_UNSUPPORTED") from exc


def _restore_undeclared_namespaces(serialized: bytes,
                                  namespaces: list[tuple[str, str]]) -> bytes:
    present = set(re.findall(br"xmlns(?::([A-Za-z_][\w.-]*))?=", serialized))
    missing = []
    for prefix, uri in namespaces:
        if prefix == "xml":
            continue
        token = prefix.encode("utf-8") if prefix else b""
        if token not in present:
            declaration = (b" xmlns=" if not prefix else b" xmlns:" + prefix.encode("utf-8") + b"=")
            if not prefix:
                declaration += quoteattr(uri).encode("utf-8")
            else:
                declaration += quoteattr(uri).encode("utf-8")
            missing.append(declaration)
    if not missing:
        return serialized
    root_start = re.search(br"<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[\s/>])", serialized)
    if root_start is None:
        _fail("PPTX_XML_SERIALIZATION_INVALID")
    return serialized[:root_start.end()] + b"".join(missing) + serialized[root_start.end():]


def _serialize(root: ET.Element, namespaces: list[tuple[str, str]]) -> bytes:
    namespace_map = getattr(ET, "_namespace_map", None)
    with _XML_NAMESPACE_LOCK:
        original = namespace_map.copy() if namespace_map is not None else None
        try:
            _register_namespaces(namespaces)
            raw = ET.tostring(root, encoding="utf-8", xml_declaration=True,
                              short_empty_elements=True)
            return _restore_undeclared_namespaces(raw, namespaces)
        finally:
            if namespace_map is not None and original is not None:
                namespace_map.clear()
                namespace_map.update(original)


def _replace_text(binding: dict, value: str) -> None:
    if binding["editable"] is not True:
        _fail("PPTX_OBJECT_NOT_EDITABLE")
    if binding.get("whole_text_editable") is not True:
        _fail("PPTX_MIXED_STYLE_REQUIRES_EXACT_SPAN")
    if "\n" in value or "\r" in value:
        _fail("PPTX_TEXT_NEWLINE_UNSUPPORTED")
    text_nodes = binding.get("text_nodes", [])
    if not text_nodes:
        _fail("PPTX_TEXT_RUN_MISSING")
    text_nodes[0].text = value
    if value[:1].isspace() or value[-1:].isspace():
        text_nodes[0].set(f"{{{_NS_XML}}}space", "preserve")
    for node in text_nodes[1:]:
        node.text = ""


def _matches(text: str, needle: str) -> list[int]:
    matches = []
    start = 0
    while True:
        position = text.find(needle, start)
        if position < 0:
            return matches
        matches.append(position)
        start = position + 1


def _replace_span_preview(current: str, before: str, after: str) -> tuple[str, str]:
    matches = _matches(current, before)
    if not matches:
        _fail("PPTX_TEXT_MATCH_NOT_FOUND")
    if len(matches) != 1:
        _fail("PPTX_TEXT_MATCH_AMBIGUOUS")
    start = matches[0]
    return current, current[:start] + after + current[start + len(before):]


def _set_text_node(node: ET.Element, value: str) -> None:
    node.text = value
    if value.startswith(" ") or value.endswith(" "):
        node.set(f"{{{_NS_XML}}}space", "preserve")
    else:
        node.attrib.pop(f"{{{_NS_XML}}}space", None)


def _replace_exact_span(binding: dict, before: str, after: str) -> tuple[str, str]:
    nodes = binding.get("text_nodes", [])
    values = [node.text or "" for node in nodes]
    current = "".join(values)
    matches = _matches(current, before)
    if not matches:
        _fail("PPTX_TEXT_MATCH_NOT_FOUND")
    if len(matches) != 1:
        _fail("PPTX_TEXT_MATCH_AMBIGUOUS")
    start = matches[0]
    end = start + len(before)
    offsets = []
    cursor = 0
    for value in values:
        offsets.append((cursor, cursor + len(value)))
        cursor += len(value)
    first = next((index for index, (left, right) in enumerate(offsets)
                  if left <= start < right), None)
    last = next((index for index, (left, right) in enumerate(offsets)
                 if left < end <= right), None)
    if first is None or last is None:
        _fail("PPTX_TEXT_RUN_MISSING")
    prefix = values[first][:start - offsets[first][0]]
    suffix = values[last][end - offsets[last][0]:]
    if first == last:
        _set_text_node(nodes[first], prefix + after + suffix)
    else:
        _set_text_node(nodes[first], prefix + after)
        for index in range(first + 1, last):
            _set_text_node(nodes[index], "")
        _set_text_node(nodes[last], suffix)
    return current, current[:start] + after + current[end:]


def _member_payloads_unchanged(before: dict[str, bytes], after: dict[str, bytes],
                               modified: set[str], added: set[str] | None = None) -> bool:
    added = added or set()
    if set(before) - set(after) or (set(after) - set(before)) != added:
        return False
    return all(before[name] == after[name] for name in before if name not in modified)


def revise_presentation(content: bytes, changes: list[dict],
                        allowed_slides: list[int]) -> dict:
    """Revise editable objects on explicitly authorized existing slides."""
    if not isinstance(changes, list):
        _fail("PPTX_CHANGES_INVALID")
    if not isinstance(allowed_slides, list) or any(type(number) is not int for number in allowed_slides):
        _fail("PPTX_ALLOWED_SLIDES_INVALID")
    if any(number <= 0 for number in allowed_slides):
        _fail("PPTX_ALLOWED_SLIDES_INVALID")
    if len(set(allowed_slides)) != len(allowed_slides):
        _fail("PPTX_ALLOWED_SLIDES_DUPLICATED")
    if len(changes) > MAX_OBJECTS_PER_SLIDE:
        _fail("PPTX_CHANGE_LIMIT")

    package = _presentation(content)
    if package["signed"]:
        _fail("PPTX_SIGNED_PACKAGE_EDIT_UNSUPPORTED")
    slide_by_number = {item["number"]: item for item in package["manifest"]}
    if any(number not in slide_by_number for number in allowed_slides):
        _fail("PPTX_ALLOWED_SLIDE_NOT_FOUND")
    seen_changes: set[str] = set()
    parsed_slides: dict[int, tuple[ET.Element, list[tuple[str, str]], dict[str, dict]]] = {}
    checked: list[dict] = []

    for item in changes:
        whole_text_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "text"}
        exact_span_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "before", "after"}
        picture_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "replace_image"}
        chart_data_change = isinstance(item, dict) and set(item) == {"slide", "object_id", "chart_data"}
        if (not (whole_text_change or exact_span_change or picture_change or chart_data_change)
                or type(item["slide"]) is not int or not isinstance(item["object_id"], str)
                or (whole_text_change and not isinstance(item["text"], str))
                or (exact_span_change and (not isinstance(item["before"], str)
                                           or not item["before"]
                                           or not isinstance(item["after"], str)))):
            _fail("PPTX_CHANGE_INVALID")
        slide_number, identity = item["slide"], item["object_id"]
        if slide_number not in allowed_slides:
            _fail("PPTX_CHANGE_OUT_OF_SCOPE")
        if identity in seen_changes:
            _fail("PPTX_OBJECT_CHANGE_DUPLICATED")
        seen_changes.add(identity)
        slide = slide_by_number.get(slide_number)
        if slide is None or slide["part"] is None:
            _fail("PPTX_SLIDE_UNAVAILABLE")
        if slide_number not in parsed_slides:
            root, namespaces = _xml_part(package["parts"], slide["part"])
            _, bindings = _slide_objects(root, slide_number, slide["p_ns"], slide["a_ns"],
                                         parts=package["parts"], slide_part=slide["part"])
            parsed_slides[slide_number] = (root, namespaces, bindings)
        root, namespaces, bindings = parsed_slides[slide_number]
        binding = bindings.get(identity)
        if binding is None:
            _fail("PPTX_OBJECT_NOT_FOUND")
        if binding["editable"] is not True:
            _fail("PPTX_OBJECT_NOT_EDITABLE")
        if picture_change:
            if binding.get("kind") != "picture" or binding.get("image_blip") is None:
                _fail("PPTX_OBJECT_NOT_EDITABLE")
            image = _replacement_image(item["replace_image"])
            if image["sha256"] == binding.get("image_media", {}).get("sha256"):
                continue
            checked.append({"slide": slide_number, "object_id": identity,
                            "kind": "picture_replacement", "binding": binding,
                            "root": root, "namespaces": namespaces, "part": slide["part"],
                            "change_mode": "picture_replace", "image": image,
                            "before_media": dict(binding.get("image_media", {}))})
            continue
        if chart_data_change:
            if binding.get("kind") != "chart" or not isinstance(binding.get("chart_root"), ET.Element):
                _fail("PPTX_CHART_OBJECT_NOT_EDITABLE")
            chart_data = _normalize_chart_data(item["chart_data"], binding)
            original_chart_data = _normalize_chart_data(binding.get("chart_data"), binding)
            if chart_data == original_chart_data:
                continue
            checked.append({"slide": slide_number, "object_id": identity,
                            "kind": "chart_data", "binding": binding,
                            "root": root, "namespaces": namespaces, "part": slide["part"],
                            "change_mode": "chart_data_replace", "chart_data": chart_data})
            continue

        value = item.get("text") if whole_text_change else item["after"]
        if len(value) > MAX_CHANGE_TEXT_CHARS or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            _fail("PPTX_TEXT_INVALID")
        before = item.get("before") if exact_span_change else None
        if exact_span_change and (len(before) > MAX_CHANGE_TEXT_CHARS
                                  or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", before)):
            _fail("PPTX_TEXT_INVALID")
        if any("\n" in text or "\r" in text for text in ([value, before] if before is not None else [value])):
            _fail("PPTX_TEXT_NEWLINE_UNSUPPORTED")
        if any(0xD800 <= ord(character) <= 0xDFFF or ord(character) in {0xFFFE, 0xFFFF}
               for text in ([value, before] if before is not None else [value])
               for character in text):
            _fail("PPTX_TEXT_INVALID")
        if whole_text_change:
            if binding.get("whole_text_editable") is not True:
                _fail("PPTX_MIXED_STYLE_REQUIRES_EXACT_SPAN")
            old_text, new_text = binding["text"], value
        else:
            old_text, new_text = _replace_span_preview(binding["text"], before, value)
        checked.append({"slide": slide_number, "object_id": identity,
                        "before": old_text, "after": new_text,
                        "kind": binding["kind"],
                        "binding": binding, "root": root, "namespaces": namespaces,
                        "part": slide["part"], "change_mode": "whole_text" if whole_text_change else "exact_span",
                        "selection": None if whole_text_change else {"before": before, "after": value}})

    checked = [item for item in checked
               if item["change_mode"] in {"picture_replace", "chart_data_replace"}
               or item["before"] != item["after"]]
    effective_slides = {item["slide"] for item in checked}
    parsed_slides = {
        number: parsed for number, parsed in parsed_slides.items()
        if number in effective_slides
    }

    if not checked:
        return {"content": content, "changes": [], "diff": [],
                "sha256": _sha256(content), "unchanged_verified": True,
                "warnings": package["warnings"]}

    direct_changes = {(item["slide"], item["object_id"]): item for item in checked}
    original_signatures: dict[int, dict[str, str]] = {}
    original_structures: dict[int, dict[str, str]] = {}
    for slide_number, (root, _namespaces, bindings) in parsed_slides.items():
        original_signatures[slide_number] = {
            identity: _canonical(binding["element"])
            for identity, binding in bindings.items()
        }
        a_ns = slide_by_number[slide_number]["a_ns"]
        original_structures[slide_number] = {
            identity: (_picture_structure_signature(
                           binding["element"], a_ns,
                           ignore_alt_text=(direct_changes.get((slide_number, identity), {})
                                            .get("image", {}).get("alt_text") is not None))
                       if binding["kind"] == "picture" else
                       _chart_frame_signature(binding["element"])
                       if binding["kind"] == "chart" else
                       _group_structure_signature(binding["element"], a_ns)
                       if binding["kind"] == "group" else
                       _text_independent_signature(binding["element"], a_ns))
            for identity, binding in bindings.items()
        }

    selected_ancestor_groups = {
        (item["slide"], group_id)
        for item in checked
        for group_id in item["binding"].get("group_ancestors", [])
    }
    changed_objects = set(direct_changes) | selected_ancestor_groups
    modified_parts = {item["part"] for item in checked
                      if item["change_mode"] != "chart_data_replace"}
    added_parts: set[str] = set()
    relationship_roots: dict[str, tuple[ET.Element, list[tuple[str, str]]]] = {}
    content_types_state: dict[str, dict] = {}
    image_after: dict[tuple[int, str], dict] = {}
    for item in checked:
        if item["change_mode"] == "picture_replace":
            image = item["image"]
            slide = slide_by_number[item["slide"]]
            image_part = _picture_part(package["parts"], image, added_parts)
            _picture_content_type(package["parts"], image, image_part,
                                  modified_parts, content_types_state)
            relationship_id = _add_image_relationship(
                package["parts"], item["part"], image_part, image["sha256"], slide["p_ns"],
                relationship_roots, modified_parts,
            )
            blip = item["binding"]["image_blip"]
            relationship_ns = _NS_R if slide["p_ns"] == _NS_P else _NS_R_STRICT
            blip.set(f"{{{relationship_ns}}}embed", relationship_id)
            blip.attrib.pop(f"{{{relationship_ns}}}link", None)
            alt_text = image.get("alt_text")
            if alt_text is not None:
                _set_picture_alt(item["binding"]["element"], alt_text)
            item["relationship_id"] = relationship_id
            item["image_part"] = image_part
            image_after[(item["slide"], item["object_id"])] = {
                "sha256": image["sha256"], "size_bytes": image["size_bytes"],
                "mime_type": image["content_type"], "filename": image["filename"],
                "material_id": image["source"]["material_id"],
                "dimensions": [image["width"], image["height"]],
                "alt_text": alt_text,
            }
        elif item["change_mode"] == "chart_data_replace":
            chart_info = item["binding"]
            before_signature = _chart_cache_independent_signature(chart_info["chart_root"])
            chart_result = _apply_chart_data(
                package["parts"], item["part"], slide_by_number[item["slide"]]["p_ns"],
                chart_info, item["chart_data"], modified_parts, added_parts,
                relationship_roots, content_types_state,
            )
            chart_result["structure_signature_before"] = before_signature
            item["chart_result"] = chart_result
        elif item["change_mode"] == "whole_text":
            _replace_text(item["binding"], item["after"])
        else:
            _replace_exact_span(item["binding"], item["selection"]["before"], item["selection"]["after"])
    for relationship_part, (root, namespaces) in relationship_roots.items():
        package["parts"][relationship_part] = _serialize(root, namespaces)
    if "state" in content_types_state and "[Content_Types].xml" in modified_parts:
        state = content_types_state["state"]
        package["parts"]["[Content_Types].xml"] = _serialize(state["root"], state["namespaces"])
    expected_slide_signatures = {
        slide_number: _canonical(root)
        for slide_number, (root, _namespaces, _bindings) in parsed_slides.items()
        if slide_by_number[slide_number]["part"] in modified_parts
    }
    replacements = {
        slide_by_number[slide_number]["part"]: _serialize(root, namespaces)
        for slide_number, (root, namespaces, _bindings) in parsed_slides.items()
        if slide_by_number[slide_number]["part"] in modified_parts
    }
    for part in modified_parts - {item["part"] for item in checked}:
        if part in package["parts"]:
            replacements[part] = package["parts"][part]

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        target.comment = package["comment"]
        for info in package["infos"]:
            payload = replacements.get(info.filename, package["parts"][info.filename])
            target.writestr(copy.copy(info), payload)
        for name in sorted(added_parts):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, package["parts"][name])
    revised = output.getvalue()
    verified_package = _presentation(revised)
    unchanged_verified = _member_payloads_unchanged(
        _presentation(content)["parts"], verified_package["parts"], modified_parts, added_parts)
    if not unchanged_verified:
        _fail("PPTX_UNMODIFIED_MEMBER_CHANGED")

    for slide in verified_package["manifest"]:
        if slide["part"] is None or slide["number"] not in parsed_slides:
            continue
        new_root, _ = _xml_part(verified_package["parts"], slide["part"])
        if (slide["part"] in modified_parts
                and _canonical(new_root) != expected_slide_signatures[slide["number"]]):
            _fail("PPTX_SLIDE_REOPEN_MISMATCH")
        _, new_bindings = _slide_objects(new_root, slide["number"], slide["p_ns"], slide["a_ns"],
                                         parts=verified_package["parts"], slide_part=slide["part"])
        for identity, expected_signature in original_signatures[slide["number"]].items():
            current = new_bindings.get(identity)
            if current is None:
                _fail("PPTX_UNSELECTED_OBJECT_CHANGED")
            if (slide["number"], identity) in changed_objects:
                original_binding = parsed_slides[slide["number"]][2].get(identity)
                change = direct_changes.get((slide["number"], identity))
                if change is not None and change["change_mode"] == "picture_replace":
                    after_image = image_after[(slide["number"], identity)]
                    structure = _picture_structure_signature(
                        current["element"], slide["a_ns"],
                        ignore_alt_text=after_image["alt_text"] is not None)
                    if (structure != original_structures[slide["number"]][identity]
                            or current.get("image_media", {}).get("sha256") != after_image["sha256"]
                            or (after_image["alt_text"] is not None
                                and _picture_alt(current["element"]) != after_image["alt_text"])):
                        _fail("PPTX_SELECTED_PICTURE_STRUCTURE_CHANGED")
                elif original_binding is not None and original_binding["kind"] == "group":
                    if _group_structure_signature(current["element"], slide["a_ns"]) != \
                            original_structures[slide["number"]][identity]:
                        _fail("PPTX_GROUP_STRUCTURE_CHANGED")
                elif original_binding is not None and original_binding["kind"] == "chart":
                    if _chart_frame_signature(current["element"]) != \
                            original_structures[slide["number"]][identity]:
                        _fail("PPTX_SELECTED_CHART_FRAME_CHANGED")
                elif _text_independent_signature(current["element"], slide["a_ns"]) != \
                        original_structures[slide["number"]][identity]:
                    _fail("PPTX_SELECTED_OBJECT_STRUCTURE_CHANGED")
            elif _canonical(current["element"]) != expected_signature:
                _fail("PPTX_UNSELECTED_OBJECT_CHANGED")
        for item in checked:
            if item["slide"] != slide["number"]:
                continue
            current = new_bindings.get(item["object_id"])
            if item["change_mode"] == "picture_replace":
                if (current is None or current.get("image_media", {}).get("sha256")
                        != item["image"]["sha256"]
                        or verified_package["parts"].get(item["image_part"]) != item["image"]["content"]):
                    _fail("PPTX_REOPEN_IMAGE_MISMATCH")
            elif item["change_mode"] == "chart_data_replace":
                chart_result = item["chart_result"]
                if current is None or current.get("kind") != "chart":
                    _fail("PPTX_REOPEN_CHART_MISMATCH")
                if _chart_cache_independent_signature(current["chart_root"]) != \
                        chart_result["structure_signature_before"]:
                    _fail("PPTX_CHART_STRUCTURE_CHANGED")
                if current.get("workbook_part") != chart_result["workbook_part"]:
                    _fail("PPTX_CHART_WORKBOOK_RELATION_MISMATCH")
                workbook_payload = verified_package["parts"].get(chart_result["workbook_part"])
                if (not isinstance(workbook_payload, bytes)
                        or _sha256(workbook_payload) != chart_result["embedded_workbook_sha256_after"]):
                    _fail("PPTX_CHART_WORKBOOK_REOPEN_MISMATCH")
                actual_projection = {
                    (value["kind"], value["formula"]): value
                    for value in _chart_cache_projection(current["chart_root"])
                }
                for expected in chart_result["expected_cache_projection"]:
                    key = (expected["kind"], expected["formula"])
                    if actual_projection.get(key) != expected:
                        _fail("PPTX_CHART_CACHE_REOPEN_MISMATCH", formula=expected["formula"])
                current_data = _normalize_chart_data(current.get("chart_data"), current)
                if current_data != item["chart_data"]:
                    _fail("PPTX_CHART_DATA_REOPEN_MISMATCH")
            elif current is None or current["text"] != item["after"]:
                _fail("PPTX_REOPEN_TEXT_MISMATCH")

    applied = []
    warnings = list(package["warnings"])
    for item in checked:
        if item["change_mode"] == "picture_replace":
            image = item["image"]
            applied.append({"slide": item["slide"], "object_id": item["object_id"],
                            "kind": "picture_replacement",
                            "before": {"sha256": item["before_media"].get("sha256"),
                                       "size_bytes": item["before_media"].get("size_bytes")},
                            "after": {"source": image["source"], "sha256": image["sha256"],
                                      "size_bytes": image["size_bytes"],
                                      "mime_type": image["content_type"],
                                      "dimensions": [image["width"], image["height"]]},
                            "group_ancestors": item["binding"].get("group_ancestors", []),
                            "geometry_preserved": True,
                            "crop_preserved": True,
                            "alt_text": item["binding"].get("text", "") if image.get("alt_text") is None
                            else image["alt_text"],
                            "alt_text_updated": image.get("alt_text") is not None})
            if image.get("alt_text") is None:
                warning = "图片替换保留了原有替代文字；其是否仍描述新图片需要人工核对。"
                if warning not in warnings:
                    warnings.append(warning)
        elif item["change_mode"] == "chart_data_replace":
            chart_result = item["chart_result"]
            applied.append({"slide": item["slide"], "object_id": item["object_id"],
                            "kind": "chart_data_replacement",
                            "before": chart_result["before"], "after": chart_result["after"],
                            "group_ancestors": item["binding"].get("group_ancestors", []),
                            "geometry_preserved": True,
                            "copy_on_write": chart_result["copy_on_write"],
                            "embedded_workbook_sha256_before": chart_result["embedded_workbook_sha256_before"],
                            "embedded_workbook_sha256_after": chart_result["embedded_workbook_sha256_after"],
                            "calculation_engine": chart_result["embedded_workbook_calculation_engine"],
                            "calculation_revision": chart_result["embedded_workbook_calculation_revision"],
                            "chart_caches_updated": chart_result["chart_caches_updated"],
                            "unchanged_verified": chart_result["unchanged_verified"]})
        else:
            applied.append({"slide": item["slide"], "object_id": item["object_id"],
                            "kind": item["kind"], "before": item["before"], "after": item["after"],
                            **({"selection": item["selection"]} if item["selection"] is not None else {})})
    return {
        "content": revised,
        "changes": applied,
        "diff": copy.deepcopy(applied),
        "sha256": _sha256(revised),
        "unchanged_verified": unchanged_verified,
        "added_parts": sorted(added_parts),
        "modified_parts": sorted(modified_parts),
        "warnings": warnings,
    }

