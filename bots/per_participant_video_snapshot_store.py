"""
Redis storage for per participant video snapshot requests and their results.

A snapshot is a short-lived request/response between the web server and the bot,
so nothing about it is kept in the database:

- The web server creates the request under a per-bot key. The key is only set if it
  doesn't already exist, so it doubles as a lock that allows one pending or active
  snapshot per bot. It expires on its own so a bot that never answers can't hold it forever.
- The bot reads the request, captures the frames and pushes the result onto a
  per-request list, which also expires on its own if nobody picks it up.
- The web server waits for the result with a blocking pop, then deletes the request key.

Callers should not touch the keys directly; everything goes through this class.
"""

import json
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

    CONNECT_TIMEOUT_SECONDS = 2

    def __init__(self, socket_timeout_seconds: float | None = None):
        # A short-lived client rather than a pooled one, because bot pods try to keep their Redis connection count low.
        # Use the store as a context manager so the connection is closed when you're done.
        # socket_timeout_seconds bounds how long a single command can take. Leave it unset when you need wait_for_result,
        # since a socket timeout shorter than the BLPOP timeout would cut the wait short.
        self._redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS, socket_connect_timeout=self.CONNECT_TIMEOUT_SECONDS, socket_timeout=socket_timeout_seconds)

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
        result_key = self._result_key(request_id)
        # In one transaction so the list never exists without an expiry
        pipeline = self._redis_client.pipeline(transaction=True)
        pipeline.rpush(result_key, json.dumps(asdict(result)))
        pipeline.expire(result_key, self.RESULT_TTL_SECONDS)
        pipeline.execute()

    def wait_for_result(self, request_id: str, timeout_seconds: int) -> PerParticipantVideoSnapshotResult | None:
        """Blocks until the result of the request arrives and removes it. Returns None if it doesn't arrive in time."""
        # Redis treats a timeout of 0 as "wait forever", and versions before 6.0 only accept whole seconds
        if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            raise ValueError(f"timeout_seconds must be a whole number of seconds of at least 1, got {timeout_seconds}")
        popped = self._redis_client.blpop([self._result_key(request_id)], timeout=timeout_seconds)
        if popped is None:
            return None
        _, raw_result = popped
        return PerParticipantVideoSnapshotResult(**json.loads(raw_result))
