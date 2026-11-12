"""Deterministic business, consumer, and budget analysis.

This module owns calculations only. Its caller supplies workspace-authorized,
versioned business tables and constraints, then keeps identity, source links,
Artifact versions, revisions, and exports in the existing business workflow.

The survey and observed-frame calculations adapt the definitions in the
read-only legacy ``canonical_metrics.py`` methods ``binomial_confidence``,
``share_of_voice``, ``net_sentiment_confidence``, ``recommendation_score`` and
``sales_market_share`` (source ranges 395-433, 436-469, 472-510, 704-776, and
779-871). They deliberately retain their stated denominator and sampling
limitations. Financial filing parsers remain upstream: rows entering this
module must retain filing entity, dimensions, period, unit/currency, filing
time, and source locator when available. This module never turns a filing
assertion into an independently verified business fact.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from calendar import monthrange
from itertools import combinations
import math
import re
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FINANCIAL_SOURCE_OPERATIONS = frozenset({
    "financial_source_analysis", "financial_fact_corrections", "financial_quarterly_series",
    "financial_mmm_analysis",
})
OPERATIONS = frozenset({
    "operating_analysis", "operating_time_series", "consumer_analysis", "financial_scenario",
    *FINANCIAL_SOURCE_OPERATIONS,
})
_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_COUNT_TEXT = re.compile(r"\d+\Z")


class BusinessAnalysisCancelled(RuntimeError):
    """Raised when the current job is cancelled before its result is committed."""


def _check_cancelled(is_cancelled) -> None:
    if is_cancelled is not None and is_cancelled():
        raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")


def evaluate_economics(operation: str, inputs: dict, *, is_cancelled=None) -> dict:
    """Evaluate one of the stable, non-AI business analysis operations.

    ``inputs`` are normalized business tables and user business constraints;
    callers should not expose these internal structures as an algorithm form.
    """
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError(f"Unknown economics operation: {operation!r}")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    if is_cancelled is not None and not callable(is_cancelled):
        raise ValueError("is_cancelled must be a callable cancellation check")
    _check_cancelled(is_cancelled)
    if operation == "operating_time_series":
        from .business_analysis_timeseries import evaluate_time_series
        return evaluate_time_series(inputs, is_cancelled=is_cancelled)
    if operation in FINANCIAL_SOURCE_OPERATIONS:
        from .business_analysis_financial import evaluate_financial
        return evaluate_financial(operation, inputs, is_cancelled=is_cancelled)
    return {
        "operating_analysis": _operating_analysis,
        "consumer_analysis": _consumer_analysis,
        "financial_scenario": _financial_scenario,
    }[operation](inputs)


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _string(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _currency(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    value = _string(value, field)
    if len(value) != 3 or not value.isascii() or not value.isalpha() or value.upper() != value:
        raise ValueError(f"{field} must be a three-letter uppercase currency code")
    return value


def _number(value: Any, field: str, *, optional: bool = False, nonnegative: bool = False) -> Decimal | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"{field} must be a finite number or None")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    if isinstance(value, str):
        value = value.strip()
        if not _NUMBER_TEXT.fullmatch(value):
            raise ValueError(f"{field} must be an unambiguous decimal value")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise ValueError(f"{field} is outside its allowed range")
    return parsed


def _count(value: Any, field: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, str) and _COUNT_TEXT.fullmatch(value.strip()):
        value = int(value.strip())
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or None")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical ISO date")
    return parsed


def _date_range(value: Any, field: str) -> tuple[date, date]:
    row = _object(value, field)
    start = _iso_date(row.get("start"), f"{field}.start")
    end = _iso_date(row.get("end"), f"{field}.end")
    if start > end:
        raise ValueError(f"{field} starts after it ends")
    return start, end


def _instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_copy(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field} contains a non-finite number")
        return _json_number(value)
    if isinstance(value, list):
        return [_json_copy(item, field) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_copy(item, field) for key, item in value.items()}
    raise ValueError(f"{field} must contain JSON-compatible values")


def _json_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValueError("calculation produced a non-finite number")
    if value == value.to_integral_value():
        try:
            return int(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("calculation is outside the JSON number range") from exc
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("calculation is outside the JSON number range")
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    # Preserve the caller's decimal scale for financial CSV/accounting values.
    return format(value, "f")


def _decimal_display(value: Any) -> str | None:
    """Render a normalized Markdown number while exact-scale strings stay in data."""
    if value is None:
        return None
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return format(decimal_value.normalize(), "f")


def _coverage(value: Any, field: str) -> dict:
    if value is None:
        value = {}
    row = _object(value, field)
    observed = _count(row.get("observed"), f"{field}.observed", optional=True)
    eligible = _count(row.get("eligible"), f"{field}.eligible", optional=True)
    if observed is not None and eligible is not None and observed > eligible:
        raise ValueError(f"{field}.observed cannot exceed {field}.eligible")
    if eligible is None:
        ratio = None
        state = "UNKNOWN_ELIGIBLE_FRAME"
    elif eligible == 0:
        ratio = None
        state = "EMPTY_ELIGIBLE_FRAME"
    elif observed is None:
        ratio = None
        state = "UNKNOWN_OBSERVED_COUNT"
    else:
        ratio = observed / eligible
        state = "MEASURED"
    return {
        "observed": observed,
        "eligible": eligible,
        "ratio": ratio,
        "state": state,
    }


def _answer(title: str, sections: list[dict], limitations: list[str]) -> dict:
    return {
        "title": title,
        "sections": sections,
        "limitations": list(dict.fromkeys(limitations)),
    }


def _section(section_id: str, title: str, body: str, data: dict) -> dict:
    return {"id": section_id, "title": title, "body": body, "data": data}


def _md_cell(value: Any) -> str:
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        rendered = format(value, ".12g")
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "本项没有可用记录。"
    head = "| " + " | ".join(_md_cell(value) for value in headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_md_cell(value) for value in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _json_number_to_text(value: Any) -> str:
    if value is None:
        return "未知"
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def _source_label(source: dict) -> str:
    if not isinstance(source, dict):
        return "来源未提供"
    name = None
    for field in ("title", "filename", "source_name", "reference", "source_ref"):
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if name is None and source.get("material") and isinstance(source["material"], dict):
        material = source["material"]
        for field in ("title", "filename"):
            value = material.get(field)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    row_number = source.get("row")
    if type(row_number) is int:
        sheet = source.get("sheet")
        location = f"{sheet} · 第{row_number}行" if isinstance(sheet, str) and sheet else f"第{row_number}行"
        return f"{name} · {location}" if name else f"原件 {location}"
    return name or ("来源已绑定" if source else "来源未提供")


def _observation_source_row(source: dict, ordinal: int) -> str:
    row_number = source.get("row") if isinstance(source, dict) else None
    sheet = source.get("sheet") if isinstance(source, dict) else None
    if type(row_number) is int:
        sheet_label = f"{sheet} · " if isinstance(sheet, str) and sheet else ""
        return f"原件 {sheet_label}第{row_number}行"
    return f"观察记录 {ordinal}"


def _requested_title(inputs: dict, fallback: str) -> str:
    title = inputs.get("title")
    return _string(title, "title") if title is not None else fallback


def _operating_observation(value: Any, index: int) -> dict:
    field = f"observations[{index}]"
    row = _object(value, field)
    identity = _string(row.get("id"), f"{field}.id")
    entity = _object(row.get("entity"), f"{field}.entity")
    entity_id = _string(entity.get("id"), f"{field}.entity.id")
    entity_name = _string(entity.get("name"), f"{field}.entity.name")
    metric = _object(row.get("metric"), f"{field}.metric")
    metric_key = _string(metric.get("key"), f"{field}.metric.key")
    definition = _string(metric.get("definition"), f"{field}.metric.definition")
    unit = _string(metric.get("unit"), f"{field}.metric.unit")
    currency = _currency(metric.get("currency"), f"{field}.metric.currency", optional=True)
    basis = metric.get("basis")
    if basis not in {"actual", "estimate", "forecast", "unknown"}:
        raise ValueError(f"{field}.metric.basis is invalid")
    denominator = _string(metric.get("denominator"), f"{field}.metric.denominator", optional=True)
    raw_dimensions = metric.get("comparison_dimensions", [])
    if (
        "comparison_dimensions" not in metric
        or not isinstance(raw_dimensions, list)
        or any(not isinstance(item, str) or not item.strip() for item in raw_dimensions)
        or len(raw_dimensions) != len(set(raw_dimensions))
    ):
        raise ValueError(f"{field}.metric.comparison_dimensions must be unique non-empty strings")
    period_start, period_end = _date_range(row.get("period"), f"{field}.period")
    frequency = row["period"].get("frequency", "custom")
    if frequency not in {"day", "week", "month", "quarter", "year", "custom"}:
        raise ValueError(f"{field}.period.frequency is invalid")
    period_length = (period_end - period_start).days + 1
    period_timezone = _string(row["period"].get("timezone"), f"{field}.period.timezone", optional=True)
    valid_frequency = (
        frequency == "custom"
        or (frequency == "day" and period_length == 1)
        or (frequency == "week" and period_length == 7)
        or (frequency == "month" and period_start.day == 1 and period_end.day == monthrange(period_end.year, period_end.month)[1])
        or (frequency == "quarter" and (
            period_start.day == 1 and period_start.month in {1, 4, 7, 10}
            and period_start.year == period_end.year
            and period_end.month == period_start.month + 2
            and period_end.day == monthrange(period_end.year, period_end.month)[1]
        ))
        or (frequency == "year" and (
            (period_start.month, period_start.day) == (1, 1)
            and (period_end.month, period_end.day) == (12, 31)
        ))
    )
    if not valid_frequency:
        raise ValueError(f"{field}.period does not match its declared frequency")
    scope = _json_copy(_object(row.get("scope", {}), f"{field}.scope"), f"{field}.scope")
    source = _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source")
    review = row.get("review", {})
    review = _object(review, f"{field}.review")
    review_status = row.get("review_status", review.get("status", "not_supplied"))
    if not isinstance(review_status, str) or review_status not in {
        "supported", "needs_review", "conflicted", "not_supplied", "withdrawn"
    }:
        raise ValueError(f"{field}.review status is invalid")
    return {
        "id": identity,
        "entity": {"id": entity_id, "name": entity_name},
        "metric": {
            "key": metric_key,
            "definition": definition,
            "value": _number(metric.get("value"), f"{field}.metric.value", optional=True),
            "unit": unit,
            "currency": currency,
            "basis": basis,
            "denominator": denominator,
            "comparison_dimensions": list(raw_dimensions),
        },
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "frequency": frequency,
            "timezone": period_timezone,
        },
        "_period_start": period_start,
        "_period_end": period_end,
        "scope": scope,
        "coverage": _coverage(row.get("coverage"), f"{field}.coverage"),
        "source": source,
        "review_status": review_status,
        "comparison_group": _string(row.get("comparison_group"), f"{field}.comparison_group", optional=True),
    }


def _public_observation(row: dict) -> dict:
    metric = row["metric"]
    return {
        "id": row["id"],
        "entity": row["entity"],
        "metric": {
            "key": metric["key"],
            "definition": metric["definition"],
            "value": _json_number(metric["value"]),
            "unit": metric["unit"],
            "currency": metric["currency"],
            "basis": metric["basis"],
            "denominator": metric["denominator"],
            "comparison_dimensions": metric["comparison_dimensions"],
        },
        "period": row["period"],
        "scope": row["scope"],
        "coverage": row["coverage"],
        "source": row["source"],
        "review_status": row["review_status"],
    }


def _comparison_result(baseline: dict, current: dict, kind: str) -> dict:
    reasons = []
    a_metric, b_metric = baseline["metric"], current["metric"]
    for name, a_value, b_value in (
        ("metric_key", a_metric["key"], b_metric["key"]),
        ("metric_definition", a_metric["definition"], b_metric["definition"]),
        ("unit", a_metric["unit"], b_metric["unit"]),
        ("currency", a_metric["currency"], b_metric["currency"]),
        ("measurement_basis", a_metric["basis"], b_metric["basis"]),
        ("denominator", a_metric["denominator"], b_metric["denominator"]),
    ):
        if a_value != b_value:
            reasons.append(f"{name}_differs")
    if a_metric["comparison_dimensions"] != b_metric["comparison_dimensions"]:
        reasons.append("comparison_dimensions_differ")
    if a_metric["basis"] == "unknown":
        reasons.append("measurement_basis_unknown")
    for dimension in a_metric["comparison_dimensions"]:
        if dimension not in baseline["scope"] or dimension not in current["scope"]:
            reasons.append(f"scope_{dimension}_unknown")
        elif baseline["scope"][dimension] != current["scope"][dimension]:
            reasons.append(f"scope_{dimension}_differs")
    if baseline["scope"] != current["scope"]:
        reasons.append("operating_scope_differs")
    baseline_timezone = baseline["period"]["timezone"]
    current_timezone = current["period"]["timezone"]
    if baseline_timezone is None or current_timezone is None:
        reasons.append("period_timezone_unknown")
    elif baseline_timezone != current_timezone:
        reasons.append("period_timezone_differs")
    if baseline["review_status"] in {"needs_review", "conflicted", "withdrawn"}:
        reasons.append(f"baseline_{baseline['review_status']}")
    if current["review_status"] in {"needs_review", "conflicted", "withdrawn"}:
        reasons.append(f"current_{current['review_status']}")
    if kind == "same_period":
        if baseline["period"] != current["period"]:
            reasons.append("period_differs")
        if baseline["entity"]["id"] == current["entity"]["id"]:
            reasons.append("entities_are_the_same")
    elif kind == "period_change":
        if baseline["entity"]["id"] != current["entity"]["id"]:
            reasons.append("entity_differs")
        first_period = baseline["period"]
        next_period = current["period"]
        if first_period["frequency"] != next_period["frequency"]:
            reasons.append("period_frequency_differs")
        elif first_period["frequency"] in {"day", "week", "custom"}:
            if (baseline["_period_end"] - baseline["_period_start"]) != (
                current["_period_end"] - current["_period_start"]
            ):
                reasons.append("period_length_differs")
            if baseline["_period_end"].toordinal() + 1 != current["_period_start"].toordinal():
                reasons.append("periods_are_not_adjacent")
        elif baseline["_period_end"].toordinal() + 1 != current["_period_start"].toordinal():
            reasons.append("periods_are_not_adjacent")
    else:
        raise ValueError("comparison kind must be same_period or period_change")

    baseline_value = a_metric["value"]
    current_value = b_metric["value"]
    if baseline_value is None:
        reasons.append("baseline_value_unknown")
    if current_value is None:
        reasons.append("current_value_unknown")
    reasons = list(dict.fromkeys(reasons))
    comparable = not reasons
    delta = current_value - baseline_value if comparable else None
    if not comparable:
        relative_state = "NOT_COMPARABLE"
        percent = None
    elif baseline_value == 0:
        relative_state = "BASELINE_ZERO"
        percent = None
    elif baseline_value < 0:
        relative_state = "NEGATIVE_BASELINE"
        percent = None
    else:
        relative_state = "AVAILABLE"
        percent = delta / baseline_value * Decimal(100)
    return {
        "baseline_id": baseline["id"],
        "current_id": current["id"],
        "kind": kind,
        "comparable": comparable,
        "reasons": reasons,
        "baseline_value": _json_number(baseline_value),
        "current_value": _json_number(current_value),
        "delta": _json_number(delta),
        "change_percent": _json_number(percent),
        "change_percent_state": relative_state,
        "interpretation": "descriptive_only",
        "causal_effect": None,
        "baseline_coverage": baseline["coverage"],
        "current_coverage": current["coverage"],
    }


def _market_sales_row(value: Any, index: int) -> dict:
    field = f"market_sales[{index}]"
    row = _object(value, field)
    identity = _string(row.get("id"), f"{field}.id")
    basis = row.get("sales_basis")
    if basis not in {"revenue", "units"}:
        raise ValueError(f"{field}.sales_basis must be revenue or units")
    numerator = _number(row.get("numerator"), f"{field}.numerator", optional=True, nonnegative=True)
    market_total = _number(row.get("market_total"), f"{field}.market_total", optional=True, nonnegative=True)
    currency = _currency(row.get("currency"), f"{field}.currency", optional=True)
    unit = _string(row.get("unit"), f"{field}.unit", optional=True)
    if basis == "revenue" and currency is None:
        raise ValueError(f"{field}.currency is required for revenue share")
    if basis == "units" and currency is not None:
        raise ValueError(f"{field}.currency must be None for unit share")
    if basis == "units":
        if any(value is not None and value != value.to_integral_value() for value in (numerator, market_total)):
            raise ValueError(f"{field} unit counts must be integers")
        if unit is None:
            raise ValueError(f"{field}.unit is required for unit share")
    if numerator is not None and market_total is not None and numerator > market_total:
        raise ValueError(f"{field}.numerator cannot exceed market_total")
    start, end = _date_range(row.get("period"), f"{field}.period")
    period_timezone = _string(
        row.get("period", {}).get("timezone"), f"{field}.period.timezone", optional=True
    )
    scope = {
        "entity": _string(row.get("entity"), f"{field}.entity"),
        "geography": _string(row.get("geography"), f"{field}.geography"),
        "category": _string(row.get("category"), f"{field}.category"),
        "market_definition": _string(row.get("market_definition"), f"{field}.market_definition"),
        "period": {
            "start": start.isoformat(), "end": end.isoformat(), "timezone": period_timezone
        },
        "sales_basis": basis,
        "currency": currency,
        "unit": unit,
    }
    status = row.get("status")
    if status not in {"actual", "estimate"}:
        raise ValueError(f"{field}.status must explicitly identify actual or estimate")
    if numerator is not None and market_total == 0:
        state, share = "ZERO_MARKET_DENOMINATOR", None
    elif numerator is None or market_total is None:
        state, share = "INCOMPLETE_SALES_VALUES", None
    else:
        state, share = ("OBSERVED" if status == "actual" else "CONDITIONAL_ESTIMATE"), numerator / market_total
    return {
        "id": identity,
        "scope": scope,
        "numerator": _json_number(numerator),
        "market_total": _json_number(market_total),
        "share": _json_number(share),
        "state": state,
        "interval": None,
        "source": _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source"),
    }


def _voice_sample(value: Any, index: int) -> dict:
    field = f"voice_samples[{index}]"
    row = _object(value, field)
    identity = _string(row.get("id"), f"{field}.id")
    counts = _object(row.get("counts"), f"{field}.counts")
    if not counts:
        raise ValueError(f"{field}.counts must not be empty")
    normalized_counts = {}
    for name, amount in counts.items():
        brand = _string(name, f"{field}.counts key")
        count = _count(amount, f"{field}.counts[{brand}]")
        normalized_counts[brand] = count
    raw_brand_labels = row.get("brand_labels", {})
    if not isinstance(raw_brand_labels, dict) or set(raw_brand_labels) - set(normalized_counts):
        raise ValueError(f"{field}.brand_labels must name only counted brands")
    brand_labels = {
        brand: _string(raw_brand_labels.get(brand, brand), f"{field}.brand_labels.{brand}")
        for brand in normalized_counts
    }
    if len(set(brand_labels.values())) != len(brand_labels):
        raise ValueError(f"{field}.brand_labels must identify distinct brands")
    coverage = row.get("platform_coverage")
    if (
        not isinstance(coverage, list)
        or not coverage
        or any(not isinstance(item, str) or not item.strip() for item in coverage)
        or len(coverage) != len(set(coverage))
    ):
        raise ValueError(f"{field}.platform_coverage must contain unique platform names")
    start, end = _date_range(row.get("window"), f"{field}.window")
    window_timezone = _string(
        row.get("window", {}).get("timezone"), f"{field}.window.timezone", optional=True
    )
    counting_rule = _string(row.get("counting_rule"), f"{field}.counting_rule")
    deduplication_rule = _string(row.get("deduplication_rule"), f"{field}.deduplication_rule")
    count_unit = row.get("count_unit")
    if count_unit != "exclusive_observed_brand_mentions":
        raise ValueError(f"{field}.count_unit must identify exclusive observed brand mentions")
    unclassified = _count(row.get("unclassified_count"), f"{field}.unclassified_count", optional=True)
    denominator = sum(normalized_counts.values())
    shares = {
        brand: _json_number(Decimal(count) / denominator) if denominator else None
        for brand, count in sorted(normalized_counts.items())
    }
    return {
        "id": identity,
        "window": {
            "start": start.isoformat(), "end": end.isoformat(), "timezone": window_timezone
        },
        "platform_coverage": list(coverage),
        "count_unit": count_unit,
        "counting_rule": counting_rule,
        "deduplication_rule": deduplication_rule,
        "counts": normalized_counts,
        "brand_labels": brand_labels,
        "source": _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source"),
        "classified_mentions": denominator,
        "unclassified_count": unclassified,
        "shares": shares,
        "state": (
            "NO_CLASSIFIED_MENTIONS" if denominator == 0
            else "UNCLASSIFIED_COUNT_UNKNOWN" if unclassified is None
            else "PARTIAL_CLASSIFIED_FRAME" if unclassified else "OBSERVED_FRAME_ONLY"
        ),
        "population_share": None,
    }


_MARKET_SUBJECT_KINDS = frozenset({"brand", "store", "commercial_area", "channel", "category", "service"})
_MARKET_REFERENCE_ROLES = frozenset({"direct", "target", "cultural", "attention", "excluded"})
_MARKET_COMPARISON_DIMENSIONS = frozenset({
    "subject_kind", "analysis_unit", "geography", "category", "use_case", "price_band",
    "channel", "purchase_unit", "period", "operating_state", "catchment_definition",
    "travel_mode", "travel_time_minutes",
})
_MARKET_DIMENSION_LABELS = {
    "subject_kind": "对象类型", "analysis_unit": "分析单位", "geography": "地区/经营市场",
    "category": "品类/业态", "use_case": "用途/购买任务", "price_band": "价格带",
    "channel": "渠道", "purchase_unit": "购买单位", "period": "期间/时区",
    "operating_state": "经营状态", "catchment_definition": "商圈边界定义",
    "travel_mode": "到达方式", "travel_time_minutes": "通行时间边界",
}


def _market_dimension_value(subject: dict, dimension: str):
    value = subject.get(dimension)
    if value is None or (isinstance(value, str) and value in {"unknown", "未知", "未声明"}):
        return None
    if dimension == "period" and isinstance(value, dict):
        if value.get("timezone") is None:
            return None
        return (value.get("start"), value.get("end"), value.get("timezone"))
    return value


def _market_comparability_label(state: str) -> str:
    return {
        "NOT_COMPARABLE_AS_DIRECT_REFERENCE": "范围不一致，不可作直接对标",
        "SCOPE_INCOMPLETE": "范围资料不全，比较资格未知",
        "SCOPE_MATCH_REQUIRES_METRIC_CHECK": "已声明范围一致；仍需核指标、分母和来源",
        "EXCLUDED_FROM_THIS_COMPARISON": "调用方已排除",
        "QUALITATIVE_REFERENCE_ONLY": "仅作定性机制参考，不参与数值排名",
    }.get(state, "比较资格未知")


def _market_reason_label(reason: str) -> str:
    if reason.startswith("scope_differs:"):
        names = [part for part in reason.split(":", 1)[1].split(",") if part]
        return "范围不同：" + "、".join(_MARKET_DIMENSION_LABELS.get(name, name) for name in names)
    if reason.startswith("scope_unknown:"):
        names = [part for part in reason.split(":", 1)[1].split(",") if part]
        return "范围未核：" + "、".join(_MARKET_DIMENSION_LABELS.get(name, name) for name in names)
    return {
        "same_declared_scope; metric, denominator, data coverage, and source quality still require separate matching": "范围声明相同；指标、分母、覆盖和来源质量仍需另核",
        "reference_role_is_not_a_direct_comparable_or_numeric_rank": "该角色仅支持机制参考，不作直接数值比较",
        "caller_excluded_this_reference": "调用方已排除该对象",
    }.get(reason, reason)


def _market_layer_state_label(state: str) -> str:
    return {
        "observed": "有来源的观察值",
        "estimate": "估计值",
        "hypothesis": "待验证假设",
        "unknown": "未知",
        "not_provided": "未提供",
    }.get(state, state)


def _market_boundary_state_label(state: str) -> str:
    return {
        "BOUNDARY_LEVELS_NOT_PROVIDED": "未提供三层边界值",
        "BOUNDARY_LEVELS_NOT_COMPARABLE": "三层值口径不可直接比较",
        "BOUNDARY_ORDER_CONFLICT": "三层值顺序冲突",
        "DECLARED_BOUNDARY_ORDER_CONSISTENT": "声明的层级顺序一致",
        "PARTIAL_BOUNDARY_LEVELS": "仅部分层级有值",
    }.get(state, "边界关系未知")


def _market_layer_order_reason_label(reason: str) -> str:
    if reason.startswith("different_fields:") or reason.startswith("unknown_fields:"):
        translated = []
        for field in re.findall(r"(?:different_fields|unknown_fields):([^;]*)", reason):
            translated.extend(_MARKET_DIMENSION_LABELS.get(name, name) for name in field.split(",") if name)
        return "层级口径有差异或缺失：" + "、".join(dict.fromkeys(translated))
    return {
        "no_user_supplied_layer_values": "未提供带来源的分层规模值",
        "one_or_more_boundary_values_are_unknown": "至少一层的数值未知，不能核算层级顺序",
        "declared_total_reachable_serviceable_values_are_not_nested": "所报总市场、可触达市场、可服务市场数值不满足嵌套顺序",
        "same_declared_basis_and_scope; not independently verified": "声明的口径和范围一致；数值关系仍是来源声明，未独立核实",
        "not_all_three_market_boundary_levels_were_supplied": "三层市场边界未全部提供",
    }.get(reason, reason)


def _market_subject(value: Any, field: str) -> dict:
    row = _object(value, field)
    allowed = _MARKET_COMPARISON_DIMENSIONS | {"id", "name", "source", "source_refs"}
    if set(row) - allowed:
        raise ValueError(f"{field} contains unsupported fields")
    identity = _string(row.get("id"), f"{field}.id")
    name = _string(row.get("name"), f"{field}.name")
    kind = row.get("subject_kind")
    if kind not in _MARKET_SUBJECT_KINDS:
        raise ValueError(f"{field}.subject_kind is invalid")
    period = row.get("period")
    if period is not None:
        start, end = _date_range(period, f"{field}.period")
        period = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": _string(period.get("timezone"), f"{field}.period.timezone", optional=True),
        }
    operating_state = row.get("operating_state")
    if operating_state is not None and operating_state not in {"open", "closed", "renovating", "seasonal", "unknown"}:
        raise ValueError(f"{field}.operating_state is invalid")
    travel_time = row.get("travel_time_minutes")
    if travel_time is not None:
        travel_time = _count(travel_time, f"{field}.travel_time_minutes")
        if travel_time <= 0:
            raise ValueError(f"{field}.travel_time_minutes must be positive")
    sources = row.get("source_refs")
    if sources is None:
        source = row.get("source")
        sources = [source] if isinstance(source, dict) and source else []
    if not isinstance(sources, list):
        raise ValueError(f"{field}.source_refs must be an array")
    normalized_sources = [_json_copy(_object(source, f"{field}.source_refs[{index}]"), f"{field}.source_refs[{index}]") for index, source in enumerate(sources)]
    return {
        "id": identity,
        "name": name,
        "subject_kind": kind,
        "analysis_unit": _string(row.get("analysis_unit"), f"{field}.analysis_unit", optional=True),
        "geography": _string(row.get("geography"), f"{field}.geography", optional=True),
        "category": _string(row.get("category"), f"{field}.category", optional=True),
        "use_case": _string(row.get("use_case"), f"{field}.use_case", optional=True),
        "price_band": _string(row.get("price_band"), f"{field}.price_band", optional=True),
        "channel": _string(row.get("channel"), f"{field}.channel", optional=True),
        "purchase_unit": _string(row.get("purchase_unit"), f"{field}.purchase_unit", optional=True),
        "period": period,
        "operating_state": operating_state,
        "catchment_definition": _string(row.get("catchment_definition"), f"{field}.catchment_definition", optional=True),
        "travel_mode": _string(row.get("travel_mode"), f"{field}.travel_mode", optional=True),
        "travel_time_minutes": travel_time,
        "source_refs": normalized_sources,
    }


def _market_layer(value: Any, index: int) -> dict:
    field = f"market_boundary.layers[{index}]"
    row = _object(value, field)
    allowed = {
        "layer", "value", "unit", "currency", "status", "definition", "basis", "coverage",
        "period", "geography", "category", "purchase_unit", "source", "source_refs",
    }
    if set(row) - allowed:
        raise ValueError(f"{field} contains unsupported fields")
    layer = row.get("layer")
    if layer not in {"total_market", "reachable_market", "serviceable_market"}:
        raise ValueError(f"{field}.layer is invalid")
    status = row.get("status")
    if status not in {"observed", "estimate", "hypothesis", "unknown"}:
        raise ValueError(f"{field}.status must distinguish observed, estimate, hypothesis, or unknown")
    amount = _number(row.get("value"), f"{field}.value", optional=True, nonnegative=True)
    if status == "unknown" and amount is not None:
        raise ValueError(f"{field}.unknown status cannot carry a numeric value")
    if status != "unknown" and amount is None:
        raise ValueError(f"{field}.value is required for an observed, estimate, or hypothesis layer")
    currency = _currency(row.get("currency"), f"{field}.currency", optional=True)
    unit = _string(row.get("unit"), f"{field}.unit")
    if re.fullmatch(r"[A-Z]{3}", unit) and currency is None:
        raise ValueError(f"{field}.currency must be explicit for monetary units")
    period = row.get("period")
    if period is not None:
        start, end = _date_range(period, f"{field}.period")
        period = {"start": start.isoformat(), "end": end.isoformat(),
                  "timezone": _string(period.get("timezone"), f"{field}.period.timezone", optional=True)}
    source_refs = row.get("source_refs")
    if source_refs is None:
        source = row.get("source")
        source_refs = [source] if isinstance(source, dict) and source else []
    if not isinstance(source_refs, list):
        raise ValueError(f"{field}.source_refs must be an array")
    normalized_sources = [_json_copy(_object(source, f"{field}.source_refs[{i}]"), f"{field}.source_refs[{i}]") for i, source in enumerate(source_refs)]
    basis = _string(row.get("basis"), f"{field}.basis", optional=True)
    if status == "observed" and not any(normalized_sources):
        raise ValueError(f"{field}.source_refs are required for an observed market value")
    coverage = _string(row.get("coverage"), f"{field}.coverage", optional=True)
    if status == "observed" and coverage is None:
        raise ValueError(f"{field}.coverage is required for an observed market value")
    if status in {"estimate", "hypothesis"} and basis is None:
        raise ValueError(f"{field}.basis must state the source or assumption behind an estimate")
    return {
        "layer": layer,
        "value": _json_number(amount),
        "value_decimal": _decimal_text(amount),
        "unit": unit,
        "currency": currency,
        "status": status,
        "definition": _string(row.get("definition"), f"{field}.definition"),
        "basis": basis,
        "coverage": coverage,
        "period": period,
        "geography": _string(row.get("geography"), f"{field}.geography", optional=True),
        "category": _string(row.get("category"), f"{field}.category", optional=True),
        "purchase_unit": _string(row.get("purchase_unit"), f"{field}.purchase_unit", optional=True),
        "source_refs": normalized_sources,
    }


def _market_boundary(value: Any) -> tuple[dict, list[dict], list[str]]:
    row = _object(value, "market_boundary")
    allowed = {
        "question", "focal_subject", "required_dimensions", "references", "layers",
        "assumptions", "limitations",
    }
    if set(row) - allowed:
        raise ValueError("market_boundary contains unsupported fields")
    question = _string(row.get("question"), "market_boundary.question", optional=True)
    focal = _market_subject(row.get("focal_subject"), "market_boundary.focal_subject")
    raw_dimensions = row.get("required_dimensions")
    if raw_dimensions is None:
        dimensions = ["subject_kind", "analysis_unit", "geography", "category", "use_case", "price_band", "channel", "purchase_unit", "period"]
        if focal["subject_kind"] in {"store", "commercial_area"}:
            dimensions.append("operating_state")
        if focal["subject_kind"] == "commercial_area":
            dimensions.extend(["catchment_definition", "travel_mode", "travel_time_minutes"])
    else:
        if not isinstance(raw_dimensions, list) or not raw_dimensions or any(item not in _MARKET_COMPARISON_DIMENSIONS for item in raw_dimensions):
            raise ValueError("market_boundary.required_dimensions must contain known business scope dimensions")
        if len(raw_dimensions) != len(set(raw_dimensions)):
            raise ValueError("market_boundary.required_dimensions cannot repeat a dimension")
        dimensions = list(dict.fromkeys(["subject_kind", "analysis_unit", *raw_dimensions]))

    references = row.get("references", [])
    if not isinstance(references, list):
        raise ValueError("market_boundary.references must be an array")
    seen_ids = {focal["id"]}
    normalized_references = []
    comparison_rows = []
    for index, raw_reference in enumerate(references):
        field = f"market_boundary.references[{index}]"
        ref_row = _object(raw_reference, field)
        allowed_reference = set(_MARKET_COMPARISON_DIMENSIONS) | {"id", "name", "source", "source_refs", "role", "basis_reason"}
        if set(ref_row) - allowed_reference:
            raise ValueError(f"{field} contains unsupported fields")
        role = ref_row.get("role")
        if role not in _MARKET_REFERENCE_ROLES:
            raise ValueError(f"{field}.role is invalid")
        subject_data = {key: value for key, value in ref_row.items() if key not in {"role", "basis_reason"}}
        subject = _market_subject(subject_data, field)
        if subject["id"] in seen_ids:
            raise ValueError("market boundary subject IDs must be unique")
        seen_ids.add(subject["id"])
        basis_reason = _string(ref_row.get("basis_reason"), f"{field}.basis_reason", optional=True)
        reasons = []
        differences = []
        unknown = []
        for dimension in dimensions:
            base_value = _market_dimension_value(focal, dimension)
            ref_value = _market_dimension_value(subject, dimension)
            if base_value is None or ref_value is None:
                unknown.append(dimension)
            elif base_value != ref_value:
                differences.append(dimension)
        if role == "direct":
            state = (
                "NOT_COMPARABLE_AS_DIRECT_REFERENCE" if differences
                else "SCOPE_INCOMPLETE" if unknown
                else "SCOPE_MATCH_REQUIRES_METRIC_CHECK"
            )
            if differences:
                reasons.append("scope_differs:" + ",".join(differences))
            if unknown:
                reasons.append("scope_unknown:" + ",".join(unknown))
            if not reasons:
                reasons.append("same_declared_scope; metric, denominator, data coverage, and source quality still require separate matching")
        elif role == "excluded":
            state = "EXCLUDED_FROM_THIS_COMPARISON"
            reasons.append(basis_reason or "caller_excluded_this_reference")
        else:
            state = "QUALITATIVE_REFERENCE_ONLY"
            reasons.append("reference_role_is_not_a_direct_comparable_or_numeric_rank")
            if differences:
                reasons.append("scope_differs:" + ",".join(differences))
            if unknown:
                reasons.append("scope_unknown:" + ",".join(unknown))
        normalized = {**subject, "role": role, "basis_reason": basis_reason, "state": state,
                      "scope_differences": differences, "scope_unknowns": unknown, "reasons": reasons}
        normalized_references.append(normalized)
        comparison_rows.append([
            subject["name"], subject["subject_kind"], {
                "direct": "直接对标", "target": "目标参考", "cultural": "文化参考",
                "attention": "注意力替代", "excluded": "已排除",
            }[role], subject["geography"], subject["category"],
            subject["price_band"], subject["channel"],
            f"{subject['period']['start']}–{subject['period']['end']}" if subject["period"] else None,
            _market_comparability_label(state), "；".join(_market_reason_label(reason) for reason in reasons),
            "; ".join(_source_label(source) for source in subject["source_refs"]),
        ])

    raw_layers = row.get("layers", [])
    if not isinstance(raw_layers, list):
        raise ValueError("market_boundary.layers must be an array")
    layers = {}
    for index, raw_layer in enumerate(raw_layers):
        layer = _market_layer(raw_layer, index)
        if layer["layer"] in layers:
            raise ValueError("market boundary layers must be unique")
        layers[layer["layer"]] = layer
    for layer_id in ("total_market", "reachable_market", "serviceable_market"):
        layers.setdefault(layer_id, {
            "layer": layer_id, "value": None, "unit": None, "currency": None,
            "value_decimal": None,
            "status": "not_provided", "definition": None, "basis": None, "coverage": None,
            "period": None, "geography": None, "category": None, "purchase_unit": None,
            "source_refs": [],
        })
    ordered_layers = [layers[item] for item in ("total_market", "reachable_market", "serviceable_market")]
    provided = [item for item in ordered_layers if item["status"] != "not_provided"]
    layer_order_state = "BOUNDARY_LEVELS_NOT_PROVIDED"
    layer_order_reason = "no_user_supplied_layer_values"
    if len(provided) == 3 and any(item["value_decimal"] is None for item in provided):
        layer_order_state = "BOUNDARY_LEVELS_NOT_COMPARABLE"
        layer_order_reason = "one_or_more_boundary_values_are_unknown"
    elif len(provided) == 3:
        compare_fields = ("unit", "currency", "status", "basis", "period", "geography", "category", "purchase_unit")
        conflicts = [field for field in compare_fields if len({repr(item.get(field)) for item in provided}) > 1]
        unknown_fields = [
            field for field in compare_fields
            if field != "currency" and any(
                item.get(field) is None
                or field == "period" and isinstance(item.get("period"), dict) and item["period"].get("timezone") is None
                for item in provided
            )
        ]
        if conflicts or unknown_fields:
            layer_order_state = "BOUNDARY_LEVELS_NOT_COMPARABLE"
            layer_order_reason = "different_fields:" + ",".join(conflicts) + ";unknown_fields:" + ",".join(unknown_fields)
        else:
            total_value, reachable_value, serviceable_value = (Decimal(item["value_decimal"]) for item in provided)
            if total_value < reachable_value or reachable_value < serviceable_value:
                layer_order_state = "BOUNDARY_ORDER_CONFLICT"
                layer_order_reason = "declared_total_reachable_serviceable_values_are_not_nested"
            else:
                layer_order_state = "DECLARED_BOUNDARY_ORDER_CONSISTENT"
                layer_order_reason = "same_declared_basis_and_scope; not independently verified"
    elif provided:
        layer_order_state = "PARTIAL_BOUNDARY_LEVELS"
        layer_order_reason = "not_all_three_market_boundary_levels_were_supplied"

    assumptions = row.get("assumptions", [])
    limitations = row.get("limitations", [])
    for field, items in (("assumptions", assumptions), ("limitations", limitations)):
        if not isinstance(items, list):
            raise ValueError(f"market_boundary.{field} must be an array")
    normalized = {
        "question": question,
        "focal_subject": focal,
        "required_dimensions": dimensions,
        "references": normalized_references,
        "layers": ordered_layers,
        "layer_order_state": layer_order_state,
        "layer_order_reason": layer_order_reason,
        "assumptions": [_string(item, f"market_boundary.assumptions[{index}]") for index, item in enumerate(assumptions)],
        "limitations": [_string(item, f"market_boundary.limitations[{index}]") for index, item in enumerate(limitations)],
    }
    layer_rows = [[
        {"total_market": "总市场", "reachable_market": "可触达市场", "serviceable_market": "可服务市场"}[item["layer"]],
        item["value_decimal"], item["unit"], item["currency"], _market_layer_state_label(item["status"]), item["definition"],
        item["basis"], item["coverage"],
        f"{item['period']['start']}–{item['period']['end']}" if item["period"] else None,
        item["geography"], item["category"], item["purchase_unit"],
        "; ".join(_source_label(source) for source in item["source_refs"]),
    ] for item in ordered_layers]
    focal_period_label = (
        f"{focal['period']['start']}–{focal['period']['end']} ({focal['period']['timezone'] or '时区未知'})"
        if focal["period"] else "未知"
    )
    scope_text = (
        f"对象：{focal['name']}（{focal['subject_kind']}；分析单位={focal['analysis_unit'] or '未知'}）；地区：{focal['geography'] or '未知'}；"
        f"品类：{focal['category'] or '未知'}；用途：{focal['use_case'] or '未知'}；"
        f"价格带：{focal['price_band'] or '未知'}；渠道：{focal['channel'] or '未知'}；"
        f"购买单位：{focal['purchase_unit'] or '未知'}；期间：{focal_period_label}；"
        f"经营状态：{focal['operating_state'] or '未知'}；商圈边界：{focal['catchment_definition'] or '未声明'}；"
        f"来源：{'; '.join(_source_label(source) for source in focal['source_refs']) or '未提供'}。"
    )
    sections = [
        _section(
            "operating.market_boundary",
            "市场范围与总量依据",
            (f"{question}\n\n" if question else "") + scope_text +
            f"\n\n三层市场值的顺序核对：{_market_boundary_state_label(layer_order_state)}（{_market_layer_order_reason_label(layer_order_reason)}）。本操作不从行业均值、搜索热度或竞品收入补造缺失的总量。\n\n"+
            _md_table(["市场层次", "数值", "单位", "币种", "性质", "定义", "口径依据", "覆盖声明", "期间", "地区", "品类", "购买单位", "来源"], layer_rows) +
            ("\n\n待检验假设：\n\n" + "\n".join(f"- {item}" for item in normalized["assumptions"]) if normalized["assumptions"] else ""),
            {"market_boundary": normalized},
        ),
        _section(
            "operating.market_comparability",
            "品牌、门店与商圈比较边界",
            "直接比较仅在所选的主体、地区、品类/用途、价格带、渠道、购买单位、期间和商圈定义均有同口径声明时标记为范围匹配；范围匹配不证明数据分母或测量质量相同。目标、文化和注意力参考用于机制研究，不能放进直接对标总榜。\n\n"+
            _md_table(["参考对象", "对象类型", "参考角色", "地区", "品类", "价格带", "渠道", "期间", "范围判断", "原因/边界差异", "来源"], comparison_rows),
            {"focal_subject": focal, "required_dimensions": dimensions, "references": normalized_references},
        ),
    ]
    limits = [
        "市场边界和覆盖均来自调用方资料/声明；本计算不联网补市场总量，也不独立验证地域、品类或可服务能力。",
        "范围匹配仅表示所选业务维度声明一致；数值比较仍须匹配指标定义、期间、单位/币种、分母、样本覆盖及来源质量。",
        "总市场、可触达市场和可服务市场的估计值不互相推导；估计或假设不会显示为已观察事实。",
        "目标、文化和注意力参考只供定性机制比较，不生成与直接竞品混排的份额/总榜。",
        *normalized["limitations"],
    ]
    return normalized, sections, limits


def _operating_analysis(inputs: dict) -> dict:
    allowed = {"title", "question", "observations", "comparisons", "market_sales", "voice_samples", "market_boundary"}
    if set(inputs) - allowed:
        raise ValueError("operating_analysis contains unsupported fields")
    question = _string(inputs.get("question"), "question", optional=True)
    raw_observations = inputs.get("observations", [])
    if not isinstance(raw_observations, list):
        raise ValueError("observations must be an array")
    observations = [_operating_observation(row, i) for i, row in enumerate(raw_observations)]
    by_id = {row["id"]: row for row in observations}
    if len(by_id) != len(observations):
        raise ValueError("observation IDs must be unique")

    raw_comparisons = inputs.get("comparisons")
    if raw_comparisons is not None and not isinstance(raw_comparisons, list):
        raise ValueError("comparisons must be an array")
    comparison_specs = []
    if raw_comparisons:
        seen = set()
        for index, raw in enumerate(raw_comparisons):
            row = _object(raw, f"comparisons[{index}]")
            baseline_id = _string(row.get("baseline_id"), f"comparisons[{index}].baseline_id")
            current_id = _string(row.get("current_id"), f"comparisons[{index}].current_id")
            kind = row.get("kind")
            if kind not in {"same_period", "period_change"}:
                raise ValueError(f"comparisons[{index}].kind is invalid")
            if baseline_id not in by_id or current_id not in by_id or baseline_id == current_id:
                raise ValueError(f"comparisons[{index}] must reference two different observations")
            key = (baseline_id, current_id, kind)
            if key in seen:
                raise ValueError("comparisons must not repeat the same directed pair")
            seen.add(key)
            comparison_specs.append(key)
    else:
        # Compare adjacent periods within the same entity/metric. Gaps remain
        # visible as non-comparable results instead of being silently bridged.
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in observations:
            groups.setdefault((row["entity"]["id"], row["metric"]["key"]), []).append(row)
        for group in groups.values():
            group.sort(key=lambda item: (item["_period_start"], item["_period_end"], item["id"]))
            for previous, current in zip(group, group[1:]):
                comparison_specs.append((previous["id"], current["id"], "period_change"))
        # A caller-created comparison group permits same-window location
        # comparisons; scope checks still decide whether the data are comparable.
        cross_groups: dict[tuple[str, str, str, str], list[dict]] = {}
        for row in observations:
            group_name = row["comparison_group"]
            if group_name:
                cross_groups.setdefault((
                    group_name, row["metric"]["key"], row["period"]["start"], row["period"]["end"]
                ), []).append(row)
        for group in cross_groups.values():
            for left, right in combinations(sorted(group, key=lambda item: item["id"]), 2):
                comparison_specs.append((left["id"], right["id"], "same_period"))

    comparison_results = [
        _comparison_result(by_id[baseline], by_id[current], kind)
        for baseline, current, kind in comparison_specs
    ]
    market_rows = inputs.get("market_sales", [])
    voice_rows = inputs.get("voice_samples", [])
    if not isinstance(market_rows, list) or not isinstance(voice_rows, list):
        raise ValueError("market_sales and voice_samples must be arrays")
    market_sales = [_market_sales_row(row, i) for i, row in enumerate(market_rows)]
    voice_samples = [_voice_sample(row, i) for i, row in enumerate(voice_rows)]

    comparable_count = sum(row["comparable"] for row in comparison_results)
    public_observations = [_public_observation(row) for row in observations]
    observations_by_id = {row["id"]: row for row in public_observations}
    observation_rows = []
    for index, row in enumerate(public_observations, 1):
        metric = row["metric"]
        coverage = row["coverage"]
        period = row["period"]
        observation_rows.append([
            _observation_source_row(row["source"], index), row["entity"]["name"], metric["key"], metric["definition"],
            _json_number_to_text(metric["value"]), metric["unit"], metric["currency"], metric["basis"],
            metric["denominator"],
            f"{period['start']}–{period['end']} ({period['frequency']}, {period['timezone'] or '时区未知'})",
            f"{coverage['observed']}/{coverage['eligible']}" if coverage["observed"] is not None and coverage["eligible"] is not None else coverage["state"],
            row["review_status"], _source_label(row["source"]),
        ])
    comparison_rows = []
    for comparison in comparison_results:
        baseline = observations_by_id[comparison["baseline_id"]]
        current = observations_by_id[comparison["current_id"]]
        metric = baseline["metric"]
        status = "可比" if comparison["comparable"] else "不可比：" + ", ".join(comparison["reasons"])
        comparison_rows.append([
            baseline["entity"]["name"],
            f"{baseline['period']['start']}–{baseline['period']['end']}",
            _json_number_to_text(comparison["baseline_value"]),
            current["entity"]["name"],
            f"{current['period']['start']}–{current['period']['end']}",
            _json_number_to_text(comparison["current_value"]),
            _json_number_to_text(comparison["delta"]),
            (f"{_json_number_to_text(comparison['change_percent'])}%" if comparison["change_percent"] is not None else comparison["change_percent_state"]),
            metric["unit"] + (f" ({metric['currency']})" if metric["currency"] else ""),
            status,
        ])
    market_rows_body = []
    for item in market_sales:
        scope = item["scope"]
        value_text = "未知" if item["share"] is None else f"{_json_number_to_text(item['share'] * 100)}%"
        market_rows_body.append([
            scope["entity"], scope["geography"], scope["category"], scope["market_definition"],
            scope["sales_basis"], _json_number_to_text(item["numerator"]), _json_number_to_text(item["market_total"]),
            value_text, scope["currency"] or scope["unit"],
            f"{scope['period']['start']}–{scope['period']['end']} ({scope['period']['timezone'] or '时区未知'})",
            item["state"], _source_label(item["source"]),
        ])
    voice_rows_body = []
    for sample in voice_samples:
        for brand, count in sample["counts"].items():
            share = sample["shares"][brand]
            voice_rows_body.append([
                sample["id"], sample["brand_labels"].get(brand, brand), count, sample["classified_mentions"],
                "未知" if share is None else f"{_json_number_to_text(share * 100)}%",
                sample["unclassified_count"], ", ".join(sample["platform_coverage"]),
                f"{sample['window']['start']}–{sample['window']['end']} ({sample['window']['timezone'] or '时区未知'})",
                sample["counting_rule"], sample["deduplication_rule"], sample["state"], _source_label(sample["source"]),
            ])
    sections = [
        _section(
            "operating.observations",
            "经营观察与覆盖",
            f"共 {len(observations)} 条指标观察。下面逐条列出原值、单位、状态、期间、样本覆盖和来源；缺值显示为未知，零值保留为0。\n\n" +
            _md_table(["记录", "对象", "指标", "定义", "数值", "单位", "币种", "实绩状态", "分母", "期间/时区", "覆盖", "复核状态", "来源"], observation_rows),
            {"observations": public_observations},
        ),
        _section(
            "operating.comparisons",
            "可比变化",
            f"共 {len(comparison_results)} 项比较，其中 {comparable_count} 项满足当前口径、样本范围和期间条件。变化为描述性差额，不证明原因或增量效果。\n\n" +
            _md_table(["基准对象", "基准期间", "基准值", "当前对象", "当前期间", "当前值", "差额", "相对变化", "单位", "判断"], comparison_rows),
            {"comparisons": comparison_results, "causal_effect": None},
        ),
        _section(
            "operating.market_sales",
            "同口径销售份额",
            f"仅按各行明确的销售分子和市场总量计算，未估算市场总量，也未从声量推导销售。\n\n" +
            _md_table(["主体", "地区", "品类", "市场定义", "销售口径", "分子", "市场分母", "份额", "币种/单位", "期间/时区", "状态", "来源"], market_rows_body),
            {"market_sales": market_sales},
        ),
        _section(
            "operating.voice_share",
            "观测范围内声量",
            f"结果限于声明的平台覆盖、观察窗口、计数/去重规则和已分类提及，不代表总体市场份额。\n\n" +
            _md_table(["样本", "品牌", "已分类提及", "分类分母", "观测份额", "未分类", "平台覆盖", "期间/时区", "计数规则", "去重规则", "状态", "来源"], voice_rows_body),
            {"voice_samples": voice_samples},
        ),
    ]
    market_boundary_limitations = []
    if "market_boundary" in inputs:
        _, boundary_sections, market_boundary_limitations = _market_boundary(inputs["market_boundary"])
        sections.extend(boundary_sections)
    limitations = [
        "本操作做口径核对和描述性比较；没有实施预测、异常检测或因果识别。",
        "比较仅使用输入中明确的观察期间、指标定义、单位、分母、测量状态及范围字段；未知或不一致处不生成差值。",
        "来源状态来自调用方快照；来源断言、人工复核和独立验证仍是不同状态。",
        "市场销售份额的范围声明未由本计算独立核实，零分母和未知分母均不产生份额。",
        "声量份额只表示互斥已分类提及中的比例；未覆盖平台、重复内容和未分类记录会限制解释。",
    ]
    limitations.extend(market_boundary_limitations)
    if question:
        sections.insert(0, _section(
            "operating.question", "分析问题", question, {"question": question}
        ))
    return _answer(_requested_title(inputs, "经营与市场分析"), sections, limitations)


def _wilson_interval(successes: int, trials: int, confidence: float) -> list[float] | None:
    """Wilson score interval used by the reviewed legacy canonical metric."""
    if trials == 0:
        return None
    p = successes / trials
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    z2 = z * z
    denominator = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def _recommendation_survey(value: Any) -> dict:
    row = _object(value, "recommendation_survey")
    if row.get("question_type") != "likelihood_to_recommend_0_10":
        raise ValueError("recommendation_survey must be an explicit 0-10 recommendation question")
    survey_id = _string(row.get("survey_id"), "recommendation_survey.survey_id")
    sampling_frame = _string(row.get("sampling_frame"), "recommendation_survey.sampling_frame")
    window = _string(row.get("observation_window"), "recommendation_survey.observation_window")
    responses = row.get("responses")
    if not isinstance(responses, list):
        raise ValueError("recommendation_survey.responses must be an array")
    identifiers = set()
    scores = []
    for index, response in enumerate(responses):
        item = _object(response, f"recommendation_survey.responses[{index}]")
        identity = _string(item.get("response_id"), f"recommendation_survey.responses[{index}].response_id")
        score = item.get("score")
        if identity in identifiers:
            raise ValueError("recommendation response IDs must be unique")
        if isinstance(score, str) and re.fullmatch(r"\d+", score.strip()):
            score = int(score.strip())
        if score is not None and (type(score) is not int or not 0 <= score <= 10):
            raise ValueError("recommendation scores must be integers from 0 to 10 or None")
        identifiers.add(identity)
        if score is not None:
            scores.append(score)
    n = len(scores)
    promoters = sum(score >= 9 for score in scores)
    detractors = sum(score <= 6 for score in scores)
    passives = n - promoters - detractors
    promoter_interval = _wilson_interval(promoters, n, 0.975)
    detractor_interval = _wilson_interval(detractors, n, 0.975)
    interval = None
    value_nps = None
    if n:
        value_nps = 100 * (promoters - detractors) / n
        interval = [
            100 * (promoter_interval[0] - detractor_interval[1]),
            100 * (promoter_interval[1] - detractor_interval[0]),
        ]
    return {
        "state": "NO_VALID_RESPONSES" if not n else "OBSERVED_SURVEY",
        "method": "recommendation-nps-bonferroni-wilson@1",
        "value": value_nps,
        "unit": "index_points[-100,100]",
        "n": n,
        "missing_n": len(identifiers) - n,
        "counts": {"promoters": promoters, "passives": passives, "detractors": detractors},
        "denominator": "all nonmissing 0-10 recommendation answers, including passives",
        "interval_95": interval,
        "survey_id": survey_id,
        "sampling_frame": sampling_frame,
        "observation_window": window,
        "limitations": [
            "The interval combines two approximate Wilson intervals conservatively and assumes independent, comparable sampled responses.",
            "Unique response IDs do not establish unique people, representative sampling, or absence of selection bias.",
            "A recommendation score is not inferred from sentiment, purchases, or public engagement.",
        ],
    }


def _sentiment_summary(value: Any) -> dict:
    row = _object(value, "sentiment_counts")
    positive = _count(row.get("positive"), "sentiment_counts.positive")
    neutral = _count(row.get("neutral"), "sentiment_counts.neutral")
    negative = _count(row.get("negative"), "sentiment_counts.negative")
    unknown = _count(row.get("unknown"), "sentiment_counts.unknown")
    sampling_frame = _string(row.get("sampling_frame"), "sentiment_counts.sampling_frame")
    window = _string(row.get("observation_window"), "sentiment_counts.observation_window")
    basis = _string(row.get("classification_basis"), "sentiment_counts.classification_basis")
    known = positive + neutral + negative
    value_nsi = 100 * (positive - negative) / known if known else None
    pos_interval = _wilson_interval(positive, known, 0.975)
    neg_interval = _wilson_interval(negative, known, 0.975)
    interval = None if not known else [
        100 * (pos_interval[0] - neg_interval[1]),
        100 * (pos_interval[1] - neg_interval[0]),
    ]
    return {
        "state": "NO_KNOWN_LABELS" if not known else "OBSERVED_LABELS",
        "method": "net-sentiment-bonferroni-wilson@1",
        "value": value_nsi,
        "unit": "index_points[-100,100]",
        "n": known,
        "unknown_n": unknown,
        "counts": {"positive": positive, "neutral": neutral, "negative": negative, "unknown": unknown},
        "denominator": "known positive, neutral, and negative labels; unknown excluded",
        "interval_95": interval,
        "sampling_frame": sampling_frame,
        "observation_window": window,
        "classification_basis": basis,
        "limitations": [
            "The interval is a conservative combination of approximate Wilson intervals; it does not correct selection bias or dependence.",
            "Sentiment labels measure classified expression in this frame and are not recommendation intent or NPS.",
        ],
    }


def _deduplicate_transactions(rows: list[dict]) -> list[dict]:
    unique = {}
    for row in rows:
        previous = unique.get(row["id"])
        if previous is None:
            unique[row["id"]] = row
        elif previous != row:
            raise ValueError("a transaction ID is repeated with conflicting data")
    return list(unique.values())


def _purchase_cohort(value: Any) -> dict:
    row = _object(value, "purchase_cohort")
    coverage_state = row.get("coverage_state", "unknown")
    if coverage_state not in {"complete", "partial", "unknown"}:
        raise ValueError("purchase_cohort.coverage_state must be complete, partial, or unknown")
    refund_coverage = row.get("refund_coverage", "unknown")
    if refund_coverage not in {"complete", "partial", "unknown"}:
        raise ValueError("purchase_cohort.refund_coverage must be complete, partial, or unknown")
    transaction_identity_basis = row.get("transaction_identity_basis", "source_column")
    if transaction_identity_basis not in {"source_column", "original_row"}:
        raise ValueError("purchase_cohort.transaction_identity_basis is invalid")
    time_precision = row.get("time_precision", "instant")
    if time_precision not in {"instant", "day"}:
        raise ValueError("purchase_cohort.time_precision must be instant or day")
    coverage = _object(row.get("coverage"), "purchase_cohort.coverage")
    cohort = _object(row.get("cohort"), "purchase_cohort.cohort")
    transaction_timezone = None
    if time_precision == "day":
        transaction_timezone = _string(row.get("timezone"), "purchase_cohort.timezone")
        try:
            ZoneInfo(transaction_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("purchase_cohort.timezone must be a known IANA timezone") from exc
        coverage_start = _iso_date(coverage.get("start"), "purchase_cohort.coverage.start")
        coverage_through = _iso_date(coverage.get("through"), "purchase_cohort.coverage.through")
        cohort_start = _iso_date(cohort.get("start"), "purchase_cohort.cohort.start")
        cohort_end = _iso_date(cohort.get("end"), "purchase_cohort.cohort.end")
    else:
        coverage_start = _instant(coverage.get("start"), "purchase_cohort.coverage.start")
        coverage_through = _instant(coverage.get("through"), "purchase_cohort.coverage.through")
        cohort_start = _instant(cohort.get("start"), "purchase_cohort.cohort.start")
        cohort_end = _instant(cohort.get("end"), "purchase_cohort.cohort.end")
    followup_days = row.get("followup_days")
    if isinstance(followup_days, str) and _COUNT_TEXT.fullmatch(followup_days.strip()):
        followup_days = int(followup_days.strip())
    if type(followup_days) is not int or followup_days <= 0:
        raise ValueError("purchase_cohort.followup_days must be a positive integer")
    if coverage_start > coverage_through or cohort_start > cohort_end:
        raise ValueError("purchase cohort and coverage windows are invalid")
    transactions = row.get("transactions")
    if not isinstance(transactions, list):
        raise ValueError("purchase_cohort.transactions must be an array")
    parsed = []
    for index, raw in enumerate(transactions):
        field = f"purchase_cohort.transactions[{index}]"
        item = _object(raw, field)
        identity = _string(item.get("id"), f"{field}.id")
        customer_id = _string(item.get("customer_id"), f"{field}.customer_id", optional=True)
        occurred_at = (
            _iso_date(item.get("occurred_at"), f"{field}.occurred_at")
            if time_precision == "day"
            else _instant(item.get("occurred_at"), f"{field}.occurred_at")
        )
        kind = item.get("kind")
        if kind not in {"purchase", "refund", "cancelled"}:
            raise ValueError(f"{field}.kind must be purchase, refund, or cancelled")
        amount = _number(item.get("amount"), f"{field}.amount", optional=True, nonnegative=True)
        currency = _currency(item.get("currency"), f"{field}.currency", optional=True)
        if amount is not None and currency is None:
            raise ValueError(f"{field}.currency is required when amount is known")
        refund_of = _string(item.get("refund_of"), f"{field}.refund_of", optional=True)
        if (kind == "refund") != (refund_of is not None):
            raise ValueError(f"{field}.refund_of is required only for refunds")
        if occurred_at < coverage_start or occurred_at > coverage_through:
            raise ValueError(f"{field}.occurred_at is outside the declared coverage window")
        parsed.append({
            "id": identity,
            "customer_id": customer_id,
            "occurred_at": occurred_at,
            "kind": kind,
            "amount": amount,
            "currency": currency,
            "refund_of": refund_of,
        })
    parsed = _deduplicate_transactions(parsed)
    by_id = {item["id"]: item for item in parsed}
    refunds_by_original: dict[str, list[dict]] = {}
    for item in parsed:
        if item["kind"] == "refund":
            original = by_id.get(item["refund_of"])
            if original is None or original["kind"] != "purchase":
                raise ValueError("refunds must reference an observed purchase")
            if original["currency"] != item["currency"]:
                raise ValueError("purchase and refund currencies must match")
            refunds_by_original.setdefault(original["id"], []).append(item)
    for purchase_id, refunds in refunds_by_original.items():
        purchase = by_id[purchase_id]
        if purchase["amount"] is not None and all(refund["amount"] is not None for refund in refunds):
            if sum((refund["amount"] for refund in refunds), Decimal(0)) > purchase["amount"]:
                raise ValueError("refund total cannot exceed its original purchase")

    purchases = [item for item in parsed if item["kind"] == "purchase"]
    customers: dict[str, list[dict]] = {}
    unidentified = 0
    for purchase in purchases:
        if purchase["customer_id"] is None:
            unidentified += 1
        else:
            customers.setdefault(purchase["customer_id"], []).append(purchase)
    cohort_customers = []
    for customer_id, customer_purchases in customers.items():
        first = min(customer_purchases, key=lambda item: (item["occurred_at"], item["id"]))
        if cohort_start <= first["occurred_at"] <= cohort_end:
            cohort_customers.append((customer_id, first, customer_purchases))
    mature = []
    immature = []
    repeat_count = 0
    for customer_id, first, customer_purchases in cohort_customers:
        followup_end = first["occurred_at"] + timedelta(days=followup_days)
        if time_precision == "day":
            fully_observed = followup_end <= coverage_through + timedelta(days=1)
        else:
            fully_observed = followup_end <= coverage_through
        if not fully_observed:
            immature.append(customer_id)
            continue
        mature.append(customer_id)
        if time_precision == "day":
            repeated = any(
                purchase["id"] != first["id"]
                and purchase["occurred_at"] > first["occurred_at"]
                and purchase["occurred_at"] + timedelta(days=1) <= followup_end
                for purchase in customer_purchases
            )
        else:
            repeated = any(
                purchase["id"] != first["id"]
                and first["occurred_at"] < purchase["occurred_at"] <= followup_end
                for purchase in customer_purchases
            )
        if repeated:
            repeat_count += 1
    cohort_observable = coverage_state == "complete" and coverage_start <= cohort_start
    repeat_rate = (repeat_count / len(mature)) if mature and cohort_observable else None

    transaction_totals_by_currency: dict[str, dict] = {}
    for item in parsed:
        if item["kind"] not in {"purchase", "refund"}:
            continue
        currency = item["currency"] or "UNKNOWN"
        summary = transaction_totals_by_currency.setdefault(currency, {
            "currency": None if currency == "UNKNOWN" else currency,
            "known_purchase_amount": Decimal(0),
            "known_refund_amount": Decimal(0),
            "unknown_amount_rows": 0,
            "purchase_count": 0,
            "refund_count": 0,
        })
        if item["kind"] == "purchase":
            summary["purchase_count"] += 1
            if item["amount"] is None:
                summary["unknown_amount_rows"] += 1
            else:
                summary["known_purchase_amount"] += item["amount"]
        else:
            summary["refund_count"] += 1
            if item["amount"] is None:
                summary["unknown_amount_rows"] += 1
            else:
                summary["known_refund_amount"] += item["amount"]
    revenue_summary = []
    for summary in transaction_totals_by_currency.values():
        net_state = (
            "INCOMPLETE_AMOUNT" if summary["unknown_amount_rows"]
            else "PARTIAL_TRANSACTION_COVERAGE" if coverage_state == "partial"
            else "UNKNOWN_TRANSACTION_COVERAGE" if coverage_state == "unknown"
            else "UNKNOWN_REFUND_COVERAGE" if refund_coverage == "unknown"
            else "PARTIAL_REFUND_COVERAGE" if refund_coverage == "partial"
            else "OBSERVED_ONLY"
        )
        revenue_summary.append({
            "currency": summary["currency"],
            "purchase_count": summary["purchase_count"],
            "refund_count": summary["refund_count"],
            "known_purchase_amount": _json_number(summary["known_purchase_amount"]),
            "known_refund_amount": _json_number(summary["known_refund_amount"]),
            "known_net_transaction_amount": _json_number(summary["known_purchase_amount"] - summary["known_refund_amount"]) if net_state == "OBSERVED_ONLY" else None,
            "unknown_amount_rows": summary["unknown_amount_rows"],
            "transaction_coverage": coverage_state,
            "refund_coverage": refund_coverage,
            "net_transaction_amount_state": net_state,
        })
    limitations = [
        "Cohort membership uses first observed purchase in the supplied coverage window, not verified lifetime acquisition.",
        "A within-window repeat rate is reported only when cohort entry is observable and the customer has the full follow-up window.",
        "No repeat purchase is not a confirmed cancellation, churn, or loss of loyalty.",
        "Daily or coarsely timed records can omit separate same-day purchases; purchase and refund amounts remain separate by currency.",
        "Currency totals cover all supplied transactions in the declared coverage window, not only matured cohort members; they are not profit or attributed ROI.",
        "Refund rows must link to the original purchase within the supplied transaction table.",
        "Net transaction amounts are shown only when refund coverage is declared complete; otherwise purchases and observed refunds remain separate.",
        "Original row position is a source-row identity, not proof that each row is a distinct transaction; no row deduplication is performed from that identity.",
    ]
    if time_precision == "day":
        limitations.append(
            "Date-only transactions represent full local calendar-day intervals in the declared IANA timezone. Purchases on the same day as first purchase and the follow-up boundary day are excluded from repeat ordering because their within-day sequence is unknown."
        )
    return {
        "state": (
            "TRANSACTION_COVERAGE_INCOMPLETE" if coverage_state != "complete"
            else "COHORT_ENTRY_UNKNOWN_BEFORE_COVERAGE" if not cohort_observable
            else "PARTIALLY_MATURE" if immature
            else "OBSERVED_COHORT_DAY_GRAIN" if time_precision == "day"
            else "OBSERVED_COHORT"
        ),
        "coverage": {
            "start": coverage_start.isoformat(),
            "through": coverage_through.isoformat(),
            "caller_declared": True,
            "state": coverage_state,
            "time_precision": time_precision,
            "timezone": transaction_timezone,
        },
        "cohort": {"start": cohort_start.isoformat(), "end": cohort_end.isoformat()},
        "time_precision": time_precision,
        "timezone": transaction_timezone,
        "followup_days": followup_days,
        "refund_coverage": refund_coverage,
        "transaction_identity_basis": transaction_identity_basis,
        "candidate_entries_when_history_incomplete": len(cohort_customers) if not cohort_observable else None,
        "first_observed_identified_customers": len(cohort_customers) if cohort_observable else None,
        "matured_customers": len(mature) if cohort_observable else None,
        "immature_customers": len(immature) if cohort_observable else None,
        "repeat_customers": repeat_count if cohort_observable else None,
        "repeat_purchase_rate": repeat_rate,
        "unidentified_purchase_rows": unidentified,
        "transaction_amounts_by_currency": revenue_summary,
        "transaction_amount_scope": {
            "start": coverage_start.isoformat(),
            "through": coverage_through.isoformat(),
            "customers": "all supplied transactions, including unidentified customers",
        },
        "churn_state": "NOT_INFERRED_FROM_NONPURCHASE",
        "limitations": limitations,
    }


def _qualitative_record_time(value: Any, field: str) -> tuple[str | None, str]:
    if value is None:
        return None, "unknown"
    value = _string(value, field)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return _iso_date(value, field).isoformat(), "day"
    try:
        _instant(value, field)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date or a timezone-aware timestamp") from error
    return value, "instant"


_QUALITATIVE_ROLE_LABELS = {
    "support": "支持该主题",
    "counterevidence": "相反证据",
    "context": "背景或条件",
    "uncoded": "未编码",
    "uncertain": "编码待复核",
}
_QUALITATIVE_SPAN_LABELS = {
    "verbatim_span": "原文唯一匹配",
    "repeated_phrase_offset_ambiguous": "摘录在原文重复，需确认具体位置",
    "not_read_for_analysis": "因同意/用途状态未核，未读取摘录",
}
_QUALITATIVE_THEME_STATE_LABELS = {
    "MIXED_EVIDENCE_IN_SUPPLIED_RECORDS": "本批材料中同时有支持与相反证据",
    "SUPPORTING_EVIDENCE_IN_SUPPLIED_RECORDS": "本批材料中有支持记录",
    "COUNTEREVIDENCE_ONLY_IN_SUPPLIED_RECORDS": "本批材料中只有相反证据",
    "CONTEXT_ONLY_IN_SUPPLIED_RECORDS": "本批材料只有背景记录",
    "NO_CODED_EVIDENCE": "本批材料没有该主题的已编码证据",
}
_QUALITATIVE_COVERAGE_LABELS = {"complete": "声明完整", "partial": "部分覆盖", "unknown": "未知"}
_QUALITATIVE_CONSENT_LABELS = {
    "participant_consent_required": "需要参与者同意",
    "public_source_use_declared": "调用方声明为公开来源用途",
    "mixed": "混合用途，逐条记录同意状态",
    "unknown": "用途状态未知",
}


def _qualitative_span_label(state: str) -> str:
    return _QUALITATIVE_SPAN_LABELS.get(state, "原文定位未知")


def _qualitative_theme_state_label(state: str) -> str:
    return _QUALITATIVE_THEME_STATE_LABELS.get(state, "证据状态未知")


def _qualitative_source_identity(source: dict, record_id: str | None, index: int) -> tuple[str, str]:
    if record_id is not None:
        material_id = source.get("material_id")
        digest = source.get("material_sha256")
        row_number = source.get("row")
        sheet = source.get("sheet") or "table"
        if (
            isinstance(material_id, str) and isinstance(digest, str) and type(row_number) is int
            and record_id == f"{material_id}:{digest}:{sheet}:row:{row_number}"
        ):
            return record_id, "saved_material_row"
        return record_id, "caller_declared_record_id"
    material_id = source.get("material_id")
    digest = source.get("material_sha256")
    row_number = source.get("row")
    sheet = source.get("sheet") or "table"
    if isinstance(material_id, str) and isinstance(digest, str) and type(row_number) is int:
        return f"{material_id}:{digest}:{sheet}:row:{row_number}", "saved_material_row"
    return f"input-row-{index + 1}", "input_order_only"


def _qualitative_code(value: Any, field: str) -> dict:
    row = _object(value, field)
    allowed = {
        "code_id", "label", "definition", "inclusion_rule", "exclusion_rule",
        "counterexample", "researcher_interpretation", "validation_question", "source",
    }
    if set(row) - allowed:
        raise ValueError(f"{field} contains unsupported fields")
    code_id = _string(row.get("code_id"), f"{field}.code_id")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", code_id):
        raise ValueError(f"{field}.code_id must be a stable lowercase codebook ID")
    return {
        "code_id": code_id,
        "label": _string(row.get("label"), f"{field}.label"),
        "definition": _string(row.get("definition"), f"{field}.definition"),
        "inclusion_rule": _string(row.get("inclusion_rule"), f"{field}.inclusion_rule"),
        "exclusion_rule": _string(row.get("exclusion_rule"), f"{field}.exclusion_rule"),
        "counterexample": _string(row.get("counterexample"), f"{field}.counterexample", optional=True),
        "researcher_interpretation": _string(row.get("researcher_interpretation"), f"{field}.researcher_interpretation", optional=True),
        "validation_question": _string(row.get("validation_question"), f"{field}.validation_question", optional=True),
        "source": _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source"),
    }


def _qualitative_research(value: Any) -> tuple[dict, list[dict], list[str]]:
    study = _object(value, "qualitative_research")
    allowed = {
        "study_id", "question", "target_population", "sampling_frame", "sampling_unit",
        "sampling_method", "recruitment_method", "collection_method", "collection_window",
        "coverage", "consent_policy", "codebook_version", "codebook", "records", "limitations",
    }
    if set(study) - allowed:
        raise ValueError("qualitative_research contains unsupported fields")
    question = _string(study.get("question"), "qualitative_research.question")
    study_id = _string(study.get("study_id"), "qualitative_research.study_id", optional=True)
    target_population = _string(study.get("target_population"), "qualitative_research.target_population", optional=True)
    sampling_frame = _string(study.get("sampling_frame"), "qualitative_research.sampling_frame", optional=True)
    sampling_unit = _string(study.get("sampling_unit"), "qualitative_research.sampling_unit", optional=True)
    sampling_method = _string(study.get("sampling_method"), "qualitative_research.sampling_method", optional=True)
    recruitment_method = _string(study.get("recruitment_method"), "qualitative_research.recruitment_method", optional=True)
    collection_method = _string(study.get("collection_method"), "qualitative_research.collection_method", optional=True)
    collection_window = _string(study.get("collection_window"), "qualitative_research.collection_window", optional=True)
    consent_policy = study.get("consent_policy", "unknown")
    if consent_policy not in {"participant_consent_required", "public_source_use_declared", "mixed", "unknown"}:
        raise ValueError("qualitative_research.consent_policy is invalid")

    raw_coverage = _object(study.get("coverage", {}), "qualitative_research.coverage")
    coverage_fields = {"state", "eligible_units", "invited_units", "responded_units", "basis", "nonresponse_reason"}
    if set(raw_coverage) - coverage_fields:
        raise ValueError("qualitative_research.coverage contains unsupported fields")
    coverage_state = raw_coverage.get("state", "unknown")
    if coverage_state not in {"complete", "partial", "unknown"}:
        raise ValueError("qualitative_research.coverage.state is invalid")
    eligible_units = _count(raw_coverage.get("eligible_units"), "qualitative_research.coverage.eligible_units", optional=True)
    invited_units = _count(raw_coverage.get("invited_units"), "qualitative_research.coverage.invited_units", optional=True)
    responded_units = _count(raw_coverage.get("responded_units"), "qualitative_research.coverage.responded_units", optional=True)
    if eligible_units is not None and invited_units is not None and invited_units > eligible_units:
        raise ValueError("invited units cannot exceed the declared eligible units")
    if invited_units is not None and responded_units is not None and responded_units > invited_units:
        raise ValueError("responded units cannot exceed the declared invited units")
    coverage = {
        "state": coverage_state,
        "eligible_units": eligible_units,
        "invited_units": invited_units,
        "responded_units": responded_units,
        "basis": _string(raw_coverage.get("basis"), "qualitative_research.coverage.basis", optional=True),
        "nonresponse_reason": _string(raw_coverage.get("nonresponse_reason"), "qualitative_research.coverage.nonresponse_reason", optional=True),
        "response_rate": (responded_units / invited_units) if invited_units else None,
        "response_rate_basis": "caller_declared_units" if invited_units is not None and responded_units is not None else "unknown",
    }

    raw_codebook = study.get("codebook", [])
    if not isinstance(raw_codebook, list):
        raise ValueError("qualitative_research.codebook must be an array")
    codebook = {}
    code_order = []
    for index, raw_code in enumerate(raw_codebook):
        code = _qualitative_code(raw_code, f"qualitative_research.codebook[{index}]")
        if code["code_id"] in codebook:
            raise ValueError("qualitative code IDs must be unique")
        codebook[code["code_id"]] = code
        code_order.append(code["code_id"])
    codebook_version = _string(study.get("codebook_version"), "qualitative_research.codebook_version", optional=True)

    raw_records = study.get("records", [])
    if not isinstance(raw_records, list):
        raise ValueError("qualitative_research.records must be an array")
    raw_study_limitations = study.get("limitations", [])
    if not isinstance(raw_study_limitations, list):
        raise ValueError("qualitative_research.limitations must be an array")
    record_fields = {
        "record_id", "respondent_id", "respondent_group", "record_type", "text", "quote",
        "observed_at", "context", "consent_status", "code_id", "coding_status", "coding_note",
        "source", "code_label", "code_definition", "inclusion_rule", "exclusion_rule",
        "counterexample", "researcher_interpretation", "validation_question",
    }
    normalized_records = []
    derived_codes = {}
    seen_record_core = {}
    seen_codings = set()
    for index, raw_record in enumerate(raw_records):
        field = f"qualitative_research.records[{index}]"
        record = _object(raw_record, field)
        if set(record) - record_fields:
            raise ValueError(f"{field} contains unsupported fields")
        source = _json_copy(_object(record.get("source", {}), f"{field}.source"), f"{field}.source")
        supplied_record_id = _string(record.get("record_id"), f"{field}.record_id", optional=True)
        record_id, record_id_basis = _qualitative_source_identity(source, supplied_record_id, index)
        consent_status = record.get("consent_status", "unknown")
        if consent_status not in {"valid", "not_applicable_public_source", "unknown", "withdrawn"}:
            raise ValueError(f"{field}.consent_status is invalid")
        if consent_status in {"unknown", "withdrawn"}:
            normalized_records.append({
                "record_id": record_id,
                "record_id_basis": record_id_basis,
                "respondent_id": None,
                "respondent_group": None,
                "record_type": None,
                "observed_at": None,
                "time_precision": "unknown",
                "context": None,
                "consent_status": consent_status,
                "included_in_analysis": False,
                "exclusion_reason": "CONSENT_UNKNOWN" if consent_status == "unknown" else "CONSENT_WITHDRAWN",
                "source": source,
                "raw_text": None,
                "quote": None,
                "code_id": None,
                "coding_status": "excluded",
                "coding_note": None,
                "quote_span_state": "not_read_for_analysis",
                "quote_start": None,
                "quote_end": None,
                "duplicate_assignment": False,
            })
            continue
        respondent_id = _string(record.get("respondent_id"), f"{field}.respondent_id", optional=True)
        respondent_group = _string(record.get("respondent_group"), f"{field}.respondent_group", optional=True)
        record_type = _string(record.get("record_type"), f"{field}.record_type")
        observed_at, time_precision = _qualitative_record_time(record.get("observed_at"), f"{field}.observed_at")
        context = _string(record.get("context"), f"{field}.context", optional=True)
        raw_text_value = record.get("text")
        if raw_text_value is None or raw_text_value == "" or isinstance(raw_text_value, str) and not raw_text_value.strip():
            normalized_records.append({
                "record_id": record_id, "record_id_basis": record_id_basis,
                "respondent_id": respondent_id, "respondent_group": respondent_group,
                "record_type": record_type, "observed_at": observed_at, "time_precision": time_precision,
                "context": context, "consent_status": consent_status,
                "included_in_analysis": False, "exclusion_reason": "SOURCE_TEXT_UNAVAILABLE",
                "source": source, "raw_text": None, "quote": None, "code_id": None,
                "coding_status": "excluded", "coding_note": None,
                "quote_span_state": "source_text_unavailable", "quote_start": None,
                "quote_end": None, "duplicate_assignment": False,
            })
            continue
        raw_text = _string(raw_text_value, f"{field}.text")
        code_id = _string(record.get("code_id"), f"{field}.code_id", optional=True)
        coding_status = record.get("coding_status", "uncoded")
        if coding_status not in {"support", "counterevidence", "context", "uncoded", "uncertain"}:
            raise ValueError(f"{field}.coding_status is invalid")
        coding_note = _string(record.get("coding_note"), f"{field}.coding_note", optional=True)

        if code_id is not None and code_id not in codebook:
            inline_code = {
                "code_id": code_id,
                "label": record.get("code_label"),
                "definition": record.get("code_definition"),
                "inclusion_rule": record.get("inclusion_rule"),
                "exclusion_rule": record.get("exclusion_rule"),
                "counterexample": record.get("counterexample"),
                "researcher_interpretation": record.get("researcher_interpretation"),
                "validation_question": record.get("validation_question"),
            }
            code = _qualitative_code(inline_code, f"{field}.codebook_entry")
            derived = derived_codes.get(code_id)
            if derived is not None and derived != code:
                raise ValueError(f"{field} conflicts with the saved definition for code {code_id!r}")
            derived_codes[code_id] = code
            if code_id not in code_order:
                code_order.append(code_id)
        elif code_id is not None:
            code_field_map = {
                "code_label": "label", "code_definition": "definition",
                "inclusion_rule": "inclusion_rule", "exclusion_rule": "exclusion_rule",
            }
            for field_name, code_field in code_field_map.items():
                declared = record.get(field_name)
                expected = codebook[code_id][code_field]
                if declared is not None and declared != expected:
                    raise ValueError(f"{field}.{field_name} conflicts with codebook {code_id!r}")
        if coding_status in {"support", "counterevidence", "context"} and code_id is None:
            raise ValueError(f"{field}.code_id is required for a coded record")

        quote = _string(record.get("quote"), f"{field}.quote", optional=True) or raw_text
        quote_start = raw_text.find(quote)
        if quote_start < 0:
            raise ValueError(f"{field}.quote must appear verbatim in text")
        repeated_quote = raw_text.find(quote, quote_start + 1) >= 0
        quote_span_state = "repeated_phrase_offset_ambiguous" if repeated_quote else "verbatim_span"
        quote_start_value = None if repeated_quote else quote_start
        quote_end_value = None if repeated_quote else quote_start + len(quote)
        core = (raw_text, source, respondent_id, record_type)
        previous_core = seen_record_core.get(record_id)
        if previous_core is not None and previous_core != core:
            raise ValueError(f"{field}.record_id repeats with conflicting source text or respondent reference")
        seen_record_core[record_id] = core
        assignment_key = (record_id, code_id, coding_status, quote)
        duplicate_assignment = assignment_key in seen_codings
        seen_codings.add(assignment_key)
        normalized_records.append({
            "record_id": record_id,
            "record_id_basis": record_id_basis,
            "respondent_id": respondent_id,
            "respondent_group": respondent_group,
            "record_type": record_type,
            "observed_at": observed_at,
            "time_precision": time_precision,
            "context": context,
            "consent_status": consent_status,
            "included_in_analysis": True,
            "exclusion_reason": None,
            "source": source,
            "raw_text": raw_text,
            "quote": quote,
            "code_id": code_id,
            "coding_status": coding_status,
            "coding_note": coding_note,
            "quote_span_state": quote_span_state,
            "quote_start": quote_start_value,
            "quote_end": quote_end_value,
            "duplicate_assignment": duplicate_assignment,
        })

    for code_id, code in derived_codes.items():
        if code_id in codebook and codebook[code_id] != code:
            raise ValueError(f"code {code_id!r} has conflicting definitions")
        codebook[code_id] = code
    if codebook and codebook_version is None:
        raise ValueError("qualitative_research.codebook_version is required when a codebook is supplied")
    for record in normalized_records:
        if record["included_in_analysis"] and record["code_id"] is not None and record["code_id"] not in codebook:
            raise ValueError(f"qualitative record refers to unknown code {record['code_id']!r}")

    included = [record for record in normalized_records if record["included_in_analysis"]]
    excluded_count = len(normalized_records) - len(included)
    excluded_consent_count = sum(record["exclusion_reason"] in {"CONSENT_UNKNOWN", "CONSENT_WITHDRAWN"} for record in normalized_records)
    unavailable_text_count = sum(record["exclusion_reason"] == "SOURCE_TEXT_UNAVAILABLE" for record in normalized_records)
    duplicate_assignment_count = sum(record["duplicate_assignment"] for record in included)
    evidence_rows = []
    for record in normalized_records:
        code = codebook.get(record["code_id"]) if record["code_id"] else None
        if record["included_in_analysis"]:
            quote = record["quote"]
            role = _QUALITATIVE_ROLE_LABELS[record["coding_status"]]
            coding_state = "重复编码候选，未重复计数" if record["duplicate_assignment"] else role
        else:
            quote = (
                "未用于分析：同意/用途状态需先确认"
                if record["exclusion_reason"] in {"CONSENT_UNKNOWN", "CONSENT_WITHDRAWN"}
                else "未用于分析：原件文本或已重算值未取得"
            )
            coding_state = "未纳入"
        evidence_rows.append([
            _source_label(record["source"]), record["record_type"], record["observed_at"],
            record["respondent_group"], quote,
            _qualitative_span_label(record["quote_span_state"]),
            code["label"] if code else "未编码",
            coding_state, record["context"], record["coding_note"],
        ])

    theme_rows = []
    themes = []
    theme_sections = []
    for code_id in code_order:
        code = codebook[code_id]
        entries = [
            record for record in included
            if record["code_id"] == code_id and not record["duplicate_assignment"]
        ]
        role_counts = {role: sum(record["coding_status"] == role for record in entries) for role in _QUALITATIVE_ROLE_LABELS}
        supporting_ids = {record["respondent_id"] for record in entries if record["coding_status"] == "support" and record["respondent_id"] is not None}
        counter_ids = {record["respondent_id"] for record in entries if record["coding_status"] == "counterevidence" and record["respondent_id"] is not None}
        supporting_without_id = sum(record["coding_status"] == "support" and record["respondent_id"] is None for record in entries)
        counter_without_id = sum(record["coding_status"] == "counterevidence" and record["respondent_id"] is None for record in entries)
        if role_counts["support"] and role_counts["counterevidence"]:
            theme_state = "MIXED_EVIDENCE_IN_SUPPLIED_RECORDS"
        elif role_counts["support"]:
            theme_state = "SUPPORTING_EVIDENCE_IN_SUPPLIED_RECORDS"
        elif role_counts["counterevidence"]:
            theme_state = "COUNTEREVIDENCE_ONLY_IN_SUPPLIED_RECORDS"
        elif role_counts["context"]:
            theme_state = "CONTEXT_ONLY_IN_SUPPLIED_RECORDS"
        else:
            theme_state = "NO_CODED_EVIDENCE"
        theme = {
            **code,
            "state": theme_state,
            "coded_record_count": len(entries),
            "role_counts": role_counts,
            "supporting_declared_respondent_ids": len(supporting_ids) if supporting_ids else None,
            "counter_declared_respondent_ids": len(counter_ids) if counter_ids else None,
            "supporting_records_without_respondent_id": supporting_without_id,
            "counter_records_without_respondent_id": counter_without_id,
            "respondent_id_basis": "caller_declared_and_not_independently_verified",
            "population_inference": "NOT_ESTABLISHED_FROM_QUALITATIVE_RECORDS",
            "evidence": [
                {key: record[key] for key in (
                    "record_id", "record_id_basis", "respondent_id", "respondent_group", "record_type",
                    "observed_at", "time_precision", "context", "quote", "quote_span_state",
                    "quote_start", "quote_end", "coding_status", "coding_note", "source",
                )}
                for record in entries
            ],
        }
        themes.append(theme)
        support_examples = [[
            _source_label(record["source"]), record["respondent_group"], record["observed_at"],
            _QUALITATIVE_ROLE_LABELS[record["coding_status"]],
            record["quote"], _qualitative_span_label(record["quote_span_state"]),
            record["context"], record["coding_note"],
        ] for record in entries if record["coding_status"] in {"support", "counterevidence", "context"}]
        body = (
            f"该主题来自本次 {len(entries)} 条非重复编码记录：支持 {role_counts['support']} 条、相反证据 {role_counts['counterevidence']} 条、背景 {role_counts['context']} 条、未编码或待复核 {role_counts['uncoded'] + role_counts['uncertain']} 条；{_qualitative_theme_state_label(theme_state)}。这些是研究者对所提供材料的编码数量，不是人群比例或独立受访者人数。\n\n"
            f"定义：{code['definition']}\n\n纳入：{code['inclusion_rule']}\n\n排除：{code['exclusion_rule']}\n\n"
            + (f"工作解释（由研究者提供）：{code['researcher_interpretation']}\n\n" if code["researcher_interpretation"] else "")
            + (f"下一项验证问题（由研究者提供）：{code['validation_question']}\n\n" if code["validation_question"] else "")
            + _md_table(["原件定位", "样本组别", "原始记录时间", "研究者编码", "原文摘录", "摘录定位状态", "上下文", "编码备注"], support_examples)
        )
        theme_rows.append([
            code["label"], code["definition"], role_counts["support"], role_counts["counterevidence"],
            role_counts["context"], role_counts["uncoded"] + role_counts["uncertain"], _qualitative_theme_state_label(theme_state),
        ])
        theme_sections.append(_section(
            f"consumer.qualitative.theme.{code_id}",
            f"主题证据：{code['label']}",
            body,
            theme,
        ))

    declared_ids = {record["respondent_id"] for record in included if record["respondent_id"] is not None}
    rows_without_declared_id = sum(record["respondent_id"] is None for record in included)
    coverage_body = (
        "本节只描述已提供并符合本次用途条件的原始记录；主题支持和反例不外推为总体比例。"
        "Material行号标识来源记录，不证明独立受访者、顾客或交易身份。\n\n" +
        _md_table(
            ["研究问题", "目标人群", "抽样框", "分析单位", "抽样方法", "招募/来源方式", "资料类型", "观察窗口", "覆盖状态", "资格样本", "邀请单位", "回应单位", "声明回应率", "原始记录行", "已纳入编码行", "同意/用途未核或撤回", "原文/重算值不可用", "用户声明受访者标识数", "无受访者标识行", "用途/同意范围"],
            [[
                question, target_population, sampling_frame, sampling_unit, sampling_method,
                recruitment_method, collection_method, collection_window, _QUALITATIVE_COVERAGE_LABELS[coverage_state],
                coverage["eligible_units"], coverage["invited_units"], coverage["responded_units"],
                "未知" if coverage["response_rate"] is None else f"{_json_number_to_text(coverage['response_rate'] * 100)}%（申报分母）",
                len(normalized_records), len(included), excluded_consent_count, unavailable_text_count,
                len(declared_ids) if declared_ids else "未知", rows_without_declared_id,
                _QUALITATIVE_CONSENT_LABELS[consent_policy],
            ]],
        )
    )
    codebook_body = (
        f"编码本版本：{codebook_version or '未提供'}。编码定义和边界由调用方/研究者提供，本计算不会自动改写主题或推断受访者观点。\n\n"
        + _md_table(
            ["主题", "定义", "纳入规则", "排除规则", "反例提示", "研究者解释", "下一步验证问题", "编码本来源"],
            [
                [codebook[code_id][key] for key in (
                    "label", "definition", "inclusion_rule", "exclusion_rule", "counterexample",
                    "researcher_interpretation", "validation_question",
                )] + [_source_label(codebook[code_id].get("source", {}))]
                for code_id in code_order
            ],
        )
    )
    evidence_body = (
        f"保留 {len(normalized_records)} 条来源记录；{len(included)} 条进入分析，{excluded_consent_count} 条因同意/用途未知或已撤回、{unavailable_text_count} 条因原文或重算值不可用而未纳入主题编码。原文为用户提供内容，编码仅按显式Codebook和记录字段保留，不由本操作自动分类。\n\n"
        + _md_table(
            ["原件定位", "资料类型", "原始记录时间", "样本组别", "逐字摘录", "摘录定位状态", "主题", "研究者编码", "上下文", "编码备注"],
            evidence_rows,
        )
    )
    limitations = [
        "编码和主题定义来自调用方/研究者声明；本操作核结构、保原话和汇总编码，不由AI补造访谈、Persona、引语或主题。",
        "没有提供多位编码者的独立编码对照，因此不计算编码者一致性或声称主题稳定。",
        "主题记录数不是独立人数；受访者标识由用户提供且未经独立验证，缺失时人数保持未知。",
        "抽样框、招募、响应数和覆盖完整性属于用户声明；便利样本、公开评论和交易记录均不能自动代表目标人群。",
        "支持/反例状态是研究者对原文的编码，不是事实裁决；出现相反证据时保留并列，不用多数行抵消少数反例。",
        "同意或用途未知/已撤回的记录不进入主题计数；Material权限检查、撤权及历史Artifact管理仍由现有业务来源链处理。",
        "原文为空或已解析值未知的记录保留来源定位但不进入主题编码；公式单元格需沿现有表格重算/复核路径续接。",
    ]
    limitations.extend(_string(item, f"qualitative_research.limitations[{index}]") for index, item in enumerate(raw_study_limitations))
    data = {
        "state": "QUALITATIVE_EVIDENCE_AVAILABLE" if included else "NO_ELIGIBLE_QUALITATIVE_RECORDS",
        "study_id": study_id,
        "question": question,
        "target_population": target_population,
        "sampling_frame": sampling_frame,
        "sampling_unit": sampling_unit,
        "sampling_method": sampling_method,
        "recruitment_method": recruitment_method,
        "collection_method": collection_method,
        "collection_window": collection_window,
        "coverage": coverage,
        "consent_policy": consent_policy,
        "codebook_version": codebook_version,
        "codebook": [codebook[code_id] for code_id in code_order],
        "records": normalized_records,
        "record_count": len(normalized_records),
        "eligible_record_count": len(included),
        "excluded_consent_record_count": excluded_consent_count,
        "source_text_unavailable_record_count": unavailable_text_count,
        "excluded_record_count": excluded_count,
        "duplicate_code_assignment_count": duplicate_assignment_count,
        "declared_respondent_identifier_count": len(declared_ids) if declared_ids else None,
        "records_without_respondent_identifier": rows_without_declared_id,
        "themes": themes,
        "population_inference": "NOT_ESTABLISHED_FROM_QUALITATIVE_RECORDS",
        "limitations": limitations,
    }
    sections = [
        _section("consumer.qualitative.coverage", "研究范围与样本覆盖", coverage_body, {
            "study_id": study_id, "question": question, "target_population": target_population,
            "sampling_frame": sampling_frame, "sampling_unit": sampling_unit,
            "sampling_method": sampling_method, "recruitment_method": recruitment_method,
            "collection_method": collection_method, "collection_window": collection_window,
            "coverage": coverage, "consent_policy": consent_policy,
            "record_count": len(normalized_records), "eligible_record_count": len(included),
            "excluded_consent_record_count": excluded_consent_count,
            "source_text_unavailable_record_count": unavailable_text_count,
            "excluded_record_count": excluded_count,
            "declared_respondent_identifier_count": len(declared_ids) if declared_ids else None,
            "records_without_respondent_identifier": rows_without_declared_id,
            "population_inference": "NOT_ESTABLISHED_FROM_QUALITATIVE_RECORDS",
        }),
        _section("consumer.qualitative.codebook", "可修订主题编码本", codebook_body, {
            "version": codebook_version,
            "codebook": [codebook[code_id] for code_id in code_order],
        }),
        _section("consumer.qualitative.evidence", "原话、上下文与研究者编码", evidence_body, {
            "records": normalized_records,
            "record_count": len(normalized_records),
            "eligible_record_count": len(included),
            "excluded_consent_record_count": excluded_consent_count,
            "source_text_unavailable_record_count": unavailable_text_count,
            "excluded_record_count": excluded_count,
            "duplicate_code_assignment_count": duplicate_assignment_count,
        }),
        _section("consumer.qualitative.themes", "主题支持与相反证据", _md_table(
            ["主题", "定义", "支持记录", "相反证据记录", "背景记录", "未编码/待复核", "本批材料内判断"], theme_rows
        ), {"themes": themes, "population_inference": "NOT_ESTABLISHED_FROM_QUALITATIVE_RECORDS"}),
        *theme_sections,
    ]
    return data, sections, limitations


def _consumer_analysis(inputs: dict) -> dict:
    allowed = {"title", "question", "recommendation_survey", "sentiment_counts", "purchase_cohort", "qualitative_research"}
    if set(inputs) - allowed:
        raise ValueError("consumer_analysis contains unsupported fields")
    question = _string(inputs.get("question"), "question", optional=True)
    nps = _recommendation_survey(inputs["recommendation_survey"]) if "recommendation_survey" in inputs else None
    sentiment = _sentiment_summary(inputs["sentiment_counts"]) if "sentiment_counts" in inputs else None
    cohort = _purchase_cohort(inputs["purchase_cohort"]) if "purchase_cohort" in inputs else None
    qualitative_sections = []
    qualitative_limitations = []
    if "qualitative_research" in inputs:
        _, qualitative_sections, qualitative_limitations = _qualitative_research(inputs["qualitative_research"])
    nps_data = nps or {"state": "NOT_PROVIDED", "value": None, "n": None, "interval_95": None}
    nps_interval = nps_data.get("interval_95")
    nps_body = (
        "未提供0–10推荐意愿调查；不从评论或互动推算NPS。"
        if nps is None else
        "推荐意愿只从明确的0–10调查计算；缺答不作0分，中立回答进入分母。\n\n" +
        _md_table(
            ["调查", "抽样框", "观察窗", "有效答卷", "缺答", "推荐者≥9", "中立7–8", "贬损者≤6", "NPS", "95%区间", "状态"],
            [[
                nps_data["survey_id"], nps_data["sampling_frame"], nps_data["observation_window"],
                nps_data["n"], nps_data["missing_n"], nps_data["counts"]["promoters"],
                nps_data["counts"]["passives"], nps_data["counts"]["detractors"],
                nps_data["value"],
                "未知" if nps_interval is None else f"{_json_number_to_text(nps_interval[0])}–{_json_number_to_text(nps_interval[1])} {nps_data['unit']}",
                nps_data["state"],
            ]],
        )
    )
    sentiment_data = sentiment or {"state": "NOT_PROVIDED", "value": None, "n": None, "unknown_n": None, "interval_95": None}
    sentiment_interval = sentiment_data.get("interval_95")
    sentiment_body = (
        "未提供有据的情绪计数；不自动分类文本或将情绪当NPS。"
        if sentiment is None else
        "净情绪只在已知正面、中性、负面标签中计算；未知标签单独保留，结果不代表推荐意愿。\n\n" +
        _md_table(
            ["样本框", "观察窗", "分类依据", "正面", "中性", "负面", "未知标签", "已知分母", "净情绪", "95%区间", "状态"],
            [[
                sentiment_data["sampling_frame"], sentiment_data["observation_window"],
                sentiment_data["classification_basis"], sentiment_data["counts"]["positive"],
                sentiment_data["counts"]["neutral"], sentiment_data["counts"]["negative"],
                sentiment_data["unknown_n"], sentiment_data["n"], sentiment_data["value"],
                "未知" if sentiment_interval is None else f"{_json_number_to_text(sentiment_interval[0])}–{_json_number_to_text(sentiment_interval[1])} {sentiment_data['unit']}",
                sentiment_data["state"],
            ]],
        )
    )
    cohort_data = cohort or {"state": "NOT_PROVIDED", "repeat_purchase_rate": None, "churn_state": "NOT_INFERRED"}
    if cohort is None:
        cohort_body = "未提供带期间覆盖和客户识别的交易记录；留存、复购和流失保持未知。"
    else:
        transaction_identity_label = (
            "原件行号，仅代表来源记录"
            if cohort_data["transaction_identity_basis"] == "original_row"
            else "来源提供的交易编号"
        )
        refund_coverage_label = {
            "complete": "用户声明完整",
            "partial": "部分覆盖",
            "unknown": "未知",
        }[cohort_data["refund_coverage"]]
        transaction_coverage_label = {
            "complete": "用户声明完整",
            "partial": "部分覆盖",
            "unknown": "未知",
        }[cohort_data["coverage"]["state"]]
        time_precision_label = (
            f"按{cohort_data['timezone']}本地自然日"
            if cohort_data["time_precision"] == "day"
            else "按来源时区时间戳"
        )
        cohort_body = (
            f"结果限于给定交易范围、客户识别覆盖和完整随访窗口；交易覆盖={transaction_coverage_label}，时间精度={time_precision_label}；交易行身份依据={transaction_identity_label}，原件行号不证明交易真实唯一，未复购不等于流失。退款数据覆盖={refund_coverage_label}。\n\n" +
            _md_table(
                ["首次观察 cohort", "随访天数", "期间覆盖", "首购识别客户", "成熟客户", "未成熟客户", "窗口内复购客户", "复购率", "未识别客户行", "流失判断"],
                [[
                    f"{cohort_data['cohort']['start']}–{cohort_data['cohort']['end']}",
                    cohort_data["followup_days"],
                    f"{cohort_data['coverage']['start']}–{cohort_data['coverage']['through']}",
                    cohort_data["first_observed_identified_customers"],
                    cohort_data["matured_customers"], cohort_data["immature_customers"],
                    cohort_data["repeat_customers"],
                    "未知" if cohort_data["repeat_purchase_rate"] is None else f"{_json_number_to_text(cohort_data['repeat_purchase_rate'] * 100)}%",
                    cohort_data["unidentified_purchase_rows"], "未据非购买推断流失",
                ]],
            ) + "\n\n" +
            "交易金额（整个输入覆盖期，非仅成熟 cohort）：\n\n" +
            _md_table(
                ["币种", "购买笔数", "退款笔数", "已知购买金额小计", "已知退款金额小计", "已知净交易金额小计", "未知金额行", "交易覆盖", "退款覆盖", "金额完整性"],
                [[
                    item["currency"], item["purchase_count"], item["refund_count"],
                    item["known_purchase_amount"], item["known_refund_amount"],
                    item["known_net_transaction_amount"], item["unknown_amount_rows"], item["transaction_coverage"], item["refund_coverage"],
                    item["net_transaction_amount_state"],
                ] for item in cohort_data["transaction_amounts_by_currency"]],
            )
        )
    sections = []
    if question:
        sections.append(_section("consumer.question", "分析问题", question, {"question": question}))
    sections.extend([
        _section(
            "consumer.recommendation_survey",
            "推荐意愿调查",
            nps_body,
            nps_data,
        ),
        _section(
            "consumer.sentiment",
            "已分类情绪",
            sentiment_body,
            sentiment_data,
        ),
        _section(
            "consumer.purchase_behavior",
            "观察期内复购",
            cohort_body,
            cohort_data,
        ),
    ])
    sections.extend(qualitative_sections)
    limitations = [
        "公开讨论、推荐调查、客户身份和交易行为是不同样本与分母，不相互替代。",
        "区间依赖所述独立、可比样本前提；产品没有从唯一ID推断唯一受访者，也没有修复选择偏差。",
        "本操作不从非购买推断流失，不把未成熟观察窗判为留存失败。",
    ]
    limitations.extend(qualitative_limitations)
    return _answer(_requested_title(inputs, "消费者反馈与行为分析"), sections, limitations)


def _financial_line(value: Any, field: str, allowed_statuses: set[str]) -> dict:
    row = _object(value, field)
    identity = _string(row.get("id"), f"{field}.id")
    kind = row.get("kind")
    if kind not in {"cost", "income"}:
        raise ValueError(f"{field}.kind must be cost or income")
    status = row.get("status")
    if status not in allowed_statuses:
        raise ValueError(f"{field}.status is invalid")
    basis = row.get("basis")
    if basis not in {"cash", "accrual"}:
        raise ValueError(f"{field}.basis must be cash or accrual")
    currency = _currency(row.get("currency"), f"{field}.currency")
    start, end = _date_range(row.get("period"), f"{field}.period")
    period_timezone = _string(
        row.get("period", {}).get("timezone"), f"{field}.period.timezone", optional=True
    )
    amount = _number(row.get("amount"), f"{field}.amount", optional=True, nonnegative=True)
    quantity = _number(row.get("quantity"), f"{field}.quantity", optional=True, nonnegative=True)
    unit_price = _number(row.get("unit_price"), f"{field}.unit_price", optional=True, nonnegative=True)
    quantity_unit = _string(row.get("quantity_unit"), f"{field}.quantity_unit", optional=True)
    unit_price_basis = row.get("unit_price_basis", "per_unit")
    if unit_price_basis not in {"per_unit", "per_thousand"}:
        raise ValueError(f"{field}.unit_price_basis must be per_unit or per_thousand")
    if unit_price_basis == "per_thousand" and quantity_unit != "impressions":
        raise ValueError(f"{field}.per_thousand rates require an impressions quantity")
    calculated_base = None
    if quantity is not None and unit_price is not None:
        calculated_base = quantity * unit_price
        if unit_price_basis == "per_thousand":
            calculated_base /= Decimal(1000)
    if amount is None:
        amount = calculated_base
    elif calculated_base is not None and amount != calculated_base:
        raise ValueError(f"{field}.amount must equal quantity times unit_price before tax")
    tax_treatment = row.get("tax_treatment")
    if tax_treatment not in {"included", "exclusive", "not_applicable", "unknown"}:
        raise ValueError(f"{field}.tax_treatment is invalid")
    tax_rate = _number(row.get("tax_rate"), f"{field}.tax_rate", optional=True, nonnegative=True)
    tax_amount = _number(row.get("tax_amount"), f"{field}.tax_amount", optional=True, nonnegative=True)
    if tax_rate is not None and tax_treatment != "exclusive":
        raise ValueError(f"{field}.tax_rate requires exclusive tax treatment")
    if tax_amount is not None and tax_treatment != "exclusive":
        raise ValueError(f"{field}.tax_amount requires exclusive tax treatment")
    tax = None
    total = None
    if amount is not None:
        if tax_treatment in {"included", "not_applicable"}:
            total = amount
        elif tax_treatment == "exclusive":
            if tax_amount is not None:
                tax = tax_amount
                total = amount + tax_amount
            elif tax_rate is not None:
                tax = amount * tax_rate
                total = amount + tax
        # Unknown tax treatment keeps the untaxed base but not a false total.
    source_ref = _string(row.get("source_ref"), f"{field}.source_ref", optional=True)
    cost_key = _string(row.get("cost_key"), f"{field}.cost_key", optional=True)
    refund_of = _string(row.get("refund_of"), f"{field}.refund_of", optional=True)
    if (status == "refund") != (refund_of is not None):
        raise ValueError(f"{field}.refund_of is required only for refunds")
    if status == "commitment" and kind != "cost":
        raise ValueError("commitment lines must be costs")
    if status == "refund" and kind not in {"cost", "income"}:
        raise ValueError("refund lines must identify cost or income")
    if status in {"estimate", "quote", "commitment"} and cost_key is None:
        raise ValueError(f"{field}.cost_key is required for planned lines")
    category = _string(row.get("category"), f"{field}.category")
    return {
        "id": identity,
        "source_ref": source_ref,
        "cost_key": cost_key,
        "category": category,
        "kind": kind,
        "status": status,
        "basis": basis,
        "currency": currency,
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": period_timezone,
        },
        "_period_timezone": period_timezone,
        "_period_start": start,
        "_period_end": end,
        "amount": amount,
        "quantity": quantity,
        "quantity_unit": quantity_unit,
        "unit_price": unit_price,
        "unit_price_basis": unit_price_basis,
        "calculation_formula": (
            "quantity_times_rate_divided_by_1000"
            if unit_price_basis == "per_thousand"
            else "quantity_times_unit_price"
        ),
        "tax_treatment": tax_treatment,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "tax": tax,
        "total": total,
        "refund_of": refund_of,
        "source": _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source"),
    }


def _same_line(left: dict, right: dict) -> bool:
    return {key: value for key, value in left.items() if key != "id"} == {
        key: value for key, value in right.items() if key != "id"
    }


def _deduplicate_financial_lines(lines: list[dict]) -> tuple[list[dict], dict[str, str], list[str]]:
    by_id: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    duplicate_ids = []
    for row in lines:
        previous = by_id.get(row["id"])
        if previous is not None:
            if not _same_line(previous, row):
                raise ValueError("a financial line ID is repeated with conflicting data")
            aliases[row["id"]] = previous["id"]
            duplicate_ids.append(row["id"])
            continue
        source_ref = row["source_ref"]
        previous_source = by_source.get(source_ref) if source_ref else None
        if previous_source is not None:
            if not _same_line(previous_source, row):
                raise ValueError("a financial source reference is repeated with conflicting data")
            aliases[row["id"]] = previous_source["id"]
            duplicate_ids.append(row["id"])
            continue
        by_id[row["id"]] = row
        if source_ref:
            by_source[source_ref] = row
    return list(by_id.values()), aliases, duplicate_ids


def _financial_line_public(row: dict, included: bool, reason: str | None = None) -> dict:
    return {
        "id": row["id"],
        "source_ref": row["source_ref"],
        "cost_key": row["cost_key"],
        "category": row["category"],
        "kind": row["kind"],
        "status": row["status"],
        "basis": row["basis"],
        "currency": row["currency"],
        "period": row["period"],
        "quantity": _json_number(row["quantity"]),
        "quantity_unit": row["quantity_unit"],
        "unit_price": _json_number(row["unit_price"]),
        "unit_price_basis": row["unit_price_basis"],
        "calculation_formula": row["calculation_formula"],
        "base_amount": _json_number(row["amount"]),
        "base_amount_exact": _decimal_text(row["amount"]),
        "tax_treatment": row["tax_treatment"],
        "tax_amount": _json_number(row["tax"]),
        "tax_amount_exact": _decimal_text(row["tax"]),
        "total_amount": _json_number(row["total"]),
        "total_amount_exact": _decimal_text(row["total"]),
        "amount_state": "UNKNOWN_AMOUNT" if row["amount"] is None else ("UNKNOWN_TOTAL_TAX" if row["total"] is None else "KNOWN"),
        "refund_of": row["refund_of"],
        "included_in_budget_period": included,
        "scope_reason": reason,
        "source": row["source"],
    }


def _quantity_price_display(row: dict) -> str:
    quantity, price = row.get("quantity"), row.get("unit_price")
    if quantity is None and price is None:
        return "直接输入金额"
    quantity_text = "未知" if quantity is None else _json_number_to_text(quantity)
    price_text = "未知" if price is None else _json_number_to_text(price)
    unit = f" {row['quantity_unit']}" if row.get("quantity_unit") else ""
    basis = row.get("unit_price_basis") or "per_unit"
    return f"{quantity_text}{unit} × {price_text} / {basis}"


def _resource_use(value: Any, field: str) -> dict:
    row = _object(value, field)
    identity = _string(row.get("id"), f"{field}.id")
    resource_id = _string(row.get("resource_id"), f"{field}.resource_id")
    resource_name = _string(row.get("resource_name"), f"{field}.resource_name")
    unit = _string(row.get("unit"), f"{field}.unit")
    quantity = _number(row.get("quantity"), f"{field}.quantity", nonnegative=True)
    capacity = _number(row.get("capacity"), f"{field}.capacity", nonnegative=True)
    if quantity == 0 or capacity == 0:
        raise ValueError(f"{field}.quantity and capacity must be positive")
    start = _instant(row.get("start"), f"{field}.start")
    end = _instant(row.get("end"), f"{field}.end")
    if start >= end:
        raise ValueError(f"{field}.start must be before end")
    status = row.get("status")
    if status not in {"fixed", "proposed"}:
        raise ValueError(f"{field}.status must be fixed or proposed")
    economic_value = _number(row.get("economic_value"), f"{field}.economic_value", optional=True, nonnegative=True)
    economic_currency = _currency(row.get("economic_currency"), f"{field}.economic_currency", optional=True)
    if economic_value is not None and economic_currency is None:
        raise ValueError(f"{field}.economic_currency is required for an economic resource value")
    return {
        "id": identity,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "unit": unit,
        "quantity": quantity,
        "capacity": capacity,
        "start": start,
        "end": end,
        "status": status,
        "economic_value": economic_value,
        "economic_currency": economic_currency,
        "source": _json_copy(_object(row.get("source", {}), f"{field}.source"), f"{field}.source"),
    }


def _resource_conflicts(uses: list[dict]) -> tuple[list[dict], list[dict]]:
    groups: dict[str, list[dict]] = {}
    for use in uses:
        groups.setdefault(use["resource_id"], []).append(use)
    checks = []
    conflicts = []
    for resource_id, rows in sorted(groups.items()):
        units = {row["unit"] for row in rows}
        capacities = {row["capacity"] for row in rows}
        if len(units) != 1 or len(capacities) != 1:
            raise ValueError(f"resource {resource_id!r} has inconsistent units or capacity")
        unit = next(iter(units))
        capacity = next(iter(capacities))
        boundaries = sorted({row["start"] for row in rows} | {row["end"] for row in rows})
        resource_conflicts = []
        peak = Decimal(0)
        for start, end in zip(boundaries, boundaries[1:]):
            active = [row for row in rows if row["start"] < end and row["end"] > start]
            load = sum((row["quantity"] for row in active), Decimal(0))
            peak = max(peak, load)
            if load > capacity:
                conflict = {
                    "resource_id": resource_id,
                    "resource_name": rows[0]["resource_name"],
                    "unit": unit,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "used": _json_number(load),
                    "capacity": _json_number(capacity),
                    "use_ids": sorted(row["id"] for row in active),
                }
                resource_conflicts.append(conflict)
                conflicts.append(conflict)
        checks.append({
            "resource_id": resource_id,
            "resource_name": rows[0]["resource_name"],
            "unit": unit,
            "capacity": _json_number(capacity),
            "peak_use": _json_number(peak),
            "state": "CAPACITY_EXCEEDED" if resource_conflicts else "WITHIN_DECLARED_CAPACITY",
            "uses": [row["id"] for row in rows],
        })
    return checks, conflicts


def _financial_scenario(inputs: dict) -> dict:
    allowed = {"title", "question", "period", "budget", "actuals", "actuals_coverage", "scenarios"}
    if set(inputs) - allowed:
        raise ValueError("financial_scenario contains unsupported fields")
    if "actuals" not in inputs or "scenarios" not in inputs:
        raise ValueError("financial_scenario requires explicit actuals and scenario arrays")
    question = _string(inputs.get("question"), "question", optional=True)
    budget = _object(inputs.get("budget"), "budget")
    budget_allowed = {"currency", "basis", "period", "hard_limit", "target", "reserve"}
    budget_required = {"currency", "basis", "hard_limit", "target", "reserve"}
    if set(budget) - budget_allowed or budget_required - set(budget):
        raise ValueError("budget must explicitly provide currency, basis, limit, target, and reserve")
    budget_currency = _currency(budget.get("currency"), "budget.currency")
    budget_basis = budget.get("basis")
    if budget_basis not in {"cash", "accrual"}:
        raise ValueError("budget.basis must be cash or accrual")
    if "period" in inputs:
        period_value = _object(inputs["period"], "period")
        period_start, period_end = _date_range(period_value, "period")
        period_timezone = _string(period_value.get("timezone"), "period.timezone", optional=True)
        if "period" in budget:
            budget_period = _object(budget["period"], "budget.period")
            if _date_range(budget_period, "budget.period") != (period_start, period_end):
                raise ValueError("budget.period and period must match when both are supplied")
            budget_timezone = _string(budget_period.get("timezone"), "budget.period.timezone", optional=True)
            if budget_timezone != period_timezone:
                raise ValueError("budget.period and period timezones must match when both are supplied")
    else:
        period_value = _object(budget.get("period"), "budget.period")
        period_start, period_end = _date_range(period_value, "budget.period")
        period_timezone = _string(period_value.get("timezone"), "budget.period.timezone", optional=True)
    hard_limit = _number(budget.get("hard_limit"), "budget.hard_limit", optional=True, nonnegative=True)
    target = _number(budget.get("target"), "budget.target", optional=True, nonnegative=True)
    reserve = _number(budget.get("reserve"), "budget.reserve", optional=True, nonnegative=True)
    if hard_limit is not None and reserve is not None and reserve > hard_limit:
        raise ValueError("budget.reserve cannot exceed budget.hard_limit")
    actuals_value = inputs.get("actuals", [])
    scenarios_value = inputs.get("scenarios", [])
    if not isinstance(actuals_value, list) or not isinstance(scenarios_value, list):
        raise ValueError("actuals and scenarios must be arrays")
    actuals_coverage = inputs.get("actuals_coverage")
    if actuals_coverage not in {"complete", "partial", "unknown"}:
        raise ValueError("actuals_coverage must be complete, partial, or unknown")
    actuals = [
        _financial_line(row, f"actuals[{i}]", {"actual", "refund"})
        for i, row in enumerate(actuals_value)
    ]
    actuals, actual_aliases, actual_duplicates = _deduplicate_financial_lines(actuals)
    actual_by_id = {row["id"]: row for row in actuals}
    refunds_by_id: dict[str, list[dict]] = {}
    for row in actuals:
        if row["status"] == "refund":
            refund_target_id = actual_aliases.get(row["refund_of"], row["refund_of"])
            target_row = actual_by_id.get(refund_target_id)
            if target_row is None or target_row["status"] != "actual":
                raise ValueError("refunds must reference an included actual line")
            if (target_row["kind"], target_row["basis"], target_row["currency"]) != (
                row["kind"], row["basis"], row["currency"]
            ):
                raise ValueError("refund and original actual must share kind, accounting basis, and currency")
            refunds_by_id.setdefault(refund_target_id, []).append(row)
    for original_id, refunds in refunds_by_id.items():
        original = actual_by_id[original_id]
        if original["total"] is not None and all(refund["total"] is not None for refund in refunds):
            if sum((refund["total"] for refund in refunds), Decimal(0)) > original["total"]:
                raise ValueError("refund total cannot exceed its original actual line")

    scenario_ids = set()
    scenarios = []
    for index, raw in enumerate(scenarios_value):
        field = f"scenarios[{index}]"
        row = _object(raw, field)
        identity = _string(row.get("id"), f"{field}.id")
        if identity in scenario_ids:
            raise ValueError("scenario IDs must be unique")
        scenario_ids.add(identity)
        title = _string(row.get("title"), f"{field}.title")
        raw_lines = row.get("lines", [])
        raw_uses = row.get("resource_uses", [])
        if not isinstance(raw_lines, list) or not isinstance(raw_uses, list):
            raise ValueError(f"{field}.lines and resource_uses must be arrays")
        line_coverage = row.get("line_coverage")
        if line_coverage not in {"complete", "partial", "unknown"}:
            raise ValueError(f"{field}.line_coverage must be complete, partial, or unknown")
        income_coverage = row.get("income_coverage", "unknown")
        if income_coverage not in {"complete", "partial", "unknown"}:
            raise ValueError(f"{field}.income_coverage must be complete, partial, or unknown")
        resource_assessment = row.get("resource_assessment", "unknown")
        if resource_assessment not in {"complete", "none_required", "unknown"}:
            raise ValueError(f"{field}.resource_assessment is invalid")
        if resource_assessment == "none_required" and raw_uses:
            raise ValueError(f"{field} cannot provide resource uses when none are required")
        lines = [
            _financial_line(line, f"{field}.lines[{line_index}]", {"estimate", "quote", "commitment"})
            for line_index, line in enumerate(raw_lines)
        ]
        lines, _, duplicate_line_ids = _deduplicate_financial_lines(lines)
        keys = [line["cost_key"] for line in lines]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{field} repeats a cost_key; alternative estimates belong in separate scenarios")
        uses = [_resource_use(use, f"{field}.resource_uses[{use_index}]") for use_index, use in enumerate(raw_uses)]
        if len({use["id"] for use in uses}) != len(uses):
            raise ValueError(f"{field}.resource_uses IDs must be unique")
        resource_checks, resource_conflicts = _resource_conflicts(uses)
        scenarios.append({
            "id": identity,
            "title": title,
            "lines": lines,
            "duplicate_line_ids": duplicate_line_ids,
            "uses": uses,
            "resource_checks": resource_checks,
            "resource_conflicts": resource_conflicts,
            "line_coverage": line_coverage,
            "income_coverage": income_coverage,
            "resource_assessment": resource_assessment,
        })

    def period_state(line: dict) -> tuple[bool, str | None]:
        if period_timezone is None or line["_period_timezone"] is None:
            return False, "period_timezone_unknown"
        if line["_period_timezone"] != period_timezone:
            return False, "period_timezone_differs"
        if line["_period_start"] < period_start or line["_period_end"] > period_end:
            if line["_period_end"] < period_start or line["_period_start"] > period_end:
                return False, "outside_budget_period"
            return False, "period_requires_allocation"
        return True, None

    all_actuals_public = []
    for line in actuals:
        included, reason = period_state(line)
        all_actuals_public.append(_financial_line_public(line, included, reason))

    summaries = []
    public_scenarios = []
    for scenario in scenarios:
        lines_public = []
        included_lines = []
        for line in scenario["lines"]:
            included, reason = period_state(line)
            lines_public.append(_financial_line_public(line, included, reason))
            if included:
                included_lines.append(line)
        actual_counts = {"known_cost": Decimal(0), "known_income": Decimal(0), "unknown_cost": 0, "unknown_income": 0}
        foreign_currency_costs = 0
        period_mismatch_costs = 0
        basis_mismatch_costs = 0
        foreign_currency_income = 0
        period_mismatch_income = 0
        basis_mismatch_income = 0
        for line in actuals:
            included, reason = period_state(line)
            if line["kind"] == "cost" and reason not in {None, "outside_budget_period"}:
                period_mismatch_costs += 1
            if line["kind"] == "income" and reason not in {None, "outside_budget_period"}:
                period_mismatch_income += 1
            if not included:
                continue
            if line["kind"] == "cost" and line["basis"] != budget_basis:
                basis_mismatch_costs += 1
            if line["kind"] == "cost" and line["currency"] != budget_currency:
                foreign_currency_costs += 1
            if line["kind"] == "income" and line["basis"] != budget_basis:
                basis_mismatch_income += 1
            if line["kind"] == "income" and line["currency"] != budget_currency:
                foreign_currency_income += 1
            if line["basis"] != budget_basis or line["currency"] != budget_currency:
                continue
            target_total = line["total"]
            if target_total is None:
                actual_counts[f"unknown_{line['kind']}"] += 1
            elif line["status"] == "actual":
                actual_counts[f"known_{line['kind']}"] += target_total
            else:
                actual_counts[f"known_{line['kind']}"] -= target_total
        planned_totals = {"commitment": Decimal(0), "quote": Decimal(0), "estimate": Decimal(0)}
        planned_unknown = 0
        planned_income_totals = {"quote": Decimal(0), "estimate": Decimal(0)}
        planned_income_unknown = 0
        planned_income_currency_mismatch = 0
        planned_income_basis_mismatch = 0
        planned_income_period_mismatch = 0
        for line in scenario["lines"]:
            included, reason = period_state(line)
            if not included:
                if line["kind"] == "cost" and reason not in {None, "outside_budget_period"}:
                    period_mismatch_costs += 1
                elif line["kind"] == "income" and reason not in {None, "outside_budget_period"}:
                    planned_income_period_mismatch += 1
                continue
            if line["kind"] == "cost":
                if line["currency"] != budget_currency:
                    foreign_currency_costs += 1
                if line["basis"] != budget_basis:
                    basis_mismatch_costs += 1
            else:
                if line["currency"] != budget_currency:
                    planned_income_currency_mismatch += 1
                if line["basis"] != budget_basis:
                    planned_income_basis_mismatch += 1
            if line["basis"] != budget_basis or line["currency"] != budget_currency:
                continue
            if line["total"] is None:
                if line["kind"] == "cost":
                    planned_unknown += 1
                else:
                    planned_income_unknown += 1
            else:
                if line["kind"] == "cost":
                    planned_totals[line["status"]] += line["total"]
                else:
                    planned_income_totals[line["status"]] += line["total"]
        actual_cost_complete = actual_counts["unknown_cost"] == 0
        actual_cost = (
            actual_counts["known_cost"]
            if actual_cost_complete and actuals_coverage == "complete"
            else None
        )
        known_actual_cost = actual_counts["known_cost"]
        projected_known = known_actual_cost + sum(planned_totals.values(), Decimal(0))
        projected_complete = (
            actual_cost_complete
            and actuals_coverage == "complete"
            and planned_unknown == 0
            and scenario["line_coverage"] == "complete"
            and foreign_currency_costs == 0
            and basis_mismatch_costs == 0
            and period_mismatch_costs == 0
        )
        available = hard_limit - reserve if hard_limit is not None and reserve is not None else None
        if projected_complete and available is not None:
            budget_state = "WITHIN_LIMIT" if projected_known <= available else "OVER_LIMIT"
        elif (
            available is not None
            and actual_cost_complete
            and actuals_coverage == "complete"
            and projected_known > available
            and planned_unknown == 0
            and foreign_currency_costs == 0
            and basis_mismatch_costs == 0
            and period_mismatch_costs == 0
        ):
            budget_state = "OVER_LIMIT"
        else:
            budget_state = "UNDETERMINED"
        target_delta = None
        if (
            target is not None
            and actual_cost_complete
            and actuals_coverage == "complete"
            and planned_unknown == 0
            and scenario["line_coverage"] == "complete"
            and foreign_currency_costs == 0
            and basis_mismatch_costs == 0
            and period_mismatch_costs == 0
        ):
            target_delta = projected_known - target
        if scenario["resource_conflicts"]:
            resource_state = "CAPACITY_EXCEEDED"
        elif scenario["resource_assessment"] == "none_required":
            resource_state = "NO_RESOURCE_REQUIRED"
        elif scenario["resource_assessment"] == "complete":
            resource_state = "WITHIN_DECLARED_CAPACITY" if scenario["uses"] else "NO_RESOURCE_USE_DECLARED"
        else:
            resource_state = "NOT_ASSESSED" if not scenario["uses"] else "PARTIAL_INPUT"
        if budget_state == "OVER_LIMIT" or scenario["resource_conflicts"]:
            overall_state = "INFEASIBLE"
        elif budget_state == "WITHIN_LIMIT" and resource_state in {
            "NO_RESOURCE_REQUIRED", "WITHIN_DECLARED_CAPACITY", "NO_RESOURCE_USE_DECLARED"
        }:
            overall_state = "FEASIBLE_UNDER_DECLARED_CONDITIONS"
        elif budget_state == "WITHIN_LIMIT":
            overall_state = "BUDGET_WITHIN_LIMIT_RESOURCES_UNASSESSED"
        else:
            overall_state = "UNDETERMINED"
        actual_income_complete = actual_counts["unknown_income"] == 0
        actual_income_valid = (
            actual_income_complete
            and actuals_coverage == "complete"
            and foreign_currency_income == 0
            and basis_mismatch_income == 0
            and period_mismatch_income == 0
        )
        known_income = actual_counts["known_income"] + sum(planned_income_totals.values(), Decimal(0))
        income_complete = (
            actual_income_valid
            and planned_income_unknown == 0
            and scenario["income_coverage"] == "complete"
            and foreign_currency_income == 0
            and basis_mismatch_income == 0
            and period_mismatch_income == 0
            and planned_income_currency_mismatch == 0
            and planned_income_basis_mismatch == 0
            and planned_income_period_mismatch == 0
        )
        income_data = {
            "actual_net": _json_number(actual_counts["known_income"])
            if actual_income_valid else None,
            "actual_net_exact": _decimal_text(actual_counts["known_income"])
            if actual_income_valid else None,
            "known_actual_subtotal": _json_number(actual_counts["known_income"]),
            "unknown_actual_lines": actual_counts["unknown_income"],
            "quotes": _json_number(planned_income_totals["quote"]),
            "quotes_exact": _decimal_text(planned_income_totals["quote"]),
            "estimates": _json_number(planned_income_totals["estimate"]),
            "estimates_exact": _decimal_text(planned_income_totals["estimate"]),
            "unknown_planned_lines": planned_income_unknown,
            "planned_currency_mismatch_lines": planned_income_currency_mismatch,
            "planned_accounting_basis_mismatch_lines": planned_income_basis_mismatch,
            "planned_period_mismatch_lines": planned_income_period_mismatch,
            "actual_currency_mismatch_lines": foreign_currency_income,
            "actual_accounting_basis_mismatch_lines": basis_mismatch_income,
            "actual_period_mismatch_lines": period_mismatch_income,
            "known_projected_subtotal": _json_number(known_income),
            "known_projected_subtotal_exact": _decimal_text(known_income),
            "projected_receipts": _json_number(known_income) if income_complete else None,
            "projected_receipts_exact": _decimal_text(known_income) if income_complete else None,
            "state": "COMPLETE_INPUTS_ONLY" if income_complete else "INCOMPLETE_OR_INCOMPARABLE",
            "coverage": actuals_coverage,
            "scenario_income_coverage": scenario["income_coverage"],
            "currency": budget_currency,
            "basis": budget_basis,
        }
        summary = {
            "scenario_id": scenario["id"],
            "scenario_title": scenario["title"],
            "currency": budget_currency,
            "basis": budget_basis,
            "period": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
                "timezone": period_timezone,
            },
            "actual_net_cost": _json_number(actual_cost),
            "actual_net_cost_exact": _decimal_text(actual_cost),
            "known_actual_cost_subtotal": _json_number(known_actual_cost),
            "known_actual_cost_subtotal_exact": _decimal_text(known_actual_cost),
            "actual_unknown_cost_lines": actual_counts["unknown_cost"],
            "outstanding_commitments": _json_number(planned_totals["commitment"]),
            "outstanding_commitments_exact": _decimal_text(planned_totals["commitment"]),
            "quotes": _json_number(planned_totals["quote"]),
            "quotes_exact": _decimal_text(planned_totals["quote"]),
            "estimates": _json_number(planned_totals["estimate"]),
            "estimates_exact": _decimal_text(planned_totals["estimate"]),
            "planned_unknown_cost_lines": planned_unknown,
            "known_projected_cost_subtotal": _json_number(projected_known),
            "known_projected_cost_subtotal_exact": _decimal_text(projected_known),
            "projected_cost": _json_number(projected_known) if projected_complete else None,
            "projected_cost_exact": _decimal_text(projected_known) if projected_complete else None,
            "hard_limit": _json_number(hard_limit),
            "hard_limit_exact": _decimal_text(hard_limit),
            "reserve": _json_number(reserve),
            "reserve_exact": _decimal_text(reserve),
            "available_after_reserve": _json_number(available),
            "available_after_reserve_exact": _decimal_text(available),
            "soft_target": _json_number(target),
            "soft_target_exact": _decimal_text(target),
            "target_variance": _json_number(target_delta),
            "target_variance_exact": _decimal_text(target_delta),
            "currency_mismatch_cost_lines": foreign_currency_costs,
            "accounting_basis_mismatch_cost_lines": basis_mismatch_costs,
            "period_mismatch_cost_lines": period_mismatch_costs,
            "actual_income_currency_mismatch_lines": foreign_currency_income,
            "actual_income_accounting_basis_mismatch_lines": basis_mismatch_income,
            "actual_income_period_mismatch_lines": period_mismatch_income,
            "budget_state": budget_state,
            "overall_feasibility": overall_state,
            "actuals_coverage": actuals_coverage,
            "scenario_line_coverage": scenario["line_coverage"],
            "resource_assessment": scenario["resource_assessment"],
            "resource_state": resource_state,
            "income": income_data,
            "resource_economic_values": _aggregate_resource_values(scenario["uses"]),
            "commitment_semantics": "commitment amount is the unsettled balance at this input snapshot",
        }
        summaries.append(summary)
        public_scenarios.append({
            "id": scenario["id"],
            "title": scenario["title"],
            "lines": lines_public,
            "duplicate_line_ids": scenario["duplicate_line_ids"],
            "resource_uses": [_resource_use_public(use) for use in scenario["uses"]],
            "resource_checks": scenario["resource_checks"],
            "resource_conflicts": scenario["resource_conflicts"],
        })

    comparisons = []
    for left, right in combinations(summaries, 2):
        same_context = (
            left["currency"] == right["currency"]
            and left["basis"] == right["basis"]
            and left["period"] == right["period"]
        )
        comparable = same_context and left["projected_cost_exact"] is not None and right["projected_cost_exact"] is not None
        delta_exact = None
        if comparable:
            delta_exact = Decimal(right["projected_cost_exact"]) - Decimal(left["projected_cost_exact"])
        comparisons.append({
            "baseline_scenario_id": left["scenario_id"],
            "scenario_id": right["scenario_id"],
            "comparable": comparable,
            "projected_cost_delta": _json_number(delta_exact),
            "projected_cost_delta_exact": _decimal_text(delta_exact),
            "reasons": (
                [] if comparable
                else (["currency_basis_or_period_differs"] if not same_context else ["scenario_amount_incomplete"])
            ),
            "causal_effect": None,
        })

    summary_rows = []
    for summary in summaries:
        summary_rows.append([
            summary["scenario_title"], summary["currency"], summary["basis"],
            f"{summary['period']['start']}–{summary['period']['end']} ({summary['period']['timezone'] or '时区未知'})",
            _decimal_display(summary["actual_net_cost_exact"]), summary["actual_unknown_cost_lines"],
            _decimal_display(summary["income"]["actual_net_exact"]), _decimal_display(summary["income"]["quotes_exact"]), _decimal_display(summary["income"]["estimates_exact"]),
            _decimal_display(summary["income"]["known_projected_subtotal_exact"]), _decimal_display(summary["income"]["projected_receipts_exact"]),
            summary["income"]["scenario_income_coverage"],
            _decimal_display(summary["outstanding_commitments_exact"]), _decimal_display(summary["quotes_exact"]), _decimal_display(summary["estimates_exact"]),
            _decimal_display(summary["known_projected_cost_subtotal_exact"]), _decimal_display(summary["projected_cost_exact"]),
            _decimal_display(summary["hard_limit_exact"]), _decimal_display(summary["reserve_exact"]), _decimal_display(summary["available_after_reserve_exact"]),
            _decimal_display(summary["target_variance_exact"]), summary["budget_state"], summary["resource_state"],
            summary["overall_feasibility"],
        ])
    line_rows = []
    for index, line in enumerate(all_actuals_public, 1):
        period = line["period"]
        line_rows.append([
            "实绩/退款", line.get("cost_key") or line["category"], line["source_ref"] or _observation_source_row(line.get("source", {}), index), line["kind"], line["status"], line["category"],
            line["currency"], line["basis"], f"{period['start']}–{period['end']} ({period['timezone'] or '时区未知'})",
            _quantity_price_display(line),
            _decimal_display(line["base_amount_exact"]), line["tax_treatment"], _decimal_display(line["tax_amount_exact"]), _decimal_display(line["total_amount_exact"]),
            line["amount_state"], line["refund_of"],
            "计入期间" if line["included_in_budget_period"] else line["scope_reason"],
            _source_label(line["source"]),
        ])
    for scenario in public_scenarios:
        for index, line in enumerate(scenario["lines"], 1):
            period = line["period"]
            line_rows.append([
                scenario["title"], line.get("cost_key") or line["category"], line["source_ref"] or _observation_source_row(line.get("source", {}), index), line["kind"], line["status"], line["category"],
                line["currency"], line["basis"], f"{period['start']}–{period['end']} ({period['timezone'] or '时区未知'})",
                _quantity_price_display(line),
                _decimal_display(line["base_amount_exact"]), line["tax_treatment"], _decimal_display(line["tax_amount_exact"]), _decimal_display(line["total_amount_exact"]),
                line["amount_state"], line["refund_of"],
                "计入期间" if line["included_in_budget_period"] else line["scope_reason"],
                _source_label(line["source"]),
            ])
    resource_use_rows = []
    resource_check_rows = []
    resource_conflict_rows = []
    for scenario in public_scenarios:
        for use in scenario["resource_uses"]:
            resource_use_rows.append([
                scenario["title"], use["resource_name"], use["resource_id"], use["status"],
                f"{use['quantity']} {use['unit']}", use["capacity"], use["start"], use["end"],
                use["economic_value"], use["economic_currency"], _source_label(use["source"]),
            ])
        for check in scenario["resource_checks"]:
            resource_check_rows.append([
                scenario["title"], check["resource_name"], check["resource_id"], check["unit"],
                check["peak_use"], check["capacity"], check["state"],
            ])
        for conflict in scenario["resource_conflicts"]:
            resource_use_by_id = {
                use["id"]: use for use in scenario["resource_uses"]
            }
            conflicting_uses = [resource_use_by_id[item] for item in conflict["use_ids"] if item in resource_use_by_id]
            resource_conflict_rows.append([
                scenario["title"], conflict["resource_name"], conflict["resource_id"],
                f"{conflict['start']}–{conflict['end']}", conflict["used"], conflict["capacity"],
                ", ".join(f"{use['resource_name']} ({use['status']})" for use in conflicting_uses),
            ])
    comparison_rows = [[
        next(item["scenario_title"] for item in summaries if item["scenario_id"] == row["baseline_scenario_id"]),
        next(item["scenario_title"] for item in summaries if item["scenario_id"] == row["scenario_id"]),
        row["projected_cost_delta_exact"], "可比" if row["comparable"] else "不可比：" + ", ".join(row["reasons"]),
        "未建立因果效果",
    ] for row in comparisons]

    sections = [
        _section(
            "finance.scenario_summary",
            "预算与费用情景",
            "金额单位为行内币种；实绩净额、未结承诺、报价和估算分列。税务、账务基础、期间或来源覆盖不全时，完整预计成本显示未知。\n\n" +
            _md_table(
                ["方案", "币种", "基础", "期间/时区", "实际净成本", "未知成本行", "实际净收入", "报价收入", "估算收入", "已知预计收入小计", "完整预计收入", "收入覆盖", "未结成本承诺", "成本报价", "成本估算", "已知成本小计", "完整预计成本", "硬上限", "预留", "预留后可用", "相对成本目标差额", "预算状态", "资源状态", "整体可行性"],
                summary_rows,
            ),
            {"scenarios": summaries, "actuals": all_actuals_public, "deduplicated_actual_ids": actual_duplicates},
        ),
        _section(
            "finance.line_items",
            "费用明细与依据",
            "税额未知时不把税前小计当含税总额；相同稳定来源引用的重复费用已去重。互相替代的报价应放在不同方案中。下表列出全部输入行及其预算期间纳入状态。\n\n" +
            _md_table(
                ["账目/方案", "预算科目", "来源行/定位", "类型", "状态", "类别", "币种", "会计基础", "期间/时区", "数量×单价口径", "税前金额", "税务处理", "税额", "含税总额", "金额状态", "退款对象", "预算纳入", "依据"],
                line_rows,
            ) + (f"\n\n已识别并合并重复输入实际行：{len(actual_duplicates)}条。" if actual_duplicates else ""),
            {"actuals": all_actuals_public, "scenarios": public_scenarios, "deduplicated_actual_ids": actual_duplicates},
        ),
        _section(
            "finance.resource_capacity",
            "资源占用与容量",
            "按资源身份、单位、时段和声明容量核对并行占用。资源经济价值单独显示，不混入现金支出。没有完整资源可用信息时，整体可行性保持未确定。\n\n声明容量核对：\n\n" +
            _md_table(["方案", "资源", "资源记录", "单位", "峰值占用", "声明容量", "状态"], resource_check_rows) +
            "\n\n资源使用时段：\n\n" +
            _md_table(["方案", "资源", "资源记录", "使用状态", "数量", "容量", "开始", "结束", "经济价值", "价值币种", "依据"], resource_use_rows) +
            "\n\n容量冲突：\n\n" +
            _md_table(["方案", "资源", "资源记录", "冲突时段", "占用", "容量", "冲突使用"], resource_conflict_rows),
            {
                "scenarios": [
                    {
                        "scenario_id": scenario["id"],
                        "checks": scenario["resource_checks"],
                        "conflicts": scenario["resource_conflicts"],
                        "uses": scenario["uses"],
                    }
                    for scenario in scenarios
                ]
            },
        ),
        _section(
            "finance.scenario_comparison",
            "方案差额",
            "仅在同期间、同币种和同会计口径且两边完整成本均可计算时比较。差额是条件情景，不是已发生收益或因果效果。\n\n" +
            _md_table(["基准方案", "对比方案", "预计成本差额", "比较状态", "因果结论"], comparison_rows),
            {"comparisons": comparisons},
        ),
    ]
    if question:
        sections.insert(0, _section("finance.question", "预算问题", question, {"question": question}))
    limitations = [
        "不跨币种换算；缺少明确来源与时点的汇率时，异币种费用不进入预算合计。",
        "现金与应计分别处理；承诺金额表示当前未结余额，已实付与退款按各自行期间入账，避免把全额承诺再次加到付款上。",
        "估算和报价只构成给定情景；本操作不计算ROI、利润或因果增量，也不把互动量转换成收入。",
        "税务仅按每条费用给定的含税、税外金额、税率或税额计算；未知税不当零。",
        "资源核验只检查输入时段的容量冲突，不自动排程、预订资源或证明资源可用性；资源价值不计入现金支出。",
        "本模块不替代财报/XBRL解析和事实更正；输入事实仍须保主体、维度、完整期间、单位、披露/取得时间及原文定位。",
    ]
    return _answer(_requested_title(inputs, "预算与资源情景"), sections, limitations)


def _aggregate_resource_values(uses: list[dict]) -> list[dict]:
    totals: dict[str, Decimal] = {}
    unknown = set()
    for use in uses:
        currency = use["economic_currency"]
        if currency is None:
            if use["economic_value"] is not None:
                raise ValueError("resource economic value currency is required")
            unknown.add("UNKNOWN")
            continue
        if use["economic_value"] is None:
            unknown.add(currency)
        else:
            totals[currency] = totals.get(currency, Decimal(0)) + use["economic_value"]
    return [
        {"currency": currency, "known_value_subtotal": _json_number(value), "state": "INCOMPLETE" if currency in unknown else "KNOWN_INPUTS_ONLY"}
        for currency, value in sorted(totals.items())
    ] + ([{"currency": None, "known_value_subtotal": None, "state": "VALUE_OR_CURRENCY_UNKNOWN"}] if "UNKNOWN" in unknown else [])


def _resource_use_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "resource_id": row["resource_id"],
        "resource_name": row["resource_name"],
        "unit": row["unit"],
        "quantity": _json_number(row["quantity"]),
        "capacity": _json_number(row["capacity"]),
        "start": row["start"].isoformat(),
        "end": row["end"].isoformat(),
        "status": row["status"],
        "economic_value": _json_number(row["economic_value"]),
        "economic_currency": row["economic_currency"],
        "source": row["source"],
    }
