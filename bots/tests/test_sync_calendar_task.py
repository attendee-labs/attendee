from datetime import datetime, timedelta
from datetime import timezone as python_timezone
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import Organization
from bots.management.commands.run_scheduler import CALENDAR_SYNC_THRESHOLD_HOURS
from bots.models import (
    Bot,
    BotStates,
    Calendar,
    CalendarEvent,
    CalendarNotificationChannel,
    CalendarPlatform,
    CalendarStates,
    Project,
    WebhookTriggerTypes,
)
from bots.tasks.sync_calendar_task import (
    CalendarAPIAuthenticationError,
    CalendarSyncHandler,
    GoogleCalendarSyncHandler,
    MicrosoftCalendarSyncHandler,
    enqueue_sync_calendar_task,
    extract_meeting_url_from_text,
    sync_bot_with_calendar_event,
    sync_bots_for_calendar_event,
    sync_calendar,
)
from bots.webhook_payloads import calendar_webhook_payload


class TestExtractMeetingUrlFromText(TestCase):
    """Test the extract_meeting_url_from_text function."""

    def test_extract_zoom_url(self):
        """Test extracting Zoom meeting URL from text."""
        text = "Join the meeting at https://zoom.us/j/123456789"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://zoom.us/j/123456789")

    def test_extract_google_meet_url(self):
        """Test extracting Google Meet URL from text."""
        text = "Meeting link: https://meet.google.com/xyz-abcd-efg"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://meet.google.com/xyz-abcd-efg")

    def test_extract_teams_url(self):
        """Test extracting Microsoft Teams URL from text."""
        text = "Join <https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D>"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D")

    def test_no_meeting_url_found(self):
        """Test when no meeting URL is found in text."""
        text = "This is just regular text with no meeting links"
        result = extract_meeting_url_from_text(text)
        self.assertIsNone(result)

    def test_empty_text(self):
        """Test with empty text."""
        result = extract_meeting_url_from_text("")
        self.assertIsNone(result)

    def test_none_text(self):
        """Test with None text."""
        result = extract_meeting_url_from_text(None)
        self.assertIsNone(result)

    def test_multiple_urls_returns_first_valid(self):
        """Test with multiple URLs, returns first valid meeting URL."""
        text = "Here's a regular link https://example.com and a meeting link https://zoom.us/j/123456789"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://zoom.us/j/123456789")

    def test_html_href_attribute(self):
        # URL appears inside an HTML attribute
        text = '<a href="https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D#frag">Join</a>'
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D#frag")

    def test_url_with_query_and_fragment(self):
        text = "Join: https://meet.google.com/xyz-abcd-efg?hs=122&pli=1#anchor"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://meet.google.com/xyz-abcd-efg?hs=122&pli=1#anchor")

    def test_scheme_less_url(self):
        # Some folks paste zoom links without scheme. We currently don't handle this case.
        text = "Dial in at zoom.us/j/123456789"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, None)

    def test_mixed_case_scheme_and_host(self):
        text = "Use HTTPS://ZOOM.US/j/123456789 to join"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, None)

    def test_newlines_tabs_and_angle_brackets(self):
        text = "Join\n\t<https://meet.google.com/xyz-abcd-efg>\nright now"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://meet.google.com/xyz-abcd-efg")

    def test_ignores_non_meeting_urls_until_valid_found(self):
        text = "See https://example.com/page then https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D")

    # --- desirable behavior (enable after you add URL normalization/stripping) ---

    def test_trailing_punctuation_stripped(self):
        text = "Join here: https://meet.google.com/xyz-abcd-efg."
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://meet.google.com/xyz-abcd-efg")

    def test_wrapped_in_parentheses(self):
        text = "Join (https://zoom.us/j/123456789)"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://zoom.us/j/123456789")

    def test_markdown_link(self):
        text = "Click [Join](https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D)"
        result = extract_meeting_url_from_text(text)
        self.assertEqual(result, "https://teams.microsoft.com/l/meetup-join/19:meeting_YWVhM2VmN2MtOGJhZC00bjdvLTksNzcfffffffffffffffff@thread.v2/0?context=%7B%22Tid%22%3A%2247e45b5d-93ff-45b2-9d36-0e8a2643a5f5%22%2C%22Oid%22%3A%22e3beb726-5124-e6a3-8ee4-ed1e2e43ef68%22%7D")


