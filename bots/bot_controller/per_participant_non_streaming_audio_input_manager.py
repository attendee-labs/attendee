import logging
import queue
import time
from datetime import datetime, timedelta

from bots.bot_controller.vad import calculate_normalized_rms, create_vad

logger = logging.getLogger(__name__)


class PerParticipantNonStreamingAudioInputManager:
    def __init__(self, *, save_audio_chunk_callback, get_participant_callback, sample_rate, utterance_size_limit, silence_duration_limit, should_print_diagnostic_info, vad_provider=None):
        self.queue = queue.Queue()

        self.save_audio_chunk_callback = save_audio_chunk_callback
        self.get_participant_callback = get_participant_callback

        # Store chunks with their silence status for trailing silence trimming
        self.utterance_chunks = {}  # speaker_id -> list of (chunk_bytes, is_silent)
        self.sample_rate = sample_rate

        self.first_nonsilent_audio_time = {}
        self.last_nonsilent_audio_time = {}

        self.UTTERANCE_SIZE_LIMIT = utterance_size_limit
        self.SILENCE_DURATION_LIMIT = silence_duration_limit
        
        # Minimum speech ratio to save an utterance (discard if below this threshold)
        # e.g., 0.15 means at least 15% of chunks must be speech
        self.MIN_SPEECH_RATIO = 0.15
        
        # Minimum duration in ms to save an utterance (filters clicks/taps)
        self.MIN_DURATION_MS = 200

        # Initialize VAD (uses VAD_PROVIDER env var if vad_provider not specified)
        self._vad = create_vad(sample_rate, provider=vad_provider)

        self.should_print_diagnostic_info = should_print_diagnostic_info
        self.reset_diagnostic_info()

    def add_chunk(self, speaker_id, chunk_time, chunk_bytes):
        self.queue.put((speaker_id, chunk_time, chunk_bytes))
        self.diagnostic_info["total_chunks_added"] += 1

    def reset_diagnostic_info(self):
        self.diagnostic_info = {
            "total_chunks_added": 0,
            "total_chunks_marked_as_silent_due_to_vad": 0,
            "total_chunks_marked_as_silent_due_to_rms_being_zero": 0,
            "total_chunks_marked_as_silent_due_to_rms_being_small": 0,
            "total_audio_chunks_sent": 0,
            "total_audio_chunks_not_sent_because_participant_not_found": 0,
            "total_audio_chunks_discarded_low_speech_ratio": 0,
            "total_audio_chunks_discarded_too_short": 0,
            "total_trailing_silent_chunks_trimmed": 0,
            "vad_provider": self._vad.name,
        }
        self.last_diagnostic_info_print_time = time.time()

    def print_diagnostic_info(self):
        if time.time() - self.last_diagnostic_info_print_time >= 30:
            if self.should_print_diagnostic_info:
                logger.info(f"PerParticipantNonStreamingAudioInputManager diagnostic info: {self.diagnostic_info}")
            self.reset_diagnostic_info()

    def process_chunks(self):
        while not self.queue.empty():
            speaker_id, chunk_time, chunk_bytes = self.queue.get()
            self.process_chunk(speaker_id, chunk_time, chunk_bytes)

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
        """Detect speech using the configured VAD provider."""
        return self._vad.is_speech(chunk_bytes)

    def silence_detected(self, chunk_bytes):
        rms_value = calculate_normalized_rms(chunk_bytes)
        if rms_value == 0:
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_zero"] += 1
            return True
        if rms_value < 0.005:
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_rms_being_small"] += 1
            return True
        
        # VAD for actual speech detection
        if not self.is_speech(chunk_bytes):
            self.diagnostic_info["total_chunks_marked_as_silent_due_to_vad"] += 1
            return True
        return False

    def process_chunk(self, speaker_id, chunk_time, chunk_bytes):
        audio_is_silent = self.silence_detected(chunk_bytes) if chunk_bytes else True

        # Initialize buffer and timing for new speaker
        if speaker_id not in self.utterance_chunks or len(self.utterance_chunks[speaker_id]) == 0:
            if audio_is_silent:
                return
            self.utterance_chunks[speaker_id] = []
            self.first_nonsilent_audio_time[speaker_id] = chunk_time
            self.last_nonsilent_audio_time[speaker_id] = chunk_time

        # Add new audio data to buffer with silence status
        if chunk_bytes:
            self.utterance_chunks[speaker_id].append((chunk_bytes, audio_is_silent))

        should_flush = False
        reason = None

        # Calculate total buffer size
        total_buffer_size = sum(len(chunk) for chunk, _ in self.utterance_chunks[speaker_id])

        # Check buffer size
        if total_buffer_size >= self.UTTERANCE_SIZE_LIMIT:
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
        if should_flush and len(self.utterance_chunks[speaker_id]) > 0:
            chunks = self.utterance_chunks[speaker_id]
            
            # Find the last non-silent chunk (for trimming calculation)
            last_non_silent_idx = len(chunks) - 1
            while last_non_silent_idx >= 0 and chunks[last_non_silent_idx][1]:  # [1] is is_silent
                last_non_silent_idx -= 1
            
            # Calculate speech ratio on TRIMMED utterance (excluding trailing silence)
            trimmed_chunk_count = last_non_silent_idx + 1
            if trimmed_chunk_count <= 0:
                # All chunks are silent, discard
                self.utterance_chunks[speaker_id] = []
                del self.first_nonsilent_audio_time[speaker_id]
                del self.last_nonsilent_audio_time[speaker_id]
                return
            
            speech_chunks = sum(1 for i in range(trimmed_chunk_count) if not chunks[i][1])
            speech_ratio = speech_chunks / trimmed_chunk_count
            
            # Calculate duration on trimmed utterance
            trimmed_bytes = sum(len(chunks[i][0]) for i in range(trimmed_chunk_count))
            duration_ms = (trimmed_bytes / 2) / self.sample_rate * 1000
            
            if speech_ratio < self.MIN_SPEECH_RATIO:
                # Discard utterance with too little speech (likely noise)
                logger.info(
                    f"Discarding low-speech utterance for speaker {speaker_id}: "
                    f"speech ratio {speech_ratio:.1%} ({speech_chunks}/{trimmed_chunk_count} chunks after trim), "
                    f"{trimmed_bytes / 1024:.1f}KB / {duration_ms:.0f}ms"
                )
                self.diagnostic_info["total_audio_chunks_discarded_low_speech_ratio"] += 1
                self.utterance_chunks[speaker_id] = []
                del self.first_nonsilent_audio_time[speaker_id]
                del self.last_nonsilent_audio_time[speaker_id]
                return
            
            if duration_ms < self.MIN_DURATION_MS:
                # Discard utterance that's too short (likely click/tap)
                logger.info(
                    f"Discarding short utterance for speaker {speaker_id}: "
                    f"{duration_ms:.0f}ms < {self.MIN_DURATION_MS}ms minimum, "
                    f"speech ratio {speech_ratio:.1%}"
                )
                self.diagnostic_info["total_audio_chunks_discarded_too_short"] += 1
                self.utterance_chunks[speaker_id] = []
                del self.first_nonsilent_audio_time[speaker_id]
                del self.last_nonsilent_audio_time[speaker_id]
                return
            
            participant = self.get_participant_callback(speaker_id)
            if participant:
                # Trim trailing silence before sending
                trimmed_audio = self._trim_trailing_silence(speaker_id)
                self.save_audio_chunk_callback(
                    {
                        **participant,
                        "audio_data": trimmed_audio,
                        "timestamp_ms": int(self.first_nonsilent_audio_time[speaker_id].timestamp() * 1000),
                        "flush_reason": reason,
                        "sample_rate": self.sample_rate,
                    }
                )
                self.diagnostic_info["total_audio_chunks_sent"] += 1
            else:
                logger.warning(f"Participant {speaker_id} not found")
                self.diagnostic_info["total_audio_chunks_not_sent_because_participant_not_found"] += 1
            # Clear the buffer
            self.utterance_chunks[speaker_id] = []
            del self.first_nonsilent_audio_time[speaker_id]
            del self.last_nonsilent_audio_time[speaker_id]

    def _trim_trailing_silence(self, speaker_id):
        """
        Trim trailing silent chunks from the utterance.

        This removes consecutive silent chunks from the end of the buffer,
        converting [WORDS][SILENCE][WORDS][SILENCE][SILENCE][SILENCE]
        to [WORDS][SILENCE][WORDS]
        """
        chunks = self.utterance_chunks[speaker_id]
        if not chunks:
            return bytes()

        # Find the last non-silent chunk
        last_non_silent_idx = len(chunks) - 1
        while last_non_silent_idx >= 0 and chunks[last_non_silent_idx][1]:  # [1] is is_silent
            last_non_silent_idx -= 1

        # Count trimmed chunks for diagnostics
        trimmed_count = len(chunks) - 1 - last_non_silent_idx
        total_chunks = len(chunks)
        
        # Calculate bytes before and after trimming
        original_bytes = sum(len(chunk) for chunk, _ in chunks)
        trimmed_bytes = sum(len(chunks[i][0]) for i in range(last_non_silent_idx + 1)) if last_non_silent_idx >= 0 else 0
        bytes_trimmed = original_bytes - trimmed_bytes
        
        if trimmed_count > 0:
            self.diagnostic_info["total_trailing_silent_chunks_trimmed"] += trimmed_count
            # Calculate duration trimmed (2 bytes per sample for 16-bit audio)
            duration_trimmed_ms = (bytes_trimmed / 2) / self.sample_rate * 1000
            duration_original_ms = (original_bytes / 2) / self.sample_rate * 1000
            logger.info(
                f"Trimmed trailing silence for speaker {speaker_id}: "
                f"{trimmed_count}/{total_chunks} chunks removed, "
                f"{bytes_trimmed / 1024:.1f}KB / {duration_trimmed_ms:.0f}ms trimmed "
                f"(original: {original_bytes / 1024:.1f}KB / {duration_original_ms:.0f}ms)"
            )

        # Concatenate all chunks up to and including the last non-silent chunk
        result = bytearray()
        for i in range(last_non_silent_idx + 1):
            result.extend(chunks[i][0])  # [0] is chunk_bytes

        return bytes(result)
