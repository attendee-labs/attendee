import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
from django.test import TestCase

from bots.bot_controller.per_participant_non_streaming_audio_input_manager import PerParticipantNonStreamingAudioInputManager
from bots.bot_controller.vad import SileroVAD, WebRTCVAD, calculate_normalized_rms


def create_audio_chunk(frequency=440, duration_ms=100, sample_rate=16000, amplitude=0.5):
    """Generate a synthetic audio chunk with a sine wave (speech-like)."""
    num_samples = int(sample_rate * duration_ms / 1000)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    # Generate sine wave with proper amplitude
    samples = (amplitude * 32767 * np.sin(2 * np.pi * frequency * t)).astype(np.int16)
    return samples.tobytes()


def create_silent_chunk(duration_ms=100, sample_rate=16000):
    """Generate a silent audio chunk (zeros)."""
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = np.zeros(num_samples, dtype=np.int16)
    return samples.tobytes()


def create_noise_chunk(duration_ms=100, sample_rate=16000, noise_level=0.005):
    """Generate a chunk with low-level background noise (below RMS threshold)."""
    num_samples = int(sample_rate * duration_ms / 1000)
    # Generate random noise with low amplitude
    samples = (noise_level * 32767 * np.random.randn(num_samples)).astype(np.int16)
    return samples.tobytes()


def create_medium_amplitude_chunk(duration_ms=100, sample_rate=16000, amplitude=0.1):
    """
    Generate a chunk with medium amplitude that passes RMS check but relies on VAD.
    This is useful for testing VAD-based silence detection.
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    # Generate sine wave with amplitude above RMS threshold (0.01) but let VAD decide
    samples = (amplitude * 32767 * np.sin(2 * np.pi * 100 * t)).astype(np.int16)
    return samples.tobytes()


class TestCalculateNormalizedRms(TestCase):
    def test_silent_audio_returns_zero(self):
        silent_chunk = create_silent_chunk()
        rms = calculate_normalized_rms(silent_chunk)
        self.assertEqual(rms, 0.0)

    def test_loud_audio_returns_high_value(self):
        # A sine wave with amplitude 0.8 should have RMS of ~0.8/sqrt(2) ≈ 0.566
        loud_chunk = create_audio_chunk(amplitude=0.8)
        rms = calculate_normalized_rms(loud_chunk)
        self.assertGreater(rms, 0.5)

    def test_quiet_audio_returns_low_value(self):
        quiet_chunk = create_noise_chunk(noise_level=0.001)
        rms = calculate_normalized_rms(quiet_chunk)
        self.assertLess(rms, 0.01)

    def test_medium_amplitude_above_threshold(self):
        """Test that medium amplitude chunks pass the RMS threshold of 0.01."""
        chunk = create_medium_amplitude_chunk(amplitude=0.1)
        rms = calculate_normalized_rms(chunk)
        # 0.1 amplitude sine wave should have RMS of ~0.07
        self.assertGreater(rms, 0.01)


class TestTrailingSilenceTrimming(TestCase):
    """Tests for the trailing silence trimming feature."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=1000000,  # Large limit so we control flushing
            silence_duration_limit=0.5,  # 500ms silence triggers flush
            should_print_diagnostic_info=False,
        )

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_trailing_silence_is_trimmed(self, mock_is_speech):
        """Test that trailing silent chunks are removed from the output."""
        # Mock VAD to return True for first 3 chunks (speech), False for last 3 (silence)
        mock_is_speech.side_effect = [True, True, True, False, False, False]

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)

        # Use medium amplitude chunks that pass RMS check, so VAD mock is called
        speech_chunk = create_medium_amplitude_chunk(amplitude=0.1)
        silent_chunk = create_medium_amplitude_chunk(amplitude=0.1)  # Same chunk, VAD will decide

        # Add speech chunks
        for i in range(3):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, speech_chunk)

        # Add silent chunks (VAD mock will return False for these)
        for i in range(3):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * (3 + i), silent_chunk)

        # Process all chunks
        self.manager.process_chunks()

        # Force flush with silence exceeding limit
        flush_time = base_time + timedelta(seconds=1)
        self.manager.process_chunk(speaker_id, flush_time, None)

        # Verify one chunk was saved
        self.assertEqual(len(self.saved_chunks), 1)

        # Verify trailing silence was trimmed
        # The saved audio should contain only the 3 speech chunks
        expected_length = len(speech_chunk) * 3
        actual_length = len(self.saved_chunks[0]["audio_data"])
        self.assertEqual(actual_length, expected_length)

        # Check diagnostic info
        self.assertEqual(self.manager.diagnostic_info["total_trailing_silent_chunks_trimmed"], 3)

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_interleaved_speech_and_silence_preserves_middle_silence(self, mock_is_speech):
        """Test that silence between speech segments is preserved."""
        # Pattern: [SPEECH][SILENCE][SPEECH][SILENCE][SILENCE]
        # Expected output: [SPEECH][SILENCE][SPEECH] (trailing silence trimmed)
        mock_is_speech.side_effect = [True, False, True, False, False]

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)

        # Use medium amplitude chunks that pass RMS check
        chunk = create_medium_amplitude_chunk(amplitude=0.1)

        # All chunks have same size, VAD mock determines speech vs silence
        for i in range(5):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, chunk)

        self.manager.process_chunks()

        # Force flush
        flush_time = base_time + timedelta(seconds=1)
        self.manager.process_chunk(speaker_id, flush_time, None)

        self.assertEqual(len(self.saved_chunks), 1)

        # Expected: 3 chunks (speech + silence + speech), trailing 2 trimmed
        expected_length = len(chunk) * 3
        actual_length = len(self.saved_chunks[0]["audio_data"])
        self.assertEqual(actual_length, expected_length)

        # 2 trailing silent chunks should have been trimmed
        self.assertEqual(self.manager.diagnostic_info["total_trailing_silent_chunks_trimmed"], 2)

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_all_speech_no_trimming(self, mock_is_speech):
        """Test that nothing is trimmed when there's no trailing silence."""
        mock_is_speech.return_value = True

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)

        speech_chunk = create_medium_amplitude_chunk(amplitude=0.1)

        for i in range(5):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, speech_chunk)

        self.manager.process_chunks()

        # Force flush
        flush_time = base_time + timedelta(seconds=1)
        self.manager.process_chunk(speaker_id, flush_time, None)

        self.assertEqual(len(self.saved_chunks), 1)
        expected_length = len(speech_chunk) * 5
        actual_length = len(self.saved_chunks[0]["audio_data"])
        self.assertEqual(actual_length, expected_length)

        # No trailing silence to trim
        self.assertEqual(self.manager.diagnostic_info["total_trailing_silent_chunks_trimmed"], 0)


