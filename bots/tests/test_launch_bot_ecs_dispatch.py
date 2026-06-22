import os
from unittest.mock import patch

from django.test import TestCase

from accounts.models import Organization
from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotEventSubTypes, BotEventTypes, BotStates, Project


class LaunchBotEcsDispatchTestCase(TestCase):
    """Covers the LAUNCH_BOT_METHOD=ecs branch of launch_bot()."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://example.zoom.us/j/123456789",
            state=BotStates.JOINING,
        )

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "ecs"})
    @patch("bots.bot_ecs_task_creator.BotEcsTaskCreator")
    def test_ecs_dispatch_launches_task(self, MockCreator):
        creator = MockCreator.return_value
        creator.create_bot_task.return_value = {"created": True, "task_arn": "arn:task/abc"}

        launch_bot(self.bot)

        creator.create_bot_task.assert_called_once_with(
            bot_id=self.bot.id,
            bot_pod_spec_type=self.bot.bot_pod_spec_type,
            add_webpage_streamer=self.bot.should_launch_webpage_streamer(),
            add_persistent_storage=self.bot.reserve_additional_storage(),
        )

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "ecs"})
    @patch("bots.launch_bot_utils.BotEventManager")
    @patch("bots.bot_ecs_task_creator.BotEcsTaskCreator")
    def test_ecs_dispatch_failure_emits_fatal_error_event(self, MockCreator, MockEventManager):
        creator = MockCreator.return_value
        creator.create_bot_task.return_value = {"created": False, "error": "boom"}

        launch_bot(self.bot)

        MockEventManager.create_event.assert_called_once()
        kwargs = MockEventManager.create_event.call_args.kwargs
        self.assertEqual(kwargs["event_type"], BotEventTypes.FATAL_ERROR)
        self.assertEqual(kwargs["event_sub_type"], BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED)
