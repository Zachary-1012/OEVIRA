"""Execute exact, user-approved business actions through existing domain owners."""
from __future__ import annotations

from copy import deepcopy
import json
import uuid

from flask import g

from .api import ApiError, _body, _create_export, _idempotency_key, _job_data, _not_found, _request_fingerprint, _success
from .auth import utc_now
from .business_execution import _hash, _insert_version, _json, _key, _positive_version, _read_business
from .collaboration import _review_access, require_artifact_access, require_work_access


def validate_execution(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError("INVALID_ACTION_EXECUTION", "Choose an available execution channel", 400)
    if value.get("kind") == "artifact_export":
        if not {"kind", "artifact_id", "version", "format"} <= value.keys() or set(value) - {"kind", "artifact_id", "version", "format", "audience"} or not isinstance(value["artifact_id"], str) or value["format"] not in {"docx", "pptx", "xlsx", "pdf", "html", "md"}:
            raise ApiError("INVALID_ACTION_EXECUTION", "File delivery requires an exact saved result, version and format", 400)
        _positive_version(value["version"])
        if value.get("audience", "internal") not in {"internal", "client"}:
            raise ApiError("INVALID_ACTION_EXECUTION", "Choose the internal or client file view", 400)
    elif value.get("kind") == "browser_action":
        required = {"kind", "session_id", "session_version", "page_id", "page_revision", "action", "purpose"}
        if set(value) != required or not all(isinstance(value[key], str) and value[key].strip() for key in ("session_id", "page_id", "purpose")):
            raise ApiError("INVALID_ACTION_EXECUTION", "Browser execution needs the exact authorized page and intended action", 400)
        _positive_version(value["session_version"])
        if type(value["page_revision"]) is not int or value["page_revision"] < 0:
            raise ApiError("INVALID_ACTION_EXECUTION", "The selected browser page revision is invalid", 400)
        from .browser_api import _safe_action
        from .browser import BrowserError
        try:
            _safe_action(value["action"])
        except BrowserError as error:
            raise ApiError(error.code, "The selected browser action is invalid", error.status) from error
        if value["action"]["type"] not in {"click", "press"}:
            raise ApiError("BROWSER_CONFIRMATION_ACTION_REQUIRED", "Prepare the content in the controlled browser, then select its exact confirmation action; credentials are not stored in business plans", 400)
    else:
        raise ApiError("EXECUTION_CHANNEL_UNAVAILABLE", "This execution channel is not connected; the plan and deliverables remain available", 409)
    return deepcopy(value)


def action_digest(action):
    return _hash({key: action.get(key) for key in ("id", "title", "target", "responsible", "deliverable", "due_at", "depends_on", "section_ids", "execution")})


def _current_actor(connection, workspace_id, user_id, session_id):
    row = connection.execute("SELECT m.role FROM sessions s JOIN memberships m ON m.workspace_id=s.workspace_id AND m.user_id=s.user_id "
                             "WHERE s.id=? AND s.workspace_id=? AND s.user_id=? AND s.revoked_at IS NULL AND s.expires_at>? AND m.active=1",
                             (session_id, workspace_id, user_id, utc_now())).fetchone()
    if row is None:
        raise ApiError("EXECUTION_AUTHORIZATION_REVOKED", "The authorizing session is no longer current", 403)
    return {"workspace": {"id": workspace_id}, "user": {"id": user_id}, "session_id": session_id, "role": row["role"]}


def _current_action(connection, identity, artifact_id, action_id, *, expected_digest=None):
    artifact, version, snapshot = _read_business(connection, identity, artifact_id, write=True)
    action = next((item for item in snapshot["business"]["actions"] if item["id"] == action_id), None)
    if action is None:
        raise _not_found()
    if expected_digest and action_digest(action) != expected_digest:
        raise ApiError("ACTION_VERSION_CHANGED", "The action changed after it was approved", 409)
    return artifact, version, snapshot, action


def register_business_action_routes(app):
    if "execute_business_action" in app.view_functions:
        return
    store = app.extensions["oevira.store"]
    app.extensions.setdefault("oevira.job_resume_validators", {})["business_action"] = validate_business_action_resume

    @app.post("/api/business-results/<artifact_id>/actions/<action_id>/execute")
    def execute_business_action(artifact_id, action_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version", "execution", "confirmation"} or data["confirmation"] != "execute_this_exact_action":
            raise ApiError("EXACT_ACTION_CONFIRMATION_REQUIRED", "Confirm the selected content, target and exact execution", 400)
        base = _positive_version(data["base_version"])
        execution = validate_execution(data["execution"])
        if execution is None:
            raise ApiError("EXECUTION_CHANNEL_REQUIRED", "Choose an available execution channel", 400)
        key = _key(_idempotency_key())
        fingerprint = _hash({"artifact_id": artifact_id, "action_id": action_id, "request": data})
        with store.transaction() as connection:
            actor = _current_actor(connection, identity["workspace"]["id"], identity["user"]["id"], identity["session_id"])
            require_artifact_access(connection, actor, artifact_id)
            prior = connection.execute("SELECT * FROM jobs WHERE workspace_id=? AND kind='business_action' AND idempotency_key=?", (identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another action", 409)
                return _success(_job_data(prior))
            artifact, _, snapshot, action = _current_action(connection, actor, artifact_id, action_id)
            if artifact["current_version"] != base:
                raise ApiError("VERSION_CONFLICT", "Review the current business result before execution", 409, {"current_version": artifact["current_version"]})
            if action.get("cancellation_requested") or action["state"] == "cancelled":
                raise ApiError("ACTION_CANCELLED", "This action was stopped; reconcile the original receipt before planning any new effect", 409)
            _review_access(connection, actor, artifact_id)
            if action.get("execution") != execution:
                raise ApiError("ACTION_EXECUTION_CHANGED", "The confirmed execution does not match the saved action", 409)
            if action["state"] in {"executed", "execution_unknown", "awaiting_outcome_verification", "effect_unknown", "recorded"}:
                raise ApiError("ACTION_ALREADY_ATTEMPTED", "Review the existing action receipt before a new execution", 409)
            active = connection.execute("SELECT id FROM jobs WHERE workspace_id=? AND kind='business_action' AND json_extract(payload_json,'$.artifact_id')=? AND json_extract(payload_json,'$.action_id')=? AND status IN ('queued','running','cancel_requested','interrupted')",
                                        (identity["workspace"]["id"], artifact_id, action_id)).fetchone()
            if active:
                raise ApiError("ACTION_ALREADY_PENDING", "This action already has an execution to reconcile", 409, {"job_id": active["id"]})
            actions = {item["id"]: item for item in snapshot["business"]["actions"]}
            if any(actions[dependency]["state"] not in {"executed", "recorded"} for dependency in action["depends_on"]):
                raise ApiError("ACTION_DEPENDENCY_UNFULFILLED", "Complete or reconcile the prerequisite actions first", 409)
            if execution["kind"] == "artifact_export":
                require_artifact_access(connection, actor, execution["artifact_id"])
            else:
                session = connection.execute("SELECT * FROM browser_sessions WHERE id=? AND workspace_id=? AND owner_user_id=? AND status='open'", (execution["session_id"], identity["workspace"]["id"], identity["user"]["id"])).fetchone()
                if session is None:
                    raise _not_found()
                if session["work_id"] != artifact["work_id"] or session["version"] != execution["session_version"] or session["page_revision"] != execution["page_revision"]:
                    raise ApiError("BROWSER_ACTION_CONTEXT_CHANGED", "Select the current authorized page in this work", 409)
            job_id, now = str(uuid.uuid4()), utc_now()
            payload = {"artifact_id": artifact_id, "artifact_version_at_confirmation": base, "action_id": action_id,
                       "action_digest": action_digest(action), "execution": execution, "actor_session_id": identity["session_id"],
                       "confirmed_by": identity["user"]["id"], "confirmed_at": now, "receipt": None}
            connection.execute("INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,progress_total,progress_label,created_at,updated_at) VALUES(?,?,?,?,'business_action','queued',?,?,?,1,'等待执行已确认动作',?,?)",
                               (job_id, identity["workspace"]["id"], identity["user"]["id"], artifact["work_id"], _json(payload), fingerprint, key, now, now))
            return _success(_job_data(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), 202)


def _recover_completed_receipt(connection, row, payload):
    receipt = payload.get("receipt")
    if isinstance(receipt, dict) and receipt.get("state") in {"file_generated", "browser_input_delivered"}:
        return receipt
    execution = payload["execution"]
    if execution["kind"] != "browser_action" or not payload.get("dispatch_started_at"):
        return None
    operation = connection.execute("SELECT * FROM browser_session_operations WHERE session_id=? AND workspace_id=? AND owner_user_id=? AND operation='action' AND idempotency_key=?",
                                   (execution["session_id"], row["workspace_id"], row["user_id"], "business-action:" + row["id"])).fetchone()
    exact_request = {"base_version": execution["session_version"], "page_revision": execution["page_revision"], "page_id": execution["page_id"], "action": execution["action"]}
    if operation is None or operation["status"] != "completed" or operation["request_hash"] != _request_fingerprint(exact_request):
        raise ApiError("ACTION_RECEIPT_LOOKUP_REQUIRED", "The original browser effect remains unconfirmed; resuming cannot send it again", 409)
    return {"kind": "browser_action", "state": "browser_input_delivered", "owner_receipt": json.loads(operation["result_json"] or "{}"),
            "publication_or_payment_confirmed": False, "business_outcome": "not_observed", "recovered_operation_id": operation["id"]}


def validate_business_action_resume(connection, identity, row, resume_context):
    if resume_context:
        raise ApiError("INVALID_INPUT", "Resume reconciles the original action; it cannot replace the target or execution", 400)
    payload = json.loads(row["payload_json"])
    receipt = _recover_completed_receipt(connection, row, payload)
    actor = identity if receipt else _current_actor(connection, row["workspace_id"], row["user_id"], payload["actor_session_id"])
    artifact, _, _, action = _current_action(connection, actor, payload["artifact_id"], payload["action_id"], expected_digest=payload["action_digest"])
    if receipt:
        payload["receipt"] = receipt
        connection.execute("UPDATE jobs SET payload_json=? WHERE id=?", (_json(payload), row["id"]))
        return
    _review_access(connection, actor, artifact["id"])
    if action.get("cancellation_requested") or action["state"] == "cancelled":
        raise ApiError("ACTION_CANCELLED", "The action was stopped; resuming cannot authorize another execution", 409)
    if action.get("approval") and not action["approval"]["valid"]:
        raise ApiError("ACTION_APPROVAL_WITHDRAWN", "The exact action approval is no longer current", 409)
    execution = payload["execution"]
    if execution["kind"] == "artifact_export":
        require_artifact_access(connection, actor, execution["artifact_id"])
    else:
        session = connection.execute("SELECT * FROM browser_sessions WHERE id=? AND workspace_id=? AND owner_user_id=? AND status='open'",
                                     (execution["session_id"], row["workspace_id"], row["user_id"])).fetchone()
        if session is None or session["work_id"] != row["work_id"] or session["version"] != execution["session_version"] or session["page_revision"] != execution["page_revision"]:
            raise ApiError("BROWSER_ACTION_CONTEXT_CHANGED", "The authorized browser target changed while this action was paused", 409)


def process_business_action_job(app, claim):
    from .worker import _advance, _set_job_error, claim_can_commit
    from .browser import BrowserError
    store = app.extensions["oevira.store"]
    if not _advance(store, claim["id"], claim["generation"], 0, 1, "复核当前动作授权"):
        return
    job = store.row("SELECT * FROM jobs WHERE id=?", (claim["id"],))
    payload = json.loads(job["payload_json"])
    started = False
    try:
        with store.transaction() as connection:
            receipt = _recover_completed_receipt(connection, job, payload)
            identity = ({"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}} if receipt
                        else _current_actor(connection, claim["workspace_id"], claim["user_id"], payload["actor_session_id"]))
            artifact, _, _, action = _current_action(connection, identity, payload["artifact_id"], payload["action_id"], expected_digest=payload["action_digest"])
            _review_access(connection, identity, artifact["id"])
            current = connection.execute("SELECT * FROM jobs WHERE id=?", (claim["id"],)).fetchone()
            if not claim_can_commit(connection, claim) or current["cancel_requested"]:
                raise ApiError("ACTION_CANCELLED", "Execution was cancelled before dispatch", 409)
            if not receipt and action.get("cancellation_requested"):
                raise ApiError("ACTION_CANCELLED", "The planned action was stopped before dispatch", 409)
            if not receipt and action.get("approval") and action["approval"].get("valid") is False:
                raise ApiError("ACTION_APPROVAL_WITHDRAWN", "The action approval was withdrawn or its inputs changed", 409)
            payload["dispatch_started_at"] = payload.get("dispatch_started_at") or utc_now()
            connection.execute("UPDATE jobs SET payload_json=?,progress_label='正在执行已确认动作',updated_at=? WHERE id=? AND lease_generation=?", (_json(payload), utc_now(), claim["id"], claim["generation"]))
        started = True
        execution = payload["execution"]
        owner_key = "business-action:" + claim["id"]
        if receipt:
            terminal = "completed"
        elif execution["kind"] == "artifact_export":
            response = app.make_response(_create_export(store, execution["artifact_id"], {"version": execution["version"], "format": execution["format"], "audience": execution.get("audience", "internal")}, identity=identity, idempotency_key=owner_key))
            receipt = {"kind": "artifact_export", "export": response.get_json()["data"], "state": "file_generated", "external_delivery": "not_sent", "business_outcome": "not_observed"}
            terminal = "completed"
        else:
            browser = app.extensions["oevira.browser"]
            observed = browser.act(identity, execution["session_id"], {"base_version": execution["session_version"], "page_revision": execution["page_revision"], "page_id": execution["page_id"], "action": execution["action"]}, owner_key)
            uncertain = observed.get("operation_state") == "unknown" or observed.get("operation", {}).get("status") == "unknown"
            receipt = {"kind": "browser_action", "state": "effect_unknown" if uncertain else "browser_input_delivered", "owner_receipt": observed,
                       "publication_or_payment_confirmed": False, "business_outcome": "not_observed"}
            terminal = "interrupted" if uncertain else "completed"
        now = utc_now()
        with store.transaction() as connection:
            current = connection.execute("SELECT * FROM jobs WHERE id=?", (claim["id"],)).fetchone()
            if not claim_can_commit(connection, claim, allow_stopping=True):
                return
            payload["receipt"] = receipt
            payload["receipt_recorded_at"] = now
            try:
                artifact, version, snapshot, action = _current_action(connection, identity, payload["artifact_id"], payload["action_id"])
                description = (f"已生成指定版本的 {execution['format'].upper()} 文件，可下载核对；尚未发送给客户。" if execution["kind"] == "artifact_export"
                               else "已向所选页面发送确认操作；发布、联系或付款的实际结果仍需核对。")
                previous_receipts = [record for record in action["records"] if record["id"] == claim["id"] or record.get("reconciliation_of") == claim["id"]]
                if not any(record.get("receipt") == receipt for record in previous_receipts):
                    record_id = claim["id"] if not previous_receipts else claim["id"] + ":" + _hash(receipt)[:16]
                    action["records"].append({"id": record_id, "status": receipt["state"], "occurred_at": now, "description": description,
                                              "reconciliation_of": claim["id"] if previous_receipts else None,
                                              "provenance": "product_domain_receipt", "receipt": receipt, "external_execution_by_product": execution["kind"] == "browser_action"})
                    action["state"] = "execution_unknown" if terminal == "interrupted" else "awaiting_outcome_verification" if execution["kind"] == "browser_action" else "executed"
                    _insert_version(connection, identity, artifact, snapshot, json.loads(version["citations_json"]), "记录已确认动作的实际回执")
            except ApiError:
                payload["artifact_record_state"] = "access_changed_receipt_preserved_in_job"
            if current["cancel_requested"] and terminal == "completed":
                terminal = "partial"
            elif current["status"] == "pause_requested" and terminal == "interrupted":
                terminal = "pause_requested"
            connection.execute("UPDATE jobs SET status=?,payload_json=?,artifact_id=?,progress_completed=1,progress_label='执行回执已保存',updated_at=? WHERE id=? AND lease_generation=?", (terminal, _json(payload), payload["artifact_id"], now, claim["id"], claim["generation"]))
    except (ApiError, BrowserError) as error:
        if started and payload["execution"]["kind"] == "browser_action":
            with store.transaction() as connection:
                if not claim_can_commit(connection, claim, allow_stopping=True):
                    return
                payload["reconciliation_required"] = True
                payload["owner_error_code"] = error.code
                connection.execute("UPDATE jobs SET status=CASE WHEN status='pause_requested' THEN status ELSE 'interrupted' END,payload_json=?,error_code=?,error_message='The existing browser operation must be reconciled before another action.',updated_at=? WHERE id=? AND lease_generation=?", (_json(payload), error.code, utc_now(), claim["id"], claim["generation"]))
        else:
            _set_job_error(store, claim["id"], claim["generation"], error.code, str(error))
    except Exception:
        with store.transaction() as connection:
            if not claim_can_commit(connection, claim, allow_stopping=True):
                return
            payload["reconciliation_required"] = started
            connection.execute("UPDATE jobs SET status=CASE WHEN status='pause_requested' THEN status ELSE 'interrupted' END,payload_json=?,error_code='ACTION_RECEIPT_UNCONFIRMED',error_message='Execution needs receipt reconciliation; do not send a new request.',updated_at=? WHERE id=? AND lease_generation=?", (_json(payload), utc_now(), claim["id"], claim["generation"]))
