from __future__ import annotations

import math
import unittest

import numpy as np
from scipy.integrate import quad

from oevira.business_analysis_events import evaluate_events, _hawkes_expected


class BusinessEventAnalysisTests(unittest.TestCase):
    def test_observed_network_keeps_layers_and_matches_independent_linear_solution(self):
        identifiers = ["a", "b", "c", "d"]
        edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
        result = evaluate_events("propagation_network", {
            "nodes": [{"id": value, "label": f"账号{index + 1}", "platform": "同一观察平台"} for index, value in enumerate(identifiers)],
            "edges": [{"id": str(index), "source": first, "target": second, "relation": "repost", "source_refs": ["isolated-observation"]} for index, (first, second) in enumerate(edges)] +
                     [{"id": "collaboration", "source": "d", "target": "a", "relation": "collaboration", "source_refs": ["separate-layer"]}],
            "relation": "repost", "data_kind": "synthetic",
        })
        network = result["sections"][0]["data"]
        self.assertEqual(network["calculation_edges"], 4)
        self.assertEqual(len(network["raw_edges"]), 5)
        transition = np.array([[0, .5, .5, 0], [0, 0, 0, 1], [0, 0, 0, 1], [.25, .25, .25, .25]])
        expected = np.linalg.solve(np.eye(4) - .85 * transition.T, np.repeat(.15 / 4, 4))
        values = {item["id"]: item for item in network["centrality"]}
        for index, identifier in enumerate(identifiers):
            self.assertAlmostEqual(values[identifier]["pagerank"], expected[index], places=9)
        self.assertAlmostEqual(values["b"]["betweenness"], 1 / 12)
        self.assertAlmostEqual(values["c"]["betweenness"], 1 / 12)
        self.assertEqual(network["hits"]["status"], "non_unique_dominant_mode")
        self.assertIn("模拟输入", result["limitations"][0])

    def test_missing_relations_do_not_become_follower_based_graph(self):
        result = evaluate_events("propagation_network", {"nodes": [{"id": "a", "label": "账号甲", "platform": "平台", "followers": 100000}], "edges": [], "relation": "mention"})
        self.assertEqual(result["sections"][0]["data"]["centrality"], [])
        self.assertEqual(result["sections"][0]["data"]["status"], "no_observed_relations")

    def test_hawkes_expected_count_matches_numerical_integration(self):
        fit = {"baseline_intensity": .4, "branching_ratio": .55, "decay_rate": 1.7}
        times, end, horizon = [1, 2, 3.1, 3.5, 4.9], 5, 3
        initial = fit["baseline_intensity"] + fit["branching_ratio"] * fit["decay_rate"] * sum(math.exp(-fit["decay_rate"] * (end - value)) for value in times)
        equilibrium = fit["baseline_intensity"] / (1 - fit["branching_ratio"])
        expected, _ = quad(lambda future: equilibrium + (initial - equilibrium) * math.exp(-fit["decay_rate"] * (1 - fit["branching_ratio"]) * future), 0, horizon)
        self.assertAlmostEqual(_hawkes_expected(fit, times, end, horizon), expected, places=10)

    def test_tied_and_unverified_event_times_keep_timeline_without_fake_fit(self):
        result = evaluate_events("propagation_hawkes", {"events": [{"id": "1", "time": 1, "source_refs": ["source"]}, {"id": "2", "time": 1, "source_refs": ["source"]}, {"id": "3", "time": None}], "observation_end": 10, "time_unit": "小时"})
        model = result["sections"][1]["data"]
        self.assertIsNone(model["fit"])
        self.assertEqual(model["observed_count"], 2)
        self.assertIsNone(model["finite_peak_time"])
        self.assertTrue(any("并列时间" in item for item in model["blockers"]))

    def test_bass_recovers_known_curve_with_disjoint_time_check(self):
        p, q, total = .035, .38, 1000
        def curve(at):
            decay = math.exp(-(p + q) * at)
            return total * (1 - decay) / (1 + q / p * decay)
        inputs = {"data_kind": "synthetic", "time_unit": "周", "observations": [{"time": at, "cumulative": curve(at), "source_refs": ["synthetic-known-curve"]} for at in range(1, 41)],
                  "qualification": {key: {"status": "supported", "basis": "Known construction of this isolated numerical case", "source_refs": ["synthetic-protocol"]} for key in ("fixed_population", "one_adoption_per_unit", "known_launch", "coverage")}}
        result = evaluate_events("adoption_diffusion", inputs)
        model = result["sections"][1]["data"]
        self.assertEqual(model["status"], "conditional_fit")
        self.assertAlmostEqual(model["fit"]["innovation"], p, places=5)
        self.assertAlmostEqual(model["fit"]["imitation"], q, places=5)
        self.assertAlmostEqual(model["fit"]["market_potential"], total, places=2)
        self.assertLess(model["holdout"]["mae"], .01)
        self.assertEqual(model["holdout"]["train_count"], 20)
        self.assertEqual(model["holdout"]["test_count"], 20)

    def test_invalid_numeric_values_and_cancellation_do_not_publish_models(self):
        for invalid in (True, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                evaluate_events("adoption_diffusion", {"time_unit": "日", "observations": [{"time": 1, "cumulative": invalid}]})
        from oevira.business_analysis_economics import BusinessAnalysisCancelled
        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_events("propagation_network", {"nodes": [], "edges": [], "relation": "repost"}, is_cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
