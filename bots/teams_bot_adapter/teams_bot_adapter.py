import logging

from selenium.webdriver.common.keys import Keys

from bots.teams_bot_adapter.teams_ui_methods import (
    TeamsUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)


class TeamsBotAdapter(WebBotAdapter, TeamsUIMethods):
    def __init__(
        self,
        *args,
        teams_closed_captions_language: str | None,
        teams_bot_login_credentials: dict | None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.teams_closed_captions_language = teams_closed_captions_language
        self.teams_bot_login_credentials = teams_bot_login_credentials

    def get_chromedriver_payload_file_name(self):
        return "teams_bot_adapter/teams_chromedriver_payload.js"

    def get_websocket_port(self):
        return 8097

    def is_sent_video_still_playing(self):
        return False

    def send_video(self, video_url):
        logger.info(f"send_video called with video_url = {video_url}. This is not supported for teams")
        return

    def send_chat_message(self, text):
        chatInput = self.driver.execute_script("return document.querySelector('[aria-label=\"Type a message\"]')")

        if not chatInput:
            logger.error("Could not find chat input")
            return

        try:
            chatInput.send_keys(text)
            chatInput.send_keys(Keys.ENTER)
        except Exception as e:
            logger.error(f"Error sending chat message: {e}")
            return

    def update_closed_captions_language(self, language):
        if self.teams_closed_captions_language == language:
            logger.info(f"In update_closed_captions_language, closed captions language is already set to {language}. Doing nothing.")
            return

        if not language:
            logger.info("In update_closed_captions_language, new language is None. Doing nothing.")
            return

        self.teams_closed_captions_language = language
        closed_caption_set_language_result = self.driver.execute_script(f"return window.callManager?.setClosedCaptionsLanguage('{self.teams_closed_captions_language}')")
        if closed_caption_set_language_result:
            logger.info("In update_closed_captions_language, closed captions language set programatically")
        else:
            logger.error("In update_closed_captions_language, failed to set closed captions language programatically")

    def get_staged_bot_join_delay_seconds(self):
        return 10

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()
