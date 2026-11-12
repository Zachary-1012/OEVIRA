"""KOS batch quality and decision eligibility.

Eight quality indicators are returned separately, each with numerator,
denominator, excluded and unknown counts. Rates are not fabricated when an
indicator is missing or has a zero denominator. Unverified or anomalous
batches are not eligible for formal tiering or effect ranking. A manual
snapshot cannot pose as a native day, week or month metric. Corrections only
recompute the submitted impact set; this operation does not invent a second
source or permission store.
"""
from __future__ import annotations


INDICATORS = (
    "acquisition",
    "valid_content",
    "subject_precision",
    "duplicates",
    "author_resolution",
    "time",
    "engagement",
    "evidence_completeness",
)
INDICATOR_LABELS = {
    "acquisition": "取得",
    "valid_content": "有效内容",
    "subject_precision": "主体精确",
    "duplicates": "重复",
    "author_resolution": "作者解析",
    "time": "时间",
    "engagement": "互动",
    "evidence_completeness": "证据完整性",
}
BATCH_STATUSES = {"verified", "unverified", "anomalous"}
BATCH_KINDS = {"native_period", "manual_snapshot"}
FORMAL_DECISIONS = {"formal_tiering", "effect_ranking"}


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value.strip()


def _optional_text(value, field: str):
    if value is None:
        return None
    return _text(value, field)


