"""Evidence-led incident briefs and the distinction between response and resolution."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

CONTENT_LABELS = {"original": "原帖", "repost": "转载", "root_comment": "根评论", "reply": "回复", "correction": "更正", "unknown": "类型待核"}
COVERAGE_LABELS = {"complete": "记录为已完整检查", "partial": "部分检查", "not_checked": "未检查", "unavailable": "不可访问", "failed": "检查失败"}
CLAIM_LABELS = {"unverified": "尚未核实", "reviewed_supported": "有支持依据的复核记录", "disputed": "存在争议", "retracted": "已撤回", "opinion": "意见表达"}
SEVERITY_LABELS = {"unknown": "待核", "routine": "一般", "material": "重要", "critical": "严重"}
TIME_LABELS = {"occurred": "事件发生", "observable": "首次可观察", "discovered": "发现", "verified": "核实", "approved": "批准", "acted": "实际行动"}
AUDIENCE_LABELS = {"customer": "顾客", "member": "会员", "tenant": "商户", "partner": "合作方", "employee": "员工", "public": "公众"}
IMPACT_LABELS = {"observed": "有观察记录", "association": "关联观察", "conditional_scenario": "条件情景", "unknown": "尚待核实"}


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value.strip()


def _optional_text(value, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text or null")
    return value.strip() or None


def _time(value, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} needs an explicit timezone")
    return value


def _section(identifier, title, body, data):
    return {"id": identifier, "title": title, "body": body, "data": data}


def evaluate_service(operation: str, inputs: dict) -> dict:
    if not isinstance(operation, str) or operation not in {"incident_brief", "service_response"}:
        raise ValueError("Unsupported service operation")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    goal = _text(inputs.get("goal"), "goal")
    cutoff = _time(inputs.get("as_of"), "as_of")
    observations = inputs.get("observations", [])
    claims, coverage = inputs.get("claims", []), inputs.get("coverage", [])
    timeline_input = inputs.get("timeline", [])
    impacts_input = inputs.get("impacts", [])
    if any(
        not isinstance(value, list)
        for value in (observations, claims, coverage, timeline_input, impacts_input)
    ):
        raise ValueError("observations, claims, coverage, timeline and impacts must be arrays")
    original_rows, latest, limitations = [], {}, []
    for index, item in enumerate(observations):
        if not isinstance(item, dict):
            raise ValueError("Each observation must be an object")
        platform, native_id = _text(item.get("platform"), "platform"), _text(item.get("native_id"), "native_id")
        content = _text(item.get("text"), "observation.text")
        captured = _time(item.get("captured_at"), "captured_at")
        if datetime.fromisoformat(captured.replace("Z", "+00:00")) > datetime.fromisoformat(cutoff.replace("Z", "+00:00")):
            limitations.append(f"{platform}/{native_id} 的第 {index + 1} 行晚于信息截止，未纳入本版。")
            continue
        kind = item.get("kind", "unknown")
        if not isinstance(kind, str) or kind not in {"original", "repost", "root_comment", "reply", "correction", "unknown"}:
            raise ValueError("Unknown observation kind")
        information_origin = _optional_text(item.get("information_origin"), "information_origin")
        row = {
            **item,
            "platform": platform,
            "native_id": native_id,
            "text": content,
            "captured_at": captured,
            "kind": kind,
            "information_origin": information_origin,
            "record_status": "user_supplied_snapshot_not_independently_verified",
        }
        original_rows.append(row)
        key = (platform, native_id)
        previous = latest.get(key)
        if previous is None or datetime.fromisoformat(captured.replace("Z", "+00:00")) > datetime.fromisoformat(previous["captured_at"].replace("Z", "+00:00")):
            latest[key] = row
        elif captured == previous["captured_at"] and row != previous:
            raise ValueError("Conflicting snapshots at the same capture time require a source correction")
    counts = dict(Counter(item["kind"] for item in latest.values()))
    source_families = Counter(
        item.get("information_origin") or f"{item['platform']}:{item['native_id']}"
        for item in latest.values()
    )
    coverage_rows = []
    for item in coverage:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("status"), str)
            or item["status"] not in {"complete", "partial", "not_checked", "unavailable", "failed"}
        ):
            raise ValueError("Coverage needs source and a valid check status")
        source = _text(item.get("source"), "coverage.source")
        coverage_rows.append(
            {
                **item,
                "source": source,
                "record_status": "user_supplied_coverage_not_independently_verified",
            }
        )
        if item["status"] != "complete":
            limitations.append(f"{source}：{COVERAGE_LABELS[item['status']]}，不能据此断言无风险或风险下降。")
    if not coverage:
        limitations.append("未提供逐来源检查覆盖，本版仅描述已保存样本。")
    elif any(item["status"] == "complete" for item in coverage_rows):
        limitations.append("来源检查完整性来自当前记录，本次未独立核实检查过程。")
    claim_rows = []
    for item in claims:
        if not isinstance(item, dict):
            raise ValueError("Each claim must be an object")
        identifier, target = _text(item.get("id"), "claim.id"), _text(item.get("target"), "claim.target")
        text = _text(item.get("text"), "claim.text")
        status = item.get("status", "unverified")
        if not isinstance(status, str) or status not in {"unverified", "reviewed_supported", "disputed", "retracted", "opinion"}:
            raise ValueError("Unknown claim review status")
        support, against = item.get("support", []), item.get("against", [])
        if not isinstance(support, list) or not isinstance(against, list):
            raise ValueError("Claim evidence references must be arrays")
        if status == "reviewed_supported":
            if not support:
                raise ValueError("A reviewed claim needs supporting references")
            _text(item.get("reviewed_by"), "claim.reviewed_by")
            _text(item.get("review_basis"), "claim.review_basis")
        severity = item.get("severity", "unknown")
        if not isinstance(severity, str) or severity not in {"unknown", "routine", "material", "critical"}:
            raise ValueError("Unknown claim severity")
        claim_rows.append({**item, "id": identifier, "target": target, "text": text, "status": status,
                           "support": support, "against": against, "severity": severity,
                           "review_provenance": "user_recorded_review" if status == "reviewed_supported" else "not_independently_verified",
                           "reference_status": "user_supplied_references_not_permission_checked_by_this_operation"})
    if len({row["id"] for row in claim_rows}) != len(claim_rows):
        raise ValueError("Claim ids must be unique")
    timeline = []
    for item in timeline_input:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("kind"), str)
            or item["kind"] not in {"occurred", "observable", "discovered", "verified", "approved", "acted"}
        ):
            raise ValueError("Timeline entries must distinguish occurrence, discovery, verification, approval and action")
        timestamp = _time(item.get("at"), "timeline.at")
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) > datetime.fromisoformat(cutoff.replace("Z", "+00:00")):
            limitations.append("一项时间线记录晚于本版信息截止，保在原输入，未写作已经发生的事实。")
            continue
        timeline.append({
            **item,
            "at": timestamp,
            "description": _text(item.get("description"), "timeline.description"),
            "record_status": "user_supplied_timeline_not_independently_verified",
        })
    timeline.sort(key=lambda item: datetime.fromisoformat(item["at"].replace("Z", "+00:00")))
    impacts = []
    for item in impacts_input:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("audience"), str)
            or item["audience"] not in {"customer", "member", "tenant", "partner", "employee", "public"}
        ):
            raise ValueError("An impact needs a concrete affected audience")
        claim_type = item.get("claim_type", "unknown")
        if not isinstance(claim_type, str) or claim_type not in {"observed", "association", "conditional_scenario", "unknown"}:
            raise ValueError("Incident briefs do not establish causal business impact")
        impacts.append({**item, "claim_type": claim_type, "causality": "not_established",
                        "record_status": "user_supplied_impact_not_independently_verified"})
    commitments = inputs.get("commitments", [])
    if not isinstance(commitments, list):
        raise ValueError("commitments must be an array")
    for item in commitments:
        if not isinstance(item, dict):
            raise ValueError("Each commitment must be an object")
        for key in ("id", "deliverable", "owner", "fulfilment_basis"):
            _text(item.get(key), f"commitment.{key}")
        if item.get("due_at") is not None:
            _time(item["due_at"], "commitment.due_at")
        if "approved_for_response" in item and type(item["approved_for_response"]) is not bool:
            raise ValueError("commitment.approved_for_response must be boolean")
    if len({item["id"] for item in commitments}) != len(commitments):
        raise ValueError("Commitment identities must be unique within this service result")
    facts = [row for row in claim_rows if row["status"] == "reviewed_supported"]
    open_claims = [row for row in claim_rows if row["status"] in {"unverified", "disputed"}]
    acknowledgment = inputs.get("acknowledgment")
    if acknowledgment is not None and not isinstance(acknowledgment, str):
        raise ValueError("acknowledgment must be text or null")
    response_lines = [acknowledgment or "我们已收到相关反馈，正在核实具体情况。"]
    response_lines.extend(row["text"] for row in facts if row.get("public_use_allowed") is True)
    response_lines.extend(item["deliverable"] for item in commitments if item.get("approved_for_response") is True)
    if open_claims:
        response_lines.append("尚未核实的事项将继续核查；后续说明以可核实事实为准。")
    response_channel = _optional_text(inputs.get("response_channel"), "response_channel")
    response = {"draft": "\n\n".join(response_lines), "status": "draft_not_sent",
                "channel": response_channel, "fact_ids": [row["id"] for row in facts if row.get("public_use_allowed") is True],
                "commitment_ids": [item["id"] for item in commitments if item.get("approved_for_response") is True]}
    claim_body = "\n\n".join(f"**{row['target']}** · {CLAIM_LABELS[row['status']]}\n\n{row['text']}\n\n严重性：{SEVERITY_LABELS[row['severity']]}；支持依据 {len(row['support'])} 项，反证 {len(row['against'])} 项。" for row in claim_rows) or "尚无逐主张核验记录。"
    coverage_body = "| 内容单位 | 当前样本数 |\n|---|---:|\n" + "\n".join(f"| {CONTENT_LABELS[kind]} | {count} |" for kind, count in sorted(counts.items()))
    if not counts:
        coverage_body = "没有在截止前可用的原生内容样本；不据此判断无风险。"
    if coverage_rows:
        coverage_body += "\n\n| 来源 | 实际检查记录 |\n|---|---|\n" + "\n".join(
            f"| {row['source'].replace('|', '／').replace(chr(10), ' ')} | {COVERAGE_LABELS[row['status']]} |" for row in coverage_rows)
    sections = [
        _section("purpose", "本次业务问题", goal, {"goal": goal, "as_of": cutoff, "case_id": inputs.get("case_id")}),
        _section("coverage", "来源覆盖与传播样本", coverage_body + "\n\n跨平台账号与内容数量均不等于独立人数。", {"coverage": coverage_rows, "counts": counts, "snapshot_count": len(original_rows), "native_content_count": len(latest), "information_origins": dict(source_families), "observations": original_rows}),
        _section("claims", "事实、主张与反证", claim_body, {"claims": claim_rows}),
        _section("timeline", "事件与处置时间线", "\n".join(f"- {row['at']} · {TIME_LABELS[row['kind']]} · {row['description']}" for row in timeline) or "各时点尚待有据补充。", {"timeline": timeline}),
        _section("impacts", "顾客与商业关系影响", "\n".join(f"- {AUDIENCE_LABELS[row['audience']]}：{row.get('description', '待补影响依据')}（{IMPACT_LABELS[row['claim_type']]}）" for row in impacts) or "没有足够业务数据确认到访、退订、销售或租金影响。", {"impacts": impacts}),
        _section("response", "回应草稿", response["draft"], response),
        _section("commitments", "责任与服务承诺", "\n".join(f"- {item['owner']}：{item['deliverable']}；期限 {item.get('due_at') or '待确认'}；履约依据 {item['fulfilment_basis']}" for item in commitments) or "尚未形成可履约的服务承诺。", {"commitments": commitments}),
    ]
    limitations.extend([
        "分类与事实复核来自当前记录；尚未由独立行业评审签收。",
        "引用的存在不代表相关主张已被独立核实；支持、反证与原话仍需分别核对。",
        "回应草稿、实际发布、服务履约和核实解决分别记录，当前未执行外部动作。",
    ])
    title = _optional_text(inputs.get("title"), "title") or "事件与服务响应"
    return {"title": title, "sections": sections, "limitations": limitations}
