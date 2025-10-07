class BotAdapter:
    class Messages:
        LEAVE_MEETING_WAITING_FOR_HOST = "Leave meeting because received waiting for host status"
        LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED = "Leave meeting because waiting room timeout exceeded"
        ZOOM_AUTHORIZATION_FAILED = "Zoom authorization failed"
        ZOOM_MEETING_STATUS_FAILED = "Zoom meeting status failed"
        ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING = "Zoom meeting status failed - unable to join external meeting"
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
        JOINING_BREAKOUT_ROOM = "Joining breakout room"
        LEAVING_BREAKOUT_ROOM = "Leaving breakout room"
        BOT_RECORDING_PERMISSION_DENIED = "Bot recording permission denied"

    class BOT_RECORDING_PERMISSION_DENIED_REASON:
        HOST_DENIED_PERMISSION = "HOST_DENIED_PERMISSION"
        REQUEST_TIMED_OUT = "REQUEST_TIMED_OUT"
        HOST_CLIENT_CANNOT_GRANT_PERMISSION = "HOST_CLIENT_CANNOT_GRANT_PERMISSION"

    class LEAVE_REASON:
        AUTO_LEAVE_SILENCE = "AUTO_LEAVE_SILENCE"
        AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING = "AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING"
        AUTO_LEAVE_MAX_UPTIME = "AUTO_LEAVE_MAX_UPTIME"

    DEBUG_RECORDING_FILE_PATH = "/tmp/debug_screen_recording.mp4"