class TestSyncBotWithCalendarEvent(TestCase):
    """Test the sync_bot_with_calendar_event function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")
        self.calendar_event = CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="test_event_123", meeting_url="https://zoom.us/j/123456789", start_time=timezone.now() + timedelta(hours=1), end_time=timezone.now() + timedelta(hours=2), raw={"test": "data"})
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/987654321", join_at=timezone.now() + timedelta(minutes=30), state=BotStates.SCHEDULED, calendar_event=self.calendar_event)

    @patch("bots.tasks.sync_calendar_task.delete_bot")
    def test_sync_bot_with_deleted_calendar_event(self, mock_delete_bot):
        """Test syncing bot when calendar event is deleted."""
        mock_delete_bot.return_value = (True, None)
        self.calendar_event.is_deleted = True

        sync_bot_with_calendar_event(self.bot, self.calendar_event)

        mock_delete_bot.assert_called_once_with(self.bot)

    @patch("bots.tasks.sync_calendar_task.delete_bot")
    def test_sync_bot_with_deleted_calendar_event_delete_fails(self, mock_delete_bot):
        """Test syncing bot when calendar event is deleted but bot deletion fails."""
        mock_delete_bot.return_value = (False, {"error": "Failed to delete"})
        self.calendar_event.is_deleted = True

        with self.assertLogs("bots.tasks.sync_calendar_task", level="ERROR") as log:
            sync_bot_with_calendar_event(self.bot, self.calendar_event)

        mock_delete_bot.assert_called_once_with(self.bot)
        self.assertIn("Failed to delete bot", log.output[0])

    @patch("bots.tasks.sync_calendar_task.patch_bot")
    def test_sync_bot_updates_meeting_url_and_join_at(self, mock_patch_bot):
        """Test syncing bot when both meeting_url and join_at need updates."""
        mock_patch_bot.return_value = (self.bot, None)

        sync_bot_with_calendar_event(self.bot, self.calendar_event)

        expected_update_data = {"meeting_url": self.calendar_event.meeting_url, "join_at": self.calendar_event.start_time}
        mock_patch_bot.assert_called_once_with(self.bot, expected_update_data)

    @patch("bots.tasks.sync_calendar_task.patch_bot")
    def test_sync_bot_updates_only_meeting_url(self, mock_patch_bot):
        """Test syncing bot when only meeting_url needs update."""
        mock_patch_bot.return_value = (self.bot, None)
        self.bot.join_at = self.calendar_event.start_time  # Make join_at match

        sync_bot_with_calendar_event(self.bot, self.calendar_event)

        expected_update_data = {"meeting_url": self.calendar_event.meeting_url}
        mock_patch_bot.assert_called_once_with(self.bot, expected_update_data)

    @patch("bots.tasks.sync_calendar_task.patch_bot")
    def test_sync_bot_updates_only_join_at(self, mock_patch_bot):
        """Test syncing bot when only join_at needs update."""
        mock_patch_bot.return_value = (self.bot, None)
        self.bot.meeting_url = self.calendar_event.meeting_url  # Make meeting_url match

        sync_bot_with_calendar_event(self.bot, self.calendar_event)

        expected_update_data = {"join_at": self.calendar_event.start_time}
        mock_patch_bot.assert_called_once_with(self.bot, expected_update_data)

    @patch("bots.tasks.sync_calendar_task.patch_bot")
    def test_sync_bot_no_updates_needed(self, mock_patch_bot):
        """Test syncing bot when no updates are needed."""
        self.bot.meeting_url = self.calendar_event.meeting_url
        self.bot.join_at = self.calendar_event.start_time

        sync_bot_with_calendar_event(self.bot, self.calendar_event)

        mock_patch_bot.assert_not_called()

    @patch("bots.tasks.sync_calendar_task.patch_bot")
    def test_sync_bot_patch_fails(self, mock_patch_bot):
        """Test syncing bot when patch operation fails."""
        mock_patch_bot.return_value = (None, {"error": "Patch failed"})

        with self.assertLogs("bots.tasks.sync_calendar_task", level="ERROR") as log:
            sync_bot_with_calendar_event(self.bot, self.calendar_event)

        mock_patch_bot.assert_called_once()
        self.assertIn("Failed to patch bot", log.output[0])


class TestSyncBotsForCalendarEvent(TestCase):
    """Test the sync_bots_for_calendar_event function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")
        self.calendar_event = CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="test_event_123", meeting_url="https://zoom.us/j/123456789", start_time=timezone.now() + timedelta(hours=1), end_time=timezone.now() + timedelta(hours=2), raw={"test": "data"})

    @patch("bots.tasks.sync_calendar_task.sync_bot_with_calendar_event")
    def test_sync_multiple_scheduled_bots(self, mock_sync_bot):
        """Test syncing multiple scheduled bots for a calendar event."""
        # Create multiple bots in different states
        scheduled_bot1 = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/111", state=BotStates.SCHEDULED, calendar_event=self.calendar_event)
        scheduled_bot2 = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/222", state=BotStates.SCHEDULED, calendar_event=self.calendar_event)
        # This bot should not be synced
        Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/333", state=BotStates.READY, calendar_event=self.calendar_event)

        sync_bots_for_calendar_event(self.calendar_event)

        # Should only sync scheduled bots
        self.assertEqual(mock_sync_bot.call_count, 2)
        mock_sync_bot.assert_any_call(scheduled_bot1, self.calendar_event)
        mock_sync_bot.assert_any_call(scheduled_bot2, self.calendar_event)

    @patch("bots.tasks.sync_calendar_task.sync_bot_with_calendar_event")
    def test_sync_no_scheduled_bots(self, mock_sync_bot):
        """Test syncing when there are no scheduled bots."""
        sync_bots_for_calendar_event(self.calendar_event)
        mock_sync_bot.assert_not_called()


