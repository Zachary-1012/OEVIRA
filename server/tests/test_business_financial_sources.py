"""Saved Artifact source resolution for financial business calculations."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oevira.business_analysis_economics import evaluate_economics
from oevira.business_financial_sources import resolve_saved_financial_inputs


class BusinessFinancialSourceTests(unittest.TestCase):
    def setUp(self):
        from test_business_execution import BusinessExecutionTests

        self.case = BusinessExecutionTests("test_full_result_revision_export_and_cold_reopen")
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def _saved_filing(self, *, filing_id: str, filed: str, value: int):
        text = f"合并利润表记载营销费用{value}万元。"
        start = text.index(str(value))
        material = self.case.post("/api/materials", {
            "title": f"隔离财报 {filing_id}",
            "filename": f"{filing_id}.txt",
            "text": text,
            "work_id": self.case.work["id"],
        })
        artifact = self.case.post(f"/api/works/{self.case.work['id']}/business-results", {
            "operation": "financial_source_analysis",
            "inputs": {
                "source_format": "disclosure-extract",
                "source": {"material_id": material["id"], "material_sha256": material["sha256"]},
                "filing_id": filing_id,
                "filed": filed,
                "form": "10-Q",
                "annotations": [{
                    "metric": "marketing_expense",
                    "value": str(value * 10000),
                    "value_literal": str(value),
                    "scale": 10000,
                    "unit": "CNY",
                    "period_start": "2025-01-01",
                    "period_end": "2025-03-31",
                    "filing_entity": {"name": "隔离主体", "scheme": "TEST", "identifier": "issuer-1"},
                    "filing_id": filing_id,
                    "filed": filed,
                    "form": "10-Q",
                    "disclosure_scope": "filing_entity",
                    "statement_basis": "consolidated-income-statement",
                    "start": start,
                    "end": start + len(str(value)),
                }],
            },
            "citations": [],
        })
        return material, artifact

    def _resolve(self, operation: str, inputs: dict):
        with self.case.store.transaction(immediate=False) as connection:
            return resolve_saved_financial_inputs(
                self.case.store, connection, self.case.identity, self.case.work["id"], operation, inputs,
            )

    @staticmethod
    def _pure_inputs(inputs: dict) -> dict:
        return {key: value for key, value in inputs.items() if key != "_resolved_financial_sources"}

    def test_fact_corrections_use_saved_versions_and_retain_original_material_links(self):
        old_material, old_artifact = self._saved_filing(filing_id="filing-old", filed="2025-04-20", value=12)
        new_material, new_artifact = self._saved_filing(filing_id="filing-new", filed="2025-05-20", value=11)
        old_fact = old_artifact["input_snapshot"]["business"]["generated_sections"][0]["data"]["facts"][0]
        new_fact = new_artifact["input_snapshot"]["business"]["generated_sections"][0]["data"]["facts"][0]
        request = {
            "source_artifacts": [
                {"artifact_id": old_artifact["id"], "artifact_version": old_artifact["version"]},
                {"artifact_id": new_artifact["id"], "artifact_version": new_artifact["version"]},
            ],
            "corrections": [{
                "old_observation_id": old_fact["observation_id"],
                "new_observation_id": new_fact["observation_id"],
                "reason": "更正申报了费用金额",
            }],
            "vintage_basis": "filed",
            "as_of": "2025-05-31",
        }
        evaluation, saved = self._resolve("financial_fact_corrections", request)
        self.assertNotIn("facts", saved)
        self.assertEqual(saved["source_artifacts"], request["source_artifacts"])
        self.assertEqual(evaluation["facts"][0]["source_ref"]["material_id"], old_material["id"])
        self.assertEqual(evaluation["facts"][1]["source_ref"]["material_id"], new_material["id"])
        relation = evaluation["corrections"][0]
        self.assertEqual(relation["source_ref"]["material_id"], new_material["id"])
        self.assertEqual(relation["captured_at"], new_fact["captured_at"])
        manifest = evaluation["_resolved_financial_sources"]
        self.assertEqual(len(manifest["resolved_source_versions"]), 2)
        self.assertEqual({row["material_id"] for row in manifest["source_materials"]}, {old_material["id"], new_material["id"]})

        result = evaluate_economics("financial_fact_corrections", self._pure_inputs(evaluation))
        active = result["sections"][1]["data"]["active_facts"]
        self.assertEqual([row["value"] for row in active], ["110000"])
        before = evaluate_economics("financial_fact_corrections", {
            **self._pure_inputs(evaluation), "as_of": "2025-05-01",
        })
        self.assertEqual([row["value"] for row in before["sections"][1]["data"]["active_facts"]], ["120000"])

    def test_quarterly_series_uses_only_selected_saved_fact_artifacts(self):
        _old_material, old_artifact = self._saved_filing(filing_id="filing-old", filed="2025-04-20", value=12)
        _new_material, new_artifact = self._saved_filing(filing_id="filing-new", filed="2025-05-20", value=11)
        request = {
            "source_artifacts": [
                {"artifact_id": old_artifact["id"], "artifact_version": old_artifact["version"]},
                {"artifact_id": new_artifact["id"], "artifact_version": new_artifact["version"]},
            ],
            "metric": "marketing_expense",
            "unit": "CNY",
            "filing_entity": {"name": "隔离主体", "scheme": "TEST", "identifier": "issuer-1"},
            "disclosure_scope": "filing_entity",
            "dimensions": {},
            "statement_basis": "consolidated-income-statement",
            "vintage_basis": "filed",
            "as_of": "2025-05-31",
            "periods": ["2025-Q1", "2025-Q2"],
        }
        evaluation, saved = self._resolve("financial_quarterly_series", request)
        self.assertNotIn("facts", saved)
        result = evaluate_economics("financial_quarterly_series", self._pure_inputs(evaluation))
        rows = result["sections"][0]["data"]["periods"]
        self.assertEqual(rows[0]["value"], "110000")
        self.assertEqual(rows[0]["state"], "AVAILABLE")
        self.assertEqual(rows[1]["state"], "MISSING")
        self.assertEqual(rows[1]["reason"], "NO_EXPLICIT_FACT_AVAILABLE_AS_OF")
        self.assertEqual(rows[0]["source_refs"][0]["material_id"], new_artifact["input_snapshot"]["business"]["generated_sections"][0]["data"]["facts"][0]["source_ref"]["material_id"])

    def test_client_fact_override_and_wrong_artifact_kind_are_rejected(self):
        _material, source = self._saved_filing(filing_id="filing-one", filed="2025-04-20", value=12)
        with self.assertRaises(Exception) as error:
            self._resolve("financial_quarterly_series", {
                "source_artifacts": [{"artifact_id": source["id"], "artifact_version": source["version"]}],
                "facts": [],
            })
        self.assertEqual(getattr(error.exception, "code", None), "CLIENT_FINANCIAL_FACTS_FORBIDDEN")
        wrong = self.case.post(f"/api/works/{self.case.work['id']}/business-results", {
            "operation": "operating_analysis", "inputs": {"observations": []}, "citations": [],
        })
        with self.assertRaises(Exception) as error:
            self._resolve("financial_quarterly_series", {
                "source_artifacts": [{"artifact_id": wrong["id"], "artifact_version": wrong["version"]}],
                "metric": "marketing_expense", "unit": "CNY",
                "filing_entity": {"name": "隔离主体", "scheme": "TEST", "identifier": "issuer-1"},
                "disclosure_scope": "filing_entity", "dimensions": {},
                "statement_basis": "consolidated-income-statement", "vintage_basis": "filed",
                "as_of": "2025-05-31",
            })
        self.assertEqual(getattr(error.exception, "code", None), "FINANCIAL_SOURCE_NOT_PARSED")

