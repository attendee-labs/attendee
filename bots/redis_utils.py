from packaging.version import Version

_redis_version = None

# Lua fallback for Redis < 7 which doesn't support EXPIRE ... NX.
_redis_lua_script_incr_and_expire_nx = None


def get_redis_version(redis_client):
    """Return the Redis server version as a packaging.version.Version object."""
    global _redis_version
    if _redis_version is None:
        _redis_version = Version(redis_client.info("server")["redis_version"])
    return _redis_version


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
    if get_redis_version(redis_client) >= Version("7.0"):
        with redis_client.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl, nx=True)
            count, ttl_set = pipe.execute()
    else:
        script = _get_redis_lua_script_incr_and_expire_nx(redis_client)
        count, ttl_set = script(keys=[key], args=[ttl])

    return count, ttl_set
