"""Source-backed financial adapter and vintage-selection regressions."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oevira.business_analysis_economics import evaluate_economics


class BusinessAnalysisFinancialTests(unittest.TestCase):
    def source(self, raw: bytes) -> dict:
        return {
            "material_id": "material-1",
            "material_version": 3,
            "material_sha256": hashlib.sha256(raw).hexdigest(),
            "captured_at": "2026-03-01T12:00:00+08:00",
        }

    def sample_fact(self, identity, value, filed, captured_at, quarter="2025-Q1", **changes):
        start, end = {
            "2025-Q1": ("2025-01-01", "2025-03-31"),
            "2025-Q2": ("2025-04-01", "2025-06-30"),
            "2025-Q3": ("2025-07-01", "2025-09-30"),
        }[quarter]
        fact = {
            "observation_id": identity,
            "metric": "revenue",
            "concept": "us-gaap:Revenue",
            "value": str(value) if value is not None else None,
            "unit": "USD",
            "currency": "USD",
            "period_start": start,
            "period_end": end,
            "period_kind": "quarter",
            "filing_entity": {"name": "Sample Corp", "scheme": "SEC-CIK", "identifier": "0000001234"},
            "filing_id": f"0000001234-25-{identity[-6:]}",
            "filed": filed,
            "captured_at": captured_at,
            "form": "10-Q",
            "disclosure_scope": "filing_entity",
            "dimensions": {},
            "statement_basis": "standard-taxonomy; statement-vs-note context not provided by SEC API",
            "source_scale": "1",
            "locator": {"unit": "json-pointer", "pointer": f"/facts/{identity}"},
            "status": "MISSING" if value is None else "DISCLOSED",
            "fact_verification": "SOURCE_ASSERTION_NOT_INDEPENDENTLY_VERIFIED",
            "source_ref": {"material_id": "material-1", "material_sha256": "a" * 64},
        }
        fact.update(changes)
        return fact

    def mmm_inputs(self, n: int = 32) -> dict:
        rows = []
        spend_values = [300 + 55 * (index % 7) + 18 * index for index in range(n)]
        mean_spend = sum(spend_values[: n - max(4, int(n * 0.2))]) / (n - max(4, int(n * 0.2)))
        lag = []
        alpha, half, beta = 0.4, 0.55, 0.8
        for index, spend in enumerate(spend_values):
            lag.append(spend / mean_spend)
            weights = [alpha ** offset for offset in range(min(8, index + 1))]
            prior = [lag[index - offset] for offset in range(len(weights))]
            adstock = sum(value * weight for value, weight in zip(prior, weights)) / sum(weights)
            contribution = beta * adstock / (half + adstock)
            time = (index - ((n - max(4, int(n * 0.2))) - 1) / 2) / (n - max(4, int(n * 0.2)))
            season = 0.2 * math.sin(2 * math.pi * index / 4) + 0.1 * math.cos(2 * math.pi * index / 4)
            noise = ((index * 7) % 5 - 2) * 2.5
            outcome = 2000 + 700 * (0.1 + 0.02 * time + season + contribution) + noise
            serial = 2018 * 4 + index
            quarter = serial % 4 + 1
            year = serial // 4
            rows.append({
                "period": f"{year}-Q{quarter}",
                "outcome": round(outcome, 4),
                "spend": {"online_video": spend},
                "source_refs": {
                    "outcome": {"material_id": "synthetic-outcome", "row_ref": f"result-{index + 1}"},
                    "spend": {"online_video": {"material_id": "synthetic-spend", "row_ref": f"spend-{index + 1}"}},
                },
            })
        return {
            "periods": rows,
            "currency": "CNY",
            "outcome_unit": "CNY",
            "filing_entity": {"name": "样例公司", "scheme": "TEST", "identifier": "issuer-1"},
            "disclosure_scope": "filing_entity",
            "spend_basis": "reported",
            "observation_frequency": "quarter",
            "original_observation_count": n,
            "transformation": "reported",
            "vintage_basis": "filed",
            "as_of": "2026-02-01",
            "source_refs": {"basis": "synthetic source-linked quarterly facts"},
            "budget": {"total": 1000, "lower": {"online_video": 0}, "upper": {"online_video": 2000}},
        }

    def _nbs_month_release(self, month: str, year: int, amount: int, cumulative: int):
        month_number = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12,
        }[month]
        published_month = month_number + 1
        published_year = year + (1 if published_month == 13 else 0)
        published_month = 1 if published_month == 13 else published_month
        published_day = 16
        raw = (
            f"Total Retail Sales of Consumer Goods in {month} {year}\n"
            f"{published_year}-{published_month:02d}-{published_day:02d} 10:00\n"
            f"In {month}, the total retail sales of consumer goods reached {amount} billion yuan.\n"
            f"From January to {month}, the total retail sales of consumer goods reached {cumulative} billion yuan."
        )
        source_url = (
            f"https://www.stats.gov.cn/english/PressRelease/"
            f"{published_year}{published_month:02d}/t{published_year}{published_month:02d}{published_day:02d}_1000000.html"
        )
        encoded = raw.encode("utf-8")
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "macro-release",
            "raw_source": raw,
            "source_url": source_url,
            "source": self.source(encoded),
        })
        section = next(row for row in result["sections"] if row["id"] == "financial_source.macro_facts")
        return result, section["data"]["facts"]

    def test_saved_nbs_macro_release_retains_monthly_and_cumulative_facts(self):
        result, facts = self._nbs_month_release("March", 2025, 3900, 10000)
        monthly = next(row for row in facts if row["period_kind"] == "month")
        cumulative = next(row for row in facts if row["period_kind"] == "cumulative")
        self.assertEqual(monthly["metric"], "retail_sales")
        self.assertEqual(monthly["value"], "3900000000000")
        self.assertEqual(monthly["period_start"], "2025-03-01")
        self.assertEqual(cumulative["value"], "10000000000000")
        self.assertEqual(cumulative["period_start"], "2025-01-01")
        self.assertEqual(monthly["captured_at"], "2026-03-01T12:00:00+08:00")
        finance_section = next(row for row in result["sections"] if row["id"] == "financial_source.facts")
        self.assertEqual(finance_section["data"]["facts"], [])
        self.assertIn("3900000000000", result["sections"][1]["body"])
        self.assertIn("10000000000000", result["sections"][1]["body"])
        self.assertIn("characters", result["sections"][1]["body"])
        self.assertTrue(any("not independently verified" in item.lower() for item in result["limitations"]))
        with self.assertRaises(ValueError):
            evaluate_economics("financial_source_analysis", {
                "source_format": "macro-release", "raw_source": "March 2025 retail",
                "source_url": "https://example.com/english/PressRelease/202503/t20250416_1000000.html",
            })

    def test_macro_quarter_alignment_sums_three_latest_months_and_keeps_conflicts_unknown(self):
        from oevira.business_analysis_financial import resolve_macro_controls

        facts = []
        for month, amount, cumulative in (
            ("January", 300, 300),
            ("February", 350, 650),
            ("March", 400, 1050),
        ):
            _, rows = self._nbs_month_release(month, 2025, amount, cumulative)
            facts.extend(rows)
        aligned = resolve_macro_controls(
            facts,
            metric="retail_sales",
            region="CN-national",
            periods=["2025-Q1"],
            vintage_basis="filed",
            as_of="2025-04-30",
        )
        self.assertEqual(aligned["periods"][0]["value"], "1050000000000")
        self.assertEqual(aligned["periods"][0]["aggregation"], "sum-complete-monthly-totals")
        self.assertEqual(len(aligned["periods"][0]["source_refs"]), 3)
        reverse_order = resolve_macro_controls(
            list(reversed(facts)),
            metric="retail_sales",
            region="CN-national",
            periods=["2025-Q1"],
            vintage_basis="filed",
            as_of="2025-04-30",
        )
        self.assertEqual(reverse_order["periods"][0]["state"], "AVAILABLE")
        self.assertEqual(reverse_order["periods"][0]["value"], "1050000000000")

        march = next(row for row in facts if row["period_start"] == "2025-03-01" and row["period_kind"] == "month")
        conflict = {**march, "value": "9999999999999", "source_ref": {**march["source_ref"], "material_id": "m-conflict"}}
        conflicted = resolve_macro_controls(
            [*facts, conflict], metric="retail_sales", region="CN-national",
            periods=["2025-Q1"], vintage_basis="filed", as_of="2025-04-30",
        )
        self.assertIsNone(conflicted["periods"][0]["value"])
        self.assertEqual(conflicted["periods"][0]["state"], "MISSING")
        self.assertEqual(conflicted["periods"][0]["reason"], "CONFLICTING_AS_OF_VINTAGES")

    def test_macro_cpi_yoy_keeps_negative_months_and_arithmetic_mean(self):
        from oevira.business_analysis_financial import resolve_macro_controls

        facts = []
        for month, release_month, wording in (
            ("January", "02", "decreased by 1.5"),
            ("February", "03", "decreased by 0.5"),
            ("March", "04", "increased by 0.1"),
        ):
            raw = (
                f"Consumer Price Index for {month} 2025\n"
                f"2025-{release_month}-16 10:00\n"
                f"In {month} 2025, the national consumer price index (CPI) {wording} percent year on year."
            )
            result = evaluate_economics("financial_source_analysis", {
                "source_format": "macro-release",
                "raw_source": raw,
                "source_url": f"https://www.stats.gov.cn/english/PressRelease/2025{release_month}/t2025{release_month}16_1000000.html",
                "source": self.source(raw.encode("utf-8")),
            })
            facts.extend(next(row["data"]["facts"] for row in result["sections"] if row["id"] == "financial_source.macro_facts"))
        aligned = resolve_macro_controls(
            facts, metric="cpi_yoy_percent", region="CN-national", periods=["2025-Q1"],
            vintage_basis="filed", as_of="2025-04-30",
        )
        quarter = aligned["periods"][0]
        self.assertEqual(quarter["state"], "AVAILABLE")
        self.assertEqual(quarter["aggregation"], "arithmetic-mean-monthly-yoy-percent")
        self.assertAlmostEqual(float(quarter["value"]), (-1.5 - 0.5 + 0.1) / 3, places=12)

    def test_mct_release_uses_published_quarter_total_without_splitting(self):
        raw = (
            "2026年二季度国内出游数据情况\n"
            "发布时间：2026-07-30 12:00\n"
            "根据国内出游抽样调查统计结果，2026年二季度，国内出游人次18.0亿，"
            "国内居民出游总花费1.9万亿元。"
        )
        source_url = "https://zwgk.mct.gov.cn/zfxxgkml/tjxx/202607/t20260730_1000001.html"
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "macro-release", "raw_source": raw, "source_url": source_url,
            "source": self.source(raw.encode("utf-8")),
        })
        facts = next(row["data"]["facts"] for row in result["sections"] if row["id"] == "financial_source.macro_facts")
        trips = next(row for row in facts if row["metric"] == "domestic_tourism_trips")
        spend = next(row for row in facts if row["metric"] == "domestic_tourism_spend")
        self.assertEqual(trips["period_kind"], "quarter")
        self.assertEqual(trips["period_label"], "2026-Q2")
        self.assertEqual(trips["value"], "1800000000.0")
        self.assertEqual(spend["value"], "1900000000000.0")
        self.assertEqual(spend["published_literal"], "2026-07-30 12:00")
        from oevira.business_analysis_financial import resolve_macro_controls
        aligned = resolve_macro_controls(
            facts, metric="domestic_tourism_spend", region="CN-national", periods=["2026-Q2"],
            vintage_basis="filed", as_of="2026-08-01",
        )
        self.assertEqual(aligned["periods"][0]["value"], "1900000000000")
        self.assertEqual(aligned["periods"][0]["aggregation"], "explicit-reported-quarter-total")

    def test_beijing_holiday_footfall_is_not_recast_as_a_quarter_total(self):
        raw = (
            "发布时间：2025-04-08 09:00\n"
            "清明假期本市消费市场活跃有序\n"
            "2025年清明假期，全市6个重点商圈客流量90万人次，假期三天。"
        )
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "macro-release", "raw_source": raw,
            "source_url": "https://sw.beijing.gov.cn/zwxx/swxx/202504/t20250404_1000001.html",
            "source": self.source(raw.encode("utf-8")),
        })
        facts = next(row["data"]["facts"] for row in result["sections"] if row["id"] == "financial_source.macro_facts")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["metric"], "monitored_district_footfall")
        self.assertEqual(facts[0]["period_kind"], "holiday")
        self.assertEqual(facts[0]["duration_days"], 3)
        self.assertEqual(facts[0]["region"], "CN-Beijing")
        from oevira.business_analysis_financial import resolve_macro_controls
        aligned = resolve_macro_controls(
            facts, metric="monitored_district_footfall", region="CN-Beijing", periods=["2025-Q2"],
            vintage_basis="filed", as_of="2025-04-30",
        )
        self.assertIsNone(aligned["periods"][0]["value"])
        self.assertEqual(aligned["periods"][0]["state"], "MISSING")

    def test_api_reads_saved_macro_artifacts_and_table_for_mmm_without_direct_resolved_values(self):
        import io

        from test_business_execution import BusinessExecutionTests

        case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        case.setUp()
        try:
            macro_sources = []
            for month, amount, cumulative in (
                ("January", 300, 300),
                ("February", 350, 650),
                ("March", 400, 1050),
            ):
                # Reuse the exact finite-release parser input, then persist it as a
                # retained Material and invoke the real business-result route.
                month_number = {"January": 1, "February": 2, "March": 3}[month]
                publication_month = month_number + 1
                raw = (
                    f"Total Retail Sales of Consumer Goods in {month} 2025\n"
                    f"2025-{publication_month:02d}-16 10:00\n"
                    f"In {month}, the total retail sales of consumer goods reached {amount} billion yuan.\n"
                    f"From January to {month}, the total retail sales of consumer goods reached {cumulative} billion yuan."
                )
                source_url = f"https://www.stats.gov.cn/english/PressRelease/2025{publication_month:02d}/t2025{publication_month:02d}16_1000000.html"
                material = case.post("/api/materials", {
                    "title": f"NBS {month} 2025 release",
                    "filename": f"nbs-2025-{publication_month:02d}.txt",
                    "text": raw,
                    "work_id": case.work["id"],
                })
                source_result = case.post(f"/api/works/{case.work['id']}/business-results", {
                    "operation": "financial_source_analysis",
                    "inputs": {
                        "source_format": "macro-release",
                        "source": {"material_id": material["id"], "material_sha256": material["sha256"]},
                        "source_url": source_url,
                    },
                    "citations": [],
                })
                macro_sources.append({
                    "artifact_id": source_result["id"],
                    "artifact_version": source_result["version"],
                })

            periods = []
            serial = 2025 * 4
            for index in range(16):
                ordinal = serial + index
                periods.append(f"{ordinal // 4}-Q{ordinal % 4 + 1}")
            csv_rows = ["quarter,outcome,online_video_spend"]
            for index, period in enumerate(periods):
                csv_rows.append(f"{period},{1000 + index * 25},{100 + index * 7}")
            upload = case.request(
                "post",
                "/api/materials",
                data={
                    "file": (io.BytesIO("\n".join(csv_rows).encode("utf-8")), "mmm-quarterly.csv"),
                    "title": "经营与渠道季度原表",
                    "work_id": case.work["id"],
                },
            )
            self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
            table = upload.json["data"]
            preview = case.request(
                "get",
                f"/api/materials/{table['id']}/business-table?work_id={case.work['id']}",
            )
            self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
            source_refs = {
                "material_id": table["id"],
                "material_sha256": preview.json["data"]["sha256"],
                "filename": table["filename"],
            }
            result = case.post(f"/api/works/{case.work['id']}/business-results", {
                "operation": "financial_mmm_analysis",
                "inputs": {
                    "periods": [],
                    "currency": "CNY",
                    "outcome_unit": "CNY",
                    "filing_entity": {"name": "样例主体", "scheme": "TEST", "identifier": "issuer-1"},
                    "disclosure_scope": "filing_entity",
                    "spend_basis": "user_declared",
                    "observation_frequency": "quarter",
                    "original_observation_count": len(periods),
                    "transformation": "reported",
                    "vintage_basis": "filed",
                    "as_of": "2025-04-30",
                    "region": "CN-national",
                    "source_refs": source_refs,
                    "channel_labels": {"online_video": "在线视频"},
                    "macro_source": {
                        "artifacts": macro_sources,
                        "metric": "retail_sales",
                        "region": "CN-national",
                    },
                },
                "tables": [{
                    "material_id": table["id"],
                    "material_sha256": preview.json["data"]["sha256"],
                    "target": "periods",
                    "source_row_field": "source_refs.row_ref",
                    "columns": {
                        "period": {"column": "quarter", "type": "text"},
                        "outcome": {"column": "outcome", "type": "decimal"},
                        "spend.online_video": {"column": "online_video_spend", "type": "decimal"},
                    },
                    "row_defaults": {"source_refs": source_refs},
                }],
                "citations": [],
            })
            business = result["input_snapshot"]["business"]
            self.assertNotIn("_resolved_macro_controls", business["inputs"])
            self.assertEqual(business["inputs"]["macro_source"]["artifacts"], macro_sources)
            macro_section = next(
                section for section in business["result"]["sections"]
                if section["id"] == "financial_mmm.macro_controls"
            )
            controls = macro_section["data"]["controls"]
            first = controls[0]["periods"][0]
            self.assertEqual(first["period"], "2025-Q1")
            self.assertEqual(first["state"], "AVAILABLE")
            self.assertEqual(first["value"], "1050000000000")
            self.assertEqual(first["aggregation"], "sum-complete-monthly-totals")
            self.assertEqual(controls[0]["periods"][1]["state"], "MISSING")
            model = next(
                section["data"] for section in business["result"]["sections"]
                if section["id"] == "financial_mmm.fit"
            )
            self.assertEqual(model["state"], "INSUFFICIENT")
            self.assertEqual(model["reason"], "MACRO_CONTROL_SERIES_INCOMPLETE_AT_SELECTED_VINTAGE")
            self.assertNotIn("sampling", model)
        finally:
            case.tearDown()

    def test_sec_companyfacts_preserves_filing_versions_and_source_pointers(self):
        payload = {
            "cik": "1234",
            "entityName": "Sample Corp",
            "facts": {
                "us-gaap": {
                    "AdvertisingExpense": {
                        "units": {"USD": [
                            {"val": 1200, "start": "2025-01-01", "end": "2025-12-31", "accn": "0000001234-26-000001", "filed": "2026-02-20", "form": "10-K", "fy": 2025, "fp": "FY"},
                            {"val": 1000, "start": "2025-01-01", "end": "2025-12-31", "accn": "0000001234-26-000002", "filed": "2026-03-02", "form": "10-K/A", "fy": 2025, "fp": "FY"},
                        ]},
                    }
                }
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "sec-companyfacts", "raw_source": raw, "source": self.source(raw),
        })
        facts = result["sections"][0]["data"]["facts"]
        self.assertEqual(len(facts), 2)
        self.assertEqual([row["value"] for row in facts], ["1200", "1000"])
        self.assertTrue(all(row["filing_entity"]["identifier"] == "0000001234" for row in facts))
        self.assertTrue(all(row["locator"]["unit"] == "json-pointer" for row in facts))
        self.assertEqual(facts[1]["filing_id"], "0000001234-26-000002")
        self.assertEqual(facts[0]["captured_at"], "2026-03-01T12:00:00+08:00")
        self.assertIn("1200", result["sections"][0]["body"])
        self.assertIn("1000", result["sections"][0]["body"])
        self.assertIn("2026-02-20", result["sections"][0]["body"])
        self.assertFalse(result["sections"][2]["data"]["source"]["canonical_fact_promotion"])

    def test_companyconcept_and_duplicate_json_keys_are_explicit(self):
        payload = {
            "cik": "1234", "entityName": "Sample Corp", "taxonomy": "us-gaap",
            "tag": "Revenue", "units": {"USD": [
                {"val": 12, "end": "2025-03-31", "accn": "0000001234-25-000001", "filed": "2025-05-01", "form": "10-Q"}
            ]},
        }
        raw = json.dumps(payload).encode("utf-8")
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "sec-companyconcept", "raw_source": raw, "source": self.source(raw),
        })
        fact = result["sections"][0]["data"]["facts"][0]
        self.assertEqual(fact["period_kind"], "instant")
        self.assertEqual(fact["locator"]["pointer"], "/units/USD/0")
        duplicate = b'{"cik":"1234","cik":"9999"}'
        with self.assertRaises(ValueError):
            evaluate_economics("financial_source_analysis", {
                "source_format": "sec-companyfacts", "raw_source": duplicate,
            })

    def test_xbrl_instance_retains_entity_dimensions_units_and_nil(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:us-gaap="http://fasb.org/us-gaap/2025"
 xmlns:ex="http://example.com/taxonomy">
 <xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="https://sec.gov/CIK">0000001234</xbrli:identifier>
 <xbrli:segment><xbrldi:explicitMember dimension="ex:ProductAxis">ex:CafeMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
 <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="c2"><xbrli:entity><xbrli:identifier scheme="https://sec.gov/CIK">0000001234</xbrli:identifier></xbrli:entity>
 <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:unit id="u1"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
 <us-gaap:AdvertisingExpense id="fact-1" contextRef="c1" unitRef="u1" decimals="-3">120000</us-gaap:AdvertisingExpense>
 <us-gaap:Revenue id="fact-2" contextRef="c2" unitRef="u1" xsi:nil="true"/>
</xbrli:xbrl>'''
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "xbrl-instance", "raw_source": xml,
            "filing_id": "0000001234-25-000001", "filed": "2025-05-01", "form": "10-Q",
        })
        facts = result["sections"][0]["data"]["facts"]
        self.assertEqual(facts[0]["value"], "120000")
        self.assertEqual(facts[0]["currency"], "USD")
        self.assertEqual(facts[0]["dimensions"]["{http://example.com/taxonomy}ProductAxis"], "{http://example.com/taxonomy}CafeMember")
        self.assertEqual(facts[1]["status"], "MISSING")
        self.assertIsNone(facts[1]["value"])

    def test_xbrl_disallows_dtd_and_external_entity_declarations(self):
        xml = b'<!DOCTYPE xbrl [<!ENTITY x SYSTEM "file:///C:/secret">]><xbrl>&x;</xbrl>'
        with self.assertRaises(ValueError):
            evaluate_economics("financial_source_analysis", {
                "source_format": "xbrl-instance", "raw_source": xml, "filing_id": "filing-1",
            })

    def test_disclosure_annotation_applies_wan_multiplier_and_keeps_zero_vs_missing(self):
        text = "2025年营销费用12万元，门店客流0人，出租率—。"
        annotations = []
        for metric, literal, unit in (
            ("marketing_expense", "12", "CNY"),
            ("footfall", "0", "people"),
            ("occupancy", "—", "%"),
        ):
            start = text.index(literal, text.index({"marketing_expense": "营销费用", "footfall": "门店客流", "occupancy": "出租率"}[metric]))
            end = start + len(literal)
            numeric = None if literal == "—" else int(literal) * (10000 if metric == "marketing_expense" else 1)
            annotations.append({
                "metric": metric, "value": numeric, "value_literal": literal,
                "scale": 10000 if metric == "marketing_expense" else 1,
                "unit": unit, "period_start": "2025-01-01", "period_end": "2025-12-31",
                "filing_entity": {"name": "Sample Corp", "scheme": "SEC-CIK", "identifier": "0000001234"},
                "filing_id": "0000001234-26-000001", "filed": "2026-02-01", "form": "10-K",
                "disclosure_scope": "filing_entity", "statement_basis": "注释披露",
                "start": start, "end": end,
            })
        raw = text
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "disclosure-extract", "raw_source": raw, "annotations": annotations,
        })
        facts = result["sections"][0]["data"]["facts"]
        by_metric = {row["metric"]: row for row in facts}
        self.assertEqual(by_metric["marketing_expense"]["value"], "120000")
        self.assertEqual(by_metric["marketing_expense"]["source_scale"], "10000")
        self.assertEqual(by_metric["footfall"]["value"], "0")
        self.assertEqual(by_metric["occupancy"]["status"], "MISSING")
        self.assertIsNone(by_metric["occupancy"]["value"])
        self.assertIn("120000", result["sections"][0]["body"])
        self.assertIn("0", result["sections"][0]["body"])

    def test_disclosure_scale_mismatch_and_wrong_source_literal_are_rejected(self):
        text = "费用12万元。"
        annotation = {
            "metric": "marketing_expense", "value": 120000, "value_literal": "12", "scale": 1000,
            "unit": "CNY", "period_start": "2025-01-01", "period_end": "2025-12-31",
            "filing_entity": {"name": "Sample Corp", "scheme": "SEC-CIK", "identifier": "0000001234"},
            "filing_id": "f-1", "filed": "2026-01-01", "form": "10-K",
            "disclosure_scope": "filing_entity", "statement_basis": "注释",
            "start": text.index("12"), "end": text.index("12") + 2,
        }
        with self.assertRaises(ValueError):
            evaluate_economics("financial_source_analysis", {
                "source_format": "disclosure-extract", "raw_source": text, "annotations": [annotation],
            })

    def test_regional_statement_explicit_wan_headers_apply_exact_multiplier(self):
        text = """广州样例股份有限公司
