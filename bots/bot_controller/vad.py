"""
Voice Activity Detection (VAD) implementations.

This module provides a clean interface for VAD with two implementations:
- WebRTC VAD (default): Fast, lightweight, good for most cases
- Silero VAD: More accurate, especially for noisy environments

Usage:
    from bots.bot_controller.vad import create_vad

    # Uses VAD_PROVIDER env var, defaults to 'webrtc'
    vad = create_vad(sample_rate=16000)

    # Check if audio contains speech
    is_speech = vad.is_speech(audio_bytes)
"""

import logging
import os
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)

# Environment variable to control VAD provider
VAD_PROVIDER_ENV_VAR = "VAD_PROVIDER"
VAD_PROVIDER_WEBRTC = "webrtc"
VAD_PROVIDER_SILERO = "silero"
DEFAULT_VAD_PROVIDER = VAD_PROVIDER_WEBRTC


def calculate_normalized_rms(audio_bytes):
    """Calculate normalized RMS value for audio bytes."""
    if not audio_bytes or len(audio_bytes) < 2:
        return 0.0

    try:
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float64)
        if len(samples) == 0:
            return 0.0

        mean_square = np.mean(np.square(samples))
        if not np.isfinite(mean_square) or mean_square < 0:
            return 0.0

        rms = np.sqrt(mean_square)
        if not np.isfinite(rms):
            return 0.0

        # Normalize by max possible value for 16-bit audio (32768)
        return rms / 32768
    except (ValueError, TypeError, BufferError):
        return 0.0


