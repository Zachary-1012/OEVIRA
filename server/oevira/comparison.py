"""Deterministic, source-bound comparison for the no-AI J-01 path.

This module searches and aligns literal evidence. It does not infer semantic
equivalence, verify claims, choose a preferred source, or persist results.
"""

from __future__ import annotations

import re
import unicodedata


_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_TOKEN = re.compile(r"[a-z0-9]+(?:[._:/%+-][a-z0-9]+)*", re.IGNORECASE)
_DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:年(?:\s*\d{1,2}月(?:\s*\d{1,2}日)?)?|[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?"
)
_NUMBER = re.compile(r"(?<!\d)[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?(?:[%％]|万|亿|千)?")
_MAX_EVIDENCE_PER_CELL = 50
_MAX_NUMBER_EVIDENCE = 250
_MAX_CRITERIA = 20
_MAX_CRITERION_CHARS = 256
_MAX_COMPARISON_TEXT_CHARS = 20_000_000


class ComparisonInputLimitError(ValueError):
    """The selected, parsed input exceeded the comparison text budget."""

    def __init__(self, actual_chars: int, limit_chars: int) -> None:
        self.actual_chars = actual_chars
        self.limit_chars = limit_chars
        super().__init__(
            f"combined parsed text contains {actual_chars} characters; "
            f"the comparison limit is {limit_chars}"
        )


# Exact lexical oppositions only. Each detected pair is a candidate for review,
# never a semantic contradiction verdict.
_OPPOSING_CUES = (
    ("增长", "下降"),
    ("增加", "减少"),
    ("上升", "下降"),
    ("盈利", "亏损"),
    ("支持", "不支持"),
    ("可行", "不可行"),
    ("允许", "禁止"),
    ("通过", "未通过"),
    ("完成", "未完成"),
    ("有效", "无效"),
    ("稳定", "不稳定"),
)


