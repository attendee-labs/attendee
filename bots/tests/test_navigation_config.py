import json
import os
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, override_settings

from bots.web_bot_adapter import navigation_config, navigation_config_signing

CONFIG_FILENAME = "zoom_web.json"


class FakeLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value

    def lock(self, key, timeout=None):
        return FakeLock()

    def close(self):
        pass


def _response(text, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    if status_code >= 400:
        response.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        response.raise_for_status.return_value = None
    return response


class NavigationConfigSignatureTest(SimpleTestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_keys_patcher = mock.patch.object(navigation_config_signing, "NAVIGATION_CONFIG_PUBLIC_KEYS", [navigation_config_signing.public_key_b64(self.private_key)])
        public_keys_patcher.start()
        self.addCleanup(public_keys_patcher.stop)

        self.redis = FakeRedis()
        self.config = {"version": "99.0", "domain_allowlist": ["remote.example.com"]}
        self.signed_config = navigation_config_signing.sign_navigation_config(self.private_key, CONFIG_FILENAME, self.config)
        self.raw_signed_config = json.dumps(self.signed_config, indent=2)

    def _mock_remote(self, response):
        def fake_get(url, timeout):
            if url == navigation_config._remote_config_url(CONFIG_FILENAME):
                return response
            raise AssertionError(f"Unexpected URL {url}")

        return mock.patch.object(navigation_config.requests, "get", side_effect=fake_get)

    def _load(self):
        return navigation_config._load_remote_navigation_config_with_redis_client(self.redis, CONFIG_FILENAME)

    def _tampered_raw_config(self):
        return json.dumps({**self.signed_config, "domain_allowlist": ["evil.example.com"]})

    def _cache(self, raw_config):
        self.redis.set(navigation_config._redis_cache_key(CONFIG_FILENAME), raw_config)

    def test_sign_places_signature_last_and_preserves_other_attributes(self):
        self.assertEqual(list(self.signed_config), ["version", "domain_allowlist", "signature"])
        self.assertEqual({k: v for k, v in self.signed_config.items() if k != "signature"}, self.config)

    def test_signature_ignores_formatting_and_key_order(self):
        reformatted = json.loads(json.dumps(dict(reversed(list(self.signed_config.items()))), separators=(",", ":")))
        navigation_config_signing.verify_navigation_config_signature(CONFIG_FILENAME, reformatted)

    def test_fetch_with_valid_signature_is_used_and_cached(self):
        with self._mock_remote(_response(self.raw_signed_config)):
            config = self._load()

        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])
        self.assertEqual(self.redis.get(navigation_config._redis_cache_key(CONFIG_FILENAME)), self.raw_signed_config.encode("utf-8"))

    def test_fetch_with_tampered_config_is_rejected_and_not_cached(self):
        with self._mock_remote(_response(self._tampered_raw_config())):
            self.assertIsNone(self._load())
        self.assertEqual(self.redis.store, {})

    def test_fetch_with_added_attribute_is_rejected(self):
        with self._mock_remote(_response(json.dumps({**self.signed_config, "extra": True}))):
            self.assertIsNone(self._load())

    def test_fetch_signed_by_untrusted_key_is_rejected(self):
        untrusted = navigation_config_signing.sign_navigation_config(Ed25519PrivateKey.generate(), CONFIG_FILENAME, self.config)
        with self._mock_remote(_response(json.dumps(untrusted))):
            self.assertIsNone(self._load())

    def test_fetch_with_signature_for_different_config_is_rejected(self):
        other = navigation_config_signing.sign_navigation_config(self.private_key, "teams.json", self.config)
        with self._mock_remote(_response(json.dumps(other))):
            self.assertIsNone(self._load())

    def test_fetch_without_signature_is_rejected(self):
        with self._mock_remote(_response(json.dumps(self.config))):
            self.assertIsNone(self._load())

    def test_fetch_with_garbage_signature_is_rejected(self):
        with self._mock_remote(_response(json.dumps({**self.config, "signature": "not base64!!"}))):
            self.assertIsNone(self._load())

    def test_cached_config_with_valid_signature_is_used_without_fetching(self):
        self._cache(self.raw_signed_config)
        with mock.patch.object(navigation_config.requests, "get") as mock_get:
            config = self._load()

        mock_get.assert_not_called()
        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    def test_tampered_cached_config_is_rejected(self):
        self._cache(self._tampered_raw_config())
        self.assertIsNone(navigation_config._load_remote_navigation_config_from_redis_cache(self.redis, CONFIG_FILENAME))

    def test_unsigned_cached_config_is_rejected(self):
        self._cache(json.dumps(self.config))
        self.assertIsNone(navigation_config._load_remote_navigation_config_from_redis_cache(self.redis, CONFIG_FILENAME))

    def test_tampered_cache_falls_back_to_verified_fetch(self):
        self._cache(self._tampered_raw_config())
        with self._mock_remote(_response(self.raw_signed_config)):
            config = self._load()

        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=True)
    def test_load_navigation_config_falls_back_to_local_when_signature_invalid(self):
        navigation_config._load_navigation_config.cache_clear()
        self.addCleanup(navigation_config._load_navigation_config.cache_clear)

        with mock.patch.object(navigation_config, "_get_redis_client", return_value=self.redis), self._mock_remote(_response(self._tampered_raw_config())):
            config = navigation_config._load_navigation_config(CONFIG_FILENAME)

        self.assertEqual(config, navigation_config._load_local_navigation_config(CONFIG_FILENAME))


class WriteSignatureIntoConfigTextTest(SimpleTestCase):
    def test_inserts_signature_last_without_reformatting(self):
        text = '{\n  "version": "1.0",\n  "selectors": {"a": {"type": "css", "selector": "x"}}\n}\n'

        new_text = navigation_config_signing.write_signature_into_config_text(text, "c2ln")

        self.assertEqual(new_text, '{\n  "version": "1.0",\n  "selectors": {"a": {"type": "css", "selector": "x"}},\n  "signature": "c2ln"\n}\n')

    def test_replaces_existing_signature_in_place(self):
        text = '{\n  "version": "1.0",\n  "signature": "b2xk"\n}\n'

        self.assertEqual(navigation_config_signing.write_signature_into_config_text(text, "bmV3"), '{\n  "version": "1.0",\n  "signature": "bmV3"\n}\n')

    def test_refuses_when_signature_key_is_ambiguous(self):
        text = '{"version": "1.0", "nested": {"signature": "x"}, "signature": "y"}'

        with self.assertRaises(ValueError):
            navigation_config_signing.write_signature_into_config_text(text, "bmV3")


class CommittedNavigationConfigSignaturesTest(SimpleTestCase):
    def test_every_committed_config_has_a_valid_signature(self):
        config_filenames = [f for f in os.listdir(navigation_config.NAVIGATION_CONFIGS_DIR) if f.endswith(".json")]
        self.assertTrue(config_filenames)
        for config_filename in config_filenames:
            with self.subTest(config_filename=config_filename):
                with open(os.path.join(navigation_config.NAVIGATION_CONFIGS_DIR, config_filename)) as f:
                    config = json.load(f)
                navigation_config_signing.verify_navigation_config_signature(config_filename, config)
