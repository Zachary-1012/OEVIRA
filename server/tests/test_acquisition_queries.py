from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from server.oevira import create_app
from server.oevira.auth import issue_local_capability, issue_session, utc_now
from server.oevira.worker import run_once


ORIGIN = "http://127.0.0.1:8341"


class FakeBrowserOwner:
    def __init__(self, *, work_id=None):
        self.work_id = work_id
        self.read_calls = []
        self.preview_calls = []
        self.session_unavailable = False
        self.preview_expired = False
        self.preview = None

    def get_session(self, identity, session_id):
        if self.session_unavailable or session_id != "selected-session":
            raise RuntimeError("not found")
        return {"id": session_id, "work_id": self.work_id, "status": "open"}

    def read_context(self, identity, session_id, data, *, actor=None, expected_control_generation=None):
        self.read_calls.append((identity, session_id, data, actor, expected_control_generation))
        snapshot = {
            "session_id": session_id, "version": data["base_version"], "work_id": self.work_id,
            "control_generation": expected_control_generation or 1, "page_id": data["page_id"],
            "page_revision": data["page_revision"], "url": "https://example.test/article",
            "title": "Release report", "text": "The release report confirms the spring product launch.",
            "text_sha256": "a" * 64,
            "snapshot_version": {"schema": 1, "page_revision": data["page_revision"],
                                 "sha256": "b" * 64, "captured_at": utc_now()},
            "frames": [{"frame_id": "main", "status": "read", "text": "The release report confirms the spring product launch."}],
            "coverage": {"status": "complete", "scope": "visible_dom_blocks", "include_frames": data["include_frames"],
                         "limitations": ["The selected page is a bounded visible DOM snapshot."]},
        }
        capture_ref = {
            "capture_id": "capture-1", "session_id": session_id, "page_id": data["page_id"],
            "page_revision": data["page_revision"], "work_id": self.work_id,
            "captured_at": snapshot["snapshot_version"]["captured_at"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=11)).isoformat(timespec="seconds").replace("+00:00", "Z"), "snapshot_sha256": "b" * 64,
            "preview_sha256": "c" * 64,
            "projection_kind": "sanitized_reader", "projection_version": 1,
            "coverage": snapshot["coverage"], "source_admission": "NOT_ADMITTED",
        }
        snapshot["capture_ref"] = capture_ref
        self.preview = snapshot
        return snapshot

    def get_context_preview(self, identity, session_id, capture_id, *, field_paths, purpose, action):
        self.preview_calls.append((identity, session_id, capture_id))
        if self.session_unavailable:
            raise FakeBrowserError("BROWSER_PREVIEW_NOT_FOUND")
        if self.preview_expired:
            raise FakeBrowserError("BROWSER_PREVIEW_EXPIRED")
        if (self.preview is None or capture_id != self.preview["capture_ref"]["capture_id"]
                or session_id != self.preview["session_id"]):
            raise FakeBrowserError("BROWSER_PREVIEW_NOT_FOUND")
        if field_paths != ("text",) or action != "search":
            raise FakeBrowserError("SOURCE_USE_ACTION_UNSUPPORTED")
        return {**self.preview, "capture_ref": {
            **self.preview["capture_ref"],
            "source_use": {"allowed": True, "state": "not_recorded_local", "decision_id": None,
                           "reason_code": "SOURCE_USE_NOT_REQUIRED_FOR_LOCAL_PROCESSING"},
        }}


