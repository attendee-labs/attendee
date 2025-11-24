import time
from datetime import datetime, timedelta

import cv2
import gi
import jwt
import numpy as np
import zoom_meeting_sdk as zoom

from bots.bot_adapter import BotAdapter
from bots.meeting_url_utils import parse_zoom_join_url
from bots.utils import png_to_yuv420_frame, scale_i420

from .mp4_demuxer import MP4Demuxer
from .video_input_manager import VideoInputManager

gi.require_version("GLib", "2.0")
import logging

from gi.repository import GLib

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.models import ParticipantEventTypes

logger = logging.getLogger(__name__)


def generate_jwt(client_id, client_secret):
    iat = datetime.utcnow()
    exp = iat + timedelta(hours=24)

    payload = {
        "iat": iat,
        "exp": exp,
        "appKey": client_id,
        "tokenExp": int(exp.timestamp()),
    }

    token = jwt.encode(payload, client_secret, algorithm="HS256")
    return token


def create_black_yuv420_frame(width=640, height=360):
    # Create BGR frame (red is [0,0,0] in BGR)
    bgr_frame = np.zeros((height, width, 3), dtype=np.uint8)
    bgr_frame[:, :] = [0, 0, 0]  # Pure black in BGR

    # Convert BGR to YUV420 (I420)
    yuv_frame = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2YUV_I420)

    # Return as bytes
    return yuv_frame.tobytes()


