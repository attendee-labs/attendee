import logging
import os
import tempfile
import threading
import urllib.request

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, GObject, Gst

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Core class                                                                 #
# --------------------------------------------------------------------------- #
class MP4Demuxer:
    """
    Stream-demux a remote MP4.

    Parameters
    ----------
    url : str
        Full HTTP/HTTPS URL of the MP4.
    on_video_sample : Callable[[float, bytes], None]
        Called with (pts_seconds, raw_rgba_frame).
    on_audio_sample : Callable[[float, bytes], None]
        Called with (pts_seconds, raw_pcm_block).
    loop : bool
        If True, replay the video from the start when it reaches the end.
    """

    def __init__(self, url, output_video_dimensions, on_video_sample, on_audio_sample, loop=False):
        Gst.init(None)
        self._url = url
        self._video_cb = on_video_sample
        self._audio_cb = on_audio_sample
        self._output_video_dimensions = output_video_dimensions
        self._loop_video = loop
        self._playing = False
        self._glib_main_loop = GObject.MainLoop()
        self._thread = None
        self._queue_elements = {}  # Store references to queue elements
        self._temp_file_path = None

        self._download_file()
        self._build_pipeline()

    # ------------------------------------------------------------------ #
    #  File download                                                     #
    # ------------------------------------------------------------------ #
    def _download_file(self) -> None:
        """
        Download the file from URL to a temporary file.
        """
        logger.info(f"Downloading MP4 from {self._url}...")

        # Create a temporary file
        temp_fd, self._temp_file_path = tempfile.mkstemp(suffix=".mp4")
        os.close(temp_fd)  # Close the file descriptor, we'll use the path

        try:
            # Download the file
            urllib.request.urlretrieve(self._url, self._temp_file_path)
            logger.info(f"Downloaded MP4 to {self._temp_file_path}")
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            if os.path.exists(self._temp_file_path):
                os.unlink(self._temp_file_path)
            raise

    # ------------------------------------------------------------------ #
    #  Public control API                                                #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """
        Start streaming in a background GLib thread.
        """
        if self._playing:
            return
        self._pipeline.set_state(Gst.State.PLAYING)
        self._thread = threading.Thread(target=self._glib_main_loop.run, daemon=True)
        self._thread.start()
        self._playing = True

        # Start queue monitoring
        GLib.timeout_add_seconds(10, self._monitor_queue_sizes)

    def stop(self) -> None:
        """
        Stop playback and free resources.
        """
        if not self._playing:
            return
        self._pipeline.send_event(Gst.Event.new_eos())  # graceful EOS
        self._pipeline.set_state(Gst.State.NULL)
        self._glib_main_loop.quit()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._playing = False

        # Clean up temporary file
        self._cleanup_temp_file()

    def is_playing(self) -> bool:
        """
        Returns True while the pipeline is running.
        """
        return self._playing

    def _cleanup_temp_file(self) -> None:
        """
        Remove the temporary file if it exists.
        """
        if self._temp_file_path and os.path.exists(self._temp_file_path):
            try:
                os.unlink(self._temp_file_path)
                logger.info(f"Cleaned up temporary file: {self._temp_file_path}")
            except Exception as e:
                logger.error(f"Error cleaning up temporary file: {e}")
            finally:
                self._temp_file_path = None

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                  #
    # ------------------------------------------------------------------ #
    def _build_pipeline(self) -> None:
        """
        Create elements, link them, and attach callbacks.
        """
        launch = f"""
            filesrc location={self._temp_file_path} ! qtdemux name=d

                d. ! queue name=video_queue                                 \
                        max-size-buffers=50 max-size-bytes=0 max-size-time=0 \
                        ! h264parse ! avdec_h264                        \
                        ! videorate max-rate=25 drop-only=true          \
                        ! videoconvert                                  \
                        ! videoscale                                    \
                        ! video/x-raw,width={self._output_video_dimensions[0]},height={self._output_video_dimensions[1]},format=I420 \
                        ! appsink name=vsink emit-signals=true sync=true   \
                                max-buffers=10 drop=false              

                d. ! queue name=audio_queue                                 \
                        max-size-buffers=100 max-size-bytes=0 max-size-time=0 \
                        ! aacparse ! avdec_aac                          \
                        ! audioconvert                                  \
                        ! audioresample                                 \
                        ! audio/x-raw,format=S16LE,channels=1,rate=8000 \
                        ! appsink name=asink emit-signals=true sync=true \
                                max-buffers=30 drop=false
        """
        self._pipeline = Gst.parse_launch(launch)

        # sink elements
        vsink = self._pipeline.get_by_name("vsink")
        asink = self._pipeline.get_by_name("asink")

        # Get queue elements for monitoring
        self._queue_elements["video_queue"] = self._pipeline.get_by_name("video_queue")
        self._queue_elements["audio_queue"] = self._pipeline.get_by_name("audio_queue")

        # connect data callbacks
        vsink.connect("new-sample", self._on_video_sample)
        asink.connect("new-sample", self._on_audio_sample)

        # bus watch to stop on EOS / ERROR
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _monitor_queue_sizes(self) -> bool:
        """
        Monitor and log queue sizes. Returns True to continue the timer.
        """
        if not self._playing:
            return False  # Stop the timer

        logger.info("\n=== MP4Demuxer Queue Status ===")
        for queue_name, queue_element in self._queue_elements.items():
            if queue_element:
                try:
                    current_buffers = queue_element.get_property("current-level-buffers")
                    max_buffers = queue_element.get_property("max-size-buffers")
                    current_bytes = queue_element.get_property("current-level-bytes")
                    max_bytes = queue_element.get_property("max-size-bytes")
                    current_time = queue_element.get_property("current-level-time")
                    max_time = queue_element.get_property("max-size-time")

                    logger.info(f"{queue_name}:")
                    if max_buffers > 0:
                        logger.info(f"  Buffers: {current_buffers}/{max_buffers} ({current_buffers / max_buffers * 100:.1f}%)")
                    else:
                        logger.info(f"  Buffers: {current_buffers} (no limit)")
                    if max_bytes > 0:
                        logger.info(f"  Bytes: {current_bytes:,}/{max_bytes:,} ({current_bytes / max_bytes * 100:.1f}%)")
                    else:
                        logger.info(f"  Bytes: {current_bytes:,} (no limit)")
                    if max_time > 0:
                        logger.info(f"  Time: {current_time / 1e9:.2f}s/{max_time / 1e9:.2f}s ({current_time / max_time * 100:.1f}%)")
                    else:
                        logger.info(f"  Time: {current_time / 1e9:.2f}s (no limit)")

                except Exception as e:
                    logger.error(f"Error getting stats for {queue_name}: {e}")
            else:
                logger.error(f"{queue_name}: Element not found")
        logger.info("===============================\n")

        return True  # Continue the timer

    # ------------------------- Sample handlers ------------------------- #
    def _on_video_sample(self, sink) -> Gst.FlowReturn:
        return self._dispatch_sample(sink, self._video_cb)

    def _on_audio_sample(self, sink) -> Gst.FlowReturn:
        return self._dispatch_sample(sink, self._audio_cb)

    def _dispatch_sample(self, sink, user_cb):
        if user_cb is None:
            return Gst.FlowReturn.OK

        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        pts = buf.pts / Gst.SECOND
        success, mapinfo = buf.map(Gst.MapFlags.READ)
        if success:
            try:
                user_cb(pts, bytes(mapinfo.data))
            finally:
                buf.unmap(mapinfo)

        return Gst.FlowReturn.OK

    # ------------------------- Bus handler ----------------------------- #
    def _on_bus_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            self.stop()
            return
        if t == Gst.MessageType.EOS:
            if self._loop_video:
                success = self._pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0,
                )
                if not success:
                    logger.warning("Seek failed, stopping playback")
                    self.stop()
            else:
                self.stop()

    def __del__(self):
        """
        Destructor to ensure cleanup of temporary file.
        """
        self._cleanup_temp_file()