def _nonneg_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _indicator(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    numerator = _nonneg_int(value.get("numerator", 0), f"{field}.numerator")
    denominator = _nonneg_int(value.get("denominator", 0), f"{field}.denominator")
    excluded = _nonneg_int(value.get("excluded", 0), f"{field}.excluded")
    unknown = _nonneg_int(value.get("unknown", 0), f"{field}.unknown")
    measured = denominator > 0
    if numerator > denominator:
        raise ValueError(f"{field}.numerator cannot exceed denominator")
    return {
        "numerator": numerator if measured else None,
        "denominator": denominator,
        "excluded": excluded,
        "unknown": unknown,
        "rate": {"numerator": numerator, "denominator": denominator} if measured else None,
        "measured": measured,
    }


def _coverage(value, field: str):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    numerator = _nonneg_int(value.get("numerator", 0), f"{field}.numerator")
    denominator = _nonneg_int(value.get("denominator", 0), f"{field}.denominator")
    return {"numerator": numerator, "denominator": denominator}


def _batches(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("batches must be a nonempty array")
    batches = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Each batch must be an object")
        batch_id = _text(item.get("id"), f"batches[{index}].id")
        if batch_id in seen:
            raise ValueError("batch ids must be unique")
        seen.add(batch_id)
        status = item.get("status") or "unverified"
        if status not in BATCH_STATUSES:
            raise ValueError("batch status must be verified, unverified or anomalous")
        kind = item.get("kind") or "native_period"
        if kind not in BATCH_KINDS:
            raise ValueError("batch kind must be native_period or manual_snapshot")
        raw_indicators = item.get("indicators") or {}
        if not isinstance(raw_indicators, dict):
            raise ValueError(f"batches[{index}].indicators must be an object")
        indicators = {}
        for name in INDICATORS:
            if name in raw_indicators:
                indicators[name] = _indicator(raw_indicators[name], f"batches[{index}].indicators.{name}")
            else:
                indicators[name] = {
                    "numerator": None,
                    "denominator": 0,
                    "excluded": 0,
                    "unknown": 0,
                    "rate": None,
                    "measured": False,
                    "missing": True,
                }
        batches.append({
            "id": batch_id,
            "account_id": _text(item.get("account_id"), f"batches[{index}].account_id"),
            "rule_version": _text(item.get("rule_version"), f"batches[{index}].rule_version"),
            "status": status,
            "kind": kind,
            "period": _optional_text(item.get("period"), f"batches[{index}].period"),
            "review_labels": item.get("review_labels") if isinstance(item.get("review_labels"), list) else [],
            "sample_coverage": _coverage(item.get("sample_coverage"), f"batches[{index}].sample_coverage"),
            "attribution_evidence": (
                item.get("attribution_evidence") if isinstance(item.get("attribution_evidence"), list) else []
            ),
            "indicators": indicators,
        })
    return batches


def _decisions(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("decision_uses must be a nonempty array")
    decisions = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Each decision use must be an object")
        decision_id = _text(item.get("id"), f"decision_uses[{index}].id")
        if decision_id in seen:
            raise ValueError("decision_use ids must be unique")
        seen.add(decision_id)
        required = item.get("required_indicators") or list(INDICATORS)
        if not isinstance(required, list) or any(name not in INDICATORS for name in required):
            raise ValueError(f"decision_uses[{index}].required_indicators contains an unknown indicator")
        decisions.append({
            "id": decision_id,
            "purpose": _text(item.get("purpose"), f"decision_uses[{index}].purpose"),
            "required_indicators": list(required),
            "conditions_version": _text(item.get("conditions_version"), f"decision_uses[{index}].conditions_version"),
            "requires_native_period": bool(item.get("requires_native_period")),
            "period": _optional_text(item.get("period"), f"decision_uses[{index}].period"),
        })
    return decisions


def _eligibility(batch: dict, decision: dict) -> dict:
    reasons = []
    pending = False
    if batch["status"] in {"unverified", "anomalous"} and decision["id"] in FORMAL_DECISIONS:
        label = "未核" if batch["status"] == "unverified" else "异常"
        reasons.append(f"{label}批次不能用于{decision['purpose']}。")
    if decision.get("requires_native_period") and batch["kind"] == "manual_snapshot":
        reasons.append("单次手工快照不能冒日、周或月的原生指标。")
    if decision.get("requires_native_period") and decision.get("period") and batch["period"] != decision["period"]:
        reasons.append(f"批次期间 {batch['period'] or '未声明'} 与决定要求的 {decision['period']} 不一致。")
    for name in decision["required_indicators"]:
        indicator = batch["indicators"][name]
        label = INDICATOR_LABELS[name]
        if indicator.get("missing") or not indicator["measured"]:
            reasons.append(f"{label}（{name}）缺失或没有分母，不虚构比率。")
            pending = True
            continue
        if indicator["unknown"] and indicator["unknown"] >= indicator["denominator"]:
            reasons.append(f"{label}的未知占满可计范围，资格仍待核。")
            pending = True
    eligible = not reasons
    return {
        "batch_id": batch["id"],
        "decision_id": decision["id"],
        "purpose": decision["purpose"],
        "conditions_version": decision["conditions_version"],
        "eligible": eligible,
        "reasons": reasons,
        "pending_review": pending or batch["status"] != "verified",
        "review_version": batch["rule_version"],
    }


def evaluate_kos_batch_quality(operation: str, inputs: dict) -> dict:
    if operation != "kos_batch_quality":
        raise ValueError("Unsupported KOS quality operation")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    goal = _text(inputs.get("goal"), "goal")
    batches = _batches(inputs.get("batches"))
    decisions = _decisions(inputs.get("decision_uses"))
    limitations: list[str] = []

    indicator_rows = []
    for batch in batches:
        for name in INDICATORS:
            row = {"batch_id": batch["id"], "name": name, "label": INDICATOR_LABELS[name], **batch["indicators"][name]}
            indicator_rows.append(row)
            if not row["measured"]:
                limitations.append(f"批次 {batch['id']} 的{row['label']}没有可测分母，本版不虚构该指标比率。")

    eligibility = []
    for batch in batches:
        for decision in decisions:
            result = _eligibility(batch, decision)
            eligibility.append(result)
            if not result["eligible"]:
                for reason in result["reasons"]:
                    note = f"批次 {batch['id']} 对「{decision['purpose']}」不合格：{reason}"
                    if note not in limitations:
                        limitations.append(note)

    body_lines = []
    for batch in batches:
        parts = []
        for name in INDICATORS:
            item = batch["indicators"][name]
            label = INDICATOR_LABELS[name]
            if item["measured"]:
                parts.append(f"{label} {item['rate']['numerator']}/{item['rate']['denominator']}（未知 {item['unknown']}）")
            else:
                parts.append(f"{label}未测")
        body_lines.append(f"批次 {batch['id']}（{batch['status']}）：" + "，".join(parts) + "。")

    eligibility_lines = []
    for row in eligibility:
        state = "具备资格" if row["eligible"] else "不具备资格"
        eligibility_lines.append(f"批次 {row['batch_id']} 用于{row['purpose']}：{state}。")

    unique_limitations = []
    for note in limitations:
        if note not in unique_limitations:
            unique_limitations.append(note)

    return {
        "title": "KOS批次质量与决定资格",
        "sections": [
            {
                "id": "purpose",
                "title": "本次业务问题",
                "body": goal,
                "data": {"goal": goal, "decision_uses": decisions},
            },
            {
                "id": "indicators",
                "title": "八项质量指标",
                "body": " ".join(body_lines),
                "data": {"indicators": indicator_rows},
            },
            {
                "id": "decision_eligibility",
                "title": "决定资格",
                "body": " ".join(eligibility_lines),
                "data": {"decisions": eligibility},
            },
            {
                "id": "review",
                "title": "复核与归属",
                "body": "纠错只重算本批影响集，不另造来源或权限库。",
                "data": {
                    "batches": [
                        {
                            "id": batch["id"],
                            "account_id": batch["account_id"],
                            "rule_version": batch["rule_version"],
                            "review_labels": batch["review_labels"],
                            "sample_coverage": batch["sample_coverage"],
                            "attribution_evidence": batch["attribution_evidence"],
                        }
                        for batch in batches
                    ]
                },
            },
        ],
        "limitations": unique_limitations,
    }
