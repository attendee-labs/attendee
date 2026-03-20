import time

import redis
from django.conf import settings
from django.test import TestCase

from bots.redis_utils import incr_and_expire_nx


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
