"""Client commission permissions within the existing workspace and work model."""
from __future__ import annotations

import json
import uuid

from flask import g, request

from .api import ApiError, _body, _idempotency_key, _not_found, _request_fingerprint, _require_write, _string, _success
from .auth import utc_now


_COMMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_comments (
    id TEXT PRIMARY KEY,
    artifact_id TEXT REFERENCES artifacts(id),
    artifact_version INTEGER,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    anchor_json TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'resolved', 'withdrawn')),
    resolution_json TEXT,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    media_project_id TEXT REFERENCES media_projects(id),
    media_project_version INTEGER,
    CHECK (
        (artifact_id IS NOT NULL AND typeof(artifact_version)='integer' AND artifact_version>0
         AND media_project_id IS NULL AND media_project_version IS NULL)
        OR
        (artifact_id IS NULL AND artifact_version IS NULL AND media_project_id IS NOT NULL
         AND typeof(media_project_version)='integer' AND media_project_version>0)
    )
);
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS client_commissions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commission_members (
    commission_id TEXT NOT NULL REFERENCES client_commissions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('owner', 'editor', 'reviewer', 'viewer')),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    PRIMARY KEY(commission_id, user_id)
);
CREATE TABLE IF NOT EXISTS commission_versions (
    commission_id TEXT NOT NULL REFERENCES client_commissions(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(commission_id, version)
);
CREATE TABLE IF NOT EXISTS work_commissions (
    work_id TEXT PRIMARY KEY REFERENCES works(id),
    commission_id TEXT NOT NULL REFERENCES client_commissions(id),
    created_at TEXT NOT NULL
);
""" + _COMMENT_SCHEMA + """
CREATE TABLE IF NOT EXISTS artifact_comment_versions (
    comment_id TEXT NOT NULL REFERENCES artifact_comments(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(comment_id, version)
);
CREATE TABLE IF NOT EXISTS artifact_approvals (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    artifact_version INTEGER NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    approved_by TEXT NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT REFERENCES users(id)
);
"""


def install_collaboration_schema(store) -> None:
    from .media import install_media_schema

    install_media_schema(store)
    connection = store.connect()
    try:
        _upgrade_review_targets(connection)
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def _upgrade_review_targets(connection) -> None:
    """Retain original comment IDs/history while admitting native Media targets."""
    legacy_columns = (
        "id", "artifact_id", "artifact_version", "workspace_id", "author_id", "anchor_json",
        "body", "state", "resolution_json", "version", "created_at", "updated_at",
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(artifact_comments)")}
        if not columns:
            connection.commit()
            return
        media_columns = {"media_project_id", "media_project_version"}
        if media_columns <= columns.keys() and not columns["artifact_id"]["notnull"] and not columns["artifact_version"]["notnull"]:
            connection.commit()
            return
        if set(columns) not in (set(legacy_columns), set(legacy_columns) | media_columns):
            raise RuntimeError("The saved review schema is not a supported migration source; all data was preserved")
        if media_columns <= columns.keys():
            legacy_columns = (*legacy_columns, "media_project_id", "media_project_version")
        schema_dependents = [row["sql"] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='artifact_comments' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )]
        temporary = "artifact_comments__target_migration"
        connection.execute(_COMMENT_SCHEMA.replace("IF NOT EXISTS artifact_comments", temporary, 1))
        names = ",".join(legacy_columns)
        connection.execute(f"INSERT INTO {temporary}({names}) SELECT {names} FROM artifact_comments")
        if connection.execute(f"SELECT {names} FROM artifact_comments EXCEPT SELECT {names} FROM {temporary}").fetchone() is not None:
            raise RuntimeError("The review target migration did not preserve every original row")
        if connection.execute(f"SELECT {names} FROM {temporary} EXCEPT SELECT {names} FROM artifact_comments").fetchone() is not None:
            raise RuntimeError("The review target migration introduced a different original row")
        connection.execute("DROP TABLE artifact_comments")
        connection.execute(f"ALTER TABLE {temporary} RENAME TO artifact_comments")
        for statement in schema_dependents:
            connection.execute(statement)
        if list(connection.execute("PRAGMA foreign_key_check(artifact_comments)")):
            raise RuntimeError("Review target references could not be verified; the migration was rolled back")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_comment_versions'").fetchone():
            if list(connection.execute("PRAGMA foreign_key_check(artifact_comment_versions)")):
                raise RuntimeError("Review history references could not be verified; the migration was rolled back")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def require_workspace_access(connection, identity: dict, *, write: bool = False) -> None:
    member = connection.execute(
        "SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ? AND active = 1",
        (identity["workspace"]["id"], identity["user"]["id"]),
    ).fetchone()
    if member is None:
        raise _not_found()
    if write and member["role"] not in {"owner", "editor"}:
        raise ApiError("FORBIDDEN", "Your current workspace role cannot make this change", 403)


def require_commission_access(connection, identity: dict, commission_id: str, *, write=False, manage=False):
    require_workspace_access(connection, identity, write=write or manage)
    row = connection.execute(
        "SELECT c.*, m.role AS commission_role FROM client_commissions c "
        "JOIN commission_members m ON m.commission_id = c.id "
        "WHERE c.id = ? AND c.workspace_id = ? AND m.user_id = ? AND m.active = 1",
        (commission_id, identity["workspace"]["id"], identity["user"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    if manage and row["commission_role"] != "owner":
        raise ApiError("FORBIDDEN", "Only this commission's owner can change its access", 403)
    if write and row["commission_role"] not in {"owner", "editor"}:
        raise ApiError("FORBIDDEN", "Your commission role cannot change this work", 403)
    if write and row["status"] != "active":
        raise ApiError("COMMISSION_ARCHIVED", "Reopen this commission before changing its work", 409)
    return row


def require_work_access(connection, identity: dict, work_id: str, *, write=False):
    require_workspace_access(connection, identity, write=write)
    row = connection.execute(
        "SELECT * FROM works WHERE id = ? AND workspace_id = ?",
        (work_id, identity["workspace"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    link = connection.execute("SELECT commission_id FROM work_commissions WHERE work_id = ?", (work_id,)).fetchone()
    if link:
        require_commission_access(connection, identity, link["commission_id"], write=write)
    if write and row["status"] != "active":
        raise ApiError("WORK_ARCHIVED", "Reopen this work before making changes", 409)
    return row


def work_commission(connection, work_id: str | None) -> str | None:
    if work_id is None:
        return None
    row = connection.execute("SELECT commission_id FROM work_commissions WHERE work_id = ?", (work_id,)).fetchone()
    return row["commission_id"] if row else None


def require_material_access(connection, identity: dict, material_id: str, *, target_work_id: str | None = None):
    require_workspace_access(connection, identity)
    row = connection.execute(
        "SELECT * FROM materials WHERE id = ? AND workspace_id = ?",
        (material_id, identity["workspace"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    if row["work_id"]:
        require_work_access(connection, identity, row["work_id"])
    source_scope = work_commission(connection, row["work_id"])
    if target_work_id is not None and source_scope and source_scope != work_commission(connection, target_work_id):
        raise ApiError("CLIENT_SCOPE_MISMATCH", "Private client material cannot be adopted into a different commission", 403)
    return row


def require_artifact_access(connection, identity: dict, artifact_id: str, *, write=False):
    require_workspace_access(connection, identity, write=write)
    row = connection.execute(
        "SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?",
        (artifact_id, identity["workspace"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    from .community import require_native_artifact_access

    require_native_artifact_access(connection, identity, artifact_id, write=write)
    if row["work_id"]:
        require_work_access(connection, identity, row["work_id"], write=write)
    return row


def require_artifact_control_access(
    connection,
    identity: dict,
    artifact_id: str,
    *,
    approval_owner_id: str | None = None,
):
    """Authorize stopping a saved plan or withdrawing only the caller's approval.

    Control still requires current workspace and Work/commission membership.
    Archived Work or commissions do not block cancellation/revocation, while
    ordinary edits continue through ``require_artifact_access(write=True)``.
    ``approval_owner_id`` must come from the persisted approval row.
    """
    artifact = require_artifact_access(connection, identity, artifact_id)
    if approval_owner_id is not None:
        if not isinstance(approval_owner_id, str) or not approval_owner_id.strip():
            raise ApiError("INVALID_APPROVAL_OWNER", "A stored approval owner is required", 400)
        if approval_owner_id != identity["user"]["id"]:
            raise ApiError("APPROVAL_OWNER_MISMATCH", "You can withdraw only your own approval", 403)

    if approval_owner_id is None:
        # Cancellation is a workspace editor action, restricted further by the
        # commission role when the artifact belongs to client work.
        require_workspace_access(connection, identity, write=True)

    commission_id = work_commission(connection, artifact["work_id"])
    if commission_id is not None:
        commission = require_commission_access(connection, identity, commission_id)
        allowed_roles = {"owner", "reviewer"} if approval_owner_id is not None else {"owner", "editor"}
        if commission["commission_role"] not in allowed_roles:
            message = (
                "Only the approval owner with current review authority can withdraw this approval"
                if approval_owner_id is not None else
                "Only a current commission owner or editor can stop this plan"
            )
            raise ApiError("FORBIDDEN", message, 403)
    elif approval_owner_id is not None:
        # Workspace-only approval uses the same owner/editor boundary as the
        # existing _review_access helper.
        require_workspace_access(connection, identity, write=True)
    return artifact


def _commission_data(connection, row) -> dict:
    members = connection.execute(
        "SELECT user_id, role, active FROM commission_members WHERE commission_id = ? ORDER BY user_id", (row["id"],)
    ).fetchall()
    return {"id": row["id"], "name": row["name"], "scope": json.loads(row["scope_json"]),
            "status": row["status"], "version": row["version"], "members": [dict(item) for item in members]}


def _save_commission_version(connection, row, user_id: str) -> None:
    connection.execute(
        "INSERT INTO commission_versions VALUES(?, ?, ?, ?, ?)",
        (row["id"], row["version"], json.dumps(_commission_data(connection, row), ensure_ascii=False), user_id, utc_now()),
    )


def _base_version(data: dict, current: int) -> None:
    base = data.get("base_version")
    if type(base) is not int or base < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400)
    if base != current:
        raise ApiError("VERSION_CONFLICT", "The commission changed since it was opened", 409, {"current_version": current})


def register_collaboration_routes(app) -> None:
    if "list_commissions" in app.view_functions:
        return
    store = app.extensions["oevira.store"]

    @app.get("/api/business/commissions")
    def list_commissions():
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute(
                "SELECT c.* FROM client_commissions c JOIN commission_members m ON m.commission_id=c.id "
                "WHERE c.workspace_id=? AND m.user_id=? AND m.active=1 ORDER BY c.updated_at DESC, c.id",
                (g.identity["workspace"]["id"], g.identity["user"]["id"]),
            ).fetchall()
            return _success([_commission_data(connection, row) for row in rows])

    @app.post("/api/business/commissions")
    def create_commission():
        identity, data = _require_write(), _body()
        if set(data) - {"name", "scope"} or not isinstance(data.get("scope", {}), dict):
            raise ApiError("INVALID_INPUT", "Provide a commission name and scope object", 400)
        name = _string(data.get("name"), "name", maximum=300)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "An idempotency key is required", 400)
        workspace, user = identity["workspace"]["id"], identity["user"]["id"]
        fingerprint = _request_fingerprint(data)
        with store.transaction() as connection:
            require_workspace_access(connection, identity, write=True)
            prior = connection.execute(
                "SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='create_commission' AND key=?", (workspace, key)
            ).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another commission request", 409)
                row = require_commission_access(connection, identity, prior["resource_id"])
                return _success(_commission_data(connection, row))
            commission_id, now = str(uuid.uuid4()), utc_now()
            connection.execute(
                "INSERT INTO client_commissions VALUES(?, ?, ?, ?, 'active', 1, ?, ?, ?)",
                (commission_id, workspace, name, json.dumps(data.get("scope", {}), ensure_ascii=False), user, now, now),
            )
            connection.execute("INSERT INTO commission_members VALUES(?, ?, 'owner', 1)", (commission_id, user))
            connection.execute("INSERT INTO idempotency_records VALUES(?, 'create_commission', ?, ?, 'commission', ?, ?)",
                               (workspace, key, fingerprint, commission_id, now))
            row = require_commission_access(connection, identity, commission_id)
            _save_commission_version(connection, row, user)
            return _success(_commission_data(connection, row), 201)

    @app.patch("/api/business/commissions/<commission_id>")
    def update_commission(commission_id):
        identity, data = _require_write(), _body()
        if set(data) - {"base_version", "name", "scope", "status", "member"}:
            raise ApiError("INVALID_INPUT", "Unsupported commission change", 400)
        with store.transaction() as connection:
            row = require_commission_access(connection, identity, commission_id, manage=True)
            _base_version(data, row["version"])
            name = _string(data.get("name", row["name"]), "name", maximum=300)
            scope, status = data.get("scope", json.loads(row["scope_json"])), data.get("status", row["status"])
            if not isinstance(scope, dict) or status not in {"active", "archived"}:
                raise ApiError("INVALID_INPUT", "Invalid commission scope or status", 400)
            if "member" in data:
                member = data["member"]
                if not isinstance(member, dict) or set(member) != {"user_id", "role", "active"}:
                    raise ApiError("INVALID_INPUT", "A member needs user_id, role and active", 400)
                if member["role"] not in {"owner", "editor", "reviewer", "viewer"} or type(member["active"]) is not bool:
                    raise ApiError("INVALID_INPUT", "Invalid member role or active state", 400)
                target = connection.execute("SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
                                            (identity["workspace"]["id"], member["user_id"])).fetchone()
                if target is None:
                    raise _not_found()
                connection.execute("INSERT INTO commission_members VALUES(?, ?, ?, ?) ON CONFLICT(commission_id, user_id) "
                                   "DO UPDATE SET role=excluded.role, active=excluded.active",
                                   (commission_id, member["user_id"], member["role"], int(member["active"])))
                owner = connection.execute("SELECT 1 FROM commission_members c JOIN memberships m ON m.user_id=c.user_id "
                                           "WHERE c.commission_id=? AND c.role='owner' AND c.active=1 AND m.active=1 AND m.workspace_id=?",
                                           (commission_id, identity["workspace"]["id"])).fetchone()
                if not owner:
                    raise ApiError("LAST_COMMISSION_OWNER", "Keep an active owner for this commission", 409)
            connection.execute("UPDATE client_commissions SET name=?, scope_json=?, status=?, version=version+1, updated_at=? WHERE id=?",
                               (name, json.dumps(scope, ensure_ascii=False), status, utc_now(), commission_id))
            updated = connection.execute("SELECT * FROM client_commissions WHERE id=?", (commission_id,)).fetchone()
            _save_commission_version(connection, updated, identity["user"]["id"])
            return _success(_commission_data(connection, updated))

    @app.post("/api/business/commissions/<commission_id>/works/<work_id>")
    def assign_commission_work(commission_id, work_id):
        identity = _require_write()
        with store.transaction() as connection:
            require_commission_access(connection, identity, commission_id, write=True)
            row = require_work_access(connection, identity, work_id, write=True)
            if row["owner_user_id"] != identity["user"]["id"]:
                raise ApiError("FORBIDDEN", "Only the work owner can assign its client scope", 403)
            prior = work_commission(connection, work_id)
            if prior:
                if prior != commission_id:
                    raise ApiError("CLIENT_SCOPE_CONFLICT", "Work cannot be moved between private client scopes", 409)
                return _success({"work_id": work_id, "commission_id": commission_id})
            for table in (
                "materials", "work_materials", "artifacts", "jobs", "media_projects",
                "media_recording_sessions", "monitor_rules", "business_cases",
                "business_evidence", "business_relations", "business_metric_observations",
            ):
                if connection.execute(f"SELECT 1 FROM {table} WHERE work_id=? LIMIT 1", (work_id,)).fetchone():
                    raise ApiError("WORK_ALREADY_HAS_CONTENT", "Assign the client before adding private material or results", 409)
            connection.execute("INSERT INTO work_commissions VALUES(?, ?, ?)", (work_id, commission_id, utc_now()))
            return _success({"work_id": work_id, "commission_id": commission_id}, 201)


def register_collaboration_boundary(app) -> None:
    """Protect the existing HTTP resource paths; domain workers also recheck at commit."""
    if app.extensions.get("oevira.collaboration_boundary"):
        return
    app.extensions["oevira.collaboration_boundary"] = True
    store = app.extensions["oevira.store"]

    @app.before_request
    def protect_commission_resources():
        if not request.path.startswith("/api/") or getattr(g, "identity", None) is None:
            return None
        identity = g.identity
        write = request.method not in {"GET", "HEAD", "OPTIONS"}
        values = request.view_args or {}
        body = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        body = body if isinstance(body, dict) else {}
        with store.transaction(immediate=False) as connection:
            work_id = values.get("work_id") or body.get("work_id")
            if isinstance(work_id, str):
                # Pending jobs can be stopped while archived; this check is read-only.
                replayable = request.path.endswith(("/compare", "/business-results", "/business-jobs"))
                stop_control = request.path.endswith(("/cancel", "/pause"))
                require_work_access(connection, identity, work_id, write=write and not replayable and not stop_control)
            artifact_id = values.get("artifact_id")
            if artifact_id:
                domain_authorizes = request.path.startswith("/api/business-results/") or "/comments" in request.path or "/approvals" in request.path or request.path.endswith("/exports")
                artifact = require_artifact_access(connection, identity, artifact_id, write=write and not domain_authorizes)
                work_id = artifact["work_id"]
                if request.path in {f"/api/artifacts/{artifact_id}/versions", f"/api/artifacts/{artifact_id}/restore"}:
                    current = connection.execute("SELECT v.input_snapshot_json FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id "
                                                 "AND a.current_version=v.version WHERE a.id=?", (artifact_id,)).fetchone()
                    if current and json.loads(current[0]).get("kind") == "business_analysis":
                        raise ApiError("BUSINESS_REVISION_REQUIRED", "Revise the selected business sections to preserve their calculations", 409)
            material_ids = []
            if values.get("material_id"):
                material_ids.append(values["material_id"])
            if isinstance(body.get("material_ids"), list):
                material_ids.extend(body["material_ids"])
            if isinstance(body.get("citations"), list):
                material_ids.extend(item.get("material_id") for item in body["citations"] if isinstance(item, dict))
            for material_id in material_ids:
                if isinstance(material_id, str):
                    require_material_access(connection, identity, material_id, target_work_id=work_id)
            if values.get("export_id"):
                export = connection.execute("SELECT artifact_id FROM exports WHERE id=? AND workspace_id=?",
                                            (values["export_id"], identity["workspace"]["id"])).fetchone()
                if export:
                    require_artifact_access(connection, identity, export["artifact_id"])
            if values.get("job_id"):
                job = connection.execute("SELECT work_id FROM jobs WHERE id=? AND workspace_id=?",
                                         (values["job_id"], identity["workspace"]["id"])).fetchone()
                if job and job["work_id"]:
                    require_work_access(connection, identity, job["work_id"], write=write and not request.path.endswith(("/cancel", "/pause")))

    @app.after_request
    def filter_commission_lists(response):
        kind = {"/api/works": "work", "/api/materials": "material", "/api/artifacts": "artifact"}.get(request.path)
        if not kind or request.method != "GET" or response.status_code != 200 or not hasattr(g, "identity"):
            return response
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return response
        checks = {"work": require_work_access, "material": require_material_access, "artifact": require_artifact_access}
        visible = []
        with store.transaction(immediate=False) as connection:
            for item in payload["data"]:
                try:
                    checks[kind](connection, g.identity, item["id"])
                except ApiError as error:
                    if error.status in {403, 404}:
                        continue
                    raise
                visible.append(item)
        payload["data"] = visible
        response.set_data(json.dumps(payload, ensure_ascii=False))
        response.headers["Content-Type"] = "application/json"
        return response


def _review_access(connection, identity, artifact_id):
    artifact = require_artifact_access(connection, identity, artifact_id)
    commission_id = work_commission(connection, artifact["work_id"])
    if commission_id:
        commission = require_commission_access(connection, identity, commission_id)
        if commission["commission_role"] not in {"owner", "reviewer"}:
            raise ApiError("FORBIDDEN", "This commission has not granted you approval authority", 403)
    else:
        require_workspace_access(connection, identity, write=True)
    return artifact


def require_artifact_approval(connection, identity, approval_id, artifact_id, version, purpose, scope):
    """Recheck exact approval and the approver's current authority before an effect."""
    artifact = require_artifact_access(connection, identity, artifact_id)
    approval = connection.execute("SELECT * FROM artifact_approvals WHERE id=? AND artifact_id=? AND workspace_id=?",
                                  (approval_id, artifact_id, identity["workspace"]["id"])).fetchone()
    if approval is None:
        raise _not_found()
    if approval["revoked_at"] or approval["artifact_version"] != version or artifact["current_version"] != version or approval["purpose"] != purpose or json.loads(approval["scope_json"]) != scope:
        raise ApiError("APPROVAL_NOT_CURRENT", "Approval does not cover this exact current result and action", 409)
    approver = {"workspace": identity["workspace"], "user": {"id": approval["approved_by"]}}
    _review_access(connection, approver, artifact_id)
    return approval


def register_review_routes(app) -> None:
    if "list_artifact_comments" in app.view_functions:
        return
    store = app.extensions["oevira.store"]

    @app.get("/api/artifacts/<artifact_id>/comments")
    def list_artifact_comments(artifact_id):
        raw_version = request.args.get("version")
        if len(request.args.getlist("version")) > 1:
            raise ApiError("INVALID_INPUT", "version cannot be repeated", 400)
        artifact_version = None
        if raw_version is not None:
            try:
                artifact_version = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from exc
        result = read_review_context(store, g.identity, artifact_id, artifact_version=artifact_version)
        return _success(result["comments"])

    @app.post("/api/artifacts/<artifact_id>/comments")
    def create_artifact_comment(artifact_id):
        result = create_review_comment(store, g.identity, artifact_id, _body(), _idempotency_key())
        return _success(result["comment"], 201 if result["created"] else 200)

    @app.patch("/api/artifacts/<artifact_id>/comments/<comment_id>")
    def revise_artifact_comment(artifact_id, comment_id):
        return _success(revise_review_comment(store, g.identity, artifact_id, comment_id, _body(), _idempotency_key()))

    @app.get("/api/media/projects/<project_id>/comments")
    def list_media_comments(project_id):
        from .review_anchors import load_media_review_version

        with store.transaction(immediate=False) as connection:
            _require_review_target(connection, g.identity, None, project_id)
            rows = connection.execute("SELECT * FROM artifact_comments WHERE media_project_id=? AND artifact_id IS NULL AND workspace_id=? ORDER BY created_at,id", (project_id, g.identity["workspace"]["id"])).fetchall()
            visible = []
            for row in rows:
                try:
                    load_media_review_version(connection, g.identity, project_id, row["media_project_version"])
                except ApiError as error:
                    if error.status in {403, 404}:
                        continue
                    raise
                visible.append(_comment_data(row))
        return _success(visible)

    @app.post("/api/media/projects/<project_id>/comments")
    def create_media_comment(project_id):
        result = create_review_comment(store, g.identity, None, _body(), _idempotency_key(), media_project_id=project_id)
        return _success(result["comment"], 201 if result["created"] else 200)

    @app.patch("/api/media/projects/<project_id>/comments/<comment_id>")
    def revise_media_comment(project_id, comment_id):
        return _success(revise_review_comment(store, g.identity, None, comment_id, _body(), _idempotency_key(), media_project_id=project_id))

    @app.post("/api/artifacts/<artifact_id>/approvals")
    def approve_artifact(artifact_id):
        data, identity = _body(), g.identity
        if set(data) != {"artifact_version", "purpose", "scope"} or not isinstance(data["scope"], dict):
            raise ApiError("INVALID_INPUT", "Approval needs a version, purpose and exact scope", 400)
        purpose = _string(data["purpose"], "purpose", maximum=200)
        with store.transaction() as connection:
            artifact = _review_access(connection, identity, artifact_id)
            if artifact["current_version"] != data["artifact_version"]:
                raise ApiError("VERSION_CONFLICT", "Approve the current saved version", 409)
            identifier, now = str(uuid.uuid4()), utc_now()
            connection.execute("INSERT INTO artifact_approvals VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                               (identifier, artifact_id, data["artifact_version"], identity["workspace"]["id"], identity["user"]["id"], purpose, json.dumps(data["scope"], ensure_ascii=False, sort_keys=True), now))
            return _success({"id": identifier, "artifact_id": artifact_id, "artifact_version": data["artifact_version"], "purpose": purpose, "scope": data["scope"], "created_at": now}, 201)

    @app.post("/api/artifacts/<artifact_id>/approvals/<approval_id>/withdraw")
    def withdraw_artifact_approval(artifact_id, approval_id):
        with store.transaction() as connection:
            artifact = require_artifact_access(connection, g.identity, artifact_id)
            row = connection.execute("SELECT * FROM artifact_approvals WHERE id=? AND artifact_id=?", (approval_id, artifact_id)).fetchone()
            if row is None:
                raise _not_found()
            if row["approved_by"] != g.identity["user"]["id"]:
                _review_access(connection, g.identity, artifact["id"])
            connection.execute("UPDATE artifact_approvals SET revoked_at=COALESCE(revoked_at,?),revoked_by=COALESCE(revoked_by,?) WHERE id=?", (utc_now(), g.identity["user"]["id"], approval_id))
            return _success({"id": approval_id, "state": "withdrawn"})


def read_review_context(store, identity: dict, artifact_id: str, *, artifact_version: int | None = None) -> dict:
    """Read feedback only after checking the exact saved result and its sources."""
    from .review_anchors import load_review_version, validate_review_anchor, verify_review_sources

    if artifact_version is not None and (type(artifact_version) is not int or artifact_version < 1):
        raise ApiError("INVALID_INPUT", "Select a positive Artifact version", 400)
    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id)
        if artifact_version is None:
            rows = connection.execute(
                "SELECT * FROM artifact_comments WHERE artifact_id=? ORDER BY created_at,id",
                (artifact_id,),
            ).fetchall()
            comments = [_comment_data(row) for row in rows]
            return {"artifact_id": artifact_id, "current_version": artifact["current_version"],
                    "comments": comments}
        source = load_review_version(
            connection, identity["workspace"]["id"], artifact_id, artifact_version,
        )
        verify_review_sources(connection, identity, source)
        rows = connection.execute(
            "SELECT * FROM artifact_comments WHERE artifact_id=? AND artifact_version=? ORDER BY created_at,id",
            (artifact_id, artifact_version),
        ).fetchall()
        comments = []
        for row in rows:
            comment = _comment_data(row)
            anchor = validate_review_anchor(store, source, comment["anchor"], identity=identity)
            if anchor != comment["anchor"]:
                raise ApiError("COMMENT_ANCHOR_CHANGED", "The saved review target no longer matches this Artifact version", 409)
            comments.append(comment)
        return {
            "artifact_id": artifact_id, "artifact_version": artifact_version,
            "current_version": artifact["current_version"], "comments": comments,
        }


def _review_target_fields(artifact_id, media_project_id):
    if (artifact_id is None) == (media_project_id is None):
        raise ApiError("INVALID_REVIEW_TARGET", "Select exactly one result or Media project", 400)
    if artifact_id is not None:
        return "artifact_id", _string(artifact_id, "artifact_id", maximum=100), "artifact_version", "artifact_version"
    return "media_project_id", _string(media_project_id, "media_project_id", maximum=100), "media_project_version", "project_version"


def _require_review_target(connection, identity, artifact_id, media_project_id, *, write=False):
    if artifact_id is not None:
        return require_artifact_access(connection, identity, artifact_id, write=write)
    require_workspace_access(connection, identity, write=write)
    project = connection.execute("SELECT * FROM media_projects WHERE id=? AND workspace_id=?", (media_project_id, identity["workspace"]["id"])).fetchone()
    if project is None:
        raise _not_found()
    if project["work_id"]:
        require_work_access(connection, identity, project["work_id"], write=write)
    return project


def _load_comment_source(connection, identity, artifact_id, media_project_id, version, *, write=False):
    from .review_anchors import load_review_version, load_media_review_version

    if artifact_id is not None:
        return load_review_version(connection, identity["workspace"]["id"], artifact_id, version)
    return load_media_review_version(connection, identity, media_project_id, version, write=write)


def create_review_comment(store, identity: dict, artifact_id: str | None, data: dict, idempotency_key: str, *, media_project_id: str | None = None) -> dict:
    from .review_anchors import validate_review_anchor, verify_review_version, verify_review_sources

    target_column, target_id, version_column, version_field = _review_target_fields(artifact_id, media_project_id)
    if not isinstance(data, dict) or set(data) != {version_field, "anchor", "body"}:
        raise ApiError("INVALID_INPUT", "A comment needs the exact saved target version, anchor and body", 400)
    body = _string(data["body"], "comment", maximum=10000)
    key = idempotency_key
    if not key:
        raise ApiError("IDEMPOTENCY_REQUIRED", "A comment requires an idempotency key", 400)
    key = _string(key, "Idempotency-Key", maximum=200)
    fingerprint = _request_fingerprint({target_column: target_id, **data})
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        _require_review_target(connection, identity, artifact_id, media_project_id)
        prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='artifact_comment' AND key=?",
                                   (workspace_id, key)).fetchone()
        if prior:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another comment", 409)
            return {"comment": _comment_data(connection.execute("SELECT * FROM artifact_comments WHERE id=?", (prior["resource_id"],)).fetchone()), "created": False}
        source = _load_comment_source(connection, identity, artifact_id, media_project_id, data[version_field])
        verify_review_sources(connection, identity, source)
    anchor = validate_review_anchor(store, source, data["anchor"], identity=identity)
    with store.transaction() as connection:
        _require_review_target(connection, identity, artifact_id, media_project_id)
        verify_review_version(connection, source)
        verify_review_sources(connection, identity, source)
        prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='artifact_comment' AND key=?", (workspace_id, key)).fetchone()
        if prior:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another comment", 409)
            return {"comment": _comment_data(connection.execute("SELECT * FROM artifact_comments WHERE id=?", (prior["resource_id"],)).fetchone()), "created": False}
        identifier, now = str(uuid.uuid4()), utc_now()
        connection.execute("INSERT INTO artifact_comments(id,artifact_id,artifact_version,workspace_id,author_id,anchor_json,body,state,resolution_json,version,created_at,updated_at,media_project_id,media_project_version) VALUES(?,?,?,?,?,?,?,'open',NULL,1,?,?,?,?)",
                           (identifier, artifact_id, data[version_field] if artifact_id is not None else None, workspace_id, identity["user"]["id"], json.dumps(anchor, ensure_ascii=False), body, now, now, media_project_id, data[version_field] if media_project_id is not None else None))
        row = connection.execute("SELECT * FROM artifact_comments WHERE id=?", (identifier,)).fetchone()
        connection.execute("INSERT INTO artifact_comment_versions VALUES(?,1,?,?,?)", (identifier, json.dumps(_comment_data(row), ensure_ascii=False), identity["user"]["id"], now))
        connection.execute("INSERT INTO idempotency_records VALUES(?,'artifact_comment',?,?,'comment',?,?)", (identity["workspace"]["id"], key, fingerprint, identifier, now))
        return {"comment": _comment_data(row), "created": True}


def revise_review_comment(store, identity: dict, artifact_id: str | None, comment_id: str, data: dict, idempotency_key: str | None = None, *, media_project_id: str | None = None) -> dict:
    from .review_anchors import review_resolution_evidence, verify_review_version, verify_review_sources, comment_revision_replay

    target_column, target_id, version_column, version_field = _review_target_fields(artifact_id, media_project_id)
    if not isinstance(data, dict) or set(data) - {"base_version", "body", "state", "resolution"}:
        raise ApiError("INVALID_INPUT", "Unsupported comment change", 400)
    workspace_id = identity["workspace"]["id"]
    key = _string(idempotency_key, "Idempotency-Key", maximum=200) if idempotency_key is not None else None
    fingerprint = _request_fingerprint({target_column: target_id, "comment_id": comment_id, **data})
    other_target = "artifact_id" if media_project_id is not None else "media_project_id"
    comment_query = f"SELECT * FROM artifact_comments WHERE id=? AND {target_column}=? AND {other_target} IS NULL AND workspace_id=?"
    source = changed = None
    with store.transaction(immediate=False) as connection:
        _require_review_target(connection, identity, artifact_id, media_project_id)
        row = connection.execute(comment_query, (comment_id, target_id, workspace_id)).fetchone()
        if row is None:
            raise _not_found()
        replay = comment_revision_replay(connection, workspace_id, comment_id, key, fingerprint)
        if replay is not None:
            return replay
        _base_version(data, row["version"])
        state = data.get("state", row["state"])
        if not isinstance(state, str) or state not in {"open", "resolved", "withdrawn"}:
            raise ApiError("INVALID_INPUT", "Unknown comment state", 400)
        body, resolution = row["body"], json.loads(row["resolution_json"]) if row["resolution_json"] else None
        if "body" in data or state == "withdrawn":
            if identity["user"]["id"] != row["author_id"]:
                raise ApiError("FORBIDDEN", "Only the author can change or withdraw their comment", 403)
            body = _string(data.get("body", body), "comment", maximum=10000)
        if state == "resolved":
            _require_review_target(connection, identity, artifact_id, media_project_id, write=True)
            resolution = data.get("resolution")
            if (not isinstance(resolution, dict) or not {version_field, "disposition", "reason"} <= set(resolution)
                    or set(resolution) - {version_field, "disposition", "reason", "target_anchor"}
                    or not isinstance(resolution.get("disposition"), str)
                    or resolution["disposition"] not in {"adopted", "not_adopted"}):
                raise ApiError("RESOLUTION_REQUIRED", "Link the actual revised version or explain why the comment was not adopted", 400)
            resolution = dict(resolution)
            resolution["reason"] = _string(resolution["reason"], "resolution.reason", maximum=5000)
            changed = _load_comment_source(connection, identity, artifact_id, media_project_id, resolution[version_field], write=True)
            source = _load_comment_source(connection, identity, artifact_id, media_project_id, row[version_column], write=True)
            verify_review_sources(connection, identity, source)
            verify_review_sources(connection, identity, changed)
            if resolution["disposition"] == "adopted" and resolution[version_field] <= row[version_column]:
                raise ApiError("RESOLUTION_VERSION_INVALID", "An adopted comment must open a later saved version", 409)
        elif "state" in data:
            resolution = None
        original_comment_version = row["version"]
        original_anchor = json.loads(row["anchor_json"])
    if state == "resolved" and resolution["disposition"] == "adopted":
        resolution["change_evidence"] = review_resolution_evidence(store, source, changed, original_anchor, resolution.get("target_anchor"), identity=identity)
    with store.transaction() as connection:
        _require_review_target(connection, identity, artifact_id, media_project_id, write=state == "resolved")
        row = connection.execute(comment_query, (comment_id, target_id, workspace_id)).fetchone()
        if row is None:
            raise _not_found()
        replay = comment_revision_replay(connection, workspace_id, comment_id, key, fingerprint)
        if replay is not None:
            return replay
        if row["version"] != original_comment_version:
            _base_version(data, row["version"])
        if source is not None:
            verify_review_version(connection, source)
            verify_review_version(connection, changed)
            verify_review_sources(connection, identity, source)
            verify_review_sources(connection, identity, changed)
        now, version = utc_now(), row["version"] + 1
        connection.execute("UPDATE artifact_comments SET body=?,state=?,resolution_json=?,version=?,updated_at=? WHERE id=?", (body, state, json.dumps(resolution, ensure_ascii=False) if resolution else None, version, now, comment_id))
        updated = connection.execute("SELECT * FROM artifact_comments WHERE id=?", (comment_id,)).fetchone()
        connection.execute("INSERT INTO artifact_comment_versions VALUES(?,?,?,?,?)", (comment_id, version, json.dumps(_comment_data(updated), ensure_ascii=False), identity["user"]["id"], now))
        if key:
            connection.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
                               (workspace_id, f"revise_comment:{comment_id}", key, fingerprint, "comment_version", str(version), now))
        return _comment_data(updated)


