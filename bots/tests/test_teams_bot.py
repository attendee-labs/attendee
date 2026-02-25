import base64
import json
import threading
import time
from unittest.mock import MagicMock, mock_open, patch

from django.db import connection
from django.test import TransactionTestCase

from bots.bot_controller.bot_controller import BotController
from bots.bots_api_views import send_sync_command
from bots.models import Bot, BotChatMessageRequest, BotChatMessageRequestStates, BotChatMessageToOptions, BotEventManager, BotEventSubTypes, BotEventTypes, BotMediaRequest, BotMediaRequestMediaTypes, BotMediaRequestStates, BotStates, Credentials, MediaBlob, Organization, Participant, ParticipantEvent, ParticipantEventTypes, Project, Recording, RecordingStates, RecordingTypes, TranscriptionProviders, TranscriptionTypes, WebhookDeliveryAttempt, WebhookSecret, WebhookSubscription, WebhookTriggerTypes
from bots.teams_bot_adapter.teams_ui_methods import TeamsUIMethods, UiTeamsBlockingUsException
from bots.web_bot_adapter.ui_methods import UiLoginRequiredException


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

            # Mock the send_slack_alert task to verify it gets called
            with patch("bots.tasks.send_slack_alert_task.send_slack_alert.delay") as mock_send_slack_alert:
                # Mock the environment variable to enable Slack alerts
                with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"}):
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

                    # Verify that send_slack_alert task was enqueued
                    self.assertEqual(mock_send_slack_alert.call_count, 1, "send_slack_alert should be called once")
                    # Verify the message contains the bot object_id and error information
                    call_args = mock_send_slack_alert.call_args
                    message = call_args[0][0]
                    self.assertIn(self.bot.object_id, message, "Message should contain bot object_id")
                    self.assertIn("fatal error", message.lower(), "Message should mention fatal error")

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

    def _run_teams_signed_in_bot_with_only_if_required_mode(
        self,
        exception_to_raise,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        MockSaveDebugRecording,
    ):
        """Helper that tests the only_if_required login mode with a configurable exception.

        Args:
            exception_to_raise: The exception instance to raise on first join attempt
        """
        # Set up Teams bot login credentials
        teams_credentials = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN,
        )
        teams_credentials.set_credentials(
            {
                "username": "testbot@example.com",
                "password": "testpassword123",
            }
        )

        # Configure bot to use login with only_if_required mode
        self.bot.settings = {
            "teams_settings": {
                "use_login": True,
                "login_mode": "only_if_required",
            },
            "recording_settings": {"format": "none"},
        }
        self.bot.save()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Track calls to fill_out_name_input to control when login is required
        fill_out_name_input_call_count = [0]  # Use list to allow mutation in nested function

        def mock_fill_out_name_input(*args, **kwargs):
            """Mock that raises an exception only on first join attempt.

            When teams_bot_login_credentials are available and teams_bot_login_should_be_used is False,
            attempt_to_join_meeting() wraps this and converts ANY exception to UiLoginRequiredException.
            """
            fill_out_name_input_call_count[0] += 1

            # First join attempt: raise the configured exception
            if fill_out_name_input_call_count[0] <= 1:
                raise exception_to_raise

            # Second join attempt: succeed
            return None

        # Mock lower-level methods to allow actual attempt_to_join_meeting_implementation logic to run
        # but let login_to_microsoft_account be called (we mock it to track calls but not change behavior)
        with (
            patch.object(TeamsUIMethods, "fill_out_name_input", side_effect=mock_fill_out_name_input),
            patch.object(TeamsUIMethods, "turn_off_media_inputs", return_value=None),
            patch.object(TeamsUIMethods, "locate_element", return_value=MagicMock()),
            patch.object(TeamsUIMethods, "click_element", return_value=None),
            patch.object(TeamsUIMethods, "click_show_more_button", return_value=None),
            patch.object(TeamsUIMethods, "click_captions_button", return_value=None),
            patch.object(TeamsUIMethods, "set_layout", return_value=None),
            patch.object(TeamsUIMethods, "disable_incoming_video_in_ui", return_value=None),
            patch("bots.web_bot_adapter.web_bot_adapter.WebBotAdapter.ready_to_show_bot_image", return_value=None),
            patch.object(TeamsUIMethods, "login_to_microsoft_account", return_value=None) as mock_login,
        ):
            # Create bot controller
            controller = BotController(self.bot.id)

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            def simulate_join_flow():
                # Sleep to allow initialization and join attempts
                time.sleep(1)

                # Add participants to keep the bot in the meeting
                controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

                # Let the bot run for a bit to "record"
                time.sleep(1)

                # Trigger auto-leave
                controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
                time.sleep(1)

                # Clean up connections in thread
                connection.close()

            # Run join flow simulation after a short delay
            threading.Timer(2, simulate_join_flow).start()

            # Give the bot some time to process
            bot_thread.join(timeout=20)

            time.sleep(1.25)

            # Refresh the bot from the database
            self.bot.refresh_from_db()

            # Assert that the bot is in the ENDED state
            self.assertEqual(self.bot.state, BotStates.ENDED)

            # Verify that fill_out_name_input was called twice
            # First call fails (triggers login), second call succeeds
            self.assertEqual(fill_out_name_input_call_count[0], 2, "Expected fill_out_name_input to be called twice - once for the initial failure and once for the retry")

            # Verify that login was attempted (should be called once on the retry)
            self.assertEqual(mock_login.call_count, 1, "Expected login_to_microsoft_account to be called once during retry")

            # Verify that teams_bot_login_should_be_used was set to True after the first failed attempt
            self.assertTrue(controller.adapter.teams_bot_login_should_be_used, "Expected teams_bot_login_should_be_used to be True after retry")

            # Verify that teams_bot_login_credentials was available
            self.assertIsNotNone(controller.adapter.teams_bot_login_credentials, "Expected teams_bot_login_credentials to be set")

            # Verify that the recording was finished
            self.recording.refresh_from_db()
            self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

            # Cleanup
            controller.cleanup()
            bot_thread.join(timeout=5)

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.bot_controller.bot_controller.BotController.save_debug_recording", return_value=None)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_teams_signed_in_bot_with_only_if_required_mode(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        MockSaveDebugRecording,
    ):
        """Test that a bot with login_mode='only_if_required' first tries without login,
        then retries with login when meeting requires sign in (UiLoginRequiredException).

        This test exercises the actual retry logic in repeatedly_attempt_to_join_meeting(),
        attempt_to_join_meeting(), and look_for_sign_in_required_element() by mocking at a low level
        (look_for_sign_in_required_element raises exception on first attempt only).

        Flow:
        1. First join attempt: look_for_sign_in_required_element raises UiLoginRequiredException
        2. Exception caught in repeatedly_attempt_to_join_meeting
        3. should_retry_joining_meeting_that_requires_login_by_logging_in() returns True
        4. teams_bot_login_should_be_used flag is set to True
        5. Second join attempt: login_to_microsoft_account is called, join succeeds
        """
        self._run_teams_signed_in_bot_with_only_if_required_mode(
            exception_to_raise=UiLoginRequiredException("Sign in required", "mock_fill_out_name_input"),
            MockFileUploader=MockFileUploader,
            MockChromeDriver=MockChromeDriver,
            MockDisplay=MockDisplay,
            MockSaveDebugRecording=MockSaveDebugRecording,
        )

    @patch.dict("os.environ", {"ENFORCE_DOMAIN_ALLOWLIST_IN_CHROME": "true"})
    @patch("bots.web_bot_adapter.web_bot_adapter.settings.ENFORCE_DOMAIN_ALLOWLIST_IN_CHROME", True)
    @patch("bots.teams_bot_adapter.teams_bot_adapter.settings.ENFORCE_DOMAIN_ALLOWLIST_IN_CHROME", True)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_domain_allow_list_violation_raises_error(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        """Test that navigating to a URL not in the allow list raises an exception.

        When Chrome's BrowserSwitcher policy blocks a URL, it redirects to
        chrome://browser-switch?url=<blocked_url>. The check_domain_allow_list_violation()
        method detects this in the navigation history and raises an exception.

        Also verifies that the Chrome policy file would be written with the correct
        BrowserSwitcher configuration for Teams.
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

        # Create bot controller
        controller = BotController(self.bot.id)

        # Mock the attempt_to_join_meeting to succeed immediately
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.return_value = None  # Successful join

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Wait for the bot to join and adapter to be created
            time.sleep(3)

            # --- Verify the Chrome policy file would be written correctly ---
            # Mock os.path.islink to return True so policy file writing code runs
            # and mock open() to capture what would be written without touching filesystem
            m = mock_open()
            with patch("bots.web_bot_adapter.web_bot_adapter.os.path.islink", return_value=True):
                with patch("builtins.open", m):
                    controller.adapter.write_chrome_policies_file()

            # Verify open was called with the correct path
            m.assert_called_once_with("/tmp/attendee-chrome-policies.json", "w")

            # Get the data that would have been written via json.dump
            # json.dump calls write() on the file handle, so we get what was written
            write_calls = m().write.call_args_list
            written_data = "".join(call[0][0] for call in write_calls)
            policy = json.loads(written_data)

            # Verify the BrowserSwitcher policy is correctly configured
            self.assertTrue(policy.get("BrowserSwitcherEnabled"), "BrowserSwitcherEnabled should be True")
            self.assertEqual(policy.get("AlternativeBrowserPath"), "/nonexistent-browser")

            # Verify the URL allow list contains the expected domains
            url_list = policy.get("BrowserSwitcherUrlList", [])
            self.assertIn("*", url_list, "URL list should block all URLs by default")
            self.assertIn("!microsoft.com", url_list, "microsoft.com should be allowed")
            self.assertIn("!office.com", url_list, "office.com should be allowed")
            self.assertIn("!cloud.microsoft", url_list, "cloud.microsoft should be allowed")
            self.assertIn("!microsoftonline.com", url_list, "microsoftonline.com should be allowed")
            self.assertIn("!live.com", url_list, "live.com should be allowed")

            # --- Now test the domain allow list violation detection ---

            # Simulate the bot having joined and being in the meeting
            controller.adapter.joined_at = time.time()

            # Mock the navigation history to include a disallowed URL
            blocked_url = "chrome://browser-switch/?url=https%3A%2F%2Fbadmicrosoft.com"
            mock_driver.execute_cdp_cmd.return_value = {
                "entries": [
                    {"url": "https://teams.microsoft.com/meet/123"},
                    {"url": blocked_url},
                ]
            }

            # Reset the last check time so the check runs immediately
            controller.adapter.last_domain_allow_list_violation_check_time = 0

            # Verify that check_domain_allow_list_violation raises an exception
            with self.assertRaises(Exception) as context:
                controller.adapter.check_domain_allow_list_violation()

            # Verify the exception message contains the blocked domain
            self.assertIn("Domain allow list violation detected", str(context.exception))
            self.assertIn("badmicrosoft.com", str(context.exception))

            # Clean up: simulate meeting ending to trigger cleanup
            controller.adapter.left_meeting = True
            controller.adapter.send_message_callback({"message": controller.adapter.Messages.MEETING_ENDED})
            time.sleep(1)

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

    @patch.dict("os.environ", {"USE_REMOTE_STORAGE_FOR_AUDIO_CHUNKS": "true", "FALLBACK_TO_DB_STORAGE_FOR_AUDIO_CHUNKS_IF_REMOTE_STORAGE_FAILS": "true"})
    @patch("bots.bot_controller.bot_controller.settings.USE_REMOTE_STORAGE_FOR_AUDIO_CHUNKS", True)
    @patch("bots.bot_controller.bot_controller.settings.FALLBACK_TO_DB_STORAGE_FOR_AUDIO_CHUNKS_IF_REMOTE_STORAGE_FAILS", True)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_audio_chunk_remote_storage_with_fallback_to_db(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
    ):
        """
        Test that audio chunks can be uploaded to remote storage, and if an upload fails,
        the system falls back to DB storage when FALLBACK_TO_DB_STORAGE_FOR_AUDIO_CHUNKS_IF_REMOTE_STORAGE_FAILS is enabled.

        Flow:
        1. Bot joins meeting with remote audio chunk storage enabled
        2. First audio chunk upload fails (simulated error)
        3. System falls back to storing audio in database
        4. Utterance is still created and processed successfully
        5. Second audio chunk upload succeeds
        6. Verify both chunks result in processed utterances
        """
        from bots.models import AudioChunk, Utterance

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

        # Track upload attempts
        upload_attempt_count = [0]

        def mock_upload_one(filename: str, data: bytes) -> str:
            """Mock upload that fails on first attempt, succeeds on second."""
            upload_attempt_count[0] += 1
            if upload_attempt_count[0] == 1:
                # First upload fails
                raise Exception("Simulated S3 upload failure")
            # Subsequent uploads succeed
            return filename

        # Mock the attempt_to_join_meeting to succeed immediately
        with patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.return_value = None  # Successful join

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Wait for the bot to join
            time.sleep(2)

            # Mock the audio chunk uploader's _upload_one method
            with patch.object(controller.audio_chunk_uploader, "_upload_one", side_effect=mock_upload_one):
                # Simulate two audio chunks being received
                test_audio_data_1 = b"\x00\x01\x02\x03" * 1000  # 4KB of test audio data
                test_audio_data_2 = b"\x04\x05\x06\x07" * 1000  # Different test audio data

                # Send first audio chunk (this will fail to upload)
                # The process_individual_audio_chunk will create the participant automatically
                controller.process_individual_audio_chunk(
                    {
                        "audio_data": test_audio_data_1,
                        "timestamp_ms": 1000,
                        "duration_ms": 500,
                        "sample_rate": 16000,
                        "participant_uuid": "test_device_123",
                        "participant_user_uuid": None,
                        "participant_full_name": "Test Participant",
                        "participant_is_the_bot": False,
                        "participant_is_host": False,
                    }
                )

                # Give time for the upload to be queued and fail
                time.sleep(0.5)

                # Process uploads to trigger the failure callback
                controller.audio_chunk_uploader.process_uploads()

                # Wait for fallback processing
                time.sleep(0.5)

                # Get the participant that was created by process_individual_audio_chunk
                participant = Participant.objects.get(bot=self.bot, uuid="test_device_123")

                # Verify first audio chunk fallback to DB
                first_audio_chunk = AudioChunk.objects.filter(participant=participant).first()
                self.assertIsNotNone(first_audio_chunk, "First audio chunk should be created")
                self.assertFalse(first_audio_chunk.is_blob_stored_remotely, "First chunk should not be stored remotely")
                self.assertIsNotNone(first_audio_chunk.blob_upload_failure_data, "First chunk should have failure data")
                self.assertEqual(first_audio_chunk.blob_upload_failure_data.get("exception_type"), "Exception")
                self.assertIn("Simulated S3 upload failure", first_audio_chunk.blob_upload_failure_data.get("error"))
                self.assertEqual(bytes(first_audio_chunk.audio_blob), test_audio_data_1, "First chunk audio should be stored in DB")

                # Verify that an utterance was created for the first chunk despite the upload failure
                first_utterance = Utterance.objects.filter(audio_chunk=first_audio_chunk).first()
                self.assertIsNotNone(first_utterance, "Utterance should be created for first chunk despite upload failure")
                self.assertEqual(first_utterance.participant, participant)

                # Send second audio chunk (this will succeed)
                controller.process_individual_audio_chunk(
                    {
                        "audio_data": test_audio_data_2,
                        "timestamp_ms": 2000,
                        "duration_ms": 500,
                        "sample_rate": 16000,
                        "participant_uuid": "test_device_123",
                        "participant_user_uuid": None,
                        "participant_full_name": "Test Participant",
                        "participant_is_the_bot": False,
                        "participant_is_host": False,
                    }
                )

                # Give time for the upload to complete
                time.sleep(0.5)

                # Process uploads to trigger the success callback
                controller.audio_chunk_uploader.process_uploads()

                # Wait for processing
                time.sleep(0.5)

                # Verify second audio chunk was uploaded successfully
                second_audio_chunk = AudioChunk.objects.filter(participant=participant).order_by("created_at").last()
                self.assertIsNotNone(second_audio_chunk, "Second audio chunk should be created")
                self.assertTrue(second_audio_chunk.is_blob_stored_remotely, "Second chunk should be stored remotely")
                self.assertIsNone(second_audio_chunk.blob_upload_failure_data, "Second chunk should not have failure data")
                self.assertTrue(second_audio_chunk.audio_blob_remote_file, "Second chunk should have remote file reference")

                # Verify that an utterance was created for the second chunk
                second_utterance = Utterance.objects.filter(audio_chunk=second_audio_chunk).first()
                self.assertIsNotNone(second_utterance, "Utterance should be created for second chunk")
                self.assertEqual(second_utterance.participant, participant)

                # Verify that both uploads were attempted
                self.assertEqual(upload_attempt_count[0], 2, "Should have attempted two uploads")

                # Verify total audio chunks and utterances
                total_chunks = AudioChunk.objects.filter(participant=participant).count()
                self.assertEqual(total_chunks, 2, "Should have exactly two audio chunks")

                total_utterances = Utterance.objects.filter(participant=participant).count()
                self.assertEqual(total_utterances, 2, "Should have exactly two utterances")

            # Clean up: simulate meeting ending to trigger cleanup
            controller.adapter.left_meeting = True
            controller.adapter.send_message_callback({"message": controller.adapter.Messages.MEETING_ENDED})
            time.sleep(1)

            # Now wait for the thread to finish naturally
            bot_thread.join(timeout=5)

            # If thread is still running after timeout, that's a problem to report
            if bot_thread.is_alive():
                print("WARNING: Bot thread did not terminate properly after cleanup")

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.teams_bot_adapter.teams_ui_methods.TeamsUIMethods.attempt_to_join_meeting", return_value=None)
    @patch("time.time")
    @patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_bot_sends_screenshare_start_stop_participant_event_webhooks(
        self,
        mock_deliver_webhook,
        mock_time,
        mock_attempt_to_join,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        mock_deliver_webhook.return_value = None

        WebhookSecret.objects.create(project=self.project)
        WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[
                WebhookTriggerTypes.BOT_STATE_CHANGE,
                WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE,
                WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP,
            ],
            is_active=True,
        )

        current_time = 1000.0
        mock_time.return_value = current_time

        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        mock_driver = create_mock_teams_driver()
        MockChromeDriver.return_value = mock_driver

        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        controller = BotController(self.bot.id)

        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            nonlocal current_time

            # Simulate participants joining
            bot_participant_data = {"deviceId": "bot1", "fullName": "Test Teams Bot", "active": True, "isCurrentUser": True}
            controller.adapter.handle_participant_update(bot_participant_data)

            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

            controller.adapter.last_audio_message_processed_time = current_time

            time.sleep(3)

            # Simulate screenshare start event for the user participant
            controller.adapter.handle_screenshare_start_stop_event(
                {
                    "participantId": "user1",
                    "isScreenshareStart": True,
                    "timestamp": int(current_time * 1000),
                }
            )

            time.sleep(0.5)

            # Simulate screenshare stop event for the user participant
            controller.adapter.handle_screenshare_start_stop_event(
                {
                    "participantId": "user1",
                    "isScreenshareStart": False,
                    "timestamp": int(current_time * 1000) + 30000,
                }
            )

            # Also simulate a screenshare event for the bot (should NOT produce a webhook)
            controller.adapter.handle_screenshare_start_stop_event(
                {
                    "participantId": "bot1",
                    "isScreenshareStart": True,
                    "timestamp": int(current_time * 1000) + 31000,
                }
            )

            time.sleep(1)

            # Simulate participant leaving
            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": False, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

            # Trigger auto-leave
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(4)

            connection.close()

        threading.Timer(2, simulate_join_flow).start()

        bot_thread.join(timeout=15)

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify ParticipantEvent records for screenshare start/stop were created for the user
        user_participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="user1")
        screenshare_start_event = user_participant_events.filter(event_type=ParticipantEventTypes.SCREENSHARE_START).first()
        self.assertIsNotNone(screenshare_start_event, "Expected a SCREENSHARE_START participant event for the user")
        self.assertEqual(screenshare_start_event.participant.full_name, "Test User")
        self.assertEqual(screenshare_start_event.timestamp_ms, int(current_time * 1000))

        screenshare_stop_event = user_participant_events.filter(event_type=ParticipantEventTypes.SCREENSHARE_STOP).first()
        self.assertIsNotNone(screenshare_stop_event, "Expected a SCREENSHARE_STOP participant event for the user")
        self.assertEqual(screenshare_stop_event.participant.full_name, "Test User")
        self.assertEqual(screenshare_stop_event.timestamp_ms, int(current_time * 1000) + 30000)

        # Verify that a SCREENSHARE_START event was also created for the bot participant
        bot_screenshare_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="bot1", event_type=ParticipantEventTypes.SCREENSHARE_START)
        self.assertEqual(bot_screenshare_events.count(), 1, "Expected a SCREENSHARE_START event for the bot participant")

        # Verify webhook delivery attempts for screenshare start/stop
        screenshare_webhook_attempts = WebhookDeliveryAttempt.objects.filter(
            bot=self.bot,
            webhook_trigger_type=WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP,
        )
        # Only user events should trigger webhooks (bot events are suppressed)
        self.assertEqual(screenshare_webhook_attempts.count(), 2, "Expected exactly 2 screenshare webhook delivery attempts (start and stop for the user)")

        screenshare_start_webhook = screenshare_webhook_attempts.filter(payload__event_type="screenshare_start").first()
        self.assertIsNotNone(screenshare_start_webhook)
        self.assertEqual(screenshare_start_webhook.payload["participant_name"], "Test User")
        self.assertEqual(screenshare_start_webhook.payload["participant_uuid"], "user1")
        self.assertEqual(screenshare_start_webhook.payload["timestamp_ms"], int(current_time * 1000))

        screenshare_stop_webhook = screenshare_webhook_attempts.filter(payload__event_type="screenshare_stop").first()
        self.assertIsNotNone(screenshare_stop_webhook)
        self.assertEqual(screenshare_stop_webhook.payload["participant_name"], "Test User")
        self.assertEqual(screenshare_stop_webhook.payload["participant_uuid"], "user1")
        self.assertEqual(screenshare_stop_webhook.payload["timestamp_ms"], int(current_time * 1000) + 30000)

        # Verify that no screenshare webhook was created for the bot participant
        bot_screenshare_webhooks = screenshare_webhook_attempts.filter(payload__participant_uuid="bot1")
        self.assertEqual(bot_screenshare_webhooks.count(), 0, "Expected no screenshare webhooks for the bot participant")

        # Verify join/leave webhooks were also created (ensuring screenshare events don't interfere)
        join_leave_webhook_attempts = WebhookDeliveryAttempt.objects.filter(
            bot=self.bot,
            webhook_trigger_type=WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE,
        )
        self.assertGreater(join_leave_webhook_attempts.count(), 0, "Expected join/leave webhook delivery attempts")

        controller.cleanup()
        bot_thread.join(timeout=5)

        connection.close()
