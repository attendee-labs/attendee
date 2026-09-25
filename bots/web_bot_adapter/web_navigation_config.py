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

WEB_NAVIGATION_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_navigation_config.json")
REMOTE_WEB_NAVIGATION_CONFIG_URL = "https://raw.githubusercontent.com/attendee-labs/attendee/main/bots/web_bot_adapter/web_navigation_config.json"
REMOTE_WEB_NAVIGATION_CONFIG_TIMEOUT_SECONDS = 3
REMOTE_WEB_NAVIGATION_CONFIG_REDIS_CACHE_KEY = "web_navigation_config:remote"
REMOTE_WEB_NAVIGATION_CONFIG_REDIS_CACHE_TTL_SECONDS = 5 * 60
REMOTE_WEB_NAVIGATION_CONFIG_REDIS_LOCK_KEY = "web_navigation_config:remote:fetch_lock"
# Must outlive the fetch timeout so the lock can't expire while its holder is still fetching.
REMOTE_WEB_NAVIGATION_CONFIG_REDIS_LOCK_TTL_SECONDS = REMOTE_WEB_NAVIGATION_CONFIG_TIMEOUT_SECONDS + 2
REMOTE_WEB_NAVIGATION_CONFIG_LOCK_WAIT_SECONDS = REMOTE_WEB_NAVIGATION_CONFIG_TIMEOUT_SECONDS + 1
REMOTE_WEB_NAVIGATION_CONFIG_LOCK_POLL_INTERVAL_SECONDS = 0.2


def _load_local_web_navigation_config():
    with open(WEB_NAVIGATION_CONFIG_PATH) as f:
        config = json.load(f)
    logger.info("Loaded web navigation config from %s", WEB_NAVIGATION_CONFIG_PATH)
    return config


def _parse_remote_web_navigation_config(raw_config):
    config = json.loads(raw_config)
    if not isinstance(config, dict) or not isinstance(config.get("platforms"), dict):
        raise ValueError("Remote web navigation config is missing a 'platforms' object")
    return config


def _get_redis_client():
    return redis.from_url(settings.REDIS_URL_WITH_PARAMS, socket_timeout=2, socket_connect_timeout=2)


def _load_remote_web_navigation_config_from_redis_cache(redis_client):
    try:
        raw_config = redis_client.get(REMOTE_WEB_NAVIGATION_CONFIG_REDIS_CACHE_KEY)
        if raw_config is None:
            return None
        config = _parse_remote_web_navigation_config(raw_config)
    except Exception as e:
        logger.warning("Failed to load web navigation config from redis cache: %s", e)
        return None
    logger.info("Loaded web navigation config from redis cache (source: %s)", REMOTE_WEB_NAVIGATION_CONFIG_URL)
    return config


def _store_remote_web_navigation_config_in_redis_cache(redis_client, raw_config):
    try:
        redis_client.set(REMOTE_WEB_NAVIGATION_CONFIG_REDIS_CACHE_KEY, raw_config, ex=REMOTE_WEB_NAVIGATION_CONFIG_REDIS_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("Failed to store web navigation config in redis cache: %s", e)


def _wait_for_remote_web_navigation_config_in_redis_cache(redis_client):
    deadline = time.time() + REMOTE_WEB_NAVIGATION_CONFIG_LOCK_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(REMOTE_WEB_NAVIGATION_CONFIG_LOCK_POLL_INTERVAL_SECONDS)
        cached_config = _load_remote_web_navigation_config_from_redis_cache(redis_client)
        if cached_config is not None:
            return cached_config
    logger.warning("Timed out waiting for another bot to cache the web navigation config, falling back to local config")
    return None


def _load_remote_web_navigation_config():
    redis_client = _get_redis_client()
    try:
        return _load_remote_web_navigation_config_with_redis_client(redis_client)
    finally:
        redis_client.close()


def _load_remote_web_navigation_config_with_redis_client(redis_client):
    cached_config = _load_remote_web_navigation_config_from_redis_cache(redis_client)
    if cached_config is not None:
        return cached_config

    fetch_lock = None
    try:
        fetch_lock = redis_client.lock(REMOTE_WEB_NAVIGATION_CONFIG_REDIS_LOCK_KEY, timeout=REMOTE_WEB_NAVIGATION_CONFIG_REDIS_LOCK_TTL_SECONDS)
        if not fetch_lock.acquire(blocking=False):
            return _wait_for_remote_web_navigation_config_in_redis_cache(redis_client)
    except Exception as e:
        logger.warning("Failed to acquire web navigation config fetch lock, fetching without it: %s", e)
        fetch_lock = None

    try:
        return _fetch_remote_web_navigation_config(redis_client)
    finally:
        if fetch_lock is not None:
            try:
                fetch_lock.release()
            except Exception as e:
                logger.warning("Failed to release web navigation config fetch lock: %s", e)


def _fetch_remote_web_navigation_config(redis_client):
    try:
        response = requests.get(REMOTE_WEB_NAVIGATION_CONFIG_URL, timeout=REMOTE_WEB_NAVIGATION_CONFIG_TIMEOUT_SECONDS)
        response.raise_for_status()
        raw_config = response.text
        config = _parse_remote_web_navigation_config(raw_config)
    except Exception as e:
        logger.warning("Failed to load web navigation config from %s, falling back to local config: %s", REMOTE_WEB_NAVIGATION_CONFIG_URL, e)
        return None
    logger.info("Loaded web navigation config from %s", REMOTE_WEB_NAVIGATION_CONFIG_URL)
    _store_remote_web_navigation_config_in_redis_cache(redis_client, raw_config)
    return config


@lru_cache(maxsize=1)
def _load_web_navigation_config():
    if settings.LOAD_WEB_NAVIGATION_CONFIG_REMOTELY:
        remote_config = _load_remote_web_navigation_config()
        if remote_config is not None:
            return remote_config
    return _load_local_web_navigation_config()


def _get_platform_config(platform):
    return _load_web_navigation_config().get("platforms", {}).get(platform, {})


def get_platform_domain_allowlist(platform):
    return list(_get_platform_config(platform).get("domain_allowlist", []))


SELECTOR_TYPE_TO_BY = {
    "css": By.CSS_SELECTOR,
    "id": By.ID,
    "xpath": By.XPATH,
}


def get_platform_selector(platform, selector_name):
    """Returns a (By, selector) tuple usable with selenium's find_element and expected_conditions."""
    selector_config = _get_platform_config(platform)["selectors"][selector_name]
    selector_type = selector_config["type"]
    selector = selector_config["selector"]
    if isinstance(selector, list):
        if selector_type != "xpath":
            raise ValueError(f"Selector '{selector_name}' for platform '{platform}' is a list, which is only supported for xpath selectors")
        selector = " | ".join(selector)
    return (SELECTOR_TYPE_TO_BY[selector_type], selector)
