import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from bots.media_capture_health_signals import MediaCaptureHealthSignal

logger = logging.getLogger(__name__)


class MediaCaptureIssueType(Enum):
    CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO = "captions_without_per_participant_audio"


@dataclass(frozen=True)
class MediaCaptureIssue:
    issue_type: MediaCaptureIssueType
    message: str


class MediaCaptureCheck:
    """Looks for one kind of media capture failure in the signals the bot adapter sends."""

    relevant_signals: frozenset = frozenset()

    def record_signal(self, signal: MediaCaptureHealthSignal, timestamp: float):
        raise NotImplementedError

    # Returns an issue if the check has found a failure
    def evaluate(self) -> MediaCaptureIssue | None:
        raise NotImplementedError


class CaptionsWithoutPerParticipantAudioCheck(MediaCaptureCheck):
    """Captions mean someone is speaking, so if captions keep arriving while no per-participant audio is captured, the per-participant audio capture has failed."""

    relevant_signals = frozenset({MediaCaptureHealthSignal.CAPTION_ACTIVITY, MediaCaptureHealthSignal.PER_PARTICIPANT_AUDIO})

    # The adapter sends each signal at most once every 3 minutes, so 3 caption activity signals mean captions were seen across at least 6 minutes
    MIN_CAPTION_ACTIVITY_SIGNALS = 3

    def __init__(self):
        self.caption_activity_timestamps_since_last_per_participant_audio = []
        self.per_participant_audio_was_ever_captured = False

    def record_signal(self, signal: MediaCaptureHealthSignal, timestamp: float):
        if signal == MediaCaptureHealthSignal.CAPTION_ACTIVITY:
            self.caption_activity_timestamps_since_last_per_participant_audio.append(timestamp)
        elif signal == MediaCaptureHealthSignal.PER_PARTICIPANT_AUDIO:
            self.caption_activity_timestamps_since_last_per_participant_audio = []
            self.per_participant_audio_was_ever_captured = True

    def evaluate(self) -> MediaCaptureIssue | None:
        timestamps = self.caption_activity_timestamps_since_last_per_participant_audio
        if len(timestamps) < self.MIN_CAPTION_ACTIVITY_SIGNALS:
            return None

        minutes_of_captions = round((timestamps[-1] - timestamps[0]) / 60)
        if self.per_participant_audio_was_ever_captured:
            audio_history = "Per-participant audio was captured earlier in the meeting, but then stopped."
        else:
            audio_history = "No per-participant audio has been captured in this meeting."
        return MediaCaptureIssue(
            issue_type=MediaCaptureIssueType.CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO,
            message=f"Closed captions were generated over {minutes_of_captions} minutes, but no per-participant audio was captured in that time. {audio_history}",
        )


class MediaCaptureHealthMonitor:
    """Passes the media capture health signals from the bot adapter to the checks, and reports each issue they find. Each check reports at most one issue per bot."""

    def __init__(self, *, checks: list[MediaCaptureCheck], on_issue_detected: Callable[[MediaCaptureIssue], None]):
        self.checks = checks
        self.on_issue_detected = on_issue_detected
        self.last_evaluated_at = None

    # Signals are recorded as they arrive, which is close enough to when they happened for checks that work in minutes
    def record_signal(self, signal: MediaCaptureHealthSignal):
        timestamp = time.time()
        for check in self.checks:
            if signal in check.relevant_signals:
                check.record_signal(signal, timestamp)

    # Called on every main loop timeout, but the signals are minutes apart, so the checks only run every 5 seconds
    def evaluate(self):
        now = time.time()
        if self.last_evaluated_at is not None and now - self.last_evaluated_at < 5:
            return
        self.last_evaluated_at = now

        for check in list(self.checks):
            # This runs in the bot's main loop, where an exception would kill the bot, so a failing check is dropped rather than raised.
            # Dropping it also means the error is only logged once.
            try:
                issue = check.evaluate()
                if issue is None:
                    continue
                logger.info(f"{type(check).__name__} detected a media capture issue: {issue.message}")
                self.checks.remove(check)
                self.on_issue_detected(issue)
            except Exception:
                logger.exception(f"Error running {type(check).__name__}. It will not run again for this bot.")
                if check in self.checks:
                    self.checks.remove(check)
