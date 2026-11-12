"""Deterministic, source-preserving planning for business campaigns.

This module is intentionally pure: it evaluates a frozen business input and
returns editable sections. It does not persist a result, select an approver,
contact a creator, book a resource, place media, publish content, or assert that
an outcome will occur. The shared business execution owner handles references,
permissions, versions, revisions, exports, and actual actions.

Input contract
--------------
Every operation accepts the same optional ``brief`` fields: ``goal``,
``audience``, ``market``, ``currency``, ``timezone``, ``start_date``,
``end_date``, ``facts`` and ``budget``. ``facts`` is a list of user supplied
claims; each row can include verbatim ``source_refs`` and ``evidence_refs``.
References are retained but never treated as independently verified here.

``strategy_proposal`` requires ``candidates`` with ``id``, ``title`` and a
concrete ``mechanism``. Each option can state its audience need, barrier,
brand reason, actions, deliverables, resources, dependencies, cost lines,
evidence, counterevidence and validation method. Options are structured and
compared on their stated causal work; there is no score or automatic winner.
On revision, stable candidate IDs preserve omitted routes; remove a prior route
only with an explicit ``candidate_removals`` entry and reason. Partial Brief and
candidate-dictionary changes retain unmentioned fields and return a field-level
scope/route impact in the existing strategy sections.

``media_plan`` accepts ``media_items`` with a channel, account/placement,
dates, deliverable IDs, pricing/quote, metric definition, denominator/window,
data rights, exclusions and brand-suitability conditions. A cancellation
scenario's ``committed_amount`` means unperformed commitment before release,
excluding separately reported actual spend. ``candidates`` may contain
alternative plans, each with its own ``media_items`` list.

``collaboration_plan`` accepts ``collaborators`` (or ``collaborations``), each
bound to a ``project_id`` and stable ``creator_id`` with separate
``accounts`` (platform plus platform account ID). Handles do not join accounts.
Each entry may include role, observations, quote, deliverables, acceptance and
rights. Podcast work can add a declared cooperation mode and its specific
terms, plus separate observations for influence, content, audience, network,
and monetization. Those dimensions are not blended into a score. A missing
contract term remains unknown; it is never assumed to be a buyout or
publication permission.

``campaign_schedule`` accepts ``resources`` and ``tasks``. A task has an ID,
positive ``duration_hours``, optional dependencies and resource assignment
options, an earliest start and a deadline (or uses the brief window). Resource
capacity and declared availability are checked. Date-only end dates include
that local calendar day. Fixed/locked tasks require both ``fixed_start`` and
``fixed_end`` and are preserved even when a conflict is reported.

It may also include audience-segment ``member_touchpoints``. An optional
``activation`` links each planned touchpoint to an existing schedule task and
records channel/purpose, contact window, per-member frequency rule and history,
offer terms, validity, inventory, redemption cap, eligible-audience denominator
and redemption/spend/refund history. Repeated ``offer.id`` values share one pool
and one budget line; conflicting terms for that ID are invalid. Per-member caps
use only exact unique-member counts or declared conservative upper bounds.
Unknown history, inventory, cost or population size remains unknown. The
evaluator never contacts members or treats consent as platform-send authority.
Customer-facing output includes an offer only when ``client_visible`` is true;
it uses the explicit client summary and client quote, never the internal unit
cost, member history, stock ledger or source references.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_OPERATIONS = frozenset(
    {
        "strategy_proposal",
        "media_plan",
        "collaboration_plan",
        "campaign_schedule",
    }
)

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ZONE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")
_QUOTE_STATES = frozenset({"quote", "estimate", "committed", "actual", "refund"})
_MECHANISM_FIELDS = (
    "audience_trigger",
    "brand_action",
    "value_exchange",
    "reinforcement",
)
_PODCAST_MODES = {
    "sponsorship_insert": {
        "label": "冠名贴片",
        "terms": ("insertion_position", "episodes", "read_length_seconds"),
    },
    "custom_program": {
        "label": "节目定制",
        "terms": ("episode_format", "episode_length_minutes", "editorial_scope"),
    },
    "podcast_co_marketing": {
        "label": "播客联合营销",
        "terms": ("partner_channels", "cross_promotion_assets", "responsibility_split"),
    },
    "brand_owned_podcast": {
        "label": "品牌自有播客",
        "terms": ("production_owner", "hosting_account", "distribution_scope"),
    },
    "audio_plus_integrated": {
        "label": "声音+整合营销",
        "terms": ("audio_deliverable", "cross_channel_deliverables", "tracking_definition"),
    },
    "offline_event_ip": {
        "label": "线下活动/IP联名",
        "terms": ("event_or_ip", "location", "recording_rights"),
    },
}
_PODCAST_POWER_LABELS = {
    "influence": "影响力",
    "content": "内容力",
    "audience": "人群力",
    "network": "矩阵力",
    "monetization": "变现力",
}

_REVISION_TASK_FIELDS = (
    "duration_hours",
    "earliest_start",
    "deadline",
    "resource_ids",
    "resource_options",
    "required_qualifications",
    "dependencies",
    "locked",
    "fixed_start",
    "fixed_end",
    "commitment_ref",
    "mandatory",
    "coverage",
    "exclusive_group",
    "benefit",
    "estimated_cost",
    "committed_cost",
    "actual_cost",
    "refund",
    "quote_id",
    "costs",
    "cost_status",
    "currency",
    "actual_start",
    "actual_end",
    "actual_publish_at",
    "execution_status",
    "receipt_status",
    "external_receipt_refs",
    "metric_snapshots",
)
_REVISION_TASK_REPLAN_FIELDS = frozenset(
    {
        "duration_hours",
        "earliest_start",
        "deadline",
        "resource_ids",
        "resource_options",
        "required_qualifications",
        "dependencies",
        "locked",
        "fixed_start",
        "fixed_end",
        "commitment_ref",
        "mandatory",
        "coverage",
        "exclusive_group",
        "benefit",
    }
)


def _revision_task_signature(task: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "resource_ids": [],
        "dependencies": [],
        "required_qualifications": [],
        "coverage": [],
        "mandatory": True,
        "locked": False,
    }
    return {
        field: deepcopy(task.get(field, defaults.get(field)))
        for field in _REVISION_TASK_FIELDS
    }


def _revision_task_resources(task: dict[str, Any]) -> set[str]:
    result = set()
    resource_ids = task.get("resource_ids", [])
    if isinstance(resource_ids, list):
        result.update(value for value in resource_ids if isinstance(value, str))
    options = task.get("resource_options", [])
    if isinstance(options, list):
        for option in options:
            if isinstance(option, list):
                result.update(value for value in option if isinstance(value, str))
    return result


def _revision_task_window(
    task: dict[str, Any], brief: dict[str, Any], tz: Any
) -> tuple[datetime, datetime] | None:
    start = task.get("earliest_start", brief.get("start_date"))
    end = task.get("deadline", brief.get("end_date"))
    if start is None or end is None:
        return None
    try:
        left = _parse_time(start, "revision.task.earliest_start", tz)
        right = _parse_time(end, "revision.task.deadline", tz, end_of_date=True)
    except (TypeError, ValueError):
        return None
    return (left, right) if left < right else None


def _strategy_delta_fields(
    before: Any, after: Any, path: str = ""
) -> list[dict[str, Any]]:
    if _deep_equal(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}" if path else key
            old_value, new_value = before.get(key), after.get(key)
            if key in {"source_refs", "evidence_refs"}:
                if not _deep_equal(old_value, new_value):
                    result.append(
                        {
                            "path": child_path,
                            "before": f"{len(old_value) if isinstance(old_value, list) else 0}条引用",
                            "after": f"{len(new_value) if isinstance(new_value, list) else 0}条引用",
                        }
                    )
            elif key in {"material_id", "material_sha256", "material_version", "artifact_id", "artifact_version", "segment_id", "post_id"}:
                if not _deep_equal(old_value, new_value):
                    result.append({"path": child_path, "before": "已保存引用", "after": "引用已更改" if new_value is not None else "已移除"})
            else:
                result.extend(_strategy_delta_fields(old_value, new_value, child_path))
        return result
    leaf = path.rsplit(".", 1)[-1].casefold()
    if leaf in {"id", "material_id", "artifact_id", "segment_id", "post_id", "source_ref"}:
        return [{"path": path or "引用", "before": "已提供引用" if before is not None else "未提供", "after": "已提供引用" if after is not None else "已移除"}]
    if isinstance(before, list) or isinstance(after, list):
        old_count = len(before) if isinstance(before, list) else 0
        new_count = len(after) if isinstance(after, list) else 0
        return [{"path": path or "内容", "before": f"{old_count}项", "after": f"{new_count}项"}]

    def safe_value(value: Any) -> Any:
        if isinstance(value, dict):
            return "结构化信息已提供" if value else "未提供"
        return deepcopy(value)

    return [{"path": path or "内容", "before": safe_value(before), "after": safe_value(after)}]


def _strategy_impact_areas(changed_fields: list[dict[str, Any]]) -> list[str]:
    paths = [row.get("path", "") for row in changed_fields if isinstance(row, dict)]
    areas = set()
    for path in paths:
        key = path.split(".", 1)[0]
        if key in {"audience", "audiences", "audience_need", "barrier", "brief"}:
            areas.add("受众、需求与传播目标")
        if key in {"mechanism", "mechanism_path", "brand_reason", "actions"}:
            areas.add("品牌机制与执行动作")
        if key in {"resource_ids", "resource_options", "dependencies", "duration_days", "deliverable_ids"}:
            areas.add("资源、交付与排期依赖")
        if key in {"cost_lines", "cost_status", "estimated_cost", "quote_id", "currency", "budget", "quotes"}:
            areas.add("预算、报价与成本条件")
        if key in {"case_basis", "transfer_conditions", "asset_uses"}:
            areas.add("案例迁移与素材权利")
        if key in {"validation", "expected_effect", "counterevidence", "risks"}:
            areas.add("验证、假设与风险")
        if key in {"goal", "market", "language", "start_date", "end_date", "timezone", "constraints", "forbidden_claims", "facts"}:
            areas.add("Brief范围与适用边界")
    return sorted(areas)


def _prepare_strategy_revision_inputs(
    prepared: dict[str, Any], previous_business: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    old_inputs = previous_business.get("inputs", {})
    old_saved_brief = old_inputs.get("brief") if isinstance(old_inputs, dict) else None
    new_saved_brief = prepared.get("brief")
    if isinstance(old_saved_brief, dict) and isinstance(new_saved_brief, dict):
        prepared["brief"] = {**deepcopy(old_saved_brief), **deepcopy(new_saved_brief)}
    old_rows = old_inputs.get("candidates", []) if isinstance(old_inputs, dict) else []
    if not isinstance(old_rows, list):
        raise ValueError("saved strategy candidates are not an array")
    old_candidates = _rows(old_rows, "saved_business.inputs.candidates") if old_rows else []
    new_raw = prepared.get("candidates", old_rows)
    if not isinstance(new_raw, list):
        raise ValueError("candidates must be an array")
    new_candidates = _rows(new_raw, "candidates")
    removals_raw = prepared.pop("candidate_removals", [])
    if not isinstance(removals_raw, list):
        raise ValueError("candidate_removals must be an array")
    old_map = {row["id"]: row for row in old_candidates}
    new_map = {row["id"]: row for row in new_candidates}
    removed: dict[str, str] = {}
    for index, removal in enumerate(removals_raw):
        path = f"candidate_removals[{index}]"
        if not isinstance(removal, dict) or set(removal) != {"id", "reason"}:
            raise ValueError(f"{path} needs a candidate id and a removal reason")
        identity = _required_text(removal.get("id"), f"{path}.id")
        reason = _required_text(removal.get("reason"), f"{path}.reason")
        if identity not in old_map or identity in removed:
            raise ValueError(f"{path}.id must uniquely name a saved strategy route")
        if identity in new_map and not _deep_equal(new_map[identity], old_map[identity]):
            raise ValueError(f"{path}.id cannot also appear in the revised candidates")
        removed[identity] = reason

    merged: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    preserved: list[str] = []
    for old in old_candidates:
        identity = old["id"]
        if identity in removed:
            deltas.append(
                {
                    "candidate_id": identity,
                    "title": old.get("title"),
                    "change": "removed_by_explicit_revision",
                    "reason": removed[identity],
                    "changed_fields": [],
                    "impact_areas": ["本方案路线及其预算/交付引用"],
                }
            )
            continue
        revised = new_map.get(identity)
        if revised is None:
            merged.append(deepcopy(old))
            preserved.append(identity)
            continue

        def merge_candidate(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(base)
            for key, value in patch.items():
                if key == "id":
                    continue
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key] = merge_candidate(result[key], value)
                else:
                    result[key] = deepcopy(value)
            return result

        updated = merge_candidate(old, revised)
        fields = _strategy_delta_fields(old, updated)
        merged.append(updated)
        if fields:
            deltas.append(
                {
                    "candidate_id": identity,
                    "title": updated.get("title") or old.get("title"),
                    "change": "updated",
                    "changed_fields": fields,
                    "impact_areas": _strategy_impact_areas(fields),
                }
            )
        else:
            preserved.append(identity)
    added = [row for row in new_candidates if row["id"] not in old_map]
    for row in added:
        merged.append(deepcopy(row))
        fields = [
            item for item in _strategy_delta_fields({}, row)
            if item.get("path") != "id" and not item.get("path", "").endswith(".id")
        ]
        deltas.append(
            {
                "candidate_id": row["id"],
                "title": row.get("title"),
                "change": "added",
                "changed_fields": fields,
                "impact_areas": _strategy_impact_areas(fields),
            }
        )
    prepared["candidates"] = merged if old_candidates or "candidates" in prepared else new_candidates

    old_brief = old_inputs.get("brief", {}) if isinstance(old_inputs, dict) and isinstance(old_inputs.get("brief", {}), dict) else {}
    new_brief = prepared.get("brief", {}) if isinstance(prepared.get("brief", {}), dict) else {}
    brief_changes = _strategy_delta_fields(old_brief, new_brief)
    section_ids = set()
    if brief_changes:
        section_ids.update({"strategy_brief", "strategy_alternatives"})
        if any(row["path"].split(".", 1)[0] in {"budget", "currency", "start_date", "end_date", "timezone"} for row in brief_changes):
            section_ids.add("strategy_scope_budget")
        if any(row["path"].split(".", 1)[0] in {"facts", "constraints", "forbidden_claims"} for row in brief_changes):
            section_ids.add("strategy_validation")
    if deltas:
        section_ids.update({"strategy_alternatives", "strategy_validation"})
        if any("预算" in area or "资源" in area or "交付" in area for row in deltas for area in row["impact_areas"]):
            section_ids.add("strategy_scope_budget")

    context = {
        "kind": "strategy_proposal_revision",
        "changed_fields": sorted(
            key for key in set(old_inputs) | set(prepared)
            if not _deep_equal(old_inputs.get(key), prepared.get(key))
        ),
        "brief_changes": brief_changes,
        "candidate_changes": deltas,
        "preserved_candidate_ids": preserved,
        "removed_candidate_ids": sorted(removed),
        "added_candidate_ids": [row["id"] for row in added],
        "affected_sections": sorted(section_ids),
    }
    return prepared, context


def prepare_campaign_revision_inputs(
    inputs: dict[str, Any],
    previous_business: dict[str, Any],
    *,
    controls: dict[str, Any] | None = None,
    revision_note: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Attach a trusted prior schedule context for an in-place Artifact revision.

    Returns ordinary candidate inputs separately from a private, non-persisted
    revision context. Callers may save the prepared inputs but must pass the
    context only through the evaluator's internal keyword argument.
    Confirmed schedules protect unchanged tasks outside the local impact
    closure. Explicit task/resource/date changes open their dependency and
    resource-competition closure. Commitment release and execution correction
    controls are accepted only as exact before/after records supplied through
    the versioned revision request; committed or executed task histories are
    never accepted from arbitrary task input fields.
    """
    prepared = deepcopy(inputs)
    if not isinstance(prepared, dict) or not isinstance(previous_business, dict):
        if controls:
            raise ValueError("Campaign schedule controls require a saved business schedule")
        return prepared, None
    if previous_business.get("operation") == "strategy_proposal":
        if controls:
            raise ValueError("Campaign schedule controls do not apply to a strategy proposal")
        return _prepare_strategy_revision_inputs(prepared, previous_business)
    if previous_business.get("operation") != "campaign_schedule":
        if controls:
            raise ValueError("Campaign schedule controls only apply to a saved campaign_schedule")
        return prepared, None
    old_inputs = deepcopy(previous_business.get("inputs"))
    if not isinstance(old_inputs, dict):
        if controls:
            raise ValueError("Campaign schedule controls require the saved input snapshot")
        return prepared, None
    old_saved_brief = old_inputs.get("brief")
    new_saved_brief = prepared.get("brief")
    if isinstance(old_saved_brief, dict) and isinstance(new_saved_brief, dict):
        prepared["brief"] = {**deepcopy(old_saved_brief), **deepcopy(new_saved_brief)}
    sections = previous_business.get("generated_sections")
    if not isinstance(sections, list):
        sections = (previous_business.get("result") or {}).get("sections")
    if not isinstance(sections, list):
        if controls:
            raise ValueError("Campaign schedule controls require the saved schedule result")
        return prepared, None
    matches = [
        section
        for section in sections
        if isinstance(section, dict) and section.get("id") == "campaign_schedule"
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("data"), dict):
        if controls:
            raise ValueError("Campaign schedule controls require one saved campaign_schedule section")
        return prepared, None
    prior_rows = matches[0]["data"].get("tasks")
    if not isinstance(prior_rows, list):
        if controls:
            raise ValueError("Campaign schedule controls require saved task rows")
        return prepared, None

    old_tasks = old_inputs.get("tasks", [])
    new_tasks = prepared.get("tasks", [])
    old_task_map = {
        row["id"]: row
        for row in old_tasks
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(old_tasks, list) else {}
    new_task_map = {
        row["id"]: row
        for row in new_tasks
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(new_tasks, list) else {}
    release_fields = {"locked", "fixed_start", "fixed_end", "resource_ids", "resource_options", "commitment_ref"}
    inherited_release_by_task: dict[str, dict[str, Any]] = {}
    release_statuses = _campaign_current_commitment_status_rows(previous_business)
    raw_old_task_map = {
        identity: deepcopy(row) for identity, row in old_task_map.items()
    }
    for status in release_statuses:
        if status.get("status") != "user_recorded_release_not_independently_verified":
            continue
        task_id = status.get("task_id")
        before, after = status.get("before"), status.get("after")
        old_task, new_task = old_task_map.get(task_id), new_task_map.get(task_id)
        if (
            not isinstance(task_id, str)
            or not isinstance(before, dict)
            or not isinstance(after, dict)
            or set(before) != release_fields
            or set(after) != release_fields
            or not isinstance(old_task, dict)
        ):
            continue
        current_fields = {
            field: deepcopy(old_task.get(field, [] if field == "resource_ids" else None))
            for field in release_fields
        }
        if _deep_equal(current_fields, after):
            # This release has already been consumed by a newer saved plan.
            continue
        if not _deep_equal(current_fields, before):
            continue
        old_raw_fields = {
            field: deepcopy(raw_old_task_map[task_id].get(field, [] if field == "resource_ids" else None))
            for field in release_fields
        }
        candidate_fields = (
            {
                field: deepcopy(new_task.get(field, [] if field == "resource_ids" else None))
                for field in release_fields
            }
            if isinstance(new_task, dict)
            else None
        )
        # Keep the restored plan's original lock as a historical planning
        # choice, but consume the later recorded release when re-evaluating.
        for field, value in after.items():
            old_task[field] = deepcopy(value)
        if candidate_fields is not None and (
            _deep_equal(candidate_fields, old_raw_fields)
            or _deep_equal(candidate_fields, before)
            or _deep_equal(candidate_fields, after)
        ):
            for field, value in after.items():
                new_task[field] = deepcopy(value)
            inherited_release_by_task[task_id] = deepcopy(status)
    prior_task_map = {
        row["id"]: row
        for row in prior_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if not prior_task_map:
        if controls:
            raise ValueError("Campaign schedule controls require a saved prior schedule")
        return prepared, None

    if controls is None:
        controls = {}
    if not isinstance(controls, dict) or set(controls) - {
        "replan_tasks",
        "commitment_releases",
        "execution_corrections",
    }:
        raise ValueError("Campaign schedule controls contain unsupported fields")
    replan_controls = controls.get("replan_tasks", [])
    release_controls = controls.get("commitment_releases", [])
    correction_controls = controls.get("execution_corrections", [])
    if any(not isinstance(value, list) for value in (replan_controls, release_controls, correction_controls)):
        raise ValueError("Campaign schedule control lists must be arrays")

    def control_reason_and_refs(control: dict[str, Any], path: str) -> tuple[str, list, list]:
        reason = _required_text(control.get("reason"), f"{path}.reason")
        source_refs = control.get("source_refs", [])
        evidence_refs = control.get("evidence_refs", [])
        if not isinstance(source_refs, list) or not isinstance(evidence_refs, list):
            raise ValueError(f"{path} source_refs and evidence_refs must be arrays")
        refs = source_refs + evidence_refs
        if not refs or any(
            not isinstance(reference, dict)
            or not (
                isinstance(reference.get("material_id"), str)
                and isinstance(reference.get("material_sha256", reference.get("material_version")), str)
                or isinstance(reference.get("artifact_id"), str)
                and type(reference.get("artifact_version", reference.get("version"))) is int
                and reference.get("artifact_version", reference.get("version")) > 0
            )
            for reference in refs
        ):
            raise ValueError(f"{path} needs exact saved Material or Artifact evidence references")
        if not isinstance(revision_note, str) or not revision_note.strip():
            raise ValueError(f"{path} requires the revision's human reason")
        return reason, deepcopy(source_refs), deepcopy(evidence_refs)

    replan_task_ids: set[str] = set()
    replan_reasons: dict[str, str] = {}
    for index, control in enumerate(replan_controls):
        path = f"campaign_schedule_controls.replan_tasks[{index}]"
        if not isinstance(control, dict) or set(control) != {"task_id", "reason"}:
            raise ValueError(f"{path} needs task_id and reason")
        task_id = _required_text(control.get("task_id"), f"{path}.task_id")
        if task_id not in prior_task_map or task_id not in new_task_map or task_id in replan_task_ids:
            raise ValueError(f"{path}.task_id must uniquely name a task in the saved and revised schedule")
        replan_task_ids.add(task_id)
        replan_reasons[task_id] = _required_text(control.get("reason"), f"{path}.reason")

    release_by_task: dict[str, dict[str, Any]] = {
        task_id: deepcopy(status)
        for task_id, status in inherited_release_by_task.items()
    }
    for index, control in enumerate(release_controls):
        path = f"campaign_schedule_controls.commitment_releases[{index}]"
        expected_keys = {"task_id", "before", "after", "reason", "source_refs", "evidence_refs"}
        if not isinstance(control, dict) or set(control) != expected_keys:
            raise ValueError(f"{path} needs exact before/after, reason and source evidence")
        task_id = _required_text(control.get("task_id"), f"{path}.task_id")
        if task_id in release_by_task or task_id not in old_task_map or task_id not in new_task_map:
            raise ValueError(f"{path}.task_id must uniquely name a saved and revised task")
        before = _mapping(control.get("before"), f"{path}.before")
        after = _mapping(control.get("after"), f"{path}.after")
        if set(before) != release_fields or set(after) != release_fields:
            raise ValueError(f"{path} must carry the complete locked schedule before and after")
        old_task = old_task_map[task_id]
        new_task = new_task_map[task_id]
        actual_before = {
            "locked": old_task.get("locked", False),
            "fixed_start": old_task.get("fixed_start"),
            "fixed_end": old_task.get("fixed_end"),
            "resource_ids": deepcopy(old_task.get("resource_ids", [])),
            "resource_options": deepcopy(old_task.get("resource_options")),
            "commitment_ref": old_task.get("commitment_ref"),
        }
        actual_after = {
            "locked": new_task.get("locked", False),
            "fixed_start": new_task.get("fixed_start"),
            "fixed_end": new_task.get("fixed_end"),
            "resource_ids": deepcopy(new_task.get("resource_ids", [])),
            "resource_options": deepcopy(new_task.get("resource_options")),
            "commitment_ref": new_task.get("commitment_ref"),
        }
        if not _deep_equal(before, actual_before) or not _deep_equal(after, actual_after):
            raise ValueError(f"{path} before/after must exactly match the base Artifact and candidate task")
        if actual_before["locked"] is not True and not actual_before["commitment_ref"]:
            raise ValueError(f"{path} does not identify an existing locked or committed task")
        if actual_after["locked"] is not False or actual_after["fixed_start"] is not None or actual_after["fixed_end"] is not None:
            raise ValueError(f"{path} must explicitly clear the released fixed schedule")
        if actual_before["commitment_ref"] and actual_after["commitment_ref"] == actual_before["commitment_ref"]:
            raise ValueError(f"{path} must clear or replace the released commitment reference")
        reason, source_refs, evidence_refs = control_reason_and_refs(control, path)
        release_by_task[task_id] = {
            "task_id": task_id,
            "before": deepcopy(before),
            "after": deepcopy(after),
            "reason": reason,
            "source_refs": source_refs,
            "evidence_refs": evidence_refs,
            "record_status": "user_recorded_release_not_independently_verified",
        }

    correction_by_task: dict[str, dict[str, Any]] = {}
    execution_fields = {
        "actual_start",
        "actual_end",
        "actual_publish_at",
        "execution_status",
        "receipt_status",
        "external_receipt_refs",
        "metric_snapshots",
    }
    for index, control in enumerate(correction_controls):
        path = f"campaign_schedule_controls.execution_corrections[{index}]"
        expected_keys = {"task_id", "fields", "before", "after", "reason", "source_refs", "evidence_refs"}
        if not isinstance(control, dict) or set(control) != expected_keys:
            raise ValueError(f"{path} needs exact before/after fields, reason and source evidence")
        task_id = _required_text(control.get("task_id"), f"{path}.task_id")
        fields = control.get("fields")
        if (
            task_id in correction_by_task
            or task_id not in old_task_map
            or task_id not in new_task_map
            or not isinstance(fields, list)
            or not fields
            or any(not isinstance(field, str) or field not in execution_fields for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError(f"{path} must identify unique execution fields of a saved task")
        before = _mapping(control.get("before"), f"{path}.before")
        after = _mapping(control.get("after"), f"{path}.after")
        if set(before) != set(fields) or set(after) != set(fields):
            raise ValueError(f"{path}.before and after must name exactly the corrected fields")
        actual_before = {field: deepcopy(old_task_map[task_id].get(field)) for field in fields}
        actual_after = {field: deepcopy(new_task_map[task_id].get(field)) for field in fields}
        if not _deep_equal(before, actual_before) or not _deep_equal(after, actual_after):
            raise ValueError(f"{path} before/after must exactly match the base Artifact and candidate task")
        if all(_deep_equal(before[field], after[field]) for field in fields):
            raise ValueError(f"{path} does not change any recorded execution field")
        old_correction = old_task_map[task_id].get("execution_correction")
        if isinstance(old_correction, dict) and _deep_equal(old_correction, control):
            raise ValueError(f"{path} repeats an earlier correction record")
        reason, source_refs, evidence_refs = control_reason_and_refs(control, path)
        correction_by_task[task_id] = {
            "task_id": task_id,
            "fields": sorted(fields),
            "before": deepcopy(before),
            "after": deepcopy(after),
            "reason": reason,
            "source_refs": source_refs,
            "evidence_refs": evidence_refs,
            "record_status": "user_corrected_execution_record_in_new_artifact_version",
        }

    for identity in set(old_task_map) & set(new_task_map):
        prior_task, candidate_task = old_task_map[identity], new_task_map[identity]
        correction = correction_by_task.get(identity)
        correction_fields = set(correction["fields"]) if correction else set()
        for field in execution_fields:
            old_value = prior_task.get(field)
            new_value = candidate_task.get(field)
            old_recorded = old_value not in (None, [], "UNKNOWN")
            new_recorded = new_value not in (None, [], "UNKNOWN")
            if field in correction_fields:
                continue
            if old_recorded and not new_recorded:
                candidate_task[field] = deepcopy(old_value)
            elif not _deep_equal(old_value, new_value) and (old_recorded or new_recorded):
                raise ValueError(
                    f"Campaign task {identity} changes recorded execution field {field}; "
                    "supply an exact, evidence-backed execution_corrections revision control"
                )

    for task_id, correction in correction_by_task.items():
        old_actual = {
            field: deepcopy(old_task_map[task_id].get(field))
            for field in correction["fields"]
        }
        new_actual = {
            field: deepcopy(new_task_map[task_id].get(field))
            for field in correction["fields"]
        }
        if not _deep_equal(old_actual, correction["before"]) or not _deep_equal(new_actual, correction["after"]):
            raise ValueError(f"Execution correction for {task_id} no longer matches the exact revision inputs")
    old_resource_rows = old_inputs.get("resources", [])
    new_resource_rows = prepared.get("resources", [])
    old_resource_map = {
        row["id"]: row
        for row in old_resource_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(old_resource_rows, list) else {}
    new_resource_map = {
        row["id"]: row
        for row in new_resource_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(new_resource_rows, list) else {}
    resource_fields = ("capacity", "availability", "available_from", "available_to", "qualifications", "unit")
    changed_resource_ids = sorted(
        identity
        for identity in set(old_resource_map) | set(new_resource_map)
        if any(
            not _deep_equal(
                old_resource_map.get(identity, {}).get(field),
                new_resource_map.get(identity, {}).get(field),
            )
            for field in resource_fields
        )
    )

    old_brief = old_inputs.get("brief", {}) if isinstance(old_inputs.get("brief", {}), dict) else {}
    new_brief = prepared.get("brief", {}) if isinstance(prepared.get("brief", {}), dict) else {}
    window_fields = ("start_date", "end_date", "timezone")
    changed_window_fields = [
        field for field in window_fields
        if not _deep_equal(old_brief.get(field), new_brief.get(field))
    ]
    budget_changed = any(
        not _deep_equal(old_brief.get(field), new_brief.get(field))
        for field in ("budget", "currency")
    ) or not _deep_equal(old_inputs.get("quotes", []), prepared.get("quotes", []))

    changed_task_ids: set[str] = set()
    direct_replan_ids: set[str] = set()
    cost_changed_task_ids: set[str] = set()
    cost_fields = {"estimated_cost", "committed_cost", "actual_cost", "refund", "quote_id", "costs", "cost_status", "currency"}
    for identity in set(old_task_map) | set(new_task_map):
        old_row, new_row = old_task_map.get(identity), new_task_map.get(identity)
        if old_row is None or new_row is None:
            changed_task_ids.add(identity)
            direct_replan_ids.add(identity)
            continue
        old_signature = _revision_task_signature(old_row)
        new_signature = _revision_task_signature(new_row)
        changed_fields = {
            field for field in _REVISION_TASK_FIELDS
            if not _deep_equal(old_signature[field], new_signature[field])
        }
        if changed_fields:
            changed_task_ids.add(identity)
        if changed_fields & cost_fields:
            cost_changed_task_ids.add(identity)
        if changed_fields & _REVISION_TASK_REPLAN_FIELDS:
            direct_replan_ids.add(identity)

    old_touchpoints = old_inputs.get("member_touchpoints", [])
    new_touchpoints = prepared.get("member_touchpoints", [])
    old_touch_map = {
        row["id"]: row
        for row in old_touchpoints
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(old_touchpoints, list) else {}
    new_touch_map = {
        row["id"]: row
        for row in new_touchpoints
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(new_touchpoints, list) else {}
    changed_touch_ids = {
        identity
        for identity in set(old_touch_map) | set(new_touch_map)
        if not _deep_equal(old_touch_map.get(identity), new_touch_map.get(identity))
    }
    offer_terms_changed = False
    for touch_id in changed_touch_ids:
        for row in (old_touch_map.get(touch_id), new_touch_map.get(touch_id)):
            activation = row.get("activation") if isinstance(row, dict) else None
            if isinstance(activation, dict) and isinstance(activation.get("task_id"), str):
                changed_task_ids.add(activation["task_id"])
                old_activation = (old_touch_map.get(touch_id) or {}).get("activation", {})
                new_activation = (new_touch_map.get(touch_id) or {}).get("activation", {})
                schedule_keys = ("task_id", "contact_window")
                if any(not _deep_equal(old_activation.get(key), new_activation.get(key)) for key in schedule_keys):
                    direct_replan_ids.add(activation["task_id"])
            old_activation = (old_touch_map.get(touch_id) or {}).get("activation", {})
            new_activation = (new_touch_map.get(touch_id) or {}).get("activation", {})
            old_offer = old_activation.get("offer") if isinstance(old_activation, dict) else None
            new_offer = new_activation.get("offer") if isinstance(new_activation, dict) else None
            if not _deep_equal(old_offer, new_offer):
                offer_terms_changed = True
                old_window = {
                    key: old_offer.get(key)
                    for key in ("valid_from", "valid_until")
                } if isinstance(old_offer, dict) else None
                new_window = {
                    key: new_offer.get(key)
                    for key in ("valid_from", "valid_until")
                } if isinstance(new_offer, dict) else None
                if not _deep_equal(old_window, new_window):
                    for activation in (old_activation, new_activation):
                        if isinstance(activation, dict) and isinstance(activation.get("task_id"), str):
                            direct_replan_ids.add(activation["task_id"])
    budget_changed = budget_changed or bool(cost_changed_task_ids) or offer_terms_changed

    old_quotes = old_inputs.get("quotes", [])
    new_quotes = prepared.get("quotes", [])
    old_quote_map = {row.get("id"): row for row in old_quotes if isinstance(row, dict)} if isinstance(old_quotes, list) else {}
    new_quote_map = {row.get("id"): row for row in new_quotes if isinstance(row, dict)} if isinstance(new_quotes, list) else {}
    changed_quote_ids = {
        identity for identity in set(old_quote_map) | set(new_quote_map)
        if not _deep_equal(old_quote_map.get(identity), new_quote_map.get(identity))
    }
    if changed_quote_ids:
        for identity, task in {**old_task_map, **new_task_map}.items():
            uses_quote = task.get("quote_id") in changed_quote_ids or any(
                isinstance(row, dict) and row.get("quote_id") in changed_quote_ids
                for row in (task.get("costs") or [])
            )
            if uses_quote:
                changed_task_ids.add(identity)

    def resources_for(identity: str) -> set[str]:
        return _revision_task_resources(old_task_map.get(identity, {})) | _revision_task_resources(new_task_map.get(identity, {}))

    def window_for(
        identity: str, task_map: dict[str, dict[str, Any]], brief: dict[str, Any]
    ) -> tuple[datetime, datetime] | None:
        row = task_map.get(identity)
        if row is None:
            return None
        zone = _as_zone(brief.get("timezone"), "revision.brief.timezone") if brief.get("timezone") else None
        return _revision_task_window(row, brief, zone)

    task_ids = set(old_task_map) | set(new_task_map)
    resource_competition: dict[tuple[str, str, str, str], None] = {}
    for index, first in enumerate(sorted(task_ids)):
        for second in sorted(task_ids)[index + 1:]:
            for task_map, brief, basis in (
                (old_task_map, old_brief, "previous"),
                (new_task_map, new_brief, "candidate"),
            ):
                shared = _revision_task_resources(task_map.get(first, {})) & _revision_task_resources(task_map.get(second, {}))
                if not shared:
                    continue
                first_window = window_for(first, task_map, brief)
                second_window = window_for(second, task_map, brief)
                overlaps = (
                    first_window is None
                    or second_window is None
                    or max(first_window[0], second_window[0]) < min(first_window[1], second_window[1])
                )
                if overlaps:
                    for resource_id in shared:
                        resource_competition[(resource_id, first, second, basis)] = None

    dependency_edges: set[tuple[str, str]] = set()
    for task_map in (old_task_map, new_task_map):
        for identity, task in task_map.items():
            dependencies = task.get("dependencies", [])
            if isinstance(dependencies, list):
                for dependency in dependencies:
                    if isinstance(dependency, str):
                        dependency_edges.add((identity, dependency))

    budget_seeds: set[str] = set()
    if budget_changed:
        budget_seeds.update(cost_changed_task_ids)
        for identity, task in {**old_task_map, **new_task_map}.items():
            if (
                task.get("costs")
                or task.get("estimated_cost") is not None
                or task.get("committed_cost") is not None
                or task.get("quote_id")
                or task.get("mandatory", True) is False
            ):
                budget_seeds.add(identity)
        for row in [*(old_touchpoints if isinstance(old_touchpoints, list) else []), *(new_touchpoints if isinstance(new_touchpoints, list) else [])]:
            activation = row.get("activation") if isinstance(row, dict) else None
            if isinstance(activation, dict) and isinstance(activation.get("task_id"), str):
                budget_seeds.add(activation["task_id"])

    resource_seeds = set()
    for identity in task_ids:
        if resources_for(identity) & set(changed_resource_ids):
            resource_seeds.add(identity)
    if changed_window_fields:
        direct_replan_ids.update(task_ids)
    objective_fields = ("mandatory_tasks", "required_coverage", "exclusions", "benefit_unit")
    if any(
        not _deep_equal(old_inputs.get(field), prepared.get(field))
        for field in objective_fields
    ):
        direct_replan_ids.update(task_ids)
    if changed_resource_ids:
        direct_replan_ids.update(resource_seeds)
    direct_replan_ids.update(replan_task_ids)
    direct_replan_ids.update(inherited_release_by_task)

    affected_reasons: dict[str, set[str]] = {}
    for identity in direct_replan_ids:
        affected_reasons.setdefault(identity, set()).add(
            "恢复后沿用已记录的档期释放状态并重新核对排期"
            if identity in inherited_release_by_task
            else "直接任务或排期约束变化"
        )
    for identity in budget_seeds:
        affected_reasons.setdefault(identity, set()).add("共同预算/报价约束变化")
    for identity in resource_seeds:
        affected_reasons.setdefault(identity, set()).add("资源可用性/容量变化")
    for identity, reason in replan_reasons.items():
        affected_reasons.setdefault(identity, set()).add(f"用户请求局部重排：{reason}")
    affected_ids = set(affected_reasons)
    changed = True
    while changed:
        changed = False
        for first, second in dependency_edges:
            if first in affected_ids and second not in affected_ids:
                affected_ids.add(second)
                affected_reasons.setdefault(second, set()).add("前置/后继依赖闭包")
                changed = True
            elif second in affected_ids and first not in affected_ids:
                affected_ids.add(first)
                affected_reasons.setdefault(first, set()).add("前置/后继依赖闭包")
                changed = True
        for resource_id, first, second, _basis in resource_competition:
            if first in affected_ids and second not in affected_ids:
                affected_ids.add(second)
                affected_reasons.setdefault(second, set()).add(f"共享资源竞争：{resource_id}")
                changed = True
            elif second in affected_ids and first not in affected_ids:
                affected_ids.add(first)
                affected_reasons.setdefault(first, set()).add(f"共享资源竞争：{resource_id}")
                changed = True

    replan_ids = set(direct_replan_ids) | resource_seeds
    changed = True
    while changed:
        changed = False
        for first, second in dependency_edges:
            if first in replan_ids and second not in replan_ids:
                replan_ids.add(second)
                changed = True
            elif second in replan_ids and first not in replan_ids:
                replan_ids.add(first)
                changed = True
        for _resource_id, first, second, basis in resource_competition:
            if basis != "candidate":
                continue
            if first in replan_ids and second not in replan_ids:
                replan_ids.add(second)
                changed = True
            elif second in replan_ids and first not in replan_ids:
                replan_ids.add(first)
                changed = True

    schedule_confirmed_sections = set(previous_business.get("confirmed_sections", []))
    schedule_confirmed_sections.update(previous_business.get("manual_sections", []))
    schedule_confirmed = "campaign_schedule" in schedule_confirmed_sections
    hard_task_reasons: dict[str, set[str]] = {}
    released_task_ids = set(release_by_task)
    corrected_execution_task_ids = set(correction_by_task)

    def has_task_execution_record(task: dict[str, Any]) -> bool:
        if any(task.get(field) for field in ("actual_start", "actual_end", "actual_publish_at")):
            return True
        status = task.get("execution_status")
        return isinstance(status, str) and status.casefold() in {
            "complete",
            "completed",
            "performed",
            "published",
            "executed",
            "user_recorded_complete",
            "user_recorded_published",
        }

    for identity in task_ids:
        old_task = old_task_map.get(identity, {})
        prior_row = prior_task_map.get(identity, {})
        revision_lock_reason = prior_row.get("schedule_revision_lock_reason")
        prior_lock_is_soft = (
            "confirmed_or_manual_schedule_outside_impact" in revision_lock_reason
            if isinstance(revision_lock_reason, list)
            else revision_lock_reason == "confirmed_or_manual_schedule_outside_impact"
        )
        prior_lock_reasons = set(revision_lock_reason) if isinstance(revision_lock_reason, list) else {revision_lock_reason}
        prior_lock_is_actual = "原任务的实际时间/执行状态记录" in prior_lock_reasons
        prior_lock_is_commitment = "既有锁定承诺" in prior_lock_reasons
        current_task = new_task_map.get(identity, {})
        current_has_execution = has_task_execution_record(current_task)
        if (old_task.get("locked") is True or old_task.get("commitment_ref")) and identity not in released_task_ids:
            hard_task_reasons.setdefault(identity, set()).add("既有锁定承诺")
        elif (
            (prior_row.get("locked") is True or prior_row.get("status") == "PRESERVED_LOCKED")
            and not prior_lock_is_soft
            and identity not in released_task_ids
            and not (
                prior_lock_is_actual
                and not prior_lock_is_commitment
                and identity in corrected_execution_task_ids
                and not current_has_execution
            )
        ):
            hard_task_reasons.setdefault(identity, set()).add("既有锁定承诺")
        execution = prior_row.get("execution", {})
        prior_execution = old_task if has_task_execution_record(old_task) else {}
        if not prior_execution and isinstance(execution, dict) and has_task_execution_record(execution):
            prior_execution = execution
        if prior_execution and current_has_execution:
            hard_task_reasons.setdefault(identity, set()).add("原任务的实际时间/执行状态记录")
        elif prior_execution and identity not in corrected_execution_task_ids:
            hard_task_reasons.setdefault(identity, set()).add("原任务的实际时间/执行状态记录")

    for task_id in set(replan_task_ids) & set(hard_task_reasons) - released_task_ids:
        raise ValueError(
            f"Campaign task {task_id} is locked or has an actual execution record; use the explicit revision control before replanning it"
        )
    for task_id in released_task_ids:
        if task_id in hard_task_reasons and "原任务的实际时间/执行状态记录" in hard_task_reasons[task_id]:
            raise ValueError(
                f"Campaign task {task_id} has actual execution history; a commitment release cannot rewrite completed work"
            )

    scheduled_prior_ids = {
        identity
        for identity, row in prior_task_map.items()
        if row.get("start") is not None and row.get("end") is not None
    }
    preserved_ids = set(hard_task_reasons) & scheduled_prior_ids
    if schedule_confirmed:
        preserved_ids.update(
            identity for identity in set(prior_task_map) & set(new_task_map)
            if identity not in replan_ids and identity in scheduled_prior_ids
        )
    requested_hard_changes = []
    for identity in hard_task_reasons:
        old_row, new_row = old_task_map.get(identity), new_task_map.get(identity)
        if old_row is None:
            continue
        if new_row is None:
            raise ValueError(
                f"Campaign task {identity} has an existing lock or execution record and cannot be removed by a schedule revision"
            )
        changed_fields = [
            field for field in _REVISION_TASK_REPLAN_FIELDS
            if not _deep_equal(
                _revision_task_signature(old_row)[field],
                _revision_task_signature(new_row)[field],
            )
        ]
        if changed_fields:
            requested_hard_changes.append({"task_id": identity, "fields": sorted(changed_fields)})
    if requested_hard_changes:
        details = ", ".join(
            f"{row['task_id']} ({', '.join(row['fields'])})"
            for row in requested_hard_changes
        )
        raise ValueError(
            "Campaign revisions cannot change a locked or executed task without reconciling its commitment first: "
            + details
        )

    hard_ids = set(hard_task_reasons)
    preserved_reasons = {
        identity: sorted(reasons) for identity, reasons in hard_task_reasons.items()
    }
    if schedule_confirmed:
        soft_confirmed = (set(prior_task_map) & set(new_task_map) & scheduled_prior_ids) - replan_ids - hard_ids
        preserved_ids.update(soft_confirmed)
        for identity in soft_confirmed:
            preserved_reasons[identity] = ["confirmed_or_manual_schedule_outside_impact"]
    previous_tasks = {}
    for identity, row in prior_task_map.items():
        previous_tasks[identity] = {
            "title": row.get("title"),
            "start": row.get("start"),
            "end": row.get("end"),
            "resource_ids": deepcopy(row.get("resource_ids", [])),
            "status": row.get("status"),
            "locked": row.get("locked", False),
            "execution": {
                key: deepcopy(row.get("execution", {}).get(key))
                for key in ("actual_start", "actual_end", "actual_publish_at", "execution_status", "receipt_status", "record_status")
            } if isinstance(row.get("execution"), dict) else {},
        }
    resource_rows = [
        {"resource_id": resource_id, "task_ids": [first, second], "basis": basis}
        for resource_id, first, second, basis in sorted(resource_competition)
        if first in affected_ids or second in affected_ids
    ]
    context = {
        "kind": "campaign_schedule_revision",
        "previous_status": matches[0]["data"].get("status"),
        "confirmed_section": schedule_confirmed,
        "changed_fields": sorted(
            key for key in set(old_inputs) | set(prepared)
            if not _deep_equal(old_inputs.get(key), prepared.get(key))
        ),
        "changed_task_ids": sorted(changed_task_ids),
        "changed_resource_ids": changed_resource_ids,
        "direct_replan_task_ids": sorted(direct_replan_ids),
        "replan_task_ids": sorted(replan_ids),
        "affected_task_ids": sorted(affected_ids),
        "affected_reasons": {identity: sorted(reasons) for identity, reasons in sorted(affected_reasons.items())},
        "preserved_task_ids": sorted(preserved_ids),
        "preserved_task_reasons": preserved_reasons,
        "hard_task_reasons": {identity: sorted(reasons) for identity, reasons in sorted(hard_task_reasons.items())},
        "explicit_replan_tasks": [
            {"task_id": identity, "reason": replan_reasons[identity]}
            for identity in sorted(replan_task_ids)
        ],
        "commitment_releases": [release_by_task[identity] for identity in sorted(release_by_task)],
        "execution_corrections": [correction_by_task[identity] for identity in sorted(correction_by_task)],
        "revision_note": revision_note,
        "resource_competition": resource_rows,
        "previous_tasks": previous_tasks,
        "removed_task_ids": sorted(set(old_task_map) - set(new_task_map)),
        "added_task_ids": sorted(set(new_task_map) - set(old_task_map)),
        "current_commitment_status": _campaign_current_commitment_status_rows(
            previous_business,
            inputs=prepared,
        ),
    }
    return prepared, context


def preserve_campaign_revision_state_on_restore(
    restored_business: dict[str, Any],
    current_business: dict[str, Any],
    *,
    current_version: int,
    restore_version: int,
) -> dict[str, Any]:
    """Return a merge fragment that keeps the latest task actuals on restore.

    Plan inputs and schedule placements come from ``restored_business``.
    Only explicit task-level execution fields and versioned revision controls
    are carried forward from ``current_business``. Business action receipts are
    intentionally outside this function; file generation or browser input does
    not establish campaign task completion.
    """
    if type(current_version) is not int or current_version < 1:
        raise ValueError("current_version must be a positive integer")
    if type(restore_version) is not int or restore_version < 1:
        raise ValueError("restore_version must be a positive integer")
    if not isinstance(restored_business, dict) or not isinstance(current_business, dict):
        raise ValueError("campaign restore needs the current and restored business snapshots")
    if (
        restored_business.get("operation") != "campaign_schedule"
        or current_business.get("operation") != "campaign_schedule"
    ):
        return {}

    task_fields = (
        "actual_start",
        "actual_end",
        "actual_publish_at",
        "execution_status",
        "receipt_status",
        "external_receipt_refs",
        "metric_snapshots",
    )

    def task_map(business: dict[str, Any]) -> dict[str, dict[str, Any]]:
        tasks = business.get("inputs", {}).get("tasks", [])
        if not isinstance(tasks, list):
            return {}
        return {
            row["id"]: row
            for row in tasks
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }

    def schedule_section(business: dict[str, Any], section_key: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        sections = business.get(section_key)
        if section_key == "result":
            sections = (sections or {}).get("sections")
        if not isinstance(sections, list):
            return [], None
        matches = [
            section for section in sections
            if isinstance(section, dict) and section.get("id") == "campaign_schedule"
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("data"), dict):
            return sections, None
        return sections, matches[0]

    restored_inputs = deepcopy(restored_business.get("inputs", {}))
    if not isinstance(restored_inputs, dict):
        restored_inputs = {}
    restored_tasks = restored_inputs.get("tasks", [])
    if not isinstance(restored_tasks, list):
        restored_tasks = []
        restored_inputs["tasks"] = restored_tasks
    restored_task_map = {
        row["id"]: row
        for row in restored_tasks
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    current_task_map = task_map(current_business)
    _, current_schedule = schedule_section(current_business, "generated_sections")
    _, current_result_schedule = schedule_section(current_business, "result")
    current_task_outputs = {}
    if current_schedule is not None:
        current_task_outputs = {
            row["id"]: row
            for row in current_schedule["data"].get("tasks", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
    for section in (current_result_schedule,):
        if section is not None:
            for row in section["data"].get("tasks", []):
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    current_task_outputs.setdefault(row["id"], row)

    execution_correction_records: list[dict[str, Any]] = []

    def collect_execution_corrections(value: Any, source_version: int) -> None:
        if not isinstance(value, dict):
            return
        candidates = []
        controls = value.get("campaign_schedule_controls")
        if isinstance(controls, dict):
            candidates.append(controls.get("execution_corrections", []))
        revision_impact = value.get("revision_impact")
        if isinstance(revision_impact, dict):
            candidates.append(revision_impact.get("execution_corrections", []))
        candidates.append(value.get("execution_corrections", []))
        for rows in candidates:
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
                    continue
                record = deepcopy(row)
                record.setdefault("source_version", source_version)
                record["record_status"] = record.get(
                    "record_status",
                    "user_corrected_execution_record_in_new_artifact_version",
                )
                if record not in execution_correction_records:
                    execution_correction_records.append(record)

    for record in current_business.get("campaign_revision_history", []):
        if isinstance(record, dict):
            collect_execution_corrections(record, record.get("source_version", current_version))
    current_impact_record = current_business.get("impact", {})
    if isinstance(current_impact_record, dict):
        collect_execution_corrections(current_impact_record, current_version)
    for section in (current_schedule, current_result_schedule):
        if section is not None:
            collect_execution_corrections(section["data"], current_version)

    corrections_by_task_field: dict[tuple[str, str], dict[str, Any]] = {}
    for correction in execution_correction_records:
        task_id = correction.get("task_id")
        fields = correction.get("fields", [])
        before = correction.get("before", {})
        after = correction.get("after", {})
        if not isinstance(fields, list) or not isinstance(before, dict) or not isinstance(after, dict):
            continue
        for field in fields:
            if isinstance(field, str) and field in before and field in after:
                corrections_by_task_field[(task_id, field)] = correction

    actual_records = []
    for task_id in sorted(set(current_task_map) | set(current_task_outputs)):
        task = current_task_map.get(task_id, {})
        output = current_task_outputs.get(task_id, {})
        output_execution = output.get("execution", {}) if isinstance(output, dict) else {}
        if not isinstance(output_execution, dict):
            output_execution = {}
        values = {}
        explicit_corrections = []
        explicitly_cleared_fields = []
        for field in task_fields:
            correction = corrections_by_task_field.get((task_id, field))
            if correction is not None:
                value = deepcopy(correction["after"][field])
                if correction not in explicit_corrections:
                    explicit_corrections.append(deepcopy(correction))
                if value in (None, [], "UNKNOWN"):
                    explicitly_cleared_fields.append(field)
                values[field] = value
                continue
            value = task.get(field)
            if value in (None, [], "UNKNOWN"):
                value = output_execution.get(field)
            if value not in (None, [], "UNKNOWN"):
                values[field] = deepcopy(value)
        actual_cost_fields = {
            field: deepcopy(task.get(field))
            for field in ("actual_cost", "refund")
            if task.get(field) is not None
        }
        if actual_cost_fields and task.get("currency") is not None:
            actual_cost_fields["currency"] = deepcopy(task["currency"])
        if not values and not actual_cost_fields and not explicit_corrections:
            continue
        actual_record = {
            "task_id": task_id,
            "title": task.get("title") or output.get("title"),
            "execution_fields": values,
            "actual_cost_fields": actual_cost_fields,
            "source_refs": deepcopy(task.get("source_refs", [])),
            "evidence_refs": deepcopy(task.get("evidence_refs", [])),
            "record_status": (
                "user_corrected_task_actual_record_in_artifact_version_not_independently_verified"
                if explicit_corrections
                else "user_supplied_task_execution_fields_not_independently_verified"
            ),
            "source_version": current_version,
            "planned_in_restored_version": task_id in restored_task_map,
            "explicitly_cleared_fields": sorted(set(explicitly_cleared_fields)),
            "correction_records": explicit_corrections,
            "has_explicit_actual_fields": bool(
                values.get("actual_start")
                or values.get("actual_end")
                or values.get("actual_publish_at")
                or (isinstance(values.get("execution_status"), str) and values["execution_status"].casefold() in {
                    "complete", "completed", "performed", "published", "executed",
                    "user_recorded_complete", "user_recorded_published",
                })
            ),
        }
        actual_records.append(actual_record)

    # Put actual facts back on matching task inputs without changing the
    # restored plan's time, resource assignment, duration or dependencies.
    history_by_id = {row["task_id"]: row for row in actual_records}
    for task_id, restored_task in restored_task_map.items():
        record = history_by_id.get(task_id)
        if record is None:
            continue
        for field, value in record["execution_fields"].items():
            restored_task[field] = deepcopy(value)

    previous_task_history = restored_business.get("campaign_task_history", [])
    current_task_history = current_business.get("campaign_task_history", [])
    merged_task_history = []
    for collection in (previous_task_history, current_task_history, actual_records):
        if not isinstance(collection, list):
            continue
        for row in collection:
            if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
                continue
            copied = deepcopy(row)
            copied.setdefault("record_status", "historical_campaign_task_record_preserved")
            if copied not in merged_task_history:
                merged_task_history.append(copied)

    previous_revision_history = restored_business.get("campaign_revision_history", [])
    current_revision_history = current_business.get("campaign_revision_history", [])
    merged_revision_history = []
    for collection in (previous_revision_history, current_revision_history):
        if isinstance(collection, list):
            for row in collection:
                if isinstance(row, dict) and row not in merged_revision_history:
                    merged_revision_history.append(deepcopy(row))

    current_impact = current_business.get("impact", {})
    current_controls = current_impact.get("campaign_schedule_controls") if isinstance(current_impact, dict) else None
    current_revision_impact = (
        current_schedule["data"].get("revision_impact")
        if current_schedule is not None
        else None
    )
    if current_controls or current_revision_impact:
        history_row = {
            "source_version": current_version,
            "restored_from_version": restore_version,
            "campaign_schedule_controls": deepcopy(current_controls or {}),
            "revision_impact": deepcopy(current_revision_impact or {}),
            "record_status": "historical_campaign_revision_control_preserved",
            "not_reapplied_on_restore": True,
        }
        if history_row not in merged_revision_history:
            merged_revision_history.append(history_row)

    commitment_status_rows = _campaign_current_commitment_status_rows(current_business)
    if not commitment_status_rows:
        commitment_status_rows = _campaign_current_commitment_status_rows(restored_business)

    current_task_titles = {
        identity: row.get("title")
        for identity, row in current_task_map.items()
        if isinstance(row.get("title"), str)
    }

    def restore_commitment_note(status: dict[str, Any], *, client: bool) -> str:
        task_id = status["task_id"]
        task_title = current_task_titles.get(task_id)
        if not task_title:
            task_title = next(
                (row.get("title") for row in restored_tasks
                 if isinstance(row, dict) and row.get("id") == task_id and isinstance(row.get("title"), str)),
                "该任务",
            )
        if client:
            return (
                f"- {task_title}：本恢复版保留旧排期作为方案参照；后续版本曾记录档期释放，"
                "场地/供应方当前是否已实际解除仍待核。恢复旧方案不会重新确认该外部承诺；"
                "再次排期时将沿用已记录的释放状态。"
            )
        return (
            f"- {task_title}：第 {status.get('source_version') or '未知'} 版记录档期释放，"
            f"理由：{status.get('reason') or '未提供'}。这只是用户记录，未独立核验外部取消；"
            "恢复版保留原排期时段/资源作为历史方案参照，不能据此确认原承诺仍有效。"
            "后续排期重算会沿用当前释放状态。"
        )

    def merge_schedule_actuals(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = deepcopy(sections)
        for section in result:
            if not isinstance(section, dict) or section.get("id") != "campaign_schedule":
                continue
            data = section.get("data")
            if not isinstance(data, dict):
                continue
            task_rows = data.get("tasks", [])
            if isinstance(task_rows, list):
                for row in task_rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        continue
                    record = history_by_id.get(row["id"])
                    if record is not None:
                        execution = row.setdefault("execution", {})
                        if isinstance(execution, dict):
                            for field, value in record["execution_fields"].items():
                                execution[field] = deepcopy(value)
                            execution["record_status"] = record["record_status"]
                        row["restore_actuals_preserved_from_version"] = current_version
            if actual_records:
                data["current_actual_task_records"] = deepcopy(actual_records)
            if merged_revision_history:
                data["restored_revision_history"] = deepcopy(merged_revision_history)
            if commitment_status_rows:
                data["current_commitment_status"] = deepcopy(commitment_status_rows)
                data["restored_plan_is_historical_reference"] = True
                for row in task_rows if isinstance(task_rows, list) else []:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        continue
                    status = next(
                        (item for item in commitment_status_rows if item["task_id"] == row["id"]),
                        None,
                    )
                    if status is not None:
                        row["current_commitment_status"] = deepcopy(status)
                internal_notes = [
                    restore_commitment_note(row, client=False)
                    for row in commitment_status_rows
                    if row.get("status") == "user_recorded_release_not_independently_verified"
                ]
                client_notes = [
                    restore_commitment_note(row, client=True)
                    for row in commitment_status_rows
                    if row.get("status") == "user_recorded_release_not_independently_verified"
                ]
                if internal_notes:
                    section["body"] = (
                        (section.get("body", "").rstrip() + "\n\n" if isinstance(section.get("body"), str) else "")
                        + "### 恢复方案中的当前承诺状态\n"
                        + "\n".join(internal_notes)
                    )
                if client_notes and isinstance(section.get("client_body"), str):
                    section["client_body"] = (
                        section["client_body"].rstrip()
                        + "\n\n"
                        + "\n".join(client_notes)
                    )
        return result

    restored_generated = restored_business.get("generated_sections", [])
    restored_result = deepcopy(restored_business.get("result", {}))
    if not isinstance(restored_result, dict):
        restored_result = {}
    fragment = {
        "inputs": restored_inputs,
        "generated_sections": merge_schedule_actuals(restored_generated if isinstance(restored_generated, list) else []),
        "result": {
            **restored_result,
            "sections": merge_schedule_actuals(restored_result.get("sections", []) if isinstance(restored_result.get("sections", []), list) else []),
        },
    }
    if merged_task_history:
        fragment["campaign_task_history"] = merged_task_history
    if merged_revision_history:
        fragment["campaign_revision_history"] = merged_revision_history
    if commitment_status_rows:
        fragment["campaign_current_commitment_status"] = commitment_status_rows
    return fragment


def _campaign_current_commitment_status_rows(
    business: dict[str, Any],
    *,
    inputs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive each task's latest saved release status from Artifact history.

    This is a projection over the existing versioned business result, not a
    second commitment ledger. A release remains a user-recorded fact and does
    not assert that a supplier or venue completed an external cancellation.
    """
    if not isinstance(business, dict) or business.get("operation") != "campaign_schedule":
        return []

    releases: list[dict[str, Any]] = []

    def collect(value: Any, source_version: Any) -> None:
        if not isinstance(value, dict):
            return
        controls = value.get("campaign_schedule_controls")
        revision_impact = value.get("revision_impact")
        collections = []
        if isinstance(controls, dict):
            collections.append(controls.get("commitment_releases", []))
        if isinstance(revision_impact, dict):
            collections.append(revision_impact.get("commitment_releases", []))
        collections.append(value.get("commitment_releases", []))
        for rows in collections:
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
                    continue
                before, after = row.get("before"), row.get("after")
                if not isinstance(before, dict) or not isinstance(after, dict):
                    continue
                record = deepcopy(row)
                record.setdefault("source_version", source_version)
                if record not in releases:
                    releases.append(record)

    for row in business.get("campaign_revision_history", []):
        if isinstance(row, dict):
            collect(row, row.get("source_version"))
    collect(business.get("impact", {}), business.get("input_snapshot_revision"))
    for key in ("generated_sections", "result"):
        sections = business.get(key, [])
        if key == "result" and isinstance(sections, dict):
            sections = sections.get("sections", [])
        if isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict) and section.get("id") == "campaign_schedule":
                    collect(section.get("data"), business.get("input_snapshot_revision"))

    release_fields = {"locked", "fixed_start", "fixed_end", "resource_ids", "resource_options", "commitment_ref"}
    source_inputs = inputs if isinstance(inputs, dict) else business.get("inputs", {})
    task_rows = source_inputs.get("tasks", []) if isinstance(source_inputs, dict) else []
    task_map = {
        row["id"]: row
        for row in task_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(task_rows, list) else {}
    latest: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(releases):
        task_id = row["task_id"]
        version = row.get("source_version")
        prior = latest.get(task_id)
        prior_version = prior.get("source_version") if isinstance(prior, dict) else None
        if prior is None or (
            type(version) is int
            and (type(prior_version) is not int or version >= prior_version)
        ):
            copied = deepcopy(row)
            copied["_history_order"] = index
            latest[task_id] = copied

    result = []
    for task_id, row in sorted(latest.items()):
        before, after = row.get("before"), row.get("after")
        if set(before) != release_fields or set(after) != release_fields:
            continue
        current_task = task_map.get(task_id)
        if current_task is None:
            current_status = "task_not_in_current_plan"
        else:
            values = {
                field: deepcopy(current_task.get(field, [] if field == "resource_ids" else None))
                for field in release_fields
            }
            if _deep_equal(values, after) or _deep_equal(values, before):
                current_status = "user_recorded_release_not_independently_verified"
            else:
                current_status = "superseded_by_later_task_state"
        copied = {key: deepcopy(value) for key, value in row.items() if key != "_history_order"}
        copied.update(
            status=current_status,
            external_release_verified=False,
            evidence_revalidation_status="historical_reference_not_revalidated",
        )
        result.append(copied)
    return result


def render_campaign_task_history(business: dict[str, Any]) -> str | None:
    """Render preserved task-level actual records for internal Artifact text.

    Call this only from the shared internal business renderer. Client sections
    remain their separately prepared safe views.
    """
    if not isinstance(business, dict) or business.get("operation") != "campaign_schedule":
        return None
    records = business.get("campaign_task_history", [])
    if not isinstance(records, list):
        records = []
    inputs = business.get("inputs", {})
    input_tasks = inputs.get("tasks", []) if isinstance(inputs, dict) else []
    task_titles = {
        row["id"]: row.get("title")
        for row in input_tasks
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(input_tasks, list) else {}
    fields = {
        "actual_start": "用户记录的实际开始",
        "actual_end": "用户记录的实际结束",
        "actual_publish_at": "用户记录的实际发布时间",
        "execution_status": "用户记录的执行状态",
        "receipt_status": "用户记录的回执状态",
    }
    execution_labels = {
        "complete": "记录为已完成（未独立核验）",
        "completed": "记录为已完成（未独立核验）",
        "performed": "记录为已执行（未独立核验）",
        "published": "记录为已发布（未独立核验）",
        "executed": "记录为已执行（未独立核验）",
        "user_recorded_complete": "用户记录完成（未独立核验）",
        "user_recorded_published": "用户记录发布（未独立核验）",
        "failed": "记录为失败（未独立核验）",
        "unknown": "结果未知",
        "not_started": "记录为尚未开始",
    }
    receipt_labels = {
        "file_generated": "文件已生成，但不代表已交付给客户",
        "browser_input_delivered": "输入已送入浏览器，业务结果仍待核对",
        "effect_unknown": "外部效果未知，需要先核对原操作回执",
        "verified": "有用户记录的回执（未独立核验）",
        "not_checked": "尚未核对回执",
        "unknown": "回执未知",
    }
    execution_field_labels = {
        "actual_start": "实际开始时间",
        "actual_end": "实际结束时间",
        "actual_publish_at": "实际发布时间",
        "execution_status": "执行状态",
        "receipt_status": "回执状态",
        "external_receipt_refs": "外部回执引用",
        "metric_snapshots": "观测快照",
    }

    def value_text(value: Any, field: str) -> str:
        if value is None or (isinstance(value, str) and value.strip().casefold() == "unknown"):
            return "未知"
        if field == "execution_status" and isinstance(value, str):
            return execution_labels.get(value.casefold(), "用户记录状态：“" + value + "”（未独立核验）")
        if field == "receipt_status" and isinstance(value, str):
            return receipt_labels.get(value.casefold(), "用户记录回执：“" + value + "”（未独立核验）")
        return str(value)

    lines = []
    if records:
        lines.extend(
            [
                "## 当前 Campaign 任务实际记录（不随方案恢复撤销）",
                "以下是与任务关联的用户记录和回执字段；保留历史不代表平台效果、现场履约或对外送达已独立核实。",
            ]
        )
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            continue
        label = record.get("title") or task_titles.get(record["task_id"]) or "未命名任务"
        lines.append(f"- {label}：来源Artifact版本 {record.get('source_version') or '未知'}；")
        lines.append(
            "  - "
            + (
                "该任务仍在恢复后的排期中。"
                if record.get("planned_in_restored_version")
                else "该任务不在恢复后的排期中；仅保留其实际记录，不重新排入计划。"
            )
        )
        execution_fields = record.get("execution_fields", {})
        if isinstance(execution_fields, dict):
            for field in ("actual_start", "actual_end", "actual_publish_at", "execution_status", "receipt_status"):
                if field in execution_fields:
                    lines.append(f"  - {fields[field]}：{value_text(execution_fields[field], field)}。")
            receipts = execution_fields.get("external_receipt_refs")
            if isinstance(receipts, list) and receipts:
                lines.append(f"  - 另有 {len(receipts)} 条外部回执引用；不据此推断活动完成或业务结果。")
            snapshots = execution_fields.get("metric_snapshots")
            if isinstance(snapshots, list) and snapshots:
                for snapshot in snapshots:
                    if not isinstance(snapshot, dict):
                        continue
                    lines.append(
                        f"  - 观测：{snapshot.get('metric') or '指标未知'} {snapshot.get('value') if snapshot.get('value') is not None else '未知'} "
                        f"{snapshot.get('unit') or ''}；分母 {snapshot.get('denominator') if snapshot.get('denominator') is not None else '未知'}；"
                        f"窗口 {snapshot.get('observation_window') or '未知'}；观察时间 {snapshot.get('observed_at') or '未知'}；"
                        "用户提供，未独立核验。"
                    )
        cost_fields = record.get("actual_cost_fields", {})
        if isinstance(cost_fields, dict) and cost_fields:
            lines.append(
                "  - 任务实支记录："
                f"{cost_fields.get('actual_cost') if cost_fields.get('actual_cost') is not None else '未知'}；"
                f"退款记录 {cost_fields.get('refund') if cost_fields.get('refund') is not None else '未知'}；"
                f"币种 {cost_fields.get('currency') or '未知'}。"
            )
        corrections = record.get("correction_records", [])
        if isinstance(corrections, list):
            for correction in corrections:
                if not isinstance(correction, dict):
                    continue
                after = correction.get("after", {})
                fields_changed = correction.get("fields", [])
                corrected = []
                if isinstance(after, dict) and isinstance(fields_changed, list):
                    for field in fields_changed:
                        if field in after:
                            label_text = fields.get(field, field)
                            corrected.append(f"{label_text}现为 {value_text(after[field], field)}")
                lines.append(
                    "  - 已保留新Artifact版更正："
                    + ("；".join(corrected) or "字段差异见结构化修订记录")
                    + f"；理由 {correction.get('reason') or '未提供'}；本次未独立核验。"
                )
    revision_rows = business.get("campaign_revision_history", [])
    if not isinstance(revision_rows, list):
        revision_rows = []
    impact = business.get("impact", {})
    current_controls = impact.get("campaign_schedule_controls") if isinstance(impact, dict) else None
    if current_controls:
        current_record = {
            "source_version": business.get("input_snapshot_revision"),
            "campaign_schedule_controls": current_controls,
            "record_status": "current_revision_controls",
        }
        if current_record not in revision_rows:
            revision_rows = [*revision_rows, current_record]
    stored_commitment_status_rows = business.get("campaign_current_commitment_status", [])
    commitment_status_rows = _campaign_current_commitment_status_rows(business)
    if not commitment_status_rows and isinstance(stored_commitment_status_rows, list):
        commitment_status_rows = deepcopy(stored_commitment_status_rows)
    if commitment_status_rows:
        lines.extend(
            [
                "## 当前承诺状态（与恢复方案分开）",
                "原版本排期保持为历史方案参照；此处记录的释放状态不会由恢复旧计划重新确认。用户记录不证明场地/供应方已实际取消。",
            ]
        )
        for status in commitment_status_rows:
            if not isinstance(status, dict):
                continue
            task_id = status.get("task_id")
            label = task_titles.get(task_id, "未命名任务")
            state = status.get("status")
            if state == "user_recorded_release_not_independently_verified":
                lines.append(
                    f"- {label}：第 {status.get('source_version') or '未知'} 版记录档期释放；"
                    f"理由 {status.get('reason') or '未提供'}；恢复版本仍保留原排期作参照，"
                    "后续排期重算会沿用该释放记录；外部取消状态未独立核验。"
                )
            elif state == "superseded_by_later_task_state":
                lines.append(
                    f"- {label}：历史释放记录已由后续任务输入替代；本段保留历史来源，不把它当当前承诺状态。"
                )
            elif state == "task_not_in_current_plan":
                lines.append(
                    f"- {label}：任务不在当前排期输入中；保留历史释放记录，不重新添加任务。"
                )
    if revision_rows:
        lines.extend(
            [
                "## Campaign 排期修订控制历史",
                "以下只记录版本化的计划变更/记录更正；不代表外部预订、取消、发送或履约结果已完成。",
            ]
        )
        for revision in revision_rows:
            if not isinstance(revision, dict):
                continue
            controls = revision.get("campaign_schedule_controls", revision.get("controls", {}))
            if not isinstance(controls, dict):
                controls = {}
            source_version = revision.get("source_version") or "未知"
            for change in controls.get("replan_tasks", []) if isinstance(controls.get("replan_tasks", []), list) else []:
                if isinstance(change, dict):
                    label = task_titles.get(change.get("task_id"), "已选择任务")
                    lines.append(
                        f"- 第 {source_version} 版请求重排 {label}：{change.get('reason') or '未提供理由'}；不代表已实际移动资源。"
                    )
            for change in controls.get("commitment_releases", []) if isinstance(controls.get("commitment_releases", []), list) else []:
                if isinstance(change, dict):
                    label = task_titles.get(change.get("task_id"), "已选择任务")
                    lines.append(
                        f"- 第 {source_version} 版记录 {label} 的锁定释放理由：{change.get('reason') or '未提供理由'}；"
                        "未独立核验场地/供应商是否已解除。"
                    )
            for change in controls.get("execution_corrections", []) if isinstance(controls.get("execution_corrections", []), list) else []:
                if isinstance(change, dict):
                    label = task_titles.get(change.get("task_id"), "已选择任务")
                    before = change.get("before", {})
                    after = change.get("after", {})
                    fields_changed = change.get("fields", [])
                    corrected = []
                    if isinstance(fields_changed, list) and isinstance(before, dict) and isinstance(after, dict):
                        for field in fields_changed:
                            if field in before and field in after:
                                field_label = execution_field_labels.get(field, field)
                                corrected.append(
                                    f"{field_label} {value_text(before[field], field)} → {value_text(after[field], field)}"
                                )
                    lines.append(
                        f"- 第 {source_version} 版修正 {label}："
                        f"{'；'.join(corrected) or '字段差异见结构化修订记录'}；"
                        f"理由 {change.get('reason') or '未提供理由'}；本次未独立核验。"
                    )
    if not lines:
        return None
    return "\n".join(lines)


def evaluate_campaign(
    operation: str,
    inputs: dict[str, Any],
    *,
    revision_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one campaign/business operation without changing ``inputs``.

    ``ValueError`` means the supplied operation or data shape cannot be
    interpreted safely. Business constraints that are valid but unsatisfied
    are returned in the affected section as explicit gaps/conflicts.
    """

    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        raise ValueError("unsupported campaign operation")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    if "_campaign_revision_context" in inputs:
        raise ValueError("campaign revision context is internal and cannot be supplied in inputs")
    if "campaign_schedule_controls" in inputs:
        raise ValueError("campaign schedule controls are revision fields, not business inputs")
    if "candidate_removals" in inputs:
        raise ValueError("candidate_removals can only be applied through a saved strategy revision")
    expected_revision_kind = {
        "campaign_schedule": "campaign_schedule_revision",
        "strategy_proposal": "strategy_proposal_revision",
    }.get(operation)
    if revision_context is not None and (
        not isinstance(revision_context, dict)
        or revision_context.get("kind") != expected_revision_kind
    ):
        raise ValueError("campaign revision context is invalid for this operation")

    original = deepcopy(inputs)
    brief = _mapping(inputs.get("brief", {}), "brief")
    _validate_brief(brief)
    quotes = _quote_index(inputs.get("quotes", []), brief.get("currency"))

    if operation == "strategy_proposal":
        result = _evaluate_strategy(brief, inputs, quotes, revision_context=revision_context)
    elif operation == "media_plan":
        result = _evaluate_media(brief, inputs, quotes)
    elif operation == "collaboration_plan":
        result = _evaluate_collaborations(brief, inputs, quotes)
    else:
        result = _evaluate_schedule(brief, inputs, quotes, revision_context=revision_context)

    result["client_title"] = brief.get("client_title") or {
        "strategy_proposal": "品牌策略与提案",
        "media_plan": "媒介方案",
        "collaboration_plan": "合作方案",
        "campaign_schedule": "活动与内容计划",
    }[operation]

    if not _deep_equal(inputs, original):
        raise RuntimeError("campaign evaluation mutated its inputs")
    return _json_safe(result)


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _rows(value: Any, path: str, *, id_field: str = "id") -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        row = _mapping(raw, f"{path}[{index}]")
        identity = _required_text(row.get(id_field), f"{path}[{index}].{id_field}")
        if row[id_field] != identity:
            raise ValueError(f"{path}[{index}].{id_field} cannot have surrounding whitespace")
        if identity in seen:
            raise ValueError(f"{path} contains duplicate {id_field}: {identity}")
        seen.add(identity)
        rows.append(row)
    return rows


def _required_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be text or null")
    return value.strip() or None


def _deep_equal(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_deep_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_deep_equal(a, b) for a, b in zip(left, right))
    return left == right


def _decimal(value: Any, path: str, *, minimum: Decimal | None = None) -> Decimal:
    if type(value) not in (int, float):
        raise ValueError(f"{path} must be a finite number")
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must be a finite number")
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise ValueError(f"{path} is outside the allowed range")
    return parsed


def _nonnegative(value: Any, path: str) -> Decimal:
    return _decimal(value, path, minimum=Decimal(0))


def _finite(value: Any, path: str) -> Decimal:
    return _decimal(value, path)


def _positive(value: Any, path: str) -> Decimal:
    parsed = _decimal(value, path)
    if parsed <= 0:
        raise ValueError(f"{path} must be greater than zero")
    return parsed


def _json_number(value: Decimal) -> int | float | str:
    if value == value.to_integral_value():
        return int(value)
    result = float(value)
    if math.isfinite(result):
        return result
    return format(value, "f")


def _currency(value: Any, path: str, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or _CURRENCY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{path} must be a three-letter uppercase currency code")
    return value


def _refs(item: dict[str, Any], path: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("source_refs", "evidence_refs"):
        refs = item.get(key, [])
        if refs is not None and not isinstance(refs, list):
            raise ValueError(f"{path}.{key} must be a list")
        result[key] = deepcopy(refs)
    result["basis_status"] = (
        "references_provided_not_independently_verified"
        if result["source_refs"] or result["evidence_refs"]
        else "user_input_not_independently_verified"
    )
    return result


def _validate_brief(brief: dict[str, Any]) -> None:
    for key in ("goal", "audience", "market", "timezone", "client_title"):
        if key in brief and brief[key] is not None:
            _optional_text(brief[key], f"brief.{key}")
    if "currency" in brief:
        _currency(brief["currency"], "brief.currency")
    for key in ("constraints", "forbidden_claims", "customer_requests"):
        value = brief.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"brief.{key} must be a list of text")
    facts = brief.get("facts", [])
    if not isinstance(facts, list):
        raise ValueError("brief.facts must be a list")
    for index, fact in enumerate(facts):
        row = _mapping(fact, f"brief.facts[{index}]")
        if "text" in row:
            _optional_text(row["text"], f"brief.facts[{index}].text")
        _refs(row, f"brief.facts[{index}]")
    budget = brief.get("budget", {})
    if budget is None:
        budget = {}
    budget = _mapping(budget, "brief.budget")
    currency = _currency(budget.get("currency"), "brief.budget.currency", brief.get("currency"))
    for key in ("maximum", "hard_limit", "target", "reserve"):
        if budget.get(key) is not None:
            _nonnegative(budget[key], f"brief.budget.{key}")
    maximum = budget.get("maximum", budget.get("hard_limit"))
    if maximum is not None and budget.get("reserve") is not None:
        if _nonnegative(budget["reserve"], "brief.budget.reserve") > _nonnegative(
            maximum, "brief.budget.maximum"
        ):
            raise ValueError("brief.budget.reserve cannot exceed the maximum")
    _refs(brief, "brief")
    _currency(currency, "brief.budget.currency") if currency is not None else None


def _client_quote(value: Any, path: str, default_currency: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _mapping(value, path)
    allowed = {"label", "amount", "currency", "status", "valid_until", "source_refs", "evidence_refs"}
    if set(row) - allowed:
        raise ValueError(f"{path} contains unsupported client-quote fields")
    amount = _nonnegative(row.get("amount"), f"{path}.amount")
    currency = _currency(row.get("currency"), f"{path}.currency", default_currency)
    if currency is None:
        raise ValueError(f"{path}.currency is required")
    status = row.get("status", "quote")
    if not isinstance(status, str) or status not in {"quote", "estimate", "committed"}:
        raise ValueError(f"{path}.status must describe a client-facing quote, estimate or commitment")
    label = _optional_text(row.get("label"), f"{path}.label") or "对客报价"
    valid_until = _optional_text(row.get("valid_until"), f"{path}.valid_until")
    refs = _refs(row, path)
    return {
        "label": label,
        "amount": _json_number(amount),
        "currency": currency,
        "status": status,
        "valid_until": valid_until,
        **refs,
    }


def _client_visibility(
    row: dict[str, Any],
    path: str,
    amount: Decimal | None,
    currency: str | None,
    status: str,
    *,
    inherited_visible: bool = False,
    inherited_quote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    visible = row.get("client_visible", inherited_visible)
    if type(visible) is not bool:
        raise ValueError(f"{path}.client_visible must be boolean")
    client_quote = _client_quote(
        row.get("client_quote"),
        f"{path}.client_quote",
        currency,
    ) or inherited_quote
    if client_quote is not None:
        return client_quote
    if not visible:
        return None
    if amount is None or currency is None or status not in {"quote", "committed"}:
        raise ValueError(
            f"{path}.client_visible requires a known quote/commitment or an explicit client_quote"
        )
    label = _optional_text(row.get("client_label"), f"{path}.client_label") or "对客报价"
    return {
        "label": label,
        "amount": _json_number(amount),
        "currency": currency,
        "status": status,
        "valid_until": row.get("valid_until"),
        **_refs(row, path),
    }


def _quote_index(value: Any, default_currency: str | None) -> dict[str, dict[str, Any]]:
    if value is None:
        value = []
    rows = _rows(value, "quotes")
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        path = f"quotes[{index}]"
        amount = _nonnegative(row.get("amount"), f"{path}.amount")
        state = row.get("status", "quote")
        if not isinstance(state, str) or state not in _QUOTE_STATES:
            raise ValueError(f"{path}.status is invalid")
        currency = _currency(row.get("currency"), f"{path}.currency", default_currency)
        if currency is None:
            raise ValueError(f"{path}.currency is required when no brief currency is set")
        quantity = _positive(row.get("quantity", 1), f"{path}.quantity")
        client_visible = row.get("client_visible", False)
        if type(client_visible) is not bool:
            raise ValueError(f"{path}.client_visible must be boolean")
        client_quote = _client_quote(row.get("client_quote"), f"{path}.client_quote", default_currency)
        if client_visible and (state not in {"quote", "committed"} or amount is None):
            raise ValueError(f"{path}.client_visible requires a known client-facing quote or commitment")
        result[row["id"]] = {
            "id": row["id"],
            "amount": amount * quantity,
            "currency": currency,
            "status": state,
            "client_visible": client_visible,
            "client_quote": client_quote,
            "valid_until": row.get("valid_until"),
            **_refs(row, path),
        }
        if row.get("valid_until") is not None:
            _optional_text(row["valid_until"], f"{path}.valid_until")
    return result


def _budget_context(brief: dict[str, Any]) -> dict[str, Any]:
    budget = brief.get("budget") or {}
    maximum = budget.get("maximum", budget.get("hard_limit"))
    reserve = budget.get("reserve", 0)
    currency = _currency(budget.get("currency"), "brief.budget.currency", brief.get("currency"))
    maximum_value = _nonnegative(maximum, "brief.budget.maximum") if maximum is not None else None
    reserve_value = _nonnegative(reserve, "brief.budget.reserve")
    return {
        "maximum": maximum_value,
        "reserve": reserve_value,
        "target": _nonnegative(budget["target"], "brief.budget.target") if budget.get("target") is not None else None,
        "currency": currency,
        "status": "LIMIT_DECLARED" if maximum_value is not None else "LIMIT_UNKNOWN",
    }


def _cost_lines(
    rows: Any,
    path: str,
    quotes: dict[str, dict[str, Any]],
    default_currency: str | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Decimal]], bool]:
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError(f"{path} must be a list")
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unknown = False
    for index, raw in enumerate(rows):
        line = _mapping(raw, f"{path}[{index}]")
        inherited_visible = False
        inherited_client_quote = None
        line_id = line.get("id", f"{index + 1}")
        if not isinstance(line_id, str) or not line_id.strip() or line_id in seen:
            raise ValueError(f"{path}[{index}].id must be unique non-empty text")
        seen.add(line_id)
        quote_id = line.get("quote_id")
        if quote_id is not None:
            if line.get("amount") is not None:
                raise ValueError(f"{path}[{index}] must use either quote_id or amount")
            quote_id = _required_text(quote_id, f"{path}[{index}].quote_id")
            if quote_id not in quotes:
                raise ValueError(f"{path}[{index}].quote_id does not match a supplied quote")
            quote = quotes[quote_id]
            amount = quote["amount"]
            currency = quote["currency"]
            status = quote["status"]
            valid_until = quote.get("valid_until")
            inherited_visible = quote.get("client_visible", False)
            inherited_client_quote = quote.get("client_quote")
            refs = {key: deepcopy(quote[key]) for key in ("source_refs", "evidence_refs", "basis_status")}
        elif line.get("amount") is not None:
            quantity = _positive(line.get("quantity", 1), f"{path}[{index}].quantity")
            amount = _nonnegative(line["amount"], f"{path}[{index}].amount") * quantity
            currency = _currency(line.get("currency"), f"{path}[{index}].currency", default_currency)
            if currency is None:
                unknown = True
                client_quote = _client_visibility(
                    line,
                    f"{path}[{index}]",
                    amount,
                    None,
                    line.get("status", "UNPRICED"),
                    inherited_visible=inherited_visible,
                    inherited_quote=inherited_client_quote,
                )
                parsed.append(
                    {
                        "id": line_id,
                        "amount": _json_number(amount),
                        "currency": None,
                        "status": "UNPRICED",
                        "client_visible": client_quote is not None,
                        "client_quote": client_quote,
                        **_refs(line, f"{path}[{index}]"),
                    }
                )
                continue
            status = line.get("status", "estimate")
            if not isinstance(status, str) or status not in _QUOTE_STATES:
                raise ValueError(f"{path}[{index}].status is invalid")
            valid_until = line.get("valid_until")
            refs = _refs(line, f"{path}[{index}]")
        else:
            unknown = True
            client_quote = _client_visibility(
                line,
                f"{path}[{index}]",
                None,
                None,
                "UNKNOWN",
                inherited_visible=inherited_visible,
                inherited_quote=inherited_client_quote,
            )
            parsed.append(
                {
                    "id": line_id,
                    "amount": None,
                    "currency": None,
                    "status": "UNKNOWN",
                    "client_visible": client_quote is not None,
                    "client_quote": client_quote,
                    **_refs(line, f"{path}[{index}]"),
                }
            )
            continue
        client_quote = _client_visibility(
            line,
            f"{path}[{index}]",
            amount,
            currency,
            status,
            inherited_visible=inherited_visible,
            inherited_quote=inherited_client_quote,
        )
        totals[currency][status] += amount
        parsed.append(
            {
                "id": line_id,
                "amount": _json_number(amount),
                "currency": currency,
                "status": status,
                "client_visible": client_quote is not None,
                "client_quote": client_quote,
                "quote_id": quote_id,
                "valid_until": valid_until,
                **refs,
            }
        )
    return parsed, totals, unknown


def _cost_summary(totals: dict[str, dict[str, Decimal]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for currency in sorted(totals):
        states = totals[currency]
        result.append(
            {
                "currency": currency,
                "amounts_by_status": {state: _json_number(states[state]) for state in sorted(states)},
                "planning_exposure": _json_number(
                    sum((states[state] for state in ("quote", "estimate", "committed")), Decimal(0))
                ),
                "actual": _json_number(states.get("actual", Decimal(0))),
                "refund": _json_number(states.get("refund", Decimal(0))),
            }
        )
    return result


def _cost_markdown(lines: list[dict[str, Any]]) -> str:
    if not lines:
        return "费用明细：未提供，金额未知。"
    rendered = ["| 项目 | 状态 | 金额 | 币种 |", "|---|---|---:|---|"]
    for index, line in enumerate(lines, 1):
        amount = line.get("amount")
        label = line.get("label") or line.get("description") or line.get("title") or f"费用项 {index}"
        rendered.append(
            f"| {label} | {_quote_status_label(line['status'])} | "
            f"{amount if amount is not None else '未知'} | "
            f"{line.get('currency') or '未知'} |"
        )
    return "\n".join(rendered)


def _client_cost_markdown(lines: list[dict[str, Any]]) -> str:
    quotes = [
        line["client_quote"]
        for line in lines
        if isinstance(line.get("client_quote"), dict)
    ]
    if not quotes:
        return "未提供明确标记的对客报价；客户版不披露供应商底价、代理成本或内部估算。"
    rendered = ["| 对客项目 | 金额 | 状态 | 有效期 |", "|---|---:|---|---|"]
    for quote in quotes:
        rendered.append(
            f"| {quote['label']} | {quote['amount']} {quote['currency']} | "
            f"{_quote_status_label(quote['status'])} | {quote.get('valid_until') or '未提供'} |"
        )
    return "\n".join(rendered) + "\n\n对客金额为用户明确提供的报价/估计字段；不由内部成本反推。"


def _client_budget_header(brief: dict[str, Any]) -> str:
    budget = _brief_data(brief)["budget"]
    if budget["maximum"] is None and budget["target"] is None:
        return "客户预算：未提供。"
    return (
        f"客户预算口径：上限 {budget['maximum'] if budget['maximum'] is not None else '未知'}；"
        f"目标 {budget['target'] if budget['target'] is not None else '未知'}；"
        f"币种 {budget['currency'] or '未知'}。"
    )


def _client_citations(inputs: dict[str, Any]) -> list[dict[str, str]]:
    rows = inputs.get("client_citations", [])
    if not isinstance(rows, list):
        raise ValueError("client_citations must be a list of explicitly client-approved citations")
    result = []
    for index, value in enumerate(rows):
        row = _mapping(value, f"client_citations[{index}]")
        material_id = _required_text(row.get("material_id"), f"client_citations[{index}].material_id")
        segment_id = _required_text(row.get("segment_id"), f"client_citations[{index}].segment_id")
        quote = _required_text(row.get("quote"), f"client_citations[{index}].quote")
        result.append({"material_id": material_id, "segment_id": segment_id, "quote": quote})
    return result


def _client_references_authorized(item: dict[str, Any], citations: list[dict[str, str]]) -> bool:
    allowed = {
        (citation["material_id"], citation["segment_id"], citation["quote"])
        for citation in citations
    }
    for reference in (item.get("source_refs") or []) + (item.get("evidence_refs") or []):
        if not isinstance(reference, dict):
            continue
        marker = (
            reference.get("material_id"),
            reference.get("segment_id"),
            reference.get("quote"),
        )
        if marker in allowed:
            return True
    return False


def _quote_status_label(value: Any) -> str:
    return {
        "quote": "报价",
        "estimate": "估计",
        "committed": "已承诺",
        "actual": "实支记录",
        "refund": "退款记录",
        "UNPRICED": "未定价",
        "UNKNOWN": "未知",
    }.get(value, "状态待核")


def _cancellation_status_label(value: Any) -> str:
    return {
        "scenario": "取消情景试算",
        "requested": "用户记录已请求取消",
        "user_recorded": "用户记录的取消状态",
        "unknown": "取消状态未知",
    }.get(value, "取消状态待核")


def _budget_markdown(check: dict[str, Any]) -> str:
    status_labels = {
        "WITHIN_LIMIT": "已列计划金额连同预留未超硬上限；不代表已经批准或承诺",
        "OVER_LIMIT": "已列计划金额连同预留超出硬上限",
        "LIMIT_UNKNOWN": "未提供硬上限，暂不能判断预算可行性",
        "CURRENCY_UNKNOWN": "预算币种未知，无法与硬上限比较",
        "PARTIAL_COST_UNKNOWN": "存在未报价/金额未知费用，预算结论不完整",
        "FOREIGN_CURRENCY_UNASSESSED": "含其他币种，未进行汇率换算",
        "NO_TASKS_TO_PRICE": "没有提供任务费用行，暂未核算预算",
    }
    fields = [
        f"- 预算判断：{status_labels.get(check['status'], '需要补充预算依据')}",
        f"- 硬上限：{check.get('maximum') if check.get('maximum') is not None else '未知'} {check.get('currency') or ''}",
        f"- 预留：{check.get('reserve', 0)} {check.get('currency') or ''}",
    ]
    if "existing_committed" in check:
        fields.extend(
            [
                f"- 已承诺：{check['existing_committed']} {check.get('currency') or ''}",
                f"- 扣除预留与承诺后的可用额度：{check['available_before_scenario']} {check.get('currency') or ''}",
                f"- 本方案报价/估计/承诺：{check['scenario_planning_exposure']} {check.get('currency') or ''}",
                f"- 本方案含预留总额：{check['total_exposure_including_reserve']} {check.get('currency') or ''}",
            ]
        )
    if check.get("unknown_cost_lines"):
        fields.append("- 未报价或金额未知的费用行仍保留未知。")
    foreign = check.get("foreign_currency_costs_not_converted", [])
    if foreign:
        fields.append("- 未换算币种：" + ", ".join(foreign))
    for bucket in check.get("scenario_costs", []):
        states = ", ".join(
            f"{state} {amount}"
            for state, amount in bucket["amounts_by_status"].items()
        )
        fields.append(
            f"- {bucket['currency']} 分状态小计：{states or '未提供费用行'}；"
            f"实支 {bucket['actual']}，退款 {bucket['refund']}"
        )
    return "\n".join(fields)


def _contactability_label(value: Any) -> str:
    return {
        "NOT_CONTACTABLE": "当前不可联系",
        "GRANT_SCOPE_OR_EXPIRY_INCOMPLETE": "许可用途、渠道、范围或期限不完整",
        "GRANT_EXPIRED_FOR_PROJECT": "用户记录的许可在项目开始前已过期",
        "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK": "用户记录已同意，实际联系前仍需重新核权限",
    }.get(value, "许可状态未知")


def _member_channel_label(value: str) -> str:
    labels = {
        "app_inbox": "应用内消息",
        "app_push": "应用推送",
        "push": "应用推送",
        "sms": "短信",
        "email": "电子邮件",
        "wechat_service": "微信服务通知",
        "wechat": "微信消息",
        "line": "LINE消息",
        "whatsapp": "WhatsApp消息",
        "instagram": "Instagram消息",
        "xhs": "小红书站内消息",
        "rednote": "小红书站内消息",
    }
    return labels.get(value.casefold(), value)


def _observation_rate_label(value: Any) -> str:
    return {
        "USER_DECLARED_SHARED_COHORT": "按用户声明的同一群组计算",
        "COHORT_NOT_CONFIRMED": "群组是否相同未确认",
        "DENOMINATOR_UNKNOWN": "分母未知",
        "ZERO_DENOMINATOR": "分母为零，未计算比率",
        "DENOMINATOR_NOT_NUMERIC": "分母不是数值，未计算比率",
        "IDENTITY_OR_WINDOW_INCOMPLETE": "账号身份或观察窗口不完整",
    }.get(value, "比率口径未确认")


def _media_comparison_label(value: Any) -> str:
    return {
        "COMPARABLE_PRICE_INPUTS": "同口径报价可比较",
        "NOT_COMPARABLE": "口径不同或关键口径缺失，不作价格比较",
        "COMPARABLE_BASIS_PRICE_UNKNOWN": "比较口径相同，但价格未知",
        "COMPARABLE_BASIS_CURRENCY_MISMATCH": "比较口径相同，但币种不同，未换算",
        "COMPARABLE_BASIS_DIFFERENT_PRICE_STATES": "比较口径相同，但金额状态不同",
        "COMPARABLE_BASIS_NOT_PLANNING_PRICE": "不是报价/估计/承诺金额，未作计划价比较",
    }.get(value, "尚未形成可比价格结论")


def _media_basis_label(value: Any) -> str:
    return {
        "channel": "渠道",
        "account_id": "账号或采购链",
        "placement": "版位",
        "market": "市场/地域",
        "audience_scope": "受众范围",
        "start_date": "开始日期",
        "end_date": "结束日期",
        "pricing_basis": "计价口径",
        "metric_definition": "指标定义",
        "denominator": "指标分母",
        "observation_window": "观察窗口",
        "data_rights": "数据使用权",
        "exclusions": "排除条件",
    }.get(value, "比较条件")


def _schedule_status_label(value: Any) -> str:
    return {
        "READY_TO_REVIEW": "当前输入条件下已排入，可供业务审阅",
        "FEASIBLE_NOT_OPTIMAL": "已找到可行安排，但未证明最优",
        "FEASIBLE_FALLBACK_UNOPTIMIZED": "保留可行试排；优化器未完成最优性证明",
        "INFEASIBLE_DECLARED_CONSTRAINTS": "已提供的约束组合不可行，锁定安排保留待处理",
        "CONFLICTS_OR_GAPS": "存在冲突或缺口，当前不能视为可执行",
        "NOT_ASSESSED_NO_TASKS": "仅整理内容/日历，未计算制作任务排期",
    }.get(value, "排期状态待核")


def _optimizer_state_label(value: Any) -> str:
    return {
        "OPTIMAL": "连续时间混合整数优化已证明最优",
        "FEASIBLE_NOT_OPTIMAL": "找到可行MILP安排，但未证明最优",
        "INFEASIBLE": "在已声明模型约束下证明不可行",
        "INPUTS_INCOMPLETE": "关键资源/工时输入未知，未运行优化",
        "SOLVER_UNAVAILABLE": "优化器不可用，保留约束试排结果",
        "LIMIT_REACHED_NO_INCUMBENT": "优化到时限仍无经核验方案；未判定无解",
        "NO_TASKS": "本次没有制作任务，未运行排期优化",
        "INDEPENDENT_CHECK_FAILED_FALLBACK": "优化结果未通过独立约束核验，改用保守试排",
    }.get(value, "优化状态待核")


def _optimizer_objective_label(value: Any) -> str:
    return {
        "MINIMUM_PROJECT_COMPLETION_TIME_PROVEN": "最小化项目完成时间",
        "MINIMUM_PROJECT_COMPLETION_TIME": "最小化项目完成时间",
        "OPTIMAL_USER_DECLARED_BENEFIT_THEN_MINIMUM_COMPLETION_TIME": "先最大化用户声明的业务价值，再最小化项目完成时间",
        "FEASIBLE_USER_DECLARED_BENEFIT_NOT_TIME_OPTIMAL": "按用户声明的业务价值选择任务；排期时间未证明最优",
        "MAXIMIZE_USER_DECLARED_BENEFIT_NOT_PROVEN": "最大化用户声明的业务价值，但最优性未证明",
        "FEASIBLE_SCHEDULE_TIME_OPTIMALITY_NOT_PROVEN": "最小化项目完成时间，但最优性未证明",
    }.get(value, "未运行可声明的优化目标")


def _policy_window_label(value: Any) -> str:
    return {
        "within_declared_window": "在用户提供的取消窗口内",
        "after_declared_window": "晚于用户提供的取消窗口",
        "window_unknown": "取消截止窗口未知",
    }.get(value, "取消窗口待核")


def _consent_status_label(value: Any) -> str:
    return {
        "granted": "用户记录已同意",
        "denied": "用户记录拒绝",
        "withdrawn": "用户记录已撤回",
        "opted_out": "用户记录已退订",
        "unknown": "同意状态未知",
    }.get(value, "同意状态未知")


def _permission_label(value: Any) -> str:
    if value is True:
        return "允许"
    if value is False:
        return "不允许"
    return "未知/待确认"


def _asset_rights_status_label(value: Any) -> str:
    return {
        "confirmed": "用户记录已确认",
        "pending": "待确认",
        "denied": "不允许",
        "unknown": "未知",
    }.get(value, "状态待核")


def _source_kind_label(value: Any) -> str:
    return {
        "platform_native": "平台原生观测",
        "user_recorded": "用户记录",
        "third_party": "第三方材料",
        "manual": "手工记录",
        "UNKNOWN": "来源类型未知",
    }.get(value, "用户提供来源类型")


def _task_status_label(value: Any) -> str:
    return {
        "PLANNED": "本次试排",
        "PRESERVED_LOCKED": "保留已锁定安排",
        "NOT_SCHEDULED": "未排入当前计划",
        "NOT_SELECTED_BY_OBJECTIVE": "按用户提供的价值与约束未纳入本次计划",
    }.get(value, "任务安排待核")


def _content_link_label(value: Any) -> str:
    return {
        "UNLINKED_TASK_TARGET": "未关联具体内容对象",
        "INCOMPLETE_TASK_TARGET_REFERENCE": "内容对象引用不完整",
        "LINKED_TO_OBJECT_IN_INPUT_SNAPSHOT": "已关联本次内容对象快照",
        "REFERENCE_NOT_INCLUDED_IN_INPUT_SNAPSHOT": "引用对象不在本次快照中，待核",
    }.get(value, "对象引用状态待核")


def _replace_object_ids(text: str, labels: dict[str, str]) -> str:
    for identity in sorted(labels, key=len, reverse=True):
        text = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(identity)}(?![A-Za-z0-9])",
            labels[identity],
            text,
        )
    return text


def _budget_check(
    budget: dict[str, Any],
    totals: dict[str, dict[str, Decimal]],
    *,
    unknown_cost: bool,
    existing_committed: Decimal = Decimal(0),
) -> dict[str, Any]:
    currency = budget["currency"]
    maximum = budget["maximum"]
    reserve = budget["reserve"]
    result: dict[str, Any] = {
        "currency": currency,
        "maximum": _json_number(maximum) if maximum is not None else None,
        "reserve": _json_number(reserve),
        "target": _json_number(budget["target"]) if budget["target"] is not None else None,
        "scenario_costs": _cost_summary(totals),
        "unknown_cost_lines": unknown_cost,
        "status": "LIMIT_UNKNOWN" if maximum is None else "WITHIN_LIMIT",
    }
    if maximum is None:
        return result
    if currency is None:
        result["status"] = "CURRENCY_UNKNOWN"
        return result
    in_currency = totals.get(currency, {})
    planned = sum(
        (in_currency.get(state, Decimal(0)) for state in ("quote", "estimate", "committed")),
        Decimal(0),
    )
    available = maximum - reserve - existing_committed
    total_exposure = planned + reserve + existing_committed
    result.update(
        {
            "existing_committed": _json_number(existing_committed),
            "available_before_scenario": _json_number(available),
            "scenario_planning_exposure": _json_number(planned),
            "total_exposure_including_reserve": _json_number(total_exposure),
            "foreign_currency_costs_not_converted": sorted(set(totals) - {currency}),
        }
    )
    if unknown_cost:
        result["status"] = "PARTIAL_COST_UNKNOWN"
    elif total_exposure > maximum:
        result["status"] = "OVER_LIMIT"
    elif set(totals) - {currency}:
        result["status"] = "FOREIGN_CURRENCY_UNASSESSED"
    return result


def _existing_committed(
    quotes: dict[str, dict[str, Any]],
    *,
    exclude: set[str] | None = None,
    currency: str | None = None,
) -> Decimal:
    exclude = exclude or set()
    return sum(
        (
            row["amount"]
            for identity, row in quotes.items()
            if identity not in exclude and row["status"] == "committed" and row["currency"] == currency
        ),
        Decimal(0),
    )


def _brief_facts(brief: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(brief.get("facts", [])):
        result.append(
            {
                "id": row.get("id"),
                "text": row.get("text"),
                "scope": row.get("scope"),
                "status": "user_supplied_fact_not_independently_verified",
                **_refs(row, f"brief.facts[{index}]"),
            }
        )
    return result


def _brief_data(brief: dict[str, Any]) -> dict[str, Any]:
    budget = _budget_context(brief)
    return {
        "goal": brief.get("goal"),
        "client_title": brief.get("client_title"),
        "audience": brief.get("audience"),
        "market": brief.get("market"),
        "language": brief.get("language"),
        "start_date": brief.get("start_date"),
        "end_date": brief.get("end_date"),
        "timezone": brief.get("timezone"),
        "facts": _brief_facts(brief),
        "constraints": deepcopy(brief.get("constraints", [])),
        "forbidden_claims": deepcopy(brief.get("forbidden_claims", [])),
        "budget": {
            "maximum": _json_number(budget["maximum"]) if budget["maximum"] is not None else None,
            "target": _json_number(budget["target"]) if budget["target"] is not None else None,
            "reserve": _json_number(budget["reserve"]),
            "currency": budget["currency"],
            "status": budget["status"],
        },
        **_refs(brief, "brief"),
    }


def _section(
    identity: str,
    title: str,
    body: str,
    data: dict[str, Any],
    *,
    client_body: str | None = None,
) -> dict[str, Any]:
    return {
        "id": identity,
        "title": title,
        "body": body.strip(),
        "client_body": client_body.strip() if isinstance(client_body, str) else None,
        "data": data,
    }


def _fact_lines(brief: dict[str, Any]) -> list[str]:
    facts = brief.get("facts", [])
    if not facts:
        return ["- 尚未提供可引用的事实；策略与主张不能据此补写。"]
    lines = []
    for index, row in enumerate(facts, 1):
        text = row.get("text") or "（未提供事实描述）"
        refs = list(row.get("source_refs", []) or []) + list(row.get("evidence_refs", []) or [])
        label = f"事实 {index}: "
        suffix = f"；引用（本次未独立核验）：{', '.join(map(_reference_label, refs))}" if refs else "；用户输入，未独立核验"
        lines.append(f"- {label}{text}{suffix}")
    return lines


def _reference_label(reference: Any) -> str:
    if isinstance(reference, dict):
        if reference.get("material_id") and reference.get("segment_id"):
            return "已选择保存资料原文片段"
        if reference.get("artifact_id") and reference.get("artifact_version"):
            return f"已选择保存成果版本 {reference['artifact_version']}"
        return "已提供结构化来源引用"
    return str(reference)


def _reference_markdown(item: dict[str, Any]) -> str:
    sources = item.get("source_refs") or []
    evidence = item.get("evidence_refs") or []
    if not sources and not evidence:
        return "用户输入，未独立核验"
    parts = []
    if sources:
        parts.append("来源：" + ", ".join(map(_reference_label, sources)))
    if evidence:
        parts.append("依据：" + ", ".join(map(_reference_label, evidence)))
    return "；".join(parts) + "（本次未独立核验）"


def _deliverable_index(value: Any) -> dict[str, dict[str, Any]]:
    rows = _rows(value or [], "deliverables")
    return {row["id"]: row for row in rows}


def _resource_index(value: Any) -> dict[str, dict[str, Any]]:
    rows = _rows(value or [], "resources")
    result = {}
    for index, row in enumerate(rows):
        capacity = row.get("capacity")
        if capacity is not None and (type(capacity) is not int or capacity < 1):
            raise ValueError(f"resources[{index}].capacity must be a positive integer")
        qualifications = row.get("qualifications", [])
        if not isinstance(qualifications, list) or any(not isinstance(x, str) for x in qualifications):
            raise ValueError(f"resources[{index}].qualifications must be a list of text")
        availability = row.get("availability")
        if availability is not None and not isinstance(availability, list):
            raise ValueError(f"resources[{index}].availability must be a list of time windows")
        if availability is not None:
            for window_index, raw_window in enumerate(availability):
                window = _mapping(raw_window, f"resources[{index}].availability[{window_index}]")
                _required_text(window.get("start"), f"resources[{index}].availability[{window_index}].start")
                _required_text(window.get("end"), f"resources[{index}].availability[{window_index}].end")
        result[row["id"]] = {**row, "capacity": capacity}
    return result


def _candidate_cost(
    row: dict[str, Any],
    path: str,
    quotes: dict[str, dict[str, Any]],
    default_currency: str | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Decimal]], bool]:
    parsed, totals, unknown = _cost_lines(row.get("cost_lines", []), f"{path}.cost_lines", quotes, default_currency)
    has_planning_amount = any(
        line["status"] in {"quote", "estimate", "committed"}
        for line in parsed
    )
    return parsed, totals, unknown or not has_planning_amount


def _candidate_time_gap(candidate: dict[str, Any], brief: dict[str, Any], path: str) -> str | None:
    if candidate.get("duration_days") is None:
        return None
    duration = _positive(candidate["duration_days"], f"{path}.duration_days")
    if brief.get("start_date") is None or brief.get("end_date") is None:
        return "未同时提供项目起止期，无法核该路线周期。"
    start = _parse_time(brief["start_date"], "brief.start_date", brief.get("timezone"))
    end = _parse_time(brief["end_date"], "brief.end_date", brief.get("timezone"), end_of_date=True)
    available_days = Decimal(str((end - start).total_seconds())) / Decimal(86400)
    if duration > available_days:
        return f"路线需 {duration} 日，可用窗口约 {available_days.normalize()} 日。"
    return None


def _mechanism_path(candidate: dict[str, Any], path: str) -> tuple[dict[str, Any], list[str]]:
    raw = candidate.get("mechanism_path", {})
    raw = _mapping(raw, f"{path}.mechanism_path")
    result = {}
    missing = []
    for field in _MECHANISM_FIELDS:
        value = _optional_text(raw.get(field), f"{path}.mechanism_path.{field}")
        result[field] = value
        if value is None:
            missing.append(field)
    return result, missing


def _audience_segments(candidate: dict[str, Any], path: str) -> list[dict[str, Any]]:
    rows = _rows(candidate.get("audiences", []), f"{path}.audiences")
    result = []
    for index, row in enumerate(rows):
        item_path = f"{path}.audiences[{index}]"
        label = _required_text(row.get("label"), f"{item_path}.label")
        for field in ("need", "behavior_basis", "alternative", "barrier"):
            if row.get(field) is not None:
                _optional_text(row[field], f"{item_path}.{field}")
        result.append(
            {
                **deepcopy(row),
                "label": label,
                "basis_status": _refs(row, item_path)["basis_status"],
            }
        )
    return result


def _case_basis(candidate: dict[str, Any], path: str) -> tuple[dict[str, Any] | None, list[str]]:
    raw = candidate.get("case_basis")
    supplied_assets = candidate.get("asset_uses")
    if raw is None and supplied_assets is None:
        return None, []
    case = _mapping(raw or {}, f"{path}.case_basis")
    if supplied_assets is not None:
        if "asset_uses" in case:
            raise ValueError(f"{path} must provide asset_uses in one place")
        case = {**case, "asset_uses": supplied_assets}
    assets = _rows(case.get("asset_uses", []), f"{path}.case_basis.asset_uses")
    gaps = []
    asset_uses = []
    for index, asset in enumerate(assets):
        asset_path = f"{path}.case_basis.asset_uses[{index}]"
        asset_id = _required_text(asset.get("asset_id"), f"{asset_path}.asset_id")
        rights_status = asset.get("rights_status", "unknown")
        if not isinstance(rights_status, str) or rights_status not in {
            "confirmed",
            "pending",
            "denied",
            "unknown",
        }:
            raise ValueError(f"{asset_path}.rights_status is invalid")
        refs = _refs(asset, asset_path)
        right_refs = asset.get("rights_refs", [])
        if right_refs is not None and not isinstance(right_refs, list):
            raise ValueError(f"{asset_path}.rights_refs must be a list")
        use_scope = _optional_text(asset.get("use_scope"), f"{asset_path}.use_scope")
        territories = asset.get("territories", [])
        if not isinstance(territories, list) or any(not isinstance(item, str) for item in territories):
            raise ValueError(f"{asset_path}.territories must be a list of places")
        term_days = asset.get("term_days")
        if term_days is not None:
            _positive(term_days, f"{asset_path}.term_days")
        missing_rights_terms = []
        if use_scope is None:
            missing_rights_terms.append("用途")
        if not territories:
            missing_rights_terms.append("地域")
        if term_days is None and not (asset.get("term_start") and asset.get("term_end")):
            missing_rights_terms.append("期限")
        if rights_status != "confirmed" or not right_refs or missing_rights_terms:
            gaps.append(f"案例素材 {asset_id} 的本案使用权未获充分确认。")
        asset_uses.append(
            {
                **deepcopy(asset),
                "asset_id": asset_id,
                "use_scope": use_scope,
                "territories": deepcopy(territories),
                "term_days": term_days,
                "missing_rights_terms": missing_rights_terms,
                "rights_status": rights_status,
                "rights_refs": deepcopy(right_refs),
                "rights_basis_status": "user_supplied_rights_refs_not_independently_verified"
                if right_refs
                else "rights_reference_not_provided",
            }
        )
    for field in (
        "non_transferable_conditions",
        "adoption_changes",
        "channels",
        "production_resources",
        "cost_drivers",
        "counterexamples",
    ):
        value = case.get(field, [])
        if not isinstance(value, list):
            raise ValueError(f"{path}.case_basis.{field} must be a list")
    if case.get("reported_result") is not None:
        _optional_text(case["reported_result"], f"{path}.case_basis.reported_result")
    result = {
        **deepcopy(case),
        "asset_uses": asset_uses,
        "basis_status": _refs(case, f"{path}.case_basis")["basis_status"],
        "reported_result_status": (
            "user_reported_not_a_guarantee"
            if case.get("reported_result") is not None
            else "unknown"
        ),
    }
    return result, gaps


def _compare_mechanism_paths(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            different_fields = []
            shared_fields = []
            complete = True
            for field in _MECHANISM_FIELDS:
                left = first["mechanism_path"][field]
                right = second["mechanism_path"][field]
                if left is None or right is None:
                    complete = False
                elif _normalize_phrase(left) == _normalize_phrase(right):
                    shared_fields.append(field)
                else:
                    different_fields.append(field)
            if different_fields:
                status = "STATED_MECHANISM_PATH_DIFFERENCE"
            elif complete:
                status = "SAME_STATED_MECHANISM_PATH"
            else:
                same_text = _normalize_phrase(first["mechanism"]) == _normalize_phrase(second["mechanism"])
                status = "SAME_STATED_MECHANISM" if same_text else "TEXT_ONLY_DIFFERENCE_UNASSESSED"
            comparisons.append(
                {
                    "candidate_ids": [first["id"], second["id"]],
                    "status": status,
                    "different_components": different_fields,
                    "shared_components": shared_fields,
                }
            )
    return comparisons


def _normalize_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _strategy_resource_checks(
    candidate: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    brief: dict[str, Any],
    path: str,
) -> list[dict[str, Any]]:
    resource_ids = candidate.get("resource_ids", [])
    if brief.get("start_date") is None or brief.get("end_date") is None:
        return [
            {
                "resource_id": identity,
                "status": "PROJECT_WINDOW_UNKNOWN",
                "available_windows": [],
                **_refs(resources[identity], f"resources.{identity}"),
            }
            for identity in resource_ids
            if identity in resources
        ]
    project_start = _parse_time(
        brief["start_date"], "brief.start_date", brief.get("timezone")
    )
    project_end = _parse_time(
        brief["end_date"],
        "brief.end_date",
        brief.get("timezone"),
        end_of_date=True,
    )
    if project_end <= project_start:
        raise ValueError("brief.end_date must follow brief.start_date")
    results = []
    for identity in resource_ids:
        resource = resources.get(identity)
        if resource is None:
            continue
        intervals = resource.get("availability")
        windows: list[tuple[str, str]] = []
        if intervals:
            for index, value in enumerate(intervals):
                interval = _mapping(
                    value,
                    f"resources.{identity}.availability[{index}]",
                )
                start = _parse_time(
                    interval.get("start"),
                    f"resources.{identity}.availability[{index}].start",
                    brief.get("timezone"),
                )
                end = _parse_time(
                    interval.get("end"),
                    f"resources.{identity}.availability[{index}].end",
                    brief.get("timezone"),
                    end_of_date=True,
                )
                if end <= start:
                    raise ValueError(
                        f"resources.{identity}.availability[{index}].end must follow start"
                    )
                windows.append((start.isoformat(), end.isoformat()))
        elif resource.get("available_from") and resource.get("available_to"):
            start = _parse_time(
                resource["available_from"],
                f"resources.{identity}.available_from",
                brief.get("timezone"),
            )
            end = _parse_time(
                resource["available_to"],
                f"resources.{identity}.available_to",
                brief.get("timezone"),
                end_of_date=True,
            )
            if end <= start:
                raise ValueError(
                    f"resources.{identity}.available_to must follow available_from"
                )
            windows.append((start.isoformat(), end.isoformat()))
        elif resource.get("available_from") or resource.get("available_to"):
            for field in ("available_from", "available_to"):
                if resource.get(field):
                    _parse_time(
                        resource[field],
                        f"resources.{identity}.{field}",
                        brief.get("timezone"),
                    )
            results.append(
                {
                    "resource_id": identity,
                    "status": "INCOMPLETE_AVAILABILITY",
                    "available_windows": [],
                    **_refs(resource, f"resources.{identity}"),
                }
            )
            continue
        else:
            results.append(
                {
                    "resource_id": identity,
                    "status": "AVAILABILITY_UNKNOWN",
                    "available_windows": [],
                    **_refs(resource, f"resources.{identity}"),
                }
            )
            continue
        overlaps = []
        for start_text, end_text in windows:
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
            left = max(start, project_start)
            right = min(end, project_end)
            if left < right:
                overlaps.append({"start": left.isoformat(), "end": right.isoformat()})
        results.append(
            {
                "resource_id": identity,
                "status": (
                    "OVERLAPS_BRIEF_WINDOW_NOT_SCHEDULED"
                    if overlaps
                    else "NO_OVERLAP_WITH_BRIEF_WINDOW"
                ),
                "available_windows": windows,
                "brief_window_overlaps": overlaps,
                **_refs(resource, f"resources.{identity}"),
            }
        )
    return results


def _evaluate_strategy(
    brief: dict[str, Any],
    inputs: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    *,
    revision_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = _rows(inputs.get("candidates"), "candidates")
    if not candidates:
        raise ValueError("strategy_proposal requires at least one concrete candidate")
    deliverables = _deliverable_index(inputs.get("deliverables", []))
    resources = _resource_index(inputs.get("resources", []))
    budget = _budget_context(brief)
    evaluated = []
    limitations: list[str] = []
    strategy_revision_impact = (
        deepcopy(revision_context)
        if isinstance(revision_context, dict)
        else None
    )
    for index, candidate in enumerate(candidates):
        path = f"candidates[{index}]"
        title = _required_text(candidate.get("title"), f"{path}.title")
        mechanism = _required_text(candidate.get("mechanism"), f"{path}.mechanism")
        mechanism_path, missing_mechanism = _mechanism_path(candidate, path)
        audiences = _audience_segments(candidate, path)
        case_basis, case_gaps = _case_basis(candidate, path)
        lines, totals, unknown_cost = _candidate_cost(candidate, path, quotes, budget["currency"])
        missing: list[str] = []
        if not candidate.get("audience_need"):
            missing.append("目标受众的真实需求或行为依据")
        if not candidate.get("barrier"):
            missing.append("本案要解决的具体障碍")
        if not candidate.get("brand_reason"):
            missing.append("为什么由该品牌承担此机制")
        if not candidate.get("actions"):
            missing.append("从策略到交付的具体动作")
        if not candidate.get("validation"):
            missing.append("本案验证方法")
        if missing_mechanism:
            missing.append("机制路径待展开：" + ", ".join(missing_mechanism))
        if not audiences:
            missing.append("需列明具体受众细分、行为依据与替代选择")
        if case_gaps:
            missing.extend(case_gaps)
        deliverable_refs = candidate.get("deliverable_ids", [])
        if not isinstance(deliverable_refs, list) or any(not isinstance(value, str) for value in deliverable_refs):
            raise ValueError(f"{path}.deliverable_ids must be a list of IDs")
        missing_deliverables = [value for value in deliverable_refs if value not in deliverables]
        resource_refs = candidate.get("resource_ids", [])
        if not isinstance(resource_refs, list) or any(not isinstance(value, str) for value in resource_refs):
            raise ValueError(f"{path}.resource_ids must be a list of IDs")
        missing_resources = [value for value in resource_refs if value not in resources]
        resource_checks = _strategy_resource_checks(
            candidate,
            resources,
            brief,
            path,
        )
        unknown_capacity_resources = [
            value
            for value in resource_refs
            if value in resources and resources[value]["capacity"] is None
        ]
        if missing_deliverables:
            missing.append(f"有 {len(missing_deliverables)} 项交付物引用未匹配本次交付清单")
        if missing_resources:
            missing.append(f"有 {len(missing_resources)} 项资源引用未匹配本次资源清单")
        if unknown_capacity_resources:
            missing.append(f"有 {len(unknown_capacity_resources)} 项资源的可用容量未知")
        resource_window_gaps = [
            item
            for item in resource_checks
            if item["status"]
            in {
                "AVAILABILITY_UNKNOWN",
                "INCOMPLETE_AVAILABILITY",
                "NO_OVERLAP_WITH_BRIEF_WINDOW",
            }
        ]
        if resource_window_gaps:
            missing.append(
                f"有 {len(resource_window_gaps)} 项资源的项目期可用窗口未核定或不重叠"
            )
        duration_days = (
            _positive(candidate["duration_days"], f"{path}.duration_days")
            if candidate.get("duration_days") is not None
            else None
        )
        time_gap = _candidate_time_gap(candidate, brief, path)
        if time_gap:
            missing.append(time_gap)
        refs = _refs(candidate, path)
        for key in ("actions", "dependencies", "risks", "counterevidence", "transfer_conditions"):
            value = candidate.get(key, [])
            if value is not None and not isinstance(value, list):
                raise ValueError(f"{path}.{key} must be a list")
        check = _budget_check(
            budget,
            totals,
            unknown_cost=unknown_cost,
            existing_committed=_existing_committed(
                quotes,
                currency=budget["currency"],
                exclude={line["quote_id"] for line in lines if line.get("quote_id")},
            ),
        )
        if check["status"] == "OVER_LIMIT":
            missing.append("按当前报价/估计和预留金超出预算上限")
        elif check["status"] in {"PARTIAL_COST_UNKNOWN", "CURRENCY_UNKNOWN", "FOREIGN_CURRENCY_UNASSESSED"}:
            missing.append("存在未报价成本或币种，预算可行性未能完整判断")
        elif check["status"] == "LIMIT_UNKNOWN":
            missing.append("未提供预算硬上限，不能判断预算可行性")
        status = "CONDITIONALLY_FEASIBLE" if not missing else "NEEDS_RESOLUTION"
        if budget["maximum"] is None:
            status = "BUDGET_UNASSESSED" if not missing else "NEEDS_RESOLUTION"
        evaluated.append(
            {
                "id": candidate["id"],
                "title": title,
                "mechanism": mechanism,
                "mechanism_path": mechanism_path,
                "audiences": audiences,
                "case_basis": case_basis,
                "audience_need": candidate.get("audience_need"),
                "barrier": candidate.get("barrier"),
                "brand_reason": candidate.get("brand_reason"),
                "actions": deepcopy(candidate.get("actions", [])),
                "deliverable_ids": deepcopy(deliverable_refs),
                "resource_ids": deepcopy(resource_refs),
                "resource_checks": resource_checks,
                "dependencies": deepcopy(candidate.get("dependencies", [])),
                "risks": deepcopy(candidate.get("risks", [])),
                "counterevidence": deepcopy(candidate.get("counterevidence") or []),
                "transfer_conditions": deepcopy(candidate.get("transfer_conditions") or []),
                "validation": deepcopy(candidate.get("validation")),
                "expected_effect": deepcopy(candidate.get("expected_effect")),
                "duration_days": _json_number(duration_days) if duration_days is not None else None,
                "cost_lines": lines,
                "budget_check": check,
                "gaps": missing,
                "planning_status": status,
                **refs,
            }
        )
        if not refs["source_refs"] and not refs["evidence_refs"]:
            limitations.append(f"策略备选“{title}”及其商业依据来自用户输入，未独立核验。")
        if candidate.get("expected_effect") is not None:
            limitations.append(f"策略备选“{title}”的预期效果仅保留为方案假设，不是预测或保证。")

    duplicate_mechanisms: list[list[str]] = []
    by_mechanism: dict[str, list[str]] = defaultdict(list)
    for row in evaluated:
        normalized = re.sub(r"\s+", " ", row["mechanism"]).strip().casefold()
        by_mechanism[normalized].append(row["id"])
    for ids in by_mechanism.values():
        if len(ids) > 1:
            duplicate_mechanisms.append(ids)
    if duplicate_mechanisms:
        limitations.append("部分备选的机制表述完全相同；需说明真实的作用机制差异后再作为可比路线。")
    mechanism_comparisons = _compare_mechanism_paths(evaluated)
    if any(
        comparison["status"] == "TEXT_ONLY_DIFFERENCE_UNASSESSED"
        for comparison in mechanism_comparisons
    ):
        limitations.append("不同机制文案没有完整的作用路径结构；文字变化不证明实际机制不同。")
    if any(row["case_basis"] for row in evaluated):
        limitations.append("案例来源、已报效果和素材权利沿用户提供的引用保留，本次未独立核验；不据此保证本案结果。")
    if any(
        check["status"] == "OVERLAPS_BRIEF_WINDOW_NOT_SCHEDULED"
        for row in evaluated
        for check in row["resource_checks"]
    ):
        limitations.append("策略阶段的资源检查只说明与项目窗口有重叠，未分配工时或证明实际并行容量；执行排期仍需第6域试算。")
    if any(
        check["status"] == "PROJECT_WINDOW_UNKNOWN"
        for row in evaluated
        for check in row["resource_checks"]
    ):
        limitations.append("已选资源有可用时段，但Brief项目窗口未提供，策略阶段无法核其时间重叠。")
    limitations.extend(
        [
            "本次处理没有外部市场研究；没有来源的事实按用户陈述展示，不作为已证实依据。",
            "候选机制是否原创、适配及能否达成效果仍需适任业务评审和实际结果验证。",
            "本结果是待修改的规划草稿，不代表客户批准、费用承诺、合作联系或对外发布。",
        ]
    )
    strategy_body = [
        f"**业务目标：** {brief.get('goal') or '未提供'}  ",
        f"**受众：** {brief.get('audience') or '未提供'}；**市场：** {brief.get('market') or '未提供'}",
        "",
        "各路线依照输入的受众需求、业务障碍、品牌理由和执行动作展开；这里不替用户选择路线。",
    ]
    for row in evaluated:
        case = row["case_basis"] or {}
        deliverable_labels = [
            deliverables[identity].get("title") or "未命名交付物"
            for identity in row["deliverable_ids"]
            if identity in deliverables
        ]
        resource_labels = [
            f"{resources[identity].get('name') or '未命名资源'}（容量 "
            f"{resources[identity]['capacity'] if resources[identity]['capacity'] is not None else '未知'} "
            f"{resources[identity].get('unit') or ''}；资格 "
            f"{_display_list(resources[identity].get('qualifications', []))}）"
            for identity in row["resource_ids"]
            if identity in resources
        ]
        resource_status_labels = {
            "PROJECT_WINDOW_UNKNOWN": "项目时窗未提供，无法检查重叠",
            "AVAILABILITY_UNKNOWN": "可用时段未知",
            "INCOMPLETE_AVAILABILITY": "可用时段未完整提供",
            "NO_OVERLAP_WITH_BRIEF_WINDOW": "与已提供项目时窗没有重叠",
            "OVERLAPS_BRIEF_WINDOW_NOT_SCHEDULED": "项目时窗有重叠，尚未进行任务级排期",
        }
        resource_check_lines = [
            f"{resources.get(check['resource_id'], {}).get('name') or '资源'}："
            f"{resource_status_labels[check['status']]}"
            for check in row["resource_checks"]
        ]
        asset_rights = [
            f"{asset['use_scope'] or '用途待补'}：{_asset_rights_status_label(asset['rights_status'])}"
            for asset in case.get("asset_uses", [])
        ]
        strategy_body.extend(
            [
                "",
                f"### {row['title']}",
                f"- **作用机制：** {row['mechanism']}",
                f"- **机制路径：** {_mechanism_path_markdown(row['mechanism_path'])}",
                f"- **受众需求：** {row['audience_need'] or '待补'}",
                f"- **受众细分：** {_display_list([item['label'] for item in row['audiences']])}",
                f"- **障碍：** {row['barrier'] or '待补'}",
                f"- **品牌理由：** {row['brand_reason'] or '待补'}",
                f"- **受众替代：** {_display_list([item.get('alternative') for item in row['audiences'] if item.get('alternative')])}",
                f"- **方案依据：** {_reference_markdown(row)}",
                f"- **执行动作：** {_display_list(row['actions'])}",
                f"- **交付与资源：** 交付 "
                f"{', '.join(deliverable_labels) or '未关联'}；"
                f"资源 {', '.join(resource_labels) or '未关联'}；"
                f"项目期核查 {_display_list(resource_check_lines) if resource_check_lines else '未关联或项目期未提供'}；"
                f"依赖关系 {'已提供' if row['dependencies'] else '未提供'}",
                f"- **费用行：**\n{_cost_markdown(row['cost_lines'])}",
                f"- **预算检查：**\n{_budget_markdown(row['budget_check'])}",
                f"- **本案验证：** {_validation_markdown(row['validation'])}",
                f"- **反证/风险：** {_display_list((row['counterevidence'] or []) + (row['risks'] or []))}",
                f"- **案例采用条件：** {_display(case.get('local_fit'))}",
                f"- **案例原问题：** {_display(case.get('problem'))}",
                f"- **案例洞察：** {_display(case.get('insight'))}",
                f"- **核心创意：** {_display(case.get('core_idea'))}",
                f"- **参与机制：** {_display(case.get('participation_mechanism'))}",
                f"- **案例内容表达：** {_display(case.get('content_expression'))}",
                f"- **案例渠道：** {_display_list(case.get('channels', []))}",
                f"- **案例生产资源：** {_display_list(case.get('production_resources', []))}",
                f"- **成本驱动项：** {_display_list(case.get('cost_drivers', []))}",
                f"- **案例反例/失败条件：** {_display_list(case.get('counterexamples', []))}",
                f"- **案例采用变化：** {_display_list(case.get('adoption_changes', []))}",
                f"- **案例结果记录：** {_display(case.get('reported_result'))}；状态 {case.get('reported_result_status', 'unknown')}；依据 {_reference_markdown(case)}",
                f"- **案例权利状态：** {_display_list(asset_rights)}",
                f"- **不可迁移条件：** {_display_list(case.get('non_transferable_conditions', []))}",
                f"- **待解决：** {_display_list(row['gaps'])}",
            ]
        )
        for audience in row["audiences"]:
            strategy_body.append(
                f"  - {audience['label']}：需求 {_display(audience.get('need'))}；"
                f"行为依据 {_display(audience.get('behavior_basis'))}；"
                f"障碍 {_display(audience.get('barrier'))}；"
                f"替代 {_display(audience.get('alternative'))}；"
                f"依据 { _reference_markdown(audience) }"
            )
    if mechanism_comparisons:
        mechanism_labels = {
            "audience_trigger": "受众触发条件",
            "brand_action": "品牌动作",
            "value_exchange": "受众价值交换",
            "reinforcement": "延续/反馈机制",
        }
        candidate_titles = {row["id"]: row["title"] for row in evaluated}
        strategy_body.extend(["", "### 路线机制对照（按输入描述）"])
        for comparison in mechanism_comparisons:
            status_label = {
                "STATED_MECHANISM_PATH_DIFFERENCE": "输入描述了不同组件，仍需业务评审确认真实作用差异",
                "SAME_STATED_MECHANISM_PATH": "四个组件的输入描述相同",
                "SAME_STATED_MECHANISM": "机制文案相同",
                "TEXT_ONLY_DIFFERENCE_UNASSESSED": "仅有文字差异，尚不能判断实际作用机制不同",
            }.get(comparison["status"], "机制差异待核")
            different = _display_list(
                [mechanism_labels.get(field, "机制组件") for field in comparison["different_components"]]
            )
            shared = _display_list(
                [mechanism_labels.get(field, "机制组件") for field in comparison["shared_components"]]
            )
            strategy_body.append(
                f"- {candidate_titles.get(comparison['candidate_ids'][0], '路线一')} 与 "
                f"{candidate_titles.get(comparison['candidate_ids'][1], '路线二')}：{status_label}；"
                f"差异组件 {different}；相同组件 {shared}。"
            )
    if revision_context is not None:
        candidate_titles = {row["id"]: row["title"] for row in evaluated}
        brief_field_labels = {
            "goal": "业务目标",
            "audience": "目标受众",
            "market": "市场",
            "language": "语言",
            "start_date": "开始日期",
            "end_date": "截止日期",
            "timezone": "时区",
            "budget.maximum": "预算硬上限",
            "budget.target": "预算目标",
            "budget.reserve": "预留预算",
            "budget.currency": "预算币种",
            "facts": "事实资料",
            "constraints": "客户约束",
            "forbidden_claims": "禁用主张",
        }
        candidate_field_labels = {
            "title": "路线名称",
            "mechanism": "作用机制",
            "mechanism_path.audience_trigger": "机制路径/受众触发",
            "mechanism_path.brand_action": "机制路径/品牌动作",
            "mechanism_path.value_exchange": "机制路径/价值交换",
            "mechanism_path.reinforcement": "机制路径/反馈延续",
            "audience_need": "受众需求",
            "audiences": "受众细分/行为依据/替代选择",
            "barrier": "目标障碍",
            "brand_reason": "品牌承担理由",
            "actions": "执行动作",
            "deliverable_ids": "交付物",
            "resource_ids": "资源要求",
            "dependencies": "前置依赖",
            "duration_days": "周期",
            "cost_lines": "费用行",
            "quote_id": "报价引用",
            "case_basis": "案例依据与迁移边界",
            "validation": "验证方法",
            "expected_effect": "预期效果假设",
            "counterevidence": "反证",
            "risks": "风险",
            "transfer_conditions": "不可迁移条件",
        }

        def delta_label(path: str) -> str:
            if path.startswith("brief."):
                key = path.removeprefix("brief.")
                return brief_field_labels.get(key, "Brief范围调整")
            if path in brief_field_labels:
                return brief_field_labels[path]
            return candidate_field_labels.get(path, "路线内容/依据调整")

        revision_lines = [
            "",
            "### 本次Brief范围与路线差异",
            "仅展示本次实际输入变化；未出现的旧路线默认保留，删除路线需要单独给出理由。",
        ]
        for change in revision_context.get("brief_changes", []):
            if not isinstance(change, dict):
                continue
            revision_lines.append(
                f"- {delta_label(change.get('path', ''))}："
                f"{_display(change.get('before'))} → {_display(change.get('after'))}。"
            )
        for change in revision_context.get("candidate_changes", []):
            if not isinstance(change, dict):
                continue
            title = change.get("title") or "未命名路线"
            kind = change.get("change")
            if kind == "removed_by_explicit_revision":
                revision_lines.append(
                    f"- 已按用户明确选择移除路线“{title}”：{change.get('reason') or '未提供理由'}；"
                    "旧路线与依据仍保留在前版。"
                )
                continue
            if kind == "added":
                revision_lines.append(f"- 新增路线“{title}”；需要重新核对其机制、资源、预算和权利条件。")
            elif kind == "updated":
                areas = _display_list(change.get("impact_areas", []))
                revision_lines.append(f"- 路线“{title}”更新；影响范围：{areas}。")
            for field_change in change.get("changed_fields", []):
                if not isinstance(field_change, dict):
                    continue
                field_label = delta_label(field_change.get("path", ""))
                revision_lines.append(
                    f"  - {field_label}：{_display(field_change.get('before'))} → "
                    f"{_display(field_change.get('after'))}。"
                )
        preserved_titles = [
            candidate_titles[identity]
            for identity in revision_context.get("preserved_candidate_ids", [])
            if identity in candidate_titles
        ]
        if preserved_titles:
            revision_lines.append(f"- 未改路线保持当前内容：{_display_list(preserved_titles)}。")
        strategy_body.extend(revision_lines)
    facts_body = "\n".join(_fact_lines(brief))
    client_fact_lines = [
        f"- 用户提供陈述：{row.get('text') or '未提供事实描述'}"
        for row in brief.get("facts", [])
    ] or ["- 未提供可用于客户引用的事实陈述。"]
    brief_body = "\n".join(
        [
            "## 目标与范围",
            "",
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众：{brief.get('audience') or '待补'}",
            f"- 市场/语言：{brief.get('market') or '待补'} / {brief.get('language') or '待补'}",
            "",
            "### 已提供事实",
            facts_body,
            "",
            "### 客户约束与禁用主张",
            _display_list(brief.get("constraints", []) + brief.get("forbidden_claims", [])),
        ]
    )
    client_brief_body = "\n".join(
        [
            "## 本次目标与范围",
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众/市场/语言：{brief.get('audience') or '待补'} / {brief.get('market') or '待补'} / {brief.get('language') or '待补'}",
            "### 用户提供陈述（尚未独立核验）",
            *client_fact_lines,
            "### 客户要求与不可用主张",
            _display_list(brief.get("constraints", []) + brief.get("forbidden_claims", [])),
        ]
    )
    budget_summary = _brief_data(brief)["budget"]
    scope_budget_lines = [
        "## 本次预算边界",
        "",
        f"- 币种：{budget_summary['currency'] or '未知'}",
        f"- 硬上限：{budget_summary['maximum'] if budget_summary['maximum'] is not None else '未知'}",
        f"- 目标预算：{budget_summary['target'] if budget_summary['target'] is not None else '未知'}",
        f"- 预留金额：{budget_summary['reserve']}",
        f"- 日期窗口：{brief.get('start_date') or '未知'} — {brief.get('end_date') or '未知'}；时区 {brief.get('timezone') or '未知'}",
        "",
        "## 各路线费用、依赖和履约资源",
        "报价/估计/承诺分开；缺金额、币种或资源条件时保留待补，不按零费用判断可行。",
    ]
    for row in evaluated:
        scope_budget_lines.extend(
            [
                "",
                f"### {row['title']}",
                _cost_markdown(row["cost_lines"]),
                _budget_markdown(row["budget_check"]),
                f"- 计划周期：{row.get('duration_days') if row.get('duration_days') is not None else '未知'} 日",
                f"- 前置依赖：{_display_list(row['dependencies'])}",
                "- 交付物：",
            ]
        )
        for identity in row["deliverable_ids"]:
            deliverable = deliverables.get(identity)
            if deliverable is None:
                scope_budget_lines.append("  - 有交付物引用未包含在本次快照中。")
            else:
                scope_budget_lines.append(
                    f"  - {deliverable.get('title') or '未命名交付物'}："
                    f"数量 {deliverable.get('quantity', '未知')} {deliverable.get('unit') or ''}；"
                    f"规格 {_display(deliverable.get('specification'))}；"
                    f"验收 {_acceptance_summary(deliverable.get('acceptance'))}"
                )
        scope_budget_lines.append("- 资源：")
        for identity in row["resource_ids"]:
            resource = resources.get(identity)
            if resource is None:
                scope_budget_lines.append("  - 有资源引用未包含在本次快照中。")
            else:
                availability = resource.get("availability")
                declared_window = (
                    _display_list(
                        [f"{item.get('start') or '未知'} — {item.get('end') or '未知'}" for item in availability]
                    )
                    if availability
                    else f"{resource.get('available_from') or '未知'} — {resource.get('available_to') or '未知'}"
                )
                scope_budget_lines.append(
                    f"  - {resource.get('name') or '未命名资源'}：容量 {resource['capacity'] if resource['capacity'] is not None else '未知'} {resource.get('unit') or ''}；"
                    f"资格 {_display_list(resource.get('qualifications', []))}；可用窗口 {declared_window}；"
                    f"{_reference_markdown(resource)}"
                )
        if not row["resource_ids"]:
            scope_budget_lines.append("  - 未关联具体资源；不据此表示无需资源。")
    scope_budget_body = "\n".join(scope_budget_lines)
    if strategy_revision_impact is not None:
        budget_scope_changes = [
            row for candidate in strategy_revision_impact.get("candidate_changes", [])
            if isinstance(candidate, dict)
            and any("预算" in area or "资源" in area or "交付" in area for area in candidate.get("impact_areas", []))
        ]
        if budget_scope_changes:
            scope_budget_body += "\n\n## 本次预算/资源/交付影响\n" + "\n".join(
                f"- {row.get('title') or '路线'}：{_display_list(row.get('impact_areas', []))}；"
                "按当前输入重新核算费用、交付和资源条件。"
                for row in budget_scope_changes
            )
    client_strategy_lines = [
        f"**业务目标：** {brief.get('goal') or '待补'}",
        f"**受众/市场：** {brief.get('audience') or '待补'} / {brief.get('market') or '待补'}",
        "以下路线按本案约束形成，供客户比较与修改；不代表已批准或承诺效果。",
    ]
    if strategy_revision_impact is not None:
        client_strategy_lines.append(
            "本版依据当前Brief和用户明确的路线调整重新整理；未采用方向及其历史版本仍可在本工作中查阅。"
        )
    client_budget_lines = [
        _client_budget_header(brief),
        "只列明确标记为对客报价/估计的金额；供应商底价、代理成本、利润与内部预留不进入客户版。",
    ]
    visible_client_quote_count = 0
    for row in evaluated:
        case = row["case_basis"] or {}
        deliverable_names = [
            deliverables[identity].get("title") or "未命名交付物"
            for identity in row["deliverable_ids"]
            if identity in deliverables
        ]
        client_strategy_lines.extend(
            [
                "",
                f"## {row['title']}",
                f"- 作用机制：{row['mechanism']}",
                f"- 机制路径：{_mechanism_path_markdown(row['mechanism_path'])}",
                f"- 受众需求/替代：{row['audience_need'] or '待补'}；"
                f"{_display_list([item.get('alternative') for item in row['audiences'] if item.get('alternative')])}",
                f"- 品牌理由：{row['brand_reason'] or '待补'}",
                f"- 执行动作：{_display_list(row['actions'])}",
                f"- 交付物：{_display_list(deliverable_names)}",
                f"- 本案验证：{_validation_markdown(row['validation'])}",
                f"- 反证/风险：{_display_list((row['counterevidence'] or []) + (row['risks'] or []))}",
                f"- 案例采用变化：{_display_list(case.get('adoption_changes', []))}",
                f"- 不可迁移条件：{_display_list(case.get('non_transferable_conditions', []))}",
                "- 案例报告结果与具体素材来源未在本客户版公开；素材使用权仍按原工作引用另行确认。",
                f"- 对客报价：\n{_client_cost_markdown(row['cost_lines'])}",
            ]
        )
        for asset in case.get("asset_uses", []):
            client_strategy_lines.append(
                f"- 拟用素材约束：用途 {asset.get('use_scope') or '待补'}；"
                f"地域 {_display_list(asset.get('territories', []))}；"
                f"期限 {_rights_term_summary(asset)}；"
                f"权利状态 {_asset_rights_status_label(asset.get('rights_status'))}。"
            )
        visible_client_quote_count += sum(
            line.get("client_quote") is not None for line in row["cost_lines"]
        )
        client_budget_lines.extend(
            [
                "",
                f"### {row['title']}",
                _client_cost_markdown(row["cost_lines"]),
                "- 对客交付范围：",
            ]
        )
        for identity in row["deliverable_ids"]:
            deliverable = deliverables.get(identity)
            if deliverable:
                client_budget_lines.append(
                    f"  - {deliverable.get('title') or '未命名交付物'}："
                    f"数量 {deliverable.get('quantity', '未知')} {deliverable.get('unit') or ''}；"
                    f"规格 {_display(deliverable.get('specification'))}；"
                    f"验收 {_acceptance_summary(deliverable.get('acceptance'))}"
                )
    if visible_client_quote_count == 0:
        client_limitations = [
            "没有成本/报价字段被明确标记为可向客户展示；客户版不显示内部金额，当前不能作为对客报价。",
        ]
    else:
        client_limitations = [
            "客户版金额仅包含输入中明确的对客报价/估计，不由供应商成本推算；仍需按实际合同与授权确认。",
        ]
    if any(
        asset.get("rights_status") != "confirmed" or not asset.get("rights_refs")
        for row in evaluated
        for asset in (row.get("case_basis") or {}).get("asset_uses", [])
    ):
        client_limitations.append("案例素材存在未确认的用途/地域/期限权利；客户版不表示该素材可对外复用。")
    for row in evaluated:
        if not row["resource_ids"]:
            client_limitations.append(
                f"路线“{row['title']}”尚未关联具体执行资源；这不表示无需资源，实际档期与容量仍需核定。"
            )
            continue
        for identity in row["resource_ids"]:
            resource = resources.get(identity)
            if resource is None:
                continue
            if resource["capacity"] is None:
                client_limitations.append(
                    f"路线“{row['title']}”有一项所需资源的可用数量未知。"
                )
            if brief.get("start_date") is not None and brief.get("end_date") is not None:
                if not resource.get("availability") and not (
                    resource.get("available_from") and resource.get("available_to")
                ):
                    client_limitations.append(
                        f"路线“{row['title']}”的资源可用时段未提供，需在排期中核定。"
                    )
        for check in row["resource_checks"]:
            if check["status"] == "NO_OVERLAP_WITH_BRIEF_WINDOW":
                client_limitations.append(
                    f"路线“{row['title']}”所需资源在已声明项目窗口内没有可用时段重叠。"
                )
            elif check["status"] == "PROJECT_WINDOW_UNKNOWN":
                client_limitations.append(
                    f"路线“{row['title']}”的项目窗口未提供，不能判断所需资源的实际日期适配。"
                )
            elif check["status"] == "OVERLAPS_BRIEF_WINDOW_NOT_SCHEDULED":
                client_limitations.append(
                    f"路线“{row['title']}”的资源时段与项目窗口有重叠，但并行容量和任务顺序需在排期中核定。"
                )
    client_citations = _client_citations(inputs)
    if not client_citations:
        client_limitations.append("没有明确标记为允许客户引用的原文；客户版不附资料原件引文或内部来源标识。")
    client_strategy_body = "\n".join(client_strategy_lines)
    client_budget_body = "\n".join(client_budget_lines)
    client_validation_body = "\n".join(
        [
            "## 对客验证说明",
            "本案指标、分母、观察窗口和数据使用范围以路线中明确给出的定义为准；缺失时保持待补。",
            "不承诺未验证的案例效果，也不将预算、曝光或相关性解释为增量结果。",
            f"客户版引用状态：{'已提供明确客户引用清单' if client_citations else '未提供客户引用授权；未附原件引用'}。",
        ]
    )
    validation_candidates = [
        {
            "id": row["id"],
            "validation": row["validation"],
            "counterevidence": row["counterevidence"],
            "source_refs": row["source_refs"],
            "evidence_refs": row["evidence_refs"],
        }
        for row in evaluated
    ]
    strategy_brief_data = _brief_data(brief)
    if strategy_revision_impact is not None:
        strategy_brief_data["revision_impact"] = {
            "brief_changes": deepcopy(strategy_revision_impact.get("brief_changes", [])),
            "affected_sections": deepcopy(strategy_revision_impact.get("affected_sections", [])),
        }
    return {
        "title": f"策略提案：{brief.get('goal') or '待定义目标'}",
        "sections": [
            _section(
                "strategy_brief",
                "Brief、事实与边界",
                brief_body,
                strategy_brief_data,
                client_body=client_brief_body,
            ),
            _section(
                "strategy_alternatives",
                "作用机制与执行路线",
                "\n".join(strategy_body),
                {
                    "candidates": evaluated,
                    "mechanism_comparisons": mechanism_comparisons,
                    "exact_mechanism_duplicates": duplicate_mechanisms,
                    "revision_impact": deepcopy(strategy_revision_impact),
                },
                client_body=client_strategy_body,
            ),
            _section(
                "strategy_scope_budget",
                "预算、依赖与待确认范围",
                scope_budget_body,
                {
                    "budget": _brief_data(brief)["budget"],
                    "quotes": _public_quotes(quotes),
                    "deliverables": deepcopy(list(deliverables.values())),
                    "resources": [_public_resource(row) for row in resources.values()],
                    "revision_impact": deepcopy(strategy_revision_impact),
                },
                client_body=client_budget_body,
            ),
            _section(
                "strategy_validation",
                "验证、反证与不确定性",
                "\n".join(
                    [
                        "## 验证设计",
                        "",
                        "各备选只保留其本案明确提供的验证方法。若指标定义、分母、观察期或数据权利缺失，相关效果仍无法判定。",
                        "",
                        "### 事实依据",
                        facts_body,
                        "",
                        "### 限制",
                        _display_list(limitations),
                    ]
                ),
                {"limitations": limitations, "candidates": validation_candidates, "revision_impact": deepcopy(strategy_revision_impact)},
                client_body=client_validation_body,
            ),
        ],
        "limitations": _dedupe(limitations),
        "client_limitations": _dedupe(client_limitations),
        "client_citations": client_citations,
    }


def _public_quotes(quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "amount": _json_number(row["amount"]),
            "currency": row["currency"],
            "status": row["status"],
            "client_visible": row.get("client_visible", False),
            "client_quote": deepcopy(row.get("client_quote")),
            "valid_until": row["valid_until"],
            "source_refs": deepcopy(row["source_refs"]),
            "evidence_refs": deepcopy(row["evidence_refs"]),
            "basis_status": row["basis_status"],
        }
        for row in quotes.values()
    ]


def _public_resource(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row.get("name"),
        "capacity": row["capacity"],
        "unit": row.get("unit"),
        "qualifications": deepcopy(row.get("qualifications", [])),
        "available_from": row.get("available_from"),
        "available_to": row.get("available_to"),
        "availability": deepcopy(row.get("availability", [])),
        **_refs(row, f"resources.{row['id']}"),
    }


def _evaluate_media(
    brief: dict[str, Any], inputs: dict[str, Any], quotes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw_candidates = inputs.get("candidates")
    if raw_candidates is None:
        candidates = [{"id": None, "title": "已提供媒介计划", "media_items": inputs.get("media_items")}]
    else:
        candidates = _rows(raw_candidates, "candidates")
    if not candidates:
        raise ValueError("media_plan requires at least one plan")
    deliverables = _deliverable_index(inputs.get("deliverables", []))
    budget = _budget_context(brief)
    brief_window = None
    if brief.get("start_date") is not None and brief.get("end_date") is not None:
        brief_start = _parse_time(brief["start_date"], "brief.start_date", brief.get("timezone"))
        brief_end = _parse_time(brief["end_date"], "brief.end_date", brief.get("timezone"), end_of_date=True)
        if brief_end <= brief_start:
            raise ValueError("brief.end_date must follow brief.start_date")
        brief_window = (brief_start, brief_end)
    evaluated = []
    all_limitations = [
        "投入金额不推导曝光、有效触达、购买或增量效果。",
        "未声明已批准的投放渠道时，本计划仅供编辑，不代表已下单或已发布。",
    ]
    for candidate_index, candidate in enumerate(candidates):
        path = f"candidates[{candidate_index}]"
        if not isinstance(candidate.get("title", ""), str):
            raise ValueError(f"{path}.title must be text")
        media_rows = _rows(candidate.get("media_items"), f"{path}.media_items")
        if not media_rows:
            raise ValueError(f"{path}.media_items must contain at least one placement")
        placements = []
        cost_lines = []
        totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        unknown_cost = False
        conflicts = []
        for index, row in enumerate(media_rows):
            row_path = f"{path}.media_items[{index}]"
            channel = _required_text(row.get("channel"), f"{row_path}.channel")
            placement = _optional_text(row.get("placement"), f"{row_path}.placement")
            account_id = _optional_text(row.get("account_id"), f"{row_path}.account_id")
            comparison_group = _optional_text(
                row.get("comparison_group"), f"{row_path}.comparison_group"
            )
            deliverable_ids = row.get("deliverable_ids", [])
            if not isinstance(deliverable_ids, list) or any(not isinstance(x, str) for x in deliverable_ids):
                raise ValueError(f"{row_path}.deliverable_ids must be a list of IDs")
            missing_deliverables = [identity for identity in deliverable_ids if identity not in deliverables]
            start = row.get("start_date")
            end = row.get("end_date")
            if start is not None:
                _parse_time(start, f"{row_path}.start_date", brief.get("timezone"))
            if end is not None:
                _parse_time(end, f"{row_path}.end_date", brief.get("timezone"), end_of_date=True)
            if start is not None and end is not None:
                parsed_start = _parse_time(start, f"{row_path}.start_date", brief.get("timezone"))
                parsed_end = _parse_time(end, f"{row_path}.end_date", brief.get("timezone"), end_of_date=True)
                if parsed_end <= parsed_start:
                    raise ValueError(f"{row_path} end_date must follow start_date")
                if brief_window and (parsed_start < brief_window[0] or parsed_end > brief_window[1]):
                    conflicts.append(f"{channel} / {placement or '未指定版位'} 超出Brief的项目窗口。")
            line = {
                "id": row["id"],
                "quote_id": row.get("quote_id"),
                "amount": row.get("amount"),
                "quantity": row.get("quantity", 1),
                "currency": row.get("currency"),
                "status": row.get("status", "estimate"),
                "source_refs": row.get("source_refs", []),
                "evidence_refs": row.get("evidence_refs", []),
                "client_visible": row.get("client_visible", False),
                "client_quote": row.get("client_quote"),
            }
            cancellation = _cancellation_scenario(
                row.get("cancellation_scenario"),
                f"{row_path}.cancellation_scenario",
                budget["currency"],
                brief.get("timezone"),
            )
            parsed_costs, row_totals, row_unknown = _cost_lines([line], f"{row_path}.cost", quotes, budget["currency"])
            cost_lines.extend(parsed_costs)
            _merge_totals(totals, row_totals)
            unknown_cost |= row_unknown
            gaps = []
            if not account_id:
                gaps.append("账号或采购链未指定")
            if not placement:
                gaps.append("具体版位未指定")
            if start is None or end is None:
                gaps.append("投放期间待补")
            required_fields = (
                ("purchase_path", "采购路径"),
                ("pricing_basis", "计价/质量口径"),
                ("metric_definition", "观测指标定义"),
                ("denominator", "观测分母"),
                ("observation_window", "观察窗口"),
                ("data_rights", "数据使用权"),
                ("exclusions", "排除规则"),
                ("brand_suitability", "品牌适宜性条件"),
            )
            for field, label in required_fields:
                if row.get(field) in (None, "", []):
                    gaps.append(label + "待补")
            if row.get("quoted_reach") is not None and type(row["quoted_reach"]) in (int, float):
                _nonnegative(row["quoted_reach"], f"{row_path}.quoted_reach")
            if missing_deliverables:
                gaps.append("未关联交付物：" + ", ".join(missing_deliverables))
            placements.append(
                {
                    "id": row["id"],
                    "channel": channel,
                    "account_id": account_id,
                    "placement": placement,
                    "comparison_group": comparison_group,
                    "market": deepcopy(row.get("market")),
                    "audience_scope": deepcopy(row.get("audience_scope")),
                    "start_date": start,
                    "end_date": end,
                    "deliverable_ids": deepcopy(deliverable_ids),
                    "pricing_basis": deepcopy(row.get("pricing_basis")),
                    "metric_definition": deepcopy(row.get("metric_definition")),
                    "denominator": deepcopy(row.get("denominator")),
                    "observation_window": deepcopy(row.get("observation_window")),
                    "data_rights": deepcopy(row.get("data_rights")),
                    "exclusions": deepcopy(row.get("exclusions")),
                    "brand_suitability": deepcopy(row.get("brand_suitability")),
                    "quoted_reach": deepcopy(row.get("quoted_reach")),
                    "cancellation_scenario": cancellation,
                    "gaps": gaps,
                    **_refs(row, row_path),
                }
            )
        has_planning_cost = any(
            line["status"] in {"quote", "estimate", "committed"}
            for line in cost_lines
        )
        budget_check = _budget_check(
            budget,
            totals,
            unknown_cost=unknown_cost or not has_planning_cost,
            existing_committed=_existing_committed(
                quotes,
                currency=budget["currency"],
                exclude={row.get("quote_id") for row in media_rows if row.get("quote_id")},
            ),
        )
        evaluated.append(
            {
                "id": candidate["id"],
                "title": candidate.get("title", "媒介计划"),
                "media_items": placements,
                "cost_lines": cost_lines,
                "budget_check": budget_check,
                "conflicts": conflicts,
                **_refs(candidate, path),
            }
        )
        if any(row["gaps"] for row in placements):
            all_limitations.append(f"媒介方案“{candidate.get('title', candidate['id'])}”存在版位、计价、口径、权利或交付缺口。")
        if conflicts:
            all_limitations.extend(conflicts)
        if any(item["cancellation_scenario"] for item in placements):
            all_limitations.append(
                f"媒介方案“{candidate.get('title', candidate['id'])}”的取消/退款仅按用户提供条款试算；未发送取消通知或办理退款。"
            )
    media_comparisons = _media_comparisons(evaluated)
    if not media_comparisons:
        all_limitations.append(
            "没有足够共同的版位、市场、受众、计价与观测口径，未生成媒介价格对比。"
        )
    body_lines = ["本节按输入保留渠道、账号/版位、期间、交付、价格和观测口径。没有将预算推算成触达或转化。"]
    for plan in evaluated:
        costs_by_id = {line["id"]: line for line in plan["cost_lines"]}
        body_lines.extend(
            [
                "",
                f"## {plan['title']}",
                f"预算检查：{_budget_markdown(plan['budget_check'])}",
                f"期间冲突：{_display_list(plan['conflicts'])}",
            ]
        )
        for row in plan["media_items"]:
            cost = costs_by_id.get(row["id"], {})
            price = (
                f"{cost['amount']} {cost['currency']}（{_quote_status_label(cost['status'])}）"
                if cost.get("amount") is not None
                else f"金额未知（{_quote_status_label(cost.get('status', 'UNKNOWN'))}）"
            )
            body_lines.append(
                f"- **{row['channel']} / {row['placement'] or '版位待定'}**；账号 {row['account_id'] or '待定'}；"
                f"期间 {row['start_date'] or '待定'} 至 {row['end_date'] or '待定'}；"
                f"交付物 {', '.join(row['deliverable_ids']) or '未关联'}；费用 {price}；"
                f"市场 {row.get('market') or '待定'}；受众 {row.get('audience_scope') or '待定'}；"
                f"缺口：{_display_list(row['gaps'])}"
            )
            cancellation = row.get("cancellation_scenario")
            if cancellation:
                body_lines.append(
                    f"  - {_cancellation_status_label(cancellation['status'])}；"
                    f"实支扣退款 {cancellation['net_actual_after_refund'] if cancellation['net_actual_after_refund'] is not None else '未知'}；"
                    f"剩余承诺与费用 {cancellation['remaining_commitment_and_fee'] if cancellation['remaining_commitment_and_fee'] is not None else '未知'}；"
                    f"合计 {cancellation['total_cost_after_cancellation'] if cancellation['total_cost_after_cancellation'] is not None else '未知'} "
                    f"{cancellation['currency'] or ''}；窗口 {_policy_window_label(cancellation['policy_window_status'])}。"
                )
    scope_body = "\n".join(
        [
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众：{brief.get('audience') or '待补'}",
            f"- 市场/期间：{brief.get('market') or '待补'} / "
            f"{brief.get('start_date') or '待补'} — {brief.get('end_date') or '待补'}",
            "",
            "### 事实依据",
            "\n".join(_fact_lines(brief)),
        ]
    )
    budget_candidates = [
        {
            "id": plan["id"],
            "cost_lines": plan["cost_lines"],
            "budget_check": plan["budget_check"],
            "cancellation_scenarios": [
                {"media_item_id": item["id"], "scenario": item["cancellation_scenario"]}
                for item in plan["media_items"]
                if item["cancellation_scenario"] is not None
            ],
        }
        for plan in evaluated
    ]
    measurement_candidates = [
        {
            "id": plan["id"],
            "items": [
                {
                    "id": item["id"],
                    "metric_definition": item["metric_definition"],
                    "denominator": item["denominator"],
                    "observation_window": item["observation_window"],
                    "source_refs": item["source_refs"],
                    "evidence_refs": item["evidence_refs"],
                }
                for item in plan["media_items"]
            ],
        }
        for plan in evaluated
    ]
    budget_body_lines = [
        "金额按报价、估计、承诺、实支和退款分开；无硬上限时不虚构预算可行结论。",
        "取消试算中的承诺金额指取消前尚未履行的承诺，不含已发生实支；请按合同金额口径核对。",
    ]
    for plan in evaluated:
        budget_body_lines.extend(
            [
                "",
                f"## {plan['title']}",
                _budget_markdown(plan["budget_check"]),
                "",
                _cost_markdown(plan["cost_lines"]),
            ]
        )
        for item in plan["media_items"]:
            if item["cancellation_scenario"] is not None:
                scenario = item["cancellation_scenario"]
                budget_body_lines.append(
                    f"- {item['channel']} / {item['placement'] or '版位待定'} 取消情景：实支 "
                    f"{scenario['actual_spend'] if scenario['actual_spend'] is not None else '未知'}；"
                    f"退款 {scenario['refund_amount'] if scenario['refund_amount'] is not None else '未知'}；"
                    f"承诺释放 {scenario['released_commitment'] if scenario['released_commitment'] is not None else '未知'}；"
                    f"取消费用 {scenario['fee_amount'] if scenario['fee_amount'] is not None else '未知'}；"
                    f"情景合计 {scenario['total_cost_after_cancellation'] if scenario['total_cost_after_cancellation'] is not None else '未知'}。"
                )
    comparison_body_lines = [
        "价格仅在媒介类型、版位、市场、受众、计价、指标、分母、窗口、数据权利和排除规则逐项一致时比较；价格差不表示效果优劣。"
    ]
    plans_by_id = {plan["id"]: plan for plan in evaluated}

    def comparison_operand(plan_index: int) -> str:
        plan_id = comparison["candidate_ids"][plan_index]
        item_id = comparison["media_item_ids"][plan_index]
        plan = plans_by_id[plan_id]
        item = next(row for row in plan["media_items"] if row["id"] == item_id)
        return f"{plan['title']} · {item['channel']} / {item['placement'] or '版位待定'}"

    for comparison in media_comparisons:
        comparison_body_lines.append(
            f"- {comparison_operand(0)} 与 {comparison_operand(1)}："
            f"{_media_comparison_label(comparison['status'])}；差异 {_display_list([_media_basis_label(field) for field in comparison['different_basis_fields']])}；"
            f"缺项 {_display_list([_media_basis_label(field) for field in comparison['missing_basis_fields']])}；"
            f"第二项减第一项 {comparison['second_minus_first_amount']} "
            f"{comparison['price_currency'] or ''}"
        )
    if not media_comparisons:
        comparison_body_lines.append("- 没有足够共同的比较组与相同口径，保留各项报价，不作横向排序。")
    client_citations = _client_citations(inputs)
    client_scope_body = "\n".join(
        [
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众/市场：{brief.get('audience') or '待补'} / {brief.get('market') or '待补'}",
            f"- 期间：{brief.get('start_date') or '待补'} — {brief.get('end_date') or '待补'}",
            "- 渠道和投放条件以本客户版所列条目为准；未确认的采购和发布条件不作为已执行事项。",
        ]
    )
    client_placement_lines = [
        "以下是候选媒介范围与客户侧交付条件；供应商底价、采购成本与内部预留不会自动展示。"
    ]
    client_budget_lines = [_client_budget_header(brief)]
    visible_client_quote_count = 0
    for plan in evaluated:
        costs_by_id = {line["id"]: line for line in plan["cost_lines"]}
        client_placement_lines.extend(["", f"## {plan['title']}"])
        client_budget_lines.extend(["", f"## {plan['title']}"])
        client_budget_lines.append(_client_cost_markdown(plan["cost_lines"]))
        for item in plan["media_items"]:
            deliverable_titles = [
                deliverables[identity].get("title") or "未命名交付物"
                for identity in item["deliverable_ids"]
                if identity in deliverables
            ]
            quote_line = costs_by_id.get(item["id"])
            visible_client_quote_count += int(bool(quote_line and quote_line.get("client_quote")))
            client_placement_lines.extend(
                [
                    f"- 渠道/版位：{item['channel']} / {item['placement'] or '待定'}",
                    f"  期间：{item['start_date'] or '待补'} — {item['end_date'] or '待补'}；"
                    f"交付物：{_display_list(deliverable_titles)}",
                    f"  受众：{item['audience_scope'] or '待补'}；市场：{item['market'] or '待补'}",
                    f"  指标：{item['metric_definition'] or '待补'}；分母：{item['denominator'] or '待补'}；"
                    f"窗口：{item['observation_window'] or '待补'}",
                    f"  数据权利：{_display(item['data_rights'])}；排除规则：{_display_list(item['exclusions'])}",
                    f"  对客报价：\n{_client_cost_markdown([quote_line] if quote_line else [])}",
                ]
            )
    client_measurement_body = "\n".join(
        [
            "客户版保留指标定义、分母、观察窗口、数据权利、排除规则和品牌适宜性条件。",
            "供应商价格差与内部采购比较不进入客户版；效果排名仍需可比的观测数据支持。",
        ]
    )
    client_limitations = []
    if visible_client_quote_count == 0:
        client_limitations.append("没有媒介成本行被明确标记为对客报价/估计；客户版不披露供应商底价，也不推算客户价格。")
    else:
        client_limitations.append("客户版只显示明确提供的对客报价/估计，不由供应商成本推算加价。")
    if not client_citations:
        client_limitations.append("没有明确授权客户引用的资料原文；客户版不附来源原件或内部来源标识。")
    return {
        "title": f"媒介计划：{brief.get('goal') or '待定义目标'}",
        "sections": [
            _section(
                "media_plan_scope",
                "市场、渠道与交付范围",
                scope_body,
                _brief_data(brief),
                client_body=client_scope_body,
            ),
            _section(
                "media_placements",
                "渠道、版位与投放版本",
                "\n".join(body_lines),
                {"candidates": evaluated},
                client_body="\n".join(client_placement_lines),
            ),
            _section(
                "media_budget",
                "报价与预算约束",
                "\n".join(budget_body_lines),
                {"candidates": budget_candidates, "quotes": _public_quotes(quotes)},
                client_body="\n".join(client_budget_lines),
            ),
            _section(
                "media_measurement",
                "观测、权利与核查",
                "\n".join(
                    [
                        "缺少指标定义、分母、观察窗口、数据权利或版位核验时不计算效果；平台自报或报价不自动成为独立审核数据。",
                        "",
                        "### 候选可比性",
                        *comparison_body_lines,
                    ]
                ),
                {
                    "limitations": _dedupe(all_limitations),
                    "measurement_fields": [
                        "metric_definition",
                        "denominator",
                        "observation_window",
                        "data_rights",
                        "exclusions",
                        "brand_suitability",
                    ],
                    "candidates": measurement_candidates,
                    "comparisons": media_comparisons,
                },
                client_body=client_measurement_body,
            ),
        ],
        "limitations": _dedupe(all_limitations),
        "client_limitations": _dedupe(client_limitations),
        "client_citations": client_citations,
    }


def _merge_totals(target: dict[str, dict[str, Decimal]], source: dict[str, dict[str, Decimal]]) -> None:
    for currency, states in source.items():
        for state, amount in states.items():
            target[currency][state] += amount


def _contact_permission(
    collaborator: dict[str, Any], path: str, brief: dict[str, Any]
) -> dict[str, Any]:
    raw = collaborator.get("contact_permission")
    if raw is None:
        return {
            "status": "unknown",
            "contactability": "NOT_CONTACTABLE",
            "record_status": "no_permission_record_supplied",
            "external_contact": "not_performed_by_evaluator",
        }
    permission = _mapping(raw, f"{path}.contact_permission")
    status = permission.get("status", "unknown")
    allowed = {"unknown", "granted", "denied", "withdrawn", "opted_out"}
    if not isinstance(status, str) or status not in allowed:
        raise ValueError(f"{path}.contact_permission.status is invalid")
    channels = permission.get("channels", [])
    if not isinstance(channels, list) or any(not isinstance(item, str) for item in channels):
        raise ValueError(f"{path}.contact_permission.channels must be a list")
    purpose = _optional_text(permission.get("purpose"), f"{path}.contact_permission.purpose")
    scope = _optional_text(permission.get("scope"), f"{path}.contact_permission.scope")
    valid_until = permission.get("valid_until")
    contactability = "NOT_CONTACTABLE"
    if status == "granted":
        if not purpose or not scope or not channels or not valid_until:
            contactability = "GRANT_SCOPE_OR_EXPIRY_INCOMPLETE"
        else:
            expiry = _parse_time(
                valid_until,
                f"{path}.contact_permission.valid_until",
                brief.get("timezone"),
                end_of_date=True,
            )
            project_start = brief.get("start_date")
            expired = project_start is not None and expiry < _parse_time(
                project_start,
                "brief.start_date",
                brief.get("timezone"),
            )
            contactability = (
                "GRANT_EXPIRED_FOR_PROJECT"
                if expired
                else "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK"
            )
    refs = _refs(permission, f"{path}.contact_permission")
    return {
        **deepcopy(permission),
        "status": status,
        "channels": deepcopy(channels),
        "purpose": purpose,
        "scope": scope,
        "contactability": contactability,
        "record_status": refs["basis_status"],
        "external_contact": "not_performed_by_evaluator",
        **refs,
    }


_PODCAST_MODE_TERM_LABELS = {
    "insertion_position": "贴片位置",
    "episodes": "期数",
    "read_length_seconds": "口播时长",
    "episode_format": "节目形式",
    "episode_length_minutes": "节目长度",
    "editorial_scope": "编辑/审阅范围",
    "partner_channels": "联合渠道",
    "cross_promotion_assets": "交叉传播素材",
    "responsibility_split": "责任分工",
    "production_owner": "制作责任方",
    "hosting_account": "托管账号",
    "distribution_scope": "分发范围",
    "audio_deliverable": "声音交付物",
    "cross_channel_deliverables": "跨渠道交付物",
    "tracking_definition": "追踪口径",
    "event_or_ip": "活动或IP身份",
    "location": "地点",
    "recording_rights": "录制与传播权利",
}
_PODCAST_NUMERIC_TERMS = {"episodes", "read_length_seconds", "episode_length_minutes"}


def _podcast_observations(value: Any, path: str) -> tuple[dict[str, Any], list[str]]:
    raw = _mapping({} if value is None else value, path)
    unknown = set(raw) - set(_PODCAST_POWER_LABELS)
    if unknown:
        raise ValueError(f"{path} has unknown dimensions: {', '.join(sorted(unknown))}")
    observations = {}
    gaps = []
    for dimension, label in _PODCAST_POWER_LABELS.items():
        item_path = f"{path}.{dimension}"
        item = _mapping(raw.get(dimension, {}), item_path)
        value_number = (
            _finite(item["value"], f"{item_path}.value")
            if item.get("value") is not None
            else None
        )
        denominator = item.get("denominator")
        if denominator is not None and type(denominator) in (int, float, Decimal):
            denominator = _nonnegative(denominator, f"{item_path}.denominator")
        elif denominator is not None:
            denominator = _optional_text(denominator, f"{item_path}.denominator")
        metric = _optional_text(item.get("metric_definition"), f"{item_path}.metric_definition")
        unit = _optional_text(item.get("unit"), f"{item_path}.unit")
        window = _optional_text(item.get("observation_window"), f"{item_path}.observation_window")
        source_kind = _optional_text(item.get("source_kind"), f"{item_path}.source_kind")
        note = _optional_text(item.get("note"), f"{item_path}.note")
        refs = _refs(item, item_path)
        supplied = any(
            part is not None
            for part in (value_number, denominator, metric, unit, window, source_kind, note)
        ) or bool(refs["source_refs"] or refs["evidence_refs"])
        missing = []
        if supplied and not metric:
            missing.append("指标定义")
        if supplied and not window:
            missing.append("观察窗口")
        if supplied and not (refs["source_refs"] or refs["evidence_refs"]):
            missing.append("可追溯来源")
        if missing:
            gaps.append(f"播客{label}观察缺少：" + "、".join(missing))
        observations[dimension] = {
            "dimension": dimension,
            "label": label,
            "metric_definition": metric,
            "value": _json_number(value_number) if value_number is not None else None,
            "unit": unit,
            "denominator": _json_number(denominator) if isinstance(denominator, Decimal) else denominator,
            "observation_window": window,
            "source_kind": source_kind or "UNKNOWN",
            "note": note,
            "status": "USER_SUPPLIED_OBSERVATION" if supplied else "UNKNOWN",
            **refs,
        }
    return observations, gaps


def _podcast_plan(
    collaborator: dict[str, Any], path: str
) -> tuple[dict[str, Any] | None, list[str]]:
    mode = _optional_text(collaborator.get("podcast_mode"), f"{path}.podcast_mode")
    terms = collaborator.get("podcast_mode_terms", {})
    if mode is None:
        if terms not in ({}, None) or collaborator.get("podcast_dimensions") not in ({}, None):
            raise ValueError(f"{path} must select podcast_mode before providing podcast details")
        return None, []
    if mode not in _PODCAST_MODES:
        raise ValueError(f"{path}.podcast_mode is unsupported")
    terms = _mapping(terms, f"{path}.podcast_mode_terms")
    unknown = set(terms) - set(_PODCAST_MODE_TERM_LABELS)
    if unknown:
        raise ValueError(f"{path}.podcast_mode_terms has unknown fields: {', '.join(sorted(unknown))}")
    normalized_terms = {}
    for key in _PODCAST_MODE_TERM_LABELS:
        raw_term = terms.get(key)
        if key in _PODCAST_NUMERIC_TERMS:
            normalized_terms[key] = (
                _json_number(_positive(raw_term, f"{path}.podcast_mode_terms.{key}"))
                if raw_term is not None
                else None
            )
        else:
            normalized_terms[key] = _optional_text(raw_term, f"{path}.podcast_mode_terms.{key}")
    mode_definition = _PODCAST_MODES[mode]
    required_terms = mode_definition["terms"]
    missing_terms = [
        _PODCAST_MODE_TERM_LABELS[key]
        for key in required_terms
        if not normalized_terms[key]
    ]
    powers, power_gaps = _podcast_observations(
        collaborator.get("podcast_dimensions", {}),
        f"{path}.podcast_dimensions",
    )
    plan = {
        "mode": mode,
        "mode_label": mode_definition["label"],
        "mode_terms": normalized_terms,
        "required_term_keys": list(required_terms),
        "missing_mode_terms": missing_terms,
        "powers": powers,
        "power_gaps": power_gaps,
        "comparison_status": "PODCAST_DIMENSIONS_KEPT_SEPARATE_NO_COMPOSITE_SCORE",
    }
    gaps = []
    if missing_terms:
        gaps.append(f"播客合作模式“{mode_definition['label']}”待补：" + "、".join(missing_terms))
    gaps.extend(power_gaps)
    return plan, gaps


def _media_comparisons(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparison_fields = (
        "channel",
        "account_id",
        "placement",
        "market",
        "audience_scope",
        "start_date",
        "end_date",
        "pricing_basis",
        "metric_definition",
        "denominator",
        "observation_window",
        "data_rights",
        "exclusions",
    )
    comparisons = []
    for index, first_plan in enumerate(candidates):
        for second_plan in candidates[index + 1 :]:
            first_costs = {line["id"]: line for line in first_plan["cost_lines"]}
            second_costs = {line["id"]: line for line in second_plan["cost_lines"]}
            for first_item in first_plan["media_items"]:
                group = first_item.get("comparison_group")
                if not group:
                    continue
                for second_item in second_plan["media_items"]:
                    if second_item.get("comparison_group") != group:
                        continue
                    missing = [
                        field
                        for field in comparison_fields
                        if first_item.get(field) in (None, "", [])
                        or second_item.get(field) in (None, "", [])
                    ]
                    different = [
                        field
                        for field in comparison_fields
                        if field not in missing
                        and first_item.get(field) != second_item.get(field)
                    ]
                    first_cost = first_costs.get(first_item["id"], {})
                    second_cost = second_costs.get(second_item["id"], {})
                    if missing or different:
                        status = "NOT_COMPARABLE"
                        amount_difference = None
                        price_currency = None
                    elif (
                        first_cost.get("amount") is None
                        or second_cost.get("amount") is None
                    ):
                        status = "COMPARABLE_BASIS_PRICE_UNKNOWN"
                        amount_difference = None
                        price_currency = None
                    elif first_cost.get("currency") != second_cost.get("currency"):
                        status = "COMPARABLE_BASIS_CURRENCY_MISMATCH"
                        amount_difference = None
                        price_currency = None
                    elif first_cost.get("status") != second_cost.get("status"):
                        status = "COMPARABLE_BASIS_DIFFERENT_PRICE_STATES"
                        amount_difference = None
                        price_currency = first_cost["currency"]
                    elif first_cost["status"] not in {"quote", "estimate", "committed"}:
                        status = "COMPARABLE_BASIS_NOT_PLANNING_PRICE"
                        amount_difference = None
                        price_currency = first_cost["currency"]
                    else:
                        status = "COMPARABLE_PRICE_INPUTS"
                        amount_difference = _json_number(
                            Decimal(str(second_cost["amount"]))
                            - Decimal(str(first_cost["amount"]))
                        )
                        price_currency = first_cost["currency"]
                    comparisons.append(
                        {
                            "comparison_group": group,
                            "candidate_ids": [first_plan["id"], second_plan["id"]],
                            "media_item_ids": [first_item["id"], second_item["id"]],
                            "status": status,
                            "missing_basis_fields": missing,
                            "different_basis_fields": different,
                            "price_currency": price_currency,
                            "price_states": [
                                first_cost.get("status"),
                                second_cost.get("status"),
                            ],
                            "second_minus_first_amount": amount_difference,
                            "effectiveness_ranking": "not_established",
                        }
                    )
    return comparisons


def _cancellation_scenario(
    value: Any,
    path: str,
    default_currency: str | None,
    tz: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    scenario = _mapping(value, path)
    state = scenario.get("status", "scenario")
    if not isinstance(state, str) or state not in {"scenario", "requested", "user_recorded", "unknown"}:
        raise ValueError(f"{path}.status is invalid")
    currency = _currency(scenario.get("currency"), f"{path}.currency", default_currency)
    amount_fields = (
        "committed_amount",
        "released_commitment",
        "actual_spend",
        "refund_amount",
        "fee_amount",
    )
    amounts = {
        field: _nonnegative(scenario[field], f"{path}.{field}")
        if scenario.get(field) is not None
        else None
        for field in amount_fields
    }
    if currency is None and any(amount is not None for amount in amounts.values()):
        raise ValueError(f"{path}.currency is required for cancellation amounts")
    if (
        amounts["committed_amount"] is not None
        and amounts["released_commitment"] is not None
        and amounts["released_commitment"] > amounts["committed_amount"]
    ):
        raise ValueError(f"{path}.released_commitment exceeds committed_amount")
    if (
        amounts["actual_spend"] is not None
        and amounts["refund_amount"] is not None
        and amounts["refund_amount"] > amounts["actual_spend"]
    ):
        raise ValueError(f"{path}.refund_amount exceeds actual_spend")
    net_actual = None
    if amounts["actual_spend"] is not None and amounts["refund_amount"] is not None:
        net_actual = amounts["actual_spend"] - amounts["refund_amount"]
    remaining_commitment = None
    if all(
        amounts[field] is not None
        for field in ("committed_amount", "released_commitment", "fee_amount")
    ):
        remaining_commitment = (
            amounts["committed_amount"]
            - amounts["released_commitment"]
            + amounts["fee_amount"]
        )
    total_cost = (
        net_actual + remaining_commitment
        if net_actual is not None and remaining_commitment is not None
        else None
    )
    requested_at = scenario.get("requested_at")
    cutoff_at = scenario.get("cutoff_at")
    if requested_at is not None:
        requested_at = _parse_time(requested_at, f"{path}.requested_at", tz)
    if cutoff_at is not None:
        cutoff_at = _parse_time(cutoff_at, f"{path}.cutoff_at", tz)
    if requested_at is not None and cutoff_at is not None:
        policy_window_status = (
            "within_declared_window" if requested_at <= cutoff_at else "after_declared_window"
        )
    else:
        policy_window_status = "window_unknown"
    return {
        "status": state,
        "commitment_basis": "unperformed_commitment_before_release_excludes_actual_spend",
        "requested_at": scenario.get("requested_at"),
        "cutoff_at": scenario.get("cutoff_at"),
        "policy_window_status": policy_window_status,
        "currency": currency,
        **{field: _json_number(amount) if amount is not None else None for field, amount in amounts.items()},
        "net_actual_after_refund": _json_number(net_actual) if net_actual is not None else None,
        "remaining_commitment_and_fee": (
            _json_number(remaining_commitment) if remaining_commitment is not None else None
        ),
        "total_cost_after_cancellation": _json_number(total_cost) if total_cost is not None else None,
        "notice_status": scenario.get("notice_status", "unknown"),
        "external_action": "not_performed_by_evaluator",
        **_refs(scenario, path),
    }


def _collaboration_comparisons(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = []
    delivery_fields = (
        "title",
        "quantity",
        "unit",
        "specification",
        "draft_due",
        "final_due",
        "acceptance",
    )
    rights_fields = (
        "media",
        "territories",
        "term_days",
        "term_start",
        "term_end",
        "paid_use",
        "editing_allowed",
        "derivative_use",
        "exclusivity",
        "withdrawal_terms",
    )

    def delivery_scope(candidate: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {field: item.get(field) for field in delivery_fields}
            for item in candidate["deliverables"]
        ]

    def metric_basis(observation: dict[str, Any]) -> tuple[Any, ...] | None:
        fields = (
            "platform",
            "metric",
            "unit",
            "denominator",
            "observation_window",
            "source_kind",
        )
        if any(observation.get(field) in (None, "") for field in fields):
            return None
        return tuple(observation[field] for field in fields)

    for first_index, first in enumerate(candidates):
        for second in candidates[first_index + 1 :]:
            same_project = first["project_id"] == second["project_id"]
            first_quote = first["quote"]
            second_quote = second["quote"]
            quote_status = "MISSING_COMPARABLE_QUOTE_BASIS"
            quote_difference = None
            quote_currency = None
            if first_quote and second_quote:
                quote_currency = first_quote.get("currency")
                if not same_project or first["task_role"] != second["task_role"]:
                    quote_status = "PROJECT_OR_ROLE_DIFFERS"
                elif first_quote.get("currency") != second_quote.get("currency"):
                    quote_status = "CURRENCY_DIFFERS"
                elif first_quote.get("status") != second_quote.get("status"):
                    quote_status = "QUOTE_STATUS_DIFFERS"
                elif delivery_scope(first) != delivery_scope(second) or any(
                    first["rights"].get(field) != second["rights"].get(field)
                    for field in rights_fields
                ):
                    quote_status = "DELIVERY_OR_RIGHTS_SCOPE_DIFFERS"
                elif first_quote.get("amount") is None or second_quote.get("amount") is None:
                    quote_status = "QUOTE_AMOUNT_UNKNOWN"
                else:
                    quote_status = "COMPARABLE_DECLARED_PRICE_BASIS"
                    quote_difference = _json_number(
                        Decimal(str(second_quote["amount"]))
                        - Decimal(str(first_quote["amount"]))
                    )
            shared_metrics = []
            for first_observation in first["observations"]:
                first_basis = metric_basis(first_observation)
                if first_basis is None or first_observation.get("value") is None:
                    continue
                for second_observation in second["observations"]:
                    if metric_basis(second_observation) != first_basis:
                        continue
                    if second_observation.get("value") is None:
                        continue
                    shared_metrics.append(
                        {
                            "basis": {
                                "platform": first_basis[0],
                                "metric": first_basis[1],
                                "unit": first_basis[2],
                                "denominator": first_basis[3],
                                "observation_window": first_basis[4],
                                "source_kind": first_basis[5],
                            },
                            "first_value": first_observation["value"],
                            "second_value": second_observation["value"],
                            "second_minus_first": _json_number(
                                Decimal(str(second_observation["value"]))
                                - Decimal(str(first_observation["value"]))
                            ),
                            "interpretation": "same_declared_raw_metric_basis_not_effectiveness_ranking",
                        }
                    )
            comparisons.append(
                {
                    "candidate_ids": [first["id"], second["id"]],
                    "project_scope_matches": same_project,
                    "role_matches": first["task_role"] == second["task_role"],
                    "quote_comparison_status": quote_status,
                    "quote_currency": quote_currency,
                    "quote_second_minus_first": quote_difference,
                    "shared_metric_comparisons": shared_metrics,
                    "composite_score": None,
                    "ranking": "not_created",
                }
            )
    return comparisons


def _evaluate_collaborations(
    brief: dict[str, Any], inputs: dict[str, Any], quotes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw = inputs.get("collaborators", inputs.get("collaborations"))
    collaborators = _rows(raw, "collaborators")
    if not collaborators:
        raise ValueError("collaboration_plan requires at least one project-bound collaborator")
    budget = _budget_context(brief)
    evaluated = []
    identities: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    limitations = [
        "昵称、粉丝数和账号名称不用于推断创作者身份、KOS/KOL类别或本案效果。",
        "方案生成不会联系创作者、发布内容、确认合作或付款。",
        "候选报价彼此为备选；除非输入明确标注组合，不把所有候选简单相加为项目预算。",
    ]
    for index, collaborator in enumerate(collaborators):
        path = f"collaborators[{index}]"
        project_id = _required_text(collaborator.get("project_id"), f"{path}.project_id")
        creator_id = _required_text(collaborator.get("creator_id"), f"{path}.creator_id")
        contact_permission = _contact_permission(collaborator, path, brief)
        raw_availability = collaborator.get("availability")
        availability = None
        if raw_availability is not None:
            availability_row = _mapping(raw_availability, f"{path}.availability")
            available_from = _optional_text(
                availability_row.get("from"), f"{path}.availability.from"
            )
            available_to = _optional_text(
                availability_row.get("to"), f"{path}.availability.to"
            )
            if available_from and available_to:
                start_time = _parse_time(
                    available_from,
                    f"{path}.availability.from",
                    brief.get("timezone"),
                )
                end_time = _parse_time(
                    available_to,
                    f"{path}.availability.to",
                    brief.get("timezone"),
                    end_of_date=True,
                )
                if end_time <= start_time:
                    raise ValueError(f"{path}.availability.to must follow from")
            availability = {
                "from": available_from,
                "to": available_to,
                "status": (
                    "user_declared_not_independently_verified"
                    if available_from and available_to
                    else "incomplete_window"
                ),
                **_refs(availability_row, f"{path}.availability"),
            }
        raw_client_availability = collaborator.get("client_availability")
        client_availability = None
        if raw_client_availability is not None:
            client_availability_row = _mapping(
                raw_client_availability,
                f"{path}.client_availability",
            )
            client_available_from = _optional_text(
                client_availability_row.get("from"),
                f"{path}.client_availability.from",
            )
            client_available_to = _optional_text(
                client_availability_row.get("to"),
                f"{path}.client_availability.to",
            )
            if client_available_from and client_available_to:
                client_start = _parse_time(
                    client_available_from,
                    f"{path}.client_availability.from",
                    brief.get("timezone"),
                )
                client_end = _parse_time(
                    client_available_to,
                    f"{path}.client_availability.to",
                    brief.get("timezone"),
                    end_of_date=True,
                )
                if client_end <= client_start:
                    raise ValueError(
                        f"{path}.client_availability.to must follow from"
                    )
            client_availability = {
                "from": client_available_from,
                "to": client_available_to,
                "status": (
                    "user_declared_client_disclosure"
                    if client_available_from and client_available_to
                    else "incomplete_window"
                ),
                **_refs(client_availability_row, f"{path}.client_availability"),
            }
        podcast_plan, podcast_gaps = _podcast_plan(collaborator, path)
        brand_fit = _optional_text(collaborator.get("brand_fit"), f"{path}.brand_fit")
        fit_concerns = collaborator.get("fit_concerns", [])
        if not isinstance(fit_concerns, list) or any(not isinstance(item, str) for item in fit_concerns):
            raise ValueError(f"{path}.fit_concerns must be a list of text")
        client_display_name = _optional_text(
            collaborator.get("client_display_name"),
            f"{path}.client_display_name",
        )
        client_fit_concerns = collaborator.get("client_fit_concerns", [])
        if not isinstance(client_fit_concerns, list) or any(
            not isinstance(item, str) for item in client_fit_concerns
        ):
            raise ValueError(f"{path}.client_fit_concerns must be a list of text")
        client_quote_raw = collaborator.get("client_quote")
        _client_quote(
            client_quote_raw,
            f"{path}.client_quote",
            budget["currency"],
        )
        accounts = collaborator.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError(f"{path}.accounts must list platform/account identities")
        public_accounts = []
        for account_index, raw_account in enumerate(accounts):
            account = _mapping(raw_account, f"{path}.accounts[{account_index}]")
            platform = _required_text(account.get("platform"), f"{path}.accounts[{account_index}].platform")
            account_id = _required_text(account.get("account_id"), f"{path}.accounts[{account_index}].account_id")
            identities[(project_id, platform, account_id)].append(collaborator["id"])
            public_accounts.append(
                {
                    "platform": platform,
                    "account_id": account_id,
                    "handle": account.get("handle"),
                    "identity_link_status": account.get("identity_link_status", "UNKNOWN"),
                    "role": account.get("role"),
                    "audience_scope": _optional_text(account.get("audience_scope"), f"{path}.accounts[{account_index}].audience_scope"),
                    "market": _optional_text(account.get("market"), f"{path}.accounts[{account_index}].market"),
                    "observation_window": deepcopy(account.get("observation_window")),
                    **_refs(account, f"{path}.accounts[{account_index}]"),
                }
            )
        quote_ref = collaborator.get("quote_id")
        if quote_ref is not None:
            lines = [{"id": collaborator["id"] + ":quote", "quote_id": quote_ref, "client_quote": client_quote_raw}]
        elif isinstance(collaborator.get("quote"), dict):
            quote = collaborator["quote"]
            quote_currency = _currency(quote.get("currency"), f"{path}.quote.currency", budget["currency"])
            if quote_currency is None:
                raise ValueError(f"{path}.quote.currency is required when no brief currency is set")
            quote_status = quote.get("status", "quote")
            if not isinstance(quote_status, str) or quote_status not in _QUOTE_STATES:
                raise ValueError(f"{path}.quote.status is invalid")
            lines = [
                {
                    "id": collaborator["id"] + ":quote",
                    "amount": quote.get("amount"),
                    "quantity": quote.get("quantity", 1),
                    "currency": quote_currency,
                    "status": quote_status,
                    "valid_until": quote.get("valid_until"),
                    "client_visible": quote.get("client_visible", False),
                    "client_quote": client_quote_raw or quote.get("client_quote"),
                    **_refs(quote, f"{path}.quote"),
                }
            ]
        elif client_quote_raw is not None:
            lines = [
                {
                    "id": collaborator["id"] + ":client_quote",
                    "client_quote": client_quote_raw,
                    **_refs(collaborator, path),
                }
            ]
        else:
            lines = []
        costs, totals, unknown_cost = _cost_lines(lines, f"{path}.quote", quotes, budget["currency"])
        deliverables = collaborator.get("deliverables", [])
        if not isinstance(deliverables, list):
            raise ValueError(f"{path}.deliverables must be a list")
        checks = []
        for deliverable_index, deliverable_value in enumerate(deliverables):
            item = _mapping(deliverable_value, f"{path}.deliverables[{deliverable_index}]")
            deliverable_id = _required_text(item.get("id"), f"{path}.deliverables[{deliverable_index}].id")
            missing_terms = []
            if item.get("quantity") is None:
                missing_terms.append("数量")
            else:
                _positive(item["quantity"], f"{path}.deliverables[{deliverable_index}].quantity")
            required_terms = (
                ("specification", "规格"),
                ("draft_due", "初稿期限"),
                ("final_due", "最终交付期限"),
                ("acceptance", "验收标准/责任人"),
            )
            for key, label in required_terms:
                if item.get(key) in (None, "", []):
                    missing_terms.append(label)
            checks.append(
                {
                    "id": deliverable_id,
                    "title": item.get("title"),
                    "quantity": item.get("quantity"),
                    "unit": item.get("unit"),
                    "specification": item.get("specification"),
                    "draft_due": item.get("draft_due"),
                    "final_due": item.get("final_due"),
                    "acceptance": deepcopy(item.get("acceptance")),
                    "missing_terms": missing_terms,
                    **_refs(item, f"{path}.deliverables[{deliverable_index}]"),
                }
            )
        rights = collaborator.get("rights", {}) or {}
        rights = _mapping(rights, f"{path}.rights")
        rights_required = {
            "media": "可使用媒体",
            "territories": "地域范围",
            "paid_use": "付费投放权限",
            "editing_allowed": "剪辑/改编权限",
            "derivative_use": "衍生使用权限",
            "exclusivity": "排他范围",
            "withdrawal_terms": "撤回条件",
        }
        missing_rights = [label for key, label in rights_required.items() if rights.get(key) in (None, "", [])]
        if rights.get("term_days") is not None:
            _positive(rights["term_days"], f"{path}.rights.term_days")
        elif rights.get("term_start") is not None and rights.get("term_end") is not None:
            rights_start = _parse_time(rights["term_start"], f"{path}.rights.term_start", brief.get("timezone"))
            rights_end = _parse_time(
                rights["term_end"],
                f"{path}.rights.term_end",
                brief.get("timezone"),
                end_of_date=True,
            )
            if rights_end <= rights_start:
                raise ValueError(f"{path}.rights.term_end must follow term_start")
        else:
            missing_rights.append("使用期限")
        observations = collaborator.get("observations", [])
        if not isinstance(observations, list):
            raise ValueError(f"{path}.observations must be a list")
        public_observations = []
        for observation_index, observation_value in enumerate(observations):
            observation = _mapping(observation_value, f"{path}.observations[{observation_index}]")
            for numeric_key in ("value", "numerator", "denominator"):
                if numeric_key == "denominator" and isinstance(observation.get(numeric_key), str):
                    continue
                if observation.get(numeric_key) is not None:
                    parser = _finite if numeric_key == "value" else _nonnegative
                    parser(observation[numeric_key], f"{path}.observations[{observation_index}].{numeric_key}")
            metric_account = observation.get("account_id")
            metric_platform = observation.get("platform")
            known_identity = any(
                account["account_id"] == metric_account
                and account["platform"] == metric_platform
                for account in public_accounts
            )
            public_observations.append(
                {
                    "platform": metric_platform,
                    "account_id": metric_account,
                    "metric": observation.get("metric"),
                    "unit": _optional_text(
                        observation.get("unit"),
                        f"{path}.observations[{observation_index}].unit",
                    ),
                    "value": observation.get("value"),
                    "numerator": observation.get("numerator"),
                    "denominator": observation.get("denominator"),
                    "same_cohort": observation.get("same_cohort"),
                    "observation_window": observation.get("observation_window"),
                    "source_kind": observation.get("source_kind", "UNKNOWN"),
                    "identity_matches_listed_account": known_identity,
                    "rate": None,
                    "rate_status": (
                        "COHORT_NOT_CONFIRMED"
                        if observation.get("numerator") is not None and observation.get("denominator") is not None
                        else "DENOMINATOR_UNKNOWN"
                    ),
                    **_refs(observation, f"{path}.observations[{observation_index}]"),
                }
            )
            if not known_identity:
                limitations.append(f"合作候选 {collaborator['id']} 的一项观测未匹配到该候选列出的平台账号。")
            numerator = observation.get("numerator")
            denominator = observation.get("denominator")
            if observation.get("same_cohort") is not None and type(observation["same_cohort"]) is not bool:
                raise ValueError(f"{path}.observations[{observation_index}].same_cohort must be boolean")
            if observation.get("same_cohort") is True and numerator is not None and denominator is not None:
                if type(denominator) not in (int, float):
                    public_observations[-1]["rate_status"] = "DENOMINATOR_NOT_NUMERIC"
                elif denominator == 0:
                    public_observations[-1]["rate_status"] = "ZERO_DENOMINATOR"
                elif known_identity and observation.get("observation_window"):
                    rate = _nonnegative(
                        numerator,
                        f"{path}.observations[{observation_index}].numerator",
                    ) / _nonnegative(
                        denominator,
                        f"{path}.observations[{observation_index}].denominator",
                    )
                    public_observations[-1]["rate"] = _json_number(rate)
                    public_observations[-1]["rate_status"] = "USER_DECLARED_SHARED_COHORT"
                    limitations.append("合作观测比率仅根据用户声明的同一cohort计算，不构成因果增量或独立核验。")
                else:
                    public_observations[-1]["rate_status"] = "IDENTITY_OR_WINDOW_INCOMPLETE"
        project_budget = _budget_check(
            budget,
            totals,
            unknown_cost=unknown_cost or not any(
                line["status"] in {"quote", "estimate", "committed"}
                for line in costs
            ),
            existing_committed=_existing_committed(
                quotes,
                currency=budget["currency"],
                exclude={quote_ref} if quote_ref else set(),
            ),
        )
        gaps = []
        gaps.extend(podcast_gaps)
        if not collaborator.get("role"):
            gaps.append("本项目角色/职责待确认")
        if not brand_fit:
            gaps.append("品牌、内容与受众适配理由待补")
        if availability is None:
            gaps.append("档期可用性待确认")
        elif availability["status"] == "incomplete_window":
            gaps.append("候选档期只提供开始或结束一端，范围待补完整")
        if missing_rights:
            gaps.append("权益范围待确认：" + ", ".join(missing_rights))
        if any(check["missing_terms"] for check in checks):
            gaps.append("交付数量、规格、时限或验收责任待补")
        active_quote = next(
            (
                line
                for line in costs
                if line["status"] == "quote" and line.get("valid_until")
            ),
            None,
        )
        if not any(
            line["status"] in {"quote", "estimate", "committed"}
            for line in costs
        ):
            gaps.append("个人报价或有效估计未提供")
        if active_quote and brief.get("start_date"):
            quote_end = _parse_time(
                active_quote["valid_until"],
                f"{path}.quote.valid_until",
                brief.get("timezone"),
                end_of_date=True,
            )
            project_start = _parse_time(brief["start_date"], "brief.start_date", brief.get("timezone"))
            if quote_end < project_start:
                gaps.append("报价有效期早于项目开始日，需更新报价")
        evaluated.append(
            {
                "id": collaborator["id"],
                "project_id": project_id,
                "creator_id": creator_id,
                "display_name": collaborator.get("display_name"),
                "display_label": (
                    collaborator.get("display_name") or f"合作候选 {index + 1}"
                ),
                "client_display_label": (
                    client_display_name or f"合作候选 {index + 1}"
                ),
                "client_display_name_provided": client_display_name is not None,
                "task_role": collaborator.get("role"),
                "identity_link_status": collaborator.get("identity_link_status", "UNKNOWN"),
                "brand_fit": brand_fit,
                "fit_concerns": deepcopy(fit_concerns),
                "client_fit_concerns": deepcopy(client_fit_concerns),
                "accounts": public_accounts,
                "availability": availability,
                "client_availability": client_availability,
                "contact_permission": contact_permission,
                "podcast_plan": podcast_plan,
                "quote": costs[0] if costs else None,
                "deliverables": checks,
                "rights": deepcopy(rights),
                "missing_rights": missing_rights,
                "observations": public_observations,
                "budget_check": project_budget,
                "gaps": gaps,
                **_refs(collaborator, path),
            }
        )
    identity_conflicts = [
        {"project_id": project, "platform": platform, "account_id": account, "collaborator_ids": ids}
        for (project, platform, account), ids in identities.items()
        if len(set(ids)) > 1
    ]
    if identity_conflicts:
        limitations.append("同一项目平台账号被关联到多个候选身份，须先消歧；未自动合并或覆盖。")
    collaboration_comparisons = _collaboration_comparisons(evaluated)
    if collaboration_comparisons:
        limitations.append("候选对照只比较相同项目、合作角色、交付/权益和原生指标口径；没有综合分、默认赢家或效果预测。")
    for row in evaluated:
        if row["missing_rights"]:
            limitations.append(f"{row['display_label']} 的缺失权益不视为已授权使用。")
        if not row["source_refs"] and not row["evidence_refs"]:
            limitations.append(f"{row['display_label']} 的身份和商业条件来自用户输入，未独立核验。")
    terms_candidates = [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "podcast_plan": row["podcast_plan"],
            "quote": row["quote"],
            "deliverables": row["deliverables"],
            "rights": row["rights"],
            "missing_rights": row["missing_rights"],
            "budget_check": row["budget_check"],
        }
        for row in evaluated
    ]
    observation_candidates = [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "creator_id": row["creator_id"],
            "accounts": row["accounts"],
            "podcast_plan": row["podcast_plan"],
            "observations": row["observations"],
            "role": row["task_role"],
            "gaps": row["gaps"],
        }
        for row in evaluated
    ]
    scope_lines = [
        f"- 项目目标：{brief.get('goal') or '待补'}",
        "",
        "创作者和平台账号按本项目输入逐项保留；昵称相同不构成跨平台身份依据。",
    ]
    terms_lines = [
        "报价是候选项目条件，不代表联系、委托、付款或外部权益已批准。",
    ]
    observation_lines = [
        "指标按平台账号、来源类型、观察窗口和分母逐项保留；不把不同平台或阶段的数值拼成一个漏斗。",
    ]
    for row in evaluated:
        account_text = ", ".join(
            f"{account['platform']}:{account['account_id']}"
            for account in row["accounts"]
        )
        availability = row["availability"]
        availability_text = (
            f"{availability['from'] or '未知'} — {availability['to'] or '未知'}"
            if availability
            else "未提供"
        )
        scope_lines.append(
            f"- {row['display_label']} / 当前工作；"
            f"本项目角色 {row['task_role'] or '待确认'}；账号 {account_text}；"
            f"候选档期 {availability_text}；"
            f"播客模式 {row['podcast_plan']['mode_label'] if row['podcast_plan'] else '不适用/未指定'}；"
            f"联系许可 {_contactability_label(row['contact_permission']['contactability'])}；"
            f"身份依据：{_reference_markdown(row)}"
        )
        scope_lines.append(
            f"  - 品牌/内容适配：{row['brand_fit'] or '待补'}；"
            f"适配风险或反证：{_display_list(row['fit_concerns'])}"
        )
        for account in row["accounts"]:
            if account["audience_scope"] or account["market"]:
                scope_lines.append(
                    f"  - {account['platform']}受众/市场："
                    f"{account['audience_scope'] or '未知'} / {account['market'] or '未知'}；"
                    f"观察窗 {account['observation_window'] or '未知'}；{_reference_markdown(account)}"
                )
        rights = row["rights"]
        rights_summary = (
            f"媒体 {_rights_value_summary(rights.get('media'), _RIGHTS_MEDIA_LABELS)}；"
            f"地域 {_rights_value_summary(rights.get('territories'), _RIGHTS_TERRITORY_LABELS)}；"
            f"期限 {_rights_term_summary(rights)}；付费使用 {_permission_label(rights.get('paid_use'))}；"
            f"改编 {_permission_label(rights.get('editing_allowed'))}；衍生 {_display(rights.get('derivative_use'))}；"
            f"排他 {_display(rights.get('exclusivity'))}；撤回 {_display(rights.get('withdrawal_terms'))}"
        )
        terms_lines.extend(
            [
                "",
                f"## {row['display_label']} / 当前工作",
                f"- 报价：{_cost_markdown([row['quote']] if row['quote'] else [])}",
                f"- 播客合作模式：{row['podcast_plan']['mode_label'] if row['podcast_plan'] else '未指定'}",
            f"- 权益范围：{rights_summary}",
                f"- 缺失权益：{_display_list(row['missing_rights'])}",
                f"- 交付数与验收：{_display_list([item.get('quantity') for item in row['deliverables']])}；"
                f"{_display_list([item.get('acceptance') for item in row['deliverables']])}",
                f"- 计划缺口：{_display_list(row['gaps'])}",
            ]
        )
        if row["podcast_plan"]:
            podcast = row["podcast_plan"]
            terms_lines.append("- 模式条件：")
            for term_key in podcast["required_term_keys"]:
                term_value = podcast["mode_terms"][term_key]
                terms_lines.append(
                    f"  - {_PODCAST_MODE_TERM_LABELS[term_key]}：{_display(term_value)}"
                )
            terms_lines.append(
                f"- 播客模式待补：{_display_list(podcast['missing_mode_terms'])}"
            )
        for item in row["deliverables"]:
            terms_lines.append(
                f"  - {item.get('title') or '交付项'}：{item['quantity'] or '数量未知'} {item.get('unit') or ''}；"
                f"规格 {item.get('specification') or '待补'}；初稿 {item.get('draft_due') or '待补'}；"
                f"终稿 {item.get('final_due') or '待补'}；验收 { _acceptance_summary(item.get('acceptance')) }"
            )
        observation_lines.extend(
            [
                "",
                f"## {row['display_label']} / 当前工作",
            ]
        )
        if row["podcast_plan"]:
            observation_lines.append("### 播客五力（逐项保留，不合成评分）")
            for power in row["podcast_plan"]["powers"].values():
                value = power["value"]
                observed_value = (
                    f"{value} {power['unit'] or ''}".strip()
                    if value is not None
                    else "数值未知"
                )
                denominator = (
                    f"；分母 {power['denominator']}"
                    if power["denominator"] is not None
                    else ""
                )
                observation_lines.append(
                    f"- {power['label']}：{power['metric_definition'] or '指标定义未知'}；"
                    f"{observed_value}{denominator}；窗口 {power['observation_window'] or '未知'}；"
                    f"来源类型 {_source_kind_label(power['source_kind'])}；{_reference_markdown(power)}"
                )
            if row["podcast_plan"]["power_gaps"]:
                observation_lines.append(
                    "- 待补：" + _display_list(row["podcast_plan"]["power_gaps"])
                )
        if not row["observations"]:
            observation_lines.append("- 尚无按平台账号和观察窗口提供的指标。")
        for observation in row["observations"]:
            value = observation["value"]
            numerator = observation["numerator"]
            denominator = observation["denominator"]
            observed_value = (
                f"{value} {observation['metric']}"
                if value is not None
                else f"{numerator if numerator is not None else '未知'} / "
                f"{denominator if denominator is not None else '分母未知'} "
                f"{observation['metric'] or ''}"
            )
            observation_lines.append(
                f"- {observation['platform'] or '平台未知'}:{observation['account_id'] or '账号未知'}；"
                f"{observed_value}；窗口 {observation['observation_window'] or '未知'}；"
                f"比率说明 {_observation_rate_label(observation['rate_status'])}；{_reference_markdown(observation)}"
            )
    observation_lines.extend(
        [
            "",
            "### 合作候选对照（逐项口径，不生成综合分）",
        ]
    )
    evaluated_by_id = {row["id"]: row for row in evaluated}
    quote_comparison_labels = {
        "MISSING_COMPARABLE_QUOTE_BASIS": "缺少同口径有效金额，不能比较价格",
        "PROJECT_OR_ROLE_DIFFERS": "项目或合作角色不同，不作价格比较",
        "CURRENCY_DIFFERS": "币种不同，不作换算或比较",
        "QUOTE_STATUS_DIFFERS": "报价/估计/承诺状态不同，不作同类比较",
        "DELIVERY_OR_RIGHTS_SCOPE_DIFFERS": "交付或权益范围不同，不作同类比较",
        "QUOTE_AMOUNT_UNKNOWN": "金额未知，不作价格比较",
        "COMPARABLE_DECLARED_PRICE_BASIS": "声明的项目、交付、权益、状态和币种一致",
    }
    for comparison in collaboration_comparisons:
        first = evaluated_by_id[comparison["candidate_ids"][0]]
        second = evaluated_by_id[comparison["candidate_ids"][1]]
        quote_line = quote_comparison_labels[comparison["quote_comparison_status"]]
        if comparison["quote_second_minus_first"] is not None:
            quote_line += (
                f"；第二候选减第一候选 {comparison['quote_second_minus_first']} "
                f"{comparison['quote_currency'] or ''}"
            )
        metric_lines = [
            f"{item['basis']['platform']} / {item['basis']['metric']} "
            f"({item['basis']['unit']})：{item['first_value']} → {item['second_value']}；"
            f"差值 {item['second_minus_first']}，仅为同口径原始值差，不是效果排序"
            for item in comparison["shared_metric_comparisons"]
        ]
        observation_lines.append(
            f"- {first['display_label']} ↔ {second['display_label']}：{quote_line}；"
            f"同口径指标 {_display_list(metric_lines) if metric_lines else '没有满足同平台、指标定义、单位、分母、窗口和来源类型的共同观测'}。"
        )
    if not collaboration_comparisons:
        observation_lines.append("- 当前只有一个候选，暂不生成候选间比较。")
    for row in evaluated:
        observation_lines.append(
            f"- {row['display_label']} 的角色 {row['task_role'] or '待确认'}；"
            f"平台 {_display_list(sorted({account['platform'] for account in row['accounts']}))}；"
            f"权益缺口 {_display_list(row['missing_rights'])}；"
            f"其他待办 {_display_list(row['gaps'])}"
        )
    scope_lines.append("\n未发送任何联系、邀请、合作确认或发布请求。")
    client_citations = _client_citations(inputs)
    client_scope_lines = [
        f"- 项目目标：{brief.get('goal') or '待补'}",
        "候选名单和公开定位以本工作关联资料与用户输入为依据；客户版不显示私有联系许可、平台账号ID或内部身份记录。",
    ]
    client_terms_lines = [
        "合作模式、供应商底价和内部议价记录默认不对客户展示；仅使用明确填写的对客报价。",
    ]
    client_observation_lines = [
        "平台指标保留在候选评估中；只有明确授权给客户引用的精确资料片段才会连同指标口径展示。",
    ]
    visible_quote_count = 0
    hidden_observation_count = 0
    for row in evaluated:
        platform_names = _display_list(sorted({account["platform"] for account in row["accounts"]}))
        client_scope_lines.extend(
            [
                "",
                f"## {row['client_display_label']}",
                f"- 本项目角色：{row['task_role'] or '待确认'}；播客模式：{row['podcast_plan']['mode_label'] if row['podcast_plan'] else '未指定'}",
                f"- 涉及平台：{platform_names}",
                f"- 品牌/内容/受众适配理由（用户输入，待核）：{row['brand_fit'] or '待补'}",
                f"- 客户可见风险或待确认项：{_display_list(row['client_fit_concerns'])}",
            ]
        )
        if row["client_availability"] and row["client_availability"]["status"] == "user_declared_client_disclosure":
            client_scope_lines.append(
                f"- 客户版可披露档期（用户提供）："
                f"{row['client_availability']['from']} — {row['client_availability']['to']}。"
            )
        terms_lines_client = [
            "",
            f"## {row['client_display_label']}",
            f"- 对客报价：\n{_client_cost_markdown([row['quote']] if row['quote'] else [])}",
            f"- 权益范围：媒体 {_rights_value_summary(row['rights'].get('media'), _RIGHTS_MEDIA_LABELS)}；"
            f"地域 {_rights_value_summary(row['rights'].get('territories'), _RIGHTS_TERRITORY_LABELS)}；"
            f"期限 {_rights_term_summary(row['rights'])}；"
            f"付费使用 {_permission_label(row['rights'].get('paid_use'))}；"
            f"改编 {_permission_label(row['rights'].get('editing_allowed'))}；"
            f"排他 {_display(row['rights'].get('exclusivity'))}；撤回 {_display(row['rights'].get('withdrawal_terms'))}",
            f"- 未确认权益：{_display_list(row['missing_rights'])}",
        ]
        for deliverable in row["deliverables"]:
            terms_lines_client.append(
                f"- 交付：{deliverable.get('title') or '未命名交付物'}；"
                f"数量 {deliverable.get('quantity') if deliverable.get('quantity') is not None else '未知'} {deliverable.get('unit') or ''}；"
                f"规格 {_display(deliverable.get('specification'))}；"
                f"初稿 {_display(deliverable.get('draft_due'))}；终稿 {_display(deliverable.get('final_due'))}；"
                f"验收 {_acceptance_summary(deliverable.get('acceptance'))}"
            )
        client_terms_lines.extend(terms_lines_client)
        visible_quote_count += int(bool(row["quote"] and row["quote"].get("client_quote")))
        for observation in row["observations"]:
            if _client_references_authorized(observation, client_citations):
                observed_value = (
                    f"{observation['value']} {observation['metric'] or ''}".strip()
                    if observation["value"] is not None
                    else f"{observation['numerator'] if observation['numerator'] is not None else '未知'} / "
                    f"{observation['denominator'] if observation['denominator'] is not None else '分母未知'} "
                    f"{observation['metric'] or ''}"
                )
                client_observation_lines.append(
                    f"- {row['client_display_label']} · {observation['platform'] or '平台未知'}："
                    f"{observed_value}；窗口 {observation['observation_window'] or '未知'}；"
                    f"分母/同群说明 {_observation_rate_label(observation['rate_status'])}。"
                )
            else:
                hidden_observation_count += 1
    client_limitations = []
    if visible_quote_count == 0:
        client_limitations.append("未提供明确的对客报价/估计；客户版不披露创作者供应商报价，也不反推客户价格。")
    else:
        client_limitations.append("客户版只包含明确标记的对客报价，不将供应商价格或采购成本推算成对客金额。")
    if hidden_observation_count:
        client_limitations.append(f"有 {hidden_observation_count} 项平台观测没有匹配客户引用授权，未进入客户版。")
    if any(not row["client_display_name_provided"] for row in evaluated):
        client_limitations.append("未明确提供对客显示名称的候选以序号呈现，避免把内部名称当作已获准公开身份。")
    if any(row["fit_concerns"] and not row["client_fit_concerns"] for row in evaluated):
        client_limitations.append("内部适配备注不会自动公开；需逐项确认可以向客户披露的风险或待确认条件。")
    if any(row["availability"] and not row["client_availability"] for row in evaluated):
        client_limitations.append("内部候选档期不会自动披露；需明确提供获准给客户看的档期。")
    if any(
        row["client_availability"]
        and row["client_availability"]["status"] == "incomplete_window"
        for row in evaluated
    ):
        client_limitations.append("对客档期只提供一端，未进入客户正文；请补全开始和结束时间。")
    if any(row["contact_permission"]["status"] != "unknown" for row in evaluated):
        client_limitations.append("候选联系许可和私有联系方式不在客户版中；候选计划不代表已联系或已获批。")
    if not client_citations:
        client_limitations.append("未提供客户引用授权；客户版不附资料原件引文或内部来源标识。")
    return {
        "title": f"创作者合作计划：{brief.get('goal') or '待定义目标'}",
        "sections": [
            _section(
                "collaboration_scope",
                "项目、创作者与平台账号",
                "\n".join(scope_lines),
                {
                    "brief": _brief_data(brief),
                    "collaborators": evaluated,
                    "identity_conflicts": identity_conflicts,
                },
                client_body="\n".join(client_scope_lines),
            ),
            _section(
                "collaboration_terms",
                "报价、交付、验收与权益",
                "\n".join(terms_lines),
                {"collaborators": terms_candidates, "quotes": _public_quotes(quotes)},
                client_body="\n".join(client_terms_lines),
            ),
            _section(
                "collaboration_observations",
                "分平台观测与候选比较",
                "\n".join(observation_lines),
                {
                    "collaborators": observation_candidates,
                    "comparisons": collaboration_comparisons,
                    "limitations": _dedupe(limitations),
                },
                client_body="\n".join(client_observation_lines),
            ),
        ],
        "limitations": _dedupe(limitations),
        "client_limitations": _dedupe(client_limitations),
        "client_citations": client_citations,
    }


def _parse_zone(brief: dict[str, Any]):
    value = brief.get("timezone")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("campaign_schedule requires brief.timezone")
    return _as_zone(value, "brief.timezone")


def _as_zone(value: Any, path: str):
    if value is None:
        return None
    if isinstance(value, _TclTimezone):
        return value
    if hasattr(value, "utcoffset"):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be an IANA timezone")
    try:
        return ZoneInfo(value)
    except (ModuleNotFoundError, ZoneInfoNotFoundError, ValueError):
        if _ZONE_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{path} must be a valid IANA timezone")
        try:
            return _TclTimezone(value)
        except (ImportError, RuntimeError, ValueError) as zone_error:
            raise ValueError(f"{path} must be a valid IANA timezone") from zone_error


class _TclTimezone:
    """Use the interpreter's bundled IANA rules when Windows has no TZif path."""

    __slots__ = ("key", "_clock")

    def __init__(self, key: str) -> None:
        from tkinter import Tcl, TclError

        self.key = key
        self._clock = Tcl()
        try:
            self._offset_at(0)
        except TclError as exc:
            raise ValueError(f"unknown IANA timezone: {key}") from exc

    def _offset_at(self, epoch_seconds: int) -> int:
        value = self._clock.call(
            "clock",
            "format",
            str(epoch_seconds),
            "-timezone",
            self.key,
            "-format",
            "%z",
        )
        match = re.fullmatch(r"([+-])(\d{2})(\d{2})", str(value))
        if match is None:
            raise ValueError(f"timezone {self.key} has an unsupported UTC offset")
        sign = 1 if match.group(1) == "+" else -1
        return sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)

    def _format_local_seconds(self, epoch_seconds: int) -> str:
        return str(
            self._clock.call(
                "clock",
                "format",
                str(epoch_seconds),
                "-timezone",
                self.key,
                "-format",
                "%Y-%m-%dT%H:%M:%S",
            )
        )

    @staticmethod
    def _epoch_seconds(value: datetime) -> int:
        utc_value = value.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = utc_value - epoch
        return delta.days * 86400 + delta.seconds

    def local_to_utc(self, value: datetime) -> datetime:
        wall_value = value.replace(tzinfo=None)
        wall_seconds = self._epoch_seconds(wall_value.replace(tzinfo=timezone.utc))
        wall_text = wall_value.strftime("%Y-%m-%dT%H:%M:%S")
        offsets = {
            self._offset_at(wall_seconds + delta)
            for delta in (-172800, -86400, -43200, 0, 43200, 86400, 172800)
        }
        valid_epochs = {
            candidate
            for offset in offsets
            if self._format_local_seconds(candidate := wall_seconds - offset) == wall_text
        }
        if len(valid_epochs) != 1:
            condition = "ambiguous" if valid_epochs else "nonexistent"
            raise ValueError(f"{condition} local time in {self.key}: {wall_text}")
        epoch_seconds = next(iter(valid_epochs))
        return datetime.fromtimestamp(epoch_seconds, timezone.utc).replace(microsecond=wall_value.microsecond)

    def format(self, value: datetime) -> str:
        epoch_seconds = self._epoch_seconds(value)
        local = self._format_local_seconds(epoch_seconds)
        offset = self._offset_at(epoch_seconds)
        sign = "+" if offset >= 0 else "-"
        minutes = abs(offset) // 60
        offset_text = f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"
        fraction = f".{value.microsecond:06d}" if value.microsecond else ""
        return local + fraction + offset_text


def _parse_time(value: Any, path: str, tz: Any = None, *, end_of_date: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO-8601 date or datetime")
    tz = _as_zone(tz, path)
    is_date = _DATE_PATTERN.fullmatch(value) is not None
    try:
        if is_date:
            parsed = datetime.combine(date.fromisoformat(value), time.min)
            if end_of_date:
                parsed += timedelta(days=1)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a valid ISO-8601 date or datetime") from exc
    if parsed.tzinfo is None:
        if isinstance(tz, _TclTimezone):
            try:
                return tz.local_to_utc(parsed)
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from exc
        if tz is not None and not isinstance(tz, timezone):
            return _zone_local_to_utc(parsed, tz, path)
        if tz is None and is_date:
            # Date-only arithmetic depends on calendar dates, not a time zone.
            # Schedule operations pass the declared project zone.
            tz = timezone.utc
        if tz is None:
            raise ValueError(f"{path} requires a timezone")
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime, tz: Any) -> str:
    if isinstance(tz, _TclTimezone):
        return tz.format(value)
    if tz is not None:
        return value.astimezone(tz).isoformat()
    return value.isoformat()


def _zone_local_to_utc(value: datetime, tz: Any, path: str) -> datetime:
    wall = value.replace(tzinfo=None)
    candidates = set()
    for fold in (0, 1):
        local = wall.replace(tzinfo=tz, fold=fold)
        utc_value = local.astimezone(timezone.utc)
        if utc_value.astimezone(tz).replace(tzinfo=None) == wall:
            candidates.add(utc_value)
    if len(candidates) != 1:
        condition = "ambiguous" if candidates else "nonexistent"
        raise ValueError(f"{path}: {condition} local time requires explicit offset or correction")
    return next(iter(candidates))


def _resource_availability(resource: dict[str, Any], brief: dict[str, Any], tz: Any) -> list[tuple[datetime, datetime]]:
    raw = resource.get("availability")
    if raw is None:
        start = resource.get("available_from")
        end = resource.get("available_to")
        if start is None or end is None:
            return []
        rows = [{"start": start, "end": end}]
    else:
        if not isinstance(raw, list):
            raise ValueError(f"resource {resource['id']} availability must be a list")
        rows = raw
    result = []
    for index, row_value in enumerate(rows):
        row = _mapping(row_value, f"resource {resource['id']} availability[{index}]")
        start = _parse_time(row.get("start"), f"resource {resource['id']}.availability.start", tz)
        end = _parse_time(row.get("end"), f"resource {resource['id']}.availability.end", tz, end_of_date=True)
        if end <= start:
            raise ValueError(f"resource {resource['id']} availability end must follow start")
        result.append((start, end))
    return sorted(result)


def _run_schedule_milp(
    tasks: list[dict[str, Any]],
    task_by_id: dict[str, dict[str, Any]],
    resources: dict[str, dict[str, Any]],
    availability: dict[str, list[tuple[datetime, datetime]]],
    windows: dict[str, tuple[datetime, datetime]],
    durations: dict[str, timedelta | None],
    assignments: dict[str, list[tuple[str, ...]]],
    locked: dict[str, dict[str, Any]],
    task_mandatory: dict[str, bool],
    task_coverage: dict[str, list[str]],
    required_coverage: list[str],
    exclusions: list[tuple[str, str]],
    task_costs: dict[str, Decimal],
    shared_cost_pools: list[dict[str, Any]],
    budget_limit: Decimal | None,
    budget_declared: bool,
    benefit_unit: str | None,
    origin: datetime,
    horizon_end: datetime,
) -> dict[str, Any]:
    """Solve the continuous-time scheduling model and verify its decoded plan.

    Resource configurations are binary decisions. Each resource is expanded to
    a bounded number of interchangeable capacity lanes; tasks assigned to the
    same lane receive a disjunctive no-overlap constraint. Time variables use
    elapsed UTC hours and are not rounded to a scheduling grid.
    """
    try:
        import numpy as np
        import scipy
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix, vstack
    except Exception:
        return {
            "state": "SOLVER_UNAVAILABLE",
            "schedule": {},
            "selected_task_ids": [],
            "solver": None,
            "incomplete_reasons": ["MILP solver is not installed or unavailable."],
        }

    identities = [task["id"] for task in tasks]
    incomplete: list[str] = []
    infeasible_notes: list[str] = []
    task_configs: dict[str, list[dict[str, Any]]] = {}
    duration_hours: dict[str, float] = {}
    for identity in identities:
        duration = durations[identity]
        if duration is None:
            incomplete.append(f"Task {identity} has no duration estimate.")
            continue
        duration_value = duration.total_seconds() / 3600
        if not math.isfinite(duration_value) or duration_value <= 0:
            raise ValueError(f"Task {identity} duration is outside the solver range")
        duration_hours[identity] = duration_value
        if identity in locked:
            resources_for_lock = tuple(locked[identity]["resource_ids"])
            if any(resources[resource_id]["capacity"] is None for resource_id in resources_for_lock):
                incomplete.append(f"Locked task {identity} uses a resource with unknown capacity.")
            task_configs[identity] = [
                {
                    "option_index": locked[identity]["option_index"],
                    "resource_ids": resources_for_lock,
                    "window": None,
                    "locked": True,
                }
            ]
            continue

        earliest, deadline = windows[identity]
        if deadline <= earliest:
            infeasible_notes.append(f"任务 {identity} 的最早开始时间不早于截止时间。")
        elif duration > deadline - earliest:
            infeasible_notes.append(f"任务 {identity} 的工时超过声明的可用窗口。")
        configurations: list[dict[str, Any]] = []
        qualification_blocked = False
        for option_index, resource_ids in enumerate(assignments[identity]):
            capacity_unknown = [
                resource_id
                for resource_id in resource_ids
                if resources[resource_id]["capacity"] is None
            ]
            if capacity_unknown:
                incomplete.append(f"Task {identity} has an alternative with unknown resource capacity.")
                continue
            qualification_gaps = _missing_qualifications(
                task_by_id[identity], resource_ids, resources
            )
            if qualification_gaps:
                qualification_blocked = True
                continue
            if not resource_ids:
                common_windows = [(origin, horizon_end)]
            else:
                missing_availability = [
                    resource_id
                    for resource_id in resource_ids
                    if not availability[resource_id]
                ]
                if missing_availability:
                    incomplete.append(f"Task {identity} has a resource alternative with undeclared availability.")
                    continue
                common_windows = [(origin, horizon_end)]
                for resource_id in resource_ids:
                    intersections = []
                    for left, right in common_windows:
                        for available_start, available_end in availability[resource_id]:
                            overlap_start = max(left, available_start)
                            overlap_end = min(right, available_end)
                            if overlap_start < overlap_end:
                                intersections.append((overlap_start, overlap_end))
                    common_windows = intersections
                    if not common_windows:
                        break
            for available_start, available_end in common_windows:
                allowed_start = max(earliest, available_start)
                allowed_latest_start = min(deadline, available_end) - durations[identity]
                if allowed_start <= allowed_latest_start:
                    configurations.append(
                        {
                            "option_index": option_index,
                            "resource_ids": tuple(resource_ids),
                            "window": (allowed_start, allowed_latest_start),
                            "locked": False,
                        }
                    )
        if not configurations and qualification_blocked:
            infeasible_notes.append(f"任务 {identity} 没有满足资格要求的资源备选。")
        elif not configurations and not incomplete:
            infeasible_notes.append(f"任务 {identity} 在声明的时间与资源条件下没有可行组合。")
        task_configs[identity] = configurations

    for first, second in exclusions:
        if task_mandatory[first] and task_mandatory[second]:
            infeasible_notes.append("两项互斥任务都被声明为必须完成。")
    for segment in required_coverage:
        if not any(segment in task_coverage[identity] for identity in identities):
            infeasible_notes.append(f"必需覆盖项“{segment}”没有关联任务。")
    if budget_limit is not None:
        mandatory_cost = sum(
            (task_costs[identity] for identity in identities if task_mandatory[identity]),
            Decimal(0),
        )
        mandatory_cost += sum(
            (
                pool["amount"]
                for pool in shared_cost_pools
                if pool.get("committed")
                or any(task_mandatory[task_id] or task_id in locked for task_id in pool["task_ids"])
                if pool.get("amount") is not None
            ),
            Decimal(0),
        )
        if mandatory_cost > budget_limit:
            infeasible_notes.append("必须完成任务的已知费用已超过扣除预留与既有承诺后的预算额度。")

    if incomplete:
        return {
            "state": "INPUTS_INCOMPLETE",
            "schedule": {},
            "selected_task_ids": [],
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__},
            "incomplete_reasons": _dedupe(incomplete),
        }

    optional = [identity for identity in identities if not task_mandatory[identity]]
    if optional and budget_declared and budget_limit is None:
        return {
            "state": "INPUTS_INCOMPLETE",
            "schedule": {},
            "selected_task_ids": [],
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__},
            "incomplete_reasons": ["An optional task selection has an unresolved hard-budget cost."],
        }
    benefit_values = {
        identity: float(task_by_id[identity]["benefit"])
        for identity in optional
    }
    horizon_hours = max(
        (horizon_end - origin).total_seconds() / 3600,
        0.0,
    )
    absolute_time_hours = [
        abs((value - origin).total_seconds() / 3600)
        for pair in windows.values()
        for value in pair
    ]
    for interval in locked.values():
        absolute_time_hours.extend(
            abs((interval[field] - origin).total_seconds() / 3600)
            for field in ("start", "end")
        )
    big_m = max([horizon_hours, *absolute_time_hours, *duration_hours.values(), 1.0]) * 3 + 1

    variable_lower: list[float] = []
    variable_upper: list[float] = []
    variable_integrality: list[int] = []
    base_objective: list[float] = []

    def add_variable(lower: float, upper: float, *, integer: bool = False) -> int:
        identity = len(variable_lower)
        variable_lower.append(lower)
        variable_upper.append(upper)
        variable_integrality.append(1 if integer else 0)
        base_objective.append(0.0)
        return identity

    start_vars: dict[str, int] = {}
    selected_vars: dict[str, int] = {}
    config_vars: dict[tuple[str, int], int] = {}
    cmax_upper = max(
        [horizon_hours, *(abs((value - origin).total_seconds() / 3600) for value in [origin, horizon_end])]
        + [big_m]
    )
    cmax_var = add_variable(-big_m, cmax_upper)
    for identity in identities:
        if identity in locked:
            fixed_start_hours = (locked[identity]["start"] - origin).total_seconds() / 3600
            start_vars[identity] = add_variable(fixed_start_hours, fixed_start_hours)
        else:
            start_vars[identity] = add_variable(-big_m, big_m)
        selected_vars[identity] = add_variable(
            1.0 if task_mandatory[identity] or identity in locked else 0.0,
            1.0,
            integer=True,
        )
        for config_index, _config in enumerate(task_configs.get(identity, [])):
            config_vars[(identity, config_index)] = add_variable(0.0, 1.0, integer=True)

    shared_pool_vars = {
        pool["id"]: add_variable(
            1.0 if pool.get("committed") else 0.0,
            1.0,
            integer=True,
        )
        for pool in shared_cost_pools
    }

    rows: list[dict[int, float]] = []
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []

    def add_constraint(coefficients: dict[int, float], lower=-math.inf, upper=math.inf) -> None:
        rows.append({key: value for key, value in coefficients.items() if value != 0})
        lower_bounds.append(float(lower))
        upper_bounds.append(float(upper))

    lane_vars: dict[tuple[str, int, str, int], int] = {}
    lane_counts = {
        resource_id: min(resource["capacity"], max(1, len(tasks)))
        for resource_id, resource in resources.items()
        if resource["capacity"] is not None
    }
    for identity in identities:
        x = selected_vars[identity]
        option_sum: dict[int, float] = {x: -1.0}
        for config_index, config in enumerate(task_configs.get(identity, [])):
            y = config_vars[(identity, config_index)]
            option_sum[y] = 1.0
            if config["window"] is not None:
                allowed_start, allowed_latest = config["window"]
                lower = (allowed_start - origin).total_seconds() / 3600
                upper = (allowed_latest - origin).total_seconds() / 3600
                start = start_vars[identity]
                add_constraint({start: 1.0, y: -big_m}, lower=lower - big_m)
                add_constraint({start: 1.0, y: big_m}, upper=upper + big_m)
            for resource_id in config["resource_ids"]:
                count = lane_counts[resource_id]
                lane_ids = []
                for lane in range(count):
                    z = add_variable(0.0, 1.0, integer=True)
                    lane_vars[(identity, config_index, resource_id, lane)] = z
                    lane_ids.append(z)
                lane_row = {z: 1.0 for z in lane_ids}
                lane_row[y] = -1.0
                add_constraint(lane_row, lower=0.0, upper=0.0)
        add_constraint(option_sum, lower=0.0, upper=0.0)

    for segment in required_coverage:
        add_constraint(
            {
                selected_vars[identity]: 1.0
                for identity in identities
                if segment in task_coverage[identity]
            },
            lower=1.0,
        )
    for first, second in exclusions:
        add_constraint(
            {selected_vars[first]: 1.0, selected_vars[second]: 1.0},
            upper=1.0,
        )
    for identity in identities:
        for dependency in task_by_id[identity].get("dependencies", []):
            add_constraint(
                {selected_vars[identity]: 1.0, selected_vars[dependency]: -1.0},
                upper=0.0,
            )
            add_constraint(
                {
                    start_vars[dependency]: 1.0,
                    start_vars[identity]: -1.0,
                    selected_vars[identity]: big_m,
                },
                upper=big_m - duration_hours[dependency],
            )
    for identity in identities:
        add_constraint(
            {
                cmax_var: 1.0,
                start_vars[identity]: -1.0,
                selected_vars[identity]: -big_m,
            },
            lower=duration_hours[identity] - big_m,
        )

    for pool in shared_cost_pools:
        pool_var = shared_pool_vars[pool["id"]]
        linked = pool["task_ids"]
        for identity in linked:
            add_constraint(
                {selected_vars[identity]: 1.0, pool_var: -1.0},
                upper=0.0,
            )
        any_linked_selected = {pool_var: 1.0}
        for identity in linked:
            any_linked_selected[selected_vars[identity]] = -1.0
        add_constraint(any_linked_selected, upper=0.0)

    if budget_limit is not None:
        cost_row = {
            selected_vars[identity]: float(task_costs[identity])
            for identity in identities
            if task_costs.get(identity) is not None
        }
        for pool in shared_cost_pools:
            if pool.get("amount") is not None:
                cost_row[shared_pool_vars[pool["id"]]] = float(pool["amount"])
        add_constraint(cost_row, upper=float(budget_limit))

    for resource_id, lane_count in lane_counts.items():
        resource_users = [
            (identity, config_index)
            for identity in identities
            for config_index, config in enumerate(task_configs.get(identity, []))
            if resource_id in config["resource_ids"]
        ]
        for user_index, (first_task, first_config) in enumerate(resource_users):
            for second_task, second_config in resource_users[user_index + 1 :]:
                if first_task == second_task:
                    continue
                first_config_data = task_configs[first_task][first_config]
                second_config_data = task_configs[second_task][second_config]
                for lane in range(lane_count):
                    first_lane = lane_vars[(first_task, first_config, resource_id, lane)]
                    second_lane = lane_vars[(second_task, second_config, resource_id, lane)]
                    before = add_variable(0.0, 1.0, integer=True)
                    first_start = start_vars[first_task]
                    second_start = start_vars[second_task]
                    first_duration = duration_hours[first_task]
                    second_duration = duration_hours[second_task]
                    add_constraint(
                        {
                            first_start: 1.0,
                            second_start: -1.0,
                            before: big_m,
                            first_lane: big_m,
                            second_lane: big_m,
                        },
                        upper=3 * big_m - first_duration,
                    )
                    add_constraint(
                        {
                            second_start: 1.0,
                            first_start: -1.0,
                            before: -big_m,
                            first_lane: big_m,
                            second_lane: big_m,
                        },
                        upper=2 * big_m - second_duration,
                    )

    matrix_data = []
    matrix_rows = []
    matrix_columns = []
    for row_index, row in enumerate(rows):
        for column, value in row.items():
            matrix_rows.append(row_index)
            matrix_columns.append(column)
            matrix_data.append(value)
    matrix = coo_matrix(
        (matrix_data, (matrix_rows, matrix_columns)),
        shape=(len(rows), len(variable_lower)),
        dtype=float,
    ).tocsc()
    constraints = LinearConstraint(matrix, lower_bounds, upper_bounds)
    bounds = Bounds(variable_lower, variable_upper)
    integrality = np.asarray(variable_integrality, dtype=np.uint8)
    options = {"time_limit": 15.0, "presolve": True, "mip_rel_gap": 0.0}

    def solve(objective: list[float], extra_rows: list[tuple[dict[int, float], float, float]] = ()):
        if not extra_rows:
            active_constraints = constraints
        else:
            extra_data, extra_rows_index, extra_cols, extra_lb, extra_ub = [], [], [], [], []
            for offset, (coefficients, lb, ub) in enumerate(extra_rows):
                for column, value in coefficients.items():
                    extra_rows_index.append(offset)
                    extra_cols.append(column)
                    extra_data.append(value)
                extra_lb.append(lb)
                extra_ub.append(ub)
            extra_matrix = coo_matrix(
                (extra_data, (extra_rows_index, extra_cols)),
                shape=(len(extra_rows), len(variable_lower)),
                dtype=float,
            ).tocsc()
            active_constraints = LinearConstraint(
                vstack([matrix, extra_matrix], format="csc"),
                [*lower_bounds, *extra_lb],
                [*upper_bounds, *extra_ub],
            )
        return milp(
            c=np.asarray(objective, dtype=float),
            integrality=integrality,
            bounds=bounds,
            constraints=active_constraints,
            options=options,
        )

    optional = [identity for identity in identities if not task_mandatory[identity]]
    candidate = None
    objective_state = "MINIMIZE_PROJECT_COMPLETION_TIME"
    if optional:
        benefit_objective = [0.0] * len(variable_lower)
        for identity in optional:
            benefit_objective[selected_vars[identity]] = -benefit_values[identity]
        benefit_result = solve(benefit_objective)
        if benefit_result.status == 2:
            return {
                "state": "INFEASIBLE",
                "schedule": {},
                "selected_task_ids": [],
                "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__},
                "incomplete_reasons": _dedupe(infeasible_notes),
            }
        if benefit_result.x is None:
            return {
                "state": "LIMIT_REACHED_NO_INCUMBENT" if benefit_result.status == 1 else "SOLVER_ERROR",
                "schedule": {},
                "selected_task_ids": [],
                "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__},
                "incomplete_reasons": [str(benefit_result.message)],
            }
        if benefit_result.status != 0:
            candidate = benefit_result
            objective_state = "MAXIMIZE_USER_DECLARED_BENEFIT_NOT_PROVEN"
        else:
            best_benefit = sum(
                benefit_values[identity] * benefit_result.x[selected_vars[identity]]
                for identity in optional
            )
            benefit_row = {
                selected_vars[identity]: benefit_values[identity]
                for identity in optional
            }
            benefit_tolerance = 1e-8 * max(1.0, abs(best_benefit))
            extra_rows = [
                (
                    benefit_row,
                    best_benefit - benefit_tolerance,
                    best_benefit + benefit_tolerance,
                )
            ]
            completion_objective = [0.0] * len(variable_lower)
            completion_objective[cmax_var] = 1.0
            completion_result = solve(completion_objective, extra_rows)
            if completion_result.status == 2:
                return {
                    "state": "INDEPENDENT_CHECK_FAILED",
                    "schedule": {},
                    "selected_task_ids": [],
                    "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__},
                    "incomplete_reasons": ["The lexicographic completion stage contradicted the proven benefit stage."],
                }
            candidate = completion_result
            objective_state = (
                "OPTIMAL_USER_DECLARED_BENEFIT_THEN_MINIMUM_COMPLETION_TIME"
                if completion_result.status == 0
                else "FEASIBLE_USER_DECLARED_BENEFIT_NOT_TIME_OPTIMAL"
            )
    else:
        completion_objective = [0.0] * len(variable_lower)
        completion_objective[cmax_var] = 1.0
        candidate = solve(completion_objective)
        objective_state = (
            "MINIMUM_PROJECT_COMPLETION_TIME_PROVEN"
            if candidate.status == 0
            else "FEASIBLE_SCHEDULE_TIME_OPTIMALITY_NOT_PROVEN"
        )

    if candidate is None or candidate.x is None:
        state = (
            "INFEASIBLE"
            if candidate is not None and candidate.status == 2
            else "LIMIT_REACHED_NO_INCUMBENT"
            if candidate is not None and candidate.status == 1
            else "SOLVER_ERROR"
        )
        return {
            "state": state,
            "schedule": {},
            "selected_task_ids": [],
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": (
                _dedupe(infeasible_notes)
                if candidate is not None and candidate.status == 2
                else [str(candidate.message) if candidate is not None else "No solver result."]
            ),
        }

    selected_ids = {
        identity
        for identity in task_by_id
        if candidate.x[selected_vars[identity]] > 0.5
    }
    schedule: dict[str, dict[str, Any]] = {}
    for identity in selected_ids:
        chosen_configs = [
            config_index
            for config_index in range(len(task_configs.get(identity, [])))
            if candidate.x[config_vars[(identity, config_index)]] > 0.5
        ]
        if len(chosen_configs) != 1:
            return {
                "state": "INDEPENDENT_CHECK_FAILED",
                "schedule": {},
                "selected_task_ids": sorted(selected_ids),
                "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
                "incomplete_reasons": ["A selected task did not resolve to exactly one resource configuration."],
            }
        configuration = task_configs[identity][chosen_configs[0]]
        raw_start = candidate.x[start_vars[identity]]
        start_us = round(raw_start * 3_600_000_000)
        start = origin + timedelta(microseconds=start_us)
        end = start + durations[identity]
        if configuration["window"] is not None:
            allowed_start, allowed_latest = configuration["window"]
            if start < allowed_start or start > allowed_latest:
                return {
                    "state": "INDEPENDENT_CHECK_FAILED",
                    "schedule": {},
                    "selected_task_ids": sorted(selected_ids),
                    "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
                    "incomplete_reasons": ["Rounded solver times did not satisfy an exact declared window."],
                }
        schedule[identity] = {
            "start": start,
            "end": end,
            "resource_ids": configuration["resource_ids"],
            "locked": configuration["locked"],
            "option_index": configuration["option_index"],
        }

    if not set(identity for identity in identities if task_mandatory[identity]) <= selected_ids:
        return {
            "state": "INDEPENDENT_CHECK_FAILED",
            "schedule": {},
            "selected_task_ids": sorted(selected_ids),
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": ["A mandatory task was not selected."],
        }
    if any(
        dependency not in selected_ids
        for identity in selected_ids
        for dependency in task_by_id[identity].get("dependencies", [])
    ):
        return {
            "state": "INDEPENDENT_CHECK_FAILED",
            "schedule": {},
            "selected_task_ids": sorted(selected_ids),
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": ["A scheduled task has an unselected prerequisite."],
        }
    covered = {
        segment
        for identity in selected_ids
        for segment in task_coverage[identity]
    }
    if not set(required_coverage) <= covered:
        return {
            "state": "INDEPENDENT_CHECK_FAILED",
            "schedule": {},
            "selected_task_ids": sorted(selected_ids),
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": ["The plan did not cover every required user-declared output."],
        }
    if any(first in selected_ids and second in selected_ids for first, second in exclusions):
        return {
            "state": "INDEPENDENT_CHECK_FAILED",
            "schedule": {},
            "selected_task_ids": sorted(selected_ids),
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": ["The plan contains a user-declared mutually exclusive pair."],
        }
    if _resource_conflicts(resources, schedule, None):
        return {
            "state": "INDEPENDENT_CHECK_FAILED",
            "schedule": {},
            "selected_task_ids": sorted(selected_ids),
            "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
            "incomplete_reasons": ["The independent resource-capacity check failed."],
        }
    for pool in shared_cost_pools:
        if pool.get("committed") and not (set(pool["task_ids"]) & selected_ids):
            return {
                "state": "INDEPENDENT_CHECK_FAILED",
                "schedule": {},
                "selected_task_ids": sorted(selected_ids),
                "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
                "incomplete_reasons": ["A committed shared offer pool has no selected activation task."],
            }
    if budget_limit is not None:
        planned_cost = sum((task_costs[identity] for identity in selected_ids), Decimal(0))
        for pool in shared_cost_pools:
            pool_selected = bool(set(pool["task_ids"]) & selected_ids)
            if pool_selected and pool.get("amount") is not None:
                planned_cost += pool["amount"]
        if planned_cost > budget_limit:
            return {
                "state": "INDEPENDENT_CHECK_FAILED",
                "schedule": {},
                "selected_task_ids": sorted(selected_ids),
                "solver": {"library": "SciPy/HiGHS", "version": scipy.__version__, "objective": objective_state},
                "incomplete_reasons": ["The independent budget check failed."],
            }
    final_state = "OPTIMAL" if candidate.status == 0 else "FEASIBLE_NOT_OPTIMAL"
    objective_summary = (
        f"先按用户声明的{benefit_unit}最大化任务价值，再最小化项目完成时间"
        if optional and benefit_unit
        else "最小化项目完成时间"
    )
    return {
        "state": final_state,
        "schedule": schedule,
        "selected_task_ids": sorted(selected_ids),
        "benefit_unit": benefit_unit,
        "objective": objective_state,
        "objective_summary": objective_summary,
        "budget_constraint_active": budget_limit is not None,
        "objective_value": float(candidate.fun) if candidate.fun is not None else None,
        "solver": {"library": "SciPy.optimize.milp / HiGHS", "version": scipy.__version__},
        "time_model": "continuous elapsed UTC hours; no time-slot discretization",
        "incomplete_reasons": [],
    }


def _candidate_resource_sets(task: dict[str, Any], resources: dict[str, dict[str, Any]]) -> list[tuple[str, ...]]:
    options = task.get("resource_options")
    if options is None:
        resource_ids = task.get("resource_ids", [])
        if not isinstance(resource_ids, list) or any(not isinstance(value, str) for value in resource_ids):
            raise ValueError(f"task {task['id']}.resource_ids must be a list")
        options = [resource_ids]
    if not isinstance(options, list) or not options:
        raise ValueError(f"task {task['id']}.resource_options must be a non-empty list")
    result = []
    for option_index, option in enumerate(options):
        if not isinstance(option, list) or any(not isinstance(value, str) for value in option):
            raise ValueError(f"task {task['id']}.resource_options[{option_index}] must be a list")
        if len(option) != len(set(option)):
            raise ValueError(f"task {task['id']} contains duplicate resource IDs in one assignment")
        unknown = [identity for identity in option if identity not in resources]
        if unknown:
            raise ValueError(f"task {task['id']} refers to unknown resources: {', '.join(unknown)}")
        result.append(tuple(option))
    return result


def _topological_order(tasks: list[dict[str, Any]]) -> list[str]:
    by_id = {task["id"]: task for task in tasks}
    for task in tasks:
        dependencies = task.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(value, str) for value in dependencies):
            raise ValueError(f"task {task['id']}.dependencies must be a list of IDs")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"task {task['id']} repeats a dependency")
        if task["id"] in dependencies or any(value not in by_id for value in dependencies):
            raise ValueError(f"task {task['id']} has an unknown or self dependency")
    pending = set(by_id)
    complete: set[str] = set()
    order: list[str] = []
    while pending:
        ready = [
            task["id"]
            for task in tasks
            if task["id"] in pending
            and set(task.get("dependencies", [])).issubset(complete)
        ]
        if not ready:
            raise ValueError("campaign task dependencies contain a cycle")
        for identity in ready:
            pending.remove(identity)
            complete.add(identity)
            order.append(identity)
    return order


def _resource_conflicts(
    resources: dict[str, dict[str, Any]], placements: dict[str, dict[str, Any]], tz: Any
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for resource_id, resource in resources.items():
        if resource["capacity"] is None:
            continue
        intervals = []
        for task_id, placement in placements.items():
            if resource_id in placement.get("resource_ids", []):
                intervals.append((placement["start"], placement["end"], task_id))
        points = sorted({point for start, end, _ in intervals for point in (start, end)})
        for left, right in zip(points, points[1:]):
            active = sorted(task_id for start, end, task_id in intervals if start < right and end > left)
            if len(active) > resource["capacity"]:
                record = {
                    "resource_id": resource_id,
                    "capacity": resource["capacity"],
                    "task_ids": active,
                    "overlap_start": _format_time(left, tz),
                    "overlap_end": _format_time(right, tz),
                }
                if not conflicts or conflicts[-1] != record:
                    conflicts.append(record)
    return conflicts


def _execution_record(
    task: dict[str, Any],
    task_id: str,
    planned: dict[str, Any] | None,
    tz: Any,
) -> dict[str, Any]:
    """Project recorded execution/observation without turning it into an action."""
    path = f"tasks.{task_id}"
    actual_fields = {}
    parsed_actual: dict[str, datetime] = {}
    for key in ("actual_start", "actual_end", "actual_publish_at"):
        value = task.get(key)
        if isinstance(value, str) and value.strip().casefold() == "unknown":
            actual_fields[key] = None
            continue
        if value is not None:
            parsed_actual[key] = _parse_time(value, f"{path}.{key}", tz)
        actual_fields[key] = value
    timing_conflict = bool(
        parsed_actual.get("actual_start")
        and parsed_actual.get("actual_end")
        and parsed_actual["actual_end"] <= parsed_actual["actual_start"]
    )
    actual_duration_hours = None
    schedule_variance_hours = None
    if "actual_start" in parsed_actual and "actual_end" in parsed_actual and not timing_conflict:
        actual_seconds = (
            parsed_actual["actual_end"] - parsed_actual["actual_start"]
        ).total_seconds()
        actual_duration_hours = _json_number(
            Decimal(str(actual_seconds)) / Decimal(3600)
        )
    if planned and "actual_start" in parsed_actual:
        variance_seconds = (
            parsed_actual["actual_start"] - planned["start"]
        ).total_seconds()
        schedule_variance_hours = _json_number(
            Decimal(str(variance_seconds)) / Decimal(3600)
        )
    snapshots = task.get("metric_snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError(f"{path}.metric_snapshots must be a list")
    receipt_refs = task.get("external_receipt_refs", [])
    if receipt_refs is not None and not isinstance(receipt_refs, list):
        raise ValueError(f"{path}.external_receipt_refs must be a list or null")
    normalized_snapshots = []
    for index, raw in enumerate(snapshots):
        row = _mapping(raw, f"{path}.metric_snapshots[{index}]")
        if row.get("value") is not None:
            _finite(row["value"], f"{path}.metric_snapshots[{index}].value")
        if row.get("denominator") is not None and not isinstance(row["denominator"], str):
            _nonnegative(row["denominator"], f"{path}.metric_snapshots[{index}].denominator")
        if row.get("observed_at") is not None:
            _parse_time(
                row["observed_at"],
                f"{path}.metric_snapshots[{index}].observed_at",
                tz,
            )
        normalized_snapshots.append(
            {
                "metric": row.get("metric"),
                "value": row.get("value"),
                "unit": row.get("unit"),
                "denominator": deepcopy(row.get("denominator")),
                "observation_window": deepcopy(row.get("observation_window")),
                "observed_at": row.get("observed_at"),
                "source_kind": row.get("source_kind", "UNKNOWN"),
                **_refs(row, f"{path}.metric_snapshots[{index}]"),
            }
        )
    has_actual = bool(parsed_actual or normalized_snapshots or receipt_refs)
    return {
        **actual_fields,
        "actual_duration_hours": actual_duration_hours,
        "schedule_variance_hours": schedule_variance_hours,
        "timing_conflict": timing_conflict,
        "execution_status": task.get("execution_status", "UNKNOWN"),
        "receipt_status": task.get("receipt_status", "UNKNOWN"),
        "external_receipt_refs": deepcopy(receipt_refs),
        "metric_snapshots": normalized_snapshots,
        "record_status": (
            "user_supplied_execution_not_independently_verified"
            if has_actual
            else "no_actual_execution_record_supplied"
        ),
    }


def _id_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{path} must be a list of non-empty IDs")
    if len(value) != len(set(value)):
        raise ValueError(f"{path} contains duplicate IDs")
    return value


def _member_count(value: Any, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{path} must be a {qualifier} integer")
    return value


def _member_money(value: Any, path: str, default_currency: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _mapping(value, path)
    amount = (
        _nonnegative(row["amount"], f"{path}.amount")
        if row.get("amount") is not None
        else None
    )
    currency = _currency(row.get("currency"), f"{path}.currency", default_currency)
    return {
        "amount": _json_number(amount) if amount is not None else None,
        "currency": currency,
        **_refs(row, path),
    }


def _member_activation_snapshot(
    value: Any, path: str, brief: dict[str, Any], tz: Any
) -> dict[str, Any]:
    activation = _mapping(value, path)
    task_id = _required_text(activation.get("task_id"), f"{path}.task_id")
    channel = _required_text(activation.get("channel"), f"{path}.channel")
    purpose = _required_text(activation.get("purpose"), f"{path}.purpose")
    window = _mapping(activation.get("contact_window"), f"{path}.contact_window")
    window_start = _parse_time(window.get("start"), f"{path}.contact_window.start", tz)
    window_end = _parse_time(
        window.get("end"), f"{path}.contact_window.end", tz, end_of_date=True
    )
    if window_end <= window_start:
        raise ValueError(f"{path}.contact_window.end must follow start")
    planned_contacts = _member_count(
        activation.get("planned_contacts_per_member"),
        f"{path}.planned_contacts_per_member",
        minimum=1,
    )

    frequency = _mapping(activation.get("frequency_rule"), f"{path}.frequency_rule")
    period_days = _member_count(
        frequency.get("period_days"), f"{path}.frequency_rule.period_days", minimum=1
    )
    max_contacts = _member_count(
        frequency.get("max_contacts_per_member"),
        f"{path}.frequency_rule.max_contacts_per_member",
        minimum=1,
    )
    frequency_history = _mapping(
        frequency.get("history", {}), f"{path}.frequency_rule.history"
    )
    frequency_history_status = frequency_history.get("status", "unknown")
    if not isinstance(frequency_history_status, str) or frequency_history_status not in {"complete", "partial", "unknown"}:
        raise ValueError(f"{path}.frequency_rule.history.status is invalid")
    prior_max = frequency_history.get("max_contacts_per_member")
    if prior_max is not None:
        prior_max = _member_count(
            prior_max,
            f"{path}.frequency_rule.history.max_contacts_per_member",
        )
    frequency_history_times = {}
    for field, end_of_date in (
        ("window_start", False),
        ("window_end", True),
        ("as_of", True),
    ):
        raw = frequency_history.get(field)
        if raw is not None:
            parsed = _parse_time(
                raw,
                f"{path}.frequency_rule.history.{field}",
                tz,
                end_of_date=end_of_date,
            )
            frequency_history_times[field] = _format_time(parsed, tz)
        else:
            frequency_history_times[field] = None
    if frequency_history_status == "complete" and prior_max is None:
        raise ValueError(
            f"{path}.frequency_rule.history.max_contacts_per_member is required when status is complete"
        )

    offer = _mapping(activation.get("offer"), f"{path}.offer")
    offer_id = _required_text(offer.get("id"), f"{path}.offer.id")
    offer_label = _required_text(offer.get("label"), f"{path}.offer.label")
    offer_terms = _optional_text(offer.get("terms"), f"{path}.offer.terms")
    eligibility = _optional_text(offer.get("eligibility"), f"{path}.offer.eligibility")
    client_visible = offer.get("client_visible", False)
    if type(client_visible) is not bool:
        raise ValueError(f"{path}.offer.client_visible must be boolean")
    client_summary = _optional_text(
        offer.get("client_summary"), f"{path}.offer.client_summary"
    )
    client_quote = _client_quote(
        offer.get("client_quote"), f"{path}.offer.client_quote", brief.get("currency")
    )
    offer_times = {}
    for field, end_of_date in (("valid_from", False), ("valid_until", True)):
        raw = offer.get(field)
        if raw is not None:
            parsed = _parse_time(raw, f"{path}.offer.{field}", tz, end_of_date=end_of_date)
            offer_times[field] = _format_time(parsed, tz)
        else:
            offer_times[field] = None
    face_value = _member_money(
        offer.get("face_value"), f"{path}.offer.face_value", brief.get("currency")
    )
    unit_cost_raw = offer.get("unit_business_cost")
    unit_cost = _member_money(
        unit_cost_raw,
        f"{path}.offer.unit_business_cost",
        brief.get("currency"),
    )
    cost_status = unit_cost_raw.get("status") if isinstance(unit_cost_raw, dict) else None
    if unit_cost and unit_cost["amount"] is not None:
        if not isinstance(cost_status, str) or cost_status not in {"estimate", "quote", "committed"}:
            raise ValueError(
                f"{path}.offer.unit_business_cost.status must be estimate, quote, or committed"
            )
    elif cost_status is not None and (
        not isinstance(cost_status, str)
        or cost_status not in {"estimate", "quote", "committed", "unknown"}
    ):
        raise ValueError(
            f"{path}.offer.unit_business_cost.status must be estimate, quote, or committed"
        )

    redemption = _mapping(
        offer.get("redemption_limit", {"basis": "unknown"}),
        f"{path}.offer.redemption_limit",
    )
    redemption_basis = redemption.get("basis", "unknown")
    if not isinstance(redemption_basis, str) or redemption_basis not in {"total", "per_member", "unknown"}:
        raise ValueError(f"{path}.offer.redemption_limit.basis is invalid")
    redemption_quantity = redemption.get("quantity")
    if redemption_basis == "unknown":
        if redemption_quantity is not None:
            raise ValueError(
                f"{path}.offer.redemption_limit.quantity requires a known limit basis"
            )
    else:
        redemption_quantity = _member_count(
            redemption_quantity,
            f"{path}.offer.redemption_limit.quantity",
            minimum=1,
        )

    inventory = _mapping(
        offer.get("inventory", {"status": "unknown"}), f"{path}.offer.inventory"
    )
    inventory_status = inventory.get("status", "unknown")
    if not isinstance(inventory_status, str) or inventory_status not in {"limited", "unlimited", "unknown"}:
        raise ValueError(f"{path}.offer.inventory.status is invalid")
    inventory_quantity = inventory.get("quantity")
    if inventory_status == "limited":
        inventory_quantity = _member_count(
            inventory_quantity, f"{path}.offer.inventory.quantity"
        )
    elif inventory_quantity is not None:
        raise ValueError(
            f"{path}.offer.inventory.quantity is only valid for limited inventory"
        )

    audience = _mapping(offer.get("audience", {}), f"{path}.offer.audience")
    audience_basis = audience.get("count_basis", "unknown")
    if not isinstance(audience_basis, str) or audience_basis not in {"exact_unique_eligible", "conservative_upper_bound", "unknown"}:
        raise ValueError(f"{path}.offer.audience.count_basis is invalid")
    audience_scope = _optional_text(
        audience.get("population_scope"), f"{path}.offer.audience.population_scope"
    )
    audience_count = audience.get("eligible_member_count")
    if audience_count is not None:
        audience_count = _member_count(
            audience_count, f"{path}.offer.audience.eligible_member_count"
        )
    if audience_basis in {"exact_unique_eligible", "conservative_upper_bound"} and audience_count is None:
        raise ValueError(
            f"{path}.offer.audience.eligible_member_count is required for its declared count basis"
        )
    if audience_basis in {"exact_unique_eligible", "conservative_upper_bound"} and not audience_scope:
        raise ValueError(
            f"{path}.offer.audience.population_scope is required for its declared count basis"
        )
    audience_as_of = audience.get("as_of")
    if audience_as_of is not None:
        audience_as_of = _format_time(
            _parse_time(audience_as_of, f"{path}.offer.audience.as_of", tz), tz
        )

    history = _mapping(offer.get("history", {}), f"{path}.offer.history")
    history_status = history.get("status", "unknown")
    if not isinstance(history_status, str) or history_status not in {"complete", "partial", "unknown"}:
        raise ValueError(f"{path}.offer.history.status is invalid")
    history_times = {}
    for field, end_of_date in (
        ("window_start", False),
        ("window_end", True),
        ("as_of", True),
    ):
        raw = history.get(field)
        if raw is not None:
            parsed = _parse_time(raw, f"{path}.offer.history.{field}", tz, end_of_date=end_of_date)
            history_times[field] = _format_time(parsed, tz)
        else:
            history_times[field] = None
    redeemed_quantity = history.get("redeemed_quantity")
    if redeemed_quantity is not None:
        redeemed_quantity = _member_count(
            redeemed_quantity, f"{path}.offer.history.redeemed_quantity"
        )
    restored_quantity = history.get("restored_quantity")
    if restored_quantity is not None:
        restored_quantity = _member_count(
            restored_quantity, f"{path}.offer.history.restored_quantity"
        )
    refunded_quantity = history.get("refunded_quantity")
    if refunded_quantity is not None:
        refunded_quantity = _member_count(
            refunded_quantity, f"{path}.offer.history.refunded_quantity"
        )
    if history_status == "complete" and (
        redeemed_quantity is None
        or restored_quantity is None
        or refunded_quantity is None
    ):
        raise ValueError(
            f"{path}.offer.history redeemed_quantity, restored_quantity, and refunded_quantity are required when status is complete"
        )
    actual_spend = _member_money(
        history.get("actual_spend"), f"{path}.offer.history.actual_spend", brief.get("currency")
    )
    actual_refund = _member_money(
        history.get("actual_refund"), f"{path}.offer.history.actual_refund", brief.get("currency")
    )
    cancellation_terms = _optional_text(
        offer.get("cancellation_terms"), f"{path}.offer.cancellation_terms"
    )
    return {
        "task_id": task_id,
        "channel": channel,
        "purpose": purpose,
        "contact_window": {
            "start": _format_time(window_start, tz),
            "end": _format_time(window_end, tz),
        },
        "planned_contacts_per_member": planned_contacts,
        "frequency_rule": {
            "period_days": period_days,
            "max_contacts_per_member": max_contacts,
            "history": {
                "status": frequency_history_status,
                "window_start": frequency_history_times["window_start"],
                "window_end": frequency_history_times["window_end"],
                "max_contacts_per_member": prior_max,
                "as_of": frequency_history_times["as_of"],
                **_refs(frequency_history, f"{path}.frequency_rule.history"),
            },
        },
        "offer": {
            "id": offer_id,
            "label": offer_label,
            "eligibility": eligibility,
            "terms": offer_terms,
            "client_visible": client_visible,
            "client_summary": client_summary,
            "client_quote": client_quote,
            **offer_times,
            "face_value": face_value,
            "unit_business_cost": {
                **(unit_cost or {"amount": None, "currency": None, "source_refs": [], "evidence_refs": [], "basis_status": "user_input_not_independently_verified"}),
                "status": cost_status if unit_cost and unit_cost["amount"] is not None else "unknown",
            },
            "redemption_limit": {"basis": redemption_basis, "quantity": redemption_quantity},
            "inventory": {
                "status": inventory_status,
                "quantity": inventory_quantity,
                **_refs(inventory, f"{path}.offer.inventory"),
            },
            "audience": {
                "eligible_member_count": audience_count,
                "count_basis": audience_basis,
                "population_scope": audience_scope,
                "as_of": audience_as_of,
                **_refs(audience, f"{path}.offer.audience"),
            },
            "history": {
                "status": history_status,
                **history_times,
                "redeemed_quantity": redeemed_quantity,
                "restored_quantity": restored_quantity,
                "refunded_quantity": refunded_quantity,
                "actual_spend": actual_spend,
                "actual_refund": actual_refund,
                **_refs(history, f"{path}.offer.history"),
            },
            "cancellation_terms": cancellation_terms,
            **_refs(offer, f"{path}.offer"),
        },
        **_refs(activation, path),
        "external_contact": "not_performed_by_evaluator",
    }


def _member_offer_signature(offer: dict[str, Any]) -> dict[str, Any]:
    def without_source_labels(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_source_labels(child)
                for key, child in value.items()
                if key not in {"source_refs", "evidence_refs", "basis_status"}
            }
        if isinstance(value, list):
            return [without_source_labels(child) for child in value]
        return deepcopy(value)

    return without_source_labels(
        {
            key: offer[key]
            for key in (
                "label",
                "eligibility",
                "terms",
                "client_visible",
                "client_summary",
                "client_quote",
                "valid_from",
                "valid_until",
                "face_value",
                "unit_business_cost",
                "redemption_limit",
                "inventory",
                "audience",
                "history",
                "cancellation_terms",
            )
        }
    )


def _member_offer_pool_plan(
    offer_id: str,
    entries: list[dict[str, Any]],
    default_currency: str | None,
    tz: Any,
) -> dict[str, Any]:
    first = entries[0]["activation"]["offer"]
    caps = []
    cap_basis = []
    limit = first["redemption_limit"]
    audience = first["audience"]
    if limit["basis"] == "total":
        caps.append(limit["quantity"])
        cap_basis.append("用户声明总核销上限")
    elif limit["basis"] == "per_member" and audience["count_basis"] in {
        "exact_unique_eligible",
        "conservative_upper_bound",
    }:
        caps.append(limit["quantity"] * audience["eligible_member_count"])
        cap_basis.append(
            "每人核销上限 × "
            + ("精确去重人数" if audience["count_basis"] == "exact_unique_eligible" else "受众人数保守上界")
        )
    inventory = first["inventory"]
    if inventory["status"] == "limited":
        caps.append(inventory["quantity"])
        cap_basis.append("明示库存总量")
    maximum_redemptions = min(caps) if caps else None
    history = first["history"]
    earliest_contact_start = min(
        _parse_time(
            entry["activation"]["contact_window"]["start"],
            "member_offer.activation.contact_window.start",
            tz,
        )
        for entry in entries
    )
    history_scope_valid = False
    if all(
        history.get(field)
        for field in ("window_start", "window_end", "as_of")
    ) and first.get("valid_from") is not None:
        history_start = _parse_time(history["window_start"], "member_offer.history.window_start", tz)
        history_end = _parse_time(
            history["window_end"], "member_offer.history.window_end", tz, end_of_date=True
        )
        history_as_of = _parse_time(history["as_of"], "member_offer.history.as_of", tz)
        offer_start = _parse_time(first["valid_from"], "member_offer.valid_from", tz)
        history_scope_valid = (
            history_start <= offer_start
            and history_end == history_as_of
            and history_as_of >= offer_start
            and history_as_of <= earliest_contact_start
        )
    history_complete = (
        history["status"] == "complete"
        and history["redeemed_quantity"] is not None
        and history["restored_quantity"] is not None
        and history["refunded_quantity"] is not None
        and bool(history["source_refs"] or history["evidence_refs"])
        and history_scope_valid
    )
    if history_complete and history["restored_quantity"] > history["redeemed_quantity"]:
        raise ValueError(
            f"member_offer.{offer_id}.history.restored_quantity cannot exceed redeemed_quantity"
        )
    if history_complete and history["refunded_quantity"] > history["redeemed_quantity"]:
        raise ValueError(
            f"member_offer.{offer_id}.history.refunded_quantity cannot exceed redeemed_quantity"
        )
    if maximum_redemptions is not None and history_complete:
        consumed = max(0, history["redeemed_quantity"] - history["restored_quantity"])
        remaining_redemptions = max(0, maximum_redemptions - consumed)
        history_limit_conflict = consumed > maximum_redemptions
        history_effect = (
            "history_exceeds_declared_pool_limit"
            if history_limit_conflict
            else "complete_history_subtracted"
        )
    else:
        consumed = None
        remaining_redemptions = maximum_redemptions
        history_limit_conflict = False
        history_effect = "history_not_subtracted"
    unit_cost = first["unit_business_cost"]
    unit_amount = unit_cost["amount"]
    unit_currency = unit_cost["currency"] or default_currency
    if unit_amount is not None and remaining_redemptions is not None:
        planned_amount = _nonnegative(
            unit_amount, f"member_offer.{offer_id}.unit_business_cost.amount"
        ) * remaining_redemptions
    elif unit_amount == 0:
        planned_amount = Decimal(0)
    else:
        planned_amount = None
    if planned_amount is not None:
        try:
            solver_amount = float(planned_amount)
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                f"member_offer.{offer_id} planned cost exceeds the supported solver range"
            ) from exc
        if not math.isfinite(solver_amount):
            raise ValueError(f"member_offer.{offer_id} planned cost exceeds the supported solver range")
    merged_refs = {key: [] for key in ("source_refs", "evidence_refs")}
    for entry in entries:
        for source_object in (
            entry["activation"],
            entry["activation"]["offer"],
            entry["activation"]["offer"]["unit_business_cost"],
            entry["activation"]["offer"]["inventory"],
            entry["activation"]["offer"]["audience"],
            entry["activation"]["offer"]["history"],
        ):
            for key in merged_refs:
                for reference in source_object.get(key, []):
                    if reference not in merged_refs[key]:
                        merged_refs[key].append(deepcopy(reference))
    return {
        "id": offer_id,
        "label": first["label"],
        "task_ids": sorted({entry["activation"]["task_id"] for entry in entries}),
        "touchpoint_ids": sorted({entry["touchpoint_id"] for entry in entries}),
        "offer": deepcopy(first),
        "inventory_status": inventory["status"],
        "maximum_redemptions_upper_bound": maximum_redemptions,
        "remaining_redemptions_upper_bound": remaining_redemptions,
        "redemption_bound_basis": " + ".join(cap_basis) if cap_basis else "上限未知",
        "prior_redemptions_consumed": consumed,
        "history_effect": history_effect,
        "history_scope_valid": history_scope_valid,
        "history_limit_conflict": history_limit_conflict,
        "planned_cost_amount": _json_number(planned_amount) if planned_amount is not None else None,
        "planned_cost_currency": unit_currency,
        "planned_cost_status": unit_cost["status"],
        "committed": unit_cost["status"] == "committed",
        "planned_cost_basis_status": (
            "bounded_from_user_terms_not_independently_verified"
            if planned_amount is not None and unit_currency is not None
            else "unknown_cost_or_quantity_bound"
        ),
        "source_refs": merged_refs["source_refs"],
        "evidence_refs": merged_refs["evidence_refs"],
        "actual_history": deepcopy(first["history"]),
        "external_contact": "not_performed_by_evaluator",
    }


def _member_frequency_assessments(
    entries: list[dict[str, Any]], tz: Any
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for current in entries:
        activation = current["activation"]
        rule = activation["frequency_rule"]
        period = timedelta(days=rule["period_days"])
        start = _parse_time(
            activation["contact_window"]["start"], "member_activation.contact_window.start", tz
        )
        end = _parse_time(
            activation["contact_window"]["end"], "member_activation.contact_window.end", tz
        )
        related = []
        for candidate in entries:
            candidate_activation = candidate["activation"]
            candidate_start = _parse_time(
                candidate_activation["contact_window"]["start"],
                "member_activation.contact_window.start",
                tz,
            )
            candidate_end = _parse_time(
                candidate_activation["contact_window"]["end"],
                "member_activation.contact_window.end",
                tz,
            )
            if candidate_start <= end + period and candidate_end >= start - period:
                related.append((candidate, candidate_start, candidate_end))
        blockers: list[str] = []
        if end - start > period:
            blockers.append("本触达窗口长于频次周期；需拆分为更短窗口")
        if any(candidate_end - candidate_start > period for _, candidate_start, candidate_end in related):
            blockers.append("同一频次窗口内有触点覆盖时间过长；需拆分后核算")

        prior_by_segment: dict[str, int] = {}
        history_incomplete = False
        for candidate, _candidate_start, _candidate_end in related:
            candidate_activation = candidate["activation"]
            history = candidate_activation["frequency_rule"]["history"]
            if (
                history["status"] != "complete"
                or history["max_contacts_per_member"] is None
                or not (history["source_refs"] or history["evidence_refs"])
                or history["window_start"] is None
                or history["window_end"] is None
                or history["as_of"] is None
            ):
                blockers.append("可能重叠的受众段有既往频次历史未知或缺少引用")
                history_incomplete = True
                continue
            history_start = _parse_time(
                history["window_start"], "member_activation.frequency.history.window_start", tz
            )
            history_end = _parse_time(
                history["window_end"],
                "member_activation.frequency.history.window_end",
                tz,
                end_of_date=True,
            )
            history_as_of = _parse_time(
                history["as_of"],
                "member_activation.frequency.history.as_of",
                tz,
                end_of_date=True,
            )
            if (
                history_start > start - period
                or history_end < start
                or history_as_of > start
                or history_end != history_as_of
            ):
                blockers.append("可能重叠的受众段没有覆盖本次触达前完整滚动窗口的频次历史")
                history_incomplete = True
                continue
            segment_id = candidate["audience_id"]
            prior_by_segment[segment_id] = max(
                prior_by_segment.get(segment_id, 0),
                history["max_contacts_per_member"],
            )
        planned_upper = sum(
            candidate["activation"]["planned_contacts_per_member"]
            for candidate, _candidate_start, _candidate_end in related
        )
        prior_upper = sum(prior_by_segment.values())
        projected_upper = prior_upper + planned_upper
        if not history_incomplete and projected_upper > rule["max_contacts_per_member"]:
            blockers.append(
                f"将可能重叠的受众段按最保守重叠口径合并后，滚动 {rule['period_days']} 天触达上界 "
                f"为 {projected_upper} 次，超过本条规则的每人 {rule['max_contacts_per_member']} 次"
            )
        results[current["touchpoint_id"]] = {
            "status": "COMPLETE_CONSERVATIVE_UPPER_BOUND" if not blockers else "INCOMPLETE_OR_OVER_LIMIT",
            "period_days": rule["period_days"],
            "limit_per_member": rule["max_contacts_per_member"],
            "prior_contacts_upper_bound": None if history_incomplete else prior_upper,
            "planned_contacts_upper_bound": planned_upper,
            "projected_contacts_upper_bound": None if history_incomplete else projected_upper,
            "possible_overlap_basis": "不同受众段按可能重叠合并；同一audience_id仅保留最高既往频次，不推断去重人数",
            "related_touchpoint_ids": sorted({row[0]["touchpoint_id"] for row in related}),
            "blockers": list(dict.fromkeys(blockers)),
        }
    return results


def _assess_member_activation(
    touchpoint: dict[str, Any],
    activation: dict[str, Any],
    pool: dict[str, Any],
    task: dict[str, Any] | None,
    task_mandatory: bool,
    tz: Any,
    frequency_assessment: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    start = _parse_time(
        activation["contact_window"]["start"], "member_activation.contact_window.start", tz
    )
    end = _parse_time(
        activation["contact_window"]["end"], "member_activation.contact_window.end", tz
    )
    if touchpoint["consent_status"] != "granted":
        blockers.append("会员同意状态不是用户记录的已同意")
    if not touchpoint["purpose"] or activation["purpose"] != touchpoint["purpose"]:
        blockers.append("触达用途未与该会员段的同意用途一致")
    if activation["channel"] not in touchpoint["channels"]:
        blockers.append("拟用渠道不在用户记录的许可渠道中")
    if not touchpoint["consent_scope"]:
        blockers.append("会员同意范围未提供")
    if not touchpoint["opt_out_handling"]:
        blockers.append("退订处理方式未提供")
    if not (touchpoint["source_refs"] or touchpoint["evidence_refs"]):
        blockers.append("会员同意缺少可核对引用")
    consent_start = touchpoint.get("valid_from")
    consent_end = touchpoint.get("valid_until")
    if consent_start is None:
        blockers.append("会员同意有效起始时间未知")
    elif _parse_time(consent_start, "member_touchpoint.valid_from", tz) > start:
        blockers.append("会员同意开始时间晚于拟触达窗口")
    if consent_end is None:
        blockers.append("会员同意有效期限未知")
    elif _parse_time(consent_end, "member_touchpoint.valid_until", tz, end_of_date=True) < end:
        blockers.append("会员同意在拟触达窗口结束前到期")

    frequency = activation["frequency_rule"]
    blockers.extend(frequency_assessment["blockers"])

    offer = activation["offer"]
    if not offer["eligibility"]:
        blockers.append("权益适用资格未写明")
    if not offer["terms"]:
        blockers.append("权益使用条款未提供")
    if not offer["cancellation_terms"]:
        blockers.append("取消/退改后果未提供")
    if offer["valid_from"] is None or offer["valid_until"] is None:
        blockers.append("权益有效窗口未完整提供")
    else:
        offer_start = _parse_time(offer["valid_from"], "member_offer.valid_from", tz)
        offer_end = _parse_time(
            offer["valid_until"], "member_offer.valid_until", tz, end_of_date=True
        )
        if offer_start > start or offer_end < end:
            blockers.append("拟触达窗口不在权益有效窗口内")
    if pool["inventory_status"] == "unknown":
        blockers.append("权益库存未知，不能证明可发放")
    if pool["maximum_redemptions_upper_bound"] is None:
        blockers.append("无法从核销上限、库存或受众人数形成总权益数量上界")
    if pool["history_limit_conflict"]:
        blockers.append("既有净核销量超过声明的权益池总上限；需核对合同、库存与实际台账")
    elif pool["remaining_redemptions_upper_bound"] == 0:
        blockers.append("扣除已核销和明确恢复数量后，权益池已无剩余名额")
    if pool["history_effect"] not in {
        "complete_history_subtracted",
        "history_exceeds_declared_pool_limit",
    }:
        blockers.append("既有核销/恢复历史未能按权益有效期完整扣除")
    if pool["planned_cost_amount"] is None or pool["planned_cost_currency"] is None:
        blockers.append("权益单位业务成本或总量上界未知，不能确认预算敞口")

    if task is None:
        blockers.append("关联排期任务缺失")
    elif task.get("start") is None or task.get("end") is None:
        if task.get("status") == "NOT_SELECTED_BY_OBJECTIVE" and not task_mandatory:
            return {
                "status": "TASK_NOT_SELECTED_BY_OBJECTIVE",
                "blockers": blockers,
                "pool_id": pool["id"],
                "task_id": activation["task_id"],
                "frequency_check": frequency_assessment,
                "external_contact": "not_performed_by_evaluator",
                "platform_authorization_status": "not_evaluated; consent does not authorize platform sending",
            }
        blockers.append("关联任务未排入当前日历")
    else:
        task_start = _parse_time(task["start"], "member_activation.task.start", tz)
        task_end = _parse_time(task["end"], "member_activation.task.end", tz)
        if task_start < start or task_end > end:
            blockers.append("关联任务的实际排期超出会员触达窗口")

    blockers = list(dict.fromkeys(blockers))
    return {
        "status": "PLAN_READY_FOR_REVIEW" if not blockers else "REQUIRES_INPUT_OR_AUTHORITY_RECHECK",
        "blockers": blockers,
        "pool_id": pool["id"],
        "task_id": activation["task_id"],
        "frequency_check": frequency_assessment,
        "external_contact": "not_performed_by_evaluator",
        "platform_authorization_status": "not_evaluated; consent does not authorize platform sending",
    }


def _campaign_content_snapshot(
    inputs: dict[str, Any], brief: dict[str, Any], tz: Any
) -> tuple[dict[str, Any], list[str]]:
    """Keep the content hierarchy and calendar references distinct from tasks."""
    campaigns = _rows(inputs.get("campaigns", []), "campaigns")
    topics = _rows(inputs.get("topics", []), "topics")
    series = _rows(inputs.get("series", []), "series")
    posts = _rows(inputs.get("posts", []), "posts")
    calendar_nodes = _rows(inputs.get("calendar_nodes", []), "calendar_nodes")
    member_touchpoints = _rows(
        inputs.get("member_touchpoints", []), "member_touchpoints"
    )
    campaign_ids = {row["id"] for row in campaigns}
    topic_ids = {row["id"] for row in topics}
    series_ids = {row["id"] for row in series}
    post_ids = {row["id"] for row in posts}
    gaps: list[str] = []

    def linked_object(row: dict[str, Any], path: str, field: str, known_ids: set[str], objects_supplied: bool):
        reference = row.get(field)
        if reference is None:
            return None, "not_linked"
        reference = _required_text(reference, f"{path}.{field}")
        if not objects_supplied:
            return reference, "reference_not_in_snapshot"
        if reference not in known_ids:
            raise ValueError(f"{path}.{field} refers to an unknown object")
        return reference, "linked_in_snapshot"

    campaign_rows = []
    for row in campaigns:
        path = f"campaigns.{row['id']}"
        start, end = row.get("start_date"), row.get("end_date")
        if start is not None:
            _parse_time(start, f"{path}.start_date", tz or brief.get("timezone"))
        if end is not None:
            _parse_time(end, f"{path}.end_date", tz or brief.get("timezone"), end_of_date=True)
        campaign_rows.append(
            {
                **deepcopy(row),
                **_refs(row, path),
                "record_status": "user_supplied_campaign_not_independently_verified",
            }
        )

    topic_rows = []
    for row in topics:
        path = f"topics.{row['id']}"
        topic_rows.append(
            {
                **deepcopy(row),
                **_refs(row, path),
                "record_status": "user_supplied_topic_not_independently_verified",
            }
        )

    series_rows = []
    for row in series:
        path = f"series.{row['id']}"
        campaign_id, campaign_link = linked_object(
            row, path, "campaign_id", campaign_ids, bool(campaigns)
        )
        linked_topics = _id_list(row.get("topic_ids", []), f"{path}.topic_ids")
        if topics and set(linked_topics) - topic_ids:
            raise ValueError(f"{path}.topic_ids refers to an unknown Topic")
        if linked_topics and not topics:
            gaps.append(f"Series {row['id']} Topic 引用未包含在本次输入快照中。")
        if campaign_link == "reference_not_in_snapshot":
            gaps.append(f"Series {row['id']} 的 Campaign 引用未包含在本次输入快照中。")
        series_rows.append(
            {
                **deepcopy(row),
                "campaign_id": campaign_id,
                "campaign_link_status": campaign_link,
                "topic_ids": deepcopy(linked_topics),
                **_refs(row, path),
                "record_status": "user_supplied_series_not_independently_verified",
            }
        )

    post_rows = []
    for row in posts:
        path = f"posts.{row['id']}"
        campaign_id, campaign_link = linked_object(
            row, path, "campaign_id", campaign_ids, bool(campaigns)
        )
        series_id, series_link = linked_object(
            row, path, "series_id", series_ids, bool(series)
        )
        variant_of, variant_status = linked_object(
            row, path, "variant_of", post_ids - {row["id"]}, bool(posts)
        )
        source_artifact_reference = row.get("source_artifact_reference")
        source_display = _optional_text(row.get("source_display"), f"{path}.source_display")
        if source_artifact_reference is not None:
            source_artifact_reference = _mapping(
                source_artifact_reference,
                f"{path}.source_artifact_reference",
            )
            artifact_id = _required_text(
                source_artifact_reference.get("artifact_id"),
                f"{path}.source_artifact_reference.artifact_id",
            )
            artifact_version = source_artifact_reference.get("artifact_version")
            if type(artifact_version) is not int or artifact_version < 1:
                raise ValueError(f"{path}.source_artifact_reference.artifact_version must be a positive integer")
            source_post_id = _required_text(
                source_artifact_reference.get("post_id"),
                f"{path}.source_artifact_reference.post_id",
            )
            source_artifact_reference = {
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "post_id": source_post_id,
            }
        linked_topics = _id_list(row.get("topic_ids", []), f"{path}.topic_ids")
        if topics and set(linked_topics) - topic_ids:
            raise ValueError(f"{path}.topic_ids refers to an unknown Topic")
        if linked_topics and not topics:
            gaps.append(f"Post {row['id']} 的 Topic 引用未包含在本次输入快照中。")
        if campaign_link == "reference_not_in_snapshot":
            gaps.append(f"Post {row['id']} 的 Campaign 引用未包含在本次输入快照中。")
        if series_link == "reference_not_in_snapshot":
            gaps.append(f"Post {row['id']} 的 Series 引用未包含在本次输入快照中。")
        if variant_status == "reference_not_in_snapshot":
            gaps.append(f"Post {row['id']} 的派生来源版本未包含在本次输入快照中。")
        for field in ("planned_publish_at", "actual_publish_at"):
            if row.get(field) is not None:
                _parse_time(row[field], f"{path}.{field}", tz or brief.get("timezone"))
        if "locked" in row and type(row["locked"]) is not bool:
            raise ValueError(f"{path}.locked must be boolean")
        content_fields = ("title", "hook", "body", "cta", "visual_brief")
        content = {
            field: _optional_text(row.get(field), f"{path}.{field}")
            for field in content_fields
        }
        hashtags = row.get("hashtags", [])
        if not isinstance(hashtags, list) or any(not isinstance(item, str) for item in hashtags):
            raise ValueError(f"{path}.hashtags must be a list of text")
        locked_fields = _id_list(row.get("locked_fields", []), f"{path}.locked_fields")
        unknown_locked = set(locked_fields) - set(content_fields) - {"hashtags"}
        if unknown_locked:
            raise ValueError(f"{path}.locked_fields contains unknown content fields")
        if row.get("locked") and not locked_fields:
            locked_fields = [field for field, value in content.items() if value]
            if hashtags:
                locked_fields.append("hashtags")
        supplied_content = any(content.values()) or bool(hashtags)
        if not supplied_content:
            gaps.append(f"Post {row['id']} 仅记录对象元数据，尚未提供可交付内容字段。")
        elif not content.get("body") and not content.get("hook"):
            gaps.append(f"Post {row['id']} 尚无正文或内容开场；目前是部分内容记录。")
        post_rows.append(
            {
                **deepcopy(row),
                **content,
                "hashtags": deepcopy(hashtags),
                "locked_fields": locked_fields,
                "locked_content_status": "user_locked_fields_preserved" if locked_fields else "no_content_fields_locked",
                "campaign_id": campaign_id,
                "campaign_link_status": campaign_link,
                "series_id": series_id,
                "series_link_status": series_link,
                "variant_of": variant_of,
                "variant_link_status": variant_status,
                "variant_source_label": _optional_text(row.get("variant_source_label"), f"{path}.variant_source_label"),
                "source_artifact_reference": source_artifact_reference,
                "source_display": source_display,
                "topic_ids": deepcopy(linked_topics),
                "publication_record_status": (
                    "user_supplied_publication_not_independently_verified"
                    if row.get("actual_publish_at") is not None
                    else "no_actual_publication_record_supplied"
                ),
                **_refs(row, path),
                "record_status": "user_supplied_post_not_independently_verified",
            }
        )

    calendar_rows = []
    for row in calendar_nodes:
        path = f"calendar_nodes.{row['id']}"
        year = row.get("year")
        if year is not None and (type(year) is not int or year < 1):
            raise ValueError(f"{path}.year must be a positive integer")
        for field in ("start", "end"):
            if row.get(field) is not None:
                _parse_time(row[field], f"{path}.{field}", tz or brief.get("timezone"), end_of_date=field == "end")
        calendar_rows.append(
            {
                **deepcopy(row),
                **_refs(row, path),
                "record_status": "user_supplied_calendar_node_not_currently_verified",
            }
        )

    member_rows = []
    for row in member_touchpoints:
        path = f"member_touchpoints.{row['id']}"
        sensitive_fields = {
            "member_id",
            "member_ids",
            "email",
            "email_address",
            "phone",
            "phone_number",
            "phone_numbers",
            "recipient",
            "recipients",
            "contact_id",
            "mobile",
        }
        def has_personal_contact_key(value: Any) -> bool:
            if isinstance(value, dict):
                return any(
                    key.casefold().replace("-", "_") in sensitive_fields
                    or has_personal_contact_key(child)
                    for key, child in value.items()
                )
            if isinstance(value, list):
                return any(has_personal_contact_key(child) for child in value)
            return False

        if has_personal_contact_key(row):
            raise ValueError(
                f"{path} must refer to an authorized audience segment, not recipient identifiers"
            )
        audience_id = _required_text(row.get("audience_id"), f"{path}.audience_id")
        audience_label = _optional_text(row.get("audience_label"), f"{path}.audience_label")
        consent_status = row.get("consent_status", "unknown")
        allowed_states = {"granted", "denied", "withdrawn", "opted_out", "unknown"}
        if not isinstance(consent_status, str) or consent_status not in allowed_states:
            raise ValueError(f"{path}.consent_status is invalid")
        channels = row.get("channels", [])
        if not isinstance(channels, list) or any(not isinstance(item, str) for item in channels):
            raise ValueError(f"{path}.channels must be a list of channels")
        purpose = _optional_text(row.get("purpose"), f"{path}.purpose")
        scope = _optional_text(row.get("consent_scope"), f"{path}.consent_scope")
        opt_out_handling = _optional_text(
            row.get("opt_out_handling"), f"{path}.opt_out_handling"
        )
        valid_from = row.get("valid_from")
        if valid_from is not None:
            _parse_time(valid_from, f"{path}.valid_from", tz or brief.get("timezone"))
        valid_until = row.get("valid_until")
        contactability = "NOT_CONTACTABLE"
        if consent_status == "granted":
            if not purpose or not scope or not channels or not valid_until or not opt_out_handling:
                contactability = "GRANT_SCOPE_OR_EXPIRY_INCOMPLETE"
            else:
                expiry = _parse_time(
                    valid_until,
                    f"{path}.valid_until",
                    tz or brief.get("timezone"),
                    end_of_date=True,
                )
                project_start = brief.get("start_date")
                expired = False
                if project_start is not None:
                    expired = expiry < _parse_time(
                        project_start,
                        "brief.start_date",
                        tz or brief.get("timezone"),
                    )
                contactability = (
                    "GRANT_EXPIRED_FOR_PROJECT"
                    if expired
                    else "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK"
                )
            if not _refs(row, path)["source_refs"] and not _refs(row, path)["evidence_refs"]:
                gaps.append(
                    f"会员触点 {row['id']} 的同意状态缺少引用；不作为有效触达许可。"
                )
        activation = (
            _member_activation_snapshot(
                row["activation"], f"{path}.activation", brief, tz or brief.get("timezone")
            )
            if row.get("activation") is not None
            else None
        )
        member_rows.append(
            {
                **deepcopy(row),
                "audience_id": audience_id,
                "audience_label": audience_label,
                "consent_status": consent_status,
                "channels": deepcopy(channels),
                "purpose": purpose,
                "consent_scope": scope,
                "opt_out_handling": opt_out_handling,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "contactability": contactability,
                "activation": activation,
                "external_contact": "not_performed_by_evaluator",
                **_refs(row, path),
            }
        )
    if member_rows:
        gaps.append(
            "会员触达许可仅按本次输入保留；未独立核验同意/退订，也未联系会员。"
        )

    if calendar_rows:
        gaps.append("Campaign 日历节点沿用输入来源和年份；本次未检索或核实当前主办方日期。")
    snapshot = {
        "campaigns": campaign_rows,
        "topics": topic_rows,
        "series": series_rows,
        "posts": post_rows,
        "calendar_nodes": calendar_rows,
        "member_touchpoints": member_rows,
    }
    return snapshot, _dedupe(gaps)


def _campaign_revision_impact(
    context: Any, current_tasks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not isinstance(context, dict) or context.get("kind") != "campaign_schedule_revision":
        return None
    prior_tasks = context.get("previous_tasks", {})
    if not isinstance(prior_tasks, dict):
        return None
    current_by_id = {
        row["id"]: row
        for row in current_tasks
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    moves = []
    for identity in sorted(set(prior_tasks) | set(current_by_id)):
        prior = prior_tasks.get(identity, {})
        current = current_by_id.get(identity, {})
        prior_resources = prior.get("resource_ids", []) if isinstance(prior, dict) else []
        current_resources = current.get("resource_ids", []) if isinstance(current, dict) else []
        before = {
            "start": prior.get("start") if isinstance(prior, dict) else None,
            "end": prior.get("end") if isinstance(prior, dict) else None,
            "resource_ids": deepcopy(prior_resources),
            "status": prior.get("status") if isinstance(prior, dict) else None,
        }
        after = {
            "start": current.get("start") if isinstance(current, dict) else None,
            "end": current.get("end") if isinstance(current, dict) else None,
            "resource_ids": deepcopy(current_resources),
            "status": current.get("status") if isinstance(current, dict) else None,
        }
        placement_changed = (
            before["start"] != after["start"]
            or before["end"] != after["end"]
            or before["resource_ids"] != after["resource_ids"]
            or (before["start"] is None) != (after["start"] is None)
        )
        if placement_changed:
            moves.append(
                {
                    "task_id": identity,
                    "title": (current.get("title") if isinstance(current, dict) else None)
                    or (prior.get("title") if isinstance(prior, dict) else None),
                    "before": before,
                    "after": after,
                    "reasons": deepcopy(context.get("affected_reasons", {}).get(identity, [])),
                }
            )
    return {
        "previous_status": context.get("previous_status"),
        "confirmed_section": context.get("confirmed_section", False),
        "changed_fields": deepcopy(context.get("changed_fields", [])),
        "changed_task_ids": deepcopy(context.get("changed_task_ids", [])),
        "changed_resource_ids": deepcopy(context.get("changed_resource_ids", [])),
        "changed_task_fields": deepcopy(context.get("changed_task_fields", {})),
        "direct_replan_task_ids": deepcopy(context.get("direct_replan_task_ids", [])),
        "replan_task_ids": deepcopy(context.get("replan_task_ids", [])),
        "affected_task_ids": deepcopy(context.get("affected_task_ids", [])),
        "affected_reasons": deepcopy(context.get("affected_reasons", {})),
        "preserved_task_ids": deepcopy(context.get("preserved_task_ids", [])),
        "preserved_task_reasons": deepcopy(context.get("preserved_task_reasons", {})),
        "resource_competition": deepcopy(context.get("resource_competition", [])),
        "explicit_replan_tasks": deepcopy(context.get("explicit_replan_tasks", [])),
        "commitment_releases": deepcopy(context.get("commitment_releases", [])),
        "execution_corrections": deepcopy(context.get("execution_corrections", [])),
        "revision_note": context.get("revision_note"),
        "added_task_ids": deepcopy(context.get("added_task_ids", [])),
        "removed_task_ids": deepcopy(context.get("removed_task_ids", [])),
        "moved_tasks": moves,
    }


def _evaluate_schedule(
    brief: dict[str, Any],
    inputs: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    *,
    revision_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tz = _as_zone(brief["timezone"], "brief.timezone") if brief.get("timezone") else None
    budget = _budget_context(brief)
    content_snapshot, content_gaps = _campaign_content_snapshot(inputs, brief, tz)
    resources = _resource_index(inputs.get("resources", []))
    tasks = [deepcopy(row) for row in _rows(inputs.get("tasks", []), "tasks")]
    if any({"commitment_release", "execution_correction"} & set(task) for task in tasks):
        raise ValueError("Campaign commitment release and execution correction must use the versioned revision control path")
    revision_lock_reasons = (
        revision_context.get("preserved_task_reasons", {})
        if isinstance(revision_context, dict)
        else {}
    )
    if isinstance(revision_context, dict) and revision_context.get("kind") == "campaign_schedule_revision":
        prior_tasks = revision_context.get("previous_tasks", {})
        preserve_ids = set(revision_context.get("preserved_task_ids", []))
        preserved_reasons = revision_context.get("preserved_task_reasons", {})
        if not isinstance(prior_tasks, dict) or not isinstance(preserved_reasons, dict):
            raise ValueError("campaign revision context is malformed")
        for task in tasks:
            identity = task["id"]
            prior = prior_tasks.get(identity)
            if identity not in preserve_ids or not isinstance(prior, dict):
                continue
            if not prior.get("start") or not prior.get("end"):
                continue
            prior_resources = prior.get("resource_ids", [])
            if not isinstance(prior_resources, list) or any(not isinstance(value, str) for value in prior_resources):
                raise ValueError(f"campaign revision prior resources are invalid for task {identity}")
            task.update(
                locked=True,
                fixed_start=prior["start"],
                fixed_end=prior["end"],
                resource_ids=deepcopy(prior_resources),
                resource_options=[deepcopy(prior_resources)],
            )
            task["_schedule_revision_lock_reason"] = preserved_reasons.get(identity, [])
    has_content_context = any(
        content_snapshot[key]
        for key in (
            "campaigns",
            "topics",
            "series",
            "posts",
            "calendar_nodes",
            "member_touchpoints",
        )
    )
    if not tasks and not has_content_context:
        raise ValueError("campaign_schedule requires tasks or a content/calendar object")
    campaign_start = campaign_end = None
    if tasks:
        if tz is None or brief.get("start_date") is None or brief.get("end_date") is None:
            raise ValueError("Scheduling tasks requires a project timezone and start/end dates")
        campaign_start = _parse_time(brief["start_date"], "brief.start_date", tz)
        campaign_end = _parse_time(brief["end_date"], "brief.end_date", tz, end_of_date=True)
        if campaign_end <= campaign_start:
            raise ValueError("brief.end_date must follow brief.start_date")
    else:
        if brief.get("start_date") is not None:
            campaign_start = _parse_time(brief["start_date"], "brief.start_date", tz)
        if brief.get("end_date") is not None:
            campaign_end = _parse_time(brief["end_date"], "brief.end_date", tz, end_of_date=True)
        if campaign_start is not None and campaign_end is not None and campaign_end <= campaign_start:
            raise ValueError("brief.end_date must follow brief.start_date")
    task_by_id = {task["id"]: task for task in tasks}
    activation_entries: list[dict[str, Any]] = []
    offer_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for touchpoint in content_snapshot["member_touchpoints"]:
        activation = touchpoint.get("activation")
        if activation is None:
            continue
        task_id = activation["task_id"]
        if task_id not in task_by_id:
            raise ValueError(
                f"member_touchpoints.{touchpoint['id']}.activation.task_id refers to an unknown task"
            )
        entry = {
            "touchpoint_id": touchpoint["id"],
            "audience_id": touchpoint["audience_id"],
            "audience_label": touchpoint["audience_label"],
            "touchpoint": touchpoint,
            "activation": activation,
        }
        activation_entries.append(entry)
        offer_entries[activation["offer"]["id"]].append(entry)
    if activation_entries and not tasks:
        raise ValueError("member activation requires an existing schedule task")
    member_offer_pools: list[dict[str, Any]] = []
    member_offer_pool_by_id: dict[str, dict[str, Any]] = {}
    for offer_id, entries in offer_entries.items():
        signature = _member_offer_signature(entries[0]["activation"]["offer"])
        if any(
            _member_offer_signature(entry["activation"]["offer"]) != signature
            for entry in entries[1:]
        ):
            raise ValueError(
                f"member offer {offer_id} has conflicting terms across touchpoints"
            )
        pool = _member_offer_pool_plan(offer_id, entries, budget["currency"], tz)
        member_offer_pools.append(pool)
        member_offer_pool_by_id[offer_id] = pool
    order = _topological_order(tasks)
    mandatory_ids = _id_list(inputs.get("mandatory_tasks", []), "mandatory_tasks")
    unknown_mandatory = set(mandatory_ids) - set(task_by_id)
    if unknown_mandatory:
        raise ValueError("mandatory_tasks refers to an unknown task")
    required_coverage = _id_list(inputs.get("required_coverage", []), "required_coverage")
    exclusion_values = inputs.get("exclusions", [])
    if not isinstance(exclusion_values, list):
        raise ValueError("exclusions must be a list")
    exclusion_pairs = []
    for index, pair in enumerate(exclusion_values):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(item, str) for item in pair)
            or pair[0] not in task_by_id
            or pair[1] not in task_by_id
            or pair[0] == pair[1]
        ):
            raise ValueError(f"exclusions[{index}] must be a pair of distinct task IDs")
        exclusion_pairs.append(tuple(pair))
    exclusive_groups: dict[str, list[str]] = defaultdict(list)
    task_mandatory: dict[str, bool] = {}
    task_coverage: dict[str, list[str]] = {}
    task_benefits: dict[str, Decimal] = {}
    benefit_unit = _optional_text(inputs.get("benefit_unit"), "benefit_unit")
    for index, task in enumerate(tasks):
        path = f"tasks[{index}]"
        mandatory = task.get("mandatory", True)
        if type(mandatory) is not bool:
            raise ValueError(f"{path}.mandatory must be boolean")
        explicit_costs = task.get("costs")
        declared_committed = (
            task.get("committed_cost") is not None
            or (
                task.get("quote_id") in quotes
                and quotes[task.get("quote_id")]["status"] == "committed"
            )
            or (
                task.get("cost_status") == "committed"
                and task.get("estimated_cost") is not None
            )
            or (
                isinstance(explicit_costs, list)
                and any(
                    isinstance(line, dict) and line.get("status", "estimate") == "committed"
                    for line in explicit_costs
                )
            )
        )
        mandatory = mandatory or task["id"] in mandatory_ids or declared_committed
        task_mandatory[task["id"]] = mandatory
        task_coverage[task["id"]] = _id_list(task.get("coverage", []), f"{path}.coverage")
        exclusive_group = _optional_text(task.get("exclusive_group"), f"{path}.exclusive_group")
        if exclusive_group:
            exclusive_groups[exclusive_group].append(task["id"])
        benefit = task.get("benefit")
        if not mandatory:
            if benefit is None or benefit_unit is None:
                raise ValueError(f"{path} is optional and needs an explicit benefit and benefit_unit")
            task_benefits[task["id"]] = _nonnegative(benefit, f"{path}.benefit")
    if any(not value for value in task_mandatory.values()) and benefit_unit is None:
        raise ValueError("Optional tasks need a declared benefit_unit")
    seen_exclusions = set(exclusion_pairs)
    for group_tasks in exclusive_groups.values():
        for first_index, first in enumerate(group_tasks):
            for second in group_tasks[first_index + 1 :]:
                pair = (first, second)
                if pair not in seen_exclusions and (second, first) not in seen_exclusions:
                    exclusion_pairs.append(pair)
                    seen_exclusions.add(pair)
    object_labels = {
        task["id"]: task.get("title") or "未命名任务"
        for task in tasks
    }
    object_labels.update(
        {
            identity: resource.get("name") or "未命名资源"
            for identity, resource in resources.items()
        }
    )
    content_labels_by_kind = {}
    for kind, collection in (
        ("campaign", "campaigns"),
        ("series", "series"),
        ("post", "posts"),
        ("topic", "topics"),
    ):
        for row in content_snapshot[collection]:
            label = row.get("title") or row.get("name") or f"未命名{kind}"
            content_labels_by_kind[(kind, row["id"])] = label
            object_labels[row["id"]] = label
    availability = {identity: _resource_availability(resource, brief, tz) for identity, resource in resources.items()}
    windows: dict[str, tuple[datetime, datetime]] = {}
    durations: dict[str, timedelta | None] = {}
    assignments: dict[str, list[tuple[str, ...]]] = {}
    locked: dict[str, dict[str, Any]] = {}
    task_context: dict[str, dict[str, Any]] = {}
    task_reference_gaps: list[str] = []
    cost_rows: list[dict[str, Any]] = []
    task_cost_rows: dict[str, list[dict[str, Any]]] = {identity: [] for identity in task_by_id}
    cost_row_labels: dict[str, str] = {}
    cost_row_tasks: dict[str, str] = {}
    cost_row_pools: dict[str, str] = {}
    member_pool_task_ids = {
        task_id for pool in member_offer_pools for task_id in pool["task_ids"]
    }
    cost_unknown = not tasks

    def add_cost_row(task_id: str, row: dict[str, Any]) -> None:
        cost_rows.append(row)
        task_cost_rows[task_id].append(row)
        detail = row.get("label") or row.get("description") or row.get("title")
        if not detail:
            detail = "报价" if row.get("quote_id") else "费用"
        task_title = task_by_id[task_id].get("title") or "未命名任务"
        cost_row_labels[row["id"]] = f"{task_title} · {detail}"
        cost_row_tasks[row["id"]] = task_id
    for index, task in enumerate(tasks):
        path = f"tasks[{index}]"
        content_kind = _optional_text(task.get("content_kind"), f"{path}.content_kind")
        if content_kind is not None and content_kind not in {"campaign", "series", "post", "topic"}:
            raise ValueError(f"{path}.content_kind is unsupported")
        content_ref = _optional_text(task.get("content_ref"), f"{path}.content_ref")
        content_version = _optional_text(task.get("content_version"), f"{path}.content_version")
        location = _optional_text(task.get("location"), f"{path}.location")
        account_id = _optional_text(task.get("account_id"), f"{path}.account_id")
        responsible_party = _optional_text(task.get("responsible_party"), f"{path}.responsible_party")
        commitment_ref = _optional_text(task.get("commitment_ref"), f"{path}.commitment_ref")
        known_content_ids = {
            row["id"]
            for collection in ("campaigns", "series", "posts", "topics")
            for row in content_snapshot[collection]
        }
        if content_kind is None and content_ref is None:
            content_link_status = "UNLINKED_TASK_TARGET"
            task_reference_gaps.append(f"任务 {task['id']} 未关联具体 Campaign/Series/Post/Topic 版本。")
        elif content_kind is None or content_ref is None:
            content_link_status = "INCOMPLETE_TASK_TARGET_REFERENCE"
            task_reference_gaps.append(f"任务 {task['id']} 的内容对象类型与 ID 未同时提供。")
        elif content_ref in known_content_ids:
            content_link_status = "LINKED_TO_OBJECT_IN_INPUT_SNAPSHOT"
        else:
            content_link_status = "REFERENCE_NOT_INCLUDED_IN_INPUT_SNAPSHOT"
            task_reference_gaps.append(f"任务 {task['id']} 关联的内容对象不在本次输入快照中。")
        task_context[task["id"]] = {
            "content_kind": content_kind,
            "content_ref": content_ref,
            "content_label": (
                content_labels_by_kind.get((content_kind, content_ref))
                if content_kind is not None and content_ref is not None
                else None
            ),
            "content_version": content_version,
            "content_link_status": content_link_status,
            "location": location,
            "account_id": account_id,
            "responsible_party": responsible_party,
            "commitment_ref": commitment_ref,
            "exclusive_group": _optional_text(task.get("exclusive_group"), f"{path}.exclusive_group"),
        }
        if task.get("locked") and not commitment_ref:
            task_reference_gaps.append(f"锁定任务 {task['id']} 未关联原承诺/预订引用；仅按固定时间保留。")
        duration_hours = (
            _positive(task["duration_hours"], f"{path}.duration_hours")
            if task.get("duration_hours") is not None
            else None
        )
        durations[task["id"]] = None
        if duration_hours is not None:
            duration_microseconds = duration_hours * Decimal(3_600_000_000)
            if duration_microseconds != duration_microseconds.to_integral_value():
                raise ValueError(f"{path}.duration_hours is more precise than one microsecond")
            try:
                durations[task["id"]] = timedelta(microseconds=int(duration_microseconds))
            except OverflowError as exc:
                raise ValueError(f"{path}.duration_hours exceeds the supported calendar range") from exc
        earliest_raw = task.get("earliest_start", brief["start_date"])
        deadline_raw = task.get("deadline", brief["end_date"])
        earliest = _parse_time(earliest_raw, f"{path}.earliest_start", tz)
        deadline = _parse_time(deadline_raw, f"{path}.deadline", tz, end_of_date=True)
        earliest = max(earliest, campaign_start)
        deadline = min(deadline, campaign_end)
        windows[task["id"]] = earliest, deadline
        assignments[task["id"]] = _candidate_resource_sets(task, resources)
        is_locked = task.get("locked", False)
        if type(is_locked) is not bool:
            raise ValueError(f"{path}.locked must be boolean")
        if is_locked:
            fixed_start = _parse_time(task.get("fixed_start"), f"{path}.fixed_start", tz)
            fixed_end = _parse_time(task.get("fixed_end"), f"{path}.fixed_end", tz)
            if fixed_end <= fixed_start:
                raise ValueError(f"locked task {task['id']} fixed interval must have positive duration")
            if durations[task["id"]] is None:
                durations[task["id"]] = fixed_end - fixed_start
                duration_hours = Decimal(str((fixed_end - fixed_start).total_seconds())) / Decimal(3600)
            elif fixed_end - fixed_start != durations[task["id"]]:
                raise ValueError(f"locked task {task['id']} fixed interval must match duration_hours")
            fixed_resources = assignments[task["id"]][0]
            if any(identity not in resources for identity in fixed_resources):
                raise ValueError(f"locked task {task['id']} has unknown resources")
            locked[task["id"]] = {
                "start": fixed_start,
                "end": fixed_end,
                "resource_ids": fixed_resources,
                "locked": True,
                "option_index": 0,
            }
        cost = task.get("estimated_cost")
        quote_id = task.get("quote_id")
        explicit_costs = task.get("costs")
        planning_basis_count = sum(
            value is not None
            for value in (cost, quote_id, task.get("committed_cost"))
        )
        if explicit_costs is not None:
            if not isinstance(explicit_costs, list):
                raise ValueError(f"{path}.costs must be a list")
            individual_costs = (
                cost,
                quote_id,
                task.get("committed_cost"),
                task.get("actual_cost"),
                task.get("refund"),
            )
            if any(value is not None for value in individual_costs):
                raise ValueError(f"{path} must use costs or individual cost fields, not both")
            for cost_index, cost_line in enumerate(explicit_costs):
                cost_line = _mapping(cost_line, f"{path}.costs[{cost_index}]")
                line_id = _required_text(cost_line.get("id"), f"{path}.costs[{cost_index}].id")
                line_status = cost_line.get("status", "estimate")
                if not isinstance(line_status, str) or line_status not in _QUOTE_STATES:
                    raise ValueError(f"{path}.costs[{cost_index}].status is invalid")
                add_cost_row(
                    task["id"],
                    {**cost_line, "id": f"{task['id']}:{line_id}"},
                )
            has_planning_cost = any(
                row.get("status", "estimate") in {"quote", "estimate", "committed"}
                for row in explicit_costs
            )
        elif planning_basis_count > 1:
            raise ValueError(f"{path} must use one planning cost basis or a costs list")
        elif quote_id is not None:
            add_cost_row(
                task["id"],
                {
                    "id": f"{task['id']}:quote",
                    "quote_id": quote_id,
                    "source_refs": task.get("source_refs", []),
                    "evidence_refs": task.get("evidence_refs", []),
                }
            )
            has_planning_cost = True
        elif cost is not None:
            cost_status = task.get("cost_status", "estimate")
            add_cost_row(
                task["id"],
                {
                    "id": f"{task['id']}:estimate",
                    "amount": cost,
                    "currency": task.get("currency"),
                    "status": cost_status,
                    "source_refs": task.get("source_refs", []),
                    "evidence_refs": task.get("evidence_refs", []),
                }
            )
            has_planning_cost = cost_status in {"quote", "estimate", "committed"}
        else:
            has_planning_cost = False
        if explicit_costs is None:
            individual_states = (
                ("committed_cost", "committed"),
                ("actual_cost", "actual"),
                ("refund", "refund"),
            )
            for state_field, state in individual_states:
                amount = task.get(state_field)
                if amount is not None:
                    add_cost_row(
                        task["id"],
                        {
                            "id": f"{task['id']}:{state}",
                            "amount": amount,
                            "currency": task.get("currency"),
                            "status": state,
                            "source_refs": task.get("source_refs", []),
                            "evidence_refs": task.get("evidence_refs", []),
                        }
                    )
                    if state == "committed":
                        has_planning_cost = True
        if not has_planning_cost and task["id"] not in member_pool_task_ids:
            cost_unknown = True
    for pool in member_offer_pools:
        row_id = f"member-offer:{pool['id']}"
        cost_rows.append(
            {
                "id": row_id,
                "amount": pool["planned_cost_amount"],
                "currency": pool["planned_cost_currency"],
                "status": pool["planned_cost_status"],
                "source_refs": pool["source_refs"],
                "evidence_refs": pool["evidence_refs"],
            }
        )
        cost_row_labels[row_id] = f"会员权益池 · {pool['label']} · 预算敞口上界"
        cost_row_pools[row_id] = pool["id"]
    milp_shared_cost_pools = [
        {
            "id": pool["id"],
            "task_ids": pool["task_ids"],
            "amount": (
                Decimal(str(pool["planned_cost_amount"]))
                if pool["planned_cost_amount"] is not None
                and budget["currency"] is not None
                and pool["planned_cost_currency"] == budget["currency"]
                else None
            ),
            "committed": pool["committed"],
        }
        for pool in member_offer_pools
    ]
    placements: dict[str, dict[str, Any]] = dict(locked)
    scheduled: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    locked_constraint_conflicts: list[str] = []
    alternative_feasibility: dict[str, list[dict[str, Any]]] = {}

    # Preserve locked intervals first; an existing conflict is reported instead
    # of silently moving an already committed plan.
    locked_conflicts = _resource_conflicts(resources, placements, tz)
    for conflict in locked_conflicts:
        message = "锁定安排超出资源容量：" + ", ".join(conflict["task_ids"])
        conflicts.append(message)
        locked_constraint_conflicts.append(message)
    for identity in order:
        if identity in locked:
            placement = locked[identity]
            earliest, deadline = windows[identity]
            if placement["start"] < earliest or placement["end"] > deadline:
                message = f"锁定任务 {identity} 超出当前日期窗口；保留锁定时间并提示复核。"
                conflicts.append(message)
                locked_constraint_conflicts.append(message)
            missing_qualifications = _missing_qualifications(task_by_id[identity], placement["resource_ids"], resources)
            if missing_qualifications:
                message = f"锁定任务 {identity} 的资源资格不满足：" + ", ".join(missing_qualifications)
                conflicts.append(message)
                locked_constraint_conflicts.append(message)
            unknown_capacity = [
                resource_id
                for resource_id in placement["resource_ids"]
                if resources[resource_id]["capacity"] is None
            ]
            if unknown_capacity:
                message = f"锁定任务 {identity} 的资源容量未知；保留原时段并待人工核对。"
                conflicts.append(message)
                locked_constraint_conflicts.append(message)
            dependencies = task_by_id[identity].get("dependencies", [])
            dependency_ends = [
                placements[dependency]["end"]
                for dependency in dependencies
                if dependency in placements
            ]
            dependencies_missing = len(dependency_ends) != len(dependencies)
            dependency_overlaps = any(
                end > placement["start"] for end in dependency_ends
            )
            if dependencies_missing or dependency_overlaps:
                message = f"锁定任务 {identity} 与其前置任务顺序冲突；不自动移动锁定安排。"
                conflicts.append(message)
                locked_constraint_conflicts.append(message)
            scheduled[identity] = placement
            continue
        task = task_by_id[identity]
        earliest, deadline = windows[identity]
        if durations[identity] is None:
            conflicts.append(f"任务 {identity} 未提供可靠工时，保留未排入；不按默认时长猜测。")
            alternative_feasibility[identity] = []
            continue
        dependency_ends = []
        missing_dependency = []
        for dependency in task.get("dependencies", []):
            if dependency not in scheduled:
                missing_dependency.append(dependency)
            else:
                dependency_ends.append(scheduled[dependency]["end"])
        start_floor = max([earliest, *dependency_ends])
        if missing_dependency:
            conflicts.append(f"任务 {identity} 前置安排未能排入：" + ", ".join(missing_dependency))
            alternative_feasibility[identity] = []
            continue
        if deadline <= start_floor:
            conflicts.append(f"任务 {identity} 的最早开始时间不早于截止时间；当前约束下无法排入。")
            alternative_feasibility[identity] = []
            continue
        selected = None
        option_checks = []
        candidate_starts = _candidate_starts(start_floor, deadline, availability, placements, assignments[identity])
        for option_index, resource_ids in enumerate(assignments[identity]):
            qualification_gaps = _missing_qualifications(task, resource_ids, resources)
            capacity_gaps = [
                resource_id
                for resource_id in resource_ids
                if resources[resource_id]["capacity"] is None
            ]
            found = None
            if not qualification_gaps and not capacity_gaps:
                for start in candidate_starts:
                    end = start + durations[identity]
                    if end > deadline:
                        continue
                    if not _within_availability(resource_ids, start, end, availability):
                        continue
                    trial = {
                        **placements,
                        identity: {
                            "start": start,
                            "end": end,
                            "resource_ids": resource_ids,
                            "locked": False,
                            "option_index": option_index,
                        },
                    }
                    if _resource_conflicts(resources, trial, tz):
                        continue
                    found = {
                        "start": start,
                        "end": end,
                        "resource_ids": resource_ids,
                        "locked": False,
                        "option_index": option_index,
                    }
                    break
            option_checks.append(
                {
                    "option_index": option_index,
                    "resource_ids": list(resource_ids),
                    "feasible_with_current_schedule": found is not None,
                    "qualification_gaps": qualification_gaps,
                    "capacity_unknown_resource_ids": capacity_gaps,
                    "start": _format_time(found["start"], tz) if found else None,
                    "end": _format_time(found["end"], tz) if found else None,
                }
            )
            if found is not None and selected is None:
                selected = found
        alternative_feasibility[identity] = option_checks
        if selected is None:
            conflicts.append(f"任务 {identity} 在声明的窗口、容量、资格和当前安排下没有可用时段；未证明全局无解。")
            continue
        scheduled[identity] = selected
        placements[identity] = selected

    resource_conflicts = _resource_conflicts(resources, placements, tz)
    for conflict in resource_conflicts:
        conflicts.append(
            f"资源 {conflict['resource_id']} 在 {conflict['overlap_start']} 至 "
            f"{conflict['overlap_end']} 超容量：{', '.join(conflict['task_ids'])}。"
        )
    explicit_exclusions = inputs.get("exclusions", [])
    if not isinstance(explicit_exclusions, list):
        raise ValueError("exclusions must be a list")
    exclusion_conflicts = []
    for index, pair in enumerate(explicit_exclusions):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(item, str) for item in pair)
        ):
            raise ValueError(f"exclusions[{index}] must be a pair of task IDs")
        if (
            pair[0] not in task_by_id
            or pair[1] not in task_by_id
            or pair[0] == pair[1]
        ):
            raise ValueError(f"exclusions[{index}] references unknown or identical tasks")
        if pair[0] in scheduled and pair[1] in scheduled:
            exclusion_conflicts.append(
                {"task_ids": list(pair), "reason": "declared_mutual_exclusion"}
            )
            conflicts.append("互斥任务同时出现在计划中：" + " / ".join(pair))
    cost_lines, totals, line_unknown = _cost_lines(
        cost_rows,
        "tasks.costs",
        quotes,
        budget["currency"],
    )
    all_cost_lines = deepcopy(cost_lines)
    for line in cost_lines:
        line["label"] = cost_row_labels.get(line["id"], "任务费用")
    for line in all_cost_lines:
        line["label"] = cost_row_labels.get(line["id"], "任务费用")
    unknown_cost = cost_unknown or line_unknown
    budget_check = _budget_check(
        budget,
        totals,
        unknown_cost=unknown_cost,
        existing_committed=_existing_committed(
            quotes,
            currency=budget["currency"],
            exclude={row.get("quote_id") for row in cost_rows if row.get("quote_id")},
        ),
    )
    if not tasks:
        budget_check["status"] = "NO_TASKS_TO_PRICE"
    task_planning_costs: dict[str, Decimal] = {}
    budget_costs_complete = budget["currency"] is not None and not unknown_cost
    for identity, rows in task_cost_rows.items():
        parsed_rows, totals_by_currency, row_unknown = _cost_lines(
            rows,
            f"tasks.{identity}.costs",
            quotes,
            budget["currency"],
        )
        planning_rows = [
            row for row in parsed_rows
            if row["status"] in {"quote", "estimate", "committed"}
        ]
        if row_unknown or (not planning_rows and identity not in member_pool_task_ids):
            budget_costs_complete = False
        if set(totals_by_currency) - {budget["currency"]}:
            budget_costs_complete = False
        amount = totals_by_currency.get(budget["currency"], {})
        task_planning_costs[identity] = sum(
            (amount.get(state, Decimal(0)) for state in ("quote", "estimate", "committed")),
            Decimal(0),
        )
    for pool in member_offer_pools:
        if (
            pool["planned_cost_amount"] is None
            or budget["currency"] is None
            or pool["planned_cost_currency"] != budget["currency"]
        ):
            budget_costs_complete = False
    budget_limit = None
    if (
        budget_costs_complete
        and budget["maximum"] is not None
        and budget["currency"] is not None
        and budget_check.get("available_before_scenario") is not None
    ):
        budget_limit = Decimal(str(budget_check["available_before_scenario"]))
    optimizer = (
        _run_schedule_milp(
            tasks,
            task_by_id,
            resources,
            availability,
            windows,
            durations,
            assignments,
            locked,
            task_mandatory,
            task_coverage,
            required_coverage,
            exclusion_pairs,
            task_planning_costs,
            milp_shared_cost_pools,
            budget_limit,
            budget["maximum"] is not None,
            benefit_unit,
            campaign_start,
            campaign_end,
        )
        if tasks
        else {
            "state": "NO_TASKS",
            "schedule": {},
            "selected_task_ids": [],
            "solver": None,
            "incomplete_reasons": [],
        }
    )
    optimizer["budget_constraint_active"] = bool(
        optimizer["state"] in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL", "INFEASIBLE"}
        and budget_limit is not None
    )
    if optimizer["state"] in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL"}:
        placements = deepcopy(optimizer["schedule"])
        scheduled = deepcopy(optimizer["schedule"])
        conflicts = list(locked_constraint_conflicts)
        resource_conflicts = _resource_conflicts(resources, placements, tz)
        for conflict in resource_conflicts:
            conflicts.append(
                f"资源 {conflict['resource_id']} 在 {conflict['overlap_start']} 至 "
                f"{conflict['overlap_end']} 超容量：{', '.join(conflict['task_ids'])}。"
            )
        exclusion_conflicts = []
        for first, second in exclusion_pairs:
            if first in scheduled and second in scheduled:
                exclusion_conflicts.append({"task_ids": [first, second], "reason": "declared_mutual_exclusion"})
                conflicts.append("互斥任务同时出现在计划中。")
    elif optimizer["state"] == "INFEASIBLE":
        placements = dict(locked)
        scheduled = dict(locked)
        conflicts = [*locked_constraint_conflicts, "MILP在已提供的资源、预算、必选/覆盖、依赖与互斥约束下证明不存在完整可行组合；原锁定安排保留。"]
        conflicts.extend(
            _replace_object_ids(message, object_labels)
            for message in optimizer.get("incomplete_reasons", [])
        )
        resource_conflicts = _resource_conflicts(resources, placements, tz)
        exclusion_conflicts = []
        for first, second in exclusion_pairs:
            if first in locked and second in locked:
                exclusion_conflicts.append({"task_ids": [first, second], "reason": "declared_mutual_exclusion"})
                conflicts.append("两项已锁定承诺互斥；原时间均保留并待人工处理。")
    elif optimizer["state"] == "INDEPENDENT_CHECK_FAILED":
        optimizer["state"] = "INDEPENDENT_CHECK_FAILED_FALLBACK"
    if optimizer["state"] not in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL", "INFEASIBLE"}:
        unassessed_optional = [
            identity
            for identity in scheduled
            if not task_mandatory[identity] and identity not in locked
        ]
        for identity in unassessed_optional:
            scheduled.pop(identity, None)
            placements.pop(identity, None)
        if unassessed_optional:
            conflicts.append("可选任务的价值/资源约束未能完整求解；未自动纳入排期。")
    for task_id, options in alternative_feasibility.items():
        selected = optimizer.get("schedule", {}).get(task_id)
        for option in options:
            is_selected = bool(
                selected
                and selected["option_index"] == option["option_index"]
                and selected["resource_ids"] == tuple(option["resource_ids"])
            )
            if optimizer["state"] in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL"}:
                option["selected_by_milp"] = is_selected
                option["feasible_with_current_schedule"] = True if is_selected else None
                option["assessment"] = (
                    "selected_and_independently_verified"
                    if is_selected
                    else "not_selected; not separately proven infeasible"
                )
            elif optimizer["state"] == "INFEASIBLE":
                option["selected_by_milp"] = False
                option["feasible_with_current_schedule"] = None
                option["assessment"] = "no_feasible_complete_plan_under_declared_model"
            else:
                option["assessment"] = "earliest_sequential_probe_only; no_global_optimality_claim"
    unselected_cost_lines: list[dict[str, Any]] = []
    if optimizer["state"] in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL"}:
        selected_ids = set(optimizer["selected_task_ids"])
        selected_pool_ids = {
            pool["id"]
            for pool in member_offer_pools
            if set(pool["task_ids"]) & selected_ids
        }
        selected_rows = [
            row
            for identity in selected_ids
            for row in task_cost_rows[identity]
        ]
        selected_rows.extend(
            row
            for row in cost_rows
            if cost_row_pools.get(row["id"]) in selected_pool_ids
        )
        cost_lines, selected_totals, selected_line_unknown = _cost_lines(
            selected_rows,
            "selected_tasks.costs",
            quotes,
            budget["currency"],
        )
        for line in cost_lines:
            line["label"] = cost_row_labels.get(line["id"], "任务费用")
        selected_cost_unknown = selected_line_unknown or any(
            not any(
                row.get("status", "estimate") in {"quote", "estimate", "committed"}
                for row in task_cost_rows[identity]
            )
            for identity in selected_ids
            if identity not in member_pool_task_ids
        )
        unknown_cost = selected_cost_unknown
        existing_quote_ids = {
            row.get("quote_id")
            for row in selected_rows
            if row.get("quote_id")
        }
        budget_check = _budget_check(
            budget,
            selected_totals,
            unknown_cost=selected_cost_unknown,
            existing_committed=_existing_committed(
                quotes,
                currency=budget["currency"],
                exclude=existing_quote_ids,
            ),
        )
        if not selected_ids:
            budget_check["status"] = "NO_TASKS_TO_PRICE"
        unselected_cost_lines = [
            line
            for line in all_cost_lines
            if (
                cost_row_pools.get(line["id"]) not in selected_pool_ids
                if line["id"] in cost_row_pools
                else cost_row_tasks.get(line["id"]) not in selected_ids
            )
        ]
    planned_tasks = []
    for identity in order:
        task = task_by_id[identity]
        placement = scheduled.get(identity)
        option_checks = alternative_feasibility.get(identity, [])
        output_duration = task.get("duration_hours")
        if output_duration is None and durations[identity] is not None:
            output_duration = _json_number(
                Decimal(str(durations[identity].total_seconds())) / Decimal(3600)
            )
        execution = _execution_record(task, identity, placement, tz)
        if execution["timing_conflict"]:
            conflicts.append(f"任务 {identity} 的实际结束时间不晚于实际开始时间；原始记录保留待核。")
        if placement is None:
            not_selected = (
                optimizer["state"] in {"OPTIMAL", "FEASIBLE_NOT_OPTIMAL"}
                and not task_mandatory[identity]
                and identity not in optimizer["selected_task_ids"]
            )
            planned_tasks.append(
                {
                    "id": identity,
                    "title": task.get("title"),
                    **task_context[identity],
                    "status": "NOT_SELECTED_BY_OBJECTIVE" if not_selected else "NOT_SCHEDULED",
                    "duration_hours": output_duration,
                    "dependencies": deepcopy(task.get("dependencies", [])),
                    "alternative_assignments": option_checks,
                    "locked": task.get("locked", False),
                    "schedule_revision_lock_reason": deepcopy(revision_lock_reasons.get(identity, [])),
                    "execution": execution,
                    **_refs(task, f"tasks.{identity}"),
                }
            )
            continue
        planned_tasks.append(
            {
                "id": identity,
                "title": task.get("title"),
                **task_context[identity],
                "status": "PRESERVED_LOCKED" if placement["locked"] else "PLANNED",
                "start": _format_time(placement["start"], tz),
                "end": _format_time(placement["end"], tz),
                "duration_hours": output_duration,
                "resource_ids": list(placement["resource_ids"]),
                "dependencies": deepcopy(task.get("dependencies", [])),
                "alternative_assignments": option_checks,
                "locked": placement["locked"],
                "schedule_revision_lock_reason": deepcopy(revision_lock_reasons.get(identity, [])),
                "execution": execution,
                **_refs(task, f"tasks.{identity}"),
            }
        )
    revision_impact = _campaign_revision_impact(revision_context, planned_tasks)
    planned_task_by_id = {row["id"]: row for row in planned_tasks}
    frequency_assessments = _member_frequency_assessments(activation_entries, tz)
    for entry in activation_entries:
        activation = entry["activation"]
        task_id = activation["task_id"]
        pool = member_offer_pool_by_id[activation["offer"]["id"]]
        assessment = _assess_member_activation(
            entry["touchpoint"],
            activation,
            pool,
            planned_task_by_id.get(task_id),
            task_mandatory.get(task_id, True),
            tz,
            frequency_assessments[entry["touchpoint_id"]],
        )
        entry["touchpoint"]["activation_assessment"] = assessment
        if assessment["status"] != "TASK_NOT_SELECTED_BY_OBJECTIVE" and assessment["blockers"]:
            blockers = "；".join(assessment["blockers"])
            conflicts.append(
                f"会员触点 {entry['audience_label'] or entry['touchpoint_id']} 的权益计划需处理：{blockers}。"
            )
    limitations = [
        "排期是本地规划试算；保存或选择方案不会预订资源、下单、通知、提交或发布。",
        "不自动移动锁定安排；有冲突时保留原时间并报告影响。",
        "资源可用期和并行数量只有在明确提供时才进入MILP约束；未提供的合同、场地、安全、账号权限和实际执行回执仍待确认。",
    ]
    if revision_impact is not None:
        limitations.append(
            "本次修订按前版排期计算任务/依赖/资源竞争影响；未受影响的已确认时段保留，实际执行或锁定承诺不能由试算改写。"
        )
        if revision_impact["commitment_releases"]:
            limitations.append(
                "锁定释放仅记录用户提供的版本化依据；没有外部场地/供应商回执时，不表示原预订已解除。"
            )
        if revision_impact["execution_corrections"]:
            limitations.append(
                "实际执行记录修正以新的Artifact版本保留前后值和依据；本次不独立证明修正内容真实。"
            )
    if activation_entries:
        limitations.append(
            "会员权益池按同一offer ID合并，只计一次预算敞口；受众池人数必须为精确去重人数或明确保守上界。权益试算、同意记录和排期均不触发平台发送。"
        )
    if optimizer["state"] == "OPTIMAL":
        limitations.append("已在当前明确输入的连续时间、预算和资源约束模型中证明目标最优；未输入的商业条件不在此证明范围内。")
    elif optimizer["state"] == "FEASIBLE_NOT_OPTIMAL":
        limitations.append("优化器找到并独立核验了一份可行安排，但未证明最优；没有把它称为最优方案。")
    elif optimizer["state"] == "INFEASIBLE":
        limitations.append("优化器证明已提供的约束模型没有可行组合；这不推断未提供的替代资源或条件也不可行。")
    elif tasks:
        limitations.append("没有可证明的MILP最优解；保留经业务约束核验的锁定安排和试排结果，未将超时或输入缺口称为无解。")
    limitations.extend(task_reference_gaps)
    if not tasks:
        limitations.append("本次只提供内容或日历对象，未提供制作任务；依赖、资源容量与任务预算尚未计算。")
    limitations.extend(content_gaps)
    if any(not availability[identity] for identity in resources):
        limitations.append("部分资源没有声明可用期；这些资源没有被视为可用或空闲。")
    if any(resource["capacity"] is None for resource in resources.values()):
        limitations.append("部分资源没有声明并行可用数量；相关任务不会按默认容量排入。")
    selected_coverage = {
        segment
        for identity in scheduled
        for segment in task_coverage[identity]
    }
    missing_required_coverage = [
        segment for segment in required_coverage if segment not in selected_coverage
    ]
    if missing_required_coverage:
        conflicts.append(
            "当前安排未覆盖用户要求的交付目标："
            + _display_list(missing_required_coverage)
            + "。"
        )
    all_mandatory_scheduled = all(
        identity in scheduled
        for identity in task_by_id
        if task_mandatory[identity]
    )
    budget_is_reviewable = budget_check["status"] in {"WITHIN_LIMIT", "LIMIT_UNKNOWN"}
    if not tasks:
        status = "NOT_ASSESSED_NO_TASKS"
    elif optimizer["state"] == "INFEASIBLE":
        status = "INFEASIBLE_DECLARED_CONSTRAINTS"
    else:
        reviewable = all_mandatory_scheduled and not conflicts and not unknown_cost and budget_is_reviewable
        if reviewable and optimizer["state"] == "OPTIMAL":
            status = "READY_TO_REVIEW"
        elif reviewable and optimizer["state"] == "FEASIBLE_NOT_OPTIMAL":
            status = "FEASIBLE_NOT_OPTIMAL"
        elif reviewable:
            status = "FEASIBLE_FALLBACK_UNOPTIMIZED"
        else:
            status = "CONFLICTS_OR_GAPS"
    task_lines = []
    for row in planned_tasks:
        execution = row["execution"]
        dependency_labels = [
            task_by_id[identity].get("title") or "未命名前置任务"
            for identity in row["dependencies"]
            if identity in task_by_id
        ]
        resource_labels = [
            resources[identity].get("name") or "未命名资源"
            for identity in row.get("resource_ids", [])
            if identity in resources
        ]
        actual_period = (
            f"{execution.get('actual_start') or '实际开始未知'} — "
            f"{execution.get('actual_end') or '实际结束未知'}"
            if execution["record_status"]
            == "user_supplied_execution_not_independently_verified"
            else "实际记录未提供"
        )
        variance = execution.get("schedule_variance_hours")
        variance_text = f"；实际开始偏差 {variance} 小时" if variance is not None else ""
        task_lines.append(
            f"- {row['title'] or '未命名任务'}：{_task_status_label(row['status'])}；"
            f"关联内容 {row['content_label'] or '未关联或对象描述未提供'} "
            f"（版本 {row['content_version'] or '未知'}；{_content_link_label(row['content_link_status'])}）；"
            f"前置任务 {_display_list(dependency_labels)}；"
            f"地点 {row['location'] or '未知'}；账号 {row['account_id'] or '未知'}；"
            f"责任方 {row['responsible_party'] or '未知'}；"
            f"原承诺 {row['commitment_ref'] or '未提供'}；"
            f"互斥备选组 {row['exclusive_group'] or '无'}；"
            f"时长 {row['duration_hours'] if row['duration_hours'] is not None else '未知'} 小时；"
            f"计划 {row.get('start', '未排入')} — {row.get('end', '未排入')}；"
            f"资源 {_display_list(resource_labels) if resource_labels else '未指定'}；"
            f"{actual_period}{variance_text}"
        )
    human_conflicts = [_replace_object_ids(message, object_labels) for message in conflicts]
    human_limitations = [_replace_object_ids(message, object_labels) for message in limitations]
    objective_label = optimizer.get("objective_summary") or _optimizer_objective_label(
        optimizer.get("objective")
    )
    budget_constraint_label = (
        "硬预算已作为MILP约束"
        if optimizer.get("budget_constraint_active")
        else "硬预算未纳入MILP（上限/币种/金额未知或优化未运行）"
    )
    revision_lines = []
    if revision_impact is not None:
        revision_lines.extend(
            [
                "### 本次修订影响",
                f"- 前版状态：{_schedule_status_label(revision_impact['previous_status'])}；"
                f"影响任务 {_display_list([object_labels.get(identity, '未命名任务') for identity in revision_impact['affected_task_ids']])}。",
                f"- 保留原排期：{_display_list([object_labels.get(identity, '未命名任务') for identity in revision_impact['preserved_task_ids']])}。",
            ]
        )
        if revision_impact["moved_tasks"]:
            for move in revision_impact["moved_tasks"]:
                label = move["title"] or object_labels.get(move["task_id"], "未命名任务")
                before = move["before"]
                after = move["after"]
                reasons = _display_list(move.get("reasons", []))
                before_text = (
                    f"{before.get('start') or '未排入'} — {before.get('end') or '未排入'}；"
                    f"资源 {_display_list([object_labels.get(item, item) for item in before.get('resource_ids', [])])}"
                )
                after_text = (
                    f"{after.get('start') or '未排入'} — {after.get('end') or '未排入'}；"
                    f"资源 {_display_list([object_labels.get(item, item) for item in after.get('resource_ids', [])])}"
                )
                revision_lines.append(
                    f"  - {label}：原安排 {before_text}；本版 {after_text}；影响依据 {reasons}。"
                )
        if revision_impact["resource_competition"]:
            for row in revision_impact["resource_competition"]:
                resource_label = object_labels.get(row["resource_id"], "未命名资源")
                task_labels = [object_labels.get(identity, "未命名任务") for identity in row["task_ids"]]
                basis_label = "前版可用资源" if row.get("basis") == "previous" else "本版候选资源"
                revision_lines.append(
                    f"- 资源竞争影响：{basis_label} {resource_label} 可能同时被 {_display_list(task_labels)} 占用；容量按前版和本版时窗重新检查。"
                )
        for release in revision_impact["commitment_releases"]:
            task_label = object_labels.get(release["task_id"], "未命名任务")
            revision_lines.append(
                f"- {task_label}：已记录用户提供的锁定释放理由“{release['reason']}”；"
                "本版本试算不证明场地/供应商已实际取消或解除承诺。"
            )
        execution_field_labels = {
            "actual_start": "实际开始时间",
            "actual_end": "实际结束时间",
            "actual_publish_at": "实际发布时间",
            "execution_status": "用户记录的执行状态",
            "receipt_status": "用户记录的回执状态",
            "external_receipt_refs": "回执引用",
            "metric_snapshots": "观测快照",
        }
        for correction in revision_impact["execution_corrections"]:
            task_label = object_labels.get(correction["task_id"], "未命名任务")
            corrected_values = []
            for field in correction["fields"]:
                before_value = correction["before"].get(field)
                after_value = correction["after"].get(field)
                corrected_values.append(
                    f"{execution_field_labels.get(field, field)}：{before_value if before_value is not None else '未知'} → "
                    f"{after_value if after_value is not None else '未知'}"
                )
            revision_lines.append(
                f"- {task_label}：按用户理由“{correction['reason']}”修订执行记录；"
                f"{'；'.join(corrected_values)}；依据保存在本版修订引用中，本次未独立核验。"
            )
        if not revision_impact["moved_tasks"]:
            revision_lines.append("- 当前可行安排没有改变既有任务时段/资源；预算、约束和影响集仍已重新核算。")
    schedule_body = "\n".join(
        [
            f"计划审阅状态：{_schedule_status_label(status)}",
            f"求解结果：{_optimizer_state_label(optimizer['state'])}；目标 {objective_label}。",
            budget_constraint_label,
            "",
            *revision_lines,
            "" if revision_lines else "",
            *task_lines,
            "" if planned_tasks else "尚未提供可排期的任务步骤。",
            "",
            "### 冲突",
            _display_list(human_conflicts),
        ]
    )
    conflict_body = "\n".join(
        [
            "冲突不会自动删除或改写工作。纠正报价、资源可用期、资格或依赖后，",
            "可用相同任务输入重算；共享版本所有者负责保存前的父版本与差异核验。",
            "",
            _display_list(human_conflicts + human_limitations),
        ]
    )
    content_lines = []
    campaign_titles = {
        row["id"]: row.get("title") or "未命名活动"
        for row in content_snapshot["campaigns"]
    }
    topic_titles = {
        row["id"]: row.get("title") or row.get("name") or "未命名议题"
        for row in content_snapshot["topics"]
    }
    series_titles = {
        row["id"]: row.get("title") or "未命名系列"
        for row in content_snapshot["series"]
    }
    for campaign in content_snapshot["campaigns"]:
        content_lines.append(f"- Campaign：{campaign_titles[campaign['id']]}")
    for series in content_snapshot["series"]:
        content_lines.append(
            f"- Series：{series_titles[series['id']]}；"
            f"Campaign {campaign_titles.get(series.get('campaign_id'), '未关联' if not series.get('campaign_id') else 'Campaign引用未加载')}；"
            f"Topic {_display_list([topic_titles.get(identity, '议题引用未加载') for identity in series['topic_ids']])}"
        )
    for post in content_snapshot["posts"]:
        variant_label = post.get("variant_source_label")
        if not variant_label:
            variant_label = "基于已有Post版本" if post.get("variant_of") else "无"
        series_label = series_titles.get(
            post.get("series_id"),
            "独立稿" if not post.get("series_id") else "Series引用未加载",
        )
        content_lines.append(
            f"- Post：{post.get('title') or '未命名作品'}；"
            f"Series {series_label}；"
            f"Topic {_display_list([topic_titles.get(identity, '议题引用未加载') for identity in post['topic_ids']])}；"
            f"变体来源 {variant_label}；"
            f"平台 {post.get('platform') or '待定'}；"
            f"计划发布 {post.get('planned_publish_at') or '待定'}；"
            f"实际发布 {post.get('actual_publish_at') or '未提供'}"
        )
        if post.get("source_display"):
            content_lines.append(
                f"  - 来源Post：{post['source_display']}；在输入数据中保留Artifact版本与原Post定位。"
            )
        content_lines.extend(
            [
                f"  - Title：{post.get('title') or '待补'}",
                f"  - Hook：{post.get('hook') or '待补'}",
                f"  - Body：{post.get('body') or '待补'}",
                f"  - CTA：{post.get('cta') or '待补'}",
                f"  - Hashtags：{_display_list(post.get('hashtags', []))}",
                f"  - VisualBrief：{post.get('visual_brief') or '待补'}",
                f"  - 人工锁定字段：{_display_list(post.get('locked_fields', []))}",
                f"  - 内容来源：{_reference_markdown(post)}",
            ]
        )
    for touch in content_snapshot["member_touchpoints"]:
        content_lines.append(
            f"- 会员触点：{touch.get('audience_label') or '受众段描述未提供'}；"
            f"同意 {_consent_status_label(touch['consent_status'])}；"
            f"可联系状态 {_contactability_label(touch['contactability'])}；"
            f"渠道 {', '.join(touch['channels']) or '未指定'}；"
            f"用途 {touch.get('purpose') or '未指定'}；范围 {touch.get('consent_scope') or '未知'}；"
            f"有效至 {touch.get('valid_until') or '未知'}；退订处理 {touch.get('opt_out_handling') or '未知'}"
        )
        activation = touch.get("activation")
        if activation is not None:
            offer = activation["offer"]
            pool = member_offer_pool_by_id[offer["id"]]
            assessment = touch.get("activation_assessment", {})

            def money_text(money: dict[str, Any] | None) -> str:
                if money is None or money.get("amount") is None:
                    return "未知"
                return f"{money['amount']} {money.get('currency') or '币种未知'}"

            face_value_text = money_text(offer.get("face_value"))
            unit_cost_text = money_text(offer.get("unit_business_cost"))
            actual_history = offer["history"]
            frequency_check = assessment.get("frequency_check", {})
            offer_audience = offer["audience"]
            audience_basis_label = {
                "exact_unique_eligible": "精确去重资格人数（用户输入）",
                "conservative_upper_bound": "保守人数上界（用户输入）",
                "unknown": "人数口径未知",
            }.get(offer_audience["count_basis"], "人数口径待核")
            activation_status_label = {
                "PLAN_READY_FOR_REVIEW": "条件完整，可供业务审阅；不代表有平台发送授权",
                "REQUIRES_INPUT_OR_AUTHORITY_RECHECK": "条件或权限待补核",
                "TASK_NOT_SELECTED_BY_OBJECTIVE": "任务未被当前选择纳入",
            }.get(assessment.get("status"), "状态待核")
            history_status_label = {
                "complete": "完整历史（用户提供，未独立核验）",
                "partial": "部分历史",
                "unknown": "历史未知",
            }.get(actual_history["status"], "历史状态待核")
            inventory_status_label = {
                "limited": f"限量 {offer['inventory'].get('quantity')} 次",
                "unlimited": "用户声明不限量",
                "unknown": "库存未知",
            }.get(offer["inventory"]["status"], "库存待核")
            content_lines.extend(
                [
                    f"  - 会员活动：任务 {task_by_id[activation['task_id']].get('title') or '未命名任务'}；"
                    f"渠道 {activation['channel']}；计划窗口 {activation['contact_window']['start']} — {activation['contact_window']['end']}；"
                    f"每人计划 {activation['planned_contacts_per_member']} 次。",
                    f"  - 触达频控：滚动 {frequency_check.get('period_days', activation['frequency_rule']['period_days'])} 天每人上限 "
                    f"{frequency_check.get('limit_per_member', activation['frequency_rule']['max_contacts_per_member'])} 次；"
                    f"既往上界 {frequency_check.get('prior_contacts_upper_bound') if frequency_check.get('prior_contacts_upper_bound') is not None else '未知'}，"
                    f"本期计划上界 {frequency_check.get('planned_contacts_upper_bound') if frequency_check.get('planned_contacts_upper_bound') is not None else '未知'}，"
                    f"合计 {frequency_check.get('projected_contacts_upper_bound') if frequency_check.get('projected_contacts_upper_bound') is not None else '未知'}；"
                    f"{frequency_check.get('possible_overlap_basis') or '受众段重叠口径未知'}。",
                    f"  - 权益 {offer['label']}：资格 {offer.get('eligibility') or '待补'}；"
                    f"条款 {offer.get('terms') or '待补'}；有效期 {offer.get('valid_from') or '未知'} — {offer.get('valid_until') or '未知'}；"
                    f"{inventory_status_label}；面额 {face_value_text}；每次核销业务成本 {unit_cost_text}。",
                    f"  - 权益池受众：{offer_audience.get('eligible_member_count') if offer_audience.get('eligible_member_count') is not None else '未知'} 人；"
                    f"口径 {audience_basis_label}；范围 {offer_audience.get('population_scope') or '未知'}。",
                    f"  - 核销池上界：{pool['maximum_redemptions_upper_bound'] if pool['maximum_redemptions_upper_bound'] is not None else '未知'} 次；"
                    f"扣除完整历史后的上界 {pool['remaining_redemptions_upper_bound'] if pool['remaining_redemptions_upper_bound'] is not None else '未知'} 次；"
                    f"预算敞口 {pool['planned_cost_amount'] if pool['planned_cost_amount'] is not None else '未知'} "
                    f"{pool['planned_cost_currency'] or ''}，不含面额。",
                    f"  - 历史：{history_status_label}；核销 {actual_history.get('redeemed_quantity') if actual_history.get('redeemed_quantity') is not None else '未知'} 次；"
                    f"明确恢复库存 {actual_history.get('restored_quantity') if actual_history.get('restored_quantity') is not None else '未知'} 次；"
                    f"退款对应数量 {actual_history.get('refunded_quantity') if actual_history.get('refunded_quantity') is not None else '未知'} 次；"
                    f"实支 {money_text(actual_history.get('actual_spend'))}；退款 {money_text(actual_history.get('actual_refund'))}。",
                    f"  - 规划状态：{activation_status_label}；"
                    f"待处理 {_display_list(assessment.get('blockers', []))}。",
                ]
            )
    for topic in content_snapshot["topics"]:
        content_lines.append(
            f"- Topic：{topic_titles[topic['id']]}；"
            f"市场 {topic.get('market') or '待补'}；依据 {_reference_markdown(topic)}"
        )
    for node in content_snapshot["calendar_nodes"]:
        content_lines.append(
            f"- 日历节点：{node.get('title') or node.get('name') or '未命名'}；"
            f"年份 {node.get('year') or '待核'}；期间 {node.get('start') or '待核'} — "
            f"{node.get('end') or '待核'}；依据 {_reference_markdown(node)}"
        )
    if not content_lines:
        content_lines.append("未提供Campaign/Series/Post结构化对象；排期任务可独立开始，不自动补建父对象。")
    content_body = "\n".join(
        [
            "Campaign 管目标与资源，Series 管连续内容，Post 保留平台作品与变体；Topic 可跨 Series 和 Post 多对多关联。",
            "实际发布与计划发布日期分别保留。内容修改和重排不会在此结果中自动写回原稿或外发。",
            "会员触点使用受众段与许可范围；本计划不含个人收件人，也不会联系会员或推断平台发送权限。",
            "",
            *content_lines,
        ]
    )
    project_window = {
        "start": _format_time(campaign_start, tz) if campaign_start is not None else None,
        "end_exclusive": _format_time(campaign_end, tz) if campaign_end is not None else None,
        "timezone": brief.get("timezone"),
    }
    resource_lines = []
    for resource in resources.values():
        availability_text = (
            ", ".join(
                f"{_format_time(start, tz)} — {_format_time(end, tz)}"
                for start, end in availability[resource["id"]]
            )
            or "未声明可用期"
        )
        resource_lines.append(
            f"- {resource.get('name') or '未命名资源'}："
            f"可用数量 {resource['capacity'] if resource['capacity'] is not None else '未知'} {resource.get('unit') or '单位未知'}；"
            f"资格 {_display_list(resource.get('qualifications', []))}；"
            f"可用 {availability_text}"
        )
    for task in planned_tasks:
        names = [
            resources[identity].get("name") or "未命名资源"
            for identity in task.get("resource_ids", [])
            if identity in resources
        ]
        if names or task["status"] == "PRESERVED_LOCKED":
            resource_lines.append(
                f"- {task['title'] or '未命名任务'}：本版安排 { _display_list(names) if names else '无需指定资源' }；"
                f"{_task_status_label(task['status'])}"
            )
    resource_body = "\n".join(
        [
            "按资源名称、容量、资格与已声明可用时段核算；未选的替代资源不会形成预订。",
            "",
            *(resource_lines or ["未提供资源记录；非资源型任务仍可试排。"]),
        ]
    )
    budget_body_lines = [
        "估计、报价、承诺、实支和退款分列；未知金额不视作零。",
        "",
        _budget_markdown(budget_check),
        "",
        "当前安排所选任务的费用",
        _cost_markdown(cost_lines),
    ]
    if unselected_cost_lines:
        budget_body_lines.extend(
            [
                "",
                "未纳入当前安排的可选任务费用（不计入当前组合）",
                _cost_markdown(unselected_cost_lines),
            ]
        )
    budget_body = "\n".join(budget_body_lines)
    scope_body = "\n".join(
        [
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众/市场：{brief.get('audience') or '待补'} / {brief.get('market') or '待补'}",
            f"- 项目时区：{project_window['timezone'] or '未知'}",
            f"- 工作窗口：{project_window['start'] or '未知'} — {project_window['end_exclusive'] or '未知'}"
            f"{'（结束界限不含该时刻）' if project_window['end_exclusive'] else ''}",
            f"- 本地预算口径：{_budget_markdown(budget_check)}",
            "- 截止范围按项目时区解释；该时间窗口是输入约束，不代表场地、人员或渠道已确认。",
        ]
    )
    client_citations = _client_citations(inputs)
    client_scope_body = "\n".join(
        [
            f"- 目标：{brief.get('goal') or '待补'}",
            f"- 受众/市场：{brief.get('audience') or '待补'} / {brief.get('market') or '待补'}",
            f"- 工作窗口：{project_window['start'] or '待补'} — {project_window['end_exclusive'] or '待补'}；"
            f"时区 {project_window['timezone'] or '待补'}",
            _client_budget_header(brief),
            "项目日期是输入约束；未提供或未确认的场地、人员与合作条件仍待确认。",
        ]
    )
    client_content_lines = []
    for campaign in content_snapshot["campaigns"]:
        client_content_lines.append(f"- 活动：{campaign_titles[campaign['id']]}")
    for series in content_snapshot["series"]:
        client_content_lines.append(f"- 内容系列：{series_titles[series['id']]}")
    for post in content_snapshot["posts"]:
        client_content_lines.extend(
            [
                f"- 作品：{post.get('title') or '未命名作品'}；"
                f"平台 {post.get('platform') or '待定'}；"
                f"计划发布 {post.get('planned_publish_at') or '待定'}；"
                f"实际发布 {post.get('actual_publish_at') or '未提供'}",
                f"  - 开场：{post.get('hook') or '待补'}",
                f"  - 正文：{post.get('body') or '待补'}",
                f"  - 行动引导：{post.get('cta') or '待补'}",
                f"  - 话题标签：{_display_list(post.get('hashtags', []))}",
                f"  - 视觉说明：{post.get('visual_brief') or '待补'}",
            ]
        )
    for topic in content_snapshot["topics"]:
        client_content_lines.append(
            f"- 议题：{topic.get('title') or topic.get('name') or '未命名议题'}；"
            f"市场 {topic.get('market') or '待补'}"
        )
    for node in content_snapshot["calendar_nodes"]:
        client_content_lines.append(
            f"- 日历节点：{node.get('title') or node.get('name') or '未命名'}；"
            f"年份 {node.get('year') or '待核'}；期间 {node.get('start') or '待核'} — {node.get('end') or '待核'}"
        )
    if not client_content_lines:
        client_content_lines.append("尚未提供活动、系列、作品或议题内容。")
    client_member_offer_lines = []
    client_visible_offer_ids = set()
    client_member_quotes = []
    client_visible_pool_ids = {
        pool["id"] for pool in member_offer_pools if pool["offer"]["client_visible"]
    }
    for pool in member_offer_pools:
        offer = pool["offer"]
        if not offer["client_visible"]:
            continue
        selected_entries = [
            entry
            for entry in offer_entries[pool["id"]]
            if entry["activation"]["task_id"] in scheduled
        ]
        if not selected_entries:
            continue
        client_visible_offer_ids.add(pool["id"])
        if offer.get("client_quote") is not None:
            client_member_quotes.append(offer["client_quote"])
        channels = sorted(
            {
                _member_channel_label(entry["activation"]["channel"])
                for entry in selected_entries
            }
        )
        face_value = offer.get("face_value")
        face_value_text = (
            f"{face_value['amount']} {face_value['currency']}"
            if face_value and face_value.get("amount") is not None and face_value.get("currency")
            else "未提供"
        )
        validity = (
            f"{offer['valid_from']} — {offer['valid_until']}"
            if offer.get("valid_from") and offer.get("valid_until")
            else "待确认"
        )
        assessment_ready = all(
            entry["touchpoint"].get("activation_assessment", {}).get("status")
            == "PLAN_READY_FOR_REVIEW"
            for entry in selected_entries
        )
        client_member_offer_lines.extend(
            [
                f"- 权益：{offer['label']}",
                f"  - 权益面额：{face_value_text}。",
                f"  - 适用条件：{offer.get('eligibility') or '待确认'}。",
                f"  - 有效期：{validity}。",
                f"  - 计划渠道：{'、'.join(channels)}。",
                f"  - 权益说明：{offer.get('client_summary') or '客户版权益说明待补；内部完整条款不自动公开。'}",
                "  - 对客报价：见费用与预算部分。"
                if offer.get("client_quote")
                else "  - 对客报价：未提供；不根据内部成本反推。",
                "  - 状态："
                + (
                    "按用户标记的对客条件可供业务审阅；仍未联系会员、发送消息或发放权益。"
                    if assessment_ready
                    else "计划条件待核；仍未联系会员、发送消息或发放权益。"
                ),
            ]
        )
    if activation_entries and not client_member_offer_lines:
        if client_visible_pool_ids:
            client_member_offer_lines.append(
                "已标记可对客披露的会员权益未排入当前任务组合；本客户版不显示未排期的权益方案。"
            )
        else:
            client_member_offer_lines.append(
                "会员权益详情未明确标记为对客披露，客户版不会显示内部方案、会员条件或内部报价。"
            )
    client_content_body = "\n".join(
        [
            "客户可见的活动、系列、作品正文与发布日期。",
            *client_content_lines,
            *client_member_offer_lines,
            "会员同意、退订、频控、权益核销历史、库存台账和内部成本不在客户版公开；本评估不执行联系。"
            if content_snapshot["member_touchpoints"]
            else "",
        ]
    )
    client_schedule_lines = [
        f"计划状态：{_schedule_status_label(status)}",
        f"优化结果：{_optimizer_state_label(optimizer['state'])}。",
    ]
    if revision_impact is not None:
        client_schedule_lines.append(
            f"本版修订影响 {len(revision_impact['affected_task_ids'])} 项排期；实际预订/发布状态仍按原执行记录核对。"
        )
    commitment_status_rows = (
        revision_context.get("current_commitment_status", [])
        if isinstance(revision_context, dict)
        else []
    )
    has_unverified_release = any(
        isinstance(row, dict)
        and row.get("status") == "user_recorded_release_not_independently_verified"
        for row in commitment_status_rows
    )
    if has_unverified_release:
        client_schedule_lines.append(
            "本版保留用户记录的档期释放状态；场地/供应方是否已实际解除仍待核，本结果不构成重新确认或取消回执。"
        )
    member_activation_task_ids = {
        entry["activation"]["task_id"] for entry in activation_entries
    }
    for row in planned_tasks:
        dependencies = [
            (
                "会员触点准备"
                if identity in member_activation_task_ids
                else task_by_id[identity].get("title") or "未命名前置任务"
            )
            for identity in row["dependencies"]
            if identity in task_by_id
        ]
        client_schedule_lines.append(
            f"- {'会员触点准备' if row['id'] in member_activation_task_ids else (row['title'] or '未命名任务')}："
            f"{_task_status_label(row['status'])}；"
            f"内容 {row['content_label'] or '待关联'}；"
            f"计划 {row.get('start', '未排入')} — {row.get('end', '未排入')}；"
            f"前置任务 {_display_list(dependencies)}；"
            f"工时 {row['duration_hours'] if row['duration_hours'] is not None else '未知'} 小时"
        )
    client_schedule_lines.append(
        "已锁定安排不会被本次规划自动改动；本客户版不披露内部负责人、账号、资源名称或预订引用。"
    )
    if conflicts:
        client_schedule_lines.append(
            f"当前约束有 {len(conflicts)} 项待处理；需要确认窗口、依赖或资源条件后再形成对客承诺。"
        )
    client_schedule_body = "\n".join(client_schedule_lines)
    client_resource_body = "\n".join(
        [
            "此计划在满足已声明资源约束的范围内排程。",
            "客户版不显示内部人员、场地和设备容量明细；尚未确认的资源可用性不会被写成客户承诺。",
        ]
    )
    client_budget_body = "\n".join(
        [
            _client_budget_header(brief),
            _client_cost_markdown(
                [
                    *cost_lines,
                    *({"client_quote": quote} for quote in client_member_quotes),
                ]
            ),
            "内部供应商底价、资源估值、实支和退款台账不进入客户版；没有明确对客报价时不推算价格。"
            "报价/估计/承诺状态按输入保留，不代表已付款、已发送或实际履约。",
        ]
    )
    client_conflict_body = "\n".join(
        [
            "对客版本保留当前需要协商的窗口、依赖、预算或资源缺口概况；不包含内部谈判和复核记录。",
            "保存试算不代表订场、下单、通知、发布或已完成履约。",
            f"当前待处理项：{len(conflicts)}；锁定安排继续保留。",
        ]
    )
    client_limitations = []
    if has_unverified_release:
        client_limitations.append(
            "客户版会说明用户记录的档期释放，但不宣称场地/供应方已实际取消；实际状态仍需核对回执。"
        )
    if not any(line.get("client_quote") is not None for line in cost_lines) and not client_member_quotes:
        client_limitations.append("没有费用项被明确标记为对客报价/估计；客户版不披露内部成本，也不能作为对客报价。")
    if content_snapshot["member_touchpoints"]:
        client_limitations.append(
            "会员许可、退订和频控明细留在内部业务记录；本评估不含个人名单，也没有执行会员联系。"
        )
    if activation_entries:
        if not client_visible_pool_ids:
            client_limitations.append(
                "没有会员权益被明确标记为可向客户披露；客户版不显示会员方案或内部报价。"
            )
        else:
            if not client_visible_offer_ids:
                client_limitations.append(
                    "已标记的对客权益未排入当前任务组合，客户版没有展示其具体条款。"
                )
            if any(
                pool["offer"]["client_visible"] and pool["id"] in client_visible_offer_ids
                and not pool["offer"].get("client_summary")
                for pool in member_offer_pools
            ):
                client_limitations.append(
                    "部分对客权益缺少客户可读的权益说明；正文只显示明确提供的名称、面额、条件和窗口。"
                )
            if not any(
                pool["id"] in client_visible_offer_ids and pool["offer"].get("client_quote")
                for pool in member_offer_pools
            ):
                client_limitations.append(
                    "没有明确提供的客户报价；客户版不从内部单位成本、库存或权益历史反推价格。"
                )
            if any(
                not pool["offer"]["client_visible"] for pool in member_offer_pools
            ):
                client_limitations.append(
                    "未显式授权对客披露的权益池仍只在内部版本查看。"
                )
        client_limitations.append(
            "客户版不包含内部单位成本、库存台账、会员人数、同意与频次历史、实支和退款；内容显示不代表实际发送、发券或履约。"
        )
    if not client_citations:
        client_limitations.append("未提供客户引用授权；客户版不附资料原件引文或内部来源标识。")
    return {
        "title": f"Campaign 排期：{brief.get('goal') or '待定义目标'}",
        "sections": [
            _section(
                "campaign_scope",
                "活动范围与工作窗口",
                scope_body,
                {
                    "brief": _brief_data(brief),
                    "project_window": project_window,
                    "limitations": limitations,
                },
                client_body=client_scope_body,
            ),
            _section(
                "campaign_content",
                "Campaign、Series、Post 与 Topic",
                content_body,
                content_snapshot,
                client_body=client_content_body,
            ),
            _section(
                "campaign_schedule",
                "依赖、排期与备选可行性",
                schedule_body,
                {
                    "optimizer": optimizer,
                    "status": status,
                    "optimizer": optimizer,
                    "tasks": planned_tasks,
                    "revision_impact": revision_impact,
                    "current_commitment_status": deepcopy(
                        revision_context.get("current_commitment_status", [])
                        if isinstance(revision_context, dict)
                        else []
                    ),
                    "member_activations": [
                        {
                            "touchpoint_id": entry["touchpoint_id"],
                            "audience_id": entry["audience_id"],
                            "audience_label": entry["audience_label"],
                            "offer_id": entry["activation"]["offer"]["id"],
                            "task_id": entry["activation"]["task_id"],
                            "assessment": entry["touchpoint"]["activation_assessment"],
                        }
                        for entry in activation_entries
                    ],
                    "member_offer_pools": member_offer_pools,
                    "conflicts": conflicts,
                    "resource_conflicts": resource_conflicts,
                    "mutual_exclusions": exclusion_conflicts,
                },
                client_body=client_schedule_body,
            ),
            _section(
                "campaign_resources",
                "资源容量、资格与替代",
                resource_body,
                {
                    "resources": [_public_resource(row) for row in resources.values()],
                    "alternative_feasibility": alternative_feasibility,
                    "optimizer": optimizer,
                },
                client_body=client_resource_body,
            ),
            _section(
                "campaign_budget",
                "费用状态与预算约束",
                budget_body,
                {
                    "cost_lines": cost_lines,
                    "unselected_cost_lines": unselected_cost_lines,
                    "member_offer_pools": member_offer_pools,
                    "budget_check": budget_check,
                    "quotes": _public_quotes(quotes),
                    "optimizer": optimizer,
                },
                client_body=client_budget_body,
            ),
            _section(
                "campaign_conflicts",
                "冲突解释与恢复边界",
                conflict_body,
                {
                    "conflicts": conflicts,
                    "limitations": limitations,
                    "revision_impact": revision_impact,
                    "optimizer": optimizer,
                    "locked_task_ids": sorted(locked),
                    "input_tasks_preserved": True,
                },
                client_body=client_conflict_body,
            ),
        ],
        "limitations": _dedupe(
            [_replace_object_ids(message, object_labels) for message in limitations + conflicts]
        ),
        "client_limitations": _dedupe(client_limitations),
        "client_citations": client_citations,
    }


def _candidate_starts(
    earliest: datetime,
    deadline: datetime,
    availability: dict[str, list[tuple[datetime, datetime]]],
    placements: dict[str, dict[str, Any]],
    resource_options: list[tuple[str, ...]],
) -> list[datetime]:
    starts = {earliest}
    resource_ids = {identity for option in resource_options for identity in option}
    for identity in resource_ids:
        for start, _ in availability.get(identity, []):
            if earliest <= start < deadline:
                starts.add(start)
    for placement in placements.values():
        end = placement["end"]
        if earliest <= end < deadline:
            starts.add(end)
    return sorted(starts)


def _within_availability(
    resource_ids: tuple[str, ...],
    start: datetime,
    end: datetime,
    availability: dict[str, list[tuple[datetime, datetime]]],
) -> bool:
    for identity in resource_ids:
        windows = availability.get(identity, [])
        if not any(start >= left and end <= right for left, right in windows):
            return False
    return True


def _missing_qualifications(
    task: dict[str, Any], resource_ids: tuple[str, ...], resources: dict[str, dict[str, Any]]
) -> list[str]:
    required = task.get("required_qualifications", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError(f"task {task.get('id')}.required_qualifications must be a list")
    gaps = []
    combined = {
        qualification
        for identity in resource_ids
        for qualification in resources[identity].get("qualifications", [])
    }
    for qualification in required:
        if qualification not in combined:
            gaps.append(qualification)
    return gaps


def _validation_markdown(value: Any) -> str:
    if not isinstance(value, dict):
        return _display(value)
    labels = {
        "metric": "指标",
        "denominator": "分母",
        "window": "观察窗口",
        "method": "方法",
        "source": "数据来源",
        "unit": "计量单位",
        "comparison": "对照条件",
    }
    fields = [
        f"{labels.get(key, key)}：{_display(item)}"
        for key, item in value.items()
        if item not in (None, "", [], {})
    ]
    return "；".join(fields) or "本案验证指标、分母、窗口和方法尚未提供。"


_RIGHTS_MEDIA_LABELS = {
    "brand_owned_social": "品牌自有社媒",
    "creator_owned_social": "创作者账号",
    "paid_media": "付费媒体",
}
_RIGHTS_TERRITORY_LABELS = {"CN-Guangzhou": "中国·广州"}


def _rights_value_summary(value: Any, labels: dict[str, str]) -> str:
    if isinstance(value, (list, tuple)):
        return _display_list([labels.get(str(item), item) for item in value])
    if value is None:
        return "待补"
    return labels.get(str(value), _display(value))


def _rights_term_summary(rights: dict[str, Any]) -> str:
    term_days = rights.get("term_days")
    if term_days is not None:
        return f"{_display(term_days)}天"
    start = rights.get("term_start")
    end = rights.get("term_end")
    if start and end:
        return f"{_display(start)} — {_display(end)}"
    if start:
        return f"自 {_display(start)} 起，结束日待补"
    if end:
        return f"结束于 {_display(end)}，开始日待补"
    return "未知"


def _mechanism_path_markdown(value: Any) -> str:
    if not isinstance(value, dict):
        return _display(value)
    stages = (
        ("audience_trigger", "受众触发"),
        ("brand_action", "品牌动作"),
        ("value_exchange", "价值交换"),
        ("reinforcement", "延续反馈"),
    )
    supplied = [value.get(key) for key, _ in stages]
    if not any(isinstance(item, str) and item.strip() for item in supplied):
        return "尚未提供机制路径；需补清受众触发、品牌动作、价值交换和延续反馈。"
    return " → ".join(
        f"{label}：{_display(value.get(key))}"
        for key, label in stages
    )


def _display(value: Any) -> str:
    if value is None:
        return "待补"
    if isinstance(value, str):
        return value or "待补"
    if type(value) is bool:
        return "是" if value else "否"
    if isinstance(value, dict):
        if not value:
            return "无"
        return "；".join(
            f"{key}：{_display(item)}"
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return _display_list(value)
    return str(value)


def _acceptance_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return _display(value)
    reviewer = _display(value.get("reviewer"))
    criteria = _display_list(value.get("criteria", []))
    return f"{reviewer}；标准 {criteria}"


def _display_list(values: Any) -> str:
    if not values:
        return "无"
    if isinstance(values, (str, bytes)):
        return str(values)
    try:
        return "；".join(_display(value) for value in values)
    except TypeError:
        return _display(values)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _json_number(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be returned as JSON")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")
