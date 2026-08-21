from unittest.mock import patch

from django.test import TestCase

from accounts.models import Organization
from bots.models import Bot, BotStates, Project
from bots.tasks.run_bot_task import run_bot


class RunBotTaskGuardTestCase(TestCase):
    """Issue #587: a redelivered run_bot task must not run a bot that already finished."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(organization=self.organization, name="Test Project")
        self.bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://meet.google.com/abc-defg-hij", state=BotStates.READY)

    def _run_task(self):
        with patch("bots.tasks.run_bot_task.BotController") as mock_controller:
            run_bot.apply(args=[self.bot.id])
        return mock_controller

    def test_skips_bot_in_post_meeting_state(self):
        for state in BotStates.post_meeting_states():
            Bot.objects.filter(pk=self.bot.pk).update(state=state)
            mock_controller = self._run_task()
            mock_controller.assert_not_called()

    def test_runs_bot_not_in_post_meeting_state(self):
        Bot.objects.filter(pk=self.bot.pk).update(state=BotStates.READY)
        mock_controller = self._run_task()
        mock_controller.assert_called_once_with(self.bot.id)
        mock_controller.return_value.run.assert_called_once()
