"""Versioned publications, reusable templates and author-attributed experience.

Private Artifact remains the editable owner. Releases contain only explicitly
selected publication content; no private input snapshot or workspace file path
is copied into a release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
import math
import secrets
import uuid
from urllib.parse import urlsplit

from flask import g, request

from .api import ApiError, _artifact_data, _body, _idempotency_key, _not_found, _optional_string, _request_fingerprint, _string, _success
from .auth import utc_now
from .collaboration import require_artifact_access, require_material_access, require_work_access, require_workspace_access, work_commission


_COMMUNITY_PUBLISH_JOB_KIND = "community_publish"
_COMMUNITY_PUBLISH_INLINE_VERIFY_BYTES = 32 * 1024 * 1024


SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    artifact_id TEXT REFERENCES artifacts(id),
    artifact_version INTEGER,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('artifact','media_material','media_render','media_project')),
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL CHECK(source_version > 0),
    kind TEXT NOT NULL CHECK(kind IN ('work','template','experience')),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','submitted','approved','rejected','published','withdrawn')),
    visibility TEXT NOT NULL CHECK(visibility IN ('private','team','unlisted','public')),
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((source_kind='artifact' AND artifact_id IS NOT NULL AND artifact_version IS NOT NULL
           AND source_id=artifact_id AND source_version=artifact_version)
       OR (source_kind!='artifact' AND artifact_id IS NULL AND artifact_version IS NULL))
);
CREATE TABLE IF NOT EXISTS publication_versions (
    publication_id TEXT NOT NULL REFERENCES publications(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(publication_id,version)
);
CREATE TABLE IF NOT EXISTS publication_reviews (
    id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    publication_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    reason TEXT NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    review_mode TEXT NOT NULL DEFAULT 'human' CHECK(review_mode IN ('human','automatic')),
    policy_version TEXT NOT NULL DEFAULT '',
    rights_digest TEXT NOT NULL DEFAULT '',
    members_digest TEXT NOT NULL DEFAULT '',
    audience_visibility TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS publication_releases (
    id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    publication_version INTEGER NOT NULL,
    review_id TEXT NOT NULL REFERENCES publication_reviews(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    share_token_hash TEXT,
    published_at TEXT NOT NULL,
    withdrawn_at TEXT,
    operation_id TEXT,
    intent_json TEXT NOT NULL DEFAULT '{}',
    receipt_json TEXT NOT NULL DEFAULT '{}',
    authors_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS publication_release_subject_refs (
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    ref_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_version INTEGER NOT NULL CHECK(subject_version > 0),
    work_id TEXT,
    relationship TEXT NOT NULL,
    relationship_origin TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY(release_id,ref_id)
);
CREATE TABLE IF NOT EXISTS community_items (
    user_id TEXT NOT NULL REFERENCES users(id),
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    kind TEXT NOT NULL CHECK(kind IN ('collection','queue')),
    position INTEGER NOT NULL,
    progress_seconds REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id,release_id,kind)
);
CREATE TABLE IF NOT EXISTS community_follows (
    follower_id TEXT NOT NULL REFERENCES users(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(follower_id,creator_id)
);
CREATE TABLE IF NOT EXISTS release_comments (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    anchor_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS publication_reports (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    reported_by TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    resolution TEXT,
    resolved_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS release_features (
    release_id TEXT PRIMARY KEY REFERENCES publication_releases(id),
    featured_by TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 10000),
    starts_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_appeals (
    id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    review_id TEXT NOT NULL REFERENCES publication_reviews(id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_restore_receipts (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    operation_key TEXT NOT NULL,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    request_hash TEXT NOT NULL,
    result_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id,operation_key)
);
CREATE TABLE IF NOT EXISTS publication_coauthors (
    id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    invited_by TEXT NOT NULL REFERENCES users(id),
    invited_version INTEGER NOT NULL CHECK(invited_version > 0),
    invited_scope_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('invited','accepted','declined','revoked')),
    accepted_version INTEGER,
    accepted_scope_digest TEXT,
    invited_at TEXT NOT NULL,
    responded_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(publication_id,user_id)
);
CREATE INDEX IF NOT EXISTS publication_coauthors_user_status
    ON publication_coauthors(workspace_id,user_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS publication_coauthor_events (
    id TEXT PRIMARY KEY,
    relation_id TEXT NOT NULL REFERENCES publication_coauthors(id),
    publication_id TEXT NOT NULL REFERENCES publications(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL CHECK(action IN ('invited','accepted','declined','revoked')),
    publication_version INTEGER NOT NULL,
    scope_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publication_coauthor_events_history
    ON publication_coauthor_events(publication_id,user_id,created_at,id);
CREATE TABLE IF NOT EXISTS publication_release_coauthors (
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    display_name TEXT NOT NULL DEFAULT '',
    accepted_version INTEGER NOT NULL,
    accepted_scope_digest TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(release_id,user_id)
);
"""


def install_community_schema(store):
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        _upgrade_publication_sources(connection)
        _upgrade_publication_reviews(connection)
        _upgrade_publication_release_operations(connection)
        _upgrade_publication_release_coauthors(connection)
    finally:
        connection.close()
    from .community_media import install_community_media_schema

    install_community_media_schema(store)


def _upgrade_publication_sources(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(publications)")}
    if "source_kind" in columns:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE publications_source_upgrade (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            author_id TEXT NOT NULL REFERENCES users(id),
            artifact_id TEXT REFERENCES artifacts(id),
            artifact_version INTEGER,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('artifact','media_material','media_render','media_project')),
            source_id TEXT NOT NULL,
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            kind TEXT NOT NULL CHECK(kind IN ('work','template','experience')),
            version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','submitted','approved','rejected','published','withdrawn')),
            visibility TEXT NOT NULL CHECK(visibility IN ('private','team','unlisted','public')),
            content_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK((source_kind='artifact' AND artifact_id IS NOT NULL AND artifact_version IS NOT NULL
                   AND source_id=artifact_id AND source_version=artifact_version)
               OR (source_kind!='artifact' AND artifact_id IS NULL AND artifact_version IS NULL))
        )""")
        connection.execute("""INSERT INTO publications_source_upgrade(
            id,workspace_id,author_id,artifact_id,artifact_version,source_kind,source_id,source_version,
            kind,version,status,visibility,content_json,created_at,updated_at)
            SELECT id,workspace_id,author_id,artifact_id,artifact_version,'artifact',artifact_id,artifact_version,
                   kind,version,status,visibility,content_json,created_at,updated_at FROM publications""")
        connection.execute("DROP TABLE publications")
        connection.execute("ALTER TABLE publications_source_upgrade RENAME TO publications")
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Publication source migration failed foreign-key validation")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _upgrade_publication_reviews(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(publication_reviews)")}
    if "review_mode" not in columns:
        connection.execute("ALTER TABLE publication_reviews ADD COLUMN review_mode TEXT NOT NULL DEFAULT 'human'")
    if "policy_version" not in columns:
        connection.execute("ALTER TABLE publication_reviews ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''")
    for column in ("rights_digest", "members_digest", "audience_visibility"):
        if column not in columns:
            connection.execute(f"ALTER TABLE publication_reviews ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def _upgrade_publication_release_operations(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(publication_releases)")}
    if "operation_id" not in columns:
        connection.execute("ALTER TABLE publication_releases ADD COLUMN operation_id TEXT")
    if "intent_json" not in columns:
        connection.execute("ALTER TABLE publication_releases ADD COLUMN intent_json TEXT NOT NULL DEFAULT '{}' ")
    if "receipt_json" not in columns:
        connection.execute("ALTER TABLE publication_releases ADD COLUMN receipt_json TEXT NOT NULL DEFAULT '{}' ")
    if "authors_json" not in columns:
        connection.execute("ALTER TABLE publication_releases ADD COLUMN authors_json TEXT NOT NULL DEFAULT '{}' ")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS publication_release_operation_id "
        "ON publication_releases(operation_id) WHERE operation_id IS NOT NULL"
    )


def _upgrade_publication_release_coauthors(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(publication_release_coauthors)")}
    if columns and "display_name" not in columns:
        connection.execute("ALTER TABLE publication_release_coauthors ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ApiError("INVALID_INPUT", "Publication metadata must contain finite JSON values", 400) from error


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _version(value):
    if type(value) is not int or value < 1:
        raise ApiError("INVALID_INPUT", "The exact positive version is required", 400)
    return value


def _aware_timestamp(value, field):
    if not isinstance(value, str) or len(value) > 64:
        raise ApiError("INVALID_INPUT", f"{field} must be a timestamp with timezone", 400)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("INVALID_INPUT", f"{field} must be a timestamp with timezone", 400) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError("INVALID_INPUT", f"{field} needs an explicit timezone", 400)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _license(value):
    required = {"view", "download", "adapt", "commercial", "redistribute", "training", "attribution", "expires_at"}
    if not isinstance(value, dict) or set(value) != required or any(type(value[key]) is not bool for key in required - {"attribution", "expires_at"}):
        raise ApiError("INVALID_LICENSE", "Specify each content-use permission separately", 400)
    if not isinstance(value["attribution"], str):
        raise ApiError("INVALID_LICENSE", "Attribution must be explicit text", 400)
    if value["expires_at"] is not None:
        try:
            expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ApiError("INVALID_LICENSE", "License expiry must be a timestamp with timezone", 400) from error
        if expiry.tzinfo is None:
            raise ApiError("INVALID_LICENSE", "License expiry needs a timezone", 400)
    return value


def _publication_series(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"series_id", "members"}:
        raise ApiError("INVALID_SERIES", "A series needs a stable series_id and an ordered members list", 400)
    series_id = value.get("series_id")
    if series_id is None:
        series_id = str(uuid.uuid4())
    else:
        series_id = _string(series_id, "series.series_id", maximum=150)
    raw_members = value.get("members")
    if not isinstance(raw_members, list) or len(raw_members) > 500:
        raise ApiError("INVALID_SERIES", "Series members must be an ordered list of at most 500 entries", 400)
    members = []
    member_ids = set()
    member_sources = set()
    for index, raw in enumerate(raw_members):
        if not isinstance(raw, dict) or raw.get("mode") not in {"pinned_release", "follow_current_public"}:
            raise ApiError("INVALID_SERIES", "Each series member must pin a release or follow a current public publication", 400, {"index": index})
        member_id = raw.get("member_id")
        if member_id is None:
            member_id = str(uuid.uuid4())
        else:
            member_id = _string(member_id, f"series.members[{index}].member_id", maximum=150)
        if member_id in member_ids:
            raise ApiError("INVALID_SERIES", "Series member IDs must be unique", 400, {"index": index})
        member_ids.add(member_id)
        if raw["mode"] == "pinned_release":
            if set(raw) - {"member_id", "mode", "release_id"} or not isinstance(raw.get("release_id"), str):
                raise ApiError("INVALID_SERIES", "A pinned series member needs one exact release_id", 400, {"index": index})
            release_id = _string(raw["release_id"], "series.release_id", maximum=255)
            source_key = ("pinned_release", release_id)
            if source_key in member_sources:
                raise ApiError("INVALID_SERIES", "A release may appear only once in an ordered series", 400, {"index": index})
            member_sources.add(source_key)
            members.append({"member_id": member_id, "mode": "pinned_release", "release_id": release_id})
        else:
            if set(raw) - {"member_id", "mode", "publication_id"} or not isinstance(raw.get("publication_id"), str):
                raise ApiError("INVALID_SERIES", "A following series member needs one publication_id", 400, {"index": index})
            source_publication = _string(raw["publication_id"], "series.publication_id", maximum=255)
            source_key = ("follow_current_public", source_publication)
            if source_key in member_sources:
                raise ApiError("INVALID_SERIES", "A followed publication may appear only once in an ordered series", 400, {"index": index})
            member_sources.add(source_key)
            members.append({"member_id": member_id, "mode": "follow_current_public", "publication_id": source_publication})
    return {"series_id": series_id, "members": members}


def _series_source_key(member: dict) -> tuple:
    if member.get("mode") == "pinned_release":
        return member.get("mode"), member.get("release_id")
    return member.get("mode"), member.get("publication_id")


def _merge_series_identity(raw_content: dict, prior_content: dict) -> None:
    """Preserve stable series/member identity while editing ordered membership."""
    previous = prior_content.get("series") if isinstance(prior_content, dict) else None
    if "series" not in raw_content:
        if previous is not None:
            raw_content["series"] = previous
        return
    incoming = raw_content.get("series")
    if not isinstance(incoming, dict) or not isinstance(previous, dict):
        return
    previous_id = previous.get("series_id")
    incoming_id = incoming.get("series_id")
    if incoming_id is not None and incoming_id != previous_id:
        raise ApiError("SERIES_ID_CONFLICT", "A publication revision cannot replace its stable series identity", 409)
    incoming["series_id"] = previous_id
    previous_members = [item for item in previous.get("members", []) if isinstance(item, dict)]
    by_source: dict[tuple, list[str]] = {}
    by_id = {item.get("member_id"): item for item in previous_members if isinstance(item.get("member_id"), str)}
    for item in previous_members:
        key = _series_source_key(item)
        if isinstance(item.get("member_id"), str):
            by_source.setdefault(key, []).append(item["member_id"])
    used: set[str] = set()
    incoming_members = incoming.get("members")
    if isinstance(incoming_members, list):
        for member in incoming_members:
            if not isinstance(member, dict):
                continue
            member_id = member.get("member_id")
            if isinstance(member_id, str) and member_id in by_id:
                used.add(member_id)
                continue
            member.pop("member_id", None)
            candidates = [value for value in by_source.get(_series_source_key(member), []) if value not in used]
            if len(candidates) == 1:
                member["member_id"] = candidates[0]
                used.add(candidates[0])


def _check_license(license_record, purpose):
    if not license_record.get(purpose):
        raise ApiError("LICENSE_NOT_GRANTED", "This release does not grant the requested use", 403)
    if license_record.get("expires_at") and datetime.fromisoformat(license_record["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise ApiError("LICENSE_EXPIRED", "The release license has expired", 403)


def _content(
    data,
    kind,
    *,
    allow_empty_body=False,
    private_work_draft=False,
    allow_included_dependencies=False,
    require_template=True,
):
    allowed = {"title", "summary", "body", "blocks", "category", "tags", "license", "template", "experience", "series", "dependencies", "privacy_review", "subject_refs"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Publication content contains an unsupported field", 400)
    if kind == "work":
        title = data.get("title", "")
        if not isinstance(title, str) or len(title) > 300:
            raise ApiError("INVALID_INPUT", "Publication title must be text within the size limit", 400)
    else:
        title = _string(data.get("title"), "title", maximum=300)

    blocks_supplied = "blocks" in data
    if kind == "work":
        if blocks_supplied:
            blocks = _publication_blocks(data["blocks"])
            body = _publication_body(blocks)
            if "body" in data and data["body"] != body:
                raise ApiError("PUBLICATION_BODY_PROJECTION_MISMATCH", "body must match the ordered native content blocks", 400)
        else:
            raw_body = data.get("body", "") if allow_empty_body or private_work_draft else data.get("body")
            if not isinstance(raw_body, str) or len(raw_body) > 200000 or (not (allow_empty_body or private_work_draft or "series" in data) and not raw_body.strip()):
                raise ApiError("INVALID_INPUT", "Publication body must be text within the size limit", 400)
            body = raw_body
            blocks = ([{"block_id": "body", "kind": "paragraph", "text": body}]
                      if body else [])
    else:
        if blocks_supplied:
            raise ApiError("INVALID_INPUT", "Native content blocks are supported for work publications", 400)
        raw_body = data.get("body", "") if allow_empty_body else data.get("body")
        if allow_empty_body:
            if not isinstance(raw_body, str) or len(raw_body) > 200000:
                raise ApiError("INVALID_INPUT", "Publication body must be text within the size limit", 400)
            body = raw_body
        else:
            body = _string(raw_body, "public body", maximum=200000)

    summary = data.get("summary", "") if kind == "work" else data.get("summary")
    if not isinstance(summary, str) or len(summary) > 2000 or (kind != "work" and not summary.strip()):
        raise ApiError("INVALID_INPUT", "Publication summary must be text within the size limit", 400)
    if private_work_draft and "license" not in data:
        license_record = {
            "view": False, "download": False, "adapt": False, "commercial": False,
            "redistribute": False, "training": False, "attribution": "", "expires_at": None,
        }
    else:
        license_record = _license(data.get("license"))
    privacy_review = data.get("privacy_review")
    if private_work_draft and privacy_review in (None, {}):
        privacy_review = {}
    elif (not isinstance(privacy_review, dict)
          or set(privacy_review) - {"selected_content_only", "client_permission"}
          or privacy_review.get("selected_content_only") is not True
          or ("client_permission" in privacy_review and type(privacy_review["client_permission"]) is not bool)):
        raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected public content before submitting it", 400)
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ApiError("INVALID_DEPENDENCIES", "Dependencies must be an array", 400)
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"id", "version", "sha256", "license", "included", "description"}:
            raise ApiError("INVALID_DEPENDENCIES", "Each dependency needs a fixed version, checksum and separate license", 400)
        _string(dependency["id"], "dependency.id", maximum=255)
        if type(dependency["version"]) is not int or dependency["version"] < 1:
            raise ApiError("INVALID_DEPENDENCIES", "Dependency versions must be positive integers", 400)
        if not isinstance(dependency["sha256"], str) or len(dependency["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in dependency["sha256"]):
            raise ApiError("INVALID_DEPENDENCIES", "Dependency SHA-256 must be lowercase hexadecimal", 400)
        if type(dependency["included"]) is not bool:
            raise ApiError("INVALID_DEPENDENCIES", "Dependency packaging state must be explicit", 400)
        _string(dependency["description"], "dependency.description", maximum=2000)
        _license(dependency["license"])
        if dependency["included"] and not allow_included_dependencies:
            # Dependency bytes are owned by media/file publishing, never assumed present from a manifest.
            raise ApiError("DEPENDENCY_PACKAGE_REQUIRED", "Attach dependencies through the verified media/file publication path", 409)
    result = {"title": title, "summary": summary, "body": body, "category": data.get("category"), "tags": data.get("tags", []),
              "license": license_record, "dependencies": dependencies,
              "subject_refs": _publication_subject_refs(data.get("subject_refs", []))}
    if kind == "work":
        result["blocks"] = blocks
        if "series" in data:
            result["series"] = _publication_series(data["series"])
    elif "series" in data:
        raise ApiError("INVALID_SERIES", "Series membership is only supported for work publications", 400)
    result["privacy_review"] = privacy_review
    if not isinstance(result["tags"], list) or not all(isinstance(tag, str) for tag in result["tags"]):
        raise ApiError("INVALID_INPUT", "Tags must be text", 400)
    if kind == "template":
        template = data.get("template")
        if template is None and not require_template:
            pass
        elif not isinstance(template, dict) or template.get("schema") != "oevira-document-template/1":
            raise ApiError("TEMPLATE_SCHEMA_UNSUPPORTED", "Provide a supported editable document or media template graph", 400)
        if isinstance(template, dict) and template.get("schema") == "oevira-document-template/1":
            if set(template) != {"schema", "blocks", "slots"} or not isinstance(template["blocks"], list) or not template["blocks"] or not isinstance(template["slots"], list):
                raise ApiError("INVALID_TEMPLATE", "Template needs editable blocks and replacement slots", 400)
            ids = set()
            for block in template["blocks"]:
                if not isinstance(block, dict) or set(block) != {"id", "kind", "content"} or block["kind"] not in {"markdown", "heading", "paragraph"}:
                    raise ApiError("INVALID_TEMPLATE", "Each editable block needs a stable id, kind and content", 400)
                _string(block["id"], "block.id", maximum=150)
                _string(block["content"], "block.content", maximum=200000)
                if block["id"] in ids:
                    raise ApiError("INVALID_TEMPLATE", "Template block ids must be unique", 400)
                ids.add(block["id"])
            slots = set()
            for slot in template["slots"]:
                if not isinstance(slot, dict) or set(slot) != {"name", "label", "required", "default", "max_length"} or type(slot["required"]) is not bool or type(slot["max_length"]) is not int or not 1 <= slot["max_length"] <= 20000:
                    raise ApiError("INVALID_TEMPLATE", "Each slot needs a name, label, required state, default and length constraint", 400)
                _string(slot["name"], "slot.name", maximum=100)
                _string(slot["label"], "slot.label", maximum=300)
                if slot["name"] in slots or not isinstance(slot["default"], str):
                    raise ApiError("INVALID_TEMPLATE", "Slot names must be unique and defaults must be text", 400)
                if not any("{{" + slot["name"] + "}}" in block["content"] for block in template["blocks"]):
                    raise ApiError("INVALID_TEMPLATE", "A slot must point to an editable block", 400)
                slots.add(slot["name"])
            result["template"] = template
    if kind == "experience":
        experience = data.get("experience")
        required = {"problem", "mechanism", "steps", "conditions", "counterexamples", "results", "limitations", "evidence", "claim_type"}
        allowed = required | {"independent_validation", "training_eligibility"}
        if (
            not isinstance(experience, dict) or not required <= set(experience) or set(experience) - allowed
            or experience["claim_type"] not in {"author_report", "observed", "inference", "unknown"}
            or experience.get("independent_validation", "not_established_by_publication") != "not_established_by_publication"
            or experience.get("training_eligibility", "not_granted") != "not_granted"
        ):
            raise ApiError("INVALID_EXPERIENCE", "Experience must preserve its mechanism, conditions, evidence, failures and claim type", 400)
        for field in ("problem", "mechanism", "limitations"):
            _string(experience[field], field, maximum=20000)
        for field in ("steps", "conditions", "counterexamples", "results", "evidence"):
            values = experience[field]
            if not isinstance(values, list) or len(values) > 100 or any(not isinstance(item, str) or not item.strip() or len(item) > 5000 for item in values):
                raise ApiError("INVALID_EXPERIENCE", f"{field} must contain bounded, non-empty statements", 400)
        if not experience["steps"]:
            raise ApiError("INVALID_EXPERIENCE", "Experience needs at least one actionable step", 400)
        if experience["claim_type"] != "unknown" and experience["results"] and not experience["evidence"]:
            raise ApiError("EXPERIENCE_EVIDENCE_REQUIRED", "Claims about results need at least one cited evidence statement", 400)
        result["experience"] = {**experience, "independent_validation": "not_established_by_publication", "training_eligibility": "not_granted"}
    _json(result)
    return result


def _publication_blocks(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 500:
        raise ApiError("INVALID_PUBLICATION_BLOCKS", "Publication blocks must be an ordered list of at most 500 items", 400)
    blocks = []
    block_ids = set()
    total_text = 0
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
            raise ApiError("INVALID_PUBLICATION_BLOCKS", "Each block needs a stable id and a supported kind", 400, {"index": index})
        kind = raw["kind"]
        expected = {
            "paragraph": {"block_id", "kind", "text"},
            "heading": {"block_id", "kind", "text"},
            "link": {"block_id", "kind", "text", "url"},
            "image": {"block_id", "kind", "item_id", "alt_text", "decorative", "caption"},
            "video": {"block_id", "kind", "item_id", "caption"},
            "audio": {"block_id", "kind", "item_id", "caption"},
        }.get(kind)
        if expected is None or set(raw) != expected:
            raise ApiError("INVALID_PUBLICATION_BLOCKS", "A block does not match its native content kind", 400, {"index": index})
        block_id = _string(raw["block_id"], "block_id", maximum=150)
        if block_id in block_ids:
            raise ApiError("INVALID_PUBLICATION_BLOCKS", "Block ids must be unique within the publication version", 400, {"index": index})
        block_ids.add(block_id)
        block = {"block_id": block_id, "kind": kind}
        if kind in {"paragraph", "heading", "link"}:
            text = _string(raw["text"], f"blocks[{index}].text", maximum=200000)
            total_text += len(text)
            block["text"] = text
            if kind == "link":
                url = _string(raw["url"], f"blocks[{index}].url", maximum=2048)
                try:
                    parsed = urlsplit(url)
                except ValueError as error:
                    raise ApiError("INVALID_PUBLICATION_BLOCKS", "Links must use an absolute HTTP or HTTPS address", 400, {"index": index}) from error
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                    raise ApiError("INVALID_PUBLICATION_BLOCKS", "Links must use an absolute HTTP or HTTPS address without embedded credentials", 400, {"index": index})
                block["url"] = url
        else:
            item_id = _string(raw["item_id"], f"blocks[{index}].item_id", maximum=150)
            caption = raw["caption"]
            if not isinstance(caption, str) or len(caption) > 20000:
                raise ApiError("INVALID_PUBLICATION_BLOCKS", "Media captions must be bounded text", 400, {"index": index})
            total_text += len(caption)
            block.update({"item_id": item_id, "caption": caption})
            if kind == "image":
                alt_text = raw["alt_text"]
                if not isinstance(alt_text, str) or len(alt_text) > 2000 or type(raw["decorative"]) is not bool:
                    raise ApiError("INVALID_PUBLICATION_BLOCKS", "Images need bounded alternative text and an explicit decorative state", 400, {"index": index})
                if raw["decorative"] and alt_text:
                    raise ApiError("INVALID_PUBLICATION_BLOCKS", "A decorative image cannot also provide alternative text", 400, {"index": index})
                block.update({"alt_text": alt_text, "decorative": raw["decorative"]})
        if total_text > 200000:
            raise ApiError("INVALID_PUBLICATION_BLOCKS", "Publication text exceeds the current 200000-character limit", 413)
        blocks.append(block)
    return blocks


def _publication_body(blocks: list[dict]) -> str:
    visible = []
    for block in blocks:
        if block["kind"] in {"paragraph", "heading", "link"}:
            visible.append(block["text"])
        elif block.get("caption"):
            visible.append(block["caption"])
    return "\n\n".join(visible)


def _publication_subject_refs(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise ApiError("INVALID_SUBJECT_REFERENCES", "subject_refs must be an ordered list of at most 100 selected Subjects", 400)
    refs = []
    ref_ids = set()
    identities = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) - {"ref_id", "subject_id", "subject_version", "work_id", "relationship", "relationship_origin"}:
            raise ApiError("INVALID_SUBJECT_REFERENCES", "Each subject reference needs a stable ref_id, exact Subject version and author-selected relationship", 400, {"index": index})
        if raw.get("relationship_origin", "author_selected_unverified") != "author_selected_unverified":
            raise ApiError("INVALID_SUBJECT_REFERENCES", "Subject relationship origin is assigned by the author and cannot be elevated", 400, {"index": index})
        ref_id = _string(raw.get("ref_id"), "subject_refs[].ref_id", maximum=150)
        subject_id = _string(raw.get("subject_id"), "subject_refs[].subject_id", maximum=200)
        version = raw.get("subject_version")
        if type(version) is not int or version < 1:
            raise ApiError("INVALID_SUBJECT_REFERENCES", "subject_version must identify a positive saved Subject version", 400, {"index": index})
        work_id = raw.get("work_id")
        if work_id is not None:
            work_id = _string(work_id, "subject_refs[].work_id", maximum=200)
        relationship = _string(raw.get("relationship"), "subject_refs[].relationship", maximum=500)
        identity = (subject_id, work_id, relationship)
        if ref_id in ref_ids or identity in identities:
            raise ApiError("INVALID_SUBJECT_REFERENCES", "Subject reference IDs and selected relationships must be unique", 400, {"index": index})
        ref_ids.add(ref_id)
        identities.add(identity)
        refs.append({
            "ref_id": ref_id, "subject_id": subject_id, "subject_version": version,
            "work_id": work_id, "relationship": relationship,
            "relationship_origin": "author_selected_unverified",
        })
    return refs


def _attach_publication_media_blocks(content: dict, members: list[dict], *, append_missing: bool) -> None:
    """Bind ordered native media blocks to the selected, verified release members."""
    if not isinstance(content, dict) or not isinstance(content.get("blocks"), list):
        return
    available = {}
    for member in members:
        if member.get("role") != "rendition":
            continue
        metadata = member.get("metadata")
        if not isinstance(metadata, dict):
            try:
                metadata = json.loads(member.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        item_id = metadata.get("item_id") or "primary"
        kind = metadata.get("media_kind")
        block_kind = "image" if kind == "image" else "audio" if kind == "audio" else "video" if kind in {"video", "video_audio"} else None
        if block_kind is None:
            raise ApiError("COMMUNITY_MEDIA_TYPE_UNSUPPORTED", "A selected release member has no supported native content kind", 415)
        prior = available.get(item_id)
        if prior is not None and prior != block_kind:
            raise ApiError("MEDIA_ITEM_KIND_CONFLICT", "Renditions for one content item must have the same media kind", 409)
        available[item_id] = block_kind

    blocks = list(content["blocks"])
    references = []
    for block in blocks:
        if block.get("kind") not in {"image", "video", "audio"}:
            continue
        item_id = block.get("item_id")
        if item_id not in available:
            raise ApiError("MEDIA_BLOCK_ITEM_NOT_FOUND", "A native media block must reference a selected release member", 409)
        expected_kind = available[item_id]
        if block["kind"] != expected_kind:
            raise ApiError("MEDIA_BLOCK_KIND_MISMATCH", "A native media block kind must match its selected source", 409)
        references.append(item_id)
    if len(references) != len(set(references)):
        raise ApiError("MEDIA_BLOCK_DUPLICATE_ITEM", "Each selected media item may appear once in the ordered publication", 400)

    missing = [item_id for item_id in available if item_id not in set(references)]
    if missing and not append_missing:
        raise ApiError("MEDIA_BLOCK_ITEM_MISSING", "Every selected media item must appear in the native publication blocks", 409)
    for item_id in missing:
        kind = available[item_id]
        block = {"block_id": f"media-{item_id}", "kind": kind, "item_id": item_id, "caption": ""}
        if kind == "image":
            block.update({"alt_text": "", "decorative": False})
        blocks.append(block)
    content["blocks"] = blocks
    content["body"] = _publication_body(blocks)
    if len(content["body"]) > 200000:
        raise ApiError("INVALID_PUBLICATION_BLOCKS", "The publication text projection exceeds the current 200000-character limit", 413)
    item_order = {item_id: position for position, item_id in enumerate(
        block["item_id"] for block in blocks if block.get("kind") in {"image", "video", "audio"}
    )}
    media = content.get("media")
    if isinstance(media, dict):
        for field in ("images", "renditions"):
            rows = media.get(field)
            if not isinstance(rows, list):
                continue
            for row in rows:
                metadata = row.get("metadata") if isinstance(row, dict) else None
                if not isinstance(metadata, dict):
                    continue
                item_id = metadata.get("item_id") or "primary"
                if item_id in item_order:
                    metadata["position"] = item_order[item_id]
            rows.sort(key=lambda row: (
                row.get("metadata", {}).get("position", 0) if isinstance(row, dict) and isinstance(row.get("metadata"), dict) else 0,
                row.get("id", "") if isinstance(row, dict) else "",
            ))


def _validate_publication_blocks_for_submit(content: dict, members: list[dict]) -> None:
    _attach_publication_media_blocks(content, members, append_missing="blocks" not in content)
    blocks = content.get("blocks", [])
    if not any(
        block.get("kind") in {"image", "video", "audio"} or
        (isinstance(block.get("text"), str) and block["text"].strip()) or
        (isinstance(block.get("caption"), str) and block["caption"].strip())
        for block in blocks
    ) and not (isinstance(content.get("series"), dict) and content["series"].get("members")):
        raise ApiError("PUBLICATION_CONTENT_EMPTY", "Add text or a selected media item before submitting this publication", 400)
    for block in blocks:
        if block.get("kind") == "image" and not block.get("decorative") and not block.get("alt_text", "").strip():
            raise ApiError("IMAGE_ALT_TEXT_REQUIRED", "Describe each non-decorative image before publication", 400, {"block_id": block.get("block_id")})


def _sync_work_media_version(store, connection, publication_id: str, publication_version: int, content: dict) -> list[dict]:
    """Keep media member identity and order aligned with this version's native blocks."""
    from .community_media import _draft_members, align_draft_media_item_order

    selected_items = {
        block["item_id"]
        for block in content.get("blocks", [])
        if isinstance(block, dict) and block.get("kind") in {"image", "video", "audio"}
    }
    rows = connection.execute(
        "SELECT member_id,role,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=?",
        (publication_id, publication_version),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError) as error:
            raise ApiError("COMMUNITY_MEDIA_METADATA_INVALID", "A saved media member has invalid metadata", 409) from error
        if not isinstance(metadata, dict):
            raise ApiError("COMMUNITY_MEDIA_METADATA_INVALID", "A saved media member has invalid metadata", 409)
        item_id = metadata.get("item_id") or "primary"
        if row["role"] == "rendition" and item_id not in selected_items:
            connection.execute(
                "DELETE FROM publication_media_drafts WHERE publication_id=? AND publication_version=? AND member_id=?",
                (publication_id, publication_version, row["member_id"]),
            )
        elif row["role"] in {"caption", "chapters"} and not selected_items:
            connection.execute(
                "DELETE FROM publication_media_drafts WHERE publication_id=? AND publication_version=? AND member_id=?",
                (publication_id, publication_version, row["member_id"]),
            )

    remaining = connection.execute(
        "SELECT member_id,role,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=?",
        (publication_id, publication_version),
    ).fetchall()
    used_dependency_ids = set()
    for row in remaining:
        metadata = json.loads(row["metadata_json"] or "{}")
        used_dependency_ids.update(item for item in metadata.get("dependency_ids", []) if isinstance(item, str))
    if isinstance(content.get("dependencies"), list):
        content["dependencies"] = [
            item for item in content["dependencies"]
            if isinstance(item, dict) and item.get("id") in used_dependency_ids
        ]
    media = content.get("media")
    if isinstance(media, dict):
        for field in ("images", "renditions"):
            items = media.get(field)
            if isinstance(items, list):
                media[field] = [
                    item for item in items
                    if isinstance(item, dict)
                    and (item.get("metadata", {}).get("item_id") or "primary") in selected_items
                ]
        if not selected_items:
            media["captions"] = []
            media["chapters"] = []
    members = _draft_members(connection, publication_id, publication_version)
    _attach_publication_media_blocks(content, members, append_missing=False)
    align_draft_media_item_order(connection, publication_id, publication_version, content.get("blocks", []))
    return members


