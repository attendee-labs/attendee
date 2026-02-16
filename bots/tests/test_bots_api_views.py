from datetime import timedelta
from urllib.parse import urlencode

from django.test import Client, TransactionTestCase
from django.utils import timezone
from rest_framework import status

from accounts.models import Organization
from bots.models import (
    ApiKey,
    Bot,
    BotStates,
    Participant,
    Project,
    Recording,
    TranscriptionTypes,
    Utterance,
)


class BotListViewTest(TransactionTestCase):
    """Tests for BotListCreateView API endpoint."""

    def setUp(self):
        # Create two organizations/projects for isolation testing
        self.organization_a = Organization.objects.create(name="Organization A")
        self.organization_b = Organization.objects.create(name="Organization B")

        self.project_a = Project.objects.create(name="Project A", organization=self.organization_a)
        self.project_b = Project.objects.create(name="Project B", organization=self.organization_b)

        self.api_key_a, self.api_key_a_plain = ApiKey.create(project=self.project_a, name="API Key A")
        self.api_key_b, self.api_key_b_plain = ApiKey.create(project=self.project_b, name="API Key B")

        # Create bots with different join_at times
        now = timezone.now()
        self.bot_a1 = Bot.objects.create(
            project=self.project_a,
            meeting_url="https://meet.google.com/abc-defg-hij",
            name="Bot A1",
            state=BotStates.SCHEDULED,
            join_at=now + timedelta(hours=1),
            deduplication_key="dedup_a1",
        )
        self.bot_a2 = Bot.objects.create(
            project=self.project_a,
            meeting_url="https://meet.google.com/xyz-uvwx-rst",
            name="Bot A2",
            state=BotStates.JOINING,
            join_at=now + timedelta(hours=3),
            deduplication_key="dedup_a2",
        )
        self.bot_a3 = Bot.objects.create(
            project=self.project_a,
            meeting_url="https://meet.google.com/abc-defg-hij",
            name="Bot A3",
            state=BotStates.JOINED_RECORDING,
            join_at=now + timedelta(hours=5),
        )
        self.bot_b = Bot.objects.create(
            project=self.project_b,
            meeting_url="https://teams.microsoft.com/meeting/123",
            name="Bot B",
            state=BotStates.SCHEDULED,
            join_at=now + timedelta(hours=2),
        )

        self.client = Client()

    def _make_authenticated_request(self, method, url, api_key, data=None):
        """Helper method to make authenticated API requests."""
        headers = {"HTTP_AUTHORIZATION": f"Token {api_key}", "HTTP_CONTENT_TYPE": "application/json"}

        if method.upper() == "GET":
            return self.client.get(url, **headers)
        elif method.upper() == "POST":
            return self.client.post(url, data=data, content_type="application/json", **headers)

    def test_list_returns_only_bots_from_authenticated_project(self):
        """Test that the list endpoint only returns bots from the authenticated project."""
        response = self._make_authenticated_request("GET", "/api/v1/bots", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        # Should see bots from project A only
        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a2.object_id, bot_ids)
        self.assertIn(self.bot_a3.object_id, bot_ids)
        self.assertNotIn(self.bot_b.object_id, bot_ids)

    def test_filter_by_meeting_url(self):
        """Test filtering bots by meeting URL."""
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?meeting_url={self.bot_a1.meeting_url}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        # bot_a1 and bot_a3 have the same meeting URL
        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a3.object_id, bot_ids)
        self.assertNotIn(self.bot_a2.object_id, bot_ids)

    def test_filter_by_deduplication_key(self):
        """Test filtering bots by deduplication key."""
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?deduplication_key={self.bot_a1.deduplication_key}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertNotIn(self.bot_a2.object_id, bot_ids)
        self.assertNotIn(self.bot_a3.object_id, bot_ids)

    def test_filter_by_states(self):
        """Test filtering bots by state."""
        response = self._make_authenticated_request(
            "GET",
            "/api/v1/bots?states=scheduled",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertNotIn(self.bot_a2.object_id, bot_ids)
        self.assertNotIn(self.bot_a3.object_id, bot_ids)

    def test_filter_by_multiple_states(self):
        """Test filtering bots by multiple states."""
        response = self._make_authenticated_request(
            "GET",
            "/api/v1/bots?states=scheduled&states=joining",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a2.object_id, bot_ids)
        self.assertNotIn(self.bot_a3.object_id, bot_ids)

    def test_filter_by_invalid_state_returns_error(self):
        """Test that an invalid state returns a 400 error."""
        response = self._make_authenticated_request(
            "GET",
            "/api/v1/bots?states=invalid_state",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.json())
        self.assertIn("Invalid state", response.json()["error"])

    def test_filter_by_join_at_after(self):
        """Test filtering bots by join_at_after."""
        # Get bots that join after bot_a1's join_at time
        filter_time = (self.bot_a1.join_at + timedelta(minutes=30)).isoformat()
        query_string = urlencode({"join_at_after": filter_time})
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?{query_string}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        # bot_a2 and bot_a3 have join_at times after the filter
        self.assertNotIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a2.object_id, bot_ids)
        self.assertIn(self.bot_a3.object_id, bot_ids)

    def test_filter_by_join_at_before(self):
        """Test filtering bots by join_at_before."""
        # Get bots that join before bot_a3's join_at time
        filter_time = (self.bot_a3.join_at - timedelta(minutes=30)).isoformat()
        query_string = urlencode({"join_at_before": filter_time})
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?{query_string}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        # bot_a1 and bot_a2 have join_at times before the filter
        self.assertIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a2.object_id, bot_ids)
        self.assertNotIn(self.bot_a3.object_id, bot_ids)

    def test_filter_by_join_at_after_and_before(self):
        """Test filtering bots by both join_at_after and join_at_before."""
        # Get bots that join between bot_a1 and bot_a3's join_at times
        after_time = (self.bot_a1.join_at + timedelta(minutes=30)).isoformat()
        before_time = (self.bot_a3.join_at - timedelta(minutes=30)).isoformat()
        query_string = urlencode({"join_at_after": after_time, "join_at_before": before_time})
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?{query_string}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        # Only bot_a2 has a join_at time between the two filters
        self.assertNotIn(self.bot_a1.object_id, bot_ids)
        self.assertIn(self.bot_a2.object_id, bot_ids)
        self.assertNotIn(self.bot_a3.object_id, bot_ids)

    def test_invalid_join_at_after_format_returns_error(self):
        """Test that invalid join_at_after format returns a 400 error."""
        response = self._make_authenticated_request(
            "GET",
            "/api/v1/bots?join_at_after=invalid-date",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.json())
        self.assertIn("join_at_after", response.json()["error"])

    def test_invalid_join_at_before_format_returns_error(self):
        """Test that invalid join_at_before format returns a 400 error."""
        response = self._make_authenticated_request(
            "GET",
            "/api/v1/bots?join_at_before=invalid-date",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.json())
        self.assertIn("join_at_before", response.json()["error"])

    def test_filter_by_join_at_with_bots_without_join_at(self):
        """Test that bots without join_at are excluded when filtering by join_at."""
        # Create a bot without join_at
        bot_no_join_at = Bot.objects.create(
            project=self.project_a,
            meeting_url="https://meet.google.com/no-join-at",
            name="Bot No Join At",
            state=BotStates.JOINING,
            join_at=None,
        )

        # Filter with join_at_after should not include bots without join_at
        filter_time = timezone.now().isoformat()
        query_string = urlencode({"join_at_after": filter_time})
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots?{query_string}",
            self.api_key_a_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", [])
        bot_ids = [b["id"] for b in results]

        self.assertNotIn(bot_no_join_at.object_id, bot_ids)