class TestSileroVADIntegration(TestCase):
    """Tests for Silero VAD integration."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )

    def test_silence_detected_with_zero_rms(self):
        """Test that zero RMS is detected as silence without calling VAD."""
        silent_chunk = create_silent_chunk()
        is_silent = self.manager.silence_detected(silent_chunk)
        self.assertTrue(is_silent)
        self.assertEqual(self.manager.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_zero"], 1)

    def test_silence_detected_with_low_rms(self):
        """Test that low RMS audio is detected as silence without calling VAD."""
        low_noise_chunk = create_noise_chunk(noise_level=0.001)
        is_silent = self.manager.silence_detected(low_noise_chunk)
        self.assertTrue(is_silent)
        # Either zero or small RMS
        total_rms_silent = (
            self.manager.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_zero"] +
            self.manager.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_small"]
        )
        self.assertGreater(total_rms_silent, 0)


class TestFlushBehavior(TestCase):
    """Tests for utterance flushing behavior."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=5000,  # Small limit for testing
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_flush_on_buffer_full(self, mock_is_speech):
        """Test that buffer is flushed when size limit is reached."""
        mock_is_speech.return_value = True

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)

        # Create chunks that will exceed buffer limit
        speech_chunk = create_medium_amplitude_chunk(amplitude=0.1, duration_ms=100)
        chunk_size = len(speech_chunk)

        # Add chunks until we exceed limit
        num_chunks = (self.manager.UTTERANCE_SIZE_LIMIT // chunk_size) + 2
        for i in range(num_chunks):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, speech_chunk)

        self.manager.process_chunks()

        # Should have triggered at least one flush due to buffer being full
        self.assertGreater(len(self.saved_chunks), 0)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "buffer_full")

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_flush_on_silence_timeout(self, mock_is_speech):
        """Test that buffer is flushed after silence duration limit."""
        mock_is_speech.side_effect = [True, True, False, False, False]

        # Create a manager with a large buffer limit so we flush on silence, not buffer size
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=1000000,  # Large limit
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()

        # Use medium amplitude chunks that pass RMS check
        chunk = create_medium_amplitude_chunk(amplitude=0.1)

        # Add speech
        manager.add_chunk(speaker_id, base_time, chunk)
        manager.add_chunk(speaker_id, base_time + timedelta(milliseconds=100), chunk)

        # Add silence with timestamps that exceed the silence limit (500ms)
        manager.add_chunk(speaker_id, base_time + timedelta(milliseconds=200), chunk)
        manager.add_chunk(speaker_id, base_time + timedelta(milliseconds=300), chunk)

        # This chunk has timestamp > silence_duration_limit (500ms) after last speech (100ms)
        manager.add_chunk(speaker_id, base_time + timedelta(milliseconds=700), chunk)

        manager.process_chunks()

        # Should have flushed due to silence limit
        self.assertEqual(len(self.saved_chunks), 1)
        self.assertEqual(self.saved_chunks[0]["flush_reason"], "silence_limit")

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_flush_utterances_flushes_all_speakers(self, mock_is_speech):
        """Test that flush_utterances clears all pending utterances."""
        mock_is_speech.return_value = True

        # Create a manager with a larger buffer for this test
        saved_chunks = []
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=100000,  # Large buffer
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )

        base_time = datetime.utcnow()
        speech_chunk = create_medium_amplitude_chunk(amplitude=0.1)

        # Add enough chunks to pass minimum duration threshold (200ms = 20 chunks at 10ms each)
        for i in range(25):
            chunk_time = base_time + timedelta(milliseconds=i * 10)
            manager.add_chunk("speaker_1", chunk_time, speech_chunk)
            manager.add_chunk("speaker_2", chunk_time, speech_chunk)
            manager.add_chunk("speaker_3", chunk_time, speech_chunk)

        manager.process_chunks()
        manager.flush_utterances()

        # All speakers should have been flushed
        self.assertEqual(len(saved_chunks), 3)
        speaker_ids = [chunk["participant_id"] for chunk in saved_chunks]
        self.assertIn("speaker_1", speaker_ids)
        self.assertIn("speaker_2", speaker_ids)
        self.assertIn("speaker_3", speaker_ids)


