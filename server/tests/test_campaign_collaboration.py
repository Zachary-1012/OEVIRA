from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from oevira import campaign_collaboration as collaboration
from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.api import ApiError
from oevira.business_execution import register_business_execution_routes
from oevira.collaboration import (
    install_collaboration_schema,
    register_collaboration_boundary,
    register_collaboration_routes,
)


class CampaignCollaborationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE artifact_versions(
                artifact_id TEXT, workspace_id TEXT, version INTEGER, title TEXT,
                body TEXT, citations_json TEXT, input_snapshot_json TEXT
            );
            CREATE TABLE materials(
                id TEXT, workspace_id TEXT, owner_user_id TEXT, work_id TEXT,
                title TEXT, filename TEXT, format TEXT, status TEXT, reason TEXT,
                sha256 TEXT
            );
            CREATE TABLE work_materials(work_id TEXT, material_id TEXT, workspace_id TEXT);
            CREATE TABLE artifact_comments(
                id TEXT, artifact_id TEXT, artifact_version INTEGER, workspace_id TEXT,
                author_id TEXT, anchor_json TEXT, body TEXT, state TEXT,
                resolution_json TEXT, version INTEGER, created_at TEXT, updated_at TEXT
            );
            """
        )
        self.connection.execute(
            "INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("CONTRACT", "WS1", "U1", "WORK1", "合作协议", "contract.pdf", "pdf", "ready", None, "contract-sha"),
        )
        self.connection.execute(
            "INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("DELIVERY-1", "WS1", "U1", "WORK1", "首稿", "draft.pdf", "pdf", "ready", None, "draft-sha"),
        )
        self.connection.execute(
            "INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("DELIVERY-2", "WS1", "U1", "WORK1", "修订稿", "revision.pdf", "pdf", "ready", None, "revision-sha"),
        )
        self.connection.execute(
            "INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?)",
            ("SUBMISSION-ARTIFACT", "WS1", 4, "短片首稿", "body text", "[]", "{}"),
        )
        self.connection.commit()
        self.identity = {"user": {"id": "U1"}, "workspace": {"id": "WS1"}}
        self.artifact = {"id": "PLAN1", "work_id": "WORK1", "workspace_id": "WS1"}
        self.current = {"version": 1}
        self.approval_calls = []
        self.business = {
            "operation": "collaboration_plan",
            "result": {
                "sections": [
                    {
                        "id": "collaboration_scope",
                        "data": {
                            "collaborators": [
                                {
                                    "id": "C1",
                                    "project_id": "WORK1",
                                    "creator_id": "creator-1",
                                    "display_label": "Internal candidate",
                                    "client_display_label": "Creator A",
                                    "task_role": "KOS",
                                    "accounts": [
                                        {"platform": "platform-a", "account_id": "private-account-1"}
                                    ],
                                    "quote": {"amount": 3000, "currency": "CNY", "status": "quote"},
                                    "rights": {},
                                    "deliverables": [
                                        {
                                            "id": "D1",
                                            "title": "竖屏短片",
                                            "quantity": 1,
                                            "unit": "条",
                                            "specification": "30秒竖屏视频",
                                            "draft_due": "2026-11-10",
                                            "final_due": "2026-11-15",
                                        }
                                    ],
                                    "source_refs": [],
                                }
                            ]
                        },
                    }
                ]
            },
            "decisions": [],
            "actions": [],
            "costs": [],
        }
        self._patchers = [
            patch.object(
                collaboration,
                "require_material_access",
                side_effect=self._require_material,
            ),
            patch.object(
                collaboration,
                "require_artifact_access",
                side_effect=self._require_artifact,
            ),
            patch.object(
                collaboration,
                "require_artifact_approval",
                side_effect=self._require_approval,
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self.connection.close()

    def _require_material(self, connection, identity, material_id, *, target_work_id=None):
        row = connection.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "missing", 404)
        return row

    def _require_artifact(self, connection, identity, artifact_id, *, write=False):
        if artifact_id != "SUBMISSION-ARTIFACT":
            raise ApiError("NOT_FOUND", "missing", 404)
        return {"id": artifact_id, "work_id": "WORK1", "workspace_id": "WS1"}

    def _require_approval(self, connection, identity, approval_id, artifact_id, version, purpose, scope):
        self.approval_calls.append(
            {
                "approval_id": approval_id,
                "artifact_id": artifact_id,
                "version": version,
                "purpose": purpose,
                "scope": scope,
            }
        )
        return {
            "id": approval_id,
            "approved_by": "REVIEWER",
            "artifact_id": artifact_id,
            "artifact_version": version,
            "purpose": purpose,
            "scope": scope,
            "created_at": "2026-11-03T00:00:00Z",
        }

    def _run(self, command, payload):
        collaboration.apply_collaboration_command(
            self.connection,
            self.identity,
            self.artifact,
            self.current,
            self.business,
            command,
            payload,
            "2026-11-03T00:00:00Z",
        )

    def _terms(self):
        return {
            "quote": {
                "amount": 3000,
                "currency": "CNY",
                "status": "committed",
                "valid_until": "2026-11-20",
                "source_refs": [],
            },
            "rights": {
                "media": ["brand_owned_social"],
                "territories": ["CN-Guangzhou"],
                "term_days": 30,
                "paid_use": False,
                "editing_allowed": False,
                "derivative_use": "不包含",
                "exclusivity": "无排他",
                "withdrawal_terms": "按协议处理",
                "source_refs": [],
            },
            "deliverables": [
                {
                    "id": "D1",
                    "scope_status": "included",
                    "title": "竖屏短片",
                    "quantity": 1,
                    "unit": "条",
                    "specification": "30秒竖屏视频",
                    "draft_due": "2026-11-10",
                    "final_due": "2026-11-15",
                    "acceptance": {"reviewer": "项目审核人", "criteria": ["事实准确"]},
                    "source_refs": [],
                }
            ],
            "availability": {"from": "2026-11-10", "to": "2026-11-20", "source_refs": []},
            "cancellation": {
                "policy": "按书面协议提前通知",
                "notice_deadline": "2026-11-08",
                "fee_amount": 500,
                "fee_currency": "CNY",
                "refund_terms": "未履行部分按合同核算",
                "source_refs": [],
            },
            "source_refs": [],
        }

    def _adopt_and_agree(self):
        self._run(
            "adopt_collaboration_candidate",
            {"candidate_id": "C1", "reason": "项目受众和规格相符"},
        )
        collab = self.business["collaborations"][0]
        self._run(
            "record_collaboration_terms",
            {
                "collaboration_id": collab["id"],
                "terms": self._terms(),
                "agreement_source": {
                    "kind": "material",
                    "material_id": "CONTRACT",
                    "material_sha256": "contract-sha",
                },
            },
        )
        self._run(
            "confirm_collaboration_agreement",
            {"collaboration_id": collab["id"], "approval_id": "APPROVAL-1"},
        )
        return collab

    def test_collaboration_lifecycle_preserves_submissions_reviews_costs_and_cancellation(self) -> None:
        collab = self._adopt_and_agree()
        deliverable = collab["deliverables"][0]
        self.assertEqual(collab["state"], "awaiting_submission")
        self.assertEqual(len(self.business["costs"]), 1)
        self.assertEqual(
            self.approval_calls[0]["purpose"],
            "creator_collaboration_agreement",
        )
        internal_terms = collaboration.render_collaboration_lifecycle(
            self.business,
            "internal",
        )
        self.assertIn("品牌自有社媒", internal_terms)
        self.assertIn("期限 30天", internal_terms)
        self.assertNotIn("['brand_owned_social']", internal_terms)
        self.assertEqual(
            self.approval_calls[0]["scope"]["collaboration_id"], collab["id"]
        )
        committed_id = collab["current_agreement"]["commitment_cost_id"]

        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-1",
                    "material_sha256": "draft-sha",
                },
                "client_disclosure": True,
                "client_summary": "首稿已按约定规格提交，等待核对。",
            },
        )
        first = deliverable["submissions"][0]
        self.assertEqual(first["content_sha256"], "draft-sha")
        self.assertNotIn(
            "private-account-1",
            collaboration.render_collaboration_lifecycle(self.business, "client"),
        )

        self._run(
            "record_collaboration_feedback",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": first["id"],
                "feedback": "片尾需补产品来源说明。",
            },
        )
        self.assertEqual(collab["state"], "feedback_recorded")
        feedback_id = deliverable["feedback"][0]["id"]
        self._run(
            "request_collaboration_revision",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": first["id"],
                "feedback_id": feedback_id,
                "reason": "在约定验收范围内补足来源说明",
            },
        )
        self.assertEqual(collab["state"], "revision_requested")
        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-2",
                    "material_sha256": "revision-sha",
                },
            },
        )
        second = deliverable["submissions"][1]
        self.assertEqual(collab["state"], "deliverable_submission_in_review")
        self.assertEqual(second["supersedes_submission_id"], first["id"])
        with self.assertRaisesRegex(ApiError, "superseded"):
            self._run(
                "accept_collaboration_deliverable",
                {
                    "collaboration_id": collab["id"],
                    "deliverable_id": "D1",
                    "submission_id": first["id"],
                    "approval_id": "APPROVAL-OLD",
                },
            )
        from oevira.campaign_collaboration import approval_scope_for_acceptance
        expected_scope = approval_scope_for_acceptance(collab, deliverable, second)
        approval = self._require_approval(
            self.connection,
            self.identity,
            "APPROVAL-2",
            self.artifact["id"],
            self.current["version"],
            "creator_deliverable_acceptance",
            expected_scope,
        )
        self._run(
            "accept_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": second["id"],
                "approval_id": approval["id"],
            },
        )
        self.assertEqual(
            self.approval_calls[-1]["scope"]["submission_id"], second["id"]
        )
        self.assertEqual(deliverable["state"], "accepted_by_exact_approval")
        self.assertEqual(collab["state"], "deliverables_accepted_by_exact_approval")
        self.assertEqual(len(deliverable["submissions"]), 2)
        accepted_decision = dict(second["review_decision"])
        self._run(
            "record_collaboration_feedback",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": second["id"],
                "feedback": "接收后补充的隔离测试反馈，不改写原审批。",
            },
        )
        with self.assertRaisesRegex(ApiError, "accepted submission"):
            self._run(
                "request_collaboration_revision",
                {
                    "collaboration_id": collab["id"],
                    "deliverable_id": "D1",
                    "submission_id": second["id"],
                    "feedback_id": deliverable["feedback"][-1]["id"],
                    "reason": "不得覆盖已确认的接收记录",
                },
            )
        with self.assertRaisesRegex(ApiError, "accepted submission"):
            self._run(
                "reject_collaboration_deliverable",
                {
                    "collaboration_id": collab["id"],
                    "deliverable_id": "D1",
                    "submission_id": second["id"],
                    "reason": "不得把已接受提交改写为拒收",
                },
            )
        with self.assertRaisesRegex(ApiError, "accepted under the current agreement"):
            self._run(
                "submit_collaboration_deliverable",
                {
                    "collaboration_id": collab["id"],
                    "deliverable_id": "D1",
                    "source": {
                        "kind": "material",
                        "material_id": "DELIVERY-2",
                        "material_sha256": "revision-sha",
                    },
                },
            )
        self.assertEqual(second["review_decision"], accepted_decision)
        self.assertEqual(deliverable["state"], "accepted_by_exact_approval")

        self._run(
            "record_collaboration_cost",
            {
                "collaboration_id": collab["id"],
                "cost": {"id": "ACTUAL-1", "kind": "actual", "amount": 3000, "currency": "CNY", "reason": "用户记录已发生实际费用"},
            },
        )
        self._run(
            "record_collaboration_cost",
            {
                "collaboration_id": collab["id"],
                "cost": {"id": "REFUND-1", "kind": "refund", "amount": 500, "currency": "CNY", "refund_of": "ACTUAL-1", "reason": "用户记录部分退款"},
            },
        )
        with self.assertRaisesRegex(ApiError, "cannot increase"):
            self._run(
                "cancel_collaboration",
                {
                    "collaboration_id": collab["id"],
                    "reason": "取消余额反例",
                    "remaining_commitment": {
                        "cost_record_id": committed_id,
                        "amount": 3500,
                        "reason": "不可高于原承诺",
                    },
                },
            )
        self._run(
            "cancel_collaboration",
            {
                "collaboration_id": collab["id"],
                "reason": "用户记录后续协作取消",
                "remaining_commitment": {"cost_record_id": committed_id, "amount": 0, "reason": "用户记录未履行承诺余额已释放"},
            },
        )
        self.assertEqual(collab["state"], "cancelled")
        self.assertEqual(len(deliverable["submissions"]), 2)
        self.assertEqual(len(self.business["costs"]), 4)
        self.assertEqual(
            collaboration.render_collaboration_lifecycle(self.business, "client"),
            "- 交付进度：首稿已按约定规格提交，等待核对。",
        )
        internal = collaboration.render_collaboration_lifecycle(self.business, "internal")
        self.assertIn("首稿", internal)
        self.assertEqual(self.business["costs"][2]["refund_of"], "ACTUAL-1")

    def test_rejected_deliverable_updates_collaboration_phase_without_external_receipt(self) -> None:
        collab = self._adopt_and_agree()
        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-1",
                    "material_sha256": "draft-sha",
                },
            },
        )
        deliverable = collab["deliverables"][0]
        submission = deliverable["submissions"][0]
        self._run(
            "reject_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": submission["id"],
                "reason": "用户记录该提交不符合约定字幕规格",
            },
        )

        self.assertEqual(deliverable["state"], "rejected_user_recorded")
        self.assertEqual(collab["state"], "deliverable_rejected_user_recorded")
        self.assertEqual(
            submission["review_decision"]["provenance"],
            "user_recorded_decision_not_external_client_confirmation",
        )
        rendered = collaboration.render_collaboration_lifecycle(self.business, "internal")
        self.assertIn("用户记录未接收该交付，非外部客户回执", rendered)

        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-2",
                    "material_sha256": "revision-sha",
                },
            },
        )
        self.assertEqual(collab["state"], "deliverable_submission_in_review")
        self.assertEqual(deliverable["submissions"][1]["supersedes_submission_id"], submission["id"])

    def test_cancel_keeps_historical_agreement_source_without_revalidating_it(self) -> None:
        collab = self._adopt_and_agree()
        historical_source = dict(collab["current_agreement"]["source"])
        with patch.object(
            collaboration,
            "_resolve_source",
            side_effect=AssertionError("default cancellation must not reread the old source"),
        ) as resolve_source:
            self._run(
                "cancel_collaboration",
                {"collaboration_id": collab["id"], "reason": "协议来源已撤回后的隔离取消测试"},
            )
            resolve_source.assert_not_called()

        self.assertEqual(collab["state"], "cancelled")
        self.assertEqual(collab["current_agreement"]["source"], historical_source)
        event = collab["events"][-1]
        self.assertEqual(event["kind"], "collaboration_cancelled")
        self.assertIsNone(event["details"]["cancellation_source"])
        self.assertEqual(
            event["details"]["historical_agreement_source"], historical_source
        )
        self.assertEqual(
            event["details"]["historical_agreement_source_status"],
            "not_revalidated",
        )
        self.assertIn(
            "取消时未重新读取或核验",
            collaboration.render_collaboration_lifecycle(self.business, "internal"),
        )

    def test_explicit_cancellation_source_is_revalidated(self) -> None:
        collab = self._adopt_and_agree()
        selected_source = dict(collab["current_agreement"]["source"])
        with patch.object(
            collaboration,
            "_resolve_source",
            side_effect=ApiError("SOURCE_ACCESS_REVOKED", "revoked", 403),
        ) as resolve_source:
            with self.assertRaisesRegex(ApiError, "revoked"):
                self._run(
                    "cancel_collaboration",
                    {
                        "collaboration_id": collab["id"],
                        "reason": "用户额外指定当前取消依据",
                        "cancellation_source": selected_source,
                    },
                )
            resolve_source.assert_called_once()
        self.assertEqual(collab["state"], "awaiting_submission")
        self.assertEqual(collab["events"][-1]["kind"], "collaboration_agreement_confirmed")

    def test_agreement_amendment_requires_a_new_submission_and_preserves_old_acceptance(self) -> None:
        from oevira.campaign_collaboration import approval_scope_for_acceptance

        collab = self._adopt_and_agree()
        deliverable = collab["deliverables"][0]
        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": deliverable["id"],
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-1",
                    "material_sha256": "draft-sha",
                },
            },
        )
        first = deliverable["submissions"][0]
        first_agreement_id = collab["current_agreement_id"]
        first_scope = approval_scope_for_acceptance(collab, deliverable, first)
        self._require_approval(
            self.connection,
            self.identity,
            "APPROVAL-OLD-SUBMISSION",
            self.artifact["id"],
            self.current["version"],
            "creator_deliverable_acceptance",
            first_scope,
        )
        self._run(
            "accept_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": deliverable["id"],
                "submission_id": first["id"],
                "approval_id": "APPROVAL-OLD-SUBMISSION",
            },
        )
        accepted = dict(first["review_decision"])

        amended_terms = self._terms()
        amended_terms["quote"]["amount"] = 3200
        amended_terms["scope_change_reason"] = "隔离测试：记录明确的协议金额变更"
        self._run(
            "record_collaboration_terms",
            {
                "collaboration_id": collab["id"],
                "terms": amended_terms,
                "agreement_source": {
                    "kind": "material",
                    "material_id": "CONTRACT",
                    "material_sha256": "contract-sha",
                },
            },
        )
        from oevira.campaign_collaboration import approval_scope_for_agreement

        amended_scope = approval_scope_for_agreement(collab)
        self._require_approval(
            self.connection,
            self.identity,
            "APPROVAL-AMENDMENT",
            self.artifact["id"],
            self.current["version"],
            "creator_collaboration_agreement",
            amended_scope,
        )
        self._run(
            "confirm_collaboration_agreement",
            {"collaboration_id": collab["id"], "approval_id": "APPROVAL-AMENDMENT"},
        )
        self.assertNotEqual(collab["current_agreement_id"], first_agreement_id)
        self.assertEqual(first["review_decision"], accepted)
        with self.assertRaisesRegex(ApiError, "older or ambiguous agreement"):
            self._run(
                "accept_collaboration_deliverable",
                {
                    "collaboration_id": collab["id"],
                    "deliverable_id": deliverable["id"],
                    "submission_id": first["id"],
                    "approval_id": "APPROVAL-AMENDMENT",
                },
            )

        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": deliverable["id"],
                "source": {
                    "kind": "material",
                    "material_id": "DELIVERY-2",
                    "material_sha256": "revision-sha",
                },
            },
        )
        second = deliverable["submissions"][1]
        self.assertEqual(second["agreement_id"], collab["current_agreement_id"])
        self.assertEqual(first["review_decision"], accepted)

    def test_collaboration_rejects_wrong_project_or_changed_material_hash(self) -> None:
        with patch.object(
            collaboration,
            "require_material_access",
            side_effect=lambda connection, identity, material_id, **kwargs: connection.execute(
                "SELECT * FROM materials WHERE id=?", (material_id,)
            ).fetchone(),
        ):
            with self.assertRaisesRegex(ApiError, "same Work"):
                collaboration._resolve_source(
                    self.connection,
                    self.identity,
                    "OTHER-WORK",
                    {"kind": "material", "material_id": "DELIVERY-1", "material_sha256": "draft-sha"},
                    "source",
                )
            with self.assertRaisesRegex(ApiError, "hash"):
                collaboration._resolve_source(
                    self.connection,
                    self.identity,
                    "WORK1",
                    {"kind": "material", "material_id": "DELIVERY-1", "material_sha256": "stale-hash"},
                    "source",
                )

    def test_feedback_can_link_an_existing_comment_on_the_exact_submitted_artifact_version(self) -> None:
        collab = self._adopt_and_agree()
        self._run(
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "artifact",
                    "artifact_id": "SUBMISSION-ARTIFACT",
                    "artifact_version": 4,
                },
            },
        )
        submission = collab["deliverables"][0]["submissions"][0]
        self.connection.execute(
            "INSERT INTO artifact_comments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "COMMENT-1",
                "SUBMISSION-ARTIFACT",
                4,
                "WS1",
                "REVIEWER",
                json.dumps({"start": 0, "end": 4, "quote": "body"}),
                "片尾需补来源说明",
                "open",
                None,
                1,
                "2026-11-03T00:00:00Z",
                "2026-11-03T00:00:00Z",
            ),
        )
        self.connection.commit()
        self._run(
            "record_collaboration_feedback",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": submission["id"],
                "comment_id": "COMMENT-1",
            },
        )
        feedback = collab["deliverables"][0]["feedback"][0]
        self.assertEqual(feedback["comment"]["artifact_version"], 4)
        self.assertEqual(feedback["comment"]["anchor"]["quote"], "body")
        self.assertEqual(feedback["comment"]["body"], "片尾需补来源说明")

    def test_incomplete_terms_cannot_be_confirmed_as_an_agreement(self) -> None:
        self._run(
            "adopt_collaboration_candidate",
            {"candidate_id": "C1", "reason": "先评估正式条款"},
        )
        collab = self.business["collaborations"][0]
        self._run(
            "record_collaboration_terms",
            {
                "collaboration_id": collab["id"],
                "terms": {"quote": {"amount": 3000, "currency": "CNY", "status": "quote"}},
                "agreement_source": {
                    "kind": "material",
                    "material_id": "CONTRACT",
                    "material_sha256": "contract-sha",
                },
            },
        )
        with self.assertRaisesRegex(ApiError, "Complete the exact delivery"):
            self._run(
                "confirm_collaboration_agreement",
                {"collaboration_id": collab["id"], "approval_id": "APPROVAL-1"},
            )
        self.assertIsNone(collab.get("current_agreement"))
        self.assertEqual(self.business["costs"], [])

    def test_unaccepted_quote_cannot_be_turned_into_a_cost_commitment(self) -> None:
        self._run(
            "adopt_collaboration_candidate",
            {"candidate_id": "C1", "reason": "隔离测试：核验报价与承诺的区别"},
        )
        collab = self.business["collaborations"][0]
        terms = self._terms()
        terms["quote"]["status"] = "quote"
        self._run(
            "record_collaboration_terms",
            {
                "collaboration_id": collab["id"],
                "terms": terms,
                "agreement_source": {
                    "kind": "material",
                    "material_id": "CONTRACT",
                    "material_sha256": "contract-sha",
                },
            },
        )
        self.assertIn(
            "报价或估算尚未明确为本次协议金额",
            collab["terms_proposal"]["gaps"],
        )
        with self.assertRaisesRegex(ApiError, "Complete the exact delivery"):
            self._run(
                "confirm_collaboration_agreement",
                {"collaboration_id": collab["id"], "approval_id": "APPROVAL-QUOTE"},
            )
        self.assertIsNone(collab.get("current_agreement"))
        self.assertEqual(self.business["costs"], [])


class CampaignCollaborationCoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        temporary_parent = repo_root / ".runtime" / "campaign-collaboration-tests"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_parent)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8317)
        install_collaboration_schema(self.app.extensions["oevira.store"])
        register_collaboration_routes(self.app)
        register_collaboration_boundary(self.app)
        register_business_execution_routes(self.app)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        capability = self.root / "launch.capability"
        issue_local_capability(self.app.extensions["oevira.store"], capability)
        response = self.request(
            "post",
            "/api/local-session",
            json={"capability": capability.read_text("ascii")},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]
        self.work = self.post(
            "/api/works",
            {"title": "创作者合作工作", "goal": "为门店新品活动完成一项创作者合作"},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        if getattr(self, "token", None):
            headers.setdefault("Authorization", f"Bearer {self.token}")
        headers.setdefault("Idempotency-Key", str(uuid.uuid4()))
        return getattr(self.client, method)(
            path,
            base_url="http://127.0.0.1:8317",
            headers=headers,
            **kwargs,
        )

    def post(self, path, payload):
        response = self.request("post", path, json=payload)
        self.assertIn(response.status_code, {200, 201}, response.get_data(as_text=True))
        return response.json["data"]

    def command(self, artifact, name, payload):
        return self.post(
            f"/api/business-results/{artifact['id']}/commands",
            {
                "base_version": artifact["version"],
                "command": name,
                "payload": payload,
            },
        )

    def test_real_artifact_command_dispatch_and_client_view_preserve_lifecycle_scope(self) -> None:
        contract = self.post(
            "/api/materials",
            {
                "title": "合作协议原件",
                "filename": "creator-agreement.txt",
                "text": "合作交付协议：一条30秒短片；交付截止2026-11-15。",
                "work_id": self.work["id"],
            },
        )
        contract_segment = contract["segments"][0]
        plan = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {
                "operation": "collaboration_plan",
                "title": "内部候选账本标题",
                "inputs": {
                    "brief": {
                        "goal": "为广州门店新品活动交付创作者短片",
                        "client_title": "广州门店新品合作方案",
                        "currency": "CNY",
                        "budget": {"maximum": 10000},
                    },
                    "collaborators": [
                        {
                            "id": "C1",
                            "project_id": self.work["id"],
                            "creator_id": "creator-internal-1",
                            "display_name": "内部候选与谈判注释",
                            "client_display_name": "公开创作者甲",
                            "role": "KOS",
                            "brand_fit": "项目资料指出该候选制作本地到店短片",
                            "client_fit_concerns": ["客户需确认最终发布日期"],
                            "accounts": [
                                {
                                    "platform": "platform-a",
                                    "account_id": "private-account-identity-1",
                                    "handle": "public-handle-a",
                                }
                            ],
                            "quote": {
                                "amount": 3000,
                                "currency": "CNY",
                                "status": "quote",
                                "source_refs": [],
                            },
                            "deliverables": [
                                {
                                    "id": "D1",
                                    "title": "竖屏新品短片",
                                    "quantity": 1,
                                    "unit": "条",
                                    "specification": "30秒竖屏视频",
                                    "draft_due": "2026-11-10",
                                    "final_due": "2026-11-15",
                                    "acceptance": {
                                        "reviewer": "项目审核人",
                                        "criteria": ["事实准确", "字幕完整"],
                                    },
                                    },
                                    {
                                        "id": "D2",
                                        "title": "活动口播版",
                                        "quantity": 1,
                                        "unit": "条",
                                        "specification": "15秒竖屏口播版本",
                                        "draft_due": "2026-11-10",
                                        "final_due": "2026-11-15",
                                        "acceptance": {
                                            "reviewer": "项目审核人",
                                            "criteria": ["产品信息准确", "权利范围符合协议"],
                                        },
                                    }
                            ],
                            "rights": {
                                "media": ["brand_owned_social"],
                                "territories": ["CN-Guangzhou"],
                                "term_days": 30,
                                "paid_use": False,
                                "editing_allowed": False,
                                "derivative_use": "不包含",
                                "exclusivity": "无排他",
                                "withdrawal_terms": "按书面协议处理",
                            },
                        }
                    ],
                },
                "citations": [
                    {
                        "material_id": contract["id"],
                        "segment_id": contract_segment["id"],
                        "quote": contract_segment["text"],
                    }
                ],
                "tables": [],
            },
        )
        initial_business = plan["input_snapshot"]["business"]
        self.assertEqual(initial_business["table_bindings"], [])
        self.assertEqual(initial_business["input_sources"], [])
        current = self.post(
            f"/api/business-results/{plan['id']}/commands",
            {
                "base_version": plan["version"],
                "command": "adopt_collaboration_candidate",
                "payload": {
                    "candidate_id": "C1",
                    "reason": "采用本地到店内容机制",
                    "client_disclosure": True,
                    "client_summary": "创作者甲进入本项目合作协商。",
                },
            },
        )
        business = current["input_snapshot"]["business"]
        collab = business["collaborations"][0]
        self.assertEqual(len(business["decisions"]), 1)
        self.assertIn("合作记录", current["body"])
        self.assertIn("private-account-identity-1", current["body"])

        from oevira.campaign_collaboration import approval_scope_for_agreement

        terms = {
            "quote": {"amount": 3000, "currency": "CNY", "status": "committed"},
            "rights": {
                "media": ["brand_owned_social"],
                "territories": ["CN-Guangzhou"],
                "term_days": 30,
                "paid_use": False,
                "editing_allowed": False,
                "derivative_use": "不包含",
                "exclusivity": "无排他",
                "withdrawal_terms": "按书面协议处理",
            },
            "deliverables": [
                {
                    "id": "D1",
                    "scope_status": "included",
                    "title": "竖屏新品短片",
                    "quantity": 1,
                    "unit": "条",
                    "specification": "30秒竖屏视频",
                    "draft_due": "2026-11-10",
                    "final_due": "2026-11-15",
                    "acceptance": {"reviewer": "项目审核人", "criteria": ["事实准确", "字幕完整"]},
                },
                {
                    "id": "D2",
                    "scope_status": "included",
                    "title": "活动口播版",
                    "quantity": 1,
                    "unit": "条",
                    "specification": "15秒竖屏口播版本",
                    "draft_due": "2026-11-10",
                    "final_due": "2026-11-15",
                    "acceptance": {"reviewer": "项目审核人", "criteria": ["产品信息准确", "权利范围符合协议"]},
                }
            ],
            "availability": {"from": "2026-11-09", "to": "2026-11-20"},
            "cancellation": {
                "policy": "提前三日书面通知",
                "notice_deadline": "2026-11-08",
                "fee_amount": 500,
                "fee_currency": "CNY",
                "refund_terms": "未履行部分按协议核算",
            },
        }
        current = self.command(
            current,
            "record_collaboration_terms",
            {
                "collaboration_id": collab["id"],
                "terms": terms,
                "agreement_source": {
                    "kind": "material",
                    "material_id": contract["id"],
                    "material_sha256": contract["sha256"],
                },
            },
        )
        collab = current["input_snapshot"]["business"]["collaborations"][0]
        approval_scope = approval_scope_for_agreement(collab)
        approval = self.post(
            f"/api/artifacts/{plan['id']}/approvals",
            {
                "artifact_version": current["version"],
                "purpose": "creator_collaboration_agreement",
                "scope": approval_scope,
            },
        )
        current = self.command(
            current,
            "confirm_collaboration_agreement",
            {"collaboration_id": collab["id"], "approval_id": approval["id"]},
        )
        business = current["input_snapshot"]["business"]
        collab = business["collaborations"][0]
        self.assertEqual(collab["state"], "awaiting_submission")
        self.assertEqual(len(business["costs"]), 1)
        self.assertEqual(business["costs"][0]["kind"], "committed")

        draft = self.post(
            "/api/materials",
            {
                "title": "短片首稿",
                "filename": "draft.txt",
                "text": "新品短片首稿正文。",
                "work_id": self.work["id"],
            },
        )
        current = self.command(
            current,
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "material",
                    "material_id": draft["id"],
                    "material_sha256": draft["sha256"],
                },
                "client_disclosure": True,
                "client_summary": "短片首稿已提交供项目审核。",
            },
        )
        business = current["input_snapshot"]["business"]
        collab = business["collaborations"][0]
        submission = collab["deliverables"][0]["submissions"][0]
        client_export = self.post(
            f"/api/artifacts/{plan['id']}/exports",
            {
                "version": current["version"],
                "format": "md",
                "audience": "client",
            },
        )
        self.assertEqual(client_export["title"], "广州门店新品合作方案")
        download = self.request("get", client_export["download_url"])
        self.assertEqual(download.status_code, 200, download.get_data(as_text=True))
        client_body = download.get_data(as_text=True)
        self.assertIn("短片首稿已提交供项目审核", client_body)
        self.assertIn("品牌自有社媒", client_body)
        self.assertIn("中国·广州", client_body)
        self.assertIn("期限 30天", client_body)
        self.assertNotIn("['brand_owned_social']", client_body)
        self.assertNotIn("['CN-Guangzhou']", client_body)
        self.assertNotIn("内部候选与谈判注释", client_body)
        self.assertNotIn("private-account-identity-1", client_body)
        self.assertNotIn("内部候选账本标题", client_export["title"])
        download.close()

        feedback = self.command(
            current,
            "record_collaboration_feedback",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": submission["id"],
                "feedback": "请补一行门店地址出处。",
            },
        )
        updated = feedback["input_snapshot"]["business"]["collaborations"][0]
        feedback_id = updated["deliverables"][0]["feedback"][0]["id"]
        self.assertEqual(updated["state"], "feedback_recorded")
        self.assertEqual(updated["deliverables"][0]["feedback"][0]["submission_id"], submission["id"])
        revised = self.command(
            feedback,
            "request_collaboration_revision",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": submission["id"],
                "feedback_id": feedback_id,
                "reason": "按客户范围补充来源说明",
            },
        )
        self.assertEqual(revised["input_snapshot"]["business"]["collaborations"][0]["state"], "revision_requested")
        self.assertIn("返修要求", revised["body"])
        revised_artifact = self.post(
            "/api/artifacts",
            {
                "title": "短片修订稿",
                "body": "已补门店地址出处的短片修订稿。",
                "citations": [],
                "work_id": self.work["id"],
            },
        )
        current = self.command(
            revised,
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "source": {
                    "kind": "artifact",
                    "artifact_id": revised_artifact["id"],
                    "artifact_version": revised_artifact["version"],
                },
                "client_disclosure": True,
                "client_summary": "修订稿已补全来源说明并重新提交。",
            },
        )
        from oevira.campaign_collaboration import approval_scope_for_acceptance
        updated_collab = current["input_snapshot"]["business"]["collaborations"][0]
        updated_deliverable = updated_collab["deliverables"][0]
        updated_submission = updated_deliverable["submissions"][-1]
        self.assertEqual(updated_submission["source"]["kind"], "artifact")
        self.assertEqual(updated_submission["source"]["artifact_id"], revised_artifact["id"])
        self.assertEqual(updated_submission["source"]["artifact_version"], revised_artifact["version"])
        self.assertTrue(updated_submission["content_sha256"])
        approval = self.post(
            f"/api/artifacts/{plan['id']}/approvals",
            {
                "artifact_version": current["version"],
                "purpose": "creator_deliverable_acceptance",
                "scope": approval_scope_for_acceptance(
                    updated_collab,
                    updated_deliverable,
                    updated_submission,
                ),
            },
        )
        accepted = self.command(
            current,
            "accept_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D1",
                "submission_id": updated_submission["id"],
                "approval_id": approval["id"],
            },
        )
        accepted_collab = accepted["input_snapshot"]["business"]["collaborations"][0]
        self.assertEqual(
            accepted_collab["deliverables"][0]["state"],
            "accepted_by_exact_approval",
        )
        self.assertEqual(accepted_collab["state"], "deliverable_review_partially_complete")
        self.assertIn("审批记录", accepted["body"])

        rejected_draft = self.post(
            "/api/materials",
            {
                "title": "活动口播版首稿",
                "filename": "voiceover-draft.txt",
                "text": "活动口播版首稿。",
                "work_id": self.work["id"],
            },
        )
        current = self.command(
            accepted,
            "submit_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D2",
                "source": {
                    "kind": "material",
                    "material_id": rejected_draft["id"],
                    "material_sha256": rejected_draft["sha256"],
                },
            },
        )
        pending = current["input_snapshot"]["business"]["collaborations"][0]
        pending_submission = next(
            row for row in pending["deliverables"] if row["id"] == "D2"
        )["submissions"][0]
        rejected = self.command(
            current,
            "reject_collaboration_deliverable",
            {
                "collaboration_id": collab["id"],
                "deliverable_id": "D2",
                "submission_id": pending_submission["id"],
                "reason": "用户记录口播稿不符合授权用语要求",
                "client_disclosure": True,
                "client_summary": "口播版本需按授权用语调整后重新提交。",
            },
        )
        rejected_collab = rejected["input_snapshot"]["business"]["collaborations"][0]
        self.assertEqual(rejected_collab["state"], "deliverable_review_partially_complete")
        self.assertEqual(
            next(row for row in rejected_collab["deliverables"] if row["id"] == "D1")["state"],
            "accepted_by_exact_approval",
        )
        self.assertEqual(
            next(row for row in rejected_collab["deliverables"] if row["id"] == "D2")["state"],
            "rejected_user_recorded",
        )

        current = self.command(
            rejected,
            "record_collaboration_cost",
            {
                "collaboration_id": collab["id"],
                "cost": {
                    "id": "ACTUAL-CORE-1",
                    "kind": "actual",
                    "amount": 3000,
                    "currency": "CNY",
                    "reason": "隔离测试用户输入的实际支出样例，非付款回执",
                },
            },
        )
        current = self.command(
            current,
            "record_collaboration_cost",
            {
                "collaboration_id": collab["id"],
                "cost": {
                    "id": "REFUND-CORE-1",
                    "kind": "refund",
                    "amount": 250,
                    "currency": "CNY",
                    "refund_of": "ACTUAL-CORE-1",
                    "reason": "隔离测试用户输入的部分退款样例，非平台回执",
                },
            },
        )
        current_collab = current["input_snapshot"]["business"]["collaborations"][0]
        commitment_id = current_collab["current_agreement"]["commitment_cost_id"]
        with patch.object(
            collaboration,
            "_resolve_source",
            side_effect=AssertionError(
                "cancelling must not reread the historical agreement source"
            ),
        ) as resolve_source, patch(
            "oevira.business_execution._check_sources",
            side_effect=AssertionError(
                "a control cancellation must not revalidate historical citations"
            ),
        ) as check_sources:
            cancelled = self.command(
                current,
                "cancel_collaboration",
                {
                    "collaboration_id": collab["id"],
                    "reason": "隔离测试：记录取消并保留所有既往交付和费用",
                    "remaining_commitment": {
                        "cost_record_id": commitment_id,
                        "amount": 0,
                        "reason": "隔离测试：已交付范围外没有继续承诺",
                    },
                },
            )
            resolve_source.assert_not_called()
            check_sources.assert_not_called()

        cancelled_business = cancelled["input_snapshot"]["business"]
        cancelled_collab = cancelled_business["collaborations"][0]
        self.assertEqual(cancelled_collab["state"], "cancelled")
        self.assertEqual(len(cancelled_collab["agreements"]), 1)
        self.assertEqual(len(cancelled_collab["deliverables"][0]["submissions"]), 2)
        self.assertEqual(
            cancelled_collab["deliverables"][0]["submissions"][-1]["review_decision"]["state"],
            "accepted_by_exact_approval",
        )
        self.assertEqual(
            [item["kind"] for item in cancelled_business["costs"]],
            ["committed", "actual", "refund", "committed"],
        )
        cancel_event = cancelled_collab["events"][-1]
        self.assertEqual(
            cancel_event["details"]["historical_agreement_source_status"],
            "not_revalidated",
        )
        self.assertEqual(cancel_event["details"]["cancellation_source"], None)

        restored = self.post(
            f"/api/business-results/{plan['id']}/restore",
            {
                "base_version": cancelled["version"],
                "restore_version": plan["version"],
                "revision_note": "恢复候选前方案版并保留已发生的协作历史",
            },
        )
        reopened = self.request("get", f"/api/artifacts/{plan['id']}")
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        reopened_business = reopened.json["data"]["input_snapshot"]["business"]
        reopened_collab = reopened_business["collaborations"][0]
        self.assertGreater(restored["version"], cancelled["version"])
        self.assertEqual(reopened_business["restored_from"], plan["version"])
        self.assertEqual(reopened_business["table_bindings"], [])
        self.assertEqual(reopened_business["input_sources"], [])
        self.assertEqual(
            [item["id"] for item in reopened.json["data"]["input_snapshot"]["materials"]],
            [contract["id"]],
        )
        self.assertEqual(reopened_collab["state"], "cancelled")
        self.assertEqual(len(reopened_collab["agreements"]), 1)
        self.assertEqual(len(reopened_collab["deliverables"][0]["submissions"]), 2)
        self.assertEqual(
            reopened_collab["deliverables"][0]["submissions"][-1]["review_decision"]["state"],
            "accepted_by_exact_approval",
        )
        self.assertEqual(
            [item["kind"] for item in reopened_business["costs"]],
            ["committed", "actual", "refund", "committed"],
        )
        self.assertEqual(
            reopened_collab["events"][-1]["details"]["historical_agreement_source_status"],
            "not_revalidated",
        )
        self.assertIn("取消记录", reopened.json["data"]["body"])


if __name__ == "__main__":
    unittest.main()