class BaseVAD(ABC):
    """Abstract base class for Voice Activity Detection."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate

    @abstractmethod
    def is_speech(self, audio_bytes: bytes) -> bool:
        """
        Determine if the audio chunk contains speech.

        Args:
            audio_bytes: Raw audio bytes (16-bit PCM, mono)

        Returns:
            True if speech is detected, False otherwise
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of the VAD implementation."""
        pass


class WebRTCVAD(BaseVAD):
    """WebRTC-based Voice Activity Detection."""

    def __init__(self, sample_rate: int):
        """
        Initialize WebRTC VAD.

        Args:
            sample_rate: Audio sample rate (8000, 16000, 32000, or 48000 Hz)
        """
        super().__init__(sample_rate)
        import webrtcvad

        self._vad = webrtcvad.Vad()

    def is_speech(self, audio_bytes: bytes) -> bool:
        try:
            # The VAD can handle a max of 30 ms of audio. If it is larger than that, just return True
            if len(audio_bytes) > 30 * self.sample_rate // 1000:
                return True
            return self._vad.is_speech(audio_bytes, self.sample_rate)
        except Exception as e:
            logger.exception(f"Error in WebRTC VAD: {e}")
            return True  # Assume speech on error

    @property
    def name(self) -> str:
        return "webrtc"


class SileroVAD(BaseVAD):
    """Silero-based Voice Activity Detection."""

    # Lazy-loaded model (shared across instances)
    _model = None
    # Target sample rate for Silero VAD (only supports 8000 and 16000)
    _TARGET_SAMPLE_RATE = 16000

    def __init__(self, sample_rate: int, threshold: float = 0.65):
        """
        Initialize Silero VAD.

        Args:
            sample_rate: Audio sample rate (any rate, will be resampled to 16kHz)
            threshold: Speech probability threshold (0.0-1.0). Higher = more strict.
                       Default 0.65 is tuned for meeting audio with some background noise.
                       Lower values (0.5) may detect noise as speech, causing utterances
                       to not split properly. Higher values (0.8) may miss quiet speech.
        """
        super().__init__(sample_rate)
        self._threshold = threshold
        self._initialized = False
        # Calculate resampling ratio
        self._resample_ratio = self._TARGET_SAMPLE_RATE / sample_rate if sample_rate != self._TARGET_SAMPLE_RATE else 1.0

    def _ensure_initialized(self):
        """Lazily initialize the Silero model."""
        if self._initialized:
            return

        import torch

        if SileroVAD._model is None:
            # Set torch to use single thread for efficiency
            torch.set_num_threads(1)
            SileroVAD._model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
        self._initialized = True

    def reset_state(self):
        """Reset the internal state of the Silero model.
        
        Important: Silero VAD is stateful and maintains context between calls.
        Call this method when starting to process a new audio stream.
        """
        if SileroVAD._model is not None:
            SileroVAD._model.reset_states()

    def _resample_audio(self, samples: np.ndarray) -> np.ndarray:
        """Resample audio to 16kHz if needed."""
        if self._resample_ratio == 1.0:
            return samples
        # Simple linear interpolation resampling
        target_length = int(len(samples) * self._resample_ratio)
        if target_length == 0:
            return samples
        indices = np.linspace(0, len(samples) - 1, target_length)
        return np.interp(indices, np.arange(len(samples)), samples).astype(np.float32)

    def is_speech(self, audio_bytes: bytes) -> bool:
        try:
            import torch

            self._ensure_initialized()

            # Convert bytes to float tensor for Silero VAD
            # Silero expects float32 tensor with values in [-1, 1]
            samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
            samples = samples / 32768.0  # Normalize to [-1, 1]

            # Resample to 16kHz if needed (Silero only supports 8k/16k)
            samples = self._resample_audio(samples)

            # Silero VAD requires exactly 512 samples for 16kHz
            required_samples = 512

            # If chunk is too short or empty, can't make a reliable decision
            if len(samples) < required_samples:
                # For very short chunks, use RMS-based silence detection instead
                rms = np.sqrt(np.mean(np.square(samples))) if len(samples) > 0 else 0
                return rms > 0.01  # Assume speech if RMS is above threshold

            # If chunk is longer than required, process in fixed-size chunks
            if len(samples) > required_samples:
                # Process in chunks and return True if any chunk has speech
                num_full_chunks = len(samples) // required_samples
                for i in range(num_full_chunks):
                    chunk = samples[i * required_samples : (i + 1) * required_samples]
                    audio_tensor = torch.from_numpy(chunk.copy())
                    speech_prob = SileroVAD._model(audio_tensor, self._TARGET_SAMPLE_RATE).item()
                    if speech_prob >= self._threshold:
                        return True
                return False

            # Exact size chunk
            audio_tensor = torch.from_numpy(samples.copy())

            # Get speech probability from Silero (always use 16kHz since we resampled)
            speech_prob = SileroVAD._model(audio_tensor, self._TARGET_SAMPLE_RATE).item()

            return speech_prob >= self._threshold
        except Exception as e:
            logger.exception(f"Error in Silero VAD: {e}")
            return True  # Assume speech on error

    @property
    def name(self) -> str:
        return "silero"


def get_vad_provider() -> str:
    """Get the configured VAD provider from environment."""
    provider = os.environ.get(VAD_PROVIDER_ENV_VAR, DEFAULT_VAD_PROVIDER).lower()
    if provider not in (VAD_PROVIDER_WEBRTC, VAD_PROVIDER_SILERO):
        logger.warning(f"Unknown VAD provider '{provider}', falling back to '{DEFAULT_VAD_PROVIDER}'")
        return DEFAULT_VAD_PROVIDER
    return provider


def create_vad(sample_rate: int, provider: str = None) -> BaseVAD:
    """
    Create a VAD instance based on configuration.

    Args:
        sample_rate: Audio sample rate in Hz
        provider: VAD provider ('webrtc' or 'silero'). If None, uses VAD_PROVIDER env var.

    Returns:
        A VAD instance implementing the BaseVAD interface
    """
    if provider is None:
        provider = get_vad_provider()

    if provider == VAD_PROVIDER_SILERO:
        logger.info("Using Silero VAD")
        return SileroVAD(sample_rate)
    else:
        logger.info("Using WebRTC VAD")
        return WebRTCVAD(sample_rate)
