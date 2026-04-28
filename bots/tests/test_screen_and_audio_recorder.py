import unittest
from unittest.mock import Mock, call, patch

from bots.bot_controller.screen_and_audio_recorder import ScreenAndAudioRecorder


class TestScreenAndAudioRecorder(unittest.TestCase):
    def create_recorder(self, audio_only=False):
        return ScreenAndAudioRecorder(
            file_location="/tmp/test-recording.mp4",
            recording_dimensions=(1280, 720),
            audio_only=audio_only,
            audio_sink_name="attendee_test_sink",
        )

    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.run")
    def test_browser_audio_environment_creates_isolated_pulseaudio_sink(self, mock_run):
        mock_run.return_value = Mock(stdout="42\n")
        recorder = self.create_recorder()

        self.assertEqual(recorder.browser_audio_environment(), {"PULSE_SINK": "attendee_test_sink"})
        mock_run.assert_called_once_with(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                "sink_name=attendee_test_sink",
                "rate=48000",
                "channels=2",
                "sink_properties=device.description=attendee_test_sink",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.Popen")
    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.run")
    def test_start_recording_uses_isolated_sink_monitor_when_available(self, mock_run, mock_popen):
        mock_run.return_value = Mock(stdout="42\n")
        recorder = self.create_recorder()
        recorder.browser_audio_environment()

        recorder.start_recording(":99")

        ffmpeg_cmd = mock_popen.call_args.args[0]
        self.assertIn("pulse", ffmpeg_cmd)
        self.assertIn("attendee_test_sink.monitor", ffmpeg_cmd)

    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.Popen")
    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.run")
    def test_start_recording_falls_back_to_alsa_default_when_sink_setup_fails(self, mock_run, mock_popen):
        mock_run.side_effect = FileNotFoundError("pactl not found")
        recorder = self.create_recorder()

        recorder.start_recording(":99")

        ffmpeg_cmd = mock_popen.call_args.args[0]
        self.assertIn("alsa", ffmpeg_cmd)
        self.assertIn("default", ffmpeg_cmd)

    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.Popen")
    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.run")
    def test_pause_recording_mutes_only_isolated_sink(self, mock_run, mock_popen):
        mock_run.return_value = Mock(stdout="42\n")
        recorder = self.create_recorder()
        recorder.browser_audio_environment()

        self.assertTrue(recorder.pause_recording())

        self.assertIn(call(["pactl", "set-sink-mute", "attendee_test_sink", "1"], check=True), mock_run.call_args_list)

    @patch("bots.bot_controller.screen_and_audio_recorder.os.path.exists", return_value=True)
    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.run")
    def test_cleanup_unloads_isolated_sink_even_for_audio_only_recordings(self, mock_run, _mock_exists):
        mock_run.return_value = Mock(stdout="42\n")
        recorder = self.create_recorder(audio_only=True)
        recorder.browser_audio_environment()

        recorder.cleanup()

        self.assertIn(call(["pactl", "unload-module", "42"], check=True, capture_output=True, text=True), mock_run.call_args_list)
