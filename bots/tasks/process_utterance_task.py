import json
import logging
import os
import time

import requests
from celery import shared_task

logger = logging.getLogger(__name__)

from bots.models import Credentials, RecordingManager, TranscriptionFailureReasons, TranscriptionProviders, Utterance, WebhookTriggerTypes
from bots.utils import pcm_to_mp3
from bots.webhook_payloads import utterance_webhook_payload
from bots.webhook_utils import trigger_webhook


def is_retryable_failure(failure_data):
    return failure_data.get("reason") in [
        TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED,
        TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
        TranscriptionFailureReasons.TIMED_OUT,
        TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED,
        TranscriptionFailureReasons.INTERNAL_ERROR,
    ]


def get_transcription(utterance):
    try:
        # Regular transcription providers that support async transcription
        if utterance.transcription_provider == TranscriptionProviders.DEEPGRAM:
            transcription, failure_data = get_transcription_via_deepgram(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.GLADIA:
            transcription, failure_data = get_transcription_via_gladia(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.OPENAI:
            transcription, failure_data = get_transcription_via_openai(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.ASSEMBLY_AI:
            transcription, failure_data = get_transcription_via_assemblyai(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.SARVAM:
            transcription, failure_data = get_transcription_via_sarvam(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.ELEVENLABS:
            transcription, failure_data = get_transcription_via_elevenlabs(utterance)
        else:
            raise Exception(f"Unknown or streaming-only transcription provider: {utterance.transcription_provider}")

        return transcription, failure_data
    except Exception as e:
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


@shared_task(
    bind=True,
    soft_time_limit=3600,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def process_utterance(self, utterance_id):
    utterance = Utterance.objects.get(id=utterance_id)
    logger.info(f"Processing utterance {utterance_id}")

    recording = utterance.recording

    if utterance.failure_data:
        logger.info(f"process_utterance was called for utterance {utterance_id} but it has already failed, skipping")
        return

    if utterance.transcription is None:
        utterance.transcription_attempt_count += 1

        transcription, failure_data = get_transcription(utterance)

        if failure_data:
            if utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
                utterance.save()
                raise Exception(f"Retryable failure when transcribing utterance {utterance_id}: {failure_data}")
            else:
                # Keep the audio blob around if it fails
                utterance.failure_data = failure_data
                utterance.save()
                logger.info(f"Transcription failed for utterance {utterance_id}, failure data: {failure_data}")
                return

        # The direct audio_blob column on the utterance model is deprecated, but for backwards compatibility, we need to clear it if it exists
        if utterance.audio_blob:
            utterance.audio_blob = b""  # set the audio blob binary field to empty byte string

        # If the utterance has an associated audio chunk, clear the audio blob on the audio chunk.
        # If async transcription data is being saved, do NOT clear it, because we may use it later in an async transcription.
        if utterance.audio_chunk and not utterance.recording.bot.record_async_transcription_audio_chunks():
            utterance_audio_chunk = utterance.audio_chunk
            utterance_audio_chunk.audio_blob = b""
            utterance_audio_chunk.save()

        utterance.transcription = transcription
        utterance.save()

        logger.info(f"Transcription complete for utterance {utterance_id}")

        # Don't send webhook for empty transcript or an async transcription
        if utterance.transcription.get("transcript") and utterance.async_transcription is None:
            trigger_webhook(
                webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE,
                bot=recording.bot,
                payload=utterance_webhook_payload(utterance),
            )

    # If the utterance is for an async transcription, we don't need to do anything with the recording state.
    if utterance.async_transcription is not None:
        return

    # If the recording is in a terminal state and there are no more utterances to transcribe, set the recording's transcription state to complete
    if RecordingManager.is_terminal_state(utterance.recording.state) and Utterance.objects.filter(recording=utterance.recording, transcription__isnull=True).count() == 0:
        RecordingManager.set_recording_transcription_complete(utterance.recording)


def get_transcription_via_gladia(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    gladia_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GLADIA).first()
    if not gladia_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    gladia_credentials = gladia_credentials_record.get_credentials()
    if not gladia_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    upload_url = "https://api.gladia.io/v2/upload"

    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())
    headers = {
        "x-gladia-key": gladia_credentials["api_key"],
    }
    files = {"audio": ("file.mp3", payload_mp3, "audio/mpeg")}
    upload_response = requests.request("POST", upload_url, headers=headers, files=files)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200 and upload_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code}

    upload_response_json = upload_response.json()
    audio_url = upload_response_json["audio_url"]

    transcribe_url = "https://api.gladia.io/v2/pre-recorded"
    transcribe_request_body = {"audio_url": audio_url}
    if transcription_settings.gladia_enable_code_switching():
        transcribe_request_body["enable_code_switching"] = True
        transcribe_request_body["code_switching_config"] = {
            "languages": transcription_settings.gladia_code_switching_languages(),
        }
    transcribe_response = requests.request("POST", transcribe_url, headers=headers, json=transcribe_request_body)

    if transcribe_response.status_code != 200 and transcribe_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_request", "status_code": transcribe_response.status_code}

    transcribe_response_json = transcribe_response.json()
    result_url = transcribe_response_json["result_url"]

    # Poll the result_url until we get a completed transcription
    max_retries = 120  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        result_response = requests.get(result_url, headers=headers)

        if result_response.status_code != 200:
            logger.error(f"Gladia result fetch failed with status code {result_response.status_code}")
            time.sleep(10)
            retry_count += 1
            continue

        result_data = result_response.json()
        status = result_data.get("status")

        if status == "done":
            # Transcription is complete
            transcription = result_data.get("result", {}).get("transcription", "")
            logger.info("Gladia transcription completed successfully, now deleting audio file from Gladia")
            # Delete the audio file from Gladia
            delete_response = requests.request("DELETE", result_url, headers=headers)
            if delete_response.status_code != 200 and delete_response.status_code != 202:
                logger.error(f"Gladia delete failed with status code {delete_response.status_code}")
            else:
                logger.info("Gladia delete successful")

            transcription["transcript"] = transcription["full_transcript"]
            del transcription["full_transcript"]

            # Extract all words from all utterances into a flat list
            all_words = []
            for utterance in transcription["utterances"]:
                if "words" in utterance:
                    all_words.extend(utterance["words"])
            transcription["words"] = all_words
            del transcription["utterances"]

            return transcription, None

        elif status == "error":
            error_code = result_data.get("error_code")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error_code": error_code}

        elif status in ["queued", "processing"]:
            # Still processing, wait and retry
            logger.info(f"Gladia transcription status: {status}, waiting...")
            time.sleep(1)
            retry_count += 1

        else:
            # Unknown status
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "status": status}

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}


def get_transcription_via_deepgram(utterance):
    from deepgram import (
        DeepgramApiError,
        DeepgramClient,
        FileSource,
        PrerecordedOptions,
    )

    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    payload: FileSource = {
        "buffer": utterance.get_audio_blob().tobytes(),
    }

    deepgram_model = transcription_settings.deepgram_model()

    options = PrerecordedOptions(
        model=deepgram_model,
        smart_format=True,
        language=transcription_settings.deepgram_language(),
        detect_language=transcription_settings.deepgram_detect_language(),
        keyterm=transcription_settings.deepgram_keyterms(),
        keywords=transcription_settings.deepgram_keywords(),
        encoding="linear16",  # for 16-bit PCM
        sample_rate=utterance.get_sample_rate(),
        redact=transcription_settings.deepgram_redaction_settings(),
        replace=transcription_settings.deepgram_replace_settings(),
    )

    deepgram_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM).first()
    if not deepgram_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram_credentials = deepgram_credentials_record.get_credentials()
    if not deepgram_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram = DeepgramClient(deepgram_credentials["api_key"])

    try:
        response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
    except DeepgramApiError as e:
        original_error_json = json.loads(e.original_error)
        if original_error_json.get("err_code") == "INVALID_AUTH":
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error_code": original_error_json.get("err_code"), "error_json": original_error_json}

    logger.info(f"Deepgram transcription complete with model {deepgram_model}")
    alternatives = response.results.channels[0].alternatives
    if len(alternatives) == 0:
        logger.info(f"Deepgram transcription with model {deepgram_model} had no alternatives, returning empty transcription")
        return {"transcript": "", "words": []}, None
    return json.loads(alternatives[0].to_json()), None


def get_transcription_via_openai(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    openai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    openai_credentials = openai_credentials_record.get_credentials()
    if not openai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    # If the audio blob is less than 80ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the openai api, it will fail with a corrupted file error
    if utterance.duration_ms < 80:
        logger.info(f"OpenAI transcription skipped for utterance {utterance.id} because it's less than 80ms in duration")
        return {"transcript": ""}, None

    # Convert PCM audio to MP3
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())

    # Prepare the request for OpenAI's transcription API
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {openai_credentials['api_key']}",
    }
    files = {"file": ("file.mp3", payload_mp3, "audio/mpeg"), "model": (None, transcription_settings.openai_transcription_model())}
    if transcription_settings.openai_transcription_prompt():
        files["prompt"] = (None, transcription_settings.openai_transcription_prompt())
    if transcription_settings.openai_transcription_language():
        files["language"] = (None, transcription_settings.openai_transcription_language())
    # Add response_format and chunking_strategy for gpt-4o-transcribe-diarize
    response_format = transcription_settings.openai_transcription_response_format()
    if response_format:
        files["response_format"] = (None, response_format)

    chunking_strategy = transcription_settings.openai_transcription_chunking_strategy()
    if chunking_strategy:
        # If chunking_strategy is a dict (server_vad object), JSON stringify it
        # Otherwise pass it as-is (e.g., "auto")
        if isinstance(chunking_strategy, dict):
            files["chunking_strategy"] = (None, json.dumps(chunking_strategy))
        else:
            files["chunking_strategy"] = (None, chunking_strategy)

    response = requests.post(url, headers=headers, files=files)

    if response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if response.status_code != 200:
        logger.error(f"OpenAI transcription failed with status code {response.status_code}: {response.text}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

    result = response.json()
    logger.info(f"OpenAI transcription completed successfully for utterance {utterance.id}.")

    # Format the response to match our expected schema
    transcription = {"transcript": result.get("text", "")}

    # If diarized_json format, preserve segments and other metadata
    if "segments" in result:
        transcription["segments"] = result.get("segments", [])
    if "duration" in result:
        transcription["duration"] = result.get("duration")
    if "usage" in result:
        transcription["usage"] = result.get("usage")
    if "task" in result:
        transcription["task"] = result.get("task")

    return transcription, None


def get_transcription_via_assemblyai(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    assemblyai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()
    if not assemblyai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    assemblyai_credentials = assemblyai_credentials_record.get_credentials()
    if not assemblyai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = assemblyai_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # If the audio blob is less than 175ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the assemblyai api, the upload will fail
    if utterance.duration_ms < 175:
        logger.info(f"AssemblyAI transcription skipped for utterance {utterance.id} because it's less than 175ms in duration")
        return {"transcript": "", "words": []}, None

    headers = {"authorization": api_key}
    base_url = transcription_settings.assemblyai_base_url()

    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())

    upload_response = requests.post(f"{base_url}/upload", headers=headers, data=payload_mp3)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code, "text": upload_response.text}

    upload_url = upload_response.json()["upload_url"]

    data = {
        "audio_url": upload_url,
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

            transcript_text = transcription_result.get("text", "")
            words = transcription_result.get("words", [])

            formatted_words = []
            if words:
                for word in words:
                    formatted_word = {
                        "word": word["text"],
                        "start": word["start"] / 1000.0,
                        "end": word["end"] / 1000.0,
                        "confidence": word["confidence"],
                    }
                    if "speaker" in word:
                        formatted_word["speaker"] = word["speaker"]

                    formatted_words.append(formatted_word)

            transcription = {"transcript": transcript_text, "words": formatted_words, "language": transcription_result.get("language_code", None)}
            return transcription, None

        elif transcription_result["status"] == "error":
            error = transcription_result.get("error")

            if error and "language_detection cannot be performed on files with no spoken audio" in error:
                logger.info(f"AssemblyAI transcription skipped for utterance {utterance.id} because it did not have any spoken audio and we tried to detect language")
                return {"transcript": "", "words": []}, None

            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error": error}

        else:  # queued, processing
            logger.info(f"AssemblyAI transcription status: {transcription_result['status']}, waiting...")
            time.sleep(1)
            retry_count += 1

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}


