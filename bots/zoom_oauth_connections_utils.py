import hashlib
import hmac
import logging

import requests
from django.db import transaction
from django.utils import timezone

from bots.meeting_url_utils import parse_zoom_join_url
from bots.models import Bot, WebhookTriggerTypes, ZoomMeetingToZoomOAuthConnectionMapping, ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.webhook_payloads import zoom_oauth_connection_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)


class ZoomAPIError(Exception):
    """Custom exception for Zoom API errors."""

    pass


class ZoomAPIAuthenticationError(ZoomAPIError):
    """Custom exception for Zoom API errors."""

    pass


def client_id_and_secret_is_valid(client_id: str, client_secret: str) -> bool:
    """
    Validate Zoom OAuth client credentials without requiring a user via client_credentials grant type

    Returns:
        True if the credentials are valid, False otherwise
    """
    try:
        response = requests.post("https://zoom.us/oauth/token", auth=(client_id, client_secret), data={"grant_type": "client_credentials"}, timeout=30)

        # If we get a 200  the credentials are valid
        if response.status_code == 200:
            return True

        return False
    except Exception as e:
        logger.exception(f"Error validating Zoom OAuth client_id and client_secret: {e}")
        return False


def _verify_zoom_webhook_signature(body: str, timestamp: str, signature: str, secret: str):
    """Verify the Zoom webhook signature."""
    hmac_hash = hmac.new(secret.encode("utf-8"), f"v0:{timestamp}:{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    expected_signature = f"v0={hmac_hash}"
    return expected_signature == signature


def compute_zoom_webhook_validation_response(plain_token: str, secret_token: str) -> dict:
    """
    Compute the response for a Zoom webhook validation request.

    Zoom sends a challenge-response validation request when setting up a webhook endpoint.
    This function creates the required HMAC SHA-256 hash of the plainToken using the
    webhook secret token.

    Args:
        plain_token: The plainToken value from the webhook request payload
        secret_token: The webhook secret token configured in Zoom

    Returns:
        dict: A dictionary containing 'plainToken' and 'encryptedToken' keys
        Example: {
            "plainToken": "qgg8vlvZRS6UYooatFL8Aw",
            "encryptedToken": "23a89b634c017e5364a1c8d9c8ea909b60dd5599e2bb04bb1558d9c3a121faa5"
        }
    """
    # Create HMAC SHA-256 hash with secret_token as salt and plain_token as the string to hash
    encrypted_token = hmac.new(secret_token.encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256).hexdigest()

    return {"plainToken": plain_token, "encryptedToken": encrypted_token}


def _raise_if_error_is_authentication_error(e: requests.RequestException):
    error_code = e.response.json().get("error")
    if error_code == "invalid_grant" or error_code == "invalid_client":
        raise ZoomAPIAuthenticationError(f"Zoom Authentication error: {e.response.json()}")

    return


def _get_access_token(zoom_oauth_connection) -> str:
    """
    Exchange the stored refresh token for a new access token.
    Zoom returns a new refresh_token on each successful refresh.
    Persist it so we don't lose the chain.
    """
    credentials = zoom_oauth_connection.get_credentials()
    if not credentials:
        raise ZoomAPIAuthenticationError("No credentials found for zoom oauth connection")

    refresh_token = credentials.get("refresh_token")
    client_id = zoom_oauth_connection.zoom_oauth_app.client_id
    client_secret = zoom_oauth_connection.zoom_oauth_app.client_secret
    if not refresh_token or not client_id or not client_secret:
        raise ZoomAPIAuthenticationError("Missing refresh_token or client_secret")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        response = requests.post("https://zoom.us/oauth/token", data=data, timeout=30)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ZoomAPIError(f"No access_token in refresh response. Response body: {response.json()}")

        # IMPORTANT: Zoom rotates refresh tokens. Save the new one if provided.
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            credentials["refresh_token"] = new_refresh
            zoom_oauth_connection.set_credentials(credentials)
            logger.info("Stored rotated Zoom refresh_token for zoom oauth connection %s", zoom_oauth_connection.object_id)

        return access_token

    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        raise ZoomAPIError(f"Failed to refresh Zoom access token. Response body: {e.response.json()}")


def _make_zoom_api_request(url: str, access_token: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}

    req = requests.Request("GET", url, headers=headers, params=params).prepare()
    try:
        # Send the request
        with requests.Session() as s:
            resp = s.send(req, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        logger.exception(f"Failed to make Zoom API request. Response body: {e.response.json()}")
        raise e


def _get_zoom_personal_meeting_id(access_token: str) -> str:
    base_url = "https://api.zoom.us/v2/users/me"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("pmi")


def _get_local_recording_token(meeting_id: str, access_token: str) -> str:
    base_url = f"https://api.zoom.us/v2/meetings/{meeting_id}/jointoken/local_recording?bypass_waiting_room=true"

    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("token")


def _get_onbehalf_token(meeting_id: str, access_token: str) -> str:
    base_url = f"https://api.zoom.us/v2/users/me/token?type=onbehalf&meeting_id={meeting_id}"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("token")


def _get_zak_token(access_token: str) -> str | None:
    base_url = "https://api.zoom.us/v2/users/me/token?type=zak"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("token")


def _get_zoom_meetings(access_token: str) -> list[dict]:
    base_url = "https://api.zoom.us/v2/users/me/meetings"
    base_params = {
        "page_size": 300,
    }

    all_meetings = []
    next_page_token = None

    while True:
        params = dict(base_params)  # copy base params
        if next_page_token:
            params["next_page_token"] = next_page_token

        logger.info(f"Fetching Zoom meetings: {base_url} with params: {params}")
        response_data = _make_zoom_api_request(base_url, access_token, params)

        meetings = response_data.get("meetings", [])
        all_meetings.extend(meetings)

        next_page_token = response_data.get("next_page_token")
        if not next_page_token:
            break

    return all_meetings


def _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(zoom_meeting_ids: list[int], zoom_oauth_connection: ZoomOAuthConnection):
    zoom_oauth_app = zoom_oauth_connection.zoom_oauth_app
    num_updated = 0
    num_created = 0

    # Iterate over the zoom meetings and upsert the zoom meeting to zoom oauth connection mapping
    for zoom_meeting_id in zoom_meeting_ids:
        if not zoom_meeting_id:
            logger.warning(f"Zoom meeting id is None for zoom oauth connection {zoom_oauth_connection.id}")
            continue

        zoom_meeting_to_zoom_oauth_connection_mapping, created = ZoomMeetingToZoomOAuthConnectionMapping.objects.update_or_create(
            zoom_oauth_app=zoom_oauth_app,
            meeting_id=zoom_meeting_id,
            defaults={"zoom_oauth_connection": zoom_oauth_connection},
        )
        # If one already exists, but it has a different zoom_oauth_connection_id, update it
        if not created and zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection_id != zoom_oauth_connection.id:
            zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection = zoom_oauth_connection
            zoom_meeting_to_zoom_oauth_connection_mapping.save()
            num_updated += 1
        if created:
            num_created += 1

    logger.info(f"Upserted {num_updated} zoom meeting ids to zoom oauth connection mappings and created {num_created} new ones for zoom oauth connection {zoom_oauth_connection.id}")


def _handle_zoom_api_authentication_error(zoom_oauth_connection: ZoomOAuthConnection, e: ZoomAPIAuthenticationError):
    if zoom_oauth_connection.state == ZoomOAuthConnectionStates.DISCONNECTED:
        logger.info(f"Zoom OAuth connection {zoom_oauth_connection.id} is already in state DISCONNECTED, skipping authentication error handling")
        return

    # Update zoom oauth connection state to indicate failure
    with transaction.atomic():
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
        zoom_oauth_connection.connection_failure_data = {
            "error": str(e),
            "timestamp": timezone.now().isoformat(),
        }
        zoom_oauth_connection.save()

    logger.exception(f"Zoom OAuth connection sync failed with ZoomAPIAuthenticationError for {zoom_oauth_connection.id}: {e}")

    # Create webhook event
    trigger_webhook(
        webhook_trigger_type=WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE,
        zoom_oauth_connection=zoom_oauth_connection,
        payload=zoom_oauth_connection_webhook_payload(zoom_oauth_connection),
    )


def get_local_recording_token_via_zoom_oauth_app(bot: Bot) -> str | None:
    project = bot.project
    meeting_url = bot.meeting_url
    zoom_oauth_app = project.zoom_oauth_apps.first()
    if not zoom_oauth_app:
        return None

    meeting_id, password = parse_zoom_join_url(meeting_url)
    if not meeting_id:
        logger.info(f"No meeting id found in join url {meeting_url}")
        return None

    mapping_for_meeting_id = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_app=zoom_oauth_app, meeting_id=str(meeting_id)).first()

    if not mapping_for_meeting_id:
        logger.info(f"No mapping found for meeting id {meeting_id} in zoom oauth app {zoom_oauth_app.id}")
        return None

    zoom_oauth_connection = mapping_for_meeting_id.zoom_oauth_connection

    if not zoom_oauth_connection.is_local_recording_token_supported:
        logger.info(f"Zoom oauth connection {zoom_oauth_connection.object_id} does not support local recording tokens, skipping")
        return None

    try:
        access_token = _get_access_token(zoom_oauth_connection)
        local_recording_token = _get_local_recording_token(meeting_id, access_token)
        return local_recording_token

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)
        logger.exception(f"Failed to get local recording token via zoom oauth app for {meeting_url}: {e}. This was considered an authentication error.")
        return None

    except Exception as e:
        logger.exception(f"Failed to get local recording token via zoom oauth app for {meeting_url}: {e}")
        return None