class TestParticipantNotFound(TestCase):
    """Tests for handling missing participants."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: None,  # Always return None
            sample_rate=16000,
            utterance_size_limit=5000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="webrtc",
        )

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_participant_not_found_logs_and_discards(self, mock_is_speech):
        """Test that chunks are discarded when participant is not found."""
        mock_is_speech.return_value = True

        # Create a manager with a larger buffer for this test
        saved_chunks = []
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: None,  # Always return None
            sample_rate=16000,
            utterance_size_limit=100000,  # Large buffer
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="webrtc",
        )

        speaker_id = "unknown_speaker"
        base_time = datetime.utcnow()
        speech_chunk = create_medium_amplitude_chunk(amplitude=0.1)

        # Add enough chunks to pass minimum duration threshold (200ms = 20 chunks at 10ms each)
        for i in range(25):
            chunk_time = base_time + timedelta(milliseconds=i * 10)
            manager.add_chunk(speaker_id, chunk_time, speech_chunk)
        
        manager.process_chunks()
        manager.flush_utterances()

        # No chunks should be saved
        self.assertEqual(len(saved_chunks), 0)
        self.assertEqual(manager.diagnostic_info["total_audio_chunks_not_sent_because_participant_not_found"], 1)


class TestVADProviders(TestCase):
    """Tests for VAD provider selection and behavior."""

    def test_webrtc_vad_provider_selection(self):
        """Test that WebRTC VAD is used when specified."""
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: None,
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="webrtc",
        )
        self.assertEqual(manager._vad.name, "webrtc")
        self.assertEqual(manager.diagnostic_info["vad_provider"], "webrtc")

    def test_silero_vad_provider_selection(self):
        """Test that Silero VAD is used when specified."""
        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: None,
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="silero",
        )
        self.assertEqual(manager._vad.name, "silero")
        self.assertEqual(manager.diagnostic_info["vad_provider"], "silero")

    def test_env_var_provider_selection(self):
        """Test that VAD_PROVIDER env var controls provider selection."""
        # Test with webrtc
        os.environ["VAD_PROVIDER"] = "webrtc"
        manager1 = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: None,
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )
        self.assertEqual(manager1._vad.name, "webrtc")

        # Test with silero
        os.environ["VAD_PROVIDER"] = "silero"
        manager2 = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: None,
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )
        self.assertEqual(manager2._vad.name, "silero")

        # Clean up
        del os.environ["VAD_PROVIDER"]

    def test_default_provider_is_webrtc(self):
        """Test that default VAD provider is webrtc when env var is not set."""
        # Ensure env var is not set
        os.environ.pop("VAD_PROVIDER", None)

        manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: None,
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
        )
        self.assertEqual(manager._vad.name, "webrtc")


class TestWebRTCVADDirectly(TestCase):
    """Direct tests for WebRTC VAD implementation."""

    def setUp(self):
        self.vad = WebRTCVAD(sample_rate=16000)

    def test_webrtc_vad_processes_valid_chunk(self):
        """Test that WebRTC VAD can process a valid 20ms chunk without errors."""
        # 20ms at 16kHz = 320 samples = 640 bytes
        chunk = create_audio_chunk(duration_ms=20)
        # Just verify it doesn't crash - webrtcvad behavior with test audio varies
        result = self.vad.is_speech(chunk)
        self.assertIsInstance(result, bool)

    def test_large_chunk_returns_true(self):
        """Test that chunks larger than 30ms return True (original behavior)."""
        # Create a chunk larger than 30ms (at 16kHz, 30ms = 480 samples = 960 bytes)
        large_chunk = create_audio_chunk(duration_ms=50)
        result = self.vad.is_speech(large_chunk)
        self.assertTrue(result)

    def test_vad_name(self):
        """Test that VAD name is correct."""
        self.assertEqual(self.vad.name, "webrtc")


class TestSileroVADDirectly(TestCase):
    """Direct tests for Silero VAD implementation."""

    def setUp(self):
        self.vad = SileroVAD(sample_rate=16000)

    def test_silero_vad_processes_valid_chunk(self):
        """Test that Silero VAD can process a valid 32ms chunk without errors."""
        # Silero requires exactly 512 samples (32ms) for 16kHz
        silent_chunk = create_silent_chunk(duration_ms=32)  # 512 samples
        result = self.vad.is_speech(silent_chunk)
        self.assertIsInstance(result, bool)

    def test_vad_name(self):
        """Test that VAD name is correct."""
        self.assertEqual(self.vad.name, "silero")


class TestTrailingSilenceTrimmingWithWebRTC(TestCase):
    """Test trailing silence trimming with WebRTC VAD provider."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="webrtc",
        )

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_trailing_silence_trimmed_webrtc(self, mock_is_speech):
        """Test trailing silence trimming with WebRTC VAD."""
        mock_is_speech.side_effect = [True, True, True, False, False, False]

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)
        chunk = create_medium_amplitude_chunk(amplitude=0.1)

        for i in range(6):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, chunk)

        self.manager.process_chunks()
        flush_time = base_time + timedelta(seconds=1)
        self.manager.process_chunk(speaker_id, flush_time, None)

        self.assertEqual(len(self.saved_chunks), 1)
        expected_length = len(chunk) * 3  # Only 3 speech chunks
        self.assertEqual(len(self.saved_chunks[0]["audio_data"]), expected_length)
        self.assertEqual(self.manager.diagnostic_info["total_trailing_silent_chunks_trimmed"], 3)
        self.assertEqual(self.manager.diagnostic_info["vad_provider"], "webrtc")