def get_transcription_via_sarvam(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    sarvam_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.SARVAM).first()
    if not sarvam_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    sarvam_credentials = sarvam_credentials_record.get_credentials()
    if not sarvam_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = sarvam_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    headers = {"api-subscription-key": api_key}
    base_url = "https://api.sarvam.ai/speech-to-text"

    # If the audio blob is less than 50ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the sarvam api, it will fail
    if utterance.duration_ms < 50:
        logger.info(f"Sarvam transcription skipped for utterance {utterance.id} because it's less than 50ms in duration")
        return {"transcript": ""}, None

    # Sarvam says 16kHz sample rate works best
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate(), output_sample_rate=16000)

    files = {"file": ("audio.mp3", payload_mp3, "audio/mpeg")}

    # Add optional parameters if configured
    data = {}
    if transcription_settings.sarvam_language_code():
        data["language_code"] = transcription_settings.sarvam_language_code()
    if transcription_settings.sarvam_model():
        data["model"] = transcription_settings.sarvam_model()

    try:
        response = requests.post(base_url, headers=headers, files=files, data=data if data else None)

        if response.status_code == 403:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}

        if response.status_code != 200:
            logger.error(f"Sarvam transcription failed with status code {response.status_code}: {response.text}")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

        result = response.json()
        logger.info("Sarvam transcription completed successfully")

        # Extract transcript from the response
        transcript_text = result.get("transcript", "")

        # Format the response to match our expected schema
        transcription = {"transcript": transcript_text}

        return transcription, None

    except requests.exceptions.RequestException as e:
        logger.error(f"Sarvam transcription request failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"Sarvam transcription response parsing failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {str(e)}"}
    except Exception as e:
        logger.error(f"Sarvam transcription unexpected error: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


def get_transcription_via_elevenlabs(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    elevenlabs_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ELEVENLABS).first()
    if not elevenlabs_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    elevenlabs_credentials = elevenlabs_credentials_record.get_credentials()
    if not elevenlabs_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = elevenlabs_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # Convert PCM audio to MP3 for ElevenLabs
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())

    # Prepare the request for ElevenLabs speech-to-text API
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {
        "xi-api-key": api_key,
    }

    # Prepare multipart form data
    files = {"file": ("audio.mp3", payload_mp3, "audio/mpeg")}

    # Add model_id if configured
    data = {}
    if transcription_settings.elevenlabs_model_id():
        data["model_id"] = transcription_settings.elevenlabs_model_id()

    if transcription_settings.elevenlabs_language_code():
        data["language_code"] = transcription_settings.elevenlabs_language_code()

    data["tag_audio_events"] = transcription_settings.elevenlabs_tag_audio_events()

    try:
        response = requests.post(url, headers=headers, files=files, data=data if data else None)

        if response.status_code == 401:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}

        if response.status_code != 200:
            logger.error(f"ElevenLabs transcription failed with status code {response.status_code}: {response.text}")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

        result = response.json()
        logger.info("ElevenLabs transcription completed successfully")

        if result.get("language_probability", 0.0) < 0.5:
            logger.info(f"ElevenLabs transcription skipped for utterance {utterance.id} because the language probability was less than 0.5")
            return {"transcript": "", "words": []}, None

        # Extract transcript and words from the response
        transcript_text = result.get("text", "")
        words = list(map(lambda word: {"word": word.get("text"), "start": word.get("start"), "end": word.get("end")}, result.get("words", [])))

        # Format the response to match our expected schema
        transcription = {"transcript": transcript_text, "words": words, "language": result.get("language_code", None)}

        return transcription, None

    except requests.exceptions.RequestException as e:
        logger.error(f"ElevenLabs transcription request failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"ElevenLabs transcription response parsing failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {str(e)}"}
    except Exception as e:
        logger.error(f"ElevenLabs transcription unexpected error: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}
