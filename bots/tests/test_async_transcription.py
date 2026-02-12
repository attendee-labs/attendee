"""
Tests for async transcription using Assembly AI (grouped utterances route).

These tests verify the end-to-end flow for async transcription with Assembly AI,
which uses the grouped utterances approach rather than individual utterance processing.
"""

import os
from unittest import mock

from django.test import override_settings
from django.test.testcases import TransactionTestCase

from bots.models import (
    AsyncTranscription,
    AsyncTranscriptionStates,
    AudioChunk,
    Bot,
    BotStates,
    Credentials,
    Organization,
    Participant,
    Project,
    Recording,
    RecordingStates,
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
from bots.tasks.process_async_transcription_task import (
    create_utterances_for_transcription_using_groups,
    process_async_transcription,
)
from bots.tasks.process_utterance_group_task import process_utterance_group_for_async_transcription
from bots.transcription_utils import split_transcription_by_utterance


@mock.patch.dict(os.environ, {"AWS_RECORDING_STORAGE_BUCKET_NAME": "test-bucket", "CHARGE_CREDITS_FOR_BOTS": "false"})
class AsyncTranscriptionTestCase(TransactionTestCase):
    """Base test case with common setup for Assembly AI async transcription tests."""

    def setUp(self):
        # Create organization and project
        self.organization = Organization.objects.create(
            name="Test Org",
            centicredits=10000,
            is_async_transcription_enabled=True,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create a bot
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.ENDED,
            settings={"recording_settings": {"record_async_transcription_audio_chunks": True}},
        )

        # Create default recording with Assembly AI as the transcription provider
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.ASSEMBLY_AI,
            is_default_recording=True,
            state=RecordingStates.COMPLETE,
        )

        # Create Assembly AI credentials
        self.assemblyai_credentials = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.ASSEMBLY_AI,
        )
        self.assemblyai_credentials.set_credentials({"api_key": "test_assemblyai_api_key"})

        # Create a participant for utterances
        self.participant = Participant.objects.create(
            bot=self.bot,
            uuid="participant-1",
            full_name="Test User",
        )

        # Create webhook subscription
        self.webhook_secret = WebhookSecret.objects.create(project=self.project)
        self.webhook_subscription = WebhookSubscription.objects.create(
            project=self.project,
            url="https://example.com/webhook",
            triggers=[WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE],
            is_active=True,
        )

    def _create_audio_chunks(self, count=3, duration_ms=1000, sample_rate=16000):
        """Helper to create audio chunks for testing."""
        chunks = []
        for i in range(count):
            # Create minimal PCM audio data (silence)
            audio_data = b"\x00\x00" * (sample_rate * duration_ms // 1000)
            chunk = AudioChunk.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_blob=audio_data,
                timestamp_ms=i * duration_ms,
                duration_ms=duration_ms,
                sample_rate=sample_rate,
            )
            chunks.append(chunk)
        return chunks


class TestAsyncTranscriptionUsesGroupedUtterances(AsyncTranscriptionTestCase):
    """Tests that Assembly AI async transcription uses grouped utterances."""

    def test_assembly_ai_transcription_uses_grouped_utterances(self):
        """Verify that AsyncTranscription with Assembly AI uses grouped utterances."""
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        self.assertEqual(async_transcription.transcription_provider, TranscriptionProviders.ASSEMBLY_AI)
        self.assertTrue(async_transcription.use_grouped_utterances)

    def test_deepgram_transcription_does_not_use_grouped_utterances(self):
        """Verify that other providers do NOT use grouped utterances."""
        # Create a recording with Deepgram
        deepgram_recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.DEEPGRAM,
            is_default_recording=False,
            state=RecordingStates.COMPLETE,
        )

        async_transcription = AsyncTranscription.objects.create(
            recording=deepgram_recording,
            settings={"transcription_settings": {"deepgram": {}}},
        )

        self.assertEqual(async_transcription.transcription_provider, TranscriptionProviders.DEEPGRAM)
        self.assertFalse(async_transcription.use_grouped_utterances)


