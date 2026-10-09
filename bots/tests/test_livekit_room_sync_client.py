"""Dependency-free regression tests using an in-memory LiveKit event simulator.

Run: python manage.py test bots.tests.test_livekit_room_sync_client
These verify client logic, not native SDK or server integration.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import types
import unittest
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from unittest.mock import patch


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    return result


class Options:
    def __init__(self, **values):
        self.__dict__.update(values)


class Reasons:
    DUPLICATE_IDENTITY = 2
    CLIENT_INITIATED = 1

    @staticmethod
    def Name(value):
        return str(value)


class Source:
    def __init__(self, sample_rate, num_channels):
        self.sample_rate, self.num_channels = sample_rate, num_channels
        self.frames = []

    async def capture_frame(self, frame):
        self.frames.append(frame)


class Bus:
    def __init__(self):
        self.rooms = set()
        self.visible = {}
        self.connect_gates = {}
        self.publish_gates = {}
        self.disconnect_gates = {}
        self.fail_connect = set()
        self.fail_publish = set()
        self.rooms_created = []
        self.duplicates = 0
        self.messages = []
        self.before_broadcast = None

    def room(self):
        room = Room(self)
        self.rooms_created.append(room)
        return room

    async def connect(self, room, token):
        room.claims = token
        identity = token["sub"]
        gate = self.connect_gates.get(identity)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                room.connect_cancelled = True
                raise
        if identity in self.fail_connect:
            raise RuntimeError("connect failed")
        room.identity = identity
        room.hidden = token["video"].get("hidden", False)
        room.local_participant.identity = identity
        room.local_participant.attributes = token.get("attributes", {}).copy()
        room.connected = True
        room.remote_participants = {key: value.local_participant for key, value in self.visible.items()}
        self.rooms.add(room)
        if not room.hidden:
            old = self.visible.get(identity)
            if old is not None:
                self.duplicates += 1
                self.drop(old, Reasons.DUPLICATE_IDENTITY)
            self.visible[identity] = room
            for other in list(self.rooms):
                if other is not room:
                    other.remote_participants[identity] = room.local_participant
                    other.emit("participant_connected", room.local_participant)

    def drop(self, room, reason=0):
        if not room.connected:
            return
        room.connected = False
        self.rooms.discard(room)
        if self.visible.get(room.identity) is room:
            del self.visible[room.identity]
            for other in list(self.rooms):
                other.remote_participants.pop(room.identity, None)
                other.emit("participant_disconnected", room.local_participant)
        room.emit("disconnected", reason)


class Room:
    def __init__(self, bus):
        self.bus = bus
        self.handlers = {}
        self.remote_participants = {}
        self.local_participant = Participant(self)
        self.connected = False
        self.hidden = False
        self.identity = None
        self.connect_cancelled = False
        self.claims = None

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def isconnected(self):
        return self.connected

    def emit(self, name, *args):
        for handler in self.handlers.get(name, []):
            handler(*args)

    async def connect(self, url, token, options):
        assert options.auto_subscribe is False
        await self.bus.connect(self, json.loads(token))

    async def disconnect(self):
        gate = self.bus.disconnect_gates.get(self.identity)
        if gate is not None:
            await gate.wait()
        self.bus.drop(self, Reasons.CLIENT_INITIATED)


class Participant:
    def __init__(self, room):
        self.room = room
        self.identity = None
        self.attributes = {}
        self.texts = []
        self.tracks = []

    async def publish_data(self, data, reliable, topic):
        if not self.room.connected:
            raise RuntimeError("room is not connected")
        message = json.loads(data)
        self.room.bus.messages.append(message)
        if self.room.bus.before_broadcast is not None:
            await self.room.bus.before_broadcast(self.room, message)
        for other in list(self.room.bus.rooms):
            if other is not self.room:
                other.emit("data_received", Options(data=data, topic=topic))

    async def set_attributes(self, attributes):
        if not self.room.connected:
            raise RuntimeError("room is not connected")
        self.attributes.update(attributes)
        for other in list(self.room.bus.rooms):
            if other is not self.room:
                other.emit("participant_attributes_changed", attributes, self)

    async def publish_track(self, track, options):
        gate = self.room.bus.publish_gates.get(self.identity)
        if gate is not None:
            await gate.wait()
        if self.identity in self.room.bus.fail_publish:
            raise RuntimeError("publish failed")
        self.tracks.append((track, options))

    async def send_text(self, text, topic):
        self.texts.append((text, topic))


rtc = Options(
    Room=Room,
    AudioSource=Source,
    AudioFrame=Options,
    RoomOptions=Options,
    LocalAudioTrack=Options(create_audio_track=lambda name, source: Options(name=name, source=source)),
    TrackPublishOptions=Options,
    TrackSource=Options(SOURCE_MICROPHONE=1),
    DisconnectReason=Reasons,
)
stubs = {
    "jwt": module("jwt", encode=lambda claims, secret, algorithm: json.dumps(claims)),
    "livekit": module("livekit", rtc=rtc),
    "bots": module("bots"),
    "bots.models": module("bots.models", ParticipantEventTypes=Options(JOIN="join", LEAVE="leave")),
    "bots.room_sync_source_participant_configuration": module(
        "bots.room_sync_source_participant_configuration",
        LivekitRoomSyncSourceParticipantConfiguration=Options,
        RoomSyncSourceParticipantConfiguration=Options,
    ),
    "bots.room_sync_utils": module("bots.room_sync_utils", does_participant_name_have_bot_indicator=lambda name: name == "BOT"),
}
spec = importlib.util.spec_from_file_location("subject", Path(__file__).resolve().parent.parent / "bot_controller" / "livekit_room_sync_client.py")
subject = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = subject
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(subject)
Client = subject.LivekitRoomSyncClient


class LoopProxy:
    """Exercise shutdown without stopping unittest's shared event loop."""

    def __init__(self, loop):
        self.loop = loop
        self.stopped = False

    def stop(self):
        self.stopped = True

    def __getattr__(self, name):
        return getattr(self.loop, name)


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = Bus()
        self.clients = []
        self.room_patch = patch.object(rtc, "Room", self.bus.room)
        self.room_patch.start()
        self.log_patch = patch.object(subject, "logger", logging.getLogger("disabled-test-log"))
        self.log_patch.start()
        subject.logger.disabled = True

    async def asyncTearDown(self):
        for gate in (*self.bus.connect_gates.values(), *self.bus.publish_gates.values(), *self.bus.disconnect_gates.values()):
            gate.set()
        await asyncio.gather(*(c._shutdown() for c in self.clients if not c._shutting_down))
        await asyncio.sleep(0)
        self.assertFalse(any(room.connect_cancelled for room in self.bus.rooms_created))
        self.assertFalse(self.bus.rooms)
        self.log_patch.stop()
        self.room_patch.stop()

    def client(self, **kwargs):
        proxy = LoopProxy(asyncio.get_running_loop())
        with patch.object(subject.asyncio, "new_event_loop", return_value=proxy), patch.object(subject.threading.Thread, "start"):
            client = Client("room", {"url": "url", "api_key": "key", "api_secret": "secret"}, **kwargs)
        client.MEMBERSHIP_SETTLE_SECONDS = 0.01
        client.HEARTBEAT_INTERVAL_SECONDS = 0.005
        client.MEMBER_TTL_SECONDS = 0.03
        client.TAKEOVER_STEP_SECONDS = 0.01
        client.UNRELEASED_SETTLE_SECONDS = 0.005
        client.CLAIM_LEASE_SECONDS = 0.04
        client.LEFT_MEETING_GRACE_SECONDS = 0.04
        client.WATCHER_RETRY_SECONDS = 0.005
        client.RECONCILE_INTERVAL_SECONDS = 0.04
        client.SHUTDOWN_DRAIN_SECONDS = 0.01
        client.STRAGGLER_MAX_SECONDS = 0.02
        self.clients.append(client)
        return client

    async def until(self, condition, timeout=1):
        async def poll():
            while not condition():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(poll(), timeout)

    async def ready(self, *clients):
        await self.until(lambda: all(c._ready_watcher() for c in clients))

    def join(self, client, identity="p", name="Alice"):
        client.handle_participant_event({"participant_uuid": identity, "event_type": "join"}, {"participant_full_name": name})

    def leave(self, client, identity="p"):
        client.handle_participant_event({"participant_uuid": identity, "event_type": "leave"})

    async def mirrored(self, client, identity="p"):
        await self.until(lambda: identity in client._mirrors and client._mirrors[identity].source is not None)
        return client._mirrors[identity]

    async def test_public_signatures_and_defaults(self):
        import inspect

        self.assertEqual(list(inspect.signature(Client).parameters), ["room", "credentials", "sample_rate", "num_channels", "source_participant", "sync_to_room"])
        self.assertEqual(list(inspect.signature(Client.handle_participant_event).parameters), ["self", "event", "participant"])
        self.assertEqual(list(inspect.signature(Client.handle_chat_message).parameters), ["self", "chat_message"])
        self.assertEqual(list(inspect.signature(Client.send_audio_chunk).parameters), ["self", "participant_uuid", "chunk_bytes"])

    async def test_tokens_and_source_configuration(self):
        client = self.client(sync_to_room=False, source_participant={"publish_on_behalf": "agent"})
        config = client.build_source_participant_configuration().livekit
        self.assertEqual((config.room_name, config.url, config.identity, config.publish_on_behalf), ("room", "url", None, "agent"))
        token = json.loads(config.token)
        self.assertEqual(token["video"], {"roomJoin": True, "room": "room", "canPublish": False, "canSubscribe": True, "hidden": True})
        token = json.loads(client._build_participant_token("p", "Alice"))
        self.assertEqual(token["attributes"], {client.OWNER_ATTRIBUTE: client._instance_id})
        self.assertEqual(token["exp"] - token["nbf"], 21600)

    async def test_sync_disabled_and_bot_filter(self):
        client = self.client(sync_to_room=False)
        self.join(client)
        client.send_audio_chunk("p", b"\0\0")
        client.handle_chat_message({"participant_uuid": "p", "text": "hello"})
        await asyncio.sleep(0.02)
        self.assertFalse(self.bus.rooms_created)
        self.assertFalse(client._meeting_participants)
        enabled = self.client()
        self.join(enabled, name="BOT")
        await self.ready(enabled)
        self.assertFalse(enabled._meeting_participants)

    async def test_join_before_watcher_ready_and_audio_chat(self):
        client = self.client()
        self.join(client)
        mirror = await self.mirrored(client)
        client.send_audio_chunk("p", b"\0\0" * 480)
        client.handle_chat_message({"participant_uuid": "p", "text": "hello"})
        await self.until(lambda: bool(mirror.source.frames) and bool(mirror.room.local_participant.texts))
        self.assertEqual(mirror.source.frames[0].samples_per_channel, 480)
        self.assertEqual(mirror.room.local_participant.texts, [("hello", "lk.chat")])

    async def test_no_claim_without_ready_watcher(self):
        client = self.client()
        await self.ready(client)
        client._on_watcher_state(client._watcher_room, "reconnecting")
        self.join(client)
        await asyncio.sleep(0.025)
        self.assertNotIn("p", self.bus.visible)
        client._on_watcher_state(client._watcher_room, "reconnected")
        await self.mirrored(client)

    async def test_stale_settle_cannot_restore_ready(self):
        client = self.client()
        await self.ready(client)
        room = client._watcher_room
        client._on_watcher_state(room, "reconnected")
        await asyncio.sleep(0.002)
        client._on_watcher_state(room, "reconnecting")
        await asyncio.sleep(0.02)
        self.assertIsNone(client._ready_watcher())

    async def test_stale_watcher_events_ignored_after_retry(self):
        client = self.client()
        await self.ready(client)
        old = client._watcher_room
        self.bus.drop(old)
        await self.until(lambda: client._watcher_room is not None and client._watcher_room is not old and client._watcher_ready)
        old.emit("reconnecting")
        old.emit("disconnected", 0)
        self.assertTrue(client._watcher_ready)
        self.join(client)
        await self.mirrored(client)

    async def test_distributed_ownership_and_shutdown_handoff(self):
        clients = [self.client() for _ in range(3)]
        await self.ready(*clients)
        identities = {f"p{i}" for i in range(20)}
        for client in clients:
            for identity in identities:
                self.join(client, identity)
        await self.until(lambda: set(self.bus.visible) == identities)
        await asyncio.sleep(0.03)
        self.assertEqual(self.bus.duplicates, 0)
        self.assertTrue(all(client._mirrors for client in clients))
        self.assertEqual(sum(len(client._mirrors) for client in clients), 20)
        await clients[0]._shutdown()
        await self.until(lambda: set(self.bus.visible) == identities)
        self.assertFalse(clients[0]._mirrors)
        self.assertEqual(sum(len(client._mirrors) for client in clients[1:]), 20)

    async def test_late_bot_does_not_steal_existing_ownership(self):
        first = self.client()
        self.join(first)
        original = await self.mirrored(first)
        second = self.client()
        self.join(second)
        await self.ready(second)
        await asyncio.sleep(0.05)
        self.assertIs(self.bus.visible["p"], original.room)
        self.assertFalse(second._mirrors)

    async def test_all_claimant_leases_are_respected(self):
        client = self.client()
        await self.ready(client)
        now = client._loop.time()
        client._claim_leases["p"] = {"a": now + 0.015, "b": now + 0.06}
        self.join(client)
        await asyncio.sleep(0.03)
        self.assertNotIn("p", self.bus.visible)
        await self.mirrored(client)

    async def test_abandonment_removes_only_sender_lease(self):
        client = self.client()
        await self.ready(client)
        client._claim_leases["p"] = {"a": client._loop.time() + 1, "b": client._loop.time() + 1}
        client._on_control_message(client._watcher_room, Options(topic=client.CONTROL_TOPIC, data=b'{"type":"abandoned","bot":"a","participant":"p"}'))
        self.assertEqual(set(client._claim_leases["p"]), {"b"})

    async def test_rank_and_excluded_owner_match_original_formula(self):
        clients = [self.client() for _ in range(3)]
        await self.ready(*clients)
        ids = {c._instance_id for c in clients}
        for identity in ["p1", "p2", "p3"]:
            order = sorted(ids, key=lambda bot: (Client._rendezvous_score(identity, bot), bot), reverse=True)
            for client in clients:
                self.assertEqual(client._claim_delay(identity, ()), order.index(client._instance_id) * client.TAKEOVER_STEP_SECONDS)
                self.assertEqual(client._claim_delay(identity, (client._instance_id,)), 2 * client.TAKEOVER_STEP_SECONDS)

    async def test_timer_replacement_and_leave_cancels_claim(self):
        client = self.client()
        await self.ready(client)
        client._meeting_participants["p"] = "Alice"
        client._schedule_check_after("p", 0.2, "join")
        old = client._pending_claims["p"]
        client._schedule_check_after("p", 0.3, "join")
        self.assertIs(client._pending_claims["p"], old)
        client._schedule_check_after("p", 0.1, "join")
        self.assertTrue(old.cancelled())
        client._handle_leave("p")
        self.assertNotIn("p", client._pending_claims)

    async def test_leave_during_connect_cannot_create_ghost(self):
        client = self.client()
        gate = self.bus.connect_gates["p"] = asyncio.Event()
        self.join(client)
        await self.until(lambda: any(r.claims and r.claims["sub"] == "p" for r in self.bus.rooms_created))
        self.leave(client)
        await asyncio.sleep(0.002)
        gate.set()
        await self.until(lambda: "p" not in client._mirrors)
        self.assertNotIn("p", self.bus.visible)

    async def test_leave_during_track_publish_cannot_create_ghost(self):
        client = self.client()
        gate = self.bus.publish_gates["p"] = asyncio.Event()
        self.join(client)
        await self.until(lambda: "p" in self.bus.visible)
        self.leave(client)
        await asyncio.sleep(0.002)
        gate.set()
        await self.until(lambda: "p" not in client._mirrors)
        self.assertNotIn("p", self.bus.visible)

    async def test_rapid_leave_rejoin_waits_for_old_connection(self):
        client = self.client()
        self.join(client)
        old = await self.mirrored(client)
        gate = self.bus.disconnect_gates["p"] = asyncio.Event()
        self.leave(client)
        self.join(client, name="New name")
        await asyncio.sleep(0.02)
        self.assertEqual(self.bus.duplicates, 0)
        gate.set()
        await self.until(lambda: "p" in client._mirrors and client._mirrors["p"] is not old and client._mirrors["p"].source is not None)
        self.assertEqual(self.bus.visible["p"].claims["name"], "New name")

    async def test_left_meeting_release_suppresses_other_bot(self):
        clients = [self.client() for _ in range(2)]
        await self.ready(*clients)
        for client in clients:
            self.join(client)
        await self.until(lambda: "p" in self.bus.visible)
        owner = next(c for c in clients if "p" in c._mirrors)
        follower = next(c for c in clients if c is not owner)
        self.leave(owner)
        await self.until(lambda: "p" not in self.bus.visible)
        await asyncio.sleep(0.02)
        self.assertNotIn("p", self.bus.visible)
        self.leave(follower)

    async def test_connect_and_publish_failures_release_resources(self):
        client = self.client()
        self.bus.fail_connect.add("bad-connect")
        self.bus.fail_publish.add("bad-publish")
        self.join(client, "bad-connect")
        self.join(client, "bad-publish")
        await self.until(lambda: {m.get("participant") for m in self.bus.messages if m["type"] == "abandoned"} >= {"bad-connect", "bad-publish"})
        self.assertFalse(self.bus.visible)
        self.assertFalse(client._mirrors)

    async def test_watcher_loss_during_claim_announcement_prevents_connect(self):
        client = self.client()
        await self.ready(client)

        async def before_broadcast(room, message):
            if message["type"] == "claiming":
                client._on_watcher_state(room, "reconnecting")

        self.bus.before_broadcast = before_broadcast
        self.join(client)
        await self.until(lambda: any(m["type"] == "abandoned" for m in self.bus.messages))
        self.assertFalse(self.bus.visible)

    async def test_leave_during_claim_announcement_does_not_invite_takeover(self):
        clients = [self.client() for _ in range(2)]
        await self.ready(*clients)
        owner, follower = sorted(clients, key=lambda c: c._claim_delay("p", ()))
        # Keep the follower's original fallback well beyond the observation
        # window. An abandoned message would re-rank it to claim immediately.
        follower.TAKEOVER_STEP_SECONDS = 0.2
        owner.CLAIM_LEASE_SECONDS = follower.CLAIM_LEASE_SECONDS = 0.3

        async def before_broadcast(room, message):
            if message["type"] == "claiming" and message["bot"] == owner._instance_id:
                owner._handle_leave("p")

        self.bus.before_broadcast = before_broadcast
        self.join(owner)
        self.join(follower)
        await self.until(lambda: any(m["type"] == "claiming" for m in self.bus.messages))
        await self.until(lambda: "p" not in owner._mirrors)
        await asyncio.sleep(0.03)
        self.assertFalse(any(m["type"] == "abandoned" for m in self.bus.messages))
        self.assertFalse(any(r.claims and r.claims["sub"] == "p" for r in self.bus.rooms_created))
        self.assertNotIn("p", self.bus.visible)
        self.leave(follower)

    async def test_broadcast_skips_unconnected_watcher(self):
        client = self.client()
        gate = self.bus.connect_gates[f"attendee-room-sync-watcher-{client._instance_id}"] = asyncio.Event()
        with patch.object(subject.logger, "warning") as warning:
            await self.until(lambda: client._watcher_room is not None)
            await client._broadcast("hello")
            await asyncio.sleep(0.02)
            self.assertFalse(self.bus.messages)
            warning.assert_not_called()
        gate.set()
        await self.ready(client)
        self.assertTrue(any(m["type"] == "hello" for m in self.bus.messages))

    async def test_reconnect_handlers_ignore_extra_event_arguments(self):
        client = self.client()
        await self.ready(client)
        room = client._watcher_room
        room.emit("reconnecting", "unexpected argument")
        self.assertIsNone(client._ready_watcher())
        room.emit("reconnected", "unexpected argument")
        await self.ready(client)
        self.assertIs(client._ready_watcher(), room)

    async def test_duplicate_disconnect_stands_down(self):
        client = self.client()
        self.join(client)
        old = await self.mirrored(client)
        external = self.bus.room()
        await external.connect("url", client._build_participant_token("p", "Alice"), Options(auto_subscribe=False))
        await asyncio.sleep(0.06)
        self.assertIs(self.bus.visible["p"], external)
        self.assertNotIn("p", client._mirrors)
        self.assertIsNone(old.source)
        self.bus.drop(external)

    async def test_unexpected_drop_is_recovered(self):
        client = self.client()
        self.join(client)
        old = await self.mirrored(client)
        self.bus.drop(old.room)
        await self.until(lambda: "p" in client._mirrors and client._mirrors["p"] is not old and client._mirrors["p"].source is not None)

    async def test_shutdown_during_connect_drains_without_cancellation(self):
        client = self.client()
        gate = self.bus.connect_gates["p"] = asyncio.Event()
        self.join(client)
        await self.until(lambda: any(r.claims and r.claims["sub"] == "p" for r in self.bus.rooms_created))
        shutdown = asyncio.create_task(client._shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutdown.done())
        self.assertFalse(client._loop.stopped)
        gate.set()
        await shutdown
        await asyncio.sleep(0)
        self.assertTrue(client._loop.stopped)
        self.assertFalse(self.bus.rooms)

    async def test_shutdown_during_watcher_connect_drains_without_cancellation(self):
        client = self.client()
        gate = self.bus.connect_gates[f"attendee-room-sync-watcher-{client._instance_id}"] = asyncio.Event()
        await self.until(lambda: bool(self.bus.rooms_created))
        shutdown = asyncio.create_task(client._shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutdown.done())
        gate.set()
        await shutdown
        self.assertFalse(self.bus.rooms)

    async def test_malformed_control_packets_are_ignored(self):
        client = self.client()
        await self.ready(client)
        for data in [b"[]", b"null", b"bad", b"{}", b'{"type":"claiming","bot":{},"participant":"p"}', b'{"type":"claiming","bot":"x","participant":{}}']:
            client._on_control_message(client._watcher_room, Options(data=data, topic=client.CONTROL_TOPIC))
        self.assertFalse(client._claim_leases)


