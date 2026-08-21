import json
import struct
import threading
import time
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import tag

from bots.bot_controller import BotController
from bots.models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    ChatMessage,
    Organization,
    Participant,
    Project,
    Recording,
    RecordingStates,
    RecordingTypes,
    TranscriptionProviders,
    TranscriptionTypes,
)
from bots.web_bot_adapter.ui_methods import UiIncorrectPasswordException


def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.filename = "test-recording-key"
    return mock_file_uploader


def create_mock_jitsi_driver():
    mock_driver = MagicMock()
    mock_driver.execute_script.return_value = "test_result"
    return mock_driver


def json_frame(payload):
    # Binary websocket frame the way the payload sends it: int32 LE type 1 + utf-8 json
    return struct.pack("<I", 1) + json.dumps(payload).encode("utf-8")


BOT_USER = {"deviceId": "bot-device", "fullName": "Notizen · bosshart.sg", "humanized_status": "in_meeting", "isCurrentUser": True}
HUMAN_USER = {"deviceId": "human-device", "fullName": "Test Human", "humanized_status": "in_meeting", "isCurrentUser": False}


@tag("jitsi_tests")
class TestJitsiBot(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        self.bot = Bot.objects.create(
            name="Notizen · bosshart.sg",
            meeting_url="https://meet.jit.si/attendee-test-room",
            state=BotStates.READY,
            project=self.project,
            settings={
                "automatic_leave_settings": {
                    "only_participant_in_meeting_timeout_seconds": 2,
                    "silence_timeout_seconds": 999999,
                    "silence_activate_after_seconds": 999999,
                },
            },
        )

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM,
            is_default_recording=True,
        )

        BotEventManager.create_event(self.bot, BotEventTypes.JOIN_REQUESTED)

    def test_get_jitsi_bot_adapter_factory(self):
        self.bot.settings = {**self.bot.settings, "jitsi_settings": {"room_password": "geheim-123"}}
        self.bot.save()

        controller = BotController(self.bot.id)
        controller.per_participant_non_streaming_audio_input_manager = MagicMock()
        controller.closed_caption_manager = MagicMock()
        controller.screen_and_audio_recorder = None
        adapter = controller.get_jitsi_bot_adapter()

        self.assertEqual(adapter.jitsi_room_password, "geheim-123")
        self.assertEqual(adapter.get_websocket_port(), 8098)
        self.assertEqual(adapter.get_chromedriver_payload_file_names(), ["jitsi_bot_adapter/jitsi_chromedriver_payload.js"])
        self.assertEqual(adapter.get_staged_bot_join_delay_seconds(), 5)
        self.assertEqual(adapter.display_name, "Notizen · bosshart.sg")
        # No caption callback — jitsi has no server-side captions
        self.assertIsNone(adapter.upsert_caption_callback)

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.tasks.send_slack_alert_task.send_slack_alert.delay")
    @patch("bots.bot_controller.bot_controller.ScreenAndAudioRecorder.start_recording", return_value=None)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.stop_recording", return_value=None)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_bot_records_meeting_and_auto_leaves_when_all_participants_leave(self, MockFileUploader, MockChromeDriver, MockDisplay, mock_stop_recording, mock_start_recording, mock_slack, mock_debug_recording):
        MockFileUploader.return_value = create_mock_file_uploader()
        mock_driver = create_mock_jitsi_driver()
        MockChromeDriver.return_value = mock_driver
        MockDisplay.return_value = MagicMock()

        controller = BotController(self.bot.id)

        with patch("bots.jitsi_bot_adapter.jitsi_ui_methods.JitsiUIMethods.attempt_to_join_meeting", return_value=None):
            bot_thread = threading.Thread(target=controller.run, daemon=True)
            bot_thread.start()

            # Wait for the bot to reach JOINED_RECORDING (jitsi grants recording right away)
            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.JOINED_RECORDING:
                    break
            self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING)

            # Feed real payload frames through the websocket handler: both users join,
            # the human sends a chat message, then leaves again
            adapter = controller.adapter
            adapter.handle_websocket([json_frame({"type": "UsersUpdate", "newUsers": [BOT_USER, HUMAN_USER], "updatedUsers": [], "removedUsers": []})])
            adapter.handle_websocket([json_frame({"type": "ChatMessage", "message_uuid": "msg-1", "participant_uuid": "human-device", "timestamp": int(time.time()), "text": "Hallo Bot"})])
            time.sleep(1)
            adapter.handle_websocket([json_frame({"type": "UsersUpdate", "newUsers": [], "updatedUsers": [], "removedUsers": [{**HUMAN_USER, "humanized_status": "not_in_meeting"}]})])

            # only_participant_in_meeting_timeout_seconds=2 → auto-leave kicks in
            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.ENDED:
                    break

            bot_thread.join(timeout=10)
            connection.close()

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)

        bot_events = self.bot.bot_events.all()
        self.assertEqual([e.event_type for e in bot_events], [BotEventTypes.JOIN_REQUESTED, BotEventTypes.BOT_JOINED_MEETING, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED, BotEventTypes.LEAVE_REQUESTED, BotEventTypes.BOT_LEFT_MEETING, BotEventTypes.POST_PROCESSING_COMPLETED])
        leave_requested_event = bot_events[3]
        self.assertEqual(leave_requested_event.event_sub_type, BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING)

        # Participants and their join/leave events landed in the db
        participants = Participant.objects.filter(bot=self.bot)
        self.assertEqual({p.full_name for p in participants}, {"Notizen · bosshart.sg", "Test Human"})
        self.assertTrue(participants.get(uuid="bot-device").is_the_bot)

        # The chat message landed in the db, attributed to the human
        chat_message = ChatMessage.objects.get(bot=self.bot)
        self.assertEqual(chat_message.text, "Hallo Bot")
        self.assertEqual(chat_message.participant.full_name, "Test Human")

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.tasks.send_slack_alert_task.send_slack_alert.delay")
    @patch("bots.bot_controller.bot_controller.ScreenAndAudioRecorder.start_recording", return_value=None)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.stop_recording", return_value=None)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_bot_ends_when_removed_from_meeting(self, MockFileUploader, MockChromeDriver, MockDisplay, mock_stop_recording, mock_start_recording, mock_slack, mock_debug_recording):
        MockFileUploader.return_value = create_mock_file_uploader()
        MockChromeDriver.return_value = create_mock_jitsi_driver()
        MockDisplay.return_value = MagicMock()

        controller = BotController(self.bot.id)

        with patch("bots.jitsi_bot_adapter.jitsi_ui_methods.JitsiUIMethods.attempt_to_join_meeting", return_value=None):
            bot_thread = threading.Thread(target=controller.run, daemon=True)
            bot_thread.start()

            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.JOINED_RECORDING:
                    break
            self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING)

            # What the payload sends when the bot gets kicked (KICKED event)
            controller.adapter.handle_websocket([json_frame({"type": "MeetingStatusChange", "change": "removed_from_meeting"})])

            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.ENDED:
                    break

            bot_thread.join(timeout=10)
            connection.close()

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)
        event_types = [e.event_type for e in self.bot.bot_events.all()]
        self.assertIn(BotEventTypes.MEETING_ENDED, event_types)
        self.assertNotIn(BotEventTypes.LEAVE_REQUESTED, event_types)

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.tasks.send_slack_alert_task.send_slack_alert.delay")
    @patch("bots.bot_controller.bot_controller.ScreenAndAudioRecorder.start_recording", return_value=None)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.stop_recording", return_value=None)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_bot_could_not_join_when_room_password_is_missing(self, MockFileUploader, MockChromeDriver, MockDisplay, mock_stop_recording, mock_start_recording, mock_slack, mock_debug_recording):
        MockFileUploader.return_value = create_mock_file_uploader()
        MockChromeDriver.return_value = create_mock_jitsi_driver()
        MockDisplay.return_value = MagicMock()

        controller = BotController(self.bot.id)

        # What wait_for_conference_joined raises for a locked room without a configured password
        with patch("bots.jitsi_bot_adapter.jitsi_ui_methods.JitsiUIMethods.attempt_to_join_meeting", side_effect=UiIncorrectPasswordException("Room requires a password but none was configured", "room_password")):
            bot_thread = threading.Thread(target=controller.run, daemon=True)
            bot_thread.start()

            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.FATAL_ERROR:
                    break

            bot_thread.join(timeout=10)
            connection.close()

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)
        could_not_join_event = self.bot.bot_events.get(event_type=BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_UNABLE_TO_CONNECT_TO_MEETING)

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.tasks.send_slack_alert_task.send_slack_alert.delay")
    @patch("bots.bot_controller.bot_controller.ScreenAndAudioRecorder.start_recording", return_value=None)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.stop_recording", return_value=None)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_bot_auto_leaves_after_silence_timeout(self, MockFileUploader, MockChromeDriver, MockDisplay, mock_stop_recording, mock_start_recording, mock_slack, mock_debug_recording):
        # Use the default (long) silence settings for this test
        self.bot.settings = {}
        self.bot.save()

        MockFileUploader.return_value = create_mock_file_uploader()
        MockChromeDriver.return_value = create_mock_jitsi_driver()
        MockDisplay.return_value = MagicMock()

        controller = BotController(self.bot.id)

        with patch("bots.jitsi_bot_adapter.jitsi_ui_methods.JitsiUIMethods.attempt_to_join_meeting", return_value=None):
            bot_thread = threading.Thread(target=controller.run, daemon=True)
            bot_thread.start()

            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.JOINED_RECORDING:
                    break
            self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING)

            adapter = controller.adapter
            adapter.handle_websocket([json_frame({"type": "UsersUpdate", "newUsers": [BOT_USER, HUMAN_USER], "updatedUsers": [], "removedUsers": []})])
            # SilenceStatus with isSilent false marks audio activity — like the payload does
            adapter.handle_websocket([json_frame({"type": "SilenceStatus", "isSilent": False, "volume": 3.5})])

            # Simulate the clock having passed the activation threshold (1200s after join);
            # activation resets last_audio_message_processed_time to now
            adapter.joined_at = time.time() - 2000
            adapter.check_auto_leave_conditions()
            self.assertTrue(adapter.silence_detection_activated)
            # Then simulate 600s+ without any audio activity
            adapter.last_audio_message_processed_time = time.time() - 700
            adapter.check_auto_leave_conditions()

            for _ in range(40):
                time.sleep(0.5)
                self.bot.refresh_from_db()
                if self.bot.state == BotStates.ENDED:
                    break

            bot_thread.join(timeout=10)
            connection.close()

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)
        leave_requested_event = self.bot.bot_events.get(event_type=BotEventTypes.LEAVE_REQUESTED)
        self.assertEqual(leave_requested_event.event_sub_type, BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_SILENCE)
