from __future__ import annotations

import copy
import math
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from oevira import create_app
from oevira.auth import issue_local_capability
from oevira.business_execution import register_business_execution_routes
from oevira.collaboration import (
    install_collaboration_schema,
    register_collaboration_boundary,
    register_collaboration_routes,
)
from oevira.campaign import (
    SUPPORTED_OPERATIONS,
    evaluate_campaign,
    prepare_campaign_revision_inputs,
    preserve_campaign_revision_state_on_restore,
    render_campaign_task_history,
)


def _member_activation_inputs(task_ids: list[str] | None = None) -> dict:
    task_ids = task_ids or ["member-send"]
    tasks = [
        {
            "id": task_id,
            "title": f"会员活动触点 {index + 1}",
            "duration_hours": 1,
            "earliest_start": "2026-11-11T08:00:00+08:00",
            "deadline": "2026-11-11T18:00:00+08:00",
            "estimated_cost": 0,
            "currency": "CNY",
            "cost_status": "estimate",
        }
        for index, task_id in enumerate(task_ids)
    ]
    member_touchpoints = []
    for index, task_id in enumerate(task_ids):
        member_touchpoints.append(
            {
                "id": f"touch-{index + 1}",
                "audience_id": f"segment-{index + 1}",
                "audience_label": f"回访会员段 {index + 1}",
                "consent_status": "granted",
                "purpose": "新品体验活动提醒",
                "consent_scope": "仅广州活动消息",
                "channels": ["app_inbox"],
                "valid_from": "2026-11-01",
                "valid_until": "2026-11-30",
                "opt_out_handling": "收到退订后停止后续触达并抑制名单",
                "source_refs": ["consent:segment-1"],
                "activation": {
                    "task_id": task_id,
                    "channel": "app_inbox",
                    "purpose": "新品体验活动提醒",
                    "contact_window": {
                        "start": "2026-11-11T08:00:00+08:00",
                        "end": "2026-11-11T18:00:00+08:00",
                    },
                    "planned_contacts_per_member": 1,
                    "frequency_rule": {
                        "period_days": 7,
                        "max_contacts_per_member": 10,
                        "history": {
                            "status": "complete",
                            "window_start": "2026-11-04T00:00:00+08:00",
                            "window_end": "2026-11-11T08:00:00+08:00",
                            "max_contacts_per_member": 1,
                            "as_of": "2026-11-11T08:00:00+08:00",
                            "source_refs": ["touch-history:segment-1"],
                        },
                    },
                    "source_refs": ["activation:brief-1"],
                    "offer": {
                        "id": "offer-pool-1",
                        "label": "新品试饮券",
                        "eligibility": "已确认的广州活动报名会员，每人限领一次",
                        "terms": "仅活动现场核销，不可转让，不与其他优惠同享",
                        "valid_from": "2026-11-01",
                        "valid_until": "2026-11-30",
                        "cancellation_terms": "活动取消时未核销券失效，已核销按门店退款规则处理",
                        "face_value": {"amount": 10, "currency": "CNY"},
                        "unit_business_cost": {
                            "amount": 2,
                            "currency": "CNY",
                            "status": "estimate",
                            "source_refs": ["offer-cost:quote-1"],
                        },
                        "redemption_limit": {"basis": "per_member", "quantity": 1},
                        "inventory": {"status": "limited", "quantity": 90},
                        "audience": {
                            "eligible_member_count": 100,
                            "count_basis": "exact_unique_eligible",
                            "population_scope": "本权益池覆盖的广州活动报名会员，按会员主键去重",
                            "as_of": "2026-11-10T18:00:00+08:00",
                            "source_refs": ["audience:segment-snapshot-4"],
                        },
                        "history": {
                            "status": "complete",
                            "window_start": "2026-11-01T00:00:00+08:00",
                            "window_end": "2026-11-11T08:00:00+08:00",
                            "as_of": "2026-11-11T08:00:00+08:00",
                            "redeemed_quantity": 20,
                            "restored_quantity": 5,
                            "refunded_quantity": 2,
                            "actual_spend": {"amount": 200, "currency": "CNY"},
                            "actual_refund": {"amount": 40, "currency": "CNY"},
                            "source_refs": ["offer-ledger:offer-1"],
                        },
                        "source_refs": ["offer:terms-v3"],
                    },
                },
            }
        )
    return {
        "brief": {
            "goal": "核对会员活动权益和执行窗口",
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "budget": {"maximum": 500, "currency": "CNY"},
        },
        "resources": [],
        "tasks": tasks,
        "member_touchpoints": member_touchpoints,
    }


def _schedule_revision_fixture() -> tuple[dict, dict]:
    inputs = {
        "brief": {
            "goal": "完成一组门店内容并排入制作资源",
            "currency": "CNY",
            "timezone": "UTC",
            "start_date": "2026-11-10T09:00:00+00:00",
            "end_date": "2026-11-10T12:00:00+00:00",
            "budget": {"maximum": 1000, "currency": "CNY"},
        },
        "resources": [
            {
                "id": "camera-a",
                "name": "摄影组A",
                "capacity": 1,
                "availability": [{"start": "2026-11-10T09:00:00+00:00", "end": "2026-11-10T12:00:00+00:00"}],
            },
            {
                "id": "camera-b",
                "name": "摄影组B",
                "capacity": 1,
                "availability": [{"start": "2026-11-10T09:00:00+00:00", "end": "2026-11-10T12:00:00+00:00"}],
            },
            {
                "id": "editor-c",
                "name": "独立剪辑组",
                "capacity": 1,
                "availability": [{"start": "2026-11-10T09:00:00+00:00", "end": "2026-11-10T12:00:00+00:00"}],
            },
        ],
        "tasks": [
            {"id": "shoot-a", "title": "门店A拍摄", "duration_hours": 1, "resource_ids": ["camera-a"], "estimated_cost": 100, "currency": "CNY"},
            {"id": "shoot-b", "title": "门店B拍摄", "duration_hours": 1, "resource_ids": ["camera-a"], "estimated_cost": 120, "currency": "CNY"},
            {"id": "edit-c", "title": "独立剪辑", "duration_hours": 1, "resource_ids": ["camera-b"], "estimated_cost": 80, "currency": "CNY"},
            {"id": "design-d", "title": "独立平面设计", "duration_hours": 1, "resource_ids": ["editor-c"], "estimated_cost": 50, "currency": "CNY"},
        ],
    }
    result = evaluate_campaign("campaign_schedule", inputs)
    previous_business = {
        "operation": "campaign_schedule",
        "inputs": copy.deepcopy(inputs),
        "result": copy.deepcopy(result),
        "generated_sections": copy.deepcopy(result["sections"]),
        "manual_sections": [],
        "manual_client_sections": [],
        "confirmed_sections": ["campaign_schedule"],
        "actions": [],
    }
    return inputs, previous_business


def _strategy_revision_fixture() -> tuple[dict, dict]:
    inputs = {
        "brief": {
            "goal": "提高广州门店新品试饮的有效参与",
            "audience": "附近到店的轻饮品顾客",
            "market": "广州",
            "language": "简体中文",
            "currency": "CNY",
            "budget": {"maximum": 1000, "reserve": 100, "currency": "CNY"},
            "constraints": ["不承诺销量提升"],
            "forbidden_claims": ["全网第一"],
        },
        "resources": [],
        "deliverables": [],
        "candidates": [
            {
                "id": "route-sampling",
                "title": "门店现场试饮",
                "mechanism": "顾客进店试饮后完成偏好反馈",
                "mechanism_path": {
                    "audience_trigger": "顾客到店并对新品口味好奇",
                    "brand_action": "在门店提供小杯试饮并邀请反馈",
                    "value_exchange": "顾客获得低门槛体验并给出偏好，品牌获得适口性观察",
                    "reinforcement": "把高频反馈交给下一轮产品问卷，不推成复购效果",
                },
                "audience_need": "先判断新口味是否适合自己",
                "audiences": [
                    {
                        "id": "nearby-visitors",
                        "label": "附近到店顾客",
                        "need": "不想购买整份后才发现口味不适合",
                        "behavior_basis": "用户给出的门店接待观察",
                        "alternative": "直接购买或不参与",
                    }
                ],
                "barrier": "整份购买前无法低成本体验",
                "brand_reason": "门店可提供真实产品体验和服务说明",
                "actions": ["安排一次试饮时段", "收集匿名口味反馈"],
                "deliverable_ids": [],
                "resource_ids": [],
                "dependencies": [],
                "cost_lines": [{"id": "sampling", "label": "试饮物料", "amount": 300, "currency": "CNY", "status": "estimate"}],
                "duration_days": 7,
                "validation": {"metric": "完成的匿名有效反馈数", "denominator": "实际完成试饮者", "window": "活动时段"},
                "counterevidence": ["参与者可能不代表附近全部顾客"],
                "risks": ["现场人员不足会降低体验质量"],
            },
            {
                "id": "route-followup",
                "title": "自愿报名后线上回访",
                "mechanism": "顾客主动报名后收到一次试饮反馈提醒",
                "mechanism_path": {
                    "audience_trigger": "顾客自愿报名并选择线上提醒",
                    "brand_action": "在明确许可渠道发送一次反馈请求",
                    "value_exchange": "顾客可以补充反馈，品牌获得延迟体验意见",
                    "reinforcement": "汇总后更新下一轮体验问卷",
                },
                "audience_need": "离店后仍可补充真实体验",
                "audiences": [
                    {
                        "id": "voluntary-signups",
                        "label": "主动报名并同意回访者",
                        "need": "稍后补充意见",
                        "behavior_basis": "本次报名与同意记录待实际取得",
                        "alternative": "不报名或退出回访",
                    }
                ],
                "barrier": "现场结束后难以补充意见",
                "brand_reason": "品牌可提供明确的退出方式和反馈用途",
                "actions": ["准备一次短问卷", "到许可窗口后复核退出状态"],
                "deliverable_ids": [],
                "resource_ids": [],
                "dependencies": [],
                "cost_lines": [{"id": "survey", "label": "问卷制作", "amount": 250, "currency": "CNY", "status": "estimate"}],
                "duration_days": 5,
                "validation": {"metric": "完成的有效反馈数", "denominator": "实际送达且获准的人数", "window": "发送后7日"},
                "counterevidence": ["许可名单可能很小或缺乏代表性"],
                "risks": ["未核实渠道发送权限前不能联系"],
            },
        ],
    }
    result = evaluate_campaign("strategy_proposal", inputs)
    previous_business = {
        "operation": "strategy_proposal",
        "inputs": copy.deepcopy(inputs),
        "result": copy.deepcopy(result),
        "generated_sections": copy.deepcopy(result["sections"]),
    }
    return inputs, previous_business


