import threading
import time
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase

from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotStates, Organization, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes
from bots.teams_bot_adapter.teams_ui_methods import UiTeamsBlockingUsException


# Helper functions for creating mocks
def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.filename = "test-recording-key"
    return mock_file_uploader


def create_mock_teams_driver():
    mock_driver = MagicMock()
    mock_driver.execute_script.return_value = "test_result"
    return mock_driver


class TestTeamsBot(TransactionTestCase):
    def setUp(self):
        # Recreate organization and project for each test
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create a bot for each test
        self.bot = Bot.objects.create(
            name="Test Teams Bot",
            meeting_url="https://teams.microsoft.com/meet/123123213?p=123123213",
            state=BotStates.READY,
            project=self.project,
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
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_join_retry_on_failure(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Set up a side effect that raises an exception on first attempt, then succeeds on second attempt
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.side_effect = [
                UiTeamsBlockingUsException("Teams is blocking us for whatever reason", "test_step"),  # First call fails
                None,  # Second call succeeds
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
            self.assertEqual(mock_attempt_to_join.call_count, 2, "attempt_to_join_meeting should be called twice - once for the initial failure and once for the retry")

            # Verify joining succeeded after retry by checking that these methods were called
            self.assertTrue(mock_driver.execute_script.called, "execute_script should be called after successful retry")

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)  # Give it time to clean up

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_handle_unexpected_exception_on_join(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Set up a side effect that raises an exception on first attempt, then succeeds on second attempt
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.side_effect = Exception("random exception")

            def save_screenshot_mock(path):
                with open(path, "w"):
                    pass

            mock_driver.save_screenshot.side_effect = save_screenshot_mock

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Allow time for the retry logic to run
            time.sleep(10)

            # Verify the attempt_to_join_meeting method was called four times
            self.assertEqual(mock_attempt_to_join.call_count, 4, "attempt_to_join_meeting should be called four times")

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)  # Give it time to clean up

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

            # Test that the last bot event is a FATAL_ERROR
            self.bot.refresh_from_db()
            last_bot_event = self.bot.bot_events.last()
            self.assertEqual(last_bot_event.event_type, BotEventTypes.FATAL_ERROR)
            self.assertEqual(last_bot_event.event_sub_type, BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND)
            self.assertEqual(last_bot_event.metadata.get("step"), "unknown")
            self.assertEqual(last_bot_event.metadata.get("exception_type"), "Exception")
            self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)
            print("last_bot_event", last_bot_event.__dict__)

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_attendee_internal_error_in_main_loop(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Mock the bot to be in JOINING state and simulate successful join
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.return_value = None  # Successful join

            # Mock one of the methods called in the main loop timeout to raise an exception
            # This will trigger the attendee internal error handling
            with patch.object(controller, "set_bot_heartbeat") as mock_set_heartbeat:
                mock_set_heartbeat.side_effect = Exception("Internal error during main loop processing")

                # Run the bot in a separate thread since it has an event loop
                bot_thread = threading.Thread(target=controller.run)
                bot_thread.daemon = True
                bot_thread.start()

                # Allow time for the bot to join and then hit the exception in the main loop
                time.sleep(10)

                # Now wait for the thread to finish naturally
                bot_thread.join(timeout=5)

                # If thread is still running after timeout, that's a problem to report
                if bot_thread.is_alive():
                    print("WARNING: Bot thread did not terminate properly after cleanup")

                # Close the database connection since we're in a thread
                connection.close()

                # Test that the last bot event is a FATAL_ERROR with ATTENDEE_INTERNAL_ERROR sub-type
                self.bot.refresh_from_db()
                last_bot_event = self.bot.bot_events.last()
                self.assertEqual(last_bot_event.event_type, BotEventTypes.FATAL_ERROR)
                self.assertEqual(last_bot_event.event_sub_type, BotEventSubTypes.FATAL_ERROR_ATTENDEE_INTERNAL_ERROR)
                self.assertEqual(last_bot_event.metadata.get("error"), "Internal error during main loop processing")
                self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)
                print("last_bot_event for attendee internal error", last_bot_event.__dict__)
