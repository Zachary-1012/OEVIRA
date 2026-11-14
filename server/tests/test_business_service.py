import unittest

from oevira.business_execution_service import evaluate_service


class BusinessServiceTests(unittest.TestCase):
    def test_future_action_is_not_a_completed_fact_at_earlier_cutoff(self):
        result = evaluate_service("incident_brief", {"goal": "核对截至午间的事件事实", "as_of": "2026-11-10T12:00:00+08:00",
            "timeline": [{"kind": "occurred", "at": "2026-11-10T09:00:00+08:00", "description": "上午收到反馈"},
                         {"kind": "acted", "at": "2026-11-10T16:00:00+08:00", "description": "下午的补救尚未发生"}]})
        timeline = next(section for section in result["sections"] if section["id"] == "timeline")
        self.assertEqual(len(timeline["data"]["timeline"]), 1)
        self.assertNotIn("下午的补救尚未发生", timeline["body"])
        self.assertTrue(any("信息截止" in note for note in result["limitations"]))

    def test_duplicate_commitment_cannot_make_fulfilment_ambiguous(self):
        commitment = {"id": "one-promise", "deliverable": "核对现场安排", "owner": "服务负责人", "fulfilment_basis": "已保存排班表"}
        with self.assertRaisesRegex(ValueError, "unique"):
            evaluate_service("service_response", {"goal": "核对反馈", "as_of": "2026-11-10T12:00:00+08:00", "commitments": [commitment, commitment]})
