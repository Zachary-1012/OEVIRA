"""Persistent user controls for the existing Job and its worker lease."""
from __future__ import annotations

import json
import uuid
from contextlib import nullcontext

from .api import ApiError, _job_data, _request_fingerprint, _string
from .auth import utc_now
from .collaboration import require_material_access, require_work_access, require_workspace_access


WAITING_STATES = {"queued", "retry_wait", "waiting_for_input", "waiting_for_auth"}


def available_controls(row, *, resume_kinds=(), cancelled_retry_kinds=()) -> list[str]:
    status = row["status"]
    if status == "completed":
        return []
    if status == "paused":
        return ["resume", "cancel"]
    if status == "pause_requested":
        return ["cancel"]
    if status == "interrupted" and "pause_requested" in row.keys() and row["pause_requested"]:
        return ["resume", "cancel"]
    if status in WAITING_STATES or status == "running":
        return ["pause", "cancel"]
    if (status in {"failed", "interrupted", "cancelled"} or (status == "partial" and row["kind"] in {"delivery_batch", "revision_batch"})) and row["artifact_id"] is None:
        if "pause_requested" in row.keys() and row["pause_requested"]:
            return []
        retry_kinds = {"material_comparison", "acquire_url"}
        retry_kinds.update(set(resume_kinds) & set(cancelled_retry_kinds))
        if status != "cancelled":
            retry_kinds.update(resume_kinds)
            retry_kinds.discard("intelligence_turn")
        return ["retry"] if row["kind"] in retry_kinds else []
    return []


def _require_job_access(connection, identity: dict, row, *, executing: bool) -> None:
    require_workspace_access(connection, identity, write=True)
    if row["work_id"]:
        require_work_access(connection, identity, row["work_id"], write=executing)
    elif row["user_id"] != identity["user"]["id"] and identity.get("role") != "owner":
        raise ApiError("NOT_FOUND", "The requested resource was not found", 404)


def _validate_resume(app, connection, identity: dict, row, resume_context: dict) -> str | None:
    if row["error_code"] == "WORKSPACE_RESTORE_HISTORY_ONLY":
        raise ApiError("JOB_RESTORE_HISTORY_ONLY", "Restored task history cannot be replayed", 409)
    # Resume runs as the original initiator. A later controller cannot confer
    # its own access on the saved attempt.
    actor = {"workspace": {"id": row["workspace_id"]}, "user": {"id": row["user_id"]}}
    _require_job_access(connection, actor, row, executing=True)
    payload = json.loads(row["payload_json"])
    if row["kind"] == "material_comparison":
        if resume_context:
            raise ApiError("INVALID_INPUT", "Continue with the original source selection and question", 400)
        for material in payload["input_snapshot"]["materials"]:
            current = require_material_access(connection, actor, material["material_id"], target_work_id=row["work_id"])
            if current["sha256"] != material["sha256"]:
                raise ApiError("JOB_INPUT_CHANGED", "A selected source changed; start a new comparison", 409)
    elif row["kind"] == "acquire_url":
        # This existing path performs an anonymous read and reconciles its
        # staged original before fetching again; it has no publishing effect.
        if resume_context:
            raise ApiError("INVALID_INPUT", "Continue with the original source request", 400)
        validator = app.extensions.get("oevira.job_resume_validators", {}).get("acquire_url")
        if validator is not None:
            return validator(connection, actor, row, resume_context)
        return
    else:
        validator = app.extensions.get("oevira.job_resume_validators", {}).get(row["kind"])
        if validator is None:
            if row["attempts"] == 0 and row["suspended_from"] in WAITING_STATES and not resume_context:
                return
            raise ApiError("JOB_RECONCILIATION_REQUIRED", "Review the saved task result before continuing", 409,
                           {"job_id": row["id"], "kind": row["kind"]})
        return validator(connection, actor, row, resume_context)


