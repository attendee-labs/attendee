from django.test import TestCase

from accounts.models import Organization
from bots.calendars_api_utils import create_calendar, delete_calendar
from bots.models import Bot, BotStates, Calendar, CalendarEvent, CalendarPlatform, CalendarStates, Project


class TestCreateCalendar(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_create_calendar_success(self):
        """Test successful calendar creation with all valid data."""
        calendar_data = {"platform": CalendarPlatform.GOOGLE, "client_id": "test_client_id_123", "client_secret": "test_client_secret_456", "refresh_token": "test_refresh_token_789", "metadata": {"department": "engineering", "team": "backend"}, "deduplication_key": "unique_calendar_key", "platform_uuid": "google_calendar_uuid_123"}

        calendar, error = create_calendar(calendar_data, self.project)

        # Verify successful creation
        self.assertIsNotNone(calendar)
        self.assertIsNone(error)

        # Verify calendar properties
        self.assertEqual(calendar.project, self.project)
        self.assertEqual(calendar.platform, CalendarPlatform.GOOGLE)
        self.assertEqual(calendar.client_id, "test_client_id_123")
        self.assertEqual(calendar.state, CalendarStates.CONNECTED)
        self.assertEqual(calendar.metadata, {"department": "engineering", "team": "backend"})
        self.assertEqual(calendar.deduplication_key, "unique_calendar_key")
        self.assertEqual(calendar.platform_uuid, "google_calendar_uuid_123")

        # Verify credentials are encrypted and stored
        credentials = calendar.get_credentials()
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials["client_secret"], "test_client_secret_456")
        self.assertEqual(credentials["refresh_token"], "test_refresh_token_789")

        # Verify object_id is generated
        self.assertIsNotNone(calendar.object_id)
        self.assertTrue(calendar.object_id.startswith("cal_"))

    def test_create_calendar_with_invalid_data(self):
        """Test calendar creation with invalid/missing data."""
        # Test with missing required fields
        invalid_data = {
            "platform": CalendarPlatform.GOOGLE,
            # Missing client_id, client_secret, refresh_token
        }

        calendar, error = create_calendar(invalid_data, self.project)

        # Verify failure
        self.assertIsNone(calendar)
        self.assertIsNotNone(error)

        # Check that required field errors are present
        self.assertIn("client_id", error)
        self.assertIn("client_secret", error)
        self.assertIn("refresh_token", error)

        # Test with invalid platform
        invalid_platform_data = {"platform": "invalid_platform", "client_id": "test_client_id", "client_secret": "test_client_secret", "refresh_token": "test_refresh_token"}

        calendar, error = create_calendar(invalid_platform_data, self.project)

        # Verify failure
        self.assertIsNone(calendar)
        self.assertIsNotNone(error)
        self.assertIn("platform", error)

    def test_create_calendar_with_duplicate_deduplication_key(self):
        """Test calendar creation with duplicate deduplication key in same project."""
        deduplication_key = "duplicate_key_test"

        # Create first calendar
        calendar_data_1 = {"platform": CalendarPlatform.GOOGLE, "client_id": "test_client_id_1", "client_secret": "test_client_secret_1", "refresh_token": "test_refresh_token_1", "deduplication_key": deduplication_key}

        calendar1, error1 = create_calendar(calendar_data_1, self.project)

        # First creation should succeed
        self.assertIsNotNone(calendar1)
        self.assertIsNone(error1)
        self.assertEqual(calendar1.deduplication_key, deduplication_key)

        # Create second calendar with same deduplication key
        calendar_data_2 = {
            "platform": CalendarPlatform.MICROSOFT,
            "client_id": "test_client_id_2",
            "client_secret": "test_client_secret_2",
            "refresh_token": "test_refresh_token_2",
            "deduplication_key": deduplication_key,  # Same key
        }

        calendar2, error2 = create_calendar(calendar_data_2, self.project)

        # Second creation should fail
        self.assertIsNone(calendar2)
        self.assertIsNotNone(error2)
        self.assertIn("deduplication_key", error2)
        self.assertIn("already exists", error2["deduplication_key"][0])

        # Verify only one calendar exists
        self.assertEqual(Calendar.objects.filter(project=self.project).count(), 1)

        # Test that same deduplication key works in different project
        other_organization = Organization.objects.create(name="Other Organization")
        other_project = Project.objects.create(name="Other Project", organization=other_organization)

        calendar3, error3 = create_calendar(calendar_data_2, other_project)

        # Should succeed in different project
        self.assertIsNotNone(calendar3)
        self.assertIsNone(error3)
        self.assertEqual(calendar3.deduplication_key, deduplication_key)
        self.assertEqual(Calendar.objects.count(), 2)

    def test_delete_calendar_success(self):
        """Test successful calendar deletion with associated calendar events and bots."""
        # First create a calendar
        calendar_data = {"platform": CalendarPlatform.GOOGLE, "client_id": "test_client_id_123", "client_secret": "test_client_secret_456", "refresh_token": "test_refresh_token_789", "deduplication_key": "calendar_to_delete"}

        calendar, error = create_calendar(calendar_data, self.project)

        # Verify calendar was created
        self.assertIsNotNone(calendar)
        self.assertIsNone(error)
        calendar_id = calendar.id

        # Create a calendar event associated with this calendar
        calendar_event = CalendarEvent.objects.create(calendar=calendar, platform_uuid="test_event_uuid", start_time="2024-01-01T10:00:00Z", end_time="2024-01-01T11:00:00Z", raw={"title": "Test Meeting"})
        calendar_event_id = calendar_event.id

        # Create a scheduled bot associated with this calendar event
        scheduled_bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://example.com/meeting", state=BotStates.SCHEDULED, calendar_event=calendar_event)
        scheduled_bot_id = scheduled_bot.id

        # Create another bot in different state to verify it's not deleted
        other_bot = Bot.objects.create(project=self.project, name="Other Bot", meeting_url="https://example.com/other", state=BotStates.READY, calendar_event=calendar_event)
        other_bot_id = other_bot.id

        # Verify all objects exist in database
        self.assertTrue(Calendar.objects.filter(id=calendar_id).exists())
        self.assertTrue(CalendarEvent.objects.filter(id=calendar_event_id).exists())
        self.assertTrue(Bot.objects.filter(id=scheduled_bot_id).exists())
        self.assertTrue(Bot.objects.filter(id=other_bot_id).exists())

        # Delete the calendar
        success, error = delete_calendar(calendar)

        # Verify successful deletion
        self.assertTrue(success)
        self.assertIsNone(error)

        # Verify calendar no longer exists in database
        self.assertFalse(Calendar.objects.filter(id=calendar_id).exists())

        # Verify calendar event is deleted (CASCADE relationship)
        self.assertFalse(CalendarEvent.objects.filter(id=calendar_event_id).exists())

        # Verify scheduled bot is deleted (removed by remove_bots_from_calendar)
        self.assertFalse(Bot.objects.filter(id=scheduled_bot_id).exists())

        # Verify non-scheduled bot still exists but calendar_event is set to NULL
        other_bot.refresh_from_db()
        self.assertIsNone(other_bot.calendar_event)
