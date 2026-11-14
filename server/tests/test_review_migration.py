from __future__ import annotations

import json
import sqlite3
import unittest
import uuid

from server.oevira import create_app
from server.oevira.auth import utc_now
from server.oevira.collaboration import _upgrade_review_targets, install_collaboration_schema
from server.tests import test_core as core_test


LEGACY_SCHEMA = """
CREATE TABLE artifact_comments (
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 artifact_version INTEGER NOT NULL, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 author_id TEXT NOT NULL REFERENCES users(id), anchor_json TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','resolved','withdrawn')), resolution_json TEXT,
 version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)
"""


class ReviewMigrationTests(unittest.TestCase):
    setUp = core_test.CoreApiTest.setUp
    tearDown = core_test.CoreApiTest.tearDown
    request = core_test.CoreApiTest.request

    def legacy(self):
        artifact = self.request("post", "/api/artifacts", json={"title": "旧评论目标", "body": "Original value.", "citations": []}).json["data"]
        connection = self.store.connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE artifact_comments")
            connection.execute(LEGACY_SCHEMA)
            now = utc_now()
            for ordinal in (1, 2):
                comment_id = f"preserved-comment-{ordinal}"
                body = f"旧意见 {ordinal}：不要丢弃人工内容。"
                connection.execute("INSERT INTO artifact_comments VALUES(?,?,?,?,?,?,?,'open',NULL,1,?,?)", (comment_id, artifact["id"], 1, self.identity["workspace"]["id"], self.identity["user"]["id"], '{"start":0, "end":8, "quote":"Original"}', body, now, now))
                connection.execute("INSERT INTO artifact_comment_versions VALUES(?,1,?,?,?)", (comment_id, json.dumps({"id": comment_id, "body": body, "keep": ["原样", 1]}, ensure_ascii=False), self.identity["user"]["id"], now))
            connection.execute("CREATE TABLE review_migration_audit(comment_id TEXT, prior_body TEXT, next_body TEXT)")
            connection.execute("CREATE INDEX comments_custom_lookup ON artifact_comments(workspace_id,created_at)")
            connection.execute("CREATE TRIGGER comments_custom_audit AFTER UPDATE OF body ON artifact_comments BEGIN INSERT INTO review_migration_audit VALUES(NEW.id,OLD.body,NEW.body); END")
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
            original = [tuple(row) for row in connection.execute("SELECT * FROM artifact_comments ORDER BY id")]
            history = [tuple(row) for row in connection.execute("SELECT * FROM artifact_comment_versions ORDER BY comment_id,version")]
            dependents = [tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('comments_custom_lookup','comments_custom_audit') ORDER BY name")]
            return artifact, original, history, dependents
        finally:
            connection.close()

    def test_old_comments_history_and_custom_schema_survive_cold_upgrade(self):
        artifact, original, history, dependents = self.legacy()
        install_collaboration_schema(self.store)
        install_collaboration_schema(self.store)
        with self.store.transaction(immediate=False) as connection:
            current = [tuple(row)[:12] for row in connection.execute("SELECT * FROM artifact_comments ORDER BY id")]
            self.assertEqual(current, original)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM artifact_comment_versions ORDER BY comment_id,version")], history)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('comments_custom_lookup','comments_custom_audit') ORDER BY name")], dependents)
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_migration_audit").fetchone()[0], 0)
        reopened = create_app(self.root, expected_port=8317)
        response = reopened.test_client().get(f"/api/artifacts/{artifact['id']}/comments", base_url=core_test.BASE_URL, headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["body"] for item in response.json["data"]], [row[6] for row in original])

    def test_failure_after_old_table_drop_rolls_back_rows_history_and_indexes(self):
        _, original, history, dependents = self.legacy()

        class InterruptedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("ALTER TABLE artifact_comments__target_migration"):
                    raise RuntimeError("Injected migration interruption before rename")
                return super().execute(sql, parameters)

        connection = sqlite3.connect(self.store.database, isolation_level=None, factory=InterruptedConnection)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(RuntimeError, "Injected migration"):
                _upgrade_review_targets(connection)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertFalse(connection.in_transaction)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM artifact_comments ORDER BY id")], original)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM artifact_comment_versions ORDER BY comment_id,version")], history)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('comments_custom_lookup','comments_custom_audit') ORDER BY name")], dependents)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='artifact_comments__target_migration'").fetchone())
        finally:
            connection.close()
        install_collaboration_schema(self.store)

    def test_media_and_artifact_targets_are_mutually_exclusive(self):
        artifact, *_ = self.legacy()
        install_collaboration_schema(self.store)
        project = self.request("post", "/api/media/projects", json={"title": "原生媒体评论", "kind": "podcast"}, headers={"Idempotency-Key": "review-project"})
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO artifact_comments(id,workspace_id,author_id,anchor_json,body,state,version,created_at,updated_at,media_project_id,media_project_version) VALUES(?,?,?,?,?,'open',1,?,?,?,1)", (str(uuid.uuid4()), self.identity["workspace"]["id"], self.identity["user"]["id"], '{}', "媒体原意见", now, now, project.json["data"]["id"]))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO artifact_comments(id,artifact_id,artifact_version,workspace_id,author_id,anchor_json,body,state,version,created_at,updated_at,media_project_id,media_project_version) VALUES(?,?,1,?,?,?,'invalid','open',1,?,?,?,1)", (str(uuid.uuid4()), artifact["id"], self.identity["workspace"]["id"], self.identity["user"]["id"], '{}', now, now, project.json["data"]["id"]))


if __name__ == "__main__":
    unittest.main()
