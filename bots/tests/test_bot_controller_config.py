from unittest import mock

from django.test import SimpleTestCase

from bots.bot_controller.bot_controller import BotController
from bots.models import TranscriptionProviders


class BotControllerConfigTest(SimpleTestCase):
    def _build_controller(self, provider):
        controller = BotController.__new__(BotController)
        controller.get_recording_transcription_provider = mock.Mock(return_value=provider)
        return controller

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_non_streaming_audio_silence_duration_limit_defaults_by_provider(self):
        sarvam_controller = self._build_controller(TranscriptionProviders.SARVAM)
        deepgram_controller = self._build_controller(TranscriptionProviders.DEEPGRAM)

        self.assertEqual(sarvam_controller.non_streaming_audio_silence_duration_limit(), 1)
        self.assertEqual(deepgram_controller.non_streaming_audio_silence_duration_limit(), 3)

    @mock.patch.dict("os.environ", {"TRANSCRIPTION_CHUNK_SILENCE_DURATION_SECONDS": "4.5"})
    def test_non_streaming_audio_silence_duration_limit_uses_env_override(self):
        sarvam_controller = self._build_controller(TranscriptionProviders.SARVAM)
        deepgram_controller = self._build_controller(TranscriptionProviders.DEEPGRAM)

        self.assertEqual(sarvam_controller.non_streaming_audio_silence_duration_limit(), 4.5)
        self.assertEqual(deepgram_controller.non_streaming_audio_silence_duration_limit(), 4.5)
