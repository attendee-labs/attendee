import datetime
import json
import os
import threading
import time
from base64 import b64encode
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import numpy as np
from django.db import connection
from django.db.backends.signals import connection_created
from django.test.testcases import TransactionTestCase
from selenium.common.exceptions import TimeoutException
from websockets.sync.client import connect as ws_connect

from bots.bot_controller import BotController
from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods
from bots.models import (
    AsyncTranscription,
    AsyncTranscriptionStates,
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Credentials,
    CreditTransaction,
    Organization,
    Project,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    TranscriptionFailureReasons,
    TranscriptionProviders,
    TranscriptionTypes,
    Utterance,
    WebhookDeliveryAttempt,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
)
from bots.tasks.process_async_transcription_task import process_async_transcription
from bots.tests.mock_data import create_mock_file_uploader, create_mock_google_meet_driver
from bots.web_bot_adapter.ui_methods import UiCouldNotJoinMeetingWaitingRoomTimeoutException


@contextmanager
def track_db_connections():
    events = []  # (thread_ident, thread_name, alias)

    def handler(sender, connection, **kwargs):
        events.append(
            (
                threading.get_ident(),
                threading.current_thread().name,
                connection.alias,
            )
        )

    uid = object()
    connection_created.connect(handler, dispatch_uid=uid)
    try:
        yield events
    finally:
        connection_created.disconnect(handler, dispatch_uid=uid)


def assert_only_threads_created_connections(testcase, events, allowed_thread_idents):
    offenders = [e for e in events if e[0] not in allowed_thread_idents]
    testcase.assertEqual(
        offenders,
        [],
        f"DB connections created from disallowed threads: {offenders}",
    )


