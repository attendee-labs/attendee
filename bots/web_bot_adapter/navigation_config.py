import json
import logging
import os
import time
from functools import lru_cache

import redis
import requests
from django.conf import settings
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

NAVIGATION_CONFIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navigation_configs")
REMOTE_NAVIGATION_CONFIGS_BASE_URL = "https://raw.githubusercontent.com/attendee-labs/attendee/main/bots/web_bot_adapter/navigation_configs"
REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS = 3
REMOTE_NAVIGATION_CONFIG_REDIS_CACHE_KEY_PREFIX = "navigation_config:remote"
REMOTE_NAVIGATION_CONFIG_REDIS_CACHE_TTL_SECONDS = 5 * 60
# Must outlive the fetch timeout so the lock can't expire while its holder is still fetching.
REMOTE_NAVIGATION_CONFIG_REDIS_LOCK_TTL_SECONDS = REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS + 2
REMOTE_NAVIGATION_CONFIG_LOCK_WAIT_SECONDS = REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS + 1
REMOTE_NAVIGATION_CONFIG_LOCK_POLL_INTERVAL_SECONDS = 0.2


def _local_config_path(config_filename):
    return os.path.join(NAVIGATION_CONFIGS_DIR, config_filename)


def _remote_config_url(config_filename):
    return f"{REMOTE_NAVIGATION_CONFIGS_BASE_URL}/{config_filename}"


def _redis_cache_key(config_filename):
    return f"{REMOTE_NAVIGATION_CONFIG_REDIS_CACHE_KEY_PREFIX}:{config_filename}"


def _redis_lock_key(config_filename):
    return f"{_redis_cache_key(config_filename)}:fetch_lock"


def _load_local_navigation_config(config_filename):
    path = _local_config_path(config_filename)
    with open(path) as f:
        config = json.load(f)
    logger.info("Loaded navigation config from %s", path)
    return config


def _parse_remote_navigation_config(raw_config):
    config = json.loads(raw_config)
    if not isinstance(config, dict):
        raise ValueError("Remote navigation config is not a JSON object")
    return config


def _get_redis_client():
    return redis.from_url(settings.REDIS_URL_WITH_PARAMS, socket_timeout=2, socket_connect_timeout=2)


def _load_remote_navigation_config_from_redis_cache(redis_client, config_filename):
    try:
        raw_config = redis_client.get(_redis_cache_key(config_filename))
        if raw_config is None:
            return None
        config = _parse_remote_navigation_config(raw_config)
    except Exception as e:
        logger.warning("Failed to load navigation config %s from redis cache: %s", config_filename, e)
        return None
    logger.info("Loaded navigation config from redis cache (source: %s)", _remote_config_url(config_filename))
    return config


def _store_remote_navigation_config_in_redis_cache(redis_client, config_filename, raw_config):
    try:
        redis_client.set(_redis_cache_key(config_filename), raw_config, ex=REMOTE_NAVIGATION_CONFIG_REDIS_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("Failed to store navigation config %s in redis cache: %s", config_filename, e)


def _wait_for_remote_navigation_config_in_redis_cache(redis_client, config_filename):
    deadline = time.time() + REMOTE_NAVIGATION_CONFIG_LOCK_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(REMOTE_NAVIGATION_CONFIG_LOCK_POLL_INTERVAL_SECONDS)
        cached_config = _load_remote_navigation_config_from_redis_cache(redis_client, config_filename)
        if cached_config is not None:
            return cached_config
    logger.warning("Timed out waiting for another bot to cache the navigation config %s, falling back to local config", config_filename)
    return None


def _load_remote_navigation_config(config_filename):
    redis_client = _get_redis_client()
    try:
        return _load_remote_navigation_config_with_redis_client(redis_client, config_filename)
    finally:
        redis_client.close()


def _load_remote_navigation_config_with_redis_client(redis_client, config_filename):
    cached_config = _load_remote_navigation_config_from_redis_cache(redis_client, config_filename)
    if cached_config is not None:
        return cached_config

    fetch_lock = None
    try:
        fetch_lock = redis_client.lock(_redis_lock_key(config_filename), timeout=REMOTE_NAVIGATION_CONFIG_REDIS_LOCK_TTL_SECONDS)
        if not fetch_lock.acquire(blocking=False):
            return _wait_for_remote_navigation_config_in_redis_cache(redis_client, config_filename)
    except Exception as e:
        logger.warning("Failed to acquire navigation config fetch lock for %s, fetching without it: %s", config_filename, e)
        fetch_lock = None

    try:
        return _fetch_remote_navigation_config(redis_client, config_filename)
    finally:
        if fetch_lock is not None:
            try:
                fetch_lock.release()
            except Exception as e:
                logger.warning("Failed to release navigation config fetch lock for %s: %s", config_filename, e)


def _fetch_remote_navigation_config(redis_client, config_filename):
    url = _remote_config_url(config_filename)
    try:
        response = requests.get(url, timeout=REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS)
        response.raise_for_status()
        raw_config = response.text
        config = _parse_remote_navigation_config(raw_config)
    except Exception as e:
        logger.warning("Failed to load navigation config from %s, falling back to local config: %s", url, e)
        return None
    logger.info("Loaded navigation config from %s", url)
    _store_remote_navigation_config_in_redis_cache(redis_client, config_filename, raw_config)
    return config


@lru_cache(maxsize=None)
def _load_navigation_config(config_filename):
    if settings.LOAD_NAVIGATION_CONFIG_REMOTELY:
        remote_config = _load_remote_navigation_config(config_filename)
        if remote_config is not None:
            return remote_config
    return _load_local_navigation_config(config_filename)


def get_platform_domain_allowlist(config_filename):
    return list(_load_navigation_config(config_filename).get("domain_allowlist", []))


SELECTOR_TYPE_TO_BY = {
    "css": By.CSS_SELECTOR,
    "id": By.ID,
    "xpath": By.XPATH,
}


def get_platform_selector(config_filename, selector_name):
    """Returns a (By, selector) tuple usable with selenium's find_element and expected_conditions."""
    selector_config = _load_navigation_config(config_filename)["selectors"][selector_name]
    selector_type = selector_config["type"]
    selector = selector_config["selector"]
    if isinstance(selector, list):
        if selector_type != "xpath":
            raise ValueError(f"Selector '{selector_name}' in {config_filename} is a list, which is only supported for xpath selectors")
        selector = " | ".join(selector)
    return (SELECTOR_TYPE_TO_BY[selector_type], selector)
