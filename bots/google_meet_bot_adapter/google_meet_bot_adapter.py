import json
import logging
import tempfile
from typing import Callable

from bots.google_meet_bot_adapter.chrome_profile_storage import (
    download_chrome_profile,
    upload_chrome_profile,
)
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
        ui_interaction_mode: str,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.google_meet_closed_captions_language = google_meet_closed_captions_language
        self.google_meet_bot_login_is_available = google_meet_bot_login_is_available
        self.google_meet_bot_login_should_be_used = google_meet_bot_login_should_be_used and google_meet_bot_login_is_available
        self.create_google_meet_bot_login_session_callback = create_google_meet_bot_login_session_callback
        self.google_meet_bot_login_session = None
        self.modify_dom_for_video_recording = modify_dom_for_video_recording
        self.number_of_times_blocked_by_google = 0
        self.number_of_times_mocap_sequence_not_available = 0
        self.ui_interaction_mode = ui_interaction_mode
        self.chrome_user_data_dir = None
        self.chrome_profile_loaded_from_s3 = False
        self.chrome_profile_s3_failed = False
        self.chrome_profile_sso_completed = False

    def should_retry_joining_meeting_that_requires_login_by_logging_in(self):
        # If we don't have the ability to login, we can't retry
        if not self.google_meet_bot_login_is_available:
            logger.info("Meeting requires login, but Google meet bot login is not available, so we can't retry")
            return False

        # If we already tried to login, we can't retry
        if self.google_meet_bot_login_should_be_used:
            logger.info("Meeting requires login, but we already tried to login, so we can't retry")
            return False

        # If we loaded a cached profile from S3 and it didn't work (cookies
        # expired), mark it so the next init_driver won't try to reuse it
        # again and will instead do a fresh SSO login.
        if self.chrome_profile_loaded_from_s3:
            logger.info("Cached Chrome profile from S3 did not work, will do fresh SSO login on retry")
            self.chrome_profile_s3_failed = True
            self.chrome_profile_loaded_from_s3 = False

        # Activate the flag that says, we are going to login this time and then retry
        self.google_meet_bot_login_should_be_used = True
        logger.info("Meeting requires login and Google meet bot login is available, so we will retry by logging in")
        return True

    def get_chromedriver_payload_file_names(self):
        return ["google_meet_bot_adapter/google_meet_chromedriver_payload.js"]

    def get_websocket_port(self):
        return 8765

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return result

    def send_video(self, video_url, loop=False, mute_video=False):
        logger.info(f"send_video called with video_url = {video_url}, loop = {loop}, mute_video = {mute_video}")
        self.driver.execute_script(f"window.botOutputManager.playVideo({json.dumps(video_url)}, {json.dumps(loop)}, {json.dumps(mute_video)})")

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
            # Use a temporary user-data-dir instead of --guest.
            # Chrome's --guest mode (as of Chrome 134) can inherit cookies from the default
            # profile, which prevents Google from triggering the SAML SSO redirect on the first
            # attempt. Using a unique temporary profile guarantees a clean session every time,
            # so the SSO flow works on the first try.
            self.chrome_user_data_dir = tempfile.mkdtemp(prefix="chrome-gmeet-")

            # Try to reuse a cached Chrome profile from S3 so we can skip the
            # SSO flow entirely. Only attempt this if a previous attempt with a
            # cached profile didn't fail (cookies may have expired).
            if not self.chrome_profile_s3_failed:
                login_domain = self._get_login_domain_for_profile()
                if login_domain:
                    if download_chrome_profile(login_domain, self.chrome_user_data_dir):
                        self.chrome_profile_loaded_from_s3 = True
                        logger.info(f"Using cached Chrome profile from S3 for domain {login_domain}")
                    else:
                        logger.info(f"No cached Chrome profile available for domain {login_domain}, will use fresh profile")

            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
            logger.info(f"Using temporary Chrome profile for Google Meet bot login: {self.chrome_user_data_dir}")

    def _get_login_domain_for_profile(self):
        """Get the login domain early so we can download a cached profile.

        Calls the create_google_meet_bot_login_session_callback and stores the
        result so login_to_google_meet_account can reuse it later without
        calling the callback again.
        """
        if self.google_meet_bot_login_session:
            return self.google_meet_bot_login_session.get("login_domain")
        try:
            self.google_meet_bot_login_session = self.create_google_meet_bot_login_session_callback()
            if self.google_meet_bot_login_session:
                return self.google_meet_bot_login_session.get("login_domain")
        except Exception as e:
            logger.warning(f"Error getting login domain for Chrome profile cache: {e}")
        return None

    def subclass_specific_before_driver_close(self):
        # Upload the Chrome profile to S3 if we completed SSO during this
        # session, so future bots can reuse it and skip the SSO flow.
        if self.chrome_profile_sso_completed and self.chrome_user_data_dir:
            login_domain = None
            if self.google_meet_bot_login_session:
                login_domain = self.google_meet_bot_login_session.get("login_domain")
            if login_domain:
                logger.info(f"Uploading Chrome profile to S3 for domain {login_domain}")
                upload_chrome_profile(login_domain, self.chrome_user_data_dir)

        # Skip the logout navigation if we are caching the profile, since
        # logging out invalidates the Google session server-side and would
        # make the cached profile useless for the next bot.
        if self.chrome_profile_loaded_from_s3 or self.chrome_profile_sso_completed:
            logger.info("Skipping Google logout to preserve Chrome profile for reuse")
        elif self.google_meet_bot_login_session:
            logger.info("Navigating to the logout page to sign out of the Google account")
            try:
                self.driver.get("https://www.google.com/accounts/logout")
            except Exception as e:
                logger.warning(f"Error navigating to the logout page to sign out of the Google account: {e}")

        # Clean up the temporary Chrome profile directory
        if self.chrome_user_data_dir:
            try:
                import shutil

                shutil.rmtree(self.chrome_user_data_dir, ignore_errors=True)
                logger.info(f"Cleaned up temporary Chrome profile: {self.chrome_user_data_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary Chrome profile: {e}")

