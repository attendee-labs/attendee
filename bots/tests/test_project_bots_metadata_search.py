from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, Project, ProjectAccess, SessionTypes


class ProjectBotsMetadataSearchTest(TransactionTestCase):
    """Tests that the bot admin search field (ProjectBotsView) also matches bot metadata."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Org", centicredits=10000)
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="testpassword123",
            role=UserRole.ADMIN,
            organization=self.organization,
        )
        self.project = Project.objects.create(name="Project", organization=self.organization)
        ProjectAccess.objects.create(project=self.project, user=self.user)

        # Bot with a metadata key/value pair
        self.bot_api = Bot.objects.create(
            project=self.project,
            name="API bot",
            meeting_url="https://zoom.us/j/111",
            session_type=SessionTypes.BOT,
            metadata={"source": "api", "customer_id": "12345"},
        )
        # Bot with different metadata
        self.bot_web = Bot.objects.create(
            project=self.project,
            name="Web bot",
            meeting_url="https://zoom.us/j/222",
            session_type=SessionTypes.BOT,
            metadata={"source": "web", "region": "us-east"},
        )
        # Bot with no metadata
        self.bot_none = Bot.objects.create(
            project=self.project,
            name="No metadata bot",
            meeting_url="https://zoom.us/j/333",
            session_type=SessionTypes.BOT,
            metadata=None,
        )

        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("bots:project-bots", kwargs={"object_id": self.project.object_id})

    def _ids(self, response):
        return {bot.object_id for bot in response.context["bots"]}

    def test_search_matches_metadata_value(self):
        response = self.client.get(self.url, {"search": "us-east"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_search_matches_metadata_value_substring(self):
        response = self.client.get(self.url, {"search": "12345"})
        self.assertEqual(self._ids(response), {self.bot_api.object_id})

    def test_search_matches_metadata_key_name(self):
        response = self.client.get(self.url, {"search": "region"})
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_search_still_matches_name(self):
        response = self.client.get(self.url, {"search": "Web bot"})
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_search_still_matches_meeting_url(self):
        response = self.client.get(self.url, {"search": "j/333"})
        self.assertEqual(self._ids(response), {self.bot_none.object_id})

    def test_search_matches_object_id(self):
        response = self.client.get(self.url, {"search": self.bot_api.object_id})
        self.assertEqual(self._ids(response), {self.bot_api.object_id})

    def test_search_no_match_returns_empty(self):
        response = self.client.get(self.url, {"search": "nonexistent-value"})
        self.assertEqual(self._ids(response), set())

    def test_no_search_returns_all(self):
        response = self.client.get(self.url)
        self.assertEqual(
            self._ids(response),
            {self.bot_api.object_id, self.bot_web.object_id, self.bot_none.object_id},
        )
