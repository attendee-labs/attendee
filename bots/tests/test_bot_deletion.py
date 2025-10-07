from unittest.mock import PropertyMock, patch

from django.core.files.base import ContentFile
from django.test import TransactionTestCase
from django.test.utils import override_settings

from bots.models import AudioChunk, Bot, BotDebugScreenshot, BotEvent, BotEventTypes, BotStates, ChatMessage, ChatMessageToOptions, Organization, Participant, Project, Recording, RecordingStates, Utterance, WebhookSubscription, WebhookTriggerTypes


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


class TestBotDeletion(TransactionTestCase):
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
        self.webhook_subscription1 = WebhookSubscription.objects.create(bot=self.bot1, project=self.project, url="https://test.com/webhook1", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])
        self.webhook_subscription2 = WebhookSubscription.objects.create(bot=self.bot2, project=self.project, url="https://test.com/webhook2", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        # Create project level webhook subscription
        self.project_webhook_subscription = WebhookSubscription.objects.create(project=self.project, url="https://test.com/project_webhook", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

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

    def test_hard_delete_bot_with_protected_references_fails(self):
        """Test that hard deleting a bot fails when there are PROTECT constraints (Utterances)"""
        from django.db.models import ProtectedError

        # Verify initial state - bot1 has utterances that reference participants
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 1)
        self.assertEqual(Utterance.objects.filter(participant__bot=self.bot1).count(), 1)

        # Try to hard delete bot1 - this should fail due to Utterance -> Participant PROTECT constraint
        with self.assertRaises(ProtectedError):
            self.bot1.delete()

        # Verify bot1 still exists
        self.assertTrue(Bot.objects.filter(id=self.bot1.id).exists())

        # Verify all related data still exists
        self.assertEqual(Participant.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(Recording.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(AudioChunk.objects.filter(recording__bot=self.bot1).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 1)
        self.assertEqual(ChatMessage.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(BotEvent.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(BotDebugScreenshot.objects.filter(bot_event__bot=self.bot1).count(), 1)
        self.assertEqual(WebhookSubscription.objects.filter(bot=self.bot1).count(), 1)

    def test_hard_delete_bot_with_credit_transactions_fails(self):
        """Test that hard deleting a bot fails when there are CreditTransaction references (PROTECT)"""
        from django.db.models import ProtectedError

        from bots.models import CreditTransactionManager

        # Create a credit transaction for bot1
        CreditTransactionManager.create_transaction(organization=self.organization, centicredits_delta=-100, bot=self.bot1, description="Test transaction")

        # Verify credit transaction exists
        self.assertEqual(self.bot1.credit_transactions.count(), 1)

        # Remove utterances first to avoid that PROTECT constraint
        Utterance.objects.filter(recording__bot=self.bot1).delete()

        # Try to hard delete bot1 - this should fail due to CreditTransaction -> Bot PROTECT constraint
        with self.assertRaises(ProtectedError):
            self.bot1.delete()

        # Verify bot1 still exists
        self.assertTrue(Bot.objects.filter(id=self.bot1.id).exists())

        # Verify credit transaction still exists
        self.assertEqual(self.bot1.credit_transactions.count(), 1)

        # Verify webhook subscription still exists
        self.assertEqual(WebhookSubscription.objects.filter(bot=self.bot1).count(), 1)

    def test_hard_delete_clean_bot_success(self):
        """Test that hard deleting a bot succeeds when there are no PROTECT constraints"""
        # Create a clean bot without utterances or credit transactions
        clean_bot = Bot.objects.create(project=self.project, name="Clean Bot", meeting_url="https://test.com/clean", state=BotStates.ENDED)

        # Create some CASCADE-related objects
        clean_participant = Participant.objects.create(bot=clean_bot, uuid="clean_participant", full_name="Clean Participant")

        clean_recording = Recording.objects.create(bot=clean_bot, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)

        clean_chat_message = ChatMessage.objects.create(bot=clean_bot, to=ChatMessageToOptions.EVERYONE, participant=clean_participant, text="Clean message", timestamp=1000)

        clean_event = BotEvent.objects.create(bot=clean_bot, old_state=BotStates.ENDED, new_state=BotStates.ENDED, event_type=BotEventTypes.BOT_JOINED_MEETING)

        clean_screenshot = BotDebugScreenshot.objects.create(bot_event=clean_event)

        clean_webhook_subscription = WebhookSubscription.objects.create(bot=clean_bot, project=self.project, url="https://test.com/clean", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        # Store counts before deletion
        initial_bot_count = Bot.objects.count()
        initial_participant_count = Participant.objects.count()
        initial_recording_count = Recording.objects.count()
        initial_chat_message_count = ChatMessage.objects.count()
        initial_bot_event_count = BotEvent.objects.count()
        initial_screenshot_count = BotDebugScreenshot.objects.count()
        initial_webhook_subscription_count = WebhookSubscription.objects.count()
        # Hard delete the clean bot - this should succeed
        clean_bot.delete()

        # Verify bot is deleted
        self.assertFalse(Bot.objects.filter(id=clean_bot.id).exists())
        self.assertEqual(Bot.objects.count(), initial_bot_count - 1)

        # Verify CASCADE deletions occurred
        self.assertFalse(Participant.objects.filter(id=clean_participant.id).exists())
        self.assertFalse(Recording.objects.filter(id=clean_recording.id).exists())
        self.assertFalse(ChatMessage.objects.filter(id=clean_chat_message.id).exists())
        self.assertFalse(BotEvent.objects.filter(id=clean_event.id).exists())
        self.assertFalse(BotDebugScreenshot.objects.filter(id=clean_screenshot.id).exists())
        self.assertFalse(WebhookSubscription.objects.filter(id=clean_webhook_subscription.id).exists())

        # Verify total counts are reduced by expected amounts
        self.assertEqual(Participant.objects.count(), initial_participant_count - 1)
        self.assertEqual(Recording.objects.count(), initial_recording_count - 1)
        self.assertEqual(ChatMessage.objects.count(), initial_chat_message_count - 1)
        self.assertEqual(BotEvent.objects.count(), initial_bot_event_count - 1)
        self.assertEqual(BotDebugScreenshot.objects.count(), initial_screenshot_count - 1)
        self.assertEqual(WebhookSubscription.objects.count(), initial_webhook_subscription_count - 1)
        self.assertEqual(WebhookSubscription.objects.filter(project=self.project, bot__isnull=True).count(), 1)
        # Verify other bots' data is untouched
        self.assertTrue(Bot.objects.filter(id=self.bot1.id).exists())
        self.assertTrue(Bot.objects.filter(id=self.bot2.id).exists())

    def test_hard_delete_cascade_relationships_summary(self):
        """Test that documents the expected cascade behavior when deleting a bot"""
        # This test serves as documentation for the expected behavior

        # CASCADE relationships (will be deleted when bot is deleted):
        # - Participant (bot -> CASCADE)
        # - Recording (bot -> CASCADE)
        # - BotEvent (bot -> CASCADE)
        # - BotDebugScreenshot (bot_event -> CASCADE, so indirectly deleted)
        # - ChatMessage (bot -> CASCADE)
        # - BotMediaRequest (bot -> CASCADE)
        # - BotChatMessageRequest (bot -> CASCADE)
        # - WebhookSubscription (bot -> CASCADE)

        # PROTECT relationships (will prevent bot deletion):
        # - CreditTransaction (bot -> PROTECT)
        # - Utterance (participant -> PROTECT, so indirectly protects bot)

        # SET_NULL relationships (will be set to NULL when bot is deleted):
        # - WebhookDeliveryAttempt (bot -> SET_NULL)

        # This test just validates our understanding is correct
        clean_bot = Bot.objects.create(project=self.project, name="Documentation Bot", meeting_url="https://test.com/docs", state=BotStates.ENDED)

        # Verify we can delete a clean bot
        clean_bot.delete()
        self.assertFalse(Bot.objects.filter(id=clean_bot.id).exists())