class TestUtteranceGrouping(AsyncTranscriptionTestCase):
    """Tests for the utterance grouping logic."""

    @mock.patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_utterances_are_grouped_by_duration(self, mock_deliver_webhook):
        """Verify that utterances are grouped into evenly-sized groups based on duration."""
        mock_deliver_webhook.return_value = None

        # Create audio chunks with varying durations (total 40 minutes worth, should create 2 groups)
        # 30 minutes is the max group duration
        chunks = []
        for i in range(8):
            # 5 minutes each = 40 minutes total
            duration_ms = 5 * 60 * 1000
            audio_data = b"\x00\x00" * 1000  # minimal data
            chunk = AudioChunk.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_blob=audio_data,
                timestamp_ms=i * duration_ms,
                duration_ms=duration_ms,
                sample_rate=16000,
            )
            chunks.append(chunk)

        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        # Count utterances before
        self.assertEqual(Utterance.objects.filter(async_transcription=async_transcription).count(), 0)

        # Use mock to track how groups are created
        with mock.patch("bots.tasks.process_async_transcription_task.process_utterance_group_for_async_transcription") as mock_group_task:
            mock_group_task.apply_async = mock.MagicMock()

            create_utterances_for_transcription_using_groups(async_transcription)

            # Should have created utterances
            utterances = Utterance.objects.filter(async_transcription=async_transcription)
            self.assertEqual(utterances.count(), 8)

            # Should have created 2 groups (40 min / 30 min max = ceil(1.33) = 2 groups)
            self.assertEqual(mock_group_task.apply_async.call_count, 2)

            # Verify each group has 4 utterances (evenly split)
            for call in mock_group_task.apply_async.call_args_list:
                utterance_ids = call.kwargs["args"][0]
                self.assertEqual(len(utterance_ids), 4)

    @mock.patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_single_group_for_short_meetings(self, mock_deliver_webhook):
        """Verify that short meetings (< 30 min) result in a single group."""
        mock_deliver_webhook.return_value = None

        # Create audio chunks totaling 10 minutes
        chunks = []
        for i in range(10):
            # 1 minute each = 10 minutes total
            duration_ms = 60 * 1000
            audio_data = b"\x00\x00" * 1000
            chunk = AudioChunk.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_blob=audio_data,
                timestamp_ms=i * duration_ms,
                duration_ms=duration_ms,
                sample_rate=16000,
            )
            chunks.append(chunk)

        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        with mock.patch("bots.tasks.process_async_transcription_task.process_utterance_group_for_async_transcription") as mock_group_task:
            mock_group_task.apply_async = mock.MagicMock()

            create_utterances_for_transcription_using_groups(async_transcription)

            # Should create only 1 group for < 30 minutes
            self.assertEqual(mock_group_task.apply_async.call_count, 1)

            # Verify all 10 utterance IDs are in the single group
            call_args = mock_group_task.apply_async.call_args
            utterance_ids = call_args.kwargs["args"][0]
            self.assertEqual(len(utterance_ids), 10)


