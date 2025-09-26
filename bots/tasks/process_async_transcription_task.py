import logging

from celery import shared_task
from django.utils import timezone

from bots.models import AsyncTranscription, AsyncTranscriptionManager, AsyncTranscriptionStates, TranscriptionFailureReasons, Utterance
from bots.tasks.process_utterance_task import process_utterance

logger = logging.getLogger(__name__)

import time

import requests

import bots.mixed_audio_diarization_utils as mixed_audio_diarization_utils
from bots.models import Credentials, ParticipantEvent, ParticipantEventTypes


def create_utterance_for_audio_chunk_based_transcription(async_transcription):
    recording = async_transcription.recording
    # Get all the audio chunks for the recording
    # then create utterances for each audio chunk
    utterance_task_delay_seconds = 0
    for audio_chunk in recording.audio_chunks.all():
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


def transcribe_recording_via_assemblyai(async_transcription):
    recording = async_transcription.recording
    transcription_settings = async_transcription.transcription_settings
    assemblyai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()
    if not assemblyai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    assemblyai_credentials = assemblyai_credentials_record.get_credentials()
    if not assemblyai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = assemblyai_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    headers = {"authorization": api_key}
    base_url = transcription_settings.assemblyai_base_url()

    data = {
        "audio_url": recording.url,
        "speech_model": "universal",
    }

    if transcription_settings.assembly_ai_language_detection():
        data["language_detection"] = True
    elif transcription_settings.assembly_ai_language_code():
        data["language_code"] = transcription_settings.assembly_ai_language_code()

    # Add keyterms_prompt and speech_model if set
    keyterms_prompt = transcription_settings.assemblyai_keyterms_prompt()
    if keyterms_prompt:
        data["keyterms_prompt"] = keyterms_prompt
    speech_model = transcription_settings.assemblyai_speech_model()
    if speech_model:
        data["speech_model"] = speech_model

    if transcription_settings.assemblyai_speaker_labels():
        data["speaker_labels"] = True

    language_detection_options = transcription_settings.assemblyai_language_detection_options()
    if language_detection_options:
        data["language_detection_options"] = language_detection_options

    url = f"{base_url}/transcript"
    response = requests.post(url, json=data, headers=headers)

    if response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "text": response.text}

    transcript_id = response.json()["id"]
    polling_endpoint = f"{base_url}/transcript/{transcript_id}"

    # Poll the result_url until we get a completed transcription
    max_retries = 120  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        polling_response = requests.get(polling_endpoint, headers=headers)

        if polling_response.status_code != 200:
            logger.error(f"AssemblyAI result fetch failed with status code {polling_response.status_code}")
            time.sleep(10)
            retry_count += 10
            continue

        transcription_result = polling_response.json()

        if transcription_result["status"] == "completed":
            logger.info("AssemblyAI transcription completed successfully, now deleting from AssemblyAI.")

            # Delete the transcript from AssemblyAI
            delete_response = requests.delete(polling_endpoint, headers=headers)
            if delete_response.status_code != 200:
                logger.error(f"AssemblyAI delete failed with status code {delete_response.status_code}: {delete_response.text}")
            else:
                logger.info("AssemblyAI delete successful")

            return transcription_result, None

        elif transcription_result["status"] == "error":
            error = transcription_result.get("error")

            if error and "language_detection cannot be performed on files with no spoken audio" in error:
                logger.info(f"AssemblyAI transcription skipped for async transcription {async_transcription.id} because it did not have any spoken audio and we tried to detect language")
                return {"transcript": "", "words": []}, None

            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error": error}

        else:  # queued, processing
            logger.info(f"AssemblyAI transcription status: {transcription_result['status']}, waiting...")
            time.sleep(1)
            retry_count += 1

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}


