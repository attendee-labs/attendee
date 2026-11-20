import logging

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)

from bots.models import ThirdPartyTranscriptionJob, Utterance
from bots.transcription_utils import (
    create_third_party_transcription_job_for_utterance_group,
    get_transcription_for_utterance_group,
    is_retryable_failure,
    should_create_third_party_transcription_job_for_utterance_group,
)


def handle_transcription_failure(first_utterance, utterances, failure_data):
    utterance_ids = [utterance.id for utterance in utterances]
    if first_utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
        first_utterance.save()
        raise Exception(f"Retryable failure when transcribing utterances {utterance_ids}: {failure_data}")

    first_utterance.save()
    # Set the failure data for all the utterances
    for utterance in utterances:
        utterance.failure_data = failure_data
        utterance.save()
    logger.info(f"Transcription failed for utterances {utterance_ids}, failure data: {failure_data}")


# The transcriptions will be written when the third party job terminates
def create_third_party_transcription_job_and_save_to_db(first_utterance, utterances):
    provider_job_id, failure_data = create_third_party_transcription_job_for_utterance_group(utterances)
    if failure_data:
        handle_transcription_failure(first_utterance, utterances, failure_data)
        return

    with transaction.atomic():
        third_party_transcription_job = ThirdPartyTranscriptionJob.objects.create(
            recording=first_utterance.recording,
            provider=first_utterance.transcription_provider,
            provider_job_id=provider_job_id,
        )
        third_party_transcription_job.utterances.set(utterances)

    logger.info(f"Created third party transcription job {third_party_transcription_job.id} for utterances {[utterance.id for utterance in utterances]}")


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

        if should_create_third_party_transcription_job_for_utterance_group(utterances):
            create_third_party_transcription_job_and_save_to_db(first_utterance, utterances)
            return

        transcriptions, failure_data = get_transcription_for_utterance_group(utterances)
        if failure_data:
            handle_transcription_failure(first_utterance, utterances, failure_data)
            return

        # Loop through the utterances and write the transcription to the utterance
        for utterance in utterances:
            utterance.transcription = transcriptions[utterance.id]
            utterance.save()

        logger.info(f"Transcription complete for utterances {utterance_ids}")