class TranscriptSplitOnTurnsViewTest(TransactionTestCase):
    """Tests for the transcript API view with split_on_turns parameter."""

    def setUp(self):
        # Create organization, project, and API key
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="Test API Key")

        # Create bot
        self.bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://meet.google.com/test-meeting",
            name="Test Bot",
            state=BotStates.JOINED_RECORDING,
        )

        # Create recording
        self.recording = Recording.objects.create(
            bot=self.bot,
            is_default_recording=True,
            recording_type=self.bot.recording_type(),
            transcription_type=TranscriptionTypes.NON_REALTIME,
        )

        # Create participants
        self.participant_a = Participant.objects.create(
            bot=self.bot,
            uuid="speaker_a_uuid",
            full_name="Speaker A",
        )
        self.participant_b = Participant.objects.create(
            bot=self.bot,
            uuid="speaker_b_uuid",
            full_name="Speaker B",
        )

        self.client = Client()

    def _make_authenticated_request(self, method, url, api_key):
        """Helper method to make authenticated API requests."""
        headers = {"HTTP_AUTHORIZATION": f"Token {api_key}"}
        if method.upper() == "GET":
            return self.client.get(url, **headers)

    def test_transcript_with_split_on_turns(self):
        """Test that the transcript API splits utterances when split_on_turns=true."""
        # Create utterances:
        # Speaker A says "Hello" at 0ms, then "World" at 2000ms (with a pause in between)
        # Speaker B says "Hi" at 1500ms (during Speaker A's pause)

        # Speaker A's utterance with a long pause between words
        Utterance.objects.create(
            recording=self.recording,
            participant=self.participant_a,
            timestamp_ms=0,
            duration_ms=3000,
            transcription={
                "transcript": "Hello World",
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 1.0},
                    {"word": "World", "start": 2.0, "end": 3.0},
                ],
            },
        )

        # Speaker B's utterance during Speaker A's pause
        Utterance.objects.create(
            recording=self.recording,
            participant=self.participant_b,
            timestamp_ms=1500,
            duration_ms=300,
            transcription={
                "transcript": "Hi",
                "words": [
                    {"word": "Hi", "start": 0.0, "end": 0.3},
                ],
            },
        )

        # Make request WITH split_on_turns=true
        response = self._make_authenticated_request(
            "GET",
            f"/api/v1/bots/{self.bot.object_id}/transcript?split_on_turns=true",
            self.api_key_plain,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json()

        # Should have 3 utterances: Speaker A part 1, Speaker B, Speaker A part 2
        self.assertEqual(len(results), 3)

        # First utterance should be Speaker A's "Hello"
        self.assertEqual(results[0]["speaker_uuid"], "speaker_a_uuid")
        self.assertEqual(results[0]["transcription"]["transcript"], "Hello")
        self.assertEqual(len(results[0]["transcription"]["words"]), 1)
        self.assertEqual(results[0]["transcription"]["words"][0]["word"], "Hello")

        # Second utterance should be Speaker B's "Hi"
        self.assertEqual(results[1]["speaker_uuid"], "speaker_b_uuid")
        self.assertEqual(results[1]["transcription"]["transcript"], "Hi")

        # Third utterance should be Speaker A's "World"
        self.assertEqual(results[2]["speaker_uuid"], "speaker_a_uuid")
        self.assertEqual(results[2]["transcription"]["transcript"], "World")
        self.assertEqual(len(results[2]["transcription"]["words"]), 1)
        self.assertEqual(results[2]["transcription"]["words"][0]["word"], "World")
