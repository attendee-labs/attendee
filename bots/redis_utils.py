# Lua fallback for Redis < 7 which doesn't support EXPIRE ... NX.
import logging
from contextlib import contextmanager
from threading import local

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_redis_lua_script_incr_and_expire_nx = None
_redis_client = None
_held_lock_keys = local()


def _get_redis_lua_script_incr_and_expire_nx(redis_client):
    """Lua fallback for Redis < 7 which doesn't support EXPIRE ... NX."""
    global _redis_lua_script_incr_and_expire_nx
    if _redis_lua_script_incr_and_expire_nx is None:
        _redis_lua_script_incr_and_expire_nx = redis_client.register_script(
            """
            -- incr_and_expire_nx: INCR key, set EXPIRE only on first creation
            local count = redis.call('INCR', KEYS[1])
            local ttl_set = 0
            if count == 1 then
                ttl_set = redis.call('EXPIRE', KEYS[1], ARGV[1])
            end
            return {count, ttl_set}
            """
        )
    return _redis_lua_script_incr_and_expire_nx


def incr_and_expire_nx(redis_client, key, ttl):
    """Atomically INCR a key and set its TTL only if the key is new.

    Returns (count, ttl_set) where ttl_set indicates whether EXPIRE was applied.
    """
    script = _get_redis_lua_script_incr_and_expire_nx(redis_client)
    count, ttl_set = script(keys=[key], args=[ttl])
    return count, ttl_set


def get_redis_client():
    """Process-wide Redis client for locks and shared utilities."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    return _redis_client


def zoom_oauth_connection_token_lock_key(zoom_oauth_connection_id) -> str:
    return f"lock:zoom-oauth-connection-token:{zoom_oauth_connection_id}"


def calendar_sync_lock_key(calendar_id) -> str:
    return f"lock:calendar-sync:{calendar_id}"


@contextmanager
def redis_lock(key: str, timeout: int = 180, blocking_timeout: int = 180):
    """
    Blocking Redis lock.

    Re-entrant for the same key within the same thread so task-level locks can
    nest around helpers that also take the lock (e.g. token refresh).
    """
    held = getattr(_held_lock_keys, "keys", None)
    if held is None:
        held = set()
        _held_lock_keys.keys = held

    if key in held:
        yield
        return

    client = get_redis_client()
    lock = client.lock(key, timeout=timeout, blocking_timeout=blocking_timeout)
    acquired = lock.acquire(blocking=True)
    if not acquired:
        raise TimeoutError(f"Could not acquire redis lock {key} within {blocking_timeout}s")

    held.add(key)
    try:
        yield
    finally:
        held.discard(key)
        try:
            lock.release()
        except redis.exceptions.LockError:
            logger.warning("Redis lock %s already released or expired", key)


@contextmanager
def zoom_oauth_connection_token_lock(zoom_oauth_connection_id, timeout: int = 180, blocking_timeout: int = 180):
    """Serialize Zoom OAuth token refresh for a connection (refresh/sync/join)."""
    with redis_lock(zoom_oauth_connection_token_lock_key(zoom_oauth_connection_id), timeout=timeout, blocking_timeout=blocking_timeout):
        yield


@contextmanager
def calendar_sync_lock(calendar_id, timeout: int = 300, blocking_timeout: int = 300):
    """Serialize calendar sync mutations (esp. Microsoft refresh_token rotation)."""
    with redis_lock(calendar_sync_lock_key(calendar_id), timeout=timeout, blocking_timeout=blocking_timeout):
        yield