def _owned_publication(connection, identity, publication_id, *, write=False):
    row = connection.execute("SELECT * FROM publications WHERE id=? AND workspace_id=?", (publication_id, identity["workspace"]["id"])).fetchone()
    if row is None:
        raise _not_found()
    if row["author_id"] != identity["user"]["id"]:
        if write:
            raise _not_found()
        relation = connection.execute(
            "SELECT * FROM publication_coauthors WHERE publication_id=? AND user_id=? AND status='accepted'",
            (publication_id, identity["user"]["id"]),
        ).fetchone()
        content = json.loads(row["content_json"])
        if relation is None or not _coauthor_scope_is_current(
            connection, row, relation, target_version=row["version"], target_content=content,
        ):
            raise _not_found()
        from .community_media import _draft_members
        _validate_publication_source_access(
            connection, identity, row, content, _draft_members(connection, publication_id, row["version"]),
        )
    if row["source_kind"] == "artifact":
        require_artifact_access(connection, identity, row["artifact_id"], write=write)
    else:
        from .community_media import require_media_source_access

        require_media_source_access(connection, identity, row, write=write)
    return row


def _validate_coauthor_source_access(connection, identity, publication, content, media_members):
    if publication["source_kind"] == "artifact":
        artifact = connection.execute(
            "SELECT current_version FROM artifacts WHERE id=? AND workspace_id=?",
            (publication["artifact_id"], publication["workspace_id"]),
        ).fetchone()
        if artifact is None:
            raise _not_found()
        if not is_native_artifact_publication_source(
            connection, publication["workspace_id"], publication["artifact_id"],
            publication["artifact_version"], publication["id"],
        ):
            require_artifact_access(connection, identity, publication["artifact_id"])
    else:
        from .community_media import require_media_source_access

        require_media_source_access(connection, identity, publication)
    _validate_publication_source_access(connection, identity, publication, content, media_members)


def _native_artifact_publication_id(connection, workspace_id: str, artifact_id: str, artifact_version: int) -> str | None:
    artifact = connection.execute(
        "SELECT id,workspace_id,owner_user_id,work_id,current_version FROM artifacts WHERE id=? AND workspace_id=?",
        (artifact_id, workspace_id),
    ).fetchone()
    if artifact is None:
        return None
    current = connection.execute(
        "SELECT input_snapshot_json,revision_note FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
        (artifact_id, workspace_id, artifact_version),
    ).fetchone()
    if current is None:
        return None
    try:
        snapshot = json.loads(current["input_snapshot_json"] or "{}")
    except (TypeError, ValueError):
        snapshot = {}

    native = snapshot.get("community_native") if isinstance(snapshot, dict) else None
    if isinstance(snapshot, dict) and snapshot.get("kind") == "community_native":
        if not isinstance(native, dict) or not isinstance(native.get("publication_id"), str) or not native["publication_id"]:
            raise _not_found()
        return native["publication_id"]
    original = connection.execute(
        "SELECT revision_note FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=1",
        (artifact_id, workspace_id),
    ).fetchone()
    if original is None or original["revision_note"] != "Native publication draft":
        return None
    if artifact["work_id"] is not None or not artifact["owner_user_id"]:
        raise _not_found()
    # Earlier native drafts have no explicit kind marker. Their immutable v1
    # note remains authoritative after later Publication-owned Artifact edits.
    legacy = connection.execute(
        "SELECT id,workspace_id,author_id,artifact_id,source_kind,source_id,kind FROM publications "
        "WHERE workspace_id=? AND artifact_id=? AND source_kind='artifact' AND source_id=? AND kind='work'",
        (workspace_id, artifact_id, artifact_id),
    ).fetchall()
    if len(legacy) != 1 or legacy[0]["author_id"] != artifact["owner_user_id"]:
        raise _not_found()
    return legacy[0]["id"]


def is_native_artifact_publication_source(connection, workspace_id: str, artifact_id: str, artifact_version: int, publication_id: str) -> bool:
    """Purely classify an exact native Artifact version for release-snapshot access."""
    native_publication_id = _native_artifact_publication_id(connection, workspace_id, artifact_id, artifact_version)
    return native_publication_id is not None and native_publication_id == publication_id


def _coauthor_scope_digest(connection, publication, content: dict) -> str:
    from .community_media import _draft_members

    members = _draft_members(connection, publication["id"], publication["version"])
    identity = {
        "workspace": {"id": publication["workspace_id"]},
        "user": {"id": publication["author_id"]},
    }
    return _digest({
        "content_digest": _publication_content_hash(connection, publication, content),
        "rights_digest": _publication_rights_digest(
            publication, content, connection=connection, identity=identity, media_members=members,
        ),
        "members_digest": _digest(_publication_member_selection(members, content)),
        "visibility": publication["visibility"],
    })


def _coauthor_scope_is_current(connection, publication, relation, *, target_version: int, target_content: dict | None = None) -> bool:
    if relation["status"] != "accepted" or relation["accepted_version"] is None or relation["accepted_version"] > target_version:
        return False
    for version in range(relation["accepted_version"], target_version + 1):
        if version == target_version and target_content is not None:
            content = target_content
            visibility = publication["visibility"]
        else:
            saved = connection.execute(
                "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                (publication["id"], version),
            ).fetchone()
            if saved is None:
                return False
            try:
                snapshot = json.loads(saved["snapshot_json"])
                content = snapshot["content"]
                visibility = snapshot["visibility"]
            except (TypeError, ValueError, KeyError):
                return False
            if not isinstance(content, dict):
                return False
        version_row = {**dict(publication), "version": version, "visibility": visibility}
        if _coauthor_scope_digest(connection, version_row, content) != relation["accepted_scope_digest"]:
            return False
    return True


def _publication_coauthors_for_version(connection, publication, *, version: int | None = None, content: dict | None = None) -> list[dict]:
    selected_version = publication["version"] if version is None else version
    rows = connection.execute(
        "SELECT * FROM publication_coauthors WHERE publication_id=? AND status='accepted' ORDER BY invited_at,user_id",
        (publication["id"],),
    ).fetchall()
    accepted = []
    for relation in rows:
        if not _coauthor_scope_is_current(
            connection, publication, relation, target_version=selected_version, target_content=content,
        ):
            continue
        user = connection.execute("SELECT name FROM users WHERE id=?", (relation["user_id"],)).fetchone()
        if user is None:
            continue
        accepted.append({
            "user_id": relation["user_id"],
            "display_name": user["name"],
            "accepted_version": relation["accepted_version"],
            "accepted_at": relation["responded_at"],
        })
    return accepted


def _publication_creator(connection, creator_id: str) -> dict:
    user = connection.execute("SELECT name FROM users WHERE id=?", (creator_id,)).fetchone()
    return {"user_id": creator_id, "display_name": user["name"] if user else "已注销创作者"}


def _coauthor_relation_data(row) -> dict:
    return {
        "id": row["id"], "publication_id": row["publication_id"], "user_id": row["user_id"],
        "status": row["status"], "invited_version": row["invited_version"],
        "accepted_version": row["accepted_version"], "invited_at": row["invited_at"],
        "responded_at": row["responded_at"],
    }


def _record_coauthor_event(connection, relation, actor_id: str, action: str, scope_digest: str, now: str) -> None:
    connection.execute(
        "INSERT INTO publication_coauthor_events(id,relation_id,publication_id,workspace_id,user_id,actor_id,action,publication_version,scope_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), relation["id"], relation["publication_id"], relation["workspace_id"],
            relation["user_id"], actor_id, action, relation["invited_version"], scope_digest, now,
        ),
    )


def require_native_artifact_access(connection, identity: dict, artifact_id: str, *, write: bool = False) -> bool:
    """Qualify the private Artifact behind a native Publication."""
    workspace_id = identity["workspace"]["id"]
    user_id = identity["user"]["id"]
    artifact = connection.execute(
        "SELECT id,workspace_id,owner_user_id,current_version FROM artifacts WHERE id=? AND workspace_id=?",
        (artifact_id, workspace_id),
    ).fetchone()
    if artifact is None:
        return False
    publication_id = _native_artifact_publication_id(connection, workspace_id, artifact_id, artifact["current_version"])
    if publication_id is None:
        return False
    publication = connection.execute(
        "SELECT * FROM publications WHERE id=? AND workspace_id=?",
        (publication_id, workspace_id),
    ).fetchone()
    if (
        publication is None or publication["source_kind"] != "artifact"
        or publication["artifact_id"] != artifact_id or publication["source_id"] != artifact_id
        or publication["author_id"] != artifact["owner_user_id"]
    ):
        raise _not_found()
    if user_id != publication["author_id"]:
        relation = connection.execute(
            "SELECT * FROM publication_coauthors WHERE publication_id=? AND user_id=? AND status='accepted'",
            (publication_id, user_id),
        ).fetchone()
        if relation is None:
            raise _not_found()
        if write:
            raise ApiError("COMMUNITY_REVISION_REQUIRED", "Only the Publication owner can revise its native Artifact", 409)
        content = json.loads(publication["content_json"])
        if not _coauthor_scope_is_current(
            connection, publication, relation, target_version=publication["version"], target_content=content,
        ):
            raise _not_found()
        from .community_media import _draft_members
        _validate_publication_source_access(
            connection, identity, publication, content,
            _draft_members(connection, publication_id, publication["version"]),
        )
    return True


def _draft_data(row, connection=None):
    result = {
        "id": row["id"], "artifact_id": row["artifact_id"], "artifact_version": row["artifact_version"], "kind": row["kind"],
        "source": {"kind": row["source_kind"], "id": row["source_id"], "version": row["source_version"]},
        "version": row["version"], "status": row["status"], "visibility": row["visibility"],
        "content": json.loads(row["content_json"]),
    }
    if connection is not None:
        result["creator"] = _publication_creator(connection, row["author_id"])
        result["coauthors"] = _publication_coauthors_for_version(
            connection, row, version=row["version"], content=result["content"],
        )
    return result


def _save_version(connection, row, user):
    connection.execute("INSERT INTO publication_versions VALUES(?,?,?,?,?)", (row["id"], row["version"], _json(_draft_data(row, connection)), user, utc_now()))


def _native_artifact_owner(connection, identity, publication, content, *, write: bool) -> tuple[dict, dict] | None:
    """Return the private Artifact only when this Publication created and owns it."""
    if publication["source_kind"] != "artifact" or not publication["artifact_id"]:
        return None
    artifact = require_artifact_access(connection, identity, publication["artifact_id"], write=write)
    artifact_version = connection.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
        (artifact["id"], publication["workspace_id"], publication["artifact_version"]),
    ).fetchone()
    if artifact_version is None:
        raise ApiError("PUBLICATION_SOURCE_VERSION_MISSING", "The saved publication Artifact version is unavailable", 409)
    try:
        snapshot = json.loads(artifact_version["input_snapshot_json"] or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        return None
    native = snapshot.get("community_native")
    if snapshot.get("kind") != "community_native" or not isinstance(native, dict) or native.get("publication_id") != publication["id"]:
        return None
    native_kind = native.get("kind", "work")
    if native_kind != publication["kind"]:
        raise ApiError("PUBLICATION_SOURCE_CONTENT_CONFLICT", "The native Artifact and Publication kinds no longer match", 409)
    if artifact["current_version"] != publication["artifact_version"]:
        raise ApiError("PUBLICATION_SOURCE_VERSION_CONFLICT", "The native publication and its Artifact changed independently", 409)
    if (
        native.get("content_sha256") != _digest(content)
        or artifact_version["body"] != content.get("body")
        or artifact_version["title"] != (content.get("title") or "作品")
        or (native_kind == "work" and native.get("blocks", []) != content.get("blocks", []))
        or (native_kind == "experience" and native.get("experience") != content.get("experience"))
        or native.get("series") != content.get("series")
    ):
        raise ApiError("PUBLICATION_SOURCE_CONTENT_CONFLICT", "The native Artifact no longer matches this Publication version", 409)
    return artifact, dict(artifact_version)


def _save_native_artifact_content(connection, identity, publication, native_owner, content, *, revision_note: str) -> int:
    artifact, parent = native_owner
    base_version = parent["version"]
    new_version = base_version + 1
    snapshot = json.loads(parent["input_snapshot_json"] or "{}")
    snapshot["community_native"] = {
        "publication_id": publication["id"],
        "kind": publication["kind"],
        "blocks": content.get("blocks", []),
        "experience": content.get("experience"),
        "series": content.get("series"),
        "summary": content["summary"],
        "content_sha256": _digest(content),
    }
    now = utc_now()
    connection.execute(
        "INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by,parent_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            artifact["id"], publication["workspace_id"], new_version,
            content.get("title") or "作品", content["body"], parent["citations_json"],
            _json(snapshot), revision_note, now, identity["user"]["id"], base_version,
        ),
    )
    updated = connection.execute(
        "UPDATE artifacts SET title=?,current_version=?,updated_at=? WHERE id=? AND workspace_id=? AND current_version=?",
        (content.get("title") or "作品", new_version, now, artifact["id"], publication["workspace_id"], base_version),
    )
    if updated.rowcount != 1:
        raise ApiError("VERSION_CONFLICT", "The native Artifact changed while the publication revision was being saved", 409)
    return new_version


def _publication_content_hash(connection, publication, content):
    from .community_media import publication_media_fingerprint

    media_digest = publication_media_fingerprint(
        connection, publication["id"], publication["version"], content,
    )
    return media_digest or _digest(content)


def _release(connection, release_id, identity=None, share_token=None, *, purpose="view"):
    row = connection.execute("SELECT * FROM publication_releases WHERE id=? AND withdrawn_at IS NULL", (release_id,)).fetchone()
    if row is None:
        raise _not_found()
    if row["visibility"] == "private":
        if identity is None:
            raise _not_found()
        if identity["user"]["id"] != row["author_id"]:
            require_workspace_access(connection, identity)
            coauthor = connection.execute(
                "SELECT 1 FROM publication_release_coauthors WHERE release_id=? AND user_id=?",
                (row["id"], identity["user"]["id"]),
            ).fetchone()
            if coauthor is None:
                raise _not_found()
    elif row["visibility"] == "team":
        if identity is None:
            raise _not_found()
        publication = connection.execute("SELECT * FROM publications WHERE id=?", (row["publication_id"],)).fetchone()
        if publication is None or publication["workspace_id"] != identity["workspace"]["id"]:
            raise _not_found()
        if publication["source_kind"] == "artifact":
            require_workspace_access(connection, identity)
            try:
                publication_version = connection.execute(
                    "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                    (publication["id"], row["publication_version"]),
                ).fetchone()
                release_snapshot = json.loads(publication_version["snapshot_json"]) if publication_version else {}
            except (TypeError, ValueError):
                raise _not_found()
            release_artifact_id = release_snapshot.get("artifact_id")
            release_artifact_version = release_snapshot.get("artifact_version")
            if (
                not isinstance(release_artifact_id, str)
                or type(release_artifact_version) is not int
                or not is_native_artifact_publication_source(
                    connection, publication["workspace_id"], release_artifact_id,
                    release_artifact_version, publication["id"],
                )
            ):
                require_artifact_access(connection, identity, publication["artifact_id"])
        else:
            from .community_media import require_media_source_access

            require_media_source_access(connection, identity, publication)
    elif row["visibility"] == "unlisted":
        owned = identity is not None and identity["user"]["id"] == row["author_id"]
        token_matches = isinstance(share_token, str) and secrets.compare_digest(hashlib.sha256(share_token.encode()).hexdigest(), row["share_token_hash"] or "")
        if not owned and not token_matches:
            raise _not_found()
    content = json.loads(row["snapshot_json"])
    _check_license(content["license"], purpose)
    from .community_media import validate_release_media_licenses

    validate_release_media_licenses(connection, release_id, content, purpose)
    return row, content


def _resolve_series_members(connection, series: dict, identity, parent_visibility: str, *, require_available: bool) -> list[dict]:
    members = []
    for position, member in enumerate(series.get("members", [])):
        if member["mode"] == "pinned_release":
            release = connection.execute(
                "SELECT * FROM publication_releases WHERE id=? AND withdrawn_at IS NULL",
                (member["release_id"],),
            ).fetchone()
        else:
            release = connection.execute(
                "SELECT * FROM publication_releases WHERE publication_id=? ORDER BY publication_version DESC,published_at DESC,id DESC LIMIT 1",
                (member["publication_id"],),
            ).fetchone()
        available = release is not None and release["withdrawn_at"] is None
        if available:
            if member["mode"] == "follow_current_public" and release["visibility"] != "public":
                available = False
            elif parent_visibility in {"public", "unlisted"} and release["visibility"] != "public":
                available = False
            elif parent_visibility == "team" and release["visibility"] in {"private", "unlisted"}:
                available = False
            elif parent_visibility == "private" and release["visibility"] == "private" and (identity is None or identity["user"]["id"] != release["author_id"]):
                available = False
        if available:
            try:
                _release(connection, release["id"], identity, purpose="view")
            except ApiError:
                available = False
        if not available:
            if require_available:
                raise ApiError("SERIES_MEMBER_UNAVAILABLE", "A selected series member is no longer readable under the current release policy", 409,
                               {"member_id": member["member_id"], "position": position})
            members.append({
                "member_id": member["member_id"], "position": position,
                "mode": member["mode"], "availability": "unavailable",
            })
            continue
        members.append({
            "member_id": member["member_id"], "position": position,
            "mode": member["mode"], "availability": "available",
            "resolved_release_id": release["id"],
            "observed_publication_version": release["publication_version"],
            "observed_content_hash": release["content_hash"],
        })
    return members