class TestEnqueueSyncCalendarTask(TransactionTestCase):
    """Test the enqueue_sync_calendar_task function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id", sync_task_requested_at=timezone.now() - timedelta(days=1))

    @patch("bots.tasks.sync_calendar_task.sync_calendar.delay")
    def test_enqueue_sync_calendar_task(self, mock_delay):
        """Test enqueuing sync calendar task updates timestamp and calls delay."""
        initial_sync_task_enqueued_at = self.calendar.sync_task_enqueued_at

        enqueue_sync_calendar_task(self.calendar)

        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.sync_task_enqueued_at)
        self.assertNotEqual(self.calendar.sync_task_enqueued_at, initial_sync_task_enqueued_at)
        self.assertEqual(self.calendar.sync_task_requested_at, None)
        mock_delay.assert_called_once_with(self.calendar.id)


class TestSyncCalendar(TestCase):
    """Test the sync_calendar Celery task."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    @patch("bots.tasks.sync_calendar_task.GoogleCalendarSyncHandler")
    def test_sync_calendar_google(self, mock_handler_class):
        """Test sync_calendar task creates GoogleCalendarSyncHandler for Google calendar."""
        calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")
        mock_handler = Mock()
        mock_handler.sync_events.return_value = {"success": True}
        mock_handler_class.return_value = mock_handler

        result = sync_calendar(calendar.id)

        mock_handler_class.assert_called_once_with(calendar.id)
        mock_handler.sync_events.assert_called_once()
        self.assertEqual(result, {"success": True})

    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler")
    def test_sync_calendar_microsoft(self, mock_handler_class):
        """Test sync_calendar task creates MicrosoftCalendarSyncHandler for Microsoft calendar."""
        calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.MICROSOFT, client_id="test_client_id")
        mock_handler = Mock()
        mock_handler.sync_events.return_value = {"success": True}
        mock_handler_class.return_value = mock_handler

        result = sync_calendar(calendar.id)

        mock_handler_class.assert_called_once_with(calendar.id)
        mock_handler.sync_events.assert_called_once()
        self.assertEqual(result, {"success": True})

    def test_sync_calendar_unsupported_platform(self):
        """Test sync_calendar task raises error for unsupported platform."""
        calendar = Calendar.objects.create(project=self.project, platform="unsupported", client_id="test_client_id")

        with self.assertRaises(ValueError) as cm:
            sync_calendar(calendar.id)

        self.assertIn("Unsupported calendar platform", str(cm.exception))


