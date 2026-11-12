from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from oevira import create_app
from oevira.api import ApiError
from oevira.auth import issue_local_capability, utc_now
from oevira.business_cases import require_case_event


BASE_URL = "http://127.0.0.1:8325"


class BusinessCaseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-business-cases-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8325)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "服务Case生命周期测试"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def create_case(self, *, key="case-create", work_id=None):
        payload = {
            "title": "服务跟进",
            "question": "门店咨询尚需核实和回复。",
            "source_refs": [],
        }
        if work_id:
            payload["work_id"] = work_id
        response = self.request("post", "/api/business/cases", json=payload, headers=self.auth_headers(key))
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]["case"]

    def command(self, case_id, version, action, payload, key):
        return self.request(
            "post", f"/api/business/cases/{case_id}/events",
            json={"base_version": version, "command": action, "payload": payload},
            headers=self.auth_headers(key),
        )

    def test_service_lifecycle_idempotency_resolution_basis_and_problem_revision(self) -> None:
        created = self.create_case()
        case_id = created["id"]
        self.assertIsNone(created["subject"]["id"])
        self.assertEqual(created["submission_id"], case_id)
        replay_create = self.request(
            "post", "/api/business/cases",
            json={"title": "服务跟进", "question": "门店咨询尚需核实和回复。", "source_refs": []},
            headers=self.auth_headers("case-create"),
        )
        self.assertEqual(replay_create.status_code, 200)
        self.assertEqual(replay_create.json["data"]["case"]["id"], case_id)
        self.assertTrue(replay_create.json["data"]["replayed"])

        acknowledge = self.command(case_id, 1, "acknowledge", {"note": "已收到用户反馈"}, "case-ack")
        self.assertEqual(acknowledge.status_code, 200, acknowledge.get_data(as_text=True))
        self.assertEqual(acknowledge.json["data"]["case"]["version"], 2)
        self.assertTrue(acknowledge.json["data"]["case"]["lifecycle"]["acknowledged"])

        due_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
        promise = self.command(
            case_id, 2, "promise",
            {"deliverable": "核实门店服务记录", "responsible": "客户协调人", "fulfilment_basis": "保存核验材料", "due_at": due_at},
            "case-promise",
        )
        self.assertEqual(promise.status_code, 200, promise.get_data(as_text=True))
        promise_event_id = promise.json["data"]["event_id"]
        promise_payload = promise.json["data"]["case"]["lifecycle"]["commitments"][0]
        self.assertEqual(promise_payload["responsible_identity_state"], "user_declared")
        self.assertFalse(promise_payload["external_commitment_sent"])

        fulfilment = self.command(
            case_id, 3, "record_fulfilment",
            {"commitment_id": promise_event_id, "note": "用户记录已提交核验材料"},
            "case-fulfilment",
        )
        self.assertEqual(fulfilment.status_code, 200, fulfilment.get_data(as_text=True))
        fulfilment_id = fulfilment.json["data"]["event_id"]
        self.assertEqual(
            fulfilment.json["data"]["case"]["lifecycle"]["commitments"][0]["fulfilments"][-1]["payload"]["fulfilment_verification"],
            "user_reported_not_independently_verified",
        )

        feedback = self.command(case_id, 4, "feedback", {"note": "反馈称问题暂时改善"}, "case-feedback")
        self.assertEqual(feedback.status_code, 200, feedback.get_data(as_text=True))
        feedback_id = feedback.json["data"]["event_id"]
        occurred = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        observation_end = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        resolution_request = {
            "base_version": 5,
            "command": "resolve",
            "payload": {
                "assessment": "用户反馈在观察窗后仍报告改善。",
                "observation_end": observation_end,
                "basis_event_ids": [fulfilment_id, feedback_id],
                "occurred_at": occurred,
            },
        }
        resolved = self.request(
            "post", f"/api/business/cases/{case_id}/events", json=resolution_request,
            headers=self.auth_headers("case-resolution"),
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.json["data"]["case"]["lifecycle"]["resolution"]["independent_verification"], "not_established_by_user_assessment")

        before_replay = self.store.row("SELECT COUNT(*) AS count FROM business_case_events WHERE case_id=?", (case_id,))["count"]
        replay_resolution = self.request(
            "post", f"/api/business/cases/{case_id}/events", json=resolution_request,
            headers=self.auth_headers("case-resolution"),
        )
        self.assertEqual(replay_resolution.status_code, 200)
        self.assertTrue(replay_resolution.json["data"]["replayed"])
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM business_case_events WHERE case_id=?", (case_id,))["count"],
            before_replay,
        )
        stale = self.command(case_id, 5, "feedback", {"note": "旧版本写入"}, "case-stale-feedback")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "VERSION_CONFLICT")

        updated = self.request(
            "patch", f"/api/business/cases/{case_id}",
            json={"base_version": 6, "question": "新问题：发现后续重复发生。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.json["data"]["review"]["status"], "needs_review")
        self.assertIsNone(updated.json["data"]["lifecycle"]["resolution"])
        self.assertIsNone(updated.json["data"]["lifecycle"]["response"])
        self.assertEqual(updated.json["data"]["lifecycle"]["problem_revisions"][-1]["kind"], "revise_problem")

        stale_resolution = self.command(
            case_id, 7, "resolve",
            {"assessment": "错误沿用旧问题结论", "observation_end": observation_end,
             "basis_event_ids": [fulfilment_id, feedback_id], "occurred_at": occurred},
            "case-old-problem-resolution",
        )
        self.assertEqual(stale_resolution.status_code, 400)
        self.assertEqual(stale_resolution.json["error"]["code"], "RESOLUTION_EVIDENCE_REQUIRED")

        new_feedback = self.command(case_id, 7, "feedback", {"note": "新问题已重新观察"}, "case-new-problem-feedback")
        self.assertEqual(new_feedback.status_code, 200, new_feedback.get_data(as_text=True))
        next_end = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        next_time = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        fresh_resolution = self.command(
            case_id, 8, "resolve",
            {"assessment": "新问题仅按新的反馈评价", "observation_end": next_end,
             "basis_event_ids": [new_feedback.json["data"]["event_id"]], "occurred_at": next_time},
            "case-new-problem-resolution",
        )
        self.assertEqual(fresh_resolution.status_code, 200, fresh_resolution.get_data(as_text=True))

        with self.store.transaction(immediate=False) as connection:
            case_row, event = require_case_event(
                connection, self.identity, case_id,
                new_feedback.json["data"]["event_id"], case_version=8,
            )
            self.assertEqual(case_row["id"], case_id)
            self.assertEqual(event["kind"], "feedback")
            self.assertEqual(event["case_version"], 8)
            with self.assertRaises(ApiError) as wrong_version:
                require_case_event(
                    connection, self.identity, case_id,
                    new_feedback.json["data"]["event_id"], case_version=7,
                )
            self.assertEqual(wrong_version.exception.status, 404)
            _current_case, old_fulfilment = require_case_event(
                connection, self.identity, case_id, fulfilment_id, case_version=4,
            )
            self.assertEqual(old_fulfilment["case_context_at_event"]["title"], "服务跟进")
            self.assertEqual(old_fulfilment["case_context_at_event"]["question"], "门店咨询尚需核实和回复。")
            self.assertEqual(old_fulfilment["case_context_at_event"]["case_version"], 4)

        close = self.command(case_id, 9, "close", {"note": "关闭记录，不代表已独立证实解决"}, "case-close")
        self.assertEqual(close.status_code, 200, close.get_data(as_text=True))
        self.assertEqual(close.json["data"]["case"]["state"], "closed")
        closed_edit = self.request(
            "patch", f"/api/business/cases/{case_id}",
            json={"base_version": 10, "question": "不允许绕过重新打开"}, headers=self.auth_headers(),
        )
        self.assertEqual(closed_edit.status_code, 409)
        self.assertEqual(closed_edit.json["error"]["code"], "CASE_CLOSED")
        reopened = self.command(
            case_id, 10, "reopen", {"note": "发生复发，重新观察", "reason_kind": "recurrence"}, "case-reopen",
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["case"]["state"], "open")
        old_recurrence_basis = self.command(
            case_id, 11, "resolve",
            {"assessment": "不得用重开前反馈结案", "observation_end": next_end,
             "basis_event_ids": [new_feedback.json["data"]["event_id"]], "occurred_at": next_time},
            "case-pre-reopen-resolution",
        )
        self.assertEqual(old_recurrence_basis.status_code, 400)
        self.assertEqual(old_recurrence_basis.json["error"]["code"], "RESOLUTION_EVIDENCE_REQUIRED")

    def test_saved_response_is_not_sent_and_problem_edit_invalidates_current_response(self) -> None:
        case = self.create_case(key="response-case")
        case_id = case["id"]
        now = utc_now()
        artifact_id = str(uuid.uuid4())
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts(id,workspace_id,work_id,owner_user_id,title,current_version,created_at,updated_at) VALUES(?,?,NULL,?,?,1,?,?)",
                (artifact_id, workspace_id, user_id, "保存的回应草稿", now, now),
            )
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by,parent_version) VALUES(?,?,1,?,'仅为保存的回应，不代表已外发。','[]','{}','回应草稿',?,?,NULL)",
                (artifact_id, workspace_id, "保存的回应草稿", now, user_id),
            )
        prepared = self.command(
            case_id, 1, "prepare_response",
            {"artifact_id": artifact_id, "artifact_version": 1, "channel": "email"}, "case-response-prepare",
        )
        self.assertEqual(prepared.status_code, 200, prepared.get_data(as_text=True))
        self.assertEqual(prepared.json["data"]["case"]["lifecycle"]["response"]["state"], "draft_not_sent")

        read_too_early = self.command(case_id, 2, "record_response", {"state": "reported_read", "note": "错误地先记已读"}, "case-read-too-early")
        self.assertEqual(read_too_early.status_code, 409)
        self.assertEqual(read_too_early.json["error"]["code"], "CASE_RESPONSE_STATE_CONFLICT")
        sent = self.command(case_id, 2, "record_response", {"state": "reported_sent", "note": "用户记录已发送，未独立核验"}, "case-response-sent")
        self.assertEqual(sent.status_code, 200, sent.get_data(as_text=True))
        self.assertEqual(sent.json["data"]["case"]["lifecycle"]["response"]["business_outcome"], "not_established_by_response")
        read = self.command(case_id, 3, "record_response", {"state": "reported_read", "note": "用户记录收到已读回执"}, "case-response-read")
        self.assertEqual(read.status_code, 200, read.get_data(as_text=True))

        revised = self.request(
            "patch", f"/api/business/cases/{case_id}",
            json={"base_version": 4, "question": "回应后发现问题范围需要更正。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        self.assertIsNone(revised.json["data"]["lifecycle"]["response"])
        response_after_revision = self.command(
            case_id, 5, "record_response", {"state": "reported_sent", "note": "不得将旧回应算作新问题回应"},
            "case-record-response-after-problem-revision",
        )
        self.assertEqual(response_after_revision.status_code, 409)
        self.assertEqual(response_after_revision.json["error"]["code"], "CASE_RESPONSE_REQUIRED")

    def test_case_sources_and_case_relations_stay_inside_their_client_work(self) -> None:
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        commission_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for index, commission_id in enumerate(commission_ids):
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                    (commission_id, workspace_id, f"Case来源客户{index}", user_id, now, now),
                )
                connection.execute(
                    "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'owner',1)",
                    (commission_id, user_id),
                )
        work_ids = []
        for index, commission_id in enumerate(commission_ids):
            created = self.request(
                "post", "/api/works",
                json={"title": f"Case来源工作{index}", "goal": "测试私有来源边界", "commission_id": commission_id},
                headers=self.auth_headers(f"case-source-work-{index}"),
            )
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            work_ids.append(created.json["data"]["id"])

        material_ids = []
        for index, work_id in enumerate(work_ids):
            material_id = str(uuid.uuid4())
            content = f"客户{index}服务Case原始来源。"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,reason,text,sha256,relative_path,size_bytes,source_url,etag,last_modified,source_json,source_text,created_at) VALUES(?,?,?,?,?,'source.txt','txt','ready',NULL,?,?,?, ?,NULL,NULL,NULL,'{}',NULL,?)",
                    (material_id, workspace_id, user_id, work_id, f"客户{index}Case来源", content, digest,
                     f"objects/{material_id}.txt", len(content.encode("utf-8")), now),
                )
            material_ids.append(material_id)

        unbound_private_source = self.request(
            "post", "/api/business/cases",
            json={"title": "未绑定工作区间的Case", "question": "不能引用客户私有材料。", "source_refs": [{"material_id": material_ids[0]}]},
            headers=self.auth_headers("unbound-case-private-source"),
        )
        self.assertEqual(unbound_private_source.status_code, 403)
        self.assertEqual(unbound_private_source.json["error"]["code"], "CASE_WORK_REQUIRED")
        cross_work_source = self.request(
            "post", "/api/business/cases",
            json={"work_id": work_ids[0], "title": "客户A的Case", "question": "不能引用客户B材料。", "source_refs": [{"material_id": material_ids[1]}]},
            headers=self.auth_headers("case-a-source-b-material"),
        )
        self.assertEqual(cross_work_source.status_code, 403)
        self.assertEqual(cross_work_source.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

        case_a_response = self.request(
            "post", "/api/business/cases",
            json={"work_id": work_ids[0], "title": "客户A服务Case", "question": "门店问题待跟进。", "source_refs": [{"material_id": material_ids[0]}]},
            headers=self.auth_headers("case-a-source-a-material"),
        )
        case_b_response = self.request(
            "post", "/api/business/cases",
            json={"work_id": work_ids[1], "title": "客户B服务Case", "question": "另一客户的门店问题。", "source_refs": [{"material_id": material_ids[1]}]},
            headers=self.auth_headers("case-b-source-b-material"),
        )
        self.assertEqual(case_a_response.status_code, 201, case_a_response.get_data(as_text=True))
        self.assertEqual(case_b_response.status_code, 201, case_b_response.get_data(as_text=True))
        case_a = case_a_response.json["data"]["case"]
        case_b = case_b_response.json["data"]["case"]
        self.assertEqual(case_a["source_refs"][0]["source"]["material"]["id"], material_ids[0])
        self.assertEqual(case_b["source_refs"][0]["source"]["material"]["id"], material_ids[1])

        cross_client_link = self.command(
            case_a["id"], 1, "link_case",
            {"related_case_id": case_b["id"], "relation": "related", "note": "跨委托关联拒绝"},
            "case-a-link-case-b",
        )
        self.assertEqual(cross_client_link.status_code, 403, cross_client_link.get_data(as_text=True))
        self.assertEqual(cross_client_link.json["error"]["code"], "CASE_RELATION_SCOPE_MISMATCH")

    def test_case_source_review_and_replacement_preserve_exact_versions(self) -> None:
        created_work = self.request(
            "post", "/api/works",
            json={"title": "Case来源核阅工作", "goal": "核阅并更换确切资料来源"},
            headers=self.auth_headers("case-source-review-work"),
        )
        self.assertEqual(created_work.status_code, 201, created_work.get_data(as_text=True))
        work_id = created_work.json["data"]["id"]
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        now = utc_now()

        def add_material(name: str, content: str) -> str:
            material_id = str(uuid.uuid4())
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            relative_path = Path("objects") / f"{material_id}.txt"
            absolute = self.root / relative_path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(content, encoding="utf-8")
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,reason,text,sha256,relative_path,size_bytes,source_url,etag,last_modified,source_json,source_text,created_at) VALUES(?,?,?,?,?,'case-source.txt','txt','ready',NULL,?,?,?, ?,NULL,NULL,NULL,'{}',NULL,?)",
                    (material_id, workspace_id, user_id, work_id, name, content, digest,
                     str(relative_path), len(content.encode("utf-8")), now),
                )
            return material_id

        material_a = add_material("最初来源", "原始服务记录A")
        material_b = add_material("更正来源", "更正后的服务记录B")
        created_case = self.request(
            "post", "/api/business/cases",
            json={"work_id": work_id, "title": "待核阅来源的Case", "question": "这条来源与本次问题有关吗？", "source_refs": [{"material_id": material_a, "source_native_id": "ticket-A"}]},
            headers=self.auth_headers("case-source-review-create"),
        )
        self.assertEqual(created_case.status_code, 201, created_case.get_data(as_text=True))
        case = created_case.json["data"]["case"]
        self.assertEqual(case["review"]["source_refs_status"], "needs_review")
        self.assertEqual(case["source_refs"][0]["source"]["link_status"], "material_linked_needs_review")

        reviewed = self.command(
            case["id"], 1, "review_sources",
            {"decision": "linked", "accepted_positions": [0], "note": "确认该保存材料支持本Case问题"},
            "case-source-review-linked",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.get_data(as_text=True))
        self.assertEqual(reviewed.json["data"]["case"]["review"]["source_refs_status"], "linked")
        self.assertEqual(reviewed.json["data"]["case"]["source_refs"][0]["source"]["link_status"], "linked_by_user")
        self.assertTrue(reviewed.json["data"]["case"]["lifecycle"]["source_review"]["material_integrity_checked"])

        replay = self.command(
            case["id"], 1, "review_sources",
            {"decision": "linked", "accepted_positions": [0], "note": "确认该保存材料支持本Case问题"},
            "case-source-review-linked",
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["data"]["replayed"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM business_case_events WHERE case_id=?", (case["id"],))["count"], 1)

        changed_problem = self.request(
            "patch", f"/api/business/cases/{case['id']}",
            json={"base_version": 2, "question": "更新后的问题需要重新核对资料。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(changed_problem.status_code, 200, changed_problem.get_data(as_text=True))
        self.assertEqual(changed_problem.json["data"]["review"]["source_refs_status"], "needs_review")
        self.assertEqual(changed_problem.json["data"]["source_refs"][0]["source"]["link_status"], "material_linked_needs_review")
        self.assertEqual(changed_problem.json["data"]["lifecycle"]["source_review"]["status"], "needs_review")

        replaced = self.command(
            case["id"], 3, "replace_sources",
            {"source_refs": [{"material_id": material_b, "source_native_id": "ticket-B"}], "note": "用更正后的原件替换旧来源"},
            "case-source-replacement",
        )
        self.assertEqual(replaced.status_code, 200, replaced.get_data(as_text=True))
        self.assertEqual(replaced.json["data"]["case"]["review"]["source_refs_status"], "needs_review")
        self.assertEqual(replaced.json["data"]["case"]["source_refs"][0]["source"]["material"]["id"], material_b)
        self.assertEqual(replaced.json["data"]["case"]["lifecycle"]["source_review"]["status"], "needs_review")
        versions = self.request("get", f"/api/business/cases/{case['id']}/versions", headers=self.auth_headers())
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(versions.json["data"][1]["snapshot"]["source_refs"][0]["material_id"], material_a)
        self.assertEqual(versions.json["data"][2]["snapshot"]["source_refs"][0]["material_id"], material_a)
        self.assertEqual(versions.json["data"][3]["snapshot"]["source_refs"][0]["material_id"], material_b)

        reviewed_b = self.command(
            case["id"], 4, "review_sources",
            {"decision": "linked", "accepted_positions": [0], "note": "确认更正来源对应本Case问题"},
            "case-source-review-b",
        )
        self.assertEqual(reviewed_b.status_code, 200, reviewed_b.get_data(as_text=True))
        self.assertEqual(reviewed_b.json["data"]["case"]["review"]["source_refs_status"], "linked")


if __name__ == "__main__":
    unittest.main()