class ThreadTests(unittest.TestCase):
    def test_cleanup_uses_concurrent_future_timeout(self):
        # Emulate the distinct exception class used before Python 3.11,
        # without waiting for an actual shutdown timeout.
        class LegacyFutureTimeout(Exception):
            pass

        self.assertIs(subject.FutureTimeoutError, FutureTimeoutError)
        client = Client.__new__(Client)
        client._submission_lock = subject.threading.Lock()
        client._cleanup_called = False
        client._loop = object()
        client._log_prefix = "[test]"
        future = unittest.mock.Mock()
        future.result.side_effect = LegacyFutureTimeout

        def schedule(coroutine, loop):
            coroutine.close()
            return future

        with patch.object(subject, "FutureTimeoutError", LegacyFutureTimeout), patch.object(subject.asyncio, "run_coroutine_threadsafe", side_effect=schedule), patch.object(subject, "logger") as logger:
            client.cleanup()
            logger.warning.assert_called_once_with("[test] Cleanup continues in the background")
            logger.exception.assert_not_called()

    def test_real_background_thread_cleanup_and_idempotence(self):
        import time

        bus = Bus()
        with patch.object(rtc, "Room", bus.room):
            client = Client("room", {"url": "url", "api_key": "key", "api_secret": "secret"})
            client.handle_participant_event({"participant_uuid": "p", "event_type": "join"})
            deadline = time.monotonic() + 2
            while "p" not in bus.visible and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIn("p", bus.visible)
            client.cleanup()
            client.cleanup()
            client.send_audio_chunk("p", b"\0\0")
            client.handle_participant_event({"participant_uuid": "p2", "event_type": "join"})
            client._thread.join(timeout=1)
            self.assertFalse(client._thread.is_alive())
            self.assertTrue(client._loop.is_closed())
            self.assertFalse(bus.rooms)

    def test_cleanup_immediately_after_construction(self):
        bus = Bus()
        with patch.object(rtc, "Room", bus.room):
            client = Client("room", {"url": "url", "api_key": "key", "api_secret": "secret"})
            client.cleanup()
            client._thread.join(timeout=1)
            self.assertFalse(client._thread.is_alive())
            self.assertFalse(bus.rooms)


if __name__ == "__main__":
    unittest.main(verbosity=2)
