import logging
from typing import Dict, Optional

import requests

from bots.models import WebhookSecret
from bots.webhook_utils import sign_payload

logger = logging.getLogger(__name__)


class CallbackError(Exception):
    """Exception raised when a callback fails"""

    pass


class CallbackTimeoutError(CallbackError):
    """Exception raised when a callback times out"""

    pass


class CallbackHTTPError(CallbackError):
    """Exception raised when a callback returns a non-success HTTP status"""

    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def make_callback_request(url: str, bot, callback_type: str, additional_data: Optional[Dict] = None) -> Dict:
    """
    Make a callback request to an external server.

    Args:
        url: The callback URL to send the request to
        bot: The Bot instance making the callback
        callback_type: Type of callback (e.g., 'zoom_tokens')
        additional_data: Optional additional data to include in the request

    Returns:
        Dict: The response data from the callback server

    Raises:
        CallbackError: If the callback fails
        CallbackTimeoutError: If the callback times out
        CallbackHTTPError: If the callback returns a non-success status
    """
    # Prepare the callback payload similar to webhook structure
    callback_data = {
        "bot_id": bot.object_id,
        "bot_metadata": bot.metadata,
        "callback_type": callback_type,
        "meeting_url": bot.meeting_url,
    }

    # Add any additional data
    if additional_data:
        callback_data.update(additional_data)

    # Get or create webhook secret for signing
    WebhookSecret.objects.get_or_create(project=bot.project)
    active_secret = bot.project.webhook_secrets.filter().order_by("-created_at").first()

    # Sign the payload
    signature = sign_payload(callback_data, active_secret.get_secret())

    try:
        logger.info(f"Making {callback_type} callback request for bot {bot.object_id} to {url}")

        response = requests.post(
            url,
            json=callback_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Attendee-Callback/1.0",
                "X-Webhook-Signature": signature,
            },
            timeout=30,  # 30-second timeout
        )

        # Log the response for debugging
        logger.info(f"Callback response status: {response.status_code}")

        # Check if the request was successful (2xx status code)
        if not (200 <= response.status_code < 300):
            error_msg = f"Callback request failed with status {response.status_code}"
            response_body = response.text[:1000]  # Limit response body logging
            logger.error(f"{error_msg}. Response: {response_body}")
            raise CallbackHTTPError(error_msg, status_code=response.status_code, response_body=response_body)

        # Parse the JSON response
        try:
            response_data = response.json()
        except ValueError as e:
            error_msg = f"Invalid JSON response from callback: {str(e)}"
            logger.error(error_msg)
            raise CallbackError(error_msg)

        logger.info(f"Callback request successful for bot {bot.object_id}")
        return response_data

    except requests.RequestException as e:
        error_msg = f"Network error during callback request: {str(e)}"
        logger.error(error_msg)
        if isinstance(e, requests.Timeout):
            raise CallbackTimeoutError(error_msg)
        else:
            raise CallbackError(error_msg)


def get_zoom_tokens(bot) -> Dict[str, str]:
    """
    Retrieve Zoom authentication tokens via callback.

    Args:
        bot: The Bot instance that needs zoom tokens

    Returns:
        Dict containing the zoom tokens:
        {
            "zak_token": str or None,
            "join_token": str or None,
            "app_privilege_token": str or None
            "onbehalf_token": str or None
        }
    """
    callback_settings = bot.settings.get("callback_settings", {})
    zoom_tokens_url = callback_settings.get("zoom_tokens_url")

    # Initialize return dict with None values
    tokens = {
        "zak_token": None,
        "join_token": None,
        "app_privilege_token": None,
        "onbehalf_token": None,
    }

    if not zoom_tokens_url:
        return tokens

    # Make the callback request
    try:
        response_data = make_callback_request(url=zoom_tokens_url, bot=bot, callback_type="zoom_tokens")
    except Exception:
        return tokens

    # Verify response_data is a dict
    if not isinstance(response_data, dict):
        logger.error(f"Invalid response data from callback: {response_data}")
        return tokens

    # Extract tokens, setting to None if missing or invalid
    required_fields = ["zak_token", "join_token", "app_privilege_token", "onbehalf_token"]
    for field in required_fields:
        token_value = response_data.get(field)
        if isinstance(token_value, str) and token_value.strip():
            tokens[field] = token_value

    logger.info(f"Retrieved zoom tokens for bot {bot.object_id}")
    return tokens
