import asyncio
import copy
import datetime
import hashlib
import json
import logging
import os
import threading
import time
from time import sleep

import numpy as np
import requests
from django.conf import settings
from pyvirtualdisplay import Display
from selenium import webdriver
from websockets.sync.server import serve

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.bot_adapter import BotAdapter
from bots.models import ParticipantEventTypes, RecordingViews
from bots.utils import half_ceil, scale_i420

from .debug_screen_recorder import DebugScreenRecorder
from .ui_methods import UiCouldNotJoinMeetingWaitingForHostException, UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiIncorrectPasswordException, UiLoginAttemptFailedException, UiLoginRequiredException, UiMeetingNotFoundException, UiRequestToJoinDeniedException, UiRetryableException, UiRetryableExpectedException

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
        add_audio_chunk_callback,
        add_mixed_audio_chunk_callback,
        add_encoded_mp4_chunk_callback,
        upsert_caption_callback,
        upsert_chat_message_callback,
        add_participant_event_callback,
        automatic_leave_configuration: AutomaticLeaveConfiguration,
        recording_view: RecordingViews,
        should_create_debug_recording: bool,
        start_recording_screen_callback,
        stop_recording_screen_callback,
        video_frame_size: tuple[int, int],
        record_chat_messages_when_paused: bool,
        disable_incoming_video: bool,
    ):
        self.display_name = display_name
        self.send_message_callback = send_message_callback
        self.add_audio_chunk_callback = add_audio_chunk_callback
        self.add_mixed_audio_chunk_callback = add_mixed_audio_chunk_callback
        self.add_video_frame_callback = add_video_frame_callback
        self.wants_any_video_frames_callback = wants_any_video_frames_callback
        self.add_encoded_mp4_chunk_callback = add_encoded_mp4_chunk_callback
        self.upsert_caption_callback = upsert_caption_callback
        self.upsert_chat_message_callback = upsert_chat_message_callback
        self.add_participant_event_callback = add_participant_event_callback
        self.start_recording_screen_callback = start_recording_screen_callback
        self.stop_recording_screen_callback = stop_recording_screen_callback
        self.recording_view = recording_view
        self.record_chat_messages_when_paused = record_chat_messages_when_paused
        self.disable_incoming_video = disable_incoming_video
        self.meeting_url = meeting_url

        # This is an internal ID that comes from the platform. It is currently only used for MS Teams.
        self.meeting_uuid = None

        self.video_frame_size = video_frame_size

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
        self.recording_permission_granted_at = None

        self.ready_to_send_chat_messages = False

        self.recording_paused = False

    def pause_recording(self):
        self.recording_paused = True

    def start_or_resume_recording(self):
        self.recording_paused = False

    def process_encoded_mp4_chunk(self, message):
        if self.recording_paused:
            return

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
                "participant_is_the_bot": self.participants_info[participant_id]["isCurrentUser"],
                "participant_is_host": self.participants_info[participant_id].get("isHost", False),
            }

        return None

    def meeting_uuid_mismatch(self, user):
        # If no meeting id was provided, then don't try to detect a mismatch
        if not user.get("meetingId"):
            return False

        # If the meeting uuid is not set, then set it to the user's meeting id
        if not self.meeting_uuid:
            self.meeting_uuid = user.get("meetingId")
            logger.info(f"meeting_uuid set to {self.meeting_uuid} for user {user}")
            return False

        if self.meeting_uuid != user.get("meetingId"):
            logger.info(f"meeting_uuid mismatch detected. meeting_uuid: {self.meeting_uuid} user_meeting_id: {user.get('meetingId')} for user {user}")
            return True

        return False

    def handle_participant_update(self, user):
        if self.meeting_uuid_mismatch(user):
            return

        user_before = self.participants_info.get(user["deviceId"], {"active": False})
        self.participants_info[user["deviceId"]] = user

        if user_before.get("active") and not user["active"]:
            self.add_participant_event_callback({"participant_uuid": user["deviceId"], "event_type": ParticipantEventTypes.LEAVE, "event_data": {}, "timestamp_ms": int(time.time() * 1000)})
            return

        if not user_before.get("active") and user["active"]:
            self.add_participant_event_callback({"participant_uuid": user["deviceId"], "event_type": ParticipantEventTypes.JOIN, "event_data": {}, "timestamp_ms": int(time.time() * 1000)})
            return

        if bool(user_before.get("isHost")) != bool(user.get("isHost")):
            changes = {
                "isHost": {
                    "before": user_before.get("isHost"),
                    "after": user.get("isHost"),
                }
            }
            self.add_participant_event_callback({"participant_uuid": user["deviceId"], "event_type": ParticipantEventTypes.UPDATE, "event_data": changes, "timestamp_ms": int(time.time() * 1000)})
            return

    def process_video_frame(self, message):
        if self.recording_paused:
            return

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

            # Scale frame to video frame size
            expected_video_data_length = width * height + 2 * half_ceil(width) * half_ceil(height)
            video_data = np.frombuffer(message[offset + 8 :], dtype=np.uint8)

            # Check if len(video_data) does not agree with width and height
            if len(video_data) == expected_video_data_length:  # I420 format uses 1.5 bytes per pixel
                scaled_i420_frame = scale_i420(video_data, (width, height), self.video_frame_size)
                if self.wants_any_video_frames_callback() and self.send_frames:
                    self.add_video_frame_callback(scaled_i420_frame, timestamp * 1000)

            else:
                logger.info(f"video data length does not agree with width and height {len(video_data)} {width} {height}")

    def process_mixed_audio_frame(self, message):
        if self.recording_paused:
            return

        self.last_media_message_processed_time = time.time()
        if len(message) > 12:
            # Convert the float32 audio data to numpy array
            audio_data = np.frombuffer(message[4:], dtype=np.float32)

            # Convert float32 to PCM 16-bit by multiplying by 32768.0
            audio_data = (audio_data * 32768.0).astype(np.int16)

            # Only mark last_audio_message_processed_time if the audio data has at least one non-zero value
            if np.any(audio_data):
                self.last_audio_message_processed_time = time.time()

            if (self.wants_any_video_frames_callback is None or self.wants_any_video_frames_callback()) and self.send_frames:
                self.add_mixed_audio_chunk_callback(chunk=audio_data.tobytes())

    def process_per_participant_audio_frame(self, message):
        if self.recording_paused:
            return

        self.last_media_message_processed_time = time.time()
        if len(message) > 12:
            # Byte 5 contains the participant ID length
            participant_id_length = int.from_bytes(message[4:5], byteorder="little")
            participant_id = message[5 : 5 + participant_id_length].decode("utf-8")

            # Convert the float32 audio data to numpy array
            audio_data = np.frombuffer(message[(5 + participant_id_length) :], dtype=np.float32)

            # Convert float32 to PCM 16-bit by multiplying by 32768.0
            audio_data = (audio_data * 32768.0).astype(np.int16)

            self.add_audio_chunk_callback(participant_id, datetime.datetime.utcnow(), audio_data.tobytes())

    def update_only_one_participant_in_meeting_at(self):
        if not self.joined_at:
            return

        # If nobody other than the bot was ever in the meeting, then don't activate this. We only want to activate if someone else was in the meeting and left
        if len(self.participants_info) <= 1:
            return

        all_participants_in_meeting = [x for x in self.participants_info.values() if x["active"]]
        if len(all_participants_in_meeting) == 1 and all_participants_in_meeting[0]["fullName"] == self.display_name:
            if self.only_one_participant_in_meeting_at is None:
                self.only_one_participant_in_meeting_at = time.time()
                logger.info(f"only_one_participant_in_meeting_at set to {self.only_one_participant_in_meeting_at}")
        else:
            self.only_one_participant_in_meeting_at = None

    def handle_removed_from_meeting(self):
        self.left_meeting = True
        self.send_message_callback({"message": self.Messages.MEETING_ENDED})

    def handle_meeting_ended(self):
        self.left_meeting = True
        self.send_message_callback({"message": self.Messages.MEETING_ENDED})

    def handle_failed_to_join(self, reason):
        logger.info(f"failed to join meeting with reason {reason}")
        self.subclass_specific_handle_failed_to_join(reason)

    def handle_caption_update(self, json_data):
        if self.recording_paused:
            return

        # Count a caption as audio activity
        self.last_audio_message_processed_time = time.time()
        self.upsert_caption_callback(json_data["caption"])

    def handle_chat_message(self, json_data):
        if self.recording_paused and not self.record_chat_messages_when_paused:
            return

        self.upsert_chat_message_callback(json_data)

    def mask_transcript_if_required(self, json_data):
        if not settings.MASK_TRANSCRIPT_IN_LOGS:
            return json_data

        json_data_masked = copy.deepcopy(json_data)
        if json_data.get("caption") and json_data.get("caption").get("text"):
            json_data_masked["caption"]["text"] = hashlib.sha256(json_data.get("caption").get("text").encode("utf-8")).hexdigest()
        return json_data_masked

    def handle_websocket(self, websocket):
        audio_format = None

        try:
            for message in websocket:
                # Get first 4 bytes as message type
                message_type = int.from_bytes(message[:4], byteorder="little")

                if message_type == 1:  # JSON
                    json_data = json.loads(message[4:].decode("utf-8"))
                    if json_data.get("type") == "CaptionUpdate":
                        logger.info("Received JSON message: %s", self.mask_transcript_if_required(json_data))
                    else:
                        logger.info("Received JSON message: %s", json_data)

                    # Handle audio format information
                    if isinstance(json_data, dict):
                        if json_data.get("type") == "AudioFormatUpdate":
                            audio_format = json_data["format"]
                            logger.info(f"audio format {audio_format}")

                        elif json_data.get("type") == "CaptionUpdate":
                            self.handle_caption_update(json_data)

                        elif json_data.get("type") == "ChatMessage":
                            self.handle_chat_message(json_data)

                        elif json_data.get("type") == "UsersUpdate":
                            for user in json_data["newUsers"]:
                                user["active"] = user["humanized_status"] == "in_meeting"
                                self.handle_participant_update(user)
                            for user in json_data["removedUsers"]:
                                user["active"] = False
                                self.handle_participant_update(user)
                            for user in json_data["updatedUsers"]:
                                user["active"] = user["humanized_status"] == "in_meeting"
                                self.handle_participant_update(user)

                                if user["humanized_status"] == "removed_from_meeting" and user["fullName"] == self.display_name:
                                    # if this is the only participant with that name in the meeting, then we can assume that it was us who was removed
                                    if len([x for x in self.participants_info.values() if x["fullName"] == self.display_name]) == 1:
                                        self.handle_removed_from_meeting()

                            self.update_only_one_participant_in_meeting_at()

                        elif json_data.get("type") == "SilenceStatus":
                            if not json_data.get("isSilent"):
                                self.last_audio_message_processed_time = time.time()

                        elif json_data.get("type") == "ChatStatusChange":
                            if json_data.get("change") == "ready_to_send":
                                self.ready_to_send_chat_messages = True
                                self.send_message_callback({"message": self.Messages.READY_TO_SEND_CHAT_MESSAGE})

                        elif json_data.get("type") == "MeetingStatusChange":
                            if json_data.get("change") == "removed_from_meeting":
                                self.handle_removed_from_meeting()
                            if json_data.get("change") == "meeting_ended":
                                self.handle_meeting_ended()
                            if json_data.get("change") == "failed_to_join":
                                self.handle_failed_to_join(json_data.get("reason"))

                        elif json_data.get("type") == "RecordingPermissionChange":
                            if json_data.get("change") == "granted":
                                self.after_bot_can_record_meeting()
                            elif json_data.get("change") == "denied":
                                self.after_bot_recording_permission_denied()

                elif message_type == 2:  # VIDEO
                    self.process_video_frame(message)
                elif message_type == 3:  # AUDIO
                    self.process_mixed_audio_frame(message)
                elif message_type == 4:  # ENCODED_MP4_CHUNK
                    self.process_encoded_mp4_chunk(message)
                elif message_type == 5:  # PER_PARTICIPANT_AUDIO
                    self.process_per_participant_audio_frame(message)

                self.last_websocket_message_processed_time = time.time()
        except Exception as e:
            logger.info(f"Websocket error: {e}")
            raise e

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

    def send_login_required_message(self):
        self.send_message_callback({"message": self.Messages.LOGIN_REQUIRED})

    def capture_screenshot_and_mhtml_file(self):
        # Take a screenshot and mhtml file of the page, because it is helpful to have for debugging
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

        return screenshot_path, mhtml_file_path, current_time

    def send_login_attempt_failed_message(self):
        screenshot_path, mhtml_file_path, current_time = self.capture_screenshot_and_mhtml_file()

        self.send_message_callback(
            {
                "message": self.Messages.LOGIN_ATTEMPT_FAILED,
                "mhtml_file_path": mhtml_file_path,
                "screenshot_path": screenshot_path,
            }
        )

    def send_incorrect_password_message(self):
        self.send_message_callback({"message": self.Messages.COULD_NOT_CONNECT_TO_MEETING})

    def send_debug_screenshot_message(self, step, exception, inner_exception):
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

    def add_subclass_specific_chrome_options(self, options):
        pass

    def init_driver(self):
        options = webdriver.ChromeOptions()

        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--use-fake-device-for-media-stream")
        options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(f"--window-size={self.video_frame_size[0]},{self.video_frame_size[1]}")
        options.add_argument("--start-fullscreen")
        # options.add_argument('--headless=new')
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        if os.getenv("ENABLE_CHROME_SANDBOX", "false").lower() != "true":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-setuid-sandbox")
            logger.info("Chrome sandboxing is disabled")
        else:
            logger.info("Chrome sandboxing is enabled")

        prefs = {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        options.add_experimental_option("prefs", prefs)

        self.add_subclass_specific_chrome_options(options)

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

        initial_data_code = f"window.initialData = {{websocketPort: {self.websocket_port}, videoFrameWidth: {self.video_frame_size[0]}, videoFrameHeight: {self.video_frame_size[1]}, botName: {json.dumps(self.display_name)}, addClickRipple: {'true' if self.should_create_debug_recording else 'false'}, recordingView: '{self.recording_view}', sendMixedAudio: {'true' if self.add_mixed_audio_chunk_callback else 'false'}, sendPerParticipantAudio: {'true' if self.add_audio_chunk_callback else 'false'}, collectCaptions: {'true' if self.upsert_caption_callback else 'false'}}}"

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

        # Read shared_chromedriver_payload.js
        with open(os.path.join(current_dir, "shared_chromedriver_payload.js"), "r") as file:
            shared_chromedriver_payload_code = file.read()

        # Combine them ensuring libraries load first
        combined_code = f"""
            {initial_data_code}
            {self.subclass_specific_initial_data_code()}
            {libraries_code}
            {shared_chromedriver_payload_code}
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

    def should_retry_joining_meeting_that_requires_login_by_logging_in(self):
        return False

    def repeatedly_attempt_to_join_meeting(self):
        logger.info(f"Trying to join meeting at {self.meeting_url}")

        # Expected exceptions are ones that we expect to happen and are not a big deal, so we only increment num_retries once every three expected exceptions
        num_expected_exceptions = 0
        num_retries = 0
        max_retries = 3
        while num_retries <= max_retries:
            try:
                self.init_driver()
                self.attempt_to_join_meeting()
                logger.info("Successfully joined meeting")
                break

            except UiLoginRequiredException:
                if not self.should_retry_joining_meeting_that_requires_login_by_logging_in():
                    self.send_login_required_message()
                    return

            except UiLoginAttemptFailedException:
                self.send_login_attempt_failed_message()
                return

            except UiRequestToJoinDeniedException:
                self.send_request_to_join_denied_message()
                return

            except UiCouldNotJoinMeetingWaitingRoomTimeoutException:
                self.send_message_callback({"message": self.Messages.LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED})
                return

            except UiCouldNotJoinMeetingWaitingForHostException:
                self.send_message_callback({"message": self.Messages.LEAVE_MEETING_WAITING_FOR_HOST})
                return

            except UiMeetingNotFoundException:
                self.send_meeting_not_found_message()
                return

            except UiIncorrectPasswordException:
                self.send_incorrect_password_message()
                return

            except UiRetryableExpectedException as e:
                if num_retries >= max_retries:
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is retryable but the number of retries exceeded the limit and there were {num_expected_exceptions} expected exceptions, so returning")
                    self.send_debug_screenshot_message(step=e.step, exception=e, inner_exception=e.inner_exception)
                    return

                num_expected_exceptions += 1
                if num_expected_exceptions % 5 == 0:
                    num_retries += 1
                    logger.info(f"Failed to join meeting and the {e.__class__.__name__} exception is expected and {num_expected_exceptions} expected exceptions have occurred, so incrementing num_retries. This usually indicates that the meeting has not started yet, so we will wait for the configured amount of time which is 180 seconds before retrying")
                    # We're going to start a new pod to see if that fixes the issue
                    self.send_message_callback({"message": self.Messages.BLOCKED_BY_PLATFORM_REPEATEDLY})
                    return
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
                if num_retries >= max_retries:
                    logger.exception(f"Failed to join meeting and the unexpected {e.__class__.__name__} exception with message {e.__str__()} is retryable but the number of retries exceeded the limit, so returning.")
                    self.send_debug_screenshot_message(step="unknown", exception=e, inner_exception=None)
                    return

                if self.left_meeting or self.cleaned_up:
                    logger.exception(f"Failed to join meeting and the unexpected {e.__class__.__name__} exception with message {e.__str__()} is retryable but the bot has left the meeting or cleaned up, so returning.")
                    return

                logger.exception(f"Failed to join meeting and the unexpected {e.__class__.__name__} exception with message {e.__str__()} is retryable so retrying")

                num_retries += 1

            sleep(1)

        self.after_bot_joined_meeting()
        self.subclass_specific_after_bot_joined_meeting()

    def after_bot_joined_meeting(self):
        self.send_message_callback({"message": self.Messages.BOT_JOINED_MEETING})
        self.joined_at = time.time()
        self.update_only_one_participant_in_meeting_at()

    def after_bot_recording_permission_denied(self):
        self.send_message_callback({"message": self.Messages.BOT_RECORDING_PERMISSION_DENIED, "denied_reason": BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION})

    def after_bot_can_record_meeting(self):
        if self.recording_permission_granted_at is not None:
            return

        self.recording_permission_granted_at = time.time()
        self.send_message_callback({"message": self.Messages.BOT_RECORDING_PERMISSION_GRANTED})
        self.send_frames = True
        self.driver.execute_script("window.ws?.enableMediaSending();")
        self.first_buffer_timestamp_ms_offset = self.driver.execute_script("return performance.timeOrigin;")

        if self.start_recording_screen_callback:
            sleep(2)
            if self.debug_screen_recorder:
                self.debug_screen_recorder.stop()
            self.start_recording_screen_callback(self.display_var_for_debug_recording)

        self.media_sending_enable_timestamp_ms = time.time() * 1000

        self.ready_to_show_webpage_stream()

    def leave(self):
        if self.left_meeting:
            return
        if self.was_removed_from_meeting:
            return
        if self.stop_recording_screen_callback:
            self.stop_recording_screen_callback()

        try:
            logger.info("disable media sending")
            self.driver.execute_script("window.ws?.disableMediaSending();")

            self.click_leave_button()
        except Exception as e:
            logger.info(f"Error during leave: {e}")
        finally:
            self.send_message_callback({"message": self.Messages.MEETING_ENDED})
            self.left_meeting = True

    def abort_join_attempt(self):
        try:
            self.driver.close()
        except Exception as e:
            logger.info(f"Error closing driver: {e}")

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
                    self.subclass_specific_before_driver_close()
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

    def check_auto_leave_conditions(self) -> None:
        if self.left_meeting:
            return
        if self.cleaned_up:
            return

        if self.only_one_participant_in_meeting_at is not None:
            if time.time() - self.only_one_participant_in_meeting_at > self.automatic_leave_configuration.only_participant_in_meeting_timeout_seconds:
                logger.info(f"Auto-leaving meeting because there was only one participant in the meeting for {self.automatic_leave_configuration.only_participant_in_meeting_timeout_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING})
                return

        if not self.silence_detection_activated and self.joined_at is not None and time.time() - self.joined_at > self.automatic_leave_configuration.silence_activate_after_seconds:
            self.silence_detection_activated = True
            self.last_audio_message_processed_time = time.time()
            logger.info(f"Silence detection activated after {self.automatic_leave_configuration.silence_activate_after_seconds} seconds")

        if self.last_audio_message_processed_time is not None and self.silence_detection_activated:
            if time.time() - self.last_audio_message_processed_time > self.automatic_leave_configuration.silence_timeout_seconds:
                logger.info(f"Auto-leaving meeting because there was no audio for {self.automatic_leave_configuration.silence_timeout_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_SILENCE})
                return

        if self.joined_at is not None and self.automatic_leave_configuration.max_uptime_seconds is not None:
            if time.time() - self.joined_at > self.automatic_leave_configuration.max_uptime_seconds:
                logger.info(f"Auto-leaving meeting because bot has been running for more than {self.automatic_leave_configuration.max_uptime_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_MAX_UPTIME})
                return

    def is_ready_to_send_chat_messages(self):
        return self.ready_to_send_chat_messages

    def webpage_streamer_get_peer_connection_offer(self):
        return self.driver.execute_script("return window.botOutputManager.getBotOutputPeerConnectionOffer();")

    def webpage_streamer_start_peer_connection(self, offer_response):
        self.driver.execute_script(f"window.botOutputManager.startBotOutputPeerConnection({json.dumps(offer_response)});")

    def webpage_streamer_play_bot_output_media_stream(self, output_destination):
        self.driver.execute_script(f"window.botOutputManager.playBotOutputMediaStream({json.dumps(output_destination)});")

    def webpage_streamer_stop_bot_output_media_stream(self):
        self.driver.execute_script("window.botOutputManager.stopBotOutputMediaStream();")

    def ready_to_show_bot_image(self):
        self.send_message_callback({"message": self.Messages.READY_TO_SHOW_BOT_IMAGE})

    def ready_to_show_webpage_stream(self):
        self.send_message_callback({"message": self.Messages.READY_TO_SHOW_WEBPAGE_STREAM})

    def get_first_buffer_timestamp_ms(self):
        if self.media_sending_enable_timestamp_ms is None:
            return None
        # Doing a manual offset for now to correct for the screen recorder delay. This seems to work reliably.
        return self.media_sending_enable_timestamp_ms

    def send_raw_image(self, image_bytes):
        # If we have a memoryview, convert it to bytes
        if isinstance(image_bytes, memoryview):
            image_bytes = image_bytes.tobytes()

        # Pass the raw bytes directly to JavaScript
        # The JavaScript side can convert it to appropriate format
        self.driver.execute_script(
            """
            const bytes = new Uint8Array(arguments[0]);
            window.botOutputManager.displayImage(bytes);
        """,
            list(image_bytes),
        )

    def send_raw_audio(self, bytes, sample_rate):
        """
        Sends raw audio bytes to the Google Meet call.

        :param bytes: Raw audio bytes in PCM format
        :param sample_rate: Sample rate of the audio in Hz
        """
        if not self.driver:
            print("Cannot send audio - driver not initialized")
            return

        # Convert bytes to Int16Array for JavaScript
        audio_data = np.frombuffer(bytes, dtype=np.int16).tolist()

        # Call the JavaScript function to enqueue the PCM chunk
        self.driver.execute_script(f"window.botOutputManager.playPCMAudio({audio_data}, {sample_rate})")

    def send_chat_message(self, text, to_user_uuid):
        logger.info("send_chat_message not supported in web bots")

    # Sub-classes can override this to add class-specific initial data code
    def subclass_specific_initial_data_code(self):
        return ""

    # Sub-classes can override this to add class-specific after bot joined meeting code
    def subclass_specific_after_bot_joined_meeting(self):
        pass

    # Sub-classes can override this to handle class-specific failed to join issues
    def subclass_specific_handle_failed_to_join(self, reason):
        pass

    # Sub-classes can override this to add class-specific before driver close code
    def subclass_specific_before_driver_close(self):
        pass
