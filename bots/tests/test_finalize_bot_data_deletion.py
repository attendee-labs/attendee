from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.models import Bot, BotDebugScreenshot, BotEvent, BotEventTypes, BotStates, Project


class FinalizeBotDataDeletionCommandTestCase(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Data Deletion Test Org")
        project = Project.objects.create(organization=organization, name="Data Deletion Test Project")
        self.bot = Bot.objects.create(project=project, name="Data Deletion Bot", meeting_url="https://test.com/meeting", state=BotStates.ENDED)
        self.now = timezone.now()

    def test_deletes_screenshot_row_and_file_for_data_deleted_bot(self):
        storage = InMemoryStorage()

        with patch.object(BotDebugScreenshot._meta.get_field("file"), "storage", storage):
            self.bot.state = BotStates.DATA_DELETED
            self.bot.last_heartbeat_timestamp = int(self.now.timestamp())
            self.bot.save(update_fields=["state", "last_heartbeat_timestamp"])
            event = BotEvent.objects.create(
                bot=self.bot,
                old_state=BotStates.ENDED,
                new_state=BotStates.DATA_DELETED,
                event_type=BotEventTypes.DATA_DELETED,
            )
            screenshot = BotDebugScreenshot.objects.create(bot_event=event)
            screenshot.file.save("debug.png", ContentFile(b"debug screenshot"))
            file_name = screenshot.file.name

            self.assertTrue(storage.exists(file_name))

            call_command("finalize_bot_data_deletion", "--lookback-days=7", "--batch-size=10")

            self.assertFalse(BotDebugScreenshot.objects.filter(pk=screenshot.pk).exists())
            self.assertFalse(storage.exists(file_name))

    def test_keeps_screenshot_for_bot_without_deleted_data(self):
        storage = InMemoryStorage()

        with patch.object(BotDebugScreenshot._meta.get_field("file"), "storage", storage):
            self.bot.last_heartbeat_timestamp = int(self.now.timestamp())
            self.bot.save(update_fields=["last_heartbeat_timestamp"])
            event = BotEvent.objects.create(
                bot=self.bot,
                old_state=BotStates.ENDED,
                new_state=BotStates.ENDED,
                event_type=BotEventTypes.BOT_JOINED_MEETING,
            )
            screenshot = BotDebugScreenshot.objects.create(bot_event=event)
            screenshot.file.save("debug.png", ContentFile(b"debug screenshot"))
            file_name = screenshot.file.name

            call_command("finalize_bot_data_deletion", "--lookback-days=7", "--batch-size=10")

            self.assertTrue(BotDebugScreenshot.objects.filter(pk=screenshot.pk).exists())
            self.assertTrue(storage.exists(file_name))
