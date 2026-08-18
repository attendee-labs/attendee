import threading
import uuid
from unittest.mock import PropertyMock, patch

from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.test import Client, TransactionTestCase
from django.test.utils import override_settings
from rest_framework import status

from bots.models import ApiKey, AudioChunk, Bot, BotDebugScreenshot, BotEvent, BotEventTypes, BotStates, ChatMessage, ChatMessageToOptions, Organization, Participant, ParticipantEvent, ParticipantEventTypes, Project, Recording, RecordingStates, Utterance, WebhookDeliveryAttempt, WebhookSubscription, WebhookTriggerTypes


def mock_file_field_delete_sets_name_to_none(instance, save=True):
    """
    A side_effect function for mocking FieldFile.delete.
    Sets the FieldFile's name to None and saves the parent model instance.
    """
    # 'instance' here is the FieldFile instance being deleted
    instance.name = None
    if save:
        # instance.instance refers to the model instance (e.g., Recording)
        # that owns this FieldFile.
        instance.instance.save()


def mock_file_field_save(instance, name, content, save=True):
    """
    A side_effect function for mocking FieldFile.save.
    Sets the FieldFile's name to the provided name and saves the parent model instance.
    """
    # 'instance' here is the FieldFile instance being saved
    instance.name = name
    if save:
        # instance.instance refers to the model instance (e.g., Recording)
        # that owns this FieldFile.
        instance.instance.save()


