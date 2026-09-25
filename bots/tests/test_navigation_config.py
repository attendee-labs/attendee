import json
import os
import re
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, override_settings
from selenium.webdriver.common.by import By

from bots.web_bot_adapter import navigation_config, navigation_config_signing

CONFIG_FILENAME = "zoom_web.json"
BOTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_DIR_TO_CONFIG_FILENAME = {
    "google_meet_bot_adapter": "google_meet.json",
    "teams_bot_adapter": "teams.json",
    "zoom_web_bot_adapter": "zoom_web.json",
}


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = False

    def acquire(self, blocking=True):
        return self.acquired

    def release(self):
        self.released = True


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.lock_acquired = True
        self.locks = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value

    def lock(self, key, timeout=None):
        fake_lock = FakeLock(acquired=self.lock_acquired)
        self.locks.append(fake_lock)
        return fake_lock

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


class RemoteNavigationConfigTestCase(SimpleTestCase):
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


class NavigationConfigSignatureTest(RemoteNavigationConfigTestCase):
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


class RemoteNavigationConfigLoadingTest(RemoteNavigationConfigTestCase):
    def _sign(self, config):
        return json.dumps(navigation_config_signing.sign_navigation_config(self.private_key, CONFIG_FILENAME, config))

    def test_http_error_returns_none_and_is_not_cached(self):
        with self._mock_remote(_response("Not Found", status_code=404)):
            self.assertIsNone(self._load())
        self.assertEqual(self.redis.store, {})

    def test_request_exception_returns_none(self):
        with mock.patch.object(navigation_config.requests, "get", side_effect=navigation_config.requests.exceptions.Timeout()):
            self.assertIsNone(self._load())

    def test_non_json_response_returns_none(self):
        with self._mock_remote(_response("<html>not json</html>")):
            self.assertIsNone(self._load())

    def test_non_object_json_response_returns_none(self):
        with self._mock_remote(_response(json.dumps([self.signed_config]))):
            self.assertIsNone(self._load())

    def test_signed_config_with_invalid_version_is_rejected_and_not_cached(self):
        with self._mock_remote(_response(self._sign({"version": "99", "domain_allowlist": []}))):
            self.assertIsNone(self._load())
        self.assertEqual(self.redis.store, {})

    def test_fetch_lock_is_released_after_fetch(self):
        with self._mock_remote(_response(self.raw_signed_config)):
            self._load()

        self.assertEqual(len(self.redis.locks), 1)
        self.assertTrue(self.redis.locks[0].released)

    def test_fetch_lock_is_released_after_failed_fetch(self):
        with self._mock_remote(_response("", status_code=500)):
            self._load()

        self.assertTrue(self.redis.locks[0].released)

    def test_when_lock_is_held_waits_for_other_bot_to_cache_config(self):
        self.redis.lock_acquired = False

        def other_bot_caches_config(_seconds):
            self._cache(self.raw_signed_config)

        with mock.patch.object(navigation_config.time, "sleep", side_effect=other_bot_caches_config), mock.patch.object(navigation_config.requests, "get") as mock_get:
            config = self._load()

        mock_get.assert_not_called()
        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    def test_when_lock_is_held_and_wait_times_out_returns_none_without_fetching(self):
        self.redis.lock_acquired = False

        with mock.patch.object(navigation_config, "REMOTE_NAVIGATION_CONFIG_LOCK_WAIT_SECONDS", 0), mock.patch.object(navigation_config.requests, "get") as mock_get:
            self.assertIsNone(self._load())

        mock_get.assert_not_called()

    def test_when_lock_cannot_be_created_fetches_anyway(self):
        with mock.patch.object(self.redis, "lock", side_effect=Exception("redis down")), self._mock_remote(_response(self.raw_signed_config)):
            config = self._load()

        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    def test_redis_read_failure_falls_back_to_fetch(self):
        with mock.patch.object(self.redis, "get", side_effect=Exception("redis down")), self._mock_remote(_response(self.raw_signed_config)):
            config = self._load()

        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    def test_redis_write_failure_still_returns_fetched_config(self):
        with mock.patch.object(self.redis, "set", side_effect=Exception("redis down")), self._mock_remote(_response(self.raw_signed_config)):
            config = self._load()

        self.assertEqual(config["domain_allowlist"], ["remote.example.com"])

    def test_cache_keys_are_scoped_per_config_filename(self):
        self.assertNotEqual(navigation_config._redis_cache_key("teams.json"), navigation_config._redis_cache_key("zoom_web.json"))
        self.assertNotEqual(navigation_config._redis_lock_key(CONFIG_FILENAME), navigation_config._redis_cache_key(CONFIG_FILENAME))


