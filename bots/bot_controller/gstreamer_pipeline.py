import gi

gi.require_version("Gst", "1.0")
import logging
import time

from gi.repository import GLib, Gst

# Set up the logging configuration
logging.basicConfig(format="%(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class GstreamerPipeline:
    AUDIO_FORMAT_PCM = "audio/x-raw,format=S16LE,channels=1,rate=32000,layout=interleaved"
    AUDIO_FORMAT_FLOAT = "audio/x-raw,format=F32LE,channels=1,rate=48000,layout=interleaved"
    OUTPUT_FORMAT_FLV = "flv"
    OUTPUT_FORMAT_MP4 = "mp4"
    OUTPUT_FORMAT_WEBM = "webm"

    SINK_TYPE_APPSINK = "appsink"
    SINK_TYPE_FILE = "filesink"

    def __init__(
        self,
        *,
        on_new_sample_callback,
        video_frame_size,
        audio_format,
        output_format,
        num_audio_sources,
        sink_type,
        file_location=None,
    ):
        self.on_new_sample_callback = on_new_sample_callback
        self.video_frame_size = video_frame_size
        self.audio_format = audio_format
        self.output_format = output_format
        self.num_audio_sources = num_audio_sources
        self.sink_type = sink_type
        self.file_location = file_location

        self.pipeline = None
        self.appsrc = None
        self.recording_active = False

        self.audio_appsrcs = []
        self.audio_recording_active = False

        self.start_time_ns = None  # Will be set on first frame/audio sample

        # Initialize GStreamer
        Gst.init(None)

        self.queue_drops = {}
        self.last_reported_drops = {}

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
        else:
            raise ValueError(f"Invalid output format: {self.output_format}")

        if self.sink_type == self.SINK_TYPE_APPSINK:
            sink_string = "appsink name=sink emit-signals=true sync=false drop=false "
        elif self.sink_type == self.SINK_TYPE_FILE:
            sink_string = f"filesink location={self.file_location} name=sink sync=false "
        else:
            raise ValueError(f"Invalid sink type: {self.sink_type}")

        if self.num_audio_sources == 1:
            # fmt: off
            audio_source_string = (
                # --- AUDIO STRING FOR 1 AUDIO SOURCE ---
                "appsrc name=audio_source_1 do-timestamp=false stream-type=0 format=time ! "
                "queue name=q5 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "audioconvert ! "
                "audiorate ! "
                "queue name=q6 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "voaacenc bitrate=128000 ! "
                "queue name=q7 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
            )
            # fmt: on
        elif self.num_audio_sources == 3:
            audio_source_string = (
                # --- AUDIO BRANCH 1 ---
                "appsrc name=audio_source_1 do-timestamp=false stream-type=0 format=time ! "
                "queue name=q5_1 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "mixer. "
                # --- AUDIO BRANCH 2 ---
                "appsrc name=audio_source_2 do-timestamp=false stream-type=0 format=time ! "
                "queue name=q5_2 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "mixer. "
                # --- AUDIO BRANCH 3 ---
                "appsrc name=audio_source_3 do-timestamp=false stream-type=0 format=time ! "
                "queue name=q5_3 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "mixer. "
                # --- AUDIO MIXER
                "adder name=mixer ! "
                "queue name=mixer_q1 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "audioconvert ! "
                "audiorate ! "
                "queue name=mixer_q2 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
                "voaacenc bitrate=128000 ! "
                "queue name=mixer_q3 leaky=downstream max-size-buffers=1000000 max-size-bytes=100000000 max-size-time=0 ! "
            )
        else:
            raise ValueError(f"Unsupported number of audio sources: {self.num_audio_sources}")

        pipeline_str = (
            "appsrc name=video_source do-timestamp=false stream-type=0 format=time ! "
            "queue name=q1 max-size-buffers=1000 max-size-bytes=100000000 max-size-time=0 ! "  # q1 can contain 100mb of video before it drops
            "videoconvert ! "
            "videorate ! "
            "queue name=q2 max-size-buffers=5000 max-size-bytes=500000000 max-size-time=0 ! "  # q2 can contain 100mb of video before it drops
            "x264enc tune=zerolatency speed-preset=ultrafast ! "
            "queue name=q3 max-size-buffers=1000 max-size-bytes=100000000 max-size-time=0 ! "
            f"{muxer_string} ! queue name=q4 ! {sink_string} "
            f"{audio_source_string} "
            "muxer. "
        )

        self.pipeline = Gst.parse_launch(pipeline_str)

        # Get both appsrc elements
        self.appsrc = self.pipeline.get_by_name("video_source")

        # Configure video appsrc
        video_caps = Gst.Caps.from_string(f"video/x-raw,format=I420,width={self.video_frame_size[0]},height={self.video_frame_size[1]},framerate=30/1")
        self.appsrc.set_property("caps", video_caps)
        self.appsrc.set_property("format", Gst.Format.TIME)
        self.appsrc.set_property("is-live", True)
        self.appsrc.set_property("do-timestamp", False)
        self.appsrc.set_property("stream-type", 0)  # GST_APP_STREAM_TYPE_STREAM
        self.appsrc.set_property("block", True)  # This helps with synchronization

        logger.info("Configuring audio appsrc...")
        audio_caps = Gst.Caps.from_string(self.audio_format)  # e.g. "audio/x-raw,rate=48000,channels=2,format=S16LE"
        self.audio_appsrcs = []
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
            logger.info(f"Error getting pipeline stats: {e}")

        return True  # Continue timer

    def on_queue_overrun(self, queue, queue_name):
        """Callback for when a queue drops buffers"""
        self.queue_drops[queue_name] += 1
        return True

    def on_mixed_audio_raw_data_received_callback(self, data, timestamp=None, audio_appsrc_idx=0):
        audio_appsrc = self.audio_appsrcs[audio_appsrc_idx]

        # Check if audio recording is active and we have an audio source
        if not self.audio_recording_active or not audio_appsrc or not self.recording_active or not self.appsrc:
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
            logger.info(f"Error processing audio data: {e}")

    def wants_any_video_frames(self):
        if not self.audio_recording_active or not self.audio_appsrcs[0] or not self.recording_active or not self.appsrc:
            return False

        return True

    def on_new_video_frame(self, frame, current_time_ns):
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
            logger.info(f"Error processing video frame: {e}")

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
            logger.info(f"Error during pipeline shutdown: {err}, {debug}")

        self.pipeline.set_state(Gst.State.NULL)
        logger.info("GStreamer pipeline shut down")
