import logging
import queue
from datetime import datetime, timedelta

import numpy as np
import webrtcvad

logger = logging.getLogger(__name__)


def calculate_normalized_rms(audio_bytes):
    samples = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(np.square(samples)))
    # Normalize by max possible value for 16-bit audio (32768)
    return rms / 32768


class PerParticipantNonStreamingAudioInputManager:
    def __init__(self, *, save_audio_chunk_callback, get_participant_callback, sample_rate, utterance_size_limit, silence_duration_limit):
        self.queue = queue.Queue()

        self.save_audio_chunk_callback = save_audio_chunk_callback
        self.get_participant_callback = get_participant_callback

        self.utterances = {}
        self.sample_rate = sample_rate

        self.first_nonsilent_audio_time = {}
        self.last_nonsilent_audio_time = {}

        self.UTTERANCE_SIZE_LIMIT = utterance_size_limit
        self.SILENCE_DURATION_LIMIT = silence_duration_limit
        self.vad = webrtcvad.Vad()

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        self.queue.put((speaker_id, chunk_time, chunk_bytes))

    def process_chunks(self):
        while not self.queue.empty():
            speaker_id, chunk_time, chunk_bytes = self.queue.get()
            self.process_chunk(speaker_id, chunk_time, chunk_bytes)

        for speaker_id in list(self.first_nonsilent_audio_time.keys()):
            self.process_chunk(speaker_id, datetime.utcnow(), None)

    # When the meeting ends, we need to flush all utterances. Do this by pretending that we received a chunk of silence at the end of the meeting.
    def flush_utterances(self):
        for speaker_id in list(self.first_nonsilent_audio_time.keys()):
            self.process_chunk(
                speaker_id,
                datetime.utcnow() + timedelta(seconds=self.SILENCE_DURATION_LIMIT + 1),
                None,
            )

    def silence_detected(self, chunk_bytes):
        if calculate_normalized_rms(chunk_bytes) < 0.01:
            return True
        return not self.vad.is_speech(chunk_bytes, self.sample_rate)

    def process_chunk(self, speaker_id, chunk_time, chunk_bytes):
        audio_is_silent = self.silence_detected(chunk_bytes) if chunk_bytes else True

        # Initialize buffer and timing for new speaker
        if speaker_id not in self.utterances or len(self.utterances[speaker_id]) == 0:
            if audio_is_silent:
                return
            self.utterances[speaker_id] = bytearray()
            self.first_nonsilent_audio_time[speaker_id] = chunk_time
            self.last_nonsilent_audio_time[speaker_id] = chunk_time

        # Add new audio data to buffer
        if chunk_bytes:
            self.utterances[speaker_id].extend(chunk_bytes)

        should_flush = False
        reason = None

        # Check buffer size
        if len(self.utterances[speaker_id]) >= self.UTTERANCE_SIZE_LIMIT:
            should_flush = True
            reason = "buffer_full"

        # Check for silence
        if audio_is_silent:
            silence_duration = (chunk_time - self.last_nonsilent_audio_time[speaker_id]).total_seconds()
            if silence_duration >= self.SILENCE_DURATION_LIMIT:
                should_flush = True
                reason = "silence_limit"
        else:
            self.last_nonsilent_audio_time[speaker_id] = chunk_time

            logger.debug(f"Speaker {speaker_id} is speaking")

        # Flush buffer if needed
        if should_flush and len(self.utterances[speaker_id]) > 0:
            participant = self.get_participant_callback(speaker_id)
            if participant:
                self.save_audio_chunk_callback(
                    {
                        **participant,
                        "audio_data": bytes(self.utterances[speaker_id]),
                        "timestamp_ms": int(self.first_nonsilent_audio_time[speaker_id].timestamp() * 1000),
                        "flush_reason": reason,
                        "sample_rate": self.sample_rate,
                    }
                )
            else:
                logger.warning(f"Participant {speaker_id} not found")
            # Clear the buffer
            self.utterances[speaker_id] = bytearray()
            del self.first_nonsilent_audio_time[speaker_id]
            del self.last_nonsilent_audio_time[speaker_id]
