import logging

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)

from bots.models import TranscriptionFailureReasons, Utterance
from bots.transcription_utils import get_transcription_for_utterance_group, is_retryable_failure

MAX_UTTERANCE_GROUP_ATTEMPTS = 5


class RetryableUtteranceGroupTranscriptionError(Exception):
    """Raised to trigger Celery retry for a retryable utterance-group transcription failure."""


@shared_task(
    bind=True,
    soft_time_limit=3600,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def process_utterance_group_for_async_transcription(self, utterance_ids):
    if len(utterance_ids) == 0:
        logger.warning("process_utterance_group_for_async_transcription was called with no utterance IDs, skipping")
        return

    # The first utterance in the group will be used to keep track of failure data and attempt count
    # The other utterances will only be written to when the utterance group succeeds or fails
    utterances = list(Utterance.objects.filter(id__in=utterance_ids))
    if len(utterances) != len(utterance_ids):
        logger.warning(f"process_utterance_group_for_async_transcription was called for utterances {utterance_ids} but some utterances were not found, skipping")
        return
    # Make sure the utterances are in order according to the utterance ids
    utterances = sorted(utterances, key=lambda x: utterance_ids.index(x.id))
    first_utterance = utterances[0]

    logger.info(f"Processing utterance group for async transcription {utterance_ids}")

    # Group is fully settled — nothing to do
    if all(u.transcription is not None or u.failure_data is not None for u in utterances):
        logger.info(f"process_utterance_group_for_async_transcription was called for utterances {utterance_ids} but all utterances are already complete, skipping")
        return

    # If the first utterance already has failure_data, propagate to any siblings still incomplete
    # so the group is not left half-failed forever.
    if first_utterance.failure_data:
        incomplete = [u for u in utterances if u.transcription is None and u.failure_data is None]
        if incomplete:
            with transaction.atomic():
                for utterance in incomplete:
                    utterance.failure_data = first_utterance.failure_data
                    utterance.save(update_fields=["failure_data"])
        logger.info(f"process_utterance_group_for_async_transcription was called for utterances {utterance_ids} but the first utterance has already failed, skipping")
        return

    # Heal partial writes from a prior crash (some transcriptions saved, others not).
    # Clear partial success so the group can be retried as a unit.
    if any(u.transcription is not None for u in utterances):
        logger.warning(f"Partial transcription write detected for utterances {utterance_ids}; clearing partial results for retry")
        with transaction.atomic():
            for utterance in utterances:
                if utterance.transcription is not None and utterance.failure_data is None:
                    utterance.transcription = None
                    utterance.save(update_fields=["transcription"])
        for utterance in utterances:
            utterance.refresh_from_db()

    try:
        first_utterance.transcription_attempt_count += 1

        transcriptions, failure_data = get_transcription_for_utterance_group(utterances)

        if failure_data:
            if first_utterance.transcription_attempt_count < MAX_UTTERANCE_GROUP_ATTEMPTS and is_retryable_failure(failure_data):
                first_utterance.save()
                raise RetryableUtteranceGroupTranscriptionError(f"Retryable failure when transcribing utterances {utterance_ids}: {failure_data}")

            with transaction.atomic():
                first_utterance.save()
                for utterance in utterances:
                    utterance.failure_data = failure_data
                    utterance.save()
            logger.info(f"Transcription failed for utterances {utterance_ids}, failure data: {failure_data}")
            return

        # Persist all transcriptions atomically so a crash cannot leave the group half-written.
        with transaction.atomic():
            for utterance in utterances:
                utterance.transcription = transcriptions[utterance.id]
                utterance.save()

        logger.info(f"Transcription complete for utterances {utterance_ids}")

    except RetryableUtteranceGroupTranscriptionError:
        raise

    except Exception as e:
        logger.exception(f"Unexpected failure processing utterance group {utterance_ids}: {e}")
        # Persist attempt count so retries are bounded even if we crashed mid-save.
        Utterance.objects.filter(id=first_utterance.id).update(transcription_attempt_count=first_utterance.transcription_attempt_count)

        if first_utterance.transcription_attempt_count < MAX_UTTERANCE_GROUP_ATTEMPTS:
            # Leave utterances incomplete (no failure_data) so Celery retry can recover.
            raise

        failure_data = {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}
        with transaction.atomic():
            for utterance in utterances:
                utterance.refresh_from_db()
                utterance.failure_data = failure_data
                utterance.save(update_fields=["failure_data"])
        logger.info(f"Marked utterance group {utterance_ids} as failed after unexpected error")
