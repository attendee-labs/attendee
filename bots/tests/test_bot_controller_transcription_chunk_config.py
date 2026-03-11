from unittest import mock

from django.test import SimpleTestCase

from bots.bot_controller.bot_controller import BotController


class BotControllerTranscriptionChunkConfigTest(SimpleTestCase):
    def _build_controller_for_speech_events(self, *, record_events=False, capture_audio_chunks=True, streaming_transcription=False):
        controller = BotController.__new__(BotController)
        controller.bot_in_db = mock.Mock()
        controller.bot_in_db.record_participant_speech_start_stop_events.return_value = record_events
        controller.should_capture_audio_chunks = mock.Mock(return_value=capture_audio_chunks)
        controller.use_streaming_transcription = mock.Mock(return_value=streaming_transcription)
        return controller

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_non_streaming_audio_min_speech_duration_limit_defaults_to_3_seconds(self):
        controller = BotController.__new__(BotController)
        self.assertEqual(controller.non_streaming_audio_min_speech_duration_limit(), 3)

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_MIN_SPEECH_DURATION_SECONDS": "4.5"})
    def test_non_streaming_audio_min_speech_duration_limit_uses_env_override(self):
        controller = BotController.__new__(BotController)
        self.assertEqual(controller.non_streaming_audio_min_speech_duration_limit(), 4.5)

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_MIN_SPEECH_DURATION_SECONDS": "invalid"})
    def test_non_streaming_audio_min_speech_duration_limit_invalid_env_falls_back_to_default(self):
        controller = BotController.__new__(BotController)
        self.assertEqual(controller.non_streaming_audio_min_speech_duration_limit(), 3)

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_non_streaming_audio_max_silence_to_append_seconds_defaults_to_1_second(self):
        controller = BotController.__new__(BotController)
        self.assertEqual(controller.non_streaming_audio_max_silence_to_append_seconds(), 1)

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_MAX_SILENCE_TO_APPEND_SECONDS": "1.5"})
    def test_non_streaming_audio_max_silence_to_append_seconds_uses_env_override(self):
        controller = BotController.__new__(BotController)
        self.assertEqual(controller.non_streaming_audio_max_silence_to_append_seconds(), 1.5)

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_non_streaming_audio_ignore_long_silence_enabled_defaults_to_true(self):
        controller = BotController.__new__(BotController)
        self.assertTrue(controller.non_streaming_audio_ignore_long_silence_enabled())

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_IGNORE_LONG_SILENCE_ENABLED": "false"})
    def test_non_streaming_audio_ignore_long_silence_enabled_uses_env_override(self):
        controller = BotController.__new__(BotController)
        self.assertFalse(controller.non_streaming_audio_ignore_long_silence_enabled())

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_non_streaming_audio_use_speech_events_as_primary_chunking_defaults_to_false(self):
        controller = BotController.__new__(BotController)
        self.assertFalse(controller.non_streaming_audio_use_speech_events_as_primary_chunking_enabled())

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_USE_SPEECH_EVENTS_PRIMARY_CHUNKING_ENABLED": "true"})
    def test_non_streaming_audio_use_speech_events_as_primary_chunking_uses_env_override(self):
        controller = BotController.__new__(BotController)
        self.assertTrue(controller.non_streaming_audio_use_speech_events_as_primary_chunking_enabled())

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_should_request_participant_speech_start_stop_events_defaults_to_false_when_not_recording_events(self):
        controller = self._build_controller_for_speech_events(record_events=False, capture_audio_chunks=True, streaming_transcription=False)
        self.assertFalse(controller.should_request_participant_speech_start_stop_events())

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_USE_SPEECH_EVENTS_PRIMARY_CHUNKING_ENABLED": "true"})
    def test_should_request_participant_speech_start_stop_events_true_when_primary_chunking_enabled(self):
        controller = self._build_controller_for_speech_events(record_events=False, capture_audio_chunks=True, streaming_transcription=False)
        self.assertTrue(controller.should_request_participant_speech_start_stop_events())

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_USE_SPEECH_EVENTS_PRIMARY_CHUNKING_ENABLED": "false"})
    def test_should_request_participant_speech_start_stop_events_true_when_recording_events_enabled(self):
        controller = self._build_controller_for_speech_events(record_events=True, capture_audio_chunks=False, streaming_transcription=True)
        self.assertTrue(controller.should_request_participant_speech_start_stop_events())
