"""Professional business results, precise revisions and action records on Artifact."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import re
import uuid

from flask import g, request

from .api import ApiError, _artifact_data, _body, _idempotency_key, _input_snapshot, _not_found, _string, _success, _validate_citations
from .auth import utc_now
from .collaboration import _review_access, require_artifact_access, require_artifact_control_access, require_material_access, require_work_access, require_workspace_access, work_commission
from .business_sources import input_source_catalog, normalized_table_bindings, prepare_source_revision


ECONOMICS_OPERATIONS = {"operating_analysis", "consumer_analysis", "financial_scenario", "financial_source_analysis", "financial_fact_corrections", "financial_quarterly_series", "operating_time_series", "financial_mmm_analysis"}
CAMPAIGN_OPERATIONS = {"strategy_proposal", "media_plan", "collaboration_plan", "campaign_schedule"}
MEASUREMENT_OPERATIONS = {"experiment_design", "experiment_analysis", "attribution_analysis", "incrementality_analysis", "calibration_analysis", "conformal_analysis"}
SERVICE_OPERATIONS = {"incident_brief", "service_response"}
EVENT_OPERATIONS = {"propagation_network", "propagation_hawkes", "adoption_diffusion"}
DECISION_OPERATIONS = {"decision_comparison"}


def install_business_execution_schema(store):
    with store.transaction() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS business_export_views (export_id TEXT PRIMARY KEY REFERENCES exports(id), audience TEXT NOT NULL CHECK(audience='client'), view_json TEXT NOT NULL)")


def business_export_view(version_row, audience):
    if audience == "internal":
        return dict(version_row)
    if audience != "client":
        raise ApiError("INVALID_EXPORT_AUDIENCE", "Choose the internal or client delivery view", 400)
    snapshot = json.loads(version_row["input_snapshot_json"])
    if snapshot.get("kind") != "business_analysis":
        raise ApiError("CLIENT_VIEW_NOT_PREPARED", "This result has no reviewed client delivery view", 409)
    business, parts = snapshot["business"], []
    result = business["result"]
    default_titles = {"strategy_proposal": "品牌策略与提案", "media_plan": "媒介方案", "collaboration_plan": "合作方案", "campaign_schedule": "活动与内容计划"}
    client_title = result.get("client_title") or default_titles.get(business["operation"], "客户交付成果")
    client_title = _string(client_title, "client_title", maximum=300)
    for section in result["sections"]:
        if not isinstance(section.get("client_body"), str) or not section["client_body"].strip() or section.get("client_review_required"):
            raise ApiError("CLIENT_VIEW_NOT_PREPARED", "Review the client text for the affected section before delivery", 409, {"section_id": section["id"], "section_title": section["title"]})
        parts.append("## " + section["title"] + "\n\n" + section["client_body"])
    if not isinstance(result.get("client_limitations"), list) or not all(isinstance(item, str) for item in result["client_limitations"]):
        raise ApiError("CLIENT_VIEW_NOT_PREPARED", "The client-facing limitations have not been prepared", 409)
    if result["client_limitations"]:
        parts.append("## 适用范围与待确认条件\n\n" + "\n".join("- " + item for item in result["client_limitations"]))
    from .campaign_collaboration import render_collaboration_lifecycle
    lifecycle = render_collaboration_lifecycle(business, "client")
    if lifecycle:
        parts.append(lifecycle)
    client_citations = result.get("client_citations", [])
    original_citations = {_json(item) for item in json.loads(version_row["citations_json"])}
    if not isinstance(client_citations, list) or any(_json(item) not in original_citations for item in client_citations):
        raise ApiError("CLIENT_CITATION_NOT_AUTHORIZED", "Client citations must be selected from the exact saved evidence", 409)
    return {**dict(version_row), "title": client_title, "body": "\n\n".join(parts), "citations_json": _json(client_citations),
            "input_snapshot_json": _json({"kind": "business_client_delivery", "audience": "client"})}


def saved_export_view(store, export_id, version_row):
    saved = store.row("SELECT view_json FROM business_export_views WHERE export_id=?", (export_id,))
    if saved is None:
        return version_row
    view = json.loads(saved["view_json"])
    return {**dict(version_row), **view}


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ApiError("INVALID_INPUT", "Business data must contain finite JSON values", 400) from error


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _evaluation_hash(inputs):
    values = dict(inputs)
    if isinstance(values.get("raw_source"), bytes):
        values["raw_source"] = {"sha256": hashlib.sha256(values["raw_source"]).hexdigest()}
    return _hash(values)


def _positive_version(value) -> int:
    if type(value) is not int or value < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    return value


def _key(value) -> str:
    return _string(value, "idempotency_key", maximum=200)


def _timestamp(value, name="occurred_at") -> str:
    value = _string(value, name, maximum=60)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_INPUT", f"{name} must be an ISO timestamp", 400) from error
    if parsed.tzinfo is None:
        raise ApiError("INVALID_INPUT", f"{name} needs an explicit timezone", 400)
    return value


def evaluate_business(operation: str, inputs: dict, *, is_cancelled=None, campaign_revision_context=None) -> dict:
    """Shared deterministic tool entry for API, Intelligence and the worker."""
    if not isinstance(inputs, dict):
        raise ApiError("INVALID_INPUT", "Business inputs must be an object", 400)
    inputs = {key: value for key, value in inputs.items() if key != "_resolved_financial_sources"}
    serializable_inputs = {key: value for key, value in inputs.items() if key != "raw_source"} if operation == "financial_source_analysis" else inputs
    _json(serializable_inputs)
    try:
        if operation in ECONOMICS_OPERATIONS:
            from .business_analysis_economics import evaluate_economics
            if is_cancelled is None:
                result = evaluate_economics(operation, inputs)
            else:
                result = evaluate_economics(operation, inputs, is_cancelled=is_cancelled)
        elif operation in CAMPAIGN_OPERATIONS:
            from .campaign import evaluate_campaign
            result = evaluate_campaign(operation, inputs, revision_context=campaign_revision_context)
        elif operation in MEASUREMENT_OPERATIONS:
            from .business_analysis_measurement import evaluate_measurement
            result = evaluate_measurement(operation, inputs, is_cancelled=is_cancelled)
        elif operation in SERVICE_OPERATIONS:
            from .business_execution_service import evaluate_service
            result = evaluate_service(operation, inputs)
        elif operation in EVENT_OPERATIONS:
            from .business_analysis_events import evaluate_events
            result = evaluate_events(operation, inputs, is_cancelled=is_cancelled)
        elif operation in DECISION_OPERATIONS:
            from .business_analysis_decision import evaluate_decision
            result = evaluate_decision(operation, inputs, is_cancelled=is_cancelled)
        else:
            raise ApiError("UNKNOWN_BUSINESS_OPERATION", "This business operation is not supported", 400)
    except ValueError as error:
        raise ApiError("INVALID_BUSINESS_INPUT", str(error), 400) from error
    if not isinstance(result, dict) or not isinstance(result.get("sections"), list):
        raise RuntimeError("Business evaluator returned an invalid result")
    ids = []
    for section in result["sections"]:
        if not isinstance(section, dict) or not all(field in section for field in ("id", "title", "body", "data")):
            raise RuntimeError("Business evaluator returned an invalid section")
        if not all(isinstance(section[field], str) for field in ("id", "title", "body")):
            raise RuntimeError("Business section text fields must be strings")
        ids.append(section["id"])
    if len(ids) != len(set(ids)):
        raise RuntimeError("Business result section identifiers must be unique")
    _json(result)
    return result


def _render(snapshot: dict) -> str:
    business = snapshot["business"]
    result = business["result"]
    parts = []
    for section in result["sections"]:
        parts.append(f"## {section['title']}\n\n{section['body']}")
    if result.get("limitations"):
        parts.append("## 适用范围与待补依据\n\n" + "\n".join(f"- {text}" for text in result["limitations"]))
    decisions = business.get("decisions", [])
    if decisions:
        parts.append("## 已记录的选择\n\n" + "\n".join(f"- {item['choice']}：{item['reason']}（记录于 {item['recorded_at']}）" for item in decisions))
    actions = business.get("actions", [])
    if actions:
        action_states = {"planned": "待执行", "approved": "内部计划已确认", "recorded": "已记录人工执行", "failed": "执行失败",
                         "effect_unknown": "实际效果待核", "executed": "本次内部操作已完成", "awaiting_outcome_verification": "已送出操作，实际结果待核",
                         "execution_unknown": "原执行回执待核", "cancelled": "未开始动作已取消", "cancellation_requested": "已请求停止，当前阶段待核"}
        parts.append("## 行动与回执\n\n| 行动 | 负责对象 | 应交成果 | 期限 | 当前记录 |\n|---|---|---|---|---|\n" + "\n".join(
            "| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in (
                item["title"], item["responsible"]["label"], item["deliverable"], item.get("due_at") or "未约定", action_states.get(item["state"], "状态待核")
            )) + " |" for item in actions))
    if business.get("costs"):
        parts.append("## 费用记录\n\n| 币种 | 估计 | 承诺 | 实支记录 | 退款记录 | 净实支记录 |\n|---|---:|---:|---:|---:|---:|\n" + "\n".join(
            "| " + " | ".join(str(value) if value is not None else "未知" for value in (currency, values.get("estimate"), values.get("committed"), values.get("actual"), values.get("refund"), values.get("net_actual"))) + " |"
            for currency, values in cost_totals(business["costs"]).items()) + "\n\n以上是附来源的用户费用记录，不是支付系统验证或资金操作。")
    if business.get("outcomes"):
        parts.append("## 实际观察与复盘\n\n" + "\n\n".join(
            f"**{item['metric']['name']} · {item['target']['label']}**\n\n{item['value'] if item['value'] is not None else '未知'} {item['metric']['unit']}；观察窗 {item['window']['start']}—{item['window']['end']}。\n\n{item['interpretation']}\n\n来源为用户观察记录，尚不能据此建立因果关系。"
            for item in business["outcomes"]))
    if business.get("service_events"):
        service_states = {"acknowledged": "确认收到", "investigated": "调查记录", "response_sent": "记录已回应", "promised": "服务承诺", "fulfilled": "履约记录", "feedback": "当事反馈", "closed": "关闭事项", "reopened": "重新处理", "observed": "后续观察"}
        service_states.update(assigned="负责人分派", response_prepared="已存回应草稿", response_delivery_unknown="回应送达待核", response_failed="记录回应未成功", response_read="记录对方已读", resolution_assessed="有依据的解决评估", related_case="关联事项", case_relation_removed="解除事项关联")
        service_states.update(problem_revised="问题修订，需重新核实", sources_replaced="来源替换，待重新核阅", sources_reviewed="来源核阅记录", case_event_recorded="已保存的服务事件")
        lines = []
        for item in business["service_events"]:
            line = f"- {item['occurred_at'] or '发生时间未提供'} · {service_states.get(item['event'], '待核服务记录')} · {item['description']}"
            context = item.get("case_context_at_event")
            if context:
                line += f"（当时问题：{context['question']}）"
            lines.append(line)
        parts.append("## 服务履约记录\n\n" + "\n".join(lines))
    from .campaign_collaboration import render_collaboration_lifecycle
    lifecycle = render_collaboration_lifecycle(business, "internal")
    if lifecycle:
        parts.append(lifecycle)
    if any(
        business.get(field)
        for field in (
            "campaign_task_history",
            "campaign_current_commitment_status",
        )
    ):
        from .campaign import render_campaign_task_history
        history = render_campaign_task_history(business)
        if history:
            parts.append(history)
    return "\n\n".join(parts)


def _read_business(connection, identity, artifact_id: str, *, write=False, version=None):
    artifact = require_artifact_access(connection, identity, artifact_id, write=write)
    selected = artifact["current_version"] if version is None else version
    row = connection.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                             (artifact_id, identity["workspace"]["id"], selected)).fetchone()
    if row is None:
        raise _not_found()
    snapshot = json.loads(row["input_snapshot_json"])
    if snapshot.get("kind") != "business_analysis" or not isinstance(snapshot.get("business"), dict):
        raise ApiError("NOT_BUSINESS_RESULT", "This result is not a structured business result", 409)
    return artifact, row, snapshot


def _replay(connection, identity, operation, key, fingerprint):
    existing = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation=? AND key=?",
                                  (identity["workspace"]["id"], operation, key)).fetchone()
    if existing is None:
        return None
    if existing["request_hash"] != fingerprint:
        raise ApiError("IDEMPOTENCY_CONFLICT", "This key already belongs to a different business request", 409)
    artifact_id, version = json.loads(existing["resource_id"])
    artifact = require_artifact_access(connection, identity, artifact_id)
    return _artifact_data(connection, artifact, version)


def _remember(connection, identity, operation, key, fingerprint, artifact_id, version):
    connection.execute("INSERT INTO idempotency_records VALUES(?, ?, ?, ?, 'business_version', ?, ?)",
                       (identity["workspace"]["id"], operation, key, fingerprint, _json([artifact_id, version]), utc_now()))


def _check_sources(connection, identity, work_id, citations):
    for item in citations:
        if not isinstance(item, dict):
            raise ApiError("INVALID_CITATION", "Each citation must be an object", 400)
        require_material_access(connection, identity, item.get("material_id"), target_work_id=work_id)
    return _validate_citations(connection, identity["workspace"]["id"], citations)


def _check_nested_sources(connection, identity, work_id, value):
    """Validate actual material references wherever a domain placed them.

    Free-text labels remain user statements; they are never promoted to saved
    citations merely because the field happens to be named evidence/support.
    """
    checked, labels, artifacts = set(), set(), {}
    def visit(item, field=""):
        if isinstance(item, dict):
            if "artifact_id" in item:
                artifact_id = item["artifact_id"]
                if not isinstance(artifact_id, str):
                    raise ApiError("INVALID_ARTIFACT_REFERENCE", "A result reference needs an exact saved identity", 400)
                version = item.get("artifact_version", item.get("version", item.get("source_version")))
                if type(version) is not int or version < 1:
                    raise ApiError("INVALID_ARTIFACT_REFERENCE", "A result reference needs its exact positive version", 400)
                artifact = require_artifact_access(connection, identity, artifact_id)
                source_scope = work_commission(connection, artifact["work_id"])
                if work_id and source_scope and source_scope != work_commission(connection, work_id):
                    raise ApiError("CLIENT_SCOPE_MISMATCH", "A private client result cannot be adopted into another commission", 403)
                saved = connection.execute("SELECT title,body,citations_json,input_snapshot_json FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?", (artifact_id, identity["workspace"]["id"], version)).fetchone()
                if saved is None:
                    raise _not_found()
                artifacts.setdefault((artifact_id, version), {"artifact_id": artifact_id, "version": version, "content_sha256": _hash(dict(saved))})
                if "post_id" in item:
                    post_id = item["post_id"]
                    source_snapshot = json.loads(saved["input_snapshot_json"])
                    source_sections = source_snapshot.get("business", {}).get("result", {}).get("sections", [])
                    posts = [post for section in source_sections if section.get("id") == "campaign_content"
                             for post in section.get("data", {}).get("posts", []) if post.get("id") == post_id]
                    if len(posts) != 1:
                        raise ApiError("SOURCE_POST_NOT_FOUND", "The selected Post does not belong to this exact saved result version", 409)
                    if "post_snapshot" in item and item["post_snapshot"] != posts[0]:
                        raise ApiError("SOURCE_POST_CHANGED", "The supplied Post does not match the selected saved version", 409)
                    artifacts[(artifact_id, version)].setdefault("posts", {})[post_id] = posts[0]
            if "material_id" in item:
                material_id = item["material_id"]
                if not isinstance(material_id, str):
                    raise ApiError("INVALID_CITATION", "Material references must identify a saved source", 400)
                material = require_material_access(connection, identity, material_id, target_work_id=work_id)
                selected_hash = item.get("material_sha256", item.get("material_version"))
                if selected_hash is not None and selected_hash != material["sha256"]:
                    raise ApiError("SOURCE_VERSION_CONFLICT", "A reference no longer matches the selected material version", 409)
                checked.add(material_id)
                if "segment_id" in item or "quote" in item:
                    if not {"segment_id", "quote"} <= item.keys():
                        raise ApiError("INVALID_CITATION", "A source quote needs both segment and exact text", 400)
                    _validate_citations(connection, identity["workspace"]["id"], [{key: item[key] for key in ("material_id", "segment_id", "quote")}])
            for key, child in item.items():
                visit(child, key)
        elif isinstance(item, list):
            for child in item:
                visit(child, field)
        elif isinstance(item, str) and field in {"source_ref", "source_refs", "evidence_refs", "support", "against"}:
            labels.add(item)
    visit(value)
    return {"checked_material_ids": sorted(checked), "checked_artifact_versions": [artifacts[key] for key in sorted(artifacts)], "unverified_reference_labels": sorted(labels)}


def _freeze_materials(connection, identity, work_id, snapshot, material_ids):
    saved = {item["id"]: item for item in snapshot.get("materials", [])}
    for material_id in material_ids:
        row = require_material_access(connection, identity, material_id, target_work_id=work_id)
        saved[material_id] = {"id": row["id"], "sha256": row["sha256"], "format": row["format"], "status": row["status"], "filename": row["filename"]}
    snapshot["materials"] = [saved[identifier] for identifier in sorted(saved)]


def _merge_source_qualifications(*qualifications):
    materials, labels, artifacts = set(), set(), {}
    for qualification in qualifications:
        materials.update(qualification["checked_material_ids"])
        labels.update(qualification["unverified_reference_labels"])
        for item in qualification["checked_artifact_versions"]:
            key = (item["artifact_id"], item["version"])
            previous = artifacts.get(key)
            if previous and previous["content_sha256"] != item["content_sha256"]:
                raise ApiError("SOURCE_CHANGED", "A referenced result changed within this business operation", 409)
            merged = {**(previous or {}), **item}
            if "posts" in item or previous and "posts" in previous:
                merged["posts"] = {**(previous or {}).get("posts", {}), **item.get("posts", {})}
            artifacts[key] = merged
    return {"checked_material_ids": sorted(materials), "checked_artifact_versions": [artifacts[key] for key in sorted(artifacts)],
            "unverified_reference_labels": sorted(labels)}


def _source_context(connection, identity, data: dict) -> dict:
    from .business import load_business_subject_context
    subjects, relations = [], []
    for reference in data.get("subjects", []):
        if not isinstance(reference, dict) or set(reference) != {"id", "version"}:
            raise ApiError("INVALID_INPUT", "A subject reference needs id and version", 400)
        row = connection.execute("SELECT * FROM business_subjects WHERE id=? AND workspace_id=?", (reference["id"], identity["workspace"]["id"])).fetchone()
        if row is None:
            raise _not_found()
        if row["canonical_subject_id"] is not None:
            raise ApiError("SUBJECT_MERGED", "Select the canonical Subject and its current version before revising this result", 409,
                           {"canonical_subject_id": row["canonical_subject_id"]})
        if row["directory_state"] != "active":
            raise ApiError("SUBJECT_REMOVED", "This subject is no longer available for new analysis", 409)
        if row["version"] != reference["version"]:
            raise ApiError("SUBJECT_VERSION_CONFLICT", "Review the updated subject before continuing", 409)
        scoped = load_business_subject_context(connection, identity, row["id"], work_id=data.get("work_id"))
        for evidence in scoped["evidence"]:
            if evidence["source"].get("material_id"):
                require_material_access(connection, identity, evidence["source"]["material_id"], target_work_id=data.get("work_id"))
        subjects.append({"id": row["id"], "version": row["version"], "name": row["name"], "kind": row["kind"],
                         "profile": json.loads(row["profile_json"]), "identity_review_status": row["identity_review_status"]})
    for reference in data.get("relationships", []):
        if not isinstance(reference, dict) or set(reference) != {"id", "version"}:
            raise ApiError("INVALID_INPUT", "A relationship reference needs id and version", 400)
        row = connection.execute("SELECT * FROM business_relations WHERE id=? AND workspace_id=?", (reference["id"], identity["workspace"]["id"])).fetchone()
        if row is None:
            raise _not_found()
        if row["directory_state"] != "active" or row["version"] != reference["version"]:
            raise ApiError("RELATIONSHIP_CHANGED", "Review the changed relationship before using it", 409)
        relation_subject = connection.execute(
            "SELECT directory_state,canonical_subject_id FROM business_subjects WHERE id=? AND workspace_id=?",
            (row["subject_id"], identity["workspace"]["id"]),
        ).fetchone()
        if relation_subject is None:
            raise _not_found()
        if relation_subject["canonical_subject_id"] is not None:
            raise ApiError("SUBJECT_MERGED", "Correct this relationship to the canonical Subject before revising the result", 409,
                           {"canonical_subject_id": relation_subject["canonical_subject_id"], "relationship_id": row["id"]})
        if relation_subject["directory_state"] != "active":
            raise ApiError("SUBJECT_REMOVED", "The relationship source Subject is no longer active", 409)
        if row["related_subject_id"]:
            related_subject = connection.execute(
                "SELECT directory_state,canonical_subject_id FROM business_subjects WHERE id=? AND workspace_id=?",
                (row["related_subject_id"], identity["workspace"]["id"]),
            ).fetchone()
            if related_subject is None:
                raise _not_found()
            if related_subject["canonical_subject_id"] is not None:
                raise ApiError("SUBJECT_MERGED", "Correct this relationship to the canonical related Subject before revising the result", 409,
                               {"canonical_subject_id": related_subject["canonical_subject_id"], "relationship_id": row["id"]})
            if related_subject["directory_state"] != "active":
                raise ApiError("RELATED_SUBJECT_REMOVED", "The relationship target Subject is no longer active", 409)
        scoped = load_business_subject_context(connection, identity, row["subject_id"], work_id=data.get("work_id"))
        selected = next((item for item in scoped["relationships"] if item["id"] == row["id"]), None)
        if selected is None:
            raise _not_found()
        for evidence in selected["review"]["evidence"]:
            if evidence["source"].get("material_id"):
                require_material_access(connection, identity, evidence["source"]["material_id"], target_work_id=data.get("work_id"))
        relations.append(dict(row))
    return {"subjects": subjects, "relationships": relations}


def _context_refs(context: dict) -> dict:
    if not isinstance(context, dict):
        context = {}
    return {
        "subjects": [{"id": item["id"], "version": item["version"]} for item in context.get("subjects", [])],
        "relationships": [{"id": item["id"], "version": item["version"]} for item in context.get("relationships", [])],
    }


def _select_context_refs(context: dict, context_changes: dict | None) -> tuple[dict, dict | None]:
    current = _context_refs(context)
    if context_changes is None:
        return current, None
    if (not isinstance(context_changes, dict)
            or set(context_changes) - {"subjects", "relationships", "reason"}
            or not (set(context_changes) & {"subjects", "relationships"})):
        raise ApiError("INVALID_CONTEXT_CHANGE", "Select a complete Subject or relationship reference list to adopt", 400)
    reason = _string(context_changes.get("reason"), "context_changes.reason", maximum=2000)
    selected = deepcopy(current)
    for field in ("subjects", "relationships"):
        if field not in context_changes:
            continue
        values = context_changes[field]
        if not isinstance(values, list) or len(values) > 500:
            raise ApiError("INVALID_CONTEXT_CHANGE", f"context_changes.{field} must be an array", 400)
        normalized, seen = [], set()
        for index, reference in enumerate(values):
            if not isinstance(reference, dict) or set(reference) != {"id", "version"}:
                raise ApiError("INVALID_CONTEXT_CHANGE", f"context_changes.{field}[{index}] needs an exact id and version", 400)
            identifier = _string(reference["id"], f"context_changes.{field}[{index}].id", maximum=200)
            version = _positive_version(reference["version"])
            if identifier in seen:
                raise ApiError("INVALID_CONTEXT_CHANGE", f"context_changes.{field} cannot repeat an identity", 400)
            seen.add(identifier)
            normalized.append({"id": identifier, "version": version})
        selected[field] = normalized
    if selected == current:
        raise ApiError("CONTEXT_CHANGE_NOOP", "Select a changed Subject or relationship context", 409)
    return selected, {"reason": reason}


def _recheck_table_sources(store, connection, identity, work_id, sources):
    for source in sources:
        material = require_material_access(connection, identity, source["material_id"], target_work_id=work_id)
        path = (store.root / material["relative_path"]).resolve()
        if material["sha256"] != source["sha256"] or not path.is_relative_to(store.root) or not path.is_file():
            raise ApiError("SOURCE_CHANGED", "A mapped source changed or became unavailable during computation", 409)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ApiError("SOURCE_CHANGED", "A mapped original changed during computation", 409)


def _calculate_table_formulas(content, source_hash, sheet_name, requested, *, is_cancelled=None):
    from openpyxl.utils.cell import get_column_letter
    from .office_spreadsheet import MAX_CHANGES, SpreadsheetError, calculate_spreadsheet_cells, inspect_spreadsheet
    try:
        inspection = inspect_spreadsheet(content)
        if inspection["sha256"] != source_hash:
            raise ApiError("SOURCE_CHANGED", "The workbook changed before formula calculation", 409)
        sheet = next((item for item in inspection["sheets"] if item["name"] == sheet_name), None)
        if sheet is None:
            raise ApiError("TABLE_SHEET_MISSING", "The selected sheet no longer belongs to this original", 409)
        by_address = {item["address"]: item for item in sheet["cells"]}
        selected, output = {}, {}
        for row, column in requested:
            cell = by_address.get(f"{get_column_letter(column + 1)}{row}")
            if cell is None:
                output[(row, column)] = {"reason": "XLSX_CALCULATION_REFERENCE_UNAVAILABLE"}
            else:
                selected[cell["object_id"]] = (row, column)
        identifiers = list(selected)
        for start in range(0, len(identifiers), MAX_CHANGES):
            if is_cancelled is not None and is_cancelled():
                from .business_analysis_economics import BusinessAnalysisCancelled
                raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")
            calculated = calculate_spreadsheet_cells(content, identifiers[start:start + MAX_CHANGES])
            if calculated["source_sha256"] != source_hash:
                raise ApiError("SOURCE_CHANGED", "The computed cells do not belong to the selected original", 409)
            for item in calculated["computed"] + calculated["unresolved"]:
                output[selected[item["object_id"]]] = item
        return output
    except SpreadsheetError as error:
        return {cell: {"reason": error.code} for cell in requested}


def _resolve_tables(store, connection, identity, work_id, bindings, inputs, *, is_cancelled=None):
    if not isinstance(bindings, list):
        raise ApiError("INVALID_INPUT", "tables must be an array of saved material bindings", 400)
    resolved, sources, targets = deepcopy(inputs), [], set()
    for binding in bindings:
        if is_cancelled is not None and is_cancelled():
            from .business_analysis_economics import BusinessAnalysisCancelled
            raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")
        if not isinstance(binding, dict) or set(binding) - {"input_id", "source_ref", "material_id", "material_sha256", "sheet", "target", "columns", "row_defaults", "source_row_field", "value_mappings"}:
            raise ApiError("INVALID_INPUT", "Unsupported table binding", 400)
        target, mapping = binding.get("target"), binding.get("columns")
        if not isinstance(target, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", target):
            raise ApiError("INVALID_INPUT", "Each table needs a business input field", 400)
        if not isinstance(mapping, dict) or not mapping or not all(isinstance(k, str) and isinstance(v, (str, dict)) for k, v in mapping.items()):
            raise ApiError("INVALID_INPUT", "Table column mappings must contain business field names and saved column names", 400)
        if not isinstance(binding.get("row_defaults", {}), dict):
            raise ApiError("INVALID_INPUT", "Table row defaults must describe known business context", 400)
        row_field = binding.get("source_row_field")
        if row_field is not None and (not isinstance(row_field, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", row_field) or row_field in mapping):
            raise ApiError("TABLE_MAPPING_CONFLICT", "The generated source-row identity must not replace an existing business field", 400)
        columns = {}
        value_mappings = binding.get("value_mappings", {})
        if not isinstance(value_mappings, dict) or any(field not in mapping or not isinstance(values, dict) or not all(isinstance(source, str) and isinstance(target_value, str) for source, target_value in values.items()) for field, values in value_mappings.items()):
            raise ApiError("INVALID_INPUT", "Value mappings must explicitly match saved column text to business values", 400)
        for field, definition in mapping.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", field):
                raise ApiError("INVALID_INPUT", "A mapped field must name a business property", 400)
            definition = {"column": definition, "type": "text"} if isinstance(definition, str) else definition
            if set(definition) != {"column", "type"} or not isinstance(definition["column"], str) or definition["type"] not in {"text", "decimal", "integer", "boolean", "date"}:
                raise ApiError("INVALID_INPUT", "Column types are text, decimal, integer, boolean or date", 400)
            columns[field] = definition
        material = require_material_access(connection, identity, binding.get("material_id"), target_work_id=work_id)
        if binding.get("source_ref") is not None:
            from .business_sources import _resolve_ref
            selected = _resolve_ref(connection, identity, work_id, binding["source_ref"])
            if selected["id"] != material["id"]:
                raise ApiError("SOURCE_REFERENCE_MISMATCH", "The table is not the selected source snapshot", 409)
        if binding.get("material_sha256") not in {None, material["sha256"]}:
            raise ApiError("SOURCE_CHANGED", "The table changed since its columns were selected", 409)
        path = (store.root / material["relative_path"]).resolve()
        if not path.is_relative_to(store.root) or not path.is_file():
            raise ApiError("SOURCE_FILE_UNAVAILABLE", "The saved table original is unavailable", 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != material["sha256"]:
            raise ApiError("SOURCE_CHANGED", "The saved table original no longer matches its version", 409)
        suffix = path.suffix.lower()
        suffix = suffix if suffix in {".csv", ".tsv", ".xlsx"} else "." + material["filename"].rsplit(".", 1)[-1].lower()
        rows = []
        formula_cells = {}
        formula_dates = set()
        recalculated_cells = []
        uncalculated_cells = []
        if suffix in {".csv", ".tsv"}:
            try:
                parsed = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter="\t" if suffix == ".tsv" else ","))
            except (UnicodeError, csv.Error) as error:
                raise ApiError("TABLE_UNREADABLE", "The saved table requires an explicit text-encoding correction", 400) from error
            header, values = (parsed[0], parsed[1:]) if parsed else ([], [])
        elif suffix == ".xlsx":
            from openpyxl import load_workbook
            from openpyxl.styles.numbers import is_date_format
            from .material_parser import _open_office
            try:
                archive, _ = _open_office(content, ("xl/workbook.xml", "[Content_Types].xml"))
                archive.close()
            except Exception as error:
                raise ApiError("TABLE_UNREADABLE", "The workbook failed the existing Office package/resource check", 400) from error
            workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
            workbook_epoch = workbook.epoch
            formula_workbook = load_workbook(io.BytesIO(content), data_only=False, read_only=True)
            try:
                sheet = binding.get("sheet")
                if sheet is None and len(workbook.sheetnames) != 1:
                    raise ApiError("TABLE_SHEET_REQUIRED", "Choose the business table sheet", 400)
                sheet = sheet or workbook.sheetnames[0]
                if sheet not in workbook.sheetnames:
                    raise ApiError("TABLE_SHEET_MISSING", "The selected table sheet is unavailable", 400)
                values_iterator = workbook[sheet].iter_rows(values_only=True)
                header = [str(item) if item is not None else "" for item in next(values_iterator, ())]
                values = list(values_iterator)
                for source_row in formula_workbook[sheet].iter_rows():
                    for cell in source_row:
                        if cell.data_type == "f":
                            if cell.row == 1:
                                raise ApiError("TABLE_HEADER_RECALCULATION_REQUIRED", "A formula-generated table header must be explicitly resolved before mapping", 409)
                            formula_cells[(cell.row, cell.column - 1)] = cell.value
                            if is_date_format(cell.number_format):
                                formula_dates.add((cell.row, cell.column - 1))
            finally:
                workbook.close()
                formula_workbook.close()
        else:
            raise ApiError("TABLE_FORMAT_UNSUPPORTED", "Use a saved CSV, TSV or XLSX business table", 400)
        if any(header.count(definition["column"]) != 1 for definition in columns.values()):
            raise ApiError("TABLE_MAPPING_CONFLICT", "A mapped column is missing or ambiguous; confirm the changed table layout", 409)
        positions = {field: header.index(definition["column"]) for field, definition in columns.items()}
        selected_formulas = {key for key in formula_cells if key[1] in positions.values()}
        calculated_formulas = _calculate_table_formulas(content, material["sha256"], sheet, selected_formulas, is_cancelled=is_cancelled) if selected_formulas else {}
        formula_row_numbers = {row_number for row_number, _ in formula_cells}
        for index, values_row in enumerate(values, 2):
            if is_cancelled is not None and (index == 2 or index % 128 == 0) and is_cancelled():
                from .business_analysis_economics import BusinessAnalysisCancelled
                raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")
            if not any(value is not None and value != "" for value in values_row) and index not in formula_row_numbers:
                continue
            row = deepcopy(binding.get("row_defaults", {}))
            for field, position in positions.items():
                value = values_row[position] if position < len(values_row) else None
                if (index, position) in formula_cells:
                    computed = calculated_formulas.get((index, position), {"reason": "XLSX_CALCULATION_UNAVAILABLE"})
                    cell_value = computed.get("value", {})
                    calculation = {"row": index, "column": header[position], "field": field, "formula": formula_cells[(index, position)],
                                   "object_id": computed.get("object_id"), "cached_value_present": value is not None}
                    if cell_value.get("verified") is True and cell_value.get("kind") in {"number", "boolean", "text", "date", "blank"}:
                        value = cell_value.get("value")
                        if cell_value["kind"] == "number" and columns[field]["type"] == "date" and (index, position) in formula_dates:
                            from openpyxl.utils.datetime import from_excel
                            if workbook_epoch.year == 1899 and Decimal("60") <= Decimal(value) < Decimal("61"):
                                raise ApiError("TABLE_DATE_INVALID", "The formula refers to Excel's nonexistent 1900 leap day", 400, {"row": index, "field": field})
                            try:
                                value = from_excel(float(value), epoch=workbook_epoch)
                            except (ValueError, OverflowError) as error:
                                raise ApiError("TABLE_DATE_INVALID", "The calculated calendar date is outside the supported range", 400, {"row": index, "field": field}) from error
                        recalculated_cells.append({**calculation, "computed_value": cell_value})
                    else:
                        uncalculated_cells.append({**calculation, "reason": computed.get("reason") or "XLSX_FORMULA_ERROR_VALUE", "error": cell_value.get("value")})
                        value = None
                if isinstance(value, datetime):
                    value = value.date().isoformat() if columns[field]["type"] == "date" and not any((value.hour, value.minute, value.second, value.microsecond)) else value.isoformat()
                value = None if value == "" else value
                if value is not None:
                    kind = columns[field]["type"]
                    if kind in {"decimal", "integer"}:
                        try:
                            number = Decimal(str(value))
                        except InvalidOperation as error:
                            raise ApiError("TABLE_VALUE_INVALID", "A mapped numeric cell is not a decimal", 400, {"row": index, "field": field}) from error
                        if isinstance(value, bool) or not number.is_finite() or number.adjusted() > 24 or number.as_tuple().exponent < -128 or len(number.as_tuple().digits) > 128 or kind == "integer" and number != number.to_integral_value():
                            raise ApiError("TABLE_VALUE_INVALID", "A mapped cell does not match its declared numeric type", 400, {"row": index, "field": field})
                        value = int(number) if kind == "integer" else str(number)
                    elif kind == "date":
                        try:
                            value = date.fromisoformat(str(value)).isoformat()
                        except ValueError as error:
                            raise ApiError("TABLE_DATE_INVALID", "A date column must contain calendar dates; preserve actual timestamp precision separately", 400, {"row": index, "field": field}) from error
                    elif kind == "boolean":
                        if type(value) is bool:
                            pass
                        elif isinstance(value, str) and value.lower() in {"true", "false"}:
                            value = value.lower() == "true"
                        else:
                            raise ApiError("TABLE_VALUE_INVALID", "Boolean table values must be true or false", 400, {"row": index, "field": field})
                    else:
                        value = str(value)
                    if field in value_mappings:
                        if str(value) not in value_mappings[field]:
                            raise ApiError("TABLE_VALUE_MAPPING_REQUIRED", "A source label has no confirmed business meaning", 409, {"row": index, "field": field, "source_value": str(value)})
                        value = value_mappings[field][str(value)]
                destination = row
                parts = field.split(".")
                for part in parts[:-1]:
                    if part not in destination:
                        destination[part] = {}
                    if not isinstance(destination[part], dict):
                        raise ApiError("TABLE_MAPPING_CONFLICT", "Mapped field conflicts with its known business context", 400)
                    destination = destination[part]
                destination[parts[-1]] = value
            if row_field:
                destination = row
                parts = row_field.split(".")
                for part in parts[:-1]:
                    destination = destination.setdefault(part, {})
                    if not isinstance(destination, dict):
                        raise ApiError("TABLE_MAPPING_CONFLICT", "A source-row field conflicts with the declared context", 400)
                if parts[-1] in destination:
                    raise ApiError("TABLE_MAPPING_CONFLICT", "Generated source identity cannot overwrite a declared value", 400)
                destination[parts[-1]] = f"{material['id']}:{material['sha256']}:{binding.get('sheet') or 'table'}:row:{index}"
            row.setdefault("source", {"material_id": material["id"], "material_sha256": material["sha256"], "filename": material["filename"], "title": material["title"], "sheet": binding.get("sheet"), "row": index})
            rows.append(row)
        destination = resolved
        target_parts = target.split(".")
        for part in target_parts[:-1]:
            destination = destination.setdefault(part, {})
            if not isinstance(destination, dict):
                raise ApiError("TABLE_MAPPING_CONFLICT", "A table target conflicts with the business input structure", 400)
        if target in targets:
            destination[target_parts[-1]].extend(rows)
        else:
            destination[target_parts[-1]] = rows
        targets.add(target)
        sources.append({"material_id": material["id"], "sha256": material["sha256"], "sheet": binding.get("sheet"),
                        "target": target, "columns": mapping, "source_data_rows": len(values), "used_nonempty_rows": len(rows),
                        "row_fingerprints": [_hash(row) for row in rows],
                        "recalculated_formula_cells": recalculated_cells,
                        "deduplication": "none", "missing_cells": "null", "value_mappings": value_mappings,
                        "time_precision": {field: "day" for field, definition in columns.items() if definition["type"] == "date"}, "uncalculated_formula_cells": uncalculated_cells})
    return resolved, sources


def _prepare_saved_business(store, connection, identity, work_id, operation, inputs, *, is_cancelled=None):
    if operation in {"financial_fact_corrections", "financial_quarterly_series"}:
        from .business_financial_sources import resolve_saved_financial_inputs
        return resolve_saved_financial_inputs(store, connection, identity, work_id, operation, inputs, is_cancelled=is_cancelled)
    if operation == "financial_mmm_analysis":
        if "_resolved_macro_controls" in inputs:
            raise ApiError("SAVED_MACRO_SOURCE_REQUIRED", "Macroeconomic controls must be derived from selected saved source versions", 400)
        from .business_financial_sources import resolve_saved_financial_inputs
        evaluation, saved_inputs = resolve_saved_financial_inputs(store, connection, identity, work_id, operation, inputs, is_cancelled=is_cancelled)
        macro_source = inputs.get("macro_source")
        if macro_source is None:
            return evaluation, saved_inputs
        if not isinstance(macro_source, dict) or set(macro_source) != {"artifacts", "metric", "region"} or not isinstance(macro_source["artifacts"], list) or not macro_source["artifacts"]:
            raise ApiError("INVALID_MACRO_SOURCE", "Choose saved macroeconomic releases, a metric and a region", 400)
        facts, source_versions = [], []
        for reference in macro_source["artifacts"]:
            if is_cancelled is not None and is_cancelled():
                from .business_analysis_economics import BusinessAnalysisCancelled
                raise BusinessAnalysisCancelled("BUSINESS_ANALYSIS_CANCELLED")
            if not isinstance(reference, dict) or set(reference) != {"artifact_id", "artifact_version"}:
                raise ApiError("INVALID_MACRO_SOURCE", "Each macroeconomic source needs its exact saved result version", 400)
            source_artifact = require_artifact_access(connection, identity, reference["artifact_id"])
            linked = connection.execute("SELECT 1 FROM work_artifacts WHERE work_id=? AND artifact_id=? AND workspace_id=?", (work_id, source_artifact["id"], identity["workspace"]["id"])).fetchone()
            if source_artifact["work_id"] != work_id and linked is None:
                raise ApiError("MACRO_SOURCE_WORK_MISMATCH", "Adopt this source into the current authorized work before using it", 403)
            source_scope = work_commission(connection, source_artifact["work_id"])
            if source_scope and source_scope != work_commission(connection, work_id):
                raise ApiError("CLIENT_SCOPE_MISMATCH", "Private macroeconomic work cannot cross client commissions", 403)
            version = _positive_version(reference["artifact_version"])
            source_version = connection.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?", (source_artifact["id"], version)).fetchone()
            if source_version is None:
                raise _not_found()
            source_snapshot = json.loads(source_version["input_snapshot_json"])
            source_business = source_snapshot.get("business", {})
            if source_business.get("operation") != "financial_source_analysis" or source_business.get("inputs", {}).get("source_format") != "macro-release":
                raise ApiError("MACRO_SOURCE_NOT_PARSED", "Select the exact saved macroeconomic release analysis", 409)
            sections = [section for section in source_business.get("generated_sections", []) if section.get("id") == "financial_source.macro_facts"]
            if len(sections) != 1:
                raise ApiError("MACRO_SOURCE_NOT_FOUND", "The selected result has no parsed macroeconomic source facts", 409)
            for source in source_snapshot.get("materials", []):
                material = require_material_access(connection, identity, source["id"], target_work_id=work_id)
                path = (store.root / material["relative_path"]).resolve()
                if material["sha256"] != source.get("sha256") or not path.is_relative_to(store.root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != material["sha256"]:
                    raise ApiError("MACRO_SOURCE_CHANGED", "A macroeconomic original changed or is no longer available", 409)
            source_facts = sections[0]["data"].get("macro_facts", sections[0]["data"].get("facts"))
            if not isinstance(source_facts, list):
                raise ApiError("MACRO_SOURCE_UNREADABLE", "The saved macroeconomic fact structure is unavailable", 409)
            facts.extend(deepcopy(source_facts))
            source_versions.append({**reference, "content_sha256": _hash(dict(source_version))})
        from .business_analysis_financial import resolve_macro_controls
        try:
            controls = resolve_macro_controls(facts, metric=macro_source["metric"], region=macro_source["region"],
                                              periods=[row["period"] for row in evaluation.get("periods", [])],
                                              vintage_basis=inputs.get("vintage_basis"), as_of=inputs.get("as_of"), is_cancelled=is_cancelled)
        except (ValueError, KeyError, TypeError) as error:
            raise ApiError("MACRO_SERIES_INVALID", str(error), 400) from error
        controls["saved_source_versions"] = source_versions
        evaluation.pop("macro_source", None)
        evaluation["_resolved_macro_controls"] = [controls]
        return evaluation, saved_inputs
    if operation != "financial_source_analysis":
        return inputs, inputs
    if "raw_source" in inputs:
        raise ApiError("SAVED_SOURCE_REQUIRED", "Financial original bytes are read from the selected saved material", 400)
    source = inputs.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
        raise ApiError("SAVED_SOURCE_REQUIRED", "Select a saved financial disclosure original", 400)
    material = require_material_access(connection, identity, source["material_id"], target_work_id=work_id)
    path = (store.root / material["relative_path"]).resolve()
    if not path.is_relative_to(store.root) or not path.is_file():
        raise ApiError("SOURCE_FILE_UNAVAILABLE", "The financial source original is unavailable", 409)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != material["sha256"]:
        raise ApiError("SOURCE_CHANGED", "The financial source failed its saved checksum", 409)
    if source.get("material_sha256") not in {None, material["sha256"]}:
        raise ApiError("SOURCE_CHANGED", "The selected financial source version changed", 409)
    saved = {**inputs, "source": {**source, "material_id": material["id"], "material_version": material["sha256"],
                                  "material_sha256": material["sha256"], "captured_at": material["created_at"]}}
    if inputs.get("source_format") in {"disclosure-extract", "regional-statement", "macro-release"}:
        raw = material["text"]
        if not raw:
            raise ApiError("SOURCE_TEXT_UNAVAILABLE", "This disclosure has no saved readable original text", 409)
    else:
        raw = content
    return {**saved, "raw_source": raw}, saved


def _insert_version(connection, identity, artifact, snapshot, citations, note, *, title=None):
    version, now = artifact["current_version"] + 1, utc_now()
    title = title or artifact["title"]
    connection.execute(
        "INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by,parent_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (artifact["id"], identity["workspace"]["id"], version, title, _render(snapshot), _json(citations), _json(snapshot), note, now, identity["user"]["id"], artifact["current_version"]),
    )
    connection.execute("UPDATE artifacts SET current_version=?,title=?,updated_at=? WHERE id=? AND current_version=?",
                       (version, title, now, artifact["id"], artifact["current_version"]))
    updated = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact["id"],)).fetchone()
    return _artifact_data(connection, updated)


def create_business_result(store, identity: dict, work_id: str, data: dict, idempotency_key: str, *, origin="user", commit_guard=None, is_cancelled=None) -> dict:
    if origin not in {"user", "intelligence"}:
        raise ValueError("Unknown business result origin")
    allowed = {"operation", "inputs", "citations", "subjects", "relationships", "tables", "title"}
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Unsupported business result field", 400)
    operation = _string(data.get("operation"), "operation", maximum=100)
    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        raise ApiError("INVALID_INPUT", "inputs must be a business input object", 400)
    table_bindings = normalized_table_bindings(data.get("tables", []))
    key, fingerprint = _key(idempotency_key), _hash({"work_id": work_id, "request": data, "origin": origin})
    with store.transaction(immediate=False) as connection:
        require_work_access(connection, identity, work_id)
        replay = _replay(connection, identity, "business_create", key, fingerprint)
        if replay is not None:
            return replay
        require_work_access(connection, identity, work_id, write=True)
        citations = _check_sources(connection, identity, work_id, data.get("citations", []))
        context = _source_context(connection, identity, {**data, "work_id": work_id})
        resolved, table_sources = _resolve_tables(store, connection, identity, work_id, table_bindings, inputs, is_cancelled=is_cancelled)
        qualification = _check_nested_sources(connection, identity, work_id, resolved)
        evaluation_inputs, resolved = _prepare_saved_business(store, connection, identity, work_id, operation, resolved, is_cancelled=is_cancelled)
        snapshot = _input_snapshot(connection, identity["workspace"]["id"], work_id, citations)
        financial_sources = evaluation_inputs.get("_resolved_financial_sources") or {}
        _freeze_materials(connection, identity, work_id, snapshot, set(qualification["checked_material_ids"]) | {source["material_id"] for source in table_sources}
                          | {source["material_id"] for source in financial_sources.get("source_materials", [])})
    # Numerical fitting must not hold the database's single writer lock.
    result = evaluate_business(operation, evaluation_inputs, is_cancelled=is_cancelled)
    for table in table_sources:
        if table["uncalculated_formula_cells"]:
            result["limitations"].append(f"业务表有 {len(table['uncalculated_formula_cells'])} 个映射公式格未能取得可用重算值；本版保未知，不把文件缓存冒充已核数值。")
    title = _string(data.get("title") or result["title"], "title", maximum=300)
    snapshot.update(kind="business_analysis", business={
        "schema_version": 1, "operation": operation, "inputs": resolved, "table_bindings": table_bindings,
        "input_snapshot_revision": 1, "source_changes": [],
        "table_sources": table_sources, "context": context, "result": result, "generated_sections": deepcopy(result["sections"]),
        "manual_sections": [], "manual_client_sections": [], "confirmed_sections": [], "changes": [], "decisions": [], "actions": [],
        "outcomes": [], "costs": [], "service_events": [], "method": "oevira-business/1",
        "input_provenance": "user_inputs_and_saved_materials" if origin == "user" else "intelligence_draft_over_authorized_materials", "source_qualification": qualification, "external_effect": "not_performed",
        "resolved_macro_controls": evaluation_inputs.get("_resolved_macro_controls"),
        "resolved_financial_sources": evaluation_inputs.get("_resolved_financial_sources"),
    })
    snapshot["business"]["input_sources"] = input_source_catalog(snapshot["business"])
    with store.transaction() as connection:
        work = require_work_access(connection, identity, work_id, write=True)
        replay = _replay(connection, identity, "business_create", key, fingerprint)
        if replay is not None:
            return replay
        if work["version"] != snapshot["work"]["version"]:
            raise ApiError("WORK_VERSION_CONFLICT", "The work changed during analysis; review the new requirements", 409)
        _check_sources(connection, identity, work_id, citations)
        _check_nested_sources(connection, identity, work_id, resolved)
        for source in snapshot["materials"]:
            current = require_material_access(connection, identity, source["id"], target_work_id=work_id)
            if current["sha256"] != source["sha256"]:
                raise ApiError("SOURCE_CHANGED", "A source changed during business analysis", 409)
        _source_context(connection, identity, {**data, "work_id": work_id})
        _recheck_table_sources(store, connection, identity, work_id, table_sources)
        rechecked_inputs, _ = _prepare_saved_business(store, connection, identity, work_id, operation, resolved, is_cancelled=is_cancelled)
        if _hash(rechecked_inputs.get("_resolved_macro_controls")) != _hash(evaluation_inputs.get("_resolved_macro_controls")):
            raise ApiError("MACRO_SOURCE_CHANGED", "The selected macroeconomic source or alignment changed during analysis", 409)
        if _evaluation_hash(rechecked_inputs) != _evaluation_hash(evaluation_inputs):
            raise ApiError("SOURCE_CHANGED", "The resolved financial inputs changed during analysis", 409)
        if commit_guard is not None:
            commit_guard(connection)
        now, artifact_id = utc_now(), str(uuid.uuid4())
        connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,1,?,?)",
                           (artifact_id, identity["workspace"]["id"], work_id, identity["user"]["id"], title, now, now))
        connection.execute("INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by,parent_version) VALUES(?,?,1,?,?,?,?,?,?,?,NULL)",
                           (artifact_id, identity["workspace"]["id"], title, _render(snapshot), _json(citations), _json(snapshot), "业务结果", now, identity["user"]["id"]))
        connection.execute("INSERT INTO work_artifacts VALUES(?,?,?,?)", (work_id, artifact_id, identity["workspace"]["id"], now))
        _remember(connection, identity, "business_create", key, fingerprint, artifact_id, 1)
        artifact = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return _artifact_data(connection, artifact)


def revise_business_result(store, identity: dict, artifact_id: str, data: dict, idempotency_key: str, *, origin="user", source_material_scope=None) -> dict:
    if origin not in {"user", "intelligence"}:
        raise ValueError("Unknown business revision origin")
    if origin == "intelligence" and {"confirm_sections", "accept_sections", "context_changes"}.intersection(data):
        raise ApiError("HUMAN_REVIEW_REQUIRED", "An automated revision cannot confirm content or select/replace identity context", 409)
    allowed = {"base_version", "input_changes", "section_edits", "confirm_sections", "accept_sections", "citations", "revision_note", "title", "campaign_schedule_controls", "context_changes"}
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Unsupported business revision field", 400)
    base, key = _positive_version(data.get("base_version")), _key(idempotency_key)
    changes = data.get("input_changes", {})
    edits = data.get("section_edits", [])
    controls = data.get("campaign_schedule_controls")
    context_changes = data.get("context_changes")
    if context_changes is not None and not isinstance(context_changes, dict):
        raise ApiError("INVALID_CONTEXT_CHANGE", "Context changes must select Subject or relationship references", 400)
    if controls is not None and not isinstance(controls, dict):
        raise ApiError("INVALID_INPUT", "Campaign controls must name the exact task changes", 400)
    if origin == "intelligence" and controls and (controls.get("commitment_releases") or controls.get("execution_corrections")):
        raise ApiError("HUMAN_REVIEW_REQUIRED", "Automated planning cannot release a confirmed commitment or rewrite actual execution records", 409)
    if not isinstance(changes, dict) or not isinstance(edits, list):
        raise ApiError("INVALID_INPUT", "Use named input changes and selected section edits", 400)
    fingerprint = _hash({"artifact_id": artifact_id, "request": data, "origin": origin})
    precomputed_result = None
    campaign_revision_context = None
    recompute = bool(changes) or bool(controls)
    expected_work_version = None
    context_refs = None
    context_preflight_hash = None
    context_change_metadata = None
    if recompute:
        with store.transaction(immediate=False) as connection:
            original_artifact, original_row, original_snapshot = _read_business(connection, identity, artifact_id)
            replay = _replay(connection, identity, "business_revision", key, fingerprint)
            if replay is not None:
                return replay
            require_artifact_access(connection, identity, artifact_id, write=True)
            expected_work_version = require_work_access(connection, identity, original_artifact["work_id"], write=True)["version"]
            if original_artifact["current_version"] != base:
                raise ApiError("VERSION_CONFLICT", "This business result has a newer version", 409, {"current_version": original_artifact["current_version"]})
            context_refs, context_change_metadata = _select_context_refs(
                original_snapshot["business"]["context"], context_changes,
            )
            preflight_context = _source_context(
                connection, identity, {**context_refs, "work_id": original_artifact["work_id"]},
            )
            context_preflight_hash = _hash(preflight_context)
            if controls and original_snapshot["business"]["operation"] != "campaign_schedule":
                raise ApiError("INVALID_INPUT", "Schedule controls apply to a saved Campaign schedule", 400)
            control_qualification = _check_nested_sources(connection, identity, original_artifact["work_id"], controls or {})
            if "source_changes" in changes:
                if origin == "intelligence":
                    raw_source_changes = changes["source_changes"]
                    if not isinstance(raw_source_changes, list) or not all(isinstance(change, dict) for change in raw_source_changes):
                        raise ApiError("INVALID_SOURCE_CHANGES", "Provide an array of exact source changes", 400)
                    new_refs = [change["new_ref"] for change in raw_source_changes if "new_ref" in change]
                    if new_refs and not isinstance(source_material_scope, (set, frozenset)):
                        raise ApiError("INTELLIGENCE_SOURCE_SCOPE_REQUIRED", "Resolve new sources against this model task's authorized materials", 403)
                    from .business_sources import _resolve_ref
                    for reference in new_refs:
                        material = _resolve_ref(connection, identity, original_artifact["work_id"], reference)
                        if material["id"] not in source_material_scope:
                            raise ApiError("INTELLIGENCE_TOOL_SCOPE_CHANGED", "This source is outside the authorized task materials", 403)
                candidate_inputs, candidate_bindings, candidate_tables, source_changes = prepare_source_revision(
                    store, connection, identity, original_artifact["work_id"], original_snapshot["business"], changes, legacy_revision=base)
            else:
                candidate_inputs = {**original_snapshot["business"]["inputs"], **deepcopy(changes)}
            if origin == "intelligence" and set(changes).intersection(source["target"].split(".")[0] for source in original_snapshot["business"]["table_sources"]):
                raise ApiError("SOURCE_DATA_CORRECTION_REQUIRED", "Reimport or explicitly correct source-bound observations; model revisions cannot replace original table values", 409)
            unprepared_inputs = deepcopy(candidate_inputs)
            if original_snapshot["business"]["operation"] in {"campaign_schedule", "strategy_proposal"}:
                from .campaign import prepare_campaign_revision_inputs
                try:
                    candidate_inputs, campaign_revision_context = prepare_campaign_revision_inputs(candidate_inputs, original_snapshot["business"], controls=controls, revision_note=data.get("revision_note"))
                except ValueError as error:
                    raise ApiError("INVALID_BUSINESS_REVISION", str(error), 409) from error
            _check_nested_sources(connection, identity, original_artifact["work_id"], candidate_inputs)
            evaluation_inputs, candidate_inputs = _prepare_saved_business(store, connection, identity, original_artifact["work_id"], original_snapshot["business"]["operation"], candidate_inputs)
        if campaign_revision_context is None:
            precomputed_result = evaluate_business(original_snapshot["business"]["operation"], evaluation_inputs)
        else:
            precomputed_result = evaluate_business(original_snapshot["business"]["operation"], evaluation_inputs, campaign_revision_context=campaign_revision_context)
    elif context_changes is not None:
        with store.transaction(immediate=False) as connection:
            original_artifact, original_row, original_snapshot = _read_business(connection, identity, artifact_id)
            replay = _replay(connection, identity, "business_revision", key, fingerprint)
            if replay is not None:
                return replay
            require_artifact_access(connection, identity, artifact_id, write=True)
            expected_work_version = require_work_access(connection, identity, original_artifact["work_id"], write=True)["version"]
            if original_artifact["current_version"] != base:
                raise ApiError("VERSION_CONFLICT", "This business result has a newer version", 409, {"current_version": original_artifact["current_version"]})
            context_refs, context_change_metadata = _select_context_refs(
                original_snapshot["business"]["context"], context_changes,
            )
            preflight_context = _source_context(
                connection, identity, {**context_refs, "work_id": original_artifact["work_id"]},
            )
            context_preflight_hash = _hash(preflight_context)
    with store.transaction() as connection:
        artifact, current, snapshot = _read_business(connection, identity, artifact_id)
        replay = _replay(connection, identity, "business_revision", key, fingerprint)
        if replay is not None:
            return replay
        require_artifact_access(connection, identity, artifact_id, write=True)
        if artifact["current_version"] != base:
            raise ApiError("VERSION_CONFLICT", "This business result has a newer version", 409, {"current_version": artifact["current_version"]})
        if (recompute or context_changes is not None) and require_work_access(connection, identity, artifact["work_id"])["version"] != expected_work_version:
            raise ApiError("WORK_VERSION_CONFLICT", "The work requirements changed during revision; review the current work", 409)
        business = snapshot["business"]
        if context_changes is not None:
            previous_refs = _context_refs(business["context"])
            updated_context = _source_context(
                connection, identity,
                {**context_refs, "work_id": artifact["work_id"]},
            )
            if _hash(updated_context) != context_preflight_hash:
                raise ApiError("BUSINESS_CONTEXT_CHANGED", "The selected Subject or relationship changed during revision", 409)
            changed_context_ids = set()
            for field in ("subjects", "relationships"):
                before = {item["id"]: item["version"] for item in previous_refs[field]}
                after = {item["id"]: item["version"] for item in context_refs[field]}
                changed_context_ids.update(
                    identifier for identifier in before.keys() | after.keys()
                    if before.get(identifier) != after.get(identifier)
                )
            invalidated_context_actions = []
            for action in business.get("actions", []):
                target_ids = {
                    item.get("id") for item in (action.get("target"), action.get("responsible"))
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                if action.get("approval") and target_ids.intersection(changed_context_ids):
                    action["approval"].update(valid=False, invalidated_reason="business_context_revised")
                    if action.get("state") == "approved":
                        action["state"] = "planned"
                    invalidated_context_actions.append(action.get("id"))
            business["context"] = updated_context
            context_revision = business.get("context_revision", 0) + 1
            business["context_revision"] = context_revision
            business.setdefault("context_revisions", []).append({
                "context_revision": context_revision,
                "from_artifact_version": base,
                "to_artifact_version": base + 1,
                "previous_refs": previous_refs,
                "selected_refs": deepcopy(context_refs),
                "changed_context_ids": sorted(changed_context_ids),
                "invalidated_action_ids": invalidated_context_actions,
                "reason": context_change_metadata["reason"],
                "recorded_by": identity["user"]["id"],
                "recorded_at": utc_now(),
                "record_status": "explicit_business_context_revision",
                "calculation_recomputed": bool(recompute),
                "identity_dependent_recalculation": False,
                "review_scope": "review identity-dependent report sections; preserve their existing text until an explicit revision",
            })
        revision_base_business = deepcopy(business) if recompute and business["operation"] in {"campaign_schedule", "strategy_proposal"} else None
        old_generated = {item["id"]: item for item in business["generated_sections"]}
        previous = {item["id"]: item for item in business["result"]["sections"]}
        input_values = {**business["inputs"], **deepcopy(changes)}
        citations = data.get("citations", json.loads(current["citations_json"]))
        if recompute:
            current_control_qualification = _check_nested_sources(connection, identity, artifact["work_id"], controls or {})
            if current_control_qualification != control_qualification:
                raise ApiError("SOURCE_CHANGED", "The schedule control evidence changed during revision", 409)
            if "source_changes" in changes:
                from .business_sources import _resolve_ref
                for change in source_changes:
                    if "new_ref" in change:
                        _resolve_ref(connection, identity, artifact["work_id"], change["new_ref"])
                _recheck_table_sources(store, connection, identity, artifact["work_id"], candidate_tables)
                input_values, new_bindings, new_tables = deepcopy(unprepared_inputs), deepcopy(candidate_bindings), deepcopy(candidate_tables)
                business["table_bindings"], business["table_sources"] = new_bindings, new_tables
                adopted_changes = [{**change, "provenance": "user_adopted_source_change" if origin == "user" else "intelligence_source_revision"} for change in source_changes]
                business["source_changes"] = deepcopy(business.get("source_changes", [])) + adopted_changes
                removed = {change["old_ref"]["id"] for change in source_changes
                           if change.get("old_ref", {}).get("kind") == "material"}
                retained = {source["material_id"] for source in new_tables}
                if "citations" not in data:
                    citations = [item for item in citations if item["material_id"] not in removed - retained]
            if business["operation"] in {"campaign_schedule", "strategy_proposal"}:
                from .campaign import prepare_campaign_revision_inputs
                try:
                    input_values, current_campaign_context = prepare_campaign_revision_inputs(input_values, revision_base_business, controls=controls, revision_note=data.get("revision_note"))
                except ValueError as error:
                    raise ApiError("INVALID_BUSINESS_REVISION", str(error), 409) from error
                if _hash(current_campaign_context) != _hash(campaign_revision_context):
                    raise ApiError("CAMPAIGN_CONTEXT_CHANGED", "The prior schedule or actual execution records changed during revision", 409)
            refs = {"subjects": [{"id": item["id"], "version": item["version"]} for item in business["context"]["subjects"]],
                    "relationships": [{"id": item["id"], "version": item["version"]} for item in business["context"]["relationships"]]}
            _source_context(connection, identity, {**refs, "work_id": artifact["work_id"]})
            business["source_qualification"] = _merge_source_qualifications(
                _check_nested_sources(connection, identity, artifact["work_id"], input_values), current_control_qualification)
            for source in business["table_sources"]:
                row = require_material_access(connection, identity, source["material_id"], target_work_id=artifact["work_id"])
                if row["sha256"] != source["sha256"]:
                    raise ApiError("SOURCE_CHANGED", "A business input table changed; adopt its new version explicitly", 409)
            rechecked_inputs, input_values = _prepare_saved_business(store, connection, identity, artifact["work_id"], business["operation"], input_values)
            if _hash(rechecked_inputs.get("_resolved_macro_controls")) != _hash(evaluation_inputs.get("_resolved_macro_controls")):
                raise ApiError("MACRO_SOURCE_CHANGED", "The selected macroeconomic source or alignment changed during revision", 409)
            business["resolved_macro_controls"] = deepcopy(rechecked_inputs.get("_resolved_macro_controls"))
            business["resolved_financial_sources"] = deepcopy(rechecked_inputs.get("_resolved_financial_sources"))
            if _evaluation_hash(rechecked_inputs) != _evaluation_hash(evaluation_inputs):
                raise ApiError("SOURCE_CHANGED", "The resolved business inputs changed during revision", 409)
            result = precomputed_result
            for table in business["table_sources"]:
                if table["target"].split(".")[0] in changes:
                    table["adopted_values_status"] = "user_corrected_after_import" if origin == "user" else "intelligence_draft"
                elif table.get("uncalculated_formula_cells"):
                    result["limitations"].append(f"业务表有 {len(table['uncalculated_formula_cells'])} 个映射公式格未能取得可用重算值；本版保未知，不把文件缓存冒充已核数值。")
        else:
            result = {**business["result"], "sections": deepcopy(business["generated_sections"])}
        if context_changes is not None:
            context_note = (
                "主体或关系引用已按用户选择更新到新版本；本次没有仅凭身份更正自动重写业务结论。"
                "请复核依赖身份的段落；原Artifact版本与当时引用仍可查看。"
            )
            limitations = list(result.get("limitations", []))
            if context_note not in limitations:
                limitations.append(context_note)
            result["limitations"] = limitations
        citations = _check_sources(connection, identity, artifact["work_id"], citations)
        generated = {item["id"]: item for item in result["sections"]}
        affected = {identifier for identifier in set(old_generated) | set(generated) if old_generated.get(identifier) != generated.get(identifier)}
        accepted = data.get("accept_sections", [])
        confirmed = data.get("confirm_sections", business["confirmed_sections"])
        if any(not isinstance(value, list) or not all(isinstance(item, str) for item in value) for value in (accepted, confirmed)):
            raise ApiError("INVALID_INPUT", "Section selections must be arrays of section ids", 400)
        if (set(accepted) | set(confirmed)) - (set(previous) | set(generated)):
            raise ApiError("SECTION_NOT_FOUND", "A selected business section no longer exists", 409)
        protected = set(business["manual_sections"]) | set(business.get("manual_client_sections", [])) | set(business["confirmed_sections"])
        automated_edits = {item["id"] for item in edits if isinstance(item, dict) and isinstance(item.get("id"), str)} & protected
        if origin == "intelligence" and automated_edits:
            raise ApiError("CONFIRMED_SECTION_AFFECTED", "An automated edit cannot replace confirmed or manually revised content", 409,
                           {"sections": [{"id": identifier, "before": previous.get(identifier),
                                          "proposed_edits": [item for item in edits if item.get("id") == identifier]} for identifier in sorted(automated_edits)]})
        conflict = affected & protected - set(accepted)
        if conflict:
            raise ApiError("CONFIRMED_SECTION_AFFECTED", "Review the exact changes to your confirmed or revised sections", 409,
                           {"sections": [{"id": identifier, "before": previous.get(identifier), "after": generated.get(identifier)} for identifier in sorted(conflict)]})
        effective = []
        for section in result["sections"]:
            identifier = section["id"]
            effective.append(deepcopy(previous[identifier] if identifier in previous and identifier not in affected else section))
        by_id = {item["id"]: item for item in effective}
        manual = set(business["manual_sections"]) - affected
        manual_client = set(business.get("manual_client_sections", [])) - affected
        edited = set()
        edited_bindings = set()
        client_reviewed = set()
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {"id", "body", "audience"} or not {"id", "body"} <= edit.keys() or not isinstance(edit["id"], str) or edit["id"] not in by_id:
                raise ApiError("INVALID_INPUT", "Each section edit needs a unique existing id and body", 400)
            if not isinstance(edit["body"], str):
                raise ApiError("INVALID_INPUT", "Section body must be text", 400)
            audience = edit.get("audience", "internal")
            if not isinstance(audience, str) or audience not in {"internal", "client"}:
                raise ApiError("INVALID_INPUT", "Choose the internal or client text for this revision", 400)
            binding = (edit["id"], audience)
            if binding in edited_bindings:
                raise ApiError("INVALID_INPUT", "Revise each audience of a section only once in a request", 400)
            edited_bindings.add(binding)
            if audience == "client":
                by_id[edit["id"]]["client_body"] = edit["body"]
                by_id[edit["id"]]["client_review_required"] = origin == "intelligence"
                if origin == "user":
                    manual_client.add(edit["id"])
                    client_reviewed.add(edit["id"])
            else:
                by_id[edit["id"]]["body"] = edit["body"]
                by_id[edit["id"]]["presentation_modified"] = True
                if "client_body" in by_id[edit["id"]]:
                    by_id[edit["id"]]["client_review_required"] = True
                if origin == "user":
                    manual.add(edit["id"])
            edited.add(edit["id"])
        for identifier in client_reviewed:
            by_id[identifier]["client_review_required"] = False
        touched = affected | edited
        for action in business["actions"]:
            if touched.intersection(action.get("section_ids", [])) and action.get("approval"):
                action["approval"]["valid"] = False
                action["approval"]["invalidated_reason"] = "dependent_result_revised"
                if action["state"] == "approved":
                    action["state"] = "planned"
        business.update(inputs=input_values, generated_sections=deepcopy(result["sections"]),
                        result={**result, "sections": effective}, manual_sections=sorted(manual), manual_client_sections=sorted(manual_client),
                        confirmed_sections=[identifier for identifier in confirmed if identifier in by_id],
                        changes=[{"id": identifier, "reason": "input_recomputed" if identifier in affected else "user_revision"} for identifier in sorted(touched)])
        if business.get("operation") == "campaign_schedule":
            schedule_output = next(
                (item for item in result["sections"] if item.get("id") == "campaign_schedule"),
                None,
            )
            schedule_data = schedule_output.get("data") if isinstance(schedule_output, dict) else None
            if isinstance(schedule_data, dict):
                business["campaign_current_commitment_status"] = deepcopy(
                    schedule_data.get("current_commitment_status", [])
                )
        if context_changes is not None:
            business["context_revisions"][-1]["review_required_sections"] = sorted(
                set(business.get("confirmed_sections", []))
                | set(business.get("manual_sections", []))
                | set(business.get("manual_client_sections", []))
            )
        business["last_revision_origin"] = origin
        business["input_change_fields"] = sorted(changes)
        if changes:
            business["input_snapshot_revision"] = business.get("input_snapshot_revision", base) + 1
        business["table_bindings"] = normalized_table_bindings(business.get("table_bindings", []))
        business["input_sources"] = input_source_catalog(business)
        business["impact"] = {"changed_sections": sorted(affected), "locked_sections": sorted(protected & affected),
                              "changed_input_fields": sorted(changes),
                              "changed_source_inputs": [item["input_id"] for item in changes.get("source_changes", [])],
                              "context_changed": context_changes is not None,
                              "context_changed_subject_ids": [item["id"] for item in context_refs["subjects"]] if context_changes is not None else [],
                              "context_changed_relationship_ids": [item["id"] for item in context_refs["relationships"]] if context_changes is not None else []}
        if controls:
            business["impact"]["campaign_schedule_controls"] = deepcopy(controls)
        if campaign_revision_context is not None and business["operation"] == "campaign_schedule":
            schedule = next((section["data"] for section in business["generated_sections"] if section["id"] == "campaign_schedule"), {})
            business.setdefault("campaign_revision_history", []).append({
                "source_version": base + 1, "campaign_schedule_controls": deepcopy(controls or {}),
                "revision_impact": deepcopy(schedule.get("revision_impact", {})),
                "revision_note": data.get("revision_note") or "按业务意见局部修订", "recorded_by": identity["user"]["id"],
                "recorded_at": utc_now(), "record_status": "saved_campaign_revision",
            })
        if "source_changes" in changes:
            snapshot["materials"] = []
        snapshot = _input_snapshot(connection, identity["workspace"]["id"], artifact["work_id"], citations, snapshot)
        _freeze_materials(connection, identity, artifact["work_id"], snapshot,
                          set(business.get("source_qualification", {}).get("checked_material_ids", [])) | {source["material_id"] for source in business["table_sources"]}
                          | {source["material_id"] for source in (business.get("resolved_financial_sources") or {}).get("source_materials", [])})
        title = _string(data.get("title", current["title"]), "title", maximum=300)
        saved = _insert_version(connection, identity, artifact, snapshot, citations, data.get("revision_note") or "按业务意见局部修订", title=title)
        _remember(connection, identity, "business_revision", key, fingerprint, artifact_id, saved["version"])
        return saved


def _money(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ApiError("INVALID_AMOUNT", "Use a finite nonnegative decimal amount", 400)
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ApiError("INVALID_AMOUNT", "Use a decimal amount", 400) from error
    if not amount.is_finite() or amount < 0 or amount.adjusted() > 24 or amount.as_tuple().exponent < -8:
        raise ApiError("INVALID_AMOUNT", "Amount exceeds the supported finite decimal precision", 400)
    return amount


def _active_costs(costs):
    superseded = {item["supersedes"] for item in costs if item.get("supersedes")}
    return [item for item in costs if item["id"] not in superseded]


def cost_totals(costs: list[dict]) -> dict:
    totals = {}
    for item in _active_costs(costs):
        currency = totals.setdefault(item["currency"], {"estimate": None, "committed": None, "actual": None, "refund": None, "known_subtotals": {}, "unknown_counts": {}})
        kind = item["kind"]
        if item["amount"] is None:
            currency["unknown_counts"][kind] = currency["unknown_counts"].get(kind, 0) + 1
        else:
            currency["known_subtotals"][kind] = str(Decimal(currency["known_subtotals"].get(kind, "0")) + Decimal(item["amount"]))
    for values in totals.values():
        for kind in ("estimate", "committed", "actual", "refund"):
            values[kind] = None if values["unknown_counts"].get(kind) else values["known_subtotals"].get(kind)
        values["net_actual"] = str(Decimal(values["actual"]) - Decimal(values["refund"] or "0")) if values["actual"] is not None and not values["unknown_counts"].get("refund") else None
    return totals


def _record_cost(business, payload, user_id, now):
    if set(payload) - {"id", "kind", "amount", "currency", "action_id", "refund_of", "supersedes", "reason", "occurred_at", "source_refs"}:
        raise ApiError("INVALID_INPUT", "Unsupported cost record field", 400)
    identifier = _string(payload.get("id"), "cost.id", maximum=150)
    kind, currency = payload.get("kind"), payload.get("currency")
    if kind not in {"estimate", "committed", "actual", "refund"} or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ApiError("INVALID_INPUT", "Cost kind and ISO currency are required", 400)
    amount = _money(payload.get("amount"))
    ledger = business["costs"]
    by_id = {item["id"]: item for item in ledger}
    if identifier in by_id:
        raise ApiError("COST_ALREADY_RECORDED", "Use the original request key or add a separate correction", 409)
    action = next((item for item in business["actions"] if item["id"] == payload.get("action_id")), None)
    if payload.get("action_id") and action is None:
        raise ApiError("ACTION_NOT_FOUND", "The cost's action does not exist in this result", 400)
    entry = {**payload, "amount": str(amount) if amount is not None else None, "recorded_by": user_id, "recorded_at": now,
             "provenance": "user_recorded_not_payment_verified", "action_snapshot": deepcopy(action)}
    if payload.get("occurred_at"):
        _timestamp(payload["occurred_at"])
    active_ids = {item["id"] for item in _active_costs(ledger)}
    if payload.get("supersedes"):
        prior = by_id.get(payload["supersedes"])
        if prior is None or prior["id"] not in active_ids or any(prior.get(field) != payload.get(field) for field in ("kind", "currency", "action_id", "refund_of")):
            raise ApiError("COST_CORRECTION_CONFLICT", "Correct the current cost with the same kind, currency, action and refund relationship", 409)
        _string(payload.get("reason"), "correction reason", maximum=3000)
    new_ledger = ledger + [entry]
    all_by_id = {item["id"]: item for item in new_ledger}
    active = _active_costs(new_ledger)
    def origin(item):
        while item.get("supersedes"):
            item = all_by_id[item["supersedes"]]
        return item["id"]
    actuals = {origin(item): item for item in active if item["kind"] == "actual"}
    grouped = {}
    for refund in (item for item in active if item["kind"] == "refund"):
        paid = all_by_id.get(refund.get("refund_of"))
        if paid is None or paid["kind"] != "actual":
            raise ApiError("REFUND_PAYMENT_REQUIRED", "A refund must reference a recorded actual payment", 400)
        current_paid = actuals[origin(paid)]
        if any(refund.get(field) != current_paid.get(field) for field in ("currency", "action_id")):
            raise ApiError("REFUND_SCOPE_MISMATCH", "Refund currency and action must match the payment", 400)
        if refund.get("occurred_at") and current_paid.get("occurred_at") and datetime.fromisoformat(refund["occurred_at"].replace("Z", "+00:00")) < datetime.fromisoformat(current_paid["occurred_at"].replace("Z", "+00:00")):
            raise ApiError("REFUND_TIME_INVALID", "Refund cannot precede the payment", 400)
        grouped.setdefault(origin(paid), []).append(refund)
    for original, refunds in grouped.items():
        paid_amount = _money(actuals[original]["amount"])
        refund_amounts = [_money(item["amount"]) for item in refunds]
        if paid_amount is not None and all(value is not None for value in refund_amounts) and sum(refund_amounts, Decimal(0)) > paid_amount:
            raise ApiError("REFUND_EXCEEDS_PAYMENT", "Total refunds exceed the corrected payment", 409)
    business["costs"] = new_ledger


def _reference(value, name):
    if not isinstance(value, dict) or set(value) != {"kind", "id", "label"}:
        raise ApiError("INVALID_INPUT", f"{name} needs kind, id and label", 400)
    for field in ("kind", "id", "label"):
        _string(value[field], f"{name}.{field}", maximum=500)
    return deepcopy(value)


def _resolve_business_reference(connection, identity, work_id, value, name):
    reference = _reference(value, name)
    kind, identifier = reference["kind"], reference["id"]
    scope = work_commission(connection, work_id)
    source_work = None
    if kind == "work":
        row = require_work_access(connection, identity, identifier)
        source_work = identifier
    elif kind == "artifact":
        row = require_artifact_access(connection, identity, identifier)
        source_work = row["work_id"]
    elif kind == "case":
        from .business_cases import require_case_access
        row = require_case_access(connection, identity, identifier)
        source_work = row["work_id"]
    elif kind == "commission":
        from .collaboration import require_commission_access
        row = require_commission_access(connection, identity, identifier)
        if identifier != scope:
            raise ApiError("CLIENT_SCOPE_MISMATCH", "The business object belongs to another commission", 403)
    elif kind == "subject":
        row = connection.execute("SELECT * FROM business_subjects WHERE id=? AND workspace_id=? AND directory_state='active'",
                                 (identifier, identity["workspace"]["id"])).fetchone()
        if row is None:
            raise _not_found()
        _source_context(connection, identity, {"work_id": work_id, "subjects": [{"id": identifier, "version": row["version"]}]})
    elif kind == "member":
        row = connection.execute("SELECT u.id,u.name,m.role FROM users u JOIN memberships m ON u.id=m.user_id WHERE u.id=? AND m.workspace_id=? AND m.active=1",
                                 (identifier, identity["workspace"]["id"])).fetchone()
        if row is None:
            raise _not_found()
        if work_id:
            require_work_access(connection, {"workspace": identity["workspace"], "user": {"id": identifier}, "role": row["role"]}, work_id)
    else:
        # Compatibility for an actual user-declared external party or location.
        # Such a label does not acquire a directory identity or platform authority.
        reference["qualification"] = "user_declared_not_resolved"
        return reference
    if source_work and work_commission(connection, source_work) != scope:
        raise ApiError("CLIENT_SCOPE_MISMATCH", "The business object belongs to another commission", 403)
    reference["qualification"] = "resolved_workspace_object"
    for field in ("version", "current_version"):
        if field in row.keys():
            reference["resolved_version"] = row[field]
            break
    return reference


def _apply_command(business, command, payload, identity, now):
    user_id = identity["user"]["id"]
    actions = {item["id"]: item for item in business["actions"]}
    if command == "record_decision":
        record = {"id": str(uuid.uuid4()), "choice": _string(payload.get("choice"), "choice", maximum=3000),
                  "reason": _string(payload.get("reason"), "reason", maximum=5000),
                  "alternatives": payload.get("alternatives", []), "counterevidence": payload.get("counterevidence", []),
                  "constraints": payload.get("constraints", {}), "section_ids": payload.get("section_ids", []),
                  "recorded_by": user_id, "recorded_at": now, "approval": "not_granted_by_decision"}
        if payload.get("option_id") is not None:
            option_id = _string(payload["option_id"], "option_id", maximum=200)
            options = [item for section in business["result"]["sections"] if section["id"] == "decision.options"
                       for item in section["data"]["alternatives"] if item["id"] == option_id and item["input_set"] == "base"]
            if len(options) != 1:
                raise ApiError("DECISION_OPTION_NOT_FOUND", "Select a saved option from this exact comparison", 409)
            if options[0]["eligibility"] != "eligible":
                raise ApiError("DECISION_OPTION_NOT_QUALIFIED", "Resolve this option's hard constraints and missing data before adopting it", 409)
            record.update(option_id=option_id, option_snapshot=deepcopy(options[0]), input_snapshot_revision=business.get("input_snapshot_revision"),
                          comparison_snapshot=deepcopy([section["data"] for section in business["result"]["sections"] if section["id"] in {"decision.question", "decision.comparison"}]))
        business["decisions"].append(record)
    elif command == "plan_action":
        identifier = _string(payload.get("id"), "action.id", maximum=150)
        if identifier in actions:
            raise ApiError("ACTION_EXISTS", "Action identity already exists", 409)
        dependencies = payload.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) or item not in actions for item in dependencies):
            raise ApiError("ACTION_DEPENDENCY_MISSING", "Plan prerequisite actions before dependent actions", 400)
        action = {"id": identifier, "title": _string(payload.get("title"), "action.title", maximum=500),
                  "target": _reference(payload.get("target"), "target"), "responsible": _reference(payload.get("responsible"), "responsible"),
                  "deliverable": _string(payload.get("deliverable"), "deliverable", maximum=5000),
                  "due_at": _timestamp(payload["due_at"], "due_at") if payload.get("due_at") else None,
                  "depends_on": dependencies, "section_ids": payload.get("section_ids", []), "state": "planned",
                  "approval": None, "records": [], "created_by": user_id, "created_at": now,
                  "external_effect": payload.get("external_effect", False)}
        from .business_execution_actions import validate_execution
        action["execution"] = validate_execution(payload.get("execution"))
        if type(action["external_effect"]) is not bool:
            raise ApiError("INVALID_INPUT", "external_effect must be boolean", 400)
        if not isinstance(action["section_ids"], list) or not all(isinstance(item, str) for item in action["section_ids"]) or not set(action["section_ids"]) <= {item["id"] for item in business["result"]["sections"]}:
            raise ApiError("SECTION_NOT_FOUND", "Action dependencies must name current result sections", 400)
        business["actions"].append(action)
    elif command in {"approve_action", "withdraw_approval", "cancel_action", "record_action"}:
        action = actions.get(payload.get("action_id"))
        if action is None:
            raise ApiError("ACTION_NOT_FOUND", "The selected action does not exist", 404)
        if command == "approve_action":
            if action["state"] not in {"planned", "approved", "failed"}:
                raise ApiError("ACTION_ALREADY_ATTEMPTED", "Reconcile the existing action before approving another attempt", 409)
            action["approval"] = {"id": str(uuid.uuid4()), "valid": True, "approved_by": user_id, "approved_at": now,
                                  "scope": _string(payload.get("scope"), "approval.scope", maximum=3000),
                                  "authority": "internal_action_decision_not_platform_authorization",
                                  "action_digest": _hash({key: action[key] for key in ("id", "target", "responsible", "deliverable", "due_at", "depends_on", "section_ids")})}
            action["state"] = "approved"
        elif command in {"withdraw_approval", "cancel_action"}:
            if action["approval"]:
                action["approval"].update(valid=False, withdrawn_by=user_id, withdrawn_at=now)
            if command == "cancel_action":
                reason = _string(payload.get("reason"), "cancellation.reason", maximum=5000)
                action["records"].append({"id": str(uuid.uuid4()), "status": "cancellation_requested", "description": reason,
                                          "recorded_by": user_id, "recorded_at": now, "occurred_at": now,
                                          "provenance": "internal_plan_control", "external_execution_by_product": False,
                                          "real_effect_reversed": False})
                action["cancellation_requested"] = True
                if action["state"] in {"planned", "approved", "failed"}:
                    action["state"] = "cancelled"
            elif action["state"] == "approved":
                action["state"] = "planned"
        else:
            status = payload.get("status")
            if status not in {"performed", "failed", "unknown"}:
                raise ApiError("INVALID_INPUT", "Record performed, failed or unknown execution separately", 400)
            if action["state"] in {"recorded", "executed", "cancelled"}:
                raise ApiError("ACTION_ALREADY_RECORDED", "The action already has a recorded performance", 409)
            if action["state"] in {"effect_unknown", "execution_unknown", "awaiting_outcome_verification", "cancellation_requested"} and not payload.get("reconciliation_of"):
                raise ApiError("RECEIPT_LOOKUP_REQUIRED", "Reconcile the original uncertain action before recording another attempt", 409)
            if payload.get("reconciliation_of") and payload["reconciliation_of"] not in {item["id"] for item in action["records"] if item["status"] in {"unknown", "effect_unknown", "browser_input_delivered"}}:
                raise ApiError("INVALID_RECONCILIATION", "Reconciliation must name this action's uncertain record", 409)
            for dependency in action["depends_on"]:
                if actions[dependency]["state"] not in {"recorded", "executed"}:
                    raise ApiError("ACTION_DEPENDENCY_UNFULFILLED", "The prerequisite action has no recorded performance", 409)
            record = {"id": str(uuid.uuid4()), "status": status, "occurred_at": _timestamp(payload.get("occurred_at")),
                      "description": _string(payload.get("description"), "description", maximum=5000),
                      "source_refs": payload.get("source_refs", []), "recorded_by": user_id, "recorded_at": now,
                      "provenance": "user_recorded_not_independently_verified", "reconciliation_of": payload.get("reconciliation_of"),
                      "external_execution_by_product": False, "approval_at_recording": deepcopy(action["approval"])}
            action["records"].append(record)
            action["state"] = {"performed": "recorded", "failed": "failed", "unknown": "effect_unknown"}[status]
    elif command == "record_cost":
        _record_cost(business, payload, user_id, now)
    elif command == "record_outcome":
        target = _reference(payload.get("target"), "target")
        metric, window = payload.get("metric"), payload.get("window")
        if not isinstance(metric, dict) or not all(isinstance(metric.get(field), str) and metric[field].strip() for field in ("id", "name", "definition", "unit")):
            raise ApiError("INVALID_INPUT", "An outcome needs a stable metric identity, definition and unit", 400)
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ApiError("INVALID_INPUT", "An observation needs a start and end window", 400)
        start, end = _timestamp(window["start"], "window.start"), _timestamp(window["end"], "window.end")
        observed = _timestamp(payload.get("observed_at"), "observed_at")
        times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end, observed)]
        if not times[0] < times[1] <= times[2]:
            raise ApiError("OUTCOME_WINDOW_INVALID", "The observed result must follow its complete observation window", 400)
        value = payload.get("value")
        if value is not None:
            try:
                numeric = Decimal(str(value))
            except InvalidOperation as error:
                raise ApiError("INVALID_INPUT", "Outcome value must be finite or unknown", 400) from error
            if isinstance(value, bool) or not numeric.is_finite():
                raise ApiError("INVALID_INPUT", "Outcome value must be finite or unknown", 400)
            value = str(numeric)
        record = {"id": str(uuid.uuid4()), "target": target, "metric": metric, "window": window, "value": value,
                  "observed_at": observed, "source_refs": payload.get("source_refs", []),
                  "denominator": payload.get("denominator"), "coverage": payload.get("coverage"),
                  "interpretation": _string(payload.get("interpretation"), "interpretation", maximum=5000),
                  "provenance": "user_recorded_observation", "causality": "not_established", "recorded_by": user_id, "recorded_at": now}
        business["outcomes"].append(record)
    elif command == "record_service_event":
        event = payload.get("event")
        if event not in {"acknowledged", "investigated", "response_sent", "promised", "fulfilled", "feedback", "closed", "reopened", "observed"}:
            raise ApiError("INVALID_INPUT", "Unknown service event", 400)
        record = {"id": str(uuid.uuid4()), "event": event, "case_id": payload.get("case_id"),
                  "occurred_at": _timestamp(payload.get("occurred_at")), "description": _string(payload.get("description"), "description", maximum=5000),
                  "commitment_id": payload.get("commitment_id"), "source_refs": payload.get("source_refs", []),
                  "recorded_by": user_id, "recorded_at": now, "provenance": "user_recorded_service_event",
                  "resolution_verified": False}
        if event == "fulfilled" and not payload.get("commitment_id"):
            raise ApiError("COMMITMENT_REQUIRED", "Link performance to the exact promise", 400)
        if event == "fulfilled":
            commitments = [item for section in business["result"]["sections"] if section["id"] == "commitments" for item in section["data"].get("commitments", [])]
            promised = [item for item in business["service_events"] if item["event"] == "promised" and item.get("case_id") == record["case_id"]]
            known_ids = {item["id"] for item in commitments} | {item["commitment_id"] for item in promised if item.get("commitment_id")}
            if payload["commitment_id"] not in known_ids:
                raise ApiError("COMMITMENT_NOT_FOUND", "The named promise does not belong to this service work", 409)
        if event == "reopened" and not any(item["event"] == "closed" and item.get("case_id") == record["case_id"] for item in business["service_events"]):
            raise ApiError("CASE_NOT_CLOSED", "Reopening must refer to an existing closure", 409)
        business["service_events"].append(record)
    else:
        raise ApiError("UNKNOWN_BUSINESS_COMMAND", "This business action is not supported", 400)


def _link_case_event(connection, identity, work_id, business, payload):
    from .business_cases import require_case_event
    if not payload.get("case_event_id") or "case_event_version" not in payload:
        raise ApiError("CASE_EVENT_REQUIRED", "Record the service action on its Case, then select that exact saved event for this report", 409)
    case, event = require_case_event(connection, identity, payload["case_id"], payload["case_event_id"],
                                    _positive_version(payload["case_event_version"]))
    if work_commission(connection, case["work_id"]) != work_commission(connection, work_id):
        raise ApiError("CLIENT_SCOPE_MISMATCH", "A Case event cannot be included in another client's report", 403)
    mapping = {"acknowledge": "acknowledged", "assign": "assigned", "investigate": "investigated", "prepare_response": "response_prepared",
               "promise": "promised", "record_fulfilment": "fulfilled", "feedback": "feedback", "resolve": "resolution_assessed",
               "close": "closed", "reopen": "reopened", "link_case": "related_case", "unlink_case": "case_relation_removed",
               "revise_problem": "problem_revised", "replace_sources": "sources_replaced", "review_sources": "sources_reviewed"}
    if event["kind"] == "record_response":
        label = {"reported_sent": "response_sent", "delivery_unknown": "response_delivery_unknown", "reported_failed": "response_failed", "reported_read": "response_read"}[event["payload"]["state"]]
    else:
        label = mapping.get(event["kind"], "case_event_recorded")
    if payload.get("event") is not None and payload["event"] != label:
        raise ApiError("CASE_EVENT_MISMATCH", "The report's service state must match the selected saved Case event", 409)
    if payload.get("occurred_at") is not None and payload["occurred_at"] != event["occurred_at"]:
        raise ApiError("CASE_EVENT_MISMATCH", "A report cannot change the occurrence time of a saved Case event", 409)
    if any(item.get("case_event_id") == event["id"] for item in business["service_events"]):
        raise ApiError("CASE_EVENT_ALREADY_LINKED", "This exact Case event is already included in the report", 409)
    details = event["payload"]
    context = event["case_context_at_event"]
    description = details.get("note") or details.get("deliverable") or details.get("assessment") or context["title"]
    record = {"id": str(uuid.uuid4()), "event": label, "case_id": case["id"], "case_event_id": event["id"], "case_event_version": event["case_version"],
              "case_event": event, "case_context_at_event": context, "occurred_at": event["occurred_at"], "description": description,
              "commitment_id": event["id"] if event["kind"] == "promise" else details.get("commitment_id"),
              "source_refs": deepcopy(details.get("source_refs", [])), "recorded_by": identity["user"]["id"], "recorded_at": utc_now(),
              "provenance": "saved_case_event_reference", "resolution_verified": False}
    if payload.get("description"):
        record["report_note"] = _string(payload["description"], "report_note", maximum=5000)
    business["service_events"].append(record)


def record_business_command(store, identity: dict, artifact_id: str, data: dict, idempotency_key: str) -> dict:
    if set(data) - {"base_version", "command", "payload", "citations", "revision_note"} or not isinstance(data.get("payload"), dict):
        raise ApiError("INVALID_INPUT", "Provide a named business command and its payload", 400)
    base, key = _positive_version(data.get("base_version")), _key(idempotency_key)
    data = {**data, "command": _string(data.get("command"), "command", maximum=100)}
    for field in ("action_id", "case_id", "commitment_id", "reconciliation_of"):
        if data["payload"].get(field) is not None:
            _string(data["payload"][field], field, maximum=200)
    if "source_refs" in data["payload"] and not isinstance(data["payload"]["source_refs"], list):
        raise ApiError("INVALID_INPUT", "source_refs must be an array", 400)
    fingerprint = _hash({"artifact_id": artifact_id, "request": data})
    with store.transaction() as connection:
        artifact, current, snapshot = _read_business(connection, identity, artifact_id)
        replay = _replay(connection, identity, "business_command", key, fingerprint)
        if replay is not None:
            return replay
        if data["command"] == "approve_action":
            _review_access(connection, identity, artifact_id)
        elif data["command"] in {"cancel_action", "cancel_collaboration", "withdraw_approval"}:
            if data["command"] == "withdraw_approval":
                action = next((item for item in snapshot["business"]["actions"] if item["id"] == data["payload"].get("action_id")), None)
                if action is None:
                    raise ApiError("ACTION_NOT_FOUND", "The selected action does not exist", 404)
                owner_id = (action.get("approval") or {}).get("approved_by")
                require_artifact_control_access(connection, identity, artifact_id, approval_owner_id=owner_id)
            else:
                require_artifact_control_access(connection, identity, artifact_id)
        else:
            require_artifact_access(connection, identity, artifact_id, write=True)
        if artifact["current_version"] != base:
            raise ApiError("VERSION_CONFLICT", "This business result has changed", 409, {"current_version": artifact["current_version"]})
        # Control of an existing plan must remain possible after its sources are withdrawn.
        control_only = data["command"] in {"withdraw_approval", "cancel_action", "cancel_collaboration", "decline_collaboration_candidate",
                                            "record_action", "record_cost", "record_outcome", "record_service_event"}
        citations = json.loads(current["citations_json"])
        if not control_only or "citations" in data:
            citations = _check_sources(connection, identity, artifact["work_id"], data.get("citations", citations))
        payload = data["payload"]
        _check_nested_sources(connection, identity, artifact["work_id"], payload)
        if data["command"] in {"record_decision", "plan_action", "approve_action", "adopt_collaboration_candidate"}:
            business = snapshot["business"]
            _check_nested_sources(connection, identity, artifact["work_id"], business["inputs"])
            for source in snapshot.get("materials", []):
                material = require_material_access(connection, identity, source["id"], target_work_id=artifact["work_id"])
                if material["sha256"] != source["sha256"]:
                    raise ApiError("SOURCE_CHANGED", "Reconcile the changed source before adopting or approving this analysis", 409)
            _source_context(connection, identity, {"work_id": artifact["work_id"],
                            "subjects": [{"id": item["id"], "version": item["version"]} for item in business["context"]["subjects"]],
                            "relationships": [{"id": item["id"], "version": item["version"]} for item in business["context"]["relationships"]]})
        resolved_refs = {field: _resolve_business_reference(connection, identity, artifact["work_id"], payload[field], field)
                         for field in ("target", "responsible") if field in payload}
        if payload.get("case_id"):
            from .business_cases import require_case_access
            case = require_case_access(connection, identity, payload["case_id"])
            if work_commission(connection, case["work_id"]) != work_commission(connection, artifact["work_id"]):
                raise ApiError("CLIENT_SCOPE_MISMATCH", "A service record cannot be linked across client commissions", 403)
        from .campaign_collaboration import _COMMANDS, apply_collaboration_command
        if data["command"] in _COMMANDS:
            apply_collaboration_command(connection, identity, artifact, current, snapshot, data["command"], payload, utc_now())
        elif data["command"] == "record_service_event" and payload.get("case_id"):
            _link_case_event(connection, identity, artifact["work_id"], snapshot["business"], payload)
        else:
            _apply_command(snapshot["business"], data["command"], payload, identity, utc_now())
            if data["command"] == "record_decision":
                snapshot["business"]["decisions"][-1]["source_result"] = {"artifact_id": artifact_id, "artifact_version": base, "content_sha256": _hash(dict(current))}
            elif data["command"] == "plan_action":
                snapshot["business"]["actions"][-1].update(resolved_refs)
            elif data["command"] == "record_outcome":
                snapshot["business"]["outcomes"][-1].update(resolved_refs)
            elif data["command"] == "cancel_action":
                from .job_control import WAITING_STATES, control_job
                jobs = connection.execute("SELECT * FROM jobs WHERE workspace_id=? AND kind='business_action' AND json_extract(payload_json,'$.artifact_id')=? "
                                          "AND json_extract(payload_json,'$.action_id')=?",
                                          (identity["workspace"]["id"], artifact_id, payload["action_id"])).fetchall()
                active_states = WAITING_STATES | {"running", "pause_requested", "paused", "cancel_requested", "interrupted"}
                active_jobs = [job for job in jobs if job["status"] in active_states]
                dispatched = any(job["status"] in {"running", "pause_requested", "cancel_requested", "interrupted"}
                                 or json.loads(job["payload_json"]).get("dispatch_started_at") for job in active_jobs)
                action = next(item for item in snapshot["business"]["actions"] if item["id"] == payload["action_id"])
                if dispatched and action["state"] == "cancelled":
                    action["state"] = "cancellation_requested"
                for job in active_jobs:
                    control_job(None, store, identity, job["id"], "cancel", {
                        "operation_id": "business-cancel:" + _hash({"job_id": job["id"], "request": fingerprint}),
                        "base_control_revision": job["control_revision"],
                    }, connection=connection)
        saved = _insert_version(connection, identity, artifact, snapshot, citations, data.get("revision_note") or data["command"])
        _remember(connection, identity, "business_command", key, fingerprint, artifact_id, saved["version"])
        return saved


def restore_business_result(store, identity, artifact_id: str, data: dict, idempotency_key: str) -> dict:
    if set(data) - {"base_version", "restore_version", "revision_note"}:
        raise ApiError("INVALID_INPUT", "Unsupported business restore field", 400)
    base = _positive_version(data.get("base_version"))
    source_version = _positive_version(data.get("restore_version"))
    key, fingerprint = _key(idempotency_key), _hash({"artifact_id": artifact_id, "request": data})
    with store.transaction() as connection:
        artifact, current, snapshot = _read_business(connection, identity, artifact_id)
        replay = _replay(connection, identity, "business_restore", key, fingerprint)
        if replay is not None:
            return replay
        require_artifact_access(connection, identity, artifact_id, write=True)
        if artifact["current_version"] != base:
            raise ApiError("VERSION_CONFLICT", "This business result has changed", 409, {"current_version": artifact["current_version"]})
        _, source, historical = _read_business(connection, identity, artifact_id, version=source_version)
        if snapshot["business"]["operation"] == "campaign_schedule":
            from .campaign import preserve_campaign_revision_state_on_restore
            historical["business"].update(preserve_campaign_revision_state_on_restore(
                historical["business"], snapshot["business"], current_version=artifact["current_version"], restore_version=source_version))
        # A restored plan does not reverse money, real actions, observations or service history.
        for field in ("decisions", "actions", "costs", "outcomes", "service_events", "collaborations", "collaboration_decisions"):
            if field in snapshot["business"]:
                historical["business"][field] = deepcopy(snapshot["business"][field])
        current_sections = {item["id"]: item for item in snapshot["business"]["result"]["sections"]}
        restored_sections = {item["id"]: item for item in historical["business"]["result"]["sections"]}
        affected = {identifier for identifier in current_sections.keys() | restored_sections.keys()
                    if current_sections.get(identifier) != restored_sections.get(identifier)}
        for action in historical["business"]["actions"]:
            if action.get("approval") and affected.intersection(action.get("section_ids", [])):
                action["approval"].update(valid=False, invalidated_reason="plan_restored")
                if action["state"] == "approved":
                    action["state"] = "planned"
        historical["business"]["restored_from"] = source_version
        historical["business"]["changes"] = [{"id": identifier, "reason": "plan_restored"} for identifier in sorted(affected)]
        # Preserve historical citations as saved evidence, without asserting current source use.
        historical["business"]["current_source_qualification"] = "recheck_required_before_recomputation"
        saved = _insert_version(connection, identity, artifact, historical, json.loads(source["citations_json"]),
                                data.get("revision_note") or f"恢复方案第 {source_version} 版，保留实际行动与费用记录", title=source["title"])
        connection.execute("UPDATE artifact_versions SET restore_version=? WHERE artifact_id=? AND version=?", (source_version, artifact_id, saved["version"]))
        _remember(connection, identity, "business_restore", key, fingerprint, artifact_id, saved["version"])
        return saved


def register_business_execution_routes(app) -> None:
    if "business_operations" in app.view_functions:
        return
    store = app.extensions["oevira.store"]
    app.extensions.setdefault("oevira.job_resume_validators", {})["business_analysis"] = validate_business_analysis_resume
    from .business_execution_actions import register_business_action_routes
    register_business_action_routes(app)

    @app.get("/api/materials/<material_id>/business-table")
    def business_table_preview(material_id):
        return _success(preview_business_table(store, g.identity, material_id, work_id=request.args.get("work_id")))

    @app.get("/api/business/operations")
    def business_operations():
        from .business_contracts import CONTRACT_VERSION, list_operation_contracts
        operations = sorted(ECONOMICS_OPERATIONS | CAMPAIGN_OPERATIONS | MEASUREMENT_OPERATIONS | SERVICE_OPERATIONS | EVENT_OPERATIONS | DECISION_OPERATIONS)
        return _success({
            "operations": operations,
            "formats": ["docx", "pptx", "xlsx", "pdf", "html"],
            "result_kind": "business_analysis",
            "contract_version": CONTRACT_VERSION,
            "contracts": list_operation_contracts(),
        })

    @app.get("/api/business/operations/<operation>")
    def business_operation_contract(operation):
        from .business_contracts import BusinessContractError, get_contract
        try:
            return _success(get_contract(operation))
        except BusinessContractError as error:
            raise ApiError(error.code, str(error), error.status) from error

    @app.post("/api/works/<work_id>/business-results")
    def business_create_result(work_id):
        return _success(create_business_result(store, g.identity, work_id, _body(), _idempotency_key()), 201)

    @app.post("/api/works/<work_id>/business-jobs")
    def business_queue_analysis(work_id):
        from .api import _job_data
        data, identity = _body(), g.identity
        if set(data) - {"operation", "inputs", "citations", "subjects", "relationships", "tables", "title"} or not isinstance(data.get("inputs"), dict):
            raise ApiError("INVALID_INPUT", "Provide a professional business operation and its inputs", 400)
        _string(data.get("operation"), "operation", maximum=100)
        if data.get("operation") not in ECONOMICS_OPERATIONS | CAMPAIGN_OPERATIONS | MEASUREMENT_OPERATIONS | SERVICE_OPERATIONS | EVENT_OPERATIONS | DECISION_OPERATIONS:
            raise ApiError("UNKNOWN_BUSINESS_OPERATION", "This business operation is not supported", 400)
        key = _key(_idempotency_key())
        fingerprint = _hash({"work_id": work_id, "request": data})
        with store.transaction() as connection:
            work = require_work_access(connection, identity, work_id)
            prior = connection.execute("SELECT * FROM jobs WHERE workspace_id=? AND kind='business_analysis' AND idempotency_key=?", (identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another business analysis", 409)
                return _success(_job_data(prior))
            require_work_access(connection, identity, work_id, write=True)
            _check_sources(connection, identity, work_id, data.get("citations", []))
            qualification = _check_nested_sources(connection, identity, work_id, data)
            context = _source_context(connection, identity, {**data, "work_id": work_id})
            materials = []
            for identifier in qualification["checked_material_ids"]:
                material = require_material_access(connection, identity, identifier, target_work_id=work_id)
                materials.append({"id": identifier, "sha256": material["sha256"]})
            job_id, now = str(uuid.uuid4()), utc_now()
            payload = {"request": data, "work_version": work["version"],
                       "input_versions": {"materials": materials, "qualification": qualification, "context_sha256": _hash(context)}}
            connection.execute("INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,progress_total,progress_label,created_at,updated_at) VALUES(?,?,?,?,'business_analysis','queued',?,?,?,2,'等待专业计算',?,?)",
                               (job_id, identity["workspace"]["id"], identity["user"]["id"], work_id, _json(payload), fingerprint, key, now, now))
            return _success(_job_data(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), 202)

    @app.post("/api/business-results/<artifact_id>/revisions")
    def business_revise_result(artifact_id):
        return _success(revise_business_result(store, g.identity, artifact_id, _body(), _idempotency_key()))

    @app.post("/api/business-results/<artifact_id>/commands")
    def business_record_command(artifact_id):
        return _success(record_business_command(store, g.identity, artifact_id, _body(), _idempotency_key()))

    @app.post("/api/business-results/<artifact_id>/restore")
    def business_restore_result(artifact_id):
        return _success(restore_business_result(store, g.identity, artifact_id, _body(), _idempotency_key()))


def validate_business_analysis_resume(connection, identity, row, resume_context):
    if resume_context:
        raise ApiError("INVALID_INPUT", "Resume preserves the original inputs; revise changed requirements in a new analysis", 400)
    payload = json.loads(row["payload_json"])
    work = require_work_access(connection, identity, row["work_id"], write=True)
    if work["version"] != payload["work_version"]:
        raise ApiError("WORK_VERSION_CONFLICT", "The business requirements changed after the analysis was queued", 409)
    frozen = payload.get("input_versions")
    if not isinstance(frozen, dict):
        raise ApiError("JOB_INPUT_VERSION_UNAVAILABLE", "This historical task has no frozen input versions; create a new analysis from its retained request", 409)
    request_data = payload["request"]
    _check_sources(connection, identity, row["work_id"], request_data.get("citations", []))
    current = _check_nested_sources(connection, identity, row["work_id"], request_data)
    if current != frozen["qualification"]:
        raise ApiError("SOURCE_CHANGED", "The analysis sources changed while execution was stopped", 409)
    for source in frozen["materials"]:
        material = require_material_access(connection, identity, source["id"], target_work_id=row["work_id"])
        if material["sha256"] != source["sha256"]:
            raise ApiError("SOURCE_CHANGED", "A selected material changed while execution was stopped", 409)
    context = _source_context(connection, identity, {**request_data, "work_id": row["work_id"]})
    if _hash(context) != frozen["context_sha256"]:
        raise ApiError("BUSINESS_CONTEXT_CHANGED", "The selected business identities or relationships changed", 409)


def process_business_analysis_job(app, claim):
    from .worker import _advance, _set_job_error, claim_can_commit
    store = app.extensions["oevira.store"]
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    row = store.row("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (claim["id"], claim["workspace_id"]))
    if row is None or not _advance(store, claim["id"], claim["generation"], 0, 2, "读取本次业务依据"):
        return
    payload = json.loads(row["payload_json"])
    def cancelled():
        current = store.row("SELECT cancel_requested,status,lease_generation FROM jobs WHERE id=?", (claim["id"],))
        if current is None or current["cancel_requested"] or current["status"] != "running" or current["lease_generation"] != claim["generation"]:
            return True
        try:
            with store.transaction(immediate=False) as connection:
                require_work_access(connection, identity, claim["work_id"], write=True)
        except ApiError:
            return True
        return False
    def guard(connection):
        current = connection.execute("SELECT * FROM jobs WHERE id=?", (claim["id"],)).fetchone()
        if current is None or current["lease_generation"] != claim["generation"] or current["status"] != "running" or current["cancel_requested"]:
            raise ApiError("JOB_CANCELLED_OR_REPLACED", "The analysis was cancelled or replaced before saving", 409)
        validate_business_analysis_resume(connection, identity, current, {})
    try:
        with store.transaction(immediate=False) as connection:
            guard(connection)
        result = create_business_result(store, identity, claim["work_id"], payload["request"],
                                        "business-job:" + claim["id"], commit_guard=guard, is_cancelled=cancelled)
        with store.transaction() as connection:
            current = connection.execute("SELECT * FROM jobs WHERE id=?", (claim["id"],)).fetchone()
            if not claim_can_commit(connection, claim, allow_stopping=True):
                return
            state = "partial" if current["cancel_requested"] else "completed"
            connection.execute("UPDATE jobs SET status=?,artifact_id=?,progress_completed=2,progress_label='专业结果已保存',updated_at=? WHERE id=? AND lease_generation=?", (state, result["id"], utc_now(), claim["id"], claim["generation"]))
    except ApiError as error:
        _set_job_error(store, claim["id"], claim["generation"], error.code, error.message)
    except Exception as error:
        from .business_analysis_economics import BusinessAnalysisCancelled
        if isinstance(error, BusinessAnalysisCancelled):
            with store.transaction() as connection:
                if not claim_can_commit(connection, claim, allow_stopping=True):
                    return
                connection.execute("UPDATE jobs SET status='cancelled',error_code='BUSINESS_ANALYSIS_CANCELLED',error_message='The business computation was cancelled before saving.',updated_at=? WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested')",
                                   (utc_now(), claim["id"], claim["generation"]))
        else:
            _set_job_error(store, claim["id"], claim["generation"], "BUSINESS_ANALYSIS_FAILED", "The business computation did not finish; its source inputs remain saved.")


def preview_business_table(store, identity, material_id, *, work_id=None):
    with store.transaction(immediate=False) as connection:
        if work_id:
            require_work_access(connection, identity, work_id)
        material = require_material_access(connection, identity, material_id, target_work_id=work_id)
        path = (store.root / material["relative_path"]).resolve()
        if not path.is_relative_to(store.root) or not path.is_file():
            raise ApiError("SOURCE_FILE_UNAVAILABLE", "The saved business table is unavailable", 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != material["sha256"]:
            raise ApiError("SOURCE_CHANGED", "The table original no longer matches its saved version", 409)
        suffix = material["filename"].rsplit(".", 1)[-1].lower()
        sheets = []
        def scan(name, iterator):
            header = [str(value) if value is not None else "" for value in next(iterator, ())]
            count = nonempty = 0
            for row in iterator:
                count += 1
                nonempty += int(any(value is not None and value != "" for value in row))
            return {"name": name, "columns": [{"index": index, "name": value, "ambiguous": not value or header.count(value) > 1} for index, value in enumerate(header)],
                    "source_data_rows": count, "nonempty_rows": nonempty, "coverage": "complete_original_rows"}
        if suffix in {"csv", "tsv"}:
            try:
                iterator = csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter="\t" if suffix == "tsv" else ",")
                sheets.append(scan(None, iterator))
            except (UnicodeError, csv.Error) as error:
                raise ApiError("TABLE_UNREADABLE", "The source encoding or table structure needs correction", 400) from error
        elif suffix == "xlsx":
            from openpyxl import load_workbook
            from .material_parser import _open_office
            try:
                archive, _ = _open_office(content, ("xl/workbook.xml", "[Content_Types].xml"))
                archive.close()
                workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
                formula_workbook = load_workbook(io.BytesIO(content), data_only=False, read_only=True)
            except Exception as error:
                raise ApiError("TABLE_UNREADABLE", "The workbook failed the existing Office package/resource check", 400) from error
            try:
                for sheet in workbook:
                    item = scan(sheet.title, sheet.iter_rows(values_only=True))
                    formula_counts, cached_counts = {}, {}
                    uncached_formula_rows = 0
                    formula_rows = formula_workbook[sheet.title].iter_rows()
                    for source_row, cached_row in zip(formula_rows, sheet.iter_rows(values_only=True)):
                        if not any(value is not None and value != "" for value in cached_row) and any(cell.data_type == "f" for cell in source_row):
                            uncached_formula_rows += 1
                        for cell in source_row:
                            if cell.data_type == "f":
                                index = cell.column - 1
                                formula_counts[index] = formula_counts.get(index, 0) + 1
                                cached_counts[index] = cached_counts.get(index, 0) + int(index < len(cached_row) and cached_row[index] is not None)
                    for column in item["columns"]:
                        column.update(formula_count=formula_counts.get(column["index"], 0), cached_formula_count=cached_counts.get(column["index"], 0), needs_recalculation=bool(formula_counts.get(column["index"])))
                    item["nonempty_rows"] += uncached_formula_rows
                    sheets.append(item)
            finally:
                workbook.close()
                formula_workbook.close()
        else:
            raise ApiError("TABLE_FORMAT_UNSUPPORTED", "Choose a saved CSV, TSV or XLSX business table", 400)
        return {"material_id": material_id, "sha256": material["sha256"], "version": material["sha256"], "filename": material["filename"], "format": suffix, "sheets": sheets,
                "source_row_identity": "material + original hash + sheet + original row; not a verified customer or transaction identity"}
