from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from oevira import create_app
from oevira.auth import issue_local_capability


BASE_URL = "http://127.0.0.1:8333"

_CREATE = {
    "method_id": "catalog:comparative-positioning",
    "method_version": "case-2026.03",
    "case_version": "case-2026.03.1",
    "problem": "在既有竞品与渠道约束下，比较本品牌的可核定位。",
    "declined_parts": ["把审美判断当作识别率"],
    "conditions": ["仅在已编码的原始回答范围内"],
    "counterexamples": ["无原始回答时不得报实测识别"],
    "target_objects": [{"kind": "brand", "id": "brand-a", "label": "本品牌"}],
    "source_refs": [],
    "rights": {"training_rights": False},
    "parameters": {"window": "2026-Q1", "market": "国内一线城市"},
}


class MethodApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-method-app-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8333)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        self.token = None
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "方法采用测试"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("Origin", BASE_URL)
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"
        kwargs["headers"] = headers
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def create_work(self, title: str = "方法采用工作"):
        response = self.request(
            "post", "/api/works",
            json={"title": title, "goal": "记录方法采用而不只返回目录编号"},
            headers={"Idempotency-Key": f"work-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_artifact(self, work_id: str, *, title: str = "分析稿", body: str = "当前分析正文。"):
        response = self.request(
            "post", "/api/artifacts",
            json={"title": title, "body": body, "citations": [], "work_id": work_id},
            headers={"Idempotency-Key": f"artifact-{title}-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_application(self, artifact_id: str, *, key: str | None = None, extra: dict | None = None, expected=201):
        payload = dict(_CREATE)
        if extra:
            payload.update(extra)
        response = self.request(
            "post", f"/api/artifacts/{artifact_id}/method-applications",
            json=payload,
            headers={"Idempotency-Key": key or f"method-app-{uuid.uuid4()}"},
        )
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response

    def test_create_is_recommended_and_catalog_id_alone_is_not_used_in_work(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        created = self.create_application(artifact["id"])
        data = created.json["data"]
        self.assertEqual(data["state"], "recommended")
        self.assertEqual(data["method_id"], _CREATE["method_id"])
        self.assertEqual(data["method_version"], _CREATE["method_version"])
        self.assertEqual(data["problem"], _CREATE["problem"])
        self.assertEqual(data["parameters"]["window"], "2026-Q1")
        self.assertFalse(data["used_in_work"])
        # A catalog id plus a version is still only a recommendation.
        listed = self.request("get", f"/api/artifacts/{artifact['id']}/method-applications")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        items = listed.json["data"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], data["id"])
        self.assertFalse(items[0]["used_in_work"])
        got = self.request("get", f"/api/artifacts/{artifact['id']}/method-applications/{data['id']}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json["data"]["state"], "recommended")
        self.assertFalse(got.json["data"]["used_in_work"])
        # The artifact snapshot is not rewritten at recommendation.
        current = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(current.json["data"]["version"], 1)
        self.assertEqual(current.json["data"]["input_snapshot"].get("method_applications") or [], [])

    def test_create_without_exact_method_version_is_rejected(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        response = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications",
            json={"method_id": "catalog:only-an-id", "problem": "缺少精确版本"},
            headers={"Idempotency-Key": "missing-version"},
        )
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(response.json["error"]["code"], "INVALID_INPUT")

    def test_create_is_idempotent_on_the_same_key(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        first = self.create_application(artifact["id"], key="create-once")
        second = self.create_application(artifact["id"], key="create-once", expected=200)
        self.assertEqual(second.json["data"]["id"], first.json["data"]["id"])
        listed = self.request("get", f"/api/artifacts/{artifact['id']}/method-applications")
        self.assertEqual(len(listed.json["data"]), 1)

    def test_cross_artifact_lookup_is_not_found(self) -> None:
        work = self.create_work()
        first = self.create_artifact(work["id"], title="稿甲")
        second = self.create_artifact(work["id"], title="稿乙")
        application = self.create_application(first["id"]).json["data"]
        missing = self.request("get", f"/api/artifacts/{second['id']}/method-applications/{application['id']}")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json["error"]["code"], "NOT_FOUND")
        preview = self.request(
            "post", f"/api/artifacts/{second['id']}/method-applications/{application['id']}/preview",
            json={},
        )
        self.assertEqual(preview.status_code, 404)

    def test_cannot_skip_recommended_to_formal_result(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        application = self.create_application(artifact["id"]).json["data"]
        skipped = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/formal-result",
            json={"base_artifact_version": artifact["version"]},
        )
        self.assertEqual(skipped.status_code, 409, skipped.get_data(as_text=True))
        self.assertEqual(skipped.json["error"]["code"], "METHOD_APPLICATION_STATE")
        current = self.request("get", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}")
        self.assertEqual(current.json["data"]["state"], "recommended")
        self.assertFalse(current.json["data"]["used_in_work"])

    def test_adopt_requires_preview_and_nonempty_mechanisms(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        application = self.create_application(artifact["id"]).json["data"]
        too_early = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={
                "base_artifact_version": artifact["version"],
                "adopted_mechanisms": ["比较资格与分母一并给出"],
            },
        )
        self.assertEqual(too_early.status_code, 409, too_early.get_data(as_text=True))
        self.assertEqual(too_early.json["error"]["code"], "METHOD_APPLICATION_STATE")
        preview = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/preview",
            json={},
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertEqual(preview.json["data"]["state"], "previewed")
        self.assertFalse(preview.json["data"]["used_in_work"])
        empty = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={"base_artifact_version": artifact["version"], "adopted_mechanisms": []},
        )
        self.assertEqual(empty.status_code, 400, empty.get_data(as_text=True))
        self.assertEqual(empty.json["error"]["code"], "INVALID_INPUT")

    def test_adopt_writes_the_full_record_into_the_artifact_snapshot(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"], body="分析正文保持不变。")
        application = self.create_application(artifact["id"]).json["data"]
        self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/preview",
            json={},
        )
        adopted = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={
                "base_artifact_version": artifact["version"],
                "adopted_mechanisms": ["比较资格与分母一并给出", "未知留在分母"],
                "declined_parts": ["把审美判断当作识别率"],
            },
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        payload = adopted.json["data"]
        record = payload["application"]
        self.assertEqual(record["state"], "adopted")
        self.assertTrue(record["used_in_work"])
        self.assertEqual(record["adopted_mechanisms"], ["比较资格与分母一并给出", "未知留在分母"])
        self.assertEqual(payload["artifact"]["version"], artifact["version"] + 1)
        self.assertEqual(payload["artifact"]["body"], "分析正文保持不变。")
        applications = payload["artifact"]["input_snapshot"]["method_applications"]
        self.assertEqual(len(applications), 1)
        snapshot_record = applications[0]
        self.assertEqual(snapshot_record["id"], application["id"])
        self.assertEqual(snapshot_record["method_version"], _CREATE["method_version"])
        self.assertEqual(snapshot_record["problem"], _CREATE["problem"])
        self.assertEqual(snapshot_record["parameters"]["window"], "2026-Q1")
        self.assertEqual(snapshot_record["adopted_mechanisms"], record["adopted_mechanisms"])
        self.assertEqual(snapshot_record["declined_parts"], ["把审美判断当作识别率"])
        self.assertTrue(snapshot_record["used_in_work"])
        # A snapshot that only stored the catalog id would fail this:
        self.assertNotEqual(list(snapshot_record.keys()), ["method_id"])
        self.assertIn("method_version", snapshot_record)
        self.assertIn("problem", snapshot_record)
        # Adopt is idempotent and does not mint another artifact version.
        replay = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={
                "base_artifact_version": payload["artifact"]["version"],
                "adopted_mechanisms": ["比较资格与分母一并给出", "未知留在分母"],
            },
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["artifact"]["version"], payload["artifact"]["version"])
        self.assertEqual(replay.json["data"]["application"]["id"], application["id"])

    def test_formal_result_follows_adopt_and_stays_used_in_work(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        application = self.create_application(artifact["id"]).json["data"]
        self.request("post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/preview", json={})
        adopted = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={
                "base_artifact_version": 1,
                "adopted_mechanisms": ["比较资格与分母一并给出"],
            },
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        version = adopted.json["data"]["artifact"]["version"]
        formal = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/formal-result",
            json={"base_artifact_version": version},
        )
        self.assertEqual(formal.status_code, 200, formal.get_data(as_text=True))
        record = formal.json["data"]["application"]
        self.assertEqual(record["state"], "formal_result")
        self.assertTrue(record["used_in_work"])
        self.assertEqual(formal.json["data"]["artifact"]["version"], version + 1)
        snapshot_record = formal.json["data"]["artifact"]["input_snapshot"]["method_applications"][0]
        self.assertEqual(snapshot_record["state"], "formal_result")
        self.assertTrue(snapshot_record["used_in_work"])
        self.assertEqual(snapshot_record["method_version"], _CREATE["method_version"])

    def test_independent_effect_does_not_imply_used_in_work(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        application = self.create_application(artifact["id"]).json["data"]
        self.request("post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/preview", json={})
        effect = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/independent-effect",
            json={"observation": "独立观察到对照渠道的误认，不写入本作品采用。"},
        )
        self.assertEqual(effect.status_code, 200, effect.get_data(as_text=True))
        record = effect.json["data"]
        self.assertEqual(record["state"], "independent_effect")
        self.assertFalse(record["used_in_work"])
        current = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(current.json["data"]["version"], 1)
        self.assertEqual(current.json["data"]["input_snapshot"].get("method_applications") or [], [])
        # Independent effect is not a back-door into a formal result.
        formal = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/formal-result",
            json={"base_artifact_version": 1},
        )
        self.assertEqual(formal.status_code, 409, formal.get_data(as_text=True))
        self.assertEqual(formal.json["error"]["code"], "METHOD_APPLICATION_STATE")
        still = self.request("get", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}")
        self.assertEqual(still.json["data"]["state"], "independent_effect")
        self.assertFalse(still.json["data"]["used_in_work"])

    def test_adopt_after_independent_effect_is_rejected(self) -> None:
        work = self.create_work()
        artifact = self.create_artifact(work["id"])
        application = self.create_application(artifact["id"]).json["data"]
        self.request("post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/preview", json={})
        self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/independent-effect",
            json={"observation": "独立效果已记录。"},
        )
        adopted = self.request(
            "post", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}/adopt",
            json={"base_artifact_version": 1, "adopted_mechanisms": ["不能从独立效果回写采用"]},
        )
        self.assertEqual(adopted.status_code, 409, adopted.get_data(as_text=True))
        self.assertFalse(
            self.request("get", f"/api/artifacts/{artifact['id']}/method-applications/{application['id']}").json["data"]["used_in_work"]
        )
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
