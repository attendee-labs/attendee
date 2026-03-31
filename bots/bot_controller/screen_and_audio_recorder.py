import gi

gi.require_version("Gst", "1.0")

import logging
import time

from gi.repository import Gst

logger = logging.getLogger(__name__)

Gst.init(None)


class ScreenAndAudioRecorder:
    def __init__(self, new_video_frame_callback, recording_dimensions, audio_only):
        self.new_video_frame_callback = new_video_frame_callback
        self.pipeline = None
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)

    def pause_recording(self):
        return True

    def resume_recording(self):
        return True

    # ------------------------------------------------------------------ #
    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        self.new_video_frame_callback(data)
        return Gst.FlowReturn.OK

    # ------------------------------------------------------------------ #
    def start_recording(self, display_var: str):
        """Launch a GStreamer capture pipeline and deliver raw frames via callback."""
        width, height = self.screen_dimensions
        crop = dict(top=10, left=10, right=0, bottom=0,
                    width=self.screen_dimensions[0] - 10,
                    height=self.screen_dimensions[1] - 10)

        pipeline_str = (
            f"ximagesrc display-name={display_var} use-damage=0 show-pointer=true "
            f"! video/x-raw,framerate=4/1 "
            f"! videocrop top={crop['top']} left={crop['left']} "
            f"right={crop['right']} bottom={crop['bottom']} "
            f"! videoscale "
            f"! video/x-raw,width={640},height={360} "
            f"! videoconvert "
            f"! video/x-raw,format=RGB "
            f"! appsink name=sink emit-signals=true sync=false drop=true"
        )

        logger.debug("GStreamer pipeline: %s", pipeline_str)
        self.pipeline = Gst.parse_launch(pipeline_str)

        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)

        self.pipeline.set_state(Gst.State.PLAYING)

        time.sleep(2)
        state_ret, current, _ = self.pipeline.get_state(0)
        if current != Gst.State.PLAYING:
            logger.error("GStreamer pipeline failed to reach PLAYING state")
            raise RuntimeError("GStreamer failed to start; see log above")

        logger.info("Recording started for display %s (frames via callback)", display_var)

    # ------------------------------------------------------------------ #
    def _on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        src_name = message.src.name if message.src else "unknown"
        logger.error("GStreamer error from %s: %s  debug: %s", src_name, err, debug)

    # ------------------------------------------------------------------ #
    def stop_recording(self):
        if not self.pipeline:
            return

        logger.info("Stopping screen recorder …")
        self.pipeline.send_event(Gst.Event.new_eos())

        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)

        self.pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        self.pipeline = None
        logger.info("Screen recorder stopped")

    # ------------------------------------------------------------------ #
    def cleanup(self):
        if self.pipeline:
            self.stop_recording()
