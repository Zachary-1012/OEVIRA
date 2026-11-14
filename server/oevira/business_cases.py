"""Service lifecycle on the existing Business Case and immutable Case versions."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import sqlite3
import uuid

from flask import g

from .api import ApiError, _body, _idempotency_key, _not_found, _request_fingerprint, _string, _success
from .auth import utc_now
from .collaboration import require_artifact_access, require_material_access, require_work_access, require_workspace_access, work_commission


def install_case_lifecycle_schema(store):
    connection = store.connect()
    try:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(business_cases)")}
        if columns["subject_id"]["notnull"] or columns["subject_version_at_link"]["notnull"]:
            _allow_independent_case(store, connection)
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(business_cases)")}
        if "work_id" not in columns:
            connection.execute("ALTER TABLE business_cases ADD COLUMN work_id TEXT REFERENCES works(id)")
        connection.execute("CREATE INDEX IF NOT EXISTS business_cases_work ON business_cases(workspace_id,work_id,updated_at)")
        connection.execute("CREATE TABLE IF NOT EXISTS business_case_events (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES business_cases(id),workspace_id TEXT NOT NULL REFERENCES workspaces(id),case_version INTEGER NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,occurred_at TEXT,recorded_at TEXT NOT NULL,recorded_by TEXT NOT NULL REFERENCES users(id),UNIQUE(case_id,case_version))")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _allow_independent_case(store, connection):
    """Rebuild Case context under a write lock and preserve a consistent recovery point."""
    # Lock before reading the rows to compare with the rebuilt table. Otherwise
    # a concurrent commit between the preflight snapshot and BEGIN IMMEDIATE
    # makes a healthy database look like a failed migration.
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    current_columns = {
        row["name"]: row for row in connection.execute("PRAGMA table_info(business_cases)")
    }
    if (current_columns.get("subject_id") is not None
            and current_columns["subject_id"]["notnull"] == 0
            and current_columns.get("subject_version_at_link") is not None
            and current_columns["subject_version_at_link"]["notnull"] == 0):
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        return
    original_sql = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_cases'").fetchone()[0]
    revised_sql = re.sub(r"CREATE TABLE\s+business_cases", "CREATE TABLE business_cases_context_migration", original_sql, count=1, flags=re.I)
    revised_sql = re.sub(r"subject_id\s+TEXT\s+NOT NULL", "subject_id TEXT", revised_sql, count=1, flags=re.I)
    revised_sql = re.sub(r"subject_version_at_link\s+INTEGER\s+NOT NULL", "subject_version_at_link INTEGER", revised_sql, count=1, flags=re.I)
    if revised_sql == original_sql or "CREATE TABLE business_cases_context_migration" not in revised_sql:
        raise RuntimeError("Business Case schema requires an explicit compatible migration")
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='business_cases_context_migration'").fetchone():
        raise RuntimeError("Unexpected Case migration table was preserved for reconciliation")
    if connection.execute("SELECT 1 FROM business_cases LIMIT 1").fetchone():
        recovery = store.root / "recovery"
        recovery.mkdir(parents=True, exist_ok=True)
        backup_path = recovery / f"before-case-context-{uuid.uuid4().hex}.sqlite3"
        source = store.connect()
        try:
            backup = sqlite3.connect(backup_path)
            try:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Case migration recovery snapshot could not be verified")
            finally:
                backup.close()
        finally:
            source.close()
    original_columns = [row["name"] for row in connection.execute("PRAGMA table_info(business_cases)")]
    quoted = ",".join('"' + name.replace('"', '""') + '"' for name in original_columns)
    indexes = [row[0] for row in connection.execute("SELECT sql FROM sqlite_schema WHERE tbl_name='business_cases' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    views = [dict(row) for row in connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='view' AND sql IS NOT NULL") if re.search(r"\bbusiness_cases\b", row["sql"], re.I)]
    before = [tuple(row) for row in connection.execute(f"SELECT {quoted} FROM business_cases ORDER BY id")]
    original_violations = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
    try:
        for view in views:
            connection.execute('DROP VIEW "' + view["name"].replace('"', '""') + '"')
        connection.execute(revised_sql)
        connection.execute(f"INSERT INTO business_cases_context_migration({quoted}) SELECT {quoted} FROM business_cases")
        connection.execute("DROP TABLE business_cases")
        connection.execute("ALTER TABLE business_cases_context_migration RENAME TO business_cases")
        for sql in indexes:
            connection.execute(sql)
        for view in views:
            connection.execute(view["sql"])
        after = [tuple(row) for row in connection.execute(f"SELECT {quoted} FROM business_cases ORDER BY id")]
        if before != after or {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")} - original_violations:
            raise RuntimeError("Case migration changed an original record or introduced an invalid reference")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _time(value, field):
    value = _string(value, field, maximum=60)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_INPUT", f"{field} must be an ISO timestamp", 400) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError("INVALID_INPUT", f"{field} needs an explicit timezone", 400)
    return value


def require_case_access(connection, identity, case_id, *, write=False, control=False):
    case_id = _string(case_id, "case_id", maximum=200)
    require_workspace_access(connection, identity, write=write or control)
    row = connection.execute("SELECT * FROM business_cases WHERE id=? AND workspace_id=?", (case_id, identity["workspace"]["id"])).fetchone()
    if row is None:
        raise _not_found()
    if row["work_id"]:
        require_work_access(connection, identity, row["work_id"], write=write and not control)
        if control:
            scope = work_commission(connection, row["work_id"])
            if scope:
                member = connection.execute("SELECT role FROM commission_members WHERE commission_id=? AND user_id=? AND active=1", (scope, identity["user"]["id"])).fetchone()
                if member is None or member["role"] not in {"owner", "editor"}:
                    raise ApiError("FORBIDDEN", "Your client role cannot change this service record", 403)
    return row


def require_case_event(connection, identity, case_id, event_id, case_version=None):
    """Return an immutable service event only while the caller can access its Case."""
    case = require_case_access(connection, identity, case_id)
    event_id = _string(event_id, "event_id", maximum=200)
    if case_version is not None and (type(case_version) is not int or case_version < 1):
        raise ApiError("INVALID_INPUT", "case_version must be a positive integer", 400)
    query = (
        "SELECT id,case_id,workspace_id,case_version,kind,payload_json,occurred_at,recorded_at,recorded_by "
        "FROM business_case_events WHERE id=? AND case_id=? AND workspace_id=?"
    )
    parameters = [event_id, case_id, identity["workspace"]["id"]]
    if case_version is not None:
        query += " AND case_version=?"
        parameters.append(case_version)
    event = connection.execute(query, tuple(parameters)).fetchone()
    if event is None:
        raise _not_found()
    version_row = connection.execute(
        "SELECT snapshot_json FROM business_case_versions WHERE case_id=? AND workspace_id=? AND version=?",
        (case_id, identity["workspace"]["id"], event["case_version"]),
    ).fetchone()
    if version_row is None:
        raise ApiError("CASE_EVENT_CONTEXT_MISSING", "The saved Case version for this event is unavailable", 409)
    snapshot = json.loads(version_row["snapshot_json"])
    if snapshot.get("id") != case_id or snapshot.get("version") != event["case_version"]:
        raise ApiError("CASE_EVENT_CONTEXT_MISMATCH", "The saved event and Case version do not match", 409)
    case_context_at_event = {
        "case_id": case_id,
        "case_version": event["case_version"],
        "title": snapshot.get("title"),
        "question": snapshot.get("question"),
        "state": snapshot.get("state"),
        "work_id": snapshot.get("work_id"),
        "subject_id": snapshot.get("subject_id"),
        "subject_version_at_link": snapshot.get("subject_version_at_link"),
        "subject_identity_review_status": snapshot.get("subject_identity_review_status"),
    }
    return case, {
        "id": event["id"],
        "case_id": event["case_id"],
        "case_version": event["case_version"],
        "kind": event["kind"],
        "payload": json.loads(event["payload_json"]),
        "occurred_at": event["occurred_at"],
        "recorded_at": event["recorded_at"],
        "recorded_by": event["recorded_by"],
        "case_context_at_event": case_context_at_event,
    }


def case_lifecycle(connection, row, *, version=None):
    events = connection.execute("SELECT * FROM business_case_events WHERE case_id=? AND workspace_id=? AND case_version<=? ORDER BY case_version,id", (row["id"], row["workspace_id"], version or row["version"])).fetchall()
    projection = {"assignment": None, "acknowledged": False, "response": None, "commitments": [], "feedback": [], "resolution": None, "source_review": None, "recurrence_count": 0, "related_cases": [], "problem_revisions": [], "events": []}
    promises = {}
    for event in events:
        payload = json.loads(event["payload_json"])
        entry = {"id": event["id"], "kind": event["kind"], "case_version": event["case_version"], "occurred_at": event["occurred_at"], "recorded_at": event["recorded_at"], "recorded_by": event["recorded_by"], "payload": payload}
        projection["events"].append(entry)
        if event["kind"] == "acknowledge":
            projection["acknowledged"] = True
        elif event["kind"] == "assign":
            projection["assignment"] = payload
        elif event["kind"] in {"prepare_response", "record_response"}:
            projection["response"] = payload
        elif event["kind"] == "promise":
            promises[event["id"]] = {"id": event["id"], **payload, "state": "promised", "fulfilments": []}
        elif event["kind"] == "record_fulfilment":
            promise = promises.get(payload["commitment_id"])
            if promise:
                promise["state"] = "reported_fulfilled"
                promise["fulfilments"].append(entry)
        elif event["kind"] == "feedback":
            projection["feedback"].append(entry)
        elif event["kind"] == "resolve":
            projection["resolution"] = payload
        elif event["kind"] == "revise_problem":
            projection["problem_revisions"].append(entry)
            projection["resolution"] = None
            projection["response"] = None
            projection["source_review"] = {
                "status": "needs_review", "event_id": event["id"],
                "note": "The Case problem changed; review the current source references again.",
            }
        elif event["kind"] == "replace_sources":
            projection["source_review"] = {
                "status": "needs_review", "event_id": event["id"],
                "note": payload.get("note"),
            }
        elif event["kind"] == "review_sources":
            projection["source_review"] = {
                "status": payload["decision"], "event_id": event["id"],
                "accepted_positions": payload["accepted_positions"],
                "note": payload.get("note"),
                "performed_by_user": True,
                "material_integrity_checked": True,
            }
        elif event["kind"] == "reopen":
            projection["recurrence_count"] += int(payload["reason_kind"] == "recurrence")
            projection["resolution"] = None
        elif event["kind"] == "link_case":
            projection["related_cases"].append({"event_id": event["id"], **payload})
        elif event["kind"] == "unlink_case":
            projection["related_cases"] = [item for item in projection["related_cases"] if item["event_id"] != payload["link_event_id"]]
    projection["commitments"] = list(promises.values())
    return projection


def _source_refs(connection, identity, work_id, value):
    from .api import _validate_citations
    if not isinstance(value, list) or len(value) > 5000:
        raise ApiError("INVALID_INPUT", "Evidence references must be an array", 400)
    refs = []
    for reference in value:
        if not isinstance(reference, dict) or not isinstance(reference.get("material_id"), str):
            raise ApiError("INVALID_INPUT", "Select saved source evidence", 400)
        if set(reference) - {"material_id", "source_native_id", "segment_id", "quote"}:
            raise ApiError("INVALID_INPUT", "A Case source contains an unsupported field", 400)
        material = require_material_access(connection, identity, reference["material_id"], target_work_id=work_id)
        if work_id is None and material["work_id"] is not None:
            raise ApiError("CASE_WORK_REQUIRED", "Keep Work-scoped source material inside an authorized Work", 403)
        selected = {"material_id": material["id"], "sha256": material["sha256"], "title": material["title"]}
        if reference.get("source_native_id") is not None:
            selected["source_native_id"] = _string(reference["source_native_id"], "source_native_id", maximum=300)
        if "segment_id" in reference or "quote" in reference:
            citation = _validate_citations(connection, identity["workspace"]["id"], [{key: reference.get(key) for key in ("material_id", "segment_id", "quote")}])[0]
            selected.update(citation)
        refs.append(selected)
    return refs


def _case_artifact(connection, identity, case, payload):
    artifact_id = _string(payload.get("artifact_id"), "artifact_id", maximum=200)
    artifact = require_artifact_access(connection, identity, artifact_id)
    if artifact["work_id"] != case["work_id"]:
        raise ApiError("CASE_ARTIFACT_SCOPE_MISMATCH", "Use a result from this Case's work", 403)
    version = payload.get("artifact_version")
    if type(version) is not int or version < 1:
        raise ApiError("INVALID_INPUT", "Select an exact saved result version", 400)
    source = connection.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
        (artifact_id, identity["workspace"]["id"], version),
    ).fetchone()
    if source is None:
        raise _not_found()
    text = source["body"]
    if payload.get("section_id"):
        snapshot = json.loads(source["input_snapshot_json"])
        sections = snapshot.get("business", {}).get("result", {}).get("sections", [])
        matches = [item for item in sections if item["id"] == payload["section_id"]]
        if len(matches) != 1:
            raise ApiError("CASE_RESPONSE_SECTION_MISSING", "The response section is not present in that exact saved version", 409)
        text = matches[0]["body"]
    return {"artifact_id": artifact_id, "artifact_version": version, "section_id": payload.get("section_id"), "title": source["title"], "body": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def register_case_lifecycle_routes(app):
    if "business_case_command" in app.view_functions:
        return
    store = app.extensions["oevira.store"]

    @app.post("/api/business/cases/<case_id>/events")
    def business_case_command(case_id):
        from .business import _case_data, _case_source_rows, _source_reference_url, _write_case_version
        data, identity = _body(), g.identity
        if set(data) != {"base_version", "command", "payload"} or type(data["base_version"]) is not int or data["base_version"] < 1 or not isinstance(data["payload"], dict):
            raise ApiError("INVALID_INPUT", "Provide the Case version, a service action and its details", 400)
        command = data["command"]
        allowed = {"acknowledge", "assign", "investigate", "prepare_response", "record_response", "promise", "record_fulfilment", "feedback", "resolve", "close", "reopen", "link_case", "unlink_case", "review_sources", "replace_sources"}
        if not isinstance(command, str) or command not in allowed:
            raise ApiError("INVALID_CASE_ACTION", "This service action is not supported", 400)
        fields = {
            "acknowledge": {"note"}, "assign": {"assignee_user_id", "due_at", "note"}, "investigate": {"note"},
            "prepare_response": {"artifact_id", "artifact_version", "section_id", "channel"},
            "record_response": {"state", "note"}, "promise": {"deliverable", "responsible", "responsible_user_id", "fulfilment_basis", "due_at"},
            "record_fulfilment": {"commitment_id", "note"}, "feedback": {"note"},
            "resolve": {"assessment", "observation_end", "basis_event_ids"}, "close": {"note", "unresolved_followup"},
            "reopen": {"note", "reason_kind"}, "link_case": {"note", "related_case_id", "relation"}, "unlink_case": {"note", "link_event_id"},
            "review_sources": {"decision", "accepted_positions", "note"},
            "replace_sources": {"source_refs", "note"},
        }
        if set(data["payload"]) - fields[command] - {"source_refs", "occurred_at"}:
            raise ApiError("INVALID_INPUT", "The service action contains an unsupported field", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Service actions require an idempotency key", 400)
        fingerprint = _request_fingerprint({"case_id": case_id, **data})
        with store.transaction() as connection:
            case = require_case_access(connection, identity, case_id)
            prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='business.case.event' AND key=?", (identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another service action", 409)
                saved_version = int(prior["resource_id"])
                historical = connection.execute("SELECT snapshot_json FROM business_case_versions WHERE case_id=? AND version=?", (case_id, saved_version)).fetchone()
                if historical is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The saved service action version is unavailable", 409)
                return _success({"case": json.loads(historical[0]), "replayed": True})
            case = require_case_access(connection, identity, case_id, write=command not in {"close", "unlink_case"}, control=command in {"close", "unlink_case"})
            if case["version"] != data["base_version"]:
                raise ApiError("VERSION_CONFLICT", "The service Case has changed", 409, {"current": _case_data(connection, case)})
            lifecycle = case_lifecycle(connection, case)
            payload = dict(data["payload"])
            problem_sha256 = hashlib.sha256(case["question"].encode("utf-8")).hexdigest()
            payload["case_question_sha256"] = problem_sha256
            now = utc_now()
            occurred_value = payload.pop("occurred_at", None)
            occurred = _time(occurred_value, "occurred_at") if occurred_value is not None else now if command in {"acknowledge", "assign", "prepare_response", "close", "reopen", "link_case", "unlink_case", "resolve"} else None
            payload["source_refs"] = _source_refs(connection, identity, case["work_id"], payload.get("source_refs", []))
            payload["provenance"] = "user_recorded_service_action"
            state = case["state"]
            if state == "closed" and command not in {"reopen", "feedback", "link_case", "unlink_case"}:
                raise ApiError("CASE_CLOSED", "Reopen this Case before adding new service work", 409)
            if command in {"acknowledge", "investigate", "feedback", "close", "reopen", "link_case", "unlink_case", "replace_sources", "review_sources"}:
                payload["note"] = _string(payload.get("note"), "note", maximum=10000)
            if command == "assign":
                assignee = _string(payload.get("assignee_user_id"), "assignee_user_id", maximum=80)
                member = connection.execute("SELECT u.name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.user_id=? AND m.active=1", (identity["workspace"]["id"], assignee)).fetchone()
                if member is None:
                    raise _not_found()
                if case["work_id"]:
                    require_work_access(
                        connection,
                        {"workspace": identity["workspace"], "user": {"id": assignee}, "role": member["role"]},
                        case["work_id"],
                    )
                payload["assignee_name"] = member["name"]
                if payload.get("due_at") is not None:
                    payload["due_at"] = _time(payload["due_at"], "due_at")
                payload["external_notification"] = "not_sent"
            elif command == "prepare_response":
                payload["response"] = _case_artifact(connection, identity, case, payload)
                payload["channel"] = _string(payload.get("channel"), "channel", maximum=300)
                payload["state"] = "draft_not_sent"
            elif command == "record_response":
                if lifecycle["response"] is None:
                    raise ApiError("CASE_RESPONSE_REQUIRED", "Save the exact response version before recording its outcome", 409)
                if not isinstance(payload.get("state"), str) or payload["state"] not in {"reported_sent", "delivery_unknown", "reported_failed", "reported_read"}:
                    raise ApiError("INVALID_INPUT", "Record sending, unknown delivery, failure and reading separately", 400)
                payload["note"] = _string(payload.get("note"), "response outcome", maximum=5000)
                previous_state = lifecycle["response"].get("state", "draft_not_sent")
                if previous_state == "reported_read" and payload["state"] != "reported_read":
                    raise ApiError("CASE_RESPONSE_STATE_CONFLICT", "A read receipt cannot be replaced by an earlier delivery state", 409)
                if payload["state"] == "reported_read" and previous_state not in {"reported_sent", "reported_read"}:
                    raise ApiError("CASE_RESPONSE_STATE_CONFLICT", "Record a sent response before recording that it was read", 409)
                if previous_state in {"delivery_unknown", "reported_failed"} and payload["state"] in {"reported_sent", "reported_read"} and not payload["source_refs"]:
                    raise ApiError("CASE_RESPONSE_RECEIPT_REQUIRED", "Reconcile an unknown or failed delivery with saved receipt evidence before recording success", 409)
                payload["response"] = lifecycle["response"]["response"]
                payload["business_outcome"] = "not_established_by_response"
            elif command == "promise":
                payload["deliverable"] = _string(payload.get("deliverable"), "deliverable", maximum=5000)
                if payload.get("responsible_user_id") is not None:
                    if "responsible" in payload:
                        raise ApiError("INVALID_INPUT", "Provide either a workspace member or a user-declared responsible party", 400)
                    responsible_id = _string(payload["responsible_user_id"], "responsible_user_id", maximum=80)
                    member = connection.execute(
                        "SELECT u.name,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.user_id=? AND m.active=1",
                        (identity["workspace"]["id"], responsible_id),
                    ).fetchone()
                    if member is None:
                        raise _not_found()
                    if case["work_id"]:
                        require_work_access(
                            connection,
                            {"workspace": identity["workspace"], "user": {"id": responsible_id}, "role": member["role"]},
                            case["work_id"],
                        )
                    payload["responsible"] = member["name"]
                    payload["responsible_user_id"] = responsible_id
                    payload["responsible_identity_state"] = "active_workspace_member"
                else:
                    payload["responsible"] = _string(payload.get("responsible"), "responsible", maximum=500)
                    payload["responsible_user_id"] = None
                    payload["responsible_identity_state"] = "user_declared"
                payload["fulfilment_basis"] = _string(payload.get("fulfilment_basis"), "fulfilment_basis", maximum=5000)
                if payload.get("due_at") is not None:
                    payload["due_at"] = _time(payload["due_at"], "due_at")
                payload["external_commitment_sent"] = False
            elif command == "record_fulfilment":
                promise = next((item for item in lifecycle["commitments"] if item["id"] == payload.get("commitment_id")), None)
                if promise is None:
                    raise ApiError("CASE_COMMITMENT_NOT_FOUND", "The promise does not belong to this Case", 409)
                payload["note"] = _string(payload.get("note"), "fulfilment record", maximum=5000)
                payload["fulfilment_verification"] = "user_reported_not_independently_verified"
            elif command == "replace_sources":
                if "source_refs" not in data["payload"]:
                    raise ApiError("SOURCE_REQUIRED", "Provide the complete replacement source list", 400)
                refs = payload["source_refs"]
                if len(refs) > 5000:
                    raise ApiError("INVALID_INPUT", "A Case can reference at most 5000 sources", 400)
                connection.execute("DELETE FROM business_case_sources WHERE case_id=? AND workspace_id=?", (case_id, identity["workspace"]["id"]))
                rows = []
                for position, reference in enumerate(refs):
                    material = require_material_access(
                        connection, identity, reference["material_id"], target_work_id=case["work_id"],
                    )
                    if case["work_id"] is None and material["work_id"] is not None:
                        raise ApiError("CASE_WORK_REQUIRED", "Keep Work-scoped source material inside an authorized Work", 403)
                    rows.append((
                        case_id, identity["workspace"]["id"], position, material["id"],
                        reference.get("source_native_id"), material["title"],
                        _source_reference_url(material["source_url"], "source_refs[].url"),
                        material["sha256"],
                    ))
                connection.executemany(
                    "INSERT INTO business_case_sources(case_id,workspace_id,position,material_id,source_native_id,historical_evidence_ref,source_title,source_url,source_sha256,link_status) VALUES(?,?,?,?,?,NULL,?,?,?,'material_linked_needs_review')",
                    rows,
                )
                payload["replaced_source_count"] = len(rows)
                payload["source_refs_status"] = "needs_review"
            elif command == "review_sources":
                decision = payload.get("decision")
                if decision not in {"linked", "partial", "needs_review"}:
                    raise ApiError("INVALID_INPUT", "Choose whether saved Case sources are linked, partial, or still need review", 400)
                accepted = payload.get("accepted_positions", [])
                if (not isinstance(accepted, list) or len(accepted) > 5000
                        or any(type(position) is not int or position < 0 for position in accepted)
                        or len(set(accepted)) != len(accepted)):
                    raise ApiError("INVALID_INPUT", "accepted_positions must be unique nonnegative source positions", 400)
                sources = _case_source_rows(connection, case_id, identity["workspace"]["id"])
                source_positions = {item["position"] for item in sources}
                if not set(accepted) <= source_positions:
                    raise ApiError("CASE_SOURCE_NOT_FOUND", "One or more selected source positions are unavailable", 404)
                if decision == "linked" and (not sources or set(accepted) != source_positions):
                    raise ApiError("CASE_SOURCE_REVIEW_INCOMPLETE", "Link all current sources before marking the Case source set linked", 409)
                if decision == "partial" and (not accepted or set(accepted) == source_positions):
                    raise ApiError("CASE_SOURCE_REVIEW_INCOMPLETE", "Partial source review needs at least one confirmed and one unconfirmed source", 409)
                if decision == "needs_review" and accepted:
                    raise ApiError("INVALID_INPUT", "A needs-review decision cannot confirm source positions", 400)
                verified = []
                for source in sources:
                    if source["position"] not in accepted:
                        continue
                    material = require_material_access(
                        connection, identity, source["material_id"], target_work_id=case["work_id"],
                    )
                    if case["work_id"] is None and material["work_id"] is not None:
                        raise ApiError("CASE_WORK_REQUIRED", "Keep Work-scoped source material inside an authorized Work", 403)
                    if material["sha256"] != source["source_sha256"] or material["status"] != "ready":
                        raise ApiError("CASE_SOURCE_CHANGED", "A selected source no longer matches the saved Case reference", 409)
                    source_path = (store.root / material["relative_path"]).resolve()
                    if not source_path.is_relative_to(store.root) or not source_path.is_file():
                        raise ApiError("CASE_SOURCE_UNAVAILABLE", "A selected original is not available in this workspace", 409)
                    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    if digest != source["source_sha256"]:
                        raise ApiError("CASE_SOURCE_CHANGED", "A selected original no longer matches its saved hash", 409)
                    verified.append({"position": source["position"], "material_id": material["id"], "sha256": digest})
                for source in sources:
                    status = "linked_by_user" if source["position"] in accepted else "material_linked_needs_review"
                    connection.execute(
                        "UPDATE business_case_sources SET link_status=? WHERE case_id=? AND workspace_id=? AND position=?",
                        (status, case_id, identity["workspace"]["id"], source["position"]),
                    )
                payload["decision"] = decision
                payload["accepted_positions"] = sorted(accepted)
                payload["verified_sources"] = verified
                payload["material_integrity_checked"] = bool(verified)
                payload["source_refs_status"] = decision
            elif command == "resolve":
                basis_ids = payload.get("basis_event_ids")
                if (not isinstance(basis_ids, list) or not basis_ids or len(basis_ids) > 100
                        or any(type(item) is not str or not item.strip() for item in basis_ids)):
                    raise ApiError("RESOLUTION_EVIDENCE_REQUIRED", "Select saved feedback or fulfilment events before assessing resolution", 400)
                basis_ids = list(dict.fromkeys(item.strip() for item in basis_ids))
                markers = ",".join("?" for _ in basis_ids)
                basis_rows = connection.execute(
                    f"SELECT id,kind,case_version,payload_json FROM business_case_events WHERE workspace_id=? AND case_id=? AND id IN ({markers})",
                    (identity["workspace"]["id"], case_id, *basis_ids),
                ).fetchall()
                problem_changed_after = max(
                    (event["case_version"] for event in connection.execute(
                        "SELECT case_version FROM business_case_events WHERE workspace_id=? AND case_id=? AND kind IN ('revise_problem','reopen')",
                        (identity["workspace"]["id"], case_id),
                    ).fetchall()),
                    default=1,
                )
                if (len(basis_rows) != len(basis_ids)
                        or any(item["kind"] not in {"feedback", "record_fulfilment"}
                               or item["case_version"] > case["version"]
                               or item["case_version"] <= problem_changed_after
                               or json.loads(item["payload_json"]).get("case_question_sha256") != problem_sha256
                               for item in basis_rows)):
                    raise ApiError("RESOLUTION_EVIDENCE_REQUIRED", "Resolution must cite feedback or reported fulfilment recorded after the latest problem revision or reopening", 400)
                payload["basis_event_ids"] = basis_ids
                payload["problem_sha256"] = problem_sha256
                payload["assessment"] = _string(payload.get("assessment"), "resolution assessment", maximum=5000)
                payload["observation_end"] = _time(payload.get("observation_end"), "observation_end")
                if datetime.fromisoformat(payload["observation_end"].replace("Z", "+00:00")) > datetime.fromisoformat(occurred.replace("Z", "+00:00")):
                    raise ApiError("OBSERVATION_WINDOW_NOT_MATURE", "The stated follow-up window has not ended at the assessment time", 409)
                payload["independent_verification"] = "not_established_by_user_assessment"
            elif command == "close":
                if "unresolved_followup" in payload:
                    payload["unresolved_followup"] = _string(
                        payload["unresolved_followup"], "unresolved_followup", maximum=5000,
                    )
                if any(item["state"] != "reported_fulfilled" for item in lifecycle["commitments"]) and not payload.get("unresolved_followup"):
                    raise ApiError("UNFULFILLED_COMMITMENTS", "Preserve the unfinished promises and their handoff before closing the Case", 409)
                payload["closure_means_resolution"] = False
                state = "closed"
            elif command == "reopen":
                if state != "closed" or not isinstance(payload.get("reason_kind"), str) or payload["reason_kind"] not in {"new_evidence", "recurrence", "unfinished_service", "correction"}:
                    raise ApiError("CASE_REOPEN_REASON_REQUIRED", "Reopening requires a closed Case and a concrete reason", 409)
                payload["prior_case_version"] = case["version"]
                state = "open"
            elif command == "link_case":
                related = require_case_access(connection, identity, payload.get("related_case_id"))
                if related["id"] == case_id or work_commission(connection, related["work_id"]) != work_commission(connection, case["work_id"]):
                    raise ApiError("CASE_RELATION_SCOPE_MISMATCH", "Link different Cases only within the same permitted client scope", 403)
                if not isinstance(payload.get("relation"), str) or payload["relation"] not in {"related", "possible_duplicate", "recurrence_of"}:
                    raise ApiError("INVALID_INPUT", "Select the actual Case relationship", 400)
                payload["related_case_version"] = related["version"]
                payload["related_case_title"] = related["title"]
                payload["actions_or_costs_merged"] = False
            elif command == "unlink_case":
                if payload.get("link_event_id") not in {item["event_id"] for item in lifecycle["related_cases"]}:
                    raise ApiError("CASE_RELATION_NOT_FOUND", "The selected relationship is not active on this Case", 409)
            event_id, version = str(uuid.uuid4()), case["version"] + 1
            connection.execute("INSERT INTO business_case_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id, case_id, identity["workspace"]["id"], version, command, json.dumps(payload, ensure_ascii=False, allow_nan=False), occurred, now, identity["user"]["id"]))
            source_refs_status = payload.get("source_refs_status", case["source_refs_status"])
            connection.execute("UPDATE business_cases SET state=?,source_refs_status=?,version=?,updated_at=? WHERE id=? AND version=?", (state, source_refs_status, version, now, case_id, case["version"]))
            updated = connection.execute("SELECT * FROM business_cases WHERE id=?", (case_id,)).fetchone()
            _write_case_version(connection, updated, command, identity["user"]["id"], now)
            connection.execute("INSERT INTO idempotency_records VALUES(?,'business.case.event',?,?,'business_case_version',?,?)", (identity["workspace"]["id"], key, fingerprint, str(version), now))
            return _success({"case": _case_data(connection, updated), "event_id": event_id, "replayed": False})