class TestSplitTranscriptionByUtterance(AsyncTranscriptionTestCase):
    """Tests for the split_transcription_by_utterance function."""

    def test_splits_transcription_correctly(self):
        """Verify transcription is correctly split back into per-utterance results."""
        # Create audio chunks
        chunks = self._create_audio_chunks(count=3, duration_ms=2000)

        # Create utterances
        utterances = []
        for chunk in chunks:
            utterance = Utterance.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_chunk=chunk,
                timestamp_ms=chunk.timestamp_ms,
                duration_ms=chunk.duration_ms,
            )
            utterances.append(utterance)

        # Simulate a combined transcription result with words spread across utterances
        # Utterance 0: 0-2s, Utterance 1: 5-7s (with 3s silence), Utterance 2: 10-12s
        transcription_result = {
            "transcript": "hello world how are you doing today",
            "language": "en",
            "words": [
                # Words in first utterance (0-2s)
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.6, "end": 1.0},
                # Words in second utterance (5-7s)
                {"word": "how", "start": 5.0, "end": 5.3},
                {"word": "are", "start": 5.4, "end": 5.6},
                {"word": "you", "start": 5.7, "end": 5.9},
                # Words in third utterance (10-12s)
                {"word": "doing", "start": 10.0, "end": 10.5},
                {"word": "today", "start": 10.6, "end": 11.0},
            ],
        }

        result = split_transcription_by_utterance(transcription_result, utterances, silence_seconds=3.0)

        # Verify each utterance got its words
        self.assertEqual(len(result), 3)

        # First utterance
        self.assertEqual(result[utterances[0].id]["transcript"], "hello world")
        self.assertEqual(len(result[utterances[0].id]["words"]), 2)
        self.assertEqual(result[utterances[0].id]["language"], "en")

        # Second utterance
        self.assertEqual(result[utterances[1].id]["transcript"], "how are you")
        self.assertEqual(len(result[utterances[1].id]["words"]), 3)

        # Third utterance
        self.assertEqual(result[utterances[2].id]["transcript"], "doing today")
        self.assertEqual(len(result[utterances[2].id]["words"]), 2)

    def test_handles_empty_utterances(self):
        """Verify empty utterance list returns empty dict."""
        result = split_transcription_by_utterance({"words": []}, [])
        self.assertEqual(result, {})

    def test_word_timestamps_adjusted_to_utterance_start(self):
        """Verify word timestamps are adjusted relative to utterance start."""
        chunks = self._create_audio_chunks(count=2, duration_ms=1000)

        utterances = []
        for chunk in chunks:
            utterance = Utterance.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_chunk=chunk,
                timestamp_ms=chunk.timestamp_ms,
                duration_ms=chunk.duration_ms,
            )
            utterances.append(utterance)

        transcription_result = {
            "words": [
                # Words in second utterance (starts at 4s with 3s silence)
                {"word": "test", "start": 4.2, "end": 4.8},
            ],
        }

        result = split_transcription_by_utterance(transcription_result, utterances, silence_seconds=3.0)

        # The word should have its timestamp adjusted to be relative to utterance start (4.0s)
        second_utterance_words = result[utterances[1].id]["words"]
        self.assertEqual(len(second_utterance_words), 1)
        # 4.2 - 4.0 = 0.2
        self.assertAlmostEqual(second_utterance_words[0]["start"], 0.2, places=2)
        # 4.8 - 4.0 = 0.8
        self.assertAlmostEqual(second_utterance_words[0]["end"], 0.8, places=2)

    def test_word_overlapping_multiple_windows_is_skipped(self):
        """Verify that a word overlapping with both current and next window is skipped."""
        chunks = self._create_audio_chunks(count=2, duration_ms=2000)

        utterances = []
        for chunk in chunks:
            utterance = Utterance.objects.create(
                recording=self.recording,
                participant=self.participant,
                audio_chunk=chunk,
                timestamp_ms=chunk.timestamp_ms,
                duration_ms=chunk.duration_ms,
            )
            utterances.append(utterance)

        # With silence_seconds=3.0:
        # Utterance 0: 0-2s
        # Utterance 1: 5-7s (0+2+3=5)
        # A word spanning from 1.0 to 6.0 overlaps both windows and should be skipped
        transcription_result = {
            "words": [
                {"word": "normal", "start": 0.0, "end": 0.5},  # Only in first window
                {"word": "spanning", "start": 1.0, "end": 6.0},  # Overlaps both, should be skipped
                {"word": "test", "start": 5.5, "end": 6.5},  # Only in second window
            ],
        }

        with self.assertLogs("bots.transcription_utils", level="WARNING") as log:
            result = split_transcription_by_utterance(transcription_result, utterances, silence_seconds=3.0)

        # Verify warning was logged
        self.assertTrue(any("overlaps with subsequent window" in msg for msg in log.output))

        # First utterance should only have "normal", not "spanning"
        self.assertEqual(result[utterances[0].id]["transcript"], "normal")
        self.assertEqual(len(result[utterances[0].id]["words"]), 1)
        self.assertEqual(result[utterances[0].id]["words"][0]["word"], "normal")

        # Second utterance should only have "test"
        self.assertEqual(result[utterances[1].id]["transcript"], "test")
        self.assertEqual(len(result[utterances[1].id]["words"]), 1)
        self.assertEqual(result[utterances[1].id]["words"][0]["word"], "test")