证券代码：600001
合并利润表
2025年度 2024年度
单位：万元
币种：人民币
营业收入 12.5 10
销售费用 (2) 0
"""
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "regional-statement", "raw_source": text,
            "exchange": "SSE", "filing_id": "filing-2025", "filed": "2026-03-01", "form": "年报",
            "locators": [{"unit": "pdf_page", "index": 1, "start": 0, "end": len(text)}],
        })
        facts = result["sections"][0]["data"]["facts"]
        revenue = next(row for row in facts if row["metric"] == "revenue" and row["period_start"] == "2025-01-01")
        selling = next(row for row in facts if row["metric"] == "selling_expense" and row["period_start"] == "2025-01-01")
        self.assertEqual(revenue["value"], "125000")
        self.assertEqual(revenue["source_scale"], "10000")
        self.assertEqual(selling["value"], "-20000")
        self.assertEqual(revenue["filing_entity"]["identifier"], "600001")
        self.assertIn("125000", result["sections"][0]["body"])
        self.assertIn("-20000", result["sections"][0]["body"])

    def test_opendart_keeps_cfs_ofs_and_accumulated_amounts_separate(self):
        payload = {
            "status": "000",
            "list": [
                {"corp_code": "00123456", "corp_name": "Sample", "rcept_no": "20260501000001", "fs_div": "CFS", "sj_div": "IS",
                 "account_nm": "매출액", "thstrm_dt": "2025.01.01 ~ 2025.03.31", "thstrm_amount": "1,234", "thstrm_add_amount": "4,000", "currency": "KRW", "reprt_code": "11013"},
                {"corp_code": "00123456", "corp_name": "Sample", "rcept_no": "20260501000002", "fs_div": "OFS", "sj_div": "IS",
                 "account_nm": "매출액", "thstrm_dt": "2025.01.01 ~ 2025.03.31", "thstrm_amount": "2,000", "currency": "KRW", "reprt_code": "11013"},
            ],
        }
        raw = json.dumps(payload).encode("utf-8")
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "opendart-json", "raw_source": raw, "fs_div": "CFS", "source": self.source(raw),
        })
        facts = result["sections"][0]["data"]["facts"]
        candidates = result["sections"][1]["data"]["candidates"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["value"], "1234")
        self.assertEqual(facts[0]["dimensions"]["financial_statement_basis"], "CFS")
        self.assertEqual(candidates[0]["reason"], "ACCUMULATED_PERIOD_NOT_EXPLICIT")
        self.assertEqual(result["sections"][2]["data"]["excluded_basis_rows"], 1)

    def test_opendart_provider_error_and_missing_period_remain_missing(self):
        denied = json.dumps({"status": "013", "list": []}).encode("utf-8")
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "opendart-json", "raw_source": denied,
        })
        self.assertEqual(result["sections"][2]["data"]["missing"][0]["reason"], "NO_DATA")
        malformed_period = {
            "status": "000",
            "list": [{"corp_code": "00123456", "corp_name": "Sample", "rcept_no": "20260501000001", "fs_div": "CFS",
                      "sj_div": "IS", "account_nm": "매출액", "thstrm_dt": "2025", "thstrm_amount": "0", "currency": "KRW"}],
        }
        result = evaluate_economics("financial_source_analysis", {
            "source_format": "opendart-json", "raw_source": json.dumps(malformed_period).encode("utf-8"),
        })
        self.assertEqual(result["sections"][0]["data"]["facts"], [])
        self.assertEqual(result["sections"][1]["data"]["candidates"][0]["reason"], "EXPLICIT_FISCAL_PERIOD_REQUIRED")

    def test_corrections_preserve_both_values_and_select_filed_or_capture_vintage(self):
        facts = [
            self.sample_fact("obs-000001", 100, "2025-04-20", "2025-04-21T12:00:00+00:00"),
            self.sample_fact("obs-000002", 90, "2025-05-20", "2025-06-02T12:00:00+00:00"),
        ]
        relation = {
            "old_observation_id": "obs-000001", "new_observation_id": "obs-000002",
            "reason": "原申报的单位换算更正", "captured_at": "2025-06-02T12:00:00+00:00",
            "source_ref": {"material_id": "amendment-material", "locator": {"pointer": "/facts/revenue"}},
        }
        before = evaluate_economics("financial_fact_corrections", {
            "facts": facts, "corrections": [relation], "vintage_basis": "filed", "as_of": "2025-05-01",
        })
        before_view = before["sections"][1]["data"]
        self.assertEqual([row["observation_id"] for row in before_view["active_facts"]], ["obs-000001"])
        after = evaluate_economics("financial_fact_corrections", {
            "facts": facts, "corrections": [relation], "vintage_basis": "filed", "as_of": "2025-06-01",
        })
        after_view = after["sections"][1]["data"]
        self.assertEqual([row["observation_id"] for row in after_view["active_facts"]], ["obs-000002"])
        self.assertEqual(after["sections"][0]["data"]["facts"][0]["value"], "100")
        self.assertIn("100", after["sections"][0]["body"])
        self.assertIn("90", after["sections"][0]["body"])
        self.assertIn("JSON /facts/obs-000001", after["sections"][0]["body"])
        self.assertNotIn("| obs-000001 |", after["sections"][0]["body"])
        capture_before = evaluate_economics("financial_fact_corrections", {
            "facts": facts, "corrections": [relation], "vintage_basis": "workspace_capture",
            "as_of": "2025-05-01T00:00:00+00:00",
        })
        self.assertEqual(capture_before["sections"][1]["data"]["active_facts"][0]["observation_id"], "obs-000001")
        self.assertIn("JSON /facts/obs-000001", capture_before["sections"][1]["body"])
        self.assertNotIn("| obs-000001 |", capture_before["sections"][1]["body"])

    def test_fact_correction_rejects_dimension_changes_and_cycles(self):
        old = self.sample_fact("obs-000001", 100, "2025-04-20", "2025-04-21T12:00:00+00:00")
        changed_dimensions = self.sample_fact(
            "obs-000002", 90, "2025-05-20", "2025-06-02T12:00:00+00:00",
            dimensions={"{urn:example}StoreAxis": "{urn:example}Store1"},
        )
        edge = {"old_observation_id": old["observation_id"], "new_observation_id": changed_dimensions["observation_id"],
                "reason": "test", "source_ref": {"material_id": "m-1"}}
        with self.assertRaises(ValueError):
            evaluate_economics("financial_fact_corrections", {
                "facts": [old, changed_dimensions], "corrections": [edge],
                "vintage_basis": "filed", "as_of": "2025-06-01",
            })
        same = self.sample_fact("obs-000002", 90, "2025-04-20", "2025-06-02T12:00:00+00:00")
        reverse = {"old_observation_id": same["observation_id"], "new_observation_id": old["observation_id"],
                   "reason": "reverse", "source_ref": {"material_id": "m-2"}}
        forward = {**edge, "new_observation_id": same["observation_id"]}
        with self.assertRaises(ValueError):
            evaluate_economics("financial_fact_corrections", {
                "facts": [old, same], "corrections": [forward, reverse],
                "vintage_basis": "filed", "as_of": "2025-06-01",
            })

    def test_quarter_selection_has_no_fixed_minimum_and_keeps_gaps_and_vintages(self):
        facts = [
            self.sample_fact("obs-000001", 95, "2025-03-20", "2025-03-22T12:00:00+00:00"),
            self.sample_fact("obs-000002", 100, "2025-04-20", "2025-05-02T12:00:00+00:00"),
            self.sample_fact("obs-000003", 200, "2025-10-20", "2025-11-01T12:00:00+00:00", quarter="2025-Q3"),
        ]
        request = {
            "facts": facts, "metric": "revenue", "unit": "USD",
            "filing_entity": {"name": "Sample Corp", "scheme": "SEC-CIK", "identifier": "0000001234"},
            "disclosure_scope": "filing_entity", "dimensions": {},
            "statement_basis": "standard-taxonomy; statement-vs-note context not provided by SEC API",
            "vintage_basis": "filed", "as_of": "2025-06-30",
            "periods": ["2025-Q1", "2025-Q2", "2025-Q3"],
        }
        result = evaluate_economics("financial_quarterly_series", request)
        data = result["sections"][0]["data"]
        self.assertEqual(data["state"], "PARTIAL")
        self.assertEqual(data["periods"][0]["value"], "100")
        self.assertEqual(data["periods"][1]["reason"], "NO_EXPLICIT_FACT_AVAILABLE_AS_OF")
        self.assertEqual(data["periods"][2]["reason"], "NO_EXPLICIT_FACT_AVAILABLE_AS_OF")
        self.assertIn("100", result["sections"][0]["body"])
        self.assertIn("2025-Q2", result["sections"][0]["body"])
        capture_request = {**request, "vintage_basis": "workspace_capture", "as_of": "2025-04-01T00:00:00+00:00"}
        capture = evaluate_economics("financial_quarterly_series", capture_request)
        self.assertEqual(capture["sections"][0]["data"]["periods"][0]["value"], "95")

    def test_quarter_selection_marks_same_vintage_conflicts_and_never_splits_annual_facts(self):
        facts = [
            self.sample_fact("obs-000001", 100, "2025-04-20", "2025-04-21T12:00:00+00:00"),
            self.sample_fact("obs-000002", 101, "2025-04-20", "2025-04-21T12:00:00+00:00"),
            self.sample_fact("obs-000003", 500, "2026-02-20", "2026-02-21T12:00:00+00:00", quarter="2025-Q1",
                             period_start="2025-01-01", period_end="2025-12-31", period_kind="year"),
        ]
        result = evaluate_economics("financial_quarterly_series", {
            "facts": facts, "metric": "revenue", "unit": "USD",
            "filing_entity": {"name": "Sample Corp", "scheme": "SEC-CIK", "identifier": "0000001234"},
            "disclosure_scope": "filing_entity", "dimensions": {},
            "statement_basis": "standard-taxonomy; statement-vs-note context not provided by SEC API",
            "vintage_basis": "filed", "as_of": "2025-06-01", "periods": ["2025-Q1"],
        })
        row = result["sections"][0]["data"]["periods"][0]
        self.assertEqual(row["state"], "CONFLICT")
        self.assertEqual(row["reason"], "CONFLICTING_FACTS_AT_SELECTED_VINTAGE")
        self.assertEqual(result["sections"][0]["data"]["excluded_non_calendar_quarter_facts"], 1)
        self.assertNotIn("FIN-", result["sections"][0]["body"])

    def test_mmm_keeps_method_specific_quarter_and_source_prerequisites(self):
        inputs = self.mmm_inputs(n=15)
        result = evaluate_economics("financial_mmm_analysis", inputs)
        model = result["sections"][0]["data"]
        self.assertEqual(model["state"], "INSUFFICIENT")
        self.assertEqual(model["reason"], "NEED_12_TRAIN_AND_4_DISJOINT_TEST_QUARTERS")
        self.assertEqual(model["roi_state"], "NOT_ESTABLISHED")
        self.assertIn("15", result["sections"][0]["body"])

        missing_source = self.mmm_inputs(n=16)
        missing_source["periods"][0]["source_refs"] = {}
        result = evaluate_economics("financial_mmm_analysis", missing_source)
        self.assertEqual(result["sections"][0]["data"]["reason"], "PERIOD_SOURCE_REFERENCES_INCOMPLETE")

    def test_mmm_sensitivity_section_renders_diagnostics_and_budget_variants(self):
        from oevira.business_analysis_financial import _mmm_sections

        result = _mmm_sections({
            "state": "READY",
            "n": 16,
            "currency": "CNY",
            "outcome_unit": "CNY sales",
            "filing_entity": {"name": "Sample", "scheme": "TEST", "identifier": "issuer"},
            "disclosure_scope": "filing_entity",
            "spend_basis": "user_declared",
            "vintage_basis": "filed",
            "as_of": "2025-12-31",
            "observation_frequency": "quarter",
            "original_observation_count": 16,
            "transformation": "reported",
            "periods": [],
            "source_refs": [],
            "input_source_refs": {},
            "channel_labels": {"c1": "在线视频"},
            "channels": [{"channel": "c1", "mean_quarter_contribution_interval_95": [1, 2, 3], "causal_effect": "NOT_ESTABLISHED"}],
            "budget_scenario": None,
            "sensitivity": {
                "state": "PARTIAL",
                "variants": [{
                    "id": "shorter_carryover_4_quarters",
                    "state": "READY",
                    "lag_count": 4,
                    "prior": {"adstock_alpha": "Beta(2,2)"},
                    "sampling": {"max_rhat": 1.02, "min_ess_bulk": 260, "divergences": 0},
                    "holdout": {"mae": 7.5, "predictive_interval_coverage": 0.75},
                    "budget_scenario": {"allocation": {"c1": 120}, "currency": "CNY"},
                }],
            },
        })
        section = next(row for row in result["sections"] if row["id"] == "financial_mmm.sensitivity")
        self.assertIn("shorter_carryover_4_quarters", section["body"])
        self.assertIn("120 CNY", section["body"])
        self.assertEqual(section["data"]["state"], "PARTIAL")

    def test_mmm_preserves_user_channel_names_in_saved_report(self):
        inputs = self.mmm_inputs(n=11)
        inputs["channel_labels"] = {"online_video": "在线视频投放"}
        result = evaluate_economics("financial_mmm_analysis", inputs)
        self.assertEqual(result["sections"][0]["data"]["channel_labels"]["online_video"], "在线视频投放")
        self.assertIn("在线视频投放投入(CNY)", result["sections"][0]["body"])
        self.assertIn("已保存原件；result-1", result["sections"][0]["body"])
        self.assertNotIn("synthetic-outcome", result["sections"][0]["body"])
        inputs["channel_labels"] = {"unselected_channel": "未选择渠道"}
        with self.assertRaises(ValueError):
            evaluate_economics("financial_mmm_analysis", inputs)

    def test_mmm_preserves_missing_outcome_and_channel_spend_without_fitting(self):
        inputs = self.mmm_inputs()
        outcome_period = inputs["periods"][3]["period"]
        spend_period = inputs["periods"][7]["period"]
        inputs["periods"][3]["outcome"] = None
        inputs["periods"][7]["spend"]["online_video"] = None
        result = evaluate_economics("financial_mmm_analysis", inputs)
        fit = result["sections"][0]["data"]
        self.assertEqual(fit["state"], "INSUFFICIENT")
        self.assertEqual(fit["reason"], "QUARTERLY_SOURCE_VALUES_INCOMPLETE")
        self.assertEqual(fit["missing_outcome_periods"], [outcome_period])
        self.assertEqual(fit["missing_spend_periods"], {"online_video": [spend_period]})
        self.assertIsNone(fit["periods"][3]["outcome"])
        self.assertIsNone(fit["periods"][7]["spend"]["online_video"])
        self.assertIsNone(result["sections"][1]["data"]["sampling"])
        self.assertIn(spend_period, result["sections"][0]["body"])

    def test_mmm_reports_missing_quarter_without_interpolation(self):
        inputs = self.mmm_inputs()
        inputs["periods"].pop(5)
        inputs["original_observation_count"] -= 1
        result = evaluate_economics("financial_mmm_analysis", inputs)
        fit = result["sections"][0]["data"]
        self.assertEqual(fit["state"], "INSUFFICIENT")
        self.assertEqual(fit["reason"], "MISSING_QUARTERS_NOT_INTERPOLATED")
        self.assertEqual(fit["missing_quarters"], ["2019-Q2"])
        self.assertIn("2019-Q2", result["sections"][0]["body"])

    def test_mmm_preserves_legitimate_negative_financial_outcomes(self):
        inputs = self.mmm_inputs(n=15)
        for index, row in enumerate(inputs["periods"]):
            row["outcome"] = -120 + index * 3
        result = evaluate_economics("financial_mmm_analysis", inputs)
        fit = result["sections"][0]["data"]
        self.assertEqual(fit["state"], "INSUFFICIENT")
        self.assertEqual(fit["reason"], "NEED_12_TRAIN_AND_4_DISJOINT_TEST_QUARTERS")
        self.assertEqual(fit["periods"][0]["outcome"], "-120")
        self.assertEqual(fit["periods"][1]["outcome"], "-117")
        self.assertIn("-120", result["sections"][0]["body"])

    def test_mmm_cpu_fit_returns_sampling_diagnostics_and_time_holdout(self):
        inputs = self.mmm_inputs()
        split = len(inputs["periods"]) - max(4, int(len(inputs["periods"]) * 0.2))
        actual = [float(row["outcome"]) for row in inputs["periods"][split:]]
        seasonal_naive = [
            float(inputs["periods"][split - 4 + index % 4]["outcome"])
            for index in range(len(actual))
        ]
        expected_seasonal_naive_mae = sum(
            abs(actual[index] - seasonal_naive[index]) for index in range(len(actual))
        ) / len(actual)
        result = evaluate_economics("financial_mmm_analysis", inputs)
        fit = result["sections"][0]["data"]
        self.assertEqual(fit["state"], "READY")
        self.assertEqual(fit["method"], "bayesian-geometric-adstock-michaelis-menten-mmm@2")
        sampling = result["sections"][1]["data"]["sampling"]
        self.assertEqual(sampling["library"], "pymc")
        self.assertEqual(sampling["version"], "6.4.0")
        self.assertEqual(sampling["cores"], 1)
        self.assertEqual(sampling["chains"], 4)
        self.assertEqual(sampling["draws_per_chain"], 500)
        self.assertEqual(sampling["tune_per_chain"], 750)
        self.assertEqual(sampling["divergences"], 0)
        self.assertLessEqual(sampling["max_rhat"], 1.05)
        self.assertGreaterEqual(sampling["min_ess_bulk"], 200)
        self.assertIn("已保存原件；result-1", result["sections"][0]["body"])
        self.assertEqual(result["sections"][0]["data"]["periods"][0]["source_refs"]["outcome"]["material_id"], "synthetic-outcome")
        self.assertEqual(fit["train_n"], 26)
        self.assertEqual(fit["test_n"], 6)
        self.assertAlmostEqual(fit["holdout"]["mae"], 3.672834615256382, delta=0.5)
        self.assertAlmostEqual(
            fit["holdout"]["benchmarks"]["seasonal_naive_4_quarter"]["mae"],
            expected_seasonal_naive_mae,
            places=10,
        )
        sensitivity = next(row["data"] for row in result["sections"] if row["id"] == "financial_mmm.sensitivity")
        self.assertEqual(sensitivity["variant_count"], 3)
        self.assertEqual(
            {row["id"] for row in sensitivity["variants"]},
            {"shorter_carryover_4_quarters", "diffuse_channel_priors", "regularized_channel_priors"},
        )
        self.assertEqual(fit["roi_state"], "NOT_ESTABLISHED")
        self.assertEqual(result["sections"][3]["data"]["budget_scenario"]["state"], "READY")
        self.assertAlmostEqual(sum(fit["budget_scenario"]["allocation"].values()), 1000, delta=1e-5)

    def test_mmm_sampling_callback_interrupts_cpu_inference(self):
        from oevira.business_analysis_economics import BusinessAnalysisCancelled

        state = {"cancelled": False}

        def is_cancelled():
            return state["cancelled"]

        def interrupted_sample(**kwargs):
            state["cancelled"] = True
            try:
                kwargs["callback"](None, None)
            except KeyboardInterrupt as error:
                # PyMC 6.4 returns an incomplete trace error after its callback
                # interruption; the wrapper must surface the domain cancel.
                raise ValueError("Not enough samples to build a trace.") from error
            raise AssertionError("PyMC callback did not observe the cancellation")

        with patch("pymc.sample", side_effect=interrupted_sample) as sample:
            with self.assertRaises(BusinessAnalysisCancelled):
                evaluate_economics("financial_mmm_analysis", self.mmm_inputs(n=16),
                                   is_cancelled=is_cancelled)
        self.assertTrue(sample.called)


if __name__ == "__main__":
    unittest.main()
