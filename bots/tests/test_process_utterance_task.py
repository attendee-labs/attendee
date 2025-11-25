import uuid
from unittest import mock

from django.test import TransactionTestCase

from bots.models import (
    AudioChunk,
    Bot,
    Credentials,
    Organization,
    Participant,
    Project,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    TranscriptionFailureReasons,
    Utterance,
)
from bots.tasks.process_utterance_task import get_transcription_via_assemblyai, get_transcription_via_deepgram, get_transcription_via_elevenlabs, get_transcription_via_gladia, get_transcription_via_openai, get_transcription_via_sarvam, process_utterance


class ProcessUtteranceTaskTest(TransactionTestCase):
    """Unit‑tests for bots.tasks.process_utterance_task.process_utterance"""

    def setUp(self):
        # Minimal object graph ------------------------------------------------------------------
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/xyz")

        # Recording already finished (so it is a terminal state) and waiting on transcription
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.IN_PROGRESS,
            transcription_provider=1,  # value irrelevant – we stub the provider call
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid=str(uuid.uuid4()))
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"rawpcmbytes", timestamp_ms=0, duration_ms=500, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=500,
        )
        self.utterance.refresh_from_db()

        # Make Celery run synchronously inside tests
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    # ------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------
    def _run_task(self):
        """Invoke the Celery task with the test utterance id (synchronously)."""
        process_utterance.apply(args=[self.utterance.id])

    # ------------------------------------------------------------------------------------------
    # Test cases
    # ------------------------------------------------------------------------------------------
    @mock.patch("bots.tasks.process_utterance_task.RecordingManager.set_recording_transcription_complete")
    @mock.patch("bots.tasks.process_utterance_task.get_transcription")
    def test_successful_transcription_marks_complete_and_clears_blob(self, mock_get_transcription, mock_set_complete):
        """Happy‑path: transcription returned → audio blob cleared, transcript saved, recording closed."""
        mock_get_transcription.return_value = ({"transcript": "hello world"}, None)

        self._run_task()
        self.utterance.refresh_from_db()

        # Utterance updated
        self.assertEqual(self.utterance.transcription["transcript"], "hello world")
        self.assertEqual(self.utterance.audio_blob, b"")
        self.assertIsNone(self.utterance.failure_data)
        self.assertEqual(self.utterance.transcription_attempt_count, 1)

        # Recording manager called because this was the last outstanding utterance
        mock_set_complete.assert_called_once_with(self.recording)

    # ------------------------------------------------------------------

    @mock.patch("bots.tasks.process_utterance_task.is_retryable_failure", return_value=True)
    @mock.patch("bots.tasks.process_utterance_task.get_transcription")
    def test_retryable_failure_raises_and_increments_counter(self, mock_get_transcription, mock_is_retryable):
        """Retryable failure → task raises for Celery to retry and attempt counter grows."""
        failure = {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED}
        mock_get_transcription.return_value = (None, failure)

        with self.assertRaises(Exception):
            self._run_task()

        self.utterance.refresh_from_db()
        self.assertEqual(self.utterance.transcription_attempt_count, 1)
        self.assertIsNone(self.utterance.failure_data)