class TestProcessUtteranceGroup(AsyncTranscriptionTestCase):
    """Tests for the process_utterance_group_for_async_transcription task."""

    @mock.patch("bots.tasks.process_utterance_group_task.get_transcription_via_assemblyai_for_utterance_group")
    def test_successful_transcription_writes_to_all_utterances(self, mock_get_transcription):
        """Verify successful transcription writes results to all utterances in the group."""
        chunks = self._create_audio_chunks(count=3, duration_ms=1000)

        utterances = []
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        for chunk in chunks:
            utterance = Utterance.objects.create(
                recording=self.recording,
                async_transcription=async_transcription,
                participant=self.participant,
                audio_chunk=chunk,
                timestamp_ms=chunk.timestamp_ms,
                duration_ms=chunk.duration_ms,
            )
            utterances.append(utterance)

        # Mock successful transcription
        mock_transcriptions = {
            utterances[0].id: {"transcript": "hello", "words": [{"word": "hello", "start": 0.0, "end": 0.5}]},
            utterances[1].id: {"transcript": "world", "words": [{"word": "world", "start": 0.0, "end": 0.5}]},
            utterances[2].id: {"transcript": "test", "words": [{"word": "test", "start": 0.0, "end": 0.5}]},
        }
        mock_get_transcription.return_value = (mock_transcriptions, None)

        # Process the group
        utterance_ids = [u.id for u in utterances]
        process_utterance_group_for_async_transcription(utterance_ids)

        # Verify all utterances have transcriptions
        for utterance in utterances:
            utterance.refresh_from_db()
            self.assertIsNotNone(utterance.transcription)
            self.assertIsNone(utterance.failure_data)

        # Verify the correct transcription was assigned
        utterances[0].refresh_from_db()
        self.assertEqual(utterances[0].transcription["transcript"], "hello")

    @mock.patch("bots.tasks.process_utterance_group_task.get_transcription_via_assemblyai_for_utterance_group")
    def test_failed_transcription_marks_all_utterances_failed(self, mock_get_transcription):
        """Verify failed transcription marks all utterances in the group as failed."""
        chunks = self._create_audio_chunks(count=2, duration_ms=1000)

        utterances = []
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        for chunk in chunks:
            utterance = Utterance.objects.create(
                recording=self.recording,
                async_transcription=async_transcription,
                participant=self.participant,
                audio_chunk=chunk,
                timestamp_ms=chunk.timestamp_ms,
                duration_ms=chunk.duration_ms,
                transcription_attempt_count=5,  # Already at max retries
            )
            utterances.append(utterance)

        # Mock failed transcription (non-retryable)
        failure_data = {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        mock_get_transcription.return_value = (None, failure_data)

        # Process the group
        utterance_ids = [u.id for u in utterances]
        process_utterance_group_for_async_transcription(utterance_ids)

        # Verify all utterances have failure_data
        for utterance in utterances:
            utterance.refresh_from_db()
            self.assertIsNone(utterance.transcription)
            self.assertIsNotNone(utterance.failure_data)
            self.assertEqual(utterance.failure_data["reason"], TranscriptionFailureReasons.CREDENTIALS_INVALID)

    def test_empty_utterance_ids_does_not_fail(self):
        """Verify empty utterance IDs list is handled gracefully."""
        # Should not raise
        process_utterance_group_for_async_transcription([])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TestEndToEndAsyncTranscriptionAssemblyAI(AsyncTranscriptionTestCase):
    """End-to-end tests for async transcription with Assembly AI."""

    @mock.patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    @mock.patch("bots.transcription_utils.requests.delete")
    @mock.patch("bots.transcription_utils.requests.get")
    @mock.patch("bots.transcription_utils.requests.post")
    @mock.patch("bots.transcription_utils.get_mp3_for_utterance_group")
    def test_complete_async_transcription_flow(
        self,
        mock_get_mp3,
        mock_post,
        mock_get,
        mock_delete,
        mock_deliver_webhook,
    ):
        """Test complete async transcription flow from creation to completion."""
        mock_deliver_webhook.return_value = None

        # Create audio chunks
        self._create_audio_chunks(count=3, duration_ms=1000)

        # Create async transcription
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.NOT_STARTED)

        # Mock MP3 generation
        mock_get_mp3.return_value = b"fake-mp3-data"

        # Mock upload response
        upload_response = mock.Mock()
        upload_response.status_code = 200
        upload_response.json.return_value = {"upload_url": "https://assemblyai.com/upload/123"}

        # Mock transcription request response
        transcribe_response = mock.Mock()
        transcribe_response.status_code = 200
        transcribe_response.json.return_value = {"id": "transcript-123"}

        mock_post.side_effect = [upload_response, transcribe_response]

        # Mock polling response (completed)
        poll_response = mock.Mock()
        poll_response.status_code = 200
        poll_response.json.return_value = {
            "status": "completed",
            "text": "hello world test",
            "language_code": "en",
            "words": [
                {"text": "hello", "start": 0, "end": 500, "confidence": 0.99},
                {"text": "world", "start": 4000, "end": 4500, "confidence": 0.98},
                {"text": "test", "start": 8000, "end": 8500, "confidence": 0.97},
            ],
        }
        mock_get.return_value = poll_response

        # Mock delete response
        delete_response = mock.Mock()
        delete_response.status_code = 200
        mock_delete.return_value = delete_response

        # Process the async transcription
        process_async_transcription.delay(async_transcription.id)

        # Refresh and verify state
        async_transcription.refresh_from_db()

        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.COMPLETE)
        self.assertIsNotNone(async_transcription.completed_at)
        self.assertIsNotNone(async_transcription.started_at)
        self.assertIsNone(async_transcription.failure_data)

        # Verify utterances were created and transcribed
        utterances = Utterance.objects.filter(async_transcription=async_transcription)
        self.assertEqual(utterances.count(), 3)

        for utterance in utterances:
            self.assertIsNotNone(utterance.transcription)
            self.assertIsNone(utterance.failure_data)

        # Verify webhook delivery attempts were created
        webhook_attempts = WebhookDeliveryAttempt.objects.filter(
            bot=self.bot,
            webhook_trigger_type=WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE,
        )
        self.assertEqual(webhook_attempts.count(), 2)  # IN_PROGRESS and COMPLETE

    @mock.patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    @mock.patch("bots.transcription_utils.requests.post")
    @mock.patch("bots.transcription_utils.get_mp3_for_utterance_group")
    def test_async_transcription_fails_with_invalid_credentials(
        self,
        mock_get_mp3,
        mock_post,
        mock_deliver_webhook,
    ):
        """Test async transcription fails properly when credentials are invalid."""
        mock_deliver_webhook.return_value = None

        # Create audio chunks
        self._create_audio_chunks(count=2, duration_ms=500)

        # Create async transcription
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        # Mock MP3 generation
        mock_get_mp3.return_value = b"fake-mp3-data"

        # Mock 401 unauthorized response (simulates invalid API key)
        upload_response = mock.Mock()
        upload_response.status_code = 401
        mock_post.return_value = upload_response

        # Process the async transcription
        process_async_transcription.delay(async_transcription.id)

        # Refresh and verify state
        async_transcription.refresh_from_db()

        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.FAILED)
        self.assertIsNotNone(async_transcription.failed_at)
        self.assertIsNotNone(async_transcription.failure_data)

        # Verify all utterances have failure data
        utterances = Utterance.objects.filter(async_transcription=async_transcription)
        for utterance in utterances:
            self.assertIsNotNone(utterance.failure_data)

    @mock.patch("bots.tasks.deliver_webhook_task.deliver_webhook")
    def test_async_transcription_fails_without_credentials(self, mock_deliver_webhook):
        """Test async transcription fails when no credentials exist."""
        mock_deliver_webhook.return_value = None

        # Delete credentials
        self.assemblyai_credentials.delete()

        # Create audio chunks
        self._create_audio_chunks(count=2, duration_ms=500)

        # Create async transcription
        async_transcription = AsyncTranscription.objects.create(
            recording=self.recording,
            settings={"transcription_settings": {"assembly_ai": {}}},
        )

        # Process the async transcription
        process_async_transcription.delay(async_transcription.id)

        # Refresh and verify state
        async_transcription.refresh_from_db()

        self.assertEqual(async_transcription.state, AsyncTranscriptionStates.FAILED)
        self.assertIsNotNone(async_transcription.failure_data)
        self.assertIn(TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, async_transcription.failure_data.get("failure_reasons", []))
