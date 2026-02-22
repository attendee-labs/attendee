import os
import threading
import time
from unittest.mock import MagicMock, patch

import requests
from django.db import connection
from django.test import TransactionTestCase
from selenium.common.exceptions import NoSuchElementException

from bots.bot_adapter import BotAdapter
from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotStates, Credentials, Organization, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes, WebhookDeliveryAttempt, WebhookSubscription, WebhookTriggerTypes, ZoomMeetingToZoomOAuthConnectionMapping, ZoomOAuthApp, ZoomOAuthConnection, ZoomOAuthConnectionStates


# Helper functions for creating mocks
def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.filename = "test-recording-key"
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

        # Recreate zoom oauth app
        self.zoom_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        self.zoom_credentials.set_credentials({"client_id": "123", "client_secret": "test_client_secret"})

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
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
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
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
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

    @patch("bots.zoom_oauth_connections_utils.requests.post")
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_zoom_oauth_app_token_failure_with_local_recording_token(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_requests_post,
    ):
        """Test that when OAuth token retrieval fails, the connection status is updated and webhook is sent"""
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE],
            is_active=True,
        )

        # Create ZoomOAuthApp
        zoom_oauth_app = ZoomOAuthApp.objects.create(
            project=self.project,
            client_id="test_client_id",
        )
        zoom_oauth_app.set_credentials(
            {
                "client_secret": "test_client_secret",
                "webhook_secret": "test_webhook_secret",
            }
        )

        # Create ZoomOAuthConnection
        zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
            state=ZoomOAuthConnectionStates.CONNECTED,
        )
        zoom_oauth_connection.set_credentials(
            {
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
            }
        )

        # Create mapping for the meeting
        meeting_id = "123123213"
        self.bot.meeting_url = f"https://zoom.us/j/{meeting_id}"
        self.bot.save()

        ZoomMeetingToZoomOAuthConnectionMapping.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            zoom_oauth_connection=zoom_oauth_connection,
            meeting_id=meeting_id,
        )

        # Mock the token refresh response to fail with authentication error
        mock_token_response = MagicMock()
        mock_token_response.status_code = 401
        mock_token_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Invalid grant",
        }

        # Create a proper HTTPError with a response
        http_error = requests.HTTPError("401 Unauthorized")
        http_error.response = mock_token_response
        mock_token_response.raise_for_status.side_effect = http_error
        mock_requests_post.return_value = mock_token_response

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot some time to attempt token retrieval
        time.sleep(2)

        # Verify that the ZoomOAuthConnection state was updated to DISCONNECTED
        zoom_oauth_connection.refresh_from_db()
        self.assertEqual(zoom_oauth_connection.state, ZoomOAuthConnectionStates.DISCONNECTED, "ZoomOAuthConnection should be DISCONNECTED after authentication failure")

        # Verify that connection_failure_data was set
        self.assertIsNotNone(zoom_oauth_connection.connection_failure_data)
        self.assertIn("error", zoom_oauth_connection.connection_failure_data)

        # Verify that a webhook was triggered for the connection state change
        webhook_attempts = WebhookDeliveryAttempt.objects.filter(
            webhook_trigger_type=WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE,
            zoom_oauth_connection=zoom_oauth_connection,
        )
        self.assertTrue(webhook_attempts.exists(), "A webhook should be triggered for ZoomOAuthConnection state change")

        # Verify the webhook payload contains the connection info
        webhook_attempt = webhook_attempts.first()
        self.assertEqual(webhook_attempt.payload["state"], "disconnected")
        self.assertIsNotNone(webhook_attempt.payload["connection_failure_data"])

        # Close the database connection since we're in a thread
        connection.close()

    def test_blocked_by_captcha_records_specific_event_subtype(self):
        """
        If the adapter reports a captcha/verification gate (Zoom Web SDK "Check Captcha" loop),
        we should record a dedicated COULD_NOT_JOIN sub-type so it's visible in analytics.
        """
        controller = BotController(self.bot.id)

        # Prevent expensive cleanup behavior (uploads, process-kill watchdog, etc.)
        controller.cleanup = MagicMock()

        controller.take_action_based_on_message_from_adapter(
            {
                "message": BotAdapter.Messages.BLOCKED_BY_CAPTCHA,
            }
        )

        self.bot.refresh_from_db()
        last_event = self.bot.bot_events.order_by("-created_at").first()
        self.assertIsNotNone(last_event, "Expected a bot event to be created")
        self.assertEqual(last_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(last_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_BLOCKED_BY_CAPTCHA)

    @patch("bots.zoom_oauth_connections_utils.requests.post")
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_zoom_oauth_app_token_failure_with_onbehalf_token(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_requests_post,
    ):
        """Test that when OAuth token retrieval fails, the connection status is updated and webhook is sent"""
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE],
            is_active=True,
        )

        # Create ZoomOAuthApp
        zoom_oauth_app = ZoomOAuthApp.objects.create(
            project=self.project,
            client_id="test_client_id",
        )
        zoom_oauth_app.set_credentials(
            {
                "client_secret": "test_client_secret",
                "webhook_secret": "test_webhook_secret",
            }
        )

        # Create ZoomOAuthConnection
        zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )
        zoom_oauth_connection.set_credentials(
            {
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
            }
        )

        # Create mapping for the meeting
        meeting_id = "123123213"
        self.bot.meeting_url = f"https://zoom.us/j/{meeting_id}"
        self.bot.settings = {
            "zoom_settings": {
                "sdk": "web",
                "onbehalf_token": {
                    "zoom_oauth_connection_user_id": "test_user_id",
                },
            },
        }
        self.bot.save()

        # Mock the token refresh response to fail with authentication error
        mock_token_response = MagicMock()
        mock_token_response.status_code = 401
        mock_token_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Invalid grant",
        }

        # Create a proper HTTPError with a response
        http_error = requests.HTTPError("401 Unauthorized")
        http_error.response = mock_token_response
        mock_token_response.raise_for_status.side_effect = http_error
        mock_requests_post.return_value = mock_token_response

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot some time to attempt token retrieval
        time.sleep(2)

        # Verify that the ZoomOAuthConnection state was updated to DISCONNECTED
        zoom_oauth_connection.refresh_from_db()
        self.assertEqual(zoom_oauth_connection.state, ZoomOAuthConnectionStates.DISCONNECTED, "ZoomOAuthConnection should be DISCONNECTED after authentication failure")

        # Verify that connection_failure_data was set
        self.assertIsNotNone(zoom_oauth_connection.connection_failure_data)
        self.assertIn("error", zoom_oauth_connection.connection_failure_data)

        # Verify that a webhook was triggered for the connection state change
        webhook_attempts = WebhookDeliveryAttempt.objects.filter(
            webhook_trigger_type=WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE,
            zoom_oauth_connection=zoom_oauth_connection,
        )
        self.assertTrue(webhook_attempts.exists(), "A webhook should be triggered for ZoomOAuthConnection state change")

        # Verify the webhook payload contains the connection info
        webhook_attempt = webhook_attempts.first()
        self.assertEqual(webhook_attempt.payload["state"], "disconnected")
        self.assertIsNotNone(webhook_attempt.payload["connection_failure_data"])

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.start_zoom_web_static_server", return_value=8080)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_waiting_room_timeout(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_start_static_server,
    ):
        """Test that bot times out if waiting room timeout is exceeded."""
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        self.bot.settings = {
            "zoom_settings": {
                "sdk": "web",
            },
            "automatic_leave_settings": {
                "waiting_room_timeout_seconds": 3,
            },
        }
        self.bot.save()

        # Mock execute_script to handle different script calls
        def execute_script_side_effect(script, *args):
            if "userHasEnteredMeeting" in script:
                return False  # User has NOT entered the meeting
            if "userHasEncounteredOnBehalfTokenUserNotInMeetingError" in script:
                return False  # No onbehalf token error
            return None

        mock_driver.execute_script.side_effect = execute_script_side_effect

        # Mock find_element to not find the "host to start meeting" text (so we're in waiting room, not waiting for host)
        mock_driver.find_element.side_effect = NoSuchElementException("Element not found")

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the bot is in the FATAL_ERROR state after timeout
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()

        # Should have at least 2 events: JOIN_REQUESTED and COULD_NOT_JOIN
        self.assertGreaterEqual(len(bot_events), 2)

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Find the COULD_NOT_JOIN event
        could_not_join_events = [e for e in bot_events if e.event_type == BotEventTypes.COULD_NOT_JOIN]
        self.assertGreaterEqual(len(could_not_join_events), 1)

        # Verify the event has the correct subtype
        could_not_join_event = could_not_join_events[0]
        self.assertEqual(could_not_join_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.start_zoom_web_static_server", return_value=8080)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_authorized_user_not_in_meeting_timeout(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_start_static_server,
    ):
        """Test that bot times out if authorized user (onbehalf token user) is not in the meeting."""
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        self.bot.settings = {
            "zoom_settings": {
                "sdk": "web",
            },
            "automatic_leave_settings": {
                "authorized_user_not_in_meeting_timeout_seconds": 3,
            },
        }
        self.bot.save()

        # Mock execute_script to handle different script calls
        def execute_script_side_effect(script, *args):
            if "userHasEnteredMeeting" in script:
                return False  # User has NOT entered the meeting
            if "userHasEncounteredOnBehalfTokenUserNotInMeetingError" in script:
                return True  # The onbehalf token user is NOT in the meeting
            return None

        mock_driver.execute_script.side_effect = execute_script_side_effect

        # Mock find_element to not find the "host to start meeting" text
        mock_driver.find_element.side_effect = NoSuchElementException("Element not found")

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot time to process
        bot_thread.join(timeout=15)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the bot is in the FATAL_ERROR state after timeout
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()

        # Should have at least 2 events: JOIN_REQUESTED and COULD_NOT_JOIN
        self.assertGreaterEqual(len(bot_events), 2)

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Find the COULD_NOT_JOIN event
        could_not_join_events = [e for e in bot_events if e.event_type == BotEventTypes.COULD_NOT_JOIN]
        self.assertGreaterEqual(len(could_not_join_events), 1)

        # Verify the event has the correct subtype
        could_not_join_event = could_not_join_events[0]
        self.assertEqual(could_not_join_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_AUTHORIZED_USER_NOT_IN_MEETING_TIMEOUT_EXCEEDED)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.WebDriverWait")
    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.start_zoom_web_static_server", return_value=8080)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_transition_from_waiting_for_host_to_waiting_room_then_admitted(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_start_static_server,
        MockWebDriverWait,
    ):
        """Test that bot successfully joins after transitioning from waiting for host to waiting room."""
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Mock WebDriverWait to return mock elements for post-admission UI
        mock_wait_instance = MagicMock()
        mock_element = MagicMock()
        mock_element.is_displayed.return_value = True
        mock_wait_instance.until.return_value = mock_element
        MockWebDriverWait.return_value = mock_wait_instance

        # Track the state transitions
        call_count = [0]
        # Phase 0: waiting for host (calls 0-2)
        # Phase 1: waiting room (calls 3-5)
        # Phase 2: admitted (call 6+)

        # Mock execute_script to handle different script calls
        def execute_script_side_effect(script, *args):
            if "userHasEnteredMeeting" in script:
                call_count[0] += 1
                # After 6 calls, user has entered the meeting
                if call_count[0] >= 6:
                    return True
                return False
            if "userHasEncounteredOnBehalfTokenUserNotInMeetingError" in script:
                return False
            if "joinMeeting" in script:
                return None
            # For clicking elements
            return None

        mock_driver.execute_script.side_effect = execute_script_side_effect

        # Mock find_element to simulate phase transitions
        find_element_call_count = [0]

        def find_element_side_effect(by, value):
            find_element_call_count[0] += 1
            if "host to start the meeting" in value:
                # First 2 calls: waiting for host (element found)
                # After that: waiting room (element not found)
                if find_element_call_count[0] <= 2:
                    mock_host_element = MagicMock()
                    mock_host_element.is_displayed.return_value = True
                    return mock_host_element
                else:
                    raise NoSuchElementException("Element not found")
            raise NoSuchElementException("Element not found")

        mock_driver.find_element.side_effect = find_element_side_effect

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Allow time for the join process
        time.sleep(10)

        # Verify the bot successfully joined (should be in a joined state, not FATAL_ERROR)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.JOINED_NOT_RECORDING)

        # Verify bot events include a successful join
        bot_events = self.bot.bot_events.all()

        # Verify join_requested_event
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify that a BOT_JOINED_MEETING event was created
        joined_events = [e for e in bot_events if e.event_type == BotEventTypes.BOT_JOINED_MEETING]
        self.assertGreaterEqual(len(joined_events), 1, "A BOT_JOINED_MEETING event should be created")

        # Simulate meeting ending to trigger cleanup
        controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
        time.sleep(4)

        # Now wait for the thread to finish naturally
        bot_thread.join(timeout=5)

        # If thread is still running after timeout, that's a problem to report
        if bot_thread.is_alive():
            print("WARNING: Bot thread did not terminate properly after cleanup")

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.WebDriverWait")
    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.start_zoom_web_static_server", return_value=8080)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("time.time")
    def test_bot_auto_leaves_only_participant_with_bot_keywords(
        self,
        mock_time,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_start_static_server,
        MockWebDriverWait,
    ):
        """
        Test that the bot auto-leaves when only another bot (identified by bot_keywords) is in the meeting.
        1. Real participant joins
        2. Another bot (name matching bot_keywords) joins
        3. Real participant leaves (timer should start, because the other bot is excluded)
        4. Wait 8+ seconds - bot should leave (even though the other bot is still in the meeting)
        """
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Mock WebDriverWait to return mock elements for post-admission UI
        mock_wait_instance = MagicMock()
        mock_element = MagicMock()
        mock_element.is_displayed.return_value = True
        mock_wait_instance.until.return_value = mock_element
        MockWebDriverWait.return_value = mock_wait_instance

        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Configure the bot with bot_keywords
        self.bot.name = "Recording Bot"
        self.bot.settings = {
            "zoom_settings": {
                "sdk": "web",
            },
            "automatic_leave_settings": {
                "only_participant_in_meeting_timeout_seconds": 8,
                "silence_timeout_seconds": 999999,  # Set very high so it doesn't interfere
                "silence_activate_after_seconds": 999999,  # Set very high so it doesn't interfere
                "bot_keywords": ["Notetaker", "Recording Bot"],  # Keywords to identify other bots
            },
        }
        self.bot.save()

        # Track the state for the mock
        call_count = [0]

        # Mock execute_script to handle different script calls
        def execute_script_side_effect(script, *args):
            if "userHasEnteredMeeting" in script:
                call_count[0] += 1
                # After 2 calls, user has entered the meeting
                if call_count[0] >= 2:
                    return True
                return False
            if "userHasEncounteredOnBehalfTokenUserNotInMeetingError" in script:
                return False
            if "joinMeeting" in script:
                return None
            return None

        mock_driver.execute_script.side_effect = execute_script_side_effect

        # Mock find_element to not find the "host to start meeting" text
        mock_driver.find_element.side_effect = NoSuchElementException("Element not found")

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Allow time for the join process
        time.sleep(5)

        # Verify bot joined
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.JOINED_NOT_RECORDING)

        adapter = controller.adapter

        # Simulate participants joining via handle_participant_update
        # The bot itself (device_id = "bot_device")
        bot_participant = {
            "deviceId": "bot_device",
            "fullName": "Recording Bot",
            "isCurrentUser": True,
            "active": True,
            "humanized_status": "in_meeting",
            "isHost": False,
        }
        adapter.handle_participant_update(bot_participant)
        adapter.update_only_one_participant_in_meeting_at()

        # Step 1: Real participant joins Notetakerz should NOT be counted as a bot
        real_participant = {
            "deviceId": "real_user_device",
            "fullName": "Notetakerz",
            "isCurrentUser": False,
            "active": True,
            "humanized_status": "in_meeting",
            "isHost": True,
        }
        adapter.handle_participant_update(real_participant)
        adapter.update_only_one_participant_in_meeting_at()

        # Verify only_one_participant_in_meeting_at is None (real participant is there)
        self.assertIsNone(adapter.only_one_participant_in_meeting_at, "Timer should not start yet")

        # Step 2: Another bot joins (Notetaker - matches bot_keywords)
        notetaker_participant = {
            "deviceId": "notetaker_device",
            "fullName": "NoteTaker",
            "isCurrentUser": False,
            "active": True,
            "humanized_status": "in_meeting",
            "isHost": False,
        }
        adapter.handle_participant_update(notetaker_participant)
        adapter.update_only_one_participant_in_meeting_at()

        # Verify only_one_participant_in_meeting_at is still None (real participant is there)
        self.assertIsNone(adapter.only_one_participant_in_meeting_at, "Timer should not start yet (real participant still there)")
        # Verify that number of participants ever in meeting excluding other bots is 2
        self.assertEqual(adapter.number_of_participants_ever_in_meeting_excluding_other_bots(), 2)

        # Step 3: Real participant leaves (only our bot and the Notetaker remain)
        real_participant_leaving = {
            "deviceId": "real_user_device",
            "fullName": "Notetakerz",
            "isCurrentUser": False,
            "active": False,
            "humanized_status": "left_meeting",
            "isHost": True,
        }
        adapter.handle_participant_update(real_participant_leaving)
        adapter.update_only_one_participant_in_meeting_at()

        # Verify only_one_participant_in_meeting_at is now set
        # (because the Notetaker is excluded from count due to bot_keywords)
        self.assertIsNotNone(adapter.only_one_participant_in_meeting_at, "Timer should start (Notetaker excluded by bot_keywords)")

        # Step 4: Advance time past the timeout
        current_time += 10  # 10 seconds, past the 8 second threshold
        mock_time.return_value = current_time

        # Set only_one_participant_in_meeting_at to a time in the past to trigger auto-leave
        adapter.only_one_participant_in_meeting_at = current_time - 10

        # Give the bot time to process auto-leave
        time.sleep(5)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Verify that the bot auto-left due to being only participant
        # (the Notetaker bot was excluded from the count)
        bot_events = self.bot.bot_events.all()
        auto_leave_events = [event for event in bot_events if event.event_sub_type == BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING]
        self.assertEqual(len(auto_leave_events), 1, "Expected exactly one auto-leave event")

        # Clean up
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch.dict(os.environ, {"ZOOM_WEB_GENERIC_JOIN_ERROR_SLEEP_TIME_SECONDS": "0.5"})
    @patch("bots.zoom_web_bot_adapter.zoom_web_ui_methods.start_zoom_web_static_server", return_value=8080)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    def test_generic_join_error_retries_then_stops(
        self,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_start_static_server,
    ):
        """
        Test that generic join error retries up to max count, then eventually stops.

        The handle_generic_join_error method in ZoomWebBotAdapter:
        - If generic_join_error_retries < max_retries: raises UiZoomWebGenericJoinErrorException (causes retry)
        - After max retries exceeded: stops retrying and sends ZOOM_MEETING_STATUS_FAILED message

        This test verifies that:
        1. The bot retries when encountering generic join error within the retry limit
        2. After max retries are exhausted, the bot stops retrying and enters FATAL_ERROR state

        Environment variables set for fast test execution:
        - ZOOM_WEB_GENERIC_JOIN_ERROR_SLEEP_TIME_SECONDS=0.5 (instead of default 5)
        """
        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_zoom_web_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Track how many times the generic join error check was called
        generic_join_error_check_count = [0]

        # Mock execute_script to handle different script calls
        def execute_script_side_effect(script, *args):
            if "userHasEnteredMeeting" in script:
                return False  # User has NOT entered the meeting
            if "userHasEncounteredOnBehalfTokenUserNotInMeetingError" in script:
                return False  # No onbehalf token error
            if "userHasEncounteredGenericJoinError" in script:
                generic_join_error_check_count[0] += 1
                return True  # Always return true to simulate persistent generic join error
            return None

        mock_driver.execute_script.side_effect = execute_script_side_effect

        # Mock find_element to not find the "host to start meeting" text
        mock_driver.find_element.side_effect = NoSuchElementException("Element not found")

        # Create bot controller (adapter is not created yet - it's created in run())
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot time to retry and then stop
        # With 2 retries and 0 sleep time, this should complete quickly
        bot_thread.join(timeout=15)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the bot is in the FATAL_ERROR state after retries exhausted
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify that the generic join error check was called multiple times (retried)
        # With max retries = 3, we expect at least 4 checks (initial + 3 retries)
        self.assertGreater(generic_join_error_check_count[0], 1, "Generic join error check should have been called multiple times (retries)")

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()

        # Should have at least 2 events: JOIN_REQUESTED and COULD_NOT_JOIN
        self.assertGreaterEqual(len(bot_events), 2)

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Find the COULD_NOT_JOIN event (should be triggered after retries exhausted)
        could_not_join_events = [e for e in bot_events if e.event_type == BotEventTypes.COULD_NOT_JOIN]
        self.assertGreaterEqual(len(could_not_join_events), 1, "A COULD_NOT_JOIN event should be created after retries exhausted")

        # Verify the event has the correct subtype for zoom meeting status failed
        could_not_join_event = could_not_join_events[0]
        self.assertEqual(could_not_join_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()