class ParseNavigationConfigVersionTest(SimpleTestCase):
    def test_parses_valid_versions(self):
        self.assertEqual(navigation_config.parse_navigation_config_version({"version": "1.0"}), (1, 0))
        self.assertEqual(navigation_config.parse_navigation_config_version({"version": "12.34"}), (12, 34))

    def test_rejects_invalid_versions(self):
        for version in ["1", "1.0.0", "1.x", "v1.0", "1.-1", "", ".", "1.", " 1.0", 1.0, 1, None]:
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    navigation_config.parse_navigation_config_version({"version": version})

    def test_rejects_missing_version(self):
        with self.assertRaises(ValueError):
            navigation_config.parse_navigation_config_version({})

    def test_versions_compare_numerically(self):
        self.assertGreater(navigation_config.parse_navigation_config_version({"version": "10.0"}), navigation_config.parse_navigation_config_version({"version": "9.9"}))
        self.assertGreater(navigation_config.parse_navigation_config_version({"version": "1.10"}), navigation_config.parse_navigation_config_version({"version": "1.9"}))


class LoadNavigationConfigTest(SimpleTestCase):
    def setUp(self):
        navigation_config._load_navigation_config.cache_clear()
        self.addCleanup(navigation_config._load_navigation_config.cache_clear)

    def _load(self, local_config, remote_config):
        with mock.patch.object(navigation_config, "_load_local_navigation_config", return_value=local_config) as mock_local, mock.patch.object(navigation_config, "_load_remote_navigation_config", return_value=remote_config) as mock_remote:
            config = navigation_config._load_navigation_config(CONFIG_FILENAME)
        return config, mock_local, mock_remote

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=False)
    def test_uses_local_config_without_fetching_when_remote_loading_disabled(self):
        local_config = {"version": "1.0"}

        config, _, mock_remote = self._load(local_config, {"version": "2.0"})

        self.assertIs(config, local_config)
        mock_remote.assert_not_called()

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=True)
    def test_uses_remote_config_when_version_is_newer_or_equal(self):
        for remote_version in ["1.1", "1.2", "1.10", "2.0"]:
            with self.subTest(remote_version=remote_version):
                navigation_config._load_navigation_config.cache_clear()
                remote_config = {"version": remote_version}

                config, _, _ = self._load({"version": "1.1"}, remote_config)

                self.assertIs(config, remote_config)

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=True)
    def test_uses_local_config_when_remote_version_is_older(self):
        for remote_version in ["1.0", "0.9", "0.99"]:
            with self.subTest(remote_version=remote_version):
                navigation_config._load_navigation_config.cache_clear()
                local_config = {"version": "1.1"}

                config, _, _ = self._load(local_config, {"version": remote_version})

                self.assertIs(config, local_config)

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=True)
    def test_uses_local_config_when_remote_unavailable(self):
        local_config = {"version": "1.0"}

        config, _, _ = self._load(local_config, None)

        self.assertIs(config, local_config)

    @override_settings(LOAD_NAVIGATION_CONFIG_REMOTELY=True)
    def test_result_is_memoized_per_process(self):
        with mock.patch.object(navigation_config, "_load_local_navigation_config", return_value={"version": "1.0"}) as mock_local, mock.patch.object(navigation_config, "_load_remote_navigation_config", return_value={"version": "2.0"}) as mock_remote:
            first = navigation_config._load_navigation_config(CONFIG_FILENAME)
            second = navigation_config._load_navigation_config(CONFIG_FILENAME)

        self.assertIs(first, second)
        mock_local.assert_called_once()
        mock_remote.assert_called_once()


