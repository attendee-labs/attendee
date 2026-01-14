import logging
import re
import subprocess

from selenium.webdriver.common.keys import Keys

from bots.teams_bot_adapter.teams_ui_methods import (
    TeamsUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)


def _html_fragment_for_clipboard(text: str) -> str:
    # Convert newlines to <br>
    result = text.replace("\n", "<br>")

    # Convert spaces outside of HTML tags to &#32;
    # Parse through the text and only replace spaces that are not inside tags
    parts = []
    i = 0
    while i < len(result):
        if result[i] == "<":
            # Find the end of the tag
            tag_end = result.find(">", i)
            if tag_end != -1:
                # Keep the tag as-is
                parts.append(result[i : tag_end + 1])
                i = tag_end + 1
            else:
                # Malformed tag, treat as text
                parts.append("&#32;" if result[i] == " " else result[i])
                i += 1
        elif result[i] == " ":
            parts.append("&#32;")
            i += 1
        else:
            parts.append(result[i])
            i += 1

    return "".join(parts)


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

    def send_chat_message(self, text, to_user_uuid):
        chatInput = self.driver.execute_script('return document.querySelector(\'[aria-label="Type a message"], [placeholder="Type a message"]\')')

        if not chatInput:
            logger.error("Could not find chat input")
            return

        text_contains_html = bool(re.search(r"<\s*(?:p|br|a|b|i)(?:\s|>|/)", text, flags=re.IGNORECASE))
        if text_contains_html:
            self.deliver_chat_message_via_xclip(chatInput, text)
        else:
            self.deliver_chat_message_via_keys(chatInput, text)

    def deliver_chat_message_via_xclip(self, chatInput, text):
        try:
            html_fragment = _html_fragment_for_clipboard(text)

            # Add the html fragment to the clipboard
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "text/html", "-i"],
                input=html_fragment.encode("utf-8"),
                check=True,
            )

            # Paste the html fragment into the chat input
            chatInput.send_keys(Keys.CONTROL, "v")
            chatInput.send_keys(Keys.ENTER)
        except Exception as e:
            logger.error(f"Error sending chat message via xclip HTML paste: {e}")

    def deliver_chat_message_via_keys(self, chatInput, text):
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
        closed_caption_set_language_result = self.driver.execute_script("return window.callManager?.setClosedCaptionsLanguage(arguments[0]);", self.teams_closed_captions_language)
        if closed_caption_set_language_result:
            logger.info("In update_closed_captions_language, closed captions language set programatically")
        else:
            logger.error("In update_closed_captions_language, failed to set closed captions language programatically")

    def get_staged_bot_join_delay_seconds(self):
        return 10

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()
