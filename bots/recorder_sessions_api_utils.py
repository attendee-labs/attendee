"""
Lifecycle logic for desktop recorder sessions.

A recorder session is a Bot row (session_type == DESKTOP_RECORDING) + its default Recording
+ a RecorderUpload tracking the S3 multipart upload. The session lifecycle is deliberately
decoupled from the BotEventManager state machine: meaningful state lives on RecorderUpload,
and the Recording is driven directly through RecordingManager (which has self-contained
transitions). See docs/desktop_recorder_sdk.md.
"""

import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import recorder_upload_storage as storage
from .models import (
    Bot,
    RecorderUpload,
    RecorderUploadStates,
    Recording,
    RecordingFormats,
    RecordingManager,
    SessionTypes,
    TranscriptionTypes,
)

logger = logging.getLogger(__name__)

# Client-supplied content type -> stored recording format. Unknown types default to MP4.
CONTENT_TYPE_TO_FORMAT = {
    "video/mp4": RecordingFormats.MP4,
    "video/webm": RecordingFormats.WEBM,
    "audio/webm": RecordingFormats.WEBM,
    "audio/mpeg": RecordingFormats.MP3,
    "audio/mp3": RecordingFormats.MP3,
}


def recording_format_for_content_type(content_type):
    return CONTENT_TYPE_TO_FORMAT.get((content_type or "").lower(), RecordingFormats.MP4)


def _recorder_session_s3_key(bot, recording, recording_format):
    return f"desktop-recordings/{bot.object_id}-{recording.object_id}.{recording_format}"


def touch(recorder_upload):
    recorder_upload.last_activity_at = timezone.now()
    recorder_upload.save(update_fields=["last_activity_at", "updated_at"])


def create_recorder_session(data, project):
    """
    Create (or idempotently return) a recorder session and initiate its multipart upload.
    Returns (recorder_upload, error_dict).
    """
    if not storage.recorder_uploads_supported():
        return None, {"error": "Desktop recorder uploads require S3 storage (STORAGE_PROTOCOL=s3)."}

    # Small grace period before rejecting, same policy as app sessions.
    if project.organization.out_of_credits():
        return None, {"error": "Organization has run out of credits. Please add more credits in the Account -> Billing page."}

    content_type = data.get("content_type") or "video/mp4"
    recording_format = recording_format_for_content_type(content_type)
    metadata = data.get("metadata")
    deduplication_key = data.get("deduplication_key")
    bytes_expected = data.get("bytes_expected")

    # Idempotency: an active session with the same dedup key is reused, not duplicated.
    if deduplication_key:
        existing = RecorderUpload.objects.filter(project=project, deduplication_key=deduplication_key).exclude(state__in=RecorderUploadStates.terminal_states()).select_related("bot").first()
        if existing:
            return existing, None

    try:
        with transaction.atomic():
            bot = Bot.objects.create(
                project=project,
                name="Desktop Recording",
                meeting_url="desktop_recording",
                session_type=SessionTypes.DESKTOP_RECORDING,
                metadata=metadata,
                settings={"recording_settings": {"format": recording_format}},
            )

            recording = Recording.objects.create(
                bot=bot,
                recording_type=bot.recording_type(),
                transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
                is_default_recording=True,
            )
            # Move the recording into progress so it can be completed on upload finalize.
            RecordingManager.set_recording_in_progress(recording)

            s3_key = _recorder_session_s3_key(bot, recording, recording_format)
            upload_id = storage.initiate_multipart_upload(s3_key, content_type)

            recorder_upload = RecorderUpload.objects.create(
                bot=bot,
                project=project,
                s3_key=s3_key,
                upload_id=upload_id,
                content_type=content_type,
                bytes_expected=bytes_expected,
                deduplication_key=deduplication_key,
            )
            return recorder_upload, None
    except IntegrityError as e:
        # Lost a create race on the dedup key: return the winner.
        if "unique_recorder_upload_deduplication_key" in str(e) and deduplication_key:
            existing = RecorderUpload.objects.filter(project=project, deduplication_key=deduplication_key).exclude(state__in=RecorderUploadStates.terminal_states()).select_related("bot").first()
            if existing:
                return existing, None
        error_id = str(uuid.uuid4())
        logger.error(f"IntegrityError creating recorder session (error_id={error_id}): {e}")
        return None, {"error": f"An error occurred while creating the recorder session. Error ID: {error_id}"}
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating recorder session (error_id={error_id}): {e}")
        return None, {"error": f"An error occurred while creating the recorder session. Error ID: {error_id}"}


