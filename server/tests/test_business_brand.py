from __future__ import annotations

import unittest

from oevira.business_contracts import CONTRACTS, get_contract, implemented_operations
from oevira.business_execution import evaluate_business
from oevira.api import ApiError


def _asset(asset_id="mark-a", *, use="商标主标识", channel="户外", variant="v3"):
    return {
        "id": asset_id,
        "legal_name": "示例注册商标",
        "use": use,
        "channel": channel,
        "variant_version": variant,
    }


def _answer(answer_id, *, asset_id="mark-a", channel="户外", coded="recognized", text="提到了该商标"):
    return {
        "id": answer_id,
        "respondent_id": f"r-{answer_id}",
        "asset_id": asset_id,
        "channel": channel,
        "text": text,
        "coded": coded,
    }


def _inputs(**overrides):
    base = {
        "goal": "核对本品牌无提示识别，不以预测或审美代替实测。",
        "assets": [_asset(channel="户外"), _asset(channel="线上", variant="v3-digital")],
        "population": {
            "definition": "国内一线城市 18–45 岁、近三个月接触过同类品类的居民",
            "recruitment": "按街区配额招募，书面同意后作答",
        },
        "unaided_test": {
            "rules": "先出示品类卡，不提示品牌名，十秒内自由回答。",
            "exposure": "先户外后线上，间隔七日。",
            "prompted": False,
        },
        "coding": {
            "version": "codebook-2026.11",
            "inclusion": "完整或可唯一对应的品牌名及注册标识别称",
            "exclusion": "只描述包装颜色、无法对应到本资产的作答",
        },
        "existing_tests": [
            {
                "id": "prior-2025",
                "comparable": False,
                "reason": "人群与暴露条件与本版不同，不可合并。",
            }
        ],
        "raw_answers": [
            _answer("a1", channel="户外", coded="recognized"),
            _answer("a2", channel="户外", coded="recognized"),
            _answer("a3", channel="户外", coded="misrecognized", text="说成了竞品"),
            _answer("a4", channel="户外", coded="unknown", text="不知道"),
            _answer("a5", channel="户外", coded="unknown", text="想不起来"),
            _answer("a6", channel="户外", coded="excluded", text="只说颜色很好看"),
            _answer("a7", channel="线上", coded="recognized"),
            _answer("a8", channel="线上", coded="misrecognized", text="认成了另一变体"),
            _answer("a9", channel="线上", coded="unknown", text="没印象"),
        ],
    }
    base.update(overrides)
    return base


def _recognition(result, *, asset_id="mark-a", channel=None):
    section = next(item for item in result["sections"] if item["id"] == "unaided_recognition")
    rows = section["data"]["rates"]
    for row in rows:
        if row["asset_id"] == asset_id and (channel is None or row.get("channel") == channel):
            return row
    raise AssertionError(f"no rate row for {asset_id} {channel}: {rows}")


