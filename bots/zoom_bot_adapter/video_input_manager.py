import collections
import logging
import threading
import time

import cv2
import numpy as np
import zoom_meeting_sdk as zoom
from gi.repository import GLib

logger = logging.getLogger(__name__)

from bots.utils import create_black_i420_frame


class RawFrameSnapshot:
    """Lightweight snapshot of SDK frame data, safe to use after the callback returns.

    Copies Y/U/V buffers out of the SDK ``data`` object so the expensive
    ``scale_i420`` work can happen on a separate thread without holding the
    GIL during the SDK callback.
    """

    __slots__ = ("width", "height", "y_buffer", "u_buffer", "v_buffer")

    def __init__(self, width, height, y_buffer, u_buffer, v_buffer):
        self.width = width
        self.height = height
        self.y_buffer = y_buffer
        self.u_buffer = u_buffer
        self.v_buffer = v_buffer

    # Duck-type the SDK frame interface so scale_i420 works unchanged.
    def GetStreamWidth(self):
        return self.width

    def GetStreamHeight(self):
        return self.height

    def GetYBuffer(self):
        return self.y_buffer

    def GetUBuffer(self):
        return self.u_buffer

    def GetVBuffer(self):
        return self.v_buffer


def scale_i420(frame, new_size):
    new_width, new_height = new_size
    """
    Scales the given frame in I420 format to new_width x new_height while
    preserving aspect ratio. If the aspect ratios do not match, letterboxes/pillarboxes
    the scaled image on a black background.
    
    :param frame: Frame object with methods:
        - GetStreamWidth()
        - GetStreamHeight()
        - GetYBuffer()
        - GetUBuffer()
        - GetVBuffer()
    :param new_width: Desired width.
    :param new_height: Desired height.
    :return: Scaled (and possibly letter/pillarboxed) I420 frame bytes.
    """
    orig_width = frame.GetStreamWidth()
    orig_height = frame.GetStreamHeight()

    # 1) Convert buffers to NumPy arrays without extra copies if possible.
    y = np.frombuffer(frame.GetYBuffer(), dtype=np.uint8, count=orig_width * orig_height)
    u = np.frombuffer(frame.GetUBuffer(), dtype=np.uint8, count=(orig_width // 2) * (orig_height // 2))
    v = np.frombuffer(frame.GetVBuffer(), dtype=np.uint8, count=(orig_width // 2) * (orig_height // 2))

    # Reshape planes
    y = y.reshape(orig_height, orig_width)
    u = u.reshape(orig_height // 2, orig_width // 2)
    v = v.reshape(orig_height // 2, orig_width // 2)

    # 2) Determine scale preserving aspect ratio
    input_aspect = orig_width / orig_height
    output_aspect = new_width / new_height

    if abs(input_aspect - output_aspect) < 1e-6:
        # Aspect ratios match (or extremely close). Just do a simple stretch to (new_width, new_height).
        scaled_y = cv2.resize(y, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
        scaled_u = cv2.resize(u, (new_width // 2, new_height // 2), interpolation=cv2.INTER_LINEAR)
        scaled_v = cv2.resize(v, (new_width // 2, new_height // 2), interpolation=cv2.INTER_LINEAR)

        # Flatten and return
        return np.concatenate([scaled_y.flatten(), scaled_u.flatten(), scaled_v.flatten()]).astype(np.uint8).tobytes()

    # Otherwise, the aspect ratios differ => letterbox or pillarbox
    # 3) Compute scaled dimensions that fit entirely within (new_width, new_height)
    if input_aspect > output_aspect:
        # The image is relatively wider => match width, shrink height
        scaled_width = new_width
        scaled_height = int(round(new_width / input_aspect))
    else:
        # The image is relatively taller => match height, shrink width
        scaled_height = new_height
        scaled_width = int(round(new_height * input_aspect))

    # 4) Resize Y, U, and V to the scaled dimensions
    scaled_y = cv2.resize(y, (scaled_width, scaled_height), interpolation=cv2.INTER_LINEAR)
    scaled_u = cv2.resize(u, (scaled_width // 2, scaled_height // 2), interpolation=cv2.INTER_LINEAR)
    scaled_v = cv2.resize(v, (scaled_width // 2, scaled_height // 2), interpolation=cv2.INTER_LINEAR)

    # 5) Create the black background only if needed
    # For I420, black is typically (Y=0, U=128, V=128) or (Y=16, U=128, V=128).
    # We'll use Y=0, U=128, V=128 for "dark" black.
    final_y = np.zeros((new_height, new_width), dtype=np.uint8)
    final_u = np.full((new_height // 2, new_width // 2), 128, dtype=np.uint8)
    final_v = np.full((new_height // 2, new_width // 2), 128, dtype=np.uint8)

    # 6) Compute centering offsets for each plane
    # For Y-plane
    offset_y = (new_height - scaled_height) // 2
    offset_x = (new_width - scaled_width) // 2

    # Insert Y
    final_y[offset_y : offset_y + scaled_height, offset_x : offset_x + scaled_width] = scaled_y

    # For U, V planes (subsampled by 2 in each dimension)
    offset_y_uv = offset_y // 2
    offset_x_uv = offset_x // 2

    final_u[
        offset_y_uv : offset_y_uv + (scaled_height // 2),
        offset_x_uv : offset_x_uv + (scaled_width // 2),
    ] = scaled_u
    final_v[
        offset_y_uv : offset_y_uv + (scaled_height // 2),
        offset_x_uv : offset_x_uv + (scaled_width // 2),
    ] = scaled_v

    # 7) Flatten back to I420 layout and return bytes
    return np.concatenate([final_y.flatten(), final_u.flatten(), final_v.flatten()]).astype(np.uint8).tobytes()


class VideoInputStream:
    def __init__(self, video_input_manager, user_id, stream_type, share_source_id):
        self.video_input_manager = video_input_manager
        self.user_id = user_id
        self.stream_type = stream_type
        self.share_source_id = share_source_id
        self.renderer_destroyed = False
        self.last_debug_frame_time = None
        self.renderer_delegate = zoom.ZoomSDKRendererDelegateCallbacks(
            onRawDataFrameReceivedCallback=self.on_raw_video_frame_received_callback,
            onRendererBeDestroyedCallback=self.on_renderer_destroyed_callback,
            onRawDataStatusChangedCallback=self.on_raw_data_status_changed_callback,
        )

        self.renderer = None

        try:
            self.renderer = zoom.createRenderer(self.renderer_delegate)
        except Exception as e:
            logger.error(f"Error creating renderer for user {self.user_id} and share source id {self.share_source_id}: {e}. Will emit black frames for this user.")

        if self.renderer:
            set_resolution_result = self.renderer.setRawDataResolution(zoom.ZoomSDKResolution_360P)
            raw_data_type = {
                VideoInputManager.StreamType.SCREENSHARE: zoom.ZoomSDKRawDataType.RAW_DATA_TYPE_SHARE,
                VideoInputManager.StreamType.VIDEO: zoom.ZoomSDKRawDataType.RAW_DATA_TYPE_VIDEO,
            }[stream_type]

            if stream_type == VideoInputManager.StreamType.SCREENSHARE:
                subscribe_result = self.renderer.subscribe(self.share_source_id, raw_data_type)
            else:
                subscribe_result = self.renderer.subscribe(self.user_id, raw_data_type)

            logger.info(f"In VideoInputStream.init self.renderer = {self.renderer}")
            logger.info(f"In VideoInputStream.init set_resolution_result for user {self.user_id} and share source id {self.share_source_id} is {set_resolution_result}")
            logger.info(f"In VideoInputStream.init subscribe_result for user {self.user_id} and share source id {self.share_source_id} is {subscribe_result}")

        self.raw_data_status = zoom.RawData_Off

        self.last_frame_time = time.time()
        self.black_frame_timer_id = GLib.timeout_add(250, self.send_black_frame)
        self.black_frame_debug_print_ticker = 0

    def on_raw_data_status_changed_callback(self, status):
        self.raw_data_status = status
        logger.info(f"In VideoInputStream.on_raw_data_status_changed_callback raw_data_status for user {self.user_id} is {self.raw_data_status}")

    def send_black_frame(self):
        if self.renderer_destroyed:
            return False

        current_time = time.time()
        if current_time - self.last_frame_time >= 0.25 and self.raw_data_status == zoom.RawData_Off:
            # Create a black frame of the same dimensions
            black_frame = create_black_i420_frame(self.video_input_manager.video_frame_size)
            self.video_input_manager.new_frame_callback(black_frame, time.time_ns())

            if self.black_frame_debug_print_ticker % 10 == 0:
                logger.info(f"In VideoInputStream.send_black_frame for user {self.user_id} sent black frame")
            self.black_frame_debug_print_ticker += 1

        return not self.renderer_destroyed  # Continue timer if not cleaned up

    def cleanup(self):
        if self.renderer_destroyed:
            return

        if self.black_frame_timer_id is not None:
            GLib.source_remove(self.black_frame_timer_id)
            self.black_frame_timer_id = None

        if self.renderer:
            logger.info(f"starting renderer unsubscription + destroy for user {self.user_id} and share source id {self.share_source_id}")
            self.renderer.unSubscribe()
            zoom.destroyRenderer(self.renderer)
            logger.info(f"finished renderer unsubscription + destroy for user {self.user_id} and share source id {self.share_source_id}")

    def on_renderer_destroyed_callback(self):
        self.renderer_destroyed = True
        logger.info(f"renderer destroyed for user {self.user_id}")

    def on_raw_video_frame_received_callback(self, data):
        current_time_ns = time.time_ns()

        if self.renderer_destroyed:
            return

        if not self.video_input_manager.wants_frames_for_user(self.user_id):
            return

        self.last_frame_time = time.time()

        i420_frame = data.GetBuffer()

        if i420_frame is None or len(i420_frame) == 0:
            logger.warning(f"In VideoInputStream.on_raw_video_frame_received_callback invalid frame received for user {self.user_id}")
            return

        if self.last_debug_frame_time is None or time.time() - self.last_debug_frame_time > 1:
            logger.debug(f"In VideoInputStream.on_raw_video_frame_received_callback for user {self.user_id} received frame")
            self.last_debug_frame_time = time.time()

        # Snapshot the raw buffers (fast memcpy) so scale_i420 can run on
        # the drain thread without holding the GIL during this SDK callback.
        snapshot = RawFrameSnapshot(
            width=data.GetStreamWidth(),
            height=data.GetStreamHeight(),
            y_buffer=bytes(data.GetYBuffer()),
            u_buffer=bytes(data.GetUBuffer()),
            v_buffer=bytes(data.GetVBuffer()),
        )
        self.video_input_manager._video_deque.append((snapshot, current_time_ns))
        self.video_input_manager._video_event.set()


class VideoInputManager:
    class StreamType:
        VIDEO = 1
        SCREENSHARE = 2

    class Mode:
        ACTIVE_SPEAKER = 1
        ACTIVE_SHARER = 2
        INACTIVE = 3

    def __init__(self, *, new_frame_callback, wants_any_frames_callback, video_frame_size):
        self.new_frame_callback = new_frame_callback
        self.wants_any_frames_callback = wants_any_frames_callback
        self.video_frame_size = video_frame_size
        self.mode = None
        self.input_streams = []

        # ── Video drain thread ───────────────────────────────────────────
        # SDK video callbacks just snapshot raw buffers into _video_deque.
        # The drain thread does the expensive scale_i420 + push to GStreamer
        # without holding the GIL during the SDK callback.
        self._video_deque = collections.deque()
        self._video_event = threading.Event()
        self._video_drain_stop = False
        self._video_drain_thread = threading.Thread(target=self._drain_video_loop, daemon=True, name="video-drain")
        self._video_drain_thread.start()

    def has_any_video_input_streams(self):
        return len(self.input_streams) > 0

    # ── Video drain thread ───────────────────────────────────────────────

    def _drain_video_loop(self):
        """Drain thread: scale + push video frames outside the SDK callback."""
        while not self._video_drain_stop:
            self._video_event.wait(timeout=0.1)
            self._video_event.clear()
            while self._video_deque:
                snapshot, timestamp_ns = self._video_deque.popleft()
                try:
                    scaled = scale_i420(snapshot, self.video_frame_size)
                    self.new_frame_callback(scaled, timestamp_ns)
                except Exception:
                    logger.exception("Error in video drain loop")

    def _stop_video_drain(self):
        self._video_drain_stop = True
        self._video_event.set()
        self._video_drain_thread.join(timeout=2.0)
        # Flush any remaining frames
        while self._video_deque:
            snapshot, timestamp_ns = self._video_deque.popleft()
            try:
                scaled = scale_i420(snapshot, self.video_frame_size)
                self.new_frame_callback(scaled, timestamp_ns)
            except Exception:
                logger.exception("Error flushing video drain")

    def add_input_streams_if_needed(self, streams_info):
        streams_to_remove = [input_stream for input_stream in self.input_streams if not any(stream_info["user_id"] == input_stream.user_id and stream_info["stream_type"] == input_stream.stream_type and stream_info["share_source_id"] == input_stream.share_source_id for stream_info in streams_info)]

        for stream in streams_to_remove:
            stream.cleanup()
            self.input_streams.remove(stream)

        for stream_info in streams_info:
            if any(input_stream.user_id == stream_info["user_id"] and input_stream.stream_type == stream_info["stream_type"] and input_stream.share_source_id == stream_info["share_source_id"] for input_stream in self.input_streams):
                continue

            self.input_streams.append(
                VideoInputStream(
                    self,
                    stream_info["user_id"],
                    stream_info["stream_type"],
                    stream_info["share_source_id"],
                )
            )

    def cleanup(self):
        self._stop_video_drain()
        for input_stream in self.input_streams:
            input_stream.cleanup()

    def set_mode(self, *, mode, active_speaker_id, active_sharer_id, active_sharer_source_id):
        if mode != VideoInputManager.Mode.ACTIVE_SPEAKER and mode != VideoInputManager.Mode.ACTIVE_SHARER and mode != VideoInputManager.Mode.INACTIVE:
            raise Exception("Unsupported mode " + str(mode))

        logger.info(f"In VideoInputManager.set_mode mode = {mode} active_speaker_id = {active_speaker_id} active_sharer_id = {active_sharer_id} active_sharer_source_id = {active_sharer_source_id}")

        self.mode = mode

        if self.mode == VideoInputManager.Mode.INACTIVE:
            self.add_input_streams_if_needed([])
            return

        if self.mode == VideoInputManager.Mode.ACTIVE_SPEAKER:
            self.active_speaker_id = active_speaker_id
            self.add_input_streams_if_needed(
                [
                    {
                        "stream_type": VideoInputManager.StreamType.VIDEO,
                        "user_id": active_speaker_id,
                        "share_source_id": None,
                    }
                ]
            )

        if self.mode == VideoInputManager.Mode.ACTIVE_SHARER:
            self.active_sharer_id = active_sharer_id
            self.active_sharer_source_id = active_sharer_source_id
            self.add_input_streams_if_needed(
                [
                    {
                        "stream_type": VideoInputManager.StreamType.SCREENSHARE,
                        "user_id": active_sharer_id,
                        "share_source_id": active_sharer_source_id,
                    }
                ]
            )

    def wants_frames_for_user(self, user_id):
        if not self.wants_any_frames_callback():
            return False

        if self.mode == VideoInputManager.Mode.ACTIVE_SPEAKER and user_id != self.active_speaker_id:
            return False

        if self.mode == VideoInputManager.Mode.ACTIVE_SHARER and user_id != self.active_sharer_id:
            return False

        return True
