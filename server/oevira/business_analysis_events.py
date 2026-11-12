"""Observed propagation structure and conditional diffusion models for B8.

Adapts the qualified R4 observed-network, exponential Hawkes and Bass methods.
Observation coverage and causal claims remain separate from numerical fitting.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np
import networkx as nx
from scipy.optimize import least_squares, minimize


OPERATIONS = {"propagation_network", "propagation_hawkes", "adoption_diffusion"}


def _number(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return float(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value.strip()


def _check_cancelled(check):
    if check is not None and check():
        from .business_analysis_economics import BusinessAnalysisCancelled
        raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")


def _table(headers, rows):
    def cell(value):
        if value is None:
            return "未知"
        if isinstance(value, float):
            return format(value, ".8g")
        return str(value).replace("|", "\\|").replace("\n", " ")
    rows = list(rows)
    return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" + "\n".join("| " + " | ".join(cell(value) for value in row) + " |" for row in rows) if rows else "当前没有可列示的合格观察。"


def _section(identifier, title, body, data):
    return {"id": identifier, "title": title, "body": body, "data": data}


def _qualification(inputs, required):
    qualification = inputs.get("qualification", {})
    if not isinstance(qualification, dict):
        raise ValueError("qualification must be an object")
    blockers = []
    for key, label in required.items():
        item = qualification.get(key)
        if not isinstance(item, dict) or item.get("status") not in {"supported", "unknown", "contradicted"}:
            blockers.append(label + "未核")
            continue
        if item["status"] != "supported" or not isinstance(item.get("basis"), str) or not item["basis"].strip() or not item.get("source_refs"):
            blockers.append(label + "缺少可核依据")
    return deepcopy(qualification), blockers


def _network(inputs, cancelled):
    raw_nodes, raw_edges = inputs.get("nodes"), inputs.get("edges")
    relation = inputs.get("relation")
    if relation not in {"repost", "mention", "collaboration"}:
        raise ValueError("Select one observed relation layer: repost, mention or collaboration")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("nodes and edges must be arrays")
    nodes = {}
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise ValueError("Each network node must be an object")
        identifier = _text(item.get("id"), "node.id")
        if identifier in nodes:
            raise ValueError("Network node identities must be unique")
        nodes[identifier] = {**item, "id": identifier, "label": _text(item.get("label"), "node.label"), "platform": _text(item.get("platform"), "node.platform")}
    edges, by_id = [], {}
    excluded = []
    for item in raw_edges:
        if not isinstance(item, dict):
            raise ValueError("Each observed relation must be an object")
        identifier = _text(item.get("id"), "edge.id")
        if identifier in by_id:
            if by_id[identifier] != item:
                raise ValueError("Conflicting observations share a relation identity")
            continue
        by_id[identifier] = item
        if item.get("source") not in nodes or item.get("target") not in nodes or item["source"] == item["target"]:
            raise ValueError("An observed edge must link two different supplied node identities")
        if item.get("relation") not in {"repost", "mention", "collaboration"}:
            raise ValueError("Unknown observed relation type")
        if not isinstance(item.get("source_refs"), list) or not item["source_refs"]:
            excluded.append({"id": identifier, "reason": "relation_evidence_missing"})
        elif item["relation"] != relation:
            excluded.append({"id": identifier, "reason": "different_relation_layer"})
        else:
            edges.append(deepcopy(item))
    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from((edge["source"], edge["target"]) for edge in edges)
    limitations = ["只计算本次有来源的关系层；同名或粉丝数不会生成边。结构中心性不是因果影响、独立触达或采购价值。", "同方向重复关系仅合并计算邻接；原始关系观察、类型与来源完整保留。跨平台身份沿原账号保留。"]
    metrics = []
    hits = {"status": "no_observed_relations"}
    if graph.number_of_edges():
        _check_cancelled(cancelled)
        pagerank = nx.pagerank(graph, alpha=0.85, tol=1e-12, max_iter=1000)
        # Subset accumulation gives exact directed Brandes centrality and lets the
        # existing Job cancellation fence run between source-node calculations.
        bridge = dict.fromkeys(nodes, 0.0)
        node_ids = list(nodes)
        for source in node_ids:
            _check_cancelled(cancelled)
            partial = nx.betweenness_centrality_subset(graph, [source], node_ids, normalized=False)
            for node, value in partial.items():
                bridge[node] += value
        normalizer = (len(nodes) - 1) * (len(nodes) - 2)
        for identifier, item in nodes.items():
            metrics.append({"id": identifier, "label": item["label"], "platform": item["platform"], "pagerank": pagerank[identifier],
                            "betweenness": bridge[identifier] / normalizer if normalizer > 0 else 0.0,
                            "in_degree": graph.in_degree(identifier), "out_degree": graph.out_degree(identifier)})
        if len(nodes) * len(nodes) <= 4_000_000:
            adjacency = nx.to_numpy_array(graph, nodelist=node_ids)
            u, singular, vh = np.linalg.svd(adjacency, full_matrices=False)
            if len(singular) == 1 or singular[0] - singular[1] > 1e-10 * max(1, singular[0]):
                hubs, authorities = np.abs(u[:, 0]), np.abs(vh[0])
                hits = {"status": "unique_dominant_mode", "nodes": [{"id": node, "label": nodes[node]["label"], "hub": float(hubs[index] / hubs.sum()), "authority": float(authorities[index] / authorities.sum())} for index, node in enumerate(node_ids)]}
            else:
                hits = {"status": "non_unique_dominant_mode", "reason": "No single HITS ranking is identified by this observed graph"}
        else:
            hits = {"status": "matrix_budget_exceeded", "requested_cells": len(nodes) ** 2, "supported_cells_per_run": 4_000_000}
            limitations.append("HITS矩阵超过本次资源预算；已交付的实际关系、PageRank与中介计算不受影响，未计算项不冒结果。")
    result = {"method": "observed-directed-network@2", "relation": relation, "nodes": list(nodes.values()), "raw_edges": list(by_id.values()), "selected_edges": edges,
              "excluded_edges": excluded, "raw_observation_count": len(raw_edges), "unique_observations": len(by_id), "calculation_edges": graph.number_of_edges(),
              "status": "calculated" if edges else "no_observed_relations", "centrality": metrics, "hits": hits, "causality": "not_established"}
    sections = [
        _section("propagation.relations", "实际传播与合作关系", _table(["来源节点", "目标节点", "关系", "来源项数"], ((nodes[row["source"]]["label"], nodes[row["target"]]["label"], {"repost": "转发", "mention": "提及", "collaboration": "合作"}[row["relation"]], len(row["source_refs"])) for row in edges)), result),
        _section("propagation.centrality", "本次关系层中的结构位置", _table(["节点", "平台", "流入关系", "流出关系", "PageRank", "中介中心性"], ((row["label"], row["platform"], row["in_degree"], row["out_degree"], row["pagerank"], row["betweenness"]) for row in metrics)), {"nodes": metrics, "unit": "dimensionless_structure_not_effect"}),
        _section("propagation.hits", "内容承接与桥接结构", _table(["节点", "枢纽分量", "权威分量"], ((row["label"], row["hub"], row["authority"]) for row in hits.get("nodes", []))) + "\n\n" + ("当前关系不足以识别唯一排序。" if hits["status"] != "unique_dominant_mode" else "该分量仅描述所选观察图的链接结构。"), hits),
    ]
    return {"title": inputs.get("title") or "传播关系分析", "sections": sections, "limitations": limitations}


def _hawkes_fit(times, end, cancelled):
    normalized = np.asarray(times, dtype=float) / end
    bounds = [(1e-6, len(times) * 10), (1e-6, 0.999), (1e-3, 1e4)]
    def loss(parameters):
        _check_cancelled(cancelled)
        mu, ratio, decay = parameters
        excitation = previous = logs = 0.0
        for index, at in enumerate(normalized):
            if index:
                excitation = math.exp(-decay * (at - previous)) * (1 + excitation)
            logs += math.log(mu + ratio * decay * excitation)
            previous = at
        return mu + ratio * float(np.sum(-np.expm1(-decay * (1 - normalized)))) - logs
    candidates = []
    for ratio in (0.1, 0.5, 0.8):
        fitted = minimize(loss, [len(times) * (1 - ratio), ratio, 20], method="L-BFGS-B", bounds=bounds, options={"maxiter": 500, "ftol": 1e-11})
        if fitted.success and np.isfinite(fitted.fun):
            candidates.append(fitted)
    if not candidates:
        return None
    best = min(candidates, key=lambda candidate: candidate.fun)
    if any(abs(value - low) <= 1e-5 * max(1, abs(low)) or abs(value - high) <= 1e-5 * max(1, abs(high)) for value, (low, high) in zip(best.x, bounds)):
        return None
    mu, ratio, decay = best.x
    return {"baseline_intensity": float(mu / end), "branching_ratio": float(ratio), "decay_rate": float(decay / end), "negative_log_likelihood_dimensionless": float(best.fun), "optimizer_iterations": int(best.nit)}


def _hawkes_expected(fit, times, end, horizon):
    mu, ratio, decay = fit["baseline_intensity"], fit["branching_ratio"], fit["decay_rate"]
    excitation = ratio * decay * float(np.exp(-decay * (end - np.asarray(times))).sum())
    rate, equilibrium = decay * (1 - ratio), mu / (1 - ratio)
    return float(equilibrium * horizon + (excitation + mu - equilibrium) * (-math.expm1(-rate * horizon)) / rate)


def _hawkes(inputs, cancelled):
    observations = inputs.get("events")
    if not isinstance(observations, list):
        raise ValueError("events must contain observed event times and source references")
    end = _number(inputs.get("observation_end"), "observation_end", minimum=0)
    if end == 0:
        raise ValueError("observation_end must be positive")
    unit = _text(inputs.get("time_unit"), "time_unit")
    seen, times, rows, unplaced = set(), [], [], []
    for event in observations:
        if not isinstance(event, dict):
            raise ValueError("Each event must be an object")
        identifier = _text(event.get("id"), "event.id")
        if identifier in seen:
            raise ValueError("Resolve duplicate event identities before fitting propagation")
        seen.add(identifier)
        if event.get("time") is None:
            unplaced.append(identifier)
            continue
        at = _number(event["time"], "event.time", minimum=0)
        if at >= end:
            raise ValueError("Event times must be inside the declared observation window")
        if not isinstance(event.get("source_refs"), list) or not event["source_refs"]:
            unplaced.append(identifier)
            continue
        times.append(at)
        rows.append(event)
    times.sort()
    qualification, blockers = _qualification(inputs, {"complete_capture": "完整事件捕获", "empty_pre_history": "观察起点无未计前史", "event_time_precision": "原生事件时间精度", "stationary_window": "窗口内稳定过程假设"})
    if unplaced:
        blockers.append(f"{len(unplaced)}项缺时间或来源")
    if len(set(times)) != len(times):
        blockers.append("存在并列时间，当前连续时间简单点过程不能靠抖动伪造时间精度")
    train = [value for value in times if value < end / 2]
    if len(train) < 30:
        blockers.append("前半观察窗不足30个事件，无法沿本方法完成分离时间检验")
    model = {"method": "exponential-hawkes-mle@2", "status": "conditions_insufficient", "qualification": qualification, "blockers": blockers,
             "events": rows, "unplaced_event_ids": unplaced, "observed_count": len(times), "observation_end": end, "time_unit": unit,
             "observed_average_rate": len(times) / end, "fit": None, "holdout": None, "finite_peak_time": None}
    if not blockers:
        baseline = _hawkes_fit(train, end / 2, cancelled)
        full = _hawkes_fit(times, end, cancelled)
        if baseline is None or full is None:
            model["status"] = "fit_not_identified"
            model["blockers"] = ["最大似然未收敛或落在不可识别边界；不提供伪精确传播参数。"]
        else:
            predicted = _hawkes_expected(baseline, train, end / 2, end / 2)
            observed = len(times) - len(train)
            poisson_prediction = len(train)
            model.update(status="conditional_fit", fit=full,
                         holdout={"train_count": len(train), "test_count": observed, "predicted_count": predicted, "absolute_count_error": abs(predicted - observed),
                                  "poisson_baseline_prediction": poisson_prediction, "poisson_baseline_error": abs(poisson_prediction - observed)},
                         kernel_half_life=math.log(2) / full["decay_rate"], mean_relaxation_half_life=math.log(2) / (full["decay_rate"] * (1 - full["branching_ratio"])),
                         expected_count_next_equal_window=_hawkes_expected(full, times, end, end))
    fit_labels = {"baseline_intensity": f"背景事件强度（每{unit}）", "branching_ratio": "条件分支系数", "decay_rate": f"衰减率（每{unit}）", "negative_log_likelihood_dimensionless": "无量纲负对数似然", "optimizer_iterations": "优化迭代次数"}
    fit_rows = [(fit_labels[key], value) for key, value in (model["fit"] or {}).items()]
    holdout_labels = {"train_count": "训练窗事件数", "test_count": "检验窗实测事件数", "predicted_count": "检验窗条件预测事件数", "absolute_count_error": "绝对事件数误差", "poisson_baseline_prediction": "恒定发生率基线预测", "poisson_baseline_error": "恒定发生率基线误差"}
    return {"title": inputs.get("title") or "传播时间变化", "sections": [
        _section("propagation.timeline", "实际事件时间与覆盖", _table([f"相对时间（{unit}）", "事件说明", "来源项数"], ((row["time"], row.get("label", "已保存事件"), len(row["source_refs"])) for row in sorted(rows, key=lambda item: item["time"]))), {"events": rows, "unknown_time_or_source": len(unplaced), "count": len(times)}),
        _section("propagation.diffusion", "有条件的传播过程", _table(["拟合量", "结果"], fit_rows + [("核衰减半衰期", model.get("kernel_half_life")), ("平均恢复半衰期", model.get("mean_relaxation_half_life")), ("下一等长窗口的条件期望事件数", model.get("expected_count_next_equal_window"))]) + "\n\n" + "\n".join("- " + item for item in model["blockers"]), model),
        _section("propagation.holdout", "后半窗口实测检验", _table(["比较项目", "结果"], ((holdout_labels[key], value) for key, value in (model["holdout"] or {}).items())), {"holdout": model["holdout"]}),
    ], "limitations": ["该模型条件于完整捕获、给定起点前史及平稳单变量指数核；资格记录是提供方的依据，不替独立验证。", "结果是点拟合，参数和未来事件数的不确定性未被本方法量化；不解释为因果扩散或全市场传播。", "条件均值向稳态单调变化，不给不存在的有限内点峰值；声量和真实服务损害分别判断。"]}


def _bass_fit(times, cumulative, cancelled):
    times, cumulative = np.asarray(times, dtype=float), np.asarray(cumulative, dtype=float)
    time_scale, count_scale = times[-1], max(cumulative[-1], 1)
    def curve(parameters, at):
        p, q, potential = np.exp(parameters)
        decay = np.exp(-(p + q) * np.asarray(at) / time_scale)
        return count_scale * potential * (1 - decay) / (1 + q / p * decay)
    def residual(parameters):
        _check_cancelled(cancelled)
        return (curve(parameters, times) - cumulative) / count_scale
    candidates = []
    low, high = np.array([-12, -12, 0]), np.array([5, 7, 12])
    for initial in ((.1, 1, 2), (.5, 2, 3), (.01, 5, 10)):
        fit = least_squares(residual, np.log(initial), bounds=(low, high), max_nfev=3000,
                            ftol=1e-12, xtol=1e-12, gtol=1e-12, x_scale="jac")
        # Market potential cannot be below observed adoption. A near-saturated
        # but identifiable curve can legitimately approach that physical lower
        # bound; it is different from a rate hitting an arbitrary numeric bound.
        if fit.success and np.linalg.matrix_rank(fit.jac) == 3 and np.linalg.cond(fit.jac) < 1e8 and not np.any(abs(fit.x[:2] - low[:2]) < 1e-4) and not np.any(abs(fit.x - high) < 1e-4):
            candidates.append(fit)
    if not candidates:
        return None, None
    fit = min(candidates, key=lambda item: item.cost)
    p, q, potential = np.exp(fit.x)
    result = {"innovation": float(p / time_scale), "imitation": float(q / time_scale), "market_potential": float(potential * count_scale), "jacobian_condition": float(np.linalg.cond(fit.jac)), "cost": float(fit.cost), "potential_lower_bound_active": bool(abs(fit.x[2]) < 1e-4)}
    return result, lambda at: curve(fit.x, at)


def _bass(inputs, cancelled):
    raw = inputs.get("observations")
    if not isinstance(raw, list):
        raise ValueError("observations must be cumulative first-adoption counts")
    unit = _text(inputs.get("time_unit"), "time_unit")
    rows = []
    for observation in raw:
        if not isinstance(observation, dict):
            raise ValueError("Each adoption observation must be an object")
        at = _number(observation.get("time"), "observation.time", minimum=0)
        count = _number(observation.get("cumulative"), "observation.cumulative", minimum=0)
        if at == 0:
            raise ValueError("Observation times must be positive relative to the declared launch")
        rows.append({**observation, "time": at, "cumulative": count})
    rows.sort(key=lambda row: row["time"])
    if len({row["time"] for row in rows}) != len(rows) or any(rows[index]["cumulative"] < rows[index - 1]["cumulative"] for index in range(1, len(rows))):
        raise ValueError("Resolve duplicate times or decreasing cumulative adoptions before fitting")
    qualification, blockers = _qualification(inputs, {"fixed_population": "封闭且固定的潜在总体", "one_adoption_per_unit": "每单位首次采用定义", "known_launch": "真实起点", "coverage": "采用观察覆盖"})
    if any(not isinstance(row.get("source_refs"), list) or not row["source_refs"] for row in rows):
        blockers.append("部分观察缺少原始来源")
    if len(rows) < 30 or not rows or rows[-1]["cumulative"] <= 0:
        blockers.append("本方法至少需要30个观察时点及正的累计首次采用数")
    model = {"method": "bass-nonlinear-least-squares@2", "status": "conditions_insufficient", "qualification": qualification, "blockers": blockers, "fit": None, "holdout": None}
    predictions = []
    if not blockers:
        times = np.array([row["time"] for row in rows])
        counts = np.array([row["cumulative"] for row in rows])
        split = int(np.searchsorted(times, times[-1] / 2, side="right"))
        if split < 10 or len(times) - split < 5:
            model["blockers"].append("前半时间训练与后半检验的观察数量不足")
        else:
            baseline, prediction = _bass_fit(times[:split], counts[:split], cancelled)
            full, full_prediction = _bass_fit(times, counts, cancelled)
            if baseline is None or full is None:
                model["status"] = "fit_not_identified"
                model["blockers"].append("拟合参数不可识别或优化未收敛")
            else:
                fitted = prediction(times[split:])
                predictions = [{"time": float(at), "observed": float(observed), "predicted": float(predicted)} for at, observed, predicted in zip(times[split:], counts[split:], fitted)]
                model.update(status="conditional_fit", fit=full, peak_time_from_launch=max(0, math.log(full["imitation"] / full["innovation"]) / (full["innovation"] + full["imitation"])),
                             holdout={"train_count": split, "test_count": len(times) - split, "mae": float(np.mean(abs(fitted - counts[split:]))), "last_relative_error": float(abs(fitted[-1] - counts[-1]) / counts[-1])},
                             fitted_cumulative=[float(value) for value in full_prediction(times)])
    fit_labels = {"innovation": f"外部采用系数（每{unit}）", "imitation": f"模仿采用系数（每{unit}）", "market_potential": "有条件的潜在采用规模", "jacobian_condition": "参数识别的雅可比条件数", "cost": "归一化残差损失", "potential_lower_bound_active": "潜在规模是否贴近已观察采用下界"}
    return {"title": inputs.get("title") or "首次采用与扩散观察", "sections": [
        _section("adoption.observations", "原始首次采用观察", _table([f"相对起点时间（{unit}）", "累计首次采用"], ((row["time"], row["cumulative"]) for row in rows)), {"observations": rows}),
        _section("adoption.model", "扩散拟合与适用条件", _table(["拟合量", "结果"], [(fit_labels[key], value) for key, value in (model["fit"] or {}).items()] + [("相对真实起点的峰值时点", model.get("peak_time_from_launch"))]) + "\n\n" + "\n".join("- " + value for value in model["blockers"]), model),
        _section("adoption.holdout", "时间外观察核对", _table(["时间", "实测累计", "训练后预测累计"], ((row["time"], row["observed"], row["predicted"]) for row in predictions)), {"observations": predictions, "diagnostics": model["holdout"]}),
    ], "limitations": ["固定潜在总体、每单位首次采用及真实起点是条件；累计互动、重复购买或发帖数不能冒首次采用人数。", "累计残差相关，当前不提供独立误差假设下的伪置信区间。拟合与峰值不建立真实总体覆盖或因果影响。", "提供方的资格依据仍须在当前任务中核对；条件不足时仍交原始观察，不补造市场潜力。"]}


def evaluate_events(operation, inputs, *, is_cancelled=None):
    if operation not in OPERATIONS or not isinstance(inputs, dict):
        raise ValueError("Unknown event analysis operation or invalid inputs")
    _check_cancelled(is_cancelled)
    data_kind = inputs.get("data_kind", "user_recorded")
    if data_kind not in {"observed", "synthetic", "user_recorded"}:
        raise ValueError("Data provenance must remain observed, synthetic or user_recorded")
    result = {"propagation_network": _network, "propagation_hawkes": _hawkes, "adoption_diffusion": _bass}[operation](inputs, is_cancelled)
    result["data_kind"] = data_kind
    result["limitations"].insert(0, {"synthetic": "本次为模拟输入，只用于核计算和条件情景，不代表真实传播或经营结果。",
                                      "observed": "本次采用调用方标为实测的原始观察，来源真实性与覆盖仍按其具体证据复核。",
                                      "user_recorded": "本次为用户提供的观察记录，尚未独立核验数据取得与覆盖。"}[data_kind])
    return result
