from unittest import mock

from django.test import SimpleTestCase

from bots.bot_controller.bot_controller import BotController


class BotControllerTranscriptionChunkConfigTest(SimpleTestCase):
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
