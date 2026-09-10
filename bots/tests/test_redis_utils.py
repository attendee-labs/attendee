import time

import redis
from django.conf import settings
from django.test import TestCase

from bots.redis_utils import (
    calendar_sync_lock_key,
    incr_and_expire_nx,
    redis_lock,
    zoom_oauth_connection_token_lock,
    zoom_oauth_connection_token_lock_key,
)


class IncrAndExpireNxTest(TestCase):
    def setUp(self):
        self.redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        self.test_key = f"test_incr_and_expire_nx:{time.time()}"

    def tearDown(self):
        self.redis_client.delete(self.test_key)
        self.redis_client.close()

    def test_first_call_sets_count_and_ttl(self):
        count, ttl_set = incr_and_expire_nx(self.redis_client, self.test_key, ttl=10)
        self.assertEqual(count, 1)
        self.assertEqual(ttl_set, 1)
        self.assertGreater(self.redis_client.ttl(self.test_key), 0)

    def test_subsequent_calls_increment_without_resetting_ttl(self):
        incr_and_expire_nx(self.redis_client, self.test_key, ttl=10)
        count, ttl_set = incr_and_expire_nx(self.redis_client, self.test_key, ttl=10)
        self.assertEqual(count, 2)
        self.assertEqual(ttl_set, 0)

    def test_count_increments_correctly(self):
        for i in range(1, 6):
            count, _ = incr_and_expire_nx(self.redis_client, self.test_key, ttl=10)
            self.assertEqual(count, i)

    def test_key_expires(self):
        incr_and_expire_nx(self.redis_client, self.test_key, ttl=1)
        time.sleep(1.5)
        self.assertIsNone(self.redis_client.get(self.test_key))


class RedisLockTest(TestCase):
    def setUp(self):
        self.redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        self.lock_key = f"test_redis_lock:{time.time()}"

    def tearDown(self):
        self.redis_client.delete(self.lock_key)
        self.redis_client.close()

    def test_redis_lock_is_reentrant_for_same_key(self):
        entered_inner = False
        with redis_lock(self.lock_key, timeout=10, blocking_timeout=5):
            with redis_lock(self.lock_key, timeout=10, blocking_timeout=5):
                entered_inner = True
        self.assertTrue(entered_inner)

    def test_zoom_and_calendar_lock_keys_are_stable(self):
        self.assertEqual(zoom_oauth_connection_token_lock_key(42), "lock:zoom-oauth-connection-token:42")
        self.assertEqual(calendar_sync_lock_key(7), "lock:calendar-sync:7")

    def test_zoom_oauth_connection_token_lock_acquires_redis_key(self):
        connection_id = int(time.time() * 1000) % 1000000
        lock_key = zoom_oauth_connection_token_lock_key(connection_id)
        with zoom_oauth_connection_token_lock(connection_id, timeout=10, blocking_timeout=5):
            # Lock key exists while held (redis-py lock uses the key directly)
            self.assertTrue(self.redis_client.exists(lock_key))
        self.redis_client.delete(lock_key)
