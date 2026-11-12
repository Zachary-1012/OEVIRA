"""Financial sources through the real business API, revisions and cold reopen."""
import unittest

import test_business_financial_sources


class BusinessFinancialFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_business_financial_sources.BusinessFinancialSourceTests()
        self.fixture.setUp()
        self.case = self.fixture.case

    def tearDown(self):
        self.fixture.tearDown()

    def sources(self):
        first_material, first = self.fixture._saved_filing(filing_id="filing-old", filed="2025-04-20", value=12)
        second_material, second = self.fixture._saved_filing(filing_id="filing-new", filed="2025-05-20", value=11)
        references = [{"artifact_id": item["id"], "artifact_version": item["version"]} for item in (first, second)]
        return (first_material, second_material), (first, second), references

    def quarters(self, references):
        return {"source_artifacts": references, "metric": "marketing_expense", "unit": "CNY",
                "filing_entity": {"name": "隔离主体", "scheme": "TEST", "identifier": "issuer-1"},
                "disclosure_scope": "filing_entity", "dimensions": {}, "statement_basis": "consolidated-income-statement",
                "vintage_basis": "filed", "as_of": "2025-05-31", "periods": ["2025-Q1", "2025-Q2"]}

    def create(self, operation, inputs):
        return self.case.post(f"/api/works/{self.case.work['id']}/business-results", {"operation": operation, "inputs": inputs})

    def test_source_versions_drive_as_of_revision_and_cold_reopen(self):
        materials, _, references = self.sources()
        artifact = self.create("financial_quarterly_series", self.quarters(references))
        business = artifact["input_snapshot"]["business"]
        self.assertNotIn("facts", business["inputs"])
        self.assertEqual(business["generated_sections"][0]["data"]["periods"][0]["value"], "110000")
        self.assertEqual({item["id"] for item in artifact["input_snapshot"]["materials"]}, {item["id"] for item in materials})
        self.assertEqual(len(business["resolved_financial_sources"]["resolved_source_versions"]), 2)
        earlier = self.case.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "input_changes": {"as_of": "2025-05-01"}})
        self.assertEqual(earlier["input_snapshot"]["business"]["generated_sections"][0]["data"]["periods"][0]["value"], "120000")
        self.case.app = self.case.new_app()
        self.case.client = self.case.app.test_client()
        reopened = self.case.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
        self.assertEqual(reopened["input_snapshot"]["business"]["inputs"]["source_artifacts"], references)
        self.assertEqual(reopened["input_snapshot"]["business"]["resolved_financial_sources"], earlier["input_snapshot"]["business"]["resolved_financial_sources"])
        export = self.case.post(f"/api/artifacts/{artifact['id']}/exports", {"version": 2, "format": "html"})
        download = self.case.request("get", export["download_url"])
        self.assertIn("120000", download.get_data(as_text=True))
        self.assertIn("2025-05-01", download.get_data(as_text=True))
        download.close()

    def test_normalized_corrections_remain_revisable_from_saved_inputs(self):
        _, artifacts, references = self.sources()
        facts = [item["input_snapshot"]["business"]["generated_sections"][0]["data"]["facts"][0] for item in artifacts]
        artifact = self.create("financial_fact_corrections", {"source_artifacts": references, "corrections": [{"old_observation_id": facts[0]["observation_id"], "new_observation_id": facts[1]["observation_id"], "reason": "采用后续明确更正"}], "vintage_basis": "filed", "as_of": "2025-05-31"})
        self.assertNotIn("captured_at", artifact["input_snapshot"]["business"]["inputs"]["corrections"][0])
        revised = self.case.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "input_changes": {"as_of": "2025-05-01"}})
        self.assertEqual(revised["input_snapshot"]["business"]["generated_sections"][1]["data"]["active_facts"][0]["value"], "120000")

    def test_mmm_uses_frozen_quarterly_outcomes_and_rejects_overrides(self):
        _, _, references = self.sources()
        series = self.create("financial_quarterly_series", self.quarters(references))
        request = {"outcome_artifact": {"artifact_id": series["id"], "artifact_version": 1},
                   "currency": "CNY", "outcome_unit": "CNY", "filing_entity": {"name": "隔离主体", "scheme": "TEST", "identifier": "issuer-1"},
                   "disclosure_scope": "filing_entity", "spend_basis": "user_declared", "vintage_basis": "filed", "as_of": "2025-05-31",
                   "observation_frequency": "quarter", "original_observation_count": 2, "transformation": "reported",
                   "periods": [{"period": "2025-Q1", "spend": {"search": 100}}, {"period": "2025-Q2", "spend": {"search": 120}}]}
        artifact = self.create("financial_mmm_analysis", request)
        business = artifact["input_snapshot"]["business"]
        self.assertTrue(all("outcome" not in row for row in business["inputs"]["periods"]))
        self.assertEqual(len(business["resolved_financial_sources"]["resolved_source_versions"]), 3)
        self.assertIn("110000", artifact["body"])
        self.assertIn("2025-Q2", artifact["body"])
        self.assertEqual(business["generated_sections"][1]["data"]["sampling"], None)
        request["periods"][0]["outcome"] = 999999
        response = self.case.request("post", f"/api/works/{self.case.work['id']}/business-results", json={"operation": "financial_mmm_analysis", "inputs": request})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.json["error"]["code"], "FINANCIAL_OUTCOME_OVERRIDE_FORBIDDEN")