def build_comparison(materials: list[dict], criteria: list[str], goal: str) -> dict:
    """Return editable criteria, source excerpts, and review-only difference candidates.

    Each material is ``{"material_id": str, "parsed": parse_material_result}``.
    For convenience a flattened parse result with ``material_id`` is also
    accepted. This function has no filesystem, database, network, or model side
    effects; changing criteria or removing a material simply recomputes output.
    """
    if not isinstance(materials, list):
        raise ValueError("materials must be a list")
    if not isinstance(criteria, list):
        raise ValueError("criteria must be a list")
    if not isinstance(goal, str):
        raise ValueError("goal must be a string")
    clean_criteria = [item.strip() for item in criteria
                      if isinstance(item, str) and item.strip()]
    if not clean_criteria:
        raise ValueError("at least one non-empty comparison criterion is required")
    if len(clean_criteria) > _MAX_CRITERIA or any(len(item) > _MAX_CRITERION_CHARS for item in clean_criteria):
        raise ValueError("comparison criteria exceed the current safety limit")

    prepared = [_prepare_material(item, index) for index, item in enumerate(materials)]
    if len({item["material_id"] for item in prepared}) != len(prepared):
        raise ValueError("material_id values must be unique within a comparison")
    total_text_chars = sum(len(item["parsed"].get("text", "")) for item in prepared)
    if total_text_chars > _MAX_COMPARISON_TEXT_CHARS:
        raise ComparisonInputLimitError(total_text_chars, _MAX_COMPARISON_TEXT_CHARS)
    limitations = [
        "本结果仅进行确定性字段/结构核对与原文词面检索；不判断语义等价、事实真伪、代表性或因果。",
        "表格字段候选仅来自首行/首个文本行等结构提示；它不是已验证的数据字典，标题行也可能被当作字段。",
        "“未发现”仅表示在已解析文字中没有匹配到当前比较依据；不证明原件不含相关信息。",
        "不同数字、日期或相反用词只是待核候选；需由用户检查单位、定义、期间、样本及上下文。",
    ]
    for item in prepared:
        parsed = item["parsed"]
        if parsed["status"] in {"partial", "unavailable", "error"}:
            detail = parsed.get("reason") or "解析未完整可用。"
            limitations.append(f"{item['material_id']}：{detail}")
        if any(segment.get("kind") == "xlsx_formula" for segment in parsed["segments"]):
            limitations.append(f"{item['material_id']}：电子表格公式未重算，缓存值可能过期。")

    rows: list[dict] = []
    all_citations: list[dict] = []
    citation_seen: set[tuple[str, str, str]] = set()
    omitted_match_count = 0
    for criterion in clean_criteria:
        terms = _criterion_terms(criterion)
        cells = []
        all_evidence = []
        for item in prepared:
            parsed = item["parsed"]
            state = parsed["status"]
            if state not in {"ready", "partial"}:
                cell_status = "read_error" if state == "error" else "unavailable"
                cells.append({
                    "material_id": item["material_id"],
                    "status": cell_status,
                    "reason": parsed.get("reason"),
                    "excerpts": [],
                    "numbers": [],
                    "matched_segment_count": 0,
                    "omitted_match_count": 0,
                })
                continue

            excerpts = []
            number_evidence = []
            matched_segment_count = 0
            for segment in parsed["segments"]:
                segment_text = segment.get("text")
                if not isinstance(segment_text, str) or not segment_text.strip():
                    continue
                match = _match_segment(segment_text, criterion, terms, segment.get("label", ""))
                if match is None:
                    continue
                matched_segment_count += 1
                if len(excerpts) >= _MAX_EVIDENCE_PER_CELL:
                    continue
                quote, quote_start, quote_end, truncated = _quote(segment_text, match["positions"])
                focus_start, focus_end = _focus_bounds(segment_text, match["positions"])
                excerpt = {
                    "segment_id": segment["id"],
                    "label": segment["label"],
                    "quote": quote,
                    "quote_start": quote_start,
                    "quote_end": quote_end,
                    "truncated": truncated,
                    "match_type": match["match_type"],
                    "matched_terms": match["matched_terms"],
                }
                excerpts.append(excerpt)
                evidence = {
                    "material_id": item["material_id"],
                    "segment_id": segment["id"],
                    "label": segment["label"],
                    "quote": quote,
                    "kind": segment.get("kind", "unknown"),
                    "full_text": segment_text,
                    "focus_start": focus_start,
                    "focus_end": focus_end,
                }
                all_evidence.append(evidence)
                if segment.get("kind") != "xlsx_formula":
                    numeric_text = segment_text[focus_start:focus_end]
                    if segment.get("kind") == "xlsx_date":
                        numeric_text = numeric_text.split("（Excel 序列值", 1)[0]
                    _append_numbers(number_evidence, item["material_id"], segment,
                                    numeric_text, focus_start)
                citation_key = (item["material_id"], segment["id"], quote)
                if citation_key not in citation_seen:
                    citation_seen.add(citation_key)
                    all_citations.append({
                        "material_id": item["material_id"],
                        "segment_id": segment["id"],
                        "quote": quote,
                    })
                if len(excerpts) >= _MAX_EVIDENCE_PER_CELL:
                    continue

            if state == "partial":
                cell_status = "partial" if excerpts else "partial_unassessed"
            else:
                cell_status = "found" if excerpts else "not_found"
            omitted_matches = max(0, matched_segment_count - len(excerpts))
            omitted_match_count += omitted_matches
            cells.append({
                "material_id": item["material_id"],
                "status": cell_status,
                "reason": parsed.get("reason") if state == "partial" else None,
                "excerpts": excerpts,
                "numbers": number_evidence,
                "matched_segment_count": matched_segment_count,
                "omitted_match_count": omitted_matches,
            })

        rows.append({
            "criterion": criterion,
            "cells": cells,
            "numeric_difference_candidates": _numeric_differences(all_evidence),
            "conflict_candidates": _lexical_conflicts(all_evidence),
        })

    if omitted_match_count:
        limitations.append(
            f"共有 {omitted_match_count} 个匹配片段超出每个材料/依据 {_MAX_EVIDENCE_PER_CELL} 条的引文与差异候选分析上限；"
            "总命中数已保留，超出部分未纳入引文及候选判断。"
        )

    goal_text = goal.strip()
    title = f"材料比较：{goal_text}" if goal_text else "材料比较"
    if len(title) > 180:
        title = title[:177] + "…"
    assessed = sum(1 for item in prepared if item["parsed"]["status"] in {"ready", "partial"})
    body_lines = [
        f"比较目标：{goal_text or '用户尚未补充目标。'}",
        f"当前输入：{len(prepared)} 份；其中 {assessed} 份存在可检索文字。",
        "每项依据保留为可编辑条件；单元格列出可定位的原文片段、数字/日期及未发现或读取状态。",
        "数字差异和相反用词只提示复核，不代表口径一致、事实冲突已确认或某份材料优先。",
    ]
    return {
        "title": title,
        "body": "\n".join(body_lines),
        "citations": all_citations,
        "rows": rows,
        "limitations": _unique(limitations),
    }


