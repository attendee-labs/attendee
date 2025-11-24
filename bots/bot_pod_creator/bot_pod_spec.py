import json
import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)


class InvalidBotPodSpecException(Exception):
    pass


class BotPodSpecType(str, Enum):
    DEFAULT = "DEFAULT"
    SCHEDULED = "SCHEDULED"


# For now, we fetch based on environment variable.
def fetch_bot_pod_spec(bot_pod_spec_type: BotPodSpecType, raise_error_on_invalid_bot_pod_spec: bool = False) -> list[dict]:
    bot_pod_spec_str = os.getenv(f"BOT_POD_SPEC_{bot_pod_spec_type}")

    if bot_pod_spec_str is None:
        return None

    try:
        bot_pod_spec_json = json.loads(bot_pod_spec_str)
    except json.JSONDecodeError as e:
        logger.error("bot pod spec is not valid JSON: %s", e)
        if raise_error_on_invalid_bot_pod_spec:
            raise InvalidBotPodSpecException("bot pod spec is not valid JSON")
        return None

    if not isinstance(bot_pod_spec_json, list):
        logger.error(
            "bot pod spec must be a JSON array of JSON6902 operations; got %r",
            type(bot_pod_spec_json),
        )
        if raise_error_on_invalid_bot_pod_spec:
            raise InvalidBotPodSpecException("bot pod spec must be a JSON array of JSON6902 operations")
        return None

    return bot_pod_spec_json
