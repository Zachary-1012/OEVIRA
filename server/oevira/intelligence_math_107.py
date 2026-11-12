"""Exact finite bilinear tensor witnesses and recursive matrix execution.

This module accepts only exact rational coefficient data. A verified finite
witness is an executable identity; its existence, asymptotic exponent and
measured business performance are reported as separate claims.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import itertools
import json
import math
from typing import Callable


class TensorMathError(ValueError):
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


class _TensorStop(Exception):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class TensorMathBudget:
    max_steps: int = 2_000_000
    max_integer_bits: int = 200_000
    max_matrix_cells: int = 1_000_000


class _TensorWork:
    def __init__(self, budget: TensorMathBudget, is_cancelled: Callable[[], bool] | None):
        self.budget = budget
        self.is_cancelled = is_cancelled
        self.steps = 0
        self.field_multiplications = 0
        self.bilinear_products = 0

    def charge(self, *values: int | Fraction) -> None:
        self.steps += 1
        if self.steps > self.budget.max_steps:
            raise _TensorStop("RESOURCE_BUDGET_EXCEEDED")
        if self.is_cancelled is not None:
            try:
                cancelled = self.is_cancelled()
            except Exception as exc:
                raise _TensorStop("CANCELLATION_CHECK_UNAVAILABLE") from exc
            if type(cancelled) is not bool:
                raise _TensorStop("CANCELLATION_CHECK_INVALID")
            if cancelled:
                raise _TensorStop("CANCELLED")
        for value in values:
            integers = (value.numerator, value.denominator) if isinstance(value, Fraction) else (value,)
            if any(abs(item).bit_length() > self.budget.max_integer_bits for item in integers):
                raise _TensorStop("INTEGER_BUDGET_EXCEEDED")


def _fraction(value, field: str) -> Fraction:
    if type(value) is int:
        return Fraction(value)
    if (not isinstance(value, dict) or set(value) != {"numerator", "denominator"}
            or type(value.get("numerator")) is not int
            or type(value.get("denominator")) is not int or value["denominator"] <= 0):
        raise TensorMathError("TENSOR_RATIONAL_INVALID", {"field": field})
    numerator, denominator = value["numerator"], value["denominator"]
    if math.gcd(abs(numerator), denominator) != 1:
        raise TensorMathError("TENSOR_RATIONAL_NOT_REDUCED", {"field": field})
    return Fraction(numerator, denominator)


def _json_fraction(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator}


class _FiniteCoefficientAlgebra:
    def __init__(self, modulus: tuple[Fraction, ...], work: _TensorWork):
        if (not isinstance(modulus, tuple) or len(modulus) < 2 or modulus[-1] != 1
                or any(type(item) is not Fraction for item in modulus)):
            raise TensorMathError("TENSOR_FIELD_MODULUS_INVALID")
        self.modulus = modulus
        self.degree = len(modulus) - 1
        self.work = work
        self.zero = (Fraction(),) * self.degree
        self.one = (Fraction(1),) + (Fraction(),) * (self.degree - 1)

    def add(self, left: tuple[Fraction, ...], right: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
        result = tuple(a + b for a, b in zip(left, right))
        self.work.charge(*result)
        return result

    def multiply(self, left: tuple[Fraction, ...], right: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
        degree = self.degree
        product = [Fraction() for _ in range(2 * degree - 1)]
        for i, a in enumerate(left):
            for j, b in enumerate(right):
                product[i + j] += a * b
                self.work.charge(product[i + j])
        for power in range(len(product) - 1, degree - 1, -1):
            coefficient = product[power]
            if coefficient:
                for index in range(degree):
                    product[power - degree + index] -= coefficient * self.modulus[index]
                    self.work.charge(product[power - degree + index])
                product[power] = Fraction()
        self.work.field_multiplications += 1
        result = tuple(product[:degree])
        self.work.charge(*result)
        return result

    def scale(self, scalar: Fraction, value: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
        result = tuple(scalar * item for item in value)
        self.work.charge(*result)
        return result

    def parse_element(self, value, field: str) -> tuple[Fraction, ...]:
        if isinstance(value, list):
            if len(value) != self.degree:
                raise TensorMathError("TENSOR_FIELD_ELEMENT_INVALID", {"field": field})
            result = tuple(_fraction(item, f"{field}[{index}]") for index, item in enumerate(value))
        else:
            result = ( _fraction(value, field),) + (Fraction(),) * (self.degree - 1)
        self.work.charge(*result)
        return result

    def serialize_element(self, value: tuple[Fraction, ...]):
        if self.degree == 1:
            return _json_fraction(value[0])
        return [_json_fraction(item) for item in value]


@dataclass(frozen=True)
class BilinearTensorWitness:
    a: int
    b: int
    c: int
    rank: int
    algebra: _FiniteCoefficientAlgebra
    alpha: tuple
    beta: tuple
    gamma: tuple
    witness_sha256: str


def _positive_int(value, field: str) -> int:
    if type(value) is not int or value < 1:
        raise TensorMathError("TENSOR_DIMENSION_INVALID", {"field": field})
    return value


def _tensor_array(raw, shape: tuple[int, ...], algebra: _FiniteCoefficientAlgebra, field: str):
    def parse(value, depth: int, prefix: str):
        if depth == len(shape):
            return algebra.parse_element(value, prefix)
        if not isinstance(value, list) or len(value) != shape[depth]:
            raise TensorMathError("TENSOR_ARRAY_SHAPE_INVALID", {"field": prefix})
        return tuple(parse(item, depth + 1, f"{prefix}[{index}]") for index, item in enumerate(value))

    return parse(raw, 0, field)


def _read_witness(raw: dict, work: _TensorWork) -> BilinearTensorWitness:
    required = {"a", "b", "c", "rank", "modulus", "alpha", "beta", "gamma"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise TensorMathError("TENSOR_WITNESS_INVALID")
    a, b, c, rank = (_positive_int(raw[name], name) for name in ("a", "b", "c", "rank"))
    if not isinstance(raw["modulus"], list) or len(raw["modulus"]) < 2:
        raise TensorMathError("TENSOR_FIELD_MODULUS_INVALID")
    modulus = tuple(_fraction(item, f"modulus[{index}]") for index, item in enumerate(raw["modulus"]))
    if modulus[-1] != 1:
        raise TensorMathError("TENSOR_FIELD_MODULUS_INVALID")
    algebra = _FiniteCoefficientAlgebra(modulus, work)
    coefficient_count = algebra.degree * (rank * a * b + rank * b * c + a * c * rank)
    if coefficient_count > work.budget.max_matrix_cells:
        raise _TensorStop("TENSOR_COEFFICIENT_BUDGET_EXCEEDED")
    alpha = _tensor_array(raw["alpha"], (rank, a, b), algebra, "alpha")
    beta = _tensor_array(raw["beta"], (rank, b, c), algebra, "beta")
    gamma = _tensor_array(raw["gamma"], (a, c, rank), algebra, "gamma")
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return BilinearTensorWitness(
        a, b, c, rank, algebra, alpha, beta, gamma,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _rank_bound(witness: BilinearTensorWitness, epsilon) -> dict | None:
    if epsilon is None:
        return None
    eps = _fraction(epsilon, "epsilon")
    if eps <= 0:
        raise TensorMathError("TENSOR_EPSILON_INVALID")
    delta = min(eps / 2, Fraction(1, 4))
    target = Fraction(9, 4) + delta
    if witness.a != witness.b or witness.b != witness.c:
        return {"target_exponent": _json_fraction(target), "finite_bound_met": None,
                "reason": "SQUARE_WITNESS_REQUIRED_FOR_THIS_BOUND"}
    return {"target_exponent": _json_fraction(target),
            "finite_bound_met": witness.rank ** target.denominator < witness.a ** target.numerator,
            "comparison": {"left": witness.rank ** target.denominator,
                           "right": witness.a ** target.numerator}}


def _verify_witness(witness: BilinearTensorWitness, work: _TensorWork, *, epsilon=None) -> dict:
    field, a, b, c, rank = witness.algebra, witness.a, witness.b, witness.c, witness.rank
    checked = 0
    for out_row in range(a):
        for out_column in range(c):
            for left_row in range(a):
                for shared_left in range(b):
                    for shared_right in range(b):
                        for right_column in range(c):
                            total = field.zero
                            for operation in range(rank):
                                term = field.multiply(
                                    field.multiply(witness.gamma[out_row][out_column][operation],
                                                   witness.alpha[operation][left_row][shared_left]),
                                    witness.beta[operation][shared_right][right_column],
                                )
                                total = field.add(total, term)
                            expected = field.one if (
                                shared_left == shared_right and left_row == out_row
                                and right_column == out_column
                            ) else field.zero
                            if total != expected:
                                raise TensorMathError("TENSOR_IDENTITY_INVALID", {
                                    "output": [out_row, out_column],
                                    "input": [left_row, shared_left, shared_right, right_column],
                                })
                            checked += 1
                            work.charge()
    return {
        "state": "VERIFIED",
        "identity_coefficients_checked": checked,
        "shape": {"a": a, "b": b, "c": c}, "rank": rank,
        "field_degree": field.degree, "field_characteristic": 0,
        "witness_sha256": witness.witness_sha256,
        "finite_rank_bound": _rank_bound(witness, epsilon),
        "global_9_4_claim": "NOT_ESTABLISHED_BY_FINITE_WITNESS",
    }


def verify_bilinear_tensor_witness(witness: dict, *, budget: TensorMathBudget | None = None,
                                  is_cancelled: Callable[[], bool] | None = None,
                                  epsilon=None) -> dict:
    budget = budget or TensorMathBudget()
    _validate_budget(budget, is_cancelled)
    work = _TensorWork(budget, is_cancelled)
    try:
        parsed = _read_witness(witness, work)
        result = _verify_witness(parsed, work, epsilon=epsilon)
        return {"algorithm": "107-exact-bilinear-witness-check@1", **result,
                "resource": {"steps": work.steps, "field_multiplications": work.field_multiplications}}
    except _TensorStop as stop:
        return _incomplete_witness(stop.code, work, budget)


def _validate_budget(budget: TensorMathBudget, is_cancelled) -> None:
    if (type(budget) is not TensorMathBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_matrix_cells,
            )) or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_matrix_cells < 1):
        raise TensorMathError("TENSOR_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise TensorMathError("TENSOR_CANCEL_CHECK_INVALID")


def _incomplete_witness(reason: str, work: _TensorWork, budget: TensorMathBudget,
                        *, witness_sha256: str | None = None) -> dict:
    return {
        "algorithm": "107-exact-tensor-operation@1", "state": "PARTIAL", "reason": reason,
        "result": None,
        "witness_sha256": witness_sha256,
        "checkpoint": {"witness_sha256": witness_sha256, "resume_mode": "restart_exact_input",
                       "steps_consumed": work.steps},
        "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                     "integer_bits_limit": budget.max_integer_bits,
                     "max_matrix_cells": budget.max_matrix_cells,
                     "field_multiplications": work.field_multiplications,
                     "bilinear_products": work.bilinear_products},
    }


def _matrix(raw, name: str, algebra: _FiniteCoefficientAlgebra, work: _TensorWork,
            budget: TensorMathBudget):
    if not isinstance(raw, list) or not raw or any(not isinstance(row, list) or not row for row in raw):
        raise TensorMathError("MATRIX_INPUT_INVALID", {"field": name})
    columns = len(raw[0])
    if any(len(row) != columns for row in raw):
        raise TensorMathError("MATRIX_INPUT_INVALID", {"field": name})
    if len(raw) * columns > budget.max_matrix_cells:
        raise _TensorStop("MATRIX_CELL_BUDGET_EXCEEDED")
    return tuple(tuple(algebra.parse_element(value, f"{name}[{i}][{j}]")
                       for j, value in enumerate(row)) for i, row in enumerate(raw))


def _zeros(rows: int, columns: int, algebra: _FiniteCoefficientAlgebra):
    return [[algebra.zero for _ in range(columns)] for _ in range(rows)]


def _linear_matrix(coefficients, blocks, algebra: _FiniteCoefficientAlgebra):
    rows, columns = len(blocks[0][0]), len(blocks[0][0][0])
    result = _zeros(rows, columns, algebra)
    for block_row in range(len(blocks)):
        for block_column in range(len(blocks[block_row])):
            coefficient = coefficients[block_row][block_column]
            if coefficient == algebra.zero:
                continue
            block = blocks[block_row][block_column]
            for row in range(rows):
                for column in range(columns):
                    result[row][column] = algebra.add(
                        result[row][column], algebra.multiply(coefficient, block[row][column]),
                    )
    return result


def _block_matrix(matrix, block_rows: int, block_columns: int):
    rows, columns = len(matrix), len(matrix[0])
    if rows % block_rows or columns % block_columns:
        raise TensorMathError("TENSOR_PADDING_NOT_DIVISIBLE")
    height, width = rows // block_rows, columns // block_columns
    return [[[
        [matrix[block_i * height + row][block_j * width + column] for column in range(width)]
        for row in range(height)
    ] for block_j in range(block_columns)] for block_i in range(block_rows)]


def _tensor_multiply(left, right, witness: BilinearTensorWitness, work: _TensorWork,
                     budget: TensorMathBudget, depth: int):
    a, b, c, rank, algebra = witness.a, witness.b, witness.c, witness.rank, witness.algebra
    expected_left_rows, expected_shared = a ** depth, b ** depth
    expected_right_rows, expected_right_columns = b ** depth, c ** depth
    if len(left) != expected_left_rows or any(len(row) != expected_shared for row in left):
        raise TensorMathError("TENSOR_LEFT_BLOCK_SHAPE_INVALID")
    if len(right) != expected_right_rows or any(len(row) != expected_right_columns for row in right):
        raise TensorMathError("TENSOR_RIGHT_BLOCK_SHAPE_INVALID")
    if depth == 1:
        output = _zeros(a, c, algebra)
        for operation in range(rank):
            left_value = algebra.zero
            for row in range(a):
                for shared in range(b):
                    left_value = algebra.add(
                        left_value,
                        algebra.multiply(witness.alpha[operation][row][shared], left[row][shared]),
                    )
            right_value = algebra.zero
            for shared in range(b):
                for column in range(c):
                    right_value = algebra.add(
                        right_value,
                        algebra.multiply(witness.beta[operation][shared][column], right[shared][column]),
                    )
            product = algebra.multiply(left_value, right_value)
            work.bilinear_products += 1
            for row in range(a):
                for column in range(c):
                    coefficient = witness.gamma[row][column][operation]
                    output[row][column] = algebra.add(
                        output[row][column], algebra.multiply(coefficient, product),
                    )
        return output

    left_blocks = _block_matrix(left, a, b)
    right_blocks = _block_matrix(right, b, c)
    block_rows = len(left_blocks[0][0])
    block_shared = len(left_blocks[0][0][0])
    block_columns = len(right_blocks[0][0][0])
    if block_rows * block_shared > budget.max_matrix_cells or block_shared * block_columns > budget.max_matrix_cells:
        raise _TensorStop("MATRIX_CELL_BUDGET_EXCEEDED")
    output_blocks = [[_zeros(block_rows, block_columns, algebra) for _ in range(c)] for _ in range(a)]
    for operation in range(rank):
        left_combination = _linear_matrix(witness.alpha[operation], left_blocks, algebra)
        right_combination = _linear_matrix(witness.beta[operation], right_blocks, algebra)
        product = _tensor_multiply(
            left_combination, right_combination, witness, work, budget, depth - 1,
        )
        for row in range(a):
            for column in range(c):
                coefficient = witness.gamma[row][column][operation]
                if coefficient == algebra.zero:
                    continue
                output = output_blocks[row][column]
                for block_row in range(block_rows):
                    for block_column in range(block_columns):
                        output[block_row][block_column] = algebra.add(
                            output[block_row][block_column],
                            algebra.multiply(coefficient, product[block_row][block_column]),
                        )
    result = _zeros(a * block_rows, c * block_columns, algebra)
    for block_row in range(a):
        for block_column in range(c):
            for row in range(block_rows):
                for column in range(block_columns):
                    result[block_row * block_rows + row][block_column * block_columns + column] = output_blocks[block_row][block_column][row][column]
    return result


def _ceil_power(value: int, base: int) -> tuple[int, int]:
    if base < 2:
        if value <= 1:
            return 1, 0
        raise TensorMathError("TENSOR_BASE_DIMENSION_UNSUPPORTED")
    power, exponent = 1, 0
    while power < value:
        power *= base
        exponent += 1
    return power, exponent


def execute_bilinear_tensor_product(
    matrix_a, matrix_b, witness: dict, *, budget: TensorMathBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None, epsilon=None,
) -> dict:
    budget = budget or TensorMathBudget()
    _validate_budget(budget, is_cancelled)
    work = _TensorWork(budget, is_cancelled)
    parsed = None
    try:
        parsed = _read_witness(witness, work)
        verification = _verify_witness(parsed, work, epsilon=epsilon)
        algebra = parsed.algebra
        left, right = _matrix(matrix_a, "matrix_a", algebra, work, budget), _matrix(matrix_b, "matrix_b", algebra, work, budget)
        rows, shared, right_rows, columns = len(left), len(left[0]), len(right), len(right[0])
        if shared != right_rows:
            raise TensorMathError("MATRIX_DIMENSIONS_INCOMPATIBLE", {"left_columns": shared, "right_rows": right_rows})
        a_size, a_depth = _ceil_power(rows, parsed.a)
        b_size, b_depth = _ceil_power(shared, parsed.b)
        c_size, c_depth = _ceil_power(columns, parsed.c)
        depth = max(a_depth, b_depth, c_depth, 1)
        target_a, target_b, target_c = parsed.a ** depth, parsed.b ** depth, parsed.c ** depth
        cells = target_a * target_b + target_b * target_c + target_a * target_c
        if cells > budget.max_matrix_cells:
            raise _TensorStop("MATRIX_CELL_BUDGET_EXCEEDED")
        padded_a = _zeros(target_a, target_b, algebra)
        padded_b = _zeros(target_b, target_c, algebra)
        for i in range(rows):
            for j in range(shared):
                padded_a[i][j] = left[i][j]
        for i in range(shared):
            for j in range(columns):
                padded_b[i][j] = right[i][j]
        result = _tensor_multiply(padded_a, padded_b, parsed, work, budget, depth)
        cropped = [row[:columns] for row in result[:rows]]
        return {
            "algorithm": "107-recursive-finite-bilinear-tensor@1",
            "state": "READY", "result": [[algebra.serialize_element(value) for value in row] for row in cropped],
            "implementation_path": "VERIFIED_FINITE_BILINEAR_TENSOR",
            "implementation_path": "VERIFIED_FINITE_BILINEAR_TENSOR",
            "result_shape": [rows, columns], "input_shapes": [[rows, shared], [right_rows, columns]],
            "witness": verification, "recursion_depth": depth,
            "padded_shapes": [[target_a, target_b], [target_b, target_c], [target_a, target_c]],
            "field_degree": algebra.degree,
            "field_characteristic": 0,
            "global_9_4_claim": "NOT_ESTABLISHED_BY_FINITE_WITNESS",
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "field_multiplications": work.field_multiplications,
                         "bilinear_products": work.bilinear_products,
                         "padded_matrix_cells": cells},
        }
    except _TensorStop as stop:
        return _incomplete_witness(stop.code, work, budget,
                                   witness_sha256=parsed.witness_sha256 if parsed else None)


def exact_standard_matrix_product(matrix_a, matrix_b, *, budget: TensorMathBudget | None = None,
                                  is_cancelled: Callable[[], bool] | None = None) -> dict:
    """Exact rational schoolbook baseline with an explicit bounded outcome."""
    budget = budget or TensorMathBudget()
    _validate_budget(budget, is_cancelled)
    work = _TensorWork(budget, is_cancelled)
    def read(values, name):
        if not isinstance(values, list) or not values or any(not isinstance(row, list) or not row for row in values):
            raise TensorMathError("MATRIX_INPUT_INVALID", {"field": name})
        width = len(values[0])
        if any(len(row) != width for row in values):
            raise TensorMathError("MATRIX_INPUT_INVALID", {"field": name})
        if len(values) * width > budget.max_matrix_cells:
            raise _TensorStop("MATRIX_CELL_BUDGET_EXCEEDED")
        return [[_fraction(item, f"{name}[{i}][{j}]") for j, item in enumerate(row)]
                for i, row in enumerate(values)]
    try:
        left, right = read(matrix_a, "matrix_a"), read(matrix_b, "matrix_b")
        if len(left[0]) != len(right):
            raise TensorMathError("MATRIX_DIMENSIONS_INCOMPATIBLE")
        rows, inner, columns = len(left), len(right), len(right[0])
        if rows * columns > budget.max_matrix_cells:
            raise _TensorStop("MATRIX_CELL_BUDGET_EXCEEDED")
        result = []
        for i in range(rows):
            result_row = []
            for j in range(columns):
                value = Fraction()
                for k in range(inner):
                    value += left[i][k] * right[k][j]
                    work.charge(value)
                result_row.append(value)
            result.append(result_row)
        return {
            "algorithm": "107-exact-rational-schoolbook-baseline@1",
            "state": "READY",
            "implementation_path": "EXACT_STANDARD_BASELINE",
            "matrix": [[_json_fraction(value) for value in row] for row in result],
            "result_shape": [rows, columns], "input_shapes": [[rows, inner], [inner, columns]],
            "tensor_witness_state": "NOT_USED",
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits,
                         "max_matrix_cells": budget.max_matrix_cells},
        }
    except _TensorStop as stop:
        return {
            "algorithm": "107-exact-rational-schoolbook-baseline@1",
            "state": "PARTIAL", "reason": stop.code,
            "matrix": None,
            "checkpoint": {"resume_mode": "restart_exact_inputs", "steps_consumed": work.steps},
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits,
                         "max_matrix_cells": budget.max_matrix_cells},
        }


def _rational_height(value: Fraction) -> int:
    return max(abs(value.numerator), value.denominator)


def _rational_values_at_height(height: int) -> tuple[Fraction, ...]:
    values = set()
    for denominator in range(1, height + 1):
        for numerator in range(-height, height + 1):
            if math.gcd(abs(numerator), denominator) == 1:
                values.add(Fraction(numerator, denominator))
    return tuple(sorted(values, key=lambda value: (value.numerator, value.denominator)))


def _tensor_from_flat_coefficients(u: int, rank: int, degree: int,
                                   coefficients: tuple[Fraction, ...]) -> dict:
    offset = 0

    def element():
        nonlocal offset
        result = [_json_fraction(value) for value in coefficients[offset:offset + degree]]
        offset += degree
        return result

    modulus = [_json_fraction(value) for value in coefficients[:degree]] + [{"numerator": 1, "denominator": 1}]
    offset = degree
    alpha = [[[element() for _ in range(u)] for _ in range(u)] for _ in range(rank)]
    beta = [[[element() for _ in range(u)] for _ in range(u)] for _ in range(rank)]
    gamma = [[[element() for _ in range(rank)] for _ in range(u)] for _ in range(u)]
    return {"a": u, "b": u, "c": u, "rank": rank,
            "modulus": modulus, "alpha": alpha, "beta": beta, "gamma": gamma}


def search_square_9_4_witness(epsilon, *, budget: TensorMathBudget | None = None,
                               is_cancelled: Callable[[], bool] | None = None,
                               checkpoint: dict | None = None) -> dict:
    """Enumerate the fixed finite coefficient-witness search by increasing height.

    A bounded run reports its exact next height/shape/coefficient-tuple index;
    exhaustion is never presented as a proof that a witness does not exist.
    """
    budget = budget or TensorMathBudget()
    _validate_budget(budget, is_cancelled)
    eps = _fraction(epsilon, "epsilon")
    if eps <= 0:
        raise TensorMathError("TENSOR_EPSILON_INVALID")
    delta = min(eps / 2, Fraction(1, 4))
    target = Fraction(9, 4) + delta
    exponent_a, exponent_b = target.numerator, target.denominator
    epsilon_sha256 = hashlib.sha256(
        f"{eps.numerator}/{eps.denominator}".encode("ascii"),
    ).hexdigest()
    resume = None
    if checkpoint is not None:
        required = {"epsilon_sha256", "height", "u", "rank", "field_degree", "candidate_index"}
        if (not isinstance(checkpoint, dict) or set(checkpoint) != required
                or checkpoint.get("epsilon_sha256") != epsilon_sha256
                or any(type(checkpoint.get(field)) is not int or checkpoint[field] < 0
                       for field in ("height", "u", "rank", "field_degree", "candidate_index"))
                or checkpoint["height"] < 2):
            raise TensorMathError("TENSOR_SEARCH_CHECKPOINT_INVALID")
        resume = checkpoint
    work = _TensorWork(budget, is_cancelled)
    height = resume["height"] if resume else 2
    current = {"height": height, "u": 2, "rank": 4, "field_degree": 1, "candidate_index": 0}
    try:
        while True:
            if height > budget.max_integer_bits:
                raise _TensorStop("RATIONAL_SEARCH_HEIGHT_BUDGET_EXCEEDED")
            if (2 * height + 1) * height > budget.max_matrix_cells:
                raise _TensorStop("RATIONAL_COEFFICIENT_POOL_TOO_LARGE")
            values = _rational_values_at_height(height)
            if len(values) > budget.max_matrix_cells:
                raise _TensorStop("RATIONAL_COEFFICIENT_POOL_TOO_LARGE")
            work.charge(height, len(values), exponent_a, exponent_b)
            skip_checkpoint = resume is not None and height == resume["height"]
            for u in range(2, height + 1):
                for rank in range(u * u, min(u ** 3, height) + 1):
                    if (rank.bit_length() * exponent_b > budget.max_integer_bits
                            or u.bit_length() * exponent_a > budget.max_integer_bits):
                        raise _TensorStop("INTEGER_BUDGET_EXCEEDED")
                    work.charge(rank.bit_length() * exponent_b, u.bit_length() * exponent_a)
                    if rank ** exponent_b >= u ** exponent_a:
                        continue
                    for degree in range(1, height + 1):
                        coefficient_count = degree * (1 + 3 * rank * u * u)
                        if coefficient_count > budget.max_matrix_cells:
                            current = {"height": height, "u": u, "rank": rank,
                                       "field_degree": degree, "candidate_index": 0}
                            raise _TensorStop("TENSOR_COEFFICIENT_BUDGET_EXCEEDED")
                        start_index = 0
                        if skip_checkpoint:
                            if (u, rank, degree) < (resume["u"], resume["rank"], resume["field_degree"]):
                                continue
                            if (u, rank, degree) == (resume["u"], resume["rank"], resume["field_degree"]):
                                start_index = resume["candidate_index"]
                            skip_checkpoint = False
                        candidates = itertools.product(values, repeat=coefficient_count)
                        if start_index:
                            candidates = itertools.islice(candidates, start_index, None)
                        candidate_index = start_index
                        for flat in candidates:
                            current = {"height": height, "u": u, "rank": rank,
                                       "field_degree": degree, "candidate_index": candidate_index}
                            candidate_index += 1
                            work.charge()
                            if max(map(_rational_height, flat), default=0) != height:
                                continue
                            witness = _tensor_from_flat_coefficients(u, rank, degree, tuple(flat))
                            try:
                                parsed = _read_witness(witness, work)
                                verified = _verify_witness(parsed, work, epsilon=eps)
                            except TensorMathError as exc:
                                if exc.code == "TENSOR_IDENTITY_INVALID":
                                    continue
                                raise
                            return {
                                "algorithm": "107-square-witness-height-search@1",
                                "state": "READY", "result": "FINITE_WITNESS_FOUND",
                                "target_exponent": _json_fraction(target),
                                "witness": witness, "verification": verified,
                                "search": {"height": height, "u": u, "rank": rank,
                                           "field_degree": degree, "candidates_examined_at_shape": candidate_index},
                                "global_9_4_claim": "DEPENDS_ON_FIXED_EXISTENCE_THEOREM_AND_SEPARATE_PROOF_REPLAY",
                                "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                                             "integer_bits_limit": budget.max_integer_bits,
                                             "max_matrix_cells": budget.max_matrix_cells},
                            }
            height += 1
            current = {"height": height, "u": 2, "rank": 4,
                       "field_degree": 1, "candidate_index": 0}
    except _TensorStop as stop:
        return {
            "algorithm": "107-square-witness-height-search@1",
            "state": "PARTIAL", "reason": stop.code,
            "target_exponent": _json_fraction(target),
            "witness": None, "global_9_4_claim": "NOT_ESTABLISHED",
            "checkpoint": {"epsilon_sha256": epsilon_sha256, **current,
                           "candidate_index_resume_repeats_current_tuple": True},
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits,
                         "max_matrix_cells": budget.max_matrix_cells},
        }
