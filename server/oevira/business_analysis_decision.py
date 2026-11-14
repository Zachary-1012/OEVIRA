"""Constraint-aware option comparison; rankings never authorize an action."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from datetime import date, datetime
import math

import numpy as np

from .business_analysis_economics import BusinessAnalysisCancelled

# Ishizaka and Labib (2009), section 2.5, Table 1, the reported Saaty RI table.
_RI_SOURCE = "https://pure.port.ac.uk/ws/portalfiles/portal/42847/ORI-preprint-AIshizaka.pdf"
_RANDOM_INDICES = {3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must contain text")
    return value.strip()


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _cancel(check):
    if check is not None and check():
        raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")


def _section(identifier, title, body, data):
    return {"id": identifier, "title": title, "body": body, "data": data}


def _weights(criteria, preference):
    supplied = [item.get("weight") for item in criteria]
    if preference is None and all(item is None for item in supplied):
        return None, {"method": "pareto_only", "reason": "No actual preference weights were supplied"}
    if preference is None:
        if any(item is None for item in supplied):
            raise ValueError("Provide all criterion weights or leave all unspecified")
        weights = np.asarray([_number(item, "criterion.weight") for item in supplied])
        if np.any(weights < 0) or not np.any(weights > 0):
            raise ValueError("Weights must be nonnegative with a positive total")
        weights /= weights.max()
        return weights / weights.sum(), {"method": "explicit_user_weights", "supplied_weights": supplied}
    if any(item is not None for item in supplied):
        raise ValueError("Use either explicit weights or pairwise preference judgments")
    if not isinstance(preference, dict) or set(preference) - {"matrix", "random_index", "consistency_threshold"}:
        raise ValueError("Pairwise preferences need a complete matrix and an attributed same-order random index")
    matrix_input = preference.get("matrix")
    size = len(criteria)
    if not isinstance(matrix_input, list) or len(matrix_input) != size or any(not isinstance(row, list) or len(row) != size for row in matrix_input):
        raise ValueError("Pairwise judgments must cover every criterion in the saved order")
    matrix = np.asarray([[_number(value, "pairwise judgment") for value in row] for row in matrix_input])
    if np.any(matrix <= 0) or not np.allclose(np.diag(matrix), 1, rtol=1e-8, atol=1e-8) or not np.allclose(matrix * matrix.T, 1, rtol=1e-8, atol=1e-8):
        raise ValueError("Pairwise judgments must be positive, reciprocal and have unit diagonals")
    eigenvalues, eigenvectors = np.linalg.eig(matrix)
    index = int(np.argmax(eigenvalues.real))
    principal = eigenvalues[index]
    vector = eigenvectors[:, index]
    if abs(principal.imag) > 1e-8 or np.max(np.abs(vector.imag)) > 1e-8:
        raise ValueError("The preference matrix did not produce a real principal priority vector")
    weights = np.abs(vector.real)
    weights /= weights.sum()
    ci = max(0.0, (float(principal.real) - size) / (size - 1)) if size > 1 else 0.0
    diagnostic = {"method": "ahp_principal_eigenvector", "matrix": matrix_input, "criterion_order": [item["id"] for item in criteria], "lambda_max": float(principal.real), "consistency_index": ci,
                  "consistency_ratio": 0.0 if size <= 2 else None, "consistency_reference_status": "exact_reciprocal_small_order" if size <= 2 else "not_available",
                  "judgment_correctness": "not_established"}
    if size > 2:
        declared = preference.get("random_index")
        random_index = _RANDOM_INDICES.get(size)
        if declared is not None:
            if not isinstance(declared, dict) or declared.get("order") != size:
                raise ValueError("A declared random index must identify the exact matrix order")
            value = _number(declared.get("value"), "random_index.value")
            if value <= 0 or (random_index is not None and not math.isclose(value, random_index, rel_tol=0, abs_tol=1e-12)):
                raise ValueError("The declared random index does not match the pinned same-order reference")
            diagnostic["declared_random_index"] = deepcopy(declared)
        if random_index is not None and np.all(matrix >= 1 / 9 - 1e-12) and np.all(matrix <= 9 + 1e-12):
            diagnostic.update(random_index={"order": size, "value": random_index, "method": "saaty-ri-table/ishizaka-labib-2009-table-1", "source_ref": _RI_SOURCE},
                              consistency_ratio=ci / random_index, consistency_reference_status="pinned_same_order_scale_1_to_9")
        elif random_index is not None:
            diagnostic["consistency_reference_status"] = "judgment_scale_outside_reference"
        else:
            diagnostic["consistency_reference_status"] = "order_outside_retained_reference"
    ratio = diagnostic["consistency_ratio"]
    diagnostic.update(reference_threshold=0.1, reference_threshold_exceeded=ratio > 0.1 if ratio is not None else None)
    threshold = preference.get("consistency_threshold")
    if threshold is not None:
        threshold = _number(threshold, "consistency_threshold")
        if threshold < 0:
            raise ValueError("The consistency diagnostic threshold cannot be negative")
        diagnostic.update(consistency_threshold=threshold, exceeds_declared_threshold=ratio > threshold if ratio is not None else None)
    return weights, diagnostic


def _scores(matrix, directions, weights):
    # Scale before taking the norm to avoid overflow on finite large units.
    scale = np.max(np.abs(matrix), axis=0)
    scaled = np.divide(matrix, scale, out=np.zeros_like(matrix), where=scale != 0)
    norm = np.linalg.norm(scaled, axis=0)
    normalized = np.divide(scaled, norm, out=np.zeros_like(matrix), where=norm != 0)
    weighted = normalized * weights
    best = np.where(directions > 0, weighted.max(axis=0), weighted.min(axis=0))
    worst = np.where(directions > 0, weighted.min(axis=0), weighted.max(axis=0))
    positive = np.linalg.norm(weighted - best, axis=1)
    negative = np.linalg.norm(weighted - worst, axis=1)
    total = positive + negative
    scores = np.divide(negative, total, out=np.full(len(matrix), np.nan), where=total > 0)
    return [None if not math.isfinite(value) else float(value) for value in scores]


def _leaders(ids, scores):
    valid = [score for score in scores if score is not None]
    if not valid:
        return []
    maximum = max(valid)
    return [identifier for identifier, score in zip(ids, scores) if score is not None and math.isclose(score, maximum, abs_tol=1e-10)]


def evaluate_decision(operation, inputs, *, is_cancelled=None):
    if operation != "decision_comparison" or not isinstance(inputs, dict):
        raise ValueError("Unsupported decision operation")
    _cancel(is_cancelled)
    if set(inputs) - {"title", "question", "criteria", "constraints", "alternatives", "pairwise_preferences", "candidate_additions", "information_cutoff"}:
        raise ValueError("Unsupported option-comparison input")
    question = _text(inputs.get("question"), "question")
    cutoff = inputs.get("information_cutoff")
    cutoff_precision = None
    if cutoff is not None:
        cutoff = _text(cutoff, "information_cutoff")
        try:
            if len(cutoff) == 10:
                date.fromisoformat(cutoff)
                cutoff_precision = "day"
            else:
                instant = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("information_cutoff timestamp needs an explicit timezone")
                cutoff_precision = "instant"
        except ValueError as error:
            raise ValueError("information_cutoff must be an ISO date or timezone-aware timestamp") from error
    criteria = inputs.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("Use the actual business criteria for this comparison")
    keys = []
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) - {"id", "name", "direction", "unit", "definition", "weight"}:
            raise ValueError("Each criterion needs a stable identity, definition, unit and direction")
        for field in ("id", "name", "unit", "definition"):
            _text(criterion.get(field), f"criterion.{field}")
        if not isinstance(criterion.get("direction"), str) or criterion["direction"] not in {"maximize", "minimize"}:
            raise ValueError("Choose maximize or minimize for every criterion")
        keys.append(criterion["id"])
    if len(keys) != len(set(keys)):
        raise ValueError("Criterion identities must be unique")
    constraints = inputs.get("constraints", [])
    if not isinstance(constraints, list) or any(not isinstance(item, dict) or not item.get("id") or not item.get("description") for item in constraints):
        raise ValueError("Hard constraints need stable identities and their actual conditions")
    constraint_ids = {_text(item["id"], "constraint.id") for item in constraints}
    for constraint in constraints:
        _text(constraint["description"], "constraint.description")
        numeric_fields = {"criterion_id", "operator", "value"}
        if numeric_fields.intersection(constraint):
            if not numeric_fields <= constraint.keys() or constraint["criterion_id"] not in keys or not isinstance(constraint["operator"], str) or constraint["operator"] not in {"<=", ">=", "=="}:
                raise ValueError("A numeric hard constraint needs a saved criterion, comparison operator and exact limit")
            _number(constraint["value"], "constraint.value")
    if len(constraint_ids) != len(constraints):
        raise ValueError("Constraint identities must be unique")
    alternatives, additions = inputs.get("alternatives"), inputs.get("candidate_additions", [])
    if not isinstance(alternatives, list) or not alternatives or not isinstance(additions, list):
        raise ValueError("Provide the alternatives and any explicitly proposed sensitivity additions")
    parsed, seen = [], set()
    for index, item in enumerate(alternatives + additions):
        _cancel(is_cancelled)
        if not isinstance(item, dict):
            raise ValueError("Each alternative must be a business option")
        identifier = _text(item.get("id"), "alternative.id")
        title = _text(item.get("title"), "alternative.title")
        if identifier in seen:
            raise ValueError("Alternative identities must be unique")
        seen.add(identifier)
        raw_values, checks = item.get("values"), item.get("constraint_checks", {})
        if not isinstance(raw_values, dict) or set(raw_values) - set(keys) or not isinstance(checks, dict) or set(checks) - constraint_ids:
            raise ValueError("Option values and constraints must use the saved definitions")
        values = {key: None if raw_values.get(key) is None else _number(raw_values[key], f"{identifier}.{key}") for key in keys}
        reasons = []
        assessed_checks = {}
        for constraint in constraints:
            check = checks.get(constraint["id"], {})
            if not isinstance(check, dict) or not isinstance(check.get("status", "unknown"), str) or check.get("status", "unknown") not in {"satisfied", "violated", "unknown"}:
                raise ValueError("Constraint checks distinguish satisfied, violated and unknown")
            status = check.get("status", "unknown")
            if "criterion_id" in constraint:
                actual = raw_values.get(constraint["criterion_id"])
                if actual is None:
                    computed = "unknown"
                else:
                    left, right = Decimal(str(actual)), Decimal(str(constraint["value"]))
                    valid = {"<=": left <= right, ">=": left >= right, "==": left == right}[constraint["operator"]]
                    computed = "satisfied" if valid else "violated"
                check = {"status": computed, "basis": {"criterion_id": constraint["criterion_id"], "value": actual, "operator": constraint["operator"], "limit": constraint["value"]},
                         "provenance": "computed_from_exact_declared_values", "user_check": deepcopy(check)}
                status = computed
            elif status != "unknown" and not check.get("basis"):
                raise ValueError("A satisfied or violated constraint needs its business basis")
            assessed_checks[constraint["id"]] = deepcopy(check)
            if status != "satisfied":
                reasons.append({"constraint_id": constraint["id"], "status": status, "basis": check.get("basis")})
        missing = [key for key in keys if values[key] is None]
        state = "infeasible" if any(reason["status"] == "violated" for reason in reasons) else "conditions_unknown" if reasons else "data_incomplete" if missing else "eligible"
        parsed.append({**deepcopy(item), "id": identifier, "title": title, "values": values, "input_values": deepcopy(raw_values), "constraint_checks": assessed_checks, "eligibility": state, "constraint_gaps": reasons, "missing_criteria": missing,
                       "input_set": "base" if index < len(alternatives) else "sensitivity_addition"})
    base = [item for item in parsed if item["input_set"] == "base"]
    eligible = [item for item in base if item["eligibility"] == "eligible"]
    directions = np.asarray([1 if item["direction"] == "maximize" else -1 for item in criteria])
    weights, preference = _weights(criteria, inputs.get("pairwise_preferences"))
    rankings, sensitivity = [], {"candidate_removals": [], "candidate_additions": [], "weight_paths": []}
    if eligible:
        matrix = np.asarray([[item["values"][key] for key in keys] for item in eligible])
        ids = [item["id"] for item in eligible]
        directed = [[Decimal(str(item["input_values"][key])) * int(direction) for key, direction in zip(keys, directions)] for item in eligible]
        scores = _scores(matrix, directions, weights) if weights is not None else [None] * len(ids)
        for index, item in enumerate(eligible):
            _cancel(is_cancelled)
            dominating = [ids[other] for other in range(len(ids))
                          if all(a >= b for a, b in zip(directed[other], directed[index]))
                          and any(a > b for a, b in zip(directed[other], directed[index]))]
            rankings.append({"id": item["id"], "title": item["title"], "dominated_by": dominating, "pareto_efficient": not dominating, "topsis_closeness": scores[index]})
        if weights is not None:
            baseline = _leaders(ids, scores)
            for index, identifier in enumerate(ids):
                _cancel(is_cancelled)
                if len(ids) <= 1:
                    break
                remaining = [item for item in ids if item != identifier]
                observed = _scores(np.delete(matrix, index, axis=0), directions, weights)
                sensitivity["candidate_removals"].append({"removed_id": identifier, "leaders": _leaders(remaining, observed), "scores": dict(zip(remaining, observed))})
            for item in parsed:
                _cancel(is_cancelled)
                if item["input_set"] != "sensitivity_addition" or item["eligibility"] != "eligible":
                    continue
                expanded = np.vstack([matrix, [item["values"][key] for key in keys]])
                expanded_ids = ids + [item["id"]]
                observed = _scores(expanded, directions, weights)
                sensitivity["candidate_additions"].append({"added_id": item["id"], "leaders": _leaders(expanded_ids, observed), "scores": dict(zip(expanded_ids, observed))})
            if len(keys) > 1:
                for column, key in enumerate(keys):
                    observations = []
                    others = weights.copy()
                    others[column] = 0
                    if others.sum() == 0:
                        sensitivity["weight_paths"].append({"criterion_id": key, "status": "not_identified", "reason": "Other criteria have no declared relative preferences"})
                        continue
                    others /= others.sum()
                    for step in range(101):
                        _cancel(is_cancelled)
                        value = step / 100
                        trial = others * (1 - value)
                        trial[column] = value
                        leaders = _leaders(ids, _scores(matrix, directions, trial))
                        if not observations or leaders != observations[-1]["leaders"]:
                            observations.append({"weight": value, "leaders": leaders})
                    sensitivity["weight_paths"].append({"criterion_id": key, "status": "sampled", "step": 0.01, "leader_transitions": observations,
                                                         "boundary_precision": "Adjacent grid points bound detected changes; changes between unsampled points are not excluded"})
            sensitivity["baseline_leaders"] = baseline
    labels = {"eligible": "条件与数据齐备", "infeasible": "不满足硬约束", "conditions_unknown": "关键条件待核", "data_incomplete": "比较数据不全"}
    def cell(value):
        return str(value).replace("|", "／").replace("\n", " ")
    option_rows = ["| 方案 | 本次资格 | " + " | ".join(f"{cell(item['name'])}（{cell(item['unit'])}）" for item in criteria) + " |",
                   "|---|---|" + "---:|" * len(criteria)]
    for item in base:
        option_rows.append(f"| {cell(item['title'])} | {labels[item['eligibility']]} | " + " | ".join("未知" if item["values"][key] is None else cell(item["input_values"][key]) for key in keys) + " |")
    constraint_labels = {item["id"]: item["description"] for item in constraints}
    criterion_labels = {item["id"]: item["name"] for item in criteria}
    for item in base:
        gaps = [constraint_labels[gap["constraint_id"]] + ("：不满足" if gap["status"] == "violated" else "：尚待核实") for gap in item["constraint_gaps"]]
        gaps.extend("缺少" + criterion_labels[key] for key in item["missing_criteria"])
        if gaps:
            option_rows.extend(["", item["title"] + "：" + "；".join(gaps) + "。"])
    rank_rows = ["| 方案 | Pareto关系 | 本次偏好接近度 |", "|---|---|---:|"]
    for item in rankings:
        score = item["topsis_closeness"]
        rank_rows.append(f"| {cell(item['title'])} | {'未被其他方案支配' if item['pareto_efficient'] else '被其他方案支配'} | {f'{score:.6f}' if score is not None else '不作单一排序'} |")
    if weights is not None:
        rank_rows.extend(["", "本次实际采用权重：" + "；".join(f"{item['name']} {weight:.4f}" for item, weight in zip(criteria, weights)) + "。"])
    if preference.get("method") == "ahp_principal_eigenvector":
        ratio = preference["consistency_ratio"]
        rank_rows.extend(["", f"成对偏好一致性指数 CI={preference['consistency_index']:.6f}；" + (f"CR={ratio:.6f}。" if ratio is not None else "本次适用的一致性比率尚未成立。")])
    question_body = question + "\n\n资料截止：" + (cutoff or "未单独声明，所用资料版本随本成果保存") + "。\n\n比较口径：\n" + "\n".join(
        f"- {item['name']}（{item['unit']}）：{item['definition']}；{'越大越好' if item['direction'] == 'maximize' else '越小越好'}。" for item in criteria)
    if constraints:
        question_body += "\n\n必须满足的条件：\n" + "\n".join(
            "- " + item["description"] + (f"（{criterion_labels[item['criterion_id']]} {item['operator']} {item['value']}）" if "criterion_id" in item else "") for item in constraints)
    option_labels = {item["id"]: item["title"] for item in parsed}
    def leader_text(identifiers):
        return "首选为" + "、".join(option_labels[key] for key in identifiers) if identifiers else "没有可定义的单一排序"
    sensitivity_text = ["候选增删与权重变化沿同一口径分别试算；原决定和实际行动不会被重算改写。"]
    for item in sensitivity["candidate_removals"]:
        sensitivity_text.append(f"- 移除{option_labels[item['removed_id']]}：{leader_text(item['leaders'])}。")
    for item in sensitivity["candidate_additions"]:
        sensitivity_text.append(f"- 加入{option_labels[item['added_id']]}：{leader_text(item['leaders'])}。")
    for item in sensitivity["weight_paths"]:
        criterion_name = next(criterion["name"] for criterion in criteria if criterion["id"] == item["criterion_id"])
        for transition in item.get("leader_transitions", []):
            sensitivity_text.append(f"- {criterion_name}权重取样至{transition['weight']:.2f}时，{leader_text(transition['leaders'])}。")
    limitations = ["硬约束依据与偏好来自本次输入，未由计算独立证明其正确。", "接近度不是成功概率；候选、权重、单位定义或约束变化后需重算。", "权重敏感性按0.01步长取样，不是完整连续区间的形式证明。", "比较结果没有自动采用方案、联系他人、付款或取得实际经营结果。"]
    if weights is None:
        limitations.append("没有实际偏好权重，只提供约束资格和Pareto比较，不擅自填等权。")
    if preference.get("reference_threshold_exceeded") or preference.get("exceeds_declared_threshold"):
        limitations.append("成对偏好超出参考或本次声明的一致性阈值，需核对矛盾判断；排序仅作条件试算。")
    if preference.get("method") == "ahp_principal_eigenvector" and preference.get("consistency_ratio") is None:
        limitations.append("本次矩阵阶数或判断尺度不在已核一致性基准的适用范围，保留优先向量与CI，但不判一致性合格。")
    return {"title": inputs.get("title") or "有据备选与取舍", "sections": [
        _section("decision.question", "本次选择与判断条件", question_body, {"question": question, "criteria": deepcopy(criteria), "constraints": deepcopy(constraints),
                 "information_cutoff": cutoff, "cutoff_precision": cutoff_precision, "cutoff_status": "user_declared" if cutoff else "not_specified"}),
        _section("decision.options", "可用方案与约束缺口", "\n".join(option_rows), {"alternatives": parsed}),
        _section("decision.comparison", "可比方案与当前偏好", "\n".join(rank_rows) if rankings else "当前没有条件和数据同时齐备的方案；保留各项缺口，不伪造赢家。", {"rankings": rankings, "weights": dict(zip(keys, weights.tolist())) if weights is not None else None, "preference_diagnostic": preference, "normalization": "criterion-wise vector norm; no missing-value imputation"}),
        _section("decision.sensitivity", "改变条件时的选择", "\n\n".join(sensitivity_text), sensitivity),
    ], "limitations": limitations}
