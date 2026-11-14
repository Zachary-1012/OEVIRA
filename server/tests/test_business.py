from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import unittest
import uuid

from oevira import create_app
from oevira.auth import _digest, issue_local_capability, utc_now
from oevira.business import install_business_schema, register_business_routes


BASE_URL = "http://127.0.0.1:8317"


class BusinessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-business-")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        install_business_schema(self.store)
        if "business_list_subjects" not in self.app.view_functions:
            register_business_routes(self.app)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "主体档案测试用户"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def idempotency_headers(self, key: str) -> dict[str, str]:
        return {**self.auth_headers(), "Idempotency-Key": key}

    def create_subject(self, *, kind: str = "brand", name: str = "青屿品牌", key: str = "subject-001") -> dict:
        response = self.request(
            "post", "/api/business/subjects", json={"kind": kind, "name": name},
            headers={**self.auth_headers(), "Idempotency-Key": key},
        )
        self.assertIn(response.status_code, {200, 201}, response.get_data(as_text=True))
        return response.json["data"]["subject"]

    def add_material(self, *, workspace_id: str | None = None, excerpt: str = "深圳区域经营记录") -> str:
        material_id = str(uuid.uuid4())
        now = utc_now()
        content = f"虚构材料：青屿品牌在{excerpt}。"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        owner_id = self.identity["user"]["id"]
        if workspace_id:
            owner = self.store.row(
                "SELECT user_id FROM memberships WHERE workspace_id = ? AND role = 'owner' ORDER BY created_at LIMIT 1",
                (workspace_id,),
            )
            owner_id = owner["user_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO materials(id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason, text, sha256, relative_path, size_bytes, source_url, etag, last_modified, source_json, source_text, created_at) VALUES(?, ?, ?, NULL, ?, ?, 'txt', 'ready', NULL, ?, ?, ?, ?, ?, NULL, NULL, '{}', NULL, ?)",
                (
                    material_id, workspace_id or self.identity["workspace"]["id"],
                    owner_id, "隔离测试材料", "evidence.txt", content,
                    digest, f"objects/{material_id}.txt", len(content.encode("utf-8")),
                    "https://example.invalid/record", now,
                ),
            )
        return material_id

    def add_evidence(self, subject: dict, *, scope: str, material_id: str, excerpt: str, key: str) -> dict:
        response = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"],
                "scope": scope,
                "source": {"kind": "material", "material_id": material_id},
                "excerpt": excerpt,
                "location": "text:1",
                "observed_at": "2025-06-01T10:30:00+08:00",
            },
            headers={**self.auth_headers(), "Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]["evidence"]

    def create_other_workspace(self, *, role: str = "owner") -> tuple[str, str]:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "隔离用户", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "隔离工作区", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, ?, 1, ?)",
                (workspace_id, user_id, role, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now, expiry),
            )
        return token, workspace_id

    def create_member(self, *, role: str) -> str:
        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, f"{role}测试用户", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, ?, 1, ?)",
                (self.identity["workspace"]["id"], user_id, role, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, self.identity["workspace"]["id"], now, expiry),
            )
        return token

    def test_subject_evidence_relation_and_remove_readd_preserve_versions(self) -> None:
        created = self.request(
            "post", "/api/business/subjects",
            json={"kind": "brand", "name": "青屿品牌", "profile": {"aliases": ["青屿"]}},
            headers={**self.auth_headers(), "Idempotency-Key": "subject-create-001"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        subject = created.json["data"]["subject"]
        self.assertEqual(subject["kind"], "brand")
        self.assertEqual(subject["identity_review"]["status"], "needs_review")
        replay = self.request(
            "post", "/api/business/subjects",
            json={"kind": "brand", "name": "青屿品牌", "profile": {"aliases": ["青屿"]}},
            headers={**self.auth_headers(), "Idempotency-Key": "subject-create-001"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["data"]["subject"]["id"], subject["id"])
        self.assertTrue(replay.json["data"]["replayed"])

        material_id = self.add_material()
        identity_evidence = self.add_evidence(
            subject, scope="identity", material_id=material_id,
            excerpt="青屿品牌在深圳区域经营记录", key="subject-identity-evidence-001",
        )
        after_evidence = {**subject, "version": subject["version"] + 1}
        reviewed = self.request(
            "patch", f"/api/business/subjects/{subject['id']}",
            json={
                "base_version": after_evidence["version"],
                "identity_review_status": "supported",
                "review_evidence_ids": [identity_evidence["id"]],
                "review_note": "隔离测试中的人工核阅",
            },
            headers=self.auth_headers(),
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.get_data(as_text=True))
        subject = reviewed.json["data"]
        self.assertEqual(subject["identity_review"]["status"], "supported")
        self.assertEqual(subject["version"], 3)

        china_evidence = self.add_evidence(
            subject, scope="china_operation", material_id=material_id,
            excerpt="深圳区域经营记录", key="subject-china-evidence-001",
        )
        subject = {**subject, "version": subject["version"] + 1}
        relation_response = self.request(
            "post", f"/api/business/subjects/{subject['id']}/relationships",
            json={
                "base_subject_version": subject["version"],
                "relation_type": "operates_in",
                "related_object": {"kind": "market", "id": "cn", "label": "中国境内经营市场"},
                "review_status": "supported",
                "evidence_ids": [china_evidence["id"]],
                "valid_time_kind": "interval",
                "valid_from": "2024-01-01",
                "valid_to": "2025-12-31",
                "observed_at": "2025-06-01T02:30:00Z",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "subject-china-relation-001"},
        )
        self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
        relation = relation_response.json["data"]["relationship"]
        self.assertEqual(relation["target"]["id"], "CN")
        self.assertEqual(relation["review"]["status"], "supported")
        self.assertEqual(relation["valid_time"]["valid_to"], "2025-12-31")

        removed = self.request(
            "delete", f"/api/business/subjects/{subject['id']}",
            json={"base_version": subject["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        self.assertEqual(removed.json["data"]["directory_state"], "removed")
        active_list = self.request("get", "/api/business/subjects", headers=self.auth_headers())
        self.assertEqual(active_list.status_code, 200)
        self.assertNotIn(subject["id"], [item["id"] for item in active_list.json["data"]])
        historical = self.request("get", f"/api/business/subjects/{subject['id']}", headers=self.auth_headers())
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(len(historical.json["data"]["evidence"]), 2)
        self.assertEqual(len(historical.json["data"]["relationships"]), 1)

        restored = self.request(
            "post", f"/api/business/subjects/{subject['id']}/restore",
            json={"base_version": removed.json["data"]["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(restored.json["data"]["directory_state"], "active")
        self.assertEqual(restored.json["data"]["identity_review"]["status"], "needs_review")
        restored_relation = restored.json["data"]["relationships"][0]
        self.assertEqual(restored_relation["review"]["status"], "needs_review")
        self.assertEqual(restored_relation["valid_time"]["valid_from"], "2024-01-01")
        self.assertEqual(restored_relation["valid_time"]["valid_to"], "2025-12-31")

        subject_versions = self.request("get", f"/api/business/subjects/{subject['id']}/versions", headers=self.auth_headers())
        self.assertEqual(subject_versions.status_code, 200)
        self.assertEqual(subject_versions.json["data"][-2]["operation"], "removed")
        self.assertEqual(subject_versions.json["data"][-1]["operation"], "restored_needs_review")
        relation_versions = self.request(
            "get", f"/api/business/relationships/{relation['id']}/versions", headers=self.auth_headers(),
        )
        self.assertEqual(relation_versions.status_code, 200)
        self.assertEqual(len(relation_versions.json["data"]), 2)
        self.assertEqual(relation_versions.json["data"][0]["snapshot"]["review_status"], "supported")
        self.assertEqual(relation_versions.json["data"][1]["snapshot"]["review_status"], "needs_review")

    def test_same_name_is_only_a_candidate_and_updates_use_compare_and_swap(self) -> None:
        first = self.create_subject(key="candidate-one")
        second_response = self.request(
            "post", "/api/business/subjects", json={"kind": "brand", "name": "青屿品牌"},
            headers={**self.auth_headers(), "Idempotency-Key": "candidate-two"},
        )
        self.assertEqual(second_response.status_code, 201)
        second = second_response.json["data"]["subject"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second_response.json["data"]["possible_matches"][0]["id"], first["id"])

        updated = self.request(
            "patch", f"/api/business/subjects/{first['id']}",
            json={"base_version": first["version"], "kind": "company", "name": "青屿商业主体"},
            headers=self.auth_headers(),
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.json["data"]["kind"], "company")
        self.assertEqual(updated.json["data"]["identity_review"]["status"], "needs_review")
        stale = self.request(
            "patch", f"/api/business/subjects/{first['id']}",
            json={"base_version": first["version"], "profile": {"industry": "零售"}},
            headers=self.auth_headers(),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "VERSION_CONFLICT")

        inferred_entry = self.request(
            "post", "/api/business/subjects", json={"name": "未分类经营对象"},
            headers={**self.auth_headers(), "Idempotency-Key": "unclassified-subject"},
        )
        self.assertEqual(inferred_entry.status_code, 201, inferred_entry.get_data(as_text=True))
        self.assertEqual(inferred_entry.json["data"]["subject"]["kind"], "unclassified")
        actual_object_type = self.request(
            "post", "/api/business/subjects", json={"kind": "merchant", "name": "青屿商业商户"},
            headers={**self.auth_headers(), "Idempotency-Key": "merchant-subject"},
        )
        self.assertEqual(actual_object_type.status_code, 201, actual_object_type.get_data(as_text=True))
        self.assertEqual(actual_object_type.json["data"]["subject"]["kind"], "merchant")
        invalid_kind = self.request(
            "post", "/api/business/subjects", json={"kind": "unmodeled_type", "name": "不支持的类型"},
            headers={**self.auth_headers(), "Idempotency-Key": "invalid-subject-type"},
        )
        self.assertEqual(invalid_kind.status_code, 400)
        self.assertEqual(invalid_kind.json["error"]["code"], "INVALID_INPUT")

    def test_workspace_material_evidence_is_checked_and_external_references_are_not_fetched(self) -> None:
        subject = self.create_subject(key="evidence-scope-subject")
        _foreign_token, foreign_workspace = self.create_other_workspace()
        foreign_material = self.add_material(workspace_id=foreign_workspace)
        denied = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"], "scope": "identity",
                "source": {"kind": "material", "material_id": foreign_material},
                "excerpt": "深圳区域经营记录",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "foreign-material"},
        )
        self.assertEqual(denied.status_code, 404)

        malformed = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"], "scope": "identity",
                "source": {"kind": "public_reference", "url": "https://example.invalid/registry", "title": "未抓取网页"},
                "excerpt": "用户提供的摘录",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "public-reference-no-fetch"},
        )
        self.assertEqual(malformed.status_code, 201, malformed.get_data(as_text=True))
        evidence = malformed.json["data"]["evidence"]
        self.assertEqual(evidence["source"]["state"], "unfetched_reference")
        self.assertIsNone(evidence["source"]["material_id"])

        source_mismatch = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": malformed.json["data"]["subject"]["version"], "scope": "identity",
                "source": {"kind": "material", "material_id": self.add_material()},
                "excerpt": "不存在于原件的摘录",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "wrong-excerpt"},
        )
        self.assertEqual(source_mismatch.status_code, 400)
        self.assertEqual(source_mismatch.json["error"]["code"], "EVIDENCE_EXCERPT_NOT_FOUND")

    def test_viewer_and_other_workspace_cannot_write_or_read_subject(self) -> None:
        subject = self.create_subject(key="permission-subject")
        viewer_token = self.create_member(role="viewer")
        forbidden = self.request(
            "patch", f"/api/business/subjects/{subject['id']}",
            json={"base_version": subject["version"], "name": "越权修改"},
            headers=self.auth_headers(viewer_token),
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json["error"]["code"], "FORBIDDEN")
        read_allowed = self.request("get", f"/api/business/subjects/{subject['id']}", headers=self.auth_headers(viewer_token))
        self.assertEqual(read_allowed.status_code, 200)

        other_token, _workspace_id = self.create_other_workspace()
        not_found = self.request("get", f"/api/business/subjects/{subject['id']}", headers=self.auth_headers(other_token))
        self.assertEqual(not_found.status_code, 404)
        self.assertEqual(not_found.json["error"]["code"], "NOT_FOUND")
        guest_response = self.app.test_client().get("/api/business/subjects", base_url=BASE_URL)
        self.assertEqual(guest_response.status_code, 401)
        guest_response.close()

    def test_relationship_time_and_evidence_scope_are_validated(self) -> None:
        subject = self.create_subject(key="period-subject")
        invalid_period = self.request(
            "post", f"/api/business/subjects/{subject['id']}/relationships",
            json={
                "base_subject_version": subject["version"], "relation_type": "operates_in",
                "related_object": {"kind": "market", "id": "CN"},
                "valid_time_kind": "interval", "valid_from": "2026-11-01", "valid_to": "2026-01-01",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "invalid-period"},
        )
        self.assertEqual(invalid_period.status_code, 400)

        unrelated_evidence = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"], "scope": "identity",
                "source": {"kind": "user_statement", "title": "主体核阅"},
                "excerpt": "同名品牌可能是不同实体。",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "identity-only-evidence"},
        )
        self.assertEqual(unrelated_evidence.status_code, 201)
        scope_error = self.request(
            "post", f"/api/business/subjects/{subject['id']}/relationships",
            json={
                "base_subject_version": unrelated_evidence.json["data"]["subject"]["version"],
                "relation_type": "operates_in", "related_object": {"kind": "market", "id": "CN"},
                "review_status": "supported", "evidence_ids": [unrelated_evidence.json["data"]["evidence"]["id"]],
                "valid_time_kind": "observation", "observed_at": "2025-06-01T00:00:00Z",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "scope-error"},
        )
        self.assertEqual(scope_error.status_code, 400)
        self.assertEqual(scope_error.json["error"]["code"], "EVIDENCE_SCOPE_MISMATCH")

    def test_nonempty_legacy_subject_and_case_migrations_preserve_rows_and_recovery(self) -> None:
        subject = self.create_subject(key="legacy-migration-subject")
        evidence_response = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"], "scope": "relationship",
                "source": {"kind": "user_statement", "title": "隔离迁移验证"},
                "excerpt": "门店由运营主体管理。",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "legacy-migration-evidence"},
        )
        self.assertEqual(evidence_response.status_code, 201, evidence_response.get_data(as_text=True))
        evidence = evidence_response.json["data"]["evidence"]
        relation_response = self.request(
            "post", f"/api/business/subjects/{subject['id']}/relationships",
            json={
                "base_subject_version": evidence_response.json["data"]["subject"]["version"],
                "relation_type": "managed_by",
                "related_object": {"kind": "merchant", "id": "merchant-migration", "label": "隔离商户"},
                "review_status": "needs_review", "evidence_ids": [],
                "valid_time_kind": "unknown",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "legacy-migration-relation"},
        )
        self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
        relation = relation_response.json["data"]["relationship"]
        case_response = self.request(
            "post", "/api/business/cases",
            json={"subject_id": subject["id"], "title": "迁移验证Case", "question": "旧Case内容保留吗？", "source_refs": []},
            headers={**self.auth_headers(), "Idempotency-Key": "legacy-migration-case"},
        )
        self.assertEqual(case_response.status_code, 201, case_response.get_data(as_text=True))
        case = case_response.json["data"]["case"]

        before = {
            "subject": dict(self.store.row("SELECT * FROM business_subjects WHERE id=?", (subject["id"],))),
            "evidence": dict(self.store.row("SELECT * FROM business_evidence WHERE id=?", (evidence["id"],))),
            "relation": dict(self.store.row("SELECT * FROM business_relations WHERE id=?", (relation["id"],))),
            "case": dict(self.store.row("SELECT * FROM business_cases WHERE id=?", (case["id"],))),
        }
        connection = self.store.connect()
        try:
            original_subject_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_subjects'"
            ).fetchone()[0]
            legacy_subject_sql = re.sub(
                r"'brand',\s*'company',\s*'legal_entity',.*?'unclassified'",
                "'brand', 'company'", original_subject_sql, count=1, flags=re.S,
            )
            self.assertNotEqual(legacy_subject_sql, original_subject_sql)
            original_case_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_cases'"
            ).fetchone()[0]
            legacy_case_sql = original_case_sql.replace(
                "subject_id TEXT REFERENCES business_subjects(id)",
                "subject_id TEXT NOT NULL REFERENCES business_subjects(id)", 1,
            ).replace(
                "subject_version_at_link INTEGER CHECK(subject_version_at_link > 0)",
                "subject_version_at_link INTEGER NOT NULL CHECK(subject_version_at_link > 0)", 1,
            )
            self.assertNotEqual(legacy_case_sql, original_case_sql)
            schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE sqlite_schema SET sql=? WHERE name='business_subjects'", (legacy_subject_sql,))
            connection.execute("UPDATE sqlite_schema SET sql=? WHERE name='business_cases'", (legacy_case_sql,))
            connection.execute(f"PRAGMA schema_version={schema_version + 1}")
            connection.commit()
            connection.execute("PRAGMA writable_schema=OFF")
        finally:
            connection.close()

        legacy_case_column = next(
            row for row in self.store.rows("PRAGMA table_info(business_cases)") if row["name"] == "subject_id"
        )
        self.assertEqual(legacy_case_column["notnull"], 1)
        from oevira.business import install_business_schema

        install_business_schema(self.store)
        migrated = {
            "subject": dict(self.store.row("SELECT * FROM business_subjects WHERE id=?", (subject["id"],))),
            "evidence": dict(self.store.row("SELECT * FROM business_evidence WHERE id=?", (evidence["id"],))),
            "relation": dict(self.store.row("SELECT * FROM business_relations WHERE id=?", (relation["id"],))),
            "case": dict(self.store.row("SELECT * FROM business_cases WHERE id=?", (case["id"],))),
        }
        self.assertEqual(migrated["subject"], before["subject"])
        self.assertEqual(migrated["evidence"], before["evidence"])
        self.assertEqual(migrated["relation"], before["relation"])
        self.assertEqual(migrated["case"], before["case"])
        migrated_case_column = next(
            row for row in self.store.rows("PRAGMA table_info(business_cases)") if row["name"] == "subject_id"
        )
        self.assertEqual(migrated_case_column["notnull"], 0)

        recovery_dir = self.store.root / "recovery"
        subject_backups = list(recovery_dir.glob("before-subject-kind-*.sqlite3"))
        case_backups = list(recovery_dir.glob("before-case-context-*.sqlite3"))
        self.assertEqual(len(subject_backups), 1)
        self.assertEqual(len(case_backups), 1)
        for path in (subject_backups[0], case_backups[0]):
            snapshot = sqlite3.connect(path)
            try:
                self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(snapshot.execute("SELECT id FROM business_subjects WHERE id=?", (subject["id"],)).fetchone()[0], subject["id"])
                self.assertEqual(snapshot.execute("SELECT id FROM business_relations WHERE id=?", (relation["id"],)).fetchone()[0], relation["id"])
                self.assertEqual(snapshot.execute("SELECT id FROM business_evidence WHERE id=?", (evidence["id"],)).fetchone()[0], evidence["id"])
                self.assertEqual(snapshot.execute("SELECT id FROM business_cases WHERE id=?", (case["id"],)).fetchone()[0], case["id"])
            finally:
                snapshot.close()

        independent_case = self.request(
            "post", "/api/business/cases",
            json={"title": "独立服务记录", "question": "没有已知主体时也可开始吗？", "source_refs": []},
            headers={**self.auth_headers(), "Idempotency-Key": "post-migration-independent-case"},
        )
        self.assertEqual(independent_case.status_code, 201, independent_case.get_data(as_text=True))
        self.assertIsNone(independent_case.json["data"]["case"]["subject"]["id"])

    def test_shared_subject_context_never_crosses_requested_work_scope(self) -> None:
        from oevira.api import ApiError
        from oevira.business import load_business_subject_context

        subject = self.create_subject(key="shared-subject-context")
        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        work_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        commission_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        with self.store.transaction() as connection:
            for index, (work_id, commission_id) in enumerate(zip(work_ids, commission_ids, strict=True)):
                connection.execute(
                    "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                    (commission_id, workspace_id, f"隔离客户{index}", user_id, now, now),
                )
                if index < 2:
                    connection.execute(
                        "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'owner',1)",
                        (commission_id, user_id),
                    )
                connection.execute(
                    "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'{}','active',1,?,?)",
                    (work_id, workspace_id, user_id, f"客户工作{index}", "隔离范围验证", now, now),
                )
                connection.execute(
                    "INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)",
                    (work_id, commission_id, now),
                )

        shared_evidence_response = self.request(
            "post", f"/api/business/subjects/{subject['id']}/evidence",
            json={
                "base_version": subject["version"], "scope": "identity",
                "source": {"kind": "user_statement", "title": "工作区共享身份说明"},
                "excerpt": "此为工作区可共享的主体身份说明。",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "shared-subject-evidence"},
        )
        self.assertEqual(shared_evidence_response.status_code, 201, shared_evidence_response.get_data(as_text=True))
        shared_evidence_id = shared_evidence_response.json["data"]["evidence"]["id"]
        subject_version = shared_evidence_response.json["data"]["subject"]["version"]
        private_evidence_ids = []
        private_relation_ids = []
        for index, work_id in enumerate(work_ids[:2]):
            material_id = str(uuid.uuid4())
            content = f"客户{index}私有证明材料，门店运营证据。"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,reason,text,sha256,relative_path,size_bytes,source_url,etag,last_modified,source_json,source_text,created_at) VALUES(?,?,?,?,?,'private.txt','txt','ready',NULL,?,?,?, ?,NULL,NULL,NULL,'{}',NULL,?)",
                    (material_id, workspace_id, user_id, work_id, f"客户{index}材料", content, digest,
                     f"objects/{material_id}.txt", len(content.encode("utf-8")), now),
                )
            evidence_response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/evidence",
                json={
                    "base_version": subject_version, "scope": "relationship",
                    "source": {"kind": "material", "material_id": material_id},
                    "excerpt": "门店运营证据。", "location": "text:1",
                },
                headers={**self.auth_headers(), "Idempotency-Key": f"private-subject-evidence-{index}"},
            )
            self.assertEqual(evidence_response.status_code, 201, evidence_response.get_data(as_text=True))
            evidence_id = evidence_response.json["data"]["evidence"]["id"]
            private_evidence_ids.append(evidence_id)
            relation_response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/relationships",
                json={
                    "base_subject_version": subject_version, "relation_type": "managed_by",
                    "related_object": {"kind": "merchant", "id": f"merchant-{index}"},
                    "review_status": "supported", "evidence_ids": [evidence_id],
                    "valid_time_kind": "unknown",
                },
                headers={**self.auth_headers(), "Idempotency-Key": f"private-subject-relation-{index}"},
            )
            self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
            private_relation_ids.append(relation_response.json["data"]["relationship"]["id"])

        contexts = {}
        with self.store.transaction(immediate=False) as connection:
            for index, work_id in enumerate(work_ids[:2]):
                contexts[index] = load_business_subject_context(
                    connection, self.identity, subject["id"], work_id=work_id,
                )
            with self.assertRaises(ApiError) as denied:
                load_business_subject_context(
                    connection, self.identity, subject["id"], work_id=work_ids[2],
                )
            self.assertEqual(denied.exception.status, 404)

        evidence_by_work = {
            index: {item["id"] for item in contexts[index]["evidence"]}
            for index in range(2)
        }
        self.assertIn(shared_evidence_id, evidence_by_work[0])
        self.assertIn(shared_evidence_id, evidence_by_work[1])
        self.assertIn(private_evidence_ids[0], evidence_by_work[0])
        self.assertNotIn(private_evidence_ids[1], evidence_by_work[0])
        self.assertIn(private_evidence_ids[1], evidence_by_work[1])
        self.assertNotIn(private_evidence_ids[0], evidence_by_work[1])
        relation_by_work = {
            index: {item["id"] for item in contexts[index]["relationships"]}
            for index in range(2)
        }
        self.assertIn(private_relation_ids[0], relation_by_work[0])
        self.assertNotIn(private_relation_ids[1], relation_by_work[0])
        self.assertIn(private_relation_ids[1], relation_by_work[1])
        self.assertNotIn(private_relation_ids[0], relation_by_work[1])
        self.assertEqual(
            contexts[0]["version"], subject_version,
            "client-private evidence must not revise the shared Subject identity version",
        )
        analysis_response = self.request(
            "post", f"/api/works/{work_ids[0]}/business-results",
            json={
                "operation": "service_response",
                "inputs": {"goal": "整理本客户服务回应", "as_of": "2026-11-10T00:00:00Z"},
                "subjects": [{"id": subject["id"], "version": subject_version}],
            },
            headers={**self.auth_headers(), "Idempotency-Key": "work-a-business-subject-context"},
        )
        self.assertEqual(analysis_response.status_code, 201, analysis_response.get_data(as_text=True))
        inaccessible_relation_history = self.request(
            "get",
            f"/api/business/relationships/{private_relation_ids[1]}/versions?work_id={work_ids[0]}",
            headers=self.auth_headers(),
        )
        self.assertEqual(inaccessible_relation_history.status_code, 404)
        cross_client_analysis = self.request(
            "post", f"/api/works/{work_ids[0]}/business-results",
            json={
                "operation": "service_response",
                "inputs": {"goal": "不得读取另一客户关系", "as_of": "2026-11-10T00:00:00Z"},
                "subjects": [{"id": subject["id"], "version": subject_version}],
                "relationships": [{"id": private_relation_ids[1], "version": 1}],
            },
            headers={**self.auth_headers(), "Idempotency-Key": "work-a-explicit-work-b-relationship"},
        )
        self.assertEqual(cross_client_analysis.status_code, 404, cross_client_analysis.get_data(as_text=True))
        versions = self.request(
            "get", f"/api/business/subjects/{subject['id']}/versions", headers=self.auth_headers(),
        )
        self.assertEqual(versions.status_code, 200)
        self.assertTrue(all(private_evidence_ids[0] not in json.dumps(item) for item in versions.json["data"]))

    def test_candidate_resolution_uses_names_ids_locations_and_material_without_auto_merge(self) -> None:
        first = self.request(
            "post", "/api/business/subjects",
            json={
                "kind": "store", "name": "云栖广场一店",
                "profile": {
                    "external_ids": [{"scheme": "venue_registry", "jurisdiction": "CN-GZ", "value": "STORE-001"}],
                    "locations": [{"role": "store", "country": "CN", "city": "广州", "address": "天河路1号"}],
                },
            },
            headers=self.idempotency_headers("identity-candidate-store-a"),
        )
        second = self.request(
            "post", "/api/business/subjects",
            json={
                "kind": "store", "name": "云栖广场一店",
                "profile": {
                    "external_ids": [{"scheme": "venue_registry", "jurisdiction": "CN-GZ", "value": "STORE-009"}],
                    "locations": [{"role": "store", "country": "CN", "city": "深圳", "address": "深南路9号"}],
                },
            },
            headers=self.idempotency_headers("identity-candidate-store-b"),
        )
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(second.status_code, 201, second.get_data(as_text=True))
        source_subject = first.json["data"]["subject"]
        different_city_subject = second.json["data"]["subject"]
        self.assertNotEqual(source_subject["id"], different_city_subject["id"])
        self.assertIsNone(source_subject["canonical_subject_id"])
        self.assertIsNone(different_city_subject["canonical_subject_id"])
        differently_typed = self.request(
            "post", "/api/business/subjects",
            json={"kind": "legal_entity", "name": "云栖广场一店"},
            headers=self.idempotency_headers("identity-candidate-different-kind"),
        )
        self.assertEqual(differently_typed.status_code, 201, differently_typed.get_data(as_text=True))
        count_before_lookup = self.store.row("SELECT COUNT(*) AS count FROM business_subjects")["count"]

        resolved = self.request(
            "post", "/api/business/subjects/resolve",
            json={
                "name": "云栖广场一店", "kind": "store",
                "profile": {
                    "external_ids": [{"scheme": "venue_registry", "jurisdiction": "CN-GZ", "value": "STORE-001"}],
                    "locations": [{"role": "store", "country": "CN", "city": "广州", "address": "天河路1号"}],
                },
            },
            headers=self.auth_headers(),
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.json["data"]["resolution_state"], "conflict")
        candidates = {item["id"]: item for item in resolved.json["data"]["candidates"]}
        self.assertIn(source_subject["id"], candidates)
        self.assertIn(different_city_subject["id"], candidates)
        self.assertIn(differently_typed.json["data"]["subject"]["id"], candidates)
        self.assertIn("stable_identifier_exact", candidates[source_subject["id"]]["match_reasons"])
        difference_kinds = {item["kind"] for item in candidates[different_city_subject["id"]]["differences"]}
        self.assertIn("stable_identifier_conflict", difference_kinds)
        self.assertIn("location_difference", difference_kinds)
        self.assertIn(
            "different_object_kind",
            {item["kind"] for item in candidates[differently_typed.json["data"]["subject"]["id"]]["differences"]},
        )
        self.assertFalse(resolved.json["data"]["automatic_merge"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM business_subjects")["count"], count_before_lookup)

        alias_subject_response = self.request(
            "post", "/api/business/subjects",
            json={"kind": "merchant", "name": "海港商户", "profile": {"aliases": ["海港市集铺"]}},
            headers=self.idempotency_headers("identity-candidate-alias"),
        )
        self.assertEqual(alias_subject_response.status_code, 201, alias_subject_response.get_data(as_text=True))
        alias_resolution = self.request(
            "post", "/api/business/subjects/resolve",
            json={"name": "海港市集铺", "kind": "merchant"}, headers=self.auth_headers(),
        )
        self.assertEqual(alias_resolution.status_code, 200)
        alias_candidate = next(
            item for item in alias_resolution.json["data"]["candidates"]
            if item["id"] == alias_subject_response.json["data"]["subject"]["id"]
        )
        self.assertIn("alias", alias_candidate["match_reasons"])

        material_subject = self.create_subject(kind="store", name="海湾门店C", key="material-identity-subject")
        material_id = self.add_material(excerpt="有海湾门店C服务记录")
        material_candidates = self.request(
            "post", "/api/business/subjects/resolve",
            json={"material_id": material_id}, headers=self.auth_headers(),
        )
        self.assertEqual(material_candidates.status_code, 200, material_candidates.get_data(as_text=True))
        self.assertIn(
            material_subject["id"],
            {item["id"] for item in material_candidates.json["data"]["candidates"]},
        )
        self.assertIn(
            "material_phrase",
            next(item for item in material_candidates.json["data"]["candidates"] if item["id"] == material_subject["id"])["match_reasons"],
        )

    def test_explicit_merge_split_preserves_subject_ids_history_and_scoped_consumers(self) -> None:
        identifier = [{"scheme": "venue_registry", "jurisdiction": "CN-GZ", "value": "COMMON-STORE-42"}]
        source_response = self.request(
            "post", "/api/business/subjects",
            json={"kind": "store", "name": "白云湖门店旧名", "profile": {"external_ids": identifier}},
            headers=self.idempotency_headers("merge-source-create"),
        )
        target_response = self.request(
            "post", "/api/business/subjects",
            json={"kind": "store", "name": "白云湖店", "profile": {"external_ids": identifier}},
            headers=self.idempotency_headers("merge-target-create"),
        )
        third_response = self.request(
            "post", "/api/business/subjects", json={"kind": "business_venue", "name": "白云湖商业项目"},
            headers=self.idempotency_headers("merge-third-create"),
        )
        self.assertEqual(source_response.status_code, 201)
        self.assertEqual(target_response.status_code, 201)
        self.assertEqual(third_response.status_code, 201)
        source = source_response.json["data"]["subject"]
        target = target_response.json["data"]["subject"]
        third = third_response.json["data"]["subject"]

        def add_identity_evidence(subject, key, excerpt):
            response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/evidence",
                json={
                    "base_version": subject["version"], "scope": "identity",
                    "source": {"kind": "user_statement", "title": "隔离身份纠正依据"},
                    "excerpt": excerpt,
                },
                headers=self.idempotency_headers(key),
            )
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            return response.json["data"]["evidence"]["id"], response.json["data"]["subject"]

        source_evidence, source = add_identity_evidence(source, "merge-source-evidence", "来源材料显示旧门店别称与登记编号。")
        target_evidence, target = add_identity_evidence(target, "merge-target-evidence", "来源材料显示新门店名称与同一登记编号。")
        relation = self.request(
            "post", f"/api/business/subjects/{source['id']}/relationships",
            json={
                "base_subject_version": source["version"], "relation_type": "located_at",
                "related_object": {"kind": "venue", "id": "venue-baiyun", "label": "白云湖商业项目"},
                "review_status": "needs_review", "valid_time_kind": "unknown",
            },
            headers=self.idempotency_headers("merge-source-relation"),
        )
        self.assertEqual(relation.status_code, 201, relation.get_data(as_text=True))
        direct_conflict = self.request(
            "post", f"/api/business/subjects/{source['id']}/relationships",
            json={"base_subject_version": source["version"], "relation_type": "agency_for",
                  "related_subject_id": target["id"], "review_status": "needs_review", "valid_time_kind": "unknown"},
            headers=self.idempotency_headers("merge-direct-relation-conflict"),
        )
        self.assertEqual(direct_conflict.status_code, 201, direct_conflict.get_data(as_text=True))
        case = self.request(
            "post", "/api/business/cases",
            json={"subject_id": source["id"], "title": "旧门店服务跟进", "question": "原门店问题待复查。", "source_refs": []},
            headers=self.idempotency_headers("merge-source-case"),
        )
        self.assertEqual(case.status_code, 201, case.get_data(as_text=True))

        preview = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge-preview",
            json={"target_subject_id": target["id"], "source_base_version": source["version"], "target_base_version": target["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertFalse(preview.json["data"]["merge_available"])
        self.assertFalse(preview.json["data"]["automatic_merge"])
        merge_request = {
            "target_subject_id": target["id"],
            "source_base_version": source["version"],
            "target_base_version": target["version"],
            "preview_token": preview.json["data"]["preview_token"],
            "reason": "人工确认登记编号、门店角色和期间相符；保留来源名作为历史别名。",
            "source_evidence_ids": [source_evidence],
            "target_evidence_ids": [target_evidence],
        }
        second_relation = self.request(
            "post", f"/api/business/subjects/{source['id']}/relationships",
            json={
                "base_subject_version": source["version"], "relation_type": "managed_by",
                "related_subject_id": third["id"],
                "review_status": "needs_review", "valid_time_kind": "unknown",
            },
            headers=self.idempotency_headers("merge-source-new-relation-after-preview"),
        )
        self.assertEqual(second_relation.status_code, 201, second_relation.get_data(as_text=True))
        blocked_merge = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge",
            json=merge_request, headers=self.idempotency_headers("merge-blocked-by-direct-edge"),
        )
        self.assertEqual(blocked_merge.status_code, 409)
        self.assertEqual(blocked_merge.json["error"]["code"], "SUBJECT_MERGE_BLOCKED")
        withdrawn_conflict = self.request(
            "delete", f"/api/business/relationships/{direct_conflict.json['data']['relationship']['id']}",
            json={"base_version": 1}, headers=self.auth_headers(),
        )
        self.assertEqual(withdrawn_conflict.status_code, 200, withdrawn_conflict.get_data(as_text=True))
        stale_merge = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge",
            json=merge_request, headers=self.idempotency_headers("stale-identity-merge-preview"),
        )
        self.assertEqual(stale_merge.status_code, 409)
        self.assertEqual(stale_merge.json["error"]["code"], "SUBJECT_MERGE_PREVIEW_STALE")
        preview = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge-preview",
            json={"target_subject_id": target["id"], "source_base_version": source["version"], "target_base_version": target["version"]},
            headers=self.auth_headers(),
        )
        merge_request["preview_token"] = preview.json["data"]["preview_token"]
        self.assertTrue(preview.json["data"]["merge_available"])
        merged = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge",
            json=merge_request, headers=self.idempotency_headers("explicit-identity-merge"),
        )
        self.assertEqual(merged.status_code, 201, merged.get_data(as_text=True))
        self.assertEqual(merged.json["data"]["source"]["canonical_subject_id"], target["id"])
        self.assertEqual(merged.json["data"]["source"]["identity_resolution"]["state"], "merged_into")
        self.assertEqual(merged.json["data"]["target"]["identity_review"]["status"], "needs_review")
        replayed = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge",
            json=merge_request, headers=self.idempotency_headers("explicit-identity-merge"),
        )
        self.assertEqual(replayed.status_code, 200)
        self.assertTrue(replayed.json["data"]["replayed"])

        target_detail = self.request("get", f"/api/business/subjects/{target['id']}", headers=self.auth_headers())
        self.assertEqual(target_detail.status_code, 200)
        self.assertEqual({item["id"] for item in target_detail.json["data"]["evidence"]}, {source_evidence, target_evidence})
        self.assertEqual(
            {item["id"] for item in target_detail.json["data"]["relationships"]},
            {relation.json["data"]["relationship"]["id"], second_relation.json["data"]["relationship"]["id"],
             direct_conflict.json["data"]["relationship"]["id"]},
        )
        self.assertEqual(target_detail.json["data"]["merged_aliases"][0]["id"], source["id"])
        alias_resolution = self.request(
            "post", "/api/business/subjects/resolve",
            json={"name": "白云湖门店旧名", "kind": "store", "profile": {"external_ids": identifier}},
            headers=self.auth_headers(),
        )
        self.assertEqual(alias_resolution.status_code, 200, alias_resolution.get_data(as_text=True))
        canonical_candidate = next(
            item for item in alias_resolution.json["data"]["candidates"] if item["id"] == target["id"]
        )
        self.assertIn("merged_alias", canonical_candidate["match_reasons"])
        active_subjects = self.request("get", "/api/business/subjects", headers=self.auth_headers())
        listed_target = next(item for item in active_subjects.json["data"] if item["id"] == target["id"])
        self.assertIn(source["id"], {item["id"] for item in listed_target["merged_aliases"]})
        source_case_list = self.request("get", f"/api/business/cases?subject_id={target['id']}", headers=self.auth_headers())
        self.assertEqual([item["id"] for item in source_case_list.json["data"]], [case.json["data"]["case"]["id"]])
        source_case_subject = source_case_list.json["data"][0]["subject"]
        self.assertEqual(source_case_subject["canonical_subject_id"], target["id"])
        cannot_remove_canonical = self.request(
            "delete", f"/api/business/subjects/{target['id']}",
            json={"base_version": merged.json["data"]["target"]["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(cannot_remove_canonical.status_code, 409)
        self.assertEqual(cannot_remove_canonical.json["error"]["code"], "SUBJECT_HAS_MERGED_ALIASES")

        old_alias_evidence = self.request(
            "post", f"/api/business/subjects/{source['id']}/evidence",
            json={"base_version": merged.json["data"]["source"]["version"], "scope": "identity",
                  "source": {"kind": "user_statement", "title": "不应写入别名"}, "excerpt": "已合并别名不能写入"},
            headers=self.idempotency_headers("merged-alias-evidence"),
        )
        self.assertEqual(old_alias_evidence.status_code, 409)
        self.assertEqual(old_alias_evidence.json["error"]["code"], "SUBJECT_MERGED")

        split_preview = self.request(
            "post", f"/api/business/subjects/{source['id']}/split-preview",
            json={"base_version": merged.json["data"]["source"]["version"],
                  "canonical_base_version": merged.json["data"]["target"]["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(split_preview.status_code, 200, split_preview.get_data(as_text=True))
        split_request = {
            "base_version": merged.json["data"]["source"]["version"],
            "canonical_base_version": merged.json["data"]["target"]["version"],
            "preview_token": split_preview.json["data"]["preview_token"],
            "reason": "后续证据表明两者为不同经营门店，撤销误合并。",
            "source_evidence_ids": [source_evidence],
            "canonical_evidence_ids": [target_evidence],
        }
        split = self.request(
            "post", f"/api/business/subjects/{source['id']}/split",
            json=split_request, headers=self.idempotency_headers("split-identity-merge"),
        )
        self.assertEqual(split.status_code, 201, split.get_data(as_text=True))
        self.assertEqual(split.json["data"]["source"]["directory_state"], "active")
        self.assertIsNone(split.json["data"]["source"]["canonical_subject_id"])
        self.assertEqual(split.json["data"]["source"]["identity_resolution"]["state"], "split_from")
        target_after_split = self.request("get", f"/api/business/subjects/{target['id']}", headers=self.auth_headers())
        source_after_split = self.request("get", f"/api/business/subjects/{source['id']}", headers=self.auth_headers())
        self.assertEqual({item["id"] for item in target_after_split.json["data"]["evidence"]}, {target_evidence})
        self.assertEqual({item["id"] for item in source_after_split.json["data"]["evidence"]}, {source_evidence})
        cases_after_split = self.request("get", f"/api/business/cases?subject_id={target['id']}", headers=self.auth_headers())
        self.assertEqual(cases_after_split.json["data"], [])

    def test_merged_subject_context_keeps_private_client_evidence_and_relations_separate(self) -> None:
        from oevira.business import load_business_subject_context

        now = utc_now()
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        work_ids, commissions = [], []
        for index in range(2):
            commission_id, work_id = str(uuid.uuid4()), str(uuid.uuid4())
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) VALUES(?,?,?,'{}','active',1,?,?,?)",
                    (commission_id, workspace_id, f"合并隔离客户{index}", user_id, now, now),
                )
                connection.execute(
                    "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?, 'owner',1)",
                    (commission_id, user_id),
                )
                connection.execute(
                    "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'{}','active',1,?,?)",
                    (work_id, workspace_id, user_id, f"合并隔离工作{index}", "验证不同客户事实隔离", now, now),
                )
                connection.execute("INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)", (work_id, commission_id, now))
            work_ids.append(work_id)
            commissions.append(commission_id)

        source_response = self.request(
            "post", "/api/business/subjects", json={"kind": "store", "name": "清河路旧门店"},
            headers=self.idempotency_headers("scoped-merge-source"),
        )
        target_response = self.request(
            "post", "/api/business/subjects", json={"kind": "store", "name": "清河路门店"},
            headers=self.idempotency_headers("scoped-merge-target"),
        )
        source, target = source_response.json["data"]["subject"], target_response.json["data"]["subject"]

        def add_shared(subject, key):
            response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/evidence",
                json={"base_version": subject["version"], "scope": "identity",
                      "source": {"kind": "user_statement", "title": "共享身份核阅"},
                      "excerpt": "用户分别提供了身份核对依据。"},
                headers=self.idempotency_headers(key),
            )
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            return response.json["data"]["evidence"]["id"], response.json["data"]["subject"]

        source_identity, source = add_shared(source, "scoped-merge-source-identity")
        target_identity, target = add_shared(target, "scoped-merge-target-identity")
        private_evidence_ids, private_relation_ids = [], []
        for index, (subject, work_id) in enumerate(zip((source, target), work_ids, strict=True)):
            material_id = str(uuid.uuid4())
            content = f"客户{index}私有门店证据。"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,reason,text,sha256,relative_path,size_bytes,source_url,etag,last_modified,source_json,source_text,created_at) VALUES(?,?,?,?,?,'private.txt','txt','ready',NULL,?,?,?, ?,NULL,NULL,NULL,'{}',NULL,?)",
                    (material_id, workspace_id, user_id, work_id, f"客户{index}私有材料", content, digest,
                     f"objects/{material_id}.txt", len(content.encode("utf-8")), now),
                )
            evidence_response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/evidence",
                json={"base_version": subject["version"], "scope": "relationship",
                      "source": {"kind": "material", "material_id": material_id},
                      "excerpt": "私有门店证据。", "location": "text:1"},
                headers=self.idempotency_headers(f"scoped-merge-private-evidence-{index}"),
            )
            self.assertEqual(evidence_response.status_code, 201, evidence_response.get_data(as_text=True))
            evidence_id = evidence_response.json["data"]["evidence"]["id"]
            private_evidence_ids.append(evidence_id)
            relation_response = self.request(
                "post", f"/api/business/subjects/{subject['id']}/relationships",
                json={"base_subject_version": subject["version"], "relation_type": "managed_by",
                      "related_object": {"kind": "merchant", "id": f"merchant-{index}"},
                      "review_status": "needs_review", "evidence_ids": [evidence_id],
                      "valid_time_kind": "unknown"},
                headers=self.idempotency_headers(f"scoped-merge-private-relation-{index}"),
            )
            self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
            private_relation_ids.append(relation_response.json["data"]["relationship"]["id"])

        preview = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge-preview",
            json={"target_subject_id": target["id"], "source_base_version": source["version"], "target_base_version": target["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        merge_data = {
            "target_subject_id": target["id"], "source_base_version": source["version"],
            "target_base_version": target["version"], "preview_token": preview.json["data"]["preview_token"],
            "reason": "分客户保存的门店别名和公开身份材料指向同一现实门店；私有证据仍按原Work隔离。",
            "source_evidence_ids": [source_identity], "target_evidence_ids": [target_identity],
        }
        merged = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge", json=merge_data,
            headers=self.idempotency_headers("scoped-subject-merge"),
        )
        self.assertEqual(merged.status_code, 201, merged.get_data(as_text=True))

        from oevira.business import load_business_subject_context
        contexts = {}
        with self.store.transaction(immediate=False) as connection:
            for index, work_id in enumerate(work_ids):
                contexts[index] = load_business_subject_context(connection, self.identity, target["id"], work_id=work_id)
        evidence_a = {item["id"] for item in contexts[0]["evidence"]}
        evidence_b = {item["id"] for item in contexts[1]["evidence"]}
        self.assertIn(private_evidence_ids[0], evidence_a)
        self.assertNotIn(private_evidence_ids[1], evidence_a)
        self.assertIn(private_evidence_ids[1], evidence_b)
        self.assertNotIn(private_evidence_ids[0], evidence_b)
        relation_a = {item["id"] for item in contexts[0]["relationships"]}
        relation_b = {item["id"] for item in contexts[1]["relationships"]}
        self.assertIn(private_relation_ids[0], relation_a)
        self.assertNotIn(private_relation_ids[1], relation_a)
        self.assertIn(private_relation_ids[1], relation_b)
        self.assertNotIn(private_relation_ids[0], relation_b)

    def test_misattached_relationship_correction_keeps_old_endpoint_in_history(self) -> None:
        original_subject = self.create_subject(kind="merchant", name="候选商户甲", key="misattached-source-a")
        corrected_subject = self.create_subject(kind="merchant", name="确认商户乙", key="misattached-source-b")
        related_subject = self.create_subject(kind="legal_entity", name="运营法律主体", key="misattached-target")
        relation_response = self.request(
            "post", f"/api/business/subjects/{original_subject['id']}/relationships",
            json={
                "base_subject_version": original_subject["version"],
                "relation_type": "operates_in",
                "related_object": {"kind": "market", "id": "CN"},
                "review_status": "needs_review",
                "valid_time_kind": "unknown",
            },
            headers=self.idempotency_headers("misattached-relation-create"),
        )
        self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
        relation_id = relation_response.json["data"]["relationship"]["id"]
        corrected = self.request(
            "patch", f"/api/business/relationships/{relation_id}",
            json={
                "base_version": 1,
                "base_subject_version": corrected_subject["version"],
                "source_subject_id": corrected_subject["id"],
                "relation_type": "managed_by",
                "related_subject_id": related_subject["id"],
                "correction_reason": "原商户归属错误；按当前材料改挂至商户乙和其运营组织。",
            },
            headers=self.auth_headers(),
        )
        self.assertEqual(corrected.status_code, 200, corrected.get_data(as_text=True))
        relationship = corrected.json["data"]
        self.assertEqual(relationship["subject_id"], corrected_subject["id"])
        self.assertEqual(relationship["relation_type"], "managed_by")
        self.assertEqual(relationship["target"]["id"], related_subject["id"])
        self.assertEqual(relationship["review"]["status"], "needs_review")
        self.assertIn("原商户归属错误", relationship["review"]["note"])
        history = self.request(
            "get", f"/api/business/relationships/{relation_id}/versions", headers=self.auth_headers(),
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json["data"]), 2)
        self.assertEqual(history.json["data"][0]["operation"], "created")
        self.assertEqual(history.json["data"][0]["snapshot"]["subject_id"], original_subject["id"])
        self.assertEqual(history.json["data"][0]["snapshot"]["related_object_id"], "CN")
        self.assertEqual(history.json["data"][1]["operation"], "corrected")
        self.assertEqual(history.json["data"][1]["snapshot"]["subject_id"], corrected_subject["id"])
        self.assertEqual(history.json["data"][1]["snapshot"]["related_subject_id"], related_subject["id"])

    def test_business_artifact_can_adopt_corrected_subject_and_relation_versions(self) -> None:
        work_response = self.request(
            "post", "/api/works", json={"title": "主体上下文Artifact工作", "goal": "复核客户门店服务"},
            headers=self.idempotency_headers("context-revision-work"),
        )
        self.assertEqual(work_response.status_code, 201, work_response.get_data(as_text=True))
        work_id = work_response.json["data"]["id"]
        source = self.create_subject(kind="store", name="清湾旧门店", key="context-revision-source")
        canonical = self.create_subject(kind="store", name="清湾门店", key="context-revision-canonical")
        source_identity = self.add_evidence(
            source, scope="identity", material_id=self.add_material(excerpt="旧门店注册编号与营业记录"),
            excerpt="旧门店注册编号与营业记录", key="context-revision-source-evidence",
        )
        canonical_identity = self.add_evidence(
            canonical, scope="identity", material_id=self.add_material(excerpt="清湾门店注册编号与营业记录"),
            excerpt="清湾门店注册编号与营业记录", key="context-revision-canonical-evidence",
        )
        relation_response = self.request(
            "post", f"/api/business/subjects/{source['id']}/relationships",
            json={"base_subject_version": source["version"] + 1,
                  "relation_type": "operates_in", "related_object": {"kind": "market", "id": "CN"},
                  "review_status": "needs_review", "valid_time_kind": "unknown"},
            headers=self.idempotency_headers("context-revision-source-relation"),
        )
        self.assertEqual(relation_response.status_code, 201, relation_response.get_data(as_text=True))
        relation_id = relation_response.json["data"]["relationship"]["id"]
        initial = self.request(
            "post", f"/api/works/{work_id}/business-results",
            json={"operation": "service_response",
                  "inputs": {"goal": "复核清湾旧门店的服务诉求", "as_of": "2026-11-10T00:00:00Z"},
                  "subjects": [{"id": source["id"], "version": source["version"] + 1}],
                  "relationships": [{"id": relation_id, "version": 1}]},
            headers=self.idempotency_headers("context-revision-result"),
        )
        self.assertEqual(initial.status_code, 201, initial.get_data(as_text=True))
        artifact_id = initial.json["data"]["id"]
        manual = self.request(
            "post", f"/api/business-results/{artifact_id}/revisions",
            json={"base_version": 1, "section_edits": [{"id": "response", "body": "客户确认保留的人工回应文本。"}]},
            headers=self.idempotency_headers("context-revision-manual-text"),
        )
        self.assertEqual(manual.status_code, 200, manual.get_data(as_text=True))
        from oevira.api import ApiError
        from oevira.business_execution import revise_business_result
        with self.assertRaises(ApiError) as automated_context_change:
            revise_business_result(
                self.store, self.identity, manual.json["data"]["id"],
                {"base_version": 2, "context_changes": {
                    "subjects": [{"id": canonical["id"], "version": canonical["version"]}],
                    "reason": "模型不能决定此身份。",
                }},
                "model-cannot-select-subject", origin="intelligence",
            )
        self.assertEqual(automated_context_change.exception.code, "HUMAN_REVIEW_REQUIRED")

        merge_preview = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge-preview",
            json={"target_subject_id": canonical["id"], "source_base_version": source["version"] + 1,
                  "target_base_version": canonical["version"] + 1},
            headers=self.auth_headers(),
        )
        self.assertEqual(merge_preview.status_code, 200, merge_preview.get_data(as_text=True))
        merged = self.request(
            "post", f"/api/business/subjects/{source['id']}/merge",
            json={"target_subject_id": canonical["id"], "source_base_version": source["version"] + 1,
                  "target_base_version": canonical["version"] + 1,
                  "preview_token": merge_preview.json["data"]["preview_token"],
                  "reason": "人工核对后将旧名称并入同一门店身份。",
                  "source_evidence_ids": [source_identity["id"]],
                  "target_evidence_ids": [canonical_identity["id"]]},
            headers=self.idempotency_headers("context-revision-merge"),
        )
        self.assertEqual(merged.status_code, 201, merged.get_data(as_text=True))
        canonical_version = merged.json["data"]["target"]["version"]
        relation_correction = self.request(
            "patch", f"/api/business/relationships/{relation_id}",
            json={"base_version": 1, "base_subject_version": canonical_version,
                  "source_subject_id": canonical["id"],
                  "correction_reason": "服务关系现归属于经纠正的规范Subject。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(relation_correction.status_code, 200, relation_correction.get_data(as_text=True))
        self.assertEqual(relation_correction.json["data"]["version"], 2)

        locked = self.request(
            "post", f"/api/business-results/{artifact_id}/revisions",
            json={"base_version": 2, "input_changes": {"goal": "重新核对清湾门店服务"}},
            headers=self.idempotency_headers("context-revision-stale-lock"),
        )
        self.assertEqual(locked.status_code, 409)
        self.assertEqual(locked.json["error"]["code"], "SUBJECT_MERGED")

        adopted = self.request(
            "post", f"/api/business-results/{artifact_id}/revisions",
            json={"base_version": 2,
                  "context_changes": {
                      "subjects": [{"id": canonical["id"], "version": canonical_version}],
                      "relationships": [{"id": relation_id, "version": 2}],
                      "reason": "明确采纳合并后的Subject与纠正后的关系版本。",
                  },
                  "revision_note": "仅更新Subject/Relation上下文，保留人工答复。"},
            headers=self.idempotency_headers("context-revision-adopt"),
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        adopted_snapshot = adopted.json["data"]["input_snapshot"]["business"]
        self.assertEqual(adopted_snapshot["context"]["subjects"][0]["id"], canonical["id"])
        self.assertEqual(adopted_snapshot["context"]["relationships"][0]["version"], 2)
        self.assertEqual(adopted_snapshot["context_revisions"][-1]["previous_refs"]["subjects"][0]["id"], source["id"])
        self.assertIn("客户确认保留的人工回应文本。", adopted.json["data"]["body"])
        self.assertIn("没有仅凭身份更正自动重写业务结论", adopted.json["data"]["body"])
        historical = self.request("get", f"/api/artifacts/{artifact_id}?version=2")
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.json["data"]["input_snapshot"]["business"]["context"]["subjects"][0]["id"], source["id"])

        recomputed = self.request(
            "post", f"/api/business-results/{artifact_id}/revisions",
            json={"base_version": 3, "input_changes": {"goal": "重新核对清湾门店的当前服务诉求"}},
            headers=self.idempotency_headers("context-revision-recompute"),
        )
        self.assertEqual(recomputed.status_code, 200, recomputed.get_data(as_text=True))
        self.assertEqual(recomputed.json["data"]["input_snapshot"]["business"]["context"]["subjects"][0]["id"], canonical["id"])
        self.assertIn("客户确认保留的人工回应文本。", recomputed.json["data"]["body"])


if __name__ == "__main__":
    unittest.main()
