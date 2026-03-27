from datetime import datetime, timedelta

import numpy as np
from django.test import SimpleTestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import PerParticipantNonStreamingAudioInputManager


class PerParticipantNonStreamingAudioInputManagerTest(SimpleTestCase):
    def _make_speech_bytes(self, *, sample_rate, seconds):
        sample_count = sample_rate * seconds
        samples = np.full(sample_count, 1000, dtype=np.int16)
        return samples.tobytes()

    def test_minimum_segment_prevents_silence_flush_until_threshold_is_met(self):
        saved_chunks = []

        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda payload: saved_chunks.append(payload),
            get_participant_callback=lambda speaker_id: {"participant_uuid": speaker_id},
            sample_rate=16000,
            utterance_size_limit=16000 * 2 * 120,
            silence_duration_limit=1.0,
            minimum_segment_for_silence_closure_seconds=10,
            should_print_diagnostic_info=False,
        )

        speaker_id = "speaker-1"
        first_time = datetime.utcnow()
        speech_5s = self._make_speech_bytes(sample_rate=16000, seconds=5)

        manager.process_chunk(speaker_id, first_time, speech_5s)
        manager.process_chunk(speaker_id, first_time + timedelta(seconds=2), None)
        self.assertEqual(len(saved_chunks), 0)

        manager.process_chunk(speaker_id, first_time + timedelta(seconds=3), speech_5s)
        manager.process_chunk(speaker_id, first_time + timedelta(seconds=5), None)

        self.assertEqual(len(saved_chunks), 1)
        self.assertEqual(saved_chunks[0]["flush_reason"], "silence_limit")

    def test_flush_utterances_forces_flush_even_when_minimum_segment_not_met(self):
        saved_chunks = []

        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda payload: saved_chunks.append(payload),
            get_participant_callback=lambda speaker_id: {"participant_uuid": speaker_id},
            sample_rate=16000,
            utterance_size_limit=16000 * 2 * 120,
            silence_duration_limit=1.0,
            minimum_segment_for_silence_closure_seconds=10,
            should_print_diagnostic_info=False,
        )

        speaker_id = "speaker-1"
        first_time = datetime.utcnow()
        speech_5s = self._make_speech_bytes(sample_rate=16000, seconds=5)

        manager.process_chunk(speaker_id, first_time, speech_5s)
        manager.flush_utterances()

        self.assertEqual(len(saved_chunks), 1)
        self.assertEqual(saved_chunks[0]["flush_reason"], "silence_limit")
