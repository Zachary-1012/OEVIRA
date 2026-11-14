"""Business time-series operation checks on isolated synthetic observations."""
from __future__ import annotations

from calendar import monthrange
from datetime import date
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oevira.business_analysis_economics import BusinessAnalysisCancelled, evaluate_economics


def monthly_observations(count: int, *, step_change_at: int | None = None, basis: str = "actual") -> list[dict]:
    rows = []
    start = date(2024, 1, 1)
    for index in range(count):
        month_number = start.year * 12 + start.month - 1 + index
        year, month0 = divmod(month_number, 12)
        month = month0 + 1
        period_start = date(year, month, 1)
        period_end = date(year, month, monthrange(year, month)[1])
        seasonal = 8 * math.sin(2 * math.pi * index / 12)
        observation_noise = ((index * 37) % 11 - 5) * 1.7
        shift = 25 if step_change_at is not None and index >= step_change_at else 0
        value = 100 + index * 0.6 + seasonal + observation_noise + shift
        rows.append({
            "id": f"month-{index + 1:03d}",
            "entity": {"id": "store-1", "name": "样例门店"},
            "metric": {
                "key": "net_sales", "definition": "扣退款后的门店月销售额",
                "value": value, "unit": "CNY", "currency": "CNY", "basis": basis,
                "denominator": None, "comparison_dimensions": ["area_m2", "tenant_scope"],
            },
            "period": {
                "start": period_start.isoformat(), "end": period_end.isoformat(),
                "frequency": "month", "timezone": "Asia/Shanghai",
            },
            "scope": {"area_m2": 420, "tenant_scope": "same_store"},
            "coverage": {"observed": 30, "eligible": 30},
            "review": {"status": "supported", "note": "synthetic test fact"},
            "source": {"title": "isolated synthetic business table"},
        })
    return rows


