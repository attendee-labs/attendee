import asyncio
import hashlib
import logging
import threading
import time
import uuid

import jwt
from livekit import rtc

from bots.models import ParticipantEventTypes
from bots.room_sync_source_participant_configuration import (
    LivekitRoomSyncSourceParticipantConfiguration,
    RoomSyncSourceParticipantConfiguration,
)
from bots.room_sync_utils import does_participant_name_have_bot_indicator

logger = logging.getLogger(__name__)


class LivekitRoomSyncClient:
    """Mirrors meeting participants into a LiveKit room.

    Each meeting participant is represented as its own LiveKit participant by
    opening a dedicated ``rtc.Room`` connection using a per-participant access
    token. Each synced participant publishes a single mono audio track, and the
    meeting participant's audio is captured into that track so that the LiveKit
    room reflects both the meeting roster and who is speaking.

    The LiveKit realtime SDK is asyncio based, whereas the bot controller runs
    on a GLib main loop. To bridge the two, this client owns a background thread
    running its own asyncio event loop and schedules all LiveKit work onto it.
    The public methods are therefore safe to call from the GLib main thread and
    return without blocking.

    When several bots mirror the same meeting into the same room, ownership of
    each mirrored participant is coordinated deterministically:

    - Every mirrored participant carries an ``attendee.room_sync.owner``
      attribute (set via its access token) naming the bot that owns it. Watcher
      connections are hidden, so this is how bots discover each other: the set
      of live bots is the set of owners visible on the room roster.
    - For each meeting participant, the bots rank themselves with rendezvous
      hashing over ``(participant_uuid, bot_id)``. Every bot computes the same
      order, so the rank-0 bot claims immediately and the others only act as
      staggered fallbacks (rank * ``TAKEOVER_STEP_SECONDS``) if it is still
      missing by then. Hashing per participant spreads a failed bot's
      participants across the survivors.
    - Owners announce deliberate releases with an ``attendee.room_sync.release``
      attribute before disconnecting. ``shutdown`` means "take this over now":
      the next bot connects straight away, and LiveKit kicks the old connection
      as a duplicate identity, so there is no gap. ``left_meeting`` means the
      person left, so nobody takes it over. A participant that vanishes without
      a release (crash, lost connection) is taken over after a short fixed
      settle by rank.

    Bots that don't own anything are invisible to the others, so they rank
    themselves after all visible bots (with a deterministic offset to avoid
    colliding with each other). If two bots still claim at once, LiveKit keeps
    the newest connection for that identity and the other bot stands down.

    ``url`` is the LiveKit server URL (e.g. wss://your-project.livekit.cloud)
    and ``room`` is the name of the room to sync participants into.

    ``sample_rate`` and ``num_channels`` describe the per-participant PCM audio
    chunks that will be captured via ``send_audio_chunk``. They default to mono
    48kHz, which matches the per-participant audio produced by most adapters.

    The ``credentials`` dict is expected to contain:
        - ``url``: the LiveKit server URL (e.g. wss://your-project.livekit.cloud)
        - ``api_key``: the LiveKit API key used to mint per-participant tokens
        - ``api_secret``: the LiveKit API secret used to mint per-participant tokens
    """

    def __init__(self, room: str, credentials: dict, sample_rate: int = 48000, num_channels: int = 1, source_participant: dict = None, sync_to_room: bool = True):
        self.room_name = room
        self.url = credentials["url"]
        self.api_key = credentials["api_key"]
        self.api_secret = credentials["api_secret"]
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.source_participant = source_participant
        # When multiple LiveKit agents share a room, only one of them should
        # mirror the meeting's participants, audio and chat into the room.
        # When this is false, the client only streams the source participant's
        # media from the room into the meeting and does not mirror anything back.
        self.sync_to_room = sync_to_room

        # Maps the meeting participant uuid to its LiveKit rtc.Room connection.
        # Only contains participants this bot currently owns.
        self._rooms: dict[str, rtc.Room] = {}
        # Maps the meeting participant uuid to the rtc.AudioSource feeding its
        # published audio track.
        self._audio_sources: dict[str, rtc.AudioSource] = {}

        # Short random id for this client instance. Used as this bot's owner id
        # in the takeover ranking, in the watcher identity and in log lines so
        # logs from different bots sharing a room can be told apart.
        self._instance_id = uuid.uuid4().hex[:12]
        self._log_prefix = f"[LiveKit room sync {self._instance_id} room={self.room_name}]"

        # Meeting participants that should be mirrored into the room, whether or
        # not this bot owns them (uuid -> display name). Used to decide whether a
        # participant that disappeared from the room needs to be taken over.
        self._meeting_participants: dict[str, str | None] = {}
        # Participants this bot is in the middle of connecting, so a join and a
        # takeover for the same participant can't open two connections at once.
        self._connecting: set[str] = set()
        # Pending claim checks, keyed by participant uuid, with the loop time
        # they are due at so an earlier check can replace a later one.
        self._pending_claims: dict[str, tuple[asyncio.Task, float]] = {}
        # How many times this bot has taken over each participant. A count that
        # keeps growing means bots are fighting over the participant.
        self._takeover_counts: dict[str, int] = {}

        # Hidden connection used to observe the room roster.
        self._watcher_room: rtc.Room | None = None
        # Set once the first watcher connection attempt has finished (whether it
        # succeeded or not), so joins don't wait on it forever.
        self._watcher_attempted = asyncio.Event()
        self._watcher_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._shutting_down = False
        self._cleanup_called = False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_event_loop, name="livekit-room-sync", daemon=True)
        self._thread.start()

        if self.sync_to_room:
            logger.info(f"{self._log_prefix} Starting room sync with deterministic failover enabled")
            self._run_coroutine(self._connect_watcher())
            self._run_coroutine(self._periodic_reconcile())

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coroutine(self, coroutine):
        """Schedule a coroutine on the background loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def handle_participant_event(self, event, participant=None):
        """Add or remove a LiveKit participant based on a meeting participant event.

        ``event`` is the same in-memory participant event dict used elsewhere in
        the bot controller, containing ``participant_uuid``, ``event_type``,
        ``event_data`` and ``timestamp_ms``.

        ``participant`` is an optional participant metadata dict (as returned by
        the adapter's ``get_participant``) used to derive a display name. It is
        optional so that callers that only have the raw event can still use this
        method.
        """
        if not self.sync_to_room or self._cleanup_called:
            return

        participant_uuid = event["participant_uuid"]
        event_type = event["event_type"]

        if event_type == ParticipantEventTypes.JOIN:
            name = None
            if participant is not None:
                name = participant.get("participant_full_name")
            # Other Attendee room sync bots tag their display name with an
            # invisible marker. Don't mirror them into the room, otherwise room
            # sync bots would endlessly reflect each other back and forth.
            if does_participant_name_have_bot_indicator(name):
                logger.info(f"Skipping LiveKit sync for room sync bot participant {participant_uuid}")
                return
            self._run_coroutine(self._handle_join(participant_uuid, name))
        elif event_type == ParticipantEventTypes.LEAVE:
            self._run_coroutine(self._handle_leave(participant_uuid))
        else:
            # Other event types (speech start/stop, updates) do not change the
            # LiveKit roster, so there is nothing to sync.
            logger.debug(f"Ignoring participant event type {event_type} for LiveKit room sync")

    def handle_chat_message(self, chat_message):
        """Mirror a meeting chat message from its synced LiveKit participant.

        ``chat_message`` is the same in-memory chat message dict used elsewhere in
        the bot controller, containing at least ``participant_uuid`` and ``text``.
        The message is sent from the LiveKit participant that mirrors the meeting
        participant who authored it, so the LiveKit room reflects the meeting chat.
        """
        if not self.sync_to_room or self._cleanup_called:
            return

        participant_uuid = chat_message["participant_uuid"]
        text = chat_message.get("text")
        if not text:
            return
        self._run_coroutine(self._send_chat_message(participant_uuid, text))

    async def _send_chat_message(self, participant_uuid: str, text: str):
        room = self._rooms.get(participant_uuid)
        if room is None:
            # The chat message can arrive before the join event has been fully
            # processed, in which case there is no LiveKit participant to send
            # it from yet.
            logger.warning(f"No LiveKit participant to mirror chat message from for {participant_uuid}")
            return

        try:
            await room.local_participant.send_text(text, topic=self.CHAT_TOPIC)
        except Exception as e:
            logger.exception(f"Failed to mirror chat message for LiveKit participant {participant_uuid}: {e}")

    # Tokens are valid for 6 hours, which comfortably outlasts any meeting.
    TOKEN_TTL_SECONDS = 6 * 60 * 60

    # LiveKit's convention for chat messages sent over text streams. Clients that
    # follow this convention (including the LiveKit JS SDK) surface text sent on
    # this topic as chat messages.
    CHAT_TOPIC = "lk.chat"

    # Participant attribute naming the bot (instance id) that owns a mirrored
    # participant. Set through the access token, so it is present from the
    # moment the participant joins. This is how bots discover each other.
    OWNER_ATTRIBUTE = "attendee.room_sync.owner"
    # Participant attribute an owner sets just before deliberately disconnecting
    # a mirrored participant, so other bots know why it is going away.
    RELEASE_ATTRIBUTE = "attendee.room_sync.release"
    # The person left the meeting: nobody should take the participant over.
    RELEASE_LEFT_MEETING = "left_meeting"
    # The owning bot is shutting down: the next bot should take over right away.
    RELEASE_SHUTDOWN = "shutdown"
    # How long to wait for a release attribute to be acknowledged before
    # disconnecting anyway.
    RELEASE_ATTRIBUTE_TIMEOUT_SECONDS = 2

    # Spacing between successive bots in the takeover order. The rank-0 bot for
    # a participant claims immediately; the rank-N bot rechecks after
    # N * TAKEOVER_STEP_SECONDS and only claims if it is still missing. This
    # should comfortably exceed the time it takes a bot to connect a
    # participant and for that to show up on the other bots' watchers.
    TAKEOVER_STEP_SECONDS = 2.0

    # Extra fixed wait before acting on a mirrored participant that vanished
    # without its owner announcing a release (crash, lost connection, or a
    # duplicate-identity swap between two bots). Gives a swap time to settle so
    # it is not mistaken for a failure. Crashes take LiveKit several seconds to
    # detect anyway, so this adds little to the overall gap.
    UNRELEASED_SETTLE_SECONDS = 0.5

    # How long a join waits for the first watcher connection attempt before
    # deciding whether to claim the participant.
    WATCHER_READY_TIMEOUT_SECONDS = 10

    # Delay between watcher connection attempts.
    WATCHER_RETRY_SECONDS = 5

    # Safety net: periodically compare the meeting roster against the room and
    # schedule claim checks for anything missing, in case an event was missed.
    RECONCILE_INTERVAL_SECONDS = 30

    # Log takeovers at warning level once this bot has taken over the same
    # participant this many times, since that suggests bots are fighting over it.
    TAKEOVER_FLAP_WARNING_THRESHOLD = 3

    def _build_token(self, identity: str, name: str | None, video_grants: dict, attributes: dict[str, str] | None = None) -> str:
        """Mint a LiveKit access token.

        A LiveKit access token is a JWT signed with the API secret (HS256). The
        API key is the issuer, the participant identity is the subject, and the
        room permissions live in the ``video`` grants claim (camelCase keys, per
        the LiveKit spec). This is a purely local signing operation, so no server
        round-trip is needed.

        ``video_grants`` supplies the permission-specific grants (e.g.
        ``canPublish``/``canSubscribe``/``hidden``); ``roomJoin`` and ``room`` are
        always added since every token this client mints is for joining this room.

        ``attributes`` become the participant's initial attributes, visible to
        everyone in the room.
        """
        now = int(time.time())
        claims = {
            "iss": self.api_key,
            "sub": identity,
            "name": name or identity,
            "nbf": now,
            "exp": now + self.TOKEN_TTL_SECONDS,
            "video": {
                "roomJoin": True,
                "room": self.room_name,
                **video_grants,
            },
        }
        if attributes:
            claims["attributes"] = attributes
        return jwt.encode(claims, self.api_secret, algorithm="HS256")

    def _build_participant_token(self, participant_uuid: str, name: str | None) -> str:
        """Mint a publish-only token for mirroring a meeting participant.

        ``canPublishData`` is granted in addition to ``canPublish`` so the synced
        participant can also mirror chat messages over LiveKit's data channel.
        ``canUpdateOwnMetadata`` lets this bot set the release attribute before
        disconnecting. The owner attribute advertises this bot to the others.
        """
        return self._build_token(
            participant_uuid,
            name,
            {"canPublish": True, "canPublishData": True, "canSubscribe": False, "canUpdateOwnMetadata": True},
            attributes={self.OWNER_ATTRIBUTE: self._instance_id},
        )

    def _build_source_subscriber_token(self, identity: str) -> str:
        """Mint a hidden, subscribe-only LiveKit access token for the JS SDK.

        The JS SDK runs inside the bot's browser and uses this token to connect
        to the room and read the source participant's tracks. ``hidden`` keeps
        this connection out of the room roster so it is not visible to the other
        participants, and granting ``canSubscribe`` without ``canPublish`` limits
        it to reading tracks rather than producing any of its own.
        """
        return self._build_token(identity, identity, {"canPublish": False, "canSubscribe": True, "hidden": True})

    def _build_watcher_token(self, identity: str) -> str:
        """Mint a hidden, subscribe-only token for this bot's watcher connection.

        The watcher only observes the room roster. ``hidden`` keeps it out of the
        roster seen by agents and other bots, and it never subscribes to media
        because it connects with ``auto_subscribe=False``.
        """
        return self._build_token(identity, identity, {"canPublish": False, "canSubscribe": True, "hidden": True})

    def build_source_participant_configuration(self) -> RoomSyncSourceParticipantConfiguration | None:
        """Build the configuration the in-browser LiveKit JS SDK uses to subscribe
        to the source participant's tracks.

        Returns ``None`` when no source participant was configured, in which case
        the bot only mirrors meeting participants into LiveKit and does not stream
        any external media back into the meeting.

        ``self.source_participant`` mirrors the ``source_participant`` object in
        ROOM_SYNC_SETTINGS_SCHEMA and contains exactly one of ``identity`` or
        ``publish_on_behalf`` identifying which participant to stream from. Those
        are passed through unchanged so the JS SDK can select the participant,
        while the ``url`` and hidden subscribe-only ``token`` are supplied by us.
        """
        if not self.source_participant:
            return None

        identity = self.source_participant.get("identity")
        publish_on_behalf = self.source_participant.get("publish_on_behalf")

        token_identity = f"attendee-source-subscriber-{uuid.uuid4().hex[:8]}"
        livekit = LivekitRoomSyncSourceParticipantConfiguration(
            room_name=self.room_name,
            url=self.url,
            token=self._build_source_subscriber_token(token_identity),
            identity=identity,
            publish_on_behalf=publish_on_behalf,
        )
        return RoomSyncSourceParticipantConfiguration(livekit=livekit)

    @staticmethod
    def _format_disconnect_reason(reason) -> str:
        try:
            return rtc.DisconnectReason.Name(reason)
        except Exception:
            return str(reason)

    # ------------------------------------------------------------------
    # Meeting roster handling
    # ------------------------------------------------------------------

    async def _handle_join(self, participant_uuid: str, name: str | None):
        """Record a meeting participant and schedule a ranked claim check for it."""
        self._meeting_participants[participant_uuid] = name
        logger.info(f"{self._log_prefix} Meeting participant {participant_uuid} joined, deciding whether to claim it")

        if not self._watcher_attempted.is_set():
            try:
                await asyncio.wait_for(self._watcher_attempted.wait(), timeout=self.WATCHER_READY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(f"{self._log_prefix} Timed out after {self.WATCHER_READY_TIMEOUT_SECONDS}s waiting for the watcher while handling join of {participant_uuid}")

        if participant_uuid not in self._meeting_participants:
            logger.info(f"{self._log_prefix} Meeting participant {participant_uuid} left before it could be claimed")
            return
        if participant_uuid in self._rooms or participant_uuid in self._connecting:
            logger.info(f"{self._log_prefix} Meeting participant {participant_uuid} is already owned by this bot")
            return

        if self._watcher_room is None:
            logger.warning(f"{self._log_prefix} Watcher unavailable, claiming {participant_uuid} without checking whether another bot already owns it")
            await self._add_participant(participant_uuid, name, reason="join")
            return

        self._schedule_claim_check(participant_uuid, trigger="join")

    async def _handle_leave(self, participant_uuid: str):
        """Forget a meeting participant and release it if this bot owns it.

        Any pending claim check for the participant is left to run; it will see
        the participant is no longer in the meeting and do nothing.
        """
        self._meeting_participants.pop(participant_uuid, None)
        self._takeover_counts.pop(participant_uuid, None)
        logger.info(f"{self._log_prefix} Meeting participant {participant_uuid} left")
        await self._remove_participant(participant_uuid)

    # ------------------------------------------------------------------
    # Watcher connection
    # ------------------------------------------------------------------

    async def _connect_watcher(self, initial_delay: float = 0):
        """Connect the hidden watcher, retrying until it succeeds or we shut down."""
        self._watcher_task = asyncio.current_task()
        if initial_delay:
            await asyncio.sleep(initial_delay)

        identity = f"attendee-room-sync-watcher-{self._instance_id}"
        attempt = 0
        while not self._shutting_down:
            attempt += 1
            room = rtc.Room()
            room.on("participant_connected", lambda participant, room=room: self._on_watcher_participant_connected(room, participant))
            room.on("participant_disconnected", lambda participant, room=room: self._on_watcher_participant_disconnected(room, participant))
            room.on(
                "participant_attributes_changed",
                lambda changed_attributes, participant, room=room: self._on_watcher_participant_attributes_changed(room, changed_attributes, participant),
            )
            room.on("reconnecting", lambda *_, room=room: self._on_watcher_reconnecting(room))
            room.on("reconnected", lambda *_, room=room: self._on_watcher_reconnected(room))
            room.on("disconnected", lambda reason, room=room: self._on_watcher_disconnected(room, reason))

            try:
                await room.connect(self.url, self._build_watcher_token(identity), options=rtc.RoomOptions(auto_subscribe=False))
            except Exception as e:
                logger.exception(f"{self._log_prefix} Failed to connect watcher (attempt {attempt}), retrying in {self.WATCHER_RETRY_SECONDS}s: {e}")
                self._watcher_attempted.set()
                await asyncio.sleep(self.WATCHER_RETRY_SECONDS)
                continue

            if self._shutting_down:
                await room.disconnect()
                return

            self._watcher_room = room
            self._watcher_attempted.set()
            logger.info(f"{self._log_prefix} Watcher connected as {identity} (attempt {attempt}), {len(room.remote_participants)} participants currently visible in the room")
            self._reconcile("watcher connected")
            return

    def _on_watcher_participant_connected(self, room: rtc.Room, participant):
        if room is not self._watcher_room:
            logger.info(f"{self._log_prefix} Observed participant {participant.identity} connect to the LiveKit room, but not the watcher room")
            return
        identity = participant.identity
        if identity in self._meeting_participants:
            owner = (participant.attributes or {}).get(self.OWNER_ATTRIBUTE) or "unknown"
            logger.info(f"{self._log_prefix} Observed mirrored participant {identity} connect to the LiveKit room (owner: {owner})")
        else:
            logger.debug(f"{self._log_prefix} Observed non-mirrored participant {identity} connect to the LiveKit room")

    def _on_watcher_participant_disconnected(self, room: rtc.Room, participant):
        if room is not self._watcher_room or self._shutting_down:
            return
        identity = participant.identity
        if identity not in self._meeting_participants:
            # Agents, the source participant, or a mirrored participant that
            # already left the meeting. Nothing to take over.
            logger.debug(f"{self._log_prefix} Observed participant {identity} leave the LiveKit room, not in meeting roster so ignoring")
            return

        attributes = participant.attributes or {}
        owner = attributes.get(self.OWNER_ATTRIBUTE)
        release = attributes.get(self.RELEASE_ATTRIBUTE)

        if release == self.RELEASE_LEFT_MEETING:
            # The owner saw the person leave the meeting. Our own LEAVE event
            # should follow shortly; don't put a ghost back in the meeting.
            logger.info(f"{self._log_prefix} Mirrored participant {identity} was released by bot {owner} because it left the meeting, not taking it over")
            return

        if release == self.RELEASE_SHUTDOWN:
            # Normally already handed over when the attribute changed; this is
            # the fallback in case that check didn't claim it.
            logger.info(f"{self._log_prefix} Mirrored participant {identity} left the LiveKit room after bot {owner} released it for shutdown")
            self._schedule_claim_check(identity, trigger="owner shut down", departed_owner=owner)
            return

        logger.info(f"{self._log_prefix} Mirrored participant {identity} (owner: {owner}) left the LiveKit room without being released while still in the meeting")
        self._schedule_claim_check(identity, trigger="participant dropped from room", departed_owner=owner, settle=self.UNRELEASED_SETTLE_SECONDS)

    def _on_watcher_participant_attributes_changed(self, room: rtc.Room, changed_attributes: dict, participant):
        if room is not self._watcher_room or self._shutting_down:
            return
        if (changed_attributes or {}).get(self.RELEASE_ATTRIBUTE) != self.RELEASE_SHUTDOWN:
            return
        identity = participant.identity
        if identity not in self._meeting_participants:
            return
        owner = (participant.attributes or {}).get(self.OWNER_ATTRIBUTE)
        # Make-before-break handoff: the next bot connects while the old
        # connection is still up, and LiveKit kicks the old one as a duplicate
        # identity, so the participant never disappears from the room.
        logger.info(f"{self._log_prefix} Bot {owner} is shutting down and releasing {identity}, handing it over")
        self._schedule_claim_check(identity, trigger="owner shutting down", departed_owner=owner)

    def _on_watcher_reconnecting(self, room: rtc.Room):
        if room is not self._watcher_room:
            logger.info(f"{self._log_prefix} Observed watcher room reconnecting, but not the watcher room")
            return
        logger.warning(f"{self._log_prefix} Watcher connection interrupted, LiveKit SDK is reconnecting")

    def _on_watcher_reconnected(self, room: rtc.Room):
        if room is not self._watcher_room:
            logger.info(f"{self._log_prefix} Observed watcher room reconnected, but not the watcher room")
            return
        logger.info(f"{self._log_prefix} Watcher reconnected")
        # Roster events may have been missed while reconnecting.
        self._reconcile("watcher reconnected")

    def _on_watcher_disconnected(self, room: rtc.Room, reason):
        if room is not self._watcher_room:
            logger.info(f"{self._log_prefix} Observed watcher room disconnected, but not the watcher room")
            return
        self._watcher_room = None
        if self._shutting_down:
            return
        logger.warning(f"{self._log_prefix} Watcher disconnected (reason: {self._format_disconnect_reason(reason)}), reconnecting in {self.WATCHER_RETRY_SECONDS}s. Takeovers are paused until it reconnects")
        self._watcher_task = self._loop.create_task(self._connect_watcher(initial_delay=self.WATCHER_RETRY_SECONDS))

    # ------------------------------------------------------------------
    # Takeover ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _rendezvous_score(participant_uuid: str, bot_id: str) -> int:
        """Stable per-(participant, bot) score. Uses sha256 rather than ``hash()``
        because ``hash()`` is randomized per process and bots must agree."""
        digest = hashlib.sha256(f"{participant_uuid}:{bot_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def _is_mirrored_in_room(self, watcher: rtc.Room, participant_uuid: str) -> bool:
        """Whether a live, unreleased mirrored participant exists in the room."""
        participant = watcher.remote_participants.get(participant_uuid)
        if participant is None:
            return False
        # A participant its owner has released is on its way out, so treat it
        # as absent. This is what lets a shutdown handoff happen without a gap.
        return not (participant.attributes or {}).get(self.RELEASE_ATTRIBUTE)

    def _visible_bot_ids(self, watcher: rtc.Room) -> set[str]:
        """Bots currently owning at least one unreleased mirrored participant."""
        bot_ids = set()
        for participant in watcher.remote_participants.values():
            attributes = participant.attributes or {}
            owner = attributes.get(self.OWNER_ATTRIBUTE)
            if owner and not attributes.get(self.RELEASE_ATTRIBUTE):
                bot_ids.add(owner)
        if self._rooms:
            bot_ids.add(self._instance_id)
        return bot_ids

    def _claim_delay(self, watcher: rtc.Room, participant_uuid: str, departed_owner: str | None) -> tuple[float, str]:
        """How long this bot should wait before claiming a missing participant.

        Every bot ranks the visible bots by rendezvous score for this
        participant, so they all agree on the order. ``departed_owner`` is
        excluded because the participant just disappeared from it, even if its
        other participants are still visible for a moment.
        """
        candidates = self._visible_bot_ids(watcher)
        candidates.discard(departed_owner)

        if not candidates:
            # No other bot is visible (a single bot, or a cold start), so there
            # is nobody to defer to.
            return 0.0, "no other bots visible"

        if self._instance_id in candidates:
            order = sorted(candidates, key=lambda bot_id: (self._rendezvous_score(participant_uuid, bot_id), bot_id), reverse=True)
            rank = order.index(self._instance_id)
            return rank * self.TAKEOVER_STEP_SECONDS, f"rank {rank + 1} of {len(order)}"

        # This bot owns nothing, so the other bots can't see it and don't count
        # it in their ranking. Go after all of them, with a deterministic offset
        # so several such bots don't all fire at the same moment.
        offset = self._rendezvous_score(participant_uuid, self._instance_id) / 2**64
        return (len(candidates) + offset) * self.TAKEOVER_STEP_SECONDS, f"after all {len(candidates)} visible bots"

    def _schedule_claim_check(self, participant_uuid: str, trigger: str, departed_owner: str | None = None, settle: float = 0.0):
        """Schedule a check of whether to claim a participant, delayed by this bot's rank.

        Must be called on the background event loop.
        """
        if self._shutting_down:
            return
        watcher = self._watcher_room
        if watcher is None:
            logger.warning(f"{self._log_prefix} Not scheduling claim check for {participant_uuid}: watcher unavailable; will reconcile once it reconnects (trigger: {trigger})")
            return

        delay, position = self._claim_delay(watcher, participant_uuid, departed_owner)
        delay += settle
        due = self._loop.time() + delay

        existing = self._pending_claims.get(participant_uuid)
        if existing is not None:
            existing_task, existing_due = existing
            if not existing_task.done() and existing_due <= due:
                logger.debug(f"{self._log_prefix} Claim check for {participant_uuid} already pending sooner, not scheduling another (trigger: {trigger})")
                return
            existing_task.cancel()

        task = self._loop.create_task(self._delayed_claim_check(participant_uuid, delay, trigger))
        self._pending_claims[participant_uuid] = (task, due)
        logger.info(f"{self._log_prefix} Scheduled claim check for {participant_uuid} in {delay:.2f}s ({position}, trigger: {trigger})")

    async def _delayed_claim_check(self, participant_uuid: str, delay: float, trigger: str):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            entry = self._pending_claims.get(participant_uuid)
            if entry is not None and entry[0] is asyncio.current_task():
                self._pending_claims.pop(participant_uuid, None)
        await self._claim_if_absent(participant_uuid, trigger)

    async def _claim_if_absent(self, participant_uuid: str, trigger: str):
        """Claim a participant if it is still in the meeting but missing from the room."""
        if self._shutting_down:
            return
        if participant_uuid not in self._meeting_participants:
            logger.info(f"{self._log_prefix} Claim check for {participant_uuid}: no longer in the meeting, nothing to do")
            return
        if participant_uuid in self._rooms or participant_uuid in self._connecting:
            logger.info(f"{self._log_prefix} Claim check for {participant_uuid}: already owned by this bot, nothing to do")
            return

        watcher = self._watcher_room
        if watcher is None:
            logger.warning(f"{self._log_prefix} Claim check for {participant_uuid}: watcher unavailable, skipping; will reconcile once it reconnects")
            return
        if self._is_mirrored_in_room(watcher, participant_uuid):
            logger.info(f"{self._log_prefix} Claim check for {participant_uuid}: present in the LiveKit room (owned by another bot), standing by")
            return

        name = self._meeting_participants[participant_uuid]
        if trigger == "join":
            logger.info(f"{self._log_prefix} Claiming {participant_uuid}: in the meeting but not in the LiveKit room")
            await self._add_participant(participant_uuid, name, reason="join")
            return

        count = self._takeover_counts.get(participant_uuid, 0) + 1
        self._takeover_counts[participant_uuid] = count
        message = f"{self._log_prefix} Taking over {participant_uuid}: in the meeting but absent from the LiveKit room (trigger: {trigger}, takeover #{count} by this bot)"
        if count >= self.TAKEOVER_FLAP_WARNING_THRESHOLD:
            logger.warning(f"{message}. Repeated takeovers of the same participant may mean bots are fighting over it")
        else:
            logger.info(message)

        await self._add_participant(participant_uuid, name, reason=f"takeover ({trigger})")

    def _reconcile(self, trigger: str):
        """Log current ownership and schedule claim checks for anything missing from the room.

        Must be called on the background event loop.
        """
        if self._shutting_down:
            return
        watcher = self._watcher_room
        if watcher is None:
            logger.info(f"{self._log_prefix} Skipping reconcile ({trigger}): watcher unavailable")
            return

        owned = [p for p in self._meeting_participants if p in self._rooms or p in self._connecting]
        owned_elsewhere = [p for p in self._meeting_participants if p not in owned and self._is_mirrored_in_room(watcher, p)]
        missing = [p for p in self._meeting_participants if p not in owned and not self._is_mirrored_in_room(watcher, p)]

        logger.info(f"{self._log_prefix} Ownership ({trigger}): {len(self._meeting_participants)} in meeting, {len(owned)} owned by this bot, {len(owned_elsewhere)} owned by other bots, {len(missing)} missing from room, {len(self._visible_bot_ids(watcher))} bots visible")
        for participant_uuid in missing:
            self._schedule_claim_check(participant_uuid, trigger=trigger)

    async def _periodic_reconcile(self):
        self._reconcile_task = asyncio.current_task()
        while not self._shutting_down:
            await asyncio.sleep(self.RECONCILE_INTERVAL_SECONDS)
            self._reconcile("periodic check")

    # ------------------------------------------------------------------
    # Per-participant connections
    # ------------------------------------------------------------------

    def _handle_room_disconnected(self, participant_uuid: str, room: rtc.Room, reason):
        """Drop tracked state for a LiveKit connection that was closed out from under us.

        Runs on the background event loop, which is the only place ``_rooms`` and
        ``_audio_sources`` are mutated. Disconnects we initiate ourselves (in
        ``_remove_participant`` / ``_disconnect_all``) pop the room before
        disconnecting, so the identity check below makes them a no-op. It also
        ensures a stale connection never clears state belonging to a newer
        connection for the same participant.

        The most common unexpected cause is ``DUPLICATE_IDENTITY``: another bot
        connected with the same participant identity and LiveKit kicked this
        connection. That bot now owns the participant, so we simply stop
        tracking it rather than reconnecting and fighting over it. For any other
        reason the participant is now missing from the room, so the watchers
        notice and the next bot in the takeover order re-syncs it.
        """
        if self._rooms.get(participant_uuid) is not room:
            return

        self._rooms.pop(participant_uuid, None)
        self._audio_sources.pop(participant_uuid, None)

        if reason == rtc.DisconnectReason.DUPLICATE_IDENTITY:
            logger.warning(f"{self._log_prefix} LiveKit participant {participant_uuid} was taken over by another connection with the same identity (most likely another bot), standing down")
        else:
            logger.warning(f"{self._log_prefix} LiveKit participant {participant_uuid} was disconnected unexpectedly (reason: {self._format_disconnect_reason(reason)}), the next bot in the takeover order will re-sync it if it is still in the meeting")

    async def _add_participant(self, participant_uuid: str, name: str | None, reason: str = "join"):
        if participant_uuid in self._rooms or participant_uuid in self._connecting:
            logger.info(f"LiveKit participant already synced for {participant_uuid}, skipping add")
            return

        self._connecting.add(participant_uuid)
        try:
            await self._connect_participant(participant_uuid, name, reason)
        finally:
            self._connecting.discard(participant_uuid)

    async def _connect_participant(self, participant_uuid: str, name: str | None, reason: str):
        token = self._build_participant_token(participant_uuid, name)
        room = rtc.Room()
        room.on("disconnected", lambda reason: self._handle_room_disconnected(participant_uuid, room, reason))

        try:
            await room.connect(self.url, token, options=rtc.RoomOptions(auto_subscribe=False))
        except Exception as e:
            logger.exception(f"Failed to connect LiveKit participant for {participant_uuid}: {e}")
            return

        try:
            source = rtc.AudioSource(self.sample_rate, self.num_channels)
            track = rtc.LocalAudioTrack.create_audio_track(f"audio-{participant_uuid}", source)
            await room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
        except Exception as e:
            logger.exception(f"Failed to publish audio track for LiveKit participant {participant_uuid}: {e}")
            await room.disconnect()
            return

        # The participant may have left the meeting (or we may have started
        # shutting down) while we were connecting. Don't leave a ghost behind.
        if self._shutting_down or participant_uuid not in self._meeting_participants:
            logger.info(f"{self._log_prefix} Meeting participant {participant_uuid} left (or client is shutting down) while connecting, disconnecting it")
            release = self.RELEASE_SHUTDOWN if self._shutting_down else self.RELEASE_LEFT_MEETING
            await self._mark_released(participant_uuid, room, release)
            try:
                await room.disconnect()
            except Exception as e:
                logger.exception(f"Failed to disconnect LiveKit participant for {participant_uuid}: {e}")
            return

        self._rooms[participant_uuid] = room
        self._audio_sources[participant_uuid] = source
        logger.info(f"{self._log_prefix} Synced LiveKit participant {participant_uuid} into room {self.room_name} (reason: {reason})")

    async def _mark_released(self, participant_uuid: str, room: rtc.Room, release: str):
        """Tell the other bots why this participant is about to disconnect.

        Best effort: if it fails, the other bots treat the disconnect as
        unexplained and fall back to the settle-then-rank path.
        """
        try:
            await asyncio.wait_for(
                room.local_participant.set_attributes({self.RELEASE_ATTRIBUTE: release}),
                timeout=self.RELEASE_ATTRIBUTE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix} Failed to mark LiveKit participant {participant_uuid} as released ({release}): {e}")

    async def _remove_participant(self, participant_uuid: str):
        self._audio_sources.pop(participant_uuid, None)
        room = self._rooms.pop(participant_uuid, None)
        if room is None:
            logger.info(f"{self._log_prefix} No LiveKit participant owned by this bot to remove for {participant_uuid}")
            return

        # Let the other bots know the person left, so they don't take it over.
        await self._mark_released(participant_uuid, room, self.RELEASE_LEFT_MEETING)

        try:
            await room.disconnect()
        except Exception as e:
            logger.exception(f"Failed to disconnect LiveKit participant for {participant_uuid}: {e}")
            return

        logger.info(f"Removed LiveKit participant {participant_uuid} from room {self.room_name}")

    def send_audio_chunk(self, participant_uuid: str, chunk_bytes: bytes):
        """Capture a chunk of a meeting participant's audio into LiveKit.

        ``chunk_bytes`` is raw little-endian signed 16-bit PCM at the sample
        rate and channel count this client was constructed with. Safe to call
        from the GLib main thread; the work is scheduled onto the background
        event loop.
        """
        if not self.sync_to_room or self._cleanup_called:
            return

        self._run_coroutine(self._capture_audio(participant_uuid, chunk_bytes))

    async def _capture_audio(self, participant_uuid: str, chunk_bytes: bytes):
        source = self._audio_sources.get(participant_uuid)
        if source is None:
            # Audio can arrive before the join event has been fully processed,
            # or for a participant another bot owns; drop it rather than
            # buffering, since it is realtime audio.
            return

        bytes_per_sample = 2 * self.num_channels
        samples_per_channel = len(chunk_bytes) // bytes_per_sample
        if samples_per_channel == 0:
            return

        frame = rtc.AudioFrame(
            data=chunk_bytes,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
            samples_per_channel=samples_per_channel,
        )
        try:
            await source.capture_frame(frame)
        except Exception as e:
            logger.exception(f"Failed to capture audio frame for LiveKit participant {participant_uuid}: {e}")

    async def _disconnect_all(self):
        self._shutting_down = True

        # Stop background work first so nothing reacts to our own disconnects.
        for task in (self._watcher_task, self._reconcile_task, *(task for task, _ in self._pending_claims.values())):
            if task is not None and not task.done():
                task.cancel()
        self._pending_claims.clear()

        watcher = self._watcher_room
        self._watcher_room = None
        if watcher is not None:
            try:
                await watcher.disconnect()
            except Exception as e:
                logger.exception(f"{self._log_prefix} Failed to disconnect watcher: {e}")

        rooms = list(self._rooms.items())
        self._rooms.clear()
        self._audio_sources.clear()
        logger.info(f"{self._log_prefix} Shutting down, handing off {len(rooms)} owned participants to other bots")

        # Announce the release first so other bots connect while ours are still
        # up (make-before-break). If another bot takes one over in the meantime,
        # LiveKit kicks ours as a duplicate identity, which is fine: we have
        # already stopped tracking it, and disconnecting it again is harmless.
        await asyncio.gather(*(self._mark_released(participant_uuid, room, self.RELEASE_SHUTDOWN) for participant_uuid, room in rooms))

        for participant_uuid, room in rooms:
            try:
                await room.disconnect()
            except Exception as e:
                logger.exception(f"Failed to disconnect LiveKit participant for {participant_uuid}: {e}")

    def cleanup(self):
        """Disconnect all synced participants and stop the background loop.

        Safe to call more than once; calls after the first are no-ops.
        """
        if self._cleanup_called:
            return
        self._cleanup_called = True
        self._shutting_down = True
        try:
            future = self._run_coroutine(self._disconnect_all())
            future.result(timeout=10)
        except Exception as e:
            logger.exception(f"Error while disconnecting LiveKit participants during shutdown: {e}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
