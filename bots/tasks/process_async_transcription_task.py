import logging
import math
import os

from celery import shared_task
from django.utils import timezone

from bots.models import AsyncTranscription, AsyncTranscriptionManager, AsyncTranscriptionStates, AsyncTranscriptionStrategies, ParticipantEvent, ParticipantEventTypes, TranscriptionFailureReasons, Utterance
from bots.tasks.process_utterance_group_for_async_transcription_task import process_utterance_group_for_async_transcription
from bots.tasks.process_utterance_task import process_utterance
from bots.transcription_utils import get_transcription_via_assemblyai_for_speaker_events

logger = logging.getLogger(__name__)


def create_utterances_for_transcription(async_transcription):
    if async_transcription.transcription_settings.strategy() == AsyncTranscriptionStrategies.SPEAKER_EVENTS:
        return create_utterances_for_transcription_with_speaker_events(async_transcription)

    return create_utterances_for_transcription_with_per_speaker_audio(async_transcription)


def create_utterances_for_transcription_with_speaker_events(async_transcription):
    AsyncTranscriptionManager.set_async_transcription_in_progress(async_transcription)

    recording = async_transcription.recording

    speaker_events = list(
        ParticipantEvent.objects.filter(
            participant__bot=recording.bot,
            event_type__in=[ParticipantEventTypes.SPEECH_START, ParticipantEventTypes.SPEECH_STOP],
        ).select_related("participant")
    )

    utterance_results, error = get_transcription_via_assemblyai_for_speaker_events(
        speaker_events=speaker_events,
        recording=recording,
        transcription_settings=async_transcription.transcription_settings,
    )

    if error:
        first_participant = recording.bot.participants.first()
        if first_participant:
            Utterance.objects.create(
                source=Utterance.Sources.MIXED_AUDIO,
                recording=recording,
                async_transcription=async_transcription,
                participant=first_participant,
                timestamp_ms=0,
                duration_ms=0,
                failure_data=error,
            )
        AsyncTranscriptionManager.set_async_transcription_failed(async_transcription, failure_data={})
    else:
        for result in utterance_results:
            participant = result["participant"]
            transcription = result["transcription"]
            start_time = result["start_time"]
            duration = result["duration"]
            words = transcription.get("words", [])

            if not words:
                continue

            Utterance.objects.create(
                source=Utterance.Sources.MIXED_AUDIO,
                recording=recording,
                async_transcription=async_transcription,
                participant=participant,
                timestamp_ms=start_time * 1000 + recording.first_buffer_timestamp_ms,
                duration_ms=duration * 1000,
                transcription=transcription,
            )

        AsyncTranscriptionManager.set_async_transcription_complete(async_transcription)


def create_utterances_for_transcription_with_per_speaker_audio(async_transcription):
    if async_transcription.use_grouped_utterances:
        return create_utterances_for_transcription_with_per_speaker_audio_using_groups(async_transcription)

    return create_utterances_for_transcription_with_per_speaker_audio_without_using_groups(async_transcription)


def create_utterances_for_transcription_with_per_speaker_audio_without_using_groups(async_transcription):
    recording = async_transcription.recording

    # Get all the audio chunks for the recording
    # then create utterances for each audio chunk
    # Do NOT load the audio blob field, because it's not needed and can consume significant memory
    utterance_task_delay_seconds = 0
    for audio_chunk in recording.audio_chunks.defer("audio_blob").all():
        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            recording=recording,
            async_transcription=async_transcription,
            participant_id=audio_chunk.participant_id,
            audio_chunk=audio_chunk,
            timestamp_ms=audio_chunk.timestamp_ms,
            duration_ms=audio_chunk.duration_ms,
        )

        # Spread out the utterance tasks a bit
        process_utterance.apply_async(args=[utterance.id], countdown=utterance_task_delay_seconds)
        utterance_task_delay_seconds += 1

    # After the utterances have been created and queued for transcription, set the recording artifact to in progress
    AsyncTranscriptionManager.set_async_transcription_in_progress(async_transcription)


def create_utterances_for_transcription_with_per_speaker_audio_using_groups(async_transcription):
    recording = async_transcription.recording

    # Get all the audio chunks for the recording, sorted by start time
    # Use defer() to exclude large audio_blob field
    audio_chunks = list(recording.audio_chunks.defer("audio_blob").order_by("timestamp_ms"))

    # Create all utterances first
    utterances = []
    for audio_chunk in audio_chunks:
        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            recording=recording,
            async_transcription=async_transcription,
            participant_id=audio_chunk.participant_id,
            audio_chunk=audio_chunk,
            timestamp_ms=audio_chunk.timestamp_ms,
            duration_ms=audio_chunk.duration_ms,
        )
        utterances.append(utterance)

    # Group utterances into evenly-sized groups based on total duration
    # Calculate number of groups needed, then divide duration evenly.
    # This avoids creating tiny groups.
    max_group_duration_ms = 30 * 60 * 1000  # 30 minutes in milliseconds
    total_duration_ms = sum(u.duration_ms for u in utterances)
    if total_duration_ms == 0:
        total_duration_ms = 1

    num_groups = math.ceil(total_duration_ms / max_group_duration_ms)
    target_group_duration_ms = total_duration_ms / num_groups

    groups = []
    current_group = []
    current_group_duration_ms = 0

    for utterance in utterances:
        current_group.append(utterance)
        current_group_duration_ms += utterance.duration_ms

        # Start a new group if we've reached the target duration (unless this is the last group)
        if current_group_duration_ms >= target_group_duration_ms and len(groups) < num_groups - 1:
            groups.append(current_group)
            current_group = []
            current_group_duration_ms = 0

    # Don't forget the last group
    if current_group:
        groups.append(current_group)

    # Log all the group total durations
    for group_index, group in enumerate(groups):
        logger.info(f"Group {group_index} total duration: {sum(u.duration_ms for u in group)}ms")

    # Queue each group for processing
    group_task_delay_seconds = 0
    for group in groups:
        utterance_ids = [u.id for u in group]
        process_utterance_group_for_async_transcription.apply_async(args=[utterance_ids], countdown=group_task_delay_seconds)
        group_task_delay_seconds += 1

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
    next_check_wait_time_seconds = int(os.getenv("ASYNC_TRANSCRIPTION_CHECK_INTERVAL_SECONDS", 60))
    logger.info(f"Checking for transcription completion for recording artifact {async_transcription.id} again in {next_check_wait_time_seconds} seconds")
    process_async_transcription.apply_async(args=[async_transcription.id], countdown=next_check_wait_time_seconds)


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
