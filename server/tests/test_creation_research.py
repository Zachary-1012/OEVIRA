from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

from server.oevira.api import ApiError
from server.oevira.creation_research import (
    _load_material_inputs,
    build_research_analysis,
    create_research_job,
    revise_research_artifact,
    _stored_segment_digest,
    validate_research_resume,
)
import test_core
from server.oevira.worker import run_once


class ResearchTests(unittest.TestCase):
    setUp = test_core.CoreApiTest.setUp
    tearDown = test_core.CoreApiTest.tearDown
    request = test_core.CoreApiTest.request
    auth_headers = test_core.CoreApiTest.auth_headers
    create_work = test_core.CoreApiTest.create_work
    create_text_material = test_core.CoreApiTest.create_text_material

    def _source_ref(self, material: dict, quote: str, *, occurrence: int = 0) -> dict:
        row = self.store.row(
            "SELECT id, text, start_offset FROM material_segments WHERE material_id = ? ORDER BY start_offset, id LIMIT 1",
            (material["id"],),
        )
        positions = []
        offset = 0
        while True:
            found = row["text"].find(quote, offset)
            if found < 0:
                break
            positions.append(found)
            offset = found + 1
        start = positions[occurrence]
        return {
            "material_id": material["id"],
            "segment_id": row["id"],
            "quote": quote,
            "source_start": row["start_offset"] + start,
            "source_end": row["start_offset"] + start + len(quote),
        }

    def _queue_research(self, work_id: str, request: dict, key: str):
        return self.request(
            "post",
            f"/api/works/{work_id}/research",
            json=request,
            headers={**self.auth_headers(), "Idempotency-Key": key},
        )

    def test_research_job_saves_source_bound_propositions_and_local_revisions(self) -> None:
        work = self.create_work(title="研究问题")
        material = self.create_text_material(work["id"], "季度报告", "营业收入为120万元，报告期为2026年第三季度。反例材料暂缺。")
        ref = self._source_ref(material, "营业收入为120万元")
        payload = {
            "question": "报告期内营业收入是多少？",
            "material_ids": [material["id"]],
            "purpose": "个人研究",
            "scope": {"period": "2026年第三季度", "unit": "万元"},
            "propositions": [
                {
                    "statement": "报告期营业收入为120万元。",
                    "kind": "source_statement",
                    "supporting_refs": [ref],
                    "reasoning": "原文直接陈述该金额。",
                    "assumptions": ["报告期口径与问题中的期间一致。"],
                },
                {
                    "statement": "收入增长可能由新品带动。",
                    "kind": "hypothesis",
                    "reasoning": "当前材料尚未说明成因。",
                },
            ],
        }
        queued = self._queue_research(work["id"], payload, "research-source-bound-01")
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        self.assertTrue(run_once(self.app, allowed_kinds={"material_research"}))
        job = self.request("get", f"/api/jobs/{job_id}").json["data"]
        self.assertEqual(job["status"], "completed")
        artifact_response = self.request("get", f"/api/artifacts/{job['artifact_id']}")
        self.assertEqual(artifact_response.status_code, 200, artifact_response.get_data(as_text=True))
        artifact = artifact_response.json["data"]
        research = artifact["input_snapshot"]["research"]
        self.assertFalse(research["analysis_complete"])
        self.assertEqual(research["judgment_state"], "not_independently_assessed")
        self.assertEqual(len(research["propositions"]), 2)
        first_id, second_id = [item["proposition_id"] for item in research["propositions"]]
        self.assertEqual(research["source_records"][0]["integrity"], "verified")
        self.assertTrue(any(item["quote"] == ref["quote"] for item in artifact["citations"]))
        self.assertIn("命题关系、推理与结论尚未独立评估", artifact["body"])

        first_revision = {
            "base_version": 1,
            "updates": [{"proposition_id": first_id, "statement": "报告期净营业收入为120万元。"}],
            "revision_note": "纠正命题范围",
        }
        changed = self.request(
            "post", f"/api/artifacts/{artifact['id']}/research-revisions", json=first_revision,
            headers={**self.auth_headers(), "Idempotency-Key": "research-revise-01"},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        current = changed.json["data"]
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["research_revision"]["parent_version"], 1)
        self.assertEqual(current["research_revision"]["changed_proposition_ids"], [first_id])
        state = current["input_snapshot"]["research"]["propositions"]
        self.assertEqual(state[0]["proposition_id"], first_id)
        self.assertEqual(state[0]["statement"], "报告期净营业收入为120万元。")
        self.assertEqual(state[0]["supporting_refs"][0]["relation_state"], "requires_reassessment")
        self.assertEqual(state[1]["proposition_id"], second_id)
        self.assertEqual(state[1]["statement"], payload["propositions"][1]["statement"])
        self.assertIn("命题修改后关系需重新核对", current["body"])
        self.assertEqual(current["input_snapshot"]["materials"][0]["parsed_text_sha256"], artifact["input_snapshot"]["materials"][0]["parsed_text_sha256"])
        self.assertEqual(current["input_snapshot"]["materials"][0]["parsed_segments_sha256"], artifact["input_snapshot"]["materials"][0]["parsed_segments_sha256"])

        replay = self.request(
            "post", f"/api/artifacts/{artifact['id']}/research-revisions", json=first_revision,
            headers={**self.auth_headers(), "Idempotency-Key": "research-revise-01"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"]["version"], 2)

        second_revision = {
            "base_version": 2,
            "updates": [{"proposition_id": second_id, "reasoning": "这仍是待验证的解释。"}],
        }
        changed_again = self.request(
            "post", f"/api/artifacts/{artifact['id']}/research-revisions", json=second_revision,
            headers={**self.auth_headers(), "Idempotency-Key": "research-revise-02"},
        )
        self.assertEqual(changed_again.status_code, 200, changed_again.get_data(as_text=True))
        self.assertEqual(changed_again.json["data"]["version"], 3)
        self.assertEqual(changed_again.json["data"]["input_snapshot"]["materials"][0]["parsed_segments_sha256"], artifact["input_snapshot"]["materials"][0]["parsed_segments_sha256"])

        generic = self.request(
            "post", f"/api/artifacts/{artifact['id']}/versions",
            json={"base_version": 3, "title": current["title"], "body": "脱离结构化命题的修改", "citations": current["citations"]},
        )
        self.assertEqual(generic.status_code, 409)
        self.assertEqual(generic.json["error"]["code"], "RESEARCH_REVISION_REQUIRED")
        restored = self.request(
            "post", f"/api/artifacts/{artifact['id']}/restore",
            json={"base_version": 3, "restore_version": 1, "revision_note": "恢复原研究判断"},
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(restored.json["data"]["version"], 4)
        restore_revision = self.request(
            "post", f"/api/artifacts/{artifact['id']}/research-revisions",
            json={"base_version": 4, "updates": [{"proposition_id": second_id, "reasoning": "恢复后继续修订，假设仍待核。"}]},
            headers={**self.auth_headers(), "Idempotency-Key": "research-after-restore"},
        )
        self.assertEqual(restore_revision.status_code, 200, restore_revision.get_data(as_text=True))
        self.assertEqual(restore_revision.json["data"]["version"], 5)
        self.assertEqual(restore_revision.json["data"]["input_snapshot"]["materials"][0]["parsed_segments_sha256"], artifact["input_snapshot"]["materials"][0]["parsed_segments_sha256"])
        old = self.request("get", f"/api/artifacts/{artifact['id']}?version=1").json["data"]
        self.assertIn("报告期营业收入为120万元。", old["body"])

    def test_research_without_materials_is_saved_as_open_work(self) -> None:
        work = self.create_work(title="独立研究")
        queued = self._queue_research(work["id"], {"question": "如何界定该问题？"}, "research-empty-sources")
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds={"material_research"}))
        job = self.request("get", f"/api/jobs/{queued.json['data']['id']}").json["data"]
        self.assertEqual(job["status"], "completed")
        artifact = self.request("get", f"/api/artifacts/{job['artifact_id']}").json["data"]
        self.assertEqual(artifact["citations"], [])
        self.assertEqual(artifact["input_snapshot"]["research"]["source_records"], [])
        self.assertFalse(artifact["input_snapshot"]["research"]["analysis_complete"])
        self.assertIn("当前未选入资料", artifact["body"])

    def test_research_stale_original_is_partial_and_not_cited(self) -> None:
        work = self.create_work(title="原件核验")
        material = self.create_text_material(work["id"], "完整报告", "预算为120万元。")
        queued = self._queue_research(
            work["id"], {"question": "预算金额", "material_ids": [material["id"]]}, "research-stale-original",
        )
        self.assertEqual(queued.status_code, 202)
        source = self.store.row("SELECT relative_path FROM materials WHERE id = ?", (material["id"],))
        source_path = self.root.joinpath(*Path(source["relative_path"]).parts)
        original = source_path.read_bytes()
        source_path.write_bytes(b"changed after the source hash was recorded")
        try:
            self.assertTrue(run_once(self.app, allowed_kinds={"material_research"}))
        finally:
            source_path.write_bytes(original)
        job = self.request("get", f"/api/jobs/{queued.json['data']['id']}").json["data"]
        self.assertEqual(job["status"], "partial")
        artifact = self.request("get", f"/api/artifacts/{job['artifact_id']}").json["data"]
        self.assertEqual(artifact["citations"], [])
        self.assertEqual(artifact["input_snapshot"]["research"]["source_records"][0]["integrity"], "unavailable")
        self.assertNotIn("预算为120万元", artifact["body"])

    def test_research_resume_rejects_changed_work_or_source_context(self) -> None:
        work = self.create_work(title="续接边界")
        material = self.create_text_material(work["id"], "原始来源", "问题证据在此。")
        queued = self._queue_research(
            work["id"], {"question": "问题证据", "material_ids": [material["id"]]}, "research-resume-check",
        )
        self.assertEqual(queued.status_code, 202)
        job = self.store.row("SELECT * FROM jobs WHERE id = ?", (queued.json["data"]["id"],))
        changed = self.request("patch", f"/api/works/{work['id']}", json={"base_version": work["version"], "goal": "新的问题范围"})
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaises(ApiError) as error:
                validate_research_resume(connection, self.identity, job, {})
        self.assertEqual(error.exception.code, "WORK_VERSION_CONFLICT")

    def test_research_revision_requires_exact_source_range_when_quote_repeats(self) -> None:
        work = self.create_work(title="重复引文")
        material = self.create_text_material(work["id"], "重复句", "预算相同。预算相同。")
        response = self._queue_research(
            work["id"],
            {
                "question": "预算核对",
                "material_ids": [material["id"]],
                "propositions": [{
                    "statement": "预算相同。",
                    "kind": "source_statement",
                    "supporting_refs": [{"material_id": material["id"], "segment_id": self.store.row("SELECT id FROM material_segments WHERE material_id = ?", (material["id"],))["id"], "quote": "预算相同。"}],
                }],
            },
            "research-ambiguous-citation",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "INVALID_RESEARCH_REFERENCE")

    def test_owner_tool_seams_need_no_http_and_enforce_explicit_material_allowlist(self) -> None:
        work = self.create_work(title="智能工具研究范围")
        material = self.create_text_material(work["id"], "经营记录", "预算为120万元，期间为2026年第三季度。")
        material_row = self.store.row("SELECT * FROM materials WHERE id = ?", (material["id"],))
        with self.store.transaction(immediate=False) as connection:
            segment_digest = _stored_segment_digest(connection, self.identity["workspace"]["id"], material["id"])
        snapshot = [{
            "id": material["id"],
            "sha256": material_row["sha256"],
            "parsed_text_sha256": hashlib.sha256((material_row["text"] or "").encode("utf-8")).hexdigest(),
            "parsed_segments_sha256": segment_digest,
            "title": material_row["title"], "filename": material_row["filename"],
            "format": material_row["format"], "status": material_row["status"], "reason": material_row["reason"],
        }]
        material_inputs, source_records, _verified = _load_material_inputs(
            self.store, self.identity, work["id"], snapshot,
        )
        analysis = build_research_analysis(
            material_inputs,
            {"question": "预算金额", "material_ids": [material["id"]], "propositions": []},
            source_records,
            allowed_material_ids={material["id"]},
        )
        self.assertFalse(analysis["analysis_complete"])
        self.assertTrue(any("预算为120万元" in item["quote"] for item in analysis["question_evidence_candidates"]))
        with self.assertRaises(ApiError) as scope_error:
            build_research_analysis(
                material_inputs,
                {"question": "扩大范围", "material_ids": [material["id"], "other-work-material"], "propositions": []},
                source_records,
                allowed_material_ids={material["id"]},
            )
        self.assertEqual(scope_error.exception.code, "SOURCE_SCOPE_MISMATCH")

        with self.app.app_context():
            job = create_research_job(
                self.store, self.identity, work["id"],
                {"question": "来源不足时仍保存待核命题", "propositions": [{
                    "statement": "某个方案可能有效。", "kind": "hypothesis",
                }]},
                "research-direct-owner-seam",
                proposition_origin="assistant_candidate",
            )
        self.assertEqual(job["status"], "queued")
        self.assertTrue(run_once(self.app, allowed_kinds={"material_research"}))
        saved_job = self.request("get", f"/api/jobs/{job['id']}").json["data"]
        artifact = self.request("get", f"/api/artifacts/{saved_job['artifact_id']}").json["data"]
        proposition = artifact["input_snapshot"]["research"]["propositions"][0]
        self.assertEqual(proposition["origin"], "assistant_candidate")
        self.assertIn("智能工具候选，未独立核实", artifact["body"])
        revised = revise_research_artifact(
            self.store, self.identity, artifact["id"],
            {"base_version": 1, "updates": [{"proposition_id": proposition["proposition_id"], "reasoning": "候选需结合实情核对。"}]},
            "research-direct-revision-seam",
            proposition_origin="assistant_candidate",
        )
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["input_snapshot"]["research"]["propositions"][0]["last_revision_origin"], "assistant_candidate")


if __name__ == "__main__":
    unittest.main()