def build_research_evidence_draft(materials: list[dict], question: str) -> dict:
    """Build an editable, source-bound research starting point without inference.

    A single source is valid, and an empty source list produces a useful blank
    research draft. Excerpts are literal candidates only; the output deliberately
    leaves argument, method choice, counterevidence and conclusions for review.
    """
    if not isinstance(materials, list):
        raise ValueError("materials must be a list")
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    clean_question = question.strip()
    if not clean_question or len(clean_question) > 2_000:
        raise ValueError("question must contain 1 to 2000 characters")

    prepared = [_prepare_material(item, index) for index, item in enumerate(materials)]
    if len({item["material_id"] for item in prepared}) != len(prepared):
        raise ValueError("material_id values must be unique within a research draft")
    total_text_chars = sum(len(item["parsed"].get("text", "")) for item in prepared)
    if total_text_chars > _MAX_COMPARISON_TEXT_CHARS:
        raise ComparisonInputLimitError(total_text_chars, _MAX_COMPARISON_TEXT_CHARS)

    limitations = [
        "本底稿只按问题词面定位已解析原文；不判断语义相关、事实真伪、来源代表性或因果关系。",
        "原文摘录、作者主张、分析推论和结论仍须分别核对与撰写；不能把词面命中当作证据已支持结论。",
    ]
    if not prepared:
        limitations.append("当前没有选入资料；本稿没有证据摘录，也不代表已完成检索。")

    terms = _criterion_terms(clean_question)
    evidence: list[dict] = []
    analysis_evidence: list[dict] = []
    citations: list[dict] = []
    citation_seen: set[tuple[str, str, str]] = set()
    source_states: list[dict] = []
    omitted_count = 0

    for item in prepared:
        material_id = item["material_id"]
        parsed = item["parsed"]
        state = parsed["status"]
        state_record = {"material_id": material_id, "status": state, "matched_segment_count": 0}
        if state in {"unavailable", "error"}:
            limitations.append(f"{material_id}：{parsed.get('reason') or '材料不可读取。'}")
            source_states.append(state_record)
            continue
        if state == "partial":
            limitations.append(f"{material_id}：{parsed.get('reason') or '材料仅部分可读。'}")

        segments = parsed["segments"]
        matches_for_material = 0
        for segment in segments:
            text = segment["text"]
            if not text.strip():
                continue
            match = _match_segment(text, clean_question, terms, segment["label"])
            if match is None:
                continue
            matches_for_material += 1
            if len(evidence) >= _MAX_EVIDENCE_PER_CELL * max(1, len(prepared)):
                omitted_count += 1
                continue
            quote, quote_start, quote_end, truncated = _quote(text, match["positions"])
            segment_start = segment["start"]
            item_record = {
                "material_id": material_id,
                "segment_id": segment["id"],
                "label": segment["label"],
                "quote": quote,
                "segment_start": quote_start,
                "segment_end": quote_end,
                "source_start": segment_start + quote_start,
                "source_end": segment_start + quote_end,
                "truncated": truncated,
                "match_type": match["match_type"],
                "matched_terms": match["matched_terms"],
                "evidence_state": "literal_candidate",
            }
            evidence.append(item_record)
            focus_start, focus_end = _focus_bounds(text, match["positions"])
            analysis_evidence.append({
                "material_id": material_id,
                "segment_id": segment["id"],
                "label": segment["label"],
                "quote": quote,
                "kind": segment.get("kind", "unknown"),
                "full_text": text,
                "focus_start": focus_start,
                "focus_end": focus_end,
            })
            citation_key = (material_id, segment["id"], quote)
            if citation_key not in citation_seen:
                citation_seen.add(citation_key)
                citations.append({"material_id": material_id, "segment_id": segment["id"], "quote": quote})

        state_record["matched_segment_count"] = matches_for_material
        if state == "ready" and not matches_for_material:
            state_record["status"] = "no_literal_match"
        source_states.append(state_record)

    if omitted_count:
        limitations.append(
            f"另有 {omitted_count} 个词面命中未展开；每份资料最多展开 {_MAX_EVIDENCE_PER_CELL} 条，调整问题或资料范围后可继续检查。"
        )

    title = f"研究草稿：{clean_question}"
    if len(title) > 180:
        title = title[:177] + "…"
    body_lines = [
        "# 研究草稿",
        "",
        "## 研究问题",
        clean_question,
        "",
        "## 资料状态",
    ]
    if source_states:
        for state in source_states:
            count = state["matched_segment_count"]
            status = state["status"]
            body_lines.append(f"- `{state['material_id']}`：{status}；找到 {count} 个词面候选片段。")
    else:
        body_lines.append("- 尚未选入资料；可以先保存问题，之后再补充来源。")
    body_lines.extend(["", "## 可核对的原文候选"])
    if evidence:
        for item in evidence:
            quote = item["quote"].replace("\r", " ").replace("\n", " ")
            label = item["label"].replace("\r", " ").replace("\n", " ") or "原文位置未提供"
            suffix = "（摘录已截短）" if item["truncated"] else ""
            body_lines.append(
                f"- {quote} — `{item['material_id']}:{item['segment_id']}` / {label}{suffix}"
            )
    else:
        body_lines.append("- 当前没有与问题词面匹配的已解析片段；这不证明资料中不存在相关内容。")
    body_lines.extend([
        "",
        "## 待完成的独立分析",
        "- 需要回答的子问题：",
        "- 支持与相反证据：",
        "- 适用方法、期间与口径：",
        "- 可成立的判断及其前提：",
        "- 未知、局限与下一步取材：",
        "",
        "## 使用边界",
        *[f"- {item}" for item in _unique(limitations)],
    ])
    return {
        "title": title,
        "body": "\n".join(body_lines),
        "citations": citations,
        "evidence": evidence,
        "sources": source_states,
        "numeric_difference_candidates": _numeric_differences(analysis_evidence),
        "conflict_candidates": _lexical_conflicts(analysis_evidence),
        "limitations": _unique(limitations),
        "status": "draft",
        "analysis_complete": False,
    }


