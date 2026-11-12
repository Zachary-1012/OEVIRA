"""Statistical core for business experiments, attribution, and incrementality.

This module consumes already-authorized, source-bound inputs. It owns no
database, identity, publication, exposure collection, or experiment execution.
In particular, a saved artifact version is not evidence that the version was
displayed to an audience.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
import random
import re
from statistics import NormalDist, mean, stdev
from typing import Any


_METHOD_LIMITATIONS = {
    "experiment_design": [
        "A protocol description does not create random assignment, distribute traffic, or verify delivery.",
        "A stored artifact version identifies an internal object only; external exposure needs its own source record.",
    ],
    "experiment_analysis": [
        "Intervals assume independent analysis units and the stated outcome model; clustering, selection, noncompliance, and missing outcomes can change interpretation.",
        "A confidence interval describes sampling uncertainty under its assumptions; it does not repair biased allocation, changed metrics, or unobserved outcomes.",
        "Randomization and exposure evidence are supplied by the caller and are not independently authenticated here.",
        "Finite-look sequential results require the exact predeclared schedule and Bonferroni family; they do not cover unplanned looks or adaptive allocation.",
    ],
    "attribution_analysis": [
        "First-touch, last-touch, and linear credits describe the supplied observed journeys; they do not estimate causal incrementality or commissions.",
        "Journey identity, conversion truth, event-time completeness, and source authorization are caller responsibilities.",
        "Currency totals are kept separate. Net actual value subtracts only supplied, matched actual refunds and does not adjust tax, fees, shipping, or foreign exchange.",
        "Markov removal redirects a removed channel to nonconversion; exact Shapley is a path-model allocation, not a causal rerouting estimate.",
    ],
    "incrementality_analysis": [
        "A difference-in-differences estimate is causal only conditional on a credible comparison and assumptions including parallel untreated trends, no anticipation, no material spillover, stable or correctly adjusted composition, and no differential co-intervention.",
        "Observed pre-period gaps can challenge a design but cannot prove post-period counterfactual parallel trends; a non-significant pretrend check is not proof of equivalence.",
        "Uncertainty uses independent unit or cluster change scores and does not cover unmodeled serial correlation, staggered treatment timing, or measurement error.",
        "This operation implements a balanced-panel DiD only; synthetic control and staggered-adoption estimators are not inferred from this result.",
    ],
}


def _fail(path: str, reason: str = "invalid") -> ValueError:
    return ValueError(f"{path}: {reason}")


def _validate_json_tree(value: Any, path: str = "inputs") -> None:
    """Reject non-JSON values and non-finite numbers without coercing booleans."""
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (int, float):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            raise _fail(path, "numeric values must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise _fail(path, "object keys must be strings")
            _validate_json_tree(item, f"{path}.{key}")
        return
    raise _fail(path, "value is not JSON-compatible")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(path, "must be an array")
    return value


def _text(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise _fail(path, "must be a non-empty string")
    return value


def _optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _text(value, path)


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_minimum: bool = False,
    strict_maximum: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise _fail(path, "must be a finite number (booleans are not numbers here)")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _fail(path, "must be finite") from exc
    if not math.isfinite(number):
        raise _fail(path, "must be finite")
    if minimum is not None and (number <= minimum if strict_minimum else number < minimum):
        raise _fail(path, f"must be {'greater than' if strict_minimum else 'at least'} {minimum}")
    if maximum is not None and (number >= maximum if strict_maximum else number > maximum):
        raise _fail(path, f"must be {'less than' if strict_maximum else 'at most'} {maximum}")
    return number


def _positive_integer(value: Any, path: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        qualifier = "a non-negative" if allow_zero else "a positive"
        raise _fail(path, f"must be {qualifier} integer")
    return value


def _instant(value: Any, path: str) -> datetime:
    if type(value) is not str:
        raise _fail(path, "must be an ISO-8601 timestamp with an explicit time zone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(path, "timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail(path, "timestamp must include an explicit time zone")
    return parsed


def _window(value: Any, path: str = "window") -> tuple[datetime | None, datetime | None]:
    if value is None:
        return None, None
    window = _mapping(value, path)
    start_value, end_value = window.get("start"), window.get("end")
    if start_value is None and end_value is None:
        return None, None
    if start_value is None or end_value is None:
        raise _fail(path, "start and end must be supplied together")
    start, end = _instant(start_value, f"{path}.start"), _instant(end_value, f"{path}.end")
    if start >= end:
        raise _fail(path, "start must precede end")
    return start, end


def _json_body(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)


def _section(section_id: str, title: str, data: dict[str, Any], explanation: str) -> dict[str, Any]:
    tables = _result_tables(section_id, data)
    return {"id": section_id, "title": title, "body": explanation + ("\n\n" + tables if tables else ""), "data": data}


def _table(headers, rows):
    def cell(value):
        if value is None:
            return "未知"
        if isinstance(value, float):
            return format(value, ".8g")
        if isinstance(value, (list, tuple)):
            return " ～ ".join(cell(item) for item in value)
        return str(value).replace("|", "\\|").replace("\n", " ")
    rows = list(rows)
    if not rows:
        return "暂无合格观测可列示。"
    return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" + "\n".join("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)


def _result_tables(section_id, data):
    """The file consumers receive the same numeric result as the structured UI."""
    if section_id == "experiment_protocol":
        metric = data.get("primary_metric") or {}
        window = data.get("window") or {}
        fields = [("待验证假设", data.get("hypothesis")), ("分配单位", data.get("assignment_unit")), ("分析单位", data.get("analysis_unit")),
                  ("主要指标", metric.get("name")), ("单位", metric.get("unit")), ("分母", metric.get("denominator")),
                  ("观察开始", window.get("start")), ("观察结束", window.get("end")), ("停止规则", data.get("stopping_rule")), ("预定成功条件", data.get("success_definition"))]
        return _table(["协议项目", "本次约定"], fields) + "\n\n" + _table(["实验组", "角色", "预定份额", "作品版本"], ((arm["id"], arm.get("role"), arm.get("expected_share"), arm.get("artifact_version")) for arm in data.get("arms") or []))
    if section_id == "experiment_design_checks":
        return "待补协议项目：" + ("、".join(data["unknown_fields"]) or "本次所核字段没有缺项；执行资格仍须核验。")
    if section_id == "experiment_power":
        return _table(
            ["功效计划项目", "计算结果"],
            [
                ("状态", data.get("state")),
                ("方法", data.get("method")),
                ("显著性水平", data.get("alpha")),
                ("目标功效", data.get("target_power")),
                ("分配比（处理/对照）", data.get("allocation_ratio_treatment_to_control")),
                ("预计对照组独立单位", data.get("required_control_n")),
                ("预计处理组独立单位", data.get("required_treatment_n")),
                ("合计单位数", data.get("total_n")),
                ("正态近似实际功效", data.get("achieved_power_normal_approximation")),
                ("缺少条件", data.get("reason")),
            ],
        )
    if section_id == "experiment_sequential":
        looks = data.get("looks", [])
        rows = [
            [
                look.get("look_index"), look.get("look_at"), look.get("mature_units_by_arm"),
                comparison.get("treatment_arm"), comparison.get("estimate"), comparison.get("interval"),
                comparison.get("p_value"), comparison.get("alpha_per_comparison_per_look"),
                "是" if comparison.get("boundary_crossed") else "否",
            ]
            for look in looks
            for comparison in look.get("comparisons", [])
        ]
        return _table(
            ["查看序号", "预定时点", "成熟样本/组", "处理组", "差值", "调整后区间", "p值", "本次alpha", "越界"],
            rows,
        ) + "\n\n" + _table(
            ["规则项目", "结果"],
            [
                ("计划查看数", data.get("planned_looks")),
                ("家族错误率", data.get("alpha_familywise")),
                ("决策状态", data.get("decision")),
                ("首次越界", data.get("first_boundary_crossing")),
                ("边界控制", data.get("boundary_familywise_control")),
            ],
        )
    if section_id == "calibration_results":
        raw = data.get("raw_test_metrics") or {}
        calibrated = data.get("calibrated_test_metrics") or {}
        bins = calibrated.get("bins", [])
        return _table(
            ["校准结果", "本次结果"],
            [
                ("状态", data.get("state")),
                ("方法", data.get("method")),
                ("训练排除核验", data.get("lineage_state")),
                ("校准样本数", data.get("calibration_n")),
                ("留出样本数", data.get("test_n")),
                ("原始Brier", raw.get("brier_score")),
                ("校准后Brier", calibrated.get("brier_score")),
                ("原始Log loss", raw.get("log_loss")),
                ("校准后Log loss", calibrated.get("log_loss")),
                ("校准后ECE（10等宽箱）", calibrated.get("ece_10_equal_width_bins")),
                ("原因", data.get("reason")),
            ],
        ) + "\n\n留出可靠性分箱：\n\n" + _table(
            ["概率下界", "概率上界", "样本数", "平均预测概率", "实测发生率"],
            ((row["lower"], row["upper"], row["n"], row["mean_probability"], row["observed_rate"]) for row in bins),
        )
    if section_id == "conformal_results":
        return _table(
            ["Conformal评价", "结果"],
            [
                ("状态", data.get("state")),
                ("方法", data.get("method")),
                ("模型训练排除", data.get("lineage_state")),
                ("校准样本数", data.get("calibration_n")),
                ("测试样本数", data.get("test_n")),
                ("名义覆盖率", data.get("nominal_coverage")),
                ("校准残差分位数", data.get("residual_quantile")),
                ("测试实测覆盖率", data.get("test_coverage")),
                ("覆盖率95% Wilson区间", data.get("test_coverage_wilson_interval")),
                ("平均区间宽度", data.get("mean_interval_width")),
                ("覆盖解释", data.get("coverage_claim")),
                ("原因", data.get("reason")),
            ],
        )
    if section_id == "experiment.next_observation_suggestion":
        rows = [
            ("本次状态", data.get("state")),
            ("候选下一臂", data.get("suggested_arm_label")),
            ("建议概率（条件后验）", data.get("suggestion_probability_best_estimate")),
            ("Beta先验", data.get("prior")),
            ("抽样次数", data.get("draw_count")),
            ("固定试验条件", data.get("eligibility_basis")),
            ("实际执行", "未分配流量" if data.get("traffic_assignment_performed") is False else data.get("traffic_assignment_performed")),
            ("原因/条件缺口", "; ".join(data.get("reason") or [])),
        ]
        return _table(["下一批观察", "本次结果"], rows) + "\n\n" + _table(
            ["实验组", "成熟结果数", "成功数", "失败数", "后验均值", "后验95%可信区间", "成为最优臂的后验概率（MC）", "MC标准误"],
            ((row.get("arm_label"), row.get("mature_outcome_units"), row.get("successes"), row.get("failures"),
              row.get("posterior_mean"), row.get("posterior_interval_95"),
              row.get("posterior_probability_best_estimate"), row.get("probability_best_mc_standard_error"))
             for row in data.get("arms", [])),
        )
    if section_id == "assignment_integrity":
        sample, diagnostics = data["sample_ratio"], data["diagnostics"]
        rows = [("样本比率卡方值", sample.get("chi_square")), ("样本比率p值", sample.get("p_value")), ("预设告警阈值", sample.get("alpha")),
                ("唯一分配单位", diagnostics.get("unique_assignment_units")), ("重复分配单位", diagnostics.get("repeated_assignment_units")),
                ("有外部曝光记录的单位", diagnostics.get("external_exposure_records")), ("曝光版本不符", diagnostics.get("exposure_version_mismatches"))]
        return _table(["质量核对", "实际结果"], rows)
    if section_id == "outcome_estimates":
        binary = data["metric"]["kind"] == "binary"
        arms = _table(["实验组", "分配单位数", "成熟结果数", "未知或未成熟", "成功数" if binary else "均值", "成熟样本比率" if binary else "样本标准差"],
                      ((arm["arm_id"], arm["assigned_units"], arm["mature_outcome_units"], arm["missing_or_immature_units"], arm.get("successes" if binary else "mean"), arm.get("rate_among_mature_outcomes" if binary else "sample_standard_deviation")) for arm in data["arms"]))
        differences = _table(["处理组", "对照组", "差值", "区间", "置信水平", "单位"], ((row["treatment_arm"], row["control_arm"], row.get("estimate"), row.get("interval"), row["confidence_level"], "绝对比率差" if binary else row["unit_of_measure"]) for row in data["comparisons"]))
        return arms + "\n\n" + differences
    if section_id == "experiment_interpretation":
        return "\n".join("- " + value for value in data.get("blockers", [])) or "本次条件记录齐备；随机分配与外部执行仍是调用方提供的证据。"
    if section_id == "attribution_journey_coverage":
        labels = {"journeys": "旅程数", "known_conversions": "已知转化旅程", "journeys_with_eligible_touch": "含合格触点旅程", "conversions_without_eligible_touch": "转化但无合格触点", "raw_touchpoints": "原始触点", "eligible_touchpoints": "窗口内合格触点", "unknown_conversion_status": "转化状态未知", "duplicate_refund_notifications_collapsed": "重复退款通知", "unmatched_refunds": "未匹配退款"}
        return _table(["覆盖项目", "数量"], ((label, data.get(key)) for key, label in labels.items()))
    if section_id == "attribution_descriptive_credits":
        parts = [_table(["渠道", "按规则分配的转化份额"], data["conversion_credit_by_channel"].items())]
        for field, title in (("gross_conversion_value_actual_by_currency", "实际成交金额"), ("actual_refunds_by_currency", "实际退款"), ("net_actual_conversion_value_by_currency", "净实际成交金额"), ("gross_conversion_value_estimated_by_currency", "估计成交金额")):
            parts.append(title + "\n\n" + _table(["币种", "金额"], data[field].items()))
        for field, title in (("allocated_value_actual_by_currency_and_channel", "按规则分配的实际成交金额"), ("net_actual_value_credit_by_currency_and_channel", "扣退款后的实际金额分配")):
            parts.append(title + "\n\n" + _table(["币种", "渠道", "金额"], ((currency, channel, amount) for currency, channels in data[field].items() for channel, amount in channels.items())))
        return "\n\n".join(parts)
    if section_id == "attribution_markov_shapley":
        summary = data.get("summary", {})
        channel_rows = data.get("channels", [])
        transitions = data.get("transition_probabilities", [])
        coverage = data.get("coverage", {})
        transition_rows = [
            [row.get("from_state"), row.get("to_state"), row.get("probability"), row.get("transition_count")]
            for row in transitions
        ]
        return (
            _table(
                ["检查项", "结果"],
                [
                    ("模型状态", data.get("state")),
                    ("计算原因", data.get("reason")),
                    ("路径样本", summary.get("path_count")),
                    ("已知转化路径", summary.get("converted_paths")),
                    ("已知未转化路径", summary.get("nonconverted_paths")),
                    ("模型基线转化概率", summary.get("baseline_conversion_probability")),
                    ("无渠道基线", summary.get("no_channel_conversion_probability")),
                    ("Shapley状态", summary.get("shapley_state")),
                    ("置换计算次数", summary.get("shapley_permutation_count")),
                    ("路径覆盖缺项", coverage.get("incomplete_path_count")),
                ],
            )
            + "\n\n" + _table(
                ["渠道", "移除后模型概率", "移除效果", "Shapley贡献", "基线份额", "计算误差标准误", "计算误差95%近似区间"],
                ((row.get("channel"), row.get("probability_without_channel"), row.get("removal_effect"),
                  row.get("shapley_contribution"), row.get("shapley_share_of_baseline"),
                  row.get("shapley_monte_carlo_standard_error"),
                  row.get("shapley_monte_carlo_approximation_interval_95")) for row in channel_rows),
            )
            + "\n\n经验转移概率：\n\n" + _table(
                ["来源状态", "下一状态", "概率", "观察到的转移次数"], transition_rows,
            )
        )
    if section_id == "incrementality_did_estimate":
        return _table(["本次估计", "结果"], [("处理组平均变化", data["treatment_mean_change"]), ("对照组平均变化", data["control_mean_change"]), ("变化之差", data["estimate"]), ("区间", data["interval"]), ("置信水平", data["confidence_level"]), ("指标单位", data["unit"]), ("处理组独立单位", data["treatment_change_n"]), ("对照组独立单位", data["control_change_n"])]) + "\n\n" + ("\n".join("- " + value for value in data["qualification_blockers"]) or "条件资格满足当前提供记录；不等无条件因果证明。")
    if section_id == "incrementality_counterfactual":
        return "比较依据：" + str(data.get("comparison_rationale") or "未知") + "\n\n" + _table(["识别条件", "当前审阅"], data["assessments"].items()) + "\n\n反证：\n" + "\n".join("- " + str(value) for value in data["counterevidence"])
    if section_id == "incrementality_period_diagnostics":
        return _table(["期间", "处理组均值", "对照组均值", "组间差"], ((period, data["period_means"].get(period, {}).get("treatment"), data["period_means"].get(period, {}).get("control"), data["period_gaps"].get(period)) for period in data["period_order"]))
    return ""


def _response(
    title: str,
    sections: list[dict[str, Any]],
    limitations: list[str],
) -> dict[str, Any]:
    result = {"title": title, "sections": sections, "limitations": list(dict.fromkeys(limitations))}
    _validate_json_tree(result, "result")
    return result


def _validate_arms(
    value: Any,
    path: str,
    *,
    require_multiple: bool = False,
) -> list[dict[str, Any]]:
    arms = _sequence(value, path)
    if require_multiple and len(arms) < 2:
        raise _fail(path, "must contain at least two arms")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_arm in enumerate(arms):
        arm = _mapping(raw_arm, f"{path}[{index}]")
        arm_id = _text(arm.get("id"), f"{path}[{index}].id")
        if arm_id in seen:
            raise _fail(f"{path}[{index}].id", "arm identifiers must be unique")
        seen.add(arm_id)
        role = arm.get("role")
        if role is not None:
            role = _text(role, f"{path}[{index}].role")
        share = arm.get("expected_share")
        if share is not None:
            share = _number(share, f"{path}[{index}].expected_share", minimum=0, maximum=1)
        normalized.append({
            "id": arm_id,
            "role": role,
            "expected_share": share,
            "artifact_version": arm.get("artifact_version"),
        })
    return normalized


def _validate_metric(value: Any, path: str = "metric") -> dict[str, Any]:
    metric = _mapping(value, path)
    kind = metric.get("kind")
    if kind not in {"binary", "continuous"}:
        raise _fail(f"{path}.kind", "must be 'binary' or 'continuous'")
    name = _optional_text(metric.get("name"), f"{path}.name")
    unit = _optional_text(metric.get("unit"), f"{path}.unit")
    denominator = _optional_text(metric.get("denominator"), f"{path}.denominator")
    success_definition = _optional_text(metric.get("success_definition"), f"{path}.success_definition")
    return {
        "kind": kind,
        "name": name,
        "unit": unit,
        "denominator": denominator,
        "success_definition": success_definition,
    }


def _power_sample_size(metric: dict[str, Any], plan: Any, *, arms: list[dict[str, Any]] | None, control_arm: str | None) -> dict[str, Any]:
    if plan is None:
        return {"state": "NOT_REQUESTED", "required_control_n": None, "required_treatment_n": None}
    plan = _mapping(plan, "power_plan")
    expected_keys = (
        {"control_rate", "treatment_rate"}
        if metric["kind"] == "binary"
        else {"standard_deviation", "minimum_effect"}
    )
    common_keys = {"target_power", "alpha"}
    if set(plan) - expected_keys - common_keys or expected_keys - set(plan):
        raise _fail("power_plan", "does not match the declared primary metric")
    if arms is None or len(arms) != 2 or control_arm is None:
        return {
            "state": "INSUFFICIENT",
            "reason": "POWER_PLAN_REQUIRES_TWO_DECLARED_ARMS_AND_CONTROL",
            "required_control_n": None,
            "required_treatment_n": None,
        }
    treatment_arms = [arm for arm in arms if arm["id"] != control_arm]
    if len(treatment_arms) != 1:
        return {
            "state": "INSUFFICIENT",
            "reason": "POWER_PLAN_REQUIRES_ONE_TREATMENT_ARM",
            "required_control_n": None,
            "required_treatment_n": None,
        }
    target_power = _number(
        plan.get("target_power", 0.8), "power_plan.target_power",
        minimum=0.5, maximum=1, strict_minimum=True, strict_maximum=True,
    )
    alpha = _number(
        plan.get("alpha", 0.05), "power_plan.alpha",
        minimum=0, maximum=1, strict_minimum=True, strict_maximum=True,
    )
    allocation_ratio = 1.0
    if arms and all(arm["expected_share"] is not None for arm in arms):
        by_id = {arm["id"]: arm["expected_share"] for arm in arms}
        control_share = by_id[control_arm]
        treatment_share = by_id[treatment_arms[0]["id"]]
        if control_share <= 0 or treatment_share <= 0:
            return {
                "state": "INSUFFICIENT",
                "reason": "DECLARED_ALLOCATION_HAS_ZERO_ARM_SHARE",
                "required_control_n": None,
                "required_treatment_n": None,
            }
        allocation_ratio = treatment_share / control_share
    z_alpha = NormalDist().inv_cdf(1 - float(alpha) / 2)
    z_power = NormalDist().inv_cdf(float(target_power))
    if metric["kind"] == "binary":
        control_rate = _number(plan["control_rate"], "power_plan.control_rate", minimum=0, maximum=1)
        treatment_rate = _number(plan["treatment_rate"], "power_plan.treatment_rate", minimum=0, maximum=1)
        effect = abs(float(treatment_rate - control_rate))
        if effect <= 0:
            raise _fail("power_plan.treatment_rate", "must differ from the control rate")
        pooled = (float(control_rate) + allocation_ratio * float(treatment_rate)) / (1 + allocation_ratio)
        null_variance_per_control = pooled * (1 - pooled) * (1 + 1 / allocation_ratio)
        alternative_variance_per_control = (
            float(control_rate) * (1 - float(control_rate))
            + float(treatment_rate) * (1 - float(treatment_rate)) / allocation_ratio
        )
        control_n = math.ceil(
            (z_alpha * math.sqrt(null_variance_per_control)
             + z_power * math.sqrt(alternative_variance_per_control)) ** 2
            / effect ** 2
        )
        treatment_n = math.ceil(control_n * allocation_ratio)
        null_se = math.sqrt(pooled * (1 - pooled) * (1 / control_n + 1 / treatment_n))
        alternative_se = math.sqrt(
            float(control_rate) * (1 - float(control_rate)) / control_n
            + float(treatment_rate) * (1 - float(treatment_rate)) / treatment_n
        )
        noncentrality = effect / null_se
        critical = z_alpha
        achieved_power = (
            1 - NormalDist().cdf(critical - noncentrality * null_se / alternative_se)
            + NormalDist().cdf(-critical - noncentrality * null_se / alternative_se)
        )
        effect_input = {"control_rate": float(control_rate), "treatment_rate": float(treatment_rate)}
        method = "two-proportion-normal-approximation@1"
    else:
        standard_deviation = _number(plan["standard_deviation"], "power_plan.standard_deviation", minimum=0, strict_minimum=True)
        effect_value = _number(plan["minimum_effect"], "power_plan.minimum_effect", minimum=0, strict_minimum=True)
        control_n = math.ceil(
            (z_alpha + z_power) ** 2 * float(standard_deviation) ** 2
            * (1 + 1 / allocation_ratio) / float(effect_value) ** 2
        )
        treatment_n = math.ceil(control_n * allocation_ratio)
        alternative_se = float(standard_deviation) * math.sqrt(1 / control_n + 1 / treatment_n)
        noncentrality = float(effect_value) / alternative_se
        achieved_power = (
            1 - NormalDist().cdf(z_alpha - noncentrality)
            + NormalDist().cdf(-z_alpha - noncentrality)
        )
        effect_input = {
            "standard_deviation": float(standard_deviation),
            "minimum_effect": float(effect_value),
        }
        method = "two-mean-normal-approximation@1"
    return {
        "state": "ESTIMATED",
        "method": method,
        "target_power": float(target_power),
        "alpha": float(alpha),
        "two_sided": True,
        "allocation_ratio_treatment_to_control": allocation_ratio,
        "required_control_n": control_n,
        "required_treatment_n": treatment_n,
        "total_n": control_n + treatment_n,
        "achieved_power_normal_approximation": float(achieved_power),
        "effect_inputs": effect_input,
        "assignment_unit": "independent analyzed units; cluster design effect not included",
        "limitations": [
            "This is an asymptotic two-arm planning approximation, not a guarantee of realized power or observed effect.",
            "It does not include multiplicity, attrition, noncompliance, repeated-measure correlation, or cluster design effects.",
            "The target effect and variance/rate assumptions must be justified from prior evidence; changing them creates a new design.",
        ],
    }


def _experiment_design(inputs: dict[str, Any]) -> dict[str, Any]:
    arms: list[dict[str, Any]] | None = None
    if "arms" in inputs:
        arms = _validate_arms(inputs["arms"], "arms")

    metric = _validate_metric(inputs["primary_metric"]) if "primary_metric" in inputs else None
    window = inputs.get("window")
    start, end = _window(window)
    if window is not None:
        _mapping(window, "window")

    control_arm = _optional_text(inputs.get("control_arm"), "control_arm")
    if control_arm is not None and arms is not None and control_arm not in {arm["id"] for arm in arms}:
        raise _fail("control_arm", "must identify one of the declared arms")

    assignment_unit = _optional_text(inputs.get("assignment_unit"), "assignment_unit")
    analysis_unit = _optional_text(inputs.get("analysis_unit"), "analysis_unit")
    allocation_method = _optional_text(inputs.get("allocation_method"), "allocation_method")
    randomization_method = _optional_text(inputs.get("randomization_method"), "randomization_method")
    randomization_record = _optional_text(inputs.get("randomization_record"), "randomization_record")
    exposure_definition = _optional_text(inputs.get("exposure_definition"), "exposure_definition")
    eligibility_definition = _optional_text(inputs.get("eligibility_definition"), "eligibility_definition")
    sample_size_target = inputs.get("sample_size_target")
    if sample_size_target is not None:
        sample_size_target = _positive_integer(sample_size_target, "sample_size_target")
    power_plan = _power_sample_size(metric, inputs.get("power_plan"), arms=arms, control_arm=control_arm) if metric is not None else (
        {"state": "INSUFFICIENT", "reason": "PRIMARY_METRIC_REQUIRED", "required_control_n": None, "required_treatment_n": None}
        if inputs.get("power_plan") is not None else {"state": "NOT_REQUESTED", "required_control_n": None, "required_treatment_n": None}
    )

    window_summary = None if window is None else {
        "start": window.get("start"),
        "end": window.get("end"),
        "valid": start is not None and end is not None,
        "timezone": window.get("timezone"),
    }
    allocation_shares = None
    if arms and all(arm["expected_share"] is not None for arm in arms):
        shares = [arm["expected_share"] for arm in arms]
        if not math.isclose(sum(shares), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise _fail("arms.expected_share", "declared shares must sum to 1")
        allocation_shares = {arm["id"]: arm["expected_share"] for arm in arms}

    protocol = {
        "hypothesis": _optional_text(inputs.get("hypothesis"), "hypothesis"),
        "assignment_unit": assignment_unit,
        "analysis_unit": analysis_unit,
        "allocation_method": allocation_method,
        "randomization_method": randomization_method,
        "randomization_record": randomization_record,
        "control_arm": control_arm,
        "arms": arms,
        "expected_allocation": allocation_shares,
        "primary_metric": metric,
        "window": window_summary,
        "eligibility_definition": eligibility_definition,
        "exposure_definition": exposure_definition,
        "sample_size_target": sample_size_target,
        "power_plan": power_plan,
        "stopping_rule": inputs.get("stopping_rule"),
        "contamination_exclusions": inputs.get("contamination_exclusions"),
        "missing_and_withdrawal_rules": inputs.get("missing_and_withdrawal_rules"),
        "cost_and_guardrail_metrics": inputs.get("cost_and_guardrail_metrics"),
        "success_definition": inputs.get("success_definition"),
    }

    limitations = list(_METHOD_LIMITATIONS["experiment_design"])
    for field, label in (
        ("hypothesis", "hypothesis"),
        ("assignment_unit", "assignment unit"),
        ("analysis_unit", "analysis unit"),
        ("allocation_method", "allocation method"),
        ("primary_metric", "primary metric and denominator"),
        ("window", "observation window"),
        ("stopping_rule", "stopping rule"),
        ("success_definition", "predefined success definition"),
    ):
        if protocol[field] is None:
            limitations.append(f"The {label} is unknown or was not supplied; the design cannot be treated as a complete prespecified protocol.")
    if arms is None or len(arms) < 2:
        limitations.append("At least one explicit treatment and one comparison arm are still needed for a between-arm experiment.")
    if arms and any(arm["artifact_version"] is not None for arm in arms):
        limitations.append("Arm artifact_version values preserve internal version binding only; they do not establish which version an audience actually saw.")
    if randomization_method and not randomization_record:
        limitations.append("A named randomization method without an assignment record is a declared design, not verified randomized execution.")

    sections = [
        _section(
            "experiment_protocol",
            "实验协议",
            protocol,
            "以下内容按给定输入保留为协议草案。未填写项仍为未知；变体版本、曝光资格、分配单位、分析单位、指标分母、窗口与预先停止/成功规则应能在同一实验版本中复查。",
        ),
        _section(
            "experiment_design_checks",
            "设计核对",
            {
                "allocation_shares_sum_to_one": allocation_shares is not None,
                "window_order_valid": start is not None and end is not None,
                "randomization_record_supplied": bool(randomization_record),
                "external_exposure_evidence_supplied_at_design_time": False,
                "unknown_fields": [
                    name for name, value in protocol.items()
                    if value is None and name not in {"randomization_record"}
                ],
            },
            "分配比例或时间窗只有在明确给出时才核验。这里不替系统分流量，也不把内容档案中的版本号提升为外部展示证据。",
        ),
    ]
    if inputs.get("power_plan") is not None:
        sections.append(_section(
            "experiment_power",
            "样本量与功效计划",
            power_plan,
            "只有给出主要指标、两组角色及必要的预期基线/目标率或连续指标标准差/最小差异时才计算样本量。此近似按独立分析单位计算，说明所用显著性水平和目标功效；它不是结果预测或成功保证。",
        ))
    return _response("内容实验设计", sections, limitations)


def _wilson(successes: int, trials: int, confidence: float) -> list[float] | None:
    if trials <= 0:
        return None
    proportion = successes / trials
    z = NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (proportion + z2 / (2.0 * trials)) / denominator
    half = z * math.sqrt(
        proportion * (1.0 - proportion) / trials + z2 / (4.0 * trials * trials)
    ) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def _newcombe_difference(
    success_treatment: int,
    trials_treatment: int,
    success_control: int,
    trials_control: int,
    confidence: float,
) -> tuple[float, list[float] | None, list[float] | None, list[float] | None]:
    rate_treatment = success_treatment / trials_treatment
    rate_control = success_control / trials_control
    interval_treatment = _wilson(success_treatment, trials_treatment, confidence)
    interval_control = _wilson(success_control, trials_control, confidence)
    assert interval_treatment is not None and interval_control is not None
    difference = rate_treatment - rate_control
    lower_distance = math.sqrt(
        (rate_treatment - interval_treatment[0]) ** 2
        + (interval_control[1] - rate_control) ** 2
    )
    upper_distance = math.sqrt(
        (interval_treatment[1] - rate_treatment) ** 2
        + (rate_control - interval_control[0]) ** 2
    )
    return (
        difference,
        [max(-1.0, difference - lower_distance), min(1.0, difference + upper_distance)],
        interval_treatment,
        interval_control,
    )


def _sample_variance(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    average = mean(values)
    return sum((value - average) ** 2 for value in values) / (len(values) - 1)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Modified Lentz evaluation used by the regularized incomplete beta."""
    maximum_iterations = 300
    epsilon = 3.0e-14
    tiny = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    result = d
    for iteration in range(1, maximum_iterations + 1):
        twice = 2 * iteration
        coefficient = iteration * (b - iteration) * x / ((qam + twice) * (a + twice))
        d = 1.0 + coefficient * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        result *= d * c
        coefficient = -(a + iteration) * (qab + iteration) * x / ((a + twice) * (qap + twice))
        d = 1.0 + coefficient * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            return result
    raise ArithmeticError("incomplete beta continued fraction did not converge")