class BrandRecognitionTests(unittest.TestCase):
    def test_contract_is_complete_and_dispatchable(self) -> None:
        self.assertIn("brand_recognition", implemented_operations())
        self.assertIn("brand_recognition", CONTRACTS)
        contract = get_contract("brand_recognition")
        self.assertEqual(contract["contract_status"], "complete")
        self.assertEqual(contract["family"], "brand")
        self.assertIn("assets", contract["input_schema"]["properties"])
        self.assertIn("raw_answers", contract["input_schema"]["properties"])
        self.assertIn("unaided_test", contract["input_schema"]["properties"])
        result = evaluate_business("brand_recognition", _inputs())
        self.assertIsInstance(result["title"], str)
        self.assertTrue(result["title"])
        ids = [section["id"] for section in result["sections"]]
        self.assertEqual(
            ids,
            ["purpose", "asset_ledger", "unaided_recognition", "cross_channel_differences", "application_plan"],
        )
        for section in result["sections"]:
            self.assertEqual(set(section) >= {"id", "title", "body", "data"}, True)

    def test_measured_rates_keep_unknown_in_the_denominator(self) -> None:
        result = evaluate_business("brand_recognition", _inputs())
        outdoor = _recognition(result, channel="户外")
        # 2 recognized + 1 misrecognized + 2 unknown = 5; the excluded color remark is out.
        self.assertTrue(outdoor["measured"])
        self.assertEqual(outdoor["recognized"], 2)
        self.assertEqual(outdoor["misrecognized"], 1)
        self.assertEqual(outdoor["unknown"], 2)
        self.assertEqual(outdoor["excluded"], 1)
        self.assertEqual(outdoor["denominator"], 5)
        self.assertEqual(outdoor["recognized_rate"], {"numerator": 2, "denominator": 5})
        self.assertEqual(outdoor["misrecognized_rate"], {"numerator": 1, "denominator": 5})
        self.assertEqual(outdoor["unknown_rate"], {"numerator": 2, "denominator": 5})
        self.assertIn("2/5", next(s for s in result["sections"] if s["id"] == "unaided_recognition")["body"])
        # Excluded answers must not inflate or shrink the unaided rates by being left in.
        self.assertNotEqual(outdoor["denominator"], 6)

    def test_prediction_and_aesthetic_judgment_cannot_pass_as_measured(self) -> None:
        inputs = _inputs(
            predicted_recognition=0.8,
            aesthetic_judgments=[{"asset_id": "mark-a", "channel": "户外", "score": 0.95, "note": "标识很好看"}],
        )
        result = evaluate_business("brand_recognition", inputs)
        outdoor = _recognition(result, channel="户外")
        self.assertTrue(outdoor["measured"])
        self.assertEqual(outdoor["recognized_rate"], {"numerator": 2, "denominator": 5})
        self.assertNotEqual(outdoor["recognized_rate"].get("numerator") / outdoor["recognized_rate"]["denominator"], 0.8)
        body = " ".join(section["body"] for section in result["sections"])
        data_blob = str(result)
        self.assertNotIn("0.8", data_blob)
        self.assertNotIn("80%", body)
        self.assertNotIn("0.95", data_blob)
        self.assertTrue(any("预测" in note or "审美" in note for note in result["limitations"]))
        # The predicted figure must not appear as a measured rate field.
        for row in next(s for s in result["sections"] if s["id"] == "unaided_recognition")["data"]["rates"]:
            self.assertNotEqual(row.get("predicted_recognition"), 0.8)
            self.assertNotIn("predicted_recognition", row)

    def test_without_raw_answers_does_not_fabricate_measured_rates(self) -> None:
        result = evaluate_business("brand_recognition", _inputs(raw_answers=[]))
        section = next(item for item in result["sections"] if item["id"] == "unaided_recognition")
        for row in section["data"]["rates"]:
            self.assertFalse(row["measured"])
            self.assertIsNone(row["recognized_rate"])
            self.assertIsNone(row["misrecognized_rate"])
            self.assertIsNone(row["unknown_rate"])
            self.assertEqual(row["denominator"], 0)
            self.assertIsNone(row["recognized"])
        self.assertTrue(any("原始回答" in note or "没有" in note for note in result["limitations"]))
        # No invented percentage in the body.
        self.assertNotRegex(section["body"], r"\d+%\b")
        self.assertNotRegex(section["body"], r"\d+/\d+")

    def test_incomparable_prior_tests_are_not_pooled_into_measured_rates(self) -> None:
        result = evaluate_business("brand_recognition", _inputs())
        outdoor = _recognition(result, channel="户外")
        self.assertEqual(outdoor["denominator"], 5)
        self.assertTrue(any("不可合并" in note or "可比" in note for note in result["limitations"]))
        plan = next(item for item in result["sections"] if item["id"] == "application_plan")
        self.assertIn("验证", plan["body"])
        ledger = next(item for item in result["sections"] if item["id"] == "asset_ledger")
        self.assertEqual(len(ledger["data"]["assets"]), 2)
        self.assertEqual({item["channel"] for item in ledger["data"]["assets"]}, {"户外", "线上"})
        differences = next(item for item in result["sections"] if item["id"] == "cross_channel_differences")
        self.assertTrue(differences["data"]["differences"])
        # Outdoor 2/5 vs online 1/3 — both measured, neither invented.
        online = _recognition(result, channel="线上")
        self.assertEqual(online["recognized_rate"], {"numerator": 1, "denominator": 3})

    def test_prompted_test_is_not_reported_as_unaided_measurement(self) -> None:
        inputs = _inputs()
        inputs["unaided_test"] = {**inputs["unaided_test"], "prompted": True}
        result = evaluate_business("brand_recognition", inputs)
        section = next(item for item in result["sections"] if item["id"] == "unaided_recognition")
        for row in section["data"]["rates"]:
            self.assertFalse(row["measured"])
            self.assertFalse(row.get("unaided", True))
            # Counts may still be shown as prompted observations, but not as unaided measured rates.
            self.assertIsNone(row["recognized_rate"])
        self.assertTrue(any("无提示" in note or "提示" in note for note in result["limitations"]))


    def test_unknown_operation_still_rejected(self) -> None:
        with self.assertRaises(ApiError) as raised:
            evaluate_business("brand_prediction", _inputs())
        self.assertEqual(raised.exception.code, "UNKNOWN_BUSINESS_OPERATION")


if __name__ == "__main__":
    unittest.main()