def _prepare_material(item: dict, index: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"materials[{index}] must be an object")
    material_id = item.get("material_id")
    parsed = item.get("parsed")
    if parsed is None and "status" in item:
        parsed = item
    if not isinstance(material_id, str) or not material_id.strip():
        raise ValueError(f"materials[{index}].material_id is required")
    if not isinstance(parsed, dict):
        parsed = {"status": "error", "reason": "未提供解析结果。", "text": "", "segments": []}
    status = parsed.get("status")
    if status not in {"ready", "partial", "unavailable", "error"}:
        status = "error"
    text = parsed.get("text")
    segments = parsed.get("segments")
    if not isinstance(text, str) or not isinstance(segments, list):
        parsed = {**parsed, "status": "error", "reason": "解析结果结构无效。", "text": "", "segments": []}
    else:
        valid_segments = []
        segment_ids = set()
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            start, end = segment.get("start"), segment.get("end")
            value, identity = segment.get("text"), segment.get("id")
            label = segment.get("label")
            if (type(start) is int and type(end) is int and 0 <= start <= end <= len(text)
                    and isinstance(value, str) and text[start:end] == value
                    and isinstance(identity, str) and identity and identity not in segment_ids
                    and isinstance(label, str)):
                valid_segments.append(segment)
                segment_ids.add(identity)
        if len(valid_segments) != len(segments):
            parsed = {**parsed, "status": "error", "reason": "引用定位与解析文本不一致。", "segments": []}
        else:
            parsed = {**parsed, "segments": valid_segments}
    parsed = {**parsed, "status": status if parsed.get("status") != "error" else "error"}
    return {"material_id": material_id.strip(), "parsed": parsed}


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _criterion_terms(criterion: str) -> list[str]:
    normalized = _normalize(criterion)
    terms: list[str] = []
    for run in _CJK_RUN.findall(normalized):
        if len(run) <= 2:
            terms.append(run)
        else:
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
    terms.extend(_TOKEN.findall(normalized))
    return list(dict.fromkeys(term for term in terms if term))


