import json
import logging

from bots.jitsi_bot_adapter.jitsi_ui_methods import (
    JitsiUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)


class JitsiBotAdapter(WebBotAdapter, JitsiUIMethods):
    """Adapter for Jitsi Meet based platforms (kMeet, Hostpoint Meet, self-hosted Jitsi).

    Joins as guest — Jitsi has no login, no captchas and no recording permission dialog.
    Which domains are treated as Jitsi is controlled by the JITSI_MEETING_DOMAINS
    environment variable (see bots/meeting_url_utils.py).
    """

    def __init__(self, *args, jitsi_room_password: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.jitsi_room_password = jitsi_room_password

    def get_chromedriver_payload_file_names(self):
        return ["jitsi_bot_adapter/jitsi_chromedriver_payload.js"]

    def get_websocket_port(self):
        return 8098

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager?.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return bool(result)

    def send_video(self, video_url, loop=False, mute_video=False):
        # Referenced eagerly by BotController's VideoOutputManager, so it must exist.
        # Same call as Google Meet / Zoom web; video output is not verified on Jitsi.
        logger.info(f"send_video called with video_url = {video_url}, loop = {loop}, mute_video = {mute_video}")
        self.driver.execute_script(f"window.botOutputManager?.playVideo({json.dumps(video_url)}, {json.dumps(loop)}, {json.dumps(mute_video)})")

    def send_chat_message(self, text, to_user_uuid):
        # window.sendChatMessage is defined by the jitsi chromedriver payload
        self.driver.execute_script("window.sendChatMessage?.(arguments[0]);", text)

    def subclass_specific_initial_data_code(self):
        return "window.jitsiInitialData = {}"

    def handle_meeting_ended(self, meeting_id):
        # The payload reports CONFERENCE_LEFT as meeting_ended, which also fires after our own
        # hangup in leave(), after a kick and right after conference.destroyed. Each of those
        # already reported the end (and set left_meeting), so report it only once.
        if self.left_meeting:
            logger.info("Ignoring meeting_ended, the end of the meeting was already reported")
            return
        super().handle_meeting_ended(meeting_id)

    def subclass_specific_after_bot_joined_meeting(self):
        # Jitsi has no recording permission concept — recording can start right away
        self.after_bot_can_record_meeting()

    def add_subclass_specific_chrome_options(self, options):
        # Newer Chrome blocks a public page (the Jitsi web app) from reaching ws://localhost
        # (Local/Private Network Access checks); the payload needs exactly that to talk to
        # the python side, so disable those checks for the bot's browser.
        options.add_argument("--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks")