class BotModelRedactionSettingsTest(TransactionTestCase):
    """Unit tests for Bot model deepgram_redaction_settings method."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def _create_bot_with_settings(self, settings):
        """Helper to create a bot with specific settings."""
        return Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/test", settings=settings)

    def test_deepgram_redaction_settings_returns_correct_list_with_single_type(self):
        """Test that deepgram_redaction_settings returns correct redaction list with single type."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"redact": ["pii"]}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, ["pii"])

    def test_deepgram_redaction_settings_returns_correct_list_with_multiple_types(self):
        """Test that deepgram_redaction_settings returns correct redaction list with multiple types."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"redact": ["pii", "pci", "numbers"]}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, ["pii", "pci", "numbers"])

    def test_deepgram_redaction_settings_returns_empty_list_when_no_redaction_configured(self):
        """Test that deepgram_redaction_settings returns empty list when no redaction is configured."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-3"}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_returns_empty_list_when_no_deepgram_settings(self):
        """Test that deepgram_redaction_settings returns empty list when no deepgram settings exist."""
        bot = self._create_bot_with_settings({"transcription_settings": {"openai": {"model": "gpt-4o-transcribe"}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_returns_empty_list_when_no_transcription_settings(self):
        """Test that deepgram_redaction_settings returns empty list when no transcription settings exist."""
        bot = self._create_bot_with_settings({"recording_settings": {"format": "mp4"}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_returns_empty_list_when_settings_is_empty(self):
        """Test that deepgram_redaction_settings returns empty list when settings is empty."""
        bot = self._create_bot_with_settings({})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_backward_compatibility_with_no_settings(self):
        """Test backward compatibility with bots that have no settings at all."""
        # Create bot without any settings (simulating old bots)
        bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/test",
            # No settings field set
        )

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_with_empty_redaction_array(self):
        """Test that deepgram_redaction_settings handles empty redaction array correctly."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"redact": []}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, [])

    def test_deepgram_redaction_settings_preserves_order(self):
        """Test that deepgram_redaction_settings preserves the order of redaction types."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"redact": ["numbers", "pii", "pci"]}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, ["numbers", "pii", "pci"])

    def test_deepgram_redaction_settings_with_other_deepgram_settings(self):
        """Test that deepgram_redaction_settings works correctly when combined with other deepgram settings."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-2", "redact": ["pci", "numbers"], "keywords": ["meeting", "agenda"]}}})

        result = bot.transcription_settings.deepgram_redaction_settings()
        self.assertEqual(result, ["pci", "numbers"])

    def test_deepgram_redaction_settings_method_consistency_across_calls(self):
        """Test that deepgram_redaction_settings method returns consistent results across multiple calls."""
        bot = self._create_bot_with_settings({"transcription_settings": {"deepgram": {"redact": ["pii", "pci"]}}})

        # Call the method multiple times
        result1 = bot.transcription_settings.deepgram_redaction_settings()
        result2 = bot.transcription_settings.deepgram_redaction_settings()
        result3 = bot.transcription_settings.deepgram_redaction_settings()

        # All results should be identical
        self.assertEqual(result1, result2)
        self.assertEqual(result2, result3)
        self.assertEqual(result1, ["pii", "pci"])


class DeepgramPrerecordedTranscriptionRedactionTest(TransactionTestCase):
    """Unit tests for pre-recorded transcription redaction integration."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create Deepgram credentials
        self.deepgram_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)

        # Mock the credentials
        self.credentials_patcher = mock.patch.object(Credentials, "get_credentials", return_value={"api_key": "test_deepgram_key"})
        self.credentials_patcher.start()
        self.addCleanup(self.credentials_patcher.stop)

    def _create_bot_with_redaction_settings(self, redaction_settings=None):
        """Helper to create a bot with specific redaction settings."""
        settings = {}
        if redaction_settings is not None:
            settings = {"transcription_settings": {"deepgram": {"redact": redaction_settings}}}

        return Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/test", settings=settings)

    def _create_utterance_for_bot(self, bot):
        """Helper to create an utterance for testing."""
        recording = Recording.objects.create(
            bot=bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.IN_PROGRESS,
            transcription_provider=1,  # Deepgram
        )

        participant = Participant.objects.create(bot=bot, uuid=str(uuid.uuid4()))
        import numpy as np

        # Create a proper audio blob as numpy array
        audio_data = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
        audio_chunk = AudioChunk.objects.create(recording=recording, participant=participant, audio_blob=audio_data, timestamp_ms=0, duration_ms=1000, sample_rate=16000)
        utterance = Utterance.objects.create(recording=recording, participant=participant, audio_chunk=audio_chunk, timestamp_ms=0, duration_ms=1000)
        return utterance

    @mock.patch("deepgram.PrerecordedOptions")
    @mock.patch("deepgram.DeepgramClient")
    def test_prerecorded_options_created_with_redaction_parameters(self, mock_deepgram_client, mock_prerecorded_options):
        """Test that PrerecordedOptions is created with correct redaction parameters."""
        # Create bot with redaction settings
        bot = self._create_bot_with_redaction_settings(["pii", "pci", "numbers"])
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with redaction parameters
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs

        # Verify redaction parameter is correctly passed
        self.assertEqual(call_kwargs["redact"], ["pii", "pci", "numbers"])

        # Verify other expected parameters are also present
        expected_params = {"model": bot.transcription_settings.deepgram_model(), "smart_format": True, "language": bot.transcription_settings.deepgram_language(), "detect_language": bot.transcription_settings.deepgram_detect_language(), "keyterm": bot.transcription_settings.deepgram_keyterms(), "keywords": bot.transcription_settings.deepgram_keywords(), "encoding": "linear16", "sample_rate": utterance.audio_chunk.sample_rate, "redact": ["pii", "pci", "numbers"]}

        for param, expected_value in expected_params.items():
            self.assertIn(param, call_kwargs)
            self.assertEqual(call_kwargs[param], expected_value)

    @mock.patch("deepgram.DeepgramClient")
    @mock.patch("deepgram.PrerecordedOptions")
    def test_prerecorded_options_created_with_empty_redaction_when_none_configured(self, mock_prerecorded_options, mock_deepgram_client):
        """Test that PrerecordedOptions is created with empty redaction list when none configured."""
        # Create bot without redaction settings
        bot = self._create_bot_with_redaction_settings(None)
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with empty redaction list
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs

        # Verify redaction parameter defaults to empty list
        self.assertEqual(call_kwargs["redact"], [])

    @mock.patch("deepgram.DeepgramClient")
    @mock.patch("deepgram.PrerecordedOptions")
    def test_prerecorded_options_created_with_single_redaction_type(self, mock_prerecorded_options, mock_deepgram_client):
        """Test that PrerecordedOptions is created correctly with single redaction type."""
        # Create bot with single redaction setting
        bot = self._create_bot_with_redaction_settings(["pii"])
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with single redaction type
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs

        # Verify redaction parameter contains single type
        self.assertEqual(call_kwargs["redact"], ["pii"])

    @mock.patch("deepgram.DeepgramClient")
    @mock.patch("deepgram.PrerecordedOptions")
    def test_redaction_settings_passed_directly_to_prerecorded_options(self, mock_prerecorded_options, mock_deepgram_client):
        """Test that redaction settings are passed directly to PrerecordedOptions without validation."""
        # Create bot with redaction settings (validation happens at serializer level)
        bot = self._create_bot_with_redaction_settings(["pii", "pci"])
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with settings passed directly (no validation)
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs
        self.assertEqual(call_kwargs["redact"], ["pii", "pci"])

    @mock.patch("deepgram.DeepgramClient")
    @mock.patch("deepgram.PrerecordedOptions")
    def test_multiple_redaction_types_properly_formatted_and_passed(self, mock_prerecorded_options, mock_deepgram_client):
        """Test that multiple redaction types are properly formatted and passed to PrerecordedOptions."""
        # Create bot with multiple redaction settings in specific order
        redaction_settings = ["numbers", "pii", "pci"]
        bot = self._create_bot_with_redaction_settings(redaction_settings)
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with all redaction types in correct order
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs

        # Verify redaction parameter contains all types in correct order
        self.assertEqual(call_kwargs["redact"], ["numbers", "pii", "pci"])
        self.assertEqual(len(call_kwargs["redact"]), 3)

    @mock.patch("deepgram.DeepgramClient")
    @mock.patch("deepgram.PrerecordedOptions")
    def test_redaction_settings_integration_with_other_deepgram_options(self, mock_prerecorded_options, mock_deepgram_client):
        """Test that redaction settings work correctly when combined with other Deepgram options."""
        # Create bot with redaction and other deepgram settings
        bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/test", settings={"transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-2", "redact": ["pii", "numbers"], "keywords": ["meeting", "agenda"]}}})
        utterance = self._create_utterance_for_bot(bot)

        # Mock successful transcription response
        mock_response = mock.Mock()
        mock_response.results.channels = [mock.Mock()]
        mock_response.results.channels[0].alternatives = [mock.Mock()]
        mock_response.results.channels[0].alternatives[0].to_json.return_value = '{"transcript": "test transcript"}'

        mock_client_instance = mock.Mock()
        mock_client_instance.listen.rest.v.return_value.transcribe_file.return_value = mock_response
        mock_deepgram_client.return_value = mock_client_instance

        # Call the transcription function
        transcription, failure = get_transcription_via_deepgram(utterance)

        # Verify PrerecordedOptions was called with all settings including redaction
        mock_prerecorded_options.assert_called_once()
        call_kwargs = mock_prerecorded_options.call_args.kwargs

        # Verify all parameters are correctly set
        self.assertEqual(call_kwargs["redact"], ["pii", "numbers"])
        self.assertEqual(call_kwargs["language"], "en-US")
        self.assertEqual(call_kwargs["model"], "nova-2")
        self.assertEqual(call_kwargs["keywords"], ["meeting", "agenda"])
        self.assertEqual(call_kwargs["smart_format"], True)
        self.assertEqual(call_kwargs["encoding"], "linear16")
        self.assertEqual(call_kwargs["sample_rate"], utterance.audio_chunk.sample_rate)

    def _run_task(self):
        """Invoke the Celery task with the test utterance id (synchronously)."""
        from bots.tasks.process_utterance_task import process_utterance

        process_utterance.apply(args=[self.utterance.id])

    # ------------------------------------------------------------------

    @mock.patch("bots.tasks.process_utterance_task.is_retryable_failure", return_value=False)
    @mock.patch("bots.tasks.process_utterance_task.get_transcription")
    def test_non_retryable_failure_sets_failure_data(self, mock_get_transcription, mock_is_retryable):
        """Non‑retryable failure → no exception, failure_data stored."""
        # Create utterance for this test
        bot = self._create_bot_with_redaction_settings(None)
        utterance = self._create_utterance_for_bot(bot)

        failure = {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        mock_get_transcription.return_value = (None, failure)

        # Should NOT raise
        from bots.tasks.process_utterance_task import process_utterance

        process_utterance.apply(args=[utterance.id])
        utterance.refresh_from_db()

        self.assertEqual(utterance.transcription_attempt_count, 1)
        self.assertEqual(utterance.failure_data, failure)

    # ------------------------------------------------------------------

    @mock.patch("bots.tasks.process_utterance_task.get_transcription")
    def test_existing_failure_data_short_circuits_task(self, mock_get_transcription):
        """If utterance already failed, task should exit early and not call provider."""
        # Create utterance for this test
        bot = self._create_bot_with_redaction_settings(None)
        utterance = self._create_utterance_for_bot(bot)

        utterance.failure_data = {"reason": TranscriptionFailureReasons.INTERNAL_ERROR}
        utterance.save(update_fields=["failure_data"])

        from bots.tasks.process_utterance_task import process_utterance

        process_utterance.apply(args=[utterance.id])

        # Provider function never invoked
        mock_get_transcription.assert_not_called()


import json
import types
from unittest import mock

from django.test import TransactionTestCase


def _build_fake_deepgram(success=True, err_code=None):
    """
    Return a fake 'deepgram' module and (optionally) the
    expected transcript text for the success case.
    """
    fake = types.ModuleType("deepgram")

    # ------------------------------------------------------------------ #
    # 1. DeepgramApiError
    class FakeDGError(Exception):
        def __init__(self, original_error):
            super().__init__("DG error")
            self.original_error = original_error

    fake.DeepgramApiError = FakeDGError

    # ------------------------------------------------------------------ #
    # 2. DeepgramClient mock & its chained method calls
    client_instance = mock.Mock(name="DeepgramClientInstance")

    if success:
        alt = mock.Mock()
        alt.to_json.return_value = json.dumps({"transcript": "hello"})
        channel = mock.Mock(alternatives=[alt])
        response = mock.Mock(results=mock.Mock(channels=[channel]))
        (client_instance.listen.rest.v.return_value.transcribe_file).return_value = response
    else:
        (client_instance.listen.rest.v.return_value.transcribe_file).side_effect = FakeDGError(json.dumps({"err_code": err_code}))

    fake.DeepgramClient = mock.Mock(return_value=client_instance)

    # ------------------------------------------------------------------ #
    # 3. Other names used in the provider
    fake.FileSource = dict
    fake.PrerecordedOptions = mock.Mock()
    return fake


class DeepgramProviderTest(TransactionTestCase):
    """Direct tests for get_transcription_via_deepgram using mock.patch."""

    def setUp(self):
        # ────────────────────────────────────────────────────────────────
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="P", organization=self.org)
        self.bot = self.project.bots.create(meeting_url="https://zoom.us/j/123")

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
        )
        self.participant = Participant.objects.create(bot=self.bot, uuid=str(uuid.uuid4()))
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"\x01\x02", timestamp_ms=0, duration_ms=500, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=500,
        )
        self.utterance.refresh_from_db()
        # Minimal Deepgram creds
        Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)
        mock.patch.object(Credentials, "get_credentials", return_value={"api_key": "dg_key"}).start()
        self.addCleanup(mock.patch.stopall)

    # ────────────────────────────────────────────────────────────────────
    def _call_with_fake_module(self, fake_module):
        """
        Helper that patches builtins.__import__ so that only imports of
        'deepgram' get our fake module, everything else behaves normally.
        """
        real_import = __import__

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "deepgram":
                return fake_module
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=_import):
            return get_transcription_via_deepgram(self.utterance)

    # ------------------------------------------------------------------ #
    def test_deepgram_success(self):
        fake = _build_fake_deepgram(success=True)
        transcription, failure = self._call_with_fake_module(fake)

        self.assertIsNone(failure)
        self.assertEqual(transcription, {"transcript": "hello"})

    # ------------------------------------------------------------------ #
    def test_deepgram_invalid_auth(self):
        fake = _build_fake_deepgram(success=False, err_code="INVALID_AUTH")
        transcription, failure = self._call_with_fake_module(fake)

        self.assertIsNone(transcription)
        self.assertEqual(
            failure,
            {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID},
        )

    # ------------------------------------------------------------------ #
    def test_deepgram_other_error(self):
        fake = _build_fake_deepgram(success=False, err_code="SOME_OTHER")
        transcription, failure = self._call_with_fake_module(fake)

        self.assertIsNone(transcription)
        self.assertEqual(
            failure,
            {
                "reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
                "error_code": "SOME_OTHER",
                "error_json": {"err_code": "SOME_OTHER"},
            },
        )