def create_utterances_for_mixed_audio_based_transcription(async_transcription):
    # transcription, failure_data = transcribe_recording_via_assemblyai(async_transcription)
    # if failure_data:
    #   AsyncTranscriptionManager.set_async_transcription_failed(async_transcription, failure_data={"reason": "bad"})
    #   return

    words = [
        {"text": "Let's", "start": 480, "end": 760, "confidence": 0.81152344, "speaker": None},
        {"text": "say", "start": 760, "end": 1040, "confidence": 0.70214844, "speaker": None},
        {"text": "varnish.", "start": 6480, "end": 7200, "confidence": 0.8463542, "speaker": None},
        {"text": "Orange.", "start": 8480, "end": 9120, "confidence": 0.9938965, "speaker": None},
        {"text": "Then", "start": 28970, "end": 29130, "confidence": 0.8046875, "speaker": None},
        {"text": "I'll", "start": 29130, "end": 29370, "confidence": 0.99934894, "speaker": None},
        {"text": "take.", "start": 29370, "end": 29610, "confidence": 0.99121094, "speaker": None},
        {"text": "Then", "start": 35050, "end": 35370, "confidence": 0.42895508, "speaker": None},
        {"text": "I'll", "start": 35370, "end": 35730, "confidence": 0.9921875, "speaker": None},
        {"text": "take.", "start": 35730, "end": 36010, "confidence": 0.99121094, "speaker": None},
        {"text": "My", "start": 46650, "end": 46970, "confidence": 0.9980469, "speaker": None},
        {"text": "name", "start": 46970, "end": 47210, "confidence": 0.9970703, "speaker": None},
        {"text": "is", "start": 47210, "end": 47450, "confidence": 0.9482422, "speaker": None},
        {"text": "John", "start": 47450, "end": 47770, "confidence": 0.92871094, "speaker": None},
        {"text": "Pettigrew.", "start": 47770, "end": 48650, "confidence": 0.94677734, "speaker": None},
        {"text": "I'm", "start": 50570, "end": 50930, "confidence": 0.9371745, "speaker": None},
        {"text": "the", "start": 50930, "end": 51090, "confidence": 0.9995117, "speaker": None},
        {"text": "other", "start": 51090, "end": 51330, "confidence": 0.96484375, "speaker": None},
        {"text": "person.", "start": 51330, "end": 51690, "confidence": 0.9848633, "speaker": None},
        {"text": "My", "start": 51770, "end": 52010, "confidence": 0.99902344, "speaker": None},
        {"text": "name", "start": 52010, "end": 52130, "confidence": 0.9995117, "speaker": None},
        {"text": "is", "start": 52130, "end": 52330, "confidence": 0.99902344, "speaker": None},
        {"text": "yt.", "start": 52330, "end": 52890, "confidence": 0.89575195, "speaker": None},
        {"text": "I'm", "start": 57210, "end": 57370, "confidence": 0.92285156, "speaker": None},
        {"text": "the", "start": 57370, "end": 57490, "confidence": 0.99853516, "speaker": None},
        {"text": "original", "start": 57490, "end": 57890, "confidence": 0.9250488, "speaker": None},
        {"text": "guy.", "start": 57890, "end": 58290, "confidence": 0.99902344, "speaker": None},
        {"text": "My", "start": 58290, "end": 58530, "confidence": 0.9995117, "speaker": None},
        {"text": "name's", "start": 58530, "end": 58770, "confidence": 0.8175456, "speaker": None},
        {"text": "Noah", "start": 58770, "end": 59090, "confidence": 0.9020996, "speaker": None},
        {"text": "Duncan.", "start": 59090, "end": 59610, "confidence": 0.8338623, "speaker": None},
        {"text": "I'm", "start": 60170, "end": 60530, "confidence": 0.9983724, "speaker": None},
        {"text": "pretty", "start": 60530, "end": 60730, "confidence": 1.0, "speaker": None},
        {"text": "cool.", "start": 60730, "end": 61050, "confidence": 0.99975586, "speaker": None},
        {"text": "People", "start": 61930, "end": 62330, "confidence": 0.9980469, "speaker": None},
        {"text": "call", "start": 62330, "end": 62610, "confidence": 0.99609375, "speaker": None},
        {"text": "me", "start": 62610, "end": 62810, "confidence": 0.99902344, "speaker": None},
        {"text": "Charles.", "start": 62810, "end": 63370, "confidence": 0.9880371, "speaker": None},
        {"text": "My", "start": 66490, "end": 66770, "confidence": 0.9868164, "speaker": None},
        {"text": "name's", "start": 66770, "end": 67210, "confidence": 0.82682294, "speaker": None},
        {"text": "the", "start": 67850, "end": 68130, "confidence": 0.55908203, "speaker": None},
        {"text": "other", "start": 68130, "end": 68330, "confidence": 0.98779297, "speaker": None},
        {"text": "guy.", "start": 68330, "end": 68650, "confidence": 0.99975586, "speaker": None},
        {"text": "I'm", "start": 68650, "end": 68930, "confidence": 0.8959961, "speaker": None},
        {"text": "kind", "start": 68930, "end": 69129, "confidence": 1.0, "speaker": None},
        {"text": "of", "start": 69129, "end": 69290, "confidence": 0.9951172, "speaker": None},
        {"text": "weird.", "start": 69290, "end": 69610, "confidence": 0.87386066, "speaker": None},
        {"text": "Most", "start": 70170, "end": 70530, "confidence": 0.86279297, "speaker": None},
        {"text": "people", "start": 70530, "end": 70850, "confidence": 1.0, "speaker": None},
        {"text": "like", "start": 70850, "end": 71130, "confidence": 0.99365234, "speaker": None},
        {"text": "me", "start": 71130, "end": 71450, "confidence": 0.9995117, "speaker": None},
        {"text": "well", "start": 71450, "end": 71730, "confidence": 0.92578125, "speaker": None},
        {"text": "enough.", "start": 71730, "end": 72010, "confidence": 0.9897461, "speaker": None},
        {"text": "It.", "start": 78820, "end": 79060, "confidence": 0.7402344, "speaker": None},
    ]

    speech_start_events = ParticipantEvent.objects.filter(participant__bot=async_transcription.recording.bot).filter(event_type=ParticipantEventTypes.SPEECH_START).order_by("timestamp_ms").all()
    first_buffer_timestamp_ms = async_transcription.recording.first_buffer_timestamp_ms

    words_in_memory = list(map(lambda x: mixed_audio_diarization_utils.Word(x["text"], x["start"], x["end"]), words))
    speech_start_events_in_memory = list(map(lambda x: mixed_audio_diarization_utils.SpeechStartEvent(x.participant_id, x.timestamp_ms), speech_start_events))
    diarization = mixed_audio_diarization_utils.diarize_words(words_in_memory, speech_start_events_in_memory, first_buffer_timestamp_ms + 1000)

    participant_id_to_participant = {p.id: p for p in async_transcription.recording.bot.participants.all()}

    current_word_group = {
        "word_indices": [],
        "participant": None,
    }
    word_groups = []

    for diarized_word_index in range(len(diarization)):
        previous_word = diarization[diarized_word_index - 1] if diarized_word_index > 0 else None
        word = diarization[diarized_word_index]

        if previous_word and previous_word.participant_id != word.participant_id:
            word_groups.append(current_word_group)
            current_word_group = {
                "word_indices": [],
                "participant": participant_id_to_participant[word.participant_id],
            }

        current_word_group["word_indices"].append(word.original_word_index)
        current_word_group["participant"] = participant_id_to_participant[word.participant_id]

    if current_word_group["participant"]:
        word_groups.append(current_word_group)

    for word_group in word_groups:
        Utterance.objects.create(
            source=Utterance.Sources.MIXED_AUDIO,
            recording=async_transcription.recording,
            async_transcription=async_transcription,
            participant=word_group["participant"],
            timestamp_ms=words[word_group["word_indices"][0]]["start"] + first_buffer_timestamp_ms,
            duration_ms=words[word_group["word_indices"][-1]]["end"] - words[word_group["word_indices"][0]]["start"],
            transcription={
                "transcript": " ".join([words[i]["text"] for i in word_group["word_indices"]]),
                "words": [
                    {
                        "word": words[i]["text"],
                        "start": words[i]["start"] - words[word_group["word_indices"][0]]["start"],
                        "end": words[i]["end"] - words[word_group["word_indices"][0]]["start"],
                    }
                    for i in word_group["word_indices"]
                ],
            },
        )


def create_utterances_for_transcription(async_transcription):
    if async_transcription.transcription_settings.requires_audio_chunks():
        create_utterance_for_audio_chunk_based_transcription(async_transcription)
    else:
        create_utterances_for_mixed_audio_based_transcription(async_transcription)

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
