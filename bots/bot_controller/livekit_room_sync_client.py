import asyncio
import collections
import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field

import jwt
from livekit import rtc

from bots.models import ParticipantEventTypes
from bots.room_sync_source_participant_configuration import (
    LivekitRoomSyncSourceParticipantConfiguration,
    RoomSyncSourceParticipantConfiguration,
)
from bots.room_sync_utils import does_participant_name_have_bot_indicator

logger = logging.getLogger(__name__)


class _AudioBuffer:
    """Bounded PCM buffer filled by the bot's thread and drained by one pump task.

    Pushing never schedules work on the event loop except to wake an idle pump,
    so a slow or stalled loop cannot accumulate unbounded callbacks, tasks or
    memory. When the buffer is full the oldest audio is dropped: for live audio,
    staying current is better than falling further and further behind.
    """

    def __init__(self, sample_frame_bytes: int, max_bytes: int):
        self.sample_frame_bytes = sample_frame_bytes
        self.max_bytes = max(max_bytes, sample_frame_bytes)
        self._chunks: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._wake_pending = False
        self.closed = False
        self.buffered_bytes = 0
        self.dropped_bytes = 0
        self.reported_dropped_bytes = 0  # Only touched by the watchdog thread.
        self.capture_failures = 0
        self.last_failure_log = 0.0
        # Throughput counters. Received is written by the caller's thread, sent
        # by the pump; the watchdog only reads them.
        self.received_chunks = 0
        self.received_bytes = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        # Values at the last throughput report. Only touched by the watchdog.
        self.reported_received_chunks = 0
        self.reported_received_bytes = 0
        self.reported_sent_frames = 0
        self.reported_sent_bytes = 0
        self.reported_stats_dropped_bytes = 0

    def push(self, data) -> bool:
        """Append audio (any thread). Returns True if the caller must wake the pump."""
        if not isinstance(data, bytes):
            data = bytes(data)
        self.received_chunks += 1
        usable = len(data) - len(data) % self.sample_frame_bytes
        self.received_bytes += usable
        if not usable:
            return False
        if usable != len(data):
            data = data[:usable]
        with self._lock:
            if self.closed:
                return False
            self._chunks.append(data)
            self.buffered_bytes += usable
            while self.buffered_bytes > self.max_bytes:
                oldest = self._chunks.popleft()
                self.buffered_bytes -= len(oldest)
                self.dropped_bytes += len(oldest)
            if self._wake_pending:
                return False
            self._wake_pending = True
            return True

    def wake(self):
        """Event-loop thread: let the pump know audio is available."""
        with self._lock:
            self._wake_pending = False
        self._event.set()

    def take(self, max_bytes: int) -> bytes | None:
        """Event-loop thread: remove up to max_bytes of whole sample frames.

        Returns None once closed and b"" when empty.
        """
        with self._lock:
            if self.closed:
                return None
            parts = []
            size = 0
            while self._chunks and size < max_bytes:
                chunk = self._chunks[0]
                room = max_bytes - size
                if len(chunk) <= room:
                    parts.append(self._chunks.popleft())
                    size += len(chunk)
                else:
                    room -= room % self.sample_frame_bytes
                    if not room:
                        break
                    parts.append(chunk[:room])
                    self._chunks[0] = chunk[room:]
                    size += room
            self.buffered_bytes -= size
            return b"".join(parts)

    def skip_to_latest(self, keep_bytes: int):
        """Event-loop thread: drop all but roughly the newest keep_bytes of audio."""
        with self._lock:
            while len(self._chunks) > 1 and self.buffered_bytes - len(self._chunks[0]) >= keep_bytes:
                oldest = self._chunks.popleft()
                self.buffered_bytes -= len(oldest)
                self.dropped_bytes += len(oldest)

    async def wait(self):
        """Event-loop thread: sleep until audio arrives or the buffer closes."""
        self._event.clear()
        with self._lock:
            if self._chunks or self.closed:
                return
        await self._event.wait()

    def close(self):
        """Event-loop thread: discard buffered audio and stop the pump."""
        with self._lock:
            self.closed = True
            self._chunks.clear()
            self.buffered_bytes = 0
        self._event.set()


@dataclass
class _Mirror:
    """One task owns this connection, from connect through disconnect."""

    participant_uuid: str
    room: rtc.Room
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    source: rtc.AudioSource | None = None
    audio: _AudioBuffer | None = None
    release: str | None = None
    disconnected: bool = False


