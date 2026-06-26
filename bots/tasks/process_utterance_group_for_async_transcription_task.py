import logging

from celery import shared_task

logger = logging.getLogger(__name__)

from bots.models import Utterance
from bots.transcription_utils import get_transcription_for_utterance_group, is_retryable_failure


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
    first_utterance = Utterance.objects.get(id=utterance_ids[0])
    utterances = Utterance.objects.filter(id__in=utterance_ids).all()
    if len(utterances) != len(utterance_ids):
        logger.warning(f"process_utterance_group_for_async_transcription was called for utterances {utterance_ids} but some utterances were not found, skipping")
        return
    # Make sure the utterances are in order according to the utterance ids
    utterances = sorted(utterances, key=lambda x: utterance_ids.index(x.id))

    logger.info(f"Processing utterance group for async transcription {utterance_ids}")

    if first_utterance.failure_data:
        logger.info(f"process_utterance_group_for_async_transcription was called for utterances {utterance_ids} but the first utterance has already failed, skipping")
        return

    if first_utterance.transcription is None:
        first_utterance.transcription_attempt_count += 1

        transcriptions, failure_data = get_transcription_for_utterance_group(utterances)

        if failure_data:
            if first_utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
                first_utterance.save()
                raise Exception(f"Retryable failure when transcribing utterances {utterance_ids}: {failure_data}")
            else:
                first_utterance.save()
                # Set the failure data for all the utterances
                for utterance in utterances:
                    utterance.failure_data = failure_data
                    utterance.save()
                logger.info(f"Transcription failed for utterances {utterance_ids}, failure data: {failure_data}")
                return

        # Loop through the utterances and write the transcription to the utterance
        for utterance in utterances:
            utterance.transcription = transcriptions[utterance.id]
            utterance.save()

        logger.info(f"Transcription complete for utterances {utterance_ids}")