def _comment_data(row):
    media_project_id = row["media_project_id"] if "media_project_id" in row.keys() else None
    target = {"kind": "artifact", "id": row["artifact_id"], "version": row["artifact_version"]} if row["artifact_id"] is not None else {
        "kind": "media_project", "id": media_project_id, "version": row["media_project_version"],
    }
    return {"id": row["id"], "artifact_id": row["artifact_id"], "artifact_version": row["artifact_version"],
            "media_project_id": media_project_id,
            "media_project_version": row["media_project_version"] if media_project_id is not None else None,
            "target_ref": target,
            "author_id": row["author_id"], "anchor": json.loads(row["anchor_json"]), "body": row["body"],
            "state": row["state"], "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None,
            "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def transfer_local_commission_membership(connection, workspace_id: str, local_user_id: str, account_user_id: str) -> dict:
    """Move current custody during the account owner's explicitly confirmed local merge.

    The account service verifies both identities and records the merge intent.
    This function shares its transaction; historical creators and approvals are
    retained, and no authority is inferred merely from matching email text.
    """
    source = connection.execute("SELECT role,active FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, local_user_id)).fetchone()
    target = connection.execute("SELECT role,active FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, account_user_id)).fetchone()
    if source is None or source["active"] != 1 or source["role"] != "owner" or target is None or target["active"] != 1:
        raise ApiError("LOCAL_MERGE_ACCESS_INVALID", "Confirm local ownership and the verified destination membership before merging", 403)
    rows = connection.execute("SELECT c.*,m.role AS source_role FROM client_commissions c JOIN commission_members m ON m.commission_id=c.id WHERE c.workspace_id=? AND m.user_id=? AND m.active=1", (workspace_id, local_user_id)).fetchall()
    transfers = []
    for row in rows:
        existing = connection.execute("SELECT role,active FROM commission_members WHERE commission_id=? AND user_id=?", (row["id"], account_user_id)).fetchone()
        role = row["source_role"]
        if existing and existing["active"] and existing["role"] != role:
            if "owner" in {existing["role"], role}:
                role = "owner"
            else:
                raise ApiError("COMMISSION_ROLE_CONFLICT", "Resolve the two different client roles before merging these identities", 409, {"commission_id": row["id"], "local_role": role, "account_role": existing["role"]})
        connection.execute("INSERT INTO commission_members VALUES(?,?,?,1) ON CONFLICT(commission_id,user_id) DO UPDATE SET role=excluded.role,active=1", (row["id"], account_user_id, role))
        connection.execute("UPDATE commission_members SET active=0 WHERE commission_id=? AND user_id=?", (row["id"], local_user_id))
        connection.execute("UPDATE client_commissions SET version=version+1,updated_at=? WHERE id=?", (utc_now(), row["id"]))
        updated = connection.execute("SELECT * FROM client_commissions WHERE id=?", (row["id"],)).fetchone()
        _save_commission_version(connection, updated, account_user_id)
        transfers.append({"commission_id": row["id"], "role": role, "version": updated["version"]})
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Draft authorship is current custody; immutable publication versions retain original creators.
    if "publications" in tables:
        connection.execute("UPDATE publications SET author_id=? WHERE workspace_id=? AND author_id=?", (account_user_id, workspace_id, local_user_id))
    return {"commission_transfers": transfers, "historical_authorship": "preserved"}


def prepare_account_deletion(connection, workspace_id, deleted_user_id, successor_user_id, *, dry_run=False):
    """Remove current commission authority without transferring private clients implicitly."""
    rows = connection.execute("SELECT c.*,m.role AS departing_role FROM client_commissions c JOIN commission_members m ON m.commission_id=c.id WHERE c.workspace_id=? AND m.user_id=? AND m.active=1", (workspace_id, deleted_user_id)).fetchall()
    conflicts = []
    for row in rows:
        if row["departing_role"] != "owner":
            continue
        replacement = connection.execute("SELECT 1 FROM commission_members c JOIN memberships m ON m.user_id=c.user_id WHERE c.commission_id=? AND c.user_id<>? AND c.role='owner' AND c.active=1 AND m.workspace_id=? AND m.active=1", (row["id"], deleted_user_id, workspace_id)).fetchone()
        if replacement is None:
            conflicts.append({"commission_id": row["id"], "name": row["name"], "reason": "last_client_owner_requires_explicit_transfer"})
    if conflicts:
        raise ApiError("CLIENT_OWNERSHIP_TRANSFER_REQUIRED", "Transfer the listed private client commissions before deleting this account", 409, {"commissions": conflicts})
    summary = {"commission_ids": [row["id"] for row in rows], "implicit_client_grants": False, "historical_authorship": "retained_as_pseudonymous_history"}
    if dry_run:
        return summary
    now = utc_now()
    for row in rows:
        connection.execute("UPDATE commission_members SET active=0 WHERE commission_id=? AND user_id=?", (row["id"], deleted_user_id))
        connection.execute("UPDATE client_commissions SET version=version+1,updated_at=? WHERE id=?", (now, row["id"]))
        updated = connection.execute("SELECT * FROM client_commissions WHERE id=?", (row["id"],)).fetchone()
        _save_commission_version(connection, updated, successor_user_id)
    connection.execute("UPDATE artifact_approvals SET revoked_at=COALESCE(revoked_at,?),revoked_by=COALESCE(revoked_by,?) WHERE workspace_id=? AND approved_by=?", (now, successor_user_id, workspace_id, deleted_user_id))
    return summary
