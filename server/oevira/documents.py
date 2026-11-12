"""Bounded DOCX object inspection and precise revision.

The original Office package remains the authority. A revision changes text nodes
inside selected paragraphs, replaces a selected inline image, updates selected
comments, or resolves simple tracked insertions/deletions. Other package members
and unsupported Word features stay readable and preserved; unsupported objects
are never silently flattened.
"""

from __future__ import annotations

from difflib import SequenceMatcher

import hashlib
import io
import mimetypes
import posixpath
import copy
import re
import stat
from urllib.parse import urlsplit
import zipfile

from lxml import etree


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 24 * 1024 * 1024
MAX_ZIP_ENTRIES = 5_000
MAX_CHANGE_TEXT_CHARS = 32_000
MAX_REPLACEMENT_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REPLACEMENT_IMAGE_PIXELS = 100_000_000

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"
_NS = {"w": W_NS, "w14": W14_NS, "rel": PKG_REL_NS}
_XML_FORBIDDEN = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WORD_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

_READ_ONLY_MARKERS = {
    f"{{{W_NS}}}{name}"
    for name in (
        "ins", "del", "moveFrom", "moveTo", "commentRangeStart", "commentRangeEnd",
        "bookmarkStart", "bookmarkEnd", "fldSimple", "fldChar", "instrText",
        "drawing", "pict", "object", "footnoteReference", "endnoteReference",
        "subDoc", "sdt", "altChunk", "br", "cr", "tab", "lastRenderedPageBreak",
    )
}
_TRACKED_CHANGE_UNSAFE_MARKERS = _READ_ONLY_MARKERS - {
    f"{{{W_NS}}}ins", f"{{{W_NS}}}del", f"{{{W_NS}}}delText",
}


