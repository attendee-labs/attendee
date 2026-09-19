from dataclasses import dataclass


@dataclass(frozen=True)
class LivekitRoomSyncSourceParticipantConfiguration:
    """Mirrors the "livekit" object in ROOM_SYNC_SETTINGS_SCHEMA (see bots/serializers.py).

    ``url`` and ``token`` have no counterpart in the schema because they are not supplied by the
    caller: the url comes from the project's LiveKit credentials and the token is a LiveKit access
    token minted by us. The LiveKit JS SDK, which runs inside the bot's browser, needs both to
    connect to the room and subscribe to the source participant's tracks.
    """

    room_name: str
    url: str = None
    token: str = None
    identity: str = None
    publish_on_behalf: str = None

    def to_dict(self) -> dict:
        return {
            "room_name": self.room_name,
            "url": self.url,
            "token": self.token,
            "identity": self.identity,
            "publish_on_behalf": self.publish_on_behalf,
        }


@dataclass(frozen=True)
class RoomSyncSourceParticipantConfiguration:
    """Describes the external real-time room the bot should stream media from."""

    livekit: LivekitRoomSyncSourceParticipantConfiguration = None

    def to_dict(self) -> dict:
        return {
            "livekit": self.livekit.to_dict() if self.livekit else None,
        }