class TestCalendarSyncHandler(TestCase):
    """Test the CalendarSyncHandler base class."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")

    def test_init(self):
        """Test CalendarSyncHandler initialization."""
        handler = CalendarSyncHandler(self.calendar.id)
        self.assertEqual(handler.calendar, self.calendar)
        self.assertIsNone(handler.time_window_start)
        self.assertIsNone(handler.time_window_end)

    def test_get_local_events_in_window(self):
        """Test _get_local_events_in_window method."""
        handler = CalendarSyncHandler(self.calendar.id)
        now = timezone.now()
        handler.time_window_start = now - timedelta(days=1)
        handler.time_window_end = now + timedelta(days=1)

        # Create events within and outside the window
        event_in_window = CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="event_in_window", start_time=now, end_time=now + timedelta(hours=1), raw={"test": "data"})
        CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="event_outside_window", start_time=now + timedelta(days=2), end_time=now + timedelta(days=2, hours=1), raw={"test": "data"})
        CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="deleted_event", start_time=now, end_time=now + timedelta(hours=1), is_deleted=True, raw={"test": "data"})

        local_events = handler._get_local_events_in_window()

        self.assertEqual(len(local_events), 1)
        self.assertIn("event_in_window", local_events)
        self.assertEqual(local_events["event_in_window"], event_in_window)

    def test_mark_calendar_event_as_deleted(self):
        """Test _mark_calendar_event_as_deleted method."""
        handler = CalendarSyncHandler(self.calendar.id)
        event = CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="test_event", start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1), raw={"test": "data"})

        self.assertFalse(event.is_deleted)
        handler._mark_calendar_event_as_deleted(event)

        event.refresh_from_db()
        self.assertTrue(event.is_deleted)

    @patch("bots.tasks.sync_calendar_task.sync_bots_for_calendar_event")
    def test_upsert_calendar_event_create_new(self, mock_sync_bots):
        """Test _upsert_calendar_event creates new event."""
        handler = CalendarSyncHandler(self.calendar.id)
        handler._remote_event_to_calendar_event_data = Mock(return_value={"platform_uuid": "new_event_123", "meeting_url": "https://zoom.us/j/123456789", "start_time": timezone.now(), "end_time": timezone.now() + timedelta(hours=1), "raw": {"test": "data"}})

        remote_event = {"id": "new_event_123", "test": "data"}

        local_event, was_created, was_updated = handler._upsert_calendar_event(remote_event)

        self.assertTrue(was_created)
        self.assertFalse(was_updated)
        self.assertEqual(local_event.platform_uuid, "new_event_123")
        mock_sync_bots.assert_not_called()

    @patch("bots.tasks.sync_calendar_task.sync_bots_for_calendar_event")
    def test_upsert_calendar_event_update_existing(self, mock_sync_bots):
        """Test _upsert_calendar_event updates existing event."""
        handler = CalendarSyncHandler(self.calendar.id)
        CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="existing_event_123", meeting_url="https://zoom.us/j/111", start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1), raw={"old": "data"})

        handler._remote_event_to_calendar_event_data = Mock(return_value={"platform_uuid": "existing_event_123", "meeting_url": "https://zoom.us/j/222", "start_time": timezone.now() + timedelta(minutes=30), "end_time": timezone.now() + timedelta(hours=1, minutes=30), "raw": {"new": "data"}})

        remote_event = {"id": "existing_event_123", "test": "data"}

        local_event, was_created, was_updated = handler._upsert_calendar_event(remote_event)

        self.assertFalse(was_created)
        self.assertTrue(was_updated)
        self.assertEqual(local_event.meeting_url, "https://zoom.us/j/222")
        self.assertEqual(local_event.raw, {"new": "data"})
        mock_sync_bots.assert_called_once_with(local_event)

    @patch("bots.tasks.sync_calendar_task.sync_bots_for_calendar_event")
    def test_upsert_calendar_event_no_change(self, mock_sync_bots):
        """Test _upsert_calendar_event when no changes are needed."""
        handler = CalendarSyncHandler(self.calendar.id)
        existing_event = CalendarEvent.objects.create(calendar=self.calendar, platform_uuid="existing_event_123", meeting_url="https://zoom.us/j/111", start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1), raw={"same": "data"})

        handler._remote_event_to_calendar_event_data = Mock(return_value={"platform_uuid": "existing_event_123", "meeting_url": "https://zoom.us/j/111", "start_time": existing_event.start_time, "end_time": existing_event.end_time, "raw": {"same": "data"}})

        remote_event = {"id": "existing_event_123", "test": "data"}

        local_event, was_created, was_updated = handler._upsert_calendar_event(remote_event)

        self.assertFalse(was_created)
        self.assertFalse(was_updated)
        self.assertEqual(local_event, existing_event)
        mock_sync_bots.assert_not_called()


class TestCalendarSyncHandlerSyncEvents(TransactionTestCase):
    """Test the sync_events method with full integration."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")

    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_sync_events_success_flow(self, mock_trigger_webhook):
        """Test successful sync_events flow."""
        handler = CalendarSyncHandler(self.calendar.id)

        # Mock abstract methods
        handler._get_access_token = Mock(return_value="mock_token")
        handler._list_events = Mock(return_value=[{"id": "event_1", "test": "data1"}, {"id": "event_2", "test": "data2"}])
        handler._get_event_by_id = Mock(return_value=None)
        handler._remote_event_to_calendar_event_data = Mock(side_effect=[{"platform_uuid": "event_1", "meeting_url": "https://zoom.us/j/111", "start_time": timezone.now(), "end_time": timezone.now() + timedelta(hours=1), "raw": {"event": "1"}}, {"platform_uuid": "event_2", "meeting_url": "https://zoom.us/j/222", "start_time": timezone.now() + timedelta(hours=2), "end_time": timezone.now() + timedelta(hours=3), "raw": {"event": "2"}}])
        handler._refresh_notification_channels = Mock()

        result = handler.sync_events()

        self.assertTrue(result["success"])
        self.assertEqual(result["created_count"], 2)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["total_remote_events"], 2)

        # Verify calendar state is updated
        self.calendar.refresh_from_db()
        self.assertEqual(self.calendar.state, CalendarStates.CONNECTED)
        self.assertIsNotNone(self.calendar.last_successful_sync_at)
        self.assertIsNone(self.calendar.connection_failure_data)

        # Verify webhook is triggered
        mock_trigger_webhook.assert_called_once_with(webhook_trigger_type=WebhookTriggerTypes.CALENDAR_EVENTS_UPDATE, calendar=self.calendar, payload=calendar_webhook_payload(self.calendar))

    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_sync_events_no_webhook_when_no_changes(self, mock_trigger_webhook):
        """Test that no webhook is triggered when sync finds no changes."""
        now = timezone.now()
        # Pre-create an existing event that matches what will be returned from remote
        existing_event = CalendarEvent.objects.create(
            calendar=self.calendar,
            platform_uuid="existing_event_1",
            meeting_url="https://zoom.us/j/111",
            start_time=now,
            end_time=now + timedelta(hours=1),
            raw={"event": "1"},
        )

        handler = CalendarSyncHandler(self.calendar.id)

        # Mock abstract methods - return the same event data as already exists
        handler._get_access_token = Mock(return_value="mock_token")
        handler._list_events = Mock(return_value=[{"id": "existing_event_1", "test": "data1"}])
        handler._get_event_by_id = Mock(return_value=None)
        handler._remote_event_to_calendar_event_data = Mock(
            return_value={
                "platform_uuid": "existing_event_1",
                "meeting_url": "https://zoom.us/j/111",  # Same as existing
                "start_time": existing_event.start_time,
                "end_time": existing_event.end_time,
                "raw": {"event": "1"},  # Same as existing
            }
        )
        handler._refresh_notification_channels = Mock()

        result = handler.sync_events()

        self.assertTrue(result["success"])
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["deleted_count"], 0)

        # Verify webhook is NOT triggered since nothing changed
        mock_trigger_webhook.assert_not_called()

    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    @patch("bots.tasks.sync_calendar_task.remove_bots_from_calendar")
    def test_sync_events_authentication_error(self, mock_remove_bots, mock_trigger_webhook):
        """Test sync_events handles authentication errors properly."""
        # Create notification channels that should be deleted on auth error
        CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="channel_1",
            unique_key="first_channel_" + self.calendar.object_id,
            expires_at=timezone.now() + timedelta(days=7),
            raw={"test": "data"},
        )
        CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="channel_2",
            unique_key="channel_1",
            expires_at=timezone.now() + timedelta(days=14),
            raw={"test": "data"},
        )
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 2)

        handler = CalendarSyncHandler(self.calendar.id)
        handler._get_access_token = Mock(side_effect=CalendarAPIAuthenticationError("Auth failed"))
        handler._refresh_notification_channels = Mock()

        result = handler.sync_events()

        self.assertIsNone(result)  # No return value on auth error

        # Verify calendar state is updated to disconnected
        self.calendar.refresh_from_db()
        self.assertEqual(self.calendar.state, CalendarStates.DISCONNECTED)
        self.assertIsNotNone(self.calendar.connection_failure_data)
        self.assertIn("Auth failed", self.calendar.connection_failure_data["error"])

        # Verify bots are removed and webhook is triggered
        mock_remove_bots.assert_called_once_with(calendar=self.calendar, project=self.calendar.project)
        mock_trigger_webhook.assert_called_once_with(webhook_trigger_type=WebhookTriggerTypes.CALENDAR_STATE_CHANGE, calendar=self.calendar, payload=calendar_webhook_payload(self.calendar))

        # Verify notification channels are deleted
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 0)

    def test_sync_events_general_exception(self):
        """Test sync_events handles general exceptions properly."""
        handler = CalendarSyncHandler(self.calendar.id)
        handler._get_access_token = Mock(side_effect=Exception("General error"))

        with self.assertRaises(Exception):
            handler.sync_events()

        # Verify calendar last_attempted_sync_at is updated
        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.last_attempted_sync_at)


