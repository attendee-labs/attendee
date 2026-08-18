from unittest.mock import patch

from django.test import TestCase

from accounts.models import Organization
from bots.models import (
    Bot,
    Project,
    RecorderUploadStates,
    Recording,
    RecordingStates,
    RecordingTypes,
    SessionTypes,
    TranscriptionTypes,
)
from bots.recorder_sessions_api_utils import (
    abort_recorder_session,
    complete_recorder_session,
    create_recorder_session,
)

STORAGE = "bots.recorder_upload_storage"


def storage_patches(**overrides):
    defaults = {
        f"{STORAGE}.recorder_uploads_supported": True,
        f"{STORAGE}.initiate_multipart_upload": "upload-123",
        f"{STORAGE}.complete_multipart_upload": None,
        f"{STORAGE}.abort_multipart_upload": None,
        f"{STORAGE}.object_size": 1024,
        f"{STORAGE}.list_uploaded_parts": [],
        f"{STORAGE}.generate_part_upload_urls": [],
    }
    defaults.update(overrides)
    return defaults


class RecorderSessionUtilsTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def _patch(self, **overrides):
        patches = storage_patches(**overrides)
        self._active = [patch(target, **({"return_value": value} if not callable(value) else {"side_effect": value})) for target, value in patches.items()]
        self._mocks = [p.start() for p in self._active]
        self.addCleanup(lambda: [p.stop() for p in self._active])

    def test_create_recorder_session(self):
        self._patch()
        recorder_upload, error = create_recorder_session({"content_type": "video/mp4"}, project=self.project)

        self.assertIsNone(error)
        self.assertIsNotNone(recorder_upload)
        self.assertEqual(recorder_upload.state, RecorderUploadStates.CREATED)
        self.assertEqual(recorder_upload.upload_id, "upload-123")
        self.assertTrue(recorder_upload.s3_key.endswith(".mp4"))

        bot = recorder_upload.bot
        self.assertEqual(bot.session_type, SessionTypes.DESKTOP_RECORDING)
        self.assertTrue(bot.object_id.startswith("drec_"))

        recording = Recording.objects.get(bot=bot, is_default_recording=True)
        self.assertEqual(recording.state, RecordingStates.IN_PROGRESS)
        self.assertEqual(recording.recording_type, RecordingTypes.AUDIO_AND_VIDEO)
        self.assertEqual(recording.transcription_type, TranscriptionTypes.NO_TRANSCRIPTION)

    def test_create_recorder_session_audio_only(self):
        self._patch()
        recorder_upload, error = create_recorder_session({"content_type": "audio/mpeg"}, project=self.project)
        self.assertIsNone(error)
        recording = Recording.objects.get(bot=recorder_upload.bot, is_default_recording=True)
        self.assertEqual(recording.recording_type, RecordingTypes.AUDIO_ONLY)

    def test_create_is_idempotent_with_deduplication_key(self):
        self._patch()
        first, _ = create_recorder_session({"deduplication_key": "abc"}, project=self.project)
        second, error = create_recorder_session({"deduplication_key": "abc"}, project=self.project)
        self.assertIsNone(error)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Bot.objects.filter(session_type=SessionTypes.DESKTOP_RECORDING).count(), 1)

    def test_create_rejected_when_out_of_credits(self):
        self._patch()
        self.organization.centicredits = -500
        self.organization.save()
        recorder_upload, error = create_recorder_session({}, project=self.project)
        self.assertIsNone(recorder_upload)
        self.assertIn("credits", error["error"].lower())

    def test_create_rejected_when_storage_unsupported(self):
        self._patch(**{f"{STORAGE}.recorder_uploads_supported": False})
        recorder_upload, error = create_recorder_session({}, project=self.project)
        self.assertIsNone(recorder_upload)
        self.assertIn("S3", error["error"])

    def test_complete_recorder_session(self):
        self._patch(**{f"{STORAGE}.object_size": 2048})
        recorder_upload, _ = create_recorder_session({}, project=self.project)

        completed, error = complete_recorder_session(recorder_upload, [{"part_number": 1, "etag": "etag-1"}])
        self.assertIsNone(error)
        self.assertEqual(completed.state, RecorderUploadStates.COMPLETE)
        self.assertEqual(completed.bytes_received, 2048)

        recording = Recording.objects.get(bot=recorder_upload.bot, is_default_recording=True)
        self.assertEqual(recording.state, RecordingStates.COMPLETE)
        self.assertEqual(recording.file.name, recorder_upload.s3_key)

    def test_complete_is_idempotent(self):
        self._patch()
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        complete_recorder_session(recorder_upload, [{"part_number": 1, "etag": "e"}])
        again, error = complete_recorder_session(recorder_upload, [{"part_number": 1, "etag": "e"}])
        self.assertIsNone(error)
        self.assertEqual(again.state, RecorderUploadStates.COMPLETE)

    def test_complete_falls_back_to_received_parts(self):
        self._patch(**{f"{STORAGE}.list_uploaded_parts": [{"part_number": 1, "etag": "s3-etag", "size": 10}]})
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        completed, error = complete_recorder_session(recorder_upload, [])
        self.assertIsNone(error)
        self.assertEqual(completed.state, RecorderUploadStates.COMPLETE)

    def test_complete_fails_with_no_parts(self):
        self._patch(**{f"{STORAGE}.list_uploaded_parts": []})
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        completed, error = complete_recorder_session(recorder_upload, [])
        self.assertIsNone(completed)
        self.assertIsNotNone(error)
        recorder_upload.refresh_from_db()
        self.assertEqual(recorder_upload.state, RecorderUploadStates.FAILED)

    def test_complete_fails_with_empty_object(self):
        self._patch(**{f"{STORAGE}.object_size": 0})
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        completed, error = complete_recorder_session(recorder_upload, [{"part_number": 1, "etag": "e"}])
        self.assertIsNone(completed)
        recorder_upload.refresh_from_db()
        self.assertEqual(recorder_upload.state, RecorderUploadStates.FAILED)
        self.assertEqual(recorder_upload.failure_data["reason"], "empty_or_missing_object")

    def test_abort_recorder_session(self):
        self._patch()
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        aborted, error = abort_recorder_session(recorder_upload)
        self.assertIsNone(error)
        self.assertEqual(aborted.state, RecorderUploadStates.EXPIRED)
        recording = Recording.objects.get(bot=recorder_upload.bot, is_default_recording=True)
        self.assertEqual(recording.state, RecordingStates.FAILED)

    def test_abort_completed_session_rejected(self):
        self._patch()
        recorder_upload, _ = create_recorder_session({}, project=self.project)
        complete_recorder_session(recorder_upload, [{"part_number": 1, "etag": "e"}])
        aborted, error = abort_recorder_session(recorder_upload)
        self.assertIsNone(aborted)
        self.assertIsNotNone(error)
