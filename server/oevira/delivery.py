"""Version-frozen delivery batches over existing exports and media renders."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import uuid
import zipfile

from flask import g, request, send_file

from .api import ApiError, _body, _create_export, _idempotency_key, _job_data, _request_fingerprint, _resource_path, _string, _success
from .auth import utc_now
from .collaboration import require_artifact_access, require_material_access, require_work_access, work_commission


SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_batches (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT NOT NULL REFERENCES works(id),
    title TEXT NOT NULL,
    parent_batch_id TEXT REFERENCES delivery_batches(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS delivery_members (
    batch_id TEXT NOT NULL REFERENCES delivery_batches(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    member_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    reference_json TEXT NOT NULL,
    reference_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    file_json TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(batch_id,member_id),
    UNIQUE(batch_id,filename)
);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _filename(value: str, extension: str) -> str:
    value = _string(value, "filename", maximum=180)
    if (value in {".", ".."} or any(char in value for char in '/\\<>:"|?*')
            or any(ord(char) < 32 for char in value) or value.endswith((".", " "))
            or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value)):
        raise ApiError("INVALID_DELIVERY_FILENAME", "Use a single valid file name for each delivery member", 400)
    if PurePosixPath(value).suffix.lower() != "." + extension:
        raise ApiError("INVALID_DELIVERY_FILENAME", "The file name must match the selected output format", 400)
    return value


def _require_batch(connection, identity, batch_id):
    row = connection.execute("SELECT * FROM delivery_batches WHERE id=? AND workspace_id=?",
                             (batch_id, identity["workspace"]["id"])).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "The requested delivery was not found", 404)
    require_work_access(connection, identity, row["work_id"])
    return row


def _artifact_version(connection, identity, work_id, reference):
    artifact = require_artifact_access(connection, identity, reference["artifact_id"])
    source_scope = work_commission(connection, artifact["work_id"])
    if source_scope and source_scope != work_commission(connection, work_id):
        raise ApiError("CLIENT_SCOPE_MISMATCH", "This private result belongs to a different client", 403)
    version = connection.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
                                 (artifact["id"], identity["workspace"]["id"], reference["version"])).fetchone()
    if version is None:
        raise ApiError("NOT_FOUND", "The selected result version was not found", 404)
    snapshot = json.loads(version["input_snapshot_json"] or "{}")
    material_ids = {item.get("material_id") for item in json.loads(version["citations_json"]) if isinstance(item, dict)}
    material_ids.update(item.get("material_id") or item.get("id") for item in snapshot.get("materials", []) if isinstance(item, dict))
    for material_id in material_ids - {None}:
        require_material_access(connection, identity, material_id, target_work_id=work_id)
    return version


def _require_member(connection, identity, work_id, reference):
    if reference["kind"] == "artifact":
        return _artifact_version(connection, identity, work_id, reference)
    if reference["kind"] == "media_podcast_sidecars":
        projection = _sidecar_projection(connection, identity, work_id, reference)
        if any(reference.get(field) != projection[field] for field in projection):
            raise ApiError("DELIVERY_MEMBER_CHANGED", "The podcast attachments no longer match the selected audio and project versions", 409)
        return projection
    if reference["kind"] == "media_interchange":
        from .media_interchange import require_media_interchange_access

        render = require_media_interchange_access(connection, identity, reference["interchange_id"], target_work_id=work_id)
    else:
        from .media_api import require_media_delivery_access

        render = require_media_delivery_access(connection, identity, reference["render_id"], target_work_id=work_id)
    if any(reference.get(field) != render[field] for field in ("project_id", "project_version")):
        raise ApiError("DELIVERY_MEMBER_CHANGED", "The media result no longer matches the selected project version", 409)
    if reference["kind"] == "media_interchange" and any(reference.get(field) != render[field] for field in ("target_profile", "adapter_revision")):
        raise ApiError("DELIVERY_MEMBER_CHANGED", "The exchange result no longer matches the selected target profile", 409)
    return render


def _sidecar_projection(connection, identity, work_id, reference):
    from .media_api import require_media_episode_sidecars_access

    selected = require_media_episode_sidecars_access(connection, identity, reference["render_id"],
                                                   target_work_id=work_id, project_version=reference.get("project_version"))
    return {"project_id": selected["project"]["id"], "project_version": selected["sidecar_version"]["version"],
            "audio_project_version": selected["render"]["project_version"], "audio_sha256": selected["render"]["sha256"],
            "audio_projection_sha256": selected["audio_projection_sha256"]}


def _resolve_member(store, identity, work_id, reference, *, export_key=None):
    if reference["kind"] == "media_podcast_sidecars":
        from .api import _write_export_bytes
        from .media_api import resolve_media_episode_sidecars

        with store.transaction(immediate=False) as connection:
            projection = _require_member(connection, identity, work_id, reference)
        if export_key is None:
            return projection
        result = resolve_media_episode_sidecars(store, identity, reference["render_id"], target_work_id=work_id,
                                               project_version=reference["project_version"])
        if (result["project_id"] != projection["project_id"] or result["project_version"] != projection["project_version"]
                or result["audio_revision"]["project_version"] != projection["audio_project_version"]
                or result["audio_revision"]["sha256"] != projection["audio_sha256"]
                or result["audio_projection_sha256"] != projection["audio_projection_sha256"]):
            raise ApiError("DELIVERY_MEMBER_CHANGED", "The podcast attachments changed while they were being prepared", 409)
        content = result["content"]
        if hashlib.sha256(content).hexdigest() != result["sha256"] or len(content) != result["size_bytes"]:
            raise ApiError("DELIVERY_READBACK_FAILED", "The podcast attachment bytes did not match their receipt", 409)
        file_id = hashlib.sha256(export_key.encode("utf-8")).hexdigest()
        relative_path = f"objects/{identity['workspace']['id']}/deliveries/members/{file_id}.zip"
        _write_export_bytes(store, {"id": file_id, "relative_path": relative_path}, content)
        return {**{key: value for key, value in result.items() if key not in {"content", "filename"}}, "relative_path": relative_path}
    if reference["kind"] in {"media_render", "media_interchange"}:
        if reference["kind"] == "media_interchange":
            from .media_interchange import resolve_media_interchange_file

            resolved = resolve_media_interchange_file(store, identity, reference["interchange_id"], target_work_id=work_id)
        else:
            from .media_api import resolve_media_delivery_file

            resolved = resolve_media_delivery_file(store, identity, reference["render_id"], target_work_id=work_id)
        if any(reference.get(field, resolved[field]) != resolved[field] for field in ("project_id", "project_version")):
            raise ApiError("DELIVERY_MEMBER_CHANGED", "The media result no longer matches the selected project version", 409)
        if reference["kind"] == "media_interchange" and any(reference.get(field, resolved[field]) != resolved[field] for field in ("target_profile", "adapter_revision")):
            raise ApiError("DELIVERY_MEMBER_CHANGED", "The exchange result no longer matches the selected target profile", 409)
        return {**resolved, "relative_path": resolved["path"].relative_to(store.root).as_posix()}
    with store.transaction(immediate=False) as connection:
        version = _artifact_version(connection, identity, work_id, reference)
        from .business_execution import business_export_view

        business_export_view(version, reference["audience"])
    if export_key is None:
        return None
    response, _ = _create_export(store, reference["artifact_id"],
                                {"version": reference["version"], "format": reference["format"], "audience": reference["audience"]},
                                identity=identity, idempotency_key=export_key)
    exported = response.get_json()["data"]
    row = store.row("SELECT * FROM exports WHERE id=? AND workspace_id=?", (exported["id"], identity["workspace"]["id"]))
    return {"relative_path": row["relative_path"], "sha256": row["sha256"], "size_bytes": row["size_bytes"],
            "export_id": row["id"], "mime_type": exported["mime_type"]}


def _normalise_members(store, identity, work_id, members):
    if not isinstance(members, list) or not 1 <= len(members) <= 5000:
        raise ApiError("INVALID_INPUT", "Select between one and 5000 delivery members", 400)
    output, names, identities = [], set(), set()
    for position, member in enumerate(members):
        if not isinstance(member, dict):
            raise ApiError("INVALID_INPUT", "Each delivery member must be an object", 400)
        member_id = _string(member.get("member_id"), "member_id", maximum=100)
        kind = member.get("kind")
        if kind == "artifact":
            if set(member) - {"member_id", "kind", "artifact_id", "version", "format", "audience", "filename"}:
                raise ApiError("INVALID_INPUT", "Artifact delivery contains an unsupported field", 400)
            if type(member.get("version")) is not int or member["version"] < 1:
                raise ApiError("INVALID_INPUT", "Select a saved positive result version", 400)
            extension = member.get("format")
            if not isinstance(extension, str) or extension not in {"md", "docx", "pptx", "xlsx", "pdf", "html"}:
                raise ApiError("INVALID_INPUT", "Select a supported delivery format", 400)
            reference = {"kind": kind, "artifact_id": _string(member.get("artifact_id"), "artifact_id", maximum=100),
                         "version": member["version"], "format": extension, "audience": member.get("audience", "internal")}
            with store.transaction(immediate=False) as connection:
                version = _artifact_version(connection, identity, work_id, reference)
            if "audience" not in member and json.loads(version["input_snapshot_json"] or "{}").get("kind") == "business_analysis":
                reference["audience"] = "client"
            if not isinstance(reference["audience"], str) or reference["audience"] not in {"internal", "client"}:
                raise ApiError("INVALID_EXPORT_AUDIENCE", "Select the internal or client delivery view", 400)
        elif kind == "media_podcast_sidecars":
            if set(member) - {"member_id", "kind", "render_id", "project_version", "filename"}:
                raise ApiError("INVALID_INPUT", "Podcast attachments contain an unsupported field", 400)
            reference = {"kind": kind, "render_id": _string(member.get("render_id"), "render_id", maximum=100)}
            if "project_version" in member:
                if type(member["project_version"]) is not int or member["project_version"] < 1:
                    raise ApiError("INVALID_INPUT", "Select a positive podcast project version", 400)
                reference["project_version"] = member["project_version"]
            with store.transaction(immediate=False) as connection:
                reference.update(_sidecar_projection(connection, identity, work_id, reference))
            extension = "zip"
        elif kind in {"media_render", "media_interchange"}:
            identity_field = "render_id" if kind == "media_render" else "interchange_id"
            if set(member) != {"member_id", "kind", identity_field, "filename"}:
                raise ApiError("INVALID_INPUT", "A media member needs its exact rendered result and file name", 400)
            reference = {"kind": kind, identity_field: _string(member.get(identity_field), identity_field, maximum=100)}
            resolved = _resolve_member(store, identity, work_id, reference)
            extension = Path(resolved["filename"]).suffix.lower().lstrip(".")
            reference.update({"project_id": resolved["project_id"], "project_version": resolved["project_version"]})
            if kind == "media_interchange":
                reference.update({"target_profile": resolved["target_profile"], "adapter_revision": resolved["adapter_revision"]})
        else:
            raise ApiError("INVALID_INPUT", "Select an existing file result or final media render", 400)
        filename = _filename(member.get("filename"), extension)
        if filename.casefold() == "交付说明.txt" or filename.casefold() in names or member_id in identities:
            raise ApiError("DUPLICATE_DELIVERY_MEMBER", "Delivery members need distinct identities and file names", 400)
        names.add(filename.casefold())
        identities.add(member_id)
        output.append({"member_id": member_id, "ordinal": position, "reference": reference,
                       "reference_hash": _request_fingerprint(reference), "filename": filename})
    return output


def _batch_data(store, identity, batch_id):
    with store.transaction(immediate=False) as connection:
        row = _require_batch(connection, identity, batch_id)
        members = connection.execute("SELECT * FROM delivery_members WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
    return {"id": row["id"], "work_id": row["work_id"], "title": row["title"], "status": row["status"],
            "filename": f"{row['id']}.zip", "mime_type": "application/zip",
            "parent_batch_id": row["parent_batch_id"], "sha256": row["sha256"], "size_bytes": row["size_bytes"],
            "download_url": f"/api/deliveries/{batch_id}/file" if row["status"] == "completed" else None,
            "job": _job_data(job), "members": [
                {"member_id": item["member_id"], "reference": json.loads(item["reference_json"]),
                 "filename": item["filename"], "status": item["status"], "error_code": item["error_code"],
                 "file": {key: value for key, value in json.loads(item["file_json"]).items() if key != "relative_path"}
                 if item["file_json"] else None} for item in members]}


def register_delivery_routes(app, store):
    connection = store.connect()
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()
    app.extensions.setdefault("oevira.job_resume_validators", {})["delivery_batch"] = validate_delivery_resume

    @app.get("/api/works/<work_id>/deliveries")
    def list_deliveries(work_id):
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
                cursor = _require_batch(connection, g.identity, before)
                if cursor["work_id"] != work_id:
                    raise ApiError("INVALID_DELIVERY_CURSOR", "The cursor belongs to a different work", 400)
                predicate = " AND (created_at<? OR (created_at=? AND id<?))"
                parameters.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
            parameters.append(limit + 1)
            rows = connection.execute("SELECT id FROM delivery_batches WHERE workspace_id=? AND work_id=?" + predicate + " ORDER BY created_at DESC,id DESC LIMIT ?", parameters).fetchall()
        return _success({"items": [_batch_data(store, g.identity, row["id"]) for row in rows[:limit]],
                         "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    @app.post("/api/works/<work_id>/deliveries")
    def create_delivery(work_id):
        result = create_delivery_batch(store, g.identity, work_id, _body(), _idempotency_key())
        return _success(result, 202 if result["status"] == "preparing" else 200)

    @app.get("/api/deliveries/<batch_id>")
    def get_delivery(batch_id):
        return _success(_batch_data(store, g.identity, batch_id))

    @app.get("/api/deliveries/<batch_id>/file")
    def download_delivery(batch_id):
        with store.transaction(immediate=False) as connection:
            row = _require_batch(connection, g.identity, batch_id)
        if row["status"] != "completed":
            raise ApiError("DELIVERY_NOT_READY", "Some delivery members are not ready", 409)
        for member in store.rows("SELECT * FROM delivery_members WHERE batch_id=?", (batch_id,)):
            _resolve_member(store, g.identity, row["work_id"], json.loads(member["reference_json"]))
        path = _resource_path(store, row["relative_path"])
        if not path.is_file() or _file_digest(path) != (row["sha256"], row["size_bytes"]):
            raise ApiError("DELIVERY_INTEGRITY_ERROR", "The saved package failed verification and was preserved", 409)
        return send_file(path, as_attachment=True, download_name=f"{batch_id}.zip", mimetype="application/zip", conditional=False)


def create_delivery_batch(store, identity: dict, work_id: str, data: dict, idempotency_key: str) -> dict:
    if not isinstance(data, dict):
        raise ApiError("INVALID_INPUT", "Delivery input must be an object", 400)
    if set(data) - {"title", "members", "parent_batch_id"}:
        raise ApiError("INVALID_INPUT", "Delivery contains an unsupported field", 400)
    title = _string(data.get("title"), "title", maximum=300)
    key = _string(idempotency_key, "Idempotency-Key", maximum=200)
    fingerprint = _request_fingerprint({"work_id": work_id, "data": data})
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        require_work_access(connection, identity, work_id, write=True)
        prior = connection.execute("SELECT * FROM delivery_batches WHERE workspace_id=? AND idempotency_key=?", (workspace_id, key)).fetchone()
    if prior:
        if prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This delivery key was already used for a different selection", 409)
        return _batch_data(store, identity, prior["id"])
    members = _normalise_members(store, identity, work_id, data.get("members"))
    batch_id, job_id, now = str(uuid.uuid4()), str(uuid.uuid4()), utc_now()
    parent_id = data.get("parent_batch_id")
    if parent_id is not None:
        parent_id = _string(parent_id, "parent_batch_id", maximum=100)
    with store.transaction() as connection:
        require_work_access(connection, identity, work_id, write=True)
        prior = connection.execute("SELECT * FROM delivery_batches WHERE workspace_id=? AND idempotency_key=?", (workspace_id, key)).fetchone()
        if prior:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This delivery key was already used for a different selection", 409)
            batch_id = prior["id"]
        else:
            if parent_id:
                parent = _require_batch(connection, identity, parent_id)
                if parent["work_id"] != work_id:
                    raise ApiError("DELIVERY_WORK_MISMATCH", "Use the original work for the next delivery", 409)
            connection.execute("INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,'delivery_batch','queued',?,?,?,?,?)",
                               (job_id, workspace_id, identity["user"]["id"], work_id, _json({"batch_id": batch_id}), fingerprint, key, now, now))
            connection.execute("INSERT INTO delivery_batches(id,workspace_id,work_id,title,parent_batch_id,job_id,status,idempotency_key,request_hash,relative_path,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'preparing',?,?,?,?,?,?)",
                               (batch_id, workspace_id, work_id, title, parent_id, job_id, key, fingerprint,
                                f"objects/{workspace_id}/deliveries/{batch_id}.zip", identity["user"]["id"], now, now))
            for item in members:
                _require_member(connection, identity, work_id, item["reference"])
                reusable = connection.execute("SELECT file_json FROM delivery_members WHERE batch_id=? AND member_id=? AND reference_hash=? AND status='completed'",
                                              (parent_id, item["member_id"], item["reference_hash"])).fetchone() if parent_id else None
                connection.execute("INSERT INTO delivery_members VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   (batch_id, workspace_id, item["member_id"], item["ordinal"], _json(item["reference"]), item["reference_hash"], item["filename"],
                                    "completed" if reusable else "pending", reusable["file_json"] if reusable else None, None, now))
    return _batch_data(store, identity, batch_id)



def validate_delivery_resume(connection, identity, row, context):
    if context:
        raise ApiError("INVALID_INPUT", "Continue the original delivery; changed members belong in a new batch", 400)
    batch_id = json.loads(row["payload_json"])["batch_id"]
    batch = _require_batch(connection, identity, batch_id)
    if batch["status"] == "completed":
        raise ApiError("DELIVERY_ALREADY_COMPLETED", "Open the saved delivery instead of preparing it again", 409)
    for member in connection.execute("SELECT * FROM delivery_members WHERE batch_id=?", (batch_id,)):
        reference = json.loads(member["reference_json"])
        _require_member(connection, identity, batch["work_id"], reference)


def _check_running(store, claim):
    row = store.row("SELECT status,lease_generation FROM jobs WHERE id=?", (claim["id"],))
    if row is None or row["lease_generation"] != claim["generation"] or row["status"] != "running":
        raise ApiError("JOB_CONTROL_STOPPED", "Delivery preparation stopped at its saved boundary", 409)


def _verify_file(store, metadata):
    path = _resource_path(store, metadata["relative_path"])
    if not path.is_file() or _file_digest(path) != (metadata["sha256"], metadata["size_bytes"]):
        raise ApiError("DELIVERY_MEMBER_INTEGRITY_ERROR", "A saved member failed verification and was preserved", 409)
    return path


def _package(store, claim, batch, members, identity):
    target = _resource_path(store, batch["relative_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    total = sum(json.loads(item["file_json"])["size_bytes"] for item in members)
    if total + 64 * 1024 * 1024 > shutil.disk_usage(target.parent).free:
        raise ApiError("DELIVERY_STORAGE_UNAVAILABLE", "There is not enough free space to preserve and package all selected members", 409)
    descriptor, name = tempfile.mkstemp(prefix=batch["id"] + "-", suffix=".partial", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for item in members:
                _check_running(store, claim)
                reference, metadata = json.loads(item["reference_json"]), json.loads(item["file_json"])
                _resolve_member(store, identity, batch["work_id"], reference)
                source = _verify_file(store, metadata)
                digest, count, checked_at = hashlib.sha256(), 0, time.monotonic()
                with source.open("rb") as incoming, archive.open(zipfile.ZipInfo(item["filename"]), "w", force_zip64=True) as outgoing:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        if time.monotonic() - checked_at > 0.1:
                            _check_running(store, claim)
                            checked_at = time.monotonic()
                        outgoing.write(block)
                        digest.update(block)
                        count += len(block)
                if (digest.hexdigest(), count) != (metadata["sha256"], metadata["size_bytes"]):
                    raise ApiError("DELIVERY_MEMBER_CHANGED", "A member changed while being packaged", 409)
            note = batch["title"] + "\n\n" + "\n".join(item["filename"] for item in members)
            archive.writestr(zipfile.ZipInfo("交付说明.txt"), note.encode("utf-8"))
        with zipfile.ZipFile(temporary) as archive:
            for item in members:
                expected = json.loads(item["file_json"])
                digest, count, checked_at = hashlib.sha256(), 0, time.monotonic()
                with archive.open(item["filename"]) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        if time.monotonic() - checked_at > 0.1:
                            _check_running(store, claim)
                            checked_at = time.monotonic()
                        digest.update(block)
                        count += len(block)
                if (digest.hexdigest(), count) != (expected["sha256"], expected["size_bytes"]):
                    raise ApiError("DELIVERY_READBACK_FAILED", "A packaged member does not match its selected result", 409)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        digest, size = _file_digest(temporary)
        _check_running(store, claim)
        if target.exists():
            if not target.is_file() or _file_digest(target) != (digest, size):
                raise ApiError("DELIVERY_INTEGRITY_ERROR", "An existing package differs and was preserved", 409)
        else:
            # Same-volume exclusive publication; never replace an unknown file.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if not target.is_file() or _file_digest(target) != (digest, size):
                    raise ApiError("DELIVERY_INTEGRITY_ERROR", "Another package already occupies this delivery path", 409)
        if _file_digest(target) != (digest, size):
            raise ApiError("DELIVERY_READBACK_FAILED", "The published package failed verification", 409)
        return digest, size
    finally:
        temporary.unlink(missing_ok=True)


def process_delivery_job(app, claim):
    from .worker import _advance, _set_job_error, claim_can_commit

    store = app.extensions["oevira.store"]
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    job = store.row("SELECT * FROM jobs WHERE id=?", (claim["id"],))
    batch_id = json.loads(job["payload_json"])["batch_id"]
    try:
        with store.transaction(immediate=False) as connection:
            batch = _require_batch(connection, identity, batch_id)
        members = store.rows("SELECT * FROM delivery_members WHERE batch_id=? ORDER BY ordinal", (batch_id,))
        failures = 0
        for position, item in enumerate(members):
            if not _advance(store, claim["id"], claim["generation"], position, len(members) + 1, "Preparing selected delivery files"):
                return
            reference = json.loads(item["reference_json"])
            try:
                _resolve_member(store, identity, batch["work_id"], reference)
                if item["status"] == "completed":
                    metadata = json.loads(item["file_json"])
                    _verify_file(store, metadata)
                else:
                    metadata = _resolve_member(store, identity, batch["work_id"], reference, export_key=f"delivery:{batch_id}:{item['member_id']}")
                    metadata = {key: value for key, value in metadata.items() if key not in {"path", "filename"}}
                    _verify_file(store, metadata)
                with store.transaction() as connection:
                    current = connection.execute("SELECT status,lease_generation FROM jobs WHERE id=?", (claim["id"],)).fetchone()
                    if current["status"] != "running" or current["lease_generation"] != claim["generation"]:
                        return
                    require_work_access(connection, identity, batch["work_id"], write=True)
                    _require_member(connection, identity, batch["work_id"], reference)
                    connection.execute("UPDATE delivery_members SET status='completed',file_json=?,error_code=NULL,updated_at=? WHERE batch_id=? AND member_id=?",
                                       (_json(metadata), utc_now(), batch_id, item["member_id"]))
            except ApiError as error:
                if error.code == "JOB_CONTROL_STOPPED":
                    return
                failures += 1
                with store.transaction() as connection:
                    if not claim_can_commit(connection, claim, allow_stopping=True):
                        return
                    connection.execute("UPDATE delivery_members SET status='failed',error_code=?,updated_at=? WHERE batch_id=? AND member_id=?", (error.code, utc_now(), batch_id, item["member_id"]))
        if failures:
            with store.transaction() as connection:
                if not claim_can_commit(connection, claim):
                    return
                connection.execute("UPDATE delivery_batches SET status='partial',updated_at=? WHERE id=?", (utc_now(), batch_id))
                connection.execute("UPDATE jobs SET status='partial',error_code='DELIVERY_MEMBERS_INCOMPLETE',error_message='Some delivery members need attention; completed files were preserved.',updated_at=? WHERE id=? AND lease_generation=? AND status='running'", (utc_now(), claim["id"], claim["generation"]))
            return
        members = store.rows("SELECT * FROM delivery_members WHERE batch_id=? ORDER BY ordinal", (batch_id,))
        digest, size = _package(store, claim, batch, members, identity)
        with store.transaction() as connection:
            require_work_access(connection, identity, batch["work_id"], write=True)
            current = connection.execute("SELECT status,lease_generation FROM jobs WHERE id=?", (claim["id"],)).fetchone()
            if current["status"] != "running" or current["lease_generation"] != claim["generation"]:
                return
            for member in members:
                _require_member(connection, identity, batch["work_id"], json.loads(member["reference_json"]))
            connection.execute("UPDATE delivery_batches SET status='completed',sha256=?,size_bytes=?,updated_at=? WHERE id=?", (digest, size, utc_now(), batch_id))
            connection.execute("UPDATE jobs SET status='completed',progress_completed=?,progress_total=?,progress_label='Delivery files verified',error_code=NULL,error_message=NULL,updated_at=? WHERE id=? AND lease_generation=?", (len(members) + 1, len(members) + 1, utc_now(), claim["id"], claim["generation"]))
    except ApiError as error:
        if error.code != "JOB_CONTROL_STOPPED":
            with store.transaction() as connection:
                if not claim_can_commit(connection, claim, allow_stopping=True):
                    return
                connection.execute("UPDATE delivery_batches SET status='partial',updated_at=? WHERE id=? AND status!='completed'", (utc_now(), batch_id))
            _set_job_error(store, claim["id"], claim["generation"], error.code, error.message)
