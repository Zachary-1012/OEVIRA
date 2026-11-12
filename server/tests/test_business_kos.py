from __future__ import annotations

import unittest

from oevira.api import ApiError
from oevira.business_contracts import CONTRACTS, get_contract, implemented_operations
from oevira.business_execution import evaluate_business


INDICATORS = (
    "acquisition",
    "valid_content",
    "subject_precision",
    "duplicates",
    "author_resolution",
    "time",
    "engagement",
    "evidence_completeness",
)


def _indicator(numerator, denominator, *, excluded=0, unknown=0):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "excluded": excluded,
        "unknown": unknown,
    }


def _complete_indicators(**overrides):
    base = {name: _indicator(8, 10, excluded=1, unknown=1) for name in INDICATORS}
    base.update(overrides)
    return base


def _batch(batch_id, *, status="verified", kind="native_period", period="week", indicators=None, account="acct-1"):
    return {
        "id": batch_id,
        "account_id": account,
        "rule_version": "kos-rules-2026.04",
        "status": status,
        "kind": kind,
        "period": period,
        "review_labels": [{"id": "rev-1", "label": "已复核样本", "coverage": {"numerator": 20, "denominator": 100}}],
        "sample_coverage": {"numerator": 20, "denominator": 100},
        "attribution_evidence": [{"material_id": "mat-batch", "segment_id": "seg-1", "quote": "账号归属证明"}],
        "indicators": indicators or _complete_indicators(),
    }


def _inputs(batches, decisions=None, **overrides):
    base = {
        "goal": "按八项质量指标核定本批决定资格，不以未核批次做效果排名。",
        "batches": batches,
        "decision_uses": decisions or [
            {
                "id": "formal_tiering",
                "purpose": "正式分层",
                "required_indicators": list(INDICATORS),
                "conditions_version": "decide-2026.04",
            },
            {
                "id": "effect_ranking",
                "purpose": "效果排名",
                "required_indicators": list(INDICATORS),
                "conditions_version": "decide-2026.04",
            },
            {
                "id": "native_weekly_metric",
                "purpose": "周原生指标",
                "required_indicators": ["engagement", "time"],
                "conditions_version": "decide-2026.04",
                "requires_native_period": True,
                "period": "week",
            },
        ],
    }
    base.update(overrides)
    return base


class KosBatchQualityTests(unittest.TestCase):
    def test_contract_is_complete_and_returns_eight_indicators(self) -> None:
        self.assertIn("kos_batch_quality", implemented_operations())
        self.assertIn("kos_batch_quality", CONTRACTS)
        contract = get_contract("kos_batch_quality")
        self.assertEqual(contract["contract_status"], "complete")
        result = evaluate_business("kos_batch_quality", _inputs([_batch("b1")]))
        ids = [section["id"] for section in result["sections"]]
        self.assertEqual(ids, ["purpose", "indicators", "decision_eligibility", "review"])
        indicators = next(item for item in result["sections"] if item["id"] == "indicators")
        names = [row["name"] for row in indicators["data"]["indicators"] if row["batch_id"] == "b1"]
        self.assertEqual(names, list(INDICATORS))
        for row in indicators["data"]["indicators"]:
            self.assertEqual(row["numerator"], 8)
            self.assertEqual(row["denominator"], 10)
            self.assertEqual(row["excluded"], 1)
            self.assertEqual(row["unknown"], 1)
            self.assertEqual(row["rate"], {"numerator": 8, "denominator": 10})

    def test_unverified_or_anomalous_batch_is_not_eligible_for_formal_ranking(self) -> None:
        result = evaluate_business(
            "kos_batch_quality",
            _inputs([
                _batch("verified-ok", status="verified"),
                _batch("open-batch", status="unverified"),
                _batch("odd-batch", status="anomalous"),
            ]),
        )
        eligibility = next(item for item in result["sections"] if item["id"] == "decision_eligibility")
        rows = {(row["batch_id"], row["decision_id"]): row for row in eligibility["data"]["decisions"]}
        self.assertTrue(rows[("verified-ok", "formal_tiering")]["eligible"])
        self.assertTrue(rows[("verified-ok", "effect_ranking")]["eligible"])
        for batch_id in ("open-batch", "odd-batch"):
            for decision in ("formal_tiering", "effect_ranking"):
                self.assertFalse(rows[(batch_id, decision)]["eligible"])
                self.assertTrue(rows[(batch_id, decision)]["reasons"])
        self.assertTrue(any("未核" in note or "异常" in note for note in result["limitations"]))

    def test_manual_snapshot_cannot_pose_as_native_period_metric(self) -> None:
        result = evaluate_business(
            "kos_batch_quality",
            _inputs([
                _batch("weekly", kind="native_period", period="week"),
                _batch("handmade", kind="manual_snapshot", period="week"),
            ]),
        )
        eligibility = next(item for item in result["sections"] if item["id"] == "decision_eligibility")
        rows = {(row["batch_id"], row["decision_id"]): row for row in eligibility["data"]["decisions"]}
        self.assertTrue(rows[("weekly", "native_weekly_metric")]["eligible"])
        self.assertFalse(rows[("handmade", "native_weekly_metric")]["eligible"])
        self.assertTrue(any("手工" in note or "快照" in note for note in result["limitations"] + rows[("handmade", "native_weekly_metric")]["reasons"]))
        # A manual snapshot may still be described; it just cannot stand in for a weekly native metric.
        self.assertTrue(any(row["batch_id"] == "handmade" for row in next(s for s in result["sections"] if s["id"] == "indicators")["data"]["indicators"]))

    def test_unknown_and_missing_indicators_block_eligibility_and_are_not_fabricated(self) -> None:
        indicators = _complete_indicators()
        # Drop engagement entirely and make author_resolution all-unknown.
        del indicators["engagement"]
        indicators["author_resolution"] = _indicator(0, 0, unknown=10)
        result = evaluate_business("kos_batch_quality", _inputs([_batch("gap-batch", indicators=indicators)]))
        section = next(item for item in result["sections"] if item["id"] == "indicators")
        by_name = {row["name"]: row for row in section["data"]["indicators"] if row["batch_id"] == "gap-batch"}
        self.assertIn("engagement", by_name)
        self.assertFalse(by_name["engagement"]["measured"])
        self.assertIsNone(by_name["engagement"]["rate"])
        self.assertEqual(by_name["author_resolution"]["unknown"], 10)
        self.assertEqual(by_name["author_resolution"]["denominator"], 0)
        self.assertIsNone(by_name["author_resolution"]["rate"])
        eligibility = next(item for item in result["sections"] if item["id"] == "decision_eligibility")
        formal = next(
            row for row in eligibility["data"]["decisions"]
            if row["batch_id"] == "gap-batch" and row["decision_id"] == "formal_tiering"
        )
        self.assertFalse(formal["eligible"])
        self.assertTrue(formal["pending_review"])
        self.assertTrue(any("engagement" in reason or "互动" in reason or "缺失" in reason for reason in formal["reasons"]))

    def test_unknown_operation_still_rejected(self) -> None:
        with self.assertRaises(ApiError) as raised:
            evaluate_business("kos_effect_guess", _inputs([_batch("b")]))
        self.assertEqual(raised.exception.code, "UNKNOWN_BUSINESS_OPERATION")


if __name__ == "__main__":
    unittest.main()
