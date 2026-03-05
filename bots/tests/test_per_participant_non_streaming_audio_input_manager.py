from unittest import mock

from django.test import TestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import (
    PerParticipantNonStreamingAudioInputManager,
)


class PerParticipantNonStreamingAudioInputManagerTest(TestCase):
    def _create_manager(self):
        return PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda _message: None,
            get_participant_callback=lambda _speaker_id: {"participant_uuid": "p1"},
            sample_rate=16000,
            utterance_size_limit=32000,
            silence_duration_limit=3,
            should_print_diagnostic_info=False,
        )

    def test_silence_threshold_increases_with_noise_floor(self):
        manager = self._create_manager()
        manager.noise_floor_by_speaker["speaker-1"] = 0.01

        threshold = manager.get_silence_threshold_for_speaker("speaker-1")

        self.assertGreater(threshold, manager.silence_min_normalized_rms)

    def test_silence_detected_when_below_dynamic_threshold(self):
        manager = self._create_manager()
        manager.noise_floor_by_speaker["speaker-1"] = 0.01

        with mock.patch(
            "bots.bot_controller.per_participant_non_streaming_audio_input_manager.calculate_normalized_rms",
            return_value=0.01,
        ):
            is_silent = manager.silence_detected("speaker-1", b"\x00\x00" * 160)

        self.assertTrue(is_silent)
        self.assertIn("speaker-1", manager.noise_floor_by_speaker)

    def test_non_speech_above_threshold_is_silent(self):
        manager = self._create_manager()

        with (
            mock.patch(
                "bots.bot_controller.per_participant_non_streaming_audio_input_manager.calculate_normalized_rms",
                return_value=0.03,
            ),
            mock.patch.object(manager, "is_speech", return_value=False),
        ):
            is_silent = manager.silence_detected("speaker-1", b"\x01\x02" * 160)

        self.assertTrue(is_silent)

    def test_speech_above_threshold_is_not_silent(self):
        manager = self._create_manager()

        with (
            mock.patch(
                "bots.bot_controller.per_participant_non_streaming_audio_input_manager.calculate_normalized_rms",
                return_value=0.03,
            ),
            mock.patch.object(manager, "is_speech", return_value=True),
        ):
            is_silent = manager.silence_detected("speaker-1", b"\x01\x02" * 160)

        self.assertFalse(is_silent)
