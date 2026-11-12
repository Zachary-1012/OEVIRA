from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import tempfile
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from server.oevira import create_app
from server.oevira.api import ApiError
from server.oevira.auth import authenticate, issue_local_capability, issue_session, utc_now
from server.oevira.browser import BrowserError, install_browser_schema
from server.oevira.browser_api import BrowserService
from server.oevira.intelligence import ExplicitModelSelection, ModelInputAuthorization, _digest
from server.oevira.intelligence_base import IntelligenceError
from server.oevira.store import Store


ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://127.0.0.1:8321"


class _TestBrowserStateProtector:
    protection_name = "isolated-test"

    @staticmethod
    def protect(payload: bytes, _entropy: bytes) -> bytes:
        return payload

    @staticmethod
    def unprotect(payload: bytes, _entropy: bytes) -> bytes:
        return payload


class BrowserControlAndToolTests(unittest.TestCase):
    def setUp(self):
        self.runtime_root = ROOT / ".runtime" / "browser-host" / "tests"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="control-", dir=self.runtime_root)
        self.app = create_app(self.temporary.name, expected_port=8321)
        self.app.config.update(TESTING=True, BROWSER_HEADLESS=True)
        self.client = self.app.test_client()
        self.store = self.app.extensions["oevira.store"]
        self.browser: BrowserService = self.app.extensions["oevira.browser"]
        self.intelligence = self.app.extensions["oevira.intelligence"]

        capability = Path(self.temporary.name) / "launch.capability"
        issue_local_capability(self.store, capability)
        response = self.client.post(
            "/api/local-session", base_url=BASE_URL,
            json={"capability": capability.read_text(encoding="ascii"), "display_name": "Control owner"},
            headers={"Origin": BASE_URL},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.token = response.json["data"]["token"]
        self.identity = authenticate(self.store, self.token)
        self.work = self.request("post", "/api/works", {
            "title": "Browser control work", "goal": "Review an authorized public source",
        }).json["data"]
        self.session_id = self.insert_browser_session(self.identity, self.work["id"])

    def tearDown(self):
        self.browser.runtime.shutdown()
        self.temporary.cleanup()

    def headers(self, token: str | None = None, key: str | None = None) -> dict:
        value = {"Authorization": "Bearer " + (token or self.token)}
        if key:
            value["Idempotency-Key"] = key
        return value

    def request(self, method: str, path: str, data: dict | None = None, *, token: str | None = None,
                key: str | None = None):
        return getattr(self.client, method)(
            path, base_url=BASE_URL, json=data,
            headers=self.headers(token, key or uuid.uuid4().hex),
        )

    def insert_browser_session(self, identity: dict, work_id: str) -> str:
        member = self.store.row(
            "SELECT created_at FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (identity["workspace"]["id"], identity["user"]["id"]),
        )
        now = utc_now()
        session_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO browser_sessions(
                       id, workspace_id, owner_user_id, membership_created_at, authorized_by_session_id,
                       work_id, engine, status, version, page_revision, current_url, current_title,
                       created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'edge', 'open', 1, 0, 'about:blank', '', ?, ?)""",
                (session_id, identity["workspace"]["id"], identity["user"]["id"], member["created_at"],
                 identity["session_id"], work_id, now, now),
            )
        return session_id

    def create_peer(self) -> tuple[str, dict]:
        peer_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id, name, created_at) VALUES(?, 'Control peer', ?)", (peer_id, now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'editor', 1, ?)",
                (self.identity["workspace"]["id"], peer_id, now),
            )
        token, _ = issue_session(self.store, peer_id, self.identity["workspace"]["id"])
        return token, authenticate(self.store, token)

    def tool_context(self, *, artifact_write_intent=True):
        prepared = self.intelligence.prepare_context(
            self.identity, "Read a selected public source for this Work.",
            work_id=self.work["id"], include_memory=False,
            artifact_write_intent=artifact_write_intent,
        )
        return self.intelligence.create_tool_context(
            prepared, caller="model", operation_scope="browser-control-test-turn",
        )

    def assert_browser_tools_registered(self):
        required = {
            "browser.sessions.list", "browser.page.read", "browser.page.navigate",
            "browser.page.capture", "browser.session.reconcile", "acquisition.enqueue_url",
            "acquisition.job.read", "monitoring.list", "monitoring.signals",
        }
        registered = self.intelligence.tools._bindings
        self.assertTrue(required.issubset(registered))
        return tuple(registered[name] for name in sorted(required))

    def test_control_defaults_manual_and_rejects_arbitrary_http_actor(self):
        state = self.request("get", f"/api/browser-sessions/{self.session_id}/control")
        self.assertEqual(state.status_code, 200, state.json)
        self.assertEqual(state.json["data"]["control_mode"], "user")
        self.assertEqual(state.json["data"]["control_generation"], 1)
        native_authorization = self.browser.authorize_host_input(self.identity, self.session_id, 1)
        self.assertTrue(native_authorization["authorized"])

        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off = self.request(
                "post", f"/api/browser-sessions/{self.session_id}/control",
                {"mode": "assistant", "base_version": 1},
            )
        self.assertEqual(handed_off.status_code, 200, handed_off.json)
        self.assertEqual(handed_off.json["data"]["control_mode"], "assistant")
        self.assertEqual(handed_off.json["data"]["control_generation"], 2)
        self.assertEqual(handed_off.json["data"]["version"], 2)
        with self.assertRaises(BrowserError) as native_input_denied:
            self.browser.authorize_host_input(self.identity, self.session_id, 2)
        self.assertEqual(native_input_denied.exception.code, "BROWSER_CONTROL_MODE_CONFLICT")

        self.browser.url_validator = lambda url: url
        with patch.object(self.browser.runtime, "navigate", side_effect=AssertionError("manual route must not run while assistant owns control")) as navigate:
            human_route = self.request(
                "post", f"/api/browser-sessions/{self.session_id}/navigate",
                {"base_version": 2, "page_revision": 0, "url": "https://example.com/"},
                key="human-route-is-fenced-by-control-mode",
            )
        self.assertEqual(human_route.status_code, 409, human_route.json)
        self.assertEqual(human_route.json["error"]["code"], "BROWSER_CONTROL_MODE_CONFLICT")
        navigate.assert_not_called()

        arbitrary_actor = self.request(
            "post", f"/api/browser-sessions/{self.session_id}/navigate",
            {"base_version": 2, "page_revision": 0, "url": "https://example.com", "actor": "user"},
            key="actor-field-must-not-be-client-controlled",
        )
        self.assertEqual(arbitrary_actor.status_code, 400, arbitrary_actor.json)
        self.assertEqual(arbitrary_actor.json["error"]["code"], "BROWSER_NAVIGATION_INVALID")

    def test_bind_unassociated_browser_to_work_through_http_and_reopen_metadata(self):
        session_id = self.insert_browser_session(self.identity, None)
        path = f"/api/browser-sessions/{session_id}/work"
        bound = self.request("post", path, {"work_id": self.work["id"], "base_version": 1})
        self.assertEqual(bound.status_code, 200, bound.json)
        self.assertEqual(bound.json["data"]["work_id"], self.work["id"])
        self.assertEqual(bound.json["data"]["version"], 2)
        self.assertEqual(bound.json["data"]["control_generation"], 2)
        reopened = self.request("get", f"/api/browser-sessions/{session_id}/control")
        self.assertEqual(reopened.json["data"]["work_id"], self.work["id"])
        repeated = self.request("post", path, {"work_id": self.work["id"], "base_version": 1})
        self.assertEqual(repeated.status_code, 200, repeated.json)
        self.assertEqual(repeated.json["data"]["version"], 2)
        other_work = self.request("post", "/api/works", {"title": "Separate work", "goal": "Keep scope separate"}).json["data"]
        moved = self.request("post", path, {"work_id": other_work["id"], "base_version": 2})
        self.assertEqual(moved.status_code, 409, moved.json)
        self.assertEqual(moved.json["error"]["code"], "BROWSER_WORK_SCOPE_MISMATCH")
        peer_token, _peer = self.create_peer()
        denied = self.request("post", path, {"work_id": self.work["id"], "base_version": 2}, token=peer_token)
        self.assertEqual(denied.status_code, 404, denied.json)

    def test_model_page_read_is_denied_while_manual(self):
        with patch.object(self.browser.runtime, "read_page", side_effect=AssertionError("must be denied before runtime")) as read:
            with self.assertRaises(BrowserError) as caught:
                self.browser.read_page(
                    self.identity, self.session_id, {"base_version": 1},
                    actor="assistant", expected_control_generation=1,
                )
        self.assertEqual(caught.exception.code, "BROWSER_CONTROL_MODE_CONFLICT")
        read.assert_not_called()

    def test_user_takeover_invalidates_inflight_assistant_effect_without_worker_wait(self):
        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off = self.browser.set_control(
                self.identity, self.session_id, {"mode": "assistant", "base_version": 1},
            )
        operation_id, reservation, replay = self.browser._reserve(
            self.identity, self.session_id, "navigate", "assistant-navigation-key",
            {"url": "https://example.com/"}, handed_off["version"],
            actor="assistant", expected_control_generation=handed_off["control_generation"],
        )
        self.assertIsNone(replay)

        with patch.object(self.browser.runtime, "is_open", side_effect=AssertionError("takeover must not wait for the worker")) as is_open:
            user_state = self.browser.set_control(
                self.identity, self.session_id,
                {"mode": "user", "base_version": handed_off["version"]},
            )
        is_open.assert_not_called()
        self.assertEqual(user_state["control_mode"], "user")
        self.assertEqual(user_state["control_generation"], handed_off["control_generation"] + 1)
        self.assertIsNone(self.store.row(
            "SELECT operation_id FROM browser_sessions WHERE id = ?", (self.session_id,),
        )["operation_id"])
        operation = self.store.row(
            "SELECT status, error_code FROM browser_session_operations WHERE id = ?", (operation_id,),
        )
        self.assertEqual(operation["status"], "unknown")
        self.assertEqual(operation["error_code"], "BROWSER_OPERATION_STATE_UNKNOWN")

        with self.assertRaises(BrowserError) as caught:
            self.browser._complete_operation(
                self.identity, reservation, operation_id, "navigated",
                snapshot={"url": "https://example.com/", "title": "Example", "page_revision": 1},
                storage_payload=None, result={"url": "https://example.com/"},
            )
        self.assertEqual(caught.exception.code, "BROWSER_OPERATION_UNKNOWN")

        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off_again = self.browser.set_control(
                self.identity, self.session_id,
                {"mode": "assistant", "base_version": user_state["version"]},
            )
        with self.assertRaises(BrowserError) as unreconciled:
            self.browser._reserve(
                self.identity, self.session_id, "navigate", "new-assistant-navigation-key",
                {"url": "https://example.com/next"}, handed_off_again["version"],
                actor="assistant", expected_control_generation=handed_off_again["control_generation"],
            )
        self.assertEqual(unreconciled.exception.code, "BROWSER_OPERATION_UNKNOWN")

        reconciled_snapshot = {
            "page_id": "current-page", "url": "https://example.com/", "title": "Example",
            "browser_version": "fixture", "page_revision": 0,
        }
        with patch.object(self.browser.runtime, "is_open", return_value=True), \
             patch.object(self.browser.runtime, "read_page", return_value=reconciled_snapshot), \
             patch.object(self.browser.runtime, "storage_payload", return_value={}):
            reconciliation = self.browser.reconcile_session(
                self.identity, self.session_id, {"base_version": handed_off_again["version"]},
                "assistant-reconcile-after-takeover", actor="assistant",
                expected_control_generation=handed_off_again["control_generation"],
            )
        self.assertEqual(reconciliation["previous_operation"]["status"], "unknown")
        next_operation, next_row, replay = self.browser._reserve(
            self.identity, self.session_id, "navigate", "new-assistant-navigation-key",
            {"url": "https://example.com/next"}, reconciliation["version"],
            actor="assistant", expected_control_generation=reconciliation["control_generation"],
        )
        self.assertIsNone(replay)
        self.browser._fail_operation(self.identity, next_row, next_operation, "TEST_RELEASE", uncertain=False)

    def test_peer_cannot_take_over_another_users_browser_session(self):
        peer_token, _peer_identity = self.create_peer()
        response = self.request(
            "post", f"/api/browser-sessions/{self.session_id}/control",
            {"mode": "assistant", "base_version": 1}, token=peer_token,
        )
        self.assertEqual(response.status_code, 404, response.json)

    def test_work_revocation_still_allows_takeover_and_cleanup_but_denies_host_input(self):
        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off = self.browser.set_control(
                self.identity, self.session_id, {"mode": "assistant", "base_version": 1},
            )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE works SET status = 'archived', version = version + 1 WHERE id = ?",
                (self.work["id"],),
            )

        user_state = self.browser.set_control(
            self.identity, self.session_id, {"mode": "user", "base_version": handed_off["version"]},
        )
        self.assertEqual(user_state["control_mode"], "user")
        with self.assertRaises(ApiError) as denied:
            self.browser.authorize_host_input(
                self.identity, self.session_id, user_state["control_generation"],
            )
        self.assertEqual(denied.exception.code, "WORK_ARCHIVED")

        with patch.object(self.browser.runtime, "close"):
            closed = self.browser.close_session(
                self.identity, self.session_id, {"base_version": user_state["version"]},
                "close-after-work-revocation",
            )
        self.assertEqual(closed["status"], "closed")

    def test_browser_tool_rejects_same_owner_session_from_another_work(self):
        self.assert_browser_tools_registered()
        other_work = self.request("post", "/api/works", {
            "title": "Separate browser work", "goal": "Keep source context isolated",
        }).json["data"]
        other_session_id = self.insert_browser_session(self.identity, other_work["id"])
        context = self.tool_context()
        with self.assertRaises(IntelligenceError) as caught:
            self.intelligence.tools.invoke(
                self.identity, "browser.page.read",
                {"browser_session_id": other_session_id, "base_version": 1}, context,
            )
        self.assertEqual(caught.exception.code, "BROWSER_SESSION_NOT_FOUND")

    def test_tool_bindings_share_acquisition_and_monitoring_families_and_queue_current_work(self):
        bindings = self.assert_browser_tools_registered()
        self.assertEqual(len(bindings), 9)
        catalog = self.intelligence.tools.catalog()
        entries = {item["tool"]: item for item in catalog["tools"]}
        self.assertEqual(entries["browser.page.capture"]["owner"], "Acquisition")
        self.assertEqual(entries["browser.page.capture"]["effect"], "draft_write")
        self.assertEqual(entries["acquisition.enqueue_url"]["effect"], "draft_write")
        self.assertEqual(entries["monitoring.signals"]["owner"], "Monitoring")
        self.assertFalse(any("submit" in name or "form" in name or "payment" in name for name in entries))

        no_write_context = self.tool_context(artifact_write_intent=False)
        with self.assertRaises(IntelligenceError) as write_denied:
            self.intelligence.tools.invoke(
                self.identity, "acquisition.enqueue_url", {"url": "https://example.com/blocked"}, no_write_context,
            )
        self.assertEqual(write_denied.exception.code, "INTELLIGENCE_TOOL_WRITE_NOT_REQUESTED")

        context = self.tool_context(artifact_write_intent=True)
        queued = self.intelligence.tools.invoke(
            self.identity, "acquisition.enqueue_url", {"url": "https://example.com/report"}, context,
        )
        job = queued["result"]
        self.assertEqual(job["kind"], "acquire_url")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["work_id"], self.work["id"])
        stored = self.store.row("SELECT work_id, user_id FROM jobs WHERE id = ?", (job["id"],))
        self.assertEqual(stored["work_id"], self.work["id"])
        self.assertEqual(stored["user_id"], self.identity["user"]["id"])
        monitor_list = self.intelligence.tools.invoke(self.identity, "monitoring.list", {}, context)["result"]
        monitor_signals = self.intelligence.tools.invoke(self.identity, "monitoring.signals", {}, context)["result"]
        self.assertEqual(monitor_list["work_id"], self.work["id"])
        self.assertEqual(monitor_list["monitors"], [])
        self.assertEqual(monitor_signals["work_id"], self.work["id"])
        self.assertEqual(monitor_signals["signals"], [])

        monitor_id = str(uuid.uuid4())
        source_id = str(uuid.uuid4())
        now = utc_now()
        columns = {row["name"] for row in self.store.rows("PRAGMA table_info(monitor_rules)")}
        fields = [
            "id", "workspace_id", "owner_user_id", "title", "scope", "work_id",
            "follow_source_links", "keywords_json", "exclude_keywords_json", "frequency_seconds",
            "stop_at", "per_run_limit", "status", "version", "next_run_at", "created_at", "updated_at",
        ]
        values = {
            "id": monitor_id, "workspace_id": self.identity["workspace"]["id"],
            "owner_user_id": self.identity["user"]["id"], "title": "Release watch",
            "scope": "public_sources", "work_id": self.work["id"], "follow_source_links": 0,
            "keywords_json": "[]", "exclude_keywords_json": "[]", "frequency_seconds": 3600,
            "stop_at": None, "per_run_limit": 5, "status": "paused", "version": 1,
            "next_run_at": None, "created_at": now, "updated_at": now,
        }
        if "subject_id" in columns:
            fields.insert(5, "subject_id")
            values["subject_id"] = None
        with self.store.transaction() as connection:
            connection.execute(
                f"INSERT INTO monitor_rules({', '.join(fields)}) VALUES({', '.join('?' for _ in fields)})",
                [values[field] for field in fields],
            )
            connection.execute(
                "INSERT INTO monitor_sources(id, monitor_id, workspace_id, url, label, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (source_id, monitor_id, self.identity["workspace"]["id"], "https://example.com/release", "Release feed", now),
            )
        monitor_list = self.intelligence.tools.invoke(
            self.identity, "monitoring.list", {}, context,
        )["result"]
        self.assertEqual(monitor_list["monitors"][0]["id"], monitor_id)
        self.assertEqual(monitor_list["monitors"][0]["title"], "Release watch")
        self.assertEqual(monitor_list["monitors"][0]["source_count"], 1)

        material_id = str(uuid.uuid4())
        material_text = "Release note for the monitored Work."
        material_sha256 = hashlib.sha256(material_text.encode("utf-8")).hexdigest()
        run_id, run_source_id, observation_id, signal_id = [str(uuid.uuid4()) for _ in range(4)]
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO materials(
                       id, workspace_id, owner_user_id, work_id, title, filename, format, status, reason,
                       text, sha256, relative_path, size_bytes, source_url, etag, last_modified,
                       source_json, source_text, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'txt', 'ready', NULL, ?, ?, ?, ?, ?, NULL, NULL, '{}', ?, ?)""",
                (material_id, self.identity["workspace"]["id"], self.identity["user"]["id"],
                 self.work["id"], "Release note", "release.txt", material_text, material_sha256,
                 f"objects/{self.identity['workspace']['id']}/materials/{material_id}/original",
                 len(material_text.encode("utf-8")), "https://example.com/release", material_text, now),
            )
            connection.execute(
                "INSERT INTO monitor_versions(monitor_id, workspace_id, version, operation, status, configuration_json, created_at, created_by) VALUES(?, ?, 1, 'created', 'paused', ?, ?, ?)",
                (monitor_id, self.identity["workspace"]["id"], json.dumps({"work_id": self.work["id"], "subject_id": None}), now, self.identity["user"]["id"]),
            )
            connection.execute(
                "INSERT INTO monitor_runs(id, monitor_id, workspace_id, config_version, kind, trigger_key, trigger_hash, status, source_count, selected_count, not_covered_count, created_at) VALUES(?, ?, ?, 1, 'manual', 'fixture-run', 'fixture-hash', 'completed', 1, 1, 0, ?)",
                (run_id, monitor_id, self.identity["workspace"]["id"], now),
            )
            connection.execute(
                """INSERT INTO monitor_run_sources(
                       id, run_id, monitor_id, workspace_id, source_id, job_id, config_version,
                       lifecycle_status, outcome, material_id, sha256, captured_at, changed_from_previous,
                       created_at, completed_at
                   ) VALUES(?, ?, ?, ?, ?, NULL, 1, 'completed', 'signal', ?, ?, ?, 1, ?, ?)""",
                (run_source_id, run_id, monitor_id, self.identity["workspace"]["id"], source_id,
                 material_id, material_sha256, now, now, now),
            )
            connection.execute(
                """INSERT INTO monitor_observations(
                       id, monitor_id, workspace_id, source_id, run_id, run_source_id, material_id,
                       sha256, captured_at, content_status, changed_from_previous, config_version, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 1, 1, ?)""",
                (observation_id, monitor_id, self.identity["workspace"]["id"], source_id, run_id,
                 run_source_id, material_id, material_sha256, now, now),
            )
            connection.execute(
                """INSERT INTO monitor_signals(
                       id, monitor_id, workspace_id, source_id, run_id, run_source_id, observation_id,
                       material_id, config_version, title, summary, matched_keywords_json, evidence_json,
                       current_sha256, captured_at, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, 'Release changed', 'Signal summary', '[]', '[]', ?, ?, ?)""",
                (signal_id, monitor_id, self.identity["workspace"]["id"], source_id, run_id,
                 run_source_id, observation_id, material_id, material_sha256, now, now),
            )
        signals = self.intelligence.tools.invoke(
            self.identity, "monitoring.signals", {}, context,
        )["result"]
        self.assertEqual(signals["signals"][0]["id"], signal_id)
        self.assertEqual(signals["signals"][0]["source_url"], "https://example.com/release")
        self.assertIn(material_id, context.allowed_material_ids)
        self.assertIn(("material", material_id), {
            (source.get("kind"), source.get("material_id")) for source in context.source_refs
        })

        other_work = self.request("post", "/api/works", {
            "title": "Unselected private comparison", "goal": "Keep this source in its own work",
        }).json["data"]
        other_material = dict(self.store.row("SELECT * FROM materials WHERE id=?", (material_id,)))
        other_material.update(id=str(uuid.uuid4()), work_id=other_work["id"])
        with self.store.transaction() as connection:
            columns = list(other_material)
            connection.execute(
                f"INSERT INTO materials ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(other_material[column] for column in columns),
            )
            connection.execute("UPDATE monitor_signals SET evidence_json=? WHERE id=?", (
                json.dumps([{"before": {"material_id": other_material["id"], "text": "Unselected old source"}}]), signal_id,
            ))
        private_history_context = self.tool_context()
        private_history_context.operation_scope = "monitor-history-" + uuid.uuid4().hex
        filtered = self.intelligence.tools.invoke(self.identity, "monitoring.signals", {}, private_history_context)["result"]
        self.assertEqual(filtered["signals"], [])
        self.assertNotIn(other_material["id"], private_history_context.allowed_material_ids)

        workless = self.intelligence.prepare_context(
            self.identity, "Read a source.", include_memory=False, artifact_write_intent=True,
        )
        workless_context = self.intelligence.create_tool_context(
            workless, caller="model", operation_scope="workless-tool-test",
        )
        with self.assertRaises(IntelligenceError) as caught:
            self.intelligence.tools.invoke(
                self.identity, "acquisition.enqueue_url", {"url": "https://example.com/other"}, workless_context,
            )
        self.assertEqual(getattr(caught.exception, "code", None), "INTELLIGENCE_TOOL_SCOPE_INVALID")

    def test_acquisition_tool_rechecks_revoked_auth_session_before_queueing(self):
        self.assert_browser_tools_registered()
        context = self.tool_context(artifact_write_intent=True)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ?",
                (utc_now(), self.identity["session_id"]),
            )
        with self.assertRaises(IntelligenceError) as caught:
            self.intelligence.tools.invoke(
                self.identity, "acquisition.enqueue_url", {"url": "https://example.com/revoked"}, context,
            )
        self.assertEqual(caught.exception.code, "UNAUTHENTICATED")
        self.assertIsNone(self.store.row("SELECT id FROM jobs WHERE kind = 'acquire_url'"))

    def test_reader_read_refreshes_page_revision_after_manual_navigation_without_body(self):
        self.assert_browser_tools_registered()
        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off = self.browser.set_control(
                self.identity, self.session_id, {"mode": "assistant", "base_version": 1},
            )
        context = self.tool_context()
        listed = self.intelligence.tools.invoke(self.identity, "browser.sessions.list", {}, context)["result"]
        self.assertEqual(listed[0]["id"], self.session_id)
        self.assertEqual(listed[0]["control_mode"], "assistant")
        self.assertEqual(listed[0]["control_generation"], handed_off["control_generation"])

        runtime_snapshot = {
            "page_id": "fixture-page", "url": "https://example.com/", "title": "Example",
            "browser_version": "fixture", "page_revision": 5, "text": "private fixture body",
            "text_sha256": "fixture-hash", "text_characters": 20, "text_truncated": False,
            "blocked_request_count": 0, "scope": "main_frame_visible_body_text",
            "content_trust": "UNTRUSTED_WEB_CONTENT", "limitations": [],
            "redacted_query_keys": [], "login_state": "NOT_VERIFIED",
        }
        reader_context = {
            "session_id": self.session_id, "page_id": "fixture-page", "page_revision": 5,
            "url": "https://example.com/", "title": "Example", "text": "private fixture body",
            "redacted_query_keys": ["access_token"], "scope": "main_frame_and_visible_iframes",
            "snapshot_version": {"sha256": "reader-snapshot-hash"},
            "frames": [{
                "frame_id": "main", "parent_frame_id": None,
                "url": "https://example.com/?access_token=READER_QUERY_SECRET",
                "title": "Example", "status": "read", "frame_sha256": "a" * 64,
                "text": "private fixture body", "text_sha256": "frame-text-hash",
                "blocks": [{"block_id": "main/body/0", "text": "private fixture body", "tag": "p"}],
                "links": [{
                    "url": "https://example.com/next?signature=READER_LINK_SECRET",
                    "text": "Read next", "title": "",
                }],
                "controls": [{"control_id": "main/body/input", "type": "password",
                              "label": "Password", "value": "PASSWORD_VALUE_NEVER_RETURN"}],
                "selection": {"quote": "SECRET_SELECTION_NEVER_RETURN"},
                "coverage": {"status": "complete"},
            }],
            "links": [{
                "frame_id": "main", "url": "https://example.com/next?signature=READER_LINK_SECRET",
                "text": "Read next", "title": "",
            }],
            "controls": [{"control_id": "main/body/input", "type": "password",
                           "label": "Password", "value": "PASSWORD_VALUE_NEVER_RETURN"}],
            "coverage": {"status": "complete", "limitations": ["Bounded Reader context."]},
        }
        with patch.object(self.browser.runtime, "read_page", return_value=runtime_snapshot), \
             patch("server.oevira.browser_host_reader.read_browser_context", return_value=reader_context), \
             patch.object(self.browser, "read_page", wraps=self.browser.read_page) as page_read:
            result = self.intelligence.tools.invoke(
                self.identity, "browser.page.read",
                {"browser_session_id": self.session_id, "base_version": handed_off["version"]}, context,
            )["result"]
        self.assertEqual(result["url"], "https://example.com/?access_token=%5Bredacted%5D")
        self.assertEqual(result["page_revision"], 5)
        self.assertEqual(result["redacted_query_keys"], ["access_token"])
        self.assertEqual(result["frames"][0]["frame_id"], "main")
        self.assertEqual(
            result["links"][0]["url"],
            "https://example.com/next?signature=%5Bredacted%5D",
        )
        self.assertNotIn("text", result["frames"][0]["blocks"][0])
        serialized = json_dumps(result)
        for private_value in (
            "private fixture body", "PASSWORD_VALUE_NEVER_RETURN", "SECRET_SELECTION_NEVER_RETURN",
            "READER_QUERY_SECRET", "READER_LINK_SECRET",
        ):
            self.assertNotIn(private_value, serialized)
        read_requests = [call.args[2] for call in page_read.call_args_list]
        self.assertEqual(read_requests, [], "Reader owner reads the current bounded context without legacy raw body reads")

    def test_reader_capture_registers_fresh_main_and_selected_iframe_materials(self):
        self.assert_browser_tools_registered()
        self.browser.protector = _TestBrowserStateProtector()
        with patch.object(self.browser.runtime, "is_open", return_value=True):
            handed_off = self.browser.set_control(
                self.identity, self.session_id, {"mode": "assistant", "base_version": 1},
            )
        prepared = self.intelligence.prepare_context(
            self.identity, "Read the public page and selected iframe for this Work.",
            work_id=self.work["id"], include_memory=False, artifact_write_intent=True,
        )
        context = self.intelligence.create_tool_context(
            prepared, caller="model", operation_scope="reader-capture-test-turn",
        )
        runtime_snapshot = {
            "page_id": "reader-page", "url": "https://example.com/article", "title": "Article",
            "browser_version": "fixture", "page_revision": 5, "text": "LEGACY_RAW_PAGE_BODY",
            "text_sha256": "legacy-page-hash", "text_characters": 20, "text_truncated": False,
            "blocked_request_count": 0, "scope": "main_frame_visible_body_text",
            "content_trust": "UNTRUSTED_WEB_CONTENT", "limitations": ["Legacy body must not be captured."],
            "redacted_query_keys": ["access_token"], "login_state": "NOT_VERIFIED",
        }
        main_hash = "a" * 64
        iframe_hash = "b" * 64
        reader_context = {
            "session_id": self.session_id, "page_id": "reader-page", "page_revision": 5,
            "url": "https://example.com/article", "title": "Article",
            "text": "PRIVATE_MAIN_TEXT_NOT_RETURNED", "redacted_query_keys": ["access_token"],
            "scope": "main_frame_and_visible_iframes",
            "snapshot_version": {"sha256": "reader-snapshot"},
            "frames": [
                {
                    "frame_id": "main", "parent_frame_id": None,
                    "url": "https://example.com/article", "redacted_query_keys": ["access_token"],
                    "title": "Article", "status": "read", "frame_sha256": main_hash,
                    "text": "PRIVATE_MAIN_TEXT_NOT_RETURNED", "text_sha256": "main-text-hash",
                    "blocks": [{"block_id": "main/body/0", "text": "PRIVATE_MAIN_TEXT_NOT_RETURNED", "tag": "p"}],
                    "links": [], "controls": [{
                        "control_id": "main/body/input0", "type": "password", "label": "Password",
                        "value": "PASSWORD_VALUE_NOT_RETURNED",
                    }],
                    "selection": {"quote": "PRIVATE_SELECTION_NOT_RETURNED"},
                    "coverage": {"status": "complete"},
                },
                {
                    "frame_id": "frame:1", "parent_frame_id": "main",
                    "url": "https://example.com/embed", "redacted_query_keys": ["access_token"],
                    "title": "Embedded article", "status": "read", "frame_sha256": iframe_hash,
                    "text": "PRIVATE_IFRAME_TEXT_NOT_RETURNED", "text_sha256": "iframe-text-hash",
                    "blocks": [{
                        "block_id": "frame:1/body/0", "text": "PRIVATE_IFRAME_TEXT_NOT_RETURNED",
                        "tag": "p", "start_offset": 0, "end_offset": 25,
                    }],
                    "links": [], "controls": [], "selection": {"quote": "PRIVATE_IFRAME_TEXT_NOT_RETURNED"},
                    "coverage": {"status": "complete"},
                },
            ],
            "links": [], "controls": [],
            "coverage": {"status": "complete", "limitations": ["Reader excludes form values."]},
        }
        capture_calls = []

        def capture_reader(_runtime, _engine, _session_id, page_id, frame_id, frame_sha256,
                           block_id=None, expected_page_revision=None, before_dispatch=None):
            if before_dispatch is not None:
                before_dispatch()
            capture_calls.append({
                "page_id": page_id, "frame_id": frame_id, "frame_sha256": frame_sha256,
                "block_id": block_id, "expected_page_revision": expected_page_revision,
            })
            is_iframe = frame_id == "frame:1"
            text = "Sanitized iframe block text." if block_id else (
                "Sanitized main Reader frame text." if not is_iframe else "Sanitized iframe frame text."
            )
            frame_url = "https://example.com/embed" if is_iframe else "https://example.com/article"
            frame_title = "Embedded article" if is_iframe else "Article"
            return {
                "snapshot": {
                    "session_id": _session_id, "engine": _engine, "page_id": page_id,
                    "page_revision": 5, "url": "https://example.com/article", "title": "Article",
                    "source_url": frame_url, "source_title": frame_title,
                    "frame_id": frame_id, "frame_sha256": frame_sha256,
                    "snapshot_sha256": "reader-snapshot", "page_text_sha256": "reader-frame-hash",
                    "text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "text_characters": len(text), "text_truncated": False,
                    "scope": "reader_block" if block_id else "reader_frame",
                    "capture_scope": "reader_block" if block_id else "reader_frame",
                    "selection_context": None, "coverage": {"status": "complete"},
                    "content_trust": "UNTRUSTED_WEB_CONTENT",
                    "redacted_query_keys": ["access_token"],
                    "limitations": ["Reader does not expose form values or browser storage."],
                },
                "storage_payload": {},
            }

        with patch.object(self.browser.runtime, "read_page", return_value=runtime_snapshot), \
             patch("server.oevira.browser_host_reader.read_browser_context", return_value=reader_context), \
             patch("server.oevira.browser_host_reader.capture_reader_context", side_effect=capture_reader):
            with self.assertRaises(IntelligenceError) as client_text_denied:
                self.intelligence.tools.invoke(
                    self.identity, "browser.page.capture",
                    {
                        "browser_session_id": self.session_id,
                        "base_version": handed_off["version"],
                        "text": "CLIENT_SUPPLIED_TEXT_MUST_NOT_BE_CAPTURED",
                    },
                    context,
                )
            self.assertEqual(client_text_denied.exception.code, "INTELLIGENCE_TOOL_INPUT_INVALID")
            main_result = self.intelligence.tools.invoke(
                self.identity, "browser.page.capture",
                {"browser_session_id": self.session_id, "base_version": handed_off["version"]},
                context,
            )["result"]
            iframe_result = self.intelligence.tools.invoke(
                self.identity, "browser.page.capture",
                {
                    "browser_session_id": self.session_id,
                    "base_version": main_result["browser"]["version"],
                    "frame_id": "frame:1", "frame_sha256": iframe_hash,
                    "block_id": "frame:1/body/0",
                },
                context,
            )["result"]
            with self.assertRaises(IntelligenceError) as stale_frame:
                self.intelligence.tools.invoke(
                    self.identity, "browser.page.capture",
                    {
                        "browser_session_id": self.session_id,
                        "base_version": iframe_result["browser"]["version"],
                        "frame_id": "frame:1", "frame_sha256": "c" * 64,
                        "block_id": "frame:1/body/0",
                    },
                    context,
                )
            self.assertEqual(stale_frame.exception.code, "BROWSER_PAGE_CHANGED")
            self.assertEqual(len(capture_calls), 2)

        self.assertEqual(capture_calls[0]["frame_id"], "main")
        self.assertIsNone(capture_calls[0]["block_id"])
        self.assertEqual(capture_calls[0]["expected_page_revision"], 5)
        self.assertEqual(capture_calls[1]["frame_id"], "frame:1")
        self.assertEqual(capture_calls[1]["frame_sha256"], iframe_hash)
        self.assertEqual(capture_calls[1]["block_id"], "frame:1/body/0")
        self.assertEqual(main_result["material"]["text"], "Sanitized main Reader frame text.")
        self.assertEqual(iframe_result["material"]["text"], "Sanitized iframe block text.")
        for private_value in (
            "LEGACY_RAW_PAGE_BODY", "PRIVATE_MAIN_TEXT_NOT_RETURNED",
            "PRIVATE_IFRAME_TEXT_NOT_RETURNED", "PASSWORD_VALUE_NOT_RETURNED",
        ):
            self.assertNotIn(private_value, json_dumps(main_result))
            self.assertNotIn(private_value, json_dumps(iframe_result))

        extended = self.intelligence.extend_context(prepared, context)
        self.intelligence._revalidate_prepared_context(extended)
        self.assertEqual(len([ref for ref in extended.source_refs if ref.get("kind") == "material"]), 2)
        self.assertTrue(any(ref.get("kind") == "material_segment" for ref in extended.source_refs))

        selected = ExplicitModelSelection(
            route="owned", selection_id="isolated-owned-selection", revision="fixture-revision",
            model_id="fixture-model", recipient_id="owned-local", selected_by_user=True,
        )
        source_admissions = []
        execution_events = []

        def authorize_sources(identity, selection, source_refs, *, purpose):
            source_admissions.append(list(source_refs))
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z")
            return ModelInputAuthorization(
                allowed=True, workspace_id=identity["workspace"]["id"],
                user_id=identity["user"]["id"], selection_id=selection.selection_id,
                selection_revision=selection.revision, route=selection.route,
                model_id=selection.model_id, source_ref_sha256=_digest(list(source_refs)),
                purpose=purpose, recipient=selection.recipient_id,
                authorization_id="isolated-source-admission", expires_at=expiry,
            )

        def owned_runner(selection, payload, *, is_cancelled=None):
            execution_events.append(payload)
            return {"status": "completed", "text": "The selected Reader Materials were admitted.", "citations": []}

        self.intelligence.model_selection_resolver = lambda _identity, _selection_id: selected
        self.intelligence.source_use_authorizer = authorize_sources
        self.intelligence.owned_inference_runner = owned_runner
        model_result = self.intelligence.run_model(extended, selection_id=selected.selection_id)
        self.assertEqual(model_result["status"], "completed")
        self.assertEqual(len(source_admissions), 2)
        for admitted_refs in source_admissions:
            admitted_material_ids = {
                ref.get("material_id") for ref in admitted_refs if ref.get("kind") == "material"
            }
            self.assertEqual(admitted_material_ids, {main_result["material"]["id"], iframe_result["material"]["id"]})
        self.assertEqual(len(execution_events), 1)
        self.assertTrue(any(
            item["result"].get("material", {}).get("text") == "Sanitized iframe block text."
            for item in execution_events[0]["tool_results"]
        ))

    def test_browser_schema_control_migration_is_idempotent(self):
        directory = tempfile.TemporaryDirectory(prefix="control-schema-", dir=self.runtime_root)
        try:
            store = Store(directory.name)
            with store.transaction() as connection:
                connection.execute(
                    "CREATE TABLE browser_sessions(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                    "owner_user_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
            install_browser_schema(store)
            install_browser_schema(store)
            columns = {row["name"] for row in store.rows("PRAGMA table_info(browser_sessions)")}
            self.assertIn("control_mode", columns)
            self.assertIn("control_generation", columns)
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO browser_sessions(id, workspace_id, owner_user_id, status, updated_at) "
                    "VALUES('legacy-session', 'legacy-workspace', 'legacy-owner', 'open', ?)",
                    (utc_now(),),
                )
            migrated = store.row(
                "SELECT control_mode, control_generation FROM browser_sessions WHERE id = 'legacy-session'"
            )
            self.assertEqual(migrated["control_mode"], "user")
            self.assertEqual(migrated["control_generation"], 1)
        finally:
            directory.cleanup()


def json_dumps(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
