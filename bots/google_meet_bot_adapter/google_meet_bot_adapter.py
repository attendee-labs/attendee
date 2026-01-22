import json
import logging
from typing import Callable

from bots.google_meet_bot_adapter.google_meet_ui_methods import (
    GoogleMeetUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)


class GoogleMeetBotAdapter(WebBotAdapter, GoogleMeetUIMethods):
    def __init__(
        self,
        *args,
        google_meet_closed_captions_language: str | None,
        google_meet_bot_login_is_available: bool,
        google_meet_bot_login_should_be_used: bool,
        create_google_meet_bot_login_session_callback: Callable[[], dict],
        modify_dom_for_video_recording: bool,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.google_meet_closed_captions_language = google_meet_closed_captions_language
        self.google_meet_bot_login_is_available = google_meet_bot_login_is_available
        self.google_meet_bot_login_should_be_used = google_meet_bot_login_should_be_used and google_meet_bot_login_is_available
        self.create_google_meet_bot_login_session_callback = create_google_meet_bot_login_session_callback
        self.google_meet_bot_login_session = None
        self.modify_dom_for_video_recording = modify_dom_for_video_recording

    def should_retry_joining_meeting_that_requires_login_by_logging_in(self):
        # If we don't have the ability to login, we can't retry
        if not self.google_meet_bot_login_is_available:
            logger.info("Meeting requires login, but Google meet bot login is not available, so we can't retry")
            return False

        # If we already tried to login, we can't retry
        if self.google_meet_bot_login_should_be_used:
            logger.info("Meeting requires login, but we already tried to login, so we can't retry")
            return False

        # Activate the flag that says, we are going to login this time and then retry
        self.google_meet_bot_login_should_be_used = True
        logger.info("Meeting requires login and Google meet bot login is available, so we will retry by logging in")
        return True

    def get_chromedriver_payload_file_name(self):
        return "google_meet_bot_adapter/google_meet_chromedriver_payload.js"

    def get_websocket_port(self):
        return 8765

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return result

    def send_video(self, video_url):
        logger.info(f"send_video called with video_url = {video_url}")
        self.driver.execute_script(f"window.botOutputManager.playVideo({json.dumps(video_url)})")

    def send_chat_message(self, text, to_user_uuid):
        self.driver.execute_script("window?.sendChatMessage(arguments[0]);", text)

    def update_closed_captions_language(self, language):
        if self.google_meet_closed_captions_language == language:
            logger.info(f"In update_closed_captions_language, closed captions language is already set to {language}. Doing nothing.")
            return

        if not language:
            logger.info("In update_closed_captions_language, new language is None. Doing nothing.")
            return

        self.google_meet_closed_captions_language = language
        closed_caption_set_language_result = self.driver.execute_script(
            "return setClosedCaptionsLanguage(arguments[0]);",
            self.google_meet_closed_captions_language,
        )
        if closed_caption_set_language_result:
            logger.info("In update_closed_captions_language, closed captions language set programatically")
        else:
            logger.error("In update_closed_captions_language, failed to set closed captions language programatically")

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def subclass_specific_initial_data_code(self):
        return f"""
            window.googleMeetInitialData = {{
                modifyDomForVideoRecording: {"true" if self.modify_dom_for_video_recording else "false"},
            }}
        """

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()

    def add_subclass_specific_chrome_options(self, options):
        if self.google_meet_bot_login_should_be_used:
            options.add_argument("--guest")

    def subclass_specific_before_driver_close(self):
        if self.google_meet_bot_login_session:
            logger.info("Navigating to the logout page to sign out of the Google account")
            try:
                self.driver.get("https://www.google.com/accounts/logout")
            except Exception as e:
                logger.warning(f"Error navigating to the logout page to sign out of the Google account: {e}")
