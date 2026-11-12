from __future__ import annotations

import unittest

from oevira.api import ApiError
from oevira.business_contracts import CONTRACTS, get_contract, implemented_operations
from oevira.business_execution import evaluate_business


def _anchor(quote, *, material_id="mat-ad-1", segment_id="seg-1"):
    return {"material_id": material_id, "segment_id": segment_id, "quote": quote}


def _ad(ad_id, *, landing=True, effect=None, hook="限时折扣", mismatch=None):
    ad = {
        "id": ad_id,
        "publisher": "示例发布者",
        "media": "信息流",
        "source": {"material_id": f"mat-{ad_id}", "version": 1},
        "market": "国内",
        "language": "zh-CN",
        "period": "2026-Q1",
        "use": "品类获客",
        "classifications": {
            "hook": {"value": hook, "kind": "observation", "evidence": [_anchor(hook, material_id=f"mat-{ad_id}")]},
            "format": {"value": "15秒横版", "kind": "observation", "evidence": [_anchor("15秒", material_id=f"mat-{ad_id}")]},
            "offer": {"value": "首单立减", "kind": "observation", "evidence": [_anchor("首单立减", material_id=f"mat-{ad_id}")]},
            "CTA": {"value": "立即购买", "kind": "observation", "evidence": [_anchor("立即购买", material_id=f"mat-{ad_id}")]},
            "awareness_stage": {"value": "consideration", "kind": "interpretation", "evidence": []},
        },
        "unknown": [],
        "abstentions": [],
        "manual_corrections": [],
    }
    if landing:
        ad["landing_page"] = {"material_id": f"mat-land-{ad_id}", "version": 1}
        ad["landing_mismatch"] = mismatch or {
            "status": "matched",
            "kind": "observation",
            "evidence": [_anchor("落地页同为立即购买", material_id=f"mat-land-{ad_id}")],
        }
    else:
        ad["landing_page"] = None
        ad["landing_mismatch"] = mismatch
    if effect is not None:
        ad["approved_effect"] = effect
    return ad


def _inputs(ads, **overrides):
    base = {
        "goal": "比较竞品信息流广告与落地页的可核差异，不把缺页判为不符。",
        "market": "国内",
        "language": "zh-CN",
        "period": "2026-Q1",
        "use": "品类获客",
        "classification_method_version": "creative-classes-2026.03",
        "inclusion": "已保存原件且用途为品类获客",
        "exclusion": "非本品类或无法核验来源版的投放",
        "coverage": {"status": "partial", "notes": "仅覆盖已授权保存的两条广告"},
        "ads": ads,
    }
    base.update(overrides)
    return base


