"""MethodApplication records on an Analysis/Artifact input snapshot.

A MethodApplication is not a second strategy platform. It records the exact
method and case version, the current problem, adopted mechanisms and declined
parts, conditions and counterexamples, target objects, source and rights, and
the actual parameters. The five states — recommended, previewed, adopted,
formal_result, independent_effect — are distinct. Returning a catalog id is
never enough to claim the method was used in a work. independent_effect does
not imply used_in_work and does not write into the artifact snapshot.
The recommended → previewed → adopted → formal_result sequence cannot be
skipped. Adopt requires a nonempty adopted_mechanisms list and writes the
full record into input_snapshot.method_applications on a new artifact version.
"""
from __future__ import annotations

import json
import uuid

from flask import g, request

from .api import (
    ApiError,
    _artifact_data,
    _artifact_row,
    _body,
    _idempotency_key,
    _not_found,
    _optional_string,
    _request_fingerprint,
    _require_write,
    _string,
    _success,
)
from .auth import utc_now
from .collaboration import require_artifact_access


STATES = ("recommended", "previewed", "adopted", "formal_result", "independent_effect")
USED_IN_WORK_STATES = frozenset({"adopted", "formal_result"})
# formal_result may only be reached along this path.
FORMAL_PATH = ("recommended", "previewed", "adopted", "formal_result")
_LIST_MAXIMUM = 200
_ITEM_MAXIMUM = 4000
_OBJECT_MAXIMUM = 8000

SCHEMA = """
CREATE TABLE IF NOT EXISTS method_applications (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    work_id TEXT REFERENCES works(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    state TEXT NOT NULL CHECK(state IN ('recommended', 'previewed', 'adopted', 'formal_result', 'independent_effect')),
    method_id TEXT NOT NULL,
    method_version TEXT NOT NULL,
    case_version TEXT,
    problem TEXT NOT NULL,
    adopted_mechanisms_json TEXT NOT NULL DEFAULT '[]',
    declined_parts_json TEXT NOT NULL DEFAULT '[]',
    conditions_json TEXT NOT NULL DEFAULT '[]',
    counterexamples_json TEXT NOT NULL DEFAULT '[]',
    target_objects_json TEXT NOT NULL DEFAULT '[]',
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    rights_json TEXT NOT NULL DEFAULT '{}',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    observation TEXT,
    artifact_version_at_adopt INTEGER,
    fingerprint TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS method_applications_idempotency
    ON method_applications(workspace_id, artifact_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS method_applications_artifact
    ON method_applications(workspace_id, artifact_id, created_at);
"""


def install_method_applications_schema(store) -> None:
    with store.transaction() as connection:
        connection.executescript(SCHEMA)


def _loads(value, default=None):
    if value is None or value == "":
        return [] if default is None else default
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return [] if default is None else default
    return loaded


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_list(value, field: str, *, item_type=(str, dict)):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError("INVALID_INPUT", f"{field} must be an array", 400, {"field": field})
    if len(value) > _LIST_MAXIMUM:
        raise ApiError("INVALID_INPUT", f"{field} has too many items", 400, {"field": field})
    items = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, item_type):
            raise ApiError("INVALID_INPUT", f"{field}[{index}] has an unsupported type", 400, {"field": field, "index": index})
        if isinstance(item, str):
            text = item.strip()
            if not text or len(text) > _ITEM_MAXIMUM:
                raise ApiError("INVALID_INPUT", f"{field}[{index}] is empty or too long", 400, {"field": field, "index": index})
            items.append(text)
        else:
            encoded = _dumps(item)
            if len(encoded) > _OBJECT_MAXIMUM:
                raise ApiError("INVALID_INPUT", f"{field}[{index}] is too large", 400, {"field": field, "index": index})
            items.append(item)
    return items