class TestGoogleCalendarSyncHandler(TestCase):
    """Test the GoogleCalendarSyncHandler class."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="test_client_id")
        self.calendar.set_credentials({"client_secret": "test_secret", "refresh_token": "test_refresh_token"})

    def test_parse_event_datetime_with_datetime(self):
        """Test parsing Google Calendar datetime with specific time."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        event_datetime = {"dateTime": "2023-12-01T10:00:00Z"}

        result = handler._parse_event_datetime(event_datetime)

        expected = datetime(2023, 12, 1, 10, 0, 0, tzinfo=python_timezone.utc)
        self.assertEqual(result, expected)

    def test_parse_event_datetime_with_date(self):
        """Test parsing Google Calendar datetime with date only (all-day event)."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        event_datetime = {"date": "2023-12-01"}

        result = handler._parse_event_datetime(event_datetime)

        expected = datetime(2023, 12, 1, 0, 0, 0, tzinfo=python_timezone.utc)
        self.assertEqual(result, expected)

    def test_parse_event_datetime_invalid_format(self):
        """Test parsing invalid Google Calendar datetime format."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        event_datetime = {"invalid": "format"}

        with self.assertRaises(ValueError):
            handler._parse_event_datetime(event_datetime)

    def test_truncate_large_text_fields(self):
        """Test truncation of large text fields in Google Calendar events."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        large_text = "x" * 10000
        google_event = {"description": large_text, "summary": large_text, "other_field": "normal_value"}

        result = handler._truncate_large_text_fields_in_gcal_event(google_event)

        self.assertEqual(len(result["description"]), 8000)
        self.assertEqual(len(result["summary"]), 8000)
        self.assertEqual(result["other_field"], "normal_value")
        # Verify original is not modified
        self.assertEqual(len(google_event["description"]), 10000)

    def test_remote_event_to_calendar_event_data(self):
        """Test converting Google Calendar event to CalendarEvent data."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)

        google_event = {"id": "google_event_123", "start": {"dateTime": "2023-12-01T10:00:00Z"}, "end": {"dateTime": "2023-12-01T11:00:00Z"}, "status": "confirmed", "iCalUID": "ical_uid_123", "description": "Event description", "summary": "Event summary", "attendees": [{"email": "user1@example.com", "displayName": "User One"}, {"email": "user2@example.com", "displayName": "User Two"}], "conferenceData": {"entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-def-ghi"}]}}

        result = handler._remote_event_to_calendar_event_data(google_event)

        self.assertEqual(result["platform_uuid"], "google_event_123")
        self.assertEqual(result["meeting_url"], "https://meet.google.com/abc-def-ghi")
        self.assertEqual(result["ical_uid"], "ical_uid_123")
        self.assertFalse(result["is_deleted"])
        self.assertEqual(len(result["attendees"]), 2)
        self.assertEqual(result["attendees"][0]["email"], "user1@example.com")

    def test_remote_event_to_calendar_event_data_cancelled(self):
        """Test converting cancelled Google Calendar event."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)

        google_event = {"id": "cancelled_event_123", "start": {"dateTime": "2023-12-01T10:00:00Z"}, "end": {"dateTime": "2023-12-01T11:00:00Z"}, "status": "cancelled"}

        result = handler._remote_event_to_calendar_event_data(google_event)

        self.assertTrue(result["is_deleted"])

    @patch("requests.post")
    def test_get_access_token_success(self, mock_post):
        """Test successful access token retrieval."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "new_access_token"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = handler._get_access_token()

        self.assertEqual(result, "new_access_token")
        mock_post.assert_called_once()

    @patch("requests.post")
    def test_get_access_token_invalid_grant(self, mock_post):
        """Test access token retrieval with invalid grant."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        mock_response = Mock()
        mock_response.json.return_value = {"error": "invalid_grant"}

        # Create a proper RequestException with a response attribute
        exception = requests.RequestException()
        exception.response = mock_response
        mock_response.raise_for_status.side_effect = exception
        mock_post.return_value = mock_response

        with self.assertRaises(CalendarAPIAuthenticationError):
            handler._get_access_token()

    @patch("requests.Session")
    def test_list_events_success(self, mock_session_class):
        """Test successful event listing."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)
        handler.time_window_start = timezone.now() - timedelta(days=1)
        handler.time_window_end = timezone.now() + timedelta(days=1)

        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"items": [{"id": "event_1", "summary": "Event 1"}, {"id": "event_2", "summary": "Event 2"}]}
        mock_response.raise_for_status.return_value = None
        mock_session.send.return_value = mock_response
        mock_session_class.return_value.__enter__.return_value = mock_session

        result = handler._list_events("mock_token")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "event_1")
        self.assertEqual(result[1]["id"], "event_2")

    @patch("requests.Session")
    def test_get_event_by_id_success(self, mock_session_class):
        """Test successful individual event retrieval."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)

        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"id": "event_123", "summary": "Individual Event"}
        mock_response.raise_for_status.return_value = None
        mock_session.send.return_value = mock_response
        mock_session_class.return_value.__enter__.return_value = mock_session

        result = handler._get_event_by_id("event_123", "mock_token")

        self.assertEqual(result["id"], "event_123")

    @patch("requests.Session")
    def test_get_event_by_id_not_found(self, mock_session_class):
        """Test individual event retrieval when event not found."""
        handler = GoogleCalendarSyncHandler(self.calendar.id)

        # Create a real Response object with 404 status
        response = requests.Response()
        response.status_code = 404
        response._content = b'{"error": {"code": 404, "message": "Not Found"}}'
        response.headers["content-type"] = "application/json"

        # Create a real HTTPError with the response
        http_error = requests.HTTPError("404 Client Error: Not Found for url: https://www.googleapis.com/calendar/v3/calendars/primary/events/nonexistent_event", response=response)

        mock_session = Mock()
        mock_session.send.side_effect = http_error
        mock_session_class.return_value.__enter__.return_value = mock_session

        result = handler._get_event_by_id("nonexistent_event", "mock_token")

        self.assertIsNone(result)


class TestMicrosoftCalendarSyncHandler(TestCase):
    """Test the MicrosoftCalendarSyncHandler class."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.MICROSOFT, client_id="test_client_id")
        self.calendar.set_credentials({"client_secret": "test_secret", "refresh_token": "test_refresh_token"})

    def test_parse_ms_datetime_utc(self):
        """Test parsing Microsoft datetime in UTC."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        result = handler._parse_ms_datetime("2023-12-01T10:00:00.0000000", "UTC")

        expected = datetime(2023, 12, 1, 10, 0, 0, tzinfo=python_timezone.utc)
        self.assertEqual(result, expected)

    def test_parse_ms_datetime_with_offset(self):
        """Test parsing Microsoft datetime with timezone offset."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        result = handler._parse_ms_datetime("2023-12-01T10:00:00.0000000+05:00", "Asia/Kolkata")

        # Should preserve the offset from the string
        self.assertEqual(result.hour, 10)
        self.assertIsNotNone(result.tzinfo)

    def test_parse_ms_datetime_truncate_fractional_seconds(self):
        """Test parsing Microsoft datetime with too many fractional seconds."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        # Microsoft can return 7-digit fractional seconds
        result = handler._parse_ms_datetime("2023-12-01T10:00:00.1234567", "UTC")

        # Should be truncated to 6 digits for Python compatibility
        self.assertEqual(result.microsecond, 123456)

    def test_extract_meeting_url_from_online_meeting(self):
        """Test extracting meeting URL from onlineMeeting object."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        event = {"onlineMeeting": {"joinUrl": "https://teams.microsoft.com/meet/123123213?p=123123213"}}

        result = handler._extract_meeting_url(event)

        self.assertEqual(result, "https://teams.microsoft.com/meet/123123213?p=123123213")

    def test_extract_meeting_url_from_legacy_field(self):
        """Test extracting meeting URL from legacy onlineMeetingUrl field."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        event = {"onlineMeetingUrl": "https://teams.microsoft.com/meet/123123213?p=11111111"}

        result = handler._extract_meeting_url(event)

        self.assertEqual(result, "https://teams.microsoft.com/meet/123123213?p=11111111")

    @patch("bots.tasks.sync_calendar_task.extract_meeting_url_from_text")
    def test_extract_meeting_url_from_subject_and_body(self, mock_extract_url):
        """Test extracting meeting URL from subject and body when no direct URL."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)
        mock_extract_url.side_effect = [
            None,  # First call for subject
            "https://zoom.us/j/123456789",  # Second call for body
        ]

        event = {"subject": "Meeting with client", "body": {"content": "Join us at https://zoom.us/j/123456789"}}

        result = handler._extract_meeting_url(event)

        self.assertEqual(result, "https://zoom.us/j/123456789")
        self.assertEqual(mock_extract_url.call_count, 2)

    def test_truncate_large_text_fields_in_ms_event(self):
        """Test truncation of large text fields in Microsoft events."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)
        large_text = "x" * 10000
        ms_event = {"body": {"content": large_text}, "subject": "Normal subject"}

        result = handler._truncate_large_text_fields_in_ms_event(ms_event)

        self.assertEqual(len(result["body"]["content"]), 8000)
        self.assertEqual(result["subject"], "Normal subject")

    def test_remote_event_to_calendar_event_data(self):
        """Test converting Microsoft event to CalendarEvent data."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)

        ms_event = {"id": "ms_event_123", "start": {"dateTime": "2023-12-01T10:00:00.0000000", "timeZone": "UTC"}, "end": {"dateTime": "2023-12-01T11:00:00.0000000", "timeZone": "UTC"}, "isCancelled": False, "iCalUId": "ical_uid_123", "attendees": [{"emailAddress": {"address": "user1@example.com", "name": "User One"}}], "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/meet/123123213?p=3333333"}}

        result = handler._remote_event_to_calendar_event_data(ms_event)

        self.assertEqual(result["platform_uuid"], "ms_event_123")
        self.assertEqual(result["meeting_url"], "https://teams.microsoft.com/meet/123123213?p=3333333")
        self.assertEqual(result["ical_uid"], "ical_uid_123")
        self.assertFalse(result["is_deleted"])
        self.assertEqual(len(result["attendees"]), 1)
        self.assertEqual(result["attendees"][0]["email"], "user1@example.com")

    @patch("requests.post")
    def test_get_access_token_with_token_rotation(self, mock_post):
        """Test access token retrieval with Microsoft's token rotation."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "new_access_token", "refresh_token": "new_refresh_token"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        original_credentials = self.calendar.get_credentials()

        result = handler._get_access_token()

        self.assertEqual(result, "new_access_token")

        self.calendar.refresh_from_db()

        # Verify new refresh token is saved
        updated_credentials = self.calendar.get_credentials()
        self.assertEqual(updated_credentials["refresh_token"], "new_refresh_token")
        self.assertNotEqual(updated_credentials["refresh_token"], original_credentials["refresh_token"])

    @patch("requests.Session")
    def test_list_events_with_pagination(self, mock_session_class):
        """Test listing events with pagination support."""
        handler = MicrosoftCalendarSyncHandler(self.calendar.id)
        handler.time_window_start = timezone.now() - timedelta(days=1)
        handler.time_window_end = timezone.now() + timedelta(days=1)

        mock_session = Mock()
        # First page
        first_response = Mock()
        first_response.json.return_value = {"value": [{"id": "event_1"}, {"id": "event_2"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skip=2"}
        first_response.raise_for_status.return_value = None

        # Second page
        second_response = Mock()
        second_response.json.return_value = {
            "value": [{"id": "event_3"}]
            # No nextLink indicates end of pagination
        }
        second_response.raise_for_status.return_value = None

        mock_session.send.side_effect = [first_response, second_response]
        mock_session_class.return_value.__enter__.return_value = mock_session

        result = handler._list_events("mock_token")

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], "event_1")
        self.assertEqual(result[2]["id"], "event_3")
        self.assertEqual(mock_session.send.call_count, 2)


class TestNotificationChannelRefreshWithScheduler(TransactionTestCase):
    """Test that notification channels are refreshed correctly even in worst-case scenarios."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test_client_id",
            state=CalendarStates.CONNECTED,
        )
        self.calendar.set_credentials({"client_secret": "test_secret", "refresh_token": "test_refresh_token"})
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    @patch("bots.tasks.sync_calendar_task.GoogleCalendarSyncHandler._list_events")
    @patch("bots.tasks.sync_calendar_task.GoogleCalendarSyncHandler._get_event_by_id")
    @patch("bots.tasks.sync_calendar_task.GoogleCalendarSyncHandler._get_access_token")
    @patch("bots.tasks.sync_calendar_task.GoogleCalendarSyncHandler._make_gcal_request")
    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_worst_case_notification_channel_refresh_before_26_hour_threshold(self, mock_trigger_webhook, mock_make_gcal_request, mock_get_access_token, mock_get_event, mock_list_events):
        """
        Test that even in the worst-case scenario where a sync happens right before
        the 26-hour mark, the scheduler will still trigger a sync that creates a new
        notification channel before the existing one expires.
        """
        # Set up the timeline
        now = timezone.now()

        # Step 1: Create a notification channel with an expiration time that is just before the renewal threshold
        # so that if we run the sync task right now, it will just miss creating a new channel
        expiration_time = now + timedelta(hours=GoogleCalendarSyncHandler.NOTIFICATION_CHANNEL_RENEWAL_THRESHOLD_HOURS, minutes=1)
        initial_channel = CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="initial_channel_uuid",
            unique_key="first_channel_" + self.calendar.object_id,
            expires_at=expiration_time,
            raw={"test": "data"},
        )

        # Step 2: Run the sync task and verify that we did not create a new channel.
        # Mock the API calls to avoid actual HTTP requests
        mock_get_access_token.return_value = "mock_token"
        mock_list_events.return_value = []  # No events to sync
        mock_get_event.return_value = None
        mock_make_gcal_request.return_value = {
            "expiration": (datetime.now() + timedelta(days=7)).timestamp() * 1000,
        }

        enqueue_sync_calendar_task(self.calendar)

        # Verify that count did not change
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1)
        # Verify that the id of the notification channel is the same as the initial channel
        first_notification_channel = CalendarNotificationChannel.objects.filter(calendar=self.calendar).first()
        self.assertEqual(first_notification_channel.platform_uuid, initial_channel.platform_uuid)

        # Step 3: Run the scheduler's periodic calendar sync logic
        # Import here to avoid circular imports
        from bots.management.commands.run_scheduler import Command

        command = Command()

        # Mock timezone.now() to return our test time
        with patch("django.utils.timezone.now", return_value=self.calendar.sync_task_enqueued_at + timedelta(hours=CALENDAR_SYNC_THRESHOLD_HOURS)):
            # Assert that the latest notification channel for the calendar has NOT expired yet
            self.assertGreater(CalendarNotificationChannel.objects.filter(calendar=self.calendar).order_by("-expires_at").first().expires_at, timezone.now())
            command._run_periodic_calendar_syncs()

        # Step 4: Verify that a new notification channel was created
        # There should now be 2 channels total (initial + newly created)
        all_channels = CalendarNotificationChannel.objects.filter(calendar=self.calendar).order_by("created_at")
        self.assertEqual(all_channels.count(), 2, "A new notification channel should have been created")

        # Verify the initial channel is still there
        self.assertEqual(all_channels[0].platform_uuid, "initial_channel_uuid")

        # Now switch to the time that the original notification channel will expire + the cleanup threshold and make sure that it is deleted
        with patch("django.utils.timezone.now", return_value=initial_channel.expires_at + timedelta(hours=GoogleCalendarSyncHandler.NOTIFICATION_CHANNEL_CLEANUP_THRESHOLD_HOURS, minutes=1)):
            command._run_periodic_calendar_syncs()
            self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1, "The initial notification channel should be deleted")
            self.assertFalse(CalendarNotificationChannel.objects.filter(calendar=self.calendar, platform_uuid="initial_channel_uuid").exists(), "The initial notification channel should be deleted")


