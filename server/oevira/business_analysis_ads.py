"""Competitive-ad and landing-page comparison.

Classifications (hook, format, offer, CTA, awareness_stage, landing_mismatch)
return evidence anchors and distinguish observation, interpretation and unknown.
A missing landing page is not judged as a mismatch. Approved effect raw counts
are the only basis for effect ranking; a missing effect is excluded, not treated
as zero. Abstentions and manual corrections are preserved. The operation does
not infer effect from a public ad, and does not invent a landing mismatch.
"""
from __future__ import annotations


DIMENSIONS = ("hook", "format", "offer", "CTA", "awareness_stage")
KINDS = {"observation", "interpretation", "unknown"}


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value.strip()


def _optional_text(value, field: str):
    if value is None:
        return None
    return _text(value, field)


def _evidence(value, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        items.append({
            "material_id": _text(item.get("material_id"), f"{field}[{index}].material_id"),
            "segment_id": _text(item.get("segment_id"), f"{field}[{index}].segment_id"),
            "quote": _text(item.get("quote"), f"{field}[{index}].quote"),
        })
    return items


def _classification(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    kind = value.get("kind") or "unknown"
    if kind not in KINDS:
        raise ValueError(f"{field}.kind must be observation, interpretation or unknown")
    evidence = _evidence(value.get("evidence"), f"{field}.evidence")
    if kind == "observation" and not evidence:
        raise ValueError(f"{field} observations require at least one evidence anchor")
    return {
        "value": _optional_text(value.get("value"), f"{field}.value"),
        "kind": kind,
        "evidence": evidence,
    }


def _source_ref(value, field: str):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a source reference object")
    material_id = _text(value.get("material_id"), f"{field}.material_id")
    version = value.get("version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        raise ValueError(f"{field}.version must be a positive integer")
    return {"material_id": material_id, "version": version}


def _approved_effect(value, field: str):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"{field}.count must be a non-negative integer raw count")
    return {
        "count": count,
        "unit": _text(value.get("unit"), f"{field}.unit"),
        "basis": _text(value.get("basis"), f"{field}.basis"),
    }


def _notes(value, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        items.append(dict(item))
    return items


def _ads(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("ads must be a nonempty array")
    ads = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Each ad must be an object")
        ad_id = _text(item.get("id"), f"ads[{index}].id")
        if ad_id in seen:
            raise ValueError("ad ids must be unique")
        seen.add(ad_id)
        classifications = item.get("classifications")
        if not isinstance(classifications, dict):
            raise ValueError(f"ads[{index}].classifications must be an object")
        parsed = {}
        for name in DIMENSIONS:
            if name in classifications:
                parsed[name] = _classification(classifications[name], f"ads[{index}].classifications.{name}")
            else:
                parsed[name] = {"value": None, "kind": "unknown", "evidence": []}
        landing = item.get("landing_page")
        landing_ref = _source_ref(landing, f"ads[{index}].landing_page") if landing else None
        mismatch = item.get("landing_mismatch")
        ads.append({
            "id": ad_id,
            "publisher": _text(item.get("publisher"), f"ads[{index}].publisher"),
            "media": _text(item.get("media"), f"ads[{index}].media"),
            "source": _source_ref(item.get("source"), f"ads[{index}].source"),
            "market": _optional_text(item.get("market"), f"ads[{index}].market"),
            "language": _optional_text(item.get("language"), f"ads[{index}].language"),
            "period": _optional_text(item.get("period"), f"ads[{index}].period"),
            "use": _optional_text(item.get("use"), f"ads[{index}].use"),
            "classifications": parsed,
            "landing_page": landing_ref,
            "landing_mismatch": (
                _classification(mismatch, f"ads[{index}].landing_mismatch") if mismatch else None
            ),
            "approved_effect": _approved_effect(item.get("approved_effect"), f"ads[{index}].approved_effect"),
            "unknown": _notes(item.get("unknown"), f"ads[{index}].unknown"),
            "abstentions": _notes(item.get("abstentions"), f"ads[{index}].abstentions"),
            "manual_corrections": _notes(item.get("manual_corrections"), f"ads[{index}].manual_corrections"),
        })
    return ads


def evaluate_competitive_ad_comparison(operation: str, inputs: dict) -> dict:
    if operation != "competitive_ad_comparison":
        raise ValueError("Unsupported competitive-ad operation")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    goal = _text(inputs.get("goal"), "goal")
    ads = _ads(inputs.get("ads"))
    method_version = _text(inputs.get("classification_method_version"), "classification_method_version")
    limitations: list[str] = []
    market = _optional_text(inputs.get("market"), "market")
    language = _optional_text(inputs.get("language"), "language")
    period = _optional_text(inputs.get("period"), "period")
    use = _optional_text(inputs.get("use"), "use")
    inclusion = _optional_text(inputs.get("inclusion"), "inclusion")
    exclusion = _optional_text(inputs.get("exclusion"), "exclusion")
    coverage = inputs.get("coverage") if isinstance(inputs.get("coverage"), dict) else {}

    dimensions = {name: [] for name in DIMENSIONS}
    for ad in ads:
        for name, classification in ad["classifications"].items():
            dimensions[name].append({"ad_id": ad["id"], **classification})

    landing_rows = []
    for ad in ads:
        if ad["landing_page"] is None:
            landing_rows.append({
                "ad_id": ad["id"],
                "status": "not_judged",
                "is_mismatch": False,
                "landing_page": None,
                "evidence": [],
                "reason": "missing_landing_page",
            })
            limitations.append(f"广告 {ad['id']} 未给出落地页，不作不符判断。")
            continue
        supplied = ad["landing_mismatch"]
        if supplied is None:
            landing_rows.append({
                "ad_id": ad["id"],
                "status": "unknown",
                "is_mismatch": False,
                "landing_page": ad["landing_page"],
                "evidence": [],
                "reason": "landing_present_without_evidence",
            })
            limitations.append(f"广告 {ad['id']} 有落地页但未给出可核差异证据，差异保持未知。")
            continue
        is_mismatch = supplied["kind"] == "observation" and (supplied.get("value") or "").strip() not in {
            "matched", "一致", "无差异", "",
        } and "不符" in (supplied.get("value") or "" ) or (
            supplied["kind"] == "observation" and supplied.get("value") in {"mismatch", "mismatched", "不符"}
        )
        # Status follows the supplied observation; we never upgrade a missing
        # landing (already handled) into a mismatch.
        status = "mismatch" if is_mismatch else (supplied["value"] or supplied["kind"] or "unknown")
        if status == "matched" or (supplied.get("value") in {"matched", "一致", "无差异"}):
            status = "matched"
            is_mismatch = False
        landing_rows.append({
            "ad_id": ad["id"],
            "status": status,
            "is_mismatch": bool(is_mismatch),
            "landing_page": ad["landing_page"],
            "evidence": supplied["evidence"],
            "kind": supplied["kind"],
            "value": supplied["value"],
        })

    ranked = []
    not_ranked = []
    for ad in ads:
        effect = ad["approved_effect"]
        if effect is None:
            not_ranked.append({"ad_id": ad["id"], "reason": "missing_approved_effect"})
            limitations.append(f"广告 {ad['id']} 没有获准效果原始计数，不参与效果排名。")
            continue
        ranked.append({
            "ad_id": ad["id"],
            "count": effect["count"],
            "unit": effect["unit"],
            "basis": effect["basis"],
        })
    ranked.sort(key=lambda item: (-item["count"], item["ad_id"]))

    abstentions = [{"ad_id": ad["id"], **item} for ad in ads for item in ad["abstentions"]]
    corrections = [{"ad_id": ad["id"], **item} for ad in ads for item in ad["manual_corrections"]]
    unknowns = [{"ad_id": ad["id"], **item} for ad in ads for item in ad["unknown"]]

    class_lines = []
    for name, rows in dimensions.items():
        parts = []
        for row in rows:
            label = row["value"] or "未知"
            parts.append(f"{row['ad_id']} 为{label}（{row['kind']}）")
        class_lines.append(f"{name}：" + "；".join(parts) + "。")

    landing_lines = []
    for row in landing_rows:
        if row["status"] == "not_judged":
            landing_lines.append(f"{row['ad_id']} 未给出落地页，不作不符判断。")
        elif row["status"] == "unknown":
            landing_lines.append(f"{row['ad_id']} 的落地页差异未知。")
        else:
            landing_lines.append(f"{row['ad_id']} 落地页状态为 {row['status']}。")

    if ranked:
        ranking_body = "效果排名（仅含获准原始计数）：" + "、".join(
            f"{item['ad_id']} {item['count']} {item['unit']}" for item in ranked
        ) + "。"
    else:
        ranking_body = "没有获准效果原始计数，本版不给出效果排名。"
    if not_ranked:
        ranking_body += "未参与排名：" + "、".join(item["ad_id"] for item in not_ranked) + "。"

    unique_limitations = []
    for note in limitations:
        if note not in unique_limitations:
            unique_limitations.append(note)

    return {
        "title": "竞品广告与落地页比较",
        "sections": [
            {
                "id": "purpose",
                "title": "本次业务问题",
                "body": goal,
                "data": {
                    "goal": goal,
                    "market": market,
                    "language": language,
                    "period": period,
                    "use": use,
                    "classification_method_version": method_version,
                    "inclusion": inclusion,
                    "exclusion": exclusion,
                    "coverage": coverage,
                },
            },
            {
                "id": "classifications",
                "title": "创意分类与证据锚",
                "body": " ".join(class_lines),
                "data": {"dimensions": dimensions},
            },
            {
                "id": "landing_comparison",
                "title": "落地页比较",
                "body": " ".join(landing_lines),
                "data": {"rows": landing_rows},
            },
            {
                "id": "effect_ranking",
                "title": "效果排名",
                "body": ranking_body,
                "data": {"ranked": ranked, "not_ranked": not_ranked},
            },
            {
                "id": "unknowns",
                "title": "未知、弃答与人工校正",
                "body": (
                    f"未知 {len(unknowns)} 项，弃答 {len(abstentions)} 项，人工校正 {len(corrections)} 项。"
                ),
                "data": {
                    "unknown": unknowns,
                    "abstentions": abstentions,
                    "manual_corrections": corrections,
                },
            },
        ],
        "limitations": unique_limitations,
    }
