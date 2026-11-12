"""Workspace-scoped business subjects, source evidence, and relations."""
from __future__ import annotations

from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import g, request

from .api import (
    ApiError,
    _body,
    _idempotency_key,
    _not_found,
    _request_fingerprint,
    _require_write,
    _success,
    _string,
)
from .auth import utc_now


BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_subjects (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    kind TEXT NOT NULL CHECK(kind IN (
        'brand', 'company', 'legal_entity', 'business_venue',
        'operating_organization', 'merchant', 'store', 'platform_account',
        'product_service', 'unclassified'
    )),
    name TEXT NOT NULL,
    profile_json TEXT NOT NULL DEFAULT '{}',
    directory_state TEXT NOT NULL CHECK(directory_state IN ('active', 'removed')),
    identity_review_status TEXT NOT NULL CHECK(identity_review_status IN ('needs_review', 'supported', 'conflicted')),
    identity_review_evidence_json TEXT NOT NULL DEFAULT '[]',
    identity_review_note TEXT NOT NULL DEFAULT '',
    identity_reviewed_by TEXT REFERENCES users(id),
    identity_reviewed_at TEXT,
    canonical_subject_id TEXT REFERENCES business_subjects(id),
    identity_resolution_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS business_subjects_workspace_state
    ON business_subjects(workspace_id, directory_state, updated_at DESC);
CREATE TABLE IF NOT EXISTS business_subject_versions (
    subject_id TEXT NOT NULL REFERENCES business_subjects(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 0),
    operation TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(subject_id, version)
);
CREATE TABLE IF NOT EXISTS business_evidence (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT REFERENCES works(id),
    subject_id TEXT NOT NULL REFERENCES business_subjects(id),
    scope TEXT NOT NULL CHECK(scope IN ('identity', 'china_operation', 'relationship')),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('material', 'public_reference', 'user_statement')),
    source_state TEXT NOT NULL CHECK(source_state IN ('workspace_material', 'unfetched_reference', 'user_statement')),
    source_title TEXT NOT NULL,
    source_url TEXT,
    material_id TEXT,
    material_sha256 TEXT,
    source_location TEXT,
    excerpt TEXT NOT NULL,
    observed_at TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS business_evidence_subject_scope
    ON business_evidence(workspace_id, subject_id, scope, recorded_at, id);
CREATE TABLE IF NOT EXISTS business_relations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT REFERENCES works(id),
    subject_id TEXT NOT NULL REFERENCES business_subjects(id),
    related_subject_id TEXT REFERENCES business_subjects(id),
    related_subject_version INTEGER,
    related_subject_name TEXT,
    related_object_kind TEXT,
    related_object_id TEXT,
    related_object_label TEXT,
    relation_type TEXT NOT NULL,
    directory_state TEXT NOT NULL CHECK(directory_state IN ('active', 'withdrawn')),
    review_status TEXT NOT NULL CHECK(review_status IN ('needs_review', 'supported', 'conflicted', 'rejected')),
    review_evidence_json TEXT NOT NULL DEFAULT '[]',
    review_note TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TEXT,
    valid_time_kind TEXT NOT NULL CHECK(valid_time_kind IN ('unknown', 'observation', 'interval')),
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT,
    version INTEGER NOT NULL CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    CHECK (
        (related_subject_id IS NOT NULL AND related_object_kind IS NULL AND related_object_id IS NULL)
        OR
        (related_subject_id IS NULL AND related_object_kind IS NOT NULL AND related_object_id IS NOT NULL)
    ),
    CHECK (
        (valid_time_kind = 'unknown' AND valid_from IS NULL AND valid_to IS NULL AND observed_at IS NULL)
        OR
        (valid_time_kind = 'observation' AND valid_from IS NULL AND valid_to IS NULL AND observed_at IS NOT NULL)
        OR
        (valid_time_kind = 'interval' AND valid_from IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS business_relations_subject_state
    ON business_relations(workspace_id, subject_id, directory_state, updated_at DESC);
CREATE INDEX IF NOT EXISTS business_relations_target_subject
    ON business_relations(workspace_id, related_subject_id);
CREATE TABLE IF NOT EXISTS business_metric_observations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT REFERENCES works(id),
    subject_id TEXT REFERENCES business_subjects(id),
    brand_focus_subject_id TEXT REFERENCES business_subjects(id),
    material_id TEXT REFERENCES materials(id),
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    metric_values_json TEXT NOT NULL,
    observed_at TEXT,
    time_status TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL DEFAULT '',
    collector_version TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL CHECK(review_status IN ('needs_review', 'supported', 'conflicted')),
    review_note TEXT NOT NULL DEFAULT '',
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    CHECK(subject_id IS NOT NULL OR brand_focus_subject_id IS NOT NULL OR material_id IS NOT NULL),
    UNIQUE(workspace_id, id)
);
CREATE INDEX IF NOT EXISTS business_metric_subject_observed
    ON business_metric_observations(workspace_id, subject_id, observed_at, id);
CREATE INDEX IF NOT EXISTS business_metric_brand_focus_observed
    ON business_metric_observations(workspace_id, brand_focus_subject_id, observed_at, id);
CREATE INDEX IF NOT EXISTS business_metric_material_observed
    ON business_metric_observations(workspace_id, material_id, observed_at, id);
CREATE TABLE IF NOT EXISTS business_cases (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    subject_id TEXT REFERENCES business_subjects(id),
    subject_version_at_link INTEGER CHECK(subject_version_at_link > 0),
    work_id TEXT REFERENCES works(id),
    submission_id TEXT NOT NULL,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'closed')),
    review_status TEXT NOT NULL CHECK(review_status IN ('needs_review', 'supported', 'conflicted')),
    source_refs_status TEXT NOT NULL CHECK(source_refs_status IN ('needs_review', 'partial', 'linked')),
    creation_evidence_id TEXT REFERENCES business_evidence(id),
    legacy_source_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    UNIQUE(workspace_id, id)
);
CREATE INDEX IF NOT EXISTS business_cases_subject_state
    ON business_cases(workspace_id, subject_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS business_cases_submission
    ON business_cases(workspace_id, submission_id);
CREATE TABLE IF NOT EXISTS business_case_sources (
    case_id TEXT NOT NULL REFERENCES business_cases(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    position INTEGER NOT NULL CHECK(position >= 0),
    material_id TEXT REFERENCES materials(id),
    source_native_id TEXT,
    historical_evidence_ref TEXT,
    source_title TEXT,
    source_url TEXT,
    source_sha256 TEXT,
    link_status TEXT NOT NULL,
    PRIMARY KEY(case_id, position)
);
CREATE INDEX IF NOT EXISTS business_case_sources_material
    ON business_case_sources(workspace_id, material_id);
CREATE TABLE IF NOT EXISTS business_case_versions (
    case_id TEXT NOT NULL REFERENCES business_cases(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 0),
    operation TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(case_id, version)
);
CREATE TABLE IF NOT EXISTS business_relation_versions (
    relation_id TEXT NOT NULL REFERENCES business_relations(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 0),
    operation TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(relation_id, version)
);
"""


SUBJECT_KIND_ALIASES = {
    "brand": "brand",
    "品牌": "brand",
    "品牌主体": "brand",
    "company": "company",
    "commercial": "company",
    "business": "company",
    "商业": "company",
    "商业主体": "company",
    "legal_entity": "legal_entity",
    "legal_organization": "legal_entity",
    "organization": "legal_entity",
    "法律组织": "legal_entity",
    "商业场所": "business_venue",
    "business_venue": "business_venue",
    "venue": "business_venue",
    "运营方": "operating_organization",
    "operator": "operating_organization",
    "operating_organization": "operating_organization",
    "merchant": "merchant",
    "商户": "merchant",
    "store": "store",
    "门店": "store",
    "platform_account": "platform_account",
    "账号": "platform_account",
    "product_service": "product_service",
    "product": "product_service",
    "service": "product_service",
    "产品": "product_service",
    "服务": "product_service",
    "unclassified": "unclassified",
    "unspecified": "unclassified",
    "unknown": "unclassified",
    "待识别": "unclassified",
}
RELATED_OBJECT_KINDS = {
    "customer_mandate",
    "market",
    "merchant",
    "platform_account",
    "store",
    "venue",
    "venue_unit",
}
RELATION_TYPES = {
    "agency_for",
    "brand_at",
    "client_mandate",
    "controls_account",
    "has_tenant",
    "located_at",
    "market_activity",
    "managed_by",
    "operates",
    "operates_in",
    "owned_by",
    "owns",
    "represents",
    "tenant_at",
}
IDENTITY_REVIEW_STATES = {"needs_review", "supported", "conflicted"}
RELATION_REVIEW_STATES = IDENTITY_REVIEW_STATES | {"rejected"}
EVIDENCE_SCOPES = {"identity", "china_operation", "relationship"}


def install_business_schema(store) -> None:
    """Install additive business tables without seeding rows or changing core version."""
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + BUSINESS_SCHEMA + "\nCOMMIT;")
        relation_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(business_relations)")
        }
        if "details_json" not in relation_columns:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "ALTER TABLE business_relations ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'"
            )
            connection.commit()
        _upgrade_business_subject_kinds(store, connection)
        _install_business_identity_resolution_columns(connection)
        _install_business_scope_columns(connection)
    finally:
        connection.close()
    from .business_cases import install_case_lifecycle_schema
    install_case_lifecycle_schema(store)


_BUSINESS_SUBJECT_KINDS = (
    "brand", "company", "legal_entity", "business_venue",
    "operating_organization", "merchant", "store", "platform_account",
    "product_service", "unclassified",
)


def _upgrade_business_subject_kinds(store, connection) -> None:
    """Expand the old two-value check without changing IDs, versions or relations."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(business_subjects)")}
    schema = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_subjects'"
    ).fetchone()
    if schema is None or "legal_entity" in schema["sql"]:
        return
    if not {"id", "workspace_id", "kind", "name"} <= columns:
        raise RuntimeError("Business Subject schema cannot be migrated without its identity columns")
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_subjects'"
        ).fetchone()
        if schema is None or "legal_entity" in schema["sql"]:
            connection.commit()
            return
        original_sql = schema["sql"]
        revised_sql = re.sub(
            r"CREATE TABLE\s+business_subjects",
            "CREATE TABLE business_subjects_kind_upgrade",
            original_sql,
            count=1,
            flags=re.I,
        )
        kind_check = re.compile(
            r"kind\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*kind\s+IN\s*\(\s*'brand'\s*,\s*'company'\s*\)\s*\)",
            re.I,
        )
        kinds_sql = ", ".join("'" + kind + "'" for kind in _BUSINESS_SUBJECT_KINDS)
        revised_sql, replaced = kind_check.subn(
            f"kind TEXT NOT NULL CHECK(kind IN ({kinds_sql}))",
            revised_sql,
            count=1,
        )
        if revised_sql == original_sql or replaced != 1:
            raise RuntimeError("Business Subject kind migration requires an explicit compatible schema")
        if connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='business_subjects_kind_upgrade'"
        ).fetchone():
            raise RuntimeError("Unexpected Business Subject migration table was preserved")
        original_columns = [row["name"] for row in connection.execute("PRAGMA table_info(business_subjects)")]
        quoted = ",".join('"' + name.replace('"', '""') + '"' for name in original_columns)
        indexes = [row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE tbl_name='business_subjects' AND type IN ('index','trigger') AND sql IS NOT NULL"
        )]
        views = [dict(row) for row in connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='view' AND sql IS NOT NULL"
        ) if re.search(r"\bbusiness_subjects\b", row["sql"], re.I)]
        before = [tuple(row) for row in connection.execute(f"SELECT {quoted} FROM business_subjects ORDER BY id")]
        original_violations = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
        if before:
            recovery = store.root / "recovery"
            recovery.mkdir(parents=True, exist_ok=True)
            backup_path = recovery / f"before-subject-kind-{uuid.uuid4().hex}.sqlite3"
            source = store.connect()
            try:
                backup = sqlite3.connect(backup_path)
                try:
                    source.backup(backup)
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Business Subject recovery snapshot could not be verified")
                finally:
                    backup.close()
            finally:
                source.close()
        for view in views:
            connection.execute('DROP VIEW "' + view["name"].replace('"', '""') + '"')
        connection.execute(revised_sql)
        connection.execute(
            f"INSERT INTO business_subjects_kind_upgrade({quoted}) SELECT {quoted} FROM business_subjects"
        )
        connection.execute("DROP TABLE business_subjects")
        connection.execute("ALTER TABLE business_subjects_kind_upgrade RENAME TO business_subjects")
        for index_sql in indexes:
            connection.execute(index_sql)
        for view in views:
            connection.execute(view["sql"])
        after = [tuple(row) for row in connection.execute(f"SELECT {quoted} FROM business_subjects ORDER BY id")]
        violations = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
        if before != after or violations - original_violations:
            raise RuntimeError("Business Subject migration changed an identity or introduced a broken reference")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _install_business_identity_resolution_columns(connection) -> None:
    additions = (
        ("canonical_subject_id", "TEXT REFERENCES business_subjects(id)"),
        ("identity_resolution_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        known = {row["name"] for row in connection.execute("PRAGMA table_info(business_subjects)")}
        for name, declaration in additions:
            if name not in known:
                connection.execute(f"ALTER TABLE business_subjects ADD COLUMN {name} {declaration}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS business_subjects_canonical ON business_subjects(workspace_id,canonical_subject_id,directory_state)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _install_business_scope_columns(connection) -> None:
    """Keep customer evidence and relationship annotations in their authorized Work."""
    additions = {
        "business_evidence": (
            ("work_id", "TEXT REFERENCES works(id)"),
        ),
        "business_relations": (
            ("work_id", "TEXT REFERENCES works(id)"),
        ),
        "business_metric_observations": (
            ("work_id", "TEXT REFERENCES works(id)"),
        ),
    }
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table, columns in additions.items():
            known = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns:
                if name not in known:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        connection.execute(
            """UPDATE business_evidence SET work_id=(
                   SELECT m.work_id FROM materials m
                   WHERE m.id=business_evidence.material_id AND m.workspace_id=business_evidence.workspace_id
               ) WHERE work_id IS NULL AND material_id IS NOT NULL"""
        )
        connection.execute(
            """UPDATE business_metric_observations SET work_id=(
                   SELECT m.work_id FROM materials m
                   WHERE m.id=business_metric_observations.material_id
                     AND m.workspace_id=business_metric_observations.workspace_id
               ) WHERE work_id IS NULL AND material_id IS NOT NULL"""
        )
        subjects = connection.execute(
            "SELECT * FROM business_subjects WHERE identity_review_evidence_json!='[]'"
        ).fetchall()
        for subject in subjects:
            try:
                evidence_ids = json.loads(subject["identity_review_evidence_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence_ids, list) or not evidence_ids:
                continue
            markers = ",".join("?" for _ in evidence_ids)
            private_evidence = connection.execute(
                f"SELECT 1 FROM business_evidence WHERE workspace_id=? AND subject_id=? AND work_id IS NOT NULL AND id IN ({markers}) LIMIT 1",
                (subject["workspace_id"], subject["id"], *evidence_ids),
            ).fetchone()
            if private_evidence is None:
                continue
            now = utc_now()
            connection.execute(
                """UPDATE business_subjects SET identity_review_status='needs_review',
                   identity_review_evidence_json='[]',
                   identity_review_note='Prior review used Work-scoped evidence; confirm this shared identity from workspace-shared evidence.',
                   identity_reviewed_by=NULL,identity_reviewed_at=NULL,version=version+1,updated_at=?
                   WHERE id=? AND workspace_id=? AND version=?""",
                (now, subject["id"], subject["workspace_id"], subject["version"]),
            )
            updated = connection.execute(
                "SELECT * FROM business_subjects WHERE id=? AND workspace_id=?",
                (subject["id"], subject["workspace_id"]),
            ).fetchone()
            _write_subject_version(connection, updated, "private_evidence_review_revalidated", subject["created_by"], now)
        relations = connection.execute(
            "SELECT id,workspace_id,subject_id,review_evidence_json FROM business_relations WHERE work_id IS NULL"
        ).fetchall()
        for relation in relations:
            try:
                evidence_ids = json.loads(relation["review_evidence_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence_ids, list) or not evidence_ids:
                continue
            markers = ",".join("?" for _ in evidence_ids)
            evidence_work_ids = {
                item["work_id"]
                for item in connection.execute(
                    f"SELECT work_id FROM business_evidence WHERE workspace_id=? AND subject_id=? AND id IN ({markers})",
                    (relation["workspace_id"], relation["subject_id"], *evidence_ids),
                ).fetchall()
                if item["work_id"] is not None
            }
            if len(evidence_work_ids) == 1:
                connection.execute(
                    "UPDATE business_relations SET work_id=? WHERE id=? AND workspace_id=? AND work_id IS NULL",
                    (next(iter(evidence_work_ids)), relation["id"], relation["workspace_id"]),
                )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS business_evidence_work
               ON business_evidence(workspace_id,work_id,subject_id,recorded_at,id)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS business_relations_work
               ON business_relations(workspace_id,work_id,subject_id,updated_at DESC)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS business_metric_work
               ON business_metric_observations(workspace_id,work_id,observed_at,id)"""
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _kind(value) -> str:
    if value is None:
        return "unclassified"
    if not isinstance(value, str) or value.strip().lower() not in SUBJECT_KIND_ALIASES:
        raise ApiError("INVALID_INPUT", "kind must identify a supported business object or be omitted while its identity is unresolved", 400, {"field": "kind"})
    return SUBJECT_KIND_ALIASES[value.strip().lower()]


def _profile(value, current: dict | None = None) -> dict:
    if not isinstance(value, dict):
        raise ApiError("INVALID_INPUT", "profile must be an object", 400, {"field": "profile"})
    allowed = {"aliases", "description", "external_ids", "industry", "headquarters", "locations"}
    unknown = set(value) - allowed
    if unknown:
        raise ApiError("INVALID_INPUT", "profile contains an unsupported field", 400, {"fields": sorted(unknown)})
    result = dict(current or {})
    if "aliases" in value:
        aliases = value["aliases"]
        if not isinstance(aliases, list) or len(aliases) > 100:
            raise ApiError("INVALID_INPUT", "profile.aliases must be an array of at most 100 names", 400)
        result["aliases"] = list(dict.fromkeys(_string(item, "profile.aliases[]", maximum=200) for item in aliases))
    for field, limit in (("description", 2000), ("industry", 120), ("headquarters", 300)):
        if field in value:
            result[field] = _string(value[field], f"profile.{field}", maximum=limit)
    if "locations" in value:
        locations = value["locations"]
        allowed_location_fields = {
            "role", "label", "country", "region", "city", "address", "valid_from", "valid_to",
        }
        roles = {"headquarters", "registration", "operating_market", "venue", "store", "unknown"}
        if not isinstance(locations, list) or len(locations) > 100:
            raise ApiError("INVALID_INPUT", "profile.locations must be an array of at most 100 entries", 400)
        normalized_locations = []
        for index, location in enumerate(locations):
            if not isinstance(location, dict) or set(location) - allowed_location_fields:
                raise ApiError("INVALID_INPUT", "Each location has an unsupported field", 400, {"index": index})
            role = location.get("role", "unknown")
            if not isinstance(role, str) or role not in roles:
                raise ApiError("INVALID_INPUT", "profile.locations[].role is not supported", 400, {"index": index})
            normalized = {"role": role}
            for field, limit in (("label", 200), ("country", 100), ("region", 120), ("city", 120), ("address", 500)):
                if location.get(field) is not None:
                    normalized[field] = _string(location[field], f"profile.locations[].{field}", maximum=limit)
            valid_from = _calendar_date(location.get("valid_from"), f"profile.locations[{index}].valid_from")
            valid_to = _calendar_date(location.get("valid_to"), f"profile.locations[{index}].valid_to")
            if valid_from and valid_to and valid_to < valid_from:
                raise ApiError("INVALID_INPUT", "A location valid_to cannot precede valid_from", 400, {"index": index})
            normalized["valid_from"], normalized["valid_to"] = valid_from, valid_to
            if not any(normalized.get(field) for field in ("label", "country", "region", "city", "address")):
                raise ApiError("INVALID_INPUT", "A location needs a literal label or geographic field", 400, {"index": index})
            normalized_locations.append(normalized)
        result["locations"] = normalized_locations
    if "external_ids" in value:
        identifiers = value["external_ids"]
        if not isinstance(identifiers, list) or len(identifiers) > 50:
            raise ApiError("INVALID_INPUT", "profile.external_ids must contain at most 50 identifiers", 400)
        normalized = []
        for item in identifiers:
            if not isinstance(item, dict) or set(item) - {"scheme", "value", "jurisdiction"}:
                raise ApiError("INVALID_INPUT", "Each external identifier needs scheme, value, and optional jurisdiction", 400)
            normalized.append({
                "scheme": _string(item.get("scheme"), "profile.external_ids[].scheme", maximum=80),
                "value": _string(item.get("value"), "profile.external_ids[].value", maximum=200),
                "jurisdiction": _optional_text(item.get("jurisdiction"), "profile.external_ids[].jurisdiction", 100),
            })
        result["external_ids"] = normalized
    return result


def _optional_text(value, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{field} must be a string", 400, {"field": field})
    value = value.strip()
    if len(value) > maximum:
        raise ApiError("INVALID_INPUT", f"{field} is too long", 400, {"field": field})
    return value or None


def _review_state(value, states: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in states:
        raise ApiError("INVALID_INPUT", f"{field} is not a supported review state", 400, {"field": field})
    return value


def _base_version(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError("INVALID_INPUT", "base_version must be a positive integer", 400, {"field": "base_version"})
    return value


def _expect_version(row, version: int) -> None:
    if row["version"] != version:
        raise ApiError(
            "VERSION_CONFLICT",
            "The object changed since it was read; read the current version before revising it",
            409,
            {"current_version": row["version"]},
        )


def _utc_timestamp(value, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ApiError("INVALID_INPUT", f"{field} is required", 400, {"field": field})
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ApiError("INVALID_INPUT", f"{field} must be a timezone-aware timestamp", 400, {"field": field})
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", f"{field} must be a timezone-aware timestamp", 400, {"field": field}) from exc
    if parsed.tzinfo is None:
        raise ApiError("INVALID_INPUT", f"{field} must include a timezone", 400, {"field": field})
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def _calendar_date(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT", f"{field} must be an ISO calendar date", 400, {"field": field})
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", f"{field} must be an ISO calendar date", 400, {"field": field}) from exc
    if parsed.isoformat() != value:
        raise ApiError("INVALID_INPUT", f"{field} must use YYYY-MM-DD", 400, {"field": field})
    return value


def _relation_time(data: dict, current: dict | None = None) -> dict:
    """Parse temporal fields, keeping a missing end distinct from a proved current state."""
    current = current or {}
    kind = data.get("valid_time_kind", current.get("valid_time_kind", "unknown"))
    if kind not in {"unknown", "observation", "interval"}:
        raise ApiError("INVALID_INPUT", "valid_time_kind must be unknown, observation, or interval", 400)
    if kind == "unknown":
        if any(data.get(key, current.get(key)) is not None for key in ("valid_from", "valid_to", "observed_at")):
            raise ApiError("INVALID_INPUT", "unknown validity cannot include dates or observed_at", 400)
        return {"valid_time_kind": kind, "valid_from": None, "valid_to": None, "observed_at": None}
    if kind == "observation":
        observed = _utc_timestamp(data.get("observed_at", current.get("observed_at")), "observed_at", required=True)
        if data.get("valid_from", current.get("valid_from")) is not None or data.get("valid_to", current.get("valid_to")) is not None:
            raise ApiError("INVALID_INPUT", "observation validity cannot include interval dates", 400)
        return {"valid_time_kind": kind, "valid_from": None, "valid_to": None, "observed_at": observed}
    valid_from = _calendar_date(data.get("valid_from", current.get("valid_from")), "valid_from")
    valid_to = _calendar_date(data.get("valid_to", current.get("valid_to")), "valid_to")
    observed = _utc_timestamp(data.get("observed_at", current.get("observed_at")), "observed_at")
    if not valid_from:
        raise ApiError("INVALID_INPUT", "interval validity needs valid_from", 400)
    if valid_to and valid_to < valid_from:
        raise ApiError("INVALID_INPUT", "valid_to cannot be earlier than valid_from", 400)
    return {"valid_time_kind": kind, "valid_from": valid_from, "valid_to": valid_to, "observed_at": observed}


def _normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _current_subject(connection, workspace_id: str, subject_id: str):
    return connection.execute(
        "SELECT * FROM business_subjects WHERE id = ? AND workspace_id = ?",
        (subject_id, workspace_id),
    ).fetchone()


def _subject_identity_group_ids(connection, row) -> list[str]:
    """IDs explicitly merged into one active Subject, without inferred graph closure."""
    if row["canonical_subject_id"] is not None:
        return [row["id"]]
    aliases = connection.execute(
        "SELECT id FROM business_subjects WHERE workspace_id=? AND canonical_subject_id=? AND directory_state='removed' ORDER BY id",
        (row["workspace_id"], row["id"]),
    ).fetchall()
    return [row["id"], *(item["id"] for item in aliases)]


def _subject_merge_children(connection, workspace_id: str, subject_id: str):
    return connection.execute(
        "SELECT * FROM business_subjects WHERE workspace_id=? AND canonical_subject_id=? AND directory_state='removed' ORDER BY id",
        (workspace_id, subject_id),
    ).fetchall()


def _require_business_work(connection, identity: dict, work_id: str | None, *, write: bool = False):
    from .collaboration import require_workspace_access, require_work_access

    if work_id is None:
        return require_workspace_access(connection, identity, write=write)
    return require_work_access(connection, identity, work_id, write=write)


def _visible_evidence_rows(connection, identity: dict, subject_id: str, work_id: str | None):
    if work_id is not None:
        _require_business_work(connection, identity, work_id)
        return connection.execute(
            "SELECT * FROM business_evidence WHERE workspace_id=? AND subject_id=? AND (work_id IS NULL OR work_id=?) ORDER BY recorded_at,id",
            (identity["workspace"]["id"], subject_id, work_id),
        ).fetchall()
    return connection.execute(
        "SELECT * FROM business_evidence WHERE workspace_id=? AND subject_id=? AND work_id IS NULL ORDER BY recorded_at,id",
        (identity["workspace"]["id"], subject_id),
    ).fetchall()


def _relation_private_work_ids(connection, row) -> set[str]:
    try:
        evidence_ids = json.loads(row["review_evidence_json"] or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(evidence_ids, list) or not evidence_ids:
        return set()
    markers = ",".join("?" for _ in evidence_ids)
    return {
        item["work_id"]
        for item in connection.execute(
            f"SELECT work_id FROM business_evidence WHERE workspace_id=? AND subject_id=? AND id IN ({markers})",
            (row["workspace_id"], row["subject_id"], *evidence_ids),
        ).fetchall()
        if item["work_id"] is not None
    }


def _relation_work_scope(connection, row):
    if row["work_id"] is not None:
        return row["work_id"]
    private_work_ids = _relation_private_work_ids(connection, row)
    if len(private_work_ids) == 1:
        return next(iter(private_work_ids))
    if len(private_work_ids) > 1:
        return False
    return None


def _relation_visible(connection, identity: dict, row, *, requested_work_id: str | None = None, write: bool = False) -> bool:
    scope = _relation_work_scope(connection, row)
    if scope is False:
        return False
    private_evidence_work_ids = _relation_private_work_ids(connection, row)
    if scope is not None and private_evidence_work_ids - {scope}:
        return False
    if scope is None:
        if requested_work_id is not None:
            _require_business_work(connection, identity, requested_work_id)
        return True
    if requested_work_id != scope:
        return False
    _require_business_work(connection, identity, scope, write=write)
    return True


def _require_active_subject(row) -> None:
    if row["canonical_subject_id"] is not None:
        raise ApiError(
            "SUBJECT_MERGED",
            "This identity is a merged alias; continue with its canonical Subject",
            409,
            {"canonical_subject_id": row["canonical_subject_id"]},
        )
    if row["directory_state"] != "active":
        raise ApiError("SUBJECT_REMOVED", "Restore the subject before adding current evidence or relations", 409)


def _identity_review(row) -> dict:
    return {
        "status": row["identity_review_status"],
        "evidence_ids": json.loads(row["identity_review_evidence_json"]),
        "note": row["identity_review_note"],
        "reviewed_by": row["identity_reviewed_by"],
        "reviewed_at": row["identity_reviewed_at"],
    }


def _subject_data(row) -> dict:
    resolution = json.loads(row["identity_resolution_json"] or "{}")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "profile": json.loads(row["profile_json"]),
        "directory_state": row["directory_state"],
        "canonical_subject_id": row["canonical_subject_id"],
        "identity_resolution": resolution,
        "identity_review": _identity_review(row),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
    }


def _evidence_data(row) -> dict:
    return {
        "id": row["id"],
        "work_id": row["work_id"],
        "scope": row["scope"],
        "source": {
            "kind": row["source_kind"],
            "state": row["source_state"],
            "title": row["source_title"],
            "url": row["source_url"],
            "material_id": row["material_id"],
            "sha256": row["material_sha256"],
            "location": row["source_location"],
            "excerpt": row["excerpt"],
        },
        "observed_at": row["observed_at"],
        "recorded_at": row["recorded_at"],
        "recorded_by": row["recorded_by"],
    }


def _metric_observation_data(row, *, requested_subject_id: str | None = None) -> dict:
    metadata = json.loads(row["source_metadata_json"] or "{}")
    associations = []
    if row["subject_id"]:
        associations.append({"subject_id": row["subject_id"], "role": "entity_subject"})
    if row["brand_focus_subject_id"]:
        associations.append({"subject_id": row["brand_focus_subject_id"], "role": "brand_focus"})
    if requested_subject_id:
        associations = [item for item in associations if item["subject_id"] == requested_subject_id]
    material = None
    if row["material_id"]:
        material = {
            "id": row["material_id"],
            "title": row["material_title"],
            "filename": row["material_filename"],
            "sha256": row["material_sha256"],
            "status": row["material_status"],
        }
    return {
        "id": row["id"],
        "work_id": row["work_id"],
        "entity": {"type": row["entity_type"], "key": row["entity_key"]},
        "associations": associations,
        "metrics": json.loads(row["metric_values_json"]),
        "observation": {
            "at": row["observed_at"],
            "time_status": row["time_status"],
        },
        "source": {
            "platform": row["source"],
            "collector_version": row["collector_version"],
            "material": material,
            "historical": metadata,
        },
        "review": {
            "status": row["review_status"],
            "note": row["review_note"],
        },
        "recorded_at": row["created_at"],
        "recorded_by": row["created_by"],
    }


def _relation_data(
    connection,
    row,
    workspace_id: str,
    *,
    viewer_subject_id: str | None = None,
    viewer_subject_ids: set[str] | None = None,
) -> dict:
    target_subject_id = row["related_subject_id"]
    if target_subject_id:
        target_row = _current_subject(connection, workspace_id, target_subject_id)
        target = {
            "type": "subject",
            "id": target_subject_id,
            "kind": target_row["kind"] if target_row else None,
            "name": target_row["name"] if target_row else row["related_subject_name"],
            "name_at_link": row["related_subject_name"],
            "version_at_link": row["related_subject_version"],
            "directory_state": target_row["directory_state"] if target_row else "unavailable",
        }
    else:
        target = {
            "type": row["related_object_kind"],
            "id": row["related_object_id"],
            "label": row["related_object_label"],
        }
    source_row = _current_subject(connection, workspace_id, row["subject_id"])
    evidence_ids = json.loads(row["review_evidence_json"] or "[]")
    scope = _relation_work_scope(connection, row)
    if scope is False:
        evidence_ids = []
    elif scope is None:
        evidence_ids = [
            item["id"] for item in connection.execute(
                "SELECT id FROM business_evidence WHERE workspace_id=? AND subject_id=? AND work_id IS NULL",
                (workspace_id, row["subject_id"]),
            ).fetchall() if item["id"] in evidence_ids
        ]
    else:
        evidence_ids = [
            item["id"] for item in connection.execute(
                "SELECT id FROM business_evidence WHERE workspace_id=? AND subject_id=? AND (work_id IS NULL OR work_id=?)",
                (workspace_id, row["subject_id"], scope),
            ).fetchall() if item["id"] in evidence_ids
        ]
    evidence_rows = []
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        evidence_rows = connection.execute(
            f"SELECT * FROM business_evidence WHERE workspace_id = ? AND id IN ({placeholders}) ORDER BY recorded_at, id",
            (workspace_id, *evidence_ids),
        ).fetchall()
    return {
        "id": row["id"],
        "work_id": scope if scope is not False else None,
        "subject_id": row["subject_id"],
        "source_subject": {
            "id": row["subject_id"],
            "kind": source_row["kind"] if source_row else None,
            "name": source_row["name"] if source_row else None,
            "directory_state": source_row["directory_state"] if source_row else "unavailable",
        },
        "relation_type": row["relation_type"],
        "target": target,
        "details": json.loads(row["details_json"] or "{}"),
        "perspective": (
            "source" if viewer_subject_id == row["subject_id"] or row["subject_id"] in (viewer_subject_ids or set()) else
            "target" if (viewer_subject_id and viewer_subject_id == target_subject_id)
            or (target_subject_id and target_subject_id in (viewer_subject_ids or set())) else None
        ),
        "directory_state": row["directory_state"],
        "review": {
            "status": row["review_status"],
            "evidence_ids": evidence_ids,
            "evidence": [_evidence_data(item) for item in evidence_rows],
            "note": row["review_note"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
        },
        "valid_time": {
            "kind": row["valid_time_kind"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "observed_at": row["observed_at"],
            "end_state": "unknown" if row["valid_time_kind"] == "interval" and row["valid_to"] is None else None,
        },
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
    }


def _subject_snapshot(connection, row) -> dict:
    value = _subject_data(row)
    subject_ids = _subject_identity_group_ids(connection, row)
    markers = ",".join("?" for _ in subject_ids)
    value["evidence_ids"] = [item["id"] for item in connection.execute(
        f"SELECT id FROM business_evidence WHERE workspace_id = ? AND subject_id IN ({markers}) AND work_id IS NULL ORDER BY recorded_at, id",
        (row["workspace_id"], *subject_ids),
    ).fetchall()]
    value["merged_aliases"] = [
        {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
        for alias in _subject_merge_children(connection, row["workspace_id"], row["id"])
    ] if row["canonical_subject_id"] is None else []
    return value


def _write_subject_version(connection, row, operation: str, user_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO business_subject_versions(subject_id, workspace_id, version, operation, snapshot_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["workspace_id"], row["version"], operation, _json(_subject_snapshot(connection, row)), now, user_id),
    )


def _relation_snapshot(row) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "work_id": row["work_id"],
        "subject_id": row["subject_id"],
        "related_subject_id": row["related_subject_id"],
        "related_subject_version": row["related_subject_version"],
        "related_subject_name": row["related_subject_name"],
        "related_object_kind": row["related_object_kind"],
        "related_object_id": row["related_object_id"],
        "related_object_label": row["related_object_label"],
        "details": json.loads(row["details_json"] or "{}"),
        "relation_type": row["relation_type"],
        "directory_state": row["directory_state"],
        "review_status": row["review_status"],
        "review_evidence_ids": json.loads(row["review_evidence_json"]),
        "review_note": row["review_note"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "valid_time_kind": row["valid_time_kind"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "observed_at": row["observed_at"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
    }


def _write_relation_version(connection, row, operation: str, user_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO business_relation_versions(relation_id, workspace_id, version, operation, snapshot_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["workspace_id"], row["version"], operation, _json(_relation_snapshot(row)), now, user_id),
    )


def _case_source_rows(connection, case_id: str, workspace_id: str):
    return connection.execute(
        "SELECT s.*, m.title AS material_title, m.filename AS material_filename, "
        "m.sha256 AS material_sha256, m.status AS material_status "
        "FROM business_case_sources s LEFT JOIN materials m "
        "ON m.id = s.material_id AND m.workspace_id = s.workspace_id "
        "WHERE s.case_id = ? AND s.workspace_id = ? ORDER BY s.position",
        (case_id, workspace_id),
    ).fetchall()


def _case_provenance_data(value) -> dict | None:
    if not isinstance(value, dict) or not value:
        return None
    raw_receipt = value.get("creation_evidence_receipt")
    receipt = None
    if isinstance(raw_receipt, dict):
        raw_reference = raw_receipt.get("reference")
        receipt_id = None
        if isinstance(raw_reference, str) and ":" in raw_reference:
            _table, _separator, receipt_id = raw_reference.rpartition(":")
        receipt_status = raw_receipt.get("status")
        if receipt_status == "historical_creation_receipt_pending_integrity_revalidation":
            receipt_status = "historical_receipt_needs_current_review"
        elif receipt_status == "legacy_receipt_binding_conflict":
            receipt_status = "receipt_binding_conflict_needs_review"
        else:
            receipt_status = "receipt_unavailable"
        receipt = {
            "kind": "case_creation_receipt",
            "id": receipt_id,
            "status": receipt_status,
            "binding_status": "matches_saved_case" if raw_receipt.get("binding_status") == "source_case_fields_match" else "conflicts_with_saved_case",
            "integrity_review_status": "not_revalidated_in_current_workspace",
            "integrity_digest_recorded": bool(
                raw_receipt.get("integrity_digest") or raw_receipt.get("evidence_hash")
            ),
            "submission_id": raw_receipt.get("submission_id"),
            "recorded_at": raw_receipt.get("recorded_at"),
            "review_note": "旧回执与Case字段匹配；原环境完整性校验尚未在当前工作区复核。",
        }
    raw_source_report = value.get("source_report")
    source_report = None
    if isinstance(raw_source_report, dict):
        source_report = {
            "reference": raw_source_report.get("report_reference"),
            "status": raw_source_report.get("status"),
            "summary_artifact_id": raw_source_report.get("summary_artifact_id"),
            "summary_artifact_version": raw_source_report.get("summary_artifact_version"),
            "summary_artifact_status": raw_source_report.get("summary_artifact_status"),
            "artifact_sha256": raw_source_report.get("artifact_sha256"),
            "content_sha256": raw_source_report.get("content_sha256"),
            "lineage_sha256": raw_source_report.get("lineage_sha256"),
            "source_snapshot_sha256": raw_source_report.get("source_snapshot_sha256"),
            "body_status": raw_source_report.get("body_status"),
            "body_length": raw_source_report.get("body_length"),
            "source_report_hash_matched_creation_receipt": raw_source_report.get("source_report_hash_matched_creation_receipt"),
            "evaluations": [
                {
                    "dimension": item.get("dimension"),
                    "metric": item.get("metric_name"),
                    "historical_value": item.get("metric_value"),
                    "historical_status": item.get("status"),
                    "observed_at": item.get("evaluated_at"),
                    "current_review_status": item.get("current_review_status"),
                }
                for item in raw_source_report.get("evaluations", [])
                if isinstance(item, dict)
            ],
        }
    return {
        "origin": "historical_case",
        "source_case_id": value.get("legacy_case_id"),
        "submission_id": value.get("submission_id"),
        "original_state": value.get("legacy_state"),
        "original_created_at": value.get("created_at"),
        "source_refs_status": (
            "original_references_readable"
            if value.get("source_refs_parse_status") == "parsed"
            else "original_reference_list_unavailable"
        ),
        "creation_receipt": receipt,
        "source_report": source_report,
        "source_use_review_status": "needs_current_review",
    }


def _case_snapshot(connection, row) -> dict:
    from .business_cases import case_lifecycle
    subject = _current_subject(connection, row["workspace_id"], row["subject_id"])
    source_refs = []
    for source in _case_source_rows(connection, row["id"], row["workspace_id"]):
        source_refs.append({
            "position": source["position"],
            "source_native_id": source["source_native_id"],
            "historical_evidence_ref": source["historical_evidence_ref"],
            "material_id": source["material_id"],
            "source_title": source["source_title"],
            "source_url": source["source_url"],
            "source_sha256": source["source_sha256"],
            "link_status": source["link_status"],
        })
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "work_id": row["work_id"],
        "subject_id": row["subject_id"],
        "subject_canonical_id_at_version": subject["canonical_subject_id"] if subject else None,
        "subject_version_at_link": row["subject_version_at_link"],
        "subject_identity_review_status": subject["identity_review_status"] if subject else "unavailable",
        "submission_id": row["submission_id"],
        "title": row["title"],
        "question": row["question"],
        "state": row["state"],
        "review_status": row["review_status"],
        "source_refs_status": row["source_refs_status"],
        "creation_evidence_id": row["creation_evidence_id"],
        "source_refs": source_refs,
        "provenance": _case_provenance_data(json.loads(row["legacy_source_json"] or "{}")),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        "lifecycle": case_lifecycle(connection, row),
    }


def _case_data(connection, row) -> dict:
    snapshot = _case_snapshot(connection, row)
    subject = _current_subject(connection, row["workspace_id"], row["subject_id"])
    evidence = None
    if row["creation_evidence_id"]:
        evidence_row = connection.execute(
            "SELECT * FROM business_evidence WHERE id = ? AND workspace_id = ?",
            (row["creation_evidence_id"], row["workspace_id"]),
        ).fetchone()
        if evidence_row:
            evidence = _evidence_data(evidence_row)
    source_refs = []
    for source in _case_source_rows(connection, row["id"], row["workspace_id"]):
        material = None
        if source["material_id"]:
            material = {
                "id": source["material_id"],
                "title": source["material_title"],
                "filename": source["material_filename"],
                "sha256": source["material_sha256"],
                "status": source["material_status"],
            }
        source_refs.append({
            "position": source["position"],
            "source_native_id": source["source_native_id"],
            "historical_evidence_ref": source["historical_evidence_ref"],
            "source": {
                "title": source["source_title"],
                "url": source["source_url"],
                "sha256": source["source_sha256"],
                "material": material,
                "link_status": source["link_status"],
            },
        })
    legacy_source = json.loads(row["legacy_source_json"] or "{}")
    provenance = _case_provenance_data(legacy_source)
    receipt_status = (
        provenance.get("creation_receipt", {}).get("status")
        if provenance and isinstance(provenance.get("creation_receipt"), dict)
        else legacy_source.get("creation_evidence_status", "not_recorded")
    )
    return {
        "id": row["id"],
        "submission_id": row["submission_id"],
        "work_id": row["work_id"],
        "title": row["title"],
        "question": row["question"],
        "state": row["state"],
        "review": {
            "status": row["review_status"],
            "source_refs_status": row["source_refs_status"],
            "subject_identity_status": subject["identity_review_status"] if subject else "unavailable",
        },
        "subject": {
            "id": row["subject_id"],
            "canonical_subject_id": subject["canonical_subject_id"] if subject else None,
            "kind": subject["kind"] if subject else None,
            "name": subject["name"] if subject else None,
            "directory_state": subject["directory_state"] if subject else "unavailable",
            "version_at_link": row["subject_version_at_link"],
        },
        "creation_evidence": {
            "id": row["creation_evidence_id"],
            "historical_receipt": provenance.get("creation_receipt") if provenance else None,
            "status": "linked" if row["creation_evidence_id"] else
            receipt_status,
            "evidence": evidence,
        },
        "source_refs": source_refs,
        "provenance": provenance,
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        "snapshot": snapshot,
        "lifecycle": snapshot["lifecycle"],
    }


def _write_case_version(connection, row, operation: str, user_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO business_case_versions(case_id, workspace_id, version, operation, snapshot_json, created_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["workspace_id"], row["version"], operation,
         _json(_case_snapshot(connection, row)), now, user_id),
    )


def _detail(connection, row, identity: dict, *, work_id: str | None = None) -> dict:
    value = _subject_data(row)
    workspace_id = row["workspace_id"]
    subject_ids = _subject_identity_group_ids(connection, row)
    evidence_rows = []
    for subject_id in subject_ids:
        evidence_rows.extend(_visible_evidence_rows(connection, identity, subject_id, work_id))
    placeholders = ",".join("?" for _ in subject_ids)
    relation_rows = connection.execute(
        f"SELECT * FROM business_relations WHERE workspace_id = ? "
        f"AND (subject_id IN ({placeholders}) OR related_subject_id IN ({placeholders})) ORDER BY created_at, id",
        (workspace_id, *subject_ids, *subject_ids),
    ).fetchall()
    value["evidence"] = [_evidence_data(item) for item in evidence_rows]
    value["merged_aliases"] = [
        {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
        for alias in _subject_merge_children(connection, workspace_id, row["id"])
    ] if row["canonical_subject_id"] is None else []
    value["relationships"] = [
        _relation_data(
            connection, item, workspace_id, viewer_subject_id=row["id"],
            viewer_subject_ids=set(subject_ids),
        )
        for item in relation_rows
        if _relation_visible(connection, identity, item, requested_work_id=work_id)
    ]
    return value


def load_business_subject_context(connection, identity: dict, subject_id: str, *, work_id: str | None = None) -> dict:
    """Load one shared Subject with only workspace-shared and requested-Work context."""
    workspace_id = identity["workspace"]["id"]
    row = _current_subject(connection, workspace_id, _string(subject_id, "subject_id", maximum=200))
    if row is None:
        raise _not_found()
    if work_id is not None:
        work_id = _string(work_id, "work_id", maximum=200)
        _require_business_work(connection, identity, work_id)
    return _detail(connection, row, identity, work_id=work_id)


def _idempotent_resource(connection, workspace_id: str, operation: str, key: str, fingerprint: str, resource_type: str):
    row = connection.execute(
        "SELECT request_hash, resource_type, resource_id FROM idempotency_records WHERE workspace_id = ? AND operation = ? AND key = ?",
        (workspace_id, operation, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != fingerprint or row["resource_type"] != resource_type:
        raise ApiError("IDEMPOTENCY_CONFLICT", "This key was already used for a different business request", 409)
    return row["resource_id"]


def _record_idempotency(connection, workspace_id: str, operation: str, key: str, fingerprint: str, resource_type: str, resource_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (workspace_id, operation, key, fingerprint, resource_type, resource_id, now),
    )


def _required_idempotency_key() -> str:
    key = _idempotency_key()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for this operation", 400)
    return key


def _external_identifier_key(value: dict) -> tuple[str, str, str]:
    return (
        _normalized_name(value.get("scheme", "")),
        _normalized_name(value.get("jurisdiction") or ""),
        _normalized_name(value.get("value", "")),
    )


def _profile_location_signature(value: dict) -> tuple[str, str, str, str, str]:
    return tuple(
        _normalized_name(value.get(field, ""))
        for field in ("role", "country", "region", "city", "address")
    )


def _identity_candidates(
    connection,
    workspace_id: str,
    *,
    kind: str = "unclassified",
    name: str | None = None,
    profile: dict | None = None,
    material_text: str | None = None,
    exclude_id: str | None = None,
    limit: int = 50,
) -> tuple[list[dict], int]:
    query_profile = profile or {}
    query_names = list(dict.fromkeys(
        value for value in ([name] if name else []) + query_profile.get("aliases", [])
        if isinstance(value, str) and value.strip()
    ))
    normalized_names = {_normalized_name(value) for value in query_names}
    query_external_ids = query_profile.get("external_ids", [])
    query_locations = query_profile.get("locations", [])
    normalized_material = _normalized_name(material_text) if material_text else ""
    rows = connection.execute(
        "SELECT id,kind,name,profile_json,directory_state,identity_review_status,version,canonical_subject_id "
        "FROM business_subjects WHERE workspace_id=? AND directory_state='active' AND canonical_subject_id IS NULL ORDER BY id",
        (workspace_id,),
    ).fetchall()
    matches = []
    for row in rows:
        if row["id"] == exclude_id:
            continue
        existing_profile = json.loads(row["profile_json"] or "{}")
        aliases = existing_profile.get("aliases", []) if isinstance(existing_profile.get("aliases", []), list) else []
        merged_aliases = connection.execute(
            "SELECT id,name,kind,profile_json FROM business_subjects WHERE workspace_id=? AND canonical_subject_id=? AND directory_state='removed' ORDER BY id",
            (workspace_id, row["id"]),
        ).fetchall()
        name_values = [(row["name"], "name"), *((alias, "alias") for alias in aliases if isinstance(alias, str))]
        name_values.extend((alias["name"], "merged_alias") for alias in merged_aliases)
        normalized_existing = [(_normalized_name(value), value, reason) for value, reason in name_values if value]
        reasons = set()
        matched_literals = []
        for query_name in normalized_names:
            for existing_name, literal, reason in normalized_existing:
                if query_name == existing_name:
                    reasons.add(reason)
                    matched_literals.append(literal)
                elif len(query_name) >= 4 and len(existing_name) >= 4 and SequenceMatcher(None, query_name, existing_name).ratio() >= 0.82:
                    reasons.add("name_near_match")
                    matched_literals.append(literal)
        if normalized_material:
            for existing_name, literal, _reason in normalized_existing:
                if len(existing_name) >= 3 and existing_name in normalized_material:
                    reasons.add("material_phrase")
                    matched_literals.append(literal)

        existing_ids = existing_profile.get("external_ids", []) if isinstance(existing_profile.get("external_ids", []), list) else []
        alias_profiles = [(alias["id"], json.loads(alias["profile_json"] or "{}")) for alias in merged_aliases]
        for _alias_id, alias_profile in alias_profiles:
            alias_ids = alias_profile.get("external_ids", [])
            if isinstance(alias_ids, list):
                existing_ids.extend(alias_ids)
        exact_identifiers = []
        material_identifier_phrases = []
        identifier_conflicts = []
        if normalized_material:
            for existing_id in existing_ids:
                if isinstance(existing_id, dict) and isinstance(existing_id.get("value"), str):
                    external_value = _normalized_name(existing_id["value"])
                    if len(external_value) >= 3 and external_value in normalized_material:
                        material_identifier_phrases.append({
                            "scheme": existing_id.get("scheme"),
                            "jurisdiction": existing_id.get("jurisdiction"),
                            "value": existing_id.get("value"),
                        })
            if material_identifier_phrases:
                reasons.add("material_identifier_phrase")
                matched_literals.extend(item["value"] for item in material_identifier_phrases)
        for query_id in query_external_ids:
            if not isinstance(query_id, dict):
                continue
            query_key = _external_identifier_key(query_id)
            for existing_id in existing_ids:
                if not isinstance(existing_id, dict):
                    continue
                existing_key = _external_identifier_key(existing_id)
                if query_key == existing_key:
                    exact_identifiers.append({"scheme": query_id.get("scheme"), "jurisdiction": query_id.get("jurisdiction")})
                elif query_key[:2] == existing_key[:2]:
                    identifier_conflicts.append({
                        "scheme": query_id.get("scheme"),
                        "jurisdiction": query_id.get("jurisdiction"),
                        "query_value": query_id.get("value"),
                        "candidate_value": existing_id.get("value"),
                    })
        if exact_identifiers:
            reasons.add("stable_identifier_exact")

        existing_locations = existing_profile.get("locations", []) if isinstance(existing_profile.get("locations", []), list) else []
        for alias_id, alias_profile in alias_profiles:
            alias_locations = alias_profile.get("locations", [])
            if isinstance(alias_locations, list):
                existing_locations.extend({**location, "merged_source_subject_id": alias_id}
                                          for location in alias_locations if isinstance(location, dict))
        location_matches = []
        location_differences = []
        for query_location in query_locations:
            if not isinstance(query_location, dict):
                continue
            query_signature = _profile_location_signature(query_location)
            for existing_location in existing_locations:
                if not isinstance(existing_location, dict):
                    continue
                existing_signature = _profile_location_signature(existing_location)
                comparable = [index for index in range(1, 5) if query_signature[index] and existing_signature[index]]
                if not comparable:
                    continue
                equal = [index for index in comparable if query_signature[index] == existing_signature[index]]
                different = [index for index in comparable if query_signature[index] != existing_signature[index]]
                if not different and equal:
                    location_matches.append(existing_location)
                    reasons.add("location_match")
                elif different:
                    fields = ("role", "country", "region", "city", "address")
                    location_differences.append({
                        "fields": [fields[index] for index in different],
                        "candidate": {key: existing_location.get(key) for key in fields if existing_location.get(key)},
                    })
        if identifier_conflicts and not reasons:
            continue
        if not reasons and not identifier_conflicts:
            continue
        differences = []
        if kind != "unclassified" and row["kind"] != kind:
            differences.append({"kind": "different_object_kind", "query": kind, "candidate": row["kind"]})
        if identifier_conflicts:
            differences.append({"kind": "stable_identifier_conflict", "identifiers": identifier_conflicts})
        if location_differences:
            differences.append({"kind": "location_difference", "locations": location_differences})
        first_reason = next((reason for reason in (
            "stable_identifier_exact", "name", "alias", "merged_alias", "material_identifier_phrase",
            "location_match", "material_phrase", "name_near_match",
        ) if reason in reasons), "stable_identifier_conflict")
        matches.append({
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "version": row["version"], "directory_state": row["directory_state"],
            "identity_review_status": row["identity_review_status"],
            "match_reason": first_reason,
            "match_reasons": sorted(reasons),
            "matched_names": list(dict.fromkeys(matched_literals)),
            "stable_identifiers": exact_identifiers,
            "material_identifiers": material_identifier_phrases,
            "locations": existing_locations,
            "differences": differences,
            "identity_decision_required": True,
            "merged_aliases": [{"id": item["id"], "kind": item["kind"], "name": item["name"],
                                "profile": json.loads(item["profile_json"] or "{}")} for item in merged_aliases],
        })
    priority = {
        "stable_identifier_exact": 0, "name": 1, "alias": 2, "merged_alias": 3,
        "material_identifier_phrase": 4, "location_match": 5, "material_phrase": 6,
        "name_near_match": 7, "stable_identifier_conflict": 8,
    }
    matches.sort(key=lambda item: (priority.get(item["match_reason"], 9), item["name"].casefold(), item["id"]))
    total = len(matches)
    return matches[:limit], total


def _duplicate_candidates(
    connection,
    workspace_id: str,
    kind: str,
    name: str,
    *,
    profile: dict | None = None,
    exclude_id: str | None = None,
) -> list[dict]:
    matches, _total = _identity_candidates(
        connection, workspace_id, kind=kind, name=name, profile=profile,
        exclude_id=exclude_id, limit=20,
    )
    return matches


def _subject_pair_review(connection, workspace_id: str, source, target) -> dict:
    source_ids = _subject_identity_group_ids(connection, source)
    target_ids = _subject_identity_group_ids(connection, target)
    source_markers, target_markers = ",".join("?" for _ in source_ids), ",".join("?" for _ in target_ids)
    source_rows = connection.execute(
        f"SELECT id,kind,profile_json FROM business_subjects WHERE workspace_id=? AND id IN ({source_markers})",
        (workspace_id, *source_ids),
    ).fetchall()
    target_rows = connection.execute(
        f"SELECT id,kind,profile_json FROM business_subjects WHERE workspace_id=? AND id IN ({target_markers})",
        (workspace_id, *target_ids),
    ).fetchall()
    source_known_kinds = {row["kind"] for row in source_rows if row["kind"] != "unclassified"}
    target_known_kinds = {row["kind"] for row in target_rows if row["kind"] != "unclassified"}
    kind_conflict = bool(source_known_kinds and target_known_kinds and source_known_kinds.isdisjoint(target_known_kinds))
    stable_identifier_conflicts = []
    source_identifier_rows = []
    target_identifier_rows = []
    source_location_rows = []
    target_location_rows = []
    for row, identifiers, locations in (
        *((row, source_identifier_rows, source_location_rows) for row in source_rows),
        *((row, target_identifier_rows, target_location_rows) for row in target_rows),
    ):
        profile = json.loads(row["profile_json"] or "{}")
        for item in profile.get("external_ids", []) if isinstance(profile.get("external_ids", []), list) else []:
            if isinstance(item, dict):
                identifiers.append((row["id"], item))
        for item in profile.get("locations", []) if isinstance(profile.get("locations", []), list) else []:
            if isinstance(item, dict):
                locations.append((row["id"], item))
    for source_id, left in source_identifier_rows:
        for target_id, right in target_identifier_rows:
            left_key, right_key = _external_identifier_key(left), _external_identifier_key(right)
            if left_key[:2] == right_key[:2] and left_key[2] != right_key[2]:
                stable_identifier_conflicts.append({
                    "source_subject_id": source_id, "target_subject_id": target_id,
                    "scheme": left.get("scheme"), "jurisdiction": left.get("jurisdiction"),
                    "source_value": left.get("value"), "target_value": right.get("value"),
                })
    location_differences = []
    for source_location_id, left in source_location_rows:
        for target_location_id, right in target_location_rows:
            if left.get("role") != right.get("role"):
                continue
            fields = [
                field for field in ("country", "region", "city", "address")
                if left.get(field) and right.get(field)
                and _normalized_name(left[field]) != _normalized_name(right[field])
            ]
            if fields:
                location_differences.append({
                    "source_subject_id": source_location_id,
                    "target_subject_id": target_location_id,
                    "role": left.get("role"), "fields": fields,
                    "source": {field: left.get(field) for field in fields},
                    "target": {field: right.get(field) for field in fields},
                    "source_period": {"from": left.get("valid_from"), "to": left.get("valid_to")},
                    "target_period": {"from": right.get("valid_from"), "to": right.get("valid_to")},
                })
    relation_rows = connection.execute(
        f"SELECT id,relation_type,review_status,work_id FROM business_relations WHERE workspace_id=? AND directory_state='active' "
        f"AND ((subject_id IN ({source_markers}) AND related_subject_id IN ({target_markers})) "
        f"OR (subject_id IN ({target_markers}) AND related_subject_id IN ({source_markers}))) ORDER BY id",
        (workspace_id, *source_ids, *target_ids, *target_ids, *source_ids),
    ).fetchall()
    return {
        "kind_conflict": kind_conflict,
        "stable_identifier_conflicts": stable_identifier_conflicts,
        "location_differences": location_differences,
        "direct_relationships": [dict(row) for row in relation_rows],
        "merge_blockers": [
            *( [{"kind": "different_object_kind", "source": sorted(source_known_kinds), "target": sorted(target_known_kinds)}] if kind_conflict else [] ),
            *( [{"kind": "direct_relationship_must_be_resolved_first", "relation_ids": [row["id"] for row in relation_rows]}] if relation_rows else [] ),
        ],
        "requires_explicit_resolution": bool(stable_identifier_conflicts or location_differences),
    }


def _subject_merge_impact_counts(connection, workspace_id: str, subject_ids: list[str]) -> dict:
    markers = ",".join("?" for _ in subject_ids)
    impacts = {
        "evidence": connection.execute(
            f"SELECT COUNT(*) FROM business_evidence WHERE workspace_id=? AND subject_id IN ({markers})",
            (workspace_id, *subject_ids),
        ).fetchone()[0],
        "relations": connection.execute(
            f"SELECT COUNT(*) FROM business_relations WHERE workspace_id=? AND (subject_id IN ({markers}) OR related_subject_id IN ({markers}))",
            (workspace_id, *subject_ids, *subject_ids),
        ).fetchone()[0],
        "metric_observations": connection.execute(
            f"SELECT COUNT(*) FROM business_metric_observations WHERE workspace_id=? AND (subject_id IN ({markers}) OR brand_focus_subject_id IN ({markers}))",
            (workspace_id, *subject_ids, *subject_ids),
        ).fetchone()[0],
        "cases": connection.execute(
            f"SELECT COUNT(*) FROM business_cases WHERE workspace_id=? AND subject_id IN ({markers})",
            (workspace_id, *subject_ids),
        ).fetchone()[0],
        "artifact_versions": len(_subject_artifact_versions(connection, workspace_id, subject_ids)),
    }
    monitors = connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='monitor_rules'").fetchone()
    if monitors and "subject_id" in {row["name"] for row in connection.execute("PRAGMA table_info(monitor_rules)")}:
        impacts["monitor_rules"] = connection.execute(
            f"SELECT COUNT(*) FROM monitor_rules WHERE workspace_id=? AND subject_id IN ({markers})",
            (workspace_id, *subject_ids),
        ).fetchone()[0]
    else:
        impacts["monitor_rules"] = 0
    return impacts


def _subject_artifact_versions(connection, workspace_id: str, subject_ids: list[str]) -> list[dict]:
    if not subject_ids or connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='artifact_versions'"
    ).fetchone() is None:
        return []
    markers = ",".join("?" for _ in subject_ids)
    query = (
        "SELECT DISTINCT v.artifact_id,v.version,a.work_id FROM artifact_versions v "
        "JOIN artifacts a ON a.id=v.artifact_id AND a.workspace_id=v.workspace_id "
        "WHERE v.workspace_id=? AND ("
        f"EXISTS(SELECT 1 FROM json_each(v.input_snapshot_json,'$.business.context.subjects') s WHERE json_extract(s.value,'$.id') IN ({markers})) "
        "OR EXISTS(SELECT 1 FROM json_each(v.input_snapshot_json,'$.business.context.relationships') r "
        f"WHERE json_extract(r.value,'$.subject_id') IN ({markers}) OR json_extract(r.value,'$.related_subject_id') IN ({markers}))) "
        "ORDER BY v.artifact_id,v.version"
    )
    return [dict(row) for row in connection.execute(query, (workspace_id, *subject_ids, *subject_ids, *subject_ids)).fetchall()]


def _subject_merge_impact_details(connection, workspace_id: str, subject_ids: list[str], *, limit: int = 200) -> dict:
    markers = ",".join("?" for _ in subject_ids)
    rows = {
        "evidence": [dict(row) for row in connection.execute(
            f"SELECT id,subject_id,work_id,scope,recorded_at FROM business_evidence WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id",
            (workspace_id, *subject_ids),
        ).fetchall()],
        "relationships": [dict(row) for row in connection.execute(
            f"SELECT id,subject_id,related_subject_id,work_id,relation_type,directory_state,review_status,version FROM business_relations WHERE workspace_id=? AND (subject_id IN ({markers}) OR related_subject_id IN ({markers})) ORDER BY id",
            (workspace_id, *subject_ids, *subject_ids),
        ).fetchall()],
        "metric_observations": [dict(row) for row in connection.execute(
            f"SELECT id,subject_id,brand_focus_subject_id,work_id,material_id FROM business_metric_observations WHERE workspace_id=? AND (subject_id IN ({markers}) OR brand_focus_subject_id IN ({markers})) ORDER BY id",
            (workspace_id, *subject_ids, *subject_ids),
        ).fetchall()],
        "cases": [dict(row) for row in connection.execute(
            f"SELECT id,subject_id,work_id,state,version FROM business_cases WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id",
            (workspace_id, *subject_ids),
        ).fetchall()],
        "artifact_versions": _subject_artifact_versions(connection, workspace_id, subject_ids),
    }
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='monitor_rules'").fetchone():
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(monitor_rules)")}
        if "subject_id" in columns:
            work_column = "work_id," if "work_id" in columns else ""
            rows["monitor_rules"] = [dict(row) for row in connection.execute(
                f"SELECT id,subject_id,{work_column}status,version FROM monitor_rules WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id",
                (workspace_id, *subject_ids),
            ).fetchall()]
    clipped = {}
    truncated = {}
    for key, items in rows.items():
        clipped[key] = items[:limit]
        if len(items) > limit:
            truncated[key] = len(items)
    return {"items": clipped, "truncated_counts": truncated}


def _subject_merge_affected_work_ids(connection, workspace_id: str, subject_ids: list[str]) -> set[str]:
    markers = ",".join("?" for _ in subject_ids)
    work_ids = set()
    for table, query, params in (
        ("business_evidence", f"SELECT DISTINCT work_id FROM business_evidence WHERE workspace_id=? AND subject_id IN ({markers}) AND work_id IS NOT NULL", (workspace_id, *subject_ids)),
        ("business_relations", f"SELECT * FROM business_relations WHERE workspace_id=? AND (subject_id IN ({markers}) OR related_subject_id IN ({markers}))", (workspace_id, *subject_ids, *subject_ids)),
        ("business_metric_observations", f"SELECT DISTINCT work_id FROM business_metric_observations WHERE workspace_id=? AND (subject_id IN ({markers}) OR brand_focus_subject_id IN ({markers})) AND work_id IS NOT NULL", (workspace_id, *subject_ids, *subject_ids)),
        ("business_cases", f"SELECT DISTINCT work_id FROM business_cases WHERE workspace_id=? AND subject_id IN ({markers}) AND work_id IS NOT NULL", (workspace_id, *subject_ids)),
    ):
        for row in connection.execute(query, params).fetchall():
            if "work_id" in row.keys() and row["work_id"] is not None:
                work_ids.add(row["work_id"])
            if table == "business_relations":
                work_ids.update(_relation_private_work_ids(connection, row))
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='business_case_sources'").fetchone():
        work_ids.update(row["work_id"] for row in connection.execute(
            f"SELECT DISTINCT m.work_id FROM business_case_sources cs JOIN business_cases c ON c.id=cs.case_id AND c.workspace_id=cs.workspace_id "
            f"JOIN materials m ON m.id=cs.material_id AND m.workspace_id=cs.workspace_id "
            f"WHERE c.workspace_id=? AND c.subject_id IN ({markers}) AND m.work_id IS NOT NULL",
            (workspace_id, *subject_ids),
        ).fetchall())
    work_ids.update(
        row["work_id"] for row in _subject_artifact_versions(connection, workspace_id, subject_ids)
        if row["work_id"] is not None
    )
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='monitor_rules'").fetchone():
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(monitor_rules)")}
        if "subject_id" in columns and "work_id" in columns:
            work_ids.update(row["work_id"] for row in connection.execute(
                f"SELECT DISTINCT work_id FROM monitor_rules WHERE workspace_id=? AND subject_id IN ({markers}) AND work_id IS NOT NULL",
                (workspace_id, *subject_ids),
            ).fetchall())
    return work_ids


def _require_subject_merge_scope(connection, identity: dict, subject_ids: list[str]) -> set[str]:
    from .collaboration import (
        require_commission_access, require_work_access, require_workspace_access, work_commission,
    )

    require_workspace_access(connection, identity, write=True)
    work_ids = _subject_merge_affected_work_ids(connection, identity["workspace"]["id"], subject_ids)
    for work_id in sorted(work_ids):
        require_work_access(connection, identity, work_id)
        commission_id = work_commission(connection, work_id)
        if commission_id:
            commission = require_commission_access(connection, identity, commission_id)
            if commission["commission_role"] not in {"owner", "editor"}:
                raise ApiError("FORBIDDEN", "Only a current client owner or editor can correct this identity's private context", 403)
    return work_ids


def _subject_merge_fingerprint(connection, workspace_id: str, source_id: str, target_id: str) -> str:
    source = _current_subject(connection, workspace_id, source_id)
    target = _current_subject(connection, workspace_id, target_id)
    if source is None or target is None:
        raise _not_found()
    ids = sorted(set(_subject_identity_group_ids(connection, source) + _subject_identity_group_ids(connection, target)))
    markers = ",".join("?" for _ in ids)
    rows = {}
    rows["subjects"] = [dict(row) for row in connection.execute(
        f"SELECT * FROM business_subjects WHERE workspace_id=? AND id IN ({markers}) ORDER BY id",
        (workspace_id, *ids),
    ).fetchall()]
    for table, query in (
        ("business_evidence", f"SELECT id,subject_id,work_id,scope,source_kind,material_sha256 FROM business_evidence WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id"),
        ("business_relations", f"SELECT id,subject_id,related_subject_id,work_id,relation_type,review_status,version FROM business_relations WHERE workspace_id=? AND (subject_id IN ({markers}) OR related_subject_id IN ({markers})) ORDER BY id"),
        ("business_metric_observations", f"SELECT id,subject_id,brand_focus_subject_id,work_id,material_id FROM business_metric_observations WHERE workspace_id=? AND (subject_id IN ({markers}) OR brand_focus_subject_id IN ({markers})) ORDER BY id"),
        ("business_cases", f"SELECT id,subject_id,work_id,version FROM business_cases WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id"),
    ):
        params = (workspace_id, *ids, *ids) if "OR " in query else (workspace_id, *ids)
        rows[table] = [dict(row) for row in connection.execute(query, params).fetchall()]
    rows["artifact_versions"] = _subject_artifact_versions(connection, workspace_id, ids)
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='monitor_rules'").fetchone():
        monitor_columns = {row["name"] for row in connection.execute("PRAGMA table_info(monitor_rules)")}
        if "subject_id" in monitor_columns:
            monitor_query = f"SELECT id,subject_id,work_id,status,version FROM monitor_rules WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id" if "work_id" in monitor_columns else f"SELECT id,subject_id,status,version FROM monitor_rules WHERE workspace_id=? AND subject_id IN ({markers}) ORDER BY id"
            rows["monitor_rules"] = [dict(row) for row in connection.execute(monitor_query, (workspace_id, *ids)).fetchall()]
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='business_case_sources'").fetchone():
        rows["business_case_sources"] = [dict(row) for row in connection.execute(
            f"SELECT cs.case_id,cs.position,cs.material_id,cs.source_native_id,cs.source_sha256,cs.link_status,m.sha256 AS current_sha256,m.status AS material_status "
            f"FROM business_case_sources cs JOIN business_cases c ON c.id=cs.case_id AND c.workspace_id=cs.workspace_id "
            f"LEFT JOIN materials m ON m.id=cs.material_id AND m.workspace_id=cs.workspace_id "
            f"WHERE c.workspace_id=? AND c.subject_id IN ({markers}) ORDER BY cs.case_id,cs.position",
            (workspace_id, *ids),
        ).fetchall()]
    state = {
        "source": dict(source), "target": dict(target), "rows": rows,
    }
    return _request_fingerprint(state)


def _review_evidence_ids(
    connection,
    workspace_id: str,
    subject_id: str,
    value,
    allowed_scopes: set[str],
    *,
    work_id: str | None = None,
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 20 or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ApiError("EVIDENCE_REQUIRED", "A supported review needs one or more evidence IDs", 400)
    ids = list(dict.fromkeys(item.strip() for item in value))
    placeholders = ",".join("?" for _ in ids)
    if work_id is None:
        rows = connection.execute(
            f"SELECT id, scope, work_id FROM business_evidence WHERE workspace_id = ? AND subject_id = ? AND work_id IS NULL AND id IN ({placeholders})",
            (workspace_id, subject_id, *ids),
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT id, scope, work_id FROM business_evidence WHERE workspace_id = ? AND subject_id = ? AND (work_id IS NULL OR work_id = ?) AND id IN ({placeholders})",
            (workspace_id, subject_id, work_id, *ids),
        ).fetchall()
    if len(rows) != len(ids):
        raise ApiError("EVIDENCE_NOT_FOUND", "One or more evidence records are unavailable for this subject", 404)
    if any(row["scope"] not in allowed_scopes for row in rows):
        raise ApiError("EVIDENCE_SCOPE_MISMATCH", "Evidence does not support this review scope", 400)
    return ids


def _review_identity_group_evidence_ids(connection, workspace_id: str, subject_ids: list[str], value) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 20 or any(type(item) is not str or not item.strip() for item in value):
        raise ApiError("EVIDENCE_REQUIRED", "A merge or split review needs shared identity Evidence IDs", 400)
    ids = list(dict.fromkeys(item.strip() for item in value))
    markers = ",".join("?" for _ in ids)
    subject_markers = ",".join("?" for _ in subject_ids)
    rows = connection.execute(
        f"SELECT id,subject_id,scope FROM business_evidence WHERE workspace_id=? AND work_id IS NULL "
        f"AND subject_id IN ({subject_markers}) AND id IN ({markers})",
        (workspace_id, *subject_ids, *ids),
    ).fetchall()
    if len(rows) != len(ids):
        raise ApiError("EVIDENCE_NOT_FOUND", "Merge evidence must be shared and belong to the selected identity group", 404)
    if any(row["scope"] != "identity" for row in rows):
        raise ApiError("EVIDENCE_SCOPE_MISMATCH", "Merge and split decisions require identity-scoped Evidence", 400)
    return ids


def _private_evidence_work_ids(connection, workspace_id: str, subject_id: str, evidence_ids) -> set[str]:
    if evidence_ids is None:
        return set()
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 20 or any(type(item) is not str for item in evidence_ids):
        raise ApiError("INVALID_INPUT", "evidence_ids must be an array of up to 20 saved records", 400)
    ids = list(dict.fromkeys(item.strip() for item in evidence_ids))
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT id,work_id FROM business_evidence WHERE workspace_id=? AND subject_id=? AND id IN ({placeholders})",
        (workspace_id, subject_id, *ids),
    ).fetchall()
    if len(rows) != len(ids):
        raise ApiError("EVIDENCE_NOT_FOUND", "One or more evidence records are unavailable for this Subject", 404)
    return {row["work_id"] for row in rows if row["work_id"] is not None}


def _source_url(value) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise ApiError("INVALID_INPUT", "source.url must be a valid HTTP(S) URL", 400)
    value = value.strip()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", "source.url contains an invalid port", 400) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError("INVALID_INPUT", "source.url must be a valid HTTP(S) URL without credentials", 400)
    if port is not None and not 1 <= port <= 65535:
        raise ApiError("INVALID_INPUT", "source.url contains an invalid port", 400)
    return value


def _normalized_excerpt(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _insert_evidence(connection, identity: dict, subject_id: str, data: dict, now: str) -> tuple[str, str | None]:
    allowed = {"base_version", "scope", "source", "location", "excerpt", "observed_at", "work_id"}
    if set(data) - allowed:
        raise ApiError("INVALID_INPUT", "Evidence contains an unsupported field", 400, {"fields": sorted(set(data) - allowed)})
    scope = data.get("scope")
    if scope not in EVIDENCE_SCOPES:
        raise ApiError("INVALID_INPUT", "scope must be identity, china_operation, or relationship", 400)
    source = data.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        raise ApiError("INVALID_INPUT", "source must identify a material, public_reference, or user_statement", 400)
    excerpt = _string(data.get("excerpt"), "excerpt", maximum=4000)
    location = _optional_text(data.get("location"), "location", 300)
    observed_at = _utc_timestamp(data.get("observed_at"), "observed_at")
    source_kind = source["kind"]
    work_id = _string(data["work_id"], "work_id", maximum=200) if data.get("work_id") else None
    source_state = None
    source_title = None
    source_url = None
    material_id = None
    material_hash = None
    if source_kind == "material":
        if set(source) - {"kind", "material_id"}:
            raise ApiError("INVALID_INPUT", "Material evidence accepts only material_id", 400)
        material_id = _string(source.get("material_id"), "source.material_id", maximum=200)
        from .collaboration import require_material_access
        material = require_material_access(
            connection, identity, material_id, target_work_id=work_id,
        )
        if work_id is None and material["work_id"] is not None:
            work_id = material["work_id"]
        if work_id is not None:
            _require_business_work(connection, identity, work_id, write=True)
        material = connection.execute(
            "SELECT id, work_id, title, source_url, sha256, status, text, source_text FROM materials WHERE id = ? AND workspace_id = ?",
            (material_id, identity["workspace"]["id"]),
        ).fetchone()
        if material is None:
            raise _not_found()
        if material["status"] not in {"ready", "partial"}:
            raise ApiError("SOURCE_UNAVAILABLE", "The selected workspace material is not readable", 409)
        excerpt_texts = [material["text"] or "", material["source_text"] or ""]
        excerpt_texts.extend(item["text"] for item in connection.execute(
            "SELECT text FROM material_segments WHERE workspace_id = ? AND material_id = ?",
            (identity["workspace"]["id"], material_id),
        ).fetchall())
        normalized_excerpt = _normalized_excerpt(excerpt)
        if not any(normalized_excerpt in _normalized_excerpt(text) for text in excerpt_texts if text):
            raise ApiError("EVIDENCE_EXCERPT_NOT_FOUND", "The excerpt was not found in the selected material", 400)
        source_state = "workspace_material"
        source_title = material["title"]
        source_url = material["source_url"]
        material_hash = material["sha256"]
    elif source_kind == "public_reference":
        if set(source) - {"kind", "url", "title"}:
            raise ApiError("INVALID_INPUT", "Public reference accepts only url and title", 400)
        source_title = _string(source.get("title"), "source.title", maximum=300)
        source_url = _source_url(source.get("url"))
        source_state = "unfetched_reference"
    elif source_kind == "user_statement":
        if set(source) - {"kind", "title"}:
            raise ApiError("INVALID_INPUT", "User statement accepts only title", 400)
        source_title = _string(source.get("title"), "source.title", maximum=300)
        source_state = "user_statement"
    else:
        raise ApiError("INVALID_INPUT", "source.kind is not supported", 400)

    if work_id is not None:
        _require_business_work(connection, identity, work_id, write=True)
    else:
        _require_business_work(connection, identity, None, write=True)
    evidence_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO business_evidence(id, workspace_id, work_id, subject_id, scope, source_kind, source_state, source_title, source_url, material_id, material_sha256, source_location, excerpt, observed_at, recorded_at, recorded_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id, identity["workspace"]["id"], work_id, subject_id, scope, source_kind,
            source_state, source_title, source_url, material_id, material_hash, location,
            excerpt, observed_at, now, identity["user"]["id"],
        ),
    )
    return evidence_id, work_id


def _valid_relation_payload(data: dict, connection, identity: dict, workspace_id: str, source_subject_id: str):
    allowed = {
        "base_subject_version", "relation_type", "related_subject_id", "related_object",
        "details", "review_status", "evidence_ids", "review_note", "valid_time_kind", "valid_from",
        "valid_to", "observed_at", "work_id",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ApiError("INVALID_INPUT", "Relationship contains an unsupported field", 400, {"fields": sorted(unknown)})
    relation_type = data.get("relation_type")
    if not isinstance(relation_type, str) or relation_type not in RELATION_TYPES:
        raise ApiError("INVALID_INPUT", "relation_type is not supported", 400, {"field": "relation_type"})
    related_subject_id = data.get("related_subject_id")
    related_object = data.get("related_object")
    if (related_subject_id is None) == (related_object is None):
        raise ApiError("INVALID_INPUT", "Provide exactly one related subject or related object", 400)
    target = {"related_subject_id": None, "related_subject_version": None, "related_subject_name": None,
              "related_object_kind": None, "related_object_id": None, "related_object_label": None}
    if related_subject_id is not None:
        related_subject_id = _string(related_subject_id, "related_subject_id", maximum=200)
        if related_subject_id == source_subject_id:
            raise ApiError("INVALID_INPUT", "A subject cannot relate to itself", 400)
        if relation_type in {"operates_in", "market_activity"}:
            raise ApiError("INVALID_INPUT", f"{relation_type} must target a market related object", 400)
        related = _current_subject(connection, workspace_id, related_subject_id)
        if related is None:
            raise _not_found()
        if related["canonical_subject_id"] is not None:
            raise ApiError(
                "SUBJECT_MERGED", "Use the canonical Subject for a new relationship",
                409, {"canonical_subject_id": related["canonical_subject_id"]},
            )
        if related["directory_state"] != "active":
            raise ApiError("RELATED_SUBJECT_REMOVED", "The related subject is no longer active", 409)
        target.update({
            "related_subject_id": related["id"],
            "related_subject_version": related["version"],
            "related_subject_name": related["name"],
        })
    else:
        if not isinstance(related_object, dict) or set(related_object) - {"kind", "id", "label"}:
            raise ApiError("INVALID_INPUT", "related_object must contain kind, id, and optional label", 400)
        object_kind = related_object.get("kind")
        if not isinstance(object_kind, str) or object_kind not in RELATED_OBJECT_KINDS:
            raise ApiError("INVALID_INPUT", "related_object.kind is not supported", 400)
        object_id = _string(related_object.get("id"), "related_object.id", maximum=200)
        label = _optional_text(related_object.get("label"), "related_object.label", 300)
        if relation_type in {"operates_in", "market_activity"} and object_kind != "market":
            raise ApiError("INVALID_INPUT", f"{relation_type} must target a market related object", 400)
        if object_kind == "market":
            object_id = object_id.upper()
        target.update({"related_object_kind": object_kind, "related_object_id": object_id, "related_object_label": label})
    work_id = _string(data["work_id"], "work_id", maximum=200) if data.get("work_id") else None
    evidence_ids = data.get("evidence_ids", [])
    private_work_ids = _private_evidence_work_ids(
        connection, workspace_id, source_subject_id, evidence_ids,
    ) if evidence_ids else set()
    if work_id is None:
        if len(private_work_ids) == 1:
            work_id = next(iter(private_work_ids))
        elif len(private_work_ids) > 1:
            raise ApiError("RELATIONSHIP_SCOPE_CONFLICT", "Separate evidence from different client Works into separate relationship records", 409)
    elif private_work_ids - {work_id}:
        raise ApiError("EVIDENCE_SCOPE_MISMATCH", "A private source can only support a relationship in its authorized Work", 403)
    if work_id is not None:
        _require_business_work(connection, identity, work_id, write=True)
    else:
        _require_business_work(connection, identity, None, write=True)
    if target["related_object_kind"] == "customer_mandate":
        if relation_type != "client_mandate" or work_id is None:
            raise ApiError("CLIENT_SCOPE_REQUIRED", "A client mandate relationship must remain inside an authorized client Work", 400)
        from .collaboration import work_commission
        if work_commission(connection, work_id) != target["related_object_id"]:
            raise ApiError("CLIENT_SCOPE_MISMATCH", "The client mandate must match the Work's current commission", 403)
    review_status = _review_state(data.get("review_status", "needs_review"), RELATION_REVIEW_STATES, "review_status")
    review_note = _optional_text(data.get("review_note"), "review_note", 1000) or ""
    if review_status == "supported":
        china_operation = (
            relation_type == "operates_in"
            and target["related_object_kind"] == "market"
            and target["related_object_id"] == "CN"
        )
        allowed_scopes = {"china_operation"} if china_operation else {"relationship"}
        evidence_ids = _review_evidence_ids(connection, workspace_id, source_subject_id, evidence_ids, allowed_scopes, work_id=work_id)
    elif evidence_ids:
        evidence_ids = _review_evidence_ids(connection, workspace_id, source_subject_id, evidence_ids, EVIDENCE_SCOPES, work_id=work_id)
    elif not isinstance(evidence_ids, list):
        raise ApiError("INVALID_INPUT", "evidence_ids must be an array", 400)
    if review_status == "conflicted" and not review_note:
        raise ApiError("INVALID_INPUT", "A conflicted relationship needs a review_note", 400)
    valid_time = _relation_time(data)
    return {
        **target,
        "work_id": work_id,
        "relation_type": relation_type,
        "details": _relation_details(data.get("details"), relation_type),
        "review_status": review_status,
        "review_evidence_json": _json(evidence_ids),
        "review_note": review_note,
        "valid_time": valid_time,
    }


def _source_reference_url(value, field: str) -> str | None:
    raw = _optional_text(value, field, 2048)
    if raw is None:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ApiError("INVALID_INPUT", f"{field} must be a safe HTTP(S) reference", 400) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError("INVALID_INPUT", f"{field} must be a safe HTTP(S) reference", 400)

    def secret_key(key: str) -> bool:
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        return normalized in {
            "auth", "authorization", "bearer", "code", "cookie", "cookies", "key",
            "password", "session", "signature", "sign", "state",
        } or any(part in normalized for part in (
            "token", "secret", "credential", "password", "cookie", "signature",
            "apikey", "accesskey", "sessionid", "sessionkey", "privatekey",
            "authorization", "bearer",
        ))

    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not secret_key(key)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _relation_details(value, relation_type: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"venue_tenant"}:
        raise ApiError("INVALID_INPUT", "details contains an unsupported relationship object", 400)
    if not value:
        return {}
    if relation_type != "has_tenant":
        raise ApiError("INVALID_INPUT", "venue_tenant details require a has_tenant relationship", 400)
    tenant = value.get("venue_tenant")
    allowed = {
        "tenant_name", "unit_or_floor_literal", "category", "detail_url", "listing_url",
        "identity_state", "source_locale",
    }
    if not isinstance(tenant, dict) or set(tenant) - allowed:
        raise ApiError("INVALID_INPUT", "details.venue_tenant is invalid", 400)
    identity_state = tenant.get("identity_state", "needs_review")
    if identity_state not in {"needs_review", "brand_subject_candidate", "merchant_candidate"}:
        raise ApiError("INVALID_INPUT", "details.venue_tenant.identity_state is invalid", 400)
    return {
        "venue_tenant": {
            "tenant_name": _optional_text(tenant.get("tenant_name"), "details.venue_tenant.tenant_name", 256),
            "unit_or_floor_literal": _optional_text(tenant.get("unit_or_floor_literal"), "details.venue_tenant.unit_or_floor_literal", 128),
            "category": _optional_text(tenant.get("category"), "details.venue_tenant.category", 256),
            "detail_url": _source_reference_url(tenant.get("detail_url"), "details.venue_tenant.detail_url"),
            "listing_url": _source_reference_url(tenant.get("listing_url"), "details.venue_tenant.listing_url"),
            "identity_state": identity_state,
            "source_locale": _optional_text(tenant.get("source_locale"), "details.venue_tenant.source_locale", 40),
        },
    }


def register_business_routes(app) -> None:
    from .business_cases import register_case_lifecycle_routes, require_case_access
    from .collaboration import require_material_access, require_work_access, require_workspace_access, work_commission
    register_case_lifecycle_routes(app)
    """Register the Business HTTP consumers on an authenticated product app."""
    store = app.extensions.get("oevira.store")
    if store is None:
        raise RuntimeError("Business routes require the product Store extension")

    @app.post("/api/business/subjects/resolve")
    def business_resolve_subject_candidates():
        identity, data = g.identity, _body()
        allowed = {"name", "kind", "profile", "material_id", "work_id"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Subject resolution contains an unsupported field", 400)
        name = _optional_text(data.get("name"), "name", 200)
        kind = _kind(data["kind"]) if "kind" in data else "unclassified"
        profile = _profile(data.get("profile", {}))
        material = None
        work_id = _string(data["work_id"], "work_id", maximum=200) if data.get("work_id") else None
        if data.get("material_id") is not None:
            material_id = _string(data["material_id"], "material_id", maximum=200)
            with store.transaction(immediate=False) as connection:
                material = require_material_access(connection, identity, material_id, target_work_id=work_id)
                if material["work_id"] is not None and work_id is None:
                    work_id = material["work_id"]
                if work_id is not None:
                    _require_business_work(connection, identity, work_id)
                material_text = material["text"] or material["source_text"] or ""
                candidates, total = _identity_candidates(
                    connection, identity["workspace"]["id"], kind=kind, name=name,
                    profile=profile, material_text=material_text,
                )
                resolution_state = (
                    "conflict" if any(
                        any(item["kind"] == "stable_identifier_conflict" for item in candidate["differences"])
                        for candidate in candidates
                    ) else "candidate" if candidates else "unresolved"
                )
                return _success({
                    "resolution_state": resolution_state,
                    "query": {"name": name, "kind": kind, "material_id": material_id, "work_id": work_id},
                    "material_text_available": bool(material_text),
                    "candidates": candidates,
                    "candidate_count": total,
                    "truncated": total > len(candidates),
                    "automatic_merge": False,
                })
        if name is None and not profile.get("external_ids") and not profile.get("aliases") and not profile.get("locations"):
            raise ApiError("IDENTITY_QUERY_REQUIRED", "Start from a name, alias, stable identifier, location or saved Material", 400)
        with store.transaction(immediate=False) as connection:
            if work_id is not None:
                _require_business_work(connection, identity, work_id)
            candidates, total = _identity_candidates(
                connection, identity["workspace"]["id"], kind=kind, name=name,
                profile=profile,
            )
            resolution_state = (
                "conflict" if any(
                    any(item["kind"] == "stable_identifier_conflict" for item in candidate["differences"])
                    for candidate in candidates
                ) else "candidate" if candidates else "unresolved"
            )
            return _success({
                "resolution_state": resolution_state,
                "query": {"name": name, "kind": kind, "material_id": None, "work_id": work_id},
                "material_text_available": False,
                "candidates": candidates,
                "candidate_count": total,
                "truncated": total > len(candidates),
                "automatic_merge": False,
            })

    @app.get("/api/business/subjects")
    def business_list_subjects():
        workspace_id = g.identity["workspace"]["id"]
        kind_value = request.args.get("kind")
        kind = _kind(kind_value) if kind_value else None
        state = request.args.get("state", "active")
        if state not in {"active", "removed", "all"}:
            raise ApiError("INVALID_INPUT", "state must be active, removed, or all", 400)
        clauses = ["workspace_id = ?"]
        parameters: list = [workspace_id]
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        if state != "all":
            clauses.append("directory_state = ?")
            parameters.append(state)
        rows = store.rows(
            f"SELECT * FROM business_subjects WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id",
            tuple(parameters),
        )
        with store.transaction(immediate=False) as connection:
            return _success([{
                **_subject_data(row),
                "merged_aliases": [
                    {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
                    for alias in _subject_merge_children(connection, workspace_id, row["id"])
                ] if row["canonical_subject_id"] is None else [],
            } for row in rows])

    @app.post("/api/business/subjects")
    def business_create_subject():
        identity = _require_write()
        data = _body()
        if set(data) - {"kind", "name", "profile"}:
            raise ApiError("INVALID_INPUT", "Subject contains an unsupported field", 400)
        kind = _kind(data.get("kind"))
        name = _string(data.get("name"), "name", maximum=200)
        profile = _profile(data.get("profile", {}))
        payload = {"kind": kind, "name": name, "profile": profile}
        fingerprint = _request_fingerprint(payload)
        key = _required_idempotency_key()
        workspace_id = identity["workspace"]["id"]
        subject_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as connection:
            existing_id = _idempotent_resource(connection, workspace_id, "business.subject.create", key, fingerprint, "business_subject")
            if existing_id:
                row = _current_subject(connection, workspace_id, existing_id)
                if row is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The subject created by this request is unavailable", 409)
                matches = _duplicate_candidates(connection, workspace_id, kind, name, profile=profile, exclude_id=existing_id)
                return _success({"subject": _subject_data(row), "possible_matches": matches, "replayed": True}, 200)
            matches = _duplicate_candidates(connection, workspace_id, kind, name, profile=profile)
            connection.execute(
                "INSERT INTO business_subjects(id, workspace_id, kind, name, profile_json, directory_state, identity_review_status, version, created_at, updated_at, created_by) VALUES(?, ?, ?, ?, ?, 'active', 'needs_review', 1, ?, ?, ?)",
                (subject_id, workspace_id, kind, name, _json(profile), now, now, identity["user"]["id"]),
            )
            row = _current_subject(connection, workspace_id, subject_id)
            _write_subject_version(connection, row, "created", identity["user"]["id"], now)
            _record_idempotency(connection, workspace_id, "business.subject.create", key, fingerprint, "business_subject", subject_id, now)
            return _success({"subject": _subject_data(row), "possible_matches": matches, "replayed": False}, 201)

    @app.post("/api/business/subjects/<source_id>/merge-preview")
    def business_preview_subject_merge(source_id: str):
        identity, data = _require_write(), _body()
        if set(data) != {"target_subject_id", "source_base_version", "target_base_version"}:
            raise ApiError("INVALID_INPUT", "A merge preview needs both Subject versions and one target", 400)
        target_id = _string(data["target_subject_id"], "target_subject_id", maximum=200)
        source_version = _base_version(data["source_base_version"])
        target_version = _base_version(data["target_base_version"])
        workspace_id = identity["workspace"]["id"]
        with store.transaction(immediate=False) as connection:
            source = _current_subject(connection, workspace_id, source_id)
            target = _current_subject(connection, workspace_id, target_id)
            if source is None or target is None:
                raise _not_found()
            _require_active_subject(source)
            _require_active_subject(target)
            if source_id == target_id:
                raise ApiError("IDENTITY_MERGE_SELF", "A Subject cannot be merged into itself", 400)
            _expect_version(source, source_version)
            _expect_version(target, target_version)
            source_aliases = _subject_merge_children(connection, workspace_id, source_id)
            if source_aliases:
                raise ApiError("SUBJECT_HAS_MERGED_ALIASES", "Split this Subject's merged aliases before merging it again", 409,
                               {"merged_alias_ids": [row["id"] for row in source_aliases]})
            scope_ids = sorted(set(_subject_identity_group_ids(connection, source) + _subject_identity_group_ids(connection, target)))
            work_ids = _require_subject_merge_scope(connection, identity, scope_ids)
            pair = _subject_pair_review(connection, workspace_id, source, target)
            fingerprint = _subject_merge_fingerprint(connection, workspace_id, source_id, target_id)
            preview_token = _request_fingerprint({
                "source_id": source_id, "target_id": target_id,
                "source_version": source_version, "target_version": target_version,
                "fingerprint": fingerprint,
            })
            return _success({
                "source": _subject_data(source),
                "target": _subject_data(target),
                "source_merged_aliases": [
                    {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
                    for alias in _subject_merge_children(connection, workspace_id, source_id)
                ],
                "target_merged_aliases": [
                    {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
                    for alias in _subject_merge_children(connection, workspace_id, target_id)
                ],
                "source_identity_review": _identity_review(source),
                "target_identity_review": _identity_review(target),
                "candidate_differences": pair,
                "impacts": _subject_merge_impact_counts(connection, workspace_id, scope_ids),
                "affected_records": _subject_merge_impact_details(connection, workspace_id, scope_ids),
                "authorized_work_scope_count": len(work_ids),
                "preview_token": preview_token,
                "automatic_merge": False,
                "merge_available": not pair["merge_blockers"],
                "requires_explicit_resolution": pair["requires_explicit_resolution"],
            })

    @app.post("/api/business/subjects/<source_id>/merge")
    def business_merge_subject(source_id: str):
        identity, data = _require_write(), _body()
        expected = {
            "target_subject_id", "source_base_version", "target_base_version", "preview_token",
            "reason", "source_evidence_ids", "target_evidence_ids",
        }
        if set(data) != expected:
            raise ApiError("INVALID_INPUT", "Confirm the exact merge preview, reason and evidence for both Subjects", 400)
        target_id = _string(data["target_subject_id"], "target_subject_id", maximum=200)
        source_version = _base_version(data["source_base_version"])
        target_version = _base_version(data["target_base_version"])
        preview_token = _string(data["preview_token"], "preview_token", maximum=200)
        reason = _string(data["reason"], "reason", maximum=2000)
        key = _required_idempotency_key()
        source_evidence_ids = data["source_evidence_ids"]
        target_evidence_ids = data["target_evidence_ids"]
        request_data = {
            "source_id": source_id, "target_subject_id": target_id,
            "source_base_version": source_version, "target_base_version": target_version,
            "preview_token": preview_token, "reason": reason,
            "source_evidence_ids": source_evidence_ids,
            "target_evidence_ids": target_evidence_ids,
        }
        fingerprint = _request_fingerprint(request_data)
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            prior = _idempotent_resource(connection, workspace_id, "business.subject.merge", key, fingerprint, "business_subject_merge")
            if prior:
                source = _current_subject(connection, workspace_id, source_id)
                target = _current_subject(connection, workspace_id, target_id)
                if source is None or target is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The merged Subject versions are unavailable", 409)
                return _success({"source": _subject_data(source), "target": _subject_data(target), "replayed": True}, 200)
            source = _current_subject(connection, workspace_id, source_id)
            target = _current_subject(connection, workspace_id, target_id)
            if source is None or target is None:
                raise _not_found()
            _require_active_subject(source)
            _require_active_subject(target)
            if source_id == target_id:
                raise ApiError("IDENTITY_MERGE_SELF", "A Subject cannot be merged into itself", 400)
            _expect_version(source, source_version)
            _expect_version(target, target_version)
            if _subject_merge_children(connection, workspace_id, source_id):
                raise ApiError("SUBJECT_HAS_MERGED_ALIASES", "Split this Subject's merged aliases before merging it again", 409)
            scope_ids = sorted(set(_subject_identity_group_ids(connection, source) + _subject_identity_group_ids(connection, target)))
            _require_subject_merge_scope(connection, identity, scope_ids)
            pair = _subject_pair_review(connection, workspace_id, source, target)
            if pair["merge_blockers"]:
                raise ApiError("SUBJECT_MERGE_BLOCKED", "Resolve object-type or direct relationship conflicts before merging", 409, pair)
            current_token = _request_fingerprint({
                "source_id": source_id, "target_id": target_id,
                "source_version": source_version, "target_version": target_version,
                "fingerprint": _subject_merge_fingerprint(connection, workspace_id, source_id, target_id),
            })
            if current_token != preview_token:
                raise ApiError("SUBJECT_MERGE_PREVIEW_STALE", "Subject or dependent records changed after this merge preview", 409)
            source_evidence = _review_identity_group_evidence_ids(
                connection, workspace_id, _subject_identity_group_ids(connection, source), source_evidence_ids,
            )
            target_evidence = _review_identity_group_evidence_ids(
                connection, workspace_id, _subject_identity_group_ids(connection, target), target_evidence_ids,
            )
            if source["kind"] != target["kind"] and "unclassified" not in {source["kind"], target["kind"]}:
                raise ApiError("SUBJECT_KIND_CONFLICT", "Different real object kinds cannot be merged into one Subject", 409)
            resolution_id = str(uuid.uuid4())
            resolution = {
                "state": "merged_into", "resolution_id": resolution_id,
                "canonical_subject_id": target_id, "reason": reason,
                "canonical_kind_before_merge": target["kind"],
                "canonical_kind_after_merge": source["kind"] if target["kind"] == "unclassified" and source["kind"] != "unclassified" else target["kind"],
                "canonical_version_after_merge": target["version"] + 1,
                "source_evidence_ids": source_evidence, "target_evidence_ids": target_evidence,
                "stable_identifier_conflicts": pair["stable_identifier_conflicts"],
                "location_differences": pair["location_differences"],
                "preview_token": preview_token, "decided_by": identity["user"]["id"], "decided_at": now,
            }
            connection.execute(
                "UPDATE business_subjects SET directory_state='removed',canonical_subject_id=?,identity_resolution_json=?,version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (target_id, _json(resolution), now, source_id, workspace_id, source_version),
            )
            target_kind = resolution["canonical_kind_after_merge"]
            connection.execute(
                "UPDATE business_subjects SET kind=?,identity_review_status='needs_review',identity_review_evidence_json='[]',identity_review_note='A Subject was explicitly merged; review the canonical identity again.',identity_reviewed_by=NULL,identity_reviewed_at=NULL,version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (target_kind, now, target_id, workspace_id, target_version),
            )
            merged_source = _current_subject(connection, workspace_id, source_id)
            canonical = _current_subject(connection, workspace_id, target_id)
            _write_subject_version(connection, merged_source, "merged_into", identity["user"]["id"], now)
            _write_subject_version(connection, canonical, "merge_received_needs_review", identity["user"]["id"], now)
            from .monitoring import stop_monitors_for_removed_subject
            stop_monitors_for_removed_subject(connection, workspace_id, source_id, identity["user"]["id"], now)
            _record_idempotency(connection, workspace_id, "business.subject.merge", key, fingerprint, "business_subject_merge", source_id, now)
            return _success({
                "source": _subject_data(merged_source), "target": _subject_data(canonical),
                "impacts": _subject_merge_impact_counts(connection, workspace_id, scope_ids),
                "affected_records": _subject_merge_impact_details(connection, workspace_id, scope_ids),
                "monitors_stopped": True, "history_preserved": True, "replayed": False,
            }, 201)

    @app.post("/api/business/subjects/<source_id>/split-preview")
    def business_preview_subject_split(source_id: str):
        identity, data = _require_write(), _body()
        if set(data) != {"base_version", "canonical_base_version"}:
            raise ApiError("INVALID_INPUT", "A split preview needs the merged and canonical Subject versions", 400)
        source_version = _base_version(data["base_version"])
        target_version = _base_version(data["canonical_base_version"])
        workspace_id = identity["workspace"]["id"]
        with store.transaction(immediate=False) as connection:
            source = _current_subject(connection, workspace_id, source_id)
            if source is None:
                raise _not_found()
            if source["canonical_subject_id"] is None or source["directory_state"] != "removed":
                raise ApiError("SUBJECT_NOT_MERGED", "Only an explicitly merged Subject can be split back out", 409)
            target_id = source["canonical_subject_id"]
            target = _current_subject(connection, workspace_id, target_id)
            if target is None or target["directory_state"] != "active" or target["canonical_subject_id"] is not None:
                raise ApiError("CANONICAL_SUBJECT_UNAVAILABLE", "The canonical Subject is no longer active", 409)
            _expect_version(source, source_version)
            _expect_version(target, target_version)
            scope_ids = sorted(set(_subject_identity_group_ids(connection, source) + _subject_identity_group_ids(connection, target)))
            work_ids = _require_subject_merge_scope(connection, identity, scope_ids)
            token = _request_fingerprint({
                "operation": "split", "source_id": source_id, "target_id": target_id,
                "source_version": source_version, "target_version": target_version,
                "fingerprint": _subject_merge_fingerprint(connection, workspace_id, source_id, target_id),
            })
            return _success({
                "source": _subject_data(source), "canonical": _subject_data(target),
                "canonical_merged_aliases": [
                    {"id": alias["id"], "kind": alias["kind"], "name": alias["name"], "version": alias["version"]}
                    for alias in _subject_merge_children(connection, workspace_id, target_id)
                ],
                "impacts": _subject_merge_impact_counts(connection, workspace_id, scope_ids),
                "affected_records": _subject_merge_impact_details(connection, workspace_id, scope_ids),
                "authorized_work_scope_count": len(work_ids),
                "preview_token": token,
                "history_preserved": True,
                "work_links_remain_with_original_subject_id": True,
                "monitors_will_remain_paused": True,
            })

    @app.post("/api/business/subjects/<source_id>/split")
    def business_split_subject(source_id: str):
        identity, data = _require_write(), _body()
        expected = {"base_version", "canonical_base_version", "preview_token", "reason", "source_evidence_ids", "canonical_evidence_ids"}
        if set(data) != expected:
            raise ApiError("INVALID_INPUT", "A split needs both current versions, a reason and evidence for each identity", 400)
        source_version = _base_version(data["base_version"])
        target_version = _base_version(data["canonical_base_version"])
        preview_token = _string(data["preview_token"], "preview_token", maximum=200)
        reason = _string(data["reason"], "reason", maximum=2000)
        source_evidence_ids, target_evidence_ids = data["source_evidence_ids"], data["canonical_evidence_ids"]
        key = _required_idempotency_key()
        workspace_id, now = identity["workspace"]["id"], utc_now()
        request_data = {"source_id": source_id, **data, "base_version": source_version, "canonical_base_version": target_version}
        fingerprint = _request_fingerprint(request_data)
        with store.transaction() as connection:
            prior = _idempotent_resource(connection, workspace_id, "business.subject.split", key, fingerprint, "business_subject_split")
            if prior:
                source = _current_subject(connection, workspace_id, source_id)
                target = _current_subject(connection, workspace_id, prior)
                if source is None or target is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The split Subject versions are unavailable", 409)
                return _success({"source": _subject_data(source), "canonical": _subject_data(target), "replayed": True}, 200)
            source = _current_subject(connection, workspace_id, source_id)
            if source is None:
                raise _not_found()
            if source["canonical_subject_id"] is None or source["directory_state"] != "removed":
                raise ApiError("SUBJECT_NOT_MERGED", "Only an explicitly merged Subject can be split back out", 409)
            target_id = source["canonical_subject_id"]
            target = _current_subject(connection, workspace_id, target_id)
            if target is None or target["directory_state"] != "active" or target["canonical_subject_id"] is not None:
                raise ApiError("CANONICAL_SUBJECT_UNAVAILABLE", "The canonical Subject is no longer active", 409)
            _expect_version(source, source_version)
            _expect_version(target, target_version)
            scope_ids = sorted(set(_subject_identity_group_ids(connection, source) + _subject_identity_group_ids(connection, target)))
            _require_subject_merge_scope(connection, identity, scope_ids)
            current_token = _request_fingerprint({
                "operation": "split", "source_id": source_id, "target_id": target_id,
                "source_version": source_version, "target_version": target_version,
                "fingerprint": _subject_merge_fingerprint(connection, workspace_id, source_id, target_id),
            })
            if current_token != preview_token:
                raise ApiError("SUBJECT_SPLIT_PREVIEW_STALE", "The Subject or one of its linked records changed after preview", 409)
            source_evidence = _review_identity_group_evidence_ids(
                connection, workspace_id, _subject_identity_group_ids(connection, source), source_evidence_ids,
            )
            target_evidence = _review_identity_group_evidence_ids(
                connection, workspace_id, _subject_identity_group_ids(connection, target), target_evidence_ids,
            )
            resolution_id = str(uuid.uuid4())
            prior_resolution = json.loads(source["identity_resolution_json"] or "{}")
            resolution = {
                "state": "split_from", "resolution_id": resolution_id,
                "previous_canonical_subject_id": target_id, "reason": reason,
                "source_evidence_ids": source_evidence, "canonical_evidence_ids": target_evidence,
                "decided_by": identity["user"]["id"], "decided_at": now,
            }
            connection.execute(
                "UPDATE business_subjects SET directory_state='active',canonical_subject_id=NULL,identity_resolution_json=?,identity_review_status='needs_review',identity_review_evidence_json='[]',identity_review_note='The prior identity merge was split; confirm this identity separately.',identity_reviewed_by=NULL,identity_reviewed_at=NULL,version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (_json(resolution), now, source_id, workspace_id, source_version),
            )
            target_kind = target["kind"]
            if (target["version"] == prior_resolution.get("canonical_version_after_merge")
                    and target["kind"] == prior_resolution.get("canonical_kind_after_merge")
                    and prior_resolution.get("canonical_kind_before_merge") is not None):
                target_kind = prior_resolution["canonical_kind_before_merge"]
            connection.execute(
                "UPDATE business_subjects SET kind=?,identity_review_status='needs_review',identity_review_evidence_json='[]',identity_review_note='A merged Subject was split; confirm the remaining canonical identity again.',identity_reviewed_by=NULL,identity_reviewed_at=NULL,version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (target_kind, now, target_id, workspace_id, target_version),
            )
            separated = _current_subject(connection, workspace_id, source_id)
            canonical = _current_subject(connection, workspace_id, target_id)
            _write_subject_version(connection, separated, "split_from_canonical", identity["user"]["id"], now)
            _write_subject_version(connection, canonical, "split_remains_needs_review", identity["user"]["id"], now)
            _record_idempotency(connection, workspace_id, "business.subject.split", key, fingerprint, "business_subject_split", target_id, now)
            return _success({
                "source": _subject_data(separated), "canonical": _subject_data(canonical),
                "affected_records": _subject_merge_impact_details(connection, workspace_id, scope_ids),
                "history_preserved": True, "monitors_remain_paused": True, "replayed": False,
            }, 201)

    @app.get("/api/business/subjects/<subject_id>")
    def business_get_subject(subject_id: str):
        workspace_id = g.identity["workspace"]["id"]
        work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            row = _current_subject(connection, workspace_id, subject_id)
            if row is None:
                raise _not_found()
            if work_id:
                _require_business_work(connection, g.identity, work_id)
            return _success(_detail(connection, row, g.identity, work_id=work_id))

    @app.get("/api/business/subjects/<subject_id>/evidence")
    def business_list_evidence(subject_id: str):
        workspace_id = g.identity["workspace"]["id"]
        work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            subject = _current_subject(connection, workspace_id, subject_id)
            if subject is None:
                raise _not_found()
            rows = []
            for member_id in _subject_identity_group_ids(connection, subject):
                rows.extend(_visible_evidence_rows(connection, g.identity, member_id, work_id))
            return _success([_evidence_data(row) for row in rows])

    @app.get("/api/business/subjects/<subject_id>/metric-observations")
    def business_list_subject_metric_observations(subject_id: str):
        workspace_id = g.identity["workspace"]["id"]
        work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            if work_id:
                _require_business_work(connection, g.identity, work_id)
            subject = _current_subject(connection, workspace_id, subject_id)
            if subject is None:
                raise _not_found()
            subject_ids = _subject_identity_group_ids(connection, subject)
            subject_markers = ",".join("?" for _ in subject_ids)
            rows = connection.execute(
                "SELECT o.*, m.title AS material_title, m.filename AS material_filename, "
                "m.sha256 AS material_sha256, m.status AS material_status "
                "FROM business_metric_observations o LEFT JOIN materials m "
                "ON m.id = o.material_id AND m.workspace_id = o.workspace_id "
                f"WHERE o.workspace_id = ? AND (o.subject_id IN ({subject_markers}) OR o.brand_focus_subject_id IN ({subject_markers})) "
                "AND (o.work_id IS NULL OR o.work_id = ?) "
                "ORDER BY o.observed_at, o.id",
                (workspace_id, *subject_ids, *subject_ids, work_id),
            ).fetchall()
            return _success([_metric_observation_data(row) for row in rows])

    @app.get("/api/business/cases")
    def business_list_cases():
        workspace_id = g.identity["workspace"]["id"]
        state = request.args.get("state", "open")
        if state not in {"open", "closed", "all"}:
            raise ApiError("INVALID_INPUT", "state must be open, closed, or all", 400)
        clauses = ["workspace_id = ?"]
        parameters: list = [workspace_id]
        if state != "all":
            clauses.append("state = ?")
            parameters.append(state)
        subject_id = request.args.get("subject_id")
        if subject_id:
            subject_id = _string(subject_id, "subject_id", maximum=200)
        work_id = request.args.get("work_id")
        if work_id:
            clauses.append("work_id = ?")
            parameters.append(_string(work_id, "work_id", maximum=200))
        with store.transaction(immediate=False) as connection:
            if subject_id:
                subject = _current_subject(connection, workspace_id, subject_id)
                if subject is None:
                    raise _not_found()
                subject_ids = _subject_identity_group_ids(connection, subject)
                markers = ",".join("?" for _ in subject_ids)
                clauses.append(f"subject_id IN ({markers})")
                parameters.extend(subject_ids)
            rows = connection.execute(
                f"SELECT * FROM business_cases WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id",
                tuple(parameters),
            ).fetchall()
            visible = []
            for row in rows:
                try:
                    require_case_access(connection, g.identity, row["id"])
                except ApiError as error:
                    if error.status in {403, 404}:
                        continue
                    raise
                visible.append(_case_data(connection, row))
            return _success(visible)

    @app.post("/api/business/cases")
    def business_create_case():
        identity = _require_write()
        data = _body()
        allowed = {"subject_id", "work_id", "submission_id", "title", "question", "source_refs"}
        if set(data) - allowed or not {"title", "question", "source_refs"} <= set(data):
            raise ApiError("INVALID_INPUT", "Case fields are incomplete or unsupported", 400)
        subject_id = _string(data["subject_id"], "subject_id", maximum=200) if data.get("subject_id") else None
        work_id = _string(data["work_id"], "work_id", maximum=200) if data.get("work_id") else None
        submission_id = _string(data["submission_id"], "submission_id", maximum=200) if data.get("submission_id") is not None else None
        title = _string(data["title"], "title", maximum=300)
        question = _string(data["question"], "question", maximum=5000)
        source_refs = data["source_refs"]
        if not isinstance(source_refs, list):
            raise ApiError("SOURCE_REQUIRED", "Saved Case sources must be an array; an initial user report may have none", 400)
        normalized_refs = []
        for index, source in enumerate(source_refs):
            if not isinstance(source, dict) or set(source) - {"material_id", "source_native_id"}:
                raise ApiError("INVALID_INPUT", "Each Case source needs a Material and optional native source ID", 400,
                               {"index": index})
            material_id = _string(source.get("material_id"), "source_refs[].material_id", maximum=200)
            native_id = _optional_text(source.get("source_native_id"), "source_refs[].source_native_id", 300)
            normalized_refs.append({"material_id": material_id, "source_native_id": native_id})
        payload = {
            "subject_id": subject_id, "submission_id": submission_id,
            "title": title, "question": question, "source_refs": normalized_refs,
        }
        if work_id:
            payload["work_id"] = work_id
        fingerprint = _request_fingerprint(payload)
        key = _required_idempotency_key()
        workspace_id = identity["workspace"]["id"]
        case_id = str(uuid.uuid4())
        stored_submission_id = submission_id or case_id
        now = utc_now()
        with store.transaction() as connection:
            require_workspace_access(connection, identity, write=True)
            if work_id:
                require_work_access(connection, identity, work_id)
            prior = connection.execute(
                "SELECT request_hash, resource_id FROM idempotency_records WHERE workspace_id = ? "
                "AND operation = 'business.case.create' AND key = ?",
                (workspace_id, key),
            ).fetchone()
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "This key was used for a different Case", 409)
                row = connection.execute(
                    "SELECT * FROM business_cases WHERE id = ? AND workspace_id = ?",
                    (prior["resource_id"], workspace_id),
                ).fetchone()
                if row is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The original Case is unavailable", 409)
                require_case_access(connection, identity, row["id"])
                return _success({"case": _case_data(connection, row), "replayed": True}, 200)
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
            subject = _current_subject(connection, workspace_id, subject_id) if subject_id else None
            if subject_id and subject is None:
                raise _not_found()
            if subject is not None and subject["canonical_subject_id"] is not None:
                raise ApiError(
                    "SUBJECT_MERGED",
                    "Use the selected canonical Subject for new Case work",
                    409,
                    {"canonical_subject_id": subject["canonical_subject_id"]},
                )
            if subject and subject["directory_state"] != "active":
                raise ApiError("SUBJECT_REMOVED", "Choose an active subject for new service work", 409)
            material_rows = []
            for source in normalized_refs:
                material = require_material_access(connection, identity, source["material_id"], target_work_id=work_id)
                if work_id is None and material["work_id"] is not None:
                    raise ApiError("CASE_WORK_REQUIRED", "Keep Work-scoped source material inside an authorized Work", 403)
                material_rows.append((source, material))
            connection.execute(
                "INSERT INTO business_cases(id, workspace_id, subject_id, subject_version_at_link, work_id, submission_id, title, question, state, review_status, source_refs_status, creation_evidence_id, legacy_source_json, version, created_at, updated_at, created_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'open', 'needs_review', 'needs_review', NULL, '{}', 1, ?, ?, ?)",
                (case_id, workspace_id, subject_id, subject["version"] if subject else None, work_id, stored_submission_id, title, question, now, now, identity["user"]["id"]),
            )
            connection.executemany(
                "INSERT INTO business_case_sources(case_id, workspace_id, position, material_id, source_native_id, historical_evidence_ref, source_title, source_url, source_sha256, link_status) "
                "VALUES(?, ?, ?, ?, ?, NULL, ?, ?, ?, 'material_linked_needs_review')",
                [
                    (case_id, workspace_id, position, source["material_id"], source["source_native_id"],
                     material["title"], _source_reference_url(material["source_url"], "source_refs[].url"), material["sha256"])
                    for position, (source, material) in enumerate(material_rows)
                ],
            )
            row = connection.execute(
                "SELECT * FROM business_cases WHERE id = ? AND workspace_id = ?",
                (case_id, workspace_id),
            ).fetchone()
            _write_case_version(connection, row, "created_needs_review", identity["user"]["id"], now)
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id, operation, key, request_hash, resource_type, resource_id, created_at) "
                "VALUES(?, 'business.case.create', ?, ?, 'business_case', ?, ?)",
                (workspace_id, key, fingerprint, case_id, now),
            )
            return _success({"case": _case_data(connection, row), "replayed": False}, 201)

    @app.get("/api/business/cases/<case_id>")
    def business_get_case(case_id: str):
        row = store.row(
            "SELECT * FROM business_cases WHERE id = ? AND workspace_id = ?",
            (case_id, g.identity["workspace"]["id"]),
        )
        if row is None:
            raise _not_found()
        with store.transaction(immediate=False) as connection:
            row = require_case_access(connection, g.identity, case_id)
            return _success(_case_data(connection, row))

    @app.get("/api/business/cases/<case_id>/versions")
    def business_case_versions(case_id: str):
        workspace_id = g.identity["workspace"]["id"]
        with store.transaction(immediate=False) as connection:
            require_case_access(connection, g.identity, case_id)
            exists = connection.execute(
                "SELECT 1 FROM business_cases WHERE id = ? AND workspace_id = ?",
                (case_id, workspace_id),
            ).fetchone()
            if exists is None:
                raise _not_found()
            rows = connection.execute(
                "SELECT version, operation, snapshot_json, created_at, created_by FROM business_case_versions "
                "WHERE case_id = ? AND workspace_id = ? ORDER BY version",
                (case_id, workspace_id),
            ).fetchall()
            return _success([{
                "version": row["version"],
                "operation": row["operation"],
                "snapshot": json.loads(row["snapshot_json"]),
                "created_at": row["created_at"],
                "created_by": row["created_by"],
            } for row in rows])

    @app.patch("/api/business/cases/<case_id>")
    def business_update_case(case_id: str):
        identity = _require_write()
        data = _body()
        if set(data) - {"base_version", "title", "question"} or len(data) == 1:
            raise ApiError("INVALID_INPUT", "Case update contains unsupported fields", 400)
        base_version = _base_version(data.get("base_version"))
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = require_case_access(connection, identity, case_id, write=True)
            if row is None:
                raise _not_found()
            if row["state"] != "open":
                raise ApiError("CASE_CLOSED", "Reopen this Case before changing its problem or title", 409)
            if row["version"] != base_version:
                raise ApiError("VERSION_CONFLICT", "Case has changed since it was opened", 409,
                               {"current": _case_data(connection, row)})
            title = _string(data["title"], "title", maximum=300) if "title" in data else row["title"]
            question = _string(data["question"], "question", maximum=5000) if "question" in data else row["question"]
            question_changed = question != row["question"]
            if title == row["title"] and not question_changed:
                return _success(_case_data(connection, row))
            new_version = row["version"] + 1
            if question_changed:
                from .business_cases import case_lifecycle
                lifecycle = case_lifecycle(connection, row)
                previous_hash = hashlib.sha256(row["question"].encode("utf-8")).hexdigest()
                problem_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
                event_payload = {
                    "previous_problem_sha256": previous_hash,
                    "case_question_sha256": problem_hash,
                    "invalidated_resolution": lifecycle["resolution"] is not None,
                    "invalidated_response": lifecycle["response"] is not None,
                    "requires_new_outcome_basis": True,
                    "provenance": "user_recorded_problem_revision",
                }
                connection.execute(
                    "INSERT INTO business_case_events(id,case_id,workspace_id,case_version,kind,payload_json,occurred_at,recorded_at,recorded_by) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), case_id, workspace_id, new_version, "revise_problem",
                     json.dumps(event_payload, ensure_ascii=False, allow_nan=False), now, now,
                     identity["user"]["id"]),
                )
                connection.execute(
                    "UPDATE business_case_sources SET link_status='material_linked_needs_review' WHERE case_id=? AND workspace_id=?",
                    (case_id, workspace_id),
                )
            connection.execute(
                "UPDATE business_cases SET title = ?, question = ?, review_status = ?, source_refs_status = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                (title, question, "needs_review" if question_changed else row["review_status"],
                 "needs_review" if question_changed else row["source_refs_status"], now,
                 case_id, workspace_id, base_version),
            )
            updated = connection.execute(
                "SELECT * FROM business_cases WHERE id = ? AND workspace_id = ?",
                (case_id, workspace_id),
            ).fetchone()
            _write_case_version(connection, updated, "content_updated", identity["user"]["id"], now)
            return _success(_case_data(connection, updated))

    @app.get("/api/business/materials/<material_id>/metric-observations")
    def business_list_material_metric_observations(material_id: str):
        workspace_id = g.identity["workspace"]["id"]
        requested_work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            material = require_material_access(
                connection, g.identity, material_id, target_work_id=requested_work_id,
            )
            metric_work_id = requested_work_id or material["work_id"]
            if metric_work_id:
                _require_business_work(connection, g.identity, metric_work_id)
            rows = connection.execute(
                "SELECT o.*, m.title AS material_title, m.filename AS material_filename, "
                "m.sha256 AS material_sha256, m.status AS material_status "
                "FROM business_metric_observations o LEFT JOIN materials m "
                "ON m.id = o.material_id AND m.workspace_id = o.workspace_id "
                "WHERE o.workspace_id = ? AND o.material_id = ? AND (o.work_id IS NULL OR o.work_id = ?) "
                "ORDER BY o.observed_at, o.id",
                (workspace_id, material_id, metric_work_id),
            ).fetchall()
            return _success([_metric_observation_data(row) for row in rows])

    @app.get("/api/business/subjects/<subject_id>/versions")
    def business_subject_versions(subject_id: str):
        workspace_id = g.identity["workspace"]["id"]
        with store.transaction(immediate=False) as connection:
            if _current_subject(connection, workspace_id, subject_id) is None:
                raise _not_found()
            rows = connection.execute(
                "SELECT version, operation, snapshot_json, created_at, created_by FROM business_subject_versions WHERE workspace_id = ? AND subject_id = ? ORDER BY version",
                (workspace_id, subject_id),
            ).fetchall()
            return _success([{
                "version": row["version"], "operation": row["operation"],
                "snapshot": json.loads(row["snapshot_json"]),
                "created_at": row["created_at"], "created_by": row["created_by"],
            } for row in rows])

    @app.patch("/api/business/subjects/<subject_id>")
    def business_update_subject(subject_id: str):
        identity = _require_write()
        data = _body()
        allowed = {"base_version", "kind", "name", "profile", "identity_review_status", "review_evidence_ids", "review_note"}
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Subject update contains an unsupported field", 400)
        base_version = _base_version(data.get("base_version"))
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = _current_subject(connection, workspace_id, subject_id)
            if row is None:
                raise _not_found()
            _require_active_subject(row)
            _expect_version(row, base_version)
            kind = _kind(data["kind"]) if "kind" in data else row["kind"]
            name = _string(data["name"], "name", maximum=200) if "name" in data else row["name"]
            current_profile = json.loads(row["profile_json"])
            profile = _profile(data["profile"], current_profile) if "profile" in data else current_profile
            identity_fields_changed = kind != row["kind"] or name != row["name"] or any(
                current_profile.get(field) != profile.get(field)
                for field in ("aliases", "external_ids", "headquarters", "locations")
            )
            if identity_fields_changed and "identity_review_status" in data:
                raise ApiError(
                    "IDENTITY_REVIEW_REQUIRES_NEW_EVIDENCE",
                    "Change the identity details first, then add or select evidence and review the new identity version",
                    409,
                )
            review_status = row["identity_review_status"]
            evidence_json = row["identity_review_evidence_json"]
            review_note = row["identity_review_note"]
            reviewed_by = row["identity_reviewed_by"]
            reviewed_at = row["identity_reviewed_at"]
            if "identity_review_status" in data:
                review_status = _review_state(data["identity_review_status"], IDENTITY_REVIEW_STATES, "identity_review_status")
                review_note = _optional_text(data.get("review_note"), "review_note", 1000) or ""
                if review_status == "supported":
                    evidence_ids = _review_evidence_ids(connection, workspace_id, subject_id, data.get("review_evidence_ids"), {"identity"})
                    evidence_json = _json(evidence_ids)
                    reviewed_by, reviewed_at = identity["user"]["id"], now
                elif review_status == "conflicted":
                    if not review_note:
                        raise ApiError("INVALID_INPUT", "A conflicted identity review needs a review_note", 400)
                    evidence_ids = data.get("review_evidence_ids", [])
                    evidence_json = _json(_review_evidence_ids(connection, workspace_id, subject_id, evidence_ids, {"identity"})) if evidence_ids else "[]"
                    reviewed_by, reviewed_at = identity["user"]["id"], now
                else:
                    evidence_json, review_note, reviewed_by, reviewed_at = "[]", "", None, None
            elif "review_evidence_ids" in data or "review_note" in data:
                raise ApiError("INVALID_INPUT", "Review evidence and note require identity_review_status", 400)
            if identity_fields_changed:
                review_status, evidence_json, review_note, reviewed_by, reviewed_at = "needs_review", "[]", "", None, None
            changed = (
                kind != row["kind"] or name != row["name"] or _json(profile) != row["profile_json"]
                or review_status != row["identity_review_status"]
                or evidence_json != row["identity_review_evidence_json"]
                or review_note != row["identity_review_note"]
                or reviewed_by != row["identity_reviewed_by"]
                or reviewed_at != row["identity_reviewed_at"]
            )
            if not changed:
                return _success(_subject_data(row))
            connection.execute(
                "UPDATE business_subjects SET kind = ?, name = ?, profile_json = ?, identity_review_status = ?, identity_review_evidence_json = ?, identity_review_note = ?, identity_reviewed_by = ?, identity_reviewed_at = ?, version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (kind, name, _json(profile), review_status, evidence_json, review_note, reviewed_by, reviewed_at, now, subject_id, workspace_id, base_version),
            )
            updated = _current_subject(connection, workspace_id, subject_id)
            _write_subject_version(connection, updated, "updated", identity["user"]["id"], now)
            response_data = _subject_data(updated)
            response_data["possible_matches"] = _duplicate_candidates(
                connection, workspace_id, kind, name, profile=profile, exclude_id=subject_id,
            )
            return _success(response_data)

    @app.delete("/api/business/subjects/<subject_id>")
    def business_remove_subject(subject_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Removal requires only base_version", 400)
        base_version = _base_version(data["base_version"])
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = _current_subject(connection, workspace_id, subject_id)
            if row is None:
                raise _not_found()
            if row["directory_state"] == "removed":
                return _success(_subject_data(row))
            merged_aliases = _subject_merge_children(connection, workspace_id, subject_id)
            if merged_aliases:
                raise ApiError(
                    "SUBJECT_HAS_MERGED_ALIASES",
                    "Split merged aliases before removing their canonical Subject",
                    409,
                    {"merged_alias_ids": [item["id"] for item in merged_aliases]},
                )
            _expect_version(row, base_version)
            connection.execute(
                "UPDATE business_subjects SET directory_state = 'removed', version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (now, subject_id, workspace_id, base_version),
            )
            updated = _current_subject(connection, workspace_id, subject_id)
            _write_subject_version(connection, updated, "removed", identity["user"]["id"], now)
            from .monitoring import stop_monitors_for_removed_subject

            stop_monitors_for_removed_subject(
                connection, workspace_id, subject_id, identity["user"]["id"], now,
            )
            return _success(_subject_data(updated))

    @app.post("/api/business/subjects/<subject_id>/restore")
    def business_restore_subject(subject_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Restore requires only base_version", 400)
        base_version = _base_version(data["base_version"])
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = _current_subject(connection, workspace_id, subject_id)
            if row is None:
                raise _not_found()
            if row["canonical_subject_id"] is not None:
                raise ApiError(
                    "SUBJECT_MERGED",
                    "Undo the explicit identity merge through the split operation; generic restore cannot split identities",
                    409,
                    {"canonical_subject_id": row["canonical_subject_id"]},
                )
            if row["directory_state"] == "active":
                return _success(_subject_data(row))
            _expect_version(row, base_version)
            connection.execute(
                "UPDATE business_subjects SET directory_state = 'active', identity_review_status = 'needs_review', identity_review_evidence_json = '[]', identity_review_note = '', identity_reviewed_by = NULL, identity_reviewed_at = NULL, version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (now, subject_id, workspace_id, base_version),
            )
            updated = _current_subject(connection, workspace_id, subject_id)
            _write_subject_version(connection, updated, "restored_needs_review", identity["user"]["id"], now)
            relations = connection.execute(
                "SELECT * FROM business_relations WHERE workspace_id = ? AND subject_id = ? AND directory_state = 'active' AND review_status = 'supported'",
                (workspace_id, subject_id),
            ).fetchall()
            for relation in relations:
                connection.execute(
                    "UPDATE business_relations SET review_status = 'needs_review', review_evidence_json = '[]', review_note = '', reviewed_by = NULL, reviewed_at = NULL, version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                    (now, relation["id"], workspace_id, relation["version"]),
                )
                updated_relation = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation["id"], workspace_id)).fetchone()
                _write_relation_version(connection, updated_relation, "subject_restored_needs_review", identity["user"]["id"], now)
            return _success(_detail(connection, updated, identity))

    @app.post("/api/business/subjects/<subject_id>/evidence")
    def business_add_evidence(subject_id: str):
        identity = _require_write()
        data = _body()
        base_version = _base_version(data.get("base_version"))
        key = _required_idempotency_key()
        normalized = dict(data)
        normalized["base_version"] = base_version
        fingerprint = _request_fingerprint(normalized)
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            existing_id = _idempotent_resource(connection, workspace_id, "business.subject.evidence", key, fingerprint, "business_evidence")
            if existing_id:
                evidence = connection.execute("SELECT * FROM business_evidence WHERE id = ? AND workspace_id = ? AND subject_id = ?", (existing_id, workspace_id, subject_id)).fetchone()
                if evidence is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The saved evidence result is unavailable", 409)
                if evidence["work_id"] is not None:
                    _require_business_work(connection, identity, evidence["work_id"])
                return _success({"evidence": _evidence_data(evidence), "subject": _subject_data(_current_subject(connection, workspace_id, subject_id)), "replayed": True}, 200)
            row = _current_subject(connection, workspace_id, subject_id)
            if row is None:
                raise _not_found()
            _require_active_subject(row)
            _expect_version(row, base_version)
            evidence_id, evidence_work_id = _insert_evidence(connection, identity, subject_id, data, now)
            if evidence_work_id is None:
                connection.execute(
                    "UPDATE business_subjects SET version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                    (now, subject_id, workspace_id, base_version),
                )
                updated = _current_subject(connection, workspace_id, subject_id)
                _write_subject_version(connection, updated, "evidence_added", identity["user"]["id"], now)
            else:
                updated = _current_subject(connection, workspace_id, subject_id)
            evidence = connection.execute("SELECT * FROM business_evidence WHERE id = ? AND workspace_id = ?", (evidence_id, workspace_id)).fetchone()
            _record_idempotency(connection, workspace_id, "business.subject.evidence", key, fingerprint, "business_evidence", evidence_id, now)
            return _success({"evidence": _evidence_data(evidence), "subject": _subject_data(updated), "replayed": False}, 201)

    @app.get("/api/business/subjects/<subject_id>/relationships")
    def business_list_relationships(subject_id: str):
        workspace_id = g.identity["workspace"]["id"]
        work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            subject = _current_subject(connection, workspace_id, subject_id)
            if subject is None:
                raise _not_found()
            if work_id:
                _require_business_work(connection, g.identity, work_id)
            subject_ids = _subject_identity_group_ids(connection, subject)
            markers = ",".join("?" for _ in subject_ids)
            rows = connection.execute(
                f"SELECT * FROM business_relations WHERE workspace_id = ? "
                f"AND (subject_id IN ({markers}) OR related_subject_id IN ({markers})) ORDER BY created_at, id",
                (workspace_id, *subject_ids, *subject_ids),
            ).fetchall()
            return _success([
                _relation_data(
                    connection, row, workspace_id, viewer_subject_id=subject_id,
                    viewer_subject_ids=set(subject_ids),
                )
                for row in rows
                if _relation_visible(connection, g.identity, row, requested_work_id=work_id)
            ])

    @app.get("/api/business/relationships")
    def business_list_all_relationships():
        workspace_id = g.identity["workspace"]["id"]
        work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            if work_id:
                _require_business_work(connection, g.identity, work_id)
            rows = connection.execute(
                "SELECT * FROM business_relations WHERE workspace_id = ? ORDER BY created_at, id",
                (workspace_id,),
            ).fetchall()
            return _success([
                _relation_data(connection, row, workspace_id)
                for row in rows
                if _relation_visible(connection, g.identity, row, requested_work_id=work_id)
            ])

    @app.post("/api/business/subjects/<subject_id>/relationships")
    def business_create_relationship(subject_id: str):
        identity = _require_write()
        data = _body()
        base_subject_version = _base_version(data.get("base_subject_version"))
        key = _required_idempotency_key()
        fingerprint = _request_fingerprint(data)
        workspace_id = identity["workspace"]["id"]
        relation_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as connection:
            existing_id = _idempotent_resource(connection, workspace_id, "business.relation.create", key, fingerprint, "business_relation")
            if existing_id:
                relation = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ? AND subject_id = ?", (existing_id, workspace_id, subject_id)).fetchone()
                if relation is None:
                    raise ApiError("IDEMPOTENCY_RESULT_MISSING", "The relationship created by this request is unavailable", 409)
                scope = _relation_work_scope(connection, relation)
                if scope is False or not _relation_visible(connection, identity, relation, requested_work_id=scope):
                    raise _not_found()
                return _success({"relationship": _relation_data(connection, relation, workspace_id), "replayed": True}, 200)
            subject = _current_subject(connection, workspace_id, subject_id)
            if subject is None:
                raise _not_found()
            _require_active_subject(subject)
            _expect_version(subject, base_subject_version)
            relation = _valid_relation_payload(data, connection, identity, workspace_id, subject_id)
            values = relation["valid_time"]
            connection.execute(
                "INSERT INTO business_relations(id, workspace_id, work_id, subject_id, related_subject_id, related_subject_version, related_subject_name, related_object_kind, related_object_id, related_object_label, relation_type, details_json, directory_state, review_status, review_evidence_json, review_note, reviewed_by, reviewed_at, valid_time_kind, valid_from, valid_to, observed_at, version, created_at, updated_at, created_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    relation_id, workspace_id, relation["work_id"], subject_id, relation["related_subject_id"],
                    relation["related_subject_version"], relation["related_subject_name"],
                    relation["related_object_kind"], relation["related_object_id"],
                    relation["related_object_label"], relation["relation_type"],
                    _json(relation["details"]),
                    relation["review_status"], relation["review_evidence_json"], relation["review_note"],
                    identity["user"]["id"] if relation["review_status"] in {"supported", "conflicted", "rejected"} else None,
                    now if relation["review_status"] in {"supported", "conflicted", "rejected"} else None,
                    values["valid_time_kind"], values["valid_from"], values["valid_to"], values["observed_at"],
                    now, now, identity["user"]["id"],
                ),
            )
            row = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            _write_relation_version(connection, row, "created", identity["user"]["id"], now)
            _record_idempotency(connection, workspace_id, "business.relation.create", key, fingerprint, "business_relation", relation_id, now)
            return _success({"relationship": _relation_data(connection, row, workspace_id), "replayed": False}, 201)

    @app.get("/api/business/relationships/<relation_id>/versions")
    def business_relationship_versions(relation_id: str):
        workspace_id = g.identity["workspace"]["id"]
        requested_work_id = _optional_text(request.args.get("work_id"), "work_id", 200)
        with store.transaction(immediate=False) as connection:
            row = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            if row is None or not _relation_visible(connection, g.identity, row, requested_work_id=requested_work_id):
                raise _not_found()
            versions = connection.execute(
                "SELECT version, operation, snapshot_json, created_at, created_by FROM business_relation_versions WHERE relation_id = ? AND workspace_id = ? ORDER BY version",
                (relation_id, workspace_id),
            ).fetchall()
            return _success([{
                "version": item["version"], "operation": item["operation"],
                "snapshot": json.loads(item["snapshot_json"]),
                "created_at": item["created_at"], "created_by": item["created_by"],
            } for item in versions])

    @app.patch("/api/business/relationships/<relation_id>")
    def business_update_relationship(relation_id: str):
        identity = _require_write()
        data = _body()
        allowed = {
            "base_version", "base_subject_version", "source_subject_id", "relation_type",
            "related_subject_id", "related_object", "correction_reason", "work_id",
            "review_status", "evidence_ids", "review_note", "details",
            "valid_time_kind", "valid_from", "valid_to", "observed_at",
        }
        if set(data) - allowed:
            raise ApiError("INVALID_INPUT", "Relationship update contains an unsupported field", 400)
        base_version = _base_version(data.get("base_version"))
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            if row is None or not _relation_visible(
                connection, identity, row, requested_work_id=_relation_work_scope(connection, row), write=True,
            ):
                raise _not_found()
            current_scope = _relation_work_scope(connection, row)
            if current_scope is False:
                raise ApiError("RELATIONSHIP_SCOPE_CONFLICT", "This relationship references evidence from multiple private Works and needs a scoped correction", 409)
            if "work_id" in data:
                work_id = _string(data["work_id"], "work_id", maximum=200) if data["work_id"] is not None else None
            else:
                work_id = current_scope
            if current_scope is not None:
                _require_business_work(connection, identity, current_scope, write=True)
                if work_id is None:
                    raise ApiError("RELATIONSHIP_SCOPE_DECLASSIFICATION_REQUIRES_NEW_RECORD", "Keep the private relationship history in its Work; create a separate public relationship from public evidence", 409)
                from .collaboration import work_commission
                if work_id != current_scope and work_commission(connection, work_id) != work_commission(connection, current_scope):
                    raise ApiError("CLIENT_SCOPE_MISMATCH", "A relationship cannot move between different client scopes", 403)
            _require_business_work(connection, identity, work_id, write=True)
            scope_changed = work_id != current_scope
            source_subject_id = (
                _string(data["source_subject_id"], "source_subject_id", maximum=200)
                if data.get("source_subject_id") is not None else row["subject_id"]
            )
            subject = _current_subject(connection, workspace_id, source_subject_id)
            if subject is None:
                raise _not_found()
            _require_active_subject(subject)
            if row["directory_state"] != "active":
                raise ApiError("RELATIONSHIP_WITHDRAWN", "A withdrawn relationship cannot be revised", 409)
            _expect_version(row, base_version)
            correction_fields = {"source_subject_id", "relation_type", "related_subject_id", "related_object"}
            identity_correction = bool(correction_fields & set(data))
            if identity_correction:
                if "base_subject_version" not in data:
                    raise ApiError("INVALID_INPUT", "Identity correction needs the selected source Subject version", 400)
                _expect_version(subject, _base_version(data["base_subject_version"]))
                correction_reason = _string(data.get("correction_reason"), "correction_reason", maximum=2000)
            elif "base_subject_version" in data or "correction_reason" in data:
                raise ApiError("INVALID_INPUT", "Subject version and correction reason require an identity correction", 400)
            review_status = row["review_status"]
            evidence_json = row["review_evidence_json"]
            review_note = row["review_note"]
            reviewed_by = row["reviewed_by"]
            reviewed_at = row["reviewed_at"]
            current_details = json.loads(row["details_json"] or "{}")
            relation_type = row["relation_type"]
            target_values = {
                "related_subject_id": row["related_subject_id"],
                "related_object": None if row["related_subject_id"] else {
                    "kind": row["related_object_kind"],
                    "id": row["related_object_id"],
                    "label": row["related_object_label"],
                },
            }
            if "relation_type" in data:
                relation_type = data["relation_type"]
            if "related_subject_id" in data or "related_object" in data:
                if "related_subject_id" in data and "related_object" in data:
                    raise ApiError("INVALID_INPUT", "Provide exactly one replacement Subject or object target", 400)
                target_values = {
                    "related_subject_id": data.get("related_subject_id"),
                    "related_object": data.get("related_object"),
                }
            current_time = {
                "valid_time_kind": row["valid_time_kind"], "valid_from": row["valid_from"],
                "valid_to": row["valid_to"], "observed_at": row["observed_at"],
            }
            time_fields = {key: data[key] for key in ("valid_time_kind", "valid_from", "valid_to", "observed_at") if key in data}
            valid_time = _relation_time(time_fields, current_time) if time_fields else current_time
            if identity_correction:
                if relation_type != row["relation_type"] and "details" not in data:
                    details = {}
                elif "details" in data:
                    details = _relation_details(data["details"], relation_type)
                else:
                    details = current_details
                historical_source = current_details.get("historical_source")
                candidate_details = {key: value for key, value in details.items() if key != "historical_source"}
                correction_payload = {
                    "base_subject_version": subject["version"],
                    "relation_type": relation_type,
                    **target_values,
                    "details": candidate_details,
                    "work_id": work_id,
                    "review_status": data.get("review_status", "needs_review"),
                    "evidence_ids": data.get("evidence_ids", []),
                    "review_note": data.get("review_note") or correction_reason,
                    **valid_time,
                }
                normalized = _valid_relation_payload(
                    correction_payload, connection, identity, workspace_id, source_subject_id,
                )
                relation_type = normalized["relation_type"]
                target_values = {
                    "related_subject_id": normalized["related_subject_id"],
                    "related_object": {
                        "kind": normalized["related_object_kind"],
                        "id": normalized["related_object_id"],
                        "label": normalized["related_object_label"],
                    } if normalized["related_object_kind"] else None,
                }
                details = normalized["details"]
                if historical_source is not None:
                    details["historical_source"] = historical_source
                review_status = normalized["review_status"]
                evidence_json = normalized["review_evidence_json"]
                review_note = normalized["review_note"] or correction_reason
                reviewed_by = identity["user"]["id"] if review_status in {"supported", "conflicted", "rejected"} else None
                reviewed_at = now if reviewed_by else None
                valid_time = normalized["valid_time"]
            else:
                if "details" in data:
                    details = _relation_details(data["details"], row["relation_type"])
                    if "historical_source" in current_details:
                        details["historical_source"] = current_details["historical_source"]
                else:
                    details = current_details
                if "review_status" in data:
                    review_status = _review_state(data["review_status"], RELATION_REVIEW_STATES, "review_status")
                    review_note = _optional_text(data.get("review_note"), "review_note", 1000) or ""
                    if review_status == "supported":
                        china_operation = (
                            row["relation_type"] == "operates_in"
                            and row["related_object_kind"] == "market"
                            and row["related_object_id"] == "CN"
                        )
                        scopes = {"china_operation"} if china_operation else {"relationship"}
                        evidence_ids = _review_evidence_ids(connection, workspace_id, source_subject_id, data.get("evidence_ids"), scopes, work_id=work_id)
                        evidence_json = _json(evidence_ids)
                        reviewed_by, reviewed_at = identity["user"]["id"], now
                    elif review_status == "conflicted":
                        if not review_note:
                            raise ApiError("INVALID_INPUT", "A conflicted relationship needs a review_note", 400)
                        evidence_ids = data.get("evidence_ids", [])
                        evidence_json = _json(_review_evidence_ids(connection, workspace_id, source_subject_id, evidence_ids, EVIDENCE_SCOPES, work_id=work_id)) if evidence_ids else "[]"
                        reviewed_by, reviewed_at = identity["user"]["id"], now
                    else:
                        evidence_ids = data.get("evidence_ids", [] if _json(details) == row["details_json"] else json.loads(row["review_evidence_json"]))
                        evidence_json = _json(_review_evidence_ids(connection, workspace_id, source_subject_id, evidence_ids, EVIDENCE_SCOPES, work_id=work_id)) if evidence_ids else "[]"
                        if not isinstance(evidence_ids, list):
                            raise ApiError("INVALID_INPUT", "evidence_ids must be an array", 400)
                        reviewed_by, reviewed_at = None, None
                elif _json(details) != row["details_json"] or scope_changed:
                    review_status = "needs_review"
                    evidence_json = "[]"
                    review_note = "Relationship scope or details changed; the source and identity must be reviewed again."
                    reviewed_by, reviewed_at = None, None
                elif "evidence_ids" in data or "review_note" in data:
                    raise ApiError("INVALID_INPUT", "Review evidence and note require review_status", 400)
            details_changed = _json(details) != row["details_json"]
            identity_changed = (
                source_subject_id != row["subject_id"]
                or relation_type != row["relation_type"]
                or target_values["related_subject_id"] != row["related_subject_id"]
                or target_values["related_object"] != (
                    None if row["related_subject_id"] else {
                        "kind": row["related_object_kind"], "id": row["related_object_id"],
                        "label": row["related_object_label"],
                    }
                )
            )
            if not identity_correction and scope_changed:
                review_status = "needs_review"
                evidence_json = "[]"
                review_note = "Relationship scope or details changed; the source and identity must be reviewed again."
                reviewed_by, reviewed_at = None, None
            changed = (
                source_subject_id != row["subject_id"] or relation_type != row["relation_type"]
                or target_values["related_subject_id"] != row["related_subject_id"]
                or target_values["related_object"] != (None if row["related_subject_id"] else {
                    "kind": row["related_object_kind"], "id": row["related_object_id"], "label": row["related_object_label"],
                })
                or work_id != row["work_id"] or review_status != row["review_status"] or evidence_json != row["review_evidence_json"]
                or review_note != row["review_note"] or reviewed_by != row["reviewed_by"]
                or reviewed_at != row["reviewed_at"]
                or details_changed
                or any(valid_time[key] != row[key] for key in ("valid_time_kind", "valid_from", "valid_to", "observed_at"))
            )
            if not changed:
                return _success(_relation_data(connection, row, workspace_id))
            connection.execute(
                "UPDATE business_relations SET subject_id=?,work_id=?,related_subject_id=?,related_subject_version=?,related_subject_name=?,related_object_kind=?,related_object_id=?,related_object_label=?,relation_type=?,review_status=?,review_evidence_json=?,review_note=?,details_json=?,reviewed_by=?,reviewed_at=?,valid_time_kind=?,valid_from=?,valid_to=?,observed_at=?,version=version+1,updated_at=? WHERE id=? AND workspace_id=? AND version=?",
                (source_subject_id, work_id, target_values["related_subject_id"],
                 normalized["related_subject_version"] if identity_correction else row["related_subject_version"],
                 normalized["related_subject_name"] if identity_correction else row["related_subject_name"],
                 target_values["related_object"]["kind"] if target_values["related_object"] else None,
                 target_values["related_object"]["id"] if target_values["related_object"] else None,
                 target_values["related_object"].get("label") if target_values["related_object"] else None,
                 relation_type, review_status, evidence_json, review_note, _json(details), reviewed_by, reviewed_at,
                 valid_time["valid_time_kind"], valid_time["valid_from"], valid_time["valid_to"], valid_time["observed_at"],
                 now, relation_id, workspace_id, base_version),
            )
            updated = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            _write_relation_version(connection, updated, "corrected" if identity_changed else "updated", identity["user"]["id"], now)
            return _success(_relation_data(connection, updated, workspace_id))

    @app.delete("/api/business/relationships/<relation_id>")
    def business_withdraw_relationship(relation_id: str):
        identity = _require_write()
        data = _body()
        if set(data) != {"base_version"}:
            raise ApiError("INVALID_INPUT", "Relationship withdrawal requires only base_version", 400)
        base_version = _base_version(data["base_version"])
        workspace_id = identity["workspace"]["id"]
        now = utc_now()
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            if row is None:
                raise _not_found()
            subject = _current_subject(connection, workspace_id, row["subject_id"])
            if subject is None:
                raise _not_found()
            _require_active_subject(subject)
            if row["directory_state"] == "withdrawn":
                return _success(_relation_data(connection, row, workspace_id))
            _expect_version(row, base_version)
            connection.execute(
                "UPDATE business_relations SET directory_state = 'withdrawn', version = version + 1, updated_at = ? WHERE id = ? AND workspace_id = ? AND version = ?",
                (now, relation_id, workspace_id, base_version),
            )
            updated = connection.execute("SELECT * FROM business_relations WHERE id = ? AND workspace_id = ?", (relation_id, workspace_id)).fetchone()
            _write_relation_version(connection, updated, "withdrawn", identity["user"]["id"], now)
            return _success(_relation_data(connection, updated, workspace_id))
