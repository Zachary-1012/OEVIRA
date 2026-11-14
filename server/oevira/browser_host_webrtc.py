"""WebRTC transport for the existing isolated Chromium page, never a CDP gateway."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from fractions import Fraction
import json
import threading
import time
from typing import Callable

from .browser import BrowserError


MAX_SDP_BYTES = 256 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_PENDING_INPUTS = 128
PEER_AUTHORIZATION_POLL_SECONDS = 0.25
PEER_CONTROL_POLL_SECONDS = 0.25
PEER_PUMP_INTERVAL_SECONDS = 0.02


async def close_webrtc_connection(connection, *, grace_seconds: float = 2.0) -> dict:
    """Finish this peer's shutdown even if Windows is still flushing UDP.

    aiortc 1.15/aioice 0.11 keep the owned datagram protocols on each ICE
    connection. Retain those exact transports before close clears the list;
    never enumerate or abort another peer's sockets.
    """
    ice_transports = {item.receiver.transport.transport for item in connection.getTransceivers()}
    if connection.sctp is not None:
        ice_transports.add(connection.sctp.transport.transport)
    datagrams = {protocol.transport for ice in ice_transports for protocol in ice._connection._protocols}
    closing = asyncio.create_task(connection.close())
    forced = False
    try:
        await asyncio.wait_for(asyncio.shield(closing), grace_seconds)
    except asyncio.TimeoutError:
        forced = True
        for transport in datagrams:
            # abort() discards queued datagrams and invokes connection_lost;
            # cancelling the close coroutine alone would leave sockets alive.
            transport.abort()
        try:
            await asyncio.wait_for(asyncio.shield(closing), grace_seconds)
        except asyncio.TimeoutError as exc:
            raise BrowserError("BROWSER_TRANSPORT_CLOSE_UNCONFIRMED", 503) from exc
    return {"forced": forced, "transports": len(datagrams), "closed": True}


class BrowserWebRTC:
    """One asyncio loop serves the bounded peers of the product browser owner."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="oevira-browser-webrtc", daemon=True)
        self._ready = threading.Event()
        self._peers: dict[str, _Peer] = {}
        self._telemetry_lock = threading.Lock()
        self._last_closure: dict | None = None
        self._thread.start()
        if not self._ready.wait(5):
            raise BrowserError("BROWSER_HOST_START_TIMEOUT", 503)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    @property
    def last_closure(self) -> dict | None:
        """Return the last bounded, non-content peer closure observation."""
        with self._telemetry_lock:
            return dict(self._last_closure) if self._last_closure is not None else None

    def _record_closure(self, host_id: str, detail: dict) -> None:
        observation = {"host_id": host_id, "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **detail}
        with self._telemetry_lock:
            self._last_closure = observation

    def offer(self, host_id: str, stream, sdp: str, authorized: Callable[[], bool], on_closed: Callable[[], None], *, on_page=None, control_state=None) -> dict:
        if not isinstance(sdp, str) or len(sdp.encode("utf-8")) > MAX_SDP_BYTES or not sdp.startswith("v=0"):
            raise BrowserError("BROWSER_HOST_OFFER_INVALID", 400)
        future = asyncio.run_coroutine_threadsafe(
            self._offer(host_id, stream, sdp, authorized, on_closed, on_page, control_state), self._loop,
        )
        try:
            return future.result(25)
        except FutureTimeout as exc:
            future.cancel()
            self.close(host_id, reason="offer_timeout")
            raise BrowserError("BROWSER_HOST_CONNECT_TIMEOUT", 504) from exc

    async def _offer(self, host_id, stream, sdp, authorized, on_closed, on_page, control_state):
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription

        # No STUN/TURN provider, relay, public signaling service or browser credentials.
        connection = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer = None

        try:
            video_track = _video_track(stream, authorized)

            def peer_closed():
                try:
                    on_closed()
                finally:
                    if self._peers.get(host_id) is peer:
                        self._peers.pop(host_id, None)

            peer = _Peer(
                host_id, connection, stream, authorized, peer_closed, on_page, control_state,
                video_track=video_track, on_closure=self._record_closure,
            )
            self._peers[host_id] = peer
            sender = connection.addTrack(video_track)
            capabilities = RTCRtpSender.getCapabilities("video").codecs
            codecs = sorted(capabilities, key=lambda codec: codec.mimeType.lower() != "video/h264")
            for transceiver in connection.getTransceivers():
                if transceiver.sender is sender:
                    transceiver.setCodecPreferences(codecs)
            if "m=audio " in sdp:
                from .browser_host_audio import browser_audio_track

                audio = await asyncio.to_thread(browser_audio_track, stream)
                if audio is not None:
                    peer.audio_track = audio
                    peer.media_tracks.append(audio)
                    peer.audio_cleanup_complete = False
                    connection.addTrack(audio)
                else:
                    stream._emit({"type": "audio", "available": False, "enabled": False,
                                  "reason": "BROWSER_AUDIO_UNAVAILABLE"})
            await connection.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await connection.setLocalDescription(await connection.createAnswer())
            peer.task = asyncio.create_task(peer.run())
            return {"type": "answer", "sdp": connection.localDescription.sdp, "host_id": host_id}
        except BaseException:
            if peer is not None:
                await self._close(host_id, reason="offer_failed")
            else:
                await connection.close()
            raise

    async def _close(self, host_id: str, *, reason: str = "host_detach"):
        peer = self._peers.get(host_id)
        if peer is not None:
            try:
                await peer.close(reason=reason)
            finally:
                if self._peers.get(host_id) is peer:
                    self._peers.pop(host_id, None)

    def close(self, host_id: str, *, reason: str = "host_detach"):
        if self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._close(host_id, reason=reason), self._loop)

    def shutdown(self):
        if self._loop.is_closed():
            return

        async def close_all():
            running = [peer.task for peer in self._peers.values() if peer.task is not None]
            closing = [
                asyncio.create_task(self._close(host_id, reason="service_shutdown"))
                for host_id in list(self._peers)
            ]
            if closing:
                await asyncio.gather(*closing, return_exceptions=True)
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(close_all(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if not self._thread.is_alive():
            self._loop.close()


def _video_track(stream, authorized):
    from aiortc import MediaStreamError, VideoStreamTrack
    import av

    class ChromiumTrack(VideoStreamTrack):
        def __init__(self):
            super().__init__()
            self.codec = av.CodecContext.create("mjpeg", "r")
            self.last_bytes = None
            self.frame = None
            self.started = time.monotonic()
            self.frames_decoded = 0
            self.frames_sent = 0
            self.decode_errors = 0
            self.authorization_failures = 0
            self.first_frame_timed_out = False
            self.last_frame_at: float | None = None

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            if not await asyncio.to_thread(authorized):
                self.authorization_failures += 1
                raise MediaStreamError
            encoded = await asyncio.to_thread(stream.latest_frame)
            if encoded is not None and encoded is not self.last_bytes:
                try:
                    frames = self.codec.decode(av.Packet(encoded))
                except Exception:
                    self.decode_errors += 1
                    raise
                if frames:
                    self.frame = frames[-1]
                    self.last_bytes = encoded
                    self.frames_decoded += len(frames)
            if self.frame is None:
                # Wait for real Chromium output. Never substitute a success/blank fixture.
                if time.monotonic() - self.started > 15:
                    self.first_frame_timed_out = True
                    raise MediaStreamError
                await asyncio.sleep(0.02)
                return await self.recv()
            # Chromium JPEG frames use full-range YUV. A fresh encoder frame also
            # prevents changing timestamps on a frame retained by the codec.
            frame = self.frame.reformat(width=self.frame.width - self.frame.width % 2,
                                        height=self.frame.height - self.frame.height % 2, format="yuv420p")
            frame.pts = pts
            frame.time_base = time_base or Fraction(1, 90000)
            self.frames_sent += 1
            self.last_frame_at = time.monotonic()
            return frame

    return ChromiumTrack()


class _Peer:
    def __init__(self, host_id, connection, stream, authorized, on_closed, on_page=None, control_state=None,
                 *, video_track=None, on_closure=None):
        self.host_id = host_id
        self.connection = connection
        self.stream = stream
        self.authorized = authorized
        self.on_closed = on_closed
        self.on_page = on_page
        self.control_state = control_state
        self.video_track = video_track
        self.audio_track = None
        self.media_tracks = [video_track] if video_track is not None else []
        self.audio_cleanup_complete = True
        self.on_closure = on_closure
        self.last_control = None
        self.next_control_read = 0.0
        self.next_authorization_check = 0.0
        self.channel = None
        self.task = None
        self.pump_task = None
        self.next_pump_time = 0.0
        self.pump_backoff = 0.0
        self.closed = False
        self.close_task = None
        self.input_task = None
        self.inputs = deque()
        self.started = time.monotonic()
        self.close_cause = None
        self.last_error_type = None
        self.last_error_code = None
        self.input_messages = 0
        self.input_dispatched = 0
        self.input_rejected = 0
        self.input_queue_high_water = 0
        self.outbound_messages = 0
        self.outbound_send_errors = 0
        self.pump_calls = 0
        self.pump_errors = 0
        self.pump_timeouts = 0
        self.last_connection_state = connection.connectionState
        self.last_ice_connection_state = connection.iceConnectionState
        self.last_signaling_state = connection.signalingState

        @connection.on("datachannel")
        def datachannel(channel):
            if channel.label != "input" or self.channel is not None or not channel.ordered:
                try:
                    channel.close()
                except Exception:
                    pass
                return
            self.channel = channel

            @channel.on("message")
            def message(data):
                self.input_messages += 1
                if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_INPUT_BYTES:
                    self.input_rejected += 1
                    self.send({"type": "error", "code": "BROWSER_INPUT_INVALID"})
                    return
                try:
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ValueError
                    if len(self.inputs) >= MAX_PENDING_INPUTS:
                        raise OverflowError
                    self.inputs.append(event)
                    self.input_queue_high_water = max(self.input_queue_high_water, len(self.inputs))
                except (ValueError, OverflowError):
                    self.input_rejected += 1
                    self._set_close_cause("input_backpressure", error_code="BROWSER_INPUT_BACKPRESSURE")
                    self.send({"type": "error", "code": "BROWSER_INPUT_BACKPRESSURE"})
                    # A missing key-up or pointer-up must not silently remain held.
                    asyncio.create_task(self.close(reason="input_backpressure"))

            @channel.on("close")
            def channel_closed():
                if not self.closed:
                    self._set_close_cause("data_channel_closed")
                    asyncio.create_task(self.close(reason="data_channel_closed"))

            @channel.on("error")
            def channel_error(error=None):
                if not self.closed:
                    self._set_close_cause("data_channel_error", exception=error)
                    asyncio.create_task(self.close(reason="data_channel_error"))

        @connection.on("connectionstatechange")
        async def changed():
            self.last_connection_state = connection.connectionState
            self.last_ice_connection_state = connection.iceConnectionState
            self.last_signaling_state = connection.signalingState
            if connection.connectionState in {"failed", "closed"}:
                cause = "connection_failed" if connection.connectionState == "failed" else "connection_closed"
                self._set_close_cause(cause)
                await self.close(reason=cause)

        @connection.on("iceconnectionstatechange")
        async def ice_changed():
            self.last_ice_connection_state = connection.iceConnectionState
            self.last_connection_state = connection.connectionState
            if connection.iceConnectionState == "failed":
                self._set_close_cause("ice_connection_failed")
                await self.close(reason="ice_connection_failed")

    def send(self, event):
        if self.channel is None or self.channel.readyState != "open":
            return False
        if self.connection.connectionState in {"failed", "closed"}:
            return False
        if self.channel.bufferedAmount > 512 * 1024:
            self._set_close_cause("data_channel_backpressure", error_code="BROWSER_HOST_BACKPRESSURE")
            asyncio.create_task(self.close(reason="data_channel_backpressure"))
            return False
        try:
            self.channel.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            self.outbound_messages += 1
            return True
        except Exception as exc:
            self.outbound_send_errors += 1
            self._set_close_cause("data_channel_send_error", exception=exc)
            asyncio.create_task(self.close(reason="data_channel_send_error"))
            return False

    def _set_close_cause(self, cause: str, *, error_code: str | None = None,
                         exception: BaseException | None = None) -> None:
        if self.close_cause is None:
            self.close_cause = cause
        if error_code is not None:
            self.last_error_code = error_code[:80]
        if exception is not None:
            self.last_error_type = type(exception).__name__[:80]

    def _closure_observation(self) -> dict:
        now = time.monotonic()
        video = self.video_track
        audio = self.audio_track
        last_frame_at = getattr(video, "last_frame_at", None) if video is not None else None
        channel_state = None
        buffered_amount = 0
        if self.channel is not None:
            try:
                channel_state = self.channel.readyState
                buffered_amount = int(self.channel.bufferedAmount)
            except Exception:
                channel_state = "unavailable"
        return {
            "cause": (self.close_cause or "peer_closed")[:80],
            "connection_state": str(getattr(self.connection, "connectionState", "unknown"))[:32],
            "ice_connection_state": str(getattr(self.connection, "iceConnectionState", "unknown"))[:32],
            "signaling_state": str(getattr(self.connection, "signalingState", "unknown"))[:32],
            "last_connection_state": str(self.last_connection_state)[:32],
            "last_ice_connection_state": str(self.last_ice_connection_state)[:32],
            "data_channel_state": channel_state,
            "data_channel_buffered_bytes": max(0, buffered_amount),
            "elapsed_ms": max(0, int((now - self.started) * 1000)),
            "input_messages": self.input_messages,
            "input_dispatched": self.input_dispatched,
            "input_rejected": self.input_rejected,
            "input_queue_high_water": self.input_queue_high_water,
            "pending_inputs": len(self.inputs),
            "input_task_pending": bool(self.input_task is not None and not self.input_task.done()),
            "outbound_messages": self.outbound_messages,
            "outbound_send_errors": self.outbound_send_errors,
            "pump_calls": self.pump_calls,
            "pump_errors": self.pump_errors,
            "pump_timeouts": self.pump_timeouts,
            "pump_task_pending": bool(self.pump_task is not None and not self.pump_task.done()),
            "video_frames_decoded": int(getattr(video, "frames_decoded", 0)) if video is not None else 0,
            "video_frames_sent": int(getattr(video, "frames_sent", 0)) if video is not None else 0,
            "video_decode_errors": int(getattr(video, "decode_errors", 0)) if video is not None else 0,
            "video_first_frame_timed_out": bool(getattr(video, "first_frame_timed_out", False)) if video is not None else False,
            "video_last_frame_age_ms": (max(0, int((now - last_frame_at) * 1000)) if last_frame_at is not None else None),
            "audio_samples_captured": int(getattr(audio, "samples", 0)) if audio is not None else 0,
            "audio_helper_returncode": getattr(getattr(audio, "process", None), "returncode", None) if audio is not None else None,
            "audio_cleanup_complete": self.audio_cleanup_complete,
            "last_error_type": self.last_error_type,
            "last_error_code": self.last_error_code,
        }

    async def run(self):
        announced = False
        try:
            while not self.closed:
                now = time.monotonic()
                if now >= self.next_authorization_check:
                    authorized = await asyncio.to_thread(self.authorized)
                    self.next_authorization_check = time.monotonic() + PEER_AUTHORIZATION_POLL_SECONDS
                else:
                    authorized = True
                if not authorized:
                    self._set_close_cause("authorization_revoked", error_code="BROWSER_REAUTH_REQUIRED")
                    self.send({"type": "error", "code": "BROWSER_REAUTH_REQUIRED"})
                    await asyncio.sleep(0.05)
                    break
                if self.connection.connectionState != "connected" and time.monotonic() - self.started > 30:
                    self._set_close_cause("connection_establishment_timeout", error_code="BROWSER_HOST_CONNECT_TIMEOUT")
                    break
                if self.control_state is not None and time.monotonic() >= self.next_control_read:
                    control = await asyncio.to_thread(self.control_state)
                    if control != self.last_control and self.channel is not None and self.channel.readyState == "open":
                        self.send({"type": "control", **control})
                        self.last_control = control
                    self.next_control_read = time.monotonic() + PEER_CONTROL_POLL_SECONDS
                if self.channel is not None and self.channel.readyState == "open" and not announced:
                    state = await asyncio.to_thread(self.stream.initial_state)
                    self.send({"type": "page", **state})
                    await self.record_page(state)
                    announced = True
                if self.input_task is not None and self.input_task.done():
                    result = self.input_task.result()
                    self.input_task = None
                    if result is not None:
                        if result.get("type") == "error" and not await asyncio.to_thread(self.authorized):
                            result = {"type": "error", "code": "BROWSER_REAUTH_REQUIRED"}
                        self.send(result)
                if self.inputs and (
                    self.inputs[0].get("type") == "dialog" or self.stream.has_open_dialog
                ):
                    event = self.inputs.popleft()
                    try:
                        result = await asyncio.to_thread(self.stream.interrupt_dialog, event)
                        if isinstance(result, dict) and result.get("type") == "error":
                            self.input_rejected += 1
                        else:
                            self.input_dispatched += 1
                        if result is not None:
                            self.send(result)
                    except BrowserError as exc:
                        self.input_rejected += 1
                        self.send({"type": "error", "code": exc.code, **exc.details})
                elif self.inputs and self.input_task is None:
                    event = self.inputs.popleft()
                    self.input_task = asyncio.create_task(asyncio.to_thread(self.stream.send_input, event))
                    self.input_dispatched += 1
                # A modal may keep the click waiting. Event delivery continues so
                # an external page navigation's dialog reply can reach the same
                # Chromium protocol loop without queueing behind that navigation.
                for event in await asyncio.to_thread(self.stream.drain_events):
                    if event.get("type") == "error" and not await asyncio.to_thread(self.authorized):
                        event = {"type": "error", "code": "BROWSER_REAUTH_REQUIRED"}
                    self.send(event)
                    if event.get("type") == "page":
                        await self.record_page(event)
                self._collect_pump_result()
                self._schedule_pump()
                await asyncio.sleep(0.008)
        except asyncio.CancelledError:
            self._set_close_cause("peer_task_cancelled")
            raise
        except Exception as exc:
            self._set_close_cause("peer_loop_error", exception=exc)
            code = "BROWSER_HOST_DISCONNECTED" if await asyncio.to_thread(self.authorized) else "BROWSER_REAUTH_REQUIRED"
            self.last_error_code = code
            self.send({"type": "error", "code": code})
            await asyncio.sleep(0.05)
        finally:
            await self.close()

    def _schedule_pump(self) -> None:
        now = time.monotonic()
        if (not self.closed and self.pump_task is None and now >= self.next_pump_time
                and not self.stream.closed):
            self.pump_task = asyncio.create_task(asyncio.to_thread(self.stream.pump))
            self.pump_calls += 1

    def _collect_pump_result(self) -> None:
        if self.pump_task is None or not self.pump_task.done():
            return
        now = time.monotonic()
        try:
            self.pump_task.result()
            self.pump_backoff = 0.0
        except BrowserError as exc:
            self.pump_errors += 1
            self.last_error_code = exc.code[:80]
            if exc.code == "BROWSER_OPERATION_STATE_UNKNOWN":
                self.pump_timeouts += 1
            self.pump_backoff = min(5.0, max(0.25, self.pump_backoff * 2 or 0.25))
        except Exception as exc:
            self.pump_errors += 1
            self.last_error_type = type(exc).__name__[:80]
            self.pump_backoff = min(5.0, max(0.25, self.pump_backoff * 2 or 0.25))
        self.pump_task = None
        self.next_pump_time = now + (self.pump_backoff or PEER_PUMP_INTERVAL_SECONDS)

    async def record_page(self, event):
        if self.on_page is not None:
            failure = await asyncio.to_thread(self.on_page, event)
            if failure is not None:
                self.send(failure)

    async def close(self, *, reason: str | None = None):
        if self.close_task is None:
            self._set_close_cause(reason or "peer_closed")
            self.closed = True
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()
            self.close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self.close_task)

    async def _finish_close(self):
        try:
            await asyncio.to_thread(self.stream.close)
        except Exception as exc:
            self._set_close_cause("stream_close_error", exception=exc)
        try:
            await close_webrtc_connection(self.connection)
        except Exception as exc:
            self._set_close_cause("connection_close_error", exception=exc)
        media_cleanup_complete = True
        for track in self.media_tracks:
            try:
                stop = getattr(track, "stop", None)
                if callable(stop):
                    stop()
                aclose = getattr(track, "aclose", None)
                if callable(aclose):
                    await asyncio.wait_for(aclose(), 5.0)
            except asyncio.TimeoutError:
                media_cleanup_complete = False
                self._set_close_cause("media_track_stop_timeout")
            except Exception as exc:
                media_cleanup_complete = False
                self._set_close_cause("media_track_stop_error", exception=exc)
        self.audio_cleanup_complete = self.audio_track is None or media_cleanup_complete
        pending = [task for task in (self.input_task, self.pump_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.on_closure is not None:
            try:
                self.on_closure(self.host_id, self._closure_observation())
            except Exception:
                pass
        try:
            self.on_closed()
        except Exception:
            pass
