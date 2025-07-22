import json
import logging

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
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.google_meet_closed_captions_language = google_meet_closed_captions_language

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

    def send_chat_message(self, text):
        self.driver.execute_script(f"window?.sendChatMessage({json.dumps(text)})")

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()
