"""Source-preserving financial fact parsing and vintage-aware selection.

This module is a pure adapter over bytes/text already read by the caller's
authorized Material path. It performs no acquisition, credential access,
identity promotion, persistence, or Artifact writes. The caller retains the
Material ID, exact hash/version, permission result, capture time, and revisions.

Methods were adapted after reading the corresponding historical R6 bodies in
``D:\\LUMENIS_REBUILD\\序\\序境\\lumenis\\importer\\financial.py``
(``_fact``, ``parse_sec_json``, ``parse_xbrl_instance``, ``parse_disclosures``,
``select_quarterly_series``) and ``financial_tables.py``
(``parse_regional_statement``, ``parse_opendart_json``). This implementation
keeps disclosed facts separate from review candidates, does not split annual or
cumulative totals, preserves XBRL contexts/dimensions and original locators,
and supports the explicit 10,000 multiplier for ``万元``.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from lxml import etree
from lxml import html as lxml_html

from .business_analysis_economics import (
    BusinessAnalysisCancelled,
    _check_cancelled,
    _json_number_to_text,
)


FINANCIAL_OPERATIONS = frozenset({
    "financial_source_analysis",
    "financial_fact_corrections",
    "financial_quarterly_series",
    "financial_mmm_analysis",
})
FINANCIAL_SCHEMA = "oevira.financial-observations/v1"
MACRO_SCHEMA = "oevira.macro-observations/v1"
MACRO_UNITS = {
    "retail_sales": "CNY",
    "cpi_yoy_percent": "percent_yoy",
    "domestic_tourism_trips": "person_trips",
    "domestic_tourism_spend": "CNY",
    "monitored_district_footfall": "person_visits",
}
CONCEPTS = {
    "AdvertisingExpense": "advertising_expense",
    "AdvertisingAndMarketingExpense": "marketing_expense",
    "MarketingAndAdvertisingExpense": "marketing_expense",
    "SellingAndMarketingExpense": "selling_marketing_expense",
    "SellingGeneralAndAdministrativeExpense": "selling_general_admin_expense",
    "SellingExpense": "selling_expense",
    "ResearchAndDevelopmentExpense": "research_and_development_expense",
    "CostOfRevenue": "cost_of_revenue",
    "CostOfGoodsAndServicesSold": "cost_of_revenue",
    "CostOfGoodsSold": "cost_of_goods_sold",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "operating_income",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": "income_before_tax",
    "IncomeTaxExpenseBenefit": "income_tax_expense",
    "NetIncomeLoss": "net_income",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_cash_equivalents",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "StockholdersEquity": "stockholders_equity",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "revenue_including_tax",
    "Revenues": "revenue",
    "Revenue": "revenue",
    "SalesRevenueNet": "net_sales",
    "OperatingLeaseLeaseIncome": "rental_income",
    "RentalIncome": "rental_income",
}
MANUAL_METRICS = set(CONCEPTS.values()) | {
    "retail_sales", "tenant_sales_growth", "rental_income_growth", "occupancy",
    "footfall", "footfall_description", "segment_revenue",
}
MISSING_METRICS = (
    "marketing_expense", "advertising_expense", "selling_marketing_expense",
    "revenue", "retail_sales", "occupancy", "footfall",
)
REGIONAL_LABELS = {
    "Revenue": "revenue",
    "Revenue from contracts with customers": "revenue",
    "Rental income": "rental_income",
    "Gross rental income": "rental_income",
    "Advertising expenses": "advertising_expense",
    "Advertising expense": "advertising_expense",
    "Marketing expenses": "marketing_expense",
    "Selling and marketing expenses": "selling_marketing_expense",
    "Administrative and selling expenses": "selling_general_admin_expense",
    "Selling, general and administrative expenses": "selling_general_admin_expense",
    "营业收入": "revenue",
    "营业总收入": "total_operating_revenue",
    "销售费用": "selling_expense",
    "广告费": "advertising_expense",
    "广告宣传费": "advertising_promotion_expense",
    "租金收入": "rental_income",
}
DART_LABELS = {
    **REGIONAL_LABELS,
    "매출액": "revenue",
    "영업수익": "revenue",
    "광고선전비": "advertising_promotion_expense",
    "판매비와관리비": "selling_general_admin_expense",
    "판매비 및 관리비": "selling_general_admin_expense",
}
_NUMBER = re.compile(r"(?:\((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)|-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|—|–|-)\Z")
_SECCIK = re.compile(r"\d{1,10}\Z")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}\Z")
_DART_CORP = re.compile(r"\d{8}\Z")
_DART_RECEIPT = re.compile(r"\d{14}\Z")
_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_REGIONAL_MONEY = {
    "HK$M": ("HKD", Decimal(1_000_000)),
    "HK$ million": ("HKD", Decimal(1_000_000)),
    "HK$'000": ("HKD", Decimal(1_000)),
    "RMB'000": ("CNY", Decimal(1_000)),
    "RMB million": ("CNY", Decimal(1_000_000)),
    "RMB千元": ("CNY", Decimal(1_000)),
}


def evaluate_financial(operation: str, inputs: dict, *, is_cancelled=None) -> dict:
    if operation not in FINANCIAL_OPERATIONS:
        raise ValueError(f"Unknown financial operation: {operation!r}")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    _check_cancelled(is_cancelled)
    return {
        "financial_source_analysis": lambda value: _financial_source_analysis(value, is_cancelled=is_cancelled),
        "financial_fact_corrections": lambda value: _financial_fact_corrections(value, is_cancelled=is_cancelled),
        "financial_quarterly_series": lambda value: _financial_quarterly_series(value, is_cancelled=is_cancelled),
        "financial_mmm_analysis": lambda value: _financial_mmm_analysis(value, is_cancelled=is_cancelled),
    }[operation](inputs)


def _obj(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _str(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{field} must be a finite decimal number")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal number") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date") from exc


def _iso_date(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    return _date(value, field).isoformat()


def _instant(value: Any, field: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_copy(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (float("-inf") < value < float("inf")):
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field} contains a non-finite number")
        return format(value, "f")
    if isinstance(value, list):
        return [_json_copy(item, field) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_copy(item, field) for key, item in value.items()}
    raise ValueError(f"{field} must contain JSON-compatible values")


def _md_cell(value: Any) -> str:
    if value is None:
        return "未知"
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
    return "\n".join([head, rule, *["| " + " | ".join(_md_cell(value) for value in row) + " |" for row in rows]])


def _entity_label(entity: Any) -> str:
    if not isinstance(entity, dict):
        return "披露主体未知"
    identity = f"{entity.get('scheme', '未知标识')}:{entity.get('identifier', '未知')}"
    name = entity.get("name")
    return f"{name}（{identity}）" if name else identity


def _dimensions_label(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "无显式维度"
    return "; ".join(f"{dimension}={member}" for dimension, member in sorted(value.items()))


def _locator_label(value: Any) -> str:
    if not isinstance(value, dict):
        return "原文定位未知"
    if value.get("unit") == "json-pointer":
        return f"JSON {value.get('pointer', '定位未知')}"
    if value.get("unit") == "xml-element-index":
        return f"XBRL元素{value.get('index', '未知')}；context={value.get('context_ref', '未知')}；id={value.get('fact_id') or '无'}"
    if value.get("unit") == "xhtml-line":
        visible = "隐藏事实" if value.get("inline_visible") is False else "正文事实"
        return (
            f"原件第{value.get('line', '?')}行；{visible}；"
            f"概念={value.get('concept_qname') or '未知'}；"
            f"context={value.get('context_ref') or '未知'}；id={value.get('fact_id') or '无'}"
        )
    if value.get("unit") == "parsed-text":
        page = f"第{value['page']}页；" if value.get("page") is not None else ""
        return f"{page}字符{value.get('start', '?')}–{value.get('end', '?')}；{value.get('value_literal') or value.get('quote', '原文定位')}"
    return "原文定位未知"


def _fact_table_rows(facts: list[dict]) -> list[list[Any]]:
    rows = []
    for fact in facts:
        if fact.get("period_start") is None:
            period = f"时点 {fact.get('period_end', '未知')}"
        else:
            period = f"{fact.get('period_start')}–{fact.get('period_end')} ({fact.get('period_kind', '期间未知')})"
        source = fact.get("source_ref") or {}
        source_label = source.get("filename") or source.get("title") or "已保存原件"
        rows.append([
            fact.get("metric"), _entity_label(fact.get("filing_entity")), fact.get("filing_id"),
            fact.get("filed"), fact.get("captured_at"), period, fact.get("value"),
            fact.get("unit"), fact.get("currency"), _dimensions_label(fact.get("dimensions")),
            fact.get("disclosure_scope"), fact.get("statement_basis"), fact.get("source_scale"),
            fact.get("status"), _locator_label(fact.get("locator")), source_label,
        ])
    return rows


def _correction_fact_label(fact: dict) -> str:
    period = (
        f"{fact.get('period_start')}–{fact.get('period_end')}"
        if fact.get("period_start") is not None
        else f"时点 {fact.get('period_end') or '未知'}"
    )
    fields = [
        _financial_metric_label(fact.get("metric")),
        _entity_label(fact.get("filing_entity")),
        period,
    ]
    if fact.get("filing_id"):
        fields.append(f"申报 {fact['filing_id']}")
    if fact.get("filed"):
        fields.append(f"披露日 {fact['filed']}")
    fields.append(_locator_label(fact.get("locator")))
    return " · ".join(fields)


def _financial_material_label(source_ref: Any) -> str:
    if not isinstance(source_ref, dict):
        return "已保存原件"
    return str(source_ref.get("filename") or source_ref.get("title") or "已保存原件")


def _candidate_table_rows(candidates: list[dict]) -> list[list[Any]]:
    return [[
        row.get("metric"), row.get("label"), row.get("status"), row.get("reason"),
        row.get("value_literal"), row.get("amount_basis"), _locator_label(row.get("locator")),
    ] for row in candidates]


def _inline_fact_table_rows(facts: list[dict]) -> list[list[Any]]:
    rows = []
    for fact in facts:
        if fact.get("period_start") is None:
            period = f"时点 {fact.get('period_end', '未知')}"
        else:
            period = f"{fact.get('period_start')}–{fact.get('period_end')} ({fact.get('period_kind', '期间未知')})"
        source = fact.get("source_ref") or {}
        rows.append([
            _financial_metric_label(fact.get("metric")), fact.get("concept"),
            _entity_label(fact.get("filing_entity")), fact.get("filing_id"), fact.get("filed"),
            fact.get("captured_at"), period, fact.get("value_literal"), fact.get("value"),
            fact.get("unit"), fact.get("currency"), _dimensions_label(fact.get("dimensions")),
            fact.get("decimals") or fact.get("precision"), fact.get("source_scale"),
            "已披露" if fact.get("status") == "DISCLOSED" else "缺失",
            fact.get("transformation_qname") or "无（十进制原文）",
            "可见" if fact.get("inline_visible") else "隐藏",
            _locator_label(fact.get("locator")),
            source.get("filename") or source.get("title") or "已保存原件",
        ])
    return rows


def _inline_candidate_table_rows(candidates: list[dict]) -> list[list[Any]]:
    return [[
        _financial_metric_label(row.get("metric")), row.get("concept_qname") or row.get("label"),
        "待核" if row.get("status") == "REVIEW_REQUIRED" else row.get("status"),
        _inline_review_reason_label(row.get("reason")), row.get("value_literal"),
        row.get("transformation_qname"), row.get("unit_ref"), row.get("sign"),
        row.get("scale_exponent"), row.get("decimals") or row.get("precision"),
        "可见" if row.get("inline_visible") else "隐藏",
        _locator_label(row.get("locator")),
    ] for row in candidates]


_FINANCIAL_METRIC_LABELS = {
    "revenue": "营业收入",
    "revenue_including_tax": "含税收入",
    "net_sales": "净销售额",
    "marketing_expense": "营销费用",
    "advertising_expense": "广告费用",
    "selling_marketing_expense": "销售及营销费用",
    "selling_general_admin_expense": "销售及管理费用",
    "selling_expense": "销售费用",
    "research_and_development_expense": "研发费用",
    "cost_of_revenue": "营业成本",
    "cost_of_goods_sold": "销售商品成本",
    "gross_profit": "毛利",
    "operating_income": "营业利润",
    "income_before_tax": "税前利润",
    "income_tax_expense": "所得税费用",
    "net_income": "净利润",
    "cash_and_cash_equivalents": "现金及现金等价物",
    "total_assets": "资产总额",
    "total_liabilities": "负债总额",
    "stockholders_equity": "股东权益",
    "unmapped_financial_fact": "未映射指标",
}


_INLINE_REVIEW_REASON_LABELS = {
    "INLINE_CONCEPT_QNAME_UNRESOLVED": "概念名称无法按原件声明的名称空间解析",
    "INLINE_FACT_CONCEPT_MISSING": "原件未提供概念名称",
    "UNSUPPORTED_TAXONOMY": "分类标准不在当前适配范围；原概念和值仍保留待核",
    "UNSUPPORTED_FINANCIAL_CONCEPT": "标准概念未映射到当前财务指标；原值不进入序列",
    "INLINE_TARGET_SELECTION_REQUIRED": "原件包含多个目标文档，需明确选择后再归入序列",
    "INLINE_CONTEXT_REFERENCE_MISSING": "原件未提供期间与主体上下文引用",
    "INLINE_CONTEXT_NOT_FOUND": "引用的期间与主体上下文未在原件中找到",
    "DUPLICATE_CONTEXT_ID": "原件重复定义该上下文编号，无法确定适用期间",
    "CONTEXT_ENTITY_IDENTIFIER_MISSING": "上下文缺少披露主体标识",
    "CONTEXT_ENTITY_SCHEME_MISSING": "披露主体标识缺少标识体系",
    "CONTEXT_PERIOD_MISSING": "上下文缺少期间",
    "UNSUPPORTED_CONTEXT_PERIOD_SHAPE": "上下文期间结构不受支持",
    "CONTEXT_PERIOD_PRECISION_OR_VALUE_UNSUPPORTED": "期间日期格式或时间精度不受支持，未降精度推断",
    "CONTEXT_PERIOD_START_AFTER_END": "期间开始日晚于结束日",
    "CONTEXT_REPEATS_DIMENSION": "上下文重复定义同一维度",
    "CONTEXT_DIMENSION_QNAME_UNRESOLVED": "上下文维度名称无法解析",
    "INLINE_UNIT_REFERENCE_MISSING": "原件未提供计量单位引用",
    "INLINE_UNIT_NOT_FOUND": "原件中未找到引用的计量单位",
    "DUPLICATE_UNIT_ID": "原件重复定义该计量单位编号",
    "UNSUPPORTED_UNIT_STRUCTURE": "计量单位结构不受支持",
    "UNIT_MEASURE_QNAME_UNRESOLVED": "计量单位名称无法解析",
    "UNSUPPORTED_UNIT_DIVIDE_SHAPE": "复合计量单位结构不完整",
    "UNIT_MEASURE_MISSING": "计量单位缺少度量项",
    "INLINE_CONTINUATION_NOT_FOUND": "原文续接片段不存在",
    "DUPLICATE_INLINE_CONTINUATION_ID": "原文续接编号重复",
    "CYCLIC_INLINE_CONTINUATION": "原文续接形成循环，未拼接数值",
    "INLINE_TRANSFORMATION_QNAME_UNRESOLVED": "数值转换规则名称无法解析",
    "INLINE_TRANSFORMATION_REGISTRY_UNSUPPORTED": "数值转换规则版本不受支持",
    "INLINE_TRANSFORMATION_UNSUPPORTED": "数值转换规则未实现；原文字面保留且不转成金额",
    "INLINE_TRANSFORMATION_VALUE_INVALID": "原文字面不符合所声明的数值转换规则",
    "INLINE_NUMERIC_LEXICAL_VALUE_UNSUPPORTED": "原文字面不是明确的非负十进制数",
    "INLINE_NUMERIC_ACCURACY_ATTRIBUTE_MISSING": "数值缺少 decimals 或 precision 精度声明",
    "INLINE_NUMERIC_ACCURACY_ATTRIBUTES_CONFLICT": "decimals 与 precision 同时出现，无法确认精度口径",
    "INLINE_DECIMALS_ATTRIBUTE_INVALID": "decimals 精度声明格式无效",
    "INLINE_PRECISION_ATTRIBUTE_INVALID": "precision 精度声明格式无效",
    "INLINE_NIL_ATTRIBUTE_INVALID": "nil 状态声明无效",
    "NIL_INLINE_FACT_HAS_VALUE_TEXT": "事实声明为 nil 但同时包含数值文字",
    "INLINE_SCALE_ATTRIBUTE_INVALID": "scale 倍率指数格式无效",
    "INLINE_SCALE_OUT_OF_SUPPORTED_RANGE": "scale 倍率超出安全精度范围",
    "INLINE_SIGN_ATTRIBUTE_UNSUPPORTED": "sign 不是受支持的负号声明",
    "DUPLICATE_INLINE_FACT_ID": "事实编号重复，无法稳定定位",
    "INLINE_FACT_DOMAIN_VALIDATION_FAILED": "事实字段未通过财务日期或主体校验",
    "INLINE_SCALED_VALUE_NOT_REPRESENTABLE": "字面值与倍率无法保真表示",
    "INLINE_FRACTION_NOT_ADAPTED": "分数事实尚未按分子/分母精确展开",
    "NON_NUMERIC_INLINE_FACT_REQUIRES_REVIEW": "这是叙述事实，不是可直接用于金额计算的数值",
}


def _financial_metric_label(metric: Any) -> str:
    if not isinstance(metric, str):
        return "指标未知"
    return _FINANCIAL_METRIC_LABELS.get(metric, metric)


def _financial_state_label(state: Any) -> str:
    return {
        "AVAILABLE": "可用",
        "MISSING": "缺失",
        "CONFLICT": "冲突待核",
        "PARTIAL": "部分可用",
        "INSUFFICIENT": "条件不足",
    }.get(state, str(state or "状态未知"))


def _financial_vintage_label(vintage_basis: str) -> str:
    return "申报披露时间" if vintage_basis == "filed" else "工作区实际取得时间"


def _quarter_selection_reason_label(reason: Any) -> str | None:
    if reason is None:
        return None
    return {
        "CONFLICTING_FACT_CORRECTIONS_AT_SELECTED_VINTAGE": "选定信息截止下的更正关系有冲突",
        "CONFLICTING_FACTS_AT_SELECTED_VINTAGE": "选定信息截止下有多个不同数值",
        "NO_EXPLICIT_FACT_AVAILABLE_AS_OF": "截至所选时间没有明确披露事实",
        "DISCLOSED_MISSING_VALUE": "原件将该事实标记为缺失",
    }.get(reason, "存在未解决问题，需回看来源事实")


def _inline_review_reason_label(reason: Any) -> str:
    if not isinstance(reason, str):
        return "需要回看原件并确认"
    return _INLINE_REVIEW_REASON_LABELS.get(reason, "暂不自动转换；需回看原件确认")


def _inline_missing_reason_label(reason: Any) -> str:
    labels = {
        "INLINE_FACT_REQUIRES_REVIEW": "原件中有待核事实，未纳入数值序列",
        "NO_SUPPORTED_INLINE_FACT": "原件未提取到此指标；不代表完整报告未披露",
    }
    return labels.get(reason, "未形成可计算事实；原因需回看原件确认")


def _json_load(raw: bytes, field: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{field} contains a duplicate JSON key")
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError(f"{field} contains a non-finite JSON number: {value}")

    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=pairs,
            parse_float=Decimal,
            parse_constant=bad_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is not valid UTF-8 JSON") from exc


def _source_bytes(value: Any, field: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ValueError(f"{field} must be non-empty bytes from an authorized Material")
    return value


def _source_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty retained text")
    return value


def _period_kind(start: str | None, end: str) -> str:
    finish = _date(end, "period_end")
    if start is None:
        return "instant"
    begin = _date(start, "period_start")
    days = (finish - begin).days + 1
    if days < 1:
        raise ValueError("financial period starts after it ends")
    if begin.month in {1, 4, 7, 10} and begin.day == 1 and begin.year == finish.year:
        expected_month = begin.month + 2
        if finish.month == expected_month and finish.day == calendar.monthrange(finish.year, finish.month)[1]:
            return "quarter"
    if begin.month == 1 and begin.day == 1 and finish.month == 12 and finish.day == 31 and begin.year == finish.year:
        return "year"
    if 170 <= days <= 195:
        return "half_year_candidate"
    if 260 <= days <= 285:
        return "nine_month_candidate"
    return "duration"


def _filing_entity(value: Any, field: str = "filing_entity") -> dict:
    row = _obj(value, field)
    scheme = _str(row.get("scheme"), f"{field}.scheme")
    identifier = _str(row.get("identifier"), f"{field}.identifier")
    name = _str(row.get("name"), f"{field}.name", optional=True)
    return {"name": name, "scheme": scheme, "identifier": identifier}


def _fact(*, metric: str, concept: str, value: Any, unit: str, start: str | None, end: str,
          entity: dict, filing_id: str, filed: str | None, form: str | None,
          locator: dict, disclosure_scope: str = "filing_entity", dimensions: dict | None = None,
          statement_basis: str = "taxonomy-disclosed", source_scale: Any = 1) -> dict:
    metric = _str(metric, "metric")
    concept = _str(concept, "concept")
    unit = _str(unit, "unit")
    filing_id = _str(filing_id, "filing_id")
    entity = _filing_entity(entity)
    start = _iso_date(start, "period_start", optional=True)
    end = _iso_date(end, "period_end")
    filed = _iso_date(filed, "filed", optional=True)
    form = _str(form, "form", optional=True)
    if filed is not None and filed < end:
        raise ValueError("filing date cannot precede the fact period end")
    if disclosure_scope not in {"filing_entity", "dimensioned", "project", "segment", "brand"}:
        raise ValueError("disclosure_scope is invalid")
    dimensions = _json_copy(dimensions or {}, "dimensions")
    value_decimal = None if value is None else _decimal(value, "value")
    scale_decimal = _decimal(source_scale, "source_scale")
    row = {
        "metric": metric,
        "concept": concept,
        "value": _decimal_text(value_decimal),
        "unit": unit,
        "currency": unit if re.fullmatch(r"[A-Z]{3}", unit) else None,
        "period_start": start,
        "period_end": end,
        "period_kind": _period_kind(start, end),
        "filing_entity": entity,
        "filing_id": filing_id,
        "filed": filed,
        "form": form,
        "disclosure_scope": disclosure_scope,
        "dimensions": dimensions,
        "statement_basis": _str(statement_basis, "statement_basis"),
        "source_scale": _decimal_text(scale_decimal),
        "locator": _json_copy(_obj(locator, "locator"), "locator"),
        "status": "MISSING" if value_decimal is None else "DISCLOSED",
        "fact_verification": "SOURCE_ASSERTION_NOT_INDEPENDENTLY_VERIFIED",
    }
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row["observation_id"] = "FIN-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return row


def _missing_metrics(facts: list[dict]) -> list[dict]:
    present = {row["metric"] for row in facts}
    return [
        {"metric": metric, "status": "MISSING", "reason": "NOT_DISCLOSED_IN_SELECTED_SUPPORTED_CONCEPTS"}
        for metric in MISSING_METRICS if metric not in present
    ]


def _parse_sec_json(raw: bytes) -> dict:
    data = _json_load(raw, "SEC source")
    if not isinstance(data, dict) or not _SECCIK.fullmatch(str(data.get("cik", ""))):
        raise ValueError("SEC filing identity is missing or invalid")
    entity = {"name": _str(data.get("entityName"), "entityName", optional=True),
              "scheme": "SEC-CIK", "identifier": str(data["cik"]).zfill(10)}
    concepts = data.get("facts")
    companyconcept = concepts is None
    if companyconcept:
        taxonomy = data.get("taxonomy")
        tag = data.get("tag")
        if not isinstance(taxonomy, str) or not isinstance(tag, str):
            raise ValueError("SEC companyconcept requires taxonomy and tag")
        concepts = {taxonomy: {tag: data}}
    if not isinstance(concepts, dict):
        raise ValueError("SEC facts must be an object")
    facts = []
    for taxonomy, tags in concepts.items():
        if taxonomy not in {"us-gaap", "ifrs-full"}:
            continue
        if not isinstance(tags, dict):
            raise ValueError("SEC taxonomy facts must be an object")
        for tag, series in tags.items():
            metric = CONCEPTS.get(tag)
            if metric is None:
                continue
            if not isinstance(series, dict) or not isinstance(series.get("units"), dict):
                raise ValueError("SEC fact units are missing")
            for unit, entries in series["units"].items():
                if not isinstance(entries, list):
                    raise ValueError("SEC fact entries must be an array")
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict) or not {"val", "end", "accn", "filed", "form"}.issubset(entry):
                        raise ValueError("SEC fact is missing a required field")
                    accession = str(entry["accn"])
                    if not _ACCESSION.fullmatch(accession):
                        raise ValueError("SEC accession number is invalid")
                    pointer = ("" if companyconcept else f"/facts/{taxonomy}/{tag}") + f"/units/{unit}/{index}"
                    fact = _fact(
                        metric=metric, concept=f"{taxonomy}:{tag}", value=entry["val"], unit=unit,
                        start=entry.get("start"), end=entry["end"], entity=entity,
                        filing_id=accession, filed=entry["filed"], form=entry["form"],
                        locator={"unit": "json-pointer", "pointer": pointer},
                        statement_basis="standard-taxonomy; statement-versus-note context is not provided by SEC companyfacts",
                    )
                    fact.update(
                        fiscal_year=entry.get("fy"), fiscal_period=entry.get("fp"),
                        sec_frame=entry.get("frame"), calendar_alignment_inferred=False,
                    )
                    facts.append(fact)
    return {
        "format": "sec-companyconcept" if companyconcept else "sec-companyfacts",
        "filing_entity": entity,
        "facts": facts,
        "candidates": [],
        "missing": _missing_metrics(facts),
        "warnings": [
            "SEC facts describe the complete filing entity; no automatic brand, subsidiary, or property allocation is made.",
            "Every filing version is retained; choose an explicit filed-time or workspace-capture-time view for comparisons.",
            "Absence from supported standard concepts does not prove absence from the complete filing.",
        ],
    }


def _parse_xbrl_instance(raw: bytes, *, filing_id: str, filed: str | None, form: str | None) -> dict:
    if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XBRL DTD and entity declarations are not accepted")
    try:
        stack: list[dict[str, str]] = []
        pending: list[tuple[str | None, str]] = []
        namespaces: dict[int, dict[str | None, str]] = {}
        root = None
        for event, node in ET.iterparse(io.BytesIO(raw), events=("start-ns", "start", "end")):
            if event == "start-ns":
                pending.append(node)
            elif event == "start":
                current = dict(stack[-1]) if stack else {}
                current.update(pending)
                pending.clear()
                stack.append(current)
                namespaces[id(node)] = current
                if root is None:
                    root = node
            else:
                if not stack:
                    raise ValueError("XBRL namespace stack is invalid")
                stack.pop()
    except ET.ParseError as exc:
        raise ValueError("XBRL instance is not well-formed XML") from exc
    xbrli = "http://www.xbrl.org/2003/instance"
    xbrldi = "http://xbrl.org/2006/xbrldi"
    if root is None or root.tag != f"{{{xbrli}}}xbrl":
        raise ValueError("An XBRL instance root is required")

    def qname(value: Any, element, field: str) -> str:
        text = _str(value, field)
        if not re.fullmatch(r"(?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*", text):
            raise ValueError(f"{field} is not a valid QName")
        prefix, local = text.split(":", 1) if ":" in text else (None, text)
        namespace = namespaces[id(element)].get(prefix)
        if not namespace:
            raise ValueError(f"{field} uses an unbound QName prefix")
        return f"{{{namespace}}}{local}"

    def measure(element) -> tuple[str, str]:
        expanded = qname(element.text, element, "XBRL measure")
        namespace, local = expanded[1:].split("}", 1)
        if namespace == "http://www.xbrl.org/2003/iso4217" and re.fullmatch(r"[A-Z]{3}", local):
            return local, expanded
        if namespace == xbrli and local in {"pure", "shares"}:
            return local, expanded
        return expanded, expanded

    contexts: dict[str, dict] = {}
    units: dict[str, dict] = {}
    for item in root.findall(f"{{{xbrli}}}context"):
        identity = item.find(f"{{{xbrli}}}entity/{{{xbrli}}}identifier")
        if identity is None or not identity.text:
            raise ValueError("XBRL context has no entity identifier")
        scheme = _str(identity.get("scheme"), "XBRL entity scheme")
        key = item.get("id")
        if not key or key in contexts:
            raise ValueError("XBRL context ID is missing or duplicated")
        start = item.findtext(f"{{{xbrli}}}period/{{{xbrli}}}startDate")
        end = item.findtext(f"{{{xbrli}}}period/{{{xbrli}}}endDate") or item.findtext(
            f"{{{xbrli}}}period/{{{xbrli}}}instant"
        )
        if not end:
            raise ValueError("XBRL context period end is missing")
        _date(end, "XBRL period end")
        if start is not None:
            _date(start, "XBRL period start")
        dimensions = {}
        members = item.findall(f".//{{{xbrldi}}}explicitMember") + item.findall(f".//{{{xbrldi}}}typedMember")
        for member in members:
            dimension = qname(member.get("dimension"), member, "XBRL dimension")
            if dimension in dimensions:
                raise ValueError("XBRL context repeats a dimension")
            dimensions[dimension] = (
                qname(member.text, member, "XBRL explicit member")
                if member.tag.endswith("explicitMember")
                else ET.tostring(member, encoding="unicode")
            )
        contexts[key] = {
            "entity": {"name": None, "scheme": scheme, "identifier": identity.text.strip()},
            "start": start,
            "end": end,
            "dimensions": dimensions,
        }

    for item in root.findall(f"{{{xbrli}}}unit"):
        key = item.get("id")
        if not key or key in units:
            raise ValueError("XBRL unit ID is missing or duplicated")
        measures = item.findall(f"{{{xbrli}}}measure")
        divide = item.find(f"{{{xbrli}}}divide")
        if measures and divide is None:
            resolved = [measure(value) for value in measures]
            units[key] = {"label": "*".join(value[0] for value in resolved),
                          "numerator": [value[1] for value in resolved], "denominator": []}
        elif divide is not None and not measures:
            numerator = divide.findall(f"{{{xbrli}}}unitNumerator/{{{xbrli}}}measure")
            denominator = divide.findall(f"{{{xbrli}}}unitDenominator/{{{xbrli}}}measure")
            if not numerator or not denominator:
                raise ValueError("XBRL divide unit is incomplete")
            top, bottom = [measure(value) for value in numerator], [measure(value) for value in denominator]
            units[key] = {
                "label": "*".join(value[0] for value in top) + "/" + "*".join(value[0] for value in bottom),
                "numerator": [value[1] for value in top],
                "denominator": [value[1] for value in bottom],
            }
        else:
            raise ValueError("XBRL unit structure is unsupported")

    supported_namespace = re.compile(
        r"(?:http://fasb\.org/us-gaap/20\d{2}|https?://xbrl\.ifrs\.org/taxonomy/20\d{2}-\d{2}-\d{2}/ifrs-full)\Z"
    )
    facts = []
    entities = {}
    for index, item in enumerate(root):
        if not isinstance(item.tag, str) or "}" not in item.tag:
            continue
        namespace, local = item.tag[1:].split("}", 1)
        metric = CONCEPTS.get(local)
        if metric is None or not supported_namespace.fullmatch(namespace):
            continue
        context = contexts.get(item.get("contextRef"))
        unit = units.get(item.get("unitRef"))
        if context is None or unit is None:
            raise ValueError("XBRL fact references a missing context or unit")
        nil = item.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}
        if not nil and item.text is None:
            raise ValueError("A non-nil XBRL fact has no numeric value")
        fact = _fact(
            metric=metric, concept=item.tag, value=None if nil else item.text,
            unit=unit["label"], start=context["start"], end=context["end"],
            entity=context["entity"], filing_id=filing_id, filed=filed, form=form,
            disclosure_scope="dimensioned" if context["dimensions"] else "filing_entity",
            dimensions=context["dimensions"],
            locator={"unit": "xml-element-index", "index": index,
                     "context_ref": item.get("contextRef"), "fact_id": item.get("id")},
        )
        fact["unit_definition"] = unit
        fact["decimals"] = item.get("decimals")
        fact["precision"] = item.get("precision")
        if unit["denominator"] or len(unit["numerator"]) != 1 or not unit["numerator"][0].startswith(
            "{http://www.xbrl.org/2003/iso4217}"
        ):
            fact["currency"] = None
        facts.append(fact)
        entities[(context["entity"]["scheme"], context["entity"]["identifier"])] = context["entity"]
    return {
        "format": "xbrl-instance",
        "filing_entities": list(entities.values()),
        "facts": facts,
        "candidates": [],
        "missing": [] if facts else [{"status": "MISSING", "reason": "NO_SUPPORTED_STANDARD_FACTS"}],
        "warnings": [
            "This adapter handles supported instance facts only; it is not inline-XBRL or taxonomy validation.",
            "Dimensions and units are preserved exactly; no dimension-to-brand or dimension-to-project mapping is inferred.",
        ],
    }


_INLINE_XBRL_NAMESPACES = frozenset({
    "http://www.xbrl.org/2008/inlineXBRL",
    "http://www.xbrl.org/2013/inlineXBRL",
})
_INLINE_TRANSFORMATION_NAMESPACES = frozenset({
    "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26",
    "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12",
    "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16",
})
_INLINE_TAXONOMY_NAMESPACE = re.compile(
    r"(?:http://fasb\.org/us-gaap/20\d{2}|https?://xbrl\.ifrs\.org/taxonomy/20\d{2}-\d{2}-\d{2}/ifrs-full)\Z"
)
_INLINE_TRANSFORMS = {
    "numdotdecimal": (".", ",", False),
    "numcommadecimal": (",", ".", False),
    "num-dot-decimal": (".", ",", False),
    "num-dot-decimal-apos": (".", ",", True),
    "num-comma-decimal": (",", ".", False),
    "num-comma-decimal-apos": (",", ".", True),
}
_INLINE_INDIAN_DECIMAL_TRANSFORMS = frozenset({"numdotdecimalin"})
_INLINE_MAX_SCALE_EXPONENT = 1000
_XBRLI_NAMESPACE = "http://www.xbrl.org/2003/instance"
_XBRLDI_NAMESPACE = "http://xbrl.org/2006/xbrldi"
_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"


def _parse_inline_xbrl(
    raw: bytes,
    *,
    filing_id: str,
    filed: str | None,
    form: str | None,
    is_cancelled=None,
) -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("inline XBRL source must be non-empty bytes")
    if b"\x00" in raw or re.search(br"<!ENTITY\b", raw, re.IGNORECASE):
        raise ValueError("Inline XBRL entity declarations are not accepted")

    xml_parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        root = etree.fromstring(raw, parser=xml_parser)
        parse_mode = "xhtml-xml"
        recovery_count = 0
    except etree.XMLSyntaxError:
        html_parser = lxml_html.HTMLParser(
            recover=True,
            no_network=True,
            huge_tree=False,
            remove_comments=False,
        )
        try:
            root = etree.fromstring(raw, parser=html_parser)
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise ValueError("Inline XBRL source is not parseable HTML/XHTML") from exc
        parse_mode = "html-recovery"
        recovery_count = len(html_parser.error_log)

    namespace_cache: dict[int, dict[str | None, str]] = {}

    def namespace_map(element) -> dict[str | None, str]:
        identity = id(element)
        cached = namespace_cache.get(identity)
        if cached is not None:
            return cached
        if parse_mode == "xhtml-xml":
            result = dict(element.nsmap or {})
        else:
            result = {}
            ancestors = list(element.iterancestors())
            for item in reversed(ancestors + [element]):
                for name, value in item.attrib.items():
                    if name.lower() == "xmlns":
                        result[None] = value
                    elif name.lower().startswith("xmlns:"):
                        result[name.split(":", 1)[1].lower()] = value
        namespace_cache[identity] = result
        return result

    def tag_name(element) -> tuple[str | None, str]:
        tag = element.tag
        if not isinstance(tag, str):
            return None, ""
        if tag.startswith("{") and "}" in tag:
            namespace, local = tag[1:].split("}", 1)
            return namespace, local
        if ":" in tag:
            prefix, local = tag.split(":", 1)
            return namespace_map(element).get(prefix.lower() if parse_mode == "html-recovery" else prefix), local
        return namespace_map(element).get(None), tag

    def attr(element, local_name: str, namespace: str | None = None) -> str | None:
        target = local_name.lower()
        bindings = namespace_map(element)
        for key, value in element.attrib.items():
            if key.startswith("{") and "}" in key:
                key_namespace, key_local = key[1:].split("}", 1)
            elif ":" in key:
                prefix, key_local = key.split(":", 1)
                key_namespace = bindings.get(prefix.lower())
            else:
                key_namespace, key_local = None, key
            if key_namespace == namespace and key_local.lower() == target:
                return value
        return None

    def qname(value: Any, element, field: str) -> str:
        text = _str(value, field)
        if not re.fullmatch(r"(?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*", text):
            raise ValueError(f"{field} is not a QName")
        prefix, local = text.split(":", 1) if ":" in text else (None, text)
        namespace = namespace_map(element).get(
            (prefix.lower() if parse_mode == "html-recovery" else prefix) if prefix else None
        )
        if not namespace:
            raise ValueError(f"{field} uses an unbound QName prefix")
        return f"{{{namespace}}}{local}"

    def qname_parts(value: str) -> tuple[str, str]:
        namespace, local = value[1:].split("}", 1)
        return namespace, local

    def inline_node(element, local_name: str | None = None) -> bool:
        namespace, local = tag_name(element)
        return (
            namespace in _INLINE_XBRL_NAMESPACES
            and (local_name is None or local.lower() == local_name.lower())
        )

    def xbrl_node(element, local_name: str) -> bool:
        namespace, local = tag_name(element)
        return namespace == _XBRLI_NAMESPACE and local.lower() == local_name.lower()

    def direct_children(element, predicate) -> list:
        return [child for child in element if isinstance(child.tag, str) and predicate(child)]

    def all_descendants(element, predicate) -> list:
        return [child for child in element.iter() if child is not element and isinstance(child.tag, str) and predicate(child)]

    if not isinstance(root.tag, str) or tag_name(root)[1].lower() != "html":
        raise ValueError("inline-xbrl requires one HTML/XHTML document")
    header_candidates = [element for element in root.iter() if isinstance(element.tag, str) and inline_node(element, "header")]
    if not header_candidates:
        apparent = [
            element for element in root.iter()
            if isinstance(element.tag, str) and tag_name(element)[1].lower() == "header"
        ]
        if apparent:
            raise ValueError("Inline XBRL namespace version is unsupported")
        raise ValueError("Inline XBRL document has no ix:header")
    if len(header_candidates) != 1:
        raise ValueError("Inline XBRL document must contain exactly one ix:header")
    header = header_candidates[0]
    inline_elements = [element for element in root.iter() if isinstance(element.tag, str) and inline_node(element)]
    _check_cancelled(is_cancelled)

    resources = [
        element for element in all_descendants(header, lambda item: inline_node(item, "resources"))
    ]
    resource_nodes = [item for resource in resources for item in resource.iter()]

    def context_record(element) -> tuple[dict | None, str | None]:
        identity = next((item for item in all_descendants(element, lambda child: xbrl_node(child, "identifier"))), None)
        if identity is None or not (identity.text or "").strip():
            return None, "CONTEXT_ENTITY_IDENTIFIER_MISSING"
        entity_node = next((item for item in all_descendants(element, lambda child: xbrl_node(child, "entity"))), None)
        scheme = attr(identity, "scheme")
        if entity_node is None or not isinstance(scheme, str) or not scheme.strip():
            return None, "CONTEXT_ENTITY_SCHEME_MISSING"
        period = next((item for item in direct_children(element, lambda child: xbrl_node(child, "period"))), None)
        if period is None:
            return None, "CONTEXT_PERIOD_MISSING"
        starts = direct_children(period, lambda child: xbrl_node(child, "startDate"))
        ends = direct_children(period, lambda child: xbrl_node(child, "endDate"))
        instants = direct_children(period, lambda child: xbrl_node(child, "instant"))
        if len(instants) == 1 and not starts and not ends:
            start = None
            end = (instants[0].text or "").strip()
        elif len(starts) == 1 and len(ends) == 1 and not instants:
            start = (starts[0].text or "").strip()
            end = (ends[0].text or "").strip()
        else:
            return None, "UNSUPPORTED_CONTEXT_PERIOD_SHAPE"
        try:
            _date(end, "inline XBRL context period end")
            if start is not None:
                _date(start, "inline XBRL context period start")
                if start > end:
                    return None, "CONTEXT_PERIOD_START_AFTER_END"
        except ValueError:
            return None, "CONTEXT_PERIOD_PRECISION_OR_VALUE_UNSUPPORTED"

        dimensions = {}
        for member in all_descendants(element, lambda child: (
            tag_name(child)[0] == _XBRLDI_NAMESPACE
            and tag_name(child)[1].lower() in {"explicitmember", "typedmember"}
        )):
            raw_dimension = attr(member, "dimension")
            try:
                dimension = qname(raw_dimension, member, "XBRL dimension")
                if dimension in dimensions:
                    return None, "CONTEXT_REPEATS_DIMENSION"
                if tag_name(member)[1].lower() == "explicitmember":
                    value = qname(member.text, member, "XBRL explicit member")
                else:
                    value = etree.tostring(member, encoding="unicode")
                dimensions[dimension] = value
            except ValueError:
                return None, "CONTEXT_DIMENSION_QNAME_UNRESOLVED"
        return {
            "entity": {
                "name": None,
                "scheme": scheme.strip(),
                "identifier": identity.text.strip(),
            },
            "start": start,
            "end": end,
            "dimensions": dimensions,
            "context_id": attr(element, "id"),
        }, None

    def unit_record(element) -> tuple[dict | None, str | None]:
        measures = all_descendants(element, lambda child: xbrl_node(child, "measure"))
        divide_nodes = all_descendants(element, lambda child: xbrl_node(child, "divide"))
        if len(divide_nodes) > 1 or (measures and divide_nodes):
            return None, "UNSUPPORTED_UNIT_STRUCTURE"
        try:
            expanded = [qname(item.text, item, "XBRL measure") for item in measures]
        except ValueError:
            return None, "UNIT_MEASURE_QNAME_UNRESOLVED"
        if divide_nodes:
            divide = divide_nodes[0]
            numerators = all_descendants(divide, lambda child: xbrl_node(child, "measure"))
            numerator_parent = next((item for item in all_descendants(divide, lambda child: xbrl_node(child, "unitNumerator"))), None)
            denominator_parent = next((item for item in all_descendants(divide, lambda child: xbrl_node(child, "unitDenominator"))), None)
            top_nodes = direct_children(numerator_parent, lambda child: xbrl_node(child, "measure")) if numerator_parent is not None else []
            bottom_nodes = direct_children(denominator_parent, lambda child: xbrl_node(child, "measure")) if denominator_parent is not None else []
            if not top_nodes or not bottom_nodes or len(numerators) != len(top_nodes) + len(bottom_nodes):
                return None, "UNSUPPORTED_UNIT_DIVIDE_SHAPE"
            try:
                top = [qname(item.text, item, "XBRL unit numerator") for item in top_nodes]
                bottom = [qname(item.text, item, "XBRL unit denominator") for item in bottom_nodes]
            except ValueError:
                return None, "UNIT_MEASURE_QNAME_UNRESOLVED"
            return {
                "label": "*".join(_unit_measure_label(item) for item in top)
                + "/" + "*".join(_unit_measure_label(item) for item in bottom),
                "numerator": top,
                "denominator": bottom,
            }, None
        if not expanded:
            return None, "UNIT_MEASURE_MISSING"
        return {
            "label": "*".join(_unit_measure_label(item) for item in expanded),
            "numerator": expanded,
            "denominator": [],
        }, None

    contexts: dict[str, tuple[dict | None, str | None]] = {}
    units: dict[str, tuple[dict | None, str | None]] = {}
    for item in resource_nodes:
        _check_cancelled(is_cancelled)
        if not isinstance(item.tag, str):
            continue
        if xbrl_node(item, "context"):
            key = attr(item, "id")
            if not key:
                continue
            record = context_record(item)
            contexts[key] = (None, "DUPLICATE_CONTEXT_ID") if key in contexts else record
        elif xbrl_node(item, "unit"):
            key = attr(item, "id")
            if not key:
                continue
            record = unit_record(item)
            units[key] = (None, "DUPLICATE_UNIT_ID") if key in units else record

    continuation_ids: dict[str, Any] = {}
    duplicate_continuations: set[str] = set()
    fact_ids: dict[str, int] = {}
    for item in inline_elements:
        _check_cancelled(is_cancelled)
        local = tag_name(item)[1].lower()
        identity = attr(item, "id")
        if identity:
            fact_ids[identity] = fact_ids.get(identity, 0) + 1
            if local == "continuation":
                if identity in continuation_ids:
                    duplicate_continuations.add(identity)
                else:
                    continuation_ids[identity] = item

    def node_text(element) -> str:
        parts = []

        def visit(node):
            if node.text:
                parts.append(node.text)
            for child in node:
                if not (isinstance(child.tag, str) and inline_node(child, "exclude")):
                    visit(child)
                if child.tail:
                    parts.append(child.tail)

        visit(element)
        return "".join(parts)

    def text_and_continuation(element) -> tuple[str, str | None, list[str]]:
        literal = node_text(element)
        current = attr(element, "continuedAt")
        seen = set()
        used = []
        while current:
            _check_cancelled(is_cancelled)
            if current in seen:
                return literal, "CYCLIC_INLINE_CONTINUATION", used
            if current in duplicate_continuations:
                return literal, "DUPLICATE_INLINE_CONTINUATION_ID", used
            continuation = continuation_ids.get(current)
            if continuation is None:
                return literal, "INLINE_CONTINUATION_NOT_FOUND", used
            seen.add(current)
            used.append(current)
            literal += node_text(continuation)
            current = attr(continuation, "continuedAt")
        return literal, None, used

    def visible_fact(element) -> bool:
        current = element
        while current is not None:
            if inline_node(current, "hidden"):
                return False
            current = current.getparent()
        return True

    def transform_value(literal: str, format_qname: str | None) -> tuple[Decimal | None, str | None]:
        text = literal.strip()
        if format_qname is None:
            if not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", text):
                return None, "INLINE_NUMERIC_LEXICAL_VALUE_UNSUPPORTED"
            try:
                return Decimal(text), None
            except InvalidOperation:
                return None, "INLINE_NUMERIC_LEXICAL_VALUE_UNSUPPORTED"
        namespace, local = qname_parts(format_qname)
        if namespace not in _INLINE_TRANSFORMATION_NAMESPACES:
            return None, "INLINE_TRANSFORMATION_REGISTRY_UNSUPPORTED"
        if local == "fixed-zero":
            return Decimal(0), None
        if local == "zerodash":
            return (Decimal(0), None) if text == "-" else (None, "INLINE_TRANSFORMATION_VALUE_INVALID")
        if local in _INLINE_INDIAN_DECIMAL_TRANSFORMS:
            if text.count(".") > 1:
                return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
            integer, separator, fraction = text.partition(".")
            if separator and not fraction:
                return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
            if not (re.fullmatch(r"\d+", integer) or re.fullmatch(r"\d{1,2}(?:,\d{2})*,\d{3}", integer)):
                return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
            if separator and not re.fullmatch(r"\d+", fraction):
                return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
            canonical = integer.replace(",", "") + ("." + fraction if separator else "")
            try:
                return Decimal(canonical), None
            except InvalidOperation:
                return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
        configuration = _INLINE_TRANSFORMS.get(local)
        if configuration is None:
            return None, "INLINE_TRANSFORMATION_UNSUPPORTED"
        decimal_separator, group_separator, allow_apostrophe = configuration
        if text.count(decimal_separator) > 1:
            return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
        integer, separator, fraction = text.partition(decimal_separator)
        if separator and not fraction:
            return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
        group_chars = re.escape(group_separator) + r"\s"
        if allow_apostrophe:
            group_chars += "'"
        if not re.fullmatch(rf"\d+(?:[{group_chars}]\d+)*", integer):
            return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
        if separator and not re.fullmatch(r"\d+", fraction):
            return None, "INLINE_TRANSFORMATION_VALUE_INVALID"
        normalized = re.sub(rf"[{group_chars}]", "", integer)
        canonical = normalized + ("." + fraction if separator else "")
        try:
            return Decimal(canonical), None
        except InvalidOperation:
            return None, "INLINE_TRANSFORMATION_VALUE_INVALID"

    def fact_locator(element, concept_qname: str | None, context_ref: str | None, fact_id: str | None) -> dict:
        return {
            "unit": "xhtml-line",
            "line": element.sourceline,
            "fact_id": fact_id,
            "context_ref": context_ref,
            "concept_qname": concept_qname,
            "inline_visible": visible_fact(element),
        }

    def add_candidate(
        element,
        *,
        reason: str,
        fact_kind: str,
        concept_qname: str | None,
        metric: str | None,
        literal: str | None = None,
        transformation_qname: str | None = None,
        used_continuations: list[str] | None = None,
    ):
        context_ref = attr(element, "contextRef")
        fact_id = attr(element, "id")
        candidate = {
            "metric": metric or "unmapped_financial_fact",
            "concept_qname": concept_qname or attr(element, "name"),
            "label": concept_qname or attr(element, "name") or tag_name(element)[1],
            "status": "REVIEW_REQUIRED",
            "reason": reason,
            "fact_kind": fact_kind,
            "value_literal": literal,
            "transformation_qname": transformation_qname,
            "context_ref": context_ref,
            "unit_ref": attr(element, "unitRef"),
            "sign": attr(element, "sign"),
            "scale_exponent": attr(element, "scale"),
            "decimals": attr(element, "decimals"),
            "precision": attr(element, "precision"),
            "continued_at": attr(element, "continuedAt"),
            "continuation_ids": used_continuations or [],
            "inline_visible": visible_fact(element),
            "locator": fact_locator(element, concept_qname, context_ref, fact_id),
        }
        candidates.append(candidate)

    facts = []
    candidates = []
    filing_entities = {}
    inline_fact_elements = [
        element for element in inline_elements
        if tag_name(element)[1].lower() in {"nonfraction", "fraction", "nonnumeric"}
    ]
    for item in inline_fact_elements:
        _check_cancelled(is_cancelled)
        local_kind = tag_name(item)[1].lower()
        fact_kind = {"nonfraction": "numeric", "fraction": "fraction", "nonnumeric": "non-numeric"}[local_kind]
        raw_name = attr(item, "name")
        concept_qname = None
        metric = None
        taxonomy_supported = False
        concept_error = None
        if raw_name is not None:
            try:
                concept_qname = qname(raw_name, item, "Inline XBRL concept")
                taxonomy_namespace, local_concept = qname_parts(concept_qname)
                taxonomy_supported = bool(_INLINE_TAXONOMY_NAMESPACE.fullmatch(taxonomy_namespace))
                if taxonomy_supported:
                    metric = CONCEPTS.get(local_concept)
            except ValueError:
                concept_error = "INLINE_CONCEPT_QNAME_UNRESOLVED"

        literal = None
        used_continuations = []
        continuation_error = None
        if local_kind in {"nonfraction", "fraction"}:
            literal, continuation_error, used_continuations = text_and_continuation(item)
        if local_kind == "nonnumeric":
            add_candidate(
                item,
                reason=concept_error or ("UNSUPPORTED_TAXONOMY" if not taxonomy_supported else "NON_NUMERIC_INLINE_FACT_REQUIRES_REVIEW"),
                fact_kind=fact_kind,
                concept_qname=concept_qname,
                metric=metric,
            )
            continue
        if local_kind == "fraction":
            add_candidate(
                item,
                reason=concept_error or continuation_error or ("UNSUPPORTED_TAXONOMY" if not taxonomy_supported else "INLINE_FRACTION_NOT_ADAPTED"),
                fact_kind=fact_kind,
                concept_qname=concept_qname,
                metric=metric,
                literal=literal,
                used_continuations=used_continuations,
            )
            continue

        context_ref = attr(item, "contextRef")
        unit_ref = attr(item, "unitRef")
        format_value = attr(item, "format")
        transformation_qname = None
        transform_error = None
        if format_value is not None:
            try:
                transformation_qname = qname(format_value, item, "Inline XBRL format")
            except ValueError:
                transform_error = "INLINE_TRANSFORMATION_QNAME_UNRESOLVED"

        reason = concept_error
        if reason is None and not raw_name:
            reason = "INLINE_FACT_CONCEPT_MISSING"
        if reason is None and not taxonomy_supported:
            reason = "UNSUPPORTED_TAXONOMY"
        if reason is None and metric is None:
            reason = "UNSUPPORTED_FINANCIAL_CONCEPT"
        if reason is None and attr(item, "target") is not None:
            reason = "INLINE_TARGET_SELECTION_REQUIRED"
        if reason is None and not context_ref:
            reason = "INLINE_CONTEXT_REFERENCE_MISSING"
        if reason is None and not unit_ref:
            reason = "INLINE_UNIT_REFERENCE_MISSING"
        if reason is None and context_ref not in contexts:
            reason = "INLINE_CONTEXT_NOT_FOUND"
        context = contexts.get(context_ref) if context_ref else None
        if reason is None and context is not None and context[1] is not None:
            reason = context[1]
        if reason is None and unit_ref not in units:
            reason = "INLINE_UNIT_NOT_FOUND"
        unit = units.get(unit_ref) if unit_ref else None
        if reason is None and unit is not None and unit[1] is not None:
            reason = unit[1]
        if reason is None and continuation_error is not None:
            reason = continuation_error
        if reason is None and transform_error is not None:
            reason = transform_error

        decimals = attr(item, "decimals")
        precision = attr(item, "precision")
        nil_value = attr(item, "nil", _XSI_NAMESPACE)
        is_nil = nil_value in {"true", "1"}
        if reason is None and nil_value is not None and nil_value not in {"true", "false", "1", "0"}:
            reason = "INLINE_NIL_ATTRIBUTE_INVALID"
        if reason is None and not is_nil:
            if decimals is None and precision is None:
                reason = "INLINE_NUMERIC_ACCURACY_ATTRIBUTE_MISSING"
            elif decimals is not None and precision is not None:
                reason = "INLINE_NUMERIC_ACCURACY_ATTRIBUTES_CONFLICT"
            elif decimals is not None and not re.fullmatch(r"(?:INF|-?\d+)", decimals):
                reason = "INLINE_DECIMALS_ATTRIBUTE_INVALID"
            elif precision is not None and not re.fullmatch(r"(?:INF|[1-9]\d*)", precision):
                reason = "INLINE_PRECISION_ATTRIBUTE_INVALID"

        if reason is None and is_nil and literal.strip():
            reason = "NIL_INLINE_FACT_HAS_VALUE_TEXT"

        numeric = None
        if reason is None and not is_nil:
            numeric, reason = transform_value(literal, transformation_qname)

        scale_text = attr(item, "scale")
        scale = 0
        if reason is None and scale_text is not None:
            if not re.fullmatch(r"-?\d+", scale_text):
                reason = "INLINE_SCALE_ATTRIBUTE_INVALID"
            elif len(scale_text.lstrip("-")) > 4:
                reason = "INLINE_SCALE_OUT_OF_SUPPORTED_RANGE"
            else:
                scale = int(scale_text)
                if abs(scale) > _INLINE_MAX_SCALE_EXPONENT:
                    reason = "INLINE_SCALE_OUT_OF_SUPPORTED_RANGE"
        sign = attr(item, "sign")
        if reason is None and sign not in {None, "-"}:
            reason = "INLINE_SIGN_ATTRIBUTE_UNSUPPORTED"

        if reason is not None:
            add_candidate(
                item,
                reason=reason,
                fact_kind=fact_kind,
                concept_qname=concept_qname,
                metric=metric,
                literal=literal,
                transformation_qname=transformation_qname,
                used_continuations=used_continuations,
            )
            continue

        context_data = context[0]
        unit_data = unit[0]
        if not is_nil:
            try:
                numeric = numeric.scaleb(scale)
            except (InvalidOperation, AttributeError) as error:
                add_candidate(
                    item,
                    reason="INLINE_SCALED_VALUE_NOT_REPRESENTABLE",
                    fact_kind=fact_kind,
                    concept_qname=concept_qname,
                    metric=metric,
                    literal=literal,
                    transformation_qname=transformation_qname,
                    used_continuations=used_continuations,
                )
                continue
            if sign == "-":
                numeric = -numeric
        fact_id = attr(item, "id")
        if fact_id and fact_ids.get(fact_id, 0) > 1:
            add_candidate(
                item,
                reason="DUPLICATE_INLINE_FACT_ID",
                fact_kind=fact_kind,
                concept_qname=concept_qname,
                metric=metric,
                literal=literal,
                transformation_qname=transformation_qname,
                used_continuations=used_continuations,
            )
            continue
        locator = fact_locator(item, concept_qname, context_ref, fact_id)
        if literal is not None:
            locator["value_literal"] = literal
        if transformation_qname:
            locator["transformation_qname"] = transformation_qname
        locator["continuation_ids"] = used_continuations
        unit_value = unit_data
        try:
            fact = _fact(
                metric=metric,
                concept=concept_qname,
                value=numeric,
                unit=unit_value["label"],
                start=context_data["start"],
                end=context_data["end"],
                entity=context_data["entity"],
                filing_id=filing_id,
                filed=filed,
                form=form,
                locator=locator,
                disclosure_scope="dimensioned" if context_data["dimensions"] else "filing_entity",
                dimensions=context_data["dimensions"],
                statement_basis="Inline XBRL numeric fact; taxonomy/DTS and presentation role not independently validated",
                source_scale=Decimal(1).scaleb(scale),
            )
        except ValueError as error:
            add_candidate(
                item,
                reason="INLINE_FACT_DOMAIN_VALIDATION_FAILED",
                fact_kind=fact_kind,
                concept_qname=concept_qname,
                metric=metric,
                literal=literal,
                transformation_qname=transformation_qname,
                used_continuations=used_continuations,
            )
            continue
        fact.update(
            unit_definition=unit_value,
            context_id=context_data["context_id"],
            context_ref=context_ref,
            unit_ref=unit_ref,
            decimals=decimals,
            precision=precision,
            value_literal=literal,
            transformation_qname=transformation_qname,
            scale_exponent=scale,
            inline_visible=locator["inline_visible"],
            continuation_ids=used_continuations,
            inline_parse_mode=parse_mode,
        )
        if unit_value["denominator"] or len(unit_value["numerator"]) != 1 or not unit_value["numerator"][0].startswith(
            "{http://www.xbrl.org/2003/iso4217}"
        ):
            fact["currency"] = None
        facts.append(fact)
        entity = context_data["entity"]
        filing_entities[(entity["scheme"], entity["identifier"])] = entity

    candidate_metrics = {row.get("metric") for row in candidates}
    present_metrics = {row["metric"] for row in facts}
    missing = []
    for metric in MISSING_METRICS:
        if metric in present_metrics:
            continue
        missing.append({
            "metric": metric,
            "status": "MISSING",
            "reason": "INLINE_FACT_REQUIRES_REVIEW" if metric in candidate_metrics else "NO_SUPPORTED_INLINE_FACT",
        })
    warnings = [
        "已保留可识别的标准分类数值事实、完整期间和维度、计量单位、原文字面与倍率；不联网加载分类文件，不验证财报展示/计算关系或申报真伪。",
        "本次只读取一份HTML/XHTML原件。多文件合并、分数、叙述文字和未映射概念逐项列为待核；数值转换只覆盖本结果明确列出的规则，其他转换不进入金额计算。",
        "隐藏事实同样保留并标注；披露主体的金额不会自动分摊给品牌、项目、物业或门店。",
    ]
    if parse_mode == "html-recovery":
        warnings.append(
            f"原件不符合严格XHTML格式；按HTML结构读取时记录 {recovery_count} 处结构差异，数值仍保留原件行号，建议回看原文确认。"
        )
    return {
        "format": "inline-xbrl",
        "filing_entities": list(filing_entities.values()),
        "facts": facts,
        "candidates": candidates,
        "missing": missing,
        "warnings": warnings,
        "parse_mode": parse_mode,
        "html_recovery_issue_count": recovery_count,
        "inline_fact_count": len(inline_fact_elements),
    }


def _unit_measure_label(expanded_qname: str) -> str:
    namespace, local = expanded_qname[1:].split("}", 1)
    if namespace == "http://www.xbrl.org/2003/iso4217" and re.fullmatch(r"[A-Z]{3}", local):
        return local
    if namespace == _XBRLI_NAMESPACE and local in {"pure", "shares"}:
        return local
    return expanded_qname


def _parse_numeric_literal(value: str) -> Decimal | None:
    text = value.strip()
    if text in {"—", "–", "-"}:
        return None
    normalized = text.replace(",", "").replace("%", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1]
    return _decimal(normalized, "value_literal")


def _parse_disclosures(text: str, annotations: list) -> dict:
    if not isinstance(annotations, list):
        raise ValueError("annotations must be an array")
    facts = []
    required = {
        "metric", "value", "value_literal", "scale", "unit", "period_start", "period_end",
        "filing_entity", "filing_id", "filed", "form", "disclosure_scope", "statement_basis",
        "start", "end",
    }
    allowed_scales = {
        Decimal(1), Decimal(1000), Decimal(10000), Decimal(1000000),
        Decimal(100000000), Decimal(1000000000),
    }
    for index, row in enumerate(annotations):
        field = f"annotations[{index}]"
        if not isinstance(row, dict) or not required.issubset(row) or set(row) - required - {"page", "dimensions"}:
            raise ValueError(f"{field} is missing required reviewed fields or contains unsupported fields")
        metric = row["metric"]
        if metric not in MANUAL_METRICS or row["disclosure_scope"] not in {
            "filing_entity", "project", "segment", "brand"
        }:
            raise ValueError(f"{field} metric or disclosure scope is unsupported")
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError(f"{field} source locator is outside retained text")
        quote = text[start:end]
        if row.get("page") is not None and (type(row["page"]) is not int or row["page"] < 1):
            raise ValueError(f"{field}.page must be a positive page number")
        entity = _filing_entity(row["filing_entity"], f"{field}.filing_entity")
        literal = _str(row["value_literal"], f"{field}.value_literal")
        if literal not in quote:
            raise ValueError(f"{field} value literal is not present at the supplied locator")
        scale = _decimal(row["scale"], f"{field}.scale")
        if scale not in allowed_scales:
            raise ValueError(f"{field}.scale is not a supported explicit source multiplier")
        if metric == "footfall_description":
            if row["value"] != literal or scale != 1 or row["unit"] != "text":
                raise ValueError(f"{field} qualitative fact does not match its literal")
            fact = _fact(
                metric=metric, concept=f"reviewed-disclosure:{metric}", value=None, unit="text",
                start=row["period_start"], end=row["period_end"], entity=entity,
                filing_id=row["filing_id"], filed=row["filed"], form=row["form"],
                disclosure_scope=row["disclosure_scope"], statement_basis=row["statement_basis"],
                locator={"unit": "parsed-text", "start": start, "end": end,
                         "quote": quote, "page": row.get("page")},
                dimensions=row.get("dimensions", {}), source_scale=scale,
            )
            fact.update(value=literal, status="QUALITATIVE", numeric_value=None)
        else:
            literal_number = _parse_numeric_literal(literal)
            expected = None if literal_number is None else literal_number * scale
            stated = _decimal(row["value"], f"{field}.value", optional=True)
            if stated != expected:
                raise ValueError(f"{field} value does not match literal times explicit scale")
            fact = _fact(
                metric=metric, concept=f"reviewed-disclosure:{metric}", value=stated,
                unit=row["unit"], start=row["period_start"], end=row["period_end"],
                entity=entity, filing_id=row["filing_id"], filed=row["filed"], form=row["form"],
                disclosure_scope=row["disclosure_scope"], statement_basis=row["statement_basis"],
                locator={"unit": "parsed-text", "start": start, "end": end, "quote": quote,
                         "page": row.get("page"), "value_literal": literal},
                dimensions=row.get("dimensions", {}), source_scale=scale,
            )
        facts.append(fact)
    present = {fact["metric"] for fact in facts}
    missing = [
        {"metric": metric, "status": "MISSING", "reason": "NOT_IN_SELECTED_DISCLOSURE_ANNOTATIONS"}
        for metric in ("marketing_expense", "retail_sales", "occupancy", "footfall")
        if metric not in present
    ]
    return {
        "format": "disclosure-extract", "filing_entity": None, "facts": facts,
        "candidates": [], "missing": missing,
        "warnings": [
            "Dates, currency, filing identity, scope, literal, and multiplier are caller-reviewed interpretations.",
            "Source assertions are not independently verified and do not automatically become canonical facts.",
        ],
    }


def _regional_entity(text: str, exchange: str) -> dict | None:
    cover = text[:6000]
    if exchange == "HKEX":
        match = re.search(
            r"(?m)^([^\n]{3,200}(?:LIMITED|Limited))\s*\n\(Incorporated[^\n]*\)\s*\n\(Stock Code:\s*(\d{4,5})\)",
            cover,
        )
        if match:
            return {"name": match[1].strip(), "scheme": "HKEX-StockCode", "identifier": match[2].zfill(5)}
    else:
        name = re.search(r"(?m)^(?:公司名称[：:]\s*)?([^\n]{2,100}股份有限公司)\s*$", cover)
        code = re.search(r"(?:股票|证券)代码\s*[：:]\s*(\d{6})", cover)
        if name and code:
            return {"name": name[1].strip(), "scheme": exchange + "-StockCode", "identifier": code[1]}
    return None


def _regional_periods(header: str, years: list[int]) -> list[tuple[str, str]] | None:
    matches = list(re.finditer(
        r"For the (year|six months|half year) ended (\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(20\d{2})",
        header, re.I,
    ))
    if len(matches) == 1:
        match = matches[0]
        if int(match[4]) != years[0] or match[3].lower() not in _MONTHS:
            return None
        month, day = _MONTHS[match[3].lower()], int(match[2])
        count = 12 if match[1].lower() == "year" else 6
        result = []
        for year in years:
            try:
                finish = date(year, month, day)
                if day != calendar.monthrange(year, month)[1]:
                    return None
                ordinal_month = year * 12 + month
                first_month = ordinal_month - count
                begin = date(first_month // 12, first_month % 12 + 1, 1)
            except ValueError:
                return None
            result.append((begin.isoformat(), finish.isoformat()))
        return result
    if all(re.search(str(year) + r"\s*年度", header) for year in years):
        return [(f"{year}-01-01", f"{year}-12-31") for year in years]
    half_years = re.findall(r"(20\d{2})\s*年\s*1\s*[-至—]\s*6\s*月", header)
    if [int(value) for value in half_years] == years:
        return [(f"{year}-01-01", f"{year}-06-30") for year in years]
    return None


def _parse_regional_statement(text: str, locators: list, *, exchange: str,
                              filing_id: str, filed: str | None, form: str | None) -> dict:
    if exchange not in {"HKEX", "SSE", "SZSE"} or not isinstance(text, str) or not text:
        raise ValueError("regional statement source or exchange is invalid")
    entity = _regional_entity(text, exchange)
    facts, candidates = [], []
    pages = [item for item in locators if item.get("unit") == "pdf_page"]
    if not pages:
        pages = [{"unit": "parsed-text", "index": None, "start": 0, "end": len(text)}]
    statement_pattern = r"Consolidated (?:Statement of Profit or Loss|Income Statement)|合并利润表"
    bounded_pages = []
    for page in pages:
        start, end = page.get("start"), page.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text):
            raise ValueError("regional PDF page locator is invalid")
        headings = list(re.finditer(statement_pattern, text[start:end], re.I))
        if not headings:
            for label, metric in REGIONAL_LABELS.items():
                for match in re.finditer(r"(?im)^\s*" + re.escape(label) + r"\s+([^\n]+)$", text[start:end]):
                    candidates.append({
                        "metric": metric, "label": label, "status": "REVIEW_REQUIRED",
                        "reason": "PRIMARY_STATEMENT_CONTEXT_NOT_VERIFIED",
                        "locator": {"unit": "parsed-text", "start": start + match.start(),
                                    "end": start + match.end(), "page": page.get("index"),
                                    "quote_sha256": hashlib.sha256(match[0].encode()).hexdigest()},
                    })
        for index, heading in enumerate(headings):
            bounded_pages.append({
                **page, "start": start + heading.start(),
                "end": start + headings[index + 1].start() if index + 1 < len(headings) else end,
            })

    number_pattern = _NUMBER.pattern[:-2]  # remove the full-string anchor for token matching
    money_markers = dict(_REGIONAL_MONEY)
    for page in bounded_pages:
        start, end = page["start"], page["end"]
        body = text[start:end]
        statement = re.search(statement_pattern, body, re.I)
        if not statement:
            continue
        rows = []
        for label, metric in REGIONAL_LABELS.items():
            for match in re.finditer(r"(?im)^\s*" + re.escape(label) + r"\s+([^\n]+)$", body):
                if match.start() > statement.end():
                    rows.append((label, metric, match))
        rows.sort(key=lambda row: row[2].start())
        if not rows:
            continue
        header = body[statement.end():min(match.start() for _, _, match in rows)]
        year_lines = re.findall(r"(?m)^\s*(20\d{2})\s*(?:年度|年)?\s+(20\d{2})\s*(?:年度|年)?\s*$", header)
        years = list(map(int, year_lines[0])) if len(year_lines) == 1 else []
        periods = _regional_periods(header, years) if len(years) == 2 and years[0] > years[1] else None
        money = {
            value for marker, value in money_markers.items()
            for line in header.splitlines()
            if re.fullmatch(r"(?:Note\s+|附注\s+)?" + re.escape(marker) + r"\s+" + re.escape(marker), line.strip(), re.I)
        }
        if re.search(r"币种\s*[：:]?\s*人民币", header):
            scales = re.findall(r"单位\s*[：:]?\s*(亿元|万元|千元|元)", header)
            if len(set(scales)) == 1:
                money.add(("CNY", {"元": 1, "千元": 1000, "万元": 10000, "亿元": 100000000}[scales[0]]))
        boundary_pattern = (
            r"(?im)^[^\S\n]*(?:(?:Notes?\s+(?:to|on)\b|Notes?\s*$|Parent\s+(?:company|entity)\b|"
            r"Company\s+(?:only|statement|income)\b|Segment\b|"
            r"(?:Consolidated\s+)?Statement\s+of\s+(?!Profit\s+or\s+Loss)|"
            r"财务报表附注|財務報表附註|母公司|分部|单位\s*[：:]|币种\s*[：:]|"
            r"For\s+the\s+(?:year|six\s+months|half\s+year)\s+ended\b|"
            r"20\d{2}(?:\s*(?:年度|年))?\s+20\d{2}\b|"
            r"(?:Note\s+|附注\s+)?(?:HK\$|RMB|USD|HKD|CNY|GBP|EUR|KRW)\b|"
            r"(?:Note\s+)?HK\$|(?-i:[A-Z]{3})\s+(?:thousand|million|billion)\b)[^\n]*)"
        )
        boundary = re.search(boundary_pattern, body[rows[0][2].end():])
        boundary_start = rows[0][2].end() + boundary.start() if boundary else None
        first_numeric_row = len(header)
        for line_match in re.finditer(r"(?m)^.*$", header):
            tokens = line_match[0].split()
            if len(tokens) >= 3 and all(re.fullmatch(number_pattern, token) for token in tokens[-2:]):
                first_numeric_row = line_match.start()
                break
        header_boundary_start = None
        for context in re.finditer(boundary_pattern, header):
            line = context[0].strip()
            known_metadata = (
                re.fullmatch(r"For the (year|six months|half year) ended \d{1,2}(?:st|nd|rd|th)?\s+\w+\s+20\d{2}", line, re.I)
                or re.fullmatch(r"(20\d{2})\s*(?:年度|年)?\s+(20\d{2})\s*(?:年度|年)?", line)
                or any(re.fullmatch(r"(?:Note\s+|附注\s+)?" + re.escape(marker) + r"\s+" + re.escape(marker), line, re.I) for marker in money_markers)
                or re.fullmatch(r"(?:单位\s*[：:]?\s*(?:亿元|万元|千元|元)(?:\s*币种\s*[：:]?\s*人民币)?|币种\s*[：:]?\s*人民币(?:\s*单位\s*[：:]?\s*(?:亿元|万元|千元|元))?)", line)
            )
            if not known_metadata or context.start() >= first_numeric_row:
                header_boundary_start = statement.end() + context.start()
                break
        for label, metric, match in rows:
            locator = {
                "unit": "parsed-text", "start": start + match.start(), "end": start + match.end(),
                "page": page.get("index"), "quote_sha256": hashlib.sha256(match[0].encode()).hexdigest(),
                "header_start": start + statement.start(),
                "header_end": start + min(item.start() for _, _, item in rows),
            }
            reason = (
                "FILING_ENTITY_UNRESOLVED" if not entity
                else "PERIOD_HEADER_AMBIGUOUS" if not periods
                else "CURRENCY_SCALE_AMBIGUOUS" if len(money) != 1
                else None
            )
            changed_context = header_boundary_start if header_boundary_start is not None else boundary_start
            if changed_context is not None and match.start() >= changed_context:
                reason = reason or "TABLE_CONTEXT_CHANGED_REVIEW_REQUIRED"
                locator["context_boundary_start"] = start + changed_context
            tokens = match[1].split()
            if len(tokens) == 3 and re.search(r"\bNote\b|附注", header) and re.fullmatch(r"\d{1,2}|[一二三四五六七八九十]+", tokens[0]):
                tokens = tokens[1:]
            if len(tokens) != 2 or any(not re.fullmatch(number_pattern, token) for token in tokens):
                reason = reason or "UNSUPPORTED_OR_AMBIGUOUS_VALUE_COLUMNS"
            if reason:
                candidates.append({"metric": metric, "label": label, "locator": locator,
                                   "status": "REVIEW_REQUIRED", "reason": reason})
                continue
            currency, scale = next(iter(money))
            for column, (literal, (period_start, period_end)) in enumerate(zip(tokens, periods), 1):
                value = _parse_numeric_literal(literal)
                if value is not None:
                    value *= Decimal(scale)
                fact = _fact(
                    metric=metric, concept=f"statement-label:{label}", value=value, unit=currency,
                    start=period_start, end=period_end, entity=entity, filing_id=filing_id,
                    filed=filed, form=form,
                    locator={**locator, "column": column, "value_literal": literal},
                    source_scale=scale,
                    statement_basis="consolidated primary income statement; explicit label and period header",
                )
                fact.update(
                    extraction_method="deterministic-explicit-statement/v1",
                    extraction_review="NOT_REVIEWED", source_label=label,
                    allocation="FILING_ENTITY_ONLY",
                )
                facts.append(fact)
    present = {fact["metric"] for fact in facts}
    return {
        "format": "regional-statement", "filing_entity": entity, "facts": facts,
        "candidates": candidates,
        "missing": [
            {"metric": metric, "status": "MISSING", "reason": "NOT_EXTRACTED_FROM_SUPPORTED_EXPLICIT_STATEMENT"}
            for metric in ("marketing_expense", "advertising_expense", "revenue", "retail_sales", "occupancy", "footfall")
            if metric not in present
        ],
        "warnings": [
            "Only explicit consolidated income-statement layouts are parsed. Scans, notes, project tables, and ambiguous headers remain candidates.",
            "Identifier and name are source assertions, not independent legal identity verification; no group-to-brand allocation is made.",
        ],
    }


def _parse_opendart_json(raw: bytes, *, fs_div: str = "CFS") -> dict:
    if fs_div not in {"CFS", "OFS"}:
        raise ValueError("OpenDART financial statement basis must be CFS or OFS")
    data = _json_load(raw, "OpenDART response")
    if not isinstance(data, dict) or not re.fullmatch(r"\d{3}", str(data.get("status", ""))):
        raise ValueError("OpenDART response status is missing or invalid")
    provider_status = data["status"]
    result = {
        "format": "opendart-json", "filing_entity": None, "facts": [], "candidates": [],
        "missing": [], "provider_status": provider_status, "selected_fs_div": fs_div,
        "excluded_basis_rows": 0,
        "warnings": ["Only explicit current-term dates and amounts are used; annual and accumulated amounts are not split into quarters."],
    }
    if provider_status != "000":
        states = {
            "010": "INVALID_CREDENTIAL", "011": "DISABLED_CREDENTIAL", "012": "IP_NOT_ALLOWED",
            "013": "NO_DATA", "014": "NO_FILE", "020": "RATE_LIMITED", "800": "MAINTENANCE",
            "901": "EXPIRED_CREDENTIAL",
        }
        result["missing"].append({"status": "MISSING", "reason": states.get(provider_status, "PROVIDER_ERROR")})
        return result
    entries = data.get("list")
    if not isinstance(entries, list):
        raise ValueError("OpenDART list must be an array")
    for index, row in enumerate(entries):
        if not isinstance(row, dict):
            raise ValueError("OpenDART account rows must be objects")
        label = row.get("account_nm", "")
        metric = DART_LABELS.get(label)
        if not metric or row.get("sj_div") not in {"IS", "CIS"}:
            continue
        locator = {"unit": "json-pointer", "pointer": f"/list/{index}"}
        if (
            not _DART_CORP.fullmatch(str(row.get("corp_code", "")))
            or not _DART_RECEIPT.fullmatch(str(row.get("rcept_no", "")))
            or row.get("fs_div") not in {"CFS", "OFS"}
        ):
            raise ValueError("OpenDART account identity is invalid")
        if row["fs_div"] != fs_div:
            result["excluded_basis_rows"] += 1
            continue
        dates = re.findall(r"(20\d{2})[.\-/](\d{2})[.\-/](\d{2})", str(row.get("thstrm_dt", "")))
        reason = None
        if len(dates) != 2:
            reason = "EXPLICIT_FISCAL_PERIOD_REQUIRED"
            period_start = period_end = None
        else:
            period_start, period_end = ("-".join(value) for value in dates)
            _date(period_start, "OpenDART period start")
            _date(period_end, "OpenDART period end")
        if row.get("thstrm_add_amount") is not None:
            result["candidates"].append({
                "metric": metric, "label": label,
                "locator": {**locator, "pointer": locator["pointer"] + "/thstrm_add_amount"},
                "status": "REVIEW_REQUIRED", "reason": "ACCUMULATED_PERIOD_NOT_EXPLICIT",
                "value_literal": row["thstrm_add_amount"], "amount_basis": "accumulated",
                "period_start": None, "period_end": None,
            })
        literal = row.get("thstrm_amount")
        currency = row.get("currency")
        if not reason and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            reason = "EXPLICIT_CURRENCY_REQUIRED"
        if not reason and (not isinstance(literal, str) or not _NUMBER.fullmatch(literal.strip())):
            reason = "CURRENT_TERM_AMOUNT_REQUIRED"
        if reason:
            result["candidates"].append({"metric": metric, "label": label, "locator": locator,
                                          "status": "REVIEW_REQUIRED", "reason": reason})
            continue
        amount = _parse_numeric_literal(literal)
        entity = {"name": row.get("corp_name"), "scheme": "OpenDART-CorpCode", "identifier": row["corp_code"]}
        fact = _fact(
            metric=metric, concept=f"opendart-account:{label}", value=amount, unit=currency,
            start=period_start, end=period_end, entity=entity, filing_id=row["rcept_no"],
            filed=None, form=row.get("reprt_code"),
            locator={**locator, "pointer": locator["pointer"] + "/thstrm_amount"},
            statement_basis=("consolidated" if row["fs_div"] == "CFS" else "separate") + " income statement; current term amount",
            dimensions={"financial_statement_basis": row["fs_div"]},
        )
        fact.update(extraction_method="opendart-explicit-date-amount/v1", source_label=label,
                    extraction_review="NOT_REVIEWED")
        result["facts"].append(fact)
    if not result["facts"]:
        result["missing"].append({"status": "MISSING", "reason": "NO_SUPPORTED_EXPLICIT_DATED_ACCOUNT"})
    return result


def _macro_provider(source_url: Any) -> tuple[str, str]:
    source_url = _str(source_url, "source_url")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source_url must be a canonical HTTPS government release URL")
    providers = {
        "www.stats.gov.cn": (
            "NBS",
            r"/english/PressRelease/\d{6}/t\d{8}_\d+\.html",
            "National Bureau of Statistics of China",
        ),
        "zwgk.mct.gov.cn": (
            "MCT",
            r"/zfxxgkml/tjxx/\d{6}/t\d{8}_\d+\.html",
            "Ministry of Culture and Tourism of China",
        ),
        "www.mct.gov.cn": (
            "MCT",
            r"/whzx/whyw/\d{6}/t\d{8}_\d+\.htm",
            "Ministry of Culture and Tourism of China",
        ),
        "sw.beijing.gov.cn": (
            "BEIJING_COMMERCE",
            r"/(?:phone/)?zwxx/swxx/\d{6}/t\d{8}_\d+\.html",
            "Beijing Municipal Commerce Bureau",
        ),
    }
    descriptor = providers.get(parsed.hostname or "")
    if descriptor is None or not re.fullmatch(descriptor[1], parsed.path):
        raise ValueError("source_url is outside the supported official release paths")
    return descriptor[0], descriptor[2]


def _macro_publication(text: str) -> tuple[str | None, str]:
    match = re.search(
        r"发布时间[：:]\s*(20\d{2}(?:-\d{2}-\d{2}|年\d{2}月\d{2}日)(?: \d{2}:\d{2})?)",
        text,
    )
    if match is None:
        return None, "unknown"
    literal = match.group(1)
    normalized = literal.replace("年", "-").replace("月", "-").replace("日", "")
    try:
        datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("macro publication date is malformed") from error
    return literal, "minute" if len(normalized) > 10 else "day"


def _macro_fact(
    text: str,
    match: re.Match,
    header: re.Match,
    *,
    metric: str,
    value: Decimal,
    publisher: str,
    region: str,
    definition: str,
    period_start: str | None = None,
    period_end: str | None = None,
    period_kind: str = "holiday",
    period_label: str | None = None,
    duration_days: int | None = None,
    coverage: dict | None = None,
    source_url: str,
    publication: str | None,
    publication_precision: str,
) -> dict:
    if metric not in MACRO_UNITS or abs(value) > Decimal("1e24"):
        raise ValueError("macro release contains an unsupported or out-of-range fact")
    if duration_days is not None and not 1 <= duration_days <= 31:
        raise ValueError("macro holiday duration must be between one and 31 days")
    if coverage and "district_count" in coverage and coverage["district_count"] <= 0:
        raise ValueError("macro coverage district count must be positive")
    if period_start is not None:
        _date(period_start, "macro period_start")
    if period_end is not None:
        _date(period_end, "macro period_end")
    if period_start and period_end and period_start > period_end:
        raise ValueError("macro period starts after it ends")
    fact = {
        "schema": MACRO_SCHEMA,
        "metric": metric,
        "value": format(value, "f"),
        "unit": MACRO_UNITS[metric],
        "region": region,
        "region_basis": "publisher-stated statistical coverage",
        "period_start": period_start,
        "period_end": period_end,
        "period_kind": period_kind,
        "period_label": period_label,
        "duration_days": duration_days,
        "period_date_status": (
            "EXPLICIT_CALENDAR_QUARTER"
            if period_kind == "quarter"
            else "EXACT_START_END_NOT_STATED"
        ),
        "definition": definition,
        "coverage": coverage or {},
        "publisher": publisher,
        "status": "DISCLOSED",
        "source_url_claim": source_url,
        "published_literal": publication,
        "publication_time_precision": publication_precision,
        "publication_timezone": "UNKNOWN",
        "locator": {
            "unit": "character-offset",
            "start": match.start(),
            "end": match.end(),
            "quote_sha256": hashlib.sha256(match.group(0).encode("utf-8")).hexdigest(),
        },
        "header_locator": {
            "unit": "character-offset",
            "start": header.start(),
            "end": header.end(),
        },
        "revision_policy": "retained source and workspace capture vintage; upstream revision identity unknown",
        "acquisition_authenticity": "NOT_INDEPENDENTLY_VERIFIED",
    }
    fact["observation_id"] = "MACRO-" + hashlib.sha256(
        json.dumps(fact, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    return fact


def _parse_macro_release(raw: str, source_url: Any) -> dict:
    provider, publisher = _macro_provider(source_url)
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("macro release text must contain at most 2 MiB of saved text")
    publication, publication_precision = _macro_publication(raw)
    facts: list[dict] = []
    missing: list[dict] = []
    if provider == "NBS":
        month_names = {name: index for index, name in enumerate(calendar.month_name) if name}
        month_pattern = "(?:" + "|".join(month_names) + ")"
        number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
        titles = list(re.finditer(
            r"(?:Total Retail Sales of Consumer Goods (?:in|from January to)|Consumer Price Index (?:for|in)) ("
            + month_pattern + r") (20\d{2})",
            raw,
        ))
        signatures = {
            (match.group(1), match.group(2), "retail" if match.group(0).startswith("Total") else "cpi")
            for match in titles
        }
        if len(signatures) != 1:
            return {
                "format": "nbs-national-macro-release",
                "provider": provider,
                "facts": [],
                "missing": [{"metric": None, "status": "MISSING", "reason": "RELEASE_TITLE_PERIOD_AMBIGUOUS"}],
                "candidates": [],
                "warnings": ["Only exact NBS release-title and headline grammars are parsed."],
            }
        month_name, year_text, kind = next(iter(signatures))
        year, month = int(year_text), month_names[month_name]
        header = titles[0]
        nbs_publication = re.search(
            r"\b(20\d{2}-\d{2}-\d{2} \d{2}:\d{2})\b",
            raw[header.end():header.end() + 300],
        )
        if nbs_publication:
            publication, publication_precision = nbs_publication.group(1), "minute"
            try:
                if datetime.fromisoformat(publication).date() < date(year, month, calendar.monthrange(year, month)[1]):
                    raise ValueError("macro publication date precedes its reported period end")
            except ValueError as error:
                raise ValueError("macro NBS publication time is inconsistent with its period") from error
        facts_before = len(facts)

        def append_nbs(match: re.Match, metric: str, value: Decimal, first_month: int, period_kind: str, definition: str):
            start = date(year, first_month, 1).isoformat()
            end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
            facts.append(_macro_fact(
                raw, match, header, metric=metric, value=value, publisher=publisher,
                region="CN-national", definition=definition, period_start=start,
                period_end=end, period_kind=period_kind,
                source_url=source_url, publication=publication,
                publication_precision=publication_precision,
            ))

        if kind == "retail":
            for match in re.finditer(
                r"In " + month_name + r", the total retail sales of consumer goods reached (" + number + r") billion yuan",
                raw,
            ):
                append_nbs(match, "retail_sales", Decimal(match.group(1).replace(",", "")) * Decimal(10**9),
                           month, "month", "national-total-nominal-retail-sales")
            for match in re.finditer(
                r"From January to " + month_name + r", the total retail sales of consumer goods reached (" + number + r") billion yuan",
                raw,
            ):
                append_nbs(match, "retail_sales", Decimal(match.group(1).replace(",", "")) * Decimal(10**9),
                           1, "cumulative", "national-total-nominal-retail-sales")
        else:
            for match in re.finditer(
                r"In " + month_name + " " + str(year)
                + r", (?:the national consumer price index|China[’']s Consumer Price Index) \(CPI\) (increased|decreased) by ("
                + number + r")(?: percent|%) year on year\b",
                raw,
            ):
                value = Decimal(match.group(2).replace(",", ""))
                if match.group(1) == "decreased":
                    value = -value
                append_nbs(match, "cpi_yoy_percent", value, month, "month",
                           "national-all-items-cpi-year-on-year-change")
        keys = [(item["metric"], item["period_start"], item["period_end"], item["period_kind"]) for item in facts]
        if len(keys) != len(set(keys)):
            facts.clear()
            missing.append({"metric": None, "status": "MISSING", "reason": "RELEASE_OBSERVATIONS_AMBIGUOUS"})
        elif len(facts) == facts_before:
            missing.append({"metric": "retail_sales" if kind == "retail" else "cpi_yoy_percent",
                            "status": "MISSING", "reason": "SUPPORTED_NATIONAL_HEADLINE_NOT_FOUND"})
    elif provider == "MCT":
        headings = list(re.finditer(r"(20\d{2})年([一二三四])季度国内出游数据情况", raw))
        if headings and "根据国内出游抽样调查统计结果" in raw:
            if len({match.group(0) for match in headings}) != 1:
                missing.append({"metric": None, "status": "MISSING", "reason": "RELEASE_TITLE_PERIOD_AMBIGUOUS"})
            else:
                header = headings[0]
                year = int(header.group(1))
                quarter = "一二三四".index(header.group(2)) + 1
                first_month = quarter * 3 - 2
                last_month = first_month + 2
                start = date(year, first_month, 1).isoformat()
                end = date(year, last_month, calendar.monthrange(year, last_month)[1]).isoformat()
                prefix = str(year) + "年" + header.group(2) + "季度，"
                sentence_tail = r"[^。；;\n]{0,160}?"
                for match in re.finditer(prefix + sentence_tail + r"国内出游人次(\d+(?:\.\d+)?)亿", raw):
                    facts.append(_macro_fact(
                        raw, match, header, metric="domestic_tourism_trips",
                        value=Decimal(match.group(1)) * Decimal(10**8), publisher=publisher,
                        region="CN-national", definition="domestic-tourism-sample-survey-person-trips-not-unique-persons",
                        period_start=start, period_end=end, period_kind="quarter", period_label=f"{year}-Q{quarter}",
                        coverage={"population": "national domestic tourism", "method": "publisher-reported sample survey"},
                        source_url=source_url, publication=publication, publication_precision=publication_precision,
                    ))
                for match in re.finditer(prefix + sentence_tail + r"国内居民出游总花费(\d+(?:\.\d+)?)万亿元", raw):
                    facts.append(_macro_fact(
                        raw, match, header, metric="domestic_tourism_spend",
                        value=Decimal(match.group(1)) * Decimal(10**12), publisher=publisher,
                        region="CN-national", definition="domestic-tourism-sample-survey-total-spend",
                        period_start=start, period_end=end, period_kind="quarter", period_label=f"{year}-Q{quarter}",
                        coverage={"population": "national domestic tourism", "method": "publisher-reported sample survey"},
                        source_url=source_url, publication=publication, publication_precision=publication_precision,
                    ))
        else:
            missing.append({"metric": None, "status": "MISSING", "reason": "SUPPORTED_MCT_QUARTER_NOT_FOUND_OR_SURVEY_BASIS_UNAVAILABLE"})
    else:
        header = re.search(r"清明假期本市消费市场活跃有序", raw)
        periods = list(re.finditer(r"(20\d{2})年清明假期", raw))
        duration = re.search(r"假期(三|3)天", raw)
        if header and len({match.group(0) for match in periods}) == 1 and duration:
            for match in re.finditer(r"全市(\d{1,3})个重点商圈客流量(\d+(?:\.\d+)?)万人次", raw):
                facts.append(_macro_fact(
                    raw, match, header, metric="monitored_district_footfall",
                    value=Decimal(match.group(2)) * Decimal(10**4), publisher=publisher,
                    region="CN-Beijing", definition="monitored-commercial-district-visitor-events-not-unique-people",
                    period_label=periods[0].group(1) + "-清明", duration_days=3,
                    coverage={"population": "monitored key commercial districts only",
                              "district_count": int(match.group(1)), "scope_not_all_city": True,
                              "method": "publisher aggregate; deduplication method not disclosed"},
                    source_url=source_url, publication=publication, publication_precision=publication_precision,
                ))
        else:
            missing.append({"metric": "monitored_district_footfall", "status": "MISSING", "reason": "SUPPORTED_BEIJING_HOLIDAY_RELEASE_NOT_FOUND"})
    keys = [
        (row["metric"], row["period_start"], row["period_end"], row["period_kind"], row["period_label"])
        for row in facts
    ]
    if len(keys) != len(set(keys)):
        return {
            "format": "official-macro-release",
            "provider": provider,
            "facts": [],
            "missing": [{"metric": None, "status": "MISSING", "reason": "RELEASE_OBSERVATIONS_AMBIGUOUS"}],
            "candidates": [],
            "warnings": ["A repeated period/metric with conflicting release statements was not selected."],
        }
    return {
        "format": "official-macro-release",
        "provider": provider,
        "facts": facts,
        "missing": missing or ([] if facts else [{"metric": None, "status": "MISSING", "reason": "SUPPORTED_RELEASE_FACT_NOT_FOUND"}]),
        "candidates": [],
        "warnings": [
            "The parser accepts only the finite NBS, MCT and Beijing Commerce release grammars named in this result.",
            "A supported official-looking URL and matching saved text are user-provided claims; acquisition and publisher authenticity are not independently verified.",
            "Unreported periods, cumulative totals and holiday totals are not converted into monthly/quarterly observations.",
            "Trips and visits count events, not unique people; coverage retains the publisher's stated survey or monitored-district boundary.",
        ],
    }


def resolve_macro_controls(
    facts: list[dict], *, metric: str, region: str, periods: list[str],
    vintage_basis: str, as_of: str, is_cancelled=None,
) -> dict:
    """Align parsed official-release facts to requested quarters without filling gaps."""
    if not isinstance(facts, list):
        raise ValueError("macro facts must be a list from a saved financial source result")
    if metric not in MACRO_UNITS or not isinstance(region, str) or not region.strip():
        raise ValueError("macro metric and region must be explicit")
    if vintage_basis not in {"filed", "workspace_capture"}:
        raise ValueError("macro vintage_basis must be filed or workspace_capture")
    quarter_pattern = re.compile(r"20\d{2}-Q[1-4]\Z")
    if not periods or any(not isinstance(item, str) or not quarter_pattern.fullmatch(item) for item in periods):
        raise ValueError("MMM macro alignment requires explicit quarter labels")
    if len(set(periods)) != len(periods):
        raise ValueError("MMM periods must be unique")
    if vintage_basis == "filed":
        cutoff_date = _date(as_of, "as_of")
        cutoff_instant = None
    else:
        cutoff_date = None
        cutoff_instant = _instant(as_of, "as_of")

    selected: dict[tuple[str, str, str, str | None], dict] = {}
    for index, fact in enumerate(facts):
        _check_cancelled(is_cancelled)
        if not isinstance(fact, dict) or fact.get("schema") != MACRO_SCHEMA:
            raise ValueError(f"macro facts[{index}] is not a parsed macro source fact")
        if fact.get("metric") != metric or fact.get("region") != region or fact.get("unit") != MACRO_UNITS[metric]:
            continue
        if fact.get("status") != "DISCLOSED" or not isinstance(fact.get("source_ref"), dict):
            continue
        if vintage_basis == "filed":
            literal = fact.get("published_literal")
            if not isinstance(literal, str):
                continue
            normalized = literal.replace("年", "-").replace("月", "-").replace("日", "")
            try:
                published_date = datetime.fromisoformat(normalized).date()
            except ValueError:
                continue
            if published_date > cutoff_date:
                continue
            version = published_date.isoformat()
        else:
            try:
                captured = _instant(fact.get("captured_at"), "macro fact captured_at")
            except ValueError:
                continue
            if captured > cutoff_instant:
                continue
            version = captured.isoformat()
        start = fact.get("period_start")
        end = fact.get("period_end")
        if start is None or end is None:
            continue
        _date(start, f"macro facts[{index}].period_start")
        _date(end, f"macro facts[{index}].period_end")
        # A January monthly total and January-to-date cumulative total share
        # the same calendar bounds but are different observations. Keep their
        # period semantics separate so source-artifact order cannot hide the
        # monthly total used for a quarterly retail-sales control.
        key = (start, end, str(fact.get("period_kind") or ""), fact.get("period_label"))
        previous = selected.get(key)
        if previous is None or version > previous["_selected_version"]:
            selected[key] = {**fact, "_selected_version": version}
        elif version == previous["_selected_version"] and (
            fact.get("value"), fact.get("definition")
        ) != (previous.get("value"), previous.get("definition")):
            selected[key] = {"_conflict": True}

    rows = []
    for quarter in periods:
        _check_cancelled(is_cancelled)
        year, number = int(quarter[:4]), int(quarter[-1])
        first_month = number * 3 - 2
        month_numbers = {first_month, first_month + 1, first_month + 2}
        start = date(year, first_month, 1).isoformat()
        end = date(year, first_month + 2, calendar.monthrange(year, first_month + 2)[1]).isoformat()
        overlapping = [
            row for (fact_start, fact_end, _period_kind, _period_label), row in selected.items()
            if fact_start <= end and fact_end >= start
        ]
        if any(row.get("_conflict") for row in overlapping):
            rows.append({"period": quarter, "value": None, "state": "MISSING", "reason": "CONFLICTING_AS_OF_VINTAGES", "source_refs": []})
            continue
        full_quarters = [row for row in overlapping if row.get("period_kind") == "quarter" and row.get("period_start") == start and row.get("period_end") == end]
        months = [
            row for row in overlapping
            if row.get("period_kind") == "month"
            and row.get("period_start")[:7] == row.get("period_end")[:7]
            and int(row["period_start"][5:7]) in month_numbers
        ]
        chosen: list[dict] = []
        aggregation = None
        if metric in {"domestic_tourism_trips", "domestic_tourism_spend"} and len(full_quarters) == 1:
            chosen, aggregation = full_quarters, "explicit-reported-quarter-total"
        elif metric in {"retail_sales", "cpi_yoy_percent"} and len(months) == 3 and {int(row["period_start"][5:7]) for row in months} == month_numbers:
            chosen = months
            aggregation = "sum-complete-monthly-totals" if metric == "retail_sales" else "arithmetic-mean-monthly-yoy-percent"
        if not chosen:
            rows.append({"period": quarter, "value": None, "state": "MISSING", "reason": "INCOMPLETE_QUARTER_OR_ASOF_VINTAGE", "source_refs": []})
            continue
        values = [Decimal(_mmm_decimal_text(row["value"], "macro fact value")) for row in chosen]
        value = sum(values, Decimal(0))
        if aggregation == "arithmetic-mean-monthly-yoy-percent":
            value /= Decimal(3)
        rows.append({
            "period": quarter,
            "value": format(value, "f"),
            "state": "AVAILABLE",
            "aggregation": aggregation,
            "source_refs": [
                {
                    "source_url": row.get("source_url_claim"),
                    "published_literal": row.get("published_literal"),
                    "locator": row.get("locator"),
                    "source_ref": row["source_ref"],
                }
                for row in chosen
            ],
        })
    return {
        "metric": metric,
        "region": region,
        "unit": MACRO_UNITS[metric],
        "periods": rows,
        "vintage_basis": vintage_basis,
        "as_of": as_of,
        "record_status": "DERIVED_FROM_SAVED_SOURCE_ASSERTIONS",
    }


def _source_provenance(value: Any) -> dict:
    source = _json_copy(_obj(value or {}, "source"), "source")
    expected_hash = source.get("material_sha256")
    if expected_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected_hash)):
        raise ValueError("source.material_sha256 must be a SHA-256 hex digest")
    if source.get("captured_at") is not None:
        _instant(source["captured_at"], "source.captured_at")
    return source


def _with_source(parsed: dict, raw: bytes | str, source: dict) -> dict:
    content = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    content_hash = hashlib.sha256(content).hexdigest()
    expected_hash = source.get("material_sha256")
    if expected_hash is not None and expected_hash.lower() != content_hash:
        raise ValueError("retained Material bytes do not match the supplied source hash")
    provenance = {
        **source,
        "content_sha256": content_hash,
        "retained_input_bytes_verified": True,
        "acquisition_authenticity": "NOT_INDEPENDENTLY_VERIFIED",
        "canonical_fact_promotion": False,
    }
    for fact in parsed["facts"]:
        fact["captured_at"] = source.get("captured_at")
        fact["source_ref"] = {
            "material_id": source.get("material_id"),
            "material_version": source.get("material_version"),
            "material_sha256": expected_hash or content_hash,
            "locator": fact["locator"],
            **({"filename": source["filename"]} if isinstance(source.get("filename"), str) else {}),
            **({"title": source["title"]} if isinstance(source.get("title"), str) else {}),
        }
    parsed["source"] = provenance
    parsed["status"] = "AVAILABLE" if parsed["facts"] else "MISSING"
    return parsed


def _financial_source_analysis(inputs: dict, *, is_cancelled=None) -> dict:
    _check_cancelled(is_cancelled)
    allowed = {
        "source_format", "raw_source", "source", "filing_id", "filed", "form",
        "annotations", "locators", "exchange", "fs_div", "source_url",
    }
    if set(inputs) - allowed:
        raise ValueError("financial_source_analysis contains unsupported fields")
    source_format = inputs.get("source_format")
    if source_format not in {
        "sec-companyfacts", "sec-companyconcept", "xbrl-instance", "inline-xbrl", "disclosure-extract",
        "regional-statement", "opendart-json", "macro-release",
    }:
        raise ValueError("source_format is unsupported")
    raw_source = inputs.get("raw_source")
    source = _source_provenance(inputs.get("source", {}))
    filed = _iso_date(inputs.get("filed"), "filed", optional=True)
    form = _str(inputs.get("form"), "form", optional=True)
    if source_format in {"sec-companyfacts", "sec-companyconcept"}:
        raw = _source_bytes(raw_source, "raw_source")
        parsed = _parse_sec_json(raw)
        if parsed["format"] != source_format:
            raise ValueError("SEC source format does not match the selected parser")
    elif source_format == "xbrl-instance":
        raw = _source_bytes(raw_source, "raw_source")
        filing_id = _str(inputs.get("filing_id"), "filing_id")
        parsed = _parse_xbrl_instance(raw, filing_id=filing_id, filed=filed, form=form)
    elif source_format == "inline-xbrl":
        raw = _source_bytes(raw_source, "raw_source")
        filing_id = _str(inputs.get("filing_id"), "filing_id")
        parsed = _parse_inline_xbrl(
            raw, filing_id=filing_id, filed=filed, form=form, is_cancelled=is_cancelled
        )
    elif source_format == "opendart-json":
        raw = _source_bytes(raw_source, "raw_source")
        parsed = _parse_opendart_json(raw, fs_div=inputs.get("fs_div", "CFS"))
    elif source_format == "macro-release":
        raw = _source_text(raw_source, "raw_source")
        parsed = _parse_macro_release(raw, inputs.get("source_url"))
    elif source_format == "disclosure-extract":
        raw = _source_text(raw_source, "raw_source")
        parsed = _parse_disclosures(raw, inputs.get("annotations", []))
    else:
        raw = _source_text(raw_source, "raw_source")
        locators = inputs.get("locators", [])
        if not isinstance(locators, list):
            raise ValueError("locators must be an array")
        exchange = inputs.get("exchange")
        filing_id = _str(inputs.get("filing_id"), "filing_id")
        parsed = _parse_regional_statement(
            raw, locators, exchange=exchange, filing_id=filing_id, filed=filed, form=form
        )
    _check_cancelled(is_cancelled)
    parsed = _with_source(parsed, raw, source)
    macro_release = source_format == "macro-release"
    inline_xbrl = parsed["format"] == "inline-xbrl"
    fact_rows = [] if macro_release else (
        _inline_fact_table_rows(parsed["facts"]) if inline_xbrl else _fact_table_rows(parsed["facts"])
    )
    macro_rows = [[
        row["metric"], row["region"], row.get("period_label") or f"{row.get('period_start')}–{row.get('period_end')}",
        row["period_kind"], row["value"], row["unit"], row["definition"],
        row.get("published_literal"), row.get("publication_time_precision"),
        row.get("coverage"), f"characters {row['locator']['start']}–{row['locator']['end']}",
        row.get("source_url_claim"),
    ] for row in parsed["facts"]] if macro_release else []
    candidate_rows = (
        _inline_candidate_table_rows(parsed["candidates"])
        if inline_xbrl else _candidate_table_rows(parsed["candidates"])
    )
    source_format_label = "网页格式财报（XBRL标记）" if inline_xbrl else parsed["format"]
    missing_rows = (
        [[_financial_metric_label(row.get("metric")), "缺失", _inline_missing_reason_label(row.get("reason"))]
         for row in parsed["missing"]]
        if inline_xbrl else
        [[row.get("metric"), row.get("status"), row.get("reason")] for row in parsed["missing"]]
    )
    sections = [
        {
            "id": "financial_source.facts",
            "title": "保留定位的财报事实",
            "body": (
                "所选原件为宏观发布材料，不是企业财务报表；宏观统计事实在独立来源表中列出。"
                if macro_release else
                (
                    f"从这份财报网页原件中提取 {len(parsed['facts'])} 条可计算的标准数值事实。"
                    "逐条列出披露主体、申报版本、完整期间、原文字面与换算值、币种/单位、维度、精度、可见状态和原件位置。"
                    if inline_xbrl else
                    f"解析得到 {len(parsed['facts'])} 条来源事实。以下完整列出主体、值与单位、完整期间、申报/取得时间、维度、会计口径和原文位置；每条仍是来源断言。"
                ) + "\n\n" + _md_table(
                    (["指标", "概念QName", "披露主体", "申报编号", "披露时间（Filed）", "工作区取得", "完整期间", "原文字面", "转换值", "单位", "币种", "维度", "精度", "倍率", "状态", "数值转换", "可见性", "原件位置", "材料"] if inline_xbrl else
                     ["指标", "披露主体", "申报编号", "Filed", "工作区取得", "期间", "字面值", "单位", "币种", "维度", "披露范围", "会计/报表基础", "倍率", "状态", "原文位置", "材料"]),
                    fact_rows,
                )),
            "data": {
                "facts": [] if macro_release else parsed["facts"],
                "filing_entities": parsed.get("filing_entities", [parsed.get("filing_entity")]),
            },
        },
        {
            "id": "financial_source.candidates",
            "title": "需要复核的原文候选",
            "body": f"有 {len(parsed['candidates'])} 条原文事实未自动纳入金额序列，逐项列出待核原因、原文字面、单位与倍率、可见状态和原件定位。\n\n" +
            _md_table(
                (["指标", "概念QName", "状态", "未采用原因", "原文字面", "转换规则", "单位引用", "符号", "倍率指数", "精度声明", "可见性", "原件位置"] if inline_xbrl else
                 ["指标", "原文标签", "状态", "未采用原因", "原文字面", "期间基础", "原文位置"]),
                candidate_rows,
            ),
            "data": {"candidates": parsed["candidates"]},
        },
        {
            "id": "financial_source.missing",
            "title": "缺失指标与解析范围",
            "body": f"来源格式：{source_format_label}；取得文件SHA-256：{parsed['source']['content_sha256']}。未披露或未提取状态不证明完整文件没有其他信息。\n\n" +
            _md_table(["指标", "状态", "原因"], missing_rows) +
            "\n\n解析说明：\n\n" + "\n".join(f"- {warning}" for warning in parsed["warnings"]),
            "data": {
                "format": parsed["format"], "missing": parsed["missing"],
                "warnings": parsed["warnings"], "source": parsed["source"],
                "provider_status": parsed.get("provider_status"),
                "selected_fs_div": parsed.get("selected_fs_div"),
                "excluded_basis_rows": parsed.get("excluded_basis_rows"),
                "parse_mode": parsed.get("parse_mode"),
                "html_recovery_issue_count": parsed.get("html_recovery_issue_count"),
                "inline_fact_count": parsed.get("inline_fact_count"),
            },
        },
    ]
    if macro_release:
        sections.insert(1, {
            "id": "financial_source.macro_facts",
            "title": "保留定位的宏观发布事实",
            "body": (
                f"按限定的官方发布文字格式识别 {len(parsed['facts'])} 条宏观事实；列出区域、期间类型、原值、单位、指标定义、发布日期精度、统计覆盖与原文字符位置。"
                "URL 与匹配文本只作为来源声明，发布方身份、网页真实性和独立采集未由本操作核验。\n\n"
                + _md_table(
                    ["指标", "区域", "统计期间", "期间性质", "披露值", "单位", "定义", "发布时间", "时间精度", "发布覆盖", "原文定位", "声明来源网址"],
                    macro_rows,
                )
            ),
            "data": {
                "schema": MACRO_SCHEMA,
                "facts": parsed["facts"],
                "provider": parsed.get("provider"),
                "source_url_claim": inputs.get("source_url"),
                "source": parsed["source"],
            },
        })
    limitations = [
        "仅解析调用方授权读取的Material原件或保留文本；不联网取文件、不读取凭据、不判断原件陈述真伪。",
        "主体是披露法律实体，不能自动分摊到品牌、项目、物业或门店。",
        "万元按明确的10000倍率处理；所有倍率、字面值和原始位置随事实保留，冲突更正不覆盖旧事实。",
    ]
    if inline_xbrl:
        limitations.extend([
            "自动提取只覆盖本次列出的单份网页财报、标准数值事实和已实现转换；不加载外部分类文件，也不验证申报信息、财报展示关系或金额计算关系。",
            "分数、叙述区块、未映射概念、多文档合并和未实现的转换规则都保留逐项待核状态，不进入财务季度序列或MMM。",
        ])
    elif source_format != "macro-release":
        limitations.append(
            "非iXBRL格式只识别各自列明的标准概念和明确表格版式；未支持taxonomy、扫描/OCR歧义或异形表保留为候选/缺失。"
        )
    if source_format == "xbrl-instance":
        limitations.append("XBRL实例解析保完整QName、实体、期间、维度和复合单位；不加载外部DTS或全部分类标准验证。")
    if macro_release:
        limitations.extend([
            "宏观发布时间或工作区取得时点缺失时，该事实不会进入相应时点的季度控制序列。",
            "宏观发布样本只代表其明示全国/监测区域范围，不自动分摊为本品牌、本项目或本门店的经营结果。",
        ])
    return {
        "title": "财报原件事实提取",
        "sections": sections,
        "limitations": limitations + parsed["warnings"],
    }


def _fact_signature(fact: dict) -> tuple:
    entity = fact.get("filing_entity") or {}
    return (
        fact.get("metric"), fact.get("concept"), fact.get("unit"), fact.get("currency"),
        fact.get("period_start"), fact.get("period_end"),
        entity.get("scheme"), entity.get("identifier"),
        json.dumps(fact.get("dimensions", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        fact.get("disclosure_scope"), fact.get("statement_basis"),
    )


def _fact_index(value: Any, *, is_cancelled=None) -> dict[str, dict]:
    if not isinstance(value, list):
        raise ValueError("facts must be an array")
    result = {}
    for index, raw in enumerate(value):
        _check_cancelled(is_cancelled)
        fact = _obj(raw, f"facts[{index}]")
        identity = _str(fact.get("observation_id"), f"facts[{index}].observation_id")
        if identity in result:
            raise ValueError("observation_id values must be unique")
        _decimal(fact.get("value"), f"facts[{index}].value", optional=True)
        result[identity] = _json_copy(fact, f"facts[{index}]")
    return result


def _selected_time(fact: dict, relation: dict, vintage_basis: str) -> str | None:
    if vintage_basis == "filed":
        return _iso_date(fact.get("filed"), "fact.filed", optional=True)
    capture = relation.get("captured_at") if relation is not None else fact.get("captured_at")
    if capture is None:
        return None
    return _instant(capture, "captured_at").isoformat()


def _correction_view(facts: dict[str, dict], corrections: list, vintage_basis: str, as_of: str,
                     *, is_cancelled=None) -> dict:
    if vintage_basis not in {"filed", "workspace_capture"}:
        raise ValueError("vintage_basis must be filed or workspace_capture")
    cutoff_date = _date(as_of, "as_of") if vintage_basis == "filed" else None
    cutoff_instant = _instant(as_of, "as_of") if vintage_basis == "workspace_capture" else None
    visible = set()
    unavailable_facts = []
    for identity, fact in facts.items():
        _check_cancelled(is_cancelled)
        fact_time = _selected_time(fact, None, vintage_basis)
        if fact_time is None:
            unavailable_facts.append(identity)
            continue
        if vintage_basis == "filed":
            if _date(fact_time, "fact.filed") <= cutoff_date:
                visible.add(identity)
        elif _instant(fact_time, "fact.captured_at") <= cutoff_instant:
            visible.add(identity)
    edges = []
    adjacency: dict[str, list[str]] = {}
    edge_keys = set()
    for index, raw in enumerate(corrections):
        _check_cancelled(is_cancelled)
        row = _obj(raw, f"corrections[{index}]")
        old_id = _str(row.get("old_observation_id"), f"corrections[{index}].old_observation_id")
        new_id = _str(row.get("new_observation_id"), f"corrections[{index}].new_observation_id")
        if old_id not in facts or new_id not in facts or old_id == new_id:
            raise ValueError("each correction must link two distinct retained facts")
        pair = (old_id, new_id)
        if pair in edge_keys:
            raise ValueError("correction relation is duplicated")
        edge_keys.add(pair)
        reason = _str(row.get("reason"), f"corrections[{index}].reason")
        source_ref = _json_copy(_obj(row.get("source_ref"), f"corrections[{index}].source_ref"),
                                f"corrections[{index}].source_ref")
        if not source_ref:
            raise ValueError("correction source_ref must identify retained evidence")
        old_fact, new_fact = facts[old_id], facts[new_id]
        if _fact_signature(old_fact) != _fact_signature(new_fact):
            raise ValueError("a correction must retain entity, metric, unit, period, dimensions, and disclosure scope")
        old_filed, new_filed = old_fact.get("filed"), new_fact.get("filed")
        if old_filed and new_filed and _date(new_filed, "new fact filed") < _date(old_filed, "old fact filed"):
            raise ValueError("correcting filing cannot precede the fact it corrects")
        capture = _str(row.get("captured_at"), f"corrections[{index}].captured_at", optional=True)
        if capture is not None:
            _instant(capture, f"corrections[{index}].captured_at")
        edge = {
            "old_observation_id": old_id,
            "new_observation_id": new_id,
            "reason": reason,
            "source_ref": source_ref,
            "published_at": new_filed,
            "captured_at": capture or new_fact.get("captured_at"),
            "old_value": old_fact.get("value"),
            "new_value": new_fact.get("value"),
        }
        edges.append(edge)
        adjacency.setdefault(old_id, []).append(new_id)

    visiting, visited = set(), set()

    def visit(node: str):
        _check_cancelled(is_cancelled)
        if node in visiting:
            raise ValueError("correction relations cannot contain cycles")
        if node in visited:
            return
        visiting.add(node)
        for successor in adjacency.get(node, []):
            visit(successor)
        visiting.remove(node)
        visited.add(node)

    for node in facts:
        _check_cancelled(is_cancelled)
        visit(node)

    eligible_by_old: dict[str, list[dict]] = {}
    ineligible_time = []
    for edge in edges:
        if edge["old_observation_id"] not in visible or edge["new_observation_id"] not in visible:
            continue
        selected_time = _selected_time(facts[edge["new_observation_id"]], edge, vintage_basis)
        if selected_time is None:
            ineligible_time.append(edge["old_observation_id"])
            continue
        if vintage_basis == "filed":
            if _date(selected_time, "correction filed") <= cutoff_date:
                eligible_by_old.setdefault(edge["old_observation_id"], []).append(edge)
        elif _instant(selected_time, "correction capture") <= cutoff_instant:
            eligible_by_old.setdefault(edge["old_observation_id"], []).append(edge)
    superseded = set()
    conflicted = set()
    for old_id, eligible in eligible_by_old.items():
        successors = {edge["new_observation_id"] for edge in eligible}
        if len(successors) > 1:
            conflicted.add(old_id)
            conflicted.update(successors)
        else:
            superseded.add(old_id)
    active = [
        facts[identity] for identity in facts
        if identity in visible and identity not in superseded and identity not in conflicted
    ]
    conflict_rows = [
        {"observation_id": identity, "reason": "MULTIPLE_ELIGIBLE_CORRECTIONS"}
        for identity in sorted(conflicted)
    ]
    return {
        "vintage_basis": vintage_basis,
        "as_of": as_of,
        "active_facts": active,
        "conflicts": conflict_rows,
        "corrections": edges,
        "relations_without_selected_time": sorted(set(ineligible_time)),
        "facts_without_selected_time": sorted(unavailable_facts),
        "facts_not_yet_available": sorted(set(facts) - visible),
        "superseded_observation_ids": sorted(superseded),
    }


def _financial_fact_corrections(inputs: dict, *, is_cancelled=None) -> dict:
    if set(inputs) - {"facts", "corrections", "vintage_basis", "as_of"}:
        raise ValueError("financial_fact_corrections contains unsupported fields")
    facts = _fact_index(inputs.get("facts"), is_cancelled=is_cancelled)
    corrections = inputs.get("corrections")
    if not isinstance(corrections, list):
        raise ValueError("corrections must be an array")
    vintage_basis = inputs.get("vintage_basis")
    as_of = _str(inputs.get("as_of"), "as_of")
    view = _correction_view(facts, corrections, vintage_basis, as_of, is_cancelled=is_cancelled)
    correction_rows = []
    for edge in view["corrections"]:
        old, new = facts[edge["old_observation_id"]], facts[edge["new_observation_id"]]
        source_ref = edge["source_ref"]
        source_label = source_ref.get("filename") or source_ref.get("title") or "已保存的更正依据"
        correction_rows.append([
            _correction_fact_label(old), old.get("value"), old.get("filed"),
            _correction_fact_label(new), new.get("value"), new.get("filed"), new.get("captured_at"),
            new.get("unit"), new.get("period_start"), new.get("period_end"),
            edge["reason"], source_label,
        ])
    conflict_rows = [[_correction_fact_label(facts[row["observation_id"]]), row["reason"]] for row in view["conflicts"]]
    sections = [
        {
            "id": "financial_corrections.history",
            "title": "不可变事实更正链",
            "body": f"保留 {len(view['corrections'])} 条事实级更正关系；原始值保持可读，新值只从对应更正依据出现时生效。\n\n" +
            _md_table(["原事实（指标/主体/期间/原文定位）", "原值", "原Filed", "更正事实（指标/主体/期间/原文定位）", "更正值", "更正Filed", "取得时间", "单位", "期间起", "期间止", "更正理由", "更正依据"], correction_rows) +
            "\n\n原始事实行：\n\n" +
            _md_table(
                ["指标", "披露主体", "申报编号", "Filed", "取得时间", "期间", "值", "单位", "维度", "披露范围", "会计基础", "状态", "原文位置", "材料"],
                _fact_table_rows(list(facts.values())),
            ),
            "data": {
                "facts": list(facts.values()), "corrections": view["corrections"],
                "superseded_observation_ids": view["superseded_observation_ids"],
            },
        },
        {
            "id": "financial_corrections.as_of_view",
            "title": "所选信息截止视图",
            "body": f"按 {vintage_basis}={as_of} 选择当时已知事实；同一事实存在多个有效更正分支时显示冲突，不选任一数值。\n\n" +
            _md_table(
                ["指标", "披露主体", "申报编号", "Filed", "取得时间", "期间", "当前可见值", "单位", "维度", "披露范围", "状态", "原文位置", "材料"],
                _fact_table_rows(view["active_facts"]),
            ) +
            "\n\n冲突分支：\n\n" + _md_table(["事实（指标/主体/期间/原文定位）", "原因"], conflict_rows),
            "data": {
                "vintage_basis": vintage_basis, "as_of": as_of,
                "active_facts": view["active_facts"], "conflicts": view["conflicts"],
                "relations_without_selected_time": view["relations_without_selected_time"],
                "facts_without_selected_time": view["facts_without_selected_time"],
                "facts_not_yet_available": view["facts_not_yet_available"],
            },
        },
    ]
    limitations = [
        "更正关系须由调用方依据保留原文提交；自动解析不证明更正具有法律效力或经济含义正确。",
        "更正只允许同一实体、指标、单位、期间、维度和披露范围；身份或期间变化应作为新事实处理。",
        "filed时间与工作区实际capture时间分别选择，不把二者拼成同一历史信息集。",
        "此操作返回不可变的修订视图，不写数据库；主接线负责Material、Artifact版本和下游影响引用。",
    ]
    return {"title": "财报事实更正与时点视图", "sections": sections, "limitations": limitations}


def _quarter_key(start: Any, end: Any) -> str | None:
    try:
        begin = _date(start, "period_start")
        finish = _date(end, "period_end")
    except ValueError:
        return None
    if begin.year != finish.year or begin.day != 1 or begin.month not in {1, 4, 7, 10}:
        return None
    end_month = begin.month + 2
    if finish.month != end_month or finish.day != calendar.monthrange(finish.year, finish.month)[1]:
        return None
    quarter = (begin.month - 1) // 3 + 1
    return f"{begin.year}-Q{quarter}"


def _quarter_sequence(first: str, last: str) -> list[str]:
    first_year, first_quarter = int(first[:4]), int(first[-1])
    last_year, last_quarter = int(last[:4]), int(last[-1])
    start = first_year * 4 + first_quarter - 1
    finish = last_year * 4 + last_quarter - 1
    return [f"{ordinal // 4}-Q{ordinal % 4 + 1}" for ordinal in range(start, finish + 1)]


def _financial_quarterly_series(inputs: dict, *, is_cancelled=None) -> dict:
    allowed = {
        "facts", "corrections", "metric", "unit", "filing_entity", "disclosure_scope",
        "dimensions", "statement_basis", "vintage_basis", "as_of", "periods",
    }
    if set(inputs) - allowed:
        raise ValueError("financial_quarterly_series contains unsupported fields")
    facts = _fact_index(inputs.get("facts"), is_cancelled=is_cancelled)
    metric = _str(inputs.get("metric"), "metric")
    unit = _str(inputs.get("unit"), "unit")
    entity = _filing_entity(inputs.get("filing_entity"))
    disclosure_scope = _str(inputs.get("disclosure_scope", "filing_entity"), "disclosure_scope")
    dimensions = _json_copy(_obj(inputs.get("dimensions", {}), "dimensions"), "dimensions")
    statement_basis = _str(inputs.get("statement_basis"), "statement_basis")
    vintage_basis = inputs.get("vintage_basis")
    if vintage_basis not in {"filed", "workspace_capture"}:
        raise ValueError("vintage_basis must be filed or workspace_capture")
    as_of = _str(inputs.get("as_of"), "as_of")
    cutoff_date = _date(as_of, "as_of") if vintage_basis == "filed" else None
    cutoff_instant = _instant(as_of, "as_of") if vintage_basis == "workspace_capture" else None
    raw_periods = inputs.get("periods")
    if raw_periods is not None:
        if (
            not isinstance(raw_periods, list)
            or any(not isinstance(period, str) or not re.fullmatch(r"20\d{2}-Q[1-4]", period) for period in raw_periods)
            or len(raw_periods) != len(set(raw_periods))
        ):
            raise ValueError("periods must contain unique YYYY-Qn values")
        requested = sorted(raw_periods, key=lambda value: int(value[:4]) * 4 + int(value[-1]))
    else:
        requested = None

    corrections = inputs.get("corrections", [])
    if not isinstance(corrections, list):
        raise ValueError("corrections must be an array")
    correction_conflicts = []
    correction_conflict_periods: dict[str, list[str]] = {}
    if corrections:
        view = _correction_view(facts, corrections, vintage_basis, as_of, is_cancelled=is_cancelled)
        current_facts = {row["observation_id"]: row for row in view["active_facts"]}
        correction_conflicts = view["conflicts"]
        for conflict in correction_conflicts:
            fact = facts.get(conflict["observation_id"])
            if fact is None:
                continue
            fact_entity = fact.get("filing_entity") or {}
            if (
                fact.get("metric") == metric
                and fact.get("unit") == unit
                and fact_entity.get("scheme") == entity["scheme"]
                and fact_entity.get("identifier") == entity["identifier"]
                and fact.get("disclosure_scope") == disclosure_scope
                and fact.get("dimensions", {}) == dimensions
                and fact.get("statement_basis") == statement_basis
            ):
                quarter = _quarter_key(fact.get("period_start"), fact.get("period_end"))
                if quarter is not None:
                    correction_conflict_periods.setdefault(quarter, []).append(fact["observation_id"])
    else:
        current_facts = facts

    eligible: dict[str, list[dict]] = {}
    excluded = 0
    for fact in current_facts.values():
        _check_cancelled(is_cancelled)
        fact_entity = fact.get("filing_entity") or {}
        if (
            fact.get("status") not in {"DISCLOSED", "MISSING"}
            or
            fact.get("metric") != metric
            or fact.get("unit") != unit
            or fact_entity.get("scheme") != entity["scheme"]
            or fact_entity.get("identifier") != entity["identifier"]
            or fact.get("disclosure_scope") != disclosure_scope
            or fact.get("dimensions", {}) != dimensions
            or fact.get("statement_basis") != statement_basis
        ):
            continue
        quarter = _quarter_key(fact.get("period_start"), fact.get("period_end"))
        if quarter is None:
            excluded += 1
            continue
        if vintage_basis == "filed":
            filed = _iso_date(fact.get("filed"), "fact.filed", optional=True)
            if filed is None or _date(filed, "fact.filed") > cutoff_date:
                continue
            selected_time = filed
        else:
            captured_at = _instant(fact.get("captured_at"), "fact.captured_at", optional=True)
            if captured_at is None or captured_at > cutoff_instant:
                continue
            selected_time = captured_at.isoformat()
        eligible.setdefault(quarter, []).append({"fact": fact, "selected_time": selected_time})

    if requested is None:
        quarters = sorted(set(eligible) | set(correction_conflict_periods), key=lambda value: int(value[:4]) * 4 + int(value[-1]))
        requested = _quarter_sequence(quarters[0], quarters[-1]) if quarters else []
    rows = []
    for quarter in requested:
        _check_cancelled(is_cancelled)
        candidates = eligible.get(quarter, [])
        if not candidates:
            if quarter in correction_conflict_periods:
                rows.append({
                    "period": quarter, "state": "CONFLICT",
                    "reason": "CONFLICTING_FACT_CORRECTIONS_AT_SELECTED_VINTAGE",
                    "value": None, "observation_ids": sorted(set(correction_conflict_periods[quarter])),
                    "fact_labels": [
                        _correction_fact_label(facts[identity])
                        for identity in sorted(set(correction_conflict_periods[quarter]))
                        if identity in facts
                    ],
                })
                continue
            rows.append({"period": quarter, "state": "MISSING", "reason": "NO_EXPLICIT_FACT_AVAILABLE_AS_OF", "value": None, "observation_ids": [], "fact_labels": [], "source_refs": []})
            continue
        latest_time = max(row["selected_time"] for row in candidates)
        latest = [row["fact"] for row in candidates if row["selected_time"] == latest_time]
        values = {row.get("value") for row in latest}
        if len(values) != 1:
            rows.append({
                "period": quarter, "state": "CONFLICT", "reason": "CONFLICTING_FACTS_AT_SELECTED_VINTAGE",
                "value": None, "selected_time": latest_time,
                "observation_ids": sorted(row["observation_id"] for row in latest),
                "filing_ids": sorted({row.get("filing_id") for row in latest}),
                "fact_labels": [_correction_fact_label(row) for row in latest],
                "source_refs": [row.get("source_ref") for row in latest if row.get("source_ref") is not None],
            })
            continue
        chosen = sorted(latest, key=lambda row: row["observation_id"])[0]
        rows.append({
            "period": quarter,
            "state": "MISSING" if chosen.get("value") is None else "AVAILABLE",
            "reason": "DISCLOSED_MISSING_VALUE" if chosen.get("value") is None else None,
            "value": chosen.get("value"),
            "selected_time": latest_time,
            "filing_id": chosen.get("filing_id"),
            "filed": chosen.get("filed"),
            "captured_at": chosen.get("captured_at"),
            "observation_ids": sorted(row["observation_id"] for row in latest),
            "fact_labels": [_correction_fact_label(row) for row in latest],
            "source_refs": [row.get("source_ref") for row in latest if row.get("source_ref") is not None],
        })
    available = sum(row["state"] == "AVAILABLE" for row in rows)
    state = "INSUFFICIENT" if not available else "AVAILABLE" if available == len(rows) else "PARTIAL"
    quarter_rows = [[
        row["period"], _financial_state_label(row["state"]), row.get("value"), unit, row.get("selected_time"),
        row.get("filing_id"), "; ".join(row.get("fact_labels", [])),
        ", ".join(_financial_material_label(source) for source in row.get("source_refs", [])),
        _quarter_selection_reason_label(row.get("reason")),
    ] for row in rows]
    statement_basis_label = statement_basis
    if statement_basis.startswith("Inline XBRL numeric fact"):
        statement_basis_label = "原件数值事实（报表/附注位置未由本次解析独立判断）"
    elif statement_basis.startswith("standard-taxonomy;"):
        statement_basis_label = "标准分类事实（财报表内或附注位置未单独区分）"
    scope_label = "披露法律主体" if disclosure_scope == "filing_entity" else disclosure_scope
    sections = [
        {
            "id": "financial_quarters.series",
            "title": "按选定信息集排列的完整季度",
            "body": f"主体：{_entity_label(entity)}；指标：{_financial_metric_label(metric)}；单位：{unit}；披露范围：{scope_label}；期间维度：{_dimensions_label(dimensions)}；口径：{statement_basis_label}。按“{_financial_vintage_label(vintage_basis)}”截至 {as_of} 选择，共 {len(rows)} 个自然季度，其中 {available} 个有可用数值。\n\n" +
            _md_table(["季度", "状态", "值", "单位", "所选信息时间", "申报编号", "事实定位（指标/主体/期间/原文）", "来源材料", "缺失/冲突原因"], quarter_rows),
            "data": {
                "state": state, "metric": metric, "unit": unit,
                "filing_entity": entity, "disclosure_scope": disclosure_scope,
                "dimensions": dimensions, "statement_basis": statement_basis,
                "vintage_basis": vintage_basis, "as_of": as_of,
                "periods": rows, "excluded_non_calendar_quarter_facts": excluded,
                "correction_conflicts": correction_conflicts,
            },
        }
    ]
    return {
        "title": "财报季度与信息截止选择",
        "sections": sections,
        "limitations": [
            "申报披露时间与工作区实际取得时间是两种不同的信息集；本次只按请求的一个时间轴筛选。",
            "只接受完整自然季度；非自然季度、年度报表、半年累计值和OpenDART累计金额不插值、不拆分、不相减。",
            "同一选定时间下出现冲突版本时明确列为待核，不按文件名、年份或原文行顺序裁决。",
            "不预设最低季度数；后续模型仍须满足自己的样本长度、变化、诊断与时间外评价条件。",
        ],
    }


def _mmm_diagnostics(summary, parameter_sizes: dict) -> dict:
    import numpy as np

    expected = [
        label for name, size in parameter_sizes.items()
        for label in ([name] if size is None else [f"{name}[{index}]" for index in range(size)])
    ]
    failures = []
    if (
        not summary.index.is_unique
        or set(summary.index) != set(expected)
        or not {"r_hat", "ess_bulk"}.issubset(summary.columns)
    ):
        return {
            "max_rhat": None, "min_ess_bulk": None,
            "diagnostic_failures": [{"reason": "REQUIRED_PARAMETER_DIAGNOSTICS_MISSING_OR_DUPLICATED"}],
            "parameter_results": [],
        }
    values = []
    parameter_results = []
    for name in expected:
        try:
            rhat, ess = (float(summary.loc[name, field]) for field in ("r_hat", "ess_bulk"))
        except (TypeError, ValueError, OverflowError):
            failures.append({"parameter": name, "reason": "DIAGNOSTICS_NOT_NUMERIC"})
            parameter_results.append({"parameter": name, "r_hat": None, "ess_bulk": None, "state": "NOT_NUMERIC"})
            continue
        if not np.isfinite([rhat, ess]).all():
            failures.append({"parameter": name, "reason": "DIAGNOSTICS_NOT_FINITE"})
            parameter_results.append({"parameter": name, "r_hat": None, "ess_bulk": None, "state": "NOT_FINITE"})
            continue
        values.append((rhat, ess))
        passed = rhat > 0 and rhat <= 1.05 and ess >= 200
        parameter_results.append({"parameter": name, "r_hat": rhat, "ess_bulk": ess,
                                  "state": "PASS" if passed else "FAIL"})
        if not passed:
            failures.append({"parameter": name, "reason": "DIAGNOSTICS_THRESHOLD_NOT_PASSED"})
    complete = len(values) == len(expected) and bool(values)
    return {
        "max_rhat": max(value[0] for value in values) if complete else None,
        "min_ess_bulk": min(value[1] for value in values) if complete else None,
        "diagnostic_failures": failures,
        "parameter_results": parameter_results,
    }


def _mmm_number(value: Any, field: str, *, nonnegative: bool = False) -> float:
    number = _decimal(value, field)
    result = float(number)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{field} is outside the finite model range")
    return result


def _mmm_decimal_text(value: Any, field: str) -> str:
    number = _decimal(value, field)
    return _decimal_text(number)


def _financial_mmm_analysis(inputs: dict, *, is_cancelled=None) -> dict:
    allowed = {
        "periods", "currency", "outcome_unit", "filing_entity", "disclosure_scope",
        "spend_basis", "budget", "region", "_resolved_macro_controls",
        "observation_frequency", "original_observation_count", "transformation",
        "vintage_basis", "as_of", "source_refs", "channel_labels",
    }
    if set(inputs) - allowed:
        raise ValueError("financial_mmm_analysis contains unsupported fields")
    _check_cancelled(is_cancelled)
    raw_periods = inputs.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods:
        raise ValueError("financial_mmm_analysis requires explicit quarterly rows")
    currency = _str(inputs.get("currency"), "currency")
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency.upper() != currency:
        raise ValueError("currency must be an explicit uppercase currency code")
    outcome_unit = _str(inputs.get("outcome_unit"), "outcome_unit")
    entity = _filing_entity(inputs.get("filing_entity"))
    disclosure_scope = inputs.get("disclosure_scope")
    if disclosure_scope not in {"filing_entity", "brand", "segment", "project", "store"}:
        raise ValueError("disclosure_scope is invalid")
    spend_basis = inputs.get("spend_basis")
    if spend_basis not in {"reported", "public_proxy", "user_declared"}:
        raise ValueError("spend_basis must identify reported, public_proxy, or user_declared values")
    vintage_basis = inputs.get("vintage_basis")
    if vintage_basis not in {"filed", "workspace_capture"}:
        raise ValueError("vintage_basis must be filed or workspace_capture")
    as_of = _str(inputs.get("as_of"), "as_of")
    if vintage_basis == "filed":
        _date(as_of, "as_of")
    else:
        _instant(as_of, "as_of")
    observation_frequency = inputs.get("observation_frequency")
    original_count = inputs.get("original_observation_count")
    transformation = inputs.get("transformation")
    if observation_frequency not in {None, "quarter"}:
        raise ValueError("MMM currently requires explicitly aligned quarterly observations")
    if original_count is not None and (type(original_count) is not int or original_count < 0):
        raise ValueError("original_observation_count must be a nonnegative integer")
    if transformation is not None and transformation not in {"reported", "none"}:
        raise ValueError("quarterly disaggregation or transformation is not accepted")
    input_source_refs = _json_copy(_obj(inputs.get("source_refs", {}), "source_refs"), "source_refs")
    controls = inputs.get("_resolved_macro_controls", [])
    if "macro_controls" in inputs or not isinstance(controls, list):
        raise ValueError("macro controls must be resolved by the financial source owner")
    if controls and inputs.get("region") != "CN-national":
        raise ValueError("resolved national macro controls cannot be mixed with another region")
    periods = []
    channels = None
    serial = []
    x_values, y_values = [], []
    period_sources = []
    missing_outcome_periods = []
    missing_spend_periods: dict[str, list[str]] = {}
    for index, row in enumerate(raw_periods):
        _check_cancelled(is_cancelled)
        field = f"periods[{index}]"
        if (
            not isinstance(row, dict)
            or set(row) - {"period", "outcome", "spend", "source_refs", "source"}
            or not {"period", "outcome", "spend"}.issubset(row)
            or not isinstance(row["period"], str)
            or not re.fullmatch(r"20\d{2}-Q[1-4]", row["period"])
            or not isinstance(row["spend"], dict)
            or not row["spend"]
            or any(not isinstance(channel, str) or not channel.strip() for channel in row["spend"])
        ):
            raise ValueError(f"{field} must contain a quarter, outcome, and channel spend map")
        current_channels = sorted(row["spend"])
        if channels is None:
            channels = current_channels
        elif current_channels != channels:
            raise ValueError("quarterly channel set changed; resolve source mapping before model selection")
        # Financial outcomes such as operating profit or net cash flow can be
        # negative. Only spend/budget inputs are constrained to nonnegative.
        outcome = None if row["outcome"] is None else _mmm_number(row["outcome"], f"{field}.outcome")
        spend = []
        for channel in channels:
            value = row["spend"][channel]
            if value is None:
                missing_spend_periods.setdefault(channel, []).append(row["period"])
                spend.append(None)
            else:
                spend.append(_mmm_number(value, f"{field}.spend[{channel}]", nonnegative=True))
        if outcome is None:
            missing_outcome_periods.append(row["period"])
        y_values.append(outcome)
        x_values.append(spend)
        serial.append(int(row["period"][:4]) * 4 + int(row["period"][-1]) - 1)
        source_refs = _json_copy(
            _obj(row.get("source_refs", input_source_refs), f"{field}.source_refs"),
            f"{field}.source_refs",
        )
        periods.append({
            "period": row["period"],
            "outcome": None if outcome is None else _mmm_decimal_text(row["outcome"], f"{field}.outcome"),
            "spend": {
                channel: None if row["spend"][channel] is None else _mmm_decimal_text(row["spend"][channel], f"{field}.spend[{channel}]")
                for channel in channels
            },
            "source_refs": source_refs,
        })
        period_sources.append(source_refs)
    if len(set(serial)) != len(serial):
        raise ValueError("MMM input cannot contain duplicate financial quarters")
    if serial != sorted(serial):
        order = sorted(range(len(serial)), key=serial.__getitem__)
        serial = [serial[index] for index in order]
        periods = [periods[index] for index in order]
        x_values = [x_values[index] for index in order]
        y_values = [y_values[index] for index in order]
        period_sources = [period_sources[index] for index in order]
    missing_quarters = [
        f"{ordinal // 4}-Q{ordinal % 4 + 1}"
        for previous, current in zip(serial, serial[1:])
        for ordinal in range(previous + 1, current)
    ]
    if not channels:
        raise ValueError("at least one channel is required")
    raw_channel_labels = inputs.get("channel_labels", {})
    if not isinstance(raw_channel_labels, dict) or set(raw_channel_labels) - set(channels):
        raise ValueError("channel_labels must name only selected spend channels")
    channel_labels = {}
    for channel in channels:
        label = raw_channel_labels.get(channel, channel)
        channel_labels[channel] = _str(label, f"channel_labels.{channel}")
    for index, control in enumerate(controls):
        _check_cancelled(is_cancelled)
        if (
            not isinstance(control, dict)
            or not isinstance(control.get("metric"), str)
            or not isinstance(control.get("periods"), list)
            or [row.get("period") for row in control["periods"]] != [row["period"] for row in periods]
        ):
            raise ValueError(f"resolved macro control {index} is invalid or misaligned")
        if control.get("vintage_basis") != vintage_basis or control.get("as_of") != as_of:
            raise ValueError("macro controls and financial series use different information vintages")
        if control.get("region") != inputs.get("region"):
            raise ValueError("macro control region does not match the outcome region")

    data = {
        "periods": periods, "currency": currency, "outcome_unit": outcome_unit,
        "filing_entity": entity, "disclosure_scope": disclosure_scope,
        "spend_basis": spend_basis, "budget": inputs.get("budget"),
        "region": inputs.get("region"), "controls": controls,
        "vintage_basis": vintage_basis, "as_of": as_of,
        "source_refs": period_sources,
        "channel_labels": channel_labels,
        "observation_frequency": observation_frequency,
        "original_observation_count": original_count,
        "transformation": transformation,
        "input_source_refs": input_source_refs,
        "missing_quarters": missing_quarters,
        "missing_outcome_periods": sorted(set(missing_outcome_periods)),
        "missing_spend_periods": {channel: sorted(set(missing)) for channel, missing in missing_spend_periods.items()},
    }

    def insufficient(reason: str, state: str = "INSUFFICIENT") -> dict:
        result = _mmm_result_kwargs(data, channels)
        result.update(state=state, reason=reason)
        return _mmm_sections(result)

    if observation_frequency is None:
        return insufficient("SOURCE_OBSERVATION_FREQUENCY_UNVERIFIED")
    if original_count is None:
        return insufficient("SOURCE_OBSERVATION_COUNT_UNVERIFIED")
    if transformation is None:
        return insufficient("PERIOD_CONSTRUCTION_UNVERIFIED")
    if original_count < len(periods):
        return insufficient("RESAMPLED_PERIODS_NOT_INDEPENDENT")
    if original_count != len(periods):
        return insufficient("ORIGINAL_OBSERVATION_COUNT_MISMATCH")
    if any(not source_refs for source_refs in period_sources):
        return insufficient("PERIOD_SOURCE_REFERENCES_INCOMPLETE")
    if missing_quarters:
        return insufficient("MISSING_QUARTERS_NOT_INTERPOLATED")
    if missing_outcome_periods or missing_spend_periods:
        return insufficient("QUARTERLY_SOURCE_VALUES_INCOMPLETE")
    try:
        import numpy as np
        import pymc as pm
        import pytensor.tensor as pt
        import arviz as az
        import statsmodels
        from scipy.optimize import minimize
    except ImportError as exc:
        return insufficient("STATISTICAL_RUNTIME_UNAVAILABLE:" + (getattr(exc, "name", None) or "unknown"), "UNAVAILABLE")
    if len(periods) < 12:
        return insufficient("NEED_AT_LEAST_12_REAL_QUARTERS")
    split = len(periods) - max(4, int(len(periods) * 0.2))
    if split < 12:
        return insufficient("NEED_12_TRAIN_AND_4_DISJOINT_TEST_QUARTERS")
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("MMM input contains a non-finite value")
    if np.any(np.std(x[:split], axis=0) <= 1e-10) or np.std(y[:split]) <= 1e-10:
        return insufficient("NO_TRAINING_CHANNEL_OR_OUTCOME_VARIATION")
    scale = x[:split].mean(axis=0)
    if np.any(scale <= 0):
        return insufficient("CHANNEL_SCALE_NOT_ESTIMABLE")
    ymean, yscale = float(y[:split].mean()), float(y[:split].std())
    scaled = x / scale
    lag_count = min(8, split)
    lagged = np.zeros((len(x), len(channels), lag_count))
    for lag in range(lag_count):
        _check_cancelled(is_cancelled)
        lagged[lag:, :, lag] = scaled[: len(x) - lag]
    time_axis = (np.arange(len(x)) - (split - 1) / 2) / split
    seasonal = np.column_stack([
        np.sin(2 * np.pi * np.arange(len(x)) / 4),
        np.cos(2 * np.pi * np.arange(len(x)) / 4),
    ])
    control_matrix = None
    control_means = control_scales = None
    if controls:
        allowed_aggregation = {
            "cpi_yoy_percent": {"arithmetic-mean-monthly-yoy-percent"},
            "retail_sales": {"sum-complete-monthly-totals", "explicit-reported-quarter-total"},
            "domestic_tourism_trips": {"explicit-reported-quarter-total"},
            "domestic_tourism_spend": {"explicit-reported-quarter-total"},
        }
        for control in controls:
            if control.get("metric") not in allowed_aggregation:
                return insufficient("MACRO_CONTROL_METRIC_NOT_SUPPORTED_BY_THIS_MODEL")
            if any(row.get("state") != "AVAILABLE" for row in control["periods"]):
                return insufficient("MACRO_CONTROL_SERIES_INCOMPLETE_AT_SELECTED_VINTAGE")
            if any(row.get("aggregation") not in allowed_aggregation[control["metric"]] for row in control["periods"]):
                return insufficient("MACRO_CONTROL_AGGREGATION_NOT_SUPPORTED_BY_THIS_MODEL")
        values = np.asarray([
            [_mmm_number(control["periods"][i]["value"], "macro control value") for control in controls]
            for i in range(len(periods))
        ], dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("macro control contains a non-finite value")
        control_means, control_scales = values[:split].mean(axis=0), values[:split].std(axis=0)
        if np.any(control_scales <= 1e-10):
            return insufficient("NO_TRAINING_MACRO_CONTROL_VARIATION")
        control_matrix = (values - control_means) / control_scales
        design = np.column_stack([np.ones(split), time_axis[:split], seasonal[:split], control_matrix[:split]])
        if np.linalg.matrix_rank(design) < design.shape[1]:
            return insufficient("MACRO_CONTROL_DESIGN_NOT_IDENTIFIABLE")

    budget = data["budget"]
    lower = upper = None
    if budget is not None:
        if not isinstance(budget, dict) or set(budget) != {"total", "lower", "upper"}:
            raise ValueError("budget must contain total, per-channel lower, and per-channel upper bounds")
        total = _mmm_number(budget["total"], "budget.total", nonnegative=True)
        if not isinstance(budget["lower"], dict) or not isinstance(budget["upper"], dict):
            raise ValueError("budget bounds must be keyed by every channel")
        if set(budget["lower"]) != set(channels) or set(budget["upper"]) != set(channels):
            raise ValueError("budget bounds must match the selected channels")
        lower = np.asarray([_mmm_number(budget["lower"][channel], f"budget.lower[{channel}]", nonnegative=True) for channel in channels])
        upper = np.asarray([_mmm_number(budget["upper"][channel], f"budget.upper[{channel}]", nonnegative=True) for channel in channels])
        if np.any(upper < lower) or not lower.sum() <= total <= upper.sum():
            raise ValueError("budget bounds do not admit the stated total")
        budget = {"total": total, "lower": lower, "upper": upper}

    _check_cancelled(is_cancelled)
    with pm.Model() as model:
        alpha = pm.Beta("adstock_alpha", 2, 2, shape=len(channels))
        half = pm.LogNormal("half_saturation", 0, 0.5, shape=len(channels))
        beta = pm.HalfNormal("channel_beta", 1, shape=len(channels))
        intercept = pm.Normal("intercept", 0, 1)
        trend = pm.Normal("trend", 0, 0.5)
        season = pm.Normal("season", 0, 0.5, shape=2)
        sigma = pm.HalfNormal("sigma", 1)
        weights = alpha[:, None] ** np.arange(lag_count)[None, :]
        adstock = pt.sum(lagged * weights[None, :, :], axis=2) / pt.sum(weights, axis=1)[None, :]
        contribution = beta[None, :] * adstock / (half[None, :] + adstock)
        mu = intercept + trend * time_axis + pt.dot(seasonal, season) + pt.sum(contribution, axis=1)
        if control_matrix is not None:
            control_beta = pm.Normal("macro_control_beta", 0, 0.5, shape=len(controls))
            mu = mu + pt.dot(control_matrix, control_beta)
            pm.Deterministic("macro_control_contribution", yscale * control_matrix * control_beta)
        pm.Deterministic("expected_outcome", ymean + yscale * mu)
        pm.Deterministic("channel_contribution", yscale * contribution)
        pm.Normal("observed", mu=mu[:split], sigma=sigma, observed=(y[:split] - ymean) / yscale)

        def sample_callback(trace, draw):
            if is_cancelled is not None and is_cancelled():
                raise KeyboardInterrupt

        try:
            trace = pm.sample(
                draws=500, tune=750, chains=4, cores=1, random_seed=20261017,
                target_accept=0.95, progressbar=False, compute_convergence_checks=True,
                nuts_sampler="pymc", callback=sample_callback if is_cancelled is not None else None,
            )
        except BaseException as exc:
            if is_cancelled is not None and is_cancelled():
                raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED_DURING_MMM_SAMPLING") from exc
            raise
    _check_cancelled(is_cancelled)

    parameters = ["adstock_alpha", "half_saturation", "channel_beta", "intercept", "trend", "season", "sigma"]
    if control_matrix is not None:
        parameters.append("macro_control_beta")
    summary = az.summary(trace, var_names=parameters, round_to=None)
    parameter_sizes = {
        name: (
            len(channels) if name in {"adstock_alpha", "half_saturation", "channel_beta"}
            else 2 if name == "season"
            else len(controls) if name == "macro_control_beta"
            else None
        )
        for name in parameters
    }
    diagnostics = _mmm_diagnostics(summary, parameter_sizes)
    divergences = int(np.asarray(trace.sample_stats["diverging"]).sum())
    sampling = {
        "library": "pymc", "version": pm.__version__, "chains": 4, "cores": 1,
        "draws_per_chain": 500, "tune_per_chain": 750, "seed": 20261017,
        "target_accept": 0.95, "divergences": divergences, **diagnostics,
    }
    base = _mmm_result_kwargs(data, channels)
    base.update(
        n=len(periods), train_n=split, test_n=len(periods) - split,
        sampling=sampling,
        priors={
            "adstock_alpha": "Beta(2,2)", "half_saturation": "LogNormal(0,0.5)",
            "channel_beta": "HalfNormal(1)", "intercept": "Normal(0,1)",
            "trend_and_season": "Normal(0,0.5)", "sigma": "HalfNormal(1)",
            "scaling": "training channel means and outcome standard deviation only",
        },
    )
    if controls:
        base["priors"]["macro_control_beta"] = "Normal(0,0.5); signed contextual association, not causal effect"
        base["control_scaling"] = {
            "basis": "training partition only", "train_n": split,
            "means": control_means.tolist(), "standard_deviations": control_scales.tolist(),
        }
    if diagnostics["diagnostic_failures"] or divergences:
        base.update(
            state="INSUFFICIENT",
            reason="POSTERIOR_DIAGNOSTICS_NOT_PASSED",
            sensitivity={"state": "NOT_RUN_PRIMARY_POSTERIOR_DIAGNOSTICS_FAILED", "variants": []},
        )
        return _mmm_sections(base, diagnostics=diagnostics, sampling=sampling)

    def draws(name: str):
        array = np.asarray(trace.posterior[name])
        return array.reshape((-1, *array.shape[2:]))

    expected = draws("expected_outcome")
    sigma_draws = draws("sigma") * yscale
    rng = np.random.default_rng(20261017)
    posterior_predictive = expected + rng.normal(size=expected.shape) * sigma_draws[:, None]
    predictions = expected.mean(axis=0)
    intervals = np.quantile(posterior_predictive, [0.025, 0.975], axis=0).T
    errors = predictions[split:] - y[split:]
    nonzero = y[split:] != 0
    holdout_actual = y[split:]
    last_value_baseline = np.full(len(holdout_actual), y[split - 1], dtype=float)
    seasonal_naive_baseline = np.asarray([
        y[split - 4 + (index % 4)] for index in range(len(holdout_actual))
    ], dtype=float)

    def baseline_metrics(predicted):
        baseline_errors = predicted - holdout_actual
        baseline_nonzero = holdout_actual != 0
        return {
            "prediction": predicted.tolist(),
            "mae": float(np.mean(np.abs(baseline_errors))),
            "rmse": float(np.sqrt(np.mean(baseline_errors ** 2))),
            "mape_percent": float(np.mean(np.abs(baseline_errors[baseline_nonzero] / holdout_actual[baseline_nonzero])) * 100)
            if np.all(baseline_nonzero) else None,
            "mape_reason": None if np.all(baseline_nonzero) else "ZERO_OUTCOME_DENOMINATOR",
        }

    contribution_draws = draws("channel_contribution")
    contribution_summary = np.quantile(contribution_draws[:, :split].mean(axis=1), [0.025, 0.5, 0.975], axis=0).T
    holdout = {
        "periods": [row["period"] for row in periods[split:]],
        "actual": y[split:].tolist(),
        "prediction": predictions[split:].tolist(),
        "predictive_intervals_95": intervals[split:].tolist(),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors ** 2))),
        "mape_percent": float(np.mean(np.abs(errors / y[split:])) * 100) if np.all(nonzero) else None,
        "mape_reason": None if np.all(nonzero) else "ZERO_OUTCOME_DENOMINATOR",
        "interval_coverage_observed": float(np.mean(
            (y[split:] >= intervals[split:, 0]) & (y[split:] <= intervals[split:, 1])
        )),
        "nominal_interval_coverage": 0.95,
        "benchmarks": {
            "last_training_value": baseline_metrics(last_value_baseline),
            "seasonal_naive_4_quarter": baseline_metrics(seasonal_naive_baseline),
            "benchmark_definition": "Last training value repeated; seasonal-naive uses the last four training quarters and repeats them without holdout updates.",
            "model_mae_delta_vs_seasonal_naive": float(np.mean(np.abs(errors)) - np.mean(np.abs(seasonal_naive_baseline - holdout_actual))),
            "model_mae_improves_over_seasonal_naive": bool(np.mean(np.abs(errors)) < np.mean(np.abs(seasonal_naive_baseline - holdout_actual))),
        },
        "interpretation": "Holdout prediction is conditional on observed holdout covariates and is not an ex-ante forecast.",
    }
    channels_result = [
        {"channel": channel, "mean_quarter_contribution_interval_95": contribution_summary[index].tolist(),
         "causal_effect": "NOT_ESTABLISHED"}
        for index, channel in enumerate(channels)
    ]
    budget_result = None
    if budget is not None:
        _check_cancelled(is_cancelled)
        betas, halves = draws("channel_beta") * yscale, draws("half_saturation")

        def utility(amount):
            normalized = amount / scale
            return np.mean(np.sum(betas * normalized / (halves + normalized), axis=1))

        initial = lower + (budget["total"] - lower.sum()) * (upper - lower) / max(1e-12, (upper - lower).sum())
        optimized = minimize(
            lambda amount: -utility(amount), initial, method="SLSQP", bounds=list(zip(lower, upper)),
            constraints={"type": "eq", "fun": lambda amount: np.sum(amount) - budget["total"]},
            options={"maxiter": 500, "ftol": 1e-9},
        )
        _check_cancelled(is_cancelled)
        if not optimized.success or abs(float(np.sum(optimized.x)) - budget["total"]) > 1e-5:
            budget_result = {"state": "INSUFFICIENT", "reason": "OPTIMIZATION_NOT_CONVERGED"}
        else:
            allocation = optimized.x
            normalized = allocation / scale
            derivatives = betas * halves / (halves + normalized) ** 2 / scale
            budget_result = {
                "state": "READY", "currency": currency,
                "allocation": {channel: float(value) for channel, value in zip(channels, allocation)},
                "objective": float(utility(allocation)),
                "marginal_outcome_per_currency_interval_95": {
                    channel: values for channel, values in zip(
                        channels, np.quantile(derivatives, [0.025, 0.5, 0.975], axis=0).T.tolist()
                    )
                },
                "assumption": "Hypothetical steady-state spend, normalized geometric carryover and concave posterior response; not a spending action or causal ROI.",
            }
    sensitivity = _mmm_sensitivity_variants(
        channels=channels,
        x=x,
        y=y,
        split=split,
        scale=scale,
        ymean=ymean,
        yscale=yscale,
        time_axis=time_axis,
        seasonal=seasonal,
        period_labels=[row["period"] for row in periods],
        controls=controls,
        control_matrix=control_matrix,
        lag_count=lag_count,
        budget=budget,
        lower=lower,
        upper=upper,
        currency=currency,
        is_cancelled=is_cancelled,
        np=np,
        pm=pm,
        pt=pt,
        az=az,
        minimize=minimize,
        main_seed=20261017,
    )
    base.update(
        state="READY", sampling=sampling, holdout=holdout,
        channels=channels_result, budget_scenario=budget_result,
        sensitivity=sensitivity,
        assumptions={
            "carryover": "geometric adstock with zero pre-window carryover and at most eight prior quarters",
            "response": "nonnegative additive Michaelis-Menten channel response",
            "model_scope": "declared filing/project scope; no automatic group-to-brand allocation",
        },
    )
    return _mmm_sections(base, diagnostics=diagnostics, sampling=sampling)


def _mmm_sensitivity_variants(
    *, channels, x, y, split, scale, ymean, yscale, time_axis, seasonal, period_labels,
    controls, control_matrix, lag_count, budget, lower, upper, currency,
    is_cancelled, np, pm, pt, az, minimize, main_seed,
) -> dict:
    """Refit the same holdout design under explicit lag and prior alternatives."""
    variants = []
    if lag_count > 4:
        variants.append({
            "id": "shorter_carryover_4_quarters",
            "lag_count": 4,
            "alpha_prior": [2, 2],
            "half_sigma": 0.5,
            "beta_scale": 1.0,
            "seed": main_seed + 1,
        })
    else:
        variants.append({
            "id": "shorter_carryover_4_quarters",
            "state": "NOT_VARIABLE_WITH_TRAINING_WINDOW",
            "lag_count": lag_count,
            "limitations": ["The primary model already uses at most four lagged quarters in this training window."],
        })
    variants.extend([
        {
            "id": "diffuse_channel_priors",
            "lag_count": lag_count,
            "alpha_prior": [1, 1],
            "half_sigma": 1.0,
            "beta_scale": 2.0,
            "seed": main_seed + 2,
        },
        {
            "id": "regularized_channel_priors",
            "lag_count": lag_count,
            "alpha_prior": [4, 4],
            "half_sigma": 0.25,
            "beta_scale": 0.5,
            "seed": main_seed + 3,
        },
    ])
    completed = []
    for variant in variants:
        _check_cancelled(is_cancelled)
        if variant.get("state"):
            completed.append(variant)
            continue
        current_lag_count = variant["lag_count"]
        variant_lagged = np.zeros((len(x), len(channels), current_lag_count))
        for lag in range(current_lag_count):
            _check_cancelled(is_cancelled)
            variant_lagged[lag:, :, lag] = x[: len(x) - lag] / scale
        alpha_a, alpha_b = variant["alpha_prior"]
        try:
            with pm.Model() as variant_model:
                alpha = pm.Beta("adstock_alpha", alpha_a, alpha_b, shape=len(channels))
                half = pm.LogNormal("half_saturation", 0, variant["half_sigma"], shape=len(channels))
                beta = pm.HalfNormal("channel_beta", variant["beta_scale"], shape=len(channels))
                intercept = pm.Normal("intercept", 0, 1)
                trend = pm.Normal("trend", 0, 0.5)
                season = pm.Normal("season", 0, 0.5, shape=2)
                sigma = pm.HalfNormal("sigma", 1)
                lag_weights = alpha[:, None] ** np.arange(current_lag_count)[None, :]
                adstock = pt.sum(
                    variant_lagged * lag_weights[None, :, :], axis=2
                ) / pt.sum(lag_weights, axis=1)[None, :]
                contribution = beta[None, :] * adstock / (half[None, :] + adstock)
                mu = intercept + trend * time_axis + pt.dot(seasonal, season) + pt.sum(contribution, axis=1)
                if control_matrix is not None:
                    control_beta = pm.Normal("macro_control_beta", 0, 0.5, shape=len(controls))
                    mu = mu + pt.dot(control_matrix, control_beta)
                pm.Deterministic("expected_outcome", ymean + yscale * mu)
                pm.Deterministic("channel_contribution", yscale * contribution)
                pm.Normal("observed", mu=mu[:split], sigma=sigma, observed=(y[:split] - ymean) / yscale)

                def variant_callback(trace, draw):
                    _check_cancelled(is_cancelled)

                trace = pm.sample(
                    draws=500,
                    tune=750,
                    chains=4,
                    cores=1,
                    random_seed=variant["seed"],
                    target_accept=0.95,
                    progressbar=False,
                    compute_convergence_checks=True,
                    nuts_sampler="pymc",
                    callback=variant_callback if is_cancelled is not None else None,
                )
        except BaseException as exc:
            if is_cancelled is not None and is_cancelled():
                raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED_DURING_MMM_SENSITIVITY") from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            completed.append({
                **variant,
                "state": "FAILED",
                "reason": "SENSITIVITY_FIT_FAILED:" + type(exc).__name__,
                "diagnostics": None,
                "holdout": None,
                "budget_scenario": None,
            })
            continue
        _check_cancelled(is_cancelled)
        parameters = ["adstock_alpha", "half_saturation", "channel_beta", "intercept", "trend", "season", "sigma"]
        if control_matrix is not None:
            parameters.append("macro_control_beta")
        summary = az.summary(trace, var_names=parameters, round_to=None)
        sizes = {
            name: (
                len(channels) if name in {"adstock_alpha", "half_saturation", "channel_beta"}
                else 2 if name == "season"
                else len(controls) if name == "macro_control_beta"
                else None
            )
            for name in parameters
        }
        diagnostics = _mmm_diagnostics(summary, sizes)
        divergences = int(np.asarray(trace.sample_stats["diverging"]).sum())
        sampling = {
            "library": "pymc",
            "version": pm.__version__,
            "chains": 4,
            "cores": 1,
            "draws_per_chain": 500,
            "tune_per_chain": 750,
            "seed": variant["seed"],
            "target_accept": 0.95,
            "divergences": divergences,
            **diagnostics,
        }
        record = {
            **variant,
            "alpha_prior": [alpha_a, alpha_b],
            "prior": {
                "adstock_alpha": f"Beta({alpha_a},{alpha_b})",
                "half_saturation": f"LogNormal(0,{variant['half_sigma']})",
                "channel_beta": f"HalfNormal({variant['beta_scale']})",
            },
            "sampling": sampling,
            "diagnostics": diagnostics,
        }
        if diagnostics["diagnostic_failures"] or divergences:
            record.update(
                state="INSUFFICIENT",
                reason="SENSITIVITY_POSTERIOR_DIAGNOSTICS_NOT_PASSED",
                holdout=None,
                budget_scenario=None,
            )
            completed.append(record)
            continue

        def variant_draws(name: str):
            array = np.asarray(trace.posterior[name])
            return array.reshape((-1, *array.shape[2:]))

        expected = variant_draws("expected_outcome")
        sigmas = variant_draws("sigma") * yscale
        rng = np.random.default_rng(variant["seed"])
        predictive = expected + rng.normal(size=expected.shape) * sigmas[:, None]
        predictions = expected.mean(axis=0)
        intervals = np.quantile(predictive, [0.025, 0.975], axis=0).T
        errors = predictions[split:] - y[split:]
        holdout = {
            "periods": period_labels[split:],
            "mae": float(np.mean(np.abs(errors))),
            "rmse": float(np.sqrt(np.mean(errors ** 2))),
            "predictive_interval_coverage": float(np.mean(
                (y[split:] >= intervals[split:, 0]) & (y[split:] <= intervals[split:, 1])
            )),
            "nominal_coverage": 0.95,
            "conditional_on_observed_holdout_covariates": True,
        }
        budget_scenario = None
        if budget is not None:
            betas = variant_draws("channel_beta") * yscale
            halves = variant_draws("half_saturation")

            def utility(amount):
                normalized = amount / scale
                return np.mean(np.sum(betas * normalized / (halves + normalized), axis=1))

            initial = lower + (budget["total"] - lower.sum()) * (upper - lower) / max(1e-12, (upper - lower).sum())
            optimized = minimize(
                lambda amount: -utility(amount),
                initial,
                method="SLSQP",
                bounds=list(zip(lower, upper)),
                constraints={"type": "eq", "fun": lambda amount: np.sum(amount) - budget["total"]},
                options={"maxiter": 500, "ftol": 1e-9},
            )
            _check_cancelled(is_cancelled)
            if optimized.success and abs(float(np.sum(optimized.x)) - budget["total"]) <= 1e-5:
                budget_scenario = {
                    "state": "READY",
                    "currency": currency,
                    "allocation": {channel: float(value) for channel, value in zip(channels, optimized.x)},
                    "objective": float(utility(optimized.x)),
                }
            else:
                budget_scenario = {"state": "INSUFFICIENT", "reason": "OPTIMIZATION_NOT_CONVERGED"}
        record.update(state="READY", holdout=holdout, budget_scenario=budget_scenario)
        completed.append(record)
    states = {item["state"] for item in completed}
    overall = "READY" if states == {"READY"} else "PARTIAL"
    return {
        "state": overall,
        "method": "full-refit-prior-and-lag-sensitivity@1",
        "variant_count": len(completed),
        "variants": completed,
        "selection_rule": "Primary model remains the declared base specification; sensitivity fits are diagnostics and are not selected by post-treatment holdout performance.",
        "limitations": [
            "Alternative prior fits retain the same outcome/holdout split and still share the primary model's structural assumptions.",
            "Shorter-lag sensitivity changes the geometric carryover window; it does not establish the true causal lag structure.",
            "A failed sensitivity posterior is reported as incomplete rather than removed from the comparison.",
        ],
    }


def _mmm_result_kwargs(data: dict, channels: list[str]) -> dict:
    return {
        "method": "bayesian-geometric-adstock-michaelis-menten-mmm@2",
        "state": "INSUFFICIENT", "n": len(data["periods"]),
        "currency": data["currency"], "unit": data["outcome_unit"],
        "filing_entity": data["filing_entity"], "disclosure_scope": data["disclosure_scope"],
        "spend_basis": data["spend_basis"], "channels": channels,
        "channel_labels": data.get("channel_labels", {channel: channel for channel in channels}),
        "vintage_basis": data["vintage_basis"], "as_of": data["as_of"],
        "observation_frequency": data.get("observation_frequency"),
        "original_observation_count": data.get("original_observation_count"),
        "transformation": data.get("transformation"),
        "periods": data["periods"],
        "missing_quarters": data.get("missing_quarters", []),
        "missing_outcome_periods": data.get("missing_outcome_periods", []),
        "missing_spend_periods": data.get("missing_spend_periods", {}),
        "controls": data.get("controls", []),
        "source_refs": data["source_refs"],
        "input_source_refs": data["input_source_refs"],
        "roi_state": "NOT_ESTABLISHED",
    }


def _mmm_reference_location(source_ref: dict) -> str:
    title = source_ref.get("filename") or source_ref.get("title") or "已保存原件"
    row_ref = source_ref.get("row_ref")
    if isinstance(row_ref, str):
        generated_row = re.search(r"(?:^|:)row:(\d+)\Z", row_ref)
        if generated_row:
            sheet = source_ref.get("sheet")
            sheet_label = f"工作表 {sheet}，" if isinstance(sheet, str) and sheet else ""
            return f"{title}；{sheet_label}原件第{generated_row.group(1)}行"
    location = source_ref.get("locator") or source_ref.get("cell") or row_ref
    return f"{title}；{location}" if location is not None else str(title)


def _mmm_source_label(source_ref: Any, channel_labels: dict | None = None) -> str:
    if not isinstance(source_ref, dict) or not source_ref:
        return "来源引用缺失"
    labels = []
    for role, caption in (("outcome", "结果"), ("spend", "投入")):
        value = source_ref.get(role)
        if role == "spend" and isinstance(value, dict) and not any(
            key in value for key in ("filename", "title", "material_id", "row_ref", "locator", "cell")
        ):
            rows = [{"channel": channel, **row} for channel, row in value.items() if isinstance(row, dict)]
        else:
            rows = value if isinstance(value, list) else [value]
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel = row.get("channel")
            channel_label = (channel_labels or {}).get(channel, channel)
            role_label = f" {channel_label}" if channel_label else ""
            labels.append(f"{caption}{role_label}:" + _mmm_reference_location(row))
    if labels:
        return " / ".join(labels)
    return _mmm_reference_location(source_ref)


def _mmm_sections(result: dict, *, diagnostics: dict | None = None, sampling: dict | None = None) -> dict:
    holdout = result.get("holdout") or {}
    channels = result.get("channels") or []
    channel_results = channels if channels and isinstance(channels[0], dict) else []
    channel_names = [item["channel"] for item in channel_results] if channel_results else channels
    channel_labels = result.get("channel_labels") or {}
    display_channel = lambda channel: channel_labels.get(channel, channel)
    budget = result.get("budget_scenario") or {}
    controls = result.get("controls") or []
    input_rows = [[
        row["period"], row["outcome"],
        *[row["spend"].get(channel) for channel in channel_names],
        _mmm_source_label(row.get("source_refs"), result.get("channel_labels")),
    ] for row in result.get("periods", [])]
    holdout_rows = []
    last_baseline = (holdout.get("benchmarks") or {}).get("last_training_value", {}).get("prediction", [])
    seasonal_baseline = (holdout.get("benchmarks") or {}).get("seasonal_naive_4_quarter", {}).get("prediction", [])
    for index, (period, actual, predicted, interval) in enumerate(zip(
        holdout.get("periods", []), holdout.get("actual", []), holdout.get("prediction", []),
        holdout.get("predictive_intervals_95", []),
    )):
        holdout_rows.append([
            period, actual, predicted, interval[0], interval[1],
            last_baseline[index] if index < len(last_baseline) else None,
            seasonal_baseline[index] if index < len(seasonal_baseline) else None,
        ])
    channel_rows = [[
        display_channel(item["channel"]), *item["mean_quarter_contribution_interval_95"], item["causal_effect"]
    ] for item in channel_results]
    budget_rows = []
    if budget.get("state") == "READY":
        for channel, amount in budget["allocation"].items():
            derivative = budget["marginal_outcome_per_currency_interval_95"][channel]
            budget_rows.append([display_channel(channel), amount, *derivative, budget["currency"]])
    diagnostic_rows = []
    if sampling is not None:
        diagnostic_rows = [[
            row["parameter"], row["r_hat"], row["ess_bulk"], row["state"]
        ] for row in sampling.get("parameter_results", [])]
    diagnostic_summary_rows = []
    if sampling is not None:
        diagnostic_summary_rows.append([
            sampling["library"], sampling["version"], sampling["chains"], sampling["draws_per_chain"],
            sampling["tune_per_chain"], sampling["divergences"], sampling.get("max_rhat"),
            sampling.get("min_ess_bulk"), "通过" if not sampling.get("diagnostic_failures") and not sampling["divergences"] else "未通过",
        ])
    macro_rows = []
    missing_input_rows = [
        ["结果", period, "所选季度来源没有该信息集内的可用实绩"]
        for period in result.get("missing_outcome_periods", [])
    ] + [
        [display_channel(channel), period, "渠道投入值缺失；未按零投入处理"]
        for channel, missing_periods in result.get("missing_spend_periods", {}).items()
        for period in missing_periods
    ] + [
        ["财务/投入序列", period, "期间缺项；未插值"]
        for period in result.get("missing_quarters", [])
    ]
    for control in controls:
        metric, region, unit = control.get("metric"), control.get("region"), control.get("unit")
        for row in control.get("periods", []):
            references = []
            for reference in row.get("source_refs", []):
                if not isinstance(reference, dict):
                    continue
                url = reference.get("source_url")
                locator = reference.get("locator")
                if url:
                    references.append(f"{url} {locator or ''}".strip())
                elif locator:
                    references.append(str(locator))
            macro_rows.append([
                metric, region, row.get("period"), row.get("state"), row.get("value"), unit,
                row.get("aggregation"), row.get("reason"), "; ".join(references),
            ])
    sections = [
        {
            "id": "financial_mmm.fit",
            "title": "营销投入与经营结果模型",
            "body": (
                f"模型状态：{result['state']}；主体：{_entity_label(result['filing_entity'])}；范围：{result['disclosure_scope']}；"
                f"期间数：{result['n']}；投入来源口径：{result['spend_basis']}；信息截止：{result['vintage_basis']}={result['as_of']}。"
                f" 频率={result.get('observation_frequency') or '未知'}；原始观察数={result.get('original_observation_count') if result.get('original_observation_count') is not None else '未知'}；期间构造={result.get('transformation') or '未知'}。"
                + (f"未运行或未采用原因：{result['reason']}。" if result.get("reason") else "")
                + "\n\n实际模型输入：\n\n" + _md_table(
                    ["季度", "结果实绩", *[f"{display_channel(channel)}投入({result['currency']})" for channel in channel_names], "来源引用"],
                    input_rows,
                )
                + ("\n\n未补齐的输入：\n\n" + _md_table(["对象", "季度", "状态"], missing_input_rows) if missing_input_rows else "")
            ),
            "data": result,
        },
        {
            "id": "financial_mmm.diagnostics",
            "title": "采样与拟合诊断",
            "body": ("贝叶斯采样与总体收敛摘要：\n\n" + _md_table(["库", "版本", "链数", "每链抽样", "每链调优", "发散", "最大R-hat", "最小bulk ESS", "诊断"], diagnostic_summary_rows) +
                     "\n\n逐参数诊断：\n\n" + _md_table(["参数", "R-hat", "bulk ESS", "状态"], diagnostic_rows)
                     if sampling else "因适用条件不足或依赖不可用，未执行采样。"),
            "data": {"sampling": sampling, "diagnostics": diagnostics},
        },
        {
            "id": "financial_mmm.holdout",
            "title": "时间外检验与渠道贡献",
            "body": (
                "预测按滚动时间切分；留出结果以当时实际投入/已解析控制项为条件，不是事前预测。\n\n" +
                _md_table(["季度", "实际结果", "模型条件预测", "95%下界", "95%上界", "末训练值基线", "季节朴素基线"], holdout_rows) +
                "\n\n误差汇总：MAE=" + _json_number_to_text(holdout.get("mae")) +
                "；MAPE=" + _json_number_to_text(holdout.get("mape_percent")) +
                ("；MAPE不可算：" + str(holdout.get("mape_reason")) if holdout.get("mape_reason") else "") +
                "；留出区间命中率=" + _json_number_to_text(holdout.get("interval_coverage_observed")) +
                "\n\n模型与朴素基线：\n\n" + _md_table(
                    ["方法", "MAE", "RMSE", "MAPE"],
                    [
                        ("MMM模型", holdout.get("mae"), holdout.get("rmse"), holdout.get("mape_percent")),
                        ("末训练值基线", (holdout.get("benchmarks") or {}).get("last_training_value", {}).get("mae"), (holdout.get("benchmarks") or {}).get("last_training_value", {}).get("rmse"), (holdout.get("benchmarks") or {}).get("last_training_value", {}).get("mape_percent")),
                        ("季节朴素基线", (holdout.get("benchmarks") or {}).get("seasonal_naive_4_quarter", {}).get("mae"), (holdout.get("benchmarks") or {}).get("seasonal_naive_4_quarter", {}).get("rmse"), (holdout.get("benchmarks") or {}).get("seasonal_naive_4_quarter", {}).get("mape_percent")),
                    ],
                ) +
                "\n\n渠道贡献区间（统计关联，不作因果解释）：\n\n" +
                _md_table(["渠道", "贡献区间下界", "中位数", "上界", "因果效果"], channel_rows)
            ) if holdout else f"没有可用的时间外结果；当前模型状态={result['state']}，原因={result.get('reason') or '未提供'}。",
            "data": {"holdout": holdout, "channel_contributions": channel_results},
        },
        {
            "id": "financial_mmm.budget_scenario",
            "title": "条件预算配置情景",
            "body": (
                f"状态：{budget.get('state')}；" +
                _md_table(["渠道", "情景分配", "边际结果下界", "中位数", "上界", "币种"], budget_rows) +
                ("\n\n" + str(budget.get("assumption")) if budget.get("assumption") else "") +
                ("\n\n未优化原因：" + str(budget.get("reason")) if budget.get("reason") else "")
            ) if budget else "未提供硬预算约束，不生成预算分配方案。",
            "data": {"budget_scenario": budget},
        },
    ]
    if controls:
        sections.append({
            "id": "financial_mmm.macro_controls",
            "title": "已解析宏观控制序列",
            "body": (
                "季度宏观控制事实及其来源定位。只把符合明确季度聚合与所选信息截止的完整值提供给模型；缺期、覆盖不完整或来源时点未知均保留原因。"
                "这些是地区背景关联，不表示宏观变量或营销投入的因果效应。\n\n"
                + _md_table(["指标", "区域", "季度", "状态", "控制值", "单位", "季度聚合", "缺失原因", "来源定位"], macro_rows)
            ),
            "data": {"controls": controls},
        })
    sensitivity = result.get("sensitivity") or {"state": "NOT_RUN", "variants": []}
    sensitivity_rows = []
    sensitivity_currency = result.get("currency") or ""
    for variant in sensitivity.get("variants", []):
        sampling_result = variant.get("sampling") or {}
        holdout_result = variant.get("holdout") or {}
        variant_budget = variant.get("budget_scenario") or {}
        allocation = variant_budget.get("allocation") or {}
        allocation_text = "; ".join(
            f"{display_channel(channel)} {amount} {sensitivity_currency}"
            for channel, amount in allocation.items()
        ) if allocation else variant_budget.get("reason")
        sensitivity_rows.append([
            variant.get("id"), variant.get("state"), variant.get("reason"),
            variant.get("lag_count"), variant.get("prior"),
            sampling_result.get("max_rhat"), sampling_result.get("min_ess_bulk"),
            sampling_result.get("divergences"), holdout_result.get("mae"),
            holdout_result.get("predictive_interval_coverage"), allocation_text,
        ])
    sections.append({
        "id": "financial_mmm.sensitivity",
        "title": "滞后窗口、先验与预算敏感性",
        "body": (
            f"整体敏感性状态：{sensitivity.get('state')}。主结果仍按预先声明的基础设定计算；变体用于检查滞后窗口和渠道先验改变后拟合、时间外误差与预算配置是否稳定，不按留出结果事后挑选方案。\n\n"
            + _md_table(
                ["变体", "状态", "原因", "滞后期数", "渠道先验", "最大R-hat", "最小bulk ESS", "发散数", "留出MAE", "留出区间覆盖", "预算分配情景"],
                sensitivity_rows,
            )
        ),
        "data": sensitivity,
    })
    return {
        "title": "营销组合与预算情景分析",
        "sections": sections,
        "limitations": [
            "结果只估计声明数据范围内的条件关联和模型情景，不证明渠道因果增量、净利润或ROI。",
            "财报披露主体不自动分配到品牌/门店；公开投入代理可能有测量误差，不能补成媒体账单。",
            "零预窗口carryover、几何衰减、非负加性渠道响应与饱和形态均为模型假设；留出预测条件于实际投入/控制变量。",
            "真实应用需独立核对源事实、独立季度观察、渠道投入变化、价格/控制项、敏感性及基线模型；诊断门槛不代表模型适用已独立证明。",
            "PyMC拟合是统计模型估计，不是自研AI训练、发布、采购或投放动作。",
        ],
    }
