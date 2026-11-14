from __future__ import annotations

import unittest
from fractions import Fraction

from server.oevira.intelligence_math import (
    MeanPayoffBudget,
    _LabelCall,
    _WorkBudget,
    _fixed_strategy_markov_values,
    _read_game,
    _run_label,
    evaluate_mean_payoff_exact_values,
    evaluate_mean_payoff_optimal_strategies,
    evaluate_mean_payoff_winning_region,
    evaluate_mean_payoff_parity_winning_region,
    evaluate_single_state_mean_payoff_parity,
)


class MeanPayoffClosedStateTests(unittest.TestCase):
    def test_recursive_label_respects_exact_midpoint_and_positive_offset(self) -> None:
        budget = MeanPayoffBudget(max_steps=2_000_000, max_integer_bits=200_000, max_frames=100_000)
        width = 128 << 12
        lower = (-(width // 2),)
        for reward, expected in ((0, ("-",)), (1, ("+",))):
            game = {
                "vertices": [{"id": "v", "owner": "max"}],
                "edges": [{"id": "loop", "source": "v", "target": "v", "reward": reward}],
            }
            vertices, _identifiers, _denominator_product = _read_game(game)
            label = _run_label(
                _LabelCall(lower, width, (Fraction(1),), (Fraction(1),)),
                vertices, 1_000, 128, _WorkBudget(budget, None),
            )
            self.assertEqual(label, expected)

    def test_max_and_min_self_loop_values_and_stable_strategy(self) -> None:
        budget = MeanPayoffBudget(max_steps=10_000, max_integer_bits=10_000, max_frames=100)
        maximize = {
            "vertices": [{"id": "v", "owner": "max"}],
            "edges": [
                {"id": "z-tied", "source": "v", "target": "v", "reward": 2},
                {"id": "a-tied", "source": "v", "target": "v", "reward": 2},
                {"id": "lower", "source": "v", "target": "v", "reward": -1},
            ],
        }
        max_result = evaluate_mean_payoff_winning_region(maximize, budget=budget)
        self.assertEqual(max_result["algorithm"], "104-single-closed-state-exact@1")
        self.assertEqual(max_result["winner_vertices"], ["v"])
        self.assertEqual(max_result["mean_payoff_values"]["v"], {"numerator": 2, "denominator": 1})
        self.assertEqual(max_result["maximizer_strategy"]["v"], "a-tied")
        exact_max = evaluate_mean_payoff_exact_values(maximize, budget=budget)
        self.assertEqual(exact_max["mean_payoff_values"]["v"], {"numerator": 2, "denominator": 1})
        solved_strategy = evaluate_mean_payoff_optimal_strategies(maximize, budget=budget)
        self.assertEqual(solved_strategy["state"], "READY")
        self.assertEqual(solved_strategy["maximizer_strategy"]["v"], "a-tied")
        self.assertEqual(solved_strategy["markov_mean_payoff_values"]["v"], {"numerator": 2, "denominator": 1})

        minimize = {
            "vertices": [{"id": "v", "owner": "min"}],
            "edges": [
                {"id": "positive", "source": "v", "target": "v", "reward": 1},
                {"id": "negative", "source": "v", "target": "v", "reward": -2},
            ],
        }
        min_result = evaluate_mean_payoff_winning_region(minimize, budget=budget)
        self.assertEqual(min_result["winner_vertices"], [])
        self.assertEqual(min_result["mean_payoff_values"]["v"], {"numerator": -2, "denominator": 1})
        self.assertEqual(min_result["minimizer_strategy"]["v"], "negative")

    def test_chance_self_loop_uses_exact_expectation_not_sampled_frequency(self) -> None:
        problem = {
            "vertices": [{"id": "v", "owner": "chance"}],
            "edges": [
                {"id": "down", "source": "v", "target": "v", "reward": -1,
                 "probability": {"numerator": 1, "denominator": 3}},
                {"id": "up", "source": "v", "target": "v", "reward": 1,
                 "probability": {"numerator": 2, "denominator": 3}},
            ],
        }
        result = evaluate_mean_payoff_winning_region(
            problem, budget=MeanPayoffBudget(max_steps=10_000, max_integer_bits=10_000, max_frames=100),
        )
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["winner_vertices"], ["v"])
        self.assertEqual(result["mean_payoff_values"]["v"], {"numerator": 1, "denominator": 3})
        self.assertEqual(result["chance_probabilities"], "EXACT_INPUT_RATIONALS_IID_SINGLE_STATE")
        self.assertEqual(result["algorithm_randomness"], "NONE")
        exact = evaluate_mean_payoff_exact_values(problem)
        self.assertEqual(exact["mean_payoff_values"]["v"], {"numerator": 1, "denominator": 3})

    def test_single_state_parity_is_conjoined_with_mean_payoff(self) -> None:
        even = {
            "vertices": [{"id": "v", "owner": "max", "priority": 2}],
            "edges": [{"id": "loop", "source": "v", "target": "v", "reward": 1}],
        }
        even_result = evaluate_single_state_mean_payoff_parity(even)
        self.assertEqual(even_result["winner_vertices"], ["v"])
        self.assertEqual(even_result["parity_state"], "EVEN")

        odd = {
            "vertices": [{"id": "v", "owner": "max", "priority": 1}],
            "edges": [{"id": "loop", "source": "v", "target": "v", "reward": 1}],
        }
        odd_result = evaluate_single_state_mean_payoff_parity(odd)
        self.assertEqual(odd_result["winner_vertices"], [])
        self.assertEqual(odd_result["mean_payoff_values"]["v"], {"numerator": 1, "denominator": 1})

        incomplete = {
            "vertices": [
                {"id": "v", "owner": "max", "priority": 2},
                {"id": "u", "owner": "min", "priority": 3},
            ],
            "edges": [
                {"id": "v-u", "source": "v", "target": "u", "reward": 1},
                {"id": "u-u", "source": "u", "target": "u", "reward": 1},
            ],
        }
        incomplete_result = evaluate_single_state_mean_payoff_parity(incomplete)
        self.assertEqual(incomplete_result["state"], "PARTIAL")
        self.assertEqual(incomplete_result["reason"], "PARITY_GENERAL_ALGORITHM_NOT_IMPLEMENTED")
        self.assertIsNone(incomplete_result["winner_vertices"])

    def test_exact_markov_recheck_uses_stationary_mass_and_transient_absorption(self) -> None:
        problem = {
            "vertices": [
                {"id": "entry", "owner": "max"},
                {"id": "A", "owner": "chance"},
                {"id": "B", "owner": "chance"},
            ],
            "edges": [
                {"id": "entry-A", "source": "entry", "target": "A", "reward": 99},
                {"id": "A-A", "source": "A", "target": "A", "reward": 1,
                 "probability": {"numerator": 1, "denominator": 2}},
                {"id": "A-B", "source": "A", "target": "B", "reward": 3,
                 "probability": {"numerator": 1, "denominator": 2}},
                {"id": "B-A", "source": "B", "target": "A", "reward": 4,
                 "probability": {"numerator": 1, "denominator": 4}},
                {"id": "B-B", "source": "B", "target": "B", "reward": 0,
                 "probability": {"numerator": 3, "denominator": 4}},
            ],
        }
        values = _fixed_strategy_markov_values(
            problem, {"entry": "entry-A"}, {},
            _WorkBudget(MeanPayoffBudget(max_steps=10_000, max_integer_bits=10_000, max_frames=100), None),
        )
        self.assertEqual(values, {"entry": Fraction(4, 3), "A": Fraction(4, 3), "B": Fraction(4, 3)})

    def test_general_parity_recursion_conjoins_average_and_minimum_infinite_priority(self) -> None:
        odd_escape = {
            "vertices": [
                {"id": "odd", "owner": "max", "reward": 0, "priority": 1},
                {"id": "even", "owner": "min", "reward": 0, "priority": 2},
            ],
            "edges": [
                {"id": "odd-loop", "source": "odd", "target": "odd"},
                {"id": "even-to-odd", "source": "even", "target": "odd"},
                {"id": "even-loop", "source": "even", "target": "even"},
            ],
        }
        min_wins = evaluate_mean_payoff_parity_winning_region(odd_escape)
        self.assertEqual(min_wins["state"], "READY")
        self.assertEqual(min_wins["winner_vertices"], [])
        self.assertEqual(min_wins["loser_vertices"], ["even", "odd"])

        even_positive = {
            "vertices": [{"id": "v", "owner": "max", "reward": 1, "priority": 2}],
            "edges": [{"id": "loop", "source": "v", "target": "v"}],
        }
        max_wins = evaluate_mean_payoff_parity_winning_region(even_positive)
        self.assertEqual(max_wins["state"], "READY")
        self.assertEqual(max_wins["winner_vertices"], ["v"])
