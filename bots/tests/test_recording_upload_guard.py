import os
import tempfile

from django.test import TestCase

from bots.bot_controller.bot_controller import BotController
from bots.bot_controller.screen_and_audio_recorder import ScreenAndAudioRecorder


class ScreenAndAudioRecorderCleanupTestCase(TestCase):
    """Issue #587: cleanup() must not create an empty placeholder file."""

    def test_cleanup_does_not_create_empty_file_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "bot-rec.mp4")
            recorder = ScreenAndAudioRecorder(file_location=missing_path, recording_dimensions=(1280, 720), audio_only=False)

            recorder.cleanup()

            self.assertFalse(os.path.exists(missing_path), "cleanup() must not create an empty placeholder file")

    def test_cleanup_leaves_existing_audio_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot-rec.mp3")
            with open(path, "wb") as f:
                f.write(b"real audio bytes")

            recorder = ScreenAndAudioRecorder(file_location=path, recording_dimensions=(1280, 720), audio_only=True)
            recorder.cleanup()

            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"real audio bytes")


class LocalRecordingFileHasContentTestCase(TestCase):
    """Issue #587 upload guard: only a non-empty existing file is uploadable."""

    def _controller(self):
        # Skip the heavy __init__; the method only touches os.
        return BotController.__new__(BotController)

    def test_missing_file_is_not_uploadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self._controller().local_recording_file_has_content(os.path.join(tmp, "nope.mp4")))

    def test_empty_file_is_not_uploadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.mp4")
            with open(path, "wb"):
                pass
            self.assertFalse(self._controller().local_recording_file_has_content(path))

    def test_non_empty_file_is_uploadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "real.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x01\x02")
            self.assertTrue(self._controller().local_recording_file_has_content(path))
