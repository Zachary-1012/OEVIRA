"""Independent iXBRL source parsing and financial-chain examples."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oevira.business_analysis_economics import BusinessAnalysisCancelled, evaluate_economics


_INLINE_NS = "http://www.xbrl.org/2013/inlineXBRL"
_TRANSFORM_2015_NS = "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26"
_TRANSFORM_NS = "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16"
_GAAP_NS = "http://fasb.org/us-gaap/2025"


def _xhtml(*, html_recovery: bool = False) -> bytes:
    meta = '<meta charset="utf-8">' if html_recovery else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="{_INLINE_NS}"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:us-gaap="{_GAAP_NS}"
      xmlns:dim="urn:example:dimensions"
      xmlns:ixt15="{_TRANSFORM_2015_NS}"
      xmlns:ixt="{_TRANSFORM_NS}"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <head>{meta}<title>Isolated filing fixture</title></head>
  <body>
    <ix:header>
      <ix:references/>
      <ix:resources>
        <xbrli:context id="q1">
          <xbrli:entity>
            <xbrli:identifier scheme="https://www.sec.gov/CIK">0000001234</xbrli:identifier>
            <xbrli:segment>
              <xbrldi:explicitMember dimension="dim:RegionAxis">dim:North</xbrldi:explicitMember>
            </xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <xbrli:context id="q2">
          <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">0000001234</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <xbrli:context id="time-precision-unknown">
          <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">0000001234</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-06-30T00:00:00</xbrli:instant></xbrli:period>
        </xbrli:context>
        <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      </ix:resources>
    </ix:header>
    <p>Revenue: <ix:nonFraction id="revenue-q1" name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
       contextRef="q2" unitRef="usd" format="ixt:num-dot-decimal" scale="3" decimals="-3">1,234.5</ix:nonFraction></p>
    <ix:hidden>
      <ix:nonFraction id="zero-q1" name="us-gaap:AdvertisingExpense" contextRef="q1"
        unitRef="usd" format="ixt:fixed-zero" decimals="INF">—</ix:nonFraction>
      <ix:nonFraction id="nil-q2" name="us-gaap:Revenues" contextRef="q2" unitRef="usd"
        xsi:nil="true"/>
    </ix:hidden>
    <p><ix:nonFraction id="continued-q1" name="us-gaap:AdvertisingExpense" contextRef="q1"
       unitRef="usd" decimals="0" sign="-" continuedAt="number-tail">12<ix:exclude>999</ix:exclude></ix:nonFraction></p>
    <ix:continuation id="number-tail">34</ix:continuation>
    <ix:nonFraction id="comma-decimal" name="us-gaap:Revenues" contextRef="q1" unitRef="usd"
       format="ixt:num-comma-decimal" decimals="2">1.234,56</ix:nonFraction>
    <ix:nonFraction id="legacy-dot-decimal" name="us-gaap:Revenues" contextRef="q1" unitRef="usd"
       format="ixt15:numdotdecimal" scale="3" decimals="2">1,234.56</ix:nonFraction>
    <ix:nonFraction id="legacy-zero-dash" name="us-gaap:AdvertisingExpense" contextRef="q2" unitRef="usd"
       format="ixt15:zerodash" decimals="INF">-</ix:nonFraction>
    <ix:nonFraction id="unknown-transform" name="us-gaap:Revenue" contextRef="q1" unitRef="usd"
       format="ixt:date-year-month-day" decimals="0">2025-01-01</ix:nonFraction>
    <ix:nonFraction id="unknown-concept" name="us-gaap:CustomerCount" contextRef="q1" unitRef="usd"
       decimals="0">7</ix:nonFraction>
    <ix:nonFraction id="extension-concept" name="dim:MarketingCost" contextRef="q1" unitRef="usd"
       decimals="0">45</ix:nonFraction>
    <ix:fraction id="fraction-fact" name="us-gaap:Revenue" contextRef="q1" unitRef="usd" decimals="0">
       <ix:numerator>1</ix:numerator><ix:denominator>3</ix:denominator>
    </ix:fraction>
    <ix:nonNumeric id="narrative-fact" name="us-gaap:BusinessDescription" contextRef="q1">Narrative only</ix:nonNumeric>
    <ix:nonFraction id="missing-continuation" name="us-gaap:Revenue" contextRef="q1" unitRef="usd"
       decimals="0" continuedAt="absent-tail">100</ix:nonFraction>
    <ix:nonFraction id="missing-context" name="us-gaap:Revenue" contextRef="absent-context" unitRef="usd"
       decimals="0">100</ix:nonFraction>
    <ix:nonFraction id="date-time-context" name="us-gaap:Revenue" contextRef="time-precision-unknown" unitRef="usd"
       decimals="0">100</ix:nonFraction>
    <ix:nonFraction id="cyclic-continuation" name="us-gaap:Revenue" contextRef="q1" unitRef="usd"
       decimals="0" continuedAt="loop-a">1</ix:nonFraction>
    <ix:continuation id="loop-a" continuedAt="loop-b">2</ix:continuation>
    <ix:continuation id="loop-b" continuedAt="loop-a">3</ix:continuation>
  </body>
</html>'''.encode("utf-8")


class BusinessAnalysisFinancialInlineTests(unittest.TestCase):
    def analyze(self, raw: bytes) -> dict:
        return evaluate_economics("financial_source_analysis", {
            "source_format": "inline-xbrl",
            "raw_source": raw,
            "source": {
                "material_id": "material-inline-1",
                "material_version": 2,
                "material_sha256": hashlib.sha256(raw).hexdigest(),
                "filename": "quarterly-report.xhtml",
                "title": "Quarterly report original",
                "captured_at": "2026-03-01T12:00:00+08:00",
            },
            "filing_id": "0000001234-25-000001",
            "filed": "2025-08-01",
            "form": "10-Q",
        })

    def test_xhtml_facts_apply_transform_scale_sign_and_preserve_context(self):
        raw = _xhtml()
        result = self.analyze(raw)
        sections = {row["id"]: row for row in result["sections"]}
        data = sections["financial_source.facts"]["data"]
        by_id = {row["locator"]["fact_id"]: row for row in data["facts"]}

        revenue = by_id["revenue-q1"]
        self.assertEqual(revenue["value_literal"], "1,234.5")
        self.assertEqual(revenue["value"], "1234500")
        self.assertEqual(revenue["source_scale"], "1000")
        self.assertEqual(revenue["scale_exponent"], 3)
        self.assertEqual(revenue["unit"], "USD")
        self.assertEqual(revenue["currency"], "USD")
        self.assertEqual(revenue["period_start"], "2025-04-01")
        self.assertEqual(revenue["period_end"], "2025-06-30")
        self.assertEqual(revenue["concept"], f"{{{_GAAP_NS}}}RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertEqual(revenue["source_ref"]["material_id"], "material-inline-1")
        self.assertEqual(revenue["source_ref"]["material_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(revenue["source_ref"]["filename"], "quarterly-report.xhtml")
        dimensioned_fact = by_id["continued-q1"]
        self.assertIn("{urn:example:dimensions}RegionAxis", dimensioned_fact["dimensions"])
        self.assertIn("{urn:example:dimensions}North", dimensioned_fact["dimensions"].values())
        self.assertIn("1234500", sections["financial_source.facts"]["body"])
        self.assertIn("quarterly-report.xhtml", sections["financial_source.facts"]["body"])
        self.assertIn("2025-08-01", sections["financial_source.facts"]["body"])
        self.assertIn("XBRL", sections["financial_source.missing"]["body"])

    def test_hidden_zero_nil_and_continuation_keep_distinct_values(self):
        result = self.analyze(_xhtml())
        facts = result["sections"][0]["data"]["facts"]
        by_id = {row["locator"]["fact_id"]: row for row in facts}

        self.assertEqual(by_id["zero-q1"]["value"], "0")
        self.assertEqual(by_id["zero-q1"]["status"], "DISCLOSED")
        self.assertFalse(by_id["zero-q1"]["inline_visible"])
        self.assertEqual(by_id["nil-q2"]["value"], None)
        self.assertEqual(by_id["nil-q2"]["status"], "MISSING")
        self.assertEqual(by_id["continued-q1"]["value_literal"], "1234")
        self.assertEqual(by_id["continued-q1"]["value"], "-1234")
        self.assertEqual(by_id["continued-q1"]["continuation_ids"], ["number-tail"])
        self.assertEqual(by_id["comma-decimal"]["value"], "1234.56")
        self.assertEqual(by_id["legacy-dot-decimal"]["value"], "1234560")
        self.assertEqual(by_id["legacy-dot-decimal"]["source_scale"], "1000")
        self.assertEqual(by_id["legacy-zero-dash"]["value"], "0")

    def test_unknown_transforms_concepts_and_missing_continuations_remain_individual_candidates(self):
        result = self.analyze(_xhtml())
        candidates = result["sections"][1]["data"]["candidates"]
        by_id = {row["locator"]["fact_id"]: row for row in candidates}

        self.assertEqual(by_id["unknown-transform"]["reason"], "INLINE_TRANSFORMATION_UNSUPPORTED")
        self.assertEqual(by_id["unknown-concept"]["reason"], "UNSUPPORTED_FINANCIAL_CONCEPT")
        self.assertEqual(by_id["extension-concept"]["reason"], "UNSUPPORTED_TAXONOMY")
        self.assertEqual(by_id["fraction-fact"]["reason"], "INLINE_FRACTION_NOT_ADAPTED")
        self.assertEqual(by_id["narrative-fact"]["reason"], "NON_NUMERIC_INLINE_FACT_REQUIRES_REVIEW")
        self.assertEqual(by_id["missing-continuation"]["reason"], "INLINE_CONTINUATION_NOT_FOUND")
        self.assertEqual(by_id["missing-context"]["reason"], "INLINE_CONTEXT_NOT_FOUND")
        self.assertEqual(by_id["date-time-context"]["reason"], "CONTEXT_PERIOD_PRECISION_OR_VALUE_UNSUPPORTED")
        self.assertEqual(by_id["cyclic-continuation"]["reason"], "CYCLIC_INLINE_CONTINUATION")
        self.assertIn("数值转换规则未实现", result["sections"][1]["body"])
        self.assertNotIn("INLINE_", result["sections"][1]["body"])
        self.assertTrue(any(row["metric"] == "marketing_expense" and row["status"] == "MISSING" for row in result["sections"][2]["data"]["missing"]))

    def test_parsed_inline_revenue_can_feed_filed_quarter_selection(self):
        source_result = self.analyze(_xhtml())
        facts = source_result["sections"][0]["data"]["facts"]
        revenue = next(row for row in facts if row["metric"] == "revenue")
        selected = evaluate_economics("financial_quarterly_series", {
            "facts": [revenue],
            "metric": "revenue",
            "unit": "USD",
            "filing_entity": revenue["filing_entity"],
            "disclosure_scope": "filing_entity",
            "dimensions": {},
            "statement_basis": revenue["statement_basis"],
            "vintage_basis": "filed",
            "as_of": "2025-08-02",
            "periods": ["2025-Q2"],
        })
        period = selected["sections"][0]["data"]["periods"][0]
        self.assertEqual(period["value"], "1234500")
        self.assertEqual(period["state"], "AVAILABLE")
        self.assertIn("1234500", selected["sections"][0]["body"])
        self.assertNotIn("FIN-", selected["sections"][0]["body"])

    def test_sec_style_html_recovery_keeps_source_location_and_numeric_value(self):
        raw = _xhtml(html_recovery=True)
        result = self.analyze(raw)
        facts = result["sections"][0]["data"]["facts"]
        revenue = next(row for row in facts if row["locator"]["fact_id"] == "revenue-q1")

        self.assertEqual(result["sections"][2]["data"]["parse_mode"], "html-recovery")
        self.assertEqual(revenue["value"], "1234500")
        self.assertGreater(revenue["locator"]["line"], 0)
        recovery_count = result["sections"][2]["data"]["html_recovery_issue_count"]
        self.assertGreater(recovery_count, 0)
        self.assertTrue(any(str(recovery_count) in line for line in result["sections"][2]["data"]["warnings"]))

    def test_entities_are_rejected_and_cancellation_is_propagated(self):
        malicious = b'<!DOCTYPE html [<!ENTITY secret SYSTEM "file:///C:/secret">]><html/>'
        with self.assertRaises(ValueError):
            self.analyze(malicious)
        with self.assertRaises(BusinessAnalysisCancelled):
            evaluate_economics("financial_source_analysis", {
                "source_format": "inline-xbrl",
                "raw_source": _xhtml(),
                "filing_id": "filing-1",
            }, is_cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
