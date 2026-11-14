from __future__ import annotations

import math
import unittest

from oevira.business_analysis_measurement import evaluate_measurement


class BusinessMeasurementTests(unittest.TestCase):
    def section(self, result, section_id):
        return next(item for item in result["sections"] if item["id"] == section_id)["data"]

    def test_design_keeps_internal_version_separate_from_external_exposure(self):
        result = evaluate_measurement("experiment_design", {
            "hypothesis": "标题A提高完成阅读率",
            "assignment_unit": "reader",
            "analysis_unit": "reader",
            "allocation_method": "randomized",
            "randomization_method": "stable hash",
            "control_arm": "control",
            "arms": [
                {"id": "control", "role": "control", "expected_share": 0.5, "artifact_version": "work-1/v3"},
                {"id": "treatment", "role": "treatment", "expected_share": 0.5, "artifact_version": "work-1/v4"},
            ],
            "primary_metric": {"kind": "binary", "name": "completed_reading", "unit": "proportion", "denominator": "assigned readers", "success_definition": "reader reached the end"},
            "window": {"start": "2026-11-01T00:00:00+08:00", "end": "2026-11-08T00:00:00+08:00", "timezone": "Asia/Shanghai"},
        })

        protocol = self.section(result, "experiment_protocol")
        checks = self.section(result, "experiment_design_checks")
        self.assertEqual(protocol["expected_allocation"], {"control": 0.5, "treatment": 0.5})
        self.assertEqual(protocol["success_definition"], None)
        self.assertFalse(checks["external_exposure_evidence_supplied_at_design_time"])
        self.assertTrue(any("do not establish" in limitation for limitation in result["limitations"]))

    def test_binary_analysis_reports_newcombe_difference_and_sample_ratio(self):
        observations = []
        for arm_id, successes in (("control", 6), ("treatment", 8)):
            for index in range(10):
                version = "v1" if arm_id == "control" else "v2"
                observations.append({
                    "unit_id": f"{arm_id}-{index}",
                    "arm_id": arm_id,
                    "outcome": int(index < successes),
                    "outcome_at": "2026-11-03T12:00:00+00:00",
                    "external_exposure": {
                        "version_id": version,
                        "observed_at": "2026-11-03T11:00:00+00:00",
                        "source_ref": f"delivery-{arm_id}-{index}",
                    },
                })
        result = evaluate_measurement("experiment_analysis", {
            "arms": [
                {"id": "control", "role": "control", "expected_share": 0.5, "artifact_version": "v1"},
                {"id": "treatment", "role": "treatment", "expected_share": 0.5, "artifact_version": "v2"},
            ],
            "control_arm": "control",
            "allocation_method": "randomized",
            "randomization_record": "assignment-log-v1",
            "assignment_unit": "reader",
            "analysis_unit": "reader",
            "metric": {"kind": "binary", "name": "completion", "unit": "proportion", "denominator": "assigned readers"},
            "window": {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-08T00:00:00+00:00"},
            "srm_alpha": 0.05,
            "observations": observations,
        })

        assignment = self.section(result, "assignment_integrity")
        estimates = self.section(result, "outcome_estimates")
        interpretation = self.section(result, "experiment_interpretation")
        self.assertEqual(assignment["sample_ratio"]["chi_square"], 0.0)
        self.assertEqual(assignment["sample_ratio"]["p_value"], 1.0)
        self.assertFalse(assignment["sample_ratio"]["alert"])
        comparison = estimates["comparisons"][0]
        self.assertAlmostEqual(comparison["estimate"], 0.2)
        self.assertAlmostEqual(comparison["interval"][0], -0.187, places=3)
        self.assertAlmostEqual(comparison["interval"][1], 0.521, places=3)
        self.assertEqual(comparison["method"], "newcombe_wilson_difference@1")
        self.assertLess(comparison["interval"][0], 0.2)
        self.assertGreater(comparison["interval"][1], 0.2)
        self.assertEqual(interpretation["comparison_interpretation"], "conditional_randomized_assignment_effect; caller supplied but randomization and execution were not independently authenticated")

    def test_sample_ratio_mismatch_is_a_diagnostic_not_a_hidden_filter(self):
        observations = [
            {"unit_id": f"t-{index}", "arm_id": "treatment", "outcome": 0}
            for index in range(18)
        ] + [
            {"unit_id": f"c-{index}", "arm_id": "control", "outcome": 0}
            for index in range(2)
        ]
        result = evaluate_measurement("experiment_analysis", {
            "arms": [
                {"id": "control", "expected_share": 0.5},
                {"id": "treatment", "expected_share": 0.5},
            ],
            "control_arm": "control",
            "metric": {"kind": "binary", "unit": "rate"},
            "srm_alpha": 0.05,
            "observations": observations,
        })
        diagnostics = self.section(result, "assignment_integrity")["sample_ratio"]
        interpretation = self.section(result, "experiment_interpretation")
        self.assertAlmostEqual(diagnostics["chi_square"], 12.8)
        self.assertAlmostEqual(diagnostics["p_value"], math.erfc(math.sqrt(12.8 / 2.0)), places=12)
        self.assertTrue(diagnostics["alert"])
        self.assertIn("sample-ratio check raised an alert", " ".join(interpretation["blockers"]))
        # The mismatch is reported and not silently used as a reason to drop an arm.
        self.assertEqual(diagnostics["observed_counts"], {"control": 2, "treatment": 18})

    def test_duplicate_assignment_rows_do_not_inflate_independent_n(self):
        observations = [
            {"unit_id": "c1", "arm_id": "control", "outcome": 0},
            {"unit_id": "c1", "arm_id": "control", "outcome": 0},
            {"unit_id": "c2", "arm_id": "control", "outcome": 1},
            {"unit_id": "t1", "arm_id": "treatment", "outcome": 1},
            {"unit_id": "t2", "arm_id": "treatment", "outcome": 1},
        ]
        result = evaluate_measurement("experiment_analysis", {
            "arms": [{"id": "control", "expected_share": 0.5}, {"id": "treatment", "expected_share": 0.5}],
            "control_arm": "control",
            "metric": {"kind": "binary"},
            "observations": observations,
        })
        integrity = self.section(result, "assignment_integrity")
        arms = self.section(result, "outcome_estimates")["arms"]
        self.assertEqual(integrity["diagnostics"]["event_rows"], 5)
        self.assertEqual(integrity["diagnostics"]["repeated_assignment_units"], 1)
        self.assertEqual(integrity["sample_ratio"]["observed_counts"], {"control": 2, "treatment": 2})
        self.assertEqual([arm["mature_outcome_units"] for arm in arms], [2, 2])

    def test_unknown_and_withdrawn_outcomes_keep_their_reason(self):
        result = evaluate_measurement("experiment_analysis", {
            "arms": [{"id": "control"}, {"id": "treatment"}],
            "control_arm": "control",
            "metric": {"kind": "binary"},
            "observations": [
                {"unit_id": "c1", "arm_id": "control", "outcome": None, "missing_reason": "not_mature"},
                {"unit_id": "t1", "arm_id": "treatment", "outcome": 0, "outcome_mature": False, "withdrawal_reason": "withdrawn"},
            ],
        })
        diagnostics = self.section(result, "assignment_integrity")["diagnostics"]
        self.assertEqual(diagnostics["missing_or_immature_rows_by_reason"], {"not_mature": 1, "withdrawn": 1})
        arms = self.section(result, "outcome_estimates")["arms"]
        self.assertEqual([arm["mature_outcome_units"] for arm in arms], [0, 0])

    def test_ambiguous_repeat_outcomes_and_shared_clusters_are_diagnosed(self):
        observations = [
            {"unit_id": "c1", "cluster_id": "store-c", "arm_id": "control", "outcome": 0},
            {"unit_id": "c1", "cluster_id": "store-c", "arm_id": "control", "outcome": 1},
            {"unit_id": "c2", "cluster_id": "store-c", "arm_id": "control", "outcome": 0},
            {"unit_id": "t1", "cluster_id": "store-t", "arm_id": "treatment", "outcome": 1},
            {"unit_id": "t2", "cluster_id": "store-t", "arm_id": "treatment", "outcome": 1},
        ]
        result = evaluate_measurement("experiment_analysis", {
            "arms": [{"id": "control", "expected_share": 0.5}, {"id": "treatment", "expected_share": 0.5}],
            "control_arm": "control",
            "metric": {"kind": "binary"},
            "observations": observations,
        })
        integrity = self.section(result, "assignment_integrity")["diagnostics"]
        comparison = self.section(result, "outcome_estimates")["comparisons"][0]
        self.assertEqual(integrity["ambiguous_repeated_units"], 1)
        self.assertEqual(integrity["clusters_shared_by_multiple_units"], 2)
        self.assertIsNone(comparison["interval"])
        self.assertIn("cluster-level analysis", comparison["interval_suppressed_reason"])

    def test_continuous_analysis_uses_welch_interval_and_preserves_measure_unit(self):
        observations = []
        for arm_id, values in (("control", [2.0, 3.0, 4.0]), ("treatment", [5.0, 6.0, 7.0])):
            for index, value in enumerate(values):
                observations.append({"unit_id": f"{arm_id}-{index}", "arm_id": arm_id, "outcome": value})
        result = evaluate_measurement("experiment_analysis", {
            "arms": [{"id": "control"}, {"id": "treatment"}],
            "control_arm": "control",
            "metric": {"kind": "continuous", "name": "reading_time", "unit": "seconds"},
            "confidence_level": 0.95,
            "observations": observations,
        })
        comparison = self.section(result, "outcome_estimates")["comparisons"][0]
        self.assertAlmostEqual(comparison["estimate"], 3.0)
        self.assertAlmostEqual(comparison["standard_error"], math.sqrt(2.0 / 3.0))
        self.assertAlmostEqual(comparison["interval"][0], 0.7330, places=3)
        self.assertAlmostEqual(comparison["interval"][1], 5.2670, places=3)
        self.assertEqual(comparison["unit_of_measure"], "seconds")

    def test_version_mismatch_is_not_reassigned_to_another_arm(self):
        result = evaluate_measurement("experiment_analysis", {
            "arms": [
                {"id": "control", "artifact_version": "saved-v1"},
                {"id": "treatment", "artifact_version": "saved-v2"},
            ],
            "control_arm": "control",
            "allocation_method": "randomized",
            "randomization_record": "assignment-log",
            "metric": {"kind": "binary"},
            "window": {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-03T00:00:00+00:00"},
            "observations": [
                {"unit_id": "c", "arm_id": "control", "outcome": 0, "outcome_at": "2026-11-02T01:00:00+00:00", "external_exposure": {"version_id": "saved-v2", "observed_at": "2026-11-02T00:00:00+00:00", "source_ref": "log-c"}},
                {"unit_id": "t", "arm_id": "treatment", "outcome": 1, "outcome_at": "2026-11-02T01:00:00+00:00", "external_exposure": {"version_id": "saved-v2", "observed_at": "2026-11-02T00:00:00+00:00", "source_ref": "log-t"}},
            ],
        })
        diagnostics = self.section(result, "assignment_integrity")["diagnostics"]
        interpretation = self.section(result, "experiment_interpretation")
        self.assertEqual(diagnostics["exposure_version_mismatches"], 1)
        self.assertEqual(interpretation["comparison_interpretation"], "descriptive_difference_only")

    def test_missing_or_out_of_window_exposure_cannot_promote_version_effect(self):
        result = evaluate_measurement("experiment_analysis", {
            "arms": [
                {"id": "control", "artifact_version": "saved-v1"},
                {"id": "treatment", "artifact_version": "saved-v2"},
            ],
            "control_arm": "control",
            "allocation_method": "randomized",
            "randomization_record": "assignment-log",
            "metric": {"kind": "binary"},
            "window": {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-03T00:00:00+00:00"},
            "observations": [
                {"unit_id": "c", "arm_id": "control", "outcome": 0, "outcome_at": "2026-11-02T01:00:00+00:00", "external_exposure": {"version_id": "saved-v1", "observed_at": "2026-11-02T00:00:00+00:00", "source_ref": "log-c"}},
                {"unit_id": "t", "arm_id": "treatment", "outcome": 1, "outcome_at": "2026-11-02T01:00:00+00:00", "external_exposure": {"version_id": "saved-v2", "observed_at": "2026-11-04T00:00:00+00:00", "source_ref": "log-t"}},
            ],
        })
        diagnostics = self.section(result, "assignment_integrity")["diagnostics"]
        interpretation = self.section(result, "experiment_interpretation")
        self.assertEqual(diagnostics["external_exposure_records"], 1)
        self.assertEqual(diagnostics["external_exposure_coverage"], 0.5)
        self.assertEqual(diagnostics["external_exposures_outside_window"], 1)
        self.assertEqual(interpretation["comparison_interpretation"], "descriptive_difference_only")
        self.assertTrue(any("falls outside" in blocker for blocker in interpretation["blockers"]))

    def test_attribution_conserves_credits_and_deduplicates_refund_notification(self):
        result = evaluate_measurement("attribution_analysis", {
            "model": "linear",
            "lookback_days": 1,
            "journeys": [{
                "journey_id": "journey-1",
                "person_key": "pseudonymous-person-1",
                "converted": True,
                "conversion_at": "2026-11-02T10:00:00+00:00",
                "conversion_value": {"amount": 100, "currency": "CNY", "status": "actual"},
                "touches": [
                    {"touchpoint_id": "touch-a", "channel": "owned", "occurred_at": "2026-11-02T08:00:00+00:00"},
                    {"touchpoint_id": "touch-a", "channel": "owned", "occurred_at": "2026-11-02T08:00:00+00:00"},
                    {"touchpoint_id": "touch-b", "channel": "creator", "occurred_at": "2026-11-02T09:00:00+00:00"},
                    {"touchpoint_id": "old", "channel": "old-channel", "occurred_at": "2026-10-30T09:00:00+00:00"},
                    {"touchpoint_id": "late", "channel": "late-channel", "occurred_at": "2026-11-02T10:01:00+00:00"},
                ],
            }],
            "refunds": [
                {"refund_id": "refund-1", "journey_id": "journey-1", "amount": 20, "currency": "CNY", "status": "actual"},
                {"refund_id": "refund-1", "journey_id": "journey-1", "amount": 20, "currency": "CNY", "status": "actual"},
            ],
        })
        coverage = self.section(result, "attribution_journey_coverage")
        credits = self.section(result, "attribution_descriptive_credits")
        interpretation = self.section(result, "attribution_interpretation")
        self.assertEqual(coverage["duplicate_touchpoint_ids_collapsed"], 1)
        self.assertEqual(coverage["refund_records_received"], 2)
        self.assertEqual(coverage["refund_ids_after_exact_deduplication"], 1)
        self.assertEqual(credits["conversion_credit_by_channel"], {"creator": 0.5, "owned": 0.5})
        self.assertEqual(credits["allocated_conversion_credit_total"], 1.0)
        self.assertEqual(credits["actual_refunds_by_currency"], {"CNY": 20.0})
        self.assertEqual(credits["net_actual_conversion_value_by_currency"], {"CNY": 80.0})
        self.assertEqual(credits["net_actual_value_credit_by_currency_and_channel"]["CNY"], {"owned": 40.0, "creator": 40.0})
        self.assertIsNone(interpretation["incrementality_estimate"])

    def test_unknown_refund_status_is_preserved_and_not_assumed_actual(self):
        result = evaluate_measurement("attribution_analysis", {
            "model": "last_touch",
            "lookback_days": 7,
            "journeys": [{
                "journey_id": "estimated-order",
                "converted": True,
                "conversion_at": "2026-11-02T10:00:00+00:00",
                "conversion_value": {"amount": 20.1, "currency": "USD", "status": "estimated"},
                "touches": [],
            }],
            "refunds": [{"refund_id": "r-unknown", "journey_id": "estimated-order", "amount": 3.05, "currency": "USD"}],
        })
        credits = self.section(result, "attribution_descriptive_credits")
        self.assertEqual(credits["gross_conversion_value_estimated_by_currency"], {"USD": 20.1})
        self.assertEqual(credits["actual_refunds_by_currency"], {})
        self.assertEqual(credits["unapplied_refund_value_by_status_and_currency"], {"unknown": {"USD": 3.05}})
        self.assertEqual(credits["net_actual_conversion_value_by_currency"], {})

    def test_attribution_without_lookback_window_keeps_touch_credits_unknown(self):
        result = evaluate_measurement("attribution_analysis", {
            "model": "last_touch",
            "journeys": [{
                "journey_id": "j1",
                "converted": True,
                "conversion_at": "2026-11-02T10:00:00+00:00",
                "touches": [{"channel": "creator", "occurred_at": "2026-11-02T09:00:00+00:00"}],
            }],
        })
        coverage = self.section(result, "attribution_journey_coverage")
        credits = self.section(result, "attribution_descriptive_credits")
        self.assertEqual(coverage["journeys_with_eligible_touch"], 0)
        self.assertEqual(credits["conversion_credit_by_channel"], {})
        self.assertTrue(any("No lookback window" in limitation for limitation in result["limitations"]))

    def test_actual_refunds_cannot_exceed_original_conversion_value(self):
        with self.assertRaises(ValueError):
            evaluate_measurement("attribution_analysis", {
                "model": "last_touch",
                "lookback_days": 1,
                "journeys": [{
                    "journey_id": "j1",
                    "converted": True,
                    "conversion_at": "2026-11-02T10:00:00+00:00",
                    "conversion_value": {"amount": 10, "currency": "CNY", "status": "actual"},
                    "touches": [],
                }],
                "refunds": [{"refund_id": "r1", "journey_id": "j1", "amount": 11, "currency": "CNY", "status": "actual"}],
            })

    def did_inputs(self):
        observations = []
        for group, base_values, pre_changes, post_changes in (
            ("control", [10.0, 20.0, 30.0], [0.9, 1.0, 1.1], [1.0, 2.0, 3.0]),
            ("treatment", [10.0, 20.0, 30.0], [1.0, 1.1, 0.9], [3.0, 4.0, 5.0]),
        ):
            for index, (base, pre_change) in enumerate(zip(base_values, pre_changes)):
                unit_id = f"{group}-{index}"
                observations.extend([
                    {"unit_id": unit_id, "cluster_id": unit_id, "group": group, "period": "pre-1", "value": base},
                    {"unit_id": unit_id, "cluster_id": unit_id, "group": group, "period": "pre-2", "value": base + pre_change},
                    {"unit_id": unit_id, "cluster_id": unit_id, "group": group, "period": "post-1", "value": base + pre_change + post_changes[index]},
                ])
        return {
            "unit_of_analysis": "store",
            "metric_unit": "orders per store-week",
            "pre_periods": ["pre-1", "pre-2"],
            "post_periods": ["post-1"],
            "period_positions": {"pre-1": 0, "pre-2": 1, "post-1": 2},
            "pretrend_equivalence_margin": 0.5,
            "observations": observations,
            "confidence_level": 0.95,
            "counterfactual_qualification": {
                "comparison_rationale": "Matched stores in the same region and format",
                "parallel_trends": "supported",
                "no_anticipation": "supported",
                "no_material_spillover": "supported",
                "no_differential_cointervention": "supported",
                "stable_composition": "supported",
                "counterevidence": "No store opening, road closure, or concurrent promotion was found in the supplied review.",
            },
        }

    def test_qualified_balanced_panel_did_estimates_unit_change_difference(self):
        result = evaluate_measurement("incrementality_analysis", self.did_inputs())
        did = self.section(result, "incrementality_did_estimate")
        diagnostics = self.section(result, "incrementality_period_diagnostics")
        counterfactual = self.section(result, "incrementality_counterfactual")
        self.assertAlmostEqual(did["estimate"], 2.0)
        self.assertIsNotNone(did["interval"])
        self.assertTrue(did["qualification_satisfied"])
        self.assertEqual(did["claim_type"], "conditional_DiD_incrementality_estimate")
        self.assertEqual(did["unit"], "orders per store-week")
        self.assertEqual(counterfactual["pretrend_diagnostic"]["equivalence_state"], "within_prespecified_margin")
        self.assertEqual(diagnostics["period_gaps"]["pre-1"], 0.0)
        self.assertAlmostEqual(diagnostics["period_gaps"]["pre-2"], 0.0)

    def test_did_with_concurrent_road_change_stays_descriptive(self):
        inputs = self.did_inputs()
        inputs["counterfactual_qualification"]["no_differential_cointervention"] = "contradicted"
        inputs["counterfactual_qualification"]["counterevidence"] = "Road reopened near treatment stores during the post period."
        result = evaluate_measurement("incrementality_analysis", inputs)
        did = self.section(result, "incrementality_did_estimate")
        self.assertAlmostEqual(did["estimate"], 2.0)
        self.assertEqual(did["claim_type"], "descriptive_difference_in_differences")
        self.assertFalse(did["qualification_satisfied"])
        self.assertIn("no_differential_cointervention is contradicted", did["qualification_blockers"])

    def test_did_requires_complete_units_for_conditional_qualification(self):
        inputs = self.did_inputs()
        inputs["observations"] = [row for row in inputs["observations"] if not (row["unit_id"] == "treatment-0" and row["period"] == "post-1")]
        result = evaluate_measurement("incrementality_analysis", inputs)
        did = self.section(result, "incrementality_did_estimate")
        diagnostics = self.section(result, "incrementality_period_diagnostics")["diagnostics"]
        self.assertFalse(did["qualification_satisfied"])
        self.assertGreater(diagnostics["incomplete_clusters"], 0)

    def test_did_without_prespecified_pretrend_margin_is_not_causal(self):
        inputs = self.did_inputs()
        inputs.pop("pretrend_equivalence_margin")
        result = evaluate_measurement("incrementality_analysis", inputs)
        did = self.section(result, "incrementality_did_estimate")
        self.assertEqual(did["claim_type"], "descriptive_difference_in_differences")
        self.assertIn("a prespecified pretrend equivalence margin is missing", did["qualification_blockers"])

    def test_bool_nan_infinity_and_timezone_errors_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_measurement("experiment_analysis", {
                "arms": [{"id": "control"}, {"id": "treatment"}],
                "control_arm": "control",
                "metric": {"kind": "binary"},
                "observations": [{"unit_id": "reader", "arm_id": "control", "outcome": True}],
            })
        with self.assertRaises(ValueError):
            evaluate_measurement("experiment_analysis", {
                "arms": [{"id": "control"}, {"id": "treatment"}],
                "control_arm": "control",
                "metric": {"kind": "continuous"},
                "confidence_level": float("nan"),
                "observations": [],
            })
        with self.assertRaises(ValueError):
            evaluate_measurement("experiment_analysis", {
                "arms": [{"id": "control"}, {"id": "treatment"}],
                "control_arm": "control",
                "metric": {"kind": "continuous"},
                "observations": [{"unit_id": "reader", "arm_id": "control", "outcome": float("inf")}],
            })
        with self.assertRaises(ValueError):
            evaluate_measurement("attribution_analysis", {
                "model": "last_touch",
                "lookback_days": 1,
                "journeys": [{
                    "journey_id": "j1",
                    "converted": True,
                    "conversion_at": "2026-11-02T10:00:00",
                    "touches": [],
                }],
            })
        with self.assertRaises(ValueError):
            evaluate_measurement("experiment_analysis", {
                "arms": [{"id": "control"}, {"id": "treatment"}],
                "control_arm": "control",
                "metric": {"kind": "binary"},
                "srm_alpha": True,
                "observations": [],
            })


if __name__ == "__main__":
    unittest.main()
