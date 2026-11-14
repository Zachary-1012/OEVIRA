from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import sqlite3
import secrets
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from server.oevira import create_app
from server.oevira.api import ApiError, _resource_path
from server.oevira.acquisition import AcquisitionError
from server.oevira.auth import _digest, issue_local_capability, utc_now
from server.oevira.monitoring import install_monitoring_schema, tick_monitoring
from server.oevira.store import Store
from server.oevira.worker import run_forever, run_once


BASE_URL = "http://127.0.0.1:8317"
ORIGIN = BASE_URL
RUNTIME_TEST_ROOT = Path(__file__).resolve().parents[2] / ".runtime" / "monitoring-tests"


def _plus_seconds(value: str, seconds: int) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-monitoring-", dir=RUNTIME_TEST_ROOT)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, expected_port=8317)
        self.store = self.app.extensions["oevira.store"]
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "监测测试"},
            headers={"Origin": ORIGIN},
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

    def config(self, *urls: str, **overrides) -> dict:
        result = {
            "title": "新品公开页面关注",
            "scope": "品牌产品公告页",
            "sources": [{"url": url, "label": f"来源 {index + 1}"} for index, url in enumerate(urls or ("https://news.example/brand",))],
            "keywords": ["release"],
            "exclude_keywords": [],
            "frequency_seconds": 3600,
            "stop_at": None,
            "per_run_limit": len(urls or ("https://news.example/brand",)),
        }
        result.update(overrides)
        return result

    def create_monitor(self, *urls: str, **overrides) -> dict:
        response = self.request(
            "post", "/api/monitors", json=self.config(*urls, **overrides),
            headers={**self.auth_headers(), "Idempotency-Key": "create-" + uuid.uuid4().hex},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_subject(self, *, name: str = "隔离测试品牌") -> dict:
        response = self.request(
            "post", "/api/business/subjects", json={"kind": "brand", "name": name},
            headers={**self.auth_headers(), "Idempotency-Key": "subject-" + uuid.uuid4().hex},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]["subject"]

    def create_work(self, *, title: str = "监测关联工作") -> dict:
        response = self.request(
            "post", "/api/works", json={"title": title, "goal": "复核获准来源变化"},
            headers={**self.auth_headers(), "Idempotency-Key": "monitor-work-" + uuid.uuid4().hex},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_other_workspace(self) -> str:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "隔离用户", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "隔离工作区", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, user_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now, expiry),
            )
        return token

    def create_workspace_member(self, *, role: str = "editor") -> tuple[str, str]:
        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "同工作区客户外成员", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, ?, 1, ?)",
                (self.identity["workspace"]["id"], user_id, role, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), _digest(token), user_id, self.identity["workspace"]["id"], now, expiry),
            )
        return user_id, token

    def check(self, monitor: dict, key: str | None = None):
        return self.request(
            "post", f"/api/monitors/{monitor['id']}/check", json={"base_version": monitor["version"]},
            headers={**self.auth_headers(), "Idempotency-Key": key or uuid.uuid4().hex},
        )

    def acquisition(self, content: bytes, text: str, *, etag: str | None = None) -> dict:
        return {
            "status": "ready", "url": "https://news.example/brand", "requested_url": "https://news.example/brand",
            "title": "新品公告", "mime_type": "text/html; charset=utf-8", "content": content, "text": text,
            "captured_at": datetime.now(timezone.utc).isoformat(), "published_at": None, "published_at_origin": None, "author": None,
            "etag": etag, "last_modified": None, "acquisition_method": "public_http",
            "limitations": [], "reason": None, "extraction_scope": "static_document_body",
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def test_store_migrates_v3_without_losing_work_or_jobs(self) -> None:
        workspace_id = self.identity["workspace"]["id"]
        user_id = self.identity["user"]["id"]
        work_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO works(id, workspace_id, owner_user_id, title, goal, brief_json, status, version, created_at, updated_at) VALUES(?, ?, ?, '保留的工作', '迁移后仍可读取', '{}', 'active', 1, ?, ?)",
                (work_id, workspace_id, user_id, now, now),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id, version, title, goal, brief_json, status, created_at, created_by) VALUES(?, 1, '保留的工作', '迁移后仍可读取', '{}', 'active', ?, ?)",
                (work_id, now, user_id),
            )
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, created_at, updated_at) VALUES(?, ?, ?, ?, 'material_comparison', 'queued', '{}', 'legacy-hash', 'legacy-key', ?, ?)",
                (job_id, workspace_id, user_id, work_id, now, now),
            )

        # Recreate the layout of a v3 installation without touching user data.
        connection = sqlite3.connect(self.store.database)
        try:
            for table in (
                "monitor_signal_works", "monitor_signals", "monitor_observations",
                "monitor_run_sources", "monitor_runs", "monitor_versions",
                "monitor_sources", "monitor_rules",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        finally:
            connection.close()

        migrated = Store(self.root)
        self.assertEqual(migrated.row("PRAGMA user_version")["user_version"], 5)
        self.assertIn("subject_id", {row["name"] for row in migrated.rows("PRAGMA table_info(monitor_rules)")})
        self.assertEqual(
            dict(migrated.row("SELECT id, title, version FROM works WHERE id = ?", (work_id,))),
            {"id": work_id, "title": "保留的工作", "version": 1},
        )
        self.assertEqual(
            dict(migrated.row("SELECT id, work_id, status FROM jobs WHERE id = ?", (job_id,))),
            {"id": job_id, "work_id": work_id, "status": "queued"},
        )
        self.assertEqual(migrated.row("SELECT COUNT(*) AS count FROM monitor_rules")["count"], 0)
        self.assertEqual(Store(self.root).row("PRAGMA user_version")["user_version"], 5)

    def test_store_migrates_v4_monitoring_rules_without_losing_history_or_jobs(self) -> None:
        monitor = self.create_monitor("https://news.example/legacy")
        run_response = self.check(monitor, "legacy-v4-run")
        self.assertEqual(run_response.status_code, 202, run_response.get_data(as_text=True))
        run_id = run_response.json["data"]["id"]
        monitor_id = monitor["id"]

        connection = sqlite3.connect(self.store.database)
        try:
            versions = connection.execute(
                "SELECT version, configuration_json FROM monitor_versions WHERE monitor_id = ? ORDER BY version",
                (monitor_id,),
            ).fetchall()
            for version, value in versions:
                configuration = json.loads(value)
                configuration.pop("subject_id", None)
                configuration.pop("subject", None)
                connection.execute(
                    "UPDATE monitor_versions SET configuration_json = ? WHERE monitor_id = ? AND version = ?",
                    (json.dumps(configuration, ensure_ascii=False, sort_keys=True), monitor_id, version),
                )
            connection.execute("DROP INDEX IF EXISTS monitor_rules_subject_status")
            connection.execute("ALTER TABLE monitor_rules DROP COLUMN subject_id")
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
        finally:
            connection.close()

        migrated = Store(self.root)
        self.assertEqual(migrated.row("PRAGMA user_version")["user_version"], 5)
        self.assertIsNone(migrated.row("SELECT subject_id FROM monitor_rules WHERE id = ?", (monitor_id,))["subject_id"])
        self.assertEqual(migrated.row("SELECT COUNT(*) AS count FROM monitor_versions WHERE monitor_id = ?", (monitor_id,))["count"], 1)
        self.assertEqual(migrated.row("SELECT COUNT(*) AS count FROM monitor_sources WHERE monitor_id = ?", (monitor_id,))["count"], 1)
        self.assertEqual(migrated.row("SELECT COUNT(*) AS count FROM monitor_runs WHERE id = ?", (run_id,))["count"], 1)
        self.assertEqual(migrated.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 1)
        old_configuration = json.loads(migrated.row(
            "SELECT configuration_json FROM monitor_versions WHERE monitor_id = ? AND version = 1", (monitor_id,),
        )["configuration_json"])
        self.assertNotIn("subject_id", old_configuration)

    def test_store_migrates_discovery_kinds_without_losing_existing_provenance(self) -> None:
        monitor = self.create_monitor("https://news.example/discovery-migration-root")
        material = self.request(
            "post", "/api/materials", json={"title": "同源发现证据", "text": "页面发现记录"},
            headers=self.auth_headers(),
        )
        self.assertEqual(material.status_code, 201, material.get_data(as_text=True))
        parent = self.store.row(
            "SELECT id FROM monitor_sources WHERE monitor_id = ? AND url = ?",
            (monitor["id"], "https://news.example/discovery-migration-root"),
        )
        child_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO monitor_sources(id, monitor_id, workspace_id, url, position, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (child_id, monitor["id"], self.identity["workspace"]["id"], "https://news.example/legacy-child", 1, now),
            )
            connection.execute("DROP TABLE monitor_source_discoveries")
            connection.execute(
                """CREATE TABLE monitor_source_discoveries (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL REFERENCES monitor_rules(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    parent_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
                    child_source_id TEXT NOT NULL REFERENCES monitor_sources(id),
                    parent_material_id TEXT NOT NULL REFERENCES materials(id),
                    config_version INTEGER NOT NULL,
                    link_kind TEXT NOT NULL CHECK(link_kind IN ('page_link', 'feed_entry', 'next_page')),
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source_native_id TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    UNIQUE(monitor_id, parent_material_id, url, link_kind, source_native_id, config_version)
                )"""
            )
            connection.execute(
                """INSERT INTO monitor_source_discoveries(
                       id, monitor_id, workspace_id, parent_source_id, child_source_id,
                       parent_material_id, config_version, link_kind, url, title, source_native_id, observed_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'page_link', ?, ?, '', ?)""",
                ("legacy-discovery-edge", monitor["id"], self.identity["workspace"]["id"], parent["id"], child_id,
                 material.json["data"]["id"], monitor["version"], "https://news.example/legacy-child", "Legacy page link", now),
            )

        install_monitoring_schema(self.store)
        preserved = self.store.row("SELECT * FROM monitor_source_discoveries WHERE id = ?", ("legacy-discovery-edge",))
        self.assertEqual(preserved["link_kind"], "page_link")
        self.assertEqual(preserved["child_source_id"], child_id)
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO monitor_source_discoveries(
                       id, monitor_id, workspace_id, parent_source_id, child_source_id,
                       parent_material_id, config_version, link_kind, url, title, source_native_id, observed_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'sitemap_url', ?, ?, '', ?)""",
                ("sitemap-discovery-edge", monitor["id"], self.identity["workspace"]["id"], parent["id"], child_id,
                 material.json["data"]["id"], monitor["version"], "https://news.example/legacy-child", "Site page", now),
            )
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_source_discoveries")["count"], 2)

    def acquire_and_harvest(self, monitor: dict, content: bytes, text: str, *, etag: str | None = None) -> dict:
        queued = self.check(monitor)
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        run = queued.json["data"]
        self.assertEqual(len([item for item in run["sources"] if item["job_id"]]), 1, run)
        with patch("server.oevira.acquisition.acquire_url", return_value=self.acquisition(content, text, etag=etag)):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        return self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]

    def test_create_paused_schedule_opt_in_source_limit_and_cas(self) -> None:
        monitor = self.create_monitor(
            "https://news.example/one", "https://news.example/two", "https://news.example/three",
            per_run_limit=1,
        )
        self.assertEqual(monitor["status"], "paused")
        self.assertEqual(monitor["version"], 1)
        self.assertEqual([item["status"] for item in monitor["sources"]], ["not_checked"] * 3)
        tick_monitoring(self.app)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 0)

        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": 1}, headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        self.assertEqual(enabled.json["data"]["status"], "enabled")
        due = enabled.json["data"]["next_run_at"]
        tick_monitoring(self.app, due)
        runs = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"]
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["coverage"], {"total_sources": 3, "selected_sources": 1, "not_covered": 2, "attempted": 0, "checked": 0, "signals": 0})
        self.assertEqual(sum(item["job_id"] is not None for item in run["sources"]), 1)
        self.assertEqual(sum(item["status"] == "not_covered" for item in run["sources"]), 2)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 1)
        tick_monitoring(self.app, due)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_runs WHERE monitor_id = ?", (monitor["id"],))["count"], 1)

        conflict = self.request(
            "patch", f"/api/monitors/{monitor['id']}", json={"base_version": 1, "frequency_seconds": 7200}, headers=self.auth_headers(),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"]["code"], "VERSION_CONFLICT")

        updated = self.request(
            "patch", f"/api/monitors/{monitor['id']}",
            json={"base_version": enabled.json["data"]["version"], "keywords": ["sale"], "frequency_seconds": 7200},
            headers=self.auth_headers(),
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.json["data"]["version"], enabled.json["data"]["version"] + 1)
        versions = self.request("get", f"/api/monitors/{monitor['id']}/versions", headers=self.auth_headers()).json["data"]
        self.assertEqual([item["version"] for item in versions], [3, 2, 1])
        self.assertEqual(versions[0]["configuration"]["keywords"], ["sale"])
        self.assertEqual(versions[-1]["configuration"]["keywords"], ["release"])

    def test_subject_binding_is_explicit_workspace_scoped_and_versioned(self) -> None:
        subject = self.create_subject(name="显式绑定的虚构品牌")
        bound = self.create_monitor("https://news.example/bound", subject_id=subject["id"])
        self.assertEqual(bound["subject_id"], subject["id"])
        self.assertEqual(bound["subject"]["name"], subject["name"])
        self.assertEqual(bound["subject"]["directory_state"], "active")
        versions = self.request("get", f"/api/monitors/{bound['id']}/versions", headers=self.auth_headers()).json["data"]
        self.assertEqual(versions[0]["configuration"]["subject_id"], subject["id"])
        self.assertEqual(versions[0]["configuration"]["subject"]["id"], subject["id"])

        unbound_update = self.request(
            "patch", f"/api/monitors/{bound['id']}",
            json={"base_version": bound["version"], "subject_id": None}, headers=self.auth_headers(),
        )
        self.assertEqual(unbound_update.status_code, 200, unbound_update.get_data(as_text=True))
        self.assertIsNone(unbound_update.json["data"]["subject_id"])
        rebound = self.request(
            "patch", f"/api/monitors/{bound['id']}",
            json={"base_version": unbound_update.json["data"]["version"], "subject_id": subject["id"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(rebound.status_code, 200, rebound.get_data(as_text=True))
        self.assertEqual(rebound.json["data"]["subject_id"], subject["id"])

        # A free-text scope may mention a name, but it never creates a subject link.
        unbound = self.create_monitor("https://news.example/unbound", scope=subject["name"])
        self.assertIsNone(unbound["subject_id"])
        self.assertIsNone(unbound["subject"])

        foreign_token = self.create_other_workspace()
        foreign = self.request(
            "post", "/api/monitors", json=self.config("https://news.example/foreign", subject_id=subject["id"]),
            headers={"Authorization": f"Bearer {foreign_token}", "Idempotency-Key": "foreign-subject-monitor"},
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json["error"]["code"], "NOT_FOUND")

    def test_work_scoped_monitor_uses_shared_material_job_and_stops_when_work_is_archived(self) -> None:
        work = self.create_work()
        monitor = self.create_monitor("https://news.example/work-source", work_id=work["id"])
        self.assertEqual(monitor["work_id"], work["id"])
        self.assertEqual(monitor["work"]["id"], work["id"])
        self.assertFalse(monitor["follow_source_links"])
        version = self.request("get", f"/api/monitors/{monitor['id']}/versions", headers=self.auth_headers()).json["data"][0]
        self.assertEqual(version["configuration"]["work_id"], work["id"])
        self.assertFalse(version["configuration"]["follow_source_links"])

        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": monitor["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        run_time = enabled.json["data"]["next_run_at"]
        tick_monitoring(self.app, run_time)
        run = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"][0]
        job_id = run["sources"][0]["job_id"]
        self.assertEqual(self.store.row("SELECT work_id FROM jobs WHERE id = ?", (job_id,))["work_id"], work["id"])

        archived = self.request(
            "patch", f"/api/works/{work['id']}",
            json={"base_version": work["version"], "status": "archived"}, headers=self.auth_headers(),
        )
        self.assertEqual(archived.status_code, 200, archived.get_data(as_text=True))
        monitor_state = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        tick_monitoring(self.app, _plus_seconds(monitor_state["next_run_at"], 1))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"], "cancelled")
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("archived-work monitor reached the public source")):
            self.assertFalse(run_once(self.app))
        paused = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(paused["next_run_at"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials WHERE work_id = ?", (work["id"],))["count"], 0)

    def test_work_scoped_monitor_saves_source_material_to_the_selected_work(self) -> None:
        work = self.create_work(title="来源归入所选工作")
        source_url = "https://news.example/work-material"
        monitor = self.create_monitor(source_url, work_id=work["id"])
        queued = self.check(monitor, "work-bound-source")
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["sources"][0]["job_id"]
        result = self.acquisition(b"<html><body>Release alpha</body></html>", "Release alpha", etag="work-alpha")
        result.update({"url": source_url, "requested_url": source_url})
        with patch("server.oevira.acquisition.acquire_url", return_value=result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        job = self.store.row("SELECT work_id, material_id FROM jobs WHERE id = ?", (job_id,))
        material = self.store.row("SELECT work_id, source_url FROM materials WHERE id = ?", (job["material_id"],))
        association = self.store.row("SELECT 1 AS linked FROM work_materials WHERE work_id = ? AND material_id = ?", (work["id"], job["material_id"]))
        self.assertEqual(job["work_id"], work["id"])
        self.assertEqual(material["work_id"], work["id"])
        self.assertEqual(material["source_url"], source_url)
        self.assertIsNotNone(association)
        second_check = self.check(monitor, "work-bound-change")
        self.assertEqual(second_check.status_code, 202, second_check.get_data(as_text=True))
        changed = self.acquisition(b"second-source", "Release beta", etag="work-beta")
        changed.update({"url": source_url, "requested_url": source_url})
        with patch("server.oevira.acquisition.acquire_url", return_value=changed):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        signal = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers()).json["data"][0]
        linked = self.request(
            "post", f"/api/monitor-signals/{signal['id']}/work", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "reuse-linked-monitor-work"},
        )
        self.assertEqual(linked.status_code, 200, linked.get_data(as_text=True))
        self.assertEqual(linked.json["data"]["id"], work["id"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM works")["count"], 1)
        current = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(current["work"]["id"], work["id"])

    def test_work_monitor_seeds_from_trusted_selected_public_http_material_without_claiming_a_check(self) -> None:
        work = self.create_work(title="复用已保存网页基线")
        source_url = "https://news.example/saved-baseline"
        captured = self.acquisition(
            b"<html><body><main>Release alpha.</main></body></html>", "Release alpha.", etag='"saved-v1"',
        )
        captured.update({"url": source_url, "requested_url": source_url})
        queued = self.request(
            "post", "/api/materials/url", json={"url": source_url, "work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "saved-baseline-material"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        with patch("server.oevira.acquisition.acquire_url", return_value=captured):
            self.assertTrue(run_once(self.app))
        material_id = self.store.row("SELECT material_id FROM jobs WHERE id = ?", (queued.json["data"]["id"],))["material_id"]
        material = self.request(
            "get", f"/api/materials/{material_id}", headers=self.auth_headers(),
        )
        self.assertEqual(material.status_code, 200, material.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (queued.json["data"]["id"],))["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs")["count"], 1)

        monitor = self.create_monitor(source_url, work_id=work["id"])
        baseline = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(baseline["status"], "baseline_established")
        self.assertEqual(baseline["last_material_id"], material_id)
        self.assertIsNone(baseline["last_check_at"])
        self.assertIsNone(baseline["last_success_at"])
        self.assertEqual(baseline["baseline"]["kind"], "selected_saved_material")
        self.assertEqual(baseline["baseline"]["material_sha256"], material.json["data"]["sha256"])
        self.assertEqual(baseline["baseline"]["text_sha256"], material.json["data"]["source"]["text_sha256"])
        self.assertEqual(baseline["baseline"]["captured_at"], material.json["data"]["source"]["captured_at"])
        self.assertEqual(baseline["baseline"]["coverage"]["source_status"], "ready")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs")["count"], 1)

        check = self.check(monitor, "saved-baseline-first-real-check")
        self.assertEqual(check.status_code, 202, check.get_data(as_text=True))
        unchanged = {
            "status": "unchanged", "url": source_url, "captured_at": utc_now(),
            "etag": '"saved-v1"', "last_modified": None,
        }
        with patch("server.oevira.acquisition.acquire_url", return_value=unchanged) as fetch:
            self.assertTrue(run_once(self.app))
        fetch.assert_called_once_with(source_url, etag='"saved-v1"', last_modified=None)
        tick_monitoring(self.app)
        first_check = self.request(
            "get", f"/api/monitor-runs/{check.json['data']['id']}", headers=self.auth_headers(),
        ).json["data"]
        self.assertEqual(first_check["sources"][0]["status"], "no_change")
        self.assertEqual(first_check["sources"][0]["material_id"], material_id)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_saved_material_baseline_rejects_cross_client_partial_expired_and_unreviewed_use(self) -> None:
        from server.oevira.monitoring import _seed_work_material_baselines

        work_a = self.create_work(title="来源客户甲工作")
        work_b = self.create_work(title="来源客户乙工作")
        commissions = []
        for index, work in enumerate((work_a, work_b), start=1):
            commission = self.request(
                "post", "/api/business/commissions", json={"name": f"来源客户{index}", "scope": {}},
                headers={**self.auth_headers(), "Idempotency-Key": f"baseline-commission-{index}"},
            )
            self.assertEqual(commission.status_code, 201, commission.get_data(as_text=True))
            value = commission.json["data"]
            assigned = self.request(
                "post", f"/api/business/commissions/{value['id']}/works/{work['id']}", json={},
                headers={**self.auth_headers(), "Idempotency-Key": f"baseline-assignment-{index}"},
            )
            self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
            commissions.append(value)

        def acquire(url: str, work_id: str, key: str, text: str, *, status: str = "ready") -> str:
            content = f"<html><body><main>{text}</main></body></html>".encode("utf-8")
            result = self.acquisition(content, text, etag=key)
            result.update({"url": url, "requested_url": url, "status": status})
            if status == "partial":
                result["reason"] = "The source text is partial."
            queued = self.request(
                "post", "/api/materials/url", json={"url": url, "work_id": work_id},
                headers={**self.auth_headers(), "Idempotency-Key": key},
            )
            self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
            with patch("server.oevira.acquisition.acquire_url", return_value=result):
                self.assertTrue(run_once(self.app))
            job = self.store.row("SELECT material_id FROM jobs WHERE id = ?", (queued.json["data"]["id"],))
            return job["material_id"]

        private_url = "https://news.example/private-client-baseline"
        private_material_id = acquire(private_url, work_a["id"], "private-baseline-material", "Release private.")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_b["id"], private_material_id, self.identity["workspace"]["id"], utc_now()),
            )
        candidates = self.request(
            "get", f"/api/works/{work_b['id']}/monitor-source-candidates", headers=self.auth_headers(),
        )
        self.assertEqual(candidates.status_code, 200, candidates.get_data(as_text=True))
        self.assertEqual(candidates.json["data"]["candidates"], [])
        self.assertEqual(candidates.json["data"]["total_linked_materials"], 0)
        self.assertEqual(candidates.json["data"]["excluded_reasons"]["inaccessible_material"], 1)
        cross_client_monitor = self.create_monitor(private_url, work_id=work_b["id"])
        cross_client_source = cross_client_monitor["sources"][0]
        self.assertIsNone(cross_client_source["last_material_id"])
        self.assertEqual(cross_client_source["status"], "not_checked")

        same_client_work = self.create_work(title="同客户复核工作")
        same_client_assignment = self.request(
            "post", f"/api/business/commissions/{commissions[0]['id']}/works/{same_client_work['id']}", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "same-client-baseline-assignment"},
        )
        self.assertEqual(same_client_assignment.status_code, 201, same_client_assignment.get_data(as_text=True))
        supporting = self.request(
            "post", "/api/materials", json={"title": "同客户辅助材料", "text": "用于显式确认同客户来源采用", "work_id": work_a["id"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(supporting.status_code, 201, supporting.get_data(as_text=True))
        compare = self.request(
            "post", f"/api/works/{same_client_work['id']}/compare",
            json={"material_ids": [private_material_id, supporting.json["data"]["id"]], "criteria": ["source scope"]},
            headers={**self.auth_headers(), "Idempotency-Key": "same-client-material-selection"},
        )
        self.assertEqual(compare.status_code, 202, compare.get_data(as_text=True))
        compare_paused = self.request(
            "post", f"/api/jobs/{compare.json['data']['id']}/pause",
            json={"operation_id": "same-client-comparison-pause", "base_control_revision": 0},
            headers=self.auth_headers(),
        )
        self.assertEqual(compare_paused.status_code, 200, compare_paused.get_data(as_text=True))
        selected_candidates = self.request(
            "get", f"/api/works/{same_client_work['id']}/monitor-source-candidates", headers=self.auth_headers(),
        ).json["data"]["candidates"]
        self.assertIn(private_url, {item["url"] for item in selected_candidates})
        same_client_monitor = self.create_monitor(private_url, work_id=same_client_work["id"])
        same_client_baseline = same_client_monitor["sources"][0]
        self.assertEqual(same_client_baseline["last_material_id"], private_material_id)
        self.assertEqual(same_client_baseline["baseline"]["kind"], "selected_saved_material")

        partial_url = "https://news.example/partial-baseline"
        partial_material_id = acquire(partial_url, work_a["id"], "partial-baseline-material", "Release partial.", status="partial")
        partial_monitor = self.create_monitor(partial_url, work_id=work_a["id"])
        partial_source = partial_monitor["sources"][0]
        self.assertIsNone(partial_source["last_material_id"])
        self.assertEqual(self.store.row("SELECT status FROM materials WHERE id = ?", (partial_material_id,))["status"], "partial")

        use_url = "https://news.example/unreviewed-use-baseline"
        use_material_id = acquire(use_url, work_a["id"], "unreviewed-use-material", "Release reviewed.")
        material_row = self.store.row("SELECT source_json FROM materials WHERE id = ?", (use_material_id,))
        source_json = json.loads(material_row["source_json"])
        source_json["source_use"] = {
            "purpose": "monitoring", "status": "revoked", "expires_at": "2000-01-01T00:00:00Z",
        }
        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET source_json = ? WHERE id = ?", (json.dumps(source_json), use_material_id))
        revoked_use_monitor = self.create_monitor(use_url, work_id=work_a["id"])
        self.assertIsNone(revoked_use_monitor["sources"][0]["last_material_id"])

        forged_url = "https://news.example/forged-public-http"
        imported = self.request(
            "post", "/api/materials", json={"title": "伪装来源记录", "text": "Unverified imported text", "work_id": work_a["id"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        forged_material_id = imported.json["data"]["id"]
        forged_projection = imported.json["data"]["source_text"] or imported.json["data"]["text"]
        forged_metadata = {
            "acquisition_method": "public_http", "status": "ready", "requested_url": forged_url,
            "text_sha256": hashlib.sha256(forged_projection.encode("utf-8")).hexdigest(),
            "captured_at": utc_now(),
        }
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET source_url = ?, source_json = ? WHERE id = ?",
                (forged_url, json.dumps(forged_metadata), forged_material_id),
            )
        forged_monitor = self.create_monitor(forged_url, work_id=work_a["id"])
        self.assertIsNone(forged_monitor["sources"][0]["last_material_id"])

        expired_url = "https://news.example/expired-monitor-baseline"
        expired_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        expired_monitor = self.create_monitor(expired_url, work_id=work_a["id"], stop_at=expired_at)
        expired_material_id = acquire(expired_url, work_a["id"], "expired-monitor-material", "Release too late.")
        past = "2000-01-01T00:00:00Z"
        with self.store.transaction() as connection:
            connection.execute("UPDATE monitor_rules SET stop_at = ? WHERE id = ?", (past, expired_monitor["id"]))
            expired_row = connection.execute("SELECT * FROM monitor_rules WHERE id = ?", (expired_monitor["id"],)).fetchone()
            source = connection.execute(
                "SELECT * FROM monitor_sources WHERE monitor_id = ? AND url = ?",
                (expired_monitor["id"], expired_url),
            ).fetchone()
            self.assertEqual(_seed_work_material_baselines(self.store, connection, self.identity, expired_row, utc_now()), 0)
            self.assertIsNone(connection.execute("SELECT last_material_id FROM monitor_sources WHERE id = ?", (source["id"],)).fetchone()["last_material_id"])
        self.assertEqual(self.store.row("SELECT status FROM materials WHERE id = ?", (expired_material_id,))["status"], "ready")

    def test_work_source_candidates_are_workspace_scoped_deduplicated_and_paginated(self) -> None:
        work = self.create_work(title="来源候选材料工作")
        workspace_id = self.identity["workspace"]["id"]
        owner_user_id = self.identity["user"]["id"]
        commission_response = self.request(
            "post", "/api/business/commissions", json={"name": "监测候选客户", "scope": {}},
            headers={**self.auth_headers(), "Idempotency-Key": "monitor-candidate-commission"},
        )
        self.assertEqual(commission_response.status_code, 201, commission_response.get_data(as_text=True))
        commission = commission_response.json["data"]
        assigned = self.request(
            "post", f"/api/business/commissions/{commission['id']}/works/{work['id']}", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "monitor-candidate-work-assignment"},
        )
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
        member_id, member_token = self.create_workspace_member()
        now = utc_now()
        safe_url = "https://news.example/research"
        second_url = "https://news.example/brief"
        token_url = "https://news.example/private?access_token=fixture-secret"
        private_url = "http://127.0.0.1/internal"
        records = [
            ("safe one", safe_url + "#section-one", {"url": safe_url + "#section-one", "title": "Research"}),
            ("safe duplicate", safe_url + "#section-two", {"source_url": safe_url + "#section-two", "title": "Research copy"}),
            ("second", None, {"url": second_url, "title": "Brief"}),
            ("token", token_url, {"url": token_url, "title": "Private query"}),
            ("private", private_url, {"url": private_url, "title": "Private host"}),
            ("missing", None, {"title": "No source"}),
        ]
        material_ids = []
        with self.store.transaction() as connection:
            for title, source_url, source_metadata in records:
                material_id = str(uuid.uuid4())
                material_ids.append(material_id)
                connection.execute(
                    """INSERT INTO materials(
                           id, workspace_id, owner_user_id, work_id, title, filename, format, status,
                           reason, text, sha256, relative_path, size_bytes, source_url, etag,
                           last_modified, source_json, source_text, created_at
                       ) VALUES(?, ?, ?, ?, ?, 'source.txt', 'text', 'ready', NULL, 'CANDIDATE BODY MUST NOT LEAK', ?, ?, 0, ?, NULL, NULL, ?, 'CANDIDATE SOURCE TEXT MUST NOT LEAK', ?)""",
                    (material_id, workspace_id, owner_user_id, work["id"], title, "0" * 64,
                     f"objects/{workspace_id}/materials/{material_id}/original", source_url,
                     json.dumps(source_metadata, ensure_ascii=False), now),
                )
                connection.execute(
                    "INSERT INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    (work["id"], material_id, workspace_id, now),
                )

        monitor = self.create_monitor(safe_url, work_id=work["id"])
        monitor_count_before = self.store.row("SELECT COUNT(*) AS count FROM monitor_rules")["count"]

        first_page = self.request(
            "get", f"/api/works/{work['id']}/monitor-source-candidates?limit=1&offset=0",
            headers=self.auth_headers(),
        )
        self.assertEqual(first_page.status_code, 200, first_page.get_data(as_text=True))
        first = first_page.json["data"]
        self.assertEqual(first["work_id"], work["id"])
        self.assertEqual(first["total_linked_materials"], len(records))
        self.assertEqual(first["total_candidates"], 2)
        self.assertEqual(first["excluded_count"], 3)
        self.assertEqual(first["excluded_reasons"], {
            "missing_url": 1, "redacted_query": 1, "invalid_url": 1, "inaccessible_material": 0,
        })
        self.assertEqual(first["next_offset"], 1)
        self.assertIn(first["candidates"][0]["url"], {safe_url, second_url})
        self.assertNotIn("text", first["candidates"][0])
        self.assertNotIn("CANDIDATE BODY MUST NOT LEAK", first_page.get_data(as_text=True))
        self.assertNotIn("CANDIDATE SOURCE TEXT MUST NOT LEAK", first_page.get_data(as_text=True))

        second_page = self.request(
            "get", f"/api/works/{work['id']}/monitor-source-candidates?limit=1&offset=1",
            headers=self.auth_headers(),
        )
        self.assertEqual(second_page.status_code, 200, second_page.get_data(as_text=True))
        second = second_page.json["data"]
        self.assertEqual({first["candidates"][0]["url"], second["candidates"][0]["url"]}, {safe_url, second_url})
        safe_candidate = first["candidates"][0] if first["candidates"][0]["url"] == safe_url else second["candidates"][0]
        self.assertEqual(safe_candidate["material_count"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_rules")["count"], monitor_count_before)

        foreign_token = self.create_other_workspace()
        hidden = self.request(
            "get", f"/api/works/{work['id']}/monitor-source-candidates",
            headers=self.auth_headers(foreign_token),
        )
        self.assertEqual(hidden.status_code, 404)

        client_hidden = self.request(
            "get", f"/api/works/{work['id']}/monitor-source-candidates",
            headers=self.auth_headers(member_token),
        )
        self.assertEqual(client_hidden.status_code, 404)
        monitor_hidden = self.request(
            "get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers(member_token),
        )
        self.assertEqual(monitor_hidden.status_code, 404)
        self.assertEqual(self.request("get", "/api/monitors", headers=self.auth_headers(member_token)).json["data"], [])
        denied_check = self.request(
            "post", f"/api/monitors/{monitor['id']}/check",
            json={"base_version": monitor["version"]},
            headers={"Authorization": f"Bearer {member_token}", "Idempotency-Key": "unauthorized-work-monitor-check"},
        )
        self.assertEqual(denied_check.status_code, 404)

        grant = self.request(
            "patch", f"/api/business/commissions/{commission['id']}",
            json={"base_version": commission["version"], "member": {"user_id": member_id, "role": "viewer", "active": True}},
            headers=self.auth_headers(),
        )
        self.assertEqual(grant.status_code, 200, grant.get_data(as_text=True))
        visible = self.request(
            "get", f"/api/works/{work['id']}/monitor-source-candidates",
            headers=self.auth_headers(member_token),
        )
        self.assertEqual(visible.status_code, 200, visible.get_data(as_text=True))
        visible_monitor = self.request(
            "get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers(member_token),
        )
        self.assertEqual(visible_monitor.status_code, 200, visible_monitor.get_data(as_text=True))
        self.assertEqual(len(self.request("get", "/api/monitors", headers=self.auth_headers(member_token)).json["data"]), 1)
        viewer_write = self.request(
            "post", f"/api/monitors/{monitor['id']}/check",
            json={"base_version": monitor["version"]},
            headers={"Authorization": f"Bearer {member_token}", "Idempotency-Key": "viewer-work-monitor-check"},
        )
        self.assertEqual(viewer_write.status_code, 403)

    def test_opt_in_same_origin_discovery_preserves_link_provenance_and_bounds_scope(self) -> None:
        root_url = "https://news.example/discovery-root"
        monitor = self.create_monitor(root_url, keywords=[], follow_source_links=True)
        result = self.acquisition(b"root-source", "Release alpha", etag="root-1")
        result.update({
            "url": root_url,
            "requested_url": root_url,
            "discovered_links": [
                {"url": "https://news.example/child", "title": "Same-origin child", "same_origin": True},
                {"url": "https://news.example/sitemap.xml", "title": "Sitemap", "same_origin": True, "kind": "robots_sitemap"},
                {"url": "https://other.example/outside", "title": "External link", "same_origin": False},
            ],
            "entries": [{
                "native_id": "feed-entry-1", "title": "Feed entry", "url": "https://news.example/entry",
                "text": "Entry content", "author": "Source author", "published_at": "2026-11-03T00:00:00Z",
            }, {
                "kind": "sitemap_url", "native_id": None, "title": None,
                "url": "https://news.example/from-sitemap", "updated_at": "2026-11-09",
            }],
            "next_url": "https://news.example/page/2",
            "entries_truncated": False,
            "links_truncated": True,
            "entry_count_observed": 2,
        })
        queued = self.check(monitor, "discover-same-origin-links")
        with patch("server.oevira.acquisition.acquire_url", return_value=result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)

        run = self.request("get", f"/api/monitor-runs/{queued.json['data']['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["sources"][0]["status"], "baseline_established")
        self.assertIn("not claimed exhaustive", run["sources"][0]["reason"])
        current = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertTrue(current["follow_source_links"])
        by_url = {item["url"]: item for item in current["sources"]}
        self.assertEqual(set(by_url), {
            root_url, "https://news.example/child", "https://news.example/entry",
            "https://news.example/sitemap.xml", "https://news.example/from-sitemap", "https://news.example/page/2",
        })
        self.assertNotIn("https://other.example/outside", by_url)
        child = by_url["https://news.example/entry"]
        self.assertEqual(child["source_origin"], "discovered")
        self.assertEqual(child["discovery_origins"][0]["parent_url"], root_url)
        self.assertEqual(child["discovery_origins"][0]["parent_material_id"], run["sources"][0]["material_id"])
        self.assertEqual(child["discovery_origins"][0]["native_id"], "feed-entry-1")
        self.assertEqual(child["discovery_origins"][0]["config_version"], monitor["version"])
        sitemap_page = by_url["https://news.example/from-sitemap"]
        self.assertEqual(sitemap_page["source_origin"], "discovered")
        self.assertEqual([item["kind"] for item in sitemap_page["discovery_origins"]], ["sitemap_url"], sitemap_page)
        self.assertEqual(sitemap_page["status"], "not_checked")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)

        follow_up = self.check(monitor, "check-discovered-child")
        self.assertEqual(follow_up.status_code, 202, follow_up.get_data(as_text=True))
        self.assertEqual(follow_up.json["data"]["coverage"]["selected_sources"], 1)
        selected_source = next(item for item in follow_up.json["data"]["sources"] if item["job_id"])
        selected_job = self.store.row("SELECT payload_json FROM jobs WHERE id = ?", (selected_source["job_id"],))
        self.assertEqual(json.loads(selected_job["payload_json"])["url"], "https://news.example/child")
        child_result = {**result, "url": "https://news.example/child", "requested_url": "https://news.example/child"}
        with patch("server.oevira.acquisition.acquire_url", return_value=child_result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)

        before_scope_edit = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        narrowed = self.request(
            "patch", f"/api/monitors/{monitor['id']}",
            json={"base_version": before_scope_edit["version"], "sources": [{"url": root_url, "label": "来源 1"}]},
            headers=self.auth_headers(),
        )
        self.assertEqual(narrowed.status_code, 200, narrowed.get_data(as_text=True))
        root_check = self.check(narrowed.json["data"], "recheck-narrowed-scope")
        self.assertEqual(root_check.status_code, 202, root_check.get_data(as_text=True))
        root_result = {**result, "url": root_url, "requested_url": root_url}
        with patch("server.oevira.acquisition.acquire_url", return_value=root_result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        narrowed_monitor = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(narrowed_monitor["source_count"], 1)
        removed_child = next(item for item in narrowed_monitor["sources"] if item["url"] == "https://news.example/child")
        self.assertFalse(removed_child["active"])
        self.assertTrue(removed_child["discovery_suppressed"])

        opt_out = self.create_monitor("https://news.example/no-follow", keywords=[])
        opt_out_result = {**result, "url": "https://news.example/no-follow", "requested_url": "https://news.example/no-follow"}
        opt_out_check = self.check(opt_out, "no-link-follow")
        self.assertEqual(opt_out_check.status_code, 202, opt_out_check.get_data(as_text=True))
        with patch("server.oevira.acquisition.acquire_url", return_value=opt_out_result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        opt_out_data = self.request("get", f"/api/monitors/{opt_out['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(opt_out_data["source_count"], 1)

    def test_partial_sitemap_still_discovers_bounded_candidates_without_claiming_page_capture(self) -> None:
        root_url = "https://news.example/bounded-sitemap-index"
        child_url = "https://news.example/child-sitemap.xml"
        monitor = self.create_monitor(root_url, keywords=[], follow_source_links=True)
        result = self.acquisition(b"sitemap-index-original", "Bounded Sitemap index projection")
        result.update({
            "url": root_url, "requested_url": root_url,
            "status": "partial", "reason": "This sitemap file exceeded the 500-entry read bound.",
            "extraction_scope": "sitemap_index",
            "entries": [{"kind": "sitemap_index", "native_id": None, "title": None,
                         "url": child_url, "updated_at": "2026-11-09"}],
            "entries_truncated": True, "entry_count_observed": 501,
            "links_truncated": True, "discovered_links": [],
        })
        queued = self.check(monitor, "bounded-sitemap-candidates")
        with patch("server.oevira.acquisition.acquire_url", return_value=result):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)

        run = self.request(
            "get", f"/api/monitor-runs/{queued.json['data']['id']}", headers=self.auth_headers(),
        ).json["data"]
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["sources"][0]["status"], "partial")
        current = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        candidate = next(item for item in current["sources"] if item["url"] == child_url)
        self.assertEqual(candidate["source_origin"], "discovered")
        self.assertEqual(candidate["discovery_origins"][0]["kind"], "sitemap_index")
        self.assertEqual(candidate["status"], "not_checked")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)

        next_check = self.check(monitor, "bounded-sitemap-child-check")
        self.assertEqual(next_check.status_code, 202, next_check.get_data(as_text=True))
        selected = next(item for item in next_check.json["data"]["sources"] if item["job_id"])
        payload = json.loads(self.store.row("SELECT payload_json FROM jobs WHERE id = ?", (selected["job_id"],))["payload_json"])
        self.assertEqual(payload["url"], child_url)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)

    def test_subject_removal_atomically_pauses_linked_monitor_and_readd_needs_explicit_enable(self) -> None:
        subject = self.create_subject(name="生命周期虚构品牌")
        monitor = self.create_monitor("https://news.example/lifecycle", subject_id=subject["id"])
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": monitor["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        due = enabled.json["data"]["next_run_at"]
        tick_monitoring(self.app, due)
        runs = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"]
        self.assertEqual(len(runs), 1)
        run = runs[0]
        job_id = run["sources"][0]["job_id"]
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"], "queued")

        removed = self.request(
            "delete", f"/api/business/subjects/{subject['id']}",
            json={"base_version": subject["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        current_monitor = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(current_monitor["subject"]["directory_state"], "removed")
        self.assertEqual(current_monitor["status"], "paused")
        self.assertIsNone(current_monitor["next_run_at"])
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"], "cancelled")
        current_run = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(current_run["status"], "cancelled")
        self.assertEqual(current_run["subject_id"], subject["id"])
        self.assertEqual(current_run["subject"]["directory_state"], "active")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals WHERE monitor_id = ?", (monitor["id"],))["count"], 0)

        new_use = self.request(
            "post", "/api/monitors", json=self.config("https://news.example/after-removal", subject_id=subject["id"]),
            headers={**self.auth_headers(), "Idempotency-Key": "create-after-subject-removal"},
        )
        self.assertEqual(new_use.status_code, 409)
        self.assertEqual(new_use.json["error"]["code"], "SUBJECT_REMOVED")

        rejected_check = self.check(current_monitor, "removed-subject-check")
        self.assertEqual(rejected_check.status_code, 409)
        self.assertEqual(rejected_check.json["error"]["code"], "SUBJECT_REMOVED")
        rejected_enable = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": current_monitor["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(rejected_enable.status_code, 409)
        self.assertEqual(rejected_enable.json["error"]["code"], "SUBJECT_REMOVED")

        tick_monitoring(self.app, _plus_seconds(due, 3600))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_runs WHERE monitor_id = ?", (monitor["id"],))["count"], 1)
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("cancelled monitoring job reached a public source")):
            self.assertFalse(run_once(self.app))

        restored = self.request(
            "post", f"/api/business/subjects/{subject['id']}/restore",
            json={"base_version": removed.json["data"]["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(restored.json["data"]["identity_review"]["status"], "needs_review")
        after_restore = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(after_restore["status"], "paused")
        tick_monitoring(self.app, _plus_seconds(due, 7200))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_runs WHERE monitor_id = ?", (monitor["id"],))["count"], 1)

        explicitly_enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": after_restore["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(explicitly_enabled.status_code, 200, explicitly_enabled.get_data(as_text=True))
        self.assertEqual(explicitly_enabled.json["data"]["status"], "enabled")

    def test_subject_removal_and_monitor_cancellation_share_one_transaction(self) -> None:
        subject = self.create_subject(name="原子移除测试品牌")
        monitor = self.create_monitor("https://news.example/atomic", subject_id=subject["id"])
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": monitor["version"]}, headers=self.auth_headers(),
        ).json["data"]
        tick_monitoring(self.app, enabled["next_run_at"])
        with patch("server.oevira.monitoring.stop_monitors_for_removed_subject", side_effect=RuntimeError("rollback probe")):
            failed = self.request(
                "delete", f"/api/business/subjects/{subject['id']}",
                json={"base_version": subject["version"]}, headers=self.auth_headers(),
            )
        self.assertEqual(failed.status_code, 500)
        subject_after_failure = self.request("get", f"/api/business/subjects/{subject['id']}", headers=self.auth_headers()).json["data"]
        monitor_after_failure = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(subject_after_failure["directory_state"], "active")
        self.assertEqual(monitor_after_failure["status"], "enabled")
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE kind = 'acquire_url'")["status"], "queued")

    def test_subject_removal_preserves_saved_material_but_suppresses_unharvested_result(self) -> None:
        subject = self.create_subject(name="未核收结果品牌")
        monitor = self.create_monitor("https://news.example/unharvested", subject_id=subject["id"])
        baseline = b"<html><body>Release alpha</body></html>"
        first = self.acquire_and_harvest(monitor, baseline, "Release alpha", etag="alpha")
        self.assertEqual(first["sources"][0]["status"], "baseline_established")

        pending = self.check(monitor, "unharvested-subject-check")
        self.assertEqual(pending.status_code, 202, pending.get_data(as_text=True))
        run_id = pending.json["data"]["id"]
        changed = b"<html><body>Release beta</body></html>"
        with patch(
            "server.oevira.acquisition.acquire_url",
            return_value=self.acquisition(changed, "Release beta", etag="beta"),
        ):
            self.assertTrue(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 2)

        removed = self.request(
            "delete", f"/api/business/subjects/{subject['id']}",
            json={"base_version": subject["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        harvested = tick_monitoring(self.app)
        self.assertEqual(harvested["harvested"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 2)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)
        second = self.request("get", f"/api/monitor-runs/{run_id}", headers=self.auth_headers()).json["data"]
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["sources"][0]["status"], "cancelled")
        self.assertIsNotNone(second["sources"][0]["material_id"])

    def test_a_b_a_creates_distinct_observations_and_signals_and_links_work(self) -> None:
        monitor = self.create_monitor("https://news.example/brand")
        original = b"<html><title>A</title><body>Release alpha</body></html>"
        changed = b"<html><title>B</title><body>Release beta</body></html>"
        first = self.acquire_and_harvest(monitor, original, "Release alpha", etag="a")
        self.assertEqual(first["sources"][0]["status"], "baseline_established")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

        second = self.acquire_and_harvest(monitor, changed, "Release beta", etag="b")
        self.assertEqual(second["sources"][0]["status"], "signal")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 1)
        signal_response = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers())
        signal = signal_response.json["data"][0]
        self.assertEqual(signal["matched_keywords"], ["release"])
        self.assertEqual(signal["material_id"], second["sources"][0]["material_id"])
        self.assertEqual(signal["evidence"][0]["matched_keyword"], "release")
        self.assertIn("Release beta", signal["evidence"][0]["quote"])

        created = self.request(
            "post", f"/api/monitor-signals/{signal['id']}/work", json={"title": "复核新品变化"},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-1"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        work = created.json["data"]
        self.assertIn(signal["material_id"], work["material_ids"])
        repeated = self.request(
            "post", f"/api/monitor-signals/{signal['id']}/work", json={"title": "复核新品变化"},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-1"},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["data"]["id"], work["id"])

        with self.store.transaction() as connection:
            connection.execute("DROP TABLE monitor_signal_works")
            connection.execute(
                """CREATE TABLE monitor_signal_works (
                    signal_id TEXT PRIMARY KEY REFERENCES monitor_signals(id),
                    work_id TEXT NOT NULL UNIQUE REFERENCES works(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO monitor_signal_works(signal_id, work_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (signal["id"], work["id"], self.identity["workspace"]["id"], utc_now()),
            )
        install_monitoring_schema(self.store)
        self.assertEqual(
            self.store.row("SELECT work_id FROM monitor_signal_works WHERE signal_id = ?", (signal["id"],))["work_id"],
            work["id"],
        )

        third = self.acquire_and_harvest(monitor, original, "Release alpha", etag="a-again")
        self.assertEqual(third["sources"][0]["status"], "signal")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 3)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 2)
        observations = self.store.rows("SELECT sha256, captured_at FROM monitor_observations ORDER BY rowid")
        self.assertEqual([item["sha256"] for item in observations], [hashlib.sha256(item).hexdigest() for item in (original, changed, original)])
        self.assertEqual(len({item["captured_at"] for item in observations}), 3)
        signals = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers()).json["data"]
        second_signal = next(item for item in signals if item["id"] != signal["id"])
        attached = self.request(
            "post", f"/api/monitor-signals/{second_signal['id']}/work", json={"work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-attach-2"},
        )
        self.assertEqual(attached.status_code, 200, attached.get_data(as_text=True))
        self.assertEqual(attached.json["data"]["id"], work["id"])
        self.assertIn(third["sources"][0]["material_id"], attached.json["data"]["material_ids"])
        replay = self.request(
            "post", f"/api/monitor-signals/{second_signal['id']}/work", json={"work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-attach-2"},
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.json["data"]["id"], work["id"])
        other_work = self.create_work(title="另一项持续工作")
        conflict = self.request(
            "post", f"/api/monitor-signals/{second_signal['id']}/work", json={"work_id": other_work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-attach-other"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.get_data(as_text=True))
        self.assertEqual(conflict.json["error"]["code"], "SIGNAL_WORK_ALREADY_LINKED")
        same_key_conflict = self.request(
            "post", f"/api/monitor-signals/{second_signal['id']}/work", json={"work_id": other_work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "signal-work-attach-2"},
        )
        self.assertEqual(same_key_conflict.status_code, 409, same_key_conflict.get_data(as_text=True))
        self.assertEqual(same_key_conflict.json["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM monitor_signal_works WHERE work_id = ?", (work["id"],))["count"],
            2,
        )
        tick_monitoring(self.app)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 2)

    def test_signal_work_link_rechecks_material_client_scope_for_existing_targets(self) -> None:
        source_work = self.create_work(title="私有来源所属工作")
        target_work = self.create_work(title="另一客户的目标工作")
        for index, work in enumerate((source_work, target_work), start=1):
            response = self.request(
                "post", "/api/business/commissions",
                json={"name": f"隔离客户{index}", "scope": {}},
                headers={**self.auth_headers(), "Idempotency-Key": f"monitor-scope-commission-{index}"},
            )
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            commission = response.json["data"]
            assigned = self.request(
                "post", f"/api/business/commissions/{commission['id']}/works/{work['id']}", json={},
                headers={**self.auth_headers(), "Idempotency-Key": f"monitor-scope-assignment-{index}"},
            )
            self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))

        monitor = self.create_monitor("https://news.example/brand")
        self.acquire_and_harvest(monitor, b"<html>Release alpha</html>", "Release alpha", etag="scope-1")
        changed = self.acquire_and_harvest(monitor, b"<html>Release beta</html>", "Release beta", etag="scope-2")
        material_id = changed["sources"][0]["material_id"]
        signal = self.request(
            "get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(),
        ).json["data"][0]

        with self.store.transaction() as connection:
            connection.execute("UPDATE materials SET work_id = ? WHERE id = ?", (source_work["id"], material_id))
            connection.execute(
                "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (source_work["id"], material_id, self.identity["workspace"]["id"], utc_now()),
            )

        denied = self.request(
            "post", f"/api/monitor-signals/{signal['id']}/work", json={"work_id": target_work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "cross-client-signal-route"},
        )
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        self.assertEqual(denied.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        self.assertIsNone(self.store.row("SELECT 1 FROM monitor_signal_works WHERE signal_id = ?", (signal["id"],)))
        self.assertIsNone(self.store.row(
            "SELECT 1 FROM work_materials WHERE work_id = ? AND material_id = ?",
            (target_work["id"], material_id),
        ))

        accepted = self.request(
            "post", f"/api/monitor-signals/{signal['id']}/work", json={"work_id": source_work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "same-client-signal-route"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.json["data"]["id"], source_work["id"])
        self.assertIn(material_id, accepted.json["data"]["material_ids"])

    def test_monitor_resume_validator_blocks_obsolete_reads_and_allows_staged_material_recovery(self) -> None:
        validator = self.app.extensions["oevira.job_resume_validators"]["acquire_url"]
        source_url = "https://news.example/resume-source"
        monitor = self.create_monitor(source_url)
        queued = self.check(monitor, "resume-monitor-pause")
        job_id = queued.json["data"]["sources"][0]["job_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'paused', suspended_from = 'queued', pause_requested = 1 WHERE id = ?",
                (job_id,),
            )
        paused = self.request(
            "post", f"/api/monitors/{monitor['id']}/pause", json={"base_version": monitor["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            with self.assertRaises(ApiError) as blocked:
                validator(connection, self.identity, row, {})
        self.assertEqual(blocked.exception.code, "MONITOR_SOURCE_REVISION_CHANGED")

        stage = {
            "material_id": "captured-material-1", "sha256": hashlib.sha256(b"captured").hexdigest(),
            "source": {"requested_url": source_url},
        }
        with self.store.transaction() as connection:
            row = connection.execute("SELECT payload_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            payload = json.loads(row["payload_json"])
            payload["_staged_acquisition"] = stage
            connection.execute(
                "UPDATE jobs SET material_id = ?, payload_json = ? WHERE id = ?",
                (stage["material_id"], json.dumps(payload), job_id),
            )
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            self.assertIsNone(validator(connection, self.identity, row, {}))

        changed_monitor = self.create_monitor("https://news.example/removed-source")
        removed_check = self.check(changed_monitor, "resume-removed-source")
        removed_job_id = removed_check.json["data"]["sources"][0]["job_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'paused', suspended_from = 'queued', pause_requested = 1 WHERE id = ?",
                (removed_job_id,),
            )
        updated = self.request(
            "patch", f"/api/monitors/{changed_monitor['id']}",
            json={"base_version": changed_monitor["version"], "sources": [{"url": "https://news.example/replacement"}]},
            headers=self.auth_headers(),
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (removed_job_id,)).fetchone()
            with self.assertRaises(ApiError) as removed:
                validator(connection, self.identity, row, {})
        self.assertEqual(removed.exception.code, "MONITOR_SOURCE_REVISION_CHANGED")

    def test_job_control_resume_preserves_direct_public_url_acquisition(self) -> None:
        source_url = "https://news.example/direct-resume"
        queued = self.request(
            "post", "/api/materials/url", json={"url": source_url},
            headers={**self.auth_headers(), "Idempotency-Key": "direct-acquisition-resume"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["id"]
        paused = self.request(
            "post", f"/api/jobs/{job_id}/pause",
            json={"operation_id": "direct-acquisition-pause", "base_control_revision": 0},
            headers=self.auth_headers(),
        )
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        self.assertEqual(paused.json["data"]["status"], "paused")
        resumed = self.request(
            "post", f"/api/jobs/{job_id}/resume",
            json={"operation_id": "direct-acquisition-resume-control", "base_control_revision": 1},
            headers=self.auth_headers(),
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["status"], "queued")

        result = self.acquisition(b"<html><body>Release direct</body></html>", "Release direct")
        result.update({"url": source_url, "requested_url": source_url})
        with patch("server.oevira.acquisition.acquire_url", return_value=result) as fetch:
            self.assertTrue(run_once(self.app))
        fetch.assert_called_once()
        completed = self.request("get", f"/api/jobs/{job_id}", headers=self.auth_headers()).json["data"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_job_control_resume_rejects_stopped_removed_and_revised_monitor_sources(self) -> None:
        cases = ("stopped", "removed", "revised")
        for case in cases:
            with self.subTest(case=case):
                monitor = self.create_monitor(f"https://news.example/resume-{case}")
                run = self.check(monitor, f"resume-obsolete-{case}").json["data"]
                job_id = run["sources"][0]["job_id"]
                paused = self.request(
                    "post", f"/api/jobs/{job_id}/pause",
                    json={"operation_id": f"job-pause-{case}", "base_control_revision": 0},
                    headers=self.auth_headers(),
                )
                self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
                self.assertEqual(paused.json["data"]["status"], "paused")

                if case == "stopped":
                    changed = self.request(
                        "post", f"/api/monitors/{monitor['id']}/stop", json={"base_version": monitor["version"]},
                        headers=self.auth_headers(),
                    )
                elif case == "removed":
                    changed = self.request(
                        "patch", f"/api/monitors/{monitor['id']}",
                        json={"base_version": monitor["version"], "sources": [{"url": "https://news.example/replacement"}]},
                        headers=self.auth_headers(),
                    )
                else:
                    changed = self.request(
                        "patch", f"/api/monitors/{monitor['id']}",
                        json={"base_version": monitor["version"], "keywords": ["new rule"]},
                        headers=self.auth_headers(),
                    )
                self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))

                rejected = self.request(
                    "post", f"/api/jobs/{job_id}/resume",
                    json={"operation_id": f"job-resume-{case}", "base_control_revision": 1},
                    headers=self.auth_headers(),
                )
                self.assertEqual(rejected.status_code, 409, rejected.get_data(as_text=True))
                self.assertEqual(rejected.json["error"]["code"], "MONITOR_SOURCE_REVISION_CHANGED")
                current_job = self.request("get", f"/api/jobs/{job_id}", headers=self.auth_headers()).json["data"]
                self.assertEqual(current_job["status"], "paused")
                self.assertEqual(current_job["control_revision"], 1)
                with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("obsolete monitor request was fetched")):
                    self.assertFalse(run_once(self.app))

    def test_job_control_resume_commits_staged_monitor_material_without_rereading(self) -> None:
        from server.oevira import worker

        source_url = "https://news.example/staged-resume"
        monitor = self.create_monitor(source_url)
        run = self.check(monitor, "staged-resume-monitor").json["data"]
        job_id = run["sources"][0]["job_id"]
        result = self.acquisition(
            b"<html><body><main>Release captured.</main></body></html>", "Release captured.", etag="captured-1",
        )
        result.update({"url": source_url, "requested_url": source_url})
        stage_ready, allow_finish = threading.Event(), threading.Event()
        worker_errors = []

        def wait_after_stage(_app, _claim, _payload, _stage):
            stage_ready.set()
            if not allow_finish.wait(10):
                raise AssertionError("The test did not release the staged acquisition")

        def execute_first_attempt():
            try:
                run_once(self.app)
            except BaseException as exc:
                worker_errors.append(exc)

        with patch("server.oevira.acquisition.acquire_url", return_value=result) as fetch, patch.object(
            worker, "_finish_staged_acquisition", side_effect=wait_after_stage,
        ):
            thread = threading.Thread(target=execute_first_attempt)
            thread.start()
            try:
                self.assertTrue(stage_ready.wait(10), "The worker should persist the source checkpoint before pausing")
                staged = self.request("get", f"/api/jobs/{job_id}", headers=self.auth_headers()).json["data"]
                self.assertTrue(staged["material_id"])
                pause_job = self.request(
                    "post", f"/api/jobs/{job_id}/pause",
                    json={"operation_id": "staged-job-pause", "base_control_revision": 0},
                    headers=self.auth_headers(),
                )
                self.assertEqual(pause_job.status_code, 200, pause_job.get_data(as_text=True))
                pause_monitor = self.request(
                    "post", f"/api/monitors/{monitor['id']}/pause", json={"base_version": monitor["version"]},
                    headers=self.auth_headers(),
                )
                self.assertEqual(pause_monitor.status_code, 200, pause_monitor.get_data(as_text=True))
            finally:
                allow_finish.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker_errors, [])
        fetch.assert_called_once()
        paused = self.request("get", f"/api/jobs/{job_id}", headers=self.auth_headers()).json["data"]
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)

        resumed = self.request(
            "post", f"/api/jobs/{job_id}/resume",
            json={"operation_id": "staged-job-resume", "base_control_revision": 1},
            headers=self.auth_headers(),
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("a staged response must not be refetched")) as refetch:
            self.assertTrue(run_once(self.app))
        refetch.assert_not_called()
        completed = self.request("get", f"/api/jobs/{job_id}", headers=self.auth_headers()).json["data"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)
        self.assertEqual(tick_monitoring(self.app)["harvested"], 1)
        self.assertEqual(self.store.row("SELECT outcome FROM monitor_run_sources WHERE job_id = ?", (job_id,))["outcome"], "stale_configuration")

    def test_unbound_monitor_does_not_reuse_private_same_url_material_for_an_owner_of_multiple_clients(self) -> None:
        private_work = self.create_work(title="甲客户私有网页工作")
        other_work = self.create_work(title="乙客户网页工作")
        for index, work in enumerate((private_work, other_work), start=1):
            commission = self.request(
                "post", "/api/business/commissions", json={"name": f"同用户客户{index}", "scope": {}},
                headers={**self.auth_headers(), "Idempotency-Key": f"unbound-monitor-client-{index}"},
            )
            self.assertEqual(commission.status_code, 201, commission.get_data(as_text=True))
            assigned = self.request(
                "post", f"/api/business/commissions/{commission.json['data']['id']}/works/{work['id']}", json={},
                headers={**self.auth_headers(), "Idempotency-Key": f"unbound-monitor-assignment-{index}"},
            )
            self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))

        source_url = "https://news.example/shared-private-url"
        private = self.acquisition(b"<html><body>Private client Alpha.</body></html>", "Private client Alpha.", etag='"private-etag"')
        private.update({"url": source_url, "requested_url": source_url})
        private_job = self.request(
            "post", "/api/materials/url", json={"url": source_url, "work_id": private_work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "unbound-private-material"},
        )
        self.assertEqual(private_job.status_code, 202, private_job.get_data(as_text=True))
        with patch("server.oevira.acquisition.acquire_url", return_value=private):
            self.assertTrue(run_once(self.app))
        private_material_id = self.store.row("SELECT material_id FROM jobs WHERE id = ?", (private_job.json["data"]["id"],))["material_id"]

        monitor = self.create_monitor(source_url, keywords=["public"])
        check = self.check(monitor, "unbound-private-url-monitor")
        self.assertEqual(check.status_code, 202, check.get_data(as_text=True))
        public = self.acquisition(b"<html><body>Public observation.</body></html>", "Public observation.", etag="public-v1")
        public.update({"url": source_url, "requested_url": source_url})
        requests = []

        def read_public(url, *, etag=None, last_modified=None, timeout=25):
            requests.append((url, etag, last_modified))
            if etag:
                return {"status": "unchanged", "url": url, "captured_at": utc_now(), "etag": etag, "last_modified": last_modified}
            return public

        with patch("server.oevira.acquisition.acquire_url", side_effect=read_public):
            self.assertTrue(run_once(self.app))
        self.assertEqual(requests, [(source_url, None, None)])
        current_job = self.store.row("SELECT material_id FROM jobs WHERE id = ?", (check.json["data"]["sources"][0]["job_id"],))
        self.assertNotEqual(current_job["material_id"], private_material_id)
        tick_monitoring(self.app)
        run = self.request("get", f"/api/monitor-runs/{check.json['data']['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(run["sources"][0]["status"], "baseline_established")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_unbinding_monitor_preserves_old_work_history_without_workspace_signal_leak(self) -> None:
        work = self.create_work(title="客户私有监测历史")
        commission = self.request(
            "post", "/api/business/commissions", json={"name": "仅客户成员可读", "scope": {}},
            headers={**self.auth_headers(), "Idempotency-Key": "historical-signal-commission"},
        )
        self.assertEqual(commission.status_code, 201, commission.get_data(as_text=True))
        assigned = self.request(
            "post", f"/api/business/commissions/{commission.json['data']['id']}/works/{work['id']}", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "historical-signal-assignment"},
        )
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))

        monitor = self.create_monitor(work_id=work["id"], keywords=["beta"])
        self.acquire_and_harvest(
            monitor, b"<html><body>Release alpha.</body></html>", "Release alpha.", etag="history-a",
        )
        changed = self.acquire_and_harvest(
            monitor, b"<html><body>Release alpha.\nBeta is live.</body></html>", "Release alpha.\nBeta is live.", etag="history-b",
        )
        historical_run_id = changed["id"]
        signal = self.request(
            "get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(),
        ).json["data"][0]
        self.assertTrue(signal["evidence"])
        member_id, member_token = self.create_workspace_member(role="editor")

        unbound = self.request(
            "patch", f"/api/monitors/{monitor['id']}",
            json={"base_version": monitor["version"], "work_id": None}, headers=self.auth_headers(),
        )
        self.assertEqual(unbound.status_code, 200, unbound.get_data(as_text=True))
        current = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(current["signal_count"], 1)
        self.assertTrue(self.request(
            "get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(),
        ).json["data"])
        self.assertEqual(self.request(
            "get", f"/api/monitor-runs/{historical_run_id}", headers=self.auth_headers(),
        ).status_code, 200)

        historical_work = self.request("get", f"/api/works/{work['id']}", headers=self.auth_headers(member_token))
        self.assertEqual(historical_work.status_code, 404)
        viewer_monitor = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers(member_token))
        self.assertEqual(viewer_monitor.status_code, 200, viewer_monitor.get_data(as_text=True))
        viewer_data = viewer_monitor.json["data"]
        self.assertEqual(viewer_data["signal_count"], 0)
        self.assertIsNone(viewer_data["sources"][0]["last_material_id"])
        self.assertEqual(viewer_data["sources"][0]["status"], "unknown")
        self.assertEqual(
            self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(member_token)).json["data"],
            [],
        )
        self.assertEqual(
            self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers(member_token)).json["data"],
            [],
        )
        self.assertEqual(
            self.request("get", f"/api/monitor-runs/{historical_run_id}", headers=self.auth_headers(member_token)).status_code,
            404,
        )
        visible_versions = self.request(
            "get", f"/api/monitors/{monitor['id']}/versions", headers=self.auth_headers(member_token),
        ).json["data"]
        self.assertEqual([item["version"] for item in visible_versions], [unbound.json["data"]["version"]])

    def test_html_markup_and_whitespace_changes_do_not_create_content_signals(self) -> None:
        monitor = self.create_monitor("https://news.example/markup", keywords=[])
        readable = "Release alpha is available."
        first_bytes = b"<html><main><p>Release alpha is available.</p></main></html>"
        first = self.acquire_and_harvest(monitor, first_bytes, readable, etag="html-a")
        self.assertEqual(first["sources"][0]["status"], "baseline_established")

        second_bytes = b'<html><main data-render="2"><div>  Release\n alpha is available. </div><script>clock = 2</script></main></html>'
        second = self.acquire_and_harvest(monitor, second_bytes, readable, etag="html-b")
        self.assertEqual(second["sources"][0]["status"], "no_change")
        self.assertFalse(second["sources"][0]["changed"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 2)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_added_and_removed_text_blocks_have_exact_change_evidence(self) -> None:
        monitor = self.create_monitor("https://news.example/text-diff", keywords=[])
        before = "Header\nRemove this old statement.\nKeep this common statement.\nTail"
        after = "Header\nKeep this common statement.\nAdd this new statement.\nTail"
        baseline = self.acquire_and_harvest(monitor, b"original", before, etag="before")
        run = self.acquire_and_harvest(monitor, b"changed", after, etag="after")
        self.assertEqual(run["sources"][0]["status"], "signal")
        signal = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers()).json["data"][0]
        by_change = {item["change"]: item for item in signal["evidence"]}
        self.assertEqual(by_change["removed"]["quote"], "Remove this old statement.")
        self.assertEqual(by_change["added"]["quote"], "Add this new statement.")
        self.assertEqual(by_change["removed"]["material_id"], baseline["sources"][0]["material_id"])
        self.assertEqual(by_change["added"]["material_id"], run["sources"][0]["material_id"])

    def test_partial_snapshot_is_observed_but_does_not_advance_evaluation_cursor(self) -> None:
        monitor = self.create_monitor("https://news.example/partial", keywords=[])
        baseline = self.acquire_and_harvest(monitor, b"baseline", "Release alpha", etag="a")
        baseline_source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(baseline_source["evaluation_cursor"]["material_id"], baseline["sources"][0]["material_id"])

        partial = self.acquisition(b"partial", "Release beta", etag="b")
        partial["status"] = "partial"
        partial["reason"] = "The bounded feed response did not include all entries."
        queued = self.check(monitor, "partial-source-change")
        with patch("server.oevira.acquisition.acquire_url", return_value=partial):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        result = self.request("get", f"/api/monitor-runs/{queued.json['data']['id']}", headers=self.auth_headers()).json["data"]
        current_source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(result["sources"][0]["status"], "partial")
        self.assertEqual(current_source["evaluation_cursor"]["material_id"], baseline_source["evaluation_cursor"]["material_id"])
        self.assertEqual(current_source["source_cursor"]["sha256"], baseline_source["source_cursor"]["sha256"])
        self.assertIsNone(result["sources"][0]["changed"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_missing_evaluation_baseline_is_unknown_not_a_change_or_no_change(self) -> None:
        monitor = self.create_monitor("https://news.example/missing-baseline", keywords=[])
        baseline = self.acquire_and_harvest(monitor, b"baseline", "Release alpha", etag="a")
        baseline_material = self.store.row("SELECT relative_path FROM materials WHERE id = ?", (baseline["sources"][0]["material_id"],))
        original_path = _resource_path(self.store, baseline_material["relative_path"])
        original_path.write_bytes(b"test-only corrupted baseline")

        queued = self.check(monitor, "unknown-prior-source")
        with patch("server.oevira.acquisition.acquire_url", return_value=self.acquisition(b"current", "Release beta", etag="b")):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        result = self.request("get", f"/api/monitor-runs/{queued.json['data']['id']}", headers=self.auth_headers()).json["data"]
        current_source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(result["sources"][0]["status"], "unknown")
        self.assertIsNone(result["sources"][0]["changed"])
        self.assertIn("failed SHA-256 verification", result["sources"][0]["reason"])
        self.assertNotEqual(current_source["evaluation_cursor"]["material_id"], baseline["sources"][0]["material_id"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

        next_check = self.check(monitor, "after-unknown-baseline")
        with patch("server.oevira.acquisition.acquire_url", return_value=self.acquisition(b"next", "Release gamma", etag="c")):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        after_unknown = self.request("get", f"/api/monitor-runs/{next_check.json['data']['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(after_unknown["sources"][0]["status"], "signal")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 1)

    def test_new_rule_rechecks_material_saved_under_suppressed_old_rule(self) -> None:
        monitor = self.create_monitor("https://news.example/rule-recheck", keywords=["alpha"])
        first = self.acquire_and_harvest(
            monitor, b"<html><body>Release alpha</body></html>", "Release alpha", etag="a",
        )
        baseline_material_id = first["sources"][0]["material_id"]

        queued = self.check(monitor, "old-rule-pending")
        run = queued.json["data"]
        job_id = run["sources"][0]["job_id"]
        changed_bytes = b"<html><body>Release beta</body></html>"
        with patch("server.oevira.acquisition.acquire_url", return_value=self.acquisition(changed_bytes, "Release beta", etag="b")):
            self.assertTrue(run_once(self.app))

        changed_config = self.request(
            "patch", f"/api/monitors/{monitor['id']}",
            json={"base_version": monitor["version"], "keywords": ["beta"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(changed_config.status_code, 200, changed_config.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"], "completed")
        tick_monitoring(self.app)
        stale = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(stale["sources"][0]["status"], "stale_configuration")
        stale_source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(stale_source["evaluation_cursor"]["material_id"], baseline_material_id)
        self.assertEqual(stale_source["source_cursor"]["sha256"], hashlib.sha256(changed_bytes).hexdigest())
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": changed_config.json["data"]["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        current = enabled.json["data"]
        recheck = self.check(current, "new-rule-recheck")
        with patch("server.oevira.acquisition.acquire_url", return_value={
            "status": "unchanged", "url": "https://news.example/rule-recheck",
            "captured_at": utc_now(), "etag": "b", "last_modified": None,
        }):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        result = self.request("get", f"/api/monitor-runs/{recheck.json['data']['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(result["sources"][0]["status"], "signal", result["sources"][0])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 2)
        signal = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers()).json["data"][0]
        self.assertEqual(signal["config_version"], current["version"])
        self.assertIn("规则版本", signal["summary"])
        self.assertEqual(signal["evidence"][0]["change"], "changed")
        self.assertIn("beta", signal["evidence"][0]["quote"].casefold())

    def test_controlled_http_acquisition_saves_material_and_harvests_signal_after_restart(self) -> None:
        state = {
            "etag": '"v1"',
            "content": b"<html><body><main><h1>Release report</h1><p>Alpha is available.</p></main></body></html>",
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/robots.txt":
                    body = b"User-agent: *\nAllow: /\n"
                    status = 200
                    content_type = "text/plain"
                else:
                    content_type = "text/html; charset=utf-8"
                    if self.headers.get("If-None-Match") == state["etag"]:
                        body = b""
                        status = 304
                    else:
                        body = state["content"]
                        status = 200
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if self.path != "/robots.txt":
                    self.send_header("ETag", state["etag"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        fixture_url = "http://monitor-fixture.test/brand"

        def allow_fixture_target(url: str):
            parts = urlsplit(url)
            if parts.hostname != "monitor-fixture.test" or (parts.port not in {None, 80}):
                raise AcquisitionError("SOURCE_TARGET_DENIED", "The controlled test source was not expected.")
            return url, parts.hostname, 80, "127.0.0.1"

        def read_fixture(url: str, *, headers: dict, deadline: float,
                         max_bytes: int = 8 * 1024 * 1024, allow_url=None):
            parts = urlsplit(url)
            if parts.hostname != "monitor-fixture.test" or (allow_url is not None and not allow_url(url)):
                raise AcquisitionError("SOURCE_TARGET_DENIED", "The controlled test source was not authorized.")
            remaining = deadline - time.monotonic()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=max(0.1, min(3.0, remaining)))
            request_headers = dict(headers)
            request_headers["Host"] = "monitor-fixture.test"
            connection.request("GET", parts.path or "/", headers=request_headers)
            response = connection.getresponse()
            body = response.read(max_bytes + 1)
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            status = response.status
            connection.close()
            if len(body) > max_bytes:
                raise AcquisitionError("SOURCE_TOO_LARGE", "The controlled fixture exceeded its read bound.")
            return url, status, response_headers, body

        try:
            monitor = self.create_monitor(fixture_url, keywords=["beta"])
            first_check = self.check(monitor, "controlled-http-baseline")
            self.assertEqual(first_check.status_code, 202, first_check.get_data(as_text=True))
            with patch("server.oevira.acquisition.public_target", side_effect=allow_fixture_target), patch(
                "server.oevira.acquisition._read_public", side_effect=read_fixture,
            ):
                self.assertTrue(run_once(self.app))
            first_job = self.store.row("SELECT status, error_code, error_message, material_id FROM jobs WHERE id = ?", (first_check.json["data"]["sources"][0]["job_id"],))
            self.assertEqual(first_job["status"], "completed", dict(first_job))
            self.assertTrue(first_job["material_id"])
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 1)

            restarted_app = create_app(self.root, expected_port=8317)
            self.assertEqual(tick_monitoring(restarted_app)["harvested"], 1)
            first_run = self.request("get", f"/api/monitor-runs/{first_check.json['data']['id']}", headers=self.auth_headers()).json["data"]
            self.assertEqual(first_run["sources"][0]["status"], "baseline_established")
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

            state["etag"] = '"v2"'
            state["content"] = (
                b"<html><body><main><h1>Release report</h1><p>Alpha is available.</p>"
                b"<p>Beta now ships.</p></main></body></html>"
            )
            second_check = self.check(monitor, "controlled-http-change")
            self.assertEqual(second_check.status_code, 202, second_check.get_data(as_text=True))
            with patch("server.oevira.acquisition.public_target", side_effect=allow_fixture_target), patch(
                "server.oevira.acquisition._read_public", side_effect=read_fixture,
            ):
                self.assertTrue(run_once(self.app))
            second_job = self.store.row("SELECT status, material_id FROM jobs WHERE id = ?", (second_check.json["data"]["sources"][0]["job_id"],))
            self.assertEqual(second_job["status"], "completed")
            self.assertNotEqual(second_job["material_id"], first_job["material_id"])
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 2)

            cold_restart_app = create_app(self.root, expected_port=8317)
            self.assertEqual(tick_monitoring(cold_restart_app)["harvested"], 1)
            result = self.request("get", f"/api/monitor-runs/{second_check.json['data']['id']}", headers=self.auth_headers()).json["data"]
            self.assertEqual(result["sources"][0]["status"], "signal")
            signal = self.request("get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers()).json["data"][0]
            added = next(item for item in signal["evidence"] if item["change"] == "added")
            self.assertEqual(added["quote"], "Beta now ships.")
            segment = self.store.row(
                "SELECT id FROM material_segments WHERE material_id = ? AND text = ?",
                (second_job["material_id"], "Beta now ships."),
            )
            self.assertIsNotNone(segment)
            self.assertEqual(added["segment_id"], segment["id"])

            created_work = self.request(
                "post", f"/api/monitor-signals/{signal['id']}/work",
                json={"title": "复核公开新品变化"},
                headers={**self.auth_headers(), "Idempotency-Key": "controlled-http-signal-work"},
            )
            self.assertEqual(created_work.status_code, 201, created_work.get_data(as_text=True))
            work = created_work.json["data"]
            self.assertIn(second_job["material_id"], work["material_ids"])

            reopened_app = create_app(self.root, expected_port=8317)
            self.client = reopened_app.test_client()
            reopened_work = self.request(
                "get", f"/api/works/{work['id']}", headers=self.auth_headers(),
            )
            self.assertEqual(reopened_work.status_code, 200, reopened_work.get_data(as_text=True))
            self.assertIn(second_job["material_id"], reopened_work.json["data"]["material_ids"])
            reopened_signals = self.request(
                "get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(),
            )
            self.assertEqual(reopened_signals.status_code, 200, reopened_signals.get_data(as_text=True))
            self.assertEqual(reopened_signals.json["data"][0]["id"], signal["id"])

            self.assertEqual(tick_monitoring(cold_restart_app)["harvested"], 0)
            self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 1)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_scheduled_monitor_recovers_after_rate_limit_across_worker_restarts(self) -> None:
        source_url = "https://news.example/scheduled-release"
        monitor = self.create_monitor(source_url, frequency_seconds=60, keywords=["beta"])
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": monitor["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        first_due = datetime.fromisoformat(enabled.json["data"]["next_run_at"].replace("Z", "+00:00"))
        self.assertEqual(tick_monitoring(self.app, first_due)["scheduled"], 1)
        first_run = self.request(
            "get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers(),
        ).json["data"][0]
        baseline = self.acquisition(
            b"<html><body><main>Release alpha.</main></body></html>", "Release alpha.", etag="scheduled-1",
        )
        baseline.update({"url": source_url, "requested_url": source_url})
        with patch("server.oevira.acquisition.acquire_url", return_value=baseline):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app, first_due + timedelta(seconds=1))
        established = self.request(
            "get", f"/api/monitor-runs/{first_run['id']}", headers=self.auth_headers(),
        ).json["data"]
        self.assertEqual(established["sources"][0]["status"], "baseline_established")

        second_app = create_app(self.root, expected_port=8317)
        second_due_value = self.store.row(
            "SELECT next_run_at FROM monitor_rules WHERE id = ?", (monitor["id"],),
        )["next_run_at"]
        second_due = datetime.fromisoformat(second_due_value.replace("Z", "+00:00"))
        self.assertEqual(tick_monitoring(second_app, second_due)["scheduled"], 1)
        runs = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"]
        failed_run = runs[0]
        failed_job_id = failed_run["sources"][0]["job_id"]
        with patch(
            "server.oevira.acquisition.acquire_url",
            side_effect=AcquisitionError("SOURCE_RATE_LIMITED", "The source returned HTTP 429.", status=429, retry_after="5"),
        ):
            self.assertTrue(run_once(second_app))
        tick_monitoring(second_app, second_due + timedelta(seconds=1))
        failed = self.request(
            "get", f"/api/monitor-runs/{failed_run['id']}", headers=self.auth_headers(),
        ).json["data"]
        self.assertEqual(failed["sources"][0]["status"], "rate_limited")
        failed_job = self.store.row("SELECT updated_at, payload_json FROM jobs WHERE id = ?", (failed_job_id,))
        failure_details = json.loads(failed_job["payload_json"])["_acquisition_failure"]
        self.assertEqual(failure_details["retry_after"], "5")
        manual_during_cooldown = self.check(enabled.json["data"], "scheduled-recovery-cooldown")
        self.assertEqual(manual_during_cooldown.status_code, 202, manual_during_cooldown.get_data(as_text=True))
        self.assertEqual(manual_during_cooldown.json["data"]["sources"][0]["status"], "cooldown")
        self.assertIsNone(manual_during_cooldown.json["data"]["sources"][0]["job_id"])

        third_app = create_app(self.root, expected_port=8317)
        third_due_value = self.store.row(
            "SELECT next_run_at FROM monitor_rules WHERE id = ?", (monitor["id"],),
        )["next_run_at"]
        third_due = datetime.fromisoformat(third_due_value.replace("Z", "+00:00"))
        self.assertEqual(tick_monitoring(third_app, third_due)["scheduled"], 1)
        recovered_runs = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"]
        recovered_run = recovered_runs[0]
        changed = self.acquisition(
            b"<html><body><main>Release alpha.</main><p>Beta now ships.</p></body></html>",
            "Release alpha.\nBeta now ships.", etag="scheduled-3",
        )
        changed.update({"url": source_url, "requested_url": source_url})
        with patch("server.oevira.acquisition.acquire_url", return_value=changed):
            self.assertTrue(run_once(third_app))
        tick_monitoring(third_app, third_due + timedelta(seconds=1))
        recovered = self.request(
            "get", f"/api/monitor-runs/{recovered_run['id']}", headers=self.auth_headers(),
        ).json["data"]
        self.assertEqual(recovered["sources"][0]["status"], "signal")
        signal = self.request(
            "get", f"/api/monitors/{monitor['id']}/signals", headers=self.auth_headers(),
        ).json["data"][0]
        self.assertEqual(signal["evidence"][0]["quote"], "Beta now ships.")

        cold_restart = create_app(self.root, expected_port=8317)
        self.client = cold_restart.test_client()
        self.assertEqual(tick_monitoring(cold_restart, third_due + timedelta(seconds=2))["harvested"], 0)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 1)

    def test_304_records_a_check_without_duplicate_signal_or_losing_capture_time(self) -> None:
        monitor = self.create_monitor("https://news.example/brand")
        body = b"<html><body>Release alpha</body></html>"
        first = self.acquire_and_harvest(monitor, body, "Release alpha", etag="stable")
        first_observation = self.store.row("SELECT * FROM monitor_observations WHERE run_id = ?", (first["id"],))
        queued = self.check(monitor)
        run = queued.json["data"]
        unchanged = {"status": "unchanged", "url": "https://news.example/brand", "captured_at": utc_now(), "etag": "stable", "last_modified": None}
        with patch("server.oevira.acquisition.acquire_url", return_value=unchanged):
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        second_run = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        second_observation = self.store.row("SELECT * FROM monitor_observations WHERE run_id = ?", (run["id"],))
        self.assertEqual(second_run["sources"][0]["status"], "no_change")
        self.assertEqual(second_observation["material_id"], first_observation["material_id"])
        self.assertNotEqual(second_observation["captured_at"], first_observation["captured_at"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_pause_cancels_queued_source_job_before_public_fetch(self) -> None:
        monitor = self.create_monitor("https://news.example/brand")
        enabled = self.request("post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": monitor["version"]}, headers=self.auth_headers()).json["data"]
        tick_monitoring(self.app, enabled["next_run_at"])
        run = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"][0]
        job_id = run["sources"][0]["job_id"]
        paused = self.request("post", f"/api/monitors/{monitor['id']}/pause", json={"base_version": enabled["version"]}, headers=self.auth_headers())
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"], "cancelled")
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("paused queued job reached public source")):
            self.assertFalse(run_once(self.app))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM materials")["count"], 0)
        refreshed = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(refreshed["sources"][0]["status"], "cancelled")
        self.assertFalse(self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]["pending"])

        manual_monitor = self.create_monitor("https://news.example/manual")
        manual_run = self.check(manual_monitor).json["data"]
        manual_job = manual_run["sources"][0]["job_id"]
        repeated_pause = self.request(
            "post", f"/api/monitors/{manual_monitor['id']}/pause", json={"base_version": manual_monitor["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(repeated_pause.status_code, 200)
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id = ?", (manual_job,))["status"], "cancelled")
        with patch("server.oevira.acquisition.acquire_url", side_effect=AssertionError("paused manual check reached public source")):
            self.assertFalse(run_once(self.app))

    def test_completed_old_configuration_is_preserved_without_alert_after_pause_and_resume(self) -> None:
        monitor = self.create_monitor("https://news.example/brand")
        baseline = b"<html><body>Release alpha</body></html>"
        baseline_run = self.acquire_and_harvest(monitor, baseline, "Release alpha", etag="a")
        baseline_material_id = baseline_run["sources"][0]["material_id"]

        check = self.check(monitor)
        run = check.json["data"]
        job_id = run["sources"][0]["job_id"]
        changed = b"<html><body>Release beta</body></html>"
        with patch("server.oevira.acquisition.acquire_url", return_value=self.acquisition(changed, "Release beta", etag="b")):
            self.assertTrue(run_once(self.app))
        # The source bytes and Material have been saved, but the monitor run has
        # not yet been harvested when the user pauses and resumes on a new version.
        with self.store.transaction() as connection:
            connection.execute("UPDATE jobs SET status = 'partial' WHERE id = ?", (job_id,))
        paused = self.request("post", f"/api/monitors/{monitor['id']}/pause", json={"base_version": monitor["version"]}, headers=self.auth_headers())
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        enabled = self.request("post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": paused.json["data"]["version"]}, headers=self.auth_headers())
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))

        tick_monitoring(self.app)
        old_run = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(old_run["sources"][0]["status"], "stale_configuration")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)
        self.assertEqual(source["last_material_id"], baseline_material_id)
        self.assertEqual(source["evaluation_cursor"]["material_id"], baseline_material_id)
        self.assertEqual(source["source_cursor"]["sha256"], hashlib.sha256(changed).hexdigest())
        saved = self.store.row("SELECT last_sha256 FROM monitor_sources WHERE id = ?", (source["id"],))
        self.assertEqual(saved["last_sha256"], hashlib.sha256(changed).hexdigest())

    def test_rate_limit_is_not_reported_as_no_change_and_private_urls_are_rejected(self) -> None:
        invalid = self.request(
            "post", "/api/monitors", json=self.config("http://127.0.0.1/private"),
            headers={**self.auth_headers(), "Idempotency-Key": "private-monitor"},
        )
        self.assertEqual(invalid.status_code, 400)

        monitor = self.create_monitor("https://news.example/brand")
        run = self.check(monitor).json["data"]
        job_id = run["sources"][0]["job_id"]
        with patch(
            "server.oevira.acquisition.acquire_url",
            side_effect=AcquisitionError("SOURCE_RATE_LIMITED", "The source returned HTTP 429.", status=429, retry_after="600"),
        ):
            self.assertTrue(run_once(self.app))
        failed_job = self.store.row("SELECT updated_at, payload_json FROM jobs WHERE id = ?", (job_id,))
        failure_details = json.loads(failed_job["payload_json"]).get("_acquisition_failure")
        self.assertEqual(failure_details, {"http_status": 429, "retry_after": "600"})
        failed_at = failed_job["updated_at"]
        tick_monitoring(self.app)
        updated = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        source = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]["sources"][0]
        self.assertEqual(updated["sources"][0]["status"], "rate_limited")
        self.assertEqual(source["status"], "rate_limited")
        retry_at = datetime.fromisoformat(source["retry_not_before"].replace("Z", "+00:00"))
        self.assertGreaterEqual(retry_at, datetime.fromisoformat(failed_at.replace("Z", "+00:00")) + timedelta(seconds=600))
        self.assertNotEqual(source["status"], "no_change")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 0)

        cooldown_run = self.check(monitor, "retry-during-cooldown")
        self.assertEqual(cooldown_run.status_code, 202, cooldown_run.get_data(as_text=True))
        self.assertEqual(cooldown_run.json["data"]["sources"][0]["status"], "cooldown")
        self.assertIn(source["retry_not_before"], cooldown_run.json["data"]["sources"][0]["reason"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 1)

        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable",
            json={"base_version": monitor["version"]}, headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        tick_monitoring(self.app, retry_at + timedelta(seconds=1))
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 2)

    def test_one_source_failure_does_not_block_another_source(self) -> None:
        monitor = self.create_monitor("https://news.example/one", "https://news.example/two")
        run = self.check(monitor).json["data"]

        def read_source(url: str, **_kwargs):
            if url.endswith("/one"):
                raise AcquisitionError("SOURCE_RATE_LIMITED", "The source returned HTTP 429", status=429)
            return {**self.acquisition(b"<html><body>Release alpha</body></html>", "Release alpha", etag="two"), "url": url, "requested_url": url}

        with patch("server.oevira.acquisition.acquire_url", side_effect=read_source):
            self.assertTrue(run_once(self.app))
            self.assertTrue(run_once(self.app))
        tick_monitoring(self.app)
        result = self.request("get", f"/api/monitor-runs/{run['id']}", headers=self.auth_headers()).json["data"]
        by_url = {item["url"]: item for item in result["sources"]}
        self.assertEqual(by_url["https://news.example/one"]["status"], "rate_limited")
        self.assertEqual(by_url["https://news.example/two"]["status"], "baseline_established")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["attempted"], 2)
        self.assertEqual(result["coverage"]["checked"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 1)

    def test_excluded_terms_suppress_the_signal_but_preserve_the_observation(self) -> None:
        monitor = self.create_monitor("https://news.example/brand", exclude_keywords=["beta"])
        self.acquire_and_harvest(monitor, b"<html><body>Release alpha</body></html>", "Release alpha", etag="a")
        changed_run = self.acquire_and_harvest(monitor, b"<html><body>Release beta</body></html>", "Release beta", etag="b")
        self.assertEqual(changed_run["sources"][0]["status"], "excluded")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_observations")["count"], 2)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM monitor_signals")["count"], 0)

    def test_stop_time_stops_paused_or_enabled_monitor_without_a_check(self) -> None:
        stop = datetime.now(timezone.utc) + timedelta(minutes=5)
        monitor = self.create_monitor("https://news.example/brand", stop_at=stop.isoformat())
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": monitor["version"]}, headers=self.auth_headers(),
        ).json["data"]
        tick_monitoring(self.app, stop + timedelta(seconds=1))
        stopped = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers()).json["data"]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["version"], enabled["version"] + 1)
        self.assertIsNone(stopped["next_run_at"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE workspace_id = ?", (self.identity["workspace"]["id"],))["count"], 0)

    def test_app_factory_and_worker_loop_run_monitoring_through_shared_queue(self) -> None:
        monitor = self.create_monitor("https://news.example/worker-loop")
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable", json={"base_version": monitor["version"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))

        stop_event = threading.Event()
        worker = threading.Thread(target=run_forever, args=(self.app, stop_event), daemon=True)
        result = self.acquisition(b"<p>release note</p>", "release note")
        with patch("server.oevira.acquisition.acquire_url", return_value=result) as acquire:
            worker.start()
            deadline = time.monotonic() + 5
            source = None
            while time.monotonic() < deadline:
                source = self.store.row(
                    "SELECT status FROM monitor_sources WHERE monitor_id = ? AND active = 1",
                    (monitor["id"],),
                )
                if source and source["status"] == "baseline_established":
                    break
                time.sleep(0.01)
            stop_event.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive(), "The isolated worker should stop promptly")
        self.assertEqual(source["status"], "baseline_established")
        acquire.assert_called_once()
        runs = self.request("get", f"/api/monitors/{monitor['id']}/runs", headers=self.auth_headers()).json["data"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["coverage"]["checked"], 1)
        state = self.app.extensions["oevira.worker_state"]
        self.assertEqual(state["status"], "stopped")
        self.assertIsNotNone(state["last_heartbeat_at"])

    def test_monitor_reads_are_workspace_isolated_and_manual_retry_is_idempotent(self) -> None:
        monitor = self.create_monitor("https://news.example/brand")
        headers = {**self.auth_headers(), "Idempotency-Key": "same-check"}
        request = {"base_version": monitor["version"]}
        first = self.request("post", f"/api/monitors/{monitor['id']}/check", json=request, headers=headers)
        repeated = self.request("post", f"/api/monitors/{monitor['id']}/check", json=request, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(first.json["data"]["id"], repeated.json["data"]["id"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE kind = 'acquire_url'")["count"], 1)

        user_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "另一工作区", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "另一个工作区", now))
            connection.execute("INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)", (workspace_id, user_id, now))
            connection.execute("INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), _digest(token), user_id, workspace_id, now, expires_at))
        other = self.request("get", "/api/monitors", headers=self.auth_headers(token))
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json["data"], [])
        hidden = self.request("get", f"/api/monitors/{monitor['id']}", headers=self.auth_headers(token))
        self.assertEqual(hidden.status_code, 404)


if __name__ == "__main__":
    unittest.main()
