import base64
import threading
import time
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase

from bots.bot_controller.bot_controller import BotController
from bots.bots_api_views import send_sync_command
from bots.models import Bot, BotChatMessageRequest, BotChatMessageRequestStates, BotChatMessageToOptions, BotEventManager, BotEventSubTypes, BotEventTypes, BotMediaRequest, BotMediaRequestMediaTypes, BotMediaRequestStates, BotStates, MediaBlob, Organization, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes
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

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_chat_message_delayed_until_adapter_ready(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        """
        Test that a chat message request is not sent immediately if the adapter is not ready,
        but is sent once the adapter becomes ready.
        """
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create a chat message request in the ENQUEUED state
        chat_message_request = BotChatMessageRequest.objects.create(
            bot=self.bot,
            message="Test message",
            to=BotChatMessageToOptions.EVERYONE,
        )

        # Create bot controller
        controller = BotController(self.bot.id)

        # Mock the attempt_to_join_meeting to succeed immediately
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.return_value = None  # Successful join

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Wait for the bot to join
            time.sleep(3)

            # Mock send_chat_message to track calls
            with patch.object(controller.adapter, "send_chat_message") as mock_send_chat_message:
                # Initially, the adapter is not ready to send chat messages
                controller.adapter.ready_to_send_chat_messages = False

                # Trigger sync_chat_message_requests
                controller.take_action_based_on_chat_message_requests_in_db()

                # Verify that send_chat_message was NOT called because adapter is not ready
                self.assertEqual(mock_send_chat_message.call_count, 0, "send_chat_message should not be called when adapter is not ready")

                # Verify that the chat message request is still in ENQUEUED state
                chat_message_request.refresh_from_db()
                self.assertEqual(chat_message_request.state, BotChatMessageRequestStates.ENQUEUED, "Chat message should remain in ENQUEUED state when adapter is not ready")

                # Wait 2 seconds
                time.sleep(2)

                # Now simulate the adapter becoming ready
                controller.adapter.ready_to_send_chat_messages = True

                # Simulate the READY_TO_SEND_CHAT_MESSAGE callback
                controller.adapter.send_message_callback({"message": controller.adapter.Messages.READY_TO_SEND_CHAT_MESSAGE})

                # Wait for the message to be processed
                time.sleep(1)

                # Verify that send_chat_message WAS called after adapter became ready
                self.assertEqual(mock_send_chat_message.call_count, 1, "send_chat_message should be called once after adapter becomes ready")

                # Verify the arguments passed to send_chat_message
                call_args = mock_send_chat_message.call_args
                self.assertEqual(call_args.kwargs["text"], "Test message")
                self.assertEqual(call_args.kwargs["to_user_uuid"], None)

                # Verify that the chat message request is now in SENT state
                chat_message_request.refresh_from_db()
                self.assertEqual(chat_message_request.state, BotChatMessageRequestStates.SENT, "Chat message should be in SENT state after being sent")

            # Clean up: simulate meeting ending to trigger cleanup
            controller.adapter.left_meeting = True
            controller.adapter.send_message_callback({"message": controller.adapter.Messages.MEETING_ENDED})
            time.sleep(2)

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.bot_controller.bot_controller.BotController.save_debug_recording", return_value=None)
    def test_audio_request_processed_after_chat_message(
        self,
        MockSaveDebugRecording,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        """
        Regression test for the Redis lambda closure bug.

        When two Redis messages are delivered before GLib processes any callbacks,
        we still expect both:
        - chat message sync, and
        - media request sync
        to be handled exactly once.
        """
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create test audio blob
        test_mp3_bytes = base64.b64decode("SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU2LjM2LjEwMAAAAAAAAAAAAAAA//OEAAAAAAAAAAAAAAAAAAAAAAAASW5mbwAAAA8AAAAEAAABIADAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV6urq6urq6urq6urq6urq6urq6urq6urq6v////////////////////////////////8AAAAATGF2YzU2LjQxAAAAAAAAAAAAAAAAJAAAAAAAAAAAASDs90hvAAAAAAAAAAAAAAAAAAAA//MUZAAAAAFkAAAAAAAAA0gAAAAATEFN//MUZAMAAAGkAAAAAAAAA0gAAAAARTMu//MUZAYAAAGkAAAAAAAAA0gAAAAAOTku//MUZAkAAAGkAAAAAAAAA0gAAAAANVVV")
        audio_blob = MediaBlob.get_or_create_from_blob(project=self.bot.project, blob=test_mp3_bytes, content_type="audio/mp3")

        # ---- GLib.idle_add patching setup ----
        deferred_callbacks = []
        pause_idle = False
        two_redis_callbacks_scheduled = threading.Event()

        def fake_idle_add(callback, *args, **kwargs):
            """
            Replacement for GLib.idle_add used in this test.

            - When pause_idle is True, we *buffer* callbacks instead of executing them.
            - When pause_idle is False, we immediately run the callback (simpler for test).
            """
            nonlocal deferred_callbacks, pause_idle
            if pause_idle:
                deferred_callbacks.append((callback, args, kwargs))
                # When redis_listener schedules two callbacks, this event will fire.
                if len(deferred_callbacks) >= 2:
                    two_redis_callbacks_scheduled.set()
            else:
                # For test purposes we just execute immediately.
                callback(*args)
            # GLib.idle_add normally returns an int source id; any int is fine here.
            return 1

        with patch("bots.bot_controller.bot_controller.GLib.idle_add", side_effect=fake_idle_add):
            # Create bot controller AFTER patching idle_add so run() uses this behavior.
            controller = BotController(self.bot.id)

            # Mock the attempt_to_join_meeting to succeed immediately
            with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
                mock_attempt_to_join.return_value = None  # Successful join

                # Run the bot in a separate thread since it has an event loop
                bot_thread = threading.Thread(target=controller.run)
                bot_thread.daemon = True
                bot_thread.start()

                # Wait for the bot to initialize and join
                time.sleep(2)

                # Spy on adapter methods
                with patch.object(controller.adapter, "send_chat_message") as mock_send_chat_message:
                    # Make the adapter ready to send chat messages and play audio
                    controller.adapter.ready_to_send_chat_messages = True
                    controller.adapter.ready_to_play_audio = True

                    # Simulate the adapter becoming ready (this will go through GLib.idle_add as well)
                    controller.adapter.send_message_callback({"message": controller.adapter.Messages.READY_TO_SEND_CHAT_MESSAGE})

                    # Let that READY message be processed immediately (pause_idle is False here)
                    time.sleep(0.5)

                    # Create chat message request
                    chat_message_request = BotChatMessageRequest.objects.create(
                        bot=self.bot,
                        message="Test message before audio",
                        to=BotChatMessageToOptions.EVERYONE,
                    )

                    # Immediately create audio media request
                    audio_request = BotMediaRequest.objects.create(
                        bot=self.bot,
                        media_blob=audio_blob,
                        media_type=BotMediaRequestMediaTypes.AUDIO,
                    )

                    # --- THIS IS THE CRITICAL WINDOW: pause GLib, then send both Redis messages ---

                    # Start buffering idle callbacks
                    pause_idle = True

                    # Send Redis commands back-to-back
                    send_sync_command(self.bot, "sync_chat_message_requests")
                    send_sync_command(self.bot, "sync_media_requests")

                    # Wait until we know two idle callbacks were scheduled
                    assert two_redis_callbacks_scheduled.wait(timeout=5), "Timed out waiting for two Redis idle callbacks to be scheduled"

                    # Now "resume" GLib: run the buffered callbacks
                    pause_idle = False
                    for cb, args, kwargs in deferred_callbacks:
                        cb(*args, **kwargs)

                    # Give a small bit of time for any follow-up logic
                    time.sleep(0.5)

                    # Refresh from DB
                    audio_request.refresh_from_db()
                    chat_message_request.refresh_from_db()

                    # ---- Assertions ----

                    # Audio request should not be stuck in ENQUEUED
                    self.assertNotEqual(
                        audio_request.state,
                        BotMediaRequestStates.ENQUEUED,
                        f"Audio request should not be ENQUEUED; got {audio_request.state}",
                    )

                    # Audio should be either PLAYING or FINISHED
                    self.assertIn(
                        audio_request.state,
                        [BotMediaRequestStates.PLAYING, BotMediaRequestStates.FINISHED],
                        f"Audio request should be PLAYING or FINISHED; got {audio_request.state}",
                    )

                    # Chat message should have been actually sent
                    self.assertGreater(
                        mock_send_chat_message.call_count,
                        0,
                        "send_chat_message should be called at least once",
                    )
                    self.assertEqual(
                        chat_message_request.state,
                        BotChatMessageRequestStates.SENT,
                        f"Chat message should be SENT; got {chat_message_request.state}",
                    )

                # Clean up: simulate meeting ending to trigger cleanup
                controller.adapter.left_meeting = True
                controller.adapter.send_message_callback({"message": controller.adapter.Messages.MEETING_ENDED})
                time.sleep(0.5)

                # Now wait for the thread to finish naturally
                bot_thread.join(timeout=2)

                if bot_thread.is_alive():
                    print("WARNING: Bot thread did not terminate properly after cleanup")

                # Close the database connection since we're in a thread
                connection.close()
