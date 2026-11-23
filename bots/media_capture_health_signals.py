from enum import Enum


class MediaCaptureHealthSignal(Enum):
    """Signals that a bot adapter sends to the bot controller, so it can check that media is being captured."""

    # A closed caption was generated in the meeting for a participant other than the bot
    CAPTION_ACTIVITY = "caption_activity"
    # A per-participant audio frame was captured
    PER_PARTICIPANT_AUDIO = "per_participant_audio"


class MediaCaptureHealthSignalDebouncer:
    """Lets a signal through the first time it is seen, then at most once per interval."""

    def __init__(self, interval_seconds: float):
        self.interval_seconds = interval_seconds
        self.last_emitted_at = {}

    def should_emit(self, signal: MediaCaptureHealthSignal, timestamp: float) -> bool:
        last_emitted_at = self.last_emitted_at.get(signal)
        if last_emitted_at is not None and timestamp - last_emitted_at < self.interval_seconds:
            return False
        self.last_emitted_at[signal] = timestamp
        return True