def control_job(app, store, identity: dict, job_id: str, action: str, data: dict, *, legacy=False, connection=None) -> dict:
    from .worker import install_worker_schema

    if connection is None:
        install_worker_schema(store)
    allowed = {"operation_id", "base_control_revision"}
    if action == "resume":
        allowed.add("resume_context")
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Task control contains an unsupported field", 400)
    operation_id = data.get("operation_id")
    base_revision = data.get("base_control_revision")
    if not legacy or data:
        operation_id = _string(operation_id, "operation_id", maximum=200)
        if type(base_revision) is not int or base_revision < 0:
            raise ApiError("INVALID_INPUT", "base_control_revision must be a nonnegative integer", 400)
    else:
        # Older clients express an explicit new action without a durable key.
        # Keep that compatibility, but retain the action in the same history.
        operation_id = str(uuid.uuid4())
    resume_context = data.get("resume_context", {})
    if not isinstance(resume_context, dict):
        raise ApiError("INVALID_INPUT", "resume_context must be an object", 400)
    fingerprint = _request_fingerprint({"job_id": job_id, "action": action, "data": data})
    workspace_id = identity["workspace"]["id"]
    transaction = store.transaction() if connection is None else nullcontext(connection)
    with transaction as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (job_id, workspace_id)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "The requested resource was not found", 404)
        _require_job_access(connection, identity, row, executing=False)
        if operation_id:
            receipt = connection.execute("SELECT * FROM job_control_receipts WHERE workspace_id=? AND operation_id=?",
                                         (workspace_id, operation_id)).fetchone()
            if receipt:
                if receipt["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This control was already used for different input", 409)
                result = _job_data(row)
                result["control_replayed"] = True
                result["control_receipt"] = dict(receipt)
                result["control_receipt"].pop("workspace_id")
                result["control_receipt"].pop("request_hash")
                return result
        if base_revision is not None and row["control_revision"] != base_revision:
            raise ApiError("JOB_CONTROL_CONFLICT", "The task was controlled by a newer operation", 409,
                           {"current": _job_data(row)})
        status = row["status"]
        interrupted_pause = status == "interrupted" and bool(row["pause_requested"])
        next_status = status
        pause_requested = row["pause_requested"]
        cancel_requested = row["cancel_requested"]
        suspended_from = row["suspended_from"]
        if action == "pause":
            if status in WAITING_STATES:
                suspended_from, next_status, pause_requested = status, "paused", 1
            elif status == "running":
                suspended_from, next_status, pause_requested = status, "pause_requested", 1
            elif status not in {"paused", "pause_requested", "completed"}:
                raise ApiError("JOB_NOT_PAUSABLE", "This task cannot be paused in its current state", 409)
        elif action == "cancel":
            if status in WAITING_STATES or status == "paused" or interrupted_pause:
                next_status, cancel_requested, pause_requested = "cancelled", 1, 0
            elif status in {"running", "pause_requested"}:
                next_status, cancel_requested, pause_requested = "cancel_requested", 1, 0
        elif action in {"resume", "retry"}:
            if status != "completed":
                if action == "resume" and status != "paused" and not interrupted_pause:
                    raise ApiError("JOB_NOT_RESUMABLE", "Only a confirmed paused task can continue", 409)
                if action == "retry" and row["kind"] == "intelligence_turn":
                    raise ApiError("INTELLIGENCE_RESULT_RECONCILIATION_REQUIRED", "Review the saved conversation and completed operations before starting a new turn", 409)
                cancelled_kinds = {"material_comparison", "acquire_url"} | set(app.extensions.get("oevira.job_cancelled_retry_kinds", ()))
                cancelled_replay = status == "cancelled" and row["kind"] in cancelled_kinds
                partial_batch = status == "partial" and row["kind"] in {"delivery_batch", "revision_batch"}
                if action == "retry" and ((status not in {"failed", "interrupted"} and not cancelled_replay and not partial_batch) or row["artifact_id"] is not None):
                    raise ApiError("JOB_NOT_RETRYABLE", "This task cannot be safely retried", 409)
                if action == "retry" and row["pause_requested"]:
                    raise ApiError("JOB_PAUSE_UNCONFIRMED", "Verify the stopped task before continuing its saved work", 409)
                _require_job_access(connection, identity, row, executing=True)
                validated_state = _validate_resume(app, connection, identity, row, resume_context)
                if validated_state is not None and (not isinstance(validated_state, str) or validated_state not in WAITING_STATES):
                    raise ApiError("JOB_RESUME_STATE_INVALID", "The task's prerequisites could not be confirmed", 409)
                next_status = validated_state or (suspended_from if action == "resume" and suspended_from in {"waiting_for_input", "waiting_for_auth", "retry_wait"} else "queued")
                pause_requested, cancel_requested, suspended_from = 0, 0, None
        else:
            raise ValueError("Unknown task control")
        revision = row["control_revision"] + 1
        now = utc_now()
        connection.execute("UPDATE jobs SET status=?,pause_requested=?,cancel_requested=?,suspended_from=?,control_revision=?,updated_at=? WHERE id=?",
                           (next_status, pause_requested, cancel_requested, suspended_from, revision, now, job_id))
        if operation_id:
            connection.execute("INSERT INTO job_control_receipts VALUES(?,?,?,?,?,?,?,?)",
                               (workspace_id, operation_id, job_id, action, fingerprint, revision, next_status, now))
        updated = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        result = _job_data(updated)
        result["control_replayed"] = False
        if operation_id:
            result["control_receipt"] = {"operation_id": operation_id, "job_id": job_id, "action": action,
                                         "control_revision": revision, "status": next_status, "created_at": now}
        return result


def acknowledge_stop(store, claim: dict) -> None:
    """Called only after the domain executor has left its operation boundary."""
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET status='paused',updated_at=? WHERE id=? AND lease_generation=? AND status='pause_requested' AND pause_requested=1",
                           (utc_now(), claim["id"], claim["generation"]))
        # A cancel may replace pause after the domain's last checkpoint but
        # before its executor returns. Confirm that stop without leaving a
        # permanently pending control; completed/unknown effects stay intact.
        connection.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=? AND status='cancel_requested' AND cancel_requested=1",
                           (utc_now(), claim["id"], claim["generation"]))
