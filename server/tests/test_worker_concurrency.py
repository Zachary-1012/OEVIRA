from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

from server.oevira import create_app
from server.oevira.acquisition import AcquisitionError
from server.oevira.auth import issue_local_capability, utc_now
from server.oevira.media import _ffmpeg_path, _ffprobe_path
from server.oevira.worker import (
    _claim_job,
    _set_job_error,
    install_worker_schema,
    run_forever,
    run_once,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".runtime" / "worker-concurrency-tests"
BASE_URL = "http://127.0.0.1:8317"


def _iso_before(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


class WorkerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self._previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
        for key in self._previous_temp:
            os.environ[key] = str(RUNTIME_ROOT)
        self._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(RUNTIME_ROOT)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=RUNTIME_ROOT)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / "data", expected_port=8317)
        self.app.config["TESTING"] = True
        self.store = self.app.extensions["oevira.store"]
        install_worker_schema(self.store)
        self.client = self.app.test_client()

        capability_file = self.store.root / "launch.capability"
        issue_local_capability(self.store, capability_file)
        response = self.client.post(
            "/api/local-session",
            base_url=BASE_URL,
            json={"capability": capability_file.read_text(encoding="ascii"), "display_name": "Worker concurrency test"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.identity = response.json["data"]
        self.token = self.identity["token"]

    def tearDown(self):
        self.temporary.cleanup()
        tempfile.tempdir = self._previous_tempdir
        for key, value in self._previous_temp.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, method: str, path: str, *, data=None, headers=None):
        request_headers = {"Authorization": f"Bearer {self.token}"}
        if headers:
            request_headers.update(headers)
        kwargs = {"base_url": BASE_URL, "headers": request_headers}
        if data is not None:
            if isinstance(data, (bytes, bytearray)):
                kwargs["data"] = bytes(data)
                kwargs["content_type"] = "application/octet-stream"
            else:
                kwargs["json"] = data
        response = getattr(self.client, method)(path, **kwargs)
        response.get_data()
        response.close()
        return response

    def enqueue_job(self, kind: str, *, status: str = "queued", heartbeat: str | None = None,
                    updated: str | None = None, generation: int = 0) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(id, workspace_id, user_id, kind, status, payload_json, request_hash, "
                "idempotency_key, attempts, lease_generation, created_at, updated_at, lease_heartbeat_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, self.identity["workspace"]["id"], self.identity["user"]["id"], kind, status,
                    "{}", "worker-concurrency-test", f"{kind}-{uuid.uuid4().hex}",
                    1 if generation else 0, generation, now, updated or now, heartbeat,
                ),
            )
        return job_id

    def start_worker(self, stop_event: threading.Event) -> threading.Thread:
        worker = threading.Thread(target=run_forever, args=(self.app, stop_event), daemon=True)
        worker.start()
        return worker

    def wait_for(self, predicate, *, timeout: float = 15.0, message: str):
        deadline = time.time() + timeout
        last_value = None
        while time.time() < deadline:
            last_value = predicate()
            if last_value:
                return last_value
            time.sleep(0.025)
        self.fail(f"Timed out waiting for {message}; last observation: {last_value!r}")

    def test_stale_progress_with_a_live_lease_is_not_reaped(self):
        stale_progress = _iso_before(20 * 60)
        live_id = self.enqueue_job(
            "intelligence_turn", status="running", generation=4,
            updated=stale_progress, heartbeat=utc_now(),
        )
        stale_id = self.enqueue_job(
            "intelligence_turn", status="running", generation=7,
            updated=utc_now(), heartbeat=_iso_before(20 * 60),
        )

        self.assertIsNone(_claim_job(self.store, allowed_kinds=[]))
        live = self.store.row("SELECT status, lease_generation, updated_at, lease_heartbeat_at FROM jobs WHERE id = ?", (live_id,))
        stale = self.store.row("SELECT status FROM jobs WHERE id = ?", (stale_id,))
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["lease_generation"], 4)
        self.assertEqual(live["updated_at"], stale_progress)
        self.assertNotEqual(live["lease_heartbeat_at"], live["updated_at"])
        self.assertEqual(stale["status"], "interrupted")

    def test_bounded_lanes_reach_only_the_configured_parallelism(self):
        queued = []
        for kind, count in (
            ("acquire_url", 4),
            ("media_render", 3),
            ("intelligence_turn", 2),
            ("business_action", 2),
        ):
            queued.extend((self.enqueue_job(kind), kind) for _ in range(count))

        lanes = {
            "acquire_url": "acquisition",
            "media_render": "media",
            "intelligence_turn": "intelligence",
            "business_action": "work",
        }
        condition = threading.Condition()
        release = threading.Event()
        active_total = 0
        active_by_lane = {"acquisition": 0, "media": 0, "intelligence": 0, "work": 0}
        maximum_total = 0
        maximum_by_lane = dict(active_by_lane)
        completed = 0

        def controlled_executor(app, claim):
            nonlocal active_total, maximum_total, completed
            lane = lanes[claim["kind"]]
            with condition:
                active_total += 1
                active_by_lane[lane] += 1
                maximum_total = max(maximum_total, active_total)
                maximum_by_lane[lane] = max(maximum_by_lane[lane], active_by_lane[lane])
                condition.notify_all()
            try:
                if not release.wait(20):
                    raise TimeoutError("The concurrency test did not release its controlled job")
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE jobs SET status='completed', updated_at=? WHERE id=? AND lease_generation=? AND status='running'",
                        (utc_now(), claim["id"], claim["generation"]),
                    )
            finally:
                with condition:
                    active_total -= 1
                    active_by_lane[lane] -= 1
                    completed += 1
                    condition.notify_all()

        stop_event = threading.Event()
        worker = self.start_worker(stop_event)
        try:
            with patch("server.oevira.worker._execute_claim", side_effect=controlled_executor):
                with condition:
                    self.assertTrue(condition.wait_for(lambda: active_total == 5, timeout=10), "five lane slots should start")
                self.assertEqual(maximum_total, 5)
                self.assertEqual(maximum_by_lane, {"acquisition": 2, "media": 1, "intelligence": 1, "work": 1})
                release.set()
                self.wait_for(
                    lambda: self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE status='completed'")["count"] == len(queued),
                    message="all bounded-lane jobs to complete",
                )
                stop_event.set()
                worker.join(timeout=5)
        finally:
            release.set()
            stop_event.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "The worker supervisor should stop after all controlled jobs finish")
        self.assertEqual(self.app.extensions["oevira.worker_state"]["status"], "stopped")
        self.assertEqual(self.store.row("SELECT COUNT(*) AS count FROM jobs WHERE status='completed'")["count"], len(queued))

    def test_user_cancellation_stays_cancelled_but_application_stop_is_interrupted(self):
        user_job = self.enqueue_job("business_action")
        gates = {user_job: threading.Event()}
        started = {user_job: threading.Event()}
        execution_counts = {user_job: 0}
        execution_lock = threading.Lock()

        def cooperative_executor(app, claim):
            job_id = claim["id"]
            with execution_lock:
                execution_counts[job_id] += 1
            started[job_id].set()
            if not gates[job_id].wait(10):
                raise TimeoutError("A controlled cancellation job was not released")
            _set_job_error(self.store, job_id, claim["generation"], "TEST_EXECUTION_ERROR", "Test job returned after cancellation.")

        first_stop = threading.Event()
        first_worker = self.start_worker(first_stop)
        try:
            with patch("server.oevira.worker._execute_claim", side_effect=cooperative_executor):
                self.assertTrue(started[user_job].wait(5), "The user-cancel job should be claimed")
                cancelled = self.request("post", f"/api/jobs/{user_job}/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
                self.assertEqual(cancelled.json["data"]["status"], "cancel_requested")
                first_stop.set()
                self.wait_for(
                    lambda: self.app.extensions["oevira.worker_state"]["status"] == "stopping",
                    message="the first worker to enter its shutdown path",
                )
                gates[user_job].set()
                first_worker.join(timeout=5)
        finally:
            gates[user_job].set()
            first_stop.set()
            first_worker.join(timeout=5)
        self.assertFalse(first_worker.is_alive())
        user_result = self.store.row("SELECT status, cancel_requested, lease_generation FROM jobs WHERE id=?", (user_job,))
        self.assertEqual(user_result["status"], "cancelled")
        self.assertEqual(user_result["cancel_requested"], 1)
        self.assertEqual(user_result["lease_generation"], 1)

        app_job = self.enqueue_job("intelligence_turn")
        gates[app_job] = threading.Event()
        started[app_job] = threading.Event()
        execution_counts[app_job] = 0
        second_stop = threading.Event()
        second_worker = self.start_worker(second_stop)
        try:
            with patch("server.oevira.worker._execute_claim", side_effect=cooperative_executor):
                self.assertTrue(started[app_job].wait(5), "The app-stop job should be claimed")
                second_stop.set()
                self.wait_for(
                    lambda: self.store.row("SELECT status FROM jobs WHERE id=?", (app_job,))["status"] == "cancel_requested",
                    message="the supervisor to request cooperative shutdown cancellation",
                )
                gates[app_job].set()
                second_worker.join(timeout=5)
        finally:
            gates[app_job].set()
            second_stop.set()
            second_worker.join(timeout=5)
        self.assertFalse(second_worker.is_alive())
        app_result = self.store.row("SELECT status, cancel_requested, error_code, lease_generation FROM jobs WHERE id=?", (app_job,))
        self.assertEqual(app_result["status"], "interrupted")
        self.assertEqual(app_result["cancel_requested"], 0)
        self.assertEqual(app_result["error_code"], "WORKER_STOPPED")
        self.assertEqual(app_result["lease_generation"], 1)
        self.assertEqual(execution_counts, {user_job: 1, app_job: 1})
        self.assertFalse(run_once(self.app), "Neither cancellation nor app stop should requeue a completed claim")

    def test_unconfirmed_stop_fences_a_late_commit_without_requeue(self):
        job_id = self.enqueue_job("intelligence_turn")
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        late_update_rows = []

        def uncooperative_executor(app, claim):
            started.set()
            if not release.wait(15):
                raise TimeoutError("The fenced job was not released")
            with self.store.transaction() as connection:
                changed = connection.execute(
                    "UPDATE jobs SET status='completed', updated_at=? WHERE id=? AND lease_generation=? AND status IN ('running','cancel_requested')",
                    (utc_now(), claim["id"], claim["generation"]),
                ).rowcount
            late_update_rows.append(changed)
            finished.set()

        fake_time = [0.0]
        fake_time_lock = threading.Lock()

        def monotonic_advances_for_supervisor():
            with fake_time_lock:
                fake_time[0] += 1.0
                return fake_time[0]

        stop_event = threading.Event()
        with patch("server.oevira.worker._execute_claim", side_effect=uncooperative_executor), \
             patch("server.oevira.worker.time.monotonic", side_effect=monotonic_advances_for_supervisor):
            worker = self.start_worker(stop_event)
            try:
                self.assertTrue(started.wait(5), "The fenced job should be running before stop")
                stop_event.set()
                worker.join(timeout=4)
            finally:
                release.set()
                stop_event.set()
                worker.join(timeout=4)
        self.assertFalse(worker.is_alive(), "The supervisor must leave after its shutdown deadline")
        fenced = self.store.row("SELECT status, error_code, lease_generation FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(fenced["status"], "interrupted")
        self.assertEqual(fenced["error_code"], "WORKER_STOP_UNCONFIRMED")
        self.assertEqual(fenced["lease_generation"], 2)
        self.assertTrue(finished.wait(3), "The late worker callback should finish after release")
        self.assertEqual(late_update_rows, [0], "The old generation must not publish a late result")
        final = self.store.row("SELECT status, lease_generation FROM jobs WHERE id=?", (job_id,))
        self.assertEqual((final["status"], final["lease_generation"]), ("interrupted", 2))
        self.assertFalse(run_once(self.app), "An unconfirmed stopped job must not be requeued")

    def _request(self, method: str, path: str, *, data=None, headers=None):
        return self.request(method, path, data=data, headers=headers)

    def _upload_material(self, source_path: Path) -> str:
        content = source_path.read_bytes()
        response = self.request("post", "/api/uploads", data={
            "kind": "media",
            "title": "isolated concurrency source",
            "filename": source_path.name,
            "expected_size": len(content),
            "expected_sha256": hashlib.sha256(content).hexdigest(),
        }, headers={"Idempotency-Key": "concurrency-upload-" + uuid.uuid4().hex})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        upload_id = response.json["data"]["id"]
        chunk_size = 1024 * 1024
        for offset in range(0, len(content), chunk_size):
            chunk = content[offset:offset + chunk_size]
            response = self.request(
                "patch", f"/api/uploads/{upload_id}", data=chunk,
                headers={"Upload-Offset": str(offset), "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            )
            self.assertEqual(response.status_code, 204, response.get_data(as_text=True))
        completed = self.request("post", f"/api/uploads/{upload_id}/complete")
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        return completed.json["data"]["material"]["id"]

    def _queue_real_media_render(self, duration: int = 15) -> tuple[str, str, str]:
        ffmpeg = _ffmpeg_path()
        ffprobe = _ffprobe_path()
        if not ffmpeg or not ffprobe:
            self.skipTest("The approved local FFmpeg/FFprobe runtime is unavailable")
        source_path = self.root / "controlled-worker-source.mp4"
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=24:duration={duration}",
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
                "-shortest", "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                "-af", "asetpts=PTS-STARTPTS", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-color_range", "tv", "-colorspace", "bt709",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(source_path),
            ],
            check=True,
            capture_output=True,
            timeout=90,
            shell=False,
        )
        material_id = self._upload_material(source_path)
        created = self.request(
            "post", "/api/media/projects", data={"title": "bounded worker render", "kind": "video", "format_ratio": "16:9"},
            headers={"Idempotency-Key": "concurrency-project-" + uuid.uuid4().hex},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        project_id = created.json["data"]["id"]
        source = self.request(
            "post", f"/api/media/projects/{project_id}/sources", data={"base_version": 1, "material_id": material_id},
            headers={"Idempotency-Key": "concurrency-source-" + uuid.uuid4().hex},
        )
        self.assertEqual(source.status_code, 200, source.get_data(as_text=True))
        asset = source.json["data"]["sources"][0]
        edit = self.request(
            "post", f"/api/media/projects/{project_id}/edits", data={
                "base_version": 2,
                "operations": [{
                    "op": "insert", "source_id": asset["asset_id"], "track_id": "video-main",
                    "source_in": {"num": 0, "den": 1}, "source_out": asset["duration_sec"],
                    "audio_role": "original",
                }],
            },
            headers={"Idempotency-Key": "concurrency-edit-" + uuid.uuid4().hex},
        )
        self.assertEqual(edit.status_code, 200, edit.get_data(as_text=True))
        queued = self.request(
            "post", f"/api/media/projects/{project_id}/renders", data={
                "version": 3,
                "output_kind": "video",
                "format": "mp4",
                "output_spec": {
                    "frame_rate": {"num": 24, "den": 1},
                    "sample_rate": 44100,
                    "audio_channels": 2,
                    "width": 3840,
                    "height": 2160,
                },
            },
            headers={"Idempotency-Key": "concurrency-render-" + uuid.uuid4().hex},
        )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        return project_id, queued.json["data"]["job"]["id"], queued.json["data"]["render_id"]

    def _create_monitor(self) -> str:
        source_url = "http://news.fixture.test/worker-concurrency"
        created = self.request(
            "post", "/api/monitors", data={
                "title": "Queue concurrency fixture",
                "scope": "Isolated worker concurrency test",
                "sources": [{"url": source_url, "label": "Controlled release source"}],
                "keywords": ["release"],
                "exclude_keywords": [],
                "frequency_seconds": 3600,
                "stop_at": None,
                "per_run_limit": 1,
            },
            headers={"Idempotency-Key": "concurrency-monitor-" + uuid.uuid4().hex},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        monitor = created.json["data"]
        enabled = self.request(
            "post", f"/api/monitors/{monitor['id']}/enable", data={"base_version": monitor["version"]},
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        return monitor["id"]

    def test_actual_ffmpeg_render_does_not_block_monitor_material_and_signal_then_cancels(self):
        project_id, job_id, render_id = self._queue_real_media_render(duration=15)
        monitor_id = self._create_monitor()
        source_url = "http://news.fixture.test/worker-concurrency"
        source_state = {"version": "v1", "requests": []}
        source_state_lock = threading.Lock()

        class ControlledSourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/robots.txt":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path != "/worker-concurrency":
                    self.send_error(404)
                    return
                with source_state_lock:
                    version = source_state["version"]
                    source_state["requests"].append((path, self.headers.get("If-None-Match")))
                body = (
                    "<!doctype html><meta charset=utf-8><title>Controlled release update</title>"
                    f"<main><h1>Release update</h1><p>Release {version} now available.</p></main>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", f'"{version}"')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        source_server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledSourceHandler)
        source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
        source_thread.start()

        def fixture_target(url):
            parsed = urlsplit(url)
            if parsed.hostname != "news.fixture.test" or parsed.scheme != "http":
                raise AcquisitionError("SOURCE_POLICY_DENIED", "The isolated worker fixture accepts one source host.")
            return url, parsed.hostname, source_server.server_port, "127.0.0.1"

        ffmpeg_started = threading.Event()
        ffmpeg_processes = []
        ffmpeg_command_shapes = []
        release_popen = threading.Event()
        real_popen = subprocess.Popen

        def track_ffmpeg_popen(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            command_parts = [str(part).lower() for part in command] if isinstance(command, (list, tuple)) else []
            executable = Path(command_parts[0]).name if command_parts else ""
            if executable.startswith("ffmpeg"):
                ffmpeg_command_shapes.append({
                    "output_name": Path(command_parts[-1]).name if command_parts else "",
                    "has_duration_limit": "-t" in command_parts,
                    "has_video_codec": "-c:v" in command_parts and "libx264" in command_parts,
                    "has_filter_graph": "-filter_complex" in command_parts,
                })
            video_encode = (
                "-c:v" in command_parts and "libx264" in command_parts
                and command_parts[-1].endswith(".mp4")
            )
            if executable.startswith("ffmpeg") and video_encode:
                ffmpeg_processes.append(process)
                ffmpeg_started.set()
                if not release_popen.wait(20):
                    process.kill()
                    process.communicate()
                    raise TimeoutError("The test did not release the actual FFmpeg process handle")
            return process

        stop_event = threading.Event()
        worker = None
        try:
            with patch("server.oevira.acquisition.public_target", side_effect=fixture_target), \
                 patch("server.oevira.media_rendering.subprocess.Popen", side_effect=track_ffmpeg_popen):
                worker = self.start_worker(stop_event)
                if not ffmpeg_started.wait(30):
                    job_state = self.store.row(
                        "SELECT status,error_code,error_message FROM jobs WHERE id=?", (job_id,),
                    )
                    self.fail(
                        "The real queued Media render should launch its final FFmpeg process; "
                        f"observed commands={ffmpeg_command_shapes!r}, job={dict(job_state)}"
                    )
                self.assertTrue(ffmpeg_processes and ffmpeg_processes[0].poll() is None,
                                "The render must be blocked on a live FFmpeg process, not a mock result")

                self.wait_for(
                    lambda: (row := self.store.row(
                        "SELECT status, pending_job_id FROM monitor_sources WHERE monitor_id=? AND active=1",
                        (monitor_id,),
                    )) and row["status"] == "baseline_established" and row["pending_job_id"] is None,
                    timeout=20,
                    message="the periodic monitor to acquire and save its initial source while FFmpeg is running",
                )
                stale_progress = _iso_before(20 * 60)
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE jobs SET updated_at=? WHERE id=? AND status='running'",
                        (stale_progress, job_id),
                    )
                self.assertIsNone(_claim_job(self.store, allowed_kinds=[]))
                still_running = self.store.row(
                    "SELECT status, updated_at, lease_heartbeat_at FROM jobs WHERE id=?", (job_id,),
                )
                self.assertEqual(still_running["status"], "running")
                self.assertEqual(still_running["updated_at"], stale_progress)
                self.assertGreater(still_running["lease_heartbeat_at"], stale_progress)

                with source_state_lock:
                    source_state["version"] = "v2"
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE monitor_rules SET next_run_at=?, updated_at=? WHERE id=?",
                        (utc_now(), utc_now(), monitor_id),
                    )
                self.wait_for(
                    lambda: self.store.row("SELECT COUNT(*) AS count FROM monitor_signals WHERE monitor_id=?", (monitor_id,))["count"] == 1,
                    timeout=20,
                    message="the scheduled second source read to save a Material and produce a change signal",
                )
                source_materials = self.store.rows(
                    "SELECT m.id, m.source_url, m.source_text, m.status FROM materials m "
                    "JOIN monitor_observations o ON o.material_id=m.id WHERE o.monitor_id=? ORDER BY m.created_at",
                    (monitor_id,),
                )
                self.assertEqual(len(source_materials), 2)
                self.assertTrue(all(item["source_url"] == source_url and item["status"] == "ready" for item in source_materials))
                self.assertTrue(any("Release v1" in item["source_text"] for item in source_materials))
                self.assertTrue(any("Release v2" in item["source_text"] for item in source_materials))
                signal = self.request("get", f"/api/monitors/{monitor_id}/signals").json["data"][0]
                self.assertTrue(any("Release v2" in item.get("quote", "") for item in signal["evidence"]))
                with source_state_lock:
                    source_requests = [item for item in source_state["requests"] if item[0] == "/worker-concurrency"]
                self.assertEqual(len(source_requests), 2)
                self.assertEqual(source_requests[0][1], None)
                self.assertEqual(source_requests[1][1], '"v1"')

                self.assertIsNone(ffmpeg_processes[0].poll(), "The actual FFmpeg encoder should still be running at user cancel")
                cancelled = self.request("post", f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
                self.assertEqual(cancelled.json["data"]["status"], "cancel_requested")
                release_popen.set()
                self.wait_for(
                    lambda: self.store.row("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "cancelled",
                    timeout=15,
                    message="the active Media job to cancel the real FFmpeg process and persist cancellation",
                )
                render = self.request("get", f"/api/media/renders/{render_id}")
                self.assertEqual(render.status_code, 200, render.get_data(as_text=True))
                self.assertEqual(render.json["data"]["status"], "cancelled")
                self.assertIsNotNone(ffmpeg_processes[0].poll(), "The cancelled FFmpeg process must be reaped")
                self.assertEqual(len(ffmpeg_processes), 1, "Cancellation must not start a second render attempt")
                self.assertEqual(
                    self.store.row("SELECT COUNT(*) AS count FROM materials WHERE source_url=?", (source_url,))["count"],
                    2,
                )
        finally:
            release_popen.set()
            stop_event.set()
            if worker is not None:
                worker.join(timeout=10)
            source_server.shutdown()
            source_server.server_close()
            source_thread.join(timeout=5)
        self.assertIsNotNone(worker)
        self.assertFalse(worker.is_alive(), "The worker should stop after the cancelled render exits")
        self.assertEqual(self.app.extensions["oevira.worker_state"]["status"], "stopped")
        self.assertFalse(run_once(self.app), "The cancelled Media job must not be automatically executed again")

        restarted = create_app(self.root / "data", expected_port=8317)
        restarted.config["TESTING"] = True
        restarted_client = restarted.test_client()
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        try:
            for material in source_materials:
                readback = restarted_client.get(
                    f"/api/materials/{material['id']}", base_url=BASE_URL, headers=auth_headers,
                )
                self.assertEqual(readback.status_code, 200, readback.get_data(as_text=True))
                self.assertEqual(readback.json["data"]["source"]["url"], source_url)
                self.assertIn("Release", readback.json["data"]["text"])
                readback.close()
            reopened_signal = restarted_client.get(
                f"/api/monitors/{monitor_id}/signals", base_url=BASE_URL, headers=auth_headers,
            )
            self.assertEqual(reopened_signal.status_code, 200, reopened_signal.get_data(as_text=True))
            self.assertEqual(len(reopened_signal.json["data"]), 1)
            self.assertTrue(any(
                "Release v2" in item.get("quote", "")
                for item in reopened_signal.json["data"][0]["evidence"]
            ))
            reopened_signal.close()
        finally:
            restarted.extensions["oevira.browser"].runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
