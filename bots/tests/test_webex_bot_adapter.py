import inspect
import json
import os
import re
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.models import RecordingViews
from bots.per_participant_realtime_video_configuration import PerParticipantRealtimeVideoConfiguration
from bots.web_bot_adapter import web_bot_adapter
from bots.web_bot_adapter.ui_methods import (
    UiBlockedByCaptchaException,
    UiCouldNotJoinMeetingWaitingForHostException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiIncorrectPasswordException,
    UiMeetingNotFoundException,
    UiRequestToJoinDeniedException,
    UiRetryableException,
)
from bots.webex_bot_adapter import WebexBotAdapter
from bots.webex_bot_adapter.webex_bot_adapter import UiWebexAuthorizationFailedException
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

MEETING_URL = "https://acme.my.webex.com/meet/jane.doe"

JOINING = {"phase": "joining", "othersInMeeting": False}
LOBBY_ALONE = {"phase": "lobby", "othersInMeeting": False}
LOBBY_WITH_OTHERS = {"phase": "lobby", "othersInMeeting": True}
JOINED = {"phase": "joined", "othersInMeeting": True}


def create_adapter(create_guest_token_callback=lambda: "guest-token", **automatic_leave_settings):
    callbacks = {name: MagicMock() for name in ("send_message_callback", "add_video_frame_callback", "wants_any_video_frames_callback", "add_audio_chunk_callback", "add_mixed_audio_chunk_callback", "add_per_participant_video_frame_callback", "add_encoded_mp4_chunk_callback", "upsert_caption_callback", "upsert_chat_message_callback", "add_participant_event_callback", "start_recording_screen_callback", "stop_recording_screen_callback")}
    adapter = WebexBotAdapter(
        display_name='Bot "Quote"',
        meeting_url=MEETING_URL,
        automatic_leave_configuration=AutomaticLeaveConfiguration(**automatic_leave_settings),
        per_participant_realtime_video_configuration=PerParticipantRealtimeVideoConfiguration(),
        recording_view=RecordingViews.SPEAKER_VIEW,
        should_create_debug_recording=False,
        video_frame_size=(1280, 720),
        record_chat_messages_when_paused=False,
        disable_incoming_video=False,
        record_participant_speech_start_stop_events=False,
        room_sync_source_participant_configuration=None,
        create_guest_token_callback=create_guest_token_callback,
        **callbacks,
    )
    adapter.driver = MagicMock()
    return adapter


def join_states(adapter, *states):
    adapter.driver.execute_script.side_effect = list(states)


class FakeClock:
    """Makes every call to sleep advance time, so waiting for a timeout does not take any real time."""

    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WebexInitialDataTest(SimpleTestCase):
    def test_initial_data_has_the_guest_token_meeting_and_bot_name(self):
        adapter = create_adapter()

        initial_data_code = adapter.subclass_specific_initial_data_code()

        self.assertIn(f"guestToken: {json.dumps('guest-token')}", initial_data_code)
        self.assertIn(f"meetingUrl: {json.dumps(MEETING_URL)}", initial_data_code)
        bot_name_literal = json.dumps('Bot "Quote"')
        self.assertIn(f"botName: {bot_name_literal}", initial_data_code)

    def test_every_attempt_gets_a_new_guest_token(self):
        callback = MagicMock(side_effect=["first", "second"])
        adapter = create_adapter(callback)

        self.assertIn('"first"', adapter.subclass_specific_initial_data_code())
        self.assertIn('"second"', adapter.subclass_specific_initial_data_code())

    def test_failing_to_create_a_guest_token_tells_the_controller_once(self):
        adapter = create_adapter(MagicMock(side_effect=WebexAuthError("refresh token expired")))

        for _ in range(2):
            with self.assertRaises(UiWebexAuthorizationFailedException):
                adapter.subclass_specific_initial_data_code()

        adapter.send_message_callback.assert_called_once_with({"message": adapter.Messages.COULD_NOT_CONNECT_TO_MEETING})


class WebexJoinFailureTest(SimpleTestCase):
    def test_failures_are_reported_as_the_exception_the_join_loop_understands(self):
        expected_exceptions = {
            "meeting_not_found": UiMeetingNotFoundException,
            "incorrect_password": UiIncorrectPasswordException,
            "blocked_by_captcha": UiBlockedByCaptchaException,
            "removed_from_waiting_room": UiRequestToJoinDeniedException,
            "join": UiRetryableException,
            "create": UiRetryableException,
        }
        for method, expected_exception in expected_exceptions.items():
            with self.subTest(method=method):
                adapter = create_adapter()
                join_states(adapter, JOINING)

                adapter.handle_failed_to_join({"method": method})

                with self.assertRaises(expected_exception):
                    adapter.wait_to_be_admitted_to_meeting()

    def test_a_failure_from_an_earlier_attempt_is_forgotten(self):
        adapter = create_adapter()
        adapter.handle_failed_to_join({"method": "meeting_not_found"})
        join_states(adapter, JOINED)

        with patch("bots.webex_bot_adapter.webex_ui_methods.start_static_page_server", return_value=8080):
            adapter.attempt_to_join_meeting()

        adapter.driver.get.assert_called_once_with("http://127.0.0.1:8080/webex_chromedriver_page.html")

    def test_not_being_able_to_read_the_page_is_retryable(self):
        adapter = create_adapter()
        adapter.driver.execute_script.side_effect = Exception("chrome not reachable")

        with self.assertRaises(UiRetryableException):
            adapter.wait_to_be_admitted_to_meeting()


