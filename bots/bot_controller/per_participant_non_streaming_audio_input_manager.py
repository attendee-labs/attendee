import logging
import queue
import time
from collections import deque
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
    QUEUE_ITEM_TYPE_AUDIO = "audio"
    QUEUE_ITEM_TYPE_SPEECH_START = "speech_start"
    QUEUE_ITEM_TYPE_SPEECH_STOP = "speech_stop"
    SPEECH_START_PRE_ROLL_SECONDS = 0.5
    DEFAULT_SPEECH_STOP_POST_ROLL_SECONDS = 2

    def __init__(
        self,
        *,
        save_audio_chunk_callback,
        get_participant_callback,
        sample_rate,
        utterance_size_limit,
        silence_duration_limit,
        min_speech_duration_limit,
        ignore_long_silence_enabled,
        max_silence_to_append_seconds,
        should_print_diagnostic_info,
        speech_stop_post_roll_seconds=DEFAULT_SPEECH_STOP_POST_ROLL_SECONDS,
    ):
        self.queue = queue.Queue()

        self.save_audio_chunk_callback = save_audio_chunk_callback
        self.get_participant_callback = get_participant_callback

        self.utterances = {}
        self.sample_rate = sample_rate

        self.first_nonsilent_audio_time = {}
        self.last_nonsilent_audio_time = {}
        self.pending_speech_start_time = {}
        self.event_reported_speaking_state = {}
        self.pending_speech_stop_deadline = {}
        self.pending_pre_roll_audio = {}
        self.pending_pre_roll_start_time = {}
        self.recent_audio_chunks = {}
        self.nonsilent_audio_duration_seconds = {}

        self.UTTERANCE_SIZE_LIMIT = utterance_size_limit
        self.SILENCE_DURATION_LIMIT = silence_duration_limit
        self.MIN_SPEECH_DURATION_LIMIT = min_speech_duration_limit
        self.IGNORE_LONG_SILENCE_ENABLED = ignore_long_silence_enabled
        self.MAX_SILENCE_TO_APPEND_SECONDS = max_silence_to_append_seconds
        self.SPEECH_STOP_POST_ROLL_SECONDS = speech_stop_post_roll_seconds
        self.vad = webrtcvad.Vad()

        self.should_print_diagnostic_info = should_print_diagnostic_info
        self.reset_diagnostic_info()

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        self.queue.put((self.QUEUE_ITEM_TYPE_AUDIO, speaker_id, chunk_time, chunk_bytes))
        self.diagnostic_info["total_chunks_added"] += 1

    def add_speech_start_event(self, speaker_id, event_time=None):
        if event_time is None:
            event_time = datetime.utcnow()
        self.queue.put((self.QUEUE_ITEM_TYPE_SPEECH_START, speaker_id, event_time, None))
        self.diagnostic_info["total_speech_start_events_added"] += 1

    def add_speech_stop_event(self, speaker_id, event_time=None):
        if event_time is None:
            event_time = datetime.utcnow()
        self.queue.put((self.QUEUE_ITEM_TYPE_SPEECH_STOP, speaker_id, event_time, None))
        self.diagnostic_info["total_speech_stop_events_added"] += 1

    def reset_diagnostic_info(self):
        self.diagnostic_info = {
            "total_chunks_added": 0,
            "total_speech_start_events_added": 0,
            "total_speech_stop_events_added": 0,
            "total_chunks_with_pre_roll_added": 0,
            "total_chunks_marked_as_silent_due_to_vad": 0,
            "total_chunks_marked_as_silent_due_to_rms_being_small": 0,
            "total_chunks_marked_as_silent_due_to_rms_being_zero": 0,
            "total_chunks_too_large_for_vad": 0,
            "total_chunks_that_caused_vad_error": 0,
            "total_audio_chunks_sent": 0,
            "total_audio_chunks_sent_due_to_speech_stop": 0,
            "total_audio_chunks_not_sent_because_participant_not_found": 0,
            "total_audio_chunks_discarded_because_min_speech_duration_not_reached": 0,
        }
        self.last_diagnostic_info_print_time = time.time()

    def print_diagnostic_info(self):
        if time.time() - self.last_diagnostic_info_print_time >= 30:
            if self.should_print_diagnostic_info:
                logger.info(f"PerParticipantNonStreamingAudioInputManager diagnostic info: {self.diagnostic_info}")
            self.reset_diagnostic_info()

    def process_chunks(self):
        pending_items = []
        while not self.queue.empty():
            pending_items.append(self.queue.get())

        # Keep event/audio ordering stable by timestamp so stop events can close the correct chunk.
        pending_items.sort(key=lambda item: item[2])

        for item_type, speaker_id, item_time, item_payload in pending_items:
            if item_type == self.QUEUE_ITEM_TYPE_AUDIO:
                self.process_chunk(speaker_id, item_time, item_payload)
            elif item_type == self.QUEUE_ITEM_TYPE_SPEECH_START:
                self.process_speech_start(speaker_id, item_time)
            elif item_type == self.QUEUE_ITEM_TYPE_SPEECH_STOP:
                self.process_speech_stop(speaker_id, item_time)

        self.process_pending_speech_stop_deadlines()

        for speaker_id in list(self.first_nonsilent_audio_time.keys()):
            self.process_chunk(speaker_id, datetime.utcnow(), None)

        self.print_diagnostic_info()

    # When the meeting ends, we need to flush all utterances. Do this by pretending that we received a chunk of silence at the end of the meeting.
    def flush_utterances(self):
        for speaker_id in list(self.first_nonsilent_audio_time.keys()):
            self.process_chunk(
                speaker_id,
                datetime.utcnow() + timedelta(seconds=self.SILENCE_DURATION_LIMIT + 1),
                None,
            )

    def is_speech(self, chunk_bytes):
        try:
            # The VAD can handle a max of 30 ms of audio. If it is larger than that, just return True
            if len(chunk_bytes) > 30 * self.sample_rate // 1000:
                self.diagnostic_info["total_chunks_too_large_for_vad"] += 1
                return True
            return self.vad.is_speech(chunk_bytes, self.sample_rate)
        except Exception as e:
            logger.exception("Error in VAD: " + str(e))
            self.diagnostic_info["total_chunks_that_caused_vad_error"] += 1
            return True

    def silence_detected(self, chunk_bytes):
        rms_value = calculate_normalized_rms(chunk_bytes)
        if rms_value == 0:
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_zero"] += 1
            return True
        if rms_value < 0.01:
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_small"] += 1
            return True
        if not self.is_speech(chunk_bytes):
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_vad"] += 1
            return True
        return False

    def process_speech_start(self, speaker_id, event_time):
        resumed_within_stop_window = False
        pending_speech_stop_deadline = self.pending_speech_stop_deadline.get(speaker_id)
        if pending_speech_stop_deadline is not None:
            if event_time < pending_speech_stop_deadline:
                # Treat short pauses as part of the same utterance.
                self.pending_speech_stop_deadline.pop(speaker_id, None)
                resumed_within_stop_window = True
            else:
                self.flush_utterance(speaker_id, reason="speech_stop")

        if self.event_reported_speaking_state.get(speaker_id) is False and not resumed_within_stop_window:
            self.flush_utterance(speaker_id, reason="speech_start")
        self.event_reported_speaking_state[speaker_id] = True

        if speaker_id in self.utterances and len(self.utterances[speaker_id]) > 0:
            self.pending_speech_start_time.pop(speaker_id, None)
            self.pending_pre_roll_audio.pop(speaker_id, None)
            self.pending_pre_roll_start_time.pop(speaker_id, None)
            return

        self.pending_speech_start_time[speaker_id] = event_time
        pre_roll_audio, pre_roll_start_time = self.get_pre_roll_audio(speaker_id, event_time)
        self.pending_pre_roll_audio[speaker_id] = pre_roll_audio
        self.pending_pre_roll_start_time[speaker_id] = pre_roll_start_time

    def process_speech_stop(self, speaker_id, event_time):
        self.event_reported_speaking_state[speaker_id] = False
        self.pending_speech_start_time.pop(speaker_id, None)
        self.pending_pre_roll_audio.pop(speaker_id, None)
        self.pending_pre_roll_start_time.pop(speaker_id, None)
        if speaker_id in self.utterances and len(self.utterances[speaker_id]) > 0:
            self.pending_speech_stop_deadline[speaker_id] = event_time + timedelta(seconds=self.SPEECH_STOP_POST_ROLL_SECONDS)
        else:
            self.pending_speech_stop_deadline.pop(speaker_id, None)

    def process_pending_speech_stop_deadlines(self):
        current_time = datetime.utcnow()
        for speaker_id, deadline in list(self.pending_speech_stop_deadline.items()):
            if current_time >= deadline:
                self.flush_utterance(speaker_id, reason="speech_stop")
                self.pending_speech_stop_deadline.pop(speaker_id, None)

    def update_recent_audio_chunks(self, speaker_id, chunk_time, chunk_bytes):
        if chunk_bytes is None:
            return
        recent_chunks = self.recent_audio_chunks.setdefault(speaker_id, deque())
        recent_chunks.append((chunk_time, chunk_bytes))
        min_time = chunk_time - timedelta(seconds=self.SPEECH_START_PRE_ROLL_SECONDS)
        while recent_chunks and recent_chunks[0][0] < min_time:
            recent_chunks.popleft()

    def get_pre_roll_audio(self, speaker_id, event_time):
        recent_chunks = self.recent_audio_chunks.get(speaker_id)
        if not recent_chunks:
            return b"", None

        min_time = event_time - timedelta(seconds=self.SPEECH_START_PRE_ROLL_SECONDS)
        selected_chunks = [chunk for chunk in recent_chunks if min_time <= chunk[0] <= event_time]
        if not selected_chunks:
            return b"", None

        pre_roll_start_time = selected_chunks[0][0]
        pre_roll_audio = b"".join(chunk_bytes for _, chunk_bytes in selected_chunks)
        return pre_roll_audio, pre_roll_start_time

    def flush_utterance(self, speaker_id, reason):
        if speaker_id not in self.utterances or len(self.utterances[speaker_id]) == 0:
            return

        speech_duration_seconds = self.nonsilent_audio_duration_seconds.get(speaker_id, 0.0)
        if speech_duration_seconds >= self.MIN_SPEECH_DURATION_LIMIT:
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
                self.diagnostic_info["total_audio_chunks_sent"] += 1
                if reason == "speech_stop":
                    self.diagnostic_info["total_audio_chunks_sent_due_to_speech_stop"] += 1
            else:
                logger.warning(f"Participant {speaker_id} not found")
                self.diagnostic_info["total_audio_chunks_not_sent_because_participant_not_found"] += 1
        else:
            self.diagnostic_info["total_audio_chunks_discarded_because_min_speech_duration_not_reached"] += 1
            logger.info(
                "Discarding chunk for speaker %s because speech duration %.2fs is below minimum %.2fs",
                speaker_id,
                speech_duration_seconds,
                self.MIN_SPEECH_DURATION_LIMIT,
            )

        self.utterances[speaker_id] = bytearray()
        del self.first_nonsilent_audio_time[speaker_id]
        del self.last_nonsilent_audio_time[speaker_id]
        self.nonsilent_audio_duration_seconds.pop(speaker_id, None)
        self.pending_speech_start_time.pop(speaker_id, None)
        self.pending_pre_roll_audio.pop(speaker_id, None)
        self.pending_pre_roll_start_time.pop(speaker_id, None)
        self.pending_speech_stop_deadline.pop(speaker_id, None)

    def process_chunk(self, speaker_id, chunk_time, chunk_bytes):
        self.update_recent_audio_chunks(speaker_id, chunk_time, chunk_bytes)

        audio_is_silent = self.silence_detected(chunk_bytes) if chunk_bytes else True
        silence_duration = 0.0

        # Initialize buffer and timing for new speaker
        if speaker_id not in self.utterances or len(self.utterances[speaker_id]) == 0:
            if audio_is_silent:
                return
            self.utterances[speaker_id] = bytearray()
            pending_pre_roll_audio = self.pending_pre_roll_audio.pop(speaker_id, b"")
            pending_pre_roll_start_time = self.pending_pre_roll_start_time.pop(speaker_id, None)
            if pending_pre_roll_audio:
                self.utterances[speaker_id].extend(pending_pre_roll_audio)
                self.diagnostic_info["total_chunks_with_pre_roll_added"] += 1

            first_audio_time = self.pending_speech_start_time.pop(speaker_id, chunk_time)
            if pending_pre_roll_start_time:
                first_audio_time = pending_pre_roll_start_time
            self.first_nonsilent_audio_time[speaker_id] = first_audio_time
            self.last_nonsilent_audio_time[speaker_id] = chunk_time
            self.nonsilent_audio_duration_seconds[speaker_id] = 0.0

        # Add speech audio data, and only short silence (for better timing continuity).
        if chunk_bytes and not audio_is_silent:
            self.utterances[speaker_id].extend(chunk_bytes)
            self.nonsilent_audio_duration_seconds[speaker_id] += len(chunk_bytes) / (self.sample_rate * 2)
        elif chunk_bytes and audio_is_silent:
            silence_duration = (chunk_time - self.last_nonsilent_audio_time[speaker_id]).total_seconds()
            if not self.IGNORE_LONG_SILENCE_ENABLED or silence_duration <= self.MAX_SILENCE_TO_APPEND_SECONDS:
                self.utterances[speaker_id].extend(chunk_bytes)

        should_flush = False
        reason = None

        pending_speech_stop_deadline = self.pending_speech_stop_deadline.get(speaker_id)
        if pending_speech_stop_deadline is not None:
            if chunk_time >= pending_speech_stop_deadline:
                should_flush = True
                reason = "speech_stop"
        else:
            # Check buffer size
            if len(self.utterances[speaker_id]) >= self.UTTERANCE_SIZE_LIMIT:
                should_flush = True
                reason = "buffer_full"

            # Check for silence
            if audio_is_silent:
                if silence_duration == 0.0:
                    silence_duration = (chunk_time - self.last_nonsilent_audio_time[speaker_id]).total_seconds()
                if silence_duration >= self.SILENCE_DURATION_LIMIT:
                    should_flush = True
                    reason = "silence_limit"
            else:
                self.last_nonsilent_audio_time[speaker_id] = chunk_time
                logger.debug(f"Speaker {speaker_id} is speaking")

        # Flush buffer if needed
        if should_flush and len(self.utterances[speaker_id]) > 0:
            self.flush_utterance(speaker_id, reason=reason)
