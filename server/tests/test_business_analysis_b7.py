"""Independent numeric regressions for advanced measurement methods."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil, sqrt
from pathlib import Path
import sys
import unittest
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oevira.business_analysis_measurement import evaluate_measurement
from oevira.business_analysis_economics import BusinessAnalysisCancelled


class BusinessAnalysisB7Tests(unittest.TestCase):
    def _thompson_request(self):
        arms = [
            {"id": "control", "role": "control", "expected_share": 0.5, "artifact_version": "control-v1"},
            {"id": "treatment", "role": "treatment", "expected_share": 0.5, "artifact_version": "treatment-v1"},
        ]
        observations = []
        for arm_id, outcome, version in (("control", 1, "control-v1"), ("treatment", 0, "treatment-v1")):
            for index in range(10):
                observed_at = "2026-01-15T12:00:00+00:00"
                observations.append({
                    "unit_id": f"{arm_id}-{index}",
                    "arm_id": arm_id,
                    "outcome": outcome,
                    "outcome_mature": True,
                    "assigned_at": "2026-01-02T12:00:00+00:00",
                    "outcome_at": observed_at,
                    "external_exposure": {
                        "version_id": version,
                        "observed_at": observed_at,
                        "source_ref": f"exposure-{arm_id}-{index}",
                    },
                })
        return {
            "arms": arms,
            "control_arm": "control",
            "metric": {"kind": "binary", "name": "到店", "unit": "率", "denominator": "获资格访问"},
            "window": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00"},
            "allocation_method": "randomized",
            "randomization_record": "pre-registered allocation ledger v1",
            "assignment_unit": "eligible_session",
            "analysis_unit": "eligible_session",
            "observations": observations,
            "srm_alpha": 0.05,
            "request_next_observation_suggestion": True,
        }

    def test_power_plan_uses_two_proportion_approximation_and_declared_allocation(self):
        result = evaluate_measurement("experiment_design", {
            "arms": [
                {"id": "control", "role": "control", "expected_share": 0.6},
                {"id": "treatment", "role": "treatment", "expected_share": 0.4},
            ],
            "control_arm": "control",
            "primary_metric": {"kind": "binary", "name": "到店", "unit": "率", "denominator": "符合资格会话"},
            "power_plan": {
                "control_rate": 0.08,
                "treatment_rate": 0.10,
                "target_power": 0.8,
                "alpha": 0.05,
            },
        })
        data = next(row["data"] for row in result["sections"] if row["id"] == "experiment_power")
        ratio = 0.4 / 0.6
        p0, p1, delta = 0.08, 0.10, 0.02
        pbar = (p0 + ratio * p1) / (1 + ratio)
        null_variance = pbar * (1 - pbar) * (1 + 1 / ratio)
        alternative_variance = p0 * (1 - p0) + p1 * (1 - p1) / ratio
        z_alpha = NormalDist().inv_cdf(0.975)
        z_power = NormalDist().inv_cdf(0.8)
        expected_control = ceil((z_alpha * sqrt(null_variance) + z_power * sqrt(alternative_variance)) ** 2 / delta**2)
        self.assertEqual(data["method"], "two-proportion-normal-approximation@1")
        self.assertEqual(data["required_control_n"], expected_control)
        self.assertEqual(data["required_treatment_n"], ceil(expected_control * ratio))
        self.assertEqual(data["total_n"], data["required_control_n"] + data["required_treatment_n"])
        self.assertFalse(data.get("deployment_eligible", False))

    def test_sequential_plan_uses_only_predeclared_looks_and_requires_execution_qualification(self):
        arms = [
            {"id": "control", "role": "control", "expected_share": 0.5, "artifact_version": "c-v1"},
            {"id": "treatment", "role": "treatment", "expected_share": 0.5, "artifact_version": "t-v1"},
        ]
        observations = []
        for index in range(10):
            for arm_id, outcome, version in (("control", 0, "c-v1"), ("treatment", 1, "t-v1")):
                observed_at = "2026-02-05T12:00:00+00:00" if index < 5 else "2026-02-15T12:00:00+00:00"
                observations.append({
                    "unit_id": f"{arm_id}-{index}",
                    "arm_id": arm_id,
                    "outcome": outcome,
                    "outcome_mature": True,
                    "assigned_at": "2026-02-01T12:00:00+00:00",
                    "outcome_at": observed_at,
                    "external_exposure": {
                        "version_id": version,
                        "observed_at": observed_at,
                        "source_ref": f"exposure-{arm_id}-{index}",
                    },
                })
        inputs = {
            "arms": arms,
            "control_arm": "control",
            "metric": {"kind": "binary", "name": "conversion", "unit": "rate", "denominator": "eligible units"},
            "window": {"start": "2026-02-01T00:00:00+00:00", "end": "2026-02-21T00:00:00+00:00"},
            "allocation_method": "randomized",
            "randomization_record": "pre-recorded allocation ledger",
            "assignment_unit": "eligible_unit",
            "analysis_unit": "eligible_unit",
            "observations": observations,
            "sequential_plan": {
                "look_times": ["2026-02-10T00:00:00+00:00", "2026-02-20T00:00:00+00:00"],
                "alpha": 0.05,
                "minimum_mature_units_per_arm": 3,
            },
        }
        result = evaluate_measurement("experiment_analysis", inputs)
        sequential = next(row["data"] for row in result["sections"] if row["id"] == "experiment_sequential")
        self.assertEqual(sequential["method"], "fixed-look-bonferroni-sequential@1")
        self.assertEqual(sequential["per_look_per_comparison_alpha"], 0.025)
        self.assertEqual(sequential["decision"], "STOP_FOR_EFFICACY_AT_FIRST_CROSSED_LOOK")
        self.assertEqual(sequential["first_boundary_crossing"], {"look_index": 1, "at": "2026-02-10T00:00:00+00:00"})
        self.assertEqual(sequential["looks"][0]["mature_units_by_arm"], {"control": 5, "treatment": 5})

        inputs["allocation_method"] = "unknown"
        unqualified = evaluate_measurement("experiment_analysis", inputs)
        sequential = next(row["data"] for row in unqualified["sections"] if row["id"] == "experiment_sequential")
        self.assertEqual(sequential["decision"], "DESCRIPTIVE_ONLY_NO_STOP_RECOMMENDATION")
        self.assertTrue(sequential["causal_qualification_blockers"])

    def test_opted_in_thompson_suggestion_uses_mature_binary_posterior_without_assigning_traffic(self):
        inputs = self._thompson_request()
        result = evaluate_measurement("experiment_analysis", inputs)
        section = next(row for row in result["sections"] if row["id"] == "experiment.next_observation_suggestion")
        data = section["data"]
        arms = {row["arm_id"]: row for row in data["arms"]}
        self.assertEqual(data["state"], "SUGGESTION_AVAILABLE")
        self.assertEqual(data["method"], "beta-bernoulli-thompson-next-observation@1")
        self.assertEqual(data["request_received"], True)
        self.assertFalse(data["request_origin_verified"])
        self.assertFalse(data["executed"])
        self.assertFalse(data["traffic_assignment_performed"])
        self.assertEqual(data["suggested_arm_id"], "control")
        self.assertEqual(arms["control"]["posterior_alpha"], 11)
        self.assertEqual(arms["control"]["posterior_beta"], 1)
        self.assertAlmostEqual(arms["control"]["posterior_mean"], 11 / 12, places=12)
        self.assertEqual(arms["treatment"]["posterior_alpha"], 1)
        self.assertEqual(arms["treatment"]["posterior_beta"], 11)
        self.assertGreater(arms["control"]["posterior_probability_best_estimate"], 0.99)
        self.assertIn("不分配流量", section["body"])
        self.assertEqual(data, next(row["data"] for row in evaluate_measurement("experiment_analysis", inputs)["sections"] if row["id"] == section["id"]))

    def test_thompson_suggestion_needs_predeclared_randomized_quality_conditions(self):
        inputs = self._thompson_request()
        inputs["allocation_method"] = "observational"
        result = evaluate_measurement("experiment_analysis", inputs)
        suggestion = next(row["data"] for row in result["sections"] if row["id"] == "experiment.next_observation_suggestion")
        self.assertEqual(suggestion["state"], "NOT_AVAILABLE")
        self.assertIn("BASELINE_ALLOCATION_IS_NOT_DECLARED_RANDOMIZED", suggestion["reason"])
        self.assertIsNone(suggestion["suggested_arm_id"])
        self.assertFalse(suggestion["traffic_assignment_performed"])

        inputs = self._thompson_request()
        inputs["sequential_plan"] = {
            "look_times": ["2026-01-20T00:00:00+00:00", "2026-01-28T00:00:00+00:00"],
            "alpha": 0.05,
            "minimum_mature_units_per_arm": 3,
        }
        result = evaluate_measurement("experiment_analysis", inputs)
        suggestion = next(row["data"] for row in result["sections"] if row["id"] == "experiment.next_observation_suggestion")
        self.assertEqual(suggestion["state"], "NOT_AVAILABLE")
        self.assertIn("FIXED_LOOK_PLAN_REQUIRES_A_SEPARATE_ADAPTIVE_STAGE", suggestion["reason"])

    def test_adaptive_observations_do_not_reuse_fixed_srm_or_confidence_intervals(self):
        inputs = self._thompson_request()
        inputs["allocation_method"] = "adaptive_randomized"
        inputs.pop("request_next_observation_suggestion")
        inputs["sequential_plan"] = {
            "look_times": ["2026-01-20T00:00:00+00:00", "2026-01-28T00:00:00+00:00"],
            "alpha": 0.05,
            "minimum_mature_units_per_arm": 3,
        }
        result = evaluate_measurement("experiment_analysis", inputs)
        sections = {row["id"]: row["data"] for row in result["sections"]}
        self.assertEqual(sections["assignment_integrity"]["sample_ratio"]["state"], "not_assessable")
        self.assertIn("adaptive assignment", sections["assignment_integrity"]["sample_ratio"]["reason"])
        for comparison in sections["outcome_estimates"]["comparisons"]:
            self.assertIsNone(comparison["interval"])
            self.assertEqual(comparison["interval_suppressed_reason"], "adaptive allocation requires a propensity-aware estimator")
        sequential = sections["experiment_sequential"]
        self.assertEqual(sequential["state"], "NOT_AVAILABLE")
        self.assertEqual(sequential["decision"], "DESCRIPTIVE_ONLY_NO_STOP_RECOMMENDATION")
        self.assertEqual(sequential["comparisons"], [])

    def test_markov_shapley_probability_and_channel_contributions_are_hand_computable(self):
        result = evaluate_measurement("attribution_analysis", {
            "model": "markov_shapley",
            "lookback_days": 30,
            "journeys": [
                {"journey_id": "converted-ab", "converted": True, "conversion_at": "2026-01-03T00:00:00+00:00",
                 "touches": [{"channel": "A", "occurred_at": "2026-01-01T00:00:00+00:00"},
                             {"channel": "B", "occurred_at": "2026-01-02T00:00:00+00:00"}]},
                {"journey_id": "not-converted-a", "converted": False, "observation_end": "2026-01-03T00:00:00+00:00",
                 "touches": [{"channel": "A", "occurred_at": "2026-01-01T00:00:00+00:00"}]},
                {"journey_id": "converted-b", "converted": True, "conversion_at": "2026-01-04T00:00:00+00:00",
                 "touches": [{"channel": "B", "occurred_at": "2026-01-02T00:00:00+00:00"}]},
            ],
        })
        section = next(row for row in result["sections"] if row["id"] == "attribution_markov_shapley")
        data = section["data"]
        self.assertEqual(data["state"], "READY")
        self.assertAlmostEqual(data["summary"]["baseline_conversion_probability"], 2 / 3, places=12)
        self.assertAlmostEqual(sum(row["shapley_contribution"] for row in data["channels"]), 2 / 3, places=12)
        by_channel = {row["channel"]: row for row in data["channels"]}
        self.assertAlmostEqual(by_channel["A"]["shapley_contribution"], 1 / 6, places=12)
        self.assertAlmostEqual(by_channel["B"]["shapley_contribution"], 1 / 2, places=12)
        self.assertEqual(data["summary"]["shapley_state"], "EXACT_WITH_EFFICIENCY_CHECK")
        self.assertIn("0.66666667", section["body"])

    def test_markov_shapley_preserves_missing_nonconversion_window(self):
        result = evaluate_measurement("attribution_analysis", {
            "model": "markov_shapley",
            "lookback_days": 30,
            "journeys": [
                {"journey_id": "c1", "converted": True, "conversion_at": "2026-01-03T00:00:00+00:00",
                 "touches": [{"channel": "A", "occurred_at": "2026-01-01T00:00:00+00:00"}]},
                {"journey_id": "n1", "converted": False,
                 "touches": [{"channel": "A", "occurred_at": "2026-01-01T00:00:00+00:00"}]},
            ],
        })
        data = next(row["data"] for row in result["sections"] if row["id"] == "attribution_markov_shapley")
        self.assertEqual(data["coverage"]["missing_endpoint_or_lookback"], 1)
        self.assertEqual(data["state"], "INSUFFICIENT")
        self.assertIsNone(data["summary"]["baseline_conversion_probability"])

    def test_markov_shapley_estimates_larger_channel_sets_with_computation_error(self):
        channels = [f"channel-{index:02d}" for index in range(13)]
        start = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        converted = {
            "journey_id": "converted-all-channels",
            "converted": True,
            "conversion_at": "2026-01-02T00:00:00+00:00",
            "touches": [
                {"channel": channel, "occurred_at": (start + timedelta(minutes=index)).isoformat()}
                for index, channel in enumerate(channels)
            ],
        }
        not_converted = {
            "journey_id": "nonconverted-no-touch",
            "converted": False,
            "observation_end": "2026-01-02T00:00:00+00:00",
            "touches": [],
        }
        inputs = {"model": "markov_shapley", "lookback_days": 30, "journeys": [converted, not_converted]}
        result = evaluate_measurement("attribution_analysis", inputs)
        section = next(row for row in result["sections"] if row["id"] == "attribution_markov_shapley")
        data = section["data"]
        self.assertEqual(data["state"], "READY")
        self.assertEqual(data["method"], "absorbing-markov-permutation-shapley@1")
        self.assertEqual(data["summary"]["baseline_conversion_probability"], 0.5)
        self.assertEqual(data["summary"]["no_channel_conversion_probability"], 0.0)
        self.assertEqual(data["summary"]["shapley_state"], "PERMUTATION_APPROXIMATION_WITH_EFFICIENCY_CHECK")
        self.assertEqual(data["summary"]["shapley_permutation_count"], 247)
        self.assertAlmostEqual(sum(row["shapley_contribution"] for row in data["channels"]), 0.5, places=12)
        expected = 0.5 / len(channels)
        self.assertTrue(all(abs(row["shapley_contribution"] - expected) < 0.012 for row in data["channels"]))
        self.assertTrue(all(row["shapley_monte_carlo_standard_error"] > 0 for row in data["channels"]))
        self.assertTrue(all(len(row["shapley_monte_carlo_approximation_interval_95"]) == 2 for row in data["channels"]))
        self.assertIn("不是人群置信区间", section["body"])
        self.assertEqual(result, evaluate_measurement("attribution_analysis", inputs))

    def test_measurement_job_cancellation_propagates_during_evaluation(self):
        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_measurement("attribution_analysis", {"model": "linear", "journeys": []}, is_cancelled=lambda: True)

    def test_isotonic_calibration_uses_disjoint_test_partition(self):
        rows = []
        for partition in ("calibration", "test"):
            for index in range(20):
                rows.append({"id": f"{partition}-low-{index}", "partition": partition, "score": 0.2, "label": 1 if index < 2 else 0})
                rows.append({"id": f"{partition}-high-{index}", "partition": partition, "score": 0.8, "label": 1 if index < 18 else 0})
        result = evaluate_measurement("calibration_analysis", {"method": "isotonic", "rows": rows})
        data = next(row["data"] for row in result["sections"] if row["id"] == "calibration_results")
        self.assertEqual(data["state"], "READY")
        self.assertEqual(data["lineage_state"], "NOT_PROVIDED")
        self.assertAlmostEqual(data["calibrated_test_metrics"]["ece_10_equal_width_bins"], 0.0, places=12)
        self.assertAlmostEqual(data["calibrated_test_metrics"]["brier_score"], 0.09, places=12)
        self.assertFalse(data["deployment_eligible"])

        with_training_partition = [
            {"id": f"train-{index}", "partition": "training", "score": None, "label": None}
            for index in range(20)
        ] + rows
        provenance = {
            "model_id": "classification-model",
            "model_version": "v3",
            "calibration_model_version": "v3",
            "test_model_version": "v3",
            "population": "same-store-and-quarter",
        }
        declared = evaluate_measurement("calibration_analysis", {
            "method": "isotonic", "rows": with_training_partition,
            "model_provenance": provenance,
        })
        declared_data = next(row["data"] for row in declared["sections"] if row["id"] == "calibration_results")
        self.assertEqual(declared_data["training_n"], 20)
        self.assertEqual(declared_data["lineage_state"], "DECLARED_DISJOINT_NOT_INDEPENDENTLY_VERIFIED")

    def test_conformal_interval_uses_finite_sample_rank_and_test_coverage(self):
        rows = []
        for index in range(20):
            rows.append({"id": f"cal-{index}", "partition": "calibration", "prediction": 100.0, "observed": 100.0 + index + 1})
        for index in range(20):
            residual = 25.0 if index == 19 else 0.0
            rows.append({"id": f"test-{index}", "partition": "test", "prediction": 200.0, "observed": 200.0 + residual})
        provenance = {
            "model_id": "forecast-model",
            "model_version": "v7",
            "training_ids": ["training-set-1"],
            "calibration_model_version": "v7",
            "test_model_version": "v7",
            "population": "same-store-quarter",
        }
        result = evaluate_measurement("conformal_analysis", {
            "rows": rows, "alpha": 0.1, "unit": "CNY", "model_provenance": provenance,
            "exchangeability_basis": "test rows are independently sampled from the declared stationary quarter population",
        })
        data = next(row["data"] for row in result["sections"] if row["id"] == "conformal_results")
        self.assertEqual(data["state"], "READY")
        self.assertEqual(data["order_statistic_rank"], 19)
        self.assertEqual(data["residual_quantile"], 19.0)
        self.assertEqual(data["test_coverage"], 0.95)
        self.assertEqual(data["mean_interval_width"], 38.0)
        self.assertEqual(data["lineage_state"], "DECLARED_DISJOINT_NOT_INDEPENDENTLY_VERIFIED")

    def test_synthetic_control_reports_simplex_weights_placebos_and_post_gap(self):
        periods = [f"2025-{month:02d}" for month in range(1, 13)] + ["2026-01", "2026-02"]
        pre = periods[:12]
        post = periods[12:]
        observations = []
        for index, period in enumerate(periods):
            baseline = 10.0 + index
            deviation = 0.0 if index % 2 == 0 else 2.0
            treatment_value = baseline + (4.0 if index >= 12 else 0.0)
            observations.extend([
                {"unit_id": "treated-1", "unit_label": "处理门店", "role": "treated", "period": period, "value": treatment_value,
                 "source": {"filename": "panel.csv", "row": index + 2}},
                {"unit_id": "donor-1", "unit_label": "供体一", "role": "donor", "untreated_declared": True, "period": period, "value": baseline + deviation},
                {"unit_id": "donor-2", "unit_label": "供体二", "role": "donor", "untreated_declared": True, "period": period, "value": baseline - deviation},
            ])
        result = evaluate_measurement("incrementality_analysis", {
            "estimator": "synthetic_control",
            "frequency": "month",
            "pre_periods": pre,
            "post_periods": post,
            "observations": observations,
            "max_pre_rmse": 0.01,
            "unit_of_analysis": "同商圈门店",
            "metric_unit": "百万元",
            "design": {
                "no_anticipation": True,
                "no_spillovers": True,
                "comparable_measurement": True,
                "selection_protocol": "仅按干预前同口径销售选择供体，未查看干预后拟合",
            },
        })
        fit = next(row["data"] for row in result["sections"] if row["id"] == "incrementality_synthetic_control")
        self.assertEqual(fit["state"], "READY")
        self.assertAlmostEqual(fit["fit"]["pre_rmse"], 0.0, places=10)
        self.assertAlmostEqual(fit["fit"]["mean_post_gap"], 4.0, places=10)
        weights = {row["unit_label"]: row["weight"] for row in fit["fit"]["weights"]}
        self.assertAlmostEqual(weights["供体一"], 0.5, places=7)
        self.assertAlmostEqual(weights["供体二"], 0.5, places=7)
        self.assertIsNone(fit["placebo_comparison"]["p_value"])
        self.assertIn("4", result["sections"][0]["body"])

    def test_synthetic_control_accepts_more_than_twenty_identifiable_donors(self):
        periods = [f"2025-{month:02d}" for month in range(1, 13)] + [
            f"{2026 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}"
            for month in range(1, 21)
        ]
        pre, post = periods[:30], periods[30:]
        observations = []
        donors = [f"donor-{index:02d}" for index in range(21)]
        for index, period in enumerate(periods):
            baseline = 100.0 + 2.0 * index
            donor_values = [baseline + (10.0 if index == donor else 0.0) for donor in range(20)] + [baseline]
            treated = baseline + (5.0 if index in {0, 1} else 0.0) + (4.0 if index >= 30 else 0.0)
            observations.append({"unit_id": "treated", "unit_label": "处理门店", "role": "treated", "period": period, "value": treated})
            for donor_id, value in zip(donors, donor_values):
                observations.append({
                    "unit_id": donor_id, "unit_label": donor_id, "role": "donor",
                    "untreated_declared": True, "period": period, "value": value,
                })
        result = evaluate_measurement("incrementality_analysis", {
            "estimator": "synthetic_control", "frequency": "month",
            "pre_periods": pre, "post_periods": post, "observations": observations,
            "max_pre_rmse": 0.01, "unit_of_analysis": "门店", "metric_unit": "元",
            "design": {
                "no_anticipation": True, "no_spillovers": True, "comparable_measurement": True,
                "selection_protocol": "按处理前独立的周期扰动确认参照门店，未使用处理后拟合选择",
            },
        })
        fit = next(row["data"] for row in result["sections"] if row["id"] == "incrementality_synthetic_control")
        self.assertEqual(fit["state"], "READY")
        self.assertEqual(fit["diagnostics"]["declared_donor_count"], 21)
        weights = {row["unit_id"]: row["weight"] for row in fit["fit"]["weights"]}
        self.assertAlmostEqual(weights["donor-00"], 0.5, delta=1e-6)
        self.assertAlmostEqual(weights["donor-01"], 0.5, delta=1e-6)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=8)
        self.assertAlmostEqual(fit["fit"]["mean_post_gap"], 4.0, places=7)

    def test_synthetic_control_accepts_regular_horizons_over_old_five_hundred_period_limit(self):
        from datetime import date, timedelta

        periods = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(503)]
        pre, post = periods[:501], periods[501:]
        observations = []
        for index, period in enumerate(periods):
            baseline = 50.0 + index / 10
            donor_a = baseline + (2.0 if index % 2 == 0 else -2.0)
            donor_b = baseline - (2.0 if index % 2 == 0 else -2.0)
            observations.extend([
                {"unit_id": "treated", "role": "treated", "period": period,
                 "value": baseline + (3.0 if index >= 501 else 0.0)},
                {"unit_id": "donor-a", "role": "donor", "untreated_declared": True,
                 "period": period, "value": donor_a},
                {"unit_id": "donor-b", "role": "donor", "untreated_declared": True,
                 "period": period, "value": donor_b},
            ])
        result = evaluate_measurement("incrementality_analysis", {
            "estimator": "synthetic_control", "frequency": "day",
            "pre_periods": pre, "post_periods": post, "observations": observations,
            "max_pre_rmse": 0.01, "unit_of_analysis": "门店", "metric_unit": "元",
            "design": {
                "no_anticipation": True, "no_spillovers": True, "comparable_measurement": True,
                "selection_protocol": "日序列仅用相同营业与指标口径的既有门店",
            },
        })
        fit = next(row["data"] for row in result["sections"] if row["id"] == "incrementality_synthetic_control")
        self.assertEqual(fit["state"], "READY")
        self.assertEqual(fit["pre_n"], 501)
        self.assertEqual(len(fit["fit"]["post_observed_minus_synthetic"]), 2)

    def test_synthetic_control_observes_cancellation_in_observation_loop(self):
        calls = {"count": 0}

        def is_cancelled():
            calls["count"] += 1
            return calls["count"] >= 20

        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_measurement("incrementality_analysis", {
                "estimator": "synthetic_control", "frequency": "month",
                "pre_periods": [f"2025-{month:02d}" for month in range(1, 13)],
                "post_periods": ["2026-01", "2026-02"],
                "observations": [{"unit_id": f"u-{index}", "role": "treated" if index == 0 else "donor",
                                  "untreated_declared": None if index == 0 else True,
                                  "period": "2025-01", "value": index + 1}
                                 for index in range(30)],
                "max_pre_rmse": 1,
                "design": {"no_anticipation": True, "no_spillovers": True, "comparable_measurement": True, "selection_protocol": "test"},
            }, is_cancelled=is_cancelled)

    def test_synthetic_control_cancels_during_simplex_optimizer(self):
        pre = [f"2025-{month:02d}" for month in range(1, 13)]
        post = ["2026-01", "2026-02"]
        observations = []
        periods = pre + post
        for index, period in enumerate(periods):
            baseline = 20.0 + index
            sign = 1.0 if index % 2 else -1.0
            observations.extend([
                {"unit_id": "treated", "role": "treated", "period": period,
                 "value": baseline + (4.0 if index >= len(pre) else 0.0)},
                {"unit_id": "donor-a", "role": "donor", "untreated_declared": True,
                 "period": period, "value": baseline + sign},
                {"unit_id": "donor-b", "role": "donor", "untreated_declared": True,
                 "period": period, "value": baseline - sign},
            ])
        calls = {"count": 0}

        def cancel_during_solver():
            calls["count"] += 1
            return calls["count"] >= 48

        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_measurement("incrementality_analysis", {
                "estimator": "synthetic_control", "frequency": "month",
                "pre_periods": pre, "post_periods": post, "observations": observations,
                "max_pre_rmse": 0.01, "unit_of_analysis": "门店", "metric_unit": "元",
                "design": {
                    "no_anticipation": True, "no_spillovers": True, "comparable_measurement": True,
                    "selection_protocol": "处理前同口径，未使用处理后结果选择供体",
                },
            }, is_cancelled=cancel_during_solver)
        self.assertEqual(calls["count"], 48)


if __name__ == "__main__":
    unittest.main()
