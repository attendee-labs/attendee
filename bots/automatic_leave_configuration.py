from dataclasses import dataclass


@dataclass(frozen=True)
class AutomaticLeaveConfiguration:
    """Specifies conditions under which the bot will automatically leave a meeting.

    Attributes:
        silence_timeout_seconds: Number of seconds of continuous silence after which the bot should leave
        only_participant_in_meeting_timeout_seconds: Number of seconds to wait before leaving if bot is the only participant (participants with names containing bot_keywords are excluded)
        wait_for_host_to_start_meeting_timeout_seconds: Number of seconds to wait for the host to start the meeting
        silence_activate_after_seconds: Number of seconds to wait before activating the silence timeout
        silence_activate_after_participant_count: Activate the silence timeout once this many participants (excluding bots) are in the meeting at the same time. Silence detection activates when either this or silence_activate_after_seconds is met, whichever comes first (disabled by default)
        silence_pending_log_after_seconds: Number of seconds of continuous silence after which the bot creates an auto_leave_silence_pending log entry, ahead of leaving at silence_timeout_seconds (disabled by default)
        waiting_room_timeout_seconds: Number of seconds to wait before leaving if the bot is in the waiting room
        max_uptime_seconds: Maximum number of seconds that the bot should be running before automatically leaving (infinite by default)
        enable_closed_captions_timeout_seconds: Number of seconds to wait before leaving if bot could not enable closed captions (infinite by default)
        authorized_user_not_in_meeting_timeout_seconds: Number of seconds to wait before leaving if the authorized user is not in the meeting. Only relevant if this is a Zoom bot using the on behalf of token.
        bot_keywords: List of keywords to identify bot participants. A participant is considered a bot if any word in their name matches a keyword (case-insensitive).
    """

    silence_timeout_seconds: int = 600
    silence_activate_after_seconds: int = 1200
    silence_activate_after_participant_count: int | None = None
    silence_pending_log_after_seconds: int | None = None
    only_participant_in_meeting_timeout_seconds: int = 60
    wait_for_host_to_start_meeting_timeout_seconds: int = 600
    waiting_room_timeout_seconds: int = 900
    max_uptime_seconds: int | None = None
    enable_closed_captions_timeout_seconds: int | None = None
    authorized_user_not_in_meeting_timeout_seconds: int = 600
    bot_keywords: list[str] | None = None
