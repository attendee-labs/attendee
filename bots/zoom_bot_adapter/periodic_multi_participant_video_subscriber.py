import base64
import logging
import time

import cv2
import numpy as np
import zoom_meeting_sdk as zoom
from gi.repository import GLib

logger = logging.getLogger(__name__)


class PeriodicMultiParticipantVideoSubscriber:
    """
    Periodically (default: every 60s) inspects all participants in the meeting and
    subscribes to up to `max_subscriptions` video feeds.

    For each subscribed participant, it forwards frames at `frames_per_second`
    to `frame_callback(participant_id, jpeg_bytes, timestamp_ns)`, with frames:

      - scaled to 360p (640x360)
      - aspect-ratio preserved with letterboxing/pillarboxing
      - encoded as JPEG

    Usage:

        subscriber = PeriodicMultiParticipantVideoSubscriber(
            get_participant_ids_callback=get_all_participant_ids,
            frame_callback=handle_frame,
            max_subscriptions=8,
            frames_per_second=2,
        )
        subscriber.start()

        # later:
        subscriber.stop()
    """

    def __init__(
        self,
        *,
        get_participant_ids_callback,
        frame_callback,
        max_subscriptions: int = 8,
        frames_per_second: float = 2.0,
        refresh_interval_seconds: int = 60,
        target_width: int = 640,
        target_height: int = 360,
        jpeg_quality: int = 70,
    ):
        if max_subscriptions <= 0:
            raise ValueError("max_subscriptions must be > 0")
        if frames_per_second <= 0:
            raise ValueError("frames_per_second must be > 0")

        self.get_participant_ids_callback = get_participant_ids_callback
        self.frame_callback = frame_callback
        self.max_subscriptions = max_subscriptions
        self.frames_per_second = frames_per_second
        self.refresh_interval_seconds = refresh_interval_seconds
        self.target_width = target_width
        self.target_height = target_height
        self.jpeg_quality = jpeg_quality

        # participant_id -> _ParticipantSubscription
        self._subscriptions = {}

        self._refresh_timer_id = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """
        Start periodic subscription management and perform an immediate refresh.
        """
        if self._refresh_timer_id is not None:
            return

        logger.info(
            "Starting PeriodicMultiParticipantVideoSubscriber: max_subscriptions=%d, fps=%.2f, refresh_interval=%ds",
            self.max_subscriptions,
            self.frames_per_second,
            self.refresh_interval_seconds,
        )

        # Immediate initial refresh so we don't wait 60s for first subscriptions
        self._refresh_subscriptions()

        # Periodic refresh
        self._refresh_timer_id = GLib.timeout_add_seconds(self.refresh_interval_seconds, self._refresh_subscriptions)

    def stop(self):
        """
        Stop periodic refresh and unsubscribe from all participants.
        """
        if self._stopped:
            return

        self._stopped = True

        if self._refresh_timer_id is not None:
            GLib.source_remove(self._refresh_timer_id)
            self._refresh_timer_id = None

        # Clean up all subscriptions
        for sub in list(self._subscriptions.values()):
            sub.cleanup()
        self._subscriptions.clear()

        logger.info("Stopped PeriodicMultiParticipantVideoSubscriber")

    def __del__(self):
        # Best-effort cleanup; ignore errors here
        try:
            self.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: subscription management
    # ------------------------------------------------------------------

    def _refresh_subscriptions(self):
        """
        GLib timeout callback. Must return True to keep running.
        """
        if self._stopped:
            self._refresh_timer_id = None
            return False

        try:
            self._do_refresh_subscriptions()
        except Exception:
            logger.exception("Error while refreshing video subscriptions")

        # Keep the timer going
        return True

    def _do_refresh_subscriptions(self):
        # Get full list of participants from the user-supplied callback
        try:
            participant_ids = list(self.get_participant_ids_callback())
        except Exception:
            logger.exception("get_participant_ids_callback raised an exception")
            return

        # Deterministic ordering; you can change this if you want a different policy
        participant_ids = sorted(participant_ids)[: self.max_subscriptions]

        desired_ids = set(participant_ids)
        current_ids = set(self._subscriptions.keys())

        # Unsubscribe from participants we no longer want
        to_remove = current_ids - desired_ids
        for pid in to_remove:
            sub = self._subscriptions.pop(pid, None)
            if sub:
                logger.info("Unsubscribing from video of participant %s", pid)
                sub.cleanup()

        # Subscribe to new participants
        to_add = desired_ids - current_ids
        for pid in to_add:
            logger.info("Subscribing to video of participant %s", pid)
            self._subscriptions[pid] = _ParticipantSubscription(
                owner=self,
                participant_id=pid,
                frames_per_second=self.frames_per_second,
                target_width=self.target_width,
                target_height=self.target_height,
                jpeg_quality=self.jpeg_quality,
            )

    # ------------------------------------------------------------------
    # Internal: frame emission
    # ------------------------------------------------------------------

    def _emit_frame(self, participant_id, jpeg_bytes: bytes, timestamp_ns: int):
        """
        Called by _ParticipantSubscription when it has a JPEG to send.
        Converts JPEG bytes to base64 data URL string to match the web format.
        """
        try:
            # Convert JPEG bytes to base64 data URL string (matching JavaScript behavior)
            base64_jpeg = base64.b64encode(jpeg_bytes).decode('ascii')
            data_url = f"data:image/jpeg;base64,{base64_jpeg}"
            # Encode the data URL as UTF-8 bytes (matching web_bot_adapter format)
            video_data_bytes = data_url.encode('utf-8')
            self.frame_callback(video_data_bytes, participant_id)
        except Exception:
            logger.exception("frame_callback raised an exception for participant %s", participant_id)

    # ------------------------------------------------------------------
    # Static helpers used by subscriptions
    # ------------------------------------------------------------------

    @staticmethod
    def _scale_i420_to_jpeg(data, target_width: int, target_height: int, jpeg_quality: int) -> bytes | None:
        """
        Convert a Zoom raw I420 frame to a letterboxed 360p JPEG.

        `data` is a Zoom raw video frame object with:
            - GetStreamWidth()
            - GetStreamHeight()
            - GetYBuffer()
            - GetUBuffer()
            - GetVBuffer()
        """
        orig_width = data.GetStreamWidth()
        orig_height = data.GetStreamHeight()

        if orig_width <= 0 or orig_height <= 0:
            return None

        try:
            # Extract I420 planes from Zoom buffers
            y_size = orig_width * orig_height
            uv_size = (orig_width // 2) * (orig_height // 2)

            y = np.frombuffer(data.GetYBuffer(), dtype=np.uint8, count=y_size)
            u = np.frombuffer(data.GetUBuffer(), dtype=np.uint8, count=uv_size)
            v = np.frombuffer(data.GetVBuffer(), dtype=np.uint8, count=uv_size)

            # Reconstruct contiguous I420 buffer: Y plane, then U, then V
            i420 = np.concatenate([y, u, v])
            # Shape: (H * 1.5, W) for cv2 COLOR_YUV2BGR_I420
            yuv = i420.reshape((orig_height * 3 // 2, orig_width))

            # Convert to BGR at original resolution
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

            # Letterbox / pillarbox to target_width x target_height
            h, w, _ = bgr.shape
            input_aspect = w / h
            output_aspect = target_width / target_height

            if input_aspect > output_aspect:
                scaled_width = target_width
                scaled_height = int(round(target_width / input_aspect))
            else:
                scaled_height = target_height
                scaled_width = int(round(target_height * input_aspect))

            resized = cv2.resize(
                bgr,
                (scaled_width, scaled_height),
                interpolation=cv2.INTER_LINEAR,
            )

            # Black canvas for letterboxing
            canvas = np.zeros((target_height, target_width, 3), dtype=np.uint8)
            y_off = (target_height - scaled_height) // 2
            x_off = (target_width - scaled_width) // 2
            canvas[y_off : y_off + scaled_height, x_off : x_off + scaled_width, :] = resized

            # Encode as JPEG
            ok, jpeg = cv2.imencode(
                ".jpg",
                canvas,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
            )
            if not ok:
                return None

            return jpeg.tobytes()
        except Exception:
            logger.exception("Failed to convert I420 frame to JPEG")
            return None


class _ParticipantSubscription:
    """
    Manages a single Zoom renderer subscription for one participant.
    """

    def __init__(
        self,
        *,
        owner: PeriodicMultiParticipantVideoSubscriber,
        participant_id,
        frames_per_second: float,
        target_width: int,
        target_height: int,
        jpeg_quality: int,
    ):
        self.owner = owner
        self.participant_id = participant_id
        self.target_width = target_width
        self.target_height = target_height
        self.jpeg_quality = jpeg_quality
        self.destroyed = False

        self.min_frame_interval_ns = int(1e9 / frames_per_second)
        self._last_sent_timestamp_ns = 0
        self.raw_data_status = zoom.RawData_Off

        # Set up renderer + delegate
        self._delegate = zoom.ZoomSDKRendererDelegateCallbacks(
            onRawDataFrameReceivedCallback=self._on_raw_video_frame_received,
            onRendererBeDestroyedCallback=self._on_renderer_destroyed,
            onRawDataStatusChangedCallback=self._on_raw_data_status_changed,
        )

        self._renderer = zoom.createRenderer(self._delegate)

        # 360p raw data resolution
        res_result = self._renderer.setRawDataResolution(zoom.ZoomSDKResolution_360P)

        # Subscribe to VIDEO raw data for this participant
        subscribe_result = self._renderer.subscribe(self.participant_id, zoom.ZoomSDKRawDataType.RAW_DATA_TYPE_VIDEO)

        logger.info(
            "Created _ParticipantSubscription for %s (setRawDataResolution=%s, subscribe=%s)",
            self.participant_id,
            res_result,
            subscribe_result,
        )

    def _on_raw_data_status_changed(self, status):
        self.raw_data_status = status
        logger.info(
            "Raw data status for participant %s changed to %s",
            self.participant_id,
            status,
        )

    def _on_renderer_destroyed(self):
        self.destroyed = True
        logger.info("Renderer destroyed for participant %s", self.participant_id)

    def _on_raw_video_frame_received(self, data):
        if self.destroyed:
            logger.info("Renderer destroyed for participant %s, skipping frame", self.participant_id)
            return

        now_ns = time.time_ns()
        # Enforce per-participant FPS
        if now_ns - self._last_sent_timestamp_ns < self.min_frame_interval_ns:
            return

        jpeg_bytes = PeriodicMultiParticipantVideoSubscriber._scale_i420_to_jpeg(
            data,
            target_width=self.target_width,
            target_height=self.target_height,
            jpeg_quality=self.jpeg_quality,
        )

        if not jpeg_bytes:
            logger.info("Failed to convert I420 frame to JPEG for participant %s", self.participant_id)
            return

        self._last_sent_timestamp_ns = now_ns
        self.owner._emit_frame(self.participant_id, jpeg_bytes, now_ns)

    def cleanup(self):
        if self.destroyed:
            return

        logger.info("Cleaning up subscription for participant %s", self.participant_id)
        try:
            self._renderer.unSubscribe()
        except Exception:
            logger.exception(
                "Error while unsubscribing renderer for participant %s",
                self.participant_id,
            )
        self.destroyed = True