from unittest import mock

from django.test import TransactionTestCase

from bots.models import (
    Credentials as CredModel,
)


class BotModelTest(TransactionTestCase):
    """Unit tests for Bot model methods related to OpenAI transcription configuration"""

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/test")

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_openai_transcription_model_default_without_env(self):
        """Test that the default model is used when no env var or settings are present"""
        model = self.bot.transcription_settings.openai_transcription_model()
        self.assertEqual(model, "gpt-4o-transcribe")

    @mock.patch.dict("os.environ", {"OPENAI_MODEL_NAME": "custom-env-model"})
    def test_openai_transcription_model_env_var_fallback(self):
        """Test that env var is used as fallback when no bot settings are present"""
        model = self.bot.transcription_settings.openai_transcription_model()
        self.assertEqual(model, "custom-env-model")

    @mock.patch.dict("os.environ", {"OPENAI_MODEL_NAME": "custom-env-model"})
    def test_openai_transcription_model_settings_override_env(self):
        """Test that bot settings override env var"""
        self.bot.settings = {"transcription_settings": {"openai": {"model": "settings-model"}}}
        self.bot.save()
        model = self.bot.transcription_settings.openai_transcription_model()
        self.assertEqual(model, "settings-model")

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_openai_transcription_model_settings_override_default(self):
        """Test that bot settings override default"""
        self.bot.settings = {"transcription_settings": {"openai": {"model": "settings-model"}}}
        self.bot.save()
        model = self.bot.transcription_settings.openai_transcription_model()
        self.assertEqual(model, "settings-model")

    @mock.patch.dict("os.environ", {"OPENAI_MODEL_NAME": "gpt-4o-transcribe-diarize"})
    def test_openai_transcription_response_format_diarize_model(self):
        """Test defaults for response_format and chunking_strategy for gpt-4o-transcribe-diarize"""
        response_format = self.bot.transcription_settings.openai_transcription_response_format()
        chunking_strategy = self.bot.transcription_settings.openai_transcription_chunking_strategy()
        self.assertEqual(response_format, "diarized_json")
        self.assertEqual(chunking_strategy, "auto")


