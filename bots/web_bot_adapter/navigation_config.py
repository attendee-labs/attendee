import json
import logging
import os
from functools import lru_cache

import jsonschema
import requests
from cryptography.exceptions import InvalidSignature
from django.conf import settings
from selenium.webdriver.common.by import By

from bots.web_bot_adapter.navigation_config_signing import verify_navigation_config_signature

logger = logging.getLogger(__name__)

# Every navigation config has a "version" of the form "x.y":
#   - Bump x (and reset y to 0) when attributes are added.
#   - Bump y when existing attributes are changed.
#   - Attributes must never be deleted, since older deployed code may still read them.
# A remote config is only used if its version is >= the local config's version, so that
# code never runs against a config that lacks attributes it expects or has staler values.
#
# A remote config is also only used if it has a valid signature (see navigation_config_signing.py),
# so that whoever controls the remote URL can't push configs to bots without the signing key.
#
# Every config, local or remote, must also match NAVIGATION_CONFIG_SCHEMA, so that a signed but
# malformed config (e.g. an integer where a string is expected) is rejected instead of breaking bots.

NAVIGATION_CONFIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navigation_configs")
REMOTE_NAVIGATION_CONFIGS_BASE_URL = "https://navigation-configs.attendee.dev/configs"
REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS = 3

SELECTOR_TYPE_TO_BY = {
    "css": By.CSS_SELECTOR,
    "id": By.ID,
    "xpath": By.XPATH,
}

_NON_EMPTY_STRING_SCHEMA = {"type": "string", "minLength": 1}

# Unknown attributes must be allowed, since a newer remote config may add attributes this code doesn't know about.
NAVIGATION_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "string", "pattern": r"^[0-9]+\.[0-9]+$"},
        # An empty allowlist disables enforcement entirely, so it must never be empty.
        "domain_allowlist": {"type": "array", "minItems": 1, "items": _NON_EMPTY_STRING_SCHEMA},
        "selectors": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "type": {"enum": list(SELECTOR_TYPE_TO_BY)},
                    "selector": {
                        "oneOf": [
                            _NON_EMPTY_STRING_SCHEMA,
                            {"type": "array", "minItems": 1, "items": _NON_EMPTY_STRING_SCHEMA},
                        ]
                    },
                },
                "required": ["type", "selector"],
                "if": {"properties": {"selector": {"type": "array"}}},
                "then": {"properties": {"type": {"const": "xpath"}}},
            },
        },
        "signature": {"type": "string"},
    },
    "required": ["version", "domain_allowlist"],
}


def validate_navigation_config(config):
    """Raises ValueError unless config matches NAVIGATION_CONFIG_SCHEMA and has a parseable version."""
    try:
        jsonschema.validate(instance=config, schema=NAVIGATION_CONFIG_SCHEMA)
    except jsonschema.exceptions.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        raise ValueError(f"Navigation config does not match schema at {path}: {e.message}") from e
    parse_navigation_config_version(config)


def _local_config_path(config_filename):
    return os.path.join(NAVIGATION_CONFIGS_DIR, config_filename)


def _remote_config_url(config_filename):
    return f"{REMOTE_NAVIGATION_CONFIGS_BASE_URL}/{config_filename}"


def parse_navigation_config_version(config):
    """Returns the config's "x.y" version as a (major, minor) tuple of ints."""
    version = config.get("version")
    if not isinstance(version, str):
        raise ValueError(f"Navigation config version must be a string of the form 'x.y', got {version!r}")
    parts = version.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Navigation config version must be of the form 'x.y', got {version!r}")
    return (int(parts[0]), int(parts[1]))


def _load_local_navigation_config(config_filename):
    path = _local_config_path(config_filename)
    with open(path) as f:
        config = json.load(f)
    validate_navigation_config(config)
    logger.info("Loaded navigation config from %s", path)
    return config


def _parse_verified_remote_navigation_config(config_filename, raw_config):
    config = json.loads(raw_config)
    if not isinstance(config, dict):
        raise ValueError("Remote navigation config is not a JSON object")
    verify_navigation_config_signature(config_filename, config)
    validate_navigation_config(config)
    return config


def _load_remote_navigation_config(config_filename):
    url = _remote_config_url(config_filename)
    try:
        response = requests.get(url, timeout=REMOTE_NAVIGATION_CONFIG_TIMEOUT_SECONDS)
        response.raise_for_status()
        config = _parse_verified_remote_navigation_config(config_filename, response.text)
    except InvalidSignature as e:
        logger.error("Rejecting navigation config from %s because its signature is invalid, falling back to local config: %s", url, e)
        return None
    except Exception as e:
        logger.warning("Failed to load navigation config from %s, falling back to local config: %s", url, e)
        return None
    logger.info("Loaded navigation config from %s", url)
    return config


@lru_cache(maxsize=None)
def _load_navigation_config(config_filename):
    local_config = _load_local_navigation_config(config_filename)
    if not settings.LOAD_NAVIGATION_CONFIG_REMOTELY:
        return local_config

    remote_config = _load_remote_navigation_config(config_filename)
    if remote_config is None:
        return local_config

    local_version = parse_navigation_config_version(local_config)
    remote_version = parse_navigation_config_version(remote_config)
    if remote_version < local_version:
        logger.warning(
            "Remote navigation config %s has version %s.%s which is older than local version %s.%s, using local config",
            config_filename,
            *remote_version,
            *local_version,
        )
        return local_config

    logger.info("Using remote navigation config %s (version %s.%s)", config_filename, *remote_version)
    return remote_config


def get_platform_domain_allowlist(config_filename):
    return list(_load_navigation_config(config_filename).get("domain_allowlist", []))


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
