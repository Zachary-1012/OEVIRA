from __future__ import annotations

import hashlib
import io
import copy
from pathlib import Path
import tempfile
import unittest
import zipfile
import uuid
from xml.etree import ElementTree as ET

from PIL import Image
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from oevira.office import inspect_presentation, revise_presentation
from oevira import create_app
from oevira.api import ApiError
from oevira.auth import issue_local_capability, utc_now
from oevira.store import Store


BASE_URL = "http://127.0.0.1:8323"


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _png(color: tuple[int, int, int], size: tuple[int, int] = (40, 24)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def _grouped_presentation(image: bytes, *, repeat_same_image: bool = True) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(io.BytesIO(image), Inches(1), Inches(1), width=Inches(2), height=Inches(1))
    slide.shapes.add_picture(io.BytesIO(image if repeat_same_image else _png((0, 0, 255))),
                             Inches(4), Inches(1), width=Inches(2), height=Inches(1))
    output = io.BytesIO()
    presentation.save(output)
    source = output.getvalue()

    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
        infos = [info for info in archive.infolist()]
        comment = archive.comment
    ET.register_namespace("p", P_NS)
    ET.register_namespace("a", A_NS)
    ET.register_namespace("r", R_NS)
    root = ET.fromstring(parts["ppt/slides/slide1.xml"])
    shape_tree = root.find(f".//{{{P_NS}}}spTree")
    pictures = shape_tree.findall(f"{{{P_NS}}}pic")
    group = ET.Element(f"{{{P_NS}}}grpSp")
    nv = ET.SubElement(group, f"{{{P_NS}}}nvGrpSpPr")
    ids = [int(node.get("id", "1")) for node in root.iter() if node.tag.endswith("}cNvPr")]
    ET.SubElement(nv, f"{{{P_NS}}}cNvPr", {"id": str(max(ids) + 1), "name": "Nested image group"})
    ET.SubElement(nv, f"{{{P_NS}}}cNvGrpSpPr")
    ET.SubElement(nv, f"{{{P_NS}}}nvPr")
    group_properties = ET.SubElement(group, f"{{{P_NS}}}grpSpPr")
    transform = ET.SubElement(group_properties, f"{{{A_NS}}}xfrm")
    ET.SubElement(transform, f"{{{A_NS}}}off", {"x": "0", "y": "0"})
    ET.SubElement(transform, f"{{{A_NS}}}ext", {"cx": "9144000", "cy": "6858000"})
    ET.SubElement(transform, f"{{{A_NS}}}chOff", {"x": "0", "y": "0"})
    ET.SubElement(transform, f"{{{A_NS}}}chExt", {"cx": "9144000", "cy": "6858000"})
    for picture in pictures:
        shape_tree.remove(picture)
        group.append(picture)
    shape_tree.insert(2, group)
    parts["ppt/slides/slide1.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    grouped = io.BytesIO()
    with zipfile.ZipFile(grouped, "w") as archive:
        archive.comment = comment
        for info in infos:
            archive.writestr(info, parts[info.filename])
    return grouped.getvalue()


def _charted_presentation(*, duplicate_shared_chart: bool = False,
                          group_charts: bool = False) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["North", "South", "East"]
    data.add_series("Revenue", [2, 3, 4])
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1),
                           Inches(6), Inches(4), data)
    output = io.BytesIO()
    presentation.save(output)
    source = output.getvalue()
    if not duplicate_shared_chart and not group_charts:
        return source

    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
        infos = list(archive.infolist())
        comment = archive.comment
    root = etree.fromstring(parts["ppt/slides/slide1.xml"])
    shape_tree = root.find(f".//{{{P_NS}}}spTree")
    frames = shape_tree.findall(f"{{{P_NS}}}graphicFrame")
    if len(frames) != 1:
        raise AssertionError("the chart fixture must contain one original chart frame")
    if duplicate_shared_chart:
        duplicate = copy.deepcopy(frames[0])
        properties = duplicate.find(f".//{{{P_NS}}}cNvPr")
        properties.set("id", "999")
        properties.set("name", "Shared chart copy")
        shape_tree.append(duplicate)
        frames.append(duplicate)
    if group_charts:
        ids = [int(node.get("id", "1")) for node in root.iter()
               if node.tag.endswith("}cNvPr")]
        group = etree.Element(f"{{{P_NS}}}grpSp")
        nonvisual = etree.SubElement(group, f"{{{P_NS}}}nvGrpSpPr")
        etree.SubElement(nonvisual, f"{{{P_NS}}}cNvPr", {
            "id": str(max(ids) + 1), "name": "Chart data group",
        })
        etree.SubElement(nonvisual, f"{{{P_NS}}}cNvGrpSpPr")
        etree.SubElement(nonvisual, f"{{{P_NS}}}nvPr")
        group_properties = etree.SubElement(group, f"{{{P_NS}}}grpSpPr")
        transform = etree.SubElement(group_properties,
                                     "{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
        etree.SubElement(transform, "{http://schemas.openxmlformats.org/drawingml/2006/main}off",
                         {"x": "914400", "y": "914400"})
        etree.SubElement(transform, "{http://schemas.openxmlformats.org/drawingml/2006/main}ext",
                         {"cx": "5486400", "cy": "3657600"})
        etree.SubElement(transform, "{http://schemas.openxmlformats.org/drawingml/2006/main}chOff",
                         {"x": "0", "y": "0"})
        etree.SubElement(transform, "{http://schemas.openxmlformats.org/drawingml/2006/main}chExt",
                         {"cx": "5486400", "cy": "3657600"})
        for frame in frames:
            shape_tree.remove(frame)
            group.append(frame)
        shape_tree.append(group)
    parts["ppt/slides/slide1.xml"] = etree.tostring(
        root, encoding="UTF-8", xml_declaration=True, standalone=True,
    )
    grouped = io.BytesIO()
    with zipfile.ZipFile(grouped, "w") as archive:
        archive.comment = comment
        for info in infos:
            archive.writestr(info, parts[info.filename])
    return grouped.getvalue()


def _members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _chart_shapes(shapes):
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.CHART:
            yield shape
        elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _chart_shapes(shape.shapes)


class PresentationImageRevisionTests(unittest.TestCase):
    def test_selected_nested_picture_replacement_preserves_shared_image_geometry_and_sibling(self) -> None:
        old_image = _png((230, 10, 10))
        replacement = _png((10, 190, 20), (80, 48))
        original = _grouped_presentation(old_image)
        before = inspect_presentation(original)
        objects = before["slides"][0]["objects"]
        pictures = [item for item in objects if item["kind"] == "picture"]
        self.assertEqual(len(pictures), 2)
        self.assertTrue(all(item["editable"] for item in pictures))
        self.assertTrue(pictures[0]["group_ancestors"])
        self.assertEqual(pictures[0]["media"]["sha256"], pictures[1]["media"]["sha256"])
        original_media_parts = {name: payload for name, payload in _members(original).items()
                                if name.startswith("ppt/media/")}

        source = {"kind": "material", "material_id": "replacement-image",
                  "filename": "replacement.png", "sha256": hashlib.sha256(replacement).hexdigest(),
                  "size_bytes": len(replacement), "format": "png"}
        result = revise_presentation(original, [{
            "slide": 1,
            "object_id": pictures[0]["id"],
            "replace_image": {"content": replacement, "filename": "replacement.png",
                              "source": source, "alt_text": "New image description"},
        }], [1])
        self.assertTrue(result["unchanged_verified"])
        self.assertEqual(result["changes"][0]["kind"], "picture_replacement")
        self.assertEqual(result["changes"][0]["after"]["sha256"], source["sha256"])
        self.assertTrue(result["changes"][0]["geometry_preserved"])
        self.assertTrue(result["changes"][0]["crop_preserved"])

        after = inspect_presentation(result["content"])
        revised_pictures = [item for item in after["slides"][0]["objects"] if item["kind"] == "picture"]
        self.assertEqual(revised_pictures[0]["media"]["sha256"], source["sha256"])
        self.assertEqual(revised_pictures[0]["media"]["dimensions"], [80, 48])
        self.assertEqual(revised_pictures[0]["text"], "New image description")
        self.assertEqual(revised_pictures[0]["geometry"], pictures[0]["geometry"])
        self.assertEqual(revised_pictures[0]["crop"], pictures[0]["crop"])
        self.assertEqual(revised_pictures[0]["group_ancestors"], pictures[0]["group_ancestors"])
        self.assertEqual(revised_pictures[1]["media"]["sha256"], pictures[1]["media"]["sha256"])

        after_parts = _members(result["content"])
        for name, payload in original_media_parts.items():
            self.assertEqual(after_parts[name], payload, name)
        new_media = [payload for name, payload in after_parts.items()
                     if name.startswith("ppt/media/") and name not in original_media_parts]
        self.assertEqual(new_media, [replacement])

        reopened = Presentation(io.BytesIO(result["content"]))
        group = next(shape for shape in reopened.slides[0].shapes
                     if shape.shape_type == MSO_SHAPE_TYPE.GROUP)
        child_pictures = [shape for shape in group.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        self.assertEqual(len(child_pictures), 2)
        self.assertEqual(child_pictures[0].image.blob, replacement)
        self.assertEqual(child_pictures[1].image.blob, old_image)


class PresentationChartRevisionTests(unittest.TestCase):
    @staticmethod
    def _replacement() -> dict:
        return {
            "categories": [
                {"kind": "text", "value": "West"},
                {"kind": "text", "value": "South"},
                {"kind": "text", "value": "East"},
            ],
            "series": [{
                "name": "Revenue",
                "values": [
                    {"kind": "number", "value": "5"},
                    {"kind": "number", "value": "3"},
                    {"kind": "number", "value": "4"},
                ],
            }],
        }

    def test_selected_chart_data_revises_embedded_workbook_and_caches_with_copy_on_write(self) -> None:
        original = _charted_presentation(duplicate_shared_chart=True, group_charts=True)
        original_parts = _members(original)
        source_chart_parts = {name: payload for name, payload in original_parts.items()
                              if name.startswith("ppt/charts/chart")}
        source_workbooks = {name: payload for name, payload in original_parts.items()
                            if name.startswith("ppt/embeddings/")}
        original_objects = [item for item in inspect_presentation(original)["slides"][0]["objects"]
                            if item["kind"] == "chart"]
        self.assertEqual(len(original_objects), 2)
        self.assertEqual(original_objects[0]["chart_data"], original_objects[1]["chart_data"])
        self.assertTrue(original_objects[0]["group_ancestors"])
        self.assertEqual(original_objects[0]["group_ancestors"],
                         original_objects[1]["group_ancestors"])

        result = revise_presentation(original, [{
            "slide": 1, "object_id": original_objects[0]["id"],
            "chart_data": self._replacement(),
        }], [1])
        self.assertTrue(result["unchanged_verified"])
        change = result["changes"][0]
        self.assertEqual(change["kind"], "chart_data_replacement")
        self.assertTrue(change["copy_on_write"])
        self.assertTrue(change["geometry_preserved"])
        self.assertEqual(change["calculation_engine"], "oevira-xlsx-formula")
        self.assertEqual(change["calculation_revision"], "2026-11-10.13")

        updated_objects = [item for item in inspect_presentation(result["content"])["slides"][0]["objects"]
                           if item["kind"] == "chart"]
        self.assertEqual(updated_objects[0]["chart_data"]["categories"][0]["value"], "West")
        self.assertEqual(updated_objects[0]["chart_data"]["series"][0]["values"][0]["value"], "5")
        self.assertEqual(updated_objects[1]["chart_data"], original_objects[1]["chart_data"])
        self.assertEqual(updated_objects[0]["group_ancestors"], original_objects[0]["group_ancestors"])
        self.assertEqual(updated_objects[1]["group_ancestors"], original_objects[1]["group_ancestors"])

        updated_parts = _members(result["content"])
        for name, payload in source_chart_parts.items():
            self.assertEqual(updated_parts[name], payload, name)
        for name, payload in source_workbooks.items():
            self.assertEqual(updated_parts[name], payload, name)

        new_workbook_part = next(name for name in result["added_parts"]
                                 if name.startswith("ppt/embeddings/") and name.endswith(".xlsx"))
        with io.BytesIO(updated_parts[new_workbook_part]) as workbook_bytes:
            from openpyxl import load_workbook

            workbook = load_workbook(workbook_bytes, data_only=True, read_only=True)
            sheet = workbook["Sheet1"]
            self.assertEqual([sheet.cell(row, 1).value for row in range(2, 5)],
                             ["West", "South", "East"])
            self.assertEqual([sheet.cell(row, 2).value for row in range(2, 5)],
                             [5, 3, 4])
            workbook.close()

        reopened = Presentation(io.BytesIO(result["content"]))
        charts = [shape.chart for shape in _chart_shapes(reopened.slides[0].shapes)]
        self.assertEqual(len(charts), 2)
        self.assertEqual(list(charts[0].series[0].values), [5, 3, 4])
        self.assertEqual([item.label for item in charts[0].plots[0].categories],
                         ["West", "South", "East"])
        self.assertEqual(list(charts[1].series[0].values), [2, 3, 4])


class PresentationImageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-ppt-image-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8323)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request("post", "/api/local-session",
                                json={"capability": capability_file.read_text(encoding="ascii"),
                                      "display_name": "PPT审阅者"},
                                headers={"Origin": BASE_URL})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, filename: str, content: bytes, *, work_id: str | None = None) -> dict:
        form = {"title": filename, "file": (io.BytesIO(content), filename)}
        if work_id is not None:
            form["work_id"] = work_id
        response = self.request("post", "/api/materials",
                                data=form,
                                headers=self.auth_headers(), content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_commission_work(self, commission_name: str, work_title: str,
                                commission_id: str | None = None) -> tuple[str, str]:
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()
        if commission_id is None:
            commission_id = str(uuid.uuid4())
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                    (commission_id, workspace_id, commission_name, user_id, now, now),
                )
                connection.execute(
                    "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'owner',1)",
                    (commission_id, user_id),
                )
        work_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'{}','active',1,?,?)",
                (work_id, workspace_id, user_id, work_title, "验证演示文稿素材客户范围", now, now),
            )
            connection.execute(
                "INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)",
                (work_id, commission_id, now),
            )
        return commission_id, work_id

    def test_selected_picture_material_replacement_reopens_and_recovers_without_touching_shared_source(self) -> None:
        original_image = _png((230, 10, 10))
        first_replacement = _png((10, 190, 20), (80, 48))
        second_replacement = _png((10, 30, 220), (64, 64))
        original_deck = _grouped_presentation(original_image)
        deck = self.upload("deck.pptx", original_deck)
        first_asset = self.upload("replacement-green.png", first_replacement)
        second_asset = self.upload("replacement-blue.png", second_replacement)

        inspected = self.request("get", f"/api/materials/{deck['id']}/presentation",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        pictures = [item for item in inspected.json["data"]["slides"][0]["objects"]
                    if item["kind"] == "picture"]
        self.assertEqual(len(pictures), 2)
        self.assertTrue(pictures[0]["editable"])

        initial = self.request("post", f"/api/materials/{deck['id']}/presentation-revisions",
                               json={"allowed_slides": [1],
                                     "changes": [{"slide": 1, "object_id": pictures[0]["id"],
                                                  "image_material_id": first_asset["id"],
                                                  "alt_text": "Green campaign artwork"}]},
                               headers={**self.auth_headers(), "Idempotency-Key": "ppt-image-initial-001"})
        self.assertEqual(initial.status_code, 201, initial.get_data(as_text=True))
        artifact_id = initial.json["data"]["artifact"]["id"]
        initial_snapshot = initial.json["data"]["artifact"]["input_snapshot"]
        self.assertIn(first_asset["id"], {item["id"] for item in initial_snapshot["materials"]})
        initial_page = self.request("get", f"/api/artifacts/{artifact_id}/presentation?version=1",
                                    headers=self.auth_headers())
        self.assertEqual(initial_page.status_code, 200, initial_page.get_data(as_text=True))
        revised_pictures = [item for item in initial_page.json["data"]["slides"][0]["objects"]
                            if item["kind"] == "picture"]
        self.assertEqual(revised_pictures[0]["media"]["sha256"], hashlib.sha256(first_replacement).hexdigest())
        self.assertEqual(revised_pictures[0]["text"], "Green campaign artwork")
        self.assertEqual(revised_pictures[1]["media"]["sha256"], hashlib.sha256(original_image).hexdigest())

        handler = self.app.extensions["oevira.revision_handlers"]["presentation"]
        pure_result = handler(self.store, self.identity, artifact_id,
                              {"base_version": 1, "allowed_slides": [1],
                               "changes": [{"slide": 1, "object_id": revised_pictures[1]["id"],
                                            "image_material_id": second_asset["id"]}]},
                              "ppt-image-pure-followup-002")
        self.assertEqual(pure_result["artifact"]["version"], 2)
        version_two = self.request("get", f"/api/artifacts/{artifact_id}/presentation?version=2",
                                   headers=self.auth_headers())
        self.assertEqual(version_two.status_code, 200, version_two.get_data(as_text=True))
        version_two_pictures = [item for item in version_two.json["data"]["slides"][0]["objects"]
                                if item["kind"] == "picture"]
        self.assertEqual(version_two_pictures[0]["media"]["sha256"], hashlib.sha256(first_replacement).hexdigest())
        self.assertEqual(version_two_pictures[1]["media"]["sha256"], hashlib.sha256(second_replacement).hexdigest())

        export = self.store.row("SELECT relative_path FROM exports WHERE artifact_id = ? AND version = 2 AND format = 'pptx'",
                                (artifact_id,))
        export_path = self.root / export["relative_path"]
        export_path.unlink()
        recovered = self.request("get", f"/api/artifacts/{artifact_id}/presentation?version=2",
                                 headers=self.auth_headers())
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        recovered_file = self.request("get", recovered.json["data"]["download_url"],
                                      headers=self.auth_headers())
        self.assertEqual(recovered_file.status_code, 200, recovered_file.get_data()[:100])
        opened = Presentation(io.BytesIO(recovered_file.data))
        group = next(shape for shape in opened.slides[0].shapes if shape.shape_type == MSO_SHAPE_TYPE.GROUP)
        child_pictures = [shape for shape in group.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        self.assertEqual(child_pictures[0].image.blob, first_replacement)
        self.assertEqual(child_pictures[1].image.blob, second_replacement)

        original_download = self.request("get", f"/api/materials/{deck['id']}/file",
                                         headers=self.auth_headers())
        first_asset_download = self.request("get", f"/api/materials/{first_asset['id']}/file",
                                            headers=self.auth_headers())
        self.assertEqual(original_download.data, original_deck)
        self.assertEqual(first_asset_download.data, first_replacement)

    def test_chart_data_revision_reopens_recovers_and_downloads_with_shared_chart_intact(self) -> None:
        original_deck = _charted_presentation(duplicate_shared_chart=True, group_charts=True)
        material = self.upload("chart-deck.pptx", original_deck)
        inspected = self.request("get", f"/api/materials/{material['id']}/presentation",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        charts = [item for item in inspected.json["data"]["slides"][0]["objects"]
                  if item["kind"] == "chart"]
        self.assertEqual(len(charts), 2)
        self.assertIn("replace_chart_data", charts[0]["edit_modes"])

        created = self.request(
            "post", f"/api/materials/{material['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [{
                "slide": 1, "object_id": charts[0]["id"],
                "chart_data": PresentationChartRevisionTests._replacement(),
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "ppt-chart-cow-initial-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        artifact_id = created.json["data"]["artifact"]["id"]
        first_page = self.request("get", f"/api/artifacts/{artifact_id}/presentation?version=1",
                                  headers=self.auth_headers())
        self.assertEqual(first_page.status_code, 200, first_page.get_data(as_text=True))
        updated_charts = [item for item in first_page.json["data"]["slides"][0]["objects"]
                          if item["kind"] == "chart"]
        self.assertEqual(updated_charts[0]["chart_data"]["categories"][0]["value"], "West")
        self.assertEqual(updated_charts[1]["chart_data"]["series"][0]["values"][0]["value"], "2")

        followup = {
            "categories": PresentationChartRevisionTests._replacement()["categories"],
            "series": [{
                "name": "Revenue",
                "values": [
                    {"kind": "number", "value": "7"},
                    {"kind": "number", "value": "3"},
                    {"kind": "number", "value": "4"},
                ],
            }],
        }
        handler = self.app.extensions["oevira.revision_handlers"]["presentation"]
        second = handler(self.store, self.identity, artifact_id,
                         {"base_version": 1, "allowed_slides": [1], "changes": [{
                             "slide": 1, "object_id": updated_charts[0]["id"],
                             "chart_data": followup,
                         }]}, "ppt-chart-cow-pure-followup-002")
        self.assertEqual(second["artifact"]["version"], 2)
        export = self.store.row(
            "SELECT relative_path FROM exports WHERE artifact_id = ? AND version = 2 AND format = 'pptx'",
            (artifact_id,),
        )
        (self.root / export["relative_path"]).unlink()

        cold_app = create_app(self.root, expected_port=8323)
        cold_app.config.update(TESTING=True)
        cold_client = cold_app.test_client()
        recovered = cold_client.get(
            f"/api/artifacts/{artifact_id}/presentation?version=2", base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        download = cold_client.get(recovered.json["data"]["download_url"], base_url=BASE_URL,
                                   headers=self.auth_headers())
        recovered.close()
        self.assertEqual(download.status_code, 200)
        reopened = Presentation(io.BytesIO(download.data))
        charts = [shape.chart for shape in _chart_shapes(reopened.slides[0].shapes)]
        self.assertEqual(list(charts[0].series[0].values), [7, 3, 4])
        self.assertEqual([item.label for item in charts[0].plots[0].categories],
                         ["West", "South", "East"])
        self.assertEqual(list(charts[1].series[0].values), [2, 3, 4])
        original_download = self.request("get", f"/api/materials/{material['id']}/file",
                                         headers=self.auth_headers())
        self.assertEqual(original_download.data, original_deck)
        download.close()

    def test_presentation_nested_image_access_rechecks_client_scope_target_work_and_revocation(self) -> None:
        client_a, work_a1 = self.create_commission_work("客户甲", "甲客户工作一")
        _, work_a2 = self.create_commission_work("客户甲", "甲客户工作二", client_a)
        client_b, work_b = self.create_commission_work("客户乙", "乙客户工作")
        original_image = _png((200, 10, 10))
        same_client_image_bytes = _png((10, 200, 10))
        other_client_image_bytes = _png((10, 10, 200))
        deck = self.upload("client-deck.pptx", _grouped_presentation(original_image),
                           work_id=work_a1)
        same_client_image = self.upload("same-client.png", same_client_image_bytes,
                                         work_id=work_a2)
        other_client_image = self.upload("other-client.png", other_client_image_bytes,
                                         work_id=work_b)

        inspected = self.request("get", f"/api/materials/{deck['id']}/presentation",
                                 headers=self.auth_headers())
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        picture = next(item for item in inspected.json["data"]["slides"][0]["objects"]
                       if item["kind"] == "picture")
        cross_client = self.request(
            "post", f"/api/materials/{deck['id']}/presentation-revisions",
            json={"work_id": work_a1, "allowed_slides": [1], "changes": [{
                "slide": 1, "object_id": picture["id"],
                "image_material_id": other_client_image["id"],
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "ppt-image-cross-client-001"},
        )
        self.assertEqual(cross_client.status_code, 403, cross_client.get_data(as_text=True))
        self.assertEqual(cross_client.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

        same_client = self.request(
            "post", f"/api/materials/{deck['id']}/presentation-revisions",
            json={"work_id": work_a1, "allowed_slides": [1], "changes": [{
                "slide": 1, "object_id": picture["id"],
                "image_material_id": same_client_image["id"],
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "ppt-image-same-client-002"},
        )
        self.assertEqual(same_client.status_code, 201, same_client.get_data(as_text=True))
        artifact_id = same_client.json["data"]["artifact"]["id"]
        self.assertEqual(same_client.json["data"]["artifact"]["work_id"], work_a1)
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET work_id=? WHERE id=?",
                               (work_b, same_client_image["id"]))
        cached_export = self.request(
            "get", f"/api/exports/{same_client.json['data']['file']['id']}/file",
            headers=self.auth_headers(),
        )
        self.assertEqual(cached_export.status_code, 403, cached_export.get_data(as_text=True))
        self.assertEqual(cached_export.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET work_id=? WHERE id=?",
                               (work_a2, same_client_image["id"]))

        unscoped_deck = self.upload("unscoped-deck.pptx", _grouped_presentation(original_image))
        unscoped_image = self.upload("unscoped.png", _png((30, 180, 180)))
        unscoped_inspection = self.request(
            "get", f"/api/materials/{unscoped_deck['id']}/presentation",
            headers=self.auth_headers(),
        )
        unscoped_picture = next(
            item for item in unscoped_inspection.json["data"]["slides"][0]["objects"]
            if item["kind"] == "picture"
        )
        unscoped_artifact = self.request(
            "post", f"/api/materials/{unscoped_deck['id']}/presentation-revisions",
            json={"allowed_slides": [1], "changes": [{
                "slide": 1, "object_id": unscoped_picture["id"],
                "image_material_id": unscoped_image["id"],
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "ppt-image-unscoped-003"},
        )
        self.assertEqual(unscoped_artifact.status_code, 201, unscoped_artifact.get_data(as_text=True))
        unscoped_id = unscoped_artifact.json["data"]["artifact"]["id"]
        self.assertIsNone(unscoped_artifact.json["data"]["artifact"]["work_id"])

        handler = self.app.extensions["oevira.revision_handlers"]["presentation"]
        with self.assertRaises(ApiError) as no_scope:
            handler(self.store, self.identity, unscoped_id, {
                "base_version": 1, "allowed_slides": [1], "changes": [{
                    "slide": 1, "object_id": unscoped_picture["id"],
                    "image_material_id": other_client_image["id"],
                }],
            }, "ppt-image-private-to-no-work-004")
        self.assertEqual(no_scope.exception.code, "CLIENT_SCOPE_MISMATCH")

        saved_page = self.request(
            "get", f"/api/artifacts/{artifact_id}/presentation?version=1",
            headers=self.auth_headers(),
        )
        self.assertEqual(saved_page.status_code, 200, saved_page.get_data(as_text=True))
        current_pictures = [item for item in saved_page.json["data"]["slides"][0]["objects"]
                            if item["kind"] == "picture"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE commission_members SET active=0 WHERE commission_id=? AND user_id=?",
                (client_a, self.identity["user"]["id"]),
            )
        with self.assertRaises(ApiError) as revoked:
            handler(self.store, self.identity, artifact_id, {
                "base_version": 1, "allowed_slides": [1], "changes": [{
                    "slide": 1, "object_id": current_pictures[0]["id"],
                    "image_material_id": same_client_image["id"],
                }],
            }, "ppt-image-revoked-actor-005")
        self.assertEqual(revoked.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