class GetPlatformSelectorTest(SimpleTestCase):
    def _patch_config(self, config):
        patcher = mock.patch.object(navigation_config, "_load_navigation_config", return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_maps_selector_types_to_selenium_by(self):
        self._patch_config(
            {
                "selectors": {
                    "a": {"type": "css", "selector": '[data-tid="a"]'},
                    "b": {"type": "id", "selector": "b-id"},
                    "c": {"type": "xpath", "selector": "//div"},
                }
            }
        )

        self.assertEqual(navigation_config.get_platform_selector(CONFIG_FILENAME, "a"), (By.CSS_SELECTOR, '[data-tid="a"]'))
        self.assertEqual(navigation_config.get_platform_selector(CONFIG_FILENAME, "b"), (By.ID, "b-id"))
        self.assertEqual(navigation_config.get_platform_selector(CONFIG_FILENAME, "c"), (By.XPATH, "//div"))

    def test_joins_xpath_list_into_union(self):
        self._patch_config({"selectors": {"msg": {"type": "xpath", "selector": ["//a", "//b", "//c"]}}})

        self.assertEqual(navigation_config.get_platform_selector(CONFIG_FILENAME, "msg"), (By.XPATH, "//a | //b | //c"))

    def test_rejects_list_for_non_xpath_selector(self):
        self._patch_config({"selectors": {"btn": {"type": "css", "selector": ["#a", "#b"]}}})

        with self.assertRaises(ValueError):
            navigation_config.get_platform_selector(CONFIG_FILENAME, "btn")

    def test_raises_for_unknown_selector_name(self):
        self._patch_config({"selectors": {}})

        with self.assertRaises(KeyError):
            navigation_config.get_platform_selector(CONFIG_FILENAME, "missing")

    def test_raises_for_unknown_selector_type(self):
        self._patch_config({"selectors": {"btn": {"type": "link_text", "selector": "Join"}}})

        with self.assertRaises(KeyError):
            navigation_config.get_platform_selector(CONFIG_FILENAME, "btn")


class GetPlatformDomainAllowlistTest(SimpleTestCase):
    def test_returns_copy_of_allowlist(self):
        config = {"domain_allowlist": ["a.example.com", "b.example.com"]}
        with mock.patch.object(navigation_config, "_load_navigation_config", return_value=config):
            allowlist = navigation_config.get_platform_domain_allowlist(CONFIG_FILENAME)
            allowlist.append("evil.example.com")

            self.assertEqual(navigation_config.get_platform_domain_allowlist(CONFIG_FILENAME), ["a.example.com", "b.example.com"])

    def test_returns_empty_list_when_allowlist_missing(self):
        with mock.patch.object(navigation_config, "_load_navigation_config", return_value={"version": "1.0"}):
            self.assertEqual(navigation_config.get_platform_domain_allowlist(CONFIG_FILENAME), [])


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


class CommittedNavigationConfigContentsTest(SimpleTestCase):
    def _committed_configs(self):
        config_filenames = sorted(f for f in os.listdir(navigation_config.NAVIGATION_CONFIGS_DIR) if f.endswith(".json"))
        self.assertTrue(config_filenames)
        return {config_filename: navigation_config._load_local_navigation_config(config_filename) for config_filename in config_filenames}

    def test_every_adapter_config_is_committed(self):
        self.assertTrue(set(ADAPTER_DIR_TO_CONFIG_FILENAME.values()).issubset(self._committed_configs()))

    def test_domain_allowlists_are_well_formed(self):
        for config_filename, config in self._committed_configs().items():
            with self.subTest(config_filename=config_filename):
                allowlist = config["domain_allowlist"]
                self.assertIsInstance(allowlist, list)
                self.assertTrue(allowlist)
                for domain in allowlist:
                    self.assertIsInstance(domain, str)
                    self.assertEqual(domain, domain.strip().lower())
                    self.assertNotIn("/", domain)
                    self.assertNotIn(":", domain)
                self.assertEqual(len(allowlist), len(set(allowlist)), "domain_allowlist contains duplicates")

    def test_selectors_are_well_formed(self):
        for config_filename, config in self._committed_configs().items():
            for selector_name, selector_config in config.get("selectors", {}).items():
                with self.subTest(config_filename=config_filename, selector_name=selector_name):
                    self.assertEqual(set(selector_config), {"type", "selector"})
                    self.assertIn(selector_config["type"], navigation_config.SELECTOR_TYPE_TO_BY)
                    selector = selector_config["selector"]
                    if isinstance(selector, list):
                        self.assertEqual(selector_config["type"], "xpath")
                        self.assertTrue(selector)
                        self.assertTrue(all(isinstance(s, str) and s for s in selector))
                    else:
                        self.assertIsInstance(selector, str)
                        self.assertTrue(selector)

    def test_every_selector_referenced_by_an_adapter_exists_in_its_config(self):
        pattern = re.compile(r'navigation_config_selector\(\s*"([^"]+)"')
        committed_configs = self._committed_configs()
        total_references = 0
        for adapter_dir, config_filename in ADAPTER_DIR_TO_CONFIG_FILENAME.items():
            selectors = committed_configs[config_filename].get("selectors", {})
            adapter_path = os.path.join(BOTS_DIR, adapter_dir)
            for filename in sorted(os.listdir(adapter_path)):
                if not filename.endswith(".py"):
                    continue
                with open(os.path.join(adapter_path, filename)) as f:
                    referenced_names = pattern.findall(f.read())
                total_references += len(referenced_names)
                for selector_name in referenced_names:
                    with self.subTest(file=f"{adapter_dir}/{filename}", selector_name=selector_name):
                        self.assertIn(selector_name, selectors)
        self.assertGreater(total_references, 0)