class TestMicrosoftNotificationChannelLifecycle(TransactionTestCase):
    """Test Microsoft calendar notification channel creation, extension, and deletion."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.MICROSOFT,
            client_id="test_client_id",
            state=CalendarStates.CONNECTED,
        )
        self.calendar.set_credentials({"client_secret": "test_secret", "refresh_token": "test_refresh_token"})
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._list_events")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_event_by_id")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_access_token")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._make_graph_request")
    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_first_sync_creates_notification_channel(self, mock_trigger_webhook, mock_make_graph_request, mock_get_access_token, mock_get_event, mock_list_events):
        """Test that the first sync creates a notification channel for Microsoft calendars."""
        # Verify no notification channels exist initially
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 0)

        # Mock the API calls
        mock_get_access_token.return_value = "mock_token"
        mock_list_events.return_value = []
        mock_get_event.return_value = None

        # Mock the subscription creation response
        new_expires_at = timezone.now() + timedelta(days=7)
        mock_make_graph_request.return_value = {
            "id": "microsoft_subscription_uuid_123",
            "expirationDateTime": new_expires_at.isoformat(),
        }

        # Run the sync task
        enqueue_sync_calendar_task(self.calendar)

        # Verify a notification channel was created
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1)
        notification_channel = CalendarNotificationChannel.objects.get(calendar=self.calendar)
        self.assertEqual(notification_channel.platform_uuid, "microsoft_subscription_uuid_123")

    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._list_events")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_event_by_id")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_access_token")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._make_graph_request")
    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_notification_channel_extended_after_24_hours(self, mock_trigger_webhook, mock_make_graph_request, mock_get_access_token, mock_get_event, mock_list_events):
        """Test that Microsoft notification channels are extended when sync happens over 24 hours later."""
        from bots.tasks.sync_calendar_task import MicrosoftCalendarSyncHandler

        # Create an existing notification channel that was created over 24 hours ago
        original_expires_at = timezone.now() + timedelta(minutes=MicrosoftCalendarSyncHandler.NOTIFICATION_CHANNEL_EXPIRATION_TIME_MINUTES - 60 * 25)  # Will need extension
        notification_channel = CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="existing_subscription_uuid",
            unique_key=f"notification_channel_{self.calendar.object_id}",
            expires_at=original_expires_at,
            raw={"id": "existing_subscription_uuid"},
        )

        # Verify notification channel exists
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1)

        # Mock the API calls
        mock_get_access_token.return_value = "mock_token"
        mock_list_events.return_value = []
        mock_get_event.return_value = None

        # Mock the subscription extension response (PATCH request)
        new_expires_at = timezone.now() + timedelta(minutes=MicrosoftCalendarSyncHandler.NOTIFICATION_CHANNEL_EXPIRATION_TIME_MINUTES)
        mock_make_graph_request.return_value = {
            "id": "existing_subscription_uuid",
            "expirationDateTime": new_expires_at.isoformat(),
        }

        # Run the sync task
        enqueue_sync_calendar_task(self.calendar)

        # Verify the notification channel was extended, not replaced
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1)
        notification_channel.refresh_from_db()
        self.assertEqual(notification_channel.platform_uuid, "existing_subscription_uuid")

        # Verify the expiration time was updated
        self.assertGreater(notification_channel.expires_at, original_expires_at)

        # Verify the PATCH request was made to extend the subscription
        mock_make_graph_request.assert_called()
        # Find the PATCH call (should be for extending the subscription)
        patch_calls = [call for call in mock_make_graph_request.call_args_list if call.kwargs.get("method") == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        patch_call = patch_calls[0]
        self.assertIn("existing_subscription_uuid", patch_call.args[0])

    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._list_events")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_event_by_id")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._get_access_token")
    @patch("bots.tasks.sync_calendar_task.MicrosoftCalendarSyncHandler._make_graph_request")
    @patch("bots.tasks.sync_calendar_task.trigger_webhook")
    def test_notification_channel_deleted_when_not_found_in_graph_api(self, mock_trigger_webhook, mock_make_graph_request, mock_get_access_token, mock_get_event, mock_list_events):
        """Test that if the notification channel is not found in Graph API (404), it gets deleted in the DB."""
        from bots.tasks.sync_calendar_task import MicrosoftCalendarSyncHandler

        # Create an existing notification channel that needs extension
        original_expires_at = timezone.now() + timedelta(minutes=MicrosoftCalendarSyncHandler.NOTIFICATION_CHANNEL_EXPIRATION_TIME_MINUTES - 60 * 25)  # Will need extension
        CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="orphaned_subscription_uuid",
            unique_key=f"notification_channel_{self.calendar.object_id}",
            expires_at=original_expires_at,
            raw={"id": "orphaned_subscription_uuid"},
        )

        # Verify notification channel exists
        self.assertEqual(CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(), 1)

        # Mock the API calls
        mock_get_access_token.return_value = "mock_token"
        mock_list_events.return_value = []
        mock_get_event.return_value = None

        # Create a 404 HTTPError to simulate the subscription not found in Graph API
        response_404 = requests.Response()
        response_404.status_code = 404
        response_404._content = b'{"error": {"code": "ResourceNotFound", "message": "The subscription was not found."}}'
        response_404.headers["content-type"] = "application/json"

        http_error_404 = requests.HTTPError(
            "404 Client Error: Not Found for url: https://graph.microsoft.com/v1.0/subscriptions/orphaned_subscription_uuid",
            response=response_404,
        )

        # Make the PATCH request raise a 404 error
        mock_make_graph_request.side_effect = http_error_404

        # Run the sync task
        enqueue_sync_calendar_task(self.calendar)

        # Verify the notification channel was deleted from the database
        self.assertEqual(
            CalendarNotificationChannel.objects.filter(calendar=self.calendar).count(),
            0,
            "The orphaned notification channel should be deleted when Graph API returns 404",
        )
        self.assertFalse(
            CalendarNotificationChannel.objects.filter(platform_uuid="orphaned_subscription_uuid").exists(),
            "The notification channel with orphaned_subscription_uuid should not exist",
        )
