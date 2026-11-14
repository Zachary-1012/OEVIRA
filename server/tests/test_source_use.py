from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.oevira import create_app
from server.oevira.auth import issue_local_capability
from server.oevira.source_use import (
    SourceUseError,
    authorize_monitor_source_refresh,
    intersect_model_or_cpu_admission,
    prepare_restored_source_use_decision,
    record_source_use_decision,
    resolve_source_use,
    revoke_source_use_decision,
)


ORIGIN = "http://127.0.0.1:8348"


class SourceUseOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-source-use-", dir=r"D:\寰意\.runtime")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8348)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.store = self.app.extensions["oevira.store"]
        self.capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, self.capability_file)
        response = self.client.post(
            "/api/local-session",
            json={"capability": self.capability_file.read_text(encoding="ascii"),
                  "display_name": "SourceUse Tester"},
            base_url=ORIGIN, headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.json["data"]
        self.token = data["token"]
        self.user_id = data["user"]["id"]
        self.workspace_id = data["workspace"]["id"]
        self.session_id = data["session"]["id"] if "session" in data and isinstance(data["session"], dict) else data.get("session_id")
        if not self.session_id:
            # Fall back to the authenticated identity stored with the session cookie/token.
            with self.store.transaction(immediate=False) as connection:
                row = connection.execute(
                    "SELECT id FROM sessions WHERE workspace_id=? AND user_id=? AND revoked_at IS NULL "
                    "ORDER BY created_at DESC",
                    (self.workspace_id, self.user_id),
                ).fetchone()
                self.session_id = row["id"]

    def tearDown(self):
        self.temporary.cleanup()

    def identity(self):
        return {
            "user": {"id": self.user_id},
            "workspace": {"id": self.workspace_id},
            "session_id": self.session_id,
        }

    def headers(self, key=None):
        value = {"Authorization": f"Bearer {self.token}", "Origin": ORIGIN}
        if key:
            value["Idempotency-Key"] = key
        return value

    def request(self, method, path, **kwargs):
        kwargs.setdefault("base_url", ORIGIN)
        kwargs.setdefault("headers", self.headers())
        return getattr(self.client, method)(path, **kwargs)

    def create_material(self, title, text, source_json=None):
        response = self.request("post", "/api/materials", json={"title": title, "text": text})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        material = response.json["data"]
        if source_json is not None:
            with self.store.transaction() as connection:
                existing = connection.execute(
                    "SELECT source_json FROM materials WHERE id=?", (material["id"],)
                ).fetchone()
                current = json.loads(existing["source_json"] or "{}") if existing else {}
                current.update(source_json)
                connection.execute(
                    "UPDATE materials SET source_json=? WHERE id=?",
                    (json.dumps(current, ensure_ascii=False), material["id"]),
                )
                material = dict(material)
                material["source_json"] = current
        return material

    def local_recipient(self):
        return {"kind": "local", "id": self.workspace_id}

    def test_public_resolver_allows_local_material_without_recorded_decision(self):
        material = self.create_material("Local note", "The launch date is recorded here.")
        with self.store.transaction(immediate=False) as connection:
            decision = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, "not_recorded_local")
        self.assertEqual(decision.reason_code, "SOURCE_USE_NOT_REQUIRED_FOR_LOCAL_PROCESSING")
        self.assertIsNone(decision.decision_id)

    def test_explicit_unresolved_declaration_is_blocked(self):
        material = self.create_material(
            "Rights-held note", "Secret date.",
            source_json={"source_use_decision_id": "unresolved"},
        )
        with self.store.transaction(immediate=False) as connection:
            decision = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "SOURCE_USE_DECLARATION_UNAVAILABLE")

    def test_record_resolve_and_revoke_round_trip(self):
        material = self.create_material("Approved source", "The product launch is confirmed.")
        with self.store.transaction() as connection:
            recorded = record_source_use_decision(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(), basis_status="user_declared",
                expires_at="2099-01-01T00:00:00Z",
            )
            self.assertTrue(recorded.allowed)
            self.assertEqual(recorded.state, "allowed")
            self.assertIsNotNone(recorded.decision_id)
            revoked = revoke_source_use_decision(connection, self.identity(), recorded.decision_id)
            self.assertFalse(revoked.allowed)
            self.assertEqual(revoked.reason_code, "SOURCE_USE_REVOKED")
            reread = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
        self.assertFalse(reread.allowed)
        self.assertEqual(reread.reason_code, "SOURCE_USE_REVOKED")
        self.assertEqual(reread.decision_id, recorded.decision_id)

    def test_derived_material_cannot_exceed_revoked_parent(self):
        parent = self.create_material("Parent capture", "Original paragraph.")
        child = self.create_material(
            "Derived excerpt", "Original paragraph, annotated.",
            source_json={
                "source_use_source_ref": {
                    "kind": "material", "id": parent["id"], "revision": parent["sha256"],
                }
            },
        )
        with self.store.transaction() as connection:
            parent_decision = record_source_use_decision(
                connection, self.identity(),
                {"kind": "material", "id": parent["id"], "revision": parent["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(), basis_status="user_declared",
                expires_at="2099-01-01T00:00:00Z",
            )
            child_before = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": child["id"], "revision": child["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
            self.assertTrue(child_before.allowed)
            self.assertIn(parent_decision.decision_id, child_before.lineage_decision_ids)
            revoke_source_use_decision(connection, self.identity(), parent_decision.decision_id)
            child_after = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": child["id"], "revision": child["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
        self.assertFalse(child_after.allowed)
        self.assertEqual(child_after.reason_code, "SOURCE_USE_REVOKED")
        self.assertIn(parent_decision.decision_id, child_after.lineage_decision_ids)

    def test_lineage_cycle_is_denied(self):
        first = self.create_material("First", "A")
        second = self.create_material(
            "Second", "B",
            source_json={"source_use_source_ref": {
                "kind": "material", "id": first["id"], "revision": first["sha256"],
            }},
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET source_json=? WHERE id=?",
                (json.dumps({
                    "source_use_source_ref": {
                        "kind": "material", "id": second["id"], "revision": second["sha256"],
                    }
                }), first["id"]),
            )
            decision = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": first["id"], "revision": first["sha256"]},
                field_paths=["text"], purpose="knowledge_retrieval", action="search",
                recipient=self.local_recipient(),
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "SOURCE_USE_LINEAGE_CYCLE")

    def test_training_grant_requires_verified_contract_basis(self):
        material = self.create_material("Private notes", "Do not train.")
        with self.store.transaction() as connection:
            with self.assertRaises(SourceUseError) as raised:
                record_source_use_decision(
                    connection, self.identity(),
                    {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                    field_paths=["text"], purpose="model_training", action="train",
                    recipient={"kind": "provider", "id": "example-model"},
                    basis_status="user_declared", allow_training=True,
                )
            self.assertEqual(raised.exception.code, "SOURCE_USE_VERIFIED_BASIS_REQUIRED")
            local = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="model_training", action="train",
                recipient={"kind": "provider", "id": "example-model"},
            )
        self.assertFalse(local.allowed)
        self.assertEqual(local.reason_code, "SOURCE_USE_DECISION_REQUIRED")

    def test_model_admission_intersection_does_not_override_denial(self):
        material = self.create_material("Notes", "Body")
        with self.store.transaction(immediate=False) as connection:
            denied = resolve_source_use(
                connection, self.identity(),
                {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                field_paths=["text"], purpose="model_training", action="train",
                recipient={"kind": "provider", "id": "example-model"},
            )
        admitted = intersect_model_or_cpu_admission(denied, {
            "status": "admitted", "allow_training": True, "allowed_purposes": ["model_training"],
        })
        self.assertFalse(admitted.allowed)
        self.assertEqual(admitted.reason_code, "SOURCE_USE_DECISION_REQUIRED")

    def test_restored_allowed_grant_requires_reauthorization(self):
        restored = prepare_restored_source_use_decision(
            {"decision_id": "d1", "decision_status": "allowed", "version": 3,
             "updated_at": "2026-01-01T00:00:00Z"},
            now="2026-11-12T00:00:00Z",
        )
        self.assertEqual(restored["decision_status"], "requires_reauthorization")
        self.assertEqual(restored["version"], 4)
        self.assertEqual(restored["updated_at"], "2026-11-12T00:00:00Z")
        revoked = prepare_restored_source_use_decision(
            {"decision_id": "d2", "decision_status": "revoked", "version": 2,
             "updated_at": "2026-01-01T00:00:00Z"},
            now="2026-11-12T00:00:00Z",
        )
        self.assertEqual(revoked["decision_status"], "revoked")
        self.assertEqual(revoked["version"], 2)

    def test_http_record_list_resolve_and_revoke(self):
        material = self.create_material("HTTP source", "Confirmed launch.")
        recorded = self.request(
            "post", "/api/source-use/decisions",
            json={
                "source_ref": {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                "field_paths": ["text"], "purpose": "knowledge_retrieval", "action": "search",
                "recipient": self.local_recipient(), "basis_status": "user_declared",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            headers=self.headers("source-use-record"),
        )
        self.assertEqual(recorded.status_code, 201, recorded.get_data(as_text=True))
        decision_id = recorded.json["data"]["decision_id"]
        self.assertTrue(recorded.json["data"]["allowed"])
        fetched = self.request("get", f"/api/source-use/decisions/{decision_id}")
        self.assertEqual(fetched.status_code, 200, fetched.get_data(as_text=True))
        self.assertEqual(fetched.json["data"]["decision_id"], decision_id)
        listed = self.request("get", f"/api/source-use/decisions?object_id={material['id']}")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(listed.json["data"]["decisions"][0]["decision_id"], decision_id)
        resolved = self.request(
            "post", "/api/source-use/resolve",
            json={
                "source_ref": {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                "field_paths": ["text"], "purpose": "knowledge_retrieval", "action": "search",
                "recipient": self.local_recipient(),
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertTrue(resolved.json["data"]["allowed"])
        revoked = self.request("post", f"/api/source-use/decisions/{decision_id}/revoke")
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        self.assertFalse(revoked.json["data"]["allowed"])
        self.assertEqual(revoked.json["data"]["reason_code"], "SOURCE_USE_REVOKED")
        after = self.request(
            "post", "/api/source-use/resolve",
            json={
                "source_ref": {"kind": "material", "id": material["id"], "revision": material["sha256"]},
                "field_paths": ["text"], "purpose": "knowledge_retrieval", "action": "search",
                "recipient": self.local_recipient(),
            },
        )
        self.assertEqual(after.status_code, 200, after.get_data(as_text=True))
        self.assertFalse(after.json["data"]["allowed"])
        self.assertEqual(after.json["data"]["reason_code"], "SOURCE_USE_REVOKED")

    def test_monitor_source_refresh_records_owner_declaration_then_resolves(self):
        now = "2026-11-12T06:00:00Z"
        monitor_id = "monitor-1"
        source_id = "monitor-source-1"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO monitor_rules(id,workspace_id,owner_user_id,title,scope,work_id,"
                "follow_source_links,keywords_json,exclude_keywords_json,frequency_seconds,"
                "stop_at,per_run_limit,status,version,next_run_at,created_at,updated_at) "
                "VALUES(?,?,?,?, '', NULL, 0, '[]', '[]', 3600, NULL, 5, 'enabled', 1, NULL, ?, ?)",
                (monitor_id, self.workspace_id, self.user_id, "Example watch", now, now),
            )
            connection.execute(
                "INSERT INTO monitor_sources(id,monitor_id,workspace_id,url,label,source_origin,"
                "discovery_suppressed,position,active,status,updated_at) "
                "VALUES(?,?,?,?, '示例来源', 'user', 0, 0, 1, 'not_checked', ?)",
                (source_id, monitor_id, self.workspace_id, "https://example.invalid/feed", now),
            )
            decision = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=now,
            )
            self.assertTrue(decision.allowed, decision.reason_code)
            self.assertEqual(decision.object_kind, "monitor_source")
            self.assertEqual(decision.purpose, "monitoring")
            self.assertEqual(decision.action, "refresh")
            self.assertEqual(decision.basis_status, "user_declared")
            self.assertIsNotNone(decision.decision_id)
            again = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=now,
            )
            self.assertTrue(again.allowed)
            self.assertEqual(again.decision_id, decision.decision_id)
            revoke_source_use_decision(connection, self.identity(), decision.decision_id)
            blocked = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=now,
            )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason_code, "SOURCE_USE_REVOKED")

    def test_monitor_source_revision_survives_operational_updates_and_keeps_revocation(self):
        now = "2026-11-12T06:00:00Z"
        later = "2026-11-12T07:00:00Z"
        after = "2026-11-12T08:00:00Z"
        moved = "2026-11-12T09:00:00Z"
        monitor_id = "monitor-revision-stable"
        source_id = "monitor-source-revision-stable"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO monitor_rules(id,workspace_id,owner_user_id,title,scope,work_id,"
                "follow_source_links,keywords_json,exclude_keywords_json,frequency_seconds,"
                "stop_at,per_run_limit,status,version,next_run_at,created_at,updated_at) "
                "VALUES(?,?,?,?, '', NULL, 0, '[]', '[]', 3600, NULL, 5, 'enabled', 1, NULL, ?, ?)",
                (monitor_id, self.workspace_id, self.user_id, "Stable watch", now, now),
            )
            connection.execute(
                "INSERT INTO monitor_sources(id,monitor_id,workspace_id,url,label,source_origin,"
                "discovery_suppressed,position,active,status,updated_at) "
                "VALUES(?,?,?,?, '示例来源', 'user', 0, 0, 1, 'not_checked', ?)",
                (source_id, monitor_id, self.workspace_id, "https://example.invalid/stable-feed", now),
            )
            first = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=now,
            )
            self.assertTrue(first.allowed, first.reason_code)
            revision = first.object_revision
            connection.execute(
                "UPDATE monitor_sources SET pending_job_id=?, last_check_at=?, status='checking', updated_at=? WHERE id=?",
                ("job-operational", later, later, source_id),
            )
            again = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=later,
            )
            self.assertTrue(again.allowed, again.reason_code)
            self.assertEqual(again.decision_id, first.decision_id)
            self.assertEqual(again.object_revision, revision)
            revoke_source_use_decision(connection, self.identity(), first.decision_id, now=later)
            connection.execute(
                "UPDATE monitor_sources SET pending_job_id=NULL, last_success_at=?, status='ready', updated_at=? WHERE id=?",
                (after, after, source_id),
            )
            blocked = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=after,
            )
            self.assertFalse(blocked.allowed)
            self.assertEqual(blocked.reason_code, "SOURCE_USE_REVOKED")
            connection.execute(
                "UPDATE monitor_sources SET url=?, updated_at=? WHERE id=?",
                ("https://example.invalid/moved-feed", moved, source_id),
            )
            still_blocked = authorize_monitor_source_refresh(
                connection, self.identity(),
                {"kind": "monitor_source", "id": source_id},
                now=moved,
            )
            self.assertFalse(still_blocked.allowed)
            self.assertEqual(still_blocked.reason_code, "SOURCE_USE_REVOKED")

    def test_service_is_registered_on_the_app(self):
        service = self.app.extensions.get("oevira.source_use")
        self.assertIsNotNone(service)
        decision = service.resolve(
            self.identity(),
            {"kind": "url", "url": "https://example.invalid/public"},
            field_paths=["content"], purpose="knowledge_retrieval", action="search",
            recipient=self.local_recipient(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.object_kind, "url")
        self.assertEqual(decision.state, "not_recorded_local")


if __name__ == "__main__":
    unittest.main()
