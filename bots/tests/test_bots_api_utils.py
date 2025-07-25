from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.bots_api_utils import BotCreationSource, create_bot, create_webhook_subscription, validate_meeting_url_and_credentials
from bots.models import Bot, BotEventTypes, BotStates, Credentials, Project, TranscriptionProviders, WebhookSubscription, WebhookTriggerTypes


class TestValidateMeetingUrlAndCredentials(TestCase):
    def setUp(self):
        # Create organization first since it's required for Project
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def test_validate_google_meet_url(self):
        """Test Google Meet URL validation"""
        # Valid Google Meet URL
        error = validate_meeting_url_and_credentials("https://meet.google.com/abc-defg-hij", self.project)
        self.assertIsNone(error)

    def test_validate_zoom_url_and_credentials(self):
        """Test Zoom URL and credentials validation"""
        # Test Zoom URL without credentials
        error = validate_meeting_url_and_credentials("https://zoom.us/j/123456789", self.project)
        self.assertEqual(error, {"error": f"Zoom App credentials are required to create a Zoom bot. Please add Zoom credentials at https://app.attendee.dev/projects/{self.project.object_id}/credentials"})

    def test_validate_teams_url(self):
        """Test Teams URL validation"""
        # Teams URLs don't require specific validation
        error = validate_meeting_url_and_credentials("https://teams.microsoft.com/meeting/123", self.project)
        self.assertIsNone(error)


