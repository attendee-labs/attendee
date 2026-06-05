import base64
import logging
import time
from typing import Callable

import cv2
import numpy as np
import zoom_meeting_sdk as zoom
from gi.repository import GLib

from bots.per_participant_realtime_video_configuration import PerParticipantRealtimeVideoConfiguration

logger = logging.getLogger(__name__)


class RealtimePerParticipantVideoFrameGenerator:
    """
    Periodically (default: every 4s) inspects all participants in the meeting and
    subscribes to up to `max_subscriptions` video feeds.

    For each subscribed participant, it forwards frames at `frames_per_second`
    to `frame_callback(frame, participant_id, source)`, with frames:

      - scaled to the configured resolution
      - aspect-ratio preserved with letterboxing/pillarboxing
      - encoded as JPEG

    Usage:

        subscriber = RealtimePerParticipantVideoFrameGenerator(
            get_participant_ids_callback=get_all_participant_ids,
            frame_callback=handle_frame,
            per_participant_realtime_video_configuration=configuration,
            max_subscriptions=8,
        )
        subscriber.start()
    """

    def __init__(
        self,
        *,
        frame_callback,
        get_participants_ctrl_callback: Callable,
        get_meeting_sharing_controller_callback: Callable,
        get_recording_is_paused_callback: Callable,
        per_participant_realtime_video_configuration: PerParticipantRealtimeVideoConfiguration,
        max_subscriptions: int = 8,
        refresh_interval_seconds: int = 4,
    ):
        if max_subscriptions <= 0:
            raise ValueError("max_subscriptions must be > 0")

        self.frame_callback = frame_callback
        self.get_participants_ctrl_callback = get_participants_ctrl_callback
        self.get_meeting_sharing_controller_callback = get_meeting_sharing_controller_callback
        self.get_recording_is_paused_callback = get_recording_is_paused_callback
        self.per_participant_realtime_video_configuration = per_participant_realtime_video_configuration
        self.max_subscriptions = max_subscriptions
        self.refresh_interval_seconds = refresh_interval_seconds
        self._participant_id_to_last_active_speaker_time = {}

        # (participant_id, share_source_id) -> _PerParticipantVideoFrameSubscription
        self._subscriptions = {}

        self._refresh_timer_id = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_last_active_speaker_time(self, participant_id):
        self._participant_id_to_last_active_speaker_time[participant_id] = time.time()

    def start(self):
        """
        Start periodic subscription management and perform an immediate refresh.
        """

        # Reset if it was already running
        self.reset()

        logger.info(
            "Starting PeriodicMultiParticipantVideoSubscriber: max_subscriptions=%d, refresh_interval=%ds, webcam=%s, screenshare=%s",
            self.max_subscriptions,
            self.refresh_interval_seconds,
            self.per_participant_realtime_video_configuration.webcam_configuration.resolution,
            self.per_participant_realtime_video_configuration.screenshare_configuration.resolution,
        )

        # Immediate initial refresh so we don't wait 60s for first subscriptions
        self._refresh_subscriptions()

        # Periodic refresh
        self._refresh_timer_id = GLib.timeout_add_seconds(self.refresh_interval_seconds, self._refresh_subscriptions)

    def reset(self):
        """
        Clear periodic refresh and unsubscribe from all participants.
        """

        if self._refresh_timer_id is not None:
            GLib.source_remove(self._refresh_timer_id)
            self._refresh_timer_id = None

        # Clean up all subscriptions
        for sub in list(self._subscriptions.values()):
            try:
                sub.cleanup()
            except Exception:
                logger.exception("Error cleaning up subscription for participant %s", sub.participant_id)
        self._subscriptions.clear()

        logger.info("Reset PeriodicMultiParticipantVideoSubscriber")

    def __del__(self):
        # Best-effort cleanup; ignore errors here
        try:
            self.reset()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: subscription management
    # ------------------------------------------------------------------

    def _refresh_subscriptions(self):
        """
        GLib timeout callback. Must return True to keep running.
        """

        try:
            self._do_refresh_subscriptions()
        except Exception:
            logger.exception("Error while refreshing video subscriptions")

        # Keep the timer going
        return True

    def _do_refresh_subscriptions(self):
        # List of (participant_id, share_source_id)
        desired_subscription_ids = set()

        webcam_configuration = self.per_participant_realtime_video_configuration.webcam_configuration
        screenshare_configuration = self.per_participant_realtime_video_configuration.screenshare_configuration

        # Get list of all participant ids
        all_participant_ids = self.get_participants_ctrl_callback().GetParticipantsList()

        # Order them by how recently they were the active speaker
        participant_ids = sorted(
            all_participant_ids,
            key=lambda x: self._participant_id_to_last_active_speaker_time.get(x, 0),
            reverse=True,
        )

        # Find all the sharers
        # If someone was sharing before we joined, we will not receive an event, so we need to poll for the active sharer
        viewable_sharing_user_list = self.get_meeting_sharing_controller_callback().GetViewableSharingUserList()

        if viewable_sharing_user_list and screenshare_configuration.enabled:
            for sharing_user_id in viewable_sharing_user_list:
                sharing_source_info_list = self.get_meeting_sharing_controller_callback().GetSharingSourceInfoList(sharing_user_id)

                if not sharing_source_info_list:
                    continue

                for sharing_source_info in sharing_source_info_list:
                    if len(desired_subscription_ids) >= self.max_subscriptions:
                        break

                    desired_subscription_ids.add((sharing_source_info.userid, sharing_source_info.shareSourceID))

        # Loop over the participant_ids ordered by how recently they were the active speaker
        # Add participants who have their video turned on to the available_subscriptions list
        # until we have max_subscriptions subscriptions

        for participant_id in participant_ids:
            participant = self.get_participants_ctrl_callback().GetUserByUserID(participant_id)
            if participant.IsVideoOn() and webcam_configuration.enabled:
                desired_subscription_ids.add((participant_id, None))

            if len(desired_subscription_ids) >= self.max_subscriptions:
                break

        current_subscription_ids = set(self._subscriptions.keys())

        # Unsubscribe from subscriptions we no longer want
        subscription_ids_to_remove = current_subscription_ids - desired_subscription_ids
        for subscription_id_to_remove in subscription_ids_to_remove:
            removed_subscription = self._subscriptions.pop(subscription_id_to_remove, None)
            if removed_subscription:
                logger.info("Unsubscribing from participant %s and share source id %s", subscription_id_to_remove[0], subscription_id_to_remove[1])
                removed_subscription.cleanup()

        # Subscribe to new participants
        subscription_ids_to_add = desired_subscription_ids - current_subscription_ids
        for subscription_id_to_add in subscription_ids_to_add:
            is_screenshare = subscription_id_to_add[1] is not None
            source_configuration = screenshare_configuration if is_screenshare else webcam_configuration

            logger.info("Subscribing to video of participant %s and share source id %s", subscription_id_to_add[0], subscription_id_to_add[1])
            self._subscriptions[subscription_id_to_add] = _PerParticipantVideoFrameSubscription(
                owner=self,
                participant_id=subscription_id_to_add[0],
                share_source_id=subscription_id_to_add[1],
                source_configuration=source_configuration,
            )

    # ------------------------------------------------------------------
    # Internal: frame emission
    # ------------------------------------------------------------------

    def _emit_frame(self, frame: bytes, participant_id: str, source: str):
        """
        Called by _PerParticipantVideoFrameSubscription when it has a JPEG to send.
        Converts JPEG bytes to base64 string.
        """
        if self.get_recording_is_paused_callback():
            return
        try:
            base64_jpeg = base64.b64encode(frame).decode("ascii")
            video_data_bytes = base64_jpeg.encode("utf-8")
            self.frame_callback(video_data_bytes, participant_id, source)
        except Exception:
            logger.exception("frame_callback raised an exception for participant %s", participant_id)

    # ------------------------------------------------------------------
    # Static helpers used by subscriptions
    # ------------------------------------------------------------------

    @staticmethod
    def _scale_i420_to_jpeg(data, target_width: int, target_height: int, jpeg_quality: int) -> bytes | None:
        """
        Convert a Zoom raw I420 frame to a letterboxed JPEG.

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


class _PerParticipantVideoFrameSubscription:
    """
    Manages a single Zoom renderer subscription for one participant.
    """

    def __init__(
        self,
        *,
        owner: RealtimePerParticipantVideoFrameGenerator,
        participant_id,
        share_source_id,
        source_configuration,
    ):
        self.owner = owner
        self.participant_id = participant_id
        self.share_source_id = share_source_id
        self.source_configuration = source_configuration
        self.destroyed = False

        self.min_frame_interval_ns = int(1e9 / source_configuration.framerate)
        self._last_sent_timestamp_ns = 0
        self.raw_data_status = zoom.RawData_Off

        self.source = "webcam" if self.share_source_id is None else "screenshare"

        # Set up renderer + delegate
        self._delegate = zoom.ZoomSDKRendererDelegateCallbacks(
            onRawDataFrameReceivedCallback=self._on_raw_video_frame_received,
            onRendererBeDestroyedCallback=self._on_renderer_destroyed,
            onRawDataStatusChangedCallback=self._on_raw_data_status_changed,
        )

        self._renderer = zoom.createRenderer(self._delegate)

        zoom_resolution_map = {
            "360p": zoom.ZoomSDKResolution_360P,
            "720p": zoom.ZoomSDKResolution_720P,
            "1080p": zoom.ZoomSDKResolution_1080P,
        }
        zoom_resolution = zoom_resolution_map.get(source_configuration.resolution, zoom.ZoomSDKResolution_360P)
        res_result = self._renderer.setRawDataResolution(zoom_resolution)

        if self.share_source_id is not None:
            raw_data_type = zoom.ZoomSDKRawDataType.RAW_DATA_TYPE_SHARE
            subscribe_result = self._renderer.subscribe(self.share_source_id, raw_data_type)
        else:
            raw_data_type = zoom.ZoomSDKRawDataType.RAW_DATA_TYPE_VIDEO
            subscribe_result = self._renderer.subscribe(self.participant_id, raw_data_type)

        logger.info(
            "Created _PerParticipantVideoFrameSubscription for participant %s, share_source_id %s, raw_data_type %s (setRawDataResolution=%s, subscribe=%s)",
            self.participant_id,
            self.share_source_id,
            raw_data_type,
            res_result,
            subscribe_result,
        )

    def _on_raw_data_status_changed(self, status):
        self.raw_data_status = status
        logger.info(
            "Raw data status for participant %s (share_source_id %s) changed to %s",
            self.participant_id,
            self.share_source_id,
            status,
        )

    def _on_renderer_destroyed(self):
        self.destroyed = True
        logger.info("Renderer destroyed for participant %s (share_source_id %s)", self.participant_id, self.share_source_id)

    def _on_raw_video_frame_received(self, data):
        if self.destroyed:
            logger.info("Renderer destroyed for participant %s (share_source_id %s), skipping frame", self.participant_id, self.share_source_id)
            return

        now_ns = time.monotonic_ns()
        # Enforce per-participant FPS
        if now_ns - self._last_sent_timestamp_ns < self.min_frame_interval_ns:
            return

        jpeg_bytes = RealtimePerParticipantVideoFrameGenerator._scale_i420_to_jpeg(
            data,
            target_width=self.source_configuration.width,
            target_height=self.source_configuration.height,
            jpeg_quality=self.source_configuration.jpeg_quality,
        )

        if not jpeg_bytes:
            logger.info("Failed to convert I420 frame to JPEG for participant %s (share_source_id %s)", self.participant_id, self.share_source_id)
            return

        self._last_sent_timestamp_ns = now_ns

        self.owner._emit_frame(jpeg_bytes, self.participant_id, self.source)

    def cleanup(self):
        if self.destroyed:
            return

        logger.info("Cleaning up subscription for participant %s (share_source_id %s)", self.participant_id, self.share_source_id)
        try:
            self._renderer.unSubscribe()
        except Exception:
            logger.exception(
                "Error while unsubscribing renderer for participant %s",
                self.participant_id,
            )
        self.destroyed = True
