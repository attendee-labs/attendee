from datetime import datetime, timedelta

from django.test import SimpleTestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import (
    PerParticipantNonStreamingAudioInputManager,
)


class PerParticipantNonStreamingAudioInputManagerSpeechEventTest(SimpleTestCase):
    def setUp(self):
        self.saved_chunks = []
        self.participant = {
            "participant_uuid": "speaker-1",
            "participant_user_uuid": "user-1",
            "participant_full_name": "Speaker One",
            "participant_is_the_bot": False,
            "participant_is_host": False,
        }
        self.non_silent_audio = b"\xe8\x03" * 960  # int16 value 1000, 30ms at 32kHz
        self.silent_audio = b"\x00\x00" * 960  # 30ms of silence at 32kHz
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=self.saved_chunks.append,
            get_participant_callback=lambda speaker_id: self.participant if speaker_id == "speaker-1" else None,
            sample_rate=32000,
            utterance_size_limit=1000000,
            silence_duration_limit=3,
            min_speech_duration_limit=0.01,
            ignore_long_silence_enabled=True,
            max_silence_to_append_seconds=1,
            should_print_diagnostic_info=False,
            speech_stop_post_roll_seconds=5,
        )
        self.manager.silence_detected = lambda chunk_bytes: chunk_bytes == self.silent_audio

    def test_speech_stop_flushes_chunk_after_post_roll_window(self):
        start_time = datetime.utcnow()
        self.manager.add_speech_start_event("speaker-1", start_time + timedelta(milliseconds=500))
        self.manager.add_chunk("speaker-1", start_time, self.non_silent_audio)
        self.manager.add_chunk("speaker-1", start_time + timedelta(milliseconds=600), self.non_silent_audio)
        self.manager.add_speech_stop_event("speaker-1", start_time + timedelta(milliseconds=700))
        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=5), self.silent_audio)

        self.manager.process_chunks()

        # Still inside the 5s post-roll after SPEECH_STOP.
        self.assertEqual(len(self.saved_chunks), 0)

        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=6), self.silent_audio)
        self.manager.process_chunks()

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "speech_stop")
        self.assertEqual(self.saved_chunks[0]["timestamp_ms"], int(start_time.timestamp() * 1000))

    def test_speech_start_within_stop_window_cancels_pending_flush(self):
        start_time = datetime.utcnow()
        self.manager.add_speech_start_event("speaker-1", start_time)
        self.manager.add_chunk("speaker-1", start_time + timedelta(milliseconds=100), self.non_silent_audio)
        self.manager.add_speech_stop_event("speaker-1", start_time + timedelta(milliseconds=200))

        # Resume speaking before 5s passes, so the first stop should not flush.
        self.manager.add_speech_start_event("speaker-1", start_time + timedelta(seconds=3))
        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=3, milliseconds=100), self.non_silent_audio)

        # Next stop should flush only after another 5s with no speech.
        self.manager.add_speech_stop_event("speaker-1", start_time + timedelta(seconds=3, milliseconds=200))
        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=9), self.silent_audio)

        self.manager.process_chunks()

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "speech_stop")

    def test_custom_speech_stop_post_roll_seconds_is_respected(self):
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=self.saved_chunks.append,
            get_participant_callback=lambda speaker_id: self.participant if speaker_id == "speaker-1" else None,
            sample_rate=32000,
            utterance_size_limit=1000000,
            silence_duration_limit=3,
            min_speech_duration_limit=0.01,
            ignore_long_silence_enabled=True,
            max_silence_to_append_seconds=1,
            should_print_diagnostic_info=False,
            speech_stop_post_roll_seconds=1,
        )
        manager.silence_detected = lambda chunk_bytes: chunk_bytes == self.silent_audio

        start_time = datetime.utcnow()
        manager.add_speech_start_event("speaker-1", start_time)
        manager.add_chunk("speaker-1", start_time + timedelta(milliseconds=100), self.non_silent_audio)
        manager.add_speech_stop_event("speaker-1", start_time + timedelta(milliseconds=200))
        manager.add_chunk("speaker-1", start_time + timedelta(milliseconds=800), self.silent_audio)
        manager.process_chunks()
        self.assertEqual(len(self.saved_chunks), 0)

        manager.add_chunk("speaker-1", start_time + timedelta(seconds=2), self.silent_audio)
        manager.process_chunks()
        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "speech_stop")

    def test_fallback_silence_still_flushes_when_no_speech_events_arrive(self):
        start_time = datetime.utcnow()
        self.manager.add_chunk("speaker-1", start_time, self.non_silent_audio)
        self.manager.process_chunks()
        self.assertEqual(len(self.saved_chunks), 0)

        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=4), self.silent_audio)
        self.manager.process_chunks()

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "silence_limit")

    def test_queue_uses_timestamps_for_event_and_audio_ordering(self):
        start_time = datetime.utcnow()
        self.manager.add_speech_start_event("speaker-1", start_time)
        self.manager.add_speech_stop_event("speaker-1", start_time + timedelta(milliseconds=200))
        self.manager.add_chunk("speaker-1", start_time + timedelta(milliseconds=100), self.non_silent_audio)
        self.manager.add_chunk("speaker-1", start_time + timedelta(seconds=6), self.silent_audio)

        self.manager.process_chunks()

        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "speech_stop")


class PerParticipantNonStreamingAudioInputManagerChunkRulesTest(SimpleTestCase):
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
