import logging
import queue
import time

import numpy as np
import webrtcvad

from bots.models import Credentials, TranscriptionProviders
from bots.transcription_providers.deepgram.deepgram_streaming_transcriber import DeepgramStreamingTranscriber

logger = logging.getLogger(__name__)


def calculate_normalized_rms(audio_bytes):
    samples = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(np.square(samples)))
    # Normalize by max possible value for 16-bit audio (32768)
    return rms / 32768


class PerParticipantStreamingAudioInputManager:
    def __init__(self, *, get_participant_callback, sample_rate, transcription_provider, bot):
        self.queue = queue.Queue()

        self.get_participant_callback = get_participant_callback

        self.utterances = {}
        self.sample_rate = sample_rate

        self.last_nonsilent_audio_time = {}

        self.SILENCE_DURATION_LIMIT = 10  # seconds

        self.vad = webrtcvad.Vad()
        self.transcription_provider = transcription_provider
        self.streaming_transcribers = {}
        self.last_nonsilent_audio_time = {}

        self.project = bot.project
        self.bot = bot
        self.deepgram_api_key = self.get_deepgram_api_key()

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

    def create_streaming_transcriber(self, speaker_id, metadata):
        logger.info(f"Creating streaming transcriber for speaker {speaker_id}")
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
                redaction_settings=self.bot.transcription_settings.deepgram_redaction_settings(),
            )
        else:
            raise Exception(f"Unsupported transcription provider: {self.transcription_provider}")

    def find_or_create_streaming_transcriber_for_speaker(self, speaker_id):
        if speaker_id not in self.streaming_transcribers:
            metadata = {"bot_id": self.bot.object_id, **(self.bot.metadata or {}), **self.get_participant_callback(speaker_id)}
            self.streaming_transcribers[speaker_id] = self.create_streaming_transcriber(speaker_id, metadata)
        return self.streaming_transcribers[speaker_id]

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        if not self.deepgram_api_key:
            return

        audio_is_silent = self.silence_detected(chunk_bytes)

        if not audio_is_silent:
            self.last_nonsilent_audio_time[speaker_id] = time.time()

        if audio_is_silent and speaker_id not in self.streaming_transcribers:
            return

        streaming_transcriber = self.find_or_create_streaming_transcriber_for_speaker(speaker_id)
        streaming_transcriber.send(chunk_bytes)

    def monitor_transcription(self):
        speakers_to_remove = []
        streaming_transcriber_keys = list(self.streaming_transcribers.keys())
        for speaker_id in streaming_transcriber_keys:
            streaming_transcriber = self.streaming_transcribers[speaker_id]
            if time.time() - self.last_nonsilent_audio_time[speaker_id] > self.SILENCE_DURATION_LIMIT:
                streaming_transcriber.finish()
                speakers_to_remove.append(speaker_id)
                logger.info(f"Speaker {speaker_id} has been silent for too long, stopping streaming transcriber")

        for speaker_id in speakers_to_remove:
            del self.streaming_transcribers[speaker_id]

        # If Number of streaming transcibers is greater than 4, then stop the oldest one
        if len(self.streaming_transcribers) > 4:
            oldest_transcriber = min(self.streaming_transcribers.values(), key=lambda x: x.last_send_time)
            oldest_transcriber.finish()
            del self.streaming_transcribers[oldest_transcriber.speaker_id]
            logger.info(f"Stopped oldest streaming transcriber for speaker {oldest_transcriber.speaker_id}")
