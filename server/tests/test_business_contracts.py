from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.oevira import create_app
from server.oevira.auth import issue_local_capability
from server.oevira.business_contracts import (
    CONTRACT_VERSION,
    CONTRACTS,
    BusinessContractError,
    assert_contracts_cover_implemented,
    get_contract,
    implemented_operations,
    list_operation_contracts,
)
from server.oevira.business_execution import evaluate_business
from server.oevira.api import ApiError


ORIGIN = "http://127.0.0.1:8361"


class BusinessContractTests(unittest.TestCase):
    def test_registry_covers_every_implemented_operation_without_stubs(self):
        assert_contracts_cover_implemented()
        operations = implemented_operations()
        self.assertEqual(set(CONTRACTS), operations)
        self.assertGreaterEqual(len(operations), 24)
        for name in sorted(operations):
            contract = get_contract(name)
            self.assertEqual(contract["contract_version"], CONTRACT_VERSION)
            self.assertEqual(contract["contract_status"], "complete")
            self.assertEqual(contract["operation"], name)
            self.assertIsInstance(contract["family"], str)
            self.assertTrue(contract["family"])
            schema = contract["input_schema"]
            self.assertEqual(schema["type"], "object")
            self.assertIsInstance(schema.get("properties"), dict)
            self.assertTrue(schema["properties"], name)
            self.assertNotIn("TODO", str(contract))
            self.assertNotIn("stub", str(contract).lower())

    def test_get_contract_rejects_unknown_operations_and_returns_a_copy(self):
        with self.assertRaises(BusinessContractError) as raised:
            get_contract("not_an_implemented_operation")
        self.assertEqual(raised.exception.code, "UNKNOWN_BUSINESS_OPERATION")
        self.assertEqual(raised.exception.status, 404)
        contract = get_contract("decision_comparison")
        contract["input_schema"]["properties"]["question"] = {"mutated": True}
        original = get_contract("decision_comparison")
        self.assertNotEqual(original["input_schema"]["properties"]["question"], {"mutated": True})
        self.assertEqual(len(list_operation_contracts()), len(implemented_operations()))

    def test_operating_analysis_contract_rejects_the_same_unknown_field_as_the_evaluator(self):
        contract = get_contract("operating_analysis")
        self.assertFalse(contract["input_schema"].get("additionalProperties"))
        self.assertIn("observations", contract["input_schema"]["properties"])
        with self.assertRaises(ApiError) as raised:
            evaluate_business("operating_analysis", {"invented_field": True})
        self.assertEqual(raised.exception.code, "INVALID_BUSINESS_INPUT")


class BusinessContractApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-business-contracts-", dir=r"D:\寰意\.runtime")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8361)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.app.extensions["oevira.store"], capability_file)
        response = self.client.post(
            "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"),
                  "display_name": "合同读取测试"},
            base_url=ORIGIN, headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, method, path, **kwargs):
        kwargs.setdefault("base_url", ORIGIN)
        headers = kwargs.setdefault("headers", {})
        headers.setdefault("Authorization", f"Bearer {self.token}")
        headers.setdefault("Origin", ORIGIN)
        return getattr(self.client, method)(path, **kwargs)

    def test_operations_list_and_single_contract_are_readable(self):
        listed = self.request("get", "/api/business/operations")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        data = listed.json["data"]
        operations = set(implemented_operations())
        self.assertEqual(set(data["operations"]), operations)
        self.assertEqual(data["contract_version"], CONTRACT_VERSION)
        self.assertEqual(data["result_kind"], "business_analysis")
        self.assertEqual({item["operation"] for item in data["contracts"]}, operations)
        for item in data["contracts"]:
            self.assertEqual(item["contract_status"], "complete")
            self.assertEqual(item, get_contract(item["operation"]))

        one = self.request("get", "/api/business/operations/experiment_design")
        self.assertEqual(one.status_code, 200, one.get_data(as_text=True))
        self.assertEqual(one.json["data"], get_contract("experiment_design"))

        missing = self.request("get", "/api/business/operations/not_an_implemented_operation")
        self.assertEqual(missing.status_code, 404, missing.get_data(as_text=True))
        self.assertEqual(missing.json["error"]["code"], "UNKNOWN_BUSINESS_OPERATION")

    def test_intelligence_tool_catalog_consumes_the_contracts(self):
        catalog = self.app.extensions["oevira.intelligence"].tools.catalog()
        business = next(item for item in catalog["tools"] if item["tool"] == "business.analyze")
        self.assertEqual(business["business_contract_version"], CONTRACT_VERSION)
        self.assertEqual(set(business["operation_contracts"]), implemented_operations())
        self.assertEqual(
            set(business["input_schema"]["properties"]["operation"]["enum"]),
            implemented_operations(),
        )
        self.assertEqual(
            business["operation_contracts"]["service_response"],
            get_contract("service_response"),
        )


if __name__ == "__main__":
    unittest.main()
