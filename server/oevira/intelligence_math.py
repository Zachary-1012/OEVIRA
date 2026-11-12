"""Exact mathematical methods owned by the Intelligence domain.

The routines here are pure: callers provide a frozen problem and a resource
budget, and receive exact values or an explicit incomplete result.  They do
not infer business probabilities, mutate user work, or install a fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from typing import Callable, Generator


class IntelligenceMathError(ValueError):
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


class _MathStop(Exception):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class MeanPayoffBudget:
    max_steps: int = 2_000_000
    max_integer_bits: int = 200_000
    max_frames: int = 100_000


@dataclass(frozen=True)
class _Edge:
    identifier: str
    target: int
    reward: int
    probability: Fraction | None


@dataclass(frozen=True)
class _Vertex:
    identifier: str
    owner: str
    edges: tuple[_Edge, ...]


@dataclass(frozen=True)
class _LabelCall:
    lower: tuple[int, ...]
    width: int
    alpha: tuple[Fraction, ...]
    beta: tuple[Fraction, ...]


@dataclass(frozen=True)
class _ParitySolveCall:
    region: tuple[str, ...]
    max_bound: int
    min_bound: int


@dataclass(frozen=True)
class _MeanPayoffCall:
    region: tuple[str, ...]


class _WorkBudget:
    def __init__(self, budget: MeanPayoffBudget, is_cancelled: Callable[[], bool] | None):
        self.budget = budget
        self.is_cancelled = is_cancelled
        self.steps = 0

    def charge(self, *values: int | Fraction) -> None:
        self.steps += 1
        if self.steps > self.budget.max_steps:
            raise _MathStop("RESOURCE_BUDGET_EXCEEDED")
        if self.is_cancelled is not None:
            try:
                cancelled = self.is_cancelled()
            except Exception as exc:
                raise _MathStop("CANCELLATION_CHECK_UNAVAILABLE") from exc
            if type(cancelled) is not bool:
                raise _MathStop("CANCELLATION_CHECK_INVALID")
            if cancelled:
                raise _MathStop("CANCELLED")
        for value in values:
            integers = (value.numerator, value.denominator) if isinstance(value, Fraction) else (value,)
            if any(abs(part).bit_length() > self.budget.max_integer_bits for part in integers):
                raise _MathStop("INTEGER_BUDGET_EXCEEDED")


def _exact_int(value, field: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": field})
    return value


def _read_game(problem: dict) -> tuple[tuple[_Vertex, ...], tuple[str, ...], int]:
    if not isinstance(problem, dict) or set(problem) != {"vertices", "edges"}:
        raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": "problem"})
    raw_vertices, raw_edges = problem["vertices"], problem["edges"]
    if not isinstance(raw_vertices, list) or not raw_vertices or not isinstance(raw_edges, list):
        raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": "vertices_or_edges"})
    identifiers = []
    owners = {}
    index_by_id = {}
    for index, item in enumerate(raw_vertices):
        if not isinstance(item, dict) or set(item) != {"id", "owner"}:
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"vertices[{index}]"})
        identifier, owner = item["id"], item["owner"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"vertices[{index}].id"})
        if (not isinstance(identifier, str) or not isinstance(owner, str)
                or identifier in owners or owner not in {"max", "min", "chance"}):
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"vertices[{index}].owner"})
        identifiers.append(identifier)
        index_by_id[identifier] = index
        owners[identifier] = owner

    by_source: dict[str, list[_Edge]] = {identifier: [] for identifier in identifiers}
    edge_ids = set()
    denominator_product = 1
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict) or set(item) - {"id", "source", "target", "reward", "probability"}:
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"edges[{index}]"})
        if not {"id", "source", "target", "reward"}.issubset(item):
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"edges[{index}]"})
        identifier, source, target = item["id"], item["source"], item["target"]
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 256
                or identifier in edge_ids or not isinstance(source, str) or not isinstance(target, str)
                or source not in owners or target not in owners):
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"edges[{index}].identity"})
        edge_ids.add(identifier)
        reward = _exact_int(item["reward"], f"edges[{index}].reward")
        probability = None
        if owners[source] == "chance":
            raw_probability = item.get("probability")
            if (not isinstance(raw_probability, dict)
                    or set(raw_probability) != {"numerator", "denominator"}):
                raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"edges[{index}].probability"})
            numerator = _exact_int(raw_probability["numerator"], f"edges[{index}].probability.numerator", minimum=0)
            denominator = _exact_int(raw_probability["denominator"], f"edges[{index}].probability.denominator", minimum=1)
            probability = Fraction(numerator, denominator)
            denominator_product *= denominator
        elif "probability" in item:
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"field": f"edges[{index}].probability"})
        by_source[source].append(_Edge(identifier, index_by_id[target], reward, probability))

    vertices = []
    for identifier in identifiers:
        edges = tuple(sorted(by_source[identifier], key=lambda edge: edge.identifier))
        if not edges:
            raise IntelligenceMathError("MATHEMATICAL_INPUT_INVALID", {"vertex_without_successor": identifier})
        if owners[identifier] == "chance" and sum((edge.probability for edge in edges), Fraction()) != 1:
            raise IntelligenceMathError("CHANCE_PROBABILITIES_MUST_SUM_TO_ONE", {"vertex": identifier})
        vertices.append(_Vertex(identifier, owners[identifier], edges))
    return tuple(vertices), tuple(identifiers), denominator_product


def _width_parts(width: int) -> tuple[int, int]:
    if type(width) is not int or width <= 0:
        raise IntelligenceMathError("MEAN_PAYOFF_WIDTH_INVALID", {"width": width})
    exponent = max(0, width.bit_length() - 8)
    mantissa = width >> exponent
    if not 128 <= mantissa <= 255 or (mantissa << exponent) != width:
        raise IntelligenceMathError("MEAN_PAYOFF_WIDTH_INVALID", {"width": width})
    return mantissa, exponent


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _discounted_floor(vertices: tuple[_Vertex, ...], lower: tuple[int, ...], width: int,
                      scale: int, denominator: int, budget: _WorkBudget) -> tuple[int, ...]:
    result = []
    for vertex in vertices:
        terms = []
        for edge in vertex.edges:
            terms.append(scale * edge.reward + (denominator - 1) * lower[edge.target])
        if vertex.owner == "max":
            value = Fraction(max(terms), denominator)
        elif vertex.owner == "min":
            value = Fraction(min(terms), denominator)
        else:
            value = sum((edge.probability * term for edge, term in zip(vertex.edges, terms)), Fraction()) / denominator
        floored = _floor(Fraction(value))
        result.append(floored)
        budget.charge(value, floored)
    return tuple(result)


def _label_frame(call: _LabelCall, vertices: tuple[_Vertex, ...], scale: int,
                 denominator: int, budget: _WorkBudget) -> Generator[_LabelCall, tuple[str, ...], tuple[str, ...]]:
    n, lower, width = len(vertices), call.lower, call.width
    alpha, beta = call.alpha, call.beta
    mantissa, exponent = _width_parts(width)
    budget.charge(width, *lower, *alpha, *beta)
    if all(alpha[index] * beta[index] > 1 for index in range(n)):
        return tuple("+" if beta[index] > 1 else "-" for index in range(n))
    if exponent < 12:
        upper = tuple(value + width for value in lower)
        current = upper
        iterations = 0
        while True:
            floored = _discounted_floor(vertices, current, width, scale, denominator, budget)
            updated = tuple(min(upper[index], max(lower[index], floored[index])) for index in range(n))
            budget.charge(*updated)
            if updated == current:
                return tuple("+" if current[index] > lower[index] + width // 2 else "-" for index in range(n))
            current = updated
            iterations += 1
            if iterations > n * width + 1:
                raise IntelligenceMathError("MEAN_PAYOFF_BASE_CASE_DID_NOT_TERMINATE")

    centre = tuple(value + width // 2 for value in lower)
    margin, child_width, step = width // 8, width // 128, width // 4096
    if child_width % 2 or width % 4096:
        raise IntelligenceMathError("MEAN_PAYOFF_WIDTH_ALIGNMENT_INVALID", {"width": width})

    pivot = centre
    down_calls = 0
    while True:
        labels = yield _LabelCall(
            tuple(value - child_width // 2 for value in pivot), child_width,
            alpha, tuple(value * Fraction(4, 3) for value in beta),
        )
        updated = tuple(
            max(centre[index] - margin, pivot[index] - step) if labels[index] == "-" else pivot[index]
            for index in range(n)
        )
        budget.charge(*updated)
        down_calls += 1
        if down_calls > 1024 * n + 1:
            raise IntelligenceMathError("MEAN_PAYOFF_DOWNWARD_PIVOT_DID_NOT_TERMINATE")
        if updated == pivot:
            break
        pivot = updated
    lower_pivot = pivot
    minus = {index for index in range(n) if lower_pivot[index] < centre[index] - width // 32}

    pivot = lower_pivot
    up_calls = 0
    while True:
        labels = yield _LabelCall(
            tuple(value - child_width // 2 for value in pivot), child_width,
            tuple(value * Fraction(4, 3) for value in alpha), beta,
        )
        updated = tuple(
            min(centre[index] + margin, pivot[index] + step) if labels[index] == "+" else pivot[index]
            for index in range(n)
        )
        budget.charge(*updated)
        up_calls += 1
        if up_calls > 1024 * n + 1:
            raise IntelligenceMathError("MEAN_PAYOFF_UPWARD_PIVOT_DID_NOT_TERMINATE")
        if updated == pivot:
            break
        pivot = updated
    final_pivot = pivot
    plus = {index for index in range(n) if final_pivot[index] > centre[index] + width // 32}

    temporary = yield _LabelCall(
        tuple(value - width // 4 for value in final_pivot), width // 2, alpha, beta,
    )
    next_alpha = tuple(value * (Fraction(4, 5) if temporary[index] == "+" else Fraction(8, 5))
                       for index, value in enumerate(alpha))
    next_beta = tuple(value * (Fraction(8, 5) if temporary[index] == "+" else Fraction(4, 5))
                      for index, value in enumerate(beta))
    labels = list((yield _LabelCall(lower, width, next_alpha, next_beta)))
    for index in minus:
        labels[index] = "-"
    for index in plus - minus:
        labels[index] = "+"
    return tuple(labels)


def _run_label(root: _LabelCall, vertices: tuple[_Vertex, ...], scale: int,
               denominator: int, budget: _WorkBudget) -> tuple[str, ...]:
    stack = [_label_frame(root, vertices, scale, denominator, budget)]
    pending = None
    has_pending = False
    while stack:
        budget.charge()
        try:
            if has_pending:
                request = stack[-1].send(pending)
                pending, has_pending = None, False
            else:
                request = next(stack[-1])
        except StopIteration as finished:
            stack.pop()
            if not stack:
                return finished.value
            pending, has_pending = finished.value, True
            continue
        if not isinstance(request, _LabelCall):
            raise IntelligenceMathError("MEAN_PAYOFF_FRAME_PROTOCOL_INVALID")
        if len(stack) >= budget.budget.max_frames:
            raise _MathStop("FRAME_BUDGET_EXCEEDED")
        stack.append(_label_frame(request, vertices, scale, denominator, budget))
    raise IntelligenceMathError("MEAN_PAYOFF_FRAME_RESULT_MISSING")


def _rational(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _incomplete_mean_payoff(input_digest: str, budget: MeanPayoffBudget,
                            work: _WorkBudget, reason: str) -> dict:
    return {
        "algorithm": "104-stochastic-mean-payoff-recursive-label@1",
        "state": "PARTIAL",
        "reason": reason,
        "winner_vertices": None,
        "mean_payoff_intervals": None,
        "input_sha256": input_digest,
        "checkpoint": {
            "algorithm": "104-stochastic-mean-payoff-recursive-label@1",
            "input_sha256": input_digest,
            "resume_mode": "restart_from_exact_input",
            "completed_subproblem_state": "NOT_SERIALIZED",
            "steps_consumed": work.steps,
        },
        "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                     "integer_bits_limit": budget.max_integer_bits},
    }


def _single_state_mean_payoff(vertex: _Vertex, input_digest: str,
                             work: _WorkBudget, budget: MeanPayoffBudget) -> dict:
    if vertex.owner == "max":
        best_reward = max(edge.reward for edge in vertex.edges)
        selected = next(edge for edge in vertex.edges if edge.reward == best_reward)
        value = Fraction(best_reward)
        max_strategy = {vertex.identifier: selected.identifier}
        min_strategy = {"state": "NO_MIN_CONTROLLED_VERTICES"}
        probability_basis = "NO_CHANCE_VERTICES"
    elif vertex.owner == "min":
        best_reward = min(edge.reward for edge in vertex.edges)
        selected = next(edge for edge in vertex.edges if edge.reward == best_reward)
        value = Fraction(best_reward)
        max_strategy = {"state": "NO_MAX_CONTROLLED_VERTICES"}
        min_strategy = {vertex.identifier: selected.identifier}
        probability_basis = "NO_CHANCE_VERTICES"
    else:
        value = sum((edge.probability * edge.reward for edge in vertex.edges), Fraction())
        max_strategy = {"state": "NO_MAX_CONTROLLED_VERTICES"}
        min_strategy = {"state": "NO_MIN_CONTROLLED_VERTICES"}
        probability_basis = "EXACT_INPUT_RATIONALS_IID_SINGLE_STATE"
    work.charge(value)
    winning = [vertex.identifier] if value >= 0 else []
    exact_value = _rational(value)
    return {
        "algorithm": "104-single-closed-state-exact@1",
        "state": "READY",
        "scope": "single_vertex_all_edges_are_self_loops",
        "winner_vertices": winning,
        "mean_payoff_values": {vertex.identifier: exact_value},
        "mean_payoff_intervals": {
            vertex.identifier: {"lower": exact_value, "upper": exact_value},
        },
        "value_reconstruction": "EXACT_SINGLE_STATE",
        "maximizer_strategy": max_strategy,
        "minimizer_strategy": min_strategy,
        "parity_product": "NOT_IMPLEMENTED",
        "independent_proof_replay": "NOT_RUN",
        "input_sha256": input_digest,
        "algorithm_randomness": "NONE",
        "chance_probabilities": probability_basis,
        "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                     "integer_bits_limit": budget.max_integer_bits, "outer_calls": 0},
        "limitations": [
            "该闭式结果仅适用于单顶点且所有边都是自环的输入。",
            "更一般的图继续使用104递归胜区；完整奇偶乘积与独立证明重放仍未完成。",
        ],
    }


def evaluate_mean_payoff_winning_region(
    problem: dict, *, budget: MeanPayoffBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Calculate the exact-rational stochastic mean-payoff nonnegative region.

    This entry implements the domain's 104 discounted-recursive-label winning
    region. It does not claim exact value reconstruction, positional strategy
    extraction, the parity-product algorithm, or an independent proof replay.
    """
    budget = budget or MeanPayoffBudget()
    if (type(budget) is not MeanPayoffBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_frames,
            ))
            or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_frames < 1):
        raise IntelligenceMathError("MATHEMATICAL_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceMathError("MATHEMATICAL_CANCEL_CHECK_INVALID")
    vertices, identifiers, denominator_product = _read_game(problem)
    canonical = json.dumps(problem, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    input_digest = hashlib.sha256(canonical).hexdigest()
    n = len(vertices)
    work = _WorkBudget(budget, is_cancelled)
    try:
        for vertex in vertices:
            for edge in vertex.edges:
                work.charge(edge.reward)
                if edge.probability is not None:
                    work.charge(edge.probability)
        work.charge(denominator_product)
    except _MathStop as stop:
        return _incomplete_mean_payoff(input_digest, budget, work, stop.code)


    maximum_reward = max(1, max(abs(edge.reward) for vertex in vertices for edge in vertex.edges))
    if n == 1 and all(edge.target == 0 for edge in vertices[0].edges):
        return _single_state_mean_payoff(vertices[0], input_digest, work, budget)
    a0 = math.factorial(n) * (denominator_product * (3 + maximum_reward)) ** n
    height = (maximum_reward + 4) * a0
    denominator = 32 * height * height
    threshold = 256 * (denominator + 1)
    scale = 16 * height * threshold
    offset = scale * maximum_reward + denominator + 1
    d0 = 0
    while 128 << d0 < 2 * offset:
        d0 += 1
    width = 128 << d0
    lower = tuple(-offset for _ in range(n))
    masses = tuple(Fraction(1, n) for _ in range(n))
    try:
        for number in (a0, height, denominator, threshold, scale, offset, width):
            work.charge(number)
        outer_calls = 0
        while width > threshold:
            labels = _run_label(
                _LabelCall(lower, width, masses, masses), vertices, scale, denominator, work,
            )
            mantissa, exponent = _width_parts(width)
            next_width = ((mantissa - 1) << exponent) if mantissa > 128 else (255 << (exponent - 1))
            if next_width <= 0 or next_width >= width:
                raise IntelligenceMathError("MEAN_PAYOFF_OUTER_WIDTH_DID_NOT_DECREASE")
            delta = width - next_width
            lower = tuple(lower[index] + delta if labels[index] == "+" else lower[index]
                          for index in range(n))
            width = next_width
            outer_calls += 1
            work.charge(delta, *lower, width)
            if outer_calls > 128 * (d0 + 1):
                raise IntelligenceMathError("MEAN_PAYOFF_OUTER_LOOP_DID_NOT_TERMINATE")
        winning = [identifiers[index] for index, value in enumerate(lower) if value >= -8 * threshold]
        intervals = {}
        error = Fraction(1, 16 * height)
        for index, identifier in enumerate(identifiers):
            intervals[identifier] = {
                "lower": _rational(Fraction(lower[index], scale) - error),
                "upper": _rational(Fraction(lower[index] + width, scale) + error),
            }
        return {
            "algorithm": "104-stochastic-mean-payoff-recursive-label@1",
            "state": "READY",
            "scope": "nonnegative_mean_payoff_winning_region_only",
            "winner_vertices": winning,
            "mean_payoff_intervals": intervals,
            "value_reconstruction": "NOT_IMPLEMENTED",
            "maximizer_strategy": "NOT_IMPLEMENTED",
            "minimizer_strategy": "NOT_IMPLEMENTED",
            "parity_product": "NOT_IMPLEMENTED",
            "independent_proof_replay": "NOT_RUN",
            "input_sha256": input_digest,
            "algorithm_randomness": "NONE",
            "chance_probabilities": "EXACT_INPUT_RATIONALS",
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits, "outer_calls": outer_calls},
            "limitations": [
                "胜区结果只回答逐路径liminf平均收益非负的数学问题，不自动选择现实业务动作。",
                "精确有理值重建、双方最优策略、奇偶乘积路线和独立证明重放仍未实现。",
                "资源预算影响能否完成，不改变适用条件；未完成结果不会返回部分胜区。",
            ],
        }
    except _MathStop as stop:
        return _incomplete_mean_payoff(input_digest, budget, work, stop.code)


def evaluate_mean_payoff_exact_values(
    problem: dict, *, budget: MeanPayoffBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Reconstruct exact rational vertex values from the 104 win-region oracle.

    The threshold transformation is applied to a private copy of each reward;
    source problem data is never mutated. General positional strategy extraction
    and the parity product are separate obligations.
    """
    budget = budget or MeanPayoffBudget()
    if (type(budget) is not MeanPayoffBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_frames,
            ))
            or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_frames < 1):
        raise IntelligenceMathError("MATHEMATICAL_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceMathError("MATHEMATICAL_CANCEL_CHECK_INVALID")
    vertices, identifiers, denominator_product = _read_game(problem)
    canonical = json.dumps(problem, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    input_digest = hashlib.sha256(canonical).hexdigest()
    n = len(vertices)
    maximum_reward = max(1, max(abs(edge.reward) for vertex in vertices for edge in vertex.edges))
    a0 = math.factorial(n) * (denominator_product * (3 + maximum_reward)) ** n
    work = _WorkBudget(budget, is_cancelled)
    try:
        work.charge(denominator_product, a0, maximum_reward)
        if n == 1 and all(edge.target == 0 for edge in vertices[0].edges):
            exact = _single_state_mean_payoff(vertices[0], input_digest, work, budget)
            values = exact["mean_payoff_values"]
            return {
                **exact,
                "algorithm": "104-exact-rational-values-single-state@1",
                "scope": "all_vertices_exact_mean_payoff_values",
                "maximizer_strategy": exact["maximizer_strategy"],
                "minimizer_strategy": exact["minimizer_strategy"],
                "mean_payoff_values": values,
                "strategy_extraction": "EXACT_SINGLE_STATE_ONLY",
            }
        width_limit = Fraction(1, 2 * a0 * a0)
        values: dict[str, dict] = {}
        intervals: dict[str, dict] = {}
        low_seed, high_seed = Fraction(-maximum_reward), Fraction(maximum_reward)
        for identifier in identifiers:
            low, high = low_seed, high_seed
            while high - low >= width_limit:
                if is_cancelled is not None:
                    try:
                        cancelled = is_cancelled()
                    except Exception:
                        cancelled = None
                    if type(cancelled) is not bool:
                        raise _MathStop("CANCELLATION_CHECK_UNAVAILABLE")
                    if cancelled:
                        raise _MathStop("CANCELLED")
                remaining_steps = budget.max_steps - work.steps
                if remaining_steps < 1:
                    raise _MathStop("RESOURCE_BUDGET_EXCEEDED")
                threshold = (low + high) / 2
                numerator, denominator = threshold.numerator, threshold.denominator
                transformed_edges = []
                for edge in problem["edges"]:
                    transformed = dict(edge)
                    transformed["reward"] = denominator * edge["reward"] - numerator
                    transformed_edges.append(transformed)
                transformed_problem = {"vertices": problem["vertices"], "edges": transformed_edges}
                result = evaluate_mean_payoff_winning_region(
                    transformed_problem,
                    budget=MeanPayoffBudget(remaining_steps, budget.max_integer_bits, budget.max_frames),
                    is_cancelled=is_cancelled,
                )
                spent = result.get("resource", {}).get("steps", remaining_steps + 1)
                work.steps += spent
                if result.get("state") != "READY":
                    raise _MathStop(result.get("reason", "MEAN_PAYOFF_THRESHOLD_INCOMPLETE"))
                if identifier in result["winner_vertices"]:
                    low = threshold
                else:
                    high = threshold
            midpoint = (low + high) / 2
            candidate = midpoint.limit_denominator(a0)
            if candidate < low or candidate > high or candidate.denominator > a0:
                raise IntelligenceMathError(
                    "MEAN_PAYOFF_RATIONAL_RECONSTRUCTION_FAILED",
                    {"vertex": identifier, "lower": _rational(low), "upper": _rational(high)},
                )
            values[identifier] = _rational(candidate)
            intervals[identifier] = {"lower": _rational(low), "upper": _rational(high)}
        winners = [identifier for identifier in identifiers
                   if Fraction(values[identifier]["numerator"], values[identifier]["denominator"]) >= 0]
        return {
            "algorithm": "104-exact-rational-values-threshold-reconstruction@1",
            "state": "READY",
            "scope": "all_vertices_exact_mean_payoff_values",
            "winner_vertices": winners,
            "mean_payoff_values": values,
            "mean_payoff_intervals": intervals,
            "denominator_bound": a0,
            "value_reconstruction": "UNIQUE_RATIONAL_WITHIN_STRICT_BOUND",
            "maximizer_strategy": "NOT_IMPLEMENTED",
            "minimizer_strategy": "NOT_IMPLEMENTED",
            "parity_product": "NOT_IMPLEMENTED",
            "independent_proof_replay": "NOT_RUN",
            "input_sha256": input_digest,
            "algorithm_randomness": "NONE",
            "chance_probabilities": "EXACT_INPUT_RATIONALS",
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits,
                         "vertices_reconstructed": len(values)},
        }
    except _MathStop as stop:
        return {
            "algorithm": "104-exact-rational-values-threshold-reconstruction@1",
            "state": "PARTIAL", "reason": stop.code,
            "winner_vertices": None,
            "mean_payoff_values": values if "values" in locals() else {},
            "input_sha256": input_digest,
            "checkpoint": {
                "algorithm": "104-exact-rational-values-threshold-reconstruction@1",
                "input_sha256": input_digest,
                "completed_vertices": sorted(values) if "values" in locals() else [],
                "resume_mode": "restart_current_vertex_from_exact_problem",
                "steps_consumed": work.steps,
            },
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits},
        }


def _solve_linear_exact(matrix: list[list[Fraction]], right: list[Fraction],
                        work: _WorkBudget) -> list[Fraction]:
    size = len(right)
    if len(matrix) != size or any(len(row) != size for row in matrix):
        raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_SYSTEM_INVALID")
    rows = [list(row) + [right[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = next((row for row in range(column, size) if rows[row][column] != 0), None)
        if pivot is None:
            raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_SYSTEM_SINGULAR")
        if pivot != column:
            rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        work.charge(*rows[column])
        for row in range(size):
            if row == column:
                continue
            multiplier = rows[row][column]
            if multiplier == 0:
                continue
            rows[row] = [value - multiplier * pivot_value
                         for value, pivot_value in zip(rows[row], rows[column])]
            work.charge(*rows[row])
    return [rows[index][-1] for index in range(size)]


def _strong_components(adjacency: list[list[int]]) -> tuple[list[list[int]], list[int]]:
    count = len(adjacency)
    visited = set()
    order = []
    for start in range(count):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, 0)]
        while stack:
            vertex, offset = stack[-1]
            if offset < len(adjacency[vertex]):
                target = adjacency[vertex][offset]
                stack[-1] = (vertex, offset + 1)
                if target not in visited:
                    visited.add(target)
                    stack.append((target, 0))
            else:
                order.append(vertex)
                stack.pop()
    reverse = [[] for _ in range(count)]
    for source, targets in enumerate(adjacency):
        for target in targets:
            reverse[target].append(source)
    component_index = [-1] * count
    components = []
    for start in reversed(order):
        if component_index[start] >= 0:
            continue
        index = len(components)
        component, stack = [], [start]
        component_index[start] = index
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for target in reverse[vertex]:
                if component_index[target] < 0:
                    component_index[target] = index
                    stack.append(target)
        components.append(sorted(component))
    return components, component_index


def _fixed_strategy_markov_values(problem: dict, maximizer_strategy: dict[str, str],
                                  minimizer_strategy: dict[str, str], work: _WorkBudget) -> dict[str, Fraction]:
    vertices, identifiers, _denominator_product = _read_game(problem)
    index_by_id = {identifier: index for index, identifier in enumerate(identifiers)}
    transition: list[dict[int, Fraction]] = []
    rewards: list[Fraction] = []
    adjacency: list[list[int]] = []
    for vertex in vertices:
        if vertex.owner == "max":
            selected = maximizer_strategy.get(vertex.identifier)
            edges = [edge for edge in vertex.edges if edge.identifier == selected]
            if len(edges) != 1:
                raise IntelligenceMathError("MEAN_PAYOFF_MAX_STRATEGY_INVALID", {"vertex": vertex.identifier})
            weighted_edges = [(edges[0], Fraction(1))]
        elif vertex.owner == "min":
            selected = minimizer_strategy.get(vertex.identifier)
            edges = [edge for edge in vertex.edges if edge.identifier == selected]
            if len(edges) != 1:
                raise IntelligenceMathError("MEAN_PAYOFF_MIN_STRATEGY_INVALID", {"vertex": vertex.identifier})
            weighted_edges = [(edges[0], Fraction(1))]
        else:
            weighted_edges = [(edge, edge.probability) for edge in vertex.edges]
        row: dict[int, Fraction] = {}
        expected_reward = Fraction()
        for edge, probability in weighted_edges:
            if probability == 0:
                continue
            row[edge.target] = row.get(edge.target, Fraction()) + probability
            expected_reward += probability * edge.reward
            work.charge(probability, expected_reward)
        if sum(row.values(), Fraction()) != 1:
            raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_TRANSITION_INVALID", {"vertex": vertex.identifier})
        transition.append(row)
        rewards.append(expected_reward)
        adjacency.append(sorted(row))
    components, component_index = _strong_components(adjacency)
    closed = [component for index, component in enumerate(components)
              if all(component_index[target] == index
                     for vertex in component for target in adjacency[vertex])]
    if not closed:
        raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_CLOSED_CLASS_MISSING")
    component_gains = []
    for component in closed:
        size = len(component)
        local = {vertex: index for index, vertex in enumerate(component)}
        matrix = []
        for target_offset, target in enumerate(component[:-1]):
            row = []
            for source_offset, source in enumerate(component):
                row.append(transition[source].get(target, Fraction())
                           - (1 if source_offset == target_offset else 0))
            matrix.append(row)
        matrix.append([Fraction(1) for _ in component])
        stationary = _solve_linear_exact(matrix, [*([Fraction()] * (size - 1)), Fraction(1)], work)
        if any(value < 0 for value in stationary) or sum(stationary, Fraction()) != 1:
            raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_STATIONARY_INVALID")
        component_gains.append(sum(
            stationary[local[vertex]] * rewards[vertex] for vertex in component
        ))
    transient = [vertex for vertex in range(len(vertices))
                 if all(vertex not in component for component in closed)]
    absorption: dict[tuple[int, int], Fraction] = {}
    for class_index, component in enumerate(closed):
        for vertex in component:
            absorption[(vertex, class_index)] = Fraction(1)
        if not transient:
            continue
        matrix = []
        right = []
        transient_index = {vertex: index for index, vertex in enumerate(transient)}
        component_set = set(component)
        for source_offset, source in enumerate(transient):
            row = []
            for target_offset, target in enumerate(transient):
                row.append((1 if source_offset == target_offset else 0)
                           - transition[source].get(target, Fraction()))
            matrix.append(row)
            right.append(sum((probability for target, probability in transition[source].items()
                              if target in component_set), Fraction()))
        probabilities = _solve_linear_exact(matrix, right, work)
        for vertex, probability in zip(transient, probabilities):
            if probability < 0 or probability > 1:
                raise IntelligenceMathError("MEAN_PAYOFF_MARKOV_ABSORPTION_INVALID")
            absorption[(vertex, class_index)] = probability
    values = {}
    for vertex, identifier in enumerate(identifiers):
        values[identifier] = sum((absorption.get((vertex, class_index), Fraction()) * gain
                                  for class_index, gain in enumerate(component_gains)), Fraction())
    return values


def _single_choice_problem(problem: dict, vertex_id: str, selected_edge_id: str) -> dict:
    vertices, _identifiers, _denominator_product = _read_game(problem)
    owner_by_id = {vertex.identifier: vertex.owner for vertex in vertices}
    edges = [edge for edge in problem["edges"]
             if edge["source"] != vertex_id or owner_by_id[vertex_id] not in {"max", "min"}
             or edge["id"] == selected_edge_id]
    return {"vertices": problem["vertices"], "edges": edges}


def evaluate_mean_payoff_optimal_strategies(
    problem: dict, *, budget: MeanPayoffBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Find exact positional MAX/MIN strategies preserving every vertex value.

    Each player's strategy is fixed independently from the original game. The
    combined strategy is then checked by exact finite Markov-chain analysis.
    """
    budget = budget or MeanPayoffBudget()
    if (type(budget) is not MeanPayoffBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_frames,
            ))
            or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_frames < 1):
        raise IntelligenceMathError("MATHEMATICAL_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceMathError("MATHEMATICAL_CANCEL_CHECK_INVALID")
    values_result = evaluate_mean_payoff_exact_values(problem, budget=budget, is_cancelled=is_cancelled)
    if values_result.get("state") != "READY":
        return {**values_result, "strategy_state": "NOT_READY_VALUES_INCOMPLETE"}
    exact_values = values_result["mean_payoff_values"]
    vertices, identifiers, _denominator_product = _read_game(problem)
    original_digest = values_result["input_sha256"]
    consumed = values_result.get("resource", {}).get("steps", 0)
    maximizer_strategy: dict[str, str] = {}
    minimizer_strategy: dict[str, str] = {}

    def solve_candidate(candidate_problem: dict) -> dict:
        nonlocal consumed
        remaining = budget.max_steps - consumed
        if remaining < 1:
            return {"state": "PARTIAL", "reason": "RESOURCE_BUDGET_EXCEEDED", "resource": {"steps": 0}}
        result = evaluate_mean_payoff_exact_values(
            candidate_problem,
            budget=MeanPayoffBudget(remaining, budget.max_integer_bits, budget.max_frames),
            is_cancelled=is_cancelled,
        )
        consumed += result.get("resource", {}).get("steps", remaining + 1)
        return result

    try:
        for owner, strategy in (("max", maximizer_strategy), ("min", minimizer_strategy)):
            current = problem if owner == "max" else problem
            controlled = [vertex for vertex in vertices if vertex.owner == owner]
            for vertex in controlled:
                selected = None
                for edge in sorted(vertex.edges, key=lambda item: item.identifier):
                    candidate = _single_choice_problem(current, vertex.identifier, edge.identifier)
                    result = solve_candidate(candidate)
                    if result.get("state") != "READY":
                        return {
                            "algorithm": "104-exact-rational-positional-strategies@1",
                            "state": "PARTIAL", "reason": result.get("reason", "STRATEGY_VALUE_RECHECK_INCOMPLETE"),
                            "winner_vertices": None, "mean_payoff_values": exact_values,
                            "candidate_maximizer_strategy": maximizer_strategy,
                            "candidate_minimizer_strategy": minimizer_strategy,
                            "input_sha256": original_digest,
                            "checkpoint": {
                                "algorithm": "104-exact-rational-positional-strategies@1",
                                "input_sha256": original_digest,
                                "owner": owner, "vertex": vertex.identifier,
                                "next_edge": edge.identifier, "resume_mode": "restart_candidate_value_check",
                                "steps_consumed": consumed,
                            },
                            "resource": {"steps": consumed, "max_steps": budget.max_steps},
                        }
                    if result["mean_payoff_values"] == exact_values:
                        strategy[vertex.identifier] = edge.identifier
                        current = candidate
                        selected = edge.identifier
                        break
                if selected is None:
                    return {
                        "algorithm": "104-exact-rational-positional-strategies@1",
                        "state": "PARTIAL", "reason": "MEAN_PAYOFF_POSITIONAL_STRATEGY_NOT_FOUND",
                        "winner_vertices": None, "mean_payoff_values": exact_values,
                        "candidate_maximizer_strategy": maximizer_strategy,
                        "candidate_minimizer_strategy": minimizer_strategy,
                        "input_sha256": original_digest,
                        "resource": {"steps": consumed, "max_steps": budget.max_steps},
                    }
        remaining = budget.max_steps - consumed
        if remaining < 1:
            return {
                "algorithm": "104-exact-rational-positional-strategies@1",
                "state": "PARTIAL", "reason": "RESOURCE_BUDGET_EXCEEDED",
                "winner_vertices": None, "mean_payoff_values": exact_values,
                "candidate_maximizer_strategy": maximizer_strategy,
                "candidate_minimizer_strategy": minimizer_strategy,
                "input_sha256": original_digest,
                "checkpoint": {"algorithm": "104-exact-rational-positional-strategies@1",
                               "input_sha256": original_digest, "resume_mode": "restart_markov_recheck",
                               "steps_consumed": consumed},
                "resource": {"steps": consumed, "max_steps": budget.max_steps},
            }
        markov_budget = MeanPayoffBudget(remaining, budget.max_integer_bits, budget.max_frames)
        markov_work = _WorkBudget(markov_budget, is_cancelled)
        evaluated = _fixed_strategy_markov_values(
            problem, maximizer_strategy, minimizer_strategy, markov_work,
        )
        consumed += markov_work.steps
    except _MathStop as stop:
        return {
            "algorithm": "104-exact-rational-positional-strategies@1",
            "state": "PARTIAL", "reason": stop.code, "winner_vertices": None,
            "mean_payoff_values": exact_values,
            "candidate_maximizer_strategy": maximizer_strategy,
            "candidate_minimizer_strategy": minimizer_strategy,
            "input_sha256": original_digest,
            "checkpoint": {"algorithm": "104-exact-rational-positional-strategies@1",
                           "input_sha256": original_digest, "resume_mode": "restart_markov_recheck",
                           "steps_consumed": consumed},
            "resource": {"steps": consumed, "max_steps": budget.max_steps},
        }
    if any(_rational(evaluated[identifier]) != exact_values[identifier] for identifier in identifiers):
        return {
            "algorithm": "104-exact-rational-positional-strategies@1",
            "state": "PARTIAL", "reason": "MEAN_PAYOFF_MARKOV_STRATEGY_DISAGREEMENT",
            "winner_vertices": None, "mean_payoff_values": exact_values,
            "candidate_maximizer_strategy": maximizer_strategy,
            "candidate_minimizer_strategy": minimizer_strategy,
            "markov_mean_payoff_values": {identifier: _rational(evaluated[identifier]) for identifier in identifiers},
            "input_sha256": original_digest,
            "resource": {"steps": consumed, "max_steps": budget.max_steps},
        }
    return {
        "algorithm": "104-exact-rational-positional-strategies@1",
        "state": "READY",
        "scope": "all_vertex_values_and_exact_stationary_strategy_check",
        "winner_vertices": values_result["winner_vertices"],
        "mean_payoff_values": exact_values,
        "maximizer_strategy": maximizer_strategy,
        "minimizer_strategy": minimizer_strategy,
        "markov_mean_payoff_values": {identifier: _rational(evaluated[identifier]) for identifier in identifiers},
        "strategy_state": "EXACT_MARKOV_CHECKED",
        "parity_product": "NOT_IMPLEMENTED",
        "independent_proof_replay": "NOT_RUN",
        "input_sha256": original_digest,
        "resource": {"steps": consumed, "max_steps": budget.max_steps,
                     "integer_bits_limit": budget.max_integer_bits},
    }


def evaluate_single_state_mean_payoff_parity(
    problem: dict, *, budget: MeanPayoffBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Solve the exact one-vertex MAX/MIN mean-payoff-and-parity subcase.

    The vertex priority is visited forever on every play, so parity is exactly
    its evenness. Multi-vertex and CHANCE parity games remain unimplemented.
    """
    budget = budget or MeanPayoffBudget()
    if (type(budget) is not MeanPayoffBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_frames,
            ))
            or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_frames < 1):
        raise IntelligenceMathError("MATHEMATICAL_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceMathError("MATHEMATICAL_CANCEL_CHECK_INVALID")
    if (not isinstance(problem, dict) or set(problem) != {"vertices", "edges"}
            or not isinstance(problem["vertices"], list) or not isinstance(problem["edges"], list)):
        raise IntelligenceMathError("PARITY_SINGLE_STATE_INPUT_INVALID")
    canonical = json.dumps(problem, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    input_digest = hashlib.sha256(canonical).hexdigest()
    if len(problem["vertices"]) != 1:
        return {
            "algorithm": "104-single-state-mean-payoff-parity-exact@1",
            "state": "PARTIAL", "reason": "PARITY_GENERAL_ALGORITHM_NOT_IMPLEMENTED",
            "winner_vertices": None, "input_sha256": input_digest,
            "checkpoint": {"algorithm": "104-single-state-mean-payoff-parity-exact@1",
                           "input_sha256": input_digest, "resume_mode": "restart_from_exact_input"},
        }
    if (not isinstance(problem["vertices"][0], dict)
            or set(problem["vertices"][0]) != {"id", "owner", "priority"}):
        raise IntelligenceMathError("PARITY_SINGLE_STATE_INPUT_INVALID")
    raw_vertex = problem["vertices"][0]
    priority = _exact_int(raw_vertex["priority"], "vertices[0].priority", minimum=0)
    owner = raw_vertex.get("owner")
    if owner not in {"max", "min"}:
        return {
            "algorithm": "104-single-state-mean-payoff-parity-exact@1",
            "state": "PARTIAL", "reason": "PARITY_CHANCE_NOT_SUPPORTED",
            "winner_vertices": None, "parity_priority": priority, "input_sha256": input_digest,
        }
    stripped = {
        "vertices": [{"id": raw_vertex["id"], "owner": owner}],
        "edges": problem["edges"],
    }
    vertices, identifiers, denominator_product = _read_game(stripped)
    work = _WorkBudget(budget, is_cancelled)
    if any(edge.target != 0 for edge in vertices[0].edges):
        return {
            "algorithm": "104-single-state-mean-payoff-parity-exact@1",
            "state": "PARTIAL", "reason": "PARITY_GENERAL_ALGORITHM_NOT_IMPLEMENTED",
            "winner_vertices": None, "input_sha256": input_digest,
            "checkpoint": {"algorithm": "104-single-state-mean-payoff-parity-exact@1",
                           "input_sha256": input_digest, "resume_mode": "restart_from_exact_input"},
        }
    try:
        for edge in vertices[0].edges:
            work.charge(edge.reward)
        work.charge(priority, denominator_product)
        result = _single_state_mean_payoff(vertices[0], input_digest, work, budget)
    except _MathStop as stop:
        return {
            "algorithm": "104-single-state-mean-payoff-parity-exact@1",
            "state": "PARTIAL", "reason": stop.code, "winner_vertices": None,
            "input_sha256": input_digest,
            "checkpoint": {"algorithm": "104-single-state-mean-payoff-parity-exact@1",
                           "input_sha256": input_digest, "resume_mode": "restart_from_exact_input",
                           "steps_consumed": work.steps},
        }
    parity_winner = priority % 2 == 0
    if not parity_winner:
        result["winner_vertices"] = []
    result.update({
        "algorithm": "104-single-state-mean-payoff-parity-exact@1",
        "scope": "one_vertex_max_min_all_edges_self_loops",
        "parity_priority": priority,
        "parity_state": "EVEN" if parity_winner else "ODD",
        "parity_product": "EXACT_SINGLE_VERTEX",
        "input_sha256": input_digest,
        "limitations": [
            "精确结果只适用于单个MAX/MIN顶点且所有边均为自环。",
            "多顶点递归parity产品、策略记忆构造和独立证明重放仍未实现。",
        ],
    })
    return result


def _read_parity_game(problem: dict) -> tuple[tuple[_Vertex, ...], dict[str, int]]:
    if (not isinstance(problem, dict) or set(problem) != {"vertices", "edges"}
            or not isinstance(problem["vertices"], list) or not problem["vertices"]
            or not isinstance(problem["edges"], list)):
        raise IntelligenceMathError("PARITY_INPUT_INVALID")
    priorities, owners, rewards = {}, {}, {}
    normalized_vertices = []
    for index, item in enumerate(problem["vertices"]):
        if not isinstance(item, dict) or set(item) != {"id", "owner", "reward", "priority"}:
            raise IntelligenceMathError("PARITY_INPUT_INVALID", {"field": f"vertices[{index}]"})
        identifier, owner = item["id"], item["owner"]
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 256
                or not isinstance(owner, str)
                or identifier in owners or owner not in {"max", "min"}):
            raise IntelligenceMathError("PARITY_INPUT_INVALID", {"field": f"vertices[{index}].identity"})
        rewards[identifier] = _exact_int(item["reward"], f"vertices[{index}].reward")
        priorities[identifier] = _exact_int(item["priority"], f"vertices[{index}].priority", minimum=0)
        owners[identifier] = owner
        normalized_vertices.append({"id": identifier, "owner": owner})
    normalized_edges = []
    for index, item in enumerate(problem["edges"]):
        if (not isinstance(item, dict) or set(item) != {"id", "source", "target"}
                or not isinstance(item["id"], str) or not item["id"]
                or not isinstance(item["source"], str) or not isinstance(item["target"], str)
                or item["source"] not in owners or item["target"] not in owners):
            raise IntelligenceMathError("PARITY_INPUT_INVALID", {"field": f"edges[{index}]"})
        normalized_edges.append({
            "id": item["id"], "source": item["source"], "target": item["target"],
            "reward": rewards[item["source"]],
        })
    vertices, _identifiers, _denominator_product = _read_game(
        {"vertices": normalized_vertices, "edges": normalized_edges},
    )
    return vertices, priorities


def _parity_subgame_edges(vertices: tuple[_Vertex, ...], region: set[str]) -> dict[str, tuple[str, ...]]:
    output = {}
    for vertex in vertices:
        if vertex.identifier not in region:
            continue
        targets = tuple(edge.target for edge in vertex.edges
                        if vertices[edge.target].identifier in region)
        if not targets:
            raise IntelligenceMathError("PARITY_SUBGAME_NOT_TOTAL", {"vertex": vertex.identifier})
        output[vertex.identifier] = tuple(vertices[target].identifier for target in targets)
    return output


def _parity_attractor(region: set[str], target: set[str], player: str,
                      vertices_by_id: dict[str, _Vertex], successors: dict[str, tuple[str, ...]],
                      work: _WorkBudget) -> set[str]:
    attracted = set(target) & region
    while True:
        work.charge()
        additions = set()
        for identifier in sorted(region - attracted):
            next_vertices = successors[identifier]
            if vertices_by_id[identifier].owner == player:
                enters = any(value in attracted for value in next_vertices)
            else:
                enters = all(value in attracted for value in next_vertices)
            if enters:
                additions.add(identifier)
        if not additions:
            return attracted
        attracted.update(additions)
        work.charge(len(attracted))


def _parity_solve_frame(call: _ParitySolveCall, vertices: tuple[_Vertex, ...],
                        priorities: dict[str, int], work: _WorkBudget) -> Generator[object, object, dict[str, set[str]]]:
    region = set(call.region)
    if not region:
        return {"max": set(), "min": set()}
    if call.max_bound == 0:
        return {"max": set(), "min": set(region)}
    if call.min_bound == 0:
        return {"max": set(region), "min": set()}
    vertices_by_id = {vertex.identifier: vertex for vertex in vertices}
    priorities_in_game = {identifier: priorities[identifier] for identifier in region}
    successors = _parity_subgame_edges(vertices, region)
    minimum = min(priorities_in_game.values())
    player = "max" if minimum % 2 == 0 else "min"
    opponent = "min" if player == "max" else "max"
    smaller_max, smaller_min = call.max_bound, call.min_bound
    if opponent == "max":
        smaller_max //= 2
    else:
        smaller_min //= 2

    def phase(start: set[str]) -> Generator[object, object, set[str]]:
        current = set(start)
        while current:
            work.charge()
            if minimum % 2 == 0:
                mean_payoff_winners = set((yield _MeanPayoffCall(tuple(sorted(current)))))
                outside = current - mean_payoff_winners
                if outside:
                    current -= _parity_attractor(
                        current, outside, opponent, vertices_by_id,
                        _parity_subgame_edges(vertices, current), work,
                    )
                    continue
            least_priority = {identifier for identifier in current
                              if priorities_in_game[identifier] == minimum}
            current_successors = _parity_subgame_edges(vertices, current)
            attracted = _parity_attractor(
                current, least_priority, player, vertices_by_id, current_successors, work,
            )
            remainder = current - attracted
            child = yield _ParitySolveCall(tuple(sorted(remainder)), smaller_max, smaller_min)
            opponent_winners = set(child[opponent])
            if not opponent_winners:
                return current
            current -= _parity_attractor(
                current, opponent_winners, opponent, vertices_by_id,
                _parity_subgame_edges(vertices, current), work,
            )
        return set()

    current = yield from phase(region)
    if current:
        least_priority = {identifier for identifier in current
                          if priorities_in_game[identifier] == minimum}
        current_successors = _parity_subgame_edges(vertices, current)
        attracted = _parity_attractor(
            current, least_priority, player, vertices_by_id, current_successors, work,
        )
        remainder = current - attracted
        child = yield _ParitySolveCall(tuple(sorted(remainder)), call.max_bound, call.min_bound)
        opponent_winners = set(child[opponent])
        current -= _parity_attractor(
            current, opponent_winners, opponent, vertices_by_id,
            _parity_subgame_edges(vertices, current), work,
        )
        current = yield from phase(current)
    return {player: current, opponent: region - current}


def _run_parity_solve(root: _ParitySolveCall, vertices: tuple[_Vertex, ...],
                      priorities: dict[str, int], work: _WorkBudget,
                      *, is_cancelled: Callable[[], bool] | None) -> dict[str, set[str]]:
    stack = [_parity_solve_frame(root, vertices, priorities, work)]
    pending = None
    has_pending = False
    while stack:
        work.charge()
        try:
            if has_pending:
                request = stack[-1].send(pending)
                pending, has_pending = None, False
            else:
                request = next(stack[-1])
        except StopIteration as finished:
            stack.pop()
            if not stack:
                return finished.value
            pending, has_pending = finished.value, True
            continue
        if isinstance(request, _ParitySolveCall):
            if len(stack) >= work.budget.max_frames:
                raise _MathStop("FRAME_BUDGET_EXCEEDED")
            stack.append(_parity_solve_frame(request, vertices, priorities, work))
            continue
        if not isinstance(request, _MeanPayoffCall):
            raise IntelligenceMathError("PARITY_FRAME_PROTOCOL_INVALID")
        remaining = work.budget.max_steps - work.steps
        if remaining < 1:
            raise _MathStop("RESOURCE_BUDGET_EXCEEDED")
        subset = set(request.region)
        subproblem = {
            "vertices": [{"id": vertex.identifier, "owner": vertex.owner}
                         for vertex in vertices if vertex.identifier in subset],
            "edges": [{"id": edge.identifier, "source": vertex.identifier,
                       "target": vertices[edge.target].identifier, "reward": edge.reward}
                      for vertex in vertices if vertex.identifier in subset for edge in vertex.edges
                      if vertices[edge.target].identifier in subset],
        }
        mean_payoff = evaluate_mean_payoff_winning_region(
            subproblem,
            budget=MeanPayoffBudget(remaining, work.budget.max_integer_bits, work.budget.max_frames),
            is_cancelled=is_cancelled,
        )
        work.steps += mean_payoff.get("resource", {}).get("steps", remaining + 1)
        if mean_payoff.get("state") != "READY":
            raise _MathStop(mean_payoff.get("reason", "MEAN_PAYOFF_SUBPROBLEM_INCOMPLETE"))
        pending, has_pending = mean_payoff["winner_vertices"], True
    raise IntelligenceMathError("PARITY_FRAME_RESULT_MISSING")


def evaluate_mean_payoff_parity_winning_region(
    problem: dict, *, budget: MeanPayoffBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Solve the domain's general deterministic mean-payoff/parity winning region.

    The recursive solver returns the conjunction winning set. Its policy-memory
    construction and independent general-game proof replay are separate outputs.
    """
    budget = budget or MeanPayoffBudget()
    if (type(budget) is not MeanPayoffBudget
            or any(type(value) is not int for value in (
                budget.max_steps, budget.max_integer_bits, budget.max_frames,
            ))
            or budget.max_steps < 1 or budget.max_integer_bits < 64 or budget.max_frames < 1):
        raise IntelligenceMathError("MATHEMATICAL_BUDGET_INVALID")
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceMathError("MATHEMATICAL_CANCEL_CHECK_INVALID")
    vertices, priorities = _read_parity_game(problem)
    canonical = json.dumps(problem, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    input_digest = hashlib.sha256(canonical).hexdigest()
    work = _WorkBudget(budget, is_cancelled)
    try:
        for vertex in vertices:
            work.charge(priorities[vertex.identifier], *(edge.reward for edge in vertex.edges))
        winners = _run_parity_solve(
            _ParitySolveCall(tuple(vertex.identifier for vertex in vertices), len(vertices), len(vertices)),
            vertices, priorities, work, is_cancelled=is_cancelled,
        )
        return {
            "algorithm": "104-deterministic-mean-payoff-parity-recursive@1",
            "state": "READY",
            "scope": "all_vertex_conjunction_winning_region_only",
            "winner_vertices": sorted(winners["max"]),
            "loser_vertices": sorted(winners["min"]),
            "mean_payoff_strategies": "NOT_IMPLEMENTED",
            "parity_memory_strategy": "NOT_IMPLEMENTED",
            "independent_proof_replay": "NOT_RUN",
            "input_sha256": input_digest,
            "algorithm_randomness": "NONE",
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits, "frames_limit": budget.max_frames},
            "limitations": [
                "胜区严格要求同一条无限路径同时满足最小无限常见优先级为偶数及平均收益非负。",
                "本入口尚未生成带无限记忆与前缀损失界的实际策略，也未完成独立通用证明重放。",
            ],
        }
    except _MathStop as stop:
        return {
            "algorithm": "104-deterministic-mean-payoff-parity-recursive@1",
            "state": "PARTIAL", "reason": stop.code,
            "winner_vertices": None, "loser_vertices": None,
            "input_sha256": input_digest,
            "checkpoint": {
                "algorithm": "104-deterministic-mean-payoff-parity-recursive@1",
                "input_sha256": input_digest,
                "resume_mode": "restart_from_exact_game",
                "steps_consumed": work.steps,
            },
            "resource": {"steps": work.steps, "max_steps": budget.max_steps,
                         "integer_bits_limit": budget.max_integer_bits, "frames_limit": budget.max_frames},
        }
