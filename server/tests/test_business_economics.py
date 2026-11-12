"""Independent contract checks for the deterministic business analysis core."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oevira.business_analysis_economics import evaluate_economics


class BusinessEconomicsTests(unittest.TestCase):
    def test_operating_analysis_computes_adjacent_change_and_keeps_coverage(self):
        result = evaluate_economics("operating_analysis", {
            "observations": [
                {
                    "id": "w1",
                    "entity": {"id": "store-1", "name": "店一"},
                    "metric": {
                        "key": "net_sales", "definition": "同币种线下净销售额",
                        "value": 100, "unit": "CNY", "currency": "CNY",
                        "basis": "actual", "denominator": None,
                        "comparison_dimensions": ["area_m2", "open_days", "tenant_scope"],
                    },
                    "period": {"start": "2026-01-01", "end": "2026-01-07", "timezone": "Asia/Shanghai"},
                    "scope": {"area_m2": 400, "open_days": 7, "tenant_scope": "同店"},
                    "coverage": {"observed": 6, "eligible": 7},
                    "source": {"material_id": "m-1"},
                },
                {
                    "id": "w2",
                    "entity": {"id": "store-1", "name": "店一"},
                    "metric": {
                        "key": "net_sales", "definition": "同币种线下净销售额",
                        "value": 120, "unit": "CNY", "currency": "CNY",
                        "basis": "actual", "denominator": None,
                        "comparison_dimensions": ["area_m2", "open_days", "tenant_scope"],
                    },
                    "period": {"start": "2026-01-08", "end": "2026-01-14", "timezone": "Asia/Shanghai"},
                    "scope": {"area_m2": 400, "open_days": 7, "tenant_scope": "同店"},
                    "coverage": {"observed": 7, "eligible": 7},
                    "source": {"material_id": "m-2"},
                },
            ]
        })
        comparison = result["sections"][1]["data"]["comparisons"][0]
        first = result["sections"][0]["data"]["observations"][0]
        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["delta"], 20)
        self.assertEqual(comparison["change_percent"], 20)
        self.assertEqual(comparison["causal_effect"], None)
        self.assertAlmostEqual(first["coverage"]["ratio"], 6 / 7)
        self.assertEqual(first["source"]["material_id"], "m-1")

    def test_operating_analysis_keeps_scope_changes_and_unknown_values_out_of_delta(self):
        base = {
            "entity": {"id": "store-1", "name": "店一"},
            "metric": {
                "key": "sales", "definition": "净销售额", "value": 0,
                "unit": "CNY", "currency": "CNY", "basis": "actual",
                "denominator": None, "comparison_dimensions": ["open_days"],
            },
            "period": {"start": "2026-01-01", "end": "2026-01-07", "timezone": "Asia/Shanghai"},
            "scope": {"open_days": 7},
        }
        current = {**base, "id": "b", "metric": {**base["metric"], "value": 25}, "period": {"start": "2026-01-08", "end": "2026-01-14", "timezone": "Asia/Shanghai"}, "scope": {"open_days": 6}}
        prior = {**base, "id": "a"}
        missing = {**current, "id": "c", "metric": {**current["metric"], "value": None}, "period": {"start": "2026-01-15", "end": "2026-01-21", "timezone": "Asia/Shanghai"}, "scope": {"open_days": 6}}
        result = evaluate_economics("operating_analysis", {
            "observations": [prior, current, missing],
            "comparisons": [
                {"baseline_id": "a", "current_id": "b", "kind": "period_change"},
                {"baseline_id": "b", "current_id": "c", "kind": "period_change"},
            ],
        })
        first, second = result["sections"][1]["data"]["comparisons"]
        self.assertFalse(first["comparable"])
        self.assertIn("scope_open_days_differs", first["reasons"])
        self.assertIsNone(first["delta"])
        self.assertFalse(second["comparable"])
        self.assertIn("current_value_unknown", second["reasons"])
        self.assertIsNone(second["delta"])

    def test_operating_analysis_rejects_comparison_across_unknown_or_different_timezones(self):
        observation = {
            "entity": {"id": "store", "name": "门店"},
            "metric": {
                "key": "visits", "definition": "门店到访次数", "value": 10,
                "unit": "visits", "currency": None, "basis": "actual",
                "denominator": None, "comparison_dimensions": ["store_status"],
            },
            "scope": {"store_status": "open"},
            "coverage": {"observed": 7, "eligible": 7},
        }
        rows = [
            {**observation, "id": "a", "entity": {"id": "store-a", "name": "门店甲"},
             "period": {"start": "2026-01-01", "end": "2026-01-07", "timezone": "Asia/Shanghai", "frequency": "week"}},
            {**observation, "id": "b", "entity": {"id": "store-b", "name": "门店乙"},
             "period": {"start": "2026-01-01", "end": "2026-01-07", "timezone": "UTC", "frequency": "week"}},
        ]
        result = evaluate_economics("operating_analysis", {
            "observations": rows,
            "comparisons": [{"baseline_id": "a", "current_id": "b", "kind": "same_period"}],
        })
        comparison = result["sections"][1]["data"]["comparisons"][0]
        self.assertFalse(comparison["comparable"])
        self.assertIn("period_timezone_differs", comparison["reasons"])

        rows[1]["period"]["timezone"] = None
        result = evaluate_economics("operating_analysis", {
            "observations": rows,
            "comparisons": [{"baseline_id": "a", "current_id": "b", "kind": "same_period"}],
        })
        comparison = result["sections"][1]["data"]["comparisons"][0]
        self.assertIn("period_timezone_unknown", comparison["reasons"])

    def test_operating_analysis_keeps_zero_and_checks_market_and_voice_denominators(self):
        result = evaluate_economics("operating_analysis", {
            "market_sales": [{
                "id": "som-1", "entity": "品牌A", "numerator": 250,
                "market_total": 1000, "sales_basis": "revenue", "currency": "CNY",
                "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                "geography": "广州", "category": "咖啡", "market_definition": "有据门店净销售额",
                "status": "actual",
            }],
            "voice_samples": [{
                "id": "sov-1", "counts": {"brand_a": 30, "brand_b": 70},
                "platform_coverage": ["平台甲"],
                "window": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                "count_unit": "exclusive_observed_brand_mentions",
                "counting_rule": "每条已确认品牌提及一次",
                "deduplication_rule": "同一内容快照仅保一条",
                "brand_labels": {"brand_a": "品牌A", "brand_b": "品牌B"},
            }],
        })
        sales = result["sections"][2]["data"]["market_sales"][0]
        voice = result["sections"][3]["data"]["voice_samples"][0]
        self.assertEqual(sales["share"], 0.25)
        self.assertIsNone(sales["interval"])
        self.assertEqual(voice["shares"]["brand_a"], 0.3)
        self.assertEqual(voice["classified_mentions"], 100)
        self.assertIsNone(voice["population_share"])
        self.assertEqual(voice["brand_labels"]["brand_a"], "品牌A")
        self.assertEqual(voice["state"], "UNCLASSIFIED_COUNT_UNKNOWN")
        self.assertIn("品牌A", result["sections"][3]["body"])

    def test_voice_share_rejects_duplicate_display_identity_and_keeps_zero_unknown(self):
        sample = {
            "id": "sov-zero",
            "counts": {"brand_a": 0, "brand_b": 0},
            "brand_labels": {"brand_a": "品牌", "brand_b": "品牌"},
            "platform_coverage": ["平台甲"],
            "window": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "count_unit": "exclusive_observed_brand_mentions",
            "counting_rule": "互斥提及计数",
            "deduplication_rule": "按原文记录去重",
            "unclassified_count": 0,
        }
        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"voice_samples": [sample]})
        sample["brand_labels"] = {"brand_a": "品牌A", "brand_b": "品牌B"}
        result = evaluate_economics("operating_analysis", {"voice_samples": [sample]})
        voice = result["sections"][3]["data"]["voice_samples"][0]
        self.assertEqual(voice["state"], "NO_CLASSIFIED_MENTIONS")
        self.assertIsNone(voice["shares"]["brand_a"])

    def test_market_share_zero_denominator_and_incompatible_units_are_not_fabricated(self):
        base = {
            "id": "som-zero", "entity": "品牌A", "numerator": 0, "market_total": 0,
            "sales_basis": "units", "currency": None, "unit": "orders",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "geography": "广州", "category": "咖啡", "market_definition": "所有同类订单",
            "status": "actual",
        }
        result = evaluate_economics("operating_analysis", {"market_sales": [base]})
        share = result["sections"][2]["data"]["market_sales"][0]
        self.assertEqual(share["numerator"], 0)
        self.assertIsNone(share["share"])
        self.assertEqual(share["state"], "ZERO_MARKET_DENOMINATOR")
        invalid = {**base, "id": "bad", "numerator": 0.5}
        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"market_sales": [invalid]})
        missing_status = {**base, "id": "status-missing"}
        del missing_status["status"]
        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"market_sales": [missing_status]})

    def test_operating_analysis_rejects_boolean_and_non_finite_measurements(self):
        observation = {
            "id": "x", "entity": {"id": "e", "name": "实体"},
            "metric": {
                "key": "sales", "definition": "销售额", "value": True,
                "unit": "CNY", "currency": "CNY", "basis": "actual",
                "denominator": None,
            },
            "period": {"start": "2026-01-01", "end": "2026-01-01"},
        }
        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"observations": [observation]})
        observation["metric"]["value"] = float("nan")
        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"observations": [observation]})

    def test_consumer_nps_uses_explicit_question_and_keeps_missing_responses(self):
        result = evaluate_economics("consumer_analysis", {
            "recommendation_survey": {
                "question_type": "likelihood_to_recommend_0_10",
                "survey_id": "survey-1", "sampling_frame": "已完成服务的本地顾客",
                "observation_window": "2026-01",
                "responses": [
                    {"response_id": "r1", "score": 10},
                    {"response_id": "r2", "score": 9},
                    {"response_id": "r3", "score": 8},
                    {"response_id": "r4", "score": 6},
                    {"response_id": "r5", "score": None},
                ],
            }
        })
        nps = result["sections"][0]["data"]
        self.assertEqual(nps["n"], 4)
        self.assertEqual(nps["missing_n"], 1)
        self.assertEqual(nps["counts"], {"promoters": 2, "passives": 1, "detractors": 1})
        self.assertEqual(nps["value"], 25)
        self.assertEqual(nps["denominator"], "all nonmissing 0-10 recommendation answers, including passives")
        self.assertEqual(len(nps["interval_95"]), 2)
        self.assertLess(nps["interval_95"][0], 25)
        self.assertGreater(nps["interval_95"][1], 25)

    def test_sentiment_is_not_nps_and_retains_unknown_labels(self):
        result = evaluate_economics("consumer_analysis", {
            "sentiment_counts": {
                "positive": 2, "neutral": 1, "negative": 1, "unknown": 2,
                "sampling_frame": "已审核的评论", "observation_window": "2026-01",
                "classification_basis": "人工复核标签",
            }
        })
        nps = result["sections"][0]["data"]
        sentiment = result["sections"][1]["data"]
        self.assertIsNone(nps["value"])
        self.assertEqual(sentiment["n"], 4)
        self.assertEqual(sentiment["unknown_n"], 2)
        self.assertEqual(sentiment["value"], 25)
        self.assertIn("not recommendation", sentiment["limitations"][1])

    def test_purchase_cohort_requires_maturity_and_does_not_label_churn(self):
        result = evaluate_economics("consumer_analysis", {
            "purchase_cohort": {
                "coverage": {"start": "2026-01-01T00:00:00+00:00", "through": "2026-04-01T00:00:00+00:00"},
                "cohort": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-31T23:59:59+00:00"},
                "followup_days": 30,
                "coverage_state": "complete",
                "transactions": [
                    {"id": "p1", "customer_id": "c1", "occurred_at": "2026-01-05T10:00:00+00:00", "kind": "purchase", "amount": 50, "currency": "CNY"},
                    {"id": "p2", "customer_id": "c1", "occurred_at": "2026-02-01T10:00:00+00:00", "kind": "purchase", "amount": 40, "currency": "CNY"},
                    {"id": "p3", "customer_id": "c2", "occurred_at": "2026-01-10T10:00:00+00:00", "kind": "purchase", "amount": 20, "currency": "CNY"},
                    {"id": "p4", "customer_id": None, "occurred_at": "2026-01-12T10:00:00+00:00", "kind": "purchase", "amount": None, "currency": None},
                ],
            }
        })
        cohort = result["sections"][2]["data"]
        self.assertEqual(cohort["matured_customers"], 2)
        self.assertEqual(cohort["repeat_customers"], 1)
        self.assertEqual(cohort["repeat_purchase_rate"], 0.5)
        self.assertEqual(cohort["unidentified_purchase_rows"], 1)
        self.assertEqual(cohort["churn_state"], "NOT_INFERRED_FROM_NONPURCHASE")
        amount = cohort["transaction_amounts_by_currency"][0]
        self.assertEqual(amount["net_transaction_amount_state"], "UNKNOWN_REFUND_COVERAGE")
        self.assertIsNone(amount["known_net_transaction_amount"])

    def test_purchase_cohort_reports_net_amount_only_with_complete_refund_coverage(self):
        result = evaluate_economics("consumer_analysis", {
            "purchase_cohort": {
                "coverage": {
                    "start": "2026-01-01T00:00:00+00:00",
                    "through": "2026-04-01T00:00:00+00:00",
                },
                "cohort": {
                    "start": "2026-01-01T00:00:00+00:00",
                    "end": "2026-01-31T23:59:59+00:00",
                },
                "followup_days": 30,
                "coverage_state": "complete",
                "refund_coverage": "complete",
                "transactions": [
                    {"id": "p1", "customer_id": "c1", "occurred_at": "2026-01-05T10:00:00+00:00", "kind": "purchase", "amount": "100", "currency": "CNY"},
                    {"id": "r1", "customer_id": "c1", "occurred_at": "2026-01-10T10:00:00+00:00", "kind": "refund", "amount": "20", "currency": "CNY", "refund_of": "p1"},
                ],
            }
        })
        summary = result["sections"][2]["data"]["transaction_amounts_by_currency"][0]
        self.assertEqual(summary["refund_count"], 1)
        self.assertEqual(summary["known_net_transaction_amount"], 80)
        self.assertEqual(summary["net_transaction_amount_state"], "OBSERVED_ONLY")

    def test_purchase_cohort_does_not_call_an_immature_customer_lost(self):
        result = evaluate_economics("consumer_analysis", {
            "purchase_cohort": {
                "coverage": {"start": "2026-01-01T00:00:00+00:00", "through": "2026-01-20T00:00:00+00:00"},
                "cohort": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-20T00:00:00+00:00"},
                "followup_days": 30,
                "coverage_state": "complete",
                "transactions": [
                    {"id": "p1", "customer_id": "c1", "occurred_at": "2026-01-10T10:00:00+00:00", "kind": "purchase", "amount": 10, "currency": "CNY"},
                ],
            }
        })
        cohort = result["sections"][2]["data"]
        self.assertEqual(cohort["immature_customers"], 1)
        self.assertIsNone(cohort["repeat_purchase_rate"])
        self.assertEqual(cohort["churn_state"], "NOT_INFERRED_FROM_NONPURCHASE")

    def test_purchase_cohort_day_precision_uses_full_local_days_without_faking_midnight_order(self):
        result = evaluate_economics("consumer_analysis", {
            "purchase_cohort": {
                "coverage": {"start": "2026-03-07", "through": "2026-04-08"},
                "cohort": {"start": "2026-03-07", "end": "2026-03-07"},
                "followup_days": 30,
                "coverage_state": "complete",
                "refund_coverage": "complete",
                "transaction_identity_basis": "source_column",
                "time_precision": "day",
                "timezone": "America/New_York",
                "transactions": [
                    {"id": "p1", "customer_id": "c1", "occurred_at": "2026-03-07", "time_precision": "day", "kind": "purchase", "amount": 20, "currency": "USD"},
                    {"id": "p1-same-day", "customer_id": "c1", "occurred_at": "2026-03-07", "time_precision": "day", "kind": "purchase", "amount": 5, "currency": "USD"},
                    {"id": "p2", "customer_id": "c1", "occurred_at": "2026-03-30", "time_precision": "day", "kind": "purchase", "amount": 10, "currency": "USD"},
                    {"id": "p3-boundary", "customer_id": "c1", "occurred_at": "2026-04-06", "time_precision": "day", "kind": "purchase", "amount": 8, "currency": "USD"},
                ],
            }
        })
        cohort = result["sections"][2]["data"]
        self.assertEqual(cohort["state"], "OBSERVED_COHORT_DAY_GRAIN")
        self.assertEqual(cohort["timezone"], "America/New_York")
        self.assertEqual(cohort["repeat_customers"], 1)
        self.assertEqual(cohort["repeat_purchase_rate"], 1)
        self.assertIn("full local calendar-day intervals", cohort["limitations"][-1])
        with self.assertRaises(ValueError):
            evaluate_economics("consumer_analysis", {
                "purchase_cohort": {
                    "coverage": {"start": "2026-03-07T00:00:00-05:00", "through": "2026-04-08T00:00:00-04:00"},
                    "cohort": {"start": "2026-03-07", "end": "2026-03-07"},
                    "followup_days": 30, "coverage_state": "complete",
                    "time_precision": "day", "timezone": "America/New_York", "transactions": [],
                }
            })

    def test_qualitative_consumer_analysis_links_codes_to_verbatim_evidence_and_counterexamples(self):
        result = evaluate_economics("consumer_analysis", {
            "qualitative_research": {
                "study_id": "jan-store-interviews",
                "question": "未购买者为何没有在本次到店购买？",
                "target_population": "本门店近期到访者中的购买者与未购买者",
                "sampling_frame": "2026年1月自愿完成的到店访谈记录",
                "sampling_unit": "访谈回答记录",
                "sampling_method": "自愿便利抽样",
                "recruitment_method": "店内邀请，非随机",
                "collection_method": "半结构访谈",
                "collection_window": "2026-01",
                "coverage": {
                    "state": "partial", "eligible_units": 30, "invited_units": 20,
                    "responded_units": 8, "basis": "研究者提供的邀约日志",
                    "nonresponse_reason": "拒访和营业高峰未完整登记",
                },
                "consent_policy": "participant_consent_required",
                "codebook_version": "codebook-2026-01-a",
                "codebook": [{
                    "code_id": "price_barrier", "label": "价格可承受度",
                    "definition": "原话明确把价格与购买犹豫、放弃或替代选择联系起来",
                    "inclusion_rule": "记录中直接说明价格影响本次选择",
                    "exclusion_rule": "一般价格评价但未关联购买决定",
                    "counterexample": "明确表示价格可接受并完成购买",
                    "researcher_interpretation": "记录内同时出现价格阻碍和价格可接受的相反情况",
                    "validation_question": "按购买与未购买情境分别核对价格构成与替代选择",
                }],
                "records": [
                    {
                        "respondent_id": "r-01", "respondent_group": "未购买者", "record_type": "访谈",
                        "text": "饮品不错，但价格高于我今天的预算，所以没有买。",
                        "quote": "价格高于我今天的预算，所以没有买。",
                        "observed_at": "2026-01-05", "consent_status": "valid",
                        "code_id": "price_barrier", "coding_status": "support",
                        "coding_note": "受访者明确把预算与未购买相连",
                        "source": {"filename": "访谈摘录.csv", "material_id": "m-1", "material_sha256": "a" * 64, "row": 2},
                    },
                    {
                        "respondent_id": "r-02", "respondent_group": "购买者", "record_type": "访谈",
                        "text": "这个价位我可以接受，今天买了两杯。",
                        "quote": "这个价位我可以接受",
                        "observed_at": "2026-01-06", "consent_status": "valid",
                        "code_id": "price_barrier", "coding_status": "counterevidence",
                        "coding_note": "购买者对本次价格持接受态度",
                        "source": {"filename": "访谈摘录.csv", "material_id": "m-1", "material_sha256": "a" * 64, "row": 3},
                    },
                    {
                        "respondent_id": None, "respondent_group": "未购买者", "record_type": "访谈",
                        "text": "地铁出口离得远，我赶时间就没进去。", "quote": "地铁出口离得远",
                        "observed_at": "2026-01-07", "consent_status": "valid",
                        "code_id": None, "coding_status": "uncoded",
                        "source": {"filename": "访谈摘录.csv", "material_id": "m-1", "material_sha256": "a" * 64, "row": 4},
                    },
                    {
                        "respondent_id": "private-r-04", "respondent_group": "未购买者", "record_type": "访谈",
                        "text": "未取得同意的私人原话，不得用于新分析。", "quote": "未取得同意的私人原话",
                        "consent_status": "withdrawn", "code_id": "nonexistent", "coding_status": "support",
                        "source": {"filename": "访谈摘录.csv", "material_id": "m-1", "material_sha256": "a" * 64, "row": 5},
                    },
                    {
                        "respondent_id": "r-05", "respondent_group": "未购买者", "record_type": "问卷备注",
                        "text": None, "consent_status": "valid", "code_id": "formula-value-unavailable",
                        "coding_status": "support",
                        "source": {"filename": "访谈摘录.xlsx", "material_id": "m-2", "material_sha256": "b" * 64, "sheet": "访谈", "row": 6},
                    },
                ],
            }
        })
        sections = {row["id"]: row for row in result["sections"]}
        coverage = sections["consumer.qualitative.coverage"]["data"]
        theme = sections["consumer.qualitative.theme.price_barrier"]["data"]
        records = sections["consumer.qualitative.evidence"]["data"]["records"]

        self.assertEqual(coverage["coverage"]["state"], "partial")
        self.assertEqual(coverage["coverage"]["response_rate"], 0.4)
        self.assertEqual(coverage["record_count"], 5)
        self.assertEqual(coverage["eligible_record_count"], 3)
        self.assertEqual(coverage["excluded_consent_record_count"], 1)
        self.assertEqual(coverage["source_text_unavailable_record_count"], 1)
        self.assertEqual(coverage["declared_respondent_identifier_count"], 2)
        self.assertEqual(coverage["records_without_respondent_identifier"], 1)
        self.assertEqual(theme["state"], "MIXED_EVIDENCE_IN_SUPPLIED_RECORDS")
        self.assertEqual(theme["role_counts"]["support"], 1)
        self.assertEqual(theme["role_counts"]["counterevidence"], 1)
        self.assertEqual(theme["supporting_declared_respondent_ids"], 1)
        self.assertEqual(theme["counter_declared_respondent_ids"], 1)
        self.assertTrue(any(row["exclusion_reason"] == "CONSENT_WITHDRAWN" and row["raw_text"] is None for row in records))
        self.assertTrue(any(row["exclusion_reason"] == "SOURCE_TEXT_UNAVAILABLE" and row["raw_text"] is None for row in records))
        self.assertNotIn("未取得同意的私人原话", sections["consumer.qualitative.evidence"]["body"])
        self.assertIn("价格高于我今天的预算，所以没有买。", sections["consumer.qualitative.theme.price_barrier"]["body"])
        self.assertIn("这个价位我可以接受", sections["consumer.qualitative.theme.price_barrier"]["body"])
        self.assertEqual(theme["population_inference"], "NOT_ESTABLISHED_FROM_QUALITATIVE_RECORDS")

    def test_qualitative_analysis_rejects_unverifiable_quote_and_keeps_duplicate_assignment_visible(self):
        base = {
            "qualitative_research": {
                "question": "为何选择品牌？", "sampling_frame": "店内访谈",
                "sampling_unit": "访谈回答", "codebook_version": "v1",
                "codebook": [{
                    "code_id": "service", "label": "服务体验", "definition": "服务对选择的影响",
                    "inclusion_rule": "明确提及服务改变选择", "exclusion_rule": "仅提及服务但未关联选择",
                }],
                "records": [{
                    "record_id": "record-1", "record_type": "interview", "text": "服务很耐心，所以我回购。",
                    "quote": "服务很耐心", "consent_status": "valid", "code_id": "service",
                    "coding_status": "support", "respondent_id": "r1",
                }],
            }
        }
        result = evaluate_economics("consumer_analysis", base)
        evidence = next(row for row in result["sections"] if row["id"] == "consumer.qualitative.evidence")
        theme = next(row for row in result["sections"] if row["id"] == "consumer.qualitative.theme.service")
        self.assertEqual(theme["data"]["coded_record_count"], 1)
        self.assertEqual(theme["data"]["state"], "SUPPORTING_EVIDENCE_IN_SUPPLIED_RECORDS")
        self.assertEqual(evidence["data"]["records"][0]["record_id_basis"], "caller_declared_record_id")
        self.assertIn("来源未提供", evidence["body"])

        duplicate = {**base["qualitative_research"]["records"][0], "coding_note": "重复编码候选"}
        repeated = evaluate_economics("consumer_analysis", {
            "qualitative_research": {**base["qualitative_research"], "records": [base["qualitative_research"]["records"][0], duplicate]},
        })
        repeated_theme = next(row for row in repeated["sections"] if row["id"] == "consumer.qualitative.theme.service")
        self.assertEqual(repeated_theme["data"]["coded_record_count"], 1)
        repeated_evidence = next(row for row in repeated["sections"] if row["id"] == "consumer.qualitative.evidence")
        self.assertEqual(repeated_evidence["data"]["duplicate_code_assignment_count"], 1)
        self.assertIn("重复编码候选", repeated_evidence["body"])

        invalid_quote = {**base["qualitative_research"], "records": [{
            **base["qualitative_research"]["records"][0], "quote": "原文里没有的结论",
        }]}
        with self.assertRaises(ValueError):
            evaluate_economics("consumer_analysis", {"qualitative_research": invalid_quote})

    def test_market_boundary_separates_estimated_layers_and_scope_matched_reference_roles(self):
        focal = {
            "id": "store-gz-1", "name": "品牌A广州店", "subject_kind": "store", "analysis_unit": "store",
            "geography": "广州天河", "category": "咖啡", "use_case": "到店即饮", "price_band": "25-35 CNY",
            "channel": "门店", "purchase_unit": "订单", "operating_state": "open",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
        }
        same_scope = {**focal, "id": "store-gz-2", "name": "品牌B广州店"}
        other_city = {**focal, "id": "store-sz-1", "name": "品牌A深圳店", "geography": "深圳南山"}
        target_reference = {**focal, "id": "brand-target", "name": "目标品牌", "subject_kind": "brand", "role": "target"}
        layer_common = {
            "unit": "orders", "currency": None, "status": "estimate", "basis": "同窗授权门店汇总情景",
            "coverage": "仅纳入现有已营业同类门店",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "geography": "广州天河", "category": "咖啡", "purchase_unit": "订单",
        }
        result = evaluate_economics("operating_analysis", {
            "market_boundary": {
                "question": "本店在天河区的可服务空间如何？",
                "focal_subject": focal,
                "references": [
                    {**same_scope, "role": "direct", "source_refs": [{"filename": "店铺清单.csv", "row": 2}]},
                    {**other_city, "role": "direct", "source_refs": [{"filename": "店铺清单.csv", "row": 3}]},
                    {**target_reference, "role": "target", "basis_reason": "研究排队体验机制"},
                ],
                "layers": [
                    {**layer_common, "layer": "total_market", "value": "10000", "definition": "同品类门店实际订单总额折算的订单数"},
                    {**layer_common, "layer": "reachable_market", "value": "3000", "definition": "当前渠道覆盖门店的订单估计"},
                    {**layer_common, "layer": "serviceable_market", "value": "1500", "definition": "当前供应能力内可服务订单假设"},
                ],
            }
        })
        sections = {row["id"]: row for row in result["sections"]}
        boundary = sections["operating.market_boundary"]["data"]["market_boundary"]
        references = sections["operating.market_comparability"]["data"]["references"]
        self.assertEqual(boundary["layer_order_state"], "DECLARED_BOUNDARY_ORDER_CONSISTENT")
        self.assertEqual([row["value_decimal"] for row in boundary["layers"]], ["10000", "3000", "1500"])
        self.assertEqual(references[0]["state"], "SCOPE_MATCH_REQUIRES_METRIC_CHECK")
        self.assertEqual(references[1]["state"], "NOT_COMPARABLE_AS_DIRECT_REFERENCE")
        self.assertEqual(references[1]["scope_differences"], ["geography"])
        self.assertEqual(references[2]["state"], "QUALITATIVE_REFERENCE_ONLY")
        self.assertNotIn("10,000", sections["operating.market_boundary"]["body"])
        self.assertIn("1500", sections["operating.market_boundary"]["body"])

        conflict = evaluate_economics("operating_analysis", {"market_boundary": {
            "focal_subject": focal,
            "layers": [
                {**layer_common, "layer": "total_market", "value": 100, "definition": "总订单"},
                {**layer_common, "layer": "reachable_market", "value": 200, "definition": "可触达订单"},
                {**layer_common, "layer": "serviceable_market", "value": 50, "definition": "可服务订单"},
            ],
        }})
        conflict_data = next(row for row in conflict["sections"] if row["id"] == "operating.market_boundary")["data"]["market_boundary"]
        self.assertEqual(conflict_data["layer_order_state"], "BOUNDARY_ORDER_CONFLICT")
        self.assertEqual([row["value_decimal"] for row in conflict_data["layers"]], ["100", "200", "50"])

    def test_market_boundary_does_not_compare_different_units_or_infer_missing_market_size(self):
        focal = {
            "id": "brand-a", "name": "品牌A", "subject_kind": "brand", "analysis_unit": "brand",
            "geography": "中国", "category": "咖啡", "use_case": "到店即饮", "price_band": "全部",
            "channel": "全部", "purchase_unit": "订单", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
        }
        result = evaluate_economics("operating_analysis", {"market_boundary": {
            "focal_subject": focal,
            "layers": [
                {"layer": "total_market", "value": None, "unit": "orders", "currency": None,
                 "status": "unknown", "definition": "未取得完整总体分母"},
                {"layer": "reachable_market", "value": "300", "unit": "visits", "currency": None,
                 "status": "estimate", "basis": "局部通行观察", "definition": "仅观察可达事件", "geography": "中国", "category": "咖啡", "purchase_unit": "订单"},
            ],
        }})
        boundary = next(row for row in result["sections"] if row["id"] == "operating.market_boundary")["data"]["market_boundary"]
        self.assertEqual(boundary["layers"][0]["value"], None)
        self.assertEqual(boundary["layers"][2]["value"], None)
        self.assertEqual(boundary["layer_order_state"], "PARTIAL_BOUNDARY_LEVELS")
        self.assertIn("不从行业均值", next(row for row in result["sections"] if row["id"] == "operating.market_boundary")["body"])

        comparable_layer = {
            "unit": "orders", "currency": None, "status": "estimate", "basis": "本地抽样估计",
            "definition": "按同一范围估计的订单数", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "geography": "中国", "category": "咖啡", "purchase_unit": "订单",
        }
        mixed_unit = evaluate_economics("operating_analysis", {"market_boundary": {
            "focal_subject": focal,
            "layers": [
                {**comparable_layer, "layer": "total_market", "value": 1000},
                {**comparable_layer, "layer": "reachable_market", "value": 300, "unit": "visits"},
                {**comparable_layer, "layer": "serviceable_market", "value": 100},
            ],
        }})
        mixed_boundary = next(row for row in mixed_unit["sections"] if row["id"] == "operating.market_boundary")["data"]["market_boundary"]
        self.assertEqual(mixed_boundary["layer_order_state"], "BOUNDARY_LEVELS_NOT_COMPARABLE")
        self.assertEqual([row["value_decimal"] for row in mixed_boundary["layers"]], ["1000", "300", "100"])

        with self.assertRaises(ValueError):
            evaluate_economics("operating_analysis", {"market_boundary": {
                "focal_subject": focal,
                "layers": [{"layer": "total_market", "value": 100, "unit": "CNY", "status": "estimate",
                            "basis": "假设", "definition": "金额但币种未知"}],
            }})

    def test_market_boundary_does_not_treat_unknown_timezone_as_same_scope(self):
        scope = {
            "subject_kind": "commercial_area", "analysis_unit": "catchment",
            "geography": "广州天河", "category": "咖啡", "use_case": "即饮",
            "price_band": "25-35 CNY", "channel": "门店", "purchase_unit": "订单",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": None},
            "catchment_definition": "步行15分钟", "travel_mode": "walk", "travel_time_minutes": 15,
        }
        result = evaluate_economics("operating_analysis", {"market_boundary": {
            "focal_subject": {"id": "area-a", "name": "商圈A", **scope},
            "references": [{"id": "area-b", "name": "商圈B", "role": "direct", **scope}],
        }})
        reference = next(row for row in result["sections"] if row["id"] == "operating.market_comparability")["data"]["references"][0]
        self.assertEqual(reference["state"], "SCOPE_INCOMPLETE")
        self.assertIn("period", reference["scope_unknowns"])

    def _financial_inputs(self, *, estimate=4800, resource_uses=None):
        return {
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "budget": {
                "currency": "CNY", "basis": "cash", "hard_limit": 10000,
                "target": 8500, "reserve": 1000,
            },
            "actuals": [
                {
                    "id": "paid-1", "kind": "cost", "status": "actual", "basis": "cash",
                    "currency": "CNY", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                    "category": "场地", "amount": 800, "tax_treatment": "included", "source_ref": "invoice-1/row-1",
                },
                {
                    "id": "refund-1", "kind": "cost", "status": "refund", "basis": "cash",
                    "currency": "CNY", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                    "category": "场地", "amount": 100, "tax_treatment": "included", "refund_of": "paid-1",
                },
            ],
            "actuals_coverage": "complete",
            "scenarios": [{
                "id": "plan-a", "title": "当前方案",
                "line_coverage": "complete",
                "resource_assessment": "none_required" if not resource_uses else "complete",
                "lines": [
                    {
                        "id": "commit-1", "cost_key": "crew", "kind": "cost", "status": "commitment",
                        "basis": "cash", "currency": "CNY", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                        "category": "制作", "amount": 3000, "tax_treatment": "included",
                    },
                    {
                        "id": "quote-1", "cost_key": "venue", "kind": "cost", "status": "quote",
                        "basis": "cash", "currency": "CNY", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                        "category": "场地", "quantity": 3, "unit_price": 120,
                        "tax_treatment": "exclusive", "tax_rate": 0.1,
                        "source_ref": "quote-1/line-1",
                    },
                    {
                        "id": "estimate-1", "cost_key": "post", "kind": "cost", "status": "estimate",
                        "basis": "cash", "currency": "CNY", "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                        "category": "后期", "amount": estimate, "tax_treatment": "not_applicable",
                    },
                ],
                "resource_uses": resource_uses or [],
            }],
        }

    def test_financial_scenario_computes_tax_refund_and_unsettled_commitment_once(self):
        result = evaluate_economics("financial_scenario", self._financial_inputs())
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["actual_net_cost"], 700)
        self.assertEqual(summary["outstanding_commitments"], 3000)
        self.assertEqual(summary["quotes"], 396)
        self.assertEqual(summary["estimates"], 4800)
        self.assertEqual(summary["projected_cost"], 8896)
        self.assertEqual(summary["available_after_reserve"], 9000)
        self.assertEqual(summary["target_variance"], 396)
        self.assertEqual(summary["budget_state"], "WITHIN_LIMIT")
        self.assertEqual(summary["overall_feasibility"], "FEASIBLE_UNDER_DECLARED_CONDITIONS")
        quote = next(row for row in result["sections"][1]["data"]["scenarios"][0]["lines"] if row["id"] == "quote-1")
        self.assertEqual(quote["base_amount"], 360)
        self.assertEqual(quote["tax_amount"], 36)
        self.assertEqual(quote["total_amount"], 396)

    def test_financial_scenario_keeps_income_nature_tax_and_receipt_coverage_separate(self):
        inputs = self._financial_inputs()
        inputs["scenarios"][0]["income_coverage"] = "complete"
        inputs["scenarios"][0]["lines"].append({
            "id": "ticket-estimate",
            "cost_key": "ticket-sales",
            "kind": "income",
            "status": "estimate",
            "basis": "cash",
            "currency": "CNY",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "category": "体验活动收入",
            "amount": "650.00",
            "tax_treatment": "not_applicable",
        })
        result = evaluate_economics("financial_scenario", inputs)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["income"]["estimates_exact"], "650.00")
        self.assertEqual(summary["income"]["projected_receipts_exact"], "650.00")
        self.assertEqual(summary["income"]["scenario_income_coverage"], "complete")
        income_row = next(
            row for row in result["sections"][1]["data"]["scenarios"][0]["lines"]
            if row["id"] == "ticket-estimate"
        )
        self.assertEqual(income_row["kind"], "income")
        self.assertEqual(income_row["status"], "estimate")
        self.assertEqual(income_row["tax_treatment"], "not_applicable")
        self.assertIn("直接输入金额", result["sections"][1]["body"])
        self.assertIn("650", result["sections"][0]["body"])
        self.assertIn("396", result["sections"][0]["body"])

        incomplete = self._financial_inputs()
        incomplete["scenarios"][0]["lines"].append({
            "id": "ticket-estimate", "cost_key": "ticket-sales", "kind": "income", "status": "estimate",
            "basis": "cash", "currency": "CNY",
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "category": "体验活动收入", "amount": 650, "tax_treatment": "not_applicable",
        })
        incomplete_summary = evaluate_economics("financial_scenario", incomplete)["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(incomplete_summary["income"]["known_projected_subtotal"], 650)
        self.assertIsNone(incomplete_summary["income"]["projected_receipts"])
        self.assertEqual(incomplete_summary["income"]["scenario_income_coverage"], "unknown")

    def test_csv_numeric_cells_are_parsed_without_confusing_blank_and_zero(self):
        inputs = self._financial_inputs()
        inputs["budget"]["hard_limit"] = "10000"
        inputs["budget"]["target"] = "8500"
        inputs["budget"]["reserve"] = "1000"
        inputs["actuals"][0]["amount"] = "800"
        inputs["actuals"][1]["amount"] = "100"
        inputs["scenarios"][0]["lines"][1]["quantity"] = "3"
        inputs["scenarios"][0]["lines"][1]["unit_price"] = "120"
        inputs["scenarios"][0]["lines"][2]["amount"] = "0"
        result = evaluate_economics("financial_scenario", inputs)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["estimates"], 0)
        self.assertEqual(summary["projected_cost"], 4096)
        line = next(row for row in result["sections"][1]["data"]["scenarios"][0]["lines"] if row["id"] == "estimate-1")
        self.assertEqual(line["base_amount"], 0)
        self.assertEqual(line["amount_state"], "KNOWN")

        inputs["scenarios"][0]["lines"][2]["amount"] = None
        result = evaluate_economics("financial_scenario", inputs)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertIsNone(summary["projected_cost"])
        self.assertEqual(summary["planned_unknown_cost_lines"], 1)

    def test_financial_scenario_marks_over_limit_even_with_known_actuals(self):
        result = evaluate_economics("financial_scenario", self._financial_inputs(estimate=5000))
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["projected_cost"], 9096)
        self.assertEqual(summary["budget_state"], "OVER_LIMIT")

    def test_financial_scenario_uses_distinct_cpm_formula_and_requires_impressions(self):
        inputs = self._financial_inputs()
        quote = inputs["scenarios"][0]["lines"][1]
        quote.update({
            "quantity": 200000,
            "quantity_unit": "impressions",
            "unit_price": 25,
            "unit_price_basis": "per_thousand",
            "tax_treatment": "not_applicable",
            "tax_rate": None,
        })
        result = evaluate_economics("financial_scenario", inputs)
        quote_result = next(
            row for row in result["sections"][1]["data"]["scenarios"][0]["lines"]
            if row["id"] == "quote-1"
        )
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(quote_result["base_amount"], 5000)
        self.assertEqual(quote_result["calculation_formula"], "quantity_times_rate_divided_by_1000")
        self.assertEqual(summary["quotes"], 5000)
        self.assertEqual(summary["budget_state"], "OVER_LIMIT")

        quote["quantity_unit"] = "content_pieces"
        with self.assertRaises(ValueError):
            evaluate_economics("financial_scenario", inputs)

    def test_financial_scenario_deduplicates_same_source_line_and_rejects_conflicts(self):
        inputs = self._financial_inputs()
        duplicate = dict(inputs["scenarios"][0]["lines"][1], id="quote-copy")
        inputs["scenarios"][0]["lines"].append(duplicate)
        result = evaluate_economics("financial_scenario", inputs)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["quotes"], 396)
        # Deduplication is visible in the detail section where scenario rows live.
        scenario = result["sections"][1]["data"]["scenarios"][0]
        self.assertIn("quote-copy", scenario["duplicate_line_ids"])

        conflicting = self._financial_inputs()
        duplicate["amount"] = 999
        duplicate["quantity"] = None
        duplicate["unit_price"] = None
        conflicting["scenarios"][0]["lines"].append(duplicate)
        with self.assertRaises(ValueError):
            evaluate_economics("financial_scenario", conflicting)

    def test_unknown_tax_and_foreign_currency_do_not_pass_budget_check(self):
        inputs = self._financial_inputs()
        line = inputs["scenarios"][0]["lines"][2]
        line["amount"] = 1200
        line["tax_treatment"] = "unknown"
        result = evaluate_economics("financial_scenario", inputs)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertIsNone(summary["projected_cost"])
        self.assertEqual(summary["budget_state"], "UNDETERMINED")

        foreign = self._financial_inputs()
        foreign["scenarios"][0]["lines"][2]["currency"] = "USD"
        result = evaluate_economics("financial_scenario", foreign)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertIsNone(summary["projected_cost"])
        self.assertEqual(summary["currency_mismatch_cost_lines"], 1)
        self.assertEqual(summary["budget_state"], "UNDETERMINED")

        accrual = self._financial_inputs()
        accrual["scenarios"][0]["lines"][2]["basis"] = "accrual"
        result = evaluate_economics("financial_scenario", accrual)
        summary = result["sections"][0]["data"]["scenarios"][0]
        self.assertEqual(summary["accounting_basis_mismatch_cost_lines"], 1)
        self.assertEqual(summary["budget_state"], "UNDETERMINED")

    def test_resource_capacity_uses_half_open_windows_and_reports_overlap(self):
        non_overlapping = [
            {"id": "use-1", "resource_id": "crew-1", "resource_name": "摄制组一", "unit": "crew", "quantity": 1, "capacity": 1, "start": "2026-01-05T09:00:00+08:00", "end": "2026-01-05T12:00:00+08:00", "status": "fixed"},
            {"id": "use-2", "resource_id": "crew-1", "resource_name": "摄制组一", "unit": "crew", "quantity": 1, "capacity": 1, "start": "2026-01-05T12:00:00+08:00", "end": "2026-01-05T14:00:00+08:00", "status": "proposed"},
        ]
        result = evaluate_economics("financial_scenario", self._financial_inputs(resource_uses=non_overlapping))
        self.assertEqual(result["sections"][0]["data"]["scenarios"][0]["resource_state"], "WITHIN_DECLARED_CAPACITY")

        overlapping = [
            dict(non_overlapping[0]),
            {**non_overlapping[1], "start": "2026-01-05T11:00:00+08:00"},
        ]
        result = evaluate_economics("financial_scenario", self._financial_inputs(resource_uses=overlapping))
        summary = result["sections"][0]["data"]["scenarios"][0]
        resource_data = result["sections"][2]["data"]["scenarios"][0]
        self.assertEqual(summary["budget_state"], "WITHIN_LIMIT")
        self.assertEqual(summary["overall_feasibility"], "INFEASIBLE")
        self.assertEqual(summary["resource_state"], "CAPACITY_EXCEEDED")
        self.assertEqual(resource_data["conflicts"][0]["used"], 2)
        self.assertEqual(resource_data["conflicts"][0]["capacity"], 1)

    def test_financial_scenario_rejects_bool_nan_and_duplicate_alternatives(self):
        inputs = self._financial_inputs()
        inputs["budget"]["hard_limit"] = True
        with self.assertRaises(ValueError):
            evaluate_economics("financial_scenario", inputs)
        inputs = self._financial_inputs()
        inputs["budget"]["hard_limit"] = float("nan")
        with self.assertRaises(ValueError):
            evaluate_economics("financial_scenario", inputs)
        inputs = self._financial_inputs()
        line = dict(inputs["scenarios"][0]["lines"][2], id="post-quote", status="quote")
        inputs["scenarios"][0]["lines"].append(line)
        with self.assertRaises(ValueError):
            evaluate_economics("financial_scenario", inputs)

    def test_unknown_operation_and_invalid_input_fail_explicitly(self):
        with self.assertRaises(ValueError):
            evaluate_economics("financial_forecast", {})
        with self.assertRaises(ValueError):
            evaluate_economics("consumer_analysis", [])

    def test_api_artifact_html_contains_computed_operating_and_budget_values(self):
        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            operating = {
                "question": "比较门店连续两周净销售额",
                "observations": [
                    {
                        "id": "w1", "entity": {"id": "store-1", "name": "店一"},
                        "metric": {"key": "net_sales", "definition": "同店净销售额", "value": 100,
                                   "unit": "CNY", "currency": "CNY", "basis": "actual", "denominator": None,
                                   "comparison_dimensions": ["area_m2", "open_days"]},
                        "period": {"start": "2026-01-01", "end": "2026-01-07", "frequency": "week", "timezone": "Asia/Shanghai"},
                        "scope": {"area_m2": 400, "open_days": 7}, "coverage": {"observed": 7, "eligible": 7},
                    },
                    {
                        "id": "w2", "entity": {"id": "store-1", "name": "店一"},
                        "metric": {"key": "net_sales", "definition": "同店净销售额", "value": 120,
                                   "unit": "CNY", "currency": "CNY", "basis": "actual", "denominator": None,
                                   "comparison_dimensions": ["area_m2", "open_days"]},
                        "period": {"start": "2026-01-08", "end": "2026-01-14", "frequency": "week", "timezone": "Asia/Shanghai"},
                        "scope": {"area_m2": 400, "open_days": 7}, "coverage": {"observed": 7, "eligible": 7},
                    },
                ],
            }
            operating_artifact = case.post(f"/api/works/{case.work['id']}/business-results", {
                "operation": "operating_analysis", "inputs": operating, "citations": [],
            })
            self.assertIn("| 20 | 20% | CNY", operating_artifact["body"])
            operating_export = case.post(
                f"/api/artifacts/{operating_artifact['id']}/exports",
                {"version": operating_artifact["version"], "format": "html"},
            )
            operating_download = case.request("get", operating_export["download_url"])
            try:
                self.assertEqual(operating_download.status_code, 200)
                self.assertIn(b"20%", operating_download.get_data())
            finally:
                operating_download.close()

            period = {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"}
            finance = {
                "period": period,
                "budget": {"currency": "CNY", "basis": "cash", "hard_limit": 10000, "target": 8500, "reserve": 1000},
                "actuals": [], "actuals_coverage": "complete",
                "scenarios": [{
                    "id": "base", "title": "一月门店活动", "line_coverage": "complete",
                    "resource_assessment": "none_required", "resource_uses": [],
                    "lines": [{
                        "id": "venue-quote", "cost_key": "venue", "category": "场地", "kind": "cost",
                        "status": "quote", "basis": "cash", "currency": "CNY", "period": period,
                        "quantity": 3, "unit_price": 120, "tax_treatment": "exclusive", "tax_rate": 0.1,
                        "source_ref": "quote-2026-01/venue",
                    }],
                }],
            }
            finance_artifact = case.post(f"/api/works/{case.work['id']}/business-results", {
                "operation": "financial_scenario", "inputs": finance, "citations": [],
            })
            self.assertIn("| 396 |", finance_artifact["body"])
            self.assertIn("场地", finance_artifact["body"])
            self.assertNotIn("venue-quote", finance_artifact["body"])
            finance_export = case.post(
                f"/api/artifacts/{finance_artifact['id']}/exports",
                {"version": finance_artifact["version"], "format": "html"},
            )
            finance_download = case.request("get", finance_export["download_url"])
            try:
                self.assertEqual(finance_download.status_code, 200)
                html = finance_download.get_data()
                self.assertIn(b"396", html)
                self.assertIn("场地".encode("utf-8"), html)
            finally:
                finance_download.close()
        finally:
            case.tearDown()

    def test_api_maps_saved_transactions_with_nested_target_and_reviewed_kind_labels(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            content = (
                "transaction_id,customer_id,occurred_at,kind,amount,currency,refund_of\n"
                "p1,c1,2026-01-05,购买,100,CNY,\n"
                "p2,c1,2026-02-01,购买,40,CNY,\n"
                "r1,c1,2026-01-10,退款,20,CNY,p1\n"
                "p3,,2026-01-12,购买,,,\n"
            ).encode("utf-8")
            uploaded = case.request(
                "post",
                "/api/materials",
                data={
                    "file": (io.BytesIO(content), "transactions.csv"),
                    "title": "交易原表",
                    "work_id": case.work["id"],
                },
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
            material = uploaded.json["data"]
            result = case.post(
                f"/api/works/{case.work['id']}/business-results",
                {
                    "operation": "consumer_analysis",
                    "inputs": {
                        "purchase_cohort": {
                            "coverage": {
                                "start": "2026-01-01",
                                "through": "2026-04-01",
                            },
                            "cohort": {
                                "start": "2026-01-01",
                                "end": "2026-01-31",
                            },
                            "followup_days": 30,
                            "coverage_state": "complete",
                            "refund_coverage": "complete",
                            "transaction_identity_basis": "source_column",
                            "time_precision": "day",
                            "timezone": "Asia/Shanghai",
                            "transactions": [],
                        }
                    },
                    "tables": [
                        {
                            "material_id": material["id"],
                            "material_sha256": material["sha256"],
                            "target": "purchase_cohort.transactions",
                            "columns": {
                                "id": {"column": "transaction_id", "type": "text"},
                                "customer_id": {"column": "customer_id", "type": "text"},
                                "occurred_at": {"column": "occurred_at", "type": "date"},
                                "kind": {"column": "kind", "type": "text"},
                                "amount": {"column": "amount", "type": "decimal"},
                                "currency": {"column": "currency", "type": "text"},
                                "refund_of": {"column": "refund_of", "type": "text"},
                            },
                            "value_mappings": {"kind": {"购买": "purchase", "退款": "refund"}},
                            "row_defaults": {},
                        }
                    ],
                },
            )
            business = result["input_snapshot"]["business"]
            rows = business["inputs"]["purchase_cohort"]["transactions"]
            self.assertEqual([row["kind"] for row in rows], ["purchase", "purchase", "refund", "purchase"])
            self.assertEqual(rows[0]["source"]["material_id"], material["id"])
            cohort = next(
                section["data"]
                for section in business["result"]["sections"]
                if section["id"] == "consumer.purchase_behavior"
            )
            self.assertEqual(cohort["repeat_purchase_rate"], 1.0)
            self.assertEqual(cohort["unidentified_purchase_rows"], 1)
            amount = cohort["transaction_amounts_by_currency"][0]
            self.assertEqual(amount["known_net_transaction_amount"], 120)
            self.assertEqual(amount["refund_coverage"], "complete")
            self.assertNotIn("customer_id", result["body"])

            bad = case.request(
                "post",
                f"/api/works/{case.work['id']}/business-results",
                json={
                    "operation": "consumer_analysis",
                    "inputs": {"purchase_cohort": {"coverage": {"start": "2026-01-01T00:00:00+00:00", "through": "2026-04-01T00:00:00+00:00"}, "cohort": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-31T23:59:59+00:00"}, "followup_days": 30, "transactions": []}},
                    "tables": [{
                        "material_id": material["id"], "material_sha256": material["sha256"],
                        "target": "purchase_cohort.transactions",
                        "columns": {"id": {"column": "transaction_id", "type": "text"}, "occurred_at": {"column": "occurred_at", "type": "text"}, "kind": {"column": "kind", "type": "text"}},
                        "value_mappings": {"kind": {"购买": "purchase"}},
                        "row_defaults": {},
                    }],
                },
            )
            self.assertEqual(bad.status_code, 409)
            self.assertEqual(bad.json["error"]["code"], "TABLE_VALUE_MAPPING_REQUIRED")
        finally:
            case.tearDown()

    def test_api_operating_table_exports_source_row_location_without_internal_row_id(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            content = (
                "start,end,sales\n"
                "2026-01-01,2026-01-31,100\n"
                "2026-02-01,2026-02-28,120\n"
            ).encode("utf-8")
            uploaded = case.request(
                "post",
                "/api/materials",
                data={
                    "file": (io.BytesIO(content), "monthly-sales.csv"),
                    "title": "门店月销售",
                    "work_id": case.work["id"],
                },
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
            material = uploaded.json["data"]
            preview = case.request(
                "get",
                f"/api/materials/{material['id']}/business-table?work_id={case.work['id']}",
            )
            self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
            result = case.post(
                f"/api/works/{case.work['id']}/business-results",
                {
                    "operation": "operating_analysis",
                    "inputs": {"observations": []},
                    "tables": [
                        {
                            "material_id": material["id"],
                            "material_sha256": preview.json["data"]["sha256"],
                            "target": "observations",
                            "source_row_field": "id",
                            "columns": {
                                "metric.value": {"column": "sales", "type": "decimal"},
                                "period.start": {"column": "start", "type": "date"},
                                "period.end": {"column": "end", "type": "date"},
                            },
                            "row_defaults": {
                                "entity": {"id": "store-1", "name": "门店甲"},
                                "metric": {
                                    "key": "net_sales",
                                    "definition": "同币种门店月净销售额",
                                    "unit": "CNY",
                                    "currency": "CNY",
                                    "basis": "actual",
                                    "denominator": None,
                                    "comparison_dimensions": [],
                                },
                                "period": {
                                    "frequency": "month",
                                    "timezone": "Asia/Shanghai",
                                },
                                "scope": {},
                            },
                        }
                    ],
                },
            )
            observations = result["input_snapshot"]["business"]["inputs"]["observations"]
            internal_row_id = observations[0]["id"]
            self.assertIn("原件 第2行", result["body"])
            self.assertIn("门店月销售 · 第2行", result["body"])
            self.assertNotIn(internal_row_id, result["body"])
            self.assertEqual(
                result["input_snapshot"]["business"]["table_sources"][0]["material_id"],
                material["id"],
            )
            self.assertEqual(
                result["input_snapshot"]["materials"][0]["filename"],
                "monthly-sales.csv",
            )
        finally:
            case.tearDown()

    def test_api_maps_saved_survey_scores_without_exposing_response_row_ids(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            content = (
                "答卷行,推荐评分\n"
                "r1,10\n"
                "r2,9\n"
                "r3,7\n"
                "r4,6\n"
                "r5,\n"
            ).encode("utf-8")
            uploaded = case.request(
                "post",
                "/api/materials",
                data={
                    "file": (io.BytesIO(content), "recommendations.csv"),
                    "title": "消费者调查答卷",
                    "work_id": case.work["id"],
                },
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
            material = uploaded.json["data"]
            result = case.post(
                f"/api/works/{case.work['id']}/business-results",
                {
                    "operation": "consumer_analysis",
                    "inputs": {
                        "recommendation_survey": {
                            "question_type": "likelihood_to_recommend_0_10",
                            "survey_id": "Q4-recommendation",
                            "sampling_frame": "已同意参与的答卷人",
                            "observation_window": "2026-11",
                            "responses": [],
                        }
                    },
                    "tables": [
                        {
                            "material_id": material["id"],
                            "material_sha256": material["sha256"],
                            "target": "recommendation_survey.responses",
                            "source_row_field": "response_id",
                            "columns": {"score": {"column": "推荐评分", "type": "integer"}},
                            "row_defaults": {},
                        }
                    ],
                },
            )
            business = result["input_snapshot"]["business"]
            survey = business["result"]["sections"][0]["data"]
            self.assertEqual(survey["n"], 4)
            self.assertEqual(survey["missing_n"], 1)
            self.assertEqual(survey["value"], 25)
            self.assertIn("25", result["body"])
            self.assertNotIn("response_id", result["body"])
        finally:
            case.tearDown()

    def test_api_maps_material_interview_rows_to_qualitative_codes_then_revises_reopens_and_exports(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            content = (
                "受访者,样本组,资料类型,原话,摘录,日期,同意,主题,编码状态,编码备注\n"
                "r-01,未购买者,访谈,饮品不错但价格高于预算所以没有买,价格高于预算所以没有买,2026-01-05,valid,price_barrier,support,明确把价格与未购买相连\n"
                "r-02,购买者,访谈,这个价位我可以接受今天买了两杯,这个价位我可以接受,2026-01-06,valid,price_barrier,counterevidence,购买者相反体验\n"
                "r-03,未购买者,访谈,地铁出口较远所以没有进去,地铁出口较远,2026-01-07,valid,,uncoded,待后续编码\n"
            ).encode("utf-8")
            uploaded = case.request(
                "post", "/api/materials",
                data={"file": (io.BytesIO(content), "consumer-interviews.csv"),
                      "title": "已同意访谈节选", "work_id": case.work["id"]},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
            material = uploaded.json["data"]
            result = case.post(
                f"/api/works/{case.work['id']}/business-results",
                {
                    "operation": "consumer_analysis",
                    "inputs": {"qualitative_research": {
                        "study_id": "service-visit-interviews-2026-01",
                        "question": "未购买者为何没有在本次到店购买？",
                        "target_population": "本门店近期到访的购买者与未购买者",
                        "sampling_frame": "2026年1月自愿完成的到店访谈",
                        "sampling_unit": "访谈回答记录",
                        "sampling_method": "自愿便利抽样",
                        "recruitment_method": "店内邀请",
                        "collection_method": "半结构访谈",
                        "collection_window": "2026-01",
                        "coverage": {"state": "partial", "invited_units": 10, "responded_units": 3,
                                     "basis": "访谈邀约表", "nonresponse_reason": "高峰时段未完整邀请"},
                        "consent_policy": "participant_consent_required",
                        "codebook_version": "codebook-2026-01-a",
                        "codebook": [{
                            "code_id": "price_barrier", "label": "价格可承受度",
                            "definition": "原话明确将价格与购买犹豫、放弃或替代选择相连",
                            "inclusion_rule": "明确描述价格影响本次选择",
                            "exclusion_rule": "一般价格评价而未关联选择",
                            "counterexample": "购买者明确表示价格可接受",
                            "validation_question": "按购买和未购买者分别核对价格构成与替代选择",
                        }],
                        "records": [],
                    }},
                    "tables": [{
                        "material_id": material["id"], "material_sha256": material["sha256"],
                        "target": "qualitative_research.records", "source_row_field": "record_id",
                        "columns": {
                            "respondent_id": "受访者", "respondent_group": "样本组", "record_type": "资料类型",
                            "text": "原话", "quote": "摘录", "observed_at": "日期", "consent_status": "同意",
                            "code_id": "主题", "coding_status": "编码状态", "coding_note": "编码备注",
                        },
                        "row_defaults": {},
                    }],
                    "citations": [],
                },
            )
            business = result["input_snapshot"]["business"]
            qualitative = next(
                section["data"] for section in business["result"]["sections"]
                if section["id"] == "consumer.qualitative.theme.price_barrier"
            )
            self.assertEqual(qualitative["state"], "MIXED_EVIDENCE_IN_SUPPLIED_RECORDS")
            self.assertEqual(qualitative["role_counts"]["support"], 1)
            self.assertEqual(qualitative["role_counts"]["counterevidence"], 1)
            self.assertEqual(business["table_sources"][0]["material_id"], material["id"])
            self.assertIn("第2行", result["body"])
            self.assertIn("价格高于预算所以没有买", result["body"])
            generated_id = business["inputs"]["qualitative_research"]["records"][0]["record_id"]
            self.assertNotIn(generated_id, result["body"])

            section_id = "consumer.qualitative.theme.price_barrier"
            note = "研究者复核：价格障碍仅见于本批自愿访谈，不能推及门店总体。"
            theme_section = next(section for section in business["generated_sections"] if section["id"] == section_id)
            revised = case.post(f"/api/business-results/{result['id']}/revisions", {
                "base_version": result["version"],
                "section_edits": [{"id": section_id, "body": theme_section["body"] + "\n\n" + note}],
                "confirm_sections": [section_id],
            })
            self.assertIn(note, revised["body"])
            self.assertEqual(revised["input_snapshot"]["business"]["generated_sections"][0]["id"], "consumer.recommendation_survey")
            case.app = case.new_app()
            case.client = case.app.test_client()
            reopened = case.request("get", f"/api/artifacts/{result['id']}").json["data"]
            self.assertEqual(reopened["version"], revised["version"])
            self.assertEqual(reopened["body"], revised["body"])
            export = case.post(f"/api/artifacts/{result['id']}/exports", {"version": revised["version"], "format": "html"})
            download = case.request("get", export["download_url"])
            html = download.get_data(as_text=True)
            self.assertIn("价格高于预算所以没有买", html)
            self.assertIn(note, html)
            download.close()
        finally:
            case.tearDown()

    def test_api_maps_material_brand_and_catchment_references_without_merging_subjects(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            material = case.post("/api/materials", {
                "title": "同品类门店范围声明", "filename": "store-comparables.csv", "work_id": case.work["id"],
                "text": (
                    "subject_id,name,subject_kind,role,geography,category,use_case,price_band,channel,purchase_unit,start,end,catchment\n"
                    "store-sz,品牌A深圳店,store,direct,深圳南山,咖啡,即饮,25-35 CNY,门店,订单,2026-01-01,2026-01-31,步行15分钟\n"
                    "brand-target,目标品牌,brand,target,广州天河,咖啡,即饮,25-35 CNY,门店,订单,2026-01-01,2026-01-31,步行15分钟\n"
                ),
            })
            result = case.post(
                f"/api/works/{case.work['id']}/business-results",
                {
                    "operation": "operating_analysis",
                    "inputs": {"market_boundary": {
                        "question": "同品类门店和目标品牌有哪些可比边界？",
                        "focal_subject": {
                            "id": "store-gz", "name": "品牌A广州店", "subject_kind": "store",
                            "analysis_unit": "store", "geography": "广州天河", "category": "咖啡",
                            "use_case": "即饮", "price_band": "25-35 CNY", "channel": "门店",
                            "purchase_unit": "订单", "operating_state": "open",
                            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
                            "catchment_definition": "步行15分钟",
                        },
                        "references": [], "layers": [],
                    }},
                    "tables": [{
                        "material_id": material["id"], "material_sha256": material["sha256"],
                        "target": "market_boundary.references",
                        "columns": {
                            "id": "subject_id", "name": "name", "subject_kind": "subject_kind", "role": "role",
                            "geography": "geography", "category": "category", "use_case": "use_case",
                            "price_band": "price_band", "channel": "channel", "purchase_unit": "purchase_unit",
                            "period.start": {"column": "start", "type": "date"},
                            "period.end": {"column": "end", "type": "date"},
                            "catchment_definition": "catchment",
                        },
                        "row_defaults": {"analysis_unit": "store", "operating_state": "open",
                                         "period": {"timezone": "Asia/Shanghai"}},
                    }],
                    "citations": [],
                },
            )
            business = result["input_snapshot"]["business"]
            section = next(row for row in business["result"]["sections"] if row["id"] == "operating.market_comparability")
            by_id = {row["id"]: row for row in section["data"]["references"]}
            self.assertEqual(by_id["store-sz"]["state"], "NOT_COMPARABLE_AS_DIRECT_REFERENCE")
            self.assertEqual(by_id["brand-target"]["state"], "QUALITATIVE_REFERENCE_ONLY")
            self.assertEqual([row["id"] for row in section["data"]["references"]], ["store-sz", "brand-target"])
            self.assertTrue(all(row["source_refs"][0]["material_id"] == material["id"] for row in by_id.values()))
            self.assertEqual(business["table_sources"][0]["target"], "market_boundary.references")
            self.assertIn("第2行", result["body"])
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
