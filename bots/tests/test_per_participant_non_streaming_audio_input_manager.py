from datetime import datetime, timedelta

from django.test import SimpleTestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import (
    PerParticipantNonStreamingAudioInputManager,
)


class PerParticipantNonStreamingAudioInputManagerTest(SimpleTestCase):
    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=self.saved_chunks.append,
            get_participant_callback=lambda speaker_id: {"speaker_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=19200000,
            silence_duration_limit=3,
            min_speech_duration_limit=3,
            ignore_long_silence_enabled=True,
            max_silence_to_append_seconds=1,
            should_print_diagnostic_info=False,
        )
        # Keep tests deterministic: any non-None chunk is treated as speech.
        self.manager.silence_detected = lambda chunk_bytes: False

    def test_discards_chunk_when_minimum_speech_duration_not_reached(self):
        t0 = datetime.utcnow()
        # 2 seconds at 16kHz mono PCM16 = 64000 bytes
        speech_chunk = b"\x01\x00" * (16000 * 2)

        self.manager.process_chunk("speaker-1", t0, speech_chunk)
        self.manager.process_chunk("speaker-1", t0 + timedelta(seconds=4), None)

        self.assertEqual(len(self.saved_chunks), 0)
        self.assertEqual(
            self.manager.diagnostic_info["total_audio_chunks_discarded_because_min_speech_duration_not_reached"],
            1,
        )

    def test_sends_chunk_when_minimum_speech_duration_reached(self):
        t0 = datetime.utcnow()
        # 3.2 seconds at 16kHz mono PCM16 = 102400 bytes
        speech_chunk = b"\x01\x00" * 51200

        self.manager.process_chunk("speaker-1", t0, speech_chunk)
        self.manager.process_chunk("speaker-1", t0 + timedelta(seconds=4), None)

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["speaker_id"], "speaker-1")
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "silence_limit")
        self.assertEqual(self.saved_chunks[0]["sample_rate"], 16000)

    def test_silence_bytes_longer_than_threshold_are_not_appended_to_chunk_audio_data(self):
        t0 = datetime.utcnow()
        speech_chunk = b"\x01\x00" * 51200  # 3.2 seconds
        silence_chunk = b"\x00\x00" * 16000  # 1 second

        self.manager.silence_detected = lambda chunk_bytes: chunk_bytes == silence_chunk

        self.manager.process_chunk("speaker-1", t0, speech_chunk)
        self.manager.process_chunk("speaker-1", t0 + timedelta(seconds=4), silence_chunk)

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(len(self.saved_chunks[0]["audio_data"]), len(speech_chunk))

    def test_short_silence_is_appended_to_chunk_audio_data(self):
        t0 = datetime.utcnow()
        speech_chunk = b"\x01\x00" * 51200  # 3.2 seconds
        silence_chunk = b"\x00\x00" * 16000  # 1 second

        self.manager.silence_detected = lambda chunk_bytes: chunk_bytes == silence_chunk

        self.manager.process_chunk("speaker-1", t0, speech_chunk)
        # silence_duration = 0.5s -> below append threshold (1s), so this silence should be appended
        self.manager.process_chunk("speaker-1", t0 + timedelta(seconds=0.5), silence_chunk)
        self.manager.process_chunk("speaker-1", t0 + timedelta(seconds=4), None)

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(len(self.saved_chunks[0]["audio_data"]), len(speech_chunk) + len(silence_chunk))

    def test_long_silence_is_appended_when_ignore_long_silence_is_disabled(self):
        saved_chunks = []
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=saved_chunks.append,
            get_participant_callback=lambda speaker_id: {"speaker_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=19200000,
            silence_duration_limit=3,
            min_speech_duration_limit=3,
            ignore_long_silence_enabled=False,
            max_silence_to_append_seconds=1,
            should_print_diagnostic_info=False,
        )

        t0 = datetime.utcnow()
        speech_chunk = b"\x01\x00" * 51200  # 3.2 seconds
        silence_chunk = b"\x00\x00" * 16000  # 1 second
        manager.silence_detected = lambda chunk_bytes: chunk_bytes == silence_chunk

        manager.process_chunk("speaker-1", t0, speech_chunk)
        # silence_duration = 4s (> 1s). With ignore_long_silence_enabled=False, silence is still appended.
        manager.process_chunk("speaker-1", t0 + timedelta(seconds=4), silence_chunk)

        self.assertEqual(len(saved_chunks), 1)
        self.assertEqual(len(saved_chunks[0]["audio_data"]), len(speech_chunk) + len(silence_chunk))