class CampaignEvaluationTests(unittest.TestCase):
    def test_supported_operations_have_stable_section_shapes(self) -> None:
        self.assertEqual(
            SUPPORTED_OPERATIONS,
            {"strategy_proposal", "media_plan", "collaboration_plan", "campaign_schedule"},
        )

    def test_safe_client_title_uses_explicit_brief_title_or_operation_generic(self) -> None:
        inputs = {
            "brief": {"goal": "内部工作名称包含敏感细节"},
            "candidates": [
                {
                    "id": "R1",
                    "title": "路线一",
                    "mechanism": "按当前任务条件形成体验和反馈路径",
                    "actions": ["组织体验"],
                }
            ],
        }
        result = evaluate_campaign("strategy_proposal", inputs)
        self.assertEqual(result["client_title"], "品牌策略与提案")
        self.assertNotIn("内部工作名称", result["client_title"])

        titled = copy.deepcopy(inputs)
        titled["brief"]["client_title"] = "广州门店新品体验方案"
        self.assertEqual(
            evaluate_campaign("strategy_proposal", titled)["client_title"],
            "广州门店新品体验方案",
        )
        titled["brief"]["client_title"] = True
        with self.assertRaisesRegex(ValueError, "brief.client_title"):
            evaluate_campaign("strategy_proposal", titled)

    def test_strategy_bodies_render_validation_and_mechanism_maps_as_readable_copy(self) -> None:
        result = evaluate_campaign(
            "strategy_proposal",
            {
                "brief": {"goal": "本地体验活动"},
                "candidates": [
                    {
                        "id": "S1",
                        "title": "门店试饮反馈",
                        "mechanism": "先体验，再自愿反馈并用于下一场说明",
                        "mechanism_path": {
                            "audience_trigger": "顾客到店并选择参加",
                            "brand_action": "门店提供一份试饮并记录场次",
                            "value_exchange": "顾客获得试饮并可表达口味偏好",
                            "reinforcement": "反馈进入下一场次的说明调整",
                        },
                        "actions": ["核对场次", "记录反馈"],
                        "validation": {
                            "metric": "自愿完成反馈人数",
                            "denominator": "实际参加试饮人数",
                            "window": "每场活动及活动结束后七日",
                        },
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        for section in (
            sections["strategy_alternatives"],
        ):
            for body in (section["body"], section["client_body"]):
                self.assertIn("受众触发：顾客到店并选择参加", body)
                self.assertIn("指标：自愿完成反馈人数", body)
                self.assertIn("分母：实际参加试饮人数", body)
                self.assertIn("观察窗口：每场活动及活动结束后七日", body)
                self.assertNotIn("{'metric'", body)

    def test_every_campaign_section_has_a_safe_client_body_and_hides_internal_costs(self) -> None:
        samples = [
            (
                "strategy_proposal",
                {
                    "brief": {"goal": "制定本地活动提案", "currency": "CNY", "budget": {"maximum": 5000}},
                    "candidates": [
                        {
                            "id": "S1",
                            "title": "门店共创",
                            "mechanism": "顾客参与口味选择并获得现场试饮",
                            "actions": ["安排试饮"],
                            "cost_lines": [{"id": "internal", "amount": 100, "currency": "CNY", "status": "estimate"}],
                        }
                    ],
                },
            ),
            (
                "media_plan",
                {
                    "brief": {"goal": "规划本地投放", "currency": "CNY", "budget": {"maximum": 5000}},
                    "media_items": [{"id": "M1", "channel": "本地平台", "amount": 100, "currency": "CNY", "status": "quote"}],
                },
            ),
            (
                "collaboration_plan",
                {
                    "brief": {"goal": "策划创作者合作", "currency": "CNY", "budget": {"maximum": 5000}},
                    "collaborators": [
                        {
                            "id": "C1",
                            "project_id": "WORK1",
                            "creator_id": "creator-one",
                            "role": "KOS",
                            "accounts": [{"platform": "platform-a", "account_id": "acct-public-1"}],
                            "quote": {"amount": 100, "currency": "CNY", "status": "quote"},
                        }
                    ],
                },
            ),
            (
                "campaign_schedule",
                {
                    "brief": {"goal": "完成门店活动", "currency": "CNY", "timezone": "UTC", "start_date": "2026-11-10", "end_date": "2026-11-11", "budget": {"maximum": 5000}},
                    "resources": [],
                    "tasks": [{"id": "T1", "title": "活动准备", "duration_hours": 1, "resource_ids": [], "estimated_cost": 100}],
                },
            ),
        ]
        for operation, inputs in samples:
            with self.subTest(operation=operation):
                result = evaluate_campaign(operation, inputs)
                self.assertTrue(all(isinstance(section.get("client_body"), str) for section in result["sections"]))
                self.assertIn("client_citations", result)
                self.assertIn("client_limitations", result)
                client_body = "\n".join(section["client_body"] for section in result["sections"])
                self.assertNotIn("100 CNY", client_body)

    def test_only_explicit_client_quote_enters_strategy_client_body(self) -> None:
        result = evaluate_campaign(
            "strategy_proposal",
            {
                "brief": {"goal": "门店试饮", "currency": "CNY", "budget": {"maximum": 1000}},
                "candidates": [
                    {
                        "id": "S1",
                        "title": "路线甲",
                        "mechanism": "体验后反馈",
                        "actions": ["组织试饮"],
                        "cost_lines": [
                            {
                                "id": "internal-supplier-cost",
                                "amount": 100,
                                "currency": "CNY",
                                "status": "estimate",
                                "client_quote": {
                                    "label": "门店活动对客报价",
                                    "amount": 180,
                                    "currency": "CNY",
                                    "status": "quote",
                                    "source_refs": ["client-quote:approved-1"],
                                },
                            }
                        ],
                    }
                ],
            },
        )
        client_body = next(section["client_body"] for section in result["sections"] if section["id"] == "strategy_scope_budget")
        self.assertIn("门店活动对客报价", client_body)
        self.assertIn("180 CNY", client_body)
        self.assertNotIn("100 CNY", client_body)

    def test_strategy_proposal_keeps_user_basis_and_compares_real_mechanisms(self) -> None:
        inputs = {
            "brief": {
                "goal": "增加广州门店新品试饮参与",
                "audience": "客户指定的周边到店客群",
                "market": "广州",
                "currency": "CNY",
                "budget": {"maximum": 50000, "reserve": 3000},
                "facts": [
                    {
                        "id": "F1",
                        "text": "门店每天可安排两场试饮",
                        "source_refs": ["material:12/page:3"],
                    }
                ],
                "forbidden_claims": ["不得宣称未经检测的健康功效"],
            },
            "quotes": [
                {
                    "id": "Q1",
                    "amount": 12000,
                    "currency": "CNY",
                    "status": "quote",
                    "source_refs": ["quote:vendor-7:v2"],
                }
            ],
            "deliverables": [
                {"id": "D1", "title": "门店试饮内容", "quantity": 3, "unit": "posts"}
            ],
            "resources": [
                {"id": "R1", "name": "项目摄影", "capacity": 1, "qualifications": ["摄影"]}
            ],
            "candidates": [
                {
                    "id": "S1",
                    "title": "把试饮做成门店共创",
                    "mechanism": "顾客参与口味选择并形成可追踪的门店反馈",
                    "mechanism_path": {
                        "audience_trigger": "顾客到店后先试饮再表达口味偏好",
                        "brand_action": "门店按场次提供新品试饮并记录反馈",
                        "value_exchange": "顾客提交反馈后获得本次试饮体验",
                        "reinforcement": "反馈结果进入下一场次的口味说明",
                    },
                    "audience_need": "客户Brief指出顾客需要先体验再决定",
                    "audiences": [
                        {
                            "id": "SEG1",
                            "label": "客户Brief指定的周边到店客群",
                            "need": "先体验再决定",
                            "behavior_basis": "Brief要求安排门店试饮",
                            "alternative": "不参与试饮的到店顾客",
                            "source_refs": ["brief:audience-1"],
                        }
                    ],
                    "barrier": "新品缺少试饮入口",
                    "brand_reason": "门店现有试饮能力可承接体验",
                    "actions": ["安排门店试饮", "收集同一批次的反馈"],
                    "deliverable_ids": ["D1"],
                    "resource_ids": ["R1"],
                    "cost_lines": [{"id": "C1", "quote_id": "Q1"}],
                    "validation": {"metric": "完成试饮人数", "denominator": "到店符合条件人数", "window": "活动期"},
                    "counterevidence": ["门店容量可能限制参与规模"],
                    "source_refs": ["material:12/page:3"],
                    "expected_effect": "需要活动后观察，当前不承诺提升幅度",
                },
                {
                    "id": "S2",
                    "title": "让到店问题进入现场解答",
                    "mechanism": "顾客提出具体疑问，由门店人员现场演示产品使用",
                    "mechanism_path": {
                        "audience_trigger": "顾客在到店前提出使用疑问",
                        "brand_action": "门店人员围绕问题现场演示使用方法",
                        "value_exchange": "顾客得到逐项问题解答",
                        "reinforcement": "常见问题作为后续内容选题",
                    },
                    "audience_need": "目标顾客需要了解使用方法",
                    "audiences": [
                        {
                            "id": "SEG2",
                            "label": "提出使用问题的到店顾客",
                            "need": "了解使用方法",
                            "behavior_basis": "Brief描述的现场问题",
                            "alternative": "只查看产品说明的顾客",
                        }
                    ],
                    "barrier": "现有内容没有现场解答",
                    "brand_reason": "门店人员可进行现场演示",
                    "actions": ["安排现场演示", "记录常见问题"],
                    "deliverable_ids": ["D1"],
                    "resource_ids": ["R1"],
                    "cost_lines": [{"id": "C2", "amount": 10000, "currency": "CNY", "status": "estimate"}],
                    "case_basis": {
                        "case_id": "CASE-7",
                        "local_fit": "只采用现场问答形式，按本店人员能力重新设计",
                        "adoption_changes": ["替换为客户确认的产品事实"],
                        "non_transferable_conditions": ["原案例的参与规模未在本案验证"],
                        "reported_result": "原案例报告了互动增长",
                        "source_refs": ["case:7#mechanism"],
                        "asset_uses": [
                            {
                                "id": "USE1",
                                "asset_id": "case-image-1",
                                "use_scope": "仅供内部审阅",
                                "territories": [],
                                "rights_status": "pending",
                                "rights_refs": [],
                            }
                        ],
                    },
                    "validation": {"method": "按活动场次复核问题与试饮记录"},
                    "counterevidence": [],
                    "evidence_refs": ["evidence:brief-4#request-2"],
                },
            ],
        }
        before = copy.deepcopy(inputs)
        result = evaluate_campaign("strategy_proposal", inputs)

        self.assertEqual(inputs, before)
        self.assertEqual(
            [section["id"] for section in result["sections"]],
            ["strategy_brief", "strategy_alternatives", "strategy_scope_budget", "strategy_validation"],
        )
        data = result["sections"][1]["data"]
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["mechanism_comparisons"][0]["status"], "STATED_MECHANISM_PATH_DIFFERENCE")
        self.assertEqual(data["candidates"][1]["audiences"][0]["label"], "提出使用问题的到店顾客")
        self.assertEqual(data["candidates"][1]["case_basis"]["reported_result_status"], "user_reported_not_a_guarantee")
        self.assertTrue(any("使用权未获充分确认" in gap for gap in data["candidates"][1]["gaps"]))
        self.assertEqual(data["candidates"][0]["cost_lines"][0]["source_refs"], ["quote:vendor-7:v2"])
        self.assertEqual(data["candidates"][0]["basis_status"], "references_provided_not_independently_verified")
        self.assertIn("未独立核验", result["sections"][0]["body"])
        self.assertIn("不是预测或保证", " ".join(result["limitations"]))
        self.assertEqual(data["candidates"][0]["budget_check"]["status"], "WITHIN_LIMIT")
        self.assertNotIn("WITHIN_LIMIT", result["sections"][2]["body"])
        self.assertNotIn("R1", result["sections"][1]["body"])
        self.assertNotIn("D1", result["sections"][2]["body"])
        self.assertIn("门店试饮", result["sections"][1]["body"])
        self.assertIn("12000", result["sections"][1]["body"])

    def test_strategy_revision_merges_partial_brief_and_candidate_without_dropping_other_routes(self) -> None:
        inputs, previous_business = _strategy_revision_fixture()
        candidate = copy.deepcopy(inputs)
        candidate["brief"] = {"budget": {"maximum": 800}}
        candidate["candidates"] = [
            {
                "id": "route-sampling",
                "mechanism": "先让顾客体验一小口，再用匿名反馈改下一轮产品说明",
                "mechanism_path": {"brand_action": "门店提供小杯试饮并收集匿名偏好"},
            }
        ]
        prepared, context = prepare_campaign_revision_inputs(candidate, previous_business)

        self.assertEqual(prepared["brief"]["goal"], inputs["brief"]["goal"])
        self.assertEqual(prepared["brief"]["audience"], inputs["brief"]["audience"])
        self.assertEqual(prepared["brief"]["budget"]["maximum"], 800)
        routes = {row["id"]: row for row in prepared["candidates"]}
        self.assertEqual(set(routes), {"route-sampling", "route-followup"})
        self.assertEqual(routes["route-sampling"]["brand_reason"], inputs["candidates"][0]["brand_reason"])
        self.assertEqual(routes["route-sampling"]["actions"], inputs["candidates"][0]["actions"])
        self.assertEqual(routes["route-followup"], inputs["candidates"][1])
        self.assertTrue(any(row["path"] == "budget.maximum" for row in context["brief_changes"]))
        changed = next(row for row in context["candidate_changes"] if row["candidate_id"] == "route-sampling")
        self.assertEqual(changed["change"], "updated")
        self.assertTrue(any(row["path"] == "mechanism" for row in changed["changed_fields"]))
        self.assertEqual(context["preserved_candidate_ids"], ["route-followup"])

        result = evaluate_campaign("strategy_proposal", prepared, revision_context=context)
        sections = {section["id"]: section for section in result["sections"]}
        alternatives = sections["strategy_alternatives"]["data"]
        self.assertEqual(
            alternatives["revision_impact"]["affected_sections"],
            ["strategy_alternatives", "strategy_brief", "strategy_scope_budget", "strategy_validation"],
        )
        self.assertIn("本次Brief范围与路线差异", sections["strategy_alternatives"]["body"])
        self.assertIn("旧路线默认保留", sections["strategy_alternatives"]["body"])
        self.assertIn("revision_impact", sections["strategy_scope_budget"]["data"])

    def test_strategy_route_removal_requires_explicit_reason_and_is_versioned(self) -> None:
        inputs, previous_business = _strategy_revision_fixture()
        candidate = copy.deepcopy(inputs)
        candidate["candidate_removals"] = [
            {"id": "route-followup", "reason": "当前缺少获准线上触达渠道，先保留线下路线"}
        ]
        prepared, context = prepare_campaign_revision_inputs(candidate, previous_business)

        self.assertEqual([row["id"] for row in prepared["candidates"]], ["route-sampling"])
        removed = next(row for row in context["candidate_changes"] if row["candidate_id"] == "route-followup")
        self.assertEqual(removed["change"], "removed_by_explicit_revision")
        self.assertEqual(removed["reason"], candidate["candidate_removals"][0]["reason"])
        self.assertNotIn("candidate_removals", prepared)

        result = evaluate_campaign("strategy_proposal", prepared, revision_context=context)
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(len(sections["strategy_alternatives"]["data"]["candidates"]), 1)
        self.assertIn("明确选择移除路线", sections["strategy_alternatives"]["body"])
        self.assertIn(candidate["candidate_removals"][0]["reason"], sections["strategy_alternatives"]["body"])
        self.assertEqual(
            sections["strategy_alternatives"]["data"]["revision_impact"]["removed_candidate_ids"],
            ["route-followup"],
        )
        with self.assertRaisesRegex(ValueError, "saved strategy revision"):
            evaluate_campaign("strategy_proposal", candidate)

    def test_strategy_resource_availability_is_checked_without_claiming_a_task_schedule(self) -> None:
        cases = [
            (
                "overlap",
                {"available_from": "2026-11-11", "available_to": "2026-11-13"},
                "OVERLAPS_BRIEF_WINDOW_NOT_SCHEDULED",
                "尚未进行任务级排期",
            ),
            (
                "outside",
                {"available_from": "2026-12-01", "available_to": "2026-12-04"},
                "NO_OVERLAP_WITH_BRIEF_WINDOW",
                "没有重叠",
            ),
            (
                "unknown",
                {},
                "AVAILABILITY_UNKNOWN",
                "可用时段未知",
            ),
        ]
        for label, availability, expected_status, body_phrase in cases:
            with self.subTest(label=label):
                result = evaluate_campaign(
                    "strategy_proposal",
                    {
                        "brief": {
                            "goal": "在项目窗口内安排门店体验路线",
                            "timezone": "UTC",
                            "start_date": "2026-11-10",
                            "end_date": "2026-11-20",
                            "currency": "CNY",
                        },
                        "resources": [
                            {
                                "id": "R1",
                                "name": "现场活动负责人",
                                "capacity": 1,
                                **availability,
                            }
                        ],
                        "candidates": [
                            {
                                "id": "ROUTE-1",
                                "title": "门店体验路线",
                                "mechanism": "顾客到店体验并提交反馈",
                                "actions": ["安排门店场次"],
                                "resource_ids": ["R1"],
                            }
                        ],
                    },
                )
                section = next(
                    item
                    for item in result["sections"]
                    if item["id"] == "strategy_alternatives"
                )
                candidate = section["data"]["candidates"][0]
                self.assertEqual(
                    candidate["resource_checks"][0]["status"],
                    expected_status,
                )
                self.assertIn(body_phrase, section["body"])
                self.assertNotIn(expected_status, section["body"])
                self.assertTrue(
                    any("资源" in item and "路线“门店体验路线”" in item for item in result["client_limitations"])
                )

    def test_media_plan_calculates_spend_without_inventing_reach(self) -> None:
        inputs = {
            "brief": {
                "goal": "在广州开展新品内容推广",
                "audience": "待项目确认",
                "market": "广州",
                "currency": "CNY",
                "budget": {"maximum": 5000, "reserve": 500},
            },
            "quotes": [
                {"id": "Q1", "amount": 2400, "currency": "CNY", "status": "quote", "source_refs": ["quote:1"]}
            ],
            "deliverables": [{"id": "D1", "title": "平台短片"}],
            "media_items": [
                {
                    "id": "M1",
                    "channel": "短视频平台",
                    "account_id": "brand-account-1",
                    "placement": "本地内容版位",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-12",
                    "deliverable_ids": ["D1"],
                    "quote_id": "Q1",
                    "pricing_basis": "按项目报价",
                    "metric_definition": "平台原生有效观看定义待核",
                    "denominator": "待取得平台口径",
                    "observation_window": "2026-11-10至2026-11-20",
                    "data_rights": "仅活动复盘",
                    "exclusions": ["剔除无效流量口径待核"],
                    "brand_suitability": "由客户确认品牌安全条件",
                    "quoted_reach": None,
                    "evidence_refs": ["evidence:media-brief#1"],
                }
            ],
        }
        result = evaluate_campaign("media_plan", inputs)

        ids = [section["id"] for section in result["sections"]]
        self.assertEqual(ids, ["media_plan_scope", "media_placements", "media_budget", "media_measurement"])
        placement = result["sections"][1]["data"]["candidates"][0]["media_items"][0]
        self.assertIsNone(placement["quoted_reach"])
        self.assertEqual(placement["evidence_refs"], ["evidence:media-brief#1"])
        budget = result["sections"][2]["data"]["candidates"][0]["budget_check"]
        self.assertEqual(budget["scenario_planning_exposure"], 2400)
        self.assertEqual(budget["available_before_scenario"], 4500)
        self.assertNotIn("曝光", placement)
        self.assertIn("2400", result["sections"][2]["body"])

    def test_collaboration_keeps_platform_accounts_rights_and_observations_separate(self) -> None:
        result = evaluate_campaign(
            "collaboration_plan",
            {
                "brief": {"goal": "为活动征集创作者合作方案", "currency": "CNY", "budget": {"maximum": 10000}},
                "collaborators": [
                    {
                        "id": "C1",
                        "project_id": "P1",
                        "creator_id": "creator-record-9",
                        "display_name": "内部候选甲·议价备注",
                        "client_display_name": "公开候选甲",
                        "fit_concerns": ["内部采购底价未确认"],
                        "client_fit_concerns": ["地域权益待确认"],
                        "role": "KOS",
                        "identity_link_status": "operator_confirmed",
                        "accounts": [
                            {"platform": "platform-a", "account_id": "acct-a-9", "handle": "same-name", "identity_link_status": "operator_confirmed"},
                            {"platform": "platform-b", "account_id": "acct-b-4", "handle": "same-name", "identity_link_status": "operator_confirmed"},
                        ],
                        "availability": {"from": "2026-11-10", "to": "2026-11-18"},
                        "quote": {"amount": 5000, "currency": "CNY", "status": "quote", "source_refs": ["quote:kos-3"]},
                        "contact_permission": {
                            "status": "granted",
                            "purpose": "项目合作咨询",
                            "channels": ["platform-a"],
                            "scope": "仅本项目初次联系",
                            "valid_until": "2026-11-30",
                            "source_refs": ["consent:creator-9"],
                        },
                        "deliverables": [
                            {"id": "D1", "quantity": 2, "unit": "videos", "specification": "竖屏视频", "draft_due": "2026-11-12", "final_due": "2026-11-16", "acceptance": {"reviewer": "客户项目负责人", "criteria": ["事实准确"]}}
                        ],
                        "rights": {"media": ["brand_owned_social"], "territories": ["CN-Guangzhou"], "term_days": 30, "paid_use": False},
                        "observations": [
                            {"platform": "platform-a", "account_id": "acct-a-9", "metric": "有效咨询", "numerator": 0, "denominator": 0, "same_cohort": True, "observation_window": "2026-10-01/2026-10-31", "source_kind": "manual", "source_refs": ["metrics:manual-7"]},
                            {"platform": "platform-b", "account_id": "acct-b-4", "metric": "互动", "value": 12, "observation_window": "2026-10-01/2026-10-31", "source_kind": "platform_native"},
                        ],
                    }
                ],
            },
        )

        sections = {section["id"]: section for section in result["sections"]}
        candidate = sections["collaboration_scope"]["data"]["collaborators"][0]
        self.assertEqual(candidate["project_id"], "P1")
        self.assertEqual(
            candidate["contact_permission"]["contactability"],
            "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK",
        )
        self.assertEqual(
            candidate["contact_permission"]["external_contact"],
            "not_performed_by_evaluator",
        )
        self.assertEqual([(row["platform"], row["account_id"]) for row in candidate["accounts"]], [("platform-a", "acct-a-9"), ("platform-b", "acct-b-4")])
        self.assertEqual(candidate["observations"][0]["rate_status"], "ZERO_DENOMINATOR")
        self.assertIsNone(candidate["observations"][0]["rate"])
        self.assertIn("撤回条件", candidate["missing_rights"])
        self.assertIn("不用于推断", " ".join(result["limitations"]))
        self.assertIn("5000", sections["collaboration_terms"]["body"])
        self.assertIn("acct-a-9", sections["collaboration_scope"]["body"])
        client_body = "\n".join(section["client_body"] for section in result["sections"])
        self.assertIn("公开候选甲", client_body)
        self.assertIn("地域权益待确认", client_body)
        self.assertNotIn("内部候选甲", client_body)
        self.assertNotIn("内部采购底价未确认", client_body)

    def test_podcast_mode_terms_and_five_dimensions_remain_separate(self) -> None:
        result = evaluate_campaign(
            "collaboration_plan",
            {
                "brief": {"goal": "比较播客内容合作", "currency": "CNY"},
                "collaborators": [
                    {
                        "id": "POD-1",
                        "project_id": "WORK-1",
                        "creator_id": "podcast-record-1",
                        "role": "播客节目合作",
                        "accounts": [{"platform": "podcast-platform", "account_id": "show-1"}],
                        "podcast_mode": "custom_program",
                        "podcast_mode_terms": {
                            "episode_format": "主持人访谈",
                            "episode_length_minutes": 28,
                        },
                        "podcast_dimensions": {
                            "influence": {
                                "metric_definition": "节目页显示的单集播放次数",
                                "value": 1200,
                                "unit": "plays",
                                "denominator": "per episode",
                                "observation_window": "2026-10",
                                "source_kind": "user_recorded",
                                "source_refs": ["report:podcast-october"],
                            },
                            "monetization": {
                                "metric_definition": "该期历史赞助报价",
                                "value": 2500,
                                "unit": "CNY",
                                "observation_window": "2026-10",
                            },
                        },
                        "rights": {},
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        plan = sections["collaboration_scope"]["data"]["collaborators"][0]["podcast_plan"]
        self.assertEqual(plan["mode_label"], "节目定制")
        self.assertEqual(plan["powers"]["influence"]["value"], 1200)
        self.assertEqual(plan["powers"]["influence"]["source_refs"], ["report:podcast-october"])
        self.assertEqual(plan["powers"]["content"]["status"], "UNKNOWN")
        self.assertEqual(plan["powers"]["monetization"]["value"], 2500)
        self.assertEqual(plan["comparison_status"], "PODCAST_DIMENSIONS_KEPT_SEPARATE_NO_COMPOSITE_SCORE")
        self.assertIn("编辑/审阅范围", plan["missing_mode_terms"])
        self.assertIn("25", sections["collaboration_observations"]["body"])
        self.assertIn("影响力", sections["collaboration_observations"]["body"])
        self.assertIn("待补", sections["collaboration_terms"]["body"])

    def test_podcast_numeric_observation_rejects_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite number"):
            evaluate_campaign(
                "collaboration_plan",
                {
                    "brief": {"goal": "播客报价比较"},
                    "collaborators": [
                        {
                            "id": "POD-1",
                            "project_id": "WORK-1",
                            "creator_id": "podcast-record-1",
                            "accounts": [{"platform": "podcast-platform", "account_id": "show-1"}],
                            "podcast_mode": "sponsorship_insert",
                            "podcast_dimensions": {"influence": {"value": True}},
                        }
                    ],
                },
            )

    def test_collaboration_pairwise_comparison_requires_matching_contract_and_metric_basis(self) -> None:
        candidates = []
        for identity, amount, value, account in (
            ("C1", 3000, 100, "acct-1"),
            ("C2", 2500, 75, "acct-2"),
        ):
            candidates.append(
                {
                    "id": identity,
                    "project_id": "PROJECT-1",
                    "creator_id": f"creator-{identity}",
                    "role": "KOS",
                    "accounts": [{"platform": "platform-a", "account_id": account}],
                    "quote": {
                        "amount": amount,
                        "currency": "CNY",
                        "status": "quote",
                    },
                    "deliverables": [
                        {
                            "id": f"{identity}-D1",
                            "title": "竖屏短片",
                            "quantity": 1,
                            "unit": "条",
                            "specification": "30秒竖屏视频",
                            "draft_due": "2026-11-12",
                            "final_due": "2026-11-16",
                            "acceptance": {
                                "reviewer": "项目负责人",
                                "criteria": ["事实准确"],
                            },
                        }
                    ],
                    "rights": {
                        "media": ["brand_owned_social"],
                        "territories": ["CN-Guangzhou"],
                        "term_days": 30,
                        "paid_use": False,
                        "editing_allowed": False,
                        "derivative_use": False,
                        "exclusivity": "none",
                        "withdrawal_terms": "提交前可撤回",
                    },
                    "observations": [
                        {
                            "platform": "platform-a",
                            "account_id": account,
                            "metric": "有效播放",
                            "unit": "plays",
                            "value": value,
                            "denominator": "每条作品",
                            "observation_window": "2026-10",
                            "source_kind": "platform_native",
                        }
                    ],
                }
            )

        result = evaluate_campaign(
            "collaboration_plan",
            {"brief": {"goal": "比较同规格创作者合作", "currency": "CNY"}, "collaborators": candidates},
        )
        observation = next(
            section for section in result["sections"] if section["id"] == "collaboration_observations"
        )
        comparison = observation["data"]["comparisons"][0]
        self.assertEqual(
            comparison["quote_comparison_status"],
            "COMPARABLE_DECLARED_PRICE_BASIS",
        )
        self.assertEqual(comparison["quote_second_minus_first"], -500)
        self.assertEqual(
            comparison["shared_metric_comparisons"][0]["second_minus_first"],
            -25,
        )
        self.assertEqual(comparison["ranking"], "not_created")
        self.assertIsNone(comparison["composite_score"])
        self.assertIn("-500", observation["body"])
        self.assertIn("不是效果排序", observation["body"])

    def test_schedule_respects_dependencies_capacity_alternatives_and_budget(self) -> None:
        inputs = {
            "brief": {
                "goal": "完成线下试饮活动制作",
                "currency": "CNY",
                "timezone": "Asia/Shanghai",
                "start_date": "2026-11-10",
                "end_date": "2026-11-20",
                "budget": {"maximum": 20000, "reserve": 1000},
            },
            "resources": [
                {"id": "R1", "name": "主摄影", "capacity": 1, "qualifications": ["摄影"], "available_from": "2026-11-10", "available_to": "2026-11-20"},
                {"id": "R2", "name": "替补摄影", "capacity": 1, "qualifications": ["摄影"], "available_from": "2026-11-10", "available_to": "2026-11-20"},
            ],
            "tasks": [
                {"id": "T1", "title": "拍摄", "duration_hours": 4, "earliest_start": "2026-11-10T09:00:00", "deadline": "2026-11-13", "resource_options": [["R1"], ["R2"]], "required_qualifications": ["摄影"], "estimated_cost": 8000, "cost_status": "estimate"},
                {"id": "T2", "title": "剪辑", "duration_hours": 6, "dependencies": ["T1"], "earliest_start": "2026-11-10T09:00:00", "deadline": "2026-11-16", "resource_ids": ["R1"], "estimated_cost": 4000, "cost_status": "quote"},
            ],
        }
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        schedule = sections["campaign_schedule"]["data"]
        self.assertEqual(schedule["status"], "READY_TO_REVIEW")
        tasks = {row["id"]: row for row in schedule["tasks"]}
        self.assertEqual(tasks["T1"]["status"], "PLANNED")
        self.assertEqual(sum(option["selected_by_milp"] for option in tasks["T1"]["alternative_assignments"]), 1)
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "OPTIMAL")
        self.assertEqual(tasks["T2"]["dependencies"], ["T1"])
        self.assertGreaterEqual(tasks["T2"]["start"], tasks["T1"]["end"])
        self.assertEqual(sections["campaign_budget"]["data"]["budget_check"]["scenario_planning_exposure"], 12000)
        self.assertEqual(sections["campaign_schedule"]["data"]["resource_conflicts"], [])
        self.assertIn("2026-11-10T09:00:00+08:00", sections["campaign_schedule"]["body"])
        self.assertIn("12000", sections["campaign_budget"]["body"])

    def test_milp_uses_alternative_resource_to_minimize_completion_time(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "三小时内完成并行拍摄",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10T09:00:00",
                    "end_date": "2026-11-10T12:00:00",
                    "budget": {"maximum": 0, "reserve": 0},
                },
                "resources": [
                    {
                        "id": "R1",
                        "name": "主摄影",
                        "capacity": 1,
                        "availability": [{"start": "2026-11-10T09:00:00+08:00", "end": "2026-11-10T12:00:00+08:00"}],
                    },
                    {
                        "id": "R2",
                        "name": "替补摄影",
                        "capacity": 1,
                        "availability": [{"start": "2026-11-10T09:00:00+08:00", "end": "2026-11-10T12:00:00+08:00"}],
                    },
                ],
                "tasks": [
                    {"id": "T1", "title": "门店A拍摄", "duration_hours": 3, "resource_options": [["R1"], ["R2"]], "estimated_cost": 0},
                    {"id": "T2", "title": "门店B拍摄", "duration_hours": 3, "resource_ids": ["R1"], "estimated_cost": 0},
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        tasks = {row["id"]: row for row in sections["campaign_schedule"]["data"]["tasks"]}
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "OPTIMAL")
        self.assertEqual(tasks["T1"]["resource_ids"], ["R2"])
        self.assertEqual(tasks["T2"]["resource_ids"], ["R1"])
        self.assertEqual(tasks["T1"]["start"], tasks["T2"]["start"])
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "READY_TO_REVIEW")

    def test_milp_uses_declared_parallel_capacity(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "并行拍摄两项门店内容",
                    "timezone": "UTC",
                    "start_date": "2026-11-10T09:00:00",
                    "end_date": "2026-11-10T11:00:00",
                    "currency": "CNY",
                    "budget": {"maximum": 0, "reserve": 0},
                },
                "resources": [
                    {
                        "id": "R-CREW",
                        "name": "两组摄影团队",
                        "capacity": 2,
                        "availability": [{"start": "2026-11-10T09:00:00+00:00", "end": "2026-11-10T11:00:00+00:00"}],
                    }
                ],
                "tasks": [
                    {"id": "T-A", "title": "门店A拍摄", "duration_hours": 2, "resource_ids": ["R-CREW"], "estimated_cost": 0},
                    {"id": "T-B", "title": "门店B拍摄", "duration_hours": 2, "resource_ids": ["R-CREW"], "estimated_cost": 0},
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        tasks = {row["id"]: row for row in sections["campaign_schedule"]["data"]["tasks"]}
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "OPTIMAL")
        self.assertEqual(tasks["T-A"]["start"], tasks["T-B"]["start"])
        self.assertEqual(tasks["T-A"]["end"], tasks["T-B"]["end"])
        self.assertEqual(sections["campaign_schedule"]["data"]["resource_conflicts"], [])

    def test_milp_obeys_budget_coverage_mandatory_tasks_and_exclusions(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "预算内至少完成一项认知传播",
                    "currency": "CNY",
                    "timezone": "UTC",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-11",
                    "budget": {"maximum": 550, "reserve": 0},
                },
                "benefit_unit": "用户提供的报名价值点",
                "mandatory_tasks": ["BASE"],
                "required_coverage": ["awareness"],
                "exclusions": [["OPTION-HIGH", "OPTION-LOW"]],
                "resources": [],
                "tasks": [
                    {"id": "BASE", "title": "事实审校", "duration_hours": 1, "resource_ids": [], "estimated_cost": 100},
                    {"id": "OPTION-HIGH", "title": "门店共创内容", "mandatory": False, "benefit": 10, "coverage": ["awareness"], "duration_hours": 2, "resource_ids": [], "estimated_cost": 450},
                    {"id": "OPTION-LOW", "title": "门店优惠帖", "mandatory": False, "benefit": 6, "coverage": ["awareness"], "duration_hours": 1, "resource_ids": [], "estimated_cost": 350},
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        schedule = sections["campaign_schedule"]["data"]
        tasks = {row["id"]: row for row in schedule["tasks"]}
        self.assertEqual(schedule["optimizer"]["state"], "OPTIMAL")
        self.assertEqual(set(schedule["optimizer"]["selected_task_ids"]), {"BASE", "OPTION-HIGH"})
        self.assertEqual(tasks["OPTION-LOW"]["status"], "NOT_SELECTED_BY_OBJECTIVE")
        budget = sections["campaign_budget"]["data"]["budget_check"]
        self.assertEqual(budget["status"], "WITHIN_LIMIT")
        self.assertEqual(budget["scenario_planning_exposure"], 550)
        self.assertIn("门店优惠帖", sections["campaign_budget"]["body"])
        self.assertIn("未纳入当前安排", sections["campaign_budget"]["body"])

    def test_milp_proves_conflicting_mandatory_tasks_infeasible(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "排期互斥检查",
                    "timezone": "UTC",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-11",
                },
                "resources": [],
                "exclusions": [["T1", "T2"]],
                "tasks": [
                    {"id": "T1", "title": "必须拍摄", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0},
                    {"id": "T2", "title": "必须开场", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0},
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "INFEASIBLE")
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "INFEASIBLE_DECLARED_CONSTRAINTS")
        self.assertTrue(any("互斥任务都被声明为必须完成" in item for item in result["limitations"]))

    def test_milp_proves_mandatory_costs_over_hard_budget_infeasible(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "硬预算内完成必要拍摄",
                    "timezone": "UTC",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-11",
                    "currency": "CNY",
                    "budget": {"maximum": 100, "reserve": 0},
                },
                "resources": [],
                "tasks": [
                    {"id": "T1", "title": "必须拍摄", "duration_hours": 1, "resource_ids": [], "estimated_cost": 80},
                    {"id": "T2", "title": "必须审稿", "duration_hours": 1, "resource_ids": [], "estimated_cost": 50},
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "INFEASIBLE")
        self.assertEqual(sections["campaign_budget"]["data"]["budget_check"]["status"], "OVER_LIMIT")
        self.assertTrue(any("必须完成任务的已知费用" in item for item in result["limitations"]))

    def test_milp_timeout_is_not_reported_as_infeasible(self) -> None:
        with patch(
            "scipy.optimize.milp",
            return_value=SimpleNamespace(status=1, x=None, message="time limit", fun=None),
        ):
            result = evaluate_campaign(
                "campaign_schedule",
                {
                    "brief": {
                        "goal": "时限状态分类",
                        "currency": "CNY",
                        "timezone": "UTC",
                        "start_date": "2026-11-10",
                        "end_date": "2026-11-11",
                        "budget": {"maximum": 10, "reserve": 0},
                    },
                    "resources": [],
                    "tasks": [
                        {"id": "T1", "title": "必须准备", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0}
                    ],
                },
            )
        sections = {section["id"]: section for section in result["sections"]}
        optimizer = sections["campaign_schedule"]["data"]["optimizer"]
        self.assertEqual(optimizer["state"], "LIMIT_REACHED_NO_INCUMBENT")
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "FEASIBLE_FALLBACK_UNOPTIMIZED")
        self.assertIn("没有可证明的MILP最优解", " ".join(result["limitations"]))
        self.assertIn("未判定无解", sections["campaign_schedule"]["body"])
        self.assertFalse(any("不可行组合" in item for item in result["limitations"]))

    def test_schedule_does_not_assume_unstated_resource_availability(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "安排门店拍摄",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-12",
                },
                "resources": [{"id": "R1", "name": "项目摄影", "capacity": 1}],
                "tasks": [
                    {"id": "T1", "title": "拍摄", "duration_hours": 2, "resource_ids": ["R1"], "estimated_cost": 0}
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        task = sections["campaign_schedule"]["data"]["tasks"][0]
        self.assertEqual(task["status"], "NOT_SCHEDULED")
        self.assertIn("没有声明可用期", " ".join(result["limitations"]))
        self.assertIn("未排入", sections["campaign_schedule"]["body"])

    def test_schedule_body_binds_task_to_exact_content_commitment_and_owner(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "完成Post审核",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-12",
                },
                "posts": [{"id": "POST-9", "title": "门店新品试饮"}],
                "resources": [],
                "tasks": [
                    {
                        "id": "T1",
                        "title": "审核Post",
                        "content_kind": "post",
                        "content_ref": "POST-9",
                        "content_version": "v3",
                        "location": "广州天河门店",
                        "account_id": "brand-account-1",
                        "responsible_party": "门店负责人",
                        "commitment_ref": "booking:review-slot-4",
                        "duration_hours": 1,
                        "resource_ids": [],
                        "estimated_cost": 0,
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        task = sections["campaign_schedule"]["data"]["tasks"][0]
        self.assertEqual(task["content_link_status"], "LINKED_TO_OBJECT_IN_INPUT_SNAPSHOT")
        body = sections["campaign_schedule"]["body"]
        for expected in ("门店新品试饮", "v3", "广州天河门店", "brand-account-1", "门店负责人", "booking:review-slot-4"):
            self.assertIn(expected, body)
        self.assertNotIn("POST-9", body)
        self.assertNotIn("T1", body)
        self.assertNotIn("READY_TO_REVIEW", body)

    def test_locked_conflict_is_reported_and_original_times_are_kept(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {"timezone": "Asia/Shanghai", "start_date": "2026-11-10", "end_date": "2026-11-12"},
                "resources": [{"id": "R1", "capacity": 1, "available_from": "2026-11-10", "available_to": "2026-11-12"}],
                "tasks": [
                    {"id": "T1", "title": "已锁拍摄", "duration_hours": 2, "resource_ids": ["R1"], "locked": True, "fixed_start": "2026-11-10T10:00:00", "fixed_end": "2026-11-10T12:00:00"},
                    {"id": "T2", "title": "已锁活动", "duration_hours": 2, "resource_ids": ["R1"], "locked": True, "fixed_start": "2026-11-10T11:00:00", "fixed_end": "2026-11-10T13:00:00"},
                ],
            },
        )
        data = next(section["data"] for section in result["sections"] if section["id"] == "campaign_schedule")
        tasks = {row["id"]: row for row in data["tasks"]}
        self.assertEqual(data["status"], "INFEASIBLE_DECLARED_CONSTRAINTS")
        self.assertEqual(tasks["T1"]["start"], "2026-11-10T10:00:00+08:00")
        self.assertEqual(tasks["T2"]["start"], "2026-11-10T11:00:00+08:00")
        self.assertTrue(data["resource_conflicts"])

    def test_local_replan_preserves_locked_commitment_and_reschedules_dependents(self) -> None:
        inputs = {
            "brief": {
                "goal": "按新增剪辑时长局部重排",
                "currency": "CNY",
                "timezone": "Asia/Shanghai",
                "start_date": "2026-11-10",
                "end_date": "2026-11-15",
                "budget": {"maximum": 10000},
            },
            "resources": [
                {"id": "R1", "capacity": 1, "available_from": "2026-11-10", "available_to": "2026-11-15"}
            ],
            "tasks": [
                {
                    "id": "T1",
                    "title": "已确认拍摄",
                    "duration_hours": 2,
                    "resource_ids": ["R1"],
                    "locked": True,
                    "fixed_start": "2026-11-10T10:00:00",
                    "fixed_end": "2026-11-10T12:00:00",
                    "estimated_cost": 1000,
                },
                {
                    "id": "T2",
                    "title": "剪辑",
                    "duration_hours": 2,
                    "dependencies": ["T1"],
                    "resource_ids": ["R1"],
                    "earliest_start": "2026-11-10T09:00:00",
                    "deadline": "2026-11-12",
                    "estimated_cost": 2000,
                },
                {
                    "id": "T3",
                    "title": "字幕校对",
                    "duration_hours": 1,
                    "dependencies": ["T2"],
                    "resource_ids": ["R1"],
                    "earliest_start": "2026-11-10T09:00:00",
                    "deadline": "2026-11-12",
                    "estimated_cost": 500,
                },
            ],
        }
        first = evaluate_campaign("campaign_schedule", inputs)
        inputs["tasks"][1]["duration_hours"] = 3
        revised = evaluate_campaign("campaign_schedule", inputs)
        first_rows = {
            row["id"]: row
            for row in next(
                section["data"]["tasks"]
                for section in first["sections"]
                if section["id"] == "campaign_schedule"
            )
        }
        revised_rows = {
            row["id"]: row
            for row in next(
                section["data"]["tasks"]
                for section in revised["sections"]
                if section["id"] == "campaign_schedule"
            )
        }
        self.assertEqual(first_rows["T1"]["start"], revised_rows["T1"]["start"])
        self.assertEqual(first_rows["T1"]["end"], revised_rows["T1"]["end"])
        self.assertEqual(first_rows["T2"]["start"], revised_rows["T2"]["start"])
        self.assertNotEqual(first_rows["T3"]["start"], revised_rows["T3"]["start"])

    def test_saved_revision_uses_previous_schedule_and_resource_competition_closure(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        prior_rows = {
            row["id"]: row
            for section in previous_business["generated_sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        candidate = copy.deepcopy(inputs)
        candidate["tasks"][0]["resource_ids"] = ["camera-b"]
        prepared, context = prepare_campaign_revision_inputs(candidate, previous_business)

        self.assertEqual(context["changed_task_ids"], ["shoot-a"])
        self.assertEqual(set(context["replan_task_ids"]), {"shoot-a", "edit-c"})
        self.assertEqual(set(context["affected_task_ids"]), {"shoot-a", "shoot-b", "edit-c"})
        self.assertEqual(set(context["preserved_task_ids"]), {"shoot-b", "design-d"})
        self.assertTrue(
            any(
                row["resource_id"] == "camera-a" and row["basis"] == "previous" and set(row["task_ids"]) == {"shoot-a", "shoot-b"}
                for row in context["resource_competition"]
            )
        )
        self.assertTrue(
            any(
                row["resource_id"] == "camera-b" and row["basis"] == "candidate" and set(row["task_ids"]) == {"shoot-a", "edit-c"}
                for row in context["resource_competition"]
            )
        )
        self.assertNotIn("_campaign_revision_context", candidate)

        revised = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        sections = {section["id"]: section for section in revised["sections"]}
        schedule = sections["campaign_schedule"]["data"]
        tasks = {row["id"]: row for row in schedule["tasks"]}
        self.assertEqual(set(schedule["revision_impact"]["preserved_task_ids"]), {"shoot-b", "design-d"})
        self.assertEqual(tasks["shoot-b"]["start"], prior_rows["shoot-b"]["start"])
        self.assertEqual(tasks["shoot-b"]["end"], prior_rows["shoot-b"]["end"])
        self.assertEqual(tasks["shoot-b"]["resource_ids"], prior_rows["shoot-b"]["resource_ids"])
        self.assertEqual(tasks["design-d"]["start"], prior_rows["design-d"]["start"])
        self.assertEqual(tasks["design-d"]["end"], prior_rows["design-d"]["end"])
        self.assertEqual(tasks["design-d"]["resource_ids"], prior_rows["design-d"]["resource_ids"])
        self.assertEqual(tasks["design-d"]["schedule_revision_lock_reason"], ["confirmed_or_manual_schedule_outside_impact"])
        self.assertTrue(any(row["task_id"] == "shoot-a" for row in schedule["revision_impact"]["moved_tasks"]))
        self.assertIn("本次修订影响", sections["campaign_schedule"]["body"])

    def test_budget_revision_keeps_confirmed_times_and_reports_budget_conflict(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        prior_rows = {
            row["id"]: row
            for section in previous_business["generated_sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        candidate = copy.deepcopy(inputs)
        candidate["brief"] = {"budget": {"maximum": 200}}
        prepared, context = prepare_campaign_revision_inputs(candidate, previous_business)
        self.assertEqual(prepared["brief"]["goal"], inputs["brief"]["goal"])
        self.assertEqual(prepared["brief"]["timezone"], "UTC")
        self.assertEqual(context["replan_task_ids"], [])
        self.assertEqual(set(context["preserved_task_ids"]), set(prior_rows))

        revised = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        sections = {section["id"]: section for section in revised["sections"]}
        schedule = sections["campaign_schedule"]["data"]
        tasks = {row["id"]: row for row in schedule["tasks"]}
        self.assertEqual(schedule["optimizer"]["state"], "INFEASIBLE")
        self.assertEqual(schedule["status"], "INFEASIBLE_DECLARED_CONSTRAINTS")
        self.assertEqual(sections["campaign_budget"]["data"]["budget_check"]["status"], "OVER_LIMIT")
        for identity, prior in prior_rows.items():
            self.assertEqual(tasks[identity]["start"], prior["start"])
            self.assertEqual(tasks[identity]["end"], prior["end"])

    def test_explicit_replan_control_opens_only_selected_task_and_competition_closure(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        prepared, context = prepare_campaign_revision_inputs(
            copy.deepcopy(inputs),
            previous_business,
            controls={
                "replan_tasks": [
                    {"task_id": "shoot-b", "reason": "用户选择重新排这项与共享摄影资源的任务"}
                ]
            },
        )

        self.assertEqual(set(context["replan_task_ids"]), {"shoot-a", "shoot-b"})
        self.assertEqual(set(context["preserved_task_ids"]), {"edit-c", "design-d"})
        self.assertEqual(
            context["explicit_replan_tasks"],
            [{"task_id": "shoot-b", "reason": "用户选择重新排这项与共享摄影资源的任务"}],
        )
        revised = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        schedule = next(section["data"] for section in revised["sections"] if section["id"] == "campaign_schedule")
        tasks = {row["id"]: row for row in schedule["tasks"]}
        prior_tasks = {
            row["id"]: row
            for section in previous_business["generated_sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        for identity in ("edit-c", "design-d"):
            self.assertEqual(tasks[identity]["start"], prior_tasks[identity]["start"])
            self.assertEqual(tasks[identity]["end"], prior_tasks[identity]["end"])

    def test_revision_preserves_actual_execution_fields_and_blocks_hard_task_rewrite(self) -> None:
        inputs, _ = _schedule_revision_fixture()
        original_result = evaluate_campaign("campaign_schedule", inputs)
        prior_rows = {
            row["id"]: row
            for section in original_result["sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        task = inputs["tasks"][0]
        task.update(
            locked=True,
            fixed_start=prior_rows["shoot-a"]["start"],
            fixed_end=prior_rows["shoot-a"]["end"],
            actual_start=prior_rows["shoot-a"]["start"],
            actual_end=prior_rows["shoot-a"]["end"],
            execution_status="user_recorded_complete",
            receipt_status="not_checked",
            external_receipt_refs=["receipt:shoot-a"],
        )
        prior_result = evaluate_campaign("campaign_schedule", inputs)
        previous_business = {
            "operation": "campaign_schedule",
            "inputs": copy.deepcopy(inputs),
            "result": copy.deepcopy(prior_result),
            "generated_sections": copy.deepcopy(prior_result["sections"]),
            "confirmed_sections": [],
            "manual_sections": [],
            "actions": [],
        }
        candidate = copy.deepcopy(inputs)
        for field in ("actual_start", "actual_end", "execution_status", "receipt_status", "external_receipt_refs"):
            candidate["tasks"][0].pop(field)
        prepared, revision_context = prepare_campaign_revision_inputs(candidate, previous_business)
        self.assertEqual(prepared["tasks"][0]["actual_start"], task["actual_start"])
        self.assertEqual(prepared["tasks"][0]["external_receipt_refs"], ["receipt:shoot-a"])
        self.assertIn("shoot-a", revision_context["hard_task_reasons"])

        changed_commitment = copy.deepcopy(candidate)
        changed_commitment["tasks"][0]["resource_ids"] = ["camera-b"]
        with self.assertRaisesRegex(ValueError, "locked or executed task"):
            prepare_campaign_revision_inputs(changed_commitment, previous_business)

    def test_versioned_commitment_release_requires_exact_diff_reason_and_saved_evidence(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        base_rows = {
            row["id"]: row
            for section in previous_business["generated_sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        inputs["tasks"][0].update(
            locked=True,
            fixed_start=base_rows["shoot-a"]["start"],
            fixed_end=base_rows["shoot-a"]["end"],
            commitment_ref="booking:shoot-a:version-2",
        )
        prior = evaluate_campaign("campaign_schedule", inputs)
        previous_business["inputs"] = copy.deepcopy(inputs)
        previous_business["result"] = copy.deepcopy(prior)
        previous_business["generated_sections"] = copy.deepcopy(prior["sections"])

        candidate = copy.deepcopy(inputs)
        task = candidate["tasks"][0]
        task.update(
            locked=False,
            fixed_start=None,
            fixed_end=None,
            resource_ids=["camera-b"],
            commitment_ref=None,
        )
        before = {
            "locked": True,
            "fixed_start": inputs["tasks"][0]["fixed_start"],
            "fixed_end": inputs["tasks"][0]["fixed_end"],
            "resource_ids": ["camera-a"],
            "resource_options": None,
            "commitment_ref": "booking:shoot-a:version-2",
        }
        after = {
            "locked": False,
            "fixed_start": None,
            "fixed_end": None,
            "resource_ids": ["camera-b"],
            "resource_options": None,
            "commitment_ref": None,
        }
        control = {
            "commitment_releases": [
                {
                    "task_id": "shoot-a",
                    "before": before,
                    "after": after,
                    "reason": "用户提供了场地方解除原档期的书面确认",
                    "source_refs": [
                        {
                            "material_id": "material-release-1",
                            "material_sha256": "a" * 64,
                        }
                    ],
                    "evidence_refs": [],
                }
            ]
        }
        prepared, context = prepare_campaign_revision_inputs(
            candidate,
            previous_business,
            controls=control,
            revision_note="按场地方书面解除确认重新排期",
        )
        result = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        sections = {section["id"]: section for section in result["sections"]}
        release = sections["campaign_schedule"]["data"]["revision_impact"]["commitment_releases"][0]

        self.assertEqual(release["record_status"], "user_recorded_release_not_independently_verified")
        self.assertEqual(release["before"]["commitment_ref"], "booking:shoot-a:version-2")
        self.assertIn("不证明场地/供应商已实际取消", sections["campaign_schedule"]["body"])
        self.assertNotIn("material-release-1", sections["campaign_schedule"]["client_body"])
        self.assertNotIn("解除原档期", sections["campaign_schedule"]["client_body"])
        self.assertEqual(prepared["tasks"][0]["commitment_ref"], None)

        altered = copy.deepcopy(control)
        altered["commitment_releases"][0]["before"]["fixed_start"] = "2026-11-10T10:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "before/after must exactly match"):
            prepare_campaign_revision_inputs(
                candidate,
                previous_business,
                controls=altered,
                revision_note="按场地方书面解除确认重新排期",
            )
        missing_evidence = copy.deepcopy(control)
        missing_evidence["commitment_releases"][0]["source_refs"] = []
        with self.assertRaisesRegex(ValueError, "exact saved Material or Artifact evidence"):
            prepare_campaign_revision_inputs(
                candidate,
                previous_business,
                controls=missing_evidence,
                revision_note="按场地方书面解除确认重新排期",
            )
        with self.assertRaisesRegex(ValueError, "requires the revision's human reason"):
            prepare_campaign_revision_inputs(candidate, previous_business, controls=control)

    def test_execution_record_correction_requires_versioned_before_after_and_evidence(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        task = inputs["tasks"][0]
        task.update(
            actual_start="2026-11-10T09:15:00+00:00",
            actual_end="2026-11-10T10:15:00+00:00",
            execution_status="user_recorded_complete",
        )
        prior = evaluate_campaign("campaign_schedule", inputs)
        previous_business["inputs"] = copy.deepcopy(inputs)
        previous_business["result"] = copy.deepcopy(prior)
        previous_business["generated_sections"] = copy.deepcopy(prior["sections"])
        candidate = copy.deepcopy(inputs)
        candidate["tasks"][0]["actual_start"] = "2026-11-10T09:20:00+00:00"
        control = {
            "execution_corrections": [
                {
                    "task_id": "shoot-a",
                    "fields": ["actual_start"],
                    "before": {"actual_start": "2026-11-10T09:15:00+00:00"},
                    "after": {"actual_start": "2026-11-10T09:20:00+00:00"},
                    "reason": "现场回执时间戳录入错误，按原始记录更正",
                    "source_refs": [
                        {
                            "material_id": "material-execution-1",
                            "material_sha256": "b" * 64,
                        }
                    ],
                    "evidence_refs": [],
                }
            ]
        }
        prepared, context = prepare_campaign_revision_inputs(
            candidate,
            previous_business,
            controls=control,
            revision_note="更正拍摄实际开始时间",
        )
        result = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        sections = {section["id"]: section for section in result["sections"]}
        schedule = sections["campaign_schedule"]["data"]
        task_result = next(row for row in schedule["tasks"] if row["id"] == "shoot-a")

        self.assertEqual(task_result["execution"]["actual_start"], "2026-11-10T09:20:00+00:00")
        self.assertIn("shoot-a", schedule["revision_impact"]["preserved_task_ids"])
        self.assertEqual(
            schedule["revision_impact"]["execution_corrections"][0]["record_status"],
            "user_corrected_execution_record_in_new_artifact_version",
        )
        self.assertIn("本次未独立核验", sections["campaign_schedule"]["body"])

        with self.assertRaisesRegex(ValueError, "execution_corrections revision control"):
            prepare_campaign_revision_inputs(candidate, previous_business)

    def test_file_and_browser_receipts_do_not_imply_campaign_task_completion(self) -> None:
        inputs, previous_business = _schedule_revision_fixture()
        previous_business["actions"] = [
            {
                "id": "file-job",
                "target": {"kind": "campaign_task", "id": "shoot-a"},
                "state": "executed",
                "execution": {"kind": "artifact_export"},
                "records": [{"status": "file_generated", "external_delivery": "not_sent"}],
            },
            {
                "id": "browser-job",
                "target": {"kind": "campaign_task", "id": "shoot-b"},
                "state": "awaiting_outcome_verification",
                "execution": {"kind": "browser_action"},
                "records": [{"status": "browser_input_delivered", "business_outcome": "not_observed"}],
            },
        ]
        candidate = copy.deepcopy(inputs)
        candidate["tasks"][0]["resource_ids"] = ["camera-b"]
        prepared, context = prepare_campaign_revision_inputs(candidate, previous_business)

        self.assertNotIn("shoot-a", context["hard_task_reasons"])
        self.assertNotIn("shoot-b", context["hard_task_reasons"])
        result = evaluate_campaign("campaign_schedule", prepared, revision_context=context)
        schedule = next(section["data"] for section in result["sections"] if section["id"] == "campaign_schedule")
        task_rows = {row["id"]: row for row in schedule["tasks"]}
        self.assertEqual(task_rows["shoot-a"]["execution"]["record_status"], "no_actual_execution_record_supplied")
        self.assertNotEqual(task_rows["shoot-a"]["status"], "PRESERVED_LOCKED")

    def test_campaign_revision_context_is_not_a_user_input_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "internal"):
            evaluate_campaign(
                "campaign_schedule",
                {"_campaign_revision_context": {"kind": "campaign_schedule_revision"}},
            )

    def test_restore_merge_preserves_latest_task_actuals_and_control_history_without_readding_removed_tasks(self) -> None:
        restored_inputs, restored_business_seed = _schedule_revision_fixture()
        restored_result = evaluate_campaign("campaign_schedule", restored_inputs)
        restored_business = {
            "operation": "campaign_schedule",
            "inputs": copy.deepcopy(restored_inputs),
            "result": copy.deepcopy(restored_result),
            "generated_sections": copy.deepcopy(restored_result["sections"]),
            "manual_sections": ["campaign_schedule"],
            "campaign_task_history": [],
            "campaign_revision_history": [],
        }
        restored_schedule = next(
            section for section in restored_business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        restored_schedule["body"] = "人工确认的旧排期正文。"
        restored_schedule["client_body"] = "对客旧排期正文。"
        restored_business["result"]["sections"] = copy.deepcopy(restored_business["generated_sections"])

        current_inputs = copy.deepcopy(restored_inputs)
        current_inputs["tasks"][0].update(
            actual_start="2026-11-10T09:05:00+00:00",
            actual_end="2026-11-10T10:05:00+00:00",
            execution_status="user_recorded_complete",
            external_receipt_refs=["task-receipt:shoot-a"],
        )
        current_inputs["tasks"].append(
            {
                "id": "later-actual-event",
                "title": "后来追加的实际活动",
                "duration_hours": 1,
                "resource_ids": [],
                "estimated_cost": 0,
                "currency": "CNY",
                "actual_cost": 240,
                "refund": 40,
                "actual_start": "2026-11-10T11:00:00+00:00",
                "actual_end": "2026-11-10T12:00:00+00:00",
                "execution_status": "user_recorded_complete",
            }
        )
        current_inputs["tasks"].append(
            {
                "id": "file-only-task",
                "title": "导出活动方案文件",
                "duration_hours": 1,
                "resource_ids": [],
                "estimated_cost": 0,
                "currency": "CNY",
            }
        )
        current_result = evaluate_campaign("campaign_schedule", current_inputs)
        controls = {
            "commitment_releases": [
                {
                    "task_id": "later-actual-event",
                    "reason": "场地方给出改期书面确认",
                    "source_refs": ["public label preserved only as user statement"],
                }
            ]
        }
        current_business = {
            "operation": "campaign_schedule",
            "inputs": current_inputs,
            "result": current_result,
            "generated_sections": current_result["sections"],
            "impact": {"campaign_schedule_controls": controls},
            "actions": [
                {
                    "id": "file-export",
                    "target": {"kind": "campaign_task", "id": "file-only-task"},
                    "state": "executed",
                    "execution": {"kind": "artifact_export"},
                    "records": [{"status": "file_generated", "external_delivery": "not_sent"}],
                }
            ],
            "campaign_task_history": [],
            "campaign_revision_history": [],
        }

        fragment = preserve_campaign_revision_state_on_restore(
            restored_business,
            current_business,
            current_version=4,
            restore_version=1,
        )
        restored_tasks = {row["id"]: row for row in fragment["inputs"]["tasks"]}
        restored_sections = {section["id"]: section for section in fragment["generated_sections"]}
        schedule_data = restored_sections["campaign_schedule"]["data"]
        task_history = {row["task_id"]: row for row in fragment["campaign_task_history"]}

        self.assertEqual(restored_tasks["shoot-a"]["actual_start"], "2026-11-10T09:05:00+00:00")
        self.assertEqual(restored_tasks["shoot-a"]["external_receipt_refs"], ["task-receipt:shoot-a"])
        self.assertEqual(restored_tasks["shoot-a"]["resource_ids"], restored_inputs["tasks"][0]["resource_ids"])
        self.assertNotIn("later-actual-event", restored_tasks)
        self.assertIn("later-actual-event", task_history)
        self.assertEqual(task_history["later-actual-event"]["actual_cost_fields"], {"actual_cost": 240, "refund": 40, "currency": "CNY"})
        self.assertNotIn("file-only-task", task_history)
        self.assertEqual(restored_sections["campaign_schedule"]["body"], "人工确认的旧排期正文。")
        self.assertEqual(restored_sections["campaign_schedule"]["client_body"], "对客旧排期正文。")
        self.assertEqual(restored_sections["campaign_schedule"]["data"]["tasks"][0]["start"], restored_schedule["data"]["tasks"][0]["start"])
        self.assertEqual(schedule_data["restored_revision_history"][0]["source_version"], 4)
        self.assertTrue(schedule_data["restored_revision_history"][0]["not_reapplied_on_restore"])

    def test_restore_does_not_revive_actual_time_explicitly_corrected_to_unknown(self) -> None:
        restored_inputs, _ = _schedule_revision_fixture()
        restored_inputs["tasks"][0].update(
            actual_start="2026-11-10T09:05:00+00:00",
            actual_end="2026-11-10T10:05:00+00:00",
            execution_status="user_recorded_complete",
        )
        restored_result = evaluate_campaign("campaign_schedule", restored_inputs)
        restored_business = {
            "operation": "campaign_schedule",
            "inputs": copy.deepcopy(restored_inputs),
            "result": copy.deepcopy(restored_result),
            "generated_sections": copy.deepcopy(restored_result["sections"]),
        }

        current_inputs = copy.deepcopy(restored_inputs)
        current_inputs["tasks"][0]["actual_start"] = None
        current_inputs["tasks"][0]["actual_end"] = "UNKNOWN"
        current_inputs["tasks"][0]["execution_status"] = "UNKNOWN"
        current_result = evaluate_campaign("campaign_schedule", current_inputs)
        correction = {
            "task_id": "shoot-a",
            "fields": ["actual_start", "actual_end"],
            "before": {
                "actual_start": "2026-11-10T09:05:00+00:00",
                "actual_end": "2026-11-10T10:05:00+00:00",
            },
            "after": {"actual_start": None, "actual_end": "UNKNOWN"},
            "reason": "原始回执确认此前填入的时刻不可信，现标为未知",
            "source_refs": [
                {"material_id": "material-correction-1", "material_sha256": "c" * 64}
            ],
            "evidence_refs": [],
            "record_status": "user_corrected_execution_record_in_new_artifact_version",
        }
        current_business = {
            "operation": "campaign_schedule",
            "inputs": current_inputs,
            "result": current_result,
            "generated_sections": current_result["sections"],
            "campaign_revision_history": [
                {"source_version": 4, "execution_corrections": [correction]}
            ],
            "impact": {"campaign_schedule_controls": {"execution_corrections": [correction]}},
        }

        fragment = preserve_campaign_revision_state_on_restore(
            restored_business,
            current_business,
            current_version=4,
            restore_version=1,
        )
        restored_task = next(row for row in fragment["inputs"]["tasks"] if row["id"] == "shoot-a")
        task_history = next(row for row in fragment["campaign_task_history"] if row["task_id"] == "shoot-a")
        section = next(row for row in fragment["generated_sections"] if row["id"] == "campaign_schedule")
        schedule_row = next(row for row in section["data"]["tasks"] if row["id"] == "shoot-a")
        history_correction = next(
            row for row in fragment["campaign_revision_history"]
            if row.get("source_version") == 4 and row.get("execution_corrections")
        )["execution_corrections"][0]

        self.assertIsNone(restored_task["actual_start"])
        self.assertEqual(restored_task["actual_end"], "UNKNOWN")
        self.assertIsNone(task_history["execution_fields"]["actual_start"])
        self.assertEqual(task_history["execution_fields"]["actual_end"], "UNKNOWN")
        self.assertIsNone(schedule_row["execution"]["actual_start"])
        self.assertEqual(schedule_row["execution"]["actual_end"], "UNKNOWN")
        self.assertEqual(history_correction["before"]["actual_start"], "2026-11-10T09:05:00+00:00")
        self.assertIsNone(history_correction["after"]["actual_start"])
        internal_body = render_campaign_task_history(
            {"operation": "campaign_schedule", **fragment}
        )
        self.assertIn("当前 Campaign 任务实际记录", internal_body)
        self.assertIn("用户记录的实际开始：未知", internal_body)
        self.assertIn("实际开始时间 2026-11-10T09:05:00+00:00 → 未知", internal_body)
        self.assertNotIn("导出活动方案文件", internal_body)

    def test_actual_execution_and_observation_remain_separate_from_plan(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "核对一项已执行的拍摄安排",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-12",
                    "budget": {"maximum": 2000},
                },
                "resources": [
                    {"id": "R1", "capacity": 1, "available_from": "2026-11-10", "available_to": "2026-11-12"}
                ],
                "tasks": [
                    {
                        "id": "T1",
                        "title": "拍摄",
                        "duration_hours": 2,
                        "resource_ids": ["R1"],
                        "earliest_start": "2026-11-10T09:00:00",
                        "deadline": "2026-11-10",
                        "estimated_cost": 800,
                        "actual_cost": 900,
                        "refund": 100,
                        "actual_start": "2026-11-10T10:00:00",
                        "actual_end": "2026-11-10T13:00:00",
                        "execution_status": "user_recorded_complete",
                        "receipt_status": "not_checked",
                        "external_receipt_refs": ["receipt:local:1"],
                        "metric_snapshots": [
                            {
                                "metric": "门店试饮人数",
                                "value": 0,
                                "unit": "people",
                                "denominator": 0,
                                "observation_window": "活动场次1",
                                "observed_at": "2026-11-10T14:00:00",
                                "source_refs": ["sheet:events#row:0"],
                            }
                        ],
                    }
                ],
            },
        )

        sections = {section["id"]: section for section in result["sections"]}
        task = sections["campaign_schedule"]["data"]["tasks"][0]
        self.assertEqual(task["start"], "2026-11-10T09:00:00+08:00")
        self.assertEqual(task["execution"]["actual_start"], "2026-11-10T10:00:00")
        self.assertEqual(task["execution"]["schedule_variance_hours"], 1)
        self.assertEqual(task["execution"]["actual_duration_hours"], 3)
        self.assertEqual(task["execution"]["record_status"], "user_supplied_execution_not_independently_verified")
        self.assertEqual(task["execution"]["metric_snapshots"][0]["value"], 0)
        self.assertEqual(task["execution"]["metric_snapshots"][0]["source_refs"], ["sheet:events#row:0"])
        cost_states = sections["campaign_budget"]["data"]["budget_check"]["scenario_costs"][0]["amounts_by_status"]
        self.assertEqual(cost_states, {"actual": 900, "estimate": 800, "refund": 100})

    def test_impossible_task_window_is_reported_as_a_business_conflict(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "报告截止期冲突",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-15",
                },
                "resources": [],
                "tasks": [
                    {
                        "id": "T1",
                        "duration_hours": 1,
                        "earliest_start": "2026-11-12",
                        "deadline": "2026-11-10",
                        "resource_ids": [],
                        "estimated_cost": 0,
                    }
                ],
            },
        )
        section = next(section for section in result["sections"] if section["id"] == "campaign_schedule")
        self.assertEqual(section["data"]["tasks"][0]["status"], "NOT_SCHEDULED")
        self.assertTrue(any("截止时间" in item for item in section["data"]["conflicts"]))

    def test_campaign_series_post_topic_links_and_publication_times_are_preserved(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "安排新产品的连续门店内容",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-20",
                    "budget": {"maximum": 1000},
                },
                "campaigns": [
                    {
                        "id": "C1",
                        "title": "门店新品体验",
                        "source_refs": ["brief:campaign-1"],
                    }
                ],
                "topics": [
                    {"id": "TOP1", "title": "试饮反馈", "source_refs": ["topic:1"]},
                    {"id": "TOP2", "title": "门店日常", "evidence_refs": ["evidence:topic-2"]},
                ],
                "series": [
                    {"id": "S1", "campaign_id": "C1", "topic_ids": ["TOP1"]},
                    {"id": "S2", "campaign_id": "C1", "topic_ids": ["TOP1", "TOP2"]},
                ],
                "posts": [
                    {
                        "id": "P0",
                        "series_id": "S1",
                        "topic_ids": ["TOP1"],
                        "platform": "platform-a",
                        "account_id": "acct-1",
                        "planned_publish_at": "2026-11-12T10:00:00",
                    },
                    {
                        "id": "P1",
                        "title": "新品试饮邀请",
                        "hook": "先尝，再选择",
                        "body": "到店参加本周试饮并告诉我们口味反馈。",
                        "cta": "到店报名",
                        "hashtags": ["新品试饮", "广州门店"],
                        "visual_brief": "门店试饮台与产品特写，不展示未获授权顾客。",
                        "locked_fields": ["hook", "body"],
                        "series_id": "S2",
                        "variant_of": "P0",
                        "topic_ids": ["TOP1", "TOP2"],
                        "platform": "platform-b",
                        "account_id": "acct-2",
                        "planned_publish_at": "2026-11-13T10:00:00",
                        "actual_publish_at": "2026-11-13T10:30:00",
                        "source_refs": ["post:variant-1"],
                    },
                    {"id": "P2", "topic_ids": ["TOP2"]},
                ],
                "calendar_nodes": [
                    {
                        "id": "N1",
                        "title": "客户确认的活动日",
                        "year": 2026,
                        "start": "2026-11-15",
                        "end": "2026-11-15",
                        "source_refs": ["client-calendar:1"],
                    }
                ],
                "resources": [],
                "tasks": [
                    {"id": "T1", "title": "准备内容", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0}
                ],
            },
        )

        content = next(section for section in result["sections"] if section["id"] == "campaign_content")
        self.assertEqual(content["data"]["series"][0]["campaign_id"], "C1")
        self.assertEqual(content["data"]["series"][1]["topic_ids"], ["TOP1", "TOP2"])
        posts = {row["id"]: row for row in content["data"]["posts"]}
        self.assertEqual(posts["P1"]["variant_of"], "P0")
        self.assertEqual(posts["P1"]["planned_publish_at"], "2026-11-13T10:00:00")
        self.assertEqual(posts["P1"]["actual_publish_at"], "2026-11-13T10:30:00")
        self.assertEqual(posts["P1"]["locked_fields"], ["hook", "body"])
        self.assertIn("到店报名", content["body"])
        self.assertIn("门店试饮台", content["body"])
        self.assertIn("人工锁定字段：hook；body", content["body"])
        self.assertEqual(posts["P1"]["publication_record_status"], "user_supplied_publication_not_independently_verified")
        self.assertIsNone(posts["P2"]["series_id"])
        self.assertEqual(content["data"]["calendar_nodes"][0]["source_refs"], ["client-calendar:1"])

    def test_standalone_post_can_start_without_a_campaign_or_task_plan(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "从单条门店内容开始",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-15",
                },
                "posts": [
                    {
                        "id": "P-alone",
                        "platform": "platform-a",
                        "account_id": "acct-1",
                        "planned_publish_at": "2026-11-12T10:00:00",
                        "source_refs": ["post:draft-1"],
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(
            sections["campaign_schedule"]["data"]["status"],
            "NOT_ASSESSED_NO_TASKS",
        )
        self.assertEqual(sections["campaign_content"]["data"]["posts"][0]["id"], "P-alone")
        self.assertIsNone(sections["campaign_content"]["data"]["posts"][0]["series_id"])
        self.assertEqual(
            sections["campaign_budget"]["data"]["budget_check"]["status"],
            "NO_TASKS_TO_PRICE",
        )

    def test_standalone_post_draft_needs_no_campaign_window_or_timezone(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {"goal": "先保存一条独立Post草稿"},
                "posts": [
                    {
                        "id": "P-DRAFT",
                        "title": "门店试饮邀请",
                        "hook": "先尝，再选择",
                        "body": "到店参加试饮。",
                        "cta": "查看门店信息",
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "NOT_ASSESSED_NO_TASKS")
        self.assertIsNone(sections["campaign_scope"]["data"]["project_window"]["start"])
        self.assertIn("先尝，再选择", sections["campaign_content"]["body"])

    def test_unknown_resource_capacity_and_duration_are_not_defaulted(self) -> None:
        base = {
            "brief": {
                "goal": "排入门店拍摄",
                "timezone": "Asia/Shanghai",
                "start_date": "2026-11-10",
                "end_date": "2026-11-12",
            },
            "resources": [
                {
                    "id": "R1",
                    "name": "摄影",
                    "available_from": "2026-11-10",
                    "available_to": "2026-11-12",
                }
            ],
            "tasks": [
                {"id": "T1", "title": "门店拍摄", "duration_hours": 2, "resource_ids": ["R1"], "estimated_cost": 0}
            ],
        }
        capacity_result = evaluate_campaign("campaign_schedule", base)
        capacity_sections = {section["id"]: section for section in capacity_result["sections"]}
        self.assertEqual(capacity_sections["campaign_schedule"]["data"]["tasks"][0]["status"], "NOT_SCHEDULED")
        self.assertIn("可用数量 未知", capacity_sections["campaign_resources"]["body"])

        duration_inputs = copy.deepcopy(base)
        duration_inputs["resources"] = []
        duration_inputs["tasks"][0]["resource_ids"] = []
        duration_inputs["tasks"][0]["duration_hours"] = None
        duration_result = evaluate_campaign("campaign_schedule", duration_inputs)
        duration_sections = {section["id"]: section for section in duration_result["sections"]}
        self.assertEqual(duration_sections["campaign_schedule"]["data"]["tasks"][0]["status"], "NOT_SCHEDULED")
        self.assertIn("未提供可靠工时", " ".join(duration_result["limitations"]))

    def test_saved_post_variant_keeps_exact_artifact_version_without_exporting_ids(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "从已保存原稿制作平台变体",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-15",
                },
                "posts": [
                    {
                        "id": "source:ARTIFACT-SECRET:v4:post-old",
                        "title": "门店试饮原稿",
                        "body": "原稿正文",
                        "source_display": "门店试饮原稿 · v4",
                        "source_artifact_reference": {
                            "artifact_id": "ARTIFACT-SECRET",
                            "artifact_version": 4,
                            "post_id": "post-old",
                        },
                    },
                    {
                        "id": "post-1",
                        "title": "平台短版",
                        "body": "适配后的短版正文",
                        "variant_of": "source:ARTIFACT-SECRET:v4:post-old",
                        "variant_source_label": "门店试饮原稿 · v4",
                    },
                ],
            },
        )
        section = next(item for item in result["sections"] if item["id"] == "campaign_content")
        source, variant = section["data"]["posts"]
        self.assertEqual(source["source_artifact_reference"]["artifact_version"], 4)
        self.assertEqual(source["source_artifact_reference"]["post_id"], "post-old")
        self.assertEqual(variant["variant_link_status"], "linked_in_snapshot")
        self.assertIn("门店试饮原稿 · v4", section["body"])
        self.assertIn("适配后的短版正文", section["body"])
        self.assertNotIn("ARTIFACT-SECRET", section["body"])
        self.assertNotIn("source:ARTIFACT-SECRET", section["body"])

    def test_member_touch_requires_scoped_consent_and_never_sends(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "规划会员活动提醒",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-15",
                },
                "member_touchpoints": [
                    {
                        "id": "MT1",
                        "audience_id": "SEG-RETURNING",
                        "consent_status": "granted",
                        "purpose": "新品试饮活动提醒",
                        "consent_scope": "仅广州活动通知",
                        "channels": ["app_inbox"],
                        "valid_until": "2026-11-30",
                        "opt_out_handling": "收到退订后立即停止后续触达",
                        "source_refs": ["consent:batch-19"],
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        touch = sections["campaign_content"]["data"]["member_touchpoints"][0]
        self.assertEqual(
            touch["contactability"],
            "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK",
        )
        self.assertEqual(touch["external_contact"], "not_performed_by_evaluator")
        self.assertIn("会员触点", sections["campaign_content"]["body"])
        self.assertEqual(
            sections["campaign_schedule"]["data"]["status"],
            "NOT_ASSESSED_NO_TASKS",
        )

    def test_member_offer_applies_per_member_cap_history_and_real_budget_once(self) -> None:
        inputs = _member_activation_inputs()
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]
        touchpoint = sections["campaign_content"]["data"]["member_touchpoints"][0]

        self.assertEqual(pool["maximum_redemptions_upper_bound"], 90)
        self.assertEqual(pool["remaining_redemptions_upper_bound"], 75)
        self.assertEqual(pool["prior_redemptions_consumed"], 15)
        self.assertEqual(pool["planned_cost_amount"], 150)
        self.assertEqual(pool["actual_history"]["actual_spend"]["amount"], 200)
        self.assertEqual(pool["actual_history"]["actual_refund"]["amount"], 40)
        self.assertEqual(pool["actual_history"]["refunded_quantity"], 2)
        self.assertEqual(
            touchpoint["activation_assessment"]["status"], "PLAN_READY_FOR_REVIEW"
        )
        self.assertEqual(
            touchpoint["activation"]["external_contact"], "not_performed_by_evaluator"
        )
        self.assertIn("面额 10 CNY", sections["campaign_content"]["body"])
        self.assertIn("预算敞口 150 CNY，不含面额", sections["campaign_content"]["body"])
        self.assertIn("实支 200 CNY；退款 40 CNY", sections["campaign_content"]["body"])
        self.assertEqual(
            sections["campaign_budget"]["data"]["budget_check"]["scenario_costs"][0]["planning_exposure"],
            150,
        )
        self.assertNotIn("新品试饮券", sections["campaign_content"]["client_body"])
        self.assertIn("会员触点准备", sections["campaign_schedule"]["client_body"])
        self.assertNotIn("会员活动触点 1", sections["campaign_schedule"]["client_body"])
        self.assertIn("不执行联系", sections["campaign_content"]["client_body"])
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "READY_TO_REVIEW")

    def test_explicitly_client_visible_member_offer_renders_safe_professional_fields(self) -> None:
        inputs = _member_activation_inputs()
        offer = inputs["member_touchpoints"][0]["activation"]["offer"]
        offer["client_visible"] = True
        offer["client_summary"] = "持券到店可参加一次新品试饮，按活动现场规则核销。"
        offer["client_quote"] = {
            "label": "会员活动方案对客报价",
            "amount": 280,
            "currency": "CNY",
            "status": "quote",
            "valid_until": "2026-11-20",
        }
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        client_body = sections["campaign_content"]["client_body"]

        for expected in (
            "新品试饮券",
            "权益面额：10 CNY",
            "已确认的广州活动报名会员，每人限领一次",
            "2026-11-01",
            "应用内消息",
            "一次新品试饮",
        ):
            self.assertIn(expected, client_body)
        client_budget = sections["campaign_budget"]["client_body"]
        self.assertIn("会员活动方案对客报价", client_budget)
        self.assertIn("280 CNY", client_budget)
        self.assertIn("不代表已付款、已发送或实际履约", client_budget)
        combined_client_body = "\n".join(
            section["client_body"] for section in result["sections"]
        )
        for internal in (
            "2 CNY",
            "150 CNY",
            "100 人",
            "实际支出",
            "200 CNY",
            "segment-1",
            "consent:segment-1",
            "offer-ledger:offer-1",
        ):
            self.assertNotIn(internal, combined_client_body)
        self.assertIn("实际发送、发券或履约", "；".join(result["client_limitations"]))

        hidden_inputs = copy.deepcopy(inputs)
        hidden_inputs["member_touchpoints"][0]["activation"]["offer"]["client_visible"] = False
        hidden = evaluate_campaign("campaign_schedule", hidden_inputs)
        hidden_body = "\n".join(section["client_body"] for section in hidden["sections"])
        self.assertNotIn("新品试饮券", hidden_body)
        self.assertNotIn("会员活动方案对客报价", hidden_body)
        self.assertNotIn("280 CNY", hidden_body)

    def test_shared_offer_pool_is_one_budget_line_across_multiple_touchpoints(self) -> None:
        inputs = _member_activation_inputs(["member-send-a", "member-send-b"])
        inputs["brief"]["budget"]["maximum"] = 160
        result = evaluate_campaign(
            "campaign_schedule", inputs
        )
        sections = {section["id"]: section for section in result["sections"]}
        pools = sections["campaign_schedule"]["data"]["member_offer_pools"]
        budget = sections["campaign_budget"]["data"]
        offer_lines = [line for line in budget["cost_lines"] if line["id"] == "member-offer:offer-pool-1"]

        self.assertEqual(len(pools), 1)
        self.assertEqual(set(pools[0]["task_ids"]), {"member-send-a", "member-send-b"})
        self.assertEqual(len(offer_lines), 1)
        self.assertEqual(offer_lines[0]["amount"], 150)
        self.assertEqual(budget["budget_check"]["scenario_costs"][0]["planning_exposure"], 150)
        self.assertTrue(budget["optimizer"]["budget_constraint_active"])

    def test_shared_offer_pool_cost_is_hard_budget_constraint(self) -> None:
        inputs = _member_activation_inputs(["member-send-a", "member-send-b"])
        inputs["brief"]["budget"]["maximum"] = 149
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}

        self.assertEqual(
            sections["campaign_schedule"]["data"]["optimizer"]["state"], "INFEASIBLE"
        )
        self.assertEqual(
            sections["campaign_schedule"]["data"]["status"],
            "INFEASIBLE_DECLARED_CONSTRAINTS",
        )

    def test_shared_offer_activations_obey_existing_resource_capacity(self) -> None:
        inputs = _member_activation_inputs(["member-send-a", "member-send-b"])
        for task in inputs["tasks"]:
            task["resource_ids"] = ["member-ops"]
        inputs["resources"] = [
            {
                "id": "member-ops",
                "name": "会员运营同事",
                "capacity": 1,
                "availability": [
                    {
                        "start": "2026-11-11T08:00:00+08:00",
                        "end": "2026-11-11T10:00:00+08:00",
                    }
                ],
            }
        ]
        for row in inputs["member_touchpoints"]:
            row["activation"]["contact_window"]["end"] = "2026-11-11T10:00:00+08:00"
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        tasks = sections["campaign_schedule"]["data"]["tasks"]
        first, second = sorted(tasks, key=lambda row: row["start"])

        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "OPTIMAL")
        self.assertLessEqual(first["end"], second["start"])
        self.assertEqual(sections["campaign_schedule"]["data"]["resource_conflicts"], [])
        self.assertTrue(
            all(
                row["activation_assessment"]["status"] == "PLAN_READY_FOR_REVIEW"
                for row in sections["campaign_content"]["data"]["member_touchpoints"]
            )
        )

    def test_shared_offer_pool_rejects_conflicting_terms_but_keeps_multiple_segments(self) -> None:
        inputs = _member_activation_inputs(["member-send-a", "member-send-b"])
        second_offer = inputs["member_touchpoints"][1]["activation"]["offer"]
        second_offer["redemption_limit"]["quantity"] = 2

        with self.assertRaisesRegex(ValueError, "conflicting terms"):
            evaluate_campaign("campaign_schedule", inputs)

    def test_unknown_per_member_denominator_is_not_inferred_from_estimate(self) -> None:
        inputs = _member_activation_inputs()
        offer = inputs["member_touchpoints"][0]["activation"]["offer"]
        offer["inventory"] = {"status": "unlimited"}
        offer["audience"] = {
            "eligible_member_count": 100,
            "count_basis": "unknown",
            "population_scope": "会员规模估数，未确认去重资格人数",
        }
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]

        self.assertIsNone(pool["maximum_redemptions_upper_bound"])
        self.assertIsNone(pool["planned_cost_amount"])
        self.assertEqual(
            sections["campaign_budget"]["data"]["budget_check"]["status"],
            "PARTIAL_COST_UNKNOWN",
        )
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")
        self.assertTrue(
            any("总权益数量上界" in item for item in sections["campaign_schedule"]["data"]["conflicts"])
        )

    def test_total_cap_can_bound_budget_without_audience_count_but_unknown_inventory_blocks_review(self) -> None:
        inputs = _member_activation_inputs()
        offer = inputs["member_touchpoints"][0]["activation"]["offer"]
        offer["redemption_limit"] = {"basis": "total", "quantity": 100}
        offer["inventory"] = {"status": "unknown"}
        offer["audience"] = {"count_basis": "unknown"}
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]

        self.assertEqual(pool["maximum_redemptions_upper_bound"], 100)
        self.assertEqual(pool["planned_cost_amount"], 170)
        self.assertEqual(sections["campaign_schedule"]["data"]["optimizer"]["state"], "OPTIMAL")
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")
        self.assertEqual(
            sections["campaign_content"]["data"]["member_touchpoints"][0]["activation_assessment"]["status"],
            "REQUIRES_INPUT_OR_AUTHORITY_RECHECK",
        )

    def test_unknown_unit_cost_does_not_become_zero_or_budget_feasible(self) -> None:
        inputs = _member_activation_inputs()
        inputs["member_touchpoints"][0]["activation"]["offer"]["unit_business_cost"] = {
            "amount": None,
            "currency": "CNY",
            "status": "unknown",
        }
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]

        self.assertIsNone(pool["planned_cost_amount"])
        self.assertEqual(
            sections["campaign_budget"]["data"]["budget_check"]["status"],
            "PARTIAL_COST_UNKNOWN",
        )
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")

    def test_unknown_frequency_history_does_not_become_zero(self) -> None:
        inputs = _member_activation_inputs()
        history = inputs["member_touchpoints"][0]["activation"]["frequency_rule"]["history"]
        history["status"] = "unknown"
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        assessment = sections["campaign_content"]["data"]["member_touchpoints"][0]["activation_assessment"]

        self.assertIsNone(assessment["frequency_check"]["prior_contacts_upper_bound"])
        self.assertIsNone(assessment["frequency_check"]["projected_contacts_upper_bound"])
        self.assertIn("历史未知", "；".join(assessment["blockers"]))
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")

    def test_incomplete_redemption_history_is_preserved_and_never_zeroed(self) -> None:
        inputs = _member_activation_inputs()
        history = inputs["member_touchpoints"][0]["activation"]["offer"]["history"]
        history["status"] = "partial"
        history["redeemed_quantity"] = 23
        history["restored_quantity"] = 4
        history["refunded_quantity"] = 3
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]
        preserved = pool["actual_history"]

        self.assertEqual(pool["history_effect"], "history_not_subtracted")
        self.assertEqual(pool["remaining_redemptions_upper_bound"], 90)
        self.assertEqual(preserved["redeemed_quantity"], 23)
        self.assertEqual(preserved["restored_quantity"], 4)
        self.assertEqual(preserved["refunded_quantity"], 3)
        self.assertEqual(preserved["actual_spend"]["amount"], 200)
        self.assertEqual(preserved["actual_refund"]["amount"], 40)
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")

    def test_history_over_redemption_pool_cap_is_reported_as_a_real_conflict(self) -> None:
        inputs = _member_activation_inputs()
        history = inputs["member_touchpoints"][0]["activation"]["offer"]["history"]
        history["redeemed_quantity"] = 100
        history["restored_quantity"] = 0
        history["refunded_quantity"] = 0
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        pool = sections["campaign_schedule"]["data"]["member_offer_pools"][0]
        assessment = sections["campaign_content"]["data"]["member_touchpoints"][0]["activation_assessment"]

        self.assertEqual(pool["maximum_redemptions_upper_bound"], 90)
        self.assertTrue(pool["history_limit_conflict"])
        self.assertEqual(pool["remaining_redemptions_upper_bound"], 0)
        self.assertIn("超过声明的权益池总上限", "；".join(assessment["blockers"]))
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")

    def test_overlapping_audience_segments_use_conservative_frequency_upper_bound(self) -> None:
        inputs = _member_activation_inputs(["member-send-a", "member-send-b"])
        for row in inputs["member_touchpoints"]:
            row["activation"]["frequency_rule"]["max_contacts_per_member"] = 3
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        touch = sections["campaign_content"]["data"]["member_touchpoints"][0]
        check = touch["activation_assessment"]["frequency_check"]

        self.assertEqual(check["prior_contacts_upper_bound"], 2)
        self.assertEqual(check["planned_contacts_upper_bound"], 2)
        self.assertEqual(check["projected_contacts_upper_bound"], 4)
        self.assertIn("不同受众段按可能重叠", check["possible_overlap_basis"])
        self.assertEqual(touch["activation_assessment"]["status"], "REQUIRES_INPUT_OR_AUTHORITY_RECHECK")
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")

    def test_member_activation_respects_opt_out_channel_consent_and_task_window(self) -> None:
        inputs = _member_activation_inputs()
        touch = inputs["member_touchpoints"][0]
        touch["consent_status"] = "opted_out"
        touch["channels"] = ["email"]
        inputs["tasks"][0]["earliest_start"] = "2026-11-12T08:00:00+08:00"
        inputs["tasks"][0]["deadline"] = "2026-11-12T18:00:00+08:00"
        result = evaluate_campaign("campaign_schedule", inputs)
        sections = {section["id"]: section for section in result["sections"]}
        assessment = sections["campaign_content"]["data"]["member_touchpoints"][0]["activation_assessment"]

        joined = "；".join(assessment["blockers"])
        self.assertIn("不是用户记录的已同意", joined)
        self.assertIn("不在用户记录的许可渠道", joined)
        self.assertIn("超出会员触达窗口", joined)
        self.assertEqual(assessment["external_contact"], "not_performed_by_evaluator")

    def test_nested_member_recipient_identifiers_are_rejected(self) -> None:
        inputs = _member_activation_inputs()
        inputs["member_touchpoints"][0]["activation"]["offer"]["member_ids"] = ["member-1"]
        with self.assertRaisesRegex(ValueError, "not recipient identifiers"):
            evaluate_campaign("campaign_schedule", inputs)

    def test_member_activation_rejects_boolean_nan_and_infinite_numbers(self) -> None:
        mutations = [
            ("planned_contacts_per_member", True),
            ("planned_contacts_per_member", float("nan")),
            ("planned_contacts_per_member", float("inf")),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                inputs = _member_activation_inputs()
                inputs["member_touchpoints"][0]["activation"][field] = value
                with self.assertRaisesRegex(ValueError, "integer"):
                    evaluate_campaign("campaign_schedule", inputs)
        inputs = _member_activation_inputs()
        inputs["member_touchpoints"][0]["activation"]["offer"]["unit_business_cost"]["amount"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite number"):
            evaluate_campaign("campaign_schedule", inputs)

    def test_campaign_content_rejects_a_broken_known_parent_link(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown object"):
            evaluate_campaign(
                "campaign_schedule",
                {
                    "brief": {
                        "goal": "排期",
                        "currency": "CNY",
                        "timezone": "Asia/Shanghai",
                        "start_date": "2026-11-10",
                        "end_date": "2026-11-12",
                    },
                    "campaigns": [{"id": "C1"}],
                    "series": [{"id": "S1", "campaign_id": "missing-campaign"}],
                    "resources": [],
                    "tasks": [{"id": "T1", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0}],
                },
            )

    def test_schedule_requires_disambiguation_at_timezone_transition(self) -> None:
        for local_time, expected in (
            ("2026-03-08T02:30:00", "nonexistent"),
            ("2026-11-01T01:30:00", "ambiguous"),
        ):
            with self.subTest(local_time=local_time):
                with self.assertRaisesRegex(ValueError, expected):
                    evaluate_campaign(
                        "campaign_schedule",
                        {
                            "brief": {
                                "goal": "排期",
                                "currency": "USD",
                                "timezone": "America/New_York",
                                "start_date": "2026-11-01",
                                "end_date": "2026-11-02",
                            },
                            "resources": [],
                            "tasks": [
                                {
                                    "id": "T1",
                                    "duration_hours": 1,
                                    "earliest_start": local_time,
                                    "deadline": "2026-11-02",
                                    "resource_ids": [],
                                    "estimated_cost": 0,
                                }
                            ],
                        },
                    )

    def test_failed_budget_scenario_can_be_recalculated_without_mutating_input(self) -> None:
        inputs = {
            "brief": {"goal": "完成门店媒体排期", "currency": "CNY", "budget": {"maximum": 1000}},
            "quotes": [{"id": "Q1", "amount": 1400, "currency": "CNY", "status": "quote"}],
            "media_items": [{"id": "M1", "channel": "platform", "placement": "feed", "quote_id": "Q1"}],
        }
        before = copy.deepcopy(inputs)
        first = evaluate_campaign("media_plan", inputs)
        self.assertEqual(inputs, before)
        first_budget = first["sections"][2]["data"]["candidates"][0]["budget_check"]
        self.assertEqual(first_budget["status"], "OVER_LIMIT")

        inputs["quotes"][0]["amount"] = 800
        second = evaluate_campaign("media_plan", inputs)
        second_budget = second["sections"][2]["data"]["candidates"][0]["budget_check"]
        self.assertEqual(second_budget["status"], "WITHIN_LIMIT")
        self.assertEqual([section["id"] for section in first["sections"]], [section["id"] for section in second["sections"]])

    def test_rejects_boolean_nonfinite_numbers_and_dependency_cycles(self) -> None:
        base = {
            "brief": {"goal": "排期", "currency": "CNY", "timezone": "UTC", "start_date": "2026-11-10", "end_date": "2026-11-12"},
            "resources": [],
            "tasks": [{"id": "T1", "duration_hours": 1, "resource_ids": [], "estimated_cost": 0}],
        }
        for invalid in (True, math.nan, math.inf, -math.inf):
            inputs = copy.deepcopy(base)
            inputs["tasks"][0]["duration_hours"] = invalid
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    evaluate_campaign("campaign_schedule", inputs)

        cyclic = copy.deepcopy(base)
        cyclic["tasks"] = [
            {"id": "T1", "duration_hours": 1, "dependencies": ["T2"], "resource_ids": []},
            {"id": "T2", "duration_hours": 1, "dependencies": ["T1"], "resource_ids": []},
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            evaluate_campaign("campaign_schedule", cyclic)

    def test_currency_mismatch_is_not_converted_or_marked_within_budget(self) -> None:
        result = evaluate_campaign(
            "media_plan",
            {
                "brief": {"goal": "评估海外版位", "currency": "CNY", "budget": {"maximum": 10000}},
                "quotes": [{"id": "Q1", "amount": 300, "currency": "USD", "status": "quote"}],
                "media_items": [{"id": "M1", "channel": "publisher", "quote_id": "Q1"}],
            },
        )
        budget = result["sections"][2]["data"]["candidates"][0]["budget_check"]
        self.assertEqual(budget["status"], "FOREIGN_CURRENCY_UNASSESSED")
        self.assertEqual(budget["foreign_currency_costs_not_converted"], ["USD"])

    def test_media_candidates_compare_shared_basis_and_show_cancel_refund_math(self) -> None:
        common = {
            "comparison_group": "LOCAL-POST-1",
            "channel": "platform-a",
            "account_id": "brand-account-1",
            "placement": "local-feed",
            "market": "CN-Guangzhou",
            "audience_scope": "brief-listed nearby visitors",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "deliverable_ids": [],
            "purchase_path": "customer-approved agency quote",
            "pricing_basis": {"model": "flat", "unit": "post"},
            "metric_definition": "platform native valid views",
            "denominator": "platform valid views for this post",
            "observation_window": "2026-11-10/2026-11-20",
            "data_rights": "brand owned reporting only",
            "exclusions": ["invalid traffic excluded under named platform rule"],
            "brand_suitability": "customer review required",
        }
        result = evaluate_campaign(
            "media_plan",
            {
                "brief": {
                    "goal": "比较本地内容版位报价并保留取消情景",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-20",
                    "budget": {"maximum": 15000, "reserve": 500},
                },
                "candidates": [
                    {
                        "id": "PLAN-A",
                        "title": "本地版位甲",
                        "media_items": [
                            {
                                **common,
                                "id": "M-A",
                                "amount": 5000,
                                "currency": "CNY",
                                "status": "quote",
                                "cancellation_scenario": {
                                    "status": "scenario",
                                    "currency": "CNY",
                                    "requested_at": "2026-11-09T10:00:00",
                                    "cutoff_at": "2026-11-09T18:00:00",
                                    "committed_amount": 5000,
                                    "released_commitment": 4000,
                                    "actual_spend": 1000,
                                    "refund_amount": 600,
                                    "fee_amount": 100,
                                    "notice_status": "not_sent",
                                    "source_refs": ["contract:cancel-term-1"],
                                },
                            }
                        ],
                    },
                    {
                        "id": "PLAN-B",
                        "title": "本地版位乙",
                        "media_items": [
                            {
                                **common,
                                "id": "M-B",
                                "amount": 4500,
                                "currency": "CNY",
                                "status": "quote",
                            }
                        ],
                    },
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        comparison = sections["media_measurement"]["data"]["comparisons"][0]
        self.assertEqual(comparison["status"], "COMPARABLE_PRICE_INPUTS")
        self.assertEqual(comparison["second_minus_first_amount"], -500)
        self.assertEqual(comparison["effectiveness_ranking"], "not_established")
        scenario = sections["media_placements"]["data"]["candidates"][0]["media_items"][0]["cancellation_scenario"]
        self.assertEqual(scenario["net_actual_after_refund"], 400)
        self.assertEqual(scenario["remaining_commitment_and_fee"], 1100)
        self.assertEqual(scenario["total_cost_after_cancellation"], 1500)
        self.assertEqual(scenario["policy_window_status"], "within_declared_window")
        self.assertEqual(scenario["external_action"], "not_performed_by_evaluator")
        self.assertIn("5000", sections["media_budget"]["body"])
        self.assertIn("1500", sections["media_budget"]["body"])

    def test_media_comparison_requires_matching_basis_and_models_cancel_cost(self) -> None:
        common = {
            "comparison_group": "LOCAL-POST-1",
            "channel": "platform-a",
            "account_id": "brand-account-1",
            "placement": "local-feed",
            "market": "CN-Guangzhou",
            "audience_scope": "brief-listed nearby visitors",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "deliverable_ids": [],
            "purchase_path": "customer-approved agency quote",
            "pricing_basis": {"model": "flat", "unit": "post"},
            "metric_definition": "platform native valid views",
            "denominator": "platform valid views for this post",
            "observation_window": "2026-11-10/2026-11-20",
            "data_rights": "brand owned reporting only",
            "exclusions": ["invalid traffic excluded under named platform rule"],
            "brand_suitability": "customer review required",
        }
        result = evaluate_campaign(
            "media_plan",
            {
                "brief": {
                    "goal": "比较本地内容版位报价并保留取消情景",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-20",
                    "budget": {"maximum": 15000, "reserve": 500},
                },
                "candidates": [
                    {
                        "id": "PLAN-A",
                        "title": "本地版位甲",
                        "media_items": [
                            {
                                **common,
                                "id": "M-A",
                                "amount": 5000,
                                "currency": "CNY",
                                "status": "quote",
                                "cancellation_scenario": {
                                    "status": "scenario",
                                    "currency": "CNY",
                                    "requested_at": "2026-11-09T10:00:00",
                                    "cutoff_at": "2026-11-09T18:00:00",
                                    "committed_amount": 5000,
                                    "released_commitment": 4000,
                                    "actual_spend": 1000,
                                    "refund_amount": 600,
                                    "fee_amount": 100,
                                    "notice_status": "not_sent",
                                    "source_refs": ["contract:cancel-term-1"],
                                },
                            }
                        ],
                    },
                    {
                        "id": "PLAN-B",
                        "title": "本地版位乙",
                        "media_items": [
                            {
                                **common,
                                "id": "M-B",
                                "amount": 4500,
                                "currency": "CNY",
                                "status": "quote",
                            }
                        ],
                    },
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        comparison = sections["media_measurement"]["data"]["comparisons"][0]
        self.assertEqual(comparison["status"], "COMPARABLE_PRICE_INPUTS")
        self.assertEqual(comparison["second_minus_first_amount"], -500)
        self.assertEqual(comparison["effectiveness_ranking"], "not_established")
        scenario = sections["media_placements"]["data"]["candidates"][0]["media_items"][0]["cancellation_scenario"]
        self.assertEqual(scenario["net_actual_after_refund"], 400)
        self.assertEqual(scenario["remaining_commitment_and_fee"], 1100)
        self.assertEqual(scenario["total_cost_after_cancellation"], 1500)
        self.assertEqual(scenario["policy_window_status"], "within_declared_window")
        self.assertEqual(scenario["external_action"], "not_performed_by_evaluator")
        self.assertIn("5000", sections["media_budget"]["body"])
        self.assertIn("1500", sections["media_budget"]["body"])

    def test_actual_cost_is_not_mistaken_for_a_planned_quote(self) -> None:
        result = evaluate_campaign(
            "campaign_schedule",
            {
                "brief": {
                    "goal": "核对已发生费用与后续计划",
                    "currency": "CNY",
                    "timezone": "Asia/Shanghai",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-12",
                    "budget": {"maximum": 1000},
                },
                "resources": [],
                "tasks": [
                    {
                        "id": "T1",
                        "title": "已完成拍摄",
                        "duration_hours": 1,
                        "resource_ids": [],
                        "estimated_cost": 200,
                        "cost_status": "actual",
                    }
                ],
            },
        )
        sections = {section["id"]: section for section in result["sections"]}
        self.assertEqual(sections["campaign_budget"]["data"]["budget_check"]["status"], "PARTIAL_COST_UNKNOWN")
        self.assertEqual(sections["campaign_schedule"]["data"]["status"], "CONFLICTS_OR_GAPS")


class CampaignRevisionCoreIntegrationTests(unittest.TestCase):
    base_url = "http://127.0.0.1:8317"

    def setUp(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        temporary_parent = repository / ".runtime" / "campaign-revision-tests"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_parent)
        self.root = Path(self.temporary.name)
        self.app = self.new_app()
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()
        capability = self.root / "launch.capability"
        issue_local_capability(self.store, capability)
        session = self.request(
            "post",
            "/api/local-session",
            json={"capability": capability.read_text("ascii")},
        )
        self.assertEqual(session.status_code, 200, session.get_data(as_text=True))
        self.token = session.json["data"]["token"]
        self.work = self.post(
            "/api/works",
            {"title": "Campaign局部修订", "goal": "按排期和资源变化重新核对"},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def new_app(self):
        app = create_app(self.root, expected_port=8317)
        install_collaboration_schema(app.extensions["oevira.store"])
        register_collaboration_routes(app)
        register_collaboration_boundary(app)
        register_business_execution_routes(app)
        app.config.update(TESTING=True)
        return app

    def request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if hasattr(self, "token"):
            headers.setdefault("Authorization", f"Bearer {self.token}")
        headers.setdefault("Idempotency-Key", str(uuid.uuid4()))
        return getattr(self.client, method)(path, base_url=self.base_url, headers=headers, **kwargs)

    def post(self, path: str, payload: dict, *, status=(200, 201)) -> dict:
        response = self.request("post", path, json=payload)
        self.assertIn(response.status_code, status, response.get_data(as_text=True))
        return response.json["data"] if response.status_code in {200, 201} else response.json["error"]

    def test_real_artifact_revision_consumes_prior_schedule_and_reopens(self) -> None:
        inputs, _ = _schedule_revision_fixture()
        created = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": inputs, "citations": []},
            status=(201,),
        )
        before = {
            row["id"]: row
            for section in created["input_snapshot"]["business"]["generated_sections"]
            if section["id"] == "campaign_schedule"
            for row in section["data"]["tasks"]
        }
        self.post(
            f"/api/business-results/{created['id']}/revisions",
            {"base_version": 1, "confirm_sections": ["campaign_schedule"]},
        )
        revised_tasks = copy.deepcopy(inputs["tasks"])
        revised_tasks[0]["resource_ids"] = ["camera-b"]
        revised = self.post(
            f"/api/business-results/{created['id']}/revisions",
            {
                "base_version": 2,
                "input_changes": {"tasks": revised_tasks},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "按用户选择更换摄影组并局部试排",
            },
        )
        business = revised["input_snapshot"]["business"]
        schedule = next(
            section["data"]
            for section in business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        rows = {row["id"]: row for row in schedule["tasks"]}

        self.assertEqual(revised["version"], 3)
        self.assertEqual(business["input_snapshot_revision"], 2)
        self.assertEqual(rows["shoot-a"]["resource_ids"], ["camera-b"])
        self.assertEqual(rows["shoot-b"]["start"], before["shoot-b"]["start"])
        self.assertEqual(rows["shoot-b"]["end"], before["shoot-b"]["end"])
        self.assertEqual(rows["design-d"]["start"], before["design-d"]["start"])
        self.assertEqual(rows["design-d"]["end"], before["design-d"]["end"])
        self.assertIn("shoot-b", schedule["revision_impact"]["affected_task_ids"])
        self.assertIn("shoot-b", schedule["revision_impact"]["preserved_task_ids"])
        self.assertEqual(
            business["inputs"]["brief"]["goal"],
            inputs["brief"]["goal"],
        )

        over_budget = self.post(
            f"/api/business-results/{created['id']}/revisions",
            {
                "base_version": 3,
                "input_changes": {"brief": {"budget": {"maximum": 200, "currency": "CNY"}}},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "缩减预算后保留已确认排期并重新核算",
            },
        )
        over_budget_business = over_budget["input_snapshot"]["business"]
        over_budget_schedule = next(
            section["data"]
            for section in over_budget_business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        over_budget_rows = {row["id"]: row for row in over_budget_schedule["tasks"]}
        self.assertEqual(over_budget["version"], 4)
        self.assertEqual(over_budget_business["input_snapshot_revision"], 3)
        self.assertEqual(over_budget_business["inputs"]["brief"]["goal"], inputs["brief"]["goal"])
        self.assertEqual(over_budget_schedule["status"], "INFEASIBLE_DECLARED_CONSTRAINTS")
        self.assertEqual(
            next(section["data"]["budget_check"]["status"] for section in over_budget_business["generated_sections"] if section["id"] == "campaign_budget"),
            "OVER_LIMIT",
        )
        for identity, prior in rows.items():
            self.assertEqual(over_budget_rows[identity]["start"], prior["start"])
            self.assertEqual(over_budget_rows[identity]["end"], prior["end"])

        self.app = self.new_app()
        self.client = self.app.test_client()
        reopened = self.request("get", f"/api/artifacts/{created['id']}")
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["version"], 4)
        reopened_business = reopened.json["data"]["input_snapshot"]["business"]
        self.assertEqual(reopened_business["inputs"]["tasks"][0]["resource_ids"], ["camera-b"])
        self.assertEqual(
            set(next(section["data"]["revision_impact"] for section in reopened_business["generated_sections"] if section["id"] == "campaign_schedule")["preserved_task_ids"]),
            {"shoot-a", "shoot-b", "edit-c", "design-d"},
        )

    def test_real_strategy_revision_preserves_unmentioned_routes_and_records_scope_delta(self) -> None:
        inputs, _ = _strategy_revision_fixture()
        artifact = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "strategy_proposal", "inputs": inputs, "citations": []},
            status=(201,),
        )
        self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {"base_version": 1, "confirm_sections": ["strategy_alternatives"]},
        )
        changed = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 2,
                "input_changes": {
                    "brief": {"budget": {"maximum": 800}},
                    "candidates": [
                        {
                            "id": "route-sampling",
                            "mechanism": "先体验一小口，再用匿名反馈改下一轮产品说明",
                            "mechanism_path": {"brand_action": "提供小杯试饮并收集匿名偏好"},
                        }
                    ],
                    "candidate_removals": [
                        {"id": "route-followup", "reason": "当前没有获准线上触达渠道，先保留线下路线"}
                    ],
                },
                "accept_sections": ["strategy_alternatives"],
                "revision_note": "按用户本次范围和渠道变化修订策略",
            },
        )
        business = changed["input_snapshot"]["business"]
        sections = {section["id"]: section for section in business["generated_sections"]}
        impact = sections["strategy_alternatives"]["data"]["revision_impact"]

        self.assertEqual(changed["version"], 3)
        self.assertEqual(business["input_snapshot_revision"], 2)
        self.assertEqual(business["inputs"]["brief"]["goal"], inputs["brief"]["goal"])
        self.assertEqual(business["inputs"]["brief"]["budget"]["maximum"], 800)
        self.assertEqual([row["id"] for row in business["inputs"]["candidates"]], ["route-sampling"])
        self.assertEqual(impact["removed_candidate_ids"], ["route-followup"])
        self.assertIn("route-followup", {row["candidate_id"] for row in impact["candidate_changes"]})
        self.assertIn("budget.maximum", {row["path"] for row in impact["brief_changes"]})
        self.assertFalse(business.get("campaign_revision_history"))

        cold = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(cold.status_code, 200, cold.get_data(as_text=True))
        self.assertEqual(cold.json["data"]["version"], 3)
        cold_business = cold.json["data"]["input_snapshot"]["business"]
        self.assertEqual([row["id"] for row in cold_business["inputs"]["candidates"]], ["route-sampling"])
        self.assertEqual(
            next(section["data"]["revision_impact"] for section in cold_business["generated_sections"] if section["id"] == "strategy_alternatives")["removed_candidate_ids"],
            ["route-followup"],
        )
        client_export = self.post(
            f"/api/artifacts/{artifact['id']}/exports",
            {"version": 3, "format": "html", "audience": "client"},
        )
        client_file = self.request("get", client_export["download_url"])
        self.assertEqual(client_file.status_code, 200, client_file.get_data(as_text=True))
        client_body = client_file.get_data(as_text=True)
        client_file.close()
        self.assertEqual(client_export["title"], "品牌策略与提案")
        self.assertIn("门店现场试饮", client_body)
        self.assertNotIn("自愿报名后线上回访", client_body)

    def test_saved_material_controls_release_commitment_and_reject_changed_hash(self) -> None:
        inputs, _ = _schedule_revision_fixture()
        task = inputs["tasks"][0]
        task.update(
            locked=True,
            fixed_start="2026-11-10T09:00:00+00:00",
            fixed_end="2026-11-10T10:00:00+00:00",
            commitment_ref="booking:shoot-a:confirmed-v2",
        )
        artifact = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": inputs, "citations": []},
            status=(201,),
        )
        material = self.post(
            "/api/materials",
            {
                "title": "场地方解除档期确认",
                "filename": "booking-release.txt",
                "text": "场地方确认原档期已解除，确认编号 booking:shoot-a:confirmed-v2。",
                "work_id": self.work["id"],
            },
            status=(201,),
        )
        revised_tasks = copy.deepcopy(inputs["tasks"])
        revised_tasks[0].update(
            locked=False,
            fixed_start=None,
            fixed_end=None,
            resource_ids=["camera-b"],
            commitment_ref=None,
        )
        before = {
            "locked": True,
            "fixed_start": "2026-11-10T09:00:00+00:00",
            "fixed_end": "2026-11-10T10:00:00+00:00",
            "resource_ids": ["camera-a"],
            "resource_options": None,
            "commitment_ref": "booking:shoot-a:confirmed-v2",
        }
        after = {
            "locked": False,
            "fixed_start": None,
            "fixed_end": None,
            "resource_ids": ["camera-b"],
            "resource_options": None,
            "commitment_ref": None,
        }
        payload = {
            "base_version": 1,
            "input_changes": {"tasks": revised_tasks},
            "accept_sections": ["campaign_schedule"],
            "revision_note": "按场地方原始确认解除预约并重新试排",
            "campaign_schedule_controls": {
                "commitment_releases": [
                    {
                        "task_id": "shoot-a",
                        "before": before,
                        "after": after,
                        "reason": "场地方确认原档期已解除",
                        "source_refs": [
                            {
                                "material_id": material["id"],
                                "material_sha256": material["sha256"],
                            }
                        ],
                        "evidence_refs": [],
                    }
                ]
            },
        }
        bad_payload = copy.deepcopy(payload)
        bad_payload["campaign_schedule_controls"]["commitment_releases"][0]["source_refs"][0]["material_sha256"] = "0" * 64
        bad = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            bad_payload,
            status=(409,),
        )
        self.assertEqual(bad["code"], "SOURCE_VERSION_CONFLICT")
        unchanged = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(unchanged.json["data"]["version"], 1)

        released = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            payload,
        )
        business = released["input_snapshot"]["business"]
        schedule = next(
            section["data"] for section in business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        self.assertEqual(released["version"], 2)
        self.assertEqual(schedule["revision_impact"]["commitment_releases"][0]["task_id"], "shoot-a")
        self.assertEqual(
            business["source_qualification"]["checked_material_ids"],
            [material["id"]],
        )
        self.assertIn(
            {"id": material["id"], "sha256": material["sha256"], "format": material["format"], "status": material["status"], "filename": material["filename"]},
            released["input_snapshot"]["materials"],
        )
        self.assertEqual(
            business["impact"]["campaign_schedule_controls"]["commitment_releases"][0]["reason"],
            "场地方确认原档期已解除",
        )

    def test_real_restore_keeps_released_commitment_separate_then_recompute_consumes_it(self) -> None:
        inputs, _ = _schedule_revision_fixture()
        inputs["tasks"][0].update(
            locked=True,
            fixed_start="2026-11-10T09:00:00+00:00",
            fixed_end="2026-11-10T10:00:00+00:00",
            commitment_ref="booking:shoot-a:confirmed-v2",
        )
        artifact = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": inputs, "citations": []},
            status=(201,),
        )
        evidence = self.post(
            "/api/materials",
            {
                "title": "場地方解除檔期記錄",
                "filename": "release-record.txt",
                "text": "場地方提供的檔期解除通知，引用 booking:shoot-a:confirmed-v2。",
                "work_id": self.work["id"],
            },
            status=(201,),
        )
        revised_tasks = copy.deepcopy(inputs["tasks"])
        revised_tasks[0].update(
            locked=False,
            fixed_start=None,
            fixed_end=None,
            resource_ids=["camera-b"],
            commitment_ref=None,
        )
        release = {
            "task_id": "shoot-a",
            "before": {
                "locked": True,
                "fixed_start": "2026-11-10T09:00:00+00:00",
                "fixed_end": "2026-11-10T10:00:00+00:00",
                "resource_ids": ["camera-a"],
                "resource_options": None,
                "commitment_ref": "booking:shoot-a:confirmed-v2",
            },
            "after": {
                "locked": False,
                "fixed_start": None,
                "fixed_end": None,
                "resource_ids": ["camera-b"],
                "resource_options": None,
                "commitment_ref": None,
            },
            "reason": "場地方記錄原檔期已解除",
            "source_refs": [
                {"material_id": evidence["id"], "material_sha256": evidence["sha256"]}
            ],
            "evidence_refs": [],
        }
        released = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 1,
                "input_changes": {"tasks": revised_tasks},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "按已存档期资料记录释放并重新排期",
                "campaign_schedule_controls": {"commitment_releases": [release]},
            },
        )
        ordinary = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 2,
                "input_changes": {"brief": {"budget": {"maximum": 900, "currency": "CNY"}}},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "调整预算后复核排期",
            },
        )
        self.assertEqual((released["version"], ordinary["version"]), (2, 3))

        ordinary_client_export = self.post(
            f"/api/artifacts/{artifact['id']}/exports",
            {"version": 3, "format": "html", "audience": "client"},
        )
        ordinary_client_file = self.request("get", ordinary_client_export["download_url"])
        self.assertEqual(ordinary_client_file.status_code, 200, ordinary_client_file.get_data(as_text=True))
        ordinary_client_body = ordinary_client_file.get_data(as_text=True)
        ordinary_client_file.close()
        self.assertIn("用户记录的档期释放状态", ordinary_client_body)
        self.assertIn("仍待核", ordinary_client_body)
        self.assertNotIn(evidence["id"], ordinary_client_body)

        restored = self.post(
            f"/api/business-results/{artifact['id']}/restore",
            {
                "base_version": 3,
                "restore_version": 1,
                "revision_note": "恢复原方案作参照并保留后续档期释放记录",
            },
        )
        business = restored["input_snapshot"]["business"]
        schedule = next(
            section for section in business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        shoot_a = next(row for row in schedule["data"]["tasks"] if row["id"] == "shoot-a")
        commitment = next(
            row for row in business["campaign_current_commitment_status"]
            if row["task_id"] == "shoot-a"
        )
        self.assertEqual(restored["version"], 4)
        self.assertTrue(business["inputs"]["tasks"][0]["locked"])
        self.assertEqual(shoot_a["status"], "PRESERVED_LOCKED")
        self.assertTrue(schedule["data"]["restored_plan_is_historical_reference"])
        self.assertEqual(commitment["status"], "user_recorded_release_not_independently_verified")
        self.assertFalse(commitment["external_release_verified"])
        self.assertEqual(commitment["source_refs"][0]["material_id"], evidence["id"])
        self.assertTrue(any(row["source_version"] == 2 for row in business["campaign_revision_history"]))
        self.assertTrue(any(row["source_version"] == 3 for row in business["campaign_revision_history"]))
        self.assertIn("恢复版保留原排期", restored["body"])
        self.assertIn("未独立核验外部取消", restored["body"])

        client_export = self.post(
            f"/api/artifacts/{artifact['id']}/exports",
            {"version": 4, "format": "html", "audience": "client"},
        )
        client_file = self.request("get", client_export["download_url"])
        self.assertEqual(client_file.status_code, 200, client_file.get_data(as_text=True))
        client_text = client_file.get_data(as_text=True)
        client_file.close()
        self.assertIn("后续版本曾记录档期释放", client_text)
        self.assertIn("仍待核", client_text)
        self.assertNotIn(evidence["id"], client_text)
        self.assertNotIn(evidence["sha256"], client_text)
        self.assertNotIn("booking:shoot-a:confirmed-v2", client_text)

        rechecked = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 4,
                "input_changes": {"brief": {"budget": {"maximum": 850, "currency": "CNY"}}},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "沿用已记录的释放状态重新核排期",
            },
        )
        rechecked_business = rechecked["input_snapshot"]["business"]
        rechecked_schedule = next(
            section["data"] for section in rechecked_business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        rechecked_task = next(row for row in rechecked_schedule["tasks"] if row["id"] == "shoot-a")
        self.assertEqual(rechecked["version"], 5)
        self.assertFalse(rechecked_business["inputs"]["tasks"][0]["locked"])
        self.assertIsNone(rechecked_business["inputs"]["tasks"][0]["commitment_ref"])
        self.assertNotEqual(rechecked_task["status"], "PRESERVED_LOCKED")
        self.assertIn(
            "shoot-a",
            rechecked_schedule["revision_impact"]["direct_replan_task_ids"],
        )

        self.app = self.new_app()
        self.client = self.app.test_client()
        cold = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(cold.status_code, 200, cold.get_data(as_text=True))
        cold_business = cold.json["data"]["input_snapshot"]["business"]
        self.assertEqual(cold.json["data"]["version"], 5)
        self.assertTrue(any(row["source_version"] == 2 for row in cold_business["campaign_revision_history"]))
        self.assertTrue(any(row["source_version"] == 3 for row in cold_business["campaign_revision_history"]))
        cold_release = next(
            row for row in cold_business["campaign_current_commitment_status"]
            if row["task_id"] == "shoot-a"
        )
        self.assertEqual(cold_release["status"], "user_recorded_release_not_independently_verified")
        self.assertEqual(cold_release["source_refs"][0]["material_id"], evidence["id"])

        recommitted_tasks = copy.deepcopy(rechecked_business["inputs"]["tasks"])
        recommitted_task = next(row for row in recommitted_tasks if row["id"] == "shoot-a")
        recommitted_task.update(
            locked=True,
            fixed_start=rechecked_task["start"],
            fixed_end=rechecked_task["end"],
            commitment_ref="booking:shoot-a:reconfirmed-v3",
        )
        recommitted = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 5,
                "input_changes": {"tasks": recommitted_tasks},
                "accept_sections": ["campaign_schedule"],
                "revision_note": "按本次用户输入记录新的档期状态",
            },
        )
        recommitted_business = recommitted["input_snapshot"]["business"]
        superseded_release = next(
            row for row in recommitted_business["campaign_current_commitment_status"]
            if row["task_id"] == "shoot-a"
        )
        self.assertEqual(recommitted["version"], 6)
        self.assertEqual(superseded_release["status"], "superseded_by_later_task_state")
        self.assertIn("历史释放记录已由后续任务输入替代", recommitted["body"])

    def test_real_campaign_client_export_keeps_only_explicit_member_offer_details(self) -> None:
        inputs = _member_activation_inputs()
        offer = inputs["member_touchpoints"][0]["activation"]["offer"]
        offer.update(
            client_visible=True,
            client_summary="持券到店可参加一次新品试饮，按现场规则核销。",
            client_quote={
                "label": "会员活动方案对客报价",
                "amount": 280,
                "currency": "CNY",
                "status": "quote",
                "valid_until": "2026-11-20",
            },
        )
        artifact = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": inputs, "citations": []},
            status=(201,),
        )
        exported = self.post(
            f"/api/artifacts/{artifact['id']}/exports",
            {"version": 1, "format": "html", "audience": "client"},
        )
        file_response = self.request("get", exported["download_url"])
        self.assertEqual(file_response.status_code, 200, file_response.get_data(as_text=True))
        body = file_response.get_data(as_text=True)
        file_response.close()
        for public_detail in (
            "新品试饮券",
            "权益面额：10 CNY",
            "已确认的广州活动报名会员，每人限领一次",
            "会员活动方案对客报价",
            "280 CNY",
        ):
            self.assertIn(public_detail, body)
        for internal_detail in (
            "2 CNY",
            "offer-ledger:offer-1",
            "consent:segment-1",
            "touch-history:segment-1",
            "offer-cost:quote-1",
            "100 人",
        ):
            self.assertNotIn(internal_detail, body)
        self.assertIn("不代表实际发送、发券或履约", body)

    def test_real_campaign_post_variant_requires_the_exact_saved_artifact_post(self) -> None:
        base_inputs = {
            "brief": {
                "goal": "从已保存原稿制作平台内容变体",
                "currency": "CNY",
                "timezone": "Asia/Shanghai",
                "start_date": "2026-11-10",
                "end_date": "2026-11-15",
            },
            "posts": [
                {
                    "id": "post-original",
                    "title": "门店试饮原稿",
                    "platform": "platform-a",
                    "body": "原稿正文，包含试饮活动规则。",
                    "planned_publish_at": "2026-11-12T12:00:00+08:00",
                }
            ],
        }
        source = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": base_inputs, "citations": []},
            status=(201,),
        )
        source_post_id = source["input_snapshot"]["business"]["generated_sections"]
        source_content = next(row["data"] for row in source_post_id if row["id"] == "campaign_content")
        self.assertEqual(source_content["posts"][0]["id"], "post-original")

        referenced_id = f"source:{source['id']}:v1:post-original"
        variant_inputs = {
            **base_inputs,
            "posts": [
                {
                    "id": referenced_id,
                    "title": "门店试饮原稿",
                    "body": "原稿正文，包含试饮活动规则。",
                    "source_display": "门店试饮原稿 · v1",
                    "source_artifact_reference": {
                        "artifact_id": source["id"],
                        "artifact_version": 1,
                        "post_id": "post-forged",
                    },
                }
            ],
        }
        rejected = self.request(
            "post",
            f"/api/works/{self.work['id']}/business-results",
            json={"operation": "campaign_schedule", "inputs": variant_inputs, "citations": []},
        )
        self.assertEqual(rejected.status_code, 409, rejected.get_data(as_text=True))
        self.assertEqual(rejected.json["error"]["code"], "SOURCE_POST_NOT_FOUND")

        variant_inputs["posts"][0]["source_artifact_reference"]["post_id"] = "post-original"
        variant_inputs["posts"].append(
            {
                "id": "post-platform-short",
                "title": "平台短版",
                "platform": "platform-a",
                "body": "适配后的平台短版正文。",
                "variant_of": referenced_id,
                "variant_source_label": "门店试饮原稿 · v1",
            }
        )
        variant = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": variant_inputs, "citations": []},
            status=(201,),
        )
        business = variant["input_snapshot"]["business"]
        content = next(
            row["data"] for row in business["generated_sections"]
            if row["id"] == "campaign_content"
        )
        self.assertEqual(content["posts"][0]["source_artifact_reference"]["artifact_id"], source["id"])
        self.assertEqual(content["posts"][0]["source_artifact_reference"]["artifact_version"], 1)
        self.assertEqual(content["posts"][0]["source_artifact_reference"]["post_id"], "post-original")
        self.assertEqual(content["posts"][1]["variant_link_status"], "linked_in_snapshot")
        self.assertTrue(
            any(
                row.get("artifact_id") == source["id"]
                and row.get("posts", {}).get("post-original", {}).get("id") == "post-original"
                for row in business["source_qualification"]["checked_artifact_versions"]
            )
        )

        client_export = self.post(
            f"/api/artifacts/{variant['id']}/exports",
            {"version": 1, "format": "html", "audience": "client"},
        )
        client_file = self.request("get", client_export["download_url"])
        self.assertEqual(client_file.status_code, 200, client_file.get_data(as_text=True))
        client_body = client_file.get_data(as_text=True)
        client_file.close()
        self.assertIn("门店试饮原稿", client_body)
        self.assertIn("平台短版", client_body)
        self.assertIn("适配后的平台短版正文", client_body)
        self.assertNotIn(source["id"], client_body)
        self.assertNotIn("source:", client_body)

    def test_real_restore_keeps_latest_corrected_actual_unknown_and_renders_it(self) -> None:
        inputs, _ = _schedule_revision_fixture()
        inputs["tasks"][0].update(
            actual_start="2026-11-10T09:05:00+00:00",
            actual_end="2026-11-10T10:05:00+00:00",
            execution_status="user_recorded_complete",
        )
        artifact = self.post(
            f"/api/works/{self.work['id']}/business-results",
            {"operation": "campaign_schedule", "inputs": inputs, "citations": []},
            status=(201,),
        )
        evidence = self.post(
            "/api/materials",
            {
                "title": "现场记录更正依据",
                "filename": "time-correction.txt",
                "text": "现场原始记录时间字段缺失，无法确认拍摄实际开始和结束时刻。",
                "work_id": self.work["id"],
            },
            status=(201,),
        )
        corrected_tasks = copy.deepcopy(inputs["tasks"])
        corrected_tasks[0].update(
            actual_start=None,
            actual_end="UNKNOWN",
            execution_status="UNKNOWN",
        )
        corrected = self.post(
            f"/api/business-results/{artifact['id']}/revisions",
            {
                "base_version": 1,
                "input_changes": {"tasks": corrected_tasks},
                "revision_note": "按现场原始记录更正不可靠的实际时间",
                "campaign_schedule_controls": {
                    "execution_corrections": [
                        {
                            "task_id": "shoot-a",
                            "fields": ["actual_start", "actual_end", "execution_status"],
                            "before": {
                                "actual_start": "2026-11-10T09:05:00+00:00",
                                "actual_end": "2026-11-10T10:05:00+00:00",
                                "execution_status": "user_recorded_complete",
                            },
                            "after": {
                                "actual_start": None,
                                "actual_end": "UNKNOWN",
                                "execution_status": "UNKNOWN",
                            },
                            "reason": "原始现场单缺少可靠时间戳，撤回此前用户记录",
                            "source_refs": [
                                {
                                    "material_id": evidence["id"],
                                    "material_sha256": evidence["sha256"],
                                }
                            ],
                            "evidence_refs": [],
                        }
                    ]
                },
            },
        )
        self.assertEqual(corrected["version"], 2)

        restored = self.post(
            f"/api/business-results/{artifact['id']}/restore",
            {"base_version": 2, "restore_version": 1, "revision_note": "恢复原排期并保留当前执行更正"},
        )
        business = restored["input_snapshot"]["business"]
        task = business["inputs"]["tasks"][0]
        schedule_section = next(
            section for section in business["generated_sections"]
            if section["id"] == "campaign_schedule"
        )
        schedule_task = next(row for row in schedule_section["data"]["tasks"] if row["id"] == "shoot-a")
        history = next(row for row in business["campaign_task_history"] if row["task_id"] == "shoot-a")

        self.assertEqual(restored["version"], 3)
        self.assertIsNone(task["actual_start"])
        self.assertEqual(task["actual_end"], "UNKNOWN")
        self.assertEqual(schedule_task["execution"]["actual_end"], "UNKNOWN")
        self.assertIsNone(history["execution_fields"]["actual_start"])
        self.assertEqual(history["execution_fields"]["actual_end"], "UNKNOWN")
        self.assertEqual(history["source_version"], 2)
        correction_history = [
            correction
            for row in business["campaign_revision_history"]
            for correction in row.get("campaign_schedule_controls", {}).get("execution_corrections", [])
        ]
        self.assertTrue(
            any(
                correction["source_refs"][0]["material_id"] == evidence["id"]
                and correction["source_refs"][0]["material_sha256"] == evidence["sha256"]
                for correction in correction_history
            )
        )

        cold = self.request("get", f"/api/artifacts/{artifact['id']}")
        self.assertEqual(cold.status_code, 200, cold.get_data(as_text=True))
        self.assertEqual(cold.json["data"]["version"], 3)
        self.assertIn("当前 Campaign 任务实际记录", cold.json["data"]["body"])
        self.assertIn("实际开始时间 2026-11-10T09:05:00+00:00 → 未知", cold.json["data"]["body"])
        self.assertIn("原始现场单缺少可靠时间戳", cold.json["data"]["body"])


if __name__ == "__main__":
    unittest.main()
