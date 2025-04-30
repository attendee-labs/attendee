import datetime
import json
import os
import threading
import time
from unittest.mock import MagicMock, call, patch

import kubernetes
import numpy as np
from django.db import connection
from django.test.testcases import TransactionTestCase
from django.utils import timezone

from bots.bot_controller import BotController
from bots.models import (
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
    RecordingTypes,
    TranscriptionProviders,
    TranscriptionTypes,
    Utterance,
)
from bots.web_bot_adapter.ui_methods import UiRetryableException


def create_mock_file_uploader():
    mock_file_uploader = MagicMock()
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.key = "test-recording-key"
    return mock_file_uploader


def create_mock_google_meet_driver():
    mock_driver = MagicMock()
    mock_driver.execute_script.side_effect = [
        None,  # First call (window.ws.enableMediaSending())
        12345,  # Second call (performance.timeOrigin)
    ]

    # Make save_screenshot actually create an empty PNG file
    def mock_save_screenshot(filepath):
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        # Create empty file
        with open(filepath, "wb") as f:
            # Write minimal valid PNG file bytes
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
        return filepath

    mock_driver.save_screenshot.side_effect = mock_save_screenshot
    return mock_driver


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

        # Configure Celery to run tasks eagerly (synchronously)
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("deepgram.DeepgramClient")
    @patch("time.time")
    def test_bot_can_join_meeting_and_record_audio_with_deepgram_transcription(
        self,
        mock_time,
        MockDeepgramClient,
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
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True}

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
            controller.individual_audio_input_manager.add_chunk("user1", datetime.datetime.utcnow(), pcm_data)

            # Process the chunks
            controller.individual_audio_input_manager.process_chunks()

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

        # Verify Deepgram was called to transcribe the audio
        mock_deepgram.listen.rest.v.return_value.transcribe_file.assert_called()

        # Verify utterances were processed
        utterances = Utterance.objects.filter(recording=self.recording)
        self.assertGreater(utterances.count(), 0)

        # Verify an audio utterance exists with the correct transcription
        audio_utterance = utterances.filter(source=Utterance.Sources.PER_PARTICIPANT_AUDIO).first()
        self.assertIsNotNone(audio_utterance)
        self.assertEqual(audio_utterance.transcription.get("transcript"), "This is a test transcription from Deepgram")

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

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("time.time")
    def test_bot_auto_leaves_meeting_after_silence_threshold(
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
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True}

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
    @patch("bots.bot_controller.bot_controller.FileUploader")
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
            # Sleep to allow initialization
            time.sleep(2)

            # Add participants - simulate websocket message processing
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True}

            # Simulate caption data arrival
            caption_data = {"captionId": "caption1", "deviceId": "user1", "text": "This is a test caption"}
            controller.closed_caption_manager.upsert_caption(caption_data)

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

    @patch("kubernetes.client.CoreV1Api")
    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    def test_terminate_bots_with_heartbeat_timeout(self, mock_load_kube_config, mock_load_incluster_config, MockCoreV1Api):
        # Set up mock Kubernetes API
        mock_k8s_api = MagicMock()
        MockCoreV1Api.return_value = mock_k8s_api

        # Set up config.load_incluster_config to raise ConfigException so load_kube_config gets called
        mock_load_incluster_config.side_effect = kubernetes.config.config_exception.ConfigException("Mock ConfigException")

        # Create a bot with a stale heartbeat (more than 10 minutes old)
        current_time = int(timezone.now().timestamp())
        eleven_minutes_ago = current_time - 660  # 11 minutes ago

        # Set the bot's heartbeat timestamps
        self.bot.first_heartbeat_timestamp = eleven_minutes_ago
        self.bot.last_heartbeat_timestamp = eleven_minutes_ago
        self.bot.state = BotStates.JOINED_RECORDING  # Set to a non-terminal state
        self.bot.save()

        # Set bot launch method to kubernetes
        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"}):
            # Import and run the command
            from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

            command = Command()
            command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was moved to FATAL_ERROR state
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify that a FATAL_ERROR event was created with the correct sub type
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT).first()
        self.assertIsNotNone(fatal_error_event)
        self.assertEqual(fatal_error_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(fatal_error_event.new_state, BotStates.FATAL_ERROR)

        # Verify Kubernetes pod deletion was attempted with the correct pod name
        pod_name = self.bot.k8s_pod_name()
        mock_k8s_api.delete_namespaced_pod.assert_called_once_with(name=pod_name, namespace="attendee", grace_period_seconds=0)

    def test_bots_with_recent_heartbeat_not_terminated(self):
        # Create a bot with a recent heartbeat (9 minutes old)
        current_time = int(timezone.now().timestamp())
        nine_minutes_ago = current_time - 540  # 9 minutes ago

        # Set the bot's heartbeat timestamps
        self.bot.first_heartbeat_timestamp = nine_minutes_ago
        self.bot.last_heartbeat_timestamp = nine_minutes_ago
        self.bot.state = BotStates.JOINED_RECORDING  # Set to a non-terminal state
        self.bot.save()

        # Import and run the command
        from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

        command = Command()
        command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was NOT moved to FATAL_ERROR state
        self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING)

        # Verify that no FATAL_ERROR event was created with heartbeat timeout subtype
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT).first()
        self.assertIsNone(fatal_error_event)

    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
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
        mock_driver = create_mock_google_meet_driver()
        MockChromeDriver.return_value = mock_driver

        # Mock virtual display
        mock_display = MagicMock()
        MockDisplay.return_value = mock_display

        # Create bot controller
        controller = BotController(self.bot.id)

        # Set up a side effect that raises an exception on first attempt, then succeeds on second attempt
        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.attempt_to_join_meeting") as mock_attempt_to_join:
            mock_attempt_to_join.side_effect = [
                UiRetryableException("Simulated first attempt failure", "test_step"),  # First call fails
                None,  # Second call succeeds
            ]

            # Run the bot in a separate thread since it has an event loop
            bot_thread = threading.Thread(target=controller.run)
            bot_thread.daemon = True
            bot_thread.start()

            # Allow time for the retry logic to run
            time.sleep(5)

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

    @patch("kubernetes.client.CoreV1Api")
    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    def test_terminate_bots_that_never_launched(self, mock_load_kube_config, mock_load_incluster_config, MockCoreV1Api):
        # Set up mock Kubernetes API
        mock_k8s_api = MagicMock()
        MockCoreV1Api.return_value = mock_k8s_api

        # Set up config.load_incluster_config to raise ConfigException so load_kube_config gets called
        mock_load_incluster_config.side_effect = kubernetes.config.config_exception.ConfigException("Mock ConfigException")

        # Create a bot that was created 2 days ago but never launched
        two_days_ago = timezone.now() - timezone.timedelta(days=2)
        self.bot.first_heartbeat_timestamp = None
        self.bot.last_heartbeat_timestamp = None
        self.bot.state = BotStates.JOINING  # Set to a non-terminal state
        self.bot.created_at = two_days_ago
        self.bot.save()

        # Set bot launch method to kubernetes
        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"}):
            # Import and run the command
            from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

            command = Command()
            command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was moved to FATAL_ERROR state
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify that a FATAL_ERROR event was created with the correct sub type
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED).first()
        self.assertIsNotNone(fatal_error_event)
        self.assertEqual(fatal_error_event.old_state, BotStates.JOINING)
        self.assertEqual(fatal_error_event.new_state, BotStates.FATAL_ERROR)

        # Verify Kubernetes pod deletion was attempted with the correct pod name
        pod_name = self.bot.k8s_pod_name()
        mock_k8s_api.delete_namespaced_pod.assert_called_once_with(name=pod_name, namespace="attendee", grace_period_seconds=0)

    def test_recent_bots_with_no_heartbeat_not_terminated(self):
        # Create a bot that was created 30 minutes ago but never launched
        thirty_minutes_ago = timezone.now() - timezone.timedelta(minutes=30)
        self.bot.first_heartbeat_timestamp = None
        self.bot.last_heartbeat_timestamp = None
        self.bot.state = BotStates.JOINING  # Set to a non-terminal state
        self.bot.created_at = thirty_minutes_ago
        self.bot.save()

        # Import and run the command
        from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

        command = Command()
        command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was NOT moved to FATAL_ERROR state since it's too recent
        self.assertEqual(self.bot.state, BotStates.JOINING)

        # Verify that no FATAL_ERROR event was created for a bot that never launched
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED).first()
        self.assertIsNone(fatal_error_event)