class CompetitiveAdComparisonTests(unittest.TestCase):
    def test_contract_is_complete_and_dispatchable(self) -> None:
        self.assertIn("competitive_ad_comparison", implemented_operations())
        self.assertIn("competitive_ad_comparison", CONTRACTS)
        contract = get_contract("competitive_ad_comparison")
        self.assertEqual(contract["contract_status"], "complete")
        result = evaluate_business(
            "competitive_ad_comparison",
            _inputs([
                _ad("ad-a", effect={"count": 12, "unit": "clicks", "basis": "平台已授权原始计数"}),
                _ad("ad-b", hook="用户证言", effect={"count": 4, "unit": "clicks", "basis": "平台已授权原始计数"}),
            ]),
        )
        ids = [section["id"] for section in result["sections"]]
        self.assertEqual(
            ids,
            ["purpose", "classifications", "landing_comparison", "effect_ranking", "unknowns"],
        )
        for section in result["sections"]:
            self.assertTrue(set(section) >= {"id", "title", "body", "data"})

    def test_missing_landing_page_is_not_judged_as_mismatch(self) -> None:
        result = evaluate_business(
            "competitive_ad_comparison",
            _inputs([_ad("ad-open", landing=False)]),
        )
        landing = next(item for item in result["sections"] if item["id"] == "landing_comparison")
        row = landing["data"]["rows"][0]
        self.assertEqual(row["ad_id"], "ad-open")
        self.assertEqual(row["status"], "not_judged")
        self.assertFalse(row.get("is_mismatch", False))
        self.assertNotEqual(row["status"], "mismatch")
        self.assertTrue(any("落地页" in note for note in result["limitations"]))
        # The classification dimensions still return with evidence, including unknown awareness_stage as interpretation.
        classifications = next(item for item in result["sections"] if item["id"] == "classifications")
        dimensions = classifications["data"]["dimensions"]
        for name in ("hook", "format", "offer", "CTA", "awareness_stage"):
            self.assertIn(name, dimensions)
            entry = next(item for item in dimensions[name] if item["ad_id"] == "ad-open")
            self.assertIn(entry["kind"], {"observation", "interpretation", "unknown"})
            if entry["kind"] == "observation":
                self.assertTrue(entry["evidence"])

    def test_missing_effect_is_excluded_from_effect_ranking(self) -> None:
        result = evaluate_business(
            "competitive_ad_comparison",
            _inputs([
                _ad("ad-ranked", effect={"count": 12, "unit": "clicks", "basis": "已授权计数"}),
                _ad("ad-no-effect"),
                _ad("ad-also", effect={"count": 20, "unit": "clicks", "basis": "已授权计数"}),
            ]),
        )
        ranking = next(item for item in result["sections"] if item["id"] == "effect_ranking")
        ranked = ranking["data"]["ranked"]
        self.assertEqual([item["ad_id"] for item in ranked], ["ad-also", "ad-ranked"])
        self.assertEqual(ranked[0]["count"], 20)
        self.assertEqual(ranked[1]["count"], 12)
        skipped = ranking["data"]["not_ranked"]
        self.assertEqual([item["ad_id"] for item in skipped], ["ad-no-effect"])
        self.assertEqual(skipped[0]["reason"], "missing_approved_effect")
        self.assertTrue(any("效果" in note for note in result["limitations"]))
        # Rank order is only among ads that have approved raw counts — a missing effect cannot be treated as zero.
        self.assertNotIn("ad-no-effect", [item["ad_id"] for item in ranked])

    def test_manual_correction_and_abstention_are_preserved_not_overwritten(self) -> None:
        ad = _ad("ad-corr", effect={"count": 3, "unit": "clicks", "basis": "已授权计数"})
        ad["abstentions"] = [{"dimension": "offer", "reason": "画面字样无法确认"}]
        ad["manual_corrections"] = [{"dimension": "hook", "from": "限时折扣", "to": "赠品", "reason": "人工按原件校正"}]
        ad["classifications"]["hook"] = {
            "value": "赠品",
            "kind": "observation",
            "evidence": [_anchor("买即赠", material_id="mat-ad-corr")],
        }
        ad["unknown"] = [{"dimension": "awareness_stage", "reason": "仅一帧，无法判断阶段"}]
        result = evaluate_business("competitive_ad_comparison", _inputs([ad]))
        unknowns = next(item for item in result["sections"] if item["id"] == "unknowns")
        self.assertTrue(unknowns["data"]["abstentions"])
        self.assertTrue(unknowns["data"]["manual_corrections"])
        self.assertTrue(unknowns["data"]["unknown"])
        hook = next(
            item
            for item in next(s for s in result["sections"] if s["id"] == "classifications")["data"]["dimensions"]["hook"]
            if item["ad_id"] == "ad-corr"
        )
        self.assertEqual(hook["value"], "赠品")

    def test_unknown_operation_still_rejected(self) -> None:
        with self.assertRaises(ApiError) as raised:
            evaluate_business("ad_effect_guess", _inputs([_ad("ad-x")]))
        self.assertEqual(raised.exception.code, "UNKNOWN_BUSINESS_OPERATION")


if __name__ == "__main__":
    unittest.main()
