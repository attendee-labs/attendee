import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    def __init__(self, file_location):
        self.file_location = file_location
        self.ffmpeg_proc = None
        self.screen_dimensions = (1930, 1090)

    def start_recording(self, display_var):
        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")
        ffmpeg_cmd = ["ffmpeg", "-y", "-thread_queue_size", "4096", "-framerate", "30", "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var, "-thread_queue_size", "4096", "-f", "pulse", "-i", "default", "-vf", "crop=1920:1080:10:10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-strict", "experimental", "-b:a", "128k", self.file_location]

        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
        self.ffmpeg_proc.terminate()
        self.ffmpeg_proc.wait()
        logger.info(f"Stopped debug screen recorder for display with dimensions {self.screen_dimensions} and file location {self.file_location}")

    def get_seekable_path(self, path):
        """
        Transform a file path to include '.seekable' before the extension.
        Example: /tmp/file.webm -> /tmp/file.seekable.webm
        """
        base, ext = os.path.splitext(path)
        return f"{base}.seekable{ext}"

    def cleanup(self):
        input_path = self.file_location

        # Check if input file exists
        if not os.path.exists(input_path):
            logger.info(f"Input file does not exist at {input_path}, creating empty file")
            with open(input_path, "wb"):
                pass  # Create empty file
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
