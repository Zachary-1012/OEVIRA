"""Original-backed PDF text anchors and non-destructive review annotations."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import re


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PAGES = 500
MAX_CHANGES = 100
MAX_QUOTE_CHARS = 8_000
MAX_COMMENT_CHARS = 8_000
MAX_ANCHOR_CHARS = 8_000
PDFIUM_INCREMENTAL_SAVE_FLAG = 1  # PDFium public fpdf_save.h: FPDF_INCREMENTAL.
_SIGNED_PDF = re.compile(rb"/ByteRange\s*\[", re.IGNORECASE)
_ENCRYPTED_PDF = re.compile(rb"/Encrypt(?:\s|/|<)", re.IGNORECASE)
_PDF_ID = re.compile(rb"(/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<)([0-9A-Fa-f]+)(>\s*\])")


class PdfRevisionError(ValueError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _fail(code: str, **details) -> None:
    raise PdfRevisionError(code, details)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _pdfium():
    try:
        import pypdfium2 as pdfium
        from pypdfium2 import raw
    except ImportError as exc:
        raise PdfRevisionError("PDF_RUNTIME_UNAVAILABLE") from exc
    return pdfium, raw


def _open_pdf(content: bytes):
    if not isinstance(content, bytes):
        _fail("PDF_CONTENT_MUST_BE_BYTES")
    if len(content) > MAX_INPUT_BYTES:
        _fail("PDF_INPUT_TOO_LARGE")
    if not content.startswith(b"%PDF-"):
        _fail("PDF_HEADER_INVALID")
    if _ENCRYPTED_PDF.search(content):
        _fail("PDF_ENCRYPTED_UNSUPPORTED")
    pdfium, _raw = _pdfium()
    try:
        document = pdfium.PdfDocument(content)
    except Exception as exc:
        message = str(exc).casefold()
        code = "PDF_PASSWORD_REQUIRED" if "password" in message or "encrypt" in message else "PDF_PACKAGE_INVALID"
        raise PdfRevisionError(code) from exc
    if len(document) < 1 or len(document) > MAX_PAGES:
        document.close()
        _fail("PDF_PAGE_COUNT_UNSUPPORTED", limit=MAX_PAGES)
    return document


def _signature_state(document, raw, content: bytes, *, strict: bool) -> dict:
    try:
        signature_count = int(raw.FPDF_GetSignatureCount(document.raw))
    except Exception as exc:
        if strict and _SIGNED_PDF.search(content):
            raise PdfRevisionError("PDF_SIGNATURE_READ_UNAVAILABLE") from exc
        return {"state": "unverified", "count": 0, "signatures": [],
                "cryptographic_validation": "not_performed"}
    if signature_count < 0 or signature_count > 100:
        _fail("PDF_SIGNATURE_COUNT_UNSUPPORTED")
    if signature_count == 0:
        if _SIGNED_PDF.search(content):
            if strict:
                _fail("PDF_SIGNATURE_READ_UNAVAILABLE")
            return {"state": "signature_marker_unverified", "count": 0, "signatures": [],
                    "cryptographic_validation": "not_performed"}
        return {"state": "unsigned", "count": 0, "signatures": [],
                "cryptographic_validation": "not_applicable"}

    signatures = []
    permissions = []
    for index in range(signature_count):
        signature = raw.FPDF_GetSignatureObject(document.raw, index)
        if not signature:
            if strict:
                _fail("PDF_SIGNATURE_READ_UNAVAILABLE", index=index)
            signatures.append({"index": index, "docmdp_permission": None, "byte_range": None,
                               "byte_range_valid": False})
            continue
        permission = int(raw.FPDFSignatureObj_GetDocMDPPermission(signature))
        permission = permission if permission in {0, 1, 2, 3} else None
        required = int(raw.FPDFSignatureObj_GetByteRange(signature, None, 0))
        byte_range = None
        valid_range = False
        if 4 <= required <= 64:
            values = (ctypes.c_long * required)()
            observed = int(raw.FPDFSignatureObj_GetByteRange(signature, values, required))
            if observed == required:
                byte_range = [int(values[position]) for position in range(required)]
                if required == 4:
                    first_offset, first_length, second_offset, second_length = byte_range
                    valid_range = (first_offset == 0 and first_length > 0
                                   and second_offset >= first_length and second_length > 0
                                   and second_offset + second_length <= len(content))
        signature_record = {"index": index, "docmdp_permission": permission,
                            "byte_range": byte_range, "byte_range_valid": valid_range}
        signatures.append(signature_record)
        permissions.append(permission)
        if strict and (permission is None or not valid_range):
            _fail("PDF_SIGNATURE_PERMISSIONS_UNVERIFIED", index=index)
        if strict and permission in {1, 2}:
            _fail("PDF_DOCMDP_CHANGE_FORBIDDEN", index=index, permission=permission)

    if any(permission in {1, 2} for permission in permissions):
        state = "docmdp_change_forbidden"
    elif any(permission == 3 for permission in permissions):
        state = "docmdp_annotations_allowed"
    else:
        state = "signed_without_docmdp"
    return {"state": state, "count": signature_count, "signatures": signatures,
            "cryptographic_validation": "not_performed"}


def _read_wide_string(raw, annotation, key: str) -> str:
    key_buffer = ctypes.create_string_buffer(key.encode("ascii") + b"\0")
    required_bytes = raw.FPDFAnnot_GetStringValue(annotation, key_buffer, None, 0)
    if required_bytes <= 2 or required_bytes > 131_072:
        return ""
    buffer = (ctypes.c_ushort * ((required_bytes + 1) // 2))()
    buffer_bytes = ctypes.sizeof(buffer)
    read_bytes = raw.FPDFAnnot_GetStringValue(annotation, key_buffer, buffer, buffer_bytes)
    if read_bytes <= 2 or read_bytes > buffer_bytes:
        return ""
    return bytes(memoryview(buffer))[:read_bytes].decode("utf-16-le", errors="replace").rstrip("\0")


def _annotation_snapshot(raw, page) -> list[dict]:
    count = raw.FPDFPage_GetAnnotCount(page.raw)
    if count < 0 or count > 10_000:
        _fail("PDF_ANNOTATION_COUNT_UNSUPPORTED")
    result = []
    for index in range(count):
        annotation = raw.FPDFPage_GetAnnot(page.raw, index)
        if not annotation:
            _fail("PDF_ANNOTATION_READ_FAILED", index=index)
        try:
            rect = raw.FS_RECTF()
            rect_value = None
            if raw.FPDFAnnot_GetRect(annotation, ctypes.byref(rect)):
                rect_value = [float(rect.left), float(rect.bottom), float(rect.right), float(rect.top)]
            result.append({
                "subtype": int(raw.FPDFAnnot_GetSubtype(annotation)),
                "contents": _read_wide_string(raw, annotation, "Contents"),
                "author": _read_wide_string(raw, annotation, "T"),
                "subject": _read_wide_string(raw, annotation, "Subj"),
                "rect": rect_value,
            })
        finally:
            raw.FPDFPage_CloseAnnot(annotation)
    return result


def inspect_pdf(content: bytes) -> dict:
    document = _open_pdf(content)
    pdfium, raw = _pdfium()
    pages = []
    signature_status = {}
    try:
        for page_number in range(1, len(document) + 1):
            page = document.get_page(page_number - 1)
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
                pages.append({
                    "page": page_number,
                    "width": float(page.get_width()),
                    "height": float(page.get_height()),
                    "bbox": [float(value) for value in page.get_bbox()],
                    "mediabox": [float(value) for value in page.get_mediabox()],
                    "cropbox": [float(value) for value in page.get_cropbox()],
                    "rotation": int(page.get_rotation()),
                    "coordinate_space": "pdf-user-space-points-bottom-left",
                    "text": text,
                    "text_available": bool(text.strip()),
                    "annotations": _annotation_snapshot(raw, page),
                })
            finally:
                page.close()
        signature_status = _signature_state(document, raw, content, strict=False)
    except PdfRevisionError:
        raise
    except Exception as exc:
        raise PdfRevisionError("PDF_PAGE_READ_FAILED", {"reason": type(exc).__name__}) from exc
    finally:
        document.close()
    return {"sha256": _hash(content), "size_bytes": len(content), "page_count": len(pages),
            "pages": pages, "signature_status": signature_status}


def _matches(textpage, quote: str) -> list[tuple[int, int]]:
    searcher = textpage.search(quote, match_case=True, match_whole_word=False, consecutive=True)
    matches = []
    try:
        while True:
            match = searcher.get_next()
            if match is None:
                break
            matches.append((int(match[0]), int(match[1])))
            if len(matches) > 1_000:
                _fail("PDF_ANCHOR_MATCH_COUNT_UNSUPPORTED")
    finally:
        searcher.close()
    return matches


def _line_rectangles(textpage, start: int, count: int) -> list[tuple[float, float, float, float]]:
    if count < 1 or count > MAX_ANCHOR_CHARS:
        _fail("PDF_ANCHOR_LENGTH_UNSUPPORTED", maximum=MAX_ANCHOR_CHARS)
    characters = []
    for index in range(start, start + count):
        try:
            left, bottom, right, top = textpage.get_charbox(index)
        except Exception as exc:
            raise PdfRevisionError("PDF_ANCHOR_COORDINATE_UNAVAILABLE") from exc
        if right > left and top > bottom:
            characters.append((float(left), float(bottom), float(right), float(top)))
    if not characters:
        _fail("PDF_ANCHOR_COORDINATE_UNAVAILABLE")

    lines: list[list[tuple[float, float, float, float]]] = []
    for character in characters:
        if not lines:
            lines.append([character])
            continue
        previous = lines[-1][-1]
        previous_mid = (previous[1] + previous[3]) / 2
        current_mid = (character[1] + character[3]) / 2
        tolerance = max(previous[3] - previous[1], character[3] - character[1]) * 0.35
        if abs(previous_mid - current_mid) <= max(tolerance, 0.5):
            lines[-1].append(character)
        else:
            lines.append([character])
    return [
        (min(box[0] for box in line), min(box[1] for box in line),
         max(box[2] for box in line), max(box[3] for box in line))
        for line in lines
    ]


def _annotation_id(source_hash: str, page: int, quote: str, occurrence: int,
                   comment: str, author: str, timestamp: str) -> str:
    payload = json.dumps([source_hash, page, quote, occurrence, comment, author, timestamp],
                         ensure_ascii=False, separators=(",", ":"))
    return "pdf:" + source_hash + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _validate_change(change, index: int) -> dict:
    allowed = {"page", "quote", "comment", "occurrence", "rect", "quads"}
    if not isinstance(change, dict) or set(change) - allowed or not {"page", "comment"} <= change.keys():
        _fail("PDF_CHANGE_INVALID", index=index)
    page = change["page"]
    comment = change["comment"]
    occurrence = change.get("occurrence")
    has_quote = "quote" in change
    has_rect = "rect" in change
    has_quads = "quads" in change
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        _fail("PDF_PAGE_NUMBER_INVALID", index=index)
    if not isinstance(comment, str) or not comment.strip() or len(comment) > MAX_COMMENT_CHARS:
        _fail("PDF_COMMENT_INVALID", index=index)
    if has_quote == (has_rect or has_quads) or has_rect and has_quads:
        _fail("PDF_ANCHOR_KIND_INVALID", index=index)
    quote = change.get("quote")
    if has_quote and (not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS):
        _fail("PDF_QUOTE_INVALID", index=index)
    if has_quote and any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in quote + comment):
        _fail("PDF_TEXT_INVALID", index=index)
    if has_quote and occurrence is not None and (
            isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0):
        _fail("PDF_OCCURRENCE_INVALID", index=index)
    if not has_quote and occurrence is not None:
        _fail("PDF_OCCURRENCE_INVALID", index=index)
    if has_rect:
        rect = _validated_rect(change["rect"], index)
        return {"page": page, "rect": rect, "comment": comment}
    if has_quads:
        quads = _validated_quads(change["quads"], index)
        return {"page": page, "quads": quads, "comment": comment}
    return {"page": page, "quote": quote, "comment": comment, "occurrence": occurrence}


def _coordinate(value, *, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail("PDF_ANCHOR_COORDINATE_INVALID", index=index)
    return float(value)


def _validated_rect(value, index: int) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        _fail("PDF_RECT_INVALID", index=index)
    left, bottom, right, top = [_coordinate(part, index=index) for part in value]
    if right <= left or top <= bottom:
        _fail("PDF_RECT_INVALID", index=index)
    return [left, bottom, right, top]


def _validated_quads(value, index: int) -> list[list[float]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        _fail("PDF_QUADS_INVALID", index=index)
    result = []
    for quad in value:
        if not isinstance(quad, list) or len(quad) != 8:
            _fail("PDF_QUAD_INVALID", index=index)
        coordinates = [_coordinate(part, index=index) for part in quad]
        if (max(coordinates[::2]) - min(coordinates[::2]) <= 0
                or max(coordinates[1::2]) - min(coordinates[1::2]) <= 0):
            _fail("PDF_QUAD_INVALID", index=index)
        result.append(coordinates)
    return result


def _rect_within_bbox(rectangle: list[float], bbox: tuple[float, float, float, float], *, index: int) -> None:
    left, bottom, right, top = rectangle
    page_left, page_bottom, page_right, page_top = bbox
    epsilon = 0.05
    if (left < page_left - epsilon or bottom < page_bottom - epsilon
            or right > page_right + epsilon or top > page_top + epsilon):
        _fail("PDF_ANCHOR_OUTSIDE_VISIBLE_PAGE", index=index)


def _quads_for_rectangles(rectangles: list[tuple[float, float, float, float]]) -> list[list[float]]:
    result = []
    for left, bottom, right, top in rectangles:
        # PDFium uses top-left, top-right, bottom-left, bottom-right in PDF
        # user-space points (the page rotation remains on the source page).
        result.append([left, top, right, top, left, bottom, right, bottom])
    return result


def _wide_value(value: str):
    encoded = value.encode("utf-16-le") + b"\0\0"
    return (ctypes.c_ushort * (len(encoded) // 2)).from_buffer_copy(encoded)


def _set_annotation_string(raw, annotation, key: str, value: str) -> None:
    key_buffer = ctypes.create_string_buffer(key.encode("ascii") + b"\0")
    wide = _wide_value(value)
    if not raw.FPDFAnnot_SetStringValue(annotation, key_buffer,
                                        ctypes.cast(wide, ctypes.POINTER(ctypes.c_ushort))):
        _fail("PDF_ANNOTATION_WRITE_FAILED", key=key)


def _normalize_output_id(content: bytes, source_hash: str, changes: list[dict]) -> bytes:
    payload = json.dumps([source_hash, changes], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    stable_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32].upper().encode("ascii")
    matches = list(_PDF_ID.finditer(content))
    if matches:
        match = matches[-1]
        return content[:match.start(3)] + stable_id + content[match.end(3):]
    trailer_start = content.rfind(b"trailer")
    trailer_end = content.find(b">>", trailer_start)
    if trailer_start < 0 or trailer_end < 0:
        _fail("PDF_OUTPUT_ID_UNAVAILABLE")
    original_id = hashlib.sha256(content[:trailer_start]).hexdigest()[:32].upper().encode("ascii")
    addition = b" /ID[<" + original_id + b"><" + stable_id + b">]"
    return content[:trailer_end] + addition + content[trailer_end:]


def _create_highlight(raw, page, quads: list[list[float]],
                      change: dict, author: str) -> None:
    annotation = raw.FPDFPage_CreateAnnot(page.raw, raw.FPDF_ANNOT_HIGHLIGHT)
    if not annotation:
        _fail("PDF_ANNOTATION_WRITE_FAILED", page=change["page"])
    try:
        all_x = [quad[index] for quad in quads for index in (0, 2, 4, 6)]
        all_y = [quad[index] for quad in quads for index in (1, 3, 5, 7)]
        left, bottom, right, top = min(all_x), min(all_y), max(all_x), max(all_y)
        bounds = raw.FS_RECTF(left, top, right, bottom)
        if not raw.FPDFAnnot_SetRect(annotation, ctypes.byref(bounds)):
            _fail("PDF_ANNOTATION_WRITE_FAILED", page=change["page"])
        for points in quads:
            quad = raw.FS_QUADPOINTSF(*points)
            if not raw.FPDFAnnot_AppendAttachmentPoints(annotation, ctypes.byref(quad)):
                _fail("PDF_ANNOTATION_GEOMETRY_UNSUPPORTED", page=change["page"])
        if not raw.FPDFAnnot_SetColor(annotation, raw.FPDFANNOT_COLORTYPE_Color, 255, 231, 0, 110):
            _fail("PDF_ANNOTATION_COLOR_UNSUPPORTED", page=change["page"])
        _set_annotation_string(raw, annotation, "Contents", change["comment"])
        _set_annotation_string(raw, annotation, "T", author)
        _set_annotation_string(raw, annotation, "Subj", "修订意见")
        _set_annotation_string(raw, annotation, "NM", change["object_id"])
        _set_annotation_string(raw, annotation, "M", change["annotated_at"])
    finally:
        raw.FPDFPage_CloseAnnot(annotation)


def revise_pdf(content: bytes, changes: list[dict], *, author: str,
               annotated_at: str | None = None) -> dict:
    """Add exact text-anchored PDF review annotations in an incremental copy."""
    if not isinstance(author, str) or not author.strip() or len(author) > 300:
        _fail("PDF_ANNOTATION_AUTHOR_INVALID")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        _fail("PDF_CHANGES_INVALID", maximum=MAX_CHANGES)
    normalized = [_validate_change(item, index) for index, item in enumerate(changes)]
    timestamp = annotated_at or datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    if not isinstance(timestamp, str) or not re.fullmatch(r"D:\d{14}Z", timestamp):
        _fail("PDF_ANNOTATION_TIMESTAMP_INVALID")
    source_hash = _hash(content)
    document = _open_pdf(content)
    source_page_count = len(document)
    pdfium, raw = _pdfium()
    source_signature_status = _signature_state(document, raw, content, strict=True)
    pages = {}
    created_annotations = []
    original_page_state = []
    try:
        for page_number in sorted({item["page"] for item in normalized}):
            if page_number > len(document):
                _fail("PDF_PAGE_NOT_FOUND", page=page_number)
            page = document.get_page(page_number - 1)
            textpage = page.get_textpage()
            original_page_state.append({
                "page": page_number,
                "width": float(page.get_width()), "height": float(page.get_height()),
                "bbox": [float(value) for value in page.get_bbox()],
                "mediabox": [float(value) for value in page.get_mediabox()],
                "cropbox": [float(value) for value in page.get_cropbox()],
                "rotation": int(page.get_rotation()),
                "text": textpage.get_text_range(),
                "annotations": _annotation_snapshot(raw, page),
            })
            pages[page_number] = (page, textpage)

        anchor_ids = set()
        for index, change in enumerate(normalized):
            page, textpage = pages[change["page"]]
            page_bbox = tuple(float(value) for value in page.get_bbox())
            if "quote" in change:
                matches = _matches(textpage, change["quote"])
                if not matches:
                    _fail("PDF_ANCHOR_NOT_FOUND", index=index, page=change["page"])
                occurrence = change["occurrence"]
                if occurrence is None:
                    if len(matches) != 1:
                        _fail("PDF_ANCHOR_AMBIGUOUS", index=index, page=change["page"],
                              occurrences=len(matches))
                    occurrence = 0
                if occurrence >= len(matches):
                    _fail("PDF_OCCURRENCE_NOT_FOUND", index=index, page=change["page"],
                          occurrences=len(matches))
                start, count = matches[occurrence]
                actual_quote = textpage.get_text_range(start, count)
                if actual_quote != change["quote"]:
                    _fail("PDF_ANCHOR_READBACK_FAILED", index=index, page=change["page"])
                rectangles = _line_rectangles(textpage, start, count)
                for rectangle in rectangles:
                    _rect_within_bbox(list(rectangle), page_bbox, index=index)
                quads = _quads_for_rectangles(rectangles)
                object_id = _annotation_id(source_hash, change["page"], change["quote"], occurrence,
                                           change["comment"], author, timestamp)
                record = {**change, "occurrence": occurrence, "rectangles": [list(rectangle) for rectangle in rectangles]}
            elif "rect" in change:
                rectangle = change["rect"]
                _rect_within_bbox(rectangle, page_bbox, index=index)
                rectangles = [tuple(rectangle)]
                quads = _quads_for_rectangles(rectangles)
                object_id = _annotation_id(source_hash, change["page"],
                                           json.dumps(rectangle, separators=(",", ":")), 0,
                                           change["comment"], author, timestamp)
                record = {**change, "anchor_kind": "rect", "rectangles": [rectangle], "occurrence": None}
            else:
                quads = change["quads"]
                rectangles = []
                for points in quads:
                    rectangle = [min(points[0::2]), min(points[1::2]),
                                 max(points[0::2]), max(points[1::2])]
                    _rect_within_bbox(rectangle, page_bbox, index=index)
                    rectangles.append(tuple(rectangle))
                object_id = _annotation_id(source_hash, change["page"],
                                           json.dumps(quads, separators=(",", ":")), 0,
                                           change["comment"], author, timestamp)
                record = {**change, "anchor_kind": "quads", "rectangles": [list(rectangle) for rectangle in rectangles],
                          "occurrence": None}
            if object_id in anchor_ids:
                _fail("PDF_ANCHOR_DUPLICATED", index=index)
            anchor_ids.add(object_id)
            record.update({
                "author": author,
                "annotated_at": timestamp,
                "quads": quads,
                "rotation": int(page.get_rotation()),
                "object_id": object_id,
                "coordinate_space": "pdf-user-space-points-bottom-left",
            })
            _create_highlight(raw, page, quads, record, author)
            created_annotations.append(record)

        output = io.BytesIO()
        try:
            document.save(output, flags=PDFIUM_INCREMENTAL_SAVE_FLAG)
        except Exception as exc:
            raise PdfRevisionError("PDF_INCREMENTAL_SAVE_FAILED", {"reason": type(exc).__name__}) from exc
        revised = output.getvalue()
        if not revised.startswith(content):
            _fail("PDF_INCREMENTAL_SAVE_UNAVAILABLE")
        revised = _normalize_output_id(revised, source_hash, created_annotations)
    except PdfRevisionError:
        raise
    except Exception as exc:
        raise PdfRevisionError("PDF_REVISION_FAILED", {"reason": type(exc).__name__}) from exc
    finally:
        for page, textpage in pages.values():
            textpage.close()
            page.close()
        document.close()

    updated_document = _open_pdf(revised)
    try:
        if len(updated_document) != source_page_count:
            _fail("PDF_PAGE_COUNT_CHANGED")
        output_signature_status = _signature_state(updated_document, raw, revised, strict=True)
        source_signature_records = source_signature_status["signatures"]
        output_signature_records = output_signature_status["signatures"]
        if len(source_signature_records) != len(output_signature_records):
            _fail("PDF_SIGNATURE_STATE_CHANGED")
        for before, after in zip(source_signature_records, output_signature_records):
            if (before["docmdp_permission"] != after["docmdp_permission"]
                    or before["byte_range"] != after["byte_range"]):
                _fail("PDF_SIGNATURE_STATE_CHANGED", index=before["index"])
        changes_per_page = {}
        for change in created_annotations:
            changes_per_page[change["page"]] = changes_per_page.get(change["page"], 0) + 1
        verified_pages = []
        for state in original_page_state:
            page = updated_document.get_page(state["page"] - 1)
            try:
                textpage = page.get_textpage()
                try:
                    actual_text = textpage.get_text_range()
                finally:
                    textpage.close()
                if actual_text != state["text"]:
                    _fail("PDF_ORIGINAL_TEXT_CHANGED", page=state["page"])
                for property_name, observed in (("bbox", page.get_bbox()),
                                                ("mediabox", page.get_mediabox()),
                                                ("cropbox", page.get_cropbox())):
                    expected = state[property_name]
                    if len(expected) != len(observed) or any(abs(float(a) - float(b)) > 0.01
                                                              for a, b in zip(expected, observed)):
                        _fail("PDF_ORIGINAL_PAGE_BOX_CHANGED", page=state["page"], box=property_name)
                if (int(page.get_rotation()) != state["rotation"]
                        or abs(float(page.get_width()) - state["width"]) > 0.01
                        or abs(float(page.get_height()) - state["height"]) > 0.01):
                    _fail("PDF_ORIGINAL_PAGE_GEOMETRY_CHANGED", page=state["page"])
                annotations = _annotation_snapshot(raw, page)
                old_annotations = state["annotations"]
                expected_count = len(old_annotations) + changes_per_page.get(state["page"], 0)
                if len(annotations) != expected_count:
                    _fail("PDF_EXISTING_ANNOTATIONS_CHANGED", page=state["page"],
                          expected=expected_count, actual=len(annotations))
                if annotations[:len(old_annotations)] != old_annotations:
                    _fail("PDF_EXISTING_ANNOTATIONS_CHANGED", page=state["page"])
                verified_pages.append({"page": state["page"], "annotation_count": len(annotations)})
            finally:
                page.close()
    finally:
        updated_document.close()
    return {
        "content": revised,
        "changes": created_annotations,
        "pages": verified_pages,
        "signature_status": {
            "state": source_signature_status["state"],
            "modified_after_signature": bool(source_signature_status["count"]),
            "original_signed_bytes_preserved": bool(source_signature_status["count"])
                                                and revised.startswith(content),
            "cryptographic_validation": "not_performed" if source_signature_status["count"]
                                        else "not_applicable",
            "signatures": source_signature_status["signatures"],
        },
        "unchanged_verified": True,
        "warnings": [],
    }