class WebexWaitToBeAdmittedTest(SimpleTestCase):
    @override_settings(ENABLE_WAITING_ROOM_STATE_FOR_WEB_BOTS=True)
    @patch("bots.webex_bot_adapter.webex_ui_methods.time")
    def test_returns_once_joined_and_reports_the_waiting_room_once(self, mock_time):
        mock_time.time.return_value = 0
        adapter = create_adapter()
        join_states(adapter, JOINING, LOBBY_WITH_OTHERS, LOBBY_WITH_OTHERS, JOINED)

        adapter.wait_to_be_admitted_to_meeting()

        adapter.send_message_callback.assert_called_once_with({"message": adapter.Messages.BOT_PUT_IN_WAITING_ROOM})

    @patch("bots.webex_bot_adapter.webex_ui_methods.time", new_callable=FakeClock)
    def test_waiting_in_the_lobby_with_other_people_times_out_as_a_waiting_room(self, _):
        adapter = create_adapter(waiting_room_timeout_seconds=30, wait_for_host_to_start_meeting_timeout_seconds=300)
        adapter.driver.execute_script.return_value = LOBBY_WITH_OTHERS
        adapter.abort_join_attempt = MagicMock()

        with self.assertRaises(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
            adapter.wait_to_be_admitted_to_meeting()

        adapter.abort_join_attempt.assert_called_once()

    @patch("bots.webex_bot_adapter.webex_ui_methods.time", new_callable=FakeClock)
    def test_waiting_in_the_lobby_alone_times_out_as_waiting_for_the_host(self, clock):
        adapter = create_adapter(waiting_room_timeout_seconds=30, wait_for_host_to_start_meeting_timeout_seconds=300)
        adapter.driver.execute_script.return_value = LOBBY_ALONE

        with self.assertRaises(UiCouldNotJoinMeetingWaitingForHostException):
            adapter.wait_to_be_admitted_to_meeting()

        self.assertGreater(clock.now, 300)

    @patch("bots.webex_bot_adapter.webex_ui_methods.time", new_callable=FakeClock)
    def test_the_timeout_restarts_when_the_host_arrives(self, clock):
        adapter = create_adapter(waiting_room_timeout_seconds=30, wait_for_host_to_start_meeting_timeout_seconds=300)
        join_states(adapter, *[LOBBY_ALONE] * 200, *[LOBBY_WITH_OTHERS] * 10, JOINED)

        adapter.wait_to_be_admitted_to_meeting()

        self.assertGreater(clock.now, 200)

    @patch("bots.webex_bot_adapter.webex_ui_methods.time", new_callable=FakeClock)
    def test_giving_up_leaves_the_meeting(self, _):
        adapter = create_adapter(waiting_room_timeout_seconds=1)
        adapter.driver.execute_script.return_value = LOBBY_WITH_OTHERS
        adapter.abort_join_attempt = MagicMock()

        with self.assertRaises(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
            adapter.wait_to_be_admitted_to_meeting()

        script = adapter.driver.execute_async_script.call_args.args[0]
        self.assertIn("window.leaveMeeting()", script)


class WebexAfterJoinTest(SimpleTestCase):
    def test_the_bot_can_record_once_it_has_joined(self):
        adapter = create_adapter()
        adapter.after_bot_can_record_meeting = MagicMock()

        adapter.subclass_specific_after_bot_joined_meeting()

        adapter.after_bot_can_record_meeting.assert_called_once()


class WebexPayloadContractTest(SimpleTestCase):
    def test_the_websocket_client_has_every_method_the_base_adapter_calls(self):
        base_adapter_source = inspect.getsource(web_bot_adapter)
        called_methods = set(re.findall(r"window\.ws\??\.(\w+)\(", base_adapter_source))
        with open(os.path.join(os.path.dirname(inspect.getfile(WebexBotAdapter)), "webex_chromedriver_payload.js")) as payload:
            payload_source = payload.read()

        self.assertTrue(called_methods)
        for method in called_methods:
            with self.subTest(method=method):
                self.assertRegex(payload_source, rf"(?m)^    {method}\(", msg=f"webex_chromedriver_payload.js does not define WebSocketClient.{method}, which WebBotAdapter calls")
