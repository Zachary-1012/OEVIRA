"""Result notifications for completed media, business and monitoring work.

A notification points back to an exact source version. Marking it read and
retrying an unknown delivery are independent of the originating job: they
never change a business final state or re-execute the task. After the
recipient loses access to the source work, historical notifications still
exist but their title, body and source details are withheld.
"""
from __future__ import annotations

import json
import uuid

from flask import g, request

from .api import ApiError, _not_found, _success
from .auth import utc_now
from .collaboration import require_work_access


SOURCE_KINDS = frozenset({"media", "business", "monitoring"})
SEND_STATUSES = frozenset({"recorded", "sent", "unknown", "failed"})
_TITLE_MAXIMUM = 300
_BODY_MAXIMUM = 20_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS result_notifications (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    recipient_user_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('media', 'business', 'monitoring')),
    source_ref_json TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    job_id TEXT,
    send_status TEXT NOT NULL CHECK(send_status IN ('recorded', 'sent', 'unknown', 'failed')),
    delivery_attempts INTEGER NOT NULL DEFAULT 1,
    read_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_notifications_recipient
    ON result_notifications(workspace_id, recipient_user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_result_notifications_job
    ON result_notifications(workspace_id, job_id) WHERE job_id IS NOT NULL;
"""


def install_notifications_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    finally:
        connection.close()


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value, default):
    if not value:
        return default
    loaded = json.loads(value)
    return loaded if isinstance(loaded, type(default)) else default


def _source_ref(value) -> dict:
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", "source_ref must be an object", 400, {"field": "source_ref"})
    kind = value.get("kind")
    object_id = value.get("id")
    if not isinstance(kind, str) or not kind or not isinstance(object_id, str) or not object_id:
        raise ApiError("INVALID_INPUT", "source_ref must include kind and id", 400, {"field": "source_ref"})
    result = {"kind": kind, "id": object_id}
    revision = value.get("revision")
    if revision is not None:
        result["revision"] = revision if isinstance(revision, str) else str(revision)
    version = value.get("version")
    if isinstance(version, bool) or (version is not None and not isinstance(version, int)):
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = None
    if isinstance(version, int):
        result["version"] = version
    work_id = value.get("work_id")
    if isinstance(work_id, str) and work_id:
        result["work_id"] = work_id
    return result


def _source_accessible(connection, identity: dict, source_ref: dict) -> bool:
    work_id = source_ref.get("work_id") if isinstance(source_ref, dict) else None
    if not work_id:
        return True
    try:
        require_work_access(connection, identity, work_id, write=False)
    except ApiError:
        return False
    return True


def _notification_payload(connection, identity: dict, row) -> dict:
    source_ref = _loads(row["source_ref_json"], {})
    granted = _source_accessible(connection, identity, source_ref)
    payload = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "recipient_user_id": row["recipient_user_id"],
        "source_kind": row["source_kind"],
        "job_id": row["job_id"],
        "send_status": row["send_status"],
        "delivery_attempts": row["delivery_attempts"],
        "read_at": row["read_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if not granted:
        payload.update({"access": "revoked", "title": None, "body": None, "source_ref": None})
        return payload
    payload.update({
        "access": "granted",
        "title": row["title"],
        "body": row["body"],
        "source_ref": source_ref,
    })
    return payload


def _owned(connection, identity: dict, notification_id: str):
    row = connection.execute(
        "SELECT * FROM result_notifications WHERE id = ? AND workspace_id = ? AND recipient_user_id = ?",
        (notification_id, identity["workspace"]["id"], identity["user"]["id"]),
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


def emit_result_notification(
    connection,
    *,
    workspace_id: str,
    recipient_user_id: str,
    source_kind: str,
    source_ref: dict,
    title: str,
    body: str,
    job_id: str | None = None,
    send_status: str = "recorded",
    now: str | None = None,
) -> dict:
    """Record a result notification on an already-open write transaction.

    The caller must pass the same connection that just committed the result.
    SQLite transactions do not nest, so this function never opens its own.
    A second emit for the same job returns the existing notification and
    does not insert another row or touch the job.
    """
    if source_kind not in SOURCE_KINDS:
        raise ApiError("INVALID_INPUT", "source_kind must be media, business or monitoring", 400)
    if send_status not in SEND_STATUSES:
        raise ApiError("INVALID_INPUT", "send_status is not a recognized delivery state", 400)
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ApiError("INVALID_INPUT", "workspace_id is required", 400)
    if not isinstance(recipient_user_id, str) or not recipient_user_id:
        raise ApiError("INVALID_INPUT", "recipient_user_id is required", 400)
    if not isinstance(title, str) or not title.strip() or len(title) > _TITLE_MAXIMUM:
        raise ApiError("INVALID_INPUT", "title is empty or too long", 400, {"field": "title"})
    if not isinstance(body, str) or not body or len(body) > _BODY_MAXIMUM:
        raise ApiError("INVALID_INPUT", "body is empty or too long", 400, {"field": "body"})
    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        raise ApiError("INVALID_INPUT", "job_id must be a string", 400, {"field": "job_id"})
    reference = _source_ref(source_ref)
    if job_id:
        existing = connection.execute(
            "SELECT * FROM result_notifications WHERE workspace_id = ? AND job_id = ?",
            (workspace_id, job_id),
        ).fetchone()
        if existing is not None:
            return {
                "id": existing["id"],
                "workspace_id": existing["workspace_id"],
                "recipient_user_id": existing["recipient_user_id"],
                "source_kind": existing["source_kind"],
                "source_ref": _loads(existing["source_ref_json"], {}),
                "title": existing["title"],
                "body": existing["body"],
                "job_id": existing["job_id"],
                "send_status": existing["send_status"],
                "delivery_attempts": existing["delivery_attempts"],
                "read_at": existing["read_at"],
                "created_at": existing["created_at"],
                "updated_at": existing["updated_at"],
                "access": "granted",
            }
    notification_id = str(uuid.uuid4())
    now_text = now or utc_now()
    connection.execute(
        "INSERT INTO result_notifications("
        "id, workspace_id, recipient_user_id, source_kind, source_ref_json, title, body, "
        "job_id, send_status, delivery_attempts, read_at, created_at, updated_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)",
        (
            notification_id, workspace_id, recipient_user_id, source_kind, _dumps(reference),
            title.strip(), body, job_id, send_status, now_text, now_text,
        ),
    )
    row = connection.execute(
        "SELECT * FROM result_notifications WHERE id = ? AND workspace_id = ?",
        (notification_id, workspace_id),
    ).fetchone()
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "recipient_user_id": row["recipient_user_id"],
        "source_kind": row["source_kind"],
        "source_ref": _loads(row["source_ref_json"], {}),
        "title": row["title"],
        "body": row["body"],
        "job_id": row["job_id"],
        "send_status": row["send_status"],
        "delivery_attempts": row["delivery_attempts"],
        "read_at": row["read_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "access": "granted",
    }


def emit_media_render_notification(connection, claim: dict, render_id: str, *, status: str, now: str) -> dict | None:
    """Emit a media result notification from an open job-finish transaction."""
    if status != "completed":
        return None
    workspace_id = claim["workspace_id"]
    job = connection.execute(
        "SELECT id, user_id, work_id FROM jobs WHERE id = ? AND workspace_id = ?",
        (claim["id"], workspace_id),
    ).fetchone()
    if job is None:
        return None
    recipient = claim.get("user_id") or job["user_id"]
    render = connection.execute(
        "SELECT id, project_id, project_version FROM media_renders WHERE id = ? AND workspace_id = ?",
        (render_id, workspace_id),
    ).fetchone()
    version = int(render["project_version"]) if render is not None and render["project_version"] else 1
    work_id = job["work_id"]
    if render is not None:
        project = connection.execute(
            "SELECT work_id, title FROM media_projects WHERE id = ? AND workspace_id = ?",
            (render["project_id"], workspace_id),
        ).fetchone()
        if project is not None:
            work_id = project["work_id"] or work_id
    return emit_result_notification(
        connection,
        workspace_id=workspace_id,
        recipient_user_id=recipient,
        source_kind="media",
        source_ref={
            "kind": "media_render",
            "id": render_id,
            "revision": str(version),
            "version": version,
            "work_id": work_id,
        },
        title="媒体成片已完成",
        body=f"成片第 {version} 版已完成。",
        job_id=claim["id"],
        send_status="recorded",
        now=now,
    )


def register_notifications_routes(app) -> None:
    store = app.extensions["oevira.store"]

    @app.get("/api/notifications")
    def list_notifications():
        identity = g.identity
        unread_only = request.args.get("unread") in {"1", "true", "yes"}
        clauses = ["workspace_id = ?", "recipient_user_id = ?"]
        params: list = [identity["workspace"]["id"], identity["user"]["id"]]
        if unread_only:
            clauses.append("read_at IS NULL")
        with store.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM result_notifications WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT 200",
                tuple(params),
            ).fetchall()
            return _success([_notification_payload(connection, identity, row) for row in rows])

    @app.get("/api/notifications/<notification_id>")
    def get_notification(notification_id: str):
        identity = g.identity
        with store.transaction(immediate=False) as connection:
            row = _owned(connection, identity, notification_id)
            return _success(_notification_payload(connection, identity, row))

    @app.post("/api/notifications/<notification_id>/read")
    def mark_notification_read(notification_id: str):
        identity = g.identity
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, notification_id)
            if row["read_at"] is None:
                connection.execute(
                    "UPDATE result_notifications SET read_at = ?, updated_at = ? "
                    "WHERE id = ? AND workspace_id = ? AND read_at IS NULL",
                    (now, now, notification_id, identity["workspace"]["id"]),
                )
                row = _owned(connection, identity, notification_id)
            return _success(_notification_payload(connection, identity, row))

    @app.post("/api/notifications/<notification_id>/retry-delivery")
    def retry_notification_delivery(notification_id: str):
        identity = g.identity
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, notification_id)
            # Delivery is retried on its own. The originating job is never
            # queued, retried or otherwise mutated from this path.
            connection.execute(
                "UPDATE result_notifications SET delivery_attempts = delivery_attempts + 1, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (now, notification_id, identity["workspace"]["id"]),
            )
            updated = _owned(connection, identity, notification_id)
            return _success({"notification": _notification_payload(connection, identity, updated)})
