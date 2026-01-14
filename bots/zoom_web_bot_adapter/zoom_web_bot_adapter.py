import json
import logging
import os
import time
from datetime import datetime
from typing import Callable

import jwt

from bots.meeting_url_utils import parse_zoom_join_url
from bots.web_bot_adapter import WebBotAdapter
from bots.zoom_web_bot_adapter.zoom_web_ui_methods import UiZoomWebGenericJoinErrorException, ZoomWebUIMethods

logger = logging.getLogger(__name__)


def zoom_meeting_sdk_signature(
    meeting_number: str | int,
    role: int,
    *,
    expiration_seconds: int = 2 * 60 * 60,  # default 2 h
    video_webrtc_mode: int | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, str]:
    """
    Create a Zoom Meeting SDK JWT signature.

    Parameters
    ----------
    meeting_number : str | int
    role           : 0 for attendee, 1 for host
    expiration_seconds : lifetime for the token (min 1 800, max 172 800)
    video_webrtc_mode  : 0 or 1 (optional)
    client_id, client_secret: str | None

    Returns
    -------
    {"signature": "<jwt>", "sdkKey": "<sdk_key>"}
    """

    if not client_id or not client_secret:
        raise RuntimeError("Client id or secret is missing")

    iat = int(datetime.utcnow().timestamp()) - int(os.getenv("ZOOM_WEB_JWT_IAT_OFFSET_SECONDS", 0))
    exp = iat + expiration_seconds

    payload = {
        "appKey": client_id,
        "sdkKey": client_id,
        "mn": str(meeting_number),
        "role": role,
        "iat": iat,
        "exp": exp,
        "tokenExp": exp,
    }
    if video_webrtc_mode is not None:
        payload["video_webrtc_mode"] = video_webrtc_mode

    token = jwt.encode(payload, client_secret, algorithm="HS256")
    return {"signature": token, "sdkKey": client_id}


class ZoomWebBotAdapter(WebBotAdapter, ZoomWebUIMethods):
    def __init__(
        self,
        *args,
        zoom_oauth_credentials_callback: Callable[[], dict[str, str]],
        zoom_closed_captions_language: str | None,
        should_ask_for_recording_permission: bool,
        zoom_tokens: dict,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.meeting_id, self.meeting_password = parse_zoom_join_url(self.meeting_url)
        self.zoom_oauth_credentials_callback = zoom_oauth_credentials_callback

        # Doing it via callback so we don't store these in-memory
        zoom_oauth_credentials = self.zoom_oauth_credentials_callback()
        zoom_client_id = zoom_oauth_credentials["client_id"]
        zoom_client_secret = zoom_oauth_credentials["client_secret"]

        self.sdk_signature = zoom_meeting_sdk_signature(self.meeting_id, 0, client_id=zoom_client_id, client_secret=zoom_client_secret)
        self.zoom_closed_captions_language = zoom_closed_captions_language
        self.should_ask_for_recording_permission = should_ask_for_recording_permission
        self.zoom_tokens = zoom_tokens

        self.generic_join_error_retries = 0

    def get_chromedriver_payload_file_name(self):
        return "zoom_web_bot_adapter/zoom_web_chromedriver_payload.js"

    def get_websocket_port(self):
        return 8765

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return result

    def send_video(self, video_url):
        logger.info(f"send_video called with video_url = {video_url}")
        self.driver.execute_script(f"window.botOutputManager.playVideo({json.dumps(video_url)})")

    def change_gallery_view_page(self, next_page: bool):
        self.driver.execute_script(f"window?.changeGalleryViewPage({json.dumps(next_page)})")

    def send_chat_message(self, text, to_user_uuid):
        self.driver.execute_script("window?.sendChatMessage(arguments[0], arguments[1]);", text, to_user_uuid)

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def subclass_specific_initial_data_code(self):
        return f"""
            window.zoomInitialData = {{
                signature: {json.dumps(self.sdk_signature["signature"])},
                sdkKey: {json.dumps(self.sdk_signature["sdkKey"])},
                meetingNumber: {json.dumps(self.meeting_id)},
                meetingPassword: {json.dumps(self.meeting_password)},
                zakToken: {json.dumps(self.zoom_tokens.get("zak_token", ""))},
                joinToken: {json.dumps(self.zoom_tokens.get("join_token", ""))},
                appPrivilegeToken: {json.dumps(self.zoom_tokens.get("app_privilege_token", ""))},
                onBehalfToken: {json.dumps(self.zoom_tokens.get("onbehalf_token", ""))},
            }}
        """

    def subclass_specific_after_bot_joined_meeting(self):
        if self.should_ask_for_recording_permission:
            self.driver.execute_script("window?.askForMediaCapturePermission()")
        else:
            self.after_bot_can_record_meeting()

    def subclass_specific_handle_failed_to_join(self, reason):
        # Special case for removed from waiting room
        if reason.get("method") == "removed_from_waiting_room":
            self.send_request_to_join_denied_message()
            return

        if reason.get("method") != "join":
            return

        # Special case for external meeting issue
        if reason.get("errorCode") == 4011:
            self.send_message_callback(
                {
                    "message": self.Messages.ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING,
                    "zoom_result_code": str(reason.get("errorCode")) + ": " + str(reason.get("errorMessage")),
                }
            )
            return

        self.send_message_callback(
            {
                "message": self.Messages.ZOOM_MEETING_STATUS_FAILED,
                "zoom_result_code": str(reason.get("errorCode")) + ": " + str(reason.get("errorMessage")),
            }
        )

    def handle_generic_join_error(self):
        max_generic_join_error_retries = int(os.getenv("ZOOM_WEB_MAX_GENERIC_JOIN_ERROR_RETRIES", 3))
        generic_join_error_sleep_time_seconds = float(os.getenv("ZOOM_WEB_GENERIC_JOIN_ERROR_SLEEP_TIME_SECONDS", 5))
        # If we haven't exceeded the max number of retries, we'll throw the exception which will retry
        if self.generic_join_error_retries < max_generic_join_error_retries:
            self.generic_join_error_retries += 1

            logger.warning(f"Bot failed to join because generic join error. Raising UiZoomWebGenericJoinErrorException after sleeping for {generic_join_error_sleep_time_seconds} seconds. Generic join error retry {self.generic_join_error_retries} of {max_generic_join_error_retries}")

            # Recompute signature
            zoom_oauth_credentials = self.zoom_oauth_credentials_callback()
            zoom_client_id = zoom_oauth_credentials["client_id"]
            zoom_client_secret = zoom_oauth_credentials["client_secret"]
            self.sdk_signature = zoom_meeting_sdk_signature(self.meeting_id, 0, client_id=zoom_client_id, client_secret=zoom_client_secret)

            time.sleep(generic_join_error_sleep_time_seconds)  # Sleep for a bit so we're not constantly retrying

            raise UiZoomWebGenericJoinErrorException("Bot failed to join because generic join error")

        # Otherwise, we'll send the message to the controller to terminate the bot.
        logger.warning("Bot failed to join because generic join error. Sending message to controller to terminate bot.")
        self.subclass_specific_handle_failed_to_join(
            {
                "method": "join",
                "errorCode": 1,
                "errorMessage": "Fail to join the meeting.",
            }
        )
