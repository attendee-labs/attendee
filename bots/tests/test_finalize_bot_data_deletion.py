from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.models import Bot, BotStates, Project


class FinalizeBotDataDeletionCommandTestCase(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Data Deletion Test Org")
        project = Project.objects.create(organization=organization, name="Data Deletion Test Project")
        self.bot = Bot.objects.create(project=project, name="Data Deletion Bot", meeting_url="https://test.com/meeting", state=BotStates.ENDED)
        self.now = timezone.now()

    def test_ensures_data_deleted_for_recent_data_deleted_bot(self):
        self.bot.state = BotStates.DATA_DELETED
        self.bot.save(update_fields=["state"])

        with patch.object(Bot, "ensure_data_deleted", autospec=True) as ensure_data_deleted:
            call_command("finalize_bot_data_deletion", "--lookback-hours=24", "--batch-size=10")

        ensure_data_deleted.assert_called_once()
        self.assertEqual(ensure_data_deleted.call_args.args[0].pk, self.bot.pk)

    def test_ignores_bot_not_in_data_deleted_state(self):
        with patch.object(Bot, "ensure_data_deleted", autospec=True) as ensure_data_deleted:
            call_command("finalize_bot_data_deletion", "--lookback-hours=24", "--batch-size=10")

        ensure_data_deleted.assert_not_called()

    def test_ignores_data_deleted_bot_outside_lookback_window(self):
        self.bot.state = BotStates.DATA_DELETED
        self.bot.save(update_fields=["state"])
        Bot.objects.filter(pk=self.bot.pk).update(updated_at=self.now - timedelta(hours=25))

        with patch.object(Bot, "ensure_data_deleted", autospec=True) as ensure_data_deleted:
            call_command("finalize_bot_data_deletion", "--lookback-hours=24", "--batch-size=10")

        ensure_data_deleted.assert_not_called()
