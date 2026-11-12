from __future__ import annotations

import io
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from oevira import create_app
from oevira.auth import _digest, issue_local_capability, utc_now
from oevira.business_execution import register_business_execution_routes
from oevira import business_execution
from oevira.worker import run_once
from oevira.collaboration import install_collaboration_schema, register_collaboration_boundary, register_collaboration_routes


ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8317"


class BusinessExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary_parent = ROOT / ".runtime" / "business-tests"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_parent)
        self.root = Path(self.temporary.name)
        self.app = self.new_app()
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        cap = self.root / "launch.capability"
        issue_local_capability(self.store, cap)
        response = self.request("post", "/api/local-session", json={"capability": cap.read_text("ascii")})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]
        self.work = self.post("/api/works", {"title": "活动服务复盘", "goal": "按证据处理门店活动服务问题"})

    def new_app(self):
        app = create_app(self.root, expected_port=8317)
        install_collaboration_schema(app.extensions["oevira.store"])
        register_collaboration_routes(app)
        register_collaboration_boundary(app)
        register_business_execution_routes(app)
        app.config.update(TESTING=True)
        return app

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        if hasattr(self, "token"):
            headers.setdefault("Authorization", f"Bearer {self.token}")
        headers.setdefault("Idempotency-Key", str(uuid.uuid4()))
        return getattr(self.client, method)(path, base_url=BASE, headers=headers, **kwargs)

    def post(self, path, payload, *, key=None, status=(200, 201)):
        response = self.request("post", path, json=payload, headers={"Idempotency-Key": key or str(uuid.uuid4())})
        self.assertIn(response.status_code, status, response.get_data(as_text=True))
        return response.json["data"] if response.status_code in {200, 201} else response.json["error"]

    def make_result(self):
        return self.post(f"/api/works/{self.work['id']}/business-results", {
            "operation": "service_response", "inputs": {
                "goal": "核对门店活动反馈并安排有责任人的补救", "as_of": "2026-11-03T12:00:00+08:00",
                "coverage": [{"source": "活动意见表", "status": "partial"}],
                "claims": [{"id": "claim-1", "target": "活动门店", "text": "现场等候较长", "status": "unverified", "severity": "material"}],
                "commitments": [{"id": "promise-1", "owner": "门店负责人", "deliverable": "核对当日排队和人员安排", "fulfilment_basis": "已有排班记录", "due_at": "2026-11-04T12:00:00+08:00"}],
            }, "citations": [],
        })

    def command(self, artifact, command, payload, *, key=None, status=(200, 201)):
        return self.post(f"/api/business-results/{artifact['id']}/commands", {"base_version": artifact["version"], "command": command, "payload": payload}, key=key, status=status)

    def test_full_result_revision_export_and_cold_reopen(self):
        artifact = self.make_result()
        revised = self.post(f"/api/business-results/{artifact['id']}/revisions", {
            "base_version": 1, "section_edits": [{"id": "response", "body": "客户确认用语：我们已收到反馈，将核对当日安排。"}], "confirm_sections": ["response"],
        })
        changed = self.post(f"/api/business-results/{artifact['id']}/revisions", {
            "base_version": 2, "input_changes": {"goal": "按顾客反馈核对门店活动排班"},
        })
        self.assertIn("客户确认用语", changed["body"])
        self.assertEqual(changed["input_snapshot"]["business"]["changes"], [{"id": "purpose", "reason": "input_recomputed"}])
        for fmt in ("docx", "pptx", "xlsx", "pdf", "html"):
            export = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": changed["version"], "format": fmt})
            download = self.request("get", export["download_url"])
            self.assertEqual(download.status_code, 200, download.get_data(as_text=True) if download.status_code != 200 else "")
            content = download.get_data()
            self.assertTrue(content)
            if fmt in {"docx", "pptx", "xlsx"}:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    self.assertIn("[Content_Types].xml", archive.namelist())
                    all_xml = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml"))
                    self.assertIn("客户确认用语", all_xml)
            elif fmt == "pdf":
                self.assertTrue(content.startswith(b"%PDF"))
            else:
                self.assertIn("客户确认用语", content.decode("utf-8"))
            download.close()
        self.app = self.new_app()
        self.client = self.app.test_client()
        reopened = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(reopened.json["data"]["body"], changed["body"])
        self.assertEqual(reopened.json["data"]["version"], 3)

    def test_confirmed_section_conflict_returns_exact_change(self):
        artifact = self.make_result()
        self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "confirm_sections": ["purpose"]})
        error = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 2, "input_changes": {"goal": "改变后的问题"}}, status=(409,))
        self.assertEqual(error["code"], "CONFIRMED_SECTION_AFFECTED")
        self.assertEqual([item["id"] for item in error["details"]["sections"]], ["purpose"])
        changed = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 2, "input_changes": {"goal": "改变后的问题"}, "accept_sections": ["purpose"]})
        self.assertIn("改变后的问题", changed["body"])

    def test_replaying_lost_response_returns_original_version(self):
        artifact = self.make_result()
        data = {"base_version": 1, "section_edits": [{"id": "response", "body": "一次已保存的修改"}]}
        first = self.post(f"/api/business-results/{artifact['id']}/revisions", data, key="lost-response")
        self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 2, "section_edits": [{"id": "response", "body": "较新的人工稿"}]})
        replay = self.post(f"/api/business-results/{artifact['id']}/revisions", data, key="lost-response")
        self.assertEqual(replay["version"], first["version"])
        self.assertEqual(replay["body"], first["body"])
        self.assertIn("较新的人工稿", self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["body"])

    def test_cost_refunds_and_downward_correction_are_exact(self):
        artifact = self.make_result()
        paid = self.command(artifact, "record_cost", {"id": "pay-1", "kind": "actual", "amount": "100.10", "currency": "CNY"})
        partial = self.command(paid, "record_cost", {"id": "refund-1", "kind": "refund", "amount": "40.10", "currency": "CNY", "refund_of": "pay-1"})
        self.assertIn("60.00", partial["body"])
        bad = self.command(partial, "record_cost", {"id": "pay-2", "kind": "actual", "amount": "30", "currency": "CNY", "supersedes": "pay-1", "reason": "更正"}, status=(409,))
        self.assertEqual(bad["code"], "REFUND_EXCEEDS_PAYMENT")
        bad = self.command(partial, "record_cost", {"id": "refund-2", "kind": "refund", "amount": "61", "currency": "CNY", "refund_of": "pay-1"}, status=(409,))
        self.assertEqual(bad["code"], "REFUND_EXCEEDS_PAYMENT")
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 3)

    def test_restore_keeps_real_action_and_cost_history(self):
        artifact = self.make_result()
        action = self.command(artifact, "plan_action", {"id": "act-1", "title": "核对排班", "target": {"kind": "location", "id": "store-1", "label": "活动门店"},
                              "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "本工作负责人"}, "deliverable": "当日排班核对记录", "section_ids": ["commitments"]})
        approved = self.command(action, "approve_action", {"action_id": "act-1", "scope": "仅内部核对排班"})
        performed = self.command(approved, "record_action", {"action_id": "act-1", "status": "performed", "occurred_at": "2026-11-03T13:00:00+08:00", "description": "负责人记录已完成排班核对"})
        paid = self.command(performed, "record_cost", {"id": "actual-1", "kind": "actual", "amount": "80", "currency": "CNY", "action_id": "act-1"})
        restored = self.post(f"/api/business-results/{artifact['id']}/restore", {"base_version": paid["version"], "restore_version": 1})
        business = restored["input_snapshot"]["business"]
        self.assertEqual(business["actions"][0]["state"], "recorded")
        self.assertTrue(business["actions"][0]["approval"]["valid"])
        self.assertEqual(business["costs"][0]["amount"], "80")
        unsafe = self.request("post", f"/api/artifacts/{artifact['id']}/restore", json={"base_version": restored["version"], "restore_version": 1})
        self.assertEqual(unsafe.status_code, 409)

    def test_unknown_effect_requires_reconciliation(self):
        artifact = self.make_result()
        action = self.command(artifact, "plan_action", {"id": "a1", "title": "外部渠道动作", "target": {"kind": "account", "id": "owned-account", "label": "客户指定账号"},
                              "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "已授权发布内容", "external_effect": True})
        unknown = self.command(action, "record_action", {"action_id": "a1", "status": "unknown", "occurred_at": "2026-11-03T13:00:00+08:00", "description": "记录外部结果待查"})
        blocked = self.command(unknown, "record_action", {"action_id": "a1", "status": "performed", "occurred_at": "2026-11-03T13:01:00+08:00", "description": "再次提交"}, status=(409,))
        self.assertEqual(blocked["code"], "RECEIPT_LOOKUP_REQUIRED")

    def add_member(self):
        uid, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users VALUES(?,?,?)", (uid, "另一位客户成员", utc_now()))
            connection.execute("INSERT INTO memberships VALUES(?,?,'editor',1,?)", (self.identity["workspace"]["id"], uid, utc_now()))
            connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?,'2099-01-01T00:00:00Z',NULL)", (str(uuid.uuid4()), _digest(token), uid, self.identity["workspace"]["id"], utc_now()))
        return uid, token

    def test_client_isolation_applies_to_lists_read_write_export_and_revoke(self):
        commission = self.post("/api/business/commissions", {"name": "甲客户", "scope": {"deliverable": "活动服务报告"}})
        self.post(f"/api/business/commissions/{commission['id']}/works/{self.work['id']}", {})
        artifact = self.make_result()
        exported = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": 1, "format": "html"})
        member, token = self.add_member()
        owner = self.token
        self.token = token
        for path in (f"/api/works/{self.work['id']}", f"/api/artifacts/{artifact['id']}", exported["download_url"]):
            self.assertEqual(self.request("get", path).status_code, 404)
        self.assertEqual(self.request("get", "/api/works").json["data"], [])
        self.assertEqual(self.request("get", "/api/artifacts").json["data"], [])
        self.token = owner
        granted = self.request("patch", f"/api/business/commissions/{commission['id']}", json={"base_version": 1, "member": {"user_id": member, "role": "viewer", "active": True}})
        self.assertEqual(granted.status_code, 200)
        self.token = token
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").status_code, 200)
        denied = self.request("post", f"/api/business-results/{artifact['id']}/revisions", json={"base_version": 1, "input_changes": {"goal": "不能修改"}})
        self.assertEqual(denied.status_code, 403)
        self.token = owner
        self.request("patch", f"/api/business/commissions/{commission['id']}", json={"base_version": 2, "member": {"user_id": member, "role": "viewer", "active": False}})
        self.token = token
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").status_code, 404)

    def test_nested_private_source_cannot_cross_commissions(self):
        commission = self.post("/api/business/commissions", {"name": "甲客户", "scope": {}})
        self.post(f"/api/business/commissions/{commission['id']}/works/{self.work['id']}", {})
        source = self.post("/api/materials", {"title": "甲方私有记录", "text": "仅甲客户可用的服务资料", "work_id": self.work["id"]})
        other = self.post("/api/works", {"title": "乙客户工作", "goal": "独立服务判断"})
        second = self.post("/api/business/commissions", {"name": "乙客户", "scope": {}})
        self.post(f"/api/business/commissions/{second['id']}/works/{other['id']}", {})
        response = self.request("post", f"/api/works/{other['id']}/business-results", json={"operation": "service_response", "inputs": {"goal": "核实事件", "as_of": "2026-11-03T12:00:00+08:00", "claims": [{"id": "c1", "target": "店铺", "text": "一个主张", "support": [{"material_id": source["id"]}]}]}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

    def test_uploaded_table_drives_business_values_without_json_algorithm_input(self):
        source = self.post("/api/materials", {"title": "隔离经营表", "filename": "sales.csv", "text": "记录,开始,结束,销售额\nw1,2026-01-01,2026-01-07,100\nw2,2026-01-08,2026-01-14,120\n", "work_id": self.work["id"]})
        result = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "operating_analysis", "inputs": {}, "tables": [{
            "material_id": source["id"], "target": "observations",
            "columns": {"id": "记录", "period.start": "开始", "period.end": "结束", "metric.value": {"column": "销售额", "type": "decimal"}},
            "row_defaults": {"entity": {"id": "test-store", "name": "隔离样例门店"}, "metric": {"key": "sales", "definition": "本店净销售额", "unit": "CNY", "currency": "CNY", "basis": "actual", "denominator": None, "comparison_dimensions": ["open_days", "area_m2"]}, "period": {"frequency": "week", "timezone": "Asia/Shanghai"}, "scope": {"open_days": 7, "area_m2": 400}, "coverage": {"observed": 7, "eligible": 7}},
        }]})
        business = result["input_snapshot"]["business"]
        comparisons = next(item["data"]["comparisons"] for item in business["result"]["sections"] if item["id"] == "operating.comparisons")
        self.assertEqual(comparisons[0]["delta"], 20)
        self.assertIn("20%", result["body"])
        self.assertEqual(business["table_sources"][0]["used_nonempty_rows"], 2)
        self.assertEqual(result["input_snapshot"]["materials"][0]["id"], source["id"])

    def test_business_job_and_concurrent_work_change_do_not_hold_writer_or_save_stale_result(self):
        response = self.request("post", f"/api/works/{self.work['id']}/business-jobs", json={"operation": "service_response", "inputs": {"goal": "核实活动服务问题", "as_of": "2026-11-03T12:00:00+08:00"}})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job_id = response.json["data"]["id"]
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(job["status"], "completed")
        self.assertIsNotNone(job["artifact_id"])
        original = business_execution.evaluate_business
        def change_work_while_calculating(operation, inputs, **kwargs):
            computed = original(operation, inputs, **kwargs)
            updated = self.request("patch", f"/api/works/{self.work['id']}", json={"base_version": 1, "title": "用户在计算期间更改工作"})
            self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
            return computed
        with patch.object(business_execution, "evaluate_business", side_effect=change_work_while_calculating):
            result = self.request("post", f"/api/works/{self.work['id']}/business-results", json={"operation": "service_response", "inputs": {"goal": "旧问题", "as_of": "2026-11-03T12:00:00+08:00"}})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json["error"]["code"], "WORK_VERSION_CONFLICT")
        self.assertEqual(len(self.request("get", "/api/artifacts").json["data"]), 1)

    def test_source_correction_recomputes_values_preserves_locked_text_and_reopens(self):
        def source(text):
            return self.post("/api/materials", {"title": "保存的经营资料", "filename": "sales.csv", "text": text, "work_id": self.work["id"]})
        original = source("记录,开始,结束,销售额\nw1,2026-01-01,2026-01-07,100\nw2,2026-01-08,2026-01-14,120\n")
        corrected = source("记录,开始,结束,销售额\nw1,2026-01-01,2026-01-07,100\nw2,2026-01-08,2026-01-14,150\n")
        binding = {"material_id": original["id"], "target": "observations",
                   "columns": {"id": "记录", "period.start": "开始", "period.end": "结束", "metric.value": {"column": "销售额", "type": "decimal"}},
                   "row_defaults": {"entity": {"id": "one-store", "name": "同一门店"}, "metric": {"key": "sales", "definition": "同口径净销售额", "unit": "CNY", "currency": "CNY", "basis": "actual", "denominator": None, "comparison_dimensions": []}, "period": {"frequency": "week", "timezone": "Asia/Shanghai"}, "scope": {}, "coverage": {"observed": 7, "eligible": 7}}}
        artifact = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "operating_analysis", "inputs": {}, "tables": [binding]})
        revised = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "section_edits": [{"id": "operating.comparisons", "body": "客户确认：金额尚待财务更正。"}], "confirm_sections": ["operating.comparisons"]})
        saved_input = artifact["input_snapshot"]["business"]["input_sources"][0]
        correction = {"op": "replace", "input_id": saved_input["input_id"], "input_snapshot_revision": 1, "old_ref": saved_input["ref"], "new_ref": {"kind": "material", "id": corrected["id"], "version": corrected["sha256"]}, "reason": "财务补齐漏记交易，采用更正原表"}
        payload = {"base_version": revised["version"], "input_changes": {"source_changes": [correction]}}
        conflict = self.post(f"/api/business-results/{artifact['id']}/revisions", payload, status=(409,))
        self.assertEqual(conflict["code"], "CONFIRMED_SECTION_AFFECTED")
        self.assertIn("客户确认", self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["body"])
        payload["accept_sections"] = ["operating.comparisons"]
        updated = self.post(f"/api/business-results/{artifact['id']}/revisions", payload, key="correct-source-once")
        comparison = next(item for item in updated["input_snapshot"]["business"]["result"]["sections"] if item["id"] == "operating.comparisons")
        self.assertEqual(comparison["data"]["comparisons"][0]["delta"], 50)
        self.assertIn("50%", updated["body"])
        self.assertNotIn("source_changes", updated["input_snapshot"]["business"]["inputs"])
        self.assertEqual(updated["input_snapshot"]["business"]["input_snapshot_revision"], 2)
        self.assertEqual([item["id"] for item in updated["input_snapshot"]["materials"]], [corrected["id"]])
        replay = self.post(f"/api/business-results/{artifact['id']}/revisions", payload, key="correct-source-once")
        self.assertEqual(replay["version"], updated["version"])
        stale = self.post(f"/api/business-results/{artifact['id']}/revisions", {**payload, "base_version": updated["version"]}, status=(409,))
        self.assertEqual(stale["code"], "INPUT_SNAPSHOT_CONFLICT")
        self.app = self.new_app()
        self.client = self.app.test_client()
        reopened = self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
        self.assertEqual(reopened["input_snapshot"]["business"]["input_sources"][0]["ref"], correction["new_ref"])
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}?version=1").json["data"]["version"], 1)
        export = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": reopened["version"], "format": "html"})
        response = self.request("get", export["download_url"])
        self.assertIn("50%", response.get_data(as_text=True))
        response.close()

    def test_restore_invalidates_only_changed_action_dependencies(self):
        artifact = self.make_result()
        for identifier, section in (("response-action", "response"), ("purpose-action", "purpose")):
            artifact = self.command(artifact, "plan_action", {"id": identifier, "title": "内部核对", "target": {"kind": "work", "id": self.work["id"], "label": "本工作"}, "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "核对记录", "section_ids": [section]})
        artifact = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": artifact["version"], "section_edits": [{"id": "response", "body": "修订后的回应"}]})
        for identifier in ("response-action", "purpose-action"):
            artifact = self.command(artifact, "approve_action", {"action_id": identifier, "scope": "当前段落的内部核对"})
        restored = self.post(f"/api/business-results/{artifact['id']}/restore", {"base_version": artifact["version"], "restore_version": 1})
        actions = {item["id"]: item for item in restored["input_snapshot"]["business"]["actions"]}
        self.assertFalse(actions["response-action"]["approval"]["valid"])
        self.assertTrue(actions["purpose-action"]["approval"]["valid"])
        self.assertEqual(restored["input_snapshot"]["business"]["changes"], [{"id": "response", "reason": "plan_restored"}])

    def test_append_and_exclude_source_reparse_only_the_selected_business_input(self):
        def material(identifier):
            return self.post("/api/materials", {"title": "隔离原生观察表", "filename": "observations.csv", "work_id": self.work["id"],
                                               "text": "平台,原帖,原文,取得时间,类型\n测试渠道," + identifier + ",活动反馈,2026-11-03T09:00:00+08:00,original\n"})
        first, second = material("post-1"), material("post-2")
        mapping = {"target": "observations", "columns": {"platform": "平台", "native_id": "原帖", "text": "原文", "captured_at": "取得时间", "kind": "类型"}}
        artifact = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "service_response", "inputs": {"goal": "对照活动诉求", "as_of": "2026-11-03T12:00:00+08:00"}, "tables": [{**mapping, "material_id": first["id"]}]})
        initial_source = artifact["input_snapshot"]["business"]["input_sources"][0]
        edited = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "section_edits": [{"id": "response", "body": "保留的人工回应，不随补充样本变化。"}]})
        appended = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 2, "input_changes": {"source_changes": [
            {"op": "append", "input_id": "additional-observations", "input_snapshot_revision": 1, "new_ref": {"kind": "material", "id": second["id"], "version": second["sha256"]}, "binding": mapping, "reason": "补入同窗口另一来源"}]}})
        self.assertEqual(len(appended["input_snapshot"]["business"]["inputs"]["observations"]), 2)
        self.assertIn("保留的人工回应", appended["body"])
        excluded = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 3, "input_changes": {"source_changes": [
            {"op": "exclude", "input_id": initial_source["input_id"], "input_snapshot_revision": 2, "old_ref": initial_source["ref"], "reason": "原表被确认不属于当前活动，保历史而排除本次"}]}})
        business = excluded["input_snapshot"]["business"]
        self.assertEqual([item["native_id"] for item in business["inputs"]["observations"]], ["post-2"])
        self.assertEqual([item["input_id"] for item in business["input_sources"]], ["additional-observations"])
        self.assertEqual([item["material_id"] for item in business["table_sources"]], [second["id"]])
        self.assertIn("保留的人工回应", excluded["body"])
        self.assertEqual(len(business["source_changes"]), 2)

    def test_measurement_job_forwards_cancellation(self):
        from oevira.business_analysis_economics import BusinessAnalysisCancelled
        with self.assertRaises(BusinessAnalysisCancelled):
            business_execution.evaluate_business("attribution_analysis", {}, is_cancelled=lambda: True)

    def test_appending_a_source_preserves_existing_unbound_human_observations(self):
        human = {"platform": "人工收录", "native_id": "original-human-record", "text": "用户原有的现场记录", "captured_at": "2026-11-03T09:00:00+08:00", "kind": "original"}
        artifact = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "service_response", "inputs": {"goal": "保留现场记录并补充平台样本", "as_of": "2026-11-03T12:00:00+08:00", "observations": [human]}})
        material = self.post("/api/materials", {"title": "追加原件", "filename": "observations.csv", "work_id": self.work["id"], "text": "渠道,原帖,原文,时间\n测试渠道,p2,新增样本,2026-11-03T10:00:00+08:00\n"})
        reference = {"kind": "material", "id": material["id"], "version": material["sha256"]}
        appended = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "input_changes": {"source_changes": [{"op": "append", "input_id": "new-sample", "input_snapshot_revision": 1, "new_ref": reference, "reason": "补充明确来源", "binding": {"target": "observations", "columns": {"platform": "渠道", "native_id": "原帖", "text": "原文", "captured_at": "时间"}}}]}})
        self.assertEqual(appended["input_snapshot"]["business"]["inputs"]["observations"][0], human)
        self.assertEqual(len(appended["input_snapshot"]["business"]["inputs"]["observations"]), 2)
        excluded = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 2, "input_changes": {"source_changes": [{"op": "exclude", "input_id": "new-sample", "input_snapshot_revision": 2, "old_ref": reference, "reason": "新增样本不属于本次范围"}]}})
        self.assertEqual(excluded["input_snapshot"]["business"]["inputs"]["observations"], [human])

    def test_cancel_queued_action_preserves_original_receipt_identity(self):
        artifact = self.make_result()
        execution = {"kind": "artifact_export", "artifact_id": artifact["id"], "version": 1, "format": "pdf"}
        planned = self.command(artifact, "plan_action", {"id": "file-action", "title": "生成文件", "target": {"kind": "artifact", "id": artifact["id"], "label": "原成果"}, "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "原版PDF", "execution": execution})
        path = f"/api/business-results/{artifact['id']}/actions/file-action/execute"
        payload = {"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"}
        queued = self.request("post", path, json=payload, headers={"Idempotency-Key": "queued-to-cancel"})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        with patch.object(business_execution, "_check_sources", side_effect=AssertionError("A cancelled plan must not depend on continued source access")):
            cancelled = self.command(planned, "cancel_action", {"action_id": "file-action", "reason": "这项文件已不需要，停止尚未执行的动作"})
        self.assertEqual(cancelled["input_snapshot"]["business"]["actions"][0]["state"], "cancelled")
        job = self.request("get", "/api/jobs/" + queued.json["data"]["id"]).json["data"]
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM exports")["n"], 0)
        replay = self.request("post", path, json=payload, headers={"Idempotency-Key": "queued-to-cancel"})
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["id"], queued.json["data"]["id"])
        repeated = self.request("post", path, json={**payload, "base_version": cancelled["version"]})
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json["error"]["code"], "ACTION_CANCELLED")

    def test_cancel_paused_action_on_archived_work_uses_one_control_transaction(self):
        artifact = self.make_result()
        execution = {"kind": "artifact_export", "artifact_id": artifact["id"], "version": 1, "format": "pdf"}
        planned = self.command(artifact, "plan_action", {"id": "stop-paused", "title": "停止已暂停的文件任务", "target": {"kind": "artifact", "id": artifact["id"], "label": "源版成果"}, "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "PDF文件", "execution": execution})
        queued = self.request("post", f"/api/business-results/{artifact['id']}/actions/stop-paused/execute", json={"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        self.post(f"/api/jobs/{job_id}/pause", {"operation_id": "pause-before-archive", "base_control_revision": 0})
        archived = self.request("patch", f"/api/works/{self.work['id']}", json={"base_version": 1, "status": "archived"})
        self.assertEqual(archived.status_code, 200, archived.get_data(as_text=True))
        cancelled = self.command(planned, "cancel_action", {"action_id": "stop-paused", "reason": "归档工作后仍需停止原任务"})
        row = self.store.row("SELECT status,pause_requested,cancel_requested,control_revision FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(dict(row), {"status": "cancelled", "pause_requested": 0, "cancel_requested": 1, "control_revision": 2})
        self.assertEqual(cancelled["input_snapshot"]["business"]["actions"][0]["state"], "cancelled")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM job_control_receipts WHERE job_id=? AND action='cancel'", (job_id,))["n"], 1)
        self.assertFalse(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM exports")["n"], 0)

    def test_automated_edits_cannot_bypass_human_content_protection(self):
        from oevira.api import ApiError
        artifact = self.make_result()
        revised = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "section_edits": [{"id": "response", "body": "用户亲自写好的回应。"}], "confirm_sections": ["response"]})
        with self.assertRaises(ApiError) as caught:
            business_execution.revise_business_result(self.store, self.identity, artifact["id"], {"base_version": 2, "section_edits": [{"id": "response", "body": "自动改写覆盖"}]}, "model-edit-protected", origin="intelligence")
        self.assertEqual(caught.exception.code, "CONFIRMED_SECTION_AFFECTED")
        with self.assertRaises(ApiError) as caught:
            business_execution.revise_business_result(self.store, self.identity, artifact["id"], {"base_version": 2, "confirm_sections": []}, "model-clear-confirmations", origin="intelligence")
        self.assertEqual(caught.exception.code, "HUMAN_REVIEW_REQUIRED")
        automated = business_execution.revise_business_result(self.store, self.identity, artifact["id"], {"base_version": 2, "section_edits": [{"id": "purpose", "body": "未锁定部分的自动草稿"}]}, "model-edit-unlocked", origin="intelligence")
        self.assertIn("用户亲自写好的回应", automated["body"])
        self.assertNotIn("purpose", automated["input_snapshot"]["business"]["manual_sections"])
        self.assertEqual(revised["input_snapshot"]["business"]["confirmed_sections"], automated["input_snapshot"]["business"]["confirmed_sections"])

    def test_revise_rechecks_work_after_outside_transaction_computation(self):
        artifact = self.make_result()
        evaluate = business_execution.evaluate_business
        def concurrent_update(operation, inputs, **kwargs):
            result = evaluate(operation, inputs, **kwargs)
            response = self.request("patch", f"/api/works/{self.work['id']}", json={"base_version": 1, "goal": "用户已改变本次要求"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return result
        with patch.object(business_execution, "evaluate_business", side_effect=concurrent_update):
            rejected = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "input_changes": {"goal": "按旧要求重新分析"}}, status=(409,))
        self.assertEqual(rejected["code"], "WORK_VERSION_CONFLICT")
        self.assertEqual(self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]["version"], 1)

    def test_model_source_alias_cannot_bypass_prepared_material_scope(self):
        from oevira.api import ApiError
        artifact = self.make_result()
        material = self.post("/api/materials", {"title": "本次明确选择的观察表", "filename": "observations.csv", "work_id": self.work["id"],
                                               "text": "渠道,原帖,原文,时间\n隔离来源,p1,原文样本,2026-11-03T09:00:00+08:00\n"})
        payload = {"base_version": 1, "input_changes": {"source_changes": [{"op": "append", "input_id": "observations-selected", "input_snapshot_revision": 1,
            "new_ref": {"kind": "material", "id": material["id"], "version": material["sha256"]}, "reason": "采用本次选定原件", "binding": {"target": "observations", "columns": {"platform": "渠道", "native_id": "原帖", "text": "原文", "captured_at": "时间"}}}]}}
        with patch.object(business_execution, "_resolve_tables", side_effect=AssertionError("Task-scope refusal must precede reading source contents")):
            with self.assertRaises(ApiError) as missing:
                business_execution.revise_business_result(self.store, self.identity, artifact["id"], payload, "model-no-scope", origin="intelligence")
            self.assertEqual(missing.exception.code, "INTELLIGENCE_SOURCE_SCOPE_REQUIRED")
            with self.assertRaises(ApiError) as denied:
                business_execution.revise_business_result(self.store, self.identity, artifact["id"], payload, "model-wrong-scope", origin="intelligence", source_material_scope=set())
            self.assertEqual(denied.exception.code, "INTELLIGENCE_TOOL_SCOPE_CHANGED")
        accepted = business_execution.revise_business_result(self.store, self.identity, artifact["id"], payload, "model-selected-scope", origin="intelligence", source_material_scope={material["id"]})
        self.assertEqual(accepted["input_snapshot"]["business"]["inputs"]["observations"][0]["text"], "原文样本")
        self.assertEqual(accepted["input_snapshot"]["business"]["source_changes"][0]["provenance"], "intelligence_source_revision")

    def test_running_analysis_pauses_then_resumes_same_frozen_inputs(self):
        queued = self.request("post", f"/api/works/{self.work['id']}/business-jobs", json={"operation": "service_response", "inputs": {"goal": "暂停后继续原问题", "as_of": "2026-11-03T12:00:00+08:00"}})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        evaluate = business_execution.evaluate_business
        def pause_during_computation(operation, inputs, **kwargs):
            paused = self.post(f"/api/jobs/{job_id}/pause", {"operation_id": "analysis-pause", "base_control_revision": 0})
            self.assertEqual(paused["status"], "pause_requested")
            return evaluate(operation, inputs, **kwargs)
        with patch.object(business_execution, "evaluate_business", side_effect=pause_during_computation):
            self.assertTrue(run_once(self.app))
        paused = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM artifacts")["n"], 0)
        resumed = self.post(f"/api/jobs/{job_id}/resume", {"operation_id": "analysis-resume", "base_control_revision": 1})
        self.assertEqual(resumed["status"], "queued")
        self.assertTrue(run_once(self.app))
        completed = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM artifacts")["n"], 1)

    def test_pause_after_file_generation_recovers_receipt_without_second_export(self):
        from oevira import business_execution_actions
        artifact = self.make_result()
        execution = {"kind": "artifact_export", "artifact_id": artifact["id"], "version": 1, "format": "pdf"}
        planned = self.command(artifact, "plan_action", {"id": "recovery-file", "title": "保存精确文件", "target": {"kind": "artifact", "id": artifact["id"], "label": "源版成果"}, "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "PDF文件", "execution": execution})
        queued = self.request("post", f"/api/business-results/{artifact['id']}/actions/recovery-file/execute", json={"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        create_export = business_execution_actions._create_export
        def pause_after_dispatch(*args, **kwargs):
            paused = self.post(f"/api/jobs/{job_id}/pause", {"operation_id": "action-pause", "base_control_revision": 0})
            self.assertEqual(paused["status"], "pause_requested")
            return create_export(*args, **kwargs)
        with patch.object(business_execution_actions, "_create_export", side_effect=pause_after_dispatch), patch.object(business_execution_actions, "_insert_version", side_effect=RuntimeError("isolated fault after real export before action receipt")):
            self.assertTrue(run_once(self.app))
        stopped = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(stopped["status"], "paused")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM exports")["n"], 1)

        self.post(f"/api/jobs/{job_id}/resume", {"operation_id": "action-resume", "base_control_revision": 1})
        with patch.object(business_execution_actions, "_create_export", side_effect=AssertionError("The original export receipt must be reused")):
            self.assertTrue(run_once(self.app))
        completed = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(completed["status"], "completed")
        reopened = self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
        action = reopened["input_snapshot"]["business"]["actions"][0]
        self.assertEqual(action["state"], "executed")
        self.assertEqual(len(action["records"]), 1)
        self.assertEqual(action["records"][0]["receipt"]["external_delivery"], "not_sent")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM exports")["n"], 1)

    def test_unknown_browser_effect_cannot_resume_without_owner_receipt(self):
        from oevira.api import ApiError
        from oevira.business_execution_actions import _recover_completed_receipt
        row = {"id": "isolated-unconfirmed-attempt", "workspace_id": self.identity["workspace"]["id"], "user_id": self.identity["user"]["id"]}
        payload = {"dispatch_started_at": "2026-11-10T09:00:00Z", "receipt": None,
                   "execution": {"kind": "browser_action", "session_id": "isolated-browser", "session_version": 1, "page_id": "selected-page", "page_revision": 1, "action": {"type": "press", "key": "Enter"}}}
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaises(ApiError) as refused:
                _recover_completed_receipt(connection, row, payload)
        self.assertEqual(refused.exception.code, "ACTION_RECEIPT_LOOKUP_REQUIRED")

    def test_late_action_failure_cannot_replace_a_completed_receipt(self):
        from oevira import business_execution_actions
        artifact = self.make_result()
        execution = {"kind": "artifact_export", "artifact_id": artifact["id"], "version": 1, "format": "pdf"}
        planned = self.command(artifact, "plan_action", {"id": "late-failure", "title": "已生成文件的迟到回执", "target": {"kind": "artifact", "id": artifact["id"], "label": "确切源版"}, "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "负责人"}, "deliverable": "PDF", "execution": execution})
        queued = self.request("post", f"/api/business-results/{artifact['id']}/actions/late-failure/execute", json={"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        create_export = business_execution_actions._create_export
        def completion_wins_before_old_error(*args, **kwargs):
            generated = self.app.make_response(create_export(*args, **kwargs)).get_json()["data"]
            with self.store.transaction() as connection:
                row = connection.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()
                payload = json.loads(row["payload_json"])
                payload["receipt"] = {"kind": "artifact_export", "state": "file_generated", "export": generated, "external_delivery": "not_sent", "business_outcome": "not_observed"}
                payload["receipt_writer"] = "completed_attempt"
                connection.execute("UPDATE jobs SET status='completed',payload_json=?,progress_completed=1 WHERE id=?", (json.dumps(payload), job_id))
            raise RuntimeError("isolated late failure after a durable completed receipt")
        with patch.object(business_execution_actions, "_create_export", side_effect=completion_wins_before_old_error):
            self.assertTrue(run_once(self.app))
        saved = self.store.row("SELECT status,payload_json,error_code FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(json.loads(saved["payload_json"])["receipt_writer"], "completed_attempt")
        self.assertIsNone(saved["error_code"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM exports")["n"], 1)

    def test_case_service_report_references_one_authoritative_event_history(self):
        case = self.post("/api/business/cases", {"work_id": self.work["id"], "title": "活动服务问题", "question": "核对排队与现场安排", "source_refs": []})["case"]
        promised = self.post(f"/api/business/cases/{case['id']}/events", {"base_version": 1, "command": "promise", "payload": {"deliverable": "核对排班并给出处理意见", "responsible": "服务负责人", "fulfilment_basis": "已存当日排班记录"}})
        artifact = self.make_result()
        invalid = self.command(artifact, "record_service_event", {"case_id": case["id"], "event": "fulfilled", "description": "没有Case履约记录却声称已完成"}, status=(409,))
        self.assertEqual(invalid["code"], "CASE_EVENT_REQUIRED")
        mismatched = self.command(artifact, "record_service_event", {"case_id": case["id"], "case_event_id": promised["event_id"], "case_event_version": 2, "event": "fulfilled"}, status=(409,))
        self.assertEqual(mismatched["code"], "CASE_EVENT_MISMATCH")
        linked = self.command(artifact, "record_service_event", {"case_id": case["id"], "case_event_id": promised["event_id"], "case_event_version": 2, "event": "promised"})
        recorded = linked["input_snapshot"]["business"]["service_events"][0]
        self.assertEqual(recorded["case_event_id"], promised["event_id"])
        self.assertIsNone(recorded["occurred_at"])
        self.assertIn("发生时间未提供", linked["body"])
        fulfilled = self.post(f"/api/business/cases/{case['id']}/events", {"base_version": 2, "command": "record_fulfilment", "payload": {"commitment_id": promised["event_id"], "note": "负责人记录已核对当日安排", "occurred_at": "2026-11-03T14:00:00+08:00"}})
        report = self.command(linked, "record_service_event", {"case_id": case["id"], "case_event_id": fulfilled["event_id"], "case_event_version": 3})
        self.assertEqual(report["input_snapshot"]["business"]["service_events"][1]["commitment_id"], promised["event_id"])
        self.assertEqual(self.store.row("SELECT version FROM business_cases WHERE id=?", (case["id"],))["version"], 3)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM business_case_events WHERE case_id=?", (case["id"],))["n"], 2)
        self.assertFalse(report["input_snapshot"]["business"]["service_events"][1]["resolution_verified"])

    def test_campaign_revision_reuses_saved_schedule_without_private_input_markers(self):
        from test_campaign import _schedule_revision_fixture
        inputs, _ = _schedule_revision_fixture()
        artifact = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "campaign_schedule", "inputs": inputs})
        previous = next(section["data"] for section in artifact["input_snapshot"]["business"]["generated_sections"] if section["id"] == "campaign_schedule")
        confirmed = self.post(f"/api/business-results/{artifact['id']}/revisions", {"base_version": 1, "confirm_sections": ["campaign_schedule"]})
        change = {"base_version": confirmed["version"], "input_changes": {"brief": {"budget": {"maximum": 200}}}}
        review = self.post(f"/api/business-results/{artifact['id']}/revisions", change, status=(409,))
        self.assertEqual(review["code"], "CONFIRMED_SECTION_AFFECTED")
        updated = self.post(f"/api/business-results/{artifact['id']}/revisions", {**change, "accept_sections": ["campaign_schedule"]})
        business = updated["input_snapshot"]["business"]
        self.assertEqual(business["inputs"]["brief"]["goal"], inputs["brief"]["goal"])
        self.assertEqual(business["inputs"]["brief"]["timezone"], "UTC")
        self.assertEqual(business["inputs"]["brief"]["currency"], "CNY")
        self.assertFalse(any(key.startswith("_") for key in business["inputs"]))
        schedule = next(section["data"] for section in business["generated_sections"] if section["id"] == "campaign_schedule")
        self.assertEqual(schedule["status"], "INFEASIBLE_DECLARED_CONSTRAINTS")
        original_times = {item["id"]: (item["start"], item["end"], item["resource_ids"]) for item in previous["tasks"]}
        for task in schedule["tasks"]:
            self.assertEqual((task["start"], task["end"], task["resource_ids"]), original_times[task["id"]])
        self.app = self.new_app()
        self.client = self.app.test_client()
        reopened = self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
        self.assertEqual(reopened["body"], updated["body"])

    def test_confirmed_file_action_executes_existing_export_and_records_real_receipt(self):
        artifact = self.make_result()
        execution = {"kind": "artifact_export", "artifact_id": artifact["id"], "version": 1, "format": "pdf"}
        planned = self.command(artifact, "plan_action", {"id": "delivery-1", "title": "生成客户核对文件",
            "target": {"kind": "artifact", "id": artifact["id"], "label": "服务回应成果"},
            "responsible": {"kind": "member", "id": self.identity["user"]["id"], "label": "本工作负责人"},
            "deliverable": "同版PDF核对文件", "execution": execution, "section_ids": ["response"]})
        queued = self.request("post", f"/api/business-results/{artifact['id']}/actions/delivery-1/execute",
            json={"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"},
            headers={"Idempotency-Key": "actual-file-action"})
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(job["status"], "completed", job)
        current = self.request("get", f"/api/artifacts/{artifact['id']}").json["data"]
        action = current["input_snapshot"]["business"]["actions"][0]
        self.assertEqual(action["state"], "executed")
        receipt = action["records"][0]["receipt"]
        self.assertEqual(receipt["state"], "file_generated")
        self.assertEqual(receipt["external_delivery"], "not_sent")
        download = self.request("get", receipt["export"]["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.get_data().startswith(b"%PDF"))
        download.close()
        replay = self.request("post", f"/api/business-results/{artifact['id']}/actions/delivery-1/execute",
            json={"base_version": planned["version"], "execution": execution, "confirmation": "execute_this_exact_action"},
            headers={"Idempotency-Key": "actual-file-action"})
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["id"], job_id)
        self.assertFalse(run_once(self.app))

    def test_xlsx_formula_cache_is_not_adopted_as_verified_business_value(self):
        import xlsxwriter
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("经营表")
        sheet.write_row(0, 0, ["开始", "结束", "销售额"])
        sheet.write_row(1, 0, ["2026-01-01", "2026-01-07", 100])
        sheet.write_row(2, 0, ["2026-01-08", "2026-01-14"])
        sheet.write_formula(2, 2, "=10+20", None, 999)
        sheet.write_row(3, 0, ["2026-01-15", "2026-01-21"])
        sheet.write_formula(3, 2, "=UNSUPPORTED(5)", None, 888)
        sheet.write_row(4, 0, ["2026-01-22", "2026-01-28"])
        sheet.write_formula(4, 2, "=1/3", None, 777)
        workbook.close()
        response = self.request("post", "/api/materials", data={"file": (io.BytesIO(output.getvalue()), "经营.xlsx"), "title": "含过期公式缓存的隔离原件", "work_id": self.work["id"]})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        source = response.json["data"]
        preview = self.request("get", f"/api/materials/{source['id']}/business-table?work_id={self.work['id']}")
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        columns = preview.json["data"]["sheets"][0]["columns"]
        self.assertTrue(columns[2]["needs_recalculation"])
        self.assertEqual(columns[2]["cached_formula_count"], 3)
        result = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "operating_analysis", "inputs": {}, "tables": [{
            "material_id": source["id"], "material_sha256": preview.json["data"]["sha256"], "sheet": "经营表", "target": "observations", "source_row_field": "id",
            "columns": {"period.start": "开始", "period.end": "结束", "metric.value": {"column": "销售额", "type": "decimal"}},
            "row_defaults": {"entity": {"id": "test-store", "name": "隔离样例门店"}, "metric": {"key": "sales", "definition": "原件声明销售额", "unit": "CNY", "currency": "CNY", "basis": "actual", "denominator": None, "comparison_dimensions": []}, "period": {"frequency": "week", "timezone": "Asia/Shanghai"}, "scope": {}, "coverage": {"observed": 7, "eligible": 7}},
        }]})
        business = result["input_snapshot"]["business"]
        self.assertEqual(business["inputs"]["observations"][0]["metric"]["value"], "100")
        self.assertEqual(business["inputs"]["observations"][1]["metric"]["value"], "30")
        self.assertIsNone(business["inputs"]["observations"][2]["metric"]["value"])
        from decimal import Decimal
        ratio = Decimal(business["inputs"]["observations"][3]["metric"]["value"])
        self.assertLess(abs(ratio - Decimal(1) / Decimal(3)), Decimal("1e-15"))
        self.assertIn("未能取得可用重算值", result["body"])
        self.assertNotIn("999", result["body"])
        self.assertEqual(business["table_sources"][0]["recalculated_formula_cells"][0]["formula"], "=10+20")
        self.assertEqual(business["table_sources"][0]["recalculated_formula_cells"][0]["computed_value"], {"kind": "number", "value": "30", "verified": True})
        self.assertEqual(business["table_sources"][0]["uncalculated_formula_cells"][0]["formula"], "=UNSUPPORTED(5)")

    def test_client_export_uses_frozen_safe_view_without_internal_costs(self):
        artifact = self.post(f"/api/works/{self.work['id']}/business-results", {"operation": "strategy_proposal", "inputs": {
            "brief": {"goal": "为门店活动比较一项可执行方案", "currency": "CNY", "budget": {"maximum": 5000}},
            "candidates": [{"id": "route-one", "title": "体验路线", "mechanism": "先提供体验再收集自愿反馈", "actions": ["举办试饮"],
                "cost_lines": [{"id": "supplier-line", "amount": 731.46, "currency": "CNY", "status": "quote", "category": "内部供应商底价",
                                "client_quote": {"amount": 1270, "currency": "CNY", "status": "quote", "label": "客户活动报价"}}]}],
        }})
        self.assertIn("731.46", artifact["body"])
        internal = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": 1, "format": "html"})
        client = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": 1, "format": "html", "audience": "client"}, key="client-delivery")
        self.assertEqual(client["audience"], "client")
        public_file = self.request("get", client["download_url"])
        self.assertEqual(public_file.status_code, 200)
        text = public_file.get_data(as_text=True)
        public_file.close()
        self.assertIn("1270", text)
        self.assertNotIn("731.46", text)
        self.assertNotIn("内部供应商底价", text)
        private_file = self.request("get", internal["download_url"])
        self.assertIn("731.46", private_file.get_data(as_text=True))
        private_file.close()
        with self.store.transaction() as connection:
            connection.execute("UPDATE exports SET status='interrupted' WHERE id=?", (client["id"],))
        self.app = self.new_app()
        self.client = self.app.test_client()
        recovered = self.request("get", client["download_url"])
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        self.assertEqual(recovered.get_data(as_text=True), text)
        recovered.close()
        replay = self.post(f"/api/artifacts/{artifact['id']}/exports", {"version": 1, "format": "html", "audience": "client"}, key="client-delivery")
        self.assertEqual(replay["id"], client["id"])


if __name__ == "__main__":
    unittest.main()
