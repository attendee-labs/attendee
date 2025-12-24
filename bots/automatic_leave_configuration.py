from dataclasses import dataclass


@dataclass(frozen=True)
class AutomaticLeaveConfiguration:
    """Specifies conditions under which the bot will automatically leave a meeting.

    Attributes:
        silence_timeout_seconds: Number of seconds of continuous silence after which the bot should leave
        only_participant_in_meeting_timeout_seconds: Number of seconds to wait before leaving if bot is the only participant
        wait_for_host_to_start_meeting_timeout_seconds: Number of seconds to wait for the host to start the meeting
        silence_activate_after_seconds: Number of seconds to wait before activating the silence timeout
        waiting_room_timeout_seconds: Number of seconds to wait before leaving if the bot is in the waiting room
        max_uptime_seconds: Maximum number of seconds that the bot should be running before automatically leaving (infinite by default)
        enable_closed_captions_timeout_seconds: Number of seconds to wait before leaving if bot could not enable closed captions (infinite by default)
        authorized_user_not_in_meeting_timeout_seconds: Number of seconds to wait before leaving if the authorized user is not in the meeting. Only relevant if this is a Zoom bot using the on behalf of token.
        only_bots_in_meeting_timeout_seconds: Number of seconds to wait before leaving if only bots (matching only_bots_in_meeting_name_patterns) remain in the meeting
        only_bots_in_meeting_name_patterns: List of regex patterns to identify bot participants. If only participants matching these patterns remain, the auto-leave timer starts.
    """

    silence_timeout_seconds: int = 600
    silence_activate_after_seconds: int = 1200
    only_participant_in_meeting_timeout_seconds: int = 60
    wait_for_host_to_start_meeting_timeout_seconds: int = 600
    waiting_room_timeout_seconds: int = 900
    max_uptime_seconds: int | None = None
    enable_closed_captions_timeout_seconds: int | None = None
    authorized_user_not_in_meeting_timeout_seconds: int = 600
    only_bots_in_meeting_timeout_seconds: int = 300
    only_bots_in_meeting_name_patterns: list[str] | None = None
