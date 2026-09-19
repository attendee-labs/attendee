from unittest.mock import patch

from django.test import TestCase

from accounts.models import Organization
from bots.app_session_api_utils import create_app_session
from bots.bots_api_utils import BotCreationSource
from bots.models import Bot, BotEventTypes, BotStates, Project, RecordingTypes, SessionTypes, TranscriptionProviders, TranscriptionTypes, WebhookSubscription, WebhookTriggerTypes


def zoom_rtms_data(rtms_stream_id="rtms_stream_123"):
    return {
        "meeting_uuid": "abcDEF123456789==",
        "rtms_stream_id": rtms_stream_id,
        "server_urls": "wss://rtms.zoom.us:443",
    }


class TestCreateAppSession(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_create_app_session(self):
        app_session, error = create_app_session(data={"zoom_rtms": zoom_rtms_data()}, source=BotCreationSource.API, project=self.project)

        self.assertIsNotNone(app_session)
        self.assertIsNone(error)
        self.assertEqual(app_session.session_type, SessionTypes.APP_SESSION)
        self.assertEqual(app_session.object_id_prefix(), "app_")
        self.assertEqual(app_session.meeting_url, "app_session")
        self.assertEqual(app_session.name, "App Session")
        self.assertEqual(app_session.zoom_rtms_stream_id, "rtms_stream_123")
        self.assertEqual(app_session.zoom_rtms(), zoom_rtms_data())

        # The connection requested event moves the app session out of the ready state
        self.assertEqual(app_session.state, BotStates.CONNECTING)
        events = app_session.bot_events
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().event_type, BotEventTypes.APP_SESSION_CONNECTION_REQUESTED)
        self.assertEqual(events.first().metadata["source"], BotCreationSource.API)

        recording = app_session.recordings.first()
        self.assertIsNotNone(recording)
        self.assertTrue(recording.is_default_recording)
        self.assertEqual(recording.recording_type, RecordingTypes.AUDIO_AND_VIDEO)
        self.assertEqual(recording.transcription_type, TranscriptionTypes.NON_REALTIME)
        self.assertEqual(recording.transcription_provider, TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM)

    def test_create_app_session_forces_720p_resolution(self):
        """App sessions only support 720p, so a requested 1080p resolution is overridden."""
        app_session, error = create_app_session(data={"zoom_rtms": zoom_rtms_data(), "recording_settings": {"resolution": "1080p"}}, source=BotCreationSource.API, project=self.project)

        self.assertIsNotNone(app_session)
        self.assertIsNone(error)
        self.assertEqual(app_session.settings["recording_settings"]["resolution"], "720p")

    def test_create_app_session_with_explicit_transcription_settings(self):
        app_session, error = create_app_session(data={"zoom_rtms": zoom_rtms_data(), "transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-3"}}}, source=BotCreationSource.API, project=self.project)

        self.assertIsNotNone(app_session)
        self.assertIsNone(error)
        self.assertEqual(app_session.recordings.first().transcription_provider, TranscriptionProviders.DEEPGRAM)

    def test_create_app_session_without_zoom_rtms_returns_error(self):
        app_session, error = create_app_session(data={}, source=BotCreationSource.API, project=self.project)

        self.assertIsNone(app_session)
        self.assertIsNotNone(error)
        self.assertIn("zoom_rtms", error)
        self.assertEqual(Bot.objects.count(), 0)

    def test_create_app_session_with_incomplete_zoom_rtms_returns_error(self):
        """zoom_rtms requires meeting_uuid, rtms_stream_id and server_urls."""
        app_session, error = create_app_session(data={"zoom_rtms": {"meeting_uuid": "abcDEF123456789=="}}, source=BotCreationSource.API, project=self.project)

        self.assertIsNone(app_session)
        self.assertIsNotNone(error)
        self.assertIn("zoom_rtms", error)
        self.assertIn("is a required property", str(error["zoom_rtms"][0]))
        self.assertEqual(Bot.objects.count(), 0)

    def test_create_app_session_with_webhooks(self):
        app_session, error = create_app_session(
            data={"zoom_rtms": zoom_rtms_data(), "webhooks": [{"url": "https://example.com/webhook", "triggers": ["bot.state_change"]}]},
            source=BotCreationSource.API,
            project=self.project,
        )

        self.assertIsNotNone(app_session)
        self.assertIsNone(error)

        webhook_subscription = WebhookSubscription.objects.get(url="https://example.com/webhook")
        self.assertEqual(webhook_subscription.bot, app_session)
        self.assertEqual(webhook_subscription.project, self.project)
        self.assertEqual(webhook_subscription.triggers, [WebhookTriggerTypes.BOT_STATE_CHANGE])

    def test_create_app_session_with_external_media_storage_settings_without_credentials(self):
        app_session, error = create_app_session(data={"zoom_rtms": zoom_rtms_data(), "external_media_storage_settings": {"bucket_name": "my-bucket"}}, source=BotCreationSource.API, project=self.project)

        self.assertIsNone(app_session)
        self.assertIsNotNone(error)
        self.assertIn("External media storage credentials are required", error["error"])
        self.assertEqual(Bot.objects.count(), 0)

    def test_create_app_session_with_duplicate_deduplication_key(self):
        deduplication_key = "app-session-key-123"
        app_session1, error1 = create_app_session(data={"zoom_rtms": zoom_rtms_data("stream-1"), "deduplication_key": deduplication_key}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(app_session1)
        self.assertIsNone(error1)

        # The first app session is in a non-terminal state, so the key is still taken
        app_session2, error2 = create_app_session(data={"zoom_rtms": zoom_rtms_data("stream-2"), "deduplication_key": deduplication_key}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(app_session2)
        self.assertIsNotNone(error2)
        self.assertIn("Deduplication key already in use", error2["error"])
        self.assertEqual(Bot.objects.count(), 1)

    def test_create_app_session_out_of_credits(self):
        self.organization.centicredits = -200
        self.organization.save()
        self.assertTrue(self.organization.out_of_credits())

        app_session, error = create_app_session(data={"zoom_rtms": zoom_rtms_data()}, source=BotCreationSource.API, project=self.project)

        self.assertIsNone(app_session)
        self.assertEqual(error, {"error": "Organization has run out of credits. Please add more credits in the Account -> Billing page."})
        self.assertEqual(Bot.objects.count(), 0)

    @patch("bots.models.Project.concurrent_bots_limit")
    def test_create_app_session_respects_concurrency_limit(self, mock_limit):
        """A connecting app session counts toward the project's concurrent bot limit."""
        mock_limit.return_value = 1

        app_session1, error1 = create_app_session(data={"zoom_rtms": zoom_rtms_data("stream-1")}, source=BotCreationSource.API, project=self.project)
        self.assertIsNotNone(app_session1)
        self.assertIsNone(error1)

        app_session2, error2 = create_app_session(data={"zoom_rtms": zoom_rtms_data("stream-2")}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(app_session2)
        self.assertEqual(error2["error"], "You have exceeded the maximum number of concurrent bots (1) for your account. Please reach out to customer support to increase the limit.")
        self.assertEqual(Bot.objects.count(), 1)
