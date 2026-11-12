from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import base64
import hashlib
import io
import json
import wave
import zipfile
from pathlib import Path
import secrets
import threading
import tempfile
import unittest
import uuid
from unittest.mock import patch

from flask import g

from server.oevira import create_app
from server.oevira.auth import _digest, issue_local_capability, utc_now
from server.oevira.acquisition import AcquisitionError
from server.oevira.intelligence import (
    ExplicitModelSelection,
    ExternalModelApproval,
    IntelligenceService,
    ModelInputAuthorization,
    install_intelligence_schema,
)
from server.oevira.intelligence_base import IntelligenceError
from server.oevira.intelligence_api import process_intelligence_job, register_routes as register_intelligence_routes
from server.oevira.intelligence_cpu import (
    CPUTrainingBlocked,
    TrainingDataAdmission,
    TASK_SCHEMAS,
    score_cpu_candidate,
)
from server.oevira.intelligence_math import (
    IntelligenceMathError,
    MeanPayoffBudget,
    _LabelCall,
    _WorkBudget,
    _discounted_floor,
    _read_game,
    _run_label,
    evaluate_mean_payoff_winning_region,
)
from server.oevira.intelligence_codex import (
    CODEX_APP_SERVER_VERSION,
    CODEX_TOOL_NAME_HASH_CHARACTERS,
    CODEX_TOOL_NAME_PREFIX,
    CodexAppServerGateway,
    CodexAppServerRuntimeResolver,
    _only_product_tools,
    _validate_responses_event,
)
from server.oevira.intelligence_provider import (
    RESPONSES_PROTOCOL,
    UserConfiguredChatGateway,
    UserConfiguredModelConnection,
    _connection_destination,
    _open_pinned_https,
)
from server.oevira.intelligence_skills import (
    GitHubSkillSource,
    SkillLifecycleError,
    install_intelligence_skill_schema,
)
from server.oevira.intelligence_models import ModelSettingsError
from server.oevira.intelligence_mcp import (
    MCP_CURRENT_PROTOCOL,
    MCP_LEGACY_PROTOCOL,
    McpClientError,
    McpHttpClient,
)
from server.oevira.intelligence_script import (
    RestrictedScriptError,
    inspect_script,
    run_restricted_script,
)
from server.oevira.intelligence_retrieval import (
    _search_preserved_research_candidates,
    revalidate_preserved_research_candidate_ref,
)
from server.oevira.store import Store


BASE_URL = "http://127.0.0.1:8317"
ORIGIN = BASE_URL
CODEX_TEST_RUNTIME_ASSET = Path(
    r"D:\寰意\.runtime\reconstruction\codex-runtime\codex-app-server-package-x86_64-pc-windows-msvc.tar.gz",
)
CODEX_TEST_RUNTIME_LICENSE = Path(
    r"D:\寰意\.runtime\reconstruction\codex-runtime\codex-source-LICENSE",
)


