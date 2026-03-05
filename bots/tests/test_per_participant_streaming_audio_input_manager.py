from unittest import mock

from django.test import TestCase

from accounts.models import Organization
from bots.bot_controller.per_participant_streaming_audio_input_manager import (
    PerParticipantStreamingAudioInputManager,
)
from bots.models import Bot, Project, TranscriptionProviders


class PerParticipantStreamingAudioInputManagerTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Org")
        project = Project.objects.create(name="Proj", organization=organization)
        self.bot = Bot.objects.create(
            project=project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={},
        )

    def _create_manager(self, provider):
        manager = PerParticipantStreamingAudioInputManager(
            get_participant_callback=lambda _speaker_id: {"participant_full_name": "Speaker"},
            sample_rate=16000,
            transcription_provider=provider,
            bot=self.bot,
        )
        manager.deepgram_api_key = "deepgram-key"
        manager.kyutai_server_url = "ws://kyutai.example.com"
        manager.openai_api_key = "openai-key"
        return manager

    def test_silent_chunk_is_not_sent_for_any_streaming_provider(self):
        providers = [
            TranscriptionProviders.DEEPGRAM,
            TranscriptionProviders.KYUTAI,
            TranscriptionProviders.OPENAI,
        ]

        for provider in providers:
            with self.subTest(provider=provider):
                manager = self._create_manager(provider)
                transcriber = mock.Mock()

                with (
                    mock.patch.object(manager, "streaming_silence_detected", return_value=True),
                    mock.patch.object(manager, "find_or_create_streaming_transcriber_for_speaker", return_value=transcriber) as find_mock,
                ):
                    manager.add_chunk("speaker-1", None, b"\x00\x00" * 160)

                find_mock.assert_not_called()
                transcriber.send.assert_not_called()

    def test_non_silent_chunk_is_sent_for_any_streaming_provider(self):
        providers = [
            TranscriptionProviders.DEEPGRAM,
            TranscriptionProviders.KYUTAI,
            TranscriptionProviders.OPENAI,
        ]

        for provider in providers:
            with self.subTest(provider=provider):
                manager = self._create_manager(provider)
                transcriber = mock.Mock()

                with (
                    mock.patch.object(manager, "streaming_silence_detected", return_value=False),
                    mock.patch.object(manager, "find_or_create_streaming_transcriber_for_speaker", return_value=transcriber) as find_mock,
                ):
                    manager.add_chunk("speaker-1", None, b"\x01\x02" * 160)

                find_mock.assert_called_once_with("speaker-1")
                transcriber.send.assert_called_once()

    def test_streaming_threshold_increases_with_noise_floor(self):
        manager = self._create_manager(TranscriptionProviders.OPENAI)
        manager.streaming_noise_floor_by_speaker["speaker-1"] = 0.01

        threshold = manager.get_streaming_silence_threshold_for_speaker("speaker-1")

        self.assertGreater(threshold, manager.streaming_silence_min_normalized_rms)

    def test_streaming_silence_detection_uses_dynamic_threshold_and_updates_noise_floor(self):
        manager = self._create_manager(TranscriptionProviders.OPENAI)

        with mock.patch(
            "bots.bot_controller.per_participant_streaming_audio_input_manager.calculate_normalized_rms",
            return_value=0.003,
        ):
            is_silent = manager.streaming_silence_detected("speaker-1", b"\x00\x00" * 160)

        self.assertTrue(is_silent)
        self.assertIn("speaker-1", manager.streaming_noise_floor_by_speaker)

    def test_non_speech_above_threshold_is_still_silence(self):
        manager = self._create_manager(TranscriptionProviders.OPENAI)
        manager.streaming_noise_floor_by_speaker["speaker-1"] = 0.001

        with (
            mock.patch(
                "bots.bot_controller.per_participant_streaming_audio_input_manager.calculate_normalized_rms",
                return_value=0.02,
            ),
            mock.patch.object(manager.vad, "is_speech", return_value=False),
        ):
            is_silent = manager.streaming_silence_detected("speaker-1", b"\x01\x02" * 160)

        self.assertTrue(is_silent)

    def test_speech_above_threshold_is_not_silence(self):
        manager = self._create_manager(TranscriptionProviders.OPENAI)
        manager.streaming_noise_floor_by_speaker["speaker-1"] = 0.001

        with (
            mock.patch(
                "bots.bot_controller.per_participant_streaming_audio_input_manager.calculate_normalized_rms",
                return_value=0.02,
            ),
            mock.patch.object(manager.vad, "is_speech", return_value=True),
        ):
            is_silent = manager.streaming_silence_detected("speaker-1", b"\x01\x02" * 160)

        self.assertFalse(is_silent)