def _regularized_beta(x: float, a: float, b: float) -> float:
    if not (a > 0.0 and b > 0.0 and 0.0 <= x <= 1.0):
        raise ArithmeticError("incomplete beta parameters are outside their domain")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return min(1.0, max(0.0, front * _beta_continued_fraction(a, b, x) / a))
    complement = front * _beta_continued_fraction(b, a, 1.0 - x) / b
    return min(1.0, max(0.0, 1.0 - complement))


def _student_t_cdf(value: float, degrees_of_freedom: float) -> float:
    if degrees_of_freedom <= 0.0:
        raise ArithmeticError("degrees of freedom must be positive")
    if value == 0.0:
        return 0.5
    x = degrees_of_freedom / (degrees_of_freedom + value * value)
    tail = 0.5 * _regularized_beta(x, degrees_of_freedom / 2.0, 0.5)
    return 1.0 - tail if value > 0.0 else tail


def _student_t_quantile(probability: float, degrees_of_freedom: float) -> float:
    if not 0.0 < probability < 1.0:
        raise ArithmeticError("probability must be strictly between zero and one")
    if math.isclose(probability, 0.5, abs_tol=1e-15):
        return 0.0
    if probability < 0.5:
        return -_student_t_quantile(1.0 - probability, degrees_of_freedom)
    low, high = 0.0, 1.0
    while _student_t_cdf(high, degrees_of_freedom) < probability:
        high *= 2.0
        if not math.isfinite(high):
            raise ArithmeticError("could not bracket Student t quantile")
    for _ in range(160):
        midpoint = (low + high) / 2.0
        if _student_t_cdf(midpoint, degrees_of_freedom) < probability:
            low = midpoint
        else:
            high = midpoint
        if high - low <= 2e-13 * max(1.0, midpoint):
            break
    return (low + high) / 2.0


def _welch_difference(
    treatment: list[float],
    control: list[float],
    confidence: float,
) -> dict[str, Any]:
    if not treatment or not control:
        return {"estimate": None, "standard_error": None, "degrees_of_freedom": None, "interval": None, "reason": "an arm has no mature outcomes"}
    estimate = mean(treatment) - mean(control)
    variance_treatment = _sample_variance(treatment)
    variance_control = _sample_variance(control)
    if variance_treatment is None or variance_control is None:
        return {"estimate": estimate, "standard_error": None, "degrees_of_freedom": None, "interval": None, "reason": "at least two independent outcomes per arm are required for a sampling interval"}
    component_treatment = variance_treatment / len(treatment)
    component_control = variance_control / len(control)
    standard_error = math.sqrt(component_treatment + component_control)
    if standard_error == 0.0:
        return {"estimate": estimate, "standard_error": 0.0, "degrees_of_freedom": None, "interval": None, "reason": "both arms have zero observed variance; a zero-width interval would overstate information"}
    denominator = (
        component_treatment * component_treatment / (len(treatment) - 1)
        + component_control * component_control / (len(control) - 1)
    )
    if denominator <= 0.0:
        return {"estimate": estimate, "standard_error": standard_error, "degrees_of_freedom": None, "interval": None, "reason": "Welch degrees of freedom are not identifiable"}
    degrees = (component_treatment + component_control) ** 2 / denominator
    critical = _student_t_quantile((1.0 + confidence) / 2.0, degrees)
    margin = critical * standard_error
    return {
        "estimate": estimate,
        "standard_error": standard_error,
        "degrees_of_freedom": degrees,
        "interval": [estimate - margin, estimate + margin],
        "reason": None,
    }


def _gamma_upper_regularized(shape: float, value: float) -> float:
    """Regularized upper incomplete gamma Q(shape, value), for SRM p-values."""
    if shape <= 0.0 or value < 0.0:
        raise ArithmeticError("incomplete gamma parameters are outside their domain")
    if value == 0.0:
        return 1.0
    if value < shape + 1.0:
        total = term = 1.0 / shape
        current_shape = shape
        for _ in range(10000):
            current_shape += 1.0
            term *= value / current_shape
            total += term
            if abs(term) <= abs(total) * 3.0e-14:
                break
        lower = total * math.exp(-value + shape * math.log(value) - math.lgamma(shape))
        return min(1.0, max(0.0, 1.0 - lower))

    tiny = 1.0e-300
    b = value + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / b
    fraction = d
    for iteration in range(1, 10000):
        coefficient = -iteration * (iteration - shape)
        b += 2.0
        d = coefficient * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        fraction *= delta
        if abs(delta - 1.0) < 3.0e-14:
            break
    upper = fraction * math.exp(-value + shape * math.log(value) - math.lgamma(shape))
    return min(1.0, max(0.0, upper))


