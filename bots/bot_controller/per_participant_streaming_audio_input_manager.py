import logging
import os
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
from bots.transcription_providers.openai.openai_streaming_transcriber import (  # noqa: E501
    OpenAIStreamingTranscriber,
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

        # Set silence duration limit based on provider
        # Deepgram has tight rate limits on concurrent streams, so we're more aggressive
        # Kyutai and other providers can handle longer inactive connections
        if transcription_provider == TranscriptionProviders.DEEPGRAM:
            self.SILENCE_DURATION_LIMIT = 10  # seconds
        else:
            self.SILENCE_DURATION_LIMIT = 300  # 5 minutes of inactivity

        self.vad = webrtcvad.Vad()
        self.transcription_provider = transcription_provider
        self.streaming_transcribers = {}
        self.last_nonsilent_audio_time = {}
        self.streaming_noise_floor_by_speaker = {}

        self.project = bot.project
        self.bot = bot
        self.deepgram_api_key = self.get_deepgram_api_key()
        self.kyutai_server_url, self.kyutai_api_key = self.get_kyutai_server_url_and_api_key()
        self.openai_api_key = self.get_openai_api_key()
        self.streaming_silence_min_normalized_rms = self.get_streaming_silence_min_normalized_rms()
        self.streaming_silence_max_normalized_rms = self.get_streaming_silence_max_normalized_rms()
        self.streaming_silence_noise_multiplier = self.get_streaming_silence_noise_multiplier()
        self.streaming_silence_margin = self.get_streaming_silence_margin()
        self.streaming_silence_ema_alpha = self.get_streaming_silence_ema_alpha()

        # Create utterance handler for providers that need it (like Kyutai)
        self.utterance_handler = DefaultUtteranceHandler(bot=bot, get_participant_callback=get_participant_callback, sample_rate=sample_rate)

    def silence_detected(self, chunk_bytes):
        if calculate_normalized_rms(chunk_bytes) < 0.0025:
            return True
        return not self.vad.is_speech(chunk_bytes, self.sample_rate)

    def get_streaming_silence_min_normalized_rms(self):
        default_threshold = 0.006
        configured_threshold = os.getenv(
            "STREAMING_TRANSCRIPTION_SILENCE_MIN_NORMALIZED_RMS",
            os.getenv("OPENAI_REALTIME_SILENCE_MIN_NORMALIZED_RMS", str(default_threshold)),
        )
        try:
            threshold = float(configured_threshold)
        except (TypeError, ValueError):
            return default_threshold
        return max(0.0, min(threshold, 1.0))

    def get_streaming_silence_max_normalized_rms(self):
        default_threshold = 0.05
        configured_threshold = os.getenv("STREAMING_TRANSCRIPTION_SILENCE_MAX_NORMALIZED_RMS", str(default_threshold))
        try:
            threshold = float(configured_threshold)
        except (TypeError, ValueError):
            return default_threshold
        return max(self.streaming_silence_min_normalized_rms, min(threshold, 1.0))

    def get_streaming_silence_noise_multiplier(self):
        default_multiplier = 2.2
        configured_multiplier = os.getenv("STREAMING_TRANSCRIPTION_SILENCE_NOISE_MULTIPLIER", str(default_multiplier))
        try:
            multiplier = float(configured_multiplier)
        except (TypeError, ValueError):
            return default_multiplier
        return max(1.0, min(multiplier, 10.0))

    def get_streaming_silence_margin(self):
        default_margin = 0.0015
        configured_margin = os.getenv("STREAMING_TRANSCRIPTION_SILENCE_MARGIN", str(default_margin))
        try:
            margin = float(configured_margin)
        except (TypeError, ValueError):
            return default_margin
        return max(0.0, min(margin, 1.0))

    def get_streaming_silence_ema_alpha(self):
        default_alpha = 0.08
        configured_alpha = os.getenv("STREAMING_TRANSCRIPTION_SILENCE_EMA_ALPHA", str(default_alpha))
        try:
            alpha = float(configured_alpha)
        except (TypeError, ValueError):
            return default_alpha
        return max(0.01, min(alpha, 1.0))

    def get_streaming_silence_threshold_for_speaker(self, speaker_id):
        noise_floor = self.streaming_noise_floor_by_speaker.get(speaker_id, 0.0)
        adaptive_threshold = (noise_floor * self.streaming_silence_noise_multiplier) + self.streaming_silence_margin
        return max(
            self.streaming_silence_min_normalized_rms,
            min(adaptive_threshold, self.streaming_silence_max_normalized_rms),
        )

    def update_streaming_noise_floor(self, speaker_id, normalized_rms):
        previous_noise_floor = self.streaming_noise_floor_by_speaker.get(speaker_id)
        if previous_noise_floor is None:
            self.streaming_noise_floor_by_speaker[speaker_id] = normalized_rms
            return

        alpha = self.streaming_silence_ema_alpha
        self.streaming_noise_floor_by_speaker[speaker_id] = (1 - alpha) * previous_noise_floor + alpha * normalized_rms

    def streaming_silence_detected(self, speaker_id, chunk_bytes):
        normalized_rms = calculate_normalized_rms(chunk_bytes)
        dynamic_threshold = self.get_streaming_silence_threshold_for_speaker(speaker_id)

        if normalized_rms < dynamic_threshold:
            self.update_streaming_noise_floor(speaker_id, normalized_rms)
            return True

        is_speech = self.vad.is_speech(chunk_bytes, self.sample_rate)
        if not is_speech:
            self.update_streaming_noise_floor(speaker_id, normalized_rms)
            return True
        return False

    def get_deepgram_api_key(self):
        deepgram_credentials_record = self.project.credentials.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM).first()
        if not deepgram_credentials_record:
            return None

        deepgram_credentials = deepgram_credentials_record.get_credentials()
        return deepgram_credentials["api_key"]

    def get_kyutai_server_url_and_api_key(self):
        kyutai_credentials_record = self.project.credentials.filter(credential_type=Credentials.CredentialTypes.KYUTAI).first()
        if not kyutai_credentials_record:
            return None, None

        kyutai_credentials = kyutai_credentials_record.get_credentials()
        if not kyutai_credentials:
            return None, None

        api_key = kyutai_credentials.get("api_key", None) or "public_token"

        # Use server_url from transcription settings if available, otherwise use the one from project credentials
        server_url = self.bot.transcription_settings.kyutai_server_url() or kyutai_credentials.get("server_url", "ws://127.0.0.1:8012/api/asr-streaming")

        return server_url, api_key

    def get_openai_api_key(self):
        openai_credentials_record = self.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
        if not openai_credentials_record:
            return None

        openai_credentials = openai_credentials_record.get_credentials()
        if not openai_credentials:
            return None

        return openai_credentials.get("api_key")

    def create_streaming_transcriber(self, speaker_id, metadata):
        if self.transcription_provider == TranscriptionProviders.DEEPGRAM:
            metadata_list = [f"{key}:{value}" for key, value in metadata.items()] if metadata else None
            return DeepgramStreamingTranscriber(
                deepgram_api_key=self.deepgram_api_key,
                interim_results=True,
                language=self.bot.transcription_settings.deepgram_language(),
                sample_rate=self.sample_rate,
                model=self.bot.transcription_settings.deepgram_model(),
                callback=self.bot.transcription_settings.deepgram_callback(),
                metadata=metadata_list,
                redaction_settings=self.bot.transcription_settings.deepgram_redaction_settings(),
                replace_settings=self.bot.transcription_settings.deepgram_replace_settings(),
            )
        elif self.transcription_provider == TranscriptionProviders.KYUTAI:

            def kyutai_save_utterance_callback(transcript_text, transcriber_metadata=None):
                # Extract duration_ms and timestamp_ms from transcriber metadata
                duration_ms = transcriber_metadata.get("duration_ms", 0) if transcriber_metadata else 0

                # Pass the full transcriber metadata which includes timestamp_ms
                self.utterance_handler.handle_utterance(speaker_id=speaker_id, transcript_text=transcript_text, metadata=transcriber_metadata, duration_ms=duration_ms)

            return KyutaiStreamingTranscriber(
                server_url=self.kyutai_server_url,
                sample_rate=self.sample_rate,
                metadata=metadata,
                interim_results=True,
                api_key=self.kyutai_api_key,
                save_utterance_callback=kyutai_save_utterance_callback,
            )
        elif self.transcription_provider == TranscriptionProviders.OPENAI:

            def openai_save_utterance_callback(transcript_text, transcriber_metadata=None):
                duration_ms = transcriber_metadata.get("duration_ms", 0) if transcriber_metadata else 0
                self.utterance_handler.handle_utterance(
                    speaker_id=speaker_id,
                    transcript_text=transcript_text,
                    metadata=transcriber_metadata,
                    duration_ms=duration_ms,
                )

            return OpenAIStreamingTranscriber(
                openai_api_key=self.openai_api_key,
                connection_model=self.bot.transcription_settings.openai_realtime_connection_model(),
                transcription_model=self.bot.transcription_settings.openai_realtime_transcription_model(),
                sample_rate=self.sample_rate,
                metadata=metadata,
                language=self.bot.transcription_settings.openai_transcription_language(),
                prompt=self.bot.transcription_settings.openai_transcription_prompt(),
                save_utterance_callback=openai_save_utterance_callback,
            )
        else:
            raise Exception(f"Unsupported transcription provider: {self.transcription_provider}")

    def find_or_create_streaming_transcriber_for_speaker(self, speaker_id):
        # If transcriber exists, return it
        if speaker_id in self.streaming_transcribers:
            return self.streaming_transcribers[speaker_id]

        # Create new transcriber
        participant_info = self.get_participant_callback(speaker_id)
        if participant_info is None:
            # Audio arrived before participant join was captured - skip creating transcriber for now
            return None
        metadata = {"bot_id": self.bot.object_id, **(self.bot.metadata or {}), **participant_info}
        participant_name = metadata.get("participant_full_name", speaker_id)

        logger.info(f"Creating streaming transcriber for speaker {speaker_id} ({participant_name})")
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
        elif self.transcription_provider == TranscriptionProviders.OPENAI:
            if not self.openai_api_key:
                logger.warning("No OpenAI API key available")
                return

        audio_is_silent = self.streaming_silence_detected(speaker_id, chunk_bytes)
        if audio_is_silent:
            return

        try:
            self.last_nonsilent_audio_time[speaker_id] = time.time()
            streaming_transcriber = self.find_or_create_streaming_transcriber_for_speaker(speaker_id)
            if streaming_transcriber:
                streaming_transcriber.send(chunk_bytes)
        except Exception as e:
            participant_info = self.get_participant_callback(speaker_id)
            participant_name = participant_info.get("participant_full_name", speaker_id) if participant_info else speaker_id
            logger.info(f"Recreating transcriber for speaker {speaker_id} ({participant_name}) after connection failure: {e}")
            if speaker_id in self.streaming_transcribers:
                del self.streaming_transcribers[speaker_id]

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
            if speaker_id in self.streaming_noise_floor_by_speaker:
                del self.streaming_noise_floor_by_speaker[speaker_id]

        # If Number of streaming transcribers is greater than 4,
        # stop the oldest one
        if len(self.streaming_transcribers) > 4:
            # Find speaker_id and transcriber with oldest last_send_time
            oldest_speaker_id, oldest_transcriber = min(self.streaming_transcribers.items(), key=lambda item: item[1].last_send_time)
            oldest_transcriber.finish()
            del self.streaming_transcribers[oldest_speaker_id]
            logger.info(f"Stopped oldest streaming transcriber for speaker {oldest_speaker_id}")
