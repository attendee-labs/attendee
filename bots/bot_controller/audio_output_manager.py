import threading
import time

from bots.utils import mp3_to_pcm

from .text_to_speech import generate_audio_from_text


class AudioOutputManager:
    SAMPLE_RATE = 44100

    def __init__(
        self,
        currently_playing_audio_media_request_finished_callback,
        play_raw_audio_callback,
        sleep_time_between_chunks_seconds,
    ):
        self.currently_playing_audio_media_request = None
        self.currently_playing_audio_media_request_started_at = None
        self.currently_playing_audio_media_request_duration_ms = None
        self.currently_playing_audio_media_request_finished_callback = currently_playing_audio_media_request_finished_callback
        self.play_raw_audio_callback = play_raw_audio_callback
        self.currently_playing_audio_media_request_raw_audio_pcm_bytes = None
        self.audio_thread = None
        self.stop_audio_thread = False
        self.sleep_time_between_chunks_seconds = sleep_time_between_chunks_seconds

    def _play_audio_chunks(self, audio_data, chunk_size):
        for i in range(0, len(audio_data), chunk_size):
            if self.stop_audio_thread:
                break
            chunk = audio_data[i : i + chunk_size]
            self.play_raw_audio_callback(bytes=chunk, sample_rate=self.SAMPLE_RATE)
            time.sleep(self.sleep_time_between_chunks_seconds)

    def _stop_audio_thread(self):
        """Stop the currently running audio thread if it exists."""
        self.stop_audio_thread = True
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join()
        self.stop_audio_thread = False

    def start_playing_audio_media_request(self, audio_media_request):
        # Stop any existing audio playback
        self._stop_audio_thread()

        if audio_media_request.media_blob:
            # Handle raw audio blob case
            self.currently_playing_audio_media_request_raw_audio_pcm_bytes = mp3_to_pcm(audio_media_request.media_blob.blob, sample_rate=self.SAMPLE_RATE)
            self.currently_playing_audio_media_request_duration_ms = audio_media_request.media_blob.duration_ms
        else:
            # Handle text-to-speech case
            audio_blob, duration_ms = generate_audio_from_text(
                text=audio_media_request.text_to_speak,
                settings=audio_media_request.text_to_speech_settings,
                sample_rate=self.SAMPLE_RATE,
                bot=audio_media_request.bot,
            )
            self.currently_playing_audio_media_request_raw_audio_pcm_bytes = audio_blob
            self.currently_playing_audio_media_request_duration_ms = duration_ms

        self.currently_playing_audio_media_request = audio_media_request
        self.currently_playing_audio_media_request_started_at = time.time()

        bytes_per_sample = 2
        # Start audio playback in a new thread
        self.audio_thread = threading.Thread(
            target=self._play_audio_chunks,
            args=(
                self.currently_playing_audio_media_request_raw_audio_pcm_bytes,
                self.SAMPLE_RATE * bytes_per_sample,
            ),
        )
        self.audio_thread.start()

    def currently_playing_audio_media_request_is_finished(self):
        if not self.currently_playing_audio_media_request or not self.currently_playing_audio_media_request_started_at:
            return False
        elapsed_ms = (time.time() - self.currently_playing_audio_media_request_started_at) * 1000
        if elapsed_ms > self.currently_playing_audio_media_request_duration_ms:
            return True
        return False

    def clear_currently_playing_audio_media_request(self):
        self._stop_audio_thread()
        self.currently_playing_audio_media_request = None
        self.currently_playing_audio_media_request_started_at = None

    def monitor_currently_playing_audio_media_request(self):
        if self.currently_playing_audio_media_request_is_finished():
            temp_currently_playing_audio_media_request = self.currently_playing_audio_media_request
            self.clear_currently_playing_audio_media_request()
            self.currently_playing_audio_media_request_finished_callback(temp_currently_playing_audio_media_request)
