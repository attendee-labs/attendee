import logging
import time

import numpy as np
import webrtcvad

from bots.models import (
    Credentials,
    TranscriptionProviders,
)
from bots.transcription_providers.deepgram.deepgram_streaming_transcriber import (  # noqa: E501
    DeepgramStreamingTranscriber,
)
from bots.transcription_providers.kyutai.kyutai_streaming_transcriber import (  # noqa: E501
    KyutaiStreamingTranscriber,
)
from bots.transcription_providers.utterance_handler import DefaultUtteranceHandler

logger = logging.getLogger(__name__)


def calculate_normalized_rms(audio_bytes):
    if not audio_bytes or len(audio_bytes) < 2:
        return 0.0

    try:
        samples = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(samples) == 0:
            return 0.0

        # Check for any NaN or infinite values in samples
        if not np.isfinite(samples).all():
            return 0.0

        # Calculate mean of squares first
        mean_square = np.mean(np.square(samples.astype(np.float64)))

        # Check if mean_square is valid before sqrt
        if not np.isfinite(mean_square) or mean_square < 0:
            return 0.0

        rms = np.sqrt(mean_square)

        # Handle NaN case (shouldn't happen with valid data, but be safe)
        if not np.isfinite(rms):
            return 0.0

        # Normalize by max possible value for 16-bit audio (32768)
        return rms / 32768
    except (ValueError, TypeError, BufferError, FloatingPointError):
        # If there's any issue with the audio data, treat as silence
        return 0.0


