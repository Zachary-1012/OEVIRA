from __future__ import annotations

import io
import hashlib
import json
from datetime import datetime, timedelta, timezone
import subprocess
import sqlite3
import tempfile
import unittest
import uuid
import wave
import zipfile
from pathlib import Path

from server.oevira import create_app
from server.oevira.auth import issue_local_capability, utc_now
from server.oevira.media import _ffmpeg_path
from server.oevira.store import Store
from server.oevira.worker import run_once


BASE_URL = "http://127.0.0.1:8317"


class WorkspaceDataApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-workspace-data-")
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)
        self.capability_file = self.root / "launch.capability"
        issue_local_capability(self.store, self.capability_file)
        self.app = create_app(self.root, expected_port=8317)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        response = self.request(
            "post",
            "/api/local-session",
            json={"capability": self.capability_file.read_text(encoding="ascii"), "display_name": "Workspace tester"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def create_user_work_content(self):
        response = self.request(
            "post",
            "/api/works",
            json={"title": "Market research", "goal": "Recover saved work", "brief": {}},
            headers={**self.headers, "Idempotency-Key": "restore-work"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        work = response.json["data"]

        response = self.request(
            "post",
            "/api/materials",
            json={
                "title": "Source note",
                "filename": "source.txt",
                "text": "A preserved source sentence.",
                "work_id": work["id"],
            },
            headers={**self.headers, "Idempotency-Key": "restore-material"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        material = response.json["data"]

        response = self.request(
            "post",
            "/api/artifacts",
            json={
                "title": "Saved result",
                "body": "The saved result body.",
                "citations": [{
                    "material_id": material["id"],
                    "segment_id": "seg-000001",
                    "quote": "A preserved source sentence.",
                }],
                "work_id": work["id"],
            },
            headers={**self.headers, "Idempotency-Key": "restore-artifact"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        artifact = response.json["data"]
        return work, material, artifact

    def create_media_project(self):
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            self.skipTest("The isolated Media round-trip requires the bundled FFmpeg tool")
        media_path = self.root / "source-tone.mp3"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=16000", "-t", "1", "-ac", "1",
                "-b:a", "64k", str(media_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with media_path.open("rb") as source:
            response = self.request(
                "post",
                "/api/materials",
                data={"file": (source, "source-tone.mp3"), "title": "Tone source"},
                content_type="multipart/form-data",
                headers={**self.headers, "Idempotency-Key": "restore-audio-material"},
            )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        material = response.json["data"]

        response = self.request(
            "post",
            "/api/media/projects",
            json={"title": "Podcast edit", "kind": "podcast"},
            headers={**self.headers, "Idempotency-Key": "restore-media-project"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        project = response.json["data"]
        response = self.request(
            "post",
            f"/api/media/projects/{project['id']}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={**self.headers, "Idempotency-Key": "restore-media-source"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        project = response.json["data"]
        return project, material

    def add_private_model_connection(self) -> None:
        identity = self.request("get", "/api/session", headers=self.headers).json["data"]
        workspace_id = identity["workspace"]["id"]
        user_id = identity["user"]["id"]
        now = utc_now()
        github_connection_id = "private-github-backup-test"
        mcp_connection_id = "private-mcp-backup-test"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO intelligence_github_connections(connection_id,workspace_id,owner_user_id,name,status,revision,"
                "credential_ciphertext,credential_protection,provider_login,verified_at,validation_status,created_at,updated_at) "
                "VALUES(?,?,?,'Private GitHub','active',1,?,'test-local-protection','private-login',?,'valid',?,?)",
                (github_connection_id, workspace_id, user_id, b"FAKE-GITHUB-CREDENTIAL", now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_mcp_connections(connection_id,workspace_id,owner_user_id,name,endpoint,status,revision,"
                "credential_ciphertext,credential_protection,protocol_version,server_info_json,capabilities_json,"
                "tool_list_sha256,tool_count,authorized_use_confirmed,validation_status,verified_at,created_at,updated_at) "
                "VALUES(?,?,?,'Private MCP','https://mcp.example.test/','active',1,?,'test-local-protection',"
                "'2025-11-25','{}','{}',?,1,1,'valid',?,?,?)",
                (mcp_connection_id, workspace_id, user_id, b"FAKE-MCP-CREDENTIAL", "a" * 64, now, now, now),
            )
            connection.execute(
                "INSERT INTO intelligence_model_connections(connection_id,workspace_id,owner_user_id,name,base_url,"
                "recipient_id,protocol,token_field,model_id,credential_ciphertext,credential_protection,"
                "allow_scoped_data,is_selected,status,revision,created_at,updated_at,created_by) "
                "VALUES(?,?,?,?,?,'example.test','openai_responses','max_tokens','private-test-model',?,"
                "'test-local-protection',1,1,'configured','revision-1',?,?,?)",
                (
                    str(uuid.uuid4()), workspace_id, user_id, "Private test connection",
                    "https://example.test/v1?api_key=NOT_FOR_BACKUP", b"FAKE-CREDENTIAL-MATERIAL", now, now, user_id,
                ),
            )

    def add_transcription_candidate(self, project: dict, material: dict) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(id,workspace_id,user_id,work_id,material_id,kind,status,payload_json,request_hash,"
                "idempotency_key,progress_completed,progress_total,progress_label,attempts,lease_generation,created_at,updated_at) "
                "VALUES(?,?,?,NULL,?,'media_transcribe','completed','{}',?,'test-transcription',1,1,'Transcript saved',1,1,?,?)",
                (
                    job_id, project["workspace_id"] if "workspace_id" in project else self.store.row(
                        "SELECT workspace_id FROM media_projects WHERE id=?", (project["id"],)
                    )["workspace_id"],
                    self.request("get", "/api/session", headers=self.headers).json["data"]["user"]["id"],
                    material["id"], "a" * 64, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO media_transcriptions(job_id,workspace_id,project_id,project_version,source_asset_id,"
                "source_material_id,source_sha256,audio_stream_index,audio_channel_index,source_sample_rate,task,"
                "normalized_sample_rate,model_version,model_sha256,result_metadata_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'transcribe',16000,'test-model',?,'{}',?,?)",
                (
                    job_id,
                    project["workspace_id"] if "workspace_id" in project else self.store.row(
                        "SELECT workspace_id FROM media_projects WHERE id=?", (project["id"],)
                    )["workspace_id"],
                    project["id"], project["current_version"], project["sources"][0]["asset_id"],
                    material["id"], material["sha256"], 0, 0, 16000, "b" * 64, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO media_transcription_chunks(job_id,chunk_index,normalized_start_sample,normalized_end_sample,"
                "source_start_sample,source_end_sample,language,text,segments_json,words_json,created_at) "
                "VALUES(?,0,0,16000,0,16000,'en','A transcript candidate.','[]','[]',?)",
                (job_id, now),
            )
        return job_id

    def run_worker(self, *kinds: str) -> None:
        self.assertTrue(run_once(self.app, allowed_kinds=kinds))

    def test_backup_download_restore_open_and_resume_user_content(self) -> None:
        from server.oevira.media_interchange import install_media_interchange_schema
        from server.oevira.acquisition_queries import install_query_schema
        from server.oevira.auth import authenticate

        install_media_interchange_schema(self.store)
        install_query_schema(self.store)
        work, text_material, artifact = self.create_user_work_content()
        session_identity = self.request("get", "/api/session", headers=self.headers).json["data"]
        workspace_id = session_identity["workspace"]["id"]
        user_id = session_identity["user"]["id"]
        auth_identity = authenticate(self.store, self.token)
        login_session_id = auth_identity["session_id"]
        membership_created_at = self.store.row(
            "SELECT created_at FROM memberships WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        )["created_at"]
        now = utc_now()
        browser_session_id = str(uuid.uuid4())
        browser_capture_id = str(uuid.uuid4())
        own_query_id = str(uuid.uuid4())
        own_query_paths = {
            "browser": str(uuid.uuid4()),
            "expired_browser": str(uuid.uuid4()),
            "url": str(uuid.uuid4()),
        }
        future_preview_expiry = (
            (datetime.now(timezone.utc) + timedelta(minutes=10))
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        other_user_id = str(uuid.uuid4())
        other_query_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,name,created_at) VALUES(?,'Other query owner',?)",
                (other_user_id, now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?,'editor',1,?)",
                (workspace_id, other_user_id, now),
            )
            connection.execute(
                "INSERT INTO browser_sessions(id,workspace_id,owner_user_id,membership_created_at,"
                "authorized_by_session_id,work_id,engine,status,version,control_mode,control_generation,"
                "current_url,current_title,state_ciphertext,state_protection,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'edge','open',1,'user',1,?,?,?,'test-dpapi',?,?)",
                (browser_session_id, workspace_id, user_id, membership_created_at, login_session_id,
                 work["id"], "https://browser.example.test/page?access_token=BROWSER_URL_SECRET",
                 "Private page title", b"FAKE-BROWSER-COOKIE-STATE", now, now),
            )
            connection.execute(
                "INSERT INTO browser_session_operations(id,session_id,workspace_id,owner_user_id,operation,idempotency_key,"
                "request_hash,status,result_json,preview_json,preview_expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?, 'context_preview',?,'snapshot','completed',?,?,?,?,?)",
                (browser_capture_id, browser_session_id, workspace_id, user_id, browser_capture_id,
                 json.dumps({"capture_ref": {"capture_id": browser_capture_id, "session_id": browser_session_id,
                                             "work_id": work["id"], "expires_at": future_preview_expiry}}),
                 json.dumps({"text": "FROZEN_BROWSER_CONTEXT_MUST_NOT_BE_BACKED_UP"}), future_preview_expiry, now, now),
            )
            for owner_id, query_id, idempotency, query in (
                (user_id, own_query_id, "workspace-owned-query-plan", "orchid"),
                (other_user_id, other_query_id, "other-user-query-plan", "other private query"),
            ):
                connection.execute(
                    "INSERT INTO acquisition_query_plans(query_id,workspace_id,owner_user_id,work_id,query_revision,"
                    "idempotency_key,request_hash,request_json,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?, ?,?)",
                    (query_id, workspace_id, owner_id, work["id"] if owner_id == user_id else None,
                     idempotency, hashlib.sha256(query.encode()).hexdigest(),
                     json.dumps({"query": query, "work_id": work["id"] if owner_id == user_id else None,
                                 "public_urls": ["https://search.example.test/?api_key=QUERY_PLAN_URL_SECRET"],
                                 "browser_targets": [{"session_id": browser_session_id,
                                                      "page_id": "page-query-plan", "page_revision": 4}]
                                 if owner_id == user_id else []}), now, now),
                )
            for kind, path_id, expiry, quote in (
                ("browser_page", own_query_paths["browser"], future_preview_expiry,
                 "UNADMITTED_BROWSER_PREVIEW_BODY"),
                ("browser_page", own_query_paths["expired_browser"], "2020-01-01T00:00:00Z",
                 "EXPIRED_BROWSER_PREVIEW_BODY"),
            ):
                connection.execute(
                    "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                    "path_kind,data_surface,source_ref_json,request_json,material_refs_json,result_json,status,"
                    "coverage_json,checkpoint_ref_json,preview_expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'browser_page','selected_browser_page',?,?, '[]',?,'complete',?,?, ?,?,?)",
                    (path_id, own_query_id, workspace_id,
                     0 if path_id == own_query_paths["browser"] else 1,
                     None if path_id == own_query_paths["browser"] else own_query_paths["browser"],
                     json.dumps({"kind": "browser_page", "session_id": browser_session_id,
                                 "page_id": "page-query-plan", "page_revision": 4}),
                     json.dumps({"query": "orchid", "query_work_id": work["id"],
                                 "target": {"session_id": browser_session_id, "page_id": "page-query-plan",
                                            "page_revision": 4}}),
                     json.dumps({"status": "complete", "results": [{"quote": quote}],
                                 "source_ref": {"kind": "browser_page", "session_id": browser_session_id,
                                                "page_id": "page-query-plan", "page_revision": 4,
                                                "snapshot_sha256": "a" * 64, "work_id": work["id"],
                                                "url": "https://browser.example.test/page?access_token=BROWSER_RESULT_SECRET",
                                                "title": "Private page title", "captured_at": now}}),
                     json.dumps({"complete_scope_scanned": True}),
                     json.dumps({"browser_session_id": browser_session_id, "page_id": "page-query-plan",
                                 "page_revision": 4, "snapshot_sha256": "a" * 64}),
                     expiry, now, now),
                )
            child_job_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,"
                "idempotency_key,created_at,updated_at) VALUES(?,?,?,?,'acquire_url','completed',?,?,?,?,?)",
                (child_job_id, workspace_id, user_id, work["id"],
                 json.dumps({"url": "https://source.example.test/?access_token=QUERY_JOB_SECRET"}),
                 "f" * 64, "query-child-job", now, now),
            )
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,child_job_id,result_json,"
                "status,coverage_json,checkpoint_ref_json,created_at,updated_at) "
                "VALUES(?,?,?,2,NULL,'public_url_acquisition','public_http','{}',?,'[]',?,NULL,'completed',?,?,?,?)",
                (own_query_paths["url"], own_query_id, workspace_id,
                 json.dumps({"url": "https://source.example.test/?access_token=QUERY_PATH_SECRET",
                             "query_work_id": work["id"]}), child_job_id,
                 json.dumps({"status": "completed"}),
                 json.dumps({"job_id": child_job_id, "kind": "acquire_url"}), now, now),
            )
            connection.execute(
                "INSERT INTO acquisition_query_paths(path_id,query_id,workspace_id,path_order,parent_path_id,"
                "path_kind,data_surface,source_ref_json,request_json,material_refs_json,result_json,status,coverage_json,created_at,updated_at) "
                "VALUES(?,?,?,0,NULL,'material_search','local_materials','{}','{}','[]',?,'complete','{}',?,?)",
                (str(uuid.uuid4()), other_query_id, workspace_id,
                 json.dumps({"results": [{"quote": "OTHER_MEMBER_PRIVATE_QUERY_RESULT"}]}), now, now),
            )
        delivery_batch_id = str(uuid.uuid4())
        delivery_job_id = str(uuid.uuid4())
        delivery_member_id = "delivery-member-preserved"
        delivery_bytes = b"Completed delivery member without a completed parent ZIP."
        delivery_relative_path = f"objects/{workspace_id}/exports/delivery-member.docx"
        delivery_file = self.root / Path(delivery_relative_path)
        delivery_file.parent.mkdir(parents=True, exist_ok=True)
        delivery_file.write_bytes(delivery_bytes)
        delivery_sha = hashlib.sha256(delivery_bytes).hexdigest()
        revision_response = self.request(
            "post", f"/api/works/{work['id']}/revision-batches",
            json={"title": "Saved revision history", "members": [{
                "member_id": "revise-result",
                "target": {"kind": "artifact", "id": artifact["id"], "base_version": 1},
                "command": {"title": "Saved result", "body": "The saved result body revised.", "citations": [{
                    "material_id": text_material["id"], "segment_id": "seg-000001",
                    "quote": "A preserved source sentence.",
                }]},
            }]},
            headers={**self.headers, "Idempotency-Key": "workspace-restore-revision-batch"},
        )
        self.assertEqual(revision_response.status_code, 202, revision_response.get_data(as_text=True))
        revision_batch_id = revision_response.json["data"]["id"]
        self.run_worker("revision_batch")
        revision_batch = self.request("get", f"/api/revision-batches/{revision_batch_id}", headers=self.headers)
        self.assertEqual(revision_batch.status_code, 200, revision_batch.get_data(as_text=True))
        self.assertEqual(revision_batch.json["data"]["members"][0]["status"], "completed")
        now = utc_now()
        research_receipt = {
            "kind": "artifact", "id": artifact["id"], "version": 2, "parent_version": 1,
        }
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO research_revision_receipts(workspace_id,operation_key,artifact_id,request_hash,"
                "result_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (workspace_id, "research-revision-roundtrip", artifact["id"], "f" * 64, 2,
                 json.dumps(research_receipt), now),
            )
        delivery_reference = {
            "kind": "artifact", "artifact_id": artifact["id"], "version": 1,
            "format": "docx", "audience": "internal",
        }
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(id,workspace_id,user_id,work_id,kind,status,payload_json,request_hash,"
                "idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,'interrupted',?,?,?,?,?)",
                (delivery_job_id, workspace_id, session_identity["user"]["id"], work["id"], "delivery_batch",
                 json.dumps({"batch_id": delivery_batch_id}), "d" * 64, "partial-delivery-job", now, now),
            )
            connection.execute(
                "INSERT INTO delivery_batches(id,workspace_id,work_id,title,parent_batch_id,job_id,status,"
                "idempotency_key,request_hash,relative_path,sha256,size_bytes,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'preparing',?,?,?,NULL,NULL,?,?,?)",
                (delivery_batch_id, workspace_id, work["id"], "Preparing delivery", None, delivery_job_id,
                 "partial-delivery-batch", "e" * 64,
                 f"objects/{workspace_id}/deliveries/{delivery_batch_id}.zip",
                 session_identity["user"]["id"], now, now),
            )
            connection.execute(
                "INSERT INTO delivery_members(batch_id,workspace_id,member_id,ordinal,reference_json,reference_hash,"
                "filename,status,file_json,error_code,updated_at) VALUES(?,?,?,?,?,?,?,'completed',?,NULL,?)",
                (delivery_batch_id, workspace_id, delivery_member_id, 0,
                 json.dumps(delivery_reference), hashlib.sha256(json.dumps(delivery_reference, sort_keys=True).encode()).hexdigest(),
                 "delivery-member.docx", json.dumps({"relative_path": delivery_relative_path,
                 "sha256": delivery_sha, "size_bytes": len(delivery_bytes), "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}), now),
            )
        project, audio_material = self.create_media_project()
        transcription_job_id = self.add_transcription_candidate(project, audio_material)
        self.add_private_model_connection()
        original_workspace_id = self.request("get", "/api/session", headers=self.headers).json["data"]["workspace"]["id"]
        artifact_comment_id = str(uuid.uuid4())
        media_comment_id = str(uuid.uuid4())
        comment_now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO artifact_comments(id,artifact_id,artifact_version,workspace_id,author_id,anchor_json,"
                "body,state,resolution_json,version,created_at,updated_at,media_project_id,media_project_version) "
                "VALUES(?,?,2,?,?,?,'Retain this Artifact review comment.','open',NULL,1,?,?,NULL,NULL)",
                (artifact_comment_id, artifact["id"], workspace_id, session_identity["user"]["id"], "{}", comment_now, comment_now),
            )
            connection.execute(
                "INSERT INTO artifact_comment_versions(comment_id,version,snapshot_json,created_by,created_at) "
                "VALUES(?,1,?,?,?)",
                (artifact_comment_id, json.dumps({"artifact_id": artifact["id"], "artifact_version": 2,
                    "body": "Retain this Artifact review comment."}), session_identity["user"]["id"], comment_now),
            )
            connection.execute(
                "INSERT INTO artifact_comments(id,artifact_id,artifact_version,workspace_id,author_id,anchor_json,"
                "body,state,resolution_json,version,created_at,updated_at,media_project_id,media_project_version) "
                "VALUES(?,NULL,NULL,?,?,?,'Retain this Media review comment.','open',NULL,1,?,?,?,?)",
                (media_comment_id, workspace_id, session_identity["user"]["id"], "{}", comment_now, comment_now,
                 project["id"], project["current_version"]),
            )
            connection.execute(
                "INSERT INTO artifact_comment_versions(comment_id,version,snapshot_json,created_by,created_at) "
                "VALUES(?,1,?,?,?)",
                (media_comment_id, json.dumps({"media_project_id": project["id"],
                    "media_project_version": project["current_version"], "body": "Retain this Media review comment."}),
                 session_identity["user"]["id"], comment_now),
            )
        completed_interchange_id = str(uuid.uuid4())
        incomplete_interchange_id = str(uuid.uuid4())
        completed_interchange_bytes = b"portable interchange archive result"
        completed_interchange_path = f"objects/{workspace_id}/media-interchanges/{completed_interchange_id}.zip"
        completed_interchange_file = self.root / Path(completed_interchange_path)
        completed_interchange_file.parent.mkdir(parents=True, exist_ok=True)
        completed_interchange_file.write_bytes(completed_interchange_bytes)
        staging_path = f"objects/{workspace_id}/media-interchanges/{incomplete_interchange_id}.partial"
        (self.root / Path(staging_path)).write_bytes(b"unfinished interchange staging")
        with self.store.transaction() as connection:
            for interchange_id, job_status, interchange_status, relative_path, staging, output_bytes in (
                (completed_interchange_id, "completed", "completed", completed_interchange_path, None,
                 completed_interchange_bytes),
                (incomplete_interchange_id, "interrupted", "writing",
                 f"objects/{workspace_id}/media-interchanges/{incomplete_interchange_id}.zip",
                 staging_path, None),
            ):
                job_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO jobs(id,workspace_id,user_id,kind,status,payload_json,request_hash,"
                    "idempotency_key,attempts,lease_generation,created_at,updated_at) "
                    "VALUES(?,?,?,'media_interchange',?,?,?,?,0,1,?,?)",
                    (job_id, workspace_id, session_identity["user"]["id"], job_status,
                     json.dumps({"interchange_id": interchange_id}), "9" * 64,
                     f"interchange-backup-{interchange_id}", now, now),
                )
                connection.execute(
                    "INSERT INTO media_interchanges(id,workspace_id,project_id,project_version,job_id,target_profile,"
                    "adapter_revision,target_settings_json,accepted_loss_item_ids_json,source_snapshot_sha256,loss_report_json,"
                    "status,relative_path,staging_path,sha256,size_bytes,checkpoint_members_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'otio','test-adapter','{}','[]',?,'{}',?,?,?,?,?,'{}',?,?)",
                    (interchange_id, workspace_id, project["id"], project["current_version"], job_id,
                     "a" * 64, interchange_status, relative_path, staging,
                     None if output_bytes is None else hashlib.sha256(output_bytes).hexdigest(),
                     None if output_bytes is None else len(output_bytes), now, now),
                )

        response = self.request(
            "post",
            "/api/workspace-data/backups",
            json={},
            headers={**self.headers, "Idempotency-Key": "round-trip-backup"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        backup_operation_id = response.json["data"]["operation"]["id"]
        from server.oevira.worker import install_worker_schema

        install_worker_schema(self.store)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO job_control_receipts(workspace_id,operation_id,job_id,action,request_hash,"
                "control_revision,status,created_at) VALUES(?,?,?,'pause',?,1,'paused',?)",
                (workspace_id, "old-control-receipt", response.json["data"]["job"]["id"], "c" * 64, utc_now()),
            )
        source_file = self.store.row("SELECT relative_path FROM materials WHERE id=?", (text_material["id"],))["relative_path"]
        workspace_service = self.app.extensions["oevira.workspace_data"]
        original_writer = workspace_service._write_workspace_package

        def verify_protection(target_path, manifest, records_db, files, operation_id, claim):
            protection = self.store.row(
                "SELECT protection_state,protection_generation,protection_expires_at,protected_files_json "
                "FROM workspace_data_operations WHERE operation_id=?",
                (operation_id,),
            )
            self.assertEqual((protection["protection_state"], protection["protection_generation"]),
                             ("active", claim["generation"]))
            self.assertGreater(protection["protection_expires_at"], utc_now())
            protected_paths = {item["relative_path"] for item in json.loads(protection["protected_files_json"])}
            self.assertIn(source_file, protected_paths)
            account_service = self.app.extensions["oevira.account_service"]
            self.assertTrue(account_service._workspace_backup_protects_file(source_file))
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE workspace_data_operations SET protection_expires_at=? WHERE operation_id=?",
                    (expired_at.isoformat(timespec="seconds").replace("+00:00", "Z"), operation_id),
                )
            released = account_service._release_expired_backup_protections(
                datetime.now(timezone.utc) + timedelta(days=1),
            )
            self.assertEqual(released, 0)
            self.assertTrue(account_service._workspace_backup_protects_file(source_file))
            self.assertFalse(account_service._unlink_account_file(source_file))
            self.assertTrue((self.root / Path(source_file)).is_file())
            return original_writer(target_path, manifest, records_db, files, operation_id, claim)

        workspace_service._write_workspace_package = verify_protection
        try:
            self.run_worker("workspace_backup")
        finally:
            workspace_service._write_workspace_package = original_writer
        backup = self.request(
            "get", f"/api/workspace-data/operations/{backup_operation_id}", headers=self.headers
        ).json["data"]
        self.assertEqual(backup["state"], "backup_ready")
        released_protection = self.store.row(
            "SELECT protection_state,protected_files_json FROM workspace_data_operations WHERE operation_id=?",
            (backup_operation_id,),
        )
        self.assertEqual((released_protection["protection_state"], json.loads(released_protection["protected_files_json"])),
                         ("released", []))
        self.assertFalse(self.app.extensions["oevira.account_service"]._workspace_backup_protects_file(source_file))

        response = self.request(
            "get", f"/api/workspace-data/operations/{backup_operation_id}/package", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.headers.get("Content-Type"))
        package_bytes = response.get_data()
        self.assertEqual(
            __import__("hashlib").sha256(package_bytes).hexdigest(), backup["package_sha256"]
        )
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                manifest["protected_or_unavailable"]["warnings"]["browser_query_preview_bodies_not_backed_up"],
                2,
            )
            self.assertIn("media_transcriptions", manifest["schema"]["table_names"])
            self.assertIn("media_transcription_chunks", manifest["schema"]["table_names"])
            self.assertIn("acquisition_query_plans", manifest["schema"]["table_names"])
            self.assertIn("acquisition_query_paths", manifest["schema"]["table_names"])
            self.assertIn("browser_sessions", manifest["schema"]["table_names"])
            self.assertIn("media_interchanges", manifest["schema"]["table_names"])
            self.assertIn("delivery_batches", manifest["schema"]["table_names"])
            self.assertIn("delivery_members", manifest["schema"]["table_names"])
            self.assertIn("revision_batches", manifest["schema"]["table_names"])
            self.assertIn("revision_members", manifest["schema"]["table_names"])
            self.assertIn("research_revision_receipts", manifest["schema"]["table_names"])
            self.assertIn("job_control_receipts", manifest["schema"]["table_names"])
            self.assertIn("artifact_comments", manifest["schema"]["table_names"])
            self.assertIn("artifact_comment_versions", manifest["schema"]["table_names"])
            record_db = io.BytesIO(archive.read(manifest["record_database"]["member"]))
            self.assertNotIn(b"FAKE-CREDENTIAL-MATERIAL", record_db.getvalue())
            self.assertNotIn(b"FAKE-GITHUB-CREDENTIAL", record_db.getvalue())
            self.assertNotIn(b"FAKE-MCP-CREDENTIAL", record_db.getvalue())
            self.assertNotIn(b"NOT_FOR_BACKUP", record_db.getvalue())
            snapshot = sqlite3.connect(":memory:")
            snapshot.row_factory = sqlite3.Row
            try:
                snapshot.deserialize(record_db.getvalue())
                self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM job_control_receipts").fetchone()[0], 0)
                plans = snapshot.execute(
                    "SELECT query_id,owner_user_id,work_id,request_json FROM acquisition_query_plans"
                ).fetchall()
                self.assertEqual([row["query_id"] for row in plans], [own_query_id])
                self.assertEqual(plans[0]["work_id"], work["id"])
                self.assertNotIn("QUERY_PLAN_URL_SECRET", plans[0]["request_json"])
                paths = snapshot.execute(
                    "SELECT path_id,query_id,parent_path_id,path_kind,status,stop_reason,preview_expires_at,"
                    "source_ref_json,request_json,result_json,child_job_id,checkpoint_ref_json "
                    "FROM acquisition_query_paths ORDER BY path_order"
                ).fetchall()
                self.assertEqual([row["path_id"] for row in paths], [
                    own_query_paths["browser"], own_query_paths["expired_browser"], own_query_paths["url"],
                ])
                active_preview = paths[0]
                expired_preview = paths[1]
                self.assertEqual(expired_preview["parent_path_id"], active_preview["path_id"])
                self.assertEqual((active_preview["status"], active_preview["stop_reason"]),
                                 ("unavailable", "BROWSER_PREVIEW_NOT_RESTORED"))
                self.assertEqual(active_preview["preview_expires_at"], future_preview_expiry)
                active_result = json.loads(active_preview["result_json"])
                self.assertEqual(active_result["results"], [])
                self.assertEqual(active_result["source_ref"]["session_id"], browser_session_id)
                self.assertEqual(active_result["source_ref"]["snapshot_sha256"], "a" * 64)
                self.assertNotIn("UNADMITTED_BROWSER_PREVIEW_BODY", active_preview["result_json"])
                self.assertNotIn("BROWSER_URL_SECRET", active_preview["request_json"])
                self.assertNotIn("BROWSER_RESULT_SECRET", active_preview["result_json"])
                self.assertEqual((expired_preview["status"], expired_preview["stop_reason"],
                                  expired_preview["preview_expires_at"]),
                                 ("unavailable", "BROWSER_PREVIEW_EXPIRED", "2020-01-01T00:00:00Z"))
                self.assertNotIn("EXPIRED_BROWSER_PREVIEW_BODY", expired_preview["result_json"])
                url_path = paths[2]
                self.assertEqual(url_path["child_job_id"], child_job_id)
                self.assertEqual(json.loads(url_path["checkpoint_ref_json"])["job_id"], child_job_id)
                self.assertNotIn("QUERY_PATH_SECRET", url_path["request_json"])
                self.assertNotIn("QUERY_JOB_SECRET", record_db.getvalue().decode("latin1"))
                self.assertEqual(tuple(snapshot.execute(
                    "SELECT status,state_ciphertext,state_protection FROM browser_sessions WHERE id=?",
                    (browser_session_id,),
                ).fetchone()), ("open", None, None))
                for table in ("intelligence_model_connections", "intelligence_github_connections",
                              "intelligence_mcp_connections"):
                    row = snapshot.execute(
                        f"SELECT status,credential_ciphertext,credential_protection FROM {table} LIMIT 1"
                    ).fetchone()
                    self.assertEqual((row[0], row[1], row[2]), ("revoked", None, None))
                batch = snapshot.execute(
                    "SELECT status,relative_path,sha256,size_bytes FROM delivery_batches WHERE id=?",
                    (delivery_batch_id,),
                ).fetchone()
                self.assertEqual(batch[0], "partial")
                self.assertEqual(batch[2:], (None, None))
                member = snapshot.execute(
                    "SELECT file_json FROM delivery_members WHERE batch_id=? AND member_id=?",
                    (delivery_batch_id, delivery_member_id),
                ).fetchone()
                member_file = json.loads(member[0])
                self.assertTrue(member_file["relative_path"].startswith("files/"))
                self.assertEqual(archive.read(member_file["relative_path"]), delivery_bytes)
                completed_interchange = snapshot.execute(
                    "SELECT status,relative_path,staging_path,sha256,size_bytes FROM media_interchanges WHERE id=?",
                    (completed_interchange_id,),
                ).fetchone()
                self.assertEqual(completed_interchange[0], "completed")
                self.assertTrue(completed_interchange[1].startswith("files/"))
                self.assertIsNone(completed_interchange[2])
                self.assertEqual(archive.read(completed_interchange[1]), completed_interchange_bytes)
                incomplete_interchange = snapshot.execute(
                    "SELECT status,relative_path,staging_path,sha256,size_bytes FROM media_interchanges WHERE id=?",
                    (incomplete_interchange_id,),
                ).fetchone()
                self.assertEqual(tuple(incomplete_interchange), ("interrupted", None, None, None, None))
            finally:
                snapshot.close()
        self.assertIn("UNADMITTED_BROWSER_PREVIEW_BODY", self.store.row(
            "SELECT result_json FROM acquisition_query_paths WHERE path_id=?",
            (own_query_paths["browser"],),
        )["result_json"])

        response = self.request(
            "post",
            "/api/workspace-data/restores",
            data={"package": (io.BytesIO(package_bytes), "workspace.zip")},
            content_type="multipart/form-data",
            headers={**self.headers, "Idempotency-Key": "round-trip-upload"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        restore_operation_id = response.json["data"]["operation"]["id"]
        preview_job_id = response.json["data"]["job"]["id"]
        pause_preview = self.request(
            "post", f"/api/jobs/{preview_job_id}/pause",
            json={"operation_id": "pause-restore-preview", "base_control_revision": 0},
            headers=self.headers,
        )
        self.assertEqual(pause_preview.status_code, 200, pause_preview.get_data(as_text=True))
        self.assertEqual(pause_preview.json["data"]["status"], "paused")
        resume_preview = self.request(
            "post", f"/api/workspace-data/operations/{restore_operation_id}/resume",
            json={"base_control_revision": 1},
            headers={**self.headers, "Idempotency-Key": "resume-restore-preview"},
        )
        self.assertEqual(resume_preview.status_code, 202, resume_preview.get_data(as_text=True))
        self.assertEqual(resume_preview.json["data"]["job"]["id"], preview_job_id)
        self.run_worker("workspace_restore")
        preview = self.request(
            "get", f"/api/workspace-data/operations/{restore_operation_id}", headers=self.headers
        ).json["data"]
        self.assertIn(preview["state"], {"preview_ready", "preview_conflicts"}, preview)
        self.assertEqual(preview["preview"]["blocking_conflicts"], [])
        self.assertEqual(preview["preview"]["missing_file_count"], 0)
        self.assertEqual(preview["preview"]["missing_author_count"], 0)

        response = self.request(
            "post",
            f"/api/workspace-data/operations/{restore_operation_id}/restore",
            json={"target_name": "Recovered workspace", "resolutions": {"accept_restricted_scope": True}},
            headers={**self.headers, "Idempotency-Key": "round-trip-execute"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        execute_job_id = response.json["data"]["job"]["id"]
        pause_execute = self.request(
            "post", f"/api/jobs/{execute_job_id}/pause",
            json={"operation_id": "pause-restore-execute", "base_control_revision": 0},
            headers=self.headers,
        )
        self.assertEqual(pause_execute.status_code, 200, pause_execute.get_data(as_text=True))
        self.assertEqual(pause_execute.json["data"]["status"], "paused")
        resume_execute = self.request(
            "post", f"/api/workspace-data/operations/{restore_operation_id}/resume",
            json={"base_control_revision": 1},
            headers={**self.headers, "Idempotency-Key": "resume-restore-execute"},
        )
        self.assertEqual(resume_execute.status_code, 202, resume_execute.get_data(as_text=True))
        self.assertEqual(resume_execute.json["data"]["job"]["id"], execute_job_id)
        self.run_worker("workspace_restore")
        restored = self.request(
            "get", f"/api/workspace-data/operations/{restore_operation_id}", headers=self.headers
        ).json["data"]
        self.assertEqual(restored["state"], "complete", restored)
        new_workspace_id = restored["target_workspace_id"]
        self.assertNotEqual(original_workspace_id, new_workspace_id)

        response = self.request(
            "post", f"/api/workspace-data/operations/{restore_operation_id}/activate",
            json={}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        restored_token = response.json["data"]["token"]
        restored_headers = {"Authorization": f"Bearer {restored_token}"}

        # Open through a fresh application/Store instance to verify persisted recovery.
        reopened_app = create_app(self.root, expected_port=8317)
        reopened_app.config["TESTING"] = True
        reopened = reopened_app.test_client()
        session = reopened.get("/api/session", headers=restored_headers, base_url=BASE_URL)
        self.assertEqual(session.status_code, 200, session.get_data(as_text=True))
        self.assertEqual(session.json["data"]["workspace"]["id"], new_workspace_id)
        works = reopened.get("/api/works", headers=restored_headers, base_url=BASE_URL).json["data"]
        materials = reopened.get("/api/materials", headers=restored_headers, base_url=BASE_URL).json["data"]
        artifacts = reopened.get("/api/artifacts", headers=restored_headers, base_url=BASE_URL).json["data"]
        projects = reopened.get("/api/media/projects", headers=restored_headers, base_url=BASE_URL).json["data"]
        self.assertEqual(len(works), 1)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(len(projects), 1)
        restored_query = self.store.row(
            "SELECT query_id,workspace_id,owner_user_id,work_id,request_json FROM acquisition_query_plans "
            "WHERE workspace_id=?", (new_workspace_id,),
        )
        self.assertEqual(restored_query["owner_user_id"], user_id)
        self.assertEqual(restored_query["work_id"], works[0]["id"])
        restored_browser_session = self.store.row(
            "SELECT id,status,membership_created_at,state_ciphertext,state_protection,work_id "
            "FROM browser_sessions WHERE workspace_id=?", (new_workspace_id,),
        )
        self.assertNotEqual(restored_browser_session["id"], browser_session_id)
        self.assertEqual(restored_browser_session["status"], "closed")
        self.assertIsNone(restored_browser_session["state_ciphertext"])
        self.assertIsNone(restored_browser_session["state_protection"])
        self.assertEqual(restored_browser_session["work_id"], works[0]["id"])
        restored_capture = self.store.row("SELECT * FROM browser_session_operations WHERE session_id=? AND operation='context_preview'",
                                          (restored_browser_session["id"],))
        self.assertIsNotNone(restored_capture)
        self.assertIsNone(restored_capture["preview_json"])
        self.assertEqual(restored_capture["preview_expires_at"], future_preview_expiry)
        self.assertTrue(restored_capture["idempotency_key"].startswith("restored-history:"))
        restored_ref = json.loads(restored_capture["result_json"])["capture_ref"]
        self.assertEqual(restored_ref["capture_id"], restored_capture["id"])
        self.assertEqual(restored_ref["session_id"], restored_browser_session["id"])
        self.assertEqual(restored_ref["work_id"], works[0]["id"])
        self.assertIn("FROZEN_BROWSER_CONTEXT_MUST_NOT_BE_BACKED_UP",
                      self.store.row("SELECT preview_json FROM browser_session_operations WHERE id=?", (browser_capture_id,))["preview_json"])
        restored_query_paths = self.store.rows(
            "SELECT * FROM acquisition_query_paths WHERE query_id=? ORDER BY path_order",
            (restored_query["query_id"],),
        )
        self.assertEqual(len(restored_query_paths), 3)
        restored_browser_path = restored_query_paths[0]
        restored_expired_browser_path = restored_query_paths[1]
        restored_url_path = restored_query_paths[2]
        self.assertEqual(restored_expired_browser_path["parent_path_id"], restored_browser_path["path_id"])
        self.assertEqual((restored_browser_path["status"], restored_browser_path["stop_reason"]),
                         ("unavailable", "BROWSER_PREVIEW_NOT_RESTORED"))
        self.assertEqual((restored_expired_browser_path["status"], restored_expired_browser_path["stop_reason"]),
                         ("unavailable", "BROWSER_PREVIEW_EXPIRED"))
        restored_request = json.loads(restored_browser_path["request_json"])
        restored_source_ref = json.loads(restored_browser_path["source_ref_json"])
        restored_checkpoint = json.loads(restored_browser_path["checkpoint_ref_json"])
        self.assertEqual(restored_request["target"]["session_id"], restored_browser_session["id"])
        self.assertEqual(restored_source_ref["session_id"], restored_browser_session["id"])
        self.assertEqual(restored_checkpoint["browser_session_id"], restored_browser_session["id"])
        restored_child_job = self.store.row(
            "SELECT id,status FROM jobs WHERE id=? AND workspace_id=?",
            (restored_url_path["child_job_id"], new_workspace_id),
        )
        self.assertNotEqual(restored_url_path["child_job_id"], child_job_id)
        self.assertEqual(restored_child_job["status"], "completed")
        self.assertEqual(
            json.loads(restored_url_path["checkpoint_ref_json"])["job_id"], restored_child_job["id"],
        )
        from unittest.mock import patch
        with patch.object(reopened_app.extensions["oevira.browser"], "read_context",
                          side_effect=AssertionError("restored Browser preview must not be reread")):
            query_response = reopened.get(
                f"/api/search/{restored_query['query_id']}",
                headers=restored_headers,
                base_url=BASE_URL,
            )
        self.assertEqual(query_response.status_code, 200, query_response.get_data(as_text=True))
        restored_query_view = query_response.json["data"]
        self.assertNotIn("UNADMITTED_BROWSER_PREVIEW_BODY", json.dumps(restored_query_view))
        self.assertNotIn("EXPIRED_BROWSER_PREVIEW_BODY", json.dumps(restored_query_view))
        self.assertIn(
            restored_browser_path["path_id"],
            {item["path_id"] for item in restored_query_view["query_paths"]},
        )
        query_response.close()
        restored_delivery = self.store.row(
            "SELECT id,status,relative_path,sha256,size_bytes FROM delivery_batches WHERE workspace_id=?",
            (new_workspace_id,),
        )
        self.assertEqual(restored_delivery["status"], "partial")
        restore_generation = self.store.row("SELECT lease_generation FROM jobs WHERE id=?", (execute_job_id,))["lease_generation"]
        self.assertEqual(
            restored_delivery["relative_path"],
            f"objects/{new_workspace_id}/restore/{restore_operation_id}/{restore_generation}/deliveries/{restored_delivery['id']}.zip",
        )
        self.assertIsNone(restored_delivery["sha256"])
        restored_delivery_member = self.store.row(
            "SELECT file_json,status FROM delivery_members WHERE batch_id=?",
            (restored_delivery["id"],),
        )
        self.assertEqual(restored_delivery_member["status"], "completed")
        restored_member_file = json.loads(restored_delivery_member["file_json"])
        restored_member_path = self.root / Path(restored_member_file["relative_path"])
        self.assertEqual(restored_member_path.read_bytes(), delivery_bytes)
        restored_interchanges = self.store.rows(
            "SELECT * FROM media_interchanges WHERE workspace_id=? ORDER BY status,id",
            (new_workspace_id,),
        )
        restored_complete_interchange = next(item for item in restored_interchanges if item["status"] == "completed")
        restored_incomplete_interchange = next(item for item in restored_interchanges if item["status"] == "interrupted")
        self.assertEqual(restored_complete_interchange["project_id"], projects[0]["id"])
        self.assertIsNone(restored_incomplete_interchange["staging_path"])
        self.assertIsNone(restored_incomplete_interchange["relative_path"])
        completed_interchange_file = self.root / Path(restored_complete_interchange["relative_path"])
        self.assertEqual(completed_interchange_file.read_bytes(), completed_interchange_bytes)
        completed_interchange_job = self.store.row(
            "SELECT status,payload_json,error_code FROM jobs WHERE id=?",
            (restored_complete_interchange["job_id"],),
        )
        self.assertEqual(completed_interchange_job["status"], "completed")
        self.assertEqual(json.loads(completed_interchange_job["payload_json"])["interchange_id"],
                         restored_complete_interchange["id"])
        incomplete_interchange_job = self.store.row(
            "SELECT status,payload_json,error_code FROM jobs WHERE id=?",
            (restored_incomplete_interchange["job_id"],),
        )
        self.assertEqual(incomplete_interchange_job["status"], "interrupted")
        self.assertEqual(incomplete_interchange_job["error_code"], "WORKSPACE_RESTORE_HISTORY_ONLY")
        self.assertEqual(json.loads(incomplete_interchange_job["payload_json"])["interchange_id"],
                         restored_incomplete_interchange["id"])
        restored_revision_batch = self.store.row(
            "SELECT id,job_id FROM revision_batches WHERE workspace_id=?",
            (new_workspace_id,),
        )
        restored_revision = reopened.get(
            f"/api/revision-batches/{restored_revision_batch['id']}",
            headers=restored_headers,
            base_url=BASE_URL,
        )
        self.assertEqual(restored_revision.status_code, 200, restored_revision.get_data(as_text=True))
        restored_member = restored_revision.json["data"]["members"][0]
        self.assertEqual(restored_member["status"], "completed")
        restored_artifact_id = self.store.row(
            "SELECT id FROM artifacts WHERE workspace_id=?", (new_workspace_id,),
        )["id"]
        self.assertEqual(restored_member["target"]["id"], restored_artifact_id)
        self.assertEqual(restored_member["result_ref"]["id"], restored_artifact_id)
        restored_revision_job = self.store.row("SELECT payload_json,status,error_code FROM jobs WHERE id=?",
                                               (restored_revision_batch["job_id"],))
        self.assertEqual(json.loads(restored_revision_job["payload_json"])["batch_id"], restored_revision_batch["id"])
        self.assertEqual(restored_revision_job["status"], "completed")
        research_result = self.store.row(
            "SELECT artifact_id,result_json FROM research_revision_receipts WHERE workspace_id=?",
            (new_workspace_id,),
        )
        self.assertEqual(research_result["artifact_id"], restored_artifact_id)
        self.assertEqual(json.loads(research_result["result_json"])["id"], restored_artifact_id)
        restored_work = works[0]
        restored_text = next(item for item in materials if item["title"] == text_material["title"])
        restored_audio = next(item for item in materials if item["title"] == audio_material["title"])
        restored_artifact = artifacts[0]
        self.assertNotEqual(restored_work["id"], work["id"])
        self.assertNotEqual(restored_text["id"], text_material["id"])
        self.assertEqual(restored_artifact["work_id"], restored_work["id"])
        restored_comments = self.store.rows(
            "SELECT * FROM artifact_comments WHERE workspace_id=? ORDER BY id", (new_workspace_id,),
        )
        self.assertEqual(len(restored_comments), 2)
        restored_artifact_comment = next(item for item in restored_comments if item["artifact_id"] is not None)
        restored_media_comment = next(item for item in restored_comments if item["media_project_id"] is not None)
        self.assertEqual((restored_artifact_comment["artifact_id"], restored_artifact_comment["artifact_version"]),
                         (restored_artifact["id"], 2))
        self.assertEqual((restored_media_comment["media_project_id"], restored_media_comment["media_project_version"]),
                         (projects[0]["id"], projects[0]["current_version"]))
        artifact_comment_history = self.store.row(
            "SELECT snapshot_json FROM artifact_comment_versions WHERE comment_id=?",
            (restored_artifact_comment["id"],),
        )
        media_comment_history = self.store.row(
            "SELECT snapshot_json FROM artifact_comment_versions WHERE comment_id=?",
            (restored_media_comment["id"],),
        )
        self.assertEqual(json.loads(artifact_comment_history["snapshot_json"])["artifact_id"], restored_artifact["id"])
        self.assertEqual(json.loads(media_comment_history["snapshot_json"])["media_project_id"], projects[0]["id"])

        artifact_detail = reopened.get(
            f"/api/artifacts/{restored_artifact['id']}", headers=restored_headers, base_url=BASE_URL
        ).json["data"]
        self.assertEqual(artifact_detail["input_snapshot"]["work"]["id"], restored_work["id"])
        self.assertEqual(artifact_detail["input_snapshot"]["materials"][0]["id"], restored_text["id"])
        self.assertEqual(artifact_detail["citations"][0]["material_id"], restored_text["id"])
        material_file = reopened.get(
            f"/api/materials/{restored_text['id']}/file", headers=restored_headers, base_url=BASE_URL
        )
        self.assertEqual(material_file.status_code, 200, material_file.get_data(as_text=True))
        self.assertEqual(material_file.get_data(), b"A preserved source sentence.")
        material_file.close()

        restored_project = projects[0]
        media_detail = reopened.get(
            f"/api/media/projects/{restored_project['id']}",
            headers=restored_headers,
            base_url=BASE_URL,
        )
        self.assertEqual(media_detail.status_code, 200, media_detail.get_data(as_text=True))
        source = media_detail.json["data"]["sources"][0]
        self.assertEqual(source["source_material_id"], restored_audio["id"])
        self.assertNotEqual(source["asset_id"], project["sources"][0]["asset_id"])
        # The transcription job id is remapped with the rest of the private
        # Job history, so reopen it through the actual Media owner endpoint.
        self.assertNotEqual(transcription_job_id, self.store.row(
            "SELECT job_id FROM media_transcriptions WHERE workspace_id=? AND project_id=?",
            (new_workspace_id, restored_project["id"]),
        )["job_id"])
        restored_transcription_id = self.store.row(
            "SELECT job_id FROM media_transcriptions WHERE workspace_id=? AND project_id=?",
            (new_workspace_id, restored_project["id"]),
        )["job_id"]
        transcript = reopened.get(
            f"/api/media/transcriptions/{restored_transcription_id}",
            headers=restored_headers,
            base_url=BASE_URL,
        )
        self.assertEqual(transcript.status_code, 200, transcript.get_data(as_text=True))
        self.assertEqual(transcript.json["data"]["candidate"]["text"], "A transcript candidate.")
        self.assertEqual(transcript.json["data"]["source"]["source_material_id"], restored_audio["id"])
        transcript.close()

        restored_secret = self.store.row(
            "SELECT status,credential_ciphertext,credential_protection,base_url FROM intelligence_model_connections "
            "WHERE workspace_id=?",
            (new_workspace_id,),
        )
        self.assertEqual(restored_secret["status"], "revoked")
        self.assertIsNone(restored_secret["credential_ciphertext"])
        self.assertIsNone(restored_secret["credential_protection"])
        self.assertNotIn("NOT_FOR_BACKUP", restored_secret["base_url"])
        restored_github = self.store.row(
            "SELECT status,credential_ciphertext,credential_protection FROM intelligence_github_connections "
            "WHERE workspace_id=?",
            (new_workspace_id,),
        )
        restored_mcp = self.store.row(
            "SELECT status,credential_ciphertext,credential_protection,authorized_use_confirmed "
            "FROM intelligence_mcp_connections WHERE workspace_id=?",
            (new_workspace_id,),
        )
        self.assertEqual((restored_github["status"], restored_github["credential_ciphertext"],
                          restored_github["credential_protection"]), ("revoked", None, None))
        self.assertEqual((restored_mcp["status"], restored_mcp["credential_ciphertext"],
                          restored_mcp["credential_protection"], restored_mcp["authorized_use_confirmed"]),
                         ("revoked", None, None, 0))
        self.assertEqual(self.store.row(
            "SELECT COUNT(*) AS count FROM job_control_receipts WHERE workspace_id=?",
            (new_workspace_id,),
        )["count"], 0)
        self.assertEqual(
            self.store.row("SELECT id FROM workspaces WHERE id=?", (original_workspace_id,))["id"],
            original_workspace_id,
        )

    def test_backup_excludes_work_and_material_outside_current_commission_access(self) -> None:
        own_work, _material, _artifact = self.create_user_work_content()
        identity = self.request("get", "/api/session", headers=self.headers).json["data"]
        workspace_id = identity["workspace"]["id"]
        other_user_id = str(uuid.uuid4())
        commission_id = str(uuid.uuid4())
        hidden_work_id = str(uuid.uuid4())
        hidden_media_project_id = str(uuid.uuid4())
        hidden_media_comment_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                (other_user_id, "Other member", now),
            )
            connection.execute(
                "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) VALUES(?,?,'editor',1,?)",
                (workspace_id, other_user_id, now),
            )
            connection.execute(
                "INSERT INTO intelligence_mcp_connections(connection_id,workspace_id,owner_user_id,name,endpoint,status,revision,"
                "credential_ciphertext,credential_protection,protocol_version,server_info_json,capabilities_json,"
                "tool_list_sha256,tool_count,authorized_use_confirmed,validation_status,verified_at,created_at,updated_at) "
                "VALUES('hidden-member-mcp',?,?, 'Hidden member MCP','https://mcp.example.test/','active',1,?,'test-protection',"
                "'2025-11-25','{}','{}',?,1,1,'valid',?,?,?)",
                (workspace_id, other_user_id, b"OTHER_MEMBER_MCP_SECRET", "a" * 64, now, now, now),
            )
            connection.execute(
                "INSERT INTO client_commissions(id,workspace_id,name,scope_json,status,version,created_by,created_at,updated_at) "
                "VALUES(?,?,?,'{}','active',1,?,?,?)",
                (commission_id, workspace_id, "Private client", other_user_id, now, now),
            )
            connection.execute(
                "INSERT INTO commission_members(commission_id,user_id,role,active) VALUES(?,?,'owner',1)",
                (commission_id, other_user_id),
            )
            connection.execute(
                "INSERT INTO works(id,workspace_id,owner_user_id,title,goal,brief_json,status,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'{}','active',1,?,?)",
                (hidden_work_id, workspace_id, other_user_id, "Hidden client work", "Private brief", now, now),
            )
            connection.execute(
                "INSERT INTO work_versions(work_id,version,title,goal,brief_json,status,created_at,created_by) "
                "VALUES(?,1,'Hidden client work','Private brief','{}','active',?,?)",
                (hidden_work_id, now, other_user_id),
            )
            connection.execute(
                "INSERT INTO work_commissions(work_id,commission_id,created_at) VALUES(?,?,?)",
                (hidden_work_id, commission_id, now),
            )
            connection.execute(
                "INSERT INTO media_projects(id,workspace_id,owner_user_id,work_id,title,kind,current_version,created_at,updated_at,created_by) "
                "VALUES(?,?,?,?,?,'video',1,?,?,?)",
                (hidden_media_project_id, workspace_id, other_user_id, hidden_work_id, "Hidden client media", now, now, other_user_id),
            )
            connection.execute(
                "INSERT INTO media_project_versions(project_id,workspace_id,version,parent_version,operation,state_json,"
                "undo_stack_json,redo_stack_json,created_at,created_by) VALUES(?,?,1,NULL,'fixture','{}','[]','[]',?,?)",
                (hidden_media_project_id, workspace_id, now, other_user_id),
            )
            connection.execute(
                "INSERT INTO artifact_comments(id,artifact_id,artifact_version,workspace_id,author_id,anchor_json,body,state,"
                "resolution_json,version,created_at,updated_at,media_project_id,media_project_version) "
                "VALUES(?,NULL,NULL,?,?,?,'Hidden client media comment','open',NULL,1,?,?,?,1)",
                (hidden_media_comment_id, workspace_id, other_user_id, "{}", now, now, hidden_media_project_id),
            )
            connection.execute(
                "INSERT INTO artifact_comment_versions(comment_id,version,snapshot_json,created_by,created_at) VALUES(?,1,?,?,?)",
                (hidden_media_comment_id, json.dumps({"media_project_id": hidden_media_project_id,
                    "media_project_version": 1, "body": "Hidden client media comment"}), other_user_id, now),
            )
            connection.execute(
                "INSERT INTO materials(id,workspace_id,owner_user_id,work_id,title,filename,format,status,sha256,relative_path,size_bytes,source_json,created_at) "
                "VALUES(?,?,?,?,?,'private.txt','txt','ready',?,'objects/placeholder',6,'{}',?)",
                (str(uuid.uuid4()), workspace_id, other_user_id, hidden_work_id, "Hidden client source", "c" * 64, now),
            )

        self.assertNotEqual(own_work["id"], hidden_work_id)
        backup_response = self.request(
            "post",
            "/api/workspace-data/backups",
            json={},
            headers={**self.headers, "Idempotency-Key": "scope-backup"},
        )
        self.assertEqual(backup_response.status_code, 202)
        operation_id = backup_response.json["data"]["operation"]["id"]
        self.run_worker("workspace_backup")
        operation = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}", headers=self.headers
        ).json["data"]
        self.assertEqual(operation["state"], "backup_ready")
        self.assertEqual(operation["summary"]["coverage"]["restricted_work_count"], 1)
        self.assertEqual(operation["summary"]["coverage"]["restricted_commission_count"], 1)
        response = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}/package", headers=self.headers
        )
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            record_path = Path(self.temporary.name) / "scope-records.sqlite3"
            record_path.write_bytes(archive.read(manifest["record_database"]["member"]))
        read_only = sqlite3.connect(f"file:{record_path.as_posix()}?mode=ro", uri=True)
        try:
            self.assertEqual(read_only.execute("SELECT COUNT(*) FROM works WHERE id=?", (hidden_work_id,)).fetchone()[0], 0)
            self.assertEqual(read_only.execute("SELECT COUNT(*) FROM materials WHERE title='Hidden client source'").fetchone()[0], 0)
            self.assertEqual(read_only.execute(
                "SELECT COUNT(*) FROM intelligence_mcp_connections WHERE owner_user_id=?", (other_user_id,),
            ).fetchone()[0], 0)
            self.assertNotIn(b"OTHER_MEMBER_MCP_SECRET", record_path.read_bytes())
            self.assertEqual(read_only.execute(
                "SELECT COUNT(*) FROM media_projects WHERE id=?", (hidden_media_project_id,),
            ).fetchone()[0], 0)
            self.assertEqual(read_only.execute(
                "SELECT COUNT(*) FROM artifact_comments WHERE id=?", (hidden_media_comment_id,),
            ).fetchone()[0], 0)
        finally:
            read_only.close()

    def test_missing_backup_original_fails_without_publishing_then_retries_after_repair(self) -> None:
        _work, material, _artifact = self.create_user_work_content()
        material_row = self.store.row("SELECT relative_path FROM materials WHERE id=?", (material["id"],))
        original_path = self.root / Path(material_row["relative_path"])
        original_bytes = original_path.read_bytes()
        original_path.unlink()
        response = self.request(
            "post", "/api/workspace-data/backups", json={},
            headers={**self.headers, "Idempotency-Key": "missing-original-backup"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        operation_id = response.json["data"]["operation"]["id"]
        job_id = response.json["data"]["job"]["id"]
        self.run_worker("workspace_backup")
        failed = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}", headers=self.headers,
        ).json["data"]
        self.assertEqual((failed["state"], failed["error_code"], failed["package_available"]),
                         ("failed", "WORKSPACE_BACKUP_INCOMPLETE", False))
        self.assertEqual(len(failed["summary"]["protected_or_unavailable"]["missing_files"]), 1)
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM account_exports")["count"], 0)

        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(original_bytes)
        retried = self.request(
            "post", f"/api/jobs/{job_id}/retry",
            json={"operation_id": "retry-after-restoring-source", "base_control_revision": 0},
            headers=self.headers,
        )
        self.assertEqual(retried.status_code, 202, retried.get_data(as_text=True))
        self.run_worker("workspace_backup")
        restored = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}", headers=self.headers,
        ).json["data"]
        self.assertEqual(restored["state"], "backup_ready", restored)

    def test_cancelled_backup_can_resume_and_untrusted_zip_paths_are_rejected(self) -> None:
        _work, material, _artifact = self.create_user_work_content()
        material_row = self.store.row("SELECT relative_path FROM materials WHERE id=?", (material["id"],))
        source_path = self.root / Path(material_row["relative_path"])
        source_bytes = source_path.read_bytes()
        response = self.request(
            "post",
            "/api/workspace-data/backups",
            json={},
            headers={**self.headers, "Idempotency-Key": "cancel-backup"},
        )
        operation_id = response.json["data"]["operation"]["id"]
        job_id = response.json["data"]["job"]["id"]
        cancelled = self.request(
            "post",
            f"/api/workspace-data/operations/{operation_id}/cancel",
            json={"base_control_revision": 0},
            headers={**self.headers, "Idempotency-Key": "cancel-backup-control"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
        self.assertEqual(cancelled.json["data"]["operation"]["state"], "cancelled")
        self.assertEqual(cancelled.json["data"]["job"]["id"], job_id)
        self.assertFalse(cancelled.json["data"]["replayed"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM job_control_receipts")["count"], 1)
        replayed_cancel = self.request(
            "post",
            f"/api/workspace-data/operations/{operation_id}/cancel",
            json={"base_control_revision": 0},
            headers={**self.headers, "Idempotency-Key": "cancel-backup-control"},
        )
        self.assertEqual(replayed_cancel.status_code, 200, replayed_cancel.get_data(as_text=True))
        self.assertTrue(replayed_cancel.json["data"]["replayed"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM job_control_receipts")["count"], 1)
        resumed = self.request(
            "post",
            f"/api/workspace-data/operations/{operation_id}/resume",
            json={"base_control_revision": 1},
            headers={**self.headers, "Idempotency-Key": "resume-backup"},
        )
        self.assertEqual(resumed.status_code, 202, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["job"]["id"], job_id)
        self.run_worker("workspace_backup")
        finished = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}", headers=self.headers
        )
        self.assertEqual(finished.json["data"]["state"], "backup_ready")
        package = self.request(
            "get", f"/api/workspace-data/operations/{operation_id}/package", headers=self.headers,
        )
        with zipfile.ZipFile(io.BytesIO(package.get_data())) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertIn("job_control_receipts", manifest["schema"]["table_names"])
            records = sqlite3.connect(":memory:")
            try:
                records.deserialize(archive.read(manifest["record_database"]["member"]))
                self.assertEqual(records.execute("SELECT COUNT(*) FROM job_control_receipts").fetchone()[0], 0)
            finally:
                records.close()

        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../escape.txt", b"never extract")
            archive.writestr("manifest.json", b"{}")
        malicious.seek(0)
        upload = self.request(
            "post",
            "/api/workspace-data/restores",
            data={"package": (malicious, "bad.zip")},
            content_type="multipart/form-data",
            headers={**self.headers, "Idempotency-Key": "malicious-zip"},
        )
        self.assertEqual(upload.status_code, 202)
        malicious_operation = upload.json["data"]["operation"]["id"]
        self.run_worker("workspace_restore")
        failed = self.request(
            "get", f"/api/workspace-data/operations/{malicious_operation}", headers=self.headers
        ).json["data"]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error_code"], "WORKSPACE_PACKAGE_PATH_INVALID")
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertTrue(source_path.is_file())
        self.assertEqual(source_path.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