class TestBotDataDeletion(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings_override = override_settings(AWS_RECORDING_STORAGE_BUCKET_NAME="test-bucket")
        cls.settings_override.enable()

    def setUp(self):
        # Setup patches
        self.bucket_mock_patch = patch("storages.backends.s3boto3.S3Boto3Storage.bucket", new_callable=PropertyMock)
        self.delete_patch = patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
        self.save_patch = patch("django.db.models.fields.files.FieldFile.save", autospec=True)

        # Start patches
        self.bucket_mock = self.bucket_mock_patch.start()
        self.delete_mock = self.delete_patch.start()
        self.save_mock = self.save_patch.start()

        # Set side effects
        self.delete_mock.side_effect = mock_file_field_delete_sets_name_to_none
        self.save_mock.side_effect = mock_file_field_save

        # Create test organization
        self.organization = Organization.objects.create(name="Test Org")

        # Create test project
        self.project = Project.objects.create(organization=self.organization, name="Test Project")

        # Create two test bots
        self.bot1 = Bot.objects.create(project=self.project, name="Bot One", meeting_url="https://test.com/meeting1", state=BotStates.ENDED)

        self.bot2 = Bot.objects.create(project=self.project, name="Bot Two", meeting_url="https://test.com/meeting2", state=BotStates.ENDED)

        # Create participants for each bot
        self.participant1 = Participant.objects.create(bot=self.bot1, uuid="participant1", full_name="Test Participant 1")

        self.participant2 = Participant.objects.create(bot=self.bot2, uuid="participant2", full_name="Test Participant 2")

        # Create webhook subscriptions for each bot
        self.webhook_subscription1 = WebhookSubscription.objects.create(project=self.project, bot=self.bot1, url="https://test.com/webhook1", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])
        self.webhook_subscription2 = WebhookSubscription.objects.create(project=self.project, bot=self.bot2, url="https://test.com/webhook2", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])
        self.project_webhook_subscription = WebhookSubscription.objects.create(project=self.project, url="https://test.com/project_webhook", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        # Create webhook delivery attempts for each bot
        self.webhook_delivery_attempt1 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE, idempotency_key=uuid.uuid4(), webhook_subscription=self.webhook_subscription1, bot=self.bot1, payload={"test": "test"})
        self.webhook_delivery_attempt2 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE, idempotency_key=uuid.uuid4(), webhook_subscription=self.webhook_subscription2, bot=self.bot2, payload={"test": "test"})
        self.sensitive_webhook_delivery_attempt1 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE, idempotency_key=uuid.uuid4(), webhook_subscription=self.webhook_subscription1, bot=self.bot1, payload={"test": "test"})
        self.sensitive_webhook_delivery_attempt2 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE, idempotency_key=uuid.uuid4(), webhook_subscription=self.webhook_subscription2, bot=self.bot2, payload={"test": "test"})

        self.project_webhook_delivery_attempt = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE, idempotency_key=uuid.uuid4(), webhook_subscription=self.project_webhook_subscription, bot=self.bot1, payload={"test": "test"})
        self.project_webhook_delivery_attempt2 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE, idempotency_key=uuid.uuid4(), webhook_subscription=self.project_webhook_subscription, bot=self.bot2, payload={"test": "test"})
        self.project_sensitive_webhook_delivery_attempt = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE, idempotency_key=uuid.uuid4(), webhook_subscription=self.project_webhook_subscription, bot=self.bot1, payload={"test": "test"})
        self.project_sensitive_webhook_delivery_attempt2 = WebhookDeliveryAttempt.objects.create(webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE, idempotency_key=uuid.uuid4(), webhook_subscription=self.project_webhook_subscription, bot=self.bot2, payload={"test": "test"})

        # Create participant events for each participant
        self.participant_event1 = ParticipantEvent.objects.create(participant=self.participant1, event_type=ParticipantEventTypes.JOIN, timestamp_ms=1000)
        self.participant_event2 = ParticipantEvent.objects.create(participant=self.participant2, event_type=ParticipantEventTypes.JOIN, timestamp_ms=1000)
        self.participant_event3 = ParticipantEvent.objects.create(participant=self.participant1, event_type=ParticipantEventTypes.LEAVE, timestamp_ms=1000)
        self.participant_event4 = ParticipantEvent.objects.create(participant=self.participant2, event_type=ParticipantEventTypes.LEAVE, timestamp_ms=1000)

        # Create recordings for each bot
        self.recording1 = Recording.objects.create(bot=self.bot1, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        # Add a file to the recording
        self.recording1.file.save("test1.mp4", ContentFile(b"test content 1"))

        self.recording2 = Recording.objects.create(bot=self.bot2, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        # Add a file to the recording
        self.recording2.file.save("test2.mp4", ContentFile(b"test content 2"))

        # Create audio chunks and utterances for each recording
        self.audio_chunk1 = AudioChunk.objects.create(recording=self.recording1, participant=self.participant1, audio_blob=b"test audio 1", timestamp_ms=1000, duration_ms=500, sample_rate=16000)
        self.audio_chunk2 = AudioChunk.objects.create(recording=self.recording2, participant=self.participant2, audio_blob=b"test audio 2", timestamp_ms=1000, duration_ms=500, sample_rate=16000)
        self.utterance1 = Utterance.objects.create(recording=self.recording1, participant=self.participant1, audio_chunk=self.audio_chunk1, timestamp_ms=1000, duration_ms=500)
        self.utterance2 = Utterance.objects.create(recording=self.recording2, participant=self.participant2, audio_chunk=self.audio_chunk2, timestamp_ms=1000, duration_ms=500)

        # Create chat messages for each bot
        self.chat_message1 = ChatMessage.objects.create(bot=self.bot1, to=ChatMessageToOptions.ONLY_BOT, participant=self.participant1, text="Hello, world!", timestamp=1000)
        self.chat_message2 = ChatMessage.objects.create(bot=self.bot2, to=ChatMessageToOptions.EVERYONE, participant=self.participant2, text="Hello, world!", timestamp=1000)

        # Create bot events and debug screenshots for each bot
        self.event1 = BotEvent.objects.create(bot=self.bot1, old_state=BotStates.ENDED, new_state=BotStates.ENDED, event_type=BotEventTypes.BOT_JOINED_MEETING)
        self.event2 = BotEvent.objects.create(bot=self.bot2, old_state=BotStates.ENDED, new_state=BotStates.ENDED, event_type=BotEventTypes.BOT_JOINED_MEETING)

        # Create debug screenshots for each event
        self.screenshot1 = BotDebugScreenshot.objects.create(bot_event=self.event1)
        self.screenshot1.file.save("test1.png", ContentFile(b"test screenshot 1"))

        self.screenshot2 = BotDebugScreenshot.objects.create(bot_event=self.event2)
        self.screenshot2.file.save("test2.png", ContentFile(b"test screenshot 2"))

    def tearDown(self):
        # Stop all patches
        self.save_patch.stop()
        self.delete_patch.stop()
        self.bucket_mock_patch.stop()

    def test_delete_data_deletes_specific_bot_data_only(self):
        """Test that deleting data for one bot doesn't affect other bots"""
        # Verify initial state
        self.assertEqual(Bot.objects.count(), 2)
        self.assertEqual(Participant.objects.count(), 2)
        self.assertEqual(Recording.objects.count(), 2)
        self.assertEqual(AudioChunk.objects.count(), 2)
        self.assertEqual(Utterance.objects.count(), 2)
        self.assertEqual(BotDebugScreenshot.objects.count(), 2)
        self.assertEqual(WebhookSubscription.objects.count(), 3)
        self.assertEqual(WebhookDeliveryAttempt.objects.count(), 8)
        self.assertEqual(WebhookDeliveryAttempt.objects.filter(bot=self.bot1, webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE).count(), 2)

        # Delete data for bot1
        self.bot1.delete_data()

        # Verify bot1's data is deleted
        self.assertEqual(Participant.objects.filter(bot=self.bot1).count(), 0)
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 0)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 0)
        self.assertEqual(ChatMessage.objects.filter(bot=self.bot1).count(), 0)
        self.assertEqual(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).count(), 0)
        self.assertEqual(ParticipantEvent.objects.filter(participant__bot=self.bot1).count(), 0)
        self.assertEqual(WebhookDeliveryAttempt.objects.filter(bot=self.bot1, webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE).count(), 3)
        self.assertEqual(WebhookDeliveryAttempt.objects.filter(bot=self.bot1, webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE).count(), 0)

        # Verify project level webhook subscription still exists
        self.assertEqual(WebhookSubscription.objects.filter(project=self.project, bot__isnull=True).count(), 1)

        # Verify bot1's webhook subscription still exists
        self.assertEqual(WebhookSubscription.objects.filter(bot=self.bot1).count(), 1)

        # Verify bot2's data is still intact
        self.assertEqual(Participant.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(Recording.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot2).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot2).count(), 1)
        self.assertEqual(ChatMessage.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot2).count(), 1)
        self.assertEqual(ParticipantEvent.objects.filter(participant__bot=self.bot2).count(), 2)
        self.assertEqual(WebhookSubscription.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(WebhookDeliveryAttempt.objects.filter(bot=self.bot2, webhook_trigger_type=WebhookTriggerTypes.BOT_STATE_CHANGE).count(), 2)
        self.assertEqual(WebhookDeliveryAttempt.objects.filter(bot=self.bot2, webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE).count(), 2)

        # Verify bot2's state is still ENDED
        self.bot2.refresh_from_db()
        self.assertEqual(self.bot2.state, BotStates.ENDED)

        # Verify bot1's state changed to DATA_DELETED
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)

        # Verify event was created
        event = self.bot1.bot_events.filter(event_type=BotEventTypes.DATA_DELETED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.old_state, BotStates.ENDED)
        self.assertEqual(event.new_state, BotStates.DATA_DELETED)

    def test_delete_data_invalid_state(self):
        """Test that delete_data raises an error if bot is not in a valid state"""
        # Change bot state to one that's not valid for data deletion
        self.bot1.state = BotStates.JOINED_RECORDING
        self.bot1.save()

        # Verify delete_data raises ValueError
        with self.assertRaises(ValueError):
            self.bot1.delete_data()

        # Verify no data was deleted
        self.assertEqual(Participant.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 1)

    def test_delete_data_deletes_recording_and_debug_files_from_storage(self):
        """Test that delete_data deletes the bot's recording and debug files from storage"""
        self.delete_mock.reset_mock()

        self.bot1.delete_data()

        deleted_file_owners = {(type(call.args[0].instance), call.args[0].instance.pk) for call in self.delete_mock.call_args_list}
        debug_file_delete_call = next(call for call in self.delete_mock.call_args_list if isinstance(call.args[0].instance, BotDebugScreenshot))
        self.assertEqual(self.delete_mock.call_count, 2)
        self.assertEqual(debug_file_delete_call.kwargs, {"save": False})
        self.assertEqual(
            deleted_file_owners,
            {
                (Recording, self.recording1.pk),
                (BotDebugScreenshot, self.screenshot1.pk),
            },
        )

    def test_delete_data_is_idempotent(self):
        """Test that retrying delete_data succeeds without creating another event"""
        self.bot1.delete_data()
        self.bot1.delete_data()

        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)
        self.assertEqual(self.bot1.bot_events.filter(event_type=BotEventTypes.DATA_DELETED).count(), 1)

    def test_debug_artifact_cannot_be_created_after_data_deletion(self):
        """Test that cleanup cannot create a new artifact after delete_data succeeds"""
        self.bot1.delete_data()
        data_deleted_event = self.bot1.bot_events.get(event_type=BotEventTypes.DATA_DELETED)

        with self.assertRaises(ValueError):
            BotDebugScreenshot.create_with_file(
                bot_event=data_deleted_event,
                filename_template="late_debug_recording_{object_id}.mp4",
                content=ContentFile(b"late debug recording"),
            )

        self.assertEqual(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).count(), 0)

    def test_direct_late_file_save_is_removed_after_data_deletion(self):
        """Test that a stale artifact instance cannot leave a late-uploaded file behind"""
        late_artifact = BotDebugScreenshot.objects.create(bot_event=self.event1)
        self.bot1.delete_data()
        self.delete_mock.reset_mock()

        with self.assertRaises(ValueError):
            late_artifact.file.save("late_debug_recording.mp4", ContentFile(b"late debug recording"))

        self.assertEqual(self.delete_mock.call_count, 1)
        self.assertEqual(self.delete_mock.call_args.kwargs, {"save": False})
        self.assertFalse(BotDebugScreenshot.objects.filter(pk=late_artifact.pk).exists())

    def test_delete_data_waits_for_in_progress_debug_upload(self):
        """Test that artifact upload and deletion serialize on the bot row"""
        upload_started = threading.Event()
        release_upload = threading.Event()
        deletion_finished = threading.Event()
        thread_errors = []

        def blocking_file_save(instance, name, content, save=True):
            if name.startswith("concurrent_debug_recording_"):
                upload_started.set()
                if not release_upload.wait(timeout=5):
                    raise TimeoutError("timed out waiting to release the test upload")
            mock_file_field_save(instance, name, content, save=save)

        self.save_mock.side_effect = blocking_file_save

        def create_artifact():
            close_old_connections()
            try:
                event = BotEvent.objects.get(pk=self.event1.pk)
                BotDebugScreenshot.create_with_file(
                    bot_event=event,
                    filename_template="concurrent_debug_recording_{object_id}.mp4",
                    content=ContentFile(b"concurrent debug recording"),
                )
            except Exception as exc:
                thread_errors.append(exc)
            finally:
                close_old_connections()

        def delete_data():
            close_old_connections()
            try:
                Bot.objects.get(pk=self.bot1.pk).delete_data()
            except Exception as exc:
                thread_errors.append(exc)
            finally:
                deletion_finished.set()
                close_old_connections()

        artifact_thread = threading.Thread(target=create_artifact)
        artifact_thread.start()
        self.assertTrue(upload_started.wait(timeout=5))

        deletion_thread = threading.Thread(target=delete_data)
        deletion_thread.start()
        self.assertFalse(deletion_finished.wait(timeout=0.2))

        release_upload.set()
        artifact_thread.join(timeout=5)
        deletion_thread.join(timeout=5)

        self.assertFalse(artifact_thread.is_alive())
        self.assertFalse(deletion_thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)
        self.assertFalse(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).exists())

    def test_debug_upload_waits_for_deletion_and_is_rejected(self):
        """Test that an upload waiting behind deletion observes DATA_DELETED"""
        deletion_holds_lock = threading.Event()
        release_deletion = threading.Event()
        upload_finished = threading.Event()
        upload_rejected = threading.Event()
        thread_errors = []

        def blocking_file_delete(instance, save=True):
            if isinstance(instance.instance, BotDebugScreenshot) and not deletion_holds_lock.is_set():
                deletion_holds_lock.set()
                if not release_deletion.wait(timeout=5):
                    raise TimeoutError("timed out waiting to release deletion")
            mock_file_field_delete_sets_name_to_none(instance, save=save)

        self.delete_mock.side_effect = blocking_file_delete

        def delete_data():
            close_old_connections()
            try:
                Bot.objects.get(pk=self.bot1.pk).delete_data()
            except Exception as exc:
                thread_errors.append(exc)
            finally:
                close_old_connections()

        def create_artifact():
            close_old_connections()
            try:
                event = BotEvent.objects.get(pk=self.event1.pk)
                BotDebugScreenshot.create_with_file(
                    bot_event=event,
                    filename_template="too_late_debug_recording_{object_id}.mp4",
                    content=ContentFile(b"too late debug recording"),
                )
            except ValueError:
                upload_rejected.set()
            except Exception as exc:
                thread_errors.append(exc)
            finally:
                upload_finished.set()
                close_old_connections()

        deletion_thread = threading.Thread(target=delete_data)
        deletion_thread.start()
        self.assertTrue(deletion_holds_lock.wait(timeout=5))

        artifact_thread = threading.Thread(target=create_artifact)
        artifact_thread.start()
        self.assertFalse(upload_finished.wait(timeout=0.2))

        release_deletion.set()
        deletion_thread.join(timeout=5)
        artifact_thread.join(timeout=5)

        self.assertFalse(deletion_thread.is_alive())
        self.assertFalse(artifact_thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertTrue(upload_rejected.is_set())
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)
        self.assertFalse(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).exists())

    def test_delete_data_can_retry_after_storage_failure(self):
        """Test that a partial storage failure rolls back database changes and can be retried"""
        failed_once = False

        def fail_recording_delete_once(instance, save=True):
            nonlocal failed_once
            if isinstance(instance.instance, Recording) and not failed_once:
                failed_once = True
                raise OSError("storage unavailable")
            mock_file_field_delete_sets_name_to_none(instance, save=save)

        self.delete_mock.side_effect = fail_recording_delete_once
        with self.assertRaises(OSError):
            self.bot1.delete_data()

        self.bot1.refresh_from_db()
        self.screenshot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.ENDED)
        self.assertTrue(self.screenshot1.file)
        self.assertTrue(Participant.objects.filter(bot=self.bot1).exists())

        self.delete_mock.side_effect = mock_file_field_delete_sets_name_to_none
        self.bot1.delete_data()
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)
        self.assertFalse(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).exists())

    def test_delete_data_removes_debug_rows_without_files(self):
        """Test that empty debug artifact rows are removed without a storage call"""
        empty_artifact = BotDebugScreenshot.objects.create(bot_event=self.event1)
        self.delete_mock.reset_mock()

        self.bot1.delete_data()

        deleted_file_owners = {(type(call.args[0].instance), call.args[0].instance.pk) for call in self.delete_mock.call_args_list}
        self.assertNotIn((BotDebugScreenshot, empty_artifact.pk), deleted_file_owners)
        self.assertFalse(BotDebugScreenshot.objects.filter(pk=empty_artifact.pk).exists())

    def test_delete_data_multiple_recordings(self):
        """Test that delete_data deletes data from multiple recordings"""
        # Create another recording for bot1
        recording1b = Recording.objects.create(bot=self.bot1, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        recording1b.file.save("test1b.mp4", ContentFile(b"test content 1b"))

        # Create utterance for the new recording
        audio_chunk1b = AudioChunk.objects.create(recording=recording1b, participant=self.participant1, audio_blob=b"test audio 1b", timestamp_ms=2000, duration_ms=500, sample_rate=16000)
        Utterance.objects.create(recording=recording1b, participant=self.participant1, audio_chunk=audio_chunk1b, timestamp_ms=2000, duration_ms=500)

        # Initial count
        self.assertEqual(Recording.objects.filter(bot=self.bot1).count(), 2)
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 2)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 2)
        self.assertEqual(AudioChunk.objects.filter(participant__bot=self.bot1).count(), 2)

        # Delete data for bot1
        self.bot1.delete_data()

        # Verify all of bot1's data is deleted
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 0)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 0)
        self.assertEqual(AudioChunk.objects.filter(participant__bot=self.bot1).count(), 0)

        # Verify recording files are deleted but records still exist
        self.bot1.refresh_from_db()
        self.assertEqual(Recording.objects.filter(bot=self.bot1).count(), 2)

        # Check files are deleted
        for recording in Recording.objects.filter(bot=self.bot1):
            self.assertFalse(recording.file)

    def test_fatal_error_to_data_deleted_transition(self):
        """Test that a bot in FATAL_ERROR state can transition to DATA_DELETED"""
        # Change bot state to FATAL_ERROR
        self.bot1.state = BotStates.FATAL_ERROR
        self.bot1.save()

        # Delete data
        self.bot1.delete_data()

        # Verify state changed to DATA_DELETED
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)

    def test_delete_transcript_deletes_specific_bot_utterances_only(self):
        """Test that DELETE /transcript only deletes utterances for the specified bot"""
        # Create API key for the project
        api_key, api_key_plain = ApiKey.create(project=self.project, name="Test API Key")

        # Set recordings as default recordings (required by the endpoint)
        self.recording1.is_default_recording = True
        self.recording1.save()
        self.recording2.is_default_recording = True
        self.recording2.save()

        # Add transcriptions to utterances so they are considered transcript entries
        self.utterance1.transcription = {"transcript": "Hello from bot 1"}
        self.utterance1.save()
        self.utterance2.transcription = {"transcript": "Hello from bot 2"}
        self.utterance2.save()

        # Verify initial state
        self.assertEqual(Utterance.objects.filter(recording=self.recording1).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording=self.recording2).count(), 1)

        # Make DELETE request for bot1's transcript
        client = Client()
        response = client.delete(
            f"/api/v1/bots/{self.bot1.object_id}/transcript",
            HTTP_AUTHORIZATION=f"Token {api_key_plain}",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify bot1's utterances are deleted
        self.assertEqual(Utterance.objects.filter(recording=self.recording1).count(), 0)

        # Verify bot2's utterances are NOT deleted
        self.assertEqual(Utterance.objects.filter(recording=self.recording2).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording=self.recording2).first().transcription, {"transcript": "Hello from bot 2"})
