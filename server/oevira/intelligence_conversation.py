"""Store schema for product-owned Intelligence conversations and turn receipts."""
from __future__ import annotations


INTELLIGENCE_CONVERSATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_conversations (
    conversation_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    work_id TEXT REFERENCES works(id),
    media_project_id TEXT,
    material_ids_json TEXT NOT NULL DEFAULT '[]',
    material_scope_mode TEXT NOT NULL CHECK(material_scope_mode IN ('none', 'selected', 'all')),
    title TEXT NOT NULL,
    selection_id TEXT,
    selection_revision TEXT,
    selection_route TEXT,
    selection_protocol TEXT,
    selection_model_id TEXT,
    skill_snapshots_json TEXT NOT NULL DEFAULT '[]',
    codex_thread_id TEXT,
    tool_catalog_sha256 TEXT NOT NULL,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    citations_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    version INTEGER NOT NULL CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    CHECK((selection_id IS NULL AND selection_revision IS NULL AND selection_route IS NULL
           AND selection_protocol IS NULL AND selection_model_id IS NULL)
       OR (selection_id IS NOT NULL AND selection_revision IS NOT NULL AND selection_route IS NOT NULL
           AND selection_protocol IS NOT NULL AND selection_model_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS intelligence_conversations_owner_scope
    ON intelligence_conversations(workspace_id, owner_user_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS intelligence_turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES intelligence_conversations(conversation_id),
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    request_json TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    assistant_text TEXT,
    citations_json TEXT NOT NULL DEFAULT '[]',
    tool_results_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'partial', 'failed', 'interrupted', 'cancelled')),
    provider TEXT,
    model_id TEXT,
    model_revision TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS intelligence_turns_conversation_order
    ON intelligence_turns(conversation_id, sequence);
"""


def install_intelligence_conversation_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + INTELLIGENCE_CONVERSATION_SCHEMA + "\nCOMMIT;")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(intelligence_conversations)")}
        if "skill_snapshots_json" not in columns:
            connection.execute(
                "ALTER TABLE intelligence_conversations ADD COLUMN skill_snapshots_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "media_project_id" not in columns:
            connection.execute(
                "ALTER TABLE intelligence_conversations ADD COLUMN media_project_id TEXT"
            )
    finally:
        connection.close()
