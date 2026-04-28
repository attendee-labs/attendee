import logging
import os
import subprocess
import uuid

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    def __init__(self, file_location, recording_dimensions, audio_only, audio_sink_name=None):
        self.file_location = file_location
        self.ffmpeg_proc = None
        # Screen will have buffer, we will crop to the recording dimensions
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)
        self.recording_dimensions = recording_dimensions
        self.audio_only = audio_only
        self.paused = False
        self.xterm_proc = None
        self.audio_sink_name = audio_sink_name or f"attendee_bot_audio_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.audio_source_name = f"{self.audio_sink_name}.monitor"
        self.audio_sink_module_id = None
        self.isolated_audio_setup_attempted = False
        self.isolated_audio_setup_succeeded = False

    def setup_isolated_audio_sink(self):
        if self.isolated_audio_setup_attempted:
            return self.isolated_audio_setup_succeeded

        self.isolated_audio_setup_attempted = True

        try:
            # Keep each web bot's Chrome audio on its own sink so simultaneous meetings do not mix in the final recording.
            result = subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={self.audio_sink_name}",
                    "rate=48000",
                    "channels=2",
                    f"sink_properties=device.description={self.audio_sink_name}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.audio_sink_module_id = result.stdout.strip() or None
            self.isolated_audio_setup_succeeded = True
            logger.info(f"Created isolated PulseAudio sink {self.audio_sink_name} with module id {self.audio_sink_module_id}")
        except Exception as e:
            logger.warning(f"Could not create isolated PulseAudio sink {self.audio_sink_name}; falling back to default audio device: {e}")
            self.isolated_audio_setup_succeeded = False

        return self.isolated_audio_setup_succeeded

    def browser_audio_environment(self):
        if not self.setup_isolated_audio_sink():
            return {}

        return {"PULSE_SINK": self.audio_sink_name}

    def ffmpeg_audio_input_args(self):
        if self.setup_isolated_audio_sink():
            return ["-f", "pulse", "-i", self.audio_source_name]

        return ["-f", "alsa", "-i", "default"]

    def audio_sink_for_mute(self):
        if self.isolated_audio_setup_succeeded:
            return self.audio_sink_name

        return "@DEFAULT_SINK@"

    def teardown_isolated_audio_sink(self):
        if not self.audio_sink_module_id:
            self.isolated_audio_setup_succeeded = False
            return

        try:
            subprocess.run(["pactl", "unload-module", self.audio_sink_module_id], check=True, capture_output=True, text=True)
            logger.info(f"Unloaded isolated PulseAudio sink {self.audio_sink_name} with module id {self.audio_sink_module_id}")
        except Exception as e:
            logger.warning(f"Failed to unload isolated PulseAudio sink {self.audio_sink_name}: {e}")
        finally:
            self.audio_sink_module_id = None
            self.isolated_audio_setup_succeeded = False

    def start_recording(self, display_var):
        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")

        if self.audio_only:
            # FFmpeg command for audio-only recording to MP3
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output file without asking
                "-thread_queue_size",
                "4096",
                *self.ffmpeg_audio_input_args(),
                "-c:a",
                "libmp3lame",  # MP3 codec
                "-b:a",
                "192k",  # Audio bitrate (192 kbps for good quality)
                "-ar",
                "44100",  # Sample rate
                "-ac",
                "1",  # Mono
                self.file_location,
            ]
        else:
            ffmpeg_cmd = ["ffmpeg", "-y", "-thread_queue_size", "256", "-framerate", "30", "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var, "-thread_queue_size", "4096", *self.ffmpeg_audio_input_args(), "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-strict", "experimental", "-b:a", "128k", self.file_location]

        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    # Pauses by muting the audio and showing a black xterm covering the entire screen
    def pause_recording(self):
        if self.paused:
            return True  # Already paused, consider this success

        try:
            sw, sh = self.screen_dimensions

            x, y = 0, 0

            self.xterm_proc = subprocess.Popen(["xterm", "-bg", "black", "-fg", "black", "-geometry", f"{sw}x{sh}+{x}+{y}", "-xrm", "*borderWidth:0", "-xrm", "*scrollBar:false"])

            subprocess.run(["pactl", "set-sink-mute", self.audio_sink_for_mute(), "1"], check=True)
            self.paused = True
            return True
        except Exception as e:
            logger.error(f"Failed to pause recording: {e}")
            return False

    # Resumes by unmuting the audio and killing the xterm proc
    def resume_recording(self):
        if not self.paused:
            return True

        try:
            self.xterm_proc.terminate()
            self.xterm_proc.wait()
            self.xterm_proc = None
            subprocess.run(["pactl", "set-sink-mute", self.audio_sink_for_mute(), "0"], check=True)
            self.paused = False
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording: {e}")
            return False

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
        self.ffmpeg_proc.terminate()
        self.ffmpeg_proc.wait()
        self.ffmpeg_proc = None
        logger.info(f"Stopped screen and audio recorder for display with dimensions {self.screen_dimensions} and file location {self.file_location}")

    def get_seekable_path(self, path):
        """
        Transform a file path to include '.seekable' before the extension.
        Example: /tmp/file.webm -> /tmp/file.seekable.webm
        """
        base, ext = os.path.splitext(path)
        return f"{base}.seekable{ext}"

    def cleanup(self):
        try:
            input_path = self.file_location

            # If no input path at all, then we aren't trying to generate a file at all
            if input_path is None:
                return

            # Check if input file exists
            if not os.path.exists(input_path):
                logger.info(f"Input file does not exist at {input_path}, creating empty file")
                with open(input_path, "wb"):
                    pass  # Create empty file
                return

            # if audio only, we don't need to make it seekable
            if self.audio_only:
                return

            # if input file is greater than 3 GB, we will skip seekability
            if os.path.getsize(input_path) > 3 * 1024 * 1024 * 1024:
                logger.info("Input file is greater than 3 GB, skipping seekability")
                return

            output_path = self.get_seekable_path(self.file_location)
            # the file is seekable, so we don't need to make it seekable
            try:
                self.make_file_seekable(input_path, output_path)
            except Exception as e:
                logger.error(f"Failed to make file seekable: {e}")
                return
        finally:
            self.teardown_isolated_audio_sink()

    def make_file_seekable(self, input_path, tempfile_path):
        """Use ffmpeg to move the moov atom to the beginning of the file."""
        logger.info(f"Making file seekable: {input_path} -> {tempfile_path}")
        # log how many bytes are in the file
        logger.info(f"File size: {os.path.getsize(input_path)} bytes")
        command = [
            "ffmpeg",
            "-i",
            str(input_path),  # Input file
            "-c",
            "copy",  # Copy streams without re-encoding
            "-avoid_negative_ts",
            "make_zero",  # Optional: Helps ensure timestamps start at or after 0
            "-movflags",
            "+faststart",  # Optimize for web playback
            "-y",  # Overwrite output file without asking
            str(tempfile_path),  # Output file
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to make file seekable: {result.stderr}")

        # Replace the original file with the seekable version
        try:
            os.replace(str(tempfile_path), str(input_path))
            logger.info(f"Replaced original file with seekable version: {input_path}")
        except Exception as e:
            logger.error(f"Failed to replace original file with seekable version: {e}")
            raise RuntimeError(f"Failed to replace original file: {e}")
