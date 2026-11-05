from datetime import datetime, timedelta

import numpy as np
from django.test import SimpleTestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import (
    PerParticipantNonStreamingAudioInputManager,
    calculate_normalized_rms,
)

SAMPLE_RATE = 16000
FRAME_MS = 20


def square_wave_frame(amplitude, duration_ms=FRAME_MS, sample_rate=SAMPLE_RATE):
    samples = np.full(sample_rate * duration_ms // 1000, amplitude, dtype=np.int16)
    samples[1::2] = -amplitude
    return samples.tobytes()


class RecordingVad:
    def __init__(self):
        self.calls = []

    def is_speech(self, chunk_bytes, sample_rate):
        self.calls.append((len(chunk_bytes), sample_rate))
        return True


class TestCalculateNormalizedRms(SimpleTestCase):
    def test_loud_audio_does_not_overflow(self):
        self.assertAlmostEqual(calculate_normalized_rms(square_wave_frame(3000)), 3000 / 32768, places=4)

    def test_quiet_room_tone_stays_finite_and_below_the_silence_threshold(self):
        rms = calculate_normalized_rms(square_wave_frame(200))

        self.assertTrue(np.isfinite(rms))
        self.assertAlmostEqual(rms, 200 / 32768, places=4)


class TestPerParticipantNonStreamingAudioInputManager(SimpleTestCase):
    def setUp(self):
        self.saved = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=self.saved.append,
            get_participant_callback=lambda speaker_id: {"participant_uuid": speaker_id},
            sample_rate=SAMPLE_RATE,
            utterance_size_limit=19_200_000,
            silence_duration_limit=3,
            should_print_diagnostic_info=False,
        )
        self.vad = RecordingVad()
        self.manager.vad = self.vad
        self.clock = datetime(2026, 10, 4, 18, 0, 0)

    def feed(self, amplitude, seconds):
        for _ in range(seconds * 1000 // FRAME_MS):
            self.manager.process_chunk("speaker", self.clock, square_wave_frame(amplitude))
            self.clock += timedelta(milliseconds=FRAME_MS)

    def test_a_20ms_frame_is_checked_by_the_vad(self):
        self.manager.is_speech(square_wave_frame(3000))

        self.assertEqual(self.vad.calls, [(640, SAMPLE_RATE)])
        self.assertEqual(self.manager.diagnostic_info["total_chunks_too_large_for_vad"], 0)

    def test_a_frame_longer_than_30ms_skips_the_vad(self):
        self.assertTrue(self.manager.is_speech(square_wave_frame(3000, duration_ms=40)))

        self.assertEqual(self.vad.calls, [])
        self.assertEqual(self.manager.diagnostic_info["total_chunks_too_large_for_vad"], 1)

    def test_a_pause_in_room_tone_splits_the_utterance(self):
        self.feed(amplitude=3000, seconds=1)
        self.feed(amplitude=200, seconds=4)
        self.feed(amplitude=3000, seconds=1)
        self.manager.flush_utterances()

        self.assertEqual([utterance["flush_reason"] for utterance in self.saved], ["silence_limit", "silence_limit"])