def _validate_series_members(
    connection, identity, content: dict, visibility: str, *, require_available: bool = True,
    previous_series: dict | None = None,
) -> None:
    series = content.get("series")
    if not isinstance(series, dict):
        return
    resolved = _resolve_series_members(connection, series, identity, visibility, require_available=require_available)
    if previous_series is not None:
        old_members = {item.get("member_id"): item for item in previous_series.get("members", []) if isinstance(item, dict)}
        for member, state in zip(series.get("members", []), resolved):
            previous = old_members.get(member.get("member_id"))
            unchanged = previous is not None and _series_source_key(previous) == _series_source_key(member)
            if not unchanged and state["availability"] != "available":
                raise ApiError("SERIES_MEMBER_UNAVAILABLE", "A new series member is not currently readable", 409,
                               {"member_id": member["member_id"], "position": state["position"]})


def _public_subject_ref_records(items: list[dict]) -> list[dict]:
    return [
        {
            "ref_id": item["ref_id"],
            "relationship": item["relationship"],
            "relationship_origin": "author_selected_unverified",
            "subject": {"kind": item["subject_kind"], "name": item["subject_name"]},
            "version_at_selection": item["subject_version"],
        }
        for item in items
    ]


def _public_content(
    content: dict, *, resolved_series_members: list[dict] | None = None,
    resolved_subject_refs: list[dict] | None = None,
) -> dict:
    safe = json.loads(_json(content))
    dependencies = safe.get("dependencies")
    if isinstance(dependencies, list):
        for index, item in enumerate(dependencies, 1):
            if isinstance(item, dict):
                item["id"] = f"dependency-{index}"
                item.pop("source_id", None)
    if isinstance(safe.get("series"), dict):
        series = safe["series"]
        safe["series"] = {
            "series_id": series.get("series_id"),
            "members": resolved_series_members if resolved_series_members is not None else [],
        }
    if "subject_refs" in safe:
        safe["subject_refs"] = resolved_subject_refs if resolved_subject_refs is not None else []
    return safe


def _public_release(row, content, app, *, connection=None, identity=None):
    resolved_series_members = None
    resolved_subject_refs = []
    coauthors = []
    creator = {"user_id": row["author_id"]}
    review = None
    try:
        intent = json.loads(row["intent_json"] or "{}")
        receipt = json.loads(row["receipt_json"] or "{}")
        authorship = json.loads(row["authors_json"] or "{}")
    except (IndexError, KeyError, TypeError, ValueError):
        intent, receipt, authorship = {}, {}, {}
    if isinstance(authorship, dict):
        if isinstance(authorship.get("creator"), dict):
            creator = authorship["creator"]
        saved_coauthors = authorship.get("coauthors")
        if isinstance(saved_coauthors, list):
            coauthors = saved_coauthors
    if connection is not None:
        review = connection.execute(
            "SELECT review_mode,policy_version FROM publication_reviews WHERE id=?",
            (row["review_id"],),
        ).fetchone()
        resolved_subject_refs = _public_subject_ref_records([
            dict(item) for item in connection.execute(
                "SELECT ref_id,relationship,subject_version,subject_kind,subject_name "
                "FROM publication_release_subject_refs WHERE release_id=? ORDER BY position",
                (row["id"],),
            ).fetchall()
        ])
        if not isinstance(authorship.get("coauthors"), list):
            coauthors = [
                {"display_name": item["display_name"] or "已注销创作者"}
                for item in connection.execute(
                    "SELECT display_name FROM publication_release_coauthors WHERE release_id=? ORDER BY accepted_at,user_id",
                    (row["id"],),
                ).fetchall()
            ]
    if isinstance(content.get("series"), dict):
        if connection is None:
            with app.extensions["oevira.store"].transaction(immediate=False) as read_connection:
                resolved_series_members = _resolve_series_members(read_connection, content["series"], identity, row["visibility"], require_available=False)
        else:
            resolved_series_members = _resolve_series_members(connection, content["series"], identity, row["visibility"], require_available=False)
    return {"id": row["id"], "kind": row["kind"], "author_id": row["author_id"],
            "creator": creator,
            "coauthors": coauthors, "visibility": row["visibility"],
            "content": _public_content(
                content, resolved_series_members=resolved_series_members,
                resolved_subject_refs=resolved_subject_refs,
            ),
            "official_creator": row["author_id"] in app.config.get("OFFICIAL_CREATOR_USER_IDS", ()),
            "published_at": row["published_at"], "content_hash": row["content_hash"],
            "review_mode": review["review_mode"] if review else "human",
            "policy_version": review["policy_version"] if review else "",
            "operation": {
                "id": row["operation_id"] if "operation_id" in row.keys() else None,
                "status": receipt.get("status"),
                "effect_phase": receipt.get("effect_phase"),
                "decision": intent.get("decision"),
                "policy_version": intent.get("policy_version"),
                "checks": receipt.get("checks", {}),
            },
            "delivery": "local_review_only"}


def _moderator(app, identity):
    if identity["user"]["id"] not in app.config.get("COMMUNITY_MODERATOR_USER_IDS", ()):
        raise ApiError("FORBIDDEN", "Content review requires a server-granted moderator role", 403)


