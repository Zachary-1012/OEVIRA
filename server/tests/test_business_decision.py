from copy import deepcopy
import unittest

from oevira.business_analysis_decision import evaluate_decision
from oevira.business_analysis_economics import BusinessAnalysisCancelled


def inputs():
    return {"question": "在同一预算范围中选择活动方案", "criteria": [
        {"id": "value", "name": "条件收益", "definition": "同一情景的条件收益", "unit": "万元", "direction": "maximize", "weight": 0.5},
        {"id": "cost", "name": "预算成本", "definition": "同一含税成本口径", "unit": "万元", "direction": "minimize", "weight": 0.5}],
        "constraints": [{"id": "budget", "description": "已确认预算内可交付"}],
        "alternatives": [
            {"id": "a", "title": "门店体验方案", "values": {"value": 100, "cost": 60}, "constraint_checks": {"budget": {"status": "satisfied", "basis": "已确认报价合计60万元"}}},
            {"id": "b", "title": "大型传播方案", "values": {"value": 60, "cost": 80}, "constraint_checks": {"budget": {"status": "satisfied", "basis": "已确认报价合计80万元"}}},
            {"id": "c", "title": "超预算方案", "values": {"value": 1000, "cost": 90}, "constraint_checks": {"budget": {"status": "violated", "basis": "已确认预算仅80万元"}}}]}


def data(result, section):
    return next(item["data"] for item in result["sections"] if item["id"] == section)


class BusinessDecisionTests(unittest.TestCase):
    def test_dominated_and_infeasible_options_cannot_become_weighted_winner(self):
        result = evaluate_decision("decision_comparison", inputs())
        rankings = data(result, "decision.comparison")["rankings"]
        self.assertEqual([item["id"] for item in rankings], ["a", "b"])
        self.assertEqual(rankings[0]["topsis_closeness"], 1)
        self.assertEqual(rankings[1]["topsis_closeness"], 0)
        self.assertEqual(rankings[1]["dominated_by"], ["a"])
        self.assertEqual(data(result, "decision.options")["alternatives"][2]["eligibility"], "infeasible")
        self.assertIn("100", result["sections"][1]["body"])
        self.assertIn("0.5000", result["sections"][2]["body"])

    def test_missing_preferences_do_not_silently_become_equal_weights(self):
        request = inputs()
        for criterion in request["criteria"]:
            del criterion["weight"]
        result = evaluate_decision("decision_comparison", request)
        comparison = data(result, "decision.comparison")
        self.assertIsNone(comparison["weights"])
        self.assertTrue(all(item["topsis_closeness"] is None for item in comparison["rankings"]))
        self.assertTrue(comparison["rankings"][0]["pareto_efficient"])

    def test_exact_numeric_budget_overrides_an_incorrect_feasibility_claim(self):
        request = inputs()
        request["constraints"][0].update(criterion_id="cost", operator="<=", value="80.000000000000000000")
        request["alternatives"][1]["values"]["cost"] = "80.000000000000000001"
        request["alternatives"][1]["constraint_checks"]["budget"] = {"status": "satisfied", "basis": "用户最初认为仍在80以内"}
        result = evaluate_decision("decision_comparison", request)
        option = data(result, "decision.options")["alternatives"][1]
        self.assertEqual(option["eligibility"], "infeasible")
        self.assertEqual(option["constraint_checks"]["budget"]["status"], "violated")
        self.assertEqual(option["constraint_checks"]["budget"]["user_check"]["status"], "satisfied")
        self.assertIn("80.000000000000000001", result["sections"][1]["body"])

    def test_ahp_recovers_independent_ratio_vector_and_checks_attributed_random_index(self):
        request = inputs()
        request["criteria"].append({"id": "time", "name": "交付时间", "definition": "工作日", "unit": "天", "direction": "minimize"})
        for criterion in request["criteria"]:
            criterion.pop("weight", None)
        for option in request["alternatives"]:
            option["values"]["time"] = 10
        priorities = [0.5, 0.3, 0.2]
        request["pairwise_preferences"] = {"matrix": [[a / b for b in priorities] for a in priorities], "random_index": {"order": 3, "value": 0.58, "source_ref": "隔离测试明确给定的同阶参照，不作默认产品常数"}}
        result = evaluate_decision("decision_comparison", request)
        comparison = data(result, "decision.comparison")
        for key, expected in zip(("value", "cost", "time"), priorities):
            self.assertAlmostEqual(comparison["weights"][key], expected, places=10)
        self.assertAlmostEqual(comparison["preference_diagnostic"]["consistency_ratio"], 0, places=10)
        del request["pairwise_preferences"]["random_index"]
        automatic = evaluate_decision("decision_comparison", request)
        self.assertEqual(data(automatic, "decision.comparison")["preference_diagnostic"]["random_index"]["value"], 0.58)

    def test_inconsistent_judgments_cannot_be_laundered_by_an_invented_random_index(self):
        request = inputs()
        request["criteria"].append({"id": "time", "name": "交付时间", "definition": "工作日", "unit": "天", "direction": "minimize"})
        for criterion in request["criteria"]:
            criterion.pop("weight", None)
        for option in request["alternatives"]:
            option["values"]["time"] = 10
        request["pairwise_preferences"] = {"matrix": [[1, 9, 1 / 9], [1 / 9, 1, 9], [9, 1 / 9, 1]], "consistency_threshold": .1}
        result = evaluate_decision("decision_comparison", request)
        diagnostic = data(result, "decision.comparison")["preference_diagnostic"]
        self.assertAlmostEqual(diagnostic["lambda_max"], 91 / 9, places=10)
        self.assertAlmostEqual(diagnostic["consistency_index"], 32 / 9, places=10)
        self.assertTrue(diagnostic["reference_threshold_exceeded"])
        request["pairwise_preferences"]["random_index"] = {"order": 3, "value": 100, "source_ref": "not-a-saved-source"}
        with self.assertRaisesRegex(ValueError, "pinned same-order"):
            evaluate_decision("decision_comparison", request)

    def test_rank_sensitivity_retains_opposing_preferences_and_equal_values(self):
        request = inputs()
        request["alternatives"] = request["alternatives"][:2]
        request["alternatives"][0]["values"] = {"value": 10, "cost": 100}
        request["alternatives"][1]["values"] = {"value": 8, "cost": 20}
        result = evaluate_decision("decision_comparison", request)
        path = data(result, "decision.sensitivity")["weight_paths"][0]
        self.assertEqual(path["leader_transitions"][0]["leaders"], ["b"])
        self.assertEqual(path["leader_transitions"][-1]["leaders"], ["a"])
        request["alternatives"][1]["values"] = deepcopy(request["alternatives"][0]["values"])
        tied = evaluate_decision("decision_comparison", request)
        self.assertTrue(all(item["topsis_closeness"] is None for item in data(tied, "decision.comparison")["rankings"]))

    def test_unknown_conditions_and_missing_values_remain_visible(self):
        request = inputs()
        request["alternatives"][0]["constraint_checks"] = {}
        request["alternatives"][1]["values"]["cost"] = None
        result = evaluate_decision("decision_comparison", request)
        self.assertEqual([item["eligibility"] for item in data(result, "decision.options")["alternatives"]], ["conditions_unknown", "data_incomplete", "infeasible"])
        self.assertEqual(data(result, "decision.comparison")["rankings"], [])

    def test_cancel_during_weight_sensitivity(self):
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 20
        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_decision("decision_comparison", inputs(), is_cancelled=cancelled)


if __name__ == "__main__":
    unittest.main()
