"""Brand recognition and asset-consistency evaluation.

Measured unaided recognition, misrecognition and unknown rates come only from
coded raw answers. Unknown remains in the denominator. Excluded answers stay
out. Aesthetic judgments and AI or other predicted recognition figures are
never reported as measured rates, and the operation does not fabricate rates
when raw answers are missing. A prompted test is not an unaided measurement.
Incomparable prior tests are not pooled into the current measured rates.
"""
from __future__ import annotations

from fractions import Fraction


_CODED = {"recognized", "misrecognized", "unknown", "excluded"}
_INCLUDED = ("recognized", "misrecognized", "unknown")


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value.strip()


def _optional_text(value, field: str):
    if value is None:
        return None
    return _text(value, field)


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator}


def _render_rate(rate: dict | None) -> str:
    if rate is None:
        return "未测"
    return f"{rate['numerator']}/{rate['denominator']}"


def _assets(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("assets must be a nonempty array of legal asset versions")
    assets = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Each asset must be an object")
        asset_id = _text(item.get("id"), f"assets[{index}].id")
        channel = _text(item.get("channel"), f"assets[{index}].channel")
        variant = _text(item.get("variant_version"), f"assets[{index}].variant_version")
        key = (asset_id, channel, variant)
        if key in seen:
            raise ValueError("Each legal asset, channel and variant version must be unique")
        seen.add(key)
        assets.append({
            "id": asset_id,
            "legal_name": _text(item.get("legal_name"), f"assets[{index}].legal_name"),
            "use": _text(item.get("use"), f"assets[{index}].use"),
            "channel": channel,
            "variant_version": variant,
        })
    return assets


def _answers(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("raw_answers must be an array")
    answers = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Each raw answer must be an object")
        answer_id = _text(item.get("id"), f"raw_answers[{index}].id")
        if answer_id in seen:
            raise ValueError("raw_answers ids must be unique")
        seen.add(answer_id)
        coded = item.get("coded")
        if not isinstance(coded, str) or coded not in _CODED:
            raise ValueError("Each raw answer needs a coding of recognized, misrecognized, unknown or excluded")
        answers.append({
            "id": answer_id,
            "respondent_id": _text(item.get("respondent_id"), f"raw_answers[{index}].respondent_id"),
            "asset_id": _text(item.get("asset_id"), f"raw_answers[{index}].asset_id"),
            "channel": _text(item.get("channel"), f"raw_answers[{index}].channel"),
            "text": _text(item.get("text"), f"raw_answers[{index}].text"),
            "coded": coded,
        })
    return answers


def _tally(answers: list[dict]) -> dict:
    counts = {name: 0 for name in _CODED}
    for item in answers:
        counts[item["coded"]] += 1
    included = sum(counts[name] for name in _INCLUDED)
    measured = included > 0
    if not measured:
        return {
            "recognized": None,
            "misrecognized": None,
            "unknown": None,
            "excluded": counts["excluded"],
            "denominator": 0,
            "recognized_rate": None,
            "misrecognized_rate": None,
            "unknown_rate": None,
            "measured": False,
            "unaided": False,
        }
    return {
        "recognized": counts["recognized"],
        "misrecognized": counts["misrecognized"],
        "unknown": counts["unknown"],
        "excluded": counts["excluded"],
        "denominator": included,
        "recognized_rate": _rate(counts["recognized"], included),
        "misrecognized_rate": _rate(counts["misrecognized"], included),
        "unknown_rate": _rate(counts["unknown"], included),
        "measured": True,
        "unaided": True,
    }


def _empty_rate_row(asset: dict) -> dict:
    return {
        "asset_id": asset["id"],
        "channel": asset["channel"],
        "variant_version": asset["variant_version"],
        "recognized": None,
        "misrecognized": None,
        "unknown": None,
        "excluded": 0,
        "denominator": 0,
        "recognized_rate": None,
        "misrecognized_rate": None,
        "unknown_rate": None,
        "measured": False,
        "unaided": False,
    }


def evaluate_brand_recognition(operation: str, inputs: dict) -> dict:
    if operation != "brand_recognition":
        raise ValueError("Unsupported brand operation")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    goal = _text(inputs.get("goal"), "goal")
    assets = _assets(inputs.get("assets"))
    answers = _answers(inputs.get("raw_answers"))
    limitations: list[str] = []

    population = inputs.get("population") or {}
    if not isinstance(population, dict):
        raise ValueError("population must be an object")
    definition = _optional_text(population.get("definition"), "population.definition")
    recruitment = _optional_text(population.get("recruitment"), "population.recruitment")
    if not definition or not recruitment:
        limitations.append("研究人群或招募口径未完整给出，本版覆盖范围仍待核，不以缺省人群补全。")

    unaided = inputs.get("unaided_test") or {}
    if not isinstance(unaided, dict):
        raise ValueError("unaided_test must be an object")
    rules = _optional_text(unaided.get("rules"), "unaided_test.rules")
    exposure = _optional_text(unaided.get("exposure"), "unaided_test.exposure")
    prompted = unaided.get("prompted", False)
    if prompted is None:
        prompted = False
    if not isinstance(prompted, bool):
        raise ValueError("unaided_test.prompted must be a boolean")
    if not rules or not exposure:
        limitations.append("无提示测试规则或暴露条件未完整给出，识别率的适用边界仍待核。")

    coding = inputs.get("coding") or {}
    if not isinstance(coding, dict):
        raise ValueError("coding must be an object")
    coding_version = _optional_text(coding.get("version"), "coding.version")
    inclusion = _optional_text(coding.get("inclusion"), "coding.inclusion")
    exclusion = _optional_text(coding.get("exclusion"), "coding.exclusion")
    if not coding_version or not inclusion or not exclusion:
        limitations.append("编码或纳排口径未完整给出；本版只按已给出的 coding 字段计次，不补编码。")

    existing = inputs.get("existing_tests") or []
    if not isinstance(existing, list):
        raise ValueError("existing_tests must be an array")
    incomparable = []
    for index, item in enumerate(existing):
        if not isinstance(item, dict):
            raise ValueError("Each existing test must be an object")
        test_id = _optional_text(item.get("id"), f"existing_tests[{index}].id") or f"prior-{index + 1}"
        comparable = item.get("comparable", False)
        if comparable is True:
            limitations.append(f"既有测试 {test_id} 虽声明可比，本运算仍不把它的识别率并入本版实测，只作对照资格记录。")
        else:
            reason = _optional_text(item.get("reason"), f"existing_tests[{index}].reason") or "未给出可比资格"
            incomparable.append({"id": test_id, "reason": reason})
            limitations.append(f"既有测试 {test_id} 与本版不可合并：{reason}")

    if "predicted_recognition" in inputs and inputs["predicted_recognition"] is not None:
        limitations.append("输入中的识别预测未被采用。审美判断或 AI 预测不能冒实测识别。")
    judgments = inputs.get("aesthetic_judgments")
    if judgments:
        limitations.append("输入中的审美判断未被采用，不能冒实测识别。")

    if not answers:
        limitations.append("没有编码后的原始回答，本版不给出实测识别率，也不虚构分母或比率。")

    # Answers whose asset/channel is not in the ledger stay out of measured rates.
    ledger_keys = {(item["id"], item["channel"]) for item in assets}
    used_answers: list[dict] = []
    for item in answers:
        if (item["asset_id"], item["channel"]) not in ledger_keys:
            limitations.append(
                f"回答 {item['id']} 指向的资产或渠道不在本版合法资产台账中，未计入实测。"
            )
            continue
        used_answers.append(item)

    unaided_eligible = (not prompted) and bool(used_answers)
    if prompted:
        limitations.append("本次测试带有提示，不能作为无提示识别的实测结果；本版不给出无提示识别率。")

    rates = []
    for asset in assets:
        subset = [
            item for item in used_answers
            if item["asset_id"] == asset["id"] and item["channel"] == asset["channel"]
        ]
        if not unaided_eligible:
            row = _empty_rate_row(asset)
            if prompted and subset:
                counts = {name: sum(item["coded"] == name for item in subset) for name in _CODED}
                row["prompted_counts"] = counts
            rates.append(row)
            continue
        tally = _tally(subset)
        rates.append({
            "asset_id": asset["id"],
            "channel": asset["channel"],
            "variant_version": asset["variant_version"],
            **tally,
        })
        if not tally["measured"] and not subset:
            limitations.append(
                f"资产 {asset['id']} 在{asset['channel']}渠道没有编码后的原始回答，该行不给出实测识别率。"
            )

    differences = []
    by_asset: dict[str, list[dict]] = {}
    for row in rates:
        by_asset.setdefault(row["asset_id"], []).append(row)
    for asset_id, rows in by_asset.items():
        measured_rows = [row for row in rows if row["measured"]]
        if len(measured_rows) < 2:
            if len(rows) >= 2 and not measured_rows:
                limitations.append(f"资产 {asset_id} 的跨渠道差异因缺少实测识别率而未计算。")
            continue
        # Pairwise differences as exact fractions (first minus second), only where both measured.
        for left, right in zip(measured_rows, measured_rows[1:]):
            left_rate = Fraction(left["recognized_rate"]["numerator"], left["recognized_rate"]["denominator"])
            right_rate = Fraction(right["recognized_rate"]["numerator"], right["recognized_rate"]["denominator"])
            delta = left_rate - right_rate
            differences.append({
                "asset_id": asset_id,
                "left_channel": left["channel"],
                "right_channel": right["channel"],
                "left_recognized_rate": left["recognized_rate"],
                "right_recognized_rate": right["recognized_rate"],
                "recognized_difference": {
                    "numerator": delta.numerator,
                    "denominator": delta.denominator,
                },
            })

    purpose_body = goal
    if definition:
        purpose_body += f" 研究人群：{definition}。"
    if recruitment:
        purpose_body += f" 招募：{recruitment}。"
    if rules:
        purpose_body += f" 无提示规则：{rules}"
    if exposure:
        purpose_body += f" 暴露条件：{exposure}"

    ledger_lines = [
        f"{item['legal_name']}（{item['id']}）用途 {item['use']}，渠道 {item['channel']}，变体 {item['variant_version']}。"
        for item in assets
    ]
    if unaided_eligible:
        rate_lines = []
        for row in rates:
            if row["measured"]:
                rate_lines.append(
                    f"{row['asset_id']} 在{row['channel']}：识别 {_render_rate(row['recognized_rate'])}、"
                    f"误认 {_render_rate(row['misrecognized_rate'])}、未知 {_render_rate(row['unknown_rate'])}"
                    f"（分母 {row['denominator']} 含未知，已排除 {row['excluded']} 条）。"
                )
            else:
                rate_lines.append(f"{row['asset_id']} 在{row['channel']}：没有可测的编码回答，不给出识别率。")
        recognition_body = " ".join(rate_lines) if rate_lines else "没有可测的编码回答，不给出识别率。"
    else:
        recognition_body = (
            "本次不能作为无提示识别的实测。"
            if prompted
            else "没有编码后的原始回答，本版不给出实测识别率。"
        )

    if differences:
        difference_body = " ".join(
            f"{item['asset_id']} 在{item['left_channel']}为{_render_rate(item['left_recognized_rate'])}，"
            f"在{item['right_channel']}为{_render_rate(item['right_recognized_rate'])}，"
            f"识别差 {_render_rate(item['recognized_difference'])}。"
            for item in differences
        )
    else:
        difference_body = "本版没有两套同时实测的渠道结果可比较，不给出跨渠道差异。"

    plan_parts = [
        "策略、原创及交付应引用本版成果，不得另造一份识别率。",
        "应用前需按同一编码与纳排口径复核原始回答，未知保持在分母中。",
    ]
    if coding_version:
        plan_parts.append(f"现行编码版为 {coding_version}。")
    if incomparable:
        plan_parts.append("与本版不可比的既有测试不得并入实测，只可在验证计划中单独对照。")
    plan_parts.append("验证计划：在相同人群、招募、无提示规则和暴露条件下独立复测，才能更新实测识别率。")

    # Deduplicate limitations while preserving order.
    unique_limitations = []
    for note in limitations:
        if note not in unique_limitations:
            unique_limitations.append(note)

    return {
        "title": "品牌识别与资产一致性",
        "sections": [
            {
                "id": "purpose",
                "title": "本次业务问题",
                "body": purpose_body,
                "data": {
                    "goal": goal,
                    "population": {"definition": definition, "recruitment": recruitment},
                    "unaided_test": {"rules": rules, "exposure": exposure, "prompted": prompted},
                    "coding": {"version": coding_version, "inclusion": inclusion, "exclusion": exclusion},
                },
            },
            {
                "id": "asset_ledger",
                "title": "合法资产台账",
                "body": " ".join(ledger_lines),
                "data": {"assets": assets},
            },
            {
                "id": "unaided_recognition",
                "title": "无提示识别",
                "body": recognition_body,
                "data": {"rates": rates},
            },
            {
                "id": "cross_channel_differences",
                "title": "跨渠道差异",
                "body": difference_body,
                "data": {"differences": differences},
            },
            {
                "id": "application_plan",
                "title": "应用与验证计划",
                "body": "".join(plan_parts),
                "data": {
                    "incomparable_prior_tests": incomparable,
                    "cite_same_version": True,
                },
            },
        ],
        "limitations": unique_limitations,
    }
