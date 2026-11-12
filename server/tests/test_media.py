from __future__ import annotations

from array import array
import base64
import hashlib
import json
import io
import math
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import wave
import zlib
import zipfile
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from server.oevira import create_app
from server.oevira import media_api as media_api_module
from server.oevira.auth import issue_local_capability, utc_now
from server.oevira.media import Timeline, _ffmpeg_path, _ffprobe_path, fingerprint_local_media
from server.oevira.media_api import recover_media_uploads
from server.oevira.media_rendering import render_timeline
from server.oevira.worker import run_once


BASE_URL = "http://127.0.0.1:8317"
ORIGIN = BASE_URL


class MediaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        task_temp_root = Path(__file__).resolve().parents[2] / ".runtime" / "verification" / "media-tests"
        task_temp_root.mkdir(parents=True, exist_ok=True)
        self._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(task_temp_root)
        self.temporary = tempfile.TemporaryDirectory(prefix="oevira-media-", dir=task_temp_root)
        self.temp_root = Path(self.temporary.name)
        self.data_root = self.temp_root / "data"
        self.app = create_app(self.data_root, expected_port=8317)
        self.assertIn("media_list_projects", self.app.view_functions)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        capability_file = self.data_root / "bootstrap.capability"
        issue_local_capability(self.store, capability_file)
        self.client = self.app.test_client()
        response = self.request(
            "post",
            "/api/local-session",
            json={
                "capability": capability_file.read_text(encoding="ascii"),
                "display_name": "Media 隔离验收",
            },
            authenticated=False,
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.token = response.json["data"]["token"]
        self.identity = response.json["data"]

    def tearDown(self) -> None:
        self.temporary.cleanup()
        tempfile.tempdir = self._previous_tempdir

    def request(self, method: str, path: str, *, authenticated: bool = True, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            headers.setdefault("Authorization", f"Bearer {self.token}")
            headers.setdefault("Origin", ORIGIN)
        kwargs["headers"] = headers
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def start_upload(
        self, data: bytes, *, filename: str, key: str, title: str = "素材", work_id: str | None = None,
    ) -> dict:
        payload = {
            "kind": "media",
            "title": title,
            "filename": filename,
            "expected_size": len(data),
            "expected_sha256": hashlib.sha256(data).hexdigest(),
        }
        if work_id is not None:
            payload["work_id"] = work_id
        response = self.request(
            "post",
            "/api/uploads",
            json=payload,
            headers={"Idempotency-Key": key},
        )
        self.assertIn(response.status_code, {200, 201}, response.get_data(as_text=True))
        return response.json["data"]

    def upload_data(
        self, data: bytes, *, filename: str, key: str, title: str = "素材", chunk_size: int = 1024,
        work_id: str | None = None,
    ) -> dict:
        upload = self.start_upload(data, filename=filename, key=key, title=title, work_id=work_id)
        upload_id = upload["id"]
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset:offset + chunk_size]
            response = self.request(
                "patch",
                f"/api/uploads/{upload_id}",
                data=chunk,
                content_type="application/octet-stream",
                headers={
                    "Upload-Offset": str(offset),
                    "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                },
            )
            self.assertEqual(response.status_code, 204, response.get_data(as_text=True))
        completed = self.request("post", f"/api/uploads/{upload_id}/complete")
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        return {"upload": completed.json["data"]["upload"], "material": completed.json["data"]["material"]}

    def create_client_work(self, name: str, key: str) -> dict:
        work_response = self.request(
            "post", "/api/works", json={"title": f"{name}工作", "goal": "媒体权限隔离验收"},
            headers={"Idempotency-Key": f"{key}-work"},
        )
        self.assertEqual(work_response.status_code, 201, work_response.get_data(as_text=True))
        work = work_response.json["data"]
        commission_response = self.request(
            "post", "/api/business/commissions", json={"name": name, "scope": {}},
            headers={"Idempotency-Key": f"{key}-commission"},
        )
        self.assertEqual(commission_response.status_code, 201, commission_response.get_data(as_text=True))
        commission = commission_response.json["data"]
        assigned = self.request(
            "post", f"/api/business/commissions/{commission['id']}/works/{work['id']}", json={},
            headers={"Idempotency-Key": f"{key}-assignment"},
        )
        self.assertEqual(assigned.status_code, 201, assigned.get_data(as_text=True))
        return {"work": work, "commission": commission}

    def create_stopped_recording_take(self, key: str) -> tuple[str, dict]:
        project_response = self.request(
            "post",
            "/api/media/projects",
            json={"title": "播客录音恢复", "kind": "podcast"},
            headers={"Idempotency-Key": f"{key}-project"},
        )
        self.assertEqual(project_response.status_code, 201, project_response.get_data(as_text=True))
        project_id = project_response.json["data"]["id"]
        take_response = self.request(
            "post",
            f"/api/media/projects/{project_id}/recordings",
            json={
                "base_version": 1,
                "cue_id": "intro",
                "speaker_label": "主持人",
                "track_kind": "voiceover",
                "timeline_start_sec": {"num": 0, "den": 1},
                "sample_rate": 48000,
                "channels": 1,
                "sample_format": "pcm_s16le",
            },
            headers={"Idempotency-Key": f"{key}-take"},
        )
        self.assertEqual(take_response.status_code, 201, take_response.get_data(as_text=True))
        take = take_response.json["data"]
        epoch_id = f"{key}-epoch"
        started = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/start",
            json={"capture_epoch_id": epoch_id},
            headers={"Idempotency-Key": f"{key}-start"},
        )
        self.assertEqual(started.status_code, 200, started.get_data(as_text=True))

        recording = io.BytesIO()
        with wave.open(recording, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00\x00" * 4800)
        uploaded = self.upload_data(
            recording.getvalue(), filename=f"{key}-segment.wav", key=f"{key}-upload",
        )
        appended = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/segments",
            json={
                "sequence": 0,
                "material_id": uploaded["material"]["id"],
                "capture_epoch_id": epoch_id,
                "capture_sample_start": 0,
                "sample_count": 4800,
            },
            headers={"Idempotency-Key": f"{key}-segment"},
        )
        self.assertEqual(appended.status_code, 201, appended.get_data(as_text=True))
        stopped = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/stop",
            json={"last_confirmed_sequence": 0, "native_capture_stopped": True},
            headers={"Idempotency-Key": f"{key}-stop"},
        )
        self.assertEqual(stopped.status_code, 200, stopped.get_data(as_text=True))
        return project_id, take

    def reopen_app(self) -> None:
        self.app = create_app(self.data_root, expected_port=8317)
        self.app.config.update(TESTING=True)
        self.store = self.app.extensions["oevira.store"]
        self.client = self.app.test_client()

    def test_material_admission_enforces_commission_scope_across_sources_imports_and_reopen(self) -> None:
        source_scope = self.create_client_work("媒体客户甲", "media-scope-source")
        other_scope = self.create_client_work("媒体客户乙", "media-scope-other")

        sibling_work_response = self.request(
            "post", "/api/works", json={"title": "甲客户第二工作", "goal": "复用同客户素材"},
            headers={"Idempotency-Key": "media-scope-sibling-work"},
        )
        self.assertEqual(sibling_work_response.status_code, 201, sibling_work_response.get_data(as_text=True))
        sibling_work = sibling_work_response.json["data"]
        sibling_assignment = self.request(
            "post",
            f"/api/business/commissions/{source_scope['commission']['id']}/works/{sibling_work['id']}",
            json={}, headers={"Idempotency-Key": "media-scope-sibling-assignment"},
        )
        self.assertEqual(sibling_assignment.status_code, 201, sibling_assignment.get_data(as_text=True))

        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00\x00" * 4800)
        original_audio = audio.getvalue()
        uploaded = self.upload_data(
            original_audio, filename="commission-source.wav", key="media-scope-source-upload",
            work_id=source_scope["work"]["id"],
        )
        material_id = uploaded["material"]["id"]

        def create_project(title: str, key: str, work_id: str | None = None) -> str:
            data = {"title": title, "kind": "podcast"}
            if work_id is not None:
                data["work_id"] = work_id
            created = self.request(
                "post", "/api/media/projects", json=data,
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            return created.json["data"]["id"]

        source_project_id = create_project(
            "甲客户源工程", "media-scope-source-project", source_scope["work"]["id"],
        )
        attached_to_source = self.request(
            "post", f"/api/media/projects/{source_project_id}/sources",
            json={"base_version": 1, "material_id": material_id},
            headers={"Idempotency-Key": "media-scope-source-attach"},
        )
        self.assertEqual(attached_to_source.status_code, 200, attached_to_source.get_data(as_text=True))

        same_commission_project_id = create_project(
            "甲客户复用工程", "media-scope-same-commission-project", sibling_work["id"],
        )
        same_commission_attach = self.request(
            "post", f"/api/media/projects/{same_commission_project_id}/sources",
            json={"base_version": 1, "material_id": material_id},
            headers={"Idempotency-Key": "media-scope-same-commission-attach"},
        )
        self.assertEqual(same_commission_attach.status_code, 200, same_commission_attach.get_data(as_text=True))

        target_project_id = create_project(
            "乙客户目标工程", "media-scope-target-project", other_scope["work"]["id"],
        )
        denied_source = self.request(
            "post", f"/api/media/projects/{target_project_id}/sources",
            json={"base_version": 1, "material_id": material_id},
            headers={"Idempotency-Key": "media-scope-cross-client-source"},
        )
        self.assertEqual(denied_source.status_code, 403, denied_source.get_data(as_text=True))
        self.assertEqual(denied_source.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        target_view = self.request("get", f"/api/media/projects/{target_project_id}")
        self.assertEqual(target_view.status_code, 200, target_view.get_data(as_text=True))
        self.assertEqual(target_view.json["data"]["version"], 1)
        self.assertEqual(
            self.store.row(
                "SELECT COUNT(*) AS count FROM work_materials WHERE work_id = ? AND material_id = ?",
                (other_scope["work"]["id"], material_id),
            )["count"],
            0,
        )

        source_export = self.request(
            "get", f"/api/media/projects/{source_project_id}/export?version=2",
        )
        self.assertEqual(source_export.status_code, 200, source_export.get_data(as_text=True))
        manifest = json.loads(source_export.data)
        existing_projects = self.store.row("SELECT COUNT(*) AS count FROM media_projects")["count"]
        denied_import = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": manifest, "work_id": other_scope["work"]["id"]},
            headers={"Idempotency-Key": "media-scope-cross-client-import"},
        )
        self.assertEqual(denied_import.status_code, 403, denied_import.get_data(as_text=True))
        self.assertEqual(denied_import.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        denied_history_import = self.request(
            "post", "/api/media/projects/import-history",
            json={
                "title": "跨客户历史工程", "kind": "podcast", "work_id": other_scope["work"]["id"],
                "current_source_version_id": "source-v2",
                "versions": [{
                    "source_version_id": "source-v2", "parent_source_version_id": None,
                    "manifest": manifest,
                    "source_origin": {
                        "source_project_id": source_project_id,
                        "source_package_sha256": hashlib.sha256(b"isolated test package").hexdigest(),
                        "source_version": manifest["project"]["version"],
                        "rights_state": "unknown",
                        "publication_state": "not_performed",
                    },
                }],
            },
            headers={"Idempotency-Key": "media-scope-cross-client-history-import"},
        )
        self.assertEqual(denied_history_import.status_code, 403, denied_history_import.get_data(as_text=True))
        self.assertEqual(denied_history_import.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM media_projects")["count"], existing_projects)
        self.assertEqual(
            self.store.row(
                "SELECT COUNT(*) AS count FROM work_materials WHERE work_id = ? AND material_id = ?",
                (other_scope["work"]["id"], material_id),
            )["count"],
            0,
        )

        unscoped_project_id = create_project("未绑定工程", "media-scope-unscoped-project")
        denied_unscoped_source = self.request(
            "post", f"/api/media/projects/{unscoped_project_id}/sources",
            json={"base_version": 1, "material_id": material_id},
            headers={"Idempotency-Key": "media-scope-private-to-unscoped"},
        )
        self.assertEqual(denied_unscoped_source.status_code, 403, denied_unscoped_source.get_data(as_text=True))
        self.assertEqual(denied_unscoped_source.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

        source_state = self.store.row(
            "SELECT state_json FROM media_project_versions WHERE project_id = ? AND version = 2",
            (source_project_id,),
        )
        asset_id = json.loads(source_state["state_json"])["timeline"]["assets"][0]["asset_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE media_project_versions SET state_json = ? WHERE project_id = ? AND workspace_id = ? AND version = 1",
                (source_state["state_json"], target_project_id, self.identity["workspace"]["id"]),
            )
        denied_reopen = self.request("get", f"/api/media/projects/{target_project_id}")
        self.assertEqual(denied_reopen.status_code, 403, denied_reopen.get_data(as_text=True))
        self.assertEqual(denied_reopen.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        denied_source_read = self.request(
            "get", f"/api/media/projects/{target_project_id}/sources/{asset_id}/file",
        )
        self.assertEqual(denied_source_read.status_code, 403, denied_source_read.get_data(as_text=True))
        self.assertEqual(denied_source_read.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

    def test_recording_segments_recheck_material_scope_on_save_view_and_playback(self) -> None:
        source_scope = self.create_client_work("录音客户甲", "media-recording-scope-source")
        other_scope = self.create_client_work("录音客户乙", "media-recording-scope-other")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x01\x00" * 4800)
        original_audio = audio.getvalue()
        material = self.upload_data(
            original_audio, filename="recording-client-source.wav", key="media-recording-source-upload",
            work_id=source_scope["work"]["id"],
        )["material"]

        target_project = self.request(
            "post", "/api/media/projects",
            json={"title": "乙客户播客", "kind": "podcast", "work_id": other_scope["work"]["id"]},
            headers={"Idempotency-Key": "media-recording-target-project"},
        )
        self.assertEqual(target_project.status_code, 201, target_project.get_data(as_text=True))
        target_project_id = target_project.json["data"]["id"]
        target_take = self.request(
            "post", f"/api/media/projects/{target_project_id}/recordings",
            json={"base_version": 1, "cue_id": "intro", "track_kind": "voiceover",
                  "timeline_start_sec": {"num": 0, "den": 1}, "sample_rate": 48000,
                  "channels": 1, "sample_format": "pcm_s16le"},
            headers={"Idempotency-Key": "media-recording-target-take"},
        )
        self.assertEqual(target_take.status_code, 201, target_take.get_data(as_text=True))
        target_take_id = target_take.json["data"]["id"]
        epoch_id = "media-recording-target-epoch"
        started = self.request(
            "post", f"/api/media/recordings/{target_take_id}/start", json={"capture_epoch_id": epoch_id},
            headers={"Idempotency-Key": "media-recording-target-start"},
        )
        self.assertEqual(started.status_code, 200, started.get_data(as_text=True))

        denied_segment = self.request(
            "post", f"/api/media/recordings/{target_take_id}/segments",
            json={"sequence": 0, "material_id": material["id"], "capture_epoch_id": epoch_id,
                  "capture_sample_start": 0, "sample_count": 4800},
            headers={"Idempotency-Key": "media-recording-cross-client-segment"},
        )
        self.assertEqual(denied_segment.status_code, 403, denied_segment.get_data(as_text=True))
        self.assertEqual(denied_segment.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        self.assertEqual(
            self.store.row(
                "SELECT COUNT(*) AS count FROM media_recording_segments WHERE session_id = ?",
                (target_take_id,),
            )["count"],
            0,
        )

        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO media_recording_segments(session_id, workspace_id, sequence, material_id, source_sha256, "
                "audio_stream_index, codec_name, sample_rate, channels, sample_count, capture_epoch_id, "
                "capture_sample_start, timeline_start_num, timeline_start_den, gap_samples_before, clock_relation, "
                "source_start_pts, source_time_base_num, source_time_base_den, created_at) "
                "VALUES(?, ?, 0, ?, ?, 0, 'pcm_s16le', 48000, 1, 4800, ?, 0, 0, 1, 0, 'epoch_anchor', NULL, NULL, NULL, ?)",
                (target_take_id, self.identity["workspace"]["id"], material["id"], material["sha256"],
                 epoch_id, utc_now()),
            )
            connection.execute(
                "UPDATE media_recording_sessions SET latest_sequence = 0, status = 'recording' WHERE id = ?",
                (target_take_id,),
            )
        denied_take_view = self.request("get", f"/api/media/recordings/{target_take_id}")
        self.assertEqual(denied_take_view.status_code, 403, denied_take_view.get_data(as_text=True))
        self.assertEqual(denied_take_view.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")
        denied_segment_playback = self.request(
            "get", f"/api/media/recordings/{target_take_id}/segments/0/file",
        )
        self.assertEqual(denied_segment_playback.status_code, 403, denied_segment_playback.get_data(as_text=True))
        self.assertEqual(denied_segment_playback.json["error"]["code"], "CLIENT_SCOPE_MISMATCH")

        source_project = self.request(
            "post", "/api/media/projects",
            json={"title": "甲客户播客", "kind": "podcast", "work_id": source_scope["work"]["id"]},
            headers={"Idempotency-Key": "media-recording-source-project"},
        )
        self.assertEqual(source_project.status_code, 201, source_project.get_data(as_text=True))
        source_project_id = source_project.json["data"]["id"]
        allowed_take = self.request(
            "post", f"/api/media/projects/{source_project_id}/recordings",
            json={"base_version": 1, "cue_id": "intro", "track_kind": "voiceover",
                  "timeline_start_sec": {"num": 0, "den": 1}, "sample_rate": 48000,
                  "channels": 1, "sample_format": "pcm_s16le"},
            headers={"Idempotency-Key": "media-recording-source-take"},
        )
        self.assertEqual(allowed_take.status_code, 201, allowed_take.get_data(as_text=True))
        source_take_id = allowed_take.json["data"]["id"]
        source_epoch_id = "media-recording-source-epoch"
        source_started = self.request(
            "post", f"/api/media/recordings/{source_take_id}/start",
            json={"capture_epoch_id": source_epoch_id},
            headers={"Idempotency-Key": "media-recording-source-start"},
        )
        self.assertEqual(source_started.status_code, 200, source_started.get_data(as_text=True))
        allowed_segment = self.request(
            "post", f"/api/media/recordings/{source_take_id}/segments",
            json={"sequence": 0, "material_id": material["id"], "capture_epoch_id": source_epoch_id,
                  "capture_sample_start": 0, "sample_count": 4800},
            headers={"Idempotency-Key": "media-recording-source-segment"},
        )
        self.assertEqual(allowed_segment.status_code, 201, allowed_segment.get_data(as_text=True))
        played_segment = self.request(
            "get", f"/api/media/recordings/{source_take_id}/segments/0/file",
        )
        self.assertEqual(played_segment.status_code, 200, played_segment.data[:200])
        self.assertEqual(played_segment.data, original_audio)

    def test_media_render_pause_and_resume_preserve_job_control_and_saved_version(self) -> None:
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00\x02" * 4800)
        original_audio = audio.getvalue()
        material = self.upload_data(
            original_audio, filename="pause-resume-source.wav", key="media-render-pause-upload",
        )["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "渲染续接", "kind": "podcast"},
            headers={"Idempotency-Key": "media-render-pause-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-render-pause-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        edited = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": source.json["data"]["version"], "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
                "timeline_start": {"num": 0, "den": 1},
            }]},
            headers={"Idempotency-Key": "media-render-pause-clip"},
        )
        self.assertEqual(edited.status_code, 200, edited.get_data(as_text=True))
        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": edited.json["data"]["version"], "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48000, "audio_channels": 2}},
            headers={"Idempotency-Key": "media-render-pause-job"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        render_id = render.json["data"]["render_id"]
        job_id = render.json["data"]["job"]["id"]

        entered_render = threading.Event()
        release_render = threading.Event()
        worker_errors = []
        original_job_check = media_api_module._job_cancel_check
        paused_at_checkpoint = [False]

        def pause_after_saved_stage(store, claim, current_project_id):
            checkpoint = store.row(
                "SELECT status FROM media_renders WHERE id = ? AND workspace_id = ?",
                (render_id, claim["workspace_id"]),
            )
            if checkpoint is not None and checkpoint["status"] == "writing" and not paused_at_checkpoint[0]:
                paused_at_checkpoint[0] = True
                entered_render.set()
                if not release_render.wait(timeout=30):
                    raise TimeoutError("The saved render checkpoint was not released")
            return original_job_check(store, claim, current_project_id)

        def run_render_worker() -> None:
            try:
                run_once(self.app, allowed_kinds=("media_render",))
            except BaseException as exc:
                worker_errors.append(type(exc).__name__)

        worker_thread = threading.Thread(target=run_render_worker, daemon=True)
        with patch.object(media_api_module, "_job_cancel_check", side_effect=pause_after_saved_stage):
            worker_thread.start()
            if not entered_render.wait(timeout=30):
                release_render.set()
                worker_thread.join(timeout=30)
                self.fail("The actual Media render did not persist a staged output checkpoint")
            current_job = self.request("get", f"/api/jobs/{job_id}")
            self.assertEqual(current_job.status_code, 200, current_job.get_data(as_text=True))
            paused_request = self.request(
                "post", f"/api/jobs/{job_id}/pause",
                json={"operation_id": "media-render-pause-control",
                      "base_control_revision": current_job.json["data"]["control_revision"]},
            )
            self.assertEqual(paused_request.status_code, 200, paused_request.get_data(as_text=True))
            self.assertEqual(paused_request.json["data"]["status"], "pause_requested")
            release_render.set()
            worker_thread.join(timeout=30)
        self.assertFalse(worker_thread.is_alive(), "The Media render did not stop at its next boundary")
        self.assertEqual(worker_errors, [])
        paused_job = self.store.row("SELECT status, cancel_requested, pause_requested FROM jobs WHERE id = ?", (job_id,))
        self.assertEqual(paused_job["status"], "paused")
        self.assertEqual(paused_job["cancel_requested"], 0)
        self.assertEqual(paused_job["pause_requested"], 1)
        paused_render = self.store.row("SELECT status, staging_path FROM media_renders WHERE id = ?", (render_id,))
        self.assertEqual(paused_render["status"], "interrupted")
        self.assertIsNotNone(paused_render["staging_path"])
        staging_path = self.store.root / Path(paused_render["staging_path"])
        self.assertTrue(staging_path.is_file())
        staged_digest = hashlib.sha256(staging_path.read_bytes()).hexdigest()
        self.assertEqual(staged_digest, self.store.row(
            "SELECT sha256 FROM media_renders WHERE id = ?", (render_id,),
        )["sha256"])

        resumed = self.request(
            "post", f"/api/jobs/{job_id}/resume",
            json={"operation_id": "media-render-resume-control",
                  "base_control_revision": paused_request.json["data"]["control_revision"],
                  "resume_context": {}},
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["status"], "queued")
        with patch.object(media_api_module, "render_timeline", side_effect=AssertionError("saved render checkpoint was rerendered")):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))
        completed_job = self.request("get", f"/api/jobs/{job_id}")
        self.assertEqual(completed_job.json["data"]["status"], "completed", completed_job.json["data"])
        completed_render = self.request("get", f"/api/media/renders/{render_id}")
        self.assertEqual(completed_render.json["data"]["status"], "completed", completed_render.json["data"])
        output = self.request("get", f"/api/media/renders/{render_id}/file")
        self.assertEqual(output.status_code, 200, output.data[:200])
        with wave.open(io.BytesIO(output.data), "rb") as rendered_audio:
            self.assertEqual(rendered_audio.getframerate(), 48000)
            self.assertEqual(rendered_audio.getnchannels(), 2)
            self.assertEqual(rendered_audio.getnframes(), 4800)
        completed_file_sha256 = hashlib.sha256(output.data).hexdigest()
        self.assertEqual(
            self.store.row("SELECT current_version FROM media_projects WHERE id = ?", (project_id,))["current_version"],
            edited.json["data"]["version"],
        )

        # Reconcile a confirmed final render without invoking the renderer again.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'paused', pause_requested = 1, suspended_from = 'running', "
                "control_revision = control_revision + 1 WHERE id = ?",
                (job_id,),
            )
        confirmed_paused_job = self.request("get", f"/api/jobs/{job_id}")
        self.assertEqual(confirmed_paused_job.json["data"]["status"], "paused")
        reconcile = self.request(
            "post", f"/api/jobs/{job_id}/resume",
            json={"operation_id": "media-render-confirmed-output-reconcile",
                  "base_control_revision": confirmed_paused_job.json["data"]["control_revision"],
                  "resume_context": {}},
        )
        self.assertEqual(reconcile.status_code, 200, reconcile.get_data(as_text=True))
        with patch.object(media_api_module, "render_timeline", side_effect=AssertionError("completed output was rerendered")):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))
        reconciled_job = self.request("get", f"/api/jobs/{job_id}")
        self.assertEqual(reconciled_job.json["data"]["status"], "completed")
        reused_file = self.request("get", f"/api/media/renders/{render_id}/file")
        self.assertEqual(reused_file.status_code, 200, reused_file.data[:200])
        self.assertEqual(hashlib.sha256(reused_file.data).hexdigest(), completed_file_sha256)

    def test_asr_analysis_copy_preserves_real_packet_clock_gaps(self) -> None:
        from server.oevira.media_speech import (
            inspect_audio_packet_clock,
            normalize_audio_channel,
            preserve_audio_packet_clock_gaps,
        )

        ffmpeg, ffprobe = _ffmpeg_path(), _ffprobe_path()
        if not ffmpeg or not ffprobe:
            self.skipTest("The approved local media tools are unavailable")
        source_path = self.temp_root / "packet-gap.mka"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-af", "aselect='not(between(t,1,2))'", "-c:a", "pcm_s16le",
                "-f", "matroska", str(source_path),
            ],
            check=True, capture_output=True, timeout=30, shell=False,
        )
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        asset = fingerprint_local_media(str(source_path))
        stream = next(item for item in asset.stream_metadata if item.get("codec_type") == "audio")
        self.assertEqual(stream["sample_rate"], 48000)
        time_base_value = stream.get("time_base")
        time_base = (
            (time_base_value["num"], time_base_value["den"])
            if isinstance(time_base_value, dict) else None
        )
        clock = inspect_audio_packet_clock(
            source_path, audio_stream_index=0, sample_rate=stream["sample_rate"],
            time_base=time_base, stream_start_pts=stream.get("start_pts"),
        )
        self.assertEqual(clock["continuity_state"], "verified_packet_gaps", clock)
        self.assertEqual(clock["gap_count"], 1, clock["gaps"])
        gap = clock["gaps"][0]
        self.assertGreater(gap["normalized_end_sample"] - gap["normalized_start_sample"], 15000)

        working_directory = self.temp_root / "packet-gap-asr"
        working_directory.mkdir()
        normalized, compact_samples, _ffmpeg_version = normalize_audio_channel(
            source_path, audio_stream_index=0, audio_channel_index=0,
            sample_rate=stream["sample_rate"], working_directory=working_directory,
        )
        self.assertEqual(compact_samples, clock["compact_normalized_sample_count"])
        clocked, timeline_samples, mapped_clock = preserve_audio_packet_clock_gaps(
            normalized, compact_samples, clock, working_directory=working_directory,
        )
        self.assertEqual(mapped_clock["normalization_state"], "insert_clock_silence")
        self.assertEqual(timeline_samples, clock["timeline_normalized_sample_count"])
        pcm = array("h")
        pcm.frombytes(clocked.read_bytes())
        self.assertEqual(len(pcm), timeline_samples)
        self.assertTrue(all(sample == 0 for sample in pcm[gap["normalized_start_sample"]:gap["normalized_end_sample"]]))
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_sha256)

    def test_whisper_transcription_maps_words_after_a_real_packet_pts_gap(self) -> None:
        from server.oevira.media_speech import verify_whisper_runtime

        if not _ffmpeg_path() or not _ffprobe_path():
            self.skipTest("The approved local media tools are unavailable")
        verify_whisper_runtime()
        generated = self.request(
            "post", "/api/media/speech/synthesize",
            json={"engine": "ffmpeg_libflite",
                  "text": "The quick brown fox jumps over the lazy dog. " * 6,
                  "voice": "slt", "language": "en"},
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        speech_bytes = base64.b64decode(generated.json["data"]["audio_base64"], validate=True)
        speech_path = self.temp_root / "speech-before-clock-gap.wav"
        speech_path.write_bytes(speech_bytes)
        gap_path = self.temp_root / "speech-with-clock-gap.mka"
        subprocess.run(
            [
                _ffmpeg_path(), "-v", "error", "-nostdin", "-y", "-i", str(speech_path),
                "-af", "aselect='not(between(t,3,5))'", "-c:a", "pcm_s16le",
                "-f", "matroska", str(gap_path),
            ],
            check=True, capture_output=True, timeout=30, shell=False,
        )
        source_sha256 = hashlib.sha256(gap_path.read_bytes()).hexdigest()
        material = self.upload_data(
            gap_path.read_bytes(), filename="speech-with-clock-gap.mka",
            key="whisper-clock-gap-material", chunk_size=1024 * 1024,
        )["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "Discontinuous source clock", "kind": "podcast"},
            headers={"Idempotency-Key": "whisper-clock-gap-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "whisper-clock-gap-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        queued = self.request(
            "post", f"/api/media/projects/{project_id}/transcriptions",
            json={"version": source.json["data"]["version"], "source_asset_id": asset["asset_id"],
                  "audio_stream_index": 0, "audio_channel_index": 0,
                  "language_hint": "en", "task": "transcribe"},
            headers={"Idempotency-Key": "whisper-clock-gap-transcription"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        transcription_id = queued.json["data"]["transcription_id"]
        self.assertTrue(run_once(self.app, allowed_kinds=("media_transcribe",)))
        candidate = self.request("get", f"/api/media/transcriptions/{transcription_id}")
        self.assertEqual(candidate.status_code, 200, candidate.get_data(as_text=True))
        result = candidate.json["data"]
        self.assertEqual(result["job"]["status"], "completed", result)
        packet_clock = result["candidate"]["packet_clock_check"]
        self.assertEqual(packet_clock["continuity_state"], "verified_packet_gaps")
        self.assertEqual(packet_clock["normalization_state"], "insert_clock_silence")
        self.assertEqual(len(packet_clock["gaps"]), 1)
        gap = packet_clock["gaps"][0]
        self.assertTrue(result["candidate"]["words"])
        self.assertTrue(
            any(word["start_sample"] >= gap["source_end_sample"]
                for word in result["candidate"]["words"]),
            result["candidate"]["coverage"],
        )
        reopened_source = self.request(
            "get", f"/api/media/projects/{project_id}/sources/{asset['asset_id']}/file",
        )
        self.assertEqual(reopened_source.status_code, 200, reopened_source.data[:100])
        self.assertEqual(hashlib.sha256(reopened_source.data).hexdigest(), source_sha256)

    def test_chunk_upload_offset_retry_recovery_and_material_readback(self) -> None:
        original = b"synthetic-media-original-with-resume-and-recovery"
        upload = self.start_upload(original, filename="source.bin", key="media-upload-1")
        repeated = self.start_upload(original, filename="source.bin", key="media-upload-1")
        self.assertEqual(repeated["id"], upload["id"])

        first = original[:13]
        headers = {
            "Upload-Offset": "0",
            "X-Chunk-SHA256": hashlib.sha256(first).hexdigest(),
        }
        response = self.request(
            "patch", f"/api/uploads/{upload['id']}", data=first,
            content_type="application/octet-stream", headers=headers,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Upload-Offset"], "13")

        replay = self.request(
            "patch", f"/api/uploads/{upload['id']}", data=first,
            content_type="application/octet-stream", headers=headers,
        )
        self.assertEqual(replay.status_code, 204)
        self.assertEqual(replay.headers["Upload-Offset"], "13")

        wrong_offset = self.request(
            "patch", f"/api/uploads/{upload['id']}", data=original[13:20],
            content_type="application/octet-stream",
            headers={
                "Upload-Offset": "12",
                "X-Chunk-SHA256": hashlib.sha256(original[13:20]).hexdigest(),
            },
        )
        self.assertEqual(wrong_offset.status_code, 409)
        self.assertEqual(wrong_offset.json["error"]["code"], "MEDIA_UPLOAD_OFFSET_CONFLICT")

        row = self.store.row("SELECT stage_relative_path FROM media_uploads WHERE id = ?", (upload["id"],))
        staging = self.data_root.joinpath(*Path(row["stage_relative_path"]).parts)
        with staging.open("ab") as stream:
            stream.write(b"uncommitted tail")
        recover_media_uploads(self.store)
        self.assertEqual(staging.stat().st_size, 13)

        remaining = original[13:]
        resumed = self.request(
            "patch", f"/api/uploads/{upload['id']}", data=remaining,
            content_type="application/octet-stream",
            headers={
                "Upload-Offset": "13",
                "X-Chunk-SHA256": hashlib.sha256(remaining).hexdigest(),
            },
        )
        self.assertEqual(resumed.status_code, 204)
        completed = self.request("post", f"/api/uploads/{upload['id']}/complete")
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        material = completed.json["data"]["material"]
        self.assertEqual(material["status"], "unavailable")
        self.assertEqual(material["sha256"], hashlib.sha256(original).hexdigest())

        app2 = create_app(self.data_root, expected_port=8317)
        app2.config.update(TESTING=True)
        client2 = app2.test_client()
        readback = client2.get(
            f"/api/materials/{material['id']}/file",
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self.token}", "Origin": ORIGIN},
        )
        try:
            self.assertEqual(readback.status_code, 200)
            self.assertEqual(readback.data, original)
        finally:
            readback.close()

    def test_chunk_upload_can_exceed_regular_material_limit_and_reopen_same_material(self) -> None:
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        size = 24 * 1024 * 1024 + 1
        source_path = self.temp_root / "large-recording.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=1",
                "-an", "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-movflags", "+faststart", str(source_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        padding = size - source_path.stat().st_size
        self.assertGreaterEqual(padding, 8)
        with source_path.open("ab") as output:
            output.write(struct.pack(">I4s", padding, b"free"))
            remaining = padding - 8
            block = bytes(1024 * 1024)
            while remaining:
                chunk = block[:min(remaining, len(block))]
                output.write(chunk)
                remaining -= len(chunk)
        original = source_path.read_bytes()
        self.assertEqual(len(original), size)
        saved = self.upload_data(
            original,
            filename=source_path.name,
            key="large-media-upload",
            chunk_size=8 * 1024 * 1024,
        )
        material = saved["material"]
        self.assertEqual(material["size_bytes"], size)
        self.assertEqual(material["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(material["status"], "unavailable")

        stored = self.store.row(
            "SELECT relative_path, status FROM materials WHERE id = ? AND workspace_id = ?",
            (material["id"], self.identity["workspace"]["id"]),
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "unavailable")
        original_path = self.data_root.joinpath(*Path(stored["relative_path"]).parts)
        self.assertEqual(original_path.stat().st_size, size)

        readback = self.request("get", f"/api/materials/{material['id']}/file")
        self.assertEqual(readback.status_code, 200, readback.data[:200])
        self.assertEqual(readback.data, original)

        project = self.request(
            "post", "/api/media/projects", json={"title": "长素材工程", "kind": "video"},
            headers={"Idempotency-Key": "large-media-project-create"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        attached = self.request(
            "post",
            f"/api/media/projects/{project.json['data']['id']}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "large-media-project-source"},
        )
        self.assertEqual(attached.status_code, 200, attached.get_data(as_text=True))
        self.assertEqual(attached.json["data"]["version"], 2)
        self.assertEqual(attached.json["data"]["sources"][0]["sha256"], material["sha256"])
        self.assertEqual(attached.json["data"]["sources"][0]["duration_sec"], {"num": 1, "den": 1})

    def test_hdr_source_is_preserved_when_the_current_video_pipeline_cannot_render_it(self) -> None:
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        source_path = self.temp_root / "hdr-source.mkv"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=1",
                "-vf", "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv",
                "-an", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p10le",
                "-color_range", "tv", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                str(source_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        source_bytes = source_path.read_bytes()
        uploaded = self.upload_data(
            source_bytes, filename=source_path.name, key="hdr-media-upload", chunk_size=2 * 1024 * 1024,
        )
        material = uploaded["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "HDR原片", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "hdr-project-create"},
        )
        project_id = project.json["data"]["id"]
        added = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "hdr-project-source"},
        )
        source = added.json["data"]["sources"][0]
        self.assertEqual(source["streams"][0]["pix_fmt"], "yuv420p10le")
        self.assertEqual(source["streams"][0]["color_transfer"], "smpte2084")
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": source["asset_id"], "track_id": "video-main",
                "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1},
                "audio_role": "muted",
            }]},
            headers={"Idempotency-Key": "hdr-project-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        refused = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": 3, "output_kind": "video", "format": "mp4"},
            headers={"Idempotency-Key": "hdr-render-unsupported"},
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json["error"]["code"], "MEDIA_HDR_RENDER_UNSUPPORTED")
        self.assertIn("HDR", refused.json["error"]["message"])
        self.assertIn("preserved", refused.json["error"]["message"])
        self.assertIsNone(self.store.row(
            "SELECT id FROM jobs WHERE workspace_id = ? AND kind = 'media_render'",
            (self.identity["workspace"]["id"],),
        ))
        preserved = self.request("get", f"/api/materials/{material['id']}/file")
        self.assertEqual(preserved.status_code, 200, preserved.data[:200])
        self.assertEqual(preserved.data, source_bytes)

    def test_audio_bus_routes_survive_undo_redo_preview_and_export(self) -> None:
        if not _ffmpeg_path() or not _ffprobe_path():
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")

        def tone(frequency: int) -> bytes:
            sample_rate = 48000
            samples = array("h", (
                round(7000 * math.sin(2 * math.pi * frequency * index / sample_rate))
                for index in range(sample_rate)
            ))
            output = io.BytesIO()
            with wave.open(output, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(sample_rate)
                stream.writeframes(samples.tobytes())
            return output.getvalue()

        sources = []
        for index, frequency in enumerate((440, 1760), start=1):
            material = self.upload_data(
                tone(frequency), filename=f"tone-{frequency}.wav", key=f"bus-tone-{index}",
            )["material"]
            project = self.request(
                "post", "/api/media/projects", json={"title": "Bus verification", "kind": "podcast"},
                headers={"Idempotency-Key": f"audio-bus-project-{index}"},
            ) if not sources else None
            if project is not None:
                project_id = project.json["data"]["id"]
                version = 1
            added = self.request(
                "post", f"/api/media/projects/{project_id}/sources",
                json={"base_version": version, "material_id": material["id"]},
                headers={"Idempotency-Key": f"audio-bus-source-{index}"},
            )
            self.assertEqual(added.status_code, 200, added.get_data(as_text=True))
            version = added.json["data"]["version"]
            sources.append(added.json["data"]["sources"][-1])

        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": version, "operations": [
                {"op": "add_track", "track_id": "dialog", "kind": "voiceover"},
                {"op": "add_track", "track_id": "music", "kind": "bgm"},
                {"op": "insert", "source_id": sources[0]["asset_id"], "track_id": "dialog",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1},
                 "audio_role": "voiceover", "audio_stream_index": 0},
                {"op": "insert", "source_id": sources[1]["asset_id"], "track_id": "music",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1},
                 "audio_role": "bgm", "audio_stream_index": 0},
            ]},
            headers={"Idempotency-Key": "audio-bus-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        version = inserted.json["data"]["version"]
        before = self.request(
            "post", f"/api/media/projects/{project_id}/audio-previews",
            json={"version": version, "start_sec": {"num": 0, "den": 1}, "end_sec": {"num": 9, "den": 10}},
        )
        self.assertEqual(before.status_code, 200, before.get_data(as_text=True))
        before_hash = before.json["data"]["samples"]["processed"]["sha256"]

        mixed = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": version, "operations": [
                {"op": "set_audio_bus", "bus": {"bus_id": "dialog-bus", "name": "Dialogue",
                 "output_bus_id": "master", "effects": [{"effect_id": "dialog-lowpass", "kind": "lowpass",
                 "parameters": {"frequency_hz": 700, "poles": 4}}]}},
                {"op": "set_audio_bus", "bus": {"bus_id": "music-bus", "name": "Music",
                 "output_bus_id": "master", "gain": 0.6,
                 "effects": [{"effect_id": "music-highpass", "kind": "highpass",
                 "parameters": {"frequency_hz": 250, "poles": 2}}]}},
                {"op": "set_audio_bus", "bus": {"bus_id": "master", "name": "Master",
                 "effects": [{"effect_id": "master-limiter", "kind": "limiter",
                 "parameters": {"limit_dbfs": -1, "attack_ms": 5, "release_ms": 50}}]}},
                {"op": "track_audio_routing", "track_id": "dialog", "gain": 0.8, "pan": -0.5,
                 "sends": [{"bus_id": "dialog-bus", "gain": 1}]},
                {"op": "track_audio_routing", "track_id": "music", "gain": 0.65, "pan": 0.5,
                 "sends": [{"bus_id": "music-bus", "gain": 1}]},
            ]},
            headers={"Idempotency-Key": "audio-bus-mix"},
        )
        self.assertEqual(mixed.status_code, 200, mixed.get_data(as_text=True))
        version = mixed.json["data"]["version"]
        timeline = mixed.json["data"]
        self.assertEqual(len(timeline["audio_buses"]), 3)
        dialog = next(track for track in timeline["tracks"] if track["track_id"] == "dialog")
        self.assertEqual(dialog["gain"], 0.8)
        self.assertEqual(dialog["pan"], -0.5)
        self.assertEqual(dialog["audio_bus_sends"][0]["bus_id"], "dialog-bus")

        undone = self.request(
            "post", f"/api/media/projects/{project_id}/undo", json={"base_version": version},
            headers={"Idempotency-Key": "audio-bus-undo"},
        )
        self.assertEqual(undone.status_code, 200, undone.get_data(as_text=True))
        self.assertEqual(undone.json["data"]["audio_buses"], [])
        redone = self.request(
            "post", f"/api/media/projects/{project_id}/redo",
            json={"base_version": undone.json["data"]["version"]},
            headers={"Idempotency-Key": "audio-bus-redo"},
        )
        self.assertEqual(redone.status_code, 200, redone.get_data(as_text=True))
        version = redone.json["data"]["version"]
        after = self.request(
            "post", f"/api/media/projects/{project_id}/audio-previews",
            json={"version": version, "start_sec": {"num": 0, "den": 1}, "end_sec": {"num": 9, "den": 10}},
        )
        self.assertEqual(after.status_code, 200, after.get_data(as_text=True))
        self.assertNotEqual(before_hash, after.json["data"]["samples"]["processed"]["sha256"])

        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": version, "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48000, "audio_channels": 2}},
            headers={"Idempotency-Key": "audio-bus-render"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}")
        self.assertEqual(job.json["data"]["status"], "completed", job.json["data"])
        result = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}/file")
        self.assertEqual(result.status_code, 200, result.data[:200])
        self.assertEqual(result.headers["Content-Type"], "audio/wav")
        with wave.open(io.BytesIO(result.data), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()),
                             (48000, 2, 3, 48000))

    def test_static_image_has_no_source_duration_and_renders_user_timed_overlay(self) -> None:
        ffmpeg = _ffmpeg_path()
        ffprobe = _ffprobe_path()
        if not ffmpeg or not ffprobe:
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        still_path = self.temp_root / "logo.png"
        background_path = self.temp_root / "blue-background.mp4"
        subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
             "color=c=blue:s=64x64:r=30:d=3",
             "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
             "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
             "-color_trc", "bt709", str(background_path)],
            check=True, capture_output=True, timeout=30, shell=False,
        )

        def png_chunk(kind: bytes, payload: bytes) -> bytes:
            return (struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

        scanlines = bytearray()
        for y in range(64):
            scanlines.append(0)
            for x in range(64):
                alpha = 255 if x < 20 and y < 20 else 128 if x < 40 and y < 20 else 0
                scanlines.extend((255, 0, 0, alpha))
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0))
            + png_chunk(b"sRGB", b"\x00")
            + png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
            + png_chunk(b"IEND", b"")
        )
        still_path.write_bytes(png_bytes)
        material = self.upload_data(
            still_path.read_bytes(), filename=still_path.name, key="static-image-upload",
        )["material"]
        background = self.upload_data(
            background_path.read_bytes(), filename=background_path.name, key="static-image-background-upload",
        )["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "静态图视频合成", "kind": "video"},
            headers={"Idempotency-Key": "static-image-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        background_added = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": background["id"]},
            headers={"Idempotency-Key": "static-image-background-source"},
        )
        self.assertEqual(background_added.status_code, 200, background_added.get_data(as_text=True))
        image_added = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": background_added.json["data"]["version"], "material_id": material["id"]},
            headers={"Idempotency-Key": "static-image-source"},
        )
        self.assertEqual(image_added.status_code, 200, image_added.get_data(as_text=True))
        source = next(item for item in image_added.json["data"]["sources"] if item["media_kind"] == "image")
        background_source = next(item for item in image_added.json["data"]["sources"]
                                 if item["asset_id"] != source["asset_id"])
        self.assertEqual(source["media_kind"], "image")
        self.assertIsNone(source["duration_sec"])
        self.assertEqual(source["duration_basis"], "none")

        no_duration = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 3, "operations": [{
                "op": "insert", "source_id": source["asset_id"], "track_id": "video-overlay",
                "audio_role": "muted", "timeline_start": {"num": 0, "den": 1},
            }]},
            headers={"Idempotency-Key": "static-image-duration-required"},
        )
        self.assertEqual(no_duration.status_code, 400, no_duration.get_data(as_text=True))
        self.assertEqual(no_duration.json["error"]["code"], "STILL_IMAGE_DISPLAY_DURATION_REQUIRED")

        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 3, "operations": [
              {
                "op": "insert", "source_id": background_source["asset_id"], "track_id": "video-main",
                "source_in": {"num": 0, "den": 1}, "source_out": {"num": 9, "den": 4},
                "audio_role": "muted",
              },
              {
                "op": "insert", "source_id": source["asset_id"], "track_id": "video-overlay",
                "source_in": {"num": 0, "den": 1}, "source_out": {"num": 0, "den": 1},
                "display_duration_sec": {"num": 3, "den": 2},
                "audio_role": "muted", "timeline_start": {"num": 0, "den": 1},
              },
            ]},
            headers={"Idempotency-Key": "static-image-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        clip = inserted.json["data"]["tracks"][1]["clips"][0]
        self.assertEqual(clip["source_in"], {"num": 0, "den": 1})
        self.assertEqual(clip["source_out"], {"num": 0, "den": 1})
        self.assertEqual(clip["display_duration_sec"], {"num": 3, "den": 2})

        adjusted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": inserted.json["data"]["version"], "operations": [{
                "op": "set_display_duration", "clip_id": clip["clip_id"],
                "display_duration_sec": {"num": 9, "den": 4},
            }]},
            headers={"Idempotency-Key": "static-image-adjust-duration"},
        )
        self.assertEqual(adjusted.status_code, 200, adjusted.get_data(as_text=True))
        undone = self.request(
            "post", f"/api/media/projects/{project_id}/undo",
            json={"base_version": adjusted.json["data"]["version"]},
            headers={"Idempotency-Key": "static-image-undo-duration"},
        )
        self.assertEqual(undone.status_code, 200, undone.get_data(as_text=True))
        redone = self.request(
            "post", f"/api/media/projects/{project_id}/redo",
            json={"base_version": undone.json["data"]["version"]},
            headers={"Idempotency-Key": "static-image-redo-duration"},
        )
        self.assertEqual(redone.status_code, 200, redone.get_data(as_text=True))
        render_version = redone.json["data"]["version"]
        reopened = self.request("get", f"/api/media/projects/{project_id}?version={render_version}")
        reopened_overlay = next(track for track in reopened.json["data"]["tracks"]
                                if track["track_id"] == "video-overlay")
        self.assertEqual(reopened_overlay["clips"][0]["display_duration_sec"], {"num": 9, "den": 4})
        exported = self.request("get", f"/api/media/projects/{project_id}/export?version={render_version}")
        self.assertEqual(exported.status_code, 200, exported.get_data(as_text=True))
        manifest = json.loads(exported.data)
        image_asset = next(asset for asset in manifest["timeline"]["assets"] if asset["media_kind"] == "image")
        self.assertIsNone(image_asset["duration_sec"])
        self.assertEqual(manifest["timeline"]["tracks"][1]["clips"][0]["display_duration_sec"],
                         {"num": 9, "den": 4})
        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": render_version, "output_kind": "video", "format": "mp4"},
            headers={"Idempotency-Key": "static-image-export"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}")
        self.assertEqual(job.json["data"]["status"], "completed", job.json["data"])
        image_color = job.json["data"]["metadata"]["render"]["video_signal_processing"]
        image_profile = next(item for item in image_color["source_profiles"]
                             if item.get("profile_evidence") is not None)
        self.assertEqual(image_profile["profile_evidence"], "srgb_tagged")
        self.assertEqual(image_color["output_profile"]["color_space"], "bt709")
        self.assertIn("sRGB_stills_converted", image_color["transform"])
        output = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}/file")
        self.assertEqual(output.status_code, 200, output.data[:200])
        output_path = self.temp_root / "rendered-logo.mp4"
        output_path.write_bytes(output.data)
        decoded = subprocess.run(
            [ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type,nb_read_frames,width,height", "-of", "json", str(output_path)],
            check=True, capture_output=True, text=True, timeout=30, shell=False,
        )
        video_stream = json.loads(decoded.stdout)["streams"][0]
        self.assertGreaterEqual(int(video_stream["nb_read_frames"]), 60)
        self.assertEqual((video_stream["width"], video_stream["height"]), (1280, 720))
        rgba = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", str(output_path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True, timeout=30, shell=False,
        ).stdout
        transparent_pixel = rgba[(100 * 1280 + 800) * 3:(100 * 1280 + 800) * 3 + 3]
        half_pixel = rgba[(100 * 1280 + 620) * 3:(100 * 1280 + 620) * 3 + 3]
        opaque_pixel = rgba[(100 * 1280 + 400) * 3:(100 * 1280 + 400) * 3 + 3]
        self.assertGreater(transparent_pixel[2], 170)
        self.assertLess(transparent_pixel[0], 80)
        self.assertGreater(half_pixel[0], 70)
        self.assertGreater(half_pixel[2], 70)
        self.assertGreater(opaque_pixel[0], 180)
        self.assertLess(opaque_pixel[2], 80)

        imported = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": manifest, "title": "Reopened static image"},
            headers={"Idempotency-Key": "static-image-import"},
        )
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        imported_view = self.request("get", f"/api/media/projects/{imported.json['data']['id']}")
        self.assertEqual(imported_view.status_code, 200, imported_view.get_data(as_text=True))
        imported_image = next(asset for asset in imported_view.json["data"]["sources"]
                              if asset["media_kind"] == "image")
        self.assertIsNone(imported_image["duration_sec"])
        imported_overlay = next(track for track in imported_view.json["data"]["tracks"]
                                if track["track_id"] == "video-overlay")
        self.assertEqual(imported_overlay["clips"][0]["display_duration_sec"], {"num": 9, "den": 4})

    def test_unknown_sdr_color_requires_versioned_user_interpretation_and_converts_to_working_space(self) -> None:
        ffmpeg = _ffmpeg_path()
        ffprobe = _ffprobe_path()
        if not ffmpeg or not ffprobe:
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        source_path = self.temp_root / "untagged-sdr.mp4"
        subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
             "color=c=red:s=64x64:r=24:d=1",
             "-vf", "format=yuv420p,setparams=color_primaries=unknown:color_trc=unknown:colorspace=unknown:range=unknown",
             "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-color_range", "unknown", "-colorspace", "unknown", "-color_primaries", "unknown",
             "-color_trc", "unknown", "-movflags", "+faststart", str(source_path)],
            check=True, capture_output=True, timeout=30, shell=False,
        )
        material = self.upload_data(
            source_path.read_bytes(), filename=source_path.name, key="unknown-color-upload",
        )["material"]
        created = self.request(
            "post", "/api/media/projects", json={"title": "Unknown SDR interpretation", "kind": "video"},
            headers={"Idempotency-Key": "unknown-color-project"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        project_id = created.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "unknown-color-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        stream = next(item for item in asset["streams"] if item["codec_type"] == "video")
        self.assertEqual(stream["pix_fmt"], "yuv420p")
        self.assertTrue(all(stream.get(key) in (None, "unknown", "unspecified") for key in
                            ("color_range", "color_space", "color_transfer", "color_primaries")))
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "video-main",
                "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1},
                "audio_role": "muted",
            }]},
            headers={"Idempotency-Key": "unknown-color-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        unsupported = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": inserted.json["data"]["version"], "output_kind": "video", "format": "mp4"},
            headers={"Idempotency-Key": "unknown-color-render-refused"},
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.json["error"]["code"], "MEDIA_COLOR_SIGNAL_UNKNOWN")

        interpreted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": inserted.json["data"]["version"], "operations": [{
                "op": "set_video_color_interpretation",
                "interpretation": {
                    "asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
                    "video_stream_index": 0,
                    "input_profile": {"color_range": "tv", "color_space": "smpte170m",
                                      "color_transfer": "smpte170m", "color_primaries": "smpte170m"},
                    "working_profile": {"color_range": "tv", "color_space": "bt709",
                                         "color_transfer": "bt709", "color_primaries": "bt709"},
                    "selected_by": "user_selected",
                },
            }]},
            headers={"Idempotency-Key": "unknown-color-interpretation"},
        )
        self.assertEqual(interpreted.status_code, 200, interpreted.get_data(as_text=True))
        interpreted_version = interpreted.json["data"]["version"]
        reopened = self.request("get", f"/api/media/projects/{project_id}?version={interpreted_version}")
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["video_color_interpretations"][0]["selected_by"], "user_selected")
        stored_stream = next(item for item in reopened.json["data"]["sources"][0]["streams"]
                             if item["codec_type"] == "video")
        self.assertTrue(all(stored_stream.get(key) in (None, "unknown", "unspecified") for key in
                            ("color_range", "color_space", "color_transfer", "color_primaries")))
        undone = self.request(
            "post", f"/api/media/projects/{project_id}/undo", json={"base_version": interpreted_version},
            headers={"Idempotency-Key": "unknown-color-undo"},
        )
        self.assertEqual(undone.status_code, 200, undone.get_data(as_text=True))
        self.assertEqual(undone.json["data"]["video_color_interpretations"], [])
        redone = self.request(
            "post", f"/api/media/projects/{project_id}/redo",
            json={"base_version": undone.json["data"]["version"]},
            headers={"Idempotency-Key": "unknown-color-redo"},
        )
        self.assertEqual(redone.status_code, 200, redone.get_data(as_text=True))
        interpreted_version = redone.json["data"]["version"]

        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": interpreted_version, "output_kind": "video", "format": "mp4"},
            headers={"Idempotency-Key": "unknown-color-render-interpreted"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}")
        self.assertEqual(job.json["data"]["status"], "completed", job.json["data"])
        signal = job.json["data"]["metadata"]["render"]["video_signal_processing"]
        self.assertEqual(signal["output_profile"]["color_space"], "bt709")
        source_profile = signal["source_profiles"][0]
        self.assertEqual(source_profile["observed_source_signal"]["color_space"], None)
        self.assertEqual(source_profile["interpretation"]["input_profile"]["color_space"], "smpte170m")
        self.assertTrue(source_profile["transform_required"])
        first_render = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}/file")
        self.assertEqual(first_render.status_code, 200, first_render.data[:200])
        first_render_path = self.temp_root / "interpreted-smpte170m.mp4"
        first_render_path.write_bytes(first_render.data)

        second_interpretation = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": interpreted_version, "operations": [{
                "op": "set_video_color_interpretation",
                "interpretation": {
                    "asset_id": asset["asset_id"], "source_sha256": asset["sha256"],
                    "video_stream_index": 0,
                    "input_profile": {"color_range": "tv", "color_space": "bt709",
                                      "color_transfer": "bt709", "color_primaries": "bt709"},
                    "working_profile": {"color_range": "tv", "color_space": "bt709",
                                         "color_transfer": "bt709", "color_primaries": "bt709"},
                    "selected_by": "user_selected",
                },
            }]},
            headers={"Idempotency-Key": "unknown-color-alternative-interpretation"},
        )
        self.assertEqual(second_interpretation.status_code, 200, second_interpretation.get_data(as_text=True))
        second_version = second_interpretation.json["data"]["version"]
        alternate_render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": second_version, "output_kind": "video", "format": "mp4"},
            headers={"Idempotency-Key": "unknown-color-render-alternate"},
        )
        self.assertEqual(alternate_render.status_code, 202, alternate_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        alternate_job = self.request(
            "get", f"/api/media/renders/{alternate_render.json['data']['render_id']}"
        )
        self.assertEqual(alternate_job.json["data"]["status"], "completed", alternate_job.json["data"])
        second_file = self.request(
            "get", f"/api/media/renders/{alternate_render.json['data']['render_id']}/file"
        )
        self.assertEqual(second_file.status_code, 200, second_file.data[:200])
        second_render_path = self.temp_root / "interpreted-bt709.mp4"
        second_render_path.write_bytes(second_file.data)
        def first_frame(path: Path) -> bytes:
            return subprocess.run(
                [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                check=True, capture_output=True, timeout=30, shell=False,
            ).stdout
        self.assertNotEqual(first_frame(first_render_path), first_frame(second_render_path))

        manifest_response = self.request("get", f"/api/media/projects/{project_id}/export?version={interpreted_version}")
        self.assertEqual(manifest_response.status_code, 200)
        manifest = json.loads(manifest_response.data)
        self.assertEqual(manifest["timeline"]["video_color_interpretations"][0]["selected_by"], "user_selected")

    def test_transcript_sample_alignment_word_cut_preview_and_saved_audio_render(self) -> None:
        ffmpeg = _ffmpeg_path()
        if not ffmpeg or not _ffprobe_path():
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")

        sample_rate = 48000
        pcm = array("h", (
            round(7000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            for index in range(sample_rate * 2)
        ))
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(pcm.tobytes())
        material = self.upload_data(
            stream.getvalue(), filename="timed-voice.wav", key="transcript-word-cut-source",
        )["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "Sample-clock word cut", "kind": "podcast"},
            headers={"Idempotency-Key": "transcript-word-cut-project"},
        )
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "transcript-word-cut-source-bind"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [
                {"op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 2, "den": 1},
                 "audio_role": "voiceover", "audio_stream_index": 0,
                 "timeline_start": {"num": 0, "den": 1}},
                {"op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 2, "den": 1},
                 "audio_role": "voiceover", "audio_stream_index": 0,
                 "timeline_start": {"num": 2, "den": 1}},
            ]},
            headers={"Idempotency-Key": "transcript-word-cut-clip"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        clip_id, repeated_clip_id = [item["clip_id"]
                                     for item in inserted.json["data"]["tracks"][0]["clips"]]
        transcript = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": inserted.json["data"]["version"], "operations": [{
                "op": "set_transcript",
                "transcript": {
                    "source_asset_id": asset["asset_id"], "audio_stream_index": 0,
                    "language": "en", "alignment_source": "human_supplied_unverified",
                    "words": [
                        {"word_id": "segment-0:word-0", "text": "remove", "start_sample": 24000,
                         "end_sample": 28800},
                        {"word_id": "segment-0:word-1", "text": "keep", "start_sample": 48000,
                         "end_sample": 52800},
                    ],
                },
            }]},
            headers={"Idempotency-Key": "transcript-word-cut-import"},
        )
        self.assertEqual(transcript.status_code, 200, transcript.get_data(as_text=True))
        base_version = transcript.json["data"]["version"]
        readback = self.request(
            "get", f"/api/media/projects/{project_id}/transcripts?version={base_version}"
            f"&source_asset_id={asset['asset_id']}",
        )
        self.assertEqual(readback.status_code, 200, readback.get_data(as_text=True))
        self.assertEqual(readback.json["data"]["word_count"], 4)
        remove_word = next(word for word in readback.json["data"]["words"]
                           if word["word_id"] == "segment-0:word-0" and word["clip_id"] == clip_id)
        self.assertEqual(remove_word["source_start_sample"], 24000)
        self.assertEqual(remove_word["source_end_sample"], 28800)
        self.assertEqual(remove_word["clip_id"], clip_id)
        transcript_hash = remove_word["transcript_sha256"]

        candidate = self.request(
            "post", f"/api/media/projects/{project_id}/word-cut-previews",
            json={
                "version": base_version,
                "source_selections": [{
                    "transcript_asset_id": asset["asset_id"],
                    "expected_transcript_sha256": transcript_hash,
                    "word_ids": ["segment-0:word-0"], "clip_ids": [clip_id],
                }],
                "synchronize": True,
                "context_before_sec": {"num": 1, "den": 5},
                "context_after_sec": {"num": 1, "den": 5},
            },
        )
        self.assertEqual(candidate.status_code, 200, candidate.get_data(as_text=True))
        self.assertFalse(candidate.json["data"]["persisted"])
        self.assertEqual(candidate.json["data"]["transcript_edit"]["parent_timeline_version"], base_version)
        audio_ab = candidate.json["data"]["audio_comparison"]["samples"]
        self.assertNotEqual(audio_ab["base"]["sha256"], audio_ab["candidate"]["sha256"])
        self.assertEqual(audio_ab["base"]["duration_sec"], {"num": 1, "den": 2})
        self.assertEqual(audio_ab["candidate"]["duration_sec"], {"num": 2, "den": 5})
        current = self.request("get", f"/api/media/projects/{project_id}")
        self.assertEqual(current.json["data"]["version"], base_version)

        adopted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": base_version, "operations": [{
                "op": "sync_cut",
                "cuts": candidate.json["data"]["transcript_edit"]["cuts"],
                "synchronize": True,
            }]},
            headers={"Idempotency-Key": "transcript-word-cut-adopt"},
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        surviving_words = self.request(
            "get", f"/api/media/projects/{project_id}/transcripts?version={adopted.json['data']['version']}"
            f"&source_asset_id={asset['asset_id']}",
        )
        removed_instances = [word for word in surviving_words.json["data"]["words"]
                             if word["word_id"] == "segment-0:word-0" and word.get("clip_id") is not None]
        self.assertEqual(len(removed_instances), 1)
        self.assertEqual(removed_instances[0]["clip_id"], repeated_clip_id)
        retained_instances = [word for word in surviving_words.json["data"]["words"]
                              if word["word_id"] == "segment-0:word-1" and word.get("clip_id") is not None]
        self.assertEqual(len(retained_instances), 2)
        retained_clip_ids = {word["clip_id"] for word in retained_instances}
        self.assertIn(repeated_clip_id, retained_clip_ids)
        self.assertTrue(any(item.startswith(f"{clip_id}-w") for item in retained_clip_ids))

        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": adopted.json["data"]["version"], "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48000, "audio_channels": 2}},
            headers={"Idempotency-Key": "transcript-word-cut-render"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        job = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}")
        self.assertEqual(job.json["data"]["status"], "completed", job.json["data"])
        final_file = self.request("get", f"/api/media/renders/{render.json['data']['render_id']}/file")
        self.assertEqual(final_file.status_code, 200, final_file.data[:200])
        with wave.open(io.BytesIO(final_file.data), "rb") as output_audio:
            self.assertEqual(output_audio.getframerate(), 48000)
            self.assertEqual(output_audio.getnchannels(), 2)
            self.assertEqual(output_audio.getnframes(), 187200)

    def test_local_speech_material_whisper_candidate_review_word_cut_and_render(self) -> None:
        from server.oevira.media_speech import _SPEECH_MODEL, verify_whisper_runtime

        if not _SPEECH_MODEL.is_file() or not _ffmpeg_path() or not _ffprobe_path():
            self.skipTest("The approved local Whisper CPU runtime and media tools are not installed")
        runtime = verify_whisper_runtime()
        self.assertEqual(runtime["engine"], "openai-whisper")
        self.assertEqual(runtime["model"], "base")
        self.assertEqual(runtime["device"], "cpu")

        capabilities = self.request("get", "/api/media/speech/capabilities")
        self.assertEqual(capabilities.status_code, 200, capabilities.get_data(as_text=True))
        self.assertTrue(capabilities.json["data"]["synthesis"][0]["available"])
        self.assertIn("slt", capabilities.json["data"]["synthesis"][0]["voices"])
        self.assertTrue(capabilities.json["data"]["recognition"][0]["available"])

        project = self.request(
            "post", "/api/media/projects", json={"title": "Local speech source and transcript", "kind": "podcast"},
            headers={"Idempotency-Key": "local-speech-source-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        generated = self.request(
            "post", "/api/media/speech/synthesize",
            json={"text": "The quick brown fox jumps over the lazy dog.", "voice": "slt", "language": "en"},
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        speech_report = generated.json["data"]["report"]
        self.assertTrue(speech_report["synthetic"])
        self.assertFalse(speech_report["voice_cloned"])
        self.assertEqual(speech_report["naturalness"], "NOT_EVALUATED")
        source_bytes = base64.b64decode(generated.json["data"]["audio_base64"], validate=True)
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), speech_report["audio"]["wav_sha256"])
        uploaded = self.upload_data(
            source_bytes, filename="media-speech-verified.wav", key="local-speech-material-upload",
        )
        material = uploaded["material"]
        project_source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "local-speech-source-bind"},
        )
        self.assertEqual(project_source.status_code, 200, project_source.get_data(as_text=True))
        asset = project_source.json["data"]["sources"][0]
        self.assertEqual(asset["source_material_id"], material["id"])
        self.assertEqual(asset["audio_stream_count"], 1)
        self.assertEqual(asset["speech_source"]["engine"], "ffmpeg_libflite")
        self.assertEqual(asset["speech_source"]["text"], "The quick brown fox jumps over the lazy dog.")
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": project_source.json["data"]["version"], "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
                "timeline_start": {"num": 0, "den": 1},
            }]},
            headers={"Idempotency-Key": "local-speech-source-clip"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        clip_id = inserted.json["data"]["tracks"][0]["clips"][0]["clip_id"]
        version = inserted.json["data"]["version"]
        queued = self.request(
            "post", f"/api/media/projects/{project_id}/transcriptions",
            json={"version": version, "source_asset_id": asset["asset_id"],
                  "audio_stream_index": 0, "audio_channel_index": 0,
                  "language_hint": "en", "task": "transcribe"},
            headers={"Idempotency-Key": "local-speech-asr-job"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        transcription_id = queued.json["data"]["transcription_id"]
        self.assertTrue(run_once(self.app, allowed_kinds=("media_transcribe",)))
        candidate = self.request("get", f"/api/media/transcriptions/{transcription_id}")
        self.assertEqual(candidate.status_code, 200, candidate.get_data(as_text=True))
        self.assertEqual(candidate.json["data"]["job"]["status"], "completed", candidate.json["data"])
        transcript_candidate = candidate.json["data"]["candidate"]
        self.assertGreaterEqual(transcript_candidate["word_count"], 8)
        self.assertIn("quick brown fox", transcript_candidate["text"].lower())
        self.assertEqual(transcript_candidate["model"]["version"], "20250625")
        self.assertEqual(transcript_candidate["model"]["quality_state"], "NOT_EVALUATED")
        self.assertEqual(transcript_candidate["review_state"], "unreviewed")

        fox_word = next(word for word in transcript_candidate["words"] if "fox" in word["text"].lower())
        fox_word_id = fox_word["word_id"]
        adopted = self.request(
            "post", f"/api/media/projects/{project_id}/transcriptions/{transcription_id}/adopt",
            json={"base_version": version,
                  "word_corrections": [{"word_id": fox_word_id, "text": " wolf"}],
                  "review_state": "human_corrected"},
            headers={"Idempotency-Key": "local-speech-asr-adopt"},
        )
        self.assertEqual(adopted.status_code, 200, adopted.get_data(as_text=True))
        transcript_version = adopted.json["data"]["project"]["version"]
        transcript_readback = self.request(
            "get", f"/api/media/projects/{project_id}/transcripts?version={transcript_version}"
            f"&source_asset_id={asset['asset_id']}",
        )
        self.assertEqual(transcript_readback.status_code, 200, transcript_readback.get_data(as_text=True))
        transcript_doc = transcript_readback.json["data"]["transcripts"][0]
        self.assertEqual(transcript_doc["alignment_source"], "whisper_base_unreviewed")
        self.assertEqual(transcript_doc["review_state"], "human_corrected")
        self.assertEqual(transcript_doc["model_provenance"]["quality_state"], "NOT_EVALUATED")
        mapped_fox = next(word for word in transcript_readback.json["data"]["words"]
                          if word["word_id"] == fox_word_id and word["clip_id"] == clip_id)
        self.assertEqual(mapped_fox["text"], " wolf")
        cut_preview = self.request(
            "post", f"/api/media/projects/{project_id}/word-cut-previews",
            json={"version": transcript_version, "source_selections": [{
                "transcript_asset_id": asset["asset_id"],
                "expected_transcript_sha256": mapped_fox["transcript_sha256"],
                "word_ids": [fox_word_id], "clip_ids": [clip_id],
            }], "synchronize": True,
                  "context_before_sec": {"num": 1, "den": 5},
                  "context_after_sec": {"num": 1, "den": 5}},
        )
        self.assertEqual(cut_preview.status_code, 200, cut_preview.get_data(as_text=True))
        samples = cut_preview.json["data"]["audio_comparison"]["samples"]
        self.assertNotEqual(samples["base"]["sha256"], samples["candidate"]["sha256"])
        self.assertFalse(cut_preview.json["data"]["persisted"])
        adopted_cut = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": transcript_version, "operations": [{
                "op": "sync_cut",
                "cuts": cut_preview.json["data"]["transcript_edit"]["cuts"],
                "synchronize": True,
            }]},
            headers={"Idempotency-Key": "local-speech-word-cut"},
        )
        self.assertEqual(adopted_cut.status_code, 200, adopted_cut.get_data(as_text=True))
        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": adopted_cut.json["data"]["version"], "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 16000, "audio_channels": 2}},
            headers={"Idempotency-Key": "local-speech-final-render"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))
        render_id = render.json["data"]["render_id"]
        render_status = self.request("get", f"/api/media/renders/{render_id}")
        self.assertEqual(render_status.json["data"]["status"], "completed", render_status.json["data"])
        output = self.request("get", f"/api/media/renders/{render_id}/file")
        self.assertEqual(output.status_code, 200, output.data[:100])
        with wave.open(io.BytesIO(output.data), "rb") as final_audio:
            self.assertEqual(final_audio.getframerate(), 16000)
            self.assertEqual(final_audio.getnchannels(), 2)
            self.assertLess(final_audio.getnframes(), speech_report["audio"]["samples_per_channel"])

    def test_espeak_ng_chinese_tts_is_saved_as_a_hash_bound_media_material(self) -> None:
        from server.oevira.media_speech import _read_tts_source, verify_espeak_runtime

        runtime = verify_espeak_runtime()
        self.assertEqual(runtime["version"], "1.52.0")
        self.assertEqual(runtime["license"], "GPL-3.0-or-later")
        capabilities = self.request("get", "/api/media/speech/capabilities")
        self.assertEqual(capabilities.status_code, 200, capabilities.get_data(as_text=True))
        espeak = next(item for item in capabilities.json["data"]["synthesis"]
                      if item["engine"] == "espeak_ng")
        self.assertTrue(espeak["available"])
        self.assertEqual(espeak["language_voice_map"]["zh-CN"], "cmn")
        self.assertEqual(espeak["naturalness"], "NOT_EVALUATED_EXPECT_MECHANICAL_VOICE")

        project = self.request(
            "post", "/api/media/projects",
            json={"title": "Local multilingual voiceover source", "kind": "podcast"},
            headers={"Idempotency-Key": "espeak-chinese-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        generated = self.request(
            "post", "/api/media/speech/synthesize",
            json={"engine": "espeak_ng", "text": "你好，欢迎使用寰意。",
                  "voice": "cmn", "language": "zh-CN"},
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        report = generated.json["data"]["report"]
        self.assertEqual(report["runtime"]["engine_version"], "1.52.0")
        self.assertTrue(report["runtime"]["engine_license"].startswith("GPL-3.0-or-later"))
        self.assertEqual(report["voice"], "cmn")
        self.assertFalse(report["voice_cloned"])
        self.assertEqual(report["naturalness"], "NOT_EVALUATED_EXPECT_MECHANICAL_VOICE")
        audio = base64.b64decode(generated.json["data"]["audio_base64"], validate=True)
        self.assertEqual(hashlib.sha256(audio).hexdigest(), report["audio"]["wav_sha256"])
        declaration = _read_tts_source(audio)
        self.assertEqual(declaration["text"], "你好，欢迎使用寰意。")
        self.assertEqual(declaration["voice"], "cmn")
        self.assertEqual(declaration["engine_sha256"], report["runtime"]["engine_sha256"])
        with wave.open(io.BytesIO(audio), "rb") as source_audio:
            self.assertEqual(source_audio.getframerate(), 16000)
            self.assertEqual(source_audio.getnchannels(), 1)
            self.assertGreater(source_audio.getnframes(), 0)

        material = self.upload_data(
            audio, filename="media-speech-zh-CN.wav", key="espeak-chinese-material",
        )["material"]
        source = self.request(
            "post", f"/api/media/projects/{project.json['data']['id']}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "espeak-chinese-source-bind"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        self.assertEqual(asset["sha256"], material["sha256"])
        self.assertEqual(asset["speech_source"]["engine"], "espeak_ng")
        self.assertEqual(asset["speech_source"]["language"], "zh-CN")
        self.assertEqual(asset["speech_source"]["text"], "你好，欢迎使用寰意。")
        original = self.request(
            "get", f"/api/media/projects/{project.json['data']['id']}/sources/{asset['asset_id']}/file"
        )
        self.assertEqual(original.status_code, 200, original.data[:100])
        self.assertEqual(hashlib.sha256(original.data).hexdigest(), asset["sha256"])
        self.assertEqual(_read_tts_source(original.data), declaration)

    def test_espeak_ng_local_synthesis_covers_the_twelve_creative_languages(self) -> None:
        from server.oevira.media_speech import verify_espeak_runtime

        runtime = verify_espeak_runtime()
        self.assertEqual(runtime["version"], "1.52.0")
        samples = (
            ("zh-CN", "cmn", "你好，欢迎使用寰意。"),
            ("en", "en-us", "This is a local synthetic voice."),
            ("ko", "ko", "안녕하세요. 환영합니다."),
            ("ja", "ja", "こんにちは。"),
            ("fr", "fr-fr", "Bonjour, bienvenue."),
            ("de", "de", "Guten Tag, willkommen."),
            ("es", "es", "Hola, bienvenidos."),
            ("pt-BR", "pt-br", "Olá, sejam bem-vindos."),
            ("ru", "ru", "Здравствуйте, добро пожаловать."),
            ("ar", "ar", "مَرْحَبًا بِكَ."),
            ("hi", "hi", "नमस्ते, आपका स्वागत है।"),
            ("id", "id", "Selamat datang."),
        )
        for index, (language, voice, source_text) in enumerate(samples):
            with self.subTest(language=language):
                response = self.request(
                    "post", "/api/media/speech/synthesize",
                    json={"engine": "espeak_ng", "text": source_text,
                          "voice": voice, "language": language},
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                result = response.json["data"]
                self.assertFalse(result["material_created"])
                self.assertFalse(result["project_changed"])
                audio = base64.b64decode(result["audio_base64"], validate=True)
                with wave.open(io.BytesIO(audio), "rb") as voice_audio:
                    self.assertEqual(voice_audio.getframerate(), 16000)
                    self.assertEqual(voice_audio.getnchannels(), 1)
                    self.assertGreater(voice_audio.getnframes(), 0)
                self.assertEqual(result["report"]["engine"], "espeak_ng")
                self.assertEqual(result["report"]["language"], language)
                self.assertEqual(result["report"]["voice"], voice)
                self.assertEqual(result["report"]["source"]["text"], source_text)
                self.assertEqual(result["report"]["audio"]["full_pcm_comparison"], "passed")
                self.assertEqual(result["report"]["naturalness"], "NOT_EVALUATED_EXPECT_MECHANICAL_VOICE")

    def test_whisper_partial_audio_chunk_survives_cancel_and_resumes(self) -> None:
        from server.oevira.media_speech import verify_whisper_runtime

        if not _ffmpeg_path() or not _ffprobe_path():
            self.skipTest("The approved local media tools are unavailable")
        verify_whisper_runtime()
        project = self.request(
            "post", "/api/media/projects",
            json={"title": "Whisper chunk resume", "kind": "podcast"},
            headers={"Idempotency-Key": "whisper-chunk-resume-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        generated = self.request(
            "post", "/api/media/speech/synthesize",
            json={"engine": "ffmpeg_libflite", "text": "The quick brown fox jumps over the lazy dog. " * 12,
                  "voice": "slt", "language": "en"},
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        source_bytes = base64.b64decode(generated.json["data"]["audio_base64"], validate=True)
        uploaded = self.upload_data(
            source_bytes, filename="whisper-resume.wav", key="whisper-resume-material",
        )["material"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": uploaded["id"]},
            headers={"Idempotency-Key": "whisper-resume-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        duration = Fraction(asset["duration_sec"]["num"], asset["duration_sec"]["den"])
        self.assertGreater(duration, 30)
        self.assertLess(duration, 90)
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": source.json["data"]["version"], "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
                "timeline_start": {"num": 0, "den": 1},
            }]},
            headers={"Idempotency-Key": "whisper-resume-clip"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        queued = self.request(
            "post", f"/api/media/projects/{project_id}/transcriptions",
            json={"version": inserted.json["data"]["version"], "source_asset_id": asset["asset_id"],
                  "audio_stream_index": 0, "audio_channel_index": 0,
                  "language_hint": "en", "task": "transcribe"},
            headers={"Idempotency-Key": "whisper-resume-job"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        transcription_id = queued.json["data"]["transcription_id"]
        worker_errors = []

        def run_transcription_worker() -> None:
            try:
                run_once(self.app, allowed_kinds=("media_transcribe",))
            except BaseException as exc:
                worker_errors.append(type(exc).__name__)

        worker_thread = threading.Thread(target=run_transcription_worker, daemon=True)
        worker_thread.start()
        deadline = time.monotonic() + 300
        partial = None
        while time.monotonic() < deadline:
            status = self.request("get", f"/api/media/transcriptions/{transcription_id}")
            self.assertEqual(status.status_code, 200, status.get_data(as_text=True))
            job = status.json["data"]["job"]
            if job["status"] == "failed":
                self.fail(f"ASR worker failed before the first complete chunk: {job}")
            if job["progress"]["completed"] >= 1 and job["status"] in {"running", "cancel_requested"}:
                partial = status.json["data"]
                break
            if job["status"] == "completed":
                self.fail("The 2-chunk fixture completed before cancellation could be requested.")
            time.sleep(0.25)
        self.assertIsNotNone(partial, "The ASR job did not persist its first chunk before the deadline.")
        self.assertEqual(partial["candidate"]["coverage"][0]["chunk_index"], 0)
        cancel = self.request("post", f"/api/jobs/{transcription_id}/cancel", json={})
        self.assertEqual(cancel.status_code, 200, cancel.get_data(as_text=True))
        worker_thread.join(timeout=30)
        self.assertFalse(worker_thread.is_alive(), "The ASR worker did not stop after cancellation.")
        self.assertEqual(worker_errors, [])
        stopped = self.request("get", f"/api/media/transcriptions/{transcription_id}")
        self.assertEqual(stopped.json["data"]["job"]["status"], "cancelled", stopped.json["data"])
        self.assertEqual(len(stopped.json["data"]["candidate"]["coverage"]), 1)

        resumed = self.request("post", f"/api/media/transcriptions/{transcription_id}/resume", json={})
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.json["data"]["job"]["status"], "queued")
        self.assertTrue(run_once(self.app, allowed_kinds=("media_transcribe",)))
        complete = self.request("get", f"/api/media/transcriptions/{transcription_id}")
        self.assertEqual(complete.json["data"]["job"]["status"], "completed", complete.json["data"])
        self.assertEqual([item["chunk_index"] for item in complete.json["data"]["candidate"]["coverage"]], [0, 1])
        self.assertEqual(complete.json["data"]["candidate"]["coverage_state"], "complete")
        self.assertGreater(complete.json["data"]["candidate"]["word_count"], 0)

    def test_project_versions_rational_edit_undo_redo_and_real_ffmpeg_render(self) -> None:
        ffmpeg = _ffmpeg_path()
        ffprobe = _ffprobe_path()
        if not ffmpeg or not ffprobe:
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")

        source_path = self.temp_root / "generated-media.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                "-shortest", "-vf", "setpts=PTS+5/TB,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                "-af", "asetpts=PTS+5.2/TB",
                "-copyts", "-avoid_negative_ts", "disabled",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", str(source_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        source_bytes = source_path.read_bytes()
        material = self.upload_data(source_bytes, filename="generated-media.mp4", key="media-source-upload")

        created = self.request(
            "post",
            "/api/media/projects",
            json={"title": "真实素材剪辑", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "media-project-create"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        project_id = created.json["data"]["id"]
        source_response = self.request(
            "post",
            f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["material"]["id"]},
            headers={"Idempotency-Key": "media-project-source"},
        )
        self.assertEqual(source_response.status_code, 200, source_response.get_data(as_text=True))
        self.assertEqual(source_response.json["data"]["version"], 2)
        source = source_response.json["data"]["sources"][0]
        video_stream = next(item for item in source["streams"] if item["codec_type"] == "video")
        audio_stream = next(item for item in source["streams"] if item["codec_type"] == "audio")
        self.assertIsInstance(video_stream["time_base"], dict)
        self.assertIsInstance(video_stream["start_pts"], int)
        self.assertEqual(video_stream["pix_fmt"], "yuv420p")
        self.assertEqual(video_stream["color_primaries"], "bt709")
        self.assertEqual(video_stream["color_transfer"], "bt709")
        self.assertEqual(video_stream["color_space"], "bt709")
        self.assertEqual(video_stream["color_range"], "tv")
        self.assertEqual(audio_stream["sample_rate"], 44100)
        self.assertIsInstance(audio_stream["time_base"], dict)
        video_origin = Fraction(video_stream["start_pts"] * video_stream["time_base"]["num"],
                                video_stream["time_base"]["den"])
        audio_origin = Fraction(audio_stream["start_pts"] * audio_stream["time_base"]["num"],
                                audio_stream["time_base"]["den"])
        self.assertLess(video_origin, audio_origin)
        self.assertEqual(source["source_material_id"], material["material"]["id"])
        media_original = (
            self.data_root / "objects" / self.identity["workspace"]["id"] / "materials"
            / material["material"]["id"] / "original"
        )
        source_asset = fingerprint_local_media(
            str(media_original), source_material_id=material["material"]["id"],
            display_filename=material["material"]["filename"],
        )
        old_timeline = Timeline.from_assets([source_asset], format_ratio="16:9").to_dict()
        for stream in old_timeline["assets"][0]["streams"]:
            for field in ("pix_fmt", "bits_per_raw_sample", "color_range", "color_space",
                          "color_transfer", "color_primaries", "chroma_location"):
                stream.pop(field, None)
        reopened_old_profile = Timeline.from_dict(
            old_timeline, assets=[source_asset], verify_local_assets=False,
        )
        self.assertEqual(reopened_old_profile.assets[0].sha256, source_asset.sha256)
        source_url = f"/api/media/projects/{project_id}/sources/{source['asset_id']}/file?version=2"
        streamed = self.request("get", source_url, headers={"Range": "bytes=0-127"})
        self.assertEqual(streamed.status_code, 206, streamed.data[:200])
        self.assertEqual(streamed.data, source_bytes[:128])
        self.assertEqual(streamed.headers["Content-Type"], "video/mp4")
        self.assertEqual(streamed.headers["Content-Range"], f"bytes 0-127/{len(source_bytes)}")
        self.assertEqual(streamed.headers["X-Media-Project-Version"], "2")
        self.assertEqual(streamed.headers["ETag"].strip('"'), source["sha256"])
        waveform = self.request(
            "get",
            f"/api/media/projects/{project_id}/sources/{source['asset_id']}/waveform"
            "?version=2&audio_stream_index=0&video_stream_index=0&start_sec=0&end_sec=1%2F4&bins=16",
        )
        self.assertEqual(waveform.status_code, 200, waveform.get_data(as_text=True))
        measured = waveform.json["data"]["waveform"]
        self.assertEqual(measured["source_sha256"], source["sha256"])
        self.assertEqual(measured["source_material_id"], material["material"]["id"])
        self.assertEqual(measured["source_sample_rate"], 44100)
        self.assertEqual(measured["sample_rate"], 44100)
        self.assertEqual(measured["source_channels"], 2)
        self.assertEqual(measured["decoded_channels"], 2)
        self.assertEqual(measured["decoded_samples_per_channel"], 11025)
        self.assertEqual(measured["coverage"]["source_end_sec"], {"num": 1, "den": 4})
        self.assertEqual([item["channel_index"] for item in measured["channels"]], [0, 1])
        self.assertEqual(len(measured["channels"][0]["bins"]), 16)
        audio_to_video = measured["video_alignment"]["audio_to_video_offset_sec"]
        self.assertGreater(Fraction(audio_to_video["num"], audio_to_video["den"]), 0)
        right_channel = self.request(
            "get",
            f"/api/media/projects/{project_id}/sources/{source['asset_id']}/waveform"
            "?version=2&audio_stream_index=0&channel_index=1&sample_rate=22050"
            "&start_sec=0&end_sec=1%2F4&bins=8",
        )
        self.assertEqual(right_channel.status_code, 200, right_channel.get_data(as_text=True))
        self.assertEqual(right_channel.json["data"]["waveform"]["source_channels"], 2)
        self.assertEqual(right_channel.json["data"]["waveform"]["sample_rate"], 22050)
        self.assertEqual(right_channel.json["data"]["waveform"]["decoded_channels"], 1)
        self.assertEqual(right_channel.json["data"]["waveform"]["channels"][0]["channel_index"], 1)

        other_project = self.request(
            "post",
            "/api/media/projects",
            json={"title": "未关联素材工程", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "media-other-project"},
        )
        self.assertEqual(other_project.status_code, 201, other_project.get_data(as_text=True))
        foreign_source = self.request(
            "get",
            f"/api/media/projects/{other_project.json['data']['id']}/sources/{source['asset_id']}/file",
        )
        self.assertEqual(foreign_source.status_code, 404)

        source_original = (
            self.data_root / "objects" / self.identity["workspace"]["id"] / "materials"
            / material["material"]["id"] / "original"
        )
        parked_original = source_original.with_name("original.temporarily-unavailable")
        source_original.rename(parked_original)
        try:
            unavailable_source = self.request("get", source_url)
            self.assertEqual(unavailable_source.status_code, 409)
            self.assertEqual(unavailable_source.json["error"]["code"], "FILE_UNAVAILABLE")
        finally:
            parked_original.rename(source_original)

        inserted = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 2,
                "operations": [{
                    "op": "insert", "source_id": source["asset_id"], "track_id": "video-main",
                    "source_in": {"num": 1, "den": 24}, "source_out": {"num": 1, "den": 1},
                    "audio_role": "original",
                }],
            },
            headers={"Idempotency-Key": "media-project-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        self.assertEqual(inserted.json["data"]["version"], 3)
        clip = inserted.json["data"]["tracks"][0]["clips"][0]
        self.assertEqual(clip["source_in"], {"num": 1, "den": 24})

        split = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={"base_version": 3, "operations": [{"op": "split", "clip_id": clip["clip_id"], "at_sec": {"num": 1, "den": 2}}]},
            headers={"Idempotency-Key": "media-project-split"},
        )
        self.assertEqual(split.status_code, 200, split.get_data(as_text=True))
        split_clips = split.json["data"]["tracks"][0]["clips"]
        self.assertEqual(len(split_clips), 2)
        self.assertEqual(split_clips[1]["timeline_start"], {"num": 11, "den": 24})

        removed = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={"base_version": 4, "operations": [{"op": "delete", "clip_id": split_clips[1]["clip_id"], "ripple": True}]},
            headers={"Idempotency-Key": "media-project-delete"},
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        self.assertEqual(len(removed.json["data"]["tracks"][0]["clips"]), 1)

        undone = self.request(
            "post", f"/api/media/projects/{project_id}/undo", json={"base_version": 5},
            headers={"Idempotency-Key": "media-project-undo"},
        )
        self.assertEqual(undone.status_code, 200, undone.get_data(as_text=True))
        self.assertEqual(len(undone.json["data"]["tracks"][0]["clips"]), 2)
        redone = self.request(
            "post", f"/api/media/projects/{project_id}/redo", json={"base_version": 6},
            headers={"Idempotency-Key": "media-project-redo"},
        )
        self.assertEqual(redone.status_code, 200, redone.get_data(as_text=True))
        self.assertEqual(len(redone.json["data"]["tracks"][0]["clips"]), 1)

        app2 = create_app(self.data_root, expected_port=8317)
        app2.config.update(TESTING=True)
        client2 = app2.test_client()
        reopened = client2.get(
            f"/api/media/projects/{project_id}?version=7",
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self.token}", "Origin": ORIGIN},
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.json["data"]["tracks"][0]["clips"][0]["source_in"], {"num": 1, "den": 24})
        reopened_source = reopened.json["data"]["sources"][0]
        self.assertEqual(reopened_source["availability"], "present_unverified")
        material_path = self.data_root / "objects" / self.identity["workspace"]["id"] / "materials" / material["material"]["id"] / "original"
        self.assertEqual(hashlib.sha256(material_path.read_bytes()).hexdigest(), material["material"]["sha256"])

        queued = self.request(
            "post",
            f"/api/media/projects/{project_id}/renders",
            json={
                "version": 7, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 320, "height": 240,
                },
            },
            headers={"Idempotency-Key": "media-render-1"},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = queued.json["data"]["job"]["id"]
        frozen_clip = reopened.json["data"]["tracks"][0]["clips"][0]
        later_edit = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 7,
                "operations": [{"op": "delete", "clip_id": frozen_clip["clip_id"], "ripple": True}],
            },
            headers={"Idempotency-Key": "media-project-edit-after-render-queue"},
        )
        self.assertEqual(later_edit.status_code, 200, later_edit.get_data(as_text=True))
        self.assertEqual(later_edit.json["data"]["version"], 8)
        self.assertFalse(any(track["clips"] for track in later_edit.json["data"]["tracks"]))

        def cancel_before_render(*args, **kwargs):
            cancelled = self.request("post", f"/api/jobs/{job_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
            self.assertEqual(cancelled.json["data"]["status"], "cancel_requested")
            kwargs["check_continue"]()

        with patch("server.oevira.media_api.render_timeline", side_effect=cancel_before_render):
            self.assertTrue(run_once(self.app))
        cancelled_render = self.request("get", f"/api/media/renders/{queued.json['data']['render_id']}")
        self.assertEqual(cancelled_render.json["data"]["status"], "cancelled")
        self.assertEqual(cancelled_render.json["data"]["version"], 7)

        retried = self.request("post", f"/api/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 202, retried.get_data(as_text=True))
        self.assertTrue(run_once(self.app))

        render_id = queued.json["data"]["render_id"]
        render = self.request("get", f"/api/media/renders/{render_id}")
        self.assertEqual(render.status_code, 200, render.get_data(as_text=True))
        self.assertEqual(render.json["data"]["status"], "completed", render.json["data"])
        self.assertEqual(render.json["data"]["version"], 7)
        self.assertEqual(
            render.json["data"]["metadata"]["render"]["measurement"]["requested_duration_sec"],
            {"num": 11, "den": 24},
        )
        file = self.request("get", f"/api/media/renders/{render_id}/file")
        self.assertEqual(file.status_code, 200, file.data[:200])
        self.assertGreater(len(file.data), 1000)
        self.assertEqual(hashlib.sha256(file.data).hexdigest(), render.json["data"]["sha256"])
        measurement = render.json["data"]["metadata"]["render"]["measurement"]
        self.assertEqual(measurement["requested_video_frames"], 11)
        self.assertEqual(measurement["video_frames"], 11)
        self.assertEqual(measurement["audio_streams"], 1)
        self.assertEqual(measurement["video_width"], 320)
        self.assertEqual(measurement["video_height"], 240)
        self.assertEqual(measurement["video_frame_rate"], {"num": 24, "den": 1})
        signal_readback = render.json["data"]["metadata"]["render"]["video_signal_processing"]
        self.assertEqual(signal_readback["source_and_output_profile"]["pixel_format"], "yuv420p")
        self.assertEqual(signal_readback["source_and_output_profile"]["bit_depth"], 8)
        self.assertEqual(signal_readback["source_and_output_profile"]["color_transfer"], "bt709")
        self.assertEqual(signal_readback["output_readback"]["color_primaries"], "bt709")
        clock_mapping = render.json["data"]["metadata"]["render"]["audio_source_clock_mappings"][0]
        self.assertEqual(clock_mapping["clock_basis"], "selected_video_and_audio_stream_pts")
        self.assertEqual(clock_mapping["video_stream_time_base"], video_stream["time_base"])
        self.assertEqual(clock_mapping["audio_stream_time_base"], audio_stream["time_base"])
        initial_silence = Fraction(
            clock_mapping["timeline_initial_silence_sec"]["num"],
            clock_mapping["timeline_initial_silence_sec"]["den"],
        )
        self.assertGreater(initial_silence, 0)

        altered_preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/previews",
            json={"version": 7, "output_kind": "video", "render_options": {"loudness_target_lufs": -16}},
            headers={"Idempotency-Key": "media-preview-cannot-remaster"},
        )
        self.assertEqual(altered_preview.status_code, 400)
        self.assertEqual(altered_preview.json["error"]["code"], "MEDIA_PREVIEW_CANNOT_CHANGE_AUDIO_PROCESSING")

        preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/previews",
            json={
                "version": 7, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 160, "height": 90,
                },
            },
            headers={"Idempotency-Key": "media-preview-same-version"},
        )
        self.assertEqual(preview.status_code, 202, preview.get_data(as_text=True))
        preview_id = preview.json["data"]["preview_id"]
        with patch("server.oevira.media_api.render_timeline", wraps=render_timeline) as actual_preview:
            self.assertTrue(run_once(self.app))
            self.assertEqual(actual_preview.call_count, 1)
        preview_view = self.request("get", f"/api/media/previews/{preview_id}")
        self.assertEqual(preview_view.status_code, 200, preview_view.get_data(as_text=True))
        self.assertEqual(preview_view.json["data"]["status"], "completed")
        self.assertEqual(preview_view.json["data"]["purpose"], "preview")
        self.assertEqual(preview_view.json["data"]["playback_url"], f"/api/media/previews/{preview_id}/file")
        preview_measurement = preview_view.json["data"]["metadata"]["render"]["measurement"]
        self.assertEqual(preview_view.json["data"]["metadata"]["render"]["output_spec"]["width"], 160)
        self.assertEqual(preview_measurement["video_width"], 160)
        self.assertEqual(preview_measurement["video_height"], 90)
        self.assertNotIn("preview_reuse", preview_view.json["data"]["metadata"])
        preview_file = self.request(
            "get", f"/api/media/previews/{preview_id}/file", headers={"Range": "bytes=0-127"},
        )
        self.assertEqual(preview_file.status_code, 206, preview_file.data[:200])
        self.assertEqual(preview_file.headers["Content-Type"], "video/mp4")
        self.assertEqual(
            preview_file.headers["ETag"].strip('"'), preview_view.json["data"]["sha256"],
        )

        reusable_preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/previews",
            json={
                "version": 7, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 320, "height": 240,
                },
            },
            headers={"Idempotency-Key": "media-preview-exact-render-profile"},
        )
        self.assertEqual(reusable_preview.status_code, 202, reusable_preview.get_data(as_text=True))
        reusable_preview_id = reusable_preview.json["data"]["preview_id"]
        with patch(
            "server.oevira.media_api.render_timeline",
            side_effect=AssertionError("an exact same-version output profile should be reused"),
        ):
            self.assertTrue(run_once(self.app))
        reusable_view = self.request("get", f"/api/media/previews/{reusable_preview_id}")
        self.assertEqual(reusable_view.json["data"]["status"], "completed")
        self.assertEqual(
            reusable_view.json["data"]["metadata"]["preview_reuse"]["reused_render_id"], render_id,
        )
        self.assertEqual(
            reusable_view.json["data"]["metadata"]["preview_reuse"]["rendered_output_spec"],
            reusable_view.json["data"]["metadata"]["preview_reuse"]["requested_output_spec"],
        )
        reused_file = self.request("get", f"/api/media/previews/{reusable_preview_id}/file")
        self.assertEqual(reused_file.status_code, 200, reused_file.data[:200])
        self.assertEqual(reused_file.data, file.data)

        queued_preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/previews",
            json={
                "version": 7, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 160, "height": 90,
                },
            },
            headers={"Idempotency-Key": "media-preview-queued-cancel"},
        )
        self.assertEqual(queued_preview.status_code, 202, queued_preview.get_data(as_text=True))
        queued_preview_id = queued_preview.json["data"]["preview_id"]
        queued_job_id = queued_preview.json["data"]["job"]["id"]
        queued_cancel = self.request("post", f"/api/jobs/{queued_job_id}/cancel")
        self.assertEqual(queued_cancel.status_code, 200, queued_cancel.get_data(as_text=True))
        self.assertEqual(queued_cancel.json["data"]["status"], "cancelled")
        cancelled_preview = self.request("get", f"/api/media/previews/{queued_preview_id}")
        self.assertEqual(cancelled_preview.json["data"]["status"], "cancelled")

        audio_preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/audio-previews",
            json={
                "version": 7,
                "start_sec": {"num": 0, "den": 1},
                "end_sec": {"num": 11, "den": 24},
                "render_options": {"noise_reduction_db": 6, "noise_floor_db": -50},
            },
        )
        self.assertEqual(audio_preview.status_code, 200, audio_preview.get_data(as_text=True))
        self.assertEqual(audio_preview.json["data"]["version"], 7)
        self.assertEqual(audio_preview.json["data"]["start_sec"], {"num": 0, "den": 1})
        self.assertEqual(audio_preview.json["data"]["end_sec"], {"num": 11, "den": 24})
        self.assertEqual(audio_preview.json["data"]["sample_rate"], 48000)
        self.assertEqual(audio_preview.json["data"]["channels"], 2)
        self.assertFalse(audio_preview.json["data"]["persisted"])
        self.assertEqual(
            audio_preview.json["data"]["audio_source_clock_mappings"][0]["clock_basis"],
            "selected_video_and_audio_stream_pts",
        )
        before_sample = base64.b64decode(audio_preview.json["data"]["samples"]["original"]["wav_base64"])
        after_sample = base64.b64decode(audio_preview.json["data"]["samples"]["processed"]["wav_base64"])
        self.assertNotEqual(hashlib.sha256(before_sample).hexdigest(), hashlib.sha256(after_sample).hexdigest())
        with wave.open(io.BytesIO(after_sample), "rb") as sample:
            self.assertEqual(sample.getframerate(), 48000)
            self.assertEqual(sample.getnchannels(), 2)
            self.assertEqual(sample.getsampwidth(), 2)
            self.assertEqual(sample.getnframes(), 22000)
        audio_samples = sample.readframes(sample.getnframes())
        pcm = array("h")
        pcm.frombytes(audio_samples)
        early = pcm[:int(0.12 * 48000) * 2]
        audible = pcm[int(0.20 * 48000) * 2:int(0.30 * 48000) * 2]
        self.assertLess(max(map(abs, early), default=0), 100)
        self.assertGreater(sum(value * value for value in audible) / max(1, len(audible)), 1000**2)
        reopened_current = self.request("get", f"/api/media/projects/{project_id}")
        self.assertEqual(reopened_current.json["data"]["version"], 8)

        replacement_path = self.temp_root / "replacement-media.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=320x240:r=24:d=1",
                "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24:d=1",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0,setpts=PTS+5/TB,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv[v];[2:a]asetpts=PTS+4.8/TB[a]",
                "-map", "[v]", "-map", "[a]", "-copyts", "-avoid_negative_ts", "disabled",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-movflags", "+faststart", str(replacement_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            shell=False,
        )
        replacement_material = self.upload_data(
            replacement_path.read_bytes(), filename="replacement-media.mp4", key="media-replacement-upload",
        )
        added_replacement = self.request(
            "post",
            f"/api/media/projects/{project_id}/sources",
            json={"base_version": 8, "material_id": replacement_material["material"]["id"]},
            headers={"Idempotency-Key": "media-project-add-replacement-source"},
        )
        self.assertEqual(added_replacement.status_code, 200, added_replacement.get_data(as_text=True))
        replacement_source = next(
            item for item in added_replacement.json["data"]["sources"]
            if item["source_material_id"] == replacement_material["material"]["id"]
        )
        inserted_again = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 9,
                "operations": [
                    {
                        "op": "insert", "source_id": source["asset_id"], "track_id": "video-main",
                        "source_in": {"num": 0, "den": 1}, "source_out": {"num": 2, "den": 5},
                        "audio_role": "original",
                    },
                    {
                        "op": "add_caption", "start_sec": {"num": 1, "den": 10},
                        "end_sec": {"num": 3, "den": 10}, "text": "A & B <clip>",
                        "style": {
                            "font_size": 32, "color": "#FFFFFF", "background_color": "#000000",
                            "background_opacity": 0.5, "max_chars_per_line": 14, "max_lines": 2,
                            "position": "top", "safe_margin_ratio": 0.06,
                        },
                    },
                    {"op": "add_chapter", "title": "Opening", "start_sec": {"num": 1, "den": 10}},
                ],
            },
            headers={"Idempotency-Key": "media-project-insert-for-replacement"},
        )
        self.assertEqual(inserted_again.status_code, 200, inserted_again.get_data(as_text=True))
        clip_before_replace = inserted_again.json["data"]["tracks"][0]["clips"][0]
        replaced = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 10,
                "operations": [{
                    "op": "replace_clip_source", "clip_id": clip_before_replace["clip_id"],
                    "source_id": replacement_source["asset_id"], "source_in": {"num": 11, "den": 10},
                    "audio_stream_index": 0,
                }],
            },
            headers={"Idempotency-Key": "media-project-exact-source-replace"},
        )
        self.assertEqual(replaced.status_code, 200, replaced.get_data(as_text=True))
        clip_after_replace = replaced.json["data"]["tracks"][0]["clips"][0]
        self.assertEqual(clip_after_replace["clip_id"], clip_before_replace["clip_id"])
        self.assertEqual(clip_after_replace["track_id"], clip_before_replace["track_id"])
        self.assertEqual(clip_after_replace["timeline_start"], clip_before_replace["timeline_start"])
        self.assertEqual(clip_after_replace["source_out"], {"num": 3, "den": 2})
        self.assertEqual(clip_after_replace["source_in"], {"num": 11, "den": 10})
        self.assertEqual(clip_after_replace["audio_role"], clip_before_replace["audio_role"])
        self.assertEqual(clip_after_replace["audio_stream_index"], 0)
        self.assertEqual(clip_after_replace["media_asset_id"], replacement_source["asset_id"])

        srt = self.request(
            "get", f"/api/media/projects/{project_id}/subtitles/srt?version=11&frame_rate_num=24&frame_rate_den=1",
        )
        self.assertEqual(srt.status_code, 200, srt.data[:200])
        self.assertIn(b"00:00:00,125 --> 00:00:00,333", srt.data)
        self.assertIn(b"A &amp; B &lt;clip&gt;", srt.data)
        self.assertEqual(srt.headers["X-Media-Subtitle-Style-Preservation"], "styles-not-preserved")
        vtt = self.request(
            "get", f"/api/media/projects/{project_id}/subtitles/vtt?version=11&frame_rate_num=24&frame_rate_den=1",
        )
        self.assertEqual(vtt.status_code, 200, vtt.data[:200])
        self.assertTrue(vtt.data.startswith(b"WEBVTT"))
        self.assertIn(b"00:00:00.125 --> 00:00:00.333", vtt.data)
        ass = self.request(
            "get", f"/api/media/projects/{project_id}/subtitles/ass?version=11&frame_rate_num=24&frame_rate_den=1",
        )
        self.assertEqual(ass.status_code, 200, ass.data[:200])
        self.assertIn(b"[V4+ Styles]", ass.data)
        self.assertIn(b"Dialogue: 0,0:00:00.12,0:00:00.33,Caption1", ass.data)
        self.assertIn(b"A & B <clip>", ass.data)
        self.assertEqual(ass.headers["X-Media-Subtitle-Style-Preservation"], "bounded-caption-style-preserved")
        chapters = self.request("get", f"/api/media/projects/{project_id}/chapters?version=11")
        self.assertEqual(chapters.status_code, 200, chapters.get_data(as_text=True))
        self.assertEqual(chapters.json["data"]["schema"], "oevira.media.chapters.v1")
        self.assertEqual(chapters.json["data"]["chapters"][0]["title"], "Opening")
        self.assertEqual(chapters.json["data"]["chapters"][0]["start_sec"], {"num": 1, "den": 10})
        self.assertEqual(chapters.json["data"]["chapters"][0]["end_sec"], {"num": 2, "den": 5})
        self.assertEqual(
            chapters.json["data"]["chapters"][0]["source_ranges"][0]["source_range"],
            [{"num": 6, "den": 5}, {"num": 3, "den": 2}],
        )
        chapter_file = self.request("get", f"/api/media/projects/{project_id}/chapters/file?version=11")
        self.assertEqual(chapter_file.status_code, 200, chapter_file.data[:200])
        self.assertEqual(json.loads(chapter_file.data)["timeline_version"], 11)

        replacement_render = self.request(
            "post",
            f"/api/media/projects/{project_id}/renders",
            json={
                "version": 11, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 320, "height": 240,
                },
            },
            headers={"Idempotency-Key": "media-render-nonzero-video-origin"},
        )
        self.assertEqual(replacement_render.status_code, 202, replacement_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        replacement_file = self.request(
            "get", f"/api/media/renders/{replacement_render.json['data']['render_id']}/file",
        )
        self.assertEqual(replacement_file.status_code, 200, replacement_file.data[:200])
        replacement_output = self.temp_root / "replacement-render.mp4"
        replacement_output.write_bytes(replacement_file.data)
        frame = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", str(replacement_output),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, timeout=60, shell=False,
        ).stdout
        center = (120 * 320 + 160) * 3
        self.assertGreater(frame[center + 2], frame[center] + 100)

        effect_edit = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 11,
                "operations": [
                    {
                        "op": "video_placement", "clip_id": clip_after_replace["clip_id"],
                        "placement": {"x": 0, "y": 0, "width": 0.5, "height": 0.5, "opacity": 1},
                    },
                    {
                        "op": "set_video_effects", "clip_id": clip_after_replace["clip_id"],
                        "effects": [
                            {"effect_id": "exposure-1", "kind": "exposure", "parameters": {"exposure_stops": -0.5}, "enabled": True},
                            {"effect_id": "white-balance-1", "kind": "white_balance",
                             "parameters": {"temperature_kelvin": 5200, "tint": 0.1, "mix": 0.5}, "enabled": True},
                            {"effect_id": "color-balance-1", "kind": "color_balance",
                             "parameters": {"shadows": {"red": 0.1}, "preserve_lightness": True}, "enabled": True},
                            {"effect_id": "curves-1", "kind": "curves",
                             "parameters": {"master": [[0, 0], [0.5, 0.45], [1, 1]], "interpolation": "pchip"}, "enabled": True},
                            {"effect_id": "unsharp-1", "kind": "unsharp",
                             "parameters": {"luma_size_x": 3, "luma_size_y": 3, "luma_amount": 0.5}, "enabled": True},
                        ],
                    },
                    {
                        "op": "set_transform_keyframes", "clip_id": clip_after_replace["clip_id"],
                        "keyframes": [
                            {"time_sec": {"num": 0, "den": 1}, "x": 0, "y": 0, "opacity": 0.25},
                            {"time_sec": {"num": 2, "den": 5}, "x": 0.5, "y": 0.5, "opacity": 1},
                        ],
                    },
                ],
            },
            headers={"Idempotency-Key": "media-project-color-stack-and-keyframes"},
        )
        self.assertEqual(effect_edit.status_code, 200, effect_edit.get_data(as_text=True))
        self.assertEqual(effect_edit.json["data"]["version"], 12)
        effect_clip = effect_edit.json["data"]["tracks"][0]["clips"][0]
        self.assertEqual([item["kind"] for item in effect_clip["effects"]],
                         ["exposure", "white_balance", "color_balance", "curves", "unsharp"])
        self.assertEqual(len(effect_clip["transform_keyframes"]), 2)

        effect_render = self.request(
            "post",
            f"/api/media/projects/{project_id}/renders",
            json={
                "version": 12, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 320, "height": 240,
                },
            },
            headers={"Idempotency-Key": "media-render-color-effects-keyframes"},
        )
        self.assertEqual(effect_render.status_code, 202, effect_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        effect_render_view = self.request("get", f"/api/media/renders/{effect_render.json['data']['render_id']}")
        self.assertEqual(effect_render_view.json["data"]["status"], "completed", effect_render_view.json["data"])
        effect_metadata = effect_render_view.json["data"]["metadata"]["render"]
        self.assertEqual(effect_metadata["video_effect_processing"][0]["effect_order"],
                         ["exposure-1", "white-balance-1", "color-balance-1", "curves-1", "unsharp-1"])
        self.assertEqual(len(effect_metadata["composition"]["animated_placements"][0]["keyframes"]), 2)
        effect_file = self.request("get", f"/api/media/renders/{effect_render.json['data']['render_id']}/file")
        self.assertEqual(effect_file.status_code, 200, effect_file.data[:200])
        effect_output = self.temp_root / "effect-keyframe-render.mp4"
        effect_output.write_bytes(effect_file.data)

        def read_effect_frame(frame_index: int) -> bytes:
            return subprocess.run(
                [ffmpeg, "-v", "error", "-nostdin", "-i", str(effect_output),
                 "-vf", f"select=eq(n\\,{frame_index})", "-frames:v", "1", "-f", "rawvideo",
                 "-pix_fmt", "rgb24", "pipe:1"],
                check=True, capture_output=True, timeout=60, shell=False,
            ).stdout

        first_effect_frame = read_effect_frame(0)
        last_effect_frame = read_effect_frame(9)
        start_position = (60 * 320 + 80) * 3
        old_position = (60 * 320 + 80) * 3
        moved_position = (180 * 320 + 230) * 3
        first_pixel = first_effect_frame[start_position:start_position + 3]
        old_pixel = last_effect_frame[old_position:old_position + 3]
        moved_pixel = last_effect_frame[moved_position:moved_position + 3]
        self.assertGreater(sum(first_pixel), 20, tuple(first_pixel))
        self.assertLess(sum(first_pixel), frame[center + 2])
        self.assertLess(sum(old_pixel), 24, tuple(old_pixel))
        self.assertGreater(sum(moved_pixel), 2 * sum(first_pixel), tuple(moved_pixel))

        effect_preview = self.request(
            "post",
            f"/api/media/projects/{project_id}/previews",
            json={
                "version": 12, "output_kind": "video", "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1}, "sample_rate": 44100,
                    "audio_channels": 2, "width": 320, "height": 240,
                },
            },
            headers={"Idempotency-Key": "media-preview-effects-version12"},
        )
        self.assertEqual(effect_preview.status_code, 202, effect_preview.get_data(as_text=True))
        effect_preview_id = effect_preview.json["data"]["preview_id"]
        with patch(
            "server.oevira.media_api.render_timeline",
            side_effect=AssertionError("the exact video preview profile should reuse the saved effect render"),
        ):
            self.assertTrue(run_once(self.app))
        effect_preview_view = self.request("get", f"/api/media/previews/{effect_preview_id}")
        self.assertEqual(effect_preview_view.json["data"]["status"], "completed")
        self.assertEqual(
            effect_preview_view.json["data"]["metadata"]["preview_reuse"]["reused_render_id"],
            effect_render.json["data"]["render_id"],
        )
        effect_preview_file = self.request("get", f"/api/media/previews/{effect_preview_id}/file")
        self.assertEqual(effect_preview_file.data, effect_file.data)

        locked = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={"base_version": 12, "operations": [{"op": "track_controls", "track_id": "video-main", "locked": True}]},
            headers={"Idempotency-Key": "media-project-lock-main-track"},
        )
        self.assertEqual(locked.status_code, 200, locked.get_data(as_text=True))
        refused_replace = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 13,
                "operations": [{
                    "op": "replace_clip_source", "clip_id": clip_after_replace["clip_id"],
                    "source_id": source["asset_id"], "source_in": {"num": 0, "den": 1},
                    "audio_stream_index": 0,
                }],
            },
            headers={"Idempotency-Key": "media-project-replace-locked-track"},
        )
        self.assertEqual(refused_replace.status_code, 400)
        self.assertEqual(refused_replace.json["error"]["code"], "TIMELINE_TRACK_LOCKED")
        caption_id = inserted_again.json["data"]["overlays"][0]["overlay_id"]
        audio_tracks_added = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 13,
                "operations": [
                    {"op": "add_track", "track_id": "dialogue", "kind": "voiceover", "layout": "free"},
                    {
                        "op": "insert", "source_id": replacement_source["asset_id"],
                        "track_id": "dialogue", "source_in": {"num": 0, "den": 1},
                        "source_out": {"num": 1, "den": 5}, "timeline_start": {"num": 0, "den": 1},
                        "audio_role": "voiceover", "audio_stream_index": 0,
                    },
                    {
                        "op": "insert", "source_id": replacement_source["asset_id"],
                        "track_id": "dialogue", "source_in": {"num": 0, "den": 1},
                        "source_out": {"num": 1, "den": 5}, "timeline_start": {"num": 1, "den": 1},
                        "audio_role": "voiceover", "audio_stream_index": 0,
                    },
                ],
            },
            headers={"Idempotency-Key": "media-project-create-voiceover-track"},
        )
        self.assertEqual(audio_tracks_added.status_code, 200, audio_tracks_added.get_data(as_text=True))
        self.assertEqual(audio_tracks_added.json["data"]["version"], 14)
        dialogue_track = next(item for item in audio_tracks_added.json["data"]["tracks"] if item["track_id"] == "dialogue")
        target_audio_clip = dialogue_track["clips"][0]
        later_audio_clip = dialogue_track["clips"][1]
        rejected_audio_replace = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 14,
                "operations": [{
                    "op": "replace_audio_source", "clip_id": target_audio_clip["clip_id"],
                    "source_id": replacement_source["asset_id"], "source_in": {"num": 0, "den": 1},
                    "source_out": {"num": 3, "den": 5}, "ripple_track_ids": ["dialogue"],
                    "audio_stream_index": 0,
                }],
            },
            headers={"Idempotency-Key": "media-audio-replace-requires-caption-plan"},
        )
        self.assertEqual(rejected_audio_replace.status_code, 400)
        self.assertEqual(
            rejected_audio_replace.json["error"]["code"],
            "AUDIO_REVISION_INTERSECTING_OVERLAY_REQUIRES_RANGE",
        )
        audio_replaced = self.request(
            "post",
            f"/api/media/projects/{project_id}/edits",
            json={
                "base_version": 14,
                "operations": [{
                    "op": "replace_audio_source", "clip_id": target_audio_clip["clip_id"],
                    "source_id": replacement_source["asset_id"], "source_in": {"num": 0, "den": 1},
                    "source_out": {"num": 3, "den": 5}, "ripple_track_ids": ["dialogue"],
                    "audio_stream_index": 0,
                    "overlay_ranges": {caption_id: [{"num": 1, "den": 2}, {"num": 4, "den": 5}]},
                }],
            },
            headers={"Idempotency-Key": "media-audio-replace-with-caption-plan"},
        )
        self.assertEqual(audio_replaced.status_code, 200, audio_replaced.get_data(as_text=True))
        self.assertEqual(audio_replaced.json["data"]["version"], 15)
        dialogue_after_replace = next(item for item in audio_replaced.json["data"]["tracks"] if item["track_id"] == "dialogue")
        self.assertEqual(dialogue_after_replace["clips"][0]["source_out"], {"num": 3, "den": 5})
        self.assertEqual(dialogue_after_replace["clips"][1]["timeline_start"], {"num": 7, "den": 5})
        self.assertEqual(audio_replaced.json["data"]["overlays"][0]["start_sec"], {"num": 1, "den": 2})

        audio_delivery = self.request(
            "post",
            f"/api/media/projects/{project_id}/renders",
            json={
                "version": 15, "output_kind": "audio", "format": "wav",
                "output_spec": {"sample_rate": 48000, "audio_channels": 2},
            },
            headers={"Idempotency-Key": "media-voiceover-mix-delivery"},
        )
        self.assertEqual(audio_delivery.status_code, 202, audio_delivery.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        audio_delivery_view = self.request("get", f"/api/media/renders/{audio_delivery.json['data']['render_id']}")
        self.assertEqual(audio_delivery_view.json["data"]["status"], "completed", audio_delivery_view.json["data"])
        self.assertEqual(audio_delivery_view.json["data"]["output_kind"], "audio")
        self.assertEqual(audio_delivery_view.json["data"]["metadata"]["render"]["measurement"]["audio_streams"], 1)
        audio_file = self.request(
            "get", f"/api/media/renders/{audio_delivery.json['data']['render_id']}/file",
        )
        self.assertEqual(audio_file.status_code, 200, audio_file.data[:200])
        self.assertEqual(audio_file.headers["Content-Type"], "audio/wav")
        self.assertEqual(hashlib.sha256(audio_file.data).hexdigest(), audio_delivery_view.json["data"]["sha256"])
        with wave.open(io.BytesIO(audio_file.data), "rb") as rendered_audio:
            self.assertEqual(rendered_audio.getframerate(), 48000)
            self.assertEqual(rendered_audio.getnchannels(), 2)
            self.assertEqual(rendered_audio.getsampwidth(), 3)
            self.assertEqual(rendered_audio.getnframes(), 76800)

        project_export = self.request("get", f"/api/media/projects/{project_id}/export?version=15")
        self.assertEqual(project_export.status_code, 200, project_export.data[:200])
        self.assertEqual(project_export.headers["X-Media-Source-Package-State"], "material-references-only")
        manifest = json.loads(project_export.data)
        self.assertEqual(manifest["schema"], "oevira.media.project.export.v1")
        self.assertEqual(manifest["project"]["version"], 15)
        self.assertEqual(manifest["sources"][0]["sha256"], source["sha256"])

        imported = self.request(
            "post",
            "/api/media/projects/import",
            json={"manifest": manifest, "title": "复制的剪辑工程"},
            headers={"Idempotency-Key": "media-project-manifest-import"},
        )
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        imported_id = imported.json["data"]["id"]
        reopened_import = self.request("get", f"/api/media/projects/{imported_id}?version=1")
        self.assertEqual(reopened_import.status_code, 200, reopened_import.get_data(as_text=True))
        self.assertEqual(reopened_import.json["data"]["title"], "复制的剪辑工程")
        self.assertEqual(reopened_import.json["data"]["version"], 1)
        self.assertEqual(reopened_import.json["data"]["tracks"][0]["locked"], True)
        self.assertEqual(
            reopened_import.json["data"]["tracks"][0]["clips"][0]["source_in"],
            {"num": 11, "den": 10},
        )
        self.assertEqual(reopened_import.json["data"]["chapters"][0]["title"], "Opening")
        imported_chapter_id = reopened_import.json["data"]["chapters"][0]["chapter_id"]
        updated_chapter = self.request(
            "post",
            f"/api/media/projects/{imported_id}/edits",
            json={
                "base_version": 1,
                "operations": [{
                    "op": "update_chapter", "chapter_id": imported_chapter_id,
                    "title": "Introduction", "start_sec": {"num": 1, "den": 20},
                }],
            },
            headers={"Idempotency-Key": "media-imported-chapter-edit"},
        )
        self.assertEqual(updated_chapter.status_code, 200, updated_chapter.get_data(as_text=True))
        undone_chapter = self.request(
            "post", f"/api/media/projects/{imported_id}/undo", json={"base_version": 2},
            headers={"Idempotency-Key": "media-imported-chapter-undo"},
        )
        self.assertEqual(undone_chapter.status_code, 200, undone_chapter.get_data(as_text=True))
        self.assertEqual(undone_chapter.json["data"]["chapters"][0]["title"], "Opening")
        redone_chapter = self.request(
            "post", f"/api/media/projects/{imported_id}/redo", json={"base_version": 3},
            headers={"Idempotency-Key": "media-imported-chapter-redo"},
        )
        self.assertEqual(redone_chapter.status_code, 200, redone_chapter.get_data(as_text=True))
        self.assertEqual(redone_chapter.json["data"]["chapters"][0]["title"], "Introduction")
        imported_again = self.request(
            "post",
            "/api/media/projects/import",
            json={"manifest": manifest, "title": "复制的剪辑工程"},
            headers={"Idempotency-Key": "media-project-manifest-import"},
        )
        self.assertEqual(imported_again.status_code, 200, imported_again.get_data(as_text=True))
        self.assertEqual(imported_again.json["data"]["id"], imported_id)

        missing_source_manifest = json.loads(project_export.data)
        missing_hash = "0" * 64
        missing_source_manifest["timeline"]["assets"][0]["sha256"] = missing_hash
        missing_source_manifest["sources"][0]["sha256"] = missing_hash
        missing_source_manifest["source_slots"][0]["source_sha256"] = missing_hash
        missing_source = self.request(
            "post",
            "/api/media/projects/import",
            json={"manifest": missing_source_manifest},
            headers={"Idempotency-Key": "media-project-missing-import-source"},
        )
        self.assertEqual(missing_source.status_code, 409)
        self.assertEqual(missing_source.json["error"]["code"], "MEDIA_IMPORT_SOURCE_MISSING")

        transition_sources = {}
        for color in ("red", "blue"):
            transition_path = self.temp_root / f"transition-{color}.mp4"
            subprocess.run(
                [
                    ffmpeg, "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=24:d=2",
                    "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p",
                    "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-an", str(transition_path),
                ],
                check=True, capture_output=True, timeout=60, shell=False,
            )
            transition_material = self.upload_data(
                transition_path.read_bytes(), filename=transition_path.name,
                key=f"media-transition-source-{color}",
            )["material"]
            transition_sources[color] = transition_material

        transition_project = self.request(
            "post", "/api/media/projects",
            json={"title": "真实转场工程", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "media-transition-project"},
        )
        self.assertEqual(transition_project.status_code, 201, transition_project.get_data(as_text=True))
        transition_project_id = transition_project.json["data"]["id"]
        transition_project_version = 1
        transition_asset_ids = {}
        for color in ("red", "blue"):
            attached_source = self.request(
                "post", f"/api/media/projects/{transition_project_id}/sources",
                json={"base_version": transition_project_version, "material_id": transition_sources[color]["id"]},
                headers={"Idempotency-Key": f"media-transition-attach-{color}"},
            )
            self.assertEqual(attached_source.status_code, 200, attached_source.get_data(as_text=True))
            transition_project_version = attached_source.json["data"]["version"]
            transition_asset_ids[color] = next(
                item["asset_id"] for item in attached_source.json["data"]["sources"]
                if item["source_material_id"] == transition_sources[color]["id"]
            )
        transition_clips_added = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={
                "base_version": transition_project_version,
                "operations": [
                    {"op": "insert", "source_id": transition_asset_ids["red"], "track_id": "video-main",
                     "source_in": {"num": 1, "den": 4}, "source_out": {"num": 5, "den": 4}},
                    {"op": "insert", "source_id": transition_asset_ids["blue"], "track_id": "video-main",
                     "source_in": {"num": 3, "den": 4}, "source_out": {"num": 7, "den": 4}},
                ],
            },
            headers={"Idempotency-Key": "media-transition-insert-clips"},
        )
        self.assertEqual(transition_clips_added.status_code, 200, transition_clips_added.get_data(as_text=True))
        transition_clips = transition_clips_added.json["data"]["tracks"][0]["clips"]
        transition_animated = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={
                "base_version": transition_clips_added.json["data"]["version"],
                "operations": [
                    {"op": "video_placement", "clip_id": transition_clips[0]["clip_id"],
                     "placement": {"x": 0, "y": 0, "width": 0.5, "height": 0.5, "opacity": 1}},
                    {"op": "set_transform_keyframes", "clip_id": transition_clips[0]["clip_id"],
                     "keyframes": [
                         {"time_sec": {"num": 0, "den": 1}, "x": 0, "y": 0, "opacity": 0.4},
                         {"time_sec": {"num": 4, "den": 5}, "x": 0.5, "y": 0.5, "opacity": 1},
                     ]},
                    {"op": "set_video_effects", "clip_id": transition_clips[0]["clip_id"],
                     "effects": [{
                         "effect_id": "red-exposure", "kind": "exposure",
                         "parameters": {"exposure_stops": -1},
                         "keyframes": [
                             {"time_sec": {"num": 0, "den": 1},
                              "parameters": {"exposure_stops": -1}, "interpolation": "linear"},
                             {"time_sec": {"num": 4, "den": 5},
                              "parameters": {"exposure_stops": 1}, "interpolation": "hold"},
                         ],
                     }, {
                         "effect_id": "red-ellipse-mask", "kind": "mask",
                         "parameters": {"shape": "ellipse", "x": 0, "y": 0, "width": 1, "height": 1,
                                        "feather": 0, "invert": False},
                     }]},
                    {"op": "video_placement", "clip_id": transition_clips[1]["clip_id"],
                     "placement": {"x": 0, "y": 0, "width": 1, "height": 1, "opacity": 1}},
                ],
            },
            headers={"Idempotency-Key": "media-transition-keyframed-source-edit"},
        )
        self.assertEqual(transition_animated.status_code, 200, transition_animated.get_data(as_text=True))
        transition_split = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": transition_animated.json["data"]["version"],
                  "operations": [{"op": "split", "clip_id": transition_clips[0]["clip_id"],
                                  "at_sec": {"num": 3, "den": 4}}]},
            headers={"Idempotency-Key": "media-transition-keyframe-split"},
        )
        self.assertEqual(transition_split.status_code, 200, transition_split.get_data(as_text=True))
        split_transition_clips = transition_split.json["data"]["tracks"][0]["clips"]
        self.assertEqual(split_transition_clips[0]["transform_keyframes"][-1]["time_sec"],
                         {"num": 1, "den": 2})
        self.assertEqual(split_transition_clips[1]["transform_keyframes"][0]["time_sec"],
                         {"num": 0, "den": 1})
        self.assertAlmostEqual(split_transition_clips[0]["transform_keyframes"][-1]["x"], 0.3125)
        self.assertAlmostEqual(split_transition_clips[1]["transform_keyframes"][0]["x"], 0.3125)
        self.assertEqual(
            split_transition_clips[1]["effects"][0]["keyframes"][0]["parameters"]["exposure_stops"], 0.25,
        )
        transition_edited = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={
                "base_version": transition_split.json["data"]["version"],
                "operations": [{"op": "transition", "from_clip": split_transition_clips[1]["clip_id"],
                                "to_clip": split_transition_clips[2]["clip_id"], "transition_type": "wipeleft",
                                "duration_sec": {"num": 2, "den": 5}, "reason": "Reviewed scene change"}],
            },
            headers={"Idempotency-Key": "media-transition-wipeleft-edit"},
        )
        self.assertEqual(transition_edited.status_code, 200, transition_edited.get_data(as_text=True))
        self.assertEqual(transition_edited.json["data"]["transitions"][0]["transition_type"], "wipeleft")
        transition_version = transition_edited.json["data"]["version"]
        transition_undo = self.request(
            "post", f"/api/media/projects/{transition_project_id}/undo", json={"base_version": transition_version},
            headers={"Idempotency-Key": "media-transition-undo"},
        )
        self.assertEqual(transition_undo.status_code, 200, transition_undo.get_data(as_text=True))
        self.assertEqual(transition_undo.json["data"]["transitions"], [])
        transition_redo = self.request(
            "post", f"/api/media/projects/{transition_project_id}/redo",
            json={"base_version": transition_undo.json["data"]["version"]},
            headers={"Idempotency-Key": "media-transition-redo"},
        )
        self.assertEqual(transition_redo.status_code, 200, transition_redo.get_data(as_text=True))
        transition_version = transition_redo.json["data"]["version"]
        self.assertEqual(transition_redo.json["data"]["transitions"][0]["transition_type"], "wipeleft")

        transition_export = self.request(
            "get", f"/api/media/projects/{transition_project_id}/export?version={transition_version}",
        )
        self.assertEqual(transition_export.status_code, 200, transition_export.data[:200])
        transition_manifest = json.loads(transition_export.data)
        self.assertEqual(transition_manifest["timeline"]["transitions"][0]["transition_type"], "wipeleft")

        transition_render = self.request(
            "post", f"/api/media/projects/{transition_project_id}/renders",
            json={"version": transition_version, "output_kind": "video", "format": "mp4",
                  "output_spec": {"frame_rate": {"num": 24, "den": 1}, "sample_rate": 48000,
                                  "audio_channels": 2, "width": 320, "height": 180}},
            headers={"Idempotency-Key": "media-transition-wipeleft-render"},
        )
        self.assertEqual(transition_render.status_code, 202, transition_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        transition_render_id = transition_render.json["data"]["render_id"]
        transition_render_view = self.request("get", f"/api/media/renders/{transition_render_id}")
        self.assertEqual(transition_render_view.json["data"]["status"], "completed")
        transition_metadata = transition_render_view.json["data"]["metadata"]["render"]["transition_processing"]
        self.assertEqual(transition_metadata["output_frame_rate"], {"num": 24, "den": 1})
        self.assertEqual(transition_metadata["applied"][0]["type"], "wipeleft")
        transition_file = self.request("get", f"/api/media/renders/{transition_render_id}/file")
        self.assertEqual(transition_file.status_code, 200, transition_file.data[:200])
        transition_output = self.temp_root / "wipeleft-transition.mp4"
        transition_output.write_bytes(transition_file.data)

        def read_transition_frame(frame_index: int) -> bytes:
            return subprocess.run(
                [ffmpeg, "-v", "error", "-nostdin", "-i", str(transition_output),
                 "-vf", f"select=eq(n\\,{frame_index})", "-frames:v", "1", "-f", "rawvideo",
                 "-pix_fmt", "rgb24", "pipe:1"],
                check=True, capture_output=True, timeout=60, shell=False,
            ).stdout

        initial_transition_frame = read_transition_frame(0)
        first_transition_frame = read_transition_frame(10)
        last_transition_frame = read_transition_frame(35)
        initial_red_pixel = (50 * 320 + 80) * 3
        animated_red_pixel = (90 * 320 + 160) * 3
        final_blue_pixel = (90 * 320 + 160) * 3
        initial_red = initial_transition_frame[initial_red_pixel:initial_red_pixel + 3]
        first_center = first_transition_frame[animated_red_pixel:animated_red_pixel + 3]
        last_center = last_transition_frame[final_blue_pixel:final_blue_pixel + 3]
        masked_corner_pixel = (5 * 320 + 5) * 3
        masked_corner = initial_transition_frame[masked_corner_pixel:masked_corner_pixel + 3]
        self.assertGreater(first_center[0], first_center[2] + 100, tuple(first_center))
        self.assertGreater(first_center[0], initial_red[0] + 40, (tuple(initial_red), tuple(first_center)))
        self.assertLess(sum(masked_corner), 12, tuple(masked_corner))
        self.assertGreater(last_center[2], last_center[0] + 150, tuple(last_center))

        transition_preview = self.request(
            "post", f"/api/media/projects/{transition_project_id}/previews",
            json={"version": transition_version, "output_kind": "video", "format": "mp4",
                  "output_spec": {"frame_rate": {"num": 24, "den": 1}, "sample_rate": 48000,
                                  "audio_channels": 2, "width": 320, "height": 180}},
            headers={"Idempotency-Key": "media-transition-wipeleft-preview"},
        )
        self.assertEqual(transition_preview.status_code, 202, transition_preview.get_data(as_text=True))
        with patch(
            "server.oevira.media_api.render_timeline",
            side_effect=AssertionError("the exact transition output profile should reuse the saved render"),
        ):
            self.assertTrue(run_once(self.app))
        transition_preview_view = self.request(
            "get", f"/api/media/previews/{transition_preview.json['data']['preview_id']}",
        )
        self.assertEqual(transition_preview_view.json["data"]["status"], "completed")
        self.assertEqual(
            transition_preview_view.json["data"]["metadata"]["preview_reuse"]["reused_render_id"],
            transition_render_id,
        )
        transition_preview_file = self.request(
            "get", f"/api/media/previews/{transition_preview.json['data']['preview_id']}/file",
            headers={"Range": "bytes=0-127"},
        )
        self.assertEqual(transition_preview_file.status_code, 206)
        self.assertEqual(transition_preview_file.headers["Content-Type"], "video/mp4")
        self.assertEqual(transition_preview_file.headers["ETag"].strip('"'), transition_render_view.json["data"]["sha256"])

        hold_effects = [
            {"effect_id": "red-exposure", "kind": "exposure", "parameters": {"exposure_stops": -1},
             "keyframes": [
                 {"time_sec": {"num": 0, "den": 1}, "parameters": {"exposure_stops": -1},
                  "interpolation": "hold"},
                 {"time_sec": {"num": 1, "den": 2}, "parameters": {"exposure_stops": 0.25},
                  "interpolation": "hold"},
             ]},
            {"effect_id": "red-ellipse-mask", "kind": "mask",
             "parameters": {"shape": "ellipse", "x": 0, "y": 0, "width": 1, "height": 1,
                            "feather": 0, "invert": False}},
        ]
        hold_effects_second_half = [
            {"effect_id": "red-exposure", "kind": "exposure", "parameters": {"exposure_stops": 0.25},
             "keyframes": [
                 {"time_sec": {"num": 0, "den": 1}, "parameters": {"exposure_stops": 0.25},
                  "interpolation": "hold"},
                 {"time_sec": {"num": 3, "den": 10}, "parameters": {"exposure_stops": 1},
                  "interpolation": "hold"},
             ]},
            {"effect_id": "red-ellipse-mask", "kind": "mask",
             "parameters": {"shape": "ellipse", "x": 0, "y": 0, "width": 1, "height": 1,
                            "feather": 0, "invert": False}},
        ]
        hold_edit = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": transition_version, "operations": [
                {"op": "set_video_effects", "clip_id": split_transition_clips[0]["clip_id"],
                 "effects": hold_effects},
                {"op": "set_video_effects", "clip_id": split_transition_clips[1]["clip_id"],
                 "effects": hold_effects_second_half},
            ]},
            headers={"Idempotency-Key": "media-transition-exposure-hold-comparison"},
        )
        self.assertEqual(hold_edit.status_code, 200, hold_edit.get_data(as_text=True))
        hold_render = self.request(
            "post", f"/api/media/projects/{transition_project_id}/renders",
            json={"version": hold_edit.json["data"]["version"], "output_kind": "video", "format": "mp4",
                  "output_spec": {"frame_rate": {"num": 24, "den": 1}, "sample_rate": 48000,
                                  "audio_channels": 2, "width": 320, "height": 180}},
            headers={"Idempotency-Key": "media-transition-exposure-hold-render"},
        )
        self.assertEqual(hold_render.status_code, 202, hold_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        hold_render_file = self.request("get", f"/api/media/renders/{hold_render.json['data']['render_id']}/file")
        self.assertEqual(hold_render_file.status_code, 200, hold_render_file.data[:200])
        hold_output = self.temp_root / "hold-exposure-render.mp4"
        hold_output.write_bytes(hold_render_file.data)
        hold_frame = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", str(hold_output),
             "-vf", "select=eq(n\\,10)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, timeout=60, shell=False,
        ).stdout
        hold_pixel = hold_frame[animated_red_pixel:animated_red_pixel + 3]
        self.assertGreater(first_center[0], hold_pixel[0] + 30, (tuple(first_center), tuple(hold_pixel)))

        red_source_slot = next(
            slot for slot in transition_manifest["source_slots"] if slot["label"] == "transition-red.mp4"
        )
        self.assertEqual(red_source_slot["required_streams"], {"video": 1, "audio": 0})
        self.assertEqual(red_source_slot["source_inventory"], {"video_streams": 1, "audio_streams": 0})
        self.assertEqual(red_source_slot["minimum_source_duration_sec"], {"num": 5, "den": 4})
        self.assertEqual(red_source_slot["rights_state"], "unknown")

        short_green_path = self.temp_root / "transition-green-short.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "color=c=green:s=320x180:r=24:d=0.5",
                "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-an", str(short_green_path),
            ],
            check=True, capture_output=True, timeout=60, shell=False,
        )
        short_green = self.upload_data(
            short_green_path.read_bytes(), filename=short_green_path.name, key="media-template-short-green",
        )["material"]
        rejected_slot_import = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": transition_manifest,
                  "source_slot_map": {red_source_slot["slot_id"]: short_green["id"]}},
            headers={"Idempotency-Key": "media-template-slot-reject-short-source"},
        )
        self.assertEqual(rejected_slot_import.status_code, 409, rejected_slot_import.get_data(as_text=True))
        self.assertEqual(rejected_slot_import.json["error"]["code"], "MEDIA_TEMPLATE_SLOT_DURATION_INSUFFICIENT")

        green_path = self.temp_root / "transition-green.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "color=c=green:s=320x180:r=24:d=2",
                "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p",
                "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-an", str(green_path),
            ],
            check=True, capture_output=True, timeout=60, shell=False,
        )
        green = self.upload_data(
            green_path.read_bytes(), filename=green_path.name, key="media-template-green-source",
        )["material"]
        templated_import = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": transition_manifest,
                  "title": "绿色素材替换模板", "source_slot_map": {red_source_slot["slot_id"]: green["id"]}},
            headers={"Idempotency-Key": "media-template-slot-replace-green"},
        )
        self.assertEqual(templated_import.status_code, 201, templated_import.get_data(as_text=True))
        templated_project_id = templated_import.json["data"]["id"]
        templated_view = self.request("get", f"/api/media/projects/{templated_project_id}?version=1")
        templated_red_asset = next(
            source for source in templated_view.json["data"]["sources"]
            if source["source_material_id"] == green["id"]
        )
        self.assertNotEqual(templated_red_asset["sha256"], red_source_slot["source_sha256"])
        self.assertEqual(templated_red_asset["filename"], "transition-green.mp4")
        self.assertEqual(templated_view.json["data"]["transitions"][0]["transition_type"], "wipeleft")

        templated_render = self.request(
            "post", f"/api/media/projects/{templated_project_id}/renders",
            json={"version": 1, "output_kind": "video", "format": "mp4",
                  "output_spec": {"frame_rate": {"num": 24, "den": 1}, "sample_rate": 48000,
                                  "audio_channels": 2, "width": 320, "height": 180}},
            headers={"Idempotency-Key": "media-template-replacement-render"},
        )
        self.assertEqual(templated_render.status_code, 202, templated_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        templated_file = self.request(
            "get", f"/api/media/renders/{templated_render.json['data']['render_id']}/file",
        )
        self.assertEqual(templated_file.status_code, 200, templated_file.data[:200])
        templated_output = self.temp_root / "green-slot-render.mp4"
        templated_output.write_bytes(templated_file.data)
        replacement_frame = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", str(templated_output),
             "-vf", "select=eq(n\\,0)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, timeout=60, shell=False,
        ).stdout
        replacement_pixel = replacement_frame[initial_red_pixel:initial_red_pixel + 3]
        self.assertGreater(replacement_pixel[1], replacement_pixel[0] + 10, tuple(replacement_pixel))

        history_root_manifest = self.request(
            "get", f"/api/media/projects/{transition_project_id}/export?version=1",
        )
        self.assertEqual(history_root_manifest.status_code, 200, history_root_manifest.data[:200])
        history_root = json.loads(history_root_manifest.data)
        history_branch_manifest = transition_manifest
        history_other_manifest_response = self.request(
            "get", f"/api/media/projects/{transition_project_id}/export?version={transition_split.json['data']['version']}",
        )
        self.assertEqual(history_other_manifest_response.status_code, 200, history_other_manifest_response.data[:200])
        history_other_branch = json.loads(history_other_manifest_response.data)
        def source_origin_for(manifest_document: dict, status: str) -> dict:
            snapshot_hash = hashlib.sha256(
                f"historical-media-package-{manifest_document['project']['version']}-{status}".encode("ascii")
            ).hexdigest()
            return {
                "source_project_id": "legacy-project-lineage",
                "source_package_sha256": snapshot_hash,
                "source_snapshot_sha256": snapshot_hash,
                "source_version": manifest_document["project"]["version"],
                "source_created_at": "2026-10-18T08:30:00+08:00",
                "historical_state": status,
                "approval_status": "historical_only",
                "rights_state": "unknown",
                "publication_state": "not_performed",
            }

        historical_import = self.request(
            "post", "/api/media/projects/import-history",
            json={
                "title": "分叉历史剪辑工程", "kind": "video",
                "current_source_version_id": "legacy-head-a",
                "versions": [
                    {"source_version_id": "legacy-root", "parent_source_version_id": None,
                     "manifest": history_root, "source_origin": source_origin_for(history_root, "root")},
                    {"source_version_id": "legacy-head-a", "parent_source_version_id": "legacy-root",
                     "manifest": history_branch_manifest,
                     "source_origin": source_origin_for(history_branch_manifest, "branch_a")},
                    {"source_version_id": "legacy-head-b", "parent_source_version_id": "legacy-root",
                     "manifest": history_other_branch,
                     "source_origin": source_origin_for(history_other_branch, "branch_b")},
                ],
            },
            headers={"Idempotency-Key": "media-import-history-dag"},
        )
        self.assertEqual(historical_import.status_code, 201, historical_import.get_data(as_text=True))
        historical_project_id = historical_import.json["data"]["id"]
        historical_view = self.request("get", f"/api/media/projects/{historical_project_id}")
        historical_versions = historical_view.json["data"]["versions"]
        self.assertEqual([item["parent_version"] for item in historical_versions], [None, 1, 1])
        self.assertEqual(historical_view.json["data"]["version"], 3)
        self.assertEqual(historical_view.json["data"]["source_origin"]["historical_state"], "branch_a")
        historical_sibling = self.request("get", f"/api/media/projects/{historical_project_id}?version=2")
        self.assertEqual(historical_sibling.json["data"]["source_origin"]["historical_state"], "branch_b")
        history_undo = self.request(
            "post", f"/api/media/projects/{historical_project_id}/undo", json={"base_version": 3},
            headers={"Idempotency-Key": "media-import-history-undo-head"},
        )
        self.assertEqual(history_undo.status_code, 200, history_undo.get_data(as_text=True))
        self.assertEqual(history_undo.json["data"]["source_origin"]["historical_state"], "root")
        self.assertFalse(any(track["clips"] for track in history_undo.json["data"]["tracks"]))
        historical_branch_after_undo = self.request(
            "get", f"/api/media/projects/{historical_project_id}?version=2",
        )
        self.assertEqual(
            historical_branch_after_undo.json["data"]["source_origin"]["historical_state"], "branch_b",
        )

        current_transition_project = self.request("get", f"/api/media/projects/{transition_project_id}")
        recording_base = current_transition_project.json["data"]["version"]

        def upload_take_audio(name: str, frequency: int) -> dict:
            wav_path = self.temp_root / name
            subprocess.run(
                [
                    ffmpeg, "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=0.25",
                    "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(wav_path),
                ],
                check=True, capture_output=True, timeout=60, shell=False,
            )
            uploaded = self.upload_data(
                wav_path.read_bytes(), filename=name, key=f"media-take-segment-{name}",
            )
            return uploaded["material"]

        def create_take(*, base_version: int, key: str, epoch_id: str, frequency: int, cue_id: str = "cue-intro"):
            created_take = self.request(
                "post", f"/api/media/projects/{transition_project_id}/recordings",
                json={"base_version": base_version, "cue_id": cue_id, "speaker_label": "主持人",
                      "track_kind": "voiceover", "timeline_start_sec": {"num": 2, "den": 1},
                      "sample_rate": 48000, "channels": 1, "sample_format": "pcm_s16le"},
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(created_take.status_code, 201, created_take.get_data(as_text=True))
            take = created_take.json["data"]
            self.assertEqual(take["status"], "armed")
            started_take = self.request(
                "post", f"/api/media/recordings/{take['id']}/start",
                json={"capture_epoch_id": epoch_id},
                headers={"Idempotency-Key": f"{key}-start"},
            )
            self.assertEqual(started_take.status_code, 200, started_take.get_data(as_text=True))
            self.assertEqual(started_take.json["data"]["status"], "awaiting_first_segment")
            materials = [upload_take_audio(f"{key}-segment-{index}.wav", frequency + index * 80)
                         for index in range(2 if key == "media-take-a" else 1)]
            sample_starts = [0, 13200] if len(materials) == 2 else [0]
            for sequence, (material_row, sample_start) in enumerate(zip(materials, sample_starts, strict=True)):
                appended = self.request(
                    "post", f"/api/media/recordings/{take['id']}/segments",
                    json={"sequence": sequence, "material_id": material_row["id"],
                          "audio_stream_index": 0, "capture_epoch_id": epoch_id,
                          "capture_sample_start": sample_start, "sample_count": 12000},
                    headers={"Idempotency-Key": f"{key}-segment-{sequence}"},
                )
                self.assertEqual(appended.status_code, 201, appended.get_data(as_text=True))
                self.assertEqual(appended.json["data"]["segments"][sequence]["sample_count"], 12000)
            if len(materials) == 2:
                self.assertEqual(appended.json["data"]["segments"][1]["gap_samples_before"], 1200)
                self.assertEqual(appended.json["data"]["segments"][1]["timeline_start_sec"],
                                 {"num": 91, "den": 40})
            self.assertEqual(appended.json["data"]["status"], "recording")
            stopped_take = self.request(
                "post", f"/api/media/recordings/{take['id']}/stop",
                json={"last_confirmed_sequence": len(materials) - 1, "native_capture_stopped": True},
                headers={"Idempotency-Key": f"{key}-stop"},
            )
            self.assertEqual(stopped_take.status_code, 200, stopped_take.get_data(as_text=True))
            completed_take = self.request(
                "post", f"/api/media/recordings/{take['id']}/complete", json={},
                headers={"Idempotency-Key": f"{key}-complete"},
            )
            self.assertEqual(completed_take.status_code, 201, completed_take.get_data(as_text=True))
            self.assertEqual(completed_take.json["data"]["recording"]["status"], "completed")
            track = next(item for item in completed_take.json["data"]["project"]["tracks"]
                         if item.get("recording_session_id") == take["id"])
            self.assertEqual(track["take_id"], take["take_id"])
            self.assertEqual(track["cue_id"], cue_id)
            self.assertEqual(len(track["clips"]), len(materials))
            return completed_take.json["data"]["recording"], completed_take.json["data"]["project"]

        take_a, project_after_take_a = create_take(
            base_version=recording_base, key="media-take-a", epoch_id="device-epoch-a", frequency=440,
        )
        first_take_segment = self.request(
            "get", f"/api/media/recordings/{take_a['id']}/segments/0/file",
            headers={"Range": "bytes=0-63"},
        )
        self.assertEqual(first_take_segment.status_code, 206)
        self.assertEqual(first_take_segment.headers["Content-Type"], "audio/wav")
        self.assertEqual(first_take_segment.headers["X-Media-Recording-Session"], take_a["id"])
        take_b, project_after_take_b = create_take(
            base_version=project_after_take_a["version"], key="media-take-b", epoch_id="device-epoch-b", frequency=880,
        )
        self.assertEqual(project_after_take_b["version"], project_after_take_a["version"] + 1)
        take_tracks = {item["recording_session_id"]: item for item in project_after_take_b["tracks"]
                       if item.get("recording_session_id")}
        take_a_track = take_tracks[take_a["id"]]
        take_b_track = take_tracks[take_b["id"]]

        solo_a = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": project_after_take_b["version"], "operations": [
                {"op": "track_controls", "track_id": take_a_track["track_id"], "solo": True},
                {"op": "track_controls", "track_id": take_b_track["track_id"], "solo": False},
            ]},
            headers={"Idempotency-Key": "media-take-solo-a"},
        )
        self.assertEqual(solo_a.status_code, 200, solo_a.get_data(as_text=True))
        audition_a = self.request(
            "post", f"/api/media/projects/{transition_project_id}/audio-previews",
            json={"version": solo_a.json["data"]["version"], "start_sec": {"num": 2, "den": 1},
                  "end_sec": {"num": 9, "den": 4}},
        )
        self.assertEqual(audition_a.status_code, 200, audition_a.get_data(as_text=True))
        samples_a = base64.b64decode(audition_a.json["data"]["samples"]["original"]["wav_base64"])
        with wave.open(io.BytesIO(samples_a), "rb") as audition_wave:
            self.assertEqual(audition_wave.getframerate(), 48000)
            self.assertEqual(audition_wave.getnframes(), 12000)

        solo_b = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": solo_a.json["data"]["version"], "operations": [
                {"op": "track_controls", "track_id": take_a_track["track_id"], "solo": False},
                {"op": "track_controls", "track_id": take_b_track["track_id"], "solo": True},
            ]},
            headers={"Idempotency-Key": "media-take-solo-b"},
        )
        self.assertEqual(solo_b.status_code, 200, solo_b.get_data(as_text=True))
        audition_b = self.request(
            "post", f"/api/media/projects/{transition_project_id}/audio-previews",
            json={"version": solo_b.json["data"]["version"], "start_sec": {"num": 2, "den": 1},
                  "end_sec": {"num": 9, "den": 4}},
        )
        self.assertEqual(audition_b.status_code, 200, audition_b.get_data(as_text=True))
        samples_b = base64.b64decode(audition_b.json["data"]["samples"]["original"]["wav_base64"])
        self.assertNotEqual(hashlib.sha256(samples_a).hexdigest(), hashlib.sha256(samples_b).hexdigest())

        solo_reset = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": solo_b.json["data"]["version"], "operations": [
                {"op": "track_controls", "track_id": take_b_track["track_id"], "solo": False},
            ]},
            headers={"Idempotency-Key": "media-take-solo-reset"},
        )
        self.assertEqual(solo_reset.status_code, 200, solo_reset.get_data(as_text=True))
        comp = self.request(
            "post", f"/api/media/projects/{transition_project_id}/takes/comp",
            json={"base_version": solo_reset.json["data"]["version"], "cue_id": "cue-intro",
                  "track_id": "comp-intro", "speaker_label": "主持人合成",
                  "segments": [
                      {"recording_session_id": take_a["id"], "sequence": 0,
                       "source_in_sample": 0, "source_out_sample": 12000,
                       "timeline_start_sec": {"num": 2, "den": 1}},
                      {"recording_session_id": take_b["id"], "sequence": 0,
                       "source_in_sample": 0, "source_out_sample": 12000,
                       "timeline_start_sec": {"num": 9, "den": 4}},
                  ]},
            headers={"Idempotency-Key": "media-create-recorded-take-comp"},
        )
        self.assertEqual(comp.status_code, 200, comp.get_data(as_text=True))
        comp_track = next(item for item in comp.json["data"]["tracks"] if item["track_id"] == "comp-intro")
        self.assertEqual(comp_track["take_type"], "comp")
        self.assertEqual(comp_track["cue_id"], "cue-intro")
        self.assertEqual(len(comp_track["clips"]), 2)

        comp_solo = self.request(
            "post", f"/api/media/projects/{transition_project_id}/edits",
            json={"base_version": comp.json["data"]["version"], "operations": [
                {"op": "track_controls", "track_id": "comp-intro", "solo": True},
            ]},
            headers={"Idempotency-Key": "media-take-comp-solo"},
        )
        self.assertEqual(comp_solo.status_code, 200, comp_solo.get_data(as_text=True))
        take_comp_render = self.request(
            "post", f"/api/media/projects/{transition_project_id}/renders",
            json={"version": comp_solo.json["data"]["version"], "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48000, "audio_channels": 2}},
            headers={"Idempotency-Key": "media-take-comp-render"},
        )
        self.assertEqual(take_comp_render.status_code, 202, take_comp_render.get_data(as_text=True))
        self.assertTrue(run_once(self.app))
        take_comp_file = self.request(
            "get", f"/api/media/renders/{take_comp_render.json['data']['render_id']}/file",
        )
        self.assertEqual(take_comp_file.status_code, 200, take_comp_file.data[:200])
        with wave.open(io.BytesIO(take_comp_file.data), "rb") as comp_wave:
            self.assertEqual(comp_wave.getframerate(), 48000)
            self.assertEqual(comp_wave.getnchannels(), 2)
            self.assertEqual(comp_wave.getnframes(), 121200)
    def test_recording_take_completion_recovers_committed_version_after_restart(self) -> None:
        project_id, take = self.create_stopped_recording_take("recording-completion-recovery")
        real_append_project_version = media_api_module._append_project_version

        def interrupt_after_legacy_project_commit(*args, **kwargs):
            # Reproduce the old split-transaction window: the project version
            # commits, then the process exits before the take session is finalized.
            kwargs.pop("after_version", None)
            real_append_project_version(*args, **kwargs)
            raise RuntimeError("simulated interruption after project version commit")

        with patch(
            "server.oevira.media_api._append_project_version",
            side_effect=interrupt_after_legacy_project_commit,
        ):
            interrupted = self.request(
                "post",
                f"/api/media/recordings/{take['id']}/complete",
                json={},
                headers={"Idempotency-Key": "recording-completion-first-attempt"},
            )
        self.assertEqual(interrupted.status_code, 500)

        before_restart = self.request("get", f"/api/media/projects/{project_id}")
        self.assertEqual(before_restart.status_code, 200, before_restart.get_data(as_text=True))
        self.assertEqual(before_restart.json["data"]["current_version"], 2)
        pending_take = self.request("get", f"/api/media/recordings/{take['id']}")
        self.assertEqual(pending_take.json["data"]["status"], "stopping")

        self.reopen_app()
        recovered = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/complete",
            json={},
            headers={"Idempotency-Key": "recording-completion-retry"},
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        result = recovered.json["data"]
        self.assertEqual(result["recording"]["status"], "completed")
        self.assertEqual(result["recording"]["project_version"], 2)
        self.assertEqual(result["project"]["version"], 2)
        self.assertEqual(result["project"]["current_version"], 2)
        recording_tracks = [
            track for track in result["project"]["tracks"]
            if track.get("recording_session_id") == take["id"]
        ]
        self.assertEqual(len(recording_tracks), 1)
        self.assertEqual(recording_tracks[0]["take_id"], take["take_id"])
        self.assertEqual(len(recording_tracks[0]["clips"]), 1)

        repeated = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/complete",
            json={},
            headers={"Idempotency-Key": "recording-completion-retry"},
        )
        self.assertEqual(repeated.status_code, 200, repeated.get_data(as_text=True))
        self.assertEqual(repeated.json["data"]["project"]["version"], 2)
        self.assertEqual(
            sum(1 for item in repeated.json["data"]["project"]["versions"] if item["operation"] == "record_take"),
            1,
        )

    def test_recording_take_completion_rolls_back_project_when_receipt_write_fails(self) -> None:
        project_id, take = self.create_stopped_recording_take("recording-completion-atomicity")
        real_finalize = media_api_module._record_media_take_completion

        def fail_inside_project_version_transaction(*args, **kwargs):
            real_finalize(*args, **kwargs)
            raise RuntimeError("simulated interruption before the shared transaction commits")

        with patch(
            "server.oevira.media_api._record_media_take_completion",
            side_effect=fail_inside_project_version_transaction,
        ):
            interrupted = self.request(
                "post",
                f"/api/media/recordings/{take['id']}/complete",
                json={},
                headers={"Idempotency-Key": "recording-completion-atomic"},
            )
        self.assertEqual(interrupted.status_code, 500)

        unchanged_project = self.request("get", f"/api/media/projects/{project_id}")
        self.assertEqual(unchanged_project.status_code, 200, unchanged_project.get_data(as_text=True))
        self.assertEqual(unchanged_project.json["data"]["current_version"], 1)
        self.assertEqual(len(unchanged_project.json["data"]["versions"]), 1)
        pending_take = self.request("get", f"/api/media/recordings/{take['id']}")
        self.assertEqual(pending_take.json["data"]["status"], "stopping")

        self.reopen_app()
        completed = self.request(
            "post",
            f"/api/media/recordings/{take['id']}/complete",
            json={},
            headers={"Idempotency-Key": "recording-completion-atomic"},
        )
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        self.assertEqual(completed.json["data"]["recording"]["status"], "completed")
        self.assertEqual(completed.json["data"]["project"]["version"], 2)


    def test_otio_interchange_carries_verified_sources_and_resolves_as_delivery_member(self) -> None:
        from server.oevira.media_interchange import resolve_media_interchange_file

        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x10\x00" * 4800)
        work_response = self.request(
            "post", "/api/works", json={"title": "OTIO交换验收", "goal": "检查可迁移时间线"},
            headers={"Idempotency-Key": "media-interchange-otio-work"},
        )
        self.assertEqual(work_response.status_code, 201, work_response.get_data(as_text=True))
        work_id = work_response.json["data"]["id"]
        material = self.upload_data(
            audio.getvalue(), filename="exchange-source.wav", key="media-interchange-otio-upload",
            work_id=work_id,
        )["material"]
        project = self.request(
            "post", "/api/media/projects",
            json={"title": "独立移动的播客工程", "kind": "podcast", "work_id": work_id},
            headers={"Idempotency-Key": "media-interchange-otio-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-interchange-otio-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        edit = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
                "timeline_start": {"num": 0, "den": 1},
            }, {"op": "add_caption", "start_sec": {"num": 0, "den": 1},
                "end_sec": asset["duration_sec"], "text": "开场"}]},
            headers={"Idempotency-Key": "media-interchange-otio-edit"},
        )
        self.assertEqual(edit.status_code, 200, edit.get_data(as_text=True))
        version = edit.json["data"]["version"]
        clip_id = edit.json["data"]["tracks"][0]["clips"][0]["clip_id"]
        from server.oevira.media_api import read_media_review_value, require_media_review_access
        review_anchor = {"kind": "media_clip", "object_id": clip_id, "property": "source_range",
                         "sample_range": {"start_sample": 0, "end_sample": 4800, "sample_rate": 48000}}
        canonical, review_value = read_media_review_value(
            self.store, self.identity, project_id, version, review_anchor,
        )
        self.assertEqual(canonical["locator"]["source_sha256"], material["sha256"])
        self.assertEqual(canonical["timebase"]["selected_value_domain"], "audio_samples")
        self.assertEqual(review_value["sample_selection"]["source_end_sample"], 4800)
        mapped, mapped_value = read_media_review_value(
            self.store, self.identity, project_id, version, canonical, mapped=True,
        )
        self.assertEqual(mapped["locator"], canonical["locator"])
        self.assertEqual(mapped_value, review_value)
        track_review, track_gain = read_media_review_value(
            self.store, self.identity, project_id, version,
            {"kind": "media_track", "object_id": "voiceover", "property": "gain"},
        )
        self.assertEqual(track_gain, 1.0)
        self.assertEqual(track_review["locator"]["track_id"], "voiceover")
        overlay_id = edit.json["data"]["overlays"][0]["overlay_id"]
        caption_anchor, caption_text = read_media_review_value(
            self.store, self.identity, project_id, version,
            {"kind": "media_caption", "object_id": overlay_id, "property": "text",
             "quote": "开场", "start": 0, "end": 2},
        )
        self.assertEqual(caption_text, "开场")
        with self.store.transaction(immediate=False) as connection:
            review_version = require_media_review_access(connection, self.identity, project_id, version)
        self.assertEqual(review_version["version"], version)
        preflight = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": version, "target_profile": "otio"},
        )
        self.assertEqual(preflight.status_code, 200, preflight.get_data(as_text=True))
        plan = preflight.json["data"]
        self.assertEqual(plan["state"], "requires_loss_acceptance")
        self.assertEqual(plan["target_host_verification"], "NOT_VERIFIED")
        caption_loss = next(item for item in plan["loss_items"] if item["object_type"] == "overlay")
        self.assertEqual(caption_loss["disposition"], "approximate")
        created = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": version, "target_profile": "otio",
                  "source_snapshot_sha256": plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": plan["required_accepted_loss_item_ids"]},
            headers={"Idempotency-Key": "media-interchange-otio-job"},
        )
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        interchange_id = created.json["data"]["id"]
        self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        completed = self.request("get", f"/api/media/interchanges/{interchange_id}")
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertEqual(completed.json["data"]["status"], "completed", completed.json)
        self.assertEqual(completed.json["data"]["loss_report"]["accepted_loss_item_ids"],
                         plan["required_accepted_loss_item_ids"])
        resolved = resolve_media_interchange_file(self.store, self.identity, interchange_id, target_work_id=work_id)
        self.assertEqual((resolved["project_id"], resolved["project_version"]), (project_id, version))
        self.assertEqual(hashlib.sha256(resolved["path"].read_bytes()).hexdigest(), resolved["sha256"])

        moved = self.temp_root / "moved-otio-package"
        moved.mkdir()
        with zipfile.ZipFile(resolved["path"], "r") as package:
            names = package.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts for name in names))
            package.extractall(moved)
        interchange_doc = json.loads((moved / "timeline.otio").read_text(encoding="utf-8"))
        self.assertEqual(interchange_doc["OTIO_SCHEMA"], "Timeline.1")
        self.assertEqual(interchange_doc["tracks"]["OTIO_SCHEMA"], "Stack.1")
        clips = []
        def collect(value):
            if isinstance(value, dict):
                if value.get("OTIO_SCHEMA") == "Clip.2":
                    clips.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(interchange_doc)
        self.assertEqual(len(clips), 1)
        self.assertTrue(any(item.get("OTIO_SCHEMA") == "Marker.3"
                            for item in interchange_doc["tracks"]["markers"]))
        relative_url = clips[0]["media_references"]["DEFAULT_MEDIA"]["target_url"]
        self.assertFalse(Path(relative_url).is_absolute())
        dependency = moved / Path(*relative_url.split("/"))
        self.assertTrue(dependency.is_file())
        self.assertEqual(hashlib.sha256(dependency.read_bytes()).hexdigest(), material["sha256"])
        native = json.loads((moved / "media" / "native-project.json").read_text(encoding="utf-8"))
        self.assertEqual(native["project"]["project_version"], version)
        self.assertEqual(native["dependencies"][0]["sha256"], material["sha256"])
        loss_report = json.loads((moved / "media" / "loss-report.json").read_text(encoding="utf-8"))
        self.assertEqual(loss_report["target_host_verification"], "NOT_VERIFIED")

        cmx_preflight = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": version, "target_profile": "cmx_3600",
                  "target_settings": {"frame_rate": "24/1", "timecode_mode": "NDF"}},
        )
        self.assertEqual(cmx_preflight.status_code, 200, cmx_preflight.get_data(as_text=True))
        cmx_plan = cmx_preflight.json["data"]
        tc_loss = next(item for item in cmx_plan["loss_items"] if item["property"] == "source_timecode_origin")
        self.assertEqual(tc_loss["disposition"], "approximate")
        cmx_created = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": version, "target_profile": "cmx_3600",
                  "target_settings": cmx_plan["target_settings"],
                  "source_snapshot_sha256": cmx_plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": cmx_plan["required_accepted_loss_item_ids"]},
            headers={"Idempotency-Key": "media-interchange-cmx-job"},
        )
        self.assertEqual(cmx_created.status_code, 202, cmx_created.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        cmx_result = self.request("get", f"/api/media/interchanges/{cmx_created.json['data']['id']}")
        self.assertEqual(cmx_result.json["data"]["status"], "completed", cmx_result.json)
        cmx_resolved = resolve_media_interchange_file(
            self.store, self.identity, cmx_created.json["data"]["id"], target_work_id=work_id,
        )
        with zipfile.ZipFile(cmx_resolved["path"], "r") as package:
            edl = package.read("sequence.edl").decode("utf-8")
            reel_map = json.loads(package.read("reel-map.json"))
        self.assertIn("FCM: NON-DROP FRAME", edl)
        self.assertIn("Source TC policy", edl)
        self.assertEqual(len(reel_map["reels"]), 1)
        self.assertTrue(next(iter(reel_map["reels"].values()))["assets"][0]["file_relative_timecode"])

        changed_caption = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": version, "operations": [{
                "op": "update_caption", "overlay_id": overlay_id,
                "start_sec": {"num": 0, "den": 1}, "end_sec": asset["duration_sec"],
                "text": "片头",
            }]},
            headers={"Idempotency-Key": "media-interchange-otio-caption-revision"},
        )
        self.assertEqual(changed_caption.status_code, 200, changed_caption.get_data(as_text=True))
        mapped_caption, revised_text = read_media_review_value(
            self.store, self.identity, project_id, changed_caption.json["data"]["version"],
            caption_anchor, mapped=True,
        )
        self.assertEqual(mapped_caption["locator"]["overlay_id"], overlay_id)
        self.assertEqual(revised_text, "片头")

        # A package-relative source still resolves when the original Material path is offline.
        material_row = self.store.row("SELECT relative_path FROM materials WHERE id=?", (material["id"],))
        original_path = self.store.root.joinpath(*material_row["relative_path"].split("/"))
        offline_path = original_path.with_suffix(original_path.suffix + ".offline")
        original_path.replace(offline_path)
        try:
            self.assertEqual(hashlib.sha256(dependency.read_bytes()).hexdigest(), material["sha256"])
        finally:
            offline_path.replace(original_path)

    def test_fcpxml_loss_acceptance_is_version_bound_and_package_carries_relative_media(self) -> None:
        ffmpeg = _ffmpeg_path()
        if not ffmpeg or not _ffprobe_path():
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        video_path = self.temp_root / "fcpxml-source.mp4"
        subprocess.run([
            ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            "color=c=red:s=320x180:r=24:d=1", "-frames:v", "24", "-an", "-c:v", "mpeg4",
            "-pix_fmt", "yuv420p", str(video_path),
        ], check=True, capture_output=True)
        material = self.upload_data(video_path.read_bytes(), filename=video_path.name,
                                    key="media-interchange-fcpx-upload")["material"]
        project = self.request(
            "post", "/api/media/projects",
            json={"title": "效果损失预检", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "media-interchange-fcpx-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-interchange-fcpx-source"},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "video-main",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "muted",
            }]},
            headers={"Idempotency-Key": "media-interchange-fcpx-insert"},
        )
        self.assertEqual(inserted.status_code, 200, inserted.get_data(as_text=True))
        clip_id = inserted.json["data"]["tracks"][0]["clips"][0]["clip_id"]
        effected = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": inserted.json["data"]["version"], "operations": [{
                "op": "set_video_effects", "clip_id": clip_id,
                "effects": [{"effect_id": "exposure-review", "kind": "exposure",
                             "parameters": {"exposure_stops": 0.5}}],
            }]},
            headers={"Idempotency-Key": "media-interchange-fcpx-effect"},
        )
        self.assertEqual(effected.status_code, 200, effected.get_data(as_text=True))
        chapter_edit = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": effected.json["data"]["version"], "operations": [{
                "op": "add_chapter", "chapter_id": "chapter-middle", "title": "关键段落",
                "start_sec": {"num": 1, "den": 2},
            }]},
            headers={"Idempotency-Key": "media-interchange-fcpx-chapter"},
        )
        self.assertEqual(chapter_edit.status_code, 200, chapter_edit.get_data(as_text=True))
        version = chapter_edit.json["data"]["version"]
        preflight = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": version, "target_profile": "fcpx_xml"},
        )
        self.assertEqual(preflight.status_code, 200, preflight.get_data(as_text=True))
        plan = preflight.json["data"]
        effect_losses = [item for item in plan["loss_items"] if item["object_type"] == "effect"]
        self.assertEqual(len(effect_losses), 1)
        self.assertEqual(effect_losses[0]["disposition"], "unsupported")
        denied = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": version, "target_profile": "fcpx_xml",
                  "target_settings": plan["target_settings"],
                  "source_snapshot_sha256": plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": []},
            headers={"Idempotency-Key": "media-interchange-fcpx-denied"},
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json["error"]["code"], "MEDIA_INTERCHANGE_LOSS_ACCEPTANCE_REQUIRED")
        changed = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": version, "operations": [{
                "op": "set_video_effects", "clip_id": clip_id,
                "effects": [{"effect_id": "exposure-review", "kind": "exposure",
                             "parameters": {"exposure_stops": 0.75}}],
            }]},
            headers={"Idempotency-Key": "media-interchange-fcpx-change-after-review"},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        stale = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": version, "target_profile": "fcpx_xml",
                  "target_settings": plan["target_settings"],
                  "source_snapshot_sha256": plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": plan["required_accepted_loss_item_ids"]},
            headers={"Idempotency-Key": "media-interchange-fcpx-stale-review"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "MEDIA_INTERCHANGE_PROJECT_CHANGED")

        current_version = changed.json["data"]["version"]
        current_preflight = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": current_version, "target_profile": "fcpx_xml",
                  "target_settings": plan["target_settings"]},
        )
        self.assertEqual(current_preflight.status_code, 200, current_preflight.get_data(as_text=True))
        current_plan = current_preflight.json["data"]
        accepted = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": current_version, "target_profile": "fcpx_xml",
                  "target_settings": current_plan["target_settings"],
                  "source_snapshot_sha256": current_plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": current_plan["required_accepted_loss_item_ids"]},
            headers={"Idempotency-Key": "media-interchange-fcpx-accepted"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.get_data(as_text=True))
        interchange_id = accepted.json["data"]["id"]
        self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        result = self.request("get", f"/api/media/interchanges/{interchange_id}")
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertEqual(result.json["data"]["status"], "completed")
        self.assertEqual(result.json["data"]["loss_report"]["target_host_verification"], "NOT_VERIFIED")
        file_response = self.request("get", f"/api/media/interchanges/{interchange_id}/file")
        self.assertEqual(file_response.status_code, 200, file_response.data[:200])
        archive_path = self.temp_root / "fcpxml-exchange.zip"
        archive_path.write_bytes(file_response.data)
        moved = self.temp_root / "moved-fcpxml-package"
        moved.mkdir()
        with zipfile.ZipFile(archive_path, "r") as package:
            names = package.namelist()
            self.assertTrue(any(name.endswith(".fcpxmld/Info.fcpxml") for name in names))
            package.extractall(moved)
        info_path = next(moved.rglob("Info.fcpxml"))
        xml_bytes = info_path.read_bytes()
        self.assertTrue(xml_bytes.startswith(b"<?xml"))
        root = ET.fromstring(xml_bytes)
        self.assertEqual(root.tag, "fcpxml")
        self.assertEqual(root.find(".//chapter-marker").get("value"), "关键段落")
        media_refs = [element.get("src") for element in root.findall(".//media-rep")]
        self.assertEqual(len(media_refs), 1)
        self.assertFalse(media_refs[0].startswith(("/", "file:")))
        bundled_source = info_path.parent.joinpath(*media_refs[0].split("/"))
        self.assertTrue(bundled_source.is_file())
        self.assertEqual(hashlib.sha256(bundled_source.read_bytes()).hexdigest(), material["sha256"])
        package_report = json.loads((moved / "media" / "loss-report.json").read_text(encoding="utf-8"))
        self.assertEqual(package_report["accepted_loss_item_ids"], current_plan["required_accepted_loss_item_ids"])
        self.assertTrue(any(item["object_type"] == "effect" and item["disposition"] == "unsupported"
                            for item in package_report["loss_items"]))

    def test_media_interchange_pause_and_cancel_retry_reuse_closed_package_checkpoint(self) -> None:
        import server.oevira.media_interchange as interchange_module

        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x20\x00" * 4800)
        material = self.upload_data(audio.getvalue(), filename="checkpoint.wav",
                                    key="media-interchange-control-upload")["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "交换检查点", "kind": "podcast"},
            headers={"Idempotency-Key": "media-interchange-control-project"},
        )
        self.assertEqual(project.status_code, 201, project.get_data(as_text=True))
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-interchange-control-source"},
        )
        asset = source.json["data"]["sources"][0]
        edit = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
            }]},
            headers={"Idempotency-Key": "media-interchange-control-edit"},
        )
        version = edit.json["data"]["version"]

        def enqueue(key: str) -> dict:
            preview = self.request(
                "post", f"/api/media/projects/{project_id}/interchanges/preflight",
                json={"version": version, "target_profile": "otio"},
            )
            self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
            body = preview.json["data"]
            created = self.request(
                "post", f"/api/media/projects/{project_id}/interchanges",
                json={"version": version, "target_profile": "otio",
                      "source_snapshot_sha256": body["source_snapshot_sha256"],
                      "accepted_loss_item_ids": []},
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
            return created.json["data"]

        original_zip_material = interchange_module._zip_material
        paused_job = enqueue("media-interchange-control-pause")
        pause_count = [0]
        def pause_after_dependency(*args, **kwargs):
            result = original_zip_material(*args, **kwargs)
            if pause_count[0] == 0:
                pause_count[0] += 1
                paused = self.request(
                    "post", f"/api/jobs/{paused_job['job']['id']}/pause",
                    json={"operation_id": "media-interchange-control-pause-op", "base_control_revision": 0},
                )
                self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
            return result
        with patch("server.oevira.media_interchange._zip_material", side_effect=pause_after_dependency):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        paused_status = self.request("get", f"/api/jobs/{paused_job['job']['id']}")
        self.assertEqual(paused_status.json["data"]["status"], "paused", paused_status.json)
        self.assertTrue(paused_status.json["data"]["pause_requested"])
        self.assertEqual(self.store.row("SELECT cancel_requested FROM jobs WHERE id=?",
                                        (paused_job["job"]["id"],))["cancel_requested"], 0)
        paused_record = self.store.row("SELECT * FROM media_interchanges WHERE id=?", (paused_job["id"],))
        self.assertEqual(paused_record["status"], "interrupted")
        self.assertTrue(paused_record["staging_path"])
        self.assertTrue(paused_record["sha256"])
        checkpoint = json.loads(paused_record["checkpoint_members_json"])
        self.assertTrue(checkpoint)

        resumed = self.request(
            "post", f"/api/jobs/{paused_job['job']['id']}/resume",
            json={"operation_id": "media-interchange-control-resume-op", "base_control_revision": 1},
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        with patch("server.oevira.media_interchange._zip_material", side_effect=AssertionError("completed dependency must be reused")):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        completed = self.request("get", f"/api/media/interchanges/{paused_job['id']}")
        self.assertEqual(completed.json["data"]["status"], "completed", completed.json)

        cancelled_job = enqueue("media-interchange-control-cancel")
        cancel_count = [0]
        def cancel_after_dependency(*args, **kwargs):
            result = original_zip_material(*args, **kwargs)
            if cancel_count[0] == 0:
                cancel_count[0] += 1
                cancelled = self.request(
                    "post", f"/api/jobs/{cancelled_job['job']['id']}/cancel",
                    json={"operation_id": "media-interchange-control-cancel-op", "base_control_revision": 0},
                )
                self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
            return result
        with patch("server.oevira.media_interchange._zip_material", side_effect=cancel_after_dependency):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        cancelled_status = self.request("get", f"/api/jobs/{cancelled_job['job']['id']}")
        self.assertEqual(cancelled_status.json["data"]["status"], "cancelled", cancelled_status.json)
        cancelled_record = self.store.row("SELECT * FROM media_interchanges WHERE id=?", (cancelled_job["id"],))
        self.assertEqual(cancelled_record["status"], "cancelled")
        self.assertTrue(cancelled_record["staging_path"])
        retried = self.request(
            "post", f"/api/jobs/{cancelled_job['job']['id']}/retry",
            json={"operation_id": "media-interchange-control-retry-op", "base_control_revision": 1},
        )
        self.assertEqual(retried.status_code, 202, retried.get_data(as_text=True))
        with patch("server.oevira.media_interchange._zip_material", side_effect=AssertionError("completed dependency must be reused")):
            self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        recovered = self.request("get", f"/api/media/interchanges/{cancelled_job['id']}")
        self.assertEqual(recovered.json["data"]["status"], "completed", recovered.json)

    def test_podcast_sidecar_bundle_binds_separate_chapter_revision_to_audio_revision(self) -> None:
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x18\x00" * 4800)
        material = self.upload_data(audio.getvalue(), filename="episode-sidecars.wav",
                                    key="media-episode-sidecars-upload")["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "可更新章节的单集", "kind": "podcast"},
            headers={"Idempotency-Key": "media-episode-sidecars-project"},
        )
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-episode-sidecars-source"},
        )
        asset = source.json["data"]["sources"][0]
        edited = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [
                {"op": "insert", "source_id": asset["asset_id"], "track_id": "voiceover",
                 "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                 "audio_role": "voiceover", "audio_stream_index": 0},
                {"op": "add_caption", "start_sec": {"num": 0, "den": 1},
                 "end_sec": asset["duration_sec"], "text": "你好"},
                {"op": "add_chapter", "chapter_id": "chapter-open", "title": "开场",
                 "start_sec": {"num": 0, "den": 1}},
                {"op": "set_transcript", "transcript": {
                    "transcript_id": "transcript-episode", "source_asset_id": asset["asset_id"],
                    "audio_stream_index": 0, "audio_channel_index": 0,
                    "language": "zh", "alignment_source": "human_supplied_unverified",
                    "review_state": "human_corrected", "task": "transcribe",
                    "words": [{"word_id": "word-hello", "text": "你好", "start_sample": 0,
                               "end_sample": 4800, "speaker_label": "主持人"}],
                }},
            ]},
            headers={"Idempotency-Key": "media-episode-sidecars-content"},
        )
        self.assertEqual(edited.status_code, 200, edited.get_data(as_text=True))
        audio_version = edited.json["data"]["version"]
        render = self.request(
            "post", f"/api/media/projects/{project_id}/renders",
            json={"version": audio_version, "output_kind": "audio", "format": "wav",
                  "output_spec": {"sample_rate": 48000, "audio_channels": 2}},
            headers={"Idempotency-Key": "media-episode-sidecars-render"},
        )
        self.assertEqual(render.status_code, 202, render.get_data(as_text=True))
        render_id = render.json["data"]["render_id"]
        self.assertTrue(run_once(self.app, allowed_kinds=("media_render",)))

        chapter_revision = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": audio_version, "operations": [{
                "op": "update_chapter", "chapter_id": "chapter-open", "title": "节目开始",
                "start_sec": {"num": 0, "den": 1},
            }]},
            headers={"Idempotency-Key": "media-episode-sidecars-chapter-revision"},
        )
        self.assertEqual(chapter_revision.status_code, 200, chapter_revision.get_data(as_text=True))
        sidecar_version = chapter_revision.json["data"]["version"]
        sidecar_response = self.request(
            "get", f"/api/media/renders/{render_id}/podcast-sidecars?project_version={sidecar_version}",
        )
        self.assertEqual(sidecar_response.status_code, 200, sidecar_response.data[:200])
        with zipfile.ZipFile(io.BytesIO(sidecar_response.data), "r") as package:
            names = set(package.namelist())
            self.assertTrue({"sidecar-manifest.json", "chapters.json", "chapters.provenance.json",
                             "captions.vtt", "transcript.vtt", "transcript.json", "storyboard.json"} <= names)
            chapter_data = json.loads(package.read("chapters.json"))
            transcript_vtt = package.read("transcript.vtt").decode("utf-8")
            storyboard = json.loads(package.read("storyboard.json"))
            manifest = json.loads(package.read("sidecar-manifest.json"))
        self.assertEqual(chapter_data["version"], "1.2.0")
        self.assertEqual(chapter_data["chapters"][0]["title"], "节目开始")
        self.assertEqual(manifest["episode_ref"], f"urn:oevira:media-episode:{project_id}")
        self.assertEqual(manifest["audio_revision"]["render_id"], render_id)
        self.assertEqual(manifest["audio_revision"]["project_version"], audio_version)
        self.assertEqual(manifest["sidecar_project_version"], sidecar_version)
        self.assertIn("<v 主持人>你好</v>", transcript_vtt)
        self.assertFalse(storyboard["inference_performed"])
        self.assertEqual(storyboard["project_version"], sidecar_version)
        self.assertEqual(storyboard["story_nodes"][0]["source"]["sha256"], material["sha256"])
        self.assertEqual(self.store.row("SELECT COUNT(*) AS n FROM media_renders WHERE project_id=?",
                                        (project_id,))["n"], 1)

        clip_id = edited.json["data"]["tracks"][0]["clips"][0]["clip_id"]
        audio_changed = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": sidecar_version, "operations": [{
                "op": "audio_mix", "clip_id": clip_id,
                "mix": {"volume": 0.5, "fade_in_sec": 0, "fade_out_sec": 0, "ducking": False},
            }]},
            headers={"Idempotency-Key": "media-episode-sidecars-audio-change"},
        )
        self.assertEqual(audio_changed.status_code, 200, audio_changed.get_data(as_text=True))
        incompatible = self.request(
            "get", f"/api/media/renders/{render_id}/podcast-sidecars?project_version={audio_changed.json['data']['version']}",
        )
        self.assertEqual(incompatible.status_code, 409, incompatible.get_data(as_text=True))
        self.assertEqual(incompatible.json["error"]["code"], "MEDIA_EPISODE_AUDIO_REVISION_INCOMPATIBLE")

    def test_cmx3600_emits_dissolve_and_linear_motion_records(self) -> None:
        ffmpeg = _ffmpeg_path()
        if not ffmpeg or not _ffprobe_path():
            self.skipTest("The approved local FFmpeg 9.0.2 runtime is unavailable")
        video_path = self.temp_root / "cmx-motion-source.mp4"
        subprocess.run([
            ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=24:d=2", "-frames:v", "48", "-an", "-c:v", "mpeg4",
            "-pix_fmt", "yuv420p", str(video_path),
        ], check=True, capture_output=True)
        material = self.upload_data(video_path.read_bytes(), filename=video_path.name,
                                    key="media-interchange-cmx-motion-upload")["material"]
        project = self.request(
            "post", "/api/media/projects", json={"title": "CMX速率和转场", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-project"},
        )
        project_id = project.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources", json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-source"},
        )
        asset_id = source.json["data"]["sources"][0]["asset_id"]
        inserted = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [
                {"op": "insert", "source_id": asset_id, "track_id": "video-main",
                 "source_in": {"num": 0, "den": 1}, "source_out": {"num": 1, "den": 1},
                 "audio_role": "muted"},
                {"op": "insert", "source_id": asset_id, "track_id": "video-main",
                 "source_in": {"num": 1, "den": 1}, "source_out": {"num": 2, "den": 1},
                 "audio_role": "muted"},
            ]},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-insert"},
        )
        clips = inserted.json["data"]["tracks"][0]["clips"]
        speed = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": inserted.json["data"]["version"], "operations": [{
                "op": "playback_rate", "clip_id": clips[1]["clip_id"], "playback_rate": {"num": 1, "den": 2},
            }]},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-speed"},
        )
        self.assertEqual(speed.status_code, 200, speed.get_data(as_text=True))
        transition = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": speed.json["data"]["version"], "operations": [{
                "op": "transition", "from_clip": clips[0]["clip_id"], "to_clip": clips[1]["clip_id"],
                "transition_type": "dissolve", "duration_sec": {"num": 1, "den": 2},
                "reason": "Explicit CMX handoff transition",
            }]},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-transition"},
        )
        self.assertEqual(transition.status_code, 200, transition.get_data(as_text=True))
        version = transition.json["data"]["version"]
        preview = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges/preflight",
            json={"version": version, "target_profile": "cmx_3600",
                  "target_settings": {"frame_rate": "24/1", "timecode_mode": "NDF"}},
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        plan = preview.json["data"]
        transition_loss = next(item for item in plan["loss_items"] if item["object_type"] == "transition")
        speed_loss = next(item for item in plan["loss_items"] if item["property"] == "linear_playback_rate")
        self.assertEqual(transition_loss["disposition"], "approximate")
        self.assertEqual(speed_loss["disposition"], "approximate")
        created = self.request(
            "post", f"/api/media/projects/{project_id}/interchanges",
            json={"version": version, "target_profile": "cmx_3600",
                  "target_settings": plan["target_settings"],
                  "source_snapshot_sha256": plan["source_snapshot_sha256"],
                  "accepted_loss_item_ids": plan["required_accepted_loss_item_ids"]},
            headers={"Idempotency-Key": "media-interchange-cmx-motion-job"},
        )
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        self.assertTrue(run_once(self.app, allowed_kinds=("media_interchange",)))
        resolved = self.request("get", f"/api/media/interchanges/{created.json['data']['id']}")
        self.assertEqual(resolved.json["data"]["status"], "completed", resolved.json)
        package_path = self.temp_root / "cmx-motion.zip"
        package_path.write_bytes(self.request(
            "get", f"/api/media/interchanges/{created.json['data']['id']}/file",
        ).data)
        with zipfile.ZipFile(package_path, "r") as package:
            edl = package.read("sequence.edl").decode("utf-8")
        self.assertIn(" D 012 ", edl)
        self.assertIn("\r\nM2 ", edl)
        self.assertIn("12 ", next(line for line in edl.splitlines() if line.startswith("M2 ")))

    def test_editable_storyboard_keeps_artifact_material_clip_links_in_project_versions(self) -> None:
        work = self.request(
            "post", "/api/works", json={"title": "播客分镜工作", "goal": "保留提纲和真实声音"},
            headers={"Idempotency-Key": "media-storyboard-work"},
        ).json["data"]
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x24\x00" * 4800)
        material = self.upload_data(audio.getvalue(), filename="storyboard.wav",
                                    key="media-storyboard-material", work_id=work["id"])["material"]
        artifact = self.request(
            "post", "/api/artifacts",
            json={"title": "来宾前采提纲", "body": "你当时怎样作出选择？", "citations": [], "work_id": work["id"]},
            headers={"Idempotency-Key": "media-storyboard-outline"},
        ).json["data"]
        project = self.request(
            "post", "/api/media/projects",
            json={"title": "分镜与真实采访", "kind": "podcast", "work_id": work["id"]},
            headers={"Idempotency-Key": "media-storyboard-project"},
        ).json["data"]
        project_id = project["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources",
            json={"base_version": 1, "material_id": material["id"]},
            headers={"Idempotency-Key": "media-storyboard-source"},
        ).json["data"]["sources"][0]
        edited = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": 2, "operations": [{
                "op": "insert", "source_id": source["asset_id"], "track_id": "voiceover",
                "source_in": {"num": 0, "den": 1}, "source_out": source["duration_sec"],
                "audio_role": "voiceover", "audio_stream_index": 0,
            }]},
            headers={"Idempotency-Key": "media-storyboard-insert-clip"},
        ).json["data"]
        clip_id = edited["tracks"][0]["clips"][0]["clip_id"]
        story = self.request(
            "post", f"/api/media/projects/{project_id}/storyboard/edits",
            json={"base_version": edited["version"], "operations": [{
                "op": "add_node", "node": {"kind": "question", "title": "追问选择",
                                                "question": "你当时怎样作出选择？", "cue_id": "choice",
                                                "speaker_label": "来宾", "status": "planned"},
            }]},
            headers={"Idempotency-Key": "media-storyboard-add-question"},
        )
        self.assertEqual(story.status_code, 200, story.get_data(as_text=True))
        story_version = story.json["data"]["project_version"]
        beat_id = story.json["data"]["storyboard"]["nodes"][0]["beat_id"]
        linked = self.request(
            "post", f"/api/media/projects/{project_id}/storyboard/edits",
            json={"base_version": story_version, "operations": [
                {"op": "add_source_ref", "beat_id": beat_id,
                 "source_ref": {"kind": "material", "material_id": material["id"], "role": "guest_audio"}},
                {"op": "add_source_ref", "beat_id": beat_id,
                 "source_ref": {"kind": "artifact", "artifact_id": artifact["id"],
                                 "artifact_version": 1, "role": "interview_outline"}},
                {"op": "link_clip", "beat_id": beat_id, "clip_id": clip_id, "role": "answer_take"},
            ]},
            headers={"Idempotency-Key": "media-storyboard-link-sources"},
        )
        self.assertEqual(linked.status_code, 200, linked.get_data(as_text=True))
        node = linked.json["data"]["storyboard"]["nodes"][0]
        self.assertEqual({item["kind"] for item in node["source_refs"]}, {"material", "artifact"})
        clip_ref = node["clip_refs"][0]
        self.assertEqual(clip_ref["source_sha256"], material["sha256"])
        self.assertIsNotNone(self.store.row(
            "SELECT 1 FROM work_artifacts WHERE work_id=? AND artifact_id=?", (work["id"], artifact["id"]),
        ))
        take_response = self.request(
            "post", f"/api/media/projects/{project_id}/recordings",
            json={"base_version": linked.json["data"]["project_version"], "cue_id": "choice",
                  "speaker_label": "来宾", "track_kind": "voiceover",
                  "timeline_start_sec": {"num": 1, "den": 10}, "sample_rate": 48000,
                  "channels": 1, "sample_format": "pcm_s16le"},
            headers={"Idempotency-Key": "media-storyboard-recording-take"},
        )
        self.assertEqual(take_response.status_code, 201, take_response.get_data(as_text=True))
        take = take_response.json["data"]
        epoch = "media-storyboard-recording-epoch"
        started = self.request(
            "post", f"/api/media/recordings/{take['id']}/start", json={"capture_epoch_id": epoch},
            headers={"Idempotency-Key": "media-storyboard-recording-start"},
        )
        self.assertEqual(started.status_code, 200, started.get_data(as_text=True))
        appended = self.request(
            "post", f"/api/media/recordings/{take['id']}/segments",
            json={"sequence": 0, "material_id": material["id"], "capture_epoch_id": epoch,
                  "capture_sample_start": 0, "sample_count": 4800},
            headers={"Idempotency-Key": "media-storyboard-recording-segment"},
        )
        self.assertEqual(appended.status_code, 201, appended.get_data(as_text=True))
        stopped = self.request(
            "post", f"/api/media/recordings/{take['id']}/stop",
            json={"last_confirmed_sequence": 0, "native_capture_stopped": True},
            headers={"Idempotency-Key": "media-storyboard-recording-stop"},
        )
        self.assertEqual(stopped.status_code, 200, stopped.get_data(as_text=True))
        completed_take = self.request(
            "post", f"/api/media/recordings/{take['id']}/complete", json={},
            headers={"Idempotency-Key": "media-storyboard-recording-complete"},
        )
        self.assertEqual(completed_take.status_code, 201, completed_take.get_data(as_text=True))
        linked_take = self.request(
            "post", f"/api/media/projects/{project_id}/storyboard/edits",
            json={"base_version": completed_take.json["data"]["project"]["version"], "operations": [
                {"op": "link_take", "beat_id": beat_id, "session_id": take["id"], "role": "guest_answer"},
                {"op": "update_node", "beat_id": beat_id, "changes": {"status": "recorded"}},
            ]},
            headers={"Idempotency-Key": "media-storyboard-link-completed-take"},
        )
        self.assertEqual(linked_take.status_code, 200, linked_take.get_data(as_text=True))
        storyboard_node = linked_take.json["data"]["storyboard"]["nodes"][0]
        self.assertEqual(storyboard_node["status"], "recorded")
        self.assertEqual(storyboard_node["recording_refs"][0]["session_id"], take["id"])
        self.assertTrue(storyboard_node["recording_refs"][0]["clip_ids"])
        take_export = self.request(
            "get", f"/api/media/projects/{project_id}/export?version={linked_take.json['data']['project_version']}",
        )
        self.assertEqual(take_export.status_code, 200, take_export.get_data(as_text=True))
        refused_take_import = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": json.loads(take_export.data), "work_id": work["id"]},
            headers={"Idempotency-Key": "media-storyboard-take-import-needs-relink"},
        )
        self.assertEqual(refused_take_import.status_code, 409)
        self.assertEqual(refused_take_import.json["error"]["code"], "MEDIA_STORYBOARD_IMPORT_TAKE_RELINK_REQUIRED")
        detached_take = self.request(
            "post", f"/api/media/projects/{project_id}/storyboard/edits",
            json={"base_version": linked_take.json["data"]["project_version"], "operations": [
                {"op": "unlink_take", "beat_id": beat_id,
                 "link_id": storyboard_node["recording_refs"][0]["link_id"]},
                {"op": "update_node", "beat_id": beat_id, "changes": {"status": "edited"}},
            ]},
            headers={"Idempotency-Key": "media-storyboard-detach-portable-take"},
        )
        self.assertEqual(detached_take.status_code, 200, detached_take.get_data(as_text=True))
        from server.oevira.media_api import read_media_review_value
        anchor, old_question = read_media_review_value(
            self.store, self.identity, project_id, detached_take.json["data"]["project_version"],
            {"kind": "media_storyboard_node", "object_id": beat_id, "property": "question"},
        )
        self.assertEqual(old_question, "你当时怎样作出选择？")
        changed = self.request(
            "post", f"/api/media/projects/{project_id}/storyboard/edits",
            json={"base_version": detached_take.json["data"]["project_version"], "operations": [{
                "op": "update_node", "beat_id": beat_id,
                "changes": {"question": "后来你怎样确认这个选择？"},
            }]},
            headers={"Idempotency-Key": "media-storyboard-revise-question"},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        self.assertEqual(changed.json["data"]["storyboard"]["nodes"][0]["clip_refs"], [clip_ref])
        _, revised_question = read_media_review_value(
            self.store, self.identity, project_id, changed.json["data"]["project_version"], anchor, mapped=True,
        )
        self.assertEqual(revised_question, "后来你怎样确认这个选择？")
        base = changed.json["data"]["project_version"]
        refused = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": base, "operations": [{"op": "delete", "clip_id": clip_id}]},
            headers={"Idempotency-Key": "media-storyboard-delete-linked-clip"},
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json["error"]["code"], "MEDIA_STORYBOARD_CLIP_REFERENCE_MISSING")
        atomic = self.request(
            "post", f"/api/media/projects/{project_id}/edits",
            json={"base_version": base, "operations": [{"op": "delete", "clip_id": clip_id}],
                  "storyboard_operations": [{"op": "unlink_clip", "beat_id": beat_id,
                                               "link_id": clip_ref["link_id"]},
                                          {"op": "update_node", "beat_id": beat_id,
                                           "changes": {"status": "planned"}}]},
            headers={"Idempotency-Key": "media-storyboard-delete-and-unlink"},
        )
        self.assertEqual(atomic.status_code, 200, atomic.get_data(as_text=True))
        self.assertEqual(atomic.json["data"]["storyboard"]["nodes"][0]["clip_refs"], [])
        exported = self.request("get", f"/api/media/projects/{project_id}/export?version={atomic.json['data']['version']}")
        self.assertEqual(exported.status_code, 200, exported.get_data(as_text=True))
        manifest = json.loads(exported.data)
        self.assertEqual(manifest["storyboard"]["nodes"][0]["beat_id"], beat_id)
        imported = self.request(
            "post", "/api/media/projects/import",
            json={"manifest": manifest, "work_id": work["id"], "title": "分镜与真实采访副本"},
            headers={"Idempotency-Key": "media-storyboard-project-import"},
        )
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        self.assertEqual(imported.json["data"]["storyboard"]["nodes"][0]["beat_id"], beat_id)
        self.assertEqual(imported.json["data"]["storyboard"]["nodes"][0]["source_refs"],
                         atomic.json["data"]["storyboard"]["nodes"][0]["source_refs"])
        # Recreate the app over the same isolated data root and read the saved
        # storyboard from its immutable Media project version.
        reopened_app = create_app(self.data_root, expected_port=8317)
        reopened_app.config.update(TESTING=True)
        reopened_client = reopened_app.test_client()
        reopened = reopened_client.get(
            f"/api/media/projects/{project_id}?version={atomic.json['data']['version']}",
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self.token}", "Origin": ORIGIN},
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        reopened_data = reopened.json["data"]
        self.assertEqual(reopened_data["storyboard"], atomic.json["data"]["storyboard"])
        self.assertEqual(reopened_data["tracks"], atomic.json["data"]["tracks"])


class MediaSidecarArchiveTests(unittest.TestCase):
    def test_same_version_sidecar_archive_is_byte_identical_after_delay(self) -> None:
        from server.oevira.media_api import _package_media_episode_sidecars

        members = {
            "chapters.json": b'{"version":"1.2.0","chapters":[]}',
            "transcript.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:00.100\n<v Host>Hello</v>\n",
        }
        manifest = {
            "schema": "oevira.media.podcast.sidecar-bundle.v1",
            "episode_ref": "urn:oevira:media-episode:episode-one",
            "project_id": "episode-one",
            "sidecar_project_version": 5,
            "audio_revision": {"render_id": "render-one", "project_version": 3,
                               "sha256": "a" * 64, "format": "wav"},
            "transcript_revision": "b" * 64,
            "chapter_revision": "c" * 64,
        }
        first = _package_media_episode_sidecars(members, manifest)
        time.sleep(1.1)
        second = _package_media_episode_sidecars(members, manifest)
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
        with zipfile.ZipFile(io.BytesIO(first), "r") as archive:
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))


if __name__ == "__main__":
    unittest.main()
