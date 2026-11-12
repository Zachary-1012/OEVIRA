import io
import unittest
from unittest.mock import patch

import test_business_execution
from oevira import api


class MaterialReadingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = test_business_execution.BusinessExecutionTests()
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def test_retry_unsupported_original_adds_text_without_upload_or_anchor_replacement(self):
        content = '<html><body><p>营业收入 769,048 千美元</p></body></html>'.encode()
        unsupported = {"status": "unavailable", "reason": "旧解析器不支持HTM", "format": "htm", "text": "", "segments": []}
        with patch.object(api, "_parse_material", return_value=unsupported):
            response = self.case.request("post", "/api/materials", data={"title": "已保存的原件", "work_id": self.case.work["id"], "file": (io.BytesIO(content), "filing.htm")})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        original = response.json["data"]
        request = {"source_sha256": original["sha256"]}
        parsed = self.case.post(f"/api/materials/{original['id']}/retry-parsing", request, key="retry-original")
        self.assertEqual(parsed["id"], original["id"])
        self.assertEqual(parsed["sha256"], original["sha256"])
        self.assertEqual(parsed["status"], "ready")
        self.assertIn("769,048", parsed["text"])
        self.assertTrue(parsed["segments"])
        with patch.object(api, "_parse_material", side_effect=AssertionError("Saved text anchors must not be overwritten")):
            replay = self.case.post(f"/api/materials/{original['id']}/retry-parsing", request, key="retry-original")
            second = self.case.post(f"/api/materials/{original['id']}/retry-parsing", request, key="read-already-parsed")
        self.assertEqual(replay["segments"], parsed["segments"])
        self.assertEqual(second["segments"], parsed["segments"])
        downloaded = self.case.request("get", f"/api/materials/{original['id']}/file")
        self.assertEqual(downloaded.get_data(), content)
        downloaded.close()
        self.assertEqual(self.case.store.row("SELECT COUNT(*) AS n FROM materials")["n"], 1)

    def test_wrong_original_hash_does_not_start_parsing(self):
        source = self.case.post("/api/materials", {"title": "源记录", "filename": "source.txt", "text": "实际原文", "work_id": self.case.work["id"]})
        with patch.object(api, "_parse_material", side_effect=AssertionError("The source version must be checked first")):
            rejected = self.case.post(f"/api/materials/{source['id']}/retry-parsing", {"source_sha256": "0" * 64}, status=(409,))
        self.assertEqual(rejected["code"], "SOURCE_CHANGED")
