from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from unittest.mock import patch
from xml.etree import ElementTree as ET

from oevira.office import inspect_presentation, revise_presentation


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _shape(object_id: int, name: str, text: str, *, runs: list[tuple[str, str]] | None = None,
           paragraphs: list[str] | None = None) -> str:
    if paragraphs is not None:
        body = "".join(f"<a:p><a:r><a:t>{value}</a:t></a:r></a:p>" for value in paragraphs)
    elif runs is not None:
        body = "<a:p>" + "".join(
            f'<a:r><a:rPr {style}/><a:t>{value}</a:t></a:r>' for value, style in runs
        ) + "</a:p>"
    elif text:
        body = f"<a:p><a:r><a:rPr lang=\"zh-CN\"/><a:t>{text}</a:t></a:r></a:p>"
    else:
        body = "<a:p><a:endParaRPr lang=\"zh-CN\"/></a:p>"
    return (
        f"<p:sp><p:nvSpPr><p:cNvPr id=\"{object_id}\" name=\"{name}\"/>"
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>"
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>"
    )


def _slide(number: int) -> bytes:
    title = _shape(2, f"第{number}页标题", f"旧标题{number}")
    extras = ""
    if number == 3:
        title = _shape(2, "客户标题", "", runs=[("旧", 'lang="zh-CN" b="1"'),
                                                  ("标题", 'lang="zh-CN" b="1"')])
        extras += _shape(3, "未选说明", "保持原样")
        extras += (
            "<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id=\"4\" name=\"图表\"/>"
            "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
            "<a:graphic><a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/chart\">"
            "<c:chart xmlns:c=\"http://schemas.openxmlformats.org/drawingml/2006/chart\" r:id=\"rIdChart\"/>"
            "</a:graphicData></a:graphic></p:graphicFrame>"
        )
        extras += (
            "<p:pic><p:nvPicPr><p:cNvPr id=\"5\" name=\"客户图片\" descr=\"只读图片\"/>"
            "<p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill/><p:spPr/></p:pic>"
        )
        extras += _shape(6, "多段对象", "", paragraphs=["第一段", "第二段"])
        extras += _shape(7, "混合样式对象", "", runs=[("粗体", 'lang="zh-CN" b="1"'),
                                                        ("常规", 'lang="zh-CN"')])
        extras += (
            "<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id=\"8\" name=\"客户表格\"/>"
            "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
            "<a:graphic><a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/table\">"
            "<a:tbl><a:tblPr/><a:tblGrid><a:gridCol w=\"1000\"/><a:gridCol w=\"1000\"/></a:tblGrid>"
            "<a:tr h=\"400\">"
            "<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang=\"zh-CN\"/>"
            "<a:t>可改单元格</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>"
            "<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang=\"zh-CN\"/>"
            "<a:t>保留单元格</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>"
            "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
        )
    elif number == 5:
        title = _shape(2, "第五页标题", "旧第五页")
    xml = (
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        "<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/>"
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
        f"{title}{extras}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )
    return xml.encode("utf-8")


def _pptx(*, external_link: bool = False, nested_zip: bool = False,
          signed: bool = False) -> bytes:
    slide_ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in range(1, 7))
    presentation = (
        f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>"
    ).encode()
    rels = [
        f'<Relationship Id="rId{n}" Type="{R_NS}/slide" Target="slides/slide{n}.xml"/>'
        for n in range(1, 7)
    ]
    presentation_rels = (
        f'<Relationships xmlns="{PKG_REL_NS}">' + "".join(rels) + "</Relationships>"
    ).encode()
    content_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{n}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for n in range(1, 7)
    )
    content_types = (
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        + content_overrides + "</Types>"
    ).encode()
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": (
            f'<Relationships xmlns="{PKG_REL_NS}"><Relationship Id="rId1" '
            f'Type="{R_NS}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'
        ).encode(),
        "ppt/presentation.xml": presentation,
        "ppt/_rels/presentation.xml.rels": presentation_rels,
        **{f"ppt/slides/slide{n}.xml": _slide(n) for n in range(1, 7)},
        "ppt/slides/_rels/slide3.xml.rels": (
            f'<Relationships xmlns="{PKG_REL_NS}">'
            f'<Relationship Id="rIdNotes" Type="{R_NS}/notesSlide" Target="../notesSlides/notesSlide3.xml"/>'
            f'<Relationship Id="rIdChart" Type="{R_NS}/chart" Target="../charts/chart1.xml"/>'
            + ('<Relationship Id="rIdExt" Type="urn:external" Target="https://example.invalid/" TargetMode="External"/>'
               if external_link else "")
            + "</Relationships>"
        ).encode(),
        "ppt/notesSlides/notesSlide3.xml": b"<notes>Preserve notes content unchanged</notes>",
        "ppt/charts/chart1.xml": b"<chart>Preserve chart content unchanged</chart>",
        "custom/payload.bin": bytes(range(64)),
    }
    if nested_zip:
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("untrusted/inner.txt", b"Do not open recursively")
        parts["ppt/embeddings/opaque.bin"] = nested.getvalue()
    if signed:
        parts["_xmlsignatures/sig1.xml"] = b"signature payload"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b"container comment"
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class PresentationOfficeTests(unittest.TestCase):
    def test_inspection_lists_text_capability_and_clear_unsupported_objects(self):
        inspected = inspect_presentation(_pptx())
        self.assertEqual([slide["number"] for slide in inspected["slides"]], list(range(1, 7)))
        self.assertTrue(inspected["slides"][0]["objects"][0]["editable"])
        self.assertTrue(inspected["slides"][1]["objects"][0]["editable"])
        slide3 = inspected["slides"][2]
        objects = {item["id"]: item for item in slide3["objects"]}
        selected_id = "slide-3-shape-2"
        self.assertEqual(objects[selected_id]["text"], "旧标题")
        self.assertTrue(objects[selected_id]["editable"])
        self.assertEqual(objects["slide-3-shape-3"]["text"], "保持原样")
        self.assertIn("多个段落", objects["slide-3-shape-6"]["reason"])
        mixed_style = objects["slide-3-shape-7"]
        self.assertTrue(mixed_style["editable"])
        self.assertEqual(mixed_style["edit_modes"], ["exact_span"])
        self.assertIn("混合字形", mixed_style["reason"])
        self.assertEqual(objects["slide-3-frame-8-r1-c1"]["text"], "可改单元格")
        self.assertTrue(objects["slide-3-frame-8-r1-c1"]["editable"])
        self.assertEqual(objects["slide-3-frame-8-r1-c2"]["text"], "保留单元格")
        self.assertFalse(objects["slide-3-frame-4"]["editable"])
        self.assertFalse(objects["slide-3-picture-5"]["editable"])

    def test_selected_text_changes_only_on_slides_3_to_5_and_all_other_members_match(self):
        original = _pptx(external_link=True)
        original_snapshot = original[:]
        before = _members(original)
        result = revise_presentation(original, [
            {"slide": 3, "object_id": "slide-3-shape-2", "text": "新标题"},
            {"slide": 3, "object_id": "slide-3-frame-8-r1-c1", "text": "新单元格"},
            {"slide": 5, "object_id": "slide-5-shape-2", "text": "已修改第五页"},
        ], allowed_slides=[3, 5])

        self.assertEqual(original, original_snapshot)  # input bytes are read-only
        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(result["sha256"], hashlib.sha256(result["content"]).hexdigest())
        self.assertEqual([change["slide"] for change in result["changes"]], [3, 3, 5])
        after = _members(result["content"])
        changed_member_names = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed_member_names, {"ppt/slides/slide3.xml", "ppt/slides/slide5.xml"})
        self.assertEqual(before["ppt/slides/_rels/slide3.xml.rels"], after["ppt/slides/_rels/slide3.xml.rels"])
        self.assertEqual(before["ppt/notesSlides/notesSlide3.xml"], after["ppt/notesSlides/notesSlide3.xml"])
        self.assertEqual(before["ppt/charts/chart1.xml"], after["ppt/charts/chart1.xml"])
        for name in before:
            if name not in {"ppt/slides/slide3.xml", "ppt/slides/slide5.xml"}:
                self.assertEqual(hashlib.sha256(before[name]).digest(), hashlib.sha256(after[name]).digest(), name)

        reopened = inspect_presentation(result["content"])
        self.assertEqual(reopened["slides"][2]["objects"][0]["text"], "新标题")
        slide3_objects = {item["id"]: item for item in reopened["slides"][2]["objects"]}
        self.assertEqual(slide3_objects["slide-3-frame-8-r1-c1"]["text"], "新单元格")
        self.assertEqual(slide3_objects["slide-3-frame-8-r1-c2"]["text"], "保留单元格")
        self.assertEqual(reopened["slides"][4]["objects"][0]["text"], "已修改第五页")
        self.assertEqual(reopened["slides"][0]["objects"][0]["text"], "旧标题1")
        self.assertEqual(reopened["slides"][1]["objects"][0]["text"], "旧标题2")
        self.assertEqual(reopened["slides"][3]["objects"][0]["text"], "旧标题4")
        self.assertEqual(reopened["slides"][5]["objects"][0]["text"], "旧标题6")
        self.assertTrue(any("外部链接" in warning for warning in result["warnings"]))

    def test_scope_errors_and_noneditable_objects_are_rejected(self):
        original = _pptx()
        with self.assertRaisesRegex(ValueError, "PPTX_CHANGE_OUT_OF_SCOPE"):
            revise_presentation(original, [{"slide": 2, "object_id": "slide-2-shape-2", "text": "越界"}], [3, 4, 5])
        with self.assertRaisesRegex(ValueError, "PPTX_ALLOWED_SLIDES_INVALID"):
            revise_presentation(original, [], [0, 3, 4, 5])
        with self.assertRaisesRegex(ValueError, "PPTX_ALLOWED_SLIDES_DUPLICATED"):
            revise_presentation(original, [], [3, 3])
        with self.assertRaisesRegex(ValueError, "PPTX_ALLOWED_SLIDE_NOT_FOUND"):
            revise_presentation(original, [], [7])
        with self.assertRaisesRegex(ValueError, "PPTX_OBJECT_NOT_EDITABLE"):
            revise_presentation(original, [{"slide": 3, "object_id": "slide-3-frame-4", "text": "改图表"}], [3, 4, 5])
        with self.assertRaisesRegex(ValueError, "PPTX_OBJECT_NOT_EDITABLE"):
            revise_presentation(original, [{"slide": 3, "object_id": "slide-3-shape-6", "text": "合并段落"}], [3, 4, 5])
        with self.assertRaisesRegex(ValueError, "PPTX_MIXED_STYLE_REQUIRES_EXACT_SPAN"):
            revise_presentation(original, [{"slide": 3, "object_id": "slide-3-shape-7", "text": "合并样式"}], [3, 4, 5])
        with self.assertRaisesRegex(ValueError, "PPTX_TEXT_NEWLINE_UNSUPPORTED"):
            revise_presentation(original, [{"slide": 3, "object_id": "slide-3-shape-2", "text": "新\n标题"}], [3, 4, 5])

    def test_same_style_runs_keep_run_styles_and_preserve_boundary_whitespace(self):
        original = _pptx()
        original_slide = ET.fromstring(_members(original)["ppt/slides/slide3.xml"])
        result = revise_presentation(original, [
            {"slide": 3, "object_id": "slide-3-shape-2", "text": "  新标题  "},
        ], [3])
        revised_slide = ET.fromstring(_members(result["content"])["ppt/slides/slide3.xml"])

        def shape_runs(root):
            shape = next(node for node in root.iter(f"{{{P_NS}}}sp")
                         if any(prop.tag == f"{{{P_NS}}}cNvPr" and prop.attrib.get("id") == "2"
                                for prop in node.iter()))
            runs = shape.findall(f".//{{{A_NS}}}r")
            styles = [ET.canonicalize(ET.tostring(run.find(f"{{{A_NS}}}rPr"), encoding="unicode"))
                      for run in runs]
            text_nodes = [run.find(f"{{{A_NS}}}t") for run in runs]
            return styles, text_nodes

        original_styles, _ = shape_runs(original_slide)
        revised_styles, revised_text_nodes = shape_runs(revised_slide)
        self.assertEqual(revised_styles, original_styles)
        self.assertEqual(revised_text_nodes[0].attrib.get("{http://www.w3.org/XML/1998/namespace}space"), "preserve")
        self.assertEqual(inspect_presentation(result["content"])["slides"][2]["objects"][0]["text"], "  新标题  ")

    def test_exact_span_crossing_mixed_runs_preserves_unselected_text_and_styles(self):
        original = _pptx()
        before = _members(original)
        result = revise_presentation(original, [{
            "slide": 3,
            "object_id": "slide-3-shape-7",
            "before": "体常",
            "after": "品类",
        }], allowed_slides=[3])

        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(result["changes"], [{
            "slide": 3,
            "object_id": "slide-3-shape-7",
            "kind": "shape_text",
            "before": "粗体常规",
            "after": "粗品类规",
            "selection": {"before": "体常", "after": "品类"},
        }])
        after = _members(result["content"])
        self.assertEqual(
            {name for name in before if before[name] != after[name]},
            {"ppt/slides/slide3.xml"},
        )

        reopened = inspect_presentation(result["content"])
        mixed_style = next(
            item for item in reopened["slides"][2]["objects"]
            if item["id"] == "slide-3-shape-7"
        )
        self.assertEqual(mixed_style["text"], "粗品类规")
        root = ET.fromstring(after["ppt/slides/slide3.xml"])
        shape = next(
            item for item in root.iter(f"{{{P_NS}}}sp")
            if any(prop.tag == f"{{{P_NS}}}cNvPr" and prop.attrib.get("id") == "7"
                   for prop in item.iter())
        )
        runs = shape.findall(f".//{{{A_NS}}}r")
        self.assertEqual([run.find(f"{{{A_NS}}}t").text or "" for run in runs], ["粗品类", "规"])
        self.assertEqual(runs[0].find(f"{{{A_NS}}}rPr").attrib.get("b"), "1")
        self.assertIsNone(runs[1].find(f"{{{A_NS}}}rPr").attrib.get("b"))

    def test_mixed_style_whole_text_replacement_requires_an_exact_span(self):
        original = _pptx()
        with self.assertRaisesRegex(ValueError, "PPTX_MIXED_STYLE_REQUIRES_EXACT_SPAN"):
            revise_presentation(original, [{
                "slide": 3,
                "object_id": "slide-3-shape-7",
                "text": "完整改写",
            }], allowed_slides=[3])

    def test_user_can_select_pages_1_and_6_with_only_those_pages_changed(self):
        original = _pptx()
        before = _members(original)
        result = revise_presentation(original, [
            {"slide": 1, "object_id": "slide-1-shape-2", "text": "首页更新"},
            {"slide": 6, "object_id": "slide-6-shape-2", "text": "末页更新"},
        ], [1, 6])
        after = _members(result["content"])
        self.assertTrue(result["unchanged_verified"])
        self.assertEqual({name for name in before if before[name] != after[name]},
                         {"ppt/slides/slide1.xml", "ppt/slides/slide6.xml"})
        reopened = inspect_presentation(result["content"])
        self.assertEqual(reopened["slides"][0]["objects"][0]["text"], "首页更新")
        self.assertEqual(reopened["slides"][5]["objects"][0]["text"], "末页更新")
        self.assertEqual(reopened["slides"][1]["objects"][0]["text"], "旧标题2")

    def test_external_targets_and_nested_zip_are_not_opened(self):
        original = _pptx(external_link=True, nested_zip=True)
        result = inspect_presentation(original)
        self.assertTrue(any("外部链接" in warning for warning in result["warnings"]))
        self.assertTrue(any("嵌入内容" in warning for warning in result["warnings"]))
        edited = revise_presentation(original, [
            {"slide": 3, "object_id": "slide-3-shape-2", "text": "文本替换"},
        ], [3])
        self.assertEqual(_members(original)["ppt/embeddings/opaque.bin"],
                         _members(edited["content"])["ppt/embeddings/opaque.bin"])

    def test_unsafe_xml_archives_and_signed_packages_fail_closed(self):
        dtd = _pptx()
        parts = _members(dtd)
        parts["ppt/slides/slide3.xml"] = b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///secret'>]>" + parts["ppt/slides/slide3.xml"]
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as archive:
            for name, payload in parts.items():
                archive.writestr(name, payload)
        with self.assertRaisesRegex(ValueError, "PPTX_XML_UNSAFE_OR_TOO_LARGE"):
            inspect_presentation(bad.getvalue())

        traversal = io.BytesIO()
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape.txt", b"no")
        with self.assertRaisesRegex(ValueError, "PPTX_MEMBER_PATH_UNSAFE"):
            inspect_presentation(traversal.getvalue())

        signed = _pptx(signed=True)
        with self.assertRaisesRegex(ValueError, "PPTX_SIGNED_PACKAGE_EDIT_UNSUPPORTED"):
            revise_presentation(signed, [{"slide": 3, "object_id": "slide-3-shape-2", "text": "改"}], [3])

    def test_expanded_member_limit_is_enforced_before_unbounded_read(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("large.bin", b"A" * 1024)
        with patch("oevira.office.MAX_MEMBER_BYTES", 128):
            with self.assertRaisesRegex(ValueError, "PPTX_MEMBER_SIZE_LIMIT"):
                inspect_presentation(archive.getvalue())

    def test_corrupt_package_and_invalid_xml_are_reported(self):
        with self.assertRaisesRegex(ValueError, "PPTX_PACKAGE_INVALID"):
            inspect_presentation(b"not a zip")
        package = _members(_pptx())
        package["ppt/slides/slide3.xml"] = b"<broken"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            for name, payload in package.items():
                output.writestr(name, payload)
        with self.assertRaisesRegex(ValueError, "PPTX_XML_INVALID:ppt/slides/slide3.xml"):
            inspect_presentation(archive.getvalue())


if __name__ == "__main__":
    unittest.main()