class PerParticipantStreamingAudioInputManager:
    def __init__(self, *, get_participant_callback, sample_rate, transcription_provider, bot):
        self.get_participant_callback = get_participant_callback

        self.utterances = {}
        self.sample_rate = sample_rate

        self.last_nonsilent_audio_time = {}

        self.SILENCE_DURATION_LIMIT = 300  # 5 minutes of inactivity
        # before finishing transcriber

        self.vad = webrtcvad.Vad()
        self.transcription_provider = transcription_provider
        self.streaming_transcribers = {}
        self.last_nonsilent_audio_time = {}

        self.project = bot.project
        self.bot = bot
        self.deepgram_api_key = self.get_deepgram_api_key()
        self.kyutai_server_url, self.kyutai_api_key = self.get_kyutai_server_url()

        # Create utterance handler for providers that need it (like Kyutai)
        self.utterance_handler = DefaultUtteranceHandler(bot=bot, get_participant_callback=get_participant_callback, sample_rate=sample_rate)

    def silence_detected(self, chunk_bytes):
        if calculate_normalized_rms(chunk_bytes) < 0.0025:
            return True
        return not self.vad.is_speech(chunk_bytes, self.sample_rate)

    def get_deepgram_api_key(self):
        deepgram_credentials_record = self.project.credentials.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM).first()
        if not deepgram_credentials_record:
            return None

        deepgram_credentials = deepgram_credentials_record.get_credentials()
        return deepgram_credentials["api_key"]

    def get_kyutai_server_url(self):
        # First try to get from transcription settings (preferred)
        server_url = self.bot.transcription_settings.kyutai_server_url()
        api_key = self.bot.transcription_settings.kyutai_api_key()

        # Fall back to project credentials if not in settings
        if not server_url:
            kyutai_credentials_record = self.project.credentials.filter(credential_type=Credentials.CredentialTypes.KYUTAI).first()
            if not kyutai_credentials_record:
                return None, None

            kyutai_credentials = kyutai_credentials_record.get_credentials()
            server_url = kyutai_credentials.get("server_url", "ws://127.0.0.1:8012/api/asr-streaming")
            api_key = kyutai_credentials.get("api_key", None)

        # Use "public_token" as default if no API key is provided
        # This matches the default behavior of the reference implementation
        if not api_key:
            api_key = "public_token"

        return server_url, api_key

    def create_streaming_transcriber(self, speaker_id, metadata):
        if self.transcription_provider == TranscriptionProviders.DEEPGRAM:
            metadata_list = [f"{key}:{value}" for key, value in metadata.items()] if metadata else None
            return DeepgramStreamingTranscriber(
                deepgram_api_key=self.deepgram_api_key,
                interim_results=True,
                sample_rate=self.sample_rate,
                callback=self.utterance_handler.handle_utterance,
                metadata=metadata_list,
                redaction_settings=(self.bot.transcription_settings.deepgram_redaction_settings()),
            )
        elif self.transcription_provider == TranscriptionProviders.KYUTAI:

            def kyutai_callback(transcript_text, transcriber_metadata=None):
                # Extract duration_ms and timestamp_ms from transcriber metadata
                duration_ms = transcriber_metadata.get("duration_ms", 0) if transcriber_metadata else 0

                # Pass the full transcriber metadata which includes timestamp_ms
                self.utterance_handler.handle_utterance(speaker_id=speaker_id, transcript_text=transcript_text, metadata=transcriber_metadata, duration_ms=duration_ms)

            return KyutaiStreamingTranscriber(
                server_url=self.kyutai_server_url,
                sample_rate=self.sample_rate,
                metadata=metadata,
                interim_results=True,
                model=self.bot.transcription_settings.kyutai_model(),
                api_key=self.kyutai_api_key,
                callback=kyutai_callback,
            )
        else:
            raise Exception(f"Unsupported transcription provider: {self.transcription_provider}")

    def find_or_create_streaming_transcriber_for_speaker(self, speaker_id):
        if speaker_id not in self.streaming_transcribers:
            metadata = {"bot_id": self.bot.object_id, **(self.bot.metadata or {}), **self.get_participant_callback(speaker_id)}
            self.streaming_transcribers[speaker_id] = self.create_streaming_transcriber(speaker_id, metadata)
            # Initialize last audio time for this speaker
            self.last_nonsilent_audio_time[speaker_id] = time.time()
        return self.streaming_transcribers[speaker_id]

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        # Check if we have credentials for the transcription provider
        if self.transcription_provider == TranscriptionProviders.DEEPGRAM:
            if not self.deepgram_api_key:
                logger.warning("No Deepgram API key available")
                return
        elif self.transcription_provider == TranscriptionProviders.KYUTAI:
            if not self.kyutai_server_url:
                logger.warning("No Kyutai server URL available")
                return

        # For Kyutai: Send all audio continuously, let semantic VAD handle it
        # For Deepgram: Use pre-filtering to reduce API costs
        if self.transcription_provider == TranscriptionProviders.KYUTAI:
            # Still detect silence for monitoring purposes, but send all audio
            audio_is_silent = self.silence_detected(chunk_bytes)

            if not audio_is_silent:
                self.last_nonsilent_audio_time[speaker_id] = time.time()

            # Create transcriber if needed and send all audio
            streaming_transcriber = self.find_or_create_streaming_transcriber_for_speaker(speaker_id)
            if streaming_transcriber:
                streaming_transcriber.send(chunk_bytes)
            else:
                logger.warning(f"Failed to create transcriber for speaker {speaker_id}")
        else:
            # Deepgram and other providers: use VAD pre-filtering
            audio_is_silent = self.silence_detected(chunk_bytes)

            if not audio_is_silent:
                self.last_nonsilent_audio_time[speaker_id] = time.time()

            if audio_is_silent and speaker_id not in self.streaming_transcribers:
                return

            streaming_transcriber = self.find_or_create_streaming_transcriber_for_speaker(speaker_id)

            # Only send audio if transcriber was successfully created
            if streaming_transcriber:
                streaming_transcriber.send(chunk_bytes)

    def monitor_transcription(self):
        speakers_to_remove = []
        streaming_transcriber_keys = list(self.streaming_transcribers.keys())
        for speaker_id in streaming_transcriber_keys:
            streaming_transcriber = self.streaming_transcribers[speaker_id]
            silence_limit = self.SILENCE_DURATION_LIMIT

            # Defensive: ensure we have timing data for this speaker
            if speaker_id not in self.last_nonsilent_audio_time:
                # Initialize with current time if missing (shouldn't happen)
                self.last_nonsilent_audio_time[speaker_id] = time.time()
                logger.warning(f"Missing last_nonsilent_audio_time for speaker {speaker_id}, initializing")
                continue

            time_since_audio = time.time() - self.last_nonsilent_audio_time[speaker_id]
            if time_since_audio > silence_limit:
                streaming_transcriber.finish()
                speakers_to_remove.append(speaker_id)
                logger.info(f"Speaker {speaker_id} has been silent for too long, stopping streaming transcriber")

        for speaker_id in speakers_to_remove:
            del self.streaming_transcribers[speaker_id]
            # Also clean up timing data
            if speaker_id in self.last_nonsilent_audio_time:
                del self.last_nonsilent_audio_time[speaker_id]

        # If Number of streaming transcribers is greater than 4,
        # stop the oldest one
        if len(self.streaming_transcribers) > 4:
            oldest_transcriber = min(self.streaming_transcribers.values(), key=lambda x: x.last_send_time)
            oldest_transcriber.finish()
            del self.streaming_transcribers[oldest_transcriber.speaker_id]
            logger.info(f"Stopped oldest streaming transcriber for speaker {oldest_transcriber.speaker_id}")