class TestTrailingSilenceTrimmingWithSilero(TestCase):
    """Test trailing silence trimming with Silero VAD provider."""

    def setUp(self):
        self.saved_chunks = []
        self.manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=lambda chunk: self.saved_chunks.append(chunk),
            get_participant_callback=lambda speaker_id: {"participant_id": speaker_id, "name": f"Speaker {speaker_id}"},
            sample_rate=16000,
            utterance_size_limit=1000000,
            silence_duration_limit=0.5,
            should_print_diagnostic_info=False,
            vad_provider="silero",
        )

    @patch.object(PerParticipantNonStreamingAudioInputManager, 'is_speech')
    def test_trailing_silence_trimmed_silero(self, mock_is_speech):
        """Test trailing silence trimming with Silero VAD."""
        mock_is_speech.side_effect = [True, True, True, False, False, False]

        speaker_id = "speaker_1"
        base_time = datetime.utcnow()
        chunk_duration = timedelta(milliseconds=100)
        chunk = create_medium_amplitude_chunk(amplitude=0.1)

        for i in range(6):
            self.manager.add_chunk(speaker_id, base_time + chunk_duration * i, chunk)

        self.manager.process_chunks()
        flush_time = base_time + timedelta(seconds=1)
        self.manager.process_chunk(speaker_id, flush_time, None)

        self.assertEqual(len(self.saved_chunks), 1)
        expected_length = len(chunk) * 3  # Only 3 speech chunks
        self.assertEqual(len(self.saved_chunks[0]["audio_data"]), expected_length)
        self.assertEqual(self.manager.diagnostic_info["total_trailing_silent_chunks_trimmed"], 3)
        self.assertEqual(self.manager.diagnostic_info["vad_provider"], "silero")


if __name__ == "__main__":
    unittest.main()
