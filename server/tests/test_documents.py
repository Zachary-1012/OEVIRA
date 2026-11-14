from __future__ import annotations

import hashlib
import io
import zipfile
import unittest

from docx import Document
from docx.shared import Inches
from PIL import Image

from oevira.documents import DocumentError, W_NS, inspect_document, revise_document


def _docx(*, signed: bool = False) -> bytes:
    document = Document()
    paragraph = document.add_paragraph(style="Heading 2")
    prefix = paragraph.add_run("前缀 ")
    prefix.bold = True
    selected = paragraph.add_run("客户")
    selected.italic = True
    suffix = paragraph.add_run(" 的原稿")
    suffix.bold = False
    document.add_paragraph("未选正文保持原样")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "待改表格单元"
    table.cell(0, 1).text = "未选表格单元"
    document.sections[0].header.paragraphs[0].text = "保留页眉"
    document.sections[0].header.add_paragraph("未选页眉段")
    document.sections[0].footer.paragraphs[0].text = "保留页脚"

    output = io.BytesIO()
    document.save(output)
    original = output.getvalue()
    enriched = io.BytesIO()
    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(original), "r") as source:
        parts = {info.filename: source.read(info) for info in source.infolist()}
        infos = list(source.infolist())
        package_comment = source.comment
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    document_root = etree.fromstring(parts["word/document.xml"])
    body = document_root.find(f"{{{w}}}body")
    second_paragraph = body.findall(f"{{{w}}}p")[1]
    reference_run = etree.SubElement(second_paragraph, f"{{{w}}}r")
    etree.SubElement(reference_run, f"{{{w}}}footnoteReference", {f"{{{w}}}id": "1"})
    parts["word/document.xml"] = etree.tostring(document_root, encoding="UTF-8",
                                                xml_declaration=True, standalone=True)
    relationship_root = etree.fromstring(parts["word/_rels/document.xml.rels"])
    etree.SubElement(relationship_root, f"{{{rel}}}Relationship", {
        "Id": "rIdOeviraFootnotes",
        "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
        "Target": "footnotes.xml",
    })
    parts["word/_rels/document.xml.rels"] = etree.tostring(
        relationship_root, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    content_types_root = etree.fromstring(parts["[Content_Types].xml"])
    etree.SubElement(content_types_root, f"{{{ct}}}Override", {
        "PartName": "/word/footnotes.xml",
        "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    })
    parts["[Content_Types].xml"] = etree.tostring(
        content_types_root, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    footnotes = etree.Element(f"{{{w}}}footnotes", nsmap={"w": w})
    separator = etree.SubElement(footnotes, f"{{{w}}}footnote", {
        f"{{{w}}}type": "separator", f"{{{w}}}id": "-1",
    })
    etree.SubElement(etree.SubElement(separator, f"{{{w}}}p"), f"{{{w}}}r")
    continuation = etree.SubElement(footnotes, f"{{{w}}}footnote", {
        f"{{{w}}}type": "continuationSeparator", f"{{{w}}}id": "0",
    })
    etree.SubElement(etree.SubElement(continuation, f"{{{w}}}p"), f"{{{w}}}r")
    note = etree.SubElement(footnotes, f"{{{w}}}footnote", {f"{{{w}}}id": "1"})
    note_paragraph = etree.SubElement(note, f"{{{w}}}p")
    note_text_run = etree.SubElement(note_paragraph, f"{{{w}}}r")
    note_text = etree.SubElement(note_text_run, f"{{{w}}}t")
    note_text.text = "脚注原文"
    parts["word/footnotes.xml"] = etree.tostring(
        footnotes, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    document_root = etree.fromstring(parts["word/document.xml"])
    second_paragraph = document_root.find(f"{{{w}}}body").findall(f"{{{w}}}p")[1]
    second_paragraph.insert(0, etree.Element(f"{{{w}}}commentRangeStart", {f"{{{w}}}id": "7"}))
    etree.SubElement(second_paragraph, f"{{{w}}}commentRangeEnd", {f"{{{w}}}id": "7"})
    comment_reference_run = etree.SubElement(second_paragraph, f"{{{w}}}r")
    etree.SubElement(comment_reference_run, f"{{{w}}}commentReference", {f"{{{w}}}id": "7"})
    parts["word/document.xml"] = etree.tostring(document_root, encoding="UTF-8",
                                                xml_declaration=True, standalone=True)
    relationship_root = etree.fromstring(parts["word/_rels/document.xml.rels"])
    etree.SubElement(relationship_root, f"{{{rel}}}Relationship", {
        "Id": "rIdOeviraComments",
        "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        "Target": "comments.xml",
    })
    parts["word/_rels/document.xml.rels"] = etree.tostring(
        relationship_root, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    content_types_root = etree.fromstring(parts["[Content_Types].xml"])
    etree.SubElement(content_types_root, f"{{{ct}}}Override", {
        "PartName": "/word/comments.xml",
        "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    })
    parts["[Content_Types].xml"] = etree.tostring(
        content_types_root, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    comments = etree.Element(f"{{{w}}}comments", nsmap={"w": w})
    comment = etree.SubElement(comments, f"{{{w}}}comment", {
        f"{{{w}}}id": "7", f"{{{w}}}author": "Reviewer",
        f"{{{w}}}date": "2026-11-10T09:00:00Z",
    })
    comment_paragraph = etree.SubElement(comment, f"{{{w}}}p")
    comment_run = etree.SubElement(comment_paragraph, f"{{{w}}}r")
    comment_text = etree.SubElement(comment_run, f"{{{w}}}t")
    comment_text.text = "评论原文"
    parts["word/comments.xml"] = etree.tostring(
        comments, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    document_root = etree.fromstring(parts["word/document.xml"])
    body = document_root.find(f"{{{w}}}body")
    revision_paragraph = etree.Element(f"{{{w}}}p")
    prefix_run = etree.SubElement(revision_paragraph, f"{{{w}}}r")
    etree.SubElement(prefix_run, f"{{{w}}}t").text = "正文前"
    inserted = etree.SubElement(revision_paragraph, f"{{{w}}}ins", {
        f"{{{w}}}id": "8", f"{{{w}}}author": "Editor", f"{{{w}}}date": "2026-11-10T09:01:00Z",
    })
    inserted_run = etree.SubElement(inserted, f"{{{w}}}r")
    etree.SubElement(inserted_run, f"{{{w}}}t").text = "新增文字"
    middle_run = etree.SubElement(revision_paragraph, f"{{{w}}}r")
    etree.SubElement(middle_run, f"{{{w}}}t").text = "正文中"
    deleted = etree.SubElement(revision_paragraph, f"{{{w}}}del", {
        f"{{{w}}}id": "9", f"{{{w}}}author": "Editor", f"{{{w}}}date": "2026-11-10T09:02:00Z",
    })
    deleted_run = etree.SubElement(deleted, f"{{{w}}}r")
    etree.SubElement(deleted_run, f"{{{w}}}delText").text = "待删除内容"
    suffix_run = etree.SubElement(revision_paragraph, f"{{{w}}}r")
    etree.SubElement(suffix_run, f"{{{w}}}t").text = "正文后"
    section_properties = body.find(f"{{{w}}}sectPr")
    body.insert(body.index(section_properties), revision_paragraph)
    parts["word/document.xml"] = etree.tostring(document_root, encoding="UTF-8",
                                                xml_declaration=True, standalone=True)
    with zipfile.ZipFile(io.BytesIO(original), "r") as source, zipfile.ZipFile(enriched, "w") as target:
        target.comment = b"original package comment"
        for info in infos:
            target.writestr(info, parts[info.filename])
        target.writestr("word/footnotes.xml", parts["word/footnotes.xml"])
        target.writestr("word/comments.xml", parts["word/comments.xml"])
        target.writestr("custom/preserved.bin", b"custom package member")
        if signed:
            target.writestr("_xmlsignatures/sig1.xml", b"signature bytes")
    return enriched.getvalue()


def _mixed_run_docx() -> bytes:
    document = Document()
    paragraph = document.add_paragraph()
    bold = paragraph.add_run("品牌")
    bold.bold = True
    italic = paragraph.add_run("定位")
    italic.italic = True
    underlined = paragraph.add_run("旧方案")
    underlined.underline = True
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _png(color: tuple[int, int, int], size: tuple[int, int] = (40, 24)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def _docx_with_shared_images(original_image: bytes) -> bytes:
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Before ")
    first = paragraph.add_run().add_picture(io.BytesIO(original_image),
                                            width=Inches(1.5), height=Inches(0.9))
    first._inline.docPr.set("descr", "First artwork")
    paragraph.add_run(" between ")
    second = paragraph.add_run().add_picture(io.BytesIO(original_image),
                                             width=Inches(0.8), height=Inches(0.6))
    second._inline.docPr.set("descr", "Second artwork")
    paragraph.add_run(" after")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _docx_with_header_image(image: bytes) -> bytes:
    document = Document()
    paragraph = document.sections[0].header.paragraphs[0]
    shape = paragraph.add_run().add_picture(io.BytesIO(image),
                                            width=Inches(1.2), height=Inches(0.7))
    shape._inline.docPr.set("descr", "Header artwork")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _member_payloads(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class DocumentEditingTests(unittest.TestCase):
    def test_inspection_returns_repeatable_paragraph_and_table_locations(self) -> None:
        original = _docx()
        first = inspect_document(original)
        second = inspect_document(original)
        self.assertEqual([item["id"] for item in first["objects"]], [item["id"] for item in second["objects"]])
        paragraph = next(item for item in first["objects"] if item["text"] == "前缀 客户 的原稿")
        selected_cell = next(item for item in first["objects"] if item["text"] == "待改表格单元")
        self.assertEqual(paragraph["kind"], "paragraph")
        self.assertEqual(selected_cell["kind"], "table_cell_paragraph")
        self.assertIn("tbl[1]/row[1]/cell[1]", selected_cell["location"]["path"])
        self.assertTrue(paragraph["editable"])
        self.assertTrue(selected_cell["editable"])
        header = next(item for item in first["objects"] if item["kind"] == "header_paragraph"
                      and item["text"] == "保留页眉")
        footer = next(item for item in first["objects"] if item["kind"] == "footer_paragraph"
                      and item["text"] == "保留页脚")
        self.assertEqual(header["location"]["used_by"], ["section[1].header.default"])
        self.assertEqual(footer["location"]["used_by"], ["section[1].footer.default"])
        self.assertTrue(header["editable"])
        self.assertTrue(footer["editable"])
        footnote = next(item for item in first["objects"] if item["kind"] == "footnote_paragraph"
                        and item["text"] == "脚注原文")
        self.assertEqual(footnote["location"]["note_id"], "1")
        self.assertEqual(footnote["location"]["used_by"], ["body/p[2]"])
        self.assertTrue(footnote["editable"])
        comment = next(item for item in first["objects"] if item["kind"] == "comment_paragraph"
                       and item["text"] == "评论原文")
        self.assertEqual(comment["location"]["comment_id"], "7")
        self.assertEqual(comment["location"]["author"], "Reviewer")
        self.assertEqual(comment["location"]["used_by"], ["body/p[2]"])
        self.assertTrue(comment["editable"])
        revisions = [item for item in first["objects"] if item["kind"] == "tracked_change"]
        self.assertEqual({item["location"]["change_type"] for item in revisions},
                         {"insertion", "deletion"})
        self.assertTrue(all(item["edit_modes"] == ["accept", "reject"] for item in revisions))

    def test_exact_text_revision_preserves_other_objects_styles_and_package_members(self) -> None:
        original = _docx()
        before_members = _member_payloads(original)
        inspected = inspect_document(original)
        selected_paragraph = next(item for item in inspected["objects"] if item["text"] == "前缀 客户 的原稿")
        selected_cell = next(item for item in inspected["objects"] if item["text"] == "待改表格单元")
        result = revise_document(original, [
            {"object_id": selected_paragraph["id"], "before": "客户", "after": "用户"},
            {"object_id": selected_cell["id"], "before": "待改", "after": "已改"},
        ])

        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(result["changed_member"], "word/document.xml")
        self.assertEqual(result["preserved_member_count"], len(before_members) - 1)
        after_members = _member_payloads(result["content"])
        self.assertEqual(set(after_members), set(before_members))
        self.assertNotEqual(after_members["word/document.xml"], before_members["word/document.xml"])
        for name in before_members:
            if name != "word/document.xml":
                self.assertEqual(after_members[name], before_members[name], name)

        reopened = Document(io.BytesIO(result["content"]))
        self.assertEqual(reopened.paragraphs[0].text, "前缀 用户 的原稿")
        self.assertTrue(reopened.paragraphs[0].runs[0].bold)
        self.assertTrue(reopened.paragraphs[0].runs[1].italic)
        self.assertEqual(reopened.paragraphs[0].runs[1].text, "用户")
        self.assertEqual(reopened.paragraphs[0].runs[2].text, " 的原稿")
        self.assertEqual(reopened.paragraphs[1].text, "未选正文保持原样")
        self.assertEqual(reopened.tables[0].cell(0, 0).text, "已改表格单元")
        self.assertEqual(reopened.tables[0].cell(0, 1).text, "未选表格单元")
        self.assertEqual(reopened.sections[0].header.paragraphs[0].text, "保留页眉")
        self.assertEqual(reopened.sections[0].header.paragraphs[1].text, "未选页眉段")
        self.assertEqual(reopened.sections[0].footer.paragraphs[0].text, "保留页脚")

        changed = {item["id"]: item["text"] for item in inspect_document(result["content"])["objects"]}
        self.assertIn("前缀 用户 的原稿", changed.values())
        self.assertIn("已改表格单元", changed.values())
        with self.assertRaisesRegex(DocumentError, "DOCX_OBJECT_NOT_FOUND"):
            revise_document(result["content"], [{
                "object_id": selected_paragraph["id"], "before": "前缀", "after": "新前缀",
            }])

    def test_footnote_text_revision_preserves_reference_body_and_other_package_members(self) -> None:
        original = _docx()
        before_parts = _member_payloads(original)
        footnote = next(item for item in inspect_document(original)["objects"]
                        if item["kind"] == "footnote_paragraph" and item["text"] == "脚注原文")
        result = revise_document(original, [{
            "object_id": footnote["id"], "before": "脚注", "after": "来源注",
        }])
        self.assertEqual(result["changed_members"], ["word/footnotes.xml"])
        self.assertTrue(result["unchanged_verified"])
        after_parts = _member_payloads(result["content"])
        for name, payload in before_parts.items():
            if name != "word/footnotes.xml":
                self.assertEqual(after_parts[name], payload, name)
        from lxml import etree

        footnotes = etree.fromstring(after_parts["word/footnotes.xml"])
        text = footnotes.xpath(".//w:footnote[@w:id='1']/w:p/w:r/w:t", namespaces={"w": W_NS})[0]
        self.assertEqual(text.text, "来源注原文")
        body_text = next(
            item["text"] for item in inspect_document(result["content"])["objects"]
            if item["kind"] == "paragraph" and item["location"]["path"] == "body/p[2]"
        )
        self.assertEqual(body_text, "未选正文保持原样")

    def test_comment_text_revision_keeps_anchor_author_and_unselected_document_range(self) -> None:
        original = _docx()
        before_parts = _member_payloads(original)
        comment = next(item for item in inspect_document(original)["objects"]
                       if item["kind"] == "comment_paragraph" and item["text"] == "评论原文")
        result = revise_document(original, [{
            "object_id": comment["id"], "before": "评论", "after": "评审意见",
        }])
        self.assertEqual(result["changed_members"], ["word/comments.xml"])
        self.assertTrue(result["unchanged_verified"])
        after_parts = _member_payloads(result["content"])
        for name, payload in before_parts.items():
            if name != "word/comments.xml":
                self.assertEqual(after_parts[name], payload, name)
        from lxml import etree

        comments = etree.fromstring(after_parts["word/comments.xml"])
        comment_node = comments.xpath(".//w:comment[@w:id='7']", namespaces={"w": W_NS})[0]
        self.assertEqual(comment_node.get(f"{{{W_NS}}}author"), "Reviewer")
        self.assertEqual(comment_node.get(f"{{{W_NS}}}date"), "2026-11-10T09:00:00Z")
        self.assertEqual("".join(comment_node.xpath(".//w:t/text()", namespaces={"w": W_NS})),
                         "评审意见原文")
        document_root = etree.fromstring(after_parts["word/document.xml"])
        self.assertEqual(len(document_root.xpath(".//w:commentRangeStart[@w:id='7']",
                                                namespaces={"w": W_NS})), 1)
        self.assertEqual(len(document_root.xpath(".//w:commentRangeEnd[@w:id='7']",
                                                namespaces={"w": W_NS})), 1)

    def test_selected_inline_image_replacement_clones_relation_and_preserves_unselected_sibling(self) -> None:
        original_image = _png((240, 15, 15))
        replacement = _png((15, 35, 240), (80, 48))
        original = _docx_with_shared_images(original_image)
        before_parts = _member_payloads(original)
        images = [item for item in inspect_document(original)["objects"] if item["kind"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(item["editable"] for item in images))
        self.assertEqual(images[0]["media"]["sha256"], images[1]["media"]["sha256"])
        self.assertEqual(images[0]["location"]["relationship_id"],
                         images[1]["location"]["relationship_id"])
        source = {
            "kind": "material", "material_id": "replacement-image",
            "filename": "replacement.png", "sha256": hashlib.sha256(replacement).hexdigest(),
            "size_bytes": len(replacement), "format": "png",
        }
        result = revise_document(original, [{
            "object_id": images[0]["id"],
            "replace_image": {"content": replacement, "filename": "replacement.png",
                              "source": source, "alt_text": "Updated artwork"},
        }])
        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(result["changes"][0]["kind"], "image_replacement")
        self.assertTrue(result["changes"][0]["copy_on_write"])
        self.assertTrue(result["changes"][0]["geometry_preserved"])
        self.assertTrue(result["changes"][0]["crop_preserved"])

        after_parts = _member_payloads(result["content"])
        original_media_parts = {name: payload for name, payload in before_parts.items()
                                if name.startswith("word/media/")}
        for name, payload in original_media_parts.items():
            self.assertEqual(after_parts[name], payload, name)
        image_part = next(name for name in result["added_parts"] if name.startswith("word/media/"))
        self.assertEqual(after_parts[image_part], replacement)

        updated_images = [item for item in inspect_document(result["content"])["objects"]
                          if item["kind"] == "image"]
        self.assertEqual(updated_images[0]["media"]["sha256"], source["sha256"])
        self.assertEqual(updated_images[0]["text"], "Updated artwork")
        self.assertEqual(updated_images[0]["geometry"], images[0]["geometry"])
        self.assertEqual(updated_images[0]["crop"], images[0]["crop"])
        self.assertEqual(updated_images[1]["media"]["sha256"], images[1]["media"]["sha256"])

        reopened = Document(io.BytesIO(result["content"]))
        self.assertEqual(len(reopened.inline_shapes), 2)
        relation_ids = [shape._inline.graphic.graphicData.pic.blipFill.blip.embed
                        for shape in reopened.inline_shapes]
        self.assertNotEqual(relation_ids[0], relation_ids[1])
        blobs = [reopened.part.related_parts[relation_id].blob for relation_id in relation_ids]
        self.assertEqual(blobs, [replacement, original_image])

    def test_header_image_replacement_updates_its_own_relationship_part(self) -> None:
        original_image = _png((220, 30, 20))
        replacement = _png((20, 30, 220), (72, 42))
        original = _docx_with_header_image(original_image)
        before_parts = _member_payloads(original)
        image = next(item for item in inspect_document(original)["objects"]
                     if item["kind"] == "image")
        self.assertEqual(image["location"]["part"], "word/header1.xml")
        self.assertEqual(image["location"]["used_by"], ["section[1].header.default"])
        source = {
            "kind": "material", "material_id": "header-replacement-image",
            "filename": "header-replacement.png", "sha256": hashlib.sha256(replacement).hexdigest(),
            "size_bytes": len(replacement), "format": "png",
        }
        result = revise_document(original, [{
            "object_id": image["id"],
            "replace_image": {"content": replacement, "filename": "header-replacement.png",
                              "source": source, "alt_text": "Updated header artwork"},
        }])
        self.assertTrue(result["unchanged_verified"])
        self.assertIn("word/header1.xml", result["changed_members"])
        self.assertIn("word/_rels/header1.xml.rels", result["changed_members"])
        after_parts = _member_payloads(result["content"])
        for name, payload in before_parts.items():
            if name.startswith("word/media/"):
                self.assertEqual(after_parts[name], payload, name)
        reopened = Document(io.BytesIO(result["content"]))
        header = reopened.sections[0].header
        blip = header._element.xpath(".//a:blip")[0]
        relationship_id = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        self.assertEqual(header.part.related_parts[relationship_id].blob, replacement)
        doc_properties = header._element.xpath(".//wp:docPr")[0]
        self.assertEqual(doc_properties.get("descr"), "Updated header artwork")

    def test_tracked_insertion_and_deletion_accept_reject_preserve_surrounding_text(self) -> None:
        original = _docx()

        def revision_objects(content: bytes) -> dict[str, dict]:
            return {
                item["location"]["change_type"]: item
                for item in inspect_document(content)["objects"]
                if item["kind"] == "tracked_change"
            }

        first_objects = revision_objects(original)
        accepted_insertion = revise_document(original, [{
            "object_id": first_objects["insertion"]["id"], "operation": "accept",
        }])
        remaining = revision_objects(accepted_insertion["content"])
        self.assertEqual(set(remaining), {"deletion"})
        rejected_deletion = revise_document(accepted_insertion["content"], [{
            "object_id": remaining["deletion"]["id"], "operation": "reject",
        }])
        self.assertEqual(revision_objects(rejected_deletion["content"]), {})
        reopened = Document(io.BytesIO(rejected_deletion["content"]))
        self.assertEqual(reopened.paragraphs[2].text, "正文前新增文字正文中待删除内容正文后")

        second_objects = revision_objects(original)
        rejected_insertion = revise_document(original, [{
            "object_id": second_objects["insertion"]["id"], "operation": "reject",
        }])
        remaining = revision_objects(rejected_insertion["content"])
        self.assertEqual(set(remaining), {"deletion"})
        accepted_deletion = revise_document(rejected_insertion["content"], [{
            "object_id": remaining["deletion"]["id"], "operation": "accept",
        }])
        reopened_rejected = Document(io.BytesIO(accepted_deletion["content"]))
        self.assertEqual(reopened_rejected.paragraphs[2].text, "正文前正文中正文后")

    def test_header_and_footer_text_revision_changes_only_selected_related_parts(self) -> None:
        original = _docx()
        before_parts = _member_payloads(original)
        objects = inspect_document(original)["objects"]
        header = next(item for item in objects if item["kind"] == "header_paragraph"
                      and item["text"] == "保留页眉")
        footer = next(item for item in objects if item["kind"] == "footer_paragraph"
                      and item["text"] == "保留页脚")
        result = revise_document(original, [
            {"object_id": header["id"], "before": "保留", "after": "新版"},
            {"object_id": footer["id"], "before": "保留", "after": "新版"},
        ])

        self.assertTrue(result["unchanged_verified"])
        self.assertIsNone(result["changed_member"])
        self.assertEqual(result["changed_members"], ["word/footer1.xml", "word/header1.xml"])
        self.assertEqual({item["part"] for item in result["changes"]}, set(result["changed_members"]))
        after_parts = _member_payloads(result["content"])
        self.assertEqual(set(after_parts), set(before_parts))
        for name, payload in before_parts.items():
            if name not in result["changed_members"]:
                self.assertEqual(after_parts[name], payload, name)

        reopened = Document(io.BytesIO(result["content"]))
        self.assertEqual(reopened.sections[0].header.paragraphs[0].text, "新版页眉")
        self.assertEqual(reopened.sections[0].header.paragraphs[1].text, "未选页眉段")
        self.assertEqual(reopened.sections[0].footer.paragraphs[0].text, "新版页脚")

    def test_mixed_run_word_change_disjoint_edits_insert_delete_and_reopen(self) -> None:
        content = _mixed_run_docx()

        def selected_id(data: bytes) -> str:
            return inspect_document(data)["objects"][0]["id"]

        # One word changes inside a styled run; the equal suffix keeps its run.
        content = revise_document(content, [{
            "object_id": selected_id(content), "before": "旧方案", "after": "新方案",
        }])["content"]
        reopened = Document(io.BytesIO(content))
        self.assertEqual(reopened.paragraphs[0].text, "品牌定位新方案")
        self.assertTrue(reopened.paragraphs[0].runs[0].bold)
        self.assertTrue(reopened.paragraphs[0].runs[1].italic)
        self.assertTrue(reopened.paragraphs[0].runs[2].underline)
        self.assertEqual(reopened.paragraphs[0].runs[2].text, "新方案")

        # Two separated replacements leave the middle and shared run text in place.
        content = revise_document(content, [{
            "object_id": selected_id(content), "before": "品牌定位新方案", "after": "公司定位新计划",
        }])["content"]
        reopened = Document(io.BytesIO(content))
        self.assertEqual(reopened.paragraphs[0].text, "公司定位新计划")
        self.assertTrue(reopened.paragraphs[0].runs[0].bold)
        self.assertTrue(reopened.paragraphs[0].runs[1].italic)
        self.assertTrue(reopened.paragraphs[0].runs[2].underline)

        # Inserted text inherits the preceding run, then deleting it and one
        # character leaves the untouched styled text in its original runs.
        content = revise_document(content, [{
            "object_id": selected_id(content), "before": "公司定位新计划", "after": "公司（推荐）定位新计划",
        }])["content"]
        reopened = Document(io.BytesIO(content))
        self.assertEqual(reopened.paragraphs[0].text, "公司（推荐）定位新计划")
        self.assertTrue(reopened.paragraphs[0].runs[0].bold)
        self.assertIn("（推荐）", reopened.paragraphs[0].runs[0].text)

        content = revise_document(content, [{
            "object_id": selected_id(content), "before": "公司（推荐）定位新计划", "after": "公司定位计划",
        }])["content"]
        reopened = Document(io.BytesIO(content))
        self.assertEqual(reopened.paragraphs[0].text, "公司定位计划")
        self.assertTrue(reopened.paragraphs[0].runs[0].bold)
        self.assertTrue(reopened.paragraphs[0].runs[1].italic)
        self.assertTrue(reopened.paragraphs[0].runs[2].underline)

    def test_invalid_or_ambiguous_selection_is_rejected_without_mutating_source(self) -> None:
        original = _docx()
        inspect_result = inspect_document(original)
        paragraph = next(item for item in inspect_result["objects"] if item["text"] == "前缀 客户 的原稿")
        source_copy = original[:]
        with self.assertRaisesRegex(DocumentError, "DOCX_TEXT_MATCH_NOT_FOUND"):
            revise_document(original, [{"object_id": paragraph["id"], "before": "不存在", "after": "替换"}])
        self.assertEqual(original, source_copy)
        with self.assertRaisesRegex(DocumentError, "DOCX_OBJECT_NOT_FOUND"):
            revise_document(original, [{"object_id": "paragraph:body/p[99]", "before": "客户", "after": "用户"}])
        with self.assertRaisesRegex(DocumentError, "DOCX_AFTER_TEXT_INVALID"):
            revise_document(original, [{"object_id": paragraph["id"], "before": "客户", "after": "\x00"}])

    def test_digital_signature_blocks_modification(self) -> None:
        original = _docx(signed=True)
        paragraph = next(item for item in inspect_document(original)["objects"] if item["text"] == "前缀 客户 的原稿")
        with self.assertRaisesRegex(DocumentError, "DOCX_SIGNATURE_PRESENT"):
            revise_document(original, [{"object_id": paragraph["id"], "before": "客户", "after": "用户"}])


if __name__ == "__main__":
    unittest.main()