# Simulate video data arrival
# Create a mock video message in the format expected by process_video_frame
def create_mock_video_frame(width=640, height=480):
    # Create a bytearray for the message
    mock_video_message = bytearray()

    # Add message type (2 for VIDEO) as first 4 bytes
    mock_video_message.extend((2).to_bytes(4, byteorder="little"))

    # Add timestamp (12345) as next 8 bytes
    mock_video_message.extend((12345).to_bytes(8, byteorder="little"))

    # Add stream ID length (4) and stream ID ("main") - total 8 bytes
    stream_id = "main"
    mock_video_message.extend(len(stream_id).to_bytes(4, byteorder="little"))
    mock_video_message.extend(stream_id.encode("utf-8"))

    # Add width and height - 8 bytes
    mock_video_message.extend(width.to_bytes(4, byteorder="little"))
    mock_video_message.extend(height.to_bytes(4, byteorder="little"))

    # Create I420 frame data (Y, U, V planes)
    # Y plane: width * height bytes
    y_plane_size = width * height
    y_plane = np.ones(y_plane_size, dtype=np.uint8) * 128  # mid-gray

    # U and V planes: (width//2 * height//2) bytes each
    uv_width = (width + 1) // 2  # half_ceil implementation
    uv_height = (height + 1) // 2
    uv_plane_size = uv_width * uv_height

    u_plane = np.ones(uv_plane_size, dtype=np.uint8) * 128  # no color tint
    v_plane = np.ones(uv_plane_size, dtype=np.uint8) * 128  # no color tint

    # Add the frame data to the message
    mock_video_message.extend(y_plane.tobytes())
    mock_video_message.extend(u_plane.tobytes())
    mock_video_message.extend(v_plane.tobytes())

    return mock_video_message