def _canonical_digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="oevira-intelligence-", dir=r"D:\寰意\.runtime",
        )
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        install_intelligence_schema(self.store)
        self.service = IntelligenceService(self.store)
        self.app.extensions["oevira.intelligence"] = self.service
        register_intelligence_routes(self.app, self.store, self.service)
        self.capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, self.capability_file)
        capability = self.capability_file.read_text(encoding="ascii")
        response = self.request(
            "post", "/api/local-session",
            json={"capability": capability, "display_name": "研究者"},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        self.identity = {**response.json["data"], "role": "owner"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_skill_schema_fresh_reopen_and_legacy_migration_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oevira-skill-schema-", dir=r"D:\寰意\.runtime") as raw_root:
            root = Path(raw_root)
            first_app = create_app(root)
            self.assertIsNotNone(first_app.extensions["oevira.store"])
            second_app = create_app(root)
            version_columns = [
                row["name"]
                for row in second_app.extensions["oevira.store"].rows(
                    "PRAGMA table_info(intelligence_skill_versions)"
                )
            ]
            self.assertEqual(version_columns.count("github_connection_id"), 1)
            self.assertEqual(version_columns.count("github_connection_revision"), 1)
            candidate_columns = [
                row["name"]
                for row in second_app.extensions["oevira.store"].rows(
                    "PRAGMA table_info(intelligence_skill_candidates)"
                )
            ]
            self.assertEqual(candidate_columns.count("github_connection_id"), 1)
            self.assertEqual(candidate_columns.count("github_connection_revision"), 1)

        with tempfile.TemporaryDirectory(prefix="oevira-skill-schema-old-", dir=r"D:\寰意\.runtime") as raw_root:
            store = Store(raw_root)
            connection = store.connect()
            try:
                connection.executescript("""
                    CREATE TABLE intelligence_skills(
                        skill_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, status TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE intelligence_github_connections(
                        connection_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
                        name TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                        credential_ciphertext BLOB, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        UNIQUE(workspace_id, owner_user_id, name)
                    );
                    CREATE TABLE intelligence_skill_candidates(
                        candidate_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_by TEXT NOT NULL,
                        source_json TEXT NOT NULL, manifest_json TEXT NOT NULL, files_json TEXT NOT NULL,
                        license_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL, installed_skill_id TEXT, created_at TEXT NOT NULL
                    );
                    CREATE TABLE intelligence_skill_versions(
                        skill_id TEXT NOT NULL, workspace_id TEXT NOT NULL, version_number INTEGER NOT NULL,
                        git_revision TEXT NOT NULL, content_sha256 TEXT NOT NULL, tool_schema_sha256 TEXT NOT NULL,
                        manifest_json TEXT NOT NULL, files_json TEXT NOT NULL, license_json TEXT NOT NULL,
                        tool_names_json TEXT NOT NULL, rights_review_sha256 TEXT NOT NULL, test_receipt_json TEXT,
                        lifecycle_state TEXT NOT NULL, installed_at TEXT NOT NULL, installed_by TEXT NOT NULL,
                        PRIMARY KEY(skill_id, version_number)
                    );
                """)
            finally:
                connection.close()
            install_intelligence_skill_schema(store)
            install_intelligence_skill_schema(store)
            migrated = [row["name"] for row in store.rows("PRAGMA table_info(intelligence_skill_versions)")]
            self.assertEqual(migrated.count("github_connection_id"), 1)
            self.assertEqual(migrated.count("github_connection_revision"), 1)
            migrated_candidates = [
                row["name"] for row in store.rows("PRAGMA table_info(intelligence_skill_candidates)")
            ]
            self.assertEqual(migrated_candidates.count("github_connection_id"), 1)
            self.assertEqual(migrated_candidates.count("github_connection_revision"), 1)
            migrated_connections = [
                row["name"] for row in store.rows("PRAGMA table_info(intelligence_github_connections)")
            ]
            for column in ("credential_protection", "provider_login", "verified_at", "validation_status"):
                self.assertEqual(migrated_connections.count(column), 1)

    def create_work(self, title: str = "行业核验") -> dict:
        response = self.request(
            "post", "/api/works",
            json={"title": title, "goal": "核对来源材料中的预算", "brief": {}},
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def create_material(self, work_id: str, *, title: str = "预算材料",
                        text: str = "2026年度预算为120万元。审批日期为2026年10月。") -> dict:
        response = self.request(
            "post", "/api/materials",
            json={"title": title, "filename": f"{title}.txt", "text": text, "work_id": work_id},
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.json["data"]

    def install_test_github_skill(self, *, tools: list[str], reference: str = "main",
                                  instruction_suffix: str = "", install: bool = True) -> dict:
        skill_markdown = (
            "---\nname: oevira-artifact-revision\n"
            "description: Create and revise an evidence-backed OEVIRA draft.\n"
            f"allowed-tools: [{', '.join(tools)}]\n---\n"
            "# OEVIRA draft workflow\n"
            "Use the current task citations, create a draft with the approved Artifact tool, "
            "and preserve user scope when revising. Never claim unsupported facts. " + instruction_suffix
        ).encode("utf-8")
        license_bytes = b"SPDX-License-Identifier: MIT\nCopyright 2026 Example\n"
        contents = {"SKILL.md": skill_markdown, "LICENSE": license_bytes}
        blob_ids = {
            name: hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
            for name, content in contents.items()
        }
        commit_sha = hashlib.sha1(reference.encode("utf-8")).hexdigest()
        tree_sha = hashlib.sha1(("tree:" + reference).encode("utf-8")).hexdigest()
        files_by_blob = {blob_ids[name]: content for name, content in contents.items()}

        def transport(path: str):
            if path == "/search/repositories?q=agent+skills&sort=stars&order=desc&per_page=30&page=1":
                payload = {"total_count": 30, "incomplete_results": False, "items": [
                    {"full_name": "example/skills", "html_url": "https://github.com/example/skills",
                     "description": "Public Skills", "default_branch": "main", "stargazers_count": 42,
                     "updated_at": "2026-11-03T00:00:00Z", "owner": {"login": "example"},
                     "license": {"spdx_id": "MIT"}},
                ]}
                return 200, {"x-ratelimit-remaining": "57",
                             "link": '<https://api.github.com/search/repositories?q=agent+skills&sort=stars&order=desc&per_page=30&page=2>; rel="next"'}, json.dumps(payload).encode()
            if path == "/repos/example/skills":
                return 200, {}, json.dumps({"private": False, "default_branch": "main",
                                            "license": {"spdx_id": "MIT"}}).encode()
            if path == f"/repos/example/skills/commits/{reference}":
                return 200, {}, json.dumps({"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}).encode()
            if path == f"/repos/example/skills/git/trees/{tree_sha}?recursive=1":
                tree = [
                    {"path": "skills/draft/SKILL.md", "type": "blob", "mode": "100644",
                     "sha": blob_ids["SKILL.md"], "size": len(skill_markdown)},
                    {"path": "LICENSE", "type": "blob", "mode": "100644",
                     "sha": blob_ids["LICENSE"], "size": len(license_bytes)},
                ]
                return 200, {}, json.dumps({"tree": tree, "truncated": False}).encode()
            if path.startswith("/repos/example/skills/git/blobs/"):
                blob_id = path.rsplit("/", 1)[-1]
                content = files_by_blob[blob_id]
                payload = {"sha": blob_id, "encoding": "base64",
                           "content": base64.b64encode(content).decode("ascii"), "size": len(content)}
                return 200, {}, json.dumps(payload).encode()
            raise AssertionError(f"Unexpected GitHub request: {path}")

        self.service.skills.source = GitHubSkillSource(transport=transport)
        discovered = self.request(
            "post", "/api/intelligence/skills/discover", json={"query": "agent skills", "page": 1},
            headers=self.auth_headers(),
        )
        self.assertEqual(discovered.status_code, 200, discovered.get_data(as_text=True))
        self.assertEqual(discovered.json["data"]["items"][0]["repository"], "example/skills")
        self.assertEqual(discovered.json["data"]["next_page"], 2)
        inspected = self.request(
            "post", "/api/intelligence/skills/inspect",
            json={"repository": "example/skills", "reference": reference, "skill_path": "skills/draft"},
            headers=self.auth_headers(),
        )
        self.assertEqual(inspected.status_code, 201, inspected.get_data(as_text=True))
        candidate = inspected.json["data"]
        self.assertEqual(candidate["revision"], commit_sha)
        self.assertEqual(candidate["license"]["rights_review"], "REQUIRES_EXPLICIT_USER_REVIEW")
        if not install:
            return {"candidate": candidate}
        installed = self.request(
            "post", "/api/intelligence/skills/install",
            json={
                "candidate_id": candidate["candidate_id"],
                "candidate_sha256": candidate["content_sha256"],
                "license_sha256": candidate["license"]["file_sha256"],
                "rights_reviewed": True, "tool_names": tools,
            },
            headers={**self.auth_headers(), "Idempotency-Key": "install-test-skill-" + reference},
        )
        self.assertEqual(installed.status_code, 201, installed.get_data(as_text=True))
        skill = installed.json["data"]
        tested = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/self-test",
            json={"version": skill["version"], "content_sha256": skill["content_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "self-test-test-skill-" + reference},
        )
        self.assertEqual(tested.status_code, 200, tested.get_data(as_text=True))
        enabled = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/enable",
            json={"version": skill["version"], "content_sha256": skill["content_sha256"],
                  "tool_schema_sha256": skill["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "enable-test-skill-" + reference},
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        skill["candidate"] = candidate
        return skill

    def test_real_material_search_correctable_memory_and_scope_denial(self) -> None:
        work = self.create_work()
        material = self.create_material(work["id"])

        evidence = self.service.search_materials(
            self.identity, "2026年度预算", work_id=work["id"],
        )
        self.assertEqual(evidence["status"], "complete")
        self.assertTrue(evidence["coverage"]["complete_scope_scanned"])
        self.assertGreater(evidence["coverage"]["scored_passages"], 0)
        citation = evidence["citations"][0]
        self.assertEqual(citation["material_id"], material["id"])
        self.assertIn(citation["quote"], material["text"])
        located = next(item for item in evidence["evidence"]
                       if item["segment_id"] == citation["segment_id"])
        self.assertEqual(material["text"][located["start_offset"]:located["end_offset"]], citation["quote"])
        self.assertEqual(located["offset_unit"], "unicode_codepoint")
        self.assertEqual(located["evidence_state"], "UNVERIFIED_SOURCE_TEXT")

        memory = self.service.memory.create(
            self.identity, kind="correction", work_id=work["id"],
            content="预算中的120万元是含税金额。", source_refs=[citation],
        )
        corrected = self.service.memory.correct(
            self.identity, memory["memory_id"], base_version=memory["version"],
            content="更正：120万元为不含税金额。",
        )
        self.assertEqual(corrected["version"], 2)
        self.assertEqual(corrected["status"], "active")
        self.assertEqual(corrected["canonical_state"], "NOT_CANONICAL")
        self.assertEqual(corrected["training_use"], "NOT_GRANTED")
        context = self.service.memory.context(self.identity, work_id=work["id"])
        self.assertEqual([item["content"] for item in context["items"]], ["更正：120万元为不含税金额。"])
        self.assertEqual(context["parameter_training"], "NOT_GRANTED")

        forgotten = self.service.memory.forget(
            self.identity, memory["memory_id"], base_version=corrected["version"],
        )
        self.assertEqual(forgotten["content_retained"], False)
        history = self.service.memory.list(self.identity, work_id=work["id"], include_history=True)
        self.assertTrue(all(item["content"] is None and item["source_refs"] == [] for item in history))

        other_identity = self._create_other_identity()
        with self.assertRaises(IntelligenceError) as raised:
            self.service.read_material(other_identity, material["id"])
        self.assertEqual(raised.exception.code, "SOURCE_SCOPE_NOT_FOUND")

    def test_memory_http_lifecycle_persists_corrects_and_forgets_exact_version(self) -> None:
        unauthenticated = self.app.test_client().get(
            "/api/intelligence/memories", base_url=BASE_URL,
        )
        self.assertEqual(unauthenticated.status_code, 401)

        work = self.create_work("记忆API闭环")
        material = self.create_material(work["id"])
        citation = self.service.search_materials(
            self.identity, "2026年度预算", work_id=work["id"],
        )["citations"][0]
        created = self.request(
            "post", "/api/intelligence/memories",
            json={
                "kind": "correction", "work_id": work["id"],
                "content": "这份预算日期为2026年10月，金额为120万元。",
                "source_refs": [citation],
            },
            headers=self.auth_headers(),
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        saved = created.json["data"]
        self.assertEqual(saved["source_state"], "current")
        self.assertEqual(saved["training_use"], "NOT_GRANTED")
        prepared_with_memory = self.service.prepare_context(
            self.identity, "在当前Work中核对该记忆。", work_id=work["id"],
        )

        stopped = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/use",
            json={"base_version": 1, "enabled": False}, headers=self.auth_headers(),
        )
        self.assertEqual(stopped.status_code, 200, stopped.get_data(as_text=True))
        self.assertEqual(stopped.json["data"]["version"], 2)
        self.assertFalse(stopped.json["data"]["use_enabled"])
        stopped_context = self.service.memory.context(self.identity, work_id=work["id"])
        self.assertEqual(stopped_context["items"], [])
        self.assertEqual(stopped_context["excluded_count"], 1)
        with self.assertRaises(IntelligenceError) as stale_context:
            self.service._revalidate_prepared_context(prepared_with_memory)
        self.assertEqual(stale_context.exception.code, "INTELLIGENCE_CONTEXT_CHANGED")

        resumed = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/use",
            json={"base_version": 2, "enabled": True}, headers=self.auth_headers(),
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["version"], 3)
        self.assertTrue(resumed.json["data"]["context_eligible"])

        current_list = self.request(
            "get", f"/api/intelligence/memories?work_id={work['id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(current_list.status_code, 200, current_list.get_data(as_text=True))
        self.assertEqual([item["memory_id"] for item in current_list.json["data"]], [saved["memory_id"]])

        corrected = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/correct",
            json={"base_version": 3, "content": "更正：材料金额为120万元，日期为2026年10月。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(corrected.status_code, 200, corrected.get_data(as_text=True))
        self.assertEqual(corrected.json["data"]["version"], 4)
        self.assertEqual(corrected.json["data"]["source_refs"], saved["source_refs"])
        self.assertEqual(corrected.json["data"]["canonical_state"], "NOT_CANONICAL")

        stale_correction = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/correct",
            json={"base_version": 3, "content": "过期改动不能覆盖第四版。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(stale_correction.status_code, 409)
        self.assertEqual(stale_correction.json["error"]["code"], "MEMORY_VERSION_CONFLICT")
        self.assertEqual(stale_correction.json["error"]["details"]["current_version"], 4)

        restarted_app = create_app(self.root, expected_port=8317)
        restarted_app.config.update(TESTING=True)
        self.app = restarted_app
        self.service = restarted_app.extensions["oevira.intelligence"]
        self.client = restarted_app.test_client()
        reopened = self.request(
            "get", f"/api/intelligence/memories?work_id={work['id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"][0]["version"], 4)
        self.assertEqual(reopened.json["data"][0]["content"], "更正：材料金额为120万元，日期为2026年10月。")
        self.assertEqual(reopened.json["data"][0]["source_state"], "current")
        self.assertEqual(reopened.json["data"][0]["source_refs"][0]["material_id"], material["id"])

        paused = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/use",
            json={"base_version": 4, "enabled": False}, headers=self.auth_headers(),
        )
        self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
        self.assertEqual(paused.json["data"]["version"], 5)
        self.assertFalse(paused.json["data"]["use_enabled"])
        self.assertEqual(paused.json["data"]["content"], "更正：材料金额为120万元，日期为2026年10月。")

        restarted_app = create_app(self.root, expected_port=8317)
        restarted_app.config.update(TESTING=True)
        self.app = restarted_app
        self.service = restarted_app.extensions["oevira.intelligence"]
        self.client = restarted_app.test_client()
        paused_readback = self.request(
            "get", f"/api/intelligence/memories?work_id={work['id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(paused_readback.status_code, 200)
        self.assertEqual(paused_readback.json["data"][0]["version"], 5)
        self.assertFalse(paused_readback.json["data"][0]["use_enabled"])
        self.assertEqual(self.service.memory.context(self.identity, work_id=work["id"])["items"], [])

        forgotten = self.request(
            "post", f"/api/intelligence/memories/{saved['memory_id']}/forget",
            json={"base_version": 5}, headers=self.auth_headers(),
        )
        self.assertEqual(forgotten.status_code, 200, forgotten.get_data(as_text=True))
        self.assertEqual(forgotten.json["data"], {
            "memory_id": saved["memory_id"], "status": "forgotten", "content_retained": False,
        })
        active_after_forget = self.request(
            "get", f"/api/intelligence/memories?work_id={work['id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(active_after_forget.status_code, 200)
        self.assertEqual(active_after_forget.json["data"], [])
        forgotten_history = self.request(
            "get", f"/api/intelligence/memories?work_id={work['id']}&include_history=true",
            headers=self.auth_headers(),
        )
        self.assertEqual(forgotten_history.status_code, 200)
        self.assertTrue(forgotten_history.json["data"])
        self.assertTrue(all(item["content"] is None and item["source_refs"] == []
                            for item in forgotten_history.json["data"]))

    def test_current_material_candidate_hints_retain_body_locations_without_becoming_citations(self) -> None:
        work = self.create_work("候选检索定位")
        material = self.create_material(
            work["id"], title="品牌素材", text="品牌增长来自品牌社群讨论。话题有 #品牌 与 #品牌。",
        )
        result = self.service.search_materials(self.identity, "品牌", work_id=work["id"])
        lane = result["research_candidates"]["derived_from_current_materials"]
        hashtag = next(item for item in lane["results"] if item["term"] == "#品牌")
        self.assertEqual(hashtag["candidate_kind"], "VERBATIM_BODY_HASHTAG_LITERAL")
        self.assertEqual(hashtag["occurrence_count"], 2)
        self.assertEqual(hashtag["source"]["material_id"], material["id"])
        self.assertEqual(hashtag["source"]["offset_unit"], "unicode-codepoint")
        self.assertEqual(hashtag["evidence_state"], "UNVERIFIED_RESEARCH_HINT_NOT_FACT")
        self.assertNotIn("citations", hashtag)
        context = self.service.prepare_context(
            self.identity, "整理品牌方向", work_id=work["id"], query="品牌", include_memory=False,
        )
        self.assertTrue(any(ref.get("kind") == "material_body_candidate" for ref in context.source_refs))
        self.service._revalidate_prepared_context(context)

    def test_github_skill_is_pinned_approved_tested_updated_rolled_back_revoked_and_uninstalled(self) -> None:
        tool_names = ["artifact.create_draft", "artifact.revise_draft"]
        skill = self.install_test_github_skill(tools=tool_names)
        artifact_response = self.request(
            "post", "/api/artifacts",
            json={"title": "Skill撤销前的成果", "body": "此既有结果应在Skill卸载后保留。", "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        artifact_id = artifact_response.json["data"]["id"]

        v2_candidate = self.install_test_github_skill(
            tools=tool_names, reference="v2", instruction_suffix="Updated review requirement.", install=False,
        )["candidate"]
        update = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/update",
            json={"candidate_id": v2_candidate["candidate_id"],
                  "candidate_sha256": v2_candidate["content_sha256"],
                  "license_sha256": v2_candidate["license"]["file_sha256"],
                  "rights_reviewed": True, "tool_names": tool_names},
            headers={**self.auth_headers(), "Idempotency-Key": "update-test-skill-v2"},
        )
        self.assertEqual(update.status_code, 200, update.get_data(as_text=True))
        self.assertEqual(update.json["data"]["version"], 2)
        self.assertEqual(update.json["data"]["status"], "installed_disabled")

        tested_v2 = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/self-test",
            json={"version": 2, "content_sha256": v2_candidate["content_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "self-test-test-skill-v2"},
        )
        self.assertEqual(tested_v2.status_code, 200, tested_v2.get_data(as_text=True))
        enabled_v2 = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/enable",
            json={"version": 2, "content_sha256": v2_candidate["content_sha256"],
                  "tool_schema_sha256": update.json["data"]["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "enable-test-skill-v2"},
        )
        self.assertEqual(enabled_v2.status_code, 200, enabled_v2.get_data(as_text=True))

        rollback = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/rollback",
            json={"version": 1, "current_version": 2,
                  "current_content_sha256": v2_candidate["content_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "rollback-test-skill-v1"},
        )
        self.assertEqual(rollback.status_code, 200, rollback.get_data(as_text=True))
        self.assertEqual(rollback.json["data"]["self_test"], "REQUIRED_AGAIN")
        tested_v1 = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/self-test",
            json={"version": 1, "content_sha256": skill["content_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "self-test-test-skill-v1-again"},
        )
        self.assertEqual(tested_v1.status_code, 200, tested_v1.get_data(as_text=True))
        enabled_v1 = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/enable",
            json={"version": 1, "content_sha256": skill["content_sha256"],
                  "tool_schema_sha256": skill["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "enable-test-skill-v1-again"},
        )
        self.assertEqual(enabled_v1.status_code, 200, enabled_v1.get_data(as_text=True))

        revoke = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/revoke",
            json={"version": 1, "content_sha256": skill["content_sha256"],
                  "tool_schema_sha256": skill["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "revoke-test-skill"},
        )
        self.assertEqual(revoke.status_code, 200, revoke.get_data(as_text=True))
        self.assertEqual(revoke.json["data"]["status"], "revoked")
        uninstall = self.request(
            "post", f"/api/intelligence/skills/{skill['skill_id']}/uninstall",
            json={"version": 1, "content_sha256": skill["content_sha256"],
                  "tool_schema_sha256": skill["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "uninstall-test-skill"},
        )
        self.assertEqual(uninstall.status_code, 200, uninstall.get_data(as_text=True))
        self.assertTrue(uninstall.json["data"]["business_outputs_preserved"])
        reopened_artifact = self.request(
            "get", f"/api/artifacts/{artifact_id}", headers=self.auth_headers(),
        )
        self.assertEqual(reopened_artifact.status_code, 200, reopened_artifact.get_data(as_text=True))
        self.assertEqual(reopened_artifact.json["data"]["body"], "此既有结果应在Skill卸载后保留。")
        history = self.request("get", f"/api/intelligence/skills/{skill['skill_id']}", headers=self.auth_headers())
        self.assertEqual(history.status_code, 200, history.get_data(as_text=True))
        self.assertEqual(history.json["data"]["skill"]["status"], "uninstalled")
        self.assertEqual(history.json["data"]["skill"]["files"], {})

    def test_material_source_credentials_are_redacted_before_model_context(self) -> None:
        work = self.create_work("临时访问链接")
        material = self.create_material(work["id"])
        original_url = "https://research.example.cn/notes/quarterly?access_token=secret-token&view=full"
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE materials SET source_url = ?, source_json = ?, source_text = ? "
                "WHERE id = ? AND workspace_id = ?",
                (original_url, json.dumps({
                    "kind": "controlled_browser_snapshot", "source_url": original_url,
                    "access_token": "secret-token", "unreviewed_private_field": "must-not-escape",
                    "capture_mode": "visible_page_text", "actual_access_class": "controlled_browser_session",
                }), "raw-html-with-secret-token", material["id"], self.identity["workspace"]["id"]),
            )

        result = self.service.read_material(self.identity, material["id"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("unreviewed_private_field", encoded)
        self.assertNotIn("source_text", result)
        self.assertIn("%5Bredacted%5D", result["source_url"])
        self.assertEqual(result["source"]["redacted_query_keys"], ["access_token"])

        context = self.service.prepare_context(
            self.identity, "总结预算", work_id=work["id"], query="预算",
        )
        context_text = json.dumps(self.service.public_context(context), ensure_ascii=False)
        self.assertNotIn("secret-token", context_text)
        self.assertNotIn("must-not-escape", context_text)

    def test_actual_ai_tool_turn_saves_and_reopens_artifact_without_raw_ids_for_user_input(self) -> None:
        work = self.create_work("AI工具闭环")
        material = self.create_material(work["id"])
        evidence = self.service.search_materials(self.identity, "预算", work_id=work["id"])
        citation = evidence["citations"][0]
        selection = ExplicitModelSelection(
            route="owned", selection_id="local-selection", revision="model-revision-1",
            model_id="test-owned-small-model", recipient_id="local-only", selected_by_user=True,
        )
        calls = []
        authorization_expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()

        def resolve_selection(identity, selection_id):
            return selection if selection_id == selection.selection_id else None

        def authorize_source(identity, selected, source_refs, *, purpose):
            self.assertEqual(purpose, "inference")
            return ModelInputAuthorization(
                allowed=True,
                workspace_id=identity["workspace"]["id"], user_id=identity["user"]["id"],
                selection_id=selected.selection_id, selection_revision=selected.revision,
                route=selected.route, model_id=selected.model_id,
                source_ref_sha256=_canonical_digest(list(source_refs)), purpose=purpose,
                recipient=selected.recipient_id,
                authorization_id=f"auth-{_canonical_digest(list(source_refs))[:12]}",
                expires_at=authorization_expiry,
            )

        def run_model(selected, payload, *, is_cancelled=None):
            calls.append(payload)
            if not payload.get("tool_results"):
                return {
                    "status": "completed",
                    "tool_calls": [{
                        "name": "artifact.create_draft",
                        "arguments": {
                            "title": "预算核验摘要",
                            "body": "材料记载2026年度预算为120万元。",
                            "citations": [citation], "work_id": work["id"],
                        },
                    }],
                }
            return {"status": "completed", "text": "已保存带来源的预算摘要。", "citations": [citation]}

        service = IntelligenceService(
            self.store, model_selection_resolver=resolve_selection,
            source_use_authorizer=authorize_source, owned_inference_runner=run_model,
        )
        prepared = service.prepare_context(
            self.identity, "对照预算材料并保存摘要", work_id=work["id"],
            query="预算",
        )
        with self.app.test_request_context(
            "/api/intelligence/turn", headers={"Idempotency-Key": "intelligence-artifact-v1"},
        ):
            g.identity = prepared.identity
            result = service.run_model(prepared, selection_id=selection.selection_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result["tool_results"]), 1)
        saved = result["tool_results"][0]["result"]
        self.assertEqual(saved["title"], "预算核验摘要")
        self.assertEqual(saved["version"], 1)
        reopened = service.read_artifact(self.identity, saved["id"])
        self.assertEqual(reopened["body"], "材料记载2026年度预算为120万元。")
        self.assertEqual(reopened["citation_state"], "current")
        self.assertEqual(reopened["validated_citations"], [citation])

    def test_business_tool_schema_is_typed_and_unselected_results_cannot_be_revised(self) -> None:
        work = self.create_work("工具合同范围")
        artifact_response = self.request(
            "post", "/api/artifacts",
            json={"title": "未选择的旧结果", "body": "保留内容", "citations": [], "work_id": work["id"]},
            headers=self.auth_headers(),
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        artifact_id = artifact_response.json["data"]["id"]
        catalog = self.service.tools.catalog()
        business = next(item for item in catalog["tools"] if item["tool"] == "business.analyze")
        schema = business["input_schema"]
        self.assertEqual(schema["properties"]["operation"]["type"], "string")
        self.assertIn("financial_mmm_analysis", schema["properties"]["operation"]["enum"])
        self.assertTrue({"propagation_network", "propagation_hawkes", "adoption_diffusion", "decision_comparison"}
                        <= set(schema["properties"]["operation"]["enum"]))
        self.assertEqual(schema["properties"]["inputs"]["type"], "object")

        prepared = self.service.prepare_context(self.identity, "回答工作问题", work_id=work["id"])
        tool_context = self.service.create_tool_context(prepared)
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            with self.assertRaises(IntelligenceError) as blocked:
                self.service.invoke_tool(self.identity, "artifact.revise_draft", {
                    "artifact_id": artifact_id, "base_version": 1,
                    "title": "不应修改", "body": "新内容", "citations": [],
                }, tool_context)
        self.assertEqual(blocked.exception.code, "INTELLIGENCE_TOOL_PARENT_VERSION_CHANGED")
        unchanged = self.service.read_artifact(self.identity, artifact_id)
        self.assertEqual(unchanged["version"], 1)
        self.assertEqual(unchanged["body"], "保留内容")

    def test_intelligence_runs_new_event_and_decision_operations_saves_revises_and_reopens_business_result(self) -> None:
        work = self.create_work("行业AI事件与方案闭环")
        material = self.create_material(
            work["id"], title="转发观察",
            text="材料记录账号甲转发账号乙的内容，当前只有这一条已保存观察。",
        )
        citation = self.service.search_materials(
            self.identity, "账号甲转发", work_id=work["id"],
        )["citations"][0]
        prepared = self.service.prepare_context(
            self.identity, "比较业务方案并分析有据转发关系。", work_id=work["id"],
            query="方案 转发", include_memory=False,
        )
        tool_context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="business-intelligence-tool-closure",
        )
        decision_inputs = {
            "question": "在同一预算约束下选择当前传播方案。",
            "criteria": [
                {"id": "reach", "name": "预估触达", "definition": "本次采用的条件估计", "unit": "人次",
                 "direction": "maximize", "weight": 0.6},
                {"id": "cost", "name": "预算成本", "definition": "同一预算口径", "unit": "万元",
                 "direction": "minimize", "weight": 0.4},
            ],
            "constraints": [{"id": "budget", "description": "不超过本次输入预算"}],
            "alternatives": [
                {"id": "owned", "title": "自有账号方案", "values": {"reach": 1000, "cost": 12},
                 "constraint_checks": {"budget": {"status": "satisfied", "basis": "本次输入预算12万元"}}},
                {"id": "partner", "title": "合作账号方案", "values": {"reach": 1400, "cost": 20},
                 "constraint_checks": {"budget": {"status": "violated", "basis": "本次输入上限18万元"}}},
            ],
        }
        event_inputs = {
            "nodes": [
                {"id": "account-a", "label": "账号甲", "platform": "小红书"},
                {"id": "account-b", "label": "账号乙", "platform": "小红书"},
            ],
            "edges": [{
                "id": "repost-a-b", "source": "account-a", "target": "account-b",
                "relation": "repost", "source_refs": ["saved-material-segment"],
                "material_id": material["id"],
            }],
            "relation": "repost",
        }
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            decision = self.service.invoke_tool(self.identity, "business.analyze", {
                "operation": "decision_comparison", "work_id": work["id"],
                "title": "方案对比", "inputs": decision_inputs, "citations": [citation],
            }, tool_context)["result"]
            self.assertEqual(decision["version"], 1)
            self.assertEqual(decision["input_snapshot"]["business"]["operation"], "decision_comparison")
            decision_body = decision["body"]
            self.assertIn("decision.options", [
                section["id"] for section in decision["input_snapshot"]["business"]["result"]["sections"]
            ])

            event = self.service.invoke_tool(self.identity, "business.analyze", {
                "operation": "propagation_network", "work_id": work["id"],
                "title": "转发关系核查", "inputs": event_inputs, "citations": [citation],
            }, tool_context)["result"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["input_snapshot"]["business"]["operation"], "propagation_network")

        revised = self.request(
            "post", f"/api/business-results/{decision['id']}/revisions",
            json={
                "base_version": 1,
                "section_edits": [{
                    "id": "decision.sensitivity",
                    "body": "用户局部补充：合作成本越过本次预算上限，不能列作可用方案。",
                }],
                "citations": [citation], "revision_note": "用户限定只修订方案敏感性说明",
            },
            headers={**self.auth_headers(), "Idempotency-Key": "decision-sensitivity-local-revision"},
        )
        self.assertEqual(revised.status_code, 200, revised.get_data(as_text=True))
        self.assertEqual(revised.json["data"]["version"], 2)

        cold_app = create_app(self.root, expected_port=8317)
        cold_service = cold_app.extensions["oevira.intelligence"]
        reopened_decision = cold_service.read_artifact(self.identity, decision["id"])
        reopened_event = cold_service.read_artifact(self.identity, event["id"])
        self.assertEqual(reopened_decision["version"], 2)
        self.assertEqual(reopened_decision["citation_state"], "current")
        self.assertEqual(reopened_event["version"], 1)
        self.assertEqual(reopened_event["citation_state"], "current")
        saved_sections = reopened_decision["input_snapshot"]["business"]["result"]["sections"]
        revised_sensitivity = next(item for item in saved_sections if item["id"] == "decision.sensitivity")
        revised_options = next(item for item in saved_sections if item["id"] == "decision.options")
        self.assertIn("用户局部补充", revised_sensitivity["body"])
        self.assertEqual(
            revised_options["body"],
            next(item["body"] for item in decision["input_snapshot"]["business"]["result"]["sections"]
                 if item["id"] == "decision.options"),
        )
        self.assertEqual(reopened_decision["body"], revised.json["data"]["body"])

    def test_intelligence_delivery_tool_packages_selected_artifact_and_cold_reopens(self) -> None:
        work = self.create_work("行业AI文件交付")
        artifact_response = self.request(
            "post", "/api/artifacts",
            json={"work_id": work["id"], "title": "已确认方案", "body": "预算为120万元；本段由用户确认。", "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        artifact = artifact_response.json["data"]
        prepared = self.service.prepare_context(
            self.identity, "交付已确认方案。", work_id=work["id"],
            artifact_id=artifact["id"], artifact_version=1,
            requested_output_formats=("md",), include_memory=False,
        )
        tool_context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="intelligence-delivery-tool-test",
        )
        delivery_tool = next(
            item for item in self.service.tools.catalog()["tools"]
            if item["tool"] == "delivery.create_batch"
        )
        self.assertEqual(delivery_tool["owner"], "Delivery")
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            operation = self.service.invoke_tool(self.identity, "delivery.create_batch", {
                "title": "方案文件", "work_id": work["id"],
                "members": [{
                    "member_id": "selected-proposal", "kind": "artifact",
                    "artifact_id": artifact["id"], "version": 1, "format": "md",
                    "audience": "internal", "filename": "已确认方案.md",
                }],
            }, tool_context)
        batch = operation["result"]
        self.assertEqual(batch["status"], "preparing")
        self.assertEqual(batch["members"][0]["reference"]["artifact_id"], artifact["id"])
        self.assertEqual(batch["members"][0]["reference"]["version"], 1)
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            status = self.service.invoke_tool(self.identity, "delivery.read", {
                "batch_id": batch["id"],
            }, tool_context)["result"]
        self.assertEqual(status["id"], batch["id"])
        self.assertEqual(status["status"], "preparing")

        from server.oevira.worker import _claim_job, _execute_claim
        claim = _claim_job(self.store, allowed_kinds=("delivery_batch",))
        self.assertEqual(claim["id"], batch["job"]["id"])
        _execute_claim(self.app, claim)
        completed = self.request("get", f"/api/deliveries/{batch['id']}", headers=self.auth_headers())
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertEqual(completed.json["data"]["status"], "completed")
        download = self.request("get", completed.json["data"]["download_url"], headers=self.auth_headers())
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertIn("预算为120万元".encode("utf-8"), archive.read("\u5df2\u786e\u8ba4\u65b9\u6848.md"))

        cold_app = create_app(self.root, expected_port=8317)
        cold_app.config.update(TESTING=True)
        self.app = cold_app
        self.client = cold_app.test_client()
        cold_batch = self.request("get", f"/api/deliveries/{batch['id']}", headers=self.auth_headers())
        self.assertEqual(cold_batch.status_code, 200, cold_batch.get_data(as_text=True))
        self.assertEqual(cold_batch.json["data"]["status"], "completed")
        cold_file = self.request("get", cold_batch.json["data"]["download_url"], headers=self.auth_headers())
        with zipfile.ZipFile(io.BytesIO(cold_file.data)) as archive:
            reopened_file = archive.read("\u5df2\u786e\u8ba4\u65b9\u6848.md")
            self.assertIn("预算为120万元".encode("utf-8"), reopened_file)
            self.assertIn("本段由用户确认".encode("utf-8"), reopened_file)

    def test_intelligence_delivery_uses_explicit_artifact_scope_and_existing_client_boundary(self) -> None:
        source_work = self.create_work("同客户来源Work")
        target_work = self.create_work("同客户交付Work")
        other_work = self.create_work("另一客户来源Work")
        first_commission = self.request(
            "post", "/api/business/commissions", json={"name": "交付客户甲", "scope": {}},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-delivery-client-a"},
        )
        second_commission = self.request(
            "post", "/api/business/commissions", json={"name": "交付客户乙", "scope": {}},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-delivery-client-b"},
        )
        self.assertEqual(first_commission.status_code, 201, first_commission.get_data(as_text=True))
        self.assertEqual(second_commission.status_code, 201, second_commission.get_data(as_text=True))
        now = utc_now()
        with self.store.transaction() as connection:
            connection.executemany(
                "INSERT INTO work_commissions(work_id, commission_id, created_at) VALUES(?,?,?)",
                [
                    (source_work["id"], first_commission.json["data"]["id"], now),
                    (target_work["id"], first_commission.json["data"]["id"], now),
                    (other_work["id"], second_commission.json["data"]["id"], now),
                ],
            )
        same_client_artifact = self.request(
            "post", "/api/artifacts",
            json={"work_id": source_work["id"], "title": "客户甲提案", "body": "可供同客户交付。", "citations": []},
            headers=self.auth_headers(),
        ).json["data"]
        other_client_artifact = self.request(
            "post", "/api/artifacts",
            json={"work_id": other_work["id"], "title": "客户乙提案", "body": "不得交给客户甲。", "citations": []},
            headers=self.auth_headers(),
        ).json["data"]
        prepared = self.service.prepare_context(
            self.identity, "准备选中成果交付。", work_id=target_work["id"],
            requested_output_formats=("md",), include_memory=False,
        )
        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="intelligence-delivery-client-boundary",
        )
        context.allowed_artifact_versions[same_client_artifact["id"]] = 1
        context.allowed_artifact_versions[other_client_artifact["id"]] = 1
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            accepted = self.service.invoke_tool(self.identity, "delivery.create_batch", {
                "title": "同客户交付", "work_id": target_work["id"],
                "members": [{
                    "member_id": "same-client", "kind": "artifact",
                    "artifact_id": same_client_artifact["id"], "version": 1, "format": "md",
                    "filename": "客户甲提案.md",
                }],
            }, context)["result"]
            self.assertEqual(accepted["work_id"], target_work["id"])
            self.assertEqual(accepted["members"][0]["reference"]["artifact_id"], same_client_artifact["id"])
            with self.assertRaises(IntelligenceError) as denied:
                self.service.invoke_tool(self.identity, "delivery.create_batch", {
                    "title": "跨客户交付", "work_id": target_work["id"],
                    "members": [{
                        "member_id": "other-client", "kind": "artifact",
                        "artifact_id": other_client_artifact["id"], "version": 1, "format": "md",
                        "filename": "客户乙提案.md",
                    }],
                }, context)
        self.assertEqual(denied.exception.code, "CLIENT_SCOPE_MISMATCH")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM delivery_batches WHERE work_id=?",
                                        (target_work["id"],))["n"], 1)

    def test_intelligence_delivery_uses_requested_render_and_interchange_from_selected_project(self) -> None:
        work = self.create_work("媒体成果精确交付")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48_000)
            wav.writeframes(b"\x10\x00" * 4_800)
        data = audio.getvalue()
        upload = self.request(
            "post", "/api/uploads",
            json={"kind": "media", "title": "对话录音", "filename": "dialogue.wav",
                  "expected_size": len(data), "expected_sha256": hashlib.sha256(data).hexdigest(),
                  "work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-upload"},
        )
        self.assertIn(upload.status_code, {200, 201}, upload.get_data(as_text=True))
        upload_id = upload.json["data"]["id"]
        for offset in range(0, len(data), 4096):
            chunk = data[offset:offset + 4096]
            response = self.request(
                "patch", f"/api/uploads/{upload_id}", data=chunk,
                content_type="application/octet-stream",
                headers={**self.auth_headers(), "Upload-Offset": str(offset),
                         "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            )
            self.assertEqual(response.status_code, 204, response.get_data(as_text=True))
        material_response = self.request(
            "post", f"/api/uploads/{upload_id}/complete", headers=self.auth_headers(),
        )
        self.assertEqual(material_response.status_code, 201, material_response.get_data(as_text=True))
        material = material_response.json["data"]["material"]

        project_response = self.request(
            "post", "/api/media/projects",
            json={"title": "媒体交付项目", "kind": "podcast", "work_id": work["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-project"},
        )
        self.assertEqual(project_response.status_code, 201, project_response.get_data(as_text=True))
        project_id = project_response.json["data"]["id"]
        source_response = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-source"},
        )
        self.assertEqual(source_response.status_code, 200, source_response.get_data(as_text=True))
        asset = source_response.json["data"]["sources"][0]
        edit_response = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
                "timeline_start": {"num": 0, "den": 1},
            }]},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-edit"},
        )
        self.assertEqual(edit_response.status_code, 200, edit_response.get_data(as_text=True))
        project_version = edit_response.json["data"]["version"]

        preflight = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": project_version, "target_profile": "otio"},
            headers=self.auth_headers(),
        )
        self.assertEqual(preflight.status_code, 200, preflight.get_data(as_text=True))
        plan = preflight.json["data"]
        self.assertEqual(plan["state"], "ready")
        interchange_response = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": project_version, "target_profile": "otio",
                  "source_snapshot_sha256": plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": plan["required_accepted_loss_item_ids"]},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-interchange"},
        )
        self.assertEqual(interchange_response.status_code, 202, interchange_response.get_data(as_text=True))
        interchange_id = interchange_response.json["data"]["id"]
        from server.oevira.worker import _claim_job, _execute_claim
        interchange_claim = _claim_job(self.store, allowed_kinds=("media_interchange",))
        self.assertEqual(interchange_claim["id"], interchange_response.json["data"]["job"]["id"])
        _execute_claim(self.app, interchange_claim)
        interchange = self.request("get", f"/api/media/interchanges/{interchange_id}", headers=self.auth_headers())
        self.assertEqual(interchange.status_code, 200, interchange.get_data(as_text=True))
        self.assertEqual(interchange.json["data"]["status"], "completed")

        render_response = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": project_version, "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48_000, "audio_channels": 1}},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-delivery-render"},
        )
        self.assertEqual(render_response.status_code, 202, render_response.get_data(as_text=True))
        render_id = render_response.json["data"]["render_id"]
        render_claim = _claim_job(self.store, allowed_kinds=("media_render",))
        self.assertEqual(render_claim["id"], render_response.json["data"]["job"]["id"])
        _execute_claim(self.app, render_claim)
        render = self.request("get", f"/api/media/renders/{render_id}", headers=self.auth_headers())
        self.assertEqual(render.status_code, 200, render.get_data(as_text=True))
        self.assertEqual(render.json["data"]["status"], "completed")

        prepared = self.service.prepare_context(
            self.identity, "交付用户要求的WAV成片和OTIO工程交换包。",
            work_id=work["id"], media_project_id=project_id,
            requested_output_formats=("wav", "otio"), include_memory=False,
        )
        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="intelligence-media-delivery", 
        )
        members = [
            {"member_id": "final-audio", "kind": "media_render", "render_id": render_id,
             "filename": "dialogue-master.wav"},
            {"member_id": "otio-project", "kind": "media_interchange", "interchange_id": interchange_id,
             "filename": "timeline.otio.zip"},
        ]
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            for requested, member in (("otio", members[0]), ("wav", members[1])):
                wrong_format_context = self.service.create_tool_context(
                    self.service.prepare_context(
                        self.identity, "交付一个精确请求的媒体结果。", work_id=work["id"],
                        media_project_id=project_id, requested_output_formats=(requested,), include_memory=False,
                    ), caller="model", operation_scope=f"wrong-format-{requested}",
                )
                with self.assertRaises(IntelligenceError) as not_requested:
                    self.service.invoke_tool(self.identity, "delivery.create_batch", {
                        "title": "不应创建的媒体交付", "work_id": work["id"], "members": [member],
                    }, wrong_format_context)
                self.assertEqual(not_requested.exception.code, "INTELLIGENCE_TOOL_OUTPUT_NOT_REQUESTED")

            allowed_version = context.allowed_media_project_versions[project_id]
            context.allowed_media_project_versions[project_id] = allowed_version - 1
            with self.assertRaises(IntelligenceError) as stale_version:
                self.service.invoke_tool(self.identity, "delivery.create_batch", {
                    "title": "过期媒体工程", "work_id": work["id"], "members": members,
                }, context)
            self.assertEqual(stale_version.exception.code, "INTELLIGENCE_TOOL_PARENT_VERSION_CHANGED")
            context.allowed_media_project_versions[project_id] = allowed_version
            package = self.service.invoke_tool(self.identity, "delivery.create_batch", {
                "title": "精确媒体交付", "work_id": work["id"], "members": members,
            }, context)["result"]
        self.assertEqual(package["status"], "preparing")
        saved = {item["member_id"]: item["reference"] for item in package["members"]}
        self.assertEqual(saved["final-audio"]["kind"], "media_render")
        self.assertEqual(saved["final-audio"]["project_version"], project_version)
        self.assertEqual(saved["otio-project"]["kind"], "media_interchange")
        self.assertEqual(saved["otio-project"]["project_version"], project_version)
        self.assertEqual(saved["otio-project"]["target_profile"], "otio")
        self.assertEqual(saved["otio-project"]["adapter_revision"], interchange.json["data"]["adapter_revision"])

        delivery_claim = _claim_job(self.store, allowed_kinds=("delivery_batch",))
        self.assertEqual(delivery_claim["id"], package["job"]["id"])
        _execute_claim(self.app, delivery_claim)
        completed = self.request("get", f"/api/deliveries/{package['id']}", headers=self.auth_headers())
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertEqual(completed.json["data"]["status"], "completed")
        download = self.request("get", completed.json["data"]["download_url"], headers=self.auth_headers())
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertIn("dialogue-master.wav", archive.namelist())
            self.assertIn("timeline.otio.zip", archive.namelist())

    def test_intelligence_research_tool_preserves_open_questions_and_unreviewed_candidate_provenance(self) -> None:
        from server.oevira.worker import _claim_job, _execute_claim

        open_work = self.create_work("开放研究问题")
        open_context = self.service.prepare_context(
            self.identity, "研究这个问题；当前没有来源材料。", work_id=open_work["id"],
            include_memory=False,
        )
        open_tool_context = self.service.create_tool_context(
            open_context, caller="model", operation_scope="research-open-question",
        )
        open_tool = next(
            item for item in self.service.tools.catalog()["tools"]
            if item["tool"] == "research.create_job"
        )
        self.assertEqual(open_tool["owner"], "Research")
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = open_context.identity
            open_job = self.service.invoke_tool(self.identity, "research.create_job", {
                "question": "当前有哪些事实尚待补证？", "work_id": open_work["id"],
            }, open_tool_context)["result"]
        self.assertEqual(open_job["kind"], "material_research")
        self.assertEqual(open_job["status"], "queued")
        open_payload = json.loads(self.store.row("SELECT payload_json FROM jobs WHERE id=?", (open_job["id"],))["payload_json"])
        self.assertEqual(open_payload["material_ids"], [])
        self.assertEqual(open_payload["research_request"]["propositions"], [])

        open_claim = _claim_job(self.store, allowed_kinds=("material_research",))
        self.assertEqual(open_claim["id"], open_job["id"])
        _execute_claim(self.app, open_claim)
        open_job_readback = self.store.row("SELECT status, artifact_id FROM jobs WHERE id=?", (open_job["id"],))
        self.assertEqual(open_job_readback["status"], "completed")
        open_artifact = self.service.read_artifact(self.identity, open_job_readback["artifact_id"])
        open_research = open_artifact["input_snapshot"]["research"]
        self.assertEqual(open_research["propositions"], [])
        self.assertEqual(open_research["judgment_state"], "not_independently_assessed")
        self.assertEqual(open_artifact["citations"], [])

        sourced_work = self.create_work("候选命题来源边界")
        material = self.create_material(sourced_work["id"], text="2026年度预算为120万元。该数值来自本次材料。")
        citation = self.service.search_materials(
            self.identity, "预算120万元", work_id=sourced_work["id"],
        )["citations"][0]
        research_context = self.service.prepare_context(
            self.identity, "形成待核研究命题。", work_id=sourced_work["id"],
            query="预算120万元", include_memory=False,
        )
        research_tool_context = self.service.create_tool_context(
            research_context, caller="model", operation_scope="research-assistant-candidate",
        )
        reference = {key: citation[key] for key in ("material_id", "segment_id", "quote")}
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = research_context.identity
            candidate_job = self.service.invoke_tool(self.identity, "research.create_job", {
                "question": "核对材料中的预算数值。", "work_id": sourced_work["id"],
                "material_ids": [material["id"]], "purpose": "产生可复核的来源候选",
                "propositions": [{
                    "statement": "材料记载2026年度预算为120万元。", "kind": "source_statement",
                    "supporting_refs": [reference], "counterevidence_refs": [],
                }],
            }, research_tool_context)["result"]
        candidate_payload = json.loads(self.store.row(
            "SELECT payload_json FROM jobs WHERE id=?", (candidate_job["id"],),
        )["payload_json"])
        proposition = candidate_payload["research_request"]["propositions"][0]
        self.assertEqual(proposition["origin"], "assistant_candidate")
        self.assertEqual(proposition["assessment_state"], "unreviewed")
        self.assertEqual(proposition["supporting_refs"][0]["material_id"], material["id"])

        candidate_claim = _claim_job(self.store, allowed_kinds=("material_research",))
        self.assertEqual(candidate_claim["id"], candidate_job["id"])
        _execute_claim(self.app, candidate_claim)
        candidate_job_readback = self.store.row("SELECT status, artifact_id FROM jobs WHERE id=?", (candidate_job["id"],))
        self.assertIn(candidate_job_readback["status"], {"completed", "partial"})
        self.assertIsInstance(candidate_job_readback["artifact_id"], str)
        candidate_artifact = self.service.read_artifact(self.identity, candidate_job_readback["artifact_id"])
        saved_candidate = candidate_artifact["input_snapshot"]["research"]["propositions"][0]
        self.assertEqual(saved_candidate["origin"], "assistant_candidate")
        self.assertEqual(saved_candidate["assessment_state"], "unreviewed")
        self.assertEqual(saved_candidate["supporting_refs"][0]["source_state"], "verified")

    def test_paused_intelligence_turn_resumes_after_saved_tool_result_without_replaying_write(self) -> None:
        work = self.create_work("智能暂停后续接")
        material = self.create_material(work["id"], text="本次材料记载预算为120万元。")
        citation = self.service.search_materials(
            self.identity, "预算120万元", work_id=work["id"],
        )["citations"][0]
        selection = ExplicitModelSelection(
            route="owned", selection_id="resume-test-selection", revision="resume-model-r1",
            model_id="resume-test-model", recipient_id="local-owned-runner", selected_by_user=True,
        )
        authorization_expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        model_calls = []

        def resolve_selection(identity, selection_id):
            return selection if selection_id == selection.selection_id else None

        def authorize_source(identity, selected, source_refs, *, purpose):
            return ModelInputAuthorization(
                allowed=True, workspace_id=identity["workspace"]["id"],
                user_id=identity["user"]["id"], selection_id=selected.selection_id,
                selection_revision=selected.revision, route=selected.route,
                model_id=selected.model_id, source_ref_sha256=_canonical_digest(list(source_refs)),
                purpose=purpose, recipient=selected.recipient_id,
                authorization_id="resume-auth-" + _canonical_digest(list(source_refs))[:12],
                expires_at=authorization_expiry,
            )

        def model_runner(selected, payload, *, is_cancelled=None):
            model_calls.append(payload)
            if len(model_calls) <= 2:
                return {
                    "status": "completed",
                    "tool_calls": [{
                        "name": "artifact.create_draft",
                        "arguments": {
                            "title": "预算结果", "body": "材料记载预算为120万元。",
                            "citations": [citation], "work_id": work["id"],
                        },
                    }],
                }
            return {"status": "completed", "text": "已保存并核对预算结果。", "citations": [citation]}

        resumed_service = IntelligenceService(
            self.store, model_selection_resolver=resolve_selection,
            source_use_authorizer=authorize_source, owned_inference_runner=model_runner,
        )
        from server.oevira.intelligence_tools import OwnerToolBinding
        owner_write_calls = []
        original_artifact_binding = resumed_service.tools._bindings["artifact.create_draft"]

        def count_artifact_write(arguments, context):
            owner_write_calls.append(arguments.copy())
            return original_artifact_binding.handler(arguments, context)

        resumed_service.tools._bindings["artifact.create_draft"] = OwnerToolBinding(
            original_artifact_binding.name, original_artifact_binding.owner,
            original_artifact_binding.required_fields, original_artifact_binding.optional_fields,
            original_artifact_binding.effect, count_artifact_write,
        )
        self.app.extensions["oevira.intelligence"] = resumed_service
        self.service = resumed_service
        request_body = {
            "message": "核对预算并保存来源摘要。", "work_id": work["id"],
            "selection_id": selection.selection_id, "include_memory": False,
        }
        queued = self.request(
            "post", "/api/intelligence/turns", json=request_body,
            headers={**self.auth_headers(), "Idempotency-Key": "pause-after-tool-result"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        conversation_id = queued.json["data"]["conversation"]["conversation_id"]
        job_id = queued.json["data"]["job"]["id"]
        from server.oevira.job_control import control_job
        from server.oevira.worker import _claim_job, _execute_claim

        original_extend = resumed_service.extend_context
        tool_receipt_ready = threading.Event()
        continue_to_model = threading.Event()

        def pause_after_saved_tool(prepared, tool_context):
            extended = original_extend(prepared, tool_context)
            if any(item.get("tool") == "artifact.create_draft" for item in tool_context.tool_results):
                tool_receipt_ready.set()
                if not continue_to_model.wait(5):
                    raise RuntimeError("test did not release the saved-tool checkpoint")
            return extended

        resumed_service.extend_context = pause_after_saved_tool
        claim = _claim_job(self.store, allowed_kinds=("intelligence_turn",))
        self.assertEqual(claim["id"], job_id)
        thread_errors = []

        def execute_claim():
            try:
                _execute_claim(self.app, claim)
            except Exception as exc:  # surface worker-thread failures in the test thread
                thread_errors.append(exc)

        worker_thread = threading.Thread(target=execute_claim, daemon=True)
        worker_thread.start()
        if not tool_receipt_ready.wait(10):
            failed_job = self.store.row("SELECT status,error_code,error_message,attempts FROM jobs WHERE id=?", (job_id,))
            failed_turn = self.store.row(
                "SELECT status,error_code,provider,model_id,context_json,tool_results_json FROM intelligence_turns WHERE job_id=?",
                (job_id,),
            )
            self.fail(
                "The domain tool did not save its Artifact receipt: "
                + json.dumps({"job": dict(failed_job) if failed_job else None,
                              "turn": dict(failed_turn) if failed_turn else None,
                              "model_calls": len(model_calls), "thread_errors": [str(error) for error in thread_errors]},
                             ensure_ascii=False)
            )
        with self.app.app_context():
            paused = control_job(
                self.app, self.store, self.identity, job_id, "pause",
                {"operation_id": "pause-intelligence-after-tool", "base_control_revision": 0},
            )
        self.assertEqual(paused["status"], "pause_requested")
        continue_to_model.set()
        worker_thread.join(15)
        self.assertFalse(worker_thread.is_alive(), "the paused Intelligence worker did not stop")
        self.assertEqual(thread_errors, [])

        paused_job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(paused_job["status"], "paused")
        paused_turn = self.store.row("SELECT * FROM intelligence_turns WHERE job_id=?", (job_id,))
        self.assertEqual(paused_turn["status"], "partial")
        self.assertEqual(len(json.loads(paused_turn["tool_results_json"])), 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM artifacts WHERE work_id=?", (work["id"],))["n"], 1)

        original_context_json = paused_turn["context_json"]
        original_tool_results_json = paused_turn["tool_results_json"]
        tampered_context = json.loads(original_context_json)
        unknown_receipt = [{"tool": "browser.page.navigate", "owner": "Acquisition",
                            "result": {"url": "https://example.test/"}}]
        tampered_context["tool_results"] = unknown_receipt
        tampered_context["resume_checkpoint"]["tool_results_sha256"] = _canonical_digest(unknown_receipt)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE intelligence_turns SET context_json=?,tool_results_json=? WHERE job_id=?",
                (json.dumps(tampered_context, ensure_ascii=False, sort_keys=True),
                 json.dumps([{"tool": "browser.page.navigate", "owner": "Acquisition", "state": "completed"}]),
                 job_id),
            )
        from server.oevira.api import ApiError
        with self.app.app_context():
            with self.assertRaises(ApiError) as unsafe_resume:
                control_job(
                    self.app, self.store, self.identity, job_id, "resume",
                    {"operation_id": "resume-intelligence-unknown-browser-result", "base_control_revision": 1},
                )
        self.assertEqual(unsafe_resume.exception.code, "INTELLIGENCE_RESUME_TOOL_RESULT_UNVERIFIED")
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (job_id,))["status"], "paused")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE intelligence_turns SET context_json=?,tool_results_json=? WHERE job_id=?",
                (original_context_json, original_tool_results_json, job_id),
            )
            connection.execute(
                "UPDATE jobs SET suspended_from='waiting_for_auth' WHERE id=?", (job_id,),
            )

        original_runner = resumed_service.owned_inference_runner
        resumed_service.owned_inference_runner = None
        with self.app.app_context():
            waiting_for_auth = control_job(
                self.app, self.store, self.identity, job_id, "resume",
                {"operation_id": "resume-intelligence-route-unavailable", "base_control_revision": 1},
            )
        self.assertEqual(waiting_for_auth["status"], "waiting_for_auth")
        self.assertEqual(self.store.row("SELECT status FROM jobs WHERE id=?", (job_id,))["status"], "waiting_for_auth")

        resumed_service.owned_inference_runner = original_runner
        with self.app.app_context():
            paused_again = control_job(
                self.app, self.store, self.identity, job_id, "pause",
                {"operation_id": "pause-intelligence-auth-restored", "base_control_revision": 2},
            )
        self.assertEqual(paused_again["status"], "paused")
        with self.app.app_context():
            resumed = control_job(
                self.app, self.store, self.identity, job_id, "resume",
                {"operation_id": "resume-intelligence-after-tool", "base_control_revision": 3},
            )
        self.assertEqual(resumed["status"], "queued")
        resumed_claim = _claim_job(self.store, allowed_kinds=("intelligence_turn",))
        self.assertEqual(resumed_claim["id"], job_id)
        _execute_claim(self.app, resumed_claim)

        completed_job = self.store.row("SELECT status, artifact_id FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(completed_job["status"], "completed")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM artifacts WHERE work_id=?", (work["id"],))["n"], 1)
        artifact = self.service.read_artifact(self.identity, completed_job["artifact_id"])
        self.assertEqual(artifact["body"], "材料记载预算为120万元。")
        self.assertEqual(artifact["citation_state"], "current")
        self.assertEqual(len(model_calls), 3)
        self.assertEqual(len(owner_write_calls), 1)
        conversation = self.request(
            "get", f"/api/intelligence/conversations/{conversation_id}", headers=self.auth_headers(),
        )
        self.assertEqual(conversation.status_code, 200)
        turn = conversation.json["data"]["turns"][0]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(len(turn["operations"]), 1)

    def test_media_tools_create_project_and_save_audio_bus_with_exact_version_scope(self) -> None:
        work = self.create_work("播客混音")
        prepared = self.service.prepare_context(
            self.identity, "为播客创建对话音频总线。", work_id=work["id"], include_memory=False,
        )
        context = self.service.create_tool_context(prepared, operation_scope="media-tool-test")
        catalog = self.service.tools.catalog()
        bus_tool = next(item for item in catalog["tools"] if item["tool"] == "media.audio_bus.configure")
        self.assertEqual(bus_tool["input_schema"]["properties"]["bus"]["type"], "object")

        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            created = self.service.invoke_tool(self.identity, "media.project.create", {
                "title": "播客混音草稿", "kind": "podcast", "work_id": work["id"],
            }, context)
            project = created["result"]["project"]
            self.assertEqual(project["current_version"], 1)
            configured = self.service.invoke_tool(self.identity, "media.audio_bus.configure", {
                "project_id": project["id"], "base_version": 1,
                "bus": {"bus_id": "dialogue", "name": "对话", "gain": 0.8, "pan": 0.0},
            }, context)
            saved = configured["result"]["project"]
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["audio_buses"][0]["bus_id"], "dialogue")
            with self.assertRaises(IntelligenceError) as stale:
                self.service.invoke_tool(self.identity, "media.audio_bus.remove", {
                    "project_id": project["id"], "base_version": 1, "bus_id": "dialogue",
                }, context)
        self.assertEqual(stale.exception.code, "INTELLIGENCE_TOOL_PARENT_VERSION_CHANGED")
        reopened = self.service.read_media_project(self.identity, project["id"], work_id=work["id"])
        self.assertEqual(reopened["current_version"], 2)
        self.assertEqual(reopened["audio_buses"][0]["name"], "对话")

        sample_rate, sample_count = 48_000, 12_000
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"\x00\x00" * sample_count)
        wav_data = wav_buffer.getvalue()
        upload = self.request(
            "post", "/api/uploads",
            json={"kind": "media", "title": "Take segment", "filename": "take.wav",
                  "expected_size": len(wav_data), "expected_sha256": hashlib.sha256(wav_data).hexdigest()},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-upload"},
        )
        self.assertEqual(upload.status_code, 201, upload.get_data(as_text=True))
        upload_id = upload.json["data"]["id"]
        chunk = self.request(
            "patch", f"/api/uploads/{upload_id}", data=wav_data,
            content_type="application/octet-stream",
            headers={**self.auth_headers(), "Upload-Offset": "0",
                     "X-Chunk-SHA256": hashlib.sha256(wav_data).hexdigest()},
        )
        self.assertEqual(chunk.status_code, 204, chunk.get_data(as_text=True))
        completed_upload = self.request("post", f"/api/uploads/{upload_id}/complete",
                                        headers=self.auth_headers())
        self.assertEqual(completed_upload.status_code, 201, completed_upload.get_data(as_text=True))
        take_material_id = completed_upload.json["data"]["material"]["id"]

        created_take = self.request(
            "post", f"/api/media/projects/{project['id']}/recordings",
            json={"base_version": 2, "cue_id": "cue-dialogue", "speaker_label": "主持人",
                  "track_kind": "voiceover", "timeline_start_sec": {"num": 0, "den": 1},
                  "sample_rate": sample_rate, "channels": 1, "sample_format": "pcm_s16le"},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-create"},
        )
        self.assertEqual(created_take.status_code, 201, created_take.get_data(as_text=True))
        take_id = created_take.json["data"]["id"]
        started = self.request(
            "post", f"/api/media/recordings/{take_id}/start", json={"capture_epoch_id": "capture-epoch-1"},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-start"},
        )
        self.assertEqual(started.status_code, 200, started.get_data(as_text=True))
        appended = self.request(
            "post", f"/api/media/recordings/{take_id}/segments",
            json={"sequence": 0, "material_id": take_material_id, "audio_stream_index": 0,
                  "capture_epoch_id": "capture-epoch-1", "capture_sample_start": 0,
                  "sample_count": sample_count},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-segment"},
        )
        self.assertEqual(appended.status_code, 201, appended.get_data(as_text=True))
        stopped = self.request(
            "post", f"/api/media/recordings/{take_id}/stop",
            json={"last_confirmed_sequence": 0, "native_capture_stopped": True},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-stop"},
        )
        self.assertEqual(stopped.status_code, 200, stopped.get_data(as_text=True))
        completed_take = self.request(
            "post", f"/api/media/recordings/{take_id}/complete", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-media-take-complete"},
        )
        self.assertEqual(completed_take.status_code, 201, completed_take.get_data(as_text=True))

        comp_context = self.service.prepare_context(
            self.identity, "用已完成的录音Take制作comp", work_id=work["id"],
            media_project_id=project["id"], include_memory=False,
        )
        comp_tool_context = self.service.create_tool_context(comp_context, operation_scope="media-take-comp-test")
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = comp_context.identity
            recording_list = self.service.invoke_tool(self.identity, "media.recordings.list", {
                "project_id": project["id"],
            }, comp_tool_context)["result"]["items"]
            recording = next(item for item in recording_list if item["id"] == take_id)
            self.assertEqual(recording["status"], "completed")
            current_version = comp_context.media_project["current_version"]
            comp = self.service.invoke_tool(self.identity, "media.take.comp", {
                "project_id": project["id"], "base_version": current_version,
                "cue_id": "cue-dialogue", "track_id": "comp-dialogue", "speaker_label": "主持人",
                "segments": [{"recording_session_id": take_id, "sequence": 0,
                              "source_in_sample": 0, "source_out_sample": sample_count,
                              "timeline_start_sec": {"num": 0, "den": 1}}],
            }, comp_tool_context)["result"]["project"]
        comp_track = next(track for track in comp["tracks"] if track["track_id"] == "comp-dialogue")
        self.assertEqual(comp_track["take_type"], "comp")
        self.assertEqual(len(comp_track["clips"]), 1)
        self.assertIn("dialogue", {bus["bus_id"] for bus in comp["audio_buses"]})
        reopened_comp = self.service.read_media_project(self.identity, project["id"], work_id=work["id"])
        self.assertEqual(reopened_comp["current_version"], comp["current_version"])

    def test_collaboration_feedback_read_revises_exact_artifact_and_reopens(self) -> None:
        work = self.create_work("客户意见精确修订")
        original_body = "服务方案：预算100万元，陈列窗口开放9小时。"
        artifact_response = self.request(
            "post", "/api/artifacts",
            json={"work_id": work["id"], "title": "服务方案", "body": original_body, "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        artifact = artifact_response.json["data"]
        quoted = "100万元"
        start = original_body.index(quoted)
        comment_response = self.request(
            "post", f"/api/artifacts/{artifact['id']}/comments",
            json={"artifact_version": 1,
                  "anchor": {"start": start, "end": start + len(quoted), "quote": quoted},
                  "body": "预算改为120万元，陈列窗口时长和其它内容保持。"},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-review-comment"},
        )
        self.assertEqual(comment_response.status_code, 201, comment_response.get_data(as_text=True))
        comment = comment_response.json["data"]
        prepared = self.service.prepare_context(
            self.identity, "按客户审阅意见只改预算数字。", work_id=work["id"],
            artifact_id=artifact["id"], artifact_version=1, include_memory=False,
        )
        context = self.service.create_tool_context(prepared, caller="model", operation_scope="reviewed-artifact-revision")
        catalog = self.service.tools.catalog()
        feedback_schema = next(item for item in catalog["tools"] if item["tool"] == "collaboration.read_feedback")
        self.assertEqual(feedback_schema["owner"], "Collaboration")
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            feedback = self.service.invoke_tool(self.identity, "collaboration.read_feedback", {
                "artifact_id": artifact["id"], "version": 1,
            }, context)["result"]
            self.assertEqual(feedback["comments"][0]["id"], comment["id"])
            self.assertEqual(feedback["comments"][0]["body"], "预算改为120万元，陈列窗口时长和其它内容保持。")
            prepared_with_feedback = self.service.extend_context(prepared, context)
        changed_comment = self.request(
            "patch", f"/api/artifacts/{artifact['id']}/comments/{comment['id']}",
            json={"base_version": 1, "body": "预算改为120万元，保留开放9小时。"},
            headers=self.auth_headers(),
        )
        self.assertEqual(changed_comment.status_code, 200, changed_comment.get_data(as_text=True))
        with self.assertRaises(IntelligenceError) as stale_feedback:
            self.service._revalidate_prepared_context(prepared_with_feedback)
        self.assertEqual(stale_feedback.exception.code, "INTELLIGENCE_CONTEXT_CHANGED")

        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="reviewed-artifact-revision-current-feedback",
        )
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            current_feedback = self.service.invoke_tool(self.identity, "collaboration.read_feedback", {
                "artifact_id": artifact["id"], "version": 1,
            }, context)["result"]
            self.assertEqual(current_feedback["comments"][0]["body"], "预算改为120万元，保留开放9小时。")
            revised = self.service.invoke_tool(self.identity, "artifact.revise_draft", {
                "artifact_id": artifact["id"], "base_version": 1, "title": "服务方案",
                "body": "服务方案：预算120万元，陈列窗口开放9小时。", "citations": [],
                "revision_note": "仅按客户对话意见修改预算数字",
            }, context)["result"]
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["body"], "服务方案：预算120万元，陈列窗口开放9小时。")

        cold_app = create_app(self.root, expected_port=8317)
        cold_service = cold_app.extensions["oevira.intelligence"]
        reopened = cold_service.read_artifact(self.identity, artifact["id"])
        self.assertEqual(reopened["version"], 2)
        self.assertEqual(reopened["body"], revised["body"])
        saved_comment = cold_app.test_client().get(
            f"/api/artifacts/{artifact['id']}/comments?version=1", base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(saved_comment.status_code, 200, saved_comment.get_data(as_text=True))
        self.assertEqual(saved_comment.json["data"][0]["state"], "open")

    def test_content_tool_creates_private_assistant_candidate_revises_and_reopens(self) -> None:
        work = self.create_work("内容发布草稿")
        artifact_response = self.request(
            "post", "/api/artifacts",
            json={"work_id": work["id"], "title": "活动方案", "body": "门店活动预算100万元。", "citations": []},
            headers=self.auth_headers(),
        )
        self.assertEqual(artifact_response.status_code, 201, artifact_response.get_data(as_text=True))
        source = artifact_response.json["data"]
        http_publication = self.request(
            "post", "/api/publications",
            json={"artifact_id": source["id"], "artifact_version": 1, "kind": "work",
                  "visibility": "private", "content": {
                      "title": "用户私有内容草稿", "summary": "用户直接创建的私有草稿。",
                      "body": "门店活动预算100万元。",
                  }},
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-private-publication-http"},
        )
        self.assertEqual(http_publication.status_code, 201, http_publication.get_data(as_text=True))
        self.assertEqual(http_publication.json["data"]["visibility"], "private")
        prepared = self.service.prepare_context(
            self.identity, "基于已选方案生成仅供审核的作品草稿。", work_id=work["id"],
            artifact_id=source["id"], artifact_version=1, include_memory=False,
        )
        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="content-publication-candidate",
        )
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            publication = self.service.invoke_tool(self.identity, "content.create_publication", {
                "artifact_id": source["id"], "artifact_version": 1, "kind": "work",
                "content": {"title": "门店活动记录", "summary": "按已选方案整理的私有草稿。",
                            "body": "门店活动预算100万元。"},
            }, context)["result"]
        self.assertEqual(publication["status"], "draft")
        self.assertEqual(publication["visibility"], "private")
        self.assertEqual(publication["content"]["assistant_provenance"]["origin"], "assistant_candidate")
        self.assertEqual(publication["source"], {"kind": "artifact", "id": source["id"], "version": 1})

        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            revised = self.service.invoke_tool(self.identity, "content.revise_publication", {
                "publication_id": publication["id"], "base_version": 1,
                "content": {"title": "门店活动记录", "summary": "按意见修订的私有草稿。",
                            "body": "门店活动预算120万元。"},
            }, context)["result"]
            readback = self.service.invoke_tool(self.identity, "content.read_publication", {
                "publication_id": publication["id"], "version": 2,
            }, context)["result"]
        self.assertEqual(revised["version"], 2)
        self.assertEqual(readback["content"]["body"], "门店活动预算120万元。")
        self.assertEqual(readback["content"]["assistant_provenance"]["source_artifact_version"], 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM publication_releases") ["n"], 0)
        prepared_with_publications = self.service.extend_context(prepared, context)
        continued = self.service.resume_from_saved_tool_results(
            prepared, tool_results=list(prepared_with_publications.tool_results),
            source_refs=list(prepared_with_publications.source_refs),
            citations=list(prepared_with_publications.citations), operation_scope="content-publication-resume",
        )
        self.assertEqual(continued.tool_results, prepared_with_publications.tool_results)

        cold_app = create_app(self.root, expected_port=8317)
        reopened = cold_app.test_client().get(
            f"/api/publications/{publication['id']}", base_url=BASE_URL,
            headers=self.auth_headers(),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["version"], 2)
        self.assertEqual(reopened.json["data"]["status"], "draft")
        self.assertEqual(reopened.json["data"]["visibility"], "private")
        self.assertEqual(reopened.json["data"]["content"]["assistant_provenance"]["origin"], "assistant_candidate")

    def test_standalone_media_project_is_a_valid_intelligence_scope_without_work(self) -> None:
        created = self.request(
            "post", "/api/media/projects",
            json={"title": "独立播客草稿", "kind": "podcast"},
            headers={**self.auth_headers(), "Idempotency-Key": "standalone-media-project"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        project_id = created.json["data"]["id"]
        queued = self.request(
            "post", "/api/intelligence/turns",
            json={"message": "为这个播客草稿设置对话音频总线。", "media_project_id": project_id},
            headers={**self.auth_headers(), "Idempotency-Key": "standalone-media-ai-turn"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        conversation = queued.json["data"]["conversation"]
        self.assertIsNone(conversation["work_id"])
        self.assertEqual(conversation["media_project_id"], project_id)
        prepared = self.service.prepare_context(
            self.identity, "为这个播客草稿设置对话音频总线。", media_project_id=project_id,
            include_memory=False,
        )
        self.assertIsNone(prepared.work)
        self.assertEqual(prepared.media_project["id"], project_id)
        self.service._revalidate_prepared_context(prepared)
        from server.oevira.worker import _claim_job
        claim = _claim_job(self.store)
        self.assertEqual(claim["id"], queued.json["data"]["job"]["id"])
        with self.app.app_context():
            process_intelligence_job(self.app, claim)
        reopened = self.request(
            "get", f"/api/intelligence/conversations/{conversation['conversation_id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(reopened.status_code, 200)
        turn = reopened.json["data"]["turns"][0]
        self.assertEqual(turn["status"], "partial")
        self.assertEqual(turn["error"]["code"], "MODEL_NOT_SELECTED")
        self.assertEqual(turn["context"]["media_project"]["version"], 1)

    def test_preserved_candidate_search_checks_private_hash_and_current_research_use(self) -> None:
        workspace_id = self.identity["workspace"]["id"]
        source_identity = r"lumenis:d:\historical\data".casefold()
        source_key = "ART-TEST-CANDIDATES"
        resource_id = str(uuid.uuid4())
        relative = f"objects/{workspace_id}/legacy-preserved/{resource_id}/original.bin"
        path = self.root.joinpath(*Path(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)

        def payload(rights):
            return {
                "source_inputs": {
                    "scope": {"domain_id": "test-domain", "brand_focus_id": "focus-test"},
                    "source_rights": rights,
                    "source_artifact_ref": "legacy-artifact-ref",
                    "source_evidence_ref": "legacy-evidence-ref",
                },
                "candidates": [{
                    "term": "云端零售话题", "kind": "DERIVED_TEXT_KEYWORD",
                    "candidate_origin": "historical_keyword_extraction", "occurrence_count": 5,
                    "body_locators": [{"start": 4, "end": 8, "offset_unit": "unicode_codepoint"}],
                    "admitted": False,
                }],
            }

        unreviewed = payload({"status": "UNREVIEWED", "allowed_uses": [], "third_party_rights_granted": False})
        raw = json.dumps(unreviewed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path.write_bytes(raw)
        with self.store.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS legacy_import_items (
                    workspace_id TEXT, source_identity TEXT, source_table TEXT, source_key TEXT,
                    resource_type TEXT, resource_id TEXT, input_sha256 TEXT, imported_at TEXT,
                    owner_adapter TEXT, disposition TEXT, metadata_json TEXT,
                    PRIMARY KEY(workspace_id, source_identity, source_table, source_key)
                );
                CREATE TABLE IF NOT EXISTS legacy_import_files (
                    workspace_id TEXT, source_identity TEXT, source_table TEXT, source_key TEXT,
                    resource_id TEXT, owner_adapter TEXT, disposition TEXT, source_relative_path TEXT,
                    private_relative_path TEXT, original_filename TEXT, expected_sha256 TEXT,
                    actual_sha256 TEXT, size_bytes INTEGER, created_at TEXT,
                    PRIMARY KEY(workspace_id, source_identity, source_table, source_key)
                );
            """)
            metadata = {"private_relative_path": relative, "expected_sha256": digest,
                        "actual_sha256": digest, "hash_matches": True}
            connection.execute(
                "INSERT INTO legacy_import_items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, source_identity, "lumenis_shared_artifacts", source_key,
                 "preserved_private_file", resource_id, _canonical_digest({"source_row": source_key}),
                 utc_now(), "Knowledge/Research owner: candidate sets, query scope, provenance and review history",
                 "owner_adapter_pending", json.dumps(metadata)),
            )
            connection.execute(
                "INSERT INTO legacy_import_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, source_identity, "lumenis_shared_artifacts", source_key, resource_id,
                 "Knowledge/Research owner: candidate sets, query scope, provenance and review history",
                 "owner_adapter_pending", "legacy-candidate.json", relative, "original.bin", digest, digest,
                 len(raw), utc_now()),
            )
        with self.store.transaction(immediate=False) as connection:
            blocked = _search_preserved_research_candidates(
                connection, self.store, workspace_id, ["云端", "零售"], 10,
            )
        self.assertEqual(blocked["status"], "source_use_blocked")
        self.assertEqual(blocked["results"], [])
        self.assertEqual(blocked["coverage"]["hash_verified_candidate_sets"], 1)

        admitted = payload({"status": "APPROVED", "allowed_uses": ["research"],
                            "third_party_rights_granted": True})
        raw = json.dumps(admitted, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path.write_bytes(raw)
        updated_metadata = {"private_relative_path": relative, "expected_sha256": digest,
                            "actual_sha256": digest, "hash_matches": True}
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE legacy_import_items SET metadata_json=? WHERE workspace_id=? AND source_identity=? "
                "AND source_table='lumenis_shared_artifacts' AND source_key=?",
                (json.dumps(updated_metadata), workspace_id, source_identity, source_key),
            )
            connection.execute(
                "UPDATE legacy_import_files SET expected_sha256=?, actual_sha256=?, size_bytes=? "
                "WHERE workspace_id=? AND source_identity=? AND source_table='lumenis_shared_artifacts' AND source_key=?",
                (digest, digest, len(raw), workspace_id, source_identity, source_key),
            )
        with self.store.transaction(immediate=False) as connection:
            allowed = _search_preserved_research_candidates(
                connection, self.store, workspace_id, ["云端", "零售"], 10,
            )
            self.assertEqual(allowed["status"], "complete")
            self.assertEqual(allowed["results"][0]["term"], "云端零售话题")
            self.assertEqual(allowed["results"][0]["evidence_state"], "UNVERIFIED_RESEARCH_CANDIDATE_NOT_FACT")
            self.assertNotIn("citations", allowed["results"][0])
            reference = {"kind": "legacy_research_candidate", **allowed["results"][0]["source"]}
            revalidate_preserved_research_candidate_ref(connection, self.store, self.identity, reference)

    @unittest.skipUnless(
        CODEX_TEST_RUNTIME_ASSET.is_file() and CODEX_TEST_RUNTIME_LICENSE.is_file(),
        "the official pinned app-server package/license are not staged",
    )
    def test_codex_app_server_dynamic_tool_call_uses_product_router_and_mock_responses_transport(self) -> None:
        skill = self.install_test_github_skill(tools=["artifact.create_draft", "artifact.revise_draft"])
        work = self.create_work("Codex harness工具回读")
        material = self.create_material(work["id"])
        citation = self.service.search_materials(
            self.identity, "预算", work_id=work["id"],
        )["citations"][0]
        model_connection = self.request(
            "post", "/api/intelligence/models",
            json={"name": "Isolated Responses test", "base_url": "https://models.example.com/v1",
                  "model_id": "local-contract-test-model", "api_key": "test-only-provider-secret",
                  "protocol": RESPONSES_PROTOCOL},
            headers={**self.auth_headers(), "Idempotency-Key": "create-responses-model"},
        )
        self.assertEqual(model_connection.status_code, 201, model_connection.get_data(as_text=True))
        selection_id = model_connection.json["data"]["connection_id"]
        self.assertNotIn("test-only-provider-secret", model_connection.get_data(as_text=True))
        selected_response = self.request(
            "post", f"/api/intelligence/models/{selection_id}/select", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "select-responses-model"},
        )
        self.assertEqual(selected_response.status_code, 200, selected_response.get_data(as_text=True))
        consent = self.request(
            "post", f"/api/intelligence/models/{selection_id}/data-use",
            json={"allow_scoped_data": True},
            headers={**self.auth_headers(), "Idempotency-Key": "allow-responses-model-data"},
        )
        self.assertEqual(consent.status_code, 200, consent.get_data(as_text=True))
        selection = self.service.model_selection_resolver(self.identity, selection_id)
        self.assertEqual(selection.protocol, RESPONSES_PROTOCOL)
        runtime_resolver = CodexAppServerRuntimeResolver(
            self.store, archive_path_for_test=CODEX_TEST_RUNTIME_ASSET,
            license_path_for_test=CODEX_TEST_RUNTIME_LICENSE,
        )
        provider_calls = []
        codex_events = []
        artifact_arguments = {
            "title": "Harness预算摘要", "body": "材料记载预算为120万元。",
            "citations": [citation], "work_id": work["id"],
        }

        def fake_responses_transport(payload, _connection):
            provider_calls.append(payload)
            self.assertEqual(len(payload["tools"]), 2, "The Skill only grants draft create/revise tools")
            if len(provider_calls) == 1:
                function = next(
                    tool for tool in payload["tools"]
                    if tool.get("name") == CODEX_TOOL_NAME_PREFIX + hashlib.sha256(
                        b"artifact.create_draft",
                    ).hexdigest()[:CODEX_TOOL_NAME_HASH_CHARACTERS]
                )
                function_call = {
                    "id": "fc_test_1", "type": "function_call", "status": "completed",
                    "call_id": "call_test_1", "name": function["name"],
                    "arguments": json.dumps(artifact_arguments, ensure_ascii=False),
                }
                events = [
                    {"type": "response.created", "sequence_number": 0,
                     "response": {"id": "resp-test-1", "object": "response", "status": "in_progress",
                                  "created_at": 1, "output": []}},
                    {"type": "response.output_item.added", "sequence_number": 1,
                     "output_index": 0, "item": {**function_call, "status": "in_progress", "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "sequence_number": 2,
                     "item_id": "fc_test_1", "output_index": 0,
                     "delta": json.dumps(artifact_arguments, ensure_ascii=False)},
                    {"type": "response.function_call_arguments.done", "sequence_number": 3,
                     "item_id": "fc_test_1", "output_index": 0,
                     "arguments": json.dumps(artifact_arguments, ensure_ascii=False)},
                    {"type": "response.output_item.done", "sequence_number": 4,
                     "output_index": 0, "item": function_call},
                    {"type": "response.completed", "sequence_number": 5,
                     "response": {"id": "resp-test-1", "object": "response", "status": "completed",
                                  "created_at": 1, "output": [function_call]}},
                ]
            elif len(provider_calls) == 3:
                function = next(
                    tool for tool in payload["tools"]
                    if tool.get("name") == CODEX_TOOL_NAME_PREFIX + hashlib.sha256(
                        b"artifact.revise_draft",
                    ).hexdigest()[:CODEX_TOOL_NAME_HASH_CHARACTERS]
                )
                revision_arguments = {
                    "artifact_id": saved_artifact_id[0], "base_version": 1,
                    "title": "Harness预算摘要（修订）",
                    "body": "修订：材料记载预算为120万元，仍待独立核验。",
                    "citations": [citation], "revision_note": "按用户意见补充核验状态",
                }
                function_call = {
                    "id": "fc_test_3", "type": "function_call", "status": "completed",
                    "call_id": "call_test_3", "name": function["name"],
                    "arguments": json.dumps(revision_arguments, ensure_ascii=False),
                }
                events = [
                    {"type": "response.created", "sequence_number": 0,
                     "response": {"id": "resp-test-3", "object": "response", "status": "in_progress",
                                  "created_at": 3, "output": []}},
                    {"type": "response.output_item.added", "sequence_number": 1,
                     "output_index": 0, "item": {**function_call, "status": "in_progress", "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "sequence_number": 2,
                     "item_id": "fc_test_3", "output_index": 0,
                     "delta": json.dumps(revision_arguments, ensure_ascii=False)},
                    {"type": "response.function_call_arguments.done", "sequence_number": 3,
                     "item_id": "fc_test_3", "output_index": 0,
                     "arguments": json.dumps(revision_arguments, ensure_ascii=False)},
                    {"type": "response.output_item.done", "sequence_number": 4,
                     "output_index": 0, "item": function_call},
                    {"type": "response.completed", "sequence_number": 5,
                     "response": {"id": "resp-test-3", "object": "response", "status": "completed",
                                  "created_at": 3, "output": [function_call]}},
                ]
            else:
                final_text = json.dumps({
                    "text": "已由Codex工具链保存并按意见修订预算摘要。", "citations": [citation],
                }, ensure_ascii=False)
                message = {
                    "id": "msg_test_2", "type": "message", "role": "assistant",
                    "status": "completed", "content": [{
                        "type": "output_text", "text": final_text, "annotations": [], "logprobs": [],
                    }],
                }
                events = [
                    {"type": "response.created", "sequence_number": 0,
                     "response": {"id": "resp-test-2", "object": "response", "status": "in_progress",
                                  "created_at": 2, "output": []}},
                    {"type": "response.output_item.added", "sequence_number": 1,
                     "output_index": 0, "item": {"id": "msg_test_2", "type": "message",
                                                  "role": "assistant", "status": "in_progress", "content": []}},
                    {"type": "response.output_text.delta", "sequence_number": 2,
                     "item_id": "msg_test_2", "output_index": 0, "content_index": 0, "delta": final_text},
                    {"type": "response.output_text.done", "sequence_number": 3,
                     "item_id": "msg_test_2", "output_index": 0, "content_index": 0, "text": final_text},
                    {"type": "response.output_item.done", "sequence_number": 4,
                     "output_index": 0, "item": message},
                    {"type": "response.completed", "sequence_number": 5,
                     "response": {"id": "resp-test-2", "object": "response", "status": "completed",
                                  "created_at": 2, "output": [message]}},
                ]
            return [
                b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
                for event in events
            ]

        isolated_codex = None
        try:
            codex_diagnostics = []
            saved_artifact_id = []
            service = self.service
            self.assertEqual(runtime_resolver.status()["state"], "NOT_INSTALLED_AUTO_INSTALL_ON_USE")
            runtime_resolver(self.identity, selection, "official-package-install-smoke")
            service.codex_runtime_resolver = runtime_resolver
            runtime_status = runtime_resolver.status()
            self.assertEqual(runtime_status["state"], "AVAILABLE_NOT_RUN", runtime_status)
            self.assertEqual(runtime_status["version"], CODEX_APP_SERVER_VERSION)
            self.assertTrue(runtime_status["authenticode_verified"])
            self.assertIn(str(self.store.root), str(runtime_resolver(self.identity, selection, "official-package-install-smoke").executable))
            service.codex_gateway = CodexAppServerGateway(
                service, connection_resolver=service.model_settings.resolve_connection,
                runtime_resolver=service.codex_runtime_resolver,
                transport_for_test=fake_responses_transport,
            )
            service.codex_gateway.diagnostic_callback = codex_diagnostics.append
            request_body = {
                "message": "从材料中核验预算，保存摘要。", "selection_id": selection_id,
                "work_id": work["id"], "query": "预算",
                "skill_ids": [skill["skill_id"]],
            }
            accepted = self.request(
                "post", "/api/intelligence/turns", json=request_body,
                headers={**self.auth_headers(), "Idempotency-Key": "codex-harness-api-v1"},
            )
            self.assertEqual(accepted.status_code, 202, accepted.get_data(as_text=True))
            conversation_id = accepted.json["data"]["conversation"]["conversation_id"]
            job_id = accepted.json["data"]["job"]["id"]
            from server.oevira.worker import _claim_job
            first_claim = _claim_job(self.store)
            self.assertEqual(first_claim["id"], job_id)
            with self.app.app_context():
                process_intelligence_job(self.app, first_claim)
            first_reopen = self.request(
                "get", f"/api/intelligence/conversations/{conversation_id}",
                headers=self.auth_headers(),
            )
            self.assertEqual(first_reopen.status_code, 200, first_reopen.get_data(as_text=True))
            first_turn = first_reopen.json["data"]["turns"][0]
            self.assertEqual(first_turn["status"], "completed", {
                **first_turn, "codex_diagnostics": codex_diagnostics,
                "provider_call_count": len(provider_calls),
            })
            result = {"status": first_turn["status"], "text": first_turn["assistant_message"],
                      "operations": first_turn["operations"]}
            self.assertEqual(result["status"], "completed", {
                **result, "provider_request_count_in_fake_transport": len(provider_calls),
                "codex_events": codex_events,
                "codex_diagnostic": codex_diagnostics,
                "provider_tool_names": [
                    [tool.get("name") for tool in request.get("tools", [])]
                    for request in provider_calls
                ],
            })
            self.assertEqual(len(provider_calls), 2)
            self.assertTrue(all(
                all(tool.get("type") == "function" and tool.get("name", "").startswith("oevira_")
                    for tool in request.get("tools", []))
                for request in provider_calls
            ))
            self.assertEqual(result["text"], "已由Codex工具链保存并按意见修订预算摘要。")
            self.assertTrue(result["operations"], result)
            self.assertIn("artifact", result["operations"][0], result["operations"])
            saved = result["operations"][0]["artifact"]
            saved_artifact_id.append(saved["id"])
            reopened = service.read_artifact(self.identity, saved["id"])
            self.assertEqual(reopened["body"], "材料记载预算为120万元。")
            self.assertEqual(reopened["citation_state"], "current")

            revise_body = {
                "message": "按意见补充核验状态并保存修订。", "selection_id": selection_id,
                "work_id": work["id"], "query": "预算", "artifact_id": saved["id"],
                "artifact_version": 1, "artifact_write_intent": True,
            }
            revise_queued = self.request(
                "post", f"/api/intelligence/conversations/{conversation_id}/turns",
                json=revise_body,
                headers={**self.auth_headers(), "Idempotency-Key": "codex-harness-revise-v1"},
            )
            self.assertEqual(revise_queued.status_code, 202, revise_queued.get_data(as_text=True))
            second_claim = _claim_job(self.store)
            self.assertEqual(second_claim["id"], revise_queued.json["data"]["job"]["id"])
            with self.app.app_context():
                process_intelligence_job(self.app, second_claim)
            final_conversation = self.request(
                "get", f"/api/intelligence/conversations/{conversation_id}",
                headers=self.auth_headers(),
            )
            self.assertEqual(final_conversation.status_code, 200)
            turns = final_conversation.json["data"]["turns"]
            self.assertEqual([turn["status"] for turn in turns], ["completed", "completed"])
            self.assertNotIn("codex_thread_id", final_conversation.json["data"]["conversation"])
            self.assertEqual(len(provider_calls), 4)
            self.assertIn("oevira-artifact-revision", json.dumps(provider_calls, ensure_ascii=False))
            self.assertNotIn("test-only-provider-secret", json.dumps(provider_calls, ensure_ascii=False))
            self.assertNotIn("唯一开发入口", json.dumps(provider_calls, ensure_ascii=False))
            reopened_revision = service.read_artifact(self.identity, saved["id"])
            self.assertEqual(reopened_revision["version"], 2)
            self.assertEqual(reopened_revision["body"], "修订：材料记载预算为120万元，仍待独立核验。")
            self.assertEqual(reopened_revision["citation_state"], "current")
        finally:
            if isolated_codex is not None:
                isolated_codex.cleanup()

    def test_default_model_is_not_selected_and_no_unapproved_send_occurs(self) -> None:
        self.assertEqual(self.service.status()["user_configured_model_route"], "SETTINGS_OWNER_BOUND")
        work = self.create_work("无模型路径")
        context = self.service.prepare_context(self.identity, "检查预算", work_id=work["id"])
        result = self.service.run_model(context, selection_id=None)
        self.assertEqual(result["code"], "MODEL_NOT_SELECTED")
        self.assertEqual(result["execution"], "NOT_RUN")
        self.assertFalse(result["fallback_allowed"])

    def test_model_connection_is_dpapi_protected_user_scoped_and_explicitly_authorized(self) -> None:
        official_openai = self.service.model_settings._normalize_configuration(
            {"name": "Official OpenAI", "base_url": "https://api.openai.com/v1",
             "model_id": "selected-test-model", "api_key": "non-production-test-key"},
            require_key=True,
        )
        self.assertEqual(official_openai["token_field"], "max_completion_tokens")
        test_key = "unit-test-api-key-never-returned"
        created = self.request(
            "post", "/api/intelligence/models",
            json={"name": "Test connection", "base_url": "https://api.example.com/v1",
                  "model_id": "selected-test-model", "api_key": test_key,
                  "protocol": "openai_chat_completions"},
            headers={**self.auth_headers(), "Idempotency-Key": "create-model-connection"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        connection_id = created.json["data"]["connection_id"]
        self.assertNotIn("api_key", created.json["data"])
        self.assertNotIn(test_key, created.get_data(as_text=True))
        stored = self.store.row(
            "SELECT credential_ciphertext, credential_protection, allow_scoped_data, is_selected "
            "FROM intelligence_model_connections WHERE connection_id = ?", (connection_id,),
        )
        self.assertIsNotNone(stored["credential_ciphertext"])
        self.assertNotIn(test_key.encode("utf-8"), bytes(stored["credential_ciphertext"]))
        self.assertEqual(stored["credential_protection"], "windows_dpapi_current_user_v1")
        self.assertEqual(stored["allow_scoped_data"], 0)
        self.assertEqual(stored["is_selected"], 0)

        self.service.model_settings.chat_test_runner = (
            lambda _selection, _connection, payload: {
                "status": "completed", "text": "READY", "sources": payload["evidence"]["sources"],
            }
        )
        test_result = self.request(
            "post", f"/api/intelligence/models/{connection_id}/test",
            json={"confirm_no_customer_data": True},
            headers={**self.auth_headers(), "Idempotency-Key": "test-model-connection"},
        )
        self.assertEqual(test_result.status_code, 200, test_result.get_data(as_text=True))
        self.assertEqual(test_result.json["data"]["state"], "CONNECTED")
        self.assertFalse(test_result.json["data"]["customer_data_sent"])

        selected_response = self.request(
            "post", f"/api/intelligence/models/{connection_id}/select", json={},
            headers={**self.auth_headers(), "Idempotency-Key": "select-model-connection"},
        )
        self.assertEqual(selected_response.status_code, 200, selected_response.get_data(as_text=True))
        selection = self.service.model_selection_resolver(self.identity, connection_id)
        self.assertIs(type(selection), ExplicitModelSelection)
        other_identity = self._create_other_identity()
        self.assertIsNone(self.service.model_selection_resolver(other_identity, connection_id))
        with self.assertRaises(ModelSettingsError) as denied:
            self.service.model_settings.authorize_model_input(
                self.identity, selection, [{"kind": "user_input"}], purpose="inference",
            )
        self.assertEqual(denied.exception.code, "MODEL_DATA_USE_NOT_AUTHORIZED")

        consent = self.request(
            "post", f"/api/intelligence/models/{connection_id}/data-use",
            json={"allow_scoped_data": True},
            headers={**self.auth_headers(), "Idempotency-Key": "allow-model-data-use"},
        )
        self.assertEqual(consent.status_code, 200, consent.get_data(as_text=True))
        selection = self.service.model_selection_resolver(self.identity, connection_id)
        source_refs = [{"kind": "user_input", "content_sha256": "a" * 64}]
        authorization = self.service.model_settings.authorize_model_input(
            self.identity, selection, source_refs, purpose="inference",
        )
        request_payload = {"model": selection.model_id, "messages": [{"role": "user", "content": "test"}]}
        request_hash = _canonical_digest(request_payload)
        approval = self.service.model_settings.authorize_provider_request(
            self.identity, selection, authorization, request_payload, request_hash,
        )
        self.assertIs(type(approval), ExternalModelApproval)
        self.assertEqual(approval.request_sha256, request_hash)
        admission = self.store.row(
            "SELECT state, request_sha256 FROM intelligence_model_admissions WHERE admission_id = ?",
            (approval.approval_id,),
        )
        self.assertEqual(admission["state"], "request_approved")
        self.assertEqual(admission["request_sha256"], request_hash)

        work = self.create_work("用户配置模型推理")
        self.create_material(work["id"])
        context = self.service.prepare_context(self.identity, "核验已选材料", work_id=work["id"])
        captured = {}

        class TestResponse:
            def read(self, _limit):
                return json.dumps({"choices": [{"message": {
                    "content": json.dumps({"text": "根据已选材料完成。", "citations": []}, ensure_ascii=False),
                }}]}).encode("utf-8")

        def fake_open(destination, hostname, body, api_key, timeout):
            captured.update(destination=destination, hostname=hostname, body=body.decode("utf-8"), api_key=api_key)
            return nullcontext(TestResponse())

        with patch("server.oevira.intelligence_provider._open_pinned_https", side_effect=fake_open), self.app.test_request_context(
            "/api/intelligence/worker/turn",
        ):
            g.identity = self.identity
            inference = self.service.run_model(context, selection_id=connection_id)
        self.assertEqual(inference["status"], "completed", inference)
        self.assertEqual(inference["text"], "根据已选材料完成。")
        self.assertEqual(captured["api_key"], test_key)
        self.assertNotIn(test_key, captured["body"])
        exact_body_hash = hashlib.sha256(captured["body"].encode("utf-8")).hexdigest()
        self.assertTrue(self.store.row(
            "SELECT 1 FROM intelligence_model_admissions WHERE state = 'request_approved' AND request_sha256 = ?",
            (exact_body_hash,),
        ))

        revoked = self.request(
            "delete", f"/api/intelligence/models/{connection_id}",
            headers={**self.auth_headers(), "Idempotency-Key": "revoke-model-connection"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        after_revoke = self.store.row(
            "SELECT credential_ciphertext, status, allow_scoped_data, is_selected "
            "FROM intelligence_model_connections WHERE connection_id = ?", (connection_id,),
        )
        self.assertIsNone(after_revoke["credential_ciphertext"])
        self.assertEqual((after_revoke["status"], after_revoke["allow_scoped_data"], after_revoke["is_selected"]),
                         ("revoked", 0, 0))

    def test_http_conversation_queue_worker_and_reopen_use_existing_job_and_store(self) -> None:
        work = self.create_work("Intelligence API消费")
        material = self.create_material(work["id"])
        request_body = {
            "message": "检查预算并指出原文位置。", "work_id": work["id"],
            "requested_output_formats": [],
        }
        accepted = self.request(
            "post", "/api/intelligence/turns", json=request_body,
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-api-turn-v1"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.get_data(as_text=True))
        conversation_id = accepted.json["data"]["conversation"]["conversation_id"]
        job_id = accepted.json["data"]["job"]["id"]
        self.assertEqual(accepted.json["data"]["turn"]["status"], "queued")

        replay = self.request(
            "post", "/api/intelligence/turns", json=request_body,
            headers={**self.auth_headers(), "Idempotency-Key": "intelligence-api-turn-v1"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["data"]["replayed"])
        self.assertEqual(replay.json["data"]["job"]["id"], job_id)

        from server.oevira.worker import _claim_job
        claim = _claim_job(self.store)
        self.assertEqual(claim["id"], job_id)
        with self.app.app_context():
            process_intelligence_job(self.app, claim)

        reopened = self.request(
            "get", f"/api/intelligence/conversations/{conversation_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        item = reopened.json["data"]["turns"][0]
        self.assertEqual(item["status"], "partial")
        self.assertEqual(item["error"]["code"], "MODEL_NOT_SELECTED")
        self.assertEqual(item["user_message"], request_body["message"])
        self.assertIn(material["id"], {source["material_id"] for source in item["context"]["evidence"]["sources"]})
        self.assertEqual(reopened.json["data"]["conversation"]["conversation_id"], conversation_id)
        with self.store.transaction() as connection:
            connection.execute("UPDATE jobs SET status = 'failed' WHERE id = ? AND kind = 'intelligence_turn'", (job_id,))
        retry = self.request(
            "post", f"/api/jobs/{job_id}/retry", headers=self.auth_headers(),
        )
        self.assertEqual(retry.status_code, 409, retry.get_data(as_text=True))
        self.assertEqual(retry.json["error"]["code"], "INTELLIGENCE_RESULT_RECONCILIATION_REQUIRED")
        archived = self.request(
            "post", f"/api/intelligence/conversations/{conversation_id}/archive",
            headers=self.auth_headers(),
        )
        self.assertEqual(archived.status_code, 200, archived.get_data(as_text=True))
        self.assertEqual(archived.json["data"]["status"], "archived")
        all_conversations = self.request(
            "get", "/api/intelligence/conversations?status=all", headers=self.auth_headers(),
        )
        self.assertEqual(all_conversations.status_code, 200, all_conversations.get_data(as_text=True))
        self.assertIn(conversation_id, {item["conversation_id"] for item in all_conversations.json["data"]})
        restored = self.request(
            "post", f"/api/intelligence/conversations/{conversation_id}/restore",
            headers=self.auth_headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(restored.json["data"]["status"], "active")

    def test_math_104_uses_exact_rationals_and_returns_checkpoint_without_partial_winners(self) -> None:
        problem = {
            "vertices": [
                {"id": "x", "owner": "max"},
                {"id": "c", "owner": "chance"},
                {"id": "z", "owner": "min"},
            ],
            "edges": [
                {"id": "x-a", "source": "x", "target": "z", "reward": 1},
                {"id": "x-b", "source": "x", "target": "c", "reward": -2},
                {"id": "c-a", "source": "c", "target": "x", "reward": 0,
                 "probability": {"numerator": 1, "denominator": 3}},
                {"id": "c-b", "source": "c", "target": "z", "reward": 1,
                 "probability": {"numerator": 2, "denominator": 3}},
                {"id": "z-loop", "source": "z", "target": "z", "reward": -1},
            ],
        }
        vertices, identifiers, denominator_product = _read_game(problem)
        self.assertEqual(identifiers, ("x", "c", "z"))
        self.assertEqual(denominator_product, 9)
        self.assertEqual(
            _discounted_floor(vertices, (1, 2, -3), 128, 10, 20, _WorkBudget(MeanPayoffBudget(), None)),
            (0, -2, -4),
        )
        self.assertEqual(
            _run_label(
                _LabelCall((-1, -1, -1), 128, (Fraction(2),) * 3, (Fraction(2),) * 3),
                vertices, 10, 20, _WorkBudget(MeanPayoffBudget(), None),
            ),
            ("+", "+", "+"),
        )

        partial = evaluate_mean_payoff_winning_region(problem, budget=MeanPayoffBudget(max_steps=1))
        self.assertEqual(partial["state"], "PARTIAL")
        self.assertIsNone(partial["winner_vertices"])
        self.assertEqual(partial["checkpoint"]["input_sha256"], partial["input_sha256"])
        cancelled = evaluate_mean_payoff_winning_region(problem, is_cancelled=lambda: True)
        self.assertEqual(cancelled["reason"], "CANCELLED")
        self.assertIsNone(cancelled["mean_payoff_intervals"])

        negative_loop = {
            "vertices": [{"id": "min_loop", "owner": "min"}],
            "edges": [{"id": "loop", "source": "min_loop", "target": "min_loop", "reward": -1}],
        }
        solved_negative = evaluate_mean_payoff_winning_region(
            negative_loop,
            budget=MeanPayoffBudget(max_steps=2_000_000, max_integer_bits=200_000, max_frames=100_000),
        )
        self.assertEqual(solved_negative["state"], "READY")
        self.assertEqual(solved_negative["winner_vertices"], [])
        self.assertLess(
            solved_negative["mean_payoff_intervals"]["min_loop"]["upper"]["numerator"], 0,
        )

        invalid = json.loads(json.dumps(problem))
        invalid["edges"][3]["probability"] = {"numerator": 1, "denominator": 3}
        with self.assertRaises(IntelligenceMathError) as invalid_chance:
            _read_game(invalid)
        self.assertEqual(invalid_chance.exception.code, "CHANCE_PROBABILITIES_MUST_SUM_TO_ONE")

    def test_intelligence_analysis_tool_saves_exact_finite_game_result_without_model_claims(self) -> None:
        work = self.create_work("有限策略计算")
        goal = "求解只有状态v且每轮收益为2的MAX平均收益游戏，并保存可读结果。"
        prepared = self.service.prepare_context(
            self.identity, goal, work_id=work["id"], include_memory=False,
        )
        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="finite-game-analysis",
        )
        problem = {
            "vertices": [{"id": "v", "owner": "max"}],
            "edges": [{"id": "repeat", "source": "v", "target": "v", "reward": 2}],
        }
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            operation = self.service.invoke_tool(self.identity, "analysis.finite_state_game", {
                "title": "有限策略收益计算", "problem": problem, "citations": [],
            }, context)
        result = operation["result"]
        self.assertEqual(operation["owner"], "Analysis")
        self.assertEqual(result["calculation"]["state"], "READY")
        self.assertEqual(result["calculation"]["mean_payoff_values"]["v"], {"numerator": 2, "denominator": 1})
        self.assertEqual(result["input_provenance"], "user_task_input_not_independently_verified")
        artifact = self.service.read_artifact(self.identity, result["artifact_id"], version=1)
        self.assertIn("user_task_input_not_independently_verified", artifact["body"])
        self.assertIn('"denominator": 1', artifact["body"])

        cold_app = create_app(self.root, expected_port=8317)
        reopened = cold_app.extensions["oevira.intelligence"].read_artifact(
            self.identity, result["artifact_id"], version=1,
        )
        self.assertEqual(reopened["body"], artifact["body"])
        self.assertEqual(reopened["input_snapshot"]["work"]["id"], work["id"])

    def test_intelligence_analysis_matrix_product_verifies_tensor_and_saves_exact_result(self) -> None:
        work = self.create_work("精确矩阵乘积")
        prepared = self.service.prepare_context(
            self.identity, "精确计算本Work选定的3×2与2×4有理矩阵乘积并保存结果。",
            work_id=work["id"], include_memory=False,
        )
        context = self.service.create_tool_context(
            prepared, caller="model", operation_scope="analysis-matrix-product",
        )
        from server.tests.test_intelligence_math_107 import rank_eight_two_by_two_witness
        from server.oevira.intelligence_math_107 import exact_standard_matrix_product
        matrix_a = [[1, 2], [3, 4], [5, 6]]
        matrix_b = [[7, 8, 9, 10], [11, 12, 13, 14]]
        witness = rank_eight_two_by_two_witness()
        standard = exact_standard_matrix_product(matrix_a, matrix_b)
        self.assertEqual(standard["state"], "READY")
        with self.app.test_request_context("/api/intelligence/turns"):
            g.identity = prepared.identity
            operation = self.service.invoke_tool(self.identity, "analysis.matrix_product", {
                "title": "有理矩阵乘积", "matrix_a": matrix_a, "matrix_b": matrix_b,
                "witness": witness, "epsilon": 1, "citations": [],
            }, context)
        self.assertEqual(operation["owner"], "Analysis")
        result = operation["result"]
        self.assertEqual(result["calculation"]["state"], "READY")
        self.assertEqual(result["calculation"]["implementation_path"], "VERIFIED_FINITE_BILINEAR_TENSOR")
        self.assertEqual(result["calculation"]["result"], standard["matrix"])
        self.assertFalse(result["calculation"]["witness"]["finite_rank_bound"]["finite_bound_met"])
        artifact = self.service.read_artifact(self.identity, result["artifact_id"], version=1)
        self.assertIn("全局渐近指数", artifact["body"])
        cold_app = create_app(self.root, expected_port=8317)
        reopened = cold_app.extensions["oevira.intelligence"].read_artifact(
            self.identity, result["artifact_id"], version=1,
        )
        self.assertEqual(reopened["body"], artifact["body"])

    def test_cpu_admission_rejects_synthetic_and_without_current_owners(self) -> None:
        work = self.create_work("CPU准入检查")
        material = self.create_material(work["id"])
        reference = {
            "kind": "material_segment", "material_id": material["id"],
            "material_sha256": material["sha256"],
            "segment_id": material["segments"][0]["id"],
            "quote": material["segments"][0]["text"],
        }
        example = {
            "sample_id": "synthetic-test-row", "group_id": "test-group-1", "partition": "train",
            "features": {"preferred": {"passage.text": "预算120万元"},
                         "other": {"passage.text": "预算90万元"}},
            "label": {"preferred_candidate": "preferred", "other_candidate": "other"},
            "source_refs": [reference], "label_schema_revision": "fixture-only-v1",
            "created_by": self.identity["user"]["id"], "synthetic": True,
        }
        with self.assertRaises(IntelligenceError) as raised:
            self.service.admit_training_dataset(self.identity, "evidence_ranking", [example])
        self.assertEqual(raised.exception.code, "TRAINING_SOURCE_USE_OWNER_UNAVAILABLE")

        fixture_admission = TrainingDataAdmission(
            self.store, source_use_resolver=lambda *args, **kwargs: None,
            label_review_resolver=lambda *args, **kwargs: None,
        )
        with self.assertRaises(CPUTrainingBlocked) as blocked:
            fixture_admission.admit(self.identity, "evidence_ranking", [example])
        self.assertEqual(blocked.exception.code, "TRAINING_SYNTHETIC_SAMPLE_NOT_ADMITTED")
        self.assertEqual(TASK_SCHEMAS["evidence_ranking"]["model"], "linear_pairwise_ranker")
        self.assertFalse(self.service.training_status()["fitting_performed"])

    def test_private_github_skill_connection_is_encrypted_scoped_and_revocable(self) -> None:
        class TestOnlyProtector:
            def protect(self, data: bytes, entropy: bytes) -> bytes:
                return bytes(value ^ entropy[index % len(entropy)] for index, value in enumerate(data))

            def unprotect(self, data: bytes, entropy: bytes) -> bytes:
                return self.protect(data, entropy)

        token = "github_pat_test_private_skill_access"
        rotated_token = "github_pat_test_private_skill_rotated"
        accepted_tokens = {token}
        skill_text = (
            "---\nname: private-brand-analysis\n"
            "description: Apply the team's approved brand analysis process.\n"
            "allowed-tools: [artifact.create_draft]\n---\n"
            "# Brand analysis\nUse current work evidence and save a cited draft."
        ).encode("utf-8")
        license_text = b"SPDX-License-Identifier: MIT\nCopyright 2026 Example\n"
        contents = {"SKILL.md": skill_text, "LICENSE": license_text}
        blob_ids = {
            name: hashlib.sha1(b"blob " + str(len(value)).encode("ascii") + b"\0" + value).hexdigest()
            for name, value in contents.items()
        }
        commit_sha = hashlib.sha1(b"private-skill-main").hexdigest()
        tree_sha = hashlib.sha1(b"private-skill-tree").hexdigest()
        blobs = {blob_ids[name]: value for name, value in contents.items()}
        authorized_calls = []

        def transport(path: str, access_token: str | None = None):
            authorized_calls.append((path, access_token))
            if path == "/user":
                if access_token not in accepted_tokens:
                    return 401, {}, b'{"message":"Bad credentials"}'
                return 200, {}, b'{"login":"brand-owner"}'
            if path == "/user/repos?visibility=all&sort=updated&per_page=100&page=1":
                if access_token not in accepted_tokens:
                    return 401, {}, b'{"message":"Bad credentials"}'
                return 200, {}, json.dumps([{
                    "full_name": "acme/private-skills", "private": True, "description": "Private brand workflow",
                    "default_branch": "main", "owner": {"login": "acme"}, "license": None,
                }]).encode()
            if path == "/repos/acme/private-skills":
                if access_token not in accepted_tokens:
                    return 404, {}, b'{"message":"Not Found"}'
                return 200, {}, b'{"private":true,"default_branch":"main","license":null}'
            if path == "/repos/acme/private-skills/commits/main":
                return 200, {}, json.dumps({"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}).encode()
            if path == f"/repos/acme/private-skills/git/trees/{tree_sha}?recursive=1":
                tree = [
                    {"path": "skills/brand/SKILL.md", "type": "blob", "mode": "100644",
                     "sha": blob_ids["SKILL.md"], "size": len(skill_text)},
                    {"path": "LICENSE", "type": "blob", "mode": "100644",
                     "sha": blob_ids["LICENSE"], "size": len(license_text)},
                ]
                return 200, {}, json.dumps({"tree": tree, "truncated": False}).encode()
            if path.startswith("/repos/acme/private-skills/git/blobs/"):
                blob_id = path.rsplit("/", 1)[-1]
                content = blobs[blob_id]
                return 200, {}, json.dumps({
                    "sha": blob_id, "encoding": "base64", "content": base64.b64encode(content).decode("ascii"),
                    "size": len(content),
                }).encode()
            raise AssertionError(f"Unexpected GitHub request: {path}")

        self.service.skills.source = GitHubSkillSource(transport=transport)
        self.service.skills.protector = TestOnlyProtector()
        created = self.request(
            "post", "/api/intelligence/skills/connections", json={"name": "品牌技能库", "access_token": token},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-connection-create"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertNotIn(token, created.get_data(as_text=True))
        connection_id = created.json["data"]["connection_id"]
        connection_row = self.store.row(
            "SELECT * FROM intelligence_github_connections WHERE connection_id = ?", (connection_id,),
        )
        self.assertNotIn(token.encode(), bytes(connection_row["credential_ciphertext"]))
        self.assertEqual(connection_row["provider_login"], "brand-owner")

        other_identity = self._create_other_identity()
        self.assertEqual(self.service.skills.list_connections(other_identity), [])
        with self.assertRaises(SkillLifecycleError) as wrong_owner:
            self.service.skills._read_connection_token(other_identity, connection_id)
        self.assertEqual(wrong_owner.exception.code, "GITHUB_CONNECTION_NOT_FOUND")

        discovered = self.request(
            "post", "/api/intelligence/skills/discover",
            json={"query": "brand", "github_connection_id": connection_id}, headers=self.auth_headers(),
        )
        self.assertEqual(discovered.status_code, 200, discovered.get_data(as_text=True))
        self.assertTrue(discovered.json["data"]["items"][0]["private"])
        anonymous_inspection = self.request(
            "post", "/api/intelligence/skills/inspect",
            json={"repository": "acme/private-skills", "reference": "main", "skill_path": "skills/brand"},
            headers=self.auth_headers(),
        )
        self.assertEqual(anonymous_inspection.json["error"]["code"], "GITHUB_RESOURCE_NOT_FOUND")
        inspected = self.request(
            "post", "/api/intelligence/skills/inspect",
            json={"repository": "acme/private-skills", "reference": "main", "skill_path": "skills/brand",
                  "github_connection_id": connection_id}, headers=self.auth_headers(),
        )
        self.assertEqual(inspected.status_code, 201, inspected.get_data(as_text=True))
        candidate = inspected.json["data"]
        self.assertEqual(candidate["repository_visibility"], "private")
        self.assertEqual(candidate["github_connection_id"], connection_id)
        self.assertNotIn(token, inspected.get_data(as_text=True))

        installed = self.request(
            "post", "/api/intelligence/skills/install",
            json={"candidate_id": candidate["candidate_id"], "candidate_sha256": candidate["content_sha256"],
                  "license_sha256": candidate["license"]["file_sha256"], "rights_reviewed": True,
                  "tool_names": ["artifact.create_draft"]},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-install"},
        )
        self.assertEqual(installed.status_code, 201, installed.get_data(as_text=True))
        skill_id = installed.json["data"]["skill_id"]
        bound = self.store.row(
            "SELECT github_connection_id, github_connection_revision FROM intelligence_skill_versions "
            "WHERE skill_id = ? AND version_number = 1", (skill_id,),
        )
        self.assertEqual(bound["github_connection_id"], connection_id)
        self.assertEqual(bound["github_connection_revision"], 1)
        tested = self.request(
            "post", f"/api/intelligence/skills/{skill_id}/self-test",
            json={"version": 1, "content_sha256": candidate["content_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-test"},
        )
        self.assertEqual(tested.status_code, 200, tested.get_data(as_text=True))
        enabled = self.request(
            "post", f"/api/intelligence/skills/{skill_id}/enable",
            json={"version": 1, "content_sha256": candidate["content_sha256"],
                  "tool_schema_sha256": installed.json["data"]["tool_schema_sha256"]},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-enable"},
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        snapshots, prompts = self.service.skills.resolve_enabled(self.identity, [skill_id], self.service)
        self.assertEqual(snapshots[0]["version"], 1)
        self.assertEqual(prompts[0]["instructions_trust"], "UNTRUSTED_THIRD_PARTY_PROMPT_CONTENT")
        self.assertEqual(prompts[0]["allowed_tools"], ["artifact.create_draft"])

        work = self.create_work("私有技能工具调用")
        self.create_material(work["id"], text="品牌社群反馈集中于产品耐用与服务及时。")
        citation = self.service.search_materials(self.identity, "品牌", work_id=work["id"])["citations"][0]
        selection = ExplicitModelSelection(
            route="owned", selection_id="private-skill-owned", revision="private-skill-model-revision",
            model_id="test-owned-small-model", recipient_id="local-only", selected_by_user=True,
        )
        model_calls = []
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()

        def resolve_selection(identity, selection_id):
            return selection if selection_id == selection.selection_id else None

        def authorize_source(identity, selected, source_refs, *, purpose):
            return ModelInputAuthorization(
                allowed=True, workspace_id=identity["workspace"]["id"], user_id=identity["user"]["id"],
                selection_id=selected.selection_id, selection_revision=selected.revision,
                route=selected.route, model_id=selected.model_id,
                source_ref_sha256=_canonical_digest(list(source_refs)), purpose=purpose,
                recipient=selected.recipient_id, authorization_id="private-skill-test-authorization",
                expires_at=expires_at,
            )

        def run_model(selected, payload, *, is_cancelled=None):
            model_calls.append(payload)
            if not payload.get("tool_results"):
                return {"status": "completed", "tool_calls": [{
                    "name": "artifact.create_draft",
                    "arguments": {"title": "品牌社群洞察", "body": "材料记录品牌反馈集中于产品耐用与服务及时。",
                                  "citations": [citation], "work_id": work["id"]},
                }]}
            return {"status": "completed", "text": "已保存带来源的品牌反馈摘要。", "citations": [citation]}

        self.service.model_selection_resolver = resolve_selection
        self.service.source_use_authorizer = authorize_source
        self.service.owned_inference_runner = run_model
        prepared = self.service.prepare_context(
            self.identity, "按团队方法分析品牌反馈并保存摘要", work_id=work["id"], query="品牌",
        )
        prepared = self.service.with_skill_context(prepared, snapshots)
        with self.app.test_request_context(
            "/api/intelligence/turn", headers={"Idempotency-Key": "private-skill-product-tool-turn"},
        ):
            g.identity = prepared.identity
            result = self.service.run_model(prepared, selection_id=selection.selection_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["tool_results"]), 1)
        self.assertEqual(result["tool_results"][0]["tool"], "artifact.create_draft")
        self.assertIn("private-brand-analysis", json.dumps(model_calls[0]["skills"]))
        saved = result["tool_results"][0]["result"]
        reopened = self.service.read_artifact(self.identity, saved["id"])
        self.assertEqual(reopened["version"], 1)
        self.assertIn("产品耐用", reopened["body"])

        accepted_tokens.add(rotated_token)
        rotated = self.request(
            "put", f"/api/intelligence/skills/connections/{connection_id}",
            json={"expected_revision": 1, "access_token": rotated_token},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-connection-rotate"},
        )
        self.assertEqual(rotated.status_code, 200, rotated.get_data(as_text=True))
        self.assertEqual(rotated.json["data"]["revision"], 2)
        self.assertEqual(rotated.json["data"]["skills_disabled_for_reinspection"], 1)
        validated = self.request(
            "post", f"/api/intelligence/skills/connections/{connection_id}/validate",
            headers=self.auth_headers(),
        )
        self.assertEqual(validated.status_code, 200, validated.get_data(as_text=True))
        self.assertEqual(validated.json["data"]["validation_status"], "valid")
        with self.assertRaises(SkillLifecycleError) as rotated_skill:
            self.service.skills.resolve_enabled(self.identity, [skill_id], self.service)
        self.assertEqual(rotated_skill.exception.code, "GITHUB_CONNECTION_REVOKED_OR_CHANGED")

        revoked = self.request(
            "post", f"/api/intelligence/skills/connections/{connection_id}/revoke", json={"expected_revision": 1},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-connection-revoke"},
        )
        self.assertEqual(revoked.status_code, 409, revoked.get_data(as_text=True))
        self.assertEqual(revoked.json["error"]["code"], "GITHUB_CONNECTION_CHANGED")
        revoked = self.request(
            "post", f"/api/intelligence/skills/connections/{connection_id}/revoke", json={"expected_revision": 2},
            headers={**self.auth_headers(), "Idempotency-Key": "private-skill-connection-revoke-v2"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        self.assertEqual(revoked.json["data"]["skills_disabled"], 0)
        self.assertIsNone(self.store.row(
            "SELECT credential_ciphertext FROM intelligence_github_connections WHERE connection_id = ?",
            (connection_id,),
        )["credential_ciphertext"])
        with self.assertRaises(SkillLifecycleError) as disabled:
            self.service.skills.resolve_enabled(self.identity, [skill_id], self.service)
        self.assertEqual(disabled.exception.code, "GITHUB_CONNECTION_REVOKED_OR_CHANGED")
        self.assertTrue(all(call_token is None or call_token in accepted_tokens for _path, call_token in authorized_calls))
        event_text = " ".join(row["result_json"] for row in self.store.rows(
            "SELECT result_json FROM intelligence_skill_events WHERE workspace_id = ?",
            (self.identity["workspace"]["id"],),
        ))
        self.assertNotIn(token, event_text)

    def test_mcp_streamable_http_uses_current_protocol_validates_tools_and_rechecks_schema(self) -> None:
        import server.oevira.intelligence_mcp as mcp_module

        calls = []
        tool = {
            "name": "brand.search",
            "title": "Search brand records",
            "description": "Search the approved record scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 100},
                    "tenant": {"type": "string", "x-mcp-header": "Tenant"},
                },
                "required": ["query", "tenant"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"records": {"type": "array", "items": {"type": "string"}}},
                "required": ["records"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }

        def transport(endpoint, headers, body, timeout):
            payload = json.loads(body)
            calls.append((payload, headers))
            self.assertEqual(endpoint, "https://mcp.example.test/api/mcp")
            self.assertLessEqual(timeout, 60)
            self.assertEqual(headers["MCP-Protocol-Version"], MCP_CURRENT_PROTOCOL)
            self.assertEqual(headers["Mcp-Method"], payload["method"])
            if payload["method"] == "server/discover":
                result = {
                    "resultType": "complete",
                    "supportedVersions": [MCP_CURRENT_PROTOCOL],
                    "capabilities": {"tools": {}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "tenant-mcp", "version": "1.4"}},
                    "instructions": "Read-only approved repository search.",
                }
            elif payload["method"] == "tools/list":
                result = {"resultType": "complete", "tools": [tool], "nextCursor": None}
            elif payload["method"] == "tools/call":
                self.assertEqual(headers["Mcp-Name"], "brand.search")
                self.assertEqual(headers["Mcp-Param-Tenant"], "north")
                self.assertEqual(payload["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
                                 MCP_CURRENT_PROTOCOL)
                result = {
                    "resultType": "complete", "isError": False,
                    "content": [{"type": "text", "text": "Two approved brand records matched."}],
                    "structuredContent": {"records": ["Brand A", "Brand B"]},
                }
                response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
                raw = ("data: " + json.dumps(response, ensure_ascii=False) + "\n\n").encode("utf-8")
                return 200, {"content-type": "text/event-stream"}, raw
            else:
                raise AssertionError(payload["method"])
            response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            return 200, {"content-type": "application/json"}, json.dumps(response).encode()

        with patch.object(
            mcp_module, "public_target",
            return_value=("https://mcp.example.test/api/mcp", "mcp.example.test", 443, "203.0.113.20"),
        ):
            client = McpHttpClient("https://mcp.example.test/api/mcp", transport=transport)
        discovered = client.discover()
        self.assertEqual(discovered["protocol_version"], MCP_CURRENT_PROTOCOL)
        self.assertEqual(discovered["server_info"]["name"], "tenant-mcp")
        listed = client.list_tools()
        reviewed = listed["tools"][0]
        self.assertEqual(listed["state"], "COMPLETE")
        self.assertEqual(reviewed["name"], "brand.search")
        result = client.call_tool(
            reviewed, {"query": "品牌", "tenant": "north"}, approved=True, effect="read",
        )
        self.assertEqual(result["trust"], "UNTRUSTED_EXTERNAL_MCP_RESULT")
        self.assertEqual(result["structured_content"]["records"], ["Brand A", "Brand B"])
        self.assertTrue(any(
            payload["method"] == "tools/call" and headers.get("Mcp-Param-Tenant") == "north"
            for payload, headers in calls
        ))

        changed = dict(tool)
        changed["inputSchema"] = dict(tool["inputSchema"])
        changed["inputSchema"]["properties"] = dict(tool["inputSchema"]["properties"])
        changed["inputSchema"]["properties"]["query"] = {"type": "string", "minLength": 2, "maxLength": 100}
        tool.clear()
        tool.update(changed)
        before = len(calls)
        with self.assertRaises(McpClientError) as schema_changed:
            client.call_tool(reviewed, {"query": "品牌", "tenant": "north"}, approved=True, effect="read")
        self.assertEqual(schema_changed.exception.code, "MCP_TOOL_SCHEMA_CHANGED")
        self.assertFalse(any(payload["method"] == "tools/call" for payload, _headers in calls[before:]))

        with self.assertRaises(McpClientError) as no_approval:
            client.call_tool(reviewed, {"query": "品牌", "tenant": "north"}, approved=False, effect="read")
        self.assertEqual(no_approval.exception.code, "MCP_TOOL_PERMISSION_REQUIRED")

    def test_mcp_legacy_http_fallback_is_explicit_and_private_endpoints_are_denied(self) -> None:
        import server.oevira.intelligence_mcp as mcp_module

        calls = []

        def transport(endpoint, headers, body, timeout):
            payload = json.loads(body)
            calls.append((payload, headers))
            if payload["method"] == "server/discover":
                return 404, {"content-type": "text/plain"}, b"Legacy endpoint"
            if payload["method"] == "initialize":
                return 200, {"content-type": "application/json", "mcp-session-id": "session-test"}, json.dumps({
                    "jsonrpc": "2.0", "id": payload["id"], "result": {
                        "protocolVersion": MCP_LEGACY_PROTOCOL, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "legacy-mcp", "version": "1"},
                    },
                }).encode()
            if payload["method"] == "notifications/initialized":
                self.assertNotIn("id", payload)
                return 202, {}, b""
            if payload["method"] == "tools/list":
                self.assertEqual(headers.get("Mcp-Session-Id"), "session-test")
                return 200, {"content-type": "application/json"}, json.dumps({
                    "jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []},
                }).encode()
            raise AssertionError(payload["method"])

        with patch.object(
            mcp_module, "public_target",
            return_value=("https://legacy.example.test/mcp", "legacy.example.test", 443, "203.0.113.21"),
        ):
            client = McpHttpClient("https://legacy.example.test/mcp", transport=transport)
        discovered = client.discover()
        self.assertEqual(discovered["protocol_version"], MCP_LEGACY_PROTOCOL)
        self.assertEqual(client.list_tools()["state"], "COMPLETE")
        self.assertTrue(any(payload["method"] == "initialize" for payload, _headers in calls))
        with patch.object(
            mcp_module, "public_target",
            side_effect=AcquisitionError("PRIVATE_SOURCE_DENIED", "private target denied"),
        ):
            with self.assertRaises(McpClientError) as denied:
                McpHttpClient("https://127.0.0.1/mcp", transport=transport)
        self.assertEqual(denied.exception.code, "MCP_ENDPOINT_NOT_ALLOWED")

    def test_restricted_skill_script_uses_only_granted_product_tools(self) -> None:
        source = """
def run(args):
    records = call("material.search", {"query": args["query"]})
    return {"count": len(records["items"]), "topic": args["query"]}
"""
        metadata = inspect_script(source)
        self.assertEqual(metadata["tool_names"], ["material.search"])
        calls = []

        def product_tool(name, arguments):
            calls.append((name, arguments))
            return {"items": [{"title": "品牌材料"}]}

        result = run_restricted_script(
            source, {"query": "品牌"}, product_tool, allowed_tools={"material.search"},
        )
        self.assertEqual(result["value"], {"count": 1, "topic": "品牌"})
        self.assertEqual(calls, [("material.search", {"query": "品牌"})])
        self.assertEqual(result["isolation"], {
            "os": False, "filesystem": False, "network": False, "processes": False,
        })
        for denied_source in (
            "import os\ndef run(args):\n    return call('material.search', {})\n",
            "def run(args):\n    return args.__class__\n",
        ):
            with self.assertRaises(RestrictedScriptError):
                inspect_script(denied_source)
        with self.assertRaises(RestrictedScriptError) as tool_denied:
            run_restricted_script(
                "def run(args):\n    return call('artifact.delete', {})\n",
                {}, product_tool, allowed_tools={"material.search"},
            )
        self.assertEqual(tool_denied.exception.code, "RESTRICTED_SCRIPT_TOOL_NOT_GRANTED")
        with self.assertRaises(RestrictedScriptError) as loop_limited:
            run_restricted_script(
                "def run(args):\n    total = 0\n    for item in range(65):\n        total = total + item\n    return total\n",
                {}, product_tool, allowed_tools=set(),
            )
        self.assertEqual(loop_limited.exception.code, "RESTRICTED_SCRIPT_LOOP_BUDGET_EXCEEDED")

    def test_mcp_connection_is_user_scoped_encrypted_rotatable_and_revocable(self) -> None:
        import server.oevira.intelligence_mcp as mcp_module

        class TestOnlyProtector:
            def protect(self, data: bytes, entropy: bytes) -> bytes:
                return bytes(value ^ entropy[index % len(entropy)] for index, value in enumerate(data))

            def unprotect(self, data: bytes, entropy: bytes) -> bytes:
                return self.protect(data, entropy)

        token = "mcp_test_bearer_token_one"
        rotated_token = "mcp_test_bearer_token_two"
        valid_tokens = {token}
        calls = []
        tool = {
            "name": "brand.search", "description": "Search approved brand records.",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }

        def transport(endpoint, headers, body, timeout):
            payload = json.loads(body)
            calls.append((payload, dict(headers)))
            self.assertEqual(endpoint, "https://mcp.example.test/mcp")
            if headers.get("Authorization") != f"Bearer {token}" and headers.get("Authorization") != f"Bearer {rotated_token}":
                return 401, {"content-type": "application/json"}, json.dumps({
                    "jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32001, "message": "Unauthorized"},
                }).encode()
            if payload["method"] == "server/discover":
                result = {
                    "resultType": "complete", "supportedVersions": [MCP_CURRENT_PROTOCOL],
                    "capabilities": {"tools": {}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "brand-service", "version": "2"}},
                }
            elif payload["method"] == "tools/list":
                result = {"resultType": "complete", "tools": [tool]}
            else:
                raise AssertionError(payload["method"])
            return 200, {"content-type": "application/json"}, json.dumps({
                "jsonrpc": "2.0", "id": payload["id"], "result": result,
            }).encode()

        def client_factory(endpoint, *, access_token=None):
            return McpHttpClient(endpoint, access_token=access_token, transport=transport)

        self.service.skills.protector = TestOnlyProtector()
        self.service.skills.mcp_client_factory = client_factory
        with patch.object(
            mcp_module, "public_target",
            return_value=("https://mcp.example.test/mcp", "mcp.example.test", 443, "203.0.113.22"),
        ):
            created = self.service.skills.create_mcp_connection(
                self.identity, {"name": "品牌检索", "endpoint": "https://mcp.example.test/mcp",
                                "access_token": token, "authorized_use_confirmed": True},
                operation_key="mcp-connection-create",
            )
            connection_id = created["connection_id"]
            self.assertEqual(created["protocol_version"], MCP_CURRENT_PROTOCOL)
            self.assertEqual(created["tool_count"], 1)
            row = self.store.row(
                "SELECT * FROM intelligence_mcp_connections WHERE connection_id = ?", (connection_id,),
            )
            self.assertNotIn(token.encode(), bytes(row["credential_ciphertext"]))
            self.assertEqual(row["credential_protection"], "windows_dpapi_current_user_v1")
            self.assertEqual(self.service.skills.list_mcp_connections(self._create_other_identity()), [])
            inspected = self.service.skills.inspect_mcp_connection(self.identity, connection_id)
            self.assertEqual(inspected["tools"][0]["name"], "brand.search")
            self.assertNotIn(token, json.dumps(inspected))

            valid_tokens.add(rotated_token)
            rotated = self.service.skills.update_mcp_connection(
                self.identity, connection_id,
                {"expected_revision": 1, "endpoint": "https://mcp.example.test/mcp",
                 "access_token": rotated_token, "authorized_use_confirmed": True},
                operation_key="mcp-connection-rotate",
            )
            self.assertEqual(rotated["revision"], 2)
            self.assertEqual(rotated["credential_present"], True)
            validated = self.service.skills.validate_mcp_connection(self.identity, connection_id)
            self.assertEqual(validated["validation_status"], "valid")
            with self.assertRaises(SkillLifecycleError) as stale:
                self.service.skills.revoke_mcp_connection(
                    self.identity, connection_id, 1, operation_key="mcp-revoke-stale",
                )
            self.assertEqual(stale.exception.code, "MCP_CONNECTION_CHANGED")
            revoked = self.service.skills.revoke_mcp_connection(
                self.identity, connection_id, 2, operation_key="mcp-connection-revoke",
            )
            self.assertEqual(revoked["status"], "revoked")
            self.assertIsNone(self.store.row(
                "SELECT credential_ciphertext FROM intelligence_mcp_connections WHERE connection_id = ?",
                (connection_id,),
            )["credential_ciphertext"])
        self.assertTrue(all(
            headers.get("Authorization") in {f"Bearer {token}", f"Bearer {rotated_token}"}
            for _payload, headers in calls
        ))

    def test_mcp_skill_invokes_read_tool_saves_material_and_builds_cited_artifact(self) -> None:
        import server.oevira.intelligence_mcp as mcp_module

        class TestOnlyProtector:
            def protect(self, data: bytes, entropy: bytes) -> bytes:
                return bytes(value ^ entropy[index % len(entropy)] for index, value in enumerate(data))

            def unprotect(self, data: bytes, entropy: bytes) -> bytes:
                return self.protect(data, entropy)

        token = "mcp_test_integration_token"
        tool = {
            "name": "brand.search",
            "title": "Search brand records",
            "description": "Search user-authorized brand records by a short query.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 100}},
                "required": ["query"], "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"brands": {"type": "array", "items": {"type": "string"}}},
                "required": ["brands"], "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }
        transport_calls = []

        def transport(endpoint, headers, body, timeout):
            payload = json.loads(body)
            transport_calls.append((payload, dict(headers)))
            self.assertEqual(endpoint, "https://mcp.example.test/mcp")
            self.assertEqual(headers.get("Authorization"), f"Bearer {token}")
            if payload["method"] == "server/discover":
                result = {
                    "resultType": "complete", "supportedVersions": [MCP_CURRENT_PROTOCOL],
                    "capabilities": {"tools": {}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "brand-records", "version": "3"}},
                }
            elif payload["method"] == "tools/list":
                result = {"resultType": "complete", "tools": [tool], "nextCursor": None}
            elif payload["method"] == "tools/call":
                self.assertEqual(headers.get("Mcp-Name"), "brand.search")
                self.assertEqual(payload["params"]["arguments"]["query"], "品牌")
                result = {
                    "resultType": "complete", "isError": False,
                    "content": [{"type": "text", "text": "品牌A：武汉门店顾客反馈产品耐用，服务响应及时。"}],
                    "structuredContent": {"brands": ["品牌A"]},
                }
            else:
                raise AssertionError(payload["method"])
            return 200, {"content-type": "application/json"}, json.dumps({
                "jsonrpc": "2.0", "id": payload["id"], "result": result,
            }, ensure_ascii=False).encode("utf-8")

        self.service.skills.protector = TestOnlyProtector()
        self.service.skills.mcp_client_factory = lambda endpoint, *, access_token=None: McpHttpClient(
            endpoint, access_token=access_token, transport=transport,
        )
        with patch.object(
            mcp_module, "public_target",
            return_value=("https://mcp.example.test/mcp", "mcp.example.test", 443, "203.0.113.23"),
        ):
            created = self.request(
                "post", "/api/intelligence/skills/mcp/connections",
                json={"name": "品牌资料服务", "endpoint": "https://mcp.example.test/mcp",
                      "access_token": token, "authorized_use_confirmed": True},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-connect"},
            )
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            self.assertNotIn(token, created.get_data(as_text=True))
            connection_id = created.json["data"]["connection_id"]
            inspected = self.request(
                "post", "/api/intelligence/skills/mcp/inspect",
                json={"connection_id": connection_id, "tool_name": "brand.search"},
                headers=self.auth_headers(),
            )
            self.assertEqual(inspected.status_code, 201, inspected.get_data(as_text=True))
            candidate = inspected.json["data"]
            self.assertEqual(candidate["source_type"], "mcp_http")
            self.assertEqual(candidate["declared_tools"], ["mcp.tool.invoke", "artifact.create_draft"])
            self.assertNotIn(token, inspected.get_data(as_text=True))
            installed = self.request(
                "post", "/api/intelligence/skills/install",
                json={"candidate_id": candidate["candidate_id"], "candidate_sha256": candidate["content_sha256"],
                      "rights_review_sha256": candidate["license"]["approval_sha256"],
                      "rights_reviewed": True, "tool_names": ["mcp.tool.invoke", "artifact.create_draft"]},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-install"},
            )
            self.assertEqual(installed.status_code, 201, installed.get_data(as_text=True))
            skill_id = installed.json["data"]["skill_id"]
            tested = self.request(
                "post", f"/api/intelligence/skills/{skill_id}/self-test",
                json={"version": 1, "content_sha256": candidate["content_sha256"]},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-self-test"},
            )
            self.assertEqual(tested.status_code, 200, tested.get_data(as_text=True))
            enabled = self.request(
                "post", f"/api/intelligence/skills/{skill_id}/enable",
                json={"version": 1, "content_sha256": candidate["content_sha256"],
                      "tool_schema_sha256": installed.json["data"]["tool_schema_sha256"]},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-enable"},
            )
            self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))

            work = self.create_work("MCP 来源回到真实工作")
            selection = ExplicitModelSelection(
                route="owned", selection_id="mcp-owned-model", revision="mcp-model-revision",
                model_id="test-owned-small-model", recipient_id="local-only", selected_by_user=True,
            )
            model_calls = []
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()

            def resolve_selection(identity, selection_id):
                return selection if selection_id == selection.selection_id else None

            def authorize_source(identity, selected, source_refs, *, purpose):
                return ModelInputAuthorization(
                    allowed=True, workspace_id=identity["workspace"]["id"], user_id=identity["user"]["id"],
                    selection_id=selected.selection_id, selection_revision=selected.revision,
                    route=selected.route, model_id=selected.model_id,
                    source_ref_sha256=_canonical_digest(list(source_refs)), purpose=purpose,
                    recipient=selected.recipient_id, authorization_id="mcp-tool-source-authorization",
                    expires_at=expiry,
                )

            def run_model(selected, payload, *, is_cancelled=None):
                model_calls.append(payload)
                tool_results = payload.get("tool_results", [])
                if not tool_results:
                    return {"status": "completed", "tool_calls": [{
                        "name": "mcp.tool.invoke",
                        "arguments": {"skill_id": skill_id, "tool_name": "brand.search",
                                      "arguments": {"query": "品牌"}},
                    }]}
                mcp_result = next((item["result"] for item in tool_results
                                   if item.get("tool") == "mcp.tool.invoke"), None)
                if mcp_result is not None and not any(item.get("tool") == "artifact.create_draft" for item in tool_results):
                    self.assertEqual(mcp_result["source_trust"], "UNTRUSTED_EXTERNAL_MCP_RESULT")
                    self.assertGreaterEqual(len(mcp_result["citations"]), 1)
                    return {"status": "completed", "tool_calls": [{
                        "name": "artifact.create_draft",
                        "arguments": {"title": "品牌检索结果", "body": "品牌A在武汉顾客反馈产品耐用、服务响应及时。",
                                      "citations": mcp_result["citations"], "work_id": work["id"]},
                    }]}
                return {"status": "completed", "text": "已保存带来源的品牌检索结果。"}

            self.service.model_selection_resolver = resolve_selection
            self.service.source_use_authorizer = authorize_source
            self.service.owned_inference_runner = run_model
            queued = self.request(
                "post", "/api/intelligence/turns",
                json={"message": "查找品牌反馈并保存摘要", "work_id": work["id"],
                      "query": "品牌", "selection_id": selection.selection_id, "skill_ids": [skill_id]},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-turn"},
            )
            self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
            from server.oevira.worker import _claim_job
            claim = _claim_job(self.store)
            self.assertEqual(claim["id"], queued.json["data"]["job"]["id"])
            with self.app.app_context():
                process_intelligence_job(self.app, claim)
            conversation_id = queued.json["data"]["conversation"]["conversation_id"]
            reopened = self.request(
                "get", f"/api/intelligence/conversations/{conversation_id}",
                headers=self.auth_headers(),
            )
            self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
            turn = reopened.json["data"]["turns"][0]
            self.assertEqual(turn["status"], "completed", turn)
            self.assertIn("已保存", turn["assistant_message"])
            self.assertEqual([item["tool"] for item in turn["operations"]],
                             ["mcp.tool.invoke", "artifact.create_draft"])
            mcp_result = next(
                item["result"] for item in model_calls[1]["tool_results"]
                if item.get("tool") == "mcp.tool.invoke"
            )
            mcp_material = mcp_result["material"]
            reopened_material = self.service.read_material(self.identity, mcp_material["id"])
            self.assertIn("UNTRUSTED EXTERNAL MCP RESULT", reopened_material["text"])
            artifact_row = self.store.row(
                "SELECT id FROM artifacts WHERE workspace_id = ? AND work_id = ? AND title = ?",
                (self.identity["workspace"]["id"], work["id"], "品牌检索结果"),
            )
            self.assertIsNotNone(artifact_row)
            reopened_artifact = self.service.read_artifact(self.identity, artifact_row["id"])
            self.assertEqual(reopened_artifact["version"], 1)
            self.assertEqual(reopened_artifact["citation_state"], "current")
            self.assertTrue(any(item["material_id"] == mcp_material["id"]
                                for item in reopened_artifact["citations"]))
            self.assertIn(mcp_material["id"], {
                ref.get("material_id") for ref in json.loads(
                    self.store.row("SELECT source_refs_json FROM intelligence_turns WHERE turn_id = ?",
                                   (turn["turn_id"],))["source_refs_json"]
                )
            })
            self.assertTrue(any(item.get("tool") == "mcp.tool.invoke" for item in model_calls[0]["tools"]))

            revoked = self.request(
                "post", f"/api/intelligence/skills/mcp/connections/{connection_id}/revoke",
                json={"expected_revision": 1},
                headers={**self.auth_headers(), "Idempotency-Key": "mcp-skill-connection-revoke"},
            )
            self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
            self.assertEqual(revoked.json["data"]["skills_disabled"], 1)
            self.assertIsNone(self.store.row(
                "SELECT credential_ciphertext FROM intelligence_mcp_connections WHERE connection_id = ?",
                (connection_id,),
            )["credential_ciphertext"])
            with self.assertRaises(SkillLifecycleError) as revoked_skill:
                self.service.skills.resolve_enabled(self.identity, [skill_id], self.service)
            self.assertEqual(revoked_skill.exception.code, "MCP_CONNECTION_REVOKED_OR_CHANGED")

    def _create_other_identity(self) -> dict:
        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
        session_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO users(id, name, created_at) VALUES(?, ?, ?)", (user_id, "另一位用户", now))
            connection.execute("INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?)", (workspace_id, "另一个空间", now))
            connection.execute(
                "INSERT INTO memberships(workspace_id, user_id, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
                (workspace_id, user_id, now),
            )
            connection.execute(
                "INSERT INTO sessions(id, token_hash, user_id, workspace_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
                (session_id, _digest(token), user_id, workspace_id, now, expiry),
            )
        return {
            "user": {"id": user_id, "name": "另一位用户"},
            "workspace": {"id": workspace_id, "name": "另一个空间"},
            "session_id": session_id, "role": "owner",
        }


class UserConfiguredGatewayTests(unittest.TestCase):
    def test_chat_completion_api_uses_the_selected_endpoint_token_field(self) -> None:
        selection = ExplicitModelSelection(
            route="user_configured", selection_id="openai-setting", revision="rev-2",
            model_id="selected-chat-model", recipient_id="https://api.openai.com/v1/chat/completions",
            selected_by_user=True,
        )
        connection = UserConfiguredModelConnection(
            selection_id=selection.selection_id, revision=selection.revision,
            model_id=selection.model_id, recipient_id=selection.recipient_id,
            base_url="https://api.openai.com/v1", api_key="model-test-key",
            protocol="openai_chat_completions", token_field="max_completion_tokens",
        )
        captured = {}

        class FakeResponse:
            def read(self, _limit):
                return json.dumps({"choices": [{"message": {"content": "READY"}}]}).encode("utf-8")

        def open_connection(destination, hostname, body, api_key, timeout):
            captured.update(destination=destination, request=json.loads(body), api_key=api_key)
            return nullcontext(FakeResponse())

        gateway = UserConfiguredChatGateway(lambda _selection: connection)
        with patch("server.oevira.intelligence_provider._open_pinned_https", side_effect=open_connection):
            result = gateway(selection, {"goal": "connection test", "tools": []})
        self.assertEqual(result["status"], "completed")
        self.assertIn("max_completion_tokens", captured["request"])
        self.assertNotIn("max_tokens", captured["request"])
        self.assertEqual(captured["destination"], "https://api.openai.com/v1/chat/completions")

    def test_codex_request_and_response_guards_allow_only_product_tools(self) -> None:
        allowed_name = "oevira_1234567890abcdefghijklmn"
        request = {
            "tools": [
                {"type": "function", "name": "shell_command", "parameters": {}},
                {"type": "function", "name": "apply_patch", "parameters": {}},
                {"type": "function", "name": allowed_name, "parameters": {}},
                {"type": "computer", "name": "computer"},
            ],
            "tool_choice": "auto",
        }
        filtered = _only_product_tools(request, {allowed_name})
        self.assertEqual([item["name"] for item in filtered["tools"]], [allowed_name])
        for item in (
            {"type": "function_call", "name": "shell_command", "arguments": "{}"},
            {"type": "function_call", "name": "apply_patch", "arguments": "{}"},
            {"type": "computer_call", "action": {}},
        ):
            with self.subTest(item_type=item["type"], name=item.get("name")), self.assertRaises(IntelligenceError):
                _validate_responses_event({
                    "type": "response.output_item.added", "output_index": 0, "item": item,
                }, {allowed_name})

    def test_responses_url_cannot_be_redirected_or_rebound_to_private_dns(self) -> None:
        destination, hostname, port, path = _connection_destination("https://models.example.com/v1/")
        self.assertEqual(destination, "https://models.example.com/v1/chat/completions")
        self.assertEqual((hostname, port, path), ("models.example.com", 443, "/v1/chat/completions"))
        for value in (
            "http://models.example.com/v1", "https://127.0.0.1/v1",
            "https://models.example.com/v1?token=secret", "https://user:pass@models.example.com/v1",
            "https://models.example.com/../admin",
        ):
            with self.subTest(value=value), self.assertRaises(IntelligenceError):
                _connection_destination(value)

        with patch(
            "server.oevira.intelligence_provider.public_target",
            side_effect=AcquisitionError("PRIVATE_SOURCE_DENIED", "blocked"),
        ), patch("server.oevira.intelligence_provider._PinnedHTTPSConnection") as connector:
            with self.assertRaises(IntelligenceError) as raised:
                with _open_pinned_https(
                    destination, hostname, b"{}", "top-secret-key", timeout=5,
                ):
                    pass
        self.assertEqual(raised.exception.code, "USER_MODEL_DESTINATION_NOT_ALLOWED")
        connector.assert_not_called()

    def test_selected_gateway_sends_only_explicit_request_and_parses_fixed_tool_calls(self) -> None:
        selection = ExplicitModelSelection(
            route="user_configured", selection_id="user-setting-1", revision="revision-7",
            model_id="provider-model", recipient_id="provider-account-3", selected_by_user=True,
        )
        connection = UserConfiguredModelConnection(
            selection_id=selection.selection_id, revision=selection.revision,
            model_id=selection.model_id, recipient_id=selection.recipient_id,
            base_url="https://models.example.com/v1",
            api_key="user-secret-key", protocol="openai_chat_completions",
        )
        captured = {}

        class FakeResponse:
            def read(self, _size):
                return json.dumps({"choices": [{"message": {
                    "tool_calls": [{
                        "type": "function", "function": {
                            "name": captured["function_name"],
                            "arguments": json.dumps({"query": "预算", "limit": 4}),
                        },
                    }],
                }}]}).encode("utf-8")

        def open_connection(destination, hostname, body, api_key, timeout):
            captured.update({
                "destination": destination, "hostname": hostname,
                "body": body.decode("utf-8"), "api_key": api_key, "timeout": timeout,
            })
            request = json.loads(captured["body"])
            captured["function_name"] = request["tools"][0]["function"]["name"]
            return nullcontext(FakeResponse())

        gateway = UserConfiguredChatGateway(lambda candidate: connection)
        request_approvals = []
        def approve_exact_request(request_body, request_sha256):
            encoded = json.dumps(request_body, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), request_sha256)
            request_approvals.append((request_body, request_sha256))
            return True
        with patch("server.oevira.intelligence_provider._open_pinned_https", side_effect=open_connection):
            result = gateway(selection, {
                "goal": "核验预算", "work": {"id": "work-1"},
                "tools": [{
                    "tool": "material.search", "owner": "Material", "available": True,
                    "required_fields": ["query"], "optional_fields": ["limit"], "effect": "read",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"], "additionalProperties": False},
                }],
            }, request_authorizer=approve_exact_request)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_calls"][0], {
            "name": "material.search", "arguments": {"query": "预算", "limit": 4},
        })
        self.assertEqual(captured["api_key"], "user-secret-key")
        self.assertNotIn("user-secret-key", captured["body"])
        self.assertEqual(captured["destination"], "https://models.example.com/v1/chat/completions")
        self.assertEqual(len(request_approvals), 1)
        self.assertEqual(request_approvals[0][1], hashlib.sha256(captured["body"].encode("utf-8")).hexdigest())
        self.assertFalse(result["fallback_allowed"])


if __name__ == "__main__":
    unittest.main()
