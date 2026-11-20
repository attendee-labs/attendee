import logging

from celery import shared_task

logger = logging.getLogger(__name__)

from bots.models import ThirdPartyTranscriptionJob, ThirdPartyTranscriptionJobStates
from bots.transcription_utils import get_transcription_for_utterance_group_from_terminated_third_party_job, is_retryable_failure


@shared_task(
    bind=True,
    soft_time_limit=300,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def process_utterance_group_for_async_transcription_upon_third_party_job_termination(self, third_party_transcription_job_id):
    third_party_transcription_job = ThirdPartyTranscriptionJob.objects.filter(id=third_party_transcription_job_id).first()
    if third_party_transcription_job is None:
        logger.warning(f"process_utterance_group_for_async_transcription_upon_third_party_job_termination was called for third party transcription job {third_party_transcription_job_id} but it was not found, skipping")
        return

    if third_party_transcription_job.state == ThirdPartyTranscriptionJobStates.PENDING:
        logger.warning(f"process_utterance_group_for_async_transcription_upon_third_party_job_termination was called for third party transcription job {third_party_transcription_job_id} but it is in state {third_party_transcription_job.get_state_display()}, skipping")
        return

    # Utterances in a group are created in order, so ordering by id preserves the order they were concatenated in
    utterances = list(third_party_transcription_job.utterances.order_by("id"))
    if len(utterances) == 0:
        logger.warning(f"process_utterance_group_for_async_transcription_upon_third_party_job_termination was called for third party transcription job {third_party_transcription_job_id} but it has no utterances, skipping")
        return
    utterance_ids = [utterance.id for utterance in utterances]

    # The first utterance in the group will be used to keep track of failure data and attempt count
    # The other utterances will only be written to when the utterance group succeeds or fails
    first_utterance = utterances[0]

    logger.info(f"Processing utterance group for async transcription upon third party job termination {utterance_ids} (third party transcription job {third_party_transcription_job_id})")

    if first_utterance.failure_data:
        logger.info(f"process_utterance_group_for_async_transcription_upon_third_party_job_termination was called for utterances {utterance_ids} but the first utterance has already failed, skipping")
        return

    if first_utterance.transcription is None:
        first_utterance.transcription_attempt_count += 1

        transcriptions, failure_data = get_transcription_for_utterance_group_from_terminated_third_party_job(third_party_transcription_job, utterances)

        if failure_data:
            if first_utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
                first_utterance.save()
                raise Exception(f"Retryable failure when transcribing utterances {utterance_ids} upon third party job termination: {failure_data}")
            else:
                first_utterance.save()
                # Set the failure data for all the utterances
                for utterance in utterances:
                    utterance.failure_data = failure_data
                    utterance.save()
                logger.info(f"Transcription failed for utterances {utterance_ids} upon third party job termination, failure data: {failure_data}")
                return

        # Loop through the utterances and write the transcription to the utterance
        for utterance in utterances:
            utterance.transcription = transcriptions[utterance.id]
            utterance.save()

        logger.info(f"Transcription complete for utterances {utterance_ids} upon third party job termination")
