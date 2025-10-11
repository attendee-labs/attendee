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
        rms = calculate_normalized_rms(chunk_bytes)

        # Very low RMS is definitely silence
        if rms < 0.0001:
            return True

        # For Google Meet, we need to be more lenient with VAD
        # Try VAD but don't rely on it solely if RMS shows some energy
        try:
            is_speech = self.vad.is_speech(chunk_bytes, self.sample_rate)
            # If RMS shows reasonable energy but VAD says no speech,
            # trust the RMS (Google Meet audio may not match VAD expectations)
            if rms > 0.001:
                # logger.info(f"Audio chunk: RMS={rms:.6f}, VAD={is_speech}")
                return False  # Has energy, send it
            return not is_speech
        except Exception as e:
            logger.warning(f"VAD error: {e}, using RMS only")
            # If VAD fails, use RMS threshold
            return rms < 0.001

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
                raise ValueError("Kyutai transcription is configured but no credentials found. " "Please add Kyutai credentials to the project or configure " "kyutai_server_url in transcription settings.")

            kyutai_credentials = kyutai_credentials_record.get_credentials()
            server_url = kyutai_credentials.get("server_url")
            api_key = kyutai_credentials.get("api_key")

        # Validate that we have a server URL
        if not server_url:
            raise ValueError("Kyutai server_url is required but not configured. " "Please set kyutai_server_url in transcription settings or credentials.")

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
                language=self.bot.transcription_settings.deepgram_language(),
                model=self.bot.transcription_settings.deepgram_model(),
                callback=self.bot.transcription_settings.deepgram_callback(),
                sample_rate=self.sample_rate,
                metadata=metadata_list,
                redaction_settings=(self.bot.transcription_settings.deepgram_redaction_settings()),
            )
        elif self.transcription_provider == TranscriptionProviders.KYUTAI:
            # Create callback that delegates to the utterance handler
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
            raise Exception(f"Unsupported transcription provider: " f"{self.transcription_provider}")

    def find_or_create_streaming_transcriber_for_speaker(self, speaker_id):
        if speaker_id not in self.streaming_transcribers:
            metadata = {"bot_id": self.bot.object_id, **(self.bot.metadata or {}), **self.get_participant_callback(speaker_id)}
            logger.info(f"Creating new transcriber for speaker {speaker_id}")
            self.streaming_transcribers[speaker_id] = self.create_streaming_transcriber(speaker_id, metadata)
            # Initialize the last audio time when creating transcriber
            # This prevents KeyError and immediate shutdown for new speakers
            self.last_nonsilent_audio_time[speaker_id] = time.time()
        return self.streaming_transcribers[speaker_id]

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        # Fast path for Kyutai: Skip silence detection, use built-in semantic VAD
        if self.transcription_provider == TranscriptionProviders.KYUTAI:
            if not self.kyutai_server_url:
                raise ValueError("Kyutai transcription is configured but no server URL found. " "Please configure Kyutai credentials or transcription settings.")

            # Update activity timestamp
            self.last_nonsilent_audio_time[speaker_id] = time.time()

            # Get or create transcriber
            transcriber = self.streaming_transcribers.get(speaker_id)
            if transcriber is None:
                transcriber = self.find_or_create_streaming_transcriber_for_speaker(speaker_id)

            # Send immediately - Kyutai has its own semantic VAD
            transcriber.send(chunk_bytes)
            return

        # Standard path for Deepgram and other providers (with silence detection)
        if self.transcription_provider == TranscriptionProviders.DEEPGRAM:
            if not self.deepgram_api_key:
                raise ValueError("Deepgram transcription is configured but no API key found. " "Please add Deepgram credentials to the project.")

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
            time_since_audio = time.time() - self.last_nonsilent_audio_time[speaker_id]
            if time_since_audio > silence_limit:
                streaming_transcriber.finish()
                speakers_to_remove.append(speaker_id)
                logger.info(f"Speaker {speaker_id} has been silent for too long, " f"stopping streaming transcriber")

        for speaker_id in speakers_to_remove:
            del self.streaming_transcribers[speaker_id]

        # If Number of streaming transcribers is greater than 4,
        # stop the oldest one
        if len(self.streaming_transcribers) > 4:
            oldest_transcriber = min(self.streaming_transcribers.values(), key=lambda x: x.last_send_time)
            oldest_transcriber.finish()
            del self.streaming_transcribers[oldest_transcriber.speaker_id]
            logger.info(f"Stopped oldest streaming transcriber for speaker " f"{oldest_transcriber.speaker_id}")