def _publication_source_work_ids(connection, publication, content: dict, media_members: list[dict]) -> set[str]:
    work_ids = set()
    if publication["source_kind"] == "artifact" and publication["artifact_id"]:
        artifact = connection.execute(
            "SELECT work_id FROM artifacts WHERE id=? AND workspace_id=?",
            (publication["artifact_id"], publication["workspace_id"]),
        ).fetchone()
        if artifact is None:
            raise _not_found()
        if artifact["work_id"]:
            work_ids.add(artifact["work_id"])
        artifact_version = connection.execute(
            "SELECT citations_json,input_snapshot_json FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
            (publication["artifact_id"], publication["workspace_id"], publication["artifact_version"]),
        ).fetchone()
        if artifact_version is not None:
            try:
                snapshot = json.loads(artifact_version["input_snapshot_json"] or "{}")
                citations = json.loads(artifact_version["citations_json"] or "[]")
            except (TypeError, ValueError):
                snapshot, citations = {}, []
            material_ids = {
                item.get("id") or item.get("material_id")
                for item in snapshot.get("materials", []) if isinstance(item, dict)
            } if isinstance(snapshot, dict) else set()
            if isinstance(citations, list):
                material_ids.update(
                    item.get("material_id") for item in citations
                    if isinstance(item, dict) and isinstance(item.get("material_id"), str)
                )
            for material_id in material_ids:
                if not isinstance(material_id, str):
                    continue
                source = connection.execute(
                    "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
                    (material_id, publication["workspace_id"]),
                ).fetchone()
                if source and source["work_id"]:
                    work_ids.add(source["work_id"])
    elif publication["source_kind"] == "media_material":
        material = connection.execute(
            "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
        if material and material["work_id"]:
            work_ids.add(material["work_id"])
    elif publication["source_kind"] == "media_project":
        project = connection.execute(
            "SELECT work_id FROM media_projects WHERE id=? AND workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
        if project and project["work_id"]:
            work_ids.add(project["work_id"])
    elif publication["source_kind"] == "media_render":
        project = connection.execute(
            "SELECT p.work_id FROM media_renders r JOIN media_projects p ON p.id=r.project_id AND p.workspace_id=r.workspace_id "
            "WHERE r.id=? AND r.workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
        if project and project["work_id"]:
            work_ids.add(project["work_id"])

    for member in media_members:
        if member.get("source_kind") == "material":
            source = connection.execute(
                "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
                (member.get("source_id"), publication["workspace_id"]),
            ).fetchone()
            if source and source["work_id"]:
                work_ids.add(source["work_id"])
        elif member.get("source_kind") == "media_project":
            source = connection.execute(
                "SELECT work_id FROM media_projects WHERE id=? AND workspace_id=?",
                (member.get("source_id"), publication["workspace_id"]),
            ).fetchone()
            if source and source["work_id"]:
                work_ids.add(source["work_id"])
        elif member.get("source_kind") == "media_render":
            source = connection.execute(
                "SELECT p.work_id FROM media_renders r JOIN media_projects p ON p.id=r.project_id AND p.workspace_id=r.workspace_id "
                "WHERE r.id=? AND r.workspace_id=?",
                (member.get("source_id"), publication["workspace_id"]),
            ).fetchone()
            if source and source["work_id"]:
                work_ids.add(source["work_id"])
    for dependency in content.get("dependencies", []):
        if isinstance(dependency, dict) and isinstance(dependency.get("id"), str):
            source = connection.execute(
                "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
                (dependency["id"], publication["workspace_id"]),
            ).fetchone()
            if source and source["work_id"]:
                work_ids.add(source["work_id"])
    for reference in content.get("subject_refs", []):
        if isinstance(reference, dict) and isinstance(reference.get("work_id"), str):
            work_ids.add(reference["work_id"])
    return work_ids


def _publication_primary_work_id(connection, publication) -> str | None:
    kind = publication["source_kind"]
    if kind == "artifact" and publication["artifact_id"]:
        row = connection.execute(
            "SELECT work_id FROM artifacts WHERE id=? AND workspace_id=?",
            (publication["artifact_id"], publication["workspace_id"]),
        ).fetchone()
    elif kind == "media_material":
        row = connection.execute(
            "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
    elif kind == "media_project":
        row = connection.execute(
            "SELECT work_id FROM media_projects WHERE id=? AND workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
    elif kind == "media_render":
        row = connection.execute(
            "SELECT p.work_id FROM media_renders r JOIN media_projects p ON p.id=r.project_id AND p.workspace_id=r.workspace_id "
            "WHERE r.id=? AND r.workspace_id=?",
            (publication["source_id"], publication["workspace_id"]),
        ).fetchone()
    else:
        row = None
    return row["work_id"] if row is not None else None


def _publication_subject_ref_snapshots(connection, identity, publication, content: dict) -> list[dict]:
    from .business import load_business_subject_context

    target_work_id = _publication_primary_work_id(connection, publication)
    snapshots = []
    for reference in content.get("subject_refs", []):
        work_id = reference.get("work_id") or target_work_id
        subject = load_business_subject_context(
            connection, identity, reference["subject_id"], work_id=work_id,
        )
        if subject["directory_state"] != "active" or subject.get("canonical_subject_id"):
            raise ApiError("SUBJECT_NOT_CURRENT", "Choose the current active Subject identity before publishing", 409)
        if subject["version"] != reference["subject_version"]:
            raise ApiError("SUBJECT_VERSION_CONFLICT", "Review the changed Subject before using it in this publication", 409)
        for evidence in subject.get("evidence", []):
            source = evidence.get("source") if isinstance(evidence, dict) else None
            material_id = source.get("material_id") if isinstance(source, dict) else None
            if isinstance(material_id, str):
                require_material_access(connection, identity, material_id, target_work_id=work_id)
        snapshots.append({
            "ref_id": reference["ref_id"],
            "subject_id": reference["subject_id"],
            "subject_version": reference["subject_version"],
            "work_id": work_id,
            "relationship": reference["relationship"],
            "relationship_origin": "author_selected_unverified",
            "subject_kind": subject["kind"],
            "subject_name": subject["name"],
        })
    return snapshots


def _validate_publication_source_access(connection, identity, publication, content: dict, media_members: list[dict]) -> None:
    """Recheck every selected source's current access and same-client boundary."""
    from .community_media import require_media_source_access

    workspace_id = identity["workspace"]["id"]
    target_work_id = _publication_primary_work_id(connection, publication)
    commission_ids = set()
    for work_id in _publication_source_work_ids(connection, publication, content, media_members):
        commission_id = work_commission(connection, work_id)
        if commission_id:
            commission_ids.add(commission_id)
    if len(commission_ids) > 1:
        raise ApiError("CLIENT_SCOPE_MISMATCH", "A publication cannot combine selected sources from different client scopes", 403)

    for member in media_members:
        source_kind = member.get("source_kind")
        source_workspace_id = member.get("source_workspace_id")
        source_id = member.get("source_id")
        if source_workspace_id != workspace_id or not isinstance(source_id, str):
            raise _not_found()
        if source_kind == "material":
            source = require_material_access(
                connection, identity, source_id, target_work_id=target_work_id,
            )
            if (
                member.get("source_version") != 1
                or member.get("sha256") != source["sha256"]
                or member.get("size_bytes") != source["size_bytes"]
            ):
                raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "A selected Material source changed after it was added", 409)
        elif source_kind in {"media_project", "media_render"}:
            require_media_source_access(
                connection, identity,
                {
                    "workspace_id": source_workspace_id,
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "source_version": member.get("source_version"),
                },
            )
        elif source_kind != "inline":
            raise ApiError("COMMUNITY_MEDIA_SOURCE_INVALID", "A selected media member has an unsupported source", 409)

    for dependency in content.get("dependencies", []):
        if not isinstance(dependency, dict) or not isinstance(dependency.get("id"), str):
            continue
        source = require_material_access(
            connection, identity, dependency["id"], target_work_id=target_work_id,
        )
        if dependency.get("version") != 1 or dependency.get("sha256") != source["sha256"]:
            raise ApiError("PUBLICATION_SOURCE_CHANGED", "A selected source dependency changed after it was recorded", 409)
    _publication_subject_ref_snapshots(connection, identity, publication, content)


def _require_client_publication_permission(connection, publication, content: dict, media_members: list[dict]) -> bool:
    if publication["visibility"] == "private":
        return False
    has_client_scope = any(
        work_commission(connection, work_id)
        for work_id in _publication_source_work_ids(connection, publication, content, media_members)
    )
    if has_client_scope and content.get("privacy_review", {}).get("client_permission") is not True:
        raise ApiError("CLIENT_PUBLICATION_PERMISSION_REQUIRED", "Client work requires explicit permission for this selected content", 403)
    return has_client_scope


def _publication_review_authorized(connection, identity, publication, content: dict, media_members: list[dict]) -> bool:
    """Require a current client-scope reviewer, in addition to community moderation."""
    commission_ids = {
        commission_id
        for work_id in _publication_source_work_ids(connection, publication, content, media_members)
        if (commission_id := work_commission(connection, work_id)) is not None
    }
    for commission_id in commission_ids:
        commission = connection.execute(
            "SELECT status FROM client_commissions WHERE id=? AND workspace_id=?",
            (commission_id, publication["workspace_id"]),
        ).fetchone()
        member = connection.execute(
            "SELECT role,active FROM commission_members WHERE commission_id=? AND user_id=?",
            (commission_id, identity["user"]["id"]),
        ).fetchone()
        if (
            commission is None or commission["status"] != "active" or member is None
            or not member["active"] or member["role"] not in {"owner", "reviewer"}
        ):
            return False
    return True


def _require_human_review_authority_current(app, connection, publication, content: dict, media_members: list[dict], review) -> None:
    reviewer_id = review["reviewer_id"]
    reviewer = {"workspace": {"id": publication["workspace_id"]}, "user": {"id": reviewer_id}}
    if (
        reviewer_id not in app.config.get("COMMUNITY_MODERATOR_USER_IDS", ())
        or not _publication_review_authorized(connection, reviewer, publication, content, media_members)
    ):
        raise ApiError("REVIEW_AUTHORITY_REVOKED", "The reviewer no longer has current authority for this client scope", 409)


def _validate_publication_source_rights(connection, identity, publication, content: dict) -> None:
    if publication["source_kind"] != "artifact" or not publication["artifact_id"]:
        return
    artifact = connection.execute(
        "SELECT work_id FROM artifacts WHERE id=? AND workspace_id=?",
        (publication["artifact_id"], publication["workspace_id"]),
    ).fetchone()
    if artifact is None:
        raise _not_found()
    version = connection.execute(
        "SELECT citations_json,input_snapshot_json FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
        (publication["artifact_id"], publication["workspace_id"], publication["artifact_version"]),
    ).fetchone()
    if version is None:
        raise ApiError("PUBLICATION_SOURCE_VERSION_MISSING", "The selected Artifact source version is unavailable", 409)
    try:
        source_snapshot = json.loads(version["input_snapshot_json"] or "{}")
        citations = json.loads(version["citations_json"] or "[]")
    except (TypeError, ValueError) as error:
        raise ApiError("PUBLICATION_SOURCE_VERSION_INVALID", "The selected Artifact source metadata cannot be read", 409) from error
    sources = {}
    if isinstance(source_snapshot, dict):
        for source in source_snapshot.get("materials", []):
            if isinstance(source, dict):
                material_id = source.get("id") or source.get("material_id")
                if isinstance(material_id, str):
                    sources[material_id] = source.get("sha256")
    if isinstance(citations, list):
        for citation in citations:
            if isinstance(citation, dict) and isinstance(citation.get("material_id"), str):
                sources.setdefault(citation["material_id"], None)
    for material_id, expected_sha256 in sources.items():
        material = require_material_access(
            connection, identity, material_id,
            target_work_id=artifact["work_id"],
        )
        if expected_sha256 and material["sha256"] != expected_sha256:
            raise ApiError("PUBLICATION_SOURCE_CHANGED", "A cited Artifact source changed; create a current version before publishing", 409)
        if publication["visibility"] == "private":
            continue
        dependency = next((item for item in content.get("dependencies", [])
                           if isinstance(item, dict) and item.get("id") == material_id
                           and item.get("version") == 1 and item.get("sha256") == material["sha256"]), None)
        if dependency is None:
            raise ApiError("PUBLICATION_SOURCE_RIGHTS_REQUIRED", "Add a separate rights record for every source used by this Artifact", 403,
                           {"material_id": material_id})
        rights = dependency.get("license", {})
        if not rights.get("view") or not rights.get("redistribute"):
            raise ApiError("PUBLICATION_SOURCE_RIGHTS_INSUFFICIENT", "A cited source does not grant the selected public distribution", 403,
                           {"material_id": material_id})


def _automatic_publication_eligibility(connection, identity, publication, content: dict, media_members: list[dict]) -> tuple[str, str]:
    """Choose automatic or human review from checked rights, scope and source state."""
    if publication["visibility"] != "private" and content.get("privacy_review", {}).get("selected_content_only") is not True:
        raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected public content before publication", 400)
    _check_license(content.get("license", {}), "view")
    if publication["kind"] == "work":
        _validate_publication_blocks_for_submit(content, media_members)
    _validate_series_members(connection, identity, content, publication["visibility"], require_available=True)
    if _require_client_publication_permission(connection, publication, content, media_members):
        return "review_required", "客户委托范围需要由适任人员检查当前批准记录。"
    return "auto_eligible", "publication-source-rights-privacy/1"


def _continue_publish_jobs_after_review(connection, publication_id: str, review: dict, approved_version: int) -> None:
    workspace_id = review["workspace_id"] if "workspace_id" in review.keys() else None
    if workspace_id is None:
        row = connection.execute("SELECT workspace_id FROM publications WHERE id=?", (publication_id,)).fetchone()
        workspace_id = row["workspace_id"] if row is not None else None
    if workspace_id is None:
        return
    jobs = connection.execute(
        "SELECT * FROM jobs WHERE workspace_id=? AND kind=? AND status IN ('waiting_for_input','paused','pause_requested','interrupted')",
        (workspace_id, _COMMUNITY_PUBLISH_JOB_KIND),
    ).fetchall()
    now = utc_now()
    for job in jobs:
        try:
            payload = json.loads(job["payload_json"])
        except (TypeError, ValueError):
            continue
        if (
            payload.get("publication_id") != publication_id
            or payload.get("review_mode") != "human"
            or payload.get("review_state") != "pending"
            or payload.get("review_submitted_version") != review["publication_version"]
        ):
            continue
        payload["review_id"] = review["id"]
        payload["review_state"] = review["decision"]
        payload["approved_publication_version"] = approved_version if review["decision"] == "approved" else None
        status = job["status"]
        error_code = job["error_code"]
        error_message = job["error_message"]
        progress_label = job["progress_label"]
        pause_requested = job["pause_requested"]
        suspended_from = job["suspended_from"]
        if review["decision"] == "approved":
            if status == "waiting_for_input" and not job["cancel_requested"] and not job["pause_requested"]:
                status = "queued"
                progress_label = "审核通过，继续原发布意图"
        elif not job["cancel_requested"]:
            status = "failed"
            error_code = "PUBLICATION_REVIEW_REJECTED"
            error_message = "The exact publication review rejected this publish intent; no release was created."
            progress_label = "审核未通过，未创建发布版"
            pause_requested = 0
            suspended_from = None
        connection.execute(
            "UPDATE jobs SET payload_json=?,status=?,error_code=?,error_message=?,progress_label=?,pause_requested=?,suspended_from=?,updated_at=? WHERE id=? AND lease_generation=?",
            (_json(payload), status, error_code, error_message, progress_label, pause_requested, suspended_from, now, job["id"], job["lease_generation"]),
        )


def _publish_request_fingerprint(publication_id: str, identity: dict, data: dict) -> str:
    return _request_fingerprint({
        "operation": "publication.publish",
        "publication_id": publication_id,
        "actor_id": identity["user"]["id"],
        "request": data,
    })


def _operation_id(workspace_id: str, key: str, fingerprint: str, requested: str | None) -> str:
    if requested is not None:
        return _string(requested, "operation_id", maximum=200)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oevira:publication-publish:{workspace_id}:{key}:{fingerprint}"))


def _publication_rights_context(connection, identity, publication, content: dict, media_members: list[dict]) -> list[dict]:
    """Freeze the current client-scope authority that governs selected sources."""
    scopes = []
    for work_id in sorted(_publication_source_work_ids(connection, publication, content, media_members)):
        relation = connection.execute(
            "SELECT commission_id FROM work_commissions WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if relation is None:
            scopes.append({"work_id": work_id, "commission": None})
            continue
        commission = connection.execute(
            "SELECT id,status,version,scope_json FROM client_commissions WHERE id=? AND workspace_id=?",
            (relation["commission_id"], publication["workspace_id"]),
        ).fetchone()
        member = connection.execute(
            "SELECT role,active FROM commission_members WHERE commission_id=? AND user_id=?",
            (relation["commission_id"], publication["author_id"]),
        ).fetchone()
        scopes.append({
            "work_id": work_id,
            "commission": None if commission is None else {
                "id": commission["id"], "status": commission["status"],
                "version": commission["version"],
                "scope_digest": _digest(json.loads(commission["scope_json"] or "{}")),
                "author_membership": None if member is None else {"role": member["role"], "active": bool(member["active"])},
            },
        })
    return scopes


def _publication_rights_digest(
    publication, content: dict, *, connection=None, identity=None, media_members: list[dict] | None = None,
) -> str:
    rights = {
        "visibility": publication["visibility"],
        "license": content.get("license", {}),
        "dependencies": content.get("dependencies", []),
        "privacy_review": content.get("privacy_review", {}),
    }
    if connection is not None and identity is not None:
        rights["source_authority"] = _publication_rights_context(
            connection, identity, publication, content, media_members or [],
        )
    return _digest(rights)


def _publication_member_selection(media_members: list[dict], content: dict) -> dict:
    media_items = []
    for member in media_members:
        metadata = member.get("metadata") if isinstance(member.get("metadata"), dict) else None
        if metadata is None:
            try:
                metadata = json.loads(member.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
        media_items.append({
            "member_id": member.get("member_id"), "role": member.get("role"),
            "source_kind": member.get("source_kind"), "source_id": member.get("source_id"),
            "source_version": member.get("source_version"), "sha256": member.get("sha256"),
            "item_id": metadata.get("item_id") if isinstance(metadata, dict) else None,
            "position": metadata.get("position") if isinstance(metadata, dict) else None,
            "dependency_ids": metadata.get("dependency_ids", []) if isinstance(metadata, dict) else [],
        })
    series = content.get("series") if isinstance(content.get("series"), dict) else None
    selected_member_ids = [item["member_id"] for item in media_items if isinstance(item.get("member_id"), str)]
    if isinstance(series, dict):
        selected_member_ids.extend(item["member_id"] for item in series.get("members", []) if isinstance(item, dict) and isinstance(item.get("member_id"), str))
    return {"media": media_items, "series": series, "selected_member_ids": selected_member_ids}


def _ensure_operation_id_unused(connection, workspace_id: str, operation_id: str) -> None:
    release = connection.execute(
        "SELECT id FROM publication_releases WHERE operation_id=? LIMIT 1",
        (operation_id,),
    ).fetchone()
    if release is not None:
        raise ApiError("OPERATION_ID_CONFLICT", "This operation_id already identifies another publication effect", 409)
    for job in connection.execute(
        "SELECT payload_json FROM jobs WHERE workspace_id=? AND kind=?",
        (workspace_id, _COMMUNITY_PUBLISH_JOB_KIND),
    ).fetchall():
        try:
            payload = json.loads(job["payload_json"])
        except (TypeError, ValueError):
            continue
        if payload.get("operation_id") == operation_id:
            raise ApiError("OPERATION_ID_CONFLICT", "This operation_id already identifies another publication intent", 409)
    for prior in connection.execute(
        "SELECT resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_publish' AND resource_type='publish_review_required'",
        (workspace_id,),
    ).fetchall():
        try:
            receipt = json.loads(prior["resource_id"])
        except (TypeError, ValueError):
            continue
        if isinstance(receipt, dict) and (
            receipt.get("operation_id") == operation_id
            or (isinstance(receipt.get("intent"), dict) and receipt["intent"].get("operation_id") == operation_id)
        ):
            raise ApiError("OPERATION_ID_CONFLICT", "This operation_id already identifies another publication intent", 409)


def _publication_operation_intent(
    connection, identity, publication, content: dict, media_members: list[dict], *,
    operation_id: str, idempotency_key: str, policy_version: str, review_mode: str,
    decision: str, review_id: str | None = None,
) -> dict:
    members = _publication_member_selection(media_members, content)
    content_digest = _publication_content_hash(connection, publication, content)
    rights_context = _publication_rights_context(connection, identity, publication, content, media_members)
    rights_digest = _publication_rights_digest(
        publication, content, connection=connection, identity=identity, media_members=media_members,
    )
    members_digest = _digest(members)
    coauthors = _publication_coauthors_for_version(connection, publication, content=content)
    coauthors_digest = _digest(coauthors)
    return {
        "schema": "oevira.operation_intent/1",
        "operation_id": operation_id,
        "action": "publication.publish",
        "actor": {"user_id": identity["user"]["id"], "workspace_id": identity["workspace"]["id"]},
        "publication": {"id": publication["id"], "base_version": publication["version"]},
        "source": {
            "kind": publication["source_kind"], "id": publication["source_id"],
            "version": publication["source_version"],
            "artifact_id": publication["artifact_id"], "artifact_version": publication["artifact_version"],
        },
        "audience": {"visibility": publication["visibility"]},
        "content_digest": content_digest,
        "rights_digest": rights_digest,
        "rights_context": rights_context,
        "members_digest": members_digest,
        "coauthors_digest": coauthors_digest,
        "selected_member_ids": members["selected_member_ids"],
        "members": members,
        "review_id": review_id,
        "review_mode": review_mode,
        "decision": decision,
        "policy_version": policy_version,
        "idempotency_key_digest": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
        "created_at": utc_now(),
    }


def _validate_publish_digest(data: dict, field: str, actual: str) -> None:
    supplied = data.get(field)
    if supplied is None:
        return
    if not isinstance(supplied, str) or supplied != actual:
        raise ApiError("OPERATION_DIGEST_MISMATCH", f"{field} does not match the current publication version", 409)


def _queue_community_publish_job(
    connection, identity, publication, *, base_version: int, intent: dict,
    request_fingerprint: str, idempotency_key: str, review_mode: str,
    waiting_for_review: bool, review_id: str | None = None, review_reason: str | None = None,
    media_member_count: int = 0,
) -> dict:
    job_id, now = str(uuid.uuid4()), utc_now()
    payload = {
        "publication_id": publication["id"],
        "operation_id": intent["operation_id"],
        "base_publication_version": base_version,
        "intent": intent,
        "request_fingerprint": request_fingerprint,
        "idempotency_key_digest": intent["idempotency_key_digest"],
        "review_mode": review_mode,
        "review_id": review_id,
        "review_state": "needs_review" if waiting_for_review else "approved" if review_id else "not_required",
        "review_reason": review_reason,
        "review_submitted_version": None,
        "approved_publication_version": publication["version"] if review_id else None,
    }
    job_status = "queued"
    progress_total = max(1, media_member_count + 1)
    connection.execute(
        "INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,idempotency_key,progress_total,progress_label,created_at,updated_at) "
        "VALUES(?,?,?,NULL,?,?,?,?,?,?,?, ?,?)",
        (
            job_id, identity["workspace"]["id"], identity["user"]["id"], _COMMUNITY_PUBLISH_JOB_KIND,
            job_status, _json(payload), request_fingerprint, idempotency_key, progress_total,
        "等待预审作品来源后提交人工审核" if waiting_for_review else "准备核对作品来源并发布", now, now,
        ),
    )
    connection.execute(
        "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_publish',?,?,'community_publish_job',?,?)",
        (identity["workspace"]["id"], idempotency_key, request_fingerprint, job_id, now),
    )
    job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job


def _community_publish_job_response(job, operation_id: str, state: str, status: int = 202, *, reason: str | None = None):
    from .api import _job_data

    result = _job_data(job)
    result["publication_operation"] = {
        "operation_id": operation_id,
        "state": state,
        "delivery": "pending",
    }
    if reason:
        result["publication_operation"]["reason"] = reason
    return _success(result, status)


def validate_community_publish_resume(connection, identity, row, resume_context):
    """Resume only the exact Publication version and approved scope in its Job."""
    if resume_context:
        raise ApiError("INVALID_INPUT", "Publication publish jobs keep their original content and rights; create a new intent after edits", 400)
    payload = json.loads(row["payload_json"])
    publication_id = payload.get("publication_id")
    base_version = payload.get("base_publication_version")
    if not isinstance(publication_id, str) or type(base_version) is not int:
        raise ApiError("JOB_INPUT_VERSION_UNAVAILABLE", "The saved publication intent is incomplete", 409)
    publication = _owned_publication(connection, identity, publication_id, write=True)
    content = json.loads(publication["content_json"])
    from .community_media import _draft_members

    media_members = _draft_members(connection, publication_id, publication["version"])
    intent = payload.get("intent")
    if not isinstance(intent, dict) or intent.get("operation_id") != payload.get("operation_id"):
        raise ApiError("JOB_INPUT_VERSION_UNAVAILABLE", "The saved publication operation intent is invalid", 409)
    content_digest = _publication_content_hash(connection, publication, content)
    rights_digest = _publication_rights_digest(
        publication, content, connection=connection, identity=identity, media_members=media_members,
    )
    members_digest = _digest(_publication_member_selection(media_members, content))
    coauthors_digest = _digest(_publication_coauthors_for_version(connection, publication, content=content))
    if (content_digest, rights_digest, members_digest, coauthors_digest) != (
        intent.get("content_digest"), intent.get("rights_digest"), intent.get("members_digest"), intent.get("coauthors_digest"),
    ):
        raise ApiError("PUBLICATION_INPUT_CHANGED", "Publication content, rights or selected members changed while the intent was stopped", 409)
    if intent.get("audience", {}).get("visibility") != publication["visibility"]:
        raise ApiError("PUBLICATION_AUDIENCE_CHANGED", "The publication audience changed while the intent was stopped", 409)
    _validate_series_members(connection, identity, content, publication["visibility"], require_available=False)
    _validate_publication_source_rights(connection, identity, publication, content)
    _require_client_publication_permission(connection, publication, content, media_members)
    review_state = payload.get("review_state")
    if payload.get("review_mode") == "human":
        if review_state == "needs_review":
            if publication["status"] != "draft" or publication["version"] != base_version:
                raise ApiError("PUBLICATION_VERSION_CONFLICT", "The draft changed before the publish intent could be submitted for review", 409)
            return "queued"
        if review_state == "pending":
            if (
                publication["status"] != "submitted"
                or publication["version"] != payload.get("review_submitted_version")
                or payload.get("review_submitted_version") != base_version + 1
            ):
                raise ApiError("PUBLICATION_REVIEW_STATE_CHANGED", "The submitted version changed while its publish intent awaited review", 409)
            return "waiting_for_input"
        if review_state == "rejected":
            raise ApiError("PUBLICATION_REVIEW_REJECTED", "The human reviewer rejected this publish intent; no release was created", 409)
        if review_state != "approved":
            raise ApiError("PUBLICATION_REVIEW_REQUIRED", "A current approval is required before continuing this intent", 409)
        review = connection.execute(
            "SELECT * FROM publication_reviews WHERE id=? AND publication_id=? AND decision='approved' AND review_mode='human'",
            (payload.get("review_id"), publication_id),
        ).fetchone()
        approved_version = payload.get("approved_publication_version")
        resumed_publish = payload.get("review_submitted_version") is not None
        expected_approved_version = base_version + 2 if resumed_publish else base_version
        if (
            publication["status"] != "approved" or publication["version"] != approved_version
            or approved_version != expected_approved_version
            or (resumed_publish and payload.get("review_submitted_version") != base_version + 1)
            or review is None or review["publication_version"] + 1 != approved_version
            or review["content_hash"] != _publication_content_hash(connection, publication, content)
            or review["rights_digest"] != rights_digest
            or review["members_digest"] != members_digest
            or review["audience_visibility"] != publication["visibility"]
        ):
            raise ApiError("REVIEW_NOT_CURRENT", "The approval no longer covers this exact publication intent", 409)
    else:
        if publication["status"] != "draft" or publication["version"] != base_version:
            raise ApiError("PUBLICATION_VERSION_CONFLICT", "The draft changed while the publish intent was stopped", 409)
    return "queued"


class _CommunityPublishStopped(Exception):
    """Leave pause/cancel confirmation to the shared Job control owner."""


def _check_community_publish_running(store, claim: dict) -> None:
    row = store.row(
        "SELECT status,cancel_requested,pause_requested,lease_generation FROM jobs WHERE id=? AND workspace_id=?",
        (claim["id"], claim["workspace_id"]),
    )
    if (
        row is None or row["lease_generation"] != claim["generation"]
        or row["status"] != "running" or row["cancel_requested"] or row["pause_requested"]
    ):
        raise _CommunityPublishStopped()


def _validate_community_publish_inputs(connection, identity, publication, content: dict, media_members: list[dict]) -> None:
    from .community_media import validate_draft_media_licenses

    _check_license(content.get("license", {}), "view")
    validate_draft_media_licenses(content, media_members, "view")
    _validate_publication_source_access(connection, identity, publication, content, media_members)
    _validate_publication_source_rights(connection, identity, publication, content)
    _require_client_publication_permission(connection, publication, content, media_members)
    _validate_series_members(connection, identity, content, publication["visibility"], require_available=False)
    if publication["kind"] == "work":
        _validate_publication_blocks_for_submit(content, media_members)
    if publication["visibility"] != "private" and content.get("privacy_review", {}).get("selected_content_only") is not True:
        raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected content before publication", 400)


def _create_publication_release(
    connection, identity, publication, content: dict, media_members: list[dict], intent: dict, review, *,
    idempotency_key: str, request_fingerprint: str, job_id: str | None = None,
) -> tuple[dict, str | None]:
    if review is None:
        raise ApiError("REVIEW_NOT_CURRENT", "The selected publication version has no confirmed review record", 409)
    content_hash = _publication_content_hash(connection, publication, content)
    rights_digest = _publication_rights_digest(
        publication, content, connection=connection, identity=identity, media_members=media_members,
    )
    members_digest = _digest(_publication_member_selection(media_members, content))
    subject_refs = _publication_subject_ref_snapshots(connection, identity, publication, content)
    coauthors = _publication_coauthors_for_version(connection, publication, content=content)
    coauthors_digest = _digest(coauthors)
    expected_version = review["publication_version"] if review["review_mode"] == "automatic" else review["publication_version"] + 1
    if (
        review["content_hash"] != content_hash
        or review["rights_digest"] != rights_digest
        or review["members_digest"] != members_digest
        or review["audience_visibility"] != publication["visibility"]
        or expected_version != publication["version"]
        or intent.get("content_digest") != content_hash
        or intent.get("rights_digest") != rights_digest
        or intent.get("members_digest") != members_digest
        or intent.get("coauthors_digest") != coauthors_digest
        or intent.get("audience", {}).get("visibility") != publication["visibility"]
    ):
        raise ApiError("PUBLICATION_INPUT_CHANGED", "Content, rights, audience or selected members no longer match the publish intent", 409)
    _check_license(content.get("license", {}), "view")
    intent = {**intent, "review_id": review["id"]}
    if review["review_mode"] == "human":
        intent["decision"] = "human_approved"
    identifier, now = str(uuid.uuid4()), utc_now()
    share_token = secrets.token_urlsafe(32) if publication["visibility"] == "unlisted" else None
    decision = "human_approved" if review["review_mode"] == "human" else "auto_eligible"
    authorship_snapshot = {
        "creator": _publication_creator(connection, identity["user"]["id"]),
        "coauthors": [{"display_name": item["display_name"]} for item in coauthors],
    }
    receipt = {
        "schema": "oevira.operation_receipt/1",
        "operation_id": intent["operation_id"],
        "status": "completed",
        "effect_phase": "oevira_release_created",
        "decision": decision,
        "publication_id": publication["id"],
        "publication_version": publication["version"],
        "release_id": identifier,
        "review_id": review["id"],
        "review_mode": review["review_mode"],
        "content_digest": content_hash,
        "rights_digest": rights_digest,
        "members_digest": members_digest,
        "coauthors_digest": coauthors_digest,
        "visibility": publication["visibility"],
        "policy_version": intent.get("policy_version"),
        "delivery": "local_review_only",
        "checks": {
            "source_and_member_access": "passed",
            "rights": "passed",
            "content_structure": "passed",
            "client_scope": "permission_checked" if review["review_mode"] == "human" else "not_applicable",
            "content_quality_review": "human_approved" if review["review_mode"] == "human" else "not_independently_reviewed",
        },
        "completed_at": now,
    }
    connection.execute(
        "INSERT INTO publication_releases(id,publication_id,publication_version,review_id,author_id,kind,visibility,snapshot_json,content_hash,share_token_hash,published_at,withdrawn_at,operation_id,intent_json,receipt_json,authors_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
        (
            identifier, publication["id"], publication["version"], review["id"], identity["user"]["id"],
            publication["kind"], publication["visibility"], _json(content), content_hash,
            hashlib.sha256(share_token.encode()).hexdigest() if share_token else None, now,
            intent["operation_id"], _json(intent), _json(receipt), _json(authorship_snapshot),
        ),
    )
    connection.executemany(
        "INSERT INTO publication_release_subject_refs(release_id,ref_id,subject_id,subject_version,work_id,relationship,relationship_origin,subject_kind,subject_name,position) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                identifier, item["ref_id"], item["subject_id"], item["subject_version"],
                item["work_id"], item["relationship"], item["relationship_origin"],
                item["subject_kind"], item["subject_name"], position,
            )
            for position, item in enumerate(subject_refs)
        ],
    )
    connection.executemany(
        "INSERT INTO publication_release_coauthors(release_id,user_id,display_name,accepted_version,accepted_scope_digest,accepted_at) VALUES(?,?,?,?,?,?)",
        [
            (
                identifier, item["user_id"], item["display_name"], item["accepted_version"],
                connection.execute(
                    "SELECT accepted_scope_digest FROM publication_coauthors WHERE publication_id=? AND user_id=? AND status='accepted'",
                    (publication["id"], item["user_id"]),
                ).fetchone()["accepted_scope_digest"],
                item["accepted_at"] or now,
            )
            for item in coauthors
        ],
    )
    from .community_media import copy_draft_media_to_release

    copy_draft_media_to_release(connection, publication["id"], publication["version"], identifier)
    connection.execute(
        "UPDATE publications SET status='published',version=version+1,updated_at=? WHERE id=? AND version=?",
        (now, publication["id"], publication["version"]),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("VERSION_CONFLICT", "The publication changed while its release was being created", 409)
    updated = connection.execute("SELECT * FROM publications WHERE id=?", (publication["id"],)).fetchone()
    _save_version(connection, updated, identity["user"]["id"])
    if job_id is None:
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_publish',?,?,'release',?,?)",
            (identity["workspace"]["id"], idempotency_key, request_fingerprint, identifier, now),
        )
    return connection.execute("SELECT * FROM publication_releases WHERE id=?", (identifier,)).fetchone(), share_token


def process_community_publish_job(app, claim: dict) -> None:
    """Verify selected publication inputs outside write locks, then commit once."""
    from .community_media import _draft_members, _verify_member_source
    from .worker import _advance, _set_job_error, claim_can_commit

    store = app.extensions["oevira.store"]
    job = store.row(
        "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
        (claim["id"], claim["workspace_id"]),
    )
    if job is None or job["status"] != "running" or job["lease_generation"] != claim["generation"]:
        return
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    payload = json.loads(job["payload_json"])
    publication_id = payload.get("publication_id")
    total = max(1, job["progress_total"] or 1)
    try:
        with store.transaction(immediate=False) as connection:
            validate_community_publish_resume(connection, identity, job, {})
            publication = _owned_publication(connection, identity, publication_id, write=True)
            content = json.loads(publication["content_json"])
            media_members = _draft_members(connection, publication_id, publication["version"])
            _validate_community_publish_inputs(connection, identity, publication, content, media_members)
        if not _advance(store, claim["id"], claim["generation"], 0, total, "核验选中来源与权限"):
            return
        for index, member in enumerate(media_members, start=1):
            _check_community_publish_running(store, claim)
            _verify_member_source(store, member, check_running=lambda: _check_community_publish_running(store, claim))
            if not _advance(store, claim["id"], claim["generation"], index, total, f"核对作品来源 {index}/{len(media_members)}"):
                return

        with store.transaction() as connection:
            current_job = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND workspace_id=?",
                (claim["id"], claim["workspace_id"]),
            ).fetchone()
            if not claim_can_commit(connection, claim):
                return
            validate_community_publish_resume(connection, identity, current_job, {})
            publication = _owned_publication(connection, identity, publication_id, write=True)
            content = json.loads(publication["content_json"])
            current_members = _draft_members(connection, publication_id, publication["version"])
            _validate_community_publish_inputs(connection, identity, publication, content, current_members)
            intent = json.loads(current_job["payload_json"]).get("intent")
            current_payload = json.loads(current_job["payload_json"])
            media_digest = _digest(_publication_member_selection(current_members, content))
            if media_digest != intent.get("members_digest"):
                raise ApiError("PUBLICATION_INPUT_CHANGED", "Selected media or series members changed during verification", 409)

            if current_payload.get("review_state") == "needs_review":
                if current_payload.get("review_mode") != "human" or publication["status"] != "draft":
                    raise ApiError("PUBLICATION_REVIEW_STATE_CHANGED", "The human review intent is no longer eligible for submission", 409)
                submitted_version = publication["version"] + 1
                connection.execute(
                    "UPDATE publications SET status='submitted',version=?,updated_at=? WHERE id=? AND version=?",
                    (submitted_version, utc_now(), publication_id, publication["version"]),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ApiError("VERSION_CONFLICT", "The publication changed before the review submission", 409)
                from .community_media import copy_draft_media_version

                copy_draft_media_version(connection, publication_id, publication["version"], submitted_version)
                submitted = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
                _save_version(connection, submitted, identity["user"]["id"])
                current_payload["review_state"] = "pending"
                current_payload["review_submitted_version"] = submitted_version
                connection.execute(
                    "UPDATE jobs SET status='waiting_for_input',payload_json=?,progress_completed=?,progress_total=?,progress_label='已送入人工审核，等待审核决定',updated_at=? WHERE id=? AND lease_generation=? AND status='running'",
                    (_json(current_payload), total, total, utc_now(), claim["id"], claim["generation"]),
                )
                return

            if current_payload.get("review_mode") == "automatic":
                route, _reason = _automatic_publication_eligibility(connection, identity, publication, content, current_members)
                if route != "auto_eligible":
                    raise ApiError("PUBLICATION_REVIEW_REQUIRED", "Current publication scope now requires human review; create a new publish intent", 409)
                review_id = str(uuid.uuid4())
                policy_version = intent.get("policy_version") or "publication-source-rights-privacy/1"
                review_rights_digest = _publication_rights_digest(
                    publication, content, connection=connection, identity=identity, media_members=current_members,
                )
                review_members_digest = _digest(_publication_member_selection(current_members, content))
                connection.execute(
                    "INSERT INTO publication_reviews(id,publication_id,publication_version,content_hash,decision,reason,reviewer_id,created_at,review_mode,policy_version,rights_digest,members_digest,audience_visibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        review_id, publication_id, publication["version"],
                        _publication_content_hash(connection, publication, content), "approved",
                        "自动核对通过：当前来源、可见范围、权利、正文结构和选中内容检查均成立；未作独立事实或专业质量验证。",
                        identity["user"]["id"], utc_now(), "automatic", policy_version,
                        review_rights_digest, review_members_digest, publication["visibility"],
                    ),
                )
                review = connection.execute("SELECT * FROM publication_reviews WHERE id=?", (review_id,)).fetchone()
            else:
                review = connection.execute(
                    "SELECT * FROM publication_reviews WHERE id=? AND publication_id=? AND decision='approved' AND review_mode='human'",
                    (current_payload.get("review_id"), publication_id),
                ).fetchone()
                if (
                    publication["status"] != "approved"
                    or publication["version"] != current_payload.get("approved_publication_version")
                    or current_payload.get("review_state") != "approved"
                    or review is None
                    or review["publication_version"] + 1 != publication["version"]
                ):
                    raise ApiError("REVIEW_NOT_CURRENT", "Human approval does not cover this exact publish intent", 409)
                _require_human_review_authority_current(
                    app, connection, publication, content, current_members, review,
                )

            release, share_token = _create_publication_release(
                connection, identity, publication, content, current_members, intent, review,
                idempotency_key=current_job["idempotency_key"],
                request_fingerprint=current_job["request_hash"], job_id=claim["id"],
            )
            current_payload["_publication_result"] = {
                "release_id": release["id"], "operation_id": intent["operation_id"], "delivery": "local_review_only",
            }
            if share_token:
                current_payload["_unlisted_share_token"] = share_token
            connection.execute(
                "UPDATE jobs SET status='completed',payload_json=?,progress_completed=?,progress_total=?,progress_label='发布版已保存',updated_at=? WHERE id=? AND lease_generation=? AND status='running' AND cancel_requested=0 AND pause_requested=0",
                (_json(current_payload), total, total, utc_now(), claim["id"], claim["generation"]),
            )
    except _CommunityPublishStopped:
        return
    except ApiError as error:
        _set_job_error(store, claim["id"], claim["generation"], error.code, error.message)
    except Exception:
        _set_job_error(store, claim["id"], claim["generation"], "COMMUNITY_PUBLISH_FAILED", "The exact publication could not be verified and committed; no partial release was returned.")


def is_public_read_route(path, method):
    method = method.upper()
    if path == "/api/discovery":
        return method in {"GET", "HEAD"}
    parts = path.strip("/").split("/")
    if method in {"GET", "HEAD"}:
        if len(parts) == 5 and parts[:4] == ["api", "community", "media", "streams"]:
            return True
        if len(parts) == 3 and parts[:2] == ["api", "creators"]:
            return True
        return len(parts) in {3, 4} and parts[:2] == ["api", "releases"] and (len(parts) == 3 or parts[3] == "comments")
    return method == "POST" and len(parts) == 6 and parts[:2] == ["api", "releases"] and parts[3] == "media" and parts[5] == "playback"


def prepare_account_deletion(connection, workspace_id, deleted_user_id, successor_user_id, tombstone_user_id, private_workspace=False):
    """Withdraw and erase the creator's source publication content; retain lawful copies."""
    publications = connection.execute("SELECT * FROM publications WHERE workspace_id=? AND author_id=?", (workspace_id, deleted_user_id)).fetchall()
    withdrawn_content = {"title": "已撤回作品", "summary": "", "body": "", "category": None, "tags": [], "dependencies": [],
                         "license": {"view": False, "download": False, "adapt": False, "commercial": False, "redistribute": False, "training": False, "attribution": "", "expires_at": None},
                         "erasure_state": "creator_account_deleted"}
    now = utc_now()
    release_ids = []
    release_licenses = {}
    for publication in publications:
        releases = connection.execute(
            "SELECT id,snapshot_json FROM publication_releases WHERE publication_id=?",
            (publication["id"],),
        ).fetchall()
        for release in releases:
            release_ids.append(release["id"])
            try:
                original_content = json.loads(release["snapshot_json"])
            except (TypeError, ValueError):
                original_content = {}
            license_record = original_content.get("license") if isinstance(original_content, dict) else None
            if isinstance(license_record, dict):
                release_licenses[release["id"]] = license_record
            erased_release = {
                **withdrawn_content,
                "retained_license": license_record if isinstance(license_record, dict) else None,
            }
            connection.execute(
                "UPDATE publication_releases SET withdrawn_at=COALESCE(withdrawn_at,?),snapshot_json=?,author_id=?,authors_json=? WHERE id=?",
                (now, _json(erased_release), tombstone_user_id,
                 _json({"creator": {"user_id": tombstone_user_id, "display_name": "已撤回作者"}, "coauthors": []}),
                 release["id"]),
            )
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_media_drafts'").fetchone():
            connection.execute("DELETE FROM publication_media_drafts WHERE publication_id=?", (publication["id"],))
        # Each historical version remains a version record, with private/public text erased.
        versions = connection.execute("SELECT version,snapshot_json,created_by FROM publication_versions WHERE publication_id=?", (publication["id"],)).fetchall()
        for version in versions:
            historical = json.loads(version["snapshot_json"])
            original_content = historical.get("content")
            retained_license = original_content.get("license") if isinstance(original_content, dict) else None
            historical["content"] = withdrawn_content
            if isinstance(retained_license, dict):
                historical["retained_license"] = retained_license
            historical.pop("source", None)
            historical.pop("artifact_id", None)
            historical.pop("artifact_version", None)
            historical["erased_at"] = now
            connection.execute("UPDATE publication_versions SET snapshot_json=?,created_by=? WHERE publication_id=? AND version=?", (_json(historical), tombstone_user_id if version["created_by"] == deleted_user_id else version["created_by"], publication["id"], version["version"]))
        connection.execute("UPDATE publications SET author_id=?,content_json=?,status='withdrawn',version=version+1,updated_at=? WHERE id=?", (tombstone_user_id, _json(withdrawn_content), now, publication["id"]))
        _save_version(connection, connection.execute("SELECT * FROM publications WHERE id=?", (publication["id"],)).fetchone(), tombstone_user_id)
    connection.execute("UPDATE release_comments SET body='',anchor_json='{}',withdrawn_at=COALESCE(withdrawn_at,?),author_id=? WHERE author_id=?", (now, tombstone_user_id, deleted_user_id))
    connection.execute("DELETE FROM community_items WHERE user_id=?", (deleted_user_id,))
    connection.execute("DELETE FROM community_follows WHERE follower_id=? OR creator_id=?", (deleted_user_id, deleted_user_id))
    connection.execute("UPDATE publication_reports SET reported_by=? WHERE reported_by=?", (tombstone_user_id, deleted_user_id))
    connection.execute("UPDATE publication_reports SET resolved_by=? WHERE resolved_by=?", (tombstone_user_id, deleted_user_id))
    connection.execute("UPDATE publication_reviews SET reviewer_id=? WHERE reviewer_id=?", (tombstone_user_id, deleted_user_id))
    connection.execute("UPDATE publication_appeals SET created_by=? WHERE created_by=?", (tombstone_user_id, deleted_user_id))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_coauthors'").fetchone():
        connection.execute("UPDATE OR IGNORE publication_coauthors SET user_id=? WHERE workspace_id=? AND user_id=?", (tombstone_user_id, workspace_id, deleted_user_id))
        connection.execute("DELETE FROM publication_coauthors WHERE workspace_id=? AND user_id=?", (workspace_id, deleted_user_id))
        connection.execute("UPDATE publication_coauthors SET invited_by=? WHERE workspace_id=? AND invited_by=?", (tombstone_user_id, workspace_id, deleted_user_id))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_coauthor_events'").fetchone():
        connection.execute("UPDATE publication_coauthor_events SET user_id=? WHERE workspace_id=? AND user_id=?", (tombstone_user_id, workspace_id, deleted_user_id))
        connection.execute("UPDATE publication_coauthor_events SET actor_id=? WHERE workspace_id=? AND actor_id=?", (tombstone_user_id, workspace_id, deleted_user_id))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_release_coauthors'").fetchone():
        connection.execute("UPDATE OR IGNORE publication_release_coauthors SET user_id=?,display_name='已注销创作者' WHERE user_id=?", (tombstone_user_id, deleted_user_id))
        connection.execute("DELETE FROM publication_release_coauthors WHERE user_id=?", (deleted_user_id,))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_features'").fetchone():
        connection.execute("UPDATE release_features SET featured_by=? WHERE featured_by=?", (tombstone_user_id, deleted_user_id))
    if release_ids:
        markers = ",".join("?" for _ in release_ids)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_features'").fetchone():
            connection.execute(f"DELETE FROM release_features WHERE release_id IN ({markers})", tuple(release_ids))
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_release_subject_refs'").fetchone():
            connection.execute(f"DELETE FROM publication_release_subject_refs WHERE release_id IN ({markers})", tuple(release_ids))
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_release_coauthors'").fetchone():
            connection.execute(f"DELETE FROM publication_release_coauthors WHERE release_id IN ({markers})", tuple(release_ids))
    from .community_media import erase_publication_media

    erase_publication_media(
        connection, [row["id"] for row in publications], release_ids,
        release_licenses=release_licenses,
    )
    return {"file_refs": [], "withdrawn_publication_ids": [row["id"] for row in publications], "previously_licensed_private_copies": "preserved",
            "publication_source_text": "erased", "draft_transfer": "not_implicitly_granted"}


def create_native_publication(store, identity: dict, data: dict, idempotency_key: str) -> dict:
    """Create an original private/public work or experience without an Artifact import."""
    if not isinstance(data, dict) or set(data) != {"kind", "visibility", "content"}:
        raise ApiError("INVALID_INPUT", "A native Publication needs kind, visibility and content", 400)
    if data["kind"] not in {"work", "experience"} or data["visibility"] not in {"private", "team", "unlisted", "public"}:
        raise ApiError("INVALID_INPUT", "Native Publication kind or visibility is unsupported", 400)
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if not 1 <= len(key) <= 200:
        raise ApiError("IDEMPOTENCY_REQUIRED", "Publication creation requires an idempotency key", 400)
    private_draft = data["visibility"] == "private"
    content = _content(
        data["content"], data["kind"], allow_empty_body=private_draft,
        private_work_draft=private_draft,
    )
    if not private_draft and not content["body"].strip() and not content.get("series", {}).get("members"):
        raise ApiError("PUBLICATION_CONTENT_EMPTY", "Add text before creating a non-private native publication", 400)
    _json(content)
    fingerprint = _request_fingerprint(data)
    workspace_id, user_id = identity["workspace"]["id"], identity["user"]["id"]
    with store.transaction() as connection:
        prior = connection.execute(
            "SELECT request_hash,resource_type,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_create' AND key=?",
            (workspace_id, key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != fingerprint or prior["resource_type"] != "publication":
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another publication", 409)
            row = _owned_publication(connection, identity, prior["resource_id"])
            return {"publication": _draft_data(row, connection), "created": False}

        source_scope = {
            "workspace_id": workspace_id, "source_kind": "artifact", "source_id": None,
            "source_version": 1, "artifact_id": None, "artifact_version": None, "work_id": None,
        }
        source_work_ids = _publication_source_work_ids(connection, source_scope, content, [])
        if (
            data["visibility"] != "private"
            and any(work_commission(connection, work_id) for work_id in source_work_ids)
            and content.get("privacy_review", {}).get("client_permission") is not True
        ):
            raise ApiError("CLIENT_PUBLICATION_PERMISSION_REQUIRED", "Client work requires explicit permission for this selected public content", 403)
        _validate_series_members(connection, identity, content, data["visibility"], require_available=True)

        now, publication_id, artifact_id = utc_now(), str(uuid.uuid4()), str(uuid.uuid4())
        artifact_title = content["title"] or "作品"
        connection.execute(
            "INSERT INTO artifacts(id,workspace_id,work_id,owner_user_id,title,current_version,created_at,updated_at) VALUES(?,?,NULL,?,?,1,?,?)",
            (artifact_id, workspace_id, user_id, artifact_title, now, now),
        )
        connection.execute(
            "INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by,parent_version) VALUES(?,?,1,?,?, '[]', ?, 'Native publication draft', ?, ?, NULL)",
            (artifact_id, workspace_id, artifact_title, content["body"], _json({
                "kind": "community_native", "work": None, "materials": [],
                "community_native": {
                    "publication_id": publication_id,
                    "kind": data["kind"],
                    "blocks": content.get("blocks", []),
                    "experience": content.get("experience"),
                    "series": content.get("series"),
                    "summary": content["summary"],
                    "content_sha256": _digest(content),
                },
            }), now, user_id),
        )
        connection.execute(
            "INSERT INTO publications(id,workspace_id,author_id,artifact_id,artifact_version,source_kind,source_id,source_version,kind,version,status,visibility,content_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'artifact',?,?,?,1,'draft',?,?,?,?)",
            (publication_id, workspace_id, user_id, artifact_id, 1, artifact_id, 1,
             data["kind"], data["visibility"], _json(content), now, now),
        )
        row = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        _validate_publication_source_access(connection, identity, row, content, [])
        _save_version(connection, row, user_id)
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_create',?,?,'publication',?,?)",
            (workspace_id, key, fingerprint, publication_id, now),
        )
        return {"publication": _draft_data(row, connection), "created": True}


def create_private_publication_from_artifact(
    store, identity: dict, data: dict, idempotency_key: str, *, assistant_provenance: dict | None = None,
) -> dict:
    """Create one private publication draft from an exact authorized Artifact version."""
    expected = {"artifact_id", "artifact_version", "kind", "visibility", "content"}
    if not isinstance(data, dict) or set(data) != expected or data.get("visibility") != "private":
        raise ApiError("INVALID_INPUT", "Create a private draft from one exact Artifact version", 400)
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200:
        raise ApiError("IDEMPOTENCY_REQUIRED", "Publication creation requires an idempotency key", 400)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"work", "template", "experience"}:
        raise ApiError("INVALID_INPUT", "Unsupported publication kind", 400)
    artifact_id = _string(data.get("artifact_id"), "artifact_id", maximum=255)
    artifact_version = _version(data.get("artifact_version"))
    content = _content(
        data["content"], kind, allow_empty_body=True, private_work_draft=True,
    )
    if assistant_provenance is not None:
        if (not isinstance(assistant_provenance, dict)
                or set(assistant_provenance) != {"source"}
                or assistant_provenance.get("source") != "assistant_candidate"):
            raise ApiError("INVALID_INPUT", "Assistant provenance is invalid", 400)
    fingerprint_input = (
        data if assistant_provenance is None
        else {"request": data, "assistant_provenance": assistant_provenance}
    )
    fingerprint = _request_fingerprint(fingerprint_input)
    with store.transaction() as connection:
        prior = connection.execute(
            "SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='publication_create' AND key=?",
            (identity["workspace"]["id"], idempotency_key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another publication", 409)
            return {"publication": _draft_data(_owned_publication(connection, identity, prior["resource_id"]), connection),
                    "created": False}
        artifact = require_artifact_access(connection, identity, artifact_id, write=True)
        source = connection.execute(
            "SELECT 1 FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
            (artifact_id, identity["workspace"]["id"], artifact_version),
        ).fetchone()
        if source is None:
            raise _not_found()
        source_work_id = artifact["work_id"]
        source_scope = {
            "workspace_id": identity["workspace"]["id"], "source_kind": "artifact",
            "source_id": artifact_id, "source_version": artifact_version,
            "artifact_id": artifact_id, "artifact_version": artifact_version,
            "work_id": source_work_id,
        }
        _publication_source_work_ids(connection, source_scope, content, [])
        _validate_publication_source_access(connection, identity, source_scope, content, [])
        _validate_series_members(connection, identity, content, "private", require_available=True)
        if assistant_provenance is not None:
            content["assistant_provenance"] = {
                "origin": "assistant_candidate",
                "source_artifact_id": artifact_id, "source_artifact_version": artifact_version,
                "created_at": utc_now(),
            }
        now = utc_now()
        publication_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO publications(id,workspace_id,author_id,artifact_id,artifact_version,source_kind,source_id,source_version,kind,version,status,visibility,content_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'artifact',?,?,?,1,'draft','private',?,?,?)",
            (publication_id, identity["workspace"]["id"], identity["user"]["id"], artifact_id,
             artifact_version, artifact_id, artifact_version, kind, _json(content), now, now),
        )
        row = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        _save_version(connection, row, identity["user"]["id"])
        connection.execute(
            "INSERT INTO idempotency_records VALUES(?,'publication_create',?,?,'publication',?,?)",
            (identity["workspace"]["id"], idempotency_key, fingerprint, publication_id, now),
        )
        return {"publication": _draft_data(row, connection), "created": True}


def read_publication(store, identity: dict, publication_id: str) -> dict:
    with store.transaction(immediate=False) as connection:
        return _draft_data(_owned_publication(connection, identity, publication_id), connection)


def read_publication_version(store, identity: dict, publication_id: str, version: int) -> dict:
    if type(version) is not int or version < 1:
        raise ApiError("INVALID_INPUT", "Select a positive Publication version", 400)
    with store.transaction(immediate=False) as connection:
        _owned_publication(connection, identity, publication_id)
        row = connection.execute(
            "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
            (publication_id, version),
        ).fetchone()
        if row is None:
            raise _not_found()
        try:
            result = json.loads(row["snapshot_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError("PUBLICATION_VERSION_INVALID", "The saved Publication version cannot be read", 409) from exc
        if (not isinstance(result, dict) or result.get("id") != publication_id
                or result.get("version") != version):
            raise ApiError("PUBLICATION_VERSION_INVALID", "The saved Publication version is inconsistent", 409)
        return result


def revise_publication_command(store, identity: dict, publication_id: str, data: dict, idempotency_key: str) -> dict:
    if not isinstance(data, dict) or set(data) - {"base_version", "content", "visibility"}:
        raise ApiError("INVALID_INPUT", "Unsupported publication revision", 400)
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if not 1 <= len(key) <= 200:
        raise ApiError("IDEMPOTENCY_REQUIRED", "Publication revisions require an idempotency key", 400)
    fingerprint = _request_fingerprint({"publication_id": publication_id, "request": data})
    with store.transaction() as connection:
        row = _owned_publication(connection, identity, publication_id, write=True)
        prior = connection.execute(
            "SELECT request_hash,resource_type,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_revision' AND key=?",
            (identity["workspace"]["id"], key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != fingerprint or prior["resource_type"] != "publication_version":
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another Publication revision", 409)
            try:
                _publication_id, version_text = prior["resource_id"].rsplit(":", 1)
                version = _version(int(version_text))
            except (AttributeError, TypeError, ValueError) as error:
                raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The saved Publication revision is unavailable", 409) from error
            if _publication_id != publication_id:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another Publication", 409)
            saved = connection.execute(
                "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                (publication_id, version),
            ).fetchone()
            if saved is None:
                raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The saved Publication revision is unavailable", 409)
            return json.loads(saved["snapshot_json"])
        if _version(data.get("base_version")) != row["version"]:
            raise ApiError("VERSION_CONFLICT", "The publication changed", 409)
        prior_content = json.loads(row["content_json"])
        visibility = data.get("visibility", row["visibility"])
        if visibility not in {"private", "team", "unlisted", "public"}:
            raise ApiError("INVALID_INPUT", "Unsupported visibility", 400)
        private_work_draft = visibility == "private"
        native_owner = _native_artifact_owner(connection, identity, row, prior_content, write=True)
        if "content" in data and row["source_kind"] != "artifact":
            raw_content = data["content"]
            if not isinstance(raw_content, dict):
                raise ApiError("INVALID_INPUT", "Publication content must be an object", 400)
            raw_content = dict(raw_content)
            _merge_series_identity(raw_content, prior_content)
            raw_content.pop("media", None)
            prior_has_media_blocks = any(
                isinstance(block, dict) and block.get("kind") in {"image", "video", "audio"}
                for block in prior_content.get("blocks", [])
            )
            if row["kind"] == "work" and prior_has_media_blocks and "blocks" not in raw_content:
                raise ApiError("PUBLICATION_BLOCKS_REQUIRED", "Revise ordered blocks to preserve selected media members", 400)
            media_template = row["kind"] == "template" and isinstance(prior_content.get("template"), dict) and prior_content["template"].get("schema") == "oevira-media-template/1"
            if media_template:
                raw_content.pop("template", None)
            content = _content(
                raw_content, row["kind"], allow_empty_body=private_work_draft,
                private_work_draft=private_work_draft,
                allow_included_dependencies=True, require_template=not media_template,
            )
            if "media" in prior_content:
                content["media"] = prior_content["media"]
            if media_template:
                content["template"] = prior_content["template"]
        else:
            if "content" in data:
                raw_content = data["content"]
                if not isinstance(raw_content, dict):
                    raise ApiError("INVALID_INPUT", "Publication content must be an object", 400)
                raw_content = dict(raw_content)
                _merge_series_identity(raw_content, prior_content)
                content = _content(
                    raw_content, row["kind"], allow_empty_body=private_work_draft,
                    private_work_draft=private_work_draft,
                )
            else:
                content = prior_content
        if "assistant_provenance" in prior_content:
            content["assistant_provenance"] = prior_content["assistant_provenance"]
        _validate_series_members(
            connection, identity, content, visibility, require_available=False,
            previous_series=prior_content.get("series"),
        )
        source_members = []
        if row["source_kind"] != "artifact" and row["kind"] == "work":
            from .community_media import _draft_members
            source_members = _draft_members(connection, publication_id, row["version"])
        _require_client_publication_permission(
            connection, {**dict(row), "visibility": visibility}, content, source_members,
        )
        new_version = row["version"] + 1
        artifact_version = row["artifact_version"]
        if native_owner is not None and content != prior_content:
            artifact_version = _save_native_artifact_content(
                connection, identity, row, native_owner, content,
                revision_note=f"Native publication content revision {new_version}",
            )
        connection.execute(
            "UPDATE publications SET content_json=?,visibility=?,status='draft',version=?,artifact_version=?,source_version=?,updated_at=? WHERE id=?",
            (_json(content), visibility, new_version, artifact_version,
             artifact_version if native_owner is not None else row["source_version"], utc_now(), publication_id),
        )
        if row["source_kind"] != "artifact":
            from .community_media import copy_draft_media_version
            copy_draft_media_version(connection, publication_id, row["version"], new_version)
            if row["kind"] == "work":
                members = _sync_work_media_version(store, connection, publication_id, new_version, content)
                if isinstance(content.get("media"), dict):
                    if private_work_draft:
                        content["media"]["rights_state"] = "private_draft_rights_pending"
                    else:
                        from .community_media import validate_draft_media_licenses
                        validate_draft_media_licenses(content, members, "view")
                        content["media"]["rights_state"] = "author_attested_source_permissions"
                connection.execute("UPDATE publications SET content_json=? WHERE id=?", (_json(content), publication_id))
        updated = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        from .community_media import _draft_members
        _validate_publication_source_access(
            connection, identity, updated, content, _draft_members(connection, publication_id, new_version),
        )
        _save_version(connection, updated, identity["user"]["id"])
        result = _draft_data(updated, connection)
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_revision',?,?,'publication_version',?,?)",
            (identity["workspace"]["id"], key, fingerprint, f"{publication_id}:{new_version}", utc_now()),
        )
        return result


def register_community_routes(app):
    app.extensions.setdefault("oevira.job_resume_validators", {})[_COMMUNITY_PUBLISH_JOB_KIND] = validate_community_publish_resume
    if "create_publication" in app.view_functions:
        return
    store = app.extensions["oevira.store"]

    @app.get("/api/community/capabilities")
    def community_capabilities():
        from .community_media import media_publication_capabilities

        identity_id = g.identity["user"]["id"]
        can_review = identity_id in app.config.get("COMMUNITY_MODERATOR_USER_IDS", ())

        return _success({"can_review": g.identity["user"]["id"] in app.config.get("COMMUNITY_MODERATOR_USER_IDS", ()),
                         "can_curate": can_review,
                         "official_creator": g.identity["user"]["id"] in app.config.get("OFFICIAL_CREATOR_USER_IDS", ()),
                         "delivery": "local_review_only",
                         "media": media_publication_capabilities(store)})

    @app.get("/api/publications")
    def list_publications():
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute(
                "SELECT DISTINCT p.* FROM publications p LEFT JOIN publication_coauthors c "
                "ON c.publication_id=p.id AND c.workspace_id=p.workspace_id AND c.user_id=? AND c.status='accepted' "
                "WHERE p.workspace_id=? AND (p.author_id=? OR c.user_id IS NOT NULL) ORDER BY p.updated_at DESC,p.id",
                (g.identity["user"]["id"], g.identity["workspace"]["id"], g.identity["user"]["id"]),
            ).fetchall()
            visible = []
            for row in rows:
                try:
                    _owned_publication(connection, g.identity, row["id"])
                except ApiError:
                    continue
                reviews = connection.execute("SELECT id,decision,reason,created_at,review_mode,policy_version FROM publication_reviews WHERE publication_id=? ORDER BY created_at DESC,id DESC", (row["id"],)).fetchall()
                visible.append({**_draft_data(row, connection), "reviews": [dict(review) for review in reviews]})
            return _success(visible)

    @app.get("/api/publications/coauthor-invitations")
    def list_coauthor_invitations():
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute(
                "SELECT c.*,p.version AS current_version,p.status AS publication_status,p.content_json,p.visibility,p.author_id "
                "FROM publication_coauthors c JOIN publications p ON p.id=c.publication_id AND p.workspace_id=c.workspace_id "
                "WHERE c.workspace_id=? AND c.user_id=? AND c.status='invited' ORDER BY c.invited_at,c.id",
                (g.identity["workspace"]["id"], g.identity["user"]["id"]),
            ).fetchall()
            invitations = []
            for relation in rows:
                if relation["current_version"] != relation["invited_version"] or relation["publication_status"] not in {"draft", "rejected"}:
                    continue
                publication = connection.execute("SELECT * FROM publications WHERE id=?", (relation["publication_id"],)).fetchone()
                content = json.loads(publication["content_json"])
                from .community_media import _draft_members

                members = _draft_members(connection, publication["id"], publication["version"])
                invitee = {"workspace": g.identity["workspace"], "user": g.identity["user"]}
                try:
                    _validate_coauthor_source_access(connection, invitee, publication, content, members)
                except ApiError:
                    continue
                if _coauthor_scope_digest(connection, publication, content) != relation["invited_scope_digest"]:
                    continue
                creator = _publication_creator(connection, publication["author_id"])
                invitations.append({
                    "id": relation["id"], "publication_id": publication["id"],
                    "publication_version": publication["version"], "status": "invited",
                    "title": content["title"], "creator": creator, "invited_at": relation["invited_at"],
                })
            return _success(invitations)

    @app.post("/api/publications")
    def create_publication():
        data, identity = _body(), g.identity
        if isinstance(data, dict) and set(data) == {"kind", "visibility", "content"}:
            created = create_native_publication(store, identity, data, _idempotency_key())
            return _success(created["publication"], 201 if created["created"] else 200)
        artifact_fields = {"artifact_id", "artifact_version", "kind", "visibility", "content"}
        if set(data) == artifact_fields and data.get("visibility") == "private":
            created = create_private_publication_from_artifact(
                store, identity, data, _idempotency_key(),
            )
            return _success(created["publication"], 201 if created["created"] else 200)
        media_fields = {"source_kind", "source_id", "source_version", "media_selection", "kind", "visibility", "content"}
        is_artifact_source = set(data) == artifact_fields
        is_media_source = set(data) == media_fields
        if not (is_artifact_source or is_media_source) or type(data.get("kind")) is not str or data.get("kind") not in {"work", "template", "experience"} or type(data.get("visibility")) is not str or data.get("visibility") not in {"private", "team", "unlisted", "public"}:
            raise ApiError("INVALID_INPUT", "Provide one exact Artifact or Media version, or create a native work draft, with an explicit kind and visibility", 400)
        private_work_draft = data["visibility"] == "private"
        source_kind = "artifact" if is_artifact_source else data["source_kind"]
        if is_artifact_source:
            source_id = _string(data["artifact_id"], "artifact_id", maximum=255)
            source_version = _version(data["artifact_version"])
            content = _content(
                data["content"], data["kind"], allow_empty_body=private_work_draft,
                private_work_draft=private_work_draft,
            )
            media_result = None
        else:
            if type(source_kind) is not str or source_kind not in {"media_material", "media_render", "media_project"}:
                raise ApiError("INVALID_INPUT", "Choose a saved Material, completed Media render or editable Media project version", 400)
            source_id = _string(data["source_id"], "source_id", maximum=255)
            source_version = _version(data["source_version"])
            if not isinstance(data["content"], dict) or "media" in data["content"] or "template" in data["content"]:
                raise ApiError("INVALID_INPUT", "Media package manifests are derived from the selected source and cannot be supplied by the client", 400)
            from .community_media import prepare_media_publication, require_media_source_access

            with store.transaction(immediate=False) as connection:
                require_media_source_access(
                    connection, identity,
                    {"workspace_id": identity["workspace"]["id"], "source_kind": source_kind,
                     "source_id": source_id, "source_version": source_version},
                    write=True,
                )
            content = _content(
                data["content"], data["kind"], allow_empty_body=data["kind"] == "work" or private_work_draft,
                private_work_draft=private_work_draft,
                allow_included_dependencies=True, require_template=data["kind"] != "template",
            )
            media_result = prepare_media_publication(
                store, identity, source_kind, source_id, source_version,
                data.get("media_selection"), content, data["kind"],
                private_draft=private_work_draft,
            )
            content["media"] = {key: value for key, value in media_result["public_media"].items() if key != "template"}
            if media_result.get("template_manifest") is not None:
                content["template"] = media_result["template_manifest"]
            if data["kind"] == "work":
                _attach_publication_media_blocks(
                    content, media_result["members"],
                    append_missing="blocks" not in data["content"],
                )
            _json(content)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Publication creation requires an idempotency key", 400)
        fingerprint = _request_fingerprint(data)
        with store.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='publication_create' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint or prior["resource_type"] != "publication":
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another publication", 409)
                return _success(_draft_data(_owned_publication(connection, identity, prior["resource_id"]), connection))
            identifier = str(uuid.uuid4())
            artifact_id = artifact_version = None
            source_work_id = None
            if source_kind == "artifact":
                artifact = require_artifact_access(connection, identity, source_id, write=True)
                source = connection.execute("SELECT 1 FROM artifact_versions WHERE artifact_id=? AND version=?", (artifact["id"], source_version)).fetchone()
                if not source:
                    raise _not_found()
                artifact_id, artifact_version = artifact["id"], source_version
                source_work_id = artifact["work_id"]
            else:
                from .community_media import require_media_source_access

                require_media_source_access(
                    connection, identity,
                    {"workspace_id": identity["workspace"]["id"], "source_kind": source_kind,
                     "source_id": source_id, "source_version": source_version},
                    write=True,
                )
                source_work_id = media_result["source_work_id"]
            source_scope = {
                "workspace_id": identity["workspace"]["id"],
                "source_kind": source_kind,
                "source_id": source_id,
                "source_version": source_version,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "work_id": source_work_id,
            }
            source_work_ids = _publication_source_work_ids(
                connection, source_scope, content, (media_result or {}).get("members", []),
            )
            if data["visibility"] != "private" and any(work_commission(connection, work_id) for work_id in set(source_work_ids) if work_id) and content.get("privacy_review", {}).get("client_permission") is not True:
                raise ApiError("CLIENT_PUBLICATION_PERMISSION_REQUIRED", "Client work requires explicit permission for the selected public content", 403)
            _validate_series_members(connection, identity, content, data["visibility"], require_available=True)
            now = utc_now()
            connection.execute(
                """INSERT INTO publications(
                       id,workspace_id,author_id,artifact_id,artifact_version,source_kind,source_id,source_version,
                       kind,version,status,visibility,content_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,1,'draft',?,?,?,?)""",
                (identifier, identity["workspace"]["id"], identity["user"]["id"], artifact_id, artifact_version,
                 source_kind, source_id, source_version, data["kind"], data["visibility"], _json(content), now, now),
            )
            row = connection.execute("SELECT * FROM publications WHERE id=?", (identifier,)).fetchone()
            if media_result is not None:
                from .community_media import _draft_members, save_draft_media_members, validate_draft_media_licenses

                save_draft_media_members(connection, identifier, 1, media_result["members"])
                verified_members = _draft_members(connection, identifier, 1)
                validate_draft_media_licenses(content, verified_members, private_draft=private_work_draft)
                if data["kind"] == "work":
                    _attach_publication_media_blocks(
                        content, verified_members,
                        append_missing="blocks" not in data["content"],
                    )
                    connection.execute("UPDATE publications SET content_json=? WHERE id=?", (_json(content), identifier))
                    row = connection.execute("SELECT * FROM publications WHERE id=?", (identifier,)).fetchone()
            elif data["kind"] == "work":
                _attach_publication_media_blocks(content, [], append_missing=False)
                connection.execute("UPDATE publications SET content_json=? WHERE id=?", (_json(content), identifier))
                row = connection.execute("SELECT * FROM publications WHERE id=?", (identifier,)).fetchone()
            from .community_media import _draft_members
            _validate_publication_source_access(
                connection, identity, row, content, _draft_members(connection, identifier, 1),
            )
            _save_version(connection, row, identity["user"]["id"])
            connection.execute("INSERT INTO idempotency_records VALUES(?,'publication_create',?,?,'publication',?,?)", (identity["workspace"]["id"], key, fingerprint, identifier, now))
            return _success(_draft_data(row, connection), 201)

    @app.get("/api/publications/<publication_id>")
    def get_publication(publication_id):
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            invite = connection.execute(
                "SELECT c.* FROM publication_coauthors c JOIN publications p ON p.id=c.publication_id AND p.workspace_id=c.workspace_id "
                "WHERE c.publication_id=? AND c.workspace_id=? AND c.user_id=? AND c.status='invited'",
                (publication_id, g.identity["workspace"]["id"], g.identity["user"]["id"]),
            ).fetchone()
            if invite is not None:
                publication = connection.execute(
                    "SELECT * FROM publications WHERE id=? AND workspace_id=?",
                    (publication_id, g.identity["workspace"]["id"]),
                ).fetchone()
                if (
                    publication is None or publication["version"] != invite["invited_version"]
                    or publication["status"] not in {"draft", "rejected"}
                    or request.args.get("version") is not None
                ):
                    raise ApiError("COAUTHOR_INVITATION_STALE", "The invited Publication version is no longer current", 409)
                content = json.loads(publication["content_json"])
                from .community_media import _draft_members, draft_media_manifest

                members = _draft_members(connection, publication_id, publication["version"])
                invitee = {"workspace": g.identity["workspace"], "user": g.identity["user"]}
                _validate_coauthor_source_access(connection, invitee, publication, content, members)
                if _coauthor_scope_digest(connection, publication, content) != invite["invited_scope_digest"]:
                    raise ApiError("COAUTHOR_INVITATION_STALE", "The selected content, source, rights or audience changed after this invitation", 409)
                safe_subjects = _public_subject_ref_records(
                    _publication_subject_ref_snapshots(connection, invitee, publication, content),
                )
                invitation_view = {
                    "id": publication_id, "kind": publication["kind"],
                    "version": publication["version"], "status": publication["status"],
                    "visibility": publication["visibility"],
                    "creator": _publication_creator(connection, publication["author_id"]),
                    "coauthor_invitation": {"id": invite["id"], "status": "invited", "invited_version": invite["invited_version"]},
                    "content": _public_content(
                        content,
                        resolved_series_members=_resolve_series_members(
                            connection, content.get("series", {}), invitee,
                            publication["visibility"], require_available=False,
                        ) if isinstance(content.get("series"), dict) else None,
                        resolved_subject_refs=safe_subjects,
                    ),
                    "media_members": draft_media_manifest(connection, publication_id, publication["version"]),
                }
                return _success(invitation_view)
            row = _owned_publication(connection, g.identity, publication_id)
            selected = request.args.get("version")
            if selected is None:
                result = _draft_data(row, connection)
            else:
                try:
                    selected_version = int(selected)
                except (TypeError, ValueError) as error:
                    raise ApiError("INVALID_INPUT", "version must be a positive integer", 400) from error
                _version(selected_version)
                saved = connection.execute(
                    "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                    (publication_id, selected_version),
                ).fetchone()
                if saved is None:
                    raise _not_found()
                result = json.loads(saved["snapshot_json"])
                if not isinstance(result, dict) or result.get("id") != publication_id or result.get("version") != selected_version:
                    raise ApiError("PUBLICATION_VERSION_INVALID", "The saved publication version is inconsistent", 409)
                result["creator"] = _publication_creator(connection, row["author_id"])
                result["coauthors"] = _publication_coauthors_for_version(
                    connection, row, version=selected_version, content=result.get("content", {}),
                )
            history = connection.execute(
                "SELECT version,created_at FROM publication_versions WHERE publication_id=? ORDER BY version",
                (publication_id,),
            ).fetchall()
            result["versions"] = [{"version": item["version"], "created_at": item["created_at"]} for item in history]
            result["current_version"] = row["version"]
            return _success(result)

    @app.post("/api/publications/<publication_id>/coauthors")
    def invite_publication_coauthor(publication_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version", "user_id"}:
            raise ApiError("INVALID_INPUT", "A coauthor invitation needs the exact Publication version and workspace user ID", 400)
        base_version = _version(data["base_version"])
        user_id = _string(data["user_id"], "user_id", maximum=200)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Coauthor invitations require an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version, "user_id": user_id})
        with store.transaction() as connection:
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_coauthor_invite' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another coauthor invitation", 409)
                relation = connection.execute("SELECT * FROM publication_coauthors WHERE id=? AND publication_id=?", (prior["resource_id"], publication_id)).fetchone()
                if relation is None or relation["invited_by"] != identity["user"]["id"]:
                    raise _not_found()
                return _success(_coauthor_relation_data(relation))
            publication = _owned_publication(connection, identity, publication_id, write=True)
            if publication["version"] != base_version or publication["status"] not in {"draft", "rejected"}:
                raise ApiError("VERSION_CONFLICT", "Invite coauthors from the current editable Publication version", 409)
            member = connection.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
                (identity["workspace"]["id"], user_id),
            ).fetchone()
            if member is None or user_id == publication["author_id"]:
                raise _not_found()
            content = json.loads(publication["content_json"])
            from .community_media import _draft_members

            media_members = _draft_members(connection, publication_id, publication["version"])
            candidate = {"workspace": identity["workspace"], "user": {"id": user_id}}
            _validate_coauthor_source_access(connection, candidate, publication, content, media_members)
            scope_digest = _coauthor_scope_digest(connection, publication, content)
            existing = connection.execute(
                "SELECT * FROM publication_coauthors WHERE publication_id=? AND user_id=?",
                (publication_id, user_id),
            ).fetchone()
            if existing is not None and existing["status"] == "accepted" and _coauthor_scope_is_current(
                connection, publication, existing, target_version=publication["version"], target_content=content,
            ):
                raise ApiError("COAUTHOR_ALREADY_ACCEPTED", "This user already has accepted authorship for the current exact content", 409)
            now = utc_now()
            relation_id = existing["id"] if existing is not None else str(uuid.uuid4())
            connection.execute(
                "INSERT INTO publication_coauthors(id,publication_id,workspace_id,user_id,invited_by,invited_version,invited_scope_digest,status,accepted_version,accepted_scope_digest,invited_at,responded_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'invited',NULL,NULL,?,NULL,?) "
                "ON CONFLICT(publication_id,user_id) DO UPDATE SET invited_by=excluded.invited_by,invited_version=excluded.invited_version,invited_scope_digest=excluded.invited_scope_digest,status='invited',accepted_version=NULL,accepted_scope_digest=NULL,invited_at=excluded.invited_at,responded_at=NULL,updated_at=excluded.updated_at",
                (relation_id, publication_id, identity["workspace"]["id"], user_id, identity["user"]["id"], base_version, scope_digest, now, now),
            )
            relation = connection.execute("SELECT * FROM publication_coauthors WHERE id=?", (relation_id,)).fetchone()
            _record_coauthor_event(connection, relation, identity["user"]["id"], "invited", scope_digest, now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_coauthor_invite',?,?,'publication_coauthor',?,?)",
                (identity["workspace"]["id"], key, fingerprint, relation_id, now),
            )
            return _success(_coauthor_relation_data(relation), 201)

    @app.post("/api/publications/<publication_id>/coauthors/<user_id>/accept")
    def accept_publication_coauthor(publication_id, user_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version"} or user_id != identity["user"]["id"]:
            raise _not_found()
        base_version = _version(data["base_version"])
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Coauthor acceptance requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version, "user_id": user_id, "decision": "accepted"})
        with store.transaction() as connection:
            require_workspace_access(connection, identity)
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_coauthor_accept' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another coauthor acceptance", 409)
                relation = connection.execute("SELECT * FROM publication_coauthors WHERE id=? AND user_id=?", (prior["resource_id"], user_id)).fetchone()
                if relation is None:
                    raise _not_found()
                return _success(_coauthor_relation_data(relation))
            publication = connection.execute(
                "SELECT * FROM publications WHERE id=? AND workspace_id=?",
                (publication_id, identity["workspace"]["id"]),
            ).fetchone()
            relation = connection.execute(
                "SELECT * FROM publication_coauthors WHERE publication_id=? AND workspace_id=? AND user_id=?",
                (publication_id, identity["workspace"]["id"], user_id),
            ).fetchone()
            if publication is None or relation is None or relation["status"] != "invited":
                raise _not_found()
            if (
                publication["version"] != base_version or relation["invited_version"] != base_version
                or publication["status"] not in {"draft", "rejected"}
            ):
                raise ApiError("COAUTHOR_INVITATION_STALE", "The invitation does not match the current editable Publication version", 409)
            content = json.loads(publication["content_json"])
            from .community_media import _draft_members

            members = _draft_members(connection, publication_id, publication["version"])
            scope_digest = _coauthor_scope_digest(connection, publication, content)
            if scope_digest != relation["invited_scope_digest"]:
                raise ApiError("COAUTHOR_INVITATION_STALE", "The selected content, audience, rights or members changed after invitation", 409)
            _validate_coauthor_source_access(connection, identity, publication, content, members)
            now = utc_now()
            connection.execute(
                "UPDATE publication_coauthors SET status='accepted',accepted_version=?,accepted_scope_digest=?,responded_at=?,updated_at=? WHERE id=? AND status='invited'",
                (base_version, scope_digest, now, now, relation["id"]),
            )
            accepted = connection.execute("SELECT * FROM publication_coauthors WHERE id=?", (relation["id"],)).fetchone()
            _record_coauthor_event(connection, accepted, user_id, "accepted", scope_digest, now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_coauthor_accept',?,?,'publication_coauthor',?,?)",
                (identity["workspace"]["id"], key, fingerprint, relation["id"], now),
            )
            return _success(_coauthor_relation_data(accepted), 201)

    @app.post("/api/publications/<publication_id>/coauthors/<user_id>/decline")
    def decline_publication_coauthor(publication_id, user_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version"} or user_id != identity["user"]["id"]:
            raise _not_found()
        base_version = _version(data["base_version"])
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Coauthor decline requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version, "user_id": user_id, "decision": "declined"})
        with store.transaction() as connection:
            require_workspace_access(connection, identity)
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_coauthor_decline' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another coauthor decline", 409)
                relation = connection.execute("SELECT * FROM publication_coauthors WHERE id=? AND user_id=?", (prior["resource_id"], user_id)).fetchone()
                if relation is None:
                    raise _not_found()
                return _success(_coauthor_relation_data(relation))
            relation = connection.execute(
                "SELECT * FROM publication_coauthors WHERE publication_id=? AND workspace_id=? AND user_id=? AND status='invited'",
                (publication_id, identity["workspace"]["id"], user_id),
            ).fetchone()
            if relation is None:
                raise _not_found()
            if relation["invited_version"] != base_version:
                raise ApiError("COAUTHOR_INVITATION_STALE", "Select the exact version named in this invitation", 409)
            now = utc_now()
            connection.execute(
                "UPDATE publication_coauthors SET status='declined',responded_at=?,updated_at=? WHERE id=? AND status='invited'",
                (now, now, relation["id"]),
            )
            declined = connection.execute("SELECT * FROM publication_coauthors WHERE id=?", (relation["id"],)).fetchone()
            _record_coauthor_event(connection, declined, user_id, "declined", relation["invited_scope_digest"], now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_coauthor_decline',?,?,'publication_coauthor',?,?)",
                (identity["workspace"]["id"], key, fingerprint, relation["id"], now),
            )
            return _success(_coauthor_relation_data(declined), 201)

    @app.delete("/api/publications/<publication_id>/coauthors/<user_id>")
    def revoke_publication_coauthor(publication_id, user_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Coauthor removal needs the current Publication version", 400)
        base_version = _version(data["base_version"])
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Coauthor removal requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version, "user_id": user_id, "decision": "revoked"})
        with store.transaction() as connection:
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_coauthor_revoke' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another coauthor removal", 409)
                relation = connection.execute("SELECT * FROM publication_coauthors WHERE id=? AND publication_id=?", (prior["resource_id"], publication_id)).fetchone()
                if relation is None or relation["invited_by"] != identity["user"]["id"]:
                    raise _not_found()
                return _success(_coauthor_relation_data(relation))
            publication = _owned_publication(connection, identity, publication_id, write=True)
            if publication["version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "The Publication changed before coauthor removal", 409)
            relation = connection.execute(
                "SELECT * FROM publication_coauthors WHERE publication_id=? AND workspace_id=? AND user_id=?",
                (publication_id, identity["workspace"]["id"], user_id),
            ).fetchone()
            if relation is None or relation["status"] in {"revoked", "declined"}:
                raise _not_found()
            now = utc_now()
            connection.execute(
                "UPDATE publication_coauthors SET status='revoked',responded_at=?,updated_at=? WHERE id=?",
                (now, now, relation["id"]),
            )
            revoked = connection.execute("SELECT * FROM publication_coauthors WHERE id=?", (relation["id"],)).fetchone()
            _record_coauthor_event(connection, revoked, identity["user"]["id"], "revoked", relation["accepted_scope_digest"] or relation["invited_scope_digest"], now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_coauthor_revoke',?,?,'publication_coauthor',?,?)",
                (identity["workspace"]["id"], key, fingerprint, relation["id"], now),
            )
            return _success(_coauthor_relation_data(revoked))

    @app.patch("/api/publications/<publication_id>")
    def revise_publication(publication_id):
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Publication revisions require an idempotency key", 400)
        updated = revise_publication_command(store, g.identity, publication_id, _body(), key)
        return _success(updated)

    @app.post("/api/publications/<publication_id>/restore")
    def restore_publication(publication_id):
        data, identity = _body(), g.identity
        if set(data) - {"base_version", "restore_version", "revision_note"}:
            raise ApiError("INVALID_INPUT", "Unsupported publication restore fields", 400)
        base_version = _version(data.get("base_version"))
        restore_version = _version(data.get("restore_version"))
        revision_note = _optional_string(data.get("revision_note"), "revision_note", maximum=2000) or f"恢复作品版本 {restore_version}"
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Publication restore requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version,
                                            "restore_version": restore_version, "revision_note": revision_note})
        now = utc_now()
        with store.transaction() as connection:
            row = _owned_publication(connection, identity, publication_id, write=True)
            receipt = connection.execute(
                "SELECT * FROM publication_restore_receipts WHERE workspace_id=? AND operation_key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if receipt is not None:
                if receipt["publication_id"] != publication_id or receipt["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another publication restore", 409)
                return _success(json.loads(receipt["result_json"]))
            if row["version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "The publication changed before restore", 409)
            if row["kind"] not in {"work", "experience"}:
                raise ApiError("PUBLICATION_RESTORE_UNSUPPORTED", "Only editable work and experience publications can be restored here", 409)
            historical_row = connection.execute(
                "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                (publication_id, restore_version),
            ).fetchone()
            if historical_row is None:
                raise _not_found()
            historical = json.loads(historical_row["snapshot_json"])
            historical_source = historical.get("source") if isinstance(historical, dict) else None
            if (
                not isinstance(historical_source, dict)
                or historical_source.get("kind") != row["source_kind"]
                or historical_source.get("id") != row["source_id"]
                or historical.get("kind") != row["kind"]
            ):
                raise ApiError("PUBLICATION_SOURCE_CONFLICT", "Restore cannot change a publication's source identity", 409)
            if row["source_kind"] != "artifact" and historical_source.get("version") != row["source_version"]:
                raise ApiError("PUBLICATION_SOURCE_VERSION_CONFLICT", "Rebind the exact source through its source revision operation", 409)
            restored_content = historical.get("content")
            if not isinstance(restored_content, dict):
                raise ApiError("PUBLICATION_VERSION_INVALID", "The selected publication version has no content", 409)
            raw_content = dict(restored_content)
            saved_media = raw_content.pop("media", None)
            private_draft = historical.get("visibility") == "private"
            content = _content(
                raw_content, row["kind"], allow_empty_body=private_draft,
                private_work_draft=private_draft,
                allow_included_dependencies=True,
            )
            if saved_media is not None:
                content["media"] = saved_media
            visibility = historical.get("visibility")
            if visibility not in {"private", "team", "unlisted", "public"}:
                raise ApiError("PUBLICATION_VERSION_INVALID", "The selected version has an unsupported visibility", 409)
            _validate_series_members(connection, identity, content, visibility, require_available=False)
            native_owner = _native_artifact_owner(connection, identity, row, json.loads(row["content_json"]), write=True)
            if native_owner is not None and historical.get("artifact_id") != row["artifact_id"]:
                raise ApiError("PUBLICATION_SOURCE_CONFLICT", "The selected version belongs to another native Artifact", 409)

            if row["source_kind"] != "artifact":
                from .community_media import _draft_members, validate_draft_media_licenses

                old_members = _draft_members(connection, publication_id, restore_version)
                validate_draft_media_licenses(content, old_members, private_draft=visibility == "private")
                _validate_publication_source_access(connection, identity, row, content, old_members)

            new_version = row["version"] + 1
            artifact_version = row["artifact_version"]
            if native_owner is not None:
                artifact_version = _save_native_artifact_content(
                    connection, identity, row, native_owner, content,
                    revision_note=f"Restore publication {restore_version} as a new Artifact version",
                )
            connection.execute(
                "UPDATE publications SET content_json=?,visibility=?,status='draft',version=?,artifact_version=?,source_version=?,updated_at=? WHERE id=? AND version=?",
                (_json(content), visibility, new_version, artifact_version,
                 artifact_version if native_owner is not None else row["source_version"], now, publication_id, base_version),
            )
            if row["source_kind"] != "artifact":
                from .community_media import copy_draft_media_version

                copy_draft_media_version(connection, publication_id, restore_version, new_version)
                if row["kind"] == "work":
                    _sync_work_media_version(store, connection, publication_id, new_version, content)
                    connection.execute("UPDATE publications SET content_json=? WHERE id=?", (_json(content), publication_id))
            updated = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            from .community_media import _draft_members
            _validate_publication_source_access(
                connection, identity, updated, content, _draft_members(connection, publication_id, new_version),
            )
            _save_version(connection, updated, identity["user"]["id"])
            result = _draft_data(updated, connection)
            result["restore"] = {"from_version": restore_version, "created_version": new_version}
            connection.execute(
                "INSERT INTO publication_restore_receipts(workspace_id,operation_key,publication_id,request_hash,result_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (identity["workspace"]["id"], key, publication_id, fingerprint, new_version, _json(result), now),
            )
            return _success(result)

    @app.post("/api/publications/<publication_id>/media-source-revisions")
    def revise_publication_media_source(publication_id):
        """Rebind a Media template draft to an exact newer saved project version."""
        data, identity = _body(), g.identity
        from .community_media import (
            prepare_media_publication,
            require_media_source_access,
            save_draft_media_members,
            validate_draft_media_licenses,
        )

        required = {"base_version", "source_version", "media_selection", "content"}
        if set(data) != required:
            raise ApiError("INVALID_INPUT", "A Media template source revision needs its exact project version, selection and rights", 400)
        base_version = _version(data["base_version"])
        source_version = _version(data["source_version"])
        if type(data["media_selection"]) is not dict or not isinstance(data["content"], dict):
            raise ApiError("INVALID_INPUT", "Media source selection and selected publication content must be objects", 400)
        raw_content = data["content"]
        if "media" in raw_content or "template" in raw_content:
            raise ApiError("INVALID_INPUT", "Media manifests are derived from the exact project version", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "A Media source revision requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, **data})
        with store.transaction(immediate=False) as connection:
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_media_source_revise' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to a different Media source revision", 409)
                publication, version_text = prior["resource_id"].rsplit(":", 1)
                row = connection.execute(
                    "SELECT workspace_id,author_id FROM publications WHERE id=?",
                    (publication,),
                ).fetchone()
                if publication != publication_id or row is None or row["workspace_id"] != identity["workspace"]["id"] or row["author_id"] != identity["user"]["id"]:
                    raise _not_found()
                historical = connection.execute(
                    "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=? AND created_by=?",
                    (publication_id, int(version_text), identity["user"]["id"]),
                ).fetchone()
                if historical is None:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "The saved Media source revision is unavailable", 409)
                return _success(json.loads(historical["snapshot_json"]))
            row = _owned_publication(connection, identity, publication_id, write=True)
            if row["source_kind"] != "media_project" or row["kind"] != "template":
                raise ApiError("MEDIA_TEMPLATE_SOURCE_REQUIRED", "Only an editable Media template can change its source project version", 409)
            if _version(row["version"]) != base_version or row["status"] not in {"draft", "rejected"}:
                raise ApiError("PUBLICATION_STATE_CONFLICT", "Only the current draft or returned submission can select a new Media version", 409)
            if source_version <= row["source_version"]:
                raise ApiError("MEDIA_TEMPLATE_VERSION_NOT_NEWER", "Choose a newer exact version of the same Media project", 409)
            source_id = row["source_id"]
        content = _content(
            raw_content, "template", allow_included_dependencies=True,
            require_template=False,
        )
        media_result = prepare_media_publication(
            store, identity, "media_project", source_id, source_version,
            data["media_selection"], content, "template",
        )
        content["media"] = {key: value for key, value in media_result["public_media"].items() if key != "template"}
        content["template"] = media_result["template_manifest"]
        _json(content)
        new_version = base_version + 1
        with store.transaction() as connection:
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_media_source_revise' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to a different Media source revision", 409)
                _publication_id, version_text = prior["resource_id"].rsplit(":", 1)
                historical = connection.execute(
                    "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=? AND created_by=?",
                    (publication_id, int(version_text), identity["user"]["id"]),
                ).fetchone()
                if historical is None:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "The saved Media source revision is unavailable", 409)
                return _success(json.loads(historical["snapshot_json"]))
            current = _owned_publication(connection, identity, publication_id, write=True)
            if current["source_kind"] != "media_project" or current["source_id"] != source_id or current["version"] != base_version or current["status"] not in {"draft", "rejected"}:
                raise ApiError("VERSION_CONFLICT", "The Media publication changed while its new source version was prepared", 409)
            require_media_source_access(
                connection, identity,
                {"workspace_id": identity["workspace"]["id"], "source_kind": "media_project",
                 "source_id": source_id, "source_version": source_version},
                write=True,
            )
            if work_commission(connection, media_result["source_work_id"]) and content["privacy_review"].get("client_permission") is not True:
                raise ApiError("CLIENT_PUBLICATION_PERMISSION_REQUIRED", "Client work needs explicit permission for this exact Media source revision", 403)
            target_version = current["version"] + 1
            now = utc_now()
            connection.execute(
                "UPDATE publications SET source_version=?,content_json=?,status='draft',version=?,updated_at=? WHERE id=? AND version=?",
                (source_version, _json(content), target_version, now, publication_id, base_version),
            )
            save_draft_media_members(connection, publication_id, target_version, media_result["members"])
            from .community_media import _draft_members
            verified = _draft_members(connection, publication_id, target_version)
            updated = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            _validate_publication_source_access(connection, identity, updated, content, verified)
            validate_draft_media_licenses(content, verified)
            _save_version(connection, updated, identity["user"]["id"])
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_media_source_revise',?,?,'publication_version',?,?)",
                (identity["workspace"]["id"], key, fingerprint, f"{publication_id}:{target_version}", now),
            )
            return _success(_draft_data(updated, connection))

    @app.post("/api/publications/<publication_id>/submit")
    def submit_publication(publication_id):
        data, identity = _body(), g.identity
        if set(data) != {"base_version", "preview_acknowledged"} or data["preview_acknowledged"] is not True:
            raise ApiError("PREVIEW_REQUIRED", "Review the actual selected content before submitting", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Publication submission requires an idempotency key", 400)
        base_version = _version(data["base_version"])
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": base_version, "preview_acknowledged": True})
        with store.transaction() as connection:
            row = _owned_publication(connection, identity, publication_id, write=True)
            prior = connection.execute(
                "SELECT request_hash,resource_type,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_submit' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint or prior["resource_type"] != "publication_version":
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another Publication submission", 409)
                saved_id, version_text = prior["resource_id"].rsplit(":", 1)
                if saved_id != publication_id:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another Publication", 409)
                saved = connection.execute(
                    "SELECT snapshot_json FROM publication_versions WHERE publication_id=? AND version=?",
                    (publication_id, _version(int(version_text))),
                ).fetchone()
                if saved is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The saved submission version is unavailable", 409)
                return _success(json.loads(saved["snapshot_json"]), 200)
            if base_version != row["version"] or row["status"] not in {"draft", "rejected"}:
                raise ApiError("PUBLICATION_STATE_CONFLICT", "Only the current draft can be submitted", 409)
            content = json.loads(row["content_json"])
            _check_license(content["license"], "view")
            _validate_series_members(connection, identity, content, row["visibility"], require_available=False)
            _validate_publication_source_rights(connection, identity, row, content)
            from .community_media import _draft_members, validate_draft_media_licenses

            media_members = _draft_members(connection, publication_id, row["version"])
            _validate_publication_source_access(connection, identity, row, content, media_members)
            _require_client_publication_permission(connection, row, content, media_members)
            validate_draft_media_licenses(content, media_members, "view")
            if row["kind"] == "work":
                _validate_publication_blocks_for_submit(content, media_members)
            if row["visibility"] != "private" and content.get("privacy_review", {}).get("selected_content_only") is not True:
                raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected public content before submission", 400)
            submitted_version = row["version"] + 1
            connection.execute("UPDATE publications SET status='submitted',version=?,updated_at=? WHERE id=?", (submitted_version, utc_now(), publication_id))
            from .community_media import copy_draft_media_version

            copy_draft_media_version(connection, publication_id, row["version"], submitted_version)
            updated = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            _save_version(connection, updated, identity["user"]["id"])
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_submit',?,?,'publication_version',?,?)",
                (identity["workspace"]["id"], key, fingerprint, f"{publication_id}:{submitted_version}", utc_now()),
            )
            return _success(_draft_data(updated, connection))

    @app.get("/api/moderation/submissions")
    def moderation_submissions():
        _moderator(app, g.identity)
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute("SELECT * FROM publications WHERE status='submitted' ORDER BY updated_at,id").fetchall()
            # Moderators receive selected submitted content and reviewed media members,
            # never source IDs, file paths, or workspace snapshots.
            from .community_media import _draft_members, draft_media_manifest

            submissions = []
            for row in rows:
                content = json.loads(row["content_json"])
                media_members = _draft_members(connection, row["id"], row["version"])
                if not _publication_review_authorized(connection, g.identity, row, content, media_members):
                    continue
                subject_refs = _public_subject_ref_records(
                    _publication_subject_ref_snapshots(connection, g.identity, row, content),
                )
                item = {
                    "id": row["id"], "version": row["version"], "kind": row["kind"],
                    "content": _public_content(
                        content,
                        resolved_series_members=_resolve_series_members(
                            connection, content.get("series", {}),
                            g.identity, row["visibility"], require_available=False,
                        ) if isinstance(content.get("series"), dict) else None,
                        resolved_subject_refs=subject_refs,
                    ),
                    "media_members": draft_media_manifest(connection, row["id"], row["version"]),
                }
                review_intent = None
                for job in connection.execute(
                    "SELECT payload_json FROM jobs WHERE workspace_id=? AND kind=? AND status IN ('waiting_for_input','paused','pause_requested','interrupted')",
                    (row["workspace_id"], _COMMUNITY_PUBLISH_JOB_KIND),
                ).fetchall():
                    try:
                        payload = json.loads(job["payload_json"])
                    except (TypeError, ValueError):
                        continue
                    if payload.get("publication_id") == row["id"] and payload.get("review_state") == "pending":
                        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
                        review_intent = {
                            "operation_id": payload.get("operation_id"),
                            "reason": payload.get("review_reason"),
                            "content_digest": intent.get("content_digest"),
                            "rights_digest": intent.get("rights_digest"),
                            "state": "pending",
                        }
                        break
                if review_intent is not None:
                    item["publish_intent"] = review_intent
                submissions.append(item)
            return _success(submissions)

    @app.post("/api/publications/<publication_id>/review")
    def review_publication(publication_id):
        _moderator(app, g.identity)
        data = _body()
        if set(data) != {"base_version", "decision", "reason"} or data["decision"] not in {"approved", "rejected"}:
            raise ApiError("INVALID_REVIEW", "Review the exact submission with a decision and reason", 400)
        reason = _string(data["reason"], "review reason", maximum=10000)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "A publication review requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"publication_id": publication_id, "base_version": data["base_version"],
                                            "decision": data["decision"], "reason": reason})
        with store.transaction() as connection:
            require_workspace_access(connection, g.identity)
            prior = connection.execute(
                "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='publication_review' AND key=?",
                (g.identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to a different publication review", 409)
                saved = connection.execute(
                    "SELECT id,publication_id,publication_version,decision,reviewer_id FROM publication_reviews WHERE id=? AND publication_id=?",
                    (prior["resource_id"], publication_id),
                ).fetchone()
                if saved is None or saved["reviewer_id"] != g.identity["user"]["id"]:
                    raise _not_found()
                return _success({"id": saved["id"], "publication_id": publication_id,
                                 "version": saved["publication_version"] + 1,
                                 "decision": saved["decision"]})
            row = connection.execute("SELECT * FROM publications WHERE id=? AND status='submitted'", (publication_id,)).fetchone()
            if row is None:
                raise _not_found()
            if _version(data["base_version"]) != row["version"]:
                raise ApiError("VERSION_CONFLICT", "The submitted version changed", 409)
            if row["author_id"] == g.identity["user"]["id"]:
                raise ApiError("INDEPENDENT_REVIEW_REQUIRED", "A creator cannot moderate their own submission", 403)
            content = json.loads(row["content_json"])
            _validate_series_members(connection, g.identity, content, row["visibility"], require_available=False)
            _validate_publication_source_rights(connection, g.identity, row, content)
            from .community_media import _draft_members, validate_draft_media_licenses

            media_members = _draft_members(connection, publication_id, row["version"])
            _validate_publication_source_access(connection, g.identity, row, content, media_members)
            _require_client_publication_permission(connection, row, content, media_members)
            if not _publication_review_authorized(connection, g.identity, row, content, media_members):
                raise _not_found()
            validate_draft_media_licenses(content, media_members, "view")
            if row["kind"] == "work":
                _validate_publication_blocks_for_submit(content, media_members)
            if row["visibility"] != "private" and content.get("privacy_review", {}).get("selected_content_only") is not True:
                raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected public content before human review", 400)
            rights_digest = _publication_rights_digest(
                row, content, connection=connection, identity=g.identity, media_members=media_members,
            )
            members_digest = _digest(_publication_member_selection(media_members, content))
            identifier, now = str(uuid.uuid4()), utc_now()
            connection.execute(
                "INSERT INTO publication_reviews(id,publication_id,publication_version,content_hash,decision,reason,reviewer_id,created_at,review_mode,policy_version,rights_digest,members_digest,audience_visibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, publication_id, row["version"], _publication_content_hash(connection, row, content),
                    data["decision"], reason, g.identity["user"]["id"], now, "human", "human-review/1",
                    rights_digest, members_digest, row["visibility"],
                ),
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'publication_review',?,?,'publication_review',?,?)",
                (g.identity["workspace"]["id"], key, fingerprint, identifier, now),
            )
            reviewed_version = row["version"] + 1
            connection.execute("UPDATE publications SET status=?,version=?,updated_at=? WHERE id=?", (data["decision"], reviewed_version, now, publication_id))
            from .community_media import copy_draft_media_version

            copy_draft_media_version(connection, publication_id, row["version"], reviewed_version)
            saved_review = connection.execute("SELECT * FROM publication_reviews WHERE id=?", (identifier,)).fetchone()
            reviewed_publication = connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            _continue_publish_jobs_after_review(connection, publication_id, saved_review, reviewed_publication["version"])
            updated = reviewed_publication
            _save_version(connection, updated, g.identity["user"]["id"])
            return _success({"id": identifier, "publication_id": publication_id, "version": updated["version"], "decision": data["decision"]}, 201)

    @app.post("/api/publications/<publication_id>/publish")
    def publish_publication(publication_id):
        data, identity = _body(), g.identity
        allowed = {"base_version", "review_id", "operation_id", "content_digest", "rights_digest", "visibility"}
        if "base_version" not in data or set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Publish requires base_version and accepts optional matching review/digest context", 400)
        base_version = _version(data["base_version"])
        manual_review = "review_id" in data
        if manual_review:
            data["review_id"] = _string(data["review_id"], "review_id", maximum=255)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Publish requires an idempotency key", 400)
        fingerprint = _publish_request_fingerprint(publication_id, identity, data)
        with store.transaction() as connection:
            row = _owned_publication(connection, identity, publication_id, write=True)
            prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='publication_publish' AND key=?", (identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another publish request", 409)
                if prior["resource_type"] == "release":
                    release, content = _release(connection, prior["resource_id"], identity)
                    return _success(_public_release(release, content, app, connection=connection, identity=identity))
                if prior["resource_type"] == "community_publish_job":
                    job = connection.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (prior["resource_id"], identity["workspace"]["id"])).fetchone()
                    if job is None:
                        raise ApiError("PUBLICATION_JOB_UNAVAILABLE", "The saved publication Job is unavailable", 409)
                    job_payload = json.loads(job["payload_json"])
                    if job["status"] == "completed":
                        result = job_payload.get("_publication_result", {})
                        if isinstance(result.get("release_id"), str):
                            release, content = _release(connection, result["release_id"], identity)
                            delivered = _public_release(release, content, app, connection=connection, identity=identity)
                            if isinstance(job_payload.get("_unlisted_share_token"), str):
                                delivered["share_token"] = job_payload["_unlisted_share_token"]
                            return _success(delivered)
                    from .api import _job_data

                    operation_state = (
                        "review_required" if job_payload.get("review_state") in {"needs_review", "pending"}
                        else "failed" if job["status"] in {"failed", "cancelled"}
                        else "checking"
                    )
                    response_status = 202 if job["status"] in {"queued", "running", "waiting_for_input", "paused", "pause_requested", "interrupted"} else 200
                    job_data = _job_data(job)
                    job_data["publication_operation"] = {
                        "operation_id": job_payload.get("operation_id"), "state": operation_state,
                        "delivery": "pending",
                    }
                    return _success(job_data, response_status)
                if prior["resource_type"] == "publish_review_required":
                    saved_operation = json.loads(prior["resource_id"])
                    return _success(saved_operation.get("receipt", saved_operation))
                raise ApiError("IDEMPOTENCY_CONFLICT", "The saved publish operation has an unsupported result type", 409)
            if base_version != row["version"]:
                raise ApiError("VERSION_CONFLICT", "The publication changed before publishing", 409)
            if "visibility" in data and data["visibility"] != row["visibility"]:
                raise ApiError("OPERATION_INTENT_MISMATCH", "The requested audience does not match the saved publication version", 409)
            content = json.loads(row["content_json"])
            _validate_series_members(connection, identity, content, row["visibility"], require_available=False)
            _validate_publication_source_rights(connection, identity, row, content)
            from .community_media import _draft_members, validate_draft_media_licenses, verify_draft_media_members

            media_members = _draft_members(connection, publication_id, row["version"])
            _validate_publication_source_access(connection, identity, row, content, media_members)
            validate_draft_media_licenses(content, media_members, "view")
            if row["kind"] == "work":
                _validate_publication_blocks_for_submit(content, media_members)
            if row["visibility"] != "private" and content.get("privacy_review", {}).get("selected_content_only") is not True:
                raise ApiError("PUBLIC_CONTENT_REVIEW_REQUIRED", "Review the exact selected public content before publication", 400)
            _require_client_publication_permission(connection, row, content, media_members)
            content_hash = _publication_content_hash(connection, row, content)
            rights_digest = _publication_rights_digest(
                row, content, connection=connection, identity=identity, media_members=media_members,
            )
            _validate_publish_digest(data, "content_digest", content_hash)
            _validate_publish_digest(data, "rights_digest", rights_digest)
            operation_id = _operation_id(identity["workspace"]["id"], key, fingerprint, data.get("operation_id"))
            if "visibility" in data and data["visibility"] != row["visibility"]:
                raise ApiError("OPERATION_INTENT_MISMATCH", "The requested audience does not match the saved publication version", 409)
            _ensure_operation_id_unused(connection, identity["workspace"]["id"], operation_id)
            automatic_review_id = None
            if manual_review:
                if row["status"] != "approved":
                    raise ApiError("PUBLICATION_NOT_APPROVED", "Submit and review this exact version before publishing", 409)
                review = connection.execute(
                    "SELECT * FROM publication_reviews WHERE id=? AND publication_id=? AND decision='approved' AND review_mode='human'",
                    (data["review_id"], publication_id),
                ).fetchone()
                if review is None or review["content_hash"] != content_hash or review["publication_version"] + 1 != row["version"]:
                    raise ApiError("REVIEW_NOT_CURRENT", "The approval does not cover this publication content", 409)
                _require_human_review_authority_current(
                    app, connection, row, content, media_members, review,
                )
                policy_version = review["policy_version"] if review is not None and review["policy_version"] else "human-review/1"
                review_mode = "human"
                decision = "human_approved"
                review_id = review["id"]
            else:
                if row["status"] != "draft":
                    raise ApiError("PUBLICATION_STATE_CONFLICT", "Only a current draft can start a new automatic publication check", 409)
                route, reason = _automatic_publication_eligibility(connection, identity, row, content, media_members)
                policy_version = "publication-source-rights-privacy/1"
                if route != "auto_eligible":
                    intent = _publication_operation_intent(
                        connection, identity, row, content, media_members,
                        operation_id=operation_id, idempotency_key=key,
                        policy_version=policy_version, review_mode="human",
                        decision="publish_requested",
                    )
                    intent["review_reason"] = reason
                    job_data = _queue_community_publish_job(
                        connection, identity, row, base_version=base_version, intent=intent,
                        request_fingerprint=fingerprint, idempotency_key=key,
                        review_mode="human", waiting_for_review=True,
                        review_reason=reason,
                        media_member_count=len(media_members),
                    )
                    return _community_publish_job_response(job_data, operation_id, "review_required", 202, reason=reason)
                review_mode = "automatic"
                decision = "auto_eligible"
                review = None
                automatic_review_id = str(uuid.uuid4())

            media_bytes = sum(item.get("size_bytes", 0) for item in media_members if isinstance(item.get("size_bytes"), int))
            heavy_media_check = bool(media_members) and (
                media_bytes >= _COMMUNITY_PUBLISH_INLINE_VERIFY_BYTES or len(media_members) > 20
            )
            if heavy_media_check:
                intent = _publication_operation_intent(
                    connection, identity, row, content, media_members,
                    operation_id=operation_id, idempotency_key=key,
                    policy_version=policy_version, review_mode=review_mode,
                    decision=decision, review_id=review_id if manual_review else None,
                )
                if intent["content_digest"] != content_hash or intent["rights_digest"] != rights_digest:
                    raise ApiError("OPERATION_INTENT_MISMATCH", "Publication content or rights changed while the intent was prepared", 409)
                job_data = _queue_community_publish_job(
                    connection, identity, row, base_version=base_version, intent=intent,
                    request_fingerprint=fingerprint, idempotency_key=key,
                    review_mode=review_mode, waiting_for_review=False,
                    review_id=review_id if manual_review else None,
                    media_member_count=len(media_members),
                )
                return _community_publish_job_response(job_data, operation_id, "checking", 202)

            media_members = verify_draft_media_members(store, connection, publication_id, row["version"])
            validate_draft_media_licenses(content, media_members, "view")
            _validate_publication_source_access(connection, identity, row, content, media_members)
            _validate_publication_source_rights(connection, identity, row, content)
            _require_client_publication_permission(connection, row, content, media_members)
            _validate_series_members(connection, identity, content, row["visibility"], require_available=False)
            content_hash = _publication_content_hash(connection, row, content)
            rights_digest = _publication_rights_digest(
                row, content, connection=connection, identity=identity, media_members=media_members,
            )
            _validate_publish_digest(data, "content_digest", content_hash)
            _validate_publish_digest(data, "rights_digest", rights_digest)
            if not manual_review:
                automatic_review_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO publication_reviews(id,publication_id,publication_version,content_hash,decision,reason,reviewer_id,created_at,review_mode,policy_version,rights_digest,members_digest,audience_visibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        automatic_review_id, publication_id, row["version"], content_hash,
                        "approved", "自动核对通过：当前来源、可见范围、权利、正文结构和选中内容检查均成立；未作独立事实或专业质量验证。",
                        identity["user"]["id"], utc_now(), "automatic", policy_version,
                        rights_digest, _digest(_publication_member_selection(media_members, content)), row["visibility"],
                    ),
                )
                review = connection.execute("SELECT * FROM publication_reviews WHERE id=?", (automatic_review_id,)).fetchone()
                review_id = review["id"]
            intent = _publication_operation_intent(
                connection, identity, row, content, media_members,
                operation_id=operation_id, idempotency_key=key,
                policy_version=policy_version, review_mode=review_mode,
                decision=decision, review_id=review_id,
            )
            if intent["content_digest"] != content_hash or intent["rights_digest"] != rights_digest:
                raise ApiError("OPERATION_INTENT_MISMATCH", "Publication content or rights changed while the intent was prepared", 409)
            release, share_token = _create_publication_release(
                connection, identity, row, content, media_members, intent, review,
                idempotency_key=key, request_fingerprint=fingerprint,
            )
            result = _public_release(release, content, app, connection=connection, identity=identity)
            if share_token:
                result["share_token"] = share_token
            return _success(result, 201)

    @app.post("/api/publications/<publication_id>/withdraw")
    def withdraw_publication(publication_id):
        data = request.get_json(silent=True) if request.is_json else None
        data = {} if data is None else data
        if not isinstance(data, dict) or set(data) - {"release_id"}:
            raise ApiError("INVALID_INPUT", "Withdrawal accepts only an optional exact release_id", 400)
        with store.transaction() as connection:
            row = _owned_publication(connection, g.identity, publication_id)
            now = utc_now()
            if data.get("release_id") is not None:
                release_id = _string(data["release_id"], "release_id", maximum=255)
                target = connection.execute(
                    "SELECT * FROM publication_releases WHERE id=? AND publication_id=?",
                    (release_id, publication_id),
                ).fetchone()
                if target is None:
                    raise _not_found()
                connection.execute(
                    "UPDATE publication_releases SET withdrawn_at=COALESCE(withdrawn_at,?) WHERE id=?",
                    (now, release_id),
                )
                active = connection.execute(
                    "SELECT COUNT(*) AS count FROM publication_releases WHERE publication_id=? AND withdrawn_at IS NULL",
                    (publication_id,),
                ).fetchone()["count"]
                if active or row["status"] == "withdrawn":
                    return _success({"id": publication_id, "release_id": release_id, "status": "release_withdrawn", "active_release_count": active})
            else:
                connection.execute("UPDATE publication_releases SET withdrawn_at=COALESCE(withdrawn_at,?) WHERE publication_id=?", (now, publication_id))
            if row["status"] != "withdrawn":
                connection.execute("UPDATE publications SET status='withdrawn',version=version+1,updated_at=? WHERE id=?", (utc_now(), publication_id))
                _save_version(connection, connection.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone(), g.identity["user"]["id"])
            return _success({"id": publication_id, "status": "withdrawn", "new_access": "revoked"})

    @app.get("/api/releases/<release_id>")
    def read_release(release_id):
        with store.transaction(immediate=False) as connection:
            release, content = _release(connection, release_id, getattr(g, "identity", None), request.headers.get("X-Share-Token"))
            return _success(_public_release(release, content, app, connection=connection, identity=getattr(g, "identity", None)))

    @app.get("/api/creators/<creator_id>")
    def creator_profile(creator_id):
        with store.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT DISTINCT r.* FROM publication_releases r LEFT JOIN publication_release_coauthors c ON c.release_id=r.id "
                "WHERE (r.author_id=? OR c.user_id=?) AND r.visibility='public' AND r.withdrawn_at IS NULL "
                "ORDER BY r.published_at DESC,r.id DESC LIMIT 100",
                (creator_id, creator_id),
            ).fetchall()
            releases = []
            for row in rows:
                content = json.loads(row["snapshot_json"])
                try:
                    _check_license(content["license"], "view")
                    from .community_media import validate_release_media_licenses

                    validate_release_media_licenses(connection, row["id"], content, "view")
                except ApiError:
                    continue
                releases.append(_public_release(row, content, app, connection=connection, identity=getattr(g, "identity", None)))
            if not releases:
                raise _not_found()
            attribution = next(
                (item["content"]["license"].get("attribution") for item in releases
                 if item["content"]["license"].get("attribution")),
                "",
            )
            coauthor_creator = connection.execute(
                "SELECT 1 FROM publication_release_coauthors WHERE user_id=? AND release_id IN (" +
                "SELECT id FROM publication_releases WHERE visibility='public' AND withdrawn_at IS NULL)",
                (creator_id,),
            ).fetchone() is not None
            profile_name = _publication_creator(connection, creator_id)["display_name"]
            followed = False
            if getattr(g, "identity", None) is not None:
                followed = connection.execute(
                    "SELECT 1 FROM community_follows WHERE follower_id=? AND creator_id=?",
                    (g.identity["user"]["id"], creator_id),
                ).fetchone() is not None
            return _success({
                "creator_id": creator_id,
                "display_name": profile_name if coauthor_creator else attribution or profile_name,
                "identity_basis": "accepted_coauthor_account" if coauthor_creator else "release_attribution_or_creator_account",
                "releases": releases,
                "followed": followed,
            })

    @app.get("/api/community/follows")
    def list_creator_follows():
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute(
                "SELECT creator_id,created_at FROM community_follows WHERE follower_id=? ORDER BY created_at DESC,creator_id",
                (g.identity["user"]["id"],),
            ).fetchall()
            follows = []
            for row in rows:
                release = connection.execute(
                    "SELECT DISTINCT r.* FROM publication_releases r LEFT JOIN publication_release_coauthors c ON c.release_id=r.id "
                    "WHERE (r.author_id=? OR c.user_id=?) AND r.visibility='public' AND r.withdrawn_at IS NULL "
                    "ORDER BY r.published_at DESC,r.id DESC LIMIT 1",
                    (row["creator_id"], row["creator_id"]),
                ).fetchone()
                if release is None:
                    follows.append({"creator_id": row["creator_id"], "created_at": row["created_at"], "availability": "no_current_public_release"})
                    continue
                content = json.loads(release["snapshot_json"])
                try:
                    _check_license(content["license"], "view")
                    from .community_media import validate_release_media_licenses

                    validate_release_media_licenses(connection, release["id"], content, "view")
                except ApiError:
                    follows.append({"creator_id": row["creator_id"], "created_at": row["created_at"], "availability": "current_release_unavailable"})
                    continue
                follows.append({
                    "creator_id": row["creator_id"],
                    "created_at": row["created_at"],
                    "display_name": (
                        _publication_creator(connection, row["creator_id"])["display_name"]
                        if release["author_id"] != row["creator_id"]
                        else content["license"].get("attribution") or _publication_creator(connection, row["creator_id"])["display_name"]
                    ),
                    "latest_release": {"id": release["id"], "kind": release["kind"], "title": content["title"], "published_at": release["published_at"]},
                })
            return _success(follows)

    @app.post("/api/creators/<creator_id>/follow")
    def set_creator_follow(creator_id):
        data = _body()
        if set(data) != {"follow"} or type(data["follow"]) is not bool:
            raise ApiError("INVALID_INPUT", "Choose whether to follow this public creator", 400)
        identity = g.identity
        if identity["user"]["id"] == creator_id and data["follow"]:
            raise ApiError("SELF_FOLLOW_NOT_ALLOWED", "You cannot follow your own creator page", 409)
        with store.transaction() as connection:
            require_workspace_access(connection, identity)
            if data["follow"]:
                release = connection.execute(
                    "SELECT DISTINCT r.* FROM publication_releases r LEFT JOIN publication_release_coauthors c ON c.release_id=r.id "
                    "WHERE (r.author_id=? OR c.user_id=?) AND r.visibility='public' AND r.withdrawn_at IS NULL "
                    "ORDER BY r.published_at DESC,r.id DESC LIMIT 1",
                    (creator_id, creator_id),
                ).fetchone()
                if release is None:
                    raise _not_found()
                content = json.loads(release["snapshot_json"])
                try:
                    _check_license(content["license"], "view")
                    from .community_media import validate_release_media_licenses

                    validate_release_media_licenses(connection, release["id"], content, "view")
                except ApiError:
                    raise _not_found()
                connection.execute(
                    "INSERT OR IGNORE INTO community_follows(follower_id,creator_id,created_at) VALUES(?,?,?)",
                    (identity["user"]["id"], creator_id, utc_now()),
                )
            else:
                connection.execute(
                    "DELETE FROM community_follows WHERE follower_id=? AND creator_id=?",
                    (identity["user"]["id"], creator_id),
                )
            return _success({"creator_id": creator_id, "following": data["follow"]})

    @app.get("/api/discovery")
    def discover_releases():
        query = request.args.get("q", "").casefold()
        kind, creator = request.args.get("kind"), request.args.get("creator")
        order = request.args.get("order", "latest")
        if order not in {"latest", "following", "featured"}:
            raise ApiError("DISCOVERY_ORDER_UNSUPPORTED", "Choose latest, following or moderator-curated featured", 400)
        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError as error:
            raise ApiError("INVALID_INPUT", "limit must be an integer", 400) from error
        if not 1 <= limit <= 100:
            raise ApiError("INVALID_INPUT", "Choose a page size from 1 to 100", 400)
        before = request.args.get("before")
        with store.transaction(immediate=False) as connection:
            if order == "following":
                identity = getattr(g, "identity", None)
                if identity is None:
                    raise ApiError("UNAUTHENTICATED", "Sign in to browse creators you follow", 401)
                creator_ids = [row["creator_id"] for row in connection.execute(
                    "SELECT creator_id FROM community_follows WHERE follower_id=?", (identity["user"]["id"],)
                ).fetchall()]
                if not creator_ids:
                    return _success({"items": [], "next_cursor": None, "order": "following_creators_latest",
                                     "explanation": "No followed creators have public releases yet.",
                                     "query": {"q": request.args.get("q", ""), "kind": kind, "creator": creator}})
                markers = ",".join("?" for _ in creator_ids)
                rows = connection.execute(
                    f"SELECT DISTINCT r.* FROM publication_releases r LEFT JOIN publication_release_coauthors c ON c.release_id=r.id "
                    f"WHERE r.visibility='public' AND r.withdrawn_at IS NULL AND (r.author_id IN ({markers}) OR c.user_id IN ({markers})) "
                    "ORDER BY r.published_at DESC,r.id DESC",
                    tuple(creator_ids) * 2,
                ).fetchall()
                feature_by_release = {}
            elif order == "featured":
                now = utc_now()
                rows = connection.execute(
                    "SELECT r.*,f.reason AS feature_reason,f.rank AS feature_rank,f.expires_at AS feature_expires_at "
                    "FROM publication_releases r JOIN release_features f ON f.release_id=r.id "
                    "WHERE r.visibility='public' AND r.withdrawn_at IS NULL AND f.starts_at<=? AND (f.expires_at IS NULL OR f.expires_at>?) "
                    "ORDER BY f.rank,f.created_at DESC,r.published_at DESC,r.id DESC",
                    (now, now),
                ).fetchall()
                feature_by_release = {row["id"]: dict(row) for row in rows}
            else:
                rows = connection.execute("SELECT * FROM publication_releases WHERE visibility='public' AND withdrawn_at IS NULL ORDER BY published_at DESC,id DESC").fetchall()
                feature_by_release = {}
            items = []
            for row in rows:
                if order == "featured":
                    cursor = f"{row['feature_rank']:05d}|{row['published_at']}|{row['id']}"
                else:
                    cursor = row["published_at"] + "|" + row["id"]
                if before and cursor >= before:
                    continue
                content = json.loads(row["snapshot_json"])
                coauthor_match = creator and connection.execute(
                    "SELECT 1 FROM publication_release_coauthors WHERE release_id=? AND user_id=?",
                    (row["id"], creator),
                ).fetchone() is not None
                if kind and row["kind"] != kind or creator and row["author_id"] != creator and not coauthor_match:
                    continue
                if query and query not in (content["title"] + " " + content["summary"] + " " + " ".join(content["tags"])).casefold():
                    continue
                try:
                    _check_license(content["license"], "view")
                    from .community_media import validate_release_media_licenses

                    validate_release_media_licenses(connection, row["id"], content, "view")
                except ApiError:
                    continue
                public_item = _public_release(row, content, app, connection=connection, identity=getattr(g, "identity", None))
                if order == "featured":
                    feature = feature_by_release[row["id"]]
                    public_item["curation"] = {
                        "basis": "moderator_curated",
                        "reason": feature["feature_reason"],
                        "expires_at": feature["feature_expires_at"],
                    }
                items.append((cursor, public_item))
                if len(items) > limit:
                    break
            return _success({"items": [item for _, item in items[:limit]], "next_cursor": items[limit - 1][0] if len(items) > limit else None,
                             "order": {"latest": "latest_published", "following": "following_creators_latest", "featured": "moderator_curated"}[order],
                             "explanation": {"latest": "Most recently published first.", "following": "Most recently published by creators you follow.", "featured": "Selected and ordered by an authorized moderator; not a popularity score."}[order],
                             "query": {"q": request.args.get("q", ""), "kind": kind, "creator": creator}})

    @app.post("/api/moderation/releases/<release_id>/feature")
    def curate_release(release_id):
        _moderator(app, g.identity)
        data = _body()
        if set(data) != {"reason", "rank", "starts_at", "expires_at"}:
            raise ApiError("INVALID_INPUT", "Curation requires a reason, rank and explicit active window", 400)
        reason = _string(data["reason"], "curation reason", maximum=2000)
        rank = data["rank"]
        if type(rank) is not int or not 1 <= rank <= 10000:
            raise ApiError("INVALID_INPUT", "Curation rank must be between 1 and 10000", 400)
        starts_at = _aware_timestamp(data["starts_at"], "starts_at")
        expires_at = None if data["expires_at"] is None else _aware_timestamp(data["expires_at"], "expires_at")
        if expires_at is not None and expires_at <= starts_at:
            raise ApiError("INVALID_INPUT", "The featured period must end after it starts", 400)
        with store.transaction() as connection:
            release = connection.execute("SELECT * FROM publication_releases WHERE id=? AND visibility='public' AND withdrawn_at IS NULL", (release_id,)).fetchone()
            if release is None:
                raise _not_found()
            content = json.loads(release["snapshot_json"])
            _check_license(content["license"], "view")
            from .community_media import validate_release_media_licenses

            validate_release_media_licenses(connection, release_id, content, "view")
            connection.execute(
                """INSERT INTO release_features(release_id,featured_by,reason,rank,starts_at,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(release_id) DO UPDATE SET
                     featured_by=excluded.featured_by,reason=excluded.reason,rank=excluded.rank,
                     starts_at=excluded.starts_at,expires_at=excluded.expires_at,created_at=excluded.created_at""",
                (release_id, g.identity["user"]["id"], reason, rank, starts_at, expires_at, utc_now()),
            )
        return _success({"release_id": release_id, "state": "featured", "rank": rank, "starts_at": starts_at, "expires_at": expires_at})

    @app.delete("/api/moderation/releases/<release_id>/feature")
    def unfeature_release(release_id):
        _moderator(app, g.identity)
        with store.transaction() as connection:
            deleted = connection.execute("DELETE FROM release_features WHERE release_id=?", (release_id,)).rowcount
            if deleted != 1:
                raise _not_found()
        return _success({"release_id": release_id, "state": "unfeatured"})

    @app.post("/api/templates/<release_id>/instantiate")
    def instantiate_template(release_id):
        data, identity = _body(), g.identity
        if set(data) - {"work_id", "purpose", "parameters", "title"} or data.get("purpose") not in {"personal", "commercial"} or not isinstance(data.get("parameters", {}), dict):
            raise ApiError("INVALID_INPUT", "Template use needs a destination work, purpose and replacement values", 400)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "Template copying requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"release_id": release_id, **data})
        from .community_media import replay_media_template_instantiation

        media_replay = replay_media_template_instantiation(store, identity, release_id, data, key)
        if media_replay is not None:
            return _success(media_replay)
        # A successfully created private copy survives withdrawal of its source
        # release. An exact retry returns that same copy before checking whether
        # the public source is still available for a *new* instantiation.
        with store.transaction(immediate=False) as connection:
            prior = connection.execute(
                "SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='template_instantiate' AND key=?",
                (identity["workspace"]["id"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another template copy", 409)
                artifact = require_artifact_access(connection, identity, prior["resource_id"])
                if artifact["owner_user_id"] != identity["user"]["id"]:
                    raise _not_found()
                return _success(_artifact_data(connection, artifact, 1))
        with store.transaction(immediate=False) as connection:
            media_release, media_content = _release(
                connection, release_id, identity, request.headers.get("X-Share-Token"), purpose="adapt"
            )
        if media_release["kind"] != "template":
            raise ApiError("NOT_TEMPLATE", "This release is not an editable template", 400)
        if isinstance(media_content.get("template"), dict) and media_content["template"].get("schema") == "oevira-media-template/1":
            from .community_media import instantiate_media_template

            return instantiate_media_template(
                store, identity, media_release, media_content, data, key,
                request.headers.get("X-Share-Token"),
            )
        with store.transaction() as connection:
            require_work_access(connection, identity, data.get("work_id"), write=True)
            prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='template_instantiate' AND key=?", (identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another template copy", 409)
                artifact = require_artifact_access(connection, identity, prior["resource_id"])
                return _success(_artifact_data(connection, artifact, 1))
            release, content = _release(connection, release_id, identity, request.headers.get("X-Share-Token"), purpose="adapt")
            if release["kind"] != "template":
                raise ApiError("NOT_TEMPLATE", "This release is not an editable template", 400)
            if data["purpose"] == "commercial":
                _check_license(content["license"], "commercial")
            template, parameters = content["template"], data.get("parameters", {})
            slot_names = {slot["name"] for slot in template["slots"]}
            if set(parameters) - slot_names:
                raise ApiError("INVALID_TEMPLATE_PARAMETER", "A replacement does not match this template's slots", 400)
            replacements = {}
            for slot in template["slots"]:
                value = parameters.get(slot["name"], slot["default"])
                if not isinstance(value, str) or len(value) > slot["max_length"] or (slot["required"] and not value.strip()):
                    raise ApiError("INVALID_TEMPLATE_PARAMETER", f"Provide an allowed value for {slot['label']}", 400)
                replacements[slot["name"]] = value
            blocks, mapping = [], {}
            for source in template["blocks"]:
                identifier = str(uuid.uuid4())
                text = source["content"]
                for name, value in replacements.items():
                    text = text.replace("{{" + name + "}}", value)
                blocks.append({"id": identifier, "kind": source["kind"], "content": text})
                mapping[source["id"]] = identifier
            now, artifact_id = utc_now(), str(uuid.uuid4())
            title = _string(data.get("title", content["title"]), "title", maximum=300)
            body = "\n\n".join(("## " if block["kind"] == "heading" else "") + block["content"] for block in blocks)
            snapshot = {"kind": "template_instance", "template": {"release_id": release_id, "release_hash": release["content_hash"], "license": content["license"], "acquired_at": now,
                        "blocks": blocks, "source_blocks": template["blocks"], "object_mapping": mapping, "parameters": replacements, "schema": template["schema"],
                        "missing_dependencies": content["dependencies"], "editable_state": "partial" if content["dependencies"] else "ready"}, "materials": []}
            if content["dependencies"]:
                body += "\n\n## 尚需补齐的素材或依赖\n\n" + "\n".join("- " + item["description"] for item in content["dependencies"])
            connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,1,?,?)", (artifact_id, identity["workspace"]["id"], data["work_id"], identity["user"]["id"], title, now, now))
            connection.execute("INSERT INTO artifact_versions(artifact_id,workspace_id,version,title,body,citations_json,input_snapshot_json,revision_note,created_at,created_by) VALUES(?,?,1,?,?,'[]',?,'从许可模板创建',?,?)", (artifact_id, identity["workspace"]["id"], title, body, _json(snapshot), now, identity["user"]["id"]))
            connection.execute("INSERT INTO work_artifacts VALUES(?,?,?,?)", (data["work_id"], artifact_id, identity["workspace"]["id"], now))
            connection.execute("INSERT INTO idempotency_records VALUES(?,'template_instantiate',?,?,'artifact',?,?)", (identity["workspace"]["id"], key, fingerprint, artifact_id, now))
            return _success(_artifact_data(connection, connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()), 201)

    @app.post("/api/releases/<release_id>/items")
    def save_community_item(release_id):
        data = _body()
        if (set(data) - {"kind", "position", "remove", "progress_seconds"}
                or data.get("kind") not in {"collection", "queue"}
                or type(data.get("position", 0)) is not int or data.get("position", 0) < 0
                or ("remove" in data and type(data["remove"]) is not bool)):
            raise ApiError("INVALID_INPUT", "Choose collection or queue and a non-negative integer position", 400)
        progress = data.get("progress_seconds")
        if progress is not None and (data["kind"] != "queue" or type(progress) not in {int, float} or not math.isfinite(progress) or progress < 0):
            raise ApiError("INVALID_INPUT", "Resume position must be a finite non-negative number on a playback queue", 400)
        with store.transaction() as connection:
            require_workspace_access(connection, g.identity)
            _, release_content = _release(connection, release_id, g.identity, request.headers.get("X-Share-Token"))
            if progress is not None:
                media = release_content.get("media")
                renditions = media.get("renditions", []) if isinstance(media, dict) else []
                durations = [member.get("metadata", {}).get("duration") for member in renditions if isinstance(member, dict)]
                if not durations:
                    raise ApiError("PLAYBACK_POSITION_UNAVAILABLE", "This release has no timed media playback", 409)
                max_duration = max(
                    (Fraction(item["num"], item["den"]) for item in durations if isinstance(item, dict) and type(item.get("num")) is int and type(item.get("den")) is int and item["den"] > 0),
                    default=Fraction(0),
                )
                if Fraction(str(progress)) > max_duration:
                    raise ApiError("INVALID_PLAYBACK_POSITION", "Resume position exceeds the published programme duration", 400)
            if data.get("remove") is True:
                connection.execute("DELETE FROM community_items WHERE user_id=? AND release_id=? AND kind=?", (g.identity["user"]["id"], release_id, data["kind"]))
            else:
                connection.execute("INSERT INTO community_items VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,release_id,kind) DO UPDATE SET position=excluded.position,progress_seconds=COALESCE(excluded.progress_seconds,community_items.progress_seconds),updated_at=excluded.updated_at", (g.identity["user"]["id"], release_id, data["kind"], data.get("position", 0), progress, utc_now()))
            return _success({"release_id": release_id, "kind": data["kind"], "saved": data.get("remove") is not True,
                             "progress_seconds": None if data.get("remove") is True else progress})

    @app.get("/api/community/items")
    def list_community_items():
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute("SELECT * FROM community_items WHERE user_id=? ORDER BY kind,position,updated_at,release_id", (g.identity["user"]["id"],)).fetchall()
            result = []
            for row in rows:
                try:
                    release, content = _release(connection, row["release_id"], g.identity)
                    result.append({
                        "kind": row["kind"], "position": row["position"],
                        "progress_seconds": row["progress_seconds"], "updated_at": row["updated_at"],
                        "release": _public_release(release, content, app, connection=connection, identity=g.identity),
                    })
                except ApiError:
                    result.append({
                        "kind": row["kind"], "position": row["position"],
                        "progress_seconds": row["progress_seconds"], "updated_at": row["updated_at"],
                        "release_id": row["release_id"], "availability": "unavailable",
                    })
            return _success(result)

    @app.post("/api/releases/<release_id>/comments")
    def create_release_comment(release_id):
        data = _body()
        if set(data) - {"body", "anchor"}:
            raise ApiError("INVALID_INPUT", "Unsupported public comment field", 400)
        body = _string(data.get("body"), "comment", maximum=10000)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "A comment requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"release_id": release_id, **data})
        with store.transaction() as connection:
            require_workspace_access(connection, g.identity)
            _, content = _release(connection, release_id, g.identity, request.headers.get("X-Share-Token"))
            prior = connection.execute("SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='release_comment' AND key=?", (g.identity["workspace"]["id"], key)).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another comment", 409)
                saved = connection.execute("SELECT * FROM release_comments WHERE id=? AND author_id=?", (prior["resource_id"], g.identity["user"]["id"])).fetchone()
                if saved is None:
                    raise _not_found()
                return _success({"id": saved["id"], "release_id": release_id, "body": saved["body"], "anchor": json.loads(saved["anchor_json"]), "created_at": saved["created_at"]})
            anchor = data.get("anchor", {})
            if anchor:
                if content.get("media"):
                    if not isinstance(anchor, dict) or set(anchor) != {"kind", "member_id", "start_ms", "end_ms"} or anchor.get("kind") != "media_time" or type(anchor.get("start_ms")) is not int or type(anchor.get("end_ms")) is not int or not 0 <= anchor["start_ms"] < anchor["end_ms"]:
                        raise ApiError("ANCHOR_MISMATCH", "Media comments must identify an exact member and millisecond range", 409)
                    member = connection.execute(
                        "SELECT role,metadata_json FROM publication_media_releases WHERE release_id=? AND member_id=?",
                        (release_id, anchor["member_id"]),
                    ).fetchone()
                    if member is None or member["role"] != "rendition":
                        raise ApiError("ANCHOR_MISMATCH", "The media comment must target a published rendition", 409)
                    metadata = json.loads(member["metadata_json"] or "{}")
                    duration = metadata.get("duration")
                    if not isinstance(duration, dict) or type(duration.get("num")) is not int or type(duration.get("den")) is not int or duration["den"] <= 0 or anchor["end_ms"] * duration["den"] > duration["num"] * 1000:
                        raise ApiError("ANCHOR_MISMATCH", "The media comment range is outside the exact published programme", 409)
                elif not isinstance(anchor, dict) or set(anchor) != {"start", "end", "quote"} or any(type(anchor.get(key)) is not int for key in ("start", "end")) or not 0 <= anchor["start"] < anchor["end"] <= len(content["body"]) or content["body"][anchor["start"]:anchor["end"]] != anchor["quote"]:
                    raise ApiError("ANCHOR_MISMATCH", "Comment anchor must match the exact published content", 409)
            identifier, now = str(uuid.uuid4()), utc_now()
            connection.execute("INSERT INTO release_comments VALUES(?,?,?,?,?,?,NULL)", (identifier, release_id, g.identity["user"]["id"], body, _json(anchor), now))
            connection.execute("INSERT INTO idempotency_records VALUES(?,'release_comment',?,?,'release_comment',?,?)", (g.identity["workspace"]["id"], key, fingerprint, identifier, now))
            return _success({"id": identifier, "release_id": release_id, "body": body, "anchor": anchor, "created_at": now}, 201)

    @app.get("/api/releases/<release_id>/comments")
    def read_release_comments(release_id):
        with store.transaction(immediate=False) as connection:
            _release(connection, release_id, getattr(g, "identity", None), request.headers.get("X-Share-Token"))
            rows = connection.execute("SELECT id,author_id,body,anchor_json,created_at FROM release_comments WHERE release_id=? AND withdrawn_at IS NULL ORDER BY created_at,id", (release_id,)).fetchall()
            return _success([{**dict(row), "anchor": json.loads(row["anchor_json"])} for row in rows])

    @app.post("/api/releases/<release_id>/reports")
    def report_release(release_id):
        data = _body()
        if set(data) != {"reason"}:
            raise ApiError("INVALID_INPUT", "A report needs one reason", 400)
        reason = _string(data.get("reason"), "report reason", maximum=10000)
        key = _idempotency_key()
        if not key:
            raise ApiError("IDEMPOTENCY_REQUIRED", "A content report requires an idempotency key", 400)
        fingerprint = _request_fingerprint({"release_id": release_id, "reason": reason})
        with store.transaction() as connection:
            _release(connection, release_id, g.identity, request.headers.get("X-Share-Token"))
            prior = connection.execute(
                "SELECT * FROM idempotency_records WHERE workspace_id=? AND operation='publication_report' AND key=?",
                (g.identity["workspace"]["id"], key),
            ).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another report", 409)
                saved = connection.execute("SELECT id,release_id,state FROM publication_reports WHERE id=?", (prior["resource_id"],)).fetchone()
                if saved is None:
                    raise _not_found()
                return _success(dict(saved))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM publication_reports WHERE reported_by=? AND created_at>=?",
                (g.identity["user"]["id"], cutoff),
            ).fetchone()["count"]
            if count >= 20:
                raise ApiError("REPORT_RATE_LIMITED", "Too many reports were submitted; try again later", 429)
            identifier = str(uuid.uuid4())
            connection.execute("INSERT INTO publication_reports VALUES(?,?,?,?,'open',NULL,NULL,?,NULL)", (identifier, release_id, g.identity["user"]["id"], reason, utc_now()))
            connection.execute(
                "INSERT INTO idempotency_records VALUES(?,'publication_report',?,?,'publication_report',?,?)",
                (g.identity["workspace"]["id"], key, fingerprint, identifier, utc_now()),
            )
            return _success({"id": identifier, "release_id": release_id, "state": "open", "release_action": "none"}, 201)

    @app.get("/api/moderation/reports")
    def list_publication_reports():
        _moderator(app, g.identity)
        state = request.args.get("state", "open")
        if state not in {"open", "resolved", "dismissed"}:
            raise ApiError("INVALID_INPUT", "Choose an open, resolved or dismissed report state", 400)
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            rows = connection.execute(
                """SELECT r.id,r.release_id,r.reported_by,r.reason,r.state,r.resolution,r.resolved_by,
                          r.created_at,r.resolved_at,p.kind,p.author_id,p.visibility,p.snapshot_json
                   FROM publication_reports r JOIN publication_releases p ON p.id=r.release_id
                   WHERE r.state=? ORDER BY r.created_at,r.id LIMIT 200""",
                (state,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                snapshot = json.loads(item.pop("snapshot_json"))
                item["title"] = snapshot.get("title", "")
                result.append(item)
            return _success(result)

    @app.post("/api/moderation/reports/<report_id>/resolve")
    def resolve_publication_report(report_id: str):
        _moderator(app, g.identity)
        data = _body()
        if set(data) != {"expected_state", "decision", "reason"} or data.get("expected_state") != "open" or data.get("decision") not in {"dismiss", "withdraw_release"}:
            raise ApiError("INVALID_REVIEW", "Resolve an open report by dismissing it or withdrawing the exact release", 400)
        reason = _string(data.get("reason"), "resolution reason", maximum=5000)
        now = utc_now()
        with store.transaction() as connection:
            require_workspace_access(connection, g.identity)
            report = connection.execute("SELECT * FROM publication_reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                raise _not_found()
            if report["state"] != data["expected_state"]:
                raise ApiError("REPORT_STATE_CONFLICT", "This report was already resolved; refresh the queue", 409)
            release = connection.execute("SELECT * FROM publication_releases WHERE id=?", (report["release_id"],)).fetchone()
            if release is None:
                raise _not_found()
            withdrawn = data["decision"] == "withdraw_release"
            if withdrawn and release["withdrawn_at"] is None:
                connection.execute("UPDATE publication_releases SET withdrawn_at=? WHERE id=? AND withdrawn_at IS NULL", (now, release["id"]))
                publication = connection.execute("SELECT * FROM publications WHERE id=?", (release["publication_id"],)).fetchone()
                if publication is not None and publication["status"] == "published":
                    next_version = publication["version"] + 1
                    connection.execute(
                        "UPDATE publications SET status='withdrawn',version=?,updated_at=? WHERE id=? AND version=?",
                        (next_version, now, publication["id"], publication["version"]),
                    )
                    _save_version(connection, connection.execute("SELECT * FROM publications WHERE id=?", (publication["id"],)).fetchone(), g.identity["user"]["id"])
            state = "dismissed" if not withdrawn else "resolved"
            resolution = _json({"decision": data["decision"], "reason": reason, "release_action": "new_access_revoked" if withdrawn else "none"})
            connection.execute(
                "UPDATE publication_reports SET state=?,resolution=?,resolved_by=?,resolved_at=? WHERE id=? AND state='open'",
                (state, resolution, g.identity["user"]["id"], now, report_id),
            )
            return _success({"id": report_id, "release_id": report["release_id"], "state": state, "release_action": "new_access_revoked" if withdrawn else "none", "resolved_at": now})

    @app.post("/api/publications/<publication_id>/appeals")
    def appeal_publication(publication_id):
        data = _body()
        reason = _string(data.get("reason"), "appeal reason", maximum=10000)
        with store.transaction() as connection:
            _owned_publication(connection, g.identity, publication_id)
            review = connection.execute("SELECT id FROM publication_reviews WHERE id=? AND publication_id=?", (data.get("review_id"), publication_id)).fetchone()
            if review is None:
                raise _not_found()
            identifier = str(uuid.uuid4())
            connection.execute("INSERT INTO publication_appeals VALUES(?,?,?,?,?,?)", (identifier, publication_id, review["id"], reason, g.identity["user"]["id"], utc_now()))
            return _success({"id": identifier, "review_id": review["id"], "state": "submitted_for_review"}, 201)

    from .community_media import register_community_media_routes

    register_community_media_routes(app, store)
