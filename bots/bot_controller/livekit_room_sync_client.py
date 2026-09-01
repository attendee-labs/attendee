import asyncio
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

    def __init__(self, room: str, credentials: dict, sample_rate: int = 48000, num_channels: int = 1, source_participant: dict = None):
        self.room_name = room
        self.url = credentials["url"]
        self.api_key = credentials["api_key"]
        self.api_secret = credentials["api_secret"]
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.source_participant = source_participant

        # Maps the meeting participant uuid to its LiveKit rtc.Room connection.
        self._rooms: dict[str, rtc.Room] = {}
        # Maps the meeting participant uuid to the rtc.AudioSource feeding its
        # published audio track.
        self._audio_sources: dict[str, rtc.AudioSource] = {}

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_event_loop, name="livekit-room-sync", daemon=True)
        self._thread.start()

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
        participant_uuid = event["participant_uuid"]
        event_type = event["event_type"]

        if event_type == ParticipantEventTypes.JOIN:
            name = None
            if participant is not None:
                name = participant.get("participant_full_name")
            self._run_coroutine(self._add_participant(participant_uuid, name))
        elif event_type == ParticipantEventTypes.LEAVE:
            self._run_coroutine(self._remove_participant(participant_uuid))
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

    def _build_token(self, identity: str, name: str | None, video_grants: dict) -> str:
        """Mint a LiveKit access token.

        A LiveKit access token is a JWT signed with the API secret (HS256). The
        API key is the issuer, the participant identity is the subject, and the
        room permissions live in the ``video`` grants claim (camelCase keys, per
        the LiveKit spec). This is a purely local signing operation, so no server
        round-trip is needed.

        ``video_grants`` supplies the permission-specific grants (e.g.
        ``canPublish``/``canSubscribe``/``hidden``); ``roomJoin`` and ``room`` are
        always added since every token this client mints is for joining this room.
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
        return jwt.encode(claims, self.api_secret, algorithm="HS256")

    def _build_participant_token(self, participant_uuid: str, name: str | None) -> str:
        """Mint a publish-only token for mirroring a meeting participant.

        ``canPublishData`` is granted in addition to ``canPublish`` so the synced
        participant can also mirror chat messages over LiveKit's data channel.
        """
        return self._build_token(participant_uuid, name, {"canPublish": True, "canPublishData": True, "canSubscribe": False})

    def _build_source_subscriber_token(self, identity: str) -> str:
        """Mint a hidden, subscribe-only LiveKit access token for the JS SDK.

        The JS SDK runs inside the bot's browser and uses this token to connect
        to the room and read the source participant's tracks. ``hidden`` keeps
        this connection out of the room roster so it is not visible to the other
        participants, and granting ``canSubscribe`` without ``canPublish`` limits
        it to reading tracks rather than producing any of its own.
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

    async def _add_participant(self, participant_uuid: str, name: str | None):
        if participant_uuid in self._rooms:
            logger.info(f"LiveKit participant already synced for {participant_uuid}, skipping add")
            return

        token = self._build_participant_token(participant_uuid, name)
        room = rtc.Room()

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

        self._rooms[participant_uuid] = room
        self._audio_sources[participant_uuid] = source
        logger.info(f"Synced LiveKit participant {participant_uuid} into room {self.room_name}")

    async def _remove_participant(self, participant_uuid: str):
        self._audio_sources.pop(participant_uuid, None)
        room = self._rooms.pop(participant_uuid, None)
        if room is None:
            logger.info(f"No LiveKit participant to remove for {participant_uuid}")
            return

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
        self._run_coroutine(self._capture_audio(participant_uuid, chunk_bytes))

    async def _capture_audio(self, participant_uuid: str, chunk_bytes: bytes):
        source = self._audio_sources.get(participant_uuid)
        if source is None:
            # Audio can arrive before the join event has been fully processed;
            # drop it rather than buffering, since it is realtime audio.
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
        rooms = list(self._rooms.items())
        self._rooms.clear()
        self._audio_sources.clear()
        for participant_uuid, room in rooms:
            try:
                await room.disconnect()
            except Exception as e:
                logger.exception(f"Failed to disconnect LiveKit participant for {participant_uuid}: {e}")

    def cleanup(self):
        """Disconnect all synced participants and stop the background loop."""
        try:
            future = self._run_coroutine(self._disconnect_all())
            future.result(timeout=10)
        except Exception as e:
            logger.exception(f"Error while disconnecting LiveKit participants during shutdown: {e}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
