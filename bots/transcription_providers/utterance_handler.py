"""
Abstract interface for handling utterances from transcription providers.

This decouples transcription providers from the specific business logic
of saving utterances and triggering webhooks.
"""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

from bots.models import (
    Participant,
    Recording,
    RecordingStates,
    Utterance,
    WebhookTriggerTypes,
)
from bots.webhook_payloads import utterance_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)


class UtteranceHandler(ABC):
    """
    Abstract base class for handling utterances from transcription providers.

    Transcribers should call handle_utterance() when they detect an
    utterance boundary (e.g., pause in speech, end of sentence).
    """

    @abstractmethod
    def handle_utterance(self, speaker_id: int, transcript_text: str, metadata: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """
        Handle a completed utterance.

        Args:
            speaker_id: Unique identifier for the speaker
            transcript_text: The transcribed text
            metadata: Optional metadata about the utterance
                     (participant info, timestamps, confidence, etc.)
            duration_ms: Duration of the utterance in milliseconds
        """
        pass


class DefaultUtteranceHandler(UtteranceHandler):
    """
    Default implementation that saves utterances to database
    and triggers webhooks.

    This is the production implementation used by the bot controller.
    """

    def __init__(self, bot, get_participant_callback: Callable[[int], Optional[Dict[str, Any]]], sample_rate: int):
        """
        Initialize the utterance handler.

        Args:
            bot: Bot instance for database queries
            get_participant_callback: Function to get participant data
            sample_rate: Audio sample rate for the utterance
        """
        self.bot = bot
        self.get_participant_callback = get_participant_callback
        self.sample_rate = sample_rate

    def handle_utterance(self, speaker_id: int, transcript_text: str, metadata: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """Save utterance to database and trigger webhook."""
        try:
            # Get participant info
            participant_data = self.get_participant_callback(speaker_id)
            if not participant_data:
                logger.warning(f"Could not get participant data for speaker {speaker_id}")
                return

            participant, _ = Participant.objects.get_or_create(
                bot=self.bot,
                uuid=participant_data.get("participant_uuid"),
                defaults={
                    "user_uuid": participant_data.get("participant_user_uuid"),
                    "full_name": participant_data.get("participant_full_name"),
                    "is_the_bot": participant_data.get("participant_is_the_bot", False),
                    "is_host": participant_data.get("participant_is_host", False),
                },
            )

            # Get current recording
            recording = Recording.objects.filter(
                bot=self.bot,
                state=RecordingStates.IN_PROGRESS,
            ).first()

            if not recording:
                logger.warning(f"No recording in progress for bot {self.bot.object_id}")
                return

            # Create unique source UUID for this utterance
            source_uuid = f"{recording.object_id}-{uuid.uuid4()}"

            # Get timestamp from metadata if provided, otherwise use current time
            # For proper video sync, timestamp should be when speech started
            if metadata and "timestamp_ms" in metadata:
                timestamp_ms = metadata["timestamp_ms"]
            else:
                timestamp_ms = int(time.time() * 1000)

            # Create utterance
            utterance, _ = Utterance.objects.update_or_create(
                recording=recording,
                source_uuid=source_uuid,
                defaults={
                    "source": Utterance.Sources.PER_PARTICIPANT_AUDIO,
                    "participant": participant,
                    "transcription": {"transcript": transcript_text},
                    "timestamp_ms": timestamp_ms,
                    "duration_ms": duration_ms,
                    "sample_rate": self.sample_rate,
                },
            )

            # Trigger webhook
            trigger_webhook(
                webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE,
                bot=self.bot,
                payload=utterance_webhook_payload(utterance),
            )

        except Exception as e:
            logger.error(f"Error in utterance handler: {e}")
            import traceback

            logger.error(traceback.format_exc())


class LoggingUtteranceHandler(UtteranceHandler):
    """
    Simple handler that logs utterances without saving to database.

    Useful for debugging, testing, or read-only transcription scenarios.
    """

    def handle_utterance(self, speaker_id: int, transcript_text: str, metadata: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """Log the utterance."""
        logger.info(f"📝 Utterance from speaker {speaker_id}: {transcript_text}")
        if metadata:
            logger.debug(f"   Metadata: {metadata}")
        if duration_ms:
            logger.debug(f"   Duration: {duration_ms}ms")


class CompositeUtteranceHandler(UtteranceHandler):
    """
    Composite handler that chains multiple handlers together.

    Calls each handler in sequence, continuing even if one fails.
    Useful for combining logging, analytics, and database handlers.

    Example:
        handler = CompositeUtteranceHandler([
            LoggingUtteranceHandler(),
            DefaultUtteranceHandler(bot, callback, rate),
            AnalyticsUtteranceHandler(analytics_client),
        ])
    """

    def __init__(self, handlers: list[UtteranceHandler]):
        """
        Initialize with a list of handlers.

        Args:
            handlers: List of UtteranceHandler instances to chain
        """
        self.handlers = handlers

    def handle_utterance(self, speaker_id: int, transcript_text: str, metadata: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """Call all handlers in sequence."""
        for handler in self.handlers:
            try:
                handler.handle_utterance(speaker_id, transcript_text, metadata, duration_ms)
            except Exception as e:
                # Log but continue to next handler
                logger.error(f"Error in {handler.__class__.__name__}: {e}", exc_info=True)


class NoOpUtteranceHandler(UtteranceHandler):
    """
    No-op handler that does nothing.

    Useful for testing or when transcription is needed but
    utterances should not be saved.
    """

    def handle_utterance(self, speaker_id: int, transcript_text: str, metadata: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """Do nothing."""
        pass
