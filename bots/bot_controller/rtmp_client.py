import logging
import subprocess

logger = logging.getLogger(__name__)


class RTMPClient:
    def __init__(self, rtmp_url):
        """
        Initialize the RTMP client for streaming FLV data to an RTMP endpoint.

        Args:
            rtmp_url (str): The RTMP endpoint URL
        """
        self.rtmp_url = rtmp_url
        self.ffmpeg_process = None
        self.is_running = False

    def start(self):
        """Start the RTMP streaming process"""
        if self.is_running:
            return False

        # Configure FFmpeg command to copy the FLV stream directly
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",  # Overwrite output if needed
            "-f",
            "flv",  # Input format is FLV
            "-i",
            "pipe:0",  # Read from stdin
            "-c",
            "copy",  # Copy both audio and video without re-encoding
            "-f",
            "flv",  # Output format
            self.rtmp_url,  # RTMP destination
        ]

        # Start FFmpeg process
        try:
            self.ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8,
            )
            self.is_running = True
            logger.info(f"FFmpeg RTMP client started with PID {self.ffmpeg_process.pid}")
            return True
        except Exception as e:
            logger.info(f"Failed to start FFmpeg process: {e}")
            return False

    def write_data(self, flv_data):
        """
        Write FLV data to the RTMP stream.

        Args:
            flv_data (bytes): FLV formatted data containing audio and video

        Returns:
            bool: True if data was written, False if failed
        """
        if not self.is_running or not self.ffmpeg_process:
            return False

        try:
            self.ffmpeg_process.stdin.write(flv_data)
            self.ffmpeg_process.stdin.flush()
            return True
        except BrokenPipeError:
            logger.info("FFmpeg pipe broken - stream may have failed")
            self.is_running = False
            return False
        except Exception as e:
            logger.warning(f"Error writing data to FFmpeg: {e}")
            self.is_running = False
            return False

    def stop(self):
        """Stop the RTMP streaming process"""
        self.is_running = False

        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdin.close()
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=5.0)
            except Exception as e:
                logger.warning(f"Error stopping FFmpeg process: {e}")
                # Force kill if graceful shutdown fails
                try:
                    self.ffmpeg_process.kill()
                except Exception:
                    pass

            self.ffmpeg_process = None
