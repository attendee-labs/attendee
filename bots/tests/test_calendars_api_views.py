from datetime import timedelta
from urllib.parse import urlencode

from django.test import Client, TransactionTestCase
from django.utils import timezone
from rest_framework import status

from accounts.models import Organization
from bots.models import (
    ApiKey,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    CalendarStates,
    Project,
)


class CalendarEventListViewTest(TransactionTestCase):
    """Tests for CalendarEventListView API endpoint."""

    def setUp(self):
        # Create two organizations/projects for isolation testing
        self.organization_a = Organization.objects.create(name="Organization A")
        self.organization_b = Organization.objects.create(name="Organization B")

        self.project_a = Project.objects.create(name="Project A", organization=self.organization_a)
        self.project_b = Project.objects.create(name="Project B", organization=self.organization_b)

        self.api_key_a, self.api_key_a_plain = ApiKey.create(project=self.project_a, name="API Key A")
        self.api_key_b, self.api_key_b_plain = ApiKey.create(project=self.project_b, name="API Key B")

        # Create calendars
        self.calendar_a = Calendar.objects.create(
            project=self.project_a,
            platform=CalendarPlatform.GOOGLE,
            client_id="client_id_a",
            state=CalendarStates.CONNECTED,
            deduplication_key="dedup_key_a",
        )
        self.calendar_b = Calendar.objects.create(
            project=self.project_b,
            platform=CalendarPlatform.MICROSOFT,
            client_id="client_id_b",
            state=CalendarStates.CONNECTED,
            deduplication_key="dedup_key_b",
        )

        # Create calendar events
        now = timezone.now()
        self.event_a1 = CalendarEvent.objects.create(
            calendar=self.calendar_a,
            platform_uuid="event_a1_uuid",
            meeting_url="https://meet.google.com/abc",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            name="Meeting A1",
            raw={"title": "Meeting A1"},
        )
        self.event_a2 = CalendarEvent.objects.create(
            calendar=self.calendar_a,
            platform_uuid="event_a2_uuid",
            meeting_url="https://meet.google.com/def",
            start_time=now + timedelta(hours=3),
            end_time=now + timedelta(hours=4),
            name="Meeting A2",
            raw={"title": "Meeting A2"},
        )
        self.event_b = CalendarEvent.objects.create(
            calendar=self.calendar_b,
            platform_uuid="event_b_uuid",
            meeting_url="https://teams.microsoft.com/xyz",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            name="Meeting B",
            raw={"title": "Meeting B"},
        )

        self.client = Client()

    def _make_authenticated_request(self, method, url, api_key, data=None):
        """Helper method to make authenticated API requests."""
        headers = {"HTTP_AUTHORIZATION": f"Token {api_key}", "HTTP_CONTENT_TYPE": "application/json"}

        if method.upper() == "GET":
            return self.client.get(url, **headers)
        elif method.upper() == "POST":
            return self.client.post(url, data=data, content_type="application/json", **headers)

    def test_list_returns_only_events_from_authenticated_project(self):
        """Test that the list endpoint only returns events from the authenticated project."""
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        event_ids = [e["id"] for e in results]

        # Should see events from project A only
        self.assertIn(self.event_a1.object_id, event_ids)
        self.assertIn(self.event_a2.object_id, event_ids)
        self.assertNotIn(self.event_b.object_id, event_ids)

    def test_filter_by_calendar_id(self):
        """Test filtering events by calendar_id."""
        # Create a second calendar in project A
        calendar_a2 = Calendar.objects.create(
            project=self.project_a,
            platform=CalendarPlatform.GOOGLE,
            client_id="client_id_a2",
            state=CalendarStates.CONNECTED,
            deduplication_key="dedup_key_a2",
        )
        event_a3 = CalendarEvent.objects.create(
            calendar=calendar_a2,
            platform_uuid="event_a3_uuid",
            start_time=timezone.now() + timedelta(hours=5),
            end_time=timezone.now() + timedelta(hours=6),
            name="Meeting A3",
            raw={"title": "Meeting A3"},
        )

        # Filter by calendar_a's object_id
        response = self._make_authenticated_request("GET", f"/api/v1/calendar_events?calendar_id={self.calendar_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        event_ids = [e["id"] for e in results]

        self.assertIn(self.event_a1.object_id, event_ids)
        self.assertIn(self.event_a2.object_id, event_ids)
        self.assertNotIn(event_a3.object_id, event_ids)

    def test_filter_by_calendar_deduplication_key(self):
        """Test filtering events by calendar deduplication key."""
        response = self._make_authenticated_request("GET", f"/api/v1/calendar_events?calendar_deduplication_key={self.calendar_a.deduplication_key}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        event_ids = [e["id"] for e in results]

        self.assertIn(self.event_a1.object_id, event_ids)
        self.assertIn(self.event_a2.object_id, event_ids)

    def test_filter_by_start_time_gte(self):
        """Test filtering events by start_time_gte."""
        # Get events that start at or after event_a1's start_time + 30 minutes
        filter_time = (self.event_a1.start_time + timedelta(minutes=30)).isoformat()
        query_string = urlencode({"start_time_gte": filter_time})
        response = self._make_authenticated_request("GET", f"/api/v1/calendar_events?{query_string}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        event_ids = [e["id"] for e in results]

        # event_a2 starts later, event_a1 should be filtered out
        self.assertIn(self.event_a2.object_id, event_ids)
        self.assertNotIn(self.event_a1.object_id, event_ids)

    def test_invalid_updated_at_gte_format_returns_error(self):
        """Test that invalid updated_at_gte format returns a 400 error."""
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events?updated_at_gte=invalid-date", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.json())
        self.assertIn("updated_at_gte", response.json()["error"])

    def test_updated_after_alias_works_for_backwards_compatibility(self):
        """Test that the deprecated updated_after parameter still works as an alias."""
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events?updated_after=invalid-date", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The error message uses the new parameter name
        self.assertIn("updated_at_gte", response.json()["error"])

    def test_default_ordering_is_descending_updated_at(self):
        """Test that results are ordered by -updated_at by default when no ordering param is passed."""
        # Set distinct updated_at values for the events
        now = timezone.now()
        CalendarEvent.objects.filter(pk=self.event_a1.pk).update(updated_at=now - timedelta(hours=2))
        CalendarEvent.objects.filter(pk=self.event_a2.pk).update(updated_at=now - timedelta(hours=1))

        # Request without ordering parameter
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        event_ids = [e["id"] for e in results]

        # event_a2 was updated more recently, so it should come first (descending order)
        self.assertEqual(event_ids.index(self.event_a2.object_id) < event_ids.index(self.event_a1.object_id), True)

    def test_ordering_parameter(self):
        """Test that ordering parameter correctly orders results."""
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events?ordering=start_time", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])

        # Verify events are ordered by start_time ascending
        if len(results) >= 2:
            # event_a1 starts before event_a2
            event_ids = [e["id"] for e in results]
            self.assertEqual(event_ids.index(self.event_a1.object_id) < event_ids.index(self.event_a2.object_id), True)

    def test_invalid_ordering_returns_error(self):
        """Test that an invalid ordering parameter returns a 400 error."""
        response = self._make_authenticated_request("GET", "/api/v1/calendar_events?ordering=invalid_field", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.json())
        self.assertIn("Invalid ordering", response.json()["error"])

    def test_descending_ordering_reverses_results(self):
        """Test that descending ordering (-start_time) reverses the order compared to ascending."""
        # Get events in ascending order
        response_asc = self._make_authenticated_request("GET", "/api/v1/calendar_events?ordering=start_time", self.api_key_a_plain)
        self.assertEqual(response_asc.status_code, status.HTTP_200_OK)
        results_asc = response_asc.json().get("results", [])

        # Get events in descending order
        response_desc = self._make_authenticated_request("GET", "/api/v1/calendar_events?ordering=-start_time", self.api_key_a_plain)
        self.assertEqual(response_desc.status_code, status.HTTP_200_OK)
        results_desc = response_desc.json().get("results", [])

        # Verify both have the same events but in opposite order
        ids_asc = [e["id"] for e in results_asc]
        ids_desc = [e["id"] for e in results_desc]

        self.assertEqual(len(ids_asc), len(ids_desc))
        self.assertEqual(ids_asc, list(reversed(ids_desc)))
