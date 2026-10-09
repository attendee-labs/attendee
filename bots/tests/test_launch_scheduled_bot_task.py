from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone as django_timezone

from accounts.models import Organization
from bots.models import Bot, BotStates, Project
from bots.tasks.launch_scheduled_bot_task import launch_scheduled_bot


class LaunchScheduledBotTaskTestCase(TestCase):
    def setUp(self):
        """Set up test data"""
        self.organization = Organization.objects.create(
            name="Test Organization",
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create a test time
        self.original_join_at = django_timezone.now().replace(microsecond=0, second=0)
        self.modified_join_at = self.original_join_at.replace(second=self.original_join_at.second + 30)

        self.bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://example.zoom.us/j/123456789", state=BotStates.SCHEDULED, join_at=self.original_join_at)

    def test_successful_launch_scheduled_bot(self):
        """Test successful execution of launch_scheduled_bot"""
        with patch("bots.tasks.launch_scheduled_bot_task.launch_bot") as mock_launch_bot:
            # Execute the task with the original join_at time
            launch_scheduled_bot(self.bot.id, self.original_join_at.isoformat())

            # Verify the bot was transitioned to STAGED state
            self.bot.refresh_from_db()
            self.assertEqual(self.bot.state, BotStates.STAGED)

            # Verify launch_bot was called
            mock_launch_bot.assert_called_once_with(self.bot)

    def test_bot_not_in_scheduled_state(self):
        """Test that task exits early if bot is not in SCHEDULED state"""
        # Change bot state to READY
        self.bot.state = BotStates.READY
        self.bot.save()

        with patch("bots.tasks.launch_scheduled_bot_task.launch_bot") as mock_launch_bot:
            # Execute the task
            launch_scheduled_bot(self.bot.id, self.original_join_at.isoformat())

            # Verify the bot state didn't change
            self.bot.refresh_from_db()
            self.assertEqual(self.bot.state, BotStates.READY)

            # Verify launch_bot was not called
            mock_launch_bot.assert_not_called()

    def test_join_at_modified_after_task_queued(self):
        """Test the race condition where join_at is modified between task queueing and execution"""

        def mock_get_bot_and_modify_join_at(id, *args, **kwargs):
            """Mock that simulates another process modifying join_at after we get the bot"""
            bot = Bot.objects.filter(id=id).first()
            # Simulate another process modifying the join_at field
            Bot.objects.filter(id=id).update(join_at=self.modified_join_at)
            return bot

        with patch("bots.tasks.launch_scheduled_bot_task.launch_bot") as mock_launch_bot:
            with patch("bots.models.Bot.objects.get", side_effect=mock_get_bot_and_modify_join_at):
                # Execute the task with the original join_at time
                # This should raise a ValidationError due to the join_at mismatch
                with self.assertRaises(ValidationError) as context:
                    launch_scheduled_bot(self.bot.id, self.original_join_at.isoformat())

                # Verify the error message contains the expected text
                error_message = str(context.exception)
                self.assertIn("join_at in event_metadata", error_message)
                self.assertIn("is different from the join_at in the database", error_message)

                # Verify launch_bot was not called due to the error
                mock_launch_bot.assert_not_called()
