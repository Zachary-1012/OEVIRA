from __future__ import annotations

import copy
import unittest

from server.oevira.intelligence_math_107 import (
    TensorMathError,
    TensorMathBudget,
    execute_bilinear_tensor_product,
    exact_standard_matrix_product,
    search_square_9_4_witness,
    verify_bilinear_tensor_witness,
)


def rank_eight_two_by_two_witness() -> dict:
    alpha = [[[0 for _ in range(2)] for _ in range(2)] for _ in range(8)]
    beta = [[[0 for _ in range(2)] for _ in range(2)] for _ in range(8)]
    gamma = [[[0 for _ in range(8)] for _ in range(2)] for _ in range(2)]
    operation = 0
    for row in range(2):
        for shared in range(2):
            for column in range(2):
                alpha[operation][row][shared] = 1
                beta[operation][shared][column] = 1
                gamma[row][column][operation] = 1
                operation += 1
    return {
        "a": 2, "b": 2, "c": 2, "rank": 8,
        "modulus": [0, 1],
        "alpha": alpha, "beta": beta, "gamma": gamma,
    }


class ExactBilinearWitnessTests(unittest.TestCase):
    def test_all_coefficients_verify_and_recursive_execution_matches_independent_baseline(self) -> None:
        witness = rank_eight_two_by_two_witness()
        verified = verify_bilinear_tensor_witness(
            witness, budget=TensorMathBudget(max_steps=100_000, max_integer_bits=10_000, max_matrix_cells=10_000),
        )
        self.assertEqual(verified["state"], "VERIFIED")
        self.assertEqual(verified["identity_coefficients_checked"], 64)
        self.assertEqual(verified["global_9_4_claim"], "NOT_ESTABLISHED_BY_FINITE_WITNESS")
        bound_check = verify_bilinear_tensor_witness(witness, epsilon=1)
        self.assertFalse(bound_check["finite_rank_bound"]["finite_bound_met"])

        matrix_a = [[1, 2], [3, 4], [5, 6]]
        matrix_b = [[7, 8, 9, 10], [11, 12, 13, 14]]
        standard = exact_standard_matrix_product(matrix_a, matrix_b)
        self.assertEqual(standard["state"], "READY")
        tensor = execute_bilinear_tensor_product(
            matrix_a, matrix_b, witness,
            budget=TensorMathBudget(max_steps=100_000, max_integer_bits=10_000, max_matrix_cells=10_000),
        )
        self.assertEqual(tensor["state"], "READY")
        decoded = [[cell["numerator"] // cell["denominator"] for cell in row] for row in tensor["result"]]
        standard_values = [[cell["numerator"] // cell["denominator"] for cell in row] for row in standard["matrix"]]
        self.assertEqual(decoded, standard_values)
        self.assertEqual(decoded, [[29, 32, 35, 38], [65, 72, 79, 86], [101, 112, 123, 134]])

    def test_coefficient_corruption_is_rejected_and_partial_budget_has_no_result(self) -> None:
        witness = rank_eight_two_by_two_witness()
        corrupted = copy.deepcopy(witness)
        corrupted["gamma"][0][0][0] = 0
        with self.assertRaises(TensorMathError) as invalid:
            verify_bilinear_tensor_witness(corrupted)
        self.assertEqual(invalid.exception.code, "TENSOR_IDENTITY_INVALID")

        partial = execute_bilinear_tensor_product(
            [[1, 2], [3, 4]], [[5, 6], [7, 8]], witness,
            budget=TensorMathBudget(max_steps=1, max_integer_bits=10_000, max_matrix_cells=10_000),
        )
        self.assertEqual(partial["state"], "PARTIAL")
        self.assertIsNone(partial["result"])
        self.assertEqual(partial["reason"], "RESOURCE_BUDGET_EXCEEDED")

    def test_fixed_height_parameter_search_reports_resumable_incomplete_not_nonexistence(self) -> None:
        result = search_square_9_4_witness(
            1,
            budget=TensorMathBudget(max_steps=3, max_integer_bits=10_000, max_matrix_cells=10_000),
        )
        self.assertEqual(result["state"], "PARTIAL")
        self.assertEqual(result["reason"], "RESOURCE_BUDGET_EXCEEDED")
        self.assertIsNone(result["witness"])
        self.assertEqual(result["checkpoint"]["height"], 4)
        self.assertEqual((result["checkpoint"]["u"], result["checkpoint"]["rank"],
                          result["checkpoint"]["field_degree"]), (2, 4, 1))
        self.assertEqual(result["global_9_4_claim"], "NOT_ESTABLISHED")