def part_upload_urls(recorder_upload, part_numbers):
    return storage.generate_part_upload_urls(recorder_upload.s3_key, recorder_upload.upload_id, part_numbers)


def received_parts(recorder_upload):
    """Parts S3 has actually received, for the SDK to resume after an interruption."""
    return storage.list_uploaded_parts(recorder_upload.s3_key, recorder_upload.upload_id)


def _mark_failed(recorder_upload, reason):
    recorder_upload.state = RecorderUploadStates.FAILED
    recorder_upload.failure_data = {"reason": reason}
    recorder_upload.last_activity_at = timezone.now()
    recorder_upload.save()
    recording = Recording.objects.filter(bot=recorder_upload.bot, is_default_recording=True).first()
    if recording:
        RecordingManager.set_recording_failed(recording)


def complete_recorder_session(recorder_upload, parts):
    """
    Finalize the multipart upload and attach the media to the recording.
    `parts` is a client-supplied list of {part_number, etag}; if empty we fall back to
    whatever S3 has received. Idempotent. Returns (recorder_upload, error_dict).
    """
    if recorder_upload.state == RecorderUploadStates.COMPLETE:
        return recorder_upload, None
    if recorder_upload.is_terminal():
        return None, {"error": f"Recorder session is already in terminal state '{RecorderUploadStates.state_to_api_code(recorder_upload.state)}'."}

    # Prefer client-reported parts; if absent, resume from what S3 actually holds.
    if not parts:
        parts = received_parts(recorder_upload)
    if not parts:
        _mark_failed(recorder_upload, "no_data_uploaded")
        return None, {"error": "No uploaded parts found for this recorder session."}

    try:
        storage.complete_multipart_upload(recorder_upload.s3_key, recorder_upload.upload_id, parts)
    except Exception as e:
        logger.error(f"Failed to complete multipart upload for {recorder_upload.object_id}: {e}")
        _mark_failed(recorder_upload, "multipart_complete_failed")
        return None, {"error": "Failed to finalize the upload. Verify all parts were uploaded and retry."}

    size = storage.object_size(recorder_upload.s3_key)
    if not size:
        _mark_failed(recorder_upload, "empty_or_missing_object")
        return None, {"error": "Uploaded media is empty or missing."}
    if size > storage.max_upload_bytes():
        _mark_failed(recorder_upload, "exceeds_max_upload_bytes")
        return None, {"error": "Uploaded media exceeds the maximum allowed size."}

    recording = Recording.objects.filter(bot=recorder_upload.bot, is_default_recording=True).first()
    recording.file = recorder_upload.s3_key
    recording.save()
    RecordingManager.set_recording_complete(recording)

    recorder_upload.state = RecorderUploadStates.COMPLETE
    recorder_upload.bytes_received = size
    recorder_upload.parts = parts
    recorder_upload.last_activity_at = timezone.now()
    recorder_upload.save()

    return recorder_upload, None


def abort_recorder_session(recorder_upload):
    """SDK-initiated cancel: abort the multipart upload and mark the session expired."""
    if recorder_upload.state == RecorderUploadStates.COMPLETE:
        return None, {"error": "Cannot abort a completed recorder session."}
    if recorder_upload.is_terminal():
        return recorder_upload, None

    storage.abort_multipart_upload(recorder_upload.s3_key, recorder_upload.upload_id)

    recorder_upload.state = RecorderUploadStates.EXPIRED
    recorder_upload.last_activity_at = timezone.now()
    recorder_upload.save()

    recording = Recording.objects.filter(bot=recorder_upload.bot, is_default_recording=True).first()
    if recording and not RecordingManager.is_terminal_state(recording.state):
        try:
            RecordingManager.set_recording_failed(recording)
        except ValueError:
            # Recording wasn't in a state that can transition to failed; leave as-is.
            pass
    return recorder_upload, None
