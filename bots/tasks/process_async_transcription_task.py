import logging

from celery import shared_task
from django.utils import timezone

from bots.models import AsyncTranscription, AsyncTranscriptionManager, AsyncTranscriptionStates, TranscriptionFailureReasons, Utterance
from bots.tasks.process_utterance_task import process_utterance

logger = logging.getLogger(__name__)


def create_utterances_for_transcription(async_transcription):
    recording = async_transcription.recording

    # Get all the audio chunks for the recording
    # then create utterances for each audio chunk
    # Use defer() to exclude large audio_blob field and iterator() to stream results
    # instead of loading all chunks into memory at once (can be >1GB of audio data)
    utterance_task_delay_seconds = 0
    for audio_chunk in recording.audio_chunks.defer("audio_blob").iterator(chunk_size=100):
        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            recording=recording,
            async_transcription=async_transcription,
            participant=audio_chunk.participant,
            audio_chunk=audio_chunk,
            timestamp_ms=audio_chunk.timestamp_ms,
            duration_ms=audio_chunk.duration_ms,
        )

        # Spread out the utterance tasks a bit
        process_utterance.apply_async(args=[utterance.id], countdown=utterance_task_delay_seconds)
        utterance_task_delay_seconds += 1

    # After the utterances have been created and queued for transcription, set the recording artifact to in progress
    AsyncTranscriptionManager.set_async_transcription_in_progress(async_transcription)


def terminate_transcription(async_transcription):
    # We'll mark it as failed if there are any failed utterances or any in progress utterances
    any_in_progress_utterances = async_transcription.utterances.filter(transcription__isnull=True, failure_data__isnull=True).exists()
    any_failed_utterances = async_transcription.utterances.filter(failure_data__isnull=False).exists()
    if any_failed_utterances or any_in_progress_utterances:
        failure_reasons = list(async_transcription.utterances.filter(failure_data__has_key="reason").values_list("failure_data__reason", flat=True).distinct())
        if any_in_progress_utterances:
            failure_reasons.append(TranscriptionFailureReasons.UTTERANCES_STILL_IN_PROGRESS_WHEN_TRANSCRIPTION_TERMINATED)
        AsyncTranscriptionManager.set_async_transcription_failed(async_transcription, failure_data={"failure_reasons": failure_reasons})
    else:
        AsyncTranscriptionManager.set_async_transcription_complete(async_transcription)


def check_for_transcription_completion(async_transcription):
    in_progress_utterances = async_transcription.utterances.filter(transcription__isnull=True, failure_data__isnull=True)

    # If no in progress utterances exist or it's been more than max_runtime_seconds, then we need to terminate the transcription
    max_runtime_seconds = max(1800, async_transcription.utterances.count() * 3)
    if not in_progress_utterances.exists() or timezone.now() - async_transcription.started_at > timezone.timedelta(seconds=max_runtime_seconds):
        logger.info(f"Terminating transcription for recording artifact {async_transcription.id} because no in progress utterances exist or it's been more than 30 minutes")
        terminate_transcription(async_transcription)
        return

    # An in progress utterance exists and we haven't timed out, so we need to check again in 1 minute
    logger.info(f"Checking for transcription completion for recording artifact {async_transcription.id} again in 1 minute")
    process_async_transcription.apply_async(args=[async_transcription.id], countdown=60)


@shared_task(
    bind=True,
    soft_time_limit=3600,
)
def process_async_transcription(self, async_transcription_id):
    async_transcription = AsyncTranscription.objects.get(id=async_transcription_id)

    try:
        if async_transcription.state == AsyncTranscriptionStates.COMPLETE or async_transcription.state == AsyncTranscriptionStates.FAILED:
            return

        if async_transcription.state == AsyncTranscriptionStates.NOT_STARTED:
            create_utterances_for_transcription(async_transcription)

        check_for_transcription_completion(async_transcription)

    except Exception as e:
        logger.exception(f"Unexpected exception in process_async_transcription: {str(e)}")
        AsyncTranscriptionManager.set_async_transcription_failed(async_transcription, failure_data={})