def get_onbehalf_token_via_zoom_oauth_app(bot: Bot) -> str | None:
    user_id_for_onbehalf_token = bot.zoom_onbehalf_token_zoom_oauth_connection_user_id()
    if not user_id_for_onbehalf_token:
        return None

    project = bot.project
    zoom_oauth_app = project.zoom_oauth_apps.first()
    if not zoom_oauth_app:
        return None

    zoom_oauth_connection = ZoomOAuthConnection.objects.filter(zoom_oauth_app=zoom_oauth_app, user_id=user_id_for_onbehalf_token).first()
    if not zoom_oauth_connection:
        return None

    if not zoom_oauth_connection.is_onbehalf_token_supported:
        logger.info(f"Zoom oauth connection {zoom_oauth_connection.object_id} does not support onbehalf tokens, skipping")
        return None

    meeting_url = bot.meeting_url

    meeting_id, password = parse_zoom_join_url(meeting_url)
    if not meeting_id:
        logger.info(f"No meeting id found in join url {meeting_url}")
        return None

    try:
        access_token = _get_access_token(zoom_oauth_connection)
        onbehalf_token = _get_onbehalf_token(meeting_id, access_token)
        return onbehalf_token

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)
        logger.exception(f"Failed to get onbehalf token via zoom oauth app with user id {user_id_for_onbehalf_token}: {e}. This was considered an authentication error.")
        return None

    except Exception as e:
        logger.exception(f"Failed to get onbehalf token via zoom oauth app with user id {user_id_for_onbehalf_token}: {e}")
        return None


def get_zak_token_via_zoom_oauth_connection(zoom_oauth_connection: ZoomOAuthConnection) -> str | None:
    access_token = _get_access_token(zoom_oauth_connection)
    return _get_zak_token(access_token)


def get_zoom_tokens_via_zoom_oauth_app(bot: Bot) -> dict | None:
    onbehalf_token = get_onbehalf_token_via_zoom_oauth_app(bot)
    local_recording_token = get_local_recording_token_via_zoom_oauth_app(bot)

    return {
        "zak_token": None,
        "join_token": None,
        "app_privilege_token": local_recording_token,
        "onbehalf_token": onbehalf_token,
    }