class TestCreateBot(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def test_create_bot(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot"}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNone(error)

    def test_create_zoom_bot_with_default_settings(self):
        Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        bot, error = create_bot(data={"meeting_url": "https://zoom.us/j/123456789", "bot_name": "Test Bot"}, source=BotCreationSource.API, project=self.project)
        print("error", error)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNone(error)
        self.assertEqual(bot.recordings.first().transcription_provider, TranscriptionProviders.DEEPGRAM)
        self.assertEqual(bot.use_zoom_web_adapter(), False)

    def test_create_zoom_bot_with_default_settings_and_web_adapter(self):
        Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        bot, error = create_bot(data={"meeting_url": "https://zoom.us/j/123456789", "bot_name": "Test Bot", "zoom_settings": {"sdk": "web"}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNone(error)
        self.assertEqual(bot.recordings.first().transcription_provider, TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM)
        self.assertEqual(bot.use_zoom_web_adapter(), True)

    def test_create_bot_with_explicit_transcription_settings(self):
        """Test creating bots with explicit transcription settings for different providers and meeting types"""

        # Test Google Meet bot with Assembly AI transcription settings
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "transcription_settings": {"assembly_ai": {"language_code": "en", "speech_model": "best"}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNone(error)
        self.assertEqual(bot.recordings.first().transcription_provider, TranscriptionProviders.ASSEMBLY_AI)

        # Test Zoom bot with explicit closed captions (requires credentials and web SDK)
        Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        bot2, error2 = create_bot(data={"meeting_url": "https://zoom.us/j/987654321", "bot_name": "Zoom CC Test Bot", "zoom_settings": {"sdk": "web"}, "transcription_settings": {"meeting_closed_captions": {"zoom_language": "Spanish"}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot2)
        self.assertIsNotNone(bot2.recordings.first())
        self.assertIsNone(error2)
        self.assertEqual(bot2.recordings.first().transcription_provider, TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM)
        self.assertEqual(bot2.use_zoom_web_adapter(), True)

    def test_create_bot_with_image(self):
        bot, error = create_bot(data={"meeting_url": "https://teams.microsoft.com/meeting/123", "bot_name": "Test Bot", "bot_image": {"type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNotNone(bot.media_requests.first())
        self.assertIsNone(error)
        events = bot.bot_events
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().metadata["source"], BotCreationSource.API)
        self.assertEqual(events.first().event_type, BotEventTypes.JOIN_REQUESTED)

    def test_create_bot_with_valid_redaction_settings(self):
        """Test creating a bot with valid redaction settings."""
        # Test with single redaction type
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot with PII Redaction", "transcription_settings": {"deepgram": {"redact": ["pii"]}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNone(error)
        self.assertEqual(bot.deepgram_redaction_settings(), ["pii"])

        # Test with multiple redaction types
        bot2, error2 = create_bot(data={"meeting_url": "https://meet.google.com/xyz-uvw-rst", "bot_name": "Test Bot with Multiple Redaction", "transcription_settings": {"deepgram": {"redact": ["pii", "pci", "numbers"]}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot2)
        self.assertIsNone(error2)
        self.assertEqual(bot2.deepgram_redaction_settings(), ["pii", "pci", "numbers"])

    def test_create_bot_with_empty_redaction_settings(self):
        """Test creating a bot with empty redaction settings."""
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/empty-redact-test", "bot_name": "Test Bot with Empty Redaction", "transcription_settings": {"deepgram": {"redact": []}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNone(error)
        self.assertEqual(bot.deepgram_redaction_settings(), [])

    def test_create_bot_with_invalid_redaction_type_returns_error(self):
        """Test that creating a bot with invalid redaction type returns validation error."""
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/invalid-redact-test", "bot_name": "Test Bot with Invalid Redaction", "transcription_settings": {"deepgram": {"redact": ["invalid_redaction_type"]}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertIsNotNone(error)
        self.assertIn("transcription_settings", error)

    def test_create_bot_with_duplicate_redaction_types_returns_error(self):
        """Test that creating a bot with duplicate redaction types returns validation error."""
        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/duplicate-redact-test",
                "bot_name": "Test Bot with Duplicate Redaction",
                "transcription_settings": {
                    "deepgram": {
                        "redact": ["pii", "pci", "pii"]  # Duplicate "pii"
                    }
                },
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(bot)
        self.assertIsNotNone(error)
        self.assertIn("transcription_settings", error)

    def test_create_bot_with_null_redaction_settings_handled_correctly(self):
        """Test that creating a bot with null redaction settings is handled correctly."""
        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/null-redact-test",
                "bot_name": "Test Bot with Null Redaction",
                "transcription_settings": {
                    "deepgram": {
                        "language": "en-US",
                        "model": "nova-3",
                        # No redact property
                    }
                },
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNotNone(bot)
        self.assertIsNone(error)
        self.assertEqual(bot.deepgram_redaction_settings(), [])

    def test_create_bot_redaction_settings_combined_with_other_deepgram_settings(self):
        """Test creating a bot with redaction settings combined with other Deepgram settings."""
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/combined-settings-test", "bot_name": "Test Bot with Combined Settings", "transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-2", "redact": ["pii", "numbers"], "keywords": ["meeting", "agenda"]}}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot)
        self.assertIsNone(error)

        # Verify redaction settings
        self.assertEqual(bot.deepgram_redaction_settings(), ["pii", "numbers"])

        # Verify other settings are preserved
        deepgram_settings = bot.settings["transcription_settings"]["deepgram"]
        self.assertEqual(deepgram_settings["language"], "en-US")
        self.assertEqual(deepgram_settings["model"], "nova-2")
        self.assertEqual(deepgram_settings["keywords"], ["meeting", "agenda"])

    def test_create_bot_with_google_meet_url_with_http(self):
        bot, error = create_bot(data={"meeting_url": "http://meet.google.com/abc-defg-hij", "bot_name": "Test Bot"}, source=BotCreationSource.DASHBOARD, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        self.assertEqual(error, {"meeting_url": ["Google Meet URL must start with https://meet.google.com/"]})

    def test_create_scheduled_bot(self):
        """Test creating a bot with join_at timestamp"""
        future_time = timezone.now() + timedelta(hours=1)
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Scheduled Test Bot", "join_at": future_time.isoformat()}, source=BotCreationSource.API, project=self.project)

        self.assertIsNotNone(bot)
        self.assertIsNone(error)
        self.assertEqual(bot.state, BotStates.SCHEDULED)
        self.assertIsNotNone(bot.join_at)
        self.assertEqual(bot.join_at.replace(microsecond=0), future_time.replace(microsecond=0))
        self.assertIsNotNone(bot.recordings.first())

        # Verify no events are created for scheduled bots
        events = bot.bot_events
        self.assertEqual(events.count(), 0)

    def test_create_bot_with_invalid_image(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "bot_image": {"type": "image/png", "data": "iVBORAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        bot_image_errors = error["bot_image"]["non_field_errors"]
        error_message = str(bot_image_errors[0])
        self.assertEqual(error_message, "Data is not a valid PNG image. This site can generate base64 encoded PNG images to test with: https://png-pixel.com")

    def test_with_too_many_webhooks(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "webhooks": [{"url": "https://example.com", "triggers": ["bot.state_change"]}, {"url": "https://example2.com", "triggers": ["bot.state_change"]}, {"url": "https://example3.com", "triggers": ["bot.state_change"]}]}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        self.assertEqual(error, {"error": "You have reached the maximum number of webhooks for a single bot"})

    def test_with_invalid_webhook_trigger(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "webhooks": [{"url": "https://example.com", "triggers": ["invalid_trigger"]}]}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        self.assertIn("webhooks", error)
        self.assertIsInstance(error["webhooks"], list)
        self.assertIn("'invalid_trigger' is not one of", str(error["webhooks"][0]))

    def test_with_invalid_webhook_url(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "webhooks": [{"url": "http://example.com", "triggers": ["bot.state_change"]}]}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        self.assertIn("webhooks", error)
        self.assertIsInstance(error["webhooks"], list)
        self.assertIn("does not match '^https://.*'", str(error["webhooks"][0]))

    def test_with_duplicate_webhook_url(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot", "webhooks": [{"url": "https://example.com", "triggers": ["bot.state_change"]}, {"url": "https://example.com", "triggers": ["bot.state_change"]}]}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(bot)
        self.assertEqual(Bot.objects.count(), 0)
        self.assertIsNotNone(error)
        self.assertEqual(error, {"error": "URL already subscribed for this bot"})

    def test_create_bot_with_duplicate_deduplication_key(self):
        """Test creating a bot with a duplicate deduplication key in the same project."""
        deduplication_key = "test-key-123"
        # First bot creation should succeed
        bot1, error1 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 1", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNotNone(bot1)
        self.assertIsNone(error1)
        self.assertEqual(bot1.recordings.first().transcription_provider, TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM)

        # Second bot creation with the same key should fail
        bot2, error2 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 2", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(bot2)
        self.assertIsNotNone(error2)
        self.assertEqual(error2, {"error": "Deduplication key already in use. A bot in a non-terminal state with this deduplication key already exists. Please use a different deduplication key or wait for that bot to terminate."})

    def test_create_bot_with_duplicate_deduplication_key_different_projects(self):
        """Test that duplicate deduplication keys are allowed in different projects."""
        deduplication_key = "test-key-456"
        organization = Organization.objects.create(name="Test Organization 2")
        project2 = Project.objects.create(name="Test Project 2", organization=organization)

        # First bot creation should succeed
        bot1, error1 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 1", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNotNone(bot1)
        self.assertIsNone(error1)

        # Second bot creation in a different project with the same key should also succeed
        bot2, error2 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 2", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=project2,
        )
        self.assertIsNotNone(bot2)
        self.assertIsNone(error2)
        self.assertEqual(Bot.objects.count(), 2)

    def test_create_bot_with_duplicate_deduplication_key_bot_in_terminal_state(self):
        """Test that a new bot can be created with a deduplication key if the existing bot is in a terminal state."""
        deduplication_key = "test-key-789"

        # First bot creation should succeed
        bot1, error1 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 1", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNotNone(bot1)
        self.assertIsNone(error1)

        # Move the first bot to a terminal state
        bot1.state = BotStates.ENDED
        bot1.save()

        # Second bot creation with the same key should now succeed
        bot2, error2 = create_bot(
            data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 2", "deduplication_key": deduplication_key},
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNotNone(bot2)
        self.assertIsNone(error2)
        self.assertEqual(Bot.objects.count(), 2)

    def test_create_bot_without_deduplication_key(self):
        """Test that multiple bots can be created without a deduplication key."""
        # First bot creation should succeed
        bot1, error1 = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 1"}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot1)
        self.assertIsNone(error1)

        # Second bot creation without a key should also succeed
        bot2, error2 = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot 2"}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(bot2)
        self.assertIsNone(error2)
        self.assertEqual(Bot.objects.count(), 2)


class TestCreateWebhookSubscription(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def test_create_webhook_subscription(self):
        self.assertEqual(WebhookSubscription.objects.count(), 0)
        create_webhook_subscription("https://example.com", ["bot.state_change"], self.project)
        webhook_subscription = WebhookSubscription.objects.get(url="https://example.com")
        self.assertEqual(webhook_subscription.triggers, [WebhookTriggerTypes.BOT_STATE_CHANGE])
        self.assertEqual(webhook_subscription.project, self.project)
        self.assertIsNone(webhook_subscription.bot)
        self.assertEqual(webhook_subscription.is_active, True)
        self.assertEqual(WebhookSubscription.objects.count(), 1)

    def test_create_webhook_subscription_with_invalid_url(self):
        with self.assertRaises(ValidationError):
            create_webhook_subscription("http://example.com", ["bot.state_change"], self.project)

    def test_create_webhook_subscription_with_invalid_triggers(self):
        with self.assertRaises(ValidationError):
            create_webhook_subscription("https://example.com", ["invalid_trigger"], self.project)

    def test_create_webhook_subscription_with_duplicate_url(self):
        create_webhook_subscription("https://example.com", ["bot.state_change"], self.project)
        with self.assertRaises(ValidationError):
            create_webhook_subscription("https://example.com", ["bot.state_change"], self.project)

    def test_create_webhook_subscription_with_too_many_webhooks(self):
        for i in range(2):
            create_webhook_subscription(f"https://example{i}.com", ["bot.state_change"], self.project)
        with self.assertRaises(ValidationError):
            create_webhook_subscription("https://example3.com", ["bot.state_change"], self.project)
