import asyncio
import datetime
import json
import logging
import os
import threading
import time
from time import sleep

import numpy as np
import requests
from pyvirtualdisplay import Display
from selenium import webdriver
from websockets.sync.server import serve

from bots.bot_adapter import BotAdapter
from bots.bot_controller.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.models import RecordingViews
from bots.utils import half_ceil, scale_i420

from .debug_screen_recorder import DebugScreenRecorder
from .ui_methods import UiMeetingNotFoundException, UiRequestToJoinDeniedException, UiRetryableException, UiRetryableExpectedException

logger = logging.getLogger(__name__)


class WebBotAdapter(BotAdapter):
    def __init__(
        self,
        *,
        display_name,
        send_message_callback,
        meeting_url,
        add_video_frame_callback,
        wants_any_video_frames_callback,
        add_mixed_audio_chunk_callback,
        add_encoded_mp4_chunk_callback,
        upsert_caption_callback,
        automatic_leave_configuration: AutomaticLeaveConfiguration,
        recording_view: RecordingViews,
        should_create_debug_recording: bool,
        start_recording_screen_callback,
        stop_recording_screen_callback,
    ):
        self.display_name = display_name
        self.send_message_callback = send_message_callback
        self.add_mixed_audio_chunk_callback = add_mixed_audio_chunk_callback
        self.add_video_frame_callback = add_video_frame_callback
        self.wants_any_video_frames_callback = wants_any_video_frames_callback
        self.add_encoded_mp4_chunk_callback = add_encoded_mp4_chunk_callback
        self.upsert_caption_callback = upsert_caption_callback
        self.start_recording_screen_callback = start_recording_screen_callback
        self.stop_recording_screen_callback = stop_recording_screen_callback
        self.recording_view = recording_view

        self.meeting_url = meeting_url

        self.video_frame_size = (1920, 1080)

        self.driver = None

        self.send_frames = True

        self.left_meeting = False
        self.was_removed_from_meeting = False
        self.cleaned_up = False

        self.websocket_port = None
        self.websocket_server = None
        self.websocket_thread = None
        self.last_websocket_message_processed_time = None
        self.last_media_message_processed_time = None
        self.last_audio_message_processed_time = None
        self.first_buffer_timestamp_ms_offset = time.time() * 1000
        self.media_sending_enable_timestamp_ms = None

        self.participants_info = {}
        self.only_one_participant_in_meeting_at = None
        self.video_frame_ticker = 0

        self.automatic_leave_configuration = automatic_leave_configuration

        self.should_create_debug_recording = should_create_debug_recording
        self.debug_screen_recorder = None

        self.silence_detection_activated = False
        self.joined_at = None

    def process_encoded_mp4_chunk(self, message):
        self.last_media_message_processed_time = time.time()
        if len(message) > 4:
            encoded_mp4_data = message[4:]
            logger.info(f"encoded mp4 data length {len(encoded_mp4_data)}")
            self.add_encoded_mp4_chunk_callback(encoded_mp4_data)

    def get_participant(self, participant_id):
        if participant_id in self.participants_info:
            return {
                "participant_uuid": participant_id,
                "participant_full_name": self.participants_info[participant_id]["fullName"],
                "participant_user_uuid": None,
            }

        return None

    def process_video_frame(self, message):
        self.last_media_message_processed_time = time.time()
        if len(message) > 24:  # Minimum length check
            # Bytes 4-12 contain the timestamp
            timestamp = int.from_bytes(message[4:12], byteorder="little")

            # Get stream ID length and string
            stream_id_length = int.from_bytes(message[12:16], byteorder="little")
            message[16 : 16 + stream_id_length].decode("utf-8")

            # Get width and height after stream ID
            offset = 16 + stream_id_length
            width = int.from_bytes(message[offset : offset + 4], byteorder="little")
            height = int.from_bytes(message[offset + 4 : offset + 8], byteorder="little")

            # Keep track of the video frame dimensions
            if self.video_frame_ticker % 300 == 0:
                logger.info(f"video dimensions {width} {height} message length {len(message) - offset - 8}")
            self.video_frame_ticker += 1

            # Scale frame to 1920x1080
            expected_video_data_length = width * height + 2 * half_ceil(width) * half_ceil(height)
            video_data = np.frombuffer(message[offset + 8 :], dtype=np.uint8)

            # Check if len(video_data) does not agree with width and height
            if len(video_data) == expected_video_data_length:  # I420 format uses 1.5 bytes per pixel
                scaled_i420_frame = scale_i420(video_data, (width, height), (1920, 1080))
                if self.wants_any_video_frames_callback() and self.send_frames:
                    self.add_video_frame_callback(scaled_i420_frame, timestamp * 1000)

            else:
                logger.info(f"video data length does not agree with width and height {len(video_data)} {width} {height}")

    def process_audio_frame(self, message):
        self.last_media_message_processed_time = time.time()
        if len(message) > 12:
            # Bytes 4-12 contain the timestamp
            timestamp = int.from_bytes(message[4:12], byteorder="little")

            # Bytes 12-16 contain the stream ID
            stream_id = int.from_bytes(message[12:16], byteorder="little")

            # Convert the float32 audio data to numpy array
            audio_data = np.frombuffer(message[16:], dtype=np.float32)

            # Only mark last_audio_message_processed_time if the audio data has at least one non-zero value
            if np.any(audio_data):
                self.last_audio_message_processed_time = time.time()

            if self.wants_any_video_frames_callback() and self.send_frames:
                self.add_mixed_audio_chunk_callback(audio_data.tobytes(), timestamp * 1000, stream_id % 3)

    def handle_websocket(self, websocket):
        audio_format = None
        output_dir = "frames"  # Add output directory

        # Create frames directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        try:
            for message in websocket:
                # Get first 4 bytes as message type
                message_type = int.from_bytes(message[:4], byteorder="little")

                if message_type == 1:  # JSON
                    json_data = json.loads(message[4:].decode("utf-8"))
                    logger.info("Received JSON message: %s", json_data)

                    # Handle audio format information
                    if isinstance(json_data, dict):
                        if json_data.get("type") == "AudioFormatUpdate":
                            audio_format = json_data["format"]
                            logger.info(f"audio format {audio_format}")

                        elif json_data.get("type") == "CaptionUpdate":
                            self.upsert_caption_callback(json_data["caption"])

                        elif json_data.get("type") == "UsersUpdate":
                            for user in json_data["newUsers"]:
                                user["active"] = user["humanized_status"] == "in_meeting"
                                self.participants_info[user["deviceId"]] = user
                            for user in json_data["removedUsers"]:
                                user["active"] = False
                                self.participants_info[user["deviceId"]] = user
                            for user in json_data["updatedUsers"]:
                                user["active"] = user["humanized_status"] == "in_meeting"
                                self.participants_info[user["deviceId"]] = user

                                if user["humanized_status"] == "removed_from_meeting" and user["fullName"] == self.display_name:
                                    # if this is the only participant with that name in the meeting, then we can assume that it was us who was removed
                                    if len([x for x in self.participants_info.values() if x["fullName"] == self.display_name]) == 1:
                                        self.was_removed_from_meeting = True
                                        self.send_message_callback({"message": self.Messages.MEETING_ENDED})

                            all_participants_in_meeting = [x for x in self.participants_info.values() if x["active"]]
                            if len(all_participants_in_meeting) == 1 and all_participants_in_meeting[0]["fullName"] == self.display_name:
                                if self.only_one_participant_in_meeting_at is None:
                                    self.only_one_participant_in_meeting_at = time.time()
                            else:
                                self.only_one_participant_in_meeting_at = None

                        elif json_data.get("type") == "SilenceStatus":
                            if not json_data.get("isSilent"):
                                self.last_audio_message_processed_time = time.time()

                elif message_type == 2:  # VIDEO
                    self.process_video_frame(message)
                elif message_type == 3:  # AUDIO
                    self.process_audio_frame(message)
                elif message_type == 4:  # ENCODED_MP4_CHUNK
                    self.process_encoded_mp4_chunk(message)

                self.last_websocket_message_processed_time = time.time()
        except Exception as e:
            logger.info(f"Websocket error: {e}")

    def run_websocket_server(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        port = self.get_websocket_port()
        max_retries = 10

        for attempt in range(max_retries):
            try:
                self.websocket_server = serve(
                    self.handle_websocket,
                    "localhost",
                    port,
                    compression=None,
                    max_size=None,
                )
                logger.info(f"Websocket server started on ws://localhost:{port}")
                self.websocket_port = port
                self.websocket_server.serve_forever()
                break
            except OSError as e:
                if e.errno == 98:  # Address already in use
                    logger.info(f"Port {port} is already in use, trying next port...")
                    port += 1
                    if attempt == max_retries - 1:
                        raise Exception(f"Could not find available port after {max_retries} attempts")
                    continue
                raise  # Re-raise other OSErrors

    def send_request_to_join_denied_message(self):
        self.send_message_callback({"message": self.Messages.REQUEST_TO_JOIN_DENIED})

    def send_meeting_not_found_message(self):
        self.send_message_callback({"message": self.Messages.MEETING_NOT_FOUND})

    def send_debug_screenshot_message(self, step, exception, inner_exception):
        """
        Send a debug screenshot message to the callback function.

        Args:
            step: at which step the exception occurred.
            exception: the exception that occurred.
            inner_exception:

        Returns: None

        """
        current_time = datetime.datetime.now()
        timestamp = current_time.strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"/tmp/ui_element_not_found_{timestamp}.png"
        try:
            self.driver.save_screenshot(screenshot_path)
        except Exception as e:
            logger.info(f"Error saving screenshot: {e}")
            screenshot_path = None

        mhtml_file_path = f"/tmp/page_snapshot_{timestamp}.mhtml"
        try:
            result = self.driver.execute_cdp_cmd("Page.captureSnapshot", {})
            mhtml_bytes = result["data"]  # Extract the data from the response dictionary
            with open(mhtml_file_path, "w", encoding="utf-8") as f:
                f.write(mhtml_bytes)
        except Exception as e:
            logger.info(f"Error saving mhtml: {e}")
            mhtml_file_path = None

        self.send_message_callback(
            {
                "message": self.Messages.UI_ELEMENT_NOT_FOUND,
                "step": step,
                "current_time": current_time,
                "mhtml_file_path": mhtml_file_path,
                "screenshot_path": screenshot_path,
                "exception_type": exception.__class__.__name__ if exception else "exception_not_available",
                "exception_message": exception.__str__() if exception else "exception_message_not_available",
                "inner_exception_type": inner_exception.__class__.__name__ if inner_exception else "inner_exception_not_available",
                "inner_exception_message": inner_exception.__str__() if inner_exception else "inner_exception_message_not_available",
            }
        )

    def init_driver(self):
        options = webdriver.ChromeOptions()

        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--use-fake-device-for-media-stream")
        options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(f"--window-size={self.video_frame_size[0]},{self.video_frame_size[1]}")
        options.add_argument("--no-sandbox")
        options.add_argument("--start-fullscreen")
        # options.add_argument('--headless=new')
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        if self.driver:
            # Simulate closing browser window
            try:
                self.driver.close()
            except Exception as e:
                logger.info(f"Error closing driver: {e}")

            try:
                self.driver.quit()
            except Exception as e:
                logger.info(f"Error closing existing driver: {e}")
            self.driver = None

        self.driver = webdriver.Chrome(options=options)
        logger.info(f"web driver server initialized at port {self.driver.service.port}")

        initial_data_code = f"window.initialData = {{websocketPort: {self.websocket_port}, addClickRipple: {'true' if self.should_create_debug_recording else 'false'}, recordingView: '{self.recording_view}'}}"

        # Define the CDN libraries needed
        CDN_LIBRARIES = ["https://cdnjs.cloudflare.com/ajax/libs/protobufjs/7.4.0/protobuf.min.js", "https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js"]

        # Download all library code
        libraries_code = ""
        for url in CDN_LIBRARIES:
            response = requests.get(url)
            if response.status_code == 200:
                libraries_code += response.text + "\n"
            else:
                raise Exception(f"Failed to download library from {url}")

        # Get directory of current file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # Read your payload using path relative to current file
        with open(os.path.join(current_dir, "..", self.get_chromedriver_payload_file_name()), "r") as file:
            payload_code = file.read()

        # Combine them ensuring libraries load first
        combined_code = f"""
            {initial_data_code}
            {libraries_code}
            {payload_code}
        """

        # Add the combined script to execute on new document
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": combined_code})

    def init(self):
        self.display_var_for_debug_recording = os.environ.get("DISPLAY")
        if os.environ.get("DISPLAY") is None:
            # Create virtual display only if no real display is available
            self.display = Display(visible=0, size=(1930, 1090))
            self.display.start()
            self.display_var_for_debug_recording = self.display.new_display_var

        if self.should_create_debug_recording:
            self.debug_screen_recorder = DebugScreenRecorder(self.display_var_for_debug_recording, self.video_frame_size, BotAdapter.DEBUG_RECORDING_FILE_PATH)
            self.debug_screen_recorder.start()

        # Start websocket server in a separate thread
        websocket_thread = threading.Thread(target=self.run_websocket_server, daemon=True)
        websocket_thread.start()

        sleep(0.5)  # Give the websocketserver time to start
        if not self.websocket_port:
            raise Exception("WebSocket server failed to start")

        repeatedly_attempt_to_join_meeting_thread = threading.Thread(target=self.repeatedly_attempt_to_join_meeting, daemon=True)
        repeatedly_attempt_to_join_meeting_thread.start()

    def repeatedly_attempt_to_join_meeting(self):
        logger.info(f"Trying to join meeting at {self.meeting_url}")

        # Expected exceptions are ones that we expect to happen and are not a big deal, so we only increment num_retries once every three expected exceptions
        num_expected_exceptions = 0
        num_retries = 0
        max_retries = 3
        while num_retries <= max_retries:
            logger.info(f"Trying to join meeting for the {num_retries + 1} time.")
            try:
                self.init_driver()
                self.attempt_to_join_meeting()
                logger.info("Successfully joined meeting")
                break

            except UiRequestToJoinDeniedException:
                self.send_request_to_join_denied_message()
                return

            except UiMeetingNotFoundException:
                self.send_meeting_not_found_message()
                return

            except UiRetryableExpectedException as e:
                if num_retries >= max_retries:
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is retryable but the number of retries exceeded the limit and there were {num_expected_exceptions} expected exceptions, so returning")
                    self.send_debug_screenshot_message(step=e.step, exception=e, inner_exception=e.inner_exception)
                    return

                num_expected_exceptions += 1
                if num_expected_exceptions % 3 == 0:
                    num_retries += 1
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is expected and {num_expected_exceptions} expected exceptions have occurred, so incrementing num_retries. This usually indicates that the meeting has not started yet, so we will wait for the configured amount of time which is {self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds} seconds before retrying")
                    sleep(self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds)
                else:
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is expected so not incrementing num_retries, but {num_expected_exceptions} expected exceptions have occurred")

            except UiRetryableException as e:
                if num_retries >= max_retries:
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is retryable but the number of retries exceeded the limit, so returning")
                    self.send_debug_screenshot_message(step=e.step, exception=e, inner_exception=e.inner_exception)
                    return

                if self.left_meeting or self.cleaned_up:
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is retryable but the bot has left the meeting or cleaned up, so returning")
                    return

                logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is retryable so retrying")

                num_retries += 1

            except Exception as e:
                logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is not retryable so returning")
                self.send_debug_screenshot_message(step="unknown", exception=e, inner_exception="None")
                return

            sleep(1)

        self.send_message_callback({"message": self.Messages.BOT_JOINED_MEETING})
        self.send_message_callback({"message": self.Messages.BOT_RECORDING_PERMISSION_GRANTED})

        self.send_frames = True
        self.driver.execute_script("window.ws?.enableMediaSending();")
        self.first_buffer_timestamp_ms_offset = self.driver.execute_script("return performance.timeOrigin;")
        self.joined_at = time.time()

        if self.start_recording_screen_callback:
            sleep(2)
            if self.debug_screen_recorder:
                self.debug_screen_recorder.stop()
            self.start_recording_screen_callback(self.display_var_for_debug_recording)

        self.media_sending_enable_timestamp_ms = time.time() * 1000

    def leave(self):
        if self.left_meeting:
            return
        if self.was_removed_from_meeting:
            return

        try:
            logger.info("disable media sending")
            self.driver.execute_script("window.ws?.disableMediaSending();")

            self.click_leave_button()
        except Exception as e:
            logger.info(f"Error during leave: {e}")
        finally:
            self.send_message_callback({"message": self.Messages.MEETING_ENDED})
            self.left_meeting = True

    def cleanup(self):
        if self.stop_recording_screen_callback:
            self.stop_recording_screen_callback()

        try:
            logger.info("disable media sending")
            self.driver.execute_script("window.ws?.disableMediaSending();")
        except Exception as e:
            logger.info(f"Error during media sending disable: {e}")

        # Wait for websocket buffers to be processed
        if self.last_websocket_message_processed_time:
            time_when_shutdown_initiated = time.time()
            while time.time() - self.last_websocket_message_processed_time < 2 and time.time() - time_when_shutdown_initiated < 30:
                logger.info(f"Waiting until it's 2 seconds since last websockets message was processed or 30 seconds have passed. Currently it is {time.time() - self.last_websocket_message_processed_time} seconds and {time.time() - time_when_shutdown_initiated} seconds have passed")
                sleep(0.5)

        try:
            if self.driver:
                # Simulate closing browser window
                try:
                    self.driver.close()
                except Exception as e:
                    logger.info(f"Error closing driver: {e}")

                # Then quit the driver
                try:
                    self.driver.quit()
                except Exception as e:
                    logger.info(f"Error quitting driver: {e}")
        except Exception as e:
            logger.info(f"Error during cleanup: {e}")

        if self.debug_screen_recorder:
            self.debug_screen_recorder.stop()

        # Properly shutdown the websocket server
        if self.websocket_server:
            try:
                self.websocket_server.shutdown()
            except Exception as e:
                logger.info(f"Error shutting down websocket server: {e}")

        self.cleaned_up = True

    def get_first_buffer_timestamp_ms_offset(self):
        return self.first_buffer_timestamp_ms_offset

    def check_auto_leave_conditions(self) -> None:
        if self.left_meeting:
            return
        if self.cleaned_up:
            return

        if self.only_one_participant_in_meeting_at is not None:
            if time.time() - self.only_one_participant_in_meeting_at > self.automatic_leave_configuration.only_participant_in_meeting_threshold_seconds:
                logger.info(f"Auto-leaving meeting because there was only one participant in the meeting for {self.automatic_leave_configuration.only_participant_in_meeting_threshold_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING})
                return

        if not self.silence_detection_activated and self.joined_at is not None and time.time() - self.joined_at > self.automatic_leave_configuration.silence_activate_after_seconds:
            self.silence_detection_activated = True
            self.last_audio_message_processed_time = time.time()
            logger.info(f"Silence detection activated after {self.automatic_leave_configuration.silence_activate_after_seconds} seconds")

        if self.last_audio_message_processed_time is not None and self.silence_detection_activated:
            if time.time() - self.last_audio_message_processed_time > self.automatic_leave_configuration.silence_threshold_seconds:
                logger.info(f"Auto-leaving meeting because there was no audio for {self.automatic_leave_configuration.silence_threshold_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_SILENCE})
                return

    def ready_to_show_bot_image(self):
        self.send_message_callback({"message": self.Messages.READY_TO_SHOW_BOT_IMAGE})

    def send_raw_audio(self, bytes, sample_rate):
        logger.info("send_raw_audio not supported in google meet bots")

    def send_raw_image(self, image_bytes):
        logger.info("send_raw_image not supported in google meet bots")
