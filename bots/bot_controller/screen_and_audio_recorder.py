import logging
import os
import shlex
import subprocess
import signal
import time

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    def __init__(self, file_location):
        self.file_location = os.path.abspath(file_location)
        self._gst_proc: subprocess.Popen | None = None
        self.screen_dimensions = (1930, 1090)

    # ------------------------------------------------------------------ #
    def start_recording(self, display_var: str):
        """Launch a GStreamer capture pipeline and verify it is alive."""
        width, height = self.screen_dimensions
        crop = dict(top=10, left=10, right=0, bottom=0,
                    width=1920, height=1080)

        pipeline = f"""
            ximagesrc display-name={display_var} use-damage=0
                ! video/x-raw,framerate=30/1,width={width},height={height}
                ! videocrop top={crop['top']} left={crop['left']}
                           right={crop['right']} bottom={crop['bottom']}
                ! video/x-raw,width={crop['width']},height={crop['height']}
                ! videoconvert
                ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30
                ! queue
                ! mux.
            alsasrc device=default
                ! audio/x-raw
                ! audioconvert
                ! voaacenc bitrate=128000
                ! queue
                ! mux.
            mp4mux name=mux faststart=true
                ! filesink location="{self.file_location}" sync=false
        """

        gst_cmd = ["gst-launch-1.0", "-e"] + shlex.split(pipeline)
        logger.debug("GStreamer command: %s", " ".join(gst_cmd))

        # Capture STDERR → Python logger so we can read the errors
        self._gst_proc = subprocess.Popen(
            gst_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,   # keep on RAM, we’ll read it below
            text=True,
        )

        # Give GStreamer a moment to spin up, then check if it quit
        time.sleep(2)
        if self._gst_proc.poll() is not None:           # already exited
            stderr = self._gst_proc.stderr.read()
            logger.error("GStreamer pipeline exited early:\n%s", stderr)
            raise RuntimeError("GStreamer failed; see log above")

        logger.info(
            "Recording started for display %s -> %s", display_var, self.file_location
        )

    # ------------------------------------------------------------------ #
    def stop_recording(self):
        if not self._gst_proc:
            return

        logger.info("Stopping screen recorder …")
        # Graceful EOS: SIGINT lets mp4mux write headers
        self._gst_proc.send_signal(signal.SIGINT)
        try:
            self._gst_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Pipeline did not stop in time; killing")
            self._gst_proc.kill()
            self._gst_proc.wait()

        # Log any final errors
        stderr = self._gst_proc.stderr.read()
        if stderr:
            logger.debug("GStreamer stderr:\n%s", stderr)

        self._gst_proc = None
        logger.info("Screen recorder stopped (%s)", self.file_location)

    # ------------------------------------------------------------------ #
    # … get_seekable_path / cleanup / make_file_seekable unchanged …

    def cleanup(self):
        if self._gst_proc:
            self.stop_recording()