class BusinessTimeSeriesTests(unittest.TestCase):
    def test_stl_arima_holdout_forecast_and_labeled_monitoring(self):
        observations = monthly_observations(60)
        event_labels = [
            {"observation_id": row["id"], "state": "event" if index >= 58 else "no_event",
             "source_ref": {"material_id": "synthetic-independent-labels"}}
            for index, row in enumerate(observations[-6:], start=54)
        ]
        result = evaluate_economics("operating_time_series", {
            "question": "评估门店月销售趋势并预测未来三个月",
            "observations": observations,
            "seasonal_cycle_periods": 12,
            "holdout_periods": 6,
            "forecast_horizon": 3,
            "event_labels": event_labels,
        })
        sections = {section["id"]: section for section in result["sections"]}
        series = sections["time_series.trend_decomposition"]["data"]["series"][0]
        self.assertEqual(series["method_state"], "AVAILABLE")
        self.assertEqual(series["decomposition"]["state"], "READY")
        self.assertEqual(series["backtest"]["state"], "READY")
        self.assertEqual(series["backtest"]["holdout_n"], 6)
        self.assertTrue(math.isfinite(series["backtest"]["metrics"]["mae"]))
        self.assertEqual(series["backtest"]["monitoring"]["label_evaluation"]["state"], "LABELED_HOLDOUT_ONLY")
        self.assertEqual(series["forecast"]["state"], "CONDITIONAL_FORECAST")
        self.assertEqual(len(series["forecast"]["periods"]), 3)
        self.assertEqual(series["forecast"]["periods"][0]["period"]["start"], "2029-01-01")
        self.assertIn("2024-01-01", sections["time_series.observations"]["body"])
        self.assertIn("2029-01-01", sections["time_series.forecast"]["body"])
        self.assertIn("ARIMA(1,1,0)", sections["time_series.forecast"]["body"])

    def test_ets_point_forecast_preserves_unknown_interval_and_compares_baselines(self):
        observations = monthly_observations(60)
        result = evaluate_economics("operating_time_series", {
            "observations": observations,
            "seasonal_cycle_periods": 12,
            "holdout_periods": 6,
            "forecast_horizon": 3,
            "forecast_method": "ets",
        })
        sections = {section["id"]: section for section in result["sections"]}
        series = sections["time_series.trend_decomposition"]["data"]["series"][0]
        backtest = series["backtest"]
        self.assertEqual(backtest["state"], "READY")
        self.assertTrue(backtest["method"].startswith("ETS("))
        self.assertEqual(backtest["prediction_interval_state"], "NOT_ESTIMATED_BY_HOLT_WINTERS_METHOD")
        self.assertIsNone(backtest["observed_interval_coverage"])
        self.assertIsNone(backtest["holdout"][0]["lower_95"])
        self.assertIsNotNone(backtest["seasonal_naive_metrics"])
        self.assertIn("季节朴素MAE", sections["time_series.backtest"]["body"])
        future = series["forecast"]
        self.assertEqual(future["state"], "CONDITIONAL_FORECAST")
        self.assertIsNone(future["intervals_95"])
        self.assertIsNone(future["periods"][0]["upper_95"])

    def test_pelt_reports_retrospective_step_change_without_claiming_cause(self):
        observations = monthly_observations(36, step_change_at=18)
        result = evaluate_economics("operating_time_series", {"observations": observations})
        sections = {section["id"]: section for section in result["sections"]}
        change = sections["time_series.change_points"]["data"]["series"][0]["change_points"]
        self.assertEqual(change["state"], "READY")
        self.assertTrue(any(abs(index - 18) <= 1 for index in change["break_indices"]))
        self.assertIn("回顾性变点，不代表因果事件", sections["time_series.change_points"]["body"])
        self.assertEqual(sections["time_series.forecast"]["data"]["series"][0]["forecast"]["state"], "NOT_REQUESTED")

    def test_pelt_reports_label_overlap_as_retrospective_not_holdout_quality(self):
        observations = monthly_observations(36, step_change_at=18)
        event_labels = [
            {"observation_id": observations[2]["id"], "state": "no_event", "source_ref": {"title": "independent event log"}},
            {"observation_id": observations[18]["id"], "state": "event", "source_ref": {"title": "independent event log"}},
        ]
        result = evaluate_economics("operating_time_series", {
            "observations": observations, "event_labels": event_labels,
        })
        section = next(row for row in result["sections"] if row["id"] == "time_series.change_points")
        series = section["data"]["series"][0]
        comparison = series["change_points"]["label_comparison"]
        self.assertEqual(comparison["state"], "RETROSPECTIVE_LABEL_COMPARISON_ONLY")
        self.assertEqual(comparison["true_positive"], 1)
        self.assertEqual(comparison["false_positive"], 0)
        self.assertEqual(comparison["false_negative"], 0)
        self.assertEqual(comparison["true_negative"], 1)
        self.assertEqual(comparison["precision"], 1)
        self.assertEqual(comparison["recall"], 1)
        self.assertEqual(comparison["false_alarm_rate"], 0)
        self.assertIn("不是独立留出", section["body"])

    def test_missing_period_estimates_and_invalid_values_do_not_get_filled(self):
        observations = monthly_observations(30)
        observations.pop(12)
        gap_result = evaluate_economics("operating_time_series", {"observations": observations})
        gap_sections = {section["id"]: section for section in gap_result["sections"]}
        gap_series = gap_sections["time_series.trend_decomposition"]["data"]["series"][0]
        self.assertEqual(gap_series["method_state"], "INSUFFICIENT")
        self.assertEqual(gap_series["reason"], "PERIODS_HAVE_GAPS_OR_INCONSISTENT_FREQUENCY")

        estimate = monthly_observations(30, basis="estimate")
        estimate_result = evaluate_economics("operating_time_series", {"observations": estimate})
        estimate_sections = {section["id"]: section for section in estimate_result["sections"]}
        self.assertEqual(estimate_sections["time_series.trend_decomposition"]["data"]["series"][0]["reason"], "ESTIMATES_AND_FORECASTS_ARE_NOT_MIXED_WITH_ACTUALS")

        invalid = monthly_observations(30)
        invalid[3]["metric"]["value"] = float("nan")
        with self.assertRaises(ValueError):
            evaluate_economics("operating_time_series", {"observations": invalid})

    def test_cancel_callback_interrupts_before_model_work(self):
        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_economics("operating_time_series", {"observations": []}, is_cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
