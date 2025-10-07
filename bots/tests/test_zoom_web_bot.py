import threading
import time
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase

from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotStates, Credentials, Organization, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes


# Helper functions for creating mocks
def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.key = "test-recording-key"
    return mock_file_uploader


def create_mock_zoom_web_driver():
    mock_driver = MagicMock()
    mock_driver.execute_script.return_value = "test_result"
    return mock_driver


class TestZoomWebBot(TransactionTestCase):
    def setUp(self):
        # Recreate organization and project for each test
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Recreate credentials
        self.credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        self.credentials.set_credentials({"client_id": "test_client_id", "client_secret": "test_client_secret"})

        # Create a bot for each test
        self.bot = Bot.objects.create(
            name="Test Zoom Web Bot",
            meeting_url="https://zoom.us/j/123123213?p=123123213",
            state=BotStates.READY,
            project=self.project,
            settings={
                "zoom_settings": {
                    "sdk": "web",
                },
            },
        )

        # Create default recording
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.DEEPGRAM,
            is_default_recording=True,
        )

        # Try to transition the state from READY to JOINING
        BotEventManager.create_event(self.bot, BotEventTypes.JOIN_REQUESTED)

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_join_meeting(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Set up a side effect that raises an exception on first attempt, then succeeds on second attempt
        with patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.ZoomWebUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.side_effect = [
                None,  # First call succeeds
            ]

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Allow time for the retry logic to run
            time.sleep(5)

            # Simulate meeting ending to trigger cleanup
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(4)

            # Verify the attempt_to_join_meeting method was called twice
            self.assertEqual(mock_attempt_to_join.call_count, 1, "attempt_to_join_meeting should be called once")

            # Verify joining succeeded after retry by checking that these methods were called
            self.assertTrue(mock_driver.execute_script.called, "execute_script should be called after join")

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)  # Give it time to clean up

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.pause_recording", return_value=True)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.resume_recording", return_value=True)
    def test_recording_permission_denied(
        self,
        mock_pause_recording,
        mock_resume_recording,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Set up a side effect that succeeds on joining meeting
        with patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.ZoomWebUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.side_effect = [
                None,  # First call succeeds
            ]

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Allow time for join processing
            time.sleep(2)

            # Simulate recording permission denied by calling the method directly
            # This simulates what would happen when a RecordingPermissionChange message
            # with "denied" change is received via websocket
            controller.adapter.after_bot_recording_permission_denied()

            # Allow time for the message to be processed
            time.sleep(2)

            # Verify that the adapter's pause_recording() method was called
            # The adapter is WebBotAdapter which sets recording_paused = True
            self.assertTrue(controller.adapter.recording_paused, "Adapter's recording_paused flag should be True after permission denied")

            # Refresh bot from database to check state changes
            self.bot.refresh_from_db()

            # Verify that the bot state changed to JOINED_RECORDING_PERMISSION_DENIED
            self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING_PERMISSION_DENIED, "Bot should be in JOINED_RECORDING_PERMISSION_DENIED state after permission denied")

            # Verify that a BOT_RECORDING_PERMISSION_DENIED event was created
            permission_denied_events = self.bot.bot_events.filter(event_type=BotEventTypes.BOT_RECORDING_PERMISSION_DENIED, event_sub_type=BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION)
            self.assertTrue(permission_denied_events.exists(), "A BOT_RECORDING_PERMISSION_DENIED event should be created")

            # Simulate meeting ending to trigger cleanup
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(4)

            # Verify the attempt_to_join_meeting method was called once
            self.assertEqual(mock_attempt_to_join.call_count, 1, "attempt_to_join_meeting should be called once")

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)  # Give it time to clean up

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()
