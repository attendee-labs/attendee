import os
import subprocess
from unittest import mock

from django.test import SimpleTestCase

from bots.bot_controller.per_bot_audio_sink import (
    PULSE_SINK_ENV_VAR,
    PULSE_SOURCE_ENV_VAR,
    PerBotAudioSink,
    audio_input_ffmpeg_args,
    audio_sink_to_mute,
)


def completed(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TestPerBotAudioSink(SimpleTestCase):
    def setUp(self):
        environment_patcher = mock.patch.dict(os.environ, {}, clear=False)
        environment_patcher.start()
        self.addCleanup(environment_patcher.stop)
        os.environ.pop(PULSE_SINK_ENV_VAR, None)
        os.environ.pop(PULSE_SOURCE_ENV_VAR, None)
        os.environ.pop("PER_BOT_AUDIO_SINK", None)

    @mock.patch("bots.bot_controller.per_bot_audio_sink.subprocess.run")
    def test_bot_sharing_the_audio_server_records_from_its_own_sink(self, run):
        run.side_effect = [completed(), completed("42\n"), completed()]

        with PerBotAudioSink(7, shares_audio_server_with_other_bots=True) as audio_sink:
            self.assertTrue(audio_sink.sink_name.startswith("attendee_bot_7_"))
            self.assertEqual(os.environ[PULSE_SINK_ENV_VAR], audio_sink.sink_name)
            self.assertEqual(audio_input_ffmpeg_args(), ["-f", "pulse", "-i", f"{audio_sink.sink_name}.monitor"])
            self.assertEqual(audio_sink_to_mute(), audio_sink.sink_name)

        load_command = run.call_args_list[1].args[0]
        self.assertEqual(load_command[:3], ["pactl", "load-module", "module-null-sink"])
        self.assertIn(f"sink_name={audio_sink.sink_name}", load_command)
        self.assertEqual(run.call_args_list[2].args[0], ["pactl", "unload-module", "42"])
        self.assertNotIn(PULSE_SINK_ENV_VAR, os.environ)
        self.assertNotIn(PULSE_SOURCE_ENV_VAR, os.environ)

    @mock.patch("bots.bot_controller.per_bot_audio_sink.subprocess.run")
    def test_switch_on_connect_is_unloaded_before_the_sink_is_created(self, run):
        run.side_effect = [completed(), completed("42\n"), completed()]

        with PerBotAudioSink(7, shares_audio_server_with_other_bots=True):
            pass

        self.assertEqual(run.call_args_list[0].args[0], ["pactl", "unload-module", "module-switch-on-connect"])

    def test_relaunched_bot_gets_a_sink_name_that_does_not_collide_with_the_previous_attempt(self):
        self.assertNotEqual(PerBotAudioSink(7, shares_audio_server_with_other_bots=True).sink_name, PerBotAudioSink(7, shares_audio_server_with_other_bots=True).sink_name)

    @mock.patch("bots.bot_controller.per_bot_audio_sink.subprocess.run")
    def test_bot_with_its_own_audio_server_keeps_the_default_device(self, run):
        with PerBotAudioSink(7, shares_audio_server_with_other_bots=False):
            self.assertEqual(audio_input_ffmpeg_args(), ["-f", "alsa", "-i", "default"])
            self.assertEqual(audio_sink_to_mute(), "@DEFAULT_SINK@")

        run.assert_not_called()

    @mock.patch("bots.bot_controller.per_bot_audio_sink.subprocess.run")
    def test_setting_can_turn_the_sink_off(self, run):
        os.environ["PER_BOT_AUDIO_SINK"] = "false"

        with PerBotAudioSink(7, shares_audio_server_with_other_bots=True):
            self.assertEqual(audio_input_ffmpeg_args(), ["-f", "alsa", "-i", "default"])

        run.assert_not_called()

    @mock.patch("bots.bot_controller.per_bot_audio_sink.subprocess.run")
    def test_failure_to_create_the_sink_falls_back_to_the_default_device(self, run):
        run.side_effect = [completed(), subprocess.CalledProcessError(1, "pactl"), completed()]

        with PerBotAudioSink(7, shares_audio_server_with_other_bots=True):
            self.assertEqual(audio_input_ffmpeg_args(), ["-f", "alsa", "-i", "default"])
            self.assertEqual(audio_sink_to_mute(), "@DEFAULT_SINK@")

        self.assertEqual(run.call_count, 2)


class TestRunBotTaskAudioSink(SimpleTestCase):
    @mock.patch("bots.tasks.run_bot_task.BotController")
    @mock.patch("bots.tasks.run_bot_task.PerBotAudioSink")
    def test_task_called_directly_does_not_share_the_audio_server(self, per_bot_audio_sink, bot_controller):
        from bots.tasks.run_bot_task import run_bot

        run_bot.run(7)

        per_bot_audio_sink.assert_called_once_with(7, shares_audio_server_with_other_bots=False)
        bot_controller.return_value.run.assert_called_once()
