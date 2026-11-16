import time


class BotAdapter:
    class Messages:
        LEAVE_MEETING_WAITING_FOR_HOST = "Leave meeting because received waiting for host status"
        LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED = "Leave meeting because waiting room timeout exceeded"
        ZOOM_AUTHORIZATION_FAILED = "Zoom authorization failed"
        ZOOM_MEETING_STATUS_FAILED = "Zoom meeting status failed"
        ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING = "Zoom meeting status failed - unable to join external meeting"
        ZOOM_MEETING_STATUS_FAILED_APP_CAN_NOT_ANONYMOUS_JOIN_MEETING = "Zoom meeting status failed - app can not anonymous join meeting"
        ZOOM_SDK_INTERNAL_ERROR = "Zoom SDK Internal Error"
        BOT_PUT_IN_WAITING_ROOM = "Bot put in waiting room"
        BOT_JOINED_MEETING = "Bot joined meeting"
        BOT_RECORDING_PERMISSION_GRANTED = "Bot recording permission granted"
        MEETING_ENDED = "Meeting ended"
        NEW_UTTERANCE = "New utterance"
        UI_ELEMENT_NOT_FOUND = "UI Element Not Found"
        REQUEST_TO_JOIN_DENIED = "Request to join denied"
        ADAPTER_REQUESTED_BOT_LEAVE_MEETING = "Adapter requested bot leave meeting"
        MEETING_NOT_FOUND = "Meeting not found"
        READY_TO_SHOW_BOT_IMAGE = "Ready to show bot image"
        READY_TO_SEND_CHAT_MESSAGE = "Ready to send chat message"
        BLOCKED_BY_PLATFORM_REPEATEDLY = "Blocked by Platform repeatedly"
        LOGIN_REQUIRED = "Login required"
        LOGIN_ATTEMPT_FAILED = "Login attempt failed"
        COULD_NOT_CONNECT_TO_MEETING = "Could not connect to meeting"
        BLOCKED_BY_CAPTCHA = "Blocked by captcha"
        JOINING_BREAKOUT_ROOM = "Joining breakout room"
        LEAVING_BREAKOUT_ROOM = "Leaving breakout room"
        WEBINAR_BOT_PROMOTED_TO_PANELIST = "Webinar bot promoted to panelist"
        BOT_RECORDING_PERMISSION_DENIED = "Bot recording permission denied"
        AUTHORIZED_USER_NOT_IN_MEETING_TIMEOUT_EXCEEDED = "Authorized user not in meeting timeout exceeded"
        COULD_NOT_ENABLE_CLOSED_CAPTIONS = "Could not enable closed captions"
        AUTO_LEAVE_SILENCE_PENDING = "Auto leave silence pending"
        SAVE_SCREENSHOT_AND_MHTML_FILE = "Save screenshot and mhtml file"

        # App session messages
        APP_SESSION_CONNECTED = "App session connected"
        APP_SESSION_DISCONNECT_REQUESTED = "App session disconnect requested"
        APP_SESSION_DISCONNECTED = "App session disconnected"

    class BOT_RECORDING_PERMISSION_DENIED_REASON:
        HOST_DENIED_PERMISSION = "HOST_DENIED_PERMISSION"
        REQUEST_TIMED_OUT = "REQUEST_TIMED_OUT"
        HOST_CLIENT_CANNOT_GRANT_PERMISSION = "HOST_CLIENT_CANNOT_GRANT_PERMISSION"
        WEBINAR_ATTENDEE_NEEDS_PANELIST_PROMOTION = "WEBINAR_ATTENDEE_NEEDS_PANELIST_PROMOTION"

    class LEAVE_REASON:
        AUTO_LEAVE_SILENCE = "AUTO_LEAVE_SILENCE"
        AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING = "AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING"
        AUTO_LEAVE_MAX_UPTIME = "AUTO_LEAVE_MAX_UPTIME"
        AUTO_LEAVE_COULD_NOT_ENABLE_CLOSED_CAPTIONS = "AUTO_LEAVE_COULD_NOT_ENABLE_CLOSED_CAPTIONS"

    DEBUG_RECORDING_FILE_PATH = "/tmp/debug_screen_recording.mp4"

    def update_participant_media_state(self, participant_uuid, is_unmuted=None, is_webcam_on=None):
        """
        Emits unmute/mute and webcam_on/webcam_off participant events when a participant's media state changes.
        Participants are assumed to start muted with their webcam off, so nothing is emitted for a participant
        who stays muted / webcam off the whole time. Pass None for a value that is unknown.
        """
        from bots.models import ParticipantEventTypes

        if not hasattr(self, "participant_media_states"):
            self.participant_media_states = {}
        state = self.participant_media_states.setdefault(participant_uuid, {"is_unmuted": False, "is_webcam_on": False})

        for key, value, on_event_type, off_event_type in (
            ("is_unmuted", is_unmuted, ParticipantEventTypes.UNMUTE, ParticipantEventTypes.MUTE),
            ("is_webcam_on", is_webcam_on, ParticipantEventTypes.WEBCAM_ON, ParticipantEventTypes.WEBCAM_OFF),
        ):
            if value is None or bool(value) == state[key]:
                continue
            state[key] = bool(value)
            self.add_participant_event_callback({"participant_uuid": participant_uuid, "event_type": on_event_type if value else off_event_type, "event_data": {}, "timestamp_ms": int(time.time() * 1000)})
