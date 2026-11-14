"""Coordinate precise domain revisions without replacing their version owners."""
from __future__ import annotations

import json
import uuid

from flask import g, request

from .api import ApiError, _body, _idempotency_key, _job_data, _request_fingerprint, _string, _success
from .auth import utc_now
from .collaboration import require_artifact_access, require_work_access, work_commission


SCHEMA = """
CREATE TABLE IF NOT EXISTS revision_batches (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT NOT NULL REFERENCES works(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    title TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS revision_members (
    batch_id TEXT NOT NULL REFERENCES revision_batches(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    member_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    target_json TEXT NOT NULL,
    command_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(batch_id,member_id)
);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _target(connection, identity, work_id, target, *, write):
    kind, object_id = target["kind"], target["id"]
    if kind == "artifact":
        row = require_artifact_access(connection, identity, object_id, write=write)
        version = connection.execute("SELECT input_snapshot_json FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                                     (object_id, identity["workspace"]["id"], target["base_version"])).fetchone()
        if version is None:
            raise ApiError("NOT_FOUND", "The selected result version was not found", 404)
        snapshot = json.loads(version["input_snapshot_json"] or "{}")
        operation_kind = snapshot.get("kind") or "text"
    elif kind == "media_project":
        row = connection.execute("SELECT * FROM media_projects WHERE id=? AND workspace_id=?",
                                 (object_id, identity["workspace"]["id"])).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "The selected media project was not found", 404)
        if row["work_id"]:
            require_work_access(connection, identity, row["work_id"], write=write)
        version = connection.execute("SELECT version FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                                     (object_id, identity["workspace"]["id"], target["base_version"])).fetchone()
        if version is None:
            raise ApiError("NOT_FOUND", "The selected media version was not found", 404)
        operation_kind = "media_project"
    else:
        raise ApiError("INVALID_REVISION_TARGET", "Select an existing editable result or media project", 400)
    source_scope = work_commission(connection, row["work_id"])
    if source_scope and source_scope != work_commission(connection, work_id):
        raise ApiError("CLIENT_SCOPE_MISMATCH", "This revision belongs to a different client", 403)
    if target.get("operation_kind", operation_kind) != operation_kind:
        raise ApiError("REVISION_TARGET_CHANGED", "The selected domain no longer matches this result version", 409)
    return operation_kind


def _batch(connection, identity, batch_id):
    row = connection.execute("SELECT * FROM revision_batches WHERE id=? AND workspace_id=?",
                             (batch_id, identity["workspace"]["id"])).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "The revision batch was not found", 404)
    require_work_access(connection, identity, row["work_id"])
    return row


def revision_batch_data(store, identity, batch_id):
    with store.transaction(immediate=False) as connection:
        batch = _batch(connection, identity, batch_id)
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (batch["job_id"],)).fetchone()
        members = connection.execute("SELECT * FROM revision_members WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
    return {"id": batch_id, "work_id": batch["work_id"], "title": batch["title"], "status": job["status"], "job": _job_data(job),
            "members": [{"member_id": row["member_id"], "target": json.loads(row["target_json"]), "status": row["status"],
                         "result_ref": json.loads(row["result_json"]) if row["result_json"] else None, "error_code": row["error_code"]} for row in members]}


def create_revision_batch(app, store, identity, work_id, data, idempotency_key):
    if not isinstance(data, dict) or set(data) - {"title", "members"}:
        raise ApiError("INVALID_INPUT", "Revision batch contains an unsupported field", 400)
    title = _string(data.get("title"), "title", maximum=300)
    key = _string(idempotency_key, "Idempotency-Key", maximum=200)
    members = data.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 5000:
        raise ApiError("INVALID_INPUT", "Select between one and 5000 revision members", 400)
    workspace_id = identity["workspace"]["id"]
    fingerprint = _request_fingerprint({"work_id": work_id, "data": data})
    batch_id, job_id, now = str(uuid.uuid4()), str(uuid.uuid4()), utc_now()
    with store.transaction() as connection:
        require_work_access(connection, identity, work_id, write=True)
        prior = connection.execute("SELECT * FROM revision_batches WHERE workspace_id=? AND idempotency_key=?", (workspace_id, key)).fetchone()
        if prior:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for another revision batch", 409)
            batch_id = prior["id"]
        else:
            qualified, seen_members, seen_targets = [], set(), set()
            handlers = app.extensions["oevira.revision_handlers"]
            for ordinal, item in enumerate(members):
                if not isinstance(item, dict) or set(item) != {"member_id", "target", "command"}:
                    raise ApiError("INVALID_INPUT", "A revision member needs member_id, target and command", 400)
                member_id = _string(item["member_id"], "member_id", maximum=100)
                target, command = item["target"], item["command"]
                if not isinstance(target, dict) or set(target) != {"kind", "id", "base_version"} or not isinstance(command, dict):
                    raise ApiError("INVALID_REVISION_TARGET", "Select the exact object, parent version and domain changes", 400)
                if not isinstance(target["kind"], str) or type(target["base_version"]) is not int or target["base_version"] < 1:
                    raise ApiError("INVALID_REVISION_TARGET", "A revision target needs a positive saved version", 400)
                object_id = _string(target["id"], "target.id", maximum=100)
                object_key = (target["kind"], object_id)
                if member_id in seen_members or object_key in seen_targets:
                    raise ApiError("DUPLICATE_REVISION_TARGET", "Combine changes for one object into a single member", 400)
                if "base_version" in command and command["base_version"] != target["base_version"]:
                    raise ApiError("REVISION_PARENT_MISMATCH", "The command and target must use the same parent version", 400)
                target = {**target, "id": object_id}
                operation_kind = _target(connection, identity, work_id, target, write=True)
                if operation_kind not in handlers:
                    raise ApiError("REVISION_DOMAIN_UNAVAILABLE", "This selected object requires its domain revision operation", 409,
                                   {"member_id": member_id, "kind": operation_kind})
                target["operation_kind"] = operation_kind
                qualified.append((member_id, ordinal, target, {**command, "base_version": target["base_version"]}))
                seen_members.add(member_id)
                seen_targets.add(object_key)
            connection.execute("INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,'revision_batch','queued',?,?,?,?,?)",
                               (job_id, workspace_id, identity["user"]["id"], work_id, _json({"batch_id": batch_id}), fingerprint, key, now, now))
            connection.execute("INSERT INTO revision_batches VALUES(?,?,?,?,?,?,?,?,?)", (batch_id, workspace_id, work_id, job_id, title, key, fingerprint, identity["user"]["id"], now))
            for member_id, ordinal, target, command in qualified:
                connection.execute("INSERT INTO revision_members VALUES(?,?,?,?,?,?,'pending',NULL,NULL,?)",
                                   (batch_id, workspace_id, member_id, ordinal, _json(target), _json(command), now))
    return revision_batch_data(store, identity, batch_id)


def validate_revision_resume(connection, identity, job, context):
    if context:
        raise ApiError("INVALID_INPUT", "Changed targets or opinions require a new revision batch", 400)
    batch = _batch(connection, identity, json.loads(job["payload_json"])["batch_id"])
    require_work_access(connection, identity, batch["work_id"], write=True)
    # Individual source/parent conflicts remain member outcomes. The worker
    # rechecks each domain before acting, so independent members can continue.


def process_revision_job(app, claim):
    from .worker import _advance, _set_job_error, claim_can_commit

    store = app.extensions["oevira.store"]
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    job = store.row("SELECT * FROM jobs WHERE id=?", (claim["id"],))
    batch_id = json.loads(job["payload_json"])["batch_id"]
    try:
        with store.transaction(immediate=False) as connection:
            batch = _batch(connection, identity, batch_id)
        members = store.rows("SELECT * FROM revision_members WHERE batch_id=? ORDER BY ordinal", (batch_id,))
        failures = 0
        for ordinal, member in enumerate(members):
            if not _advance(store, claim["id"], claim["generation"], ordinal, len(members), "Applying selected revisions"):
                return
            if member["status"] == "completed":
                continue
            target, command = json.loads(member["target_json"]), json.loads(member["command_json"])
            try:
                with store.transaction(immediate=False) as connection:
                    kind = _target(connection, identity, batch["work_id"], target, write=True)
                handler = app.extensions["oevira.revision_handlers"].get(kind)
                if handler is None:
                    raise ApiError("REVISION_DOMAIN_UNAVAILABLE", "The selected domain revision is unavailable", 409)
                result = handler(store, identity, target["id"], command, f"revision-batch:{batch_id}:{member['member_id']}")
                saved = result.get("artifact", result.get("project", result)) if isinstance(result, dict) else None
                if (not isinstance(saved, dict) or saved.get("id") != target["id"]
                        or type(saved.get("version")) is not int or saved["version"] != target["base_version"] + 1):
                    raise ApiError("REVISION_RESULT_UNCONFIRMED", "The domain did not return a confirmed successor version", 409)
                reference = {"kind": target["kind"], "id": saved["id"], "version": saved["version"], "parent_version": target["base_version"]}
                with store.transaction() as connection:
                    current = connection.execute("SELECT status,lease_generation FROM jobs WHERE id=?", (claim["id"],)).fetchone()
                    if current["lease_generation"] != claim["generation"] or current["status"] not in {"running", "pause_requested", "cancel_requested"}:
                        return
                    if target["kind"] == "artifact":
                        persisted = connection.execute("SELECT parent_version FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                                                       (target["id"], claim["workspace_id"], saved["version"])).fetchone()
                    else:
                        persisted = connection.execute("SELECT parent_version FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                                                       (target["id"], claim["workspace_id"], saved["version"])).fetchone()
                    if persisted is None or persisted["parent_version"] != target["base_version"]:
                        raise ApiError("REVISION_RESULT_UNCONFIRMED", "The returned revision has no matching saved parent relationship", 409)
                    # Persist an already committed domain result even if stop
                    # arrived in that operation; never replay or undo it.
                    connection.execute("UPDATE revision_members SET status='completed',result_json=?,error_code=NULL,updated_at=? WHERE batch_id=? AND member_id=?",
                                       (_json(reference), utc_now(), batch_id, member["member_id"]))
            except ApiError as error:
                failures += 1
                with store.transaction() as connection:
                    if not claim_can_commit(connection, claim, allow_stopping=True):
                        return
                    connection.execute("UPDATE revision_members SET status='failed',error_code=?,updated_at=? WHERE batch_id=? AND member_id=?",
                                       (error.code, utc_now(), batch_id, member["member_id"]))
        with store.transaction() as connection:
            current = connection.execute("SELECT status,lease_generation FROM jobs WHERE id=?", (claim["id"],)).fetchone()
            if current["lease_generation"] != claim["generation"] or current["status"] != "running":
                return
            connection.execute("UPDATE jobs SET status=?,progress_completed=?,progress_total=?,progress_label=?,error_code=?,error_message=?,updated_at=? WHERE id=?",
                               ("partial" if failures else "completed", len(members), len(members), "Selected revision results saved",
                                "REVISION_MEMBERS_INCOMPLETE" if failures else None,
                                "Some members need correction; completed revisions were preserved." if failures else None, utc_now(), claim["id"]))
    except ApiError as error:
        _set_job_error(store, claim["id"], claim["generation"], error.code, error.message)


def register_revision_routes(app, store):
    connection = store.connect()
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()
    from .api import revise_artifact
    from .business_execution import revise_business_result
    from .creation_research import revise_research_artifact

    handlers = app.extensions.setdefault("oevira.revision_handlers", {})
    handlers.update({"text": revise_artifact, "business_analysis": revise_business_result, "research": revise_research_artifact})
    app.extensions.setdefault("oevira.job_resume_validators", {})["revision_batch"] = validate_revision_resume

    @app.post("/api/works/<work_id>/revision-batches")
    def submit_revision_batch(work_id):
        result = create_revision_batch(app, store, g.identity, work_id, _body(), _idempotency_key())
        return _success(result, 202 if result["status"] == "queued" else 200)

    @app.get("/api/revision-batches/<batch_id>")
    def get_revision_batch(batch_id):
        return _success(revision_batch_data(store, g.identity, batch_id))

    @app.get("/api/works/<work_id>/revision-batches")
    def list_revision_batches(work_id):
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError as error:
            raise ApiError("INVALID_INPUT", "limit must be an integer", 400) from error
        if not 1 <= limit <= 200:
            raise ApiError("INVALID_INPUT", "limit must be between one and 200", 400)
        with store.transaction(immediate=False) as connection:
            require_work_access(connection, g.identity, work_id)
            parameters = [g.identity["workspace"]["id"], work_id]
            predicate = ""
            before = request.args.get("before")
            if before:
                cursor = _batch(connection, g.identity, before)
                if cursor["work_id"] != work_id:
                    raise ApiError("INVALID_REVISION_CURSOR", "The cursor belongs to a different work", 400)
                predicate = " AND (created_at<? OR (created_at=? AND id<?))"
                parameters.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
            parameters.append(limit + 1)
            rows = connection.execute("SELECT id FROM revision_batches WHERE workspace_id=? AND work_id=?" + predicate + " ORDER BY created_at DESC,id DESC LIMIT ?", parameters).fetchall()
        return _success({"items": [revision_batch_data(store, g.identity, row["id"]) for row in rows[:limit]],
                         "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})