class GladiaProviderTest(TransactionTestCase):
    """Unit‑tests for bots.tasks.process_utterance_task.get_transcription_via_gladia"""

    def setUp(self):
        # Minimal DB fixtures ---------------------------------------------------------------
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/999")

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,  # finished recording
            transcription_provider=3,  # GLADIA
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid="p1")
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"pcm-bytes", timestamp_ms=0, duration_ms=600, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=600,
        )
        self.utterance.refresh_from_db()
        # "Real" credential row – we'll monkey‑patch get_credentials() later
        self.cred = Credentials.objects.create(
            project=self.project,
            credential_type=CredModel.CredentialTypes.GLADIA,
        )

    # ------------------------------------------------------------------ helpers

    def _patch_creds(self):
        """Always return a fake API key."""
        return mock.patch.object(CredModel, "get_credentials", return_value={"api_key": "fake‑key"})

    # ------------------------------------------------------------------ SUCCESS PATH

    def test_happy_path(self):
        """Upload → transcribe → poll → delete succeeds and returns formatted transcript."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.request") as m_request,
            mock.patch("bots.tasks.process_utterance_task.requests.get") as m_get,
        ):
            # ---- requests.request calls: upload, transcribe, delete -----------------------
            def _request_side_effect(method, url, **_):
                if url.endswith("/upload"):
                    resp = mock.Mock(status_code=201)
                    resp.json.return_value = {"audio_url": "https://api.gladia.io/audio/123"}
                    return resp

                if url.endswith("/pre-recorded"):
                    resp = mock.Mock(status_code=201)
                    resp.json.return_value = {"result_url": "https://api.gladia.io/result/abc"}
                    return resp

                if url.startswith("https://api.gladia.io/result/abc") and method == "DELETE":
                    return mock.Mock(status_code=202)

                raise AssertionError(f"unexpected {method} {url}")

            m_request.side_effect = _request_side_effect

            # ---- requests.get (polling) returns "done" once --------------------------------
            done_resp = mock.Mock(status_code=200)
            done_resp.json.return_value = {
                "status": "done",
                "result": {
                    "transcription": {
                        "full_transcript": "hello world",
                        "utterances": [{"speaker": 0, "words": [{"word": "hello"}, {"word": "world"}]}],
                    }
                },
            }
            m_get.return_value = done_resp

            transcript, failure = get_transcription_via_gladia(self.utterance)

            # Assertions --------------------------------------------------------------------
            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "hello world")
            self.assertEqual([w["word"] for w in transcript["words"]], ["hello", "world"])

            # upload + transcribe + delete = 3 calls
            self.assertEqual(m_request.call_count, 3)
            m_get.assert_called_once_with("https://api.gladia.io/result/abc", headers=mock.ANY)

    # ------------------------------------------------------------------ INVALID CREDENTIALS

    def test_upload_401_returns_credentials_invalid(self):
        """Gladia 401 on upload → CREDENTIALS_INVALID."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.request") as m_request,
        ):
            resp401 = mock.Mock(status_code=401)
            m_request.return_value = resp401

            transcript, failure = get_transcription_via_gladia(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_INVALID)

    # ------------------------------------------------------------------ NO CREDENTIAL ROW

    def test_missing_credentials_row(self):
        """No Credentials row → CREDENTIALS_NOT_FOUND."""
        # Remove the credential row created in setUp
        self.cred.delete()

        transcript, failure = get_transcription_via_gladia(self.utterance)

        self.assertIsNone(transcript)
        self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND)