class TestGoogleMeetBot(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Set required environment variables
        os.environ["AWS_RECORDING_STORAGE_BUCKET_NAME"] = "test-bucket"
        os.environ["CHARGE_CREDITS_FOR_BOTS"] = "false"

    def setUp(self):
        # Recreate organization and project for each test
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create a bot for each test
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
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

        self.deepgram_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)
        self.deepgram_credentials.set_credentials({"api_key": "test_api_key"})

        # Create webhook subscription for transcript updates
        self.webhook_secret = WebhookSecret.objects.create(project=self.project)

        # Configure Celery to run tasks eagerly (synchronously)
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("deepgram.DeepgramClient")
    @patch("time.time")
    @patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_bot_can_join_meeting_and_record_audio_with_deepgram_transcription(
        self,
        mock_deliver_webhook,
        mock_time,
        MockDeepgramClient,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        mock_deliver_webhook.return_value = None

        self.webhook_subscription = WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE, WebhookTriggerTypes.TRANSCRIPT_UPDATE, WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE],
            is_active=True,
        )

        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Use Deepgram for transcription instead of closed captions
        self.recording.transcription_provider = TranscriptionProviders.DEEPGRAM
        self.recording.save()

        # Configure the mock deepgram client
        mock_deepgram = MagicMock()
        mock_response = MagicMock()

        # Create a mock transcription result that Deepgram would return
        mock_result = MagicMock()
        mock_result.to_json.return_value = json.dumps({"transcript": "This is a test transcription from Deepgram", "confidence": 0.95, "words": [{"word": "This", "start": 0.0, "end": 0.2}, {"word": "is", "start": 0.2, "end": 0.3}]})

        # Set up the mock response structure
        mock_response.results.channels = [MagicMock()]
        mock_response.results.channels[0].alternatives = [mock_result]

        # Make the deepgram client return our mock response
        mock_deepgram.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        MockDeepgramClient.return_value = mock_deepgram

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_google_meet_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            nonlocal current_time
            # Sleep to allow initialization
            time.sleep(2)

            # Add participants - simulate websocket message processing
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Simulate receiving audio by updating the last audio message processed time
            controller.adapter.last_audio_message_processed_time = current_time

            # Simulate audio frame from participant
            sample_rate = 48000  # 48kHz sample rate
            duration_ms = 10  # 10 milliseconds
            num_samples = int(sample_rate * duration_ms / 1000)  # Calculate number of samples

            # Create buffer with the right number of samples
            audio_data = np.zeros(num_samples, dtype=np.float32)

            # Generate a sine wave (440Hz = A note) for 10ms
            t = np.arange(0, duration_ms / 1000, 1 / sample_rate)
            sine_wave = 0.5 * np.sin(2 * np.pi * 440 * t)

            # Place the sine wave in the buffer
            audio_data[: len(sine_wave)] = sine_wave

            # Convert float to PCM int16
            pcm_data = (audio_data * 32768.0).astype(np.int16).tobytes()

            # Send audio chunk as if it came from the participant
            controller.per_participant_non_streaming_audio_input_manager.add_chunk("user1", datetime.datetime.utcnow(), pcm_data)

            # Process the chunks
            controller.per_participant_non_streaming_audio_input_manager.process_chunks()

            # Sleep to allow audio processing
            time.sleep(3)

            # Trigger only one participant in meeting auto leave
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(4)

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that joined at is not none
        self.assertIsNotNone(controller.adapter.joined_at)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 6)  # We expect 6 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify bot requested to leave meeting (Event 4)
        bot_requested_to_leave_meeting_event = bot_events[3]
        self.assertEqual(bot_requested_to_leave_meeting_event.event_type, BotEventTypes.LEAVE_REQUESTED)
        self.assertEqual(bot_requested_to_leave_meeting_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(bot_requested_to_leave_meeting_event.new_state, BotStates.LEAVING)

        # Verify bot left meeting (Event 5)
        bot_left_meeting_event = bot_events[4]
        self.assertEqual(bot_left_meeting_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(bot_left_meeting_event.old_state, BotStates.LEAVING)
        self.assertEqual(bot_left_meeting_event.new_state, BotStates.POST_PROCESSING)

        # Verify post_processing_completed_event (Event 6)
        post_processing_completed_event = bot_events[5]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify Deepgram was called to transcribe the audio
        mock_deepgram.listen.rest.v.return_value.transcribe_file.assert_called()

        # Verify utterances were processed
        utterances = Utterance.objects.filter(recording=self.recording)
        self.assertGreater(utterances.count(), 0)
        self.assertEqual(utterances.count(), self.recording.audio_chunks.count())

        # Verify an audio utterance exists with the correct transcription
        audio_utterance = utterances.filter(source=Utterance.Sources.PER_PARTICIPANT_AUDIO, failure_data__isnull=True).first()
        self.assertIsNotNone(audio_utterance)
        self.assertEqual(audio_utterance.transcription.get("transcript"), "This is a test transcription from Deepgram")
        self.assertEqual(audio_utterance.audio_chunk, self.recording.audio_chunks.first())

        # Verify webhook delivery attempts were created for transcript updates
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE)
        self.assertGreater(webhook_delivery_attempts.count(), 0, "Expected webhook delivery attempts for transcript updates")

        # Verify the webhook payload contains the expected utterance data
        webhook_attempt = webhook_delivery_attempts.first()
        self.assertIsNotNone(webhook_attempt.payload)
        self.assertIn("speaker_name", webhook_attempt.payload)
        self.assertIn("speaker_uuid", webhook_attempt.payload)
        self.assertIn("transcription", webhook_attempt.payload)
        self.assertEqual(webhook_attempt.payload["speaker_name"], "Test User")
        self.assertEqual(webhook_attempt.payload["speaker_uuid"], "user1")
        self.assertIsNotNone(webhook_attempt.payload["transcription"])

        # Verify WebSocket media sending was enabled and performance.timeOrigin was queried
        mock_driver.execute_script.assert_has_calls([call("window.ws?.enableMediaSending();"), call("return performance.timeOrigin;")])

        # Verify file uploader was used
        mock_uploader.upload_file.assert_called_once()
        self.assertGreater(mock_uploader.upload_file.call_count, 0)
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

        # Now test creating an async transcription
        async_transcription = AsyncTranscription.objects.create(recording=self.recording, settings={"transcription_settings": {"deepgram": {}}})
        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.NOT_STARTED)

        process_async_transcription.delay(async_transcription.id)

        async_transcription.refresh_from_db()

        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.COMPLETE)
        self.assertIsNotNone(async_transcription.completed_at)
        self.assertIsNotNone(async_transcription.started_at)
        self.assertIsNone(async_transcription.failure_data)
        self.assertEqual(utterances.first().transcription, async_transcription.utterances.first().transcription)
        self.assertEqual(Utterance.objects.filter(recording=self.recording, async_transcription=async_transcription).count(), Utterance.objects.filter(recording=self.recording, async_transcription=None).count())

        # Verify webhook delivery attempts were created for async transcription state changes
        async_transcription_webhook_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE)
        # Should have two webhook attempts: one for IN_PROGRESS, one for COMPLETE
        self.assertEqual(async_transcription_webhook_attempts.count(), 2, "Expected webhook delivery attempts for async transcription state changes")

        # Verify the webhook payloads contain the expected async transcription data
        in_progress_webhook = async_transcription_webhook_attempts.filter(payload__state="in_progress").first()
        self.assertIsNotNone(in_progress_webhook, "Expected webhook for IN_PROGRESS state")
        self.assertEqual(in_progress_webhook.payload["id"], async_transcription.object_id)
        self.assertIsNone(in_progress_webhook.payload["failure_data"])

        complete_webhook = async_transcription_webhook_attempts.filter(payload__state="complete").first()
        self.assertIsNotNone(complete_webhook, "Expected webhook for COMPLETE state")
        self.assertEqual(complete_webhook.payload["id"], async_transcription.object_id)
        self.assertIsNone(complete_webhook.payload["failure_data"])

        # Now delete the deepgram credentials to simulate transcription failure
        self.deepgram_credentials.delete()
        async_transcription_after_credentials_deleted = AsyncTranscription.objects.create(recording=self.recording, settings={"transcription_settings": {"deepgram": {}}})

        process_async_transcription.delay(async_transcription_after_credentials_deleted.id)

        async_transcription_after_credentials_deleted.refresh_from_db()

        self.assertEqual(async_transcription_after_credentials_deleted.state, AsyncTranscriptionStates.FAILED)
        self.assertIsNotNone(async_transcription_after_credentials_deleted.failure_data.get("failure_reasons"))
        self.assertIn(TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, async_transcription_after_credentials_deleted.failure_data.get("failure_reasons"))
        self.assertIsNotNone(async_transcription_after_credentials_deleted.failed_at)
        self.assertIsNotNone(async_transcription_after_credentials_deleted.started_at)
        self.assertIsNone(async_transcription_after_credentials_deleted.completed_at)
        self.assertEqual(Utterance.objects.filter(recording=self.recording, async_transcription=async_transcription_after_credentials_deleted).count(), Utterance.objects.filter(recording=self.recording, async_transcription=None).count())

        # Verify webhook delivery attempts were created for the failed async transcription
        failed_async_transcription_webhook_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE).order_by("created_at")
        # Should now have 4 total webhook attempts: 2 from successful transcription + 2 from failed transcription
        self.assertEqual(failed_async_transcription_webhook_attempts.count(), 4, "Expected additional webhook delivery attempts for failed async transcription")

        # Get the last two webhook attempts (for the failed transcription)
        latest_webhooks = failed_async_transcription_webhook_attempts.order_by("-created_at")[:2]

        # Find the IN_PROGRESS and FAILED webhooks for the second transcription
        failed_transcription_in_progress_webhook = None
        failed_transcription_failed_webhook = None

        for webhook in latest_webhooks:
            if webhook.payload.get("id") == async_transcription_after_credentials_deleted.object_id:
                if webhook.payload.get("state") == "in_progress":
                    failed_transcription_in_progress_webhook = webhook
                elif webhook.payload.get("state") == "failed":
                    failed_transcription_failed_webhook = webhook

        self.assertIsNotNone(failed_transcription_in_progress_webhook, "Expected webhook for failed transcription IN_PROGRESS state")
        self.assertIsNone(failed_transcription_in_progress_webhook.payload["failure_data"])

        self.assertIsNotNone(failed_transcription_failed_webhook, "Expected webhook for FAILED state")
        self.assertIn(TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, failed_transcription_failed_webhook.payload["failure_data"]["failure_reasons"])

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.look_for_blocked_element", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.look_for_denied_your_request_element", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_this_meeting_is_being_recorded_join_now_button", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_others_may_see_your_meeting_differently_button", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.fill_out_name_input", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.turn_off_media_inputs", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.locate_element")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_element")
    @patch("time.time")
    def test_bot_stops_after_waiting_room_timeout(
        self,
        mock_time,
        mock_click_element,
        mock_locate_element,
        mock_turn_off_media_inputs,
        mock_fill_out_name_input,
        mock_check_if_meeting_is_found,
        mock_click_others_may_see_your_meeting_differently_button,
        mock_click_this_meeting_is_being_recorded_join_now_button,
        mock_look_for_denied_your_request_element,
        mock_look_for_blocked_element,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_google_meet_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Mock join button element
        mock_join_button = MagicMock()

        # Configure locate_element to return mock join button when called for "join_button"
        def mock_locate_element_side_effect(step, condition, wait_time_seconds=60):
            if step == "join_button":
                return mock_join_button
            return MagicMock()  # Return a generic mock for other calls

        mock_locate_element.side_effect = mock_locate_element_side_effect

        def mock_click_element_side_effect(element, step):
            if step == "click_captions_button":
                raise TimeoutException("Timed out")
            return MagicMock()  # Return a generic mock for other calls

        mock_click_element.side_effect = mock_click_element_side_effect

        # Create bot controller
        controller = BotController(self.bot.id)

        # Mock the check_if_waiting_room_timeout_exceeded method to raise the exception
        # after a certain number of calls to simulate timeout
        original_check_timeout = GoogleMeetUIMethods.check_if_waiting_room_timeout_exceeded
        call_count = [0]

        def mock_check_timeout(self, waiting_room_timeout_started_at, step):
            print(f"Checking timeout for step: {step}")
            call_count[0] += 1
            if call_count[0] >= 2:  # Simulate timeout on second call
                # Increase time to simulate timeout period passed
                nonlocal current_time
                current_time += 901  # Just over the 900 second default timeout
                mock_time.return_value = current_time
                raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)
            return original_check_timeout(self, waiting_room_timeout_started_at, step)

        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_waiting_room_timeout_exceeded", mock_check_timeout):
            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Give the bot some time to process
            bot_thread.join(timeout=10)

            # Refresh the bot from the database
            self.bot.refresh_from_db()

            # Assert that the bot is in the FATAL_ERROR state (or the appropriate state after timeout)
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

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("time.time")
    def test_bot_auto_leaves_meeting_after_silence_timeout(
        self,
        mock_time,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_google_meet_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            nonlocal current_time
            # Sleep to allow initialization
            time.sleep(2)

            # Add participants - simulate websocket message processing
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Simulate receiving audio by updating the last audio message processed time
            controller.adapter.last_audio_message_processed_time = current_time

            # Sleep to allow processing
            time.sleep(2)

            # Advance time past silence activation threshold (1200 seconds)
            current_time += 1201
            mock_time.return_value = current_time

            # Trigger check of auto-leave conditions which should activate silence detection
            controller.adapter.check_auto_leave_conditions()

            # Verify silence detection was activated
            self.assertTrue(controller.adapter.silence_detection_activated)

            # Advance time past silence threshold (600 seconds)
            current_time += 601
            mock_time.return_value = current_time

            # Trigger check of auto-leave conditions which should trigger auto-leave
            controller.adapter.check_auto_leave_conditions()

            # Sleep to allow for event processing
            time.sleep(2)

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Assert that silence detection was activated
        self.assertTrue(controller.adapter.silence_detection_activated)
        self.assertIsNotNone(controller.adapter.joined_at)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 6)  # We expect 6 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify leave_requested_event (Event 4)
        leave_requested_event = bot_events[3]
        self.assertEqual(leave_requested_event.event_type, BotEventTypes.LEAVE_REQUESTED)
        self.assertEqual(leave_requested_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(leave_requested_event.new_state, BotStates.LEAVING)
        self.assertEqual(
            leave_requested_event.event_sub_type,
            BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_SILENCE,
        )

        # Verify bot_left_meeting_event (Event 5)
        bot_left_meeting_event = bot_events[4]
        self.assertEqual(bot_left_meeting_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(bot_left_meeting_event.old_state, BotStates.LEAVING)
        self.assertEqual(bot_left_meeting_event.new_state, BotStates.POST_PROCESSING)
        self.assertIsNone(bot_left_meeting_event.event_sub_type)

        # Verify post_processing_completed_event (Event 6)
        post_processing_completed_event = bot_events[5]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    def test_google_meet_bot_can_join_meeting_and_record_audio_and_video(
        self,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        with track_db_connections() as events:
            # Configure the mock uploader
            mock_uploader = create_mock_file_uploader()
            MockFileUploader.return_value = mock_uploader

            # Mock the Chrome driver
            mock_driver = create_mock_google_meet_driver()
            MockChromeDriver.return_value = mock_driver

            # Mock virtual display
            mock_display = MagicMock()
            MockDisplay.return_value = mock_display

            # Use closed captions for transcription so websocket caption messages are handled
            self.recording.transcription_provider = TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM
            self.recording.save()

            # Create bot controller
            controller = BotController(self.bot.id)

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            def send_websocket_message(port, message_dict):
                """Send a JSON message to the adapter's websocket server."""
                json_bytes = json.dumps(message_dict).encode("utf-8")
                # Message type 1 = JSON, as 4 bytes little-endian
                message = (1).to_bytes(4, byteorder="little") + json_bytes
                with ws_connect(f"ws://localhost:{port}") as websocket:
                    websocket.send(message)

            def simulate_join_flow():
                # Sleep to allow initialization and wait for websocket server to start
                time.sleep(2)

                # Wait for the websocket port to be available
                while controller.adapter.websocket_port is None:
                    time.sleep(0.1)

                ws_port = controller.adapter.websocket_port

                # Add participants via websocket UsersUpdate message
                users_update_message = {
                    "type": "UsersUpdate",
                    "newUsers": [
                        {
                            "deviceId": "user1",
                            "fullName": "Test User",
                            "humanized_status": "in_meeting",
                            "isCurrentUser": False,
                            "isHost": False,
                        }
                    ],
                    "removedUsers": [],
                    "updatedUsers": [],
                }
                send_websocket_message(ws_port, users_update_message)

                # Allow time for participant to be processed
                time.sleep(0.5)

                # Simulate caption data arrival via websocket CaptionUpdate message
                caption_update_message = {
                    "type": "CaptionUpdate",
                    "caption": {
                        "captionId": "caption1",
                        "deviceId": "user1",
                        "text": "This is a test caption",
                        "isFinal": 1,
                    },
                }
                send_websocket_message(ws_port, caption_update_message)

                # Process these events
                time.sleep(2)

                # Simulate flushing captions - normally done before leaving
                controller.closed_caption_manager.flush_captions()

                # Trigger only one participant in meeting auto leave
                controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
                time.sleep(4)

                # Clean up connections in thread
                connection.close()

            # Run join flow simulation after a short delay
            threading.Timer(2, simulate_join_flow).start()

            # Give the bot some time to process
            bot_thread.join(timeout=10)

            # Assert that only the main thread created the database connection
            assert_only_threads_created_connections(self, events, [bot_thread.ident])

            # Refresh the bot from the database
            self.bot.refresh_from_db()

            # Assert that the heartbeat timestamp was set
            self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
            self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

            # Assert that joined at is not none
            self.assertIsNotNone(controller.adapter.joined_at)

            # Assert that the bot is in the ENDED state
            self.assertEqual(self.bot.state, BotStates.ENDED)

            # Verify bot events in sequence
            bot_events = self.bot.bot_events.all()
            self.assertEqual(len(bot_events), 6)  # We expect 5 events in total

            # Verify join_requested_event (Event 1)
            join_requested_event = bot_events[0]
            self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
            self.assertEqual(join_requested_event.old_state, BotStates.READY)
            self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

            # Verify bot_joined_meeting_event (Event 2)
            bot_joined_meeting_event = bot_events[1]
            self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
            self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
            self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

            # Verify recording_permission_granted_event (Event 3)
            recording_permission_granted_event = bot_events[2]
            self.assertEqual(
                recording_permission_granted_event.event_type,
                BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
            )
            self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
            self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

            # Verify bot requested to leave meeting (Event 4)
            bot_requested_to_leave_meeting_event = bot_events[3]
            self.assertEqual(bot_requested_to_leave_meeting_event.event_type, BotEventTypes.LEAVE_REQUESTED)
            self.assertEqual(bot_requested_to_leave_meeting_event.old_state, BotStates.JOINED_RECORDING)
            self.assertEqual(bot_requested_to_leave_meeting_event.new_state, BotStates.LEAVING)

            # Verify bot left meeting (Event 5)
            bot_left_meeting_event = bot_events[4]
            self.assertEqual(bot_left_meeting_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
            self.assertEqual(bot_left_meeting_event.old_state, BotStates.LEAVING)
            self.assertEqual(bot_left_meeting_event.new_state, BotStates.POST_PROCESSING)

            # Verify post_processing_completed_event (Event 6)
            post_processing_completed_event = bot_events[5]
            self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
            self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
            self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

            # Verify that the recording was finished
            self.recording.refresh_from_db()
            self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

            # Verify captions were processed
            utterances = Utterance.objects.filter(recording=self.recording)
            self.assertGreater(utterances.count(), 0)

            # Verify a caption utterance exists with the correct text
            caption_utterance = utterances.filter(source=Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM).first()
            self.assertIsNotNone(caption_utterance)
            self.assertEqual(caption_utterance.transcription.get("transcript"), "This is a test caption")

            # Verify webhook delivery attempts were created for transcript updates
            webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE)
            self.assertEqual(webhook_delivery_attempts.count(), 0, "Expected zero webhook delivery attempts for transcript updates")

            # Verify WebSocket media sending was enabled and performance.timeOrigin was queried
            mock_driver.execute_script.assert_has_calls([call("window.ws?.enableMediaSending();"), call("return performance.timeOrigin;")])

            # Verify that no charge was created (since the env var is not set in this test suite)
            credit_transaction = CreditTransaction.objects.filter(bot=self.bot).first()
            self.assertIsNone(credit_transaction, "A credit transaction was created for the bot")

            # Verify file uploader was used
            mock_uploader.upload_file.assert_called_once()
            self.assertGreater(mock_uploader.upload_file.call_count, 0)
            mock_uploader.wait_for_upload.assert_called_once()
            mock_uploader.delete_file.assert_called_once()
            # Cleanup
            controller.cleanup()
            bot_thread.join(timeout=5)

            # Close the database connection since we're in a thread
            connection.close()

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.google_meet_bot_adapter.google_meet_bot_adapter.GoogleMeetBotAdapter.send_raw_audio")
    @patch("bots.bot_controller.bot_controller.S3FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("bots.bot_controller.bot_controller.BotWebsocketClient")
    @patch("time.time")
    def test_bot_bidirectional_audio_streaming_via_websockets(
        self,
        mock_time,
        MockBotWebsocketClient,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        mock_send_raw_audio,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Configure bot for websocket audio streaming
        self.bot.settings = {"websocket_settings": {"audio": {"url": "wss://example.com/audio-stream"}}}
        self.bot.save()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the Chrome driver
        mock_driver = create_mock_google_meet_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create a comprehensive mock for BotWebsocketClient
        mock_websocket_client = MagicMock()
        mock_websocket_client.started.return_value = True
        mock_websocket_client.start.return_value = None
        mock_websocket_client.cleanup.return_value = None
        mock_websocket_client.send_async.return_value = None

        # Mock the adapter's send_raw_audio method to track calls
        send_raw_audio_calls = []
        mock_send_raw_audio.side_effect = lambda bytes, sample_rate: send_raw_audio_calls.append({"bytes": bytes, "sample_rate": sample_rate})
        mock_send_raw_audio.return_value = None

        # Store sent messages for verification
        sent_messages = []

        def capture_sent_message(message):
            sent_messages.append(message)

        mock_websocket_client.send_async.side_effect = capture_sent_message

        MockBotWebsocketClient.return_value = mock_websocket_client

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_bidirectional_audio_streaming():
            nonlocal current_time
            # Sleep to allow initialization
            time.sleep(2)

            # Add participants - simulate websocket message processing
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Simulate receiving audio by updating the last audio message processed time
            controller.adapter.last_audio_message_processed_time = current_time

            # Test outgoing audio streaming - simulate mixed audio chunk
            sample_rate = 48000  # 48kHz sample rate
            duration_ms = 20  # 20 milliseconds

            # Generate test audio data (sine wave)
            t = np.arange(0, duration_ms / 1000, 1 / sample_rate)
            sine_wave = 0.5 * np.sin(2 * np.pi * 440 * t)  # 440Hz tone
            audio_data = (sine_wave * 32768.0).astype(np.int16)
            pcm_data = audio_data.tobytes()

            # Simulate mixed audio chunk being sent to websocket
            controller.add_mixed_audio_chunk_callback(pcm_data)

            # Allow time for processing
            time.sleep(1)

            # Test incoming audio streaming - simulate receiving audio from websocket
            # Create a mock websocket message for bot output audio
            incoming_audio_message = {
                "trigger": "realtime_audio.bot_output",
                "data": {
                    "chunk": b64encode(pcm_data).decode("ascii"),
                    "sample_rate": sample_rate,
                },
            }

            # Simulate receiving the message through the websocket callback
            for i in range(10):
                controller.on_message_from_websocket_audio(json.dumps(incoming_audio_message))

            # Allow time for audio processing and the realtime audio output manager to process
            time.sleep(3)

            # Test invalid message handling
            invalid_message = {"trigger": "unknown_trigger", "data": {}}
            controller.on_message_from_websocket_audio(json.dumps(invalid_message))

            # Test malformed JSON handling
            controller.on_message_from_websocket_audio("invalid json")

            time.sleep(1)

            # Trigger auto leave
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(4)

            # Clean up connections in thread
            connection.close()

        # Run streaming simulation after a short delay
        threading.Timer(2, simulate_bidirectional_audio_streaming).start()

        # Give the bot some time to process
        bot_thread.join(timeout=15)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the bot completed successfully
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify websocket client was created and configured correctly
        MockBotWebsocketClient.assert_called_once()
        websocket_call_args = MockBotWebsocketClient.call_args
        self.assertEqual(websocket_call_args[1]["url"], "wss://example.com/audio-stream")
        self.assertIsNotNone(websocket_call_args[1]["on_message_callback"])

        # Verify outgoing audio messages were sent
        self.assertGreater(len(sent_messages), 0, "Expected audio messages to be sent via websocket")

        # Verify the structure of sent audio messages
        audio_message = sent_messages[0]
        self.assertEqual(audio_message["trigger"], "realtime_audio.mixed")
        self.assertEqual(audio_message["bot_id"], self.bot.object_id)
        self.assertIn("data", audio_message)
        self.assertIn("chunk", audio_message["data"])
        self.assertIn("timestamp_ms", audio_message["data"])

        # Verify the audio chunk is properly base64 encoded
        from base64 import b64decode

        decoded_chunk = b64decode(audio_message["data"]["chunk"])
        self.assertGreater(len(decoded_chunk), 0)

        # Verify realtime audio output manager was created and used
        self.assertIsNotNone(controller.realtime_audio_output_manager)

        # Verify that the adapter's send raw audio method was called
        # This verifies that incoming websocket audio was processed and sent to the adapter
        self.assertGreater(len(send_raw_audio_calls), 0, "Expected adapter.send_raw_audio to be called for incoming websocket audio")

        # Verify the structure of the send_raw_audio call
        audio_call = send_raw_audio_calls[0]
        self.assertIn("bytes", audio_call, "send_raw_audio should be called with bytes parameter")
        self.assertIn("sample_rate", audio_call, "send_raw_audio should be called with sample_rate parameter")
        self.assertGreater(len(audio_call["bytes"]), 0, "Audio bytes should not be empty")
        self.assertGreater(audio_call["sample_rate"], 0, "Sample rate should be positive")

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertGreaterEqual(len(bot_events), 6)  # At least the standard sequence of events

        # Verify join_requested_event
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify final post_processing_completed_event
        post_processing_completed_event = bot_events[len(bot_events) - 1]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify WebSocket media sending was enabled
        mock_driver.execute_script.assert_has_calls([call("window.ws?.enableMediaSending();"), call("return performance.timeOrigin;")])

        # Verify file uploader was used
        mock_uploader.upload_file.assert_called_once()
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()