def _json_object(value, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or isinstance(value, bool):
        raise ApiError("INVALID_INPUT", f"{field} must be an object", 400, {"field": field})
    if len(_dumps(value)) > _OBJECT_MAXIMUM:
        raise ApiError("INVALID_INPUT", f"{field} is too large", 400, {"field": field})
    return value


def _used_in_work(state: str) -> bool:
    return state in USED_IN_WORK_STATES


def _record(row) -> dict:
    state = row["state"]
    return {
        "id": row["id"],
        "artifact_id": row["artifact_id"],
        "work_id": row["work_id"],
        "state": state,
        "method_id": row["method_id"],
        "method_version": row["method_version"],
        "case_version": row["case_version"],
        "problem": row["problem"],
        "adopted_mechanisms": _loads(row["adopted_mechanisms_json"], []),
        "declined_parts": _loads(row["declined_parts_json"], []),
        "conditions": _loads(row["conditions_json"], []),
        "counterexamples": _loads(row["counterexamples_json"], []),
        "target_objects": _loads(row["target_objects_json"], []),
        "source_refs": _loads(row["source_refs_json"], []),
        "rights": _loads(row["rights_json"], {}),
        "parameters": _loads(row["parameters_json"], {}),
        "observation": row["observation"],
        "used_in_work": _used_in_work(state),
        "artifact_version": row["artifact_version_at_adopt"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _snapshot_record(record: dict) -> dict:
    """Full snapshot entry. A catalog id alone is not a use-in-work claim."""
    return {
        "id": record["id"],
        "state": record["state"],
        "method_id": record["method_id"],
        "method_version": record["method_version"],
        "case_version": record["case_version"],
        "problem": record["problem"],
        "adopted_mechanisms": record["adopted_mechanisms"],
        "declined_parts": record["declined_parts"],
        "conditions": record["conditions"],
        "counterexamples": record["counterexamples"],
        "target_objects": record["target_objects"],
        "source_refs": record["source_refs"],
        "rights": record["rights"],
        "parameters": record["parameters"],
        "used_in_work": record["used_in_work"],
    }


def _owned(connection, identity: dict, artifact_id: str, application_id: str, *, write: bool = False):
    require_artifact_access(connection, identity, artifact_id, write=write)
    row = connection.execute(
        "SELECT * FROM method_applications WHERE id = ? AND workspace_id = ? AND artifact_id = ?",
        (application_id, identity["workspace"]["id"], artifact_id),
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


def _state_error(message: str) -> ApiError:
    return ApiError("METHOD_APPLICATION_STATE", message, 409)


def _write_snapshot(connection, identity: dict, artifact_id: str, record: dict, *, base_version: int, now: str, note: str) -> dict:
    workspace_id = identity["workspace"]["id"]
    artifact = _artifact_row(connection, workspace_id, artifact_id)
    if artifact is None:
        raise _not_found()
    if artifact["current_version"] != base_version:
        raise ApiError(
            "VERSION_CONFLICT",
            "Artifact has changed since it was opened",
            409,
            {"current": _artifact_data(connection, artifact)},
        )
    current = connection.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
        (artifact_id, workspace_id, base_version),
    ).fetchone()
    if current is None:
        raise _not_found()
    try:
        snapshot = json.loads(current["input_snapshot_json"] or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    applications = [
        item for item in (snapshot.get("method_applications") or [])
        if isinstance(item, dict) and item.get("id") != record["id"]
    ]
    applications.append(_snapshot_record(record))
    snapshot["method_applications"] = applications
    new_version = base_version + 1
    connection.execute(
        "INSERT INTO artifact_versions("
        "artifact_id, workspace_id, version, title, body, citations_json, "
        "input_snapshot_json, revision_note, created_at, created_by, parent_version"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id, workspace_id, new_version, current["title"], current["body"],
            current["citations_json"], _dumps(snapshot), note, now,
            identity["user"]["id"], base_version,
        ),
    )
    connection.execute(
        "UPDATE artifacts SET current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
        (new_version, now, artifact_id, workspace_id),
    )
    connection.execute(
        "UPDATE method_applications SET artifact_version_at_adopt = ?, updated_at = ? "
        "WHERE id = ? AND workspace_id = ?",
        (new_version, now, record["id"], workspace_id),
    )
    artifact = _artifact_row(connection, workspace_id, artifact_id)
    return _artifact_data(connection, artifact)


def _base_version(data: dict) -> int:
    value = data.get("base_artifact_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError("INVALID_INPUT", "base_artifact_version must be a positive integer", 400, {"field": "base_artifact_version"})
    return value


def register_method_application_routes(app) -> None:
    store = app.extensions["oevira.store"]

    @app.post("/api/artifacts/<artifact_id>/method-applications")
    def create_method_application(artifact_id: str):
        identity = _require_write()
        data = _body()
        allowed = {
            "method_id", "method_version", "case_version", "problem",
            "adopted_mechanisms", "declined_parts", "conditions", "counterexamples",
            "target_objects", "source_refs", "rights", "parameters",
        }
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Method application contains an unsupported field", 400)
        if "used_in_work" in data:
            raise ApiError("INVALID_INPUT", "used_in_work is computed and cannot be set by the client", 400)
        method_id = _string(data.get("method_id"), "method_id", maximum=200)
        method_version = _string(data.get("method_version"), "method_version", maximum=200)
        problem = _string(data.get("problem"), "problem", maximum=_ITEM_MAXIMUM)
        case_version = _optional_string(data.get("case_version"), "case_version", maximum=200)
        mechanisms = _json_list(data.get("adopted_mechanisms"), "adopted_mechanisms")
        declined = _json_list(data.get("declined_parts"), "declined_parts")
        conditions = _json_list(data.get("conditions"), "conditions")
        counterexamples = _json_list(data.get("counterexamples"), "counterexamples")
        targets = _json_list(data.get("target_objects"), "target_objects")
        source_refs = _json_list(data.get("source_refs"), "source_refs", item_type=(dict,))
        rights = _json_object(data.get("rights"), "rights")
        parameters = _json_object(data.get("parameters"), "parameters")
        key = _idempotency_key()
        fingerprint = _request_fingerprint({
            "method_id": method_id, "method_version": method_version,
            "case_version": case_version, "problem": problem,
            "adopted_mechanisms": mechanisms, "declined_parts": declined,
            "conditions": conditions, "counterexamples": counterexamples,
            "target_objects": targets, "source_refs": source_refs,
            "rights": rights, "parameters": parameters,
        })
        now = utc_now()
        application_id = str(uuid.uuid4())
        with store.transaction() as connection:
            artifact = require_artifact_access(connection, identity, artifact_id, write=True)
            if key:
                existing = connection.execute(
                    "SELECT * FROM method_applications WHERE workspace_id = ? AND artifact_id = ? AND idempotency_key = ?",
                    (identity["workspace"]["id"], artifact_id, key),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise ApiError("IDEMPOTENCY_CONFLICT", "The same key was used with different inputs", 409)
                    return _success(_record(existing), 200)
            connection.execute(
                "INSERT INTO method_applications("
                "id, workspace_id, artifact_id, work_id, owner_user_id, state, "
                "method_id, method_version, case_version, problem, "
                "adopted_mechanisms_json, declined_parts_json, conditions_json, "
                "counterexamples_json, target_objects_json, source_refs_json, "
                "rights_json, parameters_json, observation, artifact_version_at_adopt, "
                "fingerprint, idempotency_key, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, 'recommended', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    application_id, identity["workspace"]["id"], artifact_id,
                    artifact["work_id"], identity["user"]["id"],
                    method_id, method_version, case_version, problem,
                    _dumps(mechanisms), _dumps(declined), _dumps(conditions),
                    _dumps(counterexamples), _dumps(targets), _dumps(source_refs),
                    _dumps(rights), _dumps(parameters),
                    fingerprint, key, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM method_applications WHERE id = ?", (application_id,)
            ).fetchone()
            return _success(_record(row), 201)

    @app.get("/api/artifacts/<artifact_id>/method-applications")
    def list_method_applications(artifact_id: str):
        identity = g.identity
        with store.transaction(immediate=False) as connection:
            require_artifact_access(connection, identity, artifact_id, write=False)
            rows = connection.execute(
                "SELECT * FROM method_applications WHERE workspace_id = ? AND artifact_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (identity["workspace"]["id"], artifact_id),
            ).fetchall()
            return _success([_record(row) for row in rows])

    @app.get("/api/artifacts/<artifact_id>/method-applications/<application_id>")
    def get_method_application(artifact_id: str, application_id: str):
        identity = g.identity
        with store.transaction(immediate=False) as connection:
            row = _owned(connection, identity, artifact_id, application_id)
            return _success(_record(row))

    @app.post("/api/artifacts/<artifact_id>/method-applications/<application_id>/preview")
    def preview_method_application(artifact_id: str, application_id: str):
        identity = _require_write()
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            state = row["state"]
            if state == "previewed":
                return _success(_record(row))
            if state != "recommended":
                raise _state_error("A method application can only be previewed from the recommended state")
            connection.execute(
                "UPDATE method_applications SET state = 'previewed', updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND state = 'recommended'",
                (now, application_id, identity["workspace"]["id"]),
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            return _success(_record(row))

    @app.post("/api/artifacts/<artifact_id>/method-applications/<application_id>/adopt")
    def adopt_method_application(artifact_id: str, application_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"base_artifact_version", "adopted_mechanisms", "declined_parts"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Adopt contains an unsupported field", 400)
        base_version = _base_version(data)
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            if row["state"] in USED_IN_WORK_STATES:
                artifact = _artifact_row(connection, identity["workspace"]["id"], artifact_id)
                return _success({"application": _record(row), "artifact": _artifact_data(connection, artifact)})
            if row["state"] != "previewed":
                raise _state_error("A method must be previewed before it can be adopted into a work")
            mechanisms = _json_list(
                data["adopted_mechanisms"] if "adopted_mechanisms" in data else _loads(row["adopted_mechanisms_json"], []),
                "adopted_mechanisms",
            )
            if not mechanisms:
                raise ApiError(
                    "INVALID_INPUT",
                    "adopted_mechanisms must name at least one mechanism actually adopted",
                    400,
                    {"field": "adopted_mechanisms"},
                )
            declined = _json_list(
                data["declined_parts"] if "declined_parts" in data else _loads(row["declined_parts_json"], []),
                "declined_parts",
            )
            connection.execute(
                "UPDATE method_applications SET state = 'adopted', adopted_mechanisms_json = ?, "
                "declined_parts_json = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND state = 'previewed'",
                (_dumps(mechanisms), _dumps(declined), now, application_id, identity["workspace"]["id"]),
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            artifact = _write_snapshot(
                connection, identity, artifact_id, _record(row),
                base_version=base_version, now=now, note="采用方法到输入快照",
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            return _success({"application": _record(row), "artifact": artifact})

    @app.post("/api/artifacts/<artifact_id>/method-applications/<application_id>/formal-result")
    def formal_result_method_application(artifact_id: str, application_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"base_artifact_version"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Formal result contains an unsupported field", 400)
        base_version = _base_version(data)
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            if row["state"] == "formal_result":
                artifact = _artifact_row(connection, identity["workspace"]["id"], artifact_id)
                return _success({"application": _record(row), "artifact": _artifact_data(connection, artifact)})
            if row["state"] != "adopted":
                raise _state_error(
                    "A formal result can only follow an adopted method; "
                    "the recommended-to-formal-result sequence cannot be skipped"
                )
            connection.execute(
                "UPDATE method_applications SET state = 'formal_result', updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND state = 'adopted'",
                (now, application_id, identity["workspace"]["id"]),
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            artifact = _write_snapshot(
                connection, identity, artifact_id, _record(row),
                base_version=base_version, now=now, note="方法正式成果写入输入快照",
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            return _success({"application": _record(row), "artifact": artifact})

    @app.post("/api/artifacts/<artifact_id>/method-applications/<application_id>/independent-effect")
    def independent_effect_method_application(artifact_id: str, application_id: str):
        identity = _require_write()
        data = _body() if request.data else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            data = {}
        allowed = {"observation"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Independent effect contains an unsupported field", 400)
        observation = _optional_string(data.get("observation"), "observation", maximum=_ITEM_MAXIMUM)
        now = utc_now()
        with store.transaction() as connection:
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            if row["state"] == "independent_effect":
                return _success(_record(row))
            if row["state"] not in {"recommended", "previewed"}:
                raise _state_error(
                    "An independent effect cannot be recorded after the method has been adopted into a work, "
                    "and it does not become a formal result"
                )
            connection.execute(
                "UPDATE method_applications SET state = 'independent_effect', observation = COALESCE(?, observation), updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND state IN ('recommended', 'previewed')",
                (observation, now, application_id, identity["workspace"]["id"]),
            )
            row = _owned(connection, identity, artifact_id, application_id, write=True)
            return _success(_record(row))