class FakeBrowserError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class AcquisitionQueryHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-query-", dir=r"D:\寰意\.runtime")
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8341)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.store = self.app.extensions["oevira.store"]
        self.capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, self.capability_file)
        response = self.client.post(
            "/api/local-session", json={"capability": self.capability_file.read_text(encoding="ascii"),
                                        "display_name": "Query Tester"},
            base_url=ORIGIN, headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        self.user_id = response.json["data"]["user"]["id"]
        self.workspace_id = response.json["data"]["workspace"]["id"]

    def tearDown(self):
        self.temporary.cleanup()

    def headers(self, key=None):
        value = {"Authorization": f"Bearer {self.token}"}
        if key:
            value["Idempotency-Key"] = key
        return value

    def request(self, method, path, **kwargs):
        kwargs.setdefault("base_url", ORIGIN)
        return getattr(self.client, method)(path, **kwargs)

    def create_work(self, title):
        response = self.request("post", "/api/works", json={"title": title, "goal": "query source"},
                                headers=self.headers(f"work:{title}"))
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]["id"]

    def create_material(self, title, text, work_id=None):
        body = {"title": title, "text": text}
        if work_id:
            body["work_id"] = work_id
        response = self.request("post", "/api/materials", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def assign_commission(self, work_ids, name):
        commission_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,'active',1,?,?,?)",
                (commission_id, self.workspace_id, name, json.dumps({"purpose": "isolation test"}),
                 self.user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'owner',1)",
                (commission_id, self.user_id),
            )
            for work_id in work_ids:
                connection.execute(
                    "INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)",
                    (work_id, commission_id, now),
                )
        return commission_id

    def add_source_use(self, material, *, expires_at=None, decision_status="allowed"):
        decision_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO source_use_decisions(decision_id,workspace_id,owner_user_id,object_kind,object_id,"
                "object_revision,scope_kind,work_id,client_id,field_paths_json,purpose,action,recipient_kind,"
                "recipient_ref,basis_status,decision_status,allow_external_processing,allow_redistribution,"
                "allow_training,retention_until,refresh_after,expires_at,basis_ref_json,policy_scope_sha256,"
                "version,revoked_at,revoked_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'workspace',NULL,NULL,?,'knowledge_retrieval','search','local',?,'contract_verified',?,"
                "0,0,0,NULL,NULL,?,'{}',?,1,NULL,NULL,?,?)",
                (decision_id, self.workspace_id, self.user_id, "material", material["id"], material["sha256"],
                 json.dumps(["text"]), self.workspace_id, decision_status, expires_at,
                 hashlib.sha256(decision_id.encode("ascii")).hexdigest(), now, now),
            )
        return decision_id

    def post_query(self, body, key="query-1"):
        return self.request("post", "/api/search", json=body, headers=self.headers(key))

    def base_body(self, query, sources, *, work_id=None, period=None, required_data=None):
        body = {"query": query, "purpose": "knowledge_retrieval", "language": "en",
                "allowed_sources": sources, "business_timezone": "Asia/Shanghai"}
        if work_id:
            body["work_context"] = {"work_id": work_id}
        if period:
            body["period"] = period
        if required_data:
            body["required_data"] = required_data
        return body

    def test_work_query_returns_lexical_evidence_and_reopens_after_factory_restart(self):
        work_id = self.create_work("Source research")
        material = self.create_material("Release note", "The product launch moved to April.", work_id)
        body = self.base_body("product launch", {"workspace": True}, work_id=work_id)

        created = self.post_query(body, "work-query")
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        data = created.json["data"]
        self.assertEqual(data["query"], "product launch")
        self.assertEqual(len(data["query_paths"]), 1)
        path = data["query_paths"][0]
        self.assertEqual(path["data_surface"], "material_text")
        self.assertEqual(path["status"], "complete")
        self.assertEqual(path["result"]["evidence"][0]["material_id"], material["id"])
        self.assertEqual(path["result"]["evidence"][0]["source_work_id"], work_id)

        reopened_app = create_app(self.root, expected_port=8341)
        reopened_app.config.update(TESTING=True)
        reopened = reopened_app.test_client().get(
            f"/api/search/{data['query_id']}", base_url=ORIGIN, headers=self.headers(),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["query_id"], data["query_id"])
        self.assertEqual(reopened.json["data"]["query_paths"][0]["result"], path["result"])

    def test_material_query_rebuilds_only_from_saved_authorized_material_refs(self):
        work_id = self.create_work("Material projection recovery")
        material = self.create_material("Recoverable source", "The product launch is scheduled for April.", work_id)
        response = self.post_query(
            self.base_body("product launch", {"workspace": True}, work_id=work_id), "material-query-recovery",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        query_id = response.json["data"]["query_id"]
        path_id = response.json["data"]["query_paths"][0]["path_id"]
        expected = response.json["data"]["query_paths"][0]["result"]["evidence"]
        self.assertEqual(expected[0]["material_id"], material["id"])
        with self.store.transaction() as connection:
            connection.execute("UPDATE acquisition_query_paths SET result_json=NULL WHERE path_id=?", (path_id,))

        reopened = self.request("get", f"/api/search/{query_id}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["query_paths"][0]["result"]["evidence"], expected)
        self.assertTrue(self.store.row(
            "SELECT result_json FROM acquisition_query_paths WHERE path_id=?", (path_id,),
        )["result_json"])

    def test_no_work_workspace_query_keeps_each_original_work_in_its_own_path(self):
        work_a, work_b = self.create_work("Client A source"), self.create_work("Client B source")
        self.assign_commission([work_a], "Client A")
        self.assign_commission([work_b], "Client B")
        source_a = self.create_material("A report", "Product launch timing for client A.", work_a)
        source_b = self.create_material("B report", "Product launch timing for client B.", work_b)

        response = self.post_query(self.base_body("product launch", {"workspace": True}), "all-private-works")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.json["data"]
        work_paths = [path for path in data["query_paths"] if path["data_surface"] == "material_text"]
        self.assertEqual({path["source_ref"]["work_id"] for path in work_paths}, {work_a, work_b})
        self.assertEqual(len(data["results"]), 2)
        seen = set()
        for path in work_paths:
            refs = {item["material_id"] for item in path["result"]["evidence"]}
            self.assertEqual(len(refs), 1)
            seen.update(refs)
        self.assertEqual(seen, {source_a["id"], source_b["id"]})
        self.assertTrue(all(item["items"] for item in data["results"]))

    def test_explicit_same_client_cross_work_material_is_reused_with_original_work_ref(self):
        source_work, target_work = self.create_work("Original source work"), self.create_work("Analysis work")
        commission_id = self.assign_commission([source_work, target_work], "Shared client")
        material = self.create_material("Shared source", "Shared client's release dates are confirmed.", source_work)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO work_materials(work_id,material_id,workspace_id,created_at) VALUES(?,?,?,?)",
                (target_work, material["id"], self.workspace_id, now),
            )

        response = self.post_query(
            self.base_body("release dates", {"workspace": True, "material_ids": [material["id"]]},
                           work_id=target_work),
            "same-client-explicit-adoption",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        evidence = response.json["data"]["query_paths"][0]["result"]["evidence"][0]
        self.assertEqual(evidence["material_id"], material["id"])
        self.assertEqual(evidence["source_work_id"], source_work)
        self.assertEqual(response.json["data"]["query_paths"][0]["scope"]["work_id"], target_work)

    def test_cross_client_explicit_material_is_rejected_without_creating_query_plan(self):
        source_work, target_work = self.create_work("Private source"), self.create_work("Different client target")
        self.assign_commission([source_work], "Client source")
        self.assign_commission([target_work], "Client target")
        material = self.create_material("Private report", "Private product launch evidence.", source_work)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO work_materials(work_id,material_id,workspace_id,created_at) VALUES(?,?,?,?)",
                (target_work, material["id"], self.workspace_id, now),
            )
        response = self.post_query(
            self.base_body("private launch", {"workspace": True, "material_ids": [material["id"]]},
                           work_id=target_work),
            "cross-client-reject",
        )
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM acquisition_query_plans")["count"], 0)

    def test_selected_browser_page_is_read_by_owner_without_material_creation_and_preview_expires(self):
        owner = FakeBrowserOwner()
        self.app.extensions["oevira.browser"] = owner
        before = self.store.row("SELECT COUNT(*) AS count FROM materials")["count"]
        body = self.base_body("product launch", {
            "browser_targets": [{"session_id": "selected-session", "base_version": 3,
                                 "page_id": "page-1", "page_revision": 7,
                                 "expected_control_generation": 2}],
        })
        today = datetime.now(timezone.utc).date()
        body["period"] = {"start": (today - timedelta(days=1)).isoformat(),
                         "end": (today + timedelta(days=1)).isoformat(), "field": "captured_at"}
        body["freshness"] = {"max_age_seconds": 86_400}
        response = self.post_query(body, "selected-page")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.json["data"]
        path = data["query_paths"][0]
        self.assertEqual(path["status"], "complete")
        self.assertEqual(path["coverage"]["time_basis_state"], "matched_observation_time")
        self.assertEqual(path["coverage"]["freshness_state"], "observed_now")
        self.assertEqual(path["result"]["results"][0]["quote"],
                         "The release report confirms the spring product launch.")
        self.assertEqual(owner.read_calls[0][1], "selected-session")
        self.assertEqual(owner.read_calls[0][2], {"base_version": 3, "page_id": "page-1",
                                                  "page_revision": 7, "include_frames": True,
                                                  "retain_preview": True})
        self.assertIsNone(owner.read_calls[0][3])
        self.assertEqual(owner.read_calls[0][4], 2)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], before)

        path_row = self.store.row("SELECT * FROM acquisition_query_paths WHERE path_id=?", (path["path_id"],))
        self.assertTrue(path_row["result_json"])
        self.assertNotIn("confirms the spring product launch", path_row["result_json"].lower())
        self.assertEqual(json.loads(path_row["checkpoint_ref_json"])["capture_ref"]["capture_id"], "capture-1")
        reopened = self.request("get", f"/api/search/{data['query_id']}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json["data"]["query_paths"][0]["result"]["source_ref"]["page_revision"], 7)
        self.assertEqual(len(owner.read_calls), 1)
        owner.preview_expired = True
        expired = self.request("get", f"/api/search/{data['query_id']}", headers=self.headers())
        self.assertEqual(expired.status_code, 200)
        expired_path = expired.json["data"]["query_paths"][0]
        self.assertEqual(expired_path["stop_reason"], "BROWSER_PREVIEW_EXPIRED")
        self.assertEqual(expired_path["result"]["results"], [])
        self.assertIn("release report", path_row["result_json"].lower())

    def test_browser_page_does_not_claim_published_time_without_that_data_surface(self):
        owner = FakeBrowserOwner()
        self.app.extensions["oevira.browser"] = owner
        body = self.base_body("product launch", {
            "browser_targets": [{"session_id": "selected-session", "base_version": 3,
                                 "page_id": "page-1", "page_revision": 7}],
        }, period={"start": "2026-11-01", "end": "2026-12-01", "field": "published_at"})
        response = self.post_query(body, "browser-published-period")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        path = response.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "partial")
        self.assertEqual(path["stop_reason"], "TIME_BASIS_UNAVAILABLE_FOR_SELECTED_BROWSER_PAGE")
        self.assertEqual(path["result"]["results"], [])

    def test_browser_restore_access_loss_keeps_metadata_without_reopening_the_page(self):
        owner = FakeBrowserOwner()
        self.app.extensions["oevira.browser"] = owner
        body = self.base_body("product launch", {
            "browser_targets": [{"session_id": "selected-session", "base_version": 3,
                                 "page_id": "page-1", "page_revision": 7}],
        })
        created = self.post_query(body, "browser-restore-tombstone")
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        query_id = created.json["data"]["query_id"]
        owner.session_unavailable = True

        reopened = self.request("get", f"/api/search/{query_id}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        path = reopened.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "unavailable")
        self.assertEqual(path["stop_reason"], "BROWSER_SESSION_ACCESS_UNAVAILABLE")
        self.assertEqual(path["source_ref"]["page_revision"], 7)
        self.assertEqual(path["result"]["results"], [])
        self.assertEqual(len(owner.read_calls), 1)

    def test_query_uses_real_browser_owner_frozen_reader_preview(self):
        from server.tests.test_browser_host_reader import BrowserHostReaderTests
        from server.oevira.auth import authenticate
        from server.oevira.browser_host_reader import read_browser_context

        BrowserHostReaderTests.setUpClass()
        fixture = BrowserHostReaderTests("test_reader_returns_frame_attributed_unicode_quotes_and_safe_urls")
        setup_complete = False
        try:
            fixture.setUp()
            setup_complete = True
            session = fixture.navigate()
            before = fixture.browser.store.row("SELECT COUNT(*) AS count FROM materials")["count"]
            body = {
                "query": "main before", "purpose": "knowledge_retrieval",
                "allowed_sources": {"browser_targets": [{
                    "session_id": session["id"], "base_version": session["version"],
                    "page_id": session["page_id"], "page_revision": session["page_revision"],
                    "include_frames": False,
                }]},
            }
            with patch("server.oevira.browser_host_reader.read_browser_context",
                       wraps=read_browser_context) as read_page:
                created = fixture.request("post", "/api/search", body)
                self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
                data = created.json["data"]
                path = data["query_paths"][0]
                self.assertEqual(path["path_kind"], "browser_page")
                self.assertTrue(path["result"]["results"], path)
                self.assertTrue(any("Main before" in item["quote"] for item in path["result"]["results"]))
                self.assertEqual(path["source_ref"]["source_admission"], "NOT_ADMITTED")
                self.assertIn("capture_id", path["checkpoint_ref"]["capture_ref"])
                self.assertEqual(path["result"]["source_use_state"], "not_recorded_local")

                reopened = fixture.request("get", f"/api/search/{data['query_id']}")
                self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
                self.assertEqual(reopened.json["data"]["query_paths"][0]["result"]["results"],
                                 path["result"]["results"])
                self.assertEqual(read_page.call_count, 1, "GET must reuse the frozen Browser preview, not reread the live page")
                capture_ref = path["checkpoint_ref"]["capture_ref"]
                decision_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                fixture_identity = authenticate(fixture.browser.store, fixture.token)
                fixture_user_id = fixture_identity["user"]["id"]
                fixture_workspace_id = fixture_identity["workspace"]["id"]
                with fixture.browser.store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO source_use_decisions(decision_id,workspace_id,owner_user_id,object_kind,object_id,"
                        "object_revision,scope_kind,work_id,client_id,field_paths_json,purpose,action,recipient_kind,"
                        "recipient_ref,basis_status,decision_status,allow_external_processing,allow_redistribution,"
                        "allow_training,retention_until,refresh_after,expires_at,basis_ref_json,policy_scope_sha256,"
                        "version,revoked_at,revoked_by,created_at,updated_at) "
                        "VALUES(?,?,?,'browser_capture',?,?,'workspace',NULL,NULL,?,'knowledge_retrieval','search',"
                        "'local',?,'contract_verified','revoked',0,0,0,NULL,NULL,'2099-01-01T00:00:00Z','{}',?,1,?,?,?,?)",
                        (decision_id, fixture_workspace_id, fixture_user_id,
                         capture_ref["capture_id"], capture_ref.get("preview_sha256") or capture_ref["snapshot_sha256"],
                         json.dumps(["text"]), fixture_workspace_id, hashlib.sha256(decision_id.encode("ascii")).hexdigest(),
                         now, fixture_user_id, now, now),
                    )
                denied = fixture.request("get", f"/api/search/{data['query_id']}")
                self.assertEqual(denied.status_code, 200, denied.get_data(as_text=True))
                denied_path = denied.json["data"]["query_paths"][0]
                self.assertEqual(denied_path["status"], "blocked")
                self.assertEqual(denied_path["stop_reason"], "SOURCE_USE_REVOKED")
                self.assertEqual(denied_path["result"]["results"], [])
                self.assertEqual(read_page.call_count, 1, "SourceUse revocation must stop the frozen preview before reuse")
            after = fixture.browser.store.row("SELECT COUNT(*) AS count FROM materials")["count"]
            self.assertEqual(after, before, "A selected Reader preview is not a saved Material")
        finally:
            if setup_complete:
                fixture.tearDown()
            BrowserHostReaderTests.tearDownClass()

    def test_workspace_path_budget_keeps_explicit_browser_target_and_marks_local_remainder(self):
        owner = FakeBrowserOwner()
        self.app.extensions["oevira.browser"] = owner
        work_a, work_b = self.create_work("Budget work A"), self.create_work("Budget work B")
        self.create_material("Budget source A", "Product launch in April.", work_a)
        self.create_material("Budget source B", "Product launch in May.", work_b)
        body = self.base_body("product launch", {
            "workspace": True,
            "browser_targets": [{"session_id": "selected-session", "base_version": 3,
                                 "page_id": "page-1", "page_revision": 7}],
        })
        body["budget"] = {"max_paths": 1}
        response = self.post_query(body, "budget-selected-browser")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        paths = response.json["data"]["query_paths"]
        self.assertEqual(sum(path["path_kind"] == "browser_page" for path in paths), 1)
        self.assertEqual(sum(path["path_kind"] == "scope_budget" for path in paths), 1)
        self.assertEqual(sum(path["path_kind"] == "material_search" for path in paths), 1)
        self.assertEqual(len(owner.read_calls), 1)

    def test_public_search_unavailable_does_not_discard_local_material_results(self):
        material = self.create_material("Local article", "The product launch is planned for spring.")
        response = self.post_query(self.base_body("product launch", {"workspace": True, "public": True}),
                                   "public-and-local")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        paths = response.json["data"]["query_paths"]
        local = next(path for path in paths if path["data_surface"] == "material_text")
        public = next(path for path in paths if path["data_surface"] == "web_search")
        self.assertEqual(local["result"]["evidence"][0]["material_id"], material["id"])
        self.assertEqual(public["status"], "unavailable")
        self.assertEqual(public["stop_reason"], "SEARCH_PROVIDER_NOT_BOUND")

    def test_local_material_results_page_without_mixing_paths(self):
        work_id = self.create_work("Paged research")
        first = self.create_material("First launch note", "Launch date confirmed for April.", work_id)
        second = self.create_material("Second launch note", "Launch timing confirmed for May.", work_id)
        body = self.base_body("launch confirmed", {"workspace": True}, work_id=work_id)
        body["page"] = {"size": 1}
        response = self.post_query(body, "paged-query")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.json["data"]
        path = data["query_paths"][0]
        first_page = path["result"]["evidence"]
        self.assertEqual(len(first_page), 1)
        self.assertTrue(path["cursor"])
        next_page = self.request(
            "get", f"/api/search/{data['query_id']}?cursor={path['cursor']}", headers=self.headers(),
        )
        self.assertEqual(next_page.status_code, 200, next_page.get_data(as_text=True))
        page_evidence = next_page.json["data"]["query_paths"][0]["result"]["evidence"]
        self.assertEqual(len(page_evidence), 1)
        self.assertNotEqual(page_evidence[0]["material_id"], first_page[0]["material_id"])
        self.assertEqual({first_page[0]["material_id"], page_evidence[0]["material_id"]},
                         {first["id"], second["id"]})
        self.assertIsNone(next_page.json["data"]["query_paths"][0]["cursor"])

    def test_source_use_without_current_owner_blocks_only_that_material_path(self):
        material = self.create_material("Rights-held note", "The launch date is recorded here.")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET source_json=? WHERE id=?",
                (json.dumps({"source_use_decision_id": "unresolved"}), material["id"]),
            )
        response = self.post_query(self.base_body("launch date", {"workspace": True}), "source-use-block")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        path = response.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "blocked")
        self.assertEqual(path["stop_reason"], "SOURCE_USE_DECLARATION_UNAVAILABLE")
        self.assertEqual(path["native_refs"], [])
        self.assertNotIn(material["id"], json.dumps(path, ensure_ascii=False))

    def test_query_uses_current_source_use_decision_and_rechecks_revocation(self):
        material = self.create_material("Approved local source", "The product launch is confirmed.")
        decision_id = self.add_source_use(material, expires_at="2099-01-01T00:00:00Z")
        response = self.post_query(self.base_body("product launch", {"workspace": True}), "source-use-allow")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.json["data"]
        path = data["query_paths"][0]
        evidence = path["result"]["evidence"][0]
        self.assertEqual(evidence["source_use_decision"]["decision_id"], decision_id)
        self.assertEqual(evidence["source_use_state"], "allowed")

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE source_use_decisions SET decision_status='revoked',revoked_at=?,version=version+1,updated_at=? "
                "WHERE decision_id=?",
                (utc_now(), utc_now(), decision_id),
            )
        reopened = self.request("get", f"/api/search/{data['query_id']}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["query_paths"], [])
        self.assertEqual(reopened.json["data"]["results"], [])

    def test_expired_source_use_decision_blocks_only_that_material(self):
        material = self.create_material("Expired local source", "The product launch is confirmed.")
        self.add_source_use(material, expires_at="2000-01-01T00:00:00Z")
        response = self.post_query(self.base_body("product launch", {"workspace": True}), "source-use-expired")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        path = response.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "blocked")
        self.assertEqual(path["stop_reason"], "SOURCE_USE_EXPIRED")
        self.assertEqual(path["native_refs"], [])

    def test_period_filter_does_not_guess_dates_for_unverified_uploaded_material(self):
        material = self.create_material("Undated note", "Release launch is discussed.")
        body = self.base_body(
            "release launch", {"workspace": True},
            period={"start": "2026-11-01", "end": "2026-12-01", "field": "published_at"},
        )
        response = self.post_query(body, "unknown-date")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        path = response.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "partial")
        self.assertEqual(path["stop_reason"], "TIME_BASIS_UNVERIFIED")
        self.assertEqual(path["result"]["evidence"], [])
        self.assertEqual(path["coverage"]["unknown_time_materials"], 1)

    def test_query_is_owner_private_and_work_loss_hides_saved_source_path(self):
        work_id = self.create_work("Private query work")
        self.assign_commission([work_id], "Private query client")
        material = self.create_material("Client evidence", "Client launch is confirmed.", work_id)
        response = self.post_query(
            self.base_body("client launch", {"workspace": True}, work_id=work_id), "private-query",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        query_id = response.json["data"]["query_id"]

        other_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                               (other_id, "Other member", now))
            connection.execute("INSERT INTO memberships(workspace_id,user_id,role,active,created_at) "
                               "VALUES(?,?,'editor',1,?)", (self.workspace_id, other_id, now))
        other_token, _ = issue_session(self.store, other_id, self.workspace_id)
        other_read = self.request("get", f"/api/search/{query_id}",
                                  headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(other_read.status_code, 404)

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE commission_members SET active=0 WHERE user_id=? AND commission_id=("
                "SELECT commission_id FROM work_commissions WHERE work_id=?)",
                (self.user_id, work_id),
            )
        owner_read = self.request("get", f"/api/search/{query_id}", headers=self.headers())
        self.assertEqual(owner_read.status_code, 200)
        self.assertEqual(owner_read.json["data"]["query_paths"], [])
        self.assertEqual(owner_read.json["data"]["results"], [])
        self.assertIsNone(owner_read.json["data"]["work_context"])
        self.assertIsNone(owner_read.json["data"]["scope_used"].get("work_id"))
        self.assertNotIn("material_ids", owner_read.json["data"]["scope_used"])

    def test_explicit_url_uses_existing_acquire_url_job_and_replays_idempotently(self):
        body = self.base_body("report", {"public_urls": ["https://example.test/report"]})
        first = self.post_query(body, "url-query")
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        first_path = first.json["data"]["query_paths"][0]
        self.assertEqual(first_path["path_kind"], "public_url_acquisition")
        self.assertEqual(first_path["status"], "queued")
        job_id = first_path["checkpoint_ref"]["job_id"]
        self.assertEqual(self.store.row("SELECT kind FROM jobs WHERE id=?", (job_id,))["kind"], "acquire_url")
        second = self.post_query(body, "url-query")
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(second.json["data"]["query_id"], first.json["data"]["query_id"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind='acquire_url'")["count"], 1)

    def test_completed_url_child_job_returns_only_its_verified_saved_material(self):
        url = "https://example.test/query-result"
        body = self.base_body("product launch", {"public_urls": [url]})
        created = self.post_query(body, "completed-url-query")
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        query_id = created.json["data"]["query_id"]
        text = "The public report confirms the product launch in April."
        content = b"<html><main>source bytes</main></html>"
        acquired = {
            "status": "ready", "content": content, "text": text, "mime_type": "text/html",
            "title": "Public report", "url": url, "requested_url": url,
            "captured_at": utc_now(), "published_at": None, "author": None,
            "etag": '"query-v1"', "last_modified": None, "acquisition_method": "public_http",
            "extraction_scope": "main_content", "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "limitations": [],
        }
        with patch("server.oevira.acquisition.acquire_url", return_value=acquired) as acquire:
            self.assertTrue(run_once(self.app))
        acquire.assert_called_once()

        reopened = self.request("get", f"/api/search/{query_id}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        path = reopened.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "completed")
        material_result = path["result"]["material_result"]
        self.assertEqual(material_result["evidence"][0]["quote"], text)
        self.assertEqual(material_result["acquisition_receipt"]["kind"], "acquire_url")
        self.assertEqual(material_result["acquisition_receipt"]["job_id"], path["checkpoint_ref"]["job_id"])

    def test_url_job_does_not_claim_unknown_published_time_matches_requested_period(self):
        url = "https://news.example/undated-query"
        body = self.base_body("product launch", {"public_urls": [url]}, period={
            "start": "2026-11-01", "end": "2026-12-01", "field": "published_at",
        })
        created = self.post_query(body, "undated-url-period")
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        query_id = created.json["data"]["query_id"]
        text = "The report confirms a product launch."
        content = b"<html><main>undated public source</main></html>"
        acquired = {
            "status": "ready", "content": content, "text": text, "mime_type": "text/html",
            "title": "Undated report", "url": url, "requested_url": url,
            "captured_at": utc_now(), "published_at": None, "author": None,
            "etag": None, "last_modified": None, "acquisition_method": "public_http",
            "extraction_scope": "main_content", "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "limitations": [],
        }
        with patch("server.oevira.acquisition.acquire_url", return_value=acquired):
            self.assertTrue(run_once(self.app))
        reopened = self.request("get", f"/api/search/{query_id}", headers=self.headers())
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        path = reopened.json["data"]["query_paths"][0]
        self.assertEqual(path["status"], "completed")
        self.assertEqual(path["stop_reason"], "TIME_BASIS_UNVERIFIED")
        self.assertEqual(path["result"]["material_result"]["evidence"], [])
        self.assertEqual(path["native_refs"][-1]["kind"], "material")

    def test_missing_idempotency_or_empty_source_scope_is_rejected(self):
        body = self.base_body("empty query", {})
        response = self.post_query(body, "empty-scope")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "SOURCE_SCOPE_REQUIRED")
        body = self.base_body("valid query", {"public": True})
        response = self.request("post", "/api/search", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "IDEMPOTENCY_KEY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
