import os
import threading
import time
from unittest.mock import MagicMock, call, patch

import kubernetes
from django.db import connection
from django.test.testcases import TransactionTestCase
from django.utils import timezone

from bots.bot_adapter import BotAdapter
from bots.bot_controller import BotController
from bots.models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    ChatMessage,
    Credentials,
    Organization,
    Participant,
    ParticipantEvent,
    ParticipantEventTypes,
    Project,
    Recording,
    RecordingStates,
    RecordingTypes,
    TranscriptionProviders,
    TranscriptionTypes,
    Utterance,
    WebhookDeliveryAttempt,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
)
from bots.tests.mock_data import create_mock_file_uploader, create_mock_google_meet_driver
from bots.web_bot_adapter.ui_methods import UiRetryableException


class TestGoogleMeetBot2(TransactionTestCase):
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

    @patch("kubernetes.client.CoreV1Api")
    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    def test_scheduled_bot_with_future_join_at_not_terminated(self, mock_load_kube_config, mock_load_incluster_config, MockCoreV1Api):
        # Set up mock Kubernetes API
        mock_k8s_api = MagicMock()
        MockCoreV1Api.return_value = mock_k8s_api

        # Set up config.load_incluster_config to raise ConfigException so load_kube_config gets called
        mock_load_incluster_config.side_effect = kubernetes.config.config_exception.ConfigException("Mock ConfigException")

        # Create a scheduled bot that was created 5 days ago but has join_at in the future
        five_days_ago = timezone.now() - timezone.timedelta(days=5)
        one_hour_from_now = timezone.now() + timezone.timedelta(hours=1)

        self.bot.created_at = five_days_ago
        self.bot.join_at = one_hour_from_now  # Future join time
        self.bot.first_heartbeat_timestamp = None
        self.bot.last_heartbeat_timestamp = None
        self.bot.state = BotStates.SCHEDULED  # Set to scheduled state
        self.bot.save()

        # Set bot launch method to kubernetes
        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"}):
            # Import and run the command
            from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

            command = Command()
            command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was NOT moved to FATAL_ERROR state since join_at is in the future
        self.assertEqual(self.bot.state, BotStates.SCHEDULED)

        # Verify that no FATAL_ERROR event was created for a bot that never launched
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED).first()
        self.assertIsNone(fatal_error_event)

        # Verify that no pod deletion was attempted
        mock_k8s_api.delete_namespaced_pod.assert_not_called()

    @patch("kubernetes.client.CoreV1Api")
    @patch("kubernetes.config.load_incluster_config")
    @patch("kubernetes.config.load_kube_config")
    def test_scheduled_bot_with_past_join_at_terminated(self, mock_load_kube_config, mock_load_incluster_config, MockCoreV1Api):
        # Set up mock Kubernetes API
        mock_k8s_api = MagicMock()
        MockCoreV1Api.return_value = mock_k8s_api

        # Set up config.load_incluster_config to raise ConfigException so load_kube_config gets called
        mock_load_incluster_config.side_effect = kubernetes.config.config_exception.ConfigException("Mock ConfigException")

        # Create a scheduled bot with join_at in the past (2 days ago) but never launched
        two_days_ago = timezone.now() - timezone.timedelta(days=2)

        self.bot.join_at = two_days_ago  # Past join time
        self.bot.first_heartbeat_timestamp = None
        self.bot.last_heartbeat_timestamp = None
        self.bot.state = BotStates.SCHEDULED  # Set to scheduled state
        self.bot.save()

        # Set bot launch method to kubernetes
        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"}):
            # Import and run the command
            from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

            command = Command()
            command.handle()

        # Refresh the bot state from the database
        self.bot.refresh_from_db()

        # Verify the bot was moved to FATAL_ERROR state since join_at was in the past and it never launched
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify that a FATAL_ERROR event was created with the correct sub type
        fatal_error_event = self.bot.bot_events.filter(event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED).first()
        self.assertIsNotNone(fatal_error_event)
        self.assertEqual(fatal_error_event.old_state, BotStates.SCHEDULED)
        self.assertEqual(fatal_error_event.new_state, BotStates.FATAL_ERROR)

        # Verify Kubernetes pod deletion was attempted with the correct pod name
        pod_name = self.bot.k8s_pod_name()
        mock_k8s_api.delete_namespaced_pod.assert_called_once_with(name=pod_name, namespace="attendee", grace_period_seconds=0)

    @patch("bots.models.Bot.create_debug_recording", return_value=False)
    @patch("bots.web_bot_adapter.web_bot_adapter.Display")
    @patch("bots.web_bot_adapter.web_bot_adapter.webdriver.Chrome")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("time.time")
    @patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_bot_can_join_meeting_and_record_with_closed_caption_transcription(
        self,
        mock_deliver_webhook,
        mock_time,
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
            triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE, WebhookTriggerTypes.TRANSCRIPT_UPDATE, WebhookTriggerTypes.CHAT_MESSAGES_UPDATE, WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE],
            is_active=True,
        )

        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Use closed captions for transcription
        self.recording.transcription_provider = TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM
        self.recording.save()

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

        # Patch the controller's on_message_from_adapter method to add debugging
        original_on_message_from_adapter = controller.on_message_from_adapter

        def debug_on_message_from_adapter(message):
            original_on_message_from_adapter(message)
            if message.get("message") == BotAdapter.Messages.BOT_JOINED_MEETING:
                simulate_caption_data_arrival()

        controller.on_message_from_adapter = debug_on_message_from_adapter

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_participants_joining():
            # Simulate the bot joining the meeting
            bot_participant_data = {"deviceId": "bot1", "fullName": "Test Bot", "active": True, "isCurrentUser": True}
            controller.adapter.handle_participant_update(bot_participant_data)

            # Simulate participant joining
            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

        def simulate_participants_leaving():
            # Simulate participant leaving
            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": False, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

        def simulate_caption_data_arrival():
            # Simulate caption data arrival
            caption_data = {"captionId": "caption1", "deviceId": "user1", "text": "This is a test caption from closed captions", "isFinal": 1}
            controller.closed_caption_manager.upsert_caption(caption_data)

            # Force caption processing by flushing
            controller.closed_caption_manager.flush_captions()

            # Simulate chat message arrival
            chat_message_data = {
                "participant_uuid": "user1",
                "message_uuid": "msg123",
                "timestamp": int(current_time * 1000),  # Convert to milliseconds
                "text": "Hello, this is a test chat message!",
                "to_bot": False,
                "additional_data": {"source": "test"},
            }
            controller.on_new_chat_message(chat_message_data)

        def simulate_join_flow():
            nonlocal current_time

            simulate_participants_joining()

            simulate_caption_data_arrival()

            # Simulate receiving audio by updating the last audio message processed time
            controller.adapter.last_audio_message_processed_time = current_time

            # Sleep to allow caption processing
            time.sleep(3)

            simulate_participants_leaving()

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

        # Verify captions were processed as utterances
        utterances = Utterance.objects.filter(recording=self.recording)
        self.assertGreater(utterances.count(), 0)

        # Verify a caption utterance exists with the correct text
        caption_utterance = utterances.filter(source=Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM).first()
        self.assertIsNotNone(caption_utterance)
        self.assertEqual(caption_utterance.transcription.get("transcript"), "This is a test caption from closed captions")

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

        # Verify chat message was created
        chat_messages = ChatMessage.objects.filter(bot=self.bot)
        self.assertGreater(chat_messages.count(), 0, "Expected at least one chat message to be created")

        # Verify the chat message has the correct content
        chat_message = chat_messages.first()
        self.assertEqual(chat_message.text, "Hello, this is a test chat message!")
        self.assertEqual(chat_message.participant.full_name, "Test User")
        self.assertEqual(chat_message.participant.uuid, "user1")

        # Verify webhook delivery attempts were created for chat messages
        chat_webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.CHAT_MESSAGES_UPDATE)
        self.assertGreater(chat_webhook_delivery_attempts.count(), 0, "Expected webhook delivery attempts for chat messages")

        # Verify the chat message webhook payload contains the expected data
        chat_webhook_attempt = chat_webhook_delivery_attempts.first()
        self.assertIsNotNone(chat_webhook_attempt.payload)
        self.assertIn("text", chat_webhook_attempt.payload)
        self.assertIn("sender_name", chat_webhook_attempt.payload)
        self.assertEqual(chat_webhook_attempt.payload["text"], "Hello, this is a test chat message!")

        # Verify Bot Participant was created
        bot_participant = Participant.objects.filter(bot=self.bot, uuid="bot1").first()
        self.assertIsNotNone(bot_participant)
        self.assertEqual(bot_participant.full_name, "Test Bot")
        self.assertEqual(bot_participant.uuid, "bot1")
        self.assertTrue(bot_participant.is_the_bot)

        # Verify User Participant was created
        user_participant = Participant.objects.filter(bot=self.bot, uuid="user1").first()
        self.assertIsNotNone(user_participant)
        self.assertEqual(user_participant.full_name, "Test User")
        self.assertEqual(user_participant.uuid, "user1")
        self.assertFalse(user_participant.is_the_bot)

        # Verify Bot ParticipantEvent was created
        bot_participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="bot1")
        self.assertGreater(bot_participant_events.count(), 0, "Expected at least one participant event to be created")
        join_event = bot_participant_events.filter(event_type=ParticipantEventTypes.JOIN).first()
        self.assertIsNotNone(join_event)
        self.assertEqual(join_event.participant.full_name, "Test Bot")

        # Verify ParticipantEvent was created
        participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="user1")
        self.assertGreater(participant_events.count(), 0, "Expected at least one participant event to be created")
        join_event = participant_events.filter(event_type=ParticipantEventTypes.JOIN).first()
        self.assertIsNotNone(join_event)
        self.assertEqual(join_event.participant.full_name, "Test User")

        leave_event = participant_events.filter(event_type=ParticipantEventTypes.LEAVE).first()
        self.assertIsNotNone(leave_event)
        self.assertEqual(leave_event.participant.full_name, "Test User")

        # Verify webhook for participant event was created
        participant_webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE)
        self.assertGreater(participant_webhook_delivery_attempts.count(), 0, "Expected webhook delivery attempts for participant events")

        participant_webhook_attempts = participant_webhook_delivery_attempts.filter(payload__event_type="join").all()
        self.assertEqual(len(participant_webhook_attempts), 1)
        participant_webhook_attempt = participant_webhook_attempts[0]
        self.assertIsNotNone(participant_webhook_attempt.payload)
        self.assertEqual(participant_webhook_attempt.payload["event_type"], "join")
        self.assertEqual(participant_webhook_attempt.payload["participant_name"], "Test User")

        leave_webhook_attempt = participant_webhook_delivery_attempts.filter(payload__event_type="leave").first()
        self.assertIsNotNone(leave_webhook_attempt)
        self.assertEqual(leave_webhook_attempt.payload["event_type"], "leave")
        self.assertEqual(leave_webhook_attempt.payload["participant_name"], "Test User")

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
    @patch("bots.bot_controller.bot_controller.ScreenAndAudioRecorder.start_recording", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    def test_google_meet_bot_can_join_meeting_and_record_audio_in_mp3_format(
        self,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        mock_start_recording,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        self.bot.settings = {
            "recording_settings": {
                "format": "mp3",
            }
        }
        self.bot.save()

        # Configure the mock uploader to capture data
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

            # Add participants to keep the bot in the meeting
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Let the bot run for a bit to "record"
            time.sleep(3)

            # Trigger auto-leave
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

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

        # Verify file uploader was used. This implies a file was created and handled.
        mock_uploader.upload_file.assert_called_once()

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
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.pause_recording", return_value=True)
    @patch("bots.bot_controller.screen_and_audio_recorder.ScreenAndAudioRecorder.resume_recording", return_value=True)
    @patch("time.time")
    def test_bot_can_pause_and_resume_recording_with_proper_utterance_handling(
        self,
        mock_time,
        mock_pause_recording,
        mock_resume_recording,
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

        # Use closed captions for transcription
        self.recording.transcription_provider = TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM
        self.recording.save()

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

        self.original_recording_started_at = None

        def simulate_pause_resume_flow():
            nonlocal current_time
            # Sleep to allow initialization and joining
            time.sleep(3)

            # Add participants - simulate websocket message processing
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Simulate receiving audio to keep bot alive
            controller.adapter.last_audio_message_processed_time = current_time

            # Wait for bot to be in recording state
            timeout = time.time() + 10
            while time.time() < timeout:
                controller.bot_in_db.refresh_from_db()
                if controller.bot_in_db.state == BotStates.JOINED_RECORDING:
                    break
                time.sleep(0.1)

            # Verify we're in recording state
            controller.bot_in_db.refresh_from_db()
            self.assertEqual(controller.bot_in_db.state, BotStates.JOINED_RECORDING)

            self.original_recording_started_at = controller.bot_in_db.recordings.first().started_at

            # Send closed caption before pause (should create utterance)
            # Simulate caption coming through the web bot adapter
            caption_json_before_pause = {"type": "CaptionUpdate", "caption": {"captionId": "caption1", "deviceId": "user1", "text": "Caption before pause", "isFinal": 1}}
            controller.adapter.handle_caption_update(caption_json_before_pause)

            time.sleep(1)

            # Pause recording
            controller.pause_recording()

            # Wait for pause to take effect
            timeout = time.time() + 5
            while time.time() < timeout:
                controller.bot_in_db.refresh_from_db()
                if controller.bot_in_db.state == BotStates.JOINED_RECORDING_PAUSED:
                    break
                time.sleep(0.1)

            # Verify we're in paused state
            controller.bot_in_db.refresh_from_db()
            self.assertEqual(controller.bot_in_db.state, BotStates.JOINED_RECORDING_PAUSED)

            # Send closed caption during pause (should NOT create utterance)
            # Simulate caption coming through the web bot adapter - this should be ignored due to recording_paused check
            caption_json_during_pause = {"type": "CaptionUpdate", "caption": {"captionId": "caption2", "deviceId": "user1", "text": "Caption during pause", "isFinal": 1}}
            controller.adapter.handle_caption_update(caption_json_during_pause)

            time.sleep(1)

            # Resume recording
            controller.resume_recording()

            # Wait for resume to take effect
            timeout = time.time() + 5
            while time.time() < timeout:
                controller.bot_in_db.refresh_from_db()
                if controller.bot_in_db.state == BotStates.JOINED_RECORDING:
                    break
                time.sleep(0.1)

            # Verify we're back in recording state
            controller.bot_in_db.refresh_from_db()
            self.assertEqual(controller.bot_in_db.state, BotStates.JOINED_RECORDING)

            # Send closed caption after resume (should create utterance)
            # Simulate caption coming through the web bot adapter
            caption_json_after_resume = {"type": "CaptionUpdate", "caption": {"captionId": "caption3", "deviceId": "user1", "text": "Caption after resume", "isFinal": 1}}
            controller.adapter.handle_caption_update(caption_json_after_resume)

            time.sleep(1)

            # Trigger leave to end the test
            controller.adapter.only_one_participant_in_meeting_at = time.time() - 10000000000
            time.sleep(5)

            # Clean up connections in thread
            connection.close()

        # Run simulation after a short delay
        threading.Timer(2, simulate_pause_resume_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=20)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the bot ended properly
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify bot events include pause and resume
        bot_events = self.bot.bot_events.all()
        event_types = [event.event_type for event in bot_events]

        # Check that we have the expected sequence of events including pause and resume
        self.assertIn(BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED, event_types)
        self.assertIn(BotEventTypes.RECORDING_PAUSED, event_types)
        self.assertIn(BotEventTypes.RECORDING_RESUMED, event_types)
        self.assertIn(BotEventTypes.POST_PROCESSING_COMPLETED, event_types)

        # Verify the sequence of recording-related events
        recording_events = [e for e in bot_events if e.event_type in [BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED, BotEventTypes.RECORDING_PAUSED, BotEventTypes.RECORDING_RESUMED]]

        self.assertEqual(len(recording_events), 3)
        self.assertEqual(recording_events[0].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(recording_events[0].old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_events[0].new_state, BotStates.JOINED_RECORDING)

        self.assertEqual(recording_events[1].event_type, BotEventTypes.RECORDING_PAUSED)
        self.assertEqual(recording_events[1].old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(recording_events[1].new_state, BotStates.JOINED_RECORDING_PAUSED)

        self.assertEqual(recording_events[2].event_type, BotEventTypes.RECORDING_RESUMED)
        self.assertEqual(recording_events[2].old_state, BotStates.JOINED_RECORDING_PAUSED)
        self.assertEqual(recording_events[2].new_state, BotStates.JOINED_RECORDING)

        # Verify utterances were created correctly
        utterances = Utterance.objects.filter(recording=self.recording).order_by("created_at")

        # Should have exactly 2 utterances (before pause and after resume, but NOT during pause)
        self.assertEqual(utterances.count(), 2)

        utterance_texts = [utterance.transcription.get("transcript") for utterance in utterances]
        self.assertIn("Caption before pause", utterance_texts)
        self.assertIn("Caption after resume", utterance_texts)
        self.assertNotIn("Caption during pause", utterance_texts)

        # Verify that the recording was completed
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.started_at, self.original_recording_started_at)

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
    @patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_bot_can_join_meeting_with_no_recording_format_and_generate_transcription(
        self,
        mock_deliver_webhook,
        mock_time,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        mock_deliver_webhook.return_value = None

        # Set recording format to "none"
        self.bot.settings = {
            "recording_settings": {
                "format": "none",
            }
        }
        self.bot.save()

        self.webhook_subscription = WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE, WebhookTriggerTypes.TRANSCRIPT_UPDATE, WebhookTriggerTypes.CHAT_MESSAGES_UPDATE, WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE],
            is_active=True,
        )

        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Use closed captions for transcription
        self.recording.transcription_provider = TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM
        self.recording.save()

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

        # Patch the controller's on_message_from_adapter method to add debugging
        original_on_message_from_adapter = controller.on_message_from_adapter

        def debug_on_message_from_adapter(message):
            original_on_message_from_adapter(message)
            if message.get("message") == BotAdapter.Messages.BOT_JOINED_MEETING:
                simulate_caption_data_arrival()

        controller.on_message_from_adapter = debug_on_message_from_adapter

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_participants_joining():
            # Simulate the bot joining the meeting
            bot_participant_data = {"deviceId": "bot1", "fullName": "Test Bot", "active": True, "isCurrentUser": True}
            controller.adapter.handle_participant_update(bot_participant_data)

            # Simulate participant joining
            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

        def simulate_participants_leaving():
            # Simulate participant leaving
            participant_data = {"deviceId": "user1", "fullName": "Test User", "active": False, "isCurrentUser": False}
            controller.adapter.handle_participant_update(participant_data)

        def simulate_caption_data_arrival():
            # Simulate caption data arrival
            caption_data = {"captionId": "caption1", "deviceId": "user1", "text": "This is a test caption with no recording format", "isFinal": 1}
            controller.closed_caption_manager.upsert_caption(caption_data)

            # Force caption processing by flushing
            controller.closed_caption_manager.flush_captions()

            # Simulate chat message arrival
            chat_message_data = {
                "participant_uuid": "user1",
                "message_uuid": "msg123",
                "timestamp": int(current_time * 1000),  # Convert to milliseconds
                "text": "Hello, this is a test chat message with no recording!",
                "to_bot": False,
                "additional_data": {"source": "test"},
            }
            controller.on_new_chat_message(chat_message_data)

        def simulate_join_flow():
            nonlocal current_time

            simulate_participants_joining()

            simulate_caption_data_arrival()

            # Simulate receiving audio by updating the last audio message processed time
            controller.adapter.last_audio_message_processed_time = current_time

            # Sleep to allow caption processing
            time.sleep(3)

            simulate_participants_leaving()

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

        # Verify that the recording was finished even with no recording format
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

        # Verify captions were processed as utterances
        utterances = Utterance.objects.filter(recording=self.recording)
        self.assertGreater(utterances.count(), 0)

        # Verify a caption utterance exists with the correct text
        caption_utterance = utterances.filter(source=Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM).first()
        self.assertIsNotNone(caption_utterance)
        self.assertEqual(caption_utterance.transcription.get("transcript"), "This is a test caption with no recording format")

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

        # Verify chat message was created
        chat_messages = ChatMessage.objects.filter(bot=self.bot)
        self.assertGreater(chat_messages.count(), 0, "Expected at least one chat message to be created")

        # Verify the chat message has the correct content
        chat_message = chat_messages.first()
        self.assertEqual(chat_message.text, "Hello, this is a test chat message with no recording!")
        self.assertEqual(chat_message.participant.full_name, "Test User")
        self.assertEqual(chat_message.participant.uuid, "user1")

        # Verify webhook delivery attempts were created for chat messages
        chat_webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.CHAT_MESSAGES_UPDATE)
        self.assertGreater(chat_webhook_delivery_attempts.count(), 0, "Expected webhook delivery attempts for chat messages")

        # Verify the chat message webhook payload contains the expected data
        chat_webhook_attempt = chat_webhook_delivery_attempts.first()
        self.assertIsNotNone(chat_webhook_attempt.payload)
        self.assertIn("text", chat_webhook_attempt.payload)
        self.assertIn("sender_name", chat_webhook_attempt.payload)
        self.assertEqual(chat_webhook_attempt.payload["text"], "Hello, this is a test chat message with no recording!")

        # Verify Bot Participant was created
        bot_participant = Participant.objects.filter(bot=self.bot, uuid="bot1").first()
        self.assertIsNotNone(bot_participant)
        self.assertEqual(bot_participant.full_name, "Test Bot")
        self.assertEqual(bot_participant.uuid, "bot1")
        self.assertTrue(bot_participant.is_the_bot)

        # Verify User Participant was created
        user_participant = Participant.objects.filter(bot=self.bot, uuid="user1").first()
        self.assertIsNotNone(user_participant)
        self.assertEqual(user_participant.full_name, "Test User")
        self.assertEqual(user_participant.uuid, "user1")
        self.assertFalse(user_participant.is_the_bot)

        # Verify Bot ParticipantEvent was created
        bot_participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="bot1")
        self.assertGreater(bot_participant_events.count(), 0, "Expected at least one participant event to be created")
        join_event = bot_participant_events.filter(event_type=ParticipantEventTypes.JOIN).first()
        self.assertIsNotNone(join_event)
        self.assertEqual(join_event.participant.full_name, "Test Bot")

        # Verify ParticipantEvent was created
        participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__uuid="user1")
        self.assertGreater(participant_events.count(), 0, "Expected at least one participant event to be created")
        join_event = participant_events.filter(event_type=ParticipantEventTypes.JOIN).first()
        self.assertIsNotNone(join_event)
        self.assertEqual(join_event.participant.full_name, "Test User")

        leave_event = participant_events.filter(event_type=ParticipantEventTypes.LEAVE).first()
        self.assertIsNotNone(leave_event)
        self.assertEqual(leave_event.participant.full_name, "Test User")

        # Verify webhook for participant event was created
        participant_webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=self.bot, webhook_trigger_type=WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE)
        self.assertGreater(participant_webhook_delivery_attempts.count(), 0, "Expected webhook delivery attempts for participant events")

        participant_webhook_attempts = participant_webhook_delivery_attempts.filter(payload__event_type="join").all()
        self.assertEqual(len(participant_webhook_attempts), 1)
        participant_webhook_attempt = participant_webhook_attempts[0]
        self.assertIsNotNone(participant_webhook_attempt.payload)
        self.assertEqual(participant_webhook_attempt.payload["event_type"], "join")
        self.assertEqual(participant_webhook_attempt.payload["participant_name"], "Test User")

        leave_webhook_attempt = participant_webhook_delivery_attempts.filter(payload__event_type="leave").first()
        self.assertIsNotNone(leave_webhook_attempt)
        self.assertEqual(leave_webhook_attempt.payload["event_type"], "leave")
        self.assertEqual(leave_webhook_attempt.payload["participant_name"], "Test User")

        # Verify WebSocket media sending was enabled and performance.timeOrigin was queried
        mock_driver.execute_script.assert_has_calls([call("window.ws?.enableMediaSending();"), call("return performance.timeOrigin;")])

        # CRITICAL: Verify file uploader was NOT used since recording format is "none"
        mock_uploader.upload_file.assert_not_called()
        mock_uploader.wait_for_upload.assert_not_called()
        mock_uploader.delete_file.assert_not_called()

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
    def test_bot_uploads_to_external_storage_when_credentials_available(
        self,
        mock_wait_for_host_if_needed,
        mock_check_if_meeting_is_found,
        MockFileUploader,
        MockChromeDriver,
        MockDisplay,
        mock_create_debug_recording,
    ):
        # Configure external media storage settings on the bot
        self.bot.settings = {
            "external_media_storage_settings": {
                "bucket_name": "my-external-bucket",
                "recording_file_name": "custom-recording-name.mp4",
            }
        }
        self.bot.save()

        # Create external media storage credentials
        external_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE)
        external_credentials.set_credentials({"access_key_id": "test_access_key", "access_key_secret": "test_secret_key", "endpoint_url": "https://s3.amazonaws.com", "region_name": "us-east-1"})

        # Configure the mock uploader for both regular and external storage
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

            # Add participants to keep the bot in the meeting
            controller.adapter.participants_info["user1"] = {"deviceId": "user1", "fullName": "Test User", "active": True, "isCurrentUser": False}

            # Let the bot run for a bit to "record"
            time.sleep(3)

            # Trigger auto-leave
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

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

        # Verify file uploader was called multiple times - once for external storage and once for regular storage
        # The external storage upload happens first, then the regular upload
        self.assertEqual(mock_uploader.upload_file.call_count, 2, "FileUploader.upload_file should be called twice - once for external storage and once for regular storage")
        self.assertEqual(mock_uploader.wait_for_upload.call_count, 2, "FileUploader.wait_for_upload should be called twice")

        # Verify FileUploader was instantiated twice with different parameters
        self.assertEqual(MockFileUploader.call_count, 2, "FileUploader should be instantiated twice")

        # Check the first call (external storage)
        external_call_args = MockFileUploader.call_args_list[0]
        external_call_kwargs = external_call_args.kwargs
        self.assertEqual(external_call_kwargs["bucket"], "my-external-bucket")
        self.assertEqual(external_call_kwargs["key"], "custom-recording-name.mp4")
        self.assertEqual(external_call_kwargs["endpoint_url"], "https://s3.amazonaws.com")
        self.assertEqual(external_call_kwargs["region_name"], "us-east-1")
        self.assertEqual(external_call_kwargs["access_key_id"], "test_access_key")
        self.assertEqual(external_call_kwargs["access_key_secret"], "test_secret_key")

        # Check the second call (regular storage) - should use environment variables
        regular_call_args = MockFileUploader.call_args_list[1]
        regular_call_kwargs = regular_call_args.kwargs
        self.assertEqual(regular_call_kwargs["bucket"], "test-bucket")  # From environment variable set in setUpClass
        self.assertIsNotNone(regular_call_kwargs["key"])  # Should have some recording filename

        # Verify only one delete_file call (for the regular storage uploader)
        mock_uploader.delete_file.assert_called_once()

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()
