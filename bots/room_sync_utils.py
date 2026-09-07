"""Helpers for the invisible marker used to identify Attendee room sync bots.

Room sync bots tag their display name with a zero-width space so that other
Attendee room sync bots in the same meeting can recognize them as bots and avoid
mirroring them back into the synced room.
"""

# Zero-width space appended to a room sync bot's display name. It is invisible in
# meeting UIs but lets other Attendee bots identify the participant as a bot.
BOT_INDICATOR = "\u200b"


def add_bot_indicator_to_display_name(display_name):
    """Append the invisible bot indicator to a display name."""
    return f"{display_name}{BOT_INDICATOR}"


def does_participant_name_have_bot_indicator(name):
    """Return True if a participant name carries the invisible bot indicator."""
    if not name:
        return False
    return BOT_INDICATOR in name
