"""
Redis storage for per participant video snapshot requests and their results.

A snapshot is a short-lived request/response between the web server and the bot,
so nothing about it is kept in the database:

- The web server creates the request under a per-bot key. The key is only set if it
  doesn't already exist, so it doubles as a lock that allows one pending or active
  snapshot per bot. It expires on its own so a bot that never answers can't hold it forever.
- The bot reads the request, captures the frames and stores the result under a
  per-request key, which also expires on its own if nobody picks it up.
- The web server polls for the result, then deletes both keys.

Callers should not touch the keys directly; everything goes through this class.
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass

import redis
from django.conf import settings


@dataclass(frozen=True)
class PerParticipantVideoSnapshotRequest:
    id: str
    webcam_resolution: str
    screenshare_resolution: str


@dataclass(frozen=True)
class PerParticipantVideoSnapshotResult:
    # Exactly one of these is set
    frames: list | None
    error: str | None


# Deletes the request key only if it still holds the given request, so a caller can't release a newer request
_DELETE_REQUEST_IF_ID_MATCHES_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if raw and cjson.decode(raw)['id'] == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class PerParticipantVideoSnapshotStore:
    # Longer than the web server waits for a result, so a slow capture can't overlap with a new request
    REQUEST_TTL_SECONDS = 10
    # Results the web server stopped waiting for are dropped after this long
    RESULT_TTL_SECONDS = 30

    def __init__(self):
        # A short-lived client rather than a pooled one, because bot pods try to keep their Redis connection count low.
        # Use the store as a context manager so the connection is closed when you're done.
        self._redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self._redis_client.close()

    @staticmethod
    def _request_key(bot_id: int) -> str:
        return f"per_participant_video_snapshot_request:{bot_id}"

    @staticmethod
    def _result_key(request_id: str) -> str:
        return f"per_participant_video_snapshot_result:{request_id}"

    def create_request(self, bot_id: int, webcam_resolution: str, screenshare_resolution: str) -> PerParticipantVideoSnapshotRequest | None:
        """Creates a snapshot request for the bot. Returns None if the bot already has a pending or active one."""
        snapshot_request = PerParticipantVideoSnapshotRequest(id=uuid.uuid4().hex, webcam_resolution=webcam_resolution, screenshare_resolution=screenshare_resolution)
        was_created = self._redis_client.set(self._request_key(bot_id), json.dumps(asdict(snapshot_request)), nx=True, ex=self.REQUEST_TTL_SECONDS)
        return snapshot_request if was_created else None

    def get_request(self, bot_id: int) -> PerParticipantVideoSnapshotRequest | None:
        raw_request = self._redis_client.get(self._request_key(bot_id))
        if raw_request is None:
            return None
        return PerParticipantVideoSnapshotRequest(**json.loads(raw_request))

    def delete_request(self, bot_id: int, request_id: str):
        """Frees the bot up for a new snapshot request. Does nothing if the bot's current request is a different one."""
        self._redis_client.eval(_DELETE_REQUEST_IF_ID_MATCHES_SCRIPT, 1, self._request_key(bot_id), request_id)

    def set_result(self, request_id: str, frames: list | None = None, error: str | None = None):
        result = PerParticipantVideoSnapshotResult(frames=None if error else (frames or []), error=error)
        self._redis_client.set(self._result_key(request_id), json.dumps(asdict(result)), ex=self.RESULT_TTL_SECONDS)

    def wait_for_result(self, request_id: str, timeout_seconds: float, poll_interval_seconds: float) -> PerParticipantVideoSnapshotResult | None:
        """Polls for the result of the request and removes it once found. Returns None if it doesn't arrive in time."""
        result_key = self._result_key(request_id)
        for _ in range(round(timeout_seconds / poll_interval_seconds)):
            time.sleep(poll_interval_seconds)
            raw_result = self._redis_client.get(result_key)
            if raw_result is not None:
                self._redis_client.delete(result_key)
                return PerParticipantVideoSnapshotResult(**json.loads(raw_result))
        return None