class ZoomBotAdapter(BotAdapter):
    def __init__(
        self,
        *,
        use_one_way_audio,
        use_mixed_audio,
        use_video,
        display_name,
        send_message_callback,
        add_audio_chunk_callback,
        zoom_client_id,
        zoom_client_secret,
        meeting_url,
        add_video_frame_callback,
        wants_any_video_frames_callback,
        add_mixed_audio_chunk_callback,
        upsert_chat_message_callback,
        add_participant_event_callback,
        automatic_leave_configuration: AutomaticLeaveConfiguration,
        video_frame_size: tuple[int, int],
        zoom_tokens: dict,
        zoom_meeting_settings: dict,
        record_chat_messages_when_paused: bool,
    ):
        self.use_one_way_audio = use_one_way_audio
        self.use_mixed_audio = use_mixed_audio
        self.use_video = use_video
        self.display_name = display_name
        self.send_message_callback = send_message_callback
        self.add_audio_chunk_callback = add_audio_chunk_callback
        self.add_mixed_audio_chunk_callback = add_mixed_audio_chunk_callback
        self.add_video_frame_callback = add_video_frame_callback
        self.wants_any_video_frames_callback = wants_any_video_frames_callback
        self.upsert_chat_message_callback = upsert_chat_message_callback
        self.add_participant_event_callback = add_participant_event_callback
        self.zoom_tokens = zoom_tokens
        self.zoom_meeting_settings = zoom_meeting_settings
        self.record_chat_messages_when_paused = record_chat_messages_when_paused

        self._jwt_token = generate_jwt(zoom_client_id, zoom_client_secret)
        self.meeting_id, self.meeting_password = parse_zoom_join_url(meeting_url)

        self.meeting_service = None
        self.setting_service = None
        self.auth_service = None

        self.auth_event = None
        self.recording_event = None
        self.meeting_service_event = None

        self.audio_source = None
        self.audio_helper = None

        self.audio_settings = None

        self.use_raw_recording = True
        self.recording_permission_granted = False
        self.raw_recording_active = False

        self.reminder_controller = None

        self.recording_ctrl = None

        self.audio_raw_data_sender = None
        self.virtual_audio_mic_event_passthrough = None

        self.my_participant_id = None
        self.participants_ctrl = None
        self.meeting_reminder_event = None
        self.on_mic_start_send_callback_called = False
        self.on_virtual_camera_start_send_callback_called = False

        self.meeting_video_controller = None
        self.video_sender = None
        self.virtual_camera_video_source = None
        self.video_source_helper = None
        self.video_frame_size = video_frame_size
        self.send_image_timeout_id = None

        self.automatic_leave_configuration = automatic_leave_configuration

        self.only_one_participant_in_meeting_at = None
        self.last_audio_received_at = None
        self.silence_detection_activated = False
        self.cleaned_up = False
        self.requested_leave = False
        self.joined_at = None

        if self.use_video:
            self.video_input_manager = VideoInputManager(
                new_frame_callback=self.add_video_frame_callback,
                wants_any_frames_callback=self.wants_any_video_frames_callback,
                video_frame_size=self.video_frame_size,
            )
        else:
            self.video_input_manager = None

        self.meeting_sharing_controller = None
        self.meeting_share_ctrl_event = None

        self.active_speaker_id = None
        self.active_sharer_id = None
        self.active_sharer_source_id = None

        self._participant_cache = {}

        self.meeting_status = None

        self.suggested_video_cap = None

        self.mp4_demuxer = None

        self.cannot_send_video_error_ticker = 0
        self.cannot_send_audio_error_ticker = 0
        self.send_raw_audio_unmute_ticker = 0

        # The Zoom Linux SDK has a bug where if the meeting password is incorrect, it will not return an error
        # it will just get stuck in the connecting state. So we assume if we have been in the connecting state for
        # more than 10 seconds, we should send a message to the bot controller that we could not connect to the meeting
        # https://devforum.zoom.us/t/linux-sdk-gets-stuck-in-meeting-status-connecting-when-the-provided-password-is-incorrect/130441
        self.stuck_in_connecting_state_timeout = 60

        # Breakout room controller
        self.breakout_room_ctrl = None
        self.breakout_room_ctrl_event = None
        self.is_joining_or_leaving_breakout_room = False

        # Waiting room controller
        self.waiting_room_ctrl = None

        # webcam is muted initially
        self.webcam_is_muted = True
        # Raw bytes from the user
        self.current_raw_image_to_send = None
        # Scaled image to send to Zoom
        self.current_image_to_send = None
        self.recording_is_paused = False

        self.ready_to_send_chat_messages = False

    def pause_recording(self):
        self.recording_is_paused = True
        if not self.raw_recording_active:
            logger.info("returning early from pause_recording because raw recording is not active")
            return
        self.stop_raw_recording()

    def start_or_resume_recording(self):
        self.recording_is_paused = False
        if self.raw_recording_active:
            logger.info("returning early from resume_recording because raw recording is active")
            return
        self.start_raw_recording()

    def request_permission_to_record_if_joined_user_is_host(self, joined_user_id):
        # No need to request permission if we already have it
        if self.recording_permission_granted:
            return

        try:
            joined_user = self.participants_ctrl.GetUserByUserID(joined_user_id)
            if joined_user and joined_user.IsHost():
                logger.info("Re-requesting recording privilege since host just joined.")
                self.recording_ctrl.RequestLocalRecordingPrivilege()
        except Exception as e:
            logger.info(f"Error retrieving user in request_permission_to_record_if_joined_user_is_host: {e}")

    def on_user_join_callback(self, joined_user_ids, _):
        logger.info(f"on_user_join_callback called. joined_user_ids = {joined_user_ids}")
        self.update_only_one_participant_in_meeting_at()
        for joined_user_id in joined_user_ids:
            self.get_participant(joined_user_id)
            self.send_participant_event(joined_user_id, event_type=ParticipantEventTypes.JOIN)
            self.request_permission_to_record_if_joined_user_is_host(joined_user_id)

    def update_only_one_participant_in_meeting_at(self):
        if not self.joined_at:
            return

        # If nobody other than the bot was ever in the meeting, then don't activate this. We only want to activate if someone else was in the meeting and left
        if self.number_of_participants_ever_in_meeting() <= 1:
            return

        all_participant_ids = self.participants_ctrl.GetParticipantsList()
        if len(all_participant_ids) == 1:
            if self.only_one_participant_in_meeting_at is None:
                self.only_one_participant_in_meeting_at = time.time()
        else:
            self.only_one_participant_in_meeting_at = None

    def on_user_left_callback(self, left_user_ids, _):
        logger.info(f"on_user_left_callback called. left_user_ids = {left_user_ids}")
        self.update_only_one_participant_in_meeting_at()

        for left_user_id in left_user_ids:
            self.send_participant_event(left_user_id, event_type=ParticipantEventTypes.LEAVE)

    def on_host_request_start_audio_callback(self, handler):
        logger.info("on_host_request_start_audio_callback called. Accepting request.")
        handler.Accept()

    def on_user_active_audio_change_callback(self, user_ids):
        if len(user_ids) == 0:
            return

        if user_ids[0] == self.my_participant_id:
            return

        if self.active_speaker_id == user_ids[0]:
            return

        self.active_speaker_id = user_ids[0]
        self.set_video_input_manager_based_on_state()

    def set_video_input_manager_based_on_state(self):
        if not self.raw_recording_active and self.video_input_manager:
            self.video_input_manager.set_mode(
                mode=VideoInputManager.Mode.INACTIVE,
                active_speaker_id=None,
                active_sharer_id=None,
                active_sharer_source_id=None,
            )
            return

        if not self.wants_any_video_frames_callback():
            return

        if not self.recording_permission_granted:
            return

        if self.is_joining_or_leaving_breakout_room:
            return

        if not self.video_input_manager:
            return

        logger.info(f"set_video_input_manager_based_on_state self.active_speaker_id = {self.active_speaker_id}, self.active_sharer_id = {self.active_sharer_id}, self.active_sharer_source_id = {self.active_sharer_source_id}")
        if self.active_sharer_id:
            self.video_input_manager.set_mode(
                mode=VideoInputManager.Mode.ACTIVE_SHARER,
                active_sharer_id=self.active_sharer_id,
                active_sharer_source_id=self.active_sharer_source_id,
                active_speaker_id=self.active_speaker_id,
            )
        elif self.active_speaker_id:
            self.video_input_manager.set_mode(
                mode=VideoInputManager.Mode.ACTIVE_SPEAKER,
                active_sharer_id=self.active_sharer_id,
                active_sharer_source_id=self.active_sharer_source_id,
                active_speaker_id=self.active_speaker_id,
            )
        else:
            # If there is no active sharer or speaker, we'll just use the video of the first participant that is not the bot
            # or if there are no participants, we'll use the bot
            default_participant_id = self.my_participant_id

            participant_list = self.participants_ctrl.GetParticipantsList()
            for participant_id in participant_list:
                if participant_id != self.my_participant_id:
                    default_participant_id = participant_id
                    break

            logger.info(f"set_video_input_manager_based_on_state hit default case. default_participant_id = {default_participant_id}")
            self.video_input_manager.set_mode(
                mode=VideoInputManager.Mode.ACTIVE_SPEAKER,
                active_speaker_id=default_participant_id,
                active_sharer_id=None,
                active_sharer_source_id=None,
            )

    def set_up_video_input_manager(self):
        # If someone was sharing before we joined, we will not receive an event, so we need to poll for the active sharer
        viewable_sharing_user_list = self.meeting_sharing_controller.GetViewableSharingUserList()
        self.active_sharer_id = None
        self.active_sharer_source_id = None

        if viewable_sharing_user_list:
            sharing_source_info_list = self.meeting_sharing_controller.GetSharingSourceInfoList(viewable_sharing_user_list[0])
            if sharing_source_info_list:
                self.active_sharer_id = sharing_source_info_list[0].userid
                self.active_sharer_source_id = sharing_source_info_list[0].shareSourceID

        self.set_video_input_manager_based_on_state()

    def cleanup(self):
        if self.audio_source:
            performance_data = self.audio_source.getPerformanceData()
            logger.info(f"totalProcessingTimeMicroseconds = {performance_data.totalProcessingTimeMicroseconds}")
            logger.info(f"numCalls = {performance_data.numCalls}")
            if performance_data.numCalls > 0:
                logger.info(f"maxProcessingTimeMicroseconds = {performance_data.maxProcessingTimeMicroseconds}")
                logger.info(f"minProcessingTimeMicroseconds = {performance_data.minProcessingTimeMicroseconds}")
                logger.info(f"meanProcessingTimeMicroseconds = {float(performance_data.totalProcessingTimeMicroseconds) / performance_data.numCalls}")

                # Print processing time distribution
                bin_size = (performance_data.processingTimeBinMax - performance_data.processingTimeBinMin) / len(performance_data.processingTimeBinCounts)
                logger.info("\nProcessing time distribution (microseconds):")
                for bin_idx, count in enumerate(performance_data.processingTimeBinCounts):
                    if count > 0:
                        bin_start = bin_idx * bin_size
                        bin_end = (bin_idx + 1) * bin_size
                        logger.info(f"{bin_start:6.0f} - {bin_end:6.0f} us: {count:5d} calls")

        if self.meeting_service:
            zoom.DestroyMeetingService(self.meeting_service)
            logger.info("Destroyed Meeting service")
        if self.setting_service:
            zoom.DestroySettingService(self.setting_service)
            logger.info("Destroyed Setting service")
        if self.auth_service:
            zoom.DestroyAuthService(self.auth_service)
            logger.info("Destroyed Auth service")

        if self.audio_helper:
            audio_helper_unsubscribe_result = self.audio_helper.unSubscribe()
            logger.info(f"audio_helper.unSubscribe() returned {audio_helper_unsubscribe_result}")

        if self.video_input_manager:
            self.video_input_manager.cleanup()

        logger.info("CleanUPSDK() called")
        zoom.CleanUPSDK()
        logger.info("CleanUPSDK() finished")
        self.cleaned_up = True

    def init(self):
        init_param = zoom.InitParam()

        init_param.strWebDomain = "https://zoom.us"
        init_param.strSupportUrl = "https://zoom.us"
        init_param.enableGenerateDump = True
        init_param.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
        init_param.enableLogByDefault = True

        init_sdk_result = zoom.InitSDK(init_param)
        if init_sdk_result != zoom.SDKERR_SUCCESS:
            raise Exception("InitSDK failed")

        self.create_services()

    def get_participant(self, participant_id):
        try:
            speaker_object = self.participants_ctrl.GetUserByUserID(participant_id)
            participant_info = {
                "participant_uuid": participant_id,
                "participant_user_uuid": speaker_object.GetPersistentId(),
                "participant_full_name": speaker_object.GetUserName(),
                "participant_is_the_bot": speaker_object.GetUserID() == self.my_participant_id,
                "participant_is_host": speaker_object.IsHost(),
            }
            self._participant_cache[participant_id] = participant_info
            return participant_info
        except:
            logger.info(f"Error getting participant {participant_id}, falling back to cache")
            return self._participant_cache.get(participant_id)

    def number_of_participants_ever_in_meeting(self):
        return len(self._participant_cache)

    def on_sharing_status_callback(self, sharing_info):
        user_id = sharing_info.userid
        sharing_status = sharing_info.status
        logger.info(f"on_sharing_status_callback called. sharing_status = {sharing_status}, user_id = {user_id}")

        if sharing_status == zoom.Sharing_Other_Share_Begin or sharing_status == zoom.Sharing_View_Other_Sharing:
            new_active_sharer_id = user_id
            new_active_sharer_source_id = sharing_info.shareSourceID
        else:
            new_active_sharer_id = None
            new_active_sharer_source_id = None

        if new_active_sharer_id != self.active_sharer_id or new_active_sharer_source_id != self.active_sharer_source_id:
            self.active_sharer_id = new_active_sharer_id
            self.active_sharer_source_id = new_active_sharer_source_id
            self.set_video_input_manager_based_on_state()

    def send_chat_message(self, text, to_user_uuid):
        # Send a welcome message to the chat
        builder = self.chat_ctrl.GetChatMessageBuilder()
        builder.SetContent(text)
        if to_user_uuid:
            builder.SetReceiver(to_user_uuid)
            builder.SetMessageType(zoom.SDKChatMessageType.To_Individual)
        else:
            builder.SetReceiver(0)
            builder.SetMessageType(zoom.SDKChatMessageType.To_All)
        msg = builder.Build()
        send_chat_message_result = self.chat_ctrl.SendChatMsgTo(msg)
        logger.info(f"send_chat_message_result = {send_chat_message_result}")
        builder.Clear()

    def on_chat_msg_notification_callback(self, chat_msg_info, content):
        if self.recording_is_paused and not self.record_chat_messages_when_paused:
            logger.info("on_chat_msg_notification_callback called but recording is paused")
            return

        try:
            self.upsert_chat_message_callback(
                {
                    "text": chat_msg_info.GetContent(),
                    "participant_uuid": chat_msg_info.GetSenderUserId(),
                    "timestamp": chat_msg_info.GetTimeStamp(),
                    "message_uuid": chat_msg_info.GetMessageID(),
                    # Simplified logic to determine if the message is for the bot. Not completely accurate.
                    "to_bot": not chat_msg_info.IsChatToAllPanelist() and not chat_msg_info.IsChatToAll() and not chat_msg_info.IsChatToWaitingroom(),
                    "additional_data": {
                        "is_comment": chat_msg_info.IsComment(),
                        "is_thread": chat_msg_info.IsThread(),
                        "thread_id": chat_msg_info.GetThreadID(),
                        "is_chat_to_all": chat_msg_info.IsChatToAll(),
                        "is_chat_to_all_panelist": chat_msg_info.IsChatToAllPanelist(),
                        "is_chat_to_waitingroom": chat_msg_info.IsChatToWaitingroom(),
                    },
                }
            )
        except Exception as e:
            logger.error(f"Error processing chat message: {e}")

    def send_participant_event(self, participant_id, event_type, event_data={}):
        self.add_participant_event_callback({"participant_uuid": participant_id, "event_type": event_type, "event_data": event_data, "timestamp_ms": int(time.time() * 1000)})

    def on_has_attendee_rights_notification(self, attendee):
        logger.info(f"on_has_attendee_rights_notification called. attendee = {attendee}")
        join_bo_result = attendee.JoinBo()
        logger.info(f"join_bo_result = {join_bo_result}")

    def admit_from_waiting_room(self):
        logger.info("admit_from_waiting_room called")
        admit_all_to_meeting_result = self.waiting_room_ctrl.AdmitAllToMeeting()
        logger.info(f"admit_all_to_meeting_result = {admit_all_to_meeting_result}")

    def apply_meeting_settings(self):
        # Set various aspects of the meeting. Will only work if the bot has host privileges.

        allow_participants_to_unmute_self = self.zoom_meeting_settings.get("allow_participants_to_unmute_self", None)
        if allow_participants_to_unmute_self is not None:
            allow_participants_to_unmute_self_result = self.participants_ctrl.AllowParticipantsToUnmuteSelf(allow_participants_to_unmute_self)
            logger.info(f"AllowParticipantsToUnmuteSelf({allow_participants_to_unmute_self}) returned {allow_participants_to_unmute_self_result}")

        allow_participants_to_share_whiteboard = self.zoom_meeting_settings.get("allow_participants_to_share_whiteboard", None)
        if allow_participants_to_share_whiteboard is not None:
            allow_participants_to_share_whiteboard_result = self.participants_ctrl.AllowParticipantsToShareWhiteBoard(allow_participants_to_share_whiteboard)
            logger.info(f"AllowParticipantsToShareWhiteBoard({allow_participants_to_share_whiteboard}) returned {allow_participants_to_share_whiteboard_result}")

        allow_participants_to_request_cloud_recording = self.zoom_meeting_settings.get("allow_participants_to_request_cloud_recording", None)
        if allow_participants_to_request_cloud_recording is not None:
            allow_participants_to_request_cloud_recording_result = self.participants_ctrl.AllowParticipantsToRequestCloudRecording(allow_participants_to_request_cloud_recording)
            logger.info(f"AllowParticipantsToRequestCloudRecording({allow_participants_to_request_cloud_recording}) returned {allow_participants_to_request_cloud_recording_result}")

        allow_participants_to_request_local_recording = self.zoom_meeting_settings.get("allow_participants_to_request_local_recording", None)
        if allow_participants_to_request_local_recording is not None:
            allow_participants_to_request_local_recording_result = self.participants_ctrl.AllowParticipantsToRequestLocalRecording(allow_participants_to_request_local_recording)
            logger.info(f"AllowParticipantsToRequestLocalRecording({allow_participants_to_request_local_recording}) returned {allow_participants_to_request_local_recording_result}")

        enable_focus_mode = self.zoom_meeting_settings.get("enable_focus_mode", None)
        if enable_focus_mode is not None:
            is_focus_mode_on = self.participants_ctrl.IsFocusModeOn()
            logger.info(f"IsFocusModeOn() returned {is_focus_mode_on}")
            is_focus_mode_enabled = self.participants_ctrl.IsFocusModeEnabled()
            logger.info(f"IsFocusModeEnabled() returned {is_focus_mode_enabled}")
            turn_focus_mode_on_result = self.participants_ctrl.TurnFocusModeOn(enable_focus_mode)
            logger.info(f"TurnFocusModeOn({enable_focus_mode}) returned {turn_focus_mode_on_result}")

        allow_participants_to_share_screen = self.zoom_meeting_settings.get("allow_participants_to_share_screen", None)
        if allow_participants_to_share_screen is not None:
            lock_share_result = self.meeting_sharing_controller.LockShare(not allow_participants_to_share_screen)
            logger.info(f"LockShare({not allow_participants_to_share_screen}) returned {lock_share_result}")

        allow_participants_to_chat = self.zoom_meeting_settings.get("allow_participants_to_chat", None)
        if allow_participants_to_chat is not None:
            allow_participants_to_chat_result = self.participants_ctrl.AllowParticipantsToChat(allow_participants_to_chat)
            logger.info(f"AllowParticipantsToChat({allow_participants_to_chat}) returned {allow_participants_to_chat_result}")

    def is_ready_to_send_chat_messages(self):
        return self.ready_to_send_chat_messages

    def on_join(self):
        # Reset breakout room transition flag
        self.is_joining_or_leaving_breakout_room = False

        # Meeting reminder controller
        self.joined_at = time.time()
        self.meeting_reminder_event = zoom.MeetingReminderEventCallbacks(onReminderNotifyCallback=self.on_reminder_notify)
        self.reminder_controller = self.meeting_service.GetMeetingReminderController()
        self.reminder_controller.SetEvent(self.meeting_reminder_event)

        # Participants controller
        self.participants_ctrl = self.meeting_service.GetMeetingParticipantsController()
        self.participants_ctrl_event = zoom.MeetingParticipantsCtrlEventCallbacks(onUserJoinCallback=self.on_user_join_callback, onUserLeftCallback=self.on_user_left_callback)
        self.participants_ctrl.SetEvent(self.participants_ctrl_event)
        self.my_participant_id = self.participants_ctrl.GetMySelfUser().GetUserID()
        participant_ids_list = self.participants_ctrl.GetParticipantsList()
        for participant_id in participant_ids_list:
            self.get_participant(participant_id)
            self.send_participant_event(participant_id, event_type=ParticipantEventTypes.JOIN)

        # Chats controller
        self.chat_ctrl = self.meeting_service.GetMeetingChatController()
        self.chat_ctrl_event = zoom.MeetingChatEventCallbacks(onChatMsgNotificationCallback=self.on_chat_msg_notification_callback)
        self.chat_ctrl.SetEvent(self.chat_ctrl_event)
        self.ready_to_send_chat_messages = True
        self.send_message_callback({"message": self.Messages.READY_TO_SEND_CHAT_MESSAGE})

        # Breakout room controller
        self.breakout_room_ctrl = self.meeting_service.GetMeetingBOController()
        self.breakout_room_ctrl_event = zoom.MeetingBOEventCallbacks(onHasAttendeeRightsNotificationCallback=self.on_has_attendee_rights_notification)
        self.breakout_room_ctrl.SetEvent(self.breakout_room_ctrl_event)

        # Waiting room controller
        self.waiting_room_ctrl = self.meeting_service.GetMeetingWaitingRoomController()

        # Meeting sharing controller
        self.meeting_sharing_controller = self.meeting_service.GetMeetingShareController()
        self.meeting_share_ctrl_event = zoom.MeetingShareCtrlEventCallbacks(onSharingStatusCallback=self.on_sharing_status_callback)
        self.meeting_sharing_controller.SetEvent(self.meeting_share_ctrl_event)

        # Audio controller
        self.audio_ctrl = self.meeting_service.GetMeetingAudioController()
        self.audio_ctrl_event = zoom.MeetingAudioCtrlEventCallbacks(onHostRequestStartAudioCallback=self.on_host_request_start_audio_callback, onUserActiveAudioChangeCallback=self.on_user_active_audio_change_callback)
        self.audio_ctrl.SetEvent(self.audio_ctrl_event)
        # Raw audio input got borked in the Zoom SDK after 6.3.5.
        # This is work-around to get it to work again.
        # See here for more details: https://devforum.zoom.us/t/cant-record-audio-with-linux-meetingsdk-after-6-3-5-6495-error-code-32/130689/5
        self.audio_ctrl.JoinVoip()

        if self.use_raw_recording:
            self.recording_ctrl = self.meeting_service.GetMeetingRecordingController()

            # Wire up callbacks for changes to recording privilege

            def on_recording_privilege_changed(can_rec):
                logger.info(f"on_recording_privilege_changed called. can_record = {can_rec}")
                if can_rec:
                    self.handle_recording_permission_granted()
                elif self.recording_permission_granted:
                    self.handle_recording_permission_denied(reason=BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION)

            def on_local_recording_privilege_request_status_changed(status):
                logger.info(f"on_local_recording_privilege_request_status called. status = {status}")
                if status == zoom.RequestLocalRecordingStatus.RequestLocalRecording_Denied:
                    self.handle_recording_permission_denied(reason=BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION)
                if status == zoom.RequestLocalRecordingStatus.RequestLocalRecording_Timeout:
                    self.handle_recording_permission_denied(reason=BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.REQUEST_TIMED_OUT)

            self.recording_event = zoom.MeetingRecordingCtrlEventCallbacks(onRecordPrivilegeChangedCallback=on_recording_privilege_changed, onLocalRecordingPrivilegeRequestStatusCallback=on_local_recording_privilege_request_status_changed)
            self.recording_ctrl.SetEvent(self.recording_event)

            # Check if we can start recording.
            # If we can then start it
            # If we can't then request it

            can_start_recording_result = self.recording_ctrl.CanStartRawRecording()
            if can_start_recording_result != zoom.SDKERR_SUCCESS:
                # Check for various reasons why it might be impossible to get the permission
                is_support_request_local_recording_privilege_result = self.recording_ctrl.IsSupportRequestLocalRecordingPrivilege()
                logger.info(f"is_support_request_local_recording_privilege_result = {is_support_request_local_recording_privilege_result}")
                # This means the host is using a zoom client that is incapable of displaying the popup to allow recording (Only known client where this happens is Zoom Rooms)
                if is_support_request_local_recording_privilege_result == zoom.SDKERR_MEETING_DONT_SUPPORT_FEATURE:
                    self.handle_recording_permission_denied(reason=BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_CLIENT_CANNOT_GRANT_PERMISSION)

                self.recording_ctrl.RequestLocalRecordingPrivilege()
                logger.info("Requesting recording privilege.")
            else:
                self.handle_recording_permission_granted()

        # Apply meeting settings
        self.apply_meeting_settings()

        # Set up media streams
        GLib.timeout_add_seconds(1, self.set_up_bot_audio_input)
        GLib.timeout_add_seconds(1, self.set_up_bot_video_input)

    def set_up_bot_video_input(self):
        self.virtual_camera_video_source = zoom.ZoomSDKVideoSourceCallbacks(
            onInitializeCallback=self.on_virtual_camera_initialize_callback,
            onStartSendCallback=self.on_virtual_camera_start_send_callback,
        )
        self.video_source_helper = zoom.GetRawdataVideoSourceHelper()
        if self.video_source_helper:
            set_external_video_source_result = self.video_source_helper.setExternalVideoSource(self.virtual_camera_video_source)
            logger.info(f"set_external_video_source_result = {set_external_video_source_result}")
            if set_external_video_source_result == zoom.SDKERR_SUCCESS:
                self.meeting_video_controller = self.meeting_service.GetMeetingVideoController()
                # At this point, we can show the bot image if there is one
                self.send_message_callback({"message": self.Messages.READY_TO_SHOW_BOT_IMAGE})
        else:
            logger.info("video_source_helper is None")

    def on_virtual_camera_start_send_callback(self):
        logger.info("on_virtual_camera_start_send_callback called")
        # As soon as we get this callback, we need to send a blank frame and it will fail with SDKERR_WRONG_USAGE
        # Then the callback will be triggered again and subsequent calls will succeed.
        # Not sure why this happens.
        if self.video_sender and not self.on_virtual_camera_start_send_callback_called and self.suggested_video_cap:
            blank = create_black_yuv420_frame(self.suggested_video_cap.width, self.suggested_video_cap.height)
            initial_send_video_frame_response = self.video_sender.sendVideoFrame(blank, self.suggested_video_cap.width, self.suggested_video_cap.height, 0, zoom.FrameDataFormat_I420_FULL)
            logger.info(f"initial_send_video_frame_response = {initial_send_video_frame_response}")
        self.on_virtual_camera_start_send_callback_called = True

    def on_virtual_camera_initialize_callback(self, video_sender, support_cap_list, suggest_cap):
        logger.info(f"on_virtual_camera_initialize_callback called with support_cap_list = {list(map(lambda x: f'{x.width}x{x.height}x{x.frame}', support_cap_list))} suggest_cap = {suggest_cap.width}x{suggest_cap.height}x{suggest_cap.frame}")
        self.video_sender = video_sender
        self.suggested_video_cap = suggest_cap

    def unmute_webcam(self):
        if not self.webcam_is_muted:
            logger.info("webcam is already unmuted")
            return True

        if not self.meeting_video_controller:
            logger.info("meeting_video_controller is None so cannot unmute webcam")
            return False

        unmute_webcam_result = self.meeting_video_controller.UnmuteVideo()
        if unmute_webcam_result != zoom.SDKERR_SUCCESS:
            logger.info(f"Failed to unmute webcam. unmute_webcam_result = {unmute_webcam_result}")
            return False
        logger.info("Unmuted webcam")
        self.webcam_is_muted = False
        return True

    def compute_current_image_to_send(self):
        if not self.current_raw_image_to_send:
            logger.info("current_raw_image_to_send is None so cannot compute current image to send")
            return None
        if not self.suggested_video_cap:
            logger.info("suggested_video_cap is None so cannot compute current image to send")
            return None

        yuv420_image_bytes, original_width, original_height = png_to_yuv420_frame(self.current_raw_image_to_send)
        # We have to scale the image to the zoom video capability width and height for it to display properly
        yuv420_image_bytes_scaled = scale_i420(yuv420_image_bytes, (original_width, original_height), (self.suggested_video_cap.width, self.suggested_video_cap.height))

        return yuv420_image_bytes_scaled

    def send_raw_image(self, png_image_bytes):
        if not self.meeting_video_controller:
            logger.info("meeting_video_controller is None so cannot send raw image")
            return

        if not self.unmute_webcam():
            return

        self.current_raw_image_to_send = png_image_bytes
        # We can't compute the scaled image immediately because the video caps may have not arrived yet. So set it to None, which indicates it needs to be recomputed.
        self.current_image_to_send = None

        # Add a timeout to send the image every 500ms if one isn't already active
        if self.send_image_timeout_id is None:
            self.send_image_timeout_id = GLib.timeout_add(500, self.send_current_image_to_zoom)

    def send_current_image_to_zoom(self):
        if self.requested_leave or self.cleaned_up or (not self.current_raw_image_to_send):
            self.send_image_timeout_id = None
            return False

        if not self.on_virtual_camera_start_send_callback_called:
            if self.cannot_send_video_error_ticker % 100 == 0:
                logger.info("on_virtual_camera_start_send_callback_called not called so cannot send raw image, but will retry later")
            self.cannot_send_video_error_ticker += 1
            return True

        if not self.suggested_video_cap:
            if self.cannot_send_video_error_ticker % 100 == 0:
                logger.info("suggested_video_cap is None so cannot send raw image, but will retry later")
            self.cannot_send_video_error_ticker += 1
            return True

        if not self.current_image_to_send:
            self.current_image_to_send = self.compute_current_image_to_send()
        if not self.current_image_to_send:
            if self.cannot_send_video_error_ticker % 100 == 0:
                logger.info("Failed to compute current image to send so cannot send raw image, but will retry later")
            self.cannot_send_video_error_ticker += 1
            return True

        send_video_frame_response = self.video_sender.sendVideoFrame(self.current_image_to_send, self.suggested_video_cap.width, self.suggested_video_cap.height, 0, zoom.FrameDataFormat_I420_FULL)
        if send_video_frame_response != zoom.SDKERR_SUCCESS:
            if self.cannot_send_video_error_ticker % 100 == 0:
                logger.info(f"send_current_image_to_zoom failed with send_video_frame_response = {send_video_frame_response}")
            self.cannot_send_video_error_ticker += 1

        return True

    def send_video_frame_to_zoom(self, yuv420_image_bytes, original_width, original_height):
        if self.requested_leave or self.cleaned_up or (not self.suggested_video_cap):
            return False

        # Only scale if the dimensions are different
        if original_width != self.suggested_video_cap.width or original_height != self.suggested_video_cap.height:
            yuv420_image_bytes_scaled = scale_i420(yuv420_image_bytes, (original_width, original_height), (self.suggested_video_cap.width, self.suggested_video_cap.height))
            logger.info(f"Sending scaled video frame to Zoom. Original dimensions: {original_width}x{original_height}, Suggested dimensions: {self.suggested_video_cap.width}x{self.suggested_video_cap.height}")
        else:
            yuv420_image_bytes_scaled = yuv420_image_bytes

        send_video_frame_response = self.video_sender.sendVideoFrame(yuv420_image_bytes_scaled, self.suggested_video_cap.width, self.suggested_video_cap.height, 0, zoom.FrameDataFormat_I420_FULL)
        if send_video_frame_response != zoom.SDKERR_SUCCESS:
            logger.info(f"send_video_frame_to_zoom failed with send_video_frame_response = {send_video_frame_response}")
        return True

    def set_up_bot_audio_input(self):
        if self.audio_helper is None:
            self.audio_helper = zoom.GetAudioRawdataHelper()

        if self.audio_helper is None:
            logger.info("set_up_bot_audio_input failed because audio_helper is None")
            return

        self.virtual_audio_mic_event_passthrough = zoom.ZoomSDKVirtualAudioMicEventCallbacks(
            onMicInitializeCallback=self.on_mic_initialize_callback,
            onMicStartSendCallback=self.on_mic_start_send_callback,
        )

        audio_helper_set_external_audio_source_result = self.audio_helper.setExternalAudioSource(self.virtual_audio_mic_event_passthrough)
        logger.info(f"audio_helper_set_external_audio_source_result = {audio_helper_set_external_audio_source_result}")
        if audio_helper_set_external_audio_source_result != zoom.SDKERR_SUCCESS:
            logger.info("Failed to set external audio source")
            return

    def on_mic_initialize_callback(self, sender):
        self.audio_raw_data_sender = sender

    def periodically_unmute_audio(self):
        # Let's periodically try to unmute the audio, in case someone muted us
        if self.send_raw_audio_unmute_ticker % 1000 == 0 and self.my_participant_id is not None and self.audio_ctrl is not None:
            if self.audio_ctrl.CanUnMuteBySelf():
                unmute_result = self.audio_ctrl.UnMuteAudio(self.my_participant_id)
                if unmute_result != zoom.SDKERR_SUCCESS:
                    logger.info(f"Failed to unmute audio. unmute_result = {unmute_result}")
            else:
                logger.info("Cannot unmute audio by self")
        self.send_raw_audio_unmute_ticker += 1

    def send_raw_audio(self, bytes, sample_rate):
        self.periodically_unmute_audio()

        if not self.on_mic_start_send_callback_called:
            if self.cannot_send_audio_error_ticker % 500 == 0:
                logger.error("on_mic_start_send_callback_called not called so cannot send raw audio")
            self.cannot_send_audio_error_ticker += 1
            return

        send_result = self.audio_raw_data_sender.send(bytes, sample_rate, zoom.ZoomSDKAudioChannel_Mono)
        if send_result != zoom.SDKERR_SUCCESS:
            logger.info(f"error with send_raw_audio send_result = {send_result}")

    def on_mic_start_send_callback(self):
        self.on_mic_start_send_callback_called = True
        logger.info("on_mic_start_send_callback called")

    def on_one_way_audio_raw_data_received_callback(self, data, node_id):
        if node_id == self.my_participant_id:
            return

        if self.recording_is_paused:
            return

        current_time = datetime.utcnow()
        self.last_audio_received_at = time.time()
        self.add_audio_chunk_callback(node_id, current_time, data.GetBuffer())

    def add_mixed_audio_chunk_convert_to_bytes(self, data):
        if self.recording_is_paused:
            return
        self.add_mixed_audio_chunk_callback(chunk=data.GetBuffer())

    def handle_recording_permission_granted(self):
        if not self.recording_permission_granted:
            self.send_message_callback({"message": self.Messages.BOT_RECORDING_PERMISSION_GRANTED})
            self.recording_permission_granted = True

    def stop_raw_recording(self):
        logger.info("Stopping raw recording")
        stop_raw_recording_result = self.recording_ctrl.StopRawRecording()
        # SDKERR_TOO_FREQUENT_CALL means it was already called recently
        if stop_raw_recording_result != zoom.SDKERR_SUCCESS and stop_raw_recording_result != zoom.SDKERR_TOO_FREQUENT_CALL:
            logger.info(f"Error with stop_raw_recording_result = {stop_raw_recording_result}")
        else:
            self.raw_recording_active = False
            logger.info(f"Raw recording stopped stop_raw_recording_result = {stop_raw_recording_result}")

        self.set_video_input_manager_based_on_state()

    def start_raw_recording(self):
        logger.info("Starting raw recording")
        start_raw_recording_result = self.recording_ctrl.StartRawRecording()
        if start_raw_recording_result != zoom.SDKERR_SUCCESS:
            logger.info(f"Error with start_raw_recording_result = {start_raw_recording_result}")
        else:
            self.raw_recording_active = True
            logger.info("Raw recording started")

        if self.audio_helper is None:
            self.audio_helper = zoom.GetAudioRawdataHelper()
        if self.audio_helper is None:
            logger.info("audio_helper is None")
            return

        if self.audio_source is None:
            self.audio_source = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
                collectPerformanceData=True,
                onOneWayAudioRawDataReceivedCallback=self.on_one_way_audio_raw_data_received_callback if self.use_one_way_audio else None,
                onMixedAudioRawDataReceivedCallback=self.add_mixed_audio_chunk_convert_to_bytes if self.use_mixed_audio else None,
            )

            audio_helper_subscribe_result = self.audio_helper.subscribe(self.audio_source, False)
            logger.info(f"audio_helper_subscribe_result = {audio_helper_subscribe_result}")

        GLib.timeout_add(100, self.set_up_video_input_manager)

    def handle_recording_permission_denied(self, reason):
        self.send_message_callback({"message": self.Messages.BOT_RECORDING_PERMISSION_DENIED, "denied_reason": reason})
        self.recording_permission_granted = False

    def leave(self):
        if self.meeting_service is None:
            return

        status = self.meeting_service.GetMeetingStatus()
        if status == zoom.MEETING_STATUS_IDLE or status == zoom.MEETING_STATUS_ENDED:
            logger.info(f"Aborting leave because meeting status is {status}")
            return

        logger.info("Requesting to leave meeting...")
        leave_result = self.meeting_service.Leave(zoom.LEAVE_MEETING)
        logger.info(f"Requested to leave meeting. result = {leave_result}")
        self.requested_leave = True

    def join_meeting(self):
        meeting_number = int(self.meeting_id)

        join_param = zoom.JoinParam()
        join_param.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN

        param = join_param.param
        param.meetingNumber = meeting_number
        param.userName = self.display_name
        param.psw = self.meeting_password if self.meeting_password is not None else ""
        param.isVideoOff = True
        param.isAudioOff = False
        param.isAudioRawDataStereo = False
        param.isMyVoiceInMix = False

        # If we have tokens, we can use them to join the meeting
        if self.zoom_tokens.get("zak_token"):
            param.userZAK = self.zoom_tokens.get("zak_token")
        if self.zoom_tokens.get("join_token"):
            param.join_token = self.zoom_tokens.get("join_token")
        if self.zoom_tokens.get("app_privilege_token"):
            param.app_privilege_token = self.zoom_tokens.get("app_privilege_token")

        param.eAudioRawdataSamplingRate = zoom.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K

        join_result = self.meeting_service.Join(join_param)
        logger.info(f"join_result = {join_result}")

        self.audio_settings = self.setting_service.GetAudioSettings()
        self.audio_settings.EnableAutoJoinAudio(True)

    def on_reminder_notify(self, content, handler):
        if handler:
            handler.Accept()

    def auth_return(self, result):
        if result == zoom.AUTHRET_SUCCESS:
            logger.info("Auth completed successfully.")
            return self.join_meeting()

        self.send_message_callback(
            {
                "message": self.Messages.ZOOM_AUTHORIZATION_FAILED,
                "zoom_result_code": result,
            }
        )

    def leave_meeting_if_not_started_yet(self):
        if self.meeting_status != zoom.MEETING_STATUS_WAITINGFORHOST:
            return

        logger.info(f"Give up trying to join meeting because we've waited for the host to start it for over {self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds} seconds")
        self.send_message_callback({"message": self.Messages.LEAVE_MEETING_WAITING_FOR_HOST})

    def leave_meeting_if_still_in_waiting_room(self):
        if self.meeting_status != zoom.MEETING_STATUS_IN_WAITING_ROOM:
            return

        logger.info(f"Give up trying to join meeting because we've been in the waiting room for over {self.automatic_leave_configuration.waiting_room_timeout_seconds} seconds")
        self.send_message_callback({"message": self.Messages.LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED})

    def wait_for_host_to_start_meeting_then_give_up(self):
        wait_time = self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
        logger.info(f"Waiting for host to start meeting. If host doesn't start meeting in {wait_time} seconds, we'll give up")
        GLib.timeout_add_seconds(wait_time, self.leave_meeting_if_not_started_yet)

    def give_up_if_still_in_connecting_state(self):
        if self.meeting_status != zoom.MEETING_STATUS_CONNECTING:
            return

        if self.is_joining_or_leaving_breakout_room:
            return

        logger.info(f"We've been in the connecting state for more than {self.stuck_in_connecting_state_timeout} seconds, going to return could not connect to meeting message")
        self.send_message_callback({"message": self.Messages.COULD_NOT_CONNECT_TO_MEETING})

    def wait_to_get_out_of_connecting_state(self):
        logger.info(f"Set a timeout to abort if we're still in the connecting state after {self.stuck_in_connecting_state_timeout} seconds")
        GLib.timeout_add_seconds(self.stuck_in_connecting_state_timeout, self.give_up_if_still_in_connecting_state)

    def meeting_status_changed(self, status, iResult):
        logger.info(f"meeting_status_changed called. status = {status}, iResult={iResult}")
        self.meeting_status = status

        if status == zoom.MEETING_STATUS_JOIN_BREAKOUT_ROOM:
            self.is_joining_or_leaving_breakout_room = True
            self.send_message_callback({"message": self.Messages.JOINING_BREAKOUT_ROOM})

        if status == zoom.MEETING_STATUS_LEAVE_BREAKOUT_ROOM:
            self.is_joining_or_leaving_breakout_room = True
            self.send_message_callback({"message": self.Messages.LEAVING_BREAKOUT_ROOM})

        if status == zoom.MEETING_STATUS_CONNECTING:
            self.wait_to_get_out_of_connecting_state()

        if status == zoom.MEETING_STATUS_WAITINGFORHOST:
            self.wait_for_host_to_start_meeting_then_give_up()

        if status == zoom.MEETING_STATUS_IN_WAITING_ROOM:
            self.send_message_callback({"message": self.Messages.BOT_PUT_IN_WAITING_ROOM})
            GLib.timeout_add_seconds(self.automatic_leave_configuration.waiting_room_timeout_seconds, self.leave_meeting_if_still_in_waiting_room)

        if status == zoom.MEETING_STATUS_INMEETING:
            self.send_message_callback({"message": self.Messages.BOT_JOINED_MEETING})

        if status == zoom.MEETING_STATUS_ENDED:
            # We get the MEETING_STATUS_ENDED regardless of whether we initiated the leave or not
            self.send_message_callback({"message": self.Messages.MEETING_ENDED})

        if status == zoom.MEETING_STATUS_FAILED:
            # Since the unable to join external meeting issue is so common, we'll handle it separately
            if iResult == zoom.MeetingFailCode.MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING:
                self.send_message_callback(
                    {
                        "message": self.Messages.ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING,
                        "zoom_result_code": iResult,
                    }
                )
            else:
                self.send_message_callback(
                    {
                        "message": self.Messages.ZOOM_MEETING_STATUS_FAILED,
                        "zoom_result_code": iResult,
                    }
                )

        if status == zoom.MEETING_STATUS_INMEETING:
            return self.on_join()

    def create_services(self):
        self.meeting_service = zoom.CreateMeetingService()

        self.setting_service = zoom.CreateSettingService()

        self.meeting_service_event = zoom.MeetingServiceEventCallbacks(onMeetingStatusChangedCallback=self.meeting_status_changed)

        meeting_service_set_revent_result = self.meeting_service.SetEvent(self.meeting_service_event)
        if meeting_service_set_revent_result != zoom.SDKERR_SUCCESS:
            raise Exception("Meeting Service set event failed")

        self.auth_event = zoom.AuthServiceEventCallbacks(onAuthenticationReturnCallback=self.auth_return)

        self.auth_service = zoom.CreateAuthService()

        set_event_result = self.auth_service.SetEvent(self.auth_event)
        logger.info(f"set_event_result = {set_event_result}")

        # Use the auth service
        auth_context = zoom.AuthContext()
        auth_context.jwt_token = self._jwt_token

        result = self.auth_service.SDKAuth(auth_context)

        if result == zoom.SDKError.SDKERR_SUCCESS:
            logger.info("Authentication successful")
        else:
            logger.info(f"Authentication failed with error: {result}")
            self.send_message_callback(
                {
                    "message": self.Messages.ZOOM_SDK_INTERNAL_ERROR,
                    "zoom_result_code": result,
                }
            )

    def get_first_buffer_timestamp_ms_offset(self):
        return 0

    def check_auto_leave_conditions(self):
        if self.requested_leave:
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
            self.last_audio_received_at = time.time()
            logger.info(f"Silence detection activated after {self.automatic_leave_configuration.silence_activate_after_seconds} seconds")

        if self.last_audio_received_at is not None and self.silence_detection_activated:
            if time.time() - self.last_audio_received_at > self.automatic_leave_configuration.silence_timeout_seconds:
                logger.info(f"Auto-leaving meeting because there was no audio message for {self.automatic_leave_configuration.silence_timeout_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_SILENCE})
                return

        if self.joined_at is not None and self.automatic_leave_configuration.max_uptime_seconds is not None:
            if time.time() - self.joined_at > self.automatic_leave_configuration.max_uptime_seconds:
                logger.info(f"Auto-leaving meeting because bot has been running for more than {self.automatic_leave_configuration.max_uptime_seconds} seconds")
                self.send_message_callback({"message": self.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING, "leave_reason": BotAdapter.LEAVE_REASON.AUTO_LEAVE_MAX_UPTIME})
                return

    def is_sent_video_still_playing(self):
        if not self.mp4_demuxer:
            return False
        return self.mp4_demuxer.is_playing()

    def send_video(self, video_url):
        logger.info(f"send_video called with video_url = {video_url}")
        if not self.unmute_webcam():
            return

        if self.mp4_demuxer:
            self.mp4_demuxer.stop()
            self.mp4_demuxer = None

        if self.suggested_video_cap is None:
            logger.info("No suggested video cap. Not sending video.")
            return

        self.current_raw_image_to_send = None

        self.mp4_demuxer = MP4Demuxer(
            url=video_url,
            output_video_dimensions=(self.suggested_video_cap.width, self.suggested_video_cap.height),
            on_video_sample=self.mp4_demuxer_on_video_sample,
            on_audio_sample=self.mp4_demuxer_on_audio_sample,
        )
        self.mp4_demuxer.start()
        return

    def mp4_demuxer_on_video_sample(self, pts, bytes_from_gstreamer):
        if self.requested_leave or self.cleaned_up or (not self.suggested_video_cap):
            self.mp4_demuxer.stop()
            self.mp4_demuxer = None
            return

        self.send_video_frame_to_zoom(bytes_from_gstreamer, self.suggested_video_cap.width, self.suggested_video_cap.height)

    def mp4_demuxer_on_audio_sample(self, pts, bytes_from_gstreamer):
        if self.requested_leave or self.cleaned_up:
            self.mp4_demuxer.stop()
            self.mp4_demuxer = None
            return

        self.send_raw_audio(bytes_from_gstreamer, 8000)

    def get_staged_bot_join_delay_seconds(self):
        return 0

    # These webpage streaming functionality is not available for the zoom native adapter
    def webpage_streamer_get_peer_connection_offer(self):
        pass

    def webpage_streamer_start_peer_connection(self, offer_response):
        pass

    def webpage_streamer_play_bot_output_media_stream(self, output_destination):
        pass

    def webpage_streamer_stop_bot_output_media_stream(self, output_destination):
        pass