class DocumentError(ValueError):
    """A stable, caller-visible DOCX processing error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise DocumentError(code)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_xml(content: bytes, part_name: str):
    if len(content) > MAX_XML_BYTES or b"\x00" in content or _XML_FORBIDDEN.search(content):
        _fail("DOCX_XML_UNSAFE_OR_TOO_LARGE")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_comments=False,
        remove_pis=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except (etree.XMLSyntaxError, RecursionError, ValueError, UnicodeError) as exc:
        raise DocumentError(f"DOCX_XML_INVALID:{part_name}") from exc


def _safe_member_name(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        return False
    return not any(part == ".." for part in name.split("/"))


def _resolve_internal_target(source_part: str, target: str) -> str:
    if not target or "\\" in target or "\x00" in target:
        _fail("DOCX_RELATIONSHIP_TARGET_INVALID")
    uri = urlsplit(target)
    if uri.scheme or uri.netloc or uri.query or uri.fragment:
        _fail("DOCX_RELATIONSHIP_TARGET_INVALID")
    path = uri.path
    resolved = posixpath.normpath(path.lstrip("/") if path.startswith("/")
                                  else posixpath.join(posixpath.dirname(source_part), path))
    if resolved in {".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        _fail("DOCX_RELATIONSHIP_TARGET_INVALID")
    return resolved


def _read_package(content: bytes):
    if not isinstance(content, bytes):
        _fail("DOCX_CONTENT_MUST_BE_BYTES")
    if len(content) > MAX_INPUT_BYTES:
        _fail("DOCX_INPUT_TOO_LARGE")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError("DOCX_PACKAGE_INVALID") from exc

    parts: dict[str, bytes] = {}
    infos: list[zipfile.ZipInfo] = []
    seen_names: set[str] = set()
    seen_casefold: set[str] = set()
    expanded_size = 0
    try:
        listed = archive.infolist()
        if len(listed) > MAX_ZIP_ENTRIES:
            _fail("DOCX_ZIP_ENTRY_LIMIT")
        for info in listed:
            name = info.filename
            if not _safe_member_name(name):
                _fail("DOCX_MEMBER_PATH_UNSAFE")
            folded = name.casefold()
            if name in seen_names or folded in seen_casefold:
                _fail("DOCX_MEMBER_NAME_AMBIGUOUS")
            seen_names.add(name)
            seen_casefold.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                _fail("DOCX_MEMBER_SYMLINK_FORBIDDEN")
            if info.flag_bits & 0x1:
                _fail("DOCX_ENCRYPTED_MEMBER_UNSUPPORTED")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _fail("DOCX_COMPRESSION_METHOD_UNSUPPORTED")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                _fail("DOCX_MEMBER_SIZE_LIMIT")
            expanded_size += info.file_size
            if expanded_size > MAX_PACKAGE_BYTES:
                _fail("DOCX_EXPANDED_SIZE_LIMIT")

        for info in listed:
            try:
                payload = archive.read(info)
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError) as exc:
                raise DocumentError("DOCX_MEMBER_CORRUPT") from exc
            if len(payload) != info.file_size:
                _fail("DOCX_MEMBER_SIZE_MISMATCH")
            parts[info.filename] = payload
            infos.append(info)
        comment = archive.comment
    finally:
        archive.close()

    if "[Content_Types].xml" not in parts or "_rels/.rels" not in parts:
        _fail("DOCX_PACKAGE_INCOMPLETE")
    root_relationships = _parse_xml(parts["_rels/.rels"], "_rels/.rels")
    main_targets = [
        item.get("Target")
        for item in root_relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        if (item.get("Type") or "").rstrip("/").endswith("/officeDocument")
        and item.get("TargetMode", "Internal") != "External"
    ]
    if len(main_targets) != 1 or not main_targets[0]:
        _fail("DOCX_MAIN_DOCUMENT_RELATIONSHIP_INVALID")
    main_part = _resolve_internal_target("", main_targets[0])
    if main_part not in parts:
        _fail("DOCX_MAIN_DOCUMENT_MISSING")

    content_types = _parse_xml(parts["[Content_Types].xml"], "[Content_Types].xml")
    if content_types.tag != f"{{{CT_NS}}}Types":
        _fail("DOCX_CONTENT_TYPES_INVALID")
    root = _parse_xml(parts[main_part], main_part)
    if root.tag != f"{{{W_NS}}}document" or root.find(f"{{{W_NS}}}body") is None:
        _fail("DOCX_MAIN_DOCUMENT_UNSUPPORTED")
    return infos, parts, comment, main_part, root


def _paragraph_text(paragraph) -> str:
    chunks: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{{{W_NS}}}t":
            chunks.append(node.text or "")
        elif node.tag == f"{{{W_NS}}}tab":
            chunks.append("\t")
        elif node.tag in {f"{{{W_NS}}}br", f"{{{W_NS}}}cr"}:
            chunks.append("\n")
    return "".join(chunks)


def _unsupported_reason(paragraph, inherited_reason: str | None = None) -> str | None:
    if inherited_reason:
        return inherited_reason
    if not paragraph.xpath(".//w:r/w:t", namespaces=_NS):
        return "paragraph_has_no_editable_text"
    for node in paragraph.iter():
        if node.tag in _READ_ONLY_MARKERS:
            return "paragraph_contains_unsupported_word_feature"
    return None


def _object_record(paragraph, object_id: str, location: dict, *, inherited_reason: str | None = None) -> dict:
    style = paragraph.find(f"{{{W_NS}}}pPr/{{{W_NS}}}pStyle")
    reason = _unsupported_reason(paragraph, inherited_reason)
    return {
        "id": object_id,
        "kind": location["kind"],
        "location": location,
        "text": _paragraph_text(paragraph),
        "paragraph_style": None if style is None else style.get(f"{{{W_NS}}}val"),
        "editable": reason is None,
        "reason": reason,
    }


def _walk_paragraph_container(container, document_hash: str, *, root_path: str,
                              id_scope: str, paragraph_kind: str,
                              location_extra: dict | None = None) -> list[tuple[str, dict, object, str | None]]:
    found: list[tuple[str, dict, object, str | None]] = []

    def visit_container(current, path: str, inherited_reason: str | None = None,
                        current_paragraph_kind: str = paragraph_kind) -> None:
        paragraph_index = 0
        table_index = 0
        for child_index, child in enumerate(current, start=1):
            if child.tag == f"{{{W_NS}}}p":
                paragraph_index += 1
                paragraph_path = f"{path}/p[{paragraph_index}]"
                location = {"kind": current_paragraph_kind, "path": paragraph_path}
                if location_extra:
                    location.update(location_extra)
                found.append((f"docx:{document_hash}:{id_scope}:{paragraph_path}",
                              location, child, inherited_reason))
            elif child.tag == f"{{{W_NS}}}tbl":
                table_index += 1
                visit_table(child, f"{path}/tbl[{table_index}]", inherited_reason)
            elif child.tag == f"{{{W_NS}}}sdt":
                content = child.find(f"{{{W_NS}}}sdtContent")
                if content is not None:
                    visit_container(content, f"{path}/sdt[{child_index}]",
                                    inherited_reason or "paragraph_inside_content_control",
                                    current_paragraph_kind)
            elif child.tag == f"{{{W_NS}}}customXml":
                visit_container(child, f"{path}/customXml[{child_index}]",
                                inherited_reason or "paragraph_inside_custom_xml",
                                current_paragraph_kind)

    def visit_table(table, path: str, inherited_reason: str | None) -> None:
        for row_index, row in enumerate(table.findall(f"{{{W_NS}}}tr"), start=1):
            for cell_index, cell in enumerate(row.findall(f"{{{W_NS}}}tc"), start=1):
                cell_path = f"{path}/row[{row_index}]/cell[{cell_index}]"
                cell_properties = cell.find(f"{{{W_NS}}}tcPr")
                merged = bool(cell_properties is not None and cell_properties.xpath("./w:gridSpan | ./w:vMerge", namespaces=_NS))
                reason = inherited_reason or ("merged_table_cell" if merged else None)
                visit_container(cell, cell_path, reason, "table_cell_paragraph")

    visit_container(container, root_path)
    return found


def _walk_paragraphs(root, document_hash: str) -> list[tuple[str, dict, object, str | None]]:
    body = root.find(f"{{{W_NS}}}body")
    return _walk_paragraph_container(
        body, document_hash, root_path="body", id_scope="paragraph",
        paragraph_kind="paragraph",
    )


def _relationship_part(source_part: str) -> str:
    return posixpath.join(posixpath.dirname(source_part), "_rels",
                          posixpath.basename(source_part) + ".rels")


def _word_image_records(parts: dict[str, bytes], source_part: str,
                        paragraph_records: list[tuple[str, dict, object, str | None]],
                        document_hash: str) -> list[dict]:
    relationship_part = _relationship_part(source_part)
    relationship_payload = parts.get(relationship_part)
    if relationship_payload is None:
        return []
    relationships = _parse_xml(relationship_payload, relationship_part)
    relation_by_id = {
        item.get("Id"): item for item in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        if item.get("Id")
    }
    found = []
    for _paragraph_id, paragraph_location, paragraph, _reason in paragraph_records:
        image_index = 0
        for drawing in paragraph.iter(f"{{{W_NS}}}drawing"):
            blips = list(drawing.iter(f"{{{_DRAWING_NS}}}blip"))
            if not blips:
                continue
            doc_properties = next((item for item in drawing.iter()
                                   if isinstance(item.tag, str) and item.tag.endswith("}docPr")), None)
            transform = next((item for item in drawing.iter()
                              if item.tag == f"{{{_WORD_DRAWING_NS}}}extent"), None)
            source_rectangle = next((item for item in drawing.iter()
                                     if item.tag == f"{{{_DRAWING_NS}}}srcRect"), None)
            drawing_extent = (None if transform is None else
                              {"cx": transform.get("cx"), "cy": transform.get("cy")})
            crop = {} if source_rectangle is None else dict(source_rectangle.attrib)
            for blip in blips:
                image_index += 1
                relationship_id = blip.get(f"{{{_OFFICE_REL_NS}}}embed")
                relation = relation_by_id.get(relationship_id)
                target_part = None
                target_content = None
                internal_image = False
                if (relation is not None and relation.get("Type", "").endswith("/image")
                        and relation.get("TargetMode", "Internal") != "External"
                        and relation.get("Target")):
                    target_part = _resolve_internal_target(source_part, relation.get("Target"))
                    target_content = parts.get(target_part)
                    internal_image = (target_content is not None
                                      and target_part.startswith("word/media/"))
                media = {
                    "sha256": None if target_content is None else _sha256(target_content),
                    "size_bytes": None if target_content is None else len(target_content),
                    "mime_type": (None if target_part is None
                                  else mimetypes.guess_type(target_part)[0]),
                }
                supported_drawing = (len(blips) == 1 and not any(
                    isinstance(node.tag, str)
                    and node.tag.rsplit("}", 1)[-1] == "AlternateContent" for node in drawing.iter()
                ))
                editable = internal_image and supported_drawing
                reason = None if editable else (
                    "external_or_unresolved_image_relationship" if not internal_image
                    else "drawing_uses_alternate_content_or_multiple_blips"
                )
                alt_text = "" if doc_properties is None else (
                    doc_properties.get("descr", "") or doc_properties.get("title", "")
                )
                object_id = (f"docx:{document_hash}:image:{source_part}:"
                             f"{paragraph_location['path']}:"
                             f"image[{image_index}]")
                location = dict(paragraph_location)
                paragraph_kind = location.get("kind")
                location.update({
                    "kind": "image",
                    "paragraph_kind": paragraph_kind,
                    "part": source_part,
                    "paragraph_path": paragraph_location["path"],
                    "image_index": image_index,
                    "relationship_id": relationship_id,
                    "media_part": target_part,
                })
                found.append({
                    "id": object_id,
                    "kind": "image",
                    "location": location,
                    "text": alt_text,
                    "editable": editable,
                    "reason": reason,
                    "edit_modes": ["replace_image"] if editable else [],
                    "media": media,
                    "geometry": drawing_extent,
                    "crop": crop,
                    "element": blip,
                    "drawing": drawing,
                    "paragraph": paragraph,
                    "part": source_part,
                    "relationship_part": relationship_part,
                    "media_part": target_part,
                    "media_content": target_content,
                    "doc_properties": doc_properties,
                })
    return found


def _walk_header_footer_paragraphs(parts: dict[str, bytes], main_part: str,
                                   main_root, document_hash: str,
                                   part_roots: dict[str, object] | None = None
                                   ) -> list[tuple[str, dict, object, str | None, str]]:
    relationship_part = _relationship_part(main_part)
    relationship_payload = parts.get(relationship_part)
    if relationship_payload is None:
        return []
    relationships = _parse_xml(relationship_payload, relationship_part)
    references_by_relationship: dict[str, list[str]] = {}
    section_number = 0
    for section_number, section in enumerate(main_root.iter(f"{{{W_NS}}}sectPr"), start=1):
        for kind in ("header", "footer"):
            for reference in section.findall(f"{{{W_NS}}}{kind}Reference"):
                relationship_id = reference.get(f"{{{_OFFICE_REL_NS}}}id")
                reference_type = reference.get(f"{{{W_NS}}}type", "default")
                if relationship_id:
                    references_by_relationship.setdefault(relationship_id, []).append(
                        f"section[{section_number}].{kind}.{reference_type}",
                    )

    part_uses: dict[tuple[str, str], set[str]] = {}
    for relationship in relationships.findall(f"{{{PKG_REL_NS}}}Relationship"):
        relationship_type = relationship.get("Type", "")
        kind = ("header" if relationship_type.endswith("/header") else
                "footer" if relationship_type.endswith("/footer") else None)
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        if (kind is None or not relationship_id or not target
                or relationship.get("TargetMode", "Internal") == "External"):
            continue
        uses = references_by_relationship.get(relationship_id, [])
        if not uses:
            continue
        target_part = _resolve_internal_target(main_part, target)
        if target_part not in parts:
            _fail("DOCX_HEADER_FOOTER_PART_MISSING")
        part_uses.setdefault((kind, target_part), set()).update(uses)

    found = []
    for (kind, part_name), uses in sorted(part_uses.items()):
        part_root = _parse_xml(parts[part_name], part_name)
        expected_root = f"{{{W_NS}}}{'hdr' if kind == 'header' else 'ftr'}"
        if part_root.tag != expected_root:
            _fail("DOCX_HEADER_FOOTER_PART_INVALID")
        if part_roots is not None:
            part_roots[part_name] = part_root
        records = _walk_paragraph_container(
            part_root, document_hash, root_path=f"{kind}/{part_name}",
            id_scope=kind, paragraph_kind=f"{kind}_paragraph",
            location_extra={"part": part_name, "used_by": sorted(uses)},
        )
        found.extend((object_id, location, paragraph, reason, part_name)
                     for object_id, location, paragraph, reason in records)
    return found


def _walk_note_paragraphs(parts: dict[str, bytes], main_part: str,
                          main_root, document_hash: str,
                          part_roots: dict[str, object] | None = None
                          ) -> list[tuple[str, dict, object, str | None, str]]:
    relationship_part = _relationship_part(main_part)
    relationship_payload = parts.get(relationship_part)
    if relationship_payload is None:
        return []
    relationships = _parse_xml(relationship_payload, relationship_part)
    referenced_paths: dict[tuple[str, str], set[str]] = {}
    for object_id, location, paragraph, _reason in _walk_paragraphs(main_root, document_hash):
        for reference in paragraph.iter():
            if reference.tag == f"{{{W_NS}}}footnoteReference":
                kind = "footnote"
            elif reference.tag == f"{{{W_NS}}}endnoteReference":
                kind = "endnote"
            else:
                continue
            note_id = reference.get(f"{{{W_NS}}}id")
            if note_id is not None:
                referenced_paths.setdefault((kind, note_id), set()).add(location["path"])

    found = []
    for relationship in relationships.findall(f"{{{PKG_REL_NS}}}Relationship"):
        relationship_type = relationship.get("Type", "")
        kind = ("footnote" if relationship_type.endswith("/footnotes") else
                "endnote" if relationship_type.endswith("/endnotes") else None)
        target = relationship.get("Target")
        if (kind is None or not target
                or relationship.get("TargetMode", "Internal") == "External"):
            continue
        part_name = _resolve_internal_target(main_part, target)
        payload = parts.get(part_name)
        if payload is None:
            _fail("DOCX_NOTE_PART_MISSING")
        part_root = _parse_xml(payload, part_name)
        expected_root = f"{{{W_NS}}}{'footnotes' if kind == 'footnote' else 'endnotes'}"
        if part_root.tag != expected_root:
            _fail("DOCX_NOTE_PART_INVALID")
        if part_roots is not None:
            part_roots[part_name] = part_root
        note_tag = f"{{{W_NS}}}{kind}"
        for note in part_root.findall(note_tag):
            note_id = note.get(f"{{{W_NS}}}id")
            if not note_id or note.get(f"{{{W_NS}}}type") in {"separator", "continuationSeparator"}:
                continue
            uses = sorted(referenced_paths.get((kind, note_id), set()))
            location_extra = {"part": part_name, "note_id": note_id, "used_by": uses}
            records = _walk_paragraph_container(
                note, document_hash, root_path=f"{kind}/{part_name}/note[{note_id}]",
                id_scope=kind, paragraph_kind=f"{kind}_paragraph",
                location_extra=location_extra,
            )
            found.extend((object_id, location, paragraph, reason, part_name)
                         for object_id, location, paragraph, reason in records)
    return found


def _walk_comment_paragraphs(parts: dict[str, bytes], main_part: str,
                             main_root, document_hash: str,
                             part_roots: dict[str, object] | None = None
                             ) -> list[tuple[str, dict, object, str | None, str]]:
    relationship_part = _relationship_part(main_part)
    relationship_payload = parts.get(relationship_part)
    if relationship_payload is None:
        return []
    relationships = _parse_xml(relationship_payload, relationship_part)
    comment_part = None
    for relationship in relationships.findall(f"{{{PKG_REL_NS}}}Relationship"):
        if (relationship.get("Type", "").endswith("/comments")
                and relationship.get("TargetMode", "Internal") != "External"
                and relationship.get("Target")):
            comment_part = _resolve_internal_target(main_part, relationship.get("Target"))
            break
    if comment_part is None:
        return []
    payload = parts.get(comment_part)
    if payload is None:
        _fail("DOCX_COMMENT_PART_MISSING")
    comments_root = _parse_xml(payload, comment_part)
    if comments_root.tag != f"{{{W_NS}}}comments":
        _fail("DOCX_COMMENT_PART_INVALID")
    if part_roots is not None:
        part_roots[comment_part] = comments_root

    used_by: dict[str, set[str]] = {}
    for _object_id, location, paragraph, _reason in _walk_paragraphs(main_root, document_hash):
        for marker in paragraph.iter():
            if marker.tag in {f"{{{W_NS}}}commentRangeStart", f"{{{W_NS}}}commentRangeEnd",
                              f"{{{W_NS}}}commentReference"}:
                comment_id = marker.get(f"{{{W_NS}}}id")
                if comment_id is not None:
                    used_by.setdefault(comment_id, set()).add(location["path"])

    found = []
    for comment in comments_root.findall(f"{{{W_NS}}}comment"):
        comment_id = comment.get(f"{{{W_NS}}}id")
        if comment_id is None:
            continue
        location_extra = {
            "part": comment_part,
            "comment_id": comment_id,
            "author": comment.get(f"{{{W_NS}}}author"),
            "date": comment.get(f"{{{W_NS}}}date"),
            "used_by": sorted(used_by.get(comment_id, set())),
        }
        records = _walk_paragraph_container(
            comment, document_hash, root_path=f"comment/{comment_part}/comment[{comment_id}]",
            id_scope="comment", paragraph_kind="comment_paragraph",
            location_extra=location_extra,
        )
        found.extend((object_id, location, paragraph, reason, comment_part)
                     for object_id, location, paragraph, reason in records)
    return found


def _all_paragraph_records(parts: dict[str, bytes], main_part: str, main_root,
                           document_hash: str, part_roots: dict[str, object] | None = None
                           ) -> list[tuple[str, dict, object, str | None, str]]:
    roots = part_roots if part_roots is not None else {}
    records = [
        (object_id, location, paragraph, reason, main_part)
        for object_id, location, paragraph, reason
        in _walk_paragraphs(main_root, document_hash)
    ]
    records.extend(_walk_header_footer_paragraphs(
        parts, main_part, main_root, document_hash, roots,
    ))
    records.extend(_walk_note_paragraphs(
        parts, main_part, main_root, document_hash, roots,
    ))
    records.extend(_walk_comment_paragraphs(
        parts, main_part, main_root, document_hash, roots,
    ))
    return records


def _tracked_change_records(root, main_part: str, document_hash: str) -> list[dict]:
    revision_tags = {
        f"{{{W_NS}}}ins": "insertion",
        f"{{{W_NS}}}del": "deletion",
        f"{{{W_NS}}}moveFrom": "move_from",
        f"{{{W_NS}}}moveTo": "move_to",
    }
    found = []
    for _paragraph_id, paragraph_location, paragraph, _reason in _walk_paragraphs(root, document_hash):
        revision_index = 0
        for node in paragraph.iter():
            change_type = revision_tags.get(node.tag)
            if change_type is None:
                continue
            revision_index += 1
            revision_path = f"{paragraph_location['path']}/revision[{revision_index}]"
            editable = (change_type in {"insertion", "deletion"}
                        and not any(item is not node and item.tag in _TRACKED_CHANGE_UNSAFE_MARKERS
                                    for item in node.iter()))
            found.append({
                "id": f"docx:{document_hash}:revision:{revision_path}",
                "kind": "tracked_change",
                "location": {
                    "kind": "tracked_change",
                    "part": main_part,
                    "path": revision_path,
                    "paragraph_path": paragraph_location["path"],
                    "revision_id": node.get(f"{{{W_NS}}}id"),
                    "change_type": change_type,
                    "author": node.get(f"{{{W_NS}}}author"),
                    "date": node.get(f"{{{W_NS}}}date"),
                },
                "text": "".join(
                    item.text or "" for item in node.iter()
                    if item.tag in {f"{{{W_NS}}}t", f"{{{W_NS}}}delText"}
                ),
                "editable": editable,
                "reason": (None if editable else
                           "move_revisions_require_pair_resolution" if change_type in {"move_from", "move_to"}
                           else "tracked_change_contains_unsupported_word_feature"),
                "edit_modes": ["accept", "reject"] if editable else [],
                "element": node,
                "paragraph": paragraph,
            })
    return found


def _resolve_tracked_change(element, operation: str) -> None:
    parents = {child: parent for parent in element.getroottree().getroot().iter()
               for child in parent}
    parent = parents.get(element)
    if parent is None:
        _fail("DOCX_TRACKED_CHANGE_PARENT_MISSING")
    change_name = element.tag.rsplit("}", 1)[-1]
    if change_name not in {"ins", "del"}:
        _fail("DOCX_TRACKED_CHANGE_KIND_UNSUPPORTED")
    if any(node is not element and node.tag in _TRACKED_CHANGE_UNSAFE_MARKERS
           for node in element.iter()):
        _fail("DOCX_TRACKED_CHANGE_COMPLEX_UNSUPPORTED")

    remove_change = ((change_name == "ins" and operation == "reject")
                    or (change_name == "del" and operation == "accept"))
    if remove_change:
        parent.remove(element)
        return
    if change_name == "del" and operation == "reject":
        for node in element.iter(f"{{{W_NS}}}delText"):
            node.tag = f"{{{W_NS}}}t"
    index = parent.index(element)
    for child in list(element):
        parent.insert(index, child)
        index += 1
    parent.remove(element)


def _replacement_document_image(payload: dict) -> dict:
    if (not isinstance(payload, dict) or not {"content", "filename", "source"} <= payload.keys()
            or set(payload) - {"content", "filename", "source", "alt_text"}):
        _fail("DOCX_IMAGE_REPLACEMENT_INVALID")
    content, filename, source = payload["content"], payload["filename"], payload["source"]
    if not isinstance(content, bytes) or not content or len(content) > MAX_REPLACEMENT_IMAGE_BYTES:
        _fail("DOCX_IMAGE_SIZE_UNSUPPORTED")
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        _fail("DOCX_IMAGE_FILENAME_INVALID")
    if (not isinstance(source, dict) or source.get("kind") != "material"
            or not isinstance(source.get("material_id"), str)
            or not isinstance(source.get("sha256"), str)
            or _sha256(content) != source["sha256"]
            or source.get("size_bytes") != len(content)):
        _fail("DOCX_IMAGE_SOURCE_HASH_MISMATCH")
    alt_text = payload.get("alt_text")
    if (alt_text is not None and (not isinstance(alt_text, str) or len(alt_text) > 2_000
                                  or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", alt_text))):
        _fail("DOCX_IMAGE_ALT_TEXT_INVALID")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_REPLACEMENT_IMAGE_PIXELS:
                _fail("DOCX_IMAGE_DIMENSIONS_UNSUPPORTED")
            if image_format not in {"PNG", "JPEG"}:
                _fail("DOCX_IMAGE_FORMAT_UNSUPPORTED")
            image.verify()
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("DOCX_IMAGE_INVALID") from exc
    extension = "png" if image_format == "PNG" else "jpg"
    return {"content": content, "filename": filename, "source": source,
            "sha256": _sha256(content), "size_bytes": len(content),
            "width": width, "height": height, "extension": extension,
            "mime_type": "image/png" if extension == "png" else "image/jpeg",
            "alt_text": alt_text}


def _document_image_part(parts: dict[str, bytes], image: dict,
                         added_parts: set[str]) -> str:
    for name, payload in parts.items():
        if (name.startswith("word/media/") and name.lower().endswith("." + image["extension"])
                and payload == image["content"]):
            return name
    stem = f"word/media/oevira-{image['sha256'][:24]}"
    candidate = f"{stem}.{image['extension']}"
    suffix = 2
    while candidate in parts and parts[candidate] != image["content"]:
        candidate = f"{stem}-{suffix}.{image['extension']}"
        suffix += 1
    if candidate not in parts:
        parts[candidate] = image["content"]
        added_parts.add(candidate)
    return candidate


def _ensure_document_image_content_type(parts: dict[str, bytes], image_part: str,
                                       image: dict, modified_parts: set[str],
                                       content_types_state: dict) -> None:
    state = content_types_state.get("state")
    if state is None:
        root = _parse_xml(parts["[Content_Types].xml"], "[Content_Types].xml")
        state = {"root": root}
        content_types_state["state"] = state
    root = state["root"]
    extension = image["extension"]
    defaults = [node for node in root.findall(f"{{{CT_NS}}}Default")
                if node.get("Extension", "").casefold() == extension]
    if defaults and any(node.get("ContentType") == image["mime_type"] for node in defaults):
        return
    overrides = [node for node in root.findall(f"{{{CT_NS}}}Override")
                 if node.get("PartName") == "/" + image_part]
    if overrides:
        if overrides[0].get("ContentType") != image["mime_type"]:
            _fail("DOCX_CONTENT_TYPE_CONFLICT")
        return
    if defaults:
        etree.SubElement(root, f"{{{CT_NS}}}Override", {
            "PartName": "/" + image_part, "ContentType": image["mime_type"],
        })
    else:
        etree.SubElement(root, f"{{{CT_NS}}}Default", {
            "Extension": extension, "ContentType": image["mime_type"],
        })
    modified_parts.add("[Content_Types].xml")


def _add_document_image_relationship(parts: dict[str, bytes], main_part: str,
                                    relationship_part: str, image_part: str,
                                    image_hash: str, relationship_state: dict,
                                    modified_parts: set[str]) -> str:
    state = relationship_state.get(relationship_part)
    if state is None:
        payload = parts.get(relationship_part)
        if payload is None:
            _fail("DOCX_IMAGE_RELATIONSHIP_PART_MISSING")
        state = {"root": _parse_xml(payload, relationship_part)}
        relationship_state[relationship_part] = state
    root = state["root"]
    ids = {node.get("Id") for node in root}
    base_id = "rIdOevira" + image_hash[:16]
    relationship_id = base_id
    suffix = 2
    while relationship_id in ids:
        relationship_id = f"{base_id}-{suffix}"
        suffix += 1
    target = posixpath.relpath(image_part, posixpath.dirname(main_part))
    etree.SubElement(root, f"{{{PKG_REL_NS}}}Relationship", {
        "Id": relationship_id,
        "Type": f"{_OFFICE_REL_NS}/image",
        "Target": target,
    })
    modified_parts.add(relationship_part)
    return relationship_id


def _set_document_image_alt(drawing, value: str) -> None:
    for node in drawing.iter():
        if isinstance(node.tag, str) and node.tag.endswith("}docPr"):
            node.set("descr", value)
            return
    _fail("DOCX_IMAGE_ALT_TEXT_UNAVAILABLE")


def _document_image_signature(drawing, *, ignore_alt_text: bool = False) -> bytes:
    clone = copy.deepcopy(drawing)
    for blip in clone.iter(f"{{{_DRAWING_NS}}}blip"):
        blip.attrib.pop(f"{{{_OFFICE_REL_NS}}}embed", None)
        blip.attrib.pop(f"{{{_OFFICE_REL_NS}}}link", None)
    if ignore_alt_text:
        for node in clone.iter():
            if isinstance(node.tag, str) and node.tag.endswith("}docPr"):
                node.attrib.pop("descr", None)
                node.attrib.pop("title", None)
    return _c14n(clone)


def _warnings(parts: dict[str, bytes]) -> list[str]:
    warnings: list[str] = []
    preserved_feature_prefixes = (
        "word/commentsExtended", "word/commentsExtensible", "word/commentsIds",
        "word/people.xml", "word/embeddings", "word/media", "customXml/",
    )
    preserved_features = [name for name in parts if name.startswith(preserved_feature_prefixes)]
    if preserved_features:
        warnings.append("未选图像与嵌入部件保持原样；选定的可编辑图片、基本批注和正文对象可精确修订。")
    if any(name.casefold().startswith("_xmlsignatures/") for name in parts):
        warnings.append("文稿包含数字签名。修改会使签名失效，因此不能保存修改。")
    if any("vbaproject" in name.casefold() or "/activex/" in name.casefold() for name in parts):
        warnings.append("文稿包含宏或 ActiveX 内容；这些内容会保留，但不会运行。")
    if any(name.casefold().startswith("word/embeddings/") for name in parts):
        warnings.append("文稿包含嵌入对象；这些对象会保持原样，不会自动打开。")
    for name, payload in parts.items():
        if not name.endswith(".rels"):
            continue
        try:
            relationships = _parse_xml(payload, name)
        except DocumentError:
            continue
        if any(item.get("TargetMode") == "External" for item in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")):
            warnings.append("文稿含有外部链接；不会访问链接目标，链接保持原样。")
            break
    return warnings


def inspect_document(content: bytes) -> dict:
    """Inspect stable paragraph/table locations without rewriting the DOCX."""
    _infos, parts, _comment, _main_part, root = _read_package(content)
    digest = _sha256(content)
    objects = [
        _object_record(paragraph, object_id, location, inherited_reason=inherited_reason)
        for object_id, location, paragraph, inherited_reason in _walk_paragraphs(root, digest)
    ]
    objects.extend(
        _object_record(paragraph, object_id, location, inherited_reason=inherited_reason)
        for object_id, location, paragraph, inherited_reason, _part_name
        in _walk_header_footer_paragraphs(parts, _main_part, root, digest)
    )
    objects.extend(
        _object_record(paragraph, object_id, location, inherited_reason=inherited_reason)
        for object_id, location, paragraph, inherited_reason, _part_name
        in _walk_note_paragraphs(parts, _main_part, root, digest)
    )
    objects.extend(
        _object_record(paragraph, object_id, location, inherited_reason=inherited_reason)
        for object_id, location, paragraph, inherited_reason, _part_name
        in _walk_comment_paragraphs(parts, _main_part, root, digest)
    )
    objects.extend(
        {key: value for key, value in item.items()
         if key not in {"element", "paragraph"}}
        for item in _tracked_change_records(root, _main_part, digest)
    )
    image_paragraphs: dict[str, list[tuple[str, dict, object, str | None]]] = {}
    for object_id, location, paragraph, reason, part_name in _all_paragraph_records(
            parts, _main_part, root, digest):
        image_paragraphs.setdefault(part_name, []).append(
            (object_id, location, paragraph, reason),
        )
    image_records = []
    for part_name, paragraph_records in image_paragraphs.items():
        image_records.extend(_word_image_records(
            parts, part_name, paragraph_records, digest,
        ))
    objects.extend(
        {key: value for key, value in item.items()
         if key not in {"element", "drawing", "paragraph", "relationship_part",
                        "media_content", "media_part", "doc_properties"}}
        for item in image_records
    )
    return {
        "format": "docx",
        "sha256": digest,
        "size_bytes": len(content),
        "objects": objects,
        "warnings": _warnings(parts),
    }


def _count_occurrences(value: str, needle: str) -> list[int]:
    matches: list[int] = []
    start = 0
    while True:
        position = value.find(needle, start)
        if position < 0:
            return matches
        matches.append(position)
        start = position + 1


def _xml_text_is_valid(value: str) -> bool:
    return all(
        character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
        for character in value
    )


def _set_text(node, value: str) -> None:
    node.text = value
    preserve = value.startswith(" ") or value.endswith(" ")
    if preserve:
        node.set(f"{{{XML_NS}}}space", "preserve")
    else:
        node.attrib.pop(f"{{{XML_NS}}}space", None)


def _replace_span(paragraph, start: int, end: int, replacement: str) -> None:
    nodes = paragraph.xpath(".//w:r/w:t", namespaces=_NS)
    values = [node.text or "" for node in nodes]
    offsets = []
    cursor = 0
    for value in values:
        offsets.append((cursor, cursor + len(value)))
        cursor += len(value)
    if start == end:
        # At a run boundary, inserted text inherits the preceding character's style.
        index = next((i for i, (left, right) in enumerate(offsets)
                      if left < start <= right), None)
        if index is None:
            index = next(i for i, (left, right) in enumerate(offsets) if left <= start < right)
        offset = start - offsets[index][0]
        _set_text(nodes[index], values[index][:offset] + replacement + values[index][offset:])
        return
    first = next(i for i, (left, right) in enumerate(offsets) if left <= start < right)
    last = next(i for i, (left, right) in enumerate(offsets) if left < end <= right)
    prefix = values[first][:start - offsets[first][0]]
    suffix = values[last][end - offsets[last][0]:]
    if first == last:
        _set_text(nodes[first], prefix + replacement + suffix)
    else:
        _set_text(nodes[first], prefix + replacement)
        for index in range(first + 1, last):
            _set_text(nodes[index], "")
        _set_text(nodes[last], suffix)


def _apply_exact_change(paragraph, before: str, after: str) -> str:
    joined = "".join(node.text or "" for node in paragraph.xpath(".//w:r/w:t", namespaces=_NS))
    matches = _count_occurrences(joined, before)
    if len(matches) != 1:
        _fail("DOCX_TEXT_MATCH_AMBIGUOUS" if matches else "DOCX_TEXT_MATCH_NOT_FOUND")
    start = matches[0]
    # Clients can submit the entire paragraph. Preserve every matching text span
    # in its original run instead of moving the whole paragraph into the first run.
    changes = SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    for tag, left, right, new_left, new_right in reversed(changes):
        if tag != "equal":
            _replace_span(paragraph, start + left, start + right, after[new_left:new_right])
    return joined[:start] + after + joined[start + len(before):]


def _c14n(element) -> bytes:
    return etree.tostring(element, method="c14n", exclusive=True, with_comments=True)


def _run_style_signature(paragraph) -> tuple[bytes, ...]:
    return tuple(
        _c14n(properties) if properties is not None else b""
        for properties in (run.find(f"{{{W_NS}}}rPr") for run in paragraph.xpath(".//w:r", namespaces=_NS))
    )


def _replace_package_xml(infos: list[zipfile.ZipInfo], parts: dict[str, bytes],
                         comment: bytes, changed_roots: dict[str, object],
                         added_parts: set[str] | None = None) -> bytes:
    updated_parts = dict(parts)
    for part_name, changed_root in changed_roots.items():
        updated_parts[part_name] = etree.tostring(
            changed_root, encoding="UTF-8", xml_declaration=True, standalone=True,
        )
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            archive.comment = comment
            for info in infos:
                archive.writestr(info, updated_parts[info.filename])
            for part_name in sorted(added_parts or set()):
                info = zipfile.ZipInfo(part_name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, updated_parts[part_name])
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise DocumentError("DOCX_PACKAGE_WRITE_FAILED") from exc
    return output.getvalue()


def revise_document(content: bytes, changes: list[dict]) -> dict:
    """Revise exact Word text ranges and selected insertion/deletion revisions."""
    infos, parts, comment, main_part, root = _read_package(content)
    original_parts = dict(parts)
    if any(name.casefold().startswith("_xmlsignatures/") for name in parts):
        _fail("DOCX_SIGNATURE_PRESENT")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        _fail("DOCX_CHANGES_INVALID")

    source_hash = _sha256(content)
    support_roots: dict[str, object] = {}
    body_records = [(object_id, location, paragraph, reason, main_part)
                    for object_id, location, paragraph, reason
                    in _walk_paragraphs(root, source_hash)]
    support_records = _walk_header_footer_paragraphs(
        parts, main_part, root, source_hash, support_roots,
    )
    note_records = _walk_note_paragraphs(parts, main_part, root, source_hash, support_roots)
    comment_records = _walk_comment_paragraphs(parts, main_part, root, source_hash, support_roots)
    original_records = body_records + support_records + note_records + comment_records
    original_by_id = {record[0]: record[2] for record in original_records}
    record_by_id = {record[0]: record for record in original_records}
    tracked_records = _tracked_change_records(root, main_part, source_hash)
    tracked_by_id = {item["id"]: item for item in tracked_records}
    image_paragraphs: dict[str, list[tuple[str, dict, object, str | None]]] = {}
    for object_id, location, paragraph, reason, part_name in original_records:
        image_paragraphs.setdefault(part_name, []).append(
            (object_id, location, paragraph, reason),
        )
    image_records = []
    for part_name, paragraph_records in image_paragraphs.items():
        image_records.extend(_word_image_records(
            parts, part_name, paragraph_records, source_hash,
        ))
    images_by_id = {item["id"]: item for item in image_records}
    part_roots = {main_part: root, **support_roots}
    object_ids: set[str] = set()
    normalized: list[dict] = []
    tracked_paragraph_keys: set[tuple[str, str]] = set()
    text_paragraph_keys: set[tuple[str, str]] = set()
    image_paragraph_keys: set[tuple[str, str]] = set()
    for item in changes:
        image_change = isinstance(item, dict) and set(item) == {"object_id", "replace_image"}
        tracked_operation = isinstance(item, dict) and set(item) == {"object_id", "operation"}
        text_change = isinstance(item, dict) and set(item) == {"object_id", "before", "after"}
        if not (image_change or tracked_operation or text_change):
            _fail("DOCX_CHANGE_INVALID")
        object_id = item["object_id"]
        if not isinstance(object_id, str) or not object_id or len(object_id) > 500:
            _fail("DOCX_OBJECT_ID_INVALID")
        if object_id in object_ids:
            _fail("DOCX_OBJECT_DUPLICATED")
        object_ids.add(object_id)
        if image_change:
            image_record = images_by_id.get(object_id)
            if image_record is None:
                _fail("DOCX_OBJECT_NOT_FOUND")
            if not image_record["editable"]:
                _fail("DOCX_OBJECT_NOT_EDITABLE")
            image = _replacement_document_image(item["replace_image"])
            new_alt_text = (image["alt_text"] if image["alt_text"] is not None
                            else image_record["text"])
            if (image["sha256"] == image_record["media"]["sha256"]
                    and new_alt_text == image_record["text"]):
                _fail("DOCX_NO_CHANGES")
            location = image_record["location"]
            path_key = (image_record["part"], location["paragraph_path"])
            if path_key in image_paragraph_keys:
                _fail("DOCX_IMAGE_OBJECTS_OVERLAP")
            image_paragraph_keys.add(path_key)
            normalized.append({
                "kind": "image_replacement",
                "object_id": object_id,
                "part": image_record["part"],
                "location": location,
                "image_record": image_record,
                "image": image,
                "new_alt_text": new_alt_text,
                "paragraph": image_record["paragraph"],
            })
            continue
        if tracked_operation:
            operation = item["operation"]
            if not isinstance(operation, str) or operation not in {"accept", "reject"}:
                _fail("DOCX_TRACKED_CHANGE_OPERATION_INVALID")
            tracked = tracked_by_id.get(object_id)
            if tracked is None:
                _fail("DOCX_OBJECT_NOT_FOUND")
            if not tracked["editable"]:
                _fail("DOCX_TRACKED_CHANGE_KIND_UNSUPPORTED")
            location = tracked["location"]
            path_key = (main_part, location["paragraph_path"])
            if path_key in tracked_paragraph_keys:
                _fail("DOCX_TRACKED_CHANGE_OBJECTS_OVERLAP")
            tracked_paragraph_keys.add(path_key)
            normalized.append({
                "kind": "tracked_change",
                "object_id": object_id,
                "operation": operation,
                "part": main_part,
                "location": location,
                "revision": tracked["element"],
                "paragraph": tracked["paragraph"],
                "before_revision_text": tracked["text"],
            })
            continue

        before, after = item["before"], item["after"]
        if not isinstance(before, str) or not before or len(before) > MAX_CHANGE_TEXT_CHARS:
            _fail("DOCX_BEFORE_TEXT_INVALID")
        if (not isinstance(after, str) or len(after) > MAX_CHANGE_TEXT_CHARS
                or any(char in after for char in "\r\n\t") or not _xml_text_is_valid(after)):
            _fail("DOCX_AFTER_TEXT_INVALID")
        paragraph = original_by_id.get(object_id)
        if paragraph is None:
            _fail("DOCX_OBJECT_NOT_FOUND")
        record = record_by_id[object_id]
        if _unsupported_reason(paragraph, record[3]) is not None:
            _fail("DOCX_OBJECT_NOT_EDITABLE")
        if before == after:
            _fail("DOCX_NO_CHANGES")
        path_key = (record[4], record[1]["path"])
        text_paragraph_keys.add(path_key)
        normalized.append({"kind": "text", "object_id": object_id, "before": before, "after": after,
                           "part": record[4], "location": record[1], "paragraph": paragraph})
    if ((tracked_paragraph_keys & text_paragraph_keys)
            or (image_paragraph_keys & text_paragraph_keys)
            or (image_paragraph_keys & tracked_paragraph_keys)):
        _fail("DOCX_OBJECTS_OVERLAP")

    before_style = {object_id: _c14n(paragraph.find(f"{{{W_NS}}}pPr"))
                    if paragraph.find(f"{{{W_NS}}}pPr") is not None else b""
                    for object_id, paragraph in original_by_id.items()}
    before_run_styles = {object_id: _run_style_signature(paragraph)
                         for object_id, paragraph in original_by_id.items()}
    before_text = {object_id: _paragraph_text(paragraph) for object_id, paragraph in original_by_id.items()}
    change_records: list[dict] = []
    expected_changed_paragraphs: dict[tuple[str, str], bytes] = {}
    modified_parts = {item["part"] for item in normalized}
    added_parts: set[str] = set()
    relationship_state: dict[str, dict] = {}
    content_types_state: dict[str, dict] = {}
    image_after: dict[str, dict] = {}
    for item in normalized:
        paragraph = item["paragraph"]
        old_paragraph_text = _paragraph_text(paragraph)
        if item["kind"] == "image_replacement":
            image = item["image"]
            image_part = _document_image_part(parts, image, added_parts)
            _ensure_document_image_content_type(
                parts, image_part, image, modified_parts, content_types_state,
            )
            relationship_part = item["image_record"]["relationship_part"]
            relationship_id = _add_document_image_relationship(
                parts, item["part"], relationship_part, image_part, image["sha256"],
                relationship_state, modified_parts,
            )
            blip = item["image_record"]["element"]
            blip.set(f"{{{_OFFICE_REL_NS}}}embed", relationship_id)
            blip.attrib.pop(f"{{{_OFFICE_REL_NS}}}link", None)
            if image["alt_text"] is not None:
                _set_document_image_alt(item["image_record"]["drawing"], image["alt_text"])
            paragraph_key = (item["part"], item["location"]["paragraph_path"])
            expected_changed_paragraphs[paragraph_key] = _c14n(paragraph)
            image_after[item["object_id"]] = {
                "image": image,
                "media_part": image_part,
                "relationship_id": relationship_id,
                "new_alt_text": item["new_alt_text"],
                "structure_signature": _document_image_signature(
                    item["image_record"]["drawing"],
                    ignore_alt_text=image["alt_text"] is not None,
                ),
            }
            change_records.append({
                "kind": "image_replacement",
                "object_id": item["object_id"],
                "before": {
                    "sha256": item["image_record"]["media"]["sha256"],
                    "size_bytes": item["image_record"]["media"]["size_bytes"],
                },
                "after": {
                    "source": image["source"],
                    "sha256": image["sha256"],
                    "size_bytes": image["size_bytes"],
                    "mime_type": image["mime_type"],
                    "dimensions": [image["width"], image["height"]],
                },
                "part": item["part"],
                "location": item["location"],
                "alt_text": item["new_alt_text"],
                "alt_text_updated": image["alt_text"] is not None,
                "geometry_preserved": True,
                "crop_preserved": True,
                "copy_on_write": True,
            })
            continue
        if item["kind"] == "tracked_change":
            _resolve_tracked_change(item["revision"], item["operation"])
            new_paragraph_text = _paragraph_text(paragraph)
            paragraph_key = (item["part"], item["location"]["paragraph_path"])
            expected_changed_paragraphs[paragraph_key] = _c14n(paragraph)
            change_records.append({
                "kind": "tracked_change",
                "object_id": item["object_id"],
                "operation": item["operation"],
                "before": item["before_revision_text"],
                "after": new_paragraph_text,
                "before_paragraph": old_paragraph_text,
                "after_paragraph": new_paragraph_text,
                "part": item["part"],
                "location": item["location"],
            })
            continue
        expected = _apply_exact_change(paragraph, item["before"], item["after"])
        new_paragraph_text = _paragraph_text(paragraph)
        if new_paragraph_text != expected:
            _fail("DOCX_CHANGE_READBACK_FAILED")
        change_records.append({
            "object_id": item["object_id"],
            "before": item["before"],
            "after": item["after"],
            "before_paragraph": old_paragraph_text,
            "after_paragraph": new_paragraph_text,
            "part": item["part"],
            "location": item["location"],
            "formatting": "替换文字沿用所选文字起始处的格式",
        })

    changed_roots = {part_name: part_roots[part_name] for part_name in modified_parts
                     if part_name in part_roots}
    for relationship_part, state in relationship_state.items():
        changed_roots[relationship_part] = state["root"]
    if "state" in content_types_state:
        changed_roots["[Content_Types].xml"] = content_types_state["state"]["root"]
    new_content = _replace_package_xml(infos, parts, comment, changed_roots, added_parts)
    if len(new_content) > MAX_INPUT_BYTES:
        _fail("DOCX_OUTPUT_TOO_LARGE")
    _new_infos, new_parts, _new_comment, _new_main, reopened_root = _read_package(new_content)
    if set(new_parts) != set(parts):
        _fail("DOCX_PACKAGE_MEMBERS_CHANGED")
    for name, original_payload in original_parts.items():
        if name not in modified_parts and new_parts[name] != original_payload:
            _fail("DOCX_UNSELECTED_MEMBER_CHANGED")
    for name in added_parts:
        if new_parts.get(name) != parts[name]:
            _fail("DOCX_IMAGE_REOPEN_MISMATCH")

    after_hash = _sha256(new_content)
    after_support_roots: dict[str, object] = {}
    after_records = [
        (object_id, location, paragraph, reason, main_part)
        for object_id, location, paragraph, reason in _walk_paragraphs(reopened_root, after_hash)
    ]
    after_records.extend(_walk_header_footer_paragraphs(
        new_parts, _new_main, reopened_root, after_hash, after_support_roots,
    ))
    after_records.extend(_walk_note_paragraphs(
        new_parts, _new_main, reopened_root, after_hash, after_support_roots,
    ))
    after_records.extend(_walk_comment_paragraphs(
        new_parts, _new_main, reopened_root, after_hash, after_support_roots,
    ))
    after_image_paragraphs: dict[str, list[tuple[str, dict, object, str | None]]] = {}
    for object_id, location, paragraph, reason, part_name in after_records:
        after_image_paragraphs.setdefault(part_name, []).append(
            (object_id, location, paragraph, reason),
        )
    after_image_records = []
    for part_name, paragraph_records in after_image_paragraphs.items():
        after_image_records.extend(_word_image_records(
            new_parts, part_name, paragraph_records, after_hash,
        ))
    after_images = {
        (item["part"], item["location"]["paragraph_path"],
         item["location"]["image_index"]): item
        for item in after_image_records
    }
    for object_id, expected in image_after.items():
        location = next(item["location"] for item in normalized if item["object_id"] == object_id)
        current = after_images.get((location["part"], location["paragraph_path"],
                                    location["image_index"]))
        image = expected["image"]
        if (current is None or not current["editable"]
                or current["media"].get("sha256") != image["sha256"]
                or current.get("geometry") != next(
                    item["image_record"].get("geometry")
                    for item in normalized if item["object_id"] == object_id
                )
                or current.get("crop") != next(
                    item["image_record"].get("crop")
                    for item in normalized if item["object_id"] == object_id
                )
                or current["text"] != expected["new_alt_text"]
                or _document_image_signature(
                    current["drawing"], ignore_alt_text=image["alt_text"] is not None,
                ) != expected["structure_signature"]
                or new_parts.get(expected["media_part"]) != image["content"]):
            _fail("DOCX_IMAGE_REOPEN_MISMATCH")
    after_by_path = {
        (part_name, location["path"]): paragraph
        for _object_id, location, paragraph, _reason, part_name in after_records
    }
    for object_id, paragraph in original_by_id.items():
        record = record_by_id[object_id]
        paragraph_key = (record[4], record[1]["path"])
        reopened = after_by_path.get(paragraph_key)
        if reopened is None:
            _fail("DOCX_OBJECT_LOCATION_CHANGED")
        if paragraph_key in expected_changed_paragraphs:
            if _c14n(reopened) != expected_changed_paragraphs[paragraph_key]:
                _fail("DOCX_SELECTED_PARAGRAPH_READBACK_FAILED")
            style = reopened.find(f"{{{W_NS}}}pPr")
            style_bytes = _c14n(style) if style is not None else b""
            if style_bytes != before_style[object_id]:
                _fail("DOCX_SELECTED_PARAGRAPH_STYLE_CHANGED")
            continue
        if object_id not in object_ids and _c14n(paragraph) != _c14n(reopened):
            _fail("DOCX_UNSELECTED_OBJECT_CHANGED")
        if object_id in object_ids:
            style = reopened.find(f"{{{W_NS}}}pPr")
            style_bytes = _c14n(style) if style is not None else b""
            if style_bytes != before_style[object_id] or _run_style_signature(reopened) != before_run_styles[object_id]:
                _fail("DOCX_SELECTED_PARAGRAPH_STYLE_CHANGED")
        elif _paragraph_text(reopened) != before_text[object_id]:
            _fail("DOCX_UNSELECTED_TEXT_CHANGED")

    result_manifest = inspect_document(new_content)
    return {
        "content": new_content,
        "sha256": _sha256(new_content),
        "size_bytes": len(new_content),
        "changes": change_records,
        "objects": result_manifest["objects"],
        "warnings": result_manifest["warnings"],
        "unchanged_verified": True,
        "changed_member": next(iter(modified_parts)) if len(modified_parts) == 1 else None,
        "changed_members": sorted(modified_parts),
        "added_parts": sorted(added_parts),
        "preserved_member_count": len(original_parts) - len(modified_parts),
    }