def _sample_ratio_result(arms: list[dict[str, Any]], counts: dict[str, int], alpha: float | None) -> dict[str, Any]:
    if not arms or any(arm["expected_share"] is None for arm in arms):
        return {
            "state": "not_assessable",
            "observed_counts": counts,
            "expected_shares": None,
            "chi_square": None,
            "degrees_of_freedom": None,
            "p_value": None,
            "alpha": alpha,
            "alert": None,
            "reason": "expected allocation shares were not supplied for every arm",
        }
    shares = [arm["expected_share"] for arm in arms]
    if any(share <= 0.0 for share in shares) or not math.isclose(sum(shares), 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise _fail("arms.expected_share", "all expected shares must be positive and sum to 1")
    total = sum(counts.values())
    if total == 0:
        return {
            "state": "not_assessable",
            "observed_counts": counts,
            "expected_shares": {arm["id"]: arm["expected_share"] for arm in arms},
            "chi_square": None,
            "degrees_of_freedom": len(arms) - 1,
            "p_value": None,
            "alpha": alpha,
            "alert": None,
            "reason": "no unambiguous assigned units",
        }
    statistic = 0.0
    expected_counts: dict[str, float] = {}
    for arm in arms:
        expected = total * arm["expected_share"]
        expected_counts[arm["id"]] = expected
        statistic += (counts[arm["id"]] - expected) ** 2 / expected
    degrees = len(arms) - 1
    p_value = _gamma_upper_regularized(degrees / 2.0, statistic / 2.0)
    alert = None if alpha is None else p_value < alpha
    return {
        "state": "computed",
        "observed_counts": counts,
        "expected_counts": expected_counts,
        "expected_shares": {arm["id"]: arm["expected_share"] for arm in arms},
        "chi_square": statistic,
        "degrees_of_freedom": degrees,
        "p_value": p_value,
        "alpha": alpha,
        "alert": alert,
        "reason": None if alpha is not None else "a prespecified SRM alert threshold was not supplied; p-value is reported without a pass/fail label",
    }


def _normalise_analysis_rows(
    inputs: dict[str, Any],
    arms: list[dict[str, Any]],
    metric: dict[str, Any],
    start: datetime | None,
    end: datetime | None,
    *,
    is_cancelled=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .business_analysis_economics import _check_cancelled

    arm_ids = {arm["id"] for arm in arms}
    raw_rows = _sequence(inputs.get("observations"), "observations")
    by_unit: dict[str, list[dict[str, Any]]] = {}
    out_of_window = 0
    time_unverified = 0
    missing_reasons: dict[str, int] = {}
    exposure_outside_window = 0
    exposure_window_unknown = 0

    for index, raw in enumerate(raw_rows):
        _check_cancelled(is_cancelled)
        path = f"observations[{index}]"
        row = _mapping(raw, path)
        unit_id = _text(row.get("unit_id"), f"{path}.unit_id")
        arm_id = _text(row.get("arm_id"), f"{path}.arm_id")
        if arm_id not in arm_ids:
            raise _fail(f"{path}.arm_id", "must match a declared arm")
        if "outcome" not in row:
            raise _fail(f"{path}.outcome", "must be supplied; use null for unknown or immature outcomes")
        outcome = row["outcome"]
        if outcome is not None:
            if metric["kind"] == "binary":
                if type(outcome) is not int or outcome not in (0, 1):
                    raise _fail(f"{path}.outcome", "binary outcomes must be integer 0 or 1; booleans are not accepted")
            else:
                outcome = _number(outcome, f"{path}.outcome")
        mature = row.get("outcome_mature", outcome is not None)
        if type(mature) is not bool:
            raise _fail(f"{path}.outcome_mature", "must be boolean")
        missing_reason = row.get("missing_reason")
        if missing_reason is not None:
            missing_reason = _text(missing_reason, f"{path}.missing_reason")
        withdrawal_reason = row.get("withdrawal_reason")
        if withdrawal_reason is not None:
            withdrawal_reason = _text(withdrawal_reason, f"{path}.withdrawal_reason")
        if outcome is None or not mature:
            reason = withdrawal_reason or missing_reason or "unknown_reason"
            missing_reasons[reason] = missing_reasons.get(reason, 0) + 1
        assigned_at = None
        if row.get("assigned_at") is not None:
            assigned_at = _instant(row["assigned_at"], f"{path}.assigned_at")
        outcome_at = None
        if row.get("outcome_at") is not None:
            outcome_at = _instant(row["outcome_at"], f"{path}.outcome_at")
        usable = outcome is not None and mature
        if usable and start is not None:
            if outcome_at is None:
                usable = False
                time_unverified += 1
            elif not (start <= outcome_at < end):
                usable = False
                out_of_window += 1
        cluster_id = row.get("cluster_id")
        if cluster_id is not None:
            cluster_id = _text(cluster_id, f"{path}.cluster_id")
        external_exposure = row.get("external_exposure")
        if external_exposure is not None:
            external_exposure = _mapping(external_exposure, f"{path}.external_exposure")
            version_id = _text(external_exposure.get("version_id"), f"{path}.external_exposure.version_id")
            observed_at = _instant(external_exposure.get("observed_at"), f"{path}.external_exposure.observed_at")
            source_ref = _text(external_exposure.get("source_ref"), f"{path}.external_exposure.source_ref")
            if start is None:
                exposure_window_unknown += 1
                external_exposure = None
            elif not start <= observed_at < end:
                exposure_outside_window += 1
                external_exposure = None
            else:
                external_exposure = {"version_id": version_id, "observed_at": observed_at.isoformat(), "source_ref": source_ref}
        by_unit.setdefault(unit_id, []).append({
            "arm_id": arm_id,
            "outcome": outcome if usable else None,
            "outcome_mature": mature,
            "assigned_at": assigned_at,
            "outcome_at": outcome_at,
            "cluster_id": cluster_id,
            "external_exposure": external_exposure,
            "missing_reason": missing_reason,
            "withdrawal_reason": withdrawal_reason,
        })

    normalized: list[dict[str, Any]] = []
    cross_arm_units: list[str] = []
    ambiguous_units: list[str] = []
    repeated_units = 0
    exposure_units = 0
    exposure_mismatches: list[str] = []
    exposure_changed: list[str] = []
    exposure_unverifiable: list[str] = []
    cluster_conflicts: list[str] = []
    arm_by_id = {arm["id"]: arm for arm in arms}

    for unit_id, records in by_unit.items():
        _check_cancelled(is_cancelled)
        if len(records) > 1:
            repeated_units += 1
        unit_arms = {record["arm_id"] for record in records}
        if len(unit_arms) != 1:
            cross_arm_units.append(unit_id)
            continue
        arm_id = next(iter(unit_arms))
        cluster_ids = {record["cluster_id"] for record in records if record["cluster_id"] is not None}
        if len(cluster_ids) > 1:
            cluster_conflicts.append(unit_id)
        usable_outcomes = [record["outcome"] for record in records if record["outcome"] is not None]
        distinct_outcomes: list[int | float] = []
        for outcome in usable_outcomes:
            if not any(outcome == existing for existing in distinct_outcomes):
                distinct_outcomes.append(outcome)
        ambiguous = len(distinct_outcomes) > 1 or len(cluster_ids) > 1
        if ambiguous:
            ambiguous_units.append(unit_id)
        exposures = [record["external_exposure"] for record in records if record["external_exposure"] is not None]
        version_ids = {exposure["version_id"] for exposure in exposures}
        if exposures:
            exposure_units += 1
        if len(version_ids) > 1:
            exposure_changed.append(unit_id)
        expected_version = arm_by_id[arm_id]["artifact_version"]
        expected_version_id = expected_version if isinstance(expected_version, str) else (
            expected_version.get("version_id") if isinstance(expected_version, dict) else None
        )
        if exposures and isinstance(expected_version_id, str):
            if any(version != expected_version_id for version in version_ids):
                exposure_mismatches.append(unit_id)
        elif exposures:
            exposure_unverifiable.append(unit_id)
        normalized.append({
            "unit_id": unit_id,
            "arm_id": arm_id,
            "outcome": None if ambiguous or not distinct_outcomes else distinct_outcomes[0],
            "outcome_records": (
                [
                    {"outcome": record["outcome"], "outcome_at": record["outcome_at"].isoformat()}
                    for record in records
                    if record["outcome"] is not None and record["outcome_at"] is not None
                ]
                if not ambiguous else []
            ),
            "cluster_id": next(iter(cluster_ids)) if len(cluster_ids) == 1 else None,
            "external_exposure_recorded": bool(exposures),
            "assigned_at": min(
                (record["assigned_at"] for record in records if record["assigned_at"] is not None),
                default=None,
            ),
        })

    cluster_arms: dict[str, set[str]] = {}
    cluster_units: dict[str, set[str]] = {}
    for row in normalized:
        _check_cancelled(is_cancelled)
        cluster = row["cluster_id"]
        if cluster is None:
            continue
        cluster_arms.setdefault(cluster, set()).add(row["arm_id"])
        cluster_units.setdefault(cluster, set()).add(row["unit_id"])
    crossing_clusters = sorted(cluster for cluster, assigned_arms in cluster_arms.items() if len(assigned_arms) > 1)
    shared_clusters = sorted(cluster for cluster, unit_ids in cluster_units.items() if len(unit_ids) > 1)

    diagnostics = {
        "event_rows": len(raw_rows),
        "unique_assignment_units": len(by_unit),
        "repeated_assignment_units": repeated_units,
        "cross_arm_units": len(cross_arm_units),
        "ambiguous_repeated_units": len(ambiguous_units),
        "cluster_identity_conflicts": len(cluster_conflicts),
        "clusters_shared_by_multiple_units": len(shared_clusters),
        "clusters_crossing_arms": len(crossing_clusters),
        "external_exposure_records": exposure_units,
        "external_exposure_coverage": exposure_units / len(normalized) if normalized else None,
        "external_exposures_outside_window": exposure_outside_window,
        "external_exposure_window_unknown": exposure_window_unknown,
        "exposure_version_changes_within_unit": len(exposure_changed),
        "exposure_version_mismatches": len(exposure_mismatches),
        "exposure_version_unverifiable": len(exposure_unverifiable),
        "outcomes_outside_window": out_of_window,
        "outcomes_with_unverified_time": time_unverified,
        "missing_or_immature_rows_by_reason": dict(sorted(missing_reasons.items())),
        "units_with_repeated_rows": sorted(unit_id for unit_id, records in by_unit.items() if len(records) > 1),
        "cross_arm_unit_ids": sorted(cross_arm_units),
        "ambiguous_unit_ids": sorted(ambiguous_units),
        "shared_cluster_ids": shared_clusters,
        "crossing_cluster_ids": crossing_clusters,
        "exposure_mismatch_unit_ids": sorted(exposure_mismatches),
        "exposure_unverifiable_unit_ids": sorted(exposure_unverifiable),
        "exposure_changed_unit_ids": sorted(exposure_changed),
    }
    return normalized, diagnostics


def _sequential_analysis(
    value: Any,
    normalized: list[dict[str, Any]],
    arms: list[dict[str, Any]],
    metric: dict[str, Any],
    control_arm: str,
    *,
    start: datetime | None,
    end: datetime | None,
    causal_blockers: list[str],
    is_cancelled=None,
) -> dict[str, Any] | None:
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    if value is None:
        return None
    plan = _mapping(value, "sequential_plan")
    if set(plan) != {"look_times", "alpha", "minimum_mature_units_per_arm"}:
        raise _fail("sequential_plan", "requires planned look_times, alpha and minimum mature units per arm")
    raw_looks = _sequence(plan["look_times"], "sequential_plan.look_times")
    if not 2 <= len(raw_looks) <= 500:
        raise _fail("sequential_plan.look_times", "must list at least two predeclared looks")
    looks = [_instant(item, f"sequential_plan.look_times[{index}]") for index, item in enumerate(raw_looks)]
    if len(set(looks)) != len(looks) or any(left >= right for left, right in zip(looks, looks[1:])):
        raise _fail("sequential_plan.look_times", "must be strictly increasing and unique")
    if start is None or end is None:
        raise _fail("sequential_plan", "a fixed experiment window is required")
    if any(not start <= look < end for look in looks):
        raise _fail("sequential_plan.look_times", "all looks must fall inside the declared experiment window")
    alpha = _number(plan["alpha"], "sequential_plan.alpha", minimum=0, maximum=1, strict_minimum=True, strict_maximum=True)
    minimum = _positive_integer(plan["minimum_mature_units_per_arm"], "sequential_plan.minimum_mature_units_per_arm")
    treatment_arms = [arm for arm in arms if arm["id"] != control_arm]
    if not treatment_arms:
        raise _fail("sequential_plan", "at least one treatment arm is required")
    if any("adaptive allocation requires a propensity-aware estimator" in blocker for blocker in causal_blockers):
        return {
            "method": "fixed-look-bonferroni-sequential@1",
            "state": "NOT_AVAILABLE",
            "alpha_familywise": alpha,
            "planned_looks": len(looks),
            "planned_look_times": [look.isoformat() for look in looks],
            "comparisons": [],
            "per_look_per_comparison_alpha": None,
            "minimum_mature_units_per_arm": minimum,
            "boundary_familywise_control": None,
            "decision": "DESCRIPTIVE_ONLY_NO_STOP_RECOMMENDATION",
            "first_boundary_crossing": None,
            "looks": [],
            "causal_qualification_blockers": list(causal_blockers),
            "reason": "ADAPTIVE_ASSIGNMENT_INVALIDATES_FIXED_LOOK_TESTS",
            "limitations": [
                "This fixed-look test cannot be applied to data collected under adaptive assignment without the saved assignment probabilities and a propensity-aware sequential method.",
                "No stopping recommendation or p-value is produced for the adaptive stage.",
            ],
        }
    comparisons = len(treatment_arms)
    per_look_alpha = alpha / (len(looks) * comparisons)
    critical_normal = NormalDist().inv_cdf(1 - per_look_alpha / 2)
    by_id = {arm["id"]: arm for arm in arms}
    results = []
    first_crossing = None
    for look_index, look in enumerate(looks):
        _check_cancelled(is_cancelled)
        per_arm: dict[str, list[int | float]] = {arm["id"]: [] for arm in arms}
        for row in normalized:
            _check_cancelled(is_cancelled)
            available = []
            for record in row.get("outcome_records", []):
                measured = _instant(record["outcome_at"], "outcome_at")
                if measured <= look:
                    available.append((measured, record["outcome"]))
            if available:
                _, outcome = min(available, key=lambda item: item[0])
                per_arm[row["arm_id"]].append(outcome)
        mature_counts = {arm_id: len(values) for arm_id, values in per_arm.items()}
        ready = all(count >= minimum for count in mature_counts.values())
        look_comparisons = []
        crossed = False
        for treatment in treatment_arms:
            treatment_values = per_arm[treatment["id"]]
            control_values = per_arm[control_arm]
            estimate = standard_error = p_value = None
            interval = None
            method = None
            if ready and metric["kind"] == "binary":
                successes_treatment = sum(int(item) for item in treatment_values)
                successes_control = sum(int(item) for item in control_values)
                rate_treatment = successes_treatment / len(treatment_values)
                rate_control = successes_control / len(control_values)
                estimate = rate_treatment - rate_control
                pooled = (successes_treatment + successes_control) / (len(treatment_values) + len(control_values))
                standard_error = math.sqrt(
                    pooled * (1 - pooled) * (1 / len(treatment_values) + 1 / len(control_values))
                )
                if standard_error > 0:
                    z_score = estimate / standard_error
                    p_value = min(1.0, 2 * (1 - NormalDist().cdf(abs(z_score))))
                    interval = [estimate - critical_normal * standard_error, estimate + critical_normal * standard_error]
                elif estimate == 0:
                    p_value, interval = 1.0, [0.0, 0.0]
                else:
                    p_value, interval = 0.0, [estimate, estimate]
                method = "two-proportion-z-with-finite-look-bonferroni@1"
            elif ready:
                welch = _welch_difference(
                    [float(item) for item in treatment_values],
                    [float(item) for item in control_values],
                    1 - per_look_alpha,
                )
                estimate = welch["estimate"]
                standard_error = welch["standard_error"]
                interval = welch["interval"]
                degrees = welch["degrees_of_freedom"]
                if standard_error is not None and degrees is not None:
                    t_score = estimate / standard_error
                    p_value = min(1.0, 2 * (1 - _student_t_cdf(abs(t_score), degrees)))
                method = "welch-t-with-finite-look-bonferroni@1"
            crossed_now = p_value is not None and p_value <= per_look_alpha
            crossed = crossed or crossed_now
            look_comparisons.append({
                "treatment_arm": treatment["id"],
                "control_arm": control_arm,
                "estimate": estimate,
                "standard_error": standard_error,
                "interval": interval,
                "p_value": p_value,
                "alpha_per_comparison_per_look": per_look_alpha,
                "method": method,
                "boundary_crossed": crossed_now,
            })
        if crossed and first_crossing is None:
            first_crossing = {"look_index": look_index + 1, "at": look.isoformat()}
        results.append({
            "look_index": look_index + 1,
            "look_at": look.isoformat(),
            "mature_units_by_arm": mature_counts,
            "minimum_per_arm": minimum,
            "sample_size_ready": ready,
            "comparisons": look_comparisons,
        })
    if causal_blockers:
        decision = "DESCRIPTIVE_ONLY_NO_STOP_RECOMMENDATION"
    elif first_crossing is not None:
        decision = "STOP_FOR_EFFICACY_AT_FIRST_CROSSED_LOOK"
    else:
        decision = "NO_EFFICACY_BOUNDARY_CROSSED"
    return {
        "method": "fixed-look-bonferroni-sequential@1",
        "state": "READY" if all(row["sample_size_ready"] for row in results) else "PARTIAL",
        "alpha_familywise": alpha,
        "planned_looks": len(looks),
        "planned_look_times": [look.isoformat() for look in looks],
        "comparisons": comparisons,
        "per_look_per_comparison_alpha": per_look_alpha,
        "minimum_mature_units_per_arm": minimum,
        "boundary_familywise_control": "Bonferroni across the fixed planned look/comparison family; no unplanned extra looks",
        "decision": decision,
        "first_boundary_crossing": first_crossing,
        "looks": results,
        "causal_qualification_blockers": list(causal_blockers),
        "limitations": [
            "This fixed-look Bonferroni rule is conservative and valid only for the predeclared finite look/comparison family and valid per-look tests.",
            "Adding looks, changing outcomes, or selecting an earlier boundary after viewing results invalidates the registered rule; create a new protocol instead.",
            "Independent assignment units and correct outcome maturity are required; cluster correlation is not corrected here.",
            "A statistical boundary crossing does not authorize publication, traffic allocation, or other external action.",
        ],
    }


def _thompson_next_observation_suggestion(
    arms: list[dict[str, Any]],
    per_arm: list[dict[str, Any]],
    *,
    metric: dict[str, Any],
    allocation_method: str | None,
    randomization_record: str | None,
    assignment_unit: str | None,
    analysis_unit: str | None,
    sequential_plan: Any,
    sample_ratio: dict[str, Any],
    diagnostics: dict[str, Any],
    causal_blockers: list[str],
    is_cancelled=None,
) -> dict[str, Any]:
    """Return a reproducible recommendation only; it never assigns traffic."""
    import hashlib
    import numpy as np

    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    arm_rows = []
    for arm, summary in zip(arms, per_arm):
        _check_cancelled(is_cancelled)
        if metric.get("kind") != "binary":
            successes = None
            failures = None
        else:
            successes = summary.get("successes")
            failures = summary.get("mature_outcome_units", 0) - (successes or 0)
        arm_rows.append({
            "arm_id": arm["id"],
            "arm_label": arm.get("label") or arm["id"],
            "artifact_version": deepcopy(arm.get("artifact_version")),
            "assigned_units": summary.get("assigned_units"),
            "mature_outcome_units": summary.get("mature_outcome_units"),
            "missing_or_immature_units": summary.get("missing_or_immature_units"),
            "successes": successes,
            "failures": failures,
            "posterior_alpha": None,
            "posterior_beta": None,
            "posterior_mean": None,
            "posterior_interval_95": None,
            "posterior_probability_best_estimate": None,
            "probability_best_mc_standard_error": None,
        })

    blockers = []
    if metric.get("kind") != "binary":
        blockers.append("BETA_BERNOULLI_REQUIRES_BINARY_OUTCOMES")
    if allocation_method != "randomized":
        blockers.append("BASELINE_ALLOCATION_IS_NOT_DECLARED_RANDOMIZED")
    if not randomization_record:
        blockers.append("RANDOMIZATION_RECORD_IS_MISSING")
    if not assignment_unit or not analysis_unit or assignment_unit != analysis_unit:
        blockers.append("ASSIGNMENT_AND_ANALYSIS_UNITS_MUST_MATCH")
    if sequential_plan is not None:
        blockers.append("FIXED_LOOK_PLAN_REQUIRES_A_SEPARATE_ADAPTIVE_STAGE")
    if sample_ratio.get("state") != "computed" or sample_ratio.get("alpha") is None:
        blockers.append("PRESPECIFIED_SAMPLE_RATIO_DIAGNOSTIC_IS_REQUIRED")
    elif sample_ratio.get("alert") is not False:
        blockers.append("SAMPLE_RATIO_DIAGNOSTIC_IS_NOT_CLEAR")
    if diagnostics.get("cross_arm_units") or diagnostics.get("ambiguous_repeated_units"):
        blockers.append("ASSIGNMENT_UNIT_OR_OUTCOME_IDENTITY_IS_AMBIGUOUS")
    if diagnostics.get("clusters_shared_by_multiple_units") or diagnostics.get("clusters_crossing_arms"):
        blockers.append("CLUSTER_DEPENDENCE_IS_NOT_HANDLED_BY_THIS_BETA_BERNOULLI_MODEL")
    if any(arm.get("artifact_version") is None for arm in arms):
        blockers.append("EXACT_VARIANT_VERSION_BINDING_IS_REQUIRED")
    blockers.extend(causal_blockers)
    if metric.get("kind") == "binary" and any(row["mature_outcome_units"] < 1 for row in arm_rows):
        blockers.append("EACH_ARM_NEEDS_AT_LEAST_ONE_MATURE_OUTCOME")

    result = {
        "state": "NOT_AVAILABLE" if blockers else "SUGGESTION_AVAILABLE",
        "method": "beta-bernoulli-thompson-next-observation@1",
        "request_received": True,
        "request_origin_verified": False,
        "executed": False,
        "traffic_assignment_performed": False,
        "authorization": "suggestion_only_no_external_action",
        "prior": {"alpha": 1, "beta": 1, "basis": "fixed_uniform_bernoulli_prior"},
        "draw_count": None,
        "sampling_seed_sha256": None,
        "arms": arm_rows,
        "suggested_arm_id": None,
        "suggested_arm_label": None,
        "suggestion_probability_best_estimate": None,
        "reason": list(dict.fromkeys(blockers)) or None,
        "limitations": [
            "This is a conditional next-observation recommendation from mature supplied binary outcomes, not a causal effect or winning-variant claim.",
            "The randomization record, result labels and external exposure evidence are supplied by the caller and are not independently authenticated.",
            "No traffic is assigned and no external action is authorized by this calculation.",
            "If a later stage uses the recommendation, it must save actual assignment probabilities and exposure records; do not reuse fixed-design inferential claims on adaptively collected outcomes.",
        ],
    }
    if blockers:
        return result

    alpha = np.asarray([1 + row["successes"] for row in arm_rows], dtype=float)
    beta = np.asarray([1 + row["failures"] for row in arm_rows], dtype=float)
    seed_payload = {
        "method": result["method"],
        "arms": [{"id": arm["id"], "artifact_version": arm.get("artifact_version"),
                  "successes": row["successes"], "mature_outcome_units": row["mature_outcome_units"]}
                 for arm, row in zip(arms, arm_rows)],
        "assignment_unit": assignment_unit,
        "analysis_unit": analysis_unit,
        "randomization_record_supplied": bool(randomization_record),
    }
    seed_digest = hashlib.sha256(json.dumps(
        seed_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).digest()
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(seed_digest[:16], "big")))
    draws = 4096
    best_counts = np.zeros(len(arms), dtype=float)
    posterior_draws = [[] for _ in arms]
    for draw_index in range(draws):
        if draw_index % 128 == 0:
            _check_cancelled(is_cancelled)
        sample = rng.beta(alpha, beta)
        for index, value in enumerate(sample):
            posterior_draws[index].append(float(value))
        maximum = float(sample.max())
        winners = np.flatnonzero(np.abs(sample - maximum) <= 1e-12)
        best_counts[winners] += 1.0 / len(winners)
    recommendation_draw = rng.beta(alpha, beta)
    selected = int(np.argmax(recommendation_draw))
    for index, row in enumerate(arm_rows):
        probability = float(best_counts[index] / draws)
        row.update(
            posterior_alpha=int(alpha[index]),
            posterior_beta=int(beta[index]),
            posterior_mean=float(alpha[index] / (alpha[index] + beta[index])),
            posterior_interval_95=[float(value) for value in np.quantile(posterior_draws[index], [0.025, 0.975])],
            posterior_probability_best_estimate=probability,
            probability_best_mc_standard_error=math.sqrt(probability * (1.0 - probability) / draws),
            recommendation_draw=float(recommendation_draw[index]),
        )
    result.update(
        state="SUGGESTION_AVAILABLE",
        draw_count=draws,
        sampling_seed_sha256=seed_digest.hex(),
        suggested_arm_id=arms[selected]["id"],
        suggested_arm_label=arm_rows[selected]["arm_label"],
        suggestion_probability_best_estimate=arm_rows[selected]["posterior_probability_best_estimate"],
        reason=None,
    )
    return result


def _experiment_analysis(inputs: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    arms = _validate_arms(inputs.get("arms"), "arms", require_multiple=True)
    if len(arms) < 2:
        raise _fail("arms", "must contain at least two arms")
    metric = _validate_metric(inputs.get("metric"))
    control_arm = _text(inputs.get("control_arm"), "control_arm")
    arm_ids = {arm["id"] for arm in arms}
    if control_arm not in arm_ids:
        raise _fail("control_arm", "must match a declared arm")
    start, end = _window(inputs.get("window"))
    confidence_was_supplied = "confidence_level" in inputs
    confidence = _number(inputs.get("confidence_level", 0.95), "confidence_level", minimum=0, maximum=1, strict_minimum=True, strict_maximum=True)
    alpha = None
    if inputs.get("srm_alpha") is not None:
        alpha = _number(inputs["srm_alpha"], "srm_alpha", minimum=0, maximum=1, strict_minimum=True, strict_maximum=True)
    allocation_method = _optional_text(inputs.get("allocation_method"), "allocation_method")
    adaptive_allocation = allocation_method in {"adaptive", "adaptive_randomized"}
    randomization_record = _optional_text(inputs.get("randomization_record"), "randomization_record")
    assignment_unit = _optional_text(inputs.get("assignment_unit"), "assignment_unit")
    analysis_unit = _optional_text(inputs.get("analysis_unit"), "analysis_unit")
    sequential_plan = inputs.get("sequential_plan")
    request_next_observation = inputs.get("request_next_observation_suggestion", False)
    if type(request_next_observation) is not bool:
        raise _fail("request_next_observation_suggestion", "must be a boolean opt-in")
    normalized, diagnostics = _normalise_analysis_rows(inputs, arms, metric, start, end, is_cancelled=is_cancelled)

    assignment_counts = {arm["id"]: 0 for arm in arms}
    outcomes_by_arm: dict[str, list[int | float]] = {arm["id"]: [] for arm in arms}
    mature_counts = {arm["id"]: 0 for arm in arms}
    missing_counts = {arm["id"]: 0 for arm in arms}
    for row in normalized:
        _check_cancelled(is_cancelled)
        assignment_counts[row["arm_id"]] += 1
        if row["outcome"] is None:
            missing_counts[row["arm_id"]] += 1
        else:
            mature_counts[row["arm_id"]] += 1
            outcomes_by_arm[row["arm_id"]].append(row["outcome"])

    srm = (
        {"state": "not_assessable", "observed_counts": assignment_counts,
         "expected_shares": None, "chi_square": None, "degrees_of_freedom": None,
         "p_value": None, "alpha": alpha, "alert": None,
         "reason": "adaptive assignment probabilities vary by decision; fixed-share SRM is not applicable"}
        if adaptive_allocation
        else _sample_ratio_result(arms, assignment_counts, alpha)
    )
    shared_cluster_dependency = diagnostics["clusters_shared_by_multiple_units"] > 0
    cluster_crossing = diagnostics["clusters_crossing_arms"] > 0
    per_arm: list[dict[str, Any]] = []
    for arm in arms:
        _check_cancelled(is_cancelled)
        values = outcomes_by_arm[arm["id"]]
        row: dict[str, Any] = {
            "arm_id": arm["id"],
            "role": arm["role"],
            "assigned_units": assignment_counts[arm["id"]],
            "mature_outcome_units": mature_counts[arm["id"]],
            "missing_or_immature_units": missing_counts[arm["id"]],
        }
        if metric["kind"] == "binary":
            successes = sum(int(value) for value in values)
            rate = successes / len(values) if values else None
            row.update({
                "successes": successes,
                "rate_among_mature_outcomes": rate,
                "wilson_interval": _wilson(successes, len(values), confidence) if values else None,
            })
        else:
            row.update({
                "mean": mean(values) if values else None,
                "sample_standard_deviation": math.sqrt(_sample_variance([float(value) for value in values])) if len(values) > 1 else None,
            })
        per_arm.append(row)

    comparisons: list[dict[str, Any]] = []
    for arm in arms:
        _check_cancelled(is_cancelled)
        if arm["id"] == control_arm:
            continue
        treatment_values = outcomes_by_arm[arm["id"]]
        control_values = outcomes_by_arm[control_arm]
        comparison: dict[str, Any] = {
            "treatment_arm": arm["id"],
            "control_arm": control_arm,
            "metric": metric,
            "confidence_level": confidence,
            "confidence_level_defaulted": not confidence_was_supplied,
            "unit_of_measure": metric["unit"],
            "analysis_unit": analysis_unit,
            "interpretation": "descriptive_difference_on_mature_supplied_outcomes",
        }
        if metric["kind"] == "binary":
            if treatment_values and control_values:
                estimate, interval, treatment_interval, control_interval = _newcombe_difference(
                    sum(int(value) for value in treatment_values),
                    len(treatment_values),
                    sum(int(value) for value in control_values),
                    len(control_values),
                    confidence,
                )
                comparison.update({
                    "method": "newcombe_wilson_difference@1",
                    "estimate": estimate,
                    "interval": interval,
                    "treatment_rate_interval": treatment_interval,
                    "control_rate_interval": control_interval,
                    "direction": "treatment minus control; absolute proportion points",
                })
            else:
                comparison.update({"method": "newcombe_wilson_difference@1", "estimate": None, "interval": None, "reason": "both arms need mature outcomes"})
        else:
            result = _welch_difference([float(value) for value in treatment_values], [float(value) for value in control_values], confidence)
            comparison.update({
                "method": "welch_t_mean_difference@1",
                "estimate": result["estimate"],
                "standard_error": result["standard_error"],
                "degrees_of_freedom": result["degrees_of_freedom"],
                "interval": result["interval"],
                "reason": result["reason"],
                "direction": "treatment mean minus control mean",
            })
        if adaptive_allocation:
            comparison["interval"] = None
            comparison["standard_error"] = None
            comparison["degrees_of_freedom"] = None
            comparison["interval_suppressed_reason"] = "adaptive allocation requires a propensity-aware estimator"
            if metric["kind"] == "binary":
                comparison["treatment_rate_interval"] = None
                comparison["control_rate_interval"] = None
        if shared_cluster_dependency:
            comparison["interval"] = None
            comparison["standard_error"] = None
            comparison["degrees_of_freedom"] = None
            comparison["interval_suppressed_reason"] = "shared clusters require cluster-level analysis or a justified dependence adjustment"
        if cluster_crossing:
            comparison["interpretation"] = "descriptive_only_cluster_crosses_arms"
        comparisons.append(comparison)

    causal_blockers: list[str] = []
    if adaptive_allocation:
        causal_blockers.append("adaptive allocation requires a propensity-aware estimator")
    elif allocation_method != "randomized":
        causal_blockers.append("allocation method is not explicitly recorded as randomized")
    if not randomization_record:
        causal_blockers.append("randomization record is absent")
    if srm.get("alert") is True:
        causal_blockers.append("the prespecified sample-ratio check raised an alert")
    if diagnostics["cross_arm_units"]:
        causal_blockers.append("one or more analysis units appear in multiple arms")
    if diagnostics["ambiguous_repeated_units"]:
        causal_blockers.append("repeated-unit outcomes or cluster identity are ambiguous")
    if cluster_crossing:
        causal_blockers.append("a reported cluster contains units assigned to multiple arms")
    if diagnostics["exposure_version_mismatches"] or diagnostics["exposure_version_changes_within_unit"]:
        causal_blockers.append("reported external exposure does not consistently match the arm version")
    if diagnostics["exposure_version_unverifiable"]:
        causal_blockers.append("external exposure could not be compared with an exact arm version")
    if any(arm["artifact_version"] is None for arm in arms):
        causal_blockers.append("one or more arms lack an exact artifact version binding")
    if diagnostics["external_exposures_outside_window"]:
        causal_blockers.append("some reported external exposure falls outside the experiment window")
    if diagnostics["external_exposure_window_unknown"]:
        causal_blockers.append("the experiment window is unknown, so external exposure timing cannot be checked")
    if any(row["artifact_version"] is not None for row in arms) and diagnostics["external_exposure_records"] == 0:
        causal_blockers.append("no external exposure records bind audience observations to the arm versions")
    if any(row["artifact_version"] is not None for row in arms) and diagnostics["external_exposure_records"] < diagnostics["unique_assignment_units"]:
        causal_blockers.append("external exposure evidence is missing for one or more assigned units")
    assignment_interpretation = (
        "conditional_randomized_assignment_effect; caller supplied but randomization and execution were not independently authenticated"
        if not causal_blockers
        else "descriptive_difference_only"
    )
    for comparison in comparisons:
        comparison["interpretation"] = assignment_interpretation
        if causal_blockers:
            comparison["causal_interpretation_blockers"] = causal_blockers
        else:
            comparison["causal_interpretation_blockers"] = []

    next_observation = (
        _thompson_next_observation_suggestion(
            arms, per_arm, metric=metric, allocation_method=allocation_method,
            randomization_record=randomization_record, assignment_unit=assignment_unit,
            analysis_unit=analysis_unit, sequential_plan=sequential_plan,
            sample_ratio=srm, diagnostics=diagnostics, causal_blockers=causal_blockers,
            is_cancelled=is_cancelled,
        )
        if request_next_observation else None
    )

    sequential_result = _sequential_analysis(
        sequential_plan,
        normalized,
        arms,
        metric,
        control_arm,
        start=start,
        end=end,
        causal_blockers=causal_blockers,
        is_cancelled=is_cancelled,
    )

    limitations = list(_METHOD_LIMITATIONS["experiment_analysis"])
    if not confidence_was_supplied:
        limitations.append("A 95% confidence level was used as a disclosed computational default; it is not a success threshold or a sequential stopping rule.")
    if adaptive_allocation:
        limitations.append("Adaptive assignment has no fixed expected-share SRM or ordinary fixed-design effect interval in this result; save per-decision assignment probabilities for propensity-aware analysis.")
    elif srm["state"] == "not_assessable":
        limitations.append("Sample-ratio mismatch could not be fully assessed because expected arm shares or unambiguous assignment units were unavailable.")
    elif alpha is None:
        limitations.append("The sample-ratio chi-square p-value has no alert label because no prespecified alpha was supplied.")
    if diagnostics["repeated_assignment_units"]:
        limitations.append("Repeated rows for the same assignment unit were counted once; conflicting unit outcomes were excluded from effect estimates.")
    if diagnostics["clusters_shared_by_multiple_units"]:
        limitations.append("Some assignment units share a reported cluster; intervals are suppressed until dependence is handled at a suitable unit.")
    if diagnostics["external_exposure_records"] < diagnostics["unique_assignment_units"]:
        limitations.append("Missing external exposure records remain unknown; they are not treated as failed exposure or failed outcome.")
    if diagnostics["exposure_version_mismatches"]:
        limitations.append("Version mismatch is treated as exposure contamination; it is not silently reassigned to a different arm.")
    if sequential_result is not None:
        limitations.extend(sequential_result["limitations"])
    if next_observation is not None:
        limitations.extend(next_observation["limitations"])

    sections = [
        _section(
            "assignment_integrity",
            "分配、样本比率与单位诊断",
            {"sample_ratio": srm, "diagnostics": diagnostics, "assignment_unit": assignment_unit, "analysis_unit": analysis_unit},
            "样本比率按唯一且只有一个分组的分配单位计算；事件行或重复用户不会扩成独立试验单位。SRM 是数据质量信号，需查明来源后再解释。",
        ),
        _section(
            "outcome_estimates",
            "实绩、估计与不确定性",
            {"metric": metric, "arms": per_arm, "comparisons": comparisons, "confidence_level": confidence},
            "二项指标报告成熟结果单位中的实测成功率及 Wilson/Newcombe 区间；连续指标报告均值差和 Welch t 区间。未知或未成熟结果不补成失败。多臂比较的区间未作多重比较调整。",
        ),
        _section(
            "experiment_interpretation",
            "因果资格与限制",
            {"allocation_method": allocation_method, "randomization_record_supplied": bool(randomization_record), "comparison_interpretation": assignment_interpretation, "blockers": causal_blockers},
            "只有随机分配方法与记录、单位独立性、样本完整性和实际版本曝光都满足时，差异才标为有条件的分配效应；本模块不认证随机化、曝光或无混杂。其余结果保持描述性。",
        ),
    ]
    if sequential_result is not None:
        sections.append(
            _section(
                "experiment_sequential",
                "预先安排的顺序观察",
                sequential_result,
                "仅按事先列明的固定查看时点评估已成熟结果。有限次数的Bonferroni边界控制计划内的多次查看；新增时点或更改指标须建立新实验版本。若随机分配、独立单位或实际执行条件不成立，只列边界计算，不建议停止。",
            )
        )
    if next_observation is not None:
        sections.append(
            _section(
                "experiment.next_observation_suggestion",
                "下一批观察建议",
                next_observation,
                "这是一条经请求计算的候选建议，不表示用户已批准自适应试验；本操作不分配流量、不创建实验阶段，也不触发外部动作。",
            )
        )
    return _response("内容实验分析", sections, limitations)


def _markov_shapley(paths: list[dict[str, Any]], *, is_cancelled=None) -> dict[str, Any]:
    """Estimate an absorbing empirical path model and channel contributions.

    Removed channels route their incoming probability to nonconversion, matching
    the finite convention in the historical method. Small channel sets use exact
    Shapley enumeration; larger sets use a reproducible permutation estimate.
    Neither is a counterfactual rerouting model or a causal effect.
    """
    import numpy as np
    from scipy import sparse
    from scipy.sparse.linalg import MatrixRankWarning, spsolve

    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)

    channels = sorted({channel for path in paths for channel in path["channels"]})
    converted = sum(path["converted"] for path in paths)
    nonconverted = sum(not path["converted"] for path in paths)
    summary = {
        "path_count": len(paths),
        "converted_paths": converted,
        "nonconverted_paths": nonconverted,
        "baseline_conversion_probability": None,
        "no_channel_conversion_probability": None,
        "channel_count": len(channels),
        "shapley_state": "NOT_COMPUTED",
        "causal_effect": "NOT_ESTABLISHED",
    }
    result = {
        "method": "absorbing-markov-exact-shapley@1" if len(channels) <= 8 else "absorbing-markov-permutation-shapley@1",
        "state": "INSUFFICIENT",
        "summary": summary,
        "channels": [],
        "transition_probabilities": [],
        "limitations": [
            "A first-order absorbing Markov path model represents only supplied same-journey touchpoint sequences.",
            "When a channel is removed, its incoming transitions are sent to nonconversion; no replacement-channel rerouting is inferred.",
            "Markov/Shapley attribution allocates observed path probability and is not causal incrementality or verified sales lift.",
            "Repeated people and unverified path identity can overstate transition evidence.",
        ],
    }
    if not paths:
        result["reason"] = "NO_COMPLETE_KNOWN_OUTCOME_PATHS"
        return result
    if not channels:
        result["reason"] = "NO_ELIGIBLE_CHANNELS"
        summary["shapley_state"] = "NOT_APPLICABLE"
        return result
    if converted < 1 or nonconverted < 1:
        result["reason"] = "NEED_OBSERVED_CONVERSION_AND_NONCONVERSION_PATHS"
        summary["shapley_state"] = "INSUFFICIENT_OUTCOME_VARIATION"
        return result

    channel_index = {channel: index + 1 for index, channel in enumerate(channels)}
    conversion_state = len(channels) + 1
    nonconversion_state = len(channels) + 2
    transition_counts = np.zeros((len(channels) + 1, len(channels) + 3), dtype=float)
    for path in paths:
        sequence = [0, *[channel_index[channel] for channel in path["channels"]],
                    conversion_state if path["converted"] else nonconversion_state]
        for source, target in zip(sequence, sequence[1:]):
            transition_counts[source, target] += 1
    row_totals = transition_counts.sum(axis=1)
    if np.any(row_totals <= 0):
        result["reason"] = "TRANSITION_STATE_HAS_NO_OBSERVED_EXIT"
        return result
    transitions = transition_counts / row_totals[:, None]
    state_labels = ["START", *channels, "CONVERSION", "NONCONVERSION"]
    transition_rows = [
        {
            "from_state": state_labels[source],
            "to_state": state_labels[target],
            "probability": float(transitions[source, target]),
            "transition_count": int(transition_counts[source, target]),
        }
        for source in range(len(channels) + 1)
        for target in range(len(channels) + 3)
        if transition_counts[source, target] > 0
    ]

    edges = [
        (int(source), int(target), float(transitions[source, target]))
        for source, target in zip(*np.nonzero(transition_counts))
    ]
    probability_cache: dict[int, float] = {}

    def conversion_probability(mask: int) -> float:
        _check_cancelled(is_cancelled)
        cached = probability_cache.get(mask)
        if cached is not None:
            return cached
        sources: list[int] = []
        targets: list[int] = []
        probabilities: list[float] = []
        for source, target, probability in edges:
            if 1 <= target <= len(channels) and not mask & (1 << (target - 1)):
                target = nonconversion_state
            sources.append(source)
            targets.append(target)
            probabilities.append(probability)
        modified = sparse.csc_matrix(
            (probabilities, (sources, targets)),
            shape=(len(channels) + 1, len(channels) + 3),
        )
        transient = modified[:, : len(channels) + 1]
        absorb = np.asarray(modified[:, conversion_state].toarray()).reshape(-1)
        system = sparse.eye(len(channels) + 1, format="csc") - transient
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", MatrixRankWarning)
                values = np.asarray(spsolve(system, absorb), dtype=float)
        if any(issubclass(item.category, MatrixRankWarning) for item in caught):
            raise np.linalg.LinAlgError("NONABSORBING_OR_SINGULAR_EMPIRICAL_TRANSITION_MATRIX")
        if not np.isfinite(values).all() or np.any(values < -1e-8) or np.any(values > 1 + 1e-8):
            raise np.linalg.LinAlgError("Markov absorption probability is outside [0,1]")
        result_value = float(min(1.0, max(0.0, values[0])))
        probability_cache[mask] = result_value
        return result_value

    try:
        baseline = conversion_probability((1 << len(channels)) - 1)
        no_channel = conversion_probability(0)
    except np.linalg.LinAlgError:
        result["reason"] = "NONABSORBING_OR_SINGULAR_EMPIRICAL_TRANSITION_MATRIX"
        return result

    summary["baseline_conversion_probability"] = baseline
    summary["no_channel_conversion_probability"] = no_channel
    result["transition_probabilities"] = transition_rows
    if baseline <= 0:
        result.update(state="NO_DATA", reason="NO_MODELLED_CONVERSION_PROBABILITY")
        summary["shapley_state"] = "NOT_IDENTIFIABLE"
        return result

    import math

    monte_carlo_samples: list[list[float]] | None = None
    if len(channels) <= 8:
        coalition_values = {
            mask: conversion_probability(mask)
            for mask in range(1 << len(channels))
        }
        shapley_values = []
        for channel_index_value in range(len(channels)):
            _check_cancelled(is_cancelled)
            score = 0.0
            for mask in range(1 << len(channels)):
                if mask & (1 << channel_index_value):
                    continue
                coalition_size = mask.bit_count()
                weight = (
                    math.factorial(coalition_size)
                    * math.factorial(len(channels) - coalition_size - 1)
                    / math.factorial(len(channels))
                )
                score += weight * (
                    coalition_values[mask | (1 << channel_index_value)] - coalition_values[mask]
                )
            shapley_values.append(score)
        shapley_state = "EXACT_WITH_EFFICIENCY_CHECK"
        shapley_method = "absorbing-markov-exact-shapley@1"
    else:
        channel_count = len(channels)
        permutation_budget = min(256, max(16, 8192 // channel_count))
        canonical_paths = sorted(
            (tuple(path["channels"]), bool(path["converted"])) for path in paths
        )
        seed_text = json.dumps(canonical_paths, ensure_ascii=False, separators=(",", ":"))
        rng = random.Random(int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big"))
        monte_carlo_samples = [[] for _ in channels]
        order = list(range(channel_count))
        permutation_count = 0

        def observe_order(permutation: list[int]) -> None:
            nonlocal permutation_count
            _check_cancelled(is_cancelled)
            mask = 0
            previous_value = no_channel
            for index in permutation:
                next_mask = mask | (1 << index)
                next_value = conversion_probability(next_mask)
                monte_carlo_samples[index].append(next_value - previous_value)
                mask = next_mask
                previous_value = next_value
            permutation_count += 1

        if permutation_budget >= channel_count:
            for _ in range(permutation_budget // channel_count):
                _check_cancelled(is_cancelled)
                rng.shuffle(order)
                for offset in range(channel_count):
                    observe_order(order[offset:] + order[:offset])
        else:
            for _ in range(permutation_budget):
                rng.shuffle(order)
                observe_order(order)
        shapley_values = [math.fsum(values) / len(values) for values in monte_carlo_samples]
        summary["shapley_permutation_count"] = permutation_count
        summary["shapley_computation_interval_method"] = "normal-approximation-mean-plus-or-minus-1.96-standard-error"
        shapley_state = "PERMUTATION_APPROXIMATION_WITH_EFFICIENCY_CHECK"
        shapley_method = "absorbing-markov-permutation-shapley@1"
        result["limitations"].append(
            "For this channel count, Shapley values are estimated from reproducible channel-order permutations; their intervals measure numerical Monte Carlo error only, not population or journey-sampling uncertainty."
        )
    efficiency_gap = sum(shapley_values) - (baseline - no_channel)
    if abs(efficiency_gap) > 1e-8:
        raise RuntimeError("Markov/Shapley efficiency check failed")
    channel_rows = []
    all_channels = (1 << len(channels)) - 1
    for index, channel in enumerate(channels):
        _check_cancelled(is_cancelled)
        without = conversion_probability(all_channels ^ (1 << index))
        contribution = shapley_values[index]
        row = {
            "channel": channel,
            "probability_without_channel": without,
            "removal_effect": baseline - without,
            "relative_removal_effect": (baseline - without) / baseline,
            "shapley_contribution": contribution,
            "shapley_share_of_baseline": contribution / baseline,
        }
        if monte_carlo_samples is not None:
            samples = monte_carlo_samples[index]
            standard_error = (
                stdev(samples) / math.sqrt(len(samples))
                if len(samples) > 1 else None
            )
            row["shapley_monte_carlo_standard_error"] = standard_error
            row["shapley_monte_carlo_approximation_interval_95"] = (
                [contribution - 1.96 * standard_error, contribution + 1.96 * standard_error]
                if standard_error is not None else None
            )
        channel_rows.append(row)
    result["method"] = shapley_method
    summary["shapley_state"] = shapley_state
    summary["shapley_efficiency_gap"] = efficiency_gap
    result.update(state="READY", channels=channel_rows)
    return result


def _attribution_analysis(inputs: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    model = _text(inputs.get("model"), "model")
    if model not in {"first_touch", "last_touch", "linear", "markov_shapley"}:
        raise _fail("model", "must be first_touch, last_touch, linear, or markov_shapley")
    lookback_days: float | None = None
    if inputs.get("lookback_days") is not None:
        lookback_days = _number(inputs["lookback_days"], "lookback_days", minimum=0, strict_minimum=True)
        if lookback_days > 365000:
            raise _fail("lookback_days", "exceeds supported timestamp arithmetic range")
    journeys = _sequence(inputs.get("journeys"), "journeys")
    orphan_touchpoints = 0
    if "touchpoints" in inputs:
        raw_touchpoints = _sequence(inputs.get("touchpoints"), "touchpoints")
        journeys_by_id = {}
        for index, raw_journey in enumerate(journeys):
            _check_cancelled(is_cancelled)
            path = f"journeys[{index}]"
            journey = _mapping(raw_journey, path)
            identity = _text(journey.get("journey_id"), f"{path}.journey_id")
            if identity in journeys_by_id:
                raise _fail(f"{path}.journey_id", "journey identifiers must be unique")
            journeys_by_id[identity] = {**journey, "touches": []}
        for index, raw_touch in enumerate(raw_touchpoints):
            _check_cancelled(is_cancelled)
            path = f"touchpoints[{index}]"
            touch = _mapping(raw_touch, path)
            journey_id = _text(touch.get("journey_id"), f"{path}.journey_id")
            if journey_id not in journeys_by_id:
                orphan_touchpoints += 1
                continue
            journeys_by_id[journey_id]["touches"].append({
                "channel": touch.get("channel"),
                "touchpoint_id": touch.get("touchpoint_id"),
                "occurred_at": touch.get("occurred_at"),
                "source": touch.get("source", {}),
            })
        journeys = list(journeys_by_id.values())
    seen_journeys: set[str] = set()
    person_journeys: dict[str, int] = {}
    channel_credits: dict[str, float] = {}
    actual_gross: dict[str, Decimal] = {}
    estimated_gross: dict[str, Decimal] = {}
    unallocated_actual: dict[str, Decimal] = {}
    unallocated_estimated: dict[str, Decimal] = {}
    known_conversions = 0
    nonconversions = 0
    unknown_conversion_status = 0
    missing_conversion_time = 0
    conversion_without_touch = 0
    eligible_journeys = 0
    raw_touch_count = 0
    eligible_touch_count = 0
    missing_touch_time = 0
    outside_window_touch = 0
    post_conversion_touch = 0
    duplicate_touch_ids = 0
    allocated_value_by_channel: dict[str, dict[str, dict[str, Decimal]]] = {"actual": {}, "estimated": {}}
    markov_paths: list[dict[str, Any]] = []
    markov_missing_end = 0
    markov_missing_event_time = 0
    markov_unknown_outcome = 0
    markov_incomplete_paths = 0
    markov_simultaneous_same_channel_collapsed = 0
    refund_records = _sequence(inputs.get("refunds", []), "refunds")
    refunds_by_id: dict[str, dict[str, Any]] = {}
    unapplied_refunds: dict[str, dict[str, Decimal]] = {}
    unmatched_refund_value: dict[str, Decimal] = {}

    for index, raw_refund in enumerate(refund_records):
        _check_cancelled(is_cancelled)
        path = f"refunds[{index}]"
        refund = _mapping(raw_refund, path)
        refund_id = _text(refund.get("refund_id"), f"{path}.refund_id")
        journey_id = _text(refund.get("journey_id"), f"{path}.journey_id")
        _number(refund.get("amount"), f"{path}.amount", minimum=0, strict_minimum=True)
        amount = Decimal(str(refund["amount"]))
        currency = _text(refund.get("currency"), f"{path}.currency")
        status = refund.get("status", "unknown")
        if status not in {"actual", "pending", "estimated", "unknown"}:
            raise _fail(f"{path}.status", "must be actual, pending, estimated, or unknown")
        occurred_at = None if refund.get("occurred_at") is None else _instant(refund["occurred_at"], f"{path}.occurred_at").isoformat()
        normalized_refund = {
            "refund_id": refund_id,
            "journey_id": journey_id,
            "amount": amount,
            "currency": currency,
            "status": status,
            "occurred_at": occurred_at,
        }
        previous = refunds_by_id.get(refund_id)
        if previous is not None and previous != normalized_refund:
            raise _fail(f"{path}.refund_id", "the same refund identifier has conflicting contents")
        refunds_by_id[refund_id] = normalized_refund

    journey_info: dict[str, dict[str, Any]] = {}

    for index, raw_journey in enumerate(journeys):
        _check_cancelled(is_cancelled)
        path = f"journeys[{index}]"
        journey = _mapping(raw_journey, path)
        journey_id = _text(journey.get("journey_id"), f"{path}.journey_id")
        if journey_id in seen_journeys:
            raise _fail(f"{path}.journey_id", "journey identifiers must be unique")
        seen_journeys.add(journey_id)
        person_key = _optional_text(journey.get("person_key"), f"{path}.person_key")
        if person_key is not None:
            person_journeys[person_key] = person_journeys.get(person_key, 0) + 1
        converted = journey.get("converted")
        conversion_status = journey.get("conversion_status")
        if conversion_status is not None:
            if conversion_status not in {"converted", "not_converted", "unknown"}:
                raise _fail(f"{path}.conversion_status", "must be converted, not_converted, or unknown after explicit source-label mapping")
            mapped_converted = (
                True if conversion_status == "converted"
                else False if conversion_status == "not_converted"
                else None
            )
            if converted is not None and converted is not mapped_converted:
                raise _fail(f"{path}.converted", "boolean conversion and mapped status disagree")
            converted = mapped_converted
        if converted is not None and type(converted) is not bool:
            raise _fail(f"{path}.converted", "must be true, false, or null for unknown")
        if converted is True:
            known_conversions += 1
        elif converted is False:
            nonconversions += 1
        else:
            unknown_conversion_status += 1
        conversion_at = None
        if journey.get("conversion_at") is not None:
            conversion_at = _instant(journey["conversion_at"], f"{path}.conversion_at")
        if converted is True and conversion_at is None:
            missing_conversion_time += 1
        observation_end = None
        if journey.get("observation_end") is not None:
            observation_end = _instant(journey["observation_end"], f"{path}.observation_end")
        markov_end = conversion_at if converted is True else observation_end if converted is False else None

        conversion_value = journey.get("conversion_value")
        value_record = None
        if conversion_value is not None:
            raw_value = _mapping(conversion_value, f"{path}.conversion_value")
            _number(raw_value.get("amount"), f"{path}.conversion_value.amount", minimum=0)
            amount = Decimal(str(raw_value["amount"]))
            currency = _text(raw_value.get("currency"), f"{path}.conversion_value.currency")
            value_status = raw_value.get("status")
            if value_status not in {"actual", "estimated"}:
                raise _fail(f"{path}.conversion_value.status", "must be actual or estimated")
            value_record = {"amount": amount, "currency": currency, "status": value_status}

        touches = _sequence(journey.get("touches"), f"{path}.touches")
        raw_touch_count += len(touches)
        eligible_touches: list[tuple[datetime, str]] = []
        markov_touches: list[tuple[datetime, str]] = []
        markov_touch_time_unknown = False
        local_touch_ids: dict[str, tuple[str, str | None]] = {}
        for touch_index, raw_touch in enumerate(touches):
            touch_path = f"{path}.touches[{touch_index}]"
            touch = _mapping(raw_touch, touch_path)
            channel = _text(touch.get("channel"), f"{touch_path}.channel")
            touch_id = _optional_text(touch.get("touchpoint_id"), f"{touch_path}.touchpoint_id")
            occurred_at = None
            if touch.get("occurred_at") is not None:
                occurred_at = _instant(touch["occurred_at"], f"{touch_path}.occurred_at")
            else:
                missing_touch_time += 1
                if model == "markov_shapley" and converted is not None:
                    markov_touch_time_unknown = True
            if touch_id is not None:
                identity = (channel, None if occurred_at is None else occurred_at.isoformat())
                prior = local_touch_ids.get(touch_id)
                if prior is not None:
                    if prior != identity:
                        raise _fail(f"{touch_path}.touchpoint_id", "one touchpoint identifier has conflicting channel or time")
                    duplicate_touch_ids += 1
                    continue
                local_touch_ids[touch_id] = identity
            if model == "markov_shapley" and converted is not None:
                if occurred_at is None:
                    continue
                if markov_end is None:
                    continue
                if occurred_at > markov_end:
                    continue
                if lookback_days is None or occurred_at < markov_end - timedelta(days=lookback_days):
                    continue
                markov_touches.append((occurred_at, channel))
            if converted is not True or conversion_at is None or occurred_at is None or lookback_days is None:
                continue
            if occurred_at > conversion_at:
                post_conversion_touch += 1
                continue
            if occurred_at < conversion_at - timedelta(days=lookback_days):
                outside_window_touch += 1
                continue
            eligible_touches.append((occurred_at, channel))

        eligible_touches.sort(key=lambda item: item[0])
        if model == "markov_shapley":
            if converted is None:
                markov_unknown_outcome += 1
            elif markov_end is None or lookback_days is None:
                markov_missing_end += 1
            elif markov_touch_time_unknown:
                markov_missing_event_time += 1
            else:
                markov_touches.sort(key=lambda item: item[0])
                ordered_touches = []
                ambiguous_time_order = False
                for touch in markov_touches:
                    if ordered_touches and touch[0] == ordered_touches[-1][0]:
                        if touch[1] != ordered_touches[-1][1]:
                            ambiguous_time_order = True
                            break
                        markov_simultaneous_same_channel_collapsed += 1
                        continue
                    ordered_touches.append(touch)
                if ambiguous_time_order:
                    markov_incomplete_paths += 1
                else:
                    markov_paths.append({
                        "id": journey_id,
                        "channels": [channel for _, channel in ordered_touches],
                        "converted": converted,
                    })
        if converted is True and eligible_touches:
            eligible_journeys += 1
            eligible_touch_count += len(eligible_touches)
            if model == "first_touch":
                weights = [(eligible_touches[0][1], 1.0)]
            elif model == "last_touch":
                weights = [(eligible_touches[-1][1], 1.0)]
            elif model == "linear":
                each = 1.0 / len(eligible_touches)
                weights = [(channel, each) for _, channel in eligible_touches[:-1]]
                weights.append((eligible_touches[-1][1], 1.0 - sum(weight for _, weight in weights)))
            else:
                weights = []
            for channel, weight in weights:
                channel_credits[channel] = channel_credits.get(channel, 0.0) + weight
                if value_record is not None:
                    status = value_record["status"]
                    currency = value_record["currency"]
                    amount = value_record["amount"] * Decimal(str(weight))
                    allocated_value_by_channel[status].setdefault(currency, {})[channel] = (
                        allocated_value_by_channel[status].setdefault(currency, {}).get(channel, Decimal(0)) + amount
                    )
        elif converted is True:
            conversion_without_touch += 1

        if converted is True and value_record is not None:
            destination = actual_gross if value_record["status"] == "actual" else estimated_gross
            destination[value_record["currency"]] = destination.get(value_record["currency"], Decimal(0)) + value_record["amount"]
            if not eligible_touches or model == "markov_shapley":
                unallocated = unallocated_actual if value_record["status"] == "actual" else unallocated_estimated
                unallocated[value_record["currency"]] = unallocated.get(value_record["currency"], Decimal(0)) + value_record["amount"]
        journey_info[journey_id] = {
            "converted": converted,
            "value": value_record,
            "weights": weights if converted is True and eligible_touches else [],
        }

    unique_refunds = list(refunds_by_id.values())
    actual_refunds: dict[str, Decimal] = {}
    unmatched_refunds = 0
    nonactual_refunds = 0
    refunds_by_journey: dict[str, list[dict[str, Any]]] = {}
    for refund in unique_refunds:
        journey = journey_info.get(refund["journey_id"])
        if journey is None or journey["converted"] is not True or journey["value"] is None:
            unmatched_refunds += 1
            unmatched_refund_value[refund["currency"]] = unmatched_refund_value.get(refund["currency"], Decimal(0)) + refund["amount"]
            continue
        value = journey["value"]
        if value["currency"] != refund["currency"]:
            raise _fail("refunds", "refund currency must match the original conversion currency")
        if refund["status"] != "actual":
            nonactual_refunds += 1
            status_totals = unapplied_refunds.setdefault(refund["status"], {})
            status_totals[refund["currency"]] = status_totals.get(refund["currency"], Decimal(0)) + refund["amount"]
            continue
        if value["status"] != "actual":
            unmatched_refunds += 1
            unmatched_refund_value[refund["currency"]] = unmatched_refund_value.get(refund["currency"], Decimal(0)) + refund["amount"]
            continue
        refunds_by_journey.setdefault(refund["journey_id"], []).append(refund)
        actual_refunds[refund["currency"]] = actual_refunds.get(refund["currency"], Decimal(0)) + refund["amount"]

    channel_refund_credits: dict[str, dict[str, Decimal]] = {}
    for journey_id, refunds in refunds_by_journey.items():
        info = journey_info[journey_id]
        original_amount = info["value"]["amount"]
        total_refund = sum((refund["amount"] for refund in refunds), Decimal(0))
        if total_refund > original_amount:
            raise _fail("refunds", "cumulative actual refunds exceed the original conversion value")
        for channel, weight in info["weights"]:
            currency = info["value"]["currency"]
            channel_refund_credits.setdefault(currency, {})[channel] = (
                channel_refund_credits.setdefault(currency, {}).get(channel, Decimal(0)) + total_refund * Decimal(str(weight))
            )

    net_actual = {
        currency: amount - actual_refunds.get(currency, Decimal(0))
        for currency, amount in actual_gross.items()
    }
    actual_value_credits = allocated_value_by_channel["actual"]
    net_actual_credits: dict[str, dict[str, Decimal]] = {}
    for currency, channels in actual_value_credits.items():
        net_actual_credits[currency] = {
            channel: amount - channel_refund_credits.get(currency, {}).get(channel, Decimal(0))
            for channel, amount in channels.items()
        }

    def money_json(value: Decimal) -> float:
        converted = float(value)
        if not math.isfinite(converted):
            raise _fail("money", "currency total exceeds the supported numeric range")
        return converted

    def currency_totals(values: dict[str, Decimal]) -> dict[str, float]:
        return {currency: money_json(amount) for currency, amount in sorted(values.items())}

    def channel_totals(values: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, float]]:
        return {
            currency: {channel: money_json(amount) for channel, amount in sorted(channels.items())}
            for currency, channels in sorted(values.items())
        }

    repeated_people = sum(count > 1 for count in person_journeys.values())
    person_count = len(person_journeys)
    limitations = list(_METHOD_LIMITATIONS["attribution_analysis"])
    if lookback_days is None:
        limitations.append("No lookback window was supplied; no touchpoint was credited because the eligible period is unknown.")
    if missing_conversion_time or missing_touch_time:
        limitations.append("Touchpoints with missing conversion or event timestamps remain unallocated; collection time is not substituted for event time.")
    if duplicate_touch_ids:
        limitations.append("Repeated identical touchpoint IDs within a journey were counted once.")
    if person_count == 0:
        limitations.append("Person-level identity is unavailable, so repeat journeys and cross-device paths cannot be deduplicated.")
    elif repeated_people:
        limitations.append("Some supplied people have multiple journeys; credits are descriptive and no independent-sample interval is claimed.")
    if unmatched_refunds:
        limitations.append("Some refunds could not be matched to an actual conversion value and were preserved without changing net totals.")
    if nonactual_refunds:
        limitations.append("Pending, estimated, or unknown refunds are shown as unresolved and are not subtracted from actual totals.")

    journey_coverage = {
        "journeys": len(journeys),
        "known_conversions": known_conversions,
        "known_nonconversions": nonconversions,
        "unknown_conversion_status": unknown_conversion_status,
        "journeys_with_eligible_touch": eligible_journeys,
        "conversions_without_eligible_touch": conversion_without_touch,
        "missing_conversion_time": missing_conversion_time,
        "raw_touchpoints": raw_touch_count,
        "eligible_touchpoints": eligible_touch_count,
        "missing_touch_time": missing_touch_time,
        "touchpoints_outside_lookback": outside_window_touch,
        "touchpoints_after_conversion": post_conversion_touch,
        "duplicate_touchpoint_ids_collapsed": duplicate_touch_ids,
        "touchpoints_without_matching_journey": orphan_touchpoints,
        "person_keys": person_count if person_count else None,
        "people_with_multiple_journeys": repeated_people if person_count else None,
        "refund_records_received": len(refund_records),
        "refund_ids_after_exact_deduplication": len(unique_refunds),
        "duplicate_refund_notifications_collapsed": len(refund_records) - len(unique_refunds),
        "unmatched_refunds": unmatched_refunds,
        "nonactual_refunds_not_applied": nonactual_refunds,
        "markov_complete_paths": len(markov_paths) if model == "markov_shapley" else None,
        "markov_missing_nonconversion_observation_end": markov_missing_end if model == "markov_shapley" else None,
        "markov_paths_with_missing_touch_time": markov_missing_event_time if model == "markov_shapley" else None,
        "markov_paths_with_ambiguous_same_time_channel_order": markov_incomplete_paths if model == "markov_shapley" else None,
        "markov_same_channel_same_time_events_collapsed_for_path_model": markov_simultaneous_same_channel_collapsed if model == "markov_shapley" else None,
    }
    credits = {
        "method": model,
        "lookback_days": lookback_days,
        "conversion_credit_by_channel": dict(sorted(channel_credits.items())),
        "allocated_conversion_credit_total": sum(channel_credits.values()),
        "gross_conversion_value_actual_by_currency": currency_totals(actual_gross),
        "gross_conversion_value_estimated_by_currency": currency_totals(estimated_gross),
        "unallocated_conversion_value_actual_by_currency": currency_totals(unallocated_actual),
        "unallocated_conversion_value_estimated_by_currency": currency_totals(unallocated_estimated),
        "actual_refunds_by_currency": currency_totals(actual_refunds),
        "net_actual_conversion_value_by_currency": currency_totals(net_actual),
        "estimated_refunds_not_subtracted": {
            "count": nonactual_refunds,
            "reason": "refunds without actual status are not mixed into actual net value",
        },
        "unapplied_refund_value_by_status_and_currency": {
            status: currency_totals(values) for status, values in sorted(unapplied_refunds.items())
        },
        "unmatched_refund_value_by_currency": currency_totals(unmatched_refund_value),
        "allocated_value_actual_by_currency_and_channel": channel_totals(actual_value_credits),
        "allocated_value_estimated_by_currency_and_channel": channel_totals(allocated_value_by_channel["estimated"]),
        "allocated_refund_credit_by_currency_and_channel": channel_totals(channel_refund_credits),
        "net_actual_value_credit_by_currency_and_channel": channel_totals(net_actual_credits),
    }
    sections = [
        _section(
            "attribution_journey_coverage",
            "旅程、窗口与去重覆盖",
            journey_coverage,
            "每条旅程按输入的 journey_id 独立处理，不会把不同人的帖子拼成路径。仅按转换事件时间、明示回看窗口和去重后的触点计算；缺时间、未知转化或无合格触点保持未分配。",
        ),
    ]
    if model == "markov_shapley":
        markov_result = _markov_shapley(markov_paths, is_cancelled=is_cancelled)
        limitations.extend(markov_result["limitations"])
        incomplete_path_count = markov_missing_end + markov_missing_event_time + markov_incomplete_paths + markov_unknown_outcome
        markov_result["coverage"] = {
            "complete_paths_used": len(markov_paths),
            "missing_endpoint_or_lookback": markov_missing_end,
            "paths_with_missing_touch_time": markov_missing_event_time,
            "paths_with_ambiguous_same_time_channel_order": markov_incomplete_paths,
            "unknown_conversion_status_excluded": markov_unknown_outcome,
            "same_channel_same_time_events_collapsed": markov_simultaneous_same_channel_collapsed,
            "incomplete_path_count": incomplete_path_count,
        }
        if incomplete_path_count and markov_result["state"] in {
            "READY", "MARKOV_READY_SHAPLEY_UNAVAILABLE",
        }:
            markov_result["state"] = (
                "PARTIAL_SHAPLEY_UNAVAILABLE"
                if markov_result["state"] == "MARKOV_READY_SHAPLEY_UNAVAILABLE"
                else "PARTIAL"
            )
            markov_result["reason"] = "SOME_JOURNEY_PATHS_LACK_COMPLETE_OUTCOME_OR_EVENT_TIME"
        if markov_missing_end:
            limitations.append("Markov path attribution requires a conversion time for converted journeys and an explicit observation_end for nonconverted journeys; incomplete windows were excluded.")
        if markov_missing_event_time or markov_incomplete_paths:
            limitations.append("Touchpoint order requires event timestamps; missing times and simultaneous different-channel ties were not assigned an invented order.")
        if markov_simultaneous_same_channel_collapsed:
            limitations.append("Same-channel touchpoints at the exact same recorded instant were collapsed only inside the channel-state path model; source rows remain preserved.")
        sections.append(
            _section(
                "attribution_markov_shapley",
                "Markov路径与Shapley分摊",
                markov_result,
                "使用同一旅程的有序触点与已知转化/未转化结果估计吸收路径转化概率。移除渠道时将流入该渠道的概率送到未转化状态；小规模渠道集合给精确分摊，较大集合以可复现的渠道顺序抽样估算，并列计算误差近似区间（不是人群置信区间）。结果是观察路径分摊，不是渠道替代行为或因果增量。",
            ),
        )
        interpretation = {
            "claim_type": "descriptive_markov_path_attribution",
            "incrementality_estimate": None,
            "commission_or_settlement": None,
            "cross_platform_order_identity_verified": False,
        }
        sections.append(
            _section(
                "attribution_interpretation",
                "解释边界",
                interpretation,
                "模型只描述输入的同一旅程路径。跨平台同一订单映射、独立人群和触点来源仍按原始证据核验；输出不是渠道增量、销售因果、佣金或结算依据。",
            ),
        )
    else:
        sections.extend([
            _section(
                "attribution_descriptive_credits",
                "描述性归因分配",
                credits,
                "渠道信用按所选首触、末触或线性规则在已观察转化间守恒。金额保留实绩/估计和原币种；只扣已匹配、实际、未重复且不超过原成交金额的退款。",
            ),
            _section(
                "attribution_interpretation",
                "解释边界",
                {
                    "claim_type": "descriptive_path_credit",
                    "incrementality_estimate": None,
                    "commission_or_settlement": None,
                    "cross_platform_order_identity_verified": False,
                },
                "这些份额描述分配规则如何分摊已观察路径。它们不是渠道增量、销售因果、平台间可相加的百分比或结算依据。",
            ),
        ])
    return _response("内容路径归因分析", sections, limitations)


def _period_position(value: str, frequency: str, path: str) -> int:
    if frequency == "day":
        day = _text(value, path)
        try:
            parsed = datetime.fromisoformat(day)
            if parsed.time() != datetime.min.time() or parsed.tzinfo is not None:
                raise ValueError()
            return parsed.date().toordinal()
        except ValueError as error:
            raise _fail(path, "daily periods must be YYYY-MM-DD dates") from error
    if frequency == "month":
        match = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])", value) if isinstance(value, str) else None
        if match is None:
            raise _fail(path, "monthly periods must be YYYY-MM")
        return int(match[1]) * 12 + int(match[2]) - 1
    if frequency == "quarter":
        match = re.fullmatch(r"(20\d{2})-Q([1-4])", value) if isinstance(value, str) else None
        if match is None:
            raise _fail(path, "quarterly periods must be YYYY-Q1 through YYYY-Q4")
        return int(match[1]) * 4 + int(match[2]) - 1
    match = re.fullmatch(r"20\d{2}", value) if isinstance(value, str) else None
    if match is None:
        raise _fail(path, "yearly periods must be YYYY")
    return int(value)


def _synthetic_control_analysis(inputs: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    """Outcome-only simplex synthetic control with held-out prefit and in-space placebos."""
    import numpy as np
    from scipy.optimize import minimize
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)

    allowed = {
        "estimator", "frequency", "pre_periods", "post_periods", "observations",
        "max_pre_rmse", "design", "metric_unit", "unit_of_analysis",
    }
    if set(inputs) - allowed:
        raise _fail("inputs", "contains unsupported synthetic-control fields")
    frequency = _text(inputs.get("frequency"), "frequency")
    if frequency not in {"day", "month", "quarter", "year"}:
        raise _fail("frequency", "must be day, month, quarter, or year")
    pre_periods = [_text(item, f"pre_periods[{index}]") for index, item in enumerate(_sequence(inputs.get("pre_periods"), "pre_periods"))]
    post_periods = [_text(item, f"post_periods[{index}]") for index, item in enumerate(_sequence(inputs.get("post_periods"), "post_periods"))]
    if not pre_periods or not post_periods or len(set(pre_periods)) != len(pre_periods) or len(set(post_periods)) != len(post_periods) or set(pre_periods) & set(post_periods):
        raise _fail("pre_periods/post_periods", "must be nonempty, unique, and disjoint")
    positions = [
        _period_position(period, frequency, f"periods[{index}]")
        for index, period in enumerate(pre_periods + post_periods)
    ]
    if any(right - left != 1 for left, right in zip(positions, positions[1:])):
        raise _fail("pre_periods/post_periods", "must form one complete regular time grid")
    periods = pre_periods + post_periods
    max_pre_rmse = _number(inputs.get("max_pre_rmse"), "max_pre_rmse", minimum=0, strict_minimum=True)
    design = _mapping(inputs.get("design"), "design")
    required_design = {"no_anticipation", "no_spillovers", "comparable_measurement", "selection_protocol"}
    if set(design) != required_design:
        raise _fail("design", "must state no-anticipation, spillover, measurement and donor-selection conditions")
    for field in ("no_anticipation", "no_spillovers", "comparable_measurement"):
        if type(design[field]) is not bool:
            raise _fail(f"design.{field}", "must be an explicit true or false assessment")
    selection_protocol = _text(design["selection_protocol"], "design.selection_protocol")
    declarations = ("no_anticipation", "no_spillovers", "comparable_measurement")
    raw_rows = _sequence(inputs.get("observations"), "observations")
    if not raw_rows:
        raise _fail("observations", "must contain the treated unit and donor-pool observations")
    units: dict[str, dict[str, Any]] = {}
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    out_of_plan = 0
    for index, raw in enumerate(raw_rows):
        _check_cancelled(is_cancelled)
        path = f"observations[{index}]"
        row = _mapping(raw, path)
        if set(row) - {"unit_id", "unit_label", "role", "period", "value", "untreated_declared", "source"}:
            raise _fail(path, "contains unsupported synthetic-control observation fields")
        unit_id = _text(row.get("unit_id"), f"{path}.unit_id")
        label = _optional_text(row.get("unit_label"), f"{path}.unit_label") or unit_id
        role = _text(row.get("role"), f"{path}.role")
        if role not in {"treated", "donor"}:
            raise _fail(f"{path}.role", "must be treated or donor")
        untreated = row.get("untreated_declared")
        if role == "donor" and type(untreated) is not bool:
            raise _fail(f"{path}.untreated_declared", "each donor needs an explicit exposure declaration")
        if role == "donor" and untreated is not True:
            raise _fail(f"{path}.untreated_declared", "a donor must be explicitly declared untreated")
        if role == "treated" and untreated not in {None, False}:
            raise _fail(f"{path}.untreated_declared", "the treated unit cannot be declared untreated")
        period = _text(row.get("period"), f"{path}.period")
        if period not in set(periods):
            out_of_plan += 1
            continue
        value = _number(row.get("value"), f"{path}.value")
        source = _mapping(row.get("source", {}), f"{path}.source")
        unit = units.setdefault(unit_id, {"id": unit_id, "label": label, "role": role, "untreated_declared": untreated})
        if unit["label"] != label or unit["role"] != role or unit["untreated_declared"] != untreated:
            raise _fail(f"{path}.unit_id", "unit label, treatment role and exposure declaration must remain stable")
        key = (unit_id, period)
        if key in observed:
            raise _fail(path, "duplicate unit-period observations are not silently averaged")
        observed[key] = {"value": value, "source": source}
    treated_units = [unit for unit in units.values() if unit["role"] == "treated"]
    donor_units = [unit for unit in units.values() if unit["role"] == "donor"]
    diagnostics = {
        "unit_count": len(units),
        "treated_unit_count": len(treated_units),
        "declared_donor_count": len(donor_units),
        "observation_rows": len(raw_rows),
        "out_of_plan_rows": out_of_plan,
        "missing_unit_period_cells": sum((unit["id"], period) not in observed for unit in units.values() for period in periods),
        "untreated_declarations": {unit["id"]: unit["untreated_declared"] for unit in donor_units},
        "design_assessment": {field: design[field] for field in declarations},
        "selection_protocol": selection_protocol,
        "source_linked_rows": sum(bool(item["source"]) for item in observed.values()),
    }
    empty_fit = {
        "state": "INSUFFICIENT",
        "reason": None,
        "training_n": None,
        "validation_n": None,
        "training_rmse": None,
        "validation_rmse": None,
        "pre_rmse": None,
        "post_rmse": None,
        "post_pre_rmse_ratio": None,
        "ratio_state": "NOT_ESTIMATED",
        "mean_post_gap": None,
        "donor_weights": [],
        "post_gaps": [],
    }
    result = {
        "method": "outcome-only-simplex-synthetic-control@1",
        "state": "INSUFFICIENT",
        "frequency": frequency,
        "periods": periods,
        "pre_n": len(pre_periods),
        "post_n": len(post_periods),
        "unit_of_analysis": _optional_text(inputs.get("unit_of_analysis"), "unit_of_analysis"),
        "metric_unit": _optional_text(inputs.get("metric_unit"), "metric_unit"),
        "max_pre_rmse": max_pre_rmse,
        "design_verification": "USER_DECLARED_NOT_INDEPENDENTLY_VERIFIED",
        "fit": empty_fit,
        "placebos": [],
        "placebo_comparison": {"eligible_n": 0, "p_value": None, "reason": "OBSERVATIONAL_PLACEBOS_DO_NOT_ESTABLISH_RANDOMIZED_ASSIGNMENT"},
        "diagnostics": diagnostics,
        "source_observations": [
            {
                "unit_id": unit["id"],
                "unit_label": unit["label"],
                "role": unit["role"],
                "periods": [
                    {"period": period, **observed[(unit["id"], period)]}
                    for period in periods
                    if (unit["id"], period) in observed
                ],
            }
            for unit in units.values()
        ],
        "causal_claim": "NOT_ESTABLISHED",
        "limitations": [
            "Outcome-only nonnegative simplex weights sum to one; this is not a full covariate-predictor-weight selection procedure.",
            "Treated pre-outcomes train donor weights; a disjoint pre-intervention holdout checks fit before refitting on all pre-periods.",
            "No anticipation, no spillovers, comparable measurement and donor nonexposure are caller declarations, not independently proven facts.",
            "Post-period gaps are descriptive absent a credible untreated counterfactual; donor placebos are sensitivity diagnostics, not randomized p-values.",
            "The error tolerance is supplied in the metric's outcome units and is not automatically estimated from the post-treatment result.",
        ],
    }
    if out_of_plan:
        result["reason"] = "OBSERVATIONS_OUTSIDE_DECLARED_PERIODS"
        return _synthetic_control_response(result, units, observed)
    if len(treated_units) != 1:
        result["reason"] = "REQUIRES_EXACTLY_ONE_TREATED_UNIT"
        return _synthetic_control_response(result, units, observed)
    if len(donor_units) < 2:
        result["reason"] = "NEED_AT_LEAST_TWO_DECLARED_DONOR_UNITS"
        return _synthetic_control_response(result, units, observed)
    missing_cells = diagnostics["missing_unit_period_cells"]
    if missing_cells:
        result["reason"] = "UNBALANCED_PANEL_HAS_MISSING_UNIT_PERIODS"
        return _synthetic_control_response(result, units, observed)
    if len(pre_periods) < 12 or len(post_periods) < 2:
        result["reason"] = "NEED_12_PRE_AND_2_POST_PERIODS"
        return _synthetic_control_response(result, units, observed)
    if not all(design[field] for field in declarations):
        result["reason"] = "DECLARED_DESIGN_ASSUMPTIONS_NOT_MET"
        return _synthetic_control_response(result, units, observed)
    train_n = len(pre_periods) - max(4, len(pre_periods) // 4)
    panel = np.asarray([
        [observed[(unit["id"], period)]["value"] for period in periods]
        for unit in [treated_units[0], *donor_units]
    ], dtype=float).T
    tolerance = float(max_pre_rmse)

    def fit(target, pool, stop):
        _check_cancelled(is_cancelled)
        y, x = target[:stop], pool[:stop]
        scale = max(float(np.std(y)), float(np.std(x)), 1.0)
        y, x = y / scale, x / scale
        difference = x[:, :-1] - x[:, -1:]
        singular = np.linalg.svd(difference, compute_uv=False)
        if len(singular) and (
            len(singular) < x.shape[1] - 1
            or singular[-1] <= 1e-8 * max(singular[0], 1e-12)
        ):
            return None, "DONOR_WEIGHTS_NOT_IDENTIFIABLE"
        if x.shape[1] == 1:
            return np.ones(1), None
        objective = lambda weights: float(np.mean((x @ weights - y) ** 2))
        gradient = lambda weights: 2 * x.T @ (x @ weights - y) / len(y)

        def optimizer_callback(_weights):
            _check_cancelled(is_cancelled)

        fitted = minimize(
            objective,
            np.full(x.shape[1], 1 / x.shape[1]),
            jac=gradient,
            method="SLSQP",
            bounds=[(0.0, 1.0)] * x.shape[1],
            constraints={"type": "eq", "fun": lambda weights: float(weights.sum() - 1),
                         "jac": lambda weights: np.ones_like(weights)},
            callback=optimizer_callback,
            options={"ftol": 1e-12, "maxiter": 2000},
        )
        _check_cancelled(is_cancelled)
        weights = fitted.x
        if not fitted.success or abs(weights.sum() - 1) > 1e-7 or np.min(weights) < -1e-8:
            return None, "SIMPLEX_OPTIMIZER_NOT_CONVERGED"
        weights = np.maximum(weights, 0.0)
        weights /= weights.sum()
        gradient_value = gradient(weights)
        active = weights > 1e-7
        lagrange = float(np.mean(gradient_value[active]))
        residual = max(
            float(np.max(np.abs(gradient_value[active] - lagrange))),
            float(np.max(np.maximum(lagrange - gradient_value[~active], 0))) if (~active).any() else 0.0,
        )
        if residual > 1e-5:
            return None, "SIMPLEX_KKT_CHECK_FAILED"
        return weights, None

    def evaluate(target, pool):
        _check_cancelled(is_cancelled)
        training_weights, error = fit(target, pool, train_n)
        if error:
            return {"state": "INSUFFICIENT", "reason": error}
        validation = pool[train_n:len(pre_periods)] @ training_weights
        training_rmse = float(np.sqrt(np.mean((target[:train_n] - pool[:train_n] @ training_weights) ** 2)))
        validation_rmse = float(np.sqrt(np.mean((target[train_n:len(pre_periods)] - validation) ** 2)))
        record = {
            "training_n": train_n,
            "validation_n": len(pre_periods) - train_n,
            "training_rmse": training_rmse,
            "validation_rmse": validation_rmse,
            "max_pre_rmse": tolerance,
        }
        if max(training_rmse, validation_rmse) > tolerance:
            return {**record, "state": "INSUFFICIENT", "reason": "PRE_FIT_OR_HOLDOUT_ERROR_EXCEEDS_DECLARED_TOLERANCE"}
        weights, error = fit(target, pool, len(pre_periods))
        if error:
            return {**record, "state": "INSUFFICIENT", "reason": error}
        synthetic = pool @ weights
        gaps = target - synthetic
        pre_rmse = float(np.sqrt(np.mean(gaps[:len(pre_periods)] ** 2)))
        post_rmse = float(np.sqrt(np.mean(gaps[len(pre_periods):] ** 2)))
        denominator = max(1.0, float(np.max(np.abs(target)))) * 1e-10
        ratio = post_rmse / pre_rmse if pre_rmse > denominator else None
        return {
            **record,
            "state": "READY",
            "weights": [
                {"unit_id": donor["id"], "unit_label": donor["label"], "weight": float(weight)}
                for donor, weight in zip(donor_units, weights)
            ],
            "synthetic_outcome": synthetic.tolist(),
            "pre_rmse": pre_rmse,
            "post_rmse": post_rmse,
            "post_pre_rmse_ratio": ratio,
            "ratio_state": "FINITE" if ratio is not None else "PRE_ERROR_NEAR_ZERO_RATIO_UNDEFINED",
            "post_observed_minus_synthetic": gaps[len(pre_periods):].tolist(),
            "mean_post_gap": float(np.mean(gaps[len(pre_periods):])),
        }

    fit_result = evaluate(panel[:, 0], panel[:, 1:])
    result["fit"] = fit_result
    if fit_result["state"] != "READY":
        result["reason"] = fit_result["reason"]
        return _synthetic_control_response(result, units, observed)
    placebos = []
    for donor_index, donor in enumerate(donor_units, start=1):
        _check_cancelled(is_cancelled)
        pool_indices = [index for index in range(1, len(units)) if index != donor_index]
        placebo = evaluate(panel[:, donor_index], panel[:, pool_indices])
        placebo_ratio = placebo.get("post_pre_rmse_ratio")
        treated_ratio = fit_result.get("post_pre_rmse_ratio")
        placebos.append({
            "unit_id": donor["id"],
            "unit_label": donor["label"],
            **placebo,
            "comparison_eligible": placebo.get("state") == "READY" and placebo_ratio is not None and treated_ratio is not None,
        })
    eligible = [row for row in placebos if row["comparison_eligible"]]
    result.update(
        state="READY",
        donor_weights=fit_result["weights"],
        placebos=placebos,
        placebo_comparison={
            "eligible_n": len(eligible),
            "treated_post_pre_rmse_ratio": fit_result.get("post_pre_rmse_ratio"),
            "placebo_ratios_at_or_above_treated": sum(
                row["post_pre_rmse_ratio"] >= fit_result["post_pre_rmse_ratio"] for row in eligible
            ) if fit_result.get("post_pre_rmse_ratio") is not None else None,
            "rank": 1 + sum(
                row["post_pre_rmse_ratio"] >= fit_result["post_pre_rmse_ratio"] for row in eligible
            ) if fit_result.get("post_pre_rmse_ratio") is not None else None,
            "p_value": None,
            "reason": "IN_SPACE_PLACEBOS_ARE_SENSITIVITY_DIAGNOSTICS_NOT_RANDOMIZED_P_VALUES",
        },
    )
    return _synthetic_control_response(result, units, observed)


def _synthetic_control_response(result: dict[str, Any], units: dict[str, dict], observed: dict[tuple[str, str], dict]) -> dict[str, Any]:
    fit = result.get("fit", {})
    donors = fit.get("weights", [])
    treated_unit = next((unit for unit in units.values() if unit["role"] == "treated"), None)
    weight_rows = [
        [item.get("unit_label"), item.get("weight")]
        for item in donors
    ]
    gap_rows = []
    if treated_unit is not None:
        for index, period in enumerate(result["periods"]):
            key = (treated_unit["id"], period)
            if key not in observed:
                continue
            source = observed[key]["source"]
            source_label = "; ".join(
                str(source.get(key)) for key in ("filename", "sheet", "row") if source.get(key) is not None
            ) or "来源定位保留在结构化结果"
            synthetic = fit.get("synthetic_outcome", [None] * len(result["periods"]))[index]
            gap = (
                fit.get("post_observed_minus_synthetic", [])[index - result["pre_n"]]
                if index >= result["pre_n"] and fit.get("post_observed_minus_synthetic") else None
            )
            gap_rows.append([period, observed[key]["value"], synthetic, gap, source_label])
    placebo_rows = [
        [row.get("unit_label"), row.get("state"), row.get("pre_rmse"), row.get("post_rmse"), row.get("post_pre_rmse_ratio"), row.get("comparison_eligible")]
        for row in result.get("placebos", [])
    ]
    sections = [
        _section(
            "incrementality_synthetic_control",
            "合成控制拟合与处理后差异",
            {**result, "body_data": None},
            "单一处理单位按处理前结果变量拟合非负且和为一的供体权重；内部留出的处理前时段先核拟合，再在全部处理前重拟合。处理后差异和误差比为观察结果，不构成因果识别证明。",
        ),
        _section(
            "incrementality_synthetic_control_placebos",
            "供体安慰剂与敏感性",
            {"placebos": result.get("placebos", []), "comparison": result.get("placebo_comparison"), "diagnostics": result.get("diagnostics")},
            "把同一程序轮流用于供体单位，显示其处理前拟合是否合格以及处理后误差比。安慰剂筛选与排序仅为敏感性诊断，不是随机分配p值。",
        ),
    ]
    sections[0]["body"] += (
        "\n\n模型摘要：\n\n" + _table(
            ["状态", "原因", "独立单位", "前期数", "后期数", "训练RMSE", "留出RMSE", "完整前期RMSE", "后期RMSE", "后/前RMSE比", "平均处理后差"],
            [[
                result.get("state"), result.get("reason"), result.get("unit_of_analysis"),
                result.get("pre_n"), result.get("post_n"), fit.get("training_rmse"),
                fit.get("validation_rmse"), fit.get("pre_rmse"), fit.get("post_rmse"),
                fit.get("post_pre_rmse_ratio") if fit.get("ratio_state") == "FINITE" else "未知：前期误差近零",
                fit.get("mean_post_gap"),
            ]],
        ) + "\n\n供体权重：\n\n" + _table(["供体单位", "权重"], weight_rows)
        + "\n\n逐期结果：\n\n" + _table(["期间", "处理单位实测", "合成对照", "实测减合成", "处理单位来源"], gap_rows)
    )
    sections[1]["body"] += "\n\n安慰剂结果：\n\n" + _table(
        ["供体单位", "状态", "前期RMSE", "后期RMSE", "后/前RMSE比", "可比安慰剂"], placebo_rows
    )
    limitations = list(result["limitations"])
    if result.get("state") != "READY":
        limitations.append("当前输入未达到合成控制适用条件；未生成处理后增量估计。")
    return _response("活动增量与合成控制评价", sections, limitations)


def _incrementality_analysis(inputs: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    estimator = inputs.get("estimator", "did")
    if estimator not in {"did", "synthetic_control"}:
        raise _fail("estimator", "must be did or synthetic_control")
    if estimator == "synthetic_control":
        return _synthetic_control_analysis(inputs, is_cancelled=is_cancelled)
    pre_periods = [_text(value, f"pre_periods[{index}]") for index, value in enumerate(_sequence(inputs.get("pre_periods"), "pre_periods"))]
    post_periods = [_text(value, f"post_periods[{index}]") for index, value in enumerate(_sequence(inputs.get("post_periods"), "post_periods"))]
    if not pre_periods or not post_periods:
        raise _fail("pre_periods/post_periods", "each must contain at least one period")
    if len(set(pre_periods)) != len(pre_periods) or len(set(post_periods)) != len(post_periods):
        raise _fail("pre_periods/post_periods", "period labels must be unique within each list")
    if set(pre_periods) & set(post_periods):
        raise _fail("pre_periods/post_periods", "pre and post periods must not overlap")
    periods = pre_periods + post_periods
    period_index = {period: index for index, period in enumerate(periods)}
    period_positions: dict[str, float] | None = None
    raw_positions = inputs.get("period_positions")
    if raw_positions is not None:
        positions = _mapping(raw_positions, "period_positions")
        if set(positions) != set(periods):
            raise _fail("period_positions", "must provide one numeric position for every declared period and no others")
        period_positions = {
            period: _number(positions[period], f"period_positions.{period}")
            for period in periods
        }
        if any(period_positions[left] >= period_positions[right] for left, right in zip(periods, periods[1:])):
            raise _fail("period_positions", "positions must increase in the declared period order")
    pretrend_margin = None
    if inputs.get("pretrend_equivalence_margin") is not None:
        pretrend_margin = _number(inputs["pretrend_equivalence_margin"], "pretrend_equivalence_margin", minimum=0, strict_minimum=True)
    unit_of_analysis = _optional_text(inputs.get("unit_of_analysis"), "unit_of_analysis")
    metric_unit = _optional_text(inputs.get("metric_unit"), "metric_unit")
    confidence_was_supplied = "confidence_level" in inputs
    confidence = _number(inputs.get("confidence_level", 0.95), "confidence_level", minimum=0, maximum=1, strict_minimum=True, strict_maximum=True)

    raw_rows = _sequence(inputs.get("observations"), "observations")
    unit_groups: dict[str, set[str]] = {}
    unit_clusters: dict[str, set[str]] = {}
    unit_cluster_explicit: dict[str, set[bool]] = {}
    unit_period_values: dict[tuple[str, str], list[float]] = {}
    out_of_plan = 0
    for index, raw in enumerate(raw_rows):
        _check_cancelled(is_cancelled)
        path = f"observations[{index}]"
        row = _mapping(raw, path)
        unit_id = _text(row.get("unit_id"), f"{path}.unit_id")
        group = _text(row.get("group"), f"{path}.group")
        if group not in {"treatment", "control"}:
            raise _fail(f"{path}.group", "must be 'treatment' or 'control'")
        period = _text(row.get("period"), f"{path}.period")
        value = _number(row.get("value"), f"{path}.value")
        cluster_id = row.get("cluster_id")
        if cluster_id is not None:
            cluster_id = _text(cluster_id, f"{path}.cluster_id")
        unit_cluster_explicit.setdefault(unit_id, set()).add(cluster_id is not None)
        unit_groups.setdefault(unit_id, set()).add(group)
        unit_clusters.setdefault(unit_id, set()).add(cluster_id or unit_id)
        if period not in period_index:
            out_of_plan += 1
            continue
        unit_period_values.setdefault((unit_id, period), []).append(value)

    changing_group_units = sorted(unit_id for unit_id, groups in unit_groups.items() if len(groups) != 1)
    changing_cluster_units = sorted(unit_id for unit_id, clusters in unit_clusters.items() if len(clusters) != 1)
    has_explicit_cluster_ids = any(any(flags) for flags in unit_cluster_explicit.values())
    has_missing_cluster_ids = any(not any(flags) for flags in unit_cluster_explicit.values())
    cluster_mapping_incomplete = has_explicit_cluster_ids and has_missing_cluster_ids
    cluster_mapping_incomplete = cluster_mapping_incomplete or any(
        any(flags) and not all(flags) for flags in unit_cluster_explicit.values()
    )
    group_by_unit = {
        unit_id: next(iter(groups))
        for unit_id, groups in unit_groups.items()
        if len(groups) == 1 and unit_id not in changing_cluster_units
    }
    cluster_by_unit = {
        unit_id: next(iter(unit_clusters[unit_id]))
        for unit_id in group_by_unit
    }

    # A cluster is the independent unit when several supplied units share one.
    cluster_groups: dict[str, set[str]] = {}
    cluster_units: dict[str, set[str]] = {}
    for unit_id, group in group_by_unit.items():
        cluster = cluster_by_unit[unit_id]
        cluster_groups.setdefault(cluster, set()).add(group)
        cluster_units.setdefault(cluster, set()).add(unit_id)
    cross_arm_clusters = sorted(cluster for cluster, groups in cluster_groups.items() if len(groups) > 1)

    cluster_period_values: dict[tuple[str, str], list[float]] = {}
    repeated_unit_periods = 0
    required_periods = set(periods)
    periods_by_unit: dict[str, set[str]] = {}
    for unit_id, period in unit_period_values:
        periods_by_unit.setdefault(unit_id, set()).add(period)
    complete_units = {
        unit_id for unit_id, observed_periods in periods_by_unit.items()
        if required_periods <= observed_periods
    }
    incomplete_units = sorted(set(unit_groups) - complete_units)
    incomplete_clusters_with_units = sorted({
        cluster_by_unit[unit_id]
        for unit_id in incomplete_units
        if unit_id in cluster_by_unit
    })
    for (unit_id, period), values in unit_period_values.items():
        if len(values) > 1:
            repeated_unit_periods += 1
        if unit_id not in group_by_unit or unit_id not in complete_units:
            continue
        cluster = cluster_by_unit[unit_id]
        if cluster in cross_arm_clusters:
            continue
        # Collapse repeated measurements within a unit-period before aggregating
        # to clusters, so a busy unit cannot create extra independent samples.
        cluster_period_values.setdefault((cluster, period), []).append(mean(values))

    cluster_values: dict[str, dict[str, float]] = {}
    for (cluster, period), values in cluster_period_values.items():
        cluster_values.setdefault(cluster, {})[period] = mean(values)
    cluster_group = {
        cluster: next(iter(groups))
        for cluster, groups in cluster_groups.items()
        if len(groups) == 1 and cluster not in cross_arm_clusters
    }
    complete_clusters = {
        cluster: values
        for cluster, values in cluster_values.items()
        if cluster in cluster_group and required_periods <= values.keys()
    }
    incomplete_clusters = sorted(set(cluster_group) - set(complete_clusters))
    group_clusters = {
        group: [cluster for cluster in complete_clusters if cluster_group[cluster] == group]
        for group in ("treatment", "control")
    }

    period_means: dict[str, dict[str, float | None]] = {}
    for period in periods:
        period_means[period] = {}
        for group in ("treatment", "control"):
            values = [complete_clusters[cluster][period] for cluster in group_clusters[group]]
            period_means[period][group] = mean(values) if values else None
    period_gaps = {
        period: (
            period_means[period]["treatment"] - period_means[period]["control"]
            if period_means[period]["treatment"] is not None and period_means[period]["control"] is not None
            else None
        )
        for period in periods
    }
    pre_gap_change = None
    if len(pre_periods) >= 2 and period_gaps[pre_periods[0]] is not None and period_gaps[pre_periods[-1]] is not None:
        pre_gap_change = period_gaps[pre_periods[-1]] - period_gaps[pre_periods[0]]

    treatment_changes: list[float] = []
    control_changes: list[float] = []
    for cluster, values in complete_clusters.items():
        pre_mean = mean([values[period] for period in pre_periods])
        post_mean = mean([values[period] for period in post_periods])
        change = post_mean - pre_mean
        if cluster_group[cluster] == "treatment":
            treatment_changes.append(change)
        else:
            control_changes.append(change)
    estimate_result = _welch_difference(treatment_changes, control_changes, confidence)

    pretrend_diagnostic: dict[str, Any] = {
        "pre_periods": pre_periods,
        "period_positions": period_positions,
        "treatment_minus_control_gap_by_period": {period: period_gaps[period] for period in pre_periods},
        "first_to_last_pre_period_gap_change": pre_gap_change,
        "status": "observed_pattern_only",
        "slope_difference": None,
        "slope_difference_interval": None,
        "prespecified_equivalence_margin": pretrend_margin,
        "equivalence_state": "not_evaluated",
    }
    if len(pre_periods) >= 2 and period_positions is not None:
        slopes: dict[str, list[float]] = {"treatment": [], "control": []}
        x_values = [period_positions[period] for period in pre_periods]
        x_mean = mean(x_values)
        x_variance = sum((x - x_mean) ** 2 for x in x_values)
        if x_variance > 0.0:
            for cluster, values in complete_clusters.items():
                y_values = [values[period] for period in pre_periods]
                y_mean = mean(y_values)
                slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values)) / x_variance
                slopes[cluster_group[cluster]].append(slope)
            if slopes["treatment"] and slopes["control"]:
                slope_result = _welch_difference(slopes["treatment"], slopes["control"], confidence)
                pretrend_diagnostic["slope_difference"] = slope_result["estimate"]
                pretrend_diagnostic["slope_difference_interval"] = slope_result["interval"]
                pretrend_diagnostic["status"] = "observed_slope_difference_not_a_parallel_trends_proof"
                if pretrend_margin is not None and slope_result["interval"] is not None:
                    lower, upper = slope_result["interval"]
                    inside_margin = lower > -pretrend_margin and upper < pretrend_margin
                    pretrend_diagnostic["equivalence_state"] = "within_prespecified_margin" if inside_margin else "not_established"
                elif pretrend_margin is not None:
                    pretrend_diagnostic["equivalence_state"] = "uncertainty_interval_unavailable"

    qualification = _mapping(inputs.get("counterfactual_qualification", {}), "counterfactual_qualification")
    comparison_rationale = _optional_text(qualification.get("comparison_rationale"), "counterfactual_qualification.comparison_rationale")
    counterevidence = _optional_text(qualification.get("counterevidence"), "counterfactual_qualification.counterevidence")
    statuses: dict[str, str | None] = {}
    for name in (
        "parallel_trends",
        "no_anticipation",
        "no_material_spillover",
        "no_differential_cointervention",
        "stable_composition",
    ):
        status = qualification.get(name)
        if status is not None and status not in {"supported", "not_assessed", "contradicted"}:
            raise _fail(f"counterfactual_qualification.{name}", "must be supported, not_assessed, or contradicted")
        statuses[name] = status
    qualification_complete = (
        bool(comparison_rationale)
        and bool(counterevidence)
        and all(statuses[name] == "supported" for name in statuses)
        and len(pre_periods) >= 2
        and period_positions is not None
        and pretrend_margin is not None
        and pretrend_diagnostic["equivalence_state"] == "within_prespecified_margin"
        and not incomplete_units
        and not incomplete_clusters
        and len(group_clusters["treatment"]) >= 2
        and len(group_clusters["control"]) >= 2
        and not changing_group_units
        and not changing_cluster_units
        and not cluster_mapping_incomplete
        and not cross_arm_clusters
        and out_of_plan == 0
        and bool(unit_of_analysis)
    )
    estimate_available = estimate_result["estimate"] is not None
    qualified = qualification_complete and estimate_available
    claim_type = "conditional_DiD_incrementality_estimate" if qualified else "descriptive_difference_in_differences"
    blockers: list[str] = []
    if not comparison_rationale:
        blockers.append("comparison rationale is missing")
    if not counterevidence:
        blockers.append("counterevidence review is missing")
    for name, status in statuses.items():
        if status != "supported":
            blockers.append(f"{name} is {status or 'unknown'}")
    if len(pre_periods) < 2:
        blockers.append("fewer than two pre-treatment periods were supplied; no pretrend pattern can be examined")
    if period_positions is None:
        blockers.append("numeric period positions are missing; a comparable pretrend slope cannot be estimated")
    if pretrend_margin is None:
        blockers.append("a prespecified pretrend equivalence margin is missing")
    elif pretrend_diagnostic["equivalence_state"] != "within_prespecified_margin":
        blockers.append("the pretrend slope interval is unavailable or is not contained within the prespecified equivalence margin")
    if incomplete_clusters:
        blockers.append("the compared units do not have a complete panel for every declared period")
    if incomplete_units:
        blockers.append("some source units lack one or more periods, so cluster composition could change")
    if len(group_clusters["treatment"]) < 2 or len(group_clusters["control"]) < 2:
        blockers.append("fewer than two complete independent units or clusters are present in a group")
    if changing_group_units:
        blockers.append("unit group membership changes across records")
    if changing_cluster_units:
        blockers.append("unit cluster identity changes across records")
    if cluster_mapping_incomplete:
        blockers.append("cluster identifiers are only partially supplied")
    if cross_arm_clusters:
        blockers.append("one reported cluster crosses treatment and control")
    if out_of_plan:
        blockers.append("observations outside the prespecified period list were present")
    if not estimate_available:
        blockers.append(estimate_result["reason"] or "estimate unavailable")
    if not unit_of_analysis:
        blockers.append("the independent analysis unit is not named")

    if not confidence_was_supplied:
        limitations = list(_METHOD_LIMITATIONS["incrementality_analysis"])
        limitations.append("A 95% confidence level was used as a disclosed computational default; it is not a design qualification threshold.")
    else:
        limitations = list(_METHOD_LIMITATIONS["incrementality_analysis"])
    if repeated_unit_periods:
        limitations.append("Repeated unit-period measurements were averaged within unit before cluster aggregation; this arithmetic mean must match the metric definition.")
    if out_of_plan:
        limitations.append("Records outside the named pre/post periods were excluded from the estimate and retained as a design diagnostic count.")
    if incomplete_clusters:
        limitations.append("Incomplete clusters were excluded from the balanced-panel estimate; differential missingness can change the target population.")
    if incomplete_units:
        limitations.append("Incomplete source units were excluded before cluster aggregation; the displayed cluster sample may differ from the eligible population.")
    if not qualified:
        limitations.append("The computed difference in differences remains descriptive because the counterfactual qualification gate was not satisfied.")

    diagnostics = {
        "input_rows": len(raw_rows),
        "units": len(unit_groups),
        "independent_units_or_clusters": len(complete_clusters),
        "complete_source_units": len(complete_units),
        "incomplete_source_units": len(incomplete_units),
        "clusters_with_incomplete_source_units": len(incomplete_clusters_with_units),
        "complete_treatment_units_or_clusters": len(group_clusters["treatment"]),
        "complete_control_units_or_clusters": len(group_clusters["control"]),
        "incomplete_clusters": len(incomplete_clusters),
        "repeated_unit_periods_averaged": repeated_unit_periods,
        "units_with_changing_group": len(changing_group_units),
        "units_with_changing_cluster": len(changing_cluster_units),
        "cluster_mapping_incomplete": cluster_mapping_incomplete,
        "clusters_crossing_groups": len(cross_arm_clusters),
        "out_of_plan_rows": out_of_plan,
        "cluster_level_estimand": True,
        "weighting": "equal weight per complete independent unit or reported cluster; within-cluster unit outcomes are averaged",
    }
    did_data = {
        "method": "balanced_panel_cluster_change_welch_did@1",
        "claim_type": claim_type,
        "confidence_level": confidence,
        "confidence_level_defaulted": not confidence_was_supplied,
        "estimate": estimate_result["estimate"],
        "standard_error": estimate_result["standard_error"],
        "degrees_of_freedom": estimate_result["degrees_of_freedom"],
        "interval": estimate_result["interval"],
        "unit": metric_unit,
        "unit_of_analysis": unit_of_analysis,
        "treatment_mean_change": mean(treatment_changes) if treatment_changes else None,
        "control_mean_change": mean(control_changes) if control_changes else None,
        "treatment_change_n": len(treatment_changes),
        "control_change_n": len(control_changes),
        "reason": estimate_result["reason"],
        "qualification_satisfied": qualified,
        "qualification_blockers": blockers,
    }
    qualification_data = {
        "comparison_rationale": comparison_rationale,
        "assessments": statuses,
        "counterevidence": counterevidence,
        "pretrend_diagnostic": pretrend_diagnostic,
        "status": "conditional_qualification_supplied" if qualified else "not_qualified_for_causal_claim",
        "claims_are_caller_assessments": True,
    }
    sections = [
        _section(
            "incrementality_counterfactual",
            "反事实资格与反证",
            qualification_data,
            "先呈现比较依据、平行趋势、无预期、无明显溢出、无差异共同干预、组成稳定/已调整及反证。观测到的前趋势用于质疑和解释，不能证明后期未处理反事实成立。",
        ),
        _section(
            "incrementality_did_estimate",
            "差分中的差分估计",
            did_data,
            "以每个独立分析单位或集群的处理前均值与处理后均值之差，再比较处理组与对照组的变化；多期按协议所列期间等权汇总。只有条件检查齐全才标为条件增量估计，否则明确为描述性差异。",
        ),
        _section(
            "incrementality_period_diagnostics",
            "期间、构成与重复诊断",
            {"period_order": periods, "period_means": period_means, "period_gaps": period_gaps, "diagnostics": diagnostics},
            "每期先保组别差值，再检查平衡面板、重复单位期间和跨组集群。改变干预日期、对照组或期间定义应形成新的分析输入；道路重开、迁店及其他同期事件需要进入反证审阅。",
        ),
    ]
    return _response("活动增量与 DiD 评价", sections, limitations)


def _model_lineage(
    value: Any,
    observation_ids: set[str],
    path: str,
    *,
    derived_training_ids: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    if value is None:
        return None, "NOT_PROVIDED"
    provenance = _mapping(value, path)
    required = {
        "model_id", "model_version", "calibration_model_version",
        "test_model_version", "population",
    }
    if not required.issubset(provenance) or set(provenance) - (required | {"training_ids"}):
        raise _fail(path, "must identify model version, calibration/test versions and target population")
    for field in required:
        _text(provenance.get(field), f"{path}.{field}")
    declared_training_ids = provenance.get("training_ids", derived_training_ids)
    if declared_training_ids is None:
        raise _fail(f"{path}.training_ids", "provide training observation IDs or a training partition in the saved data")
    training_ids = _sequence(declared_training_ids, f"{path}.training_ids")
    if not training_ids or any(not isinstance(item, str) or not item.strip() for item in training_ids):
        raise _fail(f"{path}.training_ids", "must contain nonempty training observation identities")
    if len(set(training_ids)) != len(training_ids) or observation_ids.intersection(training_ids):
        raise _fail(path, "training observations overlap calibration or test observations")
    if derived_training_ids and "training_ids" in provenance and set(training_ids) != set(derived_training_ids):
        raise _fail(f"{path}.training_ids", "must match the selected training partition IDs")
    if not (
        provenance["model_version"]
        == provenance["calibration_model_version"]
        == provenance["test_model_version"]
    ):
        raise _fail(path, "model versions differ across training, calibration and test")
    return provenance, "DECLARED_DISJOINT_NOT_INDEPENDENTLY_VERIFIED"


def _isotonic_fit(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    grouped: dict[float, list[int]] = {}
    for row in rows:
        values = grouped.setdefault(row["score"], [0, 0])
        values[0] += row["label"]
        values[1] += 1
    blocks: list[dict[str, float]] = []
    for score, (successes, count) in sorted(grouped.items()):
        blocks.append({"lower": score, "upper": score, "successes": float(successes), "count": float(count)})
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            if left["successes"] / left["count"] <= right["successes"] / right["count"]:
                break
            blocks[-2:] = [{
                "lower": left["lower"],
                "upper": right["upper"],
                "successes": left["successes"] + right["successes"],
                "count": left["count"] + right["count"],
            }]
    return {
        "upper_score": [block["upper"] for block in blocks],
        "probability": [block["successes"] / block["count"] for block in blocks],
        "block_n": [int(block["count"]) for block in blocks],
    }


def _isotonic_predict(mapping: dict[str, list[float]], scores: list[float]) -> list[float]:
    thresholds = mapping["upper_score"]
    values = mapping["probability"]
    predicted = []
    for score in scores:
        index = next((position for position, threshold in enumerate(thresholds) if score <= threshold), len(thresholds) - 1)
        predicted.append(values[index])
    return predicted


def _calibration_metrics(scores: list[float], labels: list[int]) -> dict[str, Any]:
    bins = []
    ece = 0.0
    brier = mean((score - label) ** 2 for score, label in zip(scores, labels))
    epsilon = 1e-15
    log_loss = mean(
        -(label * math.log(max(epsilon, score)) + (1 - label) * math.log(max(epsilon, 1 - score)))
        for score, label in zip(scores, labels)
    )
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        selected = [
            position for position, score in enumerate(scores)
            if lower <= score < upper or index == 9 and score == 1
        ]
        if selected:
            confidence = mean(scores[position] for position in selected)
            observed = mean(labels[position] for position in selected)
            ece += len(selected) / len(scores) * abs(confidence - observed)
        else:
            confidence = observed = None
        bins.append({
            "lower": lower,
            "upper": upper,
            "n": len(selected),
            "mean_probability": confidence,
            "observed_rate": observed,
        })
    return {"brier_score": brier, "log_loss": log_loss, "ece_10_equal_width_bins": ece, "bins": bins}


def _calibration_analysis(inputs: dict[str, Any]) -> dict[str, Any]:
    if set(inputs) - {"rows", "method", "model_provenance"}:
        raise _fail("inputs", "contains unsupported calibration fields")
    method = inputs.get("method", "isotonic")
    if method not in {"isotonic", "platt"}:
        raise _fail("method", "must be isotonic or platt")
    raw_rows = _sequence(inputs.get("rows"), "rows")
    if not 1 <= len(raw_rows) <= 10000:
        raise _fail("rows", "must contain between one and 10000 labeled predictions")
    ids: set[str] = set()
    partitions: dict[str, list[dict[str, Any]]] = {"training": [], "calibration": [], "test": []}
    training_ids: list[str] = []
    for index, raw in enumerate(raw_rows):
        path = f"rows[{index}]"
        row = _mapping(raw, path)
        if set(row) - {"id", "partition", "score", "label", "source"}:
            raise _fail(path, "contains unsupported calibration row fields")
        identity = _text(row.get("id"), f"{path}.id")
        if identity in ids:
            raise _fail(f"{path}.id", "observation identities must be unique")
        ids.add(identity)
        partition = _text(row.get("partition"), f"{path}.partition")
        if partition not in partitions:
            raise _fail(f"{path}.partition", "must be training, calibration or test")
        if partition == "training":
            score, label = row.get("score"), row.get("label")
            if score is not None:
                _number(score, f"{path}.score", minimum=0, maximum=1)
            if label is not None and (type(label) is not int or label not in {0, 1}):
                raise _fail(f"{path}.label", "must be null or integer 0 or 1 in training rows")
            _mapping(row.get("source", {}), f"{path}.source")
            training_ids.append(identity)
            continue
        score = _number(row.get("score"), f"{path}.score", minimum=0, maximum=1)
        label = row.get("label")
        if type(label) is not int or label not in {0, 1}:
            raise _fail(f"{path}.label", "must be an integer 0 or 1")
        source = _mapping(row.get("source", {}), f"{path}.source")
        partitions[partition].append({"id": identity, "score": score, "label": label, "source": source})
    evaluation_ids = ids - set(training_ids)
    provenance, lineage_state = _model_lineage(
        inputs.get("model_provenance"), evaluation_ids, "model_provenance",
        derived_training_ids=training_ids or None,
    )
    calibration, test = partitions["calibration"], partitions["test"]
    result = {
        "method": f"{method}-heldout-calibration@1",
        "state": "INSUFFICIENT",
        "lineage_state": lineage_state,
        "model_provenance": provenance,
        "training_n": len(training_ids),
        "training_ids": training_ids,
        "probability_state": "NOT_ESTABLISHED",
        "deployment_eligible": False,
        "calibration_n": len(calibration),
        "test_n": len(test),
        "mapping": None,
        "raw_test_metrics": None,
        "calibrated_test_metrics": None,
        "limitations": [
            "Calibration rows fit the mapping; the disjoint test rows evaluate it and must not be reused to tune it.",
            "Unique observation IDs and a declared training manifest can reveal overlap but do not independently authenticate model training or label truth.",
            "Calibration is population- and time-specific; probabilities are not approved for delivery, ranking or deployment by this calculation.",
        ],
    }
    if len(calibration) < 20 or len(test) < 20 or len({row["label"] for row in calibration}) < 2:
        result["reason"] = "NEED_20_PER_DISJOINT_PARTITION_AND_BOTH_CALIBRATION_CLASSES"
        return _response("概率校准评价", [
            _section("calibration_results", "校准与留出表现", result, "样本或校准类别不足；不拟合映射、不填补概率。")
        ], result["limitations"])
    x_calibration = [row["score"] for row in calibration]
    y_calibration = [row["label"] for row in calibration]
    x_test = [row["score"] for row in test]
    y_test = [row["label"] for row in test]
    if method == "isotonic":
        mapping = _isotonic_fit(calibration)
        calibrated = _isotonic_predict(mapping, x_test)
    else:
        try:
            from scipy.optimize import minimize
        except ImportError:
            result["reason"] = "SCIPY_OPTIMIZER_UNAVAILABLE"
            return _response("概率校准评价", [
                _section("calibration_results", "校准与留出表现", result, "Platt逻辑校准的求解依赖不可用；不退回未核验拟合。")
            ], result["limitations"])
        epsilon = 1e-12
        logits = [math.log(max(epsilon, score) / max(epsilon, 1 - score)) for score in x_calibration]

        def objective(parameters):
            slope, intercept = parameters
            total = 0.0
            gradient_slope = gradient_intercept = 0.0
            for logit, label in zip(logits, y_calibration):
                linear = slope * logit + intercept
                probability = 1 / (1 + math.exp(-max(-40.0, min(40.0, linear))))
                total += -(label * math.log(max(epsilon, probability)) + (1 - label) * math.log(max(epsilon, 1 - probability)))
                residual = probability - label
                gradient_slope += residual * logit
                gradient_intercept += residual
            ridge = 1e-8 * slope * slope
            return total + ridge, [gradient_slope + 2e-8 * slope, gradient_intercept]

        optimized = minimize(
            lambda parameters: objective(parameters)[0],
            [1.0, 0.0],
            jac=lambda parameters: objective(parameters)[1],
            method="L-BFGS-B",
            bounds=[(0.0, None), (None, None)],
        )
        if not optimized.success or not all(math.isfinite(float(value)) for value in optimized.x):
            result["reason"] = "PLATT_OPTIMIZATION_NOT_CONVERGED"
            return _response("概率校准评价", [
                _section("calibration_results", "校准与留出表现", result, "Platt模型未收敛；结果保持未知。")
            ], result["limitations"])
        slope, intercept = (float(value) for value in optimized.x)

        def platt(score: float) -> float:
            logit = math.log(max(epsilon, score) / max(epsilon, 1 - score))
            linear = max(-40.0, min(40.0, slope * logit + intercept))
            return 1 / (1 + math.exp(-linear))

        calibrated = [platt(score) for score in x_test]
        mapping = {"slope": slope, "intercept": intercept, "input_transform": "logit(clipped raw score)"}
    raw_metrics = _calibration_metrics(x_test, y_test)
    calibrated_metrics = _calibration_metrics(calibrated, y_test)
    result.update(
        state="READY",
        probability_state="EMPIRICAL_HELDOUT_CALIBRATION_ONLY",
        mapping=mapping,
        raw_test_metrics=raw_metrics,
        calibrated_test_metrics=calibrated_metrics,
        test_predictions=[
            {
                "id": row["id"],
                "raw_score": row["score"],
                "calibrated_probability": calibrated[index],
                "label": row["label"],
                "source": row["source"],
            }
            for index, row in enumerate(test)
        ],
    )
    sections = [
        _section(
            "calibration_results",
            "校准拟合与独立留出评价",
            result,
            "仅在 calibration 分区拟合映射，在不同 test 分区报告校准曲线、Brier、log loss 与ECE。概率留出表现不能证明标签真实性、时间稳定性或部署资格。",
        ),
    ]
    return _response("概率校准评价", sections, result["limitations"])


def _conformal_analysis(inputs: dict[str, Any]) -> dict[str, Any]:
    if set(inputs) - {"rows", "alpha", "model_provenance", "unit", "exchangeability_basis"}:
        raise _fail("inputs", "contains unsupported conformal fields")
    raw_rows = _sequence(inputs.get("rows"), "rows")
    if not 1 <= len(raw_rows) <= 10000:
        raise _fail("rows", "must contain between one and 10000 prediction records")
    alpha = _number(inputs.get("alpha", 0.1), "alpha", minimum=0, maximum=1, strict_minimum=True, strict_maximum=True)
    ids: set[str] = set()
    partitions: dict[str, list[dict[str, Any]]] = {"training": [], "calibration": [], "test": []}
    training_ids: list[str] = []
    for index, raw in enumerate(raw_rows):
        path = f"rows[{index}]"
        row = _mapping(raw, path)
        if set(row) - {"id", "partition", "prediction", "observed", "source"}:
            raise _fail(path, "contains unsupported conformal row fields")
        identity = _text(row.get("id"), f"{path}.id")
        if identity in ids:
            raise _fail(f"{path}.id", "observation identities must be unique")
        ids.add(identity)
        partition = _text(row.get("partition"), f"{path}.partition")
        if partition not in partitions:
            raise _fail(f"{path}.partition", "must be training, calibration or test")
        if partition == "training":
            prediction, observed = row.get("prediction"), row.get("observed")
            if prediction is not None:
                _number(prediction, f"{path}.prediction")
            if observed is not None:
                _number(observed, f"{path}.observed")
            _mapping(row.get("source", {}), f"{path}.source")
            training_ids.append(identity)
            continue
        prediction = _number(row.get("prediction"), f"{path}.prediction")
        observed = _number(row.get("observed"), f"{path}.observed")
        source = _mapping(row.get("source", {}), f"{path}.source")
        partitions[partition].append({
            "id": identity, "prediction": prediction, "observed": observed, "source": source,
        })
    evaluation_ids = ids - set(training_ids)
    provenance, lineage_state = _model_lineage(
        inputs.get("model_provenance"), evaluation_ids, "model_provenance",
        derived_training_ids=training_ids or None,
    )
    calibration, test = partitions["calibration"], partitions["test"]
    residuals = sorted(abs(row["observed"] - row["prediction"]) for row in calibration)
    n = len(residuals)
    rank = math.ceil((n + 1) * (1 - float(alpha)))
    result = {
        "method": "split-conformal-absolute-residual@1",
        "state": "INSUFFICIENT",
        "lineage_state": lineage_state,
        "model_provenance": provenance,
        "training_ids": training_ids,
        "n": len(raw_rows),
        "calibration_n": n,
        "test_n": len(test),
        "alpha": float(alpha),
        "nominal_coverage": 1 - float(alpha),
        "order_statistic_rank": rank,
        "unit": _optional_text(inputs.get("unit"), "unit"),
        "exchangeability_basis": _optional_text(inputs.get("exchangeability_basis"), "exchangeability_basis"),
        "residual_quantile": None,
        "test_coverage": None,
        "test_coverage_wilson_interval": None,
        "mean_interval_width": None,
        "intervals": [],
        "coverage_claim": "NOT_ESTABLISHED",
        "limitations": [
            "Split-conformal finite-sample marginal coverage requires exchangeability between calibration and future/test units.",
            "Temporal, clustered, selected or distribution-shifted rows do not inherit exchangeability from separate row IDs.",
            "A declared model version and disjoint IDs do not independently prove model training exclusion or outcome provenance.",
        ],
    }
    if n < 20 or not test or rank > n:
        result["reason"] = "INSUFFICIENT_CALIBRATION_OR_TEST_FOR_FINITE_SAMPLE_QUANTILE"
        return _response("Conformal预测区间评价", [
            _section("conformal_results", "校准分位数与测试覆盖", result, "校准样本或测试样本不足以支持本次split-conformal区间。")
        ], result["limitations"])
    quantile = residuals[rank - 1]
    intervals = []
    for row in test:
        lower, upper = row["prediction"] - quantile, row["prediction"] + quantile
        intervals.append({
            "id": row["id"],
            "prediction": row["prediction"],
            "observed": row["observed"],
            "lower": lower,
            "upper": upper,
            "width": upper - lower,
            "covered": lower <= row["observed"] <= upper,
            "source": row["source"],
        })
    covered = sum(row["covered"] for row in intervals)
    coverage = covered / len(intervals)
    result.update(
        state="READY",
        residual_quantile=quantile,
        test_coverage=coverage,
        test_coverage_wilson_interval=_wilson(covered, len(intervals), 0.95),
        mean_interval_width=mean(row["width"] for row in intervals),
        intervals=intervals,
        coverage_claim="EMPIRICAL_TEST_COVERAGE_ONLY_UNDER_EXCHANGEABILITY",
    )
    sections = [
        _section(
            "conformal_results",
            "校准分位数与测试覆盖",
            result,
            "用 disjoint calibration 残差的有限样本次序统计量构造对称区间，并在独立 test 分区报告覆盖与宽度。名义覆盖只有在可交换性成立时才有有限样本含义；时间序列须另证其适用性。",
        ),
    ]
    return _response("Conformal预测区间评价", sections, result["limitations"])


def evaluate_measurement(operation: str, inputs: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    """Evaluate one measurement operation and return stable semantic sections.

    Supported operations:
      * experiment_design: preserve an explicit protocol and identify unknowns.
      * experiment_analysis: diagnose assignments, estimate observed effects, and apply an optional fixed-look plan.
      * attribution_analysis: allocate observed conversions by touch rules or same-journey Markov/Shapley.
      * incrementality_analysis: evaluate balanced-panel DiD or qualified synthetic control.
      * calibration_analysis and conformal_analysis: evaluate disjoint held-out probabilities and intervals.
    """
    if type(operation) is not str or operation not in {
        "experiment_design",
        "experiment_analysis",
        "attribution_analysis",
        "incrementality_analysis",
        "calibration_analysis",
        "conformal_analysis",
    }:
        raise _fail("operation", "unsupported measurement operation")
    if not isinstance(inputs, dict):
        raise _fail("inputs", "must be an object")
    if is_cancelled is not None and not callable(is_cancelled):
        raise _fail("is_cancelled", "must be a callable cancellation check")
    from .business_analysis_economics import _check_cancelled

    _check_cancelled(is_cancelled)
    _validate_json_tree(inputs)
    if operation == "experiment_design":
        return _experiment_design(inputs)
    if operation == "experiment_analysis":
        return _experiment_analysis(inputs, is_cancelled=is_cancelled)
    if operation == "attribution_analysis":
        return _attribution_analysis(inputs, is_cancelled=is_cancelled)
    if operation == "incrementality_analysis":
        return _incrementality_analysis(inputs, is_cancelled=is_cancelled)
    if operation == "calibration_analysis":
        return _calibration_analysis(inputs)
    return _conformal_analysis(inputs)
