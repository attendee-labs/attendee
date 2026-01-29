import gi

gi.require_version("Gst", "1.0")
import logging
import time

from gi.repository import GLib, Gst

from bots.utils import create_black_i420_frame, create_zero_pcm_audio

logger = logging.getLogger(__name__)


class RTMSGstreamerPipeline:
    AUDIO_FORMAT_PCM = "audio/x-raw,format=S16LE,channels=1,rate=32000,layout=interleaved"
    AUDIO_FORMAT_FLOAT = "audio/x-raw,format=F32LE,channels=1,rate=48000,layout=interleaved"
    AUDIO_FORMAT_PCM_16KHZ = "audio/x-raw,format=S16LE,channels=1,rate=16000,layout=interleaved"
    OUTPUT_FORMAT_FLV = "flv"
    OUTPUT_FORMAT_MP4 = "mp4"
    OUTPUT_FORMAT_WEBM = "webm"
    OUTPUT_FORMAT_MP3 = "mp3"

    SINK_TYPE_APPSINK = "appsink"
    SINK_TYPE_FILE = "filesink"

    VIDEO_FORMAT_I420 = "I420"
    VIDEO_FORMAT_H264 = "H264"

    def __init__(
        self,
        *,
        on_new_sample_callback,
        video_frame_size,
        audio_format,
        output_format,
        sink_type,
        file_location=None,
    ):
        self.on_new_sample_callback = on_new_sample_callback
        self.video_frame_size = video_frame_size
        self.video_format = self.VIDEO_FORMAT_H264
        self.audio_format = audio_format
        self.output_format = output_format
        self.sink_type = sink_type
        self.file_location = file_location

        self.pipeline = None
        self.appsrc = None
        self.recording_active = False

        self.audio_appsrcs = []
        self.audio_recording_active = False

        self.start_time_ns = None  # Will be set on first frame/audio sample

        # Pause state tracking
        self.pause_timer_id = None

        # Initialize GStreamer
        Gst.init(None)

        self.queue_drops = {}
        self.last_reported_drops = {}

        self.overlay_text = ""
        self.overlay_enabled = True

    def update_overlay_text(self, text: str | None):
        """Update the on-screen text (e.g., speaker name). Use '' or None to hide."""

        if (text or "") == self.overlay_text:
            return

        self.overlay_text = text or ""
        if not self.pipeline or not self.overlay_enabled:
            return

        overlay = self.pipeline.get_by_name("overlay")
        if not overlay:
            return

        # Apply on the GLib main loop thread to avoid cross-thread issues
        def _apply():
            try:
                overlay.set_property("text", self.overlay_text)
            except Exception as e:
                logger.info(f"Failed to set overlay text: {e}")
            return False  # don't repeat

        GLib.idle_add(_apply)

    def on_new_sample_from_appsink(self, sink):
        """Handle new samples from the appsink"""
        sample = sink.emit("pull-sample")
        if sample:
            buffer = sample.get_buffer()
            data = buffer.extract_dup(0, buffer.get_size())
            self.on_new_sample_callback(data)
            return Gst.FlowReturn.OK
        return Gst.FlowReturn.ERROR

    def setup(self):
        """Initialize GStreamer pipeline for combined MP4 recording with audio and video"""
        self.start_time_ns = None

        # Setup muxer based on output format
        if self.output_format == self.OUTPUT_FORMAT_MP4:
            muxer_string = "mp4mux name=muxer"
        elif self.output_format == self.OUTPUT_FORMAT_FLV:
            muxer_string = "h264parse ! flvmux name=muxer streamable=true"
        elif self.output_format == self.OUTPUT_FORMAT_WEBM:
            muxer_string = "h264parse ! matroskamux name=muxer"
        elif self.output_format == self.OUTPUT_FORMAT_MP3:
            muxer_string = ""
        else:
            raise ValueError(f"Invalid output format: {self.output_format}")

        if self.sink_type == self.SINK_TYPE_APPSINK:
            sink_string = "appsink name=sink emit-signals=true sync=false drop=false "
        elif self.sink_type == self.SINK_TYPE_FILE:
            sink_string = f"filesink location={self.file_location} name=sink sync=false "
        else:
            raise ValueError(f"Invalid sink type: {self.sink_type}")

        # fmt: off
        audio_source_string = (
            # --- AUDIO STRING FOR 1 AUDIO SOURCE ---
            "appsrc name=audio_source_1 do-timestamp=false stream-type=0 format=time ! "
            "queue name=q5 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
            "audioconvert ! "
            "audiorate ! "
            "queue name=q6 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
        )

        if self.output_format == self.OUTPUT_FORMAT_MP3:
            pipeline_str = (
                f"{audio_source_string}"        # raw audio → …
                "flacenc ! "
                f"{sink_string}"               # … → sink
            )
        else:
            # Common pipeline components
            video_source = "appsrc name=video_source do-timestamp=false stream-type=0 format=time ! "
            queue1 = "queue name=q1 max-size-buffers=1000 max-size-bytes=100000000 max-size-time=0 ! "
            overlay_and_encode = (
                "videoconvert ! "
                "textoverlay name=overlay halignment=left valignment=bottom xpad=10 ypad=10 "
                "font-desc=\"Sans, 10\" draw-shadow=false draw-outline=false ! "
                "videorate ! "
                "queue name=q2 max-size-buffers=5000 max-size-bytes=500000000 max-size-time=0 ! "
                "x264enc tune=zerolatency speed-preset=ultrafast ! "
                "queue name=q3 max-size-buffers=1000 max-size-bytes=100000000 max-size-time=0 ! "
            )
            
            if self.video_format == self.VIDEO_FORMAT_H264:
                # H.264 input → parse → decode → overlay → re-encode
                decode_section = "h264parse ! avdec_h264 ! "
            else:
                # Raw input (e.g., I420) → overlay → encode
                decode_section = ""
            
            video_pipeline_str = video_source + queue1 + decode_section + overlay_and_encode

            pipeline_str = (
                f"{video_pipeline_str}"
                f"{muxer_string} ! queue name=q4 ! {sink_string} "
                f"{audio_source_string} "
                "voaacenc bitrate=128000 ! "
                "queue name=q7 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "muxer. "
            )

        self.pipeline = Gst.parse_launch(pipeline_str)

        overlay = self.pipeline.get_by_name("overlay")
        if overlay and self.overlay_enabled:
            try:
                overlay.set_property("text", self.overlay_text or "")
            except Exception as e:
                logger.info(f"Unable to init overlay text: {e}")

        if self.output_format != self.OUTPUT_FORMAT_MP3:
            # Get both appsrc elements
            self.appsrc = self.pipeline.get_by_name("video_source")

            # Configure video appsrc
            if self.video_format == self.VIDEO_FORMAT_I420:
                video_caps = Gst.Caps.from_string(f"video/x-raw,format=I420,width={self.video_frame_size[0]},height={self.video_frame_size[1]},framerate=30/1")
            elif self.video_format == self.VIDEO_FORMAT_H264:
                video_caps = Gst.Caps.from_string("video/x-h264,stream-format=byte-stream,alignment=au")
            else:
                raise ValueError(f"Invalid video format: {self.video_format}")
            self.appsrc.set_property("caps", video_caps)
            self.appsrc.set_property("format", Gst.Format.TIME)
            self.appsrc.set_property("is-live", True)
            self.appsrc.set_property("do-timestamp", False)
            self.appsrc.set_property("stream-type", 0)  # GST_APP_STREAM_TYPE_STREAM
            self.appsrc.set_property("block", True)  # This helps with synchronization
        else:
            self.appsrc = None

        audio_caps = Gst.Caps.from_string(self.audio_format)  # e.g. "audio/x-raw,rate=48000,channels=2,format=S16LE"
        self.audio_appsrcs = []
        self.num_audio_sources = 1
        for i in range(self.num_audio_sources):
            audio_appsrc = self.pipeline.get_by_name(f"audio_source_{i + 1}")
            audio_appsrc.set_property("caps", audio_caps)
            audio_appsrc.set_property("format", Gst.Format.TIME)
            audio_appsrc.set_property("is-live", True)
            audio_appsrc.set_property("do-timestamp", False)
            audio_appsrc.set_property("stream-type", 0)  # GST_APP_STREAM_TYPE_STREAM
            audio_appsrc.set_property("block", True)
            self.audio_appsrcs.append(audio_appsrc)

        # Set up bus
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_pipeline_message)

        # Connect to the sink element
        if self.sink_type == self.SINK_TYPE_APPSINK:
            sink = self.pipeline.get_by_name("sink")
            sink.connect("new-sample", self.on_new_sample_from_appsink)

        # Start the pipeline
        self.pipeline.set_state(Gst.State.PLAYING)

        self.recording_active = True
        self.audio_recording_active = True

        # Initialize queue monitoring
        self.queue_drops = {}
        self.last_reported_drops = {}

        # Find all queue elements and connect drop signals
        iterator = self.pipeline.iterate_elements()
        while True:
            result, element = iterator.next()
            if result == Gst.IteratorResult.DONE:
                break
            if result != Gst.IteratorResult.OK:
                continue

            if isinstance(element, Gst.Element) and element.get_factory().get_name() == "queue":
                queue_name = element.get_name()
                self.queue_drops[queue_name] = 0
                self.last_reported_drops[queue_name] = 0
                element.connect("overrun", self.on_queue_overrun, queue_name)

        # Start statistics monitoring
        GLib.timeout_add_seconds(15, self.monitor_pipeline_stats)

    def on_pipeline_message(self, bus, message):
        """Handle pipeline messages"""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()

            src = message.src
            src_name = src.name if src else "unknown"
            logger.info(f"GStreamer Error: {err}, Debug: {debug}, src_name: {src_name}")
        elif t == Gst.MessageType.EOS:
            logger.info("GStreamer pipeline reached end of stream")

    def monitor_pipeline_stats(self):
        """Periodically print pipeline statistics"""
        if not self.recording_active:
            return False

        try:
            logger.info("\nDropped Buffers Since Last Check:")
            for queue_name in self.queue_drops:
                drops = self.queue_drops[queue_name] - self.last_reported_drops[queue_name]
                if drops > 0:
                    logger.info(f"  {queue_name}: {drops} buffers dropped")
                self.last_reported_drops[queue_name] = self.queue_drops[queue_name]

        except Exception as e:
            logger.warning(f"Error getting pipeline stats: {e}")

        return True  # Continue timer

    def send_pause_frames(self):
        """Send black frames and zero audio while paused"""
        if not self.recording_active:
            return False  # Stop timer if recording is no longer active

        current_time_ns = time.time_ns()

        # Send black video frame if video is enabled by calling existing method
        if self.appsrc and self.output_format != self.OUTPUT_FORMAT_MP3:
            black_frame = create_black_i420_frame(self.video_frame_size)
            self.on_new_video_frame(black_frame, current_time_ns, is_pause_frame=True)

        # Send zero audio for all audio sources by calling existing method
        if self.audio_recording_active and self.audio_appsrcs:
            zero_audio = create_zero_pcm_audio(self.audio_format, duration_ms=250)
            self.on_mixed_audio_raw_data_received_callback(zero_audio, current_time_ns, is_pause_frame=True)

        return True  # Continue timer

    def on_queue_overrun(self, queue, queue_name):
        """Callback for when a queue drops buffers"""
        self.queue_drops[queue_name] += 1
        return True

    def on_mixed_audio_raw_data_received_callback(self, data, timestamp=None, audio_appsrc_idx=0, is_pause_frame=False):
        if self.pause_timer_id is not None and not is_pause_frame:
            return

        audio_appsrc = self.audio_appsrcs[audio_appsrc_idx]

        if not self.audio_recording_active or not audio_appsrc or not self.recording_active or (not self.appsrc and self.output_format != self.OUTPUT_FORMAT_MP3):
            return

        try:
            current_time_ns = timestamp if timestamp else time.time_ns()
            buffer_bytes = data
            buffer = Gst.Buffer.new_wrapped(buffer_bytes)

            # Initialize start time if not set
            if self.start_time_ns is None:
                self.start_time_ns = current_time_ns

            # Calculate timestamp relative to same start time as video
            buffer.pts = current_time_ns - self.start_time_ns

            ret = audio_appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                logger.info(f"Warning: Failed to push audio buffer to pipeline: {ret}")
        except Exception as e:
            logger.warning(f"Error processing audio data: {e}")

    def wants_any_video_frames(self):
        if not self.audio_recording_active or not self.audio_appsrcs[0] or not self.recording_active or not self.appsrc:
            return False

        return True

    def on_new_video_frame(self, frame, current_time_ns, is_pause_frame=False, overlay_text=None):
        if self.pause_timer_id is not None and not is_pause_frame:
            return

        try:
            # Initialize start time if not set
            if self.start_time_ns is None:
                self.start_time_ns = current_time_ns

            # Calculate buffer timestamp relative to start time
            buffer_pts = current_time_ns - self.start_time_ns

            # Create buffer with timestamp
            buffer = Gst.Buffer.new_wrapped(frame)
            buffer.pts = buffer_pts

            # Default to 33ms (30fps)
            buffer.duration = 33 * 1000 * 1000  # 33ms in nanoseconds

            # Push buffer to pipeline
            ret = self.appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                logger.info(f"Warning: Failed to push buffer to pipeline: {ret}")

        except Exception as e:
            logger.warning(f"Error processing video frame: {e}")

        if overlay_text:
            self.update_overlay_text(overlay_text)

    def pause_recording(self):
        """Pause the pipeline and start sending black frames and zero audio"""
        if self.pause_timer_id is not None:
            return

        # If there is no start time, then the pipeline has not been started yet
        if not self.start_time_ns:
            return

        logger.info("Pausing GStreamer pipeline - switching to black frames and zero audio")

        # Start the pause timer to send black frames and zero audio every 250ms
        self.pause_timer_id = GLib.timeout_add(250, self.send_pause_frames)

    def resume_recording(self):
        """Unpause the pipeline and resume normal operation"""
        if self.pause_timer_id is None:
            return

        logger.info("Unpausing GStreamer pipeline - resuming normal operation")

        # Stop the pause timer
        GLib.source_remove(self.pause_timer_id)
        self.pause_timer_id = None

    def cleanup(self):
        logger.info("Shutting down GStreamer pipeline...")

        self.recording_active = False
        self.audio_recording_active = False

        if not self.pipeline:
            return
        bus = self.pipeline.get_bus()
        bus.remove_signal_watch()

        if self.appsrc:
            self.appsrc.emit("end-of-stream")
        for audio_appsrc in self.audio_appsrcs:
            audio_appsrc.emit("end-of-stream")

        msg = bus.timed_pop_filtered(
            5 * 60 * Gst.SECOND,  # 5 minute timeout
            Gst.MessageType.EOS | Gst.MessageType.ERROR,
        )

        if msg and msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            logger.warning(f"Error during pipeline shutdown: {err}, {debug}")

        self.pipeline.set_state(Gst.State.NULL)
        logger.info("GStreamer pipeline shut down")
