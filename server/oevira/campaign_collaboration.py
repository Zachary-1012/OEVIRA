"""Collaboration lifecycle records embedded in the owning business Artifact."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid

from .api import ApiError, _not_found
from .collaboration import (
    require_artifact_access,
    require_artifact_approval,
    require_material_access,
)


_COMMANDS = {
    "adopt_collaboration_candidate",
    "decline_collaboration_candidate",
    "record_collaboration_terms",
    "confirm_collaboration_agreement",
    "submit_collaboration_deliverable",
    "record_collaboration_feedback",
    "request_collaboration_revision",
    "accept_collaboration_deliverable",
    "reject_collaboration_deliverable",
    "cancel_collaboration",
    "record_collaboration_cost",
}
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def apply_collaboration_command(
    connection,
    identity: dict,
    artifact_row,
    current_version_row,
    business_snapshot: dict,
    command: str,
    payload: dict,
    now: str,
) -> None:
    """Apply one collaboration event to the current business Artifact snapshot.

    This function never writes a table or sends a request. The business result
    owner commits the mutated snapshot as its next immutable Artifact version.
    """
    if command not in _COMMANDS:
        raise ApiError("UNKNOWN_COLLABORATION_COMMAND", "Unsupported collaboration change", 400)
    if not isinstance(payload, dict):
        raise ApiError("INVALID_INPUT", "Collaboration payload must be an object", 400)
    business = _business_snapshot(business_snapshot)
    if business.get("operation") != "collaboration_plan":
        raise ApiError("COLLABORATION_PLAN_REQUIRED", "This result has no saved collaboration candidate set", 409)
    if not isinstance(business.get("result"), dict):
        raise ApiError("COLLABORATION_PLAN_REQUIRED", "The saved collaboration candidates are unavailable", 409)
    collaborations = business.setdefault("collaborations", [])
    if not isinstance(collaborations, list):
        raise ApiError("COLLABORATION_STATE_INVALID", "Saved collaboration records are malformed", 409)

    handlers = {
        "adopt_collaboration_candidate": _adopt_candidate,
        "decline_collaboration_candidate": _decline_candidate,
        "record_collaboration_terms": _record_terms,
        "confirm_collaboration_agreement": _confirm_agreement,
        "submit_collaboration_deliverable": _submit_deliverable,
        "record_collaboration_feedback": _record_feedback,
        "request_collaboration_revision": _request_revision,
        "accept_collaboration_deliverable": _accept_deliverable,
        "reject_collaboration_deliverable": _reject_deliverable,
        "cancel_collaboration": _cancel_collaboration,
        "record_collaboration_cost": _record_collaboration_cost,
    }
    handlers[command](
        connection,
        identity,
        artifact_row,
        current_version_row,
        business,
        payload,
        now,
    )


def render_collaboration_lifecycle(business: dict, audience: str = "internal") -> str:
    """Render collaboration history for the owner Artifact or an explicit client view."""
    if audience not in {"internal", "client"}:
        raise ValueError("audience must be internal or client")
    collaborations = business.get("collaborations", []) if isinstance(business, dict) else []
    if not isinstance(collaborations, list):
        return ""
    if audience == "client":
        lines = []
        for event in business.get("collaboration_decisions", []):
            if event.get("client_disclosure") is True and event.get("client_summary"):
                lines.append(
                    f"- {_client_event_label(event.get('kind'))}：{event['client_summary']}"
                )
        for collaboration in collaborations:
            for event in collaboration.get("events", []):
                if event.get("client_disclosure") is True and event.get("client_summary"):
                    lines.append(
                        f"- {_client_event_label(event.get('kind'))}：{event['client_summary']}"
                    )
            for deliverable in collaboration.get("deliverables", []):
                for event in deliverable.get("events", []):
                    if event.get("client_disclosure") is True and event.get("client_summary"):
                        lines.append(
                            f"- {_client_event_label(event.get('kind'))}：{event['client_summary']}"
                        )
        return "\n".join(lines)

    parts = []
    for event in business.get("collaboration_decisions", []):
        details = event.get("details", {})
        parts.append(
            f"## {_event_label(event.get('kind'))}\n\n"
            f"候选 {details.get('candidate_label') or '未命名候选'}；"
            f"原因 {details.get('reason') or '未提供'}；"
            f"记录时间 {event.get('occurred_at') or '未知'}。"
        )
    for collaboration in collaborations:
        candidate = collaboration.get("candidate_snapshot", {})
        parts.extend(
            [
                f"## 合作记录 · {candidate.get('display_label') or '未命名合作候选'}",
                "当前工作；"
                f"合作角色 {candidate.get('task_role') or '待确认'}；"
                f"跨平台身份关系 {_identity_label(candidate.get('identity_link_status'))}；"
                f"当前阶段 {_state_label(collaboration.get('state'))}。",
            ]
        )
        permission = candidate.get("contact_permission") or {}
        if isinstance(permission, dict):
            parts.append(
                f"- 联系许可：{_contactability_label(permission.get('contactability'))}；"
                f"用途 {_display(permission.get('purpose'))}；渠道 {_display(permission.get('channels'))}；"
                f"范围 {_display(permission.get('scope'))}；有效期 {_display(permission.get('valid_until'))}；"
                "此协作记录本身不会联系候选。"
            )
        agreement = collaboration.get("current_agreement")
        terms_proposal = collaboration.get("terms_proposal") or {}
        terms = agreement.get("terms") if agreement else terms_proposal.get("terms")
        if terms:
            if terms.get("scope_change_reason"):
                parts.append(f"- 合同范围变更说明：{terms['scope_change_reason']}。")
            quote = terms.get("quote") or {}
            quote_text = (
                f"{quote['amount']} {quote['currency']}（{_quote_state_label(quote.get('status'))}；"
                "用户记录，不是支付回执）"
                if quote.get("amount") is not None and quote.get("currency")
                else "金额未知"
            )
            source = agreement.get("source") if agreement else terms_proposal.get("agreement_source")
            parts.append(
                f"- {'当前协议金额' if agreement else '待议报价'}：{quote_text}；"
                f"协议来源 {_source_label(source)}。"
            )
            rights = terms.get("rights") or {}
            parts.append(
                f"- 权益：媒体 {_rights_value_summary(rights.get('media'), _RIGHTS_MEDIA_LABELS)}；"
                f"地域 {_rights_value_summary(rights.get('territories'), _RIGHTS_TERRITORY_LABELS)}；"
                f"期限 {_rights_term_summary(rights)}；付费 {_permission_label(rights.get('paid_use'))}；"
                f"改编 {_permission_label(rights.get('editing_allowed'))}；"
                f"衍生 {_display(rights.get('derivative_use'))}；排他 {_display(rights.get('exclusivity'))}；"
                f"撤回 {_display(rights.get('withdrawal_terms'))}。"
            )
            availability = terms.get("availability")
            if availability:
                parts.append(
                    f"- 双方约定档期：{availability.get('from') or '未知'} — "
                    f"{availability.get('to') or '未知'}。"
                )
            cancellation = terms.get("cancellation") or {}
            if cancellation:
                fee = (
                    f"{cancellation['fee_amount']} {cancellation['fee_currency']}"
                    if cancellation.get("fee_amount") is not None
                    else "金额未知"
                )
                parts.append(
                    f"- 取消条件：{cancellation.get('policy') or '未知'}；"
                    f"通知截止 {cancellation.get('notice_deadline') or '未知'}；"
                    f"取消费用 {fee}；退款 {cancellation.get('refund_terms') or '未知'}。"
                )
            for gap in terms_proposal.get("gaps", []):
                parts.append(f"- 条款待补：{gap}。")
        for deliverable in collaboration.get("deliverables", []):
            title = deliverable.get("title") or "未命名交付项"
            proposed_deliverable_terms = next(
                (
                    item
                    for item in terms_proposal.get("terms", {}).get("deliverables", [])
                    if item.get("id") == deliverable["id"]
                ),
                None,
            )
            deliverable_terms = (
                deliverable.get("terms")
                or proposed_deliverable_terms
                or deliverable.get("candidate_terms")
                or {}
            )
            parts.append(
                f"- 交付 {title}：数量 {_display(deliverable_terms.get('quantity'))} "
                f"{deliverable_terms.get('unit') or ''}；规格 {_display(deliverable_terms.get('specification'))}；"
                f"初稿 {_display(deliverable_terms.get('draft_due'))}；终稿 {_display(deliverable_terms.get('final_due'))}；"
                f"验收 {_acceptance_label(deliverable_terms.get('acceptance'))}；"
                f"阶段 {_state_label(deliverable.get('state'))}。"
            )
            for submission in deliverable.get("submissions", []):
                parts.append(
                    f"  - 第 {submission.get('attempt', '?')} 次提交："
                    f"{_source_label(submission.get('source'))}；"
                    f"用户提交时间 {submission.get('submitted_at') or '未知'}；"
                    f"内容指纹 {submission.get('content_sha256') or '未知'}。"
                )
            for event in deliverable.get("events", []):
                detail = event.get("details", {})
                if event.get("kind") in {"feedback_recorded", "revision_requested"}:
                    text = detail.get("feedback") or detail.get("reason") or "未提供说明"
                    parts.append(f"  - {_event_label(event.get('kind'))}：{text}。")
                elif event.get("kind") in {"deliverable_accepted", "deliverable_rejected"}:
                    approval = detail.get("approval") or {}
                    parts.append(
                        f"  - {_event_label(event.get('kind'))}："
                    f"{detail.get('reason') or '已记录该次审核决定'}；"
                    f"依据 {'当前Artifact审批记录' if approval.get('id') else '用户记录'}。"
                    )
        for event in collaboration.get("events", []):
            if event.get("kind") == "collaboration_cancelled":
                detail = event.get("details", {})
                parts.append(
                    f"- {_event_label(event['kind'])}：{detail.get('reason') or '未提供原因'}；"
                    "原协议、已交付物和费用记录仍保留。"
                )
                explicit_source = detail.get("cancellation_source")
                if explicit_source:
                    parts.append(
                        f"  - 本次取消来源：{_source_label(explicit_source)}；"
                        "用户本次选定并在记录时重新核验。"
                    )
                historical = detail.get("historical_agreement_source")
                if historical:
                    status = detail.get("historical_agreement_source_status")
                    status_text = (
                        "仅保留历史引用；取消时未重新读取或核验。"
                        if status == "not_revalidated"
                        else "历史来源状态未知。"
                    )
                    parts.append(
                        f"  - 原协议来源：{_source_label(historical)}；{status_text}"
                    )
        costs = {item.get("id"): item for item in business.get("costs", [])}
        for cost_id in collaboration.get("cost_record_ids", []):
            cost = costs.get(cost_id)
            if cost is None:
                continue
            parts.append(
                f"- 费用记录：{_cost_kind_label(cost.get('kind'))} "
                f"{_display(cost.get('amount'))} {cost.get('currency') or '币种未知'}；"
                f"{cost.get('reason') or '未提供说明'}；{cost.get('provenance') or '用户记录，未独立核验'}。"
            )
    return "\n".join(parts)


def approval_scope_for_agreement(collaboration: dict) -> dict:
    pending = collaboration.get("terms_proposal")
    if not isinstance(pending, dict) or not pending.get("agreement_source"):
        raise ValueError("An exact agreement source is required before approval")
    return {
        "collaboration_id": collaboration["id"],
        "terms": deepcopy(pending["terms"]),
        "agreement_source": deepcopy(pending["agreement_source"]),
    }


def approval_scope_for_acceptance(
    collaboration: dict,
    deliverable: dict,
    submission: dict,
) -> dict:
    if not collaboration.get("current_agreement"):
        raise ValueError("A current agreement is required before acceptance")
    return {
        "collaboration_id": collaboration["id"],
        "agreement_id": collaboration["current_agreement"]["id"],
        "deliverable_id": deliverable["id"],
        "submission_id": submission["id"],
        "submission_source": deepcopy(submission["source"]),
    }


def _business_snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", "Business snapshot must be an object", 400)
    if isinstance(value.get("business"), dict):
        return value["business"]
    return value


def _text(value, name: str, *, required=True, maximum=5000) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{name} must be text", 400)
    result = value.strip()
    if not result:
        if required:
            raise ApiError("INVALID_INPUT", f"{name} is required", 400)
        return None
    if len(result) > maximum:
        raise ApiError("INVALID_INPUT", f"{name} is too long", 400)
    return result


def _mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", f"{name} must be an object", 400)
    return value


def _keys(value: dict, required: set[str], allowed: set[str], name: str) -> None:
    if not required <= value.keys() or value.keys() - allowed:
        raise ApiError("INVALID_INPUT", f"{name} contains missing or unsupported fields", 400)


def _amount(value, name: str, *, required=False) -> str | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ApiError("INVALID_AMOUNT", f"{name} must be a finite nonnegative amount", 400)
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ApiError("INVALID_AMOUNT", f"{name} must be a decimal amount", 400) from error
    if not amount.is_finite() or amount < 0:
        raise ApiError("INVALID_AMOUNT", f"{name} must be a finite nonnegative amount", 400)
    return str(amount)


def _currency(value, name: str, *, required=False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ApiError("INVALID_CURRENCY", f"{name} must be a three-letter ISO currency", 400)
    return value


def _event(kind, identity, now, details, *, client_disclosure=False, client_summary=None, source_refs=None):
    if type(client_disclosure) is not bool:
        raise ApiError("INVALID_INPUT", "client_disclosure must be boolean", 400)
    if client_disclosure:
        client_summary = _text(client_summary, "client_summary", maximum=5000)
    elif client_summary not in (None, ""):
        raise ApiError("INVALID_INPUT", "A client summary requires explicit client disclosure", 400)
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "occurred_at": now,
        "recorded_by": identity["user"]["id"],
        "provenance": "user_recorded_not_independently_verified",
        "client_disclosure": client_disclosure,
        "client_summary": client_summary if client_disclosure else None,
        "source_refs": deepcopy(source_refs or []),
        "details": deepcopy(details),
    }


def _find_collaboration(business, identifier):
    identifier = _text(identifier, "collaboration_id")
    collaboration = next(
        (item for item in business["collaborations"] if item["id"] == identifier),
        None,
    )
    if collaboration is None:
        raise ApiError("COLLABORATION_NOT_FOUND", "The saved collaboration does not exist in this Artifact", 404)
    return collaboration


def _find_deliverable(collaboration, identifier):
    identifier = _text(identifier, "deliverable_id")
    deliverable = next(
        (item for item in collaboration["deliverables"] if item["id"] == identifier),
        None,
    )
    if deliverable is None:
        raise ApiError("COLLABORATION_DELIVERABLE_NOT_FOUND", "The deliverable is not part of this collaboration", 404)
    return deliverable


def _find_submission(deliverable, identifier):
    identifier = _text(identifier, "submission_id")
    submission = next(
        (item for item in deliverable["submissions"] if item["id"] == identifier),
        None,
    )
    if submission is None:
        raise ApiError("COLLABORATION_SUBMISSION_NOT_FOUND", "The selected submission does not belong to this deliverable", 404)
    return submission


def _refresh_collaboration_state(collaboration: dict) -> None:
    """Project the active deliverable states onto the collaboration phase."""
    if collaboration.get("state") == "cancelled" or not collaboration.get("current_agreement"):
        return
    included = [
        row
        for row in collaboration.get("deliverables", [])
        if isinstance(row, dict)
        and isinstance(row.get("terms"), dict)
        and row["terms"].get("scope_status") == "included"
    ]
    if not included:
        collaboration["state"] = "agreement_confirmed_user_recorded"
        return
    states = [row.get("state") for row in included]
    if "submission_recorded_for_review" in states:
        collaboration["state"] = "deliverable_submission_in_review"
    elif "revision_requested" in states:
        collaboration["state"] = "revision_requested"
    elif "feedback_recorded" in states:
        collaboration["state"] = "feedback_recorded"
    elif all(state == "accepted_by_exact_approval" for state in states):
        collaboration["state"] = "deliverables_accepted_by_exact_approval"
    elif "rejected_user_recorded" in states:
        if any(state in {"accepted_by_exact_approval", "awaiting_submission"} for state in states):
            collaboration["state"] = "deliverable_review_partially_complete"
        else:
            collaboration["state"] = "deliverable_rejected_user_recorded"
    elif "awaiting_submission" in states:
        if "accepted_by_exact_approval" in states:
            collaboration["state"] = "deliverable_review_partially_complete"
        else:
            collaboration["state"] = "awaiting_submission"
    else:
        collaboration["state"] = "agreement_confirmed_user_recorded"


def _is_current_agreement_submission(collaboration, submission):
    agreement = collaboration.get("current_agreement")
    if not isinstance(agreement, dict):
        return False
    submission_agreement_id = submission.get("agreement_id")
    if not submission_agreement_id:
        # Existing lifecycle snapshots predate explicit agreement linkage. A
        # single saved agreement is unambiguous; multiple agreements are not.
        agreements = collaboration.get("agreements", [])
        if len(agreements) == 1 and isinstance(agreements[0], dict):
            submission_agreement_id = agreements[0].get("id")
    return submission_agreement_id == agreement.get("id")


def _recheck_submission_source(connection, identity, work_id, submission):
    current = _resolve_source(
        connection,
        identity,
        work_id,
        submission["source"],
        "submission_source",
    )
    if current != submission["source"]:
        raise ApiError("COLLABORATION_SUBMISSION_SOURCE_CHANGED", "The exact submitted Artifact or Material changed; review its current version", 409)
    return current


def _candidate_index(business):
    sections = business["result"].get("sections", [])
    rows = [
        collaborator
        for section in sections
        if section.get("id") == "collaboration_scope"
        for collaborator in section.get("data", {}).get("collaborators", [])
    ]
    return {row.get("id"): row for row in rows if isinstance(row, dict)}


def _parse_business_date(value, name: str):
    text = _text(value, name, required=False, maximum=80)
    if text is None:
        return None
    try:
        if _DATE.fullmatch(text):
            date.fromisoformat(text)
            return text
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_COLLABORATION_DATE", f"{name} must be an ISO date or time", 400) from error
    if parsed.tzinfo is None:
        raise ApiError("COLLABORATION_TIMEZONE_REQUIRED", f"{name} needs an explicit timezone", 400)
    return text


def _timestamp(value, name: str):
    text = _text(value, name, maximum=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_INPUT", f"{name} must be an ISO timestamp", 400) from error
    if parsed.tzinfo is None:
        raise ApiError("INVALID_INPUT", f"{name} needs an explicit timezone", 400)
    return text


def _ordered_datetime(value: str, name: str) -> datetime:
    if _DATE.fullmatch(value):
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_COLLABORATION_DATE", f"{name} must be an ISO date or time", 400) from error
    if parsed.tzinfo is None:
        raise ApiError("COLLABORATION_TIMEZONE_REQUIRED", f"{name} needs an explicit timezone", 400)
    return parsed.astimezone(timezone.utc)


def _normalize_terms(collaboration, value):
    raw = _mapping(value, "terms")
    allowed = {"quote", "rights", "deliverables", "availability", "cancellation", "scope_change_reason", "source_refs"}
    _keys(raw, set(), allowed, "terms")
    scope_change_reason = _text(
        raw.get("scope_change_reason"),
        "terms.scope_change_reason",
        required=False,
    )
    result = {"quote": None, "rights": {}, "deliverables": [], "availability": None, "source_refs": []}
    gaps = []
    quote = raw.get("quote")
    if quote is not None:
        quote = _mapping(quote, "terms.quote")
        _keys(
            quote,
            set(),
            {"amount", "currency", "status", "valid_until", "source_refs"},
            "terms.quote",
        )
        amount = _amount(quote.get("amount"), "terms.quote.amount")
        currency = _currency(quote.get("currency"), "terms.quote.currency", required=amount is not None)
        status = quote.get("status")
        if status is not None and status not in {"quote", "estimate", "committed"}:
            raise ApiError("INVALID_COLLABORATION_QUOTE", "Quote status must be quote, estimate or committed", 400)
        result["quote"] = {
            "amount": amount,
            "currency": currency,
            "status": status,
            "valid_until": _parse_business_date(quote.get("valid_until"), "terms.quote.valid_until"),
            "source_refs": deepcopy(quote.get("source_refs", [])),
        }
    if result["quote"] is None or result["quote"].get("amount") is None:
        gaps.append("合作金额或报价未知")
    if result["quote"] is not None and result["quote"].get("status") is None:
        gaps.append("报价/估计/已约定状态未确认")
    elif result["quote"] is not None and result["quote"].get("status") in {"quote", "estimate"}:
        gaps.append("报价或估算尚未明确为本次协议金额")

    rights = raw.get("rights", {})
    rights = _mapping(rights, "terms.rights")
    rights_allowed = {
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
        "source_refs",
    }
    _keys(rights, set(), rights_allowed, "terms.rights")
    normalized_rights = {}
    for field in ("media", "territories"):
        rows = rights.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
            raise ApiError("INVALID_COLLABORATION_RIGHTS", f"terms.rights.{field} must be a list of text", 400)
        normalized_rights[field] = [item.strip() for item in rows if item.strip()]
    for field in ("paid_use", "editing_allowed"):
        item = rights.get(field)
        if item is not None and type(item) is not bool:
            raise ApiError("INVALID_COLLABORATION_RIGHTS", f"terms.rights.{field} must be true, false or unknown", 400)
        normalized_rights[field] = item
    if rights.get("term_days") is not None:
        term_days = _amount(rights["term_days"], "terms.rights.term_days", required=True)
        if Decimal(term_days) <= 0:
            raise ApiError("INVALID_COLLABORATION_RIGHTS", "terms.rights.term_days must be positive", 400)
        normalized_rights["term_days"] = term_days
    else:
        normalized_rights["term_days"] = None
    normalized_rights["term_start"] = _parse_business_date(rights.get("term_start"), "terms.rights.term_start")
    normalized_rights["term_end"] = _parse_business_date(rights.get("term_end"), "terms.rights.term_end")
    for field in ("derivative_use", "exclusivity", "withdrawal_terms"):
        normalized_rights[field] = _text(rights.get(field), f"terms.rights.{field}", required=False)
    normalized_rights["source_refs"] = deepcopy(rights.get("source_refs", []))
    for field, label in (
        ("media", "可使用媒体"),
        ("territories", "地域范围"),
        ("paid_use", "付费使用权"),
        ("editing_allowed", "剪辑/改编权"),
        ("derivative_use", "衍生使用权"),
        ("exclusivity", "排他范围"),
        ("withdrawal_terms", "撤回条件"),
    ):
        if normalized_rights[field] in (None, "", []):
            gaps.append(f"{label}未知")
    if normalized_rights["term_days"] is None and not (
        normalized_rights["term_start"] and normalized_rights["term_end"]
    ):
        gaps.append("使用期限未知")
    elif normalized_rights["term_start"] and normalized_rights["term_end"]:
        if _ordered_datetime(normalized_rights["term_end"], "terms.rights.term_end") < _ordered_datetime(normalized_rights["term_start"], "terms.rights.term_start"):
            raise ApiError("INVALID_COLLABORATION_RIGHTS", "terms.rights.term_end must not precede term_start", 400)
    result["rights"] = normalized_rights

    base_deliverables = {item["id"]: item for item in collaboration["deliverables"]}
    previous_terms = (collaboration.get("terms_proposal") or {}).get("terms") or {}
    previous_terms_ids = {
        item["id"]
        for item in previous_terms.get("deliverables", [])
        if isinstance(item, dict) and item.get("id")
    }
    deliverables = raw.get("deliverables", [])
    if not isinstance(deliverables, list):
        raise ApiError("INVALID_COLLABORATION_DELIVERABLES", "terms.deliverables must be an array", 400)
    seen = set()
    for index, item_value in enumerate(deliverables):
        item = _mapping(item_value, f"terms.deliverables[{index}]")
        allowed_deliverable_fields = {
            "id",
            "scope_status",
            "title",
            "quantity",
            "unit",
            "specification",
            "draft_due",
            "final_due",
            "acceptance",
            "source_refs",
        }
        _keys(item, set(), allowed_deliverable_fields, f"terms.deliverables[{index}]")
        raw_identity = item.get("id")
        if raw_identity is None:
            if not scope_change_reason:
                raise ApiError("COLLABORATION_SCOPE_CHANGE_REASON_REQUIRED", "A new contract deliverable needs an explicit scope-change reason", 400)
            identity = f"collaboration-deliverable-{uuid.uuid4()}"
        else:
            identity = _text(raw_identity, f"terms.deliverables[{index}].id")
        if identity not in base_deliverables and identity not in previous_terms_ids and raw_identity is not None:
            raise ApiError("COLLABORATION_DELIVERABLE_NOT_FOUND", "A contract term names a deliverable outside the adopted candidate", 409)
        if identity in seen:
            raise ApiError("COLLABORATION_DELIVERABLE_DUPLICATE", "A contract contains duplicate deliverable identities", 400)
        seen.add(identity)
        scope_status = item.get("scope_status", "unknown")
        if scope_status not in {"included", "excluded", "unknown"}:
            raise ApiError("INVALID_COLLABORATION_DELIVERABLES", "scope_status must be included, excluded or unknown", 400)
        quantity = _amount(item.get("quantity"), f"terms.deliverables[{index}].quantity")
        unit = _text(item.get("unit"), f"terms.deliverables[{index}].unit", required=False)
        specification = _text(item.get("specification"), f"terms.deliverables[{index}].specification", required=False)
        draft_due = _parse_business_date(item.get("draft_due"), f"terms.deliverables[{index}].draft_due")
        final_due = _parse_business_date(item.get("final_due"), f"terms.deliverables[{index}].final_due")
        if draft_due and final_due and _ordered_datetime(final_due, f"terms.deliverables[{index}].final_due") < _ordered_datetime(draft_due, f"terms.deliverables[{index}].draft_due"):
            raise ApiError("INVALID_COLLABORATION_DATE", "A final delivery deadline cannot precede the draft deadline", 400)
        acceptance_value = item.get("acceptance") or {}
        acceptance_value = _mapping(acceptance_value, f"terms.deliverables[{index}].acceptance")
        _keys(acceptance_value, set(), {"reviewer", "criteria"}, f"terms.deliverables[{index}].acceptance")
        reviewer = _text(acceptance_value.get("reviewer"), f"terms.deliverables[{index}].acceptance.reviewer", required=False)
        criteria = acceptance_value.get("criteria", [])
        if not isinstance(criteria, list) or any(not isinstance(value, str) for value in criteria):
            raise ApiError("INVALID_COLLABORATION_DELIVERABLES", "Acceptance criteria must be a list of text", 400)
        base_item = base_deliverables.get(identity, {})
        normalized = {
            "id": identity,
            "scope_status": scope_status,
            "title": _text(item.get("title"), f"terms.deliverables[{index}].title", required=False)
            or base_item.get("title")
            or "新增交付项",
            "quantity": quantity,
            "unit": unit,
            "specification": specification,
            "draft_due": draft_due,
            "final_due": final_due,
            "acceptance": {"reviewer": reviewer, "criteria": [value.strip() for value in criteria if value.strip()]},
            "source_refs": deepcopy(item.get("source_refs", [])),
        }
        result["deliverables"].append(normalized)
        if scope_status == "unknown":
            gaps.append(f"交付项 {identity} 是否纳入合同未知")
        elif scope_status == "included":
            if quantity is None or not unit or not specification or not draft_due or not final_due or not reviewer or not normalized["acceptance"]["criteria"]:
                gaps.append(f"交付项 {identity} 的数量、规格、时限或验收责任待补")
    missing_scope = set(base_deliverables) - seen
    if missing_scope:
        gaps.extend(f"交付项 {identity} 是否纳入合同未知" for identity in sorted(missing_scope))
    if any(identity not in base_deliverables for identity in seen) and not scope_change_reason:
        gaps.append("新增交付范围缺少变更理由")

    availability = raw.get("availability")
    if availability is not None:
        availability = _mapping(availability, "terms.availability")
        _keys(availability, set(), {"from", "to", "source_refs"}, "terms.availability")
        start = _parse_business_date(availability.get("from"), "terms.availability.from")
        end = _parse_business_date(availability.get("to"), "terms.availability.to")
        if start and end and _ordered_datetime(end, "terms.availability.to") < _ordered_datetime(start, "terms.availability.from"):
            raise ApiError("INVALID_COLLABORATION_DATE", "terms.availability.to must not precede from", 400)
        result["availability"] = {
            "from": start,
            "to": end,
            "source_refs": deepcopy(availability.get("source_refs", [])),
        }
        if not start or not end:
            gaps.append("双方约定档期范围不完整")
    else:
        result["availability"] = None
        gaps.append("双方约定档期未知")
    cancellation = raw.get("cancellation")
    if cancellation is not None:
        cancellation = _mapping(cancellation, "terms.cancellation")
        _keys(
            cancellation,
            {"policy", "refund_terms"},
            {"policy", "notice_deadline", "fee_amount", "fee_currency", "refund_terms", "source_refs"},
            "terms.cancellation",
        )
        policy = _text(cancellation.get("policy"), "terms.cancellation.policy")
        refund_terms = _text(cancellation.get("refund_terms"), "terms.cancellation.refund_terms")
        fee_amount = _amount(cancellation.get("fee_amount"), "terms.cancellation.fee_amount")
        fee_currency = _currency(
            cancellation.get("fee_currency"),
            "terms.cancellation.fee_currency",
            required=fee_amount is not None,
        )
        result["cancellation"] = {
            "policy": policy,
            "notice_deadline": _parse_business_date(
                cancellation.get("notice_deadline"),
                "terms.cancellation.notice_deadline",
            ),
            "fee_amount": fee_amount,
            "fee_currency": fee_currency,
            "refund_terms": refund_terms,
            "source_refs": deepcopy(cancellation.get("source_refs", [])),
        }
    else:
        result["cancellation"] = None
        gaps.append("取消通知期限、费用和退款条件未知")
    result["source_refs"] = deepcopy(raw.get("source_refs", []))
    result["scope_change_reason"] = scope_change_reason
    return result, gaps


def _collaboration(business, identifier):
    rows = business.setdefault("collaborations", [])
    if not isinstance(rows, list):
        raise ApiError("COLLABORATION_STATE_INVALID", "Saved collaboration records are malformed", 409)
    return _find_collaboration(business, identifier)


def _resolve_source(connection, identity, work_id, value, name):
    source = _mapping(value, name)
    kind = source.get("kind")
    if kind == "artifact":
        _keys(
            source,
            {"kind", "artifact_id", "artifact_version"},
            {"kind", "artifact_id", "artifact_version", "title", "content_sha256"},
            name,
        )
        artifact_id = _text(source.get("artifact_id"), f"{name}.artifact_id")
        version = source.get("artifact_version")
        if type(version) is not int or version < 1:
            raise ApiError("INVALID_ARTIFACT_REFERENCE", f"{name}.artifact_version must be a positive integer", 400)
        source_artifact = require_artifact_access(connection, identity, artifact_id)
        if source_artifact["work_id"] != work_id:
            raise ApiError("COLLABORATION_SOURCE_WORK_MISMATCH", "A collaboration submission must belong to the same Work", 403)
        saved = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
            (artifact_id, identity["workspace"]["id"], version),
        ).fetchone()
        if saved is None:
            raise _not_found()
        digest = hashlib.sha256(
            "\0".join(
                (
                    saved["title"],
                    saved["body"],
                    saved["citations_json"],
                    saved["input_snapshot_json"],
                )
            ).encode("utf-8")
        ).hexdigest()
        return {
            "kind": "artifact",
            "artifact_id": artifact_id,
            "artifact_version": version,
            "title": saved["title"],
            "content_sha256": digest,
        }
    if kind == "material":
        _keys(
            source,
            {"kind", "material_id", "material_sha256"},
            {"kind", "material_id", "material_sha256", "title", "filename", "format", "status", "reason"},
            name,
        )
        material_id = _text(source.get("material_id"), f"{name}.material_id")
        material = require_material_access(
            connection,
            identity,
            material_id,
            target_work_id=work_id,
        )
        linked = material["work_id"] == work_id or connection.execute(
            "SELECT 1 FROM work_materials WHERE work_id=? AND material_id=? AND workspace_id=?",
            (work_id, material_id, identity["workspace"]["id"]),
        ).fetchone()
        if not linked:
            raise ApiError("COLLABORATION_SOURCE_WORK_MISMATCH", "A collaboration submission must be linked to the same Work", 403)
        if source.get("material_sha256") != material["sha256"]:
            raise ApiError("COLLABORATION_SOURCE_CHANGED", "The selected Material hash is not its current saved original", 409)
        if material["status"] not in {"ready", "partial", "unavailable"}:
            raise ApiError("COLLABORATION_SOURCE_NOT_READY", "The saved Material is not ready for delivery review", 409)
        return {
            "kind": "material",
            "material_id": material_id,
            "material_sha256": material["sha256"],
            "title": material["title"],
            "filename": material["filename"],
            "format": material["format"],
            "status": material["status"],
            "reason": material["reason"],
        }
    raise ApiError("INVALID_COLLABORATION_SOURCE", f"{name}.kind must be artifact or material", 400)


def _append_event(target, kind, identity, now, details, payload, source_refs=None):
    event = _event(
        kind,
        identity,
        now,
        details,
        client_disclosure=payload.get("client_disclosure", False),
        client_summary=payload.get("client_summary"),
        source_refs=source_refs or payload.get("source_refs", []),
    )
    target.setdefault("events", []).append(event)
    return event


def _submit_deliverable(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "deliverable_id", "source"},
        {"collaboration_id", "deliverable_id", "source", "note", "client_disclosure", "client_summary", "source_refs"},
        "submit_collaboration_deliverable",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    if not collaboration.get("current_agreement"):
        raise ApiError("COLLABORATION_AGREEMENT_REQUIRED", "Record and approve the exact agreement before a contractual deliverable submission", 409)
    deliverable = _find_deliverable(collaboration, payload["deliverable_id"])
    if not isinstance(deliverable.get("terms"), dict) or deliverable["terms"].get("scope_status") != "included":
        raise ApiError("COLLABORATION_DELIVERABLE_OUT_OF_SCOPE", "This deliverable is not included in the current approved agreement", 409)
    source = _resolve_source(
        connection,
        identity,
        artifact["work_id"],
        payload["source"],
        "source",
    )
    previous = deliverable.get("current_submission_id")
    if previous:
        prior_submission = _find_submission(deliverable, previous)
        if (
            (prior_submission.get("review_decision") or {}).get("state")
            == "accepted_by_exact_approval"
            and _is_current_agreement_submission(collaboration, prior_submission)
        ):
            raise ApiError(
                "COLLABORATION_SUBMISSION_ALREADY_ACCEPTED",
                "This deliverable was accepted under the current agreement; record an approved amendment before submitting another version",
                409,
            )
    submission_id = str(uuid.uuid4())
    submission = {
        "id": submission_id,
        "deliverable_id": deliverable["id"],
        "agreement_id": collaboration["current_agreement"]["id"],
        "attempt": len(deliverable["submissions"]) + 1,
        "supersedes_submission_id": previous,
        "source": source,
        "content_sha256": source.get("content_sha256") or source.get("material_sha256"),
        "note": _text(payload.get("note"), "note", required=False),
        "submitted_by": identity["user"]["id"],
        "submitted_at": now,
        "provenance": "user_selected_exact_saved_deliverable_not_external_receipt",
        "review_decision": None,
    }
    deliverable["submissions"].append(submission)
    deliverable["current_submission_id"] = submission_id
    deliverable["state"] = "submission_recorded_for_review"
    _append_event(
        deliverable,
        "deliverable_submitted",
        identity,
        now,
        {
            "submission_id": submission_id,
            "deliverable_id": deliverable["id"],
            "source": deepcopy(source),
            "content_sha256": submission["content_sha256"],
            "note": submission["note"],
            "supersedes_submission_id": previous,
            "provenance": submission["provenance"],
        },
        payload,
    )
    _refresh_collaboration_state(collaboration)


def _comment_snapshot(connection, identity, submission, comment_id):
    source = submission["source"]
    if source["kind"] != "artifact":
        raise ApiError("COLLABORATION_COMMENT_SOURCE_INVALID", "Artifact comments can only be linked to an Artifact submission", 400)
    artifact_id, artifact_version = source["artifact_id"], source["artifact_version"]
    require_artifact_access(connection, identity, artifact_id)
    row = connection.execute(
        "SELECT * FROM artifact_comments WHERE id=? AND artifact_id=? AND workspace_id=?",
        (comment_id, artifact_id, identity["workspace"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    if row["artifact_version"] != artifact_version:
        raise ApiError("COLLABORATION_COMMENT_VERSION_MISMATCH", "The comment is not anchored to this exact submitted Artifact version", 409)
    if row["state"] == "withdrawn":
        raise ApiError("COLLABORATION_COMMENT_WITHDRAWN", "A withdrawn comment cannot be used as deliverable feedback", 409)
    return {
        "id": row["id"],
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "comment_version": row["version"],
        "anchor": json.loads(row["anchor_json"]),
        "body": row["body"],
        "state": row["state"],
        "author_id": row["author_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None,
    }


def _record_feedback(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "deliverable_id", "submission_id"},
        {"collaboration_id", "deliverable_id", "submission_id", "comment_id", "feedback", "client_disclosure", "client_summary", "source_refs"},
        "record_collaboration_feedback",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    deliverable = _find_deliverable(collaboration, payload["deliverable_id"])
    submission = _find_submission(deliverable, payload["submission_id"])
    _recheck_submission_source(connection, identity, artifact["work_id"], submission)
    comment = None
    if payload.get("comment_id"):
        comment_id = _text(payload["comment_id"], "comment_id", maximum=200)
        comment = _comment_snapshot(connection, identity, submission, comment_id)
    feedback = _text(payload.get("feedback"), "feedback", required=comment is None)
    if comment is None and not feedback:
        raise ApiError("COLLABORATION_FEEDBACK_REQUIRED", "Select an exact Artifact comment or enter feedback for this saved submission", 400)
    details = {
        "submission_id": submission["id"],
        "comment": comment,
        "feedback": feedback,
        "provenance": "linked_exact_artifact_comment" if comment else "user_recorded_feedback_for_exact_submission",
    }
    event = _append_event(
        deliverable,
        "feedback_recorded",
        identity,
        now,
        details,
        payload,
    )
    deliverable.setdefault("feedback", []).append(
        {
            "id": event["id"],
            "submission_id": submission["id"],
            "comment": deepcopy(comment),
            "feedback": feedback,
            "recorded_by": identity["user"]["id"],
            "recorded_at": now,
            "provenance": details["provenance"],
        }
    )
    if (submission.get("review_decision") or {}).get("state") != "accepted_by_exact_approval":
        deliverable["state"] = "feedback_recorded"
    _refresh_collaboration_state(collaboration)


def _request_revision(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "deliverable_id", "submission_id", "feedback_id", "reason"},
        {"collaboration_id", "deliverable_id", "submission_id", "feedback_id", "reason", "client_disclosure", "client_summary", "source_refs"},
        "request_collaboration_revision",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    deliverable = _find_deliverable(collaboration, payload["deliverable_id"])
    submission = _find_submission(deliverable, payload["submission_id"])
    _recheck_submission_source(connection, identity, artifact["work_id"], submission)
    if not _is_current_agreement_submission(collaboration, submission):
        raise ApiError(
            "COLLABORATION_SUBMISSION_AGREEMENT_CHANGED",
            "This saved submission is linked to an older or ambiguous agreement; submit an exact version under the current agreement before review",
            409,
        )
    if (submission.get("review_decision") or {}).get("state") == "accepted_by_exact_approval":
        raise ApiError(
            "COLLABORATION_ALREADY_ACCEPTED",
            "An accepted submission cannot be changed to a revision request; record an approved amendment and new submission",
            409,
        )
    feedback_id = _text(payload["feedback_id"], "feedback_id", maximum=200)
    feedback = next(
        (
            item
            for item in deliverable.get("feedback", [])
            if item["id"] == feedback_id and item["submission_id"] == submission["id"]
        ),
        None,
    )
    if feedback is None:
        raise ApiError("COLLABORATION_FEEDBACK_NOT_FOUND", "Revision requests must link feedback on this exact submission", 409)
    reason = _text(payload["reason"], "reason")
    submission["review_decision"] = {
        "state": "revision_requested_user_recorded",
        "feedback_id": feedback_id,
        "reason": reason,
        "recorded_by": identity["user"]["id"],
        "recorded_at": now,
    }
    deliverable["state"] = "revision_requested"
    _append_event(
        deliverable,
        "revision_requested",
        identity,
        now,
        {"submission_id": submission["id"], "feedback_id": feedback_id, "reason": reason},
        payload,
    )
    _refresh_collaboration_state(collaboration)


def _accept_deliverable(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "deliverable_id", "submission_id", "approval_id"},
        {"collaboration_id", "deliverable_id", "submission_id", "approval_id", "client_disclosure", "client_summary", "source_refs"},
        "accept_collaboration_deliverable",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    deliverable = _find_deliverable(collaboration, payload["deliverable_id"])
    submission = _find_submission(deliverable, payload["submission_id"])
    _recheck_submission_source(connection, identity, artifact["work_id"], submission)
    if not _is_current_agreement_submission(collaboration, submission):
        raise ApiError(
            "COLLABORATION_SUBMISSION_AGREEMENT_CHANGED",
            "This saved submission is linked to an older or ambiguous agreement; submit an exact version under the current agreement before acceptance",
            409,
        )
    if deliverable.get("current_submission_id") != submission["id"]:
        raise ApiError("COLLABORATION_SUBMISSION_SUPERSEDED", "Accept the current submitted version, not a superseded attempt", 409)
    if (submission.get("review_decision") or {}).get("state") == "accepted_by_exact_approval":
        raise ApiError("COLLABORATION_ALREADY_ACCEPTED", "This exact deliverable submission was already accepted", 409)
    scope = approval_scope_for_acceptance(collaboration, deliverable, submission)
    approval = require_artifact_approval(
        connection,
        identity,
        _text(payload["approval_id"], "approval_id", maximum=200),
        artifact["id"],
        version["version"],
        "creator_deliverable_acceptance",
        scope,
    )
    approval_summary = {
        "id": approval["id"],
        "artifact_id": artifact["id"],
        "artifact_version": version["version"],
        "purpose": approval["purpose"],
        "scope": scope,
        "approved_by": approval["approved_by"],
        "created_at": approval["created_at"],
    }
    submission["review_decision"] = {
        "state": "accepted_by_exact_approval",
        "approval": approval_summary,
        "recorded_by": identity["user"]["id"],
        "recorded_at": now,
        "provenance": "current_artifact_approval_for_exact_submission",
    }
    deliverable["state"] = "accepted_by_exact_approval"
    business["decisions"].append(
        {
            "id": str(uuid.uuid4()),
            "choice": "接收指定协作交付版本",
            "reason": f"由精确Artifact审批 {approval['id']} 覆盖提交 {submission['id']}",
            "alternatives": ["返修", "拒收"],
            "counterevidence": [],
            "constraints": scope,
            "section_ids": ["collaboration_terms", "collaboration_observations"],
            "recorded_by": identity["user"]["id"],
            "recorded_at": now,
            "provenance": "artifact_approval_recorded; not external payment or publication",
            "approval": approval_summary,
        }
    )
    _append_event(
        deliverable,
        "deliverable_accepted",
        identity,
        now,
        {"submission_id": submission["id"], "approval": approval_summary},
        payload,
    )
    _refresh_collaboration_state(collaboration)


def _reject_deliverable(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "deliverable_id", "submission_id", "reason"},
        {"collaboration_id", "deliverable_id", "submission_id", "reason", "client_disclosure", "client_summary", "source_refs"},
        "reject_collaboration_deliverable",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    deliverable = _find_deliverable(collaboration, payload["deliverable_id"])
    submission = _find_submission(deliverable, payload["submission_id"])
    _recheck_submission_source(connection, identity, artifact["work_id"], submission)
    if (submission.get("review_decision") or {}).get("state") == "accepted_by_exact_approval":
        raise ApiError(
            "COLLABORATION_ALREADY_ACCEPTED",
            "An accepted submission cannot be changed to a rejection; record an approved amendment and new submission",
            409,
        )
    if not _is_current_agreement_submission(collaboration, submission):
        raise ApiError(
            "COLLABORATION_SUBMISSION_AGREEMENT_CHANGED",
            "This saved submission is linked to an older or ambiguous agreement; submit an exact version under the current agreement before review",
            409,
        )
    if deliverable.get("current_submission_id") != submission["id"]:
        raise ApiError("COLLABORATION_SUBMISSION_SUPERSEDED", "Reject the current submitted version, not a superseded attempt", 409)
    reason = _text(payload["reason"], "reason")
    submission["review_decision"] = {
        "state": "rejected_user_recorded_not_client_receipt",
        "reason": reason,
        "recorded_by": identity["user"]["id"],
        "recorded_at": now,
        "provenance": "user_recorded_decision_not_external_client_confirmation",
    }
    deliverable["state"] = "rejected_user_recorded"
    business["decisions"].append(
        {
            "id": str(uuid.uuid4()),
            "choice": "未接收指定协作交付版本",
            "reason": reason,
            "alternatives": ["返修", "另行提交"],
            "counterevidence": [],
            "constraints": {
                "collaboration_id": collaboration["id"],
                "deliverable_id": deliverable["id"],
                "submission_id": submission["id"],
            },
            "section_ids": ["collaboration_terms", "collaboration_observations"],
            "recorded_by": identity["user"]["id"],
            "recorded_at": now,
            "provenance": "user_recorded_decision_not_client_receipt",
            "approval": "not_granted_by_decision",
        }
    )
    _append_event(
        deliverable,
        "deliverable_rejected_user_recorded",
        identity,
        now,
        {"submission_id": submission["id"], "reason": reason},
        payload,
    )
    _refresh_collaboration_state(collaboration)


def _cost_payload_for_collaboration(collaboration, cost_value):
    cost = _mapping(cost_value, "cost")
    allowed = {
        "id",
        "kind",
        "amount",
        "currency",
        "refund_of",
        "supersedes",
        "reason",
        "occurred_at",
        "source_refs",
    }
    _keys(cost, {"kind", "currency", "reason"}, allowed, "cost")
    kind = cost["kind"]
    if kind not in {"estimate", "committed", "actual", "refund"}:
        raise ApiError("INVALID_INPUT", "Unsupported collaboration cost kind", 400)
    if kind == "committed" and not collaboration.get("current_agreement"):
        raise ApiError("COLLABORATION_AGREEMENT_REQUIRED", "A commitment requires an approved agreement", 409)
    record_id = _text(cost.get("id") or f"collaboration:{collaboration['id']}:cost:{uuid.uuid4()}", "cost.id", maximum=150)
    return {
        "id": record_id,
        "kind": kind,
        "amount": cost.get("amount"),
        "currency": _currency(cost.get("currency"), "cost.currency", required=True),
        "reason": _text(cost.get("reason"), "cost.reason"),
        **({"refund_of": _text(cost.get("refund_of"), "cost.refund_of", maximum=200)} if cost.get("refund_of") else {}),
        **({"supersedes": _text(cost.get("supersedes"), "cost.supersedes", maximum=200)} if cost.get("supersedes") else {}),
        **({"occurred_at": _timestamp(cost.get("occurred_at"), "cost.occurred_at")} if cost.get("occurred_at") else {}),
        "source_refs": deepcopy(cost.get("source_refs", [])),
    }


def _record_collaboration_cost(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "cost"},
        {"collaboration_id", "cost", "client_disclosure", "client_summary"},
        "record_collaboration_cost",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    cost = _cost_payload_for_collaboration(
        collaboration,
        payload["cost"],
    )
    linked = set(collaboration.get("cost_record_ids", []))
    if cost.get("refund_of") and cost["refund_of"] not in linked:
        raise ApiError("COLLABORATION_REFUND_SCOPE_MISMATCH", "Refunds must reference an actual cost of this collaboration", 409)
    if cost.get("supersedes") and cost["supersedes"] not in linked:
        raise ApiError("COLLABORATION_COST_SCOPE_MISMATCH", "Cost corrections must reference this collaboration's cost record", 409)
    from .business_execution import _record_cost

    _record_cost(business, cost, identity["user"]["id"], now)
    collaboration.setdefault("cost_record_ids", []).append(cost["id"])
    _append_event(
        collaboration,
        "collaboration_cost_recorded",
        identity,
        now,
        {
            "cost_record_id": cost["id"],
            "kind": cost["kind"],
            "amount": cost["amount"],
            "currency": cost["currency"],
            "reason": cost["reason"],
            "provenance": "user_recorded_not_payment_verified",
        },
        payload,
        source_refs=cost.get("source_refs", []),
    )


def _cancel_collaboration(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "reason"},
        {"collaboration_id", "reason", "cancellation_source", "remaining_commitment", "client_disclosure", "client_summary", "source_refs"},
        "cancel_collaboration",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    if collaboration["state"] == "cancelled":
        raise ApiError("COLLABORATION_ALREADY_CANCELLED", "This collaboration is already cancelled", 409)
    reason = _text(payload["reason"], "reason")
    selected_cancellation_source = payload.get("cancellation_source")
    revalidated_cancellation_source = None
    if selected_cancellation_source is not None:
        revalidated_cancellation_source = _resolve_source(
            connection,
            identity,
            artifact["work_id"],
            selected_cancellation_source,
            "cancellation_source",
        )
    agreement = collaboration.get("current_agreement")
    historical_agreement_source = (
        deepcopy(agreement.get("source")) if isinstance(agreement, dict) else None
    )
    released_record_id = None
    release_value = payload.get("remaining_commitment")
    if release_value is not None:
        release = _mapping(release_value, "remaining_commitment")
        _keys(release, {"cost_record_id", "amount", "reason"}, {"cost_record_id", "amount", "reason", "source_refs"}, "remaining_commitment")
        old_id = _text(release["cost_record_id"], "remaining_commitment.cost_record_id", maximum=200)
        if old_id not in set(collaboration.get("cost_record_ids", [])):
            raise ApiError("COLLABORATION_COST_SCOPE_MISMATCH", "Release must refer to this collaboration's recorded commitment", 409)
        old_cost = next((item for item in business["costs"] if item["id"] == old_id), None)
        if old_cost is None or old_cost["kind"] != "committed":
            raise ApiError("COLLABORATION_COMMITMENT_NOT_FOUND", "The selected commitment is not active in this collaboration ledger", 409)
        if old_cost["amount"] is None:
            raise ApiError("COLLABORATION_COMMITMENT_BASIS_UNKNOWN", "The existing committed amount is unknown; correct it before recording a remaining balance", 409)
        remaining_amount = _amount(
            release["amount"],
            "remaining_commitment.amount",
            required=True,
        )
        if Decimal(remaining_amount) > Decimal(str(old_cost["amount"])):
            raise ApiError("COLLABORATION_COMMITMENT_INCREASED", "A cancellation release cannot increase the remaining commitment", 409)
        released_record_id = f"collaboration:{collaboration['id']}:release:{uuid.uuid4()}"
        from .business_execution import _record_cost

        _record_cost(
            business,
            {
                "id": released_record_id,
                "kind": "committed",
                "amount": remaining_amount,
                "currency": old_cost["currency"],
            "reason": _text(release["reason"], "remaining_commitment.reason"),
                "occurred_at": now,
                "supersedes": old_id,
                "source_refs": deepcopy(release.get("source_refs", [])),
            },
            identity["user"]["id"],
            now,
        )
        collaboration["cost_record_ids"].append(released_record_id)
    collaboration["state"] = "cancelled"
    _append_event(
        collaboration,
        "collaboration_cancelled",
        identity,
        now,
        {
            "reason": reason,
            "cancellation_source": revalidated_cancellation_source,
            "cancellation_source_status": (
                "revalidated_at_cancellation"
                if revalidated_cancellation_source is not None
                else "not_provided"
            ),
            "historical_agreement_source": historical_agreement_source,
            "historical_agreement_source_status": (
                "not_revalidated" if historical_agreement_source else "not_available"
            ),
            "contract_cancellation_terms": deepcopy(
                (collaboration.get("current_agreement") or {}).get("terms", {}).get("cancellation")
            ),
            "remaining_commitment_record_id": released_record_id,
            "remaining_commitment_semantics": "new_remaining_unperformed_commitment_balance",
            "external_cancellation": "not_performed_by_evaluator_or_record_command",
            "cost_consequences": "only_explicit_linked_cost_records_are_counted",
        },
        payload,
        source_refs=payload.get("source_refs", []),
    )


def _record_terms(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "terms"},
        {"collaboration_id", "terms", "agreement_source", "client_disclosure", "client_summary", "source_refs"},
        "record_collaboration_terms",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    if collaboration["state"] == "cancelled":
        raise ApiError("COLLABORATION_CANCELLED", "Cancelled collaboration terms cannot be changed", 409)
    terms, gaps = _normalize_terms(collaboration, payload["terms"])
    existing_deliverable_ids = {
        item["id"] for item in collaboration["deliverables"]
    }
    for deliverable_terms in terms["deliverables"]:
        if deliverable_terms["id"] in existing_deliverable_ids:
            continue
        collaboration["deliverables"].append(
            {
                "id": deliverable_terms["id"],
                "title": deliverable_terms.get("title"),
                "candidate_terms": None,
                "terms": None,
                "submissions": [],
                "events": [],
                "state": "awaiting_agreement",
            }
        )
        existing_deliverable_ids.add(deliverable_terms["id"])
    agreement_source = None
    if payload.get("agreement_source") is not None:
        agreement_source = _resolve_source(
            connection,
            identity,
            artifact["work_id"],
            payload["agreement_source"],
            "agreement_source",
        )
    approval_scope = {
        "collaboration_id": collaboration["id"],
        "terms": deepcopy(terms),
        "agreement_source": deepcopy(agreement_source),
    }
    collaboration["terms_proposal"] = {
        "terms": terms,
        "gaps": gaps,
        "agreement_source": agreement_source,
        "approval_purpose": "creator_collaboration_agreement",
        "approval_scope": approval_scope,
        "recorded_by": identity["user"]["id"],
        "recorded_at": now,
        "provenance": "user_recorded_terms_not_agreement",
    }
    collaboration["state"] = (
        "amendment_terms_recorded"
        if collaboration.get("current_agreement_id")
        else "terms_recorded_not_agreed"
    )
    _append_event(
        collaboration,
        "collaboration_terms_recorded",
        identity,
        now,
        {
            "terms": deepcopy(terms),
            "gaps": gaps,
            "agreement_source": deepcopy(agreement_source),
            "provenance": "user_recorded_terms_not_agreement",
        },
        payload,
        source_refs=terms.get("source_refs", []),
    )


def _confirm_agreement(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"collaboration_id", "approval_id"},
        {"collaboration_id", "approval_id", "client_disclosure", "client_summary", "source_refs"},
        "confirm_collaboration_agreement",
    )
    collaboration = _collaboration(business, payload["collaboration_id"])
    if collaboration["state"] == "cancelled":
        raise ApiError("COLLABORATION_CANCELLED", "Cancelled collaboration cannot be approved", 409)
    proposal = collaboration.get("terms_proposal")
    if not isinstance(proposal, dict):
        raise ApiError("COLLABORATION_TERMS_REQUIRED", "Record exact contract terms before confirming an agreement", 409)
    if proposal.get("gaps"):
        raise ApiError("COLLABORATION_TERMS_INCOMPLETE", "Complete the exact delivery, rights, deadline and quote terms before agreement approval", 409, {"gaps": proposal["gaps"]})
    agreement_source = proposal.get("agreement_source")
    if not agreement_source:
        raise ApiError("COLLABORATION_CONTRACT_SOURCE_REQUIRED", "Select the exact saved contract or agreement source before approval", 409)
    current_source = _resolve_source(
        connection,
        identity,
        artifact["work_id"],
        agreement_source,
        "agreement_source",
    )
    if current_source != agreement_source:
        raise ApiError("COLLABORATION_CONTRACT_SOURCE_CHANGED", "Recheck the exact saved contract source before confirming agreement", 409)
    existing_agreement = collaboration.get("current_agreement")
    if (
        existing_agreement
        and existing_agreement.get("terms") == proposal["terms"]
        and existing_agreement.get("source") == agreement_source
    ):
        raise ApiError("COLLABORATION_AGREEMENT_UNCHANGED", "This exact agreement was already approved", 409)
    approval_id = _text(payload["approval_id"], "approval_id", maximum=200)
    scope = approval_scope_for_agreement(collaboration)
    approval = require_artifact_approval(
        connection,
        identity,
        approval_id,
        artifact["id"],
        version["version"],
        proposal["approval_purpose"],
        scope,
    )
    agreement_id = str(uuid.uuid4())
    terms = proposal["terms"]
    quote = terms.get("quote") or {}
    commitment_cost_id = None
    currency = quote.get("currency")
    if currency:
        from .business_execution import _record_cost

        previous_agreement = collaboration.get("current_agreement")
        previous_cost_id = previous_agreement.get("commitment_cost_id") if previous_agreement else None
        previous_cost = next(
            (item for item in business["costs"] if item["id"] == previous_cost_id),
            None,
        )
        if previous_cost and previous_cost["currency"] != currency:
            raise ApiError("COLLABORATION_CURRENCY_CHANGE_REQUIRES_CANCELLATION", "Reconcile the prior currency commitment before confirming a new currency", 409)
        commitment_cost_id = f"collaboration:{collaboration['id']}:commitment:{agreement_id}"
        cost_entry = {
            "id": commitment_cost_id,
            "kind": "committed",
            "amount": quote.get("amount"),
            "currency": currency,
            "reason": f"协作协议版本 {len(collaboration['agreements']) + 1}；用户审核确认，非支付回执",
            "occurred_at": now,
            "source_refs": quote.get("source_refs") or [],
        }
        if previous_cost_id:
            cost_entry["supersedes"] = previous_cost_id
        _record_cost(business, cost_entry, identity["user"]["id"], now)
        collaboration.setdefault("cost_record_ids", []).append(commitment_cost_id)
    approval_record = {
        "id": approval["id"],
        "artifact_id": artifact["id"],
        "artifact_version": version["version"],
        "purpose": approval["purpose"],
        "scope": scope,
        "approved_by": approval["approved_by"],
        "created_at": approval["created_at"],
    }
    agreement = {
        "id": agreement_id,
        "terms": deepcopy(terms),
        "source": deepcopy(agreement_source),
        "approval": approval_record,
        "commitment_cost_id": commitment_cost_id,
        "recorded_by": identity["user"]["id"],
        "recorded_at": now,
        "provenance": "user_recorded_contract_source_and_exact_artifact_approval; signature_not_independently_verified",
    }
    collaboration.setdefault("agreements", []).append(agreement)
    collaboration["current_agreement"] = agreement
    collaboration["current_agreement_id"] = agreement_id
    collaboration["state"] = "agreement_confirmed_user_recorded"
    deliverables_by_id = {item["id"]: item for item in collaboration["deliverables"]}
    for deliverable_terms in terms["deliverables"]:
        deliverable = deliverables_by_id[deliverable_terms["id"]]
        deliverable["terms"] = deepcopy(deliverable_terms)
        deliverable["state"] = (
            "excluded_by_agreement"
            if deliverable_terms["scope_status"] == "excluded"
            else "awaiting_submission"
        )
    _refresh_collaboration_state(collaboration)
    _append_event(
        collaboration,
        "collaboration_agreement_confirmed",
        identity,
        now,
        {
            "agreement_id": agreement_id,
            "approval": approval_record,
            "agreement_source": deepcopy(agreement_source),
            "commitment_cost_id": commitment_cost_id,
            "provenance": agreement["provenance"],
        },
        payload,
        source_refs=terms.get("source_refs", []),
    )



def _adopt_candidate(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"candidate_id", "reason"},
        {"candidate_id", "reason", "client_disclosure", "client_summary"},
        "adopt_collaboration_candidate",
    )
    candidate_id = _text(payload.get("candidate_id"), "candidate_id")
    candidate = _candidate_index(business).get(candidate_id)
    if candidate is None:
        raise ApiError("COLLABORATION_CANDIDATE_NOT_FOUND", "The selected candidate is not in this saved collaboration result", 409)
    if candidate.get("project_id") != artifact["work_id"]:
        raise ApiError("COLLABORATION_PROJECT_MISMATCH", "The candidate is not bound to this Work", 409)
    reason = _text(payload.get("reason"), "reason")
    collaborations = business["collaborations"]
    active = next(
        (
            item
            for item in collaborations
            if item.get("candidate_id") == candidate_id
            and item.get("state") not in {"cancelled", "declined"}
        ),
        None,
    )
    if active is not None:
        raise ApiError("COLLABORATION_ALREADY_OPEN", "This candidate already has an open collaboration record", 409, {"collaboration_id": active["id"]})
    deliverables = []
    seen = set()
    for row in candidate.get("deliverables", []):
        deliverable_id = _text(row.get("id"), "candidate deliverable id")
        if deliverable_id in seen:
            raise ApiError("COLLABORATION_DELIVERABLE_DUPLICATE", "Candidate deliverables need unique identities", 409)
        seen.add(deliverable_id)
        deliverables.append(
            {
                "id": deliverable_id,
                "title": row.get("title"),
                "candidate_terms": deepcopy(row),
                "terms": None,
                "submissions": [],
                "events": [],
                "state": "awaiting_agreement",
            }
        )
    identifier = str(uuid.uuid4())
    event = _event(
        "candidate_adopted_for_negotiation",
        identity,
        now,
        {"reason": reason, "candidate_id": candidate_id},
        client_disclosure=payload.get("client_disclosure", False),
        client_summary=payload.get("client_summary"),
        source_refs=candidate.get("source_refs", []),
    )
    collaboration = {
        "id": identifier,
        "project_id": candidate["project_id"],
        "candidate_id": candidate_id,
        "creator_id": candidate["creator_id"],
        "candidate_snapshot": deepcopy(candidate),
        "state": "candidate_selected_for_negotiation",
        "terms_proposal": None,
        "pending_agreement": None,
        "agreements": [],
        "current_agreement_id": None,
        "deliverables": deliverables,
        "cost_record_ids": [],
        "events": [event],
        "created_by": identity["user"]["id"],
        "created_at": now,
    }
    business["collaborations"].append(collaboration)
    business["decisions"].append(
        {
            "id": str(uuid.uuid4()),
            "choice": "进入本项目合作条款协商",
            "reason": reason,
            "alternatives": [candidate.get("display_label") or candidate_id],
            "counterevidence": deepcopy(candidate.get("fit_concerns", [])),
            "constraints": {"collaboration_id": identifier, "candidate_id": candidate_id},
            "section_ids": ["collaboration_scope", "collaboration_terms"],
            "recorded_by": identity["user"]["id"],
            "recorded_at": now,
            "provenance": "user_recorded_business_decision",
            "approval": "not_granted_by_decision",
        }
    )


def _decline_candidate(connection, identity, artifact, version, business, payload, now):
    _keys(
        payload,
        {"candidate_id", "reason"},
        {"candidate_id", "reason", "client_disclosure", "client_summary"},
        "decline_collaboration_candidate",
    )
    candidate_id = _text(payload.get("candidate_id"), "candidate_id")
    candidate = _candidate_index(business).get(candidate_id)
    if candidate is None:
        raise ApiError("COLLABORATION_CANDIDATE_NOT_FOUND", "The selected candidate is not in this saved collaboration result", 409)
    reason = _text(payload.get("reason"), "reason")
    business["decisions"].append(
        {
            "id": str(uuid.uuid4()),
            "choice": "本项目未采用该合作候选",
            "reason": reason,
            "alternatives": [candidate.get("display_label") or candidate_id],
            "counterevidence": deepcopy(candidate.get("fit_concerns", [])),
            "constraints": {"candidate_id": candidate_id, "project_id": artifact["work_id"]},
            "section_ids": ["collaboration_scope", "collaboration_terms"],
            "recorded_by": identity["user"]["id"],
            "recorded_at": now,
            "provenance": "user_recorded_business_decision",
            "approval": "not_granted_by_decision",
        }
    )
    # The non-selection is kept as an event; the source candidate itself is immutable.
    business["collaboration_decisions"] = business.get("collaboration_decisions", [])
    business["collaboration_decisions"].append(
        _event(
            "candidate_not_selected",
            identity,
            now,
            {
                "candidate_id": candidate_id,
                "candidate_label": candidate.get("display_label") or "未命名候选",
                "reason": reason,
            },
            client_disclosure=payload.get("client_disclosure", False),
            client_summary=payload.get("client_summary"),
            source_refs=candidate.get("source_refs", []),
        )
    )


def _display(value):
    if value in (None, ""):
        return "未知"
    if type(value) is bool:
        return "是" if value else "否"
    if isinstance(value, dict):
        if not value:
            return "无"
        return "；".join(f"{key}：{_display(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "；".join(_display(item) for item in value) or "无"
    return str(value)


_RIGHTS_MEDIA_LABELS = {
    "brand_owned_social": "品牌自有社媒",
    "creator_owned_social": "创作者账号",
    "paid_media": "付费媒体",
}
_RIGHTS_TERRITORY_LABELS = {"CN-Guangzhou": "中国·广州"}


def _rights_value_summary(value, labels):
    if isinstance(value, list):
        return "、".join(labels.get(str(item), _display(item)) for item in value) or "未知"
    if value is None:
        return "未知"
    return labels.get(str(value), _display(value))


def _rights_term_summary(rights):
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


def _source_label(source):
    if not isinstance(source, dict):
        return "来源未知"
    if source.get("kind") == "artifact":
        return f"{source.get('title') or 'Artifact'} · v{source.get('artifact_version') or '?'}"
    if source.get("kind") == "material":
        filename = source.get("filename") or "已保存资料"
        return f"{source.get('title') or filename} · SHA-256 {str(source.get('material_sha256') or '')[:12]}"
    return "来源未知"


def _state_label(value):
    return {
        "candidate_selected_for_negotiation": "候选已选入本项目协商",
        "terms_recorded_not_agreed": "条款已记录，尚无已审核协议",
        "amendment_terms_recorded": "协议修订提案已记录，原协议仍保留",
        "agreement_confirmed_user_recorded": "精确协议来源与审批已记录",
        "deliverable_submission_in_review": "有交付版本待审核",
        "deliverable_review_partially_complete": "部分交付已有结论，仍有交付待处理",
        "cancelled": "用户记录已取消后续协作，既有交付和账目保留",
        "declined": "用户记录未选择候选",
        "awaiting_agreement": "等待合同范围确认",
        "awaiting_submission": "等待提交交付物",
        "submission_recorded_for_review": "交付版本已保存，等待验收",
        "feedback_recorded": "已记录该版本的反馈",
        "revision_requested": "已记录返修要求，等待新版本",
        "deliverables_accepted_by_exact_approval": "所有纳入交付项均有精确审批记录接收（非客户外部回执）",
        "deliverable_rejected_user_recorded": "用户记录未接收该交付，非外部客户回执",
        "accepted_by_exact_approval": "已由指定版本审批记录接收",
        "rejected_user_recorded": "用户记录未接收，非外部客户回执",
        "excluded_by_agreement": "当前协议未包含此交付项",
    }.get(value, "状态待核")


def _event_label(value):
    return {
        "candidate_adopted_for_negotiation": "候选进入协商",
        "candidate_not_selected": "本次未选候选",
        "collaboration_terms_recorded": "合作条款记录",
        "collaboration_agreement_confirmed": "协议审批记录",
        "deliverable_submitted": "交付提交记录",
        "feedback_recorded": "交付反馈记录",
        "revision_requested": "返修要求记录",
        "deliverable_accepted": "交付接收记录",
        "deliverable_rejected_user_recorded": "交付未接收记录",
        "collaboration_cancelled": "取消记录",
        "collaboration_cost_recorded": "费用记录",
    }.get(value, "协作事件")


def _client_event_label(value):
    return {
        "candidate_adopted_for_negotiation": "本项目合作进度",
        "collaboration_terms_recorded": "合作范围记录",
        "collaboration_agreement_confirmed": "协议记录",
        "deliverable_submitted": "交付进度",
        "feedback_recorded": "反馈记录",
        "revision_requested": "返修进度",
        "deliverable_accepted": "接收进度",
        "deliverable_rejected_user_recorded": "交付处理记录",
        "collaboration_cancelled": "合作调整记录",
        "collaboration_cost_recorded": "费用记录",
    }.get(value, "项目记录")


def _permission_label(value):
    if value is True:
        return "用户记录允许"
    if value is False:
        return "用户记录不允许"
    return "未知/待确认"


def _quote_state_label(value):
    return {
        "quote": "报价",
        "estimate": "估计",
        "committed": "协议金额",
    }.get(value, "状态未知")


def _identity_label(value):
    return {
        "operator_confirmed": "用户记录跨账号关联（未独立核验）",
        "user_recorded_link_not_independently_verified": "用户记录跨账号关联（未独立核验）",
        "UNKNOWN": "未核实",
    }.get(value, "身份关系待核")


def _contactability_label(value):
    return {
        "NOT_CONTACTABLE": "未获联系许可或许可不完整",
        "GRANT_SCOPE_OR_EXPIRY_INCOMPLETE": "同意范围或有效期不完整",
        "GRANT_EXPIRED_FOR_PROJECT": "许可在项目开始前到期",
        "USER_RECORDED_GRANT_REQUIRES_PERMISSION_RECHECK": "用户记录已同意，仍需按原渠道核对当前许可",
        "UNKNOWN": "许可状态未知",
    }.get(value, "许可状态待核")


def _cost_kind_label(value):
    return {
        "estimate": "估计",
        "committed": "未履行承诺",
        "actual": "用户记录实支",
        "refund": "用户记录退款",
    }.get(value, "费用类型未知")


def _acceptance_label(value):
    if not isinstance(value, dict):
        return "未知"
    reviewer = value.get("reviewer") or "验收负责人未知"
    criteria = value.get("criteria") or []
    return f"负责人 {reviewer}；标准 {_display(criteria)}"