class LivekitRoomSyncClient:
    """Mirror meeting participants, PCM audio and chat into a LiveKit room.

    Public methods submit work to a private asyncio thread. Each participant
    has one connection-owning task; cancelling a claim timer never cancels an
    SDK connect. Shutdown signals those tasks and lets them close their rooms.

    Hidden watchers exchange the existing hello/heartbeat/claiming/abandoned/
    goodbye protocol. Rendezvous hashing ranks all live bots per participant;
    claim leases suppress fallbacks while a connection is being established.
    Release attributes distinguish meeting departures from bot shutdowns.
    Claims require a connected watcher whose membership view has settled.

    Audio is pushed from the caller's thread into a bounded per-participant
    buffer, and a single pump task per mirror feeds it to the AudioSource one
    frame at a time. A watchdog thread outside the event loop logs only when
    the loop lags, audio backs up or audio is dropped.
    """

    TOKEN_TTL_SECONDS = 6 * 60 * 60
    CHAT_TOPIC = "lk.chat"
    CONTROL_TOPIC = "attendee.room_sync"
    OWNER_ATTRIBUTE = "attendee.room_sync.owner"
    RELEASE_ATTRIBUTE = "attendee.room_sync.release"
    SOURCE_ATTRIBUTE = "attendee.room_sync.source"
    RELEASE_LEFT_MEETING = "left_meeting"
    RELEASE_SHUTDOWN = "shutdown"
    RELEASE_ATTRIBUTE_TIMEOUT_SECONDS = 2
    TAKEOVER_STEP_SECONDS = 2.0
    UNRELEASED_SETTLE_SECONDS = 0.5
    CLAIM_LEASE_SECONDS = 10.0
    LEFT_MEETING_GRACE_SECONDS = 15.0
    BROADCAST_TIMEOUT_SECONDS = 1.0
    SHUTDOWN_DRAIN_SECONDS = 5.0
    STRAGGLER_MAX_SECONDS = 120.0
    HEARTBEAT_INTERVAL_SECONDS = 2.0
    MEMBER_TTL_SECONDS = 6.0
    MEMBERSHIP_SETTLE_SECONDS = 0.5
    WATCHER_READY_TIMEOUT_SECONDS = 10
    WATCHER_RETRY_SECONDS = 5
    RECONCILE_INTERVAL_SECONDS = 30
    TAKEOVER_FLAP_WARNING_THRESHOLD = 3

    # Audio: at most this much audio waits in our buffer per participant (the
    # SDK's AudioSource keeps its own ~1s queue on top of this). Each capture
    # sends at most AUDIO_MAX_CAPTURE_SECONDS, so catching up after a stall
    # takes a few large frames instead of hundreds of small ones.
    AUDIO_BUFFER_SECONDS = 0.5
    AUDIO_MAX_CAPTURE_SECONDS = 0.1
    AUDIO_PUMP_STOP_SECONDS = 2.0

    # Watchdog: runs on its own thread so it still reports when the loop is
    # stuck. It logs only when something is wrong, at most once per interval.
    WATCHDOG_INTERVAL_SECONDS = 2.0
    LOOP_LAG_WARNING_SECONDS = 0.5
    AUDIO_BACKLOG_WARNING_FRACTION = 0.5
    TASK_WARNING_HEADROOM = 50
    ISSUE_LOG_INTERVAL_SECONDS = 10.0
    # Every this often, log per-participant audio received vs sent (always,
    # not just on problems), as long as at least one participant is mirrored.
    THROUGHPUT_LOG_INTERVAL_SECONDS = 30.0

    def __init__(self, room: str, credentials: dict, sample_rate: int = 48000, num_channels: int = 1, source_participant: dict = None, sync_to_room: bool = True):
        self.room_name = room
        self.url = credentials["url"]
        self.api_key = credentials["api_key"]
        self.api_secret = credentials["api_secret"]
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.source_participant = source_participant
        self.sync_to_room = sync_to_room
        self._instance_id = uuid.uuid4().hex[:12]
        self._log_prefix = f"[LiveKit room sync {self._instance_id} room={room}]"

        self._sample_frame_bytes = 2 * num_channels
        self._audio_bytes_per_second = sample_rate * self._sample_frame_bytes
        self._audio_buffer_bytes = self._whole_frame_bytes(self.AUDIO_BUFFER_SECONDS)
        self._max_capture_bytes = self._whole_frame_bytes(self.AUDIO_MAX_CAPTURE_SECONDS)
        # Read from the caller's thread without locks; only the loop mutates it.
        self._audio_buffers: dict[str, _AudioBuffer] = {}

        self._meeting_participants: dict[str, str | None] = {}
        self._mirrors: dict[str, _Mirror] = {}
        self._pending_claims: dict[str, asyncio.TimerHandle] = {}
        self._claim_leases: dict[str, dict[str, float]] = {}
        self._left_meeting_releases: dict[str, float] = {}
        self._takeover_counts: dict[str, int] = {}
        self._members: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

        self._watcher_room: rtc.Room | None = None
        self._watcher_ready = False
        self._watcher_generation = 0
        self._watcher_wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._shutting_down = False
        self._cleanup_called = False
        self._submission_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_event_loop, name="livekit-room-sync", daemon=True)
        self._thread.start()

        self._probe_sent_at: float | None = None
        self._probe_lag = 0.0
        self._issue_active = False
        self._logged_problem_kinds: set[str] = set()
        self._last_issue_log = 0.0
        self._unreported_drops: dict[str, int] = {}
        self._last_throughput_log = time.monotonic()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        if self.sync_to_room:
            self._watchdog_thread = threading.Thread(target=self._run_watchdog, name="livekit-room-sync-watchdog", daemon=True)
            self._watchdog_thread.start()

        self._submit(self._start)

    def _whole_frame_bytes(self, seconds):
        size = int(self._audio_bytes_per_second * seconds)
        return max(size - size % self._sample_frame_bytes, self._sample_frame_bytes)

    def _audio_ms(self, num_bytes):
        return num_bytes * 1000 / self._audio_bytes_per_second

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()

    def _submit(self, callback, *args):
        """Serialize submissions with cleanup so nothing is queued after it."""
        with self._submission_lock:
            if self.sync_to_room and not self._cleanup_called:
                self._loop.call_soon_threadsafe(callback, *args)

    def _spawn(self, coroutine):
        task = self._loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(f"{self._log_prefix} Background task failed", exc_info=(type(error), error, error.__traceback__))

    def _start(self):
        if not self._shutting_down:
            self._spawn(self._watcher_loop())
            self._spawn(self._maintenance_loop())

    def handle_participant_event(self, event, participant=None):
        """Accept the bot controller's JOIN/LEAVE event and optional metadata."""
        if not self.sync_to_room or self._cleanup_called:
            return
        participant_uuid = event["participant_uuid"]
        if event["event_type"] == ParticipantEventTypes.JOIN:
            name = participant.get("participant_full_name") if participant is not None else None
            if not does_participant_name_have_bot_indicator(name):
                self._submit(self._handle_join, participant_uuid, name)
        elif event["event_type"] == ParticipantEventTypes.LEAVE:
            self._submit(self._handle_leave, participant_uuid)

    def handle_chat_message(self, chat_message):
        """Send meeting chat as the participant who authored it, on lk.chat."""
        if not self.sync_to_room or self._cleanup_called:
            return
        participant_uuid = chat_message["participant_uuid"]
        text = chat_message.get("text")
        if text:
            self._submit(self._start_chat, participant_uuid, text)

    def _start_chat(self, participant_uuid, text):
        mirror = self._mirrors.get(participant_uuid)
        if mirror is not None and mirror.source is not None:
            self._spawn(self._send_chat_message(mirror.room, text))

    async def _send_chat_message(self, room, text):
        try:
            await room.local_participant.send_text(text, topic=self.CHAT_TOPIC)
        except Exception:
            logger.exception(f"{self._log_prefix} Failed to mirror chat message")

    def send_audio_chunk(self, participant_uuid: str, chunk_bytes: bytes):
        """Accept little-endian signed PCM16 at the configured rate/channels.

        Called from the bot's thread. Audio for participants this bot is not
        currently mirroring is discarded here without touching the event loop;
        otherwise it lands in a bounded buffer and the loop is woken at most
        once until the pump catches up.
        """
        buffer = self._audio_buffers.get(participant_uuid)
        if buffer is None or not chunk_bytes:
            return
        if buffer.push(chunk_bytes):
            try:
                self._loop.call_soon_threadsafe(buffer.wake)
            except RuntimeError:
                pass  # The loop has already closed during cleanup.

    async def _pump_audio(self, participant_uuid, buffer, source):
        """Feed one participant's audio to its AudioSource, one frame at a time.

        Awaiting each capture_frame keeps exactly one capture in flight, so the
        SDK's own queue applies backpressure and our buffer absorbs the rest.

        Audio arrives and plays out at the same real-time rate, so a backlog
        never drains by itself. If the buffer overflowed, we are more than a
        full buffer behind: discard the stale audio here and in the SDK queue
        and resume from live audio instead of staying delayed indefinitely.
        """
        clear_sdk_queue = getattr(source, "clear_queue", None)
        seen_dropped = 0
        while True:
            if buffer.dropped_bytes != seen_dropped:
                buffer.skip_to_latest(self._max_capture_bytes)
                if clear_sdk_queue is not None:
                    try:
                        clear_sdk_queue()
                    except Exception:
                        logger.exception(f"{self._log_prefix} Failed to clear queued audio for {participant_uuid}")
                seen_dropped = buffer.dropped_bytes
            data = buffer.take(self._max_capture_bytes)
            if data is None:
                return
            if not data:
                await buffer.wait()
                continue
            try:
                frame = rtc.AudioFrame(
                    data=data,
                    sample_rate=self.sample_rate,
                    num_channels=self.num_channels,
                    samples_per_channel=len(data) // self._sample_frame_bytes,
                )
                await source.capture_frame(frame)
                buffer.sent_frames += 1
                buffer.sent_bytes += len(data)
            except Exception:
                buffer.capture_failures += 1
                now = time.monotonic()
                if now - buffer.last_failure_log >= self.ISSUE_LOG_INTERVAL_SECONDS:
                    buffer.last_failure_log = now
                    logger.exception(f"{self._log_prefix} Failed to capture audio for {participant_uuid} ({buffer.capture_failures} failures so far)")

    async def _stop_pump(self, pump):
        # The buffer is already closed, so the pump exits after any capture in
        # flight. Cancelling is a fallback; it never owns a connection.
        done, _ = await asyncio.wait({pump}, timeout=self.AUDIO_PUMP_STOP_SECONDS)
        if not done:
            pump.cancel()
            await asyncio.wait({pump}, timeout=1)

    async def _close_audio_source(self, source):
        try:
            await source.aclose()
        except Exception:
            logger.exception(f"{self._log_prefix} Failed to close audio source")

    def _detach_audio(self, mirror):
        """Stop accepting audio for a mirror. Safe to call more than once."""
        mirror.source = None
        buffer = mirror.audio
        if buffer is not None:
            if self._audio_buffers.get(mirror.participant_uuid) is buffer:
                self._audio_buffers.pop(mirror.participant_uuid, None)
            buffer.close()

    def _run_watchdog(self):
        """Off-loop health check, so it still reports when the loop is stuck."""
        while not self._watchdog_stop.wait(self.WATCHDOG_INTERVAL_SECONDS):
            if self._loop.is_closed():
                return
            try:
                self._watchdog_tick()
                self._maybe_log_throughput()
            except Exception:
                logger.exception(f"{self._log_prefix} Watchdog check failed")

    def _maybe_log_throughput(self):
        """Log audio in (from the meeting) vs out (to LiveKit) per mirrored participant.

        Counts cover the interval since the previous report. Received counts
        each send_audio_chunk call; sent counts capture_frame calls, which can
        batch several chunks (up to AUDIO_MAX_CAPTURE_SECONDS), so compare the
        milliseconds of audio rather than the packet counts.
        """
        now = time.monotonic()
        elapsed = now - self._last_throughput_log
        if elapsed < self.THROUGHPUT_LOG_INTERVAL_SECONDS:
            return
        self._last_throughput_log = now
        for participant_uuid, buffer in sorted(list(self._audio_buffers.items())):
            received_chunks, received_bytes = buffer.received_chunks, buffer.received_bytes
            sent_frames, sent_bytes = buffer.sent_frames, buffer.sent_bytes
            dropped_bytes = buffer.dropped_bytes
            logger.info(f"{self._log_prefix} Audio throughput for {participant_uuid} (last {elapsed:.0f}s): received {received_chunks - buffer.reported_received_chunks} packets ({self._audio_ms(received_bytes - buffer.reported_received_bytes):.0f}ms) from meeting, sent {sent_frames - buffer.reported_sent_frames} packets ({self._audio_ms(sent_bytes - buffer.reported_sent_bytes):.0f}ms) to LiveKit, dropped {self._audio_ms(dropped_bytes - buffer.reported_stats_dropped_bytes):.0f}ms, buffered {self._audio_ms(buffer.buffered_bytes):.0f}ms")
            buffer.reported_received_chunks, buffer.reported_received_bytes = received_chunks, received_bytes
            buffer.reported_sent_frames, buffer.reported_sent_bytes = sent_frames, sent_bytes
            buffer.reported_stats_dropped_bytes = dropped_bytes

    def _probe_done(self, sent_at):
        self._probe_lag = time.monotonic() - sent_at
        self._probe_sent_at = None

    def _watchdog_tick(self):
        now = time.monotonic()
        problems = {}

        # Loop responsiveness: one probe in flight at a time.
        sent_at = self._probe_sent_at
        if sent_at is not None:
            if now - sent_at >= self.LOOP_LAG_WARNING_SECONDS:
                problems["loop"] = f"event loop unresponsive for {now - sent_at:.1f}s"
        else:
            if self._probe_lag >= self.LOOP_LAG_WARNING_SECONDS:
                problems["loop"] = f"event loop lagged {self._probe_lag:.2f}s"
            self._probe_sent_at = now
            try:
                self._loop.call_soon_threadsafe(self._probe_done, now)
            except RuntimeError:
                self._probe_sent_at = None
                return

        # Audio backlog and drops. Drops are totalled between log lines so
        # rate limiting never hides how much audio was lost.
        backlogged = []
        dropping = False
        for participant_uuid, buffer in list(self._audio_buffers.items()):
            dropped = buffer.dropped_bytes - buffer.reported_dropped_bytes
            if dropped > 0:
                dropping = True
                buffer.reported_dropped_bytes += dropped
                self._unreported_drops[participant_uuid] = self._unreported_drops.get(participant_uuid, 0) + dropped
            if buffer.buffered_bytes >= self._audio_buffer_bytes * self.AUDIO_BACKLOG_WARNING_FRACTION:
                backlogged.append(f"{participant_uuid} {self._audio_ms(buffer.buffered_bytes):.0f}ms")
        if backlogged:
            problems["backlog"] = f"audio backlog: {', '.join(backlogged[:5])}"
        if dropping:
            problems["drops"] = "dropping stale audio to stay live"

        tasks = len(self._tasks)
        mirrors = len(self._mirrors)
        # Baseline: watcher + maintenance, plus a mirror task and a pump per mirror.
        if tasks > 2 + 2 * mirrors + self.TASK_WARNING_HEADROOM:
            problems["tasks"] = f"{tasks} pending tasks"

        # Log when a problem first appears or a new kind of problem joins it,
        # then at most once per interval while it persists.
        if problems:
            new_kind = not problems.keys() <= self._logged_problem_kinds
            if new_kind or now - self._last_issue_log >= self.ISSUE_LOG_INTERVAL_SECONDS:
                logger.warning(f"{self._log_prefix} Audio sync degraded: {'; '.join(problems.values())}{self._take_drop_summary()} ({tasks} tasks, {mirrors} mirrors)")
                self._last_issue_log = now
                self._logged_problem_kinds |= problems.keys()
            self._issue_active = True
        elif self._issue_active:
            self._issue_active = False
            self._logged_problem_kinds = set()
            logger.info(f"{self._log_prefix} Audio sync recovered{self._take_drop_summary()} ({tasks} tasks, {mirrors} mirrors)")

    def _take_drop_summary(self):
        if not self._unreported_drops:
            return ""
        worst = sorted(self._unreported_drops.items(), key=lambda item: item[1], reverse=True)
        self._unreported_drops.clear()
        listed = ", ".join(f"{uuid} {self._audio_ms(size):.0f}ms" for uuid, size in worst[:5])
        more = f" and {len(worst) - 5} more" if len(worst) > 5 else ""
        return f"; dropped since last report: {listed}{more}"

    def _build_token(self, identity: str, name: str | None, video_grants: dict, attributes: dict[str, str] | None = None) -> str:
        now = int(time.time())
        claims = {
            "iss": self.api_key,
            "sub": identity,
            "name": name or identity,
            "nbf": now,
            "exp": now + self.TOKEN_TTL_SECONDS,
            "video": {"roomJoin": True, "room": self.room_name, **video_grants},
        }
        if attributes:
            claims["attributes"] = attributes
        return jwt.encode(claims, self.api_secret, algorithm="HS256")

    def _build_participant_token(self, participant_uuid: str, name: str | None) -> str:
        attributes = {self.OWNER_ATTRIBUTE: self._instance_id}
        if self.source_participant and self.source_participant.get("identity"):
            attributes[self.SOURCE_ATTRIBUTE] = self.source_participant["identity"]
        return self._build_token(
            participant_uuid,
            name,
            {"canPublish": True, "canPublishData": True, "canSubscribe": False, "canUpdateOwnMetadata": True},
            attributes=attributes,
        )

    def _build_source_subscriber_token(self, identity: str) -> str:
        return self._build_token(identity, identity, {"canPublish": False, "canSubscribe": True, "hidden": True})

    def _build_watcher_token(self, identity: str) -> str:
        return self._build_token(identity, identity, {"canPublish": False, "canPublishData": True, "canSubscribe": True, "hidden": True})

    def build_source_participant_configuration(self) -> RoomSyncSourceParticipantConfiguration | None:
        if not self.source_participant:
            return None
        token_identity = f"attendee-source-subscriber-{uuid.uuid4().hex[:8]}"
        return RoomSyncSourceParticipantConfiguration(
            livekit=LivekitRoomSyncSourceParticipantConfiguration(
                room_name=self.room_name,
                url=self.url,
                token=self._build_source_subscriber_token(token_identity),
                identity=self.source_participant.get("identity"),
                publish_on_behalf=self.source_participant.get("publish_on_behalf"),
            )
        )

    def _handle_join(self, participant_uuid, name):
        self._meeting_participants[participant_uuid] = name
        self._left_meeting_releases.pop(participant_uuid, None)
        self._schedule_claim_check(participant_uuid, "join")
        # If the watcher isn't ready, its initial/reconnect reconcile picks up
        # this join. No separate waiter task is needed for each participant.

    def _handle_leave(self, participant_uuid):
        self._meeting_participants.pop(participant_uuid, None)
        self._left_meeting_releases.pop(participant_uuid, None)
        self._claim_leases.pop(participant_uuid, None)
        self._takeover_counts.pop(participant_uuid, None)
        self._cancel_pending_claim(participant_uuid)
        mirror = self._mirrors.get(participant_uuid)
        if mirror is not None:
            self._release_mirror(mirror, self.RELEASE_LEFT_MEETING)

    def _release_mirror(self, mirror, release):
        self._detach_audio(mirror)
        mirror.release = mirror.release or release
        mirror.stopped.set()

    async def _watcher_loop(self):
        """Keep one watcher alive. This task is signalled, never cancelled."""
        identity = f"attendee-room-sync-watcher-{self._instance_id}"
        while not self._shutting_down:
            room = rtc.Room()
            self._watcher_room = room
            self._watcher_wake.clear()
            room.on("participant_connected", lambda p, room=room: self._on_roster_event(room, p, "connected"))
            room.on("participant_disconnected", lambda p, room=room: self._on_roster_event(room, p, "disconnected"))
            room.on("participant_attributes_changed", lambda changed, p, room=room: self._on_roster_event(room, p, "attributes", changed))
            room.on("data_received", lambda packet, room=room: self._on_control_message(room, packet))
            room.on("reconnecting", lambda *_, room=room: self._on_watcher_state(room, "reconnecting"))
            room.on("reconnected", lambda *_, room=room: self._on_watcher_state(room, "reconnected"))
            room.on("disconnected", lambda reason, room=room: self._on_watcher_state(room, "disconnected"))
            generation = self._watcher_generation
            try:
                await room.connect(self.url, self._build_watcher_token(identity), options=rtc.RoomOptions(auto_subscribe=False))
                if not self._shutting_down and not self._watcher_wake.is_set():
                    if generation == self._watcher_generation:
                        self._on_watcher_state(room, "reconnected")
                    await self._watcher_wake.wait()
            except Exception:
                logger.exception(f"{self._log_prefix} Watcher failed; retrying in {self.WATCHER_RETRY_SECONDS}s")
            finally:
                self._watcher_room = None
                self._watcher_ready = False
                self._watcher_generation += 1
                await self._disconnect_room(room)
            await self._wait_for_stop(self.WATCHER_RETRY_SECONDS)

    def _on_watcher_state(self, room, state):
        if room is not self._watcher_room:
            return
        self._watcher_ready = False
        self._watcher_generation += 1
        if state == "disconnected":
            self._watcher_wake.set()
        elif state == "reconnected" and not self._shutting_down:
            self._spawn(self._settle_watcher(room, self._watcher_generation))

    async def _settle_watcher(self, room, generation):
        await self._broadcast("hello")
        await self._wait_for_stop(self.MEMBERSHIP_SETTLE_SECONDS)
        if not self._shutting_down and room is self._watcher_room and generation == self._watcher_generation:
            self._watcher_ready = True
            self._reconcile("watcher ready")

    def _ready_watcher(self):
        return self._watcher_room if self._watcher_ready and not self._shutting_down else None

    async def _wait_for_stop(self, delay):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _maintenance_loop(self):
        next_reconcile = self._loop.time() + self.RECONCILE_INTERVAL_SECONDS
        while not self._shutting_down:
            await self._broadcast("heartbeat")
            now = self._loop.time()
            for bot, expires in list(self._members.items()):
                if expires <= now:
                    self._members.pop(bot)
                    logger.info(f"{self._log_prefix} Bot {bot} expired from the live set (heartbeat timeout)")
            if now >= next_reconcile:
                self._reconcile("periodic check")
                next_reconcile = now + self.RECONCILE_INTERVAL_SECONDS
            await self._wait_for_stop(min(self.HEARTBEAT_INTERVAL_SECONDS, max(0, next_reconcile - self._loop.time())))

    def _on_roster_event(self, room, participant, event, changed=None):
        if room is not self._watcher_room or self._shutting_down:
            return
        identity = participant.identity
        if event == "connected":
            self._claim_leases.pop(identity, None)
            return
        if identity not in self._meeting_participants:
            return
        attributes = participant.attributes or {}
        release = attributes.get(self.RELEASE_ATTRIBUTE)
        if event == "attributes":
            release = (changed or {}).get(self.RELEASE_ATTRIBUTE)
            if release not in (self.RELEASE_LEFT_MEETING, self.RELEASE_SHUTDOWN):
                return
        if release == self.RELEASE_LEFT_MEETING:
            self._left_meeting_releases[identity] = self._loop.time() + self.LEFT_MEETING_GRACE_SECONDS
            self._cancel_pending_claim(identity)
        else:
            self._schedule_claim_check(
                identity,
                "owner shut down" if release == self.RELEASE_SHUTDOWN else "participant dropped from room",
                exclude=(attributes.get(self.OWNER_ATTRIBUTE),),
                settle=0 if release == self.RELEASE_SHUTDOWN else self.UNRELEASED_SETTLE_SECONDS,
            )

    def _on_control_message(self, room, packet):
        if room is not self._watcher_room or self._shutting_down or getattr(packet, "topic", None) != self.CONTROL_TOPIC:
            return
        try:
            message = json.loads(bytes(packet.data))
            kind, bot_id = message["type"], message["bot"]
        except (ValueError, TypeError, KeyError):
            return
        if not isinstance(bot_id, str) or bot_id == self._instance_id:
            return
        if kind == "goodbye":
            if self._members.pop(bot_id, None) is not None:
                logger.info(f"{self._log_prefix} Bot {bot_id} left the live set (goodbye)")
            return
        now = self._loop.time()
        previous_expiry = self._members.get(bot_id, 0)
        self._members[bot_id] = now + self.MEMBER_TTL_SECONDS
        if previous_expiry <= now:
            logger.info(f"{self._log_prefix} Bot {bot_id} joined the live set; {len(self._live_bot_ids())} live bots")
        participant_uuid = message.get("participant")
        if kind == "hello":
            self._spawn(self._broadcast("heartbeat"))
        elif isinstance(participant_uuid, str) and participant_uuid:
            if kind == "claiming":
                self._claim_leases.setdefault(participant_uuid, {})[bot_id] = self._loop.time() + self.CLAIM_LEASE_SECONDS
            elif kind == "abandoned":
                leases = self._claim_leases.get(participant_uuid, {})
                leases.pop(bot_id, None)
                if not leases:
                    self._claim_leases.pop(participant_uuid, None)
                self._schedule_claim_check(participant_uuid, "claim abandoned", exclude=(bot_id,))

    async def _broadcast(self, kind, participant_uuid=None):
        watcher = self._watcher_room
        if watcher is None or not watcher.isconnected():
            return
        message = {"type": kind, "bot": self._instance_id}
        if participant_uuid is not None:
            message["participant"] = participant_uuid
        try:
            await asyncio.wait_for(
                watcher.local_participant.publish_data(json.dumps(message).encode(), reliable=True, topic=self.CONTROL_TOPIC),
                timeout=self.BROADCAST_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning(f"{self._log_prefix} Failed to broadcast {kind}: {error}")

    @staticmethod
    def _rendezvous_score(participant_uuid: str, bot_id: str) -> int:
        digest = hashlib.sha256(f"{participant_uuid}:{bot_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def _live_bot_ids(self):
        now = self._loop.time()
        return {bot for bot, expires in self._members.items() if expires > now} | {self._instance_id}

    def _claim_delay(self, participant_uuid, exclude):
        excluded = {bot for bot in exclude if bot}
        others = self._live_bot_ids() - excluded - {self._instance_id}
        if self._instance_id in excluded:
            return len(others) * self.TAKEOVER_STEP_SECONDS
        order = sorted(others | {self._instance_id}, key=lambda bot: (self._rendezvous_score(participant_uuid, bot), bot), reverse=True)
        return order.index(self._instance_id) * self.TAKEOVER_STEP_SECONDS

    def _claimable(self, participant_uuid):
        watcher = self._ready_watcher()
        if watcher is None:
            logger.debug(f"{self._log_prefix} Standing by for {participant_uuid}: watcher not ready")
            return False
        if participant_uuid not in self._meeting_participants:
            logger.debug(f"{self._log_prefix} Standing by for {participant_uuid}: no longer in the meeting")
            return False
        if self._left_meeting_releases.get(participant_uuid, 0) > self._loop.time():
            logger.debug(f"{self._log_prefix} Standing by for {participant_uuid}: owner reported it left the meeting; waiting for our LEAVE event")
            return False
        participant = watcher.remote_participants.get(participant_uuid)
        if participant is None:
            return True
        # Only shutdown permits replacing an existing identity. In particular,
        # a left_meeting attribute is not permission to resurrect that person.
        attributes = participant.attributes or {}
        if attributes.get(self.RELEASE_ATTRIBUTE) == self.RELEASE_SHUTDOWN:
            return True
        logger.debug(f"{self._log_prefix} Standing by for {participant_uuid}: present in room (owner={attributes.get(self.OWNER_ATTRIBUTE)}, release={attributes.get(self.RELEASE_ATTRIBUTE)})")
        return False

    def _schedule_claim_check(self, participant_uuid, trigger, exclude=(), settle=0.0):
        if self._ready_watcher() is not None and participant_uuid in self._meeting_participants:
            self._schedule_check_after(participant_uuid, self._claim_delay(participant_uuid, exclude) + settle, trigger)

    def _schedule_check_after(self, participant_uuid, delay, trigger):
        due = self._loop.time() + delay
        existing = self._pending_claims.get(participant_uuid)
        if existing is not None and existing.when() <= due:
            return
        self._cancel_pending_claim(participant_uuid)
        self._pending_claims[participant_uuid] = self._loop.call_at(due, self._claim_if_absent, participant_uuid, trigger)
        logger.info(f"{self._log_prefix} Scheduled claim check for {participant_uuid} in {delay:.2f}s (trigger: {trigger})")

    def _cancel_pending_claim(self, participant_uuid):
        timer = self._pending_claims.pop(participant_uuid, None)
        if timer is not None:
            timer.cancel()

    def _claim_if_absent(self, participant_uuid, trigger):
        self._pending_claims.pop(participant_uuid, None)
        if participant_uuid in self._mirrors:
            logger.debug(f"{self._log_prefix} Standing by for {participant_uuid}: already owned or connecting on this bot")
            return
        if not self._claimable(participant_uuid):
            return
        now = self._loop.time()
        leases = self._claim_leases.get(participant_uuid)
        if leases:
            active = {bot: expires for bot, expires in leases.items() if expires > now}
            if active:
                delay = max(active.values()) - now
                logger.info(f"{self._log_prefix} Waiting up to {delay:.2f}s for claims on {participant_uuid} by {', '.join(sorted(active))}")
                self._schedule_check_after(participant_uuid, delay, trigger)
            else:
                logger.warning(f"{self._log_prefix} Claims on {participant_uuid} by {', '.join(sorted(leases))} timed out; re-ranking without them")
                self._claim_leases.pop(participant_uuid, None)
                self._schedule_claim_check(participant_uuid, "claim timed out", exclude=leases)
            return
        if trigger != "join":
            count = self._takeover_counts.get(participant_uuid, 0) + 1
            self._takeover_counts[participant_uuid] = count
            log = logger.warning if count >= self.TAKEOVER_FLAP_WARNING_THRESHOLD else logger.info
            log(f"{self._log_prefix} Taking over {participant_uuid} ({trigger}, takeover #{count})")
        mirror = _Mirror(participant_uuid, rtc.Room())
        self._mirrors[participant_uuid] = mirror
        self._spawn(self._mirror_participant(participant_uuid, self._meeting_participants[participant_uuid], mirror))

    def _reconcile(self, trigger):
        watcher = self._ready_watcher()
        if watcher is None:
            return
        roster = self._meeting_participants.keys()
        owned = roster & self._mirrors.keys()
        present_elsewhere = (roster & watcher.remote_participants.keys()) - owned
        missing = roster - owned - present_elsewhere
        logger.info(f"{self._log_prefix} Ownership ({trigger}): {len(roster)} in meeting, {len(owned)} owned or connecting here, {len(present_elsewhere)} present elsewhere, {len(missing)} missing, {len(self._live_bot_ids())} live bots")
        for participant_uuid in self._meeting_participants:
            if participant_uuid not in self._mirrors and self._claimable(participant_uuid):
                self._schedule_claim_check(participant_uuid, trigger)

    async def _mirror_participant(self, participant_uuid, name, mirror):
        """Own a participant's entire lifetime, including slow connects/leaves."""
        room = mirror.room

        def disconnected(reason):
            mirror.disconnected = True
            self._detach_audio(mirror)
            mirror.stopped.set()
            if self._mirrors.get(participant_uuid) is mirror:
                self._mirrors.pop(participant_uuid, None)
            if mirror.release is None and not self._shutting_down:
                logger.warning(f"{self._log_prefix} Participant {participant_uuid} disconnected ({self._format_disconnect_reason(reason)}); standing down")

        room.on("disconnected", disconnected)
        failed = False
        source = None
        pump = None
        try:
            await self._broadcast("claiming", participant_uuid)
            # Sending the announcement yields; the roster/watcher may have
            # changed in the meantime. Check again before opening an identity.
            if mirror.stopped.is_set() or not self._claimable(participant_uuid):
                # LEAVE/shutdown is deliberate: do not invite a takeover.
                failed = not mirror.stopped.is_set()
                return
            await room.connect(self.url, self._build_participant_token(participant_uuid, name), options=rtc.RoomOptions(auto_subscribe=False))
            if not mirror.stopped.is_set() and not self._shutting_down:
                source = rtc.AudioSource(self.sample_rate, self.num_channels)
                track = rtc.LocalAudioTrack.create_audio_track(f"audio-{participant_uuid}", source)
                await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
                if not mirror.stopped.is_set() and not self._shutting_down:
                    buffer = _AudioBuffer(self._sample_frame_bytes, self._audio_buffer_bytes)
                    mirror.audio = buffer
                    mirror.source = source
                    self._audio_buffers[participant_uuid] = buffer
                    pump = self._spawn(self._pump_audio(participant_uuid, buffer, source))
                    logger.info(f"{self._log_prefix} Synced participant {participant_uuid}")
                    await mirror.stopped.wait()
        except Exception:
            failed = True
            logger.exception(f"{self._log_prefix} Failed to mirror participant {participant_uuid}")
        finally:
            self._detach_audio(mirror)
            if pump is not None:
                await self._stop_pump(pump)
            release = mirror.release or (self.RELEASE_SHUTDOWN if self._shutting_down else None)
            if release is not None and not mirror.disconnected:
                await self._mark_released(participant_uuid, room, release)
            await self._disconnect_room(room)
            if source is not None:
                await self._close_audio_source(source)
            if self._mirrors.get(participant_uuid) is mirror:
                self._mirrors.pop(participant_uuid, None)
            if failed and not self._shutting_down:
                await self._broadcast("abandoned", participant_uuid)
            # A rapid LEAVE/JOIN waits for the previous lifetime to close.
            # Unexpected disconnects instead defer to watcher events/ranking.
            if mirror.release == self.RELEASE_LEFT_MEETING:
                self._schedule_claim_check(participant_uuid, "rejoined")

    @staticmethod
    def _format_disconnect_reason(reason) -> str:
        try:
            return rtc.DisconnectReason.Name(reason)
        except Exception:
            return str(reason)

    async def _mark_released(self, participant_uuid, room, release):
        try:
            await asyncio.wait_for(room.local_participant.set_attributes({self.RELEASE_ATTRIBUTE: release}), timeout=self.RELEASE_ATTRIBUTE_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning(f"{self._log_prefix} Failed to release {participant_uuid} ({release}): {error}")

    async def _disconnect_room(self, room):
        try:
            await room.disconnect()
        except Exception:
            logger.exception(f"{self._log_prefix} Failed to disconnect room")

    async def _shutdown(self):
        self._shutting_down = True
        self._watcher_ready = False
        self._stop.set()
        for timer in self._pending_claims.values():
            timer.cancel()
        self._pending_claims.clear()
        await self._broadcast("goodbye")
        for mirror in self._mirrors.values():
            self._release_mirror(mirror, self.RELEASE_SHUTDOWN)
        self._watcher_wake.set()

        # Never cancel connection-owning tasks, even if cleanup's caller stops
        # waiting. They finish connects and close their own rooms in finally.
        pending = set(self._tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.SHUTDOWN_DRAIN_SECONDS)
        if pending:
            logger.warning(f"{self._log_prefix} Finishing {len(pending)} tasks in the background")
            _, pending = await asyncio.wait(pending, timeout=self.STRAGGLER_MAX_SECONDS)
        if pending:
            # Stopping a loop with an unresolved SDK connect can panic the
            # process. Keep this daemon thread alive until it is safe to stop.
            logger.error(f"{self._log_prefix} {len(pending)} tasks still pending; leaving the loop alive until they finish")
            await asyncio.gather(*pending, return_exceptions=True)
        self._loop.call_soon(self._loop.stop)

    def cleanup(self):
        """Release participants and stop the loop; repeated calls are harmless.

        Wait only a bounded time here. Slow SDK operations finish and close
        their rooms on the daemon thread, which stops itself afterwards.
        """
        with self._submission_lock:
            if self._cleanup_called:
                return
            self._cleanup_called = True
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result(timeout=self.SHUTDOWN_DRAIN_SECONDS + self.RELEASE_ATTRIBUTE_TIMEOUT_SECONDS + 10)
        except FutureTimeoutError:
            logger.warning(f"{self._log_prefix} Cleanup continues in the background")
        except Exception:
            logger.exception(f"{self._log_prefix} Cleanup failed")
        finally:
            self._watchdog_stop.set()
