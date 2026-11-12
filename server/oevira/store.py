"""SQLite persistence for the local product core."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('owner', 'editor', 'viewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS local_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    user_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bootstrap_capabilities (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user_workspace ON sessions(user_id, workspace_id, expires_at);
CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    brief_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS works_workspace_updated ON works(workspace_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS work_versions (
    work_id TEXT NOT NULL REFERENCES works(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    brief_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(work_id, version)
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    format TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    text TEXT,
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_url TEXT,
    etag TEXT,
    last_modified TEXT,
    source_json TEXT NOT NULL DEFAULT '{}',
    source_text TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS materials_workspace_created ON materials(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS materials_workspace_source ON materials(workspace_id, source_url, created_at DESC);
CREATE TABLE IF NOT EXISTS material_segments (
    material_id TEXT NOT NULL REFERENCES materials(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    id TEXT NOT NULL,
    label TEXT NOT NULL,
    text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY(material_id, id)
);
CREATE TABLE IF NOT EXISTS work_materials (
    work_id TEXT NOT NULL REFERENCES works(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(work_id, material_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    work_id TEXT REFERENCES works(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_workspace_updated ON artifacts(workspace_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    revision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    parent_version INTEGER,
    restore_version INTEGER,
    PRIMARY KEY(artifact_id, version)
);
CREATE TABLE IF NOT EXISTS work_artifacts (
    work_id TEXT NOT NULL REFERENCES works(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(work_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    material_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    progress_completed INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    progress_label TEXT NOT NULL DEFAULT '',
    artifact_id TEXT REFERENCES artifacts(id),
    error_code TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_runnable ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    version INTEGER NOT NULL,
    format TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, idempotency_key),
    FOREIGN KEY(artifact_id, version) REFERENCES artifact_versions(artifact_id, version)
);
CREATE INDEX IF NOT EXISTS exports_workspace_created ON exports(workspace_id, created_at DESC);
CREATE TABLE IF NOT EXISTS idempotency_records (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, operation, key)
);
PRAGMA user_version = 3;
"""


MIGRATE_1_TO_2 = """
BEGIN IMMEDIATE;
ALTER TABLE materials ADD COLUMN source_url TEXT;
ALTER TABLE materials ADD COLUMN etag TEXT;
ALTER TABLE materials ADD COLUMN last_modified TEXT;
ALTER TABLE materials ADD COLUMN source_json TEXT NOT NULL DEFAULT '{}';
DROP INDEX IF EXISTS jobs_runnable;
ALTER TABLE jobs RENAME TO jobs_v1;
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    material_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    progress_completed INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    progress_label TEXT NOT NULL DEFAULT '',
    artifact_id TEXT REFERENCES artifacts(id),
    error_code TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, kind, idempotency_key)
);
INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash,
                 idempotency_key, progress_completed, progress_total, progress_label, artifact_id,
                 error_code, error_message, cancel_requested, attempts, lease_generation, created_at, updated_at)
SELECT id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash,
       idempotency_key, progress_completed, progress_total, progress_label, artifact_id,
       error_code, error_message, cancel_requested, attempts, lease_generation, created_at, updated_at
FROM jobs_v1;
DROP TABLE jobs_v1;
CREATE INDEX jobs_runnable ON jobs(status, created_at);
PRAGMA user_version = 2;
COMMIT;
"""


MIGRATE_2_TO_3 = """
BEGIN IMMEDIATE;
ALTER TABLE materials ADD COLUMN source_text TEXT;
PRAGMA user_version = 3;
COMMIT;
"""


MIGRATE_4_TO_5 = """
BEGIN IMMEDIATE;
ALTER TABLE monitor_rules ADD COLUMN subject_id TEXT REFERENCES business_subjects(id);
CREATE INDEX monitor_rules_subject_status ON monitor_rules(workspace_id, subject_id, status);
PRAGMA user_version = 5;
COMMIT;
"""


class Store:
    def __init__(self, data_root: str | Path):
        requested = Path(data_root).expanduser()
        if requested.exists() and requested.is_symlink():
            raise ValueError("Data root cannot be a symbolic link")
        self.root = requested.resolve()
        self.database = self.root / "oevira.sqlite3"
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 5:
                raise RuntimeError("Data store schema is newer than this application")
            connection.execute("PRAGMA journal_mode = WAL")
            if version == 1:
                connection.executescript(MIGRATE_1_TO_2)
                version = 2
            if version == 2:
                connection.executescript(MIGRATE_2_TO_3)
                version = 3
            if version == 0 or version == 3:
                # SCHEMA is the v3 core schema. Re-running it is idempotent and
                # preserves existing rows; v4 installations must not reset the
                # version marker by executing this script again.
                connection.executescript(SCHEMA)
                version = 3
            if version == 3:
                # Import only after package initialization: monitoring depends on
                # API contracts which themselves depend on this Store module.
                from .monitoring import MONITORING_SCHEMA

                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + MONITORING_SCHEMA
                    + "\nPRAGMA user_version = 4;\nCOMMIT;"
                )
                version = 4
            if version == 4:
                connection.executescript(MIGRATE_4_TO_5)
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def row(self, sql: str, parameters: tuple = ()) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(sql, parameters).fetchone()
        finally:
            connection.close()

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()
