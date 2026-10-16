import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from bots.google_meet_bot_adapter import GoogleMeetBotAdapter
from bots.teams_bot_adapter import TeamsBotAdapter
from bots.webex_bot_adapter import WebexBotAdapter
from bots.zoom_web_bot_adapter import ZoomWebBotAdapter

VIDEO_URL = "https://example.com/video.mp4"


def adapter_with_mock_driver(adapter_class):
    adapter = object.__new__(adapter_class)  # none of these tests need the adapter's constructor
    adapter.driver = MagicMock()
    return adapter


class WebBotAdapterVideoOutputTest(SimpleTestCase):
    def test_send_video_plays_the_video_through_the_bot_output_manager(self):
        for adapter_class in (GoogleMeetBotAdapter, ZoomWebBotAdapter, WebexBotAdapter):
            with self.subTest(adapter=adapter_class.__name__):
                adapter = adapter_with_mock_driver(adapter_class)

                adapter.send_video(VIDEO_URL, loop=True, mute_video=False)

                adapter.driver.execute_script.assert_called_once_with(f"window.botOutputManager.playVideo({json.dumps(VIDEO_URL)}, true, false)")

    def test_teams_plays_the_video_from_a_blob_url(self):
        adapter = adapter_with_mock_driver(TeamsBotAdapter)

        adapter.send_video(VIDEO_URL, loop=False, mute_video=True)

        adapter.driver.execute_script.assert_called_once_with(f"window.botOutputManager.playVideoWithBlobUrl({json.dumps(VIDEO_URL)}, false, true)")

    def test_is_sent_video_still_playing_asks_the_bot_output_manager(self):
        for adapter_class in (GoogleMeetBotAdapter, ZoomWebBotAdapter, TeamsBotAdapter, WebexBotAdapter):
            for is_playing in (True, False):
                with self.subTest(adapter=adapter_class.__name__, is_playing=is_playing):
                    adapter = adapter_with_mock_driver(adapter_class)
                    adapter.driver.execute_script.return_value = is_playing

                    self.assertEqual(adapter.is_sent_video_still_playing(), is_playing)

                    adapter.driver.execute_script.assert_called_once_with("return window.botOutputManager.isVideoPlaying();")