def _match_segment(text: str, criterion: str, terms: list[str], segment_label: str = "") -> dict | None:
    normalized = _normalize(text)
    label = _normalize(segment_label) if isinstance(segment_label, str) else ""
    phrase = _normalize(criterion)
    phrase_at = normalized.find(phrase) if phrase else -1
    if phrase_at >= 0:
        original_at = text.casefold().find(criterion.casefold())
        return {"match_type": "exact_phrase", "matched_terms": [criterion],
                "positions": [(original_at, original_at + len(criterion))] if original_at >= 0 else []}
    if phrase and phrase in label:
        return {"match_type": "structured_field_candidate", "matched_terms": [criterion], "positions": []}
    matched_text = [term for term in terms if term in normalized]
    matched_label = [term for term in terms if term in label]
    if not matched_text and not matched_label:
        return None
    positions = []
    raw_folded = text.casefold()
    for term in matched_text:
        at = raw_folded.find(term.casefold())
        if at >= 0:
            positions.append((at, at + len(term)))
    return {
        "match_type": "structured_field_candidate" if matched_label and not matched_text else "literal_keyword_overlap",
        "matched_terms": list(dict.fromkeys(matched_text + matched_label)),
        "positions": positions,
    }


def _quote(text: str, positions: list[tuple[int, int]], limit: int = 600) -> tuple[str, int, int, bool]:
    if len(text) <= limit:
        return text, 0, len(text), False
    center = positions[0][0] if positions else 0
    start = max(0, min(center - limit // 3, len(text) - limit))
    end = min(len(text), start + limit)
    return text[start:end], start, end, True


def _focus_bounds(text: str, positions: list[tuple[int, int]]) -> tuple[int, int]:
    """Bound value/cue extraction to the sentence or clause containing a match."""
    if not positions:
        return 0, len(text)
    match_start, match_end = positions[0]
    left = 0
    for index in range(min(match_start, len(text)) - 1, -1, -1):
        character = text[index]
        if character in "。！？；;!?\n\r，,":
            if character in ",，" and index > 0 and index + 1 < len(text):
                if text[index - 1].isdigit() and text[index + 1].isdigit():
                    continue
            left = index + 1
            break
    right = len(text)
    for index in range(max(0, match_end), len(text)):
        character = text[index]
        if character in "。！？；;!?\n\r，,":
            if character in ",，" and index > 0 and index + 1 < len(text):
                if text[index - 1].isdigit() and text[index + 1].isdigit():
                    continue
            right = index
            break
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    return left, right


def _extract_values(text: str) -> list[dict]:
    results: list[dict] = []
    covered: list[tuple[int, int]] = []
    for match in _DATE.finditer(text):
        value = match.group(0)
        if _valid_date_token(value):
            results.append({"value": value, "kind": "date", "start": match.start(), "end": match.end()})
            covered.append(match.span())
    for match in _NUMBER.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in covered):
            continue
        value = match.group(0)
        if not value or value in {"+", "-"}:
            continue
        results.append({"value": value, "kind": "number", "start": match.start(), "end": match.end()})
    return results


def _evidence_values(item: dict) -> list[dict]:
    if item.get("kind") == "xlsx_formula":
        return []
    focus_start = item.get("focus_start", 0)
    focus_end = item.get("focus_end", len(item["full_text"]))
    text = item["full_text"][focus_start:focus_end]
    if item.get("kind") == "xlsx_date":
        text = text.split("（Excel 序列值", 1)[0]
    values = _extract_values(text)
    for value in values:
        value["start"] += focus_start
        value["end"] += focus_start
    return values


def _valid_date_token(value: str) -> bool:
    digits = [int(part) for part in re.findall(r"\d+", value)]
    if len(digits) == 1:
        return 1900 <= digits[0] <= 2099
    if len(digits) == 2:
        _, month = digits
        return 1 <= month <= 12
    if len(digits) >= 3:
        _, month, day = digits[:3]
        return 1 <= month <= 12 and 1 <= day <= 31
    return False


def _append_numbers(target: list[dict], material_id: str, segment: dict,
                    text: str, offset: int = 0) -> None:
    for extracted in _extract_values(text):
        if len(target) >= _MAX_NUMBER_EVIDENCE:
            return
        quote, quote_start, quote_end, truncated = _quote(
            segment["text"], [(extracted["start"] + offset,
                               extracted["end"] + offset)], limit=180)
        target.append({
            "material_id": material_id,
            "segment_id": segment["id"],
            "label": segment.get("label"),
            "value": extracted["value"],
            "kind": extracted["kind"],
            "quote": quote,
            "quote_start": quote_start,
            "quote_end": quote_end,
            "truncated": truncated,
        })


def _numeric_differences(evidence: list[dict]) -> list[dict]:
    values: dict[str, dict[str, list[dict]]] = {}
    for item in evidence:
        for value in _evidence_values(item):
            by_material = values.setdefault(value["kind"], {}).setdefault(item["material_id"], [])
            record = {
                "value": value["value"],
                "segment_id": item["segment_id"],
                "label": item["label"],
                "quote": _quote(
                    item["full_text"].split("（Excel 序列值", 1)[0]
                    if item.get("kind") == "xlsx_date" else item["full_text"],
                    [(value["start"], value["end"])], limit=180)[0],
            }
            if record not in by_material:
                by_material.append(record)

    candidates = []
    for kind, material_values in values.items():
        if len(material_values) < 2:
            continue
        distinct = {record["value"] for records in material_values.values() for record in records}
        if len(distinct) < 2:
            continue
        candidates.append({
            "type": f"different_{kind}_values_pending_review",
            "evidence": [
                {"material_id": material_id, **record}
                for material_id, records in material_values.items()
                for record in records[:5]
            ],
            "note": "只比较命中片段的原文字面值；单位、定义、期间或样本范围可能不同。",
        })
    return candidates


def _cue_sides(text: str, positive: str, negative: str) -> set[str]:
    normalized = _normalize(text)
    sides: set[str] = set()
    negative_at = [match.span() for match in re.finditer(re.escape(negative), normalized)]
    if negative_at:
        sides.add("negative")
    positive_without_negative = normalized
    for start, end in reversed(negative_at):
        positive_without_negative = positive_without_negative[:start] + positive_without_negative[end:]
    if positive in positive_without_negative:
        sides.add("positive")
    return sides


def _lexical_conflicts(evidence: list[dict]) -> list[dict]:
    candidates = []
    for positive, negative in _OPPOSING_CUES:
        by_side: dict[str, dict[str, dict]] = {"positive": {}, "negative": {}}
        for item in evidence:
            focus = item["full_text"][item.get("focus_start", 0):
                                      item.get("focus_end", len(item["full_text"]))]
            sides = _cue_sides(focus, positive, negative)
            for side in sides:
                by_side[side].setdefault(item["material_id"], {
                    "material_id": item["material_id"],
                    "segment_id": item["segment_id"],
                    "label": item["label"],
                    "quote": item["quote"],
                    "cue": positive if side == "positive" else negative,
                })
        positive_ids = set(by_side["positive"])
        negative_ids = set(by_side["negative"])
        if positive_ids and negative_ids and (positive_ids - negative_ids or negative_ids - positive_ids):
            candidates.append({
                "type": "opposing_wording_pending_review",
                "positive_cue": positive,
                "negative_cue": negative,
                "evidence": list(by_side["positive"].values()) + list(by_side["negative"].values()),
                "note": "仅检测到明确相反用词；上下文、对象、期间和语义关系仍需人工核对。",
            })
    return candidates


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))

