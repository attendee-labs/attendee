"""
Webex token utilities for OAuth authentication.

This module handles refreshing Webex access tokens using refresh tokens.
Works with Credentials (credential_type=WEBEX) via _WebexCredentialsWrapper.
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


def refresh_webex_access_token(webex_bot_connection):
    """
    Refresh the Webex access token using the refresh token.

    Args:
        webex_bot_connection: Connection credentials object (e.g. _WebexCredentialsWrapper) with OAuth credentials

    Returns:
        dict: New credentials with access_token, refresh_token, and expires_at

    Raises:
        Exception: If unable to refresh token
    """
    try:
        response = requests.post(
            "https://webexapis.com/v1/access_token",
            data={
                "grant_type": "refresh_token",
                "client_id": webex_bot_connection.client_id,
                "client_secret": webex_bot_connection.client_secret,
                "refresh_token": webex_bot_connection.refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10
        )
        response.raise_for_status()

        token_data = response.json()

        # Calculate expiration timestamp
        expires_in = token_data.get("expires_in", 3600)
        expires_at = timezone.now().timestamp() + expires_in

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", webex_bot_connection.refresh_token),
            "expires_at": expires_at,
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to refresh Webex access token: {e}")
        raise Exception(f"Failed to refresh Webex token: {str(e)}")


def refresh_webex_token_if_needed(webex_bot_connection):
    """
    Refresh the Webex access token if it's expired or about to expire.

    Args:
        webex_bot_connection: Connection credentials object (e.g. _WebexCredentialsWrapper).

    Returns:
        bool: True if token was refreshed, False if no refresh was needed
    """
    # State values used when updating connection state (wrapper persists them if needed)
    CONNECTED = 1
    DISCONNECTED = 0

    # Check if token is expired or will expire in the next 5 minutes
    if webex_bot_connection.expires_at:
        time_until_expiry = webex_bot_connection.expires_at - timezone.now().timestamp()
        if time_until_expiry > 300:  # More than 5 minutes
            return False

    logger.info(f"Refreshing Webex access token for {webex_bot_connection.object_id}")

    try:
        # Refresh the token
        new_credentials = refresh_webex_access_token(webex_bot_connection)

        # Get existing credentials and update them
        existing_creds = webex_bot_connection.get_credentials() or {}
        existing_creds.update(new_credentials)

        # Save updated credentials
        webex_bot_connection.set_credentials(existing_creds)
        webex_bot_connection.state = CONNECTED
        webex_bot_connection.connection_failure_data = None
        webex_bot_connection.save()

        logger.info(f"Successfully refreshed Webex token for {webex_bot_connection.object_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to refresh Webex token for {webex_bot_connection.object_id}: {e}")
        webex_bot_connection.state = DISCONNECTED
        webex_bot_connection.connection_failure_data = {
            "error": str(e),
            "error_type": type(e).__name__,
        }
        webex_bot_connection.save()
        raise


def get_webex_access_token_for_bot(webex_bot_connection):
    """
    Get a valid access token for the bot, refreshing if necessary.

    Args:
        webex_bot_connection: Connection credentials object (e.g. _WebexCredentialsWrapper) with OAuth credentials

    Returns:
        str: Valid access token
    """
    refresh_webex_token_if_needed(webex_bot_connection)
    return webex_bot_connection.access_token
