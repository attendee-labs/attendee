import os
from unittest.mock import MagicMock, patch

from django.test import TestCase
from kubernetes import client as k8s_client

from bots.bot_adapter import BotAdapter
from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotEventManager, BotEventTypes, BotStates, Organization, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes
from bots.tasks.restart_bot_pod_task import restart_bot_pod


class RestartBlockedBotTestCase(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Org")
        project = Project.objects.create(name="Test Project", organization=organization)
        self.bot = Bot.objects.create(
            name="Test Meet Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.READY,
            project=project,
        )
        Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.DEEPGRAM,
            is_default_recording=True,
        )
        BotEventManager.create_event(self.bot, BotEventTypes.JOIN_REQUESTED)
        self.bot.refresh_from_db()
        self.bot.first_heartbeat_timestamp = 1700000000
        self.bot.last_heartbeat_timestamp = 1700000060
        self.bot.save()


class TestRestartBotPodTaskWithoutKubernetes(RestartBlockedBotTestCase):
    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "celery"})
    @patch("bots.tasks.restart_bot_pod_task.BotPodCreator")
    @patch("bots.tasks.restart_bot_pod_task.launch_bot")
    def test_bot_outside_kubernetes_is_relaunched_with_launch_bot(self, mock_launch_bot, MockBotPodCreator):
        restart_bot_pod(self.bot.id)

        mock_launch_bot.assert_called_once()
        self.assertEqual(mock_launch_bot.call_args.args[0].id, self.bot.id)
        MockBotPodCreator.assert_not_called()

        self.bot.refresh_from_db()
        self.assertIsNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNone(self.bot.last_heartbeat_timestamp)

        join_requested_event = self.bot.bot_events.get(event_type=BotEventTypes.JOIN_REQUESTED)
        self.assertIsNone(join_requested_event.requested_bot_action_taken_at)
        self.assertEqual(len(join_requested_event.metadata["pod_recreations"]), 1)

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"})
    @patch("bots.tasks.restart_bot_pod_task.BotPodCreator")
    @patch("bots.tasks.restart_bot_pod_task.find_bot_pod_name", return_value="bot-pod")
    @patch("bots.tasks.restart_bot_pod_task.client.CoreV1Api")
    @patch("bots.tasks.restart_bot_pod_task.config.load_incluster_config")
    @patch("bots.tasks.restart_bot_pod_task.launch_bot")
    def test_bot_in_kubernetes_still_gets_a_new_pod(self, mock_launch_bot, mock_load_incluster_config, MockCoreV1Api, mock_find_bot_pod_name, MockBotPodCreator):
        MockCoreV1Api.return_value.read_namespaced_pod.side_effect = k8s_client.ApiException(status=404)
        MockBotPodCreator.return_value.create_bot_pod.return_value = {"created": True}

        restart_bot_pod(self.bot.id)

        mock_launch_bot.assert_not_called()
        MockBotPodCreator.return_value.create_bot_pod.assert_called_once()


class TestBlockedByPlatformRepeatedlyReleasesResources(RestartBlockedBotTestCase):
    def build_controller_waiting_to_join(self):
        self.bot.refresh_from_db()
        controller = BotController(self.bot.id)
        controller.gstreamer_pipeline = MagicMock()
        controller.rtmp_client = None
        controller.room_sync_client = None
        controller.adapter = MagicMock()
        controller.realtime_audio_output_manager = MagicMock()
        controller.webpage_streamer_manager = None
        controller.websocket_client_manager = None
        controller.main_loop = None
        return controller

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "celery"})
    @patch("bots.tasks.restart_bot_pod_task.restart_bot_pod.apply_async")
    def test_bot_outside_kubernetes_releases_browser_and_display_before_relaunch(self, mock_restart_bot_pod_apply_async):
        controller = self.build_controller_waiting_to_join()

        controller.take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.BLOCKED_BY_PLATFORM_REPEATEDLY})

        mock_restart_bot_pod_apply_async.assert_called_once()
        controller.gstreamer_pipeline.cleanup.assert_called_once()
        controller.adapter.cleanup.assert_called_once()
        controller.adapter.display.stop.assert_called_once()
        controller.realtime_audio_output_manager.cleanup.assert_called_once()

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"})
    @patch("bots.tasks.restart_bot_pod_task.restart_bot_pod.apply_async")
    def test_bot_in_kubernetes_leaves_resources_to_the_pod_restart(self, mock_restart_bot_pod_apply_async):
        controller = self.build_controller_waiting_to_join()

        controller.take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.BLOCKED_BY_PLATFORM_REPEATEDLY})

        mock_restart_bot_pod_apply_async.assert_called_once()
        controller.adapter.cleanup.assert_not_called()
        controller.gstreamer_pipeline.cleanup.assert_not_called()
