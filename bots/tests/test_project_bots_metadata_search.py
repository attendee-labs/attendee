from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, Project, ProjectAccess, SessionTypes


class ProjectBotsMetadataSearchTest(TransactionTestCase):
    """Tests for the ProjectBotsView search field and metadata key/value filtering.

    The free-text ``search`` field matches a bot's object_id, meeting_url, and
    name. Metadata is filtered separately via parallel ``metadata_key`` /
    ``metadata_value`` GET parameters, each of which must match a key/value pair
    exactly in the bot's metadata JSON.
    """

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

    # --- Free-text search (object_id, meeting_url, name) ---

    def test_search_matches_name(self):
        response = self.client.get(self.url, {"search": "Web bot"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_search_matches_meeting_url(self):
        response = self.client.get(self.url, {"search": "j/333"})
        self.assertEqual(self._ids(response), {self.bot_none.object_id})

    def test_search_matches_object_id(self):
        response = self.client.get(self.url, {"search": self.bot_api.object_id})
        self.assertEqual(self._ids(response), {self.bot_api.object_id})

    def test_search_does_not_match_metadata_value(self):
        # Metadata values are not part of the free-text search.
        response = self.client.get(self.url, {"search": "us-east"})
        self.assertEqual(self._ids(response), set())

    def test_search_does_not_match_metadata_key(self):
        # Metadata keys are not part of the free-text search.
        response = self.client.get(self.url, {"search": "region"})
        self.assertEqual(self._ids(response), set())

    def test_search_no_match_returns_empty(self):
        response = self.client.get(self.url, {"search": "nonexistent-value"})
        self.assertEqual(self._ids(response), set())

    # --- Metadata key/value filtering ---

    def test_metadata_filter_matches_exact_key_value(self):
        response = self.client.get(self.url, {"metadata_key": "region", "metadata_value": "us-east"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_metadata_filter_matches_other_pair(self):
        response = self.client.get(self.url, {"metadata_key": "customer_id", "metadata_value": "12345"})
        self.assertEqual(self._ids(response), {self.bot_api.object_id})

    def test_metadata_filter_shared_value_matches_multiple_bots(self):
        # Both bots set "source", but with different values, so an exact
        # value match only returns the matching bot.
        response = self.client.get(self.url, {"metadata_key": "source", "metadata_value": "web"})
        self.assertEqual(self._ids(response), {self.bot_web.object_id})

    def test_metadata_filter_requires_exact_value(self):
        # A substring of the stored value should not match.
        response = self.client.get(self.url, {"metadata_key": "customer_id", "metadata_value": "123"})
        self.assertEqual(self._ids(response), set())

    def test_metadata_filter_wrong_value_returns_empty(self):
        response = self.client.get(self.url, {"metadata_key": "region", "metadata_value": "us-west"})
        self.assertEqual(self._ids(response), set())

    def test_metadata_filter_multiple_pairs_are_anded(self):
        # All provided key/value pairs must match the same bot.
        response = self.client.get(
            self.url,
            {"metadata_key": ["source", "customer_id"], "metadata_value": ["api", "12345"]},
        )
        self.assertEqual(self._ids(response), {self.bot_api.object_id})

    def test_metadata_filter_multiple_pairs_no_common_bot(self):
        response = self.client.get(
            self.url,
            {"metadata_key": ["source", "region"], "metadata_value": ["api", "us-east"]},
        )
        self.assertEqual(self._ids(response), set())

    def test_metadata_filter_ignores_pair_with_empty_key(self):
        # Pairs with an empty key are dropped, so this returns all bots.
        response = self.client.get(self.url, {"metadata_key": "", "metadata_value": "api"})
        self.assertEqual(
            self._ids(response),
            {self.bot_api.object_id, self.bot_web.object_id, self.bot_none.object_id},
        )

    def test_no_filters_returns_all(self):
        response = self.client.get(self.url)
        self.assertEqual(
            self._ids(response),
            {self.bot_api.object_id, self.bot_web.object_id, self.bot_none.object_id},
        )