from unittest import mock

from django.test import TransactionTestCase


class OpenAIProviderTest(TransactionTestCase):
    """Unit‑tests for get_transcription_via_openai"""

    def setUp(self):
        # ── minimal model graph ───────────────────────────────────────────────────────
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://example.com/meet")

        # Finished recording waiting on transcription
        self.rec = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.IN_PROGRESS,
            transcription_provider=4,  # TranscriptionProviders.OPENAI
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid="p‑1")
        self.audio_chunk = AudioChunk.objects.create(recording=self.rec, participant=self.participant, audio_blob=b"pcm", timestamp_ms=0, duration_ms=100, sample_rate=16000)
        self.utt = Utterance.objects.create(
            recording=self.rec,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=100,
        )
        self.utt.refresh_from_db()
        # Real credentials row (the crypto doesn't matter – we patch .get_credentials)
        self.creds = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.OPENAI)

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_success_path(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"text": "hello!"}
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        self.assertEqual(tx, {"transcript": "hello!"})
        mock_pcm.assert_called_once_with(b"pcm", sample_rate=16_000)
        mock_post.assert_called_once()  # ensure request made

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_invalid_credentials(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 401
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "bad"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(tx)
        self.assertEqual(
            failure,
            {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID},
        )

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_request_failure(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "boom"
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(tx)
        self.assertEqual(
            failure,
            {
                "reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
                "status_code": 500,
                "response_text": "boom",
            },
        )

    # ────────────────────────────────────────────────────────────────────────────────
    def test_no_credentials(self):
        # Remove the credentials row
        self.creds.delete()
        tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(tx)
        self.assertEqual(failure, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND})

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    @mock.patch.dict("os.environ", {"OPENAI_BASE_URL": "https://custom.openai.com/v1"})
    def test_custom_base_url_from_env(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"text": "custom endpoint!"}
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        self.assertEqual(tx, {"transcript": "custom endpoint!"})
        # Verify that the custom base URL was used
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://custom.openai.com/v1/audio/transcriptions")

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    @mock.patch.dict("os.environ", {"OPENAI_MODEL_NAME": "custom-model"})
    def test_custom_model_name_from_env(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"text": "custom model!"}
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        self.assertEqual(tx, {"transcript": "custom model!"})
        # Verify that the custom model name was used
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        files_dict = call_args[1]["files"]
        self.assertEqual(files_dict["model"][1], "custom-model")

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    @mock.patch.dict("os.environ", {"OPENAI_BASE_URL": "https://custom-ai-endpoint.example.com/v1", "OPENAI_MODEL_NAME": "gpt-4-turbo-transcribe"})
    def test_both_env_vars_together(self, mock_pcm, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"text": "both custom!"}
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        self.assertEqual(tx, {"transcript": "both custom!"})
        # Verify both custom values were used
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        print("call_args", call_args)
        self.assertEqual(call_args[0][0], "https://custom-ai-endpoint.example.com/v1/audio/transcriptions")
        files_dict = call_args[1]["files"]
        self.assertEqual(files_dict["model"][1], "gpt-4-turbo-transcribe")

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_diarize_model_with_response_format(self, mock_pcm, mock_post):
        """Test that response_format is sent when using gpt-4o-transcribe-diarize"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "text": "Hello world",
            "segments": [
                {
                    "type": "transcript.text.segment",
                    "id": "seg_001",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "Hello world",
                    "speaker": "A"
                }
            ],
            "duration": 2.0
        }
        
        # Set up bot with diarize model and response_format
        self.bot.settings = {
            "transcription_settings": {
                "openai": {
                    "model": "gpt-4o-transcribe-diarize",
                    "response_format": "diarized_json"
                }
            }
        }
        self.bot.save()
        self.utt.refresh_from_db()
        
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        self.assertEqual(tx["transcript"], "Hello world")
        self.assertIn("segments", tx)
        self.assertEqual(len(tx["segments"]), 1)
        self.assertEqual(tx["duration"], 2.0)
        
        # Verify response_format was sent
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        files_dict = call_args[1]["files"]
        self.assertEqual(files_dict["response_format"][1], "diarized_json")

    # ────────────────────────────────────────────────────────────────────────────────
    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_diarize_model_with_server_vad_chunking(self, mock_pcm, mock_post):
        """Test that chunking_strategy with server_vad is sent correctly"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"text": "Test transcription"}
        
        # Set up bot with diarize model and server_vad chunking
        self.bot.settings = {
            "transcription_settings": {
                "openai": {
                    "model": "gpt-4o-transcribe-diarize",
                    "chunking_strategy": {
                        "type": "server_vad",
                        "prefix_padding_ms": 500,
                        "silence_duration_ms": 300,
                        "threshold": 0.7
                    }
                }
            }
        }
        self.bot.save()
        self.utt.refresh_from_db()
        
        with mock.patch.object(self.creds.__class__, "get_credentials", return_value={"api_key": "sk‑XYZ"}):
            tx, failure = get_transcription_via_openai(self.utt)

        self.assertIsNone(failure)
        
        # Verify chunking_strategy was sent as JSON string
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        files_dict = call_args[1]["files"]
        import json
        chunking_strategy = json.loads(files_dict["chunking_strategy"][1])
        self.assertEqual(chunking_strategy["type"], "server_vad")
        self.assertEqual(chunking_strategy["prefix_padding_ms"], 500)
        self.assertEqual(chunking_strategy["silence_duration_ms"], 300)
        self.assertEqual(chunking_strategy["threshold"], 0.7)

class OpenAIModelValidationTest(TransactionTestCase):
    """Tests for OpenAI model validation in serializers"""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_get_openai_model_enum_default(self):
        """Test that default models are returned when no env var is set"""
        from bots.serializers import get_openai_model_enum

        models = get_openai_model_enum()
        expected = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe-diarize"]
        self.assertEqual(models, expected)

    @mock.patch.dict("os.environ", {"OPENAI_MODEL_NAME": "custom-whisper-model"})
    def test_get_openai_model_enum_custom_model(self):
        """Test that custom model is added to enum when env var is set"""
        from bots.serializers import get_openai_model_enum

        models = get_openai_model_enum()
        expected = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe-diarize", "custom-whisper-model"]
        self.assertEqual(models, expected)

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_transcription_settings_validation_default_models(self):
        """Test that default OpenAI models are accepted in validation"""
        from bots.serializers import CreateBotSerializer

        # Provide initial data with a meeting URL that supports OpenAI transcription
        data = {
            "meeting_url": "https://zoom.us/j/123456789",
            "bot_name": "Test Bot",
        }
        serializer = CreateBotSerializer(data=data)

        valid_settings = {"openai": {"model": "gpt-4o-transcribe"}}
        validated = serializer.validate_transcription_settings(valid_settings)
        self.assertEqual(validated["openai"]["model"], "gpt-4o-transcribe")

    @mock.patch("bots.serializers.os.getenv")
    def test_transcription_settings_validation_custom_model(self, mock_getenv):
        """Test that custom model from env var is accepted in validation"""
        mock_getenv.side_effect = lambda key, default=None: {
            "OPENAI_MODEL_NAME": "custom-whisper-model",
        }.get(key, default)

        # Import and reload the serializers module to pick up the mocked getenv
        import importlib

        from bots import serializers

        importlib.reload(serializers)

        # Provide initial data with a meeting URL that supports OpenAI transcription
        data = {
            "meeting_url": "https://zoom.us/j/123456789",
            "bot_name": "Test Bot",
        }
        serializer = serializers.CreateBotSerializer(data=data)

        valid_settings = {"openai": {"model": "custom-whisper-model"}}
        validated = serializer.validate_transcription_settings(valid_settings)
        self.assertEqual(validated["openai"]["model"], "custom-whisper-model")


class AssemblyAIProviderTest(TransactionTestCase):
    """Unit‑tests for bots.tasks.process_utterance_task.get_transcription_via_assemblyai"""

    def setUp(self):
        # Minimal DB fixtures
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/999")

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
            transcription_provider=5,  # ASSEMBLY_AI
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid="p1")
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"pcm-bytes", timestamp_ms=0, duration_ms=600, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=600,
        )
        self.utterance.refresh_from_db()
        self.cred = CredModel.objects.create(
            project=self.project,
            credential_type=CredModel.CredentialTypes.ASSEMBLY_AI,
        )

    def _patch_creds(self):
        """Always return a fake API key."""
        return mock.patch.object(CredModel, "get_credentials", return_value={"api_key": "fake-assembly-key"})

    def test_happy_path(self):
        """Upload → transcribe → poll succeeds and returns formatted transcript."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
            mock.patch("bots.tasks.process_utterance_task.requests.get") as m_get,
            mock.patch("bots.tasks.process_utterance_task.requests.delete") as m_delete,
        ):
            # 1. Mock upload response
            upload_response = mock.Mock(status_code=200)
            upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/upload/123"}

            # 2. Mock transcript creation response
            transcript_response = mock.Mock(status_code=200)
            transcript_response.json.return_value = {"id": "transcript-abc"}

            m_post.side_effect = [upload_response, transcript_response]

            # 3. Mock polling responses
            processing_response = mock.Mock(status_code=200)
            processing_response.json.return_value = {"status": "processing"}

            done_response = mock.Mock(status_code=200)
            done_response.json.return_value = {
                "status": "completed",
                "text": "hello assembly",
                "words": [
                    {"text": "hello", "start": 100, "end": 200, "confidence": 0.9},
                    {"text": "assembly", "start": 300, "end": 500, "confidence": 0.95},
                ],
            }
            m_get.side_effect = [processing_response, done_response]

            # 4. Mock delete response
            delete_response = mock.Mock(status_code=200)
            m_delete.return_value = delete_response

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            # Assertions
            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "hello assembly")
            self.assertEqual(len(transcript["words"]), 2)
            self.assertEqual(transcript["words"][0]["word"], "hello")

            self.assertEqual(m_post.call_count, 2)
            self.assertEqual(m_get.call_count, 2)
            m_delete.assert_called_once_with("https://api.assemblyai.com/v2/transcript/transcript-abc", headers=mock.ANY)

    def test_upload_401_returns_credentials_invalid(self):
        """AssemblyAI 401 on upload → CREDENTIALS_INVALID."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
        ):
            resp401 = mock.Mock(status_code=401)
            m_post.return_value = resp401

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_INVALID)
            m_post.assert_called_once()

    def test_missing_credentials_row(self):
        """No Credentials row → CREDENTIALS_NOT_FOUND."""
        self.cred.delete()
        transcript, failure = get_transcription_via_assemblyai(self.utterance)
        self.assertIsNone(transcript)
        self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND)

    def test_transcription_request_failed(self):
        """A non-200 response when creating the transcript job is handled."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
        ):
            upload_response = mock.Mock(status_code=200)
            upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/upload/123"}

            transcript_response = mock.Mock(status_code=400, text="Bad Request")

            m_post.side_effect = [upload_response, transcript_response]

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED)
            self.assertEqual(failure["status_code"], 400)

    def test_polling_error(self):
        """An 'error' status during polling is handled."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
            mock.patch("bots.tasks.process_utterance_task.requests.get") as m_get,
        ):
            upload_response = mock.Mock(status_code=200)
            upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/upload/123"}
            transcript_response = mock.Mock(status_code=200)
            transcript_response.json.return_value = {"id": "transcript-abc"}
            m_post.side_effect = [upload_response, transcript_response]

            error_response = mock.Mock(status_code=200)
            error_response.json.return_value = {"status": "error", "error": "Something went wrong"}
            m_get.return_value = error_response

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED)
            self.assertEqual(failure["error"], "Something went wrong")
            m_get.assert_called_once()

    def test_polling_timeout(self):
        """Polling that never completes results in a TIMED_OUT failure."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
            mock.patch("bots.tasks.process_utterance_task.requests.get") as m_get,
            mock.patch("bots.tasks.process_utterance_task.time.sleep"),  # speed up test
        ):
            upload_response = mock.Mock(status_code=200)
            upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/upload/123"}
            transcript_response = mock.Mock(status_code=200)
            transcript_response.json.return_value = {"id": "transcript-abc"}
            m_post.side_effect = [upload_response, transcript_response]

            processing_response = mock.Mock(status_code=200)
            processing_response.json.return_value = {"status": "processing"}
            m_get.return_value = processing_response

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.TIMED_OUT)
            # The code has max_retries = 120
            self.assertEqual(m_get.call_count, 120)

    def test_keyterms_prompt_and_speech_model_included(self):
        """Test that keyterms_prompt and speech_model are included in the AssemblyAI request if set in settings."""
        self.bot.settings = {
            "transcription_settings": {
                "assembly_ai": {
                    "keyterms_prompt": ["aws", "azure", "google cloud"],
                    "speech_model": "slam-1",
                }
            }
        }
        self.bot.save()
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
            mock.patch("bots.tasks.process_utterance_task.requests.get") as m_get,
            mock.patch("bots.tasks.process_utterance_task.requests.delete") as m_delete,
        ):
            # 1. Mock upload response
            upload_response = mock.Mock(status_code=200)
            upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/upload/123"}

            # 2. Mock transcript creation response
            transcript_response = mock.Mock(status_code=200)
            transcript_response.json.return_value = {"id": "transcript-abc"}

            m_post.side_effect = [upload_response, transcript_response]

            # 3. Mock polling responses
            done_response = mock.Mock(status_code=200)
            done_response.json.return_value = {
                "status": "completed",
                "text": "hello assembly",
                "words": [],
            }
            m_get.return_value = done_response

            # 4. Mock delete response
            delete_response = mock.Mock(status_code=200)
            m_delete.return_value = delete_response

            transcript, failure = get_transcription_via_assemblyai(self.utterance)

            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "hello assembly")

            # Check that the transcript creation request included keyterms_prompt and speech_model
            # The second call to requests.post is the transcript creation
            transcript_call = m_post.call_args_list[1]
            _, kwargs = transcript_call
            data = kwargs["json"]
            self.assertIn("keyterms_prompt", data)
            self.assertEqual(data["keyterms_prompt"], ["aws", "azure", "google cloud"])
            self.assertIn("speech_model", data)
            self.assertEqual(data["speech_model"], "slam-1")


from unittest import mock

from django.test import TransactionTestCase


class SarvamProviderTest(TransactionTestCase):
    """Unit‑tests for bots.tasks.process_utterance_task.get_transcription_via_sarvam"""

    def setUp(self):
        # Minimal DB fixtures
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/999")

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
            transcription_provider=6,  # SARVAM
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid="p1")
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"pcm-bytes", timestamp_ms=0, duration_ms=600, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=600,
        )
        self.utterance.refresh_from_db()
        self.cred = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.SARVAM,
        )

    def _patch_creds(self):
        """Always return a fake API key."""
        return mock.patch.object(Credentials, "get_credentials", return_value={"api_key": "fake-sarvam-key"})

    def test_happy_path(self):
        """Successful transcription returns formatted transcript."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
        ):
            success_response = mock.Mock(status_code=200)
            success_response.json.return_value = {"transcript": "hello sarvam"}
            m_post.return_value = success_response

            transcript, failure = get_transcription_via_sarvam(self.utterance)

            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "hello sarvam")
            m_post.assert_called_once()

    def test_invalid_credentials(self):
        """Sarvam 403 on request → CREDENTIALS_INVALID."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
        ):
            resp403 = mock.Mock(status_code=403)
            m_post.return_value = resp403

            transcript, failure = get_transcription_via_sarvam(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_INVALID)

    def test_rate_limit_exceeded(self):
        """Sarvam 429 on request → RATE_LIMIT_EXCEEDED."""
        with (
            self._patch_creds(),
            mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3"),
            mock.patch("bots.tasks.process_utterance_task.requests.post") as m_post,
        ):
            resp429 = mock.Mock(status_code=429)
            m_post.return_value = resp429

            transcript, failure = get_transcription_via_sarvam(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED)
            self.assertEqual(failure["status_code"], 429)


class ElevenLabsProviderTest(TransactionTestCase):
    """Unit‑tests for bots.tasks.process_utterance_task.get_transcription_via_elevenlabs"""

    def setUp(self):
        # Minimal DB fixtures ---------------------------------------------------------------
        self.org = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.org)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/999")

        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,  # finished recording
            transcription_provider=7,  # ELEVENLABS
        )

        self.participant = Participant.objects.create(bot=self.bot, uuid="p1")
        self.audio_chunk = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"pcm-bytes", timestamp_ms=0, duration_ms=600, sample_rate=16000)
        self.utterance = Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            audio_chunk=self.audio_chunk,
            timestamp_ms=0,
            duration_ms=600,
        )
        self.utterance.refresh_from_db()

        # "Real" credential row – we'll monkey‑patch get_credentials() later
        self.cred = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.ELEVENLABS,
        )

    # ------------------------------------------------------------------ helpers

    def _patch_creds(self):
        """Always return a fake API key."""
        return mock.patch.object(Credentials, "get_credentials", return_value={"api_key": "fake‑key"})

    # ------------------------------------------------------------------ SUCCESS PATH

    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_success_path(self, mock_pcm, mock_post):
        """ElevenLabs transcription succeeds and returns formatted transcript with words."""
        with self._patch_creds():
            # Mock successful response from ElevenLabs API
            mock_response = mock.Mock(status_code=200)
            mock_response.json.return_value = {"text": "hello world", "language_code": "eng", "language_probability": 0.9, "words": [{"text": "hello", "start": 0.0, "end": 0.5}, {"text": "world", "start": 0.6, "end": 1.0}]}
            mock_post.return_value = mock_response

            transcript, failure = get_transcription_via_elevenlabs(self.utterance)

            # Assertions
            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "hello world")
            self.assertEqual(len(transcript["words"]), 2)
            self.assertEqual(transcript["words"][0]["word"], "hello")
            self.assertEqual(transcript["words"][0]["start"], 0.0)
            self.assertEqual(transcript["words"][0]["end"], 0.5)
            self.assertEqual(transcript["words"][1]["word"], "world")
            self.assertEqual(transcript["words"][1]["start"], 0.6)
            self.assertEqual(transcript["words"][1]["end"], 1.0)
            self.assertEqual(transcript["language"], "eng")

            # Verify API call was made correctly
            mock_pcm.assert_called_once_with(b"pcm-bytes", sample_rate=16_000)
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            # First argument is the URL
            self.assertEqual(call_args[0][0], "https://api.elevenlabs.io/v1/speech-to-text")
            # Check headers in kwargs
            self.assertEqual(call_args[1]["headers"]["xi-api-key"], "fake‑key")

    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_success_path_with_bot_settings(self, mock_pcm, mock_post):
        """ElevenLabs transcription succeeds with bot-specific settings applied."""
        # Configure bot with ElevenLabs settings
        self.bot.settings = {"transcription_settings": {"elevenlabs": {"model_id": "scribe_v1_experimental", "language_code": "en", "tag_audio_events": True}}}
        self.bot.save()

        with self._patch_creds():
            # Mock successful response from ElevenLabs API
            mock_response = mock.Mock(status_code=200)
            mock_response.json.return_value = {"text": "test transcript", "language_probability": 0.8, "words": [{"text": "test", "start": 0.0, "end": 0.5}]}
            mock_post.return_value = mock_response

            transcript, failure = get_transcription_via_elevenlabs(self.utterance)

            # Assertions
            self.assertIsNone(failure)
            self.assertEqual(transcript["transcript"], "test transcript")

            # Verify settings were passed in the request
            call_args = mock_post.call_args
            data = call_args[1]["data"]
            self.assertEqual(data["model_id"], "scribe_v1_experimental")
            self.assertEqual(data["language_code"], "en")
            self.assertTrue(data["tag_audio_events"])

    def test_missing_credentials_row(self):
        """No Credentials row → CREDENTIALS_NOT_FOUND."""
        # Remove the credential row created in setUp
        self.cred.delete()

        transcript, failure = get_transcription_via_elevenlabs(self.utterance)

        self.assertIsNone(transcript)
        self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND)

    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_invalid_credentials_401(self, mock_pcm, mock_post):
        """ElevenLabs returns 401 → CREDENTIALS_INVALID."""
        with self._patch_creds():
            mock_response = mock.Mock(status_code=401)
            mock_post.return_value = mock_response

            transcript, failure = get_transcription_via_elevenlabs(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.CREDENTIALS_INVALID)

    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_request_failure_500(self, mock_pcm, mock_post):
        """ElevenLabs returns 500 → TRANSCRIPTION_REQUEST_FAILED."""
        with self._patch_creds():
            mock_response = mock.Mock(status_code=500)
            mock_response.text = "Internal Server Error"
            mock_post.return_value = mock_response

            transcript, failure = get_transcription_via_elevenlabs(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED)
            self.assertEqual(failure["status_code"], 500)
            self.assertEqual(failure["response_text"], "Internal Server Error")

    @mock.patch("bots.tasks.process_utterance_task.requests.post")
    @mock.patch("bots.tasks.process_utterance_task.pcm_to_mp3", return_value=b"mp3")
    def test_request_exception(self, mock_pcm, mock_post):
        """Network request exception → TRANSCRIPTION_REQUEST_FAILED."""
        with self._patch_creds():
            mock_post.side_effect = Exception("Network error")

            transcript, failure = get_transcription_via_elevenlabs(self.utterance)

            self.assertIsNone(transcript)
            self.assertEqual(failure["reason"], TranscriptionFailureReasons.INTERNAL_ERROR)
            self.assertIn("Network error", failure["error"])
